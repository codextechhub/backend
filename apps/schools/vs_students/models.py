"""The student record and everything that hangs off it.

Seven models. Every one carries its own ``tenant`` foreign key, even where its
parent already has one, because :class:`vs_rbac.managers.TenantAwareManager`
filters on a model's own ``tenant`` or ``branch`` field and returns everything
otherwise. Reaching the tenant through a parent is not scoping.

Two of them carry a branch and they carry it differently. ``Student.branch`` is
**non-null**: every school has a branch and every child attends one, so there
is no school-wide student and no shared row to leak across a boundary.
``StudentPromotionBatch.branch`` is nullable, where a null means the run
covered the school as a whole. ``Guardian`` carries no branch at all, and that
absence is the load-bearing one: one guardian row serves siblings at different
branches of one school, which is the whole reason the record is tenant-scoped
rather than student-scoped.

There is deliberately no denormalised ``current_class`` on Student. The active
enrolment is the single source of truth and a second copy of it is a second
thing that can be wrong; the cost is a join, paid once by prefetching.

FRD M11 v2.4 section 7.
"""
from __future__ import annotations

from django.conf import settings
from django.db import models
from django.db.models import Q
from django.db.models.functions import Lower
from django.utils import timezone

from vs_rbac.managers import TenantAwareManager

from .constants import (
    DocumentType,
    EnrolmentOutcome,
    Gender,
    Relationship,
    StudentStatus,
    TransferReason,
)


def student_photo_path(instance, filename):
    return f"students/{instance.tenant_id}/photos/{filename}"


def guardian_photo_path(instance, filename):
    return f"guardians/{instance.tenant_id}/photos/{filename}"


def student_document_path(instance, filename):
    return (
        f"students/{instance.tenant_id}/documents/"
        f"{instance.student_id}/{instance.document_type.lower()}/{filename}"
    )


class _Owned(models.Model):
    """The managers that enforce tenant ownership, and the timestamps.

    ``objects`` applies the ambient tenant eagerly; ``all_objects`` does not
    and exists for migrations, seeders and the constraint-level tests that have
    to write across tenants on purpose. ``base_manager_name`` is the unfiltered
    one so related traversal does not silently drop rows.

    The ``tenant`` column is not declared here on purpose: one declaration
    would mean one ``related_name`` shared by seven models, so it would have to
    be ``"+"``, and that disables the reverse accessor entirely. Each concrete
    model declares its own with the name its FRD section gives it.
    """

    created_at = models.DateTimeField(default=timezone.now, editable=False)
    updated_at = models.DateTimeField(auto_now=True)

    objects = TenantAwareManager()
    all_objects = models.Manager()

    class Meta:
        abstract = True
        default_manager_name = "objects"
        base_manager_name = "all_objects"


class Student(_Owned):
    """One row per child, for the life of their time at the school and after it."""

    tenant = models.ForeignKey(
        "vs_tenants.Tenant", on_delete=models.PROTECT, related_name="students",
    )
    #: Non-null. PROTECT costs nothing here: a branch is closed, never deleted.
    branch = models.ForeignKey(
        "vs_tenants.Branch", on_delete=models.PROTECT, related_name="students",
    )

    #: The school's own number for the child. Nothing generates it and the
    #: column enforces no format. Whether one is *required*, and whether it
    #: must match a pattern, is the school's own rule and lives in vs_config
    #: (constants.CFG_ADM_*), so a school that has configured nothing keeps the
    #: permissive behaviour. Unique within the tenant when present, never
    #: across the platform: two schools may legitimately number from one.
    student_number = models.CharField(max_length=32, blank=True, default="")

    first_name = models.CharField(max_length=100)
    middle_name = models.CharField(max_length=100, blank=True, default="")
    last_name = models.CharField(max_length=100)
    date_of_birth = models.DateField()
    gender = models.CharField(max_length=10, choices=Gender.choices)

    nationality = models.CharField(max_length=60, blank=True, default="")
    state_of_origin = models.CharField(max_length=60, blank=True, default="")
    address = models.TextField(blank=True, default="")
    #: The student's own number, not a login. No credential is ever sent to it.
    phone = models.CharField(max_length=32, blank=True, default="")
    email = models.EmailField(blank=True, default="")
    previous_school = models.CharField(max_length=200, blank=True, default="")

    #: A FileField, for the reason StudentDocument.file gives: it is what binds
    #: the stored row to this record and lets the media view refuse another
    #: school's caller.
    photo = models.FileField(upload_to=student_photo_path, blank=True)

    # ── medical ────────────────────────────────────────────────────────────
    # Five fields, not one free-text box: the profile shows five labelled rows
    # and the edit drawer five inputs. The first three are gated on
    # school.students.view_sensitive; the emergency contact deliberately is
    # not, because a contact only an administrator can read is useless in the
    # emergency it exists for. None of the five is ever in a list serializer.
    blood_group = models.CharField(max_length=4, blank=True, default="")
    allergies = models.CharField(max_length=200, blank=True, default="")
    conditions = models.CharField(max_length=200, blank=True, default="")
    emergency_contact_name = models.CharField(max_length=150, blank=True, default="")
    emergency_contact_phone = models.CharField(max_length=32, blank=True, default="")

    status = models.CharField(
        max_length=12, choices=StudentStatus.choices,
        default=StudentStatus.APPLICANT, db_index=True,
    )
    enrolment_date = models.DateField(default=timezone.localdate)

    #: Application facts. Null for a student who was never an applicant.
    #: Without them an applicant is a record with blank everything and the
    #: Applicants board has nothing to show or sort by.
    applied_for = models.ForeignKey(
        "vs_academics.Level", on_delete=models.PROTECT,
        null=True, blank=True, related_name="+",
    )
    applied_on = models.DateField(null=True, blank=True)

    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL,
        null=True, blank=True, related_name="+",
    )

    class Meta(_Owned.Meta):
        constraints = [
            models.UniqueConstraint(
                Lower("student_number"), "tenant",
                condition=~Q(student_number=""),
                name="uq_student_tenant_number_ci",
            ),
        ]
        indexes = [
            models.Index(fields=["tenant", "branch", "status"]),
            models.Index(fields=["tenant", "status"]),
            models.Index(fields=["tenant", "last_name", "first_name"]),
        ]
        ordering = ["last_name", "first_name"]

    def __str__(self):
        return self.full_name

    @property
    def full_name(self) -> str:
        parts = [self.first_name, self.middle_name, self.last_name]
        return " ".join(p for p in parts if p)


class Guardian(_Owned):
    """A person, not a login.

    This distinction is the single most load-bearing thing in the module. The
    guardian's *account*, where they have one, is an ordinary ``vs_user.User``
    of this tenant and may already hold a staff role: within one school, one
    person is one account, and giving a teacher a second one so she can see her
    own child is exactly the defect ``user`` exists to prevent.

    No branch column, deliberately. One row serves siblings at two branches of
    one school, and the parent's own User row carries a branch only where that
    person also works for the school.
    """

    tenant = models.ForeignKey(
        "vs_tenants.Tenant", on_delete=models.PROTECT, related_name="guardians",
    )
    full_name = models.CharField(max_length=150)
    phone = models.CharField(max_length=32)
    #: The parent's real address, and the one a login would be issued to.
    #: Optional on the row, because a school may hold a guardian it has no
    #: address for; required before that guardian can be given an account.
    email = models.EmailField(blank=True, default="")
    occupation = models.CharField(max_length=100, blank=True, default="")
    #: The guardian's own address, which is not always the child's.
    address = models.TextField(blank=True, default="")
    #: A face for the person collecting a child. Always optional: a school
    #: holds guardians it has never met, and a rule that demanded a photograph
    #: before a parent could be recorded would be worked around with a blank
    #: file, exactly as the document checklist would be.
    photo = models.FileField(upload_to=guardian_photo_path, blank=True)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL,
        null=True, blank=True, related_name="guardian_profiles",
    )

    class Meta(_Owned.Meta):
        constraints = [
            # Without this, adding the second sibling creates a second Guardian
            # row and the family is split in two, silently, and stays split.
            models.UniqueConstraint(
                Lower("email"), "tenant", condition=~Q(email=""),
                name="uq_guardian_tenant_email_ci",
            ),
            # Within one school, one person is one account.
            models.UniqueConstraint(
                "tenant", "user", condition=Q(user__isnull=False),
                name="uq_guardian_tenant_user",
            ),
        ]
        indexes = [models.Index(fields=["tenant", "phone"])]
        ordering = ["full_name"]

    def __str__(self):
        return self.full_name


class StudentGuardian(_Owned):
    """Many-to-many, with exactly one primary contact per student."""

    tenant = models.ForeignKey(
        "vs_tenants.Tenant", on_delete=models.PROTECT, related_name="+",
    )
    student = models.ForeignKey(
        Student, on_delete=models.CASCADE, related_name="guardian_links",
    )
    guardian = models.ForeignKey(
        Guardian, on_delete=models.CASCADE, related_name="student_links",
    )
    relationship = models.CharField(max_length=16, choices=Relationship.choices)
    is_primary = models.BooleanField(default=False)

    class Meta(_Owned.Meta):
        constraints = [
            models.UniqueConstraint(
                fields=["student", "guardian"], name="uq_studentguardian_pair",
            ),
            # The partial constraint that makes "exactly one primary" true in
            # the database and not only in a service, so two concurrent writes
            # cannot leave a child with two primary contacts.
            models.UniqueConstraint(
                fields=["student"], condition=Q(is_primary=True),
                name="uq_studentguardian_one_primary",
            ),
        ]
        indexes = [models.Index(fields=["tenant", "guardian"])]
        ordering = ["-is_primary", "id"]


class ClassEnrolment(_Owned):
    """A student's placement in a class for a session.

    Rows are never deleted. The history is the point, and it is what the
    profile's class-history trail reads.
    """

    tenant = models.ForeignKey(
        "vs_tenants.Tenant", on_delete=models.PROTECT,
        related_name="class_enrolments",
    )
    student = models.ForeignKey(
        Student, on_delete=models.CASCADE, related_name="enrolments",
    )
    #: PROTECT, never CASCADE. A class is archived and not deleted; if a delete
    #: route ever appears it must not take a roster with it.
    school_class = models.ForeignKey(
        "vs_academics.SchoolClass", on_delete=models.PROTECT,
        related_name="enrolments",
    )
    session = models.ForeignKey(
        "vs_academics.AcademicSession", on_delete=models.PROTECT,
        related_name="enrolments",
    )
    is_active = models.BooleanField(default=True, db_index=True)

    #: When the placement takes effect for the school, which is not
    #: ``assigned_at``: a transfer agreed on Friday to start on Monday has two
    #: different dates, and a register that uses the wrong one is wrong for a
    #: weekend.
    effective_date = models.DateField(default=timezone.localdate)
    #: Blank on a first placement, where there is nothing to explain. Required
    #: on a transfer: a class move is the one record-changing act that would
    #: otherwise carry no explanation.
    reason = models.CharField(
        max_length=24, choices=TransferReason.choices, blank=True, default="",
    )
    outcome = models.CharField(
        max_length=12, choices=EnrolmentOutcome.choices,
        default=EnrolmentOutcome.CURRENT,
    )

    assigned_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL,
        null=True, blank=True, related_name="+",
    )
    assigned_at = models.DateTimeField(default=timezone.now, editable=False)
    #: Set when is_active goes false. Without it a historical row records that
    #: it ended but not when, which makes "which class was this child in last
    #: March" unanswerable.
    ended_at = models.DateTimeField(null=True, blank=True)

    class Meta(_Owned.Meta):
        constraints = [
            models.UniqueConstraint(
                fields=["student", "session"], condition=Q(is_active=True),
                name="uq_enrolment_one_active_per_session",
            ),
        ]
        indexes = [
            models.Index(fields=["school_class", "is_active"]),
            models.Index(fields=["tenant", "session", "is_active"]),
        ]
        ordering = ["-assigned_at"]


class StudentStatusLog(_Owned):
    """Append-only history of status transitions.

    Written by the state machine and by nothing else. This is the student's own
    history, read by a school user on the profile screen; it is not a
    substitute for vs_audit and does not replace it. Both are required and
    neither is the other's backup.
    """

    tenant = models.ForeignKey(
        "vs_tenants.Tenant", on_delete=models.PROTECT, related_name="+",
    )
    student = models.ForeignKey(
        Student, on_delete=models.CASCADE, related_name="status_logs",
    )
    from_status = models.CharField(
        max_length=12, choices=StudentStatus.choices, blank=True, default="",
    )
    to_status = models.CharField(max_length=12, choices=StudentStatus.choices)
    reason = models.CharField(max_length=200, blank=True, default="")
    #: When the change takes effect for the school, as distinct from
    #: ``changed_at``, which is when the system recorded it.
    effective_date = models.DateField(default=timezone.localdate)
    #: Required when to_status is TRANSFERRED, blank otherwise. Free text: the
    #: receiving school is not a tenant of this platform.
    destination_school = models.CharField(max_length=200, blank=True, default="")
    changed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL,
        null=True, blank=True, related_name="+",
    )
    changed_at = models.DateTimeField(default=timezone.now, editable=False)

    class Meta(_Owned.Meta):
        indexes = [models.Index(fields=["student", "changed_at"])]
        ordering = ["-changed_at"]


class StudentPromotionBatch(_Owned):
    """One row per promotion run, for the summary and for restartability."""

    tenant = models.ForeignKey(
        "vs_tenants.Tenant", on_delete=models.PROTECT, related_name="+",
    )
    #: Null means the run covered the school as a whole.
    branch = models.ForeignKey(
        "vs_tenants.Branch", on_delete=models.PROTECT,
        null=True, blank=True, related_name="+",
    )
    from_session = models.ForeignKey(
        "vs_academics.AcademicSession", on_delete=models.PROTECT, related_name="+",
    )
    to_session = models.ForeignKey(
        "vs_academics.AcademicSession", on_delete=models.PROTECT, related_name="+",
    )
    initiated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL,
        null=True, blank=True, related_name="+",
    )
    #: The platform already records the run's status, timings and failure
    #: through TrackedTask; this points at that row rather than duplicating its
    #: five status values here.
    background_job = models.ForeignKey(
        "core.BackgroundJob", on_delete=models.SET_NULL,
        null=True, blank=True, related_name="+",
    )

    total = models.PositiveIntegerField(default=0)
    promoted = models.PositiveIntegerField(default=0)
    repeated = models.PositiveIntegerField(default=0)
    graduated = models.PositiveIntegerField(default=0)
    #: Considered and left where they were.
    held = models.PositiveIntegerField(default=0)
    #: Never considered at all.
    excluded = models.PositiveIntegerField(default=0)
    failed = models.PositiveIntegerField(default=0)

    class Meta(_Owned.Meta):
        indexes = [models.Index(fields=["tenant", "from_session"])]
        ordering = ["-created_at"]


class StudentDocument(_Owned):
    """A document a school holds against a child.

    The bytes live in ``core.StoredFile``, which is the platform's
    database-backed storage and is already served with authentication. This row
    records what one of those files is, and whose.
    """

    tenant = models.ForeignKey(
        "vs_tenants.Tenant", on_delete=models.PROTECT,
        related_name="student_documents",
    )
    student = models.ForeignKey(
        Student, on_delete=models.CASCADE, related_name="documents",
    )
    document_type = models.CharField(max_length=24, choices=DocumentType.choices)
    #: An ordinary FileField on the platform's database-backed storage, not a
    #: hand-rolled foreign key to StoredFile. That is what gets the row bound
    #: to this record by ``core.binding``, retired when it is replaced, and
    #: refused to a caller of another tenant by ``core.media.authorize`` - all
    #: of which a bare FK skips, leaving a file that is served to anybody
    #: signed in who has ever seen the URL.
    file = models.FileField(upload_to=student_document_path)
    original_name = models.CharField(max_length=255)
    content_type = models.CharField(max_length=120, blank=True, default="")
    size = models.PositiveIntegerField(default=0)
    uploaded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL,
        null=True, blank=True, related_name="+",
    )
    uploaded_at = models.DateTimeField(default=timezone.now, editable=False)

    class Meta(_Owned.Meta):
        constraints = [
            # At most one of each type per student, so attaching a second
            # replaces the first rather than silently keeping both.
            models.UniqueConstraint(
                fields=["student", "document_type"],
                name="uq_student_document_type",
            ),
        ]
        indexes = [models.Index(fields=["tenant", "student"])]
        ordering = ["document_type"]

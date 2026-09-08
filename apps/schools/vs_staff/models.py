"""The staff record and everything that hangs off it.

Six models. Every one carries its own ``tenant`` foreign key, even where its
parent already has one, because :class:`vs_rbac.managers.TenantAwareManager`
filters on a model's own ``tenant`` or ``branch`` field and returns everything
otherwise. Reaching the tenant through a parent is not scoping.

Only :class:`StaffProfile` carries a branch, and it means the person's
**posting**: where they are based, nullable, where a null is the first-class
value "across the whole school" rather than missing data. It is never the
authority on what they can reach. Reach comes from their role grants and is
answered by ``vs_rbac.scoping.visible_branch_ids``, which can express Ikeja and
Lekki but not Yaba where a single column cannot. :class:`TeachingAssignment`
deliberately has no branch at all: it points at a class, a class has one, and
asking twice would let the two disagree.

Two things this module does not own, and would produce a second record of if it
did. The login, the invitation and the account's state are ``vs_user``'s, and
FR-008 calls those services rather than writing ``User.status``. The role
catalogue and the grant are ``vs_rbac``'s, and this module reads grants and
writes none.

FRD M12 v2.1 section 7.
"""
from __future__ import annotations

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models
from django.db.models import Q
from django.utils import timezone

from vs_rbac.managers import TenantAwareManager

from .constants import (
    DocumentType,
    EmploymentStatus,
    EmploymentType,
    LeaveStatus,
    LeaveType,
    OFF_ROLL_STATUSES,
    TeachingPart,
)


def staff_photo_path(instance, filename):
    return f"staff/{instance.tenant_id}/photos/{filename}"


def staff_document_path(instance, filename):
    return (
        f"staff/{instance.tenant_id}/documents/"
        f"{instance.staff_id}/{instance.document_type.lower()}/{filename}"
    )


class _Owned(models.Model):
    """The managers that enforce tenant ownership, and the timestamps.

    ``objects`` applies the ambient tenant eagerly; ``all_objects`` does not and
    exists for migrations, seeders and the constraint-level tests that have to
    write across tenants on purpose. ``base_manager_name`` is the unfiltered one
    so related traversal does not silently drop rows.

    The ``tenant`` column is not here, for the reason ``vs_academics`` gives:
    one declaration in a base would mean one ``related_name`` shared by six
    models, so it would have to be ``"+"``, and that disables the reverse
    accessor every caller of ``tenant.staff_profiles`` needs.
    """

    created_at = models.DateTimeField(default=timezone.now, editable=False)
    updated_at = models.DateTimeField(auto_now=True)

    objects = TenantAwareManager()
    all_objects = models.Manager()

    class Meta:
        abstract = True
        default_manager_name = "objects"
        base_manager_name = "all_objects"


class StaffProfile(_Owned):
    """One row per member of staff, one-to-one with their account.

    Created in the same transaction as the account and never before it: there is
    no user-less staff record and this module must not be able to produce one.

    Two things are deliberately absent and must stay absent. There is no
    ``is_teaching`` flag, because whether somebody teaches is answered by
    whether they hold a teaching assignment, which is a fact rather than a
    claim, and a flag would be a second answer that could disagree with the
    first. And there is no salary, band or bank detail: payroll is
    ``vs_finance``'s, it already carries a branch and a scope setting, and a
    second figure here would be the one a bursar read on the day they were asked
    what somebody earns.
    """

    tenant = models.ForeignKey(
        "vs_tenants.Tenant", on_delete=models.PROTECT, related_name="staff_profiles",
    )
    #: PROTECT rather than CASCADE: deleting a school's account is refused while
    #: a staff record points at it, because the employment history is the thing
    #: a school will be asked for years later.
    user = models.OneToOneField(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT,
        related_name="staff_profile",
    )
    #: The posting. NULL means across the whole school, exactly as it does on
    #: ``User.branch``, and is a first-class value rather than a gap.
    branch = models.ForeignKey(
        "vs_tenants.Branch", on_delete=models.PROTECT, null=True, blank=True,
        related_name="staff_profiles",
    )
    #: The number the SCHOOL chose, in whatever format it uses. BFS/STF/0012 is
    #: not a shape the platform imposes, and a school that does not number its
    #: staff is not made to invent one, which is why blank is allowed and the
    #: uniqueness is partial.
    staff_number = models.CharField(max_length=32, blank=True, default="")
    #: What this person is, which is not what their role grants. Two people
    #: holding Teacher may be a form teacher and a head of department.
    job_title = models.CharField(max_length=150, blank=True, default="")
    employment_type = models.CharField(
        max_length=16, choices=EmploymentType.choices, blank=True, default="",
    )
    #: Never derived, never written except through ``services.employment``.
    employment_status = models.CharField(
        max_length=16, choices=EmploymentStatus.choices,
        default=EmploymentStatus.INVITED,
    )
    #: When they started, which is not ``User.created_at``. Tenure derives from
    #: it and is absent rather than guessed when it is empty.
    hire_date = models.DateField(null=True, blank=True)
    #: The last working day, set by the RESIGNED and TERMINATED transitions and
    #: by nothing else.
    exit_date = models.DateField(null=True, blank=True)

    middle_name = models.CharField(max_length=100, blank=True, default="")
    date_of_birth = models.DateField(null=True, blank=True)
    #: One image, over the DB-backed storage that already exists and the
    #: authenticated media view that already serves it. A CV is a
    #: :class:`StaffDocument` rather than a second image field.
    photo = models.ImageField(upload_to=staff_photo_path, blank=True, null=True)

    #: SET_NULL because a record must survive the departure of whoever made it.
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="created_staff_profiles",
    )

    class Meta(_Owned.Meta):
        constraints = [
            # Partial, so a school that numbers nobody is not forced to invent
            # one number per person to satisfy the index.
            models.UniqueConstraint(
                fields=["tenant", "staff_number"],
                condition=~Q(staff_number=""),
                name="uq_staff_number_per_tenant",
            ),
        ]
        indexes = [
            models.Index(fields=["tenant", "employment_status"]),
            models.Index(fields=["tenant", "branch"]),
            models.Index(fields=["tenant", "staff_number"]),
        ]
        ordering = ["-created_at", "-id"]

    def __str__(self):
        return f"StaffProfile<{self.user_id}:{self.job_title or 'staff'}>"

    def clean(self):
        """Refuse a branch or a user belonging to another tenant.

        Both are reachable only by a crafted write, and both are the kind of
        cross-tenant leak that is cheap to prevent and expensive to find.
        """
        super().clean()
        if self.branch_id and self.branch.tenant_id != self.tenant_id:
            raise ValidationError("That branch belongs to a different school.")
        if self.user_id and self.user.tenant_id != self.tenant_id:
            raise ValidationError("That account belongs to a different school.")

    @property
    def is_on_roll(self) -> bool:
        """Still employed. Not a statement about signing in."""
        return self.employment_status not in OFF_ROLL_STATUSES


class StaffEmploymentEvent(_Owned):
    """Append-only. One row per employment transition.

    Written in the same transaction as the transition it records, never edited
    and never deleted, which ``save`` and ``delete`` enforce the way
    ``RBACAuditLog`` already does.

    Two records of one transition is not duplication, and it is worth saying why
    before somebody removes one. This is a domain log a school reads on a
    profile, listable and filterable under this module's own permission key. The
    ``vs_audit`` event is the platform's tamper-evident trail, read by the
    console under an audit key, and it also records the account changes that
    have no employment event at all.
    """

    tenant = models.ForeignKey(
        "vs_tenants.Tenant", on_delete=models.PROTECT,
        related_name="staff_employment_events",
    )
    #: PROTECT, because the log is the reason the record survives an exit.
    staff = models.ForeignKey(
        StaffProfile, on_delete=models.PROTECT, related_name="employment_events",
    )
    #: Blank on the first row, which records the creation.
    from_status = models.CharField(
        max_length=16, choices=EmploymentStatus.choices, blank=True, default="",
    )
    to_status = models.CharField(max_length=16, choices=EmploymentStatus.choices)
    #: Required by the service for SUSPENDED, RESIGNED and TERMINATED, and
    #: optional otherwise. Required there rather than at the column, because the
    #: column is also written by the activation path, which has no reason to
    #: give.
    reason = models.CharField(max_length=200, blank=True, default="")
    #: May be in the past, and may be in the future for a resignation announced
    #: in advance.
    effective_date = models.DateField()
    last_working_day = models.DateField(null=True, blank=True)
    note = models.TextField(blank=True, default="")
    changed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="staff_employment_changes",
    )

    class Meta(_Owned.Meta):
        indexes = [models.Index(fields=["tenant", "staff", "-created_at"])]
        ordering = ["-created_at", "-id"]

    def __str__(self):
        return f"{self.from_status or 'NEW'} to {self.to_status}"

    def save(self, *args, **kwargs):
        """Refuse an update. An append-only log that can be edited is not one."""
        if self.pk is not None:
            raise ValidationError("Employment events cannot be changed once written.")
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValidationError("Employment events cannot be deleted.")


class StaffQualification(_Owned):
    """A degree or a certification, as the school typed it.

    There is no verified flag, no verifier and no verified_at. Nothing verifies
    a qualification, there is no register to verify one against, and a field
    somebody sets by hand is read by everybody else as a check that was made.

    There is no uniqueness constraint either. A person may legitimately hold two
    qualifications with the same name from different institutions, and one from
    the same institution in two years.
    """

    tenant = models.ForeignKey(
        "vs_tenants.Tenant", on_delete=models.PROTECT,
        related_name="staff_qualifications",
    )
    #: CASCADE, unlike the employment log: a qualification is a detail of the
    #: record rather than the history a school must keep, and the record itself
    #: is never deleted while somebody has worked there.
    staff = models.ForeignKey(
        StaffProfile, on_delete=models.CASCADE, related_name="qualifications",
    )
    #: Free text. B.Ed Mathematics, NCE, PGDE, a Cambridge certificate. A closed
    #: vocabulary would be wrong within a year and wrong for half of Nigeria
    #: immediately.
    qualification = models.CharField(max_length=200)
    institution = models.CharField(max_length=200, blank=True, default="")
    year_obtained = models.PositiveSmallIntegerField(null=True, blank=True)
    note = models.TextField(blank=True, default="")
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="created_staff_qualifications",
    )

    class Meta(_Owned.Meta):
        indexes = [models.Index(fields=["tenant", "staff"])]
        ordering = ["-year_obtained", "qualification"]

    def __str__(self):
        return self.qualification


class StaffDocument(_Owned):
    """A file held against a person.

    New as a table and not new as a capability: ``core.storage.DatabaseStorage``
    already keeps file bytes in ``StoredFile`` and ``MediaView`` already serves
    them with authentication in every environment, so this is a new parent for
    storage that works rather than a new storage.

    The most sensitive payload this module serves, and the one most likely to be
    exposed by accident, because a file URL looks like a URL rather than like a
    record. The serializer emits the media path and never a signed or guessable
    direct link, and a person may always read their own and never anybody else's
    without ``school.teachers.view``.

    There is no verification, approval or expiry state. An expiry on a teaching
    certificate is a real thing a school tracks and is a decision nobody has
    taken; FRD section 14, decision 9.
    """

    tenant = models.ForeignKey(
        "vs_tenants.Tenant", on_delete=models.PROTECT,
        related_name="staff_documents",
    )
    staff = models.ForeignKey(
        StaffProfile, on_delete=models.CASCADE, related_name="documents",
    )
    document_type = models.CharField(max_length=32, choices=DocumentType.choices)
    #: What this file is, in the school's words.
    title = models.CharField(max_length=200)
    file = models.FileField(upload_to=staff_document_path)
    uploaded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="uploaded_staff_documents",
    )

    class Meta(_Owned.Meta):
        indexes = [models.Index(fields=["tenant", "staff", "document_type"])]
        ordering = ["-created_at", "-id"]

    def __str__(self):
        return self.title


class LeaveRequest(_Owned):
    """An absence, applied for and decided.

    The decision half is the workflow engine's: this row declares
    ``workflow_document_type`` and is submitted on creation, and its status is
    written by the handler's callbacks rather than by any serializer. A status a
    form can set is a status that disagrees with the instance that decided it.

    ``branch`` is a property rather than a column so the engine's branch to
    tenant to platform template cascade resolves at the posting the person
    actually holds, without this table keeping a second copy of it.
    """

    #: Read by ``vs_workflow.services.submission``. Not a column.
    workflow_document_type = "schools.leave_request"

    tenant = models.ForeignKey(
        "vs_tenants.Tenant", on_delete=models.PROTECT,
        related_name="staff_leave_requests",
    )
    #: PROTECT: leave taken is part of the employment history.
    staff = models.ForeignKey(
        StaffProfile, on_delete=models.PROTECT, related_name="leave_requests",
    )
    leave_type = models.CharField(max_length=20, choices=LeaveType.choices)
    start_date = models.DateField()
    end_date = models.DateField()
    #: Stored rather than derived, and the one derived-looking value in this
    #: module that is deliberately a column. Working days are not calendar days,
    #: a school's teaching week is recorded nowhere, and a school that runs
    #: Saturday classes and one that does not would get different answers from
    #: one formula. The service defaults it to the inclusive calendar span and
    #: lets the school correct it.
    days = models.PositiveSmallIntegerField()
    note = models.TextField(blank=True, default="")
    status = models.CharField(
        max_length=12, choices=LeaveStatus.choices, default=LeaveStatus.PENDING,
    )
    #: Stamped by the same callback that writes the status. WHO decided it is
    #: the workflow instance's and is read through it rather than copied, so
    #: there is no second copy of an approver's name to keep in step.
    decided_at = models.DateTimeField(null=True, blank=True)
    #: Who filed it, which is not always who it is for: a teacher applies for
    #: their own and an administrator records one on somebody's behalf.
    requested_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="requested_leave",
    )

    class Meta(_Owned.Meta):
        indexes = [
            models.Index(fields=["tenant", "staff", "start_date"]),
            models.Index(fields=["tenant", "start_date", "end_date"]),
            models.Index(fields=["tenant", "status"]),
        ]
        ordering = ["-start_date", "-id"]

    def __str__(self):
        return f"{self.get_leave_type_display()} {self.start_date} to {self.end_date}"

    @property
    def branch(self):
        """The posting the approver is sought at. Derived, never stored."""
        return self.staff.branch

    def clean(self):
        super().clean()
        if self.start_date and self.end_date and self.end_date < self.start_date:
            raise ValidationError("Leave cannot end before it starts.")

    def display_status(self, today=None) -> str:
        """What a screen shows, which has one more value than the column.

        An approved absence whose end date has passed reads Completed. It is
        derived here rather than stored, because a value that follows from a
        date it sits beside is a second thing that can be wrong.
        """
        today = today or timezone.localdate()
        if self.status == LeaveStatus.APPROVED and self.end_date < today:
            return "COMPLETED"
        return self.status


class TeachingAssignment(_Owned):
    """This person teaches this subject to this class, this session.

    The teacher half of the assignment layer, which M13 withdrew and recorded as
    waiting for this module.

    The one thing this table must not become is a second timetable. It says who
    teaches what; it says nothing about when. A school with a paper timetable
    will want to record a period here, and the moment it does, two tables answer
    where somebody is on Wednesday afternoon and the clash rules read only one
    of them.
    """

    tenant = models.ForeignKey(
        "vs_tenants.Tenant", on_delete=models.PROTECT,
        related_name="teaching_assignments",
    )
    #: PROTECT, so a record cannot be removed while a class still believes it is
    #: covered.
    staff = models.ForeignKey(
        StaffProfile, on_delete=models.PROTECT, related_name="teaching_assignments",
    )
    school_class = models.ForeignKey(
        "vs_academics.SchoolClass", on_delete=models.PROTECT,
        related_name="teaching_assignments",
    )
    subject = models.ForeignKey(
        "vs_academics.Subject", on_delete=models.PROTECT,
        related_name="teaching_assignments",
    )
    #: PROTECT and not CASCADE: an archived year's assignments are the record of
    #: who taught what, and M13 archives a session rather than deleting it.
    session = models.ForeignKey(
        "vs_academics.AcademicSession", on_delete=models.PROTECT,
        related_name="teaching_assignments",
    )
    part = models.CharField(
        max_length=10, choices=TeachingPart.choices, default=TeachingPart.LEAD,
    )
    assigned_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="made_teaching_assignments",
    )

    class Meta(_Owned.Meta):
        constraints = [
            # Two teachers may share a subject in a class, which is ordinary
            # where a class is split. The same teacher recorded twice is a
            # duplicate.
            models.UniqueConstraint(
                fields=["tenant", "session", "school_class", "subject", "staff"],
                name="uq_teaching_assignment",
            ),
            # Assistants are unbounded and a lead is not. This is what makes
            # "no lead" a countable state rather than a judgement.
            models.UniqueConstraint(
                fields=["tenant", "session", "school_class", "subject"],
                condition=Q(part=TeachingPart.LEAD),
                name="uq_teaching_lead",
            ),
        ]
        indexes = [
            models.Index(fields=["tenant", "session", "staff"]),
            models.Index(fields=["tenant", "session", "school_class"]),
            models.Index(fields=["tenant", "session", "subject"]),
        ]
        ordering = ["subject__name", "school_class__name"]

    def __str__(self):
        return f"{self.staff_id}: {self.subject_id} to {self.school_class_id}"

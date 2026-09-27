"""The staff record and everything that hangs off it.

Ten models. Every one carries its own ``tenant`` foreign key, even where its
parent already has one, because :class:`vs_rbac.managers.TenantAwareManager`
filters on a model's own ``tenant`` or ``branch`` field and returns everything
otherwise. Reaching the tenant through a parent is not scoping.

Two carry a branch. :class:`StaffProfile`'s means the person's
**posting**: where they are based, nullable, where a null is the first-class
value "across the whole school" rather than missing data. It is never the
authority on what they can reach. Reach comes from their role grants and is
answered by ``vs_rbac.scoping.visible_branch_ids``, which can express Ikeja and
Lekki but not Yaba where a single column cannot. :class:`TeachingAssignment`
deliberately has no branch at all: it points at a class, a class has one, and
asking twice would let the two disagree.

:class:`StaffOrgNode` carries the other, and it means which part of the school a
unit of the org chart belongs to. The organogram's other three tables take
their branch from it rather than storing a second copy: a post is in its unit's
branch, and an appointment and a dotted line are in their post's.

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
from django.core.validators import MinValueValidator
from django.db import models
from django.db.models import Q
from django.db.models.functions import Lower
from django.utils import timezone

from vs_history.queryset import VersionedManager
from vs_rbac.managers import TenantAwareManager

from .constants import (
    NON_HOLDING_STATUSES,
    OFF_ROLL_STATUSES,
    ORG_UNIT_CODE_PREFIX,
    ORG_UNIT_PARENT_KIND,
    DocumentType,
    EmploymentStatus,
    EmploymentType,
    LeaveStatus,
    LeaveType,
    OrgUnitKind,
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
    all_objects = VersionedManager()

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
    additional_postings = models.ManyToManyField(
        "vs_tenants.Branch", blank=True, related_name="additional_staff_postings",
    )

    @property
    def posting_branch_ids(self):
        """Every equal branch posting, empty for a school-wide posting."""
        other_ids = [branch.pk for branch in self.additional_postings.all()]
        return ([self.branch_id] if self.branch_id else []) + other_ids
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
            # Case-insensitive, as admission numbers are: see
            # services/numbers.py.
            models.UniqueConstraint(
                Lower("staff_number"), "tenant",
                condition=~Q(staff_number=""),
                name="uq_staff_number_per_tenant_ci",
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


# =============================================================================
# The organogram
# =============================================================================


def line_allowed(branch_id, manager_branch_id) -> bool:
    """Whether something in *branch_id* may answer to something in *manager_branch_id*.

    One rule for every line on the chart: a solid line from a post to the post
    it reports to, a dotted line, and a unit to the post that heads it. The
    upper end must be school-wide or in the same branch. A Lekki bursar may
    report to the school's Director of Finance, and never to the Ikeja head
    teacher, because nobody at Ikeja is answerable for what happens at Lekki.
    A school-wide post therefore reports only to another school-wide post.
    """
    return manager_branch_id is None or manager_branch_id == branch_id


class StaffOrgNode(_Owned):
    """A unit of a school's org chart, tiered division, department, team.

    The platform's own chart has the same shape and lives in ``vs_user``; this
    one is the school's, scoped to one tenant and aware of branches.

    **The branch is optional, and a null is school-wide**, which is the ordinary
    case for the top of the chart: the Academics division belongs to the school,
    and the Lekki science department sits under it. The rule that keeps the tree
    coherent is that a unit may be no wider than its parent: under a branch unit
    every child carries that same branch, and under a school-wide one a child is
    school-wide or narrows to one branch.

    Sibling names are unique within one parent, checked here rather than by a
    conditional constraint, because MariaDB cannot express one and the rule has
    to hold on both engines the platform runs on.
    """

    Kind = OrgUnitKind

    tenant = models.ForeignKey(
        "vs_tenants.Tenant", on_delete=models.PROTECT, related_name="staff_org_nodes",
    )
    #: NULL means school-wide, which is a first-class value, not a gap.
    branch = models.ForeignKey(
        "vs_tenants.Branch", on_delete=models.PROTECT, null=True, blank=True,
        related_name="staff_org_nodes",
    )
    name = models.CharField(max_length=150)
    #: Unique within the school, and always carries its tier's prefix.
    code = models.CharField(max_length=40)
    kind = models.CharField(
        max_length=16, choices=OrgUnitKind.choices, default=OrgUnitKind.DEPARTMENT,
    )
    #: PROTECT rather than SET_NULL: a department left without its division is a
    #: shape the tier rule forbids, so it could be neither saved nor deleted.
    #: The API answers 409 with what is in the way instead.
    parent = models.ForeignKey(
        "self", on_delete=models.PROTECT, null=True, blank=True,
        related_name="children",
    )
    #: The post whose holder heads this unit. Nullable, so a unit can exist
    #: before its head post is drawn, and SET_NULL, so removing that post leaves
    #: the unit standing.
    head_position = models.ForeignKey(
        "StaffPosition", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="heads_units",
    )
    description = models.TextField(blank=True, default="")
    is_active = models.BooleanField(default=True)

    class Meta(_Owned.Meta):
        constraints = [
            models.UniqueConstraint(
                fields=["tenant", "code"], name="uq_staff_org_node_code",
            ),
        ]
        indexes = [
            models.Index(fields=["tenant", "is_active"]),
            models.Index(fields=["tenant", "parent"]),
            models.Index(fields=["tenant", "kind"]),
            models.Index(fields=["tenant", "branch"]),
        ]
        ordering = ["name", "id"]

    def __str__(self):
        return f"StaffOrgNode<{self.kind}:{self.code}>"

    @staticmethod
    def prefixed_code(kind, code) -> str:
        """*code* carrying *kind*'s prefix, with any other tier's prefix removed.

        So a unit that changes tier changes prefix, and typing the prefix
        yourself does not double it.
        """
        bare = (code or "").strip()
        for prefix in ORG_UNIT_CODE_PREFIX.values():
            if bare.upper().startswith(prefix):
                bare = bare[len(prefix):]
                break
        return f"{ORG_UNIT_CODE_PREFIX.get(kind, '')}{bare.upper()}"

    def save(self, *args, **kwargs):
        self.code = self.prefixed_code(self.kind, self.code)
        super().save(*args, **kwargs)

    def ancestors(self):
        """The parent, then its parent, up to the top of the chart."""
        node = self.parent
        seen = {self.pk}
        while node is not None and node.pk not in seen:
            seen.add(node.pk)
            yield node
            node = node.parent

    def clean(self):
        """The tenant, the tree, the tier, the name and the branch rules.

        Changing the branch of a unit that already has children or posts is
        refused where it would leave any of them inconsistent, or leave somebody
        appointed at a branch they are not posted to, and the refusal counts
        what is in the way, because "cannot change the branch" alone
        sends an administrator searching the chart for the reason.
        """
        super().clean()
        from vs_tenants.models import Branch

        if self.branch_id and not Branch.all_objects.filter(
            pk=self.branch_id, tenant_id=self.tenant_id,
        ).exists():
            raise ValidationError({"branch": "That branch belongs to a different school."})
        if self.parent_id:
            if self.parent_id == self.pk:
                raise ValidationError({"parent": "A unit cannot sit under itself."})
            if self.parent.tenant_id != self.tenant_id:
                raise ValidationError({"parent": "That unit belongs to a different school."})
            if self.pk and any(node.pk == self.pk for node in self.parent.ancestors()):
                raise ValidationError({
                    "parent": "That would put this unit underneath itself.",
                })

        siblings = StaffOrgNode.all_objects.filter(
            tenant_id=self.tenant_id, parent_id=self.parent_id,
            name__iexact=(self.name or "").strip(),
        ).exclude(pk=self.pk)
        if siblings.exists():
            where = f'under "{self.parent.name}"' if self.parent_id else "at the top"
            raise ValidationError({
                "name": f'There is already a unit called "{self.name}" {where}.',
            })

        required = ORG_UNIT_PARENT_KIND.get(self.kind)
        label = OrgUnitKind(self.kind).label if self.kind in OrgUnitKind.values else self.kind
        if required is None and self.parent_id:
            raise ValidationError({
                "parent": f"A {label.lower()} sits at the top and has no parent.",
            })
        if required is not None:
            wanted = OrgUnitKind(required).label.lower()
            if not self.parent_id:
                raise ValidationError({
                    "parent": f"A {label.lower()} must sit under a {wanted}.",
                })
            if self.parent.kind != required:
                raise ValidationError({
                    "parent": (
                        f"A {label.lower()} must sit under a {wanted}, not a "
                        f"{self.parent.get_kind_display().lower()}."
                    ),
                })

        if self.parent_id and self.parent.branch_id is not None:
            if self.branch_id != self.parent.branch_id:
                raise ValidationError({
                    "branch": (
                        f"{self.parent.name} belongs to {self.parent.branch.name}, "
                        f"so everything under it belongs there too."
                    ),
                })

        if self.head_position_id:
            head = self.head_position
            if head.tenant_id != self.tenant_id:
                raise ValidationError({
                    "head_position": "That post belongs to a different school.",
                })
            head_branch = (
                self.branch_id if head.org_node_id == self.pk else head.branch_id
            )
            if not line_allowed(self.branch_id, head_branch):
                raise ValidationError({
                    "head_position": (
                        "A unit is headed from its own branch or from a "
                        "school-wide post."
                    ),
                })

        self._refuse_inconsistent_rebranch()

    def _refuse_inconsistent_rebranch(self):
        """Refuse a branch change the children, posts or lines could not follow."""
        if not self.pk:
            return
        stored = (
            StaffOrgNode.all_objects.filter(pk=self.pk)
            .values_list("branch_id", flat=True).first()
        )
        if stored == self.branch_id:
            return
        new = self.branch_id
        units = (
            self.children.exclude(branch_id=new).count() if new is not None else 0
        )

        def branch_of(post):
            return new if post.org_node_id == self.pk else post.org_node.branch_id

        posts = 0
        for post in (
            StaffPosition.all_objects.filter(org_node_id=self.pk)
            .select_related("reports_to__org_node")
            .prefetch_related(
                "direct_reports__org_node",
                "dotted_lines__reports_to__org_node",
                "dotted_reports__position__org_node",
                "heads_units",
            )
        ):
            ok = (
                (post.reports_to_id is None or line_allowed(new, branch_of(post.reports_to)))
                and all(line_allowed(branch_of(d), new) for d in post.direct_reports.all())
                and all(
                    line_allowed(new, branch_of(line.reports_to))
                    for line in post.dotted_lines.all()
                )
                and all(
                    line_allowed(branch_of(line.position), new)
                    for line in post.dotted_reports.all()
                )
                and all(
                    line_allowed(new if unit.pk == self.pk else unit.branch_id, new)
                    for unit in post.heads_units.all()
                )
            )
            posts += 0 if ok else 1
        people = _appointed_elsewhere(
            StaffPosition.all_objects.filter(org_node_id=self.pk), new,
        )
        if units or posts or people:
            parts = []
            if units:
                parts.append(f"{units} unit{'s' if units != 1 else ''} under it")
            if posts:
                parts.append(
                    f"{posts} post{'s' if posts != 1 else ''} in it whose reporting "
                    f"lines cross into another branch"
                )
            if people:
                parts.append(
                    f"{people} {'people' if people != 1 else 'person'} appointed in "
                    f"it who {'are' if people != 1 else 'is'} not posted there"
                )
            raise ValidationError({
                "branch": (
                    f"{self.name} cannot move branch while {' and '.join(parts)} "
                    f"would be left behind. Move or re-link them first."
                ),
            })


class StaffPosition(_Owned):
    """A post on the chart: a seat people are appointed to, not a person.

    **Lines connect posts.** The solid line is ``reports_to``, from this post to
    the post it answers to. When a head of department leaves, her post stays on
    the chart with its reports still under it, and whoever is appointed next
    inherits them without a line being redrawn.

    The branch is the unit's and is never stored here. A post whose unit moves
    to another branch moves with it, and a second column could only disagree.

    There is no default role. The platform chart stores one and grants nothing
    from it, and a school's restricted role grants go through the approval
    ladder, which an appointment must not be a way around.
    """

    tenant = models.ForeignKey(
        "vs_tenants.Tenant", on_delete=models.PROTECT, related_name="staff_positions",
    )
    title = models.CharField(max_length=150)
    #: Unique within the school.
    code = models.CharField(max_length=40)
    #: PROTECT: a unit holding posts is not deleted out from under them.
    org_node = models.ForeignKey(
        StaffOrgNode, on_delete=models.PROTECT, related_name="positions",
    )
    #: The solid line. NULL at the top of the chart.
    reports_to = models.ForeignKey(
        "self", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="direct_reports",
    )
    #: How many people the post seats at once. One is the ordinary post; a
    #: class teacher post in a large school may seat twelve.
    headcount = models.PositiveSmallIntegerField(
        default=1, validators=[MinValueValidator(1)],
    )
    is_active = models.BooleanField(default=True)

    class Meta(_Owned.Meta):
        constraints = [
            models.UniqueConstraint(
                fields=["tenant", "code"], name="uq_staff_position_code",
            ),
        ]
        indexes = [
            models.Index(fields=["tenant", "is_active"]),
            models.Index(fields=["org_node", "is_active"]),
            models.Index(fields=["tenant", "reports_to"]),
            models.Index(fields=["tenant", "title"]),
        ]
        ordering = ["title", "id"]

    def __str__(self):
        return f"StaffPosition<{self.code}>"

    @property
    def branch(self):
        """The unit's branch. Derived, never stored."""
        return self.org_node.branch if self.org_node_id else None

    @property
    def branch_id(self):
        """The unit's branch id, read by the branch write rules."""
        return self.org_node.branch_id if self.org_node_id else None

    def clean(self):
        """The tenant, the reporting line and what a move would break.

        ``reports_to`` must be school-wide or in this post's branch; see
        :func:`line_allowed`. A post moving to a unit in another branch is
        refused where it would leave a direct report, a dotted line or a unit it
        heads answering across a branch, or somebody appointed to it at a branch
        they are not posted to, counted in the message.
        """
        super().clean()
        if self.org_node_id and self.org_node.tenant_id != self.tenant_id:
            raise ValidationError({"org_node": "That unit belongs to a different school."})
        if self.reports_to_id:
            manager = self.reports_to
            if manager.tenant_id != self.tenant_id:
                raise ValidationError({
                    "reports_to": "That post belongs to a different school.",
                })
            if self.reports_to_id == self.pk:
                raise ValidationError({"reports_to": "A post cannot report to itself."})
            seen = {self.pk}
            node = manager
            while node is not None:
                if node.pk in seen:
                    raise ValidationError({
                        "reports_to": "That would make the reporting line a loop.",
                    })
                seen.add(node.pk)
                node = node.reports_to
            if not line_allowed(self.branch_id, manager.branch_id):
                raise ValidationError({
                    "reports_to": (
                        f"{manager.title} is at {manager.branch.name}, so only "
                        f"a post at {manager.branch.name} can report to it."
                        if manager.branch_id else
                        "A post reports to a school-wide post or one in its own branch."
                    ),
                })
        self._refuse_inconsistent_move()

    def _refuse_inconsistent_move(self):
        """Refuse moving to another branch where its lines could not follow."""
        if not self.pk or not self.org_node_id:
            return
        stored = (
            StaffPosition.all_objects.filter(pk=self.pk)
            .values_list("org_node__branch_id", flat=True).first()
        )
        new = self.branch_id
        if stored == new:
            return
        broken = sum(
            1 for d in self.direct_reports.select_related("org_node")
            if not line_allowed(d.branch_id, new)
        ) + sum(
            1 for line in self.dotted_reports.select_related("position__org_node")
            if not line_allowed(line.position.branch_id, new)
        ) + sum(
            1 for line in self.dotted_lines.select_related("reports_to__org_node")
            if not line_allowed(new, line.reports_to.branch_id)
        ) + sum(
            1 for unit in self.heads_units.all()
            if not line_allowed(unit.branch_id, new)
        )
        if broken:
            raise ValidationError({
                "org_node_id": (
                    f"{self.title} cannot move branch while {broken} reporting "
                    f"line{'s' if broken != 1 else ''} to or from it would cross "
                    f"into another branch. Re-link them first."
                ),
            })
        people = _appointed_elsewhere(
            StaffPosition.all_objects.filter(pk=self.pk), new,
        )
        if people:
            raise ValidationError({
                "org_node_id": (
                    f"{self.title} cannot move branch while {people} "
                    f"{'people' if people != 1 else 'person'} appointed to it "
                    f"{'are' if people != 1 else 'is'} not posted there. End "
                    f"{'their appointments' if people != 1 else 'the appointment'} first."
                ),
            })


class StaffPositionAssignment(_Owned):
    """A person appointed to a post, dated at both ends. The full history.

    A row with no ``end_date`` is a current appointment, and ending one is
    setting it: past rows are kept, because who held the bursary in 2024 is a
    question a school is asked.

    **One current primary appointment per person.** Their primary post is the
    one their line manager and department are read from; they may also hold
    other posts at once, and may be acting in one. MariaDB cannot express the
    rule as a conditional constraint, so it is kept here and by
    ``services.organogram``, which closes the old primary when a new one is
    made.

    **Who may hold which post** follows the posting. A school-wide person may
    hold any post, anybody may hold a school-wide post, and otherwise the post's
    branch must be one of the person's postings: an Ikeja teacher is not
    appointed head of the Lekki science department until she is posted there.
    """

    tenant = models.ForeignKey(
        "vs_tenants.Tenant", on_delete=models.PROTECT,
        related_name="staff_position_assignments",
    )
    #: PROTECT: an appointment is employment history.
    staff = models.ForeignKey(
        StaffProfile, on_delete=models.PROTECT, related_name="appointments",
    )
    #: PROTECT: a post somebody has held is kept, and deactivated instead.
    position = models.ForeignKey(
        StaffPosition, on_delete=models.PROTECT, related_name="appointments",
    )
    is_primary = models.BooleanField(default=True)
    #: Covering the post rather than holding it.
    is_acting = models.BooleanField(default=False)
    start_date = models.DateField(default=timezone.localdate)
    end_date = models.DateField(null=True, blank=True)

    class Meta(_Owned.Meta):
        indexes = [
            models.Index(fields=["tenant", "end_date"]),
            models.Index(fields=["position", "end_date"]),
            models.Index(fields=["staff", "end_date"]),
            models.Index(fields=["tenant", "-start_date"]),
        ]
        ordering = ["-start_date", "-id"]

    def __str__(self):
        state = "current" if self.end_date is None else f"ended {self.end_date}"
        return f"StaffPositionAssignment<{self.staff_id}@{self.position_id}:{state}>"

    @property
    def is_current(self) -> bool:
        return self.end_date is None

    @property
    def branch_id(self):
        """The post's branch id, read by the branch write rules."""
        return self.position.branch_id if self.position_id else None

    @property
    def is_holding(self) -> bool:
        """Whether this appointment puts somebody in the seat today.

        Open, and held by somebody still employed who has accepted their
        invitation. A suspended person still holds their seat; they are only
        passed over as an approver (``services.organogram.approving_q``).
        """
        return (
            self.end_date is None
            and self.staff.employment_status not in NON_HOLDING_STATUSES
        )

    def clean(self):
        super().clean()
        if self.staff_id and self.staff.tenant_id != self.tenant_id:
            raise ValidationError({"staff": "That person works at a different school."})
        if self.position_id and self.position.tenant_id != self.tenant_id:
            raise ValidationError({"position": "That post belongs to a different school."})
        if self.end_date and self.start_date and self.end_date < self.start_date:
            raise ValidationError({
                "end_date": "An appointment cannot end before it starts.",
            })
        if self.end_date is None and self.staff_id and self.position_id:
            open_rows = StaffPositionAssignment.all_objects.filter(
                staff_id=self.staff_id, end_date__isnull=True,
            ).exclude(pk=self.pk)
            if open_rows.filter(position_id=self.position_id).exists():
                raise ValidationError({
                    "position": "They already hold this post.",
                })
            if self.is_primary and open_rows.filter(is_primary=True).exists():
                raise ValidationError({
                    "is_primary": (
                        "They already have a current primary post. End it before "
                        "appointing another."
                    ),
                })
        if self.staff_id and self.position_id and not self.eligible(self.staff, self.position):
            raise ValidationError({
                "staff": (
                    f"{_person_name(self.staff)} is not posted to "
                    f"{self.position.branch.name}, so cannot hold a post there."
                ),
            })

    @staticmethod
    def eligible(staff, position) -> bool:
        """Whether *staff* may hold *position*, going by postings alone."""
        postings = staff.posting_branch_ids
        return (
            not postings
            or position.branch_id is None
            or position.branch_id in postings
        )


class StaffMatrixReport(_Owned):
    """A dotted line: a second reporting line between two posts.

    The exams officer answers to the vice principal on the solid line and to
    the head of academics on a dotted one. Distinct from ``reports_to``, and
    kept to the same branch rule, so a dotted line cannot carry authority
    across branches that the solid line may not.
    """

    tenant = models.ForeignKey(
        "vs_tenants.Tenant", on_delete=models.PROTECT,
        related_name="staff_matrix_reports",
    )
    position = models.ForeignKey(
        StaffPosition, on_delete=models.CASCADE, related_name="dotted_lines",
    )
    reports_to = models.ForeignKey(
        StaffPosition, on_delete=models.CASCADE, related_name="dotted_reports",
    )
    relationship_label = models.CharField(max_length=120, blank=True, default="")

    class Meta(_Owned.Meta):
        constraints = [
            models.UniqueConstraint(
                fields=["position", "reports_to"], name="uq_staff_matrix_report",
            ),
        ]
        indexes = [
            models.Index(fields=["tenant", "position"]),
            models.Index(fields=["tenant", "reports_to"]),
        ]
        ordering = ["-created_at", "-id"]

    def __str__(self):
        return f"StaffMatrixReport<{self.position_id} to {self.reports_to_id}>"

    @property
    def branch_id(self):
        """The reporting post's branch id, read by the branch write rules."""
        return self.position.branch_id if self.position_id else None

    def clean(self):
        super().clean()
        if self.position_id and self.position.tenant_id != self.tenant_id:
            raise ValidationError({"position": "That post belongs to a different school."})
        if self.reports_to_id and self.reports_to.tenant_id != self.tenant_id:
            raise ValidationError({"reports_to": "That post belongs to a different school."})
        if self.position_id and self.position_id == self.reports_to_id:
            raise ValidationError({"reports_to": "A post cannot have a dotted line to itself."})
        if self.position_id and self.reports_to_id:
            if not line_allowed(self.position.branch_id, self.reports_to.branch_id):
                raise ValidationError({
                    "reports_to": (
                        "A dotted line runs to a school-wide post or one in the "
                        "same branch."
                    ),
                })
            clash = StaffMatrixReport.all_objects.filter(
                position_id=self.position_id, reports_to_id=self.reports_to_id,
            ).exclude(pk=self.pk)
            if clash.exists():
                raise ValidationError({"reports_to": "That dotted line is already drawn."})


def _appointed_elsewhere(positions, branch_id) -> int:
    """Open appointments to *positions* whose person is not posted to *branch_id*.

    What moving those posts to *branch_id* would leave behind: somebody sitting
    in a post at a branch they do not work at. School-wide is never a problem.
    """
    if branch_id is None:
        return 0
    rows = (
        StaffPositionAssignment.all_objects
        .filter(position__in=positions, end_date__isnull=True)
        .select_related("staff")
        .prefetch_related("staff__additional_postings")
    )
    return sum(
        1 for row in rows
        if row.staff.posting_branch_ids and branch_id not in row.staff.posting_branch_ids
    )


def _person_name(staff) -> str:
    user = staff.user
    name = " ".join(part for part in (user.first_name, user.last_name) if part).strip()
    return name or "This person"

"""The academic backbone: the school year, and the hierarchy taught inside it.

Nine models. Every one carries its own ``tenant`` foreign key, even where its
parent already has one, because :class:`vs_rbac.managers.TenantAwareManager`
filters on a model's own ``tenant`` or ``branch`` field and returns everything
otherwise. Reaching the tenant through a parent is not scoping.

Five of them carry a nullable ``branch``. A null there means the item is shared
by the whole school - a deliberate, first-class value, never "no branches
exist" - and a set branch means it belongs to that branch alone. M13 FRD v2.6
section 5 is the rule and section 5.4 is why it is a one-way door.

``SessionBranch`` is the one model here that is nothing but a branch reference,
and it is what lets a school year apply to some branches rather than all. An
empty set means the whole school, deliberately not materialised as one row per
branch: materialising would freeze "the whole school" to the branches that
existed the day the session was written, so a branch opening in January would
sit outside the running year (FRD v2.6 FR-013).
"""
from __future__ import annotations

from django.conf import settings
from django.db import models
from django.db.models import F, Q
from django.db.models.functions import Lower
from django.utils import timezone

from vs_rbac.managers import TenantAwareManager


class _Owned(models.Model):
    """The managers that enforce tenant ownership, and the timestamps.

    ``objects`` applies the ambient tenant eagerly; ``all_objects`` does not and
    exists for migrations, seeders and the constraint-level tests that have to
    write across tenants on purpose. ``base_manager_name`` is the unfiltered one
    so related traversal does not silently drop rows.

    The ``tenant`` column itself is **not** here, and its absence is deliberate.
    Declaring it once in this base would mean one ``related_name`` shared by
    five models, so it would have to be ``"+"``, which disables the reverse
    accessor entirely. That is tidier to write and quietly makes
    ``tenant.departments`` and ``branch.classes`` impossible - which is how the
    branch class count that ``vs_schools`` had been waiting for turned out to be
    unbuildable. Each concrete model declares its own, with the name its FRD
    section gives it.
    """

    created_at = models.DateTimeField(default=timezone.now, editable=False)
    updated_at = models.DateTimeField(auto_now=True)

    objects = TenantAwareManager()
    all_objects = models.Manager()

    class Meta:
        abstract = True
        default_manager_name = "objects"
        base_manager_name = "all_objects"


class _Branched(_Owned):
    """A tenant-owned row that may belong to one branch or to the school.

    ``branch`` itself is declared on each concrete model, for the reason
    ``_Owned`` gives.
    """

    is_active = models.BooleanField(default=True, db_index=True)

    class Meta(_Owned.Meta):
        abstract = True


class SessionStatus(models.TextChoices):
    DRAFT = "DRAFT", "Draft"
    ACTIVE = "ACTIVE", "Active"
    ARCHIVED = "ARCHIVED", "Archived"


class AcademicSession(models.Model):
    """A school year, and (through SessionBranch) the branches it applies to."""

    tenant = models.ForeignKey(
        "vs_tenants.Tenant", on_delete=models.PROTECT,
        related_name="academic_sessions",
    )
    name = models.CharField(max_length=20)
    start_date = models.DateField()
    end_date = models.DateField()
    status = models.CharField(
        max_length=10, choices=SessionStatus.choices,
        default=SessionStatus.DRAFT, db_index=True,
    )
    activated_at = models.DateTimeField(null=True, blank=True)
    archived_at = models.DateTimeField(null=True, blank=True)
    # True when the session names no branches. Maintained by the service in the
    # same transaction as the SessionBranch rows and never written by a
    # serializer. It exists because a partial unique constraint cannot ask
    # whether a related table is empty, and without it the school-wide half of
    # the one-active-per-branch rule could not be enforced in the database at
    # all - only in service logic, which loses a race.
    is_school_wide = models.BooleanField(default=True, db_index=True)
    created_at = models.DateTimeField(default=timezone.now, editable=False)
    updated_at = models.DateTimeField(auto_now=True)

    objects = TenantAwareManager()
    all_objects = models.Manager()

    class Meta:
        default_manager_name = "objects"
        base_manager_name = "all_objects"
        ordering = ["-start_date"]
        constraints = [
            models.UniqueConstraint(
                Lower("name"), "tenant",
                name="uq_academic_session_tenant_name",
            ),
            # Half of the one-active rule. The other half is on SessionBranch,
            # and the case neither can see - a school-wide session colliding
            # with a branch-scoped one - is refused in the activation service
            # under the row lock, because it is a question about two tables.
            models.UniqueConstraint(
                fields=["tenant"],
                condition=Q(status="ACTIVE", is_school_wide=True),
                name="uq_academic_session_one_active_school_wide",
            ),
            models.CheckConstraint(
                condition=Q(end_date__gt=F("start_date")),
                name="ck_academic_session_dates",
            ),
        ]
        indexes = [models.Index(fields=["tenant", "status"])]

    def __str__(self):
        return self.name


class SessionBranch(models.Model):
    """The branches a session applies to. No rows means the whole school."""

    tenant = models.ForeignKey(
        "vs_tenants.Tenant", on_delete=models.PROTECT,
        related_name="session_branches",
    )
    session = models.ForeignKey(
        AcademicSession, on_delete=models.CASCADE, related_name="branch_links",
    )
    branch = models.ForeignKey(
        "vs_tenants.Branch", on_delete=models.PROTECT,
        related_name="academic_sessions",
    )
    # Denormalised from the session, in the same transaction. A partial unique
    # constraint cannot read the parent's status through the foreign key, and
    # without this column the per-branch rule is service logic only. The
    # session's own status stays the authority; a test asserts they never
    # disagree.
    session_status = models.CharField(
        max_length=10, choices=SessionStatus.choices, db_index=True,
    )
    created_at = models.DateTimeField(default=timezone.now, editable=False)
    updated_at = models.DateTimeField(auto_now=True)

    objects = TenantAwareManager()
    all_objects = models.Manager()

    class Meta:
        default_manager_name = "objects"
        base_manager_name = "all_objects"
        constraints = [
            models.UniqueConstraint(
                fields=["session", "branch"], name="uq_session_branch",
            ),
            models.UniqueConstraint(
                fields=["branch"],
                condition=Q(session_status="ACTIVE"),
                name="uq_session_branch_one_active",
            ),
        ]
        indexes = [models.Index(fields=["tenant", "branch"])]

    def __str__(self):
        return f"{self.session_id}@{self.branch_id}"


class AcademicTerm(models.Model):
    """A term inside a session. It has no lifecycle of its own."""

    tenant = models.ForeignKey(
        "vs_tenants.Tenant", on_delete=models.PROTECT,
        related_name="academic_terms",
    )
    session = models.ForeignKey(
        AcademicSession, on_delete=models.CASCADE, related_name="terms",
    )
    name = models.CharField(max_length=30)
    order_index = models.PositiveSmallIntegerField()
    start_date = models.DateField()
    end_date = models.DateField()
    # Stamped only when the session is archived, and cleared only when it is
    # activated again. A term is never archived or restored on its own: giving
    # it a second lifecycle leads straight back to the state activation refuses.
    archived_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(default=timezone.now, editable=False)
    updated_at = models.DateTimeField(auto_now=True)

    objects = TenantAwareManager()
    all_objects = models.Manager()

    class Meta:
        default_manager_name = "objects"
        base_manager_name = "all_objects"
        ordering = ["order_index"]
        constraints = [
            models.UniqueConstraint(
                fields=["session", "order_index"], name="uq_academic_term_order",
            ),
            models.UniqueConstraint(
                Lower("name"), "session", name="uq_academic_term_name",
            ),
            models.CheckConstraint(
                condition=Q(end_date__gt=F("start_date")),
                name="ck_academic_term_dates",
            ),
        ]

    def __str__(self):
        return self.name


class Department(_Branched):
    """A faculty grouping that programs and subjects hang off."""

    tenant = models.ForeignKey(
        "vs_tenants.Tenant", on_delete=models.PROTECT,
        related_name="departments",
    )
    branch = models.ForeignKey(
        "vs_tenants.Branch", on_delete=models.PROTECT,
        null=True, blank=True, related_name="departments",
        help_text="Leave blank for an item the whole school shares.",
    )
    name = models.CharField(max_length=100)
    code = models.CharField(max_length=20)
    description = models.TextField(blank=True, default="")

    class Meta(_Branched.Meta):
        ordering = ["name"]
        constraints = [
            models.UniqueConstraint(
                Lower("name"), "tenant", name="uq_academic_department_name",
            ),
            models.UniqueConstraint(
                Lower("code"), "tenant", name="uq_academic_department_code",
            ),
        ]
        indexes = [models.Index(fields=["tenant", "branch", "is_active"])]

    def __str__(self):
        return self.name


class Program(_Branched):
    """A stage a pupil moves through: Nursery, Primary, Junior Secondary."""

    tenant = models.ForeignKey(
        "vs_tenants.Tenant", on_delete=models.PROTECT,
        related_name="programs",
    )
    branch = models.ForeignKey(
        "vs_tenants.Branch", on_delete=models.PROTECT,
        null=True, blank=True, related_name="programs",
        help_text="Leave blank for an item the whole school shares.",
    )
    department = models.ForeignKey(
        Department, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="programs",
    )
    name = models.CharField(max_length=100)
    code = models.CharField(max_length=20)
    # Every entity the structure drawer writes carries one, so the same field
    # means the same thing on all five. Department and Subject had it from the
    # start; Programme, Level and Class did not, and the drawer's Description box
    # silently dropped what was typed into it on those three.
    description = models.TextField(blank=True, default="")
    order_index = models.PositiveSmallIntegerField(default=0)

    class Meta(_Branched.Meta):
        ordering = ["order_index", "name"]
        constraints = [
            models.UniqueConstraint(
                Lower("name"), "tenant", name="uq_academic_program_name",
            ),
            models.UniqueConstraint(
                Lower("code"), "tenant", name="uq_academic_program_code",
            ),
        ]
        indexes = [models.Index(fields=["tenant", "branch", "is_active"])]

    def __str__(self):
        return self.name


class Level(_Branched):
    """A year group inside a programme: JSS1, Primary 4."""

    tenant = models.ForeignKey(
        "vs_tenants.Tenant", on_delete=models.PROTECT,
        related_name="levels",
    )
    branch = models.ForeignKey(
        "vs_tenants.Branch", on_delete=models.PROTECT,
        null=True, blank=True, related_name="levels",
        help_text="Leave blank for an item the whole school shares.",
    )
    # PROTECT, not CASCADE: deleting a programme must not silently destroy the
    # levels that classes hang off. The platform answers the blocked delete
    # with 409 PROTECTED_REFERENCE and a count.
    program = models.ForeignKey(
        Program, on_delete=models.PROTECT, related_name="levels",
    )
    #: The year this level belongs to.
    #:
    #: Levels, classes and subjects are what a school rebuilds each year;
    #: departments and programmes are the spine they hang off and stay shared.
    #: A class carries its own copy, because a constraint cannot join: "this
    #: code is free again next September" has to be expressed as
    #: (tenant, session, code), and Postgres will not take `level__session`
    #: there. The two are kept in step by `assert_same_session` on every write,
    #: which is the price of the denormalisation and is paid in one place.
    session = models.ForeignKey(
        AcademicSession, on_delete=models.PROTECT, related_name="levels",
    )
    name = models.CharField(max_length=60)
    code = models.CharField(max_length=20)
    description = models.TextField(blank=True, default="")
    order_index = models.PositiveSmallIntegerField()
    # The promotion target. Null means graduation to the promoting module - and
    # also means nothing has wired promotion yet, because no screen writes this
    # today. M11 must not read the two as the same thing (FRD v2.6 FR-005).
    next_level = models.ForeignKey(
        "self", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="previous_levels",
    )

    class Meta(_Branched.Meta):
        ordering = ["program", "order_index"]
        constraints = [
            models.UniqueConstraint(
                Lower("name"), "program", "session",
                name="uq_academic_level_name",
            ),
            models.UniqueConstraint(
                Lower("code"), "program", "session",
                name="uq_academic_level_code",
            ),
            models.UniqueConstraint(
                fields=["program", "session", "order_index"],
                name="uq_academic_level_order",
            ),
        ]
        indexes = [models.Index(fields=["tenant", "branch", "is_active"])]

    def __str__(self):
        return self.name


class SchoolClass(_Branched):
    """A class pupils sit in: JSS1 A."""

    tenant = models.ForeignKey(
        "vs_tenants.Tenant", on_delete=models.PROTECT,
        related_name="classes",
    )
    branch = models.ForeignKey(
        "vs_tenants.Branch", on_delete=models.PROTECT,
        null=True, blank=True, related_name="classes",
        help_text="Leave blank for an item the whole school shares.",
    )
    level = models.ForeignKey(
        Level, on_delete=models.PROTECT, related_name="classes",
    )
    #: Always its level's session - see Level.session for why it is stored
    #: rather than joined, and services.sessions.assert_same_session for what
    #: keeps the two from drifting.
    session = models.ForeignKey(
        AcademicSession, on_delete=models.PROTECT, related_name="classes",
    )
    name = models.CharField(max_length=60)
    code = models.CharField(max_length=20)
    description = models.TextField(blank=True, default="")
    arm = models.CharField(max_length=30, blank=True, default="")
    # Advisory here in the exact sense that nothing in this module reads it to
    # refuse anything: there is no Student model here to count. M11 enforces it
    # on placement. No screen writes it today either, so M11 must read a null
    # as "no limit" rather than as a limit not yet reached.
    capacity = models.PositiveSmallIntegerField(null=True, blank=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL,
        null=True, blank=True, related_name="+",
    )

    class Meta(_Branched.Meta):
        ordering = ["level", "name"]
        constraints = [
            # Uniqueness across a NULLABLE branch takes two constraints, not
            # one. In PostgreSQL NULL != NULL, so a single constraint over
            # (level, branch, name) would not stop two branch-less classes
            # called 'JSS1 A' in the same level.
            models.UniqueConstraint(
                Lower("name"), "level", "branch",
                condition=Q(branch__isnull=False),
                name="uq_academic_class_level_branch_name",
            ),
            models.UniqueConstraint(
                Lower("name"), "level",
                condition=Q(branch__isnull=True),
                name="uq_academic_class_level_name_nobranch",
            ),
            models.UniqueConstraint(
                Lower("code"), "tenant", "session",
                name="uq_academic_class_code",
            ),
        ]
        indexes = [
            models.Index(fields=["tenant", "branch", "is_active"]),
            models.Index(fields=["level", "is_active"]),
        ]

    def __str__(self):
        return self.name


class Subject(_Branched):
    """Something taught, and (through SubjectOffering) where it is taught."""

    tenant = models.ForeignKey(
        "vs_tenants.Tenant", on_delete=models.PROTECT,
        related_name="subjects",
    )
    branch = models.ForeignKey(
        "vs_tenants.Branch", on_delete=models.PROTECT,
        null=True, blank=True, related_name="subjects",
        help_text="Leave blank for an item the whole school shares.",
    )
    department = models.ForeignKey(
        Department, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="subjects",
    )
    #: The year this subject is taught in. See Level.session.
    session = models.ForeignKey(
        AcademicSession, on_delete=models.PROTECT, related_name="subjects",
    )
    name = models.CharField(max_length=100)
    code = models.CharField(max_length=20)
    is_core = models.BooleanField(default=True)
    description = models.TextField(blank=True, default="")
    # Declared through an explicit model, never bare: an implicit join table
    # has no tenant column, so TenantAwareManager returns it unscoped.
    levels = models.ManyToManyField(
        Level, through="SubjectOffering", related_name="subjects",
    )

    class Meta(_Branched.Meta):
        ordering = ["name"]
        constraints = [
            models.UniqueConstraint(
                Lower("name"), "tenant", "session",
                name="uq_academic_subject_name",
            ),
            models.UniqueConstraint(
                Lower("code"), "tenant", "session",
                name="uq_academic_subject_code",
            ),
        ]
        indexes = [models.Index(fields=["tenant", "branch", "is_active"])]

    def __str__(self):
        return self.name


class SubjectOffering(models.Model):
    """A subject offered at a level. The reason Subject.levels has a through."""

    tenant = models.ForeignKey(
        "vs_tenants.Tenant", on_delete=models.PROTECT,
        related_name="subject_offerings",
    )
    subject = models.ForeignKey(
        Subject, on_delete=models.CASCADE, related_name="offerings",
    )
    # CASCADE, not PROTECT. An offering is a statement ABOUT a level -
    # "Mathematics is taught at JSS1" - and it is meaningless the moment JSS1
    # is gone. Protecting it made a level with no classes undeletable until
    # somebody opened every subject offered at it and unticked that level, none
    # of which edits meant anything. Classes still PROTECT: those carry history.
    level = models.ForeignKey(
        Level, on_delete=models.CASCADE, related_name="subject_offerings",
    )
    # Overrides Subject.is_core for this level when set; null means inherit.
    is_core = models.BooleanField(null=True, blank=True)
    created_at = models.DateTimeField(default=timezone.now, editable=False)
    updated_at = models.DateTimeField(auto_now=True)

    objects = TenantAwareManager()
    all_objects = models.Manager()

    class Meta:
        default_manager_name = "objects"
        base_manager_name = "all_objects"
        constraints = [
            models.UniqueConstraint(
                fields=["subject", "level"], name="uq_academic_offering",
            ),
        ]
        indexes = [models.Index(fields=["tenant", "level"])]

    def __str__(self):
        return f"{self.subject_id}@{self.level_id}"

from __future__ import annotations

from django.core.exceptions import ValidationError
from django.core.validators import RegexValidator
from django.db import models, transaction
from django.db.models import Max, Q
from django.utils import timezone

from vs_rbac.managers import TenantAwareManager

from .exceptions import InvalidBranchTransition


tenant_slug_validator = RegexValidator(
    regex=r"^[a-z0-9]+(?:-[a-z0-9]+)*$",
    message="Slug must be lowercase letters/numbers separated by single hyphens.",
)


class Tenant(models.Model):
    """A customer or platform security boundary.

    Numeric primary keys are deliberately internal. ``slug`` is the stable,
    human-readable identifier accepted by tenant-scoped APIs.
    """

    class Kind(models.TextChoices):
        PLATFORM = "PLATFORM", "Platform"
        SCHOOL = "SCHOOL", "School"
        ORGANIZATION = "ORGANIZATION", "Organization"

    class Status(models.TextChoices):
        PENDING = "PENDING", "Pending"
        ACTIVE = "ACTIVE", "Active"
        SUSPENDED = "SUSPENDED", "Suspended"
        INACTIVE = "INACTIVE", "Inactive"

    name = models.CharField(max_length=255)
    slug = models.SlugField(
        max_length=80, unique=True, validators=[tenant_slug_validator],
    )
    kind = models.CharField(max_length=16, choices=Kind.choices)
    status = models.CharField(
        max_length=16, choices=Status.choices, default=Status.PENDING, db_index=True,
    )
    activated_at = models.DateTimeField(null=True, blank=True)
    deactivated_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(default=timezone.now, editable=False)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["name", "id"]
        indexes = [models.Index(fields=["kind", "status"])]
        constraints = [
            models.CheckConstraint(
                condition=~models.Q(slug=""), name="tenant_slug_not_empty",
            ),
        ]

    def clean(self):
        super().clean()
        self.slug = (self.slug or "").strip().lower()

    def activate(self):
        self.status = self.Status.ACTIVE
        self.activated_at = self.activated_at or timezone.now()
        self.deactivated_at = None

    def __str__(self):
        return self.slug


class BranchStatus(models.TextChoices):
    """Lifecycle states a site can occupy. Nothing here is school-shaped."""

    ACTIVE = "ACTIVE", "Active"
    PENDING = "PENDING", "Pending Activation"
    SUSPENDED = "SUSPENDED", "Suspended"
    INACTIVE = "INACTIVE", "Inactive"
    CLOSED = "CLOSED", "Closed"


class Branch(models.Model):
    """One physical site owned by a tenant: a campus, a clinic, a store.

    "Branch" is not school vocabulary - a bank, a clinic chain and a retail
    group all have branches - so the model belongs beside ``Tenant`` rather
    than inside the schools product. It used to hang off ``vs_schools.School``
    and every app that needed to know which tenant owned a site had to travel
    ``branch.school.tenant``; the tenant is now a column on the row.

    The table is still ``vs_schools_branch``. Keeping the class name, the
    integer primary key and the table meant the move to this app changed only
    Django's model state: no row moved, no foreign key constraint was rebuilt,
    and every ``branch_id`` already stored anywhere (including in issued JWTs)
    still names the same site.

    Fields:
        tenant: FK to the owning Tenant. Every creation path must supply it;
            there is no longer a school to derive it from.
        name: Display label such as "Lekki Campus".
        code: Integer code unique within the tenant; filled on first save.
        is_main: Boolean marker for the canonical site (constraint enforces 1).
        _type: Optional free-form descriptor (e.g., Primary, Secondary).
        address / email / country / state: Contact + location metadata.
        status: Lifecycle state (BranchStatus choices, indexed).
        opened_at / closed_at / activated_at / deactivated_at: Lifecycle stamps.

    Meta:
        - indexes on (`tenant`, `is_main`), (`tenant`, `status`), (`tenant`, `code`)
        - unique code per tenant, and at most one `is_main` branch per tenant.

    Helpers:
        allocate_next_code() locks the tenant row and returns the next code.
        transition()/mark_*() mutate status and append a BranchLifecycle event.
        clean() auto-populates `closed_at` when the status is CLOSED.
    """

    tenant = models.ForeignKey(
        "vs_tenants.Tenant",
        on_delete=models.PROTECT,
        related_name="branches",
        db_index=True,
    )

    name = models.CharField(max_length=255, help_text="Branch display name, e.g., 'Lekki Campus'")
    code = models.PositiveIntegerField(
        editable=False,
        null=False,
        help_text="Branch code unique per tenant (1..N).",
        db_index=True,
    )
    is_main = models.BooleanField(
        default=False,
        help_text="Marks the primary/main branch for this tenant.",
    )

    _type = models.CharField(max_length=80)  # e.g., Primary, Secondary, etc. --- optional freeform for now

    # Branch contact/location info
    address = models.CharField(max_length=255, blank=True, default="")
    email = models.EmailField(blank=True, default="")
    country = models.CharField(max_length=80, default="Nigeria")
    state = models.CharField(max_length=120, blank=True, default="")

    status = models.CharField(
        max_length=16,
        choices=BranchStatus.choices,
        default=BranchStatus.PENDING,
        db_index=True,
    )

    opened_at = models.DateTimeField(null=True, blank=True)
    closed_at = models.DateTimeField(null=True, blank=True)

    activated_at = models.DateTimeField(null=True, blank=True)
    deactivated_at = models.DateTimeField(null=True, blank=True)

    created_at = models.DateTimeField(default=timezone.now, editable=False)
    updated_at = models.DateTimeField(auto_now=True)

    # Tenant isolation: tenant users are automatically scoped to their tenant;
    # all_objects is the unscoped escape hatch for platform code.
    objects = TenantAwareManager()
    all_objects = models.Manager()

    class Meta:
        # The table did not move with the class. Renaming it would rewrite 39
        # foreign key constraints for a cosmetic gain.
        db_table = "vs_schools_branch"
        default_manager_name = "objects"
        base_manager_name = "all_objects"
        indexes = [
            models.Index(fields=["tenant", "is_main"]),
            models.Index(fields=["tenant", "status"]),
            models.Index(fields=["tenant", "code"]),
        ]
        constraints = [
            # The uniqueness the docstring has always promised and the table
            # had never had until phase B.
            models.UniqueConstraint(
                fields=["tenant", "code"],
                name="uq_branch_tenant_code",
            ),
            # A partial unique index. Every engine this repo runs on supports
            # them (PostgreSQL local/CI/staging, SQLite in apps.settings.test);
            # the MariaDB fallback that could not was retired 2026-06-12. Same
            # shape as vs_notifications.NotificationSetting and vs_user.User.
            models.UniqueConstraint(
                fields=["tenant"],
                condition=Q(is_main=True),
                name="uq_branch_one_main_per_tenant",
            ),
        ]
        ordering = ["-created_at"]

    def __str__(self) -> str:
        # Was ``school.slug:code``. ``Tenant.slug`` and ``School.slug`` can
        # diverge for a renamed school (``School.save`` syncs the tenant's
        # name and status but deliberately not its slug, because the tenant
        # slug is the public ``?tenant=`` key), so this string can differ from
        # the old one for such a school. It is a debugging repr, used nowhere
        # in an API response or an audit label.
        return f"{self.tenant.slug}:{self.code}"

    def clean(self):
        super().clean()

        self._assert_single_main()

        if self.status == BranchStatus.CLOSED and self.closed_at is None:
            self.closed_at = timezone.now()

    def _assert_single_main(self):
        """Reject a second main branch for the same tenant.

        ``uq_branch_one_main_per_tenant`` is what actually makes this race-proof;
        this check exists so the ordinary path fails as a field error the API can
        render, instead of surfacing an IntegrityError as a 500.
        """
        if not self.is_main or not self.tenant_id:
            return
        clash = Branch.all_objects.filter(tenant_id=self.tenant_id, is_main=True)
        if self.pk:
            clash = clash.exclude(pk=self.pk)
        if clash.exists():
            raise ValidationError({"is_main": "This tenant already has a main branch."})

    @staticmethod
    def allocate_next_code(*, tenant_id: int) -> int:
        """Allocate the next branch code (1..N) for one tenant.

        Locks the *tenant* row rather than the branch rows. An older version
        locked ``select_for_update().filter(school=school)``, which locks
        nothing at all when the owner has no branches yet, so two concurrent
        first-branch creates both read max=0 and both wrote code 1. The tenant
        row always exists (``tenant`` is non-nullable), so locking it serialises
        allocation for an empty tenant exactly as well as for a full one.

        Reads through ``all_objects`` deliberately: ``objects`` is the
        ``TenantAwareManager``, and under an ambient tenant context that differs
        from ``tenant_id`` (platform code creating a branch for a customer) it
        would aggregate over zero rows and hand back a duplicate code.

        Must run inside a transaction; :meth:`save` opens one.
        """
        Tenant.objects.select_for_update().only("id").get(pk=tenant_id)
        current_max = (
            Branch.all_objects
            .filter(tenant_id=tenant_id)
            .aggregate(m=Max("code"))["m"]
            or 0
        )
        return current_max + 1

    def save(self, *args, **kwargs):
        self._assert_single_main()

        # Allocate code only on first save if missing/zero.
        if not self.code:
            with transaction.atomic():
                self.code = Branch.allocate_next_code(tenant_id=self.tenant_id)
                super().save(*args, **kwargs)
            return
        return super().save(*args, **kwargs)

    # --- Lifecycle helpers ---

    def mark_active(self, *, actor_id: str, reason: str = ""):
        self.transition(to_state=BranchStatus.ACTIVE, actor_id=actor_id, reason=reason)

    def suspend(self, *, actor_id: str, reason: str):
        self.transition(to_state=BranchStatus.SUSPENDED, actor_id=actor_id, reason=reason)

    def reactivate(self, *, actor_id: str, reason: str = ""):
        self.transition(to_state=BranchStatus.ACTIVE, actor_id=actor_id, reason=reason)

    def mark_inactive(self, *, actor_id: str, reason: str):
        self.transition(to_state=BranchStatus.INACTIVE, actor_id=actor_id, reason=reason)

    # States meaning "no longer in service". Entering any of them stamps
    # `deactivated_at`; coming back to ACTIVE clears it again.
    OUT_OF_SERVICE_STATES = frozenset({
        BranchStatus.SUSPENDED,
        BranchStatus.INACTIVE,
        BranchStatus.CLOSED,
    })

    #: The complement of :attr:`OUT_OF_SERVICE_STATES`: a branch somebody may
    #: still be posted to. Stated positively because the authorisation filters
    #: in ``vs_rbac`` join through a *nullable* branch column, where a negative
    #: filter would also drop the whole-tenant grants (``branch IS NULL``) that
    #: must always count. Derived from the choices rather than written out, so a
    #: new lifecycle state cannot be silently treated as in service.
    #: Written as a set difference rather than a comprehension because a
    #: comprehension in a class body cannot see the class-level name above it.
    IN_SERVICE_STATES = frozenset(BranchStatus.values) - OUT_OF_SERVICE_STATES

    # The lifecycle edges a branch may travel. Two rules shape it: CLOSED is
    # terminal (a shut-down branch is re-created, not resurrected), and PENDING
    # is never a target, because "pending activation" is a fact about a branch
    # that has never opened and activation cannot be undone. SUSPENDED is only
    # reachable from ACTIVE - you cannot suspend what was never trading.
    ALLOWED_TRANSITIONS = {
        BranchStatus.PENDING: frozenset({
            BranchStatus.ACTIVE, BranchStatus.INACTIVE, BranchStatus.CLOSED,
        }),
        BranchStatus.ACTIVE: frozenset({
            BranchStatus.SUSPENDED, BranchStatus.INACTIVE, BranchStatus.CLOSED,
        }),
        BranchStatus.SUSPENDED: frozenset({
            BranchStatus.ACTIVE, BranchStatus.INACTIVE, BranchStatus.CLOSED,
        }),
        BranchStatus.INACTIVE: frozenset({
            BranchStatus.ACTIVE, BranchStatus.CLOSED,
        }),
        BranchStatus.CLOSED: frozenset(),
    }

    @transaction.atomic
    def transition(self, *, to_state: str, actor_id: str, reason: str = ""):
        """
        Moves the branch to `to_state` and records a `BranchLifecycle` row.

        Refuses any edge outside `ALLOWED_TRANSITIONS`. Asking for the state the
        branch is already in is a no-op, not an error, so the helpers below stay
        idempotent; the API surface rejects it explicitly instead.

        The status write and the audit row are one unit: a branch must never
        change state without the history entry that explains it.
        """
        from_state = self.status
        if from_state == to_state:
            return

        if to_state not in self.ALLOWED_TRANSITIONS.get(from_state, frozenset()):
            raise InvalidBranchTransition(from_state=from_state, to_state=to_state)

        now = timezone.now()
        self.status = to_state

        if to_state == BranchStatus.ACTIVE:
            # `activated_at` is the first activation and is never rewritten;
            # `deactivated_at` describes the current state, so it clears.
            if self.activated_at is None:
                self.activated_at = now
            self.deactivated_at = None
        elif to_state in self.OUT_OF_SERVICE_STATES:
            self.deactivated_at = now
            if to_state == BranchStatus.CLOSED and self.closed_at is None:
                # Mirrors clean(); transition() saves with update_fields and so
                # never runs full_clean().
                self.closed_at = now

        self.save(update_fields=[
            "status",
            "activated_at",
            "deactivated_at",
            "closed_at",
            "updated_at",
        ])

        BranchLifecycle.objects.create(
            branch=self,
            from_state=from_state,
            to_state=to_state,
            # Callers pass the actor as a User; actor_id is a CharField.
            actor_id=str(actor_id or ""),
            reason=reason or "",
        )


class BranchLifecycle(models.Model):
    """
    Audit log entry for a Branch status transition.

    Rows are created by `Branch.transition()`, capturing who initiated the change,
    the previous and new states, an optional free-form reason, and when it occurred.
    Indexed lookups support timeline views per branch or filtering by resulting state
    via (`branch`, `occurred_at`) and (`branch`, `to_state`).

    Travels with ``Branch`` because ``Branch.transition`` writes it: leaving it
    behind would have meant this app importing the schools app on every
    lifecycle change.
    """

    branch = models.ForeignKey(
        Branch,
        on_delete=models.CASCADE,
        related_name="lifecycle_events",
    )

    from_state = models.CharField(max_length=32, choices=BranchStatus.choices)
    to_state = models.CharField(max_length=32, choices=BranchStatus.choices)

    actor_id = models.CharField(max_length=120)
    # The column is NOT NULL, so default=None made every writer that omitted
    # `reason` raise IntegrityError.
    reason = models.TextField(blank=True, default="")

    occurred_at = models.DateTimeField(default=timezone.now, db_index=True)

    class Meta:
        db_table = "vs_schools_branchlifecycle"
        indexes = [
            models.Index(fields=["branch", "occurred_at"]),
            models.Index(fields=["branch", "to_state"]),
        ]


class TenantOwnedModel(models.Model):
    """Abstract contract for rows owned by exactly one tenant."""

    tenant = models.ForeignKey(
        "vs_tenants.Tenant",
        on_delete=models.PROTECT,
        related_name="+",
        db_index=True,
    )

    class Meta:
        abstract = True


class TenantDocumentSequence(models.Model):
    """Last number allocated for one tenant, document code, and local date.

    All human-facing document numbers route through this tenant-level counter so
    ledger entities and branches owned by the same tenant cannot open competing
    series for the same document code.
    """

    tenant = models.ForeignKey(
        Tenant, on_delete=models.PROTECT, related_name="document_sequences",
    )
    document_code = models.CharField(max_length=16)
    date = models.DateField()
    last_number = models.PositiveIntegerField(default=0)
    created_at = models.DateTimeField(default=timezone.now, editable=False)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["tenant", "document_code", "date"],
                name="uniq_tenant_docseq_code_date",
            ),
        ]
        indexes = [
            models.Index(
                fields=["tenant", "date"], name="tenant_docseq_tenant_date_idx",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.tenant_id}/{self.document_code}/{self.date}: {self.last_number}"

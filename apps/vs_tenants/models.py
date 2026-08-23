from __future__ import annotations

from django.core.exceptions import ValidationError
from django.core.validators import RegexValidator
from django.db import models, transaction
from django.db.models import Max, Q
from django.utils import timezone

from vs_rbac.managers import TenantAwareManager

from .exceptions import (
    BranchNotInService,
    InvalidBranchTransition,
    LastBranchCannotLeaveService,
    MainBranchCannotLeaveService,
)


tenant_slug_validator = RegexValidator(
    regex=r"^[a-z0-9]+(?:-[a-z0-9]+)*$",
    message="Slug must be lowercase letters/numbers separated by single hyphens.",
)


#: Slugs no customer may take, because each one is (or will be) a hostname the
#: platform itself answers on. Every tenant is served from its own subdomain -
#: ``bright-star.xvs.codexng.com``, matched by the CORS origin regex in
#: ``settings.base`` - so a tenant slug is a DNS label first and an identifier
#: second, and a school called "Support Academy" that took ``support`` would be
#: served the help site instead of its own.
#:
#: It lives here, in the platform app, rather than in ``schools.vs_schools``
#: where it began. The names being protected are platform infrastructure, not
#: school vocabulary: an ORGANIZATION tenant and a VIGIL clinic group get a
#: subdomain off the same wildcard and must be held to the same list, and the
#: engines may not import the schools app to reach it.
#:
#: It sits *beside* ``tenant_slug_validator`` rather than inside it, and is
#: enforced in :meth:`Tenant.save`, for one blunt reason: field validators do
#: not run on ``Tenant.objects.create()``, which is how every writer in this
#: codebase makes a tenant. A validator on the field would have looked like
#: enforcement and caught nothing. Extend the set here; no migration is needed
#: because nothing about it is stored in the field definition.
RESERVED_TENANT_SLUGS = frozenset({
    # The bare product and marketing hosts.
    "www", "xvs", "vigil", "intranet", "blog", "docs", "help", "support",
    "status", "portal", "about", "careers", "legal", "privacy", "terms",
    # The API and its neighbours.
    "api", "app", "auth", "login", "logout", "signup", "register", "account",
    "accounts", "schema", "graphql", "webhook", "webhooks", "oauth", "sso",
    # Infrastructure and delivery.
    "admin", "root", "system", "internal", "static", "assets", "cdn", "media",
    "files", "img", "images", "download", "downloads", "mail", "email", "smtp",
    "imap", "ns", "ns1", "ns2", "ftp", "vpn", "proxy", "metrics", "health",
    # Environments. A school called "Dev" would otherwise take the host the
    # next environment needs.
    "dev", "staging", "test", "demo", "sandbox", "preview", "local",
    # The platform tenant itself, seeded by vs_tenants 0002.
    "codex",
    # Commercial surfaces that will want their own host.
    "billing", "payments", "pay", "checkout",
})


def slug_is_reserved(slug: str) -> bool:
    """Whether ``slug`` collides with a platform hostname, after normalising."""
    return (slug or "").strip().lower() in RESERVED_TENANT_SLUGS


class Tenant(models.Model):
    """A customer or platform security boundary.

    Numeric primary keys are deliberately internal. ``slug`` is the stable,
    human-readable identifier accepted by tenant-scoped APIs - and, since every
    tenant is served from its own subdomain, it is the sign-in address too.
    Two rules follow, both enforced in :meth:`save` so no writer can miss them:
    it may not be one of :data:`RESERVED_TENANT_SLUGS`, and once the tenant has
    gone live it may not change at all.
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

    # The statuses whose users may sign in and assert this tenant. PENDING is
    # here because a school has to onboard itself before it goes live (FR-012);
    # being admitted says nothing about which surfaces are open, which is
    # settled per view by vs_rbac.permissions.TenantSurfaceAllowed. SUSPENDED
    # and INACTIVE are refused outright, as an unknown slug is.
    AUTHENTICABLE_STATUSES = (Status.ACTIVE, Status.PENDING)

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
    # When this tenant entered the PENDING spell it is in now; null whenever it
    # is not PENDING. `created_at` cannot answer that question, which is the
    # whole reason this column exists: a school suspended for an abandoned
    # onboarding and later put back to PENDING is pending again from the moment
    # it was reinstated, while its creation date stays where it always was. A
    # sweep reading `created_at` would expire such a school again on its very
    # next run, forever.
    pending_since = models.DateTimeField(null=True, blank=True, db_index=True)
    # When the tenant was last told its pending spell is about to run out. It
    # belongs to the spell, not to the tenant: a sweep that asked only "is this
    # school within the warning window?" would answer yes every day and send
    # the same warning a dozen times, and one that never cleared the stamp
    # would leave a reinstated school silently unwarnable for ever.
    expiry_warned_at = models.DateTimeField(null=True, blank=True)
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
        self._assert_slug_allowed()

    def _assert_slug_allowed(self):
        """Refuse a reserved slug, whoever is writing it.

        PLATFORM tenants are exempt because they *are* the infrastructure the
        list protects: ``codex`` is a reserved name precisely so that no school
        can take the host the platform tenant already answers on.
        """
        if self.kind == self.Kind.PLATFORM:
            return
        if slug_is_reserved(self.slug):
            raise ValidationError({
                "slug": (
                    "This address is reserved for the platform. Choose another."
                )
            })

    def _assert_slug_unchanged_once_live(self):
        """Freeze the slug from the moment the tenant first goes live.

        The slug is the school's sign-in address
        (``bright-star.xvs.codexng.com``, and the ``?tenant=`` key that
        ``vs_rbac.authentication`` resolves against). Changing it after go-live
        moves every parent, teacher and pupil to an address nobody told them
        about, and the old one stops resolving the same second.

        "Live" is ``activated_at is not None or status == ACTIVE`` - live at
        some point in the past, or live now. The first half carries the rule,
        and it is deliberately not ``status == ACTIVE`` on its own:

        * ``activated_at`` is written once and never cleared - :meth:`activate`
          keeps the first value, and ``School.save()`` mirrors the school's own
          stamp - so it is exactly the question "has this address ever been
          handed out?".
        * ``status == ACTIVE`` alone would let a school that is suspended for
          an unpaid invoice, or temporarily deactivated, rename itself while it
          is off. It comes back on reinstatement at an address its families
          cannot reach - the one school least able to absorb that. The status
          stays in the test only as a second reading of "live now", covering a
          row activated by some path that forgot the stamp.
        * The onboarding gate's ``ReadinessState.LIVE`` is not used because its
          own constants file calls it "a projection, not the source of truth",
          and a tenant that is not a school has no readiness row at all.

        PENDING is the interesting exclusion, because a pending school's admins
        *can* already sign in - ``AUTHENTICABLE_STATUSES`` admits them so they
        can onboard. They are also the only people who can: the handful of
        admins setting the school up, one of whom is the person fixing the typo.
        Bright Star is created as ``bright-star`` when it meant
        ``bright-star-academy``; before go-live that costs two admins a
        re-bookmark, and after it costs every parent their sign-in.
        """
        if not self.pk:
            return
        stored = (
            Tenant.objects.filter(pk=self.pk)
            .values("slug", "activated_at", "status")
            .first()
        )
        if stored is None or stored["slug"] == self.slug:
            return
        has_been_live = (
            stored["activated_at"] is not None
            or stored["status"] == self.Status.ACTIVE
        )
        if not has_been_live:
            return
        raise ValidationError({
            "slug": (
                "This school is live, so its sign-in address is fixed. "
                "Changing it would lock every user out of the address they "
                "already use."
            )
        })

    def save(self, *args, **kwargs):
        # Both guards live here rather than on the field, because nothing in
        # this codebase creates a tenant through a form: it is
        # ``Tenant.objects.create()`` from ``School.save()``, from the
        # onboarding services and from the seeds, and field validators never
        # run on that path.
        self.slug = (self.slug or "").strip().lower()
        self._assert_slug_allowed()
        self._assert_slug_unchanged_once_live()
        return super().save(*args, **kwargs)

    @classmethod
    def pending_since_for(cls, *, new_status, previous_status, current):
        """The value :attr:`pending_since` must take for a status write.

        One rule in one place, because more than one writer has to get it
        right: ``School.save()`` mirrors a school's status onto its tenant, and
        the onboarding lifecycle service writes a tenant that has no school.

        * Leaving PENDING clears it, so the column always describes the spell
          the tenant is in now rather than a spell it once had.
        * Entering PENDING stamps it.
        * Staying PENDING keeps the stamp already there, so an ordinary edit (a
          rename, a metadata fix) does not restart the clock the expiry sweep
          reads.
        """
        if new_status != cls.Status.PENDING:
            return None
        if previous_status == cls.Status.PENDING and current is not None:
            return current
        return timezone.now()

    @classmethod
    def pending_stamps_for(cls, *, new_status, previous_status, pending_since, warned_at):
        """Both pending columns for one status write, as a pair.

        A warning describes the spell it was sent during. So whenever
        ``pending_since`` changes (a new spell begins, or the spell ends), the
        warning stamp goes with it, and a school put back into onboarding is
        warned again in its new cycle instead of being silently skipped for
        ever. When the spell simply continues, both are left exactly as they
        were.
        """
        new_pending_since = cls.pending_since_for(
            new_status=new_status,
            previous_status=previous_status,
            current=pending_since,
        )
        if new_pending_since != pending_since:
            return new_pending_since, None
        return new_pending_since, warned_at

    def activate(self):
        self.status = self.Status.ACTIVE
        self.activated_at = self.activated_at or timezone.now()
        self.deactivated_at = None
        # Not pending any more, so neither pending column describes anything.
        self.pending_since = None
        self.expiry_warned_at = None

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
    """One physical site owned by a tenant: a school site, a clinic, a store.

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
        name: Display label such as "Lekki Branch".
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
        promote_to_main() hands `is_main` over, demoting the incumbent first.
        clean() auto-populates `closed_at` when the status is CLOSED.

    The main branch may not be taken out of service. `transition()` refuses it
    for every out-of-service state, so a tenant is never left with a canonical
    site that is suspended, deactivated or - permanently, since CLOSED is
    terminal - shut. Hand `is_main` over with `promote_to_main()` first.
    """

    tenant = models.ForeignKey(
        "vs_tenants.Tenant",
        on_delete=models.PROTECT,
        related_name="branches",
        db_index=True,
    )

    name = models.CharField(max_length=255, help_text="Branch display name, e.g., 'Lekki Branch'")
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

    # Free-form label for the site: "Primary", "Secondary", "Nursery". Nothing
    # branches on it - it is rendered in the branch list and detail payloads,
    # carried by the import template as a not-required column, and read nowhere
    # else - so it is declared optional, which is what its comment always said
    # it was. It was ``CharField(max_length=80)`` with no ``blank``: optional in
    # prose and required in the schema. Every row made outside the serializers
    # (``seed_import``, a data migration, the shell, the test factories) stored
    # ``""`` and was then permanently unpatchable through the API, because
    # ``BranchUpdateSerializer`` runs ``full_clean()`` over the whole instance
    # and the blank it refused was one nobody had touched.
    _type = models.CharField(max_length=80, blank=True, default="")

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

    def _assert_may_leave_service(self, to_state: str) -> None:
        """Refuse to take the tenant's canonical site out of service.

        Only ``is_main`` rows are checked, and that is the whole invariant
        rather than a shortcut: a tenant has exactly one main branch
        (``uq_branch_one_main_per_tenant``), so retiring a *non*-main branch
        can never leave the tenant without an in-service main one, while
        retiring the main branch always does.

        Every out-of-service state is guarded, not only CLOSED. CLOSED is the
        state that makes the damage permanent - it is terminal, and the partial
        unique index then refuses to hand ``is_main`` to any survivor - but a
        SUSPENDED or INACTIVE main branch is already wrong today: every reader
        of ``School.main_branch`` and every default-branch pick lands on a site
        nobody may be posted to, silently. Deriving the set from
        :attr:`OUT_OF_SERVICE_STATES` also means a lifecycle state added later
        is guarded the moment it is declared out of service.

        Which refusal is raised depends on whether there is anything to hand
        over to, because the advice differs: a multi-branch school promotes a
        sibling, and a one-branch school has no sibling and must wind the
        school down instead.
        """
        if to_state not in self.OUT_OF_SERVICE_STATES or not self.is_main:
            return

        has_sibling = (
            Branch.all_objects
            .filter(tenant_id=self.tenant_id)
            .exclude(pk=self.pk)
            .exists()
        )
        if has_sibling:
            raise MainBranchCannotLeaveService(
                branch_name=self.name, to_state=to_state,
            )
        raise LastBranchCannotLeaveService(
            branch_name=self.name, to_state=to_state,
        )

    @transaction.atomic
    def promote_to_main(self, *, actor_id: str = "", reason: str = ""):
        """Make this branch the tenant's main one, demoting the incumbent.

        The way out of the refusals above, so it has to actually work. Two
        things make it safe:

        * The incumbent is demoted in its own UPDATE *before* this row is
          promoted. ``uq_branch_one_main_per_tenant`` is an ordinary (not
          deferrable) partial unique index, so the two writes cannot be
          reordered or batched: promoting first would trip the index even
          though the end state is legal.
        * The tenant row is locked first, exactly as
          :meth:`allocate_next_code` does. Two admins promoting two different
          branches at the same moment would otherwise both read one incumbent,
          both demote it, and both insert a main branch.

        An out-of-service branch is refused: promoting a closed branch would
        rebuild the dead end by hand.

        No ``BranchLifecycle`` row is written, and ``actor_id`` is taken only
        so callers can pass it symmetrically with :meth:`transition`. That
        table records status edges (``from_state`` -> ``to_state``) and a
        handover changes no status; the branch update serializer already emits
        the ordinary audit event, whose diff carries ``is_main``.
        """
        if self.status not in self.IN_SERVICE_STATES:
            raise BranchNotInService(branch_name=self.name, status=self.status)

        if self.is_main:
            # Idempotent on purpose: the serializer and any retry can call this
            # without first asking whether it is needed.
            return self

        Tenant.objects.select_for_update().only("id").get(pk=self.tenant_id)
        demoted = list(
            Branch.all_objects
            .filter(tenant_id=self.tenant_id, is_main=True)
            .exclude(pk=self.pk)
            .values_list("pk", flat=True)
        )
        if demoted:
            Branch.all_objects.filter(pk__in=demoted).update(
                is_main=False, updated_at=timezone.now(),
            )

        self.is_main = True
        self.save(update_fields=["is_main", "updated_at"])
        return self

    @transaction.atomic
    def transition(self, *, to_state: str, actor_id: str, reason: str = ""):
        """
        Moves the branch to `to_state` and records a `BranchLifecycle` row.

        Refuses any edge outside `ALLOWED_TRANSITIONS`. Asking for the state the
        branch is already in is a no-op, not an error, so the helpers below stay
        idempotent; the API surface rejects it explicitly instead.

        The status write and the audit row are one unit: a branch must never
        change state without the history entry that explains it.

        Every route to a branch's status runs through here - the ``mark_*``
        helpers above, the API's transition serializer, the shell - so the
        main-branch guard below is stated once and cannot be walked around by
        adding another caller.
        """
        from_state = self.status
        if from_state == to_state:
            return

        if to_state not in self.ALLOWED_TRANSITIONS.get(from_state, frozenset()):
            raise InvalidBranchTransition(from_state=from_state, to_state=to_state)

        self._assert_may_leave_service(to_state)

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

    # A creation event has no state to come from, and ``BranchCreateSerializer``
    # has always written ``from_state=""`` for one. ``to_state`` is the opposite
    # and stays required: an event that does not say where the branch went is
    # not an event.
    from_state = models.CharField(
        max_length=32, choices=BranchStatus.choices, blank=True, default="",
    )
    to_state = models.CharField(max_length=32, choices=BranchStatus.choices)

    # ``Branch.transition`` writes ``str(actor_id or "")``, so a system-driven
    # transition - the onboarding sweep, a management command - stores a blank
    # here by design. Same fix as ``reason`` below, which this sweep missed the
    # first time round.
    actor_id = models.CharField(max_length=120, blank=True, default="")
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

# models.py
# All models for the vs_users module in one flat file.
#
# Contents (in order):
#   TimeStampedModel      - shared abstract base
#   User + UserManager    - platform-wide custom user model (AUTH_USER_MODEL)
#   UserInvitation        - invitation expiry/usage gate (UUID-based, no token)
#   LoginSession          - application-level session tracker
#   AuthAttempt           - every login attempt, success or failure
#   AccountLockout        - per-user brute-force lockout state
#   PasswordResetRequest  - hashed reset token store
#   AuthEventLog          - append-only audit event log

from __future__ import annotations

import hashlib
from datetime import timedelta

import uuid
from django.conf import settings
from django.contrib.auth.base_user import AbstractBaseUser, BaseUserManager
from django.contrib.auth.models import PermissionsMixin
from django.core.exceptions import ValidationError
from django.db import models, transaction
from django.db.models import Q, Max
from django.db.models.functions import Lower
from django.utils import timezone

from vs_tenants.models import Branch, Tenant
from vs_rbac.managers import TenantAwareManager

from . import email_normalization

# The one fact that used to be recorded twice - once as the tenant's kind and
# once as a ``CX_STAFF`` persona on every one of its users. Bound to a name
# here so the several places that ask it read alike.
PLATFORM_TENANT_KIND = Tenant.Kind.PLATFORM


# =============================================================================
# TimeStampedModel
# =============================================================================

class TimeStampedModel(models.Model):
    created_at = models.DateTimeField(default=timezone.now, editable=False)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        abstract = True

# =============================================================================
# UserManager + User
# =============================================================================

class UserQuerySet(models.QuerySet):
    """The bulk writes, which do not go through ``User.save()``.

    ``bulk_create`` and ``bulk_update`` build their SQL from the instances
    directly, so the normalisation in ``save()`` never runs for them and a
    mixed-case address would land in the table. They are folded here instead.

    ``QuerySet.update(email=...)`` cannot be intercepted this way - the value
    is an expression, not an instance - and neither can raw SQL. Those are
    caught by the ``ck_user_email_lowercase`` database constraint, which is the
    only guard that holds for every path including psql.
    """

    # The helper is applied to the attribute rather than through
    # User._normalize_email(): UserManager is use_in_migrations, so these two
    # methods also run against the historical model rebuilt from migration
    # state, and that model has the fields but none of the methods.

    def bulk_create(self, objs, *args, **kwargs):
        objs = list(objs)  # may arrive as a generator; normalising consumes it
        for obj in objs:
            obj.email = email_normalization.normalize_email(obj.email)
        return super().bulk_create(objs, *args, **kwargs)

    def bulk_update(self, objs, fields, *args, **kwargs):
        objs = list(objs)
        if 'email' in fields:
            for obj in objs:
                obj.email = email_normalization.normalize_email(obj.email)
        return super().bulk_update(objs, fields, *args, **kwargs)


class UserManager(BaseUserManager.from_queryset(UserQuerySet)):
    use_in_migrations = True

    # Django's inherited normalize_email() lowercases only the domain. Every
    # path that reaches it - this manager, createsuperuser, anything that calls
    # User.objects.normalize_email() directly - must get the whole address
    # folded, so it delegates to the shared helper. Written module-qualified on
    # purpose: a bare name here would read like a recursive call.
    @classmethod
    def normalize_email(cls, email):
        return email_normalization.normalize_email(email)

    def _create_user(self, email: str, password=None, **extra_fields):
        if not email:
            raise ValueError('Email is required')
        email = self.normalize_email(email)
        user  = self.model(email=email, **extra_fields)
        if password:
            user.set_password(password)
        else:
            # No password on creation.
            # The user sets their own password during activation
            # via the invitation link - see models/invitation.py.
            user.set_unusable_password()
        user.full_clean()
        user.save(using=self._db)
        return user

    def create_user(self, email: str, password=None, **extra_fields):
        extra_fields.setdefault('is_staff', False)
        extra_fields.setdefault('is_superuser', False)
        return self._create_user(email, password, **extra_fields)

    def create_superuser(self, email: str, password=None, **extra_fields):
        extra_fields.setdefault('is_staff', True)
        extra_fields.setdefault('is_superuser', True)
        if not extra_fields.get('is_staff'):
            raise ValueError('Superuser must have is_staff=True.')
        if not extra_fields.get('is_superuser'):
            raise ValueError('Superuser must have is_superuser=True.')
        return self._create_user(email, password, **extra_fields)


# ─────────────────────────────────────────────────────────────────────────────
# User model
# ─────────────────────────────────────────────────────────────────────────────

# What a caller is told when an address is refused because it already belongs
# to an account at ANOTHER tenant and sign-in cannot yet tell the two apart.
#
# It names no tenant, no school, no account and no person, and it does not say
# that an account exists elsewhere. An administrator at Greenfield who typed
# ada@gmail.com must not be able to learn from this refusal that Bright Star
# has a parent by that address - that is the same enumeration concern the
# barcode preview was scoped for, and it applies to any message a customer's
# own staff can trigger with a guess.
CROSS_TENANT_EMAIL_REFUSAL = (
    'This email address cannot be used for a new account here yet. Sign-in '
    'does not yet name the tenant it is addressed to, so two accounts sharing '
    'one address could not be told apart. Please use a different address, or '
    'contact CodeX support.'
)


class User(AbstractBaseUser, PermissionsMixin, TimeStampedModel):
    """
    Every person who logs into any part of CodeX Vision - Vision staff,
    school admins, teachers, students, parents - is a record here.
    """

    workflow_document_type = "PLATFORM_USER_CREATION"

    # ── Choices ──────────────────────────────────────────────────────────────

    # There is deliberately no ``UserType``.
    #
    # A persona column can disagree with reality, and nothing could ever
    # detect the disagreement: a row marked STUDENT with no student record
    # anywhere was writable and undetectable. Every question the column used to
    # answer is now asked of something that cannot be wrong about itself.
    #
    #   "does this person work for the platform?"  ->  ``is_platform_user``,
    #       which reads the kind of the tenant the account actually belongs to;
    #   "is this person a parent?"                 ->  they have a guardian
    #       record;
    #   "what does this person do here?"           ->  their role, which is
    #       also the only thing that decides what they may do.
    #
    # CX_STAFF and "belongs to the PLATFORM tenant" were the same fact recorded
    # twice, with nothing holding the two copies together. STUDENT and PARENT
    # were read by no line of code at all.

    class Status(models.TextChoices):
        DRAFT            = 'DRAFT',            'Draft'
        PENDING_APPROVAL = 'PENDING_APPROVAL', 'Pending Approval'
        PENDING          = 'PENDING',          'Pending Activation'
        ACTIVE           = 'ACTIVE',           'Active'
        SUSPENDED        = 'SUSPENDED',        'Suspended'
        LOCKED           = 'LOCKED',           'Locked (security)'
        DEACTIVATED      = 'DEACTIVATED',      'Deactivated'
        REJECTED         = 'REJECTED',         'Creation Rejected'

    # ── Which statuses may authenticate, and which may hold a password ────────
    #
    # These two sets are the single answer to a question that used to be asked
    # in five places, each of them by listing the statuses it wanted to REFUSE
    # and letting everything else through:
    #
    #   * ``LoginService._check_status`` named PENDING, LOCKED, SUSPENDED and
    #     DEACTIVATED, so DRAFT, PENDING_APPROVAL and REJECTED signed in;
    #   * ``IsAuthenticatedAndActive`` (vs_rbac) named SUSPENDED, LOCKED and
    #     DEACTIVATED, as string literals, so the same three passed;
    #   * ``AdminPasswordResetView`` named none at all, so an admin could put a
    #     working password on any account in any state;
    #   * ``PasswordService.request_reset`` named only DEACTIVATED;
    #   * ``PasswordService.confirm_reset`` promoted LOCKED and PENDING and left
    #     every other status where it was, holding a brand-new usable password.
    #
    # Naming the refusals is the defect, not a detail of it. Every status added
    # to the enum since - DRAFT, PENDING_APPROVAL and REJECTED all postdate that
    # code - became able to sign in the moment it was added, silently, because
    # no one edited five lists. So the sets below enumerate what is PERMITTED
    # and everything absent is refused. A status added tomorrow can do nothing
    # until it is deliberately written into one of them.

    #: The only statuses a sign-in may succeed from. Not a shorthand for
    #: "not obviously bad": PENDING has been invited but has not set a password,
    #: LOCKED is mid-incident, SUSPENDED and DEACTIVATED are administratively
    #: closed, and DRAFT / PENDING_APPROVAL / REJECTED were never granted a
    #: login at all. Exactly one status means "this person may work today".
    SIGN_IN_STATUSES = frozenset({Status.ACTIVE})

    #: The statuses that may hold a usable password. Wider than SIGN_IN_STATUSES
    #: on purpose, and the two cannot be collapsed into one set:
    #:
    #:   * PENDING is the normal invited-but-not-activated path - the invitation
    #:     link exists precisely to put a first password on the account;
    #:   * LOCKED is unlocked BY a reset, so refusing one would strand the
    #:     account behind the lockout it is meant to clear;
    #:   * SUSPENDED may be given a new password (an admin resetting a
    #:     suspected-compromised credential before reinstating) and still may
    #:     not sign in, which is what suspension means.
    #:
    #: Absent, and refused: DEACTIVATED, which is terminal and which the
    #: self-service reset already refused while the admin reset did not; and
    #: DRAFT, PENDING_APPROVAL and REJECTED, which are not accounts anyone has
    #: been granted yet. A credential on one of those is the bug this exists to
    #: close - see the two properties below and their call sites.
    PASSWORD_STATUSES = frozenset({
        Status.ACTIVE, Status.PENDING, Status.LOCKED, Status.SUSPENDED,
    })

    class Gender(models.TextChoices):
        MALE    = 'MALE',   'Male'
        FEMALE  = 'FEMALE', 'Female'

    # ── Tenant scoping ────────────────────────────────────────────────────────

    tenant = models.ForeignKey(
        "vs_tenants.Tenant",
        on_delete=models.PROTECT,
        related_name="users",
        help_text="Canonical home tenant.",
    )

    branch = models.ForeignKey(
        Branch, on_delete=models.PROTECT,
        related_name='users', null=True, blank=True,
        help_text=(
            'The one branch this person is posted to. NULL means "across the '
            'whole tenant" for a tenant user, and is the only legal value for '
            'Vision Staff, who belong to no tenant branch at all.'
        ),
    )

    # ── Identity ──────────────────────────────────────────────────────────────

    # NOT unique on its own: one real address may be a login at more than one
    # customer of this platform. Ada Okoye has a child at Bright Star and
    # another at Greenfield and uses ada@gmail.com at both, and neither school
    # may learn anything about the other. Uniqueness is per tenant instead -
    # see uq_user_email_per_tenant below.
    email      = models.EmailField(max_length=254)
    first_name = models.CharField(max_length=100)
    last_name  = models.CharField(max_length=100)
    gender     = models.CharField(max_length=20, choices=Gender.choices, blank=True, default='')
    phone      = models.CharField(max_length=32, blank=True, null=True, default='')

    # Auto-assigned on create; unique within the TENANT for tenant-scoped
    # users - which is what unique_uid_per_tenant below actually enforces, and
    # a school is one tenant, not the key - and unique across all CX staff.
    # Starts at 10.
    uid = models.PositiveIntegerField(null=True, blank=True, editable=False)

    # ── Status ────────────────────────────────────────────────────────────────

    role      = models.CharField(max_length=120, blank=True, default='')  # Denormalized display name; actual grants live in TenantUserRoleAssignment.
    status    = models.CharField(max_length=16, choices=Status.choices, default=Status.PENDING)

    # ── Django auth flags ─────────────────────────────────────────────────────

    is_staff = models.BooleanField(default=False)

    # False until the user completes activation via the invitation link.
    # Set to True by InvitationService.activate().
    is_active = models.BooleanField(default=False)
    # activation_token is a UUID used to validate the invitation link.
    activation_key = models.UUIDField(unique=True, default=uuid.uuid4, editable=False)

    # ── Audit timestamps ──────────────────────────────────────────────────────

    password_changed_at = models.DateTimeField(null=True, blank=True)
    last_login_at       = models.DateTimeField(null=True, blank=True)

    # Tracks which admin created this user - useful for audit and support.
    invited_by = models.ForeignKey(
        'self', on_delete=models.SET_NULL,
        null=True, blank=True, related_name='invited_users',
    )
    # Denormalized snapshot of the inviter's name at invite time.
    # Survives deletion of the inviting admin's account.
    invited_by_name = models.CharField(max_length=200, blank=True, default='')

    # ── Auth config ───────────────────────────────────────────────────────────

    USERNAME_FIELD  = 'email'
    REQUIRED_FIELDS = []
    objects = UserManager()

    # ── Meta ──────────────────────────────────────────────────────────────────

    class Meta:
        db_table = 'vs_users_user'
        constraints = [
            # THE BRANCH RULE IS NOT HERE. It is a database TRIGGER, installed
            # by vs_user migration 0009, and this comment is the signpost to it.
            #
            # The rule itself is unchanged: a user on a PLATFORM-kind tenant
            # must not be bound to a branch. Platform staff work for the
            # platform, and the platform tenant owns no branches for them to be
            # bound to. Every tenant user MAY carry one, and a NULL means
            # "across the whole tenant" - the same first-class value the
            # academic structure and procurement documents already use. It does
            # not mean "no branches exist".
            #
            # It used to be ``ck_vision_staff_no_branch``, a CheckConstraint
            # reading ``user_type='CX_STAFF'``. That was a correlated proxy for
            # the tenant kind, not the rule, and the two could drift apart in
            # silence. Stating the real rule needs the tenant's ``kind``, which
            # lives in another table - and a CHECK constraint is evaluated per
            # row and may not contain a subquery, on PostgreSQL or anywhere
            # else, so no CheckConstraint can express it. Django says so first:
            # a relational lookup in a CheckConstraint raises FieldError
            # ("Joined field references are not permitted in this query").
            #
            # A trigger can, so the guarantee is kept rather than downgraded to
            # a Python-only check. Two triggers, in fact - see the migration -
            # because the pair (user.tenant.kind, user.branch_id) can be broken
            # from either side: by writing the user row, or by flipping an
            # existing tenant to PLATFORM underneath its users.
            #
            # ``User.branch_assignment_error`` states the same rule for Python,
            # and ``clean()`` and ``UserCreateSerializer`` both consult it, so
            # the database and the application cannot drift apart.

            # uid is unique within its tenant - for every user, now that there
            # is no persona to split them by.
            #
            # This was two constraints: uid unique per tenant for non-CX users,
            # and uid unique globally for CX staff. They were never two rules.
            # All platform staff live in the one PLATFORM tenant, so "unique
            # among CX staff" and "unique within the platform tenant" pick out
            # exactly the same rows - the second constraint was the first one
            # spelled differently because the persona was doing the tenant's
            # job. One rule states it for everybody.
            models.UniqueConstraint(
                fields=['tenant', 'uid'],
                name='unique_uid_per_tenant',
            ),
            # No address reaches this table with an uppercase character in it.
            # save(), full_clean() and the bulk writes all fold the value, but
            # a QuerySet.update(), a data migration or a hand-typed psql
            # statement goes round every one of them. Every existence check on
            # this column now compares with '=' against a normalised input, so
            # a single stray capital would make an account invisible to the
            # check that is supposed to find it - and, once Phase 3 lands,
            # invisible to the constraint that is supposed to reject it.
            models.CheckConstraint(
                condition=Q(email=Lower('email')),
                name='ck_user_email_lowercase',
            ),
            # One address, one account, PER TENANT.
            #
            # Written on the plain columns rather than as a Lower('email')
            # expression, and it is genuinely case-insensitive all the same,
            # because ck_user_email_lowercase above holds every stored address
            # to its folded form: a row that could defeat this constraint by
            # capitalising a letter cannot be written in the first place.
            #
            # The plain form also earns its keep twice. Its index is on
            # (tenant_id, email), which the ordinary equality lookups every
            # caller writes - filter(tenant=..., email=...) - can actually use;
            # an index on (tenant_id, lower(email)) would serve neither those
            # nor Django's __iexact, which compiles to UPPER() on PostgreSQL
            # (backends/postgresql/operations.py lookup_cast) and so matches no
            # LOWER() index either. And a `fields` constraint is one Django's
            # own validation can report in words - UniqueConstraint.validate
            # falls back to unique_error_message() for it, where an expression
            # constraint can only say that some named constraint was violated.
            models.UniqueConstraint(
                fields=['tenant', 'email'],
                name='uq_user_email_per_tenant',
            ),
        ]
        indexes = [
            models.Index(fields=['tenant', 'status']),
            models.Index(fields=['tenant', 'branch']),
            models.Index(fields=['email', 'status']),
        ]
        ordering = ['-created_at']

    # ── Validation ────────────────────────────────────────────────────────────

    @classmethod
    def branch_assignment_error(cls, tenant, has_branch: bool) -> str | None:
        """The branch rule, stated once, for every writer to consult.

        Returns the refusal in words, or ``None`` when the pairing is legal.

        The rule is short: a user on the PLATFORM tenant takes no branch, and
        everybody else may or may not have one. It used to be asked of
        ``user_type``, which only correlated with the answer; asking the tenant
        is asking the fact.

        Takes ``has_branch`` as a bool rather than the branch itself so a caller
        holding only an unresolved reference - the create serializer, which must
        judge a platform hire before it looks a branch up - can ask the same
        question as a caller holding a saved row. ``tenant`` is likewise taken
        as the object (or ``None``) rather than read off an instance, for the
        same caller.

        The database enforces this too. See the note in ``Meta.constraints``:
        it is a trigger rather than a CheckConstraint, because the rule spans
        two tables.
        """
        if getattr(tenant, 'kind', None) == PLATFORM_TENANT_KIND and has_branch:
            return 'Platform staff must not be assigned to a branch.'
        return None

    def clean(self):
        super().clean()
        # Reads tenant_id first so an unsaved instance with no tenant yet asks
        # nothing of the database and simply passes - the missing tenant is
        # reported by clean_fields(), which has already run.
        tenant = self.tenant if self.tenant_id else None
        error = self.branch_assignment_error(tenant, bool(self.branch_id))
        if error:
            raise ValidationError(error)
        self._guard_cross_tenant_email()

    def validate_unique(self, exclude=None):
        """Keep a same-tenant duplicate address reported ON the email field.

        ``unique=True`` used to make this Django's own job, and its error came
        back as ``{'email': [...]}``. The replacement is a two-column
        UniqueConstraint, and Django reports a multi-field constraint under
        NON_FIELD_ERRORS - so leaving it to the framework would quietly change
        the error shape of every creation path that goes through
        ``full_clean()``, including ``UserManager._create_user``.

        Checking it here restores the old shape, and costs nothing extra:
        ``full_clean()`` adds every field it has already collected an error for
        to the exclusion set before it runs ``validate_constraints()``, so
        ``uq_user_email_per_tenant`` skips itself and the clash is reported
        once, not twice.
        """
        super().validate_unique(exclude=exclude)
        if exclude and ('email' in exclude or 'tenant' in exclude):
            return
        if not self.email or not self.tenant_id:
            return
        clash = User.objects.filter(tenant_id=self.tenant_id, email=self.email)
        if self.pk is not None:
            clash = clash.exclude(pk=self.pk)
        if clash.exists():
            raise ValidationError({
                'email': [ValidationError(
                    'A user with this email already exists.', code='unique',
                )],
            })

    def _guard_cross_tenant_email(self, update_fields=None):
        """Refuse a second tenant's copy of an address while sign-in is unscoped.

        ``uq_user_email_per_tenant`` makes ada@gmail.com legal at Bright Star
        AND at Greenfield. Sign-in can only tell those two accounts apart when
        the request names the tenant it is addressed to. While
        ``sign_in_scope.REQUIRE_TENANT_ON_SIGN_IN`` was False an unscoped
        sign-in fell back to ``filter(email__iexact=...).first()``, so Ada - who
        reuses her password - would have been signed in to whichever of her two
        schools came back first, silently, and a reset asked for at one would
        have rewritten her password at the other.

        Nothing but the order of two deployments stopped that pair from being
        created, and an ordering assumption that lives only in a plan document
        is not a safeguard. So while the switch is off the pair is refused;
        when it is on the refusal lifts and no code changes.

        THE SWITCH IS NOW ON, so this guard is standing down: one address may
        be an account at several tenants. It stays here because the switch is
        meant to be flippable, and because turning it back off must re-arm the
        refusal in the same instant.

        It lives here, called from both ``clean()`` and ``save()``, for the
        same reason ``_derive_tenant`` and ``_normalize_email`` are: a
        serializer covers one door, and this table is written through many -
        ``objects.create()``, ``create_user``, the school-admin provisioner,
        the bulk importer, the seeders and the management commands. The
        database cannot hold this rule itself, because it is conditional on a
        Python constant that is meant to be flipped.

        Only an address being INTRODUCED is checked. If a legal pair ever does
        exist - made while the switch was on - a later save of either account
        must still work, or a status change, or the ``last_login_at`` write at
        the end of every successful sign-in, would start failing outright.

        ``bulk_create``/``bulk_update`` go round ``save()`` and are not covered.
        Nothing in this codebase creates users that way (only the test that
        proves the case-folding in ``UserQuerySet``), and the queryset methods
        run against historical models during migrations, where reaching into
        ``services.sign_in_scope`` would be wrong.
        """
        from .services import sign_in_scope

        if sign_in_scope.tenant_is_required():
            return
        if not self.email or not self.tenant_id:
            return
        if update_fields is not None and 'email' not in update_fields:
            return
        if not self._state.adding and self.pk is not None:
            unchanged = not (
                User.objects.filter(pk=self.pk).exclude(email=self.email).exists()
            )
            if unchanged:
                return

        clash = User.objects.filter(email=self.email).exclude(tenant_id=self.tenant_id)
        if self.pk is not None:
            clash = clash.exclude(pk=self.pk)
        if clash.exists():
            raise ValidationError({'email': [CROSS_TENANT_EMAIL_REFUSAL]})

    def _derive_tenant(self):
        """Fill in the canonical home tenant when one wasn't supplied.

        A branch names its own tenant, so a branch-bound user inherits it.
        There is exactly one such derivation, and it reads a real relationship.

        Nothing else can be derived, and nothing should be. A user with no
        branch could belong to any tenant on the platform, and picking one
        would put a person inside a customer they have no business being in.
        There used to be a second rule here - a ``CX_STAFF`` account fell back
        to the Codex PLATFORM tenant - and it was the persona column standing
        in for the answer it was supposed to be derived FROM. With the column
        gone the circle is broken: a caller creating platform staff names the
        platform tenant, the same way a caller creating a school user names
        the school's.

        Note what is deliberately NOT written here. "No branch and no tenant"
        does not mean "platform staff" - that would be inferring an identity
        from an absence, and one mistyped tenant would silently mint a
        colleague inside CodeX. The instance is left with a null tenant and
        refused instead: ``full_clean()`` reports it as
        ``{'tenant': ['This field cannot be null.']}``, and ``save()`` raises
        rather than reaching the database, where it would surface as an
        IntegrityError naming a column instead of the mistake.
        """
        if self.tenant_id:
            return
        if self.branch_id:
            self.tenant_id = self.branch.tenant_id

    def _normalize_email(self):
        """Fold the address to the single form this table stores.

        Called from both ``full_clean()`` and ``save()``, for the same reason
        ``_derive_tenant`` is: an instance that has only been validated must
        already carry the value that will be persisted, or the two disagree.

        Doing it before ``super().full_clean()`` also makes the model's own
        uniqueness check case-insensitive for free. ``validate_unique()``
        compares with ``=``, so ``Ada@gmail.com`` used to slip past a stored
        ``ada@gmail.com`` and fail later as an IntegrityError; now both sides
        of that comparison are lowercase.
        """
        self.email = email_normalization.normalize_email(self.email)

    def full_clean(self, *args, **kwargs):
        # Derive the tenant BEFORE super().full_clean(): clean_fields() runs
        # first inside it and would otherwise collect a spurious
        # {'tenant': ['This field cannot be null.']}. Setting it in clean()
        # is too late - clean_fields() has already run by then.
        self._derive_tenant()
        self._normalize_email()
        super().full_clean(*args, **kwargs)

    def save(self, *args, **kwargs):
        self._derive_tenant()  # backstop for saves that skip full_clean()
        self._normalize_email()  # ditto - every write lands lowercase
        if not self.tenant_id:
            # See _derive_tenant: nothing to infer one from, and no safe guess.
            raise ValidationError({'tenant': [
                'A tenant is required. A user with no branch cannot have one '
                'derived, so it must be supplied.'
            ]})
        if self.branch_id and self.branch.tenant_id != self.tenant_id:
            raise ValidationError("User branch must belong to the user's tenant.")
        # Backstop for the many writers that never call full_clean().
        self._guard_cross_tenant_email(update_fields=kwargs.get('update_fields'))
        if self.uid is None:
            with transaction.atomic():
                # One allocation rule, matching the one uid constraint: the
                # next number within this account's own tenant. Platform staff
                # used to be counted separately, over every CX_STAFF row
                # regardless of tenant - which, since they all sit in the one
                # PLATFORM tenant, produced the same sequence this does.
                max_uid = (
                    User.objects.select_for_update()
                    .filter(tenant_id=self.tenant_id)
                    .aggregate(m=Max('uid'))['m']
                )
                self.uid = (max_uid or 9) + 1
                self._sync_is_active()
                update_fields = kwargs.get('update_fields')
                if update_fields is not None and 'status' in update_fields and 'is_active' not in update_fields:
                    kwargs['update_fields'] = list(update_fields) + ['is_active']
                super().save(*args, **kwargs)
                return

        self._sync_is_active()
        update_fields = kwargs.get('update_fields')
        if update_fields is not None and 'status' in update_fields and 'is_active' not in update_fields:
            kwargs['update_fields'] = list(update_fields) + ['is_active']
        super().save(*args, **kwargs)

    def _sync_is_active(self):
        """Keep Django's ``is_active`` flag derived from ``status``.

        ``is_active`` is NOT a third opinion about whether this account may be
        used. It is a cache of one bit of ``status``, maintained here so that
        the Django and SimpleJWT machinery this project does not own - notably
        ``JWTAuthentication.get_user``, which rejects an inactive user, and the
        ``user__is_active=True`` filters in ``Position.occupancy`` - agree with
        the status column instead of drifting from it. ``status`` is the source
        of truth; every authorisation decision in this codebase reads it, or
        reads ``may_sign_in`` / ``may_hold_password`` above it.

        Written as a single derivation rather than a list of statuses that mean
        False. The old form listed five and so silently left DRAFT alone: a
        draft that had ever been ``is_active=True`` stayed that way, and
        ``confirm_reset`` set exactly that flag, which is how a parked draft
        ended up with a working password AND a session the API accepted.

        LOCKED remains the one deliberate exception. A lockout is temporary and
        clearing it must restore the account as it was, so the flag is left
        where the last real status put it and the refusal is made by
        ``may_sign_in`` instead. Every other status now answers here.
        """
        if self.status == self.Status.LOCKED:
            return
        self.is_active = self.status == self.Status.ACTIVE

    # ── Properties ────────────────────────────────────────────────────────────

    @property
    def may_sign_in(self) -> bool:
        """Whether a sign-in may succeed for this account right now.

        The one place that question is answered. ``LoginService._check_status``
        and ``vs_rbac.permissions.IsAuthenticatedAndActive`` both read it, so a
        session cannot be issued to an account the request gate would refuse,
        nor the reverse.
        """
        return self.status in self.SIGN_IN_STATUSES

    @property
    def may_hold_password(self) -> bool:
        """Whether a usable password may be set on this account.

        Read by every route that can put one there: the admin reset, the
        self-service reset request, the reset confirmation and the logged-in
        change. Deliberately wider than :attr:`may_sign_in` - see
        PASSWORD_STATUSES for why the two are separate sets and not one.
        """
        return self.status in self.PASSWORD_STATUSES

    @property
    def full_name(self) -> str:
        return f'{self.first_name} {self.last_name}'.strip()

    @property
    def is_locked(self) -> bool:
        return self.status == self.Status.LOCKED

    @property
    def is_suspended(self) -> bool:
        return self.status == self.Status.SUSPENDED

    @property
    def is_platform_user(self) -> bool:
        """True when this account belongs to a PLATFORM-kind tenant.

        The replacement for ``user_type == CX_STAFF``, and the same question -
        asked of the tenant the account is actually in rather than of a column
        that merely agreed with it. Nothing kept those two in step, so they
        could have disagreed at any time without anything noticing.

        Says nothing about what the person may do: that is their role, and only
        their role. This answers "which side of the platform boundary is this
        account on", which is a different question and the only one it answers.
        """
        return getattr(self.tenant, 'kind', None) == PLATFORM_TENANT_KIND

    def mark_password_change(self):
        self.password_changed_at = timezone.now()

    def __str__(self):
        return f'{self.email} ({self.status})'

# =============================================================================
# UserInvitation
# =============================================================================

class UserInvitation(TimeStampedModel):
    """
    One record per user. Tracks whether the invitation link is still
    valid (unused and within the configured invitation window).

    OneToOneField enforces one active invitation per user at all times.
    On resend, the existing record is reset rather than a new one created.
    """

    class EmailStatus(models.TextChoices):
        PENDING = 'PENDING', 'Pending'
        SENT    = 'SENT',    'Sent'
        FAILED  = 'FAILED',  'Failed'

    # OneToOne - a user can only have one invitation record at a time.
    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='invitation',
    )

    # The admin who created or last resent this invitation.
    invited_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        related_name='sent_invitations',
    )

    # Configured number of days from creation or the most recent resend.
    expires_at = models.DateTimeField()

    # Flipped to True on successful activation. Once True the activation link is dead.
    is_used = models.BooleanField(default=False)

    # ── Email delivery tracking ───────────────────────────────────────────────
    email_status    = models.CharField(
        max_length=10,
        choices=EmailStatus.choices,
        default=EmailStatus.PENDING,
    )
    email_sent_at   = models.DateTimeField(null=True, blank=True)
    email_last_error = models.TextField(blank=True, default='')
    email_attempts  = models.PositiveSmallIntegerField(default=0)

    # ── State helpers ─────────────────────────────────────────────────────────

    @property
    def is_expired(self) -> bool:
        return timezone.now() > self.expires_at

    @property
    def is_valid(self) -> bool:
        """True only if the link has not been used and has not expired."""
        return not self.is_used and not self.is_expired

    # ── Actions ───────────────────────────────────────────────────────────────

    def consume(self):
        """
        Mark as used after successful activation.
        After this, visiting vision.codexng.com/invite/{user.id}/
        will show an error - account is already active.
        """
        self.is_used = True
        self.save(update_fields=['is_used'])

    def reset(self, invited_by=None):
        """
        Reset for a resend. Gives the user a fresh configured window from now.
        Updates invited_by if a new admin triggered the resend.
        """
        from vs_config.runtime_settings import get_security_value

        self.is_used         = False
        self.expires_at      = timezone.now() + timedelta(
            days=get_security_value(
                "invitation_expiry_days", tenant=self.user.tenant, branch=self.user.branch,
            )
        )
        self.email_status    = self.EmailStatus.PENDING
        self.email_sent_at   = None
        self.email_last_error = ''
        self.email_attempts  = 0
        if invited_by:
            self.invited_by = invited_by
        self.save(update_fields=[
            'is_used', 'expires_at', 'invited_by_id',
            'email_status', 'email_sent_at', 'email_last_error', 'email_attempts',
        ])

    def __str__(self) -> str:
        return f'Invitation<user={self.user_id} used={self.is_used}>'

# =============================================================================
# LoginSession
# =============================================================================

class LoginSession(TimeStampedModel):
    """
    One record per login. Tracks the refresh token JTI so sessions can
    be linked to SimpleJWT's blacklist for forced logout operations.
    """

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='sessions',
    )

    # Copied from user context at login time for fast filtering
    # without joining back to the User table on every query.
    tenant = models.ForeignKey(
        "vs_tenants.Tenant", on_delete=models.PROTECT,
        related_name='login_sessions', null=True, blank=True,
    )

    ip_address   = models.GenericIPAddressField(null=True, blank=True)
    user_agent   = models.TextField(blank=True, default='')
    device_label = models.CharField(max_length=128, blank=True, default='')
    last_seen_at = models.DateTimeField(default=timezone.now)

    # JTI of the refresh token - links this session to the SimpleJWT blacklist.
    refresh_jti = models.CharField(max_length=64, blank=True, default='', db_index=True)

    is_active  = models.BooleanField(default=True)
    ended_at   = models.DateTimeField(null=True, blank=True)
    end_reason = models.CharField(
        max_length=64, blank=True, default='',
        help_text='LOGOUT / FORCE_LOGOUT / EXPIRED',
    )
    objects = TenantAwareManager()
    all_objects = models.Manager()

    class Meta:
        default_manager_name = "objects"
        base_manager_name = "all_objects"
        ordering = ['-last_seen_at']
        indexes = [
            models.Index(fields=['user', 'is_active']),
            models.Index(fields=['is_active', 'last_seen_at']),
        ]

    def end(self, reason: str = 'LOGOUT'):
        self.is_active  = False
        self.ended_at   = timezone.now()
        self.end_reason = reason

    def __str__(self) -> str:
        state = 'active' if self.is_active else 'ended'
        return f'Session<{self.user_id}:{state}>'

# =============================================================================
# AuthAttempt / AccountLockout
# =============================================================================

class AuthAttempt(TimeStampedModel):

    class Result(models.TextChoices):
        SUCCESS = 'SUCCESS', 'Success'
        FAIL    = 'FAIL',    'Fail'
        BLOCKED = 'BLOCKED', 'Blocked (policy)'

    email_entered = models.EmailField(max_length=254)

    # Null if the email was not found - do not reveal its existence.
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL,
        null=True, blank=True, related_name='auth_attempts',
    )
    tenant = models.ForeignKey(
        "vs_tenants.Tenant", on_delete=models.SET_NULL,
        null=True, blank=True, related_name='auth_attempts',
    )

    ip_address   = models.GenericIPAddressField(null=True, blank=True)
    user_agent   = models.TextField(blank=True, default='')
    result       = models.CharField(max_length=16, choices=Result.choices)
    failure_code = models.CharField(
        max_length=64, blank=True, default='',
        help_text='e.g. INVALID_CREDENTIALS / SCHOOL_MISMATCH / LOCKED',
    )
    metadata = models.JSONField(default=dict, blank=True)

    def __str__(self) -> str:
        return f'AuthAttempt<{self.email_entered}:{self.result}>'

    objects = TenantAwareManager()
    all_objects = models.Manager()

    class Meta:
        default_manager_name = "objects"
        base_manager_name = "all_objects"
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['email_entered']),
            models.Index(fields=['created_at']),
            models.Index(fields=['user', 'result']),
        ]


# ─────────────────────────────────────────────────────────────────────────────
# AccountLockout
# One row per user - keeps lockout state separate from the User model
# so the User model stays lean and lockout queries stay simple.
# ─────────────────────────────────────────────────────────────────────────────

class AccountLockout(TimeStampedModel):

    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='lockout',
    )
    locked_until    = models.DateTimeField(null=True, blank=True)
    locked_reason   = models.CharField(max_length=128, blank=True, default='')
    failure_count   = models.PositiveIntegerField(default=0)
    last_failure_at = models.DateTimeField(null=True, blank=True)
    last_failure_ip = models.GenericIPAddressField(null=True, blank=True)

    def is_locked_now(self) -> bool:
        return self.locked_until is not None and timezone.now() < self.locked_until

    def register_failure(self, ip=None, lock_threshold: int = 5, lock_minutes: int = 15):
        now = timezone.now()
        self.failure_count  += 1
        self.last_failure_at = now
        if ip:
            self.last_failure_ip = ip
        if self.failure_count >= lock_threshold:
            self.locked_until  = now + timezone.timedelta(minutes=lock_minutes)
            self.locked_reason = 'BRUTE_FORCE_THRESHOLD'

    def clear(self):
        self.failure_count = 0
        self.locked_until  = None
        self.locked_reason = ''

    def __str__(self) -> str:
        state = 'locked' if self.is_locked_now() else 'ok'
        return f'Lockout<{self.user_id}:{state}>'


# =============================================================================
# PasswordResetRequest
# =============================================================================

class PasswordResetRequest(TimeStampedModel):
    """
    Tracks password reset tokens. Only the SHA-256 hash is stored -
    never the raw token. This limits exposure if the database is compromised.

    requested_by distinguishes self-service (1 hr expiry) from
    admin-triggered resets (24 hr expiry).
    """

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='password_resets',
    )

    expires_at = models.DateTimeField()
    used_at    = models.DateTimeField(null=True, blank=True)

    # Tracks origin for expiry logic and audit context.
    # SELF = self-service (1 hour), ADMIN = admin-triggered (24 hours).
    requested_by = models.CharField(
        max_length=10,
        choices=[('SELF', 'Self-Service'), ('ADMIN', 'Admin-Initiated')],
        default='SELF',
    )

    requested_ip         = models.GenericIPAddressField(null=True, blank=True)
    requested_user_agent = models.TextField(blank=True, default='')

    # ── State helpers ─────────────────────────────────────────────────────────

    def is_expired(self) -> bool:
        return timezone.now() >= self.expires_at

    @property
    def is_valid(self) -> bool:
        return self.used_at is None and not self.is_expired()

    def mark_used(self):
        self.used_at = timezone.now()

    def __str__(self) -> str:
        state = 'used' if self.used_at else 'active'
        return f'PasswordResetRequest<{self.user_id}:{state}>'

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=['user'],
                condition=Q(used_at__isnull=True),
                name='one_active_reset_per_user',
            ),
        ]

# =============================================================================
# AuthEventLog
# =============================================================================

class AuthEventLog(TimeStampedModel):

    class Event(models.TextChoices):
        USER_CREATED             = 'USER_CREATED',             'User Created'
        INVITATION_SENT          = 'INVITATION_SENT',          'Invitation Sent'
        ACCOUNT_ACTIVATED        = 'ACCOUNT_ACTIVATED',        'Account Activated'
        LOGIN_SUCCESS            = 'LOGIN_SUCCESS',            'Login Success'
        LOGIN_FAILURE            = 'LOGIN_FAILURE',            'Login Failure'
        TOKEN_REVOKED            = 'TOKEN_REVOKED',            'Token Revoked'
        FORCE_LOGOUT             = 'FORCE_LOGOUT',             'Force Logout'
        ACCOUNT_LOCKED           = 'ACCOUNT_LOCKED',           'Account Locked'
        ACCOUNT_UNLOCKED         = 'ACCOUNT_UNLOCKED',         'Account Unlocked'
        ACCOUNT_SUSPENDED        = 'ACCOUNT_SUSPENDED',        'Account Suspended'
        ACCOUNT_REACTIVATED      = 'ACCOUNT_REACTIVATED',      'Account Reactivated'
        ACCOUNT_DEACTIVATED      = 'ACCOUNT_DEACTIVATED',      'Account Deactivated'
        PASSWORD_RESET_REQUESTED = 'PASSWORD_RESET_REQUESTED', 'Password Reset Requested'
        PASSWORD_RESET_COMPLETED = 'PASSWORD_RESET_COMPLETED', 'Password Reset Completed'
        PASSWORD_CHANGED         = 'PASSWORD_CHANGED',         'Password Changed'
        EMAIL_CHANGED            = 'EMAIL_CHANGED',            'Email Changed'

    # Who performed the action - could be the user themselves or an admin.
    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL,
        null=True, blank=True, related_name='auth_events_as_actor',
    )

    # Who the action was performed on.
    subject = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL,
        null=True, blank=True, related_name='auth_events_as_subject',
    )

    tenant = models.ForeignKey(
        "vs_tenants.Tenant", on_delete=models.SET_NULL,
        null=True, blank=True, related_name='auth_events',
    )

    event      = models.CharField(max_length=40, choices=Event.choices)
    ip_address = models.GenericIPAddressField(null=True, blank=True)
    user_agent = models.TextField(blank=True, default='')
    metadata   = models.JSONField(default=dict, blank=True)

    objects = TenantAwareManager()
    all_objects = models.Manager()

    class Meta:
        default_manager_name = "objects"
        base_manager_name = "all_objects"
        ordering = ['-created_at']

    def __str__(self) -> str:
        return f'AuthEvent<{self.event}>'


# =============================================================================
# PlatformStaffProfile
# =============================================================================

class PlatformStaffProfile(TimeStampedModel):
    """
    Extended personal / HR profile for platform staff - the users whose
    tenant is PLATFORM-kind.
    One row per platform staff member. Kept separate from User so the auth
    model stays lean - same pattern as AccountLockout / LoginSession.

    CX-only by design. School-side staff profiles will live in the future
    `staff` app and are intentionally out of scope here.
    """

    class MaritalStatus(models.TextChoices):
        SINGLE   = 'SINGLE',   'Single'
        MARRIED  = 'MARRIED',  'Married'
        DIVORCED = 'DIVORCED', 'Divorced'
        WIDOWED  = 'WIDOWED',  'Widowed'

    class EmploymentType(models.TextChoices):
        FULL_TIME = 'FULL_TIME', 'Full-time'
        PART_TIME = 'PART_TIME', 'Part-time'
        CONTRACT  = 'CONTRACT',  'Contract'
        INTERN    = 'INTERN',    'Intern'

    class EmploymentStatus(models.TextChoices):
        ACTIVE    = 'ACTIVE',    'Active'
        ON_LEAVE  = 'ON_LEAVE',  'On Leave'
        SUSPENDED = 'SUSPENDED', 'Suspended'
        EXITED    = 'EXITED',    'Exited'

    # ── Link ──────────────────────────────────────────────────────────────────
    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='platform_staff_profile',
    )

    # ── Personal ──────────────────────────────────────────────────────────────
    date_of_birth   = models.DateField(null=True, blank=True)
    marital_status  = models.CharField(
        max_length=16, choices=MaritalStatus.choices, blank=True, default='',
    )
    nationality     = models.CharField(max_length=80, blank=True, default='')
    state_of_origin = models.CharField(max_length=80, blank=True, default='')
    profile_photo   = models.ImageField(
        upload_to='platform_staff/photos/', null=True, blank=True,
    )
    bio             = models.TextField(blank=True, default='')

    # ── Contact (personal - work email/phone live on User) ────────────────────
    personal_email      = models.EmailField(max_length=254, blank=True, default='')
    alternate_phone     = models.CharField(max_length=32, blank=True, default='')
    residential_address = models.TextField(blank=True, default='')
    city                = models.CharField(max_length=80, blank=True, default='')
    state               = models.CharField(max_length=80, blank=True, default='')

    # ── Next of kin ───────────────────────────────────────────────────────────
    nok_name         = models.CharField(max_length=200, blank=True, default='')
    nok_relationship = models.CharField(max_length=80,  blank=True, default='')
    nok_phone        = models.CharField(max_length=32,  blank=True, default='')
    nok_address      = models.TextField(blank=True, default='')

    # ── Employment ────────────────────────────────────────────────────────────
    # Human-facing staff number, distinct from User.uid.
    employee_id       = models.CharField(max_length=32, null=True, blank=True, unique=True)
    # Mirrors Position.title because a primary seat is the source of truth.
    job_title         = models.CharField(max_length=150, blank=True, default='')
    position          = models.ForeignKey(
        'Position',
        on_delete=models.SET_NULL, null=True, blank=True,
        related_name='staff_profiles',
    )
    employment_type   = models.CharField(
        max_length=16, choices=EmploymentType.choices, blank=True, default='',
    )
    employment_status = models.CharField(
        max_length=16, choices=EmploymentStatus.choices,
        default=EmploymentStatus.ACTIVE,
    )
    date_joined       = models.DateField(null=True, blank=True)
    date_exited       = models.DateField(null=True, blank=True)

    # ── Payroll (sensitive - gated behind FLS at the serializer layer) ────────
    bank_name      = models.CharField(max_length=120, blank=True, default='')
    account_name   = models.CharField(max_length=200, blank=True, default='')
    account_number = models.CharField(max_length=20,  blank=True, default='')

    class Meta:
        db_table = 'vs_users_platform_staff_profile'
        verbose_name = 'Platform Staff Profile'
        indexes = [
            models.Index(fields=['position', 'employment_status']),
            models.Index(fields=['employee_id']),
        ]

    def clean(self):
        super().clean()
        # Platform staff only. The fact lives on the User's TENANT, one join
        # away, so it is enforced here rather than via a DB CheckConstraint -
        # the same reason the branch rule needed a trigger.
        if self.user_id and not self.user.is_platform_user:
            raise ValidationError('PlatformStaffProfile can only be attached to platform staff.')

    @property
    def is_active_employee(self) -> bool:
        return self.employment_status == self.EmploymentStatus.ACTIVE

    @property
    def org_node(self):
        """The exact org node the person sits in (could be a Team). Or None."""
        return self.position.org_node if self.position_id else None

    @property
    def department(self):
        """
        The DEPARTMENT-tier node the person belongs to, derived by walking up
        from their seat's org node (a Team resolves to its parent Department).
        Returns an OrgNode of kind DEPARTMENT, or None if they sit directly on a
        Division (no department tier).
        """
        node = self.org_node
        return node.nearest_of_kind(OrgNode.Kind.DEPARTMENT) if node else None

    @property
    def division(self):
        """
        The DIVISION-tier node the person belongs to, derived by walking up the
        org tree from their seat (Team → Department → Division). Returns an
        OrgNode of kind DIVISION, or None if there is no division ancestor.
        """
        node = self.org_node
        return node.nearest_of_kind(OrgNode.Kind.DIVISION) if node else None

    @property
    def primary_assignment(self):
        """The user's current primary PositionAssignment (carries dates/acting), or None."""
        return (
            self.user.position_assignments
            .filter(is_primary=True, end_date__isnull=True)
            .select_related('position', 'position__org_node')
            .first()
        )

    @property
    def current_line_manager(self):
        """
        Derives the user's line manager from their cached primary position's
        reports_to seat. Returns a User (the holder of the manager position),
        or None if there is no position, no parent seat, or it is vacant.
        """
        if not self.position_id or self.position.reports_to_id is None:
            return None
        return self.position.reports_to.current_holder

    def __str__(self) -> str:
        return f'PlatformStaffProfile<{self.user_id}:{self.job_title or "staff"}>'


# =============================================================================
# Organogram - OrgNode / Position / PositionAssignment / MatrixReport
# =============================================================================
#
# Position-based organisational chart for CX (platform) staff only.
#
#   OrgNode             - hierarchical tree of org units (self-referential),
#                         tiered as DIVISION → DEPARTMENT → TEAM
#   Position            - a seat in the org (title), belonging to an org node,
#                         reporting to another position (solid line)
#   PositionAssignment  - effective-dated link of a user to a position
#                         (FULL HISTORY: end_date IS NULL == current)
#   MatrixReport        - dotted-line / secondary reporting between positions
#
# CX-only by design: no school / branch fields. School org charts will live in
# the future `staff` app.

class OrgNode(TimeStampedModel):
    """
    A node in the CX org tree, tiered DIVISION → DEPARTMENT → TEAM.

    The hierarchy is strictly enforced in clean(): a Division is top-level, a
    Department must sit under a Division, and a Team must sit under a Department.
    """

    class Kind(models.TextChoices):
        DIVISION   = 'DIVISION',   'Division'
        DEPARTMENT = 'DEPARTMENT', 'Department'
        TEAM       = 'TEAM',       'Team'

    # Which parent tier each kind must sit under (None == must be top-level).
    _REQUIRED_PARENT_KIND = {
        Kind.DIVISION:   None,
        Kind.DEPARTMENT: Kind.DIVISION,
        Kind.TEAM:       Kind.DEPARTMENT,
    }

    _KIND_PREFIX = {
        Kind.DIVISION:   'DV-',
        Kind.DEPARTMENT: 'DT-',
        Kind.TEAM:       'TM-',
    }

    name        = models.CharField(max_length=150)
    code        = models.CharField(max_length=40, unique=True)
    kind        = models.CharField(
        max_length=16, choices=Kind.choices, default=Kind.DEPARTMENT,
    )
    # PROTECT, not SET_NULL: orphaning a child would produce a parentless
    # Department/Team, a state clean() below forbids outright - such rows are
    # then neither editable (the serializer re-runs clean()) nor deletable, and
    # drop out of every department/division roll-up. Delete or re-parent the
    # subtree first; the API answers 409 with the blocking counts.
    parent      = models.ForeignKey(
        'self',
        on_delete=models.PROTECT, null=True, blank=True,
        related_name='children',
    )
    # The position whose holder heads this node (e.g. "Head of Engineering").
    # Nullable so a node can exist before its head seat is defined.
    head_position = models.ForeignKey(
        'Position',
        on_delete=models.SET_NULL, null=True, blank=True,
        related_name='heads_node',
    )
    description = models.TextField(blank=True, default='')
    is_active   = models.BooleanField(default=True)

    class Meta:
        db_table = 'vs_users_org_node'
        verbose_name = 'Org Node'
        ordering = ['name']
        indexes = [
            models.Index(fields=['parent', 'is_active']),
            models.Index(fields=['kind']),
            models.Index(fields=['code']),
        ]
        constraints = [
            # No two sibling nodes may share the same name under the same parent.
            models.UniqueConstraint(
                fields=['name', 'parent'],
                condition=models.Q(parent__isnull=False),
                name='unique_org_node_name_per_parent',
            ),
            # No two top-level nodes (Divisions) may share the same name.
            models.UniqueConstraint(
                fields=['name'],
                condition=models.Q(parent__isnull=True),
                name='unique_org_node_name_top_level',
            ),
        ]

    def clean(self):
        super().clean()
        if self.parent_id and self.parent_id == self.pk:
            raise ValidationError('An org node cannot be its own parent.')
        # Guard against cycles in the parent chain.
        ancestor = self.parent
        while ancestor is not None:
            if ancestor.pk == self.pk:
                raise ValidationError('Org node parent chain cannot contain a cycle.')
            ancestor = ancestor.parent

        # Uniqueness: name must be unique within the same parent tier.
        qs = OrgNode.objects.filter(name=self.name, parent=self.parent)
        if self.pk:
            qs = qs.exclude(pk=self.pk)
        if qs.exists():
            scope = f'under "{self.parent.name}"' if self.parent_id else 'at the top level'
            raise ValidationError(
                {'name': f'An org node named "{self.name}" already exists {scope}.'}
            )

        # Enforce the DIVISION → DEPARTMENT → TEAM tiering.
        required_parent = self._REQUIRED_PARENT_KIND.get(self.kind)
        if required_parent is None:
            if self.parent_id is not None:
                raise ValidationError(
                    {'parent': f'A {self.get_kind_display()} is top-level and cannot have a parent.'}
                )
        else:
            if self.parent_id is None:
                raise ValidationError(
                    {'parent': f'A {self.get_kind_display()} must sit under a {OrgNode.Kind(required_parent).label}.'}
                )
            if self.parent.kind != required_parent:
                raise ValidationError(
                    {'parent': f'A {self.get_kind_display()} must sit under a '
                               f'{OrgNode.Kind(required_parent).label}, not a {self.parent.get_kind_display()}.'}
                )

    @property
    def head(self):
        """The User currently heading this node, or None."""
        if self.head_position_id is None:
            return None
        return self.head_position.current_holder

    def ancestors(self):
        """Yields nodes from immediate parent up to the root."""
        node = self.parent
        while node is not None:
            yield node
            node = node.parent

    def nearest_of_kind(self, kind):
        """
        Returns self (or the nearest ancestor) whose kind matches `kind`,
        walking up the tree. None if no node of that kind exists in the chain.
        """
        node = self
        while node is not None:
            if node.kind == kind:
                return node
            node = node.parent
        return None

    def save(self, *args, **kwargs):
        prefix = self._KIND_PREFIX.get(self.kind, '')
        # Strip any existing tier prefix so re-saves and kind changes stay clean.
        bare = self.code
        for p in self._KIND_PREFIX.values():
            if bare.upper().startswith(p):
                bare = bare[len(p):]
                break
        self.code = f'{prefix}{bare}'
        super().save(*args, **kwargs)

    def __str__(self) -> str:
        return f'OrgNode<{self.kind}:{self.code}>'


class Position(TimeStampedModel):
    """
    A seat in the organogram (e.g. "Backend Engineer"). People are assigned to
    positions via PositionAssignment; the solid reporting line is position→position
    through `reports_to`.
    """

    title        = models.CharField(max_length=150)
    code         = models.CharField(max_length=40, unique=True)
    # The org node this seat belongs to - may be a Division, Department, or Team.
    org_node     = models.ForeignKey(
        OrgNode,
        on_delete=models.PROTECT,
        related_name='positions',
    )
    # Solid-line manager seat. Null for the top of the tree (e.g. CEO).
    reports_to   = models.ForeignKey(
        'self',
        on_delete=models.SET_NULL, null=True, blank=True,
        related_name='direct_reports',
    )
    # Optional default RBAC role granted when someone is assigned this seat.
    default_role = models.ForeignKey(
        'vs_rbac.TenantRoleTemplate',
        on_delete=models.SET_NULL, null=True, blank=True,
        related_name='default_for_positions',
    )
    # Number of people the seat may hold simultaneously (1 == single-incumbent).
    headcount    = models.PositiveSmallIntegerField(default=1)
    is_active    = models.BooleanField(default=True)

    class Meta:
        db_table = 'vs_users_position'
        verbose_name = 'Position'
        ordering = ['title']
        indexes = [
            models.Index(fields=['org_node', 'is_active'], name='pos_orgnode_active_idx'),
            models.Index(fields=['reports_to']),
            models.Index(fields=['code']),
        ]

    def clean(self):
        super().clean()
        if self.reports_to_id and self.reports_to_id == self.pk:
            raise ValidationError('A position cannot report to itself.')
        # Guard against cycles in the reporting chain.
        node = self.reports_to
        while node is not None:
            if node.pk == self.pk:
                raise ValidationError('Position reporting chain cannot contain a cycle.')
            node = node.reports_to

    # NOTE: "current holder" means an OPEN assignment held by an ACTIVE user.
    # A seat reserved for a hire who is still pending approval/activation
    # (user.is_active == False) is intentionally NOT counted as occupied, so
    # such a hire never shows up as a holder and never blocks headcount until
    # they actually activate.
    @property
    def current_assignments(self):
        """Open assignments to this seat held by active users."""
        return (
            self.assignments
            .filter(end_date__isnull=True, user__is_active=True)
            .select_related('user')
        )

    @property
    def current_holder(self):
        """
        The single primary current holder of this seat, or None.
        For multi-incumbent seats this returns the primary holder.
        """
        assignment = (
            self.assignments
            .filter(end_date__isnull=True, is_primary=True, user__is_active=True)
            .select_related('user')
            .first()
        )
        if assignment is None:
            assignment = (
                self.assignments
                .filter(end_date__isnull=True, user__is_active=True)
                .select_related('user')
                .first()
            )
        return assignment.user if assignment else None

    @property
    def current_holders(self):
        """All active Users currently holding this seat (multi-incumbent aware)."""
        return [a.user for a in self.current_assignments]

    @property
    def is_vacant(self) -> bool:
        return not self.assignments.filter(
            end_date__isnull=True, user__is_active=True,
        ).exists()

    @property
    def open_seats(self) -> int:
        filled = self.assignments.filter(
            end_date__isnull=True, user__is_active=True,
        ).count()
        return max(self.headcount - filled, 0)

    def __str__(self) -> str:
        return f'Position<{self.code}>'


class PositionAssignment(TimeStampedModel):
    """
    Effective-dated assignment of a user to a position. FULL HISTORY:
    a row with end_date IS NULL is the user's current tenure in that seat;
    closing a tenure is setting end_date. Past rows are retained.

    "One current primary assignment per user" cannot be a conditional unique
    constraint on MariaDB, so it is enforced in OrganogramService / clean().
    """

    user       = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='position_assignments',
    )
    position   = models.ForeignKey(
        Position,
        on_delete=models.PROTECT,
        related_name='assignments',
    )
    # The primary seat drives department + line-manager derivation. A user may
    # hold secondary (non-primary) seats simultaneously.
    is_primary = models.BooleanField(default=True)
    # Acting / interim cover (e.g. covering a vacant manager seat).
    is_acting  = models.BooleanField(default=False)
    start_date = models.DateField(default=timezone.localdate)
    end_date   = models.DateField(null=True, blank=True)

    class Meta:
        db_table = 'vs_users_position_assignment'
        verbose_name = 'Position Assignment'
        ordering = ['-start_date']
        indexes = [
            models.Index(fields=['user', 'end_date']),
            models.Index(fields=['position', 'end_date']),
            models.Index(fields=['is_primary', 'end_date']),
        ]

    @property
    def is_current(self) -> bool:
        return self.end_date is None

    def clean(self):
        super().clean()
        if self.user_id and not self.user.is_platform_user:
            raise ValidationError('Only platform staff can be assigned to a position.')
        if self.end_date and self.end_date < self.start_date:
            raise ValidationError('end_date cannot be before start_date.')
        # One current primary assignment per user (MariaDB-safe: enforced here).
        if self.is_primary and self.end_date is None and self.user_id:
            clash = (
                PositionAssignment.objects
                .filter(user_id=self.user_id, is_primary=True, end_date__isnull=True)
                .exclude(pk=self.pk)
            )
            if clash.exists():
                raise ValidationError(
                    'This user already has a current primary position. '
                    'Close it before assigning a new primary position.'
                )

    def __str__(self) -> str:
        state = 'current' if self.is_current else f'ended {self.end_date}'
        return f'PositionAssignment<{self.user_id}@{self.position_id}:{state}>'


class MatrixReport(TimeStampedModel):
    """
    Dotted-line (matrix / secondary) reporting between two positions. Distinct
    from the solid line carried by Position.reports_to. Used for cross-functional
    or project reporting that the approval engine can optionally honour.
    """

    position    = models.ForeignKey(
        Position,
        on_delete=models.CASCADE,
        related_name='matrix_reports',
    )
    reports_to  = models.ForeignKey(
        Position,
        on_delete=models.CASCADE,
        related_name='matrix_directs',
    )
    relationship_label = models.CharField(max_length=120, blank=True, default='')

    class Meta:
        db_table = 'vs_users_matrix_report'
        verbose_name = 'Matrix Report'
        unique_together = [('position', 'reports_to')]
        indexes = [
            models.Index(fields=['position']),
            models.Index(fields=['reports_to']),
        ]

    def clean(self):
        super().clean()
        if self.position_id and self.position_id == self.reports_to_id:
            raise ValidationError('A position cannot have a matrix line to itself.')

    def __str__(self) -> str:
        return f'MatrixReport<{self.position_id}⇢{self.reports_to_id}>'

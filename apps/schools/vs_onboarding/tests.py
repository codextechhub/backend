"""M9 School Onboarding: the control room, the checklist and the go-live gate.

The tests are ordered the way the acceptance criteria are: security and
tenancy first, because those are the ones that must not be deferred, then
behaviour, then shape and scale, then the two shapes of school this platform
has to serve.

Two habits are deliberate throughout.

**Audit is asserted by the row existing**, never by "no exception was raised".
``emit_audit_event`` never raises, and an action type it does not recognise is
swallowed in silence, so a test that only proves nothing blew up would pass
against a module whose entire audit trail goes nowhere. That is not a
hypothetical: it is what happened to every school the bulk importer created.

**Every tenancy test uses two tenants.** A single-tenant test proves nothing
about tenancy; the row that must not appear has to exist for its absence to
mean anything.
"""
from __future__ import annotations

import itertools
from io import StringIO
from unittest.mock import patch

from django.core.management import call_command
from django.db import IntegrityError, connection, transaction
from django.test.utils import CaptureQueriesContext
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone
from rest_framework.test import APIClient

from schools.vs_schools.models import School, SchoolStatus
from vs_audit.models import AuditActionType, AuditEvent, AuditModuleKey
from vs_rbac.models import Permission
from vs_rbac.tests.helpers import (
    codex_tenant,
    make_assignment,
    make_branch,
    make_permission,
    make_platform_assignment,
    make_platform_role,
    make_platform_role_permission,
    make_role,
    make_role_permission,
    make_school,
    make_school_admin,
    make_vision_user,
)
from vs_tenants.models import Branch, BranchStatus, Tenant
from vs_user.tokens import CodeXRefreshToken

from .constants import (
    GoLiveStatus,
    PERM_GO_LIVE_APPROVE,
    PERM_GO_LIVE_REJECT,
    PERM_GO_LIVE_SUBMIT,
    PERM_GO_LIVE_VIEW,
    PERM_PROGRESS_CREATE,
    PERM_PROGRESS_REACTIVATE,
    PERM_PROGRESS_VIEW,
    PERM_TASK_UPDATE,
    ReadinessState,
    TASK_CATALOG,
    TaskKey,
    TaskStatus,
)
from .models import GoLiveRequest, OnboardingProgress, OnboardingTask
from .services.provisioning import provision_onboarding
from .services.readiness import ReadinessService

_counter = itertools.count(1)


SCHOOL_KEYS = (
    PERM_PROGRESS_VIEW, PERM_TASK_UPDATE, PERM_GO_LIVE_SUBMIT, PERM_GO_LIVE_VIEW,
)
PLATFORM_KEYS = (
    PERM_GO_LIVE_APPROVE, PERM_GO_LIVE_REJECT, PERM_PROGRESS_CREATE,
    PERM_PROGRESS_REACTIVATE,
)
ALL_KEYS = tuple(dict.fromkeys(SCHOOL_KEYS + PLATFORM_KEYS))


def grant_school_admin(tenant, user, *keys):
    """Give ``user`` the tenant's ``school_admin`` role, carrying ``keys``.

    The role key matters: FIRST_ADMIN and ROLE_BASELINE both look for a
    whole-tenant role keyed ``school_admin`` with at least one granted
    permission, which is what school creation provisions from the prebuilt
    template.
    """
    role = (
        make_role(tenant, name="School Admin", key="school_admin")
        if not _school_admin_role(tenant)
        else _school_admin_role(tenant)
    )
    for key in keys:
        make_role_permission(role, make_permission(key))
    make_assignment(tenant, user, role)
    return role


def _school_admin_role(tenant):
    from vs_rbac.models import TenantRoleTemplate

    return TenantRoleTemplate.objects.filter(tenant=tenant, key="school_admin").first()


def grant_extra(tenant, user, *keys):
    """A second, ordinary role carrying exactly ``keys`` and nothing else."""
    role = make_role(tenant, name=f"Onboarding Extra {next(_counter)}")
    for key in keys:
        make_role_permission(role, make_permission(key))
    make_assignment(tenant, user, role)
    return role


def grant_platform(user, *keys):
    role = make_platform_role(name=f"Onboarding Reviewer {next(_counter)}")
    for key in keys:
        make_platform_role_permission(role, make_permission(key))
    make_platform_assignment(user, role)
    return role


class OnboardingFixture(TestCase):
    """Two schools that are both onboarding, and the people around them.

    The school under test is PENDING, which is the state every school is in
    while it uses this module. If any endpoint here forgot to declare itself
    part of the pending-tenant surface, every test in this file fails with
    TENANT_NOT_LIVE, which is the intended alarm.
    """

    def setUp(self):
        self.school = make_school(
            slug="alpha-school", name="Alpha School", status="PENDING",
        )
        self.tenant = self.school.tenant
        self.admin = make_school_admin(
            None, email="alpha-admin@test.com", tenant=self.tenant,
        )
        grant_school_admin(self.tenant, self.admin, *SCHOOL_KEYS)

        # A second school, onboarding at the same time. Its rows exist so that
        # their absence from Alpha's answers means something.
        self.rival = make_school(
            slug="beta-school", name="Beta School", status="PENDING",
        )
        self.rival_tenant = self.rival.tenant
        self.rival_admin = make_school_admin(
            None, email="beta-admin@test.com", tenant=self.rival_tenant,
        )
        grant_school_admin(self.rival_tenant, self.rival_admin, *SCHOOL_KEYS)

        # Platform reviewer: explicit grants, not the super-admin bypass, so a
        # 403 in these tests means the permission gate actually ran.
        self.reviewer = make_vision_user(email="reviewer@codex.test")
        grant_platform(self.reviewer, *PLATFORM_KEYS)

        self.progress = provision_onboarding(self.tenant, actor=self.admin)
        self.rival_progress = provision_onboarding(
            self.rival_tenant, actor=self.rival_admin,
        )

    # ── plumbing ──────────────────────────────────────────────────────────

    def client_for(self, user):
        token = str(CodeXRefreshToken.for_user(user).access_token)
        client = APIClient()
        client.credentials(HTTP_AUTHORIZATION=f"Bearer {token}")
        return client

    def url(self, name, *args):
        return reverse(name, args=args)

    def scoped(self, name, *args, tenant=None):
        tenant = tenant or self.tenant
        return f"{self.url(name, *args)}?tenant={tenant.slug}"

    def tasks_for(self, tenant=None):
        tenant = tenant or self.tenant
        return OnboardingTask.all_objects.filter(tenant=tenant).order_by("order_index")

    def task(self, key, tenant=None):
        return self.tasks_for(tenant).get(key=key)

    def set_status(self, tenant, key, status):
        OnboardingTask.all_objects.filter(tenant=tenant, key=key).update(status=status)

    def complete_all_but(self, tenant, spare_key):
        """Mark every required task DONE except ``spare_key``.

        Written straight to the rows on purpose: these are set-up steps, not
        the transition under test, and routing them through the service would
        make every go-live test also a test of six conditions it does not care
        about.
        """
        OnboardingTask.all_objects.filter(tenant=tenant).exclude(
            key=spare_key,
        ).update(status=TaskStatus.DONE, completed_at=timezone.now())

    def make_ready(self, tenant=None):
        tenant = tenant or self.tenant
        OnboardingTask.all_objects.filter(tenant=tenant).update(
            status=TaskStatus.DONE, completed_at=timezone.now(),
        )
        return ReadinessService.evaluate(tenant)

    def submit_request(self, tenant=None, actor=None):
        tenant = tenant or self.tenant
        return GoLiveRequest.all_objects.create(
            tenant=tenant,
            requested_by=actor or self.admin,
            preferred_go_live_at=timezone.now() + timezone.timedelta(days=1),
            acknowledged=True,
            status=GoLiveStatus.PENDING,
        )

    def audit_rows(self, action_type=None, tenant=None):
        rows = AuditEvent.objects.filter(module_key=AuditModuleKey.ONBOARDING)
        if action_type is not None:
            rows = rows.filter(action_type=action_type)
        if tenant is not None:
            rows = rows.filter(tenant=tenant)
        return rows

    def assertErrorCode(self, response, status_code, code):
        self.assertEqual(response.status_code, status_code, response.data)
        self.assertEqual(response.data["error"]["code"], code, response.data)


# ══════════════════════════════════════════════════════════════════════════
# 10.1  Security and tenancy
# ══════════════════════════════════════════════════════════════════════════

class EndpointPermissionTests(OnboardingFixture):
    """Every endpoint answers 403 to a caller who does not hold its key."""

    def setUp(self):
        super().setUp()
        # Authenticated, in the right tenant, holding nothing at all.
        self.nobody = make_school_admin(
            None, email="nobody@test.com", tenant=self.tenant,
        )
        # The registry rows have to exist for the gate to be evaluating real
        # keys rather than keys nobody could hold in any case.
        for key in ALL_KEYS:
            make_permission(key)
        self.request_row = self.submit_request()

    def _calls(self):
        return [
            ("get", self.scoped("onboarding-state")),
            ("post", self.scoped("onboarding-provision")),
            ("post", self.scoped("onboarding-revalidate")),
            ("get", self.scoped("onboarding-task-list")),
            ("patch", self.scoped("onboarding-task-detail", TaskKey.FIRST_ADMIN)),
            ("post", self.scoped("onboarding-go-live-request")),
            ("get", self.scoped("onboarding-go-live-list")),
            ("post", self.scoped("onboarding-go-live-approve", self.request_row.pk)),
            ("post", self.scoped("onboarding-go-live-reject", self.request_row.pk)),
            ("post", self.scoped("onboarding-reinstate", self.tenant.slug)),
        ]

    def test_every_endpoint_refuses_a_caller_without_its_key(self):
        client = self.client_for(self.nobody)
        for method, url in self._calls():
            with self.subTest(url=url):
                response = getattr(client, method)(url, {}, format="json")
                self.assertEqual(response.status_code, 403, f"{url}: {response.data}")
                # And specifically not the pending-tenant refusal, which would
                # mean the surface declaration is missing rather than the key.
                self.assertNotEqual(
                    response.data.get("error", {}).get("code"), "TENANT_NOT_LIVE",
                )

    def test_a_school_admin_is_refused_approve_and_reject_for_their_own_request(self):
        """The two keys that open the platform belong to platform roles only."""
        client = self.client_for(self.admin)

        approve = client.post(
            self.scoped("onboarding-go-live-approve", self.request_row.pk),
            {}, format="json",
        )
        reject = client.post(
            self.scoped("onboarding-go-live-reject", self.request_row.pk),
            {"rejection_reason": "no"}, format="json",
        )

        self.assertEqual(approve.status_code, 403, approve.data)
        self.assertEqual(reject.status_code, 403, reject.data)
        self.request_row.refresh_from_db()
        self.assertEqual(self.request_row.status, GoLiveStatus.PENDING)

    def test_a_request_with_no_tenant_parameter_is_refused(self):
        response = self.client_for(self.admin).get(self.url("onboarding-state"))

        self.assertEqual(response.status_code, 400, response.data)


class SelfApprovalAttackTests(OnboardingFixture):
    """A school must not be able to take itself live, however it gets the key.

    The defence used to be a sentence: ``onboarding.go_live.approve`` is
    "granted to platform roles by default", so no school holds it. A default is
    not a boundary. Both keys live in the ``onboarding`` module, so migration
    0007 classified them TENANT, and the guard from ad41a03 - which refuses a
    PLATFORM-scoped key granted inside a tenant - has nothing to say about
    them. A school admin who can mint a role can mint one carrying either.

    The gate is therefore the caller's asserted tenant, exactly as on
    :class:`OnboardingReinstateView`: only the platform tenant may decide.
    """

    def setUp(self):
        super().setUp()
        self.approve_key = make_permission(PERM_GO_LIVE_APPROVE)
        self.reject_key = make_permission(PERM_GO_LIVE_REJECT)
        self.make_ready()
        self.request_row = self.submit_request()
        OnboardingProgress.all_objects.filter(tenant=self.tenant).update(
            readiness_state=ReadinessState.PENDING_APPROVAL,
        )

    def _self_grant(self, *keys):
        """The mint-a-role-and-assign-it path, as a school admin would run it.

        Written against the models rather than the RBAC endpoints on purpose:
        those endpoints are closed to a PENDING tenant by
        ``TenantSurfaceAllowed``, so driving them here would prove only that
        the pending gate works. What has to be proved is that *holding* the key
        confers nothing, because the same admin can mint this role the moment
        their school is live and the grant outlives the moment.
        """
        return grant_extra(self.tenant, self.admin, *keys)

    def test_the_grant_itself_is_not_refused(self):
        """The premise: nothing stops a school from carrying these keys.

        Both are ``PermissionScope.TENANT`` - the seeder says so - so the
        scope guard never runs. If this ever starts failing, the boundary has
        moved to the registry and the view gate below is belt and braces.
        """
        from vs_rbac.models import PermissionScope

        self.assertEqual(self.approve_key.scope, PermissionScope.TENANT)
        self.assertEqual(self.reject_key.scope, PermissionScope.TENANT)
        role = self._self_grant(PERM_GO_LIVE_APPROVE, PERM_GO_LIVE_REJECT)
        self.assertTrue(role.pk)

    def test_a_school_admin_holding_the_key_cannot_approve_its_own_school(self):
        """The attack, end to end. Before the tenant gate this returned 200."""
        self._self_grant(PERM_GO_LIVE_APPROVE)

        with self.captureOnCommitCallbacks(execute=True):
            response = self.client_for(self.admin).post(
                self.scoped("onboarding-go-live-approve", self.request_row.pk),
                {}, format="json",
            )

        self.assertEqual(response.status_code, 403, response.data)
        self.request_row.refresh_from_db()
        self.assertEqual(self.request_row.status, GoLiveStatus.PENDING)
        self.school.refresh_from_db()
        self.assertEqual(self.school.status, SchoolStatus.PENDING)
        self.assertEqual(
            Tenant.objects.get(pk=self.tenant.pk).status, Tenant.Status.PENDING,
        )

    def test_a_school_admin_holding_the_key_cannot_reject_either(self):
        """Reject is not harmless: it is how a school buries an inconvenient review."""
        self._self_grant(PERM_GO_LIVE_REJECT)

        response = self.client_for(self.admin).post(
            self.scoped("onboarding-go-live-reject", self.request_row.pk),
            {"rejection_reason": "Reviewing myself."}, format="json",
        )

        self.assertEqual(response.status_code, 403, response.data)
        self.request_row.refresh_from_db()
        self.assertEqual(self.request_row.status, GoLiveStatus.PENDING)

    def test_the_refusal_does_not_say_whether_the_key_was_held(self):
        """Two callers, one holding the key and one not, get the same answer.

        A refusal that differed would be a probe: mint the role, call the
        endpoint, and read from the error whether the grant landed.
        """
        self._self_grant(PERM_GO_LIVE_APPROVE)
        holder = self.client_for(self.admin)
        pauper = self.client_for(
            make_school_admin(None, email="pauper@test.com", tenant=self.tenant),
        )
        url = self.scoped("onboarding-go-live-approve", self.request_row.pk)

        held = holder.post(url, {}, format="json")
        unheld = pauper.post(url, {}, format="json")

        self.assertEqual(held.status_code, unheld.status_code)
        # The whole envelope, not just the code: the wording is the part that
        # would otherwise say "you hold it, but not here".
        self.assertEqual(held.data["error"], unheld.data["error"], held.data)

    def test_the_platform_reviewer_still_approves_normally(self):
        """The gate must not cost the people it exists to serve."""
        with self.captureOnCommitCallbacks(execute=True):
            response = self.client_for(self.reviewer).post(
                self.scoped("onboarding-go-live-approve", self.request_row.pk),
                {}, format="json",
            )

        self.assertEqual(response.status_code, 200, response.data)
        self.request_row.refresh_from_db()
        self.assertEqual(self.request_row.status, GoLiveStatus.ACTIVATED)
        self.school.refresh_from_db()
        self.assertEqual(self.school.status, SchoolStatus.ACTIVE)

    def test_the_platform_reviewer_still_rejects_normally(self):
        response = self.client_for(self.reviewer).post(
            self.scoped("onboarding-go-live-reject", self.request_row.pk),
            {"rejection_reason": "Come back with a set of books."}, format="json",
        )

        self.assertEqual(response.status_code, 200, response.data)
        self.request_row.refresh_from_db()
        self.assertEqual(self.request_row.status, GoLiveStatus.REJECTED)


class CrossTenantIsolationTests(OnboardingFixture):
    """404, never 403: a tenant identifier must not be enumerable."""

    def test_asserting_another_tenants_slug_is_404(self):
        client = self.client_for(self.admin)

        for method, name, args in (
            ("get", "onboarding-state", ()),
            ("get", "onboarding-task-list", ()),
            ("get", "onboarding-go-live-list", ()),
            ("post", "onboarding-revalidate", ()),
        ):
            with self.subTest(name=name):
                response = getattr(client, method)(
                    self.scoped(name, *args, tenant=self.rival_tenant),
                    {}, format="json",
                )
                self.assertEqual(response.status_code, 404, response.data)

    def test_a_task_key_that_exists_in_both_tenants_cannot_be_crossed(self):
        """The key is identical in both schools, so only the scoping separates them."""
        before = self.task(TaskKey.FIRST_ADMIN, self.rival_tenant).status

        response = self.client_for(self.admin).patch(
            self.scoped(
                "onboarding-task-detail", TaskKey.FIRST_ADMIN,
                tenant=self.rival_tenant,
            ),
            {"status": TaskStatus.SKIPPED}, format="json",
        )

        self.assertEqual(response.status_code, 404, response.data)
        self.assertEqual(self.task(TaskKey.FIRST_ADMIN, self.rival_tenant).status, before)

    def test_another_tenants_go_live_request_is_404_not_403(self):
        rival_request = self.submit_request(
            tenant=self.rival_tenant, actor=self.rival_admin,
        )

        response = self.client_for(self.reviewer).post(
            f"{self.url('onboarding-go-live-approve', rival_request.pk)}"
            f"?tenant={self.tenant.slug}",
            {}, format="json",
        )

        self.assertEqual(response.status_code, 404, response.data)
        rival_request.refresh_from_db()
        self.assertEqual(rival_request.status, GoLiveStatus.PENDING)

    def test_no_row_of_the_other_tenant_appears_in_any_list(self):
        client = self.client_for(self.admin)
        self.submit_request(tenant=self.rival_tenant, actor=self.rival_admin)
        mine = self.submit_request()

        tasks = client.get(self.scoped("onboarding-task-list"))
        requests = client.get(self.scoped("onboarding-go-live-list"))

        self.assertEqual(
            len(tasks.data["data"]), self.tasks_for().count(),
        )
        ids = {row["id"] for row in requests.data["data"]}
        self.assertEqual(ids, {mine.pk})


class PlatformReviewerReachTests(OnboardingFixture):
    """A Codex reviewer may assert a school's slug on two views and no others."""

    def setUp(self):
        super().setUp()
        self.make_ready()
        self.request_row = self.submit_request()
        OnboardingProgress.all_objects.filter(tenant=self.tenant).update(
            readiness_state=ReadinessState.PENDING_APPROVAL,
        )

    def test_the_reviewer_may_reject_across_tenants(self):
        response = self.client_for(self.reviewer).post(
            self.scoped("onboarding-go-live-reject", self.request_row.pk),
            {"rejection_reason": "Come back with a main branch."}, format="json",
        )

        self.assertEqual(response.status_code, 200, response.data)
        self.request_row.refresh_from_db()
        self.assertEqual(self.request_row.status, GoLiveStatus.REJECTED)

    def test_the_reviewer_may_approve_across_tenants(self):
        with self.captureOnCommitCallbacks(execute=True):
            response = self.client_for(self.reviewer).post(
                self.scoped("onboarding-go-live-approve", self.request_row.pk),
                {}, format="json",
            )

        self.assertEqual(response.status_code, 200, response.data)

    def test_the_reviewer_cannot_assert_the_slug_anywhere_else(self):
        """Cross-tenant reach is per view, and only approve and reject opt in."""
        client = self.client_for(self.reviewer)

        for name in (
            "onboarding-state", "onboarding-task-list", "onboarding-go-live-list",
        ):
            with self.subTest(name=name):
                response = client.get(self.scoped(name))
                self.assertEqual(response.status_code, 404, response.data)

        provision = client.post(self.scoped("onboarding-provision"), {}, format="json")
        self.assertEqual(provision.status_code, 404, provision.data)


class PayloadExposureTests(OnboardingFixture):
    """What the control room payload must not carry."""

    def test_the_state_payload_carries_only_the_named_fields(self):
        response = self.client_for(self.admin).get(self.scoped("onboarding-state"))

        data = response.data["data"]
        self.assertEqual(
            set(data),
            {
                "readiness_state", "go_live_at", "last_validation_at", "tasks",
                "counts", "go_live_blocked", "blocking_tasks", "expiry",
            },
        )
        # Dates and counts only: the expiry block must not become a place a
        # related object drags a name or an address into this payload.
        self.assertEqual(
            set(data["expiry"]),
            {
                "applies", "pending_since", "expires_at", "days_remaining",
                "warning_sent", "warning_sent_at", "expiry_days", "warning_days",
            },
        )
        self.assertEqual(
            set(data["tasks"][0]),
            {"key", "title", "is_required", "status", "completed_at"},
        )
        body = str(response.data)
        self.assertNotIn("@test.com", body)
        self.assertNotIn("metadata", body)
        self.assertNotIn("onboarding.progress.view", body)

    def test_the_go_live_payload_exposes_names_and_never_accounts(self):
        self.submit_request()

        response = self.client_for(self.admin).get(self.scoped("onboarding-go-live-list"))

        row = response.data["data"][0]
        self.assertEqual(
            set(row),
            {
                "id", "status", "preferred_go_live_at", "note", "acknowledged",
                "requested_by_name", "reviewed_by_name", "reviewed_at",
                "rejection_reason", "failure_reference", "created_at",
            },
        )
        self.assertNotIn("@test.com", str(row))


# ══════════════════════════════════════════════════════════════════════════
# 10.2  Behaviour - FR-001 provisioning
# ══════════════════════════════════════════════════════════════════════════

class ProvisioningTests(OnboardingFixture):
    def test_it_creates_one_progress_row_and_one_task_per_applicable_entry(self):
        # Every catalog entry, with nothing left out: no step is conditional on
        # the shape of the school any more.
        expected = {entry.key for entry in TASK_CATALOG}

        self.assertEqual(
            OnboardingProgress.all_objects.filter(tenant=self.tenant).count(), 1,
        )
        self.assertEqual(
            {task.key for task in self.tasks_for()}, expected,
        )
        self.assertEqual(self.progress.readiness_state, ReadinessState.NOT_READY)

    def test_required_flags_and_order_come_from_the_catalog(self):
        by_key = {entry.key: entry for entry in TASK_CATALOG}
        for task in self.tasks_for():
            with self.subTest(key=task.key):
                self.assertEqual(task.is_required, by_key[task.key].is_required)
                self.assertEqual(task.order_index, by_key[task.key].order_index)
                self.assertEqual(task.title, by_key[task.key].title)

    def test_provisioning_twice_adds_nothing_and_resets_no_status(self):
        self.set_status(self.tenant, TaskKey.ACADEMIC_STRUCTURE, TaskStatus.DONE)
        before = self.tasks_for().count()

        provision_onboarding(self.tenant, actor=self.admin)

        self.assertEqual(self.tasks_for().count(), before)
        self.assertEqual(
            OnboardingProgress.all_objects.filter(tenant=self.tenant).count(), 1,
        )
        self.assertEqual(
            self.task(TaskKey.ACADEMIC_STRUCTURE).status, TaskStatus.DONE,
        )

    def test_a_new_catalog_entry_is_added_and_nothing_else_is_touched(self):
        from .constants import CatalogEntry

        self.set_status(self.tenant, TaskKey.FIRST_ADMIN, TaskStatus.IN_PROGRESS)
        OnboardingTask.all_objects.filter(
            tenant=self.tenant, key=TaskKey.STAFF_INVITATIONS,
        ).delete()
        before = set(self.tasks_for().values_list("key", flat=True))

        extended = TASK_CATALOG + (
            CatalogEntry(
                key=TaskKey.STAFF_INVITATIONS, title="Invite your staff",
                is_required=False, order_index=7,
            ),
        )
        with patch("schools.vs_onboarding.services.provisioning.TASK_CATALOG", extended):
            provision_onboarding(self.tenant, actor=self.admin)

        after = set(self.tasks_for().values_list("key", flat=True))
        self.assertEqual(after - before, {TaskKey.STAFF_INVITATIONS.value})
        self.assertEqual(
            self.task(TaskKey.FIRST_ADMIN).status, TaskStatus.IN_PROGRESS,
        )

    def test_an_entry_that_does_not_apply_to_this_school_is_never_created(self):
        """``applies_to`` still works, and is still the only route to a row.

        No shipped entry uses it since the branch step was retired, so it is
        pinned with a synthetic one: an unused seam that nothing exercises is
        one that quietly stops working before the next conditional step needs
        it.
        """
        from .constants import CatalogEntry

        OnboardingTask.all_objects.filter(
            tenant=self.tenant, key=TaskKey.STAFF_INVITATIONS,
        ).delete()
        seen = []

        def _never(tenant, school):
            seen.append((tenant.pk, school.pk))
            return False

        extended = tuple(
            entry for entry in TASK_CATALOG
            if entry.key != TaskKey.STAFF_INVITATIONS
        ) + (
            CatalogEntry(
                key=TaskKey.STAFF_INVITATIONS, title="Invite your staff",
                is_required=False, order_index=7, applies_to=_never,
            ),
        )
        with patch("schools.vs_onboarding.services.provisioning.TASK_CATALOG", extended):
            provision_onboarding(self.tenant, actor=self.admin)

        # The predicate was consulted, with this school, and its answer was
        # obeyed: not present-and-optional, absent.
        self.assertEqual(seen, [(self.tenant.pk, self.school.pk)])
        self.assertFalse(
            OnboardingTask.all_objects.filter(
                tenant=self.tenant, key=TaskKey.STAFF_INVITATIONS,
            ).exists(),
        )

    def test_provisioning_writes_one_audit_row(self):
        AuditEvent.objects.all().delete()

        with self.captureOnCommitCallbacks(execute=True):
            provision_onboarding(self.tenant, actor=self.admin)

        rows = self.audit_rows(
            AuditActionType.ONBOARDING_PROVISIONED, tenant=self.tenant,
        )
        self.assertEqual(rows.count(), 1)
        self.assertEqual(rows.first().entity_type, "OnboardingProgress")

    def test_a_tenant_that_is_not_a_school_is_refused(self):
        from .exceptions import OnboardingTenantNotSchool

        with self.assertRaises(OnboardingTenantNotSchool) as raised:
            provision_onboarding(codex_tenant())

        self.assertEqual(raised.exception.error_code, "ONBOARDING_TENANT_NOT_SCHOOL")
        self.assertEqual(raised.exception.http_status, 422)

    def test_the_provision_endpoint_tops_up_and_answers_state(self):
        ops = make_school_admin(None, email="ops@test.com", tenant=self.tenant)
        grant_extra(self.tenant, ops, PERM_PROGRESS_CREATE)
        OnboardingTask.all_objects.filter(
            tenant=self.tenant, key=TaskKey.INITIAL_DATA,
        ).delete()

        with self.captureOnCommitCallbacks(execute=True):
            response = self.client_for(ops).post(
                self.scoped("onboarding-provision"), {}, format="json",
            )

        self.assertEqual(response.status_code, 200, response.data)
        self.assertIn(
            TaskKey.INITIAL_DATA.value,
            [row["key"] for row in response.data["data"]["tasks"]],
        )


class SchoolCreationProvisionsOnboardingTests(TestCase):
    """A school has its control room from the moment it exists.

    Provisioning is best effort for the same reason the books are: a school,
    its tenant, its first administrator and its branches are worth more than
    its checklist, and the checklist can be rebuilt at any time. The savepoint
    is what makes "best effort" true, and the failure test below is the only
    thing that proves it: a database failure inside the creation transaction
    poisons it, so catching alone would still lose the school.
    """

    @classmethod
    def setUpTestData(cls):
        cls.vision_user = make_vision_user(
            email="creation-onboarding@example.com", super_admin=True,
        )
        call_command("seed_actions", stdout=StringIO())
        call_command("seed_prebuilt_role_templates", stdout=StringIO())

    @staticmethod
    def _branch(name, email, *, is_main=True):
        return {
            "name": name, "_type": "Main" if is_main else "Annex", "state": "Lagos",
            "is_main": is_main,
            "primary_admin_data": {"full_name": f"{name} Head", "email": email},
        }

    def _create(self, payload, *, expect=201):
        # Every school is created with at least one branch. These tests are
        # about the onboarding control room, so the main branch is scenery, but
        # the payload is refused without it.
        payload = dict(payload)
        payload.setdefault("branches", [
            self._branch("Main Campus", f"main@{payload['slug']}.test"),
        ])
        client = APIClient()
        client.force_authenticate(user=self.vision_user)
        response = client.post(reverse("school-create"), payload, format="json")
        self.assertEqual(response.status_code, expect, response.data)
        return response

    def test_creating_a_school_provisions_its_control_room(self):
        self._create({"name": "Fresh School", "slug": "fresh-school"})

        school = School.objects.get(slug="fresh-school")
        self.assertTrue(
            OnboardingProgress.all_objects.filter(tenant=school.tenant).exists(),
        )
        keys = set(
            OnboardingTask.all_objects
            .filter(tenant=school.tenant)
            .values_list("key", flat=True)
        )
        self.assertEqual(keys, {entry.key.value for entry in TASK_CATALOG})

    def test_a_second_branch_changes_nothing_about_the_checklist(self):
        """The checklist no longer varies with how many sites a school runs.

        It used to: a school that submitted a second branch was handed a branch
        step that a single-site school never saw. Both schools now get the same
        list, which is what makes the list something the control room can
        explain.
        """
        self._create({"name": "One Site", "slug": "one-site-school"})
        self._create({
            "name": "Branchy School", "slug": "branchy-school",
            "branches": [
                self._branch("Main Campus", "main@branchy.test"),
                self._branch("Lekki Annex", "lekki@branchy.test", is_main=False),
            ],
        })

        def keys_for(slug):
            tenant = School.objects.get(slug=slug).tenant
            return set(
                OnboardingTask.all_objects
                .filter(tenant=tenant)
                .values_list("key", flat=True)
            )

        self.assertEqual(
            School.objects.get(slug="branchy-school").branches.count(), 2,
        )
        self.assertEqual(keys_for("one-site-school"), keys_for("branchy-school"))

    def test_an_onboarding_failure_does_not_cost_us_the_school(self):
        def _failing_statement(*args, **kwargs):
            # A *database* failure, not a Python one: only a failed statement
            # actually aborts the transaction, which is the state the savepoint
            # exists to escape. A bare raise would pass against a plain
            # try/except and prove nothing.
            with connection.cursor() as cursor:
                cursor.execute("SELECT * FROM a_table_that_does_not_exist")

        with patch(
            "schools.vs_onboarding.services.provisioning.provision_onboarding",
            side_effect=_failing_statement,
        ):
            self._create({"name": "Resilient", "slug": "resilient-onboarding"})

        school = School.objects.get(slug="resilient-onboarding")
        self.assertEqual(school.status, SchoolStatus.PENDING)
        self.assertFalse(
            OnboardingProgress.all_objects.filter(tenant=school.tenant).exists(),
        )

    def test_the_failure_is_logged_with_the_school_so_it_can_be_found(self):
        with patch(
            "schools.vs_onboarding.services.provisioning.provision_onboarding",
            side_effect=RuntimeError("the catalog is on fire"),
        ):
            with self.assertLogs("vs_onboarding", level="ERROR") as logs:
                self._create({"name": "Noisy", "slug": "noisy-onboarding"})

        self.assertTrue(any("noisy-onboarding" in line for line in logs.output))


# ══════════════════════════════════════════════════════════════════════════
# 10.2  Behaviour - FR-003 transitions
# ══════════════════════════════════════════════════════════════════════════

class TaskTransitionTests(OnboardingFixture):
    def patch_task(self, key, status, user=None, tenant=None):
        client = self.client_for(user or self.admin)
        return client.patch(
            self.scoped("onboarding-task-detail", key, tenant=tenant),
            {"status": status}, format="json",
        )

    def test_marking_a_task_done_stamps_completed_at(self):
        with self.captureOnCommitCallbacks(execute=True):
            response = self.patch_task(TaskKey.ACADEMIC_STRUCTURE, TaskStatus.DONE)

        self.assertEqual(response.status_code, 200, response.data)
        task = self.task(TaskKey.ACADEMIC_STRUCTURE)
        self.assertEqual(task.status, TaskStatus.DONE)
        self.assertIsNotNone(task.completed_at)

    def test_reopening_a_done_task_clears_completed_at(self):
        with self.captureOnCommitCallbacks(execute=True):
            self.patch_task(TaskKey.ACADEMIC_STRUCTURE, TaskStatus.DONE)
            response = self.patch_task(
                TaskKey.ACADEMIC_STRUCTURE, TaskStatus.IN_PROGRESS,
            )

        self.assertEqual(response.status_code, 200, response.data)
        task = self.task(TaskKey.ACADEMIC_STRUCTURE)
        self.assertEqual(task.status, TaskStatus.IN_PROGRESS)
        self.assertIsNone(task.completed_at)

    def test_a_disallowed_edge_is_refused(self):
        self.set_status(self.tenant, TaskKey.ACADEMIC_STRUCTURE, TaskStatus.DONE)

        response = self.patch_task(TaskKey.ACADEMIC_STRUCTURE, TaskStatus.SKIPPED)

        self.assertErrorCode(response, 422, "INVALID_TASK_TRANSITION")

    def test_asking_for_the_current_status_is_a_conflict(self):
        response = self.patch_task(TaskKey.FIRST_ADMIN, TaskStatus.NOT_STARTED)

        self.assertErrorCode(response, 409, "TASK_ALREADY_IN_STATE")

    def test_done_is_refused_when_the_platform_can_see_it_is_not_done(self):
        """SET_OF_BOOKS with no ledger entity: the school says done, we do not."""
        response = self.patch_task(TaskKey.SET_OF_BOOKS, TaskStatus.DONE)

        self.assertErrorCode(response, 422, "TASK_CONDITION_NOT_MET")
        self.assertEqual(self.task(TaskKey.SET_OF_BOOKS).status, TaskStatus.NOT_STARTED)

    def test_done_is_allowed_once_the_condition_holds(self):
        """The other side of the previous test: STAFF_INVITATIONS, satisfied.

        The condition wants somebody in this tenant beyond the first
        administrator, so one more user is the whole of the set-up.
        """
        refused = self.patch_task(TaskKey.STAFF_INVITATIONS, TaskStatus.DONE)
        self.assertErrorCode(refused, 422, "TASK_CONDITION_NOT_MET")

        make_school_admin(None, email="alpha-second@test.com", tenant=self.tenant)

        with self.captureOnCommitCallbacks(execute=True):
            response = self.patch_task(TaskKey.STAFF_INVITATIONS, TaskStatus.DONE)

        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(
            self.task(TaskKey.STAFF_INVITATIONS).status, TaskStatus.DONE,
        )

    def test_the_first_admin_condition_reads_a_real_active_assignment(self):
        with self.captureOnCommitCallbacks(execute=True):
            allowed = self.patch_task(TaskKey.FIRST_ADMIN, TaskStatus.DONE)
        self.assertEqual(allowed.status_code, 200, allowed.data)

        # The rival school's admin exists too, so this cannot be passing on a
        # query that forgot to name a tenant. Their permission to move the task
        # is moved onto a role of its own first, so revoking the school_admin
        # assignment removes the *condition* and not the caller's access: a 403
        # here would prove nothing about the condition.
        from vs_rbac.models import TenantUserRoleAssignment

        grant_extra(self.rival_tenant, self.rival_admin, PERM_TASK_UPDATE)
        TenantUserRoleAssignment.objects.filter(
            tenant=self.rival_tenant, role__key="school_admin",
        ).update(assignment_status="REVOKED")
        refused = self.client_for(self.rival_admin).patch(
            self.scoped(
                "onboarding-task-detail", TaskKey.FIRST_ADMIN,
                tenant=self.rival_tenant,
            ),
            {"status": TaskStatus.DONE}, format="json",
        )
        self.assertErrorCode(refused, 422, "TASK_CONDITION_NOT_MET")

    def test_an_unknown_task_key_is_a_plain_404(self):
        """No domain code: a key that is not a task is a missing row, and it
        must be indistinguishable from another tenant's task."""
        response = self.patch_task("NOT_A_TASK", TaskStatus.DONE)

        self.assertEqual(response.status_code, 404, response.data)

    def test_an_invalid_target_status_is_rejected_before_anything_is_read(self):
        response = self.patch_task(TaskKey.FIRST_ADMIN, "TELEPORTED")

        self.assertEqual(response.status_code, 400, response.data)
        self.assertEqual(self.task(TaskKey.FIRST_ADMIN).status, TaskStatus.NOT_STARTED)

    def test_a_key_this_tenant_does_not_have_is_a_plain_404(self):
        """A real catalog key, and a school with no row for it.

        The rival school keeps its row, so a 404 here cannot come from the key
        being unknown to the platform: it comes from this tenant not holding
        one, which must be indistinguishable from another tenant's task.
        """
        OnboardingTask.all_objects.filter(
            tenant=self.tenant, key=TaskKey.STAFF_INVITATIONS,
        ).delete()

        response = self.patch_task(TaskKey.STAFF_INVITATIONS, TaskStatus.DONE)

        self.assertEqual(response.status_code, 404, response.data)
        self.assertTrue(
            OnboardingTask.all_objects.filter(
                tenant=self.rival_tenant, key=TaskKey.STAFF_INVITATIONS,
            ).exists(),
        )

    def test_a_required_task_cannot_be_skipped(self):
        """The product decision that replaced "skippable but still blocking".

        It used to be allowed and simply kept go-live blocked. It is now
        refused outright, with a code of its own so the client can say why
        rather than blaming the edge.
        """
        self.complete_all_but(self.tenant, TaskKey.ACADEMIC_STRUCTURE)

        with self.captureOnCommitCallbacks(execute=True):
            response = self.patch_task(TaskKey.ACADEMIC_STRUCTURE, TaskStatus.SKIPPED)

        self.assertErrorCode(response, 422, "REQUIRED_TASK_NOT_SKIPPABLE")
        self.assertEqual(
            self.task(TaskKey.ACADEMIC_STRUCTURE).status, TaskStatus.NOT_STARTED,
        )
        state = self.client_for(self.admin).get(self.scoped("onboarding-state")).data
        self.assertTrue(state["data"]["go_live_blocked"])
        self.assertEqual(
            state["data"]["blocking_tasks"], [TaskKey.ACADEMIC_STRUCTURE.value],
        )

    def test_a_required_task_in_progress_cannot_be_skipped_either(self):
        """The refusal is about the task, so it holds from every legal edge."""
        self.set_status(self.tenant, TaskKey.FIRST_ADMIN, TaskStatus.IN_PROGRESS)

        response = self.patch_task(TaskKey.FIRST_ADMIN, TaskStatus.SKIPPED)

        self.assertErrorCode(response, 422, "REQUIRED_TASK_NOT_SKIPPABLE")
        self.assertEqual(self.task(TaskKey.FIRST_ADMIN).status, TaskStatus.IN_PROGRESS)

    def test_the_refusal_writes_no_audit_row_and_no_notification(self):
        """A refused transition changed nothing, so it announces nothing."""
        AuditEvent.objects.all().delete()

        with patch("schools.vs_onboarding.services.effects._send") as send:
            with self.captureOnCommitCallbacks(execute=True):
                self.patch_task(TaskKey.ACADEMIC_STRUCTURE, TaskStatus.SKIPPED)

        self.assertEqual(self.audit_rows(tenant=self.tenant).count(), 0)
        send.assert_not_called()

    def test_skipping_an_optional_task_does_not_block(self):
        self.complete_all_but(self.tenant, TaskKey.INITIAL_DATA)

        with self.captureOnCommitCallbacks(execute=True):
            self.patch_task(TaskKey.INITIAL_DATA, TaskStatus.SKIPPED)

        state = self.client_for(self.admin).get(self.scoped("onboarding-state")).data
        self.assertFalse(state["data"]["go_live_blocked"])
        self.assertEqual(state["data"]["blocking_tasks"], [])
        self.assertEqual(state["data"]["readiness_state"], ReadinessState.READY)

    def test_every_transition_is_refused_once_the_school_is_live(self):
        OnboardingProgress.all_objects.filter(tenant=self.tenant).update(
            readiness_state=ReadinessState.LIVE,
        )

        response = self.patch_task(TaskKey.ACADEMIC_STRUCTURE, TaskStatus.IN_PROGRESS)

        self.assertErrorCode(response, 409, "ONBOARDING_ALREADY_LIVE")


class TaskTransitionAuditTests(OnboardingFixture):
    """One audit row per transition, asserted by the row existing.

    Never by "no exception was raised": ``emit_audit_event`` swallows an
    unregistered action type in silence, so the only proof that the trail
    exists is reading it back.
    """

    def patch_task(self, key, status):
        return self.client_for(self.admin).patch(
            self.scoped("onboarding-task-detail", key),
            {"status": status}, format="json",
        )

    def _transition(self, key, status):
        AuditEvent.objects.all().delete()
        with self.captureOnCommitCallbacks(execute=True):
            response = self.patch_task(key, status)
        self.assertEqual(response.status_code, 200, response.data)
        return self.audit_rows(tenant=self.tenant)

    def test_completion_writes_exactly_one_completed_row(self):
        rows = self._transition(TaskKey.ACADEMIC_STRUCTURE, TaskStatus.DONE)

        self.assertEqual(rows.count(), 1)
        row = rows.first()
        self.assertEqual(row.action_type, AuditActionType.ONBOARDING_TASK_COMPLETED)
        self.assertEqual(row.entity_type, "OnboardingTask")
        self.assertEqual(
            row.entity_id, str(self.task(TaskKey.ACADEMIC_STRUCTURE).pk),
        )

    def test_skipping_writes_exactly_one_skipped_row(self):
        rows = self._transition(TaskKey.INITIAL_DATA, TaskStatus.SKIPPED)

        self.assertEqual(rows.count(), 1)
        self.assertEqual(
            rows.first().action_type, AuditActionType.ONBOARDING_TASK_SKIPPED,
        )

    def test_reopening_writes_exactly_one_reopened_row(self):
        self.set_status(self.tenant, TaskKey.ACADEMIC_STRUCTURE, TaskStatus.DONE)

        rows = self._transition(TaskKey.ACADEMIC_STRUCTURE, TaskStatus.IN_PROGRESS)

        self.assertEqual(rows.count(), 1)
        self.assertEqual(
            rows.first().action_type, AuditActionType.ONBOARDING_TASK_REOPENED,
        )

    def test_every_action_type_this_module_writes_is_registered(self):
        registered = set(AuditActionType.values)
        for action in (
            AuditActionType.ONBOARDING_PROVISIONED,
            AuditActionType.ONBOARDING_TASK_COMPLETED,
            AuditActionType.ONBOARDING_TASK_SKIPPED,
            AuditActionType.ONBOARDING_TASK_REOPENED,
            AuditActionType.GO_LIVE_REQUESTED,
            AuditActionType.GO_LIVE_APPROVED,
            AuditActionType.GO_LIVE_REJECTED,
            AuditActionType.GO_LIVE_ACTIVATED,
            AuditActionType.GO_LIVE_FAILED,
            AuditActionType.ONBOARDING_EXPIRED,
            AuditActionType.ONBOARDING_REINSTATED,
        ):
            self.assertIn(action, registered)


# ══════════════════════════════════════════════════════════════════════════
# 10.2  Behaviour - FR-004 readiness
# ══════════════════════════════════════════════════════════════════════════

class ReadinessTests(OnboardingFixture):
    def test_the_last_required_task_moves_the_school_to_ready(self):
        self.complete_all_but(self.tenant, TaskKey.ACADEMIC_STRUCTURE)

        with patch(
            "schools.vs_onboarding.services.effects._send",
        ) as send, self.captureOnCommitCallbacks(execute=True):
            self.client_for(self.admin).patch(
                self.scoped("onboarding-task-detail", TaskKey.ACADEMIC_STRUCTURE),
                {"status": TaskStatus.DONE}, format="json",
            )

        progress = OnboardingProgress.all_objects.get(tenant=self.tenant)
        self.assertEqual(progress.readiness_state, ReadinessState.READY)

        ready_calls = [c for c in send.call_args_list if c.args[0] == "onboarding.go_live_ready"]
        self.assertEqual(len(ready_calls), 1)
        context = ready_calls[0].args[1]
        self.assertEqual(
            set(context), {"school_name", "school_slug", "completed_by_name"},
        )

    def test_re_entering_ready_after_a_rejection_announces_again(self):
        self.make_ready()
        OnboardingProgress.all_objects.filter(tenant=self.tenant).update(
            readiness_state=ReadinessState.NOT_READY,
        )

        with patch("schools.vs_onboarding.services.effects._send") as send:
            with self.captureOnCommitCallbacks(execute=True):
                ReadinessService.evaluate(self.tenant)

        self.assertEqual(
            [c.args[0] for c in send.call_args_list], ["onboarding.go_live_ready"],
        )

    def test_a_live_school_is_never_downgraded(self):
        OnboardingProgress.all_objects.filter(tenant=self.tenant).update(
            readiness_state=ReadinessState.LIVE,
        )

        progress = ReadinessService.evaluate(self.tenant)

        self.assertEqual(progress.readiness_state, ReadinessState.LIVE)

    def test_a_school_awaiting_approval_is_never_downgraded(self):
        OnboardingProgress.all_objects.filter(tenant=self.tenant).update(
            readiness_state=ReadinessState.PENDING_APPROVAL,
        )

        progress = ReadinessService.evaluate(self.tenant)

        self.assertEqual(progress.readiness_state, ReadinessState.PENDING_APPROVAL)

    def test_revalidate_stamps_the_timestamp_even_when_nothing_changes(self):
        before = OnboardingProgress.all_objects.get(tenant=self.tenant)
        self.assertIsNone(before.last_validation_at)

        response = self.client_for(self.admin).post(
            self.scoped("onboarding-revalidate"), {}, format="json",
        )

        self.assertEqual(response.status_code, 200, response.data)
        after = OnboardingProgress.all_objects.get(tenant=self.tenant)
        self.assertIsNotNone(after.last_validation_at)
        self.assertEqual(after.readiness_state, ReadinessState.NOT_READY)

    def test_revalidate_on_an_unprovisioned_tenant_is_404(self):
        OnboardingTask.all_objects.filter(tenant=self.tenant).delete()
        OnboardingProgress.all_objects.filter(tenant=self.tenant).delete()

        response = self.client_for(self.admin).post(
            self.scoped("onboarding-revalidate"), {}, format="json",
        )

        self.assertErrorCode(response, 404, "ONBOARDING_NOT_PROVISIONED")


# ══════════════════════════════════════════════════════════════════════════
# 10.2  Behaviour - FR-007 / FR-008 / FR-009 go-live
# ══════════════════════════════════════════════════════════════════════════

class GoLiveSubmissionTests(OnboardingFixture):
    def submit(self, **body):
        payload = {
            "preferred_go_live_at": (
                timezone.now() + timezone.timedelta(days=3)
            ).isoformat(),
            "acknowledged": True,
        }
        payload.update(body)
        return self.client_for(self.admin).post(
            self.scoped("onboarding-go-live-request"), payload, format="json",
        )

    def test_a_request_before_ready_names_every_outstanding_task(self):
        self.complete_all_but(self.tenant, TaskKey.ACADEMIC_STRUCTURE)
        OnboardingTask.all_objects.filter(
            tenant=self.tenant, key=TaskKey.SET_OF_BOOKS,
        ).update(status=TaskStatus.NOT_STARTED)

        response = self.submit()

        self.assertErrorCode(response, 422, "ONBOARDING_NOT_READY")
        self.assertEqual(
            set(response.data["error"]["detail"]["outstanding_required_tasks"]),
            {TaskKey.SET_OF_BOOKS.value, TaskKey.ACADEMIC_STRUCTURE.value},
        )
        self.assertFalse(GoLiveRequest.all_objects.filter(tenant=self.tenant).exists())

    def test_an_unacknowledged_request_is_refused(self):
        self.make_ready()

        response = self.submit(acknowledged=False)

        self.assertErrorCode(response, 422, "ACKNOWLEDGEMENT_REQUIRED")

    def test_a_request_with_no_acknowledgement_field_is_refused_the_same_way(self):
        self.make_ready()

        response = self.client_for(self.admin).post(
            self.scoped("onboarding-go-live-request"),
            {"preferred_go_live_at": (
                timezone.now() + timezone.timedelta(days=3)
            ).isoformat()},
            format="json",
        )

        self.assertErrorCode(response, 422, "ACKNOWLEDGEMENT_REQUIRED")

    def test_a_ready_school_may_submit(self):
        self.make_ready()

        with self.captureOnCommitCallbacks(execute=True):
            response = self.submit(note="We are ready.")

        self.assertEqual(response.status_code, 201, response.data)
        row = GoLiveRequest.all_objects.get(tenant=self.tenant)
        self.assertEqual(row.status, GoLiveStatus.PENDING)
        self.assertTrue(row.acknowledged)
        self.assertEqual(row.requested_by_id, self.admin.pk)
        self.assertEqual(
            OnboardingProgress.all_objects.get(tenant=self.tenant).readiness_state,
            ReadinessState.PENDING_APPROVAL,
        )
        self.assertEqual(
            self.audit_rows(AuditActionType.GO_LIVE_REQUESTED, self.tenant).count(), 1,
        )

    def test_a_second_request_while_one_is_pending_is_a_conflict(self):
        self.make_ready()
        with self.captureOnCommitCallbacks(execute=True):
            self.submit()

        second = self.submit()

        self.assertErrorCode(second, 409, "GOLIVE_ALREADY_PENDING")
        self.assertEqual(
            GoLiveRequest.all_objects.filter(
                tenant=self.tenant, status=GoLiveStatus.PENDING,
            ).count(),
            1,
        )

    def test_the_partial_unique_constraint_is_what_actually_stops_the_race(self):
        """Straight at the database, past the friendly pre-check.

        The 409 above is the friendly path and it loses the race it exists to
        prevent; this is the guarantee underneath it.
        """
        self.submit_request()

        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                GoLiveRequest.all_objects.create(
                    tenant=self.tenant,
                    requested_by=self.admin,
                    preferred_go_live_at=timezone.now(),
                    acknowledged=True,
                    status=GoLiveStatus.PENDING,
                )

        self.assertEqual(
            GoLiveRequest.all_objects.filter(
                tenant=self.tenant, status=GoLiveStatus.PENDING,
            ).count(),
            1,
        )

    def test_a_rejected_request_does_not_block_the_next_one(self):
        """The constraint is partial on purpose: history is kept, not deduped."""
        GoLiveRequest.all_objects.create(
            tenant=self.tenant, requested_by=self.admin,
            preferred_go_live_at=timezone.now(), acknowledged=True,
            status=GoLiveStatus.REJECTED, rejection_reason="no",
        )
        self.make_ready()

        with self.captureOnCommitCallbacks(execute=True):
            response = self.submit()

        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(GoLiveRequest.all_objects.filter(tenant=self.tenant).count(), 2)


class GoLiveReviewTests(OnboardingFixture):
    def setUp(self):
        super().setUp()
        self.make_ready()
        self.request_row = self.submit_request()
        OnboardingProgress.all_objects.filter(tenant=self.tenant).update(
            readiness_state=ReadinessState.PENDING_APPROVAL,
        )

    def approve(self, pk=None):
        with self.captureOnCommitCallbacks(execute=True):
            return self.client_for(self.reviewer).post(
                self.scoped(
                    "onboarding-go-live-approve", pk or self.request_row.pk,
                ),
                {}, format="json",
            )

    def reject(self, reason="Not yet.", pk=None):
        with self.captureOnCommitCallbacks(execute=True):
            return self.client_for(self.reviewer).post(
                self.scoped("onboarding-go-live-reject", pk or self.request_row.pk),
                {"rejection_reason": reason}, format="json",
            )

    # ── reject ────────────────────────────────────────────────────────────

    def test_rejection_records_the_reviewer_and_returns_the_school_to_ready(self):
        response = self.reject("Come back with a main branch.")

        self.assertEqual(response.status_code, 200, response.data)
        row = GoLiveRequest.all_objects.get(pk=self.request_row.pk)
        self.assertEqual(row.status, GoLiveStatus.REJECTED)
        self.assertEqual(row.reviewed_by_id, self.reviewer.pk)
        self.assertIsNotNone(row.reviewed_at)
        self.assertEqual(row.rejection_reason, "Come back with a main branch.")
        self.assertEqual(
            OnboardingProgress.all_objects.get(tenant=self.tenant).readiness_state,
            ReadinessState.READY,
        )
        self.assertEqual(
            self.audit_rows(AuditActionType.GO_LIVE_REJECTED, self.tenant).count(), 1,
        )

    def test_rejection_without_a_reason_is_refused(self):
        response = self.reject("   ")

        self.assertErrorCode(response, 422, "REJECTION_REASON_REQUIRED")
        self.assertEqual(
            GoLiveRequest.all_objects.get(pk=self.request_row.pk).status,
            GoLiveStatus.PENDING,
        )

    def test_reviewing_a_request_that_is_not_pending_is_a_conflict(self):
        self.reject()

        again = self.reject()
        approve = self.approve()

        self.assertErrorCode(again, 409, "GOLIVE_NOT_PENDING")
        self.assertErrorCode(approve, 409, "GOLIVE_NOT_PENDING")

    # ── approve and activate ──────────────────────────────────────────────

    def test_approval_takes_the_school_live_everywhere_it_has_to(self):
        response = self.approve()

        self.assertEqual(response.status_code, 200, response.data)

        school = School.objects.get(pk=self.school.pk)
        self.assertEqual(school.status, SchoolStatus.ACTIVE)
        self.assertIsNotNone(school.activated_at)

        # The tenant is written by School.save()'s mirror, never separately.
        # Verified, not assumed.
        tenant = Tenant.objects.get(pk=self.tenant.pk)
        self.assertEqual(tenant.status, Tenant.Status.ACTIVE)
        self.assertIsNotNone(tenant.activated_at)

        progress = OnboardingProgress.all_objects.get(tenant=self.tenant)
        self.assertEqual(progress.readiness_state, ReadinessState.LIVE)
        self.assertIsNotNone(progress.go_live_at)

        row = GoLiveRequest.all_objects.get(pk=self.request_row.pk)
        self.assertEqual(row.status, GoLiveStatus.ACTIVATED)
        self.assertEqual(row.reviewed_by_id, self.reviewer.pk)
        self.assertIsNotNone(row.reviewed_at)

        self.assertEqual(
            self.audit_rows(AuditActionType.GO_LIVE_APPROVED, self.tenant).count(), 1,
        )
        self.assertEqual(
            self.audit_rows(AuditActionType.GO_LIVE_ACTIVATED, self.tenant).count(), 1,
        )

    def test_a_forced_failure_leaves_nothing_half_live(self):
        with patch(
            "schools.vs_onboarding.services.go_live._write_school_live",
            side_effect=RuntimeError("the mirror is on fire"),
        ):
            response = self.approve()

        self.assertErrorCode(response, 500, "GOLIVE_ACTIVATION_FAILED")
        reference = response.data["error"]["detail"]["failure_reference"]
        self.assertTrue(reference)

        school = School.objects.get(pk=self.school.pk)
        self.assertEqual(school.status, SchoolStatus.PENDING)
        self.assertEqual(
            Tenant.objects.get(pk=self.tenant.pk).status, Tenant.Status.PENDING,
        )

        progress = OnboardingProgress.all_objects.get(tenant=self.tenant)
        self.assertEqual(progress.readiness_state, ReadinessState.READY)
        self.assertIsNone(progress.go_live_at)

        row = GoLiveRequest.all_objects.get(pk=self.request_row.pk)
        self.assertEqual(row.status, GoLiveStatus.FAILED)
        self.assertEqual(row.failure_reference, reference)
        self.assertEqual(row.reviewed_by_id, self.reviewer.pk)

        failed = self.audit_rows(AuditActionType.GO_LIVE_FAILED, self.tenant)
        self.assertEqual(failed.count(), 1)
        self.assertEqual(
            failed.first().metadata.get("failure_reference"), reference,
        )
        # And nothing claimed success on the way past.
        self.assertEqual(
            self.audit_rows(AuditActionType.GO_LIVE_ACTIVATED, self.tenant).count(), 0,
        )

    def test_a_failure_after_the_school_is_written_is_rolled_back(self):
        """The test that actually proves the transaction.

        The failure above is forced at the school write, so nothing had been
        written when it fired and the test would pass even with no transaction
        at all. This one fails on the *last* write in the block, after the
        school has already been saved and its tenant mirrored, so a school left
        ACTIVE here is a half-live school.
        """
        with patch.object(
            GoLiveRequest, "save", side_effect=RuntimeError("the request write failed"),
        ):
            response = self.approve()

        self.assertErrorCode(response, 500, "GOLIVE_ACTIVATION_FAILED")
        self.assertEqual(
            School.objects.get(pk=self.school.pk).status, SchoolStatus.PENDING,
        )
        self.assertEqual(
            Tenant.objects.get(pk=self.tenant.pk).status, Tenant.Status.PENDING,
        )
        progress = OnboardingProgress.all_objects.get(tenant=self.tenant)
        self.assertIsNone(progress.go_live_at)
        self.assertEqual(progress.readiness_state, ReadinessState.READY)
        self.assertEqual(
            GoLiveRequest.all_objects.get(pk=self.request_row.pk).status,
            GoLiveStatus.FAILED,
        )

    def test_activating_an_already_live_school_is_a_no_op(self):
        self.approve()
        original = OnboardingProgress.all_objects.get(tenant=self.tenant).go_live_at
        second = self.submit_request()

        response = self.approve(pk=second.pk)

        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(
            OnboardingProgress.all_objects.get(tenant=self.tenant).go_live_at, original,
        )
        self.assertEqual(
            GoLiveRequest.all_objects.get(pk=second.pk).status, GoLiveStatus.ACTIVATED,
        )

    def test_activation_opens_the_rest_of_the_platform_to_the_same_caller(self):
        """No role, assignment or permission key changes. Only the status."""
        client = self.client_for(self.admin)
        closed = client.get(f"/v1/finance/entities/?tenant={self.tenant.slug}")
        self.assertEqual(closed.status_code, 403, closed.data)
        self.assertEqual(closed.data["error"]["code"], "TENANT_NOT_LIVE")

        self.approve()

        opened = client.get(f"/v1/finance/entities/?tenant={self.tenant.slug}")
        if opened.status_code == 403:
            self.assertNotEqual(
                opened.data.get("error", {}).get("code"), "TENANT_NOT_LIVE",
            )


class GoLiveListTests(OnboardingFixture):
    def test_the_list_is_newest_first_and_paginated(self):
        old = GoLiveRequest.all_objects.create(
            tenant=self.tenant, requested_by=self.admin,
            preferred_go_live_at=timezone.now(), acknowledged=True,
            status=GoLiveStatus.REJECTED, rejection_reason="no",
            created_at=timezone.now() - timezone.timedelta(days=2),
        )
        new = self.submit_request()

        response = self.client_for(self.admin).get(self.scoped("onboarding-go-live-list"))

        self.assertEqual(response.status_code, 200, response.data)
        self.assertIn("pagination", response.data)
        self.assertEqual([row["id"] for row in response.data["data"]], [new.pk, old.pk])

    def test_an_empty_go_live_list_keeps_its_list_shape(self):
        """The paginated list is the one place the empty-list coercion does not bite.

        ``XVSPagination`` builds its own envelope and writes ``data`` straight
        in, so an empty page is ``[]``, while the unpaginated task list goes
        through ``success_response`` and arrives as ``{}``. Two endpoints in one
        module answering an empty collection differently is exactly the kind of
        thing a client gets wrong, so both shapes are pinned.
        """
        response = self.client_for(self.admin).get(self.scoped("onboarding-go-live-list"))

        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["data"], [])
        self.assertEqual(response.data["pagination"]["totalItems"], 0)


# ══════════════════════════════════════════════════════════════════════════
# 10.3  Shape and scale
# ══════════════════════════════════════════════════════════════════════════

class StatePayloadTests(OnboardingFixture):
    def test_the_state_payload_counts_and_blocks_from_the_current_rows(self):
        self.set_status(self.tenant, TaskKey.FIRST_ADMIN, TaskStatus.DONE)
        self.set_status(self.tenant, TaskKey.INITIAL_DATA, TaskStatus.SKIPPED)

        response = self.client_for(self.admin).get(self.scoped("onboarding-state"))

        data = response.data["data"]
        total = self.tasks_for().count()
        self.assertEqual(data["counts"]["done"], 1)
        self.assertEqual(data["counts"]["skipped"], 1)
        self.assertEqual(data["counts"]["total"], total)
        self.assertEqual(data["counts"]["remaining"], total - 2)
        self.assertTrue(data["go_live_blocked"])
        self.assertNotIn(TaskKey.FIRST_ADMIN.value, data["blocking_tasks"])
        self.assertNotIn(TaskKey.INITIAL_DATA.value, data["blocking_tasks"])

    def test_state_is_404_for_a_tenant_that_was_never_provisioned(self):
        OnboardingTask.all_objects.filter(tenant=self.tenant).delete()
        OnboardingProgress.all_objects.filter(tenant=self.tenant).delete()

        response = self.client_for(self.admin).get(self.scoped("onboarding-state"))

        self.assertErrorCode(response, 404, "ONBOARDING_NOT_PROVISIONED")

    def test_state_costs_the_same_number_of_queries_however_many_tasks_there_are(self):
        """One query for the progress row, one for the tasks, none per task."""
        client = self.client_for(self.admin)
        url = self.scoped("onboarding-state")
        OnboardingTask.all_objects.filter(tenant=self.tenant).exclude(
            key__in=[TaskKey.FIRST_ADMIN, TaskKey.ROLE_BASELINE],
        ).delete()

        with CaptureQueriesContext(connection) as captured:
            self.assertEqual(client.get(url).status_code, 200)
        baseline = len(captured.captured_queries)

        for index, entry in enumerate(TASK_CATALOG):
            OnboardingTask.all_objects.get_or_create(
                tenant=self.tenant, key=entry.key,
                defaults={
                    "title": entry.title, "is_required": entry.is_required,
                    "order_index": entry.order_index,
                },
            )
        self.assertGreater(self.tasks_for().count(), 2)

        with self.assertNumQueries(baseline):
            self.assertEqual(client.get(url).status_code, 200)

    def test_an_empty_task_list_is_an_empty_object(self):
        """``success_response`` writes ``data or {}``, so [] arrives as {}.

        Not the shape a reader expects, which is exactly why it is pinned.
        """
        OnboardingTask.all_objects.filter(tenant=self.tenant).delete()

        response = self.client_for(self.admin).get(self.scoped("onboarding-task-list"))

        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["data"], {})

    def test_the_task_list_is_returned_in_catalog_order(self):
        response = self.client_for(self.admin).get(self.scoped("onboarding-task-list"))

        keys = [row["key"] for row in response.data["data"]]
        expected = [entry.key.value for entry in TASK_CATALOG]
        self.assertEqual(keys, expected)


class IndexTests(TestCase):
    """The columns every list and filter in this module reads."""

    def test_onboarding_task_is_indexed_on_tenant_and_status(self):
        indexes = [
            tuple(index.fields) for index in OnboardingTask._meta.indexes
        ]
        self.assertIn(("tenant", "status"), indexes)

    def test_readiness_state_is_indexed(self):
        field = OnboardingProgress._meta.get_field("readiness_state")
        self.assertTrue(field.db_index)

    def test_both_named_constraints_exist(self):
        names = {c.name for c in OnboardingTask._meta.constraints}
        self.assertIn("uq_onboarding_task_tenant_key", names)
        names = {c.name for c in GoLiveRequest._meta.constraints}
        self.assertIn("uq_golive_one_pending_per_tenant", names)


# ══════════════════════════════════════════════════════════════════════════
# 10.4  Multi-shape tenancy
# ══════════════════════════════════════════════════════════════════════════

class BranchShapeTests(TestCase):
    """Single-branch and multi-branch schools must both work, identically.

    Onboarding used to vary with branch shape: a branch step existed only for a
    school whose ``operates_branches`` flag was set. The flag is gone, and with
    it the last thing in this module that asked how many sites a school runs.
    These tests are what stops it coming back as a conditional step keyed off a
    branch count instead.
    """

    def _keys_after_provisioning(self, school):
        provision_onboarding(school.tenant)
        return set(
            OnboardingTask.all_objects
            .filter(tenant=school.tenant)
            .values_list("key", flat=True)
        )

    def test_a_single_branch_school_gets_the_whole_catalog(self):
        school = make_school(
            slug="single-site", name="Single Site", status="PENDING",
        )
        make_branch(school, name="HQ", status=BranchStatus.ACTIVE)

        self.assertEqual(
            self._keys_after_provisioning(school),
            {entry.key.value for entry in TASK_CATALOG},
        )

    def test_a_three_branch_school_gets_exactly_the_same_steps(self):
        school = make_school(
            slug="three-sites", name="Three Sites", status="PENDING",
        )
        main = make_branch(school, name="HQ", status=BranchStatus.ACTIVE)
        make_branch(school, name="Annex", is_main=False, status=BranchStatus.ACTIVE)
        make_branch(school, name="Lekki", is_main=False, status=BranchStatus.PENDING)

        keys = self._keys_after_provisioning(school)

        self.assertEqual(keys, {entry.key.value for entry in TASK_CATALOG})
        self.assertEqual(Branch.all_objects.filter(tenant=school.tenant).count(), 3)
        self.assertTrue(main.is_main)

    def test_no_step_is_named_for_branches_any_more(self):
        """The retired key is gone from the vocabulary, not merely unused.

        A key still in ``TaskKey`` would be a step the API advertises and no
        school can ever hold.
        """
        from .services.conditions import TASK_CONDITIONS

        self.assertNotIn("BRANCH_SETUP", TaskKey.values)
        self.assertNotIn("BRANCH_SETUP", [entry.key for entry in TASK_CATALOG])
        self.assertNotIn("BRANCH_SETUP", TASK_CONDITIONS)


class TwoTenantsOnboardingTests(TestCase):
    """Two schools onboarding at once see and affect nothing of each other's."""

    def setUp(self):
        self.first = make_school(slug="tenant-one", name="One", status="PENDING")
        self.second = make_school(slug="tenant-two", name="Two", status="PENDING")
        provision_onboarding(self.first.tenant)
        provision_onboarding(self.second.tenant)

    def test_their_task_sets_are_separate_rows(self):
        first_ids = set(
            OnboardingTask.all_objects
            .filter(tenant=self.first.tenant).values_list("pk", flat=True)
        )
        second_ids = set(
            OnboardingTask.all_objects
            .filter(tenant=self.second.tenant).values_list("pk", flat=True)
        )

        self.assertTrue(first_ids)
        self.assertEqual(first_ids & second_ids, set())

    def test_completing_one_schools_checklist_leaves_the_other_untouched(self):
        OnboardingTask.all_objects.filter(tenant=self.first.tenant).update(
            status=TaskStatus.DONE, completed_at=timezone.now(),
        )
        ReadinessService.evaluate(self.first.tenant)
        ReadinessService.evaluate(self.second.tenant)

        self.assertEqual(
            OnboardingProgress.all_objects.get(
                tenant=self.first.tenant,
            ).readiness_state,
            ReadinessState.READY,
        )
        self.assertEqual(
            OnboardingProgress.all_objects.get(
                tenant=self.second.tenant,
            ).readiness_state,
            ReadinessState.NOT_READY,
        )
        self.assertFalse(
            OnboardingTask.all_objects
            .filter(tenant=self.second.tenant, status=TaskStatus.DONE)
            .exists()
        )


# ══════════════════════════════════════════════════════════════════════════
# Notifications and the seeder
# ══════════════════════════════════════════════════════════════════════════

class NotificationTests(OnboardingFixture):
    """The context keys have to match the templates that were already seeded.

    Asserted against real rendering, not against a mock: a context key that the
    template does not use renders an empty space rather than raising, so the
    only way to know they line up is to read the rendered body back.
    """

    def setUp(self):
        super().setUp()
        from vs_notifications.services.seed import (
            seed_notification_templates, seed_platform_settings,
        )

        seed_notification_templates()
        seed_platform_settings()

    def _notifications(self, event_key):
        from vs_notifications.models import Notification

        return Notification.objects.filter(event_type__key=event_key)

    def test_completing_a_step_renders_the_seeded_template(self):
        with self.captureOnCommitCallbacks(execute=True):
            response = self.client_for(self.admin).patch(
                self.scoped("onboarding-task-detail", TaskKey.ACADEMIC_STRUCTURE),
                {"status": TaskStatus.DONE}, format="json",
            )
        self.assertEqual(response.status_code, 200, response.data)

        rows = self._notifications("onboarding.step_completed")
        self.assertTrue(rows.exists())
        body = rows.first().body
        self.assertIn("Set up your academic structure", body)
        self.assertIn("Alpha School", str([r.body + r.subject for r in rows]))
        self.assertNotIn("{{", body)

    def test_activation_notifies_with_the_go_live_timestamp(self):
        self.make_ready()
        request_row = self.submit_request()
        OnboardingProgress.all_objects.filter(tenant=self.tenant).update(
            readiness_state=ReadinessState.PENDING_APPROVAL,
        )

        with self.captureOnCommitCallbacks(execute=True):
            response = self.client_for(self.reviewer).post(
                self.scoped("onboarding-go-live-approve", request_row.pk),
                {}, format="json",
            )
        self.assertEqual(response.status_code, 200, response.data)

        reviewed = self._notifications("onboarding.go_live_reviewed")
        activated = self._notifications("onboarding.activated")
        self.assertTrue(reviewed.exists())
        self.assertTrue(activated.exists())
        self.assertIn("approved", reviewed.first().body)
        self.assertNotIn("{{", activated.first().body)

    def test_a_notification_failure_never_undoes_committed_state(self):
        from vs_notifications.exceptions import UnknownEventTypeError

        with patch(
            "vs_notifications.services.dispatch.NotificationService.send",
            side_effect=UnknownEventTypeError(message="gone"),
        ):
            with self.captureOnCommitCallbacks(execute=True):
                response = self.client_for(self.admin).patch(
                    self.scoped("onboarding-task-detail", TaskKey.ACADEMIC_STRUCTURE),
                    {"status": TaskStatus.DONE}, format="json",
                )

        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(
            self.task(TaskKey.ACADEMIC_STRUCTURE).status, TaskStatus.DONE,
        )
        self.assertEqual(
            self.audit_rows(
                AuditActionType.ONBOARDING_TASK_COMPLETED, self.tenant,
            ).count(),
            1,
        )

    def test_recipients_are_the_holders_of_the_control_room_key(self):
        """The audience and the permission gate read the same key by design."""
        from .services.effects import notification_recipients

        outsider = make_school_admin(
            None, email="outsider@test.com", tenant=self.tenant,
        )

        recipients = {user.pk for user in notification_recipients(self.tenant)}

        self.assertIn(self.admin.pk, recipients)
        self.assertNotIn(outsider.pk, recipients)
        self.assertNotIn(self.rival_admin.pk, recipients)


class SeederTests(TestCase):
    """The eight keys, their sensitivities and their default holders."""

    EXPECTED = {
        "onboarding.progress.view": "NORMAL",
        "onboarding.progress.create": "SENSITIVE",
        "onboarding.progress.reactivate": "SENSITIVE",
        "onboarding.task.update": "NORMAL",
        "onboarding.go_live.submit": "SENSITIVE",
        "onboarding.go_live.view": "NORMAL",
        "onboarding.go_live.approve": "CRITICAL",
        "onboarding.go_live.reject": "CRITICAL",
    }

    def _seed(self, *args):
        out = StringIO()
        call_command("seed_onboarding_permissions", *args, stdout=out, stderr=StringIO())
        return out.getvalue()

    def setUp(self):
        from vs_rbac.models import TenantRoleTemplate

        call_command("seed_actions", stdout=StringIO())
        call_command("seed_prebuilt_role_templates", stdout=StringIO())
        # The platform roles arrive with create_superuser rather than with the
        # migrations, so a test database has none. Without them the seeder
        # correctly warns and skips its fourth phase, and a grant assertion
        # would be checking nothing.
        for key, name in (
            ("xvs_super_admin", "XVS Super Admin"),
            ("xvs_platform_admin", "XVS Platform Admin"),
        ):
            TenantRoleTemplate.objects.get_or_create(
                tenant=codex_tenant(), key=key,
                defaults={"name": name, "status": "ACTIVE", "is_system_role": True},
            )

    def test_it_registers_the_seven_keys_with_their_sensitivities(self):
        self._seed()

        for key, sensitivity in self.EXPECTED.items():
            with self.subTest(key=key):
                permission = Permission.objects.get(key=key)
                self.assertEqual(permission.sensitivity_level, sensitivity)
                self.assertEqual(
                    permission.is_restricted, sensitivity != "NORMAL",
                )

    def test_the_school_admin_default_holds_the_school_keys_and_not_the_others(self):
        from vs_rbac.models import PrebuiltRolePermission

        self._seed()

        held = set(
            PrebuiltRolePermission.objects
            .filter(prebuilt_role__key="school_admin", permission__key__startswith="onboarding.")
            .values_list("permission_id", flat=True)
        )
        self.assertEqual(held, set(SCHOOL_KEYS))
        self.assertNotIn("onboarding.go_live.approve", held)
        self.assertNotIn("onboarding.progress.create", held)
        self.assertNotIn("onboarding.progress.reactivate", held)

    def test_the_platform_roles_hold_the_review_keys(self):
        from vs_rbac.models import TenantRolePermission

        self._seed()

        for role_key in ("xvs_super_admin", "xvs_platform_admin"):
            with self.subTest(role=role_key):
                held = set(
                    TenantRolePermission.objects
                    .filter(
                        role__key=role_key, role__tenant__slug="codex",
                        permission__key__startswith="onboarding.",
                        granted=True,
                    )
                    .values_list("permission_id", flat=True)
                )
                self.assertIn("onboarding.go_live.approve", held)
                self.assertIn("onboarding.go_live.reject", held)
                self.assertIn("onboarding.progress.create", held)
                self.assertIn("onboarding.progress.reactivate", held)

    def test_it_is_idempotent(self):
        self._seed()
        before = Permission.objects.filter(key__startswith="onboarding.").count()

        output = self._seed()

        self.assertEqual(
            Permission.objects.filter(key__startswith="onboarding.").count(), before,
        )
        self.assertIn("0 new permission(s) created", output)

    def test_it_backfills_a_school_provisioned_before_the_keys_existed(self):
        from vs_rbac.models import TenantRolePermission

        school = make_school(slug="legacy-school", name="Legacy", status="PENDING")
        role = make_role(
            school.tenant, name="School Admin", key="school_admin",
            is_system_role=True,
        )

        self._seed()

        held = set(
            TenantRolePermission.objects
            .filter(role=role, permission__key__startswith="onboarding.", granted=True)
            .values_list("permission_id", flat=True)
        )
        self.assertEqual(held, set(SCHOOL_KEYS))

    def test_dry_run_writes_nothing(self):
        output = self._seed("--dry-run")

        self.assertIn("rolled back", output)
        self.assertFalse(
            Permission.objects.filter(key__startswith="onboarding.").exists(),
        )

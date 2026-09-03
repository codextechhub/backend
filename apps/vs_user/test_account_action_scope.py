"""Who a caller may act *on* through the account actions.

The eight endpoints below all take a target account by raw primary key:

    PATCH /v1/user/<id>/email/change/
    POST  /v1/user/<id>/suspend/
    POST  /v1/user/<id>/reactivate/
    POST  /v1/user/<id>/unlock/
    POST  /v1/user/<id>/password-reset/
    POST  /v1/user/<id>/invite/resend/
    POST  /v1/user/sessions/force-logout/        {"user_id": <id>}
    POST  /v1/user/account-lockouts/unlock/      {"user_id": <id>}

Every one of them resolved that id through ``User.objects``, which is a plain
manager, and the key is a sequential integer. The RBAC gate answered "may you
suspend staff?" and nobody ever asked "*whose* staff?" - so Bright Star's
administrator, holding ``platform.team.suspend`` for her own people, could
suspend a Greenfield teacher by sending Greenfield's id.

The fix is one shared lookup (:mod:`vs_user.account_scope`), so these tests are
written as a matrix over all eight rather than eight separate suites: the point
is that no endpoint is left beside the gate rather than behind it.

Both shapes of school are here on purpose. Bright Star has two branches and
Greenfield has one, and the boundary has to hold in both directions.
"""
from __future__ import annotations

from core.test_utils import TenantAPIClient
from django.test import TestCase

from vs_rbac.tests.helpers import (
    make_assignment,
    make_branch,
    make_permission,
    make_role,
    make_role_permission,
    platform_tenant,
)
from vs_user.models import (
    AccountLockout,
    LoginSession,
    PasswordResetRequest,
    User,
    UserInvitation,
)


KEYS = (
    "platform.team.view",
    "platform.team.create",
    "platform.team.update",
    "platform.team.suspend",
    "platform.team.reactivate",
)

#: The status each action needs its target to be in, so that a refusal can only
#: ever be about scope. A target in the wrong status answers 422
#: (INVALID_STATUS_TRANSITION), which would hide the thing under test.
TARGET_STATUS = {
    "email": User.Status.ACTIVE,
    "suspend": User.Status.ACTIVE,
    "reactivate": User.Status.SUSPENDED,
    "unlock": User.Status.LOCKED,
    "password_reset": User.Status.ACTIVE,
    "resend": User.Status.PENDING,
    "force_logout": User.Status.ACTIVE,
    "lockout_unlock": User.Status.LOCKED,
}

ACTIONS = tuple(TARGET_STATUS)


def _school(name, slug):
    from schools.vs_schools.models import School

    return School.objects.create(name=name, slug=slug, status="ACTIVE")


class AccountActionTenantScopeTests(TestCase):
    """One matrix over every account action that resolves a user by id."""

    def setUp(self):
        self.bright_star = _school("Bright Star School", "bright-star")
        self.greenfield = _school("Greenfield Academy", "greenfield")
        self.codex = platform_tenant()

        # Bright Star runs two sites; Greenfield runs one. A single-branch test
        # proves nothing about a multi-branch one, so both are here.
        self.ikeja = self._main_branch(self.bright_star)
        self.lekki = make_branch(self.bright_star, name="Lekki", is_main=False)
        self.greenfield_main = self._main_branch(self.greenfield)

        # A school-wide administrator: no branch posting, whole-tenant grant.
        # ``a4916e9`` made the null branch the normal shape for a school user,
        # and it is what "administers the whole school" means, so she must reach
        # Ikeja and Lekki alike - and no further.
        self.amaka = self._user("amaka@bright-star.test", self.bright_star.tenant)
        self.grant(self.amaka, self.bright_star.tenant)

        # Greenfield's own administrator, so the boundary is tested from the
        # one-branch side as well as the two-branch side.
        self.folake = self._user("folake@greenfield.test", self.greenfield.tenant)
        self.grant(self.folake, self.greenfield.tenant)

        # A Codex operator holding the same keys on the platform tenant. Not a
        # super admin - the bypass would prove nothing about the queryset.
        self.operator = self._user("operator@codex.test", self.codex)
        self.grant(self.operator, self.codex)

        self.client = TenantAPIClient(self.amaka)

    # -- fixtures ---------------------------------------------------------

    @staticmethod
    def _main_branch(school):
        from vs_tenants.models import Branch

        branch = Branch.all_objects.filter(tenant=school.tenant, is_main=True).first()
        if branch is None:
            branch = make_branch(school, name="Main Branch", is_main=True)
        return branch

    _person = 0

    @classmethod
    def _user(cls, email, tenant, *, branch=None, status=User.Status.ACTIVE):
        cls._person += 1
        return User.objects.create_user(
            email=email,
            password="Str0ng!pass123",
            status=status,
            first_name="Test",
            last_name=f"Person{cls._person}",
            tenant=tenant,
            branch=branch,
        )

    @staticmethod
    def grant(user, tenant, *, branch=None):
        """The whole team key family, as a school administrator holds it."""
        role = make_role(tenant, name=f"Administrator {user.email}", branch=branch)
        for key in KEYS:
            make_role_permission(role, make_permission(key))
        return make_assignment(tenant, user, role, branch=branch)

    def target(self, action, tenant, *, branch=None, email=None):
        """A fresh account in whatever status *action* requires."""
        status = TARGET_STATUS[action]
        user = self._user(
            email or f"{action}-{self._person + 1}@{tenant.slug}.test",
            tenant, branch=branch, status=status,
        )
        if status == User.Status.LOCKED:
            AccountLockout.objects.create(user=user, failure_count=5)
        if action == "force_logout":
            LoginSession.all_objects.create(
                user=user, tenant=tenant, refresh_jti=f"jti-{user.pk}",
            )
        return user

    # -- the calls --------------------------------------------------------

    def act(self, client, action, target):
        """Perform *action* against *target* exactly as the frontend would."""
        pk = target.pk
        if action == "email":
            return client.patch(
                f"/v1/user/{pk}/email/change/",
                {"email": f"moved-{pk}@relocated.test"}, format="json",
            )
        if action == "suspend":
            return client.post(f"/v1/user/{pk}/suspend/", {}, format="json")
        if action == "reactivate":
            return client.post(f"/v1/user/{pk}/reactivate/", {}, format="json")
        if action == "unlock":
            return client.post(f"/v1/user/{pk}/unlock/", {}, format="json")
        if action == "password_reset":
            return client.post(f"/v1/user/{pk}/password-reset/", {}, format="json")
        if action == "resend":
            return client.post(f"/v1/user/{pk}/invite/resend/", {}, format="json")
        if action == "force_logout":
            return client.post(
                "/v1/user/sessions/force-logout/",
                {"user_id": pk, "reason": "Device lost"}, format="json",
            )
        if action == "lockout_unlock":
            return client.post(
                "/v1/user/account-lockouts/unlock/",
                {"user_id": pk}, format="json",
            )
        raise AssertionError(f"unknown action {action!r}")

    @staticmethod
    def untouched(target, action):
        """Whether *target* is still exactly as the fixture left it.

        Each action is judged by the row it actually writes, not by a proxy: a
        refused resend must leave no invitation behind and a refused force
        logout must leave the session running, and neither of those shows up in
        ``User.status``.
        """
        fresh = User.objects.get(pk=target.pk)
        if action == "email":
            return fresh.email == target.email
        if action == "password_reset":
            return not PasswordResetRequest.objects.filter(user=fresh).exists()
        if action == "resend":
            return not UserInvitation.objects.filter(user=fresh).exists()
        if action == "force_logout":
            return LoginSession.all_objects.filter(
                user=fresh, is_active=True,
            ).exists()
        return fresh.status == TARGET_STATUS[action]

    # -- the boundary -----------------------------------------------------

    def test_an_admin_cannot_act_on_another_tenants_user(self):
        """The hole this suite exists to close.

        Amaka administers Bright Star. She sends the primary key of Tunde, a
        Greenfield teacher she has never heard of, to each of the eight
        endpoints. Unscoped, every one of them does the work.
        """
        for action in ACTIONS:
            with self.subTest(action=action):
                tunde = self.target(action, self.greenfield.tenant,
                                    branch=self.greenfield_main)

                response = self.act(self.client, action, tunde)

                self.assertEqual(response.status_code, 404, response.data)
                self.assertTrue(self.untouched(tunde, action))

    def test_the_refusal_does_not_confirm_the_id_exists(self):
        """404, not 403.

        A 403 would answer "yes, account 41 is real, you just may not have it",
        which is the enumeration the scoping is closing. An id belonging to
        another school and an id belonging to nobody must read identically.
        """
        for action in ACTIONS:
            with self.subTest(action=action):
                tunde = self.target(action, self.greenfield.tenant,
                                    branch=self.greenfield_main)
                nobody = User.objects.order_by("-pk").first().pk + 5000

                real = self.act(self.client, action, tunde)
                fictional = self.act(
                    self.client, action, type("X", (), {"pk": nobody})(),
                )

                self.assertEqual(real.status_code, fictional.status_code)
                self.assertEqual(real.data["message"], fictional.data["message"])

    def test_the_boundary_holds_from_the_single_branch_school_too(self):
        """Greenfield has one branch. That must not be why it is safe."""
        for action in ACTIONS:
            with self.subTest(action=action):
                chidi = self.target(action, self.bright_star.tenant,
                                    branch=self.lekki)

                response = self.act(
                    TenantAPIClient(self.folake), action, chidi,
                )

                self.assertEqual(response.status_code, 404, response.data)
                self.assertTrue(self.untouched(chidi, action))

    # -- what must keep working -------------------------------------------

    def test_an_admin_can_still_act_on_their_own_tenants_users(self):
        for action in ACTIONS:
            with self.subTest(action=action):
                chidi = self.target(action, self.bright_star.tenant,
                                    branch=self.lekki)

                response = self.act(self.client, action, chidi)

                self.assertIn(response.status_code, (200, 201), response.data)
                self.assertFalse(self.untouched(chidi, action))

    def test_a_platform_operator_still_reaches_every_tenant(self):
        """The console is *for* acting across tenants; it must not be narrowed."""
        operator = TenantAPIClient(self.operator)
        for action in ACTIONS:
            with self.subTest(action=action):
                tunde = self.target(action, self.greenfield.tenant,
                                    branch=self.greenfield_main)

                response = self.act(operator, action, tunde)

                self.assertIn(response.status_code, (200, 201), response.data)
                self.assertFalse(self.untouched(tunde, action))

    def test_a_branch_pinned_admin_can_still_act_on_her_own_account(self):
        """Self-service is the common case and must survive the narrowing.

        Bisi is posted at Ikeja but her only role grant is pinned to Lekki, so
        the branch rule says she sees Lekki's rows - and her own row is not one
        of them. Changing her own email address must still work: an account is
        always within reach of the person it belongs to.
        """
        bisi = self._user(
            "bisi@bright-star.test", self.bright_star.tenant, branch=self.ikeja,
        )
        self.grant(bisi, self.bright_star.tenant, branch=self.lekki)

        response = TenantAPIClient(bisi).patch(
            f"/v1/user/{bisi.pk}/email/change/",
            {"email": "bisi.new@bright-star.test"}, format="json",
        )

        self.assertEqual(response.status_code, 200, response.data)
        bisi.refresh_from_db()
        self.assertEqual(bisi.email, "bisi.new@bright-star.test")

    def test_a_branch_pinned_admin_cannot_act_across_branches(self):
        """The same line that stops a cross-tenant reach stops a cross-branch one.

        ``654e7af`` already made this true of retrieve/update/destroy through
        ``UserAccountViewSet``. Suspending somebody is at least as strong as
        deactivating them, so it answers the same way.
        """
        bisi = self._user(
            "pinned@bright-star.test", self.bright_star.tenant, branch=self.ikeja,
        )
        self.grant(bisi, self.bright_star.tenant, branch=self.ikeja)
        lekki_colleague = self.target(
            "suspend", self.bright_star.tenant, branch=self.lekki,
        )

        response = self.act(TenantAPIClient(bisi), "suspend", lekki_colleague)

        self.assertEqual(response.status_code, 404, response.data)
        self.assertTrue(self.untouched(lekki_colleague, "suspend"))

    def test_a_non_numeric_id_is_not_found_rather_than_a_server_error(self):
        """The routes used to declare ``<str:user_id>``, so
        ``/v1/user/abc/suspend/`` reached the view and handed ``'abc'`` to the
        ORM, which raised ``ValueError`` and answered 500 for what is plainly a
        bad address."""
        response = self.client.post("/v1/user/not-a-number/suspend/", {}, format="json")

        self.assertEqual(response.status_code, 404)

    def test_the_lockout_unlock_route_is_not_swallowed_by_the_user_routes(self):
        """``account-lockouts/unlock/`` is a router route, and the by-id account
        routes are resolved before it.

        While ``<str:user_id>/unlock/`` existed it matched that URL first, with
        ``user_id="account-lockouts"``, and every call to the account-lockout
        unlock endpoint died in the ORM with a 500. The endpoint was unreachable
        for as long as the converter was a string, which is also why it had no
        tenant scoping worth speaking of: nothing could call it.
        """
        target = self.target("lockout_unlock", self.bright_star.tenant,
                             branch=self.lekki)

        response = self.client.post(
            "/v1/user/account-lockouts/unlock/", {"user_id": target.pk},
            format="json",
        )

        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(
            User.objects.get(pk=target.pk).status, User.Status.ACTIVE,
        )

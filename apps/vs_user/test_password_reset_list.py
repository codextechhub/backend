"""Regression coverage for the Password Activity screen's reset list.

``GET /v1/user/password-resets/?tenant=codex`` returned 500 SERVER_ERROR on
every call, for every caller, from the day it was written: the view returned
``success_response(data=...)`` and ``message`` was a required positional
argument, so the handler raised TypeError before it could serialise anything.
The queryset, the serializer and the permission gate were all fine, which is
why the failure was invisible until the response was built.

These tests go through the real auth layer with ``TenantAPIClient`` so the
``?tenant=`` assertion the console sends is exercised, not bypassed.
"""

from datetime import timedelta

from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.utils import timezone
from rest_framework.test import APIClient

from core.test_utils import TenantAPIClient
from vs_rbac.tests.helpers import (
    codex_tenant,
    make_branch,
    make_school,
    make_school_admin,
    make_vision_user,
)
from vs_user.models import PasswordResetRequest
from vs_user.action_tokens import issue_password_reset_token

URL = "/v1/user/password-resets/"


def _reset(user, *, hours=1, used=False):
    """One reset row for *user*. Only one may be active per user."""
    _token, token_hash = issue_password_reset_token()
    return PasswordResetRequest.objects.create(
        user=user,
        token_hash=token_hash,
        expires_at=timezone.now() + timedelta(hours=hours),
        used_at=timezone.now() if used else None,
        requested_by="ADMIN",
        requested_ip="203.0.113.7",
    )


class PasswordResetListTests(TestCase):
    """A CX operator reading the pending resets across the platform."""

    def setUp(self):
        self.codex = codex_tenant()
        self.operator = make_vision_user(
            email="reset.operator@codex.test", super_admin=True,
        )
        self.cx_colleague = make_vision_user(email="reset.colleague@codex.test")

        self.school = make_school(slug="reset-academy", name="Reset Academy")
        self.principal = make_school_admin(
            make_branch(self.school),
            email="principal@reset-academy.test",
            first_name="Ada",
            last_name="Okeye",
        )

        self.client = TenantAPIClient(self.operator, "codex")

    def _get(self, **params):
        response = self.client.get(URL, params or None)
        self.assertEqual(response.status_code, 200, response.content)
        return response.json()

    # ── the crash itself ──────────────────────────────────────────────────

    def test_the_console_request_returns_200_not_server_error(self):
        """The exact call the Password Activity screen makes."""
        _reset(self.principal)

        response = self.client.get(URL)

        self.assertEqual(response.status_code, 200, response.content)
        body = response.json()
        self.assertTrue(body["success"])
        self.assertIn("message", body)

    def test_the_row_carries_the_fields_the_screen_renders(self):
        row = _reset(self.principal)

        data = self._get()["data"]

        self.assertEqual([item["id"] for item in data], [row.id])
        self.assertEqual(data[0]["requested_by"], "ADMIN")
        self.assertEqual(data[0]["requested_ip"], "203.0.113.7")
        self.assertIsNone(data[0]["used_at"])
        self.assertEqual(data[0]["user"]["email"], "principal@reset-academy.test")
        self.assertEqual(data[0]["user"]["full_name"], "Ada Okeye")

    def test_every_row_names_the_tenant_it_belongs_to(self):
        """The list crosses tenants, so a row that does not name one is unusable.

        Revoking is destructive and irreversible for the holder of the link,
        and two schools can easily hold accounts under similar names.
        """
        school_row = _reset(self.principal)
        codex_row = _reset(self.cx_colleague)

        rows = {item["id"]: item for item in self._get()["data"]}

        self.assertEqual(rows[school_row.id]["tenant_name"], "Reset Academy")
        self.assertEqual(rows[school_row.id]["tenant_slug"], self.school.slug)
        self.assertEqual(rows[school_row.id]["tenant_id"], self.school.tenant_id)
        self.assertEqual(rows[codex_row.id]["tenant_slug"], "codex")

    def test_the_list_does_not_re_query_the_tenant_for_every_row(self):
        """Naming the tenant must not cost one query per pending reset.

        Pinned as "the same either way" rather than as a fixed number, so the
        test keeps its meaning when unrelated request overhead changes: what
        matters is that ten schools cost no more queries than one.
        """
        def _add_school(index):
            _reset(make_school_admin(
                make_branch(make_school(
                    slug=f"n-plus-one-{index}", name=f"N Plus One {index}",
                )),
                email=f"principal{index}@n-plus-one.test",
            ))

        _add_school(0)
        with CaptureQueriesContext(connection) as one_row:
            self.client.get(URL)

        for index in range(1, 10):
            _add_school(index)
        with CaptureQueriesContext(connection) as ten_rows:
            self.client.get(URL)

        self.assertEqual(len(ten_rows), len(one_row))

    # ── empty result set ──────────────────────────────────────────────────

    def test_an_empty_list_is_a_success_envelope_not_an_error(self):
        """No pending resets is the normal state, and must not read as failure.

        It must also still be a *list*. The envelope used to write
        ``data or {}``, which handed the screen an object and crashed it on
        ``pendingResets.map`` the moment nothing was pending - the ordinary
        case, not an edge one.
        """
        self.assertFalse(PasswordResetRequest.objects.exists())

        body = self._get()

        self.assertTrue(body["success"])
        self.assertEqual(body["data"], [])

    def test_used_and_expired_resets_are_left_out(self):
        _reset(self.principal, used=True)
        _reset(self.cx_colleague, hours=-1)

        self.assertEqual(self._get()["data"], [])

    # ── tenant scope ──────────────────────────────────────────────────────

    def test_the_list_reaches_every_tenant_not_only_the_asserted_one(self):
        """Asserting ``?tenant=codex`` must not hide the school rows.

        The screen exists to revoke a live reset link for a school's
        principal, so scoping the list to the platform tenant's own staff
        would empty it of everything worth seeing.
        """
        school_row = _reset(self.principal)
        codex_row = _reset(self.cx_colleague)

        ids = {item["id"] for item in self._get()["data"]}

        self.assertEqual(ids, {school_row.id, codex_row.id})

    def test_tenant_id_narrows_the_list_to_one_tenant(self):
        school_row = _reset(self.principal)
        _reset(self.cx_colleague)

        ids = {
            item["id"]
            for item in self._get(tenant_id=self.school.tenant_id)["data"]
        }

        self.assertEqual(ids, {school_row.id})

    def test_tenant_id_matching_nothing_returns_the_empty_envelope(self):
        _reset(self.principal)

        body = self._get(tenant_id=self.codex.id)

        self.assertTrue(body["success"])
        self.assertEqual(body["data"], [])

    def test_a_non_numeric_tenant_id_is_refused_not_crashed(self):
        response = self.client.get(URL, {"tenant_id": "codex"})

        self.assertEqual(response.status_code, 400, response.content)
        self.assertFalse(response.json()["success"])

    # ── who may read it ───────────────────────────────────────────────────

    def test_a_school_admin_cannot_read_the_platform_reset_list(self):
        _reset(self.cx_colleague)
        client = TenantAPIClient(self.principal, self.school.slug)

        response = client.get(URL)

        self.assertEqual(response.status_code, 403, response.content)

    def test_an_anonymous_caller_is_refused(self):
        response = APIClient().get(f"{URL}?tenant=codex")

        self.assertEqual(response.status_code, 401, response.content)

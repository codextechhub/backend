"""The Field Access endpoints: a role's switches and one-person field exceptions.

Security first. Bright Star's administrator must never read or change
Greenfield's switches or exceptions by editing a slug, a role key or a user id,
and must never learn that a platform-only field exists. A caller without the
Field Access keys is refused on every verb, and nobody records an exception on
themselves, including through a proxy session.

Then the audit trail, which is what makes an immediate, unapproved switch
accountable, and the rules a PATCH applies before anything is stored.
"""
import itertools
import json
from datetime import timedelta
from unittest import mock

from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone
from rest_framework import status
from rest_framework.test import APIClient

from vs_rbac.field_evaluator import get_field_access
from vs_rbac.models import (
    PermissionScope,
    RBACAuditLog,
    RoleFieldAccess,
    UserFieldAccessOverride,
)
from vs_tenants.models import Tenant
from vs_user.models import User
from vs_user.tokens import CodeXRefreshToken

from .helpers import (
    make_assignment,
    make_branch,
    make_field_definition,
    make_permission,
    make_platform_role,
    make_role,
    make_role_permission,
    make_school,
    make_school_admin,
    make_staff_user,
    make_vision_user,
)

_counter = itertools.count(1)

VIEW = "school.field_access.view"
UPDATE = "school.field_access.update"
EXCEPTION_VIEW = "school.user_overrides.view"
EXCEPTION_CREATE = "school.user_overrides.create"
EXCEPTION_DELETE = "school.user_overrides.delete"


def _grant(user, keys, tenant=None):
    tenant = tenant or user.tenant
    role = make_role(tenant, name=f"FA Grant {next(_counter)}")
    for key in keys:
        make_role_permission(role, make_permission(key))
    make_assignment(tenant, user, role)
    return role


def _client(user):
    client = APIClient()
    client.credentials(
        HTTP_AUTHORIZATION=f"Bearer {CodeXRefreshToken.for_user(user).access_token}",
    )
    return client


def _with_tenant(url, slug):
    return f"{url}?tenant={slug}"


class _FieldAccessApi(TestCase):
    """Bright Star School: an administrator, a Storekeeper role and a storekeeper."""

    def setUp(self):
        self.school = make_school(slug="fa-bright-star", name="Bright Star School")
        self.branch = make_branch(self.school, name="Ikeja Branch")
        self.tenant = self.school.tenant
        self.slug = self.tenant.slug

        self.name = make_field_definition("faapi.vendor.name", "Name", group="Vendor")
        self.phone = make_field_definition(
            "faapi.vendor.phone", "Phone", group="Contact", sensitive=True,
        )
        self.bank = make_field_definition(
            "faapi.vendor.bank_account_number", "Bank account number",
            group="Banking", sensitive=True,
        )
        self.total = make_field_definition(
            "faapi.vendor.total_spend", "Total spend",
            group="Vendor", sort_order=1, writable=False,
        )
        self.platform_field = make_field_definition(
            "faapi.staff.bank_name", "Bank name",
            sensitive=True, scope=PermissionScope.PLATFORM,
        )

        self.admin = make_school_admin(self.branch, email="fa-admin@test.com")
        self.admin_role = _grant(
            self.admin,
            [VIEW, UPDATE, EXCEPTION_VIEW, EXCEPTION_CREATE, EXCEPTION_DELETE],
        )
        self.storekeeper = make_role(self.tenant, name="Storekeeper", key="storekeeper")
        self.target = make_staff_user(self.branch, email="fa-target@test.com")
        make_assignment(self.tenant, self.target, self.storekeeper)

    # -- role switches ------------------------------------------------------
    def _role_url(self, key="storekeeper", slug=None):
        return reverse(
            "rbac-role-field-access",
            kwargs={"tenant_slug": slug or self.slug, "key": key},
        )

    def _get(self, user=None, key="storekeeper", **params):
        return _client(user or self.admin).get(
            self._role_url(key), {"tenant": self.slug, **params},
        )

    def _patch(self, changes, user=None, key="storekeeper"):
        return _client(user or self.admin).patch(
            _with_tenant(self._role_url(key), self.slug),
            {"changes": changes}, format="json",
        )

    # -- exceptions -----------------------------------------------------------
    def _exceptions_url(self, user_id=None, slug=None):
        return reverse(
            "rbac-user-field-access-override-list-create",
            kwargs={"tenant_slug": slug or self.slug, "user_id": user_id or self.target.pk},
        )

    def _exception_url(self, exception_id, user_id=None):
        return reverse(
            "rbac-user-field-access-override-detail",
            kwargs={
                "tenant_slug": self.slug,
                "user_id": user_id or self.target.pk,
                "id": exception_id,
            },
        )

    def _create_exception(self, client=None, user_id=None, **payload):
        body = {
            "field": self.bank.key, "access": "READ", "mode": "ALLOW",
            "reason": "Covering bursar duties 14-28 Sept.",
        }
        body.update(payload)
        return (client or _client(self.admin)).post(
            _with_tenant(self._exceptions_url(user_id), self.slug), body, format="json",
        )

    def _entries(self, response):
        return {entry["key"]: entry for entry in response.json()["data"]["fields"]}

    def _stored(self, field, role=None):
        row = RoleFieldAccess.objects.filter(role=role or self.storekeeper, field=field).first()
        return None if row is None else (row.can_read, row.can_write)


# =============================================================================
# Security
# =============================================================================
class CrossTenantIsolationTests(_FieldAccessApi):
    def setUp(self):
        super().setUp()
        self.other = make_school(slug="fa-greenfield", name="Greenfield School")
        self.other_branch = make_branch(self.other, name="Greenfield Main")
        self.other_role = make_role(self.other.tenant, name="Bursar", key="greenfield-bursar")
        self.other_user = make_staff_user(self.other_branch, email="fa-greenfield@test.com")

    def test_another_schools_role_key_reads_as_no_role(self):
        theirs = self._get(key="greenfield-bursar")
        missing = self._get(key="no-such-role")
        self.assertEqual(theirs.status_code, status.HTTP_404_NOT_FOUND)
        self.assertEqual(theirs.content, missing.content)

        patched = self._patch([{"field": self.name.key, "read": False}], key="greenfield-bursar")
        self.assertEqual(patched.status_code, status.HTTP_404_NOT_FOUND)
        self.assertFalse(RoleFieldAccess.objects.filter(role=self.other_role).exists())

    def test_another_schools_slug_is_404_either_way(self):
        url = self._role_url("greenfield-bursar", slug=self.other.tenant.slug)
        client = _client(self.admin)
        self.assertEqual(
            client.get(url, {"tenant": self.slug}).status_code, status.HTTP_404_NOT_FOUND,
        )
        self.assertEqual(
            client.get(url, {"tenant": self.other.tenant.slug}).status_code,
            status.HTTP_404_NOT_FOUND,
        )

    def test_another_schools_user_reads_as_no_user(self):
        client = _client(self.admin)
        url = _with_tenant(self._exceptions_url(self.other_user.pk), self.slug)
        self.assertEqual(client.get(url).status_code, status.HTTP_404_NOT_FOUND)
        created = self._create_exception(user_id=self.other_user.pk, field=self.name.key)
        self.assertEqual(created.status_code, status.HTTP_404_NOT_FOUND)
        self.assertFalse(UserFieldAccessOverride.objects.exists())

    def test_another_schools_exception_cannot_be_lifted(self):
        theirs = UserFieldAccessOverride.objects.create(
            tenant=self.other.tenant, user=self.other_user, field=self.name,
            access="READ", mode="DENY", reason="Greenfield's own.",
        )
        client = _client(self.admin)
        for user_id in (self.target.pk, self.other_user.pk):
            response = client.delete(
                _with_tenant(self._exception_url(theirs.pk, user_id=user_id), self.slug),
            )
            self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)
        self.assertTrue(UserFieldAccessOverride.objects.filter(pk=theirs.pk).exists())


class MissingKeysTests(_FieldAccessApi):
    def test_a_caller_without_keys_is_refused_on_every_endpoint_and_verb(self):
        stranger = make_staff_user(self.branch, email="fa-stranger@test.com")
        client = _client(stranger)
        existing = UserFieldAccessOverride.objects.create(
            tenant=self.tenant, user=self.target, field=self.name,
            access="READ", mode="DENY", reason="Existing.",
        )

        responses = {
            "role GET": self._get(user=stranger),
            "role PATCH": self._patch([{"field": self.name.key, "read": False}], user=stranger),
            "exceptions GET": client.get(_with_tenant(self._exceptions_url(), self.slug)),
            "exceptions POST": self._create_exception(client=client),
            "exception DELETE": client.delete(
                _with_tenant(self._exception_url(existing.pk), self.slug),
            ),
        }
        for label, response in responses.items():
            with self.subTest(label):
                self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.assertFalse(RoleFieldAccess.objects.exists())
        self.assertEqual(UserFieldAccessOverride.objects.count(), 1)

    def test_the_view_key_reads_and_cannot_change(self):
        viewer = make_staff_user(self.branch, email="fa-viewer@test.com")
        _grant(viewer, [VIEW])
        self.assertEqual(self._get(user=viewer).status_code, status.HTTP_200_OK)
        patched = self._patch([{"field": self.name.key, "read": False}], user=viewer)
        self.assertEqual(patched.status_code, status.HTTP_403_FORBIDDEN)
        self.assertFalse(RoleFieldAccess.objects.exists())

    def test_the_update_key_alone_can_read(self):
        manager = make_staff_user(self.branch, email="fa-manager@test.com")
        _grant(manager, [UPDATE])
        self.assertEqual(self._get(user=manager).status_code, status.HTTP_200_OK)

    def test_the_exception_view_key_lists_and_cannot_create(self):
        viewer = make_staff_user(self.branch, email="fa-exception-viewer@test.com")
        _grant(viewer, [EXCEPTION_VIEW])
        client = _client(viewer)
        listed = client.get(_with_tenant(self._exceptions_url(), self.slug))
        self.assertEqual(listed.status_code, status.HTTP_200_OK)
        self.assertEqual(
            self._create_exception(client=client).status_code, status.HTTP_403_FORBIDDEN,
        )

    def test_a_school_exception_key_gives_a_platform_actor_nothing(self):
        cx = make_vision_user(email="fa-cx-stranger@test.com")
        _grant(cx, [EXCEPTION_CREATE])
        url = reverse(
            "rbac-user-field-access-override-list-create",
            kwargs={"tenant_slug": cx.tenant.slug, "user_id": make_vision_user(
                email="fa-cx-colleague@test.com",
            ).pk},
        )
        response = _client(cx).get(_with_tenant(url, cx.tenant.slug))
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)


class PlatformFieldTests(_FieldAccessApi):
    def _redacted(self, response, key):
        return json.dumps(response.json()).replace(key, "<field>")

    def test_a_school_never_sees_a_platform_field(self):
        self.assertNotIn(self.platform_field.key, self._entries(self._get()))

    def test_setting_a_platform_field_fails_exactly_like_an_unknown_one(self):
        platform = self._patch([{"field": self.platform_field.key, "read": True}])
        unknown = self._patch([{"field": "faapi.staff.no_such_field", "read": True}])
        self.assertEqual(platform.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(unknown.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(
            self._redacted(platform, self.platform_field.key),
            self._redacted(unknown, "faapi.staff.no_such_field"),
        )
        self.assertFalse(RoleFieldAccess.objects.exists())

    def test_an_exception_on_a_platform_field_fails_exactly_like_an_unknown_one(self):
        platform = self._create_exception(field=self.platform_field.key, mode="DENY")
        unknown = self._create_exception(field="faapi.staff.no_such_field", mode="DENY")
        self.assertEqual(platform.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(
            self._redacted(platform, self.platform_field.key),
            self._redacted(unknown, "faapi.staff.no_such_field"),
        )
        self.assertFalse(UserFieldAccessOverride.objects.exists())

    def test_the_platform_tenant_can_set_a_platform_field(self):
        cx = make_vision_user(email="fa-cx-admin@test.com")
        _grant(cx, ["platform.field_access.view", "platform.field_access.update"])
        role = make_platform_role(name="FA Payroll Officer", key="fa-payroll-officer")
        slug = cx.tenant.slug
        url = reverse("rbac-role-field-access", kwargs={"tenant_slug": slug, "key": role.key})

        listed = _client(cx).get(url, {"tenant": slug})
        self.assertIn(self.platform_field.key, self._entries(listed))

        response = _client(cx).patch(
            _with_tenant(url, slug),
            {"changes": [{"field": self.platform_field.key, "read": True}]}, format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.content)
        self.assertEqual(self._stored(self.platform_field, role=role), (True, False))


class SelfExceptionTests(_FieldAccessApi):
    def test_an_exception_on_yourself_is_refused(self):
        created = self._create_exception(user_id=self.admin.pk)
        self.assertEqual(created.status_code, status.HTTP_403_FORBIDDEN)
        self.assertFalse(UserFieldAccessOverride.objects.exists())

    def test_lifting_your_own_exception_is_refused(self):
        mine = UserFieldAccessOverride.objects.create(
            tenant=self.tenant, user=self.admin, field=self.name,
            access="READ", mode="DENY", reason="Set by a colleague.",
        )
        response = _client(self.admin).delete(
            _with_tenant(self._exception_url(mine.pk, user_id=self.admin.pk), self.slug),
        )
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.assertTrue(UserFieldAccessOverride.objects.filter(pk=mine.pk).exists())

    def test_a_proxy_session_cannot_reach_either_identitys_own_exceptions(self):
        from vs_admin_console.models import ImpersonationSession

        _grant(self.admin, ["school.impersonation.start"])
        deputy = make_staff_user(self.branch, email="fa-deputy@test.com")
        _grant(deputy, [EXCEPTION_VIEW, EXCEPTION_CREATE, EXCEPTION_DELETE])
        session = ImpersonationSession.objects.create(
            staff_user=self.admin, tenant=self.tenant, target_user=deputy,
            justification="Support diagnosis.",
        )
        client = APIClient()
        client.credentials(
            HTTP_AUTHORIZATION=f"Bearer {CodeXRefreshToken.for_user(self.admin).access_token}",
            HTTP_X_IMPERSONATION_SESSION=str(session.pk),
        )

        as_proxied = self._create_exception(client=client, user_id=deputy.pk)
        as_actor = self._create_exception(client=client, user_id=self.admin.pk)
        self.assertEqual(as_proxied.status_code, status.HTTP_403_FORBIDDEN, as_proxied.content)
        self.assertEqual(as_actor.status_code, status.HTTP_403_FORBIDDEN, as_actor.content)
        self.assertFalse(UserFieldAccessOverride.objects.exists())


# =============================================================================
# Audit
# =============================================================================
class FieldAccessAuditTests(_FieldAccessApi):
    def _rows(self, action_type):
        return list(RBACAuditLog.objects.filter(action_type=action_type).order_by("id"))

    def test_one_flip_writes_one_row_with_the_escalation_marker(self):
        response = self._patch([{"field": self.bank.key, "read": True}])
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.content)

        rows = self._rows("FIELD_ACCESS_CHANGED")
        self.assertEqual(len(rows), 1)
        log = rows[0]
        self.assertEqual(log.entity_type, "RoleFieldAccess")
        self.assertEqual(log.entity_label, f"storekeeper:{self.bank.key}")
        self.assertEqual(log.severity, "WARNING")
        self.assertEqual(log.actor_id, self.admin.pk)
        self.assertEqual(log.school_id, self.slug)
        self.assertEqual(log.before_data, {"read": False, "write": False})
        self.assertEqual(log.diff_data, {"read": True, "write": False})
        self.assertEqual(log.metadata["tenant_id"], str(self.tenant.pk))
        self.assertEqual(log.metadata["school_id"], self.slug)
        self.assertEqual(log.metadata["role_key"], "storekeeper")
        self.assertEqual(log.metadata["field_key"], self.bank.key)
        self.assertEqual(log.metadata["switch"], "read")
        self.assertTrue(log.metadata["sensitive"])
        # The administrator's own role leaves the bank field at its closed default.
        self.assertFalse(log.metadata["actor_holds_read"])

    def test_the_marker_is_true_when_the_administrator_can_read_the_field(self):
        RoleFieldAccess.objects.create(
            role=self.admin_role, field=self.bank, can_read=True, can_write=False,
        )
        self._patch([{"field": self.bank.key, "read": True}])
        self.assertTrue(self._rows("FIELD_ACCESS_CHANGED")[0].metadata["actor_holds_read"])

    def test_each_switch_that_moves_gets_its_own_row(self):
        self._patch([{"field": self.phone.key, "write": True}])
        rows = self._rows("FIELD_ACCESS_CHANGED")
        self.assertEqual([row.metadata["switch"] for row in rows], ["read", "write"])
        self.assertEqual({row.severity for row in rows}, {"WARNING"})

    def test_closing_a_normal_field_is_info(self):
        self._patch([{"field": self.name.key, "read": False}])
        rows = self._rows("FIELD_ACCESS_CHANGED")
        self.assertEqual(len(rows), 2)
        self.assertEqual({row.severity for row in rows}, {"INFO"})

    def test_a_change_to_the_state_already_in_force_writes_nothing(self):
        version = self.storekeeper.version
        response = self._patch([{"field": self.name.key, "read": True, "write": True}])
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(self._entries(response)[self.name.key]["source"], "default")
        self.assertFalse(RBACAuditLog.objects.filter(entity_type="RoleFieldAccess").exists())
        self.assertFalse(RoleFieldAccess.objects.exists())
        self.storekeeper.refresh_from_db()
        self.assertEqual(self.storekeeper.version, version)

    def test_a_failed_audit_write_rolls_the_switch_back(self):
        version = self.storekeeper.version
        with mock.patch(
            "vs_rbac.audit.RBACAuditLog.objects.create",
            side_effect=RuntimeError("audit store unavailable"),
        ) as create:
            try:
                response = self._patch([{"field": self.bank.key, "read": True}])
            except RuntimeError:
                response = None
        self.assertTrue(create.called)
        if response is not None:
            self.assertGreaterEqual(response.status_code, 500)
        self.assertFalse(RoleFieldAccess.objects.exists())
        self.storekeeper.refresh_from_db()
        self.assertEqual(self.storekeeper.version, version)

    def test_a_reset_audits_once_and_a_reset_with_no_row_audits_nothing(self):
        self._patch([{"field": self.phone.key, "write": True}])
        self._patch([{"field": self.phone.key, "reset": True}])
        resets = self._rows("FIELD_ACCESS_RESET")
        self.assertEqual(len(resets), 1)
        self.assertEqual(resets[0].severity, "INFO")
        self.assertEqual(resets[0].before_data, {"read": True, "write": True})
        self.assertEqual(resets[0].diff_data, {"read": False, "write": False})

        self._patch([{"field": self.phone.key, "reset": True}])
        self.assertEqual(len(self._rows("FIELD_ACCESS_RESET")), 1)

    def test_exceptions_audit_their_creation_replacement_and_lifting(self):
        created = self._create_exception()
        self.assertEqual(created.status_code, status.HTTP_201_CREATED, created.content)
        log = self._rows("FIELD_OVERRIDE_CREATED")[0]
        self.assertEqual(log.entity_type, "UserFieldAccessOverride")
        self.assertEqual(log.entity_label, f"fa-target@test.com:{self.bank.key}:READ")
        self.assertEqual(log.severity, "WARNING")
        self.assertEqual(log.before_data, {"read": False, "write": False})
        self.assertEqual(log.diff_data, {"read": True, "write": False})
        self.assertEqual(log.metadata["target_user_id"], str(self.target.pk))
        self.assertEqual(log.metadata["field_key"], self.bank.key)
        self.assertEqual(log.metadata["access"], "READ")
        self.assertEqual(log.metadata["mode"], "ALLOW")
        self.assertTrue(log.metadata["sensitive"])
        self.assertFalse(log.metadata["actor_holds_read"])
        self.assertFalse(log.metadata["replaced"])

        replaced = self._create_exception(mode="DENY", reason="Cover ended early.")
        self.assertEqual(replaced.status_code, status.HTTP_201_CREATED, replaced.content)
        lifted = self._rows("FIELD_OVERRIDE_LIFTED")
        self.assertEqual(len(lifted), 1)
        self.assertTrue(lifted[0].metadata["replaced"])
        self.assertEqual(lifted[0].before_data, {"read": True, "write": False})
        self.assertEqual(lifted[0].diff_data, {"read": False, "write": False})
        self.assertEqual(len(self._rows("FIELD_OVERRIDE_CREATED")), 2)

        _client(self.admin).delete(
            _with_tenant(self._exception_url(replaced.json()["data"]["id"]), self.slug),
        )
        self.assertEqual(len(self._rows("FIELD_OVERRIDE_LIFTED")), 2)
        self.assertFalse(self._rows("FIELD_OVERRIDE_LIFTED")[1].metadata["replaced"])

    def test_every_field_access_event_also_reaches_the_central_trail(self):
        from vs_audit.models import AuditEvent

        self._patch([{"field": self.phone.key, "write": True}])
        self._patch([{"field": self.phone.key, "reset": True}])
        created = self._create_exception()
        _client(self.admin).delete(
            _with_tenant(self._exception_url(created.json()["data"]["id"]), self.slug),
        )
        central = set(AuditEvent.objects.values_list("action_type", flat=True))
        self.assertLessEqual(
            {"FIELD_ACCESS_CHANGED", "FIELD_ACCESS_RESET",
             "FIELD_OVERRIDE_CREATED", "FIELD_OVERRIDE_LIFTED"},
            central,
        )


# =============================================================================
# PATCH rules
# =============================================================================
class RoleFieldAccessPatchTests(_FieldAccessApi):
    def test_write_on_stores_read_on(self):
        response = self._patch([{"field": self.phone.key, "write": True}])
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.content)
        entry = self._entries(response)[self.phone.key]
        self.assertEqual((entry["read"], entry["write"], entry["source"]), (True, True, "role"))
        self.assertEqual(self._stored(self.phone), (True, True))

    def test_read_off_stores_write_off(self):
        self._patch([{"field": self.name.key, "write": False}])
        self.assertEqual(self._stored(self.name), (True, False))
        self._patch([{"field": self.phone.key, "write": True}])
        self._patch([{"field": self.phone.key, "read": False}])
        self.assertEqual(self._stored(self.phone), (False, False))

    def test_read_off_with_write_on_is_refused(self):
        response = self._patch([{"field": self.name.key, "read": False, "write": True}])
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertFalse(RoleFieldAccess.objects.exists())

    def test_write_on_a_non_writable_field_is_refused_by_name(self):
        response = self._patch([{"field": self.total.key, "write": True}])
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn(self.total.key, response.content.decode())
        self.assertFalse(RoleFieldAccess.objects.exists())

        closed = self._patch([{"field": self.total.key, "read": False}])
        self.assertEqual(closed.status_code, status.HTTP_200_OK)
        self.assertEqual(self._stored(self.total), (False, False))

    def test_reset_returns_the_field_to_its_default(self):
        self._patch([{"field": self.bank.key, "write": True}])
        response = self._patch([{"field": self.bank.key, "reset": True}])
        entry = self._entries(response)[self.bank.key]
        self.assertEqual(
            (entry["read"], entry["write"], entry["source"]), (False, False, "default"),
        )
        self.assertIsNone(self._stored(self.bank))

    def test_a_row_equal_to_the_default_after_a_change_is_kept(self):
        self._patch([{"field": self.name.key, "write": False}])
        response = self._patch([{"field": self.name.key, "write": True}])
        self.assertEqual(self._entries(response)[self.name.key]["source"], "role")
        self.assertEqual(self._stored(self.name), (True, True))

    def test_the_body_is_checked_before_anything_is_stored(self):
        bodies = {
            "duplicate field": [
                {"field": self.name.key, "read": False},
                {"field": self.name.key, "write": False},
            ],
            "201 changes": [{"field": self.name.key, "read": True}] * 201,
            "no changes": [],
            "no switch": [{"field": self.name.key}],
            "reset with a switch": [{"field": self.name.key, "reset": True, "read": True}],
            "unknown field": [{"field": "faapi.vendor.nope", "read": True}],
        }
        for label, changes in bodies.items():
            with self.subTest(label):
                response = self._patch(changes)
                self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertFalse(RoleFieldAccess.objects.exists())
        self.assertFalse(RBACAuditLog.objects.filter(entity_type="RoleFieldAccess").exists())

    def test_the_role_version_moves_once_per_request_that_changes_something(self):
        version = self.storekeeper.version
        self._patch([
            {"field": self.name.key, "read": False},
            {"field": self.bank.key, "write": True},
        ])
        self.storekeeper.refresh_from_db()
        self.assertEqual(self.storekeeper.version, version + 1)

    def test_the_response_carries_the_fields_named_in_the_request(self):
        response = self._patch([
            {"field": self.name.key, "read": False},
            {"field": self.bank.key, "read": True},
        ])
        data = response.json()["data"]
        self.assertEqual(data["role"], {"key": "storekeeper", "name": "Storekeeper", "branch_name": None})
        self.assertEqual([entry["key"] for entry in data["fields"]], [self.bank.key, self.name.key])

    def test_an_administrator_may_change_a_role_they_hold(self):
        response = self._patch([{"field": self.bank.key, "read": True}], key=self.admin_role.key)
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.content)
        self.assertEqual(self._stored(self.bank, role=self.admin_role), (True, False))

    def test_a_stored_switch_reaches_the_role_holders_next_evaluation(self):
        self._patch([{"field": self.bank.key, "read": True}])
        target = User.objects.get(pk=self.target.pk)
        self.assertTrue(get_field_access(target, tenant=self.tenant).can_read(self.bank.key))


# =============================================================================
# GET
# =============================================================================
class RoleFieldAccessReadTests(_FieldAccessApi):
    def test_the_shape_ordering_and_defaults(self):
        response = self._get()
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.content)
        data = response.json()["data"]
        self.assertEqual(data["role"], {"key": "storekeeper", "name": "Storekeeper", "branch_name": None})
        self.assertEqual(
            [entry["key"] for entry in data["fields"]],
            [self.bank.key, self.phone.key, self.name.key, self.total.key],
        )
        self.assertEqual(data["fields"][0], {
            "key": self.bank.key,
            "name": "bank_account_number",
            "api_names": ["bank_account_number"],
            "label": "Bank account number",
            "module": "faapi",
            "resource": "vendor",
            "group": "Banking",
            "sensitive": True,
            "writable": True,
            "read": False,
            "write": False,
            "source": "default",
            "default": {"read": False, "write": False},
            "set_by_name": None,
            "set_at": None,
        })

    def test_a_stored_switch_says_who_set_it(self):
        self._patch([{"field": self.bank.key, "read": True}])
        entry = self._entries(self._get())[self.bank.key]
        self.assertEqual(entry["source"], "role")
        self.assertIn(entry["set_by_name"], {self.admin.full_name, self.admin.email})
        self.assertIsNotNone(entry["set_at"])

    def test_filters(self):
        def keys(**params):
            return [entry["key"] for entry in self._get(**params).json()["data"]["fields"]]

        self.assertEqual(keys(state="hidden"), [self.bank.key, self.phone.key])
        self.assertEqual(keys(state="read_only"), [self.total.key])
        self.assertEqual(keys(state="full"), [self.name.key])
        self.assertEqual(keys(search="phone"), [self.phone.key])
        self.assertEqual(keys(search="bank account"), [self.bank.key])
        self.assertEqual(len(keys(module="faapi", resource="vendor")), 4)
        self.assertEqual(keys(module="no_such_module"), [])
        self.assertEqual(self._get(state="secret").status_code, status.HTTP_400_BAD_REQUEST)

    def test_a_branch_pinned_role_names_its_branch(self):
        make_role(self.tenant, name="Ikeja Storekeeper", key="ikeja-storekeeper", branch=self.branch)
        response = self._get(key="ikeja-storekeeper")
        self.assertEqual(response.json()["data"]["role"]["branch_name"], "Ikeja Branch")

    def test_the_query_count_does_not_grow_with_fields_or_rows(self):
        self._get()

        def count():
            with CaptureQueriesContext(connection) as queries:
                self.assertEqual(self._get().status_code, status.HTTP_200_OK)
            return len(queries)

        before = count()
        for n in range(6):
            field = make_field_definition(f"faapi.vendor.extra_{n}", f"Extra {n}")
            RoleFieldAccess.objects.create(
                role=self.storekeeper, field=field, can_read=True, can_write=False,
                set_by=self.admin,
            )
        self.assertEqual(count(), before)

    def test_a_school_that_has_not_gone_live_can_shape_field_access(self):
        Tenant.objects.filter(pk=self.tenant.pk).update(status=Tenant.Status.PENDING)
        self.assertEqual(self._get().status_code, status.HTTP_200_OK)
        patched = self._patch([{"field": self.bank.key, "read": True}])
        self.assertEqual(patched.status_code, status.HTTP_200_OK, patched.content)


# =============================================================================
# One-person exceptions
# =============================================================================
class UserFieldAccessOverrideApiTests(_FieldAccessApi):
    def _list(self, **params):
        return _client(self.admin).get(
            self._exceptions_url(), {"tenant": self.slug, **params},
        )

    def test_an_exception_applies_on_the_next_request(self):
        response = self._create_exception()
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.content)
        data = response.json()["data"]
        self.assertEqual(data["field_key"], self.bank.key)
        self.assertEqual(data["field_label"], "Bank account number")
        self.assertEqual((data["access"], data["mode"]), ("READ", "ALLOW"))
        self.assertEqual(data["role_state"], {"read": False, "write": False})
        self.assertFalse(data["is_expired"])
        self.assertIn(data["created_by_name"], {self.admin.full_name, self.admin.email})

        target = User.objects.get(pk=self.target.pk)
        self.assertTrue(get_field_access(target, tenant=self.tenant).can_read(self.bank.key))

    def test_the_list_shape_filters_and_empty_answer(self):
        empty = self._list()
        self.assertEqual(empty.status_code, status.HTTP_200_OK)
        self.assertEqual(empty.json()["data"], [])

        self._create_exception(field=self.name.key, access="READ", mode="ALLOW")
        self._create_exception(field=self.name.key, access="WRITE", mode="DENY")
        rows = self._list().json()["data"]
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["role_state"], {"read": True, "write": True})
        self.assertEqual(len(self._list(access="write").json()["data"]), 1)
        self.assertEqual(len(self._list(mode="ALLOW").json()["data"]), 1)

    def test_a_new_exception_replaces_rather_than_stacks(self):
        self._create_exception(mode="ALLOW")
        self._create_exception(mode="DENY", reason="Withdrawn.")
        rows = UserFieldAccessOverride.objects.filter(user=self.target)
        self.assertEqual(rows.count(), 1)
        self.assertEqual(rows.get().mode, "DENY")

    def test_the_reason_is_required_and_trimmed(self):
        self.assertEqual(
            self._create_exception(reason="   ").status_code, status.HTTP_400_BAD_REQUEST,
        )
        self._create_exception(reason="  Covering leave.  ")
        self.assertEqual(UserFieldAccessOverride.objects.get().reason, "Covering leave.")

    def test_a_past_expiry_is_refused(self):
        response = self._create_exception(
            expires_at=(timezone.now() - timedelta(days=1)).isoformat(),
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_allow_write_on_a_non_writable_field_is_refused_and_deny_write_is_not(self):
        refused = self._create_exception(field=self.total.key, access="WRITE", mode="ALLOW")
        self.assertEqual(refused.status_code, status.HTTP_400_BAD_REQUEST)
        allowed = self._create_exception(field=self.total.key, access="WRITE", mode="DENY")
        self.assertEqual(allowed.status_code, status.HTTP_201_CREATED, allowed.content)

    def test_tenant_and_user_cannot_be_mass_assigned(self):
        other = make_school(slug="fa-mass", name="Mass School")
        victim = make_staff_user(make_branch(other), email="fa-victim@test.com")
        response = self._create_exception(user=victim.pk, tenant=other.tenant.pk)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.content)
        row = UserFieldAccessOverride.objects.get()
        self.assertEqual((row.user_id, row.tenant_id), (self.target.pk, self.tenant.pk))

    def test_lifting_restores_what_the_roles_say(self):
        created = self._create_exception(field=self.name.key, mode="DENY").json()["data"]
        target = User.objects.get(pk=self.target.pk)
        self.assertFalse(get_field_access(target, tenant=self.tenant).can_read(self.name.key))

        response = _client(self.admin).delete(
            _with_tenant(self._exception_url(created["id"]), self.slug),
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        target = User.objects.get(pk=self.target.pk)
        self.assertTrue(get_field_access(target, tenant=self.tenant).can_read(self.name.key))

    def test_a_platform_operator_can_manage_a_school_users_exceptions(self):
        cx = make_vision_user(email="fa-cx-operator@test.com")
        _grant(cx, ["platform.team_overrides.create"])
        response = _client(cx).post(
            _with_tenant(self._exceptions_url(), self.slug),
            {"field": self.name.key, "access": "READ", "mode": "DENY", "reason": "Escalated support."},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.content)
        row = UserFieldAccessOverride.objects.get()
        self.assertEqual((row.tenant_id, row.user_id), (self.tenant.pk, self.target.pk))


# =============================================================================
# Boundaries around who may reach the endpoints
# =============================================================================
class PlatformOperatorOnSchoolRolesTests(_FieldAccessApi):
    """Role field access does not accept a platform operator's cross-tenant assertion."""

    def test_a_platform_operator_asserting_a_school_is_refused_with_404(self):
        cx = make_vision_user(email="fa-cx-roles@test.com")
        _grant(cx, ["platform.field_access.view", "platform.field_access.update"])
        client = _client(cx)
        url = self._role_url()

        read = client.get(url, {"tenant": self.slug})
        changed = client.patch(
            _with_tenant(url, self.slug),
            {"changes": [{"field": self.bank.key, "read": True}]}, format="json",
        )
        self.assertEqual(read.status_code, status.HTTP_404_NOT_FOUND)
        self.assertEqual(changed.status_code, status.HTTP_404_NOT_FOUND)
        self.assertFalse(RoleFieldAccess.objects.exists())


class TwoAdministratorsOnOneRoleTests(_FieldAccessApi):
    """Two administrators saving the same role one after the other."""

    def test_each_save_lands_on_what_the_last_one_stored(self):
        colleague = make_school_admin(self.branch, email="fa-admin-two@test.com")
        _grant(colleague, [VIEW, UPDATE])
        version = self.storekeeper.version

        first = self._patch([{"field": self.phone.key, "read": True}])
        second = self._patch([{"field": self.phone.key, "write": True}], user=colleague)
        self.assertEqual(first.status_code, status.HTTP_200_OK, first.content)
        self.assertEqual(second.status_code, status.HTTP_200_OK, second.content)

        row = RoleFieldAccess.objects.get(role=self.storekeeper, field=self.phone)
        self.assertEqual((row.can_read, row.can_write, row.set_by_id), (True, True, colleague.pk))

        audits = list(
            RBACAuditLog.objects.filter(action_type="FIELD_ACCESS_CHANGED").order_by("id")
        )
        self.assertEqual(
            [(log.metadata["switch"], log.actor_id) for log in audits],
            [("read", self.admin.pk), ("write", colleague.pk)],
        )
        self.assertEqual(audits[1].before_data, {"read": True, "write": False})
        self.storekeeper.refresh_from_db()
        self.assertEqual(self.storekeeper.version, version + 2)


class PendingSchoolExceptionTests(_FieldAccessApi):
    """Field exceptions stay closed before go-live, as permission overrides do."""

    def test_a_school_that_has_not_gone_live_is_refused_every_verb(self):
        existing = UserFieldAccessOverride.objects.create(
            tenant=self.tenant, user=self.target, field=self.name,
            access="READ", mode="DENY", reason="Existing.",
        )
        Tenant.objects.filter(pk=self.tenant.pk).update(status=Tenant.Status.PENDING)
        client = _client(self.admin)

        responses = {
            "GET": client.get(_with_tenant(self._exceptions_url(), self.slug)),
            "POST": self._create_exception(client=client),
            "DELETE": client.delete(_with_tenant(self._exception_url(existing.pk), self.slug)),
            "permission overrides GET": client.get(_with_tenant(
                reverse(
                    "rbac-user-permission-override-list-create",
                    kwargs={"tenant_slug": self.slug, "user_id": self.target.pk},
                ),
                self.slug,
            )),
        }
        for label, response in responses.items():
            with self.subTest(label):
                self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
                self.assertIn("TENANT_NOT_LIVE", response.content.decode())
        self.assertEqual(UserFieldAccessOverride.objects.count(), 1)


# =============================================================================
# Reading exceptions as at an earlier day
# =============================================================================
class FieldExceptionsAsAtTests(_FieldAccessApi):
    """Bright Star's bursar asks what exceptions Ada held on an earlier day.

    Ada's account history starts twenty days ago. An exception granted twelve
    days ago and lifted five days ago must appear on the day between, must be
    gone on the day after, and a day before anything was recorded is refused
    rather than answered with an empty list.
    """

    def setUp(self):
        super().setUp()
        import datetime as dt

        from vs_history.as_at import RECORD_DAY_TIMEZONE, record_today
        from vs_history.models import RecordVersion

        today = record_today()

        def moment(days_ago):
            day = today - dt.timedelta(days=days_ago)
            return dt.datetime(day.year, day.month, day.day, 12, tzinfo=RECORD_DAY_TIMEZONE)

        self.day = lambda days_ago: (today - dt.timedelta(days=days_ago)).isoformat()
        RecordVersion.objects.filter(
            record_type="vs_user.user", record_id=str(self.target.pk),
        ).update(recorded_at=moment(20))
        with mock.patch("vs_history.recorder.timezone.now", return_value=moment(12)):
            created = self._create_exception()
        self.assertEqual(created.status_code, status.HTTP_201_CREATED, created.content)
        with mock.patch("vs_history.recorder.timezone.now", return_value=moment(5)):
            _client(self.admin).delete(
                _with_tenant(self._exception_url(created.json()["data"]["id"]), self.slug),
            )

    def _list(self, as_at, user=None):
        url = _with_tenant(self._exceptions_url(), self.slug)
        return _client(user or self.admin).get(f"{url}&as_at={as_at}")

    def test_the_exception_is_listed_on_a_day_it_was_in_force(self):
        response = self._list(self.day(8))
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.content)
        body = response.json()
        self.assertEqual([row["field_key"] for row in body["data"]], [self.bank.key])
        row = body["data"][0]
        self.assertEqual((row["access"], row["mode"]), ("READ", "ALLOW"))
        self.assertEqual(row["field_label"], self.bank.label)
        self.assertEqual(row["created_by_name"], self.admin.full_name or self.admin.email)
        self.assertIsNone(row["role_state"])
        self.assertFalse(row["is_expired"])
        self.assertEqual(body["as_at"], self.day(8))
        self.assertEqual(body["history_starts"], self.day(12))

    def test_the_exception_is_gone_after_it_was_lifted(self):
        response = self._list(self.day(3))
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.content)
        self.assertEqual(response.json()["data"], [])
        self.assertEqual(len(self._list("").json()["data"]), 0)

    def test_a_day_before_exceptions_were_recorded_is_refused(self):
        response = self._list(self.day(15))
        self.assertEqual(response.status_code, status.HTTP_409_CONFLICT, response.content)
        error = response.json()["error"]
        self.assertEqual(error["code"], "HISTORY_NOT_KEPT")
        self.assertEqual(error["detail"]["history_starts"], self.day(12))

    def test_a_stamped_tracking_start_governs_before_any_row_existed(self):
        import datetime as dt

        from vs_history.models import TrackingStart

        TrackingStart.objects.create(
            record_type="vs_rbac.userfieldaccessoverride",
            started_at=dt.datetime.fromisoformat(f"{self.day(18)}T09:00:00+01:00"),
        )
        response = self._list(self.day(15))
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.content)
        self.assertEqual(response.json()["data"], [])
        self.assertEqual(response.json()["history_starts"], self.day(18))

    def test_a_past_read_keeps_the_view_key(self):
        stranger = make_staff_user(self.branch, email="fa-stranger@test.com")
        response = self._list(self.day(8), user=stranger)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN, response.content)

    def test_another_schools_admin_cannot_read_ada_as_at(self):
        other = make_school(slug="fa-greenfield-asat", name="Greenfield School")
        other_admin = make_school_admin(
            make_branch(other, name="Greenfield Main"), email="fa-gf-admin@test.com",
        )
        url = _with_tenant(self._exceptions_url(slug=other.tenant.slug), other.tenant.slug)
        response = _client(other_admin).get(f"{url}&as_at={self.day(8)}")
        self.assertIn(response.status_code, {status.HTTP_403_FORBIDDEN, status.HTTP_404_NOT_FOUND})

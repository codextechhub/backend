"""Per-branch notification settings: who may set them, and what dispatch obeys.

A tenant's administrators choose which events email people for all of their
branches; a branch admin may switch an email on or off for their own branch
only. The layers resolve branch -> tenant -> platform -> default, and a branch
counts only for events sent with the branch they are about (``branch_scoped``).

Two tenants throughout, because a single-branch test proves nothing about a
multi-branch one: Bright Star runs Ikeja (main) and Lekki, Green Field runs one
branch. Security cases come first: who is refused, and whose rows a request can
reach.
"""
from django.db import IntegrityError, transaction
from django.test import TestCase, tag

from core.migration_testing import RewoundSchemaTestCase
from core.test_utils import TenantAPIClient
from vs_rbac.tests.helpers import (
    make_assignment,
    make_branch,
    make_permission,
    make_role,
    make_role_permission,
    make_school,
    make_school_admin,
    make_staff_user,
    make_vision_user,
)

from .constants import ChannelChoices, NotificationErrorCode, NotificationPermission
from .models import Notification, NotificationEventType, NotificationSetting
from .services.dispatch import NotificationService, UnregisteredRecipient
from .services.seed import seed_notification_templates, seed_platform_settings
from .services.settings import resolve_channels, resolve_settings_bulk

SETTINGS_URL = "/v1/notify/settings/"
UPDATE_URL = "/v1/notify/settings/update/"

#: Branch-scoped: every sender passes the invoice's branch.
FEE_EMAIL = "billing.invoice_issued"
#: Not branch-scoped: a ticket is not about a branch.
TICKET_EMAIL = "ticket.created"
#: Transactional: never configurable at any scope.
RESET_EMAIL = "user.password_reset"

EMAIL = ChannelChoices.EMAIL
IN_APP = ChannelChoices.IN_APP


class _BranchSettingsFixture(TestCase):
    """Two tenants, their admins, and a branch admin pinned to one branch in each."""

    @classmethod
    def setUpTestData(cls):
        seed_notification_templates()
        seed_platform_settings()
        cls.enforce = make_permission(NotificationPermission.ENFORCE_PERMISSIONS)

        # Bright Star: two branches.
        cls.bright = make_school(slug="bright-star-nb", name="Bright Star")
        cls.ikeja = make_branch(cls.bright, name="Ikeja Branch")
        cls.lekki = make_branch(cls.bright, name="Lekki Branch", is_main=False)
        cls.bright_admin = cls._tenant_admin(cls.bright, cls.ikeja, "admin@bright-nb.example.com")
        cls.ikeja_admin = cls._branch_admin(cls.bright, cls.ikeja, "ikeja@bright-nb.example.com")
        cls.teacher = make_staff_user(cls.ikeja, email="teacher@bright-nb.example.com")

        # Green Field: one branch.
        cls.green = make_school(slug="green-field-nb", name="Green Field")
        cls.green_main = make_branch(cls.green, name="Main Branch")
        cls.green_admin = cls._tenant_admin(cls.green, cls.green_main, "admin@green-nb.example.com")
        cls.green_branch_admin = cls._branch_admin(
            cls.green, cls.green_main, "branch@green-nb.example.com",
        )

        cls.operator = make_vision_user(email="operator@codex-nb.example.com", super_admin=True)

    @classmethod
    def _tenant_admin(cls, school, home_branch, email):
        """An administrator whose settings grant covers every branch."""
        role = make_role(school, name="School Admin", key="school_admin")
        make_role_permission(role, cls.enforce)
        user = make_school_admin(home_branch, email=email)
        make_assignment(school, user, role, branch=None)
        return user

    @classmethod
    def _branch_admin(cls, school, branch, email):
        """An administrator whose settings grant is pinned to *branch*."""
        role = make_role(school, name="Branch Admin", key="branch_admin")
        make_role_permission(role, cls.enforce)
        user = make_school_admin(branch, email=email)
        make_assignment(school, user, role, branch=branch)
        return user

    def get(self, user, **query):
        return TenantAPIClient(user=user).get(SETTINGS_URL, query or None)

    def patch(self, user, updates, **query):
        suffix = "".join(f"&{key}={value}" for key, value in query.items())
        return TenantAPIClient(user=user).patch(
            f"{UPDATE_URL}?tenant={user.tenant.slug}{suffix}",
            {"updates": updates}, format="json",
        )

    @staticmethod
    def item(key, channel, is_enabled):
        return {"event_type_key": key, "channel": channel, "is_enabled": is_enabled}

    @staticmethod
    def row(response, key, channel):
        return next(
            r for r in response.data["data"]
            if r["event_type_key"] == key and r["channel"] == channel
        )

    def setting(self, tenant, branch, key, channel):
        return NotificationSetting.all_objects.filter(
            tenant=tenant, branch=branch, event_type__key=key, channel=channel,
        ).first()


class BranchAdminAccessTests(_BranchSettingsFixture):
    """A branch admin reaches their own branch, and nothing wider."""

    def test_a_branch_admin_can_switch_an_email_off_for_their_own_branch(self):
        response = self.patch(
            self.ikeja_admin, [self.item(FEE_EMAIL, EMAIL, False)], branch=self.ikeja.pk,
        )
        self.assertEqual(response.status_code, 200, response.data)
        row = self.setting(self.bright.tenant, self.ikeja, FEE_EMAIL, EMAIL)
        self.assertFalse(row.is_enabled)
        self.assertEqual(response.data["data"], [{
            "event_type_key": FEE_EMAIL,
            "event_type_label": NotificationEventType.objects.get(key=FEE_EMAIL).label,
            "source_module": "vs_billing",
            "source_module_label": "Billing",
            "channel": EMAIL,
            "channel_label": "Email",
            "is_enabled": False,
            "is_transactional": False,
            "source": "branch",
            "source_label": "Branch setting",
            "branch_scoped": True,
            "can_edit": True,
        }])

    def test_another_branch_of_their_own_tenant_is_a_404(self):
        response = self.patch(
            self.ikeja_admin, [self.item(FEE_EMAIL, EMAIL, False)], branch=self.lekki.pk,
        )
        self.assertEqual(response.status_code, 404, response.data)
        self.assertEqual(self.get(self.ikeja_admin, branch=self.lekki.pk).status_code, 404)
        self.assertFalse(NotificationSetting.all_objects.filter(branch=self.lekki).exists())

    def test_another_tenants_branch_is_a_404(self):
        response = self.patch(
            self.ikeja_admin, [self.item(FEE_EMAIL, EMAIL, False)], branch=self.green_main.pk,
        )
        self.assertEqual(response.status_code, 404, response.data)
        self.assertFalse(NotificationSetting.all_objects.filter(branch=self.green_main).exists())

    def test_the_whole_tenant_patch_is_refused_with_a_403(self):
        response = self.patch(self.ikeja_admin, [self.item(FEE_EMAIL, EMAIL, False)])
        self.assertEqual(response.status_code, 403, response.data)
        self.assertEqual(response.data["code"], NotificationErrorCode.BRANCH_SCOPE_REQUIRED)
        self.assertIn("only some branches", response.data["message"])
        self.assertIsNone(self.setting(self.bright.tenant, None, FEE_EMAIL, EMAIL))

    def test_the_whole_tenant_view_is_read_only_for_them(self):
        response = self.get(self.ikeja_admin)
        self.assertEqual(response.status_code, 200, response.data)
        self.assertTrue(response.data["data"])
        self.assertFalse(any(r["can_edit"] for r in response.data["data"]))

    def test_their_own_branch_view_marks_exactly_the_branch_emails_editable(self):
        response = self.get(self.ikeja_admin, branch=self.ikeja.pk)
        self.assertEqual(response.status_code, 200, response.data)
        rows = response.data["data"]
        editable = {(r["event_type_key"], r["channel"]) for r in rows if r["can_edit"]}
        expected = {
            (r["event_type_key"], r["channel"]) for r in rows
            if r["branch_scoped"] and not r["is_transactional"] and r["channel"] == EMAIL
        }
        self.assertTrue(editable)
        self.assertEqual(editable, expected)
        self.assertTrue(self.row(response, FEE_EMAIL, EMAIL)["can_edit"])
        self.assertFalse(self.row(response, FEE_EMAIL, IN_APP)["can_edit"])
        self.assertFalse(self.row(response, TICKET_EMAIL, EMAIL)["can_edit"])
        self.assertFalse(self.row(response, RESET_EMAIL, EMAIL)["can_edit"])

    def test_a_caller_without_the_key_is_refused_at_every_scope(self):
        self.assertEqual(self.get(self.teacher).status_code, 403)
        self.assertEqual(self.get(self.teacher, branch=self.ikeja.pk).status_code, 403)
        response = self.patch(
            self.teacher, [self.item(FEE_EMAIL, EMAIL, False)], branch=self.ikeja.pk,
        )
        self.assertEqual(response.status_code, 403, response.data)

    def test_a_malformed_branch_is_the_same_404(self):
        for ref in ("abc", "99999999999999999999", "-1"):
            self.assertEqual(self.get(self.bright_admin, branch=ref).status_code, 404, ref)


class TenantAdminTests(_BranchSettingsFixture):
    """An administrator with every branch writes both scopes."""

    def test_writes_the_whole_tenant_row(self):
        response = self.patch(self.bright_admin, [self.item(FEE_EMAIL, EMAIL, False)])
        self.assertEqual(response.status_code, 200, response.data)
        self.assertFalse(self.setting(self.bright.tenant, None, FEE_EMAIL, EMAIL).is_enabled)
        row = response.data["data"][0]
        self.assertEqual((row["source"], row["can_edit"]), ("tenant", True))

    def test_writes_any_branch_row_and_null_resets_it(self):
        self.patch(self.bright_admin, [self.item(FEE_EMAIL, EMAIL, False)])
        on = self.patch(
            self.bright_admin, [self.item(FEE_EMAIL, EMAIL, True)], branch=self.lekki.pk,
        )
        self.assertEqual(on.status_code, 200, on.data)
        self.assertEqual(
            (on.data["data"][0]["is_enabled"], on.data["data"][0]["source"]), (True, "branch"),
        )

        reset = self.patch(
            self.bright_admin, [self.item(FEE_EMAIL, EMAIL, None)], branch=self.lekki.pk,
        )
        self.assertEqual(reset.status_code, 200, reset.data)
        self.assertIsNone(self.setting(self.bright.tenant, self.lekki, FEE_EMAIL, EMAIL))
        row = reset.data["data"][0]
        self.assertEqual((row["is_enabled"], row["source"]), (False, "tenant"))

    def test_null_on_a_branch_with_no_row_is_accepted_and_changes_nothing(self):
        response = self.patch(
            self.bright_admin, [self.item(FEE_EMAIL, EMAIL, None)], branch=self.ikeja.pk,
        )
        self.assertEqual(response.status_code, 200, response.data)
        self.assertFalse(NotificationSetting.all_objects.filter(branch=self.ikeja).exists())

    def test_null_without_a_branch_is_refused(self):
        response = self.patch(self.bright_admin, [self.item(FEE_EMAIL, EMAIL, None)])
        self.assertEqual(response.status_code, 400, response.data)
        self.assertEqual(
            response.data["error"]["updates"][0]["error_code"],
            NotificationErrorCode.RESET_NEEDS_BRANCH,
        )

    def test_one_branch_row_does_not_show_at_another_scope(self):
        self.patch(self.bright_admin, [self.item(FEE_EMAIL, EMAIL, False)], branch=self.ikeja.pk)

        whole = self.row(self.get(self.bright_admin), FEE_EMAIL, EMAIL)
        lekki = self.row(self.get(self.bright_admin, branch=self.lekki.pk), FEE_EMAIL, EMAIL)
        ikeja = self.row(self.get(self.bright_admin, branch=self.ikeja.pk), FEE_EMAIL, EMAIL)
        self.assertEqual((whole["is_enabled"], whole["source"]), (True, "platform"))
        self.assertEqual((lekki["is_enabled"], lekki["source"]), (True, "platform"))
        self.assertEqual((ikeja["is_enabled"], ikeja["source"]), (False, "branch"))


class BranchRefusalTests(_BranchSettingsFixture):
    """What a branch may not set, and the all-or-nothing rule around it."""

    def _refusal(self, updates):
        response = self.patch(self.bright_admin, updates, branch=self.ikeja.pk)
        self.assertEqual(response.status_code, 400, response.data)
        return response.data["error"]["updates"]

    def test_an_event_sent_without_a_branch_is_not_configurable_per_branch(self):
        errors = self._refusal([self.item(TICKET_EMAIL, EMAIL, False)])
        self.assertEqual(errors, [{
            "index": 0,
            "error_code": NotificationErrorCode.BRANCH_NOT_CONFIGURABLE,
            "message": (
                f"'{TICKET_EMAIL}' is not sent for a particular branch, so it can "
                "only be set for all branches together."
            ),
        }])

    def test_its_row_at_branch_scope_says_so(self):
        row = self.row(self.get(self.bright_admin, branch=self.ikeja.pk), TICKET_EMAIL, EMAIL)
        self.assertEqual((row["branch_scoped"], row["can_edit"]), (False, False))

    def test_transactional_events_are_refused_at_branch_scope(self):
        errors = self._refusal([self.item(RESET_EMAIL, EMAIL, False)])
        self.assertEqual(
            errors[0]["error_code"], NotificationErrorCode.TRANSACTIONAL_NOT_CONFIGURABLE,
        )

    def test_in_app_cannot_be_switched_off_at_branch_scope(self):
        errors = self._refusal([self.item(FEE_EMAIL, IN_APP, False)])
        self.assertEqual(errors[0]["error_code"], NotificationErrorCode.IN_APP_ALWAYS_ENABLED)

    def test_one_refused_item_writes_nothing(self):
        errors = self._refusal([
            self.item(FEE_EMAIL, EMAIL, False),
            self.item(TICKET_EMAIL, EMAIL, False),
        ])
        self.assertEqual([e["index"] for e in errors], [1])
        self.assertFalse(NotificationSetting.all_objects.filter(branch=self.ikeja).exists())

    def test_the_platform_layer_has_no_branches(self):
        response = self.patch(
            self.operator, [self.item(FEE_EMAIL, EMAIL, False)], branch=self.ikeja.pk,
        )
        self.assertEqual(response.status_code, 404, response.data)
        self.assertEqual(self.get(self.operator, branch=self.ikeja.pk).status_code, 404)


class TenantIsolationTests(_BranchSettingsFixture):
    """One tenant's choices never reach another's, whatever the shape."""

    def test_a_tenant_admin_cannot_name_another_tenants_branch(self):
        self.assertEqual(self.get(self.green_admin, branch=self.ikeja.pk).status_code, 404)
        response = self.patch(
            self.green_admin, [self.item(FEE_EMAIL, EMAIL, False)], branch=self.ikeja.pk,
        )
        self.assertEqual(response.status_code, 404, response.data)

    def test_a_branch_write_moves_nothing_in_another_tenant(self):
        self.patch(self.bright_admin, [self.item(FEE_EMAIL, EMAIL, False)], branch=self.ikeja.pk)
        et = NotificationEventType.objects.get(key=FEE_EMAIL)
        self.assertTrue(
            resolve_channels(et, tenant=self.green.tenant, branch=self.green_main)[EMAIL],
        )

    def test_a_single_branch_tenant_admin_writes_both_scopes(self):
        whole = self.patch(self.green_admin, [self.item(FEE_EMAIL, EMAIL, False)])
        self.assertEqual(whole.status_code, 200, whole.data)
        branch = self.patch(
            self.green_admin, [self.item(FEE_EMAIL, EMAIL, True)], branch=self.green_main.pk,
        )
        self.assertEqual(branch.status_code, 200, branch.data)
        self.assertEqual(branch.data["data"][0]["source"], "branch")

    def test_a_single_branch_tenants_branch_admin_writes_both_scopes(self):
        """Pinned to the only branch, the tenant scope reaches nobody else.

        Green Field has one branch, so a tenant-wide switch changes only what
        its branch admin already covers. The same grant at Bright Star, where a
        second branch exists, is refused the tenant scope
        (``test_the_whole_tenant_patch_is_refused_with_a_403``).
        """
        whole = self.patch(self.green_branch_admin, [self.item(FEE_EMAIL, EMAIL, False)])
        self.assertEqual(whole.status_code, 200, whole.data)
        self.assertFalse(self.setting(self.green.tenant, None, FEE_EMAIL, EMAIL).is_enabled)
        branch = self.patch(
            self.green_branch_admin, [self.item(FEE_EMAIL, EMAIL, True)],
            branch=self.green_main.pk,
        )
        self.assertEqual(branch.status_code, 200, branch.data)

    def test_a_single_branch_tenants_branch_admin_is_narrowed_once_a_second_opens(self):
        """The rule reads the branch count at the request, not at the grant."""
        make_branch(self.green, name="Ajah Branch", is_main=False)
        refused = self.patch(self.green_branch_admin, [self.item(FEE_EMAIL, EMAIL, False)])
        self.assertEqual(refused.status_code, 403, refused.data)
        self.assertEqual(refused.data["code"], NotificationErrorCode.BRANCH_SCOPE_REQUIRED)
        self.assertIsNone(self.setting(self.green.tenant, None, FEE_EMAIL, EMAIL))


class BranchSettingAuditTests(_BranchSettingsFixture):
    """A branch's change is audited as that branch's, never as the tenant's."""

    def _events(self):
        from vs_audit.models import AuditEvent

        return AuditEvent.objects.filter(entity_type="NotificationSetting").order_by("event_at")

    def test_a_branch_change_and_its_reset_name_the_branch(self):
        self.patch(self.ikeja_admin, [self.item(FEE_EMAIL, EMAIL, False)], branch=self.ikeja.pk)
        self.patch(self.ikeja_admin, [self.item(FEE_EMAIL, EMAIL, None)], branch=self.ikeja.pk)

        switched, reset = list(self._events())
        for event in (switched, reset):
            self.assertEqual(event.tenant_id, self.bright.tenant_id)
            self.assertEqual(event.actor_user, self.ikeja_admin)
            self.assertEqual(event.entity_id, f"{FEE_EMAIL}:{EMAIL}:branch-{self.ikeja.pk}")
            self.assertEqual(event.metadata["layer"], "branch")
            self.assertEqual(event.metadata["branch_id"], self.ikeja.pk)
            self.assertIn("Ikeja Branch", event.summary)
        self.assertEqual(switched.diff_data, {"is_enabled": {"before": None, "after": False}})
        self.assertEqual(reset.diff_data, {"is_enabled": {"before": False, "after": None}})
        self.assertTrue(reset.metadata["removed"])
        self.assertIn("reset to inherit", reset.summary)


class BranchResolutionTests(_BranchSettingsFixture):
    """The resolver: which rows count for which scope."""

    def _row(self, key, branch, is_enabled, tenant=None):
        return NotificationSetting.all_objects.create(
            tenant=tenant or self.bright.tenant, branch=branch,
            event_type=NotificationEventType.objects.get(key=key),
            channel=EMAIL, is_enabled=is_enabled,
        )

    def test_the_branch_row_wins_only_for_its_own_branch(self):
        self._row(FEE_EMAIL, None, True)
        self._row(FEE_EMAIL, self.ikeja, False)
        et = NotificationEventType.objects.get(key=FEE_EMAIL)
        tenant = self.bright.tenant
        self.assertFalse(resolve_channels(et, tenant=tenant, branch=self.ikeja)[EMAIL])
        self.assertTrue(resolve_channels(et, tenant=tenant, branch=self.lekki)[EMAIL])
        self.assertTrue(resolve_channels(et, tenant=tenant)[EMAIL])

    def test_a_branch_row_for_an_event_that_is_not_branch_scoped_is_ignored(self):
        self._row(TICKET_EMAIL, self.ikeja, False)
        et = NotificationEventType.objects.get(key=TICKET_EMAIL)
        resolved = resolve_settings_bulk([et], tenant=self.bright.tenant, branch=self.ikeja)
        self.assertEqual(resolved[et.id][EMAIL], (True, "platform"))

    def test_another_tenants_branch_speaks_for_nobody(self):
        self._row(FEE_EMAIL, self.green_main, False, tenant=self.green.tenant)
        et = NotificationEventType.objects.get(key=FEE_EMAIL)
        resolved = resolve_settings_bulk([et], tenant=self.bright.tenant, branch=self.green_main)
        self.assertEqual(resolved[et.id][EMAIL], (True, "platform"))

    def test_the_database_holds_one_row_per_branch_and_needs_a_tenant(self):
        self._row(FEE_EMAIL, self.ikeja, False)
        with self.assertRaises(IntegrityError), transaction.atomic():
            self._row(FEE_EMAIL, self.ikeja, True)
        with self.assertRaises(IntegrityError), transaction.atomic():
            NotificationSetting.all_objects.create(
                tenant=None, branch=self.ikeja,
                event_type=NotificationEventType.objects.get(key=FEE_EMAIL),
                channel=EMAIL, is_enabled=True,
            )

    def test_the_seed_restores_the_flags_from_the_registry(self):
        from .constants import EVENT_TYPE_REGISTRY
        from .services.seed import seed_event_types

        NotificationEventType.objects.update(branch_scoped=False)
        NotificationEventType.objects.filter(key=TICKET_EMAIL).update(branch_scoped=True)
        seed_event_types()
        expected = {e["key"] for e in EVENT_TYPE_REGISTRY if e.get("branch_scoped")}
        self.assertEqual(
            set(NotificationEventType.objects.filter(branch_scoped=True).values_list("key", flat=True)),
            expected,
        )
        self.assertIn(FEE_EMAIL, expected)

    def test_the_matrix_still_costs_two_queries_at_branch_scope(self):
        from .views import NotificationSettingViewSet

        with self.assertNumQueries(2):
            NotificationSettingViewSet()._build_matrix(self.bright.tenant, self.ikeja)


class BranchDispatchTests(_BranchSettingsFixture):
    """A branch_scoped send obeys the branch it names, and only that branch."""

    def _send(self, branch, recipients=(), key=FEE_EMAIL):
        with self.captureOnCommitCallbacks(execute=False):
            return NotificationService.send(
                event_key=key,
                context={"customer_name": "Tunde", "invoice_number": "INV-1"},
                recipients=list(recipients),
                unregistered_recipients=[] if recipients else [
                    UnregisteredRecipient(email="payer@example.com", name="Tunde's mother"),
                ],
                tenant=self.bright.tenant,
                branch=branch,
            )

    def _emails(self, ids):
        return Notification.all_objects.filter(pk__in=ids, channel=EMAIL).count()

    def test_ikeja_off_silences_ikeja_alone(self):
        NotificationSetting.all_objects.create(
            tenant=self.bright.tenant, branch=None,
            event_type=NotificationEventType.objects.get(key=FEE_EMAIL),
            channel=EMAIL, is_enabled=True,
        )
        self.patch(self.ikeja_admin, [self.item(FEE_EMAIL, EMAIL, False)], branch=self.ikeja.pk)

        self.assertEqual(self._emails(self._send(self.ikeja)), 0)
        self.assertEqual(self._emails(self._send(self.lekki)), 1)
        self.assertEqual(self._emails(self._send(None)), 1)

    def test_a_branch_can_keep_an_email_the_tenant_switched_off(self):
        self.patch(self.bright_admin, [self.item(FEE_EMAIL, EMAIL, False)])
        self.patch(self.bright_admin, [self.item(FEE_EMAIL, EMAIL, True)], branch=self.lekki.pk)

        self.assertEqual(self._emails(self._send(self.lekki)), 1)
        self.assertEqual(self._emails(self._send(self.ikeja)), 0)
        self.assertEqual(self._emails(self._send(None)), 0)

    def test_the_branch_never_silences_a_recipient_in_another_tenant(self):
        """Platform staff read their own settings, whatever Ikeja chose."""
        self.patch(
            self.bright_admin, [self.item("workflow.stage_activated", EMAIL, False)],
            branch=self.ikeja.pk,
        )
        ids = self._send(self.ikeja, recipients=[self.operator], key="workflow.stage_activated")
        self.assertEqual(self._emails(ids), 1)
        self.assertEqual(
            Notification.all_objects.get(pk__in=ids, channel=EMAIL).tenant_id,
            self.operator.tenant_id,
        )


@tag("slow")
class BranchSettingsMigrationTests(RewoundSchemaTestCase):
    """0019 adds the branch layer without disturbing rows, and reverses cleanly."""

    APP = "vs_notifications"
    BEFORE = "0018_in_app_notification_subjects"
    AFTER = "0019_branch_notification_settings"

    def setUp(self):
        self.school = make_school(slug="migration-nb", name="Migration School")
        self.branch = make_branch(self.school, name="Main Branch")

    def _models(self, target):
        apps = self.historical_apps(target)
        return (
            apps.get_model("vs_notifications", "NotificationEventType"),
            apps.get_model("vs_notifications", "NotificationSetting"),
        )

    def test_forward_keeps_tenant_rows_and_flags_the_registry_events(self):
        EventType, Setting = self._models(self.BEFORE)
        Setting.objects.create(
            tenant_id=self.school.tenant_id,
            event_type=EventType.objects.get(key=FEE_EMAIL),
            channel=EMAIL, is_enabled=False,
        )

        self.migrate_to(self.AFTER)
        EventType, Setting = self._models(self.AFTER)
        row = Setting.objects.get(tenant_id=self.school.tenant_id)
        self.assertIsNone(row.branch_id)
        self.assertFalse(row.is_enabled)
        self.assertTrue(EventType.objects.get(key=FEE_EMAIL).branch_scoped)
        self.assertFalse(EventType.objects.get(key=TICKET_EMAIL).branch_scoped)

    def test_reverse_drops_branch_rows_and_keeps_tenant_rows(self):
        self.migrate_to(self.AFTER)
        EventType, Setting = self._models(self.AFTER)
        et = EventType.objects.get(key=FEE_EMAIL)
        Setting.objects.create(
            tenant_id=self.school.tenant_id, branch_id=None,
            event_type=et, channel=EMAIL, is_enabled=True,
        )
        Setting.objects.create(
            tenant_id=self.school.tenant_id, branch_id=self.branch.pk,
            event_type=et, channel=EMAIL, is_enabled=False,
        )

        self.migrate_to(self.BEFORE)
        EventType, Setting = self._models(self.BEFORE)
        rows = list(Setting.objects.filter(tenant_id=self.school.tenant_id))
        self.assertEqual([r.is_enabled for r in rows], [True])
        with self.assertRaises(IntegrityError), transaction.atomic():
            Setting.objects.create(
                tenant_id=self.school.tenant_id, event_type_id=et.pk,
                channel=EMAIL, is_enabled=False,
            )

"""Entitlement windows honour the subscription date switches of the school they belong to.

A grant a school's plan wrote (``source=PACKAGE``) starts and ends with the
school's subscription, which are the registered
``platform.schools.subscription_starts_at`` and
``platform.schools.subscription_expires_at``. A grant written by hand carries a
window of its own. CodeX staff read both through the entitlement list, the
renewal calendar and the answers to a schedule; a staff role that may not read
the subscription dates is served the plan-written windows without them and the
hand-written ones whole.
"""
from datetime import timedelta

from django.test import TestCase
from django.utils import timezone

from core.test_utils import TenantAPIClient
from vs_config.models import Capability, CapabilityEntitlement
from vs_config.services.capabilities import set_entitlement
from vs_rbac.tests.helpers import (
    install_declared_fields,
    make_branch,
    make_permission,
    make_platform_assignment,
    make_platform_role,
    make_platform_role_permission,
    make_school,
    make_school_admin,
    make_vision_user,
    set_field_access,
)

STARTS = "platform.schools.subscription_starts_at"
EXPIRES = "platform.schools.subscription_expires_at"
PACKAGE = CapabilityEntitlement.Source.PACKAGE
MANUAL = CapabilityEntitlement.Source.MANUAL
GRANTED = CapabilityEntitlement.State.GRANTED
KEYS = ("config.entitlement.view", "config.entitlement.update")


class _EntitlementAccess(TestCase):
    """Corona has a plan-written grant (starting soon) and a hand-written one; Sunrise has a plan grant."""

    @classmethod
    def setUpTestData(cls):
        install_declared_fields("platform.schools")
        install_declared_fields("platform.entitlements")
        cls.corona = make_school(slug="window-corona")
        cls.sunrise = make_school(slug="window-sunrise")
        cls.plan = Capability.objects.create(
            key="window-plan", label="Plan module", requires_entitlement=True)
        cls.trial = Capability.objects.create(
            key="window-trial", label="Trial module", requires_entitlement=True)
        cls.admin = make_vision_user(email="window-admin@codex.test", super_admin=True)
        now = timezone.now()
        cls.starts = now + timedelta(days=10)
        cls.package_ends = now + timedelta(days=200)
        cls.plan_row = set_entitlement(
            capability=cls.plan, tenant=cls.corona.tenant, state=GRANTED, source=PACKAGE,
            actor=cls.admin, starts_at=cls.starts, ends_at=cls.package_ends,
        )
        cls.trial_row = set_entitlement(
            capability=cls.trial, tenant=cls.corona.tenant, state=GRANTED, source=MANUAL,
            actor=cls.admin, starts_at=now - timedelta(days=1), ends_at=now + timedelta(days=5),
        )
        cls.sunrise_row = set_entitlement(
            capability=cls.plan, tenant=cls.sunrise.tenant, state=GRANTED, source=PACKAGE,
            actor=cls.admin, ends_at=now + timedelta(days=20),
        )
        cls.open = cls.operator("open")
        cls.no_expiry = cls.operator("no-expiry", EXPIRES)
        cls.no_start = cls.operator("no-start", STARTS)
        cls.no_dates = cls.operator("no-dates", STARTS, EXPIRES)
        cls.no_window = cls.operator("no-window", "platform.entitlements.window")
        cls.no_calendar = cls.operator("no-calendar", "platform.entitlements.calendar")

    @classmethod
    def operator(cls, label, *closed):
        """CodeX staff holding the entitlement keys, with the named subscription switches off."""
        user = make_vision_user(email=f"window-{label}@codex.test")
        role = make_platform_role(name=f"Window {label}")
        for key in KEYS:
            make_platform_role_permission(role, make_permission(key))
        make_platform_assignment(user, role)
        if closed:
            set_field_access(role, *closed, read=False, write=False)
        return user

    def api(self, user):
        return TenantAPIClient(user=user)

    def listed(self, user, school=None):
        school = school or self.corona
        response = self.api(user).get(f"/v1/config/entitlements/?tenant={school.slug}")
        self.assertEqual(response.status_code, 200, response.data)
        return {row["capability_key"]: row for row in response.data["data"]}

    def calendar(self, user, window_days=366):
        response = self.api(user).get(
            f"/v1/config/entitlements/calendar/?all_tenants=true&window_days={window_days}")
        self.assertEqual(response.status_code, 200, response.data)
        return response.data["data"]


class EntitlementListAccessTests(_EntitlementAccess):

    def test_hidden_window_removes_both_dates_from_manual_and_plan_grants(self):
        rows = self.listed(self.no_window)
        for key in ("window-plan", "window-trial"):
            self.assertNotIn("starts_at", rows[key])
            self.assertNotIn("ends_at", rows[key])

    def test_window_write_is_refused_when_closed(self):
        response = self.api(self.no_window).post(
            f"/v1/config/entitlements/?tenant={self.corona.slug}",
            {
                "capability": "window-plan", "state": GRANTED, "source": MANUAL,
                "starts_at": timezone.now().isoformat(),
            }, format="json",
        )
        self.assertEqual(response.status_code, 403, response.data)

    def test_hidden_calendar_is_refused(self):
        response = self.api(self.no_calendar).get("/v1/config/entitlements/calendar/")
        self.assertEqual(response.status_code, 403, response.data)

    def test_an_open_role_reads_every_window(self):
        rows = self.listed(self.open)

        for key in ("window-plan", "window-trial"):
            self.assertIn("starts_at", rows[key])
            self.assertIn("ends_at", rows[key])

    def test_a_hidden_expiry_removes_the_end_of_a_plan_written_window_only(self):
        rows = self.listed(self.no_expiry)

        self.assertNotIn("ends_at", rows["window-plan"])
        self.assertIn("starts_at", rows["window-plan"])
        self.assertIn("ends_at", rows["window-trial"])
        self.assertEqual(rows["window-plan"]["source"], "PACKAGE")

    def test_a_hidden_start_removes_the_start_of_a_plan_written_window_only(self):
        rows = self.listed(self.no_start)

        self.assertNotIn("starts_at", rows["window-plan"])
        self.assertIn("ends_at", rows["window-plan"])
        self.assertIn("starts_at", rows["window-trial"])

    def test_the_answer_to_a_schedule_follows_the_same_switches(self):
        response = self.api(self.no_dates).post(
            "/v1/config/entitlements/bulk-schedule/",
            {
                "items": [{"capability": "window-plan", "tenant": self.corona.slug}],
                "ends_at": (timezone.now() + timedelta(days=300)).isoformat(),
                "reason": "Renew the plan",
            },
            format="json",
        )

        self.assertEqual(response.status_code, 200, response.data)
        (row,) = response.data["data"]
        self.assertNotIn("starts_at", row)
        self.assertNotIn("ends_at", row)
        self.assertEqual(row["capability_key"], "window-plan")

    def test_the_answer_to_a_grant_follows_the_same_switches(self):
        extra = Capability.objects.create(
            key="window-extra", label="Extra module", requires_entitlement=True)
        ends = timezone.now() + timedelta(days=60)

        response = self.api(self.no_expiry).post(
            f"/v1/config/entitlements/?tenant={self.sunrise.slug}",
            {"capability": extra.key, "state": "GRANTED", "source": "PACKAGE",
             "ends_at": ends.isoformat(), "reason": "Plan grant"},
            format="json",
        )

        self.assertEqual(response.status_code, 201, response.data)
        self.assertNotIn("ends_at", response.data["data"])
        self.assertEqual(
            CapabilityEntitlement.all_objects.get(
                capability=extra, tenant=self.sunrise.tenant).ends_at.replace(microsecond=0),
            ends.replace(microsecond=0),
        )

    def test_a_hand_written_grant_answer_is_whole_for_a_role_that_may_not_read_the_dates(self):
        extra = Capability.objects.create(
            key="window-hand", label="Hand module", requires_entitlement=True)
        response = self.api(self.no_dates).post(
            f"/v1/config/entitlements/?tenant={self.sunrise.slug}",
            {"capability": extra.key, "state": "GRANTED", "source": "MANUAL",
             "ends_at": (timezone.now() + timedelta(days=60)).isoformat(),
             "reason": "Trial"},
            format="json",
        )

        self.assertEqual(response.status_code, 201, response.data)
        self.assertIn("ends_at", response.data["data"])

    def test_a_school_role_is_not_served_the_platforms_entitlements(self):
        branch = make_branch(self.corona)
        school_admin = make_school_admin(branch, email="window-school@corona.test")

        response = self.api(school_admin).get(f"/v1/config/entitlements/?tenant={self.sunrise.slug}")

        self.assertIn(response.status_code, (403, 404))


class EntitlementCalendarAccessTests(_EntitlementAccess):

    def entries(self, user, window_days=366):
        data = self.calendar(user, window_days)
        return {(row["tenant_slug"], row["capability"]): row for row in data["entries"]}

    def test_an_open_role_reads_every_row_and_count(self):
        data = self.calendar(self.open)
        entries = self.entries(self.open)

        self.assertEqual(
            set(entries),
            {("window-corona", "window-plan"), ("window-corona", "window-trial"),
             ("window-sunrise", "window-plan")},
        )
        self.assertEqual(data["summary"]["expiring_7_days"], 1)
        self.assertEqual(data["summary"]["expiring_30_days"], 2)
        self.assertEqual(data["summary"]["scheduled"], 1)
        for row in entries.values():
            self.assertIn("days_until_expiry", row)

    def test_a_hidden_expiry_drops_plan_written_rows_that_the_expiry_placed_on_the_calendar(self):
        data = self.calendar(self.no_expiry)
        entries = self.entries(self.no_expiry)

        self.assertNotIn(("window-sunrise", "window-plan"), entries)
        self.assertIn(("window-corona", "window-trial"), entries)
        self.assertEqual(data["summary"]["expiring_30_days"], 1)
        self.assertEqual(data["summary"]["expiring_7_days"], 1)

    def test_a_plan_written_row_placed_by_its_start_loses_its_expiry_columns(self):
        entry = self.entries(self.no_expiry)[("window-corona", "window-plan")]

        self.assertIn("starts_at", entry)
        self.assertNotIn("ends_at", entry)
        self.assertNotIn("days_until_expiry", entry)
        self.assertEqual(entry["status"], "scheduled")

    def test_a_hand_written_row_stays_whole(self):
        entry = self.entries(self.no_dates)[("window-corona", "window-trial")]

        for key in ("starts_at", "ends_at", "days_until_expiry"):
            self.assertIn(key, entry)

    def test_a_hidden_start_removes_the_scheduled_count_and_the_scheduled_row(self):
        self.assertIn(("window-corona", "window-plan"), self.entries(self.open, 90))

        data = self.calendar(self.no_start, 90)
        entries = self.entries(self.no_start, 90)

        self.assertEqual(data["summary"]["scheduled"], 0)
        self.assertNotIn(("window-corona", "window-plan"), entries)
        self.assertIn(("window-sunrise", "window-plan"), entries)

    def test_a_row_placed_by_its_expiry_loses_its_start_when_the_start_is_hidden(self):
        entry = self.entries(self.no_start)[("window-corona", "window-plan")]

        self.assertNotIn("starts_at", entry)
        self.assertIn("ends_at", entry)
        self.assertNotEqual(entry["status"], "scheduled")

    def test_a_role_that_may_read_neither_date_reads_only_hand_written_rows(self):
        entries = self.entries(self.no_dates)

        self.assertEqual(set(entries), {("window-corona", "window-trial")})

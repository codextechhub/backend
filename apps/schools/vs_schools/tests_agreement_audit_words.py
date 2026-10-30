"""Historical agreement snapshots obey the reader's field decisions.

The central configuration mirror carries action and target only. Older rows
can contain before and after snapshots of a subscription's start, expiry and
plan depth. Their nested metadata is filtered on read. These tests attach the
authoritative local snapshots to an in-memory central row to exercise that
legacy reader path without storing a new value-bearing central event.

Bright Star has two branches and an uplift on Finance; Sunrise has one. The
school's own audit officer holds no CodeX switch at all, so an agreement is
cut from their view of their own school's trail too.
"""
from datetime import date, timedelta

from django.core.management import call_command
from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIRequestFactory

from vs_audit import protected_words
from vs_audit.models import AuditEvent
from vs_audit.serializers import AuditEventDetailSerializer
from vs_config.models import Capability, CapabilityEntitlement, ConfigurationAuditEvent
from vs_config.services.capabilities import (
    clear_depth_grant,
    clear_entitlement,
    set_depth_grant,
)
from vs_rbac.tests.helpers import (
    install_declared_fields,
    make_assignment,
    make_branch,
    make_permission,
    make_platform_assignment,
    make_platform_role,
    make_platform_role_permission,
    make_role,
    make_school,
    make_school_admin,
    make_vision_user,
    platform_tenant,
    set_field_access,
)

from .models import PackagePlan, SchoolPackageSetup
from .services.packages import apply_plan_entitlements, change_plan

RESOURCE = "platform.schools"
FIELDS = (
    "subscription_starts_at", "subscription_expires_at", "package_plan", "uplift",
)
RATE = 351_234
NOTES = "Agreed on a call with the proprietor at the Ikeja office."


def key(name):
    return f"{RESOURCE}.{name}"


def make_operator(email, *, closed=()):
    user = make_vision_user(email=email)
    role = make_platform_role(name=f"Account manager {email}")
    make_platform_role_permission(role, make_permission("platform.schools.view"))
    make_platform_assignment(user, role)
    if closed:
        set_field_access(role, *(key(n) for n in closed), read=False, write=False)
    return user


def provision(slug, branch_names):
    school = make_school(slug=slug, name=slug.replace("-", " ").title())
    for index, name in enumerate(branch_names):
        make_branch(school, name=name, is_main=index == 0)
    plan = PackagePlan.objects.get(code="basic")
    SchoolPackageSetup.objects.create(
        school=school, package_plan=plan,
        subscription_starts_at=date.today(),
        subscription_expires_at=date.today() + timedelta(days=365),
        agreed_price_per_student=RATE, minimum_billable_students=25, notes=NOTES,
    )
    apply_plan_entitlements(
        school=school, plan=plan,
        expires_at=date.today() + timedelta(days=365), actor=None,
    )
    return school


class _Fixture(TestCase):
    @classmethod
    def setUpTestData(cls):
        call_command("seed_actions", verbosity=0)
        call_command("seed_prebuilt_role_templates", verbosity=0)
        call_command("seed_config_catalogue", verbosity=0)
        call_command("seed_package", verbosity=0)
        install_declared_fields(RESOURCE)

        cls.finance = Capability.objects.get(key="finance")
        cls.bright = provision("bright-star-agreement", ("Ikeja Branch", "Lekki Branch"))
        cls.sunrise = provision("sunrise-agreement", ("Main Branch",))
        cls.ends = timezone.now() + timedelta(days=90)
        set_depth_grant(
            capability=cls.finance, tenant=cls.bright.tenant,
            depth=Capability.Depth.ADVANCED, actor=None,
            reason="Signed on the promise of payroll.", ends_at=cls.ends,
        )

        cls.ngozi = make_operator("ngozi@agreement.test")
        cls.without = {
            name: make_operator(f"without-{name}@agreement.test", closed=(name,))
            for name in FIELDS
        }
        role = make_role(cls.bright, name="Audit officer", key="audit_officer")
        cls.officer = make_school_admin(
            None, email="audit@bright-star-agreement.test", tenant=cls.bright.tenant,
        )
        make_assignment(cls.bright, cls.officer, role, branch=None)

    def render(self, event, user):
        if "before" not in event.metadata and "after" not in event.metadata:
            local = ConfigurationAuditEvent.objects.filter(
                action=event.metadata["config_action"],
                target_type=event.entity_type,
                target_id=event.entity_id,
                tenant=event.tenant,
            ).latest("created_at")
            event.metadata = {
                **event.metadata,
                "before": local.before_data,
                "after": local.after_data,
            }
        request = APIRequestFactory().get("/")
        request.user = user
        request.tenant = user.tenant
        return AuditEventDetailSerializer(event, context={"request": request}).data

    def latest(self, entity_type, tenant, action):
        return AuditEvent.objects.filter(
            entity_type=entity_type, tenant=tenant,
            metadata__config_action=action,
        ).latest("event_at")


class EntitlementEventTests(_Fixture):
    """A grant's dates and depth, as the old and new state of the grant."""

    def event(self, school=None):
        return self.latest(
            "CapabilityEntitlement", (school or self.bright).tenant,
            "config.entitlement.updated",
        )

    def test_a_reader_of_every_field_sees_the_states_as_written(self):
        shown = self.render(self.event(), self.ngozi)["metadata"]
        self.assertTrue({"starts_at", "ends_at", "depth"} <= set(shown["after"]), shown)

    def test_each_switch_cuts_only_its_own_value(self):
        for school in (self.bright, self.sunrise):
            event = self.event(school)
            for name, gone in (
                ("subscription_starts_at", "starts_at"),
                ("subscription_expires_at", "ends_at"),
                ("package_plan", "depth"),
            ):
                with self.subTest(school=school.slug, switch=name):
                    shown = self.render(event, self.without[name])["metadata"]
                    for state in ("before", "after"):
                        self.assertNotIn(gone, shown[state])
                    self.assertEqual(shown["after"]["state"], "GRANTED")
                    self.assertIn("source", shown["after"])
                    kept = {"starts_at", "ends_at", "depth"} - {gone}
                    self.assertTrue(kept <= set(shown["after"]), shown)

    def test_an_uplift_switch_does_not_cut_an_entitlement(self):
        shown = self.render(self.event(), self.without["uplift"])["metadata"]
        self.assertTrue({"starts_at", "ends_at", "depth"} <= set(shown["after"]))

    def test_the_schools_own_officer_reads_none_of_the_agreement(self):
        shown = self.render(self.event(), self.officer)["metadata"]
        for gone in ("starts_at", "ends_at", "depth"):
            self.assertNotIn(gone, shown["after"])
        self.assertEqual(shown["after"]["state"], "GRANTED")

    def test_the_stored_row_is_untouched(self):
        event = self.event()
        self.render(event, self.officer)
        event.refresh_from_db()
        self.assertNotIn("after", event.metadata)


class UpliftEventTests(_Fixture):
    def test_an_uplift_is_cut_whole_for_a_reader_without_its_switch(self):
        event = self.latest(
            "CapabilityDepthGrant", self.bright.tenant, "config.depth_grant.updated",
        )
        open_view = self.render(event, self.ngozi)["metadata"]
        self.assertEqual(open_view["after"]["depth"], Capability.Depth.ADVANCED)
        closed = self.render(event, self.without["uplift"])["metadata"]
        self.assertNotIn("before", closed)
        self.assertNotIn("after", closed)
        self.assertEqual(closed["config_action"], "config.depth_grant.updated")

    def test_the_plan_and_date_switches_leave_an_uplift_alone(self):
        event = self.latest(
            "CapabilityDepthGrant", self.bright.tenant, "config.depth_grant.updated",
        )
        for name in ("package_plan", "subscription_expires_at", "subscription_starts_at"):
            shown = self.render(event, self.without[name])["metadata"]
            self.assertIn("ends_at", shown["after"])

    def test_withdrawing_an_uplift_is_cut_the_same_way(self):
        set_depth_grant(
            capability=self.finance, tenant=self.sunrise.tenant,
            depth=Capability.Depth.ADVANCED, actor=None, ends_at=self.ends,
        )
        clear_depth_grant(capability=self.finance, tenant=self.sunrise.tenant, actor=None)
        event = self.latest(
            "Capability", self.sunrise.tenant, "config.depth_grant.cleared",
        )
        self.assertEqual(
            self.render(event, self.ngozi)["metadata"]["before"]["depth"],
            Capability.Depth.ADVANCED,
        )
        closed = self.render(event, self.without["uplift"])["metadata"]
        self.assertNotIn("before", closed)


class WithdrawnEntitlementTests(_Fixture):
    def test_the_dates_of_a_withdrawn_grant_follow_the_subscription_switches(self):
        clear_entitlement(
            capability=self.finance, tenant=self.sunrise.tenant, actor=None,
        )
        event = self.latest(
            "Capability", self.sunrise.tenant, "config.entitlement.cleared",
        )
        before = self.render(event, self.ngozi)["metadata"]["before"]
        self.assertTrue({"starts_at", "ends_at", "state"} <= set(before))
        for name, gone in (
            ("subscription_starts_at", "starts_at"),
            ("subscription_expires_at", "ends_at"),
        ):
            shown = self.render(event, self.without[name])["metadata"]["before"]
            self.assertNotIn(gone, shown)
            self.assertEqual(shown["state"], before["state"])


class WhatIsNotAgreementTests(_Fixture):
    def test_a_catalogue_edit_that_changes_a_depth_is_not_cut(self):
        from vs_audit.services import emit_audit_event

        emit_audit_event(
            module_key="CONFIG", action_type="CONFIG_CHANGED",
            entity_type="Capability", entity_id=str(self.finance.pk),
            tenant=None,
            metadata={
                "before": {"depth": 1}, "after": {"depth": 2},
                "config_action": "config.capability.updated",
            },
        )
        event = AuditEvent.objects.filter(
            entity_type="Capability", metadata__config_action="config.capability.updated",
        ).latest("event_at")
        shown = self.render(event, self.without["uplift"])["metadata"]
        self.assertEqual(shown["before"], {"depth": 1})
        self.assertEqual(shown["after"], {"depth": 2})

    def test_the_agreed_price_and_notes_are_in_no_audit_row(self):
        plan = PackagePlan.objects.exclude(code="basic").first()
        change_plan(
            school=self.sunrise, plan=plan, actor=None,
            agreed_price_per_student=RATE + 1,
        )
        for event in AuditEvent.objects.all():
            blob = str((event.before_data, event.diff_data, event.metadata, event.summary))
            self.assertNotIn(NOTES, blob)
            self.assertNotIn(str(RATE), blob)
            self.assertNotIn(str(RATE + 1), blob)

    def test_every_agreement_rule_is_a_valid_query(self):
        from vs_audit.services import emit_audit_event

        emit_audit_event(
            module_key="CONFIG", action_type="CONFIG_CHANGED",
            entity_type="CapabilityEntitlement", entity_id=str(self.finance.pk),
            tenant=self.bright.tenant,
            metadata={
                "config_action": "config.entitlement.updated",
                "before": {},
                "after": {"starts_at": self.ends.isoformat()},
            },
        )
        rules = [
            rule for name, rule in protected_words._RULES.items()
            if name.startswith("schools.agreement.")
        ]
        self.assertEqual(len(rules), 7)
        for rule in rules:
            self.assertFalse(rule.carries_words)
            AuditEvent.objects.filter(rule.q()).count()
        self.assertTrue(AuditEvent.objects.filter(rules[0].q()).exists())

    def test_the_summary_cut_and_the_search_ignore_the_agreement_rules(self):
        event = AuditEvent.objects.filter(
            entity_type="CapabilityEntitlement",
        ).first()
        self.assertIsNone(protected_words.rule_for(event))
        self.assertEqual(
            protected_words.visible_summary(event, lambda field: False), event.summary,
        )

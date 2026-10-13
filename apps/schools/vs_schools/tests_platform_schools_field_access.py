"""Field Access over a customer school's commercial terms and billing contact.

CodeX agrees a price and a plan with each school and invoices the school at a
billing email and phone. Those facts belong to the platform: the catalogue price
of a plan, the price agreed with one school, the minimum it is billed for, the
plan it is on, when its subscription starts and ends, the notes CodeX keeps, the
depth uplifts it was given and the billing contact are each a switch of
``platform.schools``, evaluated against the CodeX caller's roles.

Bright Star has two branches and an uplift on Finance; Sunrise has one branch.
Tunde, a CodeX account manager, can see Bright Star's terms but not agree a
different price; Ngozi can do everything; Emeka can see the school and none of
its terms. No school role ever reads or writes any of it, and a school's own
branch and profile switches never move a CodeX screen.
"""
from datetime import date, timedelta

from django.core.management import call_command
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone
from rest_framework.test import APIRequestFactory

from core.test_utils import TenantAPIClient
from vs_audit.models import AuditEvent
from vs_audit.serializers import AuditEventDetailSerializer
from vs_config.models import Capability, CapabilityDepthGrant
from vs_rbac.field_enforcement import field_access_payload
from vs_rbac.tests.helpers import (
    install_declared_fields,
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
    platform_tenant,
    set_field_access,
)

from .models import PackagePlan, School, SchoolPackageSetup
from .services.packages import apply_plan_entitlements

RESOURCE = "platform.schools"

#: Every registered field of the resource, by name.
FIELDS = (
    "catalogue_price_per_student",
    "agreed_price_per_student",
    "minimum_billable_students",
    "package_plan",
    "subscription_starts_at",
    "subscription_expires_at",
    "uplift",
    "notes",
    "billing_email",
    "billing_phone",
)

#: The keys a CodeX account manager holds over customer schools.
PLATFORM_KEYS = (
    "platform.schools.view",
    "platform.schools.create",
    "platform.schools.update",
    "platform.schools.configure",
)


def key(name):
    return f"{RESOURCE}.{name}"


def make_operator(email, *, closed=(), read_only=()):
    """A CodeX account manager whose role has *closed* and *read_only* switches set."""
    user = make_vision_user(email=email)
    role = make_platform_role(name=f"Account manager {email}")
    for permission in PLATFORM_KEYS:
        make_platform_role_permission(role, make_permission(permission))
    make_platform_assignment(user, role)
    if closed:
        set_field_access(role, *(key(n) for n in closed), read=False, write=False)
    if read_only:
        set_field_access(role, *(key(n) for n in read_only), read=True, write=False)
    return user


def provision(slug, branch_names, *, rate=350_000):
    """A school with branches, a Basic plan, agreed terms and its module grants."""
    school = make_school(
        slug=slug, name=slug.replace("-", " ").title(),
        email=f"billing@{slug}.test", phone="08031234567", address="1 School Road",
    )
    for index, name in enumerate(branch_names):
        make_branch(
            school, name=name, is_main=index == 0,
            address=f"{index + 1} Branch Road", email=f"branch{index}@{slug}.test",
        )
    plan = PackagePlan.objects.get(code="basic")
    SchoolPackageSetup.objects.create(
        school=school, package_plan=plan,
        subscription_starts_at=date.today(),
        subscription_expires_at=date.today() + timedelta(days=365),
        agreed_price_per_student=rate, minimum_billable_students=25,
        notes="Agreed on a call with the proprietor.",
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
        cls.bright = provision("bright-star-terms", ("Ikeja Branch", "Lekki Branch"))
        cls.sunrise = provision("sunrise-terms", ("Main Branch",))
        CapabilityDepthGrant.all_objects.create(
            capability=cls.finance, tenant=cls.bright.tenant,
            depth=Capability.Depth.ADVANCED, reason="Signed on the promise of payroll.",
            ends_at=timezone.now() + timedelta(days=90),
        )

        cls.ngozi = make_operator("ngozi@codexng.test")
        cls.tunde = make_operator("tunde@codexng.test", read_only=FIELDS)
        cls.emeka = make_operator("emeka@codexng.test", closed=FIELDS)
        #: One operator per field, with only that field closed.
        cls.without = {
            name: make_operator(f"without-{name}@codexng.test", closed=(name,))
            for name in FIELDS
        }
        #: One operator per field, with only that field read-only.
        cls.frozen = {
            name: make_operator(f"frozen-{name}@codexng.test", read_only=(name,))
            for name in FIELDS
        }

    def api(self, user):
        return TenantAPIClient(user=user)

    def detail(self, user, school=None):
        school = school or self.bright
        response = self.api(user).get(
            reverse("school-detail", kwargs={"slug": school.slug}),
        )
        self.assertEqual(response.status_code, 200, response.data)
        return response.data["data"]

    def plan_page(self, user, school=None):
        school = school or self.bright
        response = self.api(user).get(
            reverse("school-plan", kwargs={"slug": school.slug}),
        )
        self.assertEqual(response.status_code, 200, response.data)
        return response.data["data"]

    def denied(self, response, *labels):
        self.assertEqual(response.status_code, 403, response.data)
        self.assertEqual(response.data["error"]["code"], "field_write_denied")
        for label in labels:
            self.assertIn(label, response.data["message"])


class ReadingTermsAndBillingContactTests(_Fixture):
    def test_an_open_role_reads_every_term_on_the_school_detail(self):
        data = self.detail(self.ngozi)
        setup = data["package_setup"]
        self.assertEqual(data["email"], "billing@bright-star-terms.test")
        self.assertEqual(data["phone"], "08031234567")
        for name in (
            "package_plan", "subscription_starts_at", "subscription_expires_at",
            "agreed_price_per_student", "minimum_billable_students", "notes",
        ):
            self.assertIn(name, setup)
        self.assertIn("price_per_student", setup["package_plan"])
        self.assertEqual(data["_read_only_fields"], [])

    def test_a_role_with_every_switch_closed_reads_the_school_and_none_of_its_terms(self):
        data = self.detail(self.emeka)
        setup = data["package_setup"]
        self.assertEqual(data["name"], "Bright Star Terms")
        self.assertNotIn("email", data)
        self.assertNotIn("phone", data)
        self.assertEqual(
            set(setup) & {
                "package_plan", "subscription_starts_at", "subscription_expires_at",
                "agreed_price_per_student", "minimum_billable_students", "notes",
            },
            set(),
        )

    def test_each_switch_removes_only_its_own_field_from_the_detail(self):
        where = {
            "agreed_price_per_student": ("package_setup", "agreed_price_per_student"),
            "minimum_billable_students": ("package_setup", "minimum_billable_students"),
            "package_plan": ("package_setup", "package_plan"),
            "subscription_starts_at": ("package_setup", "subscription_starts_at"),
            "subscription_expires_at": ("package_setup", "subscription_expires_at"),
            "notes": ("package_setup", "notes"),
            "billing_email": (None, "email"),
            "billing_phone": (None, "phone"),
        }
        baseline = self.detail(self.ngozi)
        for name, (parent, field) in where.items():
            with self.subTest(field=name):
                data = self.detail(self.without[name])
                block = data[parent] if parent else data
                self.assertNotIn(field, block)
                expected = (baseline[parent] if parent else baseline)
                self.assertEqual(
                    set(expected) - set(block), {field},
                    "closing one switch must remove exactly one field",
                )

    def test_the_catalogue_price_is_separate_from_the_agreed_price(self):
        data = self.detail(self.without["catalogue_price_per_student"])
        setup = data["package_setup"]
        self.assertNotIn("price_per_student", setup["package_plan"])
        self.assertEqual(setup["agreed_price_per_student"], 350_000)

        data = self.detail(self.without["agreed_price_per_student"])
        setup = data["package_setup"]
        self.assertNotIn("agreed_price_per_student", setup)
        self.assertEqual(setup["package_plan"]["price_per_student"], 350_000)

    def test_the_plan_catalogue_drops_the_price_for_a_role_that_may_not_read_it(self):
        response = self.api(self.without["catalogue_price_per_student"]).get(
            reverse("package-plan-list"),
        )
        self.assertEqual(response.status_code, 200, response.data)
        rows = response.data["data"]
        self.assertTrue(rows)
        for row in rows:
            self.assertNotIn("price_per_student", row)
            self.assertIn("code", row)

        response = self.api(self.ngozi).get(reverse("package-plan-list"))
        self.assertIn("price_per_student", response.data["data"][0])

    def test_the_school_list_drops_the_billing_contact(self):
        response = self.api(self.without["billing_email"]).get(reverse("school-list"))
        self.assertEqual(response.status_code, 200, response.data)
        rows = response.data["data"]
        self.assertTrue(rows)
        for row in rows:
            self.assertNotIn("email", row)
            self.assertIn("phone", row)

    def test_the_plan_page_follows_the_plan_expiry_and_uplift_switches(self):
        baseline = self.plan_page(self.ngozi)
        finance = next(m for m in baseline["modules"] if m["key"] == "finance")
        self.assertEqual(finance["source"], "uplift")
        self.assertIsNotNone(finance["uplift"])
        self.assertIn("code", baseline["plan"])

        data = self.plan_page(self.without["package_plan"])
        self.assertNotIn("code", data["plan"])
        self.assertNotIn("default_depth", data["plan"])
        self.assertIn("subscription_expires_at", data["plan"])
        finance = next(m for m in data["modules"] if m["key"] == "finance")
        self.assertNotIn("plan_depth", finance)
        self.assertNotIn("source", finance)
        self.assertIn("uplift", finance)

        data = self.plan_page(self.without["subscription_expires_at"])
        self.assertNotIn("subscription_expires_at", data["plan"])
        self.assertIn("code", data["plan"])
        finance = next(m for m in data["modules"] if m["key"] == "finance")
        self.assertNotIn("expires_at", finance)

    def test_the_module_depths_of_a_school_follow_the_package_plan_switch(self):
        baseline = self.detail(self.ngozi)["package_setup"]["enabled_modules"]
        self.assertTrue(baseline)
        for module in baseline:
            self.assertIn("depth", module)
            self.assertIn("depth_label", module)

        for school in (self.bright, self.sunrise):
            with self.subTest(school=school.slug, plan="hidden"):
                modules = self.detail(
                    self.without["package_plan"], school,
                )["package_setup"]["enabled_modules"]
                self.assertTrue(modules)
                for module in modules:
                    self.assertNotIn("depth", module)
                    self.assertNotIn("depth_label", module)
                    self.assertIn("key", module)
            with self.subTest(school=school.slug, plan="visible"):
                modules = self.detail(
                    self.frozen["package_plan"], school,
                )["package_setup"]["enabled_modules"]
                for module in modules:
                    self.assertIn("depth", module)
                    self.assertIn("depth_label", module)

    def test_closing_another_switch_leaves_the_module_depths_in_place(self):
        modules = self.detail(
            self.without["agreed_price_per_student"],
        )["package_setup"]["enabled_modules"]
        for module in modules:
            self.assertIn("depth", module)
            self.assertIn("depth_label", module)

    def test_a_hidden_uplift_leaves_no_trace_of_it_on_the_plan_page(self):
        data = self.plan_page(self.without["uplift"])
        for module in data["modules"]:
            self.assertNotIn("uplift", module)
            self.assertNotEqual(module.get("source"), "uplift")
        finance = next(m for m in data["modules"] if m["key"] == "finance")
        self.assertNotIn("source", finance)

    def test_a_single_branch_school_reads_the_same_way(self):
        data = self.detail(self.without["agreed_price_per_student"], self.sunrise)
        self.assertNotIn("agreed_price_per_student", data["package_setup"])
        self.assertEqual(len(data["branches"]), 1)

    def test_a_read_only_role_is_told_which_fields_it_cannot_change(self):
        data = self.detail(self.tunde)
        self.assertEqual(data["email"], "billing@bright-star-terms.test")
        self.assertIn("email", data["_read_only_fields"])
        self.assertIn("phone", data["_read_only_fields"])

    def test_the_me_map_lists_what_a_role_may_not_read_or_change(self):
        tenant = platform_tenant()
        hidden = field_access_payload(self.emeka, tenant)[RESOURCE]
        self.assertEqual(
            set(hidden["hidden"]),
            {
                "price_per_student", "agreed_price_per_student",
                "minimum_billable_students", "package_plan", "subscription_starts_at",
                "subscription_expires_at", "uplift", "notes", "email", "phone",
            },
        )
        read_only = field_access_payload(self.tunde, tenant)[RESOURCE]
        self.assertEqual(read_only["hidden"], [])
        self.assertIn("agreed_price_per_student", read_only["read_only"])
        open_map = field_access_payload(self.ngozi, tenant)[RESOURCE]
        self.assertEqual(open_map["hidden"], [])
        self.assertEqual(open_map["read_only"], ["notes"])


class WritingTheCataloguePriceTests(_Fixture):
    def patch(self, user, price=400_000):
        return self.api(user).patch(
            reverse("package-plan-price", kwargs={"code": "basic"}),
            {"price_per_student": price}, format="json",
        )

    def test_a_role_with_write_changes_the_price_and_the_audit_row_carries_it(self):
        response = self.patch(self.ngozi)
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["price_per_student"], 400_000)
        self.assertEqual(PackagePlan.objects.get(code="basic").price_per_student, 400_000)

    def test_a_read_only_role_is_refused_by_name_and_nothing_is_saved(self):
        before = AuditEvent.objects.filter(entity_type="PackagePlan").count()
        response = self.patch(self.frozen["catalogue_price_per_student"])
        self.denied(response, "Catalogue price per student")
        self.assertEqual(PackagePlan.objects.get(code="basic").price_per_student, 350_000)
        self.assertEqual(AuditEvent.objects.filter(entity_type="PackagePlan").count(), before)

    def test_a_role_that_may_not_read_the_price_is_refused_the_same_way(self):
        response = self.patch(self.without["catalogue_price_per_student"])
        self.denied(response, "Catalogue price per student")
        self.assertEqual(PackagePlan.objects.get(code="basic").price_per_student, 350_000)

    def test_closing_the_agreed_price_does_not_close_the_catalogue_price(self):
        response = self.patch(self.frozen["agreed_price_per_student"])
        self.assertEqual(response.status_code, 200, response.data)


class WritingAPlanChangeTests(_Fixture):
    def move(self, user, **body):
        body.setdefault("package_plan", "standard")
        return self.api(user).patch(
            reverse("school-plan", kwargs={"slug": self.bright.slug}), body, format="json",
        )

    def setup_row(self):
        return SchoolPackageSetup.objects.get(school=self.bright)

    def test_an_open_role_moves_the_school_onto_the_plan(self):
        response = self.move(self.ngozi)
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(self.setup_row().package_plan.code, "standard")
        self.assertEqual(response.data["data"]["plan"]["code"], "standard")

    def test_a_read_only_plan_switch_refuses_the_move_and_names_the_field(self):
        response = self.move(self.frozen["package_plan"])
        self.denied(response, "Package plan")
        self.assertEqual(self.setup_row().package_plan.code, "basic")

    def test_a_read_only_expiry_switch_refuses_a_new_expiry_only(self):
        expiry = (date.today() + timedelta(days=500)).isoformat()
        response = self.move(self.frozen["subscription_expires_at"], subscription_expires_at=expiry)
        self.denied(response, "Subscription expiry date")
        self.assertEqual(self.setup_row().package_plan.code, "basic")

        response = self.move(self.frozen["subscription_expires_at"])
        self.assertEqual(response.status_code, 200, response.data)

    def test_a_quoted_rate_is_refused_to_a_role_that_may_not_change_the_agreed_price(self):
        response = self.move(
            self.frozen["agreed_price_per_student"],
            package_plan="enterprise", agreed_price_per_student=900_000,
        )
        self.denied(response, "Agreed price per student")
        self.assertEqual(self.setup_row().package_plan.code, "basic")
        self.assertEqual(self.setup_row().agreed_price_per_student, 350_000)

    def test_a_catalogue_plan_cannot_overwrite_an_agreed_price_the_role_may_not_change(self):
        """Bright Star's 3,500 would become Standard's 5,000 without anyone typing it."""
        response = self.move(self.frozen["agreed_price_per_student"])
        self.denied(response, "Agreed price per student")
        self.assertEqual(self.setup_row().package_plan.code, "basic")
        self.assertEqual(self.setup_row().agreed_price_per_student, 350_000)

    def test_the_same_refusal_reaches_a_role_that_may_not_read_the_agreed_price(self):
        response = self.move(self.without["agreed_price_per_student"])
        self.denied(response, "Agreed price per student")
        self.assertEqual(self.setup_row().package_plan.code, "basic")

    def test_a_move_that_leaves_the_agreed_price_alone_passes_for_a_read_only_role(self):
        SchoolPackageSetup.objects.filter(school=self.bright).update(
            agreed_price_per_student=PackagePlan.objects.get(code="standard").price_per_student,
        )
        response = self.move(self.frozen["agreed_price_per_student"])
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(self.setup_row().package_plan.code, "standard")

    def test_a_role_that_may_change_the_agreed_price_moves_the_school_and_its_price(self):
        response = self.move(self.frozen["catalogue_price_per_student"])
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(
            self.setup_row().agreed_price_per_student,
            PackagePlan.objects.get(code="standard").price_per_student,
        )

    def test_a_single_branch_school_is_judged_the_same_way(self):
        response = self.api(self.frozen["agreed_price_per_student"]).patch(
            reverse("school-plan", kwargs={"slug": self.sunrise.slug}),
            {"package_plan": "standard"}, format="json",
        )
        self.denied(response, "Agreed price per student")
        self.assertEqual(
            SchoolPackageSetup.objects.get(school=self.sunrise).package_plan.code, "basic",
        )

    def test_a_role_that_may_not_read_the_plan_cannot_move_a_school(self):
        response = self.move(self.without["package_plan"])
        self.denied(response, "Package plan")
        self.assertEqual(self.setup_row().package_plan.code, "basic")

    def test_the_response_to_a_move_is_filtered_for_the_caller(self):
        response = self.move(self.frozen["subscription_expires_at"])
        self.assertEqual(response.status_code, 200, response.data)
        self.assertIn("subscription_expires_at", response.data["data"]["plan"])


class WritingAnUpliftTests(_Fixture):
    def url(self):
        return reverse("school-plan-uplift", kwargs={"slug": self.sunrise.slug})

    def give(self, user):
        return self.api(user).post(
            self.url(),
            {"capability": "finance", "depth": Capability.Depth.PLUS, "reason": "Pilot."},
            format="json",
        )

    def test_an_open_role_gives_an_uplift(self):
        response = self.give(self.ngozi)
        self.assertEqual(response.status_code, 200, response.data)
        self.assertTrue(
            CapabilityDepthGrant.all_objects.filter(
                tenant=self.sunrise.tenant, capability=self.finance,
            ).exists()
        )

    def test_a_read_only_uplift_switch_refuses_a_new_uplift(self):
        response = self.give(self.frozen["uplift"])
        self.denied(response, "Module depth uplifts")
        self.assertFalse(
            CapabilityDepthGrant.all_objects.filter(tenant=self.sunrise.tenant).exists()
        )

    def test_a_read_only_uplift_switch_refuses_withdrawing_one(self):
        response = self.api(self.frozen["uplift"]).delete(
            reverse(
                "school-plan-uplift-detail",
                kwargs={"slug": self.bright.slug, "capability": "finance"},
            ),
        )
        self.denied(response, "Module depth uplifts")
        self.assertTrue(
            CapabilityDepthGrant.all_objects.filter(tenant=self.bright.tenant).exists()
        )


class CreatingAndCorrectingASchoolTests(_Fixture):
    def payload(self, slug, **package):
        body = {
            "name": "Greenfield Academy",
            "slug": slug,
            "email": f"billing@{slug}.test",
            "phone": "+2348012345678",
            "address": "1 Learning Road, Lagos",
            "branches": [{
                "name": "Main Branch", "state": "Lagos", "is_main": True,
                "primary_admin_data": {
                    "full_name": "Ada Okoye", "email": f"ada@{slug}.test",
                },
            }],
        }
        if package is not None:
            body["package_setup_data"] = {
                "package_plan": "basic",
                "subscription_starts_at": date.today().isoformat(),
                "subscription_expires_at": (date.today() + timedelta(days=365)).isoformat(),
                **package,
            }
        return body

    def create(self, user, slug, **package):
        return self.api(user).post(
            reverse("school-create"), self.payload(slug, **package), format="json",
        )

    def test_an_open_role_creates_a_school_with_its_terms(self):
        response = self.create(self.ngozi, "greenfield-open", minimum_billable_students=40)
        self.assertEqual(response.status_code, 202, response.data)
        school = School.objects.get(slug="greenfield-open")
        self.assertEqual(school.package_setup.minimum_billable_students, 40)

    def test_each_closed_switch_refuses_the_school_that_sets_its_term(self):
        bodies = {
            "package_plan": {},
            "subscription_starts_at": {},
            "subscription_expires_at": {},
            "minimum_billable_students": {"minimum_billable_students": 40},
            "agreed_price_per_student": {"agreed_price_per_student": 350_000},
        }
        labels = {
            "package_plan": "Package plan",
            "subscription_starts_at": "Subscription start date",
            "subscription_expires_at": "Subscription expiry date",
            "minimum_billable_students": "Minimum billable students",
            "agreed_price_per_student": "Agreed price per student",
        }
        for name, extra in bodies.items():
            for kind, operators in (("read-only", self.frozen), ("hidden", self.without)):
                slug = f"greenfield-{name}-{kind}".replace("_", "-")
                with self.subTest(field=name, state=kind):
                    response = self.create(operators[name], slug, **extra)
                    self.denied(response, labels[name])
                    self.assertFalse(School.objects.filter(slug=slug).exists())

    def test_a_catalogue_plan_cannot_set_the_agreed_price_for_a_role_that_may_not_agree_it(self):
        for kind, operators in (("read-only", self.frozen), ("hidden", self.without)):
            slug = f"greenfield-catalogue-{kind}"
            with self.subTest(state=kind):
                response = self.create(operators["agreed_price_per_student"], slug)
                self.denied(response, "Agreed price per student")
                self.assertFalse(School.objects.filter(slug=slug).exists())

    def test_a_role_that_may_not_agree_terms_still_registers_a_school_without_a_package(self):
        body = self.payload("greenfield-bare")
        body.pop("package_setup_data")
        response = self.api(self.emeka).post(reverse("school-create"), body, format="json")
        self.assertEqual(response.status_code, 202, response.data)
        school = School.objects.get(slug="greenfield-bare")
        self.assertEqual(school.email, "billing@greenfield-bare.test")
        self.assertFalse(hasattr(school, "package_setup"))

    def test_the_billing_contact_is_set_freely_on_creation_and_governed_afterwards(self):
        body = self.payload("greenfield-contact")
        body.pop("package_setup_data")
        response = self.api(self.frozen["billing_email"]).post(
            reverse("school-create"), body, format="json",
        )
        self.assertEqual(response.status_code, 202, response.data)

        school = School.objects.get(slug="greenfield-contact")
        update = self.api(self.frozen["billing_email"]).patch(
            reverse("school-update", kwargs={"slug": school.slug}),
            {"email": "accounts@greenfield-contact.test"}, format="json",
        )
        self.denied(update, "School billing email")
        school.refresh_from_db()
        self.assertEqual(school.email, "billing@greenfield-contact.test")

    def test_each_billing_contact_switch_guards_its_own_correction(self):
        for name, field, value in (
            ("billing_email", "email", "accounts@bright-star-terms.test"),
            ("billing_phone", "phone", "+2348099999999"),
        ):
            for kind, operators in (("read-only", self.frozen), ("hidden", self.without)):
                with self.subTest(field=name, state=kind):
                    response = self.api(operators[name]).patch(
                        reverse("school-update", kwargs={"slug": self.bright.slug}),
                        {field: value}, format="json",
                    )
                    self.assertEqual(response.status_code, 403, response.data)
                    self.assertEqual(
                        getattr(School.objects.get(pk=self.bright.pk), field),
                        {"email": "billing@bright-star-terms.test",
                         "phone": "08031234567"}[field],
                    )

    def test_the_other_billing_contact_field_stays_writable(self):
        response = self.api(self.frozen["billing_email"]).patch(
            reverse("school-update", kwargs={"slug": self.bright.slug}),
            {"phone": "+2348077777777"}, format="json",
        )
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(School.objects.get(pk=self.bright.pk).phone, "+2348077777777")

    def test_an_unrelated_correction_does_not_need_the_billing_switches(self):
        response = self.api(self.frozen["billing_phone"]).patch(
            reverse("school-update", kwargs={"slug": self.sunrise.slug}),
            {"motto": "Light and learning"}, format="json",
        )
        self.assertEqual(response.status_code, 200, response.data)


class ImportingSchoolsTests(_Fixture):
    ROW = {
        "name": "Importville Academy",
        "slug": "importville",
        "email": "billing@importville.test",
        "phone": "+2348011111111",
        "address": "1 Import Road, Lagos",
        "branch_admin_full_name": "Tunde Bello",
        "branch_admin_email": "tunde@importville.test",
        "package_plan": "basic",
        "subscription_starts_at": date.today().isoformat(),
        "agreed_price_per_student": "3500",
        "minimum_billable_students": "30",
    }

    def run_row(self, user, **overrides):
        from types import SimpleNamespace

        from vs_import_data.services.import_executor import import_schools_row

        return import_schools_row(
            import_batch=SimpleNamespace(school=None, template=None),
            payload={**self.ROW, **overrides}, queued_by=user,
        )

    def test_an_open_role_imports_a_school_with_its_agreed_terms(self):
        result = self.run_row(self.ngozi, slug="import-open")
        self.assertIsNotNone(result.instance, result.message)
        setup = School.objects.get(slug="import-open").package_setup
        self.assertEqual(setup.minimum_billable_students, 30)

    def test_a_closed_term_refuses_the_row_and_creates_nothing(self):
        from vs_rbac.field_enforcement import FieldWriteDenied

        for name in (
            "package_plan", "minimum_billable_students", "subscription_starts_at",
        ):
            with self.subTest(field=name):
                slug = f"import-{name}".replace("_", "-")
                with self.assertRaises(FieldWriteDenied) as raised:
                    self.run_row(self.frozen[name], slug=slug)
                self.assertIn(name, raised.exception.fields)
                self.assertFalse(School.objects.filter(slug=slug).exists())

    def test_the_agreed_price_column_is_refused_to_a_role_that_may_not_change_it(self):
        from vs_rbac.field_enforcement import FieldWriteDenied

        with self.assertRaises(FieldWriteDenied) as raised:
            self.run_row(self.frozen["agreed_price_per_student"], slug="import-price")
        self.assertEqual(raised.exception.fields, ["agreed_price_per_student"])
        self.assertFalse(School.objects.filter(slug="import-price").exists())

    def test_a_whole_file_reports_the_refused_row_by_name_and_imports_the_rest(self):
        from vs_import_data.models import (
            DatasetTypeChoices,
            FileFormatChoices,
            ImportBatch,
            ImportJobRowResult,
            ImportTemplate,
            ImportTemplateColumn,
            TemplateColumnDataTypeChoices,
        )
        from vs_import_data.services.import_executor import execute_import

        template = ImportTemplate.objects.create(
            code="schools-terms", name="Schools",
            dataset_type=DatasetTypeChoices.SCHOOLS,
            default_file_format=FileFormatChoices.CSV,
        )
        for order, target in enumerate(self.ROW, start=1):
            ImportTemplateColumn.objects.create(
                template=template, column_name=target, target_field=target,
                column_order=order, data_type=TemplateColumnDataTypeChoices.STRING,
            )
        no_terms = {
            k: v for k, v in self.ROW.items()
            if k not in ("package_plan", "subscription_starts_at",
                         "agreed_price_per_student", "minimum_billable_students")
        }
        batch = ImportBatch.all_objects.create(
            tenant=platform_tenant(), uploaded_by=self.tunde, template=template,
            dataset_type=DatasetTypeChoices.SCHOOLS, original_filename="schools.csv",
            preview_rows=[
                {**self.ROW, "slug": "file-with-terms"},
                {**no_terms, "slug": "file-without-terms", "name": "No Terms Academy",
                 "branch_admin_email": "bello@file-without-terms.test"},
            ],
            is_ready_for_import=True, total_rows=2,
        )

        job = execute_import(batch, self.tunde)

        self.assertEqual((job.succeeded_rows, job.failed_rows), (1, 1))
        refused = ImportJobRowResult.objects.get(job=job, row_number=1)
        self.assertIn("Package plan", refused.status_message)
        self.assertFalse(School.objects.filter(slug="file-with-terms").exists())
        self.assertTrue(School.objects.filter(slug="file-without-terms").exists())


class PreviewingASchoolsImportTests(_Fixture):
    """A dry run tells an operator what execution will refuse, row by row, and writes nothing."""

    ROW = ImportingSchoolsTests.ROW

    def batch(self, rows):
        from vs_import_data.models import (
            DatasetTypeChoices,
            FileFormatChoices,
            ImportBatch,
            ImportTemplate,
            ImportTemplateColumn,
            TemplateColumnDataTypeChoices,
        )

        template = ImportTemplate.objects.create(
            code="schools-preview", name="Schools preview",
            dataset_type=DatasetTypeChoices.SCHOOLS,
            default_file_format=FileFormatChoices.CSV,
        )
        for order, target in enumerate(self.ROW, start=1):
            ImportTemplateColumn.objects.create(
                template=template, column_name=target.replace("_", " ").title(),
                target_field=target, column_order=order,
                data_type=TemplateColumnDataTypeChoices.STRING,
            )
        return ImportBatch.all_objects.create(
            tenant=platform_tenant(), uploaded_by=self.ngozi, template=template,
            dataset_type=DatasetTypeChoices.SCHOOLS, original_filename="schools.csv",
            preview_rows=[
                {key.replace("_", " ").title(): value for key, value in row.items()}
                for row in rows
            ],
            total_rows=len(rows),
        )

    def refused(self, result):
        return [
            issue for issue in result["issues"]
            if "field_write_denied" in (issue.get("metadata") or {})
        ]

    def preview(self, user, *rows):
        from vs_import_data.services.validation_service import validate_import_batch

        return validate_import_batch(self.batch(rows), actor=user)

    def terms_row(self, slug="preview-with-terms"):
        return {**self.ROW, "slug": slug,
                "branch_admin_email": f"bello@{slug}.test"}

    def bare_row(self, slug="preview-without-terms"):
        row = {key: value for key, value in self.ROW.items() if key not in (
            "package_plan", "subscription_starts_at", "agreed_price_per_student",
            "minimum_billable_students",
        )}
        return {**row, "slug": slug, "name": "No Terms Academy",
                "branch_admin_email": f"bello@{slug}.test"}

    def test_a_restricted_operator_is_told_which_row_and_which_term_will_be_refused(self):
        before = School.objects.count()
        result = self.preview(self.tunde, self.terms_row(), self.bare_row())

        refused = self.refused(result)
        self.assertEqual({issue["row_number"] for issue in refused}, {1})
        self.assertEqual({issue["severity"] for issue in refused}, {"error"})
        by_column = {issue["column_name"]: issue["message"] for issue in refused}
        self.assertIn("Package plan", by_column["Package Plan"])
        self.assertIn("Minimum billable students", by_column["Minimum Billable Students"])
        self.assertEqual(School.objects.count(), before)

    def test_the_preview_uses_the_words_execution_uses(self):
        from vs_import_data.services.import_executor import school_row_write_refusal

        row = self.terms_row()
        refusal = school_row_write_refusal(row, self.frozen["minimum_billable_students"])
        self.assertIsNotNone(refusal)

        result = self.preview(self.frozen["minimum_billable_students"], row)
        self.assertEqual(self.refused(result)[0]["message"], refusal.message)
        self.assertIn("Minimum billable students", refusal.message)

    def test_a_derived_agreed_price_is_previewed_as_execution_refuses_it(self):
        row = {key: value for key, value in self.terms_row().items()
               if key != "agreed_price_per_student"}
        refused = self.refused(self.preview(self.frozen["agreed_price_per_student"], row))
        self.assertEqual(len(refused), 1)
        self.assertIn("Agreed price per student", refused[0]["message"])

    def test_an_open_role_sees_no_field_refusal(self):
        result = self.preview(self.ngozi, self.terms_row(), self.bare_row())
        self.assertEqual(self.refused(result), [])

    def test_a_file_with_no_terms_passes_for_a_role_that_may_set_none(self):
        result = self.preview(self.emeka, self.bare_row())
        self.assertEqual(self.refused(result), [])

    def test_a_preview_without_an_actor_judges_no_write_access(self):
        from vs_import_data.services.validation_service import validate_import_batch

        result = validate_import_batch(self.batch([self.terms_row()]))
        self.assertEqual(self.refused(result), [])


class SchoolSideIsolationTests(_Fixture):
    """The school never reads or writes its own commercial terms or billing contact."""

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.view = make_permission("school.profile.view")
        cls.admin = make_school_admin(None, email="admin@bright-star-terms.test",
                                      tenant=cls.bright.tenant)
        role = make_role(cls.bright, name="School Admin", key="school_admin")
        for permission in (cls.view, make_permission("school.profile.update"),
                           make_permission("school.branches.view")):
            make_role_permission(role, permission)
        make_assignment(cls.bright, cls.admin, role, branch=None)

    def test_the_schools_own_profile_carries_none_of_the_terms(self):
        response = TenantAPIClient(user=self.admin).get(reverse("school-profile"))
        self.assertEqual(response.status_code, 200, response.data)
        data = response.data["data"]
        for name in (
            "email", "phone", "package_setup", "package_plan", "agreed_price_per_student",
            "minimum_billable_students", "subscription_starts_at",
            "subscription_expires_at", "uplift", "notes", "price_per_student",
        ):
            self.assertNotIn(name, data)

    def test_the_schools_capability_read_carries_states_and_no_terms(self):
        response = TenantAPIClient(user=self.admin).get("/v1/config/my-capabilities/")
        self.assertEqual(response.status_code, 200, response.data)
        for row in response.data["data"]:
            self.assertEqual(set(row), {"key", "enabled"})

    def test_a_school_administrator_cannot_reach_the_platform_screens(self):
        client = TenantAPIClient(user=self.admin)
        for method, url, body in (
            ("get", reverse("school-detail", kwargs={"slug": self.bright.slug}), None),
            ("get", reverse("school-plan", kwargs={"slug": self.bright.slug}), None),
            ("patch", reverse("school-plan", kwargs={"slug": self.bright.slug}),
             {"package_plan": "standard"}),
            ("patch", reverse("package-plan-price", kwargs={"code": "basic"}),
             {"price_per_student": 1}),
            ("get", reverse("package-plan-list"), None),
            ("patch", reverse("school-update", kwargs={"slug": self.bright.slug}),
             {"email": "x@y.test"}),
        ):
            with self.subTest(method=method, url=url):
                if body is None:
                    response = getattr(client, method)(url)
                else:
                    response = getattr(client, method)(url, body, format="json")
                self.assertIn(response.status_code, (403, 404), response.data)
        setup = SchoolPackageSetup.objects.get(school=self.bright)
        self.assertEqual(setup.package_plan.code, "basic")
        self.assertEqual(School.objects.get(pk=self.bright.pk).email, "billing@bright-star-terms.test")

    def test_a_school_role_cannot_even_hold_a_platform_switch(self):
        """A school's role cannot carry a switch over CodeX's terms, so none can move them."""
        from django.core.exceptions import ValidationError

        role = make_role(self.bright, name="Hides everything", key="hides_everything")
        with self.assertRaises(ValidationError):
            set_field_access(role, key("agreed_price_per_student"), read=False, write=False)
        data = self.detail(self.ngozi)
        self.assertEqual(data["email"], "billing@bright-star-terms.test")
        self.assertIn("agreed_price_per_student", data["package_setup"])


class AuditSnapshotTests(_Fixture):
    """The trail repeats a price or a contact only to readers of that field."""

    def render(self, event, user):
        request = APIRequestFactory().get("/")
        request.user = user
        request.tenant = platform_tenant()
        return AuditEventDetailSerializer(event, context={"request": request}).data

    def test_a_price_change_shows_its_values_only_to_a_reader_of_the_price(self):
        self.assertEqual(
            self.api(self.ngozi).patch(
                reverse("package-plan-price", kwargs={"code": "basic"}),
                {"price_per_student": 410_000}, format="json",
            ).status_code, 200,
        )
        event = AuditEvent.objects.filter(entity_type="PackagePlan").latest("event_at")

        open_view = self.render(event, self.ngozi)
        self.assertEqual(open_view["before_data"], {"price_per_student": 350_000})
        self.assertIn("price_per_student", open_view["diff_data"])

        closed_view = self.render(event, self.without["catalogue_price_per_student"])
        self.assertEqual(closed_view["before_data"], {})
        self.assertEqual(closed_view["diff_data"], {})

        other_closed = self.render(event, self.without["agreed_price_per_student"])
        self.assertIn("price_per_student", other_closed["diff_data"])

    def test_a_billing_contact_correction_hides_the_old_and_new_email(self):
        self.assertEqual(
            self.api(self.ngozi).patch(
                reverse("school-update", kwargs={"slug": self.sunrise.slug}),
                {"email": "accounts@sunrise-terms.test"}, format="json",
            ).status_code, 200,
        )
        event = AuditEvent.objects.filter(
            entity_type="School", entity_id=str(self.sunrise.pk),
        ).latest("event_at")

        open_view = self.render(event, self.ngozi)
        self.assertEqual(open_view["diff_data"]["email"]["after"], "accounts@sunrise-terms.test")
        self.assertEqual(open_view["before_data"]["email"], "billing@sunrise-terms.test")

        closed_view = self.render(event, self.without["billing_email"])
        self.assertNotIn("email", closed_view["diff_data"])
        self.assertNotIn("email", closed_view["before_data"])
        self.assertEqual(closed_view["before_data"]["phone"], "08031234567")

    def test_a_snapshot_rule_leaves_the_summary_and_the_search_alone(self):
        from vs_audit.protected_words import rule_for, visible_summary, visible_summary_expression

        event = AuditEvent.objects.filter(entity_type="PackagePlan").first()
        if event is None:
            self.api(self.ngozi).patch(
                reverse("package-plan-price", kwargs={"code": "basic"}),
                {"price_per_student": 360_000}, format="json",
            )
            event = AuditEvent.objects.filter(entity_type="PackagePlan").latest("event_at")
        self.assertIsNone(rule_for(event))
        self.assertEqual(visible_summary(event, lambda field: False), event.summary)
        visible_summary_expression(lambda field: False)


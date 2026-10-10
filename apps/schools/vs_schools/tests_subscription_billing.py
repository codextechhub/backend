"""School subscription pricing and CodeX customer registration."""
from datetime import date, timedelta
from unittest.mock import patch

from django.core.management import call_command
from django.test import TestCase
from django.urls import reverse
from rest_framework.test import APIClient

from vs_finance.models import Customer, LedgerEntity
from vs_audit.models import AuditActionType, AuditEvent, AuditModuleKey
from vs_rbac.tests.helpers import assert_school_created, make_vision_user
from schools.vs_students.constants import Gender, StudentStatus
from schools.vs_students.models import Student

from .models import PackagePlan, School
from .services.platform_customer import SOURCE_TYPE
from .services.subscription_billing import calculate_subscription_charge


class SchoolSubscriptionBillingTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        call_command("seed_actions", verbosity=0)
        call_command("seed_prebuilt_role_templates", verbosity=0)
        call_command("seed_package", verbosity=0)
        cls.operator = make_vision_user(
            email="subscription-operator@codexng.test",
            super_admin=True,
        )

    def setUp(self):
        self.client = APIClient()
        self.client.force_authenticate(user=self.operator)

    def payload(self, *, slug="bright-star-billing", plan="basic", rate=None):
        package = {
            "package_plan": plan,
            "subscription_starts_at": date.today().isoformat(),
            "subscription_expires_at": (date.today() + timedelta(days=365)).isoformat(),
        }
        if rate is not None:
            package["agreed_price_per_student"] = rate
        return {
            "name": "Bright Star School",
            "slug": slug,
            "email": "accounts@bright-star.test",
            "phone": "+2348012345678",
            "address": "1 Learning Road, Lagos",
            "package_setup_data": package,
            "branches": [{
                "name": "Main Branch",
                "state": "Lagos",
                "is_main": True,
                "primary_admin_data": {
                    "full_name": "Ada Okoye",
                    "email": f"ada@{slug}.test",
                },
            }],
        }

    def create_school(self, **kwargs):
        response = self.client.post(
            reverse("school-create"), self.payload(**kwargs), format="json",
        )
        assert_school_created(self, response)
        return School.objects.get(slug=kwargs.get("slug", "bright-star-billing"))

    def test_creation_registers_the_school_as_a_platform_customer(self):
        school = self.create_school()

        customer = Customer.objects.get(
            entity=LedgerEntity.objects.platform(),
            source_type=SOURCE_TYPE,
            source_id=str(school.pk),
        )
        self.assertEqual(customer.name, school.name)
        self.assertEqual(customer.billing_email, school.email)
        self.assertEqual(customer.billing_phone, school.phone)
        self.assertEqual(customer.billing_address, school.address)
        self.assertTrue(customer.is_active)

    def test_catalogue_rate_is_snapshotted_on_the_school_subscription(self):
        school = self.create_school()

        self.assertEqual(
            school.package_setup.agreed_price_per_student,
            PackagePlan.objects.get(code="basic").price_per_student,
        )
        self.assertEqual(school.package_setup.subscription_starts_at, date.today())
        self.assertEqual(school.package_setup.minimum_billable_students, 0)

    def test_enterprise_requires_the_schools_quoted_rate(self):
        response = self.client.post(
            reverse("school-create"),
            self.payload(slug="quoted-school", plan="enterprise"),
            format="json",
        )

        self.assertEqual(response.status_code, 400)
        self.assertIn(
            "agreed_price_per_student",
            response.data["error"]["detail"]["package_setup_data"],
        )

    def test_school_contact_and_status_changes_sync_the_customer(self):
        school = self.create_school()
        response = self.client.patch(
            reverse("school-update", kwargs={"slug": school.slug}),
            {
                "email": "billing@bright-star.test",
                "phone": "+2348099999999",
                "address": "9 New Road, Lagos",
            },
            format="json",
        )

        self.assertEqual(response.status_code, 200, response.data)
        customer = Customer.objects.get(source_type=SOURCE_TYPE, source_id=str(school.pk))
        self.assertEqual(customer.billing_email, "billing@bright-star.test")
        self.assertEqual(customer.billing_phone, "+2348099999999")
        self.assertEqual(customer.billing_address, "9 New Road, Lagos")

        school.status = "INACTIVE"
        school.save(update_fields=["status"])
        customer.refresh_from_db()
        self.assertFalse(customer.is_active)

    def test_price_settings_change_future_catalogue_rate(self):
        response = self.client.patch(
            reverse("package-plan-price", kwargs={"code": "basic"}),
            {"price_per_student": 400_000},
            format="json",
        )

        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(PackagePlan.objects.get(code="basic").price_per_student, 400_000)
        self.assertTrue(AuditEvent.objects.filter(
            module_key=AuditModuleKey.CONFIG,
            action_type=AuditActionType.UPDATE,
            entity_type="PackagePlan",
            entity_label="Basic",
        ).exists())

    def test_price_settings_fail_before_writing_without_platform_finance(self):
        plan = PackagePlan.objects.get(code="basic")
        original_price = plan.price_per_student

        with patch.object(LedgerEntity.objects, "platform", return_value=None):
            response = self.client.patch(
                reverse("package-plan-price", kwargs={"code": "basic"}),
                {"price_per_student": 400_000},
                format="json",
            )

        self.assertEqual(response.status_code, 400, response.data)
        plan.refresh_from_db()
        self.assertEqual(plan.price_per_student, original_price)

    def test_charge_uses_active_students_and_preserves_the_agreed_rate(self):
        school = self.create_school()
        branch = school.main_branch
        for index, status in enumerate((StudentStatus.ACTIVE, StudentStatus.ENROLLED)):
            Student.all_objects.create(
                tenant=school.tenant,
                branch=branch,
                first_name=f"Student {index}",
                last_name="Okoye",
                date_of_birth=date(2015, 1, index + 1),
                gender=Gender.FEMALE,
                status=status,
                enrolment_date=date.today(),
            )

        snapshot = calculate_subscription_charge(school, billing_date=date.today())

        self.assertEqual(snapshot.active_students, 1)
        self.assertEqual(snapshot.billable_students, 1)
        self.assertEqual(snapshot.price_per_student, 350_000)
        self.assertEqual(snapshot.amount, 350_000)
        self.assertTrue(snapshot.should_invoice)

    def test_empty_school_with_no_minimum_has_no_first_invoice(self):
        school = self.create_school()

        snapshot = calculate_subscription_charge(school, billing_date=date.today())

        self.assertEqual(snapshot.billable_students, 0)
        self.assertEqual(snapshot.amount, 0)
        self.assertFalse(snapshot.should_invoice)

    def test_enterprise_catalogue_rate_cannot_replace_the_school_quote(self):
        response = self.client.patch(
            reverse("package-plan-price", kwargs={"code": "enterprise"}),
            {"price_per_student": 900_000},
            format="json",
        )

        self.assertEqual(response.status_code, 400)

    def test_price_settings_require_school_configuration_permission(self):
        reader = make_vision_user(email="pricing-reader@codexng.test")
        self.client.force_authenticate(user=reader)

        response = self.client.patch(
            reverse("package-plan-price", kwargs={"code": "basic"}),
            {"price_per_student": 400_000},
            format="json",
        )

        self.assertEqual(response.status_code, 403)

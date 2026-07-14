from django.test import RequestFactory, TestCase
from rest_framework.exceptions import NotFound, ValidationError

from vs_schools.models import School, SchoolStatus
from vs_tenants.models import Tenant
from vs_tenants.resolution import resolve_tenant
from vs_user.models import User


class TenantFoundationTests(TestCase):
    def setUp(self):
        self.factory = RequestFactory()
        self.school = School.objects.create(
            name="Cedar Academy", slug="cedar-academy", code="CEDAR",
            status=SchoolStatus.ACTIVE,
        )
        self.user = User.objects.create_user(
            email="admin@cedar.test", password="pw", first_name="Ada", last_name="Okafor",
            user_type=User.UserType.SCHOOL_ADMIN, tenant=self.school.tenant,
            status=User.Status.ACTIVE, is_active=True,
        )

    def request(self, query=""):
        request = self.factory.get("/v1/example/" + query)
        request.user = self.user
        request.query_params = request.GET
        return request

    def test_school_creation_atomically_provisions_tenant(self):
        self.assertEqual(self.school.tenant.slug, "cedar-academy")
        self.assertEqual(self.school.tenant.kind, Tenant.Kind.SCHOOL)
        self.assertEqual(self.user.tenant, self.school.tenant)

    def test_tenant_parameter_is_required(self):
        with self.assertRaises(ValidationError):
            resolve_tenant(self.request())

    def test_cross_tenant_slug_is_non_enumerating(self):
        other = School.objects.create(
            name="Other", slug="other", code="OTHER", status=SchoolStatus.ACTIVE,
        )
        with self.assertRaises(NotFound):
            resolve_tenant(self.request(f"?tenant={other.tenant.slug}"))

    def test_matching_slug_resolves(self):
        tenant = resolve_tenant(self.request("?tenant=cedar-academy"))
        self.assertEqual(tenant, self.school.tenant)


class TenantAuthorityTests(TestCase):
    def test_user_classification_does_not_imply_authority(self):
        school = School.objects.create(
            name="Persona School", slug="persona-school", code="PERSONA",
            status=SchoolStatus.ACTIVE,
        )
        user = User.objects.create_user(
            email="staff@persona.test", password="pw", first_name="No", last_name="Role",
            user_type=User.UserType.STAFF, tenant=school.tenant,
            branch=school.branches.create(name="Main", code=1, is_main=True, _type="Main"),
            status=User.Status.ACTIVE, is_active=True,
        )
        from vs_rbac.evaluator import get_effective_permissions
        self.assertEqual(get_effective_permissions(user, tenant=school.tenant), set())

from django.http import HttpResponse
from django.test import RequestFactory, TestCase
from rest_framework.exceptions import NotFound, ValidationError

from vs_schools.models import School, SchoolStatus
from vs_rbac.tests.helpers import make_vision_user
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


class ProxyAuditMiddlewareTests(TestCase):
    def setUp(self):
        from vs_admin_console.models import ImpersonationSession

        self.factory = RequestFactory()
        self.tenant = Tenant.objects.get(slug="codex", kind=Tenant.Kind.PLATFORM)
        self.actor = make_vision_user(email="middleware-proxier@codex.test")
        self.actor.first_name = "Ada"
        self.actor.last_name = "Admin"
        self.actor.save(update_fields=["first_name", "last_name"])
        self.target = make_vision_user(email="middleware-target@codex.test")
        self.target.first_name = "Rashida"
        self.target.last_name = "Sule"
        self.target.save(update_fields=["first_name", "last_name"])
        self.session = ImpersonationSession.objects.create(
            staff_user=self.actor, target_user=self.target, tenant=self.tenant,
            justification="Middleware policy test",
        )

    def _run(self, method="get", status_code=200, emit_business_event=False):
        from vs_audit.services import emit_audit_event
        from vs_tenants.context import set_current_audit_identity
        from vs_tenants.middleware import TenantContextCleanupMiddleware

        request = getattr(self.factory, method.lower())("/v1/user/example/")

        def get_response(req):
            req.actor_user = self.actor
            req.effective_user = self.target
            req.impersonation_session = self.session
            req.tenant = self.tenant
            set_current_audit_identity(
                actor_user=self.actor,
                effective_user=self.target,
                impersonation_session=self.session,
            )
            if emit_business_event:
                emit_audit_event(
                    module_key="USER", action_type="UPDATE",
                    entity_type="User", entity_id=str(self.target.pk),
                    entity_label=self.target.full_name, actor_user=self.target,
                    tenant=self.tenant,
                )
            return HttpResponse(status=status_code)

        return TenantContextCleanupMiddleware(get_response)(request)

    def test_successful_read_is_not_audited(self):
        from vs_audit.models import AuditEvent

        self._run("get")
        self.assertFalse(AuditEvent.objects.exists())

    def test_successful_change_without_business_event_gets_one_fallback(self):
        from vs_audit.models import AuditEvent

        self._run("patch")
        event = AuditEvent.objects.get()
        self.assertEqual(event.action_type, "PROXY_CHANGE")
        self.assertNotIn("/v1/", event.summary)
        self.assertEqual(event.metadata["path"], "/v1/user/example/")

    def test_failed_read_remains_visible_for_security_review(self):
        from vs_audit.models import AuditEvent

        self._run("get", status_code=403)
        event = AuditEvent.objects.get()
        self.assertEqual(event.action_type, "PROXY_ACTION_FAILED")
        self.assertEqual(event.status, "DENIED")
        self.assertIn("was blocked", event.summary)

    def test_business_event_suppresses_generic_change_fallback(self):
        from vs_audit.models import AuditEvent

        self._run("patch", emit_business_event=True)
        self.assertEqual(list(AuditEvent.objects.values_list("action_type", flat=True)), ["UPDATE"])

import datetime

from django.http import HttpResponse
from django.test import RequestFactory, TestCase
from rest_framework.exceptions import NotFound, ValidationError

from vs_schools.models import School, SchoolStatus
from vs_rbac.tests.helpers import make_vision_user
from vs_tenants.models import Tenant
from vs_tenants.numbering import next_tenant_document_number
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


class TenantDocumentNumberTests(TestCase):
    def setUp(self):
        self.a = Tenant.objects.create(
            name="Alpha", slug="alpha", kind=Tenant.Kind.ORGANIZATION,
            status=Tenant.Status.ACTIVE,
        )
        self.b = Tenant.objects.create(
            name="Beta", slug="beta", kind=Tenant.Kind.ORGANIZATION,
            status=Tenant.Status.ACTIVE,
        )
        self.day = datetime.date(2026, 7, 22)

    def test_exact_format_and_same_scope_increment(self):
        first = next_tenant_document_number(
            tenant=self.a, document_code="iv", allocation_date=self.day,
        )
        second = next_tenant_document_number(
            tenant=self.a, document_code="IV", allocation_date=self.day,
        )
        self.assertEqual(first, f"IV-{self.a.pk}2607221")
        self.assertEqual(second, f"IV-{self.a.pk}2607222")

    def test_tenants_and_document_codes_have_independent_series(self):
        a_invoice = next_tenant_document_number(
            tenant=self.a, document_code="IV", allocation_date=self.day,
        )
        b_invoice = next_tenant_document_number(
            tenant=self.b, document_code="IV", allocation_date=self.day,
        )
        a_payment = next_tenant_document_number(
            tenant=self.a, document_code="PY", allocation_date=self.day,
        )
        self.assertTrue(a_invoice.endswith("2607221"))
        self.assertTrue(b_invoice.endswith("2607221"))
        self.assertTrue(a_payment.endswith("2607221"))

    def test_next_date_starts_at_one(self):
        next_tenant_document_number(
            tenant=self.a, document_code="IV", allocation_date=self.day,
        )
        result = next_tenant_document_number(
            tenant=self.a, document_code="IV",
            allocation_date=datetime.date(2026, 7, 23),
        )
        self.assertEqual(result, f"IV-{self.a.pk}2607231")


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


class ReconcileTenantsInvariantTests(TestCase):
    """``reconcile_tenants`` asserts the invariants that survived phase D.

    The check that compared ``Branch.tenant`` against ``school.tenant`` went
    with the ``school`` column: with a single statement of ownership there is
    no second path left to disagree with it, and comparing ``tenant`` with
    itself would have been vacuous. What remains for branches is the null
    check. The role-template check reads ``branch__tenant`` and is a real
    cross-tenant assertion, so it must still fire - that is what the failing
    case below proves.
    """

    def _run(self):
        from io import StringIO

        from django.core.management import call_command

        out = StringIO()
        call_command("reconcile_tenants", stdout=out)
        return out.getvalue()

    def test_a_clean_database_reconciles(self):
        school = School.objects.create(
            name="Reconcile School", slug="reconcile-school", code="RECON",
            status=SchoolStatus.ACTIVE,
        )
        school.branches.create(name="Main", code=1, is_main=True, _type="Main")

        self.assertIn("passed", self._run())

    def test_a_role_template_on_another_tenants_branch_is_reported(self):
        from django.core.management.base import CommandError

        from vs_rbac.models import TenantRoleTemplate

        school = School.objects.create(
            name="Recon A", slug="recon-a", code="RECONA", status=SchoolStatus.ACTIVE,
        )
        rival = School.objects.create(
            name="Recon B", slug="recon-b", code="RECONB", status=SchoolStatus.ACTIVE,
        )
        rival_branch = rival.branches.create(
            name="Main", code=1, is_main=True, _type="Main",
        )
        # Written straight to the table: the model's clean() refuses this, and
        # the command exists precisely to find rows that got in anyway.
        TenantRoleTemplate.objects.create(
            tenant=school.tenant, key="recon-role", name="Recon Role",
            status="ACTIVE", branch=rival_branch,
        )

        with self.assertRaises(CommandError) as caught:
            self._run()
        self.assertIn("cross-tenant branches", str(caught.exception))

    def test_a_role_template_on_its_own_branch_is_not_reported(self):
        from vs_rbac.models import TenantRoleTemplate

        school = School.objects.create(
            name="Recon C", slug="recon-c", code="RECONC", status=SchoolStatus.ACTIVE,
        )
        branch = school.branches.create(name="Main", code=1, is_main=True, _type="Main")
        TenantRoleTemplate.objects.create(
            tenant=school.tenant, key="recon-ok", name="Recon Ok",
            status="ACTIVE", branch=branch,
        )

        self.assertIn("passed", self._run())


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

    def _run(
        self, method="get", status_code=200, emit_business_event=False,
        path="/v1/user/example/",
    ):
        from vs_audit.services import emit_audit_event
        from vs_tenants.context import set_current_audit_identity
        from vs_tenants.middleware import TenantContextCleanupMiddleware

        request = getattr(self.factory, method.lower())(path)

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
        self.assertEqual(
            event.summary,
            "Ada Admin updated user example while proxied as Rashida Sule",
        )
        self.assertEqual(event.metadata["path"], "/v1/user/example/")
        self.assertEqual(event.metadata["change_description"], "updated user example")

    def test_successful_notification_housekeeping_is_not_audited(self):
        from vs_audit.models import AuditEvent

        for path in (
            "/v1/notify/mark-read/",
            "/v1/notify/mark-all-read/",
            "/v1/notify/acknowledge-route/",
        ):
            with self.subTest(path=path):
                self._run("post", path=path)
        self.assertFalse(AuditEvent.objects.exists())

    def test_failed_notification_housekeeping_remains_audited(self):
        from vs_audit.models import AuditEvent

        self._run("post", status_code=403, path="/v1/notify/mark-read/")

        event = AuditEvent.objects.get()
        self.assertEqual(event.action_type, "PROXY_ACTION_FAILED")
        self.assertEqual(event.status, "DENIED")

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

import datetime

from django.core.exceptions import ValidationError as DjangoValidationError
from django.db import IntegrityError, transaction
from django.http import HttpResponse
from django.test import RequestFactory, TestCase
from django.utils import timezone
from rest_framework.exceptions import NotFound, ValidationError

from schools.vs_schools.models import School, SchoolStatus
from vs_rbac.tests.helpers import make_vision_user
from vs_tenants.exceptions import (
    BranchNotInService,
    InvalidBranchTransition,
    LastBranchCannotLeaveService,
    MainBranchCannotLeaveService,
)
from vs_tenants.models import (
    RESERVED_TENANT_SLUGS,
    Branch,
    BranchStatus,
    Tenant,
)
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
            user_type=User.UserType.STAFF, tenant=self.school.tenant,
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


class BranchDatabaseConstraintTests(TestCase):
    """The two promises in ``Branch``'s docstring, tested at the database.

    ``Branch.save()`` and ``Branch.clean()`` both guard the main-branch rule in
    Python, and the school-creation serializer guards both rules again. None of
    that is what makes the rules true: a management command, a data migration,
    the shell or a bulk import reaches the table without passing any of them.
    So every test here writes through ``bulk_create``, which skips ``save()``
    entirely, and asserts on ``IntegrityError`` from the constraint itself.

    ``all_objects`` throughout: ``objects`` is the ``TenantAwareManager`` and
    would hide the rows of whichever tenant is not ambient.
    """

    def setUp(self):
        self.alpha = Tenant.objects.create(
            name="Alpha School", slug="alpha", kind=Tenant.Kind.SCHOOL,
        )
        self.beta = Tenant.objects.create(
            name="Beta School", slug="beta", kind=Tenant.Kind.SCHOOL,
        )

    def _branch(self, tenant, *, code, is_main=False, name="Site",
                status=BranchStatus.ACTIVE):
        return Branch(
            tenant=tenant, name=name, code=code, is_main=is_main,
            _type="Main" if is_main else "Sub", status=status,
        )

    def _insert(self, *branches):
        """Write straight to the table, past ``save()`` and ``clean()``."""
        return Branch.all_objects.bulk_create(list(branches))

    # --- uq_branch_tenant_code ---------------------------------------------

    def test_duplicate_code_in_the_same_tenant_is_refused(self):
        self._insert(self._branch(self.alpha, code=1, is_main=True))

        with self.assertRaises(IntegrityError), transaction.atomic():
            self._insert(self._branch(self.alpha, code=1, name="Clash"))

    def test_the_same_code_in_a_different_tenant_is_allowed(self):
        """Proves the uniqueness is scoped to the tenant, not global."""
        self._insert(
            self._branch(self.alpha, code=1, is_main=True),
            self._branch(self.beta, code=1, is_main=True),
        )

        self.assertEqual(Branch.all_objects.filter(code=1).count(), 2)

    def test_code_is_not_nullable_so_the_constraint_has_no_null_escape(self):
        """A nullable column would let Postgres wave through unlimited rows.

        In PostgreSQL two NULLs are never equal, so a unique index over a
        nullable column does not constrain NULL rows at all. ``code`` is
        declared NOT NULL, which is what closes that hole; this test fails the
        moment somebody relaxes it.
        """
        self.assertFalse(Branch._meta.get_field("code").null)

        with self.assertRaises(IntegrityError), transaction.atomic():
            self._insert(self._branch(self.alpha, code=None))

    # --- uq_branch_one_main_per_tenant -------------------------------------

    def test_a_second_main_branch_in_the_same_tenant_is_refused(self):
        self._insert(self._branch(self.alpha, code=1, is_main=True, name="HQ"))

        with self.assertRaises(IntegrityError), transaction.atomic():
            self._insert(
                self._branch(self.alpha, code=2, is_main=True, name="Lekki"),
            )

    def test_each_tenant_may_have_its_own_main_branch(self):
        self._insert(
            self._branch(self.alpha, code=1, is_main=True, name="Alpha HQ"),
            self._branch(self.beta, code=1, is_main=True, name="Beta HQ"),
        )

        self.assertEqual(Branch.all_objects.filter(is_main=True).count(), 2)

    def test_many_non_main_branches_are_allowed(self):
        """The index is partial: only ``is_main=True`` rows are constrained."""
        self._insert(
            self._branch(self.alpha, code=1, is_main=True, name="HQ"),
            self._branch(self.alpha, code=2, name="Lekki"),
            self._branch(self.alpha, code=3, name="Ikeja"),
        )

        self.assertEqual(Branch.all_objects.filter(tenant=self.alpha).count(), 3)

    # --- how the lifecycle interacts with both rules ------------------------

    def test_a_closed_main_branch_still_blocks_a_second_main(self):
        """Neither constraint looks at ``status``, and that is deliberate.

        ``is_main`` records which site is the canonical one, not which site is
        trading. If the index were narrowed to in-service rows, closing the main
        branch would silently let a tenant hold two ``is_main=True`` rows, and
        ``School.main_branch`` (a ``.filter(is_main=True).first()``) would then
        return whichever the database handed back first.
        """
        main = self._branch(self.alpha, code=1, is_main=True, name="HQ")
        self._insert(main)
        # Straight to the table, exactly as the class docstring describes: the
        # lifecycle refuses to close a main branch, so a row in this shape can
        # only arrive by a path that skips ``transition()``. The constraint has
        # to keep holding anyway.
        Branch.all_objects.filter(pk=main.pk).update(status=BranchStatus.CLOSED)
        self.assertEqual(
            Branch.all_objects.get(pk=main.pk).status, BranchStatus.CLOSED,
        )

        with self.assertRaises(IntegrityError), transaction.atomic():
            self._insert(
                self._branch(self.alpha, code=2, is_main=True, name="New HQ"),
            )

    def test_a_closed_branchs_code_stays_reserved(self):
        """Codes are historical labels, so a closed branch keeps its number.

        ``allocate_next_code`` is ``max(code) + 1`` over every row regardless of
        status, so it never re-issues a closed branch's code either. The two
        agree, and a document that cites branch 2 keeps meaning one branch.
        """
        self._insert(
            self._branch(self.alpha, code=1, is_main=True, name="HQ"),
            self._branch(self.alpha, code=2, name="Lekki"),
        )
        closed = Branch.all_objects.get(tenant=self.alpha, code=2)
        closed.transition(to_state=BranchStatus.CLOSED, actor_id="1", reason="shut")

        with self.assertRaises(IntegrityError), transaction.atomic():
            self._insert(self._branch(self.alpha, code=2, name="Reused"))

        self.assertEqual(
            Branch.allocate_next_code(tenant_id=self.alpha.pk), 3,
        )

    def test_closing_a_main_branch_is_refused_so_the_tenant_keeps_one(self):
        """The dead end these constraints used to permit, now closed off.

        This test used to assert the damage rather than prevent it: nothing
        demoted ``is_main`` when a branch closed and nothing refused the
        closure, so a tenant could be left with a main branch that was out of
        service and could never be replaced - the partial unique index below
        refuses to hand the flag to any survivor, and CLOSED is terminal, so
        there was no way back. The constraints were never the gap; the
        lifecycle was, and ``Branch.transition`` now refuses the edge.

        The second half still asserts the old dead end, because it is what
        makes the refusal necessary: were a main branch ever closed by a path
        that skips ``transition()``, the survivor still could not be promoted.
        """
        main = self._branch(self.alpha, code=1, is_main=True, name="HQ")
        other = self._branch(self.alpha, code=2, name="Lekki")
        self._insert(main, other)

        with self.assertRaises(MainBranchCannotLeaveService):
            main.transition(to_state=BranchStatus.CLOSED, actor_id="1", reason="shut")

        # Nothing moved: the main branch is still in service, and still main.
        main.refresh_from_db()
        self.assertEqual(main.status, BranchStatus.ACTIVE)
        self.assertTrue(main.is_main)
        self.assertTrue(
            Branch.all_objects.filter(
                tenant=self.alpha, is_main=True,
                status__in=Branch.IN_SERVICE_STATES,
            ).exists()
        )

        # Why the refusal has to exist: written straight to the table, past
        # transition(), the closed main branch cannot hand the flag over. The
        # model guard refuses it, and so does the database.
        Branch.all_objects.filter(pk=main.pk).update(status=BranchStatus.CLOSED)
        other.is_main = True
        with self.assertRaises(DjangoValidationError):
            other.save()
        with self.assertRaises(IntegrityError), transaction.atomic():
            Branch.all_objects.filter(pk=other.pk).update(is_main=True)

    # --- the Python guards agree with the database --------------------------

    def test_the_model_guard_refuses_a_second_main_before_the_database_does(self):
        """``save()`` should fail as a field error, not as a 500 IntegrityError."""
        self._insert(self._branch(self.alpha, code=1, is_main=True, name="HQ"))

        second = self._branch(self.alpha, code=2, is_main=True, name="Lekki")
        with self.assertRaises(DjangoValidationError) as caught:
            second.save()
        self.assertIn("is_main", caught.exception.message_dict)


class MainBranchLifecycleGuardTests(TestCase):
    """A tenant may never be left without an in-service main branch.

    The guard lives in ``Branch.transition`` and nowhere else, because every
    route to a branch's status runs through it: the ``mark_*`` helpers, the
    API's transition serializer, a management command, the shell. These tests
    therefore drive the model directly, and the API-level test in
    ``schools.vs_schools`` proves the same refusal arrives as a 409.
    """

    def setUp(self):
        self.tenant = Tenant.objects.create(
            name="Corona Secondary School",
            slug="corona-guard",
            kind=Tenant.Kind.SCHOOL,
        )
        self.solo = Tenant.objects.create(
            name="One Site School", slug="one-site-guard", kind=Tenant.Kind.SCHOOL,
        )

    def _branch(self, tenant, *, name, is_main=False, status=BranchStatus.ACTIVE):
        return Branch.all_objects.create(
            tenant=tenant, name=name, is_main=is_main,
            _type="Main" if is_main else "Sub", status=status,
        )

    # --- the refusal --------------------------------------------------------

    def test_every_out_of_service_state_is_refused_for_the_main_branch(self):
        """CLOSED is the permanent one; the other two are wrong straight away.

        A suspended or deactivated main branch is not a dead end - it can be
        reversed - but while it lasts, ``School.main_branch`` and every
        default-branch pick name a site nobody may be posted to. The set is
        read off ``OUT_OF_SERVICE_STATES`` so a state added later is guarded
        the moment it is declared out of service.
        """
        for state in sorted(Branch.OUT_OF_SERVICE_STATES):
            with self.subTest(state=state):
                vi = self._branch(self.tenant, name=f"VI {state}", is_main=True)
                self._branch(self.tenant, name=f"Lekki {state}")

                with self.assertRaises(MainBranchCannotLeaveService) as caught:
                    vi.transition(to_state=state, actor_id="1", reason="shut")

                self.assertEqual(
                    caught.exception.error_code, "MAIN_BRANCH_CANNOT_LEAVE_SERVICE",
                )
                self.assertEqual(caught.exception.http_status, 409)
                self.assertIn("main branch", caught.exception.message)
                vi.refresh_from_db()
                self.assertEqual(vi.status, BranchStatus.ACTIVE)
                # No lifecycle row either: the refusal happens before the write.
                self.assertFalse(vi.lifecycle_events.exists())

                Branch.all_objects.filter(tenant=self.tenant).delete()

    def test_the_only_branch_is_refused_with_advice_it_can_follow(self):
        """There is no sibling to promote, so the message must not say so.

        Bright Star School has one campus. Telling its admin to "make another
        branch the main branch first" is advice for a school that does not
        exist; what they actually want is to deactivate the school.
        """
        only = self._branch(self.solo, name="Bright Star Main", is_main=True)

        with self.assertRaises(LastBranchCannotLeaveService) as caught:
            only.transition(to_state=BranchStatus.CLOSED, actor_id="1", reason="shut")

        self.assertEqual(
            caught.exception.error_code, "LAST_BRANCH_CANNOT_LEAVE_SERVICE",
        )
        self.assertIn("only branch", caught.exception.message)
        self.assertNotIn("another branch", caught.exception.message)
        only.refresh_from_db()
        self.assertEqual(only.status, BranchStatus.ACTIVE)

    def test_a_non_main_branch_still_closes_normally(self):
        """The guard is scoped to ``is_main``; ordinary sites are untouched."""
        self._branch(self.tenant, name="Victoria Island", is_main=True)
        lekki = self._branch(self.tenant, name="Lekki")

        lekki.transition(to_state=BranchStatus.CLOSED, actor_id="1", reason="shut")

        lekki.refresh_from_db()
        self.assertEqual(lekki.status, BranchStatus.CLOSED)
        self.assertIsNotNone(lekki.closed_at)
        self.assertEqual(
            list(lekki.lifecycle_events.values_list("to_state", flat=True)),
            [BranchStatus.CLOSED],
        )

    def test_a_non_main_branch_may_be_suspended_and_deactivated(self):
        self._branch(self.tenant, name="Victoria Island", is_main=True)
        lekki = self._branch(self.tenant, name="Lekki")

        lekki.suspend(actor_id="1", reason="renovation")
        self.assertEqual(lekki.status, BranchStatus.SUSPENDED)
        lekki.mark_inactive(actor_id="1", reason="mothballed")
        self.assertEqual(lekki.status, BranchStatus.INACTIVE)

    def test_an_impossible_edge_is_still_reported_as_an_invalid_transition(self):
        """The edge check runs first: a CLOSED branch is not a main-branch problem."""
        self._branch(self.tenant, name="Victoria Island", is_main=True)
        lekki = self._branch(self.tenant, name="Lekki", status=BranchStatus.CLOSED)

        with self.assertRaises(InvalidBranchTransition):
            lekki.transition(to_state=BranchStatus.ACTIVE, actor_id="1")

    # --- the way out --------------------------------------------------------

    def test_promotion_hands_the_flag_over_and_the_close_then_succeeds(self):
        """The sequence the refusal message tells an admin to perform.

        Corona shuts Victoria Island. The admin makes Lekki the main branch,
        which demotes Victoria Island in the same transaction, and only then is
        the closure allowed.
        """
        vi = self._branch(self.tenant, name="Victoria Island", is_main=True)
        lekki = self._branch(self.tenant, name="Lekki")

        lekki.promote_to_main(actor_id="1")

        lekki.refresh_from_db()
        vi.refresh_from_db()
        self.assertTrue(lekki.is_main)
        self.assertFalse(vi.is_main)
        self.assertEqual(
            Branch.all_objects.filter(tenant=self.tenant, is_main=True).count(), 1,
        )

        vi.transition(to_state=BranchStatus.CLOSED, actor_id="1", reason="shut")
        vi.refresh_from_db()
        self.assertEqual(vi.status, BranchStatus.CLOSED)
        # And the school still has an in-service main branch, which was the
        # whole point.
        self.assertTrue(
            Branch.all_objects.filter(
                tenant=self.tenant, is_main=True,
                status__in=Branch.IN_SERVICE_STATES,
            ).exists()
        )

    def test_promotion_does_not_trip_the_partial_unique_index(self):
        """Demote-then-promote, in that order, inside one transaction.

        ``uq_branch_one_main_per_tenant`` is not deferrable, so promoting first
        would raise IntegrityError even though the end state is legal.
        """
        self._branch(self.tenant, name="Victoria Island", is_main=True)
        lekki = self._branch(self.tenant, name="Lekki")

        with transaction.atomic():
            lekki.promote_to_main(actor_id="1")

        self.assertEqual(
            list(
                Branch.all_objects
                .filter(tenant=self.tenant, is_main=True)
                .values_list("name", flat=True)
            ),
            ["Lekki"],
        )

    def test_promoting_the_branch_that_is_already_main_is_a_no_op(self):
        vi = self._branch(self.tenant, name="Victoria Island", is_main=True)
        self._branch(self.tenant, name="Lekki")

        vi.promote_to_main(actor_id="1")

        self.assertTrue(Branch.all_objects.get(pk=vi.pk).is_main)
        self.assertEqual(
            Branch.all_objects.filter(tenant=self.tenant, is_main=True).count(), 1,
        )

    def test_an_out_of_service_branch_cannot_be_promoted(self):
        """Otherwise the dead end is rebuilt by hand."""
        self._branch(self.tenant, name="Victoria Island", is_main=True)
        closed = self._branch(
            self.tenant, name="Lekki", status=BranchStatus.CLOSED,
        )

        with self.assertRaises(BranchNotInService) as caught:
            closed.promote_to_main(actor_id="1")

        self.assertEqual(caught.exception.error_code, "BRANCH_NOT_IN_SERVICE")
        self.assertTrue(
            Branch.all_objects.get(tenant=self.tenant, name="Victoria Island").is_main
        )

    def test_promotion_never_reaches_across_tenants(self):
        """Another school's main branch is not this school's incumbent."""
        rival_main = self._branch(self.solo, name="Rival Main", is_main=True)
        self._branch(self.tenant, name="Victoria Island", is_main=True)
        lekki = self._branch(self.tenant, name="Lekki")

        lekki.promote_to_main(actor_id="1")

        rival_main.refresh_from_db()
        self.assertTrue(rival_main.is_main)


class TenantSlugRuleTests(TestCase):
    """The slug is a hostname, so it is neither free-form nor editable for ever.

    Every school is served from its own subdomain
    (``bright-star.xvs.codexng.com``, per the CORS origin regex in
    ``settings.base``), which makes ``Tenant.slug`` the sign-in address rather
    than an identifier. Both rules are enforced in ``Tenant.save`` because
    ``Tenant.objects.create()`` is how every writer in this codebase makes a
    tenant, and field validators never run on that path.
    """

    def _tenant(self, **kwargs):
        defaults = {
            "name": "Some School",
            "kind": Tenant.Kind.SCHOOL,
            "status": Tenant.Status.PENDING,
        }
        defaults.update(kwargs)
        return Tenant.objects.create(**defaults)

    # --- reserved slugs -----------------------------------------------------

    def test_every_reserved_slug_is_refused_at_creation(self):
        for slug in sorted(RESERVED_TENANT_SLUGS):
            with self.subTest(slug=slug):
                with self.assertRaises(DjangoValidationError) as caught:
                    self._tenant(slug=slug)
                self.assertIn("slug", caught.exception.message_dict)

    def test_the_named_infrastructure_hosts_are_all_covered(self):
        """The list the platform actually answers on, spelled out.

        A regression here is silent and expensive: a school called "Support
        Academy" auto-slugged to ``support`` would be served the help site.
        """
        required = {
            "www", "api", "admin", "app", "mail", "static", "cdn", "assets",
            "media", "docs", "help", "support", "status", "portal", "blog",
            "dev", "staging", "test", "xvs",
        }
        self.assertTrue(required <= set(RESERVED_TENANT_SLUGS))

    def test_a_reserved_slug_is_refused_on_update_too(self):
        tenant = self._tenant(slug="bright-star-reserve")

        tenant.slug = "portal"
        with self.assertRaises(DjangoValidationError):
            tenant.save()

        self.assertEqual(
            Tenant.objects.get(pk=tenant.pk).slug, "bright-star-reserve",
        )

    def test_a_reserved_slug_is_refused_however_it_is_cased_or_spaced(self):
        with self.assertRaises(DjangoValidationError):
            self._tenant(slug="  API  ")

    def test_an_ordinary_slug_is_accepted(self):
        tenant = self._tenant(slug="bright-star-academy")

        self.assertEqual(tenant.slug, "bright-star-academy")

    def test_the_platform_tenant_keeps_its_reserved_name(self):
        """``codex`` is reserved *because* the platform tenant answers on it."""
        codex = Tenant.objects.get(slug="codex", kind=Tenant.Kind.PLATFORM)

        codex.name = "CodeX Renamed"
        codex.save()

        self.assertEqual(Tenant.objects.get(pk=codex.pk).slug, "codex")

    def test_a_school_cannot_be_created_on_a_reserved_slug(self):
        """The check reaches the school app for free.

        ``School.save()`` creates the tenant, so the platform guard fires
        before the school row is written and no half-made school survives.
        """
        with self.assertRaises(DjangoValidationError):
            School.objects.create(
                name="Support Academy", slug="support", code="SC-RSV1",
                status=SchoolStatus.PENDING,
            )

        self.assertFalse(School.objects.filter(name="Support Academy").exists())

    # --- immutability once live ---------------------------------------------

    def test_a_school_that_has_never_been_live_may_fix_a_typo(self):
        tenant = self._tenant(slug="corona-secondry")

        tenant.slug = "corona-secondary"
        tenant.save()

        self.assertEqual(
            Tenant.objects.get(pk=tenant.pk).slug, "corona-secondary",
        )

    def test_a_live_school_may_not_move_its_sign_in_address(self):
        tenant = self._tenant(slug="corona-live")
        tenant.activate()
        tenant.save()

        tenant.slug = "corona-secondary-school"
        with self.assertRaises(DjangoValidationError) as caught:
            tenant.save()

        self.assertIn("slug", caught.exception.message_dict)
        self.assertEqual(Tenant.objects.get(pk=tenant.pk).slug, "corona-live")

    def test_a_suspended_school_that_was_once_live_is_still_frozen(self):
        """The case ``status == ACTIVE`` would have got wrong.

        Corona falls behind on its invoice and is suspended. Its address was
        published to every parent last September and it comes back the moment
        the invoice is paid, so it is exactly as fixed as it was yesterday.
        """
        tenant = self._tenant(slug="corona-suspended")
        tenant.activate()
        tenant.save()
        Tenant.objects.filter(pk=tenant.pk).update(
            status=Tenant.Status.SUSPENDED,
        )
        tenant.refresh_from_db()

        tenant.slug = "corona-renamed"
        with self.assertRaises(DjangoValidationError):
            tenant.save()

    def test_a_live_school_may_still_be_saved_for_anything_else(self):
        """The freeze is on the slug, not on the row."""
        tenant = self._tenant(slug="corona-editable")
        tenant.activate()
        tenant.save()

        tenant.name = "Corona Secondary School, Victoria Island"
        tenant.save()

        self.assertEqual(
            Tenant.objects.get(pk=tenant.pk).name,
            "Corona Secondary School, Victoria Island",
        )

    def test_a_pending_school_correcting_its_slug_moves_the_sign_in_address(self):
        """The school's own slug and the tenant's must not drift apart.

        Fixing ``corona-secondry`` on the school row alone would leave the
        school addressed correctly in the API and still served at the misspelt
        host, which is the only address its admins actually type.
        """
        school = School.objects.create(
            name="Corona Secondary", slug="corona-secondry", code="SC-TYP1",
            status=SchoolStatus.PENDING,
        )

        school.slug = "corona-secondary-fixed"
        school.save()

        school.refresh_from_db()
        self.assertEqual(school.slug, "corona-secondary-fixed")
        self.assertEqual(school.tenant.slug, "corona-secondary-fixed")

    def test_a_live_school_row_refuses_the_rename_as_well(self):
        school = School.objects.create(
            name="Live Academy", slug="live-academy-frozen", code="SC-TYP2",
            status=SchoolStatus.ACTIVE, activated_at=timezone.now(),
        )

        school.slug = "live-academy-renamed"
        with self.assertRaises(DjangoValidationError):
            school.save()

        school.refresh_from_db()
        self.assertEqual(school.slug, "live-academy-frozen")
        self.assertEqual(school.tenant.slug, "live-academy-frozen")

    def test_an_ordinary_save_never_moves_a_live_schools_sign_in_address(self):
        """Legacy divergence must not be "fixed" by a metadata edit.

        ``School.save()`` did not use to mirror the slug, so a school row and
        its tenant row could hold different ones. Mirroring unconditionally
        would mean the next save of an unrelated field - a motto, a website -
        silently moved a live school's sign-in address to the school row's
        value. The mirror is therefore only applied before go-live.
        """
        school = School.objects.create(
            name="Diverged Academy", slug="diverged-academy", code="SC-DIV1",
            status=SchoolStatus.ACTIVE, activated_at=timezone.now(),
        )
        Tenant.objects.filter(pk=school.tenant_id).update(slug="diverged-legacy")

        school.motto = "Onward"
        school.save()

        self.assertEqual(
            Tenant.objects.get(pk=school.tenant_id).slug, "diverged-legacy",
        )

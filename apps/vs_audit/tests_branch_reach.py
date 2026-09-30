"""A branch-bound audit reader, in the platform audit trail.

Lagoon View runs Ikeja, Lekki and Yaba. Ngozi keeps Lekki's books, pinned to
Lekki, and holds the platform audit keys. The platform trail keeps a copy of
every finance and procurement entry, and Ngozi reads those copies the way they
read the finance trail itself: "Disbursed Lekki Branch's net wages" and
nothing of Ikeja's, and never an old copy carrying no branch, such as the
whole school's "Accrued payroll: gross 190000, net 171000". A sign-in event
belongs to no document and is read as before.

Mr Bello reads the whole school, Codex staff read across tenants, and the
auditor at a rival school reads their own school only: none of them is
narrowed by this.
"""
from __future__ import annotations

import csv
import io

from django.contrib.auth import get_user_model
from django.core.files.storage import default_storage
from django.test import TestCase

from core.test_utils import TenantAPIClient
from vs_rbac.tests.helpers import make_branch, make_school
from vs_tenants.models import Tenant

from .models import AuditEvent
from .services import emit_audit_event

#: What Ngozi must never read: Ikeja's pay, the school's totals, other branches' names.
NOT_LEKKIS = ("Ikeja", "Yaba", "45000", "190000", "171000")

AUDIT_KEYS = ("platform.audit.view", "platform.audit.export")


def _grant(user, tenant, role_key, *, branch=None):
    """A real role grant of the audit keys, optionally pinned to one branch."""
    from vs_rbac.models import (
        Permission, PermissionAction, PermissionModule, PermissionResource,
        TenantRolePermission, TenantRoleTemplate, TenantUserRoleAssignment,
    )
    from vs_rbac.tests.helpers import scope_for_key

    role, _ = TenantRoleTemplate.objects.get_or_create(
        tenant=tenant, key=role_key, defaults={"name": role_key, "status": "ACTIVE"},
    )
    for key in AUDIT_KEYS:
        module_name, resource_name, action_name = key.split(".")
        module, _ = PermissionModule.objects.get_or_create(name=module_name)
        resource, _ = PermissionResource.objects.get_or_create(module=module, name=resource_name)
        action, _ = PermissionAction.objects.get_or_create(name=action_name)
        permission, _ = Permission.objects.get_or_create(
            key=key, defaults={"module": module, "resource": resource, "action": action,
                               "scope": scope_for_key(key)},
        )
        TenantRolePermission.objects.get_or_create(role=role, permission=permission, defaults={"granted": True})
    TenantUserRoleAssignment.objects.get_or_create(
        tenant=tenant, user=user, role=role,
        defaults={"assignment_status": "ACTIVE", "branch": branch},
    )
    return user


class _BranchReachFixture(TestCase):

    def setUp(self):
        self.school = make_school(slug="lagoon-audit-reach", name="Lagoon View", status="ACTIVE")
        self.tenant = self.school.tenant
        self.ikeja = make_branch(self.school, name="Ikeja Branch")
        self.lekki = make_branch(self.school, name="Lekki Branch", is_main=False)
        self.yaba = make_branch(self.school, name="Yaba Branch", is_main=False)
        self.rival_school = make_school(slug="rival-audit-reach", name="Rival Group", status="ACTIVE")
        self.rival_tenant = self.rival_school.tenant
        self.rival_branch = make_branch(self.rival_school, name="Rival Main")

        self.ngozi_user = self.person("ngozi@lagoon.test", self.tenant, "lekki-audit", branch=self.lekki)
        self.ngozi = TenantAPIClient(self.ngozi_user)
        self.bello = TenantAPIClient(self.person("bello@lagoon.test", self.tenant, "school-audit"))
        self.rival = TenantAPIClient(self.person("auditor@rival.test", self.rival_tenant, "rival-audit"))
        codex = Tenant.objects.get(slug="codex", kind=Tenant.Kind.PLATFORM)
        self.codex = TenantAPIClient(self.person("auditor@codex.test", codex, "codex-audit"))

        self.lekki_paid = self.finance("Disbursed Lekki Branch's net wages 72000 kobo.", self.lekki)
        self.ikeja_paid = self.finance("Disbursed Ikeja Branch's net wages 45000 kobo.", self.ikeja)
        self.old_total = self.finance("Accrued payroll: gross 190000, net 171000 kobo.", None)
        self.sign_in = emit_audit_event(
            module_key="IDENTITY", action_type="LOGIN_SUCCESS", entity_type="User",
            entity_id=str(self.ngozi_user.pk), tenant=self.tenant, summary="A sign-in.",
        )
        self.rival_paid = self.finance("Disbursed Rival Main's net wages 9000 kobo.", self.rival_branch,
                                       tenant=self.rival_tenant, entity_id="77")

    def person(self, email, tenant, role_key, *, branch=None):
        user = get_user_model().objects.create_user(
            email=email, password="pw", tenant=tenant, branch=None, status="ACTIVE",
            first_name="Audit", last_name="Reader",
        )
        return _grant(user, tenant, role_key, branch=branch)

    def finance(self, summary, branch, *, tenant=None, entity_id=None):
        """A platform copy of a finance entry, about a payroll run."""
        entity_id = entity_id or {self.lekki: "11", self.ikeja: "12"}.get(branch, "13")
        event = emit_audit_event(
            module_key="FINANCE", action_type="FINANCIAL_TRANSACTION",
            entity_type="vs_finance.PayrollRun", entity_id=entity_id, entity_label=f"PL-{entity_id}",
            tenant=tenant or self.tenant, summary=summary, branch=branch,
        )
        self.assertIsNotNone(event)
        return event

    def events(self, client, **params):
        response = client.get("/v1/audit/events/", {"page_size": 100, **params})
        self.assertEqual(response.status_code, 200, response.data)
        return response.data

    def ids(self, client, **params):
        return {row["id"] for row in self.events(client, **params)["data"]}

    def lagoon_ids(self):
        return {str(e.id) for e in (self.lekki_paid, self.ikeja_paid, self.old_total, self.sign_in)}


class ALekkiReaderReadsLekkisCopiesTests(_BranchReachFixture):

    def test_they_see_lekkis_copy_and_no_ikeja_or_unbranched_one(self):
        seen = self.ids(self.ngozi, module_key="FINANCE")

        self.assertEqual(seen, {str(self.lekki_paid.id)})

    def test_nothing_of_ikejas_pay_or_the_school_totals_reaches_them(self):
        payload = self.events(self.ngozi)

        for leaked in NOT_LEKKIS:
            self.assertNotIn(leaked, str(payload))

    def test_another_branchs_copy_by_id_is_not_found(self):
        self.assertEqual(self.ngozi.get(f"/v1/audit/events/{self.lekki_paid.id}/").status_code, 200)
        for event in (self.ikeja_paid, self.old_total):
            with self.subTest(event=event.summary):
                self.assertEqual(self.ngozi.get(f"/v1/audit/events/{event.id}/").status_code, 404)

    def test_counts_and_trails_say_nothing_of_other_branches(self):
        self.assertEqual(self.events(self.ngozi, module_key="FINANCE")["pagination"]["totalItems"], 1)

        dashboard = self.ngozi.get("/v1/audit/dashboard-summary/").data["data"]
        finance = [row["count"] for row in dashboard["module_breakdown"] if row["module_key"] == "FINANCE"]
        self.assertEqual(finance, [1])

        trails = self.ngozi.get("/v1/audit/entity-trails/", {"page_size": 100}).data["data"]
        self.assertEqual(
            {row["entity_id"] for row in trails if row["entity_type"] == "vs_finance.PayrollRun"}, {"11"},
        )
        self.assertEqual(
            self.ngozi.get("/v1/audit/entity-trails/vs_finance.PayrollRun/12/").status_code, 404,
        )

    def test_their_exports_carry_lekkis_copy_only(self):
        from vs_exports.catalogue import ScopeContext, get_dataset

        created = self.ngozi.post(
            "/v1/audit/exports/", {"filter_payload": {"module_key": ["FINANCE"]}}, format="json",
        )
        self.assertEqual(created.status_code, 201, created.data)
        from .models import AuditExportJob

        job = AuditExportJob.objects.get(pk=created.data["data"]["id"])
        with default_storage.open(job.file_path) as handle:
            rows = list(csv.reader(io.StringIO(handle.read().decode("utf-8-sig"))))
        self.assertEqual({row[0] for row in rows[1:]}, {str(self.lekki_paid.id)})

        dataset = get_dataset("audit.events")
        exported = dataset.base(ScopeContext(tenant=self.tenant, user=self.ngozi_user))
        self.assertEqual(
            set(exported.filter(module_key="FINANCE").values_list("id", flat=True)), {self.lekki_paid.id},
        )

    def test_events_of_other_modules_are_read_as_before(self):
        self.assertIn(str(self.sign_in.id), self.ids(self.ngozi))

    def test_another_modules_event_about_an_ikeja_document_stays_ikejas(self):
        ikeja_post = self.unapproved_post(self.ikeja, "21")
        lekki_post = self.unapproved_post(self.lekki, "22")

        seen = self.ids(self.ngozi, module_key="WORKFLOW")

        self.assertEqual(seen, {str(lekki_post.id)})
        self.assertEqual(self.ngozi.get(f"/v1/audit/events/{ikeja_post.id}/").status_code, 404)
        self.assertEqual(
            self.ids(self.bello, module_key="WORKFLOW"), {str(ikeja_post.id), str(lekki_post.id)},
        )

    def unapproved_post(self, branch, entity_id):
        """The record that a branch's payout went out with no approval configured."""
        return emit_audit_event(
            module_key="WORKFLOW", action_type="POSTED_WITHOUT_APPROVAL",
            entity_type="PayoutRun", entity_id=entity_id, tenant=self.tenant,
            summary=f"PayoutRun {entity_id} posted with no approval stages configured.",
            branch=branch,
        )


class OtherReadersAreUnchangedTests(_BranchReachFixture):

    def test_the_whole_school_reader_sees_every_copy(self):
        self.assertTrue(self.lagoon_ids() <= self.ids(self.bello))
        self.assertEqual(self.bello.get(f"/v1/audit/events/{self.old_total.id}/").status_code, 200)

    def test_codex_staff_read_across_tenants_unnarrowed(self):
        seen = self.ids(self.codex, module_key="FINANCE")

        self.assertTrue({str(self.ikeja_paid.id), str(self.old_total.id), str(self.rival_paid.id)} <= seen)

    def test_another_tenants_auditor_reads_their_own_school_only(self):
        seen = self.ids(self.rival)

        self.assertIn(str(self.rival_paid.id), seen)
        self.assertFalse(self.lagoon_ids() & seen)
        self.assertEqual(self.rival.get(f"/v1/audit/events/{self.lekki_paid.id}/").status_code, 404)


class TheFinanceCopyCarriesTheEntrysBranchTests(_BranchReachFixture):

    def test_a_finance_entry_is_copied_with_its_branch(self):
        from vs_finance.audit import record
        from vs_finance.constants import FinanceAuditAction
        from vs_finance.models import LedgerEntity

        books = LedgerEntity.objects.create(
            name="Lagoon Books", code="LAGREACH", kind=LedgerEntity.Kind.TENANT, tenant=self.tenant,
        )
        entry = record(
            entity=books, action=FinanceAuditAction.FINANCE_SETTINGS_UPDATED, branch=self.lekki,
            target_type="PayrollRun", target_id="21", message="copied",
        )

        copy = AuditEvent.objects.get(entity_type="vs_finance.PayrollRun", entity_id="21")
        self.assertEqual((entry.branch_id, copy.branch_id), (self.lekki.pk, self.lekki.pk))

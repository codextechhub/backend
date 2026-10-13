"""Field Access on the purchase order approval card.

An order carries the payment terms of the vendor it was raised for, a field a
school can close per role on the vendor record
(``procurement.vendor.payment_terms``). The approval card restates it, so an
approver whose role may not read the vendor's terms is not shown them either.
The vendor's code and name, the order's amount and its lines have no registered
field behind them and always show.

Corona Group has several branches, Single Site one, and Rival Group is a third
school whose roles and orders never meet these.
"""
from __future__ import annotations

import datetime
import itertools
import json

from django.contrib.contenttypes.models import ContentType

from core.test_utils import TenantAPIClient
from vs_finance.tests_branch_scope import _FinanceBranchFixture
from vs_rbac.models import TenantRoleTemplate
from vs_rbac.tests.helpers import install_declared_fields, set_field_access
from vs_workflow.constants import WorkflowInstanceStatus
from vs_workflow.handlers import get_handler
from vs_workflow.models import WorkflowInstance, WorkflowTemplate
from vs_workflow.services.submission import document_snapshot

from .constants import WF_DOCTYPE_PURCHASE_ORDER
from .models import PurchaseOrder, Vendor
from .workflow_handlers import VENDOR_PAYMENT_TERMS

_seq = itertools.count(1)


class PurchaseOrderCardFieldAccessTests(_FinanceBranchFixture):

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        install_declared_fields("procurement.vendor")
        cls.orders = {}
        for shape, books, branch, tenant in (
            ("multi", cls.books, cls.ikeja, cls.tenant),
            ("solo", cls.solo_books, cls.solo_main, cls.solo_tenant),
            ("rival", cls.rival_books, cls.rival_branch, cls.rival_tenant),
        ):
            vendor = Vendor.objects.create(
                entity=books, code=f"ADE-{shape}", name=f"Ade Stationers {shape}",
                payment_terms="NET_30",
            )
            order = PurchaseOrder.objects.create(
                entity=books, branch=branch, vendor=vendor,
                order_date=datetime.date(2026, 1, 5), payment_terms="NET_30",
                status="PENDING_APPROVAL",
            )
            cls.orders[shape] = (order, tenant)
        cls.requesters = {
            tenant: cls.user_for(tenant, f"requester@{shape}-po.test")
            for shape, (_, tenant) in cls.orders.items()
        }

    @classmethod
    def approver(cls, tenant, *, hide=()):
        name = f"po-approver-{next(_seq)}"
        user = cls.grant(
            cls.user_for(tenant, f"{name}@po.test"), "workflow.instance.view",
            tenant=tenant, role_key=name,
        )
        role = TenantRoleTemplate.objects.get(tenant=tenant, key=name)
        keys = install_declared_fields("procurement.vendor")
        set_field_access(role, *keys, read=True, write=True)
        if hide:
            set_field_access(role, *hide, read=False, write=False)
        return user

    def request_for(self, shape):
        order, tenant = self.orders[shape]
        summary, details = document_snapshot(
            get_handler(WF_DOCTYPE_PURCHASE_ORDER), order, WF_DOCTYPE_PURCHASE_ORDER)
        template = WorkflowTemplate.objects.create(
            tenant=tenant, branch=None, document_type=WF_DOCTYPE_PURCHASE_ORDER,
            code=f"po-{next(_seq)}", name="Order route")
        return WorkflowInstance.all_objects.create(
            tenant=tenant, branch=order.branch, template=template,
            document_content_type=ContentType.objects.get_for_model(PurchaseOrder),
            document_object_id=str(order.pk), document_type=WF_DOCTYPE_PURCHASE_ORDER,
            status=WorkflowInstanceStatus.IN_PROGRESS, requested_by=self.requesters[tenant],
            document_summary=summary, document_details=details)

    def read(self, user, instance):
        response = TenantAPIClient(user=user).get(f"/v1/workflow/instances/{instance.pk}/")
        self.assertEqual(response.status_code, 200, response.data)
        return response.data

    @staticmethod
    def labels(card) -> set:
        found = {f["label"] for f in card["document_summary"].get("fields", [])}
        for section in card["document_details"]["sections"]:
            if section["kind"] == "fields":
                found.update(item["label"] for item in section["items"])
        return found

    def test_an_approver_who_may_read_the_vendors_terms_sees_them(self):
        for shape in ("multi", "solo"):
            with self.subTest(shape=shape):
                card = self.read(self.approver(self.orders[shape][1]), self.request_for(shape))
                self.assertIn("Payment terms", self.labels(card))
                self.assertIn("NET_30", json.dumps(card))

    def test_closing_the_vendors_payment_terms_removes_the_row_and_nothing_else(self):
        for shape in ("multi", "solo"):
            with self.subTest(shape=shape):
                tenant = self.orders[shape][1]
                instance = self.request_for(shape)
                open_card = self.read(self.approver(tenant), instance)
                closed_card = self.read(self.approver(tenant, hide=[VENDOR_PAYMENT_TERMS]), instance)
                self.assertEqual(
                    self.labels(closed_card), self.labels(open_card) - {"Payment terms"})
                self.assertNotIn("NET_30", json.dumps(closed_card))

    def test_the_vendor_and_the_amount_show_whatever_the_switches_say(self):
        every = [f"procurement.vendor.{name}" for name in (
            "payment_terms", "bank_account_name", "bank_account_number", "bank_code",
            "email", "tax_id")]
        for shape in ("multi", "solo"):
            with self.subTest(shape=shape):
                card = self.read(
                    self.approver(self.orders[shape][1], hide=every), self.request_for(shape))
                summary = {f["label"]: f["value"] for f in card["document_summary"]["fields"]}
                self.assertIn(f"Ade Stationers {shape}", summary["Vendor"])
                self.assertIn("Amount", summary)

    def test_the_stored_snapshot_names_the_key_that_governs_the_terms(self):
        instance = self.request_for("multi")
        stored = WorkflowInstance.all_objects.get(pk=instance.pk).document_details
        items = [i for s in stored["sections"] if s["kind"] == "fields" for i in s["items"]]
        terms = next(i for i in items if i["label"] == "Payment terms")
        self.assertEqual(terms["access"], VENDOR_PAYMENT_TERMS)
        self.assertEqual(terms["value"], "NET_30")

    def test_one_schools_switch_does_not_reach_another_and_its_order_is_not_served(self):
        closed = self.approver(self.tenant, hide=[VENDOR_PAYMENT_TERMS])
        open_rival = self.approver(self.rival_tenant)
        self.assertNotIn("NET_30", json.dumps(self.read(closed, self.request_for("multi"))))
        rival_instance = self.request_for("rival")
        self.assertIn("NET_30", json.dumps(self.read(open_rival, rival_instance)))
        refused = TenantAPIClient(user=closed).get(f"/v1/workflow/instances/{rival_instance.pk}/")
        self.assertEqual(refused.status_code, 404, refused.data)

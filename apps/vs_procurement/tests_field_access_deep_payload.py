"""No vendor value or contact detail reaches a caller who may read none of them.

The declared surfaces of ``procurement.vendor`` and ``procurement.vendor_contact``
are rendered on a real supplier whose contact, tax, bank, category, terms and
compliance values are all filled in, with a contact person who has a name,
email, phone and routing flags and a quotation request that was sent to that person, as a
caller whose role has every field of the resource switched off. The whole
payload is walked.

The vendor's own render keeps the contact people nested under it, which are
held to their own resource's switches rather than the vendor's: the vendor
case therefore lists the contact's email and phone as names that belong to
something else, and the contact surfaces are rendered separately as a caller
who may read none of the contact's details or routing flags.

Two tenants: one with two branches and one with a single branch.
"""
from __future__ import annotations

import datetime

from django.test import TestCase

from vs_finance.models import LedgerEntity
from vs_finance.seed import seed_chart_of_accounts, seed_currencies
from vs_procurement.models import (
    RequestForQuotation,
    RfqInvitation,
    RfqInvitationRecipient,
    Vendor,
    VendorCategory,
    VendorContact,
)
from vs_rbac.tests.deep_payload import DeepPayloadChecks, Sample
from vs_rbac.tests.helpers import make_branch, make_school

_VENDOR = "vs_procurement.serializers.VendorSerializer"
_VENDOR_LIST = "vs_procurement.serializers.VendorListSerializer"
_CONTACT = "vs_procurement.serializers.VendorContactSerializer"
_RECIPIENT = "vs_procurement.serializers.RfqInvitationRecipientSerializer"
_APPROVAL_RECEIPT = "vs_procurement.views.approval_queue.ProcurementApprovalHistoryReceiptSerializer"
_NESTED_REASON = (
    "The contact person's own detail, a procurement.vendor_contact field carried "
    "under the vendor and held to that resource's switches, not the vendor's."
)


class VendorDeepPayloadTests(DeepPayloadChecks, TestCase):
    resources = frozenset({
        "procurement.approval", "procurement.vendor", "procurement.vendor_contact",
    })
    covers = frozenset({_APPROVAL_RECEIPT, _VENDOR, _VENDOR_LIST, _CONTACT, _RECIPIENT})
    allowlist = {
        (_VENDOR, "contacts[].email"): _NESTED_REASON,
        (_VENDOR, "contacts[].phone"): _NESTED_REASON,
    }

    @classmethod
    def setUpTestData(cls):
        seed_currencies()
        multi = make_school(slug="deep-proc-multi", name="Corona Group").tenant
        make_branch(multi, name="Ikeja Branch")
        make_branch(multi, name="Lekki Branch", is_main=False)
        solo = make_school(slug="deep-proc-solo", name="Single Site").tenant
        make_branch(solo, name="Main Branch")
        cls.tenant = multi
        cls.vendors = [(multi, cls._vendor(multi, "DPPMULTI")), (solo, cls._vendor(solo, "DPPSOLO"))]

    @classmethod
    def _vendor(cls, tenant, code):
        entity = LedgerEntity.objects.create(
            name=f"{code} Books", code=code, kind=LedgerEntity.Kind.TENANT, tenant=tenant,
        )
        seed_chart_of_accounts(entity)
        category = VendorCategory.objects.create(entity=entity, code="STAT", name="Stationery")
        vendor = Vendor.objects.create(
            entity=entity, code="ADE", name="Ade Stationers",
            email="accounts@ade.example", phone="08030000000",
            address="12 Broad Street, Lagos", tax_id="TIN-2233",
            bank_name="GTBank", bank_code="058",
            bank_account_number="0123456789", bank_account_name="Ade Stationers Ltd",
            category=category, payment_terms="NET_30", kyc_status="VERIFIED",
            risk="MEDIUM", on_hold=True,
        )
        contact = VendorContact.objects.create(
            vendor=vendor, name="Amina Bello", email="amina@ade.example",
            phone="08030000001", is_primary=True,
        )
        rfq = RequestForQuotation.objects.create(
            entity=entity, title="Chalk", issue_date=datetime.date(2026, 1, 10),
        )
        invitation = RfqInvitation.objects.create(rfq=rfq, vendor=vendor)
        RfqInvitationRecipient.objects.create(
            invitation=invitation, contact=contact, name=contact.name, email=contact.email,
        )
        return vendor

    def samples(self):
        samples = [Sample(
            _APPROVAL_RECEIPT,
            {
                "reference": "REQ-1042",
                "decision": "APPROVED",
                "stage": "Manager approval",
                "decided_at": "2026-10-10T09:00:00Z",
                "on_behalf_of": "Tunde Bello",
                "reversed_at": "2026-10-11T09:00:00Z",
            },
            tenant=self.tenant,
        )]
        for tenant, vendor in self.vendors:
            samples.append(Sample(
                _VENDOR, Vendor.objects.prefetch_related("contacts").get(pk=vendor.pk),
                tenant=tenant,
            ))
            samples.append(Sample(
                _CONTACT, VendorContact.objects.get(vendor=vendor), tenant=tenant,
            ))
            samples.append(Sample(
                _RECIPIENT, RfqInvitationRecipient.objects.get(invitation__vendor=vendor),
                tenant=tenant,
            ))
        # The vendor list row, rendered as a list is.
        samples.append(Sample(
            _VENDOR_LIST, Vendor.objects.filter(pk=self.vendors[0][1].pk),
            tenant=self.tenant, many=True,
        ))
        return samples

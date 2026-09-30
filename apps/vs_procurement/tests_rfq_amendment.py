"""Publishing an amendment to an issued RFQ, with and without a new deadline.

Harbour Group buys for its Ikeja branch, which keeps Nairobi time. Its RFQ's
deadline is moved to 21:30 UTC on 20 March: 00:30 on the 21st in Nairobi but
still 22:30 on the 20th in Lagos. The instant is stored as sent, its day is
stored as the 21st (the RFQ's own zone, not the school's), and the vendor is
told "21 Mar 2026, 12:30 am EAT".

An amendment that does not name a deadline leaves the stored one exactly as it
was and still tells every invited vendor.
"""
from __future__ import annotations

import datetime
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone

from core.test_utils import TenantAPIClient
from vs_config.clock import TIME_ZONE_KEY, forget_tenant_zone
from vs_config.models import ConfigurationDefinition
from vs_config.services.resolution import set_value
from vs_finance.models import LedgerEntity
from vs_procurement.constants import RfqStatus
from vs_procurement.models import (
    RequestForQuotation,
    RfqAmendment,
    RfqInvitation,
    RfqLine,
    VendorContact,
)
from vs_rbac.tests.helpers import make_branch
from vs_tenants.models import Tenant

from .tests import _P2PFixtureMixin

UTC = datetime.timezone.utc
#: 00:30 on 21 March in Nairobi, 22:30 on 20 March in Lagos.
NEW_DEADLINE = datetime.datetime(2026, 3, 20, 21, 30, tzinfo=UTC)


@patch("vs_rbac.permissions.HasRBACPermission.has_permission", return_value=True)
class RfqAmendmentTests(_P2PFixtureMixin, TestCase):

    def setUp(self):
        self.tenant = Tenant.objects.create(
            name="Harbour Group", slug="harbour-group-rfq", kind=Tenant.Kind.ORGANIZATION,
            status=Tenant.Status.ACTIVE,
        )
        make_branch(self.tenant, name="Lekki Branch", is_main=True)
        self.ikeja = make_branch(self.tenant, name="Ikeja Branch", is_main=False)
        set_value(
            definition=ConfigurationDefinition.objects.get(key=TIME_ZONE_KEY),
            value="Africa/Nairobi", actor=None, branch=self.ikeja,
        )
        forget_tenant_zone(self.tenant)

        self.entity, _, self.vendor, _, _ = self.build_p2p()
        LedgerEntity.objects.filter(pk=self.entity.pk).update(tenant=self.tenant)
        self.entity.refresh_from_db()
        self.vendor.email = "quotes@acme.test"
        self.vendor.save(update_fields=["email", "updated_at"])
        VendorContact.objects.create(
            vendor=self.vendor, name="Amina Vendor", email=self.vendor.email,
            is_primary=True, receives_rfqs=True,
        )
        self.original_deadline = timezone.now() + datetime.timedelta(days=5)
        self.rfq = RequestForQuotation.objects.create(
            entity=self.entity, branch=self.ikeja, title="Laboratory glassware",
            rfq_status=RfqStatus.ISSUED, issue_date=datetime.date(2026, 3, 10),
            response_due_date=datetime.date(2026, 3, 15),
            response_due_at=self.original_deadline,
        )
        RfqLine.objects.create(
            rfq=self.rfq, description="Beakers", quantity=10,
            expense_account=self.acc(self.entity, "5300"), line_no=1,
        )
        invitation = RfqInvitation.objects.create(rfq=self.rfq, vendor=self.vendor)
        invitation.recipients.create(name="Amina Vendor", email=self.vendor.email)

        user = get_user_model().objects.create_user(
            email="buyer@harbour.test", password="pw", tenant=self.tenant,
            status="ACTIVE", first_name="Buyer", last_name="Tester",
        )
        self.client = TenantAPIClient(user=user)

    def publish(self, body):
        with patch("vs_procurement.vendor_portal.send_notification") as send, \
                self.captureOnCommitCallbacks(execute=True):
            response = self.client.post(
                f"/v1/procurement/rfqs/{self.rfq.pk}/amendments/?entity={self.entity.code}",
                body, format="json",
            )
        return response, send

    def test_moving_the_deadline_stores_it_on_the_rfqs_clock_and_tells_the_vendor(self, _perm):
        response, send = self.publish({
            "summary": "Deadline moved for the stocktake", "response_required": False,
            "deadline": NEW_DEADLINE.isoformat(),
        })
        self.assertEqual(response.status_code, 200, response.data)

        self.rfq.refresh_from_db()
        self.assertEqual(self.rfq.response_due_at, NEW_DEADLINE)
        self.assertEqual(self.rfq.response_due_date, datetime.date(2026, 3, 21))
        self.assertEqual(self.rfq.version, 2)
        self.assertTrue(RfqAmendment.objects.filter(rfq=self.rfq, version=2).exists())

        send.assert_called_once()
        kwargs = send.call_args.kwargs
        self.assertEqual(kwargs["event_key"], "procurement.rfq_amended")
        self.assertEqual(kwargs["context"]["deadline"], "21 Mar 2026, 12:30 am EAT")
        self.assertEqual(kwargs["context"]["response_required"], "No")
        self.assertEqual(
            [r.email for r in kwargs["unregistered_recipients"]], ["quotes@acme.test"],
        )

    def test_an_amendment_without_a_deadline_leaves_it_alone(self, _perm):
        response, send = self.publish({"summary": "Clarified the beaker sizes"})
        self.assertEqual(response.status_code, 200, response.data)

        self.rfq.refresh_from_db()
        self.assertEqual(self.rfq.response_due_at, self.original_deadline)
        self.assertEqual(self.rfq.response_due_date, datetime.date(2026, 3, 15))
        self.assertEqual(self.rfq.version, 2)
        send.assert_called_once()
        self.assertEqual(send.call_args.kwargs["context"]["response_required"], "Yes")

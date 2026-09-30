"""A vendor assessment's date is always named, and named on the tenant's day.

The date has no column default: the only default a model can have is the
server's UTC day. A write that names none is refused by the database
(``IntegrityError``, the column is NOT NULL). An assessment recorded at 23:30
UTC on 14 March, when it is already 00:30 on the 15th in Lagos, is dated the
15th when the assessor gives no date.
"""
from __future__ import annotations

import datetime
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.db import IntegrityError, transaction
from django.test import TestCase

from core.test_utils import TenantAPIClient
from vs_procurement.models import VendorAssessment

from .tests import _P2PFixtureMixin

#: The 14th in UTC, the 15th in Lagos.
LATE_EVENING_UTC = datetime.datetime(2026, 3, 14, 23, 30, tzinfo=datetime.timezone.utc)
SCORES = dict(on_time_delivery=85, quality_acceptance=90, invoice_accuracy=85, responsiveness=80)


class VendorAssessmentDateTests(_P2PFixtureMixin, TestCase):

    def setUp(self):
        self.entity, _, self.vendor, _, _ = self.build_p2p()

    def test_an_assessment_without_a_date_is_refused(self):
        with self.assertRaises(IntegrityError), transaction.atomic():
            VendorAssessment.objects.create(entity=self.entity, vendor=self.vendor, **SCORES)

    @patch("vs_rbac.permissions.HasRBACPermission.has_permission", return_value=True)
    def test_an_assessment_given_no_date_is_dated_on_the_tenants_day(self, _perm):
        user = get_user_model().objects.create_user(
            email="assessor@test.com", password="pw", tenant=self.entity.tenant,
            status="ACTIVE", first_name="Assess", last_name="Or",
        )
        with patch("django.utils.timezone.now", return_value=LATE_EVENING_UTC):
            response = TenantAPIClient(user=user).post(
                f"/v1/procurement/vendor-assessments/?entity={self.entity.code}",
                {"vendor": self.vendor.code, **SCORES}, format="json",
            )
        self.assertEqual(response.status_code, 201, response.data)
        row = VendorAssessment.objects.get(entity=self.entity, vendor=self.vendor)
        self.assertEqual(row.assessment_date, datetime.date(2026, 3, 15))

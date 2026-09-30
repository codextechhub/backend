"""Dates in the sentences the student record writes follow the school's settings.

Brightfield writes its dates DD/MM/YYYY. The audit summary of a status change
and of an offer moving an applicant along is a sentence a person reads on the
record's history, so its day is written that way; the audit metadata beside it
keeps ISO, because it is read by code. A school that has chosen nothing reads
the default, "24 Mar 2026".
"""
from __future__ import annotations

import datetime as dt
from unittest import mock
from zoneinfo import ZoneInfo

from vs_audit.models import AuditEvent
from vs_config.clock import forget_tenant_zone
from vs_config.display import DATE_FORMAT_KEY
from vs_config.models import ConfigurationDefinition
from vs_config.services.resolution import set_value

from ..constants import StudentStatus
from ..models import AdmissionStage
from ..services.admission import move_to_stage
from ..services.status import transition
from .base import StudentsFixture

#: Mid-morning on 10 March 2026 in Lagos, the school's today in these tests.
TEN_MARCH = dt.datetime(2026, 3, 10, 10, 0, tzinfo=ZoneInfo("Africa/Lagos"))


class StudentSentencesFollowTheSchoolsDateFormatTests(StudentsFixture):
    def setUp(self):
        forget_tenant_zone(self.tenant)

    def use_day_first(self):
        set_value(
            definition=ConfigurationDefinition.objects.get(key=DATE_FORMAT_KEY),
            value="DD_MM_YYYY", actor=None, tenant=self.tenant,
        )
        forget_tenant_zone(self.tenant)

    def last_summary(self, student):
        return AuditEvent.objects.filter(
            entity_type="Student", entity_id=str(student.pk),
        ).order_by("-event_at", "-id").first()

    def offer(self):
        return AdmissionStage.all_objects.create(
            tenant=self.tenant, name="Offer", position=1, is_offer=True,
            offer_valid_days=14,
        )

    def test_an_offer_summary_writes_its_last_day_the_schools_way(self):
        self.use_day_first()
        tunde = self.student(first="Tunde", status=StudentStatus.APPLICANT)
        with mock.patch("vs_config.clock.tenant_now", return_value=TEN_MARCH):
            move_to_stage(tunde, self.offer(), actor=self.admin)
        event = self.last_summary(tunde)
        self.assertIn("Offer open until 24/03/2026.", event.summary)
        self.assertEqual(event.metadata["offer_expires_on"], "2026-03-24")

    def test_an_offer_summary_at_a_school_that_chose_nothing_reads_the_default(self):
        tunde = self.student(first="Tunde", status=StudentStatus.APPLICANT)
        with mock.patch("vs_config.clock.tenant_now", return_value=TEN_MARCH):
            move_to_stage(tunde, self.offer(), actor=self.admin)
        self.assertIn("Offer open until 24 Mar 2026.", self.last_summary(tunde).summary)

    def test_a_status_summary_writes_its_effective_day_the_schools_way(self):
        self.use_day_first()
        chiamaka = self.student(status=StudentStatus.ENROLLED)
        transition(
            chiamaka, StudentStatus.WITHDRAWN, actor=self.admin,
            reason="The family moved to Abuja.",
            effective_date=dt.date(2026, 3, 15),
        )
        event = self.last_summary(chiamaka)
        self.assertIn("to Withdrawn on 15/03/2026.", event.summary)
        self.assertEqual(event.metadata["effective_date"], "2026-03-15")

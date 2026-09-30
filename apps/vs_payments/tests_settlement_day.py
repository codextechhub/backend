"""A settlement window names the tenant's days, not the server's.

23:30 UTC on 30 September is 00:30 on 1 October in Lagos, so a payment
confirmed then belongs to October's settlement report and is matched against
bank lines dated from 1 October.
"""
import datetime
from zoneinfo import ZoneInfo

from django.test import SimpleTestCase

from vs_payments.reconciliation import _date_in_window, _day_at

LAGOS = ZoneInfo("Africa/Lagos")
CONFIRMED = datetime.datetime(2026, 9, 30, 23, 30, tzinfo=datetime.timezone.utc)
OCTOBER = (datetime.date(2026, 10, 1), datetime.date(2026, 10, 31))
SEPTEMBER = (datetime.date(2026, 9, 1), datetime.date(2026, 9, 30))


class SettlementDayTests(SimpleTestCase):
    def test_a_confirmation_after_lagos_midnight_is_octobers(self):
        self.assertEqual(_day_at(CONFIRMED, LAGOS), datetime.date(2026, 10, 1))
        self.assertTrue(_date_in_window(CONFIRMED, *OCTOBER, LAGOS))
        self.assertFalse(_date_in_window(CONFIRMED, *SEPTEMBER, LAGOS))

    def test_a_plain_date_and_an_open_window(self):
        self.assertEqual(_day_at(datetime.date(2026, 9, 30), LAGOS), datetime.date(2026, 9, 30))
        self.assertTrue(_date_in_window(CONFIRMED, None, None, LAGOS))
        self.assertTrue(_date_in_window(None, None, None, LAGOS))
        self.assertFalse(_date_in_window(None, *OCTOBER, LAGOS))

"""The fiscal calendar stays ahead of today, and the year close and voids stay shut.

Every posting needs a fiscal period covering its date. These tests cover the three
ways that could quietly fail: the calendar running out (the daily rollover and its
settings), a gap between two years (the runway and the year-opening checks), and
the dates a new set of books starts on. They also cover two ways a finished piece
of work could be undone by hand: reversing the journal a void posted, and closing a
year on a date outside it.
"""
from __future__ import annotations

import datetime
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase
from rest_framework.exceptions import ValidationError

from core.test_utils import TenantAPIClient
from schools.vs_schools.models import School

from .calendar_settings import SETTING_FIELDS as CALENDAR_SETTING_FIELDS
from .constants import DocumentStatus, FinanceAuditAction, PeriodStatus
from .exceptions import PostingError
from .fiscal_calendar import CALENDAR_ALERT_EVENT, open_fiscal_year, roll_fiscal_calendar
from .models import (
    Account,
    FinanceAuditLog,
    FinanceCalendarSettings,
    FiscalPeriod,
    FiscalYear,
    LedgerEntity,
    Payment,
)
from .posting import fiscal_calendar_runway, journal_reversal_action, post_journal, reverse_journal
from .receivables import post_invoice, post_payment
from .seed import seed_chart_of_accounts, seed_currencies, seed_fiscal_year
from .settings_ownership import CALENDAR_SETTING_CONSUMERS
from .tests import _ARFixtureMixin, _GLFixtureMixin
from .voids import void_invoice, void_payment


def _books(code, *, tenant=None):
    """An entity with a chart and no calendar, owned by ``tenant`` when given."""
    seed_currencies()
    fields = {"name": f"Calendar {code}", "code": code, "kind": LedgerEntity.Kind.TENANT}
    if tenant is not None:
        fields["tenant"] = tenant
    entity = LedgerEntity.objects.create(**fields)
    seed_chart_of_accounts(entity)
    return entity


def _years(entity):
    return list(
        FiscalYear.objects.filter(entity=entity)
        .order_by("start_date").values_list("year", "start_date", "end_date"),
    )


class FiscalCalendarRunwayGapTests(TestCase):
    """The runway reads the first uncovered day, not only the last period's end."""

    def test_a_gap_between_years_is_the_break_the_runway_reports(self):
        # A January 2026 year, then a September 2027 year: nothing covers
        # January to August 2027, although the calendar runs to August 2028.
        entity = _books("GAPRW")
        seed_fiscal_year(entity, year=2026)
        seed_fiscal_year(entity, year=2027, start_month=9)

        runway = fiscal_calendar_runway(entity, today=datetime.date(2026, 11, 30))

        self.assertEqual(runway["calendar_end"], datetime.date(2028, 8, 31))
        self.assertEqual(runway["first_uncovered_date"], datetime.date(2027, 1, 1))
        self.assertEqual(runway["days_remaining"], 31)
        self.assertEqual(runway["status"], "EXPIRING")
        self.assertEqual(runway["gaps"], [
            {"start": datetime.date(2027, 1, 1), "end": datetime.date(2027, 8, 31)},
        ])

    def test_today_inside_a_gap_is_expired(self):
        entity = _books("GAPIN")
        seed_fiscal_year(entity, year=2026)
        seed_fiscal_year(entity, year=2027, start_month=9)

        runway = fiscal_calendar_runway(entity, today=datetime.date(2027, 3, 1))

        self.assertEqual(runway["status"], "EXPIRED")
        self.assertEqual(runway["first_uncovered_date"], datetime.date(2027, 3, 1))
        self.assertEqual(runway["days_remaining"], -60)  # 31 December 2026 was the last day.

    def test_contiguous_years_have_no_gap(self):
        entity = _books("GAPNO")
        seed_fiscal_year(entity, year=2026)
        seed_fiscal_year(entity, year=2027)

        runway = fiscal_calendar_runway(entity, today=datetime.date(2026, 11, 30))

        self.assertEqual(runway["gaps"], [])
        self.assertEqual(runway["first_uncovered_date"], datetime.date(2028, 1, 1))
        self.assertEqual(runway["status"], "HEALTHY")

    def test_the_entity_lead_sets_the_warning_threshold(self):
        entity = _books("GAPLEAD")
        seed_fiscal_year(entity, year=2026)
        today = datetime.date(2026, 11, 20)  # 41 days before the calendar ends.
        self.assertEqual(fiscal_calendar_runway(entity, today=today)["status"], "EXPIRING")

        FinanceCalendarSettings.objects.create(entity=entity, next_year_lead_days=30)

        runway = fiscal_calendar_runway(entity, today=today)
        self.assertEqual(runway["threshold_days"], 30)
        self.assertEqual(runway["status"], "HEALTHY")


class FiscalYearOpeningTests(TestCase):
    """A new year must continue the calendar, never leave a stretch uncovered."""

    def setUp(self):
        self.school = School.objects.create(
            name="Calendar School", slug="calendar-school", code="CALSC", status="ACTIVE",
        )
        self.entity = _books("CALBK", tenant=self.school.tenant)
        seed_fiscal_year(self.entity, year=2026)
        self.user = get_user_model().objects.create_user(
            email="calendar-opener@test.com", password="pw", tenant=self.school.tenant,
            status="ACTIVE", first_name="Calendar", last_name="Opener",
        )
        self.client = TenantAPIClient(user=self.user)
        self.url = f"/v1/finance/fiscal-years/?entity={self.entity.code}"

    @patch("vs_rbac.permissions.HasRBACPermission.has_permission", return_value=True)
    def test_a_year_that_leaves_a_gap_is_refused(self, _permission):
        response = self.client.post(self.url, {"year": 2027, "start_month": 9}, format="json")

        self.assertEqual(response.status_code, 400, response.content)
        self.assertIn("uncovered", str(response.content))
        self.assertIn("2027-01-01", str(response.content))
        self.assertFalse(FiscalYear.objects.filter(entity=self.entity, year=2027).exists())
        self.assertFalse(FiscalPeriod.objects.filter(
            entity=self.entity, start_date__gte=datetime.date(2027, 1, 1),
        ).exists())

    @patch("vs_rbac.permissions.HasRBACPermission.has_permission", return_value=True)
    def test_the_default_year_continues_the_calendar_and_is_audited(self, _permission):
        response = self.client.post(self.url, {}, format="json")

        self.assertEqual(response.status_code, 201, response.content)
        self.assertEqual(_years(self.entity)[-1], (
            2027, datetime.date(2027, 1, 1), datetime.date(2027, 12, 31),
        ))
        audit = FinanceAuditLog.objects.get(
            entity=self.entity, action=FinanceAuditAction.FISCAL_YEAR_OPENED,
        )
        self.assertEqual(audit.actor, self.user)
        self.assertFalse(audit.metadata["automatic"])

    def test_a_year_that_leaves_a_gap_before_the_next_one_is_refused(self):
        with self.assertRaises(ValidationError) as caught:
            open_fiscal_year(
                self.entity, year=2024, start_month=1, start_day=1, frequency="MONTHLY",
            )

        self.assertIn("before FY2026", str(caught.exception.detail))
        self.assertFalse(FiscalYear.objects.filter(entity=self.entity, year=2024).exists())

    def test_an_overlapping_year_is_still_refused(self):
        with self.assertRaises(ValidationError) as caught:
            open_fiscal_year(
                self.entity, year=2025, start_month=6, start_day=1, frequency="MONTHLY",
            )

        self.assertIn("overlaps FY2026", str(caught.exception.detail))
        self.assertFalse(FiscalYear.objects.filter(entity=self.entity, year=2025).exists())


class FiscalCalendarRolloverTests(TestCase):
    """The daily rollover opens the next year, or warns, and never does either twice."""

    def _september_books(self, code, *, frequency="MONTHLY"):
        entity = _books(code)
        seed_fiscal_year(entity, year=2026, start_month=9, fiscal_period_frequency=frequency)
        return entity

    def test_auto_open_creates_the_contiguous_next_year_and_is_idempotent(self):
        entity = self._september_books("ROLLAUTO")
        today = datetime.date(2027, 7, 15)  # 47 days before 31 August 2027.

        with patch("vs_notifications.notify.send_notification") as send:
            first = roll_fiscal_calendar(entity, today=today)
            second = roll_fiscal_calendar(entity, today=today)

        self.assertEqual(first["opened"], [2027])
        self.assertFalse(first["warned"])
        self.assertEqual(second["opened"], [])
        send.assert_not_called()
        self.assertEqual(_years(entity), [
            (2026, datetime.date(2026, 9, 1), datetime.date(2027, 8, 31)),
            (2027, datetime.date(2027, 9, 1), datetime.date(2028, 8, 31)),
        ])
        self.assertEqual(
            FiscalPeriod.objects.filter(entity=entity, fiscal_year__year=2027).count(), 12,
        )
        audit = FinanceAuditLog.objects.get(
            entity=entity, action=FinanceAuditAction.FISCAL_YEAR_OPENED,
        )
        self.assertIsNone(audit.actor)
        self.assertTrue(audit.metadata["automatic"])

    def test_auto_open_keeps_the_period_length(self):
        entity = self._september_books("ROLLQTR", frequency="QUARTERLY")

        roll_fiscal_calendar(entity, today=datetime.date(2027, 7, 15))

        self.assertEqual(
            list(FiscalPeriod.objects.filter(entity=entity, fiscal_year__year=2027)
                 .order_by("period_no").values_list("start_date", flat=True)),
            [datetime.date(2027, 9, 1), datetime.date(2027, 12, 1),
             datetime.date(2028, 3, 1), datetime.date(2028, 6, 1)],
        )

    def test_nothing_is_opened_outside_the_lead_window(self):
        entity = self._september_books("ROLLEARLY")

        outcome = roll_fiscal_calendar(entity, today=datetime.date(2027, 5, 1))

        self.assertEqual(outcome, {"opened": [], "warned": False, "failure": ""})
        self.assertEqual(len(_years(entity)), 1)

    def test_warn_only_notifies_once_a_day_and_opens_nothing(self):
        entity = self._september_books("ROLLWARN")
        FinanceCalendarSettings.objects.create(
            entity=entity,
            next_year_mode=FinanceCalendarSettings.NextYearMode.WARN_ONLY,
        )
        bursar = get_user_model().objects.create_user(
            email="rollover-bursar@test.com", password="pw", tenant=entity.tenant,
            status="ACTIVE", first_name="Ada", last_name="Nwosu",
        )
        today = datetime.date(2027, 7, 15)

        with patch("vs_rbac.evaluator.resolve_users_with_permission",
                   return_value=[bursar]) as resolve, \
                patch("vs_notifications.notify.send_notification",
                      return_value=["n1"]) as send:
            first = roll_fiscal_calendar(entity, today=today)
            second = roll_fiscal_calendar(entity, today=today)

        self.assertEqual(first, {"opened": [], "warned": True, "failure": ""})
        self.assertEqual(second, {"opened": [], "warned": False, "failure": ""})
        self.assertEqual(len(_years(entity)), 1)
        send.assert_called_once()
        self.assertEqual(send.call_args.args[0], CALENDAR_ALERT_EVENT)
        self.assertEqual(send.call_args.kwargs["recipients"], [bursar])
        context = send.call_args.kwargs["context"]
        self.assertEqual(context["first_uncovered_date"], "2027-09-01")
        self.assertEqual(context["days_remaining"], 47)
        self.assertIn("2027-08-31", context["situation"])
        self.assertEqual(resolve.call_args.args[2], "finance.period.create")
        warned = FinanceAuditLog.objects.get(
            entity=entity, action=FinanceAuditAction.FISCAL_CALENDAR_WARNED,
        )
        self.assertEqual(warned.metadata["first_uncovered_date"], "2027-09-01")
        self.assertEqual(warned.metadata["warned_on"], "2027-07-15")

    def test_a_warning_repeats_after_its_interval(self):
        entity = self._september_books("ROLLAGAIN")
        FinanceCalendarSettings.objects.create(
            entity=entity,
            next_year_mode=FinanceCalendarSettings.NextYearMode.WARN_ONLY,
        )
        FinanceAuditLog.objects.create(
            entity=entity, action=FinanceAuditAction.FISCAL_CALENDAR_WARNED,
            metadata={"first_uncovered_date": "2027-09-01", "warned_on": "2027-07-08"},
            created_at=datetime.datetime(2027, 7, 8, 9, 0, tzinfo=datetime.timezone.utc),
        )

        with patch("vs_rbac.evaluator.resolve_users_with_permission",
                   return_value=[object()]), \
                patch("vs_notifications.notify.send_notification",
                      return_value=["n1"]) as send:
            six_days = roll_fiscal_calendar(entity, today=datetime.date(2027, 7, 14))
            seven_days = roll_fiscal_calendar(entity, today=datetime.date(2027, 7, 15))

        self.assertFalse(six_days["warned"])
        self.assertTrue(seven_days["warned"])
        self.assertEqual(send.call_count, 1)

    def test_a_gap_no_new_year_can_fill_is_warned_about_under_auto_open(self):
        entity = _books("ROLLGAP")
        seed_fiscal_year(entity, year=2026)
        seed_fiscal_year(entity, year=2027, start_month=9)

        with patch("vs_rbac.evaluator.resolve_users_with_permission",
                   return_value=[object()]), \
                patch("vs_notifications.notify.send_notification",
                      return_value=["n1"]) as send:
            outcome = roll_fiscal_calendar(entity, today=datetime.date(2026, 11, 30))

        self.assertEqual(outcome["opened"], [])
        self.assertTrue(outcome["warned"])
        context = send.call_args.kwargs["context"]
        self.assertIn("2027-01-01 to 2027-08-31", context["situation"])

    def test_a_failed_automatic_opening_is_warned_about(self):
        entity = self._september_books("ROLLFAIL")
        today = datetime.date(2027, 7, 15)

        with patch("vs_finance.fiscal_calendar.open_fiscal_year",
                   side_effect=ValidationError({"year": "Boom."})), \
                patch("vs_rbac.evaluator.resolve_users_with_permission",
                      return_value=[object()]), \
                patch("vs_notifications.notify.send_notification",
                      return_value=["n1"]) as send:
            outcome = roll_fiscal_calendar(entity, today=today)

        self.assertEqual(outcome["opened"], [])
        self.assertEqual(outcome["failure"], "Boom.")
        self.assertTrue(outcome["warned"])
        self.assertIn("failed: Boom.", send.call_args.kwargs["context"]["action"])

    def test_the_daily_task_rolls_every_active_entity(self):
        from .tasks import roll_fiscal_calendars

        entity = self._september_books("ROLLTASK")
        with patch("vs_finance.fiscal_calendar.tenant_today",
                   return_value=datetime.date(2027, 7, 15)), \
                patch("vs_notifications.notify.send_notification", return_value=[]):
            first = roll_fiscal_calendars()
            roll_fiscal_calendars()

        self.assertGreaterEqual(first["opened"], 1)
        self.assertEqual([row[0] for row in _years(entity)], [2026, 2027])

    def test_the_warning_is_delivered_through_the_real_notification_stack(self):
        from django.core.management import call_command

        from vs_notifications.models import Notification

        call_command("seed_notification_templates", verbosity=0)
        school = School.objects.create(
            name="Warned School", slug="warned-school", code="WRNSC", status="ACTIVE",
        )
        entity = _books("ROLLREAL", tenant=school.tenant)
        seed_fiscal_year(entity, year=2026, start_month=9)
        FinanceCalendarSettings.objects.create(
            entity=entity,
            next_year_mode=FinanceCalendarSettings.NextYearMode.WARN_ONLY,
        )
        bursar = get_user_model().objects.create_user(
            email="real-bursar@test.com", password="pw", tenant=school.tenant,
            status="ACTIVE", first_name="Ada", last_name="Nwosu",
        )

        with patch("vs_rbac.evaluator.resolve_users_with_permission",
                   return_value=[bursar]):
            outcome = roll_fiscal_calendar(entity, today=datetime.date(2027, 7, 15))

        self.assertTrue(outcome["warned"])
        delivered = Notification.objects.filter(event_type__key=CALENDAR_ALERT_EVENT)
        self.assertEqual({n.recipient_id for n in delivered}, {bursar.pk})
        in_app = delivered.get(channel="in_app")
        self.assertIn("2027-09-01", in_app.subject)
        self.assertIn("2027-08-31", in_app.body)


class FinanceCalendarSettingsAPITests(TestCase):
    def setUp(self):
        self.school = School.objects.create(
            name="Calendar Settings School", slug="calendar-settings-school",
            code="CSTSC", status="ACTIVE",
        )
        self.entity = _books("CSTBK", tenant=self.school.tenant)
        self.user = get_user_model().objects.create_user(
            email="calendar-settings@test.com", password="pw", tenant=self.school.tenant,
            status="ACTIVE", first_name="Calendar", last_name="Settings",
        )
        self.client = TenantAPIClient(user=self.user)
        self.url = f"/v1/finance/settings/calendar/?entity={self.entity.code}"

    @patch("vs_rbac.permissions.HasRBACPermission.has_permission", return_value=False)
    def test_read_and_update_require_settings_permission(self, _permission):
        self.assertEqual(self.client.get(self.url).status_code, 403)
        self.assertEqual(self.client.patch(
            self.url, {"next_year_mode": "WARN_ONLY"}, format="json",
        ).status_code, 403)

    @patch("vs_rbac.permissions.HasRBACPermission.has_permission", return_value=True)
    def test_defaults_are_returned_without_creating_a_row(self, _permission):
        response = self.client.get(self.url)

        self.assertEqual(response.status_code, 200)
        values = response.data["data"]["settings"]
        self.assertEqual(values["next_year_mode"], "AUTO_OPEN")
        self.assertEqual(values["next_year_lead_days"], 60)
        self.assertEqual(set(response.data["data"]["consumers"]), set(CALENDAR_SETTING_CONSUMERS))
        self.assertEqual(set(CALENDAR_SETTING_CONSUMERS), set(CALENDAR_SETTING_FIELDS))
        self.assertFalse(FinanceCalendarSettings.objects.filter(entity=self.entity).exists())

    @patch("vs_rbac.permissions.HasRBACPermission.has_permission", return_value=True)
    def test_update_is_typed_and_audited(self, _permission):
        response = self.client.patch(
            self.url, {"next_year_mode": "warn_only", "next_year_lead_days": 90}, format="json",
        )

        self.assertEqual(response.status_code, 200, response.content)
        settings = FinanceCalendarSettings.objects.get(entity=self.entity)
        self.assertEqual(settings.next_year_mode, "WARN_ONLY")
        self.assertEqual(settings.next_year_lead_days, 90)
        audit = FinanceAuditLog.objects.get(
            action=FinanceAuditAction.FINANCE_CALENDAR_SETTINGS_UPDATED,
        )
        self.assertEqual(audit.before["next_year_mode"], "AUTO_OPEN")
        self.assertEqual(audit.after["next_year_lead_days"], 90)
        self.assertEqual(response.data["data"]["history"][0]["id"], audit.id)

    @patch("vs_rbac.permissions.HasRBACPermission.has_permission", return_value=True)
    def test_out_of_range_and_unknown_values_are_refused(self, _permission):
        for body in (
            {"next_year_lead_days": 3},
            {"next_year_lead_days": 400},
            {"next_year_lead_days": True},
            {"next_year_mode": "SOMETIMES"},
            {"reminder_colour": "red"},
            {},
        ):
            with self.subTest(body=body):
                response = self.client.patch(self.url, body, format="json")
                self.assertEqual(response.status_code, 400, response.content)
        self.assertFalse(FinanceCalendarSettings.objects.filter(entity=self.entity).exists())


class SeedDefaultYearTests(TestCase):
    """New books cover the entity's own today, not UTC's and not a future year."""

    def _seed_at(self, instant, **kwargs):
        entity = _books(kwargs.pop("code"))
        with patch("django.utils.timezone.now", return_value=instant):
            fiscal_year, _periods = seed_fiscal_year(entity, **kwargs)
        return fiscal_year

    def test_the_default_year_follows_the_entity_time_zone(self):
        # 23:30 UTC on 31 December is 00:30 on 1 January in Lagos.
        fiscal_year = self._seed_at(
            datetime.datetime(2026, 12, 31, 23, 30, tzinfo=datetime.timezone.utc),
            code="SEEDTZ",
        )

        self.assertEqual(fiscal_year.year, 2027)
        self.assertEqual(fiscal_year.start_date, datetime.date(2027, 1, 1))

    def test_the_default_year_covers_today_for_a_september_start(self):
        fiscal_year = self._seed_at(
            datetime.datetime(2027, 3, 10, 9, 0, tzinfo=datetime.timezone.utc),
            code="SEEDSEP", start_month=9,
        )

        self.assertEqual(fiscal_year.year, 2026)
        self.assertEqual(fiscal_year.start_date, datetime.date(2026, 9, 1))
        self.assertEqual(fiscal_year.end_date, datetime.date(2027, 8, 31))

    def test_on_the_start_day_the_new_year_is_chosen(self):
        fiscal_year = self._seed_at(
            datetime.datetime(2027, 9, 1, 9, 0, tzinfo=datetime.timezone.utc),
            code="SEEDDAY", start_month=9,
        )

        self.assertEqual(fiscal_year.year, 2027)


class YearCloseDateTests(_GLFixtureMixin, TestCase):
    """The closing entry lands inside the year it closes, or not at all."""

    def test_a_closing_date_outside_the_year_is_refused(self):
        from .close import close_fiscal_year

        entity, jan = self.build_ledger()
        post_journal(self.make_entry(entity, jan, [("1100", 100000, 0), ("4100", 0, 100000)]))
        jan.status = PeriodStatus.SOFT_CLOSED
        jan.save(update_fields=["status"])

        for closing_date in (datetime.date(2027, 1, 15), datetime.date(2025, 12, 31)):
            with self.subTest(closing_date=closing_date):
                with self.assertRaises(ValidationError) as caught:
                    close_fiscal_year(entity, jan.fiscal_year, closing_date=closing_date)
                self.assertIn("closing_date", caught.exception.detail)
                self.assertIn("inside FY2026", str(caught.exception.detail))

        jan.fiscal_year.refresh_from_db()
        self.assertEqual(jan.fiscal_year.status, PeriodStatus.OPEN)
        self.assertFalse(FinanceAuditLog.objects.filter(
            entity=entity, action=FinanceAuditAction.FISCAL_YEAR_CLOSED,
        ).exists())

    def test_the_endpoint_answers_400_for_a_closing_date_in_the_next_year(self):
        entity, jan = self.build_ledger()
        school = School.objects.create(
            name="Close Date School", slug="close-date-school", code="CLDSC", status="ACTIVE",
        )
        LedgerEntity.objects.filter(pk=entity.pk).update(tenant=school.tenant)
        user = get_user_model().objects.create_user(
            email="close-date@test.com", password="pw", tenant=school.tenant,
            status="ACTIVE", first_name="Close", last_name="Date",
        )
        client = TenantAPIClient(user=user)

        with patch("vs_rbac.permissions.HasRBACPermission.has_permission", return_value=True):
            response = client.post(
                f"/v1/finance/fiscal-years/{jan.fiscal_year.pk}/close/?entity={entity.code}",
                {"closing_date": "2027-01-15", "force": True}, format="json",
            )

        self.assertEqual(response.status_code, 400, response.content)
        self.assertIn("closing_date", str(response.content))


class VoidReversalOwnershipTests(_ARFixtureMixin, TestCase):
    """The journal a void posts belongs to the voided document too."""

    def test_reversing_a_void_reversal_is_refused(self):
        entity, _period, customer, _ = self.build_ar()
        invoice = self.make_invoice(entity, customer, lines=[("4100", 1, 50000, None)])
        post_invoice(invoice)
        void_invoice(invoice)
        settled = self.make_invoice(entity, customer, lines=[("4100", 1, 20000, None)])
        post_invoice(settled)
        payment = Payment.objects.create(
            entity=entity, customer=customer, payment_date=datetime.date(2026, 1, 15),
            amount=20000, deposit_account=Account.objects.get(entity=entity, code="1100"),
        )
        post_payment(payment)
        void_payment(payment)

        for document in (invoice, payment):
            with self.subTest(document=type(document).__name__):
                document.refresh_from_db()
                reversal = document.journal.reversed_by
                with self.assertRaises(PostingError) as caught:
                    reverse_journal(reversal)
                self.assertIn(document.document_number, str(caught.exception))
                reversal.refresh_from_db()
                self.assertEqual(reversal.status, DocumentStatus.POSTED)
                self.assertEqual(journal_reversal_action(reversal), {
                    "kind": "SOURCE_DOCUMENT_ACTION",
                    "document_type": type(document).__name__,
                    "document_number": document.document_number,
                })

    def test_a_manual_journal_and_its_reversal_stay_reversible(self):
        entity, period, _customer, _ = self.build_ar()
        manual = self.make_entry(entity, period, [("1100", 1000, 0), ("4100", 0, 1000)])
        post_journal(manual)

        reversal = reverse_journal(manual)
        self.assertEqual(journal_reversal_action(reversal), {"kind": "REVERSE_JOURNAL"})
        again = reverse_journal(reversal)

        self.assertEqual(again.status, DocumentStatus.POSTED)
        self.assertEqual(again.reverses_id, reversal.pk)

"""The history engine: what gets recorded, what does not, and how it reads back.

Built on real tracked models (a guardian, a staff record) rather than a test
model, because the engine's promise is about the doors real code writes
through: ``save``, ``QuerySet.update``, the bulk writes, deletes and
many-to-many changes.
"""
from __future__ import annotations

import datetime as dt
from io import StringIO
from unittest import mock
from zoneinfo import ZoneInfo

from django.core.exceptions import ImproperlyConfigured
from django.core.management import CommandError, call_command
from django.test import RequestFactory, TestCase
from django.utils import timezone
from rest_framework.request import Request

from schools.vs_staff.models import StaffProfile
from schools.vs_students.history import GUARDIAN, STUDENT
from schools.vs_students.models import Guardian
from vs_history.as_at import (
    AsAt,
    AsAtError,
    HistoryNotKept,
    instance_at,
    instances_at,
    parse_as_at,
    record_today,
    require_history,
    tracking_starts,
)
from vs_config.clock import DEFAULT_TIME_ZONE, TIME_ZONE_KEY
from vs_config.models import ConfigurationDefinition
from vs_config.services.resolution import set_value
from vs_history.models import RecordVersion
from vs_history.queryset import assert_managers_versioned
from vs_history.registry import all_specs, spec_for
from vs_rbac.tests.helpers import make_branch, make_school, make_school_admin
from vs_tenants.context import clear_current_tenant, set_current_audit_identity


#: The zone a school keeps until it chooses another.
DEFAULT_ZONE = ZoneInfo(DEFAULT_TIME_ZONE)


def _at(year, month, day, hour=12, minute=0):
    """An instant on a day in a school on the default zone."""
    return dt.datetime(year, month, day, hour, minute, tzinfo=DEFAULT_ZONE)


class HistoryFixture(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.school = make_school(slug="greenfield", name="Greenfield Schools")
        cls.tenant = cls.school.tenant
        cls.main = make_branch(cls.school, name="Ikeja", is_main=True)
        cls.second = make_branch(cls.school, name="Lekki", is_main=False)
        cls.admin = make_school_admin(None, email="bursar@greenfield.test", tenant=cls.tenant)

    def tearDown(self):
        clear_current_tenant()

    def make_guardian(self, **fields):
        defaults = {"tenant": self.tenant, "first_name": "Ngozi", "last_name": "Okafor",
                    "phone": "08030000001"}
        defaults.update(fields)
        return Guardian.all_objects.create(**defaults)

    def versions(self, row):
        spec = spec_for(type(row))
        return list(RecordVersion.objects.filter(
            record_type=spec.record_type, record_id=str(row.pk),
        ).order_by("recorded_at", "id"))


class RecordingTests(HistoryFixture):
    def test_create_writes_a_first_version_naming_every_field(self):
        guardian = self.make_guardian()
        [version] = self.versions(guardian)
        self.assertFalse(version.is_baseline)
        self.assertEqual(version.data["phone"], "08030000001")
        self.assertIn("first_name", version.changed)
        self.assertNotIn("updated_at", version.data)
        self.assertNotIn("tenant_id", version.data)

    def test_a_save_that_changes_nothing_writes_nothing(self):
        guardian = self.make_guardian()
        guardian.save()
        self.assertEqual(len(self.versions(guardian)), 1)

    def test_a_change_names_only_the_fields_it_touched(self):
        guardian = self.make_guardian()
        guardian.phone = "08039999999"
        guardian.save()
        latest = self.versions(guardian)[-1]
        self.assertEqual(latest.changed, ["phone"])
        self.assertEqual(latest.data["phone"], "08039999999")

    def test_a_save_of_untracked_columns_reads_no_history(self):
        guardian = self.make_guardian()
        with self.assertNumQueries(1):
            guardian.save(update_fields=["updated_at"])

    def test_queryset_update_of_a_tracked_field_is_recorded(self):
        guardian = self.make_guardian()
        Guardian.all_objects.filter(pk=guardian.pk).update(occupation="Nurse")
        self.assertEqual(self.versions(guardian)[-1].changed, ["occupation"])

    def test_queryset_update_through_the_tenant_manager_is_recorded(self):
        guardian = self.make_guardian()
        Guardian.objects.for_tenant(self.tenant).filter(pk=guardian.pk).update(
            occupation="Engineer",
        )
        self.assertEqual(self.versions(guardian)[-1].data["occupation"], "Engineer")

    def test_bulk_create_and_bulk_update_are_recorded(self):
        rows = Guardian.all_objects.bulk_create([
            Guardian(tenant=self.tenant, full_name="Tunde Bakare", first_name="Tunde",
                     last_name="Bakare", phone="0801"),
        ])
        self.assertEqual(len(self.versions(rows[0])), 1)
        rows[0].phone = "0802"
        Guardian.all_objects.bulk_update(rows, ["phone"])
        self.assertEqual(self.versions(rows[0])[-1].data["phone"], "0802")

    def test_delete_writes_a_final_version_and_the_row_reads_absent_after(self):
        with mock.patch("vs_history.recorder.timezone.now", return_value=_at(2026, 3, 2)):
            guardian = self.make_guardian()
        pk = guardian.pk
        with mock.patch("vs_history.recorder.timezone.now", return_value=_at(2026, 3, 9)):
            guardian.delete()
        spec = spec_for(Guardian)
        self.assertIsNotNone(instance_at(spec, pk, AsAt(dt.date(2026, 3, 5), DEFAULT_ZONE)))
        self.assertIsNone(instance_at(spec, pk, AsAt(dt.date(2026, 3, 10), DEFAULT_ZONE)))

    def test_the_actor_is_the_request_audit_identity(self):
        set_current_audit_identity(actor_user=self.admin, effective_user=self.admin)
        guardian = self.make_guardian()
        self.assertEqual(self.versions(guardian)[0].actor_id, self.admin.pk)

    def test_a_rebuilt_row_refuses_to_be_saved_or_deleted(self):
        guardian = self.make_guardian()
        past = instance_at(spec_for(Guardian), guardian.pk, AsAt(record_today(DEFAULT_ZONE) + dt.timedelta(days=1), DEFAULT_ZONE))
        with self.assertRaises(ImproperlyConfigured):
            past.save()
        with self.assertRaises(ImproperlyConfigured):
            past.delete()

    def test_a_model_with_a_plain_manager_cannot_be_tracked(self):
        from vs_audit.models import AuditEvent

        with self.assertRaises(ImproperlyConfigured):
            assert_managers_versioned(AuditEvent)


class ManyToManyTests(HistoryFixture):
    def make_staff(self):
        from vs_rbac.tests.helpers import make_school_admin as make_user

        user = make_user(None, email="teacher@greenfield.test", tenant=self.tenant)
        return StaffProfile.all_objects.create(tenant=self.tenant, user=user, branch=self.main)

    def test_adding_and_removing_a_posting_is_recorded_from_either_side(self):
        staff = self.make_staff()
        staff.additional_postings.add(self.second)
        self.assertEqual(self.versions(staff)[-1].data["additional_postings"], [self.second.pk])
        accessor = StaffProfile._meta.get_field("additional_postings").remote_field.get_accessor_name()
        getattr(self.second, accessor).remove(staff)
        self.assertEqual(self.versions(staff)[-1].data["additional_postings"], [])

    def test_a_reverse_clear_records_the_rows_it_detached(self):
        staff = self.make_staff()
        staff.additional_postings.add(self.second)
        accessor = StaffProfile._meta.get_field("additional_postings").remote_field.get_accessor_name()
        getattr(self.second, accessor).clear()
        self.assertEqual(self.versions(staff)[-1].data["additional_postings"], [])


class ReadingTests(HistoryFixture):
    def test_a_day_includes_changes_up_to_its_last_minute_in_school_time(self):
        with mock.patch("vs_history.recorder.timezone.now", return_value=_at(2026, 3, 1)):
            guardian = self.make_guardian(phone="0801")
        guardian.phone = "0802"
        with mock.patch("vs_history.recorder.timezone.now", return_value=_at(2026, 3, 5, 23, 30)):
            guardian.save()
        guardian.phone = "0803"
        with mock.patch("vs_history.recorder.timezone.now", return_value=_at(2026, 3, 6, 0, 30)):
            guardian.save()
        spec = spec_for(Guardian)
        self.assertEqual(instance_at(spec, guardian.pk, AsAt(dt.date(2026, 3, 4), DEFAULT_ZONE)).phone, "0801")
        self.assertEqual(instance_at(spec, guardian.pk, AsAt(dt.date(2026, 3, 5), DEFAULT_ZONE)).phone, "0802")
        self.assertEqual(instance_at(spec, guardian.pk, AsAt(dt.date(2026, 3, 6), DEFAULT_ZONE)).phone, "0803")

    def test_a_date_before_the_history_starts_is_refused_with_the_first_date(self):
        with mock.patch("vs_history.recorder.timezone.now", return_value=_at(2026, 3, 5)):
            guardian = self.make_guardian()
        with self.assertRaises(HistoryNotKept) as caught:
            require_history(spec_for(Guardian), guardian.pk, AsAt(dt.date(2026, 3, 4), DEFAULT_ZONE), noun="this guardian")
        self.assertEqual(caught.exception.extra["history_starts"], "2026-03-05")
        self.assertEqual(
            require_history(spec_for(Guardian), guardian.pk, AsAt(dt.date(2026, 3, 5), DEFAULT_ZONE), noun="x"),
            dt.date(2026, 3, 5),
        )

    def test_rows_listed_on_an_owner_page_are_rebuilt_as_they_stood(self):
        from schools.vs_students.constants import Gender, Relationship
        from schools.vs_students.models import Student, StudentGuardian

        with mock.patch("vs_history.recorder.timezone.now", return_value=_at(2026, 3, 1)):
            student = Student.all_objects.create(
                tenant=self.tenant, branch=self.main, first_name="Tunde", last_name="Bakare",
                date_of_birth=dt.date(2015, 1, 1), gender=Gender.MALE,
            )
            guardian = self.make_guardian()
            link = StudentGuardian.all_objects.create(
                tenant=self.tenant, student=student, guardian=guardian,
                relationship=Relationship.MOTHER, is_primary=True,
            )
        link_pk = link.pk
        with mock.patch("vs_history.recorder.timezone.now", return_value=_at(2026, 3, 8)):
            link.delete()
        spec = spec_for(StudentGuardian)
        before = instances_at(spec, STUDENT, student.pk, AsAt(dt.date(2026, 3, 7), DEFAULT_ZONE))
        self.assertEqual([row.guardian_id for row in before], [guardian.pk])
        self.assertEqual(instances_at(spec, GUARDIAN, guardian.pk, AsAt(dt.date(2026, 3, 7), DEFAULT_ZONE))[0].pk, link_pk)
        self.assertEqual(instances_at(spec, STUDENT, student.pk, AsAt(dt.date(2026, 3, 9), DEFAULT_ZONE)), [])


class ParseAsAtTests(TestCase):
    def parse(self, value):
        return parse_as_at(Request(RequestFactory().get("/", {"as_at": value} if value is not None else {})))

    def test_no_date_and_today_both_mean_the_live_record(self):
        self.assertIsNone(self.parse(None))
        self.assertIsNone(self.parse(record_today(DEFAULT_ZONE).isoformat()))

    def test_a_past_day_is_parsed(self):
        self.assertEqual(self.parse("2026-03-05"), AsAt(dt.date(2026, 3, 5), DEFAULT_ZONE))

    def test_a_future_day_and_a_malformed_one_are_refused(self):
        with self.assertRaises(AsAtError):
            self.parse((record_today(DEFAULT_ZONE) + dt.timedelta(days=1)).isoformat())
        with self.assertRaises(AsAtError):
            self.parse("05/03/2026")



def _utc(day, hour, minute=0):
    """An instant in March 2026, given in UTC so no school's zone is assumed."""
    return dt.datetime(2026, 3, day, hour, minute, tzinfo=dt.timezone.utc)


class SchoolZoneTests(HistoryFixture):
    """A school's day is counted on its own clock, not on a fixed zone's.

    Greenfield keeps Nairobi time (UTC+3), two hours ahead of Lagos. Bright
    Star keeps the default. Between 21:00 and 23:00 UTC the two schools are on
    different calendar days, which is where a fixed zone answers one of them
    wrongly.
    """

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.set_zone(cls.tenant, "Africa/Nairobi")
        cls.lagos_school = make_school(slug="brightstar", name="Bright Star School")
        cls.lagos_tenant = cls.lagos_school.tenant
        make_branch(cls.lagos_school, name="Yaba", is_main=True)

    @staticmethod
    def set_zone(tenant, name):
        set_value(
            definition=ConfigurationDefinition.objects.get(key=TIME_ZONE_KEY),
            value=name, actor=None, tenant=tenant,
        )

    def parse(self, value, tenant, now):
        request = RequestFactory().get("/", {"as_at": value})
        request.tenant = tenant
        with mock.patch("vs_history.as_at.timezone.now", return_value=now):
            return parse_as_at(Request(request))

    def guardian_with_three_phones(self, tenant):
        """0801 at noon on 14 March, 0802 at 20:45 UTC, 0803 at 21:15 UTC."""
        with mock.patch("vs_history.recorder.timezone.now", return_value=_utc(14, 12)):
            guardian = self.make_guardian(tenant=tenant)
        for phone, when in (("0802", _utc(14, 20, 45)), ("0803", _utc(14, 21, 15))):
            guardian.phone = phone
            with mock.patch("vs_history.recorder.timezone.now", return_value=when):
                guardian.save()
        return guardian

    def test_a_school_ahead_of_lagos_reads_its_own_yesterday_just_after_its_midnight(self):
        """00:30 on 15 March in Nairobi is still 22:30 on the 14th in Lagos.

        The 14th is Greenfield's yesterday, so it is a past day and not the
        live record. Its day ended at 21:00 UTC: the 0802 change at 23:45
        Nairobi time is in, the 0803 change at 00:15 the next morning is not.
        """
        guardian = self.guardian_with_three_phones(self.tenant)
        as_at = self.parse("2026-03-14", self.tenant, now=_utc(14, 21, 30))
        self.assertEqual(as_at, AsAt(dt.date(2026, 3, 14), ZoneInfo("Africa/Nairobi")))
        self.assertEqual(instance_at(spec_for(Guardian), guardian.pk, as_at).phone, "0802")
        self.assertEqual(
            require_history(spec_for(Guardian), guardian.pk, as_at, noun="x"),
            dt.date(2026, 3, 14),
        )

    def test_a_school_ahead_of_lagos_is_not_refused_its_own_today(self):
        """At 00:30 on 15 March in Nairobi, the 15th has happened there."""
        self.assertIsNone(self.parse("2026-03-15", self.tenant, now=_utc(14, 21, 30)))

    def test_a_school_on_lagos_time_is_unchanged(self):
        """The same instants, read by a school on the default zone.

        At 22:30 Lagos time on the 14th, the 14th is still today there, the
        15th has not happened, and the 14th read the next morning includes
        every change made before Lagos's midnight at 23:00 UTC.
        """
        guardian = self.guardian_with_three_phones(self.lagos_tenant)
        self.assertIsNone(self.parse("2026-03-14", self.lagos_tenant, now=_utc(14, 21, 30)))
        with self.assertRaises(AsAtError):
            self.parse("2026-03-15", self.lagos_tenant, now=_utc(14, 21, 30))
        as_at = self.parse("2026-03-14", self.lagos_tenant, now=_utc(14, 23, 30))
        self.assertEqual(as_at, AsAt(dt.date(2026, 3, 14), DEFAULT_ZONE))
        self.assertEqual(instance_at(spec_for(Guardian), guardian.pk, as_at).phone, "0803")

    def test_history_starts_on_the_day_the_school_was_on_when_it_was_recorded(self):
        """A record first saved at 23:30 UTC on 14 March.

        That is 02:30 on the 15th in Nairobi and 23:30 on the 14th in Abidjan
        (UTC+0), so each school names its own day and refuses the day before.
        """
        abidjan = make_school(slug="harbour", name="Harbour School").tenant
        self.set_zone(abidjan, "Africa/Abidjan")
        with mock.patch("vs_history.recorder.timezone.now", return_value=_utc(14, 23, 30)):
            guardian = self.make_guardian()
        spec = spec_for(Guardian)
        now = _utc(20, 12)
        nairobi = self.parse("2026-03-15", self.tenant, now=now)
        self.assertEqual(require_history(spec, guardian.pk, nairobi, noun="x"), dt.date(2026, 3, 15))
        with self.assertRaises(HistoryNotKept) as caught:
            require_history(spec, guardian.pk, self.parse("2026-03-14", self.tenant, now=now), noun="x")
        self.assertEqual(caught.exception.extra["history_starts"], "2026-03-15")
        abidjan_day = self.parse("2026-03-14", abidjan, now=now)
        self.assertEqual(require_history(spec, guardian.pk, abidjan_day, noun="x"), dt.date(2026, 3, 14))
        self.assertEqual(tracking_starts(spec, nairobi.zone), dt.date(2026, 3, 15))
        self.assertEqual(tracking_starts(spec, abidjan_day.zone), dt.date(2026, 3, 14))


class BaselineCommandTests(HistoryFixture):
    def test_rows_without_history_get_one_baseline_and_a_rerun_adds_none(self):
        guardian = self.make_guardian()
        RecordVersion.objects.filter(record_type=GUARDIAN).delete()
        with self.assertRaises(CommandError):
            call_command("baseline_record_history", "--check", stdout=StringIO())
        call_command("baseline_record_history", stdout=StringIO())
        [version] = self.versions(guardian)
        self.assertTrue(version.is_baseline)
        self.assertIsNone(version.actor_id)
        call_command("baseline_record_history", stdout=StringIO())
        self.assertEqual(len(self.versions(guardian)), 1)
        call_command("baseline_record_history", "--check", stdout=StringIO())

    def test_the_baseline_is_the_day_the_history_starts(self):
        guardian = self.make_guardian()
        RecordVersion.objects.filter(record_type=GUARDIAN).delete()
        with mock.patch("vs_history.management.commands.baseline_record_history.timezone.now",
                        return_value=_at(2026, 9, 26)):
            call_command("baseline_record_history", stdout=StringIO())
        with self.assertRaises(HistoryNotKept):
            require_history(spec_for(Guardian), guardian.pk, AsAt(dt.date(2026, 9, 25), DEFAULT_ZONE), noun="x")

    def test_the_first_run_stamps_when_tracking_reached_each_model(self):
        """A model with versions keeps its earliest; one without any starts at the run."""
        from vs_history.models import TrackingStart

        with mock.patch("vs_history.recorder.timezone.now", return_value=_at(2026, 3, 2)):
            self.make_guardian()
        TrackingStart.objects.all().delete()
        with mock.patch("vs_history.management.commands.baseline_record_history.timezone.now",
                        return_value=_at(2026, 9, 26)):
            call_command("baseline_record_history", stdout=StringIO())
        self.assertEqual(tracking_starts(spec_for(Guardian), DEFAULT_ZONE), dt.date(2026, 3, 2))
        empty = next(
            spec for spec in all_specs()
            if not RecordVersion.objects.filter(record_type=spec.record_type).exists()
        )
        self.assertEqual(tracking_starts(empty, DEFAULT_ZONE), dt.date(2026, 9, 26))
        call_command("baseline_record_history", stdout=StringIO())
        self.assertEqual(tracking_starts(spec_for(Guardian), DEFAULT_ZONE), dt.date(2026, 3, 2))


class UserHistoryTests(HistoryFixture):
    def test_an_account_keeps_only_its_allow_listed_identity(self):
        from vs_user.models import User

        user = make_school_admin(None, email="head@greenfield.test", tenant=self.tenant)
        data = self.versions(user)[-1].data
        self.assertNotIn("password", data)
        self.assertNotIn("last_login_at", data)
        self.assertEqual(data["email"], "head@greenfield.test")
        before = len(self.versions(user))
        with self.assertNumQueries(1):
            User.objects.filter(pk=user.pk).update(last_login_at=timezone.now())
        self.assertEqual(len(self.versions(user)), before)

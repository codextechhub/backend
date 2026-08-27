"""Calendar events: the refusal, the two warnings, and the audience narrowing."""
from __future__ import annotations

import datetime as dt

from ..models import CalendarEvent, CalendarEventAudience, EventType
from .base import _Base, _SingleBranchBase


class EventValidationTests(_Base):
    def test_a_date_outside_the_session_is_refused(self):
        response = self.post(self.admin, "calendar-event-list", {
            "name": "Too early", "event_type": "HOLIDAY",
            "start_date": "2025-08-01", "end_date": "2025-08-01",
        })
        self.assertEqual(response.status_code, 422, response.data)
        self.assertEqual(response.data["error"]["code"], "EVENT_OUTSIDE_SESSION")

    def test_an_end_before_the_start_is_refused(self):
        response = self.post(self.admin, "calendar-event-list", {
            "name": "Backwards", "event_type": "HOLIDAY",
            "start_date": "2025-10-10", "end_date": "2025-10-01",
        })
        self.assertEqual(response.status_code, 422, response.data)
        self.assertEqual(response.data["error"]["code"], "INVALID_DATE_RANGE")

    def test_a_one_day_event_is_fine(self):
        """Inclusive, deliberately: a one-day holiday is the ordinary case."""
        response = self.post(self.admin, "calendar-event-list", {
            "name": "Independence Day", "event_type": "HOLIDAY",
            "start_date": "2025-10-01", "end_date": "2025-10-01",
        })
        self.assertEqual(response.status_code, 201, response.data)

    def test_a_date_outside_every_term_warns_and_still_saves(self):
        """The December break is a real entry on a real calendar."""
        response = self.post(self.admin, "calendar-event-list", {
            "name": "Christmas break", "event_type": "HOLIDAY",
            "start_date": "2025-12-19", "end_date": "2026-01-02",
        })
        self.assertEqual(response.status_code, 201, response.data)
        codes = {w["code"] for w in response.data["data"]["warnings"]}
        self.assertIn("EVENT_OUTSIDE_ANY_TERM", codes)
        self.assertTrue(CalendarEvent.all_objects.filter(name="Christmas break").exists())

    def test_an_overlap_of_the_same_type_and_scope_warns_and_still_saves(self):
        CalendarEvent.all_objects.create(
            tenant=self.tenant, session=self.year, name="Mid-term break",
            event_type=EventType.MIDTERM_BREAK,
            start_date=dt.date(2025, 10, 27), end_date=dt.date(2025, 10, 31),
        )
        response = self.post(self.admin, "calendar-event-list", {
            "name": "Another break", "event_type": "MIDTERM_BREAK",
            "start_date": "2025-10-29", "end_date": "2025-11-01",
        })
        self.assertEqual(response.status_code, 201, response.data)
        codes = {w["code"] for w in response.data["data"]["warnings"]}
        self.assertIn("EVENT_OVERLAP", codes)

    def test_an_overlap_of_a_different_type_warns_about_nothing(self):
        CalendarEvent.all_objects.create(
            tenant=self.tenant, session=self.year, name="Mid-term break",
            event_type=EventType.MIDTERM_BREAK,
            start_date=dt.date(2025, 10, 27), end_date=dt.date(2025, 10, 31),
        )
        response = self.post(self.admin, "calendar-event-list", {
            "name": "PTA Meeting", "event_type": "PTA",
            "start_date": "2025-10-29", "end_date": "2025-10-29",
        })
        self.assertEqual(response.data["data"]["warnings"], [])

    def test_the_term_is_derived_and_never_stored(self):
        response = self.post(self.admin, "calendar-event-list", {
            "name": "PTA Meeting", "event_type": "PTA",
            "start_date": "2025-11-08", "end_date": "2025-11-08",
        })
        self.assertEqual(response.data["data"]["term"]["name"], "First Term")


class EventScopeTests(_Base):
    def test_school_wide_is_rendered_as_a_chip_never_as_a_blank(self):
        response = self.post(self.admin, "calendar-event-list", {
            "name": "Independence Day", "event_type": "HOLIDAY",
            "start_date": "2025-10-01", "end_date": "2025-10-01",
        })
        self.assertEqual(response.data["data"]["scope_label"], "School-wide")

    def test_a_branch_admin_does_not_see_another_branchs_event(self):
        CalendarEvent.all_objects.create(
            tenant=self.tenant, session=self.year, branch=self.lekki,
            name="Founder's Day", event_type=EventType.SCHOOL_EVENT,
            start_date=dt.date(2025, 11, 21), end_date=dt.date(2025, 11, 21),
        )
        response = self.get(self.ikeja_admin, "calendar-event-list")
        names = {row["name"] for row in response.data["data"]}
        self.assertNotIn("Founder's Day", names)

    def test_a_branch_admin_still_sees_school_wide_events(self):
        """The read is inclusive: the shared rows are most of a calendar."""
        CalendarEvent.all_objects.create(
            tenant=self.tenant, session=self.year, name="Independence Day",
            event_type=EventType.HOLIDAY,
            start_date=dt.date(2025, 10, 1), end_date=dt.date(2025, 10, 1),
        )
        response = self.get(self.ikeja_admin, "calendar-event-list")
        names = {row["name"] for row in response.data["data"]}
        self.assertIn("Independence Day", names)

    def test_a_branch_admin_cannot_create_a_school_wide_event(self):
        response = self.post(self.ikeja_admin, "calendar-event-list", {
            "name": "Everyone", "event_type": "HOLIDAY",
            "start_date": "2025-10-01", "end_date": "2025-10-01",
            "branch": None,
        })
        self.assertEqual(response.status_code, 403, response.data)


class EventAudienceTests(_Base):
    """Lekki's primary Speech Day: Primary 4 A is off, JSS1 is not."""

    def test_an_event_with_no_audience_covers_everybody(self):
        response = self.post(self.admin, "calendar-event-list", {
            "name": "Independence Day", "event_type": "HOLIDAY",
            "start_date": "2025-10-01", "end_date": "2025-10-01",
        })
        # None, not an empty list: an empty list renders as "Applies to: none",
        # which is the opposite of what no rows mean.
        self.assertIsNone(response.data["data"]["audience"])

    def test_an_event_can_be_narrowed_to_a_level(self):
        response = self.post(self.admin, "calendar-event-list", {
            "name": "Primary Speech Day", "event_type": "SCHOOL_EVENT",
            "start_date": "2025-11-14", "end_date": "2025-11-14",
            "closes_school": True,
            "audience": [{"type": "level", "id": self.primary4.pk}],
        })
        self.assertEqual(response.status_code, 201, response.data)
        audience = response.data["data"]["audience"]
        self.assertEqual(len(audience), 1)
        self.assertEqual(audience[0]["name"], "Primary 4")

    def test_an_event_can_be_narrowed_to_a_class(self):
        response = self.post(self.admin, "calendar-event-list", {
            "name": "JSS1 A trip", "event_type": "SCHOOL_EVENT",
            "start_date": "2025-11-14", "end_date": "2025-11-14",
            "audience": [{"type": "class", "id": self.jss1a.pk}],
        })
        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(response.data["data"]["audience"][0]["name"], "JSS1 A")

    def test_editing_the_audience_replaces_it_rather_than_adding_to_it(self):
        created = self.post(self.admin, "calendar-event-list", {
            "name": "Speech Day", "event_type": "SCHOOL_EVENT",
            "start_date": "2025-11-14", "end_date": "2025-11-14",
            "audience": [{"type": "level", "id": self.primary4.pk}],
        })
        pk = created.data["data"]["id"]
        response = self.patch(self.admin, "calendar-event-detail", {
            "audience": [{"type": "level", "id": self.jss1.pk}],
        }, pk=pk)
        self.assertEqual(response.status_code, 200, response.data)
        names = {a["name"] for a in response.data["data"]["audience"]}
        self.assertEqual(names, {"JSS1"})

    def test_the_audience_can_be_cleared_back_to_everybody(self):
        created = self.post(self.admin, "calendar-event-list", {
            "name": "Speech Day", "event_type": "SCHOOL_EVENT",
            "start_date": "2025-11-14", "end_date": "2025-11-14",
            "audience": [{"type": "level", "id": self.primary4.pk}],
        })
        pk = created.data["data"]["id"]
        response = self.patch(
            self.admin, "calendar-event-detail", {"audience": []}, pk=pk,
        )
        self.assertIsNone(response.data["data"]["audience"])

    def test_a_branch_event_cannot_be_narrowed_to_another_branchs_class(self):
        response = self.post(self.admin, "calendar-event-list", {
            "name": "Lekki only", "event_type": "SCHOOL_EVENT",
            "start_date": "2025-11-14", "end_date": "2025-11-14",
            "branch": self.lekki.pk,
            "audience": [{"type": "class", "id": self.sss2.pk}],
        })
        self.assertEqual(response.status_code, 422, response.data)
        self.assertEqual(
            response.data["error"]["code"], "EVENT_AUDIENCE_OUT_OF_SCOPE",
        )

    def test_a_level_in_the_audience_covers_its_classes(self):
        from ..services.calendar import classes_covered_by

        created = self.post(self.admin, "calendar-event-list", {
            "name": "JSS1 Speech Day", "event_type": "SCHOOL_EVENT",
            "start_date": "2025-11-14", "end_date": "2025-11-14",
            "audience": [{"type": "level", "id": self.jss1.pk}],
        })
        event = CalendarEvent.all_objects.get(pk=created.data["data"]["id"])
        covered = classes_covered_by(event)
        # A school says "the whole of JSS1" rather than naming three arms.
        self.assertIn(self.jss1a.pk, covered)
        self.assertIn(self.jss1b.pk, covered)
        self.assertNotIn(self.pry4a.pk, covered)


class TeachingDayTests(_Base):
    """The half that actually goes wrong without an audience."""

    def test_a_narrowed_closure_only_costs_the_classes_it_reaches(self):
        from ..services.calendar import teaching_days

        event = CalendarEvent.all_objects.create(
            tenant=self.tenant, session=self.year, branch=self.lekki,
            name="Primary Speech Day", event_type=EventType.SCHOOL_EVENT,
            start_date=dt.date(2025, 11, 14), end_date=dt.date(2025, 11, 14),
            closes_school=True,
        )
        CalendarEventAudience.all_objects.create(
            tenant=self.tenant, event=event, level=self.primary4,
        )
        events = [event]

        primary = teaching_days(self.first_term, events, school_class=self.pry4a)
        jss = teaching_days(self.first_term, events, school_class=self.jss1a)
        # 14 November is a Friday. Primary 4 A loses it; JSS1 A does not.
        self.assertEqual(jss - primary, 1)

    def test_an_unnarrowed_closure_costs_every_class_the_day(self):
        from ..services.calendar import teaching_days

        event = CalendarEvent.all_objects.create(
            tenant=self.tenant, session=self.year, name="Independence Day",
            event_type=EventType.HOLIDAY,
            start_date=dt.date(2025, 10, 1), end_date=dt.date(2025, 10, 1),
            closes_school=True,
        )
        primary = teaching_days(self.first_term, [event], school_class=self.pry4a)
        jss = teaching_days(self.first_term, [event], school_class=self.jss1a)
        self.assertEqual(primary, jss)


class EventSecurityTests(_Base):
    def test_deleting_needs_the_manage_key_not_the_update_key(self):
        from vs_rbac.models import TenantRolePermission

        event = CalendarEvent.all_objects.create(
            tenant=self.tenant, session=self.year, name="Sports",
            event_type=EventType.SPORTS,
            start_date=dt.date(2025, 11, 14), end_date=dt.date(2025, 11, 14),
        )
        TenantRolePermission.objects.filter(
            role=self.role, permission__key="academics.calendar.manage",
        ).update(granted=False)
        response = self.delete(self.admin, "calendar-event-detail", pk=event.pk)
        self.assertEqual(response.status_code, 403, response.data)

    def test_another_tenants_event_answers_404(self):
        theirs = CalendarEvent.all_objects.create(
            tenant=self.other.tenant, session=self.other_year, name="Theirs",
            event_type=EventType.HOLIDAY,
            start_date=dt.date(2025, 10, 1), end_date=dt.date(2025, 10, 1),
        )
        response = self.get(self.admin, "calendar-event-detail", pk=theirs.pk)
        self.assertEqual(response.status_code, 404, response.data)


class SingleBranchEventTests(_SingleBranchBase):
    def test_the_scope_field_is_absent(self):
        self.post(self.admin, "calendar-event-list", {
            "name": "Independence Day", "event_type": "HOLIDAY",
            "start_date": "2025-10-01", "end_date": "2025-10-01",
        })
        response = self.get(self.admin, "calendar-event-list")
        row = response.data["data"][0]
        self.assertNotIn("branch", row)
        self.assertNotIn("scope_label", row)

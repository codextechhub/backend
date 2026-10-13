"""Staff numbers and employment moves held to their switches outside the register.

Two payloads restated a registered staff field:

* the staff number rule's suggestion is the next number after those already
  issued, so it follows the ``school.teachers.staff_number`` switch;
* an employment status move is audited with the statuses it moved between, the
  reason given, the day it took effect and a leaver's last working day, which a
  reader sees only where they may read ``employment_status`` and ``exit_date``.

Brightfield has two branches and Sunrise one; a switch belongs to a role and a
role to one school.
"""
from __future__ import annotations

from rest_framework.test import APIRequestFactory

from schools.vs_staff.field_access import EMPLOYMENT_STATUS_FIELD, EXIT_DATE_FIELD
from vs_audit.models import AuditEvent
from vs_audit.serializers import (
    AuditEventDetailSerializer,
    AuditEventListSerializer,
)
from vs_rbac.tests.helpers import (
    install_declared_fields,
    make_assignment,
    make_role,
    make_role_permission,
    make_school_admin,
    set_field_access,
)

from .base import StaffFixture

STAFF_NUMBER_FIELD = "school.teachers.staff_number"
REASON = "Moving abroad to join family."


class _Fixture(StaffFixture):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        install_declared_fields("school.teachers")
        cls.readers = {}
        for label, school in (("multi", cls.school), ("single", cls.solo)):
            cls.readers[label] = {
                "open": cls.reader(school, label, "open"),
                "no_number": cls.reader(
                    school, label, "no-number", closed=(STAFF_NUMBER_FIELD,),
                ),
                "no_status": cls.reader(
                    school, label, "no-status", closed=(EMPLOYMENT_STATUS_FIELD,),
                ),
                "no_exit": cls.reader(
                    school, label, "no-exit", closed=(EXIT_DATE_FIELD,),
                ),
            }

    @classmethod
    def reader(cls, school, label, name, *, closed=()):
        role = make_role(school, name=f"{name} {label}", key=f"leaks_{name}_{label}")
        make_role_permission(role, cls.permissions["school.teachers.view"])
        if closed:
            set_field_access(role, *closed, read=False, write=False)
        user = make_school_admin(
            None, email=f"{name}.{label}@staff-leaks.test", tenant=school.tenant,
        )
        make_assignment(school, user, role, branch=None)
        return user


class StaffNumberSuggestionTests(_Fixture):
    def test_the_suggestion_follows_the_staff_number_switch(self):
        for label in ("multi", "single"):
            for who, present in (
                ("open", True), ("no_status", True), ("no_number", False),
            ):
                with self.subTest(school=label, caller=who):
                    response = self.get(self.readers[label][who], "staff-number-policy")
                    self.assertEqual(response.status_code, 200, response.data)
                    data = response.data["data"]
                    self.assertEqual("suggestion" in data, present, data)
                    self.assertIn("pattern", data)

    def test_a_branch_rule_read_is_held_to_the_same_switch(self):
        response = self.get(
            self.readers["multi"]["no_number"], "staff-number-policy",
            {"branch": str(self.lekki.pk)},
        )
        self.assertEqual(response.status_code, 200, response.data)
        self.assertNotIn("suggestion", response.data["data"])

    def test_a_school_that_closed_the_switch_does_not_close_another(self):
        data = self.get(self.readers["single"]["open"], "staff-number-policy").data["data"]
        self.assertIn("suggestion", data)


class EmploymentMoveAuditTests(_Fixture):
    def moved(self, label, admin, staff):
        response = self.post(
            admin, "staff-status",
            {
                "to_status": "RESIGNED", "reason": REASON,
                "last_working_day": "2025-12-18",
            },
            pk=staff.pk,
        )
        self.assertEqual(response.status_code, 200, response.data)
        return AuditEvent.objects.filter(
            entity_type="StaffProfile", entity_id=str(staff.pk),
            action_type="STAFF_EMPLOYMENT_STATUS_CHANGED",
        ).latest("event_at")

    def render(self, event, user, serializer=AuditEventDetailSerializer):
        request = APIRequestFactory().get("/")
        request.user = user
        request.tenant = user.tenant
        return serializer(event, context={"request": request}).data

    def shapes(self):
        return (
            ("multi", self.admin, self.eze),
            ("single", self.solo_admin, self.solo_staff),
        )

    def test_a_reader_of_both_fields_sees_the_move_as_written(self):
        for label, admin, staff in self.shapes():
            with self.subTest(school=label):
                event = self.moved(label, admin, staff)
                shown = self.render(event, self.readers[label]["open"])
                self.assertEqual(shown["metadata"]["reason"], REASON)
                self.assertEqual(shown["metadata"]["to_status"], "RESIGNED")
                self.assertEqual(shown["metadata"]["last_working_day"], "2025-12-18")
                self.assertIn("moved from", shown["summary"])

    def test_without_the_status_switch_the_statuses_reason_and_date_are_cut(self):
        for label, admin, staff in self.shapes():
            with self.subTest(school=label):
                event = self.moved(label, admin, staff)
                for serializer in (AuditEventDetailSerializer, AuditEventListSerializer):
                    shown = self.render(event, self.readers[label]["no_status"], serializer)
                    self.assertEqual(shown["summary"], "Employment status moved")
                    self.assertNotIn("RESIGNED", str(shown))
                    self.assertNotIn(REASON, str(shown))
                detail = self.render(event, self.readers[label]["no_status"])
                for key in ("from_status", "to_status", "reason", "effective_date"):
                    self.assertNotIn(key, detail["metadata"])
                # The last working day is the exit date's, and this reader may read it.
                self.assertEqual(detail["metadata"]["last_working_day"], "2025-12-18")

    def test_without_the_exit_date_switch_only_the_last_working_day_is_cut(self):
        for label, admin, staff in self.shapes():
            with self.subTest(school=label):
                event = self.moved(label, admin, staff)
                shown = self.render(event, self.readers[label]["no_exit"])
                self.assertNotIn("last_working_day", shown["metadata"])
                self.assertEqual(shown["metadata"]["reason"], REASON)
                self.assertEqual(shown["metadata"]["to_status"], "RESIGNED")
                self.assertIn("moved from", shown["summary"])

    def test_a_search_cannot_find_a_status_the_reader_may_not_read(self):
        from vs_audit.protected_words import reader_for, search_summary

        event = self.moved("multi", self.admin, self.eze)
        rows = AuditEvent.objects.filter(pk=event.pk)
        for who, found in (("open", 1), ("no_status", 0)):
            request = APIRequestFactory().get("/")
            request.tenant = self.tenant
            request.user = self.readers["multi"][who]
            queryset, lookup = search_summary(rows, reader_for(request))
            self.assertEqual(
                queryset.filter(**{f"{lookup}__icontains": "RESIGNED"}).count(), found, who,
            )

    def test_the_person_stays_the_label_of_the_row(self):
        event = self.moved("multi", self.admin, self.eze)
        shown = self.render(event, self.readers["multi"]["no_status"])
        self.assertEqual(shown["entity_label"], "Chukwuemeka Eze")

    def test_the_stored_row_is_untouched(self):
        event = self.moved("multi", self.admin, self.eze)
        self.render(event, self.readers["multi"]["no_status"])
        event.refresh_from_db()
        self.assertEqual(event.metadata["reason"], REASON)
        self.assertIn("moved from", event.summary)

    def test_the_failed_go_live_invitation_is_not_a_status_move(self):
        from schools.vs_staff.services import audit

        audit.emit_setup_invitation_failed(self.eze, "SMTP down", actor=self.admin)
        event = AuditEvent.objects.filter(
            entity_type="StaffProfile", entity_id=str(self.eze.pk),
            action_type="STAFF_EMPLOYMENT_STATUS_CHANGED", status="FAILED",
        ).latest("event_at")
        shown = self.render(event, self.readers["multi"]["no_status"])
        self.assertIn("Resend it from the staff list", shown["summary"])
        self.assertEqual(shown["metadata"]["to_status"], "INVITED")

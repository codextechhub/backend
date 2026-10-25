"""A class's class teachers over the year, and who made each change.

Brightfield's administrator makes Chukwuemeka Eze the class teacher of the
shared class, then hands it to the registrar, then leaves it with nobody. Each
change closes one term and opens the next; the class's own page reads them back
newest first, with who set and who ended each one, under two Field Access
switches. Somebody who has left can be neither class teacher nor given a
subject.
"""
from __future__ import annotations

import importlib

from django.apps import apps
from django.db import IntegrityError, transaction

from schools.vs_staff.constants import EmploymentStatus
from schools.vs_staff.models import ClassTeacherTerm
from schools.vs_staff.services import teaching
from vs_audit.models import AuditEvent
from vs_rbac.tests.helpers import install_declared_fields, set_field_access

from .base import StaffFixture

HISTORY = "academics.classes.class_teacher_history"
CHANGED_BY = "academics.classes.class_teacher_changed_by"


class _Base(StaffFixture):
    def designate(self, staff, *, school_class=None, user=None):
        return self.put(
            user or self.admin, "staff-class-teacher",
            {
                "school_class": (school_class or self.shared_class).pk,
                "staff": staff.pk if staff is not None else None,
            },
        )

    def terms(self, school_class=None):
        return list(
            ClassTeacherTerm.all_objects
            .filter(school_class=school_class or self.shared_class)
            .order_by("id"),
        )

    def class_page(self, user=None, school_class=None):
        return self.client_for(user or self.admin).get(
            f"/v1/academics/classes/{(school_class or self.shared_class).pk}/",
            {"tenant": self.tenant.slug, "session": self.year.pk},
        )


class TermsTests(_Base):
    def test_each_change_closes_one_term_and_opens_the_next(self):
        self.designate(self.eze)
        self.designate(self.registrar)

        first, second = self.terms()
        self.assertEqual(first.staff_id, self.eze.pk)
        self.assertEqual(first.started_by_id, self.admin.pk)
        self.assertEqual(first.ended_by_id, self.admin.pk)
        self.assertIsNotNone(first.ended_at)
        self.assertEqual(second.staff_id, self.registrar.pk)
        self.assertEqual(second.started_at, first.ended_at)
        self.assertIsNone(second.ended_at)

    def test_clearing_ends_the_term_and_opens_none(self):
        self.designate(self.eze)
        self.designate(None)

        (only,) = self.terms()
        self.assertIsNotNone(only.ended_at)
        self.assertEqual(only.ended_by_id, self.admin.pk)

    def test_saving_the_same_person_again_records_nothing(self):
        self.designate(self.eze)
        self.designate(self.eze)
        self.assertEqual(len(self.terms()), 1)

    def test_a_class_has_one_open_term_at_most(self):
        self.designate(self.eze)
        with self.assertRaises(IntegrityError), transaction.atomic():
            ClassTeacherTerm.all_objects.create(
                tenant=self.tenant, school_class=self.shared_class, staff=self.registrar,
            )


class LeaverTests(_Base):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.gone = cls.make_staff(
            "tolu@brightfield.test", "Tolu", "Bankole", status=EmploymentStatus.RESIGNED,
        )

    def test_somebody_who_has_left_cannot_be_made_class_teacher(self):
        response = self.designate(self.gone)
        self.assertEqual(response.status_code, 422, response.data)
        self.assertEqual(response.data["error"]["code"], "STAFF_HAS_LEFT")
        self.assertIn("Tolu Bankole", response.data["message"])
        self.shared_class.refresh_from_db()
        self.assertIsNone(self.shared_class.class_teacher_id)
        self.assertEqual(self.terms(), [])

    def test_somebody_who_has_left_cannot_be_given_a_subject(self):
        response = self.post(
            self.admin, "staff-teaching",
            {"school_class": self.shared_class.pk, "subject": self.maths.pk, "part": "LEAD"},
            pk=self.gone.pk,
        )
        self.assertEqual(response.status_code, 422, response.data)
        self.assertEqual(response.data["error"]["code"], "STAFF_HAS_LEFT")


class ClassPageHistoryTests(_Base):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        install_declared_fields("academics.classes")

    def history(self, response):
        self.assertEqual(response.status_code, 200, response.data)
        return response.data["data"]["class_teacher_history"]

    def test_the_class_page_reads_the_terms_newest_first(self):
        self.designate(self.eze)
        self.designate(self.registrar)

        current, earlier = self.history(self.class_page())
        self.assertEqual(current["staff_id"], self.registrar.pk)
        self.assertIsNone(current["ended_at"])
        self.assertEqual(earlier["name"], "Chukwuemeka Eze")
        self.assertIsNotNone(earlier["ended_at"])
        self.assertEqual(earlier["set_by"]["id"], self.admin.pk)
        self.assertEqual(earlier["ended_by"]["id"], self.admin.pk)

    def test_a_class_nobody_has_held_reads_an_empty_history(self):
        self.assertEqual(self.history(self.class_page()), [])

    def test_the_class_list_does_not_carry_it(self):
        self.designate(self.eze)
        listed = self.client_for(self.admin).get(
            "/v1/academics/classes/", {"tenant": self.tenant.slug, "session": self.year.pk},
        )
        row = next(r for r in listed.data["data"] if r["id"] == self.shared_class.pk)
        self.assertNotIn("class_teacher_history", row)

    def test_hiding_the_history_removes_it_and_keeps_the_current_teacher(self):
        self.designate(self.eze)
        set_field_access(self.role, HISTORY, read=False)

        response = self.class_page()
        self.assertEqual(response.status_code, 200, response.data)
        self.assertNotIn("class_teacher_history", response.data["data"])
        self.assertEqual(response.data["data"]["class_teacher"]["staff_id"], self.eze.pk)

    def test_hiding_who_changed_it_keeps_the_teachers_and_dates(self):
        self.designate(self.eze)
        self.designate(self.registrar)
        set_field_access(self.role, CHANGED_BY, read=False)

        for term in self.history(self.class_page()):
            self.assertNotIn("set_by", term)
            self.assertNotIn("ended_by", term)
            self.assertIn("started_at", term)
            self.assertIn("name", term)

    def test_another_branch_cannot_read_a_class_it_does_not_run(self):
        self.designate(self.ikeja_teacher, school_class=self.ikeja_class)
        response = self.class_page(user=self.lekki_head, school_class=self.ikeja_class)
        self.assertEqual(response.status_code, 404, response.data)

    def test_a_reader_without_the_class_permission_is_refused(self):
        response = self.class_page(user=self.nobody)
        self.assertEqual(response.status_code, 403, response.data)


class RebuiltFromTheAuditTrailTests(_Base):
    """The data migration that fills in the terms served before they were kept."""

    migration = importlib.import_module(
        "schools.vs_staff.migrations.0016_class_teacher_terms_from_the_audit_trail",
    )

    def rebuild(self):
        ClassTeacherTerm.all_objects.all().delete()
        self.migration.rebuild_terms(apps, None)

    def test_the_audited_changes_become_terms(self):
        self.designate(self.eze)
        self.designate(self.registrar)
        recorded = self.terms()
        self.rebuild()
        rebuilt = self.terms()

        # The audit event is stamped a moment after the term it records.
        self.assertEqual(len(rebuilt), len(recorded))
        for was, now in zip(recorded, rebuilt):
            self.assertEqual(
                (now.staff_id, now.started_by_id, now.ended_by_id),
                (was.staff_id, was.started_by_id, was.ended_by_id),
            )
            for a, b in ((was.started_at, now.started_at), (was.ended_at, now.ended_at)):
                self.assertEqual(a is None, b is None)
                if a is not None:
                    self.assertLess(abs((a - b).total_seconds()), 1)

    def test_a_holder_from_before_the_trail_gets_an_unknown_start(self):
        self.shared_class.class_teacher = self.eze
        self.shared_class.save(update_fields=["class_teacher"])
        self.designate(self.registrar)
        self.rebuild()

        first, second = self.terms()
        self.assertEqual(first.staff_id, self.eze.pk)
        self.assertIsNone(first.started_at)
        self.assertIsNotNone(first.ended_at)
        self.assertEqual(second.staff_id, self.registrar.pk)

    def test_a_trail_that_disagrees_with_the_class_keeps_only_the_holder(self):
        self.designate(self.eze)
        self.shared_class.class_teacher = self.registrar
        self.shared_class.save(update_fields=["class_teacher"])
        self.rebuild()

        (only,) = self.terms()
        self.assertEqual(only.staff_id, self.registrar.pk)
        self.assertIsNone(only.started_at)
        self.assertIsNone(only.ended_at)

    def test_a_class_with_no_trail_and_no_holder_gets_nothing(self):
        AuditEvent.objects.filter(entity_type="SchoolClass").delete()
        self.rebuild()
        self.assertEqual(self.terms(), [])


class ClassTeacherSwitchTests(_Base):
    """The Class teacher switch on the class's own reads and on the change.

    Read off: the class list and page leave the class teacher out (absent, not
    null), and the history drops the term running now, which names the same
    person. Write off: the change is refused and the class keeps its teacher.
    """

    CLASS_TEACHER = "academics.classes.class_teacher"

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        install_declared_fields("academics.classes")

    def listed(self):
        response = self.client_for(self.admin).get(
            "/v1/academics/classes/", {"tenant": self.tenant.slug, "session": self.year.pk},
        )
        self.assertEqual(response.status_code, 200, response.data)
        return next(r for r in response.data["data"] if r["id"] == self.shared_class.pk)

    def test_without_read_the_list_and_the_page_leave_it_out(self):
        self.designate(self.eze)
        self.designate(self.registrar)
        set_field_access(self.role, self.CLASS_TEACHER, read=False, write=False)

        self.assertNotIn("class_teacher", self.listed())
        page = self.class_page()
        self.assertEqual(page.status_code, 200, page.data)
        self.assertNotIn("class_teacher", page.data["data"])
        history = page.data["data"]["class_teacher_history"]
        self.assertEqual([t["staff_id"] for t in history], [self.eze.pk])
        self.assertNotIn(self.registrar.pk, [t["staff_id"] for t in history])

    def test_without_write_the_change_is_refused(self):
        self.designate(self.eze)
        set_field_access(self.role, self.CLASS_TEACHER, read=True, write=False)

        for staff in (self.registrar, None):
            with self.subTest(staff=staff):
                response = self.designate(staff)
                self.assertEqual(response.status_code, 403, response.data)
        self.shared_class.refresh_from_db()
        self.assertEqual(self.shared_class.class_teacher_id, self.eze.pk)
        self.assertEqual(len(self.terms()), 1)

    def test_without_write_resending_the_holder_is_not_a_change(self):
        self.designate(self.eze)
        set_field_access(self.role, self.CLASS_TEACHER, read=True, write=False)
        self.assertEqual(self.designate(self.eze).status_code, 200)

    def test_open_by_default(self):
        self.designate(self.eze)
        self.assertEqual(self.listed()["class_teacher"]["staff_id"], self.eze.pk)
        self.assertEqual(self.class_page().data["data"]["class_teacher"]["staff_id"], self.eze.pk)

"""Asking for a correction to a published result, the drawer that precedes it and the list.

Security first: the key each route needs, another school's and another branch's
class, student and amendment answering 404, and the person who may ask not being
the person who may decide. Then each refusal in the words the screen shows, what
a raise stores and the rest of the world it leaves untouched, and the list's
shape at a school with several branches and at one with a single branch.
"""
from __future__ import annotations

from urllib.parse import urlencode

from django.db import IntegrityError, connection, transaction
from django.test.utils import CaptureQueriesContext
from django.urls import reverse

from core.test_utils import TenantAPIClient
from schools.vs_gradebook.constants import AmendmentStatus
from schools.vs_gradebook.models import Amendment, Score, StudentTermResult
from vs_audit.models import AuditEvent

from .amend_base import AmendmentFixture


class KeyTests(AmendmentFixture):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.published()

    def test_only_the_create_key_may_raise_a_correction(self):
        for user in (
            self.nobody, self.eze, self.releaser, self.principal, self.approver,
            self.bursar, self.hub_reader,
        ):
            with self.subTest(user=user.email):
                self.assertEqual(self.raise_amendment(user, self.kelechi).status_code, 403)
        self.assertEqual(Amendment.objects.count(), 0)
        self.assertEqual(self.raise_amendment(self.amender, self.kelechi).status_code, 201)

    def test_either_amendment_key_may_open_the_drawer_and_the_list_and_nothing_else_may(self):
        for user in (self.nobody, self.eze, self.releaser, self.principal, self.bursar):
            with self.subTest(user=user.email):
                self.assertEqual(self.amendment_form(user, self.kelechi).status_code, 403)
                self.assertEqual(self.amendments_list(user).status_code, 403)
        for user in (self.amender, self.approver, self.both_keys):
            with self.subTest(user=user.email):
                self.assertEqual(self.amendment_form(user, self.kelechi).status_code, 200)
                self.assertEqual(self.amendments_list(user).status_code, 200)

    def test_only_the_approve_key_may_decide_a_correction(self):
        found = self.raised(self.kelechi)
        for user in (self.nobody, self.amender, self.principal, self.eze, self.releaser):
            with self.subTest(user=user.email):
                self.assertEqual(self.approve_amendment(user, found).status_code, 403)
                self.assertEqual(
                    self.reject_amendment(user, found, note="No.").status_code, 403,
                )
        self.assertEqual(Amendment.objects.get(pk=found).status, AmendmentStatus.PENDING)


class IsolationTests(AmendmentFixture):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.published()

    def test_another_schools_caller_cannot_reach_this_schools_class_student_or_term(self):
        self.assertEqual(self.raise_amendment(self.solo_amender, self.kelechi).status_code, 404)
        self.assertEqual(self.amendment_form(self.solo_amender, self.kelechi).status_code, 404)
        self.assertEqual(self.amendments_list(self.solo_amender).status_code, 404)

    def test_this_schools_caller_cannot_name_another_schools_term_student_or_subject(self):
        cases = {
            "term": {"term": self.solo_term.pk},
            "student": {"student": self.solo_pupil.pk},
            "subject": {"subject": self.solo_maths.pk},
            "class": {"class": self.solo_class.pk},
        }
        for name, change in cases.items():
            with self.subTest(name):
                body = {**self.amendment_body(self.kelechi), **change}
                self.assertEqual(self.post_amendment(self.amender, body).status_code, 404)
        self.assertEqual(Amendment.objects.count(), 0)

    def test_another_schools_approver_gets_a_404_for_an_amendment_here(self):
        found = self.raised(self.kelechi)
        self.assertEqual(self.approve_amendment(self.solo_approver, found).status_code, 404)
        self.assertEqual(
            self.reject_amendment(self.solo_approver, found, note="No.").status_code, 404,
        )
        self.assertEqual(Amendment.objects.get(pk=found).status, AmendmentStatus.PENDING)

    def test_an_unknown_amendment_is_a_404(self):
        self.assertEqual(self.approve_amendment(self.approver, 999999).status_code, 404)
        self.assertEqual(self.reject_amendment(self.approver, 999999, note="No.").status_code, 404)

    def test_a_caller_pinned_to_another_branch_cannot_raise_for_this_branchs_class(self):
        self.assertEqual(self.raise_amendment(self.ikeja_amender, self.kelechi).status_code, 404)
        self.assertEqual(self.amendment_form(self.ikeja_amender, self.kelechi).status_code, 404)
        self.assertEqual(self.raise_amendment(self.lekki_amender, self.kelechi).status_code, 201)

    def test_a_caller_pinned_to_another_branch_cannot_decide_or_list_this_branchs_amendment(self):
        found = self.raised(self.kelechi)
        self.assertEqual(self.approve_amendment(self.ikeja_approver, found).status_code, 404)
        self.assertEqual(
            self.reject_amendment(self.ikeja_approver, found, note="No.").status_code, 404,
        )
        self.assertEqual(self.data(self.amendments_list(self.ikeja_approver)), [])
        self.assertEqual(
            self.amendments_list(self.ikeja_approver, branch=self.lekki.pk).status_code, 404,
        )
        listed = self.data(self.amendments_list(self.lekki_approver))
        self.assertEqual([row["id"] for row in listed], [found])
        self.assertEqual(self.approve_amendment(self.lekki_approver, found).status_code, 200)

    def test_nobody_decides_an_amendment_they_raised(self):
        found = self.raised(self.kelechi, user=self.both_keys)
        for response in (
            self.approve_amendment(self.both_keys, found),
            self.reject_amendment(self.both_keys, found, note="Changed my mind."),
        ):
            self.assertEqual(response.status_code, 403)
            self.assertEqual(self.code(response), "SELF_DECISION")
        self.assertEqual(Amendment.objects.get(pk=found).status, AmendmentStatus.PENDING)
        # Somebody else holding the same key can.
        self.assertEqual(self.approve_amendment(self.approver, found).status_code, 200)


class NotPublishedTests(AmendmentFixture):
    def test_a_class_with_nothing_published_cannot_be_amended(self):
        for response in (
            self.raise_amendment(self.amender, self.kelechi),
            self.amendment_form(self.amender, self.kelechi),
        ):
            self.assertEqual(response.status_code, 409)
            self.assertEqual(self.code(response), "NOT_PUBLISHED")
            self.assertIn("change the marks on the score sheet", self.message(response))
        self.assertEqual(Amendment.objects.count(), 0)

    def test_a_class_awaiting_the_principal_cannot_be_amended_either(self):
        self.drive_to_awaiting()
        response = self.raise_amendment(self.amender, self.kelechi)
        self.assertEqual((response.status_code, self.code(response)), (409, "NOT_PUBLISHED"))


class RaiseTests(AmendmentFixture):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.published()

    def refused(self, response, code, status=422):
        self.assertEqual((response.status_code, self.code(response)), (status, code), response.data)
        self.assertEqual(Amendment.objects.count(), 0)
        return response

    def test_a_correction_is_stored_as_pending_with_the_cells_before_and_after(self):
        response = self.raise_amendment(self.amender, self.kelechi)
        self.assertEqual(response.status_code, 201, response.data)
        body = self.data(response)
        self.assertEqual(body["status"], "PENDING")
        self.assertEqual(body["student"]["name"], "Kelechi Eze")
        self.assertEqual(body["subject"]["name"], "Mathematics")
        self.assertEqual(body["class"]["name"], "JSS1 A")
        self.assertEqual(body["term"]["name"], "First Term")
        self.assertEqual(body["amendment_reason"], "Marked the paper wrongly.")
        self.assertEqual(body["raised_by"], "Amara Amend")
        self.assertIsNone(body["decided_by"])
        self.assertEqual(body["decision_note"], "")
        self.assertIsNone(body["version"])
        self.assertEqual(
            [(c["mark"], c["marker"]) for c in body["before"]["cells"]],
            [(10, None), (10, None), (20, None)],
        )
        self.assertEqual(
            [(c["mark"], c["marker"]) for c in body["after"]["cells"]],
            [(10, None), (10, None), (50, None)],
        )
        self.assertEqual((body["before"]["total"], body["before"]["grade"]), (40, "D"))
        self.assertEqual((body["after"]["total"], body["after"]["grade"]), (70, "A"))
        stored = Amendment.objects.get()
        self.assertEqual(stored.before[2], {"component": self.exam.pk, "mark": 20, "marker": None})
        self.assertEqual(stored.after[2], {"component": self.exam.pk, "mark": 50, "marker": None})

    def test_raising_changes_nothing_that_was_published(self):
        before = self.snapshots()
        scores = list(Score.objects.order_by("pk").values_list("pk", "mark", "marker"))
        self.raise_amendment(self.amender, self.kelechi)
        self.assertEqual(self.snapshots(), before)
        self.assertEqual(list(Score.objects.order_by("pk").values_list("pk", "mark", "marker")), scores)

    def test_a_marker_may_replace_a_mark(self):
        response = self.raise_amendment(
            self.amender, self.kelechi, cells=self.cells(None, "EXC", None),
        )
        self.assertEqual(response.status_code, 201, response.data)
        body = self.data(response)
        self.assertEqual(body["after"]["cells"][1], {
            "component": self.ca2.pk, "mark": None, "marker": "EXC",
        })
        # Excusing the second assessment leaves 80 obtainable: 30 of 80.
        self.assertEqual((body["after"]["total"], body["after"]["obtainable"]), (30, 80))

    def test_a_reason_is_required(self):
        for reason in ("", "   ", None):
            with self.subTest(reason=reason):
                response = self.raise_amendment(self.amender, self.kelechi, reason=reason)
                self.refused(response, "REASON_REQUIRED")
        body = self.amendment_body(self.kelechi)
        del body["reason"]
        response = TenantAPIClient(self.amender).post(
            reverse("gradebook-amendments"), body, format="json",
        )
        self.refused(response, "REASON_REQUIRED")

    def test_the_registered_name_of_the_reason_is_accepted_too(self):
        body = self.amendment_body(self.kelechi)
        body["amendment_reason"] = body.pop("reason")
        response = TenantAPIClient(self.amender).post(
            reverse("gradebook-amendments"), body, format="json",
        )
        self.assertEqual(response.status_code, 201, response.data)

    def test_marks_are_judged_exactly_as_a_sheet_judges_them(self):
        cases = {
            "OVER_MAX": (self.cells(None, None, 61), "Max 60"),
            "NOT_A_WHOLE_MARK": (self.cells(None, None, 12.5), None),
            "NEGATIVE_MARK": (self.cells(None, None, -1), None),
        }
        for code, (cells, message) in cases.items():
            with self.subTest(code):
                response = self.refused(
                    self.raise_amendment(self.amender, self.kelechi, cells=cells), "CELLS_REFUSED",
                )
                [refused] = response.data["error"]["detail"]["cells"]
                self.assertEqual(refused["code"], code)
                self.assertEqual(refused["component"], self.exam.pk)
                self.assertEqual(refused["student"], self.kelechi.pk)
                if message:
                    self.assertEqual(refused["message"], message)

    def test_every_refused_cell_is_listed_at_once_and_nothing_is_saved(self):
        cells = self.cells(25, None, 61)
        response = self.refused(
            self.raise_amendment(self.amender, self.kelechi, cells=cells), "CELLS_REFUSED",
        )
        codes = {c["component"]: c["code"] for c in response.data["error"]["detail"]["cells"]}
        self.assertEqual(codes, {self.ca1.pk: "OVER_MAX", self.exam.pk: "OVER_MAX"})

    def test_a_published_result_cannot_be_left_with_a_blank_cell(self):
        cells = [{"component": self.exam.pk, "mark": None, "marker": None}]
        response = self.refused(
            self.raise_amendment(self.amender, self.kelechi, cells=cells), "CELLS_REFUSED",
        )
        [refused] = response.data["error"]["detail"]["cells"]
        self.assertEqual(refused["code"], "BLANK_CELL")

    def test_a_bad_marker_a_duplicate_and_a_column_that_is_not_the_results_are_refused(self):
        cases = {
            "INVALID_MARKER": [{"component": self.exam.pk, "mark": None, "marker": "XYZ"}],
            "MARK_AND_MARKER": [{"component": self.exam.pk, "mark": 40, "marker": "ABS"}],
            "DUPLICATE_CELL": self.cells(None, None, 40) + self.cells(None, None, 41),
            "UNKNOWN_COMPONENT": [{"component": self.sexam.pk, "mark": 40, "marker": None}],
        }
        for code, cells in cases.items():
            with self.subTest(code):
                response = self.refused(
                    self.raise_amendment(self.amender, self.kelechi, cells=cells),
                    "CELLS_REFUSED",
                )
                self.assertEqual(
                    {c["code"] for c in response.data["error"]["detail"]["cells"]}, {code},
                )

    def test_cells_are_required_and_may_not_be_empty(self):
        self.assertEqual(
            self.raise_amendment(self.amender, self.kelechi, cells=[]).status_code, 400,
        )
        body = self.amendment_body(self.kelechi)
        del body["cells"]
        response = TenantAPIClient(self.amender).post(
            reverse("gradebook-amendments"), body, format="json",
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(Amendment.objects.count(), 0)

    def test_marks_identical_to_the_published_ones_are_refused(self):
        same = self.cells(10, 10, 20)
        response = self.refused(
            self.raise_amendment(self.amender, self.kelechi, cells=same), "NOTHING_CHANGED",
        )
        self.assertIn("Change at least one mark", self.message(response))

    def test_a_subject_the_student_does_not_take_is_refused(self):
        response = self.raise_amendment(self.amender, self.kelechi, subject=self.french)
        self.refused(response, "SUBJECT_NOT_TAKEN")

    def test_a_student_the_class_published_no_result_for_is_a_404(self):
        self.assertEqual(self.raise_amendment(self.amender, self.obinna).status_code, 404)
        self.assertEqual(self.raise_amendment(self.amender, self.sade).status_code, 404)

    def test_only_one_correction_waits_at_a_time_for_a_students_subject(self):
        self.assertEqual(self.raise_amendment(self.amender, self.kelechi).status_code, 201)
        response = self.raise_amendment(
            self.amender, self.kelechi, cells=self.cells(None, None, 55),
        )
        self.assertEqual((response.status_code, self.code(response)), (409, "AMENDMENT_PENDING"))
        self.assertEqual(Amendment.objects.count(), 1)
        # Another subject of the same student, and the same subject of another student, are free.
        self.assertEqual(self.raise_amendment(self.amender, self.tunde).status_code, 201)
        self.assertEqual(
            self.raise_amendment(
                self.amender, self.tunde, subject=self.french,
                cells=self.cells(None, None, 50),
            ).status_code, 201,
        )

    def test_the_database_holds_the_one_pending_rule_too(self):
        first = Amendment.objects.create(
            tenant=self.tenant, class_term=self.result_of(self.kelechi).class_term,
            student=self.kelechi, subject=self.maths, reason="One.", before=[], after=[],
        )
        with self.assertRaises(IntegrityError), transaction.atomic():
            Amendment.objects.create(
                tenant=self.tenant, class_term=first.class_term, student=self.kelechi,
                subject=self.maths, reason="Two.", before=[], after=[],
            )
        first.status = AmendmentStatus.REJECTED
        first.save()
        Amendment.objects.create(
            tenant=self.tenant, class_term=first.class_term, student=self.kelechi,
            subject=self.maths, reason="Three.", before=[], after=[],
        )

    def test_a_rejected_correction_frees_the_subject_for_another(self):
        found = self.raised(self.kelechi)
        self.assertEqual(self.reject_amendment(self.approver, found, note="No.").status_code, 200)
        self.assertEqual(self.raise_amendment(self.amender, self.kelechi).status_code, 201)

    def test_the_audit_trail_records_who_asked_and_carries_no_reason_or_mark(self):
        self.raise_amendment(self.amender, self.kelechi, reason="A very private reason.")
        event = AuditEvent.objects.get(module_key="GRADEBOOK", entity_type="Amendment")
        self.assertIn("Amara Amend asked to correct Mathematics", event.summary)
        self.assertIn("Kelechi Eze", event.summary)
        self.assertNotIn("private", event.summary + str(event.metadata))
        self.assertNotIn("50", event.summary)
        # Record ids only; an id can itself be 50, so the keys are checked.
        self.assertEqual(set(event.metadata), {"term_id", "class_id", "student_id", "subject_id"})


class DrawerTests(AmendmentFixture):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.published()

    def test_the_drawer_carries_the_columns_the_published_marks_and_the_figures_they_earn(self):
        body = self.data(self.amendment_form(self.amender, self.kelechi))
        self.assertEqual(body["student"]["name"], "Kelechi Eze")
        self.assertEqual(body["subject"]["name"], "Mathematics")
        self.assertEqual(body["class"]["name"], "JSS1 A")
        self.assertEqual(body["version"], 1)
        self.assertEqual([c["short_name"] for c in body["components"]], ["CA1", "CA2", "EXAM"])
        self.assertEqual(
            [(c["component"], c["mark"], c["marker"]) for c in body["cells"]],
            [(self.ca1.pk, 10, None), (self.ca2.pk, 10, None), (self.exam.pk, 20, None)],
        )
        self.assertEqual(
            (body["total"], body["obtainable"], body["percentage"], body["grade"], body["remark"]),
            (40, 100, "40.00", "D", "Fair"),
        )
        self.assertIsNone(body["pending"])

    def test_the_drawer_names_the_correction_already_waiting(self):
        found = self.raised(self.kelechi)
        body = self.data(self.amendment_form(self.amender, self.kelechi))
        self.assertEqual(body["pending"]["id"], found)
        self.assertIsNone(self.data(self.amendment_form(self.amender, self.tunde))["pending"])

    def test_the_drawer_reads_the_marks_a_correction_left(self):
        found = self.raised(self.kelechi)
        self.approve_amendment(self.approver, found)
        body = self.data(self.amendment_form(self.amender, self.kelechi))
        self.assertEqual(body["version"], 2)
        self.assertEqual((body["cells"][2]["mark"], body["total"], body["grade"]), (50, 70, "A"))

    def test_the_drawer_needs_a_class_a_student_and_a_subject(self):
        for name in ("class", "student", "subject"):
            with self.subTest(name):
                query = {"term": self.first_term.pk, "class": self.jss1a.pk,
                         "student": self.kelechi.pk, "subject": self.maths.pk}
                del query[name]
                response = TenantAPIClient(self.amender).get(
                    f"{reverse('gradebook-amendment-form')}?{urlencode(query)}",
                )
                self.assertEqual(response.status_code, 400, response.data)

    def test_a_malformed_identifier_is_a_404(self):
        self.assertEqual(
            self.amendment_form(self.amender, self.kelechi, **{"class": "abc"}).status_code, 404,
        )

    def test_a_subject_not_on_the_students_card_is_refused(self):
        response = self.amendment_form(self.amender, self.kelechi, self.french)
        self.assertEqual((response.status_code, self.code(response)), (422, "SUBJECT_NOT_TAKEN"))


class ListTests(AmendmentFixture):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.published()

    def test_nothing_raised_is_an_empty_list_with_the_pagination_around_it(self):
        response = self.amendments_list(self.approver)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["data"], [])
        self.assertEqual(response.data["pagination"]["totalItems"], 0)
        self.assertTrue(response.data["branch_dimension"])

    def test_a_row_names_the_branch_at_a_school_with_several(self):
        self.raised(self.kelechi)
        [row] = self.data(self.amendments_list(self.approver))
        self.assertEqual(row["branch"], {"id": self.lekki.pk, "name": "Lekki Branch"})

    def test_waiting_corrections_come_first_then_the_newest(self):
        first = self.raised(self.kelechi)
        second = self.raised(self.tunde)
        third = self.raised(self.chiamaka)
        self.reject_amendment(self.approver, second, note="No.")
        rows = self.data(self.amendments_list(self.approver))
        self.assertEqual([r["id"] for r in rows], [third, first, second])

    def test_the_list_filters_by_status_and_refuses_an_unknown_one(self):
        first = self.raised(self.kelechi)
        second = self.raised(self.tunde)
        self.reject_amendment(self.approver, second, note="No.")
        pending = self.data(self.amendments_list(self.approver, status="PENDING"))
        self.assertEqual([r["id"] for r in pending], [first])
        rejected = self.data(self.amendments_list(self.approver, status="REJECTED"))
        self.assertEqual([r["id"] for r in rejected], [second])
        self.assertEqual(self.data(self.amendments_list(self.approver, status="APPROVED")), [])
        self.assertEqual(self.amendments_list(self.approver, status="LATER").status_code, 400)

    def test_the_list_filters_by_branch_and_by_term(self):
        self.raised(self.kelechi)
        self.assertEqual(len(self.data(self.amendments_list(self.approver, branch=self.lekki.pk))), 1)
        self.assertEqual(self.data(self.amendments_list(self.approver, branch=self.ikeja.pk)), [])
        self.assertEqual(
            self.data(self.amendments_list(self.approver, term=self.second_term.pk)), [],
        )

    def test_a_row_carries_how_it_was_decided(self):
        found = self.raised(self.kelechi)
        self.approve_amendment(self.approver, found, note="Checked the script.")
        [row] = self.data(self.amendments_list(self.approver))
        self.assertEqual(row["status"], "APPROVED")
        self.assertEqual(row["decided_by"], "Apollo Approve")
        self.assertIsNotNone(row["decided_at"])
        self.assertEqual(row["decision_note"], "Checked the script.")
        self.assertEqual(row["version"], 2)

    def test_the_cost_of_the_list_does_not_grow_with_the_corrections_on_it(self):
        def cost():
            with CaptureQueriesContext(connection) as captured:
                response = self.amendments_list(self.approver)
            self.assertEqual(response.status_code, 200)
            return len(captured)

        self.raised(self.kelechi)
        one = cost()
        self.raised(self.tunde)
        self.raised(self.chiamaka)
        self.assertEqual(cost(), one)


class SingleBranchSchoolTests(AmendmentFixture):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.publish_sunrise()

    def raise_for_sunrise(self, user=None):
        return TenantAPIClient(user or self.solo_amender).post(
            reverse("gradebook-amendments"),
            {
                "term": self.solo_term.pk, "class": self.solo_class.pk,
                "student": self.solo_pupil_two.pk, "subject": self.solo_maths.pk,
                "reason": "A mark was added up wrongly.",
                "cells": [{"component": self.solo_component.pk, "mark": 50, "marker": None}],
            },
            format="json",
        )

    @property
    def solo_component(self):
        return list(self.solo_structure.components.all())[2]

    def test_a_correction_is_raised_and_approved_and_names_no_branch(self):
        response = self.raise_for_sunrise()
        self.assertEqual(response.status_code, 201, response.data)
        body = self.data(response)
        self.assertIsNone(body["branch"])
        self.assertEqual((body["before"]["total"], body["after"]["total"]), (40, 70))
        self.assertEqual(self.approve_amendment(self.solo_approver, body["id"]).status_code, 200)
        listed = self.amendments_list(self.solo_approver, term=self.solo_term.pk)
        self.assertFalse(listed.data["branch_dimension"])
        self.assertIsNone(listed.data["data"][0]["branch"])

    def test_a_school_that_ranks_nobody_gets_no_position_on_the_corrected_card(self):
        found = self.data(self.raise_for_sunrise())["id"]
        self.approve_amendment(self.solo_approver, found)
        result = StudentTermResult.objects.get(student=self.solo_pupil_two)
        self.assertEqual(result.version, 2)
        self.assertNotIn("position", result.snapshot)
        self.assertEqual(self.subject_of(result.snapshot, "Mathematics")["total"], 70)

"""Fee holds: the list, placing a hold, lifting it, and the rule that no score is ever shown."""
from __future__ import annotations

import datetime as dt
from types import SimpleNamespace
from unittest import mock

from schools.core.fal import registry
from schools.core.fal.contracts import AgeingBucket, DebtorRow
from schools.core.fal.testing import FakeFinanceReader
from schools.vs_academics.models import AcademicSession, AcademicTerm
from schools.vs_gradebook.constants import EmailState, StoredStage
from schools.vs_gradebook.models import ClassTerm, ResultHold, StudentTermResult
from vs_audit.models import AuditEvent

from .class_base import ClassFixture

SEND = "schools.vs_gradebook.services.publishing.send_notification"
SUMMARY = "schools.vs_gradebook.services.publishing.class_term_summary"
SCORE_WORDS = {"grade", "total", "average", "percentage", "mark", "marks", "score", "position", "remark"}

TUNDE_OWES = 8_500_000


class SpyReader(FakeFinanceReader):
    """A fake that remembers what it was asked."""

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.calls = []

    def debtors(self, school_ref, branch_ref=None, filters=(), page=1, page_size=20):
        self.calls.append((school_ref, branch_ref, filters, page, page_size))
        return super().debtors(school_ref, branch_ref, filters, page, page_size)


def keys_at_any_depth(value):
    if isinstance(value, dict):
        for key, inner in value.items():
            yield key
            yield from keys_at_any_depth(inner)
    elif isinstance(value, (list, tuple)):
        for inner in value:
            yield from keys_at_any_depth(inner)


class HoldsListTests(ClassFixture):
    def setUp(self):
        super().setUp()
        self.finance((self.tunde, TUNDE_OWES, dt.date(2025, 9, 20)))

    def test_it_lists_the_roll_students_of_the_callers_classes_by_class_then_student_name(self):
        body = self.data(self.holds(self.bursar))
        names = [(r["class"]["name"], r["student"]["name"]) for r in body]
        self.assertEqual(
            names,
            [
                ("JSS1 A", "Tunde Bello"), ("JSS1 A", "Kelechi Eze"), ("JSS1 A", "Chiamaka Nwosu"),
                ("JSS1 A", "Amina Yusuf"), ("JSS1 C", "Ife Adeola"), ("JSS1 C", "Sade Ojo"),
                ("SSS1 A", "Zainab Aliyu"), ("SSS1 A", "Femi Balogun"),
            ],
        )
        self.assertEqual(body[0]["student"]["admission_number"], "BFS/002")

    def test_a_class_with_no_branch_and_a_student_not_yet_placed_are_not_listed(self):
        names = {r["student"]["name"] for r in self.data(self.holds(self.bursar))}
        self.assertNotIn("Obinna Okafor", names)
        self.assertNotIn("JSS1 Shared", {r["class"]["name"] for r in self.data(self.holds(self.bursar))})

    def test_the_balance_the_oldest_unpaid_day_and_the_days_owed_come_from_finance(self):
        response = self.holds(self.bursar)
        row = self.row_of_student(self.data(response), self.tunde)
        self.assertEqual(row["balance"], TUNDE_OWES)
        self.assertEqual(row["oldest_unpaid_on"], "2025-09-20")
        self.assertEqual(row["days_owed"], 23)
        self.assertTrue(response.data["finance_available"])
        self.assertIsNotNone(response.data["as_of"])

    def test_a_student_finance_does_not_name_owes_nothing(self):
        row = self.row_of_student(self.data(self.holds(self.bursar)), self.chiamaka)
        self.assertEqual(row["balance"], 0)
        self.assertIsNone(row["oldest_unpaid_on"])
        self.assertIsNone(row["days_owed"])

    def test_a_day_not_yet_due_is_zero_days_owed_not_negative(self):
        self.finance((self.tunde, 5000, dt.date(2025, 11, 1)))
        row = self.row_of_student(self.data(self.holds(self.bursar)), self.tunde)
        self.assertEqual(row["days_owed"], 0)

    def test_several_bills_of_one_child_are_added_and_the_oldest_day_wins(self):
        rows = tuple(
            DebtorRow(
                student_ref=str(self.tunde.pk), student_name="Tunde Bello", class_label="",
                outstanding=amount, ageing=AgeingBucket.CURRENT, oldest_unpaid_on=day,
            )
            for amount, day in ((1000, dt.date(2025, 10, 1)), (2500, dt.date(2025, 9, 1)))
        )
        registry.set_finance_reader(FakeFinanceReader(debtors=rows))
        row = self.row_of_student(self.data(self.holds(self.bursar)), self.tunde)
        self.assertEqual((row["balance"], row["oldest_unpaid_on"]), (3500, "2025-09-01"))

    def test_finance_is_asked_once_for_exactly_the_students_on_the_page(self):
        spy = SpyReader(debtors=())
        registry.set_finance_reader(spy)
        self.holds(self.bursar, page_size=2)
        [call] = spy.calls
        school_ref, branch_ref, filters, page, _ = call
        self.assertEqual(school_ref, self.school.pk)
        self.assertIsNone(branch_ref)
        [clause] = filters
        self.assertEqual((clause.field, clause.op), ("student_ref", "in"))
        self.assertEqual(set(clause.value), {str(self.tunde.pk), str(self.kelechi.pk)})

    def test_a_debtor_who_is_not_on_the_page_does_not_appear(self):
        self.finance((self.sade, 999, dt.date(2025, 9, 1)), (self.tunde, TUNDE_OWES, dt.date(2025, 9, 20)))
        body = self.data(self.holds(self.lekki_bursar))
        self.assertEqual({r["student"]["name"] for r in body}, {
            "Tunde Bello", "Kelechi Eze", "Chiamaka Nwosu", "Amina Yusuf",
            "Zainab Aliyu", "Femi Balogun",
        })
        self.assertEqual({r["balance"] for r in body if r["student"]["id"] != self.tunde.pk}, {0})

    def test_it_is_paginated_with_the_standard_envelope(self):
        response = self.holds(self.bursar, page_size=4, page=2)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            [r["student"]["name"] for r in response.data["data"]],
            ["Ife Adeola", "Sade Ojo", "Zainab Aliyu", "Femi Balogun"],
        )
        pagination = response.data["pagination"]
        self.assertEqual(
            (pagination["currentPage"], pagination["pageSize"], pagination["totalItems"], pagination["totalPages"]),
            (2, 4, 8, 2),
        )

    def test_a_page_past_the_end_is_a_404(self):
        self.assertEqual(self.holds(self.bursar, page=9).status_code, 404)

    def test_the_class_filter_narrows_to_one_class(self):
        body = self.data(self.holds(self.bursar, **{"class": self.jss1c.pk}))
        self.assertEqual({r["class"]["name"] for r in body}, {"JSS1 C"})

    def test_a_class_the_caller_cannot_see_or_that_is_another_schools_is_a_404(self):
        self.assertEqual(self.holds(self.lekki_bursar, **{"class": self.jss1c.pk}).status_code, 404)
        self.assertEqual(self.holds(self.bursar, **{"class": self.solo_class.pk}).status_code, 404)
        self.assertEqual(self.holds(self.bursar, **{"class": "x"}).status_code, 404)

    def test_the_branch_filter_narrows_to_one_branch(self):
        body = self.data(self.holds(self.bursar, branch=self.ikeja.pk))
        self.assertEqual({r["class"]["name"] for r in body}, {"JSS1 C"})
        self.assertEqual({r["branch"]["name"] for r in body}, {"Ikeja Branch"})

    def test_a_branch_bound_bursar_sees_only_their_own_branch(self):
        self.assertEqual(
            {r["class"]["name"] for r in self.data(self.holds(self.lekki_bursar))},
            {"JSS1 A", "SSS1 A"},
        )
        self.assertEqual({r["class"]["name"] for r in self.data(self.holds(self.ikeja_bursar))}, {"JSS1 C"})
        self.assertEqual(self.holds(self.ikeja_bursar, branch=self.lekki.pk).status_code, 404)

    def test_the_branch_is_named_at_a_school_with_several(self):
        row = self.data(self.holds(self.bursar))[0]
        self.assertEqual(row["branch"], {"id": self.lekki.pk, "name": "Lekki Branch"})

    def test_an_empty_term_is_an_empty_list_not_an_object(self):
        later = AcademicSession.all_objects.create(
            tenant=self.tenant, name="2026/2027", start_date=dt.date(2026, 9, 7),
            end_date=dt.date(2027, 7, 16), status="DRAFT",
        )
        term = AcademicTerm.all_objects.create(
            tenant=self.tenant, session=later, name="First Term", order_index=1,
            start_date=dt.date(2026, 9, 7), end_date=dt.date(2026, 12, 11),
        )
        response = self.holds(self.bursar, term=term.pk)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["data"], [])

    @staticmethod
    def row_of_student(body, student):
        return next(r for r in body if r["student"]["id"] == student.pk)


class HoldsFilterTests(ClassFixture):
    """Search by name or admission number, and the list of families who owe."""

    def setUp(self):
        super().setUp()
        self.finance((self.tunde, TUNDE_OWES, dt.date(2025, 9, 20)))

    def names(self, response):
        return [r["student"]["name"] for r in self.data(response)]

    def test_search_finds_a_student_by_any_part_of_the_name_ignoring_case(self):
        self.assertEqual(self.names(self.holds(self.bursar, search="  oJo ")), ["Sade Ojo"])

    def test_search_finds_a_student_by_admission_number(self):
        self.assertEqual(self.names(self.holds(self.bursar, search="bfs/002")), ["Tunde Bello"])

    def test_a_search_nobody_matches_is_an_empty_list(self):
        response = self.holds(self.bursar, search="Nobody Here")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.data(response), [])

    def test_owes_keeps_the_students_who_owe_and_says_it_applied(self):
        response = self.holds(self.bursar, owes="true")
        self.assertEqual(self.names(response), ["Tunde Bello"])
        self.assertTrue(response.data["owes_applied"])
        self.assertEqual(response.data["pagination"]["totalItems"], 1)

    def test_owes_keeps_a_held_student_who_has_paid_so_the_hold_can_be_lifted(self):
        self.assertEqual(self.hold(self.bursar, self.zainab).status_code, 201)
        self.assertEqual(
            self.names(self.holds(self.bursar, owes="true")), ["Tunde Bello", "Zainab Aliyu"],
        )

    def test_owes_reads_finance_once_for_the_whole_list(self):
        spy = SpyReader(debtors=())
        registry.set_finance_reader(spy)
        self.holds(self.bursar, owes="true", page_size=2)
        [call] = spy.calls
        [clause] = call[2]
        self.assertEqual(len(clause.value), 8)

    def test_owes_and_search_and_class_narrow_together(self):
        self.finance(
            (self.tunde, TUNDE_OWES, dt.date(2025, 9, 20)), (self.sade, 999, dt.date(2025, 9, 1)),
        )
        self.assertEqual(self.names(self.holds(self.bursar, owes="true", search="sade")), ["Sade Ojo"])
        self.assertEqual(
            self.names(self.holds(self.bursar, owes="true", **{"class": self.jss1a.pk})),
            ["Tunde Bello"],
        )

    def test_owes_is_not_applied_while_finance_cannot_answer(self):
        self.finance_down()
        response = self.holds(self.bursar, owes="true")
        self.assertEqual(len(self.data(response)), 8)
        self.assertFalse(response.data["owes_applied"])
        self.assertFalse(response.data["finance_available"])

    def test_the_list_names_its_term_so_a_hold_can_be_placed_for_it(self):
        self.assertEqual(
            self.holds(self.bursar).data["term"],
            {"id": self.first_term.pk, "name": self.first_term.name},
        )

    def test_the_list_names_every_class_it_can_be_narrowed_to_whatever_the_filters(self):
        for query in ({}, {"class": self.jss1c.pk}, {"search": "Ojo"}):
            with self.subTest(query=query):
                classes = self.holds(self.bursar, **query).data["classes"]
                self.assertEqual([c["name"] for c in classes], ["JSS1 A", "JSS1 B", "JSS1 C", "SSS1 A"])
        lekki = self.holds(self.lekki_bursar).data["classes"]
        self.assertEqual([c["name"] for c in lekki], ["JSS1 A", "JSS1 B", "SSS1 A"])

    def test_owes_applied_is_absent_unless_owes_was_asked_for(self):
        self.assertNotIn("owes_applied", self.holds(self.bursar).data)


class HoldsStateTests(ClassFixture):
    def setUp(self):
        super().setUp()
        self.finance((self.tunde, TUNDE_OWES, dt.date(2025, 9, 20)))

    def test_a_held_student_carries_the_hold_with_who_placed_it_and_why(self):
        ResultHold.objects.create(
            tenant=self.tenant, term=self.first_term, student=self.tunde,
            reason="Fees outstanding since September.", placed_by=self.bursar,
        )
        row = next(r for r in self.data(self.holds(self.bursar)) if r["student"]["id"] == self.tunde.pk)
        self.assertEqual(row["hold"]["reason"], "Fees outstanding since September.")
        self.assertEqual(row["hold"]["placed_by"], "Bimpe Bursar")
        self.assertIsNotNone(row["hold"]["placed_at"])
        self.assertFalse(row["released"])

    def test_an_unheld_student_has_no_hold(self):
        row = next(r for r in self.data(self.holds(self.bursar)) if r["student"]["id"] == self.chiamaka.pk)
        self.assertIsNone(row["hold"])
        self.assertIsNone(row["released_on"])

    def test_a_lifted_hold_is_not_shown(self):
        ResultHold.objects.create(
            tenant=self.tenant, term=self.first_term, student=self.tunde, reason="x",
            placed_by=self.bursar, lifted_at=dt.datetime(2025, 10, 10, tzinfo=dt.timezone.utc),
        )
        row = next(r for r in self.data(self.holds(self.bursar)) if r["student"]["id"] == self.tunde.pk)
        self.assertIsNone(row["hold"])

    def test_a_released_result_says_so_and_when(self):
        class_term = self.make_class_term(self.jss1a, stage=StoredStage.PUBLISHED, structure=self.junior_structure)
        StudentTermResult.objects.create(
            tenant=self.tenant, class_term=class_term, student=self.chiamaka,
            released_at=dt.datetime(2025, 10, 13, 23, 30, tzinfo=dt.timezone.utc),
        )
        row = next(r for r in self.data(self.holds(self.bursar)) if r["student"]["id"] == self.chiamaka.pk)
        self.assertTrue(row["released"])
        # 23:30 UTC is half past midnight in Lagos, the next school day.
        self.assertEqual(row["released_on"], "2025-10-14")

    def test_the_list_never_carries_a_score_a_grade_or_an_average(self):
        self.sheets_in()
        self.comment_all()
        response = self.holds(self.bursar)
        found = {str(key).lower() for key in keys_at_any_depth(response.data)}
        self.assertEqual(found & SCORE_WORDS, set())
        for unwanted in ("Excellent", "64.5", "overall"):
            self.assertNotIn(unwanted, str(response.data))

    def test_a_bursar_with_only_the_hold_keys_gets_no_score_from_any_hold_route(self):
        self.sheets_in()
        placed = self.hold(self.bursar, self.chiamaka)
        self.assertEqual(placed.status_code, 201, placed.data)
        lifted = self.lift(self.bursar, placed.data["data"]["hold"]["id"])
        for response in (self.holds(self.bursar), placed, lifted):
            found = {str(key).lower() for key in keys_at_any_depth(response.data)}
            self.assertEqual(found & SCORE_WORDS, set())

    def test_holds_are_never_placed_by_any_automatic_path(self):
        self.finance((self.tunde, 999_999_999, dt.date(2020, 1, 1)))
        self.drive_to_awaiting()
        self.holds(self.bursar)
        self.preview(self.principal)
        with mock.patch(SEND, return_value=["id"]), mock.patch(
            SUMMARY, return_value=SimpleNamespace(days_not_taken=0, students={}),
        ):
            self.publish(self.principal)
        self.assertFalse(ResultHold.objects.exists())


class FinanceUnavailableTests(ClassFixture):
    def test_balances_are_null_never_zero_and_the_list_says_finance_is_down(self):
        self.finance_down()
        response = self.holds(self.bursar)
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.data["finance_available"])
        for row in self.data(response):
            self.assertIsNone(row["balance"])
            self.assertIsNone(row["oldest_unpaid_on"])
            self.assertIsNone(row["days_owed"])

    def test_a_school_with_no_books_yet_reads_as_unavailable_too(self):
        from schools.core.fal.exceptions import EntityNotProvisioned

        class NoBooks(FakeFinanceReader):
            def debtors(self, *args, **kwargs):
                raise EntityNotProvisioned("No ledger entity.")

        registry.set_finance_reader(NoBooks())
        self.addCleanup(registry.reset)
        response = self.holds(self.bursar)
        self.assertFalse(response.data["finance_available"])
        self.assertIsNone(self.data(response)[0]["balance"])

    def test_holding_still_works_when_finance_is_down(self):
        self.finance_down()
        response = self.hold(self.bursar, self.tunde)
        self.assertEqual(response.status_code, 201, response.data)
        self.assertIsNone(self.data(response)["balance"])
        self.assertEqual(ResultHold.objects.count(), 1)


class HoldsSecurityTests(ClassFixture):
    def setUp(self):
        super().setUp()
        self.finance()

    def test_only_the_hold_view_key_reads_the_list(self):
        for user in (self.eze, self.admin, self.principal, self.hub_reader, self.nobody):
            with self.subTest(user=user.email):
                self.assertEqual(self.holds(user).status_code, 403)
        self.assertEqual(self.holds(self.hold_viewer).status_code, 200)

    def test_the_view_key_alone_cannot_place_or_lift(self):
        self.assertEqual(self.hold(self.hold_viewer, self.tunde).status_code, 403)
        placed = self.hold(self.bursar, self.tunde)
        self.assertEqual(self.lift(self.hold_viewer, placed.data["data"]["hold"]["id"]).status_code, 403)
        self.assertEqual(ResultHold.objects.get().lifted_at, None)

    def test_a_branch_bound_bursar_cannot_hold_a_student_of_another_branch(self):
        response = self.hold(self.lekki_bursar, self.sade)
        self.assertEqual(response.status_code, 404)
        self.assertFalse(ResultHold.objects.exists())
        self.assertEqual(self.hold(self.ikeja_bursar, self.tunde).status_code, 404)

    def test_a_branch_bound_bursar_cannot_lift_a_hold_in_another_branch(self):
        placed = self.hold(self.bursar, self.sade)
        hold_id = placed.data["data"]["hold"]["id"]
        self.assertEqual(self.lift(self.lekki_bursar, hold_id).status_code, 404)
        self.assertIsNone(ResultHold.objects.get().lifted_at)
        self.assertEqual(self.lift(self.ikeja_bursar, hold_id).status_code, 200)

    def test_another_schools_students_terms_and_holds_are_404s(self):
        self.assertEqual(self.hold(self.solo_bursar, self.tunde, term=self.solo_term).status_code, 404)
        self.assertEqual(self.hold(self.solo_bursar, self.solo_pupil, term=self.first_term).status_code, 404)
        placed = self.hold(self.bursar, self.tunde)
        self.assertEqual(self.lift(self.solo_bursar, placed.data["data"]["hold"]["id"]).status_code, 404)
        self.assertEqual(self.holds(self.solo_bursar, term=self.first_term.pk).status_code, 404)

    def test_a_hold_that_does_not_exist_is_a_404(self):
        self.assertEqual(self.lift(self.bursar, 999_999).status_code, 404)


class PlaceHoldTests(ClassFixture):
    def setUp(self):
        super().setUp()
        self.finance((self.tunde, TUNDE_OWES, dt.date(2025, 9, 20)))

    def test_a_hold_is_placed_with_a_reason_and_the_row_comes_back(self):
        response = self.hold(self.bursar, self.tunde)
        self.assertEqual(response.status_code, 201, response.data)
        body = self.data(response)
        self.assertEqual(body["student"]["name"], "Tunde Bello")
        self.assertEqual(body["class"]["name"], "JSS1 A")
        self.assertEqual(body["hold"]["reason"], "Fees outstanding since September.")
        self.assertEqual(body["hold"]["placed_by"], "Bimpe Bursar")
        self.assertEqual(body["balance"], TUNDE_OWES)
        hold = ResultHold.objects.get()
        self.assertEqual((hold.student, hold.term, hold.placed_by), (self.tunde, self.first_term, self.bursar))
        self.assertIsNone(hold.lifted_at)

    def test_a_reason_is_required(self):
        for reason in ("", "   ", None):
            with self.subTest(reason=reason):
                response = self.hold(self.bursar, self.tunde, reason=reason)
                self.assertEqual(response.status_code, 422)
                self.assertEqual(self.code(response), "REASON_REQUIRED")
        self.assertFalse(ResultHold.objects.exists())

    def test_a_missing_reason_key_is_the_same_refusal(self):
        from core.test_utils import TenantAPIClient
        from django.urls import reverse

        response = TenantAPIClient(self.bursar).post(
            reverse("gradebook-holds"),
            {"student": self.tunde.pk, "term": self.first_term.pk}, format="json",
        )
        self.assertEqual(self.code(response), "REASON_REQUIRED")

    def test_a_student_already_held_cannot_be_held_again(self):
        self.hold(self.bursar, self.tunde)
        response = self.hold(self.bursar, self.tunde, reason="Again.")
        self.assertEqual(response.status_code, 409)
        self.assertEqual(self.code(response), "ALREADY_HELD")
        self.assertEqual(ResultHold.objects.count(), 1)

    def test_a_released_result_cannot_be_held(self):
        class_term = self.make_class_term(self.jss1a, stage=StoredStage.PUBLISHED, structure=self.junior_structure)
        StudentTermResult.objects.create(
            tenant=self.tenant, class_term=class_term, student=self.chiamaka,
            released_at=dt.datetime(2025, 10, 13, 9, 0, tzinfo=dt.timezone.utc),
        )
        response = self.hold(self.bursar, self.chiamaka)
        self.assertEqual(response.status_code, 409)
        self.assertEqual(self.code(response), "ALREADY_RELEASED")
        self.assertEqual(self.message(response), "A released result cannot be held.")
        self.assertFalse(ResultHold.objects.exists())

    def test_a_hold_can_be_placed_on_a_result_awaiting_approval_but_not_after_release(self):
        self.drive_to_awaiting()
        self.assertEqual(self.hold(self.bursar, self.tunde).status_code, 201)
        with mock.patch(SEND, return_value=["id"]), mock.patch(
            SUMMARY, return_value=SimpleNamespace(days_not_taken=0, students={}),
        ):
            self.publish(self.principal)
        self.assertEqual(self.code(self.hold(self.bursar, self.chiamaka)), "ALREADY_RELEASED")

    def test_a_student_not_on_a_class_roll_this_term_is_a_404(self):
        self.assertEqual(self.hold(self.bursar, self.obinna).status_code, 404)
        later = AcademicSession.all_objects.create(
            tenant=self.tenant, name="2026/2027", start_date=dt.date(2026, 9, 7),
            end_date=dt.date(2027, 7, 16), status="DRAFT",
        )
        term = AcademicTerm.all_objects.create(
            tenant=self.tenant, session=later, name="First Term", order_index=1,
            start_date=dt.date(2026, 9, 7), end_date=dt.date(2026, 12, 11),
        )
        self.assertEqual(self.hold(self.bursar, self.tunde, term=term).status_code, 404)

    def test_the_body_must_name_a_student_and_a_term(self):
        from core.test_utils import TenantAPIClient
        from django.urls import reverse

        client = TenantAPIClient(self.bursar)
        for body in ({"term": self.first_term.pk, "reason": "x"}, {"student": self.tunde.pk, "reason": "x"}):
            with self.subTest(body=body):
                self.assertEqual(client.post(reverse("gradebook-holds"), body, format="json").status_code, 400)

    def test_the_hold_is_audited(self):
        self.hold(self.bursar, self.tunde)
        event = AuditEvent.objects.get(module_key="GRADEBOOK", entity_type="ResultHold")
        self.assertIn("held Tunde Bello's result for First Term", event.summary)

    def test_a_single_branch_school_holds_and_the_branch_stays_out_of_the_row(self):
        from core.test_utils import TenantAPIClient
        from django.urls import reverse

        response = TenantAPIClient(self.solo_bursar).post(
            reverse("gradebook-holds"),
            {"student": self.solo_pupil.pk, "term": self.solo_term.pk, "reason": "Fees."},
            format="json",
        )
        self.assertEqual(response.status_code, 201, response.data)
        self.assertIsNone(self.data(response)["branch"])
        self.assertEqual(ResultHold.objects.get().tenant, self.solo)


class LiftHoldTests(ClassFixture):
    def setUp(self):
        super().setUp()
        self.finance((self.tunde, TUNDE_OWES, dt.date(2025, 9, 20)))
        patcher = mock.patch(SEND, return_value=["notification-id"])
        self.send = patcher.start()
        self.addCleanup(patcher.stop)
        summary = mock.patch(SUMMARY, return_value=SimpleNamespace(days_not_taken=0, students={}))
        summary.start()
        self.addCleanup(summary.stop)

    def placed(self, student):
        return self.hold(self.bursar, student).data["data"]["hold"]["id"]

    def test_lifting_before_publication_only_lifts(self):
        hold_id = self.placed(self.tunde)
        response = self.lift(self.bursar, hold_id, note="Paid at the bank.")
        self.assertEqual(response.status_code, 200, response.data)
        body = self.data(response)
        self.assertFalse(body["released"])
        self.assertEqual(body["hold"]["lift_note"], "Paid at the bank.")
        self.assertEqual(body["hold"]["lifted_by"], "Bimpe Bursar")
        self.assertIsNone(body["row"]["hold"])
        hold = ResultHold.objects.get()
        self.assertEqual((hold.lifted_by, hold.lift_note), (self.bursar, "Paid at the bank."))
        self.assertIsNotNone(hold.lifted_at)
        self.assertFalse(StudentTermResult.objects.exists())
        self.assertEqual(self.send.call_count, 0)

    def test_lifting_after_publication_releases_the_result_and_emails_the_guardian(self):
        self.drive_to_awaiting()
        hold_id = self.placed(self.chiamaka)
        self.publish(self.principal)
        self.send.reset_mock()
        held = StudentTermResult.objects.get(student=self.chiamaka)
        self.assertEqual((held.email_state, held.released_at), (EmailState.HELD, None))

        response = self.lift(self.bursar, hold_id)
        self.assertEqual(response.status_code, 200, response.data)
        body = self.data(response)
        self.assertTrue(body["released"])
        self.assertTrue(body["row"]["released"])
        released = StudentTermResult.objects.get(student=self.chiamaka)
        self.assertIsNotNone(released.released_at)
        self.assertEqual(released.email_state, EmailState.SENT)
        self.assertIsNotNone(released.emailed_at)
        call = self.send.call_args.kwargs
        self.assertEqual(call["event_key"], "gradebook.report_card_ready")
        self.assertEqual(call["context"]["student_first_name"], "Chiamaka")
        self.assertEqual(call["unregistered_recipients"][0].email, "ngozi.nwosu@example.ng")
        self.assertEqual(self.send.call_count, 1)

    def test_a_released_pupil_with_a_portal_account_is_told_too(self):
        from schools.vs_students.services import portal_access
        from vs_user.models import User

        self.chiamaka.email = "chiamaka.n@example.ng"
        self.chiamaka.save(update_fields=["email"])
        portal_access.issue(self.chiamaka, actor=self.admin)
        self.chiamaka.refresh_from_db()
        User.objects.filter(pk=self.chiamaka.user_id).update(status="ACTIVE", is_active=True)

        self.drive_to_awaiting()
        hold_id = self.placed(self.chiamaka)
        self.publish(self.principal)
        self.send.reset_mock()
        self.lift(self.bursar, hold_id)

        pupil = [c.kwargs for c in self.send.call_args_list
                 if c.kwargs["event_key"] == "gradebook.result_released"]
        self.assertEqual(len(pupil), 1)
        self.assertEqual(pupil[0]["recipients"], [User.objects.get(pk=self.chiamaka.user_id)])
        self.assertEqual(set(pupil[0]["context"]), {
            "student_first_name", "term_name", "session_name", "tenant_name",
        })

    def test_lifting_after_publication_for_a_guardian_with_no_email_records_it_and_sends_nothing(self):
        self.drive_to_awaiting()
        hold_id = self.placed(self.tunde)
        self.publish(self.principal)
        self.send.reset_mock()
        self.lift(self.bursar, hold_id)
        released = StudentTermResult.objects.get(student=self.tunde)
        self.assertIsNotNone(released.released_at)
        self.assertEqual(released.email_state, EmailState.NO_EMAIL)
        self.assertEqual(self.send.call_count, 0)

    def test_an_email_waits_for_the_commit(self):
        self.drive_to_awaiting()
        hold_id = self.placed(self.chiamaka)
        self.publish(self.principal)
        self.send.reset_mock()
        from core.test_utils import TenantAPIClient
        from django.urls import reverse

        with self.captureOnCommitCallbacks(execute=False) as callbacks:
            TenantAPIClient(self.bursar).post(
                reverse("gradebook-hold-lift", kwargs={"hold_id": hold_id}), {}, format="json",
            )
        self.assertEqual(self.send.call_count, 0)
        self.assertEqual(len(callbacks), 1)

    def test_a_hold_already_lifted_cannot_be_lifted_again(self):
        hold_id = self.placed(self.tunde)
        self.lift(self.bursar, hold_id)
        response = self.lift(self.bursar, hold_id)
        self.assertEqual(response.status_code, 409)
        self.assertEqual(self.code(response), "HOLD_NOT_OPEN")

    def test_a_lifted_student_can_be_held_again_before_release(self):
        hold_id = self.placed(self.tunde)
        self.lift(self.bursar, hold_id)
        self.assertEqual(self.hold(self.bursar, self.tunde, reason="New arrears.").status_code, 201)
        self.assertEqual(ResultHold.objects.count(), 2)
        self.assertEqual(ResultHold.objects.filter(lifted_at__isnull=True).count(), 1)

    def test_the_note_is_optional(self):
        hold_id = self.placed(self.tunde)
        response = self.lift(self.bursar, hold_id)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(ResultHold.objects.get().lift_note, "")

    def test_the_lift_is_audited_and_says_when_it_released(self):
        self.drive_to_awaiting()
        hold_id = self.placed(self.chiamaka)
        self.publish(self.principal)
        self.lift(self.bursar, hold_id)
        event = AuditEvent.objects.get(module_key="GRADEBOOK", summary__contains="lifted the hold")
        self.assertIn("lifted the hold on Chiamaka Nwosu's result for First Term and released it", event.summary)

    def test_publishing_leaves_a_held_class_term_published_and_a_lift_does_not_touch_it(self):
        self.drive_to_awaiting()
        hold_id = self.placed(self.chiamaka)
        self.publish(self.principal)
        self.lift(self.bursar, hold_id)
        self.assertEqual(ClassTerm.objects.get(school_class=self.jss1a).stage, StoredStage.PUBLISHED)


class HugeIdentifierTests(ClassFixture):
    def test_an_identifier_larger_than_the_database_holds_is_a_404_not_a_crash(self):
        self.finance()
        self.assertEqual(self.lift(self.bursar, 10**30).status_code, 404)
        response = self.call(
            "get", self.principal, "gradebook-broadsheet", class_id=10**30, term=self.first_term,
        )
        self.assertEqual(response.status_code, 404)
        response = self.call(
            "get", self.eze, "gradebook-compile", class_id=self.jss1a.pk, student_id=10**30,
            term=self.first_term,
        )
        self.assertEqual(response.status_code, 404)

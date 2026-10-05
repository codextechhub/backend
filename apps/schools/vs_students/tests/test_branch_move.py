"""Moving a pupil to another branch, from the school app.

Tunde attends Ikeja at Corona, sits in JSS1 Ikeja, owes N400,000 for the term
and holds N10,000 of credit. Mrs Bello runs the whole school; Mrs Adeyemi works
at Ikeja only. Greenfield is another school, with one branch. The fixture and
the finance figures are the FAL's (``schools/core/fal/tests/test_pupil_move.py``);
these tests are about the route: who may move a pupil, what the move changes,
and that a refused move changes nothing.
"""
from __future__ import annotations

import datetime

from rest_framework.test import APIClient

from schools.core.fal.tests.test_pupil_move import CREDIT, TERM_FEE, PupilMoveFixture, net
from vs_user.tokens import CodeXRefreshToken

MOVE_KEYS = (
    "school.students.view", "school.students.change_branch", "academics.classes.assign",
)


class BranchMoveFixture(PupilMoveFixture):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.bello = cls.user_for(cls.corona, "bello@corona.test")
        for key in MOVE_KEYS + ("finance.invoice.view",):
            cls.grant(cls.bello, key)
        cls.okafor = cls.user_for(cls.corona, "okafor@corona.test")
        for key in MOVE_KEYS:
            cls.grant(cls.okafor, key)
        cls.adeyemi = cls.user_for(cls.corona, "adeyemi@corona.test", branch=cls.ikeja)
        for key in MOVE_KEYS + ("finance.invoice.view",):
            cls.grant(cls.adeyemi, key, branch=cls.ikeja)
        cls.reader = cls.user_for(cls.corona, "reader@corona.test")
        cls.grant(cls.reader, "school.students.view")
        cls.green_head = cls.user_for(cls.greenfield, "head@greenfield.test")
        for key in MOVE_KEYS:
            cls.grant(cls.green_head, key)

    def call(self, user, method, path, body=None):
        client = APIClient()
        client.credentials(
            HTTP_AUTHORIZATION=f"Bearer {CodeXRefreshToken.for_user(user).access_token}",
        )
        url = f"/v1/students/{path}?tenant={user.tenant.slug}"
        if method == "get":
            return client.get(url)
        return client.post(url, body or {}, format="json")

    def move(self, user=None, pupil=None, **body):
        body.setdefault("to_branch", str(self.lekki.pk))
        body.setdefault("school_class", self.lekki_class.pk)
        body.setdefault("reason", "Family moved to Lekki")
        return self.call(user or self.bello, "post", f"{(pupil or self.tunde).pk}/move-branch/", body)

    def preview(self, user=None, **body):
        body.setdefault("to_branch", str(self.lekki.pk))
        return self.call(user or self.bello, "post", f"{self.tunde.pk}/move-branch/preview/", body)

    def assertUnmoved(self):
        from schools.vs_students.models import StudentBranchMove

        self.tunde.refresh_from_db()
        self.bill.refresh_from_db()
        self.customer.refresh_from_db()
        self.assertEqual(self.tunde.branch_id, self.ikeja.pk)
        self.assertEqual(self.bill.branch_id, self.ikeja.pk)
        self.assertEqual(self.customer.branch_id, self.ikeja.pk)
        self.assertTrue(self.tunde.enrolments.get(is_active=True).school_class_id == self.ikeja_class.pk)
        self.assertFalse(StudentBranchMove.all_objects.exists())
        self.assertFalse(self.moves().exists())


class MovePupilTests(BranchMoveFixture):
    """Test 4.14: Tunde moves from Ikeja to Lekki and his fee account goes with him."""

    def test_the_pupil_class_and_account_move_and_lekki_owes_ikeja(self):
        from schools.vs_students.constants import TransferReason
        from schools.vs_students.models import StudentBranchMove

        unearned = self.unearned()
        ikeja_revenue = net(self.ledger("4100"), self.ikeja)

        res = self.move()

        self.assertEqual(res.status_code, 200, res.data)
        self.tunde.refresh_from_db()
        self.assertEqual(self.tunde.branch_id, self.lekki.pk)
        now = self.tunde.enrolments.get(is_active=True)
        self.assertEqual(now.school_class_id, self.lekki_class.pk)
        self.assertEqual(now.reason, TransferReason.BRANCH_MOVE)
        self.placement.refresh_from_db()
        self.assertFalse(self.placement.is_active)

        self.bill.refresh_from_db()
        self.customer.refresh_from_db()
        self.assertEqual(self.bill.branch_id, self.lekki.pk)
        self.assertEqual(self.customer.branch_id, self.lekki.pk)
        owed_to_ikeja = TERM_FEE - CREDIT - unearned
        ib = self.ledger("1260")
        self.assertEqual(net(ib, self.ikeja, self.lekki), owed_to_ikeja)
        self.assertEqual(net(ib, self.lekki, self.ikeja), -owed_to_ikeja)
        self.assertEqual(net(self.ledger("4100"), self.ikeja), ikeja_revenue)
        self.assertEqual(net(self.ledger("4100"), self.lekki), 0)

        data = res.data["data"]
        self.assertEqual(
            data["totals"],
            {"owed_amount": TERM_FEE, "credit_amount": CREDIT, "deferred_amount": unearned,
             "inter_branch_amount": owed_to_ikeja, "amount": TERM_FEE - CREDIT},
        )
        self.assertEqual(
            [b["number"] for b in data["accounts"][0]["bills"]], [self.bill.document_number],
        )
        move = StudentBranchMove.all_objects.get()
        self.assertEqual(data["move"], move.pk)
        self.assertEqual((move.from_branch_id, move.to_branch_id), (self.ikeja.pk, self.lekki.pk))
        self.assertEqual((move.from_enrolment_id, move.to_enrolment_id), (self.placement.pk, now.pk))
        self.assertTrue(self.moves().get().move_key.startswith(f"pupil-move:M{move.pk}:"))

    def test_the_move_is_in_the_pupils_history(self):
        self.move()

        res = self.call(self.bello, "get", f"{self.tunde.pk}/history/")

        self.assertEqual(res.status_code, 200, res.data)
        rows = res.data["data"] if isinstance(res.data["data"], list) else res.data["data"]["results"]
        branch_rows = [r for r in rows if r["kind"] == "branch"]
        self.assertEqual(len(branch_rows), 1, rows)
        self.assertIn("moved from Ikeja to Lekki", branch_rows[0]["text"])
        self.assertIn("fee account moved", branch_rows[0]["text"])
        self.assertNotIn("Family moved", branch_rows[0]["text"])

    def test_sending_the_same_move_again_moves_nothing_twice(self):
        self.assertEqual(self.move().status_code, 200)
        ib = net(self.ledger("1260"), self.ikeja)

        again = self.move()

        self.assertEqual(again.status_code, 409, again.data)
        self.assertEqual(again.data["error"]["code"], "ALREADY_AT_BRANCH")
        self.assertEqual(self.moves().count(), 1)
        self.assertEqual(net(self.ledger("1260"), self.ikeja), ib)

    def test_a_refused_finance_move_leaves_the_pupil_where_they_were(self):
        """Lekki has closed this month, so its books take nothing, and Tunde stays at Ikeja."""
        from vs_finance.models import BranchFiscalPeriod, LedgerEntity
        from vs_finance.posting import resolve_period

        entity = LedgerEntity.objects.get(pk=self.corona_books.entity_ref)
        BranchFiscalPeriod.objects.create(
            period=resolve_period(entity, self.today), branch=self.lekki, status="CLOSED",
        )

        res = self.move()

        self.assertEqual(res.status_code, 409, res.data)
        self.assertEqual(res.data["error"]["code"], "PERIOD_CLOSED")
        self.assertUnmoved()

    def test_a_preview_shows_what_would_move_and_moves_nothing(self):
        unearned = self.unearned()

        res = self.preview()

        self.assertEqual(res.status_code, 200, res.data)
        data = res.data["data"]
        self.assertIsNone(data["move"])
        self.assertEqual(data["totals"]["inter_branch_amount"], TERM_FEE - CREDIT - unearned)
        self.assertEqual(data["accounts"][0]["transfer"], None)
        self.assertUnmoved()

    def test_a_mover_without_finance_reading_sees_no_figures(self):
        res = self.preview(self.okafor)

        self.assertEqual(res.status_code, 200, res.data)
        data = res.data["data"]
        self.assertFalse(data["figures_shown"])
        self.assertNotIn("totals", data)
        self.assertNotIn("owed_amount", data["accounts"][0])
        self.assertNotIn("bills", data["accounts"][0])
        self.assertEqual(data["accounts"][0]["invoice_count"], 1)

    def test_the_form_offers_the_other_branch_and_asks_for_a_class(self):
        res = self.call(self.bello, "get", f"{self.tunde.pk}/move-branch/")

        self.assertEqual(res.status_code, 200, res.data)
        data = res.data["data"]
        self.assertEqual([b["id"] for b in data["branches"]], [self.lekki.pk])
        self.assertTrue(data["needs_class"])
        self.assertEqual(data["school_class_name"], "JSS1 Ikeja")


class MoveRulesTests(BranchMoveFixture):
    def test_a_pupil_in_a_branch_class_needs_a_class_at_the_new_branch(self):
        res = self.move(school_class=None)
        self.assertEqual(res.status_code, 422, res.data)
        self.assertEqual(res.data["error"]["code"], "CLASS_REQUIRED")
        self.assertUnmoved()

    def test_a_class_at_another_branch_is_refused(self):
        res = self.move(school_class=self.ikeja_class.pk)
        self.assertEqual(res.status_code, 422, res.data)
        self.assertEqual(res.data["error"]["code"], "BRANCH_SCOPE_CONFLICT")
        self.assertUnmoved()

    def test_a_pupil_in_a_school_wide_class_keeps_it(self):
        self.placement.school_class = self.shared_class
        self.placement.save(update_fields=["school_class"])

        res = self.move(school_class=None)

        self.assertEqual(res.status_code, 200, res.data)
        self.assertEqual(res.data["data"]["school_class"], self.shared_class.pk)
        self.assertTrue(self.tunde.enrolments.get(is_active=True).pk == self.placement.pk)

    def test_a_move_cannot_be_dated_in_the_future(self):
        res = self.move(effective_date=str(self.today + datetime.timedelta(days=1)))
        self.assertEqual(res.status_code, 422, res.data)
        self.assertEqual(res.data["error"]["code"], "INVALID_EFFECTIVE_DATE")
        self.assertUnmoved()

    def test_a_move_cannot_be_dated_before_the_pupil_joined_their_class(self):
        res = self.move(effective_date=str(self.today - datetime.timedelta(days=61)))
        self.assertEqual(res.status_code, 422, res.data)
        self.assertEqual(res.data["error"]["code"], "INVALID_EFFECTIVE_DATE")

    def test_a_withdrawn_pupil_does_not_move(self):
        from schools.vs_students.models import Student

        Student.all_objects.filter(pk=self.tunde.pk).update(status="WITHDRAWN")
        res = self.move()
        self.assertEqual(res.status_code, 422, res.data)
        self.assertEqual(res.data["error"]["code"], "NOT_ON_ROLL")

    def test_the_same_branch_is_refused(self):
        res = self.move(to_branch=str(self.ikeja.pk), school_class=self.ikeja_class.pk)
        self.assertEqual(res.status_code, 409, res.data)
        self.assertEqual(res.data["error"]["code"], "ALREADY_AT_BRANCH")

    def test_a_reason_is_required(self):
        res = self.move(reason="  ")
        self.assertEqual(res.status_code, 400, res.data)
        self.assertUnmoved()


class MoveAccessTests(BranchMoveFixture):
    """Security first: the key, both branches, and the school's own pupils only."""

    def test_without_the_key_the_move_is_refused(self):
        for path in (f"{self.tunde.pk}/move-branch/", f"{self.tunde.pk}/move-branch/preview/"):
            res = self.call(self.reader, "post", path, {
                "to_branch": str(self.lekki.pk), "school_class": self.lekki_class.pk,
                "reason": "x",
            })
            self.assertEqual(res.status_code, 403, (path, res.data))
        self.assertUnmoved()

    def test_naming_a_class_also_needs_the_class_key(self):
        nnamdi = self.user_for(self.corona, "nnamdi@corona.test")
        for key in ("school.students.view", "school.students.change_branch"):
            self.grant(nnamdi, key)
        res = self.move(nnamdi)
        self.assertEqual(res.status_code, 403, res.data)
        self.assertUnmoved()

    def test_an_ikeja_only_administrator_cannot_push_a_pupil_into_lekki(self):
        res = self.move(self.adeyemi)
        self.assertEqual(res.status_code, 403, res.data)
        self.assertIn("both branches", str(res.data))
        self.assertUnmoved()
        offer = self.call(self.adeyemi, "get", f"{self.tunde.pk}/move-branch/")
        self.assertEqual(offer.data["data"]["branches"], [])

    def test_an_ikeja_only_administrator_cannot_reach_a_lekki_pupil(self):
        amaka = self.student(self.corona, self.lekki, first="Amaka")
        res = self.move(self.adeyemi, pupil=amaka, to_branch=str(self.ikeja.pk),
                        school_class=self.ikeja_class.pk)
        self.assertEqual(res.status_code, 404, res.data)

    def test_another_school_cannot_move_this_pupil(self):
        res = self.move(self.green_head)
        self.assertEqual(res.status_code, 404, res.data)
        self.assertUnmoved()

    def test_a_branch_of_another_school_is_refused(self):
        res = self.move(to_branch=str(self.greenfield_main.pk))
        self.assertEqual(res.status_code, 400, res.data)
        self.assertUnmoved()

    def test_a_school_with_one_branch_has_nowhere_to_move_a_pupil(self):
        offer = self.call(self.green_head, "get", f"{self.ada.pk}/move-branch/")
        self.assertEqual(offer.status_code, 200, offer.data)
        self.assertEqual(offer.data["data"]["branches"], [])

        res = self.move(self.green_head, pupil=self.ada,
                        to_branch=str(self.greenfield_main.pk), school_class=None)
        self.assertEqual(res.status_code, 409, res.data)
        self.assertEqual(res.data["error"]["code"], "ONE_BRANCH")

"""The release list of a class: guardian, result state and what became of the email."""
from __future__ import annotations

from unittest import mock

from schools.vs_gradebook.models import ResultHold, StudentTermResult

from .card_base import RELEASED_AT, SEND, CardFixture


def by_name(body):
    return {row["student"]["name"]: row for row in body["rows"]}


class ReleaseBeforePublicationTests(CardFixture):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.drive_to_awaiting()

    def test_every_row_is_not_published_and_has_no_email_state(self):
        body = self.data(self.release(self.releaser))
        self.assertEqual(body["stage"], "AWAITING")
        self.assertIsNone(body["published_at"])
        self.assertEqual(body["class_size"], 4)
        self.assertEqual(len(body["rows"]), 4)
        for row in body["rows"]:
            self.assertEqual(row["result_state"], "NOT_PUBLISHED")
            self.assertIsNone(row["email_state"])
            self.assertIsNone(row["emailed_at"])
            self.assertIsNone(row["released_at"])

    def test_rows_are_ordered_by_student_name_and_carry_the_admission_number(self):
        body = self.data(self.release(self.releaser))
        self.assertEqual(
            [r["student"]["name"] for r in body["rows"]],
            ["Tunde Bello", "Kelechi Eze", "Chiamaka Nwosu", "Amina Yusuf"],
        )
        self.assertEqual(body["rows"][0]["student"]["admission_number"], "BFS/002")

    def test_a_hold_already_placed_is_counted_as_to_be_held_but_the_row_is_not_published(self):
        ResultHold.objects.create(
            tenant=self.tenant, term=self.first_term, student=self.tunde,
            reason="Fees.", placed_by=self.bursar,
        )
        body = self.data(self.release(self.releaser))
        self.assertEqual(body["held_count"], 1)
        self.assertEqual(by_name(body)["Tunde Bello"]["result_state"], "NOT_PUBLISHED")

    def test_the_primary_guardian_is_named_with_the_address_the_school_writes_to(self):
        rows = by_name(self.data(self.release(self.releaser)))
        self.assertEqual(
            rows["Chiamaka Nwosu"]["guardian"],
            {"name": "Mrs. Ngozi Nwosu", "email": "ngozi.nwosu@example.ng"},
        )
        self.assertEqual(rows["Tunde Bello"]["guardian"], {"name": "Mrs. Aduke Bello", "email": None})

    def test_a_student_with_no_primary_guardian_has_a_null_guardian(self):
        rows = by_name(self.data(self.release(self.releaser)))
        from schools.vs_students.models import StudentGuardian

        StudentGuardian.all_objects.filter(student=self.chiamaka).delete()
        rows = by_name(self.data(self.release(self.releaser)))
        self.assertIsNone(rows["Chiamaka Nwosu"]["guardian"])

    def test_the_stage_before_the_class_is_submitted_is_read_off_its_sheets(self):
        from schools.vs_gradebook.models import ClassTerm

        ClassTerm.objects.filter(school_class=self.jss1a).update(stage=None)
        self.assertEqual(self.data(self.release(self.releaser))["stage"], "COMPILING")


class ReleaseAfterPublicationTests(CardFixture):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.class_term = cls.publish_jss1a(held=[cls.amina])

    def test_each_row_reads_its_result_and_email_state(self):
        body = self.data(self.release(self.releaser))
        rows = by_name(body)
        self.assertEqual(body["stage"], "PUBLISHED")
        self.assertIsNotNone(body["published_at"])
        self.assertEqual(body["held_count"], 1)
        self.assertEqual(body["class_size"], 4)
        expected = {
            "Chiamaka Nwosu": ("RELEASED", "SENT"),
            "Kelechi Eze": ("RELEASED", "SENT"),
            "Tunde Bello": ("RELEASED", "NO_EMAIL"),
            "Amina Yusuf": ("HELD", "HELD"),
        }
        for name, (state, email) in expected.items():
            with self.subTest(student=name):
                self.assertEqual((rows[name]["result_state"], rows[name]["email_state"]), (state, email))

    def test_a_sent_row_carries_when_it_was_emailed_and_a_held_row_carries_neither_time(self):
        rows = by_name(self.data(self.release(self.releaser)))
        self.assertIsNotNone(rows["Chiamaka Nwosu"]["emailed_at"])
        self.assertIsNotNone(rows["Chiamaka Nwosu"]["released_at"])
        self.assertIsNone(rows["Tunde Bello"]["emailed_at"])
        self.assertIsNone(rows["Amina Yusuf"]["released_at"])
        self.assertIsNone(rows["Amina Yusuf"]["emailed_at"])

    def test_a_released_row_whose_send_failed_reads_not_sent_never_sent(self):
        StudentTermResult.objects.filter(student=self.chiamaka).update(
            email_state=None, emailed_at=None,
        )
        rows = by_name(self.data(self.release(self.releaser)))
        self.assertEqual(rows["Chiamaka Nwosu"]["result_state"], "RELEASED")
        self.assertEqual(rows["Chiamaka Nwosu"]["email_state"], "NOT_SENT")

    def test_a_guardian_who_shares_two_children_is_named_on_both_rows(self):
        rows = by_name(self.data(self.release(self.releaser)))
        self.assertEqual(rows["Kelechi Eze"]["guardian"]["name"], "Mrs. Ada Eze")
        self.assertEqual(rows["Amina Yusuf"]["guardian"]["name"], "Mrs. Ada Eze")

    def test_lifting_the_hold_moves_the_row_to_released(self):
        hold = ResultHold.objects.get(student=self.amina)
        with mock.patch(SEND, return_value=["sent"]):
            self.assertEqual(self.lift(self.bursar, hold.pk).status_code, 200)
        rows = by_name(self.data(self.release(self.releaser)))
        self.assertEqual(rows["Amina Yusuf"]["result_state"], "RELEASED")
        self.assertEqual(self.data(self.release(self.releaser))["held_count"], 0)

    def test_a_student_who_joined_after_publication_is_not_published(self):
        from schools.vs_students.models import ClassEnrolment

        ClassEnrolment.all_objects.filter(student=self.obinna).update(
            effective_date=self.first_term.start_date,
        )
        rows = by_name(self.data(self.release(self.releaser)))
        self.assertEqual(rows["Obinna Okafor"]["result_state"], "NOT_PUBLISHED")
        self.assertIsNone(rows["Obinna Okafor"]["email_state"])
        self.assertEqual(self.data(self.release(self.releaser))["class_size"], 4)

    def test_a_student_who_left_the_class_after_publication_still_has_a_row(self):
        from schools.vs_students.models import ClassEnrolment

        ClassEnrolment.all_objects.filter(student=self.kelechi).delete()
        rows = by_name(self.data(self.release(self.releaser)))
        self.assertEqual(rows["Kelechi Eze"]["result_state"], "RELEASED")

    def test_the_release_time_is_returned_as_recorded(self):
        rows = by_name(self.data(self.release(self.releaser)))
        self.assertTrue(rows["Chiamaka Nwosu"]["released_at"].startswith(RELEASED_AT.strftime("%Y-%m-%dT09:00")))


class ReleaseSecurityTests(CardFixture):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.publish_jss1a(held=[cls.amina])

    def test_only_the_release_key_may_read_the_list(self):
        for user in (self.result_reader, self.principal, self.eze, self.admin, self.bursar, self.nobody):
            with self.subTest(user=user.email):
                self.assertEqual(self.release(user).status_code, 403)
        self.assertEqual(self.release(self.releaser).status_code, 200)

    def test_a_branch_pinned_reader_reads_their_own_branchs_class(self):
        self.assertEqual(self.release(self.lekki_releaser).status_code, 200)

    def test_another_branchs_class_is_a_404(self):
        self.assertEqual(self.release(self.ikeja_releaser).status_code, 404)

    def test_another_schools_reader_is_told_no_such_class(self):
        response = self.call(
            "get", self.solo_releaser, "gradebook-release", class_id=self.jss1a.pk,
            term=self.solo_term,
        )
        self.assertEqual(response.status_code, 404)

    def test_a_term_of_another_school_is_a_404(self):
        self.assertEqual(self.release(self.releaser, term=self.solo_term).status_code, 404)

    def test_a_term_of_another_school_year_is_refused_for_the_class(self):
        from schools.vs_academics.models import AcademicSession, AcademicTerm
        import datetime as dt

        year = AcademicSession.all_objects.create(
            tenant=self.tenant, name="2026/2027", start_date=dt.date(2026, 9, 7),
            end_date=dt.date(2027, 7, 16), status="DRAFT",
        )
        term = AcademicTerm.all_objects.create(
            tenant=self.tenant, session=year, name="First Term", order_index=1,
            start_date=dt.date(2026, 9, 7), end_date=dt.date(2026, 12, 11),
        )
        response = self.release(self.releaser, term=term)
        self.assertGreaterEqual(response.status_code, 400)
        self.assertEqual(self.code(response), "INVALID_TERM")

    def test_the_list_is_the_same_for_every_reader_of_the_key(self):
        a = self.data(self.release(self.releaser))
        b = self.data(self.release(self.lekki_releaser))
        self.assertEqual(a["rows"], b["rows"])


class ReleaseAtAOneBranchSchoolTests(CardFixture):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.publish_sunrise()

    def test_sunrise_lists_its_two_pupils_released_with_their_guardians(self):
        response = self.call(
            "get", self.solo_releaser, "gradebook-release", class_id=self.solo_class.pk,
            term=self.solo_term,
        )
        self.assertEqual(response.status_code, 200, response.data)
        rows = by_name(self.data(response))
        self.assertEqual(set(rows), {"Ngozi Eke", "Tobi Ojo"})
        for row in rows.values():
            self.assertEqual(row["result_state"], "RELEASED")
        self.assertEqual(rows["Ngozi Eke"]["guardian"]["name"], "Mr. Okoro")

    def test_brightfields_reader_cannot_read_sunrises_class(self):
        response = self.call(
            "get", self.releaser, "gradebook-release", class_id=self.solo_class.pk,
            term=self.first_term,
        )
        self.assertEqual(response.status_code, 404)

"""The school's reasons for excusing an absence: listed, added, renamed, retired and restored.

A reason is never deleted, because a mark that used it must keep it. Retiring
one offers it on no new mark; the marks that used it still show it.
"""
from __future__ import annotations

import datetime as dt

from django.urls import reverse

from core.test_utils import TenantAPIClient
from vs_audit.models import AuditEvent
from vs_rbac.tests.helpers import make_assignment, make_school_admin

from ..constants import PERM_REGISTER_VIEW, PERM_SETTINGS_UPDATE
from ..models import ClassRegister, ReasonCode, StudentMark
from ..services.reasons import ensure_default_reason_codes
from .base import NOW, TODAY, AttendanceFixture, make_key_role

LIST = "attendance-reason-codes"


class _Reasons(AttendanceFixture):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        role = make_key_role(
            cls.tenant, "Configurer", "configurer", (PERM_REGISTER_VIEW, PERM_SETTINGS_UPDATE),
        )
        cls.configurer = make_school_admin(
            None, email="config@brightfield.test", tenant=cls.tenant,
            first_name="Chioma", last_name="Dike",
        )
        make_assignment(cls.tenant, cls.configurer, role, branch=None)
        cls.branch_configurer = make_school_admin(
            cls.ikeja, email="config@ikeja.test", first_name="Ibe", last_name="Ugo",
        )
        make_assignment(cls.tenant, cls.branch_configurer, role, branch=cls.ikeja)
        solo_role = make_key_role(
            cls.solo, "Configurer", "configurer", (PERM_REGISTER_VIEW, PERM_SETTINGS_UPDATE),
        )
        cls.solo_configurer = make_school_admin(
            None, email="config@sunrise.test", tenant=cls.solo,
            first_name="Seun", last_name="Ola",
        )
        make_assignment(cls.solo, cls.solo_configurer, solo_role, branch=None)
        ensure_default_reason_codes(cls.tenant)
        ensure_default_reason_codes(cls.solo)

    def list(self, user=None, **params):
        response = TenantAPIClient(user or self.configurer).get(reverse(LIST), params)
        self.assertEqual(response.status_code, 200, response.data)
        return self.data(response)

    def create(self, body, user=None):
        return TenantAPIClient(user or self.configurer).post(
            reverse(LIST), body, format="json",
        )

    def change(self, reason_id, body, user=None):
        return TenantAPIClient(user or self.configurer).patch(
            reverse("attendance-reason-code", kwargs={"reason_id": reason_id}),
            body, format="json",
        )

    def verb(self, reason_id, verb, user=None):
        return TenantAPIClient(user or self.configurer).post(
            reverse(f"attendance-reason-code-{verb}", kwargs={"reason_id": reason_id}),
            {}, format="json",
        )

    def by_name(self, **params):
        return {row["name"]: row for row in self.list(**params)}

    def reason(self, name="Sick", tenant=None):
        return ReasonCode.objects.get(tenant=tenant or self.tenant, name=name)


class ListTests(_Reasons):
    def test_the_list_is_the_schools_seven_in_the_order_a_drawer_offers_them(self):
        rows = self.list()
        self.assertEqual(
            [r["name"] for r in rows],
            [
                "Sick", "Medical appointment", "Family matter", "Religious observance",
                "Travel", "Representing the school", "Other",
            ],
        )
        sick = rows[0]
        self.assertEqual(
            set(sick),
            {"id", "name", "counts_as_authorised", "requires_note", "retired", "used_this_term"},
        )
        self.assertEqual(
            (sick["counts_as_authorised"], sick["requires_note"], sick["retired"], sick["used_this_term"]),
            (True, False, False, 0),
        )
        self.assertTrue(rows[-1]["requires_note"])

    def test_a_school_gets_its_defaults_the_first_time_it_asks(self):
        ReasonCode.all_objects.filter(tenant=self.tenant).delete()
        self.assertEqual(len(self.list()), 7)

    def test_the_active_filter_leaves_out_what_is_retired(self):
        self.assertEqual(self.verb(self.reason("Travel").pk, "retire").status_code, 200)
        self.assertIn("Travel", self.by_name())
        self.assertEqual(self.by_name()["Travel"]["retired"], True)
        self.assertNotIn("Travel", self.by_name(active="1"))
        self.assertEqual(len(self.list(active="1")), 6)

    def test_every_holder_of_the_register_key_may_read_the_list(self):
        self.assertEqual(len(self.list(self.eze)), 7)
        self.assertEqual(
            TenantAPIClient(self.nobody).get(reverse(LIST)).status_code, 403,
        )

    def test_how_many_marks_used_a_reason_counts_this_term_only(self):
        self.assertEqual(
            self.mark(
                self.eze, self.jss1a, self.kelechi, "E",
                reason_code=self.reason("Sick").pk, note="Fever",
            ).status_code, 200,
        )
        # A mark after the term ended is not this term's.
        register = ClassRegister.objects.create(
            tenant=self.tenant, school_class=self.jss1a, date=dt.date(2026, 1, 5),
            branch=self.lekki, session=self.year, submitted_at=NOW, submitted_by=self.admin,
        )
        StudentMark.objects.create(
            tenant=self.tenant, register=register, student=self.kelechi, mark="E",
            reason_code=self.reason("Sick"), marked_by=self.admin,
        )
        rows = self.by_name()
        self.assertEqual(rows["Sick"]["used_this_term"], 1)
        self.assertEqual(rows["Travel"]["used_this_term"], 0)

    def test_another_schools_marks_do_not_count_here(self):
        register = ClassRegister.objects.create(
            tenant=self.solo, school_class=self.solo_class, date=TODAY,
            branch=self.solo_branch, session=self.solo_class.session,
            submitted_at=NOW, submitted_by=self.solo_teacher,
        )
        StudentMark.objects.create(
            tenant=self.solo, register=register, student=self.solo_pupil, mark="E",
            reason_code=self.reason("Sick", self.solo), marked_by=self.solo_teacher,
        )
        self.assertEqual(self.by_name()["Sick"]["used_this_term"], 0)
        theirs = {r["name"]: r for r in self.list(self.solo_configurer)}
        self.assertEqual(theirs["Sick"]["used_this_term"], 1)


class CreateTests(_Reasons):
    def test_a_new_reason_goes_to_the_end_of_the_list(self):
        response = self.create({"name": "Bereavement", "requires_note": True})
        self.assertEqual(response.status_code, 201, response.data)
        row = self.data(response)
        self.assertEqual(row["name"], "Bereavement")
        self.assertEqual(
            (row["counts_as_authorised"], row["requires_note"], row["retired"], row["used_this_term"]),
            (True, True, False, 0),
        )
        self.assertEqual(self.list()[-1]["name"], "Bereavement")

    def test_the_flags_default_to_authorised_and_no_note(self):
        row = self.data(self.create({"name": "Exams elsewhere"}))
        self.assertEqual((row["counts_as_authorised"], row["requires_note"]), (True, False))
        row = self.data(self.create({"name": "Unexplained", "counts_as_authorised": False}))
        self.assertFalse(row["counts_as_authorised"])

    def test_the_name_is_trimmed(self):
        self.assertEqual(self.data(self.create({"name": "  Bereavement  "}))["name"], "Bereavement")

    def test_a_name_that_is_blank_or_too_long_is_refused(self):
        for name in ("", "   ", "x" * 61):
            with self.subTest(name=name):
                self.assertEqual(self.create({"name": name}).status_code, 400)
        self.assertEqual(self.create({}).status_code, 400)
        self.assertEqual(self.create({"name": "x" * 60}).status_code, 201)

    def test_a_name_in_use_is_refused_whatever_the_case(self):
        for name in ("Sick", "sick", " SICK "):
            with self.subTest(name=name):
                response = self.create({"name": name})
                self.assertEqual(response.status_code, 409)
                self.assertEqual(self.code(response), "DUPLICATE_REASON_CODE")
        self.assertEqual(len(self.list()), 7)

    def test_a_retired_reasons_name_is_still_taken(self):
        self.verb(self.reason("Travel").pk, "retire")
        self.assertEqual(self.create({"name": "travel"}).status_code, 409)

    def test_another_schools_name_does_not_clash(self):
        solo = {r["name"] for r in self.list(self.solo_configurer)}
        self.assertIn("Sick", solo)
        self.assertEqual(
            self.create({"name": "Sick"}, self.solo_configurer).status_code, 409,
        )
        self.assertEqual(
            self.create({"name": "Bereavement"}, self.solo_configurer).status_code, 201,
        )
        self.assertNotIn("Bereavement", self.by_name())

    def test_adding_is_audited(self):
        self.create({"name": "Bereavement"})
        event = AuditEvent.objects.get(entity_type="ReasonCode", action_type="CREATE")
        self.assertEqual(event.module_key, "ATTENDANCE")
        self.assertIn("Bereavement", event.summary)


class ChangeTests(_Reasons):
    def test_a_reason_can_be_renamed_and_its_flags_changed(self):
        sick = self.reason()
        response = self.change(
            sick.pk, {"name": "Illness", "requires_note": True, "counts_as_authorised": False},
        )
        self.assertEqual(response.status_code, 200, response.data)
        row = self.data(response)
        self.assertEqual(
            (row["name"], row["requires_note"], row["counts_as_authorised"]),
            ("Illness", True, False),
        )
        self.assertEqual(self.by_name()["Illness"]["id"], sick.pk)

    def test_changing_only_a_flag_leaves_the_name(self):
        self.change(self.reason().pk, {"requires_note": True})
        self.assertEqual(self.reason().requires_note, True)

    def test_renaming_to_another_reasons_name_is_refused(self):
        response = self.change(self.reason().pk, {"name": "travel"})
        self.assertEqual(response.status_code, 409)
        self.assertEqual(self.code(response), "DUPLICATE_REASON_CODE")
        self.assertEqual(self.reason().name, "Sick")

    def test_a_reason_may_be_renamed_to_its_own_name_in_another_case(self):
        self.assertEqual(self.change(self.reason().pk, {"name": "SICK"}).status_code, 200)
        self.assertEqual(self.reason("SICK").name, "SICK")

    def test_an_empty_change_is_accepted_and_changes_nothing(self):
        self.assertEqual(self.change(self.reason().pk, {}).status_code, 200)
        self.assertFalse(AuditEvent.objects.filter(entity_type="ReasonCode").exists())

    def test_a_change_is_audited_with_what_changed(self):
        self.change(self.reason().pk, {"name": "Illness"})
        event = AuditEvent.objects.get(entity_type="ReasonCode")
        self.assertIn("Sick", event.summary)
        self.assertIn("Illness", event.summary)


class RetireTests(_Reasons):
    def test_a_retired_reason_is_refused_on_a_new_mark_and_kept_on_an_old_one(self):
        sick = self.reason()
        self.assertEqual(
            self.mark(
                self.eze, self.jss1a, self.kelechi, "E", reason_code=sick.pk,
            ).status_code, 200,
        )
        self.assertEqual(self.verb(sick.pk, "retire").status_code, 200)
        refused = self.mark(self.eze, self.jss1a, self.tunde, "E", reason_code=sick.pk)
        self.assertEqual(refused.status_code, 422)
        self.assertEqual(self.code(refused), "REASON_CODE_RETIRED")
        row = self.rows(self.read(self.eze, self.jss1a))[self.kelechi.pk]
        self.assertEqual((row["mark"], row["reason_label"]), ("E", "Sick"))
        self.assertEqual(self.by_name()["Sick"]["used_this_term"], 1)

    def test_a_restored_reason_is_accepted_again(self):
        sick = self.reason()
        self.verb(sick.pk, "retire")
        response = self.verb(sick.pk, "restore")
        self.assertEqual(response.status_code, 200)
        self.assertFalse(self.data(response)["retired"])
        self.assertEqual(
            self.mark(self.eze, self.jss1a, self.tunde, "E", reason_code=sick.pk).status_code,
            200,
        )

    def test_retiring_twice_is_harmless_and_audited_once(self):
        sick = self.reason()
        for _ in range(2):
            response = self.verb(sick.pk, "retire")
            self.assertEqual(response.status_code, 200)
            self.assertTrue(self.data(response)["retired"])
        self.assertEqual(AuditEvent.objects.filter(entity_type="ReasonCode").count(), 1)
        self.assertEqual(self.verb(sick.pk, "restore").status_code, 200)
        self.assertEqual(self.verb(sick.pk, "restore").status_code, 200)
        self.assertEqual(AuditEvent.objects.filter(entity_type="ReasonCode").count(), 2)

    def test_the_last_reason_in_use_cannot_be_retired(self):
        names = [row["name"] for row in self.list()]
        for name in names[:-1]:
            self.assertEqual(self.verb(self.reason(name).pk, "retire").status_code, 200)
        last = self.reason(names[-1])
        response = self.verb(last.pk, "retire")
        self.assertEqual(response.status_code, 422)
        self.assertEqual(self.code(response), "LAST_LIVE_REASON_CODE")
        self.assertEqual(
            response.data["message"],
            "Keep at least one reason code in use, or no absence can be excused. "
            "Add another code before retiring this one.",
        )
        self.assertIsNone(self.reason(names[-1]).retired_at)
        self.assertEqual(
            AuditEvent.objects.filter(entity_type="ReasonCode").count(), len(names) - 1,
        )

    def test_adding_another_reason_frees_the_last_one_to_be_retired(self):
        for row in self.list():
            if row["name"] != "Other":
                self.verb(row["id"], "retire")
        self.assertEqual(self.verb(self.reason("Other").pk, "retire").status_code, 422)
        self.assertEqual(self.create({"name": "Bereavement"}).status_code, 201)
        self.assertEqual(self.verb(self.reason("Other").pk, "retire").status_code, 200)

    def test_retiring_a_reason_already_retired_is_not_refused_as_the_last(self):
        names = [row["name"] for row in self.list()]
        for name in names[:-1]:
            self.verb(self.reason(name).pk, "retire")
        self.assertEqual(self.verb(self.reason(names[0]).pk, "retire").status_code, 200)

    def test_another_schools_reasons_do_not_count_as_live(self):
        for row in self.list(self.solo_configurer)[:-1]:
            self.verb(row["id"], "retire", self.solo_configurer)
        # Brightfield still has all seven, so its own retire is unaffected.
        self.assertEqual(self.verb(self.reason().pk, "retire").status_code, 200)

    def test_a_reason_is_never_deleted(self):
        response = TenantAPIClient(self.configurer).delete(
            reverse("attendance-reason-code", kwargs={"reason_id": self.reason().pk}),
        )
        self.assertEqual(response.status_code, 405)
        self.assertEqual(len(self.list()), 7)


class SecurityTests(_Reasons):
    def test_every_write_needs_the_settings_key(self):
        sick = self.reason().pk
        for user in (self.eze, self.admin, self.nobody):
            with self.subTest(user=user.email):
                self.assertEqual(self.create({"name": "Bereavement"}, user).status_code, 403)
                self.assertEqual(self.change(sick, {"name": "Illness"}, user).status_code, 403)
                self.assertEqual(self.verb(sick, "retire", user).status_code, 403)
                self.assertEqual(self.verb(sick, "restore", user).status_code, 403)
        self.assertEqual(self.reason().name, "Sick")
        self.assertEqual(len(self.list()), 7)

    def test_a_branch_administrator_holding_the_key_cannot_change_the_schools_list(self):
        sick = self.reason().pk
        for response in (
            self.create({"name": "Bereavement"}, self.branch_configurer),
            self.change(sick, {"name": "Illness"}, self.branch_configurer),
            self.verb(sick, "retire", self.branch_configurer),
        ):
            self.assertEqual(response.status_code, 403)
            self.assertEqual(self.code(response), "SHARED_RECORD_READ_ONLY")
        self.assertEqual(len(self.list(self.branch_configurer)), 7)

    def test_another_schools_reason_answers_404_on_every_verb(self):
        theirs = self.reason("Sick", self.solo).pk
        self.assertEqual(self.change(theirs, {"name": "Mine now"}).status_code, 404)
        self.assertEqual(self.verb(theirs, "retire").status_code, 404)
        self.assertEqual(self.verb(theirs, "restore").status_code, 404)
        self.assertEqual(self.reason("Sick", self.solo).name, "Sick")
        self.assertIsNone(self.reason("Sick", self.solo).retired_at)

    def test_a_reason_that_does_not_exist_is_a_404(self):
        self.assertEqual(self.change(999999, {"name": "x"}).status_code, 404)

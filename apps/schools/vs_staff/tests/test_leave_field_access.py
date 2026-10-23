"""A leave request under the ``school.leave`` Field Access switches.

At Bright Star School Mrs Adeyemi files a sick day with the note "hospital
appointment". Her bursar, Mr Eze, opens the leave page: the school has turned
the Note switch off for the Bursar role, so he reads her dates and the days she
is away and nothing of why. Brightfield (two branches) and Sunrise (one) each
have a caller per switch, so every path that carries a leave fact is read as
somebody who may not see it: the list, the echo of a write, the page beside it
that carries the balance, the preview, the as-at read, the section timeline, the
warnings a write answers with and the audit trail.

The switches are six: the type, the dates, the days, the note, the status and
the balance. The status and the balance are read-only, so nobody can write them
through this resource, and the type and the dates are open on create so a role
that may file a request can name its kind and its days.
"""
from __future__ import annotations

import datetime as dt

from schools.vs_staff.constants import LeaveStatus
from schools.vs_staff.field_access import (
    LEAVE_BALANCE_NAMES,
    LEAVE_DATE_NAMES,
    LEAVE_STATUS_NAMES,
    LEAVE_TYPE_NAMES,
)
from schools.vs_staff.models import LeaveRequest
from vs_rbac.tests.deep_payload import walk
from vs_rbac.tests.helpers import (
    install_declared_fields,
    make_assignment,
    make_role,
    make_role_permission,
    make_school_admin,
    set_field_access,
)

from .base import ALL_KEYS
from .test_staff_settings import _LeaveSettingsFixture

FIELDS = {
    "leave_type": "school.leave.leave_type",
    "dates": "school.leave.dates",
    "days": "school.leave.days",
    "note": "school.leave.note",
    "status": "school.leave.status",
    "balance": "school.leave.balance",
}
NAMES = {
    "leave_type": set(LEAVE_TYPE_NAMES),
    "dates": set(LEAVE_DATE_NAMES),
    "days": {"days"},
    "note": {"note"},
    "status": set(LEAVE_STATUS_NAMES),
    "balance": set(LEAVE_BALANCE_NAMES),
}
NOTE = "hospital appointment"


def keys_of(payload) -> set:
    return {key for _, key, _ in walk(payload)}


class _LeaveAccessFixture(_LeaveSettingsFixture):
    """Callers who each lose one switch, at both shapes of school."""

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        install_declared_fields("school.leave")
        cls.blind = {}
        cls.read_only = {}
        for label, school in (("multi", cls.school), ("single", cls.solo)):
            for name, key in FIELDS.items():
                cls.blind[label, name] = cls.caller(school, f"blind_{name}_{label}", key, read=False)
                cls.read_only[label, name] = cls.caller(
                    school, f"readonly_{name}_{label}", key, read=True,
                )
        cls.sick = cls.leave_row(
            cls.eze, "SICK", dt.date(2026, 3, 2), dt.date(2026, 3, 6), 5,
            LeaveStatus.APPROVED, over=2,
        )
        cls.annual = cls.leave_row(
            cls.eze, "ANNUAL", dt.date(2026, 4, 6), dt.date(2026, 4, 10), 5,
            LeaveStatus.PENDING,
        )
        cls.solo_sick = cls.leave_row(
            cls.solo_staff, "SICK", dt.date(2026, 3, 2), dt.date(2026, 3, 6), 5,
            LeaveStatus.APPROVED, over=2,
        )
        cls.person = {"multi": cls.eze, "single": cls.solo_staff}
        cls.admins = {"multi": cls.admin, "single": cls.solo_admin}

    @classmethod
    def caller(cls, school, name, key, *, read):
        role = make_role(school, name=name, key=name)
        for perm in ALL_KEYS:
            make_role_permission(role, cls.permissions[perm])
        set_field_access(role, key, read=read, write=False)
        user = make_school_admin(None, email=f"{name}@leave.test", tenant=school.tenant)
        make_assignment(school, user, role, branch=None)
        return user

    @classmethod
    def leave_row(cls, staff, kind, start, end, days, status, *, over=0):
        return LeaveRequest.all_objects.create(
            tenant=staff.tenant, staff=staff, leave_type=kind, start_date=start,
            end_date=end, days=days, resumption_date=end + dt.timedelta(days=3),
            over_allowance_by=over, status=status, note=NOTE,
        )

    def page(self, user, person, **params):
        response = self.get(user, "staff-leave", params, pk=person.pk)
        self.assertEqual(response.status_code, 200, response.data)
        return response.data["data"]


class DefaultAccessTests(_LeaveAccessFixture):
    def test_a_role_with_no_restriction_reads_every_leave_fact(self):
        for label, person in self.person.items():
            with self.subTest(school=label):
                page = self.page(self.admins[label], person)
                row = next(r for r in page["leave"] if r["leave_type"] == "SICK")
                self.assertEqual(row["note"], NOTE)
                self.assertEqual(row["days"], 5)
                self.assertEqual(row["over_allowance_by"], 2)
                self.assertEqual(row["status"], "APPROVED")
                self.assertEqual(row["start_date"], "2026-03-02")
                self.assertTrue({
                    "days_taken", "balances", "balance_session", "balance_note",
                } <= set(page))

    def test_the_echo_of_a_write_names_what_the_caller_may_not_change(self):
        response = self.post(self.admin, "staff-leave", self.body(), pk=self.eze.pk)
        self.assertEqual(response.status_code, 201, response.data)
        leave = response.data["data"]["leave"]
        self.assertEqual(leave["note"], "")
        self.assertTrue({"status", "display_status", "decided_at"} <= set(leave["_read_only_fields"]))
        self.assertNotIn("note", leave["_read_only_fields"])

    def test_the_preview_carries_every_figure(self):
        for label, person in self.person.items():
            with self.subTest(school=label):
                response = self.get(
                    self.admins[label], "staff-leave-preview",
                    {"leave_type": "SICK", "start_date": "2026-03-09", "end_date": "2026-03-10"},
                    pk=person.pk,
                )
                self.assertEqual(response.status_code, 200, response.data)
                self.assertTrue({
                    "days", "resumption_date", "leave_type", "allowance", "taken",
                    "pending", "remaining", "remaining_after", "over_allowance_by",
                } <= set(response.data["data"]))

    def test_another_schools_switches_do_not_reach_this_one(self):
        """Brightfield's note-blind bursar does not hide Sunrise's notes from Sunrise's admin."""
        hidden = self.page(self.blind["multi", "note"], self.eze)
        self.assertNotIn("note", keys_of(hidden["leave"]))
        shown = self.page(self.solo_admin, self.solo_staff)
        self.assertEqual(shown["leave"][0]["note"], NOTE)


class ReadPathTests(_LeaveAccessFixture):
    def test_each_hidden_switch_removes_its_names_from_the_list_and_only_those(self):
        for label, person in self.person.items():
            full = keys_of(self.page(self.admins[label], person)["leave"])
            for switch in ("leave_type", "dates", "days", "note", "status"):
                with self.subTest(school=label, switch=switch):
                    page = self.page(self.blind[label, switch], person)
                    shown = keys_of(page["leave"])
                    self.assertEqual(shown & NAMES[switch], set())
                    self.assertEqual(shown, full - NAMES[switch])

    def test_a_hidden_note_is_nowhere_in_the_page(self):
        for label, person in self.person.items():
            with self.subTest(school=label):
                response = self.get(self.blind[label, "note"], "staff-leave", pk=person.pk)
                self.assertEqual(response.status_code, 200, response.data)
                self.assertNotIn(NOTE, str(response.data))
                rows = response.data["data"]["leave"]
                self.assertTrue(rows and all("start_date" in r and "days" in r for r in rows))

    def test_a_hidden_balance_removes_every_figure_and_leaves_the_days(self):
        for label, person in self.person.items():
            with self.subTest(school=label):
                page = self.page(self.blind[label, "balance"], person)
                self.assertEqual(keys_of(page) & NAMES["balance"], set())
                self.assertTrue(all("days" in row for row in page["leave"]))
                self.assertTrue(all(row["leave_type"] for row in page["leave"]))

    def test_a_hidden_type_removes_the_breakdowns_by_type_even_with_the_balance_open(self):
        """Without it a role would read how many sick days were taken."""
        for label, person in self.person.items():
            with self.subTest(school=label):
                page = self.page(self.blind[label, "leave_type"], person)
                self.assertNotIn("days_taken", page)
                self.assertNotIn("balances", page)
                self.assertEqual(keys_of(page["leave"]) & NAMES["leave_type"], set())
                self.assertNotIn("SICK", str(page))

    def test_the_as_at_read_is_held_to_the_same_switches(self):
        today = dt.date.today().isoformat()
        for label, person in self.person.items():
            with self.subTest(school=label):
                admin = self.page(self.admins[label], person, as_at=today)
                self.assertTrue(admin["leave"])
                note_blind = self.page(self.blind[label, "note"], person, as_at=today)
                self.assertNotIn("note", keys_of(note_blind["leave"]))
                balance_blind = self.page(self.blind[label, "balance"], person, as_at=today)
                self.assertEqual(keys_of(balance_blind) & NAMES["balance"], set())

    def test_the_preview_withholds_figures_by_switch(self):
        query = {"leave_type": "SICK", "start_date": "2026-03-09", "end_date": "2026-03-10"}
        for label, person in self.person.items():
            def preview(switch):
                response = self.get(
                    self.blind[label, switch], "staff-leave-preview", query, pk=person.pk,
                )
                self.assertEqual(response.status_code, 200, response.data)
                return response.data["data"]

            with self.subTest(school=label):
                balance = preview("balance")
                self.assertEqual(set(balance) & NAMES["balance"], set())
                self.assertEqual(balance["days"], 2)
                self.assertIn("resumption_date", balance)
                dates = preview("dates")
                self.assertNotIn("resumption_date", dates)
                self.assertIn("allowance", dates)
                self.assertNotIn("days", preview("days"))
                kind = preview("leave_type")
                self.assertNotIn("leave_type", kind)
                self.assertEqual(set(kind) & NAMES["balance"], set())
                self.assertEqual(kind["days"], 2)

    def test_the_section_timeline_follows_the_type_and_dates_switches(self):
        def entries(user, person):
            response = self.get(
                user, "staff-section-history", {"section": "leave"}, pk=person.pk,
            )
            self.assertEqual(response.status_code, 200, response.data)
            return response.data["data"]["entries"]

        for label, person in self.person.items():
            with self.subTest(school=label):
                full = entries(self.admins[label], person)
                self.assertTrue(full)
                self.assertIn("Sick", {row["title"] for row in full})
                typeless = entries(self.blind[label, "leave_type"], person)
                self.assertNotIn("Leave type", str(typeless))
                self.assertEqual({row["title"] for row in typeless}, {"Leave"})
                dateless = entries(self.blind[label, "dates"], person)
                for field in ("Start date", "End date", "Resumption date"):
                    self.assertNotIn(field, str(dateless))
                self.assertIn("Leave type", str(dateless))
                self.assertNotIn("Days", str(entries(self.blind[label, "days"], person)))
                self.assertNotIn("Status", str(entries(self.blind[label, "status"], person)))

    def test_the_audit_trail_hides_the_values_each_switch_owns(self):
        from schools.vs_staff.services.audit import emit_leave_decided
        from vs_audit.models import AuditActionType, AuditEvent
        from vs_audit.protected_words import visible_metadata, visible_summary

        response = self.post(self.admin, "staff-leave", self.body(), pk=self.eze.pk)
        self.assertEqual(response.status_code, 201, response.data)
        leave_id = response.data["data"]["leave"]["id"]
        filed = AuditEvent.objects.get(
            entity_type="LeaveRequest", entity_id=str(leave_id),
            action_type=AuditActionType.STAFF_LEAVE_RECORDED,
        )

        def read_all(key):
            return True

        def without(switch):
            return lambda key: key != FIELDS[switch]

        self.assertEqual(visible_summary(filed, read_all), filed.summary)
        self.assertIn("Annual", filed.summary)
        self.assertEqual(visible_summary(filed, without("leave_type")), "Leave filed")
        metadata = visible_metadata(filed, without("leave_type"))
        self.assertNotIn("leave_type", metadata)
        self.assertIn("start_date", metadata)
        self.assertEqual(
            {"start_date", "end_date"} & set(visible_metadata(filed, without("dates"))), set(),
        )
        self.assertNotIn("days", visible_metadata(filed, without("days")))
        self.assertNotIn("status", visible_metadata(filed, without("status")))

        emit_leave_decided(self.sick, actor=self.admin)
        decided = AuditEvent.objects.get(
            entity_type="LeaveRequest", entity_id=str(self.sick.pk),
            action_type=AuditActionType.STAFF_LEAVE_DECIDED,
        )
        self.assertEqual(decided.summary, "Leave approved.")
        self.assertEqual(visible_summary(decided, without("status")), "Leave")
        self.assertNotIn("status", visible_metadata(decided, without("status")))
        self.assertNotIn("leave_type", visible_metadata(decided, without("leave_type")))
        self.assertEqual(
            {"start_date", "end_date"} & set(visible_metadata(decided, without("dates"))), set(),
        )


class WarningTests(_LeaveAccessFixture):
    """The sentences a write answers with say no more than the caller may read."""

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.warnings_body = cls.body_for(start_date="2026-04-06", end_date="2026-04-17")

    @classmethod
    def body_for(cls, **overrides):
        return {"leave_type": "ANNUAL", "start_date": "2026-04-06", "end_date": "2026-04-17", **overrides}

    def setUp(self):
        super().setUp()
        self.save_rules(leave={"allowances": {"ANNUAL": 10}})

    def warnings(self, user):
        response = self.post(user, "staff-leave", self.warnings_body, pk=self.eze.pk)
        self.assertEqual(response.status_code, 201, response.data)
        return {w["code"]: w for w in response.data["data"]["warnings"]}

    def test_the_caller_with_everything_open_reads_the_whole_sentences(self):
        found = self.warnings(self.admin)
        self.assertIn("annual leave", found["OVER_ALLOWANCE"]["message"])
        self.assertIn("Annual", found["LEAVE_OVERLAP"]["message"])
        self.assertIn("2026", found["LEAVE_OVERLAP"]["message"])

    def test_a_hidden_balance_removes_the_over_allowance_warning(self):
        found = self.warnings(self.blind["multi", "balance"])
        self.assertNotIn("OVER_ALLOWANCE", found)
        self.assertIn("LEAVE_OVERLAP", found)

    def test_a_hidden_type_is_not_named_in_either_sentence(self):
        found = self.warnings(self.blind["multi", "leave_type"])
        self.assertNotIn("annual", found["OVER_ALLOWANCE"]["message"].lower())
        self.assertNotIn("annual", found["LEAVE_OVERLAP"]["message"].lower())
        self.assertIn("2026", found["LEAVE_OVERLAP"]["message"])

    def test_hidden_dates_are_not_named_in_the_overlap(self):
        found = self.warnings(self.blind["multi", "dates"])
        self.assertIn("Annual", found["LEAVE_OVERLAP"]["message"])
        self.assertNotIn("2026", found["LEAVE_OVERLAP"]["message"])

    def test_neither_type_nor_dates_leaves_only_the_fact_of_a_clash(self):
        role = make_role(self.school, name="no_type_no_dates", key="no_type_no_dates")
        for perm in ALL_KEYS:
            make_role_permission(role, self.permissions[perm])
        set_field_access(role, FIELDS["leave_type"], FIELDS["dates"], read=False, write=False)
        user = make_school_admin(None, email="nothing@leave.test", tenant=self.tenant)
        make_assignment(self.school, user, role, branch=None)
        found = self.warnings(user)
        self.assertEqual(
            found["LEAVE_OVERLAP"]["message"],
            "This overlaps leave already recorded for this person",
        )


class WritePathTests(_LeaveAccessFixture):
    def filed(self, **overrides):
        return self.post(self.admin, "staff-leave", self.body(**overrides), pk=self.eze.pk)

    def count(self):
        return LeaveRequest.all_objects.filter(staff=self.eze).count()

    def assert_refused(self, response, *names):
        self.assertEqual(response.status_code, 403, response.data)
        self.assertEqual(response.data["error"]["code"], "field_write_denied")
        self.assertEqual(sorted(response.data["error"]["detail"]), sorted(names))

    def test_a_note_cannot_be_sent_by_a_role_that_may_not_write_it(self):
        for label in ("multi", "single"):
            person = self.person[label]
            before = LeaveRequest.all_objects.filter(staff=person).count()
            with self.subTest(school=label):
                response = self.post(
                    self.blind[label, "note"], "staff-leave",
                    self.body(note="Please keep private."), pk=person.pk,
                )
                self.assert_refused(response, "note")
                readable = self.post(
                    self.read_only[label, "note"], "staff-leave",
                    self.body(note="Please keep private."), pk=person.pk,
                )
                self.assert_refused(readable, "note")
                self.assertEqual(LeaveRequest.all_objects.filter(staff=person).count(), before)

    def test_an_empty_note_from_a_form_that_posts_every_input_is_not_a_write(self):
        response = self.post(
            self.read_only["multi", "note"], "staff-leave", self.body(note=""), pk=self.eze.pk,
        )
        self.assertEqual(response.status_code, 201, response.data)

    def test_days_cannot_be_sent_by_a_role_that_may_not_write_them(self):
        before = self.count()
        response = self.post(
            self.read_only["multi", "days"], "staff-leave", self.body(days=3), pk=self.eze.pk,
        )
        self.assert_refused(response, "days")
        self.assertEqual(self.count(), before)

    def test_the_type_and_the_dates_are_open_on_create_and_closed_to_correction(self):
        for switch in ("leave_type", "dates"):
            with self.subTest(switch=switch):
                response = self.post(
                    self.read_only["multi", switch], "staff-leave", self.body(), pk=self.eze.pk,
                )
                self.assertEqual(response.status_code, 201, response.data)
                row = response.data["data"]["leave"]
                patch = self.patch(
                    self.read_only["multi", switch], "staff-leave-detail",
                    {"leave_type": "SICK"} if switch == "leave_type" else {"end_date": "2026-01-05"},
                    pk=row["id"],
                )
                self.assert_refused(patch, "leave_type" if switch == "leave_type" else "end_date")

    def test_a_status_is_never_accepted_from_anyone(self):
        before = self.count()
        for body in ({"status": "APPROVED"}, {"display_status": "Approved"}, {"decided_at": "2026-03-01"}):
            with self.subTest(body=body):
                response = self.filed(**body)
                self.assert_refused(response, *body)
        self.assertEqual(self.count(), before)
        row = self.annual
        for body in ({"status": "APPROVED"}, {"approval": {}}):
            with self.subTest(patch=body):
                response = self.patch(self.admin, "staff-leave-detail", body, pk=row.pk)
                self.assert_refused(response, *body)
        row.refresh_from_db()
        self.assertEqual(row.status, LeaveStatus.PENDING)

    def test_a_balance_figure_is_never_accepted_from_anyone(self):
        for body in ({"over_allowance_by": 0}, {"balances": []}, {"remaining": 9}):
            with self.subTest(body=body):
                self.assert_refused(self.filed(**body), *body)
        self.assert_refused(
            self.patch(self.admin, "staff-leave-detail", {"over_allowance_by": 0}, pk=self.annual.pk),
            "over_allowance_by",
        )

    def test_a_form_that_sends_the_whole_request_back_is_not_refused_for_what_is_unchanged(self):
        """A role that reads the note and cannot change it sends it back unchanged without a refusal."""
        user = self.read_only["multi", "note"]
        row = self.get(user, "staff-leave", pk=self.eze.pk).data["data"]["leave"]
        annual = next(r for r in row if r["id"] == self.annual.pk)
        response = self.patch(
            user, "staff-leave-detail", {"note": annual["note"], "days": annual["days"]},
            pk=self.annual.pk,
        )
        self.assertEqual(response.status_code, 200, response.data)
        changed = self.patch(user, "staff-leave-detail", {"note": "A different note."}, pk=self.annual.pk)
        self.assert_refused(changed, "note")

    def test_a_correction_of_a_field_the_caller_may_write_goes_through(self):
        response = self.patch(
            self.read_only["multi", "note"], "staff-leave-detail", {"days": 4}, pk=self.annual.pk,
        )
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["data"]["leave"]["days"], 4)

    def test_the_echo_of_a_write_hides_what_the_caller_may_not_read(self):
        response = self.post(
            self.blind["multi", "note"], "staff-leave", self.body(), pk=self.eze.pk,
        )
        self.assertEqual(response.status_code, 201, response.data)
        self.assertNotIn("note", response.data["data"]["leave"])
        self.assertNotIn("note", response.data["data"]["leave"]["_read_only_fields"])
        cancelled = self.delete(
            self.blind["multi", "dates"], "staff-leave-detail", pk=self.annual.pk,
        )
        self.assertEqual(cancelled.status_code, 200, cancelled.data)
        self.assertEqual(keys_of(cancelled.data["data"]) & NAMES["dates"], set())
        self.assertEqual(cancelled.data["data"]["status"], "CANCELLED")

    def test_a_role_without_the_leave_key_is_refused_before_any_switch_is_asked(self):
        response = self.post(self.nobody, "staff-leave", self.body(), pk=self.eze.pk)
        self.assertEqual(response.status_code, 403, response.data)
        self.assertNotEqual(response.data["error"]["code"], "field_write_denied")


class OwnerRuleTests(_LeaveAccessFixture):
    """A person reads and supplies their own leave whatever their role's switches say.

    Mr Eze is a teacher at Lekki and the school has closed every leave switch for
    the Teacher role. He still reads his own sick leave in full, note, balance and
    all, and files and annotates his own requests. A Lekki colleague with the same
    role who has been given the leave view key reads none of it, and neither does
    an administrator who filed a request on his behalf: owning is being the
    person the leave belongs to, not having filed it.
    """

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        from vs_rbac.models import PermissionScope
        from vs_rbac.tests.helpers import make_permission

        install_declared_fields("school.leave")
        set_field_access(cls.teacher_role, *FIELDS.values(), read=False, write=False)
        make_role_permission(cls.teacher_role, cls.permissions["school.leave.view"])
        make_role_permission(
            cls.teacher_role,
            make_permission("workflow.instance.view", scope=PermissionScope.TENANT),
        )
        cls.peer = cls.make_staff(
            "peer@brightfield.test", "Kemi", "Peer", branch=cls.lekki,
            role=cls.teacher_role,
        ).user

    def setUp(self):
        super().setUp()
        self.save_rules(leave={"allowances": {"ANNUAL": 10}})

    def test_the_owner_reads_their_own_note_and_balance_with_every_switch_closed(self):
        page = self.page(self.eze.user, self.eze)
        row = next(r for r in page["leave"] if r["leave_type"] == "SICK")
        self.assertEqual(row["note"], NOTE)
        self.assertEqual(row["over_allowance_by"], 2)
        self.assertEqual(row["status"], "APPROVED")
        self.assertTrue({"days_taken", "balances", "balance_session", "balance_note"} <= set(page))

    def test_a_colleague_with_the_same_role_reads_none_of_it(self):
        page = self.page(self.peer, self.eze)
        self.assertEqual(keys_of(page) & (set().union(*NAMES.values())), set())
        self.assertNotIn(NOTE, str(page))

    def test_the_owners_record_names_only_what_nobody_can_write(self):
        echo = self.post(self.eze.user, "staff-leave", self.body(), pk=self.eze.pk)
        self.assertEqual(echo.status_code, 201, echo.data)
        self.assertNotIn("note", echo.data["data"]["leave"]["_read_only_fields"])
        fixed = set(NAMES["status"]) | set(NAMES["balance"])
        self.assertTrue(set(echo.data["data"]["leave"]["_read_only_fields"]) <= fixed)
        self.assertIn("status", echo.data["data"]["leave"]["_read_only_fields"])

    def test_an_administrator_who_filed_for_the_person_is_not_the_owner(self):
        filer = self.blind["multi", "note"]
        response = self.post(filer, "staff-leave", self.body(), pk=self.eze.pk)
        self.assertEqual(response.status_code, 201, response.data)
        row_id = response.data["data"]["leave"]["id"]
        LeaveRequest.all_objects.filter(pk=row_id).update(note=NOTE)
        rows = self.page(filer, self.eze)["leave"]
        filed = next(r for r in rows if r["id"] == row_id)
        self.assertNotIn("note", filed)
        self.assertEqual(filed["requested_by"]["id"], filer.pk)

    def test_the_owner_files_their_own_leave_with_a_note_whatever_the_write_switches_say(self):
        response = self.post(
            self.eze.user, "staff-leave",
            self.body(note="Please keep private.", days=3), pk=self.eze.pk,
        )
        self.assertEqual(response.status_code, 201, response.data)
        leave = response.data["data"]["leave"]
        self.assertEqual(leave["note"], "Please keep private.")
        self.assertEqual(leave["days"], 3)

    def test_the_owner_is_still_refused_a_status_or_a_balance_figure(self):
        for body in ({"status": "APPROVED"}, {"over_allowance_by": 0}, {"balances": []}):
            with self.subTest(body=body):
                response = self.post(
                    self.eze.user, "staff-leave", self.body(**body), pk=self.eze.pk,
                )
                self.assertEqual(response.status_code, 403, response.data)
                self.assertEqual(response.data["error"]["code"], "field_write_denied")

    def test_someone_with_the_same_role_filing_for_another_is_not_excused_a_note(self):
        """The peer's role may update leave only with the key, and a note still needs the switch."""
        make_role_permission(self.teacher_role, self.permissions["school.leave.update"])
        make_role_permission(self.teacher_role, self.permissions["school.teachers.view"])
        response = self.post(
            self.peer, "staff-leave", self.body(note="Not mine to add."), pk=self.eze.pk,
        )
        self.assertEqual(response.status_code, 403, response.data)
        self.assertEqual(response.data["error"]["code"], "field_write_denied")

    def test_the_owner_reads_the_whole_preview_and_the_warnings(self):
        query = {"leave_type": "SICK", "start_date": "2026-03-09", "end_date": "2026-03-10"}
        preview = self.get(self.eze.user, "staff-leave-preview", query, pk=self.eze.pk)
        self.assertEqual(preview.status_code, 200, preview.data)
        self.assertTrue({"leave_type", "allowance", "taken", "remaining_after"} <= set(preview.data["data"]))
        response = self.post(
            self.eze.user, "staff-leave",
            self.body(start_date="2026-04-06", end_date="2026-04-17"), pk=self.eze.pk,
        )
        self.assertEqual(response.status_code, 201, response.data)
        found = {w["code"]: w for w in response.data["data"]["warnings"]}
        self.assertIn("annual leave", found["OVER_ALLOWANCE"]["message"])
        self.assertIn("2026", found["LEAVE_OVERLAP"]["message"])

    def test_the_owner_reads_their_whole_leave_timeline(self):
        response = self.get(
            self.eze.user, "staff-section-history", {"section": "leave"}, pk=self.eze.pk,
        )
        self.assertEqual(response.status_code, 200, response.data)
        entries = response.data["data"]["entries"]
        self.assertIn("Sick", {row["title"] for row in entries})
        self.assertIn("Start date", str(entries))

    def test_the_approval_card_shows_the_owner_all_of_their_own_request(self):
        from vs_workflow.models import WorkflowInstance

        response = self.post(
            self.admin, "staff-leave", self.body(note="Family wedding."), pk=self.eze.pk,
        )
        self.assertEqual(response.status_code, 201, response.data)
        instance = WorkflowInstance.all_objects.get(
            document_type="schools.leave_request",
            document_object_id=str(response.data["data"]["leave"]["id"]),
        )
        self.assertEqual(instance.request_for_id, self.eze.user_id)

        from core.test_utils import TenantAPIClient

        def card(user):
            result = TenantAPIClient(user=user).get(f"/v1/workflow/instances/{instance.pk}/")
            self.assertEqual(result.status_code, 200, result.data)
            return str(result.data)

        self.assertIn("Family wedding.", card(self.eze.user))
        self.assertIn("Annual leave", card(self.eze.user))
        self.assertNotIn("Family wedding.", card(self.peer))
        self.assertNotIn("Annual leave", card(self.peer))


class DirectoryUntilTests(_LeaveAccessFixture):
    """The day somebody's leave ends follows the dates switch, and the owner's own row keeps it."""

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        today = dt.date.today()
        for staff in (cls.eze, cls.solo_staff):
            LeaveRequest.all_objects.create(
                tenant=staff.tenant, staff=staff, leave_type="ANNUAL",
                start_date=today - dt.timedelta(days=3), end_date=today + dt.timedelta(days=3),
                days=5, status=LeaveStatus.APPROVED,
            )
        set_field_access(cls.teacher_role, FIELDS["dates"], read=False, write=False)

    def row(self, user, person):
        response = self.get(user, "staff-list")
        self.assertEqual(response.status_code, 200, response.data)
        return next(r for r in response.data["data"] if r["id"] == person.pk)

    def test_with_the_switch_open_the_row_says_until_when(self):
        for label, person in self.person.items():
            with self.subTest(school=label):
                row = self.row(self.admins[label], person)
                self.assertTrue(row["on_leave_today"])
                self.assertIsNotNone(row["on_leave_until"])

    def test_with_the_dates_hidden_the_row_still_says_they_are_away_and_not_until_when(self):
        for label, person in self.person.items():
            with self.subTest(school=label):
                row = self.row(self.blind[label, "dates"], person)
                self.assertTrue(row["on_leave_today"])
                self.assertEqual(row["display_employment_status"], "ON_LEAVE")
                self.assertNotIn("on_leave_until", row)
                detail = self.get(self.blind[label, "dates"], "staff-detail", pk=person.pk)
                self.assertEqual(detail.status_code, 200, detail.data)
                self.assertTrue(detail.data["data"]["on_leave_today"])
                self.assertNotIn("on_leave_until", detail.data["data"])

    def test_the_owner_reads_their_own_end_date_with_the_switch_closed_for_their_role(self):
        detail = self.get(self.eze.user, "staff-detail", pk=self.eze.pk)
        self.assertEqual(detail.status_code, 200, detail.data)
        self.assertIsNotNone(detail.data["data"]["on_leave_until"])

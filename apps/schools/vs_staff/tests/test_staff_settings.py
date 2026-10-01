"""Settings, Staff: a school's own rules for its staff.

Brightfield runs Lekki and Ikeja. Adaeze is its school administrator and holds
every key. Tolu is its settings administrator: ``school.settings.update`` and
the register's view key, across the whole school. Kemi holds the same role
pinned to Lekki. Chukwuemeka Eze teaches at Lekki with staff ID BFS/STF/0012.

Each rule is checked four ways: a school that has saved nothing behaves as it
always has, a saved rule reads back as saved, a bad value is refused on its own
field in a sentence, and a caller whose reach is one branch cannot change a
rule that binds the whole school.
"""
from __future__ import annotations

import datetime as dt
from uuid import uuid4
from unittest import mock

from django.urls import reverse

from schools.vs_staff.constants import EmploymentStatus, LeaveStatus
from schools.vs_staff.models import LeaveRequest, StaffDocument, StaffProfile
from vs_rbac.models import PermissionScope, TenantUserRoleAssignment
from vs_rbac.tests.helpers import (
    make_assignment,
    make_permission,
    make_role,
    make_role_permission,
    make_school_admin,
)
from vs_user.models import User
from vs_workflow.models import WorkflowInstance

from .base import StaffFixture
from .test_imports import _ImportFixture
from .test_leave import LeaveFixture

REFUSED = "SHARED_RECORD_READ_ONLY"
DEFAULT_SELF = {"middle_name", "date_of_birth", "photo", "phone"}


class _SettingsMixin:
    """Tolu and Kemi, and the settings key on the school admin role."""

    @classmethod
    def add_settings_people(cls):
        settings_key = make_permission(
            "school.settings.update", scope=PermissionScope.TENANT,
        )
        make_role_permission(cls.role, settings_key)
        settings_role = make_role(cls.school, name="Settings Admin", key="settings_admin")
        make_role_permission(settings_role, settings_key)
        make_role_permission(settings_role, cls.permissions["school.teachers.view"])

        cls.tolu = make_school_admin(None, email="tolu@brightfield.test", tenant=cls.tenant)
        make_assignment(cls.school, cls.tolu, settings_role, branch=None)
        cls.kemi = make_school_admin(cls.lekki, email="kemi@lekki.test", tenant=cls.tenant)
        make_assignment(cls.school, cls.kemi, settings_role, branch=cls.lekki)

    def rules_body(self, **overrides):
        body = {
            "starting_role": "teacher",
            "required_documents": [],
            "self_editable_fields": sorted(DEFAULT_SELF),
            "hire_requires_approval": False,
            "leave": {
                "allowances": {}, "working_days": [1, 2, 3, 4, 5],
                "exclude_closures": True,
            },
        }
        leave = overrides.pop("leave", None)
        body.update(overrides)
        if leave:
            body["leave"] = {**body["leave"], **leave}
        return body

    def save_rules(self, user=None, **overrides):
        response = self.put(user or self.admin, "staff-rules", self.rules_body(**overrides))
        self.assertEqual(response.status_code, 200, response.data)
        return response

    def policy_path(self, branch=None):
        path = f"{reverse('staff-number-policy')}?tenant={self.tenant.slug}"
        return f"{path}&branch={branch.pk}" if branch is not None else path

    def save_policy(self, branch=None, user=None, **body):
        response = self.client_for(user or self.admin).put(
            self.policy_path(branch),
            {"required": False, "pattern": "", "hint": "", **body}, format="json",
        )
        self.assertEqual(response.status_code, 200, response.data)
        return response

    def add(self, **overrides):
        return self.post(self.admin, "staff-list", self.invite_body(**overrides))

    def assert_refused(self, response, message):
        self.assertEqual(response.status_code, 403, response.data)
        self.assertEqual(response.data["error"]["code"], REFUSED)
        self.assertEqual(response.data["message"], message)


class _SettingsFixture(_SettingsMixin, StaffFixture):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.add_settings_people()


# ── The school-wide rules ──────────────────────────────────────────────────


class StaffRulesDefaultTests(_SettingsFixture):
    def test_a_school_that_saved_nothing_reads_the_old_behaviour(self):
        data = self.get(self.tolu, "staff-rules").data["data"]

        self.assertEqual(data["starting_role"], "teacher")
        self.assertEqual(data["required_documents"], [])
        self.assertEqual(set(data["self_editable_fields"]), DEFAULT_SELF)
        self.assertFalse(data["hire_requires_approval"])
        self.assertTrue(all(v is None for v in data["leave"]["allowances"].values()))
        self.assertEqual(set(data["leave"]["allowances"]), {
            "ANNUAL", "SICK", "MATERNITY", "PATERNITY", "STUDY", "COMPASSIONATE", "OTHER",
        })
        self.assertEqual(data["leave"]["working_days"], [1, 2, 3, 4, 5])
        self.assertTrue(data["leave"]["exclude_closures"])

    def test_every_choice_the_screen_offers_travels_with_the_values(self):
        data = self.get(self.tolu, "staff-rules").data["data"]

        self.assertIn({"value": "teacher", "label": "Teacher"}, data["starting_role_options"])
        self.assertIn({"value": "CV", "label": "CV"}, data["document_types"])
        offered = {o["value"] for o in data["self_editable_options"]}
        locked = {o["value"] for o in data["self_editable_locked"]}
        self.assertTrue(DEFAULT_SELF <= offered)
        self.assertEqual(offered & locked, set())
        self.assertTrue({
            "staff_number", "job_title", "employment_type", "hire_date", "exit_date",
            "email", "branch",
        } <= locked)
        self.assertIn({"value": "ANNUAL", "label": "Annual"}, data["leave"]["leave_types"])

    def test_a_person_with_no_register_key_cannot_read_them(self):
        self.assertEqual(self.get(self.nobody, "staff-rules").status_code, 403)


class StaffRulesRoundTripTests(_SettingsFixture):
    def test_what_is_saved_reads_back(self):
        response = self.save_rules(
            self.tolu,
            required_documents=["IDENTIFICATION", "CV"],
            self_editable_fields=["phone", "gender"],
            leave={"allowances": {"ANNUAL": 20, "SICK": None}, "working_days": [6, 1, 2]},
        )

        self.assertEqual(response.data["message"], "Staff rules saved.")
        data = self.get(self.tolu, "staff-rules").data["data"]
        self.assertEqual(data["required_documents"], ["CV", "IDENTIFICATION"])
        self.assertEqual(set(data["self_editable_fields"]), {"phone", "gender"})
        self.assertEqual(data["leave"]["allowances"]["ANNUAL"], 20)
        self.assertIsNone(data["leave"]["allowances"]["SICK"])
        self.assertEqual(data["leave"]["working_days"], [1, 2, 6])

    def test_the_reason_is_recorded_and_an_unchanged_save_writes_nothing(self):
        from vs_config.models import ConfigurationAuditEvent

        self.save_rules(required_documents=["CV"], reason="Inspection asked for CVs.")
        self.assertTrue(ConfigurationAuditEvent.objects.filter(
            reason="Inspection asked for CVs.",
        ).exists())
        before = ConfigurationAuditEvent.objects.count()
        self.save_rules(required_documents=["CV"])
        self.assertEqual(ConfigurationAuditEvent.objects.count(), before)


class LeaveGroupRuleTests(_SettingsFixture):
    def test_group_assignment_and_exception_precedence(self):
        from schools.vs_staff.services.rules import leave_rules

        group_id = str(uuid4())
        self.save_rules(leave={
            "allowances": {"ANNUAL": 20},
            "groups": [{"id": group_id, "name": "Senior staff"}],
            "overrides": [
                {"branch_id": self.lekki.pk, "group_id": None, "leave_type": "ANNUAL", "days": 18},
                {"branch_id": None, "group_id": group_id, "leave_type": "ANNUAL", "days": 25},
                {"branch_id": self.lekki.pk, "group_id": group_id, "leave_type": "ANNUAL", "days": 30},
            ],
        })
        response = self.put(self.tolu, "staff-leave-group", {"group_id": group_id}, pk=self.eze.pk)
        self.assertEqual(response.status_code, 200, response.data)
        self.eze.refresh_from_db()
        rules = leave_rules(self.tenant)
        self.assertEqual(rules.allowance_for("ANNUAL", self.eze), 30)
        self.assertEqual(rules.allowance_for("ANNUAL", self.ikeja_teacher), 20)
        self.assertEqual(rules.allowance_for("ANNUAL", self.registrar), 20)
        self.assertEqual(self.get(self.admin, "staff-leave", pk=self.eze.pk).data["data"]["leave_group"]["name"], "Senior staff")

        self.put(self.tolu, "staff-leave-group", {"group_id": None}, pk=self.eze.pk)
        self.eze.refresh_from_db()
        self.assertEqual(rules.allowance_for("ANNUAL", self.eze), 18)

    def test_group_write_requires_settings_key_and_staff_scope(self):
        group_id = str(uuid4())
        self.save_rules(leave={"groups": [{"id": group_id, "name": "Senior staff"}]})
        self.assertEqual(self.put(self.nobody, "staff-leave-group", {"group_id": group_id}, pk=self.eze.pk).status_code, 403)
        self.assertEqual(self.put(self.kemi, "staff-leave-group", {"group_id": group_id}, pk=self.ikeja_teacher.pk).status_code, 404)
        self.assertEqual(self.put(self.tolu, "staff-leave-group", {"group_id": group_id}, pk=self.solo_staff.pk).status_code, 404)
        self.assertEqual(self.put(self.tolu, "staff-leave-group", {"group_id": str(uuid4())}, pk=self.eze.pk).status_code, 400)
        self.eze.refresh_from_db()
        self.assertEqual(self.eze.leave_group, "")

    def test_removing_an_assigned_group_is_refused(self):
        group_id = str(uuid4())
        self.save_rules(leave={"groups": [{"id": group_id, "name": "Senior staff"}]})
        self.put(self.tolu, "staff-leave-group", {"group_id": group_id}, pk=self.eze.pk)
        response = self.put(self.tolu, "staff-rules", self.rules_body(leave={"groups": []}))
        self.assertEqual(response.status_code, 400, response.data)
        self.assertIn("Move staff out", str(response.data))

    def test_group_assignment_appears_in_the_leave_history(self):
        group_id = str(uuid4())
        self.save_rules(leave={"groups": [{"id": group_id, "name": "Senior staff"}]})
        self.put(self.tolu, "staff-leave-group", {"group_id": group_id}, pk=self.eze.pk)
        response = self.get(self.eze.user, "staff-section-history", {"section": "leave"}, pk=self.eze.pk)
        self.assertEqual(response.status_code, 200, response.data)
        self.assertIn({
            "field": "Leave group", "before": None, "after": "Senior staff",
        }, response.data["data"]["entries"][0]["changes"])

    def test_other_schools_branch_cannot_be_used_for_exception(self):
        response = self.put(self.tolu, "staff-rules", self.rules_body(leave={
            "overrides": [{"branch_id": self.solo_branch.pk, "group_id": None, "leave_type": "ANNUAL", "days": 5}],
        }))
        self.assertEqual(response.status_code, 400, response.data)
        self.assertIn("belonging to this school", str(response.data))


class StaffRulesValidationTests(_SettingsFixture):
    def refused(self, field, **overrides):
        response = self.put(self.admin, "staff-rules", self.rules_body(**overrides))
        self.assertEqual(response.status_code, 400, response.data)
        return str(response.data)

    def test_each_bad_value_is_refused_on_its_own_field(self):
        self.assertIn(
            "'PASSPORT' is not a document type this school can expect.",
            self.refused("required_documents", required_documents=["PASSPORT"]),
        )
        self.assertIn(
            "Choose one of this school's active roles for new staff to start with.",
            self.refused("starting_role", starting_role="caretaker"),
        )
        self.assertIn(
            "'salary' is not a detail of a staff record",
            self.refused("self_editable_fields", self_editable_fields=["salary"]),
        )
        self.assertIn(
            "'HOLIDAY' is not a leave type this school records.",
            self.refused("leave", leave={"allowances": {"HOLIDAY": 5}}),
        )
        self.assertIn(
            "Give annual leave as a whole number of days from 0 to 366",
            self.refused("leave", leave={"allowances": {"ANNUAL": 400}}),
        )
        self.assertIn(
            "Choose at least one day of the week that counts for leave.",
            self.refused("leave", leave={"working_days": []}),
        )
        self.assertIn(
            "weekdays numbered 1 (Monday) to 7 (Sunday)",
            self.refused("leave", leave={"working_days": [0, 8]}),
        )

    def test_every_rule_must_be_sent(self):
        body = self.rules_body()
        body.pop("hire_requires_approval")
        response = self.put(self.admin, "staff-rules", body)
        self.assertEqual(response.status_code, 400, response.data)
        self.assertIn("hire_requires_approval", response.data["error"]["detail"])


class StaffRulesReachTests(_SettingsFixture):
    """Kemi turning on hire approval would hold up Ikeja's hires too."""

    def test_a_branch_bound_caller_holding_the_key_is_refused_and_nothing_moves(self):
        response = self.put(
            self.kemi, "staff-rules", self.rules_body(hire_requires_approval=True),
        )
        self.assert_refused(
            response,
            "Only a school-wide administrator can change the school's staff rules.",
        )
        self.assertFalse(self.get(self.tolu, "staff-rules").data["data"]["hire_requires_approval"])

    def test_a_school_wide_settings_administrator_changes_them(self):
        self.save_rules(self.tolu, required_documents=["CV"])
        self.assertEqual(
            self.get(self.tolu, "staff-rules").data["data"]["required_documents"], ["CV"],
        )


# ── 18. The starting role ──────────────────────────────────────────────────


class StartingRoleTests(_SettingsFixture):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.assistant = make_role(cls.school, name="Class Assistant", key="class_assistant")
        cls.bursar = make_role(cls.school, name="Bursar", key="bursar")
        cls.restricted = make_permission("finance.journal.post", is_restricted=True)
        make_role_permission(cls.bursar, cls.restricted)

    def grant(self, email):
        return TenantUserRoleAssignment.objects.get(
            user__email=email, assignment_status="ACTIVE",
        )

    def test_new_staff_start_on_the_role_the_school_chose(self):
        self.save_rules(starting_role="class_assistant")

        response = self.add()

        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(self.grant("funke@brightfield.test").role.key, "class_assistant")
        listed = self.get(self.admin, "staff-list").data["starting_role"]
        self.assertEqual(listed, {"value": "class_assistant", "label": "Class Assistant"})

    def test_naming_another_role_is_refused_in_the_chosen_roles_name(self):
        self.save_rules(starting_role="class_assistant")

        response = self.add(role="teacher")

        self.assertEqual(response.status_code, 400, response.data)
        self.assertIn("New staff start as Class Assistant.", str(response.data))

    def test_a_retired_starting_role_is_named_when_the_add_is_refused(self):
        self.save_rules(starting_role="class_assistant")
        self.assistant.status = "INACTIVE"
        self.assistant.save(update_fields=["status"])

        response = self.add()

        self.assertEqual(response.status_code, 400, response.data)
        self.assertIn("no active Class Assistant role", str(response.data))
        self.assertFalse(User.objects.filter(email="funke@brightfield.test").exists())

    def test_a_restricted_role_the_saver_cannot_grant_is_refused_as_the_starting_role(self):
        response = self.put(self.tolu, "staff-rules", self.rules_body(starting_role="bursar"))

        self.assertEqual(response.status_code, 400, response.data)
        self.assertIn(
            "Bursar carries restricted permissions you do not hold, so it cannot "
            "be the role every new member of staff starts with.",
            str(response.data),
        )
        self.assertEqual(self.get(self.tolu, "staff-rules").data["data"]["starting_role"], "teacher")

    def test_saving_the_screen_unchanged_is_not_refused_for_a_key_the_saver_lacks(self):
        """Teacher carries a restricted key at every real school; Tolu holds none."""
        make_role_permission(self.teacher_role, self.restricted)

        self.save_rules(self.tolu, required_documents=["CV"])

    def test_an_adder_who_cannot_grant_the_starting_role_adds_nobody(self):
        """A starting role is never a way round the restricted grant rule."""
        from vs_config.models import ConfigurationDefinition
        from vs_config.services.resolution import set_value

        set_value(
            definition=ConfigurationDefinition.objects.get(key="staff.starting_role"),
            value="bursar", actor=self.admin, tenant=self.tenant,
        )

        response = self.add()

        self.assertEqual(response.status_code, 403, response.data)
        self.assertIn(
            "New staff start as Bursar, which carries restricted permissions you "
            "do not hold",
            response.data["message"],
        )
        self.assertFalse(User.objects.filter(email="funke@brightfield.test").exists())


# ── 19. Required documents, flagged and never enforced ─────────────────────


class RequiredDocumentTests(_SettingsFixture):
    def hold(self, staff, kind):
        StaffDocument.all_objects.create(
            tenant=self.tenant, staff=staff, document_type=kind, title=kind,
            file=f"staff/{self.tenant.pk}/documents/{staff.pk}/{kind.lower()}.pdf",
        )

    def test_a_school_that_expects_nothing_flags_nothing(self):
        data = self.get(self.admin, "staff-detail", pk=self.eze.pk).data["data"]
        self.assertEqual(data["missing_documents"], [])
        self.assertIsNone(self.get(self.admin, "staff-list").data["counts"]["missing_documents"])

    def test_the_record_names_what_it_is_missing(self):
        self.save_rules(required_documents=["CV", "IDENTIFICATION"])
        self.hold(self.eze, "CV")

        data = self.get(self.admin, "staff-detail", pk=self.eze.pk).data["data"]

        self.assertEqual(
            data["missing_documents"], [{"type": "IDENTIFICATION", "label": "National ID"}],
        )

    def test_the_list_filters_and_counts_the_people_missing_one(self):
        self.save_rules(required_documents=["CV"])
        self.hold(self.eze, "CV")

        response = self.get(self.admin, "staff-list", {"missing_documents": "true"})

        ids = {row["id"] for row in response.data["data"]}
        self.assertNotIn(self.eze.pk, ids)
        self.assertIn(self.registrar.pk, ids)
        self.assertEqual(response.data["counts"]["missing_documents"], len(ids))

    def test_a_reader_without_a_records_key_learns_nothing_about_documents(self):
        self.save_rules(required_documents=["CV"])

        response = self.get(self.tolu, "staff-list", {"missing_documents": "true"})

        self.assertEqual(response.data["data"], [])
        self.assertIsNone(response.data["counts"]["missing_documents"])

    def test_nothing_is_refused_for_a_missing_document(self):
        self.save_rules(required_documents=["CV", "DEGREE_CERTIFICATE"])
        self.assertEqual(self.add().status_code, 201)


# ── 22. What staff may change about themselves ─────────────────────────────


class SelfEditTests(_SettingsFixture):
    def test_by_default_the_four_personal_facts_and_nothing_else(self):
        allowed = self.patch(self.eze.user, "staff-detail", {"phone": "0803 111 2222"}, pk=self.eze.pk)
        refused = self.patch(self.eze.user, "staff-detail", {"gender": "MALE"}, pk=self.eze.pk)

        self.assertEqual(allowed.status_code, 200, allowed.data)
        self.assertEqual(refused.status_code, 422, refused.data)
        self.assertEqual(refused.data["error"]["code"], "FIELD_NOT_SELF_EDITABLE")

    def test_the_schools_list_decides(self):
        self.save_rules(self_editable_fields=["gender"])

        phone = self.patch(self.eze.user, "staff-detail", {"phone": "0803 111 2222"}, pk=self.eze.pk)
        gender = self.patch(self.eze.user, "staff-detail", {"gender": "MALE"}, pk=self.eze.pk)

        self.assertEqual(phone.status_code, 422, phone.data)
        self.assertIn("phone", str(phone.data))
        self.assertEqual(gender.status_code, 200, gender.data)

    def test_the_own_record_carries_the_list_and_nobody_elses_does(self):
        self.save_rules(self_editable_fields=["gender", "phone"])

        own = self.get(self.eze.user, "staff-detail", pk=self.eze.pk).data["data"]
        other = self.get(self.admin, "staff-detail", pk=self.eze.pk).data["data"]

        self.assertEqual(set(own["self_editable_fields"]), {"gender", "phone"})
        self.assertNotIn("self_editable_fields", other)

    def test_the_floor_can_never_be_made_self_editable(self):
        response = self.put(
            self.admin, "staff-rules", self.rules_body(self_editable_fields=["staff_number"]),
        )
        self.assertEqual(response.status_code, 400, response.data)
        self.assertIn(
            "Staff ID is the school's to set, so staff can never change it about themselves.",
            str(response.data),
        )
        job = self.patch(self.eze.user, "staff-detail", {"job_title": "Head"}, pk=self.eze.pk)
        self.assertEqual(job.status_code, 422, job.data)


# ── 17. The staff-number rule ──────────────────────────────────────────────


class NumberPolicyReadTests(_SettingsFixture):
    def test_a_school_with_no_rule_reads_the_permissive_default(self):
        data = self.client_for(self.admin).get(self.policy_path()).data["data"]
        self.assertEqual(data, {
            "required": False, "pattern": "", "hint": "", "auto_issue": False,
            "source": "default", "suggestion": "BFS/STF/0013",
        })

    def test_an_add_with_no_number_stays_unnumbered_by_default(self):
        response = self.add(staff_number="")
        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(response.data["data"]["staff_number"], "")


class NumberPolicyEnforcementTests(_SettingsFixture):
    HINT = "Use BFS/STF/ and four digits, such as BFS/STF/0042."

    def test_a_required_pattern_refuses_a_blank_and_a_wrong_shape_in_the_hint(self):
        self.save_policy(required=True, pattern=r"BFS/STF/\d{4}", hint=self.HINT)

        blank = self.add(staff_number="")
        wrong = self.add(staff_number="STAFF-9")
        right = self.add(staff_number="BFS/STF/0042")

        for response in (blank, wrong):
            self.assertEqual(response.status_code, 400, response.data)
            self.assertEqual(str(response.data["error"]["detail"]["staff_number"]), self.HINT)
        self.assertEqual(right.status_code, 201, right.data)

    def test_the_pattern_is_anchored(self):
        self.save_policy(pattern=r"BFS/STF/\d{4}")
        response = self.add(staff_number="BFS/STF/00429")
        self.assertEqual(response.status_code, 400, response.data)

    def test_an_edit_is_checked_and_an_echo_of_the_stored_number_is_not(self):
        self.save_policy(required=True, pattern=r"IKJ/\d{3}")

        echo = self.patch(self.admin, "staff-detail", {"staff_number": "bfs/stf/0012"}, pk=self.eze.pk)
        changed = self.patch(self.admin, "staff-detail", {"staff_number": "BFS/STF/0077"}, pk=self.eze.pk)
        blanked = self.patch(self.admin, "staff-detail", {"staff_number": ""}, pk=self.eze.pk)

        self.assertEqual(echo.status_code, 200, echo.data)
        self.assertEqual(changed.status_code, 400, changed.data)
        self.assertEqual(blanked.status_code, 400, blanked.data)
        self.eze.refresh_from_db()
        self.assertEqual(self.eze.staff_number, "BFS/STF/0012")

    def test_an_uncompilable_pattern_is_refused_on_its_field(self):
        response = self.client_for(self.admin).put(
            self.policy_path(), {"required": False, "pattern": "BFS/(", "hint": ""},
            format="json",
        )
        self.assertEqual(response.status_code, 400, response.data)
        self.assertIn("That pattern is not a valid expression", str(response.data["error"]["detail"]["pattern"]))


class NumberAutoIssueTests(_SettingsFixture):
    def setUp(self):
        super().setUp()
        self.save_policy(auto_issue=True)

    def test_a_blank_number_is_issued_the_next_in_the_series(self):
        first = self.add(staff_number="")
        second = self.add(staff_number="", email="sade@brightfield.test")

        self.assertEqual(first.data["data"]["staff_number"], "BFS/STF/0013")
        self.assertEqual(second.data["data"]["staff_number"], "BFS/STF/0014")

    def test_a_number_once_held_is_never_issued_again(self):
        """BFS/STF/0014 was Sade's sign-in ID before it was taken off her."""
        self.add(staff_number="", email="funke@brightfield.test")
        sade = self.add(staff_number="", email="sade@brightfield.test").data["data"]
        self.assertEqual(sade["staff_number"], "BFS/STF/0014")
        cleared = self.patch(self.admin, "staff-detail", {"staff_number": ""}, pk=sade["id"])
        self.assertEqual(cleared.status_code, 200, cleared.data)

        third = self.add(staff_number="", email="bayo@brightfield.test")

        self.assertEqual(third.data["data"]["staff_number"], "BFS/STF/0015")

    def test_a_required_rule_with_no_series_to_continue_says_so(self):
        StaffProfile.all_objects.filter(tenant=self.tenant).update(staff_number="")
        self.save_policy(required=True, auto_issue=True)

        response = self.add(staff_number="")

        self.assertEqual(response.status_code, 400, response.data)
        self.assertIn("has none to continue from yet", str(response.data))


class NumberBranchOverrideTests(_SettingsFixture):
    HINT = "Ikeja numbers look like IKJ/001."

    def setUp(self):
        super().setUp()
        response = self.save_policy(
            branch=self.ikeja, required=True, pattern=r"IKJ/\d{3}", hint=self.HINT,
        )
        self.assertEqual(response.data["data"]["source"], "branch")

    def test_a_person_posted_to_the_branch_follows_its_own_rule(self):
        wrong = self.add(branch=self.ikeja.pk, staff_number="BFS/STF/0099")
        right = self.add(branch=self.ikeja.pk, staff_number="IKJ/001", email="ik@brightfield.test")
        lekki = self.add(branch=self.lekki.pk, staff_number="BFS/STF/0099", email="lk@brightfield.test")

        self.assertEqual(wrong.status_code, 400, wrong.data)
        self.assertEqual(str(wrong.data["error"]["detail"]["staff_number"]), self.HINT)
        self.assertEqual(right.status_code, 201, right.data)
        self.assertEqual(lekki.status_code, 201, lekki.data)

    def test_removing_the_branchs_rule_returns_it_to_the_schools(self):
        client = self.client_for(self.admin)
        removed = client.delete(self.policy_path(self.ikeja))
        refused = client.delete(self.policy_path())

        self.assertEqual(removed.status_code, 200, removed.data)
        self.assertEqual(removed.data["data"]["source"], "default")
        self.assertEqual(refused.status_code, 400, refused.data)


class NumberPolicyReachTests(_SettingsFixture):
    def test_the_schools_rule_is_refused_to_a_branch_bound_caller_and_nothing_moves(self):
        response = self.client_for(self.kemi).put(
            self.policy_path(), {"required": True, "pattern": "", "hint": ""}, format="json",
        )
        self.assert_refused(
            response,
            "Only a school-wide administrator can change the school's staff number "
            "rule. Choose one of your branches to set its own.",
        )
        from schools.vs_staff.services.number_policy import read_policy

        self.assertEqual(read_policy(self.tenant).source, "default")

    def test_a_branch_bound_caller_sets_their_own_branchs_rule_and_not_anothers(self):
        client = self.client_for(self.kemi)
        own = client.put(
            self.policy_path(self.lekki), {"required": True, "pattern": "", "hint": ""},
            format="json",
        )
        other = client.put(
            self.policy_path(self.ikeja), {"required": True, "pattern": "", "hint": ""},
            format="json",
        )
        self.assertEqual(own.status_code, 200, own.data)
        self.assertEqual(other.status_code, 404, other.data)


class NumberPolicyImportTests(_SettingsMixin, _ImportFixture):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.add_settings_people()

    def test_a_row_is_held_to_the_rule_for_its_branch(self):
        from schools.vs_staff.imports import validate_rows

        self.save_policy(required=True, pattern=r"BFS/IMP/\d{3}", hint="Use BFS/IMP/ and three digits.")

        issues = validate_rows(self.batch(
            self.row(**{"Staff ID": ""}),
            self.row(**{"Staff ID": "IMP-1", "Email": "two@brightfield.test"}),
        ))

        self.assertEqual(
            [(i["code"], i["message"]) for i in issues],
            [("staff_number_required", "Use BFS/IMP/ and three digits."),
             ("staff_number_format", "Use BFS/IMP/ and three digits.")],
        )

    def test_a_blank_row_is_issued_a_number_when_the_rule_issues_them(self):
        from schools.vs_staff.imports import create_staff_from_row, resolve_row

        self.save_policy(required=True, auto_issue=True)
        row = resolve_row(
            {"first_name": "Ifeoma", "last_name": "Anyanwu",
             "email": "ifeoma@brightfield.test"},
            tenant=self.tenant, actor=self.admin,
        )
        self.assertTrue(row.ok, row.issues)

        profile = create_staff_from_row(row, tenant=self.tenant, created_by=self.admin)

        self.assertEqual(profile.staff_number, "BFS/STF/0013")


# ── 20 and 21. Leave allowances and working days ───────────────────────────


class _LeaveSettingsFixture(_SettingsMixin, LeaveFixture):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.add_settings_people()

    def file(self, **body):
        response = self.post(self.admin, "staff-leave", self.body(**body), pk=self.eze.pk)
        self.assertEqual(response.status_code, 201, response.data)
        return response

    def instance(self, leave_id):
        return WorkflowInstance.all_objects.get(
            document_type="schools.leave_request", document_object_id=str(leave_id),
        )

    def balance(self, leave_type="ANNUAL", **params):
        data = self.get(self.admin, "staff-leave", params, pk=self.eze.pk).data["data"]
        return next(row for row in data["balances"] if row["leave_type"] == leave_type), data


class LeaveAllowanceTests(_LeaveSettingsFixture):
    def setUp(self):
        super().setUp()
        self.save_rules(leave={"allowances": {"ANNUAL": 10}})

    def test_leave_past_the_allowance_is_filed_and_marked_for_the_approver(self):
        within = self.file(start_date="2026-03-02", end_date="2026-03-13")
        over = self.file(start_date="2026-03-16", end_date="2026-03-18")

        self.assertEqual(within.data["data"]["leave"]["over_allowance_by"], 0)
        self.assertEqual(within.data["data"]["warnings"], [])
        leave = over.data["data"]["leave"]
        self.assertEqual(leave["over_allowance_by"], 3)
        self.assertEqual(leave["status"], LeaveStatus.PENDING)
        warning = over.data["data"]["warnings"][0]
        self.assertEqual(warning["code"], "OVER_ALLOWANCE")
        self.assertEqual(warning["over_allowance_by"], 3)
        self.assertIn("3 days past the 10 days of annual leave", warning["message"])

        instance = self.instance(leave["id"])
        self.assertIn(
            {"label": "Over allowance", "value": "3 days"}, instance.document_summary["fields"],
        )
        self.assertIn(
            {"label": "Over allowance by", "value": "3 days"},
            instance.document_details["sections"][0]["items"],
        )

    def test_the_leave_list_carries_this_sessions_balances(self):
        from vs_workflow.services import actions

        first = self.file(start_date="2026-03-02", end_date="2026-03-13").data["data"]["leave"]
        actions.record_action(self.instance(first["id"]).id, self.lekki_head, "APPROVED", "")
        self.file(start_date="2026-03-16", end_date="2026-03-18")

        annual, data = self.balance()
        sick, _ = self.balance("SICK")

        self.assertEqual(data["balance_session"]["id"], self.year.pk)
        self.assertEqual(annual, {
            "leave_type": "ANNUAL", "label": "Annual", "allowance": 10,
            "taken": 10, "pending": 3, "remaining": -3,
        })
        self.assertEqual(sick["allowance"], None)
        self.assertEqual(sick["remaining"], None)

    def test_each_session_counts_on_its_own(self):
        """Leave last session does not eat into this session's allowance."""
        self.file(start_date="2025-03-03", end_date="2025-03-14")
        this_year = self.file(start_date="2026-03-02", end_date="2026-03-13")

        self.assertEqual(this_year.data["data"]["leave"]["over_allowance_by"], 0)
        current, _ = self.balance()
        last, data = self.balance(session=self.archived_year.pk)
        self.assertEqual(current["pending"], 10)
        self.assertEqual(last["pending"], 10)
        self.assertEqual(data["balance_session"]["name"], "2024/2025")

    def test_another_schools_session_is_a_404(self):
        from schools.vs_academics.models import AcademicSession

        theirs = AcademicSession.all_objects.create(
            tenant=self.solo.tenant, name="2025/2026",
            start_date=dt.date(2025, 9, 1), end_date=dt.date(2026, 7, 31),
        )
        response = self.get(self.admin, "staff-leave", {"session": theirs.pk}, pk=self.eze.pk)
        self.assertEqual(response.status_code, 404, response.data)


class LeaveWorkingDayTests(_LeaveSettingsFixture):
    """Christmas week at Lekki: 22 to 26 December 2025, Monday to Friday."""

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        from schools.vs_calendar.models import CalendarEvent, CalendarEventAudience

        def closure(name, day, branch=None):
            return CalendarEvent.all_objects.create(
                tenant=cls.tenant, session=cls.year, branch=branch, name=name,
                event_type="HOLIDAY", start_date=day, end_date=day, closes_school=True,
            )

        closure("Lekki staff retreat", dt.date(2025, 12, 23), cls.lekki)
        closure("Ikeja flooding", dt.date(2025, 12, 24), cls.ikeja)
        closure("Christmas Day", dt.date(2025, 12, 25))
        speech_day = closure("JSS1 speech day", dt.date(2025, 12, 26), cls.lekki)
        CalendarEventAudience.all_objects.create(
            tenant=cls.tenant, event=speech_day, level=cls.jss1,
        )

    def filed_days(self, **body):
        return self.file(**body).data["data"]["leave"]["days"]

    def test_weekends_and_the_closures_that_reach_the_person_are_left_out(self):
        """The Lekki closure and Christmas count; Ikeja's and a class event do not."""
        self.assertEqual(self.filed_days(start_date="2025-12-20", end_date="2025-12-28"), 3)

    def test_a_school_that_counts_closures_counts_them(self):
        self.save_rules(leave={"exclude_closures": False})
        self.assertEqual(self.filed_days(start_date="2025-12-22", end_date="2025-12-26"), 5)

    def test_a_school_that_works_saturdays_counts_them(self):
        self.save_rules(leave={"working_days": [1, 2, 3, 4, 5, 6]})
        self.assertEqual(self.filed_days(start_date="2026-01-09", end_date="2026-01-11"), 2)

    def test_a_request_on_days_the_school_does_not_count_is_refused(self):
        response = self.post(
            self.admin, "staff-leave",
            self.body(start_date="2026-01-10", end_date="2026-01-11"), pk=self.eze.pk,
        )
        self.assertEqual(response.status_code, 422, response.data)
        self.assertEqual(response.data["error"]["code"], "NO_WORKING_DAYS")

    def test_a_count_already_stored_is_never_recomputed(self):
        days = self.filed_days(start_date="2026-01-05", end_date="2026-01-09")
        self.save_rules(leave={"working_days": [1, 2, 3]})

        self.assertEqual(days, 5)
        self.assertEqual(LeaveRequest.all_objects.get(staff=self.eze).days, 5)


# ── 23. Approval before a new hire is invited ──────────────────────────────


class HireApprovalTests(_SettingsFixture):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        from vs_workflow.models import WorkflowApproverGroup, WorkflowApproverGroupMember

        from schools.vs_staff.approvals import ensure_hire_approval_template

        ensure_hire_approval_template(cls.tenant)
        group = WorkflowApproverGroup.all_objects.get(tenant=cls.tenant, code="hire-approvers")
        WorkflowApproverGroupMember.objects.create(group=group, kind="USER", user=cls.lekki_head)

    def add_with_mail(self, **overrides):
        with mock.patch("vs_user.tasks.send_invitation_email_task.delay") as delay:
            with self.captureOnCommitCallbacks(execute=True):
                response = self.add(branch=self.lekki.pk, **overrides)
        return response, delay

    def decide(self, staff_id, action, comment=""):
        from vs_workflow.services import actions

        instance = WorkflowInstance.all_objects.get(
            document_type="schools.staff_hire", document_object_id=str(staff_id),
        )
        with mock.patch("vs_user.tasks.send_invitation_email_task.delay") as delay:
            with self.captureOnCommitCallbacks(execute=True):
                actions.record_action(instance.id, self.lekki_head, action, comment)
        return delay

    def test_by_default_an_add_invites_straight_away(self):
        response, delay = self.add_with_mail()

        self.assertEqual(response.data["message"], "Invitation sent.")
        self.assertFalse(response.data["data"]["awaiting_approval"])
        self.assertEqual(delay.call_count, 1)

    def test_with_approval_on_the_add_waits_and_nobody_is_emailed(self):
        self.save_rules(hire_requires_approval=True)

        response, delay = self.add_with_mail()

        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(
            response.data["message"],
            "Added. Their invitation is waiting for the hire to be approved.",
        )
        data = response.data["data"]
        self.assertTrue(data["awaiting_approval"])
        self.assertEqual(data["employment_status"], "PENDING_APPROVAL")
        self.assertEqual(data["employment_status_label"], "Awaiting approval")
        self.assertTrue(data["lifecycle"]["on_path"])
        self.assertEqual(
            [step["value"] for step in data["lifecycle"]["steps"]],
            ["PENDING_APPROVAL", "INVITED", "ACTIVE"],
        )
        self.assertEqual(delay.call_count, 0)
        user = User.objects.get(email="funke@brightfield.test")
        self.assertEqual(user.status, User.Status.PENDING_APPROVAL)
        self.assertFalse(hasattr(user, "invitation") and user.invitation)
        self.assertTrue(WorkflowInstance.all_objects.filter(
            document_type="schools.staff_hire", document_object_id=str(data["id"]),
        ).exists())

    def test_the_list_filters_and_counts_hires_awaiting_approval(self):
        self.save_rules(hire_requires_approval=True)
        staff_id = self.add_with_mail()[0].data["data"]["id"]

        response = self.get(self.admin, "staff-list", {"employment_status": "PENDING_APPROVAL"})

        self.assertEqual([row["id"] for row in response.data["data"]], [staff_id])
        self.assertIn(
            {"value": "PENDING_APPROVAL", "label": "Awaiting approval", "count": 1},
            response.data["counts"]["by_employment_status"],
        )

    def test_approval_sends_the_invitation(self):
        self.save_rules(hire_requires_approval=True)
        staff_id = self.add_with_mail()[0].data["data"]["id"]

        delay = self.decide(staff_id, "APPROVED")

        self.assertEqual(delay.call_count, 1)
        staff = StaffProfile.all_objects.get(pk=staff_id)
        self.assertEqual(staff.employment_status, EmploymentStatus.INVITED)
        self.assertEqual(staff.user.status, User.Status.PENDING)
        self.assertEqual(
            staff.employment_events.order_by("-created_at", "-id").first().reason,
            "Hire approved",
        )

    def test_rejection_closes_the_hire(self):
        self.save_rules(hire_requires_approval=True)
        staff_id = self.add_with_mail()[0].data["data"]["id"]

        delay = self.decide(staff_id, "REJECTED", "We are not hiring a bursar this term.")

        self.assertEqual(delay.call_count, 0)
        staff = StaffProfile.all_objects.get(pk=staff_id)
        self.assertEqual(staff.employment_status, EmploymentStatus.TERMINATED)
        self.assertEqual(staff.user.status, User.Status.REJECTED)
        self.assertFalse(TenantUserRoleAssignment.objects.filter(
            user=staff.user, assignment_status="ACTIVE",
        ).exists())

    def test_resend_is_refused_while_the_hire_waits(self):
        self.save_rules(hire_requires_approval=True)
        staff_id = self.add_with_mail()[0].data["data"]["id"]

        response = self.post(self.admin, "staff-resend", pk=staff_id)

        self.assertEqual(response.status_code, 422, response.data)
        self.assertEqual(response.data["error"]["code"], "HIRE_AWAITING_APPROVAL")

    def test_revoking_a_waiting_hire_withdraws_it(self):
        self.save_rules(hire_requires_approval=True)
        staff_id = self.add_with_mail()[0].data["data"]["id"]

        response = self.post(
            self.admin, "staff-invitation-revoke", {"reason": "Wrong email typed."}, pk=staff_id,
        )

        self.assertEqual(response.status_code, 200, response.data)
        self.assertIn("Hire withdrawn before it was approved", response.data["message"])
        staff = StaffProfile.all_objects.get(pk=staff_id)
        self.assertEqual(staff.employment_status, EmploymentStatus.TERMINATED)
        self.assertEqual(staff.user.status, User.Status.REJECTED)
        instance = WorkflowInstance.all_objects.get(
            document_type="schools.staff_hire", document_object_id=str(staff_id),
        )
        self.assertEqual(instance.status, "CANCELLED")

    def seat(self, *people):
        """Put exactly *people* in the hire-approvers group."""
        from vs_workflow.models import WorkflowApproverGroup, WorkflowApproverGroupMember

        group = WorkflowApproverGroup.all_objects.get(tenant=self.tenant, code="hire-approvers")
        WorkflowApproverGroupMember.objects.filter(group=group).delete()
        for person in people:
            WorkflowApproverGroupMember.objects.create(group=group, kind="USER", user=person)

    def vote(self, staff_id, person):
        from vs_workflow.services import actions

        instance = WorkflowInstance.all_objects.get(
            document_type="schools.staff_hire", document_object_id=str(staff_id),
        )
        with mock.patch("vs_user.tasks.send_invitation_email_task.delay"):
            with self.captureOnCommitCallbacks(execute=True):
                actions.record_action(instance.id, person, "APPROVED", "")

    def test_the_adder_alone_in_the_group_approves_their_own_hire(self):
        """Adaeze runs Brightfield on her own: her hires are not stranded."""
        self.save_rules(hire_requires_approval=True)
        self.seat(self.admin)
        staff_id = self.add_with_mail()[0].data["data"]["id"]

        self.vote(staff_id, self.admin)

        staff = StaffProfile.all_objects.get(pk=staff_id)
        self.assertEqual(staff.employment_status, EmploymentStatus.INVITED)

    def test_the_adder_cannot_approve_while_somebody_else_is_on_the_stage(self):
        """With the Lekki head in the group, Adaeze's own hire is hers to decide."""
        self.save_rules(hire_requires_approval=True)
        self.seat(self.admin, self.lekki_head)
        staff_id = self.add_with_mail()[0].data["data"]["id"]

        from vs_workflow.exceptions import NotAnEligibleApproverError

        with self.assertRaises(NotAnEligibleApproverError):
            self.vote(staff_id, self.admin)
        self.assertEqual(
            StaffProfile.all_objects.get(pk=staff_id).employment_status,
            EmploymentStatus.PENDING_APPROVAL,
        )

        self.vote(staff_id, self.lekki_head)
        self.assertEqual(
            StaffProfile.all_objects.get(pk=staff_id).employment_status,
            EmploymentStatus.INVITED,
        )

    def test_turning_it_on_publishes_the_ladder(self):
        from vs_workflow.models import WorkflowTemplate

        WorkflowTemplate.all_objects.filter(document_type="schools.staff_hire").delete()
        self.save_rules(hire_requires_approval=True)
        self.assertTrue(WorkflowTemplate.all_objects.filter(
            tenant=self.tenant, document_type="schools.staff_hire",
        ).exists())


class HireApprovalImportTests(_SettingsMixin, _ImportFixture):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.add_settings_people()

    def test_an_imported_row_waits_for_approval_too(self):
        from schools.vs_staff.imports import create_staff_from_row, resolve_row

        self.save_rules(hire_requires_approval=True)
        row = resolve_row(
            {"first_name": "Ifeoma", "last_name": "Anyanwu",
             "email": "ifeoma@brightfield.test"},
            tenant=self.tenant, actor=self.admin,
        )
        with mock.patch("vs_user.tasks.send_invitation_email_task.delay") as delay:
            with self.captureOnCommitCallbacks(execute=True):
                profile = create_staff_from_row(row, tenant=self.tenant, created_by=self.admin)

        self.assertEqual(profile.employment_status, EmploymentStatus.PENDING_APPROVAL)
        self.assertEqual(delay.call_count, 0)

"""A school's student settings: enrolment rules, and admission numbers per branch.

Security first: who may read and change the rules, and that one school's rules
and branches never reach another. Then each rule's effect on the paths it
governs. Brightfield has two branches and Sunrise one, so every rule is seen
at both shapes of school.
"""
from __future__ import annotations

import datetime as dt
from unittest import mock

from vs_rbac.models import PermissionScope
from vs_rbac.tests.helpers import (
    make_assignment,
    make_permission,
    make_role,
    make_role_permission,
    make_school_admin,
)

from ..constants import StudentStatus
from ..models import ClassEnrolment, Student
from .base import StudentsFixture


def years_ago(years, *, month=1, day=15):
    """A birth date that makes a child *years* old by calendar year."""
    return dt.date(dt.date.today().year - years, month, day)


class _SettingsFixture(StudentsFixture):
    """The shared fixture plus a settings admin at each school."""

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        settings_key = make_permission(
            "school.settings.update", scope=PermissionScope.TENANT,
        )
        view_key = cls.permissions["school.students.view"]

        cls.settings_role = make_role(
            cls.school, name="Settings Admin", key="settings_admin",
        )
        make_role_permission(cls.settings_role, settings_key)
        make_role_permission(cls.settings_role, view_key)
        cls.settings_admin = make_school_admin(
            None, email="settings@brightfield.test", tenant=cls.tenant,
        )
        make_assignment(cls.school, cls.settings_admin, cls.settings_role, branch=None)

        solo_role = make_role(cls.solo, name="Settings Admin", key="settings_admin")
        make_role_permission(solo_role, settings_key)
        make_role_permission(solo_role, view_key)
        cls.solo_settings = make_school_admin(
            None, email="settings@sunrise.test", tenant=cls.solo.tenant,
        )
        make_assignment(cls.solo, cls.solo_settings, solo_role, branch=None)

    def rules_body(self, **overrides):
        body = {
            "min_age_years": 2, "max_age_years": 25,
            "required_documents": ["BIRTH_CERTIFICATE"],
            "required_fields": [], "capacity_mode": "WARN",
            "default_capacity": None,
        }
        body.update(overrides)
        return body

    def set_rules(self, user=None, **overrides):
        response = self.put(
            user or self.settings_admin, "student-enrolment-rules",
            self.rules_body(**overrides),
        )
        self.assertEqual(response.status_code, 200, response.data)
        return response


# ── security ────────────────────────────────────────────────────────────────

class EnrolmentRulesSecurityTests(_SettingsFixture):

    def test_reading_the_rules_needs_the_students_view_key(self):
        self.assertEqual(
            self.get(self.nobody, "student-enrolment-rules").status_code, 403,
        )
        self.assertEqual(
            self.get(self.admin, "student-enrolment-rules").status_code, 200,
        )

    def test_changing_the_rules_needs_the_settings_key_not_a_student_key(self):
        """Adaeze holds every student key and still may not change the rules."""
        refused = self.put(self.admin, "student-enrolment-rules", self.rules_body(
            min_age_years=4,
        ))
        self.assertEqual(refused.status_code, 403, refused.data)
        self.assertEqual(
            self.put(self.nobody, "student-enrolment-rules", self.rules_body())
            .status_code, 403,
        )
        self.set_rules(min_age_years=4)

    def test_one_schools_rules_never_reach_another(self):
        self.set_rules(min_age_years=5, max_age_years=11, capacity_mode="HARD")
        theirs = self.get(self.solo_admin, "student-enrolment-rules").data["data"]
        self.assertEqual(
            (theirs["min_age_years"], theirs["max_age_years"], theirs["capacity_mode"]),
            (2, 25, "WARN"),
        )

    def test_a_single_branch_school_sets_its_own(self):
        self.set_rules(user=self.solo_settings, max_age_years=18)
        self.assertEqual(
            self.get(self.solo_admin, "student-enrolment-rules")
            .data["data"]["max_age_years"], 18,
        )
        self.assertEqual(
            self.get(self.admin, "student-enrolment-rules")
            .data["data"]["max_age_years"], 25,
        )


class AdmissionPolicyBranchSecurityTests(_SettingsFixture):

    def test_another_schools_branch_is_a_404_on_every_verb(self):
        params = {"branch": self.solo_branch.pk}
        url = "student-admission-policy"
        self.assertEqual(self.get(self.admin, url, params).status_code, 404)
        client = self.client_for(self.admin)
        from django.urls import reverse

        path = f"{reverse(url)}?tenant={self.tenant.slug}&branch={self.solo_branch.pk}"
        body = {"required": True, "pattern": "", "hint": ""}
        self.assertEqual(client.put(path, body, format="json").status_code, 404)
        self.assertEqual(client.delete(path).status_code, 404)

    def test_a_branch_the_caller_cannot_see_is_a_404(self):
        """The Lekki head cannot read or set Ikeja's rule by naming it."""
        self.assertEqual(
            self.get(
                self.lekki_head, "student-admission-policy",
                {"branch": self.ikeja.pk},
            ).status_code, 404,
        )
        self.assertEqual(
            self.get(
                self.lekki_head, "student-admission-policy",
                {"branch": self.lekki.pk},
            ).status_code, 200,
        )

    def test_a_malformed_branch_is_a_404_not_an_error(self):
        self.assertEqual(
            self.get(
                self.admin, "student-admission-policy", {"branch": "lekki"},
            ).status_code, 404,
        )

    def test_removing_a_branch_rule_needs_the_update_key(self):
        from django.urls import reverse

        path = (
            f"{reverse('student-admission-policy')}?tenant={self.tenant.slug}"
            f"&branch={self.ikeja.pk}"
        )
        self.assertEqual(self.client_for(self.nobody).delete(path).status_code, 403)


# ── the rules endpoint ──────────────────────────────────────────────────────

class EnrolmentRulesShapeTests(_SettingsFixture):

    def test_a_school_that_has_set_nothing_reads_todays_behaviour(self):
        data = self.get(self.admin, "student-enrolment-rules").data["data"]
        self.assertEqual(data["min_age_years"], 2)
        self.assertEqual(data["max_age_years"], 25)
        self.assertEqual(data["required_documents"], ["BIRTH_CERTIFICATE"])
        self.assertEqual(data["required_fields"], [])
        self.assertEqual(data["capacity_mode"], "WARN")
        self.assertIsNone(data["default_capacity"])
        self.assertEqual(
            [d["value"] for d in data["document_types"]],
            ["BIRTH_CERTIFICATE", "REPORT_CARD", "PASSPORT_PHOTO",
             "TRANSFER_CERTIFICATE", "IMMUNISATION"],
        )
        self.assertEqual(
            {f["value"] for f in data["optional_fields"]},
            {"nationality", "state_of_origin", "address", "phone", "email",
             "previous_school", "emergency_contact_name",
             "emergency_contact_phone", "blood_group", "middle_name"},
        )
        self.assertTrue(all(f["label"] for f in data["optional_fields"]))

    def test_every_requirable_field_is_optional_on_the_enrolment_serializer(self):
        """The list the screen offers is the serializer's real optional set."""
        from ..constants import REQUIRABLE_FIELDS
        from ..serializers import EnrolmentWriteSerializer

        fields = EnrolmentWriteSerializer().fields
        for name in REQUIRABLE_FIELDS:
            with self.subTest(field=name):
                self.assertIn(name, fields)
                self.assertFalse(fields[name].required)

    def test_put_returns_the_refreshed_body(self):
        response = self.set_rules(
            min_age_years=3, max_age_years=12,
            required_documents=["IMMUNISATION", "BIRTH_CERTIFICATE"],
            required_fields=["address", "nationality"],
            capacity_mode="HARD", default_capacity=35,
            reason="Primary school intake rules.",
        )
        data = response.data["data"]
        self.assertEqual((data["min_age_years"], data["max_age_years"]), (3, 12))
        # Stored in the order the screen offers them.
        self.assertEqual(
            data["required_documents"], ["BIRTH_CERTIFICATE", "IMMUNISATION"],
        )
        self.assertEqual(data["required_fields"], ["nationality", "address"])
        self.assertEqual(data["capacity_mode"], "HARD")
        self.assertEqual(data["default_capacity"], 35)
        self.assertIn("document_types", data)

    def test_a_null_default_capacity_removes_the_schools_value(self):
        self.set_rules(default_capacity=35)
        data = self.set_rules(default_capacity=None).data["data"]
        self.assertIsNone(data["default_capacity"])

    def test_every_write_is_audited_and_an_unchanged_save_writes_nothing(self):
        from vs_config.models import ConfigurationAuditEvent

        before = ConfigurationAuditEvent.objects.count()
        self.set_rules(min_age_years=4, capacity_mode="OFF")
        after_change = ConfigurationAuditEvent.objects.count()
        self.assertEqual(after_change - before, 2)
        self.set_rules(min_age_years=4, capacity_mode="OFF")
        self.assertEqual(ConfigurationAuditEvent.objects.count(), after_change)

    def assert_refused(self, field, **overrides):
        response = self.put(
            self.settings_admin, "student-enrolment-rules",
            self.rules_body(**overrides),
        )
        self.assertEqual(response.status_code, 400, response.data)
        detail = response.data["error"]["detail"]
        self.assertIn(field, detail, detail)
        return detail[field][0]

    def test_the_youngest_age_must_be_below_the_oldest(self):
        message = self.assert_refused(
            "max_age_years", min_age_years=12, max_age_years=12,
        )
        self.assertEqual(message, "The oldest age must be above the youngest.")

    def test_ages_stay_within_zero_and_ninety_nine(self):
        self.assertEqual(
            self.assert_refused("min_age_years", min_age_years=-1),
            "The youngest age cannot be below 0.",
        )
        self.assertEqual(
            self.assert_refused("max_age_years", max_age_years=100),
            "The oldest age cannot be above 99.",
        )
        self.set_rules(min_age_years=0, max_age_years=99)

    def test_an_unknown_document_is_refused(self):
        self.assertEqual(
            self.assert_refused("required_documents", required_documents=["VISA"]),
            "'VISA' is not a document this school can ask for.",
        )

    def test_only_an_optional_field_can_be_made_required(self):
        self.assertEqual(
            self.assert_refused("required_fields", required_fields=["allergies"]),
            "'allergies' is not a field this school can make required.",
        )

    def test_the_default_class_size_is_between_one_and_five_hundred(self):
        self.assert_refused("default_capacity", default_capacity=0)
        self.assert_refused("default_capacity", default_capacity=501)
        self.set_rules(default_capacity=500)

    def test_an_unknown_capacity_mode_is_refused(self):
        self.assertEqual(
            self.assert_refused("capacity_mode", capacity_mode="SOMETIMES"),
            "Choose WARN, HARD or OFF for what a full class does.",
        )

    def test_every_rule_is_sent_every_time(self):
        body = self.rules_body()
        body.pop("capacity_mode")
        response = self.put(self.settings_admin, "student-enrolment-rules", body)
        self.assertEqual(response.status_code, 400)
        self.assertIn("capacity_mode", response.data["error"]["detail"])


# ── what each rule changes ──────────────────────────────────────────────────

class AgeRuleTests(_SettingsFixture):
    """A primary school enrols children aged 4 to 12."""

    def setUp(self):
        self.set_rules(min_age_years=4, max_age_years=12)

    def enrol(self, dob):
        return self.post(
            self.admin, "student-list",
            self.enrolment_body(date_of_birth=dob.isoformat()),
        )

    def test_enrolment_refuses_either_side_and_allows_between(self):
        young = self.enrol(years_ago(3))
        self.assertEqual(young.status_code, 400, young.data)
        self.assertIn("date_of_birth", young.data["error"]["detail"])
        self.assertIn("from 4", young.data["message"])
        old = self.enrol(years_ago(13))
        self.assertEqual(old.status_code, 400, old.data)
        self.assertIn("up to 12", old.data["message"])
        self.assertEqual(self.enrol(years_ago(8)).status_code, 201)

    def test_a_school_with_no_rule_keeps_two_to_twenty_five(self):
        """Sunrise has set nothing, so a 20-year-old is still plausible there."""
        from ..ages import date_of_birth_problem

        self.assertEqual(
            date_of_birth_problem(years_ago(20), tenant=self.solo.tenant), "",
        )
        self.assertIn(
            "up to 12", date_of_birth_problem(years_ago(20), tenant=self.tenant),
        )

    def test_an_edit_is_held_to_the_schools_range(self):
        row = self.student(dob=years_ago(8))
        refused = self.patch(
            self.admin, "student-detail",
            {"date_of_birth": years_ago(20).isoformat()}, pk=row.pk,
        )
        self.assertEqual(refused.status_code, 400, refused.data)
        allowed = self.patch(
            self.admin, "student-detail",
            {"date_of_birth": years_ago(10).isoformat()}, pk=row.pk,
        )
        self.assertEqual(allowed.status_code, 200, allowed.data)

    def test_an_import_row_is_held_to_the_schools_range(self):
        from ..imports import resolve_row

        def issues(dob):
            row = resolve_row(
                {
                    "first_name": "Ada", "last_name": "Obi",
                    "date_of_birth": dob.isoformat(), "gender": "Female",
                    "branch": "Lekki", "guardian_first_name": "Ngozi",
                    "guardian_last_name": "Obi", "guardian_phone": "08035550101",
                },
                tenant=self.tenant, session=self.year, batch_branch=None,
                multi_branch=True,
            )
            return [i for i in row.issues if i.field == "date_of_birth"]

        self.assertTrue(issues(years_ago(14)))
        self.assertEqual(issues(years_ago(9)), [])


class RequiredDocumentsTests(_SettingsFixture):

    def test_the_schools_list_decides_what_is_missing(self):
        from ..services.documents import checklist, missing_required

        row = self.student()
        self.assertEqual(missing_required(row), ["BIRTH_CERTIFICATE"])
        self.set_rules(required_documents=["BIRTH_CERTIFICATE", "IMMUNISATION"])
        self.assertEqual(
            missing_required(row), ["BIRTH_CERTIFICATE", "IMMUNISATION"],
        )
        required = {r["document_type"] for r in checklist(row) if r["required"]}
        self.assertEqual(required, {"BIRTH_CERTIFICATE", "IMMUNISATION"})

        self.set_rules(required_documents=[])
        self.assertEqual(missing_required(row), [])

    def test_another_schools_list_is_its_own(self):
        from ..services.documents import missing_required

        self.set_rules(required_documents=[])
        theirs = self.student(tenant=self.solo.tenant, branch=self.solo_branch)
        self.assertEqual(missing_required(theirs), ["BIRTH_CERTIFICATE"])

    def test_a_missing_required_document_still_never_blocks_enrolment(self):
        self.set_rules(required_documents=["BIRTH_CERTIFICATE", "REPORT_CARD"])
        response = self.post(self.admin, "student-list", self.enrolment_body())
        self.assertEqual(response.status_code, 201, response.data)


class RequiredFieldsTests(_SettingsFixture):

    def setUp(self):
        self.set_rules(required_fields=["nationality", "address"])

    def test_enrolment_refuses_a_blank_required_field_by_name(self):
        response = self.post(
            self.admin, "student-list", self.enrolment_body(nationality=" "),
        )
        self.assertEqual(response.status_code, 400, response.data)
        detail = response.data["error"]["detail"]
        self.assertEqual(detail["nationality"], ["Nationality is required at this school."])
        self.assertEqual(detail["address"], ["Home address is required at this school."])
        self.assertFalse(Student.all_objects.filter(first_name="Zainab").exists())

        allowed = self.post(self.admin, "student-list", self.enrolment_body(
            nationality="Nigerian", address="12 Admiralty Way, Lekki",
        ))
        self.assertEqual(allowed.status_code, 201, allowed.data)

    def test_a_single_branch_school_is_not_held_to_another_schools_fields(self):
        from ..serializers import EnrolmentWriteSerializer

        writer = EnrolmentWriteSerializer(
            data={**self.enrolment_body(), "branch": ""},
            context={"tenant": self.solo.tenant},
        )
        writer.is_valid()
        self.assertNotIn("nationality", writer.errors)

    def test_an_edit_that_blanks_a_required_field_is_refused(self):
        row = self.student(nationality="Nigerian", address="12 Admiralty Way")
        refused = self.patch(
            self.admin, "student-detail", {"address": ""}, pk=row.pk,
        )
        self.assertEqual(refused.status_code, 400, refused.data)
        self.assertIn("address", refused.data["error"]["detail"])

    def test_an_edit_that_does_not_touch_them_passes_on_an_old_record(self):
        """Chiamaka was enrolled before the rule, with neither field."""
        row = self.student()
        response = self.patch(
            self.admin, "student-detail", {"first_name": "Chiamaka Ada"}, pk=row.pk,
        )
        self.assertEqual(response.status_code, 200, response.data)

    def test_an_import_row_without_a_required_column_value_is_refused(self):
        from ..imports import resolve_row

        payload = {
            "first_name": "Ada", "last_name": "Obi",
            "date_of_birth": years_ago(9).isoformat(), "gender": "Female",
            "branch": "Lekki", "guardian_first_name": "Ngozi",
            "guardian_last_name": "Obi", "guardian_phone": "08035550101",
        }
        row = resolve_row(
            payload, tenant=self.tenant, session=self.year,
            batch_branch=None, multi_branch=True,
        )
        address = [i for i in row.issues if i.field == "address"]
        self.assertEqual(len(address), 1)
        self.assertEqual(address[0].severity, "error")
        self.assertEqual(address[0].message, "Home address is required at this school.")
        # Nationality has no column in the template, so no row is refused for it.
        self.assertFalse([i for i in row.issues if i.field == "nationality"])

        row = resolve_row(
            {**payload, "address": "12 Admiralty Way"}, tenant=self.tenant,
            session=self.year, batch_branch=None, multi_branch=True,
        )
        self.assertTrue(row.ok, row.issues)


class CapacityModeTests(_SettingsFixture):
    """JSS1 B at Lekki holds two, and both seats are taken."""

    def setUp(self):
        for first in ("Amaka", "Bisi"):
            self.place(self.student(first=first, last="Full"), self.lekki_class)
        self.newcomer = self.student(
            first="Chidi", last="New", status=StudentStatus.ENROLLED,
        )

    def assign(self, *, acknowledged):
        return self.post(
            self.admin, "student-assign-class",
            {"school_class": self.lekki_class.pk,
             "allow_over_capacity": acknowledged},
            pk=self.newcomer.pk,
        )

    def test_warn_refuses_until_acknowledged(self):
        refused = self.assign(acknowledged=False)
        self.assertEqual(refused.status_code, 422, refused.data)
        self.assertEqual(refused.data["error"]["code"], "CLASS_AT_CAPACITY")
        self.assertEqual(self.assign(acknowledged=True).status_code, 200)

    def test_hard_refuses_even_when_acknowledged(self):
        self.set_rules(capacity_mode="HARD")
        refused = self.assign(acknowledged=True)
        self.assertEqual(refused.status_code, 422, refused.data)
        self.assertEqual(refused.data["error"]["code"], "CLASS_FULL")
        self.assertEqual(
            refused.data["message"],
            "JSS1 B holds 2 of 2 seats, and this school does not put classes "
            "over capacity.",
        )
        self.assertEqual(
            refused.data["error"]["detail"],
            {"school_class": self.lekki_class.pk, "capacity": 2, "used": 2,
             "adding": 1},
        )
        self.assertFalse(
            ClassEnrolment.all_objects.filter(student=self.newcomer).exists(),
        )

    def test_off_checks_nothing(self):
        self.set_rules(capacity_mode="OFF")
        response = self.assign(acknowledged=False)
        self.assertEqual(response.status_code, 200, response.data)

    def test_enrolment_follows_the_same_rule(self):
        self.set_rules(capacity_mode="HARD")
        refused = self.post(self.admin, "student-list", self.enrolment_body(
            school_class=self.lekki_class.pk, allow_over_capacity=True,
        ))
        self.assertEqual(refused.status_code, 422, refused.data)
        self.assertEqual(refused.data["error"]["code"], "CLASS_FULL")
        self.assertFalse(Student.all_objects.filter(first_name="Zainab").exists())

    def bulk(self, *, acknowledged):
        extra = self.student(first="Dayo", last="New", status=StudentStatus.ENROLLED)
        return self.post(self.admin, "student-bulk-assign", {
            "student_ids": [self.newcomer.pk, extra.pk],
            "school_class": self.lekki_class.pk,
            "allow_over_capacity": acknowledged,
        })

    def test_bulk_assign_under_hard_refuses_the_whole_selection(self):
        self.set_rules(capacity_mode="HARD")
        refused = self.bulk(acknowledged=True)
        self.assertEqual(refused.status_code, 422, refused.data)
        self.assertEqual(refused.data["error"]["code"], "CLASS_FULL")
        self.assertEqual(
            ClassEnrolment.all_objects.filter(school_class=self.lekki_class).count(),
            2,
        )

    def test_bulk_assign_under_warn_and_off(self):
        refused = self.bulk(acknowledged=False)
        self.assertEqual(refused.data["error"]["code"], "CLASS_AT_CAPACITY")
        self.set_rules(capacity_mode="OFF")
        extra = self.student(first="Efe", last="New", status=StudentStatus.ENROLLED)
        allowed = self.post(self.admin, "student-bulk-assign", {
            "student_ids": [self.newcomer.pk, extra.pk],
            "school_class": self.lekki_class.pk,
        })
        self.assertEqual(allowed.status_code, 200, allowed.data)
        self.assertEqual(allowed.data["data"]["assigned"], 2)

    def test_a_single_branch_school_keeps_its_own_mode(self):
        """Brightfield going HARD leaves Sunrise on WARN."""
        from ..services.rules import capacity_mode

        self.set_rules(capacity_mode="HARD")
        self.assertEqual(capacity_mode(self.solo.tenant), "WARN")


class PromotionCapacityModeTests(_SettingsFixture):
    """Next year's JSS2 A holds one seat and two pupils are promoted into it."""

    def setUp(self):
        from schools.vs_academics.models import Level, SchoolClass

        next_jss2 = Level.all_objects.create(
            tenant=self.tenant, program=self.program, session=self.next_year,
            name="JSS2", code="JSS2", order_index=2,
        )
        Level.all_objects.create(
            tenant=self.tenant, program=self.program, session=self.next_year,
            name="JSS1", code="JSS1", order_index=1, next_level=next_jss2,
        )
        SchoolClass.all_objects.create(
            tenant=self.tenant, level=next_jss2, session=self.next_year,
            name="JSS2 A", code="N-JSS2A", arm="A", capacity=1,
        )
        for first in ("Chiamaka", "Tunde"):
            self.place(self.student(first=first, last="Mover"), self.shared_class)

    def preview(self):
        response = self.post(self.admin, "student-promotion-preview", {
            "to_session": self.next_year.pk,
        })
        self.assertEqual(response.status_code, 200, response.data)
        return response.data["data"]

    def run_promotion(self, **body):
        return self.post(self.admin, "student-promotion-run", {
            "to_session": self.next_year.pk, **body,
        })

    def test_the_preview_names_the_schools_mode(self):
        self.assertEqual(self.preview()["capacity_mode"], "WARN")
        self.set_rules(capacity_mode="HARD")
        self.assertEqual(self.preview()["capacity_mode"], "HARD")

    def test_warn_waits_for_an_acknowledgement(self):
        refused = self.run_promotion()
        self.assertEqual(refused.data["error"]["code"], "PROMOTION_OVER_CAPACITY")
        self.assertEqual(self.run_promotion(allow_over_capacity=True).status_code, 201)

    def test_hard_refuses_even_when_acknowledged_and_moves_nobody(self):
        self.set_rules(capacity_mode="HARD")
        refused = self.run_promotion(allow_over_capacity=True)
        self.assertEqual(refused.status_code, 422, refused.data)
        self.assertEqual(refused.data["error"]["code"], "CLASS_FULL")
        classes = refused.data["error"]["detail"]["classes"]
        self.assertEqual(
            (classes[0]["class_name"], classes[0]["capacity"], classes[0]["adding"]),
            ("JSS2 A", 1, 2),
        )
        self.assertFalse(
            ClassEnrolment.all_objects.filter(session=self.next_year).exists(),
        )

    def test_off_lists_nothing_and_runs(self):
        self.set_rules(capacity_mode="OFF")
        self.assertEqual(self.preview()["over_capacity"], [])
        response = self.run_promotion()
        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(response.data["data"]["promoted"], 2)


class ImportCapacityModeTests(_SettingsFixture):
    """A file naming JSS1 B, already full, for one more child."""

    def run_check(self, mode):
        from ..imports import _check_capacity

        for first in ("Amaka", "Bisi"):
            self.place(self.student(first=first, last="Full"), self.lekki_class)
        found = []
        _check_capacity(
            lambda row_number, issue: found.append(issue),
            self.lekki_class, self.year, [1], 1, set(), mode=mode,
        )
        return found

    def test_warn_warns(self):
        found = self.run_check("WARN")
        self.assertEqual([i.severity for i in found], ["warning"])

    def test_hard_refuses_the_row_before_anything_is_written(self):
        found = self.run_check("HARD")
        self.assertEqual([i.severity for i in found], ["error"])
        self.assertIn("does not put classes over capacity", found[0].message)

    def test_off_says_nothing(self):
        self.assertEqual(self.run_check("OFF"), [])


# ── admission numbers per branch, and automatic numbers ─────────────────────

class BranchAdmissionPolicyTests(_SettingsFixture):
    """Brightfield numbers CSS-24-NNNN; its Ikeja branch numbers IKJ/NNNN."""

    def policy(self, branch=None, user=None):
        params = {"branch": branch.pk} if branch is not None else {}
        response = self.get(user or self.admin, "student-admission-policy", params)
        self.assertEqual(response.status_code, 200, response.data)
        return response.data["data"]

    def put_policy(self, body, branch=None):
        from django.urls import reverse

        path = f"{reverse('student-admission-policy')}?tenant={self.tenant.slug}"
        if branch is not None:
            path += f"&branch={branch.pk}"
        response = self.client_for(self.admin).put(path, body, format="json")
        self.assertEqual(response.status_code, 200, response.data)
        return response.data["data"]

    def delete_policy(self, branch=None):
        from django.urls import reverse

        path = f"{reverse('student-admission-policy')}?tenant={self.tenant.slug}"
        if branch is not None:
            path += f"&branch={branch.pk}"
        return self.client_for(self.admin).delete(path)

    def setUp(self):
        self.put_policy({
            "required": True, "pattern": r"CSS-\d{2}-\d{4}",
            "hint": "Use CSS-YY-NNNN.",
        })

    def enrol_at(self, branch, number):
        return self.post(self.admin, "student-list", self.enrolment_body(
            branch=str(branch.pk), student_number=number,
        ))

    def test_the_body_says_where_the_rule_comes_from(self):
        fresh = self.get(self.solo_admin, "student-admission-policy").data["data"]
        self.assertEqual(
            fresh,
            {"required": False, "pattern": "", "hint": "", "auto_issue": False,
             "source": "default", "suggestion": ""},
        )
        self.assertEqual(self.policy()["source"], "school")
        self.assertEqual(self.policy(self.ikeja)["source"], "school")

    def test_a_branch_rule_overrides_the_schools_for_its_students_only(self):
        body = self.put_policy(
            {"required": True, "pattern": r"IKJ/\d{4}", "hint": "Use IKJ/NNNN."},
            branch=self.ikeja,
        )
        self.assertEqual(body["source"], "branch")
        self.assertEqual(body["pattern"], r"IKJ/\d{4}")
        self.assertEqual(self.policy(self.lekki)["pattern"], r"CSS-\d{2}-\d{4}")
        self.assertEqual(self.policy()["source"], "school")

        refused = self.enrol_at(self.ikeja, "CSS-24-0117")
        self.assertEqual(refused.status_code, 422, refused.data)
        self.assertEqual(refused.data["message"], "Use IKJ/NNNN.")
        self.assertEqual(self.enrol_at(self.ikeja, "IKJ/0001").status_code, 201)
        wrong = self.post(self.admin, "student-list", self.enrolment_body(
            first_name="Musa", student_number="IKJ/0002",
        ))
        self.assertEqual(wrong.status_code, 422, wrong.data)

    def test_a_branch_rule_is_whole_so_an_empty_pattern_means_none(self):
        """Ikeja takes any shape although the school insists on CSS-YY-NNNN."""
        body = self.put_policy(
            {"required": False, "pattern": "", "hint": ""}, branch=self.ikeja,
        )
        self.assertEqual((body["source"], body["pattern"]), ("branch", ""))
        self.assertEqual(self.enrol_at(self.ikeja, "ANYTHING-7").status_code, 201)

    def test_delete_resets_the_branch_to_the_schools_rule(self):
        self.put_policy(
            {"required": True, "pattern": r"IKJ/\d{4}", "hint": "Use IKJ/NNNN."},
            branch=self.ikeja,
        )
        response = self.delete_policy(self.ikeja)
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["data"]["source"], "school")
        self.assertEqual(response.data["data"]["pattern"], r"CSS-\d{2}-\d{4}")
        self.assertEqual(self.enrol_at(self.ikeja, "CSS-24-0117").status_code, 201)

    def test_delete_without_a_branch_is_refused(self):
        response = self.delete_policy()
        self.assertEqual(response.status_code, 400, response.data)
        self.assertIn("branch", response.data["error"]["detail"])
        self.assertEqual(self.policy()["source"], "school")

    def test_confirmation_uses_the_applicants_branch_rule(self):
        self.put_policy(
            {"required": True, "pattern": r"IKJ/\d{4}", "hint": "Use IKJ/NNNN."},
            branch=self.ikeja,
        )
        applicant = self.student(
            branch=self.ikeja, status=StudentStatus.APPLICANT, first="Kemi",
        )
        refused = self.post(
            self.admin, "student-confirm", {"student_number": "CSS-24-0001"},
            pk=applicant.pk,
        )
        self.assertEqual(refused.status_code, 422, refused.data)
        allowed = self.post(
            self.admin, "student-confirm", {"student_number": "IKJ/0001"},
            pk=applicant.pk,
        )
        self.assertEqual(allowed.status_code, 200, allowed.data)

    def test_the_status_routes_confirm_under_the_same_rule(self):
        """Ikeja requires a number; the status drawer and bulk bar cannot skip it."""
        self.put_policy(
            {"required": True, "pattern": r"IKJ/\d{4}", "hint": "Use IKJ/NNNN."},
            branch=self.ikeja,
        )
        kemi = self.student(
            branch=self.ikeja, status=StudentStatus.APPLICANT, first="Kemi",
        )
        tobi = self.student(
            branch=self.ikeja, status=StudentStatus.APPLICANT, first="Tobi",
        )
        one = self.post(
            self.admin, "student-status",
            {"to_status": "ENROLLED", "reason": "Offer accepted."}, pk=kemi.pk,
        )
        self.assertEqual(one.status_code, 422, one.data)
        self.assertEqual(one.data["error"]["code"], "ADMISSION_NUMBER_REQUIRED")

        bulk = self.post(self.admin, "student-bulk-status", {
            "student_ids": [tobi.pk], "to_status": "ENROLLED",
            "reason": "Offer accepted.",
        })
        self.assertEqual(bulk.status_code, 200, bulk.data)
        row = bulk.data["data"]["results"][0]
        self.assertFalse(row["ok"])
        self.assertEqual(row["code"], "ADMISSION_NUMBER_REQUIRED")
        for child in (kemi, tobi):
            child.refresh_from_db()
            self.assertEqual(child.status, StudentStatus.APPLICANT)

    def test_an_import_row_uses_its_branchs_rule(self):
        from ..imports import resolve_row

        self.put_policy(
            {"required": True, "pattern": r"IKJ/\d{4}", "hint": "Use IKJ/NNNN."},
            branch=self.ikeja,
        )
        payload = {
            "first_name": "Ada", "last_name": "Obi",
            "date_of_birth": years_ago(9).isoformat(), "gender": "Female",
            "student_number": "IKJ/0005", "guardian_first_name": "Ngozi",
            "guardian_last_name": "Obi", "guardian_phone": "08035550101",
        }
        at_ikeja = resolve_row(
            {**payload, "branch": "Ikeja"}, tenant=self.tenant, session=self.year,
            batch_branch=None, multi_branch=True,
        )
        self.assertTrue(at_ikeja.ok, at_ikeja.issues)
        at_lekki = resolve_row(
            {**payload, "branch": "Lekki"}, tenant=self.tenant, session=self.year,
            batch_branch=None, multi_branch=True,
        )
        self.assertEqual(
            [i.field for i in at_lekki.issues if i.severity == "error"],
            ["student_number"],
        )

    def test_numbers_stay_unique_across_branches(self):
        self.put_policy(
            {"required": False, "pattern": "", "hint": ""}, branch=self.ikeja,
        )
        self.assertEqual(self.enrol_at(self.lekki, "CSS-24-0117").status_code, 201)
        clash = self.post(self.admin, "student-list", self.enrolment_body(
            first_name="Musa", branch=str(self.ikeja.pk),
            student_number="css-24-0117",
        ))
        self.assertEqual(clash.status_code, 409, clash.data)


class AutoIssueTests(_SettingsFixture):
    """Brightfield issues BFS/2025/NNNN numbers automatically."""

    def setUp(self):
        from ..services.policy import write_policy

        write_policy(
            self.tenant, self.admin, required=True, pattern=r"BFS/\d{4}/\d{4}",
            hint="Use BFS/YYYY/NNNN.", auto_issue=True,
        )
        self.student(first="Old", last="Hand", number="BFS/2025/0142")

    def enrol(self, **overrides):
        return self.post(self.admin, "student-list", self.enrolment_body(**overrides))

    def test_a_blank_number_is_issued_at_enrolment(self):
        response = self.enrol()
        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(response.data["data"]["student_number"], "BFS/2025/0143")

    def test_a_typed_number_is_kept(self):
        response = self.enrol(student_number="BFS/2025/0900")
        self.assertEqual(response.data["data"]["student_number"], "BFS/2025/0900")

    def test_saving_an_applicant_issues_nothing_and_confirming_does(self):
        response = self.enrol(as_applicant=True, applied_for=self.jss1.pk)
        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(response.data["data"]["student_number"], "")

        confirmed = self.post(
            self.admin, "student-confirm", {}, pk=response.data["data"]["id"],
        )
        self.assertEqual(confirmed.status_code, 200, confirmed.data)
        self.assertEqual(
            Student.all_objects.get(pk=response.data["data"]["id"]).student_number,
            "BFS/2025/0143",
        )

    def test_a_number_taken_by_another_enrolment_is_skipped(self):
        """Another registrar takes 0143 between the suggestion and the save."""
        from ..services import enrolment as enrolment_service

        real = enrolment_service.suggest_number
        raced = []

        def racing(tenant, **kwargs):
            candidate = real(tenant, **kwargs)
            if not raced:
                raced.append(candidate)
                self.student(first="Quick", last="Registrar", number=candidate)
            return candidate

        with mock.patch.object(enrolment_service, "suggest_number", racing):
            response = self.enrol()
        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(raced, ["BFS/2025/0143"])
        self.assertEqual(response.data["data"]["student_number"], "BFS/2025/0144")

    def test_a_collision_at_the_constraint_is_retried(self):
        """The other enrolment commits after the free check, so the insert fails."""
        from ..services import enrolment as enrolment_service

        real = enrolment_service.suggest_number
        calls = []

        def racing(tenant, **kwargs):
            candidate = real(tenant, **kwargs)
            if not calls:
                self.student(first="Quick", last="Registrar", number=candidate)
            calls.append(candidate)
            return candidate

        with mock.patch.object(enrolment_service, "suggest_number", racing), \
                mock.patch.object(
                    enrolment_service, "assert_number_free",
                    side_effect=lambda tenant, number, **kw: number,
                ):
            response = self.enrol()
        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(calls, ["BFS/2025/0143", "BFS/2025/0144"])
        self.assertEqual(response.data["data"]["student_number"], "BFS/2025/0144")

    def test_with_no_series_a_required_rule_still_refuses(self):
        Student.all_objects.filter(student_number="BFS/2025/0142").update(
            student_number="",
        )
        response = self.enrol()
        self.assertEqual(response.status_code, 422, response.data)
        self.assertEqual(response.data["error"]["code"], "ADMISSION_NUMBER_REQUIRED")

    def test_with_no_series_an_optional_rule_leaves_it_blank(self):
        from ..services.policy import write_policy

        write_policy(self.tenant, self.admin, required=False)
        Student.all_objects.filter(student_number="BFS/2025/0142").update(
            student_number="",
        )
        response = self.enrol()
        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(response.data["data"]["student_number"], "")

    def test_a_branch_rule_issues_from_its_own_series(self):
        """Ikeja's IKJ numbers continue although a CSS number was issued later."""
        from ..services.policy import write_policy

        write_policy(
            self.tenant, self.admin, branch=self.ikeja, required=True,
            pattern=r"IKJ/\d{4}", hint="Use IKJ/NNNN.", auto_issue=True,
        )
        self.student(branch=self.ikeja, first="Ike", last="One", number="IKJ/0007")
        self.student(first="Later", last="Lekki", number="BFS/2025/0150")
        response = self.enrol(branch=str(self.ikeja.pk))
        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(response.data["data"]["student_number"], "IKJ/0008")

    def test_a_single_branch_school_issues_too(self):
        from ..services.policy import write_policy
        from ..services.enrolment import confirm_applicant

        write_policy(self.solo.tenant, self.solo_admin, auto_issue=True)
        self.student(
            tenant=self.solo.tenant, branch=self.solo_branch, first="S",
            last="One", number="SUN-0009",
        )
        applicant = self.student(
            tenant=self.solo.tenant, branch=self.solo_branch, first="S",
            last="Two", status=StudentStatus.APPLICANT,
        )
        confirm_applicant(applicant, actor=self.solo_admin)
        applicant.refresh_from_db()
        self.assertEqual(applicant.student_number, "SUN-0010")


class SettingsCatalogueTests(_SettingsFixture):
    """The definitions the migration and the seeder both declare."""

    def test_every_key_exists_with_todays_default_and_its_scopes(self):
        from vs_config.models import ConfigurationDefinition

        expected = {
            "students.age.min_years": (2, ["platform", "school"]),
            "students.age.max_years": (25, ["platform", "school"]),
            "students.documents.required": (
                ["BIRTH_CERTIFICATE"], ["platform", "school"],
            ),
            "students.enrolment.required_fields": ([], ["platform", "school"]),
            "students.capacity.mode": ("WARN", ["platform", "school"]),
            "students.capacity.default": (None, ["platform", "school"]),
            "students.admission_number.auto_issue": (
                False, ["branch", "platform", "school"],
            ),
        }
        for key, (default, scopes) in expected.items():
            with self.subTest(key=key):
                row = ConfigurationDefinition.objects.get(key=key)
                self.assertEqual(row.default_value, default)
                self.assertEqual(sorted(row.allowed_scopes), scopes)
        for key in (
            "students.admission_number.required",
            "students.admission_number.pattern",
            "students.admission_number.hint",
        ):
            with self.subTest(key=key):
                self.assertIn(
                    "branch",
                    ConfigurationDefinition.objects.get(key=key).allowed_scopes,
                )

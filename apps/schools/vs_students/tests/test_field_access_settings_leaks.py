"""Settings and audit payloads that restate a student field a role may not read.

Four places carried a student value past its switch, and each is closed at the
one place all its readers share:

* the enrolment rules offer the student fields a school can make mandatory, by
  label, and offered all of them to a caller who may read only some;
* a field the school makes mandatory (and the guardian email it may demand)
  cannot be supplied by a caller who may not write it, so the requirement does
  not bind that caller: the child is enrolled without the value and somebody
  with Write completes it later;
* the admission number rule's suggestion is the next number after the highest
  one issued, so it follows the student number switch;
* a record edit stores the profile and each changed value in the audit row,
  which a reader sees only for the fields they may read.

Brightfield has two branches and Sunrise one; a switch belongs to a role and a
role to one school, so nothing one school closes reaches the other.
"""
from __future__ import annotations

from rest_framework.test import APIRequestFactory

from vs_audit.models import AuditEvent
from vs_audit.serializers import AuditEventDetailSerializer
from vs_rbac.tests.helpers import (
    install_declared_fields,
    make_assignment,
    make_role,
    make_role_permission,
    make_school_admin,
    set_field_access,
)

from ..constants import REQUIRABLE_FIELDS, StudentStatus
from ..field_access import STATUS_REASON_FIELD
from ..models import Guardian, Student, StudentStatusLog
from ..services.guardian_rules import write_guardian_rules
from ..services.rules import write_rules
from .test_field_write_paths import OFFICER_KEYS, _WritePathFixture

NATIONALITY = "school.students.nationality"
BLOOD_GROUP = "school.students.blood_group"
PHONE = "school.students.phone"
LAST_NAME = "school.students.last_name"
STUDENT_NUMBER = "school.students.student_number"
GUARDIAN_EMAIL = "school.guardians.email"


class _Fixture(_WritePathFixture):
    """Three callers per school: one open, one read-only, one blind.

    The read-only caller reads a nationality and a guardian's email and may
    not write either. The blind caller reads neither. Both hold exactly the
    keys an admissions officer holds. The school requires a nationality and a
    home address of every pupil and an email of every guardian.
    """

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        install_declared_fields("school.students", "school.guardians")
        cls.callers = {}
        for label, school, admin in (
            ("multi", cls.school, cls.admin), ("single", cls.solo, cls.solo_admin),
        ):
            cls.callers[label] = {
                "open": cls.caller(school, label, "open"),
                "read_only": cls.caller(
                    school, label, "read-only",
                    read_only=(NATIONALITY, GUARDIAN_EMAIL),
                ),
                "blind": cls.caller(
                    school, label, "blind",
                    closed=(NATIONALITY, BLOOD_GROUP, GUARDIAN_EMAIL, STUDENT_NUMBER),
                ),
            }
            write_rules(
                school.tenant, admin, min_age_years=2, max_age_years=25,
                required_documents=[], required_fields=["nationality", "address"],
                capacity_mode="WARN", default_capacity=None,
            )
            write_guardian_rules(
                school.tenant, admin, min_per_student=1, email_required=True,
                matching="EMAIL_THEN_PHONE", extra_relationships=[],
            )

    @classmethod
    def caller(cls, school, label, name, *, closed=(), read_only=()):
        role = make_role(school, name=f"{name} {label}", key=f"leaks_{name}_{label}")
        for key in OFFICER_KEYS + ("school.students.update",):
            make_role_permission(role, cls.permissions[key])
        if closed:
            set_field_access(role, *closed, read=False, write=False)
        if read_only:
            set_field_access(role, *read_only, read=True, write=False)
        user = make_school_admin(
            None, email=f"{name}.{label}@leaks.test", tenant=school.tenant,
        )
        make_assignment(school, user, role, branch=None)
        return user

    def rules(self, user, name="student-enrolment-rules"):
        response = self.get(user, name)
        self.assertEqual(response.status_code, 200, response.data)
        return response.data["data"]

    def enrol(self, label, who, **overrides):
        build = dict((l, b) for l, _, b in self.shapes())[label]
        return self.post(self.callers[label][who], "student-list", build(**overrides))


class OptionalFieldsTests(_Fixture):
    def test_the_choices_a_caller_is_offered_are_the_fields_they_may_read(self):
        for label in ("multi", "single"):
            with self.subTest(school=label):
                everything = set(REQUIRABLE_FIELDS)

                def offered(user):
                    return {f["value"] for f in self.rules(user)["optional_fields"]}

                # Blood group is sensitive, so only the nurse reads it.
                self.assertEqual(offered(self.people[label]["nurse"]), everything)
                self.assertEqual(
                    offered(self.callers[label]["open"]), everything - {"blood_group"},
                )
                self.assertEqual(
                    offered(self.callers[label]["read_only"]), everything - {"blood_group"},
                )
                self.assertEqual(
                    offered(self.callers[label]["blind"]),
                    everything - {"nationality", "blood_group"},
                )

    def test_a_label_goes_with_its_field(self):
        offered = self.rules(self.callers["multi"]["blind"])["optional_fields"]
        labels = {f["label"] for f in offered}
        self.assertNotIn(REQUIRABLE_FIELDS["nationality"], labels)
        self.assertNotIn(REQUIRABLE_FIELDS["blood_group"], labels)
        self.assertIn(REQUIRABLE_FIELDS["address"], labels)

    def test_a_switch_one_school_closes_does_not_close_the_other(self):
        # Sunrise's open caller keeps nationality although Brightfield's blind
        # caller lost it.
        self.assertIn(
            "nationality",
            {f["value"] for f in self.rules(self.callers["single"]["open"])["optional_fields"]},
        )


class RequirementsYieldTests(_Fixture):
    def test_the_payload_names_the_requirements_that_bind_the_caller(self):
        for label in ("multi", "single"):
            for who, binds in (
                ("open", ["nationality", "address"]),
                ("read_only", ["address"]),
                ("blind", ["address"]),
            ):
                with self.subTest(school=label, caller=who):
                    data = self.rules(self.callers[label][who])
                    self.assertEqual(data["required_fields"], ["nationality", "address"])
                    self.assertEqual(data["binding_required_fields"], binds)
                    guardians = self.rules(self.callers[label][who], "student-guardian-rules")
                    self.assertTrue(guardians["email_required"])
                    self.assertEqual(guardians["binding_email_required"], who == "open")

    def test_a_caller_who_cannot_write_a_required_field_enrols_the_child_without_it(self):
        for label, tenant, _ in self.shapes():
            for who in ("read_only", "blind"):
                with self.subTest(school=label, caller=who):
                    name = f"Yield{label}{who}".replace("_", "")
                    response = self.enrol(
                        label, who, first_name=name, address="5 Allen Avenue",
                        guardians=[{
                            "full_name": "Mrs. Ade Bello", "phone": "08025550199",
                            "relationship": "MOTHER", "is_primary": True,
                        }],
                    )
                    self.assertEqual(response.status_code, 201, response.data)
                    child = Student.all_objects.get(tenant=tenant, first_name=name)
                    self.assertEqual(child.nationality, "")
                    self.assertEqual(child.address, "5 Allen Avenue")
                    self.assertEqual(
                        Guardian.all_objects.get(tenant=tenant, phone="08025550199").email, "",
                    )

    def test_the_requirement_still_binds_a_caller_who_can_write_the_field(self):
        for label, tenant, _ in self.shapes():
            with self.subTest(school=label):
                before = self.counts(tenant)
                response = self.enrol(
                    label, "open", address="5 Allen Avenue",
                    guardians=[{
                        "full_name": "Mrs. Ade Bello", "phone": "08025550198",
                        "email": "ade@example.ng", "relationship": "MOTHER",
                        "is_primary": True,
                    }],
                )
                self.assertEqual(response.status_code, 400, response.data)
                self.assertEqual(self.refused_fields(response), {"nationality"})
                self.assertEqual(self.counts(tenant), before)

    def test_a_requirement_the_caller_can_meet_is_not_waived_with_the_others(self):
        for label, tenant, _ in self.shapes():
            with self.subTest(school=label):
                response = self.enrol(
                    label, "read_only", guardians=[{
                        "full_name": "Mrs. Ade Bello", "phone": "08025550197",
                        "relationship": "MOTHER", "is_primary": True,
                    }],
                )
                self.assertEqual(response.status_code, 400, response.data)
                self.assertEqual(self.refused_fields(response), {"address"})

    def test_a_guardian_email_is_still_required_of_a_caller_who_can_write_it(self):
        for label, tenant, _ in self.shapes():
            with self.subTest(school=label):
                response = self.enrol(
                    label, "open", nationality="Nigerian", address="5 Allen Avenue",
                    guardians=[{
                        "full_name": "Mrs. Ade Bello", "phone": "08025550196",
                        "relationship": "MOTHER", "is_primary": True,
                    }],
                )
                self.assertEqual(response.status_code, 400, response.data)
                self.assertEqual(self.refused_fields(response), {"guardians"})

    def test_a_write_the_caller_may_not_make_is_still_refused_rather_than_waived(self):
        for label, tenant, _ in self.shapes():
            with self.subTest(school=label):
                before = self.counts(tenant)
                response = self.enrol(
                    label, "read_only", nationality="Nigerian", address="5 Allen Avenue",
                )
                self.assertFieldWriteDenied(response)
                self.assertEqual(self.counts(tenant), before)

    def test_linking_a_guardian_to_a_record_follows_the_same_rule(self):
        for label, tenant, _ in self.shapes():
            child = self.student(
                tenant=tenant, branch=self.lekki if label == "multi" else self.solo_branch,
                first="Linkee", last=f"Of{label}",
            )
            for who, expected in (("read_only", 201), ("open", 400)):
                with self.subTest(school=label, caller=who):
                    response = self.post(
                        self.callers[label][who], "student-guardians",
                        {
                            "full_name": "Mr. Kunle Adebayo",
                            "phone": f"0802555{'1' if who == 'open' else '2'}{label[0]}00",
                            "relationship": "FATHER",
                        },
                        pk=child.pk,
                    )
                    self.assertEqual(response.status_code, expected, response.data)

    def test_an_edit_still_refuses_blanking_a_required_field_it_may_write(self):
        child = self.student(address="1 Old Road", first="Edited", last="Pupil")
        response = self.patch(
            self.callers["multi"]["open"], "student-detail", {"address": ""}, pk=child.pk,
        )
        self.assertEqual(response.status_code, 400, response.data)


class NumberSuggestionTests(_Fixture):
    def test_the_suggestion_follows_the_student_number_switch(self):
        for label in ("multi", "single"):
            for who, present in (("open", True), ("read_only", True), ("blind", False)):
                with self.subTest(school=label, caller=who):
                    data = self.rules(self.callers[label][who], "student-admission-policy")
                    self.assertEqual("suggestion" in data, present, data)
                    self.assertIn("pattern", data)


class _AuditFixture(_Fixture):
    """A pupil edited by an open caller, and readers of the trail."""

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.readers = {}
        for label, school in (("multi", cls.school), ("single", cls.solo)):
            cls.readers[label] = {
                "open": cls.caller(school, label, "audit-open"),
                "closed": cls.caller(
                    school, label, "audit-closed", closed=(PHONE, LAST_NAME),
                ),
            }
        # The editor also reads the reason behind a suspension, so the stored
        # snapshot holds it.
        role = make_role(cls.school, name="Counsellor editor", key="counsellor_editor")
        for key in OFFICER_KEYS + ("school.students.update",):
            make_role_permission(role, cls.permissions[key])
        set_field_access(role, STATUS_REASON_FIELD, read=True, write=False)
        cls.counsellor = make_school_admin(
            None, email="counsellor.editor@leaks.test", tenant=cls.tenant,
        )
        make_assignment(cls.school, cls.counsellor, role, branch=None)

    def edited(self, user, tenant, branch, **changes):
        child = self.student(
            tenant=tenant, branch=branch, first="Audited", last="Pupil",
            phone="08011112222",
        )
        response = self.patch(user, "student-detail", changes, pk=child.pk)
        self.assertEqual(response.status_code, 200, response.data)
        event = AuditEvent.objects.filter(
            entity_type="Student", entity_id=str(child.pk), action_type="UPDATE",
        ).latest("event_at")
        return child, event

    def render(self, event, user):
        request = APIRequestFactory().get("/")
        request.user = user
        request.tenant = user.tenant
        return AuditEventDetailSerializer(event, context={"request": request}).data


class StudentUpdateAuditTests(_AuditFixture):
    def test_a_reader_sees_only_the_fields_they_may_read(self):
        for label, tenant, branch, editor in (
            ("multi", self.tenant, self.lekki, self.callers["multi"]["open"]),
            ("single", self.solo.tenant, self.solo_branch, self.callers["single"]["open"]),
        ):
            with self.subTest(school=label):
                _, event = self.edited(
                    editor, tenant, branch, phone="08033334444", last_name="Okafor",
                    address="9 New Road",
                )
                open_view = self.render(event, self.readers[label]["open"])
                self.assertEqual(open_view["before_data"]["phone"], "08011112222")
                self.assertEqual(open_view["diff_data"]["phone"]["to"], "08033334444")
                self.assertEqual(open_view["diff_data"]["last_name"]["to"], "Okafor")
                self.assertIn("full_name", open_view["diff_data"])

                closed_view = self.render(event, self.readers[label]["closed"])
                for snapshot in ("before_data", "diff_data"):
                    for hidden in ("phone", "last_name", "full_name"):
                        self.assertNotIn(hidden, closed_view[snapshot], snapshot)
                self.assertEqual(closed_view["diff_data"]["address"]["to"], "9 New Road")
                self.assertEqual(closed_view["before_data"]["first_name"], "Audited")
                self.assertNotIn("08033334444", str(closed_view))
                self.assertNotIn("08011112222", str(closed_view))

    def test_the_pupils_name_stays_as_the_rows_label(self):
        _, event = self.edited(
            self.callers["multi"]["open"], self.tenant, self.lekki, last_name="Okafor",
        )
        closed_view = self.render(event, self.readers["multi"]["closed"])
        self.assertEqual(closed_view["entity_label"], "Audited Okafor")
        self.assertIn("Audited Okafor", closed_view["summary"])

    def test_a_derived_value_follows_the_field_it_is_made_from(self):
        _, event = self.edited(
            self.callers["multi"]["open"], self.tenant, self.lekki, address="9 New Road",
        )
        self.assertIn("age", event.before_data)
        role = make_role(self.school, name="No birth dates", key="no_birth_dates")
        set_field_access(role, "school.students.date_of_birth", read=False, write=False)
        reader = make_school_admin(None, email="nodob@leaks.test", tenant=self.tenant)
        make_assignment(self.school, reader, role, branch=None)
        view = self.render(event, reader)
        self.assertNotIn("age", view["before_data"])
        self.assertNotIn("date_of_birth", view["before_data"])

    def test_the_reason_inside_a_suspension_block_follows_its_switch(self):
        child = self.student(
            first="Suspended", last="Pupil", status=StudentStatus.SUSPENDED,
        )
        StudentStatusLog.all_objects.create(
            tenant=self.tenant, student=child,
            from_status=StudentStatus.ACTIVE, to_status=StudentStatus.SUSPENDED,
            reason="Fighting in the dining hall.", effective_date="2026-03-09",
        )
        self.assertEqual(
            self.patch(self.counsellor, "student-detail", {"address": "2 Close"}, pk=child.pk)
            .status_code, 200,
        )
        event = AuditEvent.objects.filter(
            entity_type="Student", entity_id=str(child.pk), action_type="UPDATE",
        ).latest("event_at")
        self.assertEqual(
            event.before_data["suspension"]["reason"], "Fighting in the dining hall.",
        )

        shown = self.render(event, self.counsellor)
        self.assertEqual(
            shown["before_data"]["suspension"]["reason"], "Fighting in the dining hall.",
        )
        hidden = self.render(event, self.readers["multi"]["open"])
        self.assertNotIn("reason", hidden["before_data"]["suspension"])
        self.assertIn("effective_date", hidden["before_data"]["suspension"])
        self.assertNotIn("Fighting", str(hidden))
        # The stored row is untouched.
        event.refresh_from_db()
        self.assertIn("reason", event.before_data["suspension"])

    def test_another_schools_reader_is_not_affected_by_a_switch_closed_here(self):
        _, event = self.edited(
            self.callers["single"]["open"], self.solo.tenant, self.solo_branch,
            phone="08033334444",
        )
        view = self.render(event, self.readers["single"]["open"])
        self.assertEqual(view["diff_data"]["phone"]["to"], "08033334444")

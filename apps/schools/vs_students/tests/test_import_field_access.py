"""Spreadsheet imports of students and guardians apply the runner's Field Access.

A school's roll and its household list reach ``school.students`` and
``school.guardians`` without a serializer, so neither the enrol form's guard
nor the edit form's ever saw them. Both imports now ask the same questions of
each non-blank cell: a field the runner may not write is refused unless it is
declared open on create, a blank cell is dropped, and the school's own
requirements bind only a runner able to supply the field.

Brightfield has two branches and Sunrise one, and a switch belongs to a role
and a role to one school, so nothing one school closes reaches the other.
"""
from __future__ import annotations

import datetime as dt

from schools.vs_students.constants import Gender, StudentStatus
from schools.vs_students.guardian_imports import (
    execute_guardians_import,
    validate_guardians_import_batch,
)
from schools.vs_students.imports import validate_students_import_batch
from schools.vs_students.models import Guardian, Student, StudentGuardian
from schools.vs_students.services.guardian_rules import (
    read_guardian_rules,
    write_guardian_rules,
)
from schools.vs_students.services.rules import read_rules, write_rules
from vs_import_data.models import ImportJobStatusChoices, ImportRowActionChoices
from vs_import_data.services.import_executor import (
    execute_dataset_handler,
    execute_import,
    map_row_to_payload,
)
from vs_rbac.field_enforcement import FieldWriteDenied
from vs_rbac.models import PermissionScope
from vs_rbac.tests.helpers import (
    install_declared_fields,
    make_assignment,
    make_permission,
    make_role,
    make_role_permission,
    make_school_admin,
    set_field_access,
)

from .test_field_write_paths import OFFICER_KEYS, _WritePathFixture

STUDENT_ADDRESS = "school.students.address"
STUDENT_MIDDLE = "school.students.middle_name"
PREVIOUS_SCHOOL = "school.students.previous_school"
GUARDIAN_EMAIL = "school.guardians.email"
GUARDIAN_ADDRESS = "school.guardians.address"
GUARDIAN_MIDDLE = "school.guardians.middle_name"
GUARDIAN_OCCUPATION = "school.guardians.occupation"
BLOOD_GROUP = "school.students.blood_group"

#: Every field a roll or a household list sets while it creates a record. A
#: runner with Write off on all of them still loads a roll, because setting a
#: value as a record is created is not correcting it.
OPEN_ON_CREATE = (
    "school.students.first_name", "school.students.last_name",
    "school.students.date_of_birth", "school.students.gender",
    "school.students.student_number", "school.students.enrolment_date",
    "school.guardians.first_name", "school.guardians.last_name",
    "school.guardians.phone",
)


class _Fixture(_WritePathFixture):
    """Seven runners per school, each closed on a different field."""

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        install_declared_fields("school.students", "school.guardians")
        cls.settings_key = make_permission(
            "school.settings.update", scope=PermissionScope.TENANT,
        )
        closures = {
            "open": {},
            "address_ro": {"read_only": (STUDENT_ADDRESS,)},
            "guardian_address_ro": {"read_only": (GUARDIAN_ADDRESS,)},
            "guardian_email_ro": {"read_only": (GUARDIAN_EMAIL,)},
            "occupation_ro": {"read_only": (GUARDIAN_OCCUPATION,)},
            "middle_ro": {"read_only": (STUDENT_MIDDLE, GUARDIAN_MIDDLE)},
            "previous_school_ro": {"read_only": (PREVIOUS_SCHOOL,)},
            "previous_school_blind": {"closed": (PREVIOUS_SCHOOL,)},
            "create_only": {"read_only": OPEN_ON_CREATE},
            "two_ro": {"read_only": (
                STUDENT_ADDRESS, PREVIOUS_SCHOOL, GUARDIAN_EMAIL, GUARDIAN_OCCUPATION,
            )},
        }
        cls.runners = {
            label: {
                name: cls.runner(school, label, name, **closed)
                for name, closed in closures.items()
            }
            for label, school in (("multi", cls.school), ("single", cls.solo))
        }
        born = dt.date(2014, 3, 3)
        cls.children = {
            "multi": Student.all_objects.create(
                tenant=cls.tenant, branch=cls.lekki, first_name="Amaka",
                last_name="Adeleke", student_number="BFS/1",
                date_of_birth=born, gender=Gender.FEMALE,
                status=StudentStatus.ACTIVE, enrolment_date=dt.date(2025, 9, 8),
            ),
            "single": Student.all_objects.create(
                tenant=cls.solo.tenant, branch=cls.solo_branch,
                first_name="Amaka", last_name="Adeleke", student_number="BFS/1",
                date_of_birth=born, gender=Gender.FEMALE,
                status=StudentStatus.ACTIVE, enrolment_date=dt.date(2025, 9, 8),
            ),
        }

    @classmethod
    def runner(cls, school, label, name, *, closed=(), read_only=()):
        role = make_role(school, name=f"{name} {label}", key=f"imp_{name}_{label}")
        for key in OFFICER_KEYS:
            make_role_permission(role, cls.permissions[key])
        if closed:
            set_field_access(role, *closed, read=False, write=False)
        if read_only:
            set_field_access(role, *read_only, read=True, write=False)
        user = make_school_admin(
            None, email=f"{name}.{label}@import-fa.test", tenant=school.tenant,
        )
        make_assignment(school, user, role, branch=None)
        return user

    def shapes_of_import(self):
        """``(label, tenant, branch, row overrides)`` for each shape of school."""
        return (
            ("multi", self.tenant, self.lekki, {}),
            ("single", self.solo.tenant, self.solo_branch, {"Branch": "", "Class": ""}),
        )

    def counts_of(self, tenant):
        return tuple(
            model.all_objects.filter(tenant=tenant).count()
            for model in (Student, Guardian, StudentGuardian)
        )


# ── the roll ────────────────────────────────────────────────────────────────

class _RollFixture(_Fixture):
    def batch_for(self, rows, *, tenant, branch, uploaded_by):
        from vs_import_data.models import ImportBatch, ImportTemplate

        return ImportBatch.all_objects.create(
            tenant=tenant, branch=branch,
            template=ImportTemplate.objects.get(code="students_v1"),
            dataset_type="students", preview_rows=rows,
            original_filename="roll.xlsx", uploaded_by=uploaded_by,
        )

    def preview(self, label, who, **cells):
        shape = next(s for s in self.shapes_of_import() if s[0] == label)
        _, tenant, branch, overrides = shape
        user = self.runners[label][who]
        batch = self.batch_for(
            [self.row(**{**overrides, **cells})],
            tenant=tenant, branch=branch, uploaded_by=user,
        )
        return validate_students_import_batch(batch, actor=user)

    def refusals(self, issues):
        return [i for i in issues if i.get("metadata", {}).get("field_write_denied")]

    def import_row(self, label, who, **cells):
        shape = next(s for s in self.shapes_of_import() if s[0] == label)
        _, tenant, branch, overrides = shape
        user = self.runners[label][who]
        raw_row = self.row(**{**overrides, **cells})
        batch = self.batch_for(
            [raw_row], tenant=tenant, branch=branch, uploaded_by=user,
        )
        return execute_dataset_handler(
            batch, map_row_to_payload(batch, raw_row), user,
        )


class RollWriteSwitchTests(_RollFixture):
    def test_a_field_the_runner_may_not_write_is_refused_and_nothing_is_written(self):
        for label, tenant, _, _ in self.shapes_of_import():
            with self.subTest(school=label):
                before = self.counts_of(tenant)
                with self.assertRaises(FieldWriteDenied) as caught:
                    self.import_row(label, "address_ro", **{"Home Address": "14 Admiralty Way"})
                self.assertEqual(caught.exception.fields, ["address"])
                self.assertEqual(
                    caught.exception.message,
                    "You do not have permission to change Address.",
                )
                self.assertEqual(self.counts_of(tenant), before)

    def test_the_preview_reports_the_same_refusal_against_its_row_and_column(self):
        for label, _, _, _ in self.shapes_of_import():
            with self.subTest(school=label):
                found = self.refusals(self.preview(
                    label, "address_ro", **{"Home Address": "14 Admiralty Way"},
                ))
                self.assertEqual(len(found), 1, found)
                self.assertEqual(found[0]["severity"], "error")
                self.assertEqual(found[0]["row_number"], 1)
                self.assertEqual(found[0]["column_name"], "Home Address")
                self.assertEqual(
                    found[0]["message"],
                    "You do not have permission to change Address.",
                )

    def test_a_preview_made_for_nobody_refuses_nothing(self):
        for label, tenant, branch, overrides in self.shapes_of_import():
            with self.subTest(school=label):
                batch = self.batch_for(
                    [self.row(**{**overrides, "Home Address": "14 Admiralty Way"})],
                    tenant=tenant, branch=branch,
                    uploaded_by=self.runners[label]["address_ro"],
                )
                self.assertEqual(
                    self.refusals(validate_students_import_batch(batch)), [],
                )

    def test_a_blank_cell_is_not_a_write(self):
        for label, _, _, _ in self.shapes_of_import():
            with self.subTest(school=label):
                self.assertEqual(self.refusals(self.preview(
                    label, "address_ro", **{"Home Address": ""},
                )), [])
                result = self.import_row(
                    label, "address_ro", **{"Home Address": "   ", "First Name": "Blank"},
                )
                self.assertEqual(result.instance.address, "")

    def test_a_runner_who_may_write_the_field_loads_it(self):
        for label, _, _, _ in self.shapes_of_import():
            with self.subTest(school=label):
                cells = {"Home Address": "14 Admiralty Way", "First Name": "Writer"}
                self.assertEqual(self.refusals(self.preview(label, "open", **cells)), [])
                result = self.import_row(label, "open", **cells)
                self.assertEqual(result.instance.address, "14 Admiralty Way")

    def test_a_field_open_on_create_loads_for_a_runner_who_cannot_correct_it(self):
        for label, _, _, _ in self.shapes_of_import():
            with self.subTest(school=label):
                cells = {
                    "First Name": "Created", "Admission No.": f"NEW/{label}/1",
                    "Admission Date": "2025-09-08",
                    "Guardian Name": "Mrs Ngozi Okoro",
                }
                self.assertEqual(
                    self.refusals(self.preview(label, "create_only", **cells)), [],
                )
                result = self.import_row(label, "create_only", **cells)
                self.assertEqual(result.instance.student_number, f"NEW/{label}/1")
                self.assertEqual(result.instance.enrolment_date, dt.date(2025, 9, 8))

    def test_a_guardian_detail_is_judged_under_the_guardian_switch(self):
        for label, tenant, _, _ in self.shapes_of_import():
            with self.subTest(school=label):
                before = self.counts_of(tenant)
                with self.assertRaises(FieldWriteDenied) as caught:
                    self.import_row(label, "guardian_email_ro", **{"First Name": "Mailed"})
                self.assertEqual(caught.exception.fields, ["guardian_email"])
                self.assertEqual(
                    caught.exception.message,
                    "You do not have permission to change Guardian Email.",
                )
                self.assertEqual(self.counts_of(tenant), before)

    def test_the_one_address_column_is_also_the_address_of_a_new_guardian(self):
        for label, tenant, _, _ in self.shapes_of_import():
            with self.subTest(school=label):
                before = self.counts_of(tenant)
                with self.assertRaises(FieldWriteDenied) as caught:
                    self.import_row(
                        label, "guardian_address_ro",
                        **{"Home Address": "14 Admiralty Way", "First Name": "Housed"},
                    )
                self.assertEqual(caught.exception.fields, ["address"])
                self.assertEqual(
                    caught.exception.message,
                    "You do not have permission to change Guardian Address.",
                )
                self.assertEqual(self.counts_of(tenant), before)

    def test_a_one_line_guardian_name_with_a_middle_part_sets_the_middle_name(self):
        for label, _, _, _ in self.shapes_of_import():
            with self.subTest(school=label):
                with self.assertRaises(FieldWriteDenied) as caught:
                    self.import_row(
                        label, "middle_ro",
                        **{"Guardian Name": "Mrs Ngozi Ada Okoro", "First Name": "Mid"},
                    )
                self.assertEqual(caught.exception.fields, ["guardian_full_name"])
                # Two words set no middle name.
                result = self.import_row(
                    label, "middle_ro",
                    **{"Guardian Name": "Mrs Ngozi Okoro", "First Name": "Mid"},
                )
                self.assertIsNotNone(result.instance)

    def test_every_refused_field_of_a_row_is_named_together(self):
        for label, _, _, _ in self.shapes_of_import():
            with self.subTest(school=label):
                with self.assertRaises(FieldWriteDenied) as caught:
                    self.import_row(
                        label, "two_ro",
                        **{"Home Address": "14 Admiralty Way", "Previous School": "Hilltop"},
                    )
                self.assertEqual(
                    caught.exception.fields,
                    ["address", "guardian_email", "previous_school"],
                )
                self.assertEqual(
                    caught.exception.message,
                    "You do not have permission to change Address, Guardian "
                    "Email and Previous school.",
                )

    def test_a_runner_of_another_school_holds_nothing_here(self):
        """Roles are read in the batch's school, so a stranger's switches never count."""
        stranger = self.runners["multi"]["open"]
        batch = self.batch_for(
            [self.row(**{"Branch": "", "Class": "", "Home Address": "1 Elsewhere"})],
            tenant=self.solo.tenant, branch=self.solo_branch, uploaded_by=stranger,
        )
        found = self.refusals(validate_students_import_batch(batch, actor=stranger))
        self.assertEqual(
            [f["column_name"] for f in found], ["Home Address", "Guardian Email"],
        )

    def test_a_job_row_fails_with_the_standard_wording_and_the_rest_of_the_file_loads(self):
        label, tenant, branch, overrides = self.shapes_of_import()[0]
        runner = self.runners[label]["address_ro"]
        batch = self.batch_for(
            [
                self.row(**{"First Name": "Refused", "Home Address": "14 Admiralty Way"}),
                self.row(**{"First Name": "Loaded", "Home Address": ""}),
            ],
            tenant=tenant, branch=branch, uploaded_by=runner,
        )
        batch.is_ready_for_import = True
        batch.save(update_fields=["is_ready_for_import"])
        job = execute_import(batch, runner)
        results = {r.row_number: r for r in job.row_results.all()}
        self.assertEqual(results[1].action, ImportRowActionChoices.FAILED)
        self.assertEqual(
            results[1].status_message,
            "You do not have permission to change Address.",
        )
        self.assertEqual(
            results[1].error_details, {"field_write_denied": ["address"]},
        )
        self.assertEqual(results[2].action, ImportRowActionChoices.CREATE)
        self.assertFalse(Student.all_objects.filter(first_name="Refused").exists())
        self.assertTrue(Student.all_objects.filter(first_name="Loaded").exists())


class RollRuleYieldTests(_RollFixture):
    """The school's requirements bind a runner able to supply the field."""

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        for school in (cls.school, cls.solo):
            admin = cls.admin if school is cls.school else cls.solo_admin
            write_rules(
                school.tenant, admin, min_age_years=2, max_age_years=25,
                required_documents=[], required_fields=["previous_school"],
                capacity_mode="WARN", default_capacity=None,
            )
            write_guardian_rules(
                school.tenant, admin, min_per_student=1, email_required=True,
                matching="EMAIL_THEN_PHONE", extra_relationships=[],
            )

    def errors(self, issues):
        return [i["message"] for i in issues if i["severity"] == "error"]

    def test_a_requirement_the_runner_cannot_meet_does_not_bind_them(self):
        for label, _, _, _ in self.shapes_of_import():
            for who in ("previous_school_ro", "previous_school_blind"):
                with self.subTest(school=label, runner=who):
                    cells = {"Previous School": "", "First Name": f"Yield{who}"}
                    self.assertEqual(self.errors(self.preview(label, who, **cells)), [])
                    result = self.import_row(label, who, **cells)
                    self.assertEqual(result.instance.previous_school, "")

    def test_the_same_requirement_binds_a_runner_who_can_write_the_field(self):
        for label, _, _, _ in self.shapes_of_import():
            with self.subTest(school=label):
                self.assertEqual(
                    self.errors(self.preview(label, "open", **{"Previous School": ""})),
                    ["Previous school is required at this school."],
                )
                with self.assertRaises(ValueError) as caught:
                    self.import_row(label, "open", **{"Previous School": ""})
                self.assertIn("Previous school is required", str(caught.exception))

    def test_a_preview_made_for_nobody_demands_every_requirement(self):
        label, tenant, branch, overrides = self.shapes_of_import()[0]
        batch = self.batch_for(
            [self.row(**{**overrides, "Previous School": ""})],
            tenant=tenant, branch=branch, uploaded_by=self.admin,
        )
        self.assertEqual(
            self.errors(validate_students_import_batch(batch)),
            ["Previous school is required at this school."],
        )

    def test_a_value_supplied_for_a_field_the_runner_cannot_write_is_still_refused(self):
        for label, _, _, _ in self.shapes_of_import():
            with self.subTest(school=label):
                with self.assertRaises(FieldWriteDenied):
                    self.import_row(label, "previous_school_ro", **{"Previous School": "Hilltop"})

    def test_the_guardian_email_requirement_yields_to_a_runner_who_cannot_write_it(self):
        for label, tenant, _, _ in self.shapes_of_import():
            with self.subTest(school=label):
                cells = {
                    "Previous School": "Hilltop", "Guardian Email": "",
                    "First Name": "Mailless", "Guardian Phone": f"0803555{label[0]}202",
                }
                self.assertEqual(
                    self.errors(self.preview(label, "guardian_email_ro", **cells)), [],
                )
                result = self.import_row(label, "guardian_email_ro", **cells)
                guardian = result.instance.guardian_links.get().guardian
                self.assertEqual(guardian.email, "")

    def test_the_guardian_email_requirement_binds_a_runner_who_can_write_it(self):
        for label, _, _, _ in self.shapes_of_import():
            with self.subTest(school=label):
                self.assertEqual(
                    self.errors(self.preview(
                        label, "open",
                        **{"Previous School": "Hilltop", "Guardian Email": ""},
                    )),
                    ["A guardian email is required at this school."],
                )

    def test_one_school_closing_a_switch_does_not_relax_the_other(self):
        self.assertEqual(
            read_rules(self.solo.tenant).required_fields, ("previous_school",),
        )
        self.assertTrue(read_guardian_rules(self.solo.tenant).email_required)


# ── the household list ──────────────────────────────────────────────────────

class _HouseholdFixture(_Fixture):
    def grow(self, **overrides):
        base = {
            "Guardian Name": "Mr. Emeka Adeleke",
            "Guardian Phone": "08035550102",
            "Guardian Email": "emeka@example.ng",
            "Admission Number": "BFS/1",
            "Student First Name": "", "Student Last Name": "",
            "Student Date of Birth": "",
            "Relationship": "Father", "Primary Contact": "Yes",
            "Occupation": "", "Home Address": "",
        }
        base.update(overrides)
        return base

    def gbatch(self, label, who, **cells):
        from vs_import_data.models import ImportBatch, ImportTemplate

        tenant = self.tenant if label == "multi" else self.solo.tenant
        return ImportBatch.all_objects.create(
            tenant=tenant,
            template=ImportTemplate.objects.get(code="guardians_v1"),
            dataset_type="guardians", preview_rows=[self.grow(**cells)],
            original_filename="households.xlsx",
            uploaded_by=self.runners[label][who],
        )

    def gpreview(self, label, who, **cells):
        batch = self.gbatch(label, who, **cells)
        return validate_guardians_import_batch(batch, actor=self.runners[label][who])

    def refusals(self, issues):
        return [i for i in issues if i.get("metadata", {}).get("field_write_denied")]

    def grun(self, label, who, **cells):
        return execute_guardians_import(
            self.gbatch(label, who, **cells), self.runners[label][who],
        )


class HouseholdWriteSwitchTests(_HouseholdFixture):
    def test_the_preview_reports_a_field_the_runner_may_not_write(self):
        for label in ("multi", "single"):
            with self.subTest(school=label):
                found = self.refusals(self.gpreview(label, "guardian_email_ro"))
                self.assertEqual(len(found), 1, found)
                self.assertEqual(found[0]["column_name"], "Guardian Email")
                self.assertEqual(found[0]["row_number"], 1)
                self.assertEqual(
                    found[0]["message"],
                    "You do not have permission to change Email.",
                )

    def test_a_file_that_sets_a_refused_field_is_refused_whole_and_writes_nothing(self):
        from vs_import_data.models import ImportJob

        for label in ("multi", "single"):
            with self.subTest(school=label):
                tenant = self.tenant if label == "multi" else self.solo.tenant
                before = self.counts_of(tenant)
                batch = self.gbatch(label, "guardian_email_ro")
                with self.assertRaises(ValueError) as caught:
                    execute_guardians_import(batch, self.runners[label]["guardian_email_ro"])
                self.assertEqual(
                    str(caught.exception),
                    "You do not have permission to change Email.",
                )
                job = ImportJob.objects.filter(import_batch=batch).get()
                self.assertEqual(job.status, ImportJobStatusChoices.FAILED)
                self.assertEqual(job.last_error_code, "FIELD_WRITE_DENIED")
                self.assertEqual(
                    job.last_error_message,
                    "You do not have permission to change Email.",
                )
                self.assertEqual(self.counts_of(tenant), before)

    def test_every_refused_column_is_reported_against_its_own_header(self):
        found = self.refusals(self.gpreview(
            "multi", "two_ro", Occupation="Engineer", **{"Guardian Email": "a@b.ng"},
        ))
        self.assertEqual(
            sorted(f["column_name"] for f in found), ["Guardian Email", "Occupation"],
        )

    def test_a_blank_cell_is_not_a_write(self):
        for label in ("multi", "single"):
            with self.subTest(school=label):
                self.assertEqual(
                    self.refusals(self.gpreview(label, "occupation_ro")), [],
                )

    def test_a_runner_who_may_write_the_fields_links_the_household(self):
        for label in ("multi", "single"):
            with self.subTest(school=label):
                job = self.grun(label, "open", Occupation="Engineer")
                self.assertEqual(job.status, ImportJobStatusChoices.SUCCEEDED)
                tenant = self.tenant if label == "multi" else self.solo.tenant
                guardian = Guardian.all_objects.get(tenant=tenant, email="emeka@example.ng")
                self.assertEqual(guardian.occupation, "Engineer")
                self.assertEqual(
                    self.children[label].guardian_links.get().guardian, guardian,
                )

    def test_fields_open_on_create_load_for_a_runner_who_cannot_correct_them(self):
        for label in ("multi", "single"):
            with self.subTest(school=label):
                self.assertEqual(
                    self.refusals(self.gpreview(label, "create_only", **{"Guardian Email": ""})),
                    [],
                )
                job = self.grun(label, "create_only", **{"Guardian Email": ""})
                self.assertEqual(job.status, ImportJobStatusChoices.SUCCEEDED)

    def test_a_runner_of_another_school_holds_nothing_here(self):
        from vs_import_data.models import ImportBatch, ImportTemplate

        stranger = self.runners["multi"]["open"]
        batch = ImportBatch.all_objects.create(
            tenant=self.solo.tenant,
            template=ImportTemplate.objects.get(code="guardians_v1"),
            dataset_type="guardians", preview_rows=[self.grow(Occupation="Engineer")],
            original_filename="households.xlsx", uploaded_by=stranger,
        )
        found = self.refusals(validate_guardians_import_batch(batch, actor=stranger))
        self.assertIn("Occupation", [f["column_name"] for f in found])


class HouseholdRuleYieldTests(_HouseholdFixture):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        for school, admin in ((cls.school, cls.admin), (cls.solo, cls.solo_admin)):
            write_guardian_rules(
                school.tenant, admin, min_per_student=1, email_required=True,
                matching="EMAIL_THEN_PHONE", extra_relationships=[],
            )

    def errors(self, issues):
        return [i["message"] for i in issues if i["severity"] == "error"]

    def test_the_email_requirement_yields_to_a_runner_who_cannot_write_it(self):
        for label in ("multi", "single"):
            with self.subTest(school=label):
                self.assertEqual(
                    self.errors(self.gpreview(
                        label, "guardian_email_ro", **{"Guardian Email": ""},
                    )),
                    [],
                )
                job = self.grun(label, "guardian_email_ro", **{"Guardian Email": ""})
                self.assertEqual(job.status, ImportJobStatusChoices.SUCCEEDED)
                tenant = self.tenant if label == "multi" else self.solo.tenant
                self.assertEqual(
                    Guardian.all_objects.get(tenant=tenant, phone="08035550102").email, "",
                )

    def test_the_email_requirement_binds_a_runner_who_can_write_it(self):
        for label in ("multi", "single"):
            with self.subTest(school=label):
                self.assertEqual(
                    self.errors(self.gpreview(label, "open", **{"Guardian Email": ""})),
                    ["A guardian email is required at this school."],
                )

    def test_a_preview_made_for_nobody_demands_it(self):
        batch = self.gbatch("multi", "open", **{"Guardian Email": ""})
        self.assertEqual(
            self.errors(validate_guardians_import_batch(batch)),
            ["A guardian email is required at this school."],
        )


# ── saving the rules ────────────────────────────────────────────────────────

class _RulesFixture(_Fixture):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.settings_admins = {}
        for label, school in (("multi", cls.school), ("single", cls.solo)):
            cls.settings_admins[label] = {
                "open": cls.settings_admin(school, label, "open", read_blood=True),
                "blind": cls.settings_admin(school, label, "blind", read_blood=False),
            }
            admin = cls.admin if label == "multi" else cls.solo_admin
            write_rules(
                school.tenant, admin, min_age_years=2, max_age_years=25,
                required_documents=[], required_fields=["blood_group", "address"],
                capacity_mode="WARN", default_capacity=None,
            )
            write_guardian_rules(
                school.tenant, admin, min_per_student=1, email_required=True,
                matching="EMAIL_THEN_PHONE", extra_relationships=[],
            )

    @classmethod
    def settings_admin(cls, school, label, name, *, read_blood):
        role = make_role(school, name=f"cfg {name} {label}", key=f"cfg_{name}_{label}")
        make_role_permission(role, cls.settings_key)
        make_role_permission(role, cls.permissions["school.students.view"])
        if read_blood:
            set_field_access(role, BLOOD_GROUP, GUARDIAN_EMAIL, read=True, write=True)
        else:
            set_field_access(role, BLOOD_GROUP, GUARDIAN_EMAIL, read=False, write=False)
        user = make_school_admin(
            None, email=f"cfg.{name}.{label}@import-fa.test", tenant=school.tenant,
        )
        make_assignment(school, user, role, branch=None)
        return user

    def enrolment_body(self, required_fields):
        return {
            "min_age_years": 2, "max_age_years": 25, "required_documents": [],
            "required_fields": required_fields, "capacity_mode": "WARN",
            "default_capacity": None,
        }

    def guardian_body(self, email_required):
        return {
            "min_per_student": 1, "email_required": email_required,
            "matching": "EMAIL_THEN_PHONE", "extra_relationships": [],
        }

    def stored(self, label):
        tenant = self.tenant if label == "multi" else self.solo.tenant
        return set(read_rules(tenant).required_fields)


class EnrolmentRulesSaveTests(_RulesFixture):
    def test_a_requirement_for_a_field_the_admin_cannot_read_survives_their_save(self):
        for label in ("multi", "single"):
            with self.subTest(school=label):
                response = self.put(
                    self.settings_admins[label]["blind"], "student-enrolment-rules",
                    self.enrolment_body(["nationality"]),
                )
                self.assertEqual(response.status_code, 200, response.data)
                # The hidden requirement stays; the readable one they dropped is gone.
                self.assertEqual(self.stored(label), {"blood_group", "nationality"})
                self.assertEqual(
                    set(response.data["data"]["required_fields"]),
                    {"blood_group", "nationality"},
                )

    def test_an_admin_cannot_add_a_requirement_for_a_field_they_cannot_read(self):
        for label in ("multi", "single"):
            with self.subTest(school=label):
                tenant = self.tenant if label == "multi" else self.solo.tenant
                admin = self.admin if label == "multi" else self.solo_admin
                write_rules(
                    tenant, admin, min_age_years=2, max_age_years=25,
                    required_documents=[], required_fields=[],
                    capacity_mode="WARN", default_capacity=None,
                )
                response = self.put(
                    self.settings_admins[label]["blind"], "student-enrolment-rules",
                    self.enrolment_body(["blood_group", "address"]),
                )
                self.assertEqual(response.status_code, 200, response.data)
                self.assertEqual(self.stored(label), {"address"})

    def test_an_admin_who_can_read_the_field_sets_and_clears_it_freely(self):
        for label in ("multi", "single"):
            with self.subTest(school=label):
                response = self.put(
                    self.settings_admins[label]["open"], "student-enrolment-rules",
                    self.enrolment_body(["address"]),
                )
                self.assertEqual(response.status_code, 200, response.data)
                self.assertEqual(self.stored(label), {"address"})

    def test_one_school_saving_never_changes_the_other(self):
        self.put(
            self.settings_admins["multi"]["blind"], "student-enrolment-rules",
            self.enrolment_body([]),
        )
        self.assertEqual(self.stored("single"), {"blood_group", "address"})


class GuardianRulesSaveTests(_RulesFixture):
    def guardian_rules(self, label):
        tenant = self.tenant if label == "multi" else self.solo.tenant
        return read_guardian_rules(tenant)

    def test_an_email_requirement_the_admin_cannot_read_is_not_theirs_to_relax(self):
        for label in ("multi", "single"):
            with self.subTest(school=label):
                response = self.put(
                    self.settings_admins[label]["blind"], "student-guardian-rules",
                    self.guardian_body(False),
                )
                self.assertEqual(response.status_code, 200, response.data)
                self.assertTrue(self.guardian_rules(label).email_required)
                self.assertTrue(response.data["data"]["email_required"])

    def test_nor_to_tighten(self):
        for label in ("multi", "single"):
            with self.subTest(school=label):
                tenant = self.tenant if label == "multi" else self.solo.tenant
                admin = self.admin if label == "multi" else self.solo_admin
                write_guardian_rules(
                    tenant, admin, min_per_student=1, email_required=False,
                    matching="EMAIL_THEN_PHONE", extra_relationships=[],
                )
                self.put(
                    self.settings_admins[label]["blind"], "student-guardian-rules",
                    self.guardian_body(True),
                )
                self.assertFalse(self.guardian_rules(label).email_required)

    def test_an_admin_who_can_read_the_email_sets_the_requirement(self):
        for label in ("multi", "single"):
            with self.subTest(school=label):
                response = self.put(
                    self.settings_admins[label]["open"], "student-guardian-rules",
                    self.guardian_body(False),
                )
                self.assertEqual(response.status_code, 200, response.data)
                self.assertFalse(self.guardian_rules(label).email_required)

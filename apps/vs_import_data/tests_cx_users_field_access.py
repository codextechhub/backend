"""Adding a member of CodeX staff applies the caller's Field Access, by form or by file.

``platform.staff_profile`` holds the details of a CodeX staff member. The Add
form, the draft save and the CX users spreadsheet all set them as the account
is created, through ``UserCreateSerializer``, so each is judged by its Write
switch there: a detail the caller may not write is refused unless the registry
declares it open on create (the name), and an empty input is dropped.

The import previews the same refusals against the row and column, and the
engine fails the refused row with the same sentence while loading the rest.

A school's own staff never reach these switches: only an account created on the
platform tenant is judged.
"""
from __future__ import annotations

import datetime as dt
from types import SimpleNamespace
from unittest import mock

from django.test import TestCase
from rest_framework.test import APIClient

from vs_import_data.models import (
    DatasetTypeChoices,
    ImportBatch,
    ImportJobRowResult,
    ImportRowActionChoices,
    ImportTemplate,
    ImportTemplateColumn,
    TemplateColumnDataTypeChoices,
    TemplateStatusChoices,
)
from vs_import_data.services.import_executor import (
    cx_user_row_write_refusal,
    execute_dataset_handler,
    execute_import,
    map_row_to_payload,
)
from vs_import_data.services.validation_service import _validate_cx_users_rules
from vs_rbac.field_enforcement import FieldWriteDenied
from vs_rbac.tests.helpers import (
    install_declared_fields,
    make_assignment,
    make_permission,
    make_role,
    make_role_permission,
    make_school,
    make_school_admin,
    make_vision_user,
    platform_tenant,
    set_field_access,
)
from vs_user.models import OrgNode, Position, User
from vs_user.serializers import UserCreateSerializer

HEADINGS = {
    "first_name": "First Name",
    "last_name": "Last Name",
    "email": "Email",
    "role": "Role",
    "phone": "Phone",
    "gender": "Gender",
    "employment_type": "Employment Type",
    "position": "Position",
    "date_joined": "Date Joined",
}

EMPLOYMENT_TYPE = "platform.staff_profile.employment_type"
DATE_JOINED = "platform.staff_profile.date_joined"
FIRST_NAME = "platform.staff_profile.first_name"
LAST_NAME = "platform.staff_profile.last_name"
DATE_OF_BIRTH = "platform.staff_profile.date_of_birth"


class _Fixture(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.tenant = platform_tenant()
        cls.field_keys = install_declared_fields("platform.staff_profile")
        cls.create_key = make_permission("platform.team.create")
        cls.hire_role = make_role(cls.tenant, name="Platform Admin", key="xvs_platform_admin")
        node = OrgNode.objects.create(
            name="Import Division", code="IMPORT", kind=OrgNode.Kind.DIVISION,
        )
        cls.position = Position.objects.create(
            title="Import Analyst", code="IMPORT-ANALYST", org_node=node,
        )
        cls.template = ImportTemplate.objects.create(
            code="cx_users_fa_v1", name="CX users", dataset_type=DatasetTypeChoices.CX_USERS,
            status=TemplateStatusChoices.ACTIVE,
        )
        for order, (field, heading) in enumerate(HEADINGS.items()):
            ImportTemplateColumn.objects.create(
                template=cls.template, column_name=heading, target_field=field,
                display_name=heading, data_type=TemplateColumnDataTypeChoices.STRING,
                is_required=field in ("first_name", "last_name", "email"),
                column_order=order,
            )
        closures = {
            "open": {},
            "type_ro": {"read_only": (EMPLOYMENT_TYPE,)},
            "joined_blind": {"closed": (DATE_JOINED,)},
            "name_ro": {"read_only": (FIRST_NAME, LAST_NAME)},
            "birth_ro": {"read_only": (DATE_OF_BIRTH,)},
            "two_ro": {"read_only": (EMPLOYMENT_TYPE, DATE_JOINED)},
        }
        cls.runners = {
            name: cls.runner(name, **closed) for name, closed in closures.items()
        }
        cls.super_admin = make_vision_user(email="super@cx-fa.test", super_admin=True)

    @classmethod
    def runner(cls, name, *, closed=(), read_only=()):
        role = make_role(cls.tenant, name=f"runner {name}", key=f"cx_runner_{name}")
        make_role_permission(role, cls.create_key)
        if closed:
            set_field_access(role, *closed, read=False, write=False)
        if read_only:
            set_field_access(role, *read_only, read=True, write=False)
        user = make_vision_user(email=f"{name}@cx-fa.test")
        make_assignment(cls.tenant, user, role, branch=None)
        return user

    def cells(self, email="new.hire@codex.test", **overrides):
        values = {
            "First Name": "Bulk", "Last Name": "Hire", "Email": email,
            "Role": self.hire_role.key, "Phone": "", "Gender": "MALE",
            "Employment Type": "FULL_TIME", "Position": self.position.code,
            "Date Joined": "2026-09-07",
        }
        values.update(overrides)
        return values

    def batch_for(self, rows, uploaded_by):
        return ImportBatch.all_objects.create(
            tenant=self.tenant, template=self.template, dataset_type="cx_users",
            preview_rows=list(rows), original_filename="cx.csv", uploaded_by=uploaded_by,
        )

    def import_row(self, runner, **cells):
        user = runner if isinstance(runner, User) else self.runners[runner]
        raw_row = self.cells(**cells)
        batch = self.batch_for([raw_row], user)
        with mock.patch("vs_workflow.services.submission.submit_for_approval"):
            return execute_dataset_handler(batch, map_row_to_payload(batch, raw_row), user)

    def preview(self, runner, **cells):
        user = self.runners[runner]
        return _validate_cx_users_rules(self.batch_for([self.cells(**cells)], user), user)


class CxImportWriteSwitchTests(_Fixture):
    def test_a_detail_the_runner_may_not_write_is_refused_and_nobody_is_created(self):
        before = User.objects.count()
        with self.assertRaises(FieldWriteDenied) as caught:
            self.import_row("type_ro")
        self.assertEqual(caught.exception.fields, ["employment_type"])
        self.assertEqual(
            caught.exception.message,
            "You do not have permission to change Employment type.",
        )
        self.assertEqual(User.objects.count(), before)

    def test_a_detail_the_runner_cannot_read_is_refused_the_same_way(self):
        with self.assertRaises(FieldWriteDenied) as caught:
            self.import_row("joined_blind")
        self.assertEqual(caught.exception.fields, ["date_joined"])

    def test_every_refused_detail_of_a_row_is_named_at_once(self):
        with self.assertRaises(FieldWriteDenied) as caught:
            self.import_row("two_ro")
        self.assertEqual(caught.exception.fields, ["date_joined", "employment_type"])

    def test_a_blank_cell_is_dropped_not_refused(self):
        result = self.import_row(
            "two_ro", **{"Employment Type": "", "Date Joined": ""},
        )
        self.assertEqual(result.action, ImportRowActionChoices.CREATE)

    def test_the_name_is_open_on_create(self):
        result = self.import_row("name_ro")
        self.assertEqual(result.action, ImportRowActionChoices.CREATE)
        self.assertEqual(result.instance.first_name, "Bulk")

    def test_a_runner_who_may_write_everything_loads_the_row(self):
        result = self.import_row("open")
        self.assertEqual(result.action, ImportRowActionChoices.CREATE)
        profile = result.instance.platform_staff_profile
        self.assertEqual(profile.employment_type, "FULL_TIME")
        self.assertEqual(profile.date_joined, dt.date(2026, 9, 7))

    def test_the_platform_super_admin_is_never_refused(self):
        result = self.import_row(self.super_admin)
        self.assertEqual(result.action, ImportRowActionChoices.CREATE)


class CxImportPreviewTests(_Fixture):
    def test_the_preview_reports_the_refusal_against_its_row_and_column(self):
        issues = self.preview("type_ro")
        self.assertEqual(len(issues), 1, issues)
        issue = issues[0]
        self.assertEqual(issue["severity"], "error")
        self.assertEqual(issue["code"], "business_rule")
        self.assertEqual(issue["row_number"], 1)
        self.assertEqual(issue["column_name"], "Employment Type")
        self.assertEqual(issue["metadata"], {"field_write_denied": ["employment_type"]})
        self.assertEqual(
            issue["message"], "You do not have permission to change Employment type.",
        )

    def test_the_preview_and_execution_agree_for_every_runner(self):
        for who in self.runners:
            with self.subTest(runner=who):
                previewed = sorted(
                    field for issue in self.preview(who, Email=f"{who}@new.codex.test")
                    for field in issue["metadata"]["field_write_denied"]
                )
                try:
                    self.import_row(who, Email=f"{who}@new.codex.test")
                    executed = []
                except FieldWriteDenied as refusal:
                    executed = refusal.fields
                self.assertEqual(previewed, executed)

    def test_a_preview_made_for_nobody_refuses_nothing(self):
        batch = self.batch_for([self.cells()], self.runners["type_ro"])
        self.assertEqual(_validate_cx_users_rules(batch, None), [])
        self.assertIsNone(
            cx_user_row_write_refusal(map_row_to_payload(batch, self.cells()), None),
        )


class CxImportEngineTests(_Fixture):
    def test_the_engine_fails_the_refused_row_and_still_loads_the_others(self):
        batch = self.batch_for(
            [
                self.cells(email="first@new.codex.test"),
                self.cells(
                    email="second@new.codex.test",
                    **{"Employment Type": "", "Date Joined": ""},
                ),
            ],
            self.runners["two_ro"],
        )
        batch.is_ready_for_import = True
        batch.total_rows = 2
        batch.save(update_fields=["is_ready_for_import", "total_rows"])

        with mock.patch("vs_workflow.services.submission.submit_for_approval"):
            job = execute_import(batch, self.runners["two_ro"])

        self.assertEqual(job.failed_rows, 1)
        self.assertEqual(job.succeeded_rows, 1)
        refused = ImportJobRowResult.objects.get(job=job, row_number=1)
        self.assertEqual(refused.action, ImportRowActionChoices.FAILED)
        self.assertEqual(
            refused.error_details,
            {"field_write_denied": ["date_joined", "employment_type"]},
        )
        self.assertFalse(User.objects.filter(email="first@new.codex.test").exists())
        self.assertTrue(User.objects.filter(email="second@new.codex.test").exists())


class CxAddFormTests(_Fixture):
    """The Add form and the draft save are judged by the same switches as the file."""

    def post(self, runner, **body):
        client = APIClient()
        client.force_authenticate(user=self.runners[runner])
        payload = {
            "first_name": "Form", "last_name": "Hire", "email": f"{runner}.form@codex.test",
            "gender": "FEMALE", "role": self.hire_role.key,
            "position": self.position.code, "save_as_draft": True,
        }
        payload.update(body)
        return client.post("/v1/user/users/", payload, format="json")

    def test_a_personal_detail_the_caller_may_not_write_is_refused_with_403(self):
        before = User.objects.count()
        resp = self.post("birth_ro", date_of_birth="1990-05-04")
        self.assertEqual(resp.status_code, 403, resp.content)
        self.assertIn("date_of_birth", resp.json()["error"]["detail"])
        self.assertEqual(User.objects.count(), before)

    def test_the_same_detail_is_accepted_from_a_caller_who_may_write_it(self):
        resp = self.post("open", date_of_birth="1990-05-04")
        self.assertEqual(resp.status_code, 201, resp.content)
        self.assertEqual(
            User.objects.get(email="open.form@codex.test")
            .platform_staff_profile.date_of_birth,
            dt.date(1990, 5, 4),
        )

    def test_a_form_that_leaves_the_detail_empty_is_not_refused(self):
        resp = self.post("birth_ro", date_of_birth=None)
        self.assertEqual(resp.status_code, 201, resp.content)

    def test_the_name_is_open_on_create(self):
        resp = self.post("name_ro")
        self.assertEqual(resp.status_code, 201, resp.content)


class SchoolStaffAreNotJudgedByThePlatformSwitchesTests(_Fixture):
    def test_a_school_account_is_created_without_asking_the_platform_switches(self):
        school = make_school(slug="cx-fa-school", name="Brightfield Schools")
        admin = make_school_admin(None, email="adaeze@cx-fa-school.test", tenant=school.tenant)
        role = make_role(school, name="Teacher", key="teacher")
        request = SimpleNamespace(user=admin, tenant=school.tenant)
        serializer = UserCreateSerializer(
            data={
                "first_name": "Ifeoma", "last_name": "Anyanwu",
                "email": "ifeoma@cx-fa-school.test", "role": role.key,
            },
            context={"request": request},
        )
        with mock.patch(
            "vs_user.serializers.refuse_unwritable_staff_profile_fields",
        ) as judged:
            self.assertTrue(serializer.is_valid(), serializer.errors)
        judged.assert_not_called()

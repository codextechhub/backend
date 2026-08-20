from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase

from core.test_utils import TenantAPIClient
from vs_rbac.tests.helpers import (
    codex_tenant,
    make_assignment,
    make_branch,
    make_permission,
    make_role,
    make_role_permission,
    make_school,
    make_school_admin,
    make_vision_user,
)

from .models import (
    DatasetTypeChoices,
    FileFormatChoices,
    ImportBatch,
    ImportBatchStatusChoices,
    ImportTemplate,
    ImportTemplateColumn,
    TemplateColumnDataTypeChoices,
)
from .services.validation_service import validate_import_batch


class ImportValidationPublishGateTests(TestCase):
    def test_one_valid_row_does_not_hide_another_rows_validation_error(self):
        tenant = codex_tenant()
        user = make_vision_user(email="import-validation-gate@test.com", tenant=tenant)
        template = ImportTemplate.objects.create(
            code="publish-gate-test",
            name="Publish Gate",
            dataset_type=DatasetTypeChoices.CX_USERS,
            default_file_format=FileFormatChoices.CSV,
        )
        ImportTemplateColumn.objects.create(
            template=template,
            column_name="Name",
            target_field="name",
            data_type=TemplateColumnDataTypeChoices.STRING,
            is_required=True,
        )
        batch = ImportBatch.objects.create(
            tenant=tenant,
            uploaded_by=user,
            template=template,
            dataset_type=DatasetTypeChoices.CX_USERS,
            file=SimpleUploadedFile("gate.csv", b"Name\nValid\n\n"),
            file_format=FileFormatChoices.CSV,
            original_filename="gate.csv",
            total_rows=2,
            total_columns=1,
            uploaded_headers=["Name"],
            preview_rows=[{"Name": "Valid"}, {"Name": ""}],
        )

        result = validate_import_batch(batch)
        batch.refresh_from_db()

        self.assertEqual(result["summary"]["error_count"], 1)
        self.assertTrue(batch.has_critical_errors)
        self.assertFalse(batch.is_ready_for_import)
        self.assertEqual(batch.status, ImportBatchStatusChoices.VALIDATION_FAILED)


class ImportBatchCancellationTests(TestCase):
    def setUp(self):
        self.school = make_school(slug="cancel-import-school")
        self.branch = make_branch(self.school)
        self.user = make_school_admin(
            self.branch,
            email="cancel-import@test.com",
        )
        role = make_role(self.school.tenant, name="Import uploader")
        make_role_permission(role, make_permission("import.batches.create"))
        make_assignment(self.school.tenant, self.user, role)
        self.client = TenantAPIClient(user=self.user)
        self.template = ImportTemplate.objects.create(
            code="cancel-import-template",
            name="Cancel Import",
            dataset_type=DatasetTypeChoices.CX_USERS,
            default_file_format=FileFormatChoices.CSV,
        )

    def make_batch(self, *, status=ImportBatchStatusChoices.READY_TO_IMPORT):
        return ImportBatch.objects.create(
            tenant=self.school.tenant,
            uploaded_by=self.user,
            template=self.template,
            dataset_type=DatasetTypeChoices.CX_USERS,
            file=SimpleUploadedFile("cancel.csv", b"Name\nOne\n"),
            file_format=FileFormatChoices.CSV,
            original_filename="cancel.csv",
            status=status,
            total_rows=1,
            total_columns=1,
            uploaded_headers=["Name"],
            preview_rows=[{"Name": "One"}],
            is_ready_for_import=status == ImportBatchStatusChoices.READY_TO_IMPORT,
        )

    def test_uploader_can_cancel_before_execution(self):
        batch = self.make_batch()

        response = self.client.post(f"/v1/import/batches/{batch.pk}/cancel/")

        self.assertEqual(response.status_code, 200, response.content)
        batch.refresh_from_db()
        self.assertEqual(batch.status, ImportBatchStatusChoices.CANCELLED)
        self.assertFalse(batch.is_ready_for_import)

    def test_running_import_cannot_be_cancelled(self):
        batch = self.make_batch(status=ImportBatchStatusChoices.IMPORT_RUNNING)

        response = self.client.post(f"/v1/import/batches/{batch.pk}/cancel/")

        self.assertEqual(response.status_code, 400, response.content)
        batch.refresh_from_db()
        self.assertEqual(batch.status, ImportBatchStatusChoices.IMPORT_RUNNING)

    def test_other_tenant_cannot_cancel_batch_by_id(self):
        batch = self.make_batch()
        other_school = make_school(slug="other-cancel-import-school")
        other_branch = make_branch(other_school)
        other_user = make_school_admin(
            other_branch,
            email="other-cancel-import@test.com",
        )
        role = make_role(other_school.tenant, name="Other import uploader")
        make_role_permission(role, make_permission("import.batches.create"))
        make_assignment(other_school.tenant, other_user, role)
        other_client = TenantAPIClient(user=other_user)

        response = other_client.post(f"/v1/import/batches/{batch.pk}/cancel/")

        self.assertEqual(response.status_code, 404, response.content)
        batch.refresh_from_db()
        self.assertEqual(batch.status, ImportBatchStatusChoices.READY_TO_IMPORT)


class SchoolImportRollbackTests(TestCase):
    """Rolling back an imported school actually deletes it.

    ``ImportJobRowResult.target_object_pk`` records ``School.pk``. B23 moved that
    from the slug to a surrogate integer id, but the rollback kept matching on
    ``slug`` - so for every school imported since, the delete matched no rows and
    the rollback still reported success. Only the pk shape matters here, so the
    row result is a stub rather than a full job graph.
    """

    @staticmethod
    def _row(pk):
        from types import SimpleNamespace

        return SimpleNamespace(target_object_pk=str(pk))

    def test_rollback_deletes_a_school_recorded_by_its_surrogate_id(self):
        from schools.vs_schools.models import School
        from vs_import_data.services.rollback_service import reverse_target_record

        school = make_school(slug="rollback-by-id", name="Rollback By Id")

        self.assertTrue(reverse_target_record(self._row(school.pk)))
        self.assertFalse(School.objects.filter(pk=school.pk).exists())

    def test_rollback_still_deletes_a_school_recorded_by_slug_before_b23(self):
        """Rows written while the slug WAS the primary key must still reverse."""
        from schools.vs_schools.models import School
        from vs_import_data.services.rollback_service import reverse_target_record

        school = make_school(slug="rollback-by-slug", name="Rollback By Slug")

        self.assertTrue(reverse_target_record(self._row(school.slug)))
        self.assertFalse(School.objects.filter(pk=school.pk).exists())

    def test_rollback_of_an_unknown_reference_is_a_no_op_not_an_error(self):
        from schools.vs_schools.models import School
        from vs_import_data.services.rollback_service import reverse_target_record

        school = make_school(slug="rollback-untouched", name="Untouched")

        self.assertTrue(reverse_target_record(self._row("9" * 40)))
        self.assertTrue(reverse_target_record(self._row("")))
        self.assertTrue(School.objects.filter(pk=school.pk).exists())


class ImportAdminEmailScopeTests(TestCase):
    """The importer's "already exists" question is per tenant, not per platform.

    Unscoped it refused a legitimate row: Greenfield joins the platform with
    ada.okoye@example.test as its branch administrator, and the file was
    rejected because Bright Star - imported last term - already uses it.
    """

    def setUp(self):
        from unittest import mock

        self.tenant_required = lambda value=True: mock.patch(
            "vs_user.services.sign_in_scope.REQUIRE_TENANT_ON_SIGN_IN", value,
        )
        self.bright_star = make_school(slug="bright-star", name="Bright Star School")
        self.greenfield = make_school(slug="greenfield", name="Greenfield Academy")
        self.bright_star_branch = make_branch(self.bright_star)
        self.greenfield_branch = make_branch(self.greenfield)
        self.ada = make_school_admin(
            self.bright_star_branch, email="ada.okoye@example.test",
        )

    def _branches_batch(self, school=None, email="ada.okoye@example.test"):
        from types import SimpleNamespace

        template = ImportTemplate.objects.create(
            code=f"branches-scope-{school.pk if school else 'open'}",
            name="Branches",
            dataset_type=DatasetTypeChoices.BRANCHES,
            default_file_format=FileFormatChoices.CSV,
        )
        for target in ("school_slug", "name", "branch_admin_email",
                       "branch_admin_full_name", "is_main"):
            ImportTemplateColumn.objects.create(
                template=template, column_name=target, target_field=target,
                data_type=TemplateColumnDataTypeChoices.STRING,
            )
        return SimpleNamespace(
            template=template,
            school=school,
            preview_rows=[{
                "school_slug": "" if school else self.greenfield.slug,
                "name": "Annexe",
                "branch_admin_email": email,
                "branch_admin_full_name": "Ada Okoye",
                "is_main": "FALSE",
            }],
        )

    def _duplicate_issues(self, issues):
        return [i for i in issues if i["code"] == "duplicate_record"]

    def test_an_address_held_by_another_tenant_is_accepted(self):
        from .services.validation_service import _validate_branches_rules

        with self.tenant_required():
            issues = _validate_branches_rules(self._branches_batch())

        self.assertEqual(self._duplicate_issues(issues), [])

    def test_an_address_held_in_the_same_tenant_is_refused(self):
        from .services.validation_service import _validate_branches_rules

        with self.tenant_required():
            issues = _validate_branches_rules(
                self._branches_batch(school=self.bright_star),
            )

        self.assertEqual(len(self._duplicate_issues(issues)), 1)
        self.assertEqual(
            self._duplicate_issues(issues)[0]["column_name"], "branch_admin_email",
        )

    def test_it_is_still_refused_across_tenants_while_the_switch_is_off(self):
        """The pre-check must not be laxer than ``User.save``'s own guard, or
        the row would pass validation and blow up during execution."""
        from .services.validation_service import _validate_branches_rules

        issues = _validate_branches_rules(self._branches_batch())

        self.assertEqual(len(self._duplicate_issues(issues)), 1)

    def _schools_batch(self, email="ada.okoye@example.test"):
        from types import SimpleNamespace

        template = ImportTemplate.objects.create(
            code="schools-scope",
            name="Schools",
            dataset_type=DatasetTypeChoices.SCHOOLS,
            default_file_format=FileFormatChoices.CSV,
        )
        for target in ("name", "slug", "school_admin_email", "school_admin_full_name"):
            ImportTemplateColumn.objects.create(
                template=template, column_name=target, target_field=target,
                data_type=TemplateColumnDataTypeChoices.STRING,
            )
        return SimpleNamespace(
            template=template,
            school=None,
            preview_rows=[{
                "name": "Sunrise College",
                "slug": "sunrise",
                "school_admin_email": email,
                "school_admin_full_name": "Ada Okoye",
            }],
        )

    def test_a_new_school_may_reuse_an_address_once_the_switch_is_on(self):
        """Every schools-import row creates a brand-new tenant, so there is no
        tenant the address can already be taken in."""
        from .services.validation_service import _validate_schools_rules

        with self.tenant_required():
            issues = _validate_schools_rules(self._schools_batch())

        self.assertEqual(self._duplicate_issues(issues), [])

    def test_a_new_school_is_still_refused_while_the_switch_is_off(self):
        from .services.validation_service import _validate_schools_rules

        issues = _validate_schools_rules(self._schools_batch())

        self.assertEqual(len(self._duplicate_issues(issues)), 1)

    def test_a_within_file_repeat_is_still_caught(self):
        """The scoping change must not weaken the row-against-row rule."""
        from .services.validation_service import _validate_schools_rules

        batch = self._schools_batch(email="fresh@example.test")
        batch.preview_rows.append(dict(batch.preview_rows[0], slug="sunset",
                                       name="Sunset College"))

        with self.tenant_required():
            issues = _validate_schools_rules(batch)

        self.assertEqual(len(self._duplicate_issues(issues)), 1)
        self.assertEqual(self._duplicate_issues(issues)[0]["row_number"], 2)


class ImportDoesNotWriteARoleLabelTests(TestCase):
    """The importer no longer stamps a persona onto the admin link records.

    Both handlers used to put a hardcoded ``role_label`` of "SCHOOL_ADMIN" or
    "BRANCH_ADMIN" into the payload they hand to the school serializers. The
    value never came from the sheet - it was a constant - and the column it
    fed was read by nothing, so it is gone.

    No official template ever offered ``role_label`` as a column: both handlers
    build their payload from a fixed set of keys they name in their docstrings
    (``school_admin_role`` and ``branch_admin_role``, which carry the human job
    title), so a hand-built template declaring the field was already inert. The
    last test pins that, because "already inert" is the reason a sheet still
    carrying it needs no migration path.
    """

    @classmethod
    def setUpTestData(cls):
        cls.queued_by = make_vision_user(
            email="import-role-label@example.com", super_admin=True,
        )

    @staticmethod
    def _batch(school=None):
        from types import SimpleNamespace

        return SimpleNamespace(school=school, template=None)

    def test_a_school_row_still_imports_and_records_both_job_titles(self):
        from schools.vs_schools.models import (
            BranchPrimaryAdmin, School, SchoolPrimaryAdmin,
        )
        from .services.import_executor import import_schools_row

        result = import_schools_row(
            import_batch=self._batch(),
            payload={
                "name": "Greenfield Academy",
                "slug": "import-greenfield",
                "school_admin_full_name": "Ada Okoye",
                "school_admin_email": "ada@import-greenfield.test",
                "school_admin_role": "Director of ICT",
                "branch_name": "Main Branch",
                "branch_admin_full_name": "Tunde Bello",
                "branch_admin_email": "tunde@import-greenfield.test",
                "branch_admin_role": "Principal",
            },
            queued_by=self.queued_by,
        )

        self.assertIsNotNone(result.instance, result.message)
        school = School.objects.get(slug="import-greenfield")

        self.assertEqual(
            SchoolPrimaryAdmin.objects.get(school=school).school_role,
            "Director of ICT",
        )
        self.assertEqual(
            BranchPrimaryAdmin.objects.get(branch=school.main_branch).branch_role,
            "Principal",
        )

    def test_a_school_row_that_omits_the_job_titles_falls_back_to_defaults(self):
        from schools.vs_schools.models import (
            BranchPrimaryAdmin, School, SchoolPrimaryAdmin,
        )
        from .services.import_executor import import_schools_row

        result = import_schools_row(
            import_batch=self._batch(),
            payload={
                "name": "Bare Minimum",
                "slug": "import-bare",
                "school_admin_full_name": "Ada Okoye",
                "school_admin_email": "ada@import-bare.test",
                "branch_admin_full_name": "Tunde Bello",
                "branch_admin_email": "tunde@import-bare.test",
            },
            queued_by=self.queued_by,
        )

        self.assertIsNotNone(result.instance, result.message)
        school = School.objects.get(slug="import-bare")

        self.assertEqual(
            SchoolPrimaryAdmin.objects.get(school=school).school_role, "IT Head",
        )
        self.assertEqual(
            BranchPrimaryAdmin.objects.get(branch=school.main_branch).branch_role,
            "Head Teacher",
        )

    def test_a_branch_row_still_imports_into_an_existing_school(self):
        from schools.vs_schools.models import BranchPrimaryAdmin
        from .services.import_executor import import_branches_row

        school = make_school(slug="import-annexe", name="Import Annexe School")
        make_branch(school, name="HQ")

        result = import_branches_row(
            import_batch=self._batch(school=school),
            payload={
                "name": "Lekki Annexe",
                "branch_admin_full_name": "Chidi Eze",
                "branch_admin_email": "chidi@import-annexe.test",
                "branch_admin_role": "Vice Principal",
                "is_main": "false",
            },
            queued_by=self.queued_by,
        )

        self.assertIsNotNone(result.instance, result.message)
        self.assertEqual(
            BranchPrimaryAdmin.objects.get(branch=result.instance).branch_role,
            "Vice Principal",
        )

    def test_a_sheet_still_carrying_a_role_label_column_imports_unchanged(self):
        """A stray column is mapped into the payload and then simply not read.

        Bright Star re-uploads last term's spreadsheet, which still has a
        ``role_label`` header reading "BRANCH_ADMIN". The handler builds its
        payload from the keys it names, so the value is carried as far as the
        payload dict and goes no further: the school is created exactly as it
        would be without the column, and no refusal is owed to the uploader.
        """
        from schools.vs_schools.models import School, SchoolPrimaryAdmin
        from .services.import_executor import import_schools_row

        result = import_schools_row(
            import_batch=self._batch(),
            payload={
                "name": "Stale Template",
                "slug": "import-stale",
                "school_admin_full_name": "Ada Okoye",
                "school_admin_email": "ada@import-stale.test",
                "school_admin_role": "IT Head",
                "branch_admin_full_name": "Tunde Bello",
                "branch_admin_email": "tunde@import-stale.test",
                "role_label": "BRANCH_ADMIN",
            },
            queued_by=self.queued_by,
        )

        self.assertIsNotNone(result.instance, result.message)
        school = School.objects.get(slug="import-stale")
        link = SchoolPrimaryAdmin.objects.get(school=school)

        self.assertEqual(link.school_role, "IT Head")
        self.assertFalse(hasattr(link, "role_label"))

    def test_an_unknown_header_is_a_warning_not_a_refusal(self):
        """What an uploader is told about the stale column, at header check.

        ``compare_uploaded_headers_to_template`` is where a sheet's headers meet
        the official template. A header the template does not declare is a
        ``column_unknown`` **warning**, so the upload proceeds. That is the
        right answer here: the field was never a template column, so no sheet
        was ever meant to carry it, and refusing one now would fail an upload
        over a value that has been ignored all along.
        """
        from .services.template_validation import (
            compare_uploaded_headers_to_template,
        )

        template = ImportTemplate.objects.create(
            code="schools-stale-header",
            name="Schools",
            dataset_type=DatasetTypeChoices.SCHOOLS,
            default_file_format=FileFormatChoices.CSV,
        )
        for target in ("name", "school_admin_email", "school_admin_role"):
            ImportTemplateColumn.objects.create(
                template=template, column_name=target, target_field=target,
                data_type=TemplateColumnDataTypeChoices.STRING,
            )

        issues = compare_uploaded_headers_to_template(
            ["name", "school_admin_email", "school_admin_role", "role_label"],
            template,
        )

        self.assertEqual(len(issues), 1)
        self.assertEqual(issues[0]["code"], "column_unknown")
        self.assertEqual(issues[0]["severity"], "warning")
        self.assertEqual(issues[0]["column_name"], "role_label")

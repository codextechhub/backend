"""Pre-execution validation for the three organogram import datasets."""
from types import SimpleNamespace

from django.test import TestCase

from vs_import_data.models import DatasetTypeChoices, ImportTemplate
from vs_rbac.models import TenantRoleTemplate
from vs_rbac.tests.helpers import codex_tenant
from vs_user.imports import validate_organogram_import
from vs_user.models import OrgNode, Position


class OrganogramImportValidationTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.tenant = codex_tenant()
        cls.role = TenantRoleTemplate.objects.create(
            tenant=cls.tenant,
            key="validation_role",
            name="Validation Role",
        )
        cls.division = OrgNode.objects.create(
            code="VALIDATION",
            name="Validation Division",
            kind=OrgNode.Kind.DIVISION,
        )
        cls.department = OrgNode.objects.create(
            code="VALIDATION",
            name="Validation Department",
            kind=OrgNode.Kind.DEPARTMENT,
            parent=cls.division,
        )
        cls.chief = Position.objects.create(
            code="VALIDATION-CHIEF",
            title="Validation Chief",
            org_node=cls.division,
        )
        cls.worker = Position.objects.create(
            code="VALIDATION-WORKER",
            title="Validation Worker",
            org_node=cls.department,
            reports_to=cls.chief,
        )

    def _validate(self, dataset_type, payloads):
        template = ImportTemplate.objects.prefetch_related("columns").get(
            dataset_type=dataset_type,
        )
        headers = {
            column.target_field: column.column_name
            for column in template.columns.all()
        }
        rows = [
            {headers.get(field, field): value for field, value in payload.items()}
            for payload in payloads
        ]
        batch = SimpleNamespace(
            template=template,
            tenant=self.tenant,
            preview_rows=rows,
        )
        return validate_organogram_import(batch)

    def assert_issue(self, issues, row_number, column_name, code):
        self.assertTrue(
            any(
                issue["row_number"] == row_number
                and issue["column_name"] == column_name
                and issue["code"] == code
                for issue in issues
            ),
            issues,
        )

    def test_valid_ordered_rows_pass_for_all_three_datasets(self):
        org_issues = self._validate(DatasetTypeChoices.ORG_UNITS, [
            {
                "code": "ORDERED",
                "name": "Ordered Division",
                "kind": "DIVISION",
            },
            {
                "code": "ORDERED",
                "name": "Ordered Department",
                "kind": "DEPARTMENT",
                "parent_code": "DV-ORDERED",
            },
        ])
        position_issues = self._validate(DatasetTypeChoices.POSITIONS, [
            {
                "code": "ORDERED-LEAD",
                "title": "Ordered Lead",
                "org_unit_code": self.division.code,
                "default_role_key": self.role.key,
                "headcount": "2",
            },
            {
                "code": "ORDERED-REPORT",
                "title": "Ordered Report",
                "org_unit_code": self.department.code,
                "reports_to_code": "ORDERED-LEAD",
            },
        ])
        matrix_issues = self._validate(DatasetTypeChoices.MATRIX_REPORTS, [{
            "position_code": self.worker.code,
            "reports_to_code": self.chief.code,
            "relationship_label": "Oversight",
        }])

        self.assertEqual(org_issues, [])
        self.assertEqual(position_issues, [])
        self.assertEqual(matrix_issues, [])

    def test_org_units_reject_parent_and_uniqueness_errors(self):
        issues = self._validate(DatasetTypeChoices.ORG_UNITS, [
            {
                "code": "TOP-WITH-PARENT",
                "name": "Top With Parent",
                "kind": "DIVISION",
                "parent_code": self.division.code,
            },
            {
                "code": "MISSING-PARENT",
                "name": "Missing Parent",
                "kind": "DEPARTMENT",
            },
            {
                "code": "LATE-CHILD",
                "name": "Late Child",
                "kind": "DEPARTMENT",
                "parent_code": "DV-LATE-PARENT",
            },
            {
                "code": "LATE-PARENT",
                "name": "Late Parent",
                "kind": "DIVISION",
            },
            {
                "code": "SAME-NAME",
                "name": self.division.name,
                "kind": "DIVISION",
            },
            {
                "code": "NORMALIZED",
                "name": "Normalized One",
                "kind": "DIVISION",
            },
            {
                "code": "DV-NORMALIZED",
                "name": "Normalized Two",
                "kind": "DIVISION",
            },
        ])

        self.assert_issue(issues, 1, "Parent Code", "business_rule")
        self.assert_issue(issues, 2, "Parent Code", "required_value_missing")
        self.assert_issue(issues, 3, "Parent Code", "cross_reference_missing")
        self.assert_issue(issues, 5, "Name", "duplicate_record")
        self.assert_issue(issues, 7, "Code", "duplicate_record")

    def test_invalid_org_unit_is_not_available_to_a_later_child(self):
        issues = self._validate(DatasetTypeChoices.ORG_UNITS, [
            {
                "code": "REJECTED-PARENT",
                "name": self.division.name,
                "kind": "DIVISION",
            },
            {
                "code": "REJECTED-CHILD",
                "name": "Rejected Child",
                "kind": "DEPARTMENT",
                "parent_code": "DV-REJECTED-PARENT",
            },
        ])

        self.assert_issue(issues, 1, "Name", "duplicate_record")
        self.assert_issue(issues, 2, "Parent Code", "cross_reference_missing")

    def test_positions_reject_reference_range_duplicate_and_graph_errors(self):
        issues = self._validate(DatasetTypeChoices.POSITIONS, [
            {
                "code": "BROKEN-REFS",
                "title": "Broken References",
                "org_unit_code": "DV-NOT-THERE",
                "reports_to_code": "NOT-THERE",
                "default_role_key": "not-a-role",
                "headcount": "0",
            },
            {
                "code": "SELF-REPORT",
                "title": "Self Report",
                "org_unit_code": self.division.code,
                "reports_to_code": "self-report",
                "default_role_key": self.role.key.upper(),
            },
            {
                "code": self.chief.code,
                "title": self.chief.title,
                "org_unit_code": self.division.code,
                "reports_to_code": self.worker.code,
            },
            {
                "code": "DUPLICATE-POSITION",
                "title": "Duplicate One",
                "org_unit_code": self.division.code,
            },
            {
                "code": "duplicate-position",
                "title": "Duplicate Two",
                "org_unit_code": self.division.code,
            },
        ])

        self.assert_issue(issues, 1, "Org Unit Code", "cross_reference_missing")
        self.assert_issue(issues, 1, "Reports To Code", "cross_reference_missing")
        self.assert_issue(issues, 1, "Default Role Key", "cross_reference_missing")
        self.assert_issue(issues, 1, "Headcount", "business_rule")
        self.assert_issue(issues, 2, "Reports To Code", "business_rule")
        self.assert_issue(issues, 2, "Default Role Key", "cross_reference_missing")
        self.assert_issue(issues, 3, "Reports To Code", "business_rule")
        self.assert_issue(issues, 5, "Code", "duplicate_record")

    def test_invalid_position_is_not_available_to_a_later_report(self):
        issues = self._validate(DatasetTypeChoices.POSITIONS, [
            {
                "code": "REJECTED-MANAGER",
                "title": "Rejected Manager",
                "org_unit_code": "DV-NOT-THERE",
            },
            {
                "code": "REJECTED-REPORT",
                "title": "Rejected Report",
                "org_unit_code": self.division.code,
                "reports_to_code": "REJECTED-MANAGER",
            },
        ])

        self.assert_issue(issues, 1, "Org Unit Code", "cross_reference_missing")
        self.assert_issue(issues, 2, "Reports To Code", "cross_reference_missing")

    def test_matrix_lines_reject_missing_self_and_duplicate_pairs(self):
        issues = self._validate(DatasetTypeChoices.MATRIX_REPORTS, [
            {
                "position_code": "NOT-THERE",
                "reports_to_code": "ALSO-NOT-THERE",
            },
            {
                "position_code": self.chief.code,
                "reports_to_code": self.chief.code,
            },
            {
                "position_code": self.worker.code,
                "reports_to_code": self.chief.code,
            },
            {
                "position_code": self.worker.code.lower(),
                "reports_to_code": self.chief.code.lower(),
            },
        ])

        self.assert_issue(issues, 1, "Position Code", "cross_reference_missing")
        self.assert_issue(issues, 1, "Reports To Code", "cross_reference_missing")
        self.assert_issue(issues, 2, "Reports To Code", "business_rule")
        self.assert_issue(issues, 4, "Reports To Code", "duplicate_record")

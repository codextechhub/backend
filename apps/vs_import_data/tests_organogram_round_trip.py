"""Portable platform organogram imports and export round trips."""
import csv
import io
from types import SimpleNamespace

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.test import TestCase

from vs_exports.catalogue import ScopeContext, get_dataset
from vs_exports.constants import ValuesMode
from vs_exports.engine import produce
from vs_import_data.models import DatasetTypeChoices, ImportTemplate
from vs_import_data.services.import_executor import (
    execute_dataset_handler,
    map_row_to_payload,
)
from vs_import_data.services.reversers import REVERTED, reverse_row
from vs_rbac.models import TenantRoleTemplate, TenantUserRoleAssignment
from vs_tenants.models import Tenant
from vs_user.models import (
    MatrixReport,
    OrgNode,
    PlatformStaffProfile,
    Position,
    PositionAssignment,
)

User = get_user_model()


class OrganogramRoundTripTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        call_command("seed_actions", verbosity=0)
        call_command("seed_platform_permissions", verbosity=0)
        call_command("seed_exports_permissions", verbosity=0)
        call_command("seed_import", verbosity=0)
        cls.tenant = Tenant.objects.get(slug="codex", kind=Tenant.Kind.PLATFORM)
        cls.super_role, _ = TenantRoleTemplate.objects.get_or_create(
            tenant=cls.tenant, key="xvs_super_admin",
            defaults={"name": "XVS Super Admin", "status": "ACTIVE"},
        )
        cls.staff_role, _ = TenantRoleTemplate.objects.get_or_create(
            tenant=cls.tenant, key="round_trip_staff",
            defaults={"name": "Round Trip Staff", "status": "ACTIVE"},
        )
        cls.operator = User.objects.create_user(
            email="roundtrip.operator@codex.test", password="testpass123",
            first_name="Round", last_name="Operator", tenant=cls.tenant,
            status=User.Status.ACTIVE,
        )
        TenantUserRoleAssignment.objects.create(
            tenant=cls.tenant, user=cls.operator, role=cls.super_role,
            assignment_status=TenantUserRoleAssignment.AssignmentStatus.ACTIVE,
        )

    def _export_rows(self, key, *, filters=None):
        dataset = get_dataset(key)
        body, headers, _fields, _count, _omissions = produce(
            self.operator,
            {
                "dataset_key": key,
                "columns": list(dataset.default_columns),
                "filters": filters or [],
                "sort": [],
                "format": "csv",
                "format_options": {},
                "values_mode": ValuesMode.SYSTEM,
            },
            ScopeContext(tenant=self.tenant, user=self.operator),
            self.tenant,
        )
        return headers, list(csv.DictReader(io.StringIO(body.decode("utf-8"))))

    def _import_rows(self, dataset_type, rows):
        template = ImportTemplate.objects.get(dataset_type=dataset_type)
        batch = SimpleNamespace(template=template, tenant=self.tenant, branch=None)
        return [
            execute_dataset_handler(
                batch, map_row_to_payload(batch, row), self.operator,
            )
            for row in rows
        ]

    def test_system_exports_import_into_an_empty_organogram(self):
        division = OrgNode.objects.create(
            code="ROUND", name="Z Round Trip Division", kind=OrgNode.Kind.DIVISION,
        )
        department = OrgNode(
            code="ROUND", name="A Round Trip Department",
            kind=OrgNode.Kind.DEPARTMENT, parent=division,
        )
        department.full_clean()
        department.save()
        chief = Position.objects.create(
            code="ROUND-CHIEF", title="Z Round Trip Chief", org_node=division,
            default_role=self.staff_role,
        )
        analyst = Position(
            code="ROUND-ANALYST", title="A Round Trip Analyst",
            org_node=department, reports_to=chief, default_role=self.staff_role,
            headcount=2,
        )
        analyst.full_clean()
        analyst.save()
        MatrixReport.objects.create(
            position=analyst, reports_to=chief,
            relationship_label="Operational oversight",
        )
        staff = User.objects.create_user(
            email="portable.staff@codex.test", first_name="Portable", last_name="Staff",
            tenant=self.tenant, status=User.Status.ACTIVE, gender=User.Gender.FEMALE,
            phone="+2348011112222", role=self.staff_role.name,
        )
        TenantUserRoleAssignment.objects.create(
            tenant=self.tenant, user=staff, role=self.staff_role,
            assignment_status=TenantUserRoleAssignment.AssignmentStatus.ACTIVE,
        )
        PlatformStaffProfile.objects.create(
            user=staff, position=analyst, employment_type="FULL_TIME",
            date_joined="2026-09-01", employee_id="CX-ROUNDTRIP",
            job_title=analyst.title,
        )
        PositionAssignment.objects.create(user=staff, position=analyst)

        _headers, org_rows = self._export_rows("admin.org_units")
        _headers, position_rows = self._export_rows("admin.positions")
        _headers, matrix_rows = self._export_rows("admin.matrix_reports")
        user_headers, user_rows = self._export_rows(
            "admin.users",
            filters=[{"id": "email", "value": "portable.staff@codex.test"}],
        )
        self.assertEqual(
            user_headers,
            [
                "Email", "First Name", "Last Name", "Role Key", "Phone", "Gender",
                "Employment Type", "Position", "Date Joined",
            ],
        )

        staff.delete()
        MatrixReport.objects.all().delete()
        Position.objects.all().delete()
        OrgNode.objects.filter(kind=OrgNode.Kind.TEAM).delete()
        OrgNode.objects.filter(kind=OrgNode.Kind.DEPARTMENT).delete()
        OrgNode.objects.filter(kind=OrgNode.Kind.DIVISION).delete()

        self._import_rows(DatasetTypeChoices.ORG_UNITS, org_rows)
        self._import_rows(DatasetTypeChoices.POSITIONS, position_rows)
        self._import_rows(DatasetTypeChoices.MATRIX_REPORTS, matrix_rows)
        self._import_rows(DatasetTypeChoices.CX_USERS, user_rows)

        imported_department = OrgNode.objects.get(code=department.code)
        imported_analyst = Position.objects.get(code=analyst.code)
        imported_staff = User.objects.get(email="portable.staff@codex.test")
        self.assertEqual(imported_department.parent.code, division.code)
        self.assertEqual(imported_analyst.reports_to.code, chief.code)
        self.assertEqual(imported_analyst.default_role.key, self.staff_role.key)
        self.assertEqual(
            MatrixReport.objects.get(position=imported_analyst).reports_to.code,
            chief.code,
        )
        self.assertEqual(
            PositionAssignment.objects.get(
                user=imported_staff, end_date__isnull=True,
            ).position.code,
            analyst.code,
        )
        self.assertEqual(imported_staff.phone, "+2348011112222")
        self.assertEqual(imported_staff.gender, User.Gender.FEMALE)
        self.assertEqual(
            imported_staff.platform_staff_profile.employment_type, "FULL_TIME",
        )

    def test_reimport_updates_stable_keys_without_duplicates(self):
        batch = SimpleNamespace(tenant=self.tenant)
        from vs_user.imports import import_org_unit_row

        first = import_org_unit_row(batch, {
            "code": "REIMPORT", "name": "Original", "kind": "DIVISION",
            "is_active": "Yes",
        }, self.operator)
        second = import_org_unit_row(batch, {
            "code": "REIMPORT", "name": "Renamed", "kind": "DIVISION",
            "is_active": "No",
        }, self.operator)
        self.assertEqual(OrgNode.objects.filter(code=first.instance.code).count(), 1)
        self.assertEqual(second.instance.name, "Renamed")
        self.assertFalse(second.instance.is_active)

    def test_each_created_organogram_row_has_a_safe_reverser(self):
        node = OrgNode.objects.create(
            code="REVERSE", name="Reverse Division", kind=OrgNode.Kind.DIVISION,
        )
        first = Position.objects.create(
            code="REVERSE-A", title="Reverse A", org_node=node,
        )
        second = Position.objects.create(
            code="REVERSE-B", title="Reverse B", org_node=node,
        )
        line = MatrixReport.objects.create(position=second, reports_to=first)

        def row(model, instance, payload, number):
            return SimpleNamespace(
                row_number=number, target_model=model,
                target_object_pk=str(instance.pk), normalized_payload=payload,
                action="create",
            )

        outcomes = [
            reverse_row(row("MatrixReport", line, {
                "position_code": second.code, "reports_to_code": first.code,
            }, 1)),
            reverse_row(row("Position", second, {"code": second.code}, 2)),
            reverse_row(row("Position", first, {"code": first.code}, 3)),
            reverse_row(row("OrgNode", node, {"code": node.code}, 4)),
        ]
        self.assertEqual([outcome.status for outcome in outcomes], [REVERTED] * 4)

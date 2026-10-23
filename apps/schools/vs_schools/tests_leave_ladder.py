"""A school is created with its leave approval ladder.

Leave has no platform fallback, so a school without its own ladder refuses the
first leave anybody files. Creation provisions it the way it provisions books:
best effort, in a savepoint of its own, so a fault in the ladder cannot cost the
school.
"""
from __future__ import annotations

from unittest.mock import patch

from django.test import TestCase

from vs_tenants.models import Branch
from vs_workflow.models import WorkflowTemplate

from .models import School
from .tests_books import _failing_statement, _SchoolCreationMixin


class SchoolCreationLeaveLadderTests(_SchoolCreationMixin, TestCase):
    def ladder(self, school):
        return WorkflowTemplate.all_objects.filter(
            tenant=school.tenant, document_type="schools.leave_request",
        )

    def test_creating_a_school_publishes_its_line_manager_leave_ladder(self):
        self._create({"name": "Corona Secondary", "slug": "corona-secondary"})
        school = School.objects.get(slug="corona-secondary")

        [template] = self.ladder(school)
        [stage] = template.stages.filter(retired_at__isnull=True)
        self.assertEqual(
            (stage.approver_source, stage.organogram_target, stage.approver_scope),
            ("ORGANOGRAM", "DIRECT_MANAGER", "SCHOOL"),
        )

    def test_a_school_with_several_branches_gets_one_school_wide_ladder(self):
        self._create({
            "name": "Greenfield",
            "slug": "greenfield",
            "branches": [
                self._branch("Main Branch", is_main=True, email="main@greenfield.test"),
                self._branch("Annex", is_main=False, email="annex@greenfield.test"),
            ],
        })
        school = School.objects.get(slug="greenfield")

        self.assertEqual(Branch.objects.filter(tenant=school.tenant).count(), 2)
        [template] = self.ladder(school)
        self.assertIsNone(template.branch_id)

    def test_a_ladder_failure_does_not_cost_us_the_school(self):
        with patch(
            "schools.vs_staff.approvals.ensure_tenant_approval_templates",
            side_effect=_failing_statement,
        ):
            response = self._create({"name": "Resilient", "slug": "resilient"})

        self.assertTrue(response.data["success"])
        school = School.objects.get(slug="resilient")
        self.assertTrue(Branch.objects.filter(tenant=school.tenant).exists())
        self.assertFalse(self.ladder(school).exists())

    def test_the_failure_is_logged_with_the_school_so_it_can_be_found(self):
        with patch(
            "schools.vs_staff.approvals.ensure_tenant_approval_templates",
            side_effect=RuntimeError("the workflow engine is on fire"),
        ):
            with self.assertLogs("schools.vs_staff.approvals", level="ERROR") as logs:
                self._create({"name": "Noisy", "slug": "noisy"})

        self.assertTrue(any("noisy" in line for line in logs.output))

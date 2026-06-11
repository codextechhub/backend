"""Tests for vs_todo — the ToDo org-accountability tool.

Covers the two things the design is really about: the organogram-derived
hierarchy (roll-up + assign-down rules) and task status/stats. The org tree is
built from the real vs_user organogram models, so these also pin the contract
this app depends on.
"""
from __future__ import annotations

from datetime import timedelta

from django.test import TestCase
from django.utils import timezone
from rest_framework.exceptions import PermissionDenied

from vs_user.models import (
    OrgNode, Position, PositionAssignment, User,
)

from .constants import Priority, TaskStatus
from .models import Task
from .services import dashboards as dashboards_svc
from .services import tasks as tasks_svc
from .services.hierarchy import TodoHierarchy
from .services.stats import area_tasks_qs, stats_for


def _staff(email, first, last, **extra):
    return User.objects.create_user(
        email=email, first_name=first, last_name=last,
        user_type=User.UserType.CX_STAFF, status=User.Status.ACTIVE, **extra,
    )


class OrganogramFixtureMixin:
    """A small three-tier CX org:  md → head → member."""

    def build_org(self):
        self.division = OrgNode.objects.create(
            name="Executive", code="EXE", kind=OrgNode.Kind.DIVISION,
        )
        self.department = OrgNode.objects.create(
            name="Sales", code="SAL", kind=OrgNode.Kind.DEPARTMENT, parent=self.division,
        )

        self.md = _staff("md@cx.test", "Ada", "Director")
        self.head = _staff("head@cx.test", "Chidi", "Head")
        self.member = _staff("member@cx.test", "Tobi", "Member")
        self.outsider = _staff("out@cx.test", "Sola", "Outsider")

        self.pos_md = Position.objects.create(
            title="Managing Director", code="MD", org_node=self.division,
        )
        self.pos_head = Position.objects.create(
            title="Head of Sales", code="HOS", org_node=self.department,
            reports_to=self.pos_md,
        )
        self.pos_member = Position.objects.create(
            title="Sales Rep", code="REP", org_node=self.department,
            reports_to=self.pos_head,
        )
        self.pos_out = Position.objects.create(
            title="Lone Wolf", code="LW", org_node=self.division,
        )

        for user, pos in [
            (self.md, self.pos_md), (self.head, self.pos_head),
            (self.member, self.pos_member), (self.outsider, self.pos_out),
        ]:
            PositionAssignment.objects.create(user=user, position=pos, is_primary=True)


class HierarchyTests(OrganogramFixtureMixin, TestCase):
    def setUp(self):
        self.build_org()

    def test_descendants_roll_up_the_whole_subtree(self):
        ids = {u.pk for u in TodoHierarchy.descendant_users(self.md)}
        self.assertEqual(ids, {self.head.pk, self.member.pk})

    def test_direct_reports_are_one_level_only(self):
        ids = {u.pk for u in TodoHierarchy.direct_report_users(self.md)}
        self.assertEqual(ids, {self.head.pk})

    def test_area_includes_self_plus_descendants(self):
        self.assertEqual(
            TodoHierarchy.area_user_ids(self.head),
            {self.head.pk, self.member.pk},
        )

    def test_is_manager(self):
        self.assertTrue(TodoHierarchy.is_manager(self.md))
        self.assertTrue(TodoHierarchy.is_manager(self.head))
        self.assertFalse(TodoHierarchy.is_manager(self.member))

    def test_can_assign_only_downward(self):
        self.assertTrue(TodoHierarchy.can_assign(self.md, self.member))
        self.assertTrue(TodoHierarchy.can_assign(self.head, self.member))
        self.assertFalse(TodoHierarchy.can_assign(self.member, self.head))  # up
        self.assertFalse(TodoHierarchy.can_assign(self.head, self.md))      # up
        self.assertFalse(TodoHierarchy.can_assign(self.head, self.outsider))  # sideways
        self.assertFalse(TodoHierarchy.can_assign(self.md, self.md))        # self

    def test_chain_to_builds_root_first_breadcrumb(self):
        chain = [u.pk for u in TodoHierarchy.chain_to(self.member)]
        self.assertEqual(chain, [self.md.pk, self.head.pk, self.member.pk])


class TaskStatusTests(OrganogramFixtureMixin, TestCase):
    def setUp(self):
        self.build_org()
        self.today = timezone.localdate()

    def test_status_derivation(self):
        done = Task.objects.create(
            assignee=self.member, title="done", deadline=self.today, is_done=True,
        )
        overdue = Task.objects.create(
            assignee=self.member, title="late", deadline=self.today - timedelta(days=1),
        )
        ongoing = Task.objects.create(
            assignee=self.member, title="soon", deadline=self.today + timedelta(days=3),
        )
        self.assertEqual(done.status, TaskStatus.COMPLETED)
        self.assertEqual(overdue.status, TaskStatus.OVERDUE)
        self.assertEqual(ongoing.status, TaskStatus.IN_PROGRESS)

    def test_stats_for_counts_and_pct(self):
        Task.objects.create(assignee=self.member, title="a", deadline=self.today, is_done=True)
        Task.objects.create(assignee=self.member, title="b", deadline=self.today, is_done=True)
        Task.objects.create(assignee=self.member, title="c", deadline=self.today - timedelta(days=1))
        stats = stats_for(Task.objects.filter(assignee=self.member))
        self.assertEqual(stats["total"], 3)
        self.assertEqual(stats["done"], 2)
        self.assertEqual(stats["overdue"], 1)
        self.assertEqual(stats["pct"], 67)


class TaskServiceTests(OrganogramFixtureMixin, TestCase):
    def setUp(self):
        self.build_org()
        self.today = timezone.localdate()

    def test_self_set_task_has_no_assigned_by(self):
        task = tasks_svc.create_task(
            actor=self.member, title="mine", deadline=self.today, priority=Priority.LOW,
        )
        self.assertIsNone(task.assigned_by_id)
        self.assertTrue(task.is_self_set)
        self.assertEqual(task.assignee_id, self.member.pk)

    def test_assignment_down_records_manager_and_snapshots_department(self):
        task = tasks_svc.create_task(
            actor=self.head, title="do it", deadline=self.today, assignee=self.member,
        )
        self.assertEqual(task.assigned_by_id, self.head.pk)
        self.assertEqual(task.assigned_by_name, self.head.full_name)

    def test_assignment_upward_is_rejected(self):
        with self.assertRaises(PermissionDenied):
            tasks_svc.create_task(
                actor=self.member, title="nope", deadline=self.today, assignee=self.head,
            )

    def test_set_done_stamps_completion(self):
        task = tasks_svc.create_task(actor=self.member, title="x", deadline=self.today)
        tasks_svc.set_done(task, done=True)
        self.assertTrue(task.is_done)
        self.assertIsNotNone(task.completed_at)
        tasks_svc.set_done(task, done=False)
        self.assertFalse(task.is_done)
        self.assertIsNone(task.completed_at)


class DashboardTests(OrganogramFixtureMixin, TestCase):
    def setUp(self):
        self.build_org()
        self.today = timezone.localdate()
        # member: 1 done, 1 open; head: 1 open
        Task.objects.create(assignee=self.member, title="m1", deadline=self.today, is_done=True)
        Task.objects.create(assignee=self.member, title="m2", deadline=self.today)
        Task.objects.create(assignee=self.head, title="h1", deadline=self.today)

    def test_area_tasks_roll_up(self):
        # head's area = head + member = 3 tasks
        self.assertEqual(area_tasks_qs(self.head).count(), 3)
        # md's area = everyone = 3 tasks
        self.assertEqual(area_tasks_qs(self.md).count(), 3)

    def test_node_dashboard_shape(self):
        data = dashboards_svc.node_dashboard(self.head)
        self.assertTrue(data["is_manager"])
        self.assertEqual(data["own_stats"]["total"], 1)
        self.assertEqual(data["area_stats"]["total"], 3)
        report_ids = {r["person"].pk for r in data["reports"]}
        self.assertEqual(report_ids, {self.member.pk})

    def test_org_rollup_tree(self):
        tree = dashboards_svc.org_rollup(self.md)
        self.assertEqual(tree["person"].pk, self.md.pk)
        self.assertEqual(tree["area_stats"]["total"], 3)
        head_node = tree["direct_reports"][0]
        self.assertEqual(head_node["person"].pk, self.head.pk)
        self.assertEqual(head_node["area_stats"]["total"], 3)

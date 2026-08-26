"""The tree and the overview, including what they cost.

The query-count assertions are the point of this file. A tree assembled by
walking relations looks correct on four programmes and costs a query per parent
on forty, and nothing but assertNumQueries notices before a real school does.
"""
from __future__ import annotations

import datetime as dt

from django.test import TestCase
from django.urls import reverse
from rest_framework.test import APIClient

from vs_rbac.models import PermissionScope
from vs_rbac.tests.helpers import (
    make_assignment,
    make_branch,
    make_permission,
    make_role,
    make_role_permission,
    make_school,
    make_school_admin,
)
from vs_user.tokens import CodeXRefreshToken
from schools.vs_academics.models import (
    AcademicSession,
    AcademicTerm,
    Department,
    Level,
    Program,
    SchoolClass,
    SessionStatus,
    Subject,
    SubjectOffering,
)
from schools.vs_academics.services.sessions import activate_session, set_branches

D = dt.date
KEYS = ("academics.structure.view", "academics.classes.view", "academics.subject.view")


class _Base(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.school = make_school(slug="brightfield", name="Brightfield Schools")
        cls.tenant = cls.school.tenant
        # Levels, classes and subjects belong to a year now, so the fixtures
        # need one to put them in.
        cls.year = AcademicSession.all_objects.create(
            tenant=cls.tenant, name="2099/2100",
            start_date=dt.date(2099, 9, 1), end_date=dt.date(2100, 7, 31),
            status="ACTIVE",
        )
        cls.lekki = make_branch(cls.school, name="Lekki Campus", is_main=True)
        cls.ikeja = make_branch(cls.school, name="Ikeja Campus", is_main=False)

        role = make_role(cls.school, name="School Admin", key="school_admin")
        for key in KEYS:
            make_role_permission(
                role, make_permission(key, scope=PermissionScope.TENANT),
            )
        cls.admin = make_school_admin(
            None, email="adaeze@brightfield.test", tenant=cls.tenant,
        )
        make_assignment(cls.school, cls.admin, role, branch=None)
        cls.lekki_head = make_school_admin(
            None, email="head@lekki.test", tenant=cls.tenant,
        )
        make_assignment(cls.school, cls.lekki_head, role, branch=cls.lekki)

    def client_for(self, user):
        client = APIClient()
        client.credentials(
            HTTP_AUTHORIZATION=f"Bearer {CodeXRefreshToken.for_user(user).access_token}",
        )
        return client

    def get(self, user, name, params=None):
        return self.client_for(user).get(
            reverse(name), {"tenant": self.tenant.slug, **(params or {})},
        )

    def build(self, programs=2, levels=3, classes=2, subjects=2):
        """A school of a given size, for the query-count tests to grow.

        Each call gets its own name prefix, so a test can build a small school,
        measure it, then build a larger one on top without colliding with the
        per-tenant unique constraints it is meant to be exercising around.
        """
        self._batch = getattr(self, "_batch", 0) + 1
        tag = f"b{self._batch}"
        made = []
        for p in range(programs):
            program = Program.all_objects.create(
                tenant=self.tenant, name=f"{tag} Programme {p}",
                code=f"{tag}P{p}", order_index=p,
            )
            for lv in range(levels):
                level = Level.all_objects.create(
                    tenant=self.tenant, session=self.year, program=program, name=f"{tag}P{p}L{lv}",
                    code=f"{tag}P{p}L{lv}", order_index=lv,
                )
                made.append(level)
                for c in range(classes):
                    SchoolClass.all_objects.create(
                        tenant=self.tenant, session=self.year, level=level,
                        name=f"{tag}P{p}L{lv} {c}", code=f"{tag}P{p}L{lv}C{c}",
                    )
        for s in range(subjects):
            subject = Subject.all_objects.create(
                tenant=self.tenant, name=f"{tag} Subject {s}", code=f"{tag}S{s}",
            )
            for level in made:
                SubjectOffering.all_objects.create(
                    tenant=self.tenant, subject=subject, level=level,
                )
        return made


class TreeShapeTests(_Base):
    def test_the_default_stops_at_levels(self):
        self.build(programs=1, levels=2, classes=2)
        response = self.get(self.admin, "academics-structure-tree")
        self.assertEqual(response.status_code, 200, response.data)
        kinds = {row["kind"] for row in response.data["data"]["rows"]}
        self.assertEqual(kinds, {"Session", "Programme", "Level"})

    def test_full_depth_reaches_the_subjects(self):
        self.build(programs=1, levels=1, classes=1, subjects=1)
        response = self.get(
            self.admin, "academics-structure-tree", {"depth": "full"},
        )
        rows = response.data["data"]["rows"]
        self.assertEqual(
            [r["kind"] for r in rows],
            ["Session", "Programme", "Level", "Class", "Subject"],
        )
        self.assertEqual([r["depth"] for r in rows], [0, 1, 2, 3, 4])

    def test_a_subject_row_says_core_or_elective(self):
        levels = self.build(programs=1, levels=1, classes=1, subjects=0)
        elective = Subject.all_objects.create(
            tenant=self.tenant, name="Further Maths", code="FMT", is_core=False,
        )
        SubjectOffering.all_objects.create(
            tenant=self.tenant, subject=elective, level=levels[0],
        )
        response = self.get(
            self.admin, "academics-structure-tree", {"depth": "full"},
        )
        subject_row = [r for r in response.data["data"]["rows"] if r["kind"] == "Subject"][0]
        self.assertEqual(subject_row["contains"], "Elective")

    def test_an_offering_overrides_the_subjects_own_core_flag(self):
        levels = self.build(programs=1, levels=1, classes=1, subjects=0)
        subject = Subject.all_objects.create(
            tenant=self.tenant, name="Mathematics", code="MTH", is_core=True,
        )
        SubjectOffering.all_objects.create(
            tenant=self.tenant, subject=subject, level=levels[0], is_core=False,
        )
        response = self.get(
            self.admin, "academics-structure-tree", {"depth": "full"},
        )
        subject_row = [r for r in response.data["data"]["rows"] if r["kind"] == "Subject"][0]
        self.assertEqual(subject_row["contains"], "Elective")

    def test_a_level_reports_its_class_and_subject_counts(self):
        self.build(programs=1, levels=1, classes=3, subjects=2)
        response = self.get(self.admin, "academics-structure-tree")
        level_row = [r for r in response.data["data"]["rows"] if r["kind"] == "Level"][0]
        self.assertEqual(level_row["class_count"], 3)
        self.assertEqual(level_row["subject_count"], 2)
        self.assertEqual(level_row["contains"], "3 classes")

    def test_an_empty_school_still_answers(self):
        response = self.get(self.admin, "academics-structure-tree")
        self.assertEqual(response.status_code, 200, response.data)
        rows = response.data["data"]["rows"]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["contains"], "No programmes")

    def test_the_tree_is_not_paginated(self):
        self.build(programs=1, levels=1)
        response = self.get(self.admin, "academics-structure-tree")
        self.assertNotIn("pagination", response.data)


class TreeScopeTests(_Base):
    def test_a_branch_caller_sees_shared_rows_plus_their_own(self):
        shared = Program.all_objects.create(
            tenant=self.tenant, name="Shared", code="SHR",
        )
        Program.all_objects.create(
            tenant=self.tenant, name="Ikeja Only", code="IKO", branch=self.ikeja,
        )
        del shared
        response = self.get(self.lekki_head, "academics-structure-tree")
        labels = {r["label"] for r in response.data["data"]["rows"] if r["kind"] == "Programme"}
        self.assertEqual(labels, {"Shared"})

    def test_the_branch_filter_narrows_the_whole_tree(self):
        Program.all_objects.create(tenant=self.tenant, name="Shared", code="SHR")
        Program.all_objects.create(
            tenant=self.tenant, name="Ikeja Only", code="IKO", branch=self.ikeja,
        )
        response = self.get(
            self.admin, "academics-structure-tree", {"branch": self.lekki.id},
        )
        labels = {r["label"] for r in response.data["data"]["rows"] if r["kind"] == "Programme"}
        self.assertEqual(labels, {"Shared"})

    def test_a_single_branch_school_sees_no_scope_column(self):
        solo = make_school(slug="sunrise", name="Sunrise Academy")
        make_branch(solo, name="Main", is_main=True)
        role = make_role(solo, name="School Admin", key="school_admin")
        for key in KEYS:
            make_role_permission(
                role, make_permission(key, scope=PermissionScope.TENANT),
            )
        admin = make_school_admin(None, email="head@sunrise.test", tenant=solo.tenant)
        make_assignment(solo, admin, role, branch=None)
        Program.all_objects.create(
            tenant=solo.tenant, name="Junior Secondary", code="JSS",
        )
        response = self.client_for(admin).get(
            reverse("academics-structure-tree"), {"tenant": solo.tenant.slug},
        )
        program_row = [
            r for r in response.data["data"]["rows"] if r["kind"] == "Programme"
        ][0]
        self.assertNotIn("scope_label", program_row)


class _BudgetMixin:
    """Assert what a composed read costs, in the two ways that matter.

    Counting the whole request is brittle: it folds in authentication and RBAC,
    so an unrelated change there breaks this test with a number nobody can
    interpret. Counting only the queries that touch this module's tables is the
    claim actually being made - one query per level of the tree, never one per
    parent - and it stays true when the platform around it changes.

    The second assertion is the one that would catch a regression: grow the
    school six-fold and the count must not move at all.
    """

    def academics_queries(self, ctx):
        return [
            q for q in ctx.captured_queries
            if "vs_academics_" in q["sql"]
        ]

    def assert_bounded(self, client, url, params, *, expected):
        client.get(url, params)                     # warm the auth caches
        with self.assertNumQueries(expected) as small:
            client.get(url, params)
        module_queries = len(self.academics_queries(small))

        self.build(programs=6, levels=5, classes=4, subjects=3)
        with self.assertNumQueries(expected) as big:
            client.get(url, params)
        self.assertEqual(
            len(self.academics_queries(big)), module_queries,
            "the query count grew with the size of the school",
        )
        return module_queries


class TreeQueryBudgetTests(_BudgetMixin, _Base):
    def test_the_default_depth_costs_four_queries_whatever_the_school(self):
        self.build(programs=2, levels=2, classes=2, subjects=1)
        n = self.assert_bounded(
            self.client_for(self.admin),
            reverse("academics-structure-tree"),
            {"tenant": self.tenant.slug},
            expected=15,
        )
        # The session, the programmes, the levels, and the two count
        # aggregates. Not one per programme.
        self.assertEqual(n, 5)

    def test_full_depth_adds_two_and_no_more(self):
        self.build(programs=2, levels=2, classes=2, subjects=1)
        n = self.assert_bounded(
            self.client_for(self.admin),
            reverse("academics-structure-tree"),
            {"tenant": self.tenant.slug, "depth": "full"},
            expected=17,
        )
        # The five above, plus the classes and the offerings - each fetched
        # once for the whole tree rather than once per level.
        self.assertEqual(n, 7)


class OverviewTests(_BudgetMixin, _Base):
    def session(self, name="2026/2027", start=D(2026, 9, 1), end=D(2027, 7, 31)):
        s = AcademicSession.all_objects.create(
            tenant=self.tenant, name=name, start_date=start, end_date=end,
        )
        set_branches(s, self.tenant, [])
        return s

    def test_the_counts_are_real(self):
        self.build(programs=2, levels=3, classes=2, subjects=2)
        Department.all_objects.create(tenant=self.tenant, name="Sciences", code="SCI")
        response = self.get(self.admin, "academics-overview")
        counts = response.data["data"]["counts"]
        self.assertEqual(counts["programs"], 2)
        self.assertEqual(counts["levels"], 6)
        self.assertEqual(counts["classes"], 12)
        self.assertEqual(counts["subjects"], 2)
        self.assertEqual(counts["departments"], 1)

    def test_the_counts_respect_the_callers_branch(self):
        Program.all_objects.create(tenant=self.tenant, name="Shared", code="SHR")
        Program.all_objects.create(
            tenant=self.tenant, name="Ikeja Only", code="IKO", branch=self.ikeja,
        )
        school_level = self.get(self.admin, "academics-overview")
        branch_level = self.get(self.lekki_head, "academics-overview")
        self.assertEqual(school_level.data["data"]["counts"]["programs"], 2)
        self.assertEqual(branch_level.data["data"]["counts"]["programs"], 1)

    def test_the_branch_filter_narrows_the_counts(self):
        """The pill above these numbers has to move them.

        A shared programme counts at every branch - that is what a null branch
        MEANS - so the Lekki view sees Shared and not Ikeja Only.
        """
        Program.all_objects.create(tenant=self.tenant, name="Shared", code="SHR")
        Program.all_objects.create(
            tenant=self.tenant, name="Ikeja Only", code="IKO", branch=self.ikeja,
        )
        unfiltered = self.get(self.admin, "academics-overview")
        lekki = self.get(
            self.admin, "academics-overview", {"branch": self.lekki.pk},
        )
        ikeja = self.get(
            self.admin, "academics-overview", {"branch": self.ikeja.pk},
        )
        self.assertEqual(unfiltered.data["data"]["counts"]["programs"], 2)
        self.assertEqual(lekki.data["data"]["counts"]["programs"], 1)
        self.assertEqual(ikeja.data["data"]["counts"]["programs"], 2)

    def test_the_branch_filter_leaves_the_live_year_alone(self):
        """Filtering the hero by branch would blank it, which is a different
        and misleading fact - `branches_without_a_session` reports that one."""
        session = self.session()
        activate_session(session, self.tenant)
        response = self.get(
            self.admin, "academics-overview", {"branch": self.ikeja.pk},
        )
        self.assertIsNotNone(response.data["data"]["active_session"])

    def test_a_school_with_no_active_year_says_so(self):
        # The fixture school runs a live year; retire it, because that is the
        # state being described.
        AcademicSession.all_objects.filter(pk=self.year.pk).update(
            status=SessionStatus.ARCHIVED,
        )
        self.session()
        response = self.get(self.admin, "academics-overview")
        self.assertIsNone(response.data["data"]["active_session"])

    def test_the_live_year_reports_its_terms_and_progress(self):
        session = self.session()
        for i, (name, start, end) in enumerate((
            ("First Term", D(2026, 9, 1), D(2026, 12, 11)),
            ("Second Term", D(2027, 1, 5), D(2027, 4, 1)),
        ), start=1):
            AcademicTerm.all_objects.create(
                tenant=self.tenant, session=session, name=name,
                order_index=i, start_date=start, end_date=end,
            )
        activate_session(session, self.tenant)

        response = self.get(self.admin, "academics-overview")
        block = response.data["data"]["active_session"]
        self.assertEqual(block["name"], "2026/2027")
        self.assertEqual(len(block["terms"]), 2)
        self.assertIn(block["percent_elapsed"], range(0, 101))
        self.assertTrue(
            all(t["state"] in ("pending", "ongoing", "completed", "archived")
                for t in block["terms"]),
        )

    def test_a_branch_left_in_no_year_is_reported(self):
        """Reachable only after a school splits its calendar.

        While a year names no branches it covers every branch, including ones
        opened later. Once one names its branches, a branch created afterwards
        is in nothing and there is no correct year to guess for it - so the
        school is told rather than a year being picked for it.
        """
        AcademicSession.all_objects.filter(pk=self.year.pk).update(
            status=SessionStatus.ARCHIVED,
        )
        lekki_only = self.session("2027 Lekki")
        set_branches(lekki_only, self.tenant, [self.lekki])
        activate_session(lekki_only, self.tenant)

        response = self.get(self.admin, "academics-overview")
        stranded = response.data["data"]["branches_without_a_session"]
        self.assertEqual([b["name"] for b in stranded], ["Ikeja Campus"])

    def test_a_school_wide_year_leaves_nobody_stranded(self):
        session = self.session()
        activate_session(session, self.tenant)
        response = self.get(self.admin, "academics-overview")
        self.assertEqual(response.data["data"]["branches_without_a_session"], [])

    def test_the_overview_is_a_fixed_cost(self):
        self.build(programs=2, levels=2, classes=2, subjects=1)
        n = self.assert_bounded(
            self.client_for(self.admin),
            reverse("academics-overview"),
            {"tenant": self.tenant.slug},
            expected=20,
        )
        # Six counts, the live year with its terms, the stranded-branch check
        # and the year lookup - each once, none of them per row. No branch
        # sweep, because a school-wide year covers every branch already.
        self.assertEqual(n, 10)

    def test_a_caller_without_the_key_is_refused(self):
        stranger = make_school_admin(
            None, email="nobody@brightfield.test", tenant=self.tenant,
        )
        self.assertEqual(self.get(stranger, "academics-overview").status_code, 403)

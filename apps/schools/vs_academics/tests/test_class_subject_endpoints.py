"""Classes and subjects over HTTP."""
from __future__ import annotations

import datetime as dt

from django.test import TestCase
from django.urls import reverse
from rest_framework.test import APIClient

from vs_audit.models import AuditEvent
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
    Level,
    Program,
    SchoolClass,
    Subject,
    SubjectOffering,
)

KEYS = (
    "academics.classes.view", "academics.classes.create",
    "academics.classes.update", "academics.classes.manage",
    "academics.subject.view", "academics.subject.create",
    "academics.subject.update", "academics.subject.manage",
    "academics.structure.view",
)


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

        cls.role = make_role(cls.school, name="School Admin", key="school_admin")
        for key in KEYS:
            make_role_permission(
                cls.role, make_permission(key, scope=PermissionScope.TENANT),
            )
        cls.admin = make_school_admin(
            None, email="adaeze@brightfield.test", tenant=cls.tenant,
        )
        make_assignment(cls.school, cls.admin, cls.role, branch=None)
        cls.lekki_head = make_school_admin(
            None, email="head@lekki.test", tenant=cls.tenant,
        )
        make_assignment(cls.school, cls.lekki_head, cls.role, branch=cls.lekki)

        cls.other = make_school(slug="sunrise", name="Sunrise Academy")
        # The other school needs a year of its own: a level belongs to one, and
        # a year belongs to one school.
        cls.other_year = AcademicSession.all_objects.create(
            tenant=cls.other.tenant, name="2099/2100",
            start_date=dt.date(2099, 9, 1), end_date=dt.date(2100, 7, 31),
            status="ACTIVE",
        )
        make_branch(cls.other, name="Main", is_main=True)

    def setUp(self):
        self.prog = Program.all_objects.create(
            tenant=self.tenant, name="Junior Secondary", code="JSS",
        )
        self.jss1 = Level.all_objects.create(
            tenant=self.tenant, session=self.year, program=self.prog, name="JSS1",
            code="JSS1", order_index=1,
        )
        self.jss2 = Level.all_objects.create(
            tenant=self.tenant, session=self.year, program=self.prog, name="JSS2",
            code="JSS2", order_index=2,
        )

    def client_for(self, user):
        client = APIClient()
        client.credentials(
            HTTP_AUTHORIZATION=f"Bearer {CodeXRefreshToken.for_user(user).access_token}",
        )
        return client

    def post(self, user, name, body, **kwargs):
        url = reverse(name, kwargs=kwargs or None)
        return self.client_for(user).post(
            f"{url}?tenant={self.tenant.slug}", body, format="json",
        )

    def get(self, user, name, params=None, **kwargs):
        url = reverse(name, kwargs=kwargs or None)
        return self.client_for(user).get(
            url, {"tenant": self.tenant.slug, **(params or {})},
        )


class ClassSecurityTests(_Base):
    def test_a_caller_without_the_key_is_refused(self):
        stranger = make_school_admin(
            None, email="nobody@brightfield.test", tenant=self.tenant,
        )
        self.assertEqual(
            self.get(stranger, "academics-class-list").status_code, 403,
        )

    def test_another_schools_class_is_a_404(self):
        their_prog = Program.all_objects.create(
            tenant=self.other.tenant, name="Theirs", code="THR",
        )
        their_level = Level.all_objects.create(
            tenant=self.other.tenant, session=self.other_year, program=their_prog, name="JSS1",
            code="JSS1", order_index=1,
        )
        theirs = SchoolClass.all_objects.create(
            tenant=self.other.tenant, session=self.other_year, level=their_level, name="JSS1 A", code="X1",
        )
        response = self.get(self.admin, "academics-class-detail", pk=theirs.pk)
        self.assertEqual(response.status_code, 404, response.data)

    def test_there_is_no_delete_route_for_a_class(self):
        """Its absence is a promise M11 depends on.

        ClassEnrolment points here with on_delete=PROTECT, which is safe only
        because nothing can reach that refusal. If a delete is added, M11 has
        to agree first - so this fails and asks.
        """
        klass = SchoolClass.all_objects.create(
            tenant=self.tenant, session=self.year, level=self.jss1, name="JSS1 A", code="JSS1-A",
        )
        url = reverse("academics-class-detail", kwargs={"pk": klass.pk})
        response = self.client_for(self.admin).delete(
            f"{url}?tenant={self.tenant.slug}",
        )
        self.assertEqual(response.status_code, 405, response.data)


class ClassLifecycleTests(_Base):
    def klass(self, name="JSS1 A", code="JSS1-A", branch=None, active=True):
        return SchoolClass.all_objects.create(
            tenant=self.tenant, session=self.year, level=self.jss1, name=name, code=code,
            branch=branch, is_active=active,
        )

    def test_a_class_is_archived_and_restored(self):
        klass = self.klass()
        archived = self.post(
            self.admin, "academics-class-archive", {}, pk=klass.pk,
        )
        self.assertEqual(archived.status_code, 200, archived.data)
        klass.refresh_from_db()
        self.assertFalse(klass.is_active)

        restored = self.post(
            self.admin, "academics-class-restore", {}, pk=klass.pk,
        )
        self.assertEqual(restored.status_code, 200, restored.data)
        klass.refresh_from_db()
        self.assertTrue(klass.is_active)

    def test_both_state_changes_are_audited(self):
        klass = self.klass()
        self.post(self.admin, "academics-class-archive", {}, pk=klass.pk)
        self.post(self.admin, "academics-class-restore", {}, pk=klass.pk)
        types = set(
            AuditEvent.objects.filter(entity_id=str(klass.pk))
            .values_list("action_type", flat=True)
        )
        self.assertIn("ACADEMIC_CLASS_ARCHIVED", types)
        self.assertIn("ACADEMIC_CLASS_RESTORED", types)

    def test_an_archived_class_is_out_of_the_default_list(self):
        self.klass()
        self.klass("JSS1 B", "JSS1-B", active=False)
        response = self.get(self.admin, "academics-class-list")
        self.assertEqual([r["name"] for r in response.data["data"]], ["JSS1 A"])

        asked = self.get(
            self.admin, "academics-class-list", {"is_active": "archived"},
        )
        self.assertEqual([r["name"] for r in asked.data["data"]], ["JSS1 B"])

    def test_the_level_filter_narrows(self):
        self.klass()
        SchoolClass.all_objects.create(
            tenant=self.tenant, session=self.year, level=self.jss2, name="JSS2 A", code="JSS2-A",
        )
        response = self.get(
            self.admin, "academics-class-list", {"level": self.jss2.pk},
        )
        self.assertEqual([r["name"] for r in response.data["data"]], ["JSS2 A"])


class GenerateArmsTests(_Base):
    def test_one_class_per_arm(self):
        response = self.post(
            self.admin, "academics-class-arms",
            {"level": self.jss1.pk, "arms": ["A", "B", "C"]},
        )
        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(
            [r["name"] for r in response.data["data"]],
            ["JSS1 A", "JSS1 B", "JSS1 C"],
        )

    def test_it_skips_what_already_exists_rather_than_refusing(self):
        """A school adding a fourth arm types A, B, C, D and gets one class."""
        self.post(
            self.admin, "academics-class-arms",
            {"level": self.jss1.pk, "arms": ["A", "B", "C"]},
        )
        response = self.post(
            self.admin, "academics-class-arms",
            {"level": self.jss1.pk, "arms": ["A", "B", "C", "D"]},
        )
        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual([r["name"] for r in response.data["data"]], ["JSS1 D"])
        self.assertEqual(SchoolClass.all_objects.count(), 4)

    def test_running_it_twice_creates_nothing_the_second_time(self):
        body = {"level": self.jss1.pk, "arms": ["A", "B"]}
        self.post(self.admin, "academics-class-arms", body)
        again = self.post(self.admin, "academics-class-arms", body)
        self.assertEqual(again.status_code, 200, again.data)
        self.assertEqual(SchoolClass.all_objects.count(), 2)

    def test_each_branch_may_run_its_own_arms(self):
        self.post(
            self.admin, "academics-class-arms",
            {"level": self.jss1.pk, "arms": ["A"], "branch": self.lekki.id},
        )
        response = self.post(
            self.admin, "academics-class-arms",
            {"level": self.jss1.pk, "arms": ["A"], "branch": self.ikeja.id},
        )
        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(SchoolClass.all_objects.count(), 2)


class ClassScopeTests(_Base):
    def test_capacity_is_writable(self):
        response = self.post(
            self.admin, "academics-class-list",
            {"name": "JSS1 A", "level": self.jss1.pk, "capacity": 30},
        )
        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(response.data["data"]["capacity"], 30)

    def test_a_class_may_not_be_wider_than_its_level(self):
        branch_level = Level.all_objects.create(
            tenant=self.tenant, session=self.year, program=self.prog, name="Lekki Only",
            code="LKO", order_index=9, branch=self.lekki,
        )
        response = self.post(
            self.admin, "academics-class-list",
            {"name": "Lekki Only A", "level": branch_level.pk, "branch": None},
        )
        self.assertEqual(response.status_code, 422, response.data)
        self.assertEqual(response.data["error"]["code"], "BRANCH_SCOPE_CONFLICT")

    def test_the_subject_count_is_this_classs_own(self):
        """Not the school's subject total wearing a class's name."""
        maths = Subject.all_objects.create(
            tenant=self.tenant, session=self.year, name="Mathematics", code="MTH",
        )
        SubjectOffering.all_objects.create(
            tenant=self.tenant, subject=maths, level=self.jss1,
        )
        Subject.all_objects.create(
            tenant=self.tenant, session=self.year, name="Yoruba", code="YOR",
        )   # offered nowhere
        SchoolClass.all_objects.create(
            tenant=self.tenant, session=self.year, level=self.jss1, name="JSS1 A", code="JSS1-A",
        )
        response = self.get(self.admin, "academics-class-list")
        self.assertEqual(response.data["data"][0]["subject_count"], 1)


class SubjectTests(_Base):
    def test_a_subject_and_its_offerings_are_created_in_one_call(self):
        response = self.post(
            self.admin, "academics-subject-list",
            {"name": "Mathematics", "level_ids": [self.jss1.pk, self.jss2.pk]},
        )
        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(response.data["data"]["level_count"], 2)
        self.assertEqual(response.data["data"]["code"], "MAT")

    def test_the_offered_label_collapses_a_run(self):
        levels = [self.jss1.pk, self.jss2.pk]
        for i in range(3, 6):
            levels.append(Level.all_objects.create(
                tenant=self.tenant, session=self.year, program=self.prog, name=f"JSS{i}",
                code=f"JSS{i}", order_index=i,
            ).pk)
        response = self.post(
            self.admin, "academics-subject-list",
            {"name": "Mathematics", "level_ids": levels},
        )
        self.assertEqual(response.data["data"]["offered_label"], "JSS1-JSS5")

    def test_two_levels_are_named_rather_than_collapsed(self):
        response = self.post(
            self.admin, "academics-subject-list",
            {"name": "Mathematics", "level_ids": [self.jss1.pk, self.jss2.pk]},
        )
        self.assertEqual(response.data["data"]["offered_label"], "JSS1, JSS2")

    def test_offerings_are_replaced_not_diffed(self):
        created = self.post(
            self.admin, "academics-subject-list",
            {"name": "Mathematics", "level_ids": [self.jss1.pk, self.jss2.pk]},
        )
        pk = created.data["data"]["id"]
        url = reverse("academics-subject-offerings", kwargs={"pk": pk})
        response = self.client_for(self.admin).put(
            f"{url}?tenant={self.tenant.slug}",
            {"level_ids": [self.jss2.pk]}, format="json",
        )
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["data"]["level_count"], 1)

    def test_one_foreign_level_writes_nothing_at_all(self):
        """Including the valid ids in the same request."""
        created = self.post(
            self.admin, "academics-subject-list",
            {"name": "Mathematics", "level_ids": [self.jss1.pk]},
        )
        pk = created.data["data"]["id"]
        their_prog = Program.all_objects.create(
            tenant=self.other.tenant, name="Theirs", code="THR",
        )
        theirs = Level.all_objects.create(
            tenant=self.other.tenant, session=self.other_year, program=their_prog, name="JSS9",
            code="JSS9", order_index=1,
        )
        url = reverse("academics-subject-offerings", kwargs={"pk": pk})
        response = self.client_for(self.admin).put(
            f"{url}?tenant={self.tenant.slug}",
            {"level_ids": [self.jss2.pk, theirs.pk]}, format="json",
        )
        self.assertEqual(response.status_code, 404, response.data)
        self.assertEqual(
            list(SubjectOffering.all_objects.filter(subject_id=pk)
                 .values_list("level_id", flat=True)),
            [self.jss1.pk],
        )

    def test_a_branch_subject_may_not_be_offered_at_another_branchs_level(self):
        ikeja_level = Level.all_objects.create(
            tenant=self.tenant, session=self.year, program=self.prog, name="Ikeja Only",
            code="IKO", order_index=9, branch=self.ikeja,
        )
        response = self.post(
            self.admin, "academics-subject-list",
            {"name": "Yoruba", "branch": self.lekki.id,
             "level_ids": [ikeja_level.pk]},
        )
        self.assertEqual(response.status_code, 422, response.data)
        self.assertEqual(response.data["error"]["code"], "BRANCH_SCOPE_CONFLICT")

    def test_a_shared_subject_may_be_offered_anywhere(self):
        ikeja_level = Level.all_objects.create(
            tenant=self.tenant, session=self.year, program=self.prog, name="Ikeja Only",
            code="IKO", order_index=9, branch=self.ikeja,
        )
        response = self.post(
            self.admin, "academics-subject-list",
            {"name": "Mathematics", "level_ids": [ikeja_level.pk, self.jss1.pk]},
        )
        self.assertEqual(response.status_code, 201, response.data)

    def test_the_core_filter(self):
        self.post(
            self.admin, "academics-subject-list",
            {"name": "Mathematics", "is_core": True},
        )
        self.post(
            self.admin, "academics-subject-list",
            {"name": "Further Maths", "is_core": False},
        )
        core = self.get(
            self.admin, "academics-subject-list", {"is_core": "core"},
        )
        self.assertEqual([r["name"] for r in core.data["data"]], ["Mathematics"])
        elective = self.get(
            self.admin, "academics-subject-list", {"is_core": "elective"},
        )
        self.assertEqual([r["name"] for r in elective.data["data"]], ["Further Maths"])

    def test_a_subject_is_archived_rather_than_deleted(self):
        """Archiving keeps the offerings: it is reversible, so it takes nothing.

        A delete used to cascade them away, which is fine for a subject nobody
        ever taught and wrong for one that ran for two years - the offerings
        ARE the record of where it was taught.
        """
        created = self.post(
            self.admin, "academics-subject-list",
            {"name": "Mathematics", "level_ids": [self.jss1.pk]},
        )
        pk = created.data["data"]["id"]

        url = reverse("academics-subject-detail", kwargs={"pk": pk})
        gone = self.client_for(self.admin).delete(f"{url}?tenant={self.tenant.slug}")
        self.assertEqual(gone.status_code, 405, gone.data)

        response = self.post(self.admin, "academics-subject-archive", {}, pk=pk)
        self.assertEqual(response.status_code, 200, response.data)
        self.assertFalse(Subject.all_objects.get(pk=pk).is_active)
        self.assertEqual(SubjectOffering.all_objects.count(), 1)

    def test_an_archived_subject_comes_back(self):
        created = self.post(
            self.admin, "academics-subject-list", {"name": "Mathematics"},
        )
        pk = created.data["data"]["id"]
        self.post(self.admin, "academics-subject-archive", {}, pk=pk)
        response = self.post(self.admin, "academics-subject-restore", {}, pk=pk)
        self.assertEqual(response.status_code, 200, response.data)
        self.assertTrue(Subject.all_objects.get(pk=pk).is_active)

    def test_an_empty_subject_list_is_still_a_list(self):
        response = self.get(self.admin, "academics-subject-list")
        self.assertEqual(response.data["data"], [])

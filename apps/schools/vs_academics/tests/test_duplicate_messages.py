"""Duplicates are refused with a sentence the person can act on.

The database refused these before this module did. What is asserted here is not
that a duplicate fails - `test_constraints` covers that - but that the failure
NAMES the field and the row it hit, because the drawer renders the message
verbatim under the control that caused it. A refusal reading "a record with
these details already exists" over a form with a Name box and a Code box is one
the person cannot act on: they cannot tell which box was wrong.

Two rules are load-bearing and each has its own test:

  * the rule the message STATES is the rule the constraint enforces. A level's
    name is unique inside its programme, not across the school, and a message
    that says otherwise is a lie a school will act on;
  * the branch is named EVEN WHEN THE CALLER CANNOT SEE IT, because the
    alternative is a refusal that cannot be resolved - their own list does not
    contain the row that blocked them.
"""
from __future__ import annotations

from vs_rbac.models import PermissionScope
from vs_rbac.tests.helpers import make_permission, make_role_permission

from .test_structure_endpoints import _Base
from schools.vs_academics.models import Level, SchoolClass, Subject


class _AllAcademics(_Base):
    """_Base grants academics.structure.* only.

    Classes and subjects are separate RESOURCES on purpose - the backend seeds
    subject.create to a branch admin and structure.create not - so a test that
    touches them has to hold their keys too. Granting them here rather than
    widening _Base keeps the structure tests honest about what they prove.
    """

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        for key in (
            "academics.classes.view", "academics.classes.create",
            "academics.classes.update", "academics.classes.manage",
            "academics.subject.view", "academics.subject.create",
            "academics.subject.update", "academics.subject.manage",
        ):
            make_role_permission(
                cls.role, make_permission(key, scope=PermissionScope.TENANT),
            )


class DuplicateNameTests(_Base):
    def test_a_repeated_department_name_names_the_field_and_the_row(self):
        self.dept("Sciences", "SCI")
        response = self.post(
            self.admin, "academics-department-list",
            {"name": "Sciences", "code": "SCX"},
        )
        self.assertEqual(response.status_code, 409, response.data)
        self.assertEqual(response.data["error"]["code"], "DUPLICATE_NAME")
        self.assertEqual(response.data["error"]["detail"]["field"], "name")
        self.assertIn("Sciences", response.data["message"])

    def test_a_repeated_code_says_what_holds_it(self):
        self.dept("Sciences", "SCI")
        response = self.post(
            self.admin, "academics-department-list",
            {"name": "Applied Science", "code": "SCI"},
        )
        self.assertEqual(response.status_code, 409, response.data)
        self.assertEqual(response.data["error"]["code"], "DUPLICATE_CODE")
        self.assertEqual(response.data["error"]["detail"]["field"], "code")
        # The row that holds the code, not just "a record".
        self.assertIn("Sciences", response.data["message"])

    def test_the_blocking_row_is_named_with_its_branch(self):
        self.dept("General Studies", "GST", branch=self.ikeja)
        response = self.post(
            self.admin, "academics-department-list",
            {"name": "General Studies", "code": "GS2"},
        )
        self.assertEqual(response.status_code, 409, response.data)
        self.assertIn("Ikeja Campus", response.data["message"])
        self.assertEqual(
            response.data["error"]["detail"]["scope_label"], "Ikeja Campus",
        )

    def test_a_branch_bound_caller_is_told_which_branch_blocked_them(self):
        """The case the whole feature exists for.

        The Lekki head cannot READ the Ikeja row - scope_to_visible_branches
        removed it - so nothing on their screen can explain the refusal. Only
        the server knows, so only the server can say it.
        """
        self.dept("General Studies", "GST", branch=self.ikeja)
        response = self.post(
            self.lekki_head, "academics-department-list",
            {"name": "General Studies", "code": "GS2"},
        )
        self.assertEqual(response.status_code, 409, response.data)
        self.assertIn("Ikeja Campus", response.data["message"])

    def test_a_school_wide_row_tells_a_branch_caller_not_to_copy_it(self):
        self.dept("Sciences", "SCI")
        response = self.post(
            self.lekki_head, "academics-department-list",
            {"name": "Sciences", "code": "SC2"},
        )
        self.assertEqual(response.status_code, 409, response.data)
        self.assertIn("school-wide", response.data["message"])

    def test_editing_a_row_does_not_clash_with_itself(self):
        dept = self.dept("Sciences", "SCI")
        url = f"/v1/academics/departments/{dept.pk}/?tenant={self.tenant.slug}"
        response = self.client_for(self.admin).patch(
            url, {"name": "Sciences", "code": "SCI", "description": "x"},
            format="json",
        )
        self.assertEqual(response.status_code, 200, response.data)


class ScopeOfTheRuleTests(_AllAcademics):
    def test_a_level_name_is_unique_inside_its_programme_only(self):
        """Two programmes may both run a "Year 1"; the constraint allows it.

        A message saying "already exists in this school" would tell a school to
        rename something it is entitled to keep.
        """
        jss = self.program("Junior Secondary", "JSS")
        nursery = self.program("Nursery", "NUR")
        Level.all_objects.create(
            tenant=self.tenant, session=self.year, program=jss, name="Year 1", code="J1",
            order_index=1,
        )
        ok = self.post(
            self.admin, "academics-level-list",
            {"name": "Year 1", "code": "N1"}, pk=nursery.pk,
        )
        self.assertEqual(ok.status_code, 201, ok.data)

        clash = self.post(
            self.admin, "academics-level-list",
            {"name": "Year 1", "code": "J2"}, pk=jss.pk,
        )
        self.assertEqual(clash.status_code, 409, clash.data)
        self.assertIn("Junior Secondary", clash.data["message"])

    def test_a_class_name_is_unique_inside_its_level_at_its_branch(self):
        """"JSS1 A" may exist at Lekki and at Ikeja. The constraints say so."""
        jss = self.program("Junior Secondary", "JSS")
        level = Level.all_objects.create(
            tenant=self.tenant, session=self.year, program=jss, name="JSS1", code="JSS1",
            order_index=1,
        )
        SchoolClass.all_objects.create(
            tenant=self.tenant, session=self.year, level=level, branch=self.lekki,
            name="JSS1 A", code="JSS1-A",
        )
        url = f"/v1/academics/classes/?tenant={self.tenant.slug}"
        ok = self.client_for(self.admin).post(
            url,
            {"name": "JSS1 A", "code": "JSS1-A-IK", "level": level.pk,
             "branch": self.ikeja.pk},
            format="json",
        )
        self.assertEqual(ok.status_code, 201, ok.data)

        clash = self.client_for(self.admin).post(
            url,
            {"name": "JSS1 A", "code": "JSS1-A2", "level": level.pk,
             "branch": self.lekki.pk},
            format="json",
        )
        self.assertEqual(clash.status_code, 409, clash.data)
        self.assertIn("JSS1", clash.data["message"])

    def test_a_class_code_is_unique_across_the_whole_school(self):
        jss = self.program("Junior Secondary", "JSS")
        level = Level.all_objects.create(
            tenant=self.tenant, session=self.year, program=jss, name="JSS1", code="JSS1",
            order_index=1,
        )
        SchoolClass.all_objects.create(
            tenant=self.tenant, session=self.year, level=level, branch=self.lekki,
            name="JSS1 A", code="JSS1-A",
        )
        url = f"/v1/academics/classes/?tenant={self.tenant.slug}"
        response = self.client_for(self.admin).post(
            url,
            {"name": "JSS1 B", "code": "JSS1-A", "level": level.pk,
             "branch": self.ikeja.pk},
            format="json",
        )
        self.assertEqual(response.status_code, 409, response.data)
        self.assertEqual(response.data["error"]["code"], "DUPLICATE_CODE")


class ArchivedRowsStillHoldTheirNameTests(_AllAcademics):
    def test_an_archived_subject_still_blocks_its_name(self):
        """The constraint does not exempt it, so neither may the guard.

        Passing `objects` instead of `all_objects` here would refuse the write
        at the database with the generic message - the exact refusal this whole
        module exists to replace.
        """
        Subject.all_objects.create(
            tenant=self.tenant, name="Yoruba", code="YOR", is_active=False,
        )
        url = f"/v1/academics/subjects/?tenant={self.tenant.slug}"
        response = self.client_for(self.admin).post(
            url, {"name": "Yoruba", "code": "YO2"}, format="json",
        )
        self.assertEqual(response.status_code, 409, response.data)
        self.assertIn("Yoruba", response.data["message"])


class ArchivingRatherThanDeletingTests(_AllAcademics):
    """Nothing here is deleted, so nothing here needs a blocked-delete message.

    The delete routes and their refusals were removed together: a programme
    holding levels no longer has to be refused, because archiving it is
    reversible and leaves the levels alone. What replaced the refusal is a
    lifecycle that says one thing and does one thing.
    """

    def test_archiving_a_level_leaves_its_classes_and_its_offerings(self):
        """The two things a delete used to argue about, both untouched."""
        from schools.vs_academics.models import SubjectOffering

        jss = self.program("Junior Secondary", "JSS")
        level = Level.all_objects.create(
            tenant=self.tenant, session=self.year, program=jss, name="JSS1",
            code="JSS1", order_index=1,
        )
        SchoolClass.all_objects.create(
            tenant=self.tenant, session=self.year, level=level, branch=self.lekki,
            name="JSS1 A", code="JSS1-A",
        )
        subject = Subject.all_objects.create(
            tenant=self.tenant, name="Mathematics", code="MTH",
        )
        SubjectOffering.all_objects.create(
            tenant=self.tenant, subject=subject, level=level,
        )

        url = f"/v1/academics/levels/{level.pk}/archive/?tenant={self.tenant.slug}"
        response = self.client_for(self.admin).post(url)
        self.assertEqual(response.status_code, 200, response.data)

        level.refresh_from_db()
        self.assertFalse(level.is_active)
        self.assertEqual(SchoolClass.all_objects.filter(level=level).count(), 1)
        self.assertEqual(SubjectOffering.all_objects.filter(level=level).count(), 1)
        self.assertTrue(Subject.all_objects.get(pk=subject.pk).is_active)

    def test_a_level_that_ran_cannot_be_deleted_at_all(self):
        """Not refused with an explanation - not offered.

        A level belongs to a year, so deleting one edits what that year says
        the school ran. There is no wording that makes that acceptable, which
        is why the route is gone rather than guarded.
        """
        jss = self.program("Junior Secondary", "JSS")
        level = Level.all_objects.create(
            tenant=self.tenant, session=self.year, program=jss, name="JSS3",
            code="JSS3", order_index=3,
        )
        url = f"/v1/academics/levels/{level.pk}/?tenant={self.tenant.slug}"
        response = self.client_for(self.admin).delete(url)
        self.assertEqual(response.status_code, 405, response.data)
        self.assertTrue(Level.all_objects.filter(pk=level.pk).exists())

    def test_the_level_list_says_how_many_subjects_would_go_with_it(self):
        """The delete confirmation reads this, so it has to be there.

        Without it the screen removes offerings the reader was never told about,
        which is the failure that makes a cascade feel like data loss.
        """
        from schools.vs_academics.models import SubjectOffering

        jss = self.program("Junior Secondary", "JSS")
        level = Level.all_objects.create(
            tenant=self.tenant, session=self.year, program=jss, name="JSS1", code="JSS1", order_index=1,
        )
        subject = Subject.all_objects.create(
            tenant=self.tenant, name="Mathematics", code="MTH",
        )
        SubjectOffering.all_objects.create(
            tenant=self.tenant, subject=subject, level=level,
        )
        # Both routes to a level, because the accordion reads the NESTED one
        # and it has its own prefetch queryset - an annotation added to only one
        # of them reaches the serializer as a silent zero.
        flat = self.get(self.admin, "academics-level-list", pk=jss.pk)
        self.assertEqual(flat.status_code, 200, flat.data)
        self.assertEqual(flat.data["data"][0]["subject_count"], 1)

        nested = self.get(self.admin, "academics-program-list")
        program = next(
            p for p in nested.data["data"] if p["id"] == jss.pk
        )
        self.assertEqual(program["levels"][0]["subject_count"], 1)


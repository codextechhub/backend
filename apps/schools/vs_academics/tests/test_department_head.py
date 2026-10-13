"""Who heads a department.

The head is a member of staff on the school's roll, named on the department. The
endpoint tests cover the refusals that only the API layer makes (another school's
staff, a person who has left, Field Access); the service tests call the two
questions other modules ask.
"""
from __future__ import annotations

from django.urls import reverse

from core.test_utils import TenantAPIClient
from schools.vs_staff.models import StaffProfile
from vs_rbac.tests.helpers import install_declared_fields, make_school_admin, set_field_access

from ..models import Department
from ..services.heads import departments_headed_by, head_of
from .electives_base import ElectivesFixture

HEAD = "academics.department.head"


class _Base(ElectivesFixture):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.keys = install_declared_fields("academics.department")

    def detail(self, user, department, method="get", **body):
        url = reverse("academics-department-detail", kwargs={"pk": department.pk})
        client = TenantAPIClient(user)
        if method == "get":
            return client.get(url)
        return getattr(client, method)(url, body, format="json")

    def set_head(self, user, department, staff_id):
        return self.detail(user, department, "patch", head=staff_id)

    def staff_head(self, response):
        return self.data(response)["head"]


class HeadOnTheDepartmentTests(_Base):
    def test_a_department_with_no_head_reads_null(self):
        response = self.detail(self.admin, self.sciences)
        self.assertEqual(response.status_code, 200, response.data)
        self.assertIsNone(self.staff_head(response))

    def test_a_head_is_set_read_back_and_cleared(self):
        saved = self.set_head(self.admin, self.sciences, self.eze_staff.pk)
        self.assertEqual(saved.status_code, 200, saved.data)
        self.assertEqual(
            self.staff_head(saved), {"id": self.eze_staff.pk, "name": "Chidi Eze"},
        )
        self.sciences.refresh_from_db()
        self.assertEqual(self.sciences.head_id, self.eze_staff.pk)
        self.assertEqual(
            self.staff_head(self.detail(self.admin, self.sciences)),
            {"id": self.eze_staff.pk, "name": "Chidi Eze"},
        )

        cleared = self.set_head(self.admin, self.sciences, None)
        self.assertEqual(cleared.status_code, 200, cleared.data)
        self.assertIsNone(self.staff_head(cleared))
        self.sciences.refresh_from_db()
        self.assertIsNone(self.sciences.head_id)

    def test_a_head_can_be_named_when_the_department_is_created(self):
        created = TenantAPIClient(self.admin).post(
            reverse("academics-department-list"),
            {"name": "Humanities", "head": self.bello_staff.pk}, format="json",
        )
        self.assertEqual(created.status_code, 201, created.data)
        self.assertEqual(self.staff_head(created)["id"], self.bello_staff.pk)

    def test_the_list_carries_the_head(self):
        self.set_head(self.admin, self.sciences, self.eze_staff.pk)
        listed = TenantAPIClient(self.admin).get(reverse("academics-department-list"))
        rows = {row["name"]: row for row in listed.data["data"]}
        self.assertEqual(rows["Sciences"]["head"]["id"], self.eze_staff.pk)

    def test_an_edit_that_leaves_the_head_out_leaves_the_head_alone(self):
        self.set_head(self.admin, self.sciences, self.eze_staff.pk)
        renamed = self.detail(self.admin, self.sciences, "patch", description="Labs")
        self.assertEqual(renamed.status_code, 200, renamed.data)
        self.assertEqual(self.staff_head(renamed)["id"], self.eze_staff.pk)

    def test_a_member_of_staff_of_another_school_is_refused_as_if_they_did_not_exist(self):
        foreign = self.set_head(self.admin, self.sciences, self.solo_staff.pk)
        missing = self.set_head(self.admin, self.sciences, 99999999)
        self.assertEqual(foreign.status_code, 404, foreign.data)
        self.assertEqual(missing.status_code, 404)
        self.assertEqual(foreign.data["message"], missing.data["message"])
        self.sciences.refresh_from_db()
        self.assertIsNone(self.sciences.head_id)

    def test_somebody_who_has_left_the_roll_cannot_be_named(self):
        gone = StaffProfile.all_objects.create(
            tenant=self.tenant,
            user=make_school_admin(
                None, email="ex@brightfield.test", tenant=self.tenant,
                first_name="Kunle", last_name="Ojo",
            ),
            employment_status="RESIGNED",
        )
        refused = self.set_head(self.admin, self.sciences, gone.pk)
        self.assertEqual(refused.status_code, 422, refused.data)
        self.assertEqual(refused.data["error"]["code"], "DEPARTMENT_HEAD_REFUSED")
        self.assertIn("Kunle Ojo", refused.data["message"])

    def test_a_head_who_later_leaves_reads_as_nobody(self):
        self.set_head(self.admin, self.sciences, self.eze_staff.pk)
        StaffProfile.all_objects.filter(pk=self.eze_staff.pk).update(employment_status="TERMINATED")
        self.assertIsNone(self.staff_head(self.detail(self.admin, self.sciences)))

    def test_a_caller_without_the_structure_update_key_cannot_name_a_head(self):
        refused = self.set_head(self.viewer, self.sciences, self.eze_staff.pk)
        self.assertEqual(refused.status_code, 403)

    def test_at_a_one_branch_school_a_head_is_set_and_no_branch_field_appears(self):
        saved = self.set_head(self.solo_admin, self.solo_dept, self.solo_staff.pk)
        self.assertEqual(saved.status_code, 200, saved.data)
        self.assertEqual(self.staff_head(saved)["name"], "Bola Ade")
        self.assertNotIn("branch", self.data(saved))


class HeadServiceTests(_Base):
    def test_head_of_answers_the_member_of_staff_or_nobody(self):
        self.assertIsNone(head_of(self.sciences))
        self.sciences.head = self.eze_staff
        self.sciences.save(update_fields=["head"])
        self.assertEqual(head_of(self.sciences), self.eze_staff)
        StaffProfile.all_objects.filter(pk=self.eze_staff.pk).update(employment_status="RESIGNED")
        self.sciences.refresh_from_db()
        self.assertIsNone(head_of(self.sciences))

    def test_departments_headed_by_are_the_active_ones_of_that_school_by_name(self):
        arts = Department.all_objects.create(tenant=self.tenant, name="Arts", code="ART", head=self.eze_staff)
        sciences = Department.all_objects.get(pk=self.sciences.pk)
        sciences.head = self.eze_staff
        sciences.save(update_fields=["head"])
        Department.all_objects.create(
            tenant=self.tenant, name="Commerce", code="COM", head=self.bello_staff,
        )
        Department.all_objects.create(
            tenant=self.tenant, name="Old", code="OLD", head=self.eze_staff, is_active=False,
        )
        self.assertEqual(
            list(departments_headed_by(self.eze, self.tenant)), [arts, sciences],
        )
        self.assertEqual(list(departments_headed_by(self.admin, self.tenant)), [])
        self.assertEqual(list(departments_headed_by(self.eze, self.solo)), [])

    def test_a_head_who_has_left_heads_nothing(self):
        Department.all_objects.filter(pk=self.sciences.pk).update(head=self.eze_staff)
        StaffProfile.all_objects.filter(pk=self.eze_staff.pk).update(employment_status="TERMINATED")
        self.assertEqual(list(departments_headed_by(self.eze, self.tenant)), [])


class HeadFieldAccessTests(_Base):
    def setUp(self):
        super().setUp()
        Department.all_objects.filter(pk=self.sciences.pk).update(head=self.eze_staff)

    def test_the_declaration_is_one_open_writable_field(self):
        from vs_rbac.models import FieldDefinition

        [field] = FieldDefinition.objects.filter(key__startswith="academics.department.")
        self.assertEqual(field.key, HEAD)
        self.assertTrue(field.writable)
        self.assertFalse(field.sensitive)

    def test_a_role_that_may_not_read_the_head_does_not_see_it(self):
        set_field_access(self.admin_role, HEAD, read=False, write=False)
        detail = self.data(self.detail(self.admin, self.sciences))
        self.assertNotIn("head", detail)
        listed = TenantAPIClient(self.admin).get(reverse("academics-department-list"))
        self.assertNotIn("head", listed.data["data"][0])

    def test_a_role_that_may_read_but_not_write_the_head_is_refused_a_change(self):
        set_field_access(self.admin_role, HEAD, read=True, write=False)
        self.assertIn("head", self.data(self.detail(self.admin, self.sciences)))
        refused = self.set_head(self.admin, self.sciences, self.bello_staff.pk)
        self.assertEqual(refused.status_code, 403)
        self.assertEqual(refused.data["error"]["code"], "field_write_denied")
        self.assertIn("head", refused.data["error"]["detail"])
        self.sciences.refresh_from_db()
        self.assertEqual(self.sciences.head_id, self.eze_staff.pk)

    def test_the_detail_names_a_read_only_head_so_a_form_can_grey_it(self):
        set_field_access(self.admin_role, HEAD, read=True, write=False)
        self.assertEqual(self.data(self.detail(self.admin, self.sciences))["_read_only_fields"], ["head"])

    def test_a_form_that_sends_the_head_back_unchanged_is_not_refused(self):
        set_field_access(self.admin_role, HEAD, read=True, write=False)
        echoed = self.detail(
            self.admin, self.sciences, "patch", head=self.eze_staff.pk, description="Labs",
        )
        self.assertEqual(echoed.status_code, 200, echoed.data)

    def test_other_department_fields_stay_editable_when_the_head_is_closed(self):
        set_field_access(self.admin_role, HEAD, read=False, write=False)
        renamed = self.detail(self.admin, self.sciences, "patch", description="Labs")
        self.assertEqual(renamed.status_code, 200, renamed.data)
        self.assertNotIn("head", self.data(renamed))

    def test_creating_a_department_with_a_head_needs_the_write_switch(self):
        set_field_access(self.admin_role, HEAD, read=True, write=False)
        refused = TenantAPIClient(self.admin).post(
            reverse("academics-department-list"),
            {"name": "Humanities", "head": self.bello_staff.pk}, format="json",
        )
        self.assertEqual(refused.status_code, 403)
        self.assertFalse(Department.all_objects.filter(name="Humanities").exists())

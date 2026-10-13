"""Which elective fields a role reads and changes.

``takes`` is one switch with two names on the wire: the subjects a student takes,
and the number of students taking each subject. These tests install the real
declaration and switch it per role.
"""
from __future__ import annotations

from vs_rbac.models import FieldDefinition
from vs_rbac.tests.helpers import install_declared_fields, set_field_access

from ..models import ElectiveTaker
from .electives_base import ElectivesFixture

RESOURCE = "academics.electives"
TAKES = "academics.electives.takes"


class _Switches(ElectivesFixture):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.keys = install_declared_fields(RESOURCE)

    def recorded(self):
        ElectiveTaker.all_objects.create(
            tenant=self.tenant, school_class=self.jss1a, term=self.first_term,
            subject=self.yoruba, student=self.tunde,
        )


class DefaultAccessTests(_Switches):
    def test_the_declaration_is_one_open_writable_field_with_two_names(self):
        [field] = FieldDefinition.objects.filter(key__startswith=RESOURCE)
        self.assertEqual(field.key, TAKES)
        self.assertEqual(sorted(field.api_names), ["takers", "takes"])
        self.assertTrue(field.writable)
        self.assertFalse(field.sensitive)

    def test_without_a_switch_every_role_that_may_open_the_screen_reads_and_writes_it(self):
        self.recorded()
        body = self.data(self.read(self.eze, self.jss1a))
        self.assertIn("takers", body["subjects"][0])
        self.assertIn("takes", body["students"][0])
        saved = self.save(self.eze, self.jss1a, [self.change(self.kelechi, self.yoruba)])
        self.assertEqual(saved.status_code, 200, saved.data)


class ReadSwitchTests(_Switches):
    def test_a_role_that_may_not_read_the_choice_loses_both_names_and_keeps_the_roll(self):
        self.recorded()
        set_field_access(self.teacher_role, TAKES, read=False, write=False)
        body = self.data(self.read(self.eze, self.jss1a))

        for subject in body["subjects"]:
            self.assertNotIn("takers", subject)
            self.assertEqual(set(subject), {"id", "name", "code", "roll"})
        for student in body["students"]:
            self.assertNotIn("takes", student)
            self.assertEqual(set(student), {"id", "name", "admission_number"})
        self.assertEqual(len(body["students"]), 4)

    def test_the_switch_is_per_role(self):
        set_field_access(self.teacher_role, TAKES, read=False, write=False)
        body = self.data(self.read(self.admin, self.jss1a))
        self.assertIn("takes", body["students"][0])

    def test_a_role_that_cannot_read_the_choice_cannot_save_it_either(self):
        # Write implies Read, so closing Read closes the save as well.
        set_field_access(self.teacher_role, TAKES, read=False, write=False)
        refused = self.save(self.eze, self.jss1a, [self.change(self.tunde, self.yoruba)])
        self.assertEqual(refused.status_code, 403)
        self.assertEqual(ElectiveTaker.objects.count(), 0)


class WriteSwitchTests(_Switches):
    def test_a_role_that_may_read_but_not_write_sees_the_grid_and_is_refused_a_save(self):
        self.recorded()
        set_field_access(self.teacher_role, TAKES, read=True, write=False)
        self.assertIn("takes", self.data(self.read(self.eze, self.jss1a))["students"][0])

        refused = self.save(self.eze, self.jss1a, [self.change(self.kelechi, self.yoruba)])
        self.assertEqual(refused.status_code, 403)
        self.assertEqual(refused.data["error"]["code"], "field_write_denied")
        self.assertIn("takes", refused.data["error"]["detail"])
        self.assertEqual(ElectiveTaker.objects.count(), 1)

    def test_a_removal_is_a_write_too(self):
        self.recorded()
        set_field_access(self.teacher_role, TAKES, read=True, write=False)
        refused = self.save(self.eze, self.jss1a, [self.change(self.tunde, self.yoruba, False)])
        self.assertEqual(refused.status_code, 403)
        self.assertEqual(ElectiveTaker.objects.count(), 1)

    def test_a_role_that_may_not_read_is_refused_a_matching_value_too(self):
        # "Equal is accepted, different is refused" would reveal the hidden value.
        self.recorded()
        set_field_access(self.teacher_role, TAKES, read=False, write=False)
        refused = self.save(self.eze, self.jss1a, [self.change(self.tunde, self.yoruba, True)])
        self.assertEqual(refused.status_code, 403)

    def test_the_switch_does_not_reach_another_role(self):
        set_field_access(self.teacher_role, TAKES, read=True, write=False)
        saved = self.save(self.admin, self.jss1a, [self.change(self.kelechi, self.yoruba)])
        self.assertEqual(saved.status_code, 200, saved.data)

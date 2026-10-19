"""Which assessment-setup fields a role reads and changes, and what the registry holds.

Every setup field is open to every role by default. These tests install the
real declarations (``install_declared_fields``) and switch them per role, then
prove each switch on every route that carries the field: the list, the detail,
the write and the copy that answers with the record.
"""
from __future__ import annotations

from io import StringIO
from unittest import mock

from django.core.management import call_command
from django.test import TestCase

from vs_rbac.field_registry import all_declarations, get_declaration
from vs_rbac.models import FieldDefinition, PrebuiltRoleFieldAccess
from vs_rbac.tests.helpers import install_declared_fields, set_field_access

from ..constants import SetupStatus
from ..models import AssessmentStructure
from .base import GradebookFixture

SYNC = "vs_rbac.management.commands.sync_field_registry.all_declarations"
RESOURCE = "gradebook.setup"
COMPONENTS = "gradebook.setup.components"
MAX_SCORE = "gradebook.setup.max_score"
BANDS = "gradebook.setup.bands"
REMARK = "gradebook.setup.remark"
TEMPLATES = "gradebook.setup.remark_templates"
TELL = "gradebook.setup.tell_every_parent"


class _Switches(GradebookFixture):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.keys = install_declared_fields(RESOURCE)
        cls.structure = cls.make_structure(cls.junior, status=SetupStatus.DRAFT)
        cls.scale = cls.make_scale(cls.junior, status=SetupStatus.DRAFT)

    def close(self, *keys, read=False, write=False):
        set_field_access(self.admin_role, *keys, read=read, write=write)

    def structure_row(self):
        return self.data(self.get(self.admin, "gradebook-structure", pk=self.structure.pk))

    def scale_row(self):
        return self.data(self.get(self.admin, "gradebook-scale", pk=self.scale.pk))

    def component_rows(self):
        return [
            {"id": c.pk, "name": c.name, "short_name": c.short_name,
             "max_score": c.max_score, "is_exam": c.is_exam}
            for c in self.structure.components.all()
        ]


class DefaultAccessTests(_Switches):
    def test_the_declaration_matches_what_the_registry_holds(self):
        self.assertEqual(
            set(FieldDefinition.objects.filter(key__startswith=RESOURCE).values_list("key", flat=True)),
            {COMPONENTS, MAX_SCORE, BANDS, REMARK, TEMPLATES, TELL},
        )

    def test_no_setup_field_is_sensitive_and_every_one_is_writable(self):
        fields = FieldDefinition.objects.filter(key__startswith=RESOURCE)
        self.assertEqual([f.key for f in fields if f.sensitive or not f.writable], [])

    def test_without_a_switch_every_field_is_readable_and_writable(self):
        row = self.structure_row()
        for name in ("components", "total", "total_check"):
            self.assertIn(name, row)
        self.assertIn("max_score", row["components"][0])
        scale = self.scale_row()
        self.assertIn("bands", scale)
        self.assertIn("check", scale)
        self.assertIn("remark", scale["bands"][0])
        self.assertIn("templates", self.data(self.get(self.admin, "gradebook-remark-templates")))

        rows = self.component_rows()
        rows[0]["max_score"] = 25
        rows[2]["max_score"] = 55
        response = self.patch(
            self.admin, "gradebook-structure", {"name": "Edited", "components": rows},
            pk=self.structure.pk,
        )
        self.assertEqual(response.status_code, 200, response.data)


class StructureSwitchTests(_Switches):
    def test_hiding_the_maximums_hides_each_maximum_the_total_and_the_verdict(self):
        self.close(MAX_SCORE)
        for row in (
            self.structure_row(),
            self.data(self.get(self.admin, "gradebook-structures"))[0],
        ):
            self.assertNotIn("total", row)
            self.assertNotIn("total_check", row)
            self.assertEqual(len(row["components"]), 3)
            for component in row["components"]:
                self.assertNotIn("max_score", component)
                self.assertIn("name", component)

    def test_the_copy_and_the_activation_answer_with_the_same_filtered_record(self):
        self.close(MAX_SCORE)
        copy = self.post(self.admin, "gradebook-structure-copy", pk=self.structure.pk)
        self.assertEqual(copy.status_code, 201)
        self.assertNotIn("total", self.data(copy))
        self.assertNotIn("max_score", self.data(copy)["components"][0])

    def test_hiding_the_components_hides_the_whole_list(self):
        self.close(COMPONENTS)
        row = self.structure_row()
        self.assertNotIn("components", row)
        self.assertIn("total", row)
        self.assertNotIn("components", self.data(self.get(self.admin, "gradebook-structures"))[0])

    def test_a_role_that_may_not_write_the_maximum_is_refused_a_new_one(self):
        self.close(MAX_SCORE, read=True, write=False)
        rows = self.component_rows()
        rows[0]["max_score"] = 25
        rows[2]["max_score"] = 55
        response = self.patch(
            self.admin, "gradebook-structure", {"components": rows}, pk=self.structure.pk,
        )
        self.assertEqual(response.status_code, 403)
        self.assertIn("max_score", str(response.data))
        self.assertEqual(self.structure.components.first().max_score, 20)

    def test_a_role_that_may_not_write_the_components_can_still_rename_the_structure(self):
        self.close(COMPONENTS, read=True, write=False)
        renamed = self.patch(self.admin, "gradebook-structure", {"name": "Renamed"}, pk=self.structure.pk)
        self.assertEqual(renamed.status_code, 200, renamed.data)
        refused = self.patch(
            self.admin, "gradebook-structure", {"components": self.component_rows()},
            pk=self.structure.pk,
        )
        self.assertEqual(refused.status_code, 403)
        self.assertIn("components", str(refused.data))

    def test_a_role_that_may_not_write_the_components_cannot_create_a_structure(self):
        self.close(COMPONENTS, read=True, write=False)
        response = self.post(self.admin, "gradebook-structures", {
            "name": "New", "program": self.junior.pk, "components": self.components_body(),
        })
        self.assertEqual(response.status_code, 403)
        self.assertEqual(AssessmentStructure.objects.filter(name="New").count(), 0)

    def test_another_role_is_unaffected_by_a_switch_on_the_admin_role(self):
        self.close(MAX_SCORE)
        self.assertIn("total", self.data(self.get(self.viewer, "gradebook-structure", pk=self.structure.pk)))


class ScaleSwitchTests(_Switches):
    def test_hiding_the_bands_hides_the_list_and_its_verdict(self):
        self.close(BANDS)
        for row in (self.scale_row(), self.data(self.get(self.admin, "gradebook-scales"))[0]):
            self.assertNotIn("bands", row)
            self.assertNotIn("check", row)
            self.assertIn("name", row)

    def test_hiding_the_remark_hides_it_on_every_band(self):
        self.close(REMARK)
        bands = self.scale_row()["bands"]
        self.assertEqual(len(bands), 5)
        for band in bands:
            self.assertNotIn("remark", band)
            self.assertIn("grade", band)

    def test_a_role_that_may_not_write_a_remark_is_refused_one(self):
        self.close(REMARK, read=True, write=False)
        rows = [
            {"id": b.pk, "min_score": b.min_score, "grade": b.grade, "remark": "Changed",
             "is_fail": b.is_fail}
            for b in self.scale.bands.all()
        ]
        response = self.patch(self.admin, "gradebook-scale", {"bands": rows}, pk=self.scale.pk)
        self.assertEqual(response.status_code, 403)
        self.assertIn("remark", str(response.data))

    def test_a_role_that_may_not_write_the_bands_is_refused_new_ones(self):
        self.close(BANDS, read=True, write=False)
        response = self.patch(
            self.admin, "gradebook-scale", {"bands": self.bands_body()}, pk=self.scale.pk,
        )
        self.assertEqual(response.status_code, 403)


class TemplateSwitchTests(_Switches):
    def test_hiding_the_templates_hides_the_list(self):
        self.close(TEMPLATES)
        response = self.get(self.admin, "gradebook-remark-templates")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.data(response), {})

    def test_a_role_that_may_not_write_the_templates_is_refused(self):
        self.close(TEMPLATES, read=True, write=False)
        response = self.put(self.admin, "gradebook-remark-templates", {
            "templates": [{"band_min": 0, "text": "Come and see me."}],
        })
        self.assertEqual(response.status_code, 403)
        self.assertIn("templates", str(response.data))


class TellEveryParentSwitchTests(_Switches):
    """Who is told of a released result follows its own switch; the other settings do not."""

    def settings(self):
        return self.data(self.get(self.admin, "gradebook-settings"))

    def test_open_it_is_read_and_changed(self):
        self.assertNotIn("tell_every_parent", self.settings()["_read_only_fields"])
        response = self.patch(self.admin, "gradebook-settings", {"tell_every_parent": True})
        self.assertEqual(response.status_code, 200, response.data)
        self.assertTrue(self.data(response)["tell_every_parent"])

    def test_hidden_it_leaves_the_settings_and_the_rest_stay(self):
        self.close(TELL)
        body = self.settings()
        self.assertNotIn("tell_every_parent", body)
        self.assertIn("rank_in_class", body)

    def test_read_only_it_is_shown_greyed_and_a_change_to_it_is_refused(self):
        self.close(TELL, read=True, write=False)
        body = self.settings()
        self.assertFalse(body["tell_every_parent"])
        self.assertIn("tell_every_parent", body["_read_only_fields"])
        response = self.patch(self.admin, "gradebook-settings", {"tell_every_parent": True})
        self.assertEqual(response.status_code, 403)
        self.assertIn("tell_every_parent", str(response.data))
        allowed = self.patch(self.admin, "gradebook-settings", {"rank_in_class": False})
        self.assertEqual(allowed.status_code, 200, allowed.data)


class RegistryTests(TestCase):
    """The declarations, as the sync writes them against the seeded permission tree."""

    @classmethod
    def setUpTestData(cls):
        for command in (
            "seed_actions", "seed_prebuilt_role_templates", "seed_school_permissions",
        ):
            call_command(command, verbosity=0, stdout=StringIO())
        cls.sync()

    @staticmethod
    def sync(*args):
        """Sync the gradebook's real declarations; the other apps' resources need their own seeds."""
        out = StringIO()
        mine = [d for d in all_declarations() if d.module == "gradebook"]
        with mock.patch(SYNC, return_value=mine):
            call_command("sync_field_registry", *args, stdout=out)
        return out.getvalue()

    def rows(self, resource):
        return {
            f.name: f for f in FieldDefinition.objects.filter(
                resource__module_id="gradebook", resource__name=resource, is_active=True,
            )
        }

    def test_the_sync_writes_the_five_resources_under_the_gradebook_module(self):
        declared = {
            d.resource: {s.name for s in d.fields}
            for d in all_declarations() if d.module == "gradebook"
        }
        self.assertEqual(set(declared), {"sheet", "result", "hold", "setup", "amendment"})
        for resource, names in declared.items():
            self.assertEqual(set(self.rows(resource)), names)

    def test_every_field_of_the_design_table_has_its_own_switch(self):
        sheet, result, hold = self.rows("sheet"), self.rows("result"), self.rows("hold")
        self.assertEqual(
            set(sheet), {"mark", "row_result", "class_stats", "return_note"},
        )
        self.assertEqual(
            set(result),
            {"overall_average", "position", "ratings", "teacher_comment",
             "principal_comment", "guardian_contact", "promotion_line", "email_state"},
        )
        self.assertEqual(set(hold), {"reason", "balance"})

    def test_derived_and_server_owned_fields_are_read_only_and_typed_ones_are_writable(self):
        read_only = {
            "gradebook.sheet.row_result", "gradebook.sheet.class_stats",
            "gradebook.result.overall_average", "gradebook.result.position",
            "gradebook.result.guardian_contact", "gradebook.result.email_state",
            "gradebook.result.promotion_line", "gradebook.hold.balance",
        }
        writable = {
            "gradebook.sheet.mark", "gradebook.sheet.return_note",
            "gradebook.result.ratings", "gradebook.result.teacher_comment",
            "gradebook.result.principal_comment", "gradebook.hold.reason",
            TELL,
        }
        everything = {
            f.key: f for f in FieldDefinition.objects.filter(resource__module_id="gradebook")
        }
        self.assertEqual({k for k, f in everything.items() if not f.writable} & read_only, read_only)
        for key in writable:
            self.assertTrue(everything[key].writable, key)
        self.assertEqual({k for k in read_only if everything[k].writable}, set())

    def test_the_balance_is_the_only_sensitive_field_and_finance_roles_read_it_by_default(self):
        sensitive = {
            f.key for f in FieldDefinition.objects.filter(
                resource__module_id="gradebook", sensitive=True,
            )
        }
        self.assertEqual(sensitive, {"gradebook.hold.balance"})
        readers = set(
            PrebuiltRoleFieldAccess.objects.filter(field_id="gradebook.hold.balance")
            .values_list("prebuilt_role__key", flat=True)
        )
        self.assertEqual(readers, {"school_admin", "finance_admin"})

    def test_the_guardian_switch_is_read_only_and_names_the_guardian_entry(self):
        spec = {s.name: s for s in get_declaration("gradebook", "result").fields}["guardian_contact"]
        self.assertFalse(spec.writable)
        self.assertEqual(spec.resolved_api_names, ("guardian",))

    def test_the_position_has_a_switch_of_its_own_apart_from_the_average(self):
        names = {s.name: s.resolved_api_names for s in get_declaration("gradebook", "result").fields}
        self.assertEqual(names["position"], ("position", "annual_position", "tops"))
        self.assertEqual(
            names["overall_average"],
            ("overall_average", "average", "annual_average", "from", "to", "change"),
        )

    def test_a_second_sync_changes_nothing(self):
        self.assertIn("in sync", self.sync("--check"))

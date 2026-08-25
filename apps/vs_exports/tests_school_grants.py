"""School roles hold the Export Centre keys their screens need.

The Export Centre was platform-only, which meant an Export button on a school's
own class list was refused by RBAC with nothing the reader could act on. What
these assert is the shape of the answer, not just that it changed: a school user
exports what they can already read, a restricted column is still a separate
grant, and reading other people's activity is still nobody's.
"""
from __future__ import annotations

from django.core.management import call_command
from django.test import TestCase

from vs_rbac.models import PrebuiltRolePermission, PrebuiltRoleTemplate


class SchoolExportGrantTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        call_command("seed_actions", verbosity=0)
        call_command("seed_prebuilt_role_templates", verbosity=0)
        call_command("seed_exports_permissions", verbosity=0)

    def keys_for(self, role_key):
        role = PrebuiltRoleTemplate.objects.get(key=role_key)
        return set(
            PrebuiltRolePermission.objects
            # permission_id IS the dotted key (Permission's pk), but it is a
            # FK column, so the text lookup goes through the related field.
            .filter(prebuilt_role=role, permission__key__startswith="exports.")
            .values_list("permission__key", flat=True)
        )

    def test_a_school_admin_can_prepare_run_and_download_an_export(self):
        # The three the Export button on a list screen actually needs: translate
        # the screen, run it, take the file.
        keys = self.keys_for("school_admin")
        self.assertIn("exports.catalogue.view", keys)
        self.assertIn("exports.run.create", keys)
        self.assertIn("exports.file.download", keys)

    def test_a_branch_admin_and_a_teacher_can_export_what_they_can_see(self):
        for role in ("branch_admin", "teacher"):
            keys = self.keys_for(role)
            self.assertIn("exports.catalogue.view", keys, role)
            self.assertIn("exports.run.create", keys, role)
            self.assertIn("exports.file.download", keys, role)

    def test_only_the_school_admin_may_include_a_restricted_column(self):
        """The sensitivity gate has to mean something.

        Granting it to every role that can export would make "sensitive" a label
        rather than a rule - the whole reason it is a key of its own.
        """
        self.assertIn(
            "exports.sensitive_field.export", self.keys_for("school_admin"),
        )
        self.assertNotIn(
            "exports.sensitive_field.export", self.keys_for("branch_admin"),
        )
        self.assertNotIn(
            "exports.sensitive_field.export", self.keys_for("teacher"),
        )

    def test_saving_and_scheduling_stay_with_the_school_admin(self):
        # A branch admin exporting their own list is one thing; leaving a
        # scheduled export running for everyone is another.
        for role in ("branch_admin", "teacher"):
            keys = self.keys_for(role)
            self.assertNotIn("exports.definition.create", keys, role)
            self.assertNotIn("exports.schedule.create", keys, role)
            self.assertNotIn("exports.definition.share", keys, role)

    def test_nobody_at_a_school_reads_other_people_s_export_activity(self):
        # An administrator's power over other administrators, and the read is
        # itself audited. Super-admin only.
        for role in ("school_admin", "branch_admin", "teacher"):
            self.assertNotIn("exports.activity.view", self.keys_for(role), role)

    def test_re_running_the_seeder_changes_nothing(self):
        before = self.keys_for("school_admin")
        call_command("seed_exports_permissions", verbosity=0)
        self.assertEqual(self.keys_for("school_admin"), before)

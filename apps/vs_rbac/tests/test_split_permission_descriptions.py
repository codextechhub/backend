"""Permissions split out of a broad key read as their own action.

Two switches that both read "Manage dunning notices." cannot be told apart on a
role screen. The split migration gives each replacement its own verb, a second
migration repairs databases that already ran the split, and a guard over the
seeded registry keeps the wording distinct for every resource.
"""
from collections import defaultdict
from importlib import import_module
from io import StringIO

from django.apps import apps as global_apps
from django.core.management import call_command
from django.test import TestCase

from vs_rbac.models import Permission

from .helpers import make_permission

SPLIT = "vs_rbac.migrations.0027_manage_is_not_an_action"
REPAIR = "vs_rbac.migrations.0034_split_permissions_name_their_own_action"


class SplitMigrationDescriptionTests(TestCase):
    def test_each_replacement_names_its_own_verb(self):
        make_permission("finance.dunning.manage", description="Manage dunning notices.")

        import_module(SPLIT).replace_manage_permissions(global_apps, None)

        self.assertEqual(
            Permission.objects.get(key="finance.dunning.create").description,
            "Create dunning notices.",
        )
        self.assertEqual(
            Permission.objects.get(key="finance.dunning.update").description,
            "Update dunning notices.",
        )

    def test_an_existing_replacement_with_the_broad_label_is_corrected(self):
        make_permission("finance.dunning.manage", description="Manage dunning notices.")
        make_permission("finance.dunning.update", description="Manage dunning notices.")

        import_module(SPLIT).replace_manage_permissions(global_apps, None)

        self.assertEqual(
            Permission.objects.get(key="finance.dunning.update").description,
            "Update dunning notices.",
        )

    def test_an_existing_replacement_with_its_own_wording_is_kept(self):
        make_permission("finance.dunning.manage", description="Manage dunning notices.")
        make_permission("finance.dunning.update", description="Edit a dunning notice.")

        import_module(SPLIT).replace_manage_permissions(global_apps, None)

        self.assertEqual(
            Permission.objects.get(key="finance.dunning.update").description,
            "Edit a dunning notice.",
        )

    def test_a_description_without_the_broad_prefix_is_rebuilt_from_the_resource(self):
        make_permission("tickets.ticket.manage", description="Everything about tickets.")

        import_module(SPLIT).replace_manage_permissions(global_apps, None)

        self.assertEqual(
            Permission.objects.get(key="tickets.ticket.triage").description,
            "Triage ticket.",
        )


class RepairMigrationTests(TestCase):
    def test_only_replacement_keys_with_the_broad_label_are_rewritten(self):
        make_permission("procurement.approval.view", description="Manage spend approvals.")
        make_permission("procurement.approval.update", description="Manage spend approvals.")
        unrelated = make_permission(
            "finance.invoice.export", description="Manage invoice exports.",
        )

        import_module(REPAIR).name_each_split_permission_by_its_action(global_apps, None)

        self.assertEqual(
            Permission.objects.get(key="procurement.approval.view").description,
            "View spend approvals.",
        )
        self.assertEqual(
            Permission.objects.get(key="procurement.approval.update").description,
            "Update spend approvals.",
        )
        unrelated.refresh_from_db()
        self.assertEqual(unrelated.description, "Manage invoice exports.")

    def test_a_reworded_replacement_is_left_alone(self):
        make_permission("finance.tax.create", description="Add a tax rate.")

        import_module(REPAIR).name_each_split_permission_by_its_action(global_apps, None)

        self.assertEqual(
            Permission.objects.get(key="finance.tax.create").description,
            "Add a tax rate.",
        )


class SeededRegistryDescriptionTests(TestCase):
    """The seeded registry never shows two switches with the same wording.

    A blank description is not wording: the screens fall back to a label built
    from the key, so blank rows are left out of the comparison.
    """

    @classmethod
    def setUpTestData(cls):
        for command in ("seed_actions", "seed_all_permissions"):
            call_command(command, stdout=StringIO(), stderr=StringIO())
        cls.rows = list(
            Permission.objects.values_list(
                "key", "resource_id", "action_id", "description",
            )
        )

    def test_no_two_permissions_on_a_resource_share_a_description(self):
        by_resource = defaultdict(lambda: defaultdict(list))
        for key, resource_id, _action, description in self.rows:
            if not description.strip():
                continue
            by_resource[resource_id][description].append(key)
        clashes = sorted(
            keys
            for descriptions in by_resource.values()
            for keys in descriptions.values()
            if len(keys) > 1
        )
        self.assertEqual(clashes, [], f"Shared descriptions on one resource: {clashes}")

    def test_only_a_manage_action_may_read_as_manage(self):
        offenders = sorted(
            key
            for key, _resource, action, description in self.rows
            if action != "manage" and description.startswith("Manage ")
        )
        self.assertEqual(offenders, [], f"Broad wording on a narrower action: {offenders}")

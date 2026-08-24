"""A school may import its own data, and nothing else.

Written after a school administrator was able, through the public API, to
provision a tenant and to put an account inside CodeX's own tenant. Both were
reproduced against a running server before these were written; see
``datasets.py`` for the account of it.

The three gates are tested separately and on purpose. They are not redundant:
the list is a courtesy, validation is the rule, and the executor catches a batch
that was built before the rule existed.
"""
from __future__ import annotations

from django.test import TestCase

from vs_import_data.datasets import (
    PLATFORM_ONLY_DATASETS,
    TENANT_DATASETS,
    may_import,
    platform_only,
)
from vs_import_data.models import DatasetTypeChoices


class _Tenant:
    def __init__(self, kind):
        self.kind = kind


class _User:
    def __init__(self, kind):
        self.tenant = _Tenant(kind)


SCHOOL = _User("SCHOOL")
CODEX = _User("PLATFORM")


class DatasetOwnershipRuleTests(TestCase):
    """The rule itself, before any layer applies it."""

    def test_provisioning_datasets_are_platform_only(self):
        """The two that were exploited.

        ``schools`` creates a tenant. ``cx_users`` creates an account inside the
        platform tenant - its handler forces that target deliberately, which is
        right for a CodeX operator and catastrophic for anybody else.
        """
        self.assertFalse(may_import(SCHOOL, DatasetTypeChoices.SCHOOLS))
        self.assertFalse(may_import(SCHOOL, DatasetTypeChoices.CX_USERS))

    def test_no_dataset_is_a_school_import_today(self):
        """The honest state of this step, asserted rather than assumed.

        The three datasets it exists for - students, staff, parents - have no
        template and no model to import into. Every dataset that DOES exist
        belongs to CodeX: schools and cx_users are its records, branches are its
        to create, and bank statements are ledger reconciliation rather than
        onboarding.

        This test is meant to fail the day a students template lands. That is
        the point: adding it to TENANT_DATASETS is the only change needed, and
        this is what says so.
        """
        for dataset in DatasetTypeChoices.values:
            self.assertFalse(
                may_import(SCHOOL, dataset),
                f"{dataset} is now a school import - add it to TENANT_DATASETS "
                f"and update this test",
            )

    def test_codex_keeps_every_dataset(self):
        for dataset in DatasetTypeChoices.values:
            self.assertTrue(
                may_import(CODEX, dataset),
                f"CodeX must still be able to import {dataset}",
            )

    def test_an_unknown_dataset_fails_closed(self):
        """The failure that matters is the one nobody is thinking about.

        A dataset added to the choices and forgotten here is withheld from
        schools rather than handed to them. Withholding it is a bug report;
        handing it over is what this module exists to prevent.
        """
        self.assertTrue(platform_only("some_future_dataset"))
        self.assertFalse(may_import(SCHOOL, "some_future_dataset"))
        self.assertFalse(may_import(SCHOOL, None))
        self.assertTrue(may_import(CODEX, "some_future_dataset"))

    def test_every_dataset_type_is_classified(self):
        """Fails when a dataset is added without deciding who owns it.

        This is the test that keeps the module honest. Adding a choice and
        running the suite says, here, that somebody has to choose.
        """
        classified = set(PLATFORM_ONLY_DATASETS) | set(TENANT_DATASETS)
        unclassified = sorted(set(DatasetTypeChoices.values) - classified)
        self.assertEqual(
            unclassified, [],
            "Classify these in vs_import_data/datasets.py - a school may "
            f"import a dataset only if it is listed there: {unclassified}",
        )

    def test_branches_are_not_a_school_dataset(self):
        """A school cannot create a branch by asking, so not by uploading either.

        Every view in vs_schools/views/branch.py demands platform.branches.*,
        which no school role holds - a live school administrator posting to the
        branch endpoint is refused. The import engine asked none of that, so a
        branches CSV created the branch, a branch administrator and a
        branch-scoped role: a way around a permission the API refuses at the
        front door.
        """
        self.assertFalse(may_import(SCHOOL, DatasetTypeChoices.BRANCHES))
        self.assertTrue(may_import(CODEX, DatasetTypeChoices.BRANCHES))

    def test_the_picker_and_the_rule_cannot_disagree(self):
        """The list must narrow by the same set the rule reads.

        These were two expressions of one idea - the list excluded a
        PLATFORM_ONLY set while the rule tested membership of TENANT_DATASETS -
        and they drifted the first time a dataset moved: branches was refused on
        upload and still offered in the picker. A reader saw a template, chose
        it, filled it in, and was refused at the end.
        """
        for dataset in DatasetTypeChoices.values:
            offered = dataset in TENANT_DATASETS
            self.assertEqual(
                offered, may_import(SCHOOL, dataset),
                f"the picker and the rule disagree about {dataset}",
            )

    def test_a_user_with_no_tenant_is_not_treated_as_codex(self):
        class _Nobody:
            tenant = None

        self.assertFalse(may_import(_Nobody(), DatasetTypeChoices.SCHOOLS))
        self.assertFalse(may_import(None, DatasetTypeChoices.SCHOOLS))


class ExecutorRefusesPlatformDatasetsTests(TestCase):
    """The gate that does not assume the other two ran.

    A batch uploaded before this rule existed still has its row handlers reached
    by the job runner. Validation cannot help it - the batch row already exists -
    so the refusal has to live where the write happens.
    """

    def test_a_school_queued_batch_is_skipped_not_executed(self):
        from unittest.mock import patch

        from vs_import_data.services import import_executor as ex
        from vs_import_data.models import ImportRowActionChoices

        class _Template:
            dataset_type = DatasetTypeChoices.SCHOOLS

        class _Batch:
            template = _Template()

        # If the gate is absent, this reaches import_schools_row and provisions
        # a tenant, so the handler is stubbed to fail loudly rather than run.
        with patch.object(
            ex, "import_schools_row",
            side_effect=AssertionError("the school dataset handler must not run"),
        ):
            result = ex.execute_dataset_handler(_Batch(), {}, SCHOOL)

        self.assertEqual(result.action, ImportRowActionChoices.SKIP)
        self.assertIn("not available", result.message)

    def test_codex_still_reaches_the_handler(self):
        from unittest.mock import patch

        from vs_import_data.services import import_executor as ex

        class _Template:
            dataset_type = DatasetTypeChoices.SCHOOLS

        class _Batch:
            template = _Template()

        sentinel = object()
        with patch.object(ex, "import_schools_row", return_value=sentinel) as handler:
            result = ex.execute_dataset_handler(_Batch(), {"a": 1}, CODEX)

        self.assertIs(result, sentinel)
        handler.assert_called_once()

    def test_a_school_queued_branch_batch_is_skipped_too(self):
        """Branches moved out of the school's reach; the executor follows.

        This test used to assert the opposite - that branches reached its
        handler for a school - which is exactly the behaviour that let a school
        create branches it cannot create through the branch endpoint.
        """
        from unittest.mock import patch

        from vs_import_data.services import import_executor as ex
        from vs_import_data.models import ImportRowActionChoices

        class _Template:
            dataset_type = DatasetTypeChoices.BRANCHES

        class _Batch:
            template = _Template()

        with patch.object(
            ex, "import_branches_row",
            side_effect=AssertionError("the branch handler must not run for a school"),
        ):
            result = ex.execute_dataset_handler(_Batch(), {}, SCHOOL)

        self.assertEqual(result.action, ImportRowActionChoices.SKIP)

    def test_codex_still_reaches_the_branch_handler(self):
        """CodeX provisions branches, and must keep being able to."""
        from unittest.mock import patch

        from vs_import_data.services import import_executor as ex

        class _Template:
            dataset_type = DatasetTypeChoices.BRANCHES

        class _Batch:
            template = _Template()

        sentinel = object()
        with patch.object(ex, "import_branches_row", return_value=sentinel):
            result = ex.execute_dataset_handler(_Batch(), {"a": 1}, CODEX)

        self.assertIs(result, sentinel)

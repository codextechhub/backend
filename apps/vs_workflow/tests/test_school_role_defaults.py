"""Which school roles reach the workflow module, and where that is decided.

The decision has to live in the prebuilt role library, because the library is
what every school created from here on is built from. Writing it only into the
tenant roles that exist today leaves the schools created tomorrow without it,
and for ``workflow.template.manage`` that is not a gap a school can close by
itself: the key is restricted, so the only route to it is a change request, and
approving one grants nothing the approver does not already hold. A School Admin
provisioned without the key raises a request nobody in the school can decide.

These tests pin both halves - the library carries the defaults, and the schools
already standing get them too - and the shapes that made the first attempt miss:
the tenant-side spellings, and the branch-scoped copies.
"""
from io import StringIO

from django.core.management import call_command
from django.test import TestCase

from vs_rbac.models import (
    PrebuiltRolePermission,
    PrebuiltRoleTemplate,
    TenantRolePermission,
)
from vs_rbac.services import provision_role_from_prebuilt
from vs_rbac.tests.helpers import make_branch, make_permission, make_role, make_school
from vs_workflow.management.commands.seed_workflow_permissions import (
    SCHOOL_ROLE_DEFAULTS,
)

WORKFLOW_KEYS = [
    "workflow.template.view", "workflow.template.manage",
    "workflow.group.view", "workflow.group.manage",
    "workflow.instance.view", "workflow.instance.cancel",
]


def _seed():
    out = StringIO()
    call_command("seed_workflow_permissions", stdout=out, stderr=out)
    return out.getvalue()


def _granted(role):
    return set(
        TenantRolePermission.objects.filter(role=role, granted=True)
        .values_list("permission_id", flat=True)
    )


class SchoolWorkflowRoleDefaultsTests(TestCase):
    def setUp(self):
        # The seeder registers keys it does not find, but it needs the actions
        # to exist first and skips any whose action is missing. Creating the
        # keys here is the cheap way to have all four actions present.
        for key in WORKFLOW_KEYS:
            make_permission(key)

        # The library is seeded by migration, so these exist already.
        self.library = PrebuiltRoleTemplate.objects.get(key="school_admin")

        self.school = make_school(slug="wf-defaults", name="Workflow Defaults School")
        self.branch = make_branch(self.school)

    def test_the_library_carries_the_defaults(self):
        """What a school created tomorrow is provisioned with."""
        _seed()

        attached = set(
            PrebuiltRolePermission.objects
            .filter(prebuilt_role=self.library)
            .values_list("permission_id", flat=True)
        )
        self.assertEqual(attached, set(SCHOOL_ROLE_DEFAULTS["school_admin"]))
        self.assertIn("workflow.template.manage", attached)

    def test_a_school_provisioned_from_the_library_can_manage_its_own_rules(self):
        """The failure this closes, stated as the thing that should be true.

        A head teacher signing in on her school's first day owns her approval
        rules. Without this she owns nothing under ``workflow.*`` and cannot
        grant herself the key either.
        """
        _seed()

        role = provision_role_from_prebuilt(
            tenant=self.school.tenant, prebuilt_key="school_admin",
        )

        self.assertIn("workflow.template.manage", _granted(role))

    def test_a_school_that_already_exists_is_brought_up_to_the_same_set(self):
        """The library alone would leave every school standing today behind."""
        existing = make_role(self.school, name="School Admin", key="school_admin")
        self.assertEqual(_granted(existing), set())

        _seed()

        self.assertEqual(
            _granted(existing), set(SCHOOL_ROLE_DEFAULTS["school_admin"]),
        )

    def test_the_tenant_side_spelling_is_reached(self):
        """A school's copy does not always carry the library's key.

        ``adopt_console_admin_roles`` created the finance and procurement copies
        with hyphens. Matching on the library key alone would sync neither.
        """
        hyphenated = make_role(self.school, name="Finance Admin", key="finance-admin")

        _seed()

        self.assertEqual(
            _granted(hyphenated), set(SCHOOL_ROLE_DEFAULTS["finance_admin"]),
        )

    def test_a_branch_scoped_copy_is_reached(self):
        """Branch roles take a per-branch key suffix, and were missed once.

        A school with three sites carries ``branch_admin-11``, ``branch_admin-12``
        and ``branch_admin-13``, and an exact-key match reaches none of them.
        """
        per_branch = make_role(
            self.school, name="Branch Admin - Ikeja", key="branch_admin-11",
        )

        _seed()

        self.assertEqual(
            _granted(per_branch), set(SCHOOL_ROLE_DEFAULTS["branch_admin"]),
        )

    def test_a_role_that_merely_starts_the_same_way_is_left_alone(self):
        """A role the school invented is not a copy of the library's.

        "Finance Admin Assistant" is somebody's own role, and quietly granting
        it the finance defaults would be this command deciding what a school's
        own roles reach.
        """
        invented = make_role(
            self.school, name="Finance Assistant", key="finance-admin-assistant",
        )

        _seed()

        self.assertEqual(_granted(invented), set())

    def test_a_school_that_denied_a_key_keeps_its_denial(self):
        """Additive only: a school's own decision is not this command's to undo."""
        existing = make_role(self.school, name="School Admin", key="school_admin")
        TenantRolePermission.objects.create(
            role=existing, permission_id="workflow.instance.cancel", granted=False,
        )

        _seed()

        self.assertNotIn("workflow.instance.cancel", _granted(existing))
        self.assertIn("workflow.template.manage", _granted(existing))

    def test_running_it_twice_grants_nothing_the_second_time(self):
        existing = make_role(self.school, name="School Admin", key="school_admin")
        _seed()
        first = _granted(existing)

        output = _seed()

        self.assertEqual(_granted(existing), first)
        self.assertIn("already", output)

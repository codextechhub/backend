"""One person, several postings: what an admin who already has an account gets.

``provision_admin_user`` is idempotent about the account and deliberately not
about the grant. Finding a User already on the tenant, stamping the admin link
SENT and returning would write the grant that makes an administrator able to do
anything for whichever posting happened to be provisioned first, and for no
other.

Corona names ``head@corona.ng`` as the primary admin of both Lekki and Ikeja in
one create request. The first branch mints the account; the second finds it,
marks its link SENT and stops. Corona's head signs in, and can work at Lekki
only - Ikeja's admin screen refuses them, no error was ever raised, and the
admin link says SENT as though everything went through. The same shape bites
without any duplicate in the request at all: a branch added months later to a
school whose admin already has an account gets no grant either, because the
existing-user path is the same path.

The grant is now written on that path too, keyed on the columns the ACTIVE
partial unique indexes cover so re-running is a no-op rather than a crash.

The invitation email is mocked throughout. These tests are about what is
granted, and the dispatch is the one step that needs a seeded notification
event type to survive - see ENV-10 in the README's Getting started notes.
"""
from unittest import mock

from django.db import transaction
from django.test import TestCase
from django.urls import reverse
from rest_framework.test import APIClient

from vs_rbac.models import (
    PrebuiltRoleTemplate,
    TenantRoleTemplate,
    TenantUserRoleAssignment,
)
from vs_rbac.tests.helpers import make_branch, make_school, make_vision_user
from vs_user.models import User

from .exceptions import AdminProvisioningError
from .models import (
    BranchPrimaryAdmin,
    ContactInfo,
    InviteStatus,
    School,
    SchoolPrimaryAdmin,
)
from vs_tenants.models import Branch, Tenant


def _seed_prebuilt_roles():
    """The two templates school and branch provisioning copy from.

    Without them ``provision_role_from_prebuilt`` returns None,
    ``provision_admin_user`` refuses to mint an admin with no role, and every
    assertion below would be about a User that was never created.
    """
    PrebuiltRoleTemplate.objects.update_or_create(
        key="school_admin",
        defaults={"name": "School Admin", "scope": "institution", "tier": "A"},
    )
    PrebuiltRoleTemplate.objects.update_or_create(
        key="branch_admin",
        defaults={"name": "Branch Admin", "scope": "branch", "tier": "A"},
    )


class ANewSchoolGetsTheRolesCodeXShipsTests(TestCase):
    """Every tenant-wide prebuilt role, on the day the school is created.

    Creation provisioned School Admin and a Branch Admin per branch, and nothing
    else. Teacher, Finance Admin and Procurement Admin reached a school only if
    an operator remembered to run ``adopt_console_admin_roles`` afterwards, so
    schools created days apart carried different sets and a school opening its
    roles screen on its first day found two rows where the product is built
    around five.
    """

    @classmethod
    def setUpTestData(cls):
        cls.vision_user = make_vision_user(
            email="full-set@example.com", super_admin=True,
        )
        _seed_prebuilt_roles()
        for key, name in (
            ("teacher", "Teacher"),
            ("finance_admin", "Finance Admin"),
            ("procurement_admin", "Procurement Admin"),
        ):
            PrebuiltRoleTemplate.objects.update_or_create(
                key=key,
                defaults={"name": name, "scope": "institution", "tier": "A"},
            )

    def _create(self, slug, branch_name):
        client = APIClient()
        client.force_authenticate(user=self.vision_user)
        with mock.patch("vs_user.tasks.send_invitation_email_task.delay"):
            with self.captureOnCommitCallbacks(execute=True):
                return client.post(
                    reverse("school-create"),
                    {
                        "name": slug.replace("-", " ").title(),
                        "slug": slug,
                        "branches": [
                            {
                                "name": branch_name,
                                "state": "Lagos",
                                "is_main": True,
                                "primary_admin_data": {
                                    "full_name": "Ada Obi",
                                    "email": f"admin@{slug}.ng",
                                },
                            },
                        ],
                    },
                    format="json",
                )

    def test_the_full_set_is_provisioned_without_anybody_running_a_command(self):
        response = self._create("holy-trinity", "Main Branch")
        self.assertIn(response.status_code, (200, 201), response.data)

        tenant = Tenant.objects.get(slug="holy-trinity")
        keys = set(
            TenantRoleTemplate.objects.filter(tenant=tenant)
            .values_list("key", flat=True)
        )

        # The four that belong to the school as a whole...
        self.assertTrue(
            {"school_admin", "teacher", "finance_admin", "procurement_admin"}
            <= keys,
            keys,
        )
        # ...and the branch-scoped one, which is keyed per branch.
        self.assertTrue(any(k.startswith("branch_admin-") for k in keys), keys)

    def test_one_branch_means_the_role_does_not_name_it(self):
        """The suffix repeats what the row already says, so it is not there.

        "Branch Admin - Main Branch" on a school with one site says "Branch"
        twice and tells nobody anything. Where a school has one branch the
        dimension recedes, the same way a switcher with one entry does.
        """
        self._create("riverbank-two", "Main Branch")

        tenant = Tenant.objects.get(slug="riverbank-two")
        role = TenantRoleTemplate.objects.get(
            tenant=tenant, key__startswith="branch_admin-",
        )
        self.assertEqual(role.name, "Branch Admin")

    def test_a_second_branch_makes_every_sibling_say_which(self):
        """And the first one is renamed, so neither is ambiguous.

        Leaving it plain beside "Branch Admin - Lekki" would leave a reader
        guessing which site the unlabelled one runs.
        """
        from vs_rbac.services import provision_role_from_prebuilt

        self._create("two-sites", "Ikeja")
        tenant = Tenant.objects.get(slug="two-sites")
        first = TenantRoleTemplate.objects.get(
            tenant=tenant, key__startswith="branch_admin-",
        )
        self.assertEqual(first.name, "Branch Admin")

        lekki = Branch.all_objects.create(
            tenant=tenant, name="Lekki", state="Lagos", status="ACTIVE",
        )
        provision_role_from_prebuilt(
            tenant=tenant, branch=lekki, prebuilt_key="branch_admin",
        )

        first.refresh_from_db()
        self.assertEqual(first.name, "Branch Admin - Ikeja")
        self.assertEqual(
            TenantRoleTemplate.objects.get(
                tenant=tenant, key=f"branch_admin-{lekki.pk}",
            ).name,
            "Branch Admin - Lekki",
        )


class OnePersonWearingSeveralHatsTests(TestCase):
    """The head teacher who is also her own branch admin, in one create request.

    A small school names the same person at the top and at its only site,
    because there is only one person. Two branches of a bigger one are often run
    by the same deputy. Neither is a mistake to refuse: the address is the same
    human, and what differs is what they are allowed to do where.

    The cross-tenant guard cannot see any of this. ``email_refusal`` is called
    with ``tenant=None`` during creation, because the tenant does not exist yet,
    which makes its same-tenant rule vacuous by construction. What keeps these
    right is the reuse in ``provision_admin_user``: one account, one invitation,
    and a grant per posting.
    """

    @classmethod
    def setUpTestData(cls):
        cls.vision_user = make_vision_user(
            email="hats@example.com", super_admin=True,
        )
        _seed_prebuilt_roles()

    def _create(self, slug, school_admin_email, branches):
        client = APIClient()
        client.force_authenticate(user=self.vision_user)
        with mock.patch("vs_user.tasks.send_invitation_email_task.delay") as delay:
            with self.captureOnCommitCallbacks(execute=True):
                response = client.post(
                    reverse("school-create"),
                    {
                        "name": slug.replace("-", " ").title(),
                        "slug": slug,
                        "primary_admin_data": {
                            "full_name": "Ngozi Eze",
                            "email": school_admin_email,
                        },
                        "branches": branches,
                    },
                    format="json",
                )
        return response, delay

    def test_the_head_teacher_is_also_the_admin_of_the_only_branch(self):
        response, delay = self._create(
            "small-school",
            "ngozi@small.ng",
            [{
                "name": "Main Branch", "state": "Lagos", "is_main": True,
                "primary_admin_data": {
                    "full_name": "Ngozi Eze", "email": "ngozi@small.ng",
                },
            }],
        )

        self.assertIn(response.status_code, (200, 201), response.data)
        tenant = Tenant.objects.get(slug="small-school")

        # One account, not two.
        holders = User.objects.filter(email="ngozi@small.ng", tenant=tenant)
        self.assertEqual(holders.count(), 1, "the same address made two accounts")

        # Both hats, on the one account.
        keys = set(
            TenantUserRoleAssignment.objects
            .filter(user=holders.get(), role__tenant=tenant)
            .values_list("role__key", flat=True)
        )
        self.assertIn("school_admin", keys)
        self.assertTrue(
            any(k.startswith("branch_admin") for k in keys),
            f"no branch posting was granted: {keys}",
        )

        # And she is asked to activate once, not once per hat.
        self.assertEqual(delay.call_count, 1, "invited more than once")

    def test_one_person_runs_every_branch_and_the_school(self):
        response, delay = self._create(
            "three-hats",
            "solo@three.ng",
            [
                {
                    "name": "Ikeja", "state": "Lagos", "is_main": True,
                    "primary_admin_data": {
                        "full_name": "Solo Head", "email": "solo@three.ng",
                    },
                },
                {
                    "name": "Lekki", "state": "Lagos",
                    "primary_admin_data": {
                        "full_name": "Solo Head", "email": "solo@three.ng",
                    },
                },
            ],
        )

        self.assertIn(response.status_code, (200, 201), response.data)
        tenant = Tenant.objects.get(slug="three-hats")

        user = User.objects.get(email="solo@three.ng", tenant=tenant)
        keys = set(
            TenantUserRoleAssignment.objects
            .filter(user=user, role__tenant=tenant)
            .values_list("role__key", flat=True)
        )
        self.assertIn("school_admin", keys)
        # A posting at each site, because the grant is the per-branch part.
        self.assertEqual(
            len([k for k in keys if k.startswith("branch_admin-")]), 2, keys,
        )
        self.assertEqual(delay.call_count, 1, "invited more than once")


class SharedAdminAcrossBranchesTests(TestCase):
    """Two branches, one admin address, in a single create request."""

    @classmethod
    def setUpTestData(cls):
        cls.vision_user = make_vision_user(
            email="shared-admin@example.com", super_admin=True,
        )
        _seed_prebuilt_roles()

    def _client(self):
        client = APIClient()
        client.force_authenticate(user=self.vision_user)
        return client

    def _create_corona(self):
        """Corona, with Lekki and Ikeja both administered by the same person."""
        with mock.patch("vs_user.tasks.send_invitation_email_task.delay") as delay:
            with self.captureOnCommitCallbacks(execute=True):
                response = self._client().post(
                    reverse("school-create"),
                    {
                        "name": "Corona Secondary",
                        "slug": "corona-secondary",
                        "branches": [
                            {
                                "name": "Lekki",
                                "state": "Lagos",
                                "is_main": True,
                                "primary_admin_data": {
                                    "full_name": "Bola Adeniyi",
                                    "email": "head@corona.ng",
                                },
                            },
                            {
                                "name": "Ikeja",
                                "state": "Lagos",
                                "is_main": False,
                                "primary_admin_data": {
                                    "full_name": "Bola Adeniyi",
                                    "email": "head@corona.ng",
                                },
                            },
                        ],
                    },
                    format="json",
                )
        self.assertEqual(response.status_code, 201, response.data)
        return School.objects.get(slug="corona-secondary"), delay

    # --- the account is still made once -----------------------------------

    def test_one_account_is_created_for_the_shared_address(self):
        school, _ = self._create_corona()

        self.assertEqual(
            User.objects.filter(email="head@corona.ng", tenant=school.tenant).count(),
            1,
        )

    def test_one_invitation_is_sent_for_the_shared_address(self):
        """Two postings are not two invitations. The person has one inbox."""
        _, delay = self._create_corona()

        self.assertEqual(delay.call_count, 1)

    # --- and granted at both sites ----------------------------------------

    def test_the_shared_admin_is_granted_at_both_branches(self):
        """The defect. Ikeja's grant was the one that went missing."""
        school, _ = self._create_corona()
        user = User.objects.get(email="head@corona.ng", tenant=school.tenant)

        branch_names = {
            assignment.branch.name
            for assignment in TenantUserRoleAssignment.objects.filter(
                tenant=school.tenant,
                user=user,
                assignment_status=TenantUserRoleAssignment.AssignmentStatus.ACTIVE,
            ).select_related("branch")
            if assignment.branch is not None
        }

        self.assertEqual(branch_names, {"Lekki", "Ikeja"})

    def test_each_branch_gets_its_own_branch_admin_role(self):
        """Branch-scoped templates are per-branch: ``branch_admin-<pk>``.

        Asserting on the branch alone would pass if both grants pointed at
        Lekki's template, which is not the same person being able to work at
        Ikeja.
        """
        school, _ = self._create_corona()
        user = User.objects.get(email="head@corona.ng", tenant=school.tenant)

        expected = {
            f"branch_admin-{branch.pk}"
            for branch in school.tenant.branches.all()
        }
        granted = {
            assignment.role.key
            for assignment in TenantUserRoleAssignment.objects.filter(
                tenant=school.tenant, user=user,
            ).select_related("role")
        }

        self.assertEqual(granted, expected)

    def test_both_admin_links_are_marked_sent(self):
        """SENT is now true of both: one invitation covers the one address."""
        school, _ = self._create_corona()

        statuses = {
            link.branch.name: link.invite_status
            for link in BranchPrimaryAdmin.objects.filter(
                branch__tenant=school.tenant,
            ).select_related("branch")
        }

        self.assertEqual(
            statuses, {"Lekki": InviteStatus.SENT, "Ikeja": InviteStatus.SENT},
        )


class ExistingAccountIsGrantedAtItsNewPostingTests(TestCase):
    """The same guard, at the service rather than through an endpoint.

    ``provision_admin_user`` is the choke point all three creation paths pass
    through, and only one of them can reach its existing-user branch today: the
    nested branch list inside school creation, covered above. The standalone
    branch endpoint refuses a known address before it ever gets here, and the
    school-admin path is refused the same way.

    That makes this the guard rather than a duplicate. The pre-checks are the
    only thing keeping the service's own behaviour unreachable from two of its
    three callers, and a pre-check is a rule stated somewhere else - the moment
    one of them is relaxed, or the bulk importer grows a caller, the grant has
    to already be written here. It was not, which is how the defect above
    existed at all.
    """

    @classmethod
    def setUpTestData(cls):
        cls.vision_user = make_vision_user(
            email="existing-posting@example.com", super_admin=True,
        )

    def _post_an_existing_admin_to(self, branch, *, school, role_key, link=None):
        """Run the service the way a creation path does, email mocked.

        ``link`` is reused on a repeat run: ``BranchPrimaryAdmin.branch`` is a
        OneToOne, so a branch has one admin link and a re-run is that same row
        being processed again, not a second one.
        """
        from .models import BranchPrimaryAdmin, ContactInfo
        from .services.admin_provisioning import provision_admin_user

        if link is None:
            contact = ContactInfo.objects.create(
                full_name="Bola Adeniyi", email="head@corona-later.test",
            )
            link = BranchPrimaryAdmin.objects.create(
                branch=branch, contact=contact, branch_role="Head Teacher",
                invite_status=InviteStatus.QUEUED,
            )
        else:
            contact = link.contact
        with mock.patch("vs_user.tasks.send_invitation_email_task.delay") as delay:
            with self.captureOnCommitCallbacks(execute=True):
                returned = provision_admin_user(
                    contact=contact, admin_link=link, school=school, branch=branch,
                    role=role_key, actor=self.vision_user,
                )
        return returned, link, delay

    def _corona_with_an_incumbent(self):
        """Corona, its Ikeja site, and a head who already has an account."""
        school = make_school(slug="corona-later", name="Corona Later")
        ikeja = make_branch(school, name="Ikeja")
        role = TenantRoleTemplate.objects.create(
            tenant=school.tenant, key=f"branch_admin-{ikeja.pk}",
            name="Branch Admin - Ikeja", branch=ikeja, status="ACTIVE",
        )
        incumbent = User.objects.create_user(
            email="head@corona-later.test", password="testpass123",
            tenant=school.tenant, status="ACTIVE",
            first_name="Bola", last_name="Adeniyi",
        )
        return school, ikeja, role, incumbent

    def test_the_existing_account_is_granted_at_the_new_branch(self):
        """The defect: the account was found and the grant was never written."""
        school, ikeja, role, incumbent = self._corona_with_an_incumbent()

        returned, _, _ = self._post_an_existing_admin_to(
            ikeja, school=school, role_key=role.key,
        )

        self.assertEqual(returned, incumbent)
        self.assertTrue(
            TenantUserRoleAssignment.objects.filter(
                tenant=school.tenant, user=incumbent, role=role, branch=ikeja,
                assignment_status=TenantUserRoleAssignment.AssignmentStatus.ACTIVE,
            ).exists(),
            "the branch's admin holds no grant at the branch they administer",
        )

    def test_no_second_account_is_minted(self):
        """The account itself is minted once, however many postings name it."""
        school, ikeja, role, _ = self._corona_with_an_incumbent()

        self._post_an_existing_admin_to(ikeja, school=school, role_key=role.key)

        self.assertEqual(
            User.objects.filter(
                email="head@corona-later.test", tenant=school.tenant,
            ).count(),
            1,
        )

    def test_no_second_invitation_is_sent(self):
        school, ikeja, role, _ = self._corona_with_an_incumbent()

        _, _, delay = self._post_an_existing_admin_to(
            ikeja, school=school, role_key=role.key,
        )

        self.assertEqual(delay.call_count, 0)

    def test_the_link_is_marked_sent(self):
        school, ikeja, role, _ = self._corona_with_an_incumbent()

        _, link, _ = self._post_an_existing_admin_to(
            ikeja, school=school, role_key=role.key,
        )

        link.refresh_from_db()
        self.assertEqual(link.invite_status, InviteStatus.SENT)

    def test_repeating_the_posting_is_a_no_op_rather_than_a_crash(self):
        """``get_or_create`` on the columns the ACTIVE partial index covers.

        A plain ``create`` here would raise IntegrityError the second time, the
        savepoint would roll back, and a re-run of an import would leave the
        link QUEUED with the grant already in place - a failure that looks like
        the defect it replaced.
        """
        school, ikeja, role, incumbent = self._corona_with_an_incumbent()

        _, link, _ = self._post_an_existing_admin_to(
            ikeja, school=school, role_key=role.key,
        )
        returned, _, _ = self._post_an_existing_admin_to(
            ikeja, school=school, role_key=role.key, link=link,
        )

        self.assertEqual(returned, incumbent)
        self.assertEqual(
            TenantUserRoleAssignment.objects.filter(
                tenant=school.tenant, user=incumbent, role=role, branch=ikeja,
            ).count(),
            1,
        )

    def test_a_posting_with_no_role_template_is_refused(self):
        """An admin who can sign in and do nothing is not a provisioned admin.

        The same refusal the fresh-account path makes. Without it the
        existing-user branch quietly stamps the link SENT and hands back an
        account with no authority at the site it was posted to, which is this
        class's own defect wearing a different hat.
        """
        school, ikeja, _, _ = self._corona_with_an_incumbent()

        with self.assertRaises(AdminProvisioningError):
            _, link, _ = self._post_an_existing_admin_to(
                ikeja, school=school, role_key="no-such-role",
            )

        link = BranchPrimaryAdmin.objects.get(branch=ikeja)
        link.refresh_from_db()
        self.assertEqual(link.invite_status, InviteStatus.QUEUED)


class RequiredAdminProvisioningIsAtomicTests(TestCase):
    """Creation never commits a school or branch without its required admin."""

    @classmethod
    def setUpTestData(cls):
        cls.vision_user = make_vision_user(
            email="atomic-admin-provisioning@example.com", super_admin=True,
        )

    def _client(self):
        client = APIClient()
        client.force_authenticate(user=self.vision_user)
        return client

    @staticmethod
    def _branch(name, email, *, is_main=True):
        return {
            "name": name,
            "state": "Lagos",
            "is_main": is_main,
            "primary_admin_data": {
                "full_name": f"{name} Head",
                "email": email,
            },
        }

    def test_school_create_returns_503_and_rolls_back_when_role_is_missing(self):
        PrebuiltRoleTemplate.objects.filter(key="branch_admin").delete()

        with self.assertLogs("vs_schools.admin_provisioning", level="ERROR"):
            response = self._client().post(
                reverse("school-create"),
                {
                    "name": "Bright Star School",
                    "slug": "bright-star-atomic",
                    "branches": [self._branch(
                        "Main Branch", "head@bright-star-atomic.test",
                    )],
                },
                format="json",
            )

        self.assertEqual(response.status_code, 503, response.data)
        self.assertEqual(
            response.data["error"]["code"], "ADMIN_PROVISIONING_FAILED",
        )
        self.assertFalse(School.objects.filter(slug="bright-star-atomic").exists())
        self.assertFalse(Tenant.objects.filter(slug="bright-star-atomic").exists())
        self.assertFalse(Branch.all_objects.filter(
            tenant__slug="bright-star-atomic",
        ).exists())
        self.assertFalse(ContactInfo.objects.filter(
            email="head@bright-star-atomic.test",
        ).exists())
        self.assertFalse(User.objects.filter(
            email="head@bright-star-atomic.test",
        ).exists())

    def test_second_branch_failure_rolls_back_the_first_admin_too(self):
        _seed_prebuilt_roles()
        from .services.admin_provisioning import provision_admin_user

        calls = 0

        def fail_the_second_admin(**kwargs):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise AdminProvisioningError()
            return provision_admin_user(**kwargs)

        with mock.patch(
            "schools.vs_schools.services.admin_provisioning.provision_admin_user",
            side_effect=fail_the_second_admin,
        ), mock.patch("vs_user.tasks.send_invitation_email_task.delay"):
            response = self._client().post(
                reverse("school-create"),
                {
                    "name": "Two Branch School",
                    "slug": "two-branch-atomic",
                    "branches": [
                        self._branch(
                            "Main Branch", "main@two-branch-atomic.test",
                            is_main=True,
                        ),
                        self._branch(
                            "Ikeja Branch", "ikeja@two-branch-atomic.test",
                            is_main=False,
                        ),
                    ],
                },
                format="json",
            )

        self.assertEqual(response.status_code, 503, response.data)
        self.assertFalse(School.objects.filter(slug="two-branch-atomic").exists())
        self.assertFalse(Tenant.objects.filter(slug="two-branch-atomic").exists())
        self.assertFalse(User.objects.filter(
            email__in=[
                "main@two-branch-atomic.test",
                "ikeja@two-branch-atomic.test",
            ],
        ).exists())
        self.assertFalse(BranchPrimaryAdmin.objects.filter(
            contact__email__in=[
                "main@two-branch-atomic.test",
                "ikeja@two-branch-atomic.test",
            ],
        ).exists())

    def test_failed_school_admin_cannot_leave_same_email_branch_marked_sent(self):
        _seed_prebuilt_roles()
        PrebuiltRoleTemplate.objects.filter(key="school_admin").delete()
        email = "ada@same-admin-atomic.test"

        with self.assertLogs("vs_schools.admin_provisioning", level="ERROR"):
            response = self._client().post(
                reverse("school-create"),
                {
                    "name": "Same Admin School",
                    "slug": "same-admin-atomic",
                    "primary_admin_data": {
                        "full_name": "Ada Okoye",
                        "email": email,
                    },
                    "branches": [self._branch("Main Branch", email)],
                },
                format="json",
            )

        self.assertEqual(response.status_code, 503, response.data)
        self.assertFalse(School.objects.filter(slug="same-admin-atomic").exists())
        self.assertFalse(SchoolPrimaryAdmin.objects.filter(
            contact__email=email,
        ).exists())
        self.assertFalse(BranchPrimaryAdmin.objects.filter(
            contact__email=email,
        ).exists())

    def test_standalone_branch_create_rolls_back_only_the_new_branch(self):
        school = make_school(
            slug="existing-school-atomic", name="Existing School Atomic",
        )
        school.status = "ACTIVE"
        school.save(update_fields=["status"])
        PrebuiltRoleTemplate.objects.filter(key="branch_admin").delete()

        with self.assertLogs("vs_schools.admin_provisioning", level="ERROR"):
            response = self._client().post(
                reverse("branch-create", kwargs={"slug": school.slug}),
                self._branch(
                    "New Branch", "head@new-branch-atomic.test", is_main=False,
                ),
                format="json",
            )

        self.assertEqual(response.status_code, 503, response.data)
        self.assertTrue(School.objects.filter(pk=school.pk).exists())
        self.assertFalse(Branch.all_objects.filter(
            tenant=school.tenant, name="New Branch",
        ).exists())
        self.assertFalse(User.objects.filter(
            email="head@new-branch-atomic.test", tenant=school.tenant,
        ).exists())


class ProvisioningInviteWaitsForCommitTests(TestCase):
    """The invite is queued after the commit that publishes the invitation row.

    ``provision_admin_user`` runs inside a savepoint of the creation request's
    transaction. Queued from in there, the job carries an invitation id the
    database has not published yet: a worker that starts first finds no row and
    returns, and the head teacher whose link says SENT never gets an email.
    Worse, the savepoint can still roll back afterwards - an email advertising
    an account that no longer exists.
    """

    @classmethod
    def setUpTestData(cls):
        cls.actor = make_vision_user(email="commit-provision@example.com",
                                     super_admin=True)
        _seed_prebuilt_roles()

    def _posting(self):
        school = make_school(slug="commit-school", name="Commit School")
        branch = make_branch(school, name="Ikeja")
        role = TenantRoleTemplate.objects.create(
            tenant=school.tenant, key=f"branch_admin-{branch.pk}",
            name="Branch Admin - Ikeja", branch=branch, status="ACTIVE",
        )
        contact = ContactInfo.objects.create(
            full_name="Bola Adeniyi", email="head@commit-school.test",
        )
        link = BranchPrimaryAdmin.objects.create(
            branch=branch, contact=contact, branch_role="Head Teacher",
            invite_status=InviteStatus.QUEUED,
        )
        return school, branch, role, contact, link

    def test_the_invite_is_not_queued_until_the_invitation_row_commits(self):
        from vs_user.models import UserInvitation

        from .services.admin_provisioning import provision_admin_user

        school, branch, role, contact, link = self._posting()
        with mock.patch("vs_user.tasks.send_invitation_email_task.delay") as delay:
            with self.captureOnCommitCallbacks() as callbacks:
                provision_admin_user(
                    contact=contact, admin_link=link, school=school,
                    branch=branch, role=role.key, actor=self.actor,
                )
            self.assertFalse(
                delay.called,
                "the invite must not be queued while its row is uncommitted",
            )
            self.assertEqual(len(callbacks), 1)
            callbacks[0]()

        invitation = UserInvitation.objects.get(
            user__email="head@commit-school.test",
        )
        self.assertEqual(delay.call_args.kwargs["invitation_id"], invitation.pk)

    def test_a_rolled_back_provisioning_queues_no_invite(self):
        from .services.admin_provisioning import provision_admin_user

        school, branch, role, contact, link = self._posting()
        with mock.patch("vs_user.tasks.send_invitation_email_task.delay") as delay:
            with self.captureOnCommitCallbacks(execute=True):
                with self.assertRaises(RuntimeError):
                    with transaction.atomic():
                        provision_admin_user(
                            contact=contact, admin_link=link, school=school,
                            branch=branch, role=role.key, actor=self.actor,
                        )
                        raise RuntimeError("the rest of the creation failed")

        self.assertFalse(delay.called)
        self.assertFalse(
            User.objects.filter(email="head@commit-school.test").exists(),
        )


class TheAdminLinkNeverClaimsAnInvitationNobodySentTests(TestCase):
    """Greenfield Academy is created while Redis is unavailable.

    The account is made and its grant is written, and then the enqueue that
    should have produced the principal's invitation email is refused by a broker
    that is not there. The link was stamped SENT inside the creation
    transaction, before the enqueue had even been attempted, so the console
    reported an invitation that no task existed for and no email carried. Ada
    could not activate Greenfield, and nothing on the record said why.

    The link is written from the hand-off's outcome now, and written again when
    a retry succeeds - a school does not stay marked Failed for an invitation
    that has since gone out.
    """

    class _BrokerDown(Exception):
        """Stands in for the broker's connection error, which is not the point."""

    @classmethod
    def setUpTestData(cls):
        cls.actor = make_vision_user(
            email="broker-outage@example.com", super_admin=True,
        )

    def _greenfield(self):
        """A school, its branch, the branch admin role, and a queued admin link."""
        school = make_school(slug="greenfield", name="Greenfield Academy")
        branch = make_branch(school, name="Main Branch")
        role = TenantRoleTemplate.objects.create(
            tenant=school.tenant, key=f"branch_admin-{branch.pk}",
            name="Branch Admin - Main Branch", branch=branch, status="ACTIVE",
        )
        contact = ContactInfo.objects.create(
            full_name="Ada Okoye", email="principal@greenfield.test",
        )
        link = BranchPrimaryAdmin.objects.create(
            branch=branch, contact=contact, branch_role="Head Teacher",
            invite_status=InviteStatus.QUEUED,
        )
        return school, branch, role, link

    def _provision(self, *, refused):
        from .services.admin_provisioning import provision_admin_user

        school, branch, role, link = self._greenfield()
        with mock.patch(
            "vs_user.tasks.send_invitation_email_task.delay",
            side_effect=self._BrokerDown("connection refused") if refused else None,
        ):
            with self.captureOnCommitCallbacks(execute=True):
                user = provision_admin_user(
                    contact=link.contact, admin_link=link, school=school,
                    branch=branch, role=role.key, actor=self.actor,
                )
        link.refresh_from_db()
        return link, user, role, school

    def test_the_link_is_marked_failed_when_the_broker_refuses(self):
        link, _, _, _ = self._provision(refused=True)

        self.assertEqual(link.invite_status, InviteStatus.FAILED)
        self.assertIsNone(link.invite_sent_at)

    def test_the_link_is_marked_sent_only_once_the_broker_has_the_job(self):
        link, _, _, _ = self._provision(refused=False)

        self.assertEqual(link.invite_status, InviteStatus.SENT)
        self.assertIsNotNone(link.invite_sent_at)

    def test_the_account_and_its_grant_survive_the_outage(self):
        """The school is still created. Only the email is owed.

        Rolling the provisioning back would be worse: the account, the role and
        the branch would all have to be made again, and the one thing actually
        missing is recoverable on its own.
        """
        link, user, role, school = self._provision(refused=True)

        self.assertEqual(user.email, "principal@greenfield.test")
        self.assertTrue(
            TenantUserRoleAssignment.objects.filter(
                tenant=school.tenant, user=user, role=role,
                assignment_status=TenantUserRoleAssignment.AssignmentStatus.ACTIVE,
            ).exists()
        )

    def test_a_successful_retry_clears_the_failed_badge(self):
        from vs_user.services.invitation import InvitationService

        link, _, _, _ = self._provision(refused=True)

        with mock.patch("vs_user.tasks.send_invitation_email_task.delay"):
            with self.captureOnCommitCallbacks(execute=True):
                summary = InvitationService.retry_failed_deliveries()

        self.assertEqual(summary["retried"], 1)
        link.refresh_from_db()
        self.assertEqual(link.invite_status, InviteStatus.SENT)
        self.assertIsNotNone(link.invite_sent_at)


class AnAdministratorIsAMemberOfStaffTests(TestCase):
    """Provisioning writes the staff record the school reads its people from.

    The directory lists the people who hold a ``StaffProfile``, and this service
    wrote the account, the grant and the invitation without one. So a school's
    own administrators were absent from its staff list, and stayed absent: the
    head teacher who runs a branch could not be given a class, could not file
    leave, was missing from her own branch roster, and could not be found by the
    search box - while the checklist card above the empty directory read as done,
    because it counts accounts rather than records.
    """

    @classmethod
    def setUpTestData(cls):
        cls.vision_user = make_vision_user(
            email="admins-are-staff@example.com", super_admin=True,
        )
        _seed_prebuilt_roles()

    def _create(self, slug, *, school_admin=None, branches):
        client = APIClient()
        client.force_authenticate(user=self.vision_user)
        payload = {
            "name": slug.replace("-", " ").title(),
            "slug": slug,
            "branches": branches,
        }
        if school_admin:
            payload["primary_admin_data"] = school_admin
        with mock.patch("vs_user.tasks.send_invitation_email_task.delay"):
            with self.captureOnCommitCallbacks(execute=True):
                response = client.post(
                    reverse("school-create"), payload, format="json",
                )
        self.assertIn(response.status_code, (200, 201), response.data)
        return Tenant.objects.get(slug=slug)

    @staticmethod
    def _profile(user):
        from schools.vs_staff.models import StaffProfile

        return StaffProfile.all_objects.filter(user=user).first()

    def test_the_school_admin_is_on_the_staff_list(self):
        tenant = self._create(
            "st-monicas-staffed",
            school_admin={"full_name": "Grace Okonkwo", "email": "grace@monicas.ng"},
            branches=[{
                "name": "Main Branch", "state": "Lagos", "is_main": True,
                "primary_admin_data": {
                    "full_name": "Tunde Adeyemi", "email": "tunde@monicas.ng",
                },
            }],
        )

        profile = self._profile(User.objects.get(email="grace@monicas.ng", tenant=tenant))
        self.assertIsNotNone(profile, "the school's own administrator has no record")
        # School-wide, because that is what a null posting means and what
        # running the whole school is.
        self.assertIsNone(profile.branch_id)
        # The title the creation form collected, not the role she holds.
        self.assertEqual(profile.job_title, "IT Head")

    def test_the_branch_admin_is_posted_to_their_branch(self):
        tenant = self._create(
            "brightfield-staffed",
            branches=[{
                "name": "Ikeja", "state": "Lagos", "is_main": True,
                "primary_admin_data": {
                    "full_name": "Tunde Adeyemi", "email": "tunde@brightfield.ng",
                    "branch_role": "Head Teacher",
                },
            }],
        )

        profile = self._profile(
            User.objects.get(email="tunde@brightfield.ng", tenant=tenant),
        )
        self.assertIsNotNone(profile)
        self.assertEqual(profile.branch.name, "Ikeja")
        self.assertEqual(profile.job_title, "Head Teacher")

    def test_the_record_starts_invited_and_says_so_in_its_history(self):
        """Invited, because nobody has used the link yet.

        The activation signal moves it to Active when they do, which is the one
        path out of Invited. A record written Active here would tell a school
        that somebody who has never signed in is at work.
        """
        from schools.vs_staff.models import StaffEmploymentEvent

        tenant = self._create(
            "new-dawn-staffed",
            branches=[{
                "name": "Main Branch", "state": "Lagos", "is_main": True,
                "primary_admin_data": {
                    "full_name": "Ada Obi", "email": "ada@new-dawn.ng",
                },
            }],
        )

        profile = self._profile(User.objects.get(email="ada@new-dawn.ng", tenant=tenant))
        self.assertEqual(profile.employment_status, "INVITED")

        event = StaffEmploymentEvent.all_objects.get(staff=profile)
        self.assertEqual(event.from_status, "")
        self.assertEqual(event.to_status, "INVITED")

    def test_one_person_wearing_two_hats_gets_one_record(self):
        """And keeps the school-wide posting the first hat gave her.

        A posting is where somebody is based and there is one of it. Rewriting
        it for the second hat would move the person who runs the whole school
        onto whichever branch happened to be provisioned last, and take her off
        every other branch's roster.
        """
        from schools.vs_staff.models import StaffProfile

        tenant = self._create(
            "small-staffed",
            school_admin={"full_name": "Ngozi Eze", "email": "ngozi@small.ng"},
            branches=[{
                "name": "Main Branch", "state": "Lagos", "is_main": True,
                "primary_admin_data": {
                    "full_name": "Ngozi Eze", "email": "ngozi@small.ng",
                },
            }],
        )

        user = User.objects.get(email="ngozi@small.ng", tenant=tenant)
        self.assertEqual(StaffProfile.all_objects.filter(user=user).count(), 1)
        self.assertIsNone(StaffProfile.all_objects.get(user=user).branch_id)


class AnIncumbentsRecordReadsActiveTests(TestCase):
    """Somebody already signing in is not somebody with a pending invitation.

    A record starts at Invited and leaves it once, when the invited person uses
    their own link. Corona's head has an account and has been using it for a
    year when Ikeja opens and names her its administrator: a record opened at
    Invited there has no activation left to promote it, so it would read as an
    unaccepted invitation for the rest of her employment, with a Resend button
    beside it.
    """

    @classmethod
    def setUpTestData(cls):
        cls.vision_user = make_vision_user(
            email="incumbent-record@example.com", super_admin=True,
        )

    def test_the_record_written_for_an_existing_account_reads_active(self):
        from schools.vs_staff.models import StaffProfile

        from .models import BranchPrimaryAdmin, ContactInfo
        from .services.admin_provisioning import provision_admin_user

        school = make_school(slug="corona-incumbent", name="Corona Incumbent")
        ikeja = make_branch(school, name="Ikeja")
        role = TenantRoleTemplate.objects.create(
            tenant=school.tenant, key=f"branch_admin-{ikeja.pk}",
            name="Branch Admin - Ikeja", branch=ikeja, status="ACTIVE",
        )
        incumbent = User.objects.create_user(
            email="head@corona-incumbent.test", password="testpass123",
            tenant=school.tenant, status="ACTIVE",
            first_name="Bola", last_name="Adeniyi",
        )
        contact = ContactInfo.objects.create(
            full_name="Bola Adeniyi", email="head@corona-incumbent.test",
        )
        link = BranchPrimaryAdmin.objects.create(
            branch=ikeja, contact=contact, branch_role="Head Teacher",
            invite_status=InviteStatus.QUEUED,
        )

        with mock.patch("vs_user.tasks.send_invitation_email_task.delay"):
            with self.captureOnCommitCallbacks(execute=True):
                provision_admin_user(
                    contact=contact, admin_link=link, school=school, branch=ikeja,
                    role=role.key, actor=self.vision_user,
                )

        profile = StaffProfile.all_objects.get(user=incumbent)
        self.assertEqual(profile.employment_status, "ACTIVE")
        # Her own account's posting, which is the older fact. The branch this
        # hat names is her reach, and that comes from the grant.
        self.assertIsNone(profile.branch_id)

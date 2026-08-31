"""One person, several postings: what an admin who already has an account gets.

``provision_admin_user`` is idempotent about the account and used to be
idempotent about everything else with it. Finding a User already on the tenant,
it stamped the admin link SENT and returned - so the grant that makes an
administrator able to do anything was written only for whichever posting
happened to be provisioned first.

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
        """The idempotency this path has always had must survive the fix."""
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

        The same refusal the fresh-account path has always made. Without it the
        existing-user branch would quietly stamp the link SENT and hand back an
        account with no authority at the site it was posted to - which is the
        defect this class exists for, wearing a different hat.
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

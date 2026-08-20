"""The main-branch guard as an API caller meets it.

The rule itself lives in ``Branch.transition`` and is proved against the model
in ``vs_tenants.tests``. What is proved here is the half a school admin
actually experiences: the transition endpoint answers 409 with a code and a
sentence naming the way out, and the way out - the ``is_main`` field on the
branch update endpoint - actually works.

Two shapes of school throughout, because a single-branch test proves nothing
about a multi-branch one and the two get *different* refusals: one can promote
a sibling, the other has none.
"""
from django.test import TestCase
from django.urls import reverse
from rest_framework.test import APIClient

from vs_rbac.tests.helpers import make_branch, make_school, make_vision_user
from vs_tenants.models import Branch, BranchStatus


class BranchTransitionMainGuardTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.vision_user = make_vision_user(
            email="main-guard@example.com", super_admin=True,
        )

        cls.corona = make_school(slug="mg-corona", name="Corona Secondary")
        cls.vi = make_branch(
            cls.corona, name="Victoria Island", is_main=True, _type="Main",
        )
        cls.lekki = make_branch(
            cls.corona, name="Lekki", is_main=False, _type="Annex",
        )

        cls.bright_star = make_school(slug="mg-bright-star", name="Bright Star")
        cls.only_branch = make_branch(
            cls.bright_star, name="Bright Star Main", is_main=True, _type="Main",
        )

    def _client(self):
        client = APIClient()
        client.force_authenticate(user=self.vision_user)
        return client

    def _transition(self, school, branch, to_state, reason="shut"):
        return self._client().post(
            reverse(
                "branch-transition",
                kwargs={"slug": school.slug, "code": branch.code},
            ),
            {"to_state": to_state, "reason": reason},
            format="json",
        )

    def _update(self, school, branch, payload):
        return self._client().patch(
            reverse(
                "branch-update",
                kwargs={"slug": school.slug, "code": branch.code},
            ),
            payload,
            format="json",
        )

    # --- the refusal --------------------------------------------------------

    def test_closing_the_main_branch_answers_409_with_the_way_out(self):
        response = self._transition(self.corona, self.vi, "CLOSED")

        self.assertEqual(response.status_code, 409, response.data)
        self.assertEqual(
            response.data["error"]["code"], "MAIN_BRANCH_CANNOT_LEAVE_SERVICE",
        )
        self.assertIn("main branch", response.data["message"])
        self.vi.refresh_from_db()
        self.assertEqual(self.vi.status, BranchStatus.ACTIVE)

    def test_suspending_and_deactivating_the_main_branch_are_refused_too(self):
        for to_state in ("SUSPENDED", "INACTIVE"):
            with self.subTest(to_state=to_state):
                response = self._transition(self.corona, self.vi, to_state)

                self.assertEqual(response.status_code, 409, response.data)
                self.assertEqual(
                    response.data["error"]["code"],
                    "MAIN_BRANCH_CANNOT_LEAVE_SERVICE",
                )
                self.vi.refresh_from_db()
                self.assertEqual(self.vi.status, BranchStatus.ACTIVE)

    def test_the_only_branch_gets_advice_it_can_act_on(self):
        response = self._transition(
            self.bright_star, self.only_branch, "CLOSED",
        )

        self.assertEqual(response.status_code, 409, response.data)
        self.assertEqual(
            response.data["error"]["code"], "LAST_BRANCH_CANNOT_LEAVE_SERVICE",
        )
        # It must not tell an admin to promote a branch that does not exist.
        self.assertNotIn("another branch", response.data["message"])

    def test_a_non_main_branch_still_closes(self):
        response = self._transition(self.corona, self.lekki, "CLOSED")

        self.assertEqual(response.status_code, 200, response.data)
        self.lekki.refresh_from_db()
        self.assertEqual(self.lekki.status, BranchStatus.CLOSED)

    # --- the way out --------------------------------------------------------

    def test_promote_then_close_is_the_sequence_the_message_describes(self):
        promote = self._update(self.corona, self.lekki, {"is_main": True})
        self.assertEqual(promote.status_code, 200, promote.data)

        self.lekki.refresh_from_db()
        self.vi.refresh_from_db()
        self.assertTrue(self.lekki.is_main)
        self.assertFalse(self.vi.is_main)
        self.assertEqual(
            Branch.all_objects.filter(
                tenant=self.corona.tenant, is_main=True,
            ).count(),
            1,
        )

        close = self._transition(self.corona, self.vi, "CLOSED")
        self.assertEqual(close.status_code, 200, close.data)
        self.vi.refresh_from_db()
        self.assertEqual(self.vi.status, BranchStatus.CLOSED)

    def test_promotion_used_to_be_refused_outright(self):
        """The regression this change is really about.

        ``is_main=true`` was rejected whenever another main branch existed,
        which is every school - so the advice "make another branch main first"
        had no path behind it. It is now a handover.
        """
        response = self._update(self.corona, self.lekki, {"is_main": True})

        self.assertEqual(response.status_code, 200, response.data)
        self.assertNotIn("already exists", str(response.data))

    def test_an_out_of_service_branch_cannot_be_promoted(self):
        self.lekki.transition(to_state=BranchStatus.CLOSED, actor_id="1")

        response = self._update(self.corona, self.lekki, {"is_main": True})

        self.assertEqual(response.status_code, 400, response.data)
        self.vi.refresh_from_db()
        self.assertTrue(self.vi.is_main)

    def test_the_main_flag_cannot_simply_be_cleared(self):
        """The same damage by another route, so it is refused the same way.

        Were this allowed, an admin who wanted Victoria Island closed could
        clear its ``is_main`` and close it, leaving Corona with no main branch
        and ``School.main_branch`` returning None for every reader.
        """
        response = self._update(self.corona, self.vi, {"is_main": False})

        self.assertEqual(response.status_code, 400, response.data)
        self.vi.refresh_from_db()
        self.assertTrue(self.vi.is_main)
        self.assertEqual(self.corona.main_branch, self.vi)

    def test_promotion_does_not_reach_into_another_school(self):
        self._update(self.corona, self.lekki, {"is_main": True})

        self.only_branch.refresh_from_db()
        self.assertTrue(self.only_branch.is_main)

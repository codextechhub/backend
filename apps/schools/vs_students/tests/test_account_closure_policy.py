"""How pupils and parents close their own accounts at a school.

Brightfield has two branches, Lekki and Ikeja, and Sunrise has one. Mrs Adaeze
Nwosu has Chiamaka at Lekki and Kemi at Ikeja, so her request belongs to no one
branch. Mr Tunde Ade has one child, at Ikeja. Mrs Ngozi Eze teaches at
Brightfield and her daughter attends it, so her sign-in is her working login
and keeps the refusal that points at an administrator. Chiamaka has her own
pupil sign-in, which only the school may close.
"""
from __future__ import annotations

from core.test_utils import TenantAPIClient
from schools.vs_students.account_closure import closure_policy
from schools.vs_students.constants import StudentStatus
from schools.vs_students.models import Guardian
from vs_rbac.models import PermissionScope
from vs_rbac.tests.helpers import (
    make_assignment,
    make_permission,
    make_role,
    make_role_permission,
    make_school_admin,
)
from vs_user.models import AccountClosureRequest, User
from vs_user.services.account_closure import (
    MODE_CLOSE,
    MODE_REQUEST,
    PERM_UPDATE,
    PERM_VIEW,
)

from .base import StudentsFixture

ME = "/v1/user/me/account-closure/"
LIST = "/v1/user/account-closures/"


def account(email, tenant, first, last):
    return make_school_admin(None, email=email, tenant=tenant, first_name=first, last_name=last)


class ClosurePolicyFixture(StudentsFixture):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        view = make_permission(PERM_VIEW, scope=PermissionScope.TENANT)
        update = make_permission(PERM_UPDATE, scope=PermissionScope.TENANT)
        for role in (cls.role, cls.solo_role):
            make_role_permission(role, view)
            make_role_permission(role, update)

        # Mrs Nwosu: one parent, two children at two branches.
        cls.mother_user = account("adaeze@nwosu.test", cls.tenant, "Adaeze", "Nwosu")
        cls.mother = cls.guardian(
            cls, name="Mrs. Adaeze Nwosu", email="adaeze@nwosu.test", phone="08021112233",
        )
        cls.mother.user = cls.mother_user
        cls.mother.save(update_fields=["user"])
        cls.chiamaka_user = account("chiamaka@brightfield.test", cls.tenant, "Chiamaka", "Nwosu")
        cls.chiamaka = cls.student(
            cls, first="Chiamaka", last="Nwosu", number="BFS/1", user=cls.chiamaka_user,
        )
        cls.kemi = cls.student(
            cls, first="Kemi", last="Nwosu", number="BFS/2", branch=cls.ikeja,
        )
        cls.link(cls, cls.chiamaka, cls.mother)
        cls.link(cls, cls.kemi, cls.mother)

        # Mr Ade: one child, at Ikeja.
        cls.father_user = account("tunde@ade.test", cls.tenant, "Tunde", "Ade")
        cls.father = cls.guardian(
            cls, name="Mr. Tunde Ade", email="tunde@ade.test", phone="08031112233",
        )
        cls.father.user = cls.father_user
        cls.father.save(update_fields=["user"])
        cls.dayo = cls.student(cls, first="Dayo", last="Ade", number="BFS/3", branch=cls.ikeja)
        cls.link(cls, cls.dayo, cls.father)

        # A parent with a child on the roll at Lekki and one who has graduated at Ikeja.
        cls.mixed_user = account("ify@eze.test", cls.tenant, "Ify", "Eze")
        cls.mixed = cls.guardian(cls, name="Mrs. Ify Eze", email="ify@eze.test", phone="08041112233")
        cls.mixed.user = cls.mixed_user
        cls.mixed.save(update_fields=["user"])
        cls.link(cls, cls.student(cls, first="Obi", last="Eze", number="BFS/4"), cls.mixed)
        cls.link(cls, cls.student(
            cls, first="Ada", last="Eze", number="BFS/5", branch=cls.ikeja,
            status=StudentStatus.GRADUATED,
        ), cls.mixed)

        # A parent whose every child has left, and one with no child linked.
        cls.leaver_user = account("uche@obi.test", cls.tenant, "Uche", "Obi")
        cls.leaver = cls.guardian(cls, name="Mr. Uche Obi", email="uche@obi.test", phone="08051112233")
        cls.leaver.user = cls.leaver_user
        cls.leaver.save(update_fields=["user"])
        cls.link(cls, cls.student(
            cls, first="Eze", last="Obi", number="BFS/6", branch=cls.ikeja,
            status=StudentStatus.WITHDRAWN,
        ), cls.leaver)
        cls.childless_user = account("zara@none.test", cls.tenant, "Zara", "None")
        cls.childless = cls.guardian(cls, name="Zara None", email="zara@none.test", phone="08061112233")
        cls.childless.user = cls.childless_user
        cls.childless.save(update_fields=["user"])

        # A teacher who is also a parent.
        cls.teacher_role = make_role(cls.school, name="Teacher", key="teacher")
        cls.teacher = account("ngozi@brightfield.test", cls.tenant, "Ngozi", "Eze")
        make_assignment(cls.school, cls.teacher, cls.teacher_role, branch=cls.lekki)
        cls.teacher_guardian = cls.guardian(
            cls, name="Mrs. Ngozi Eze", email="ngozi@brightfield.test", phone="08071112233",
        )
        cls.teacher_guardian.user = cls.teacher
        cls.teacher_guardian.save(update_fields=["user"])
        cls.link(cls, cls.student(cls, first="Nneka", last="Eze", number="BFS/7"), cls.teacher_guardian)

        # Sunrise has one branch.
        cls.solo_pupil_user = account("pupil@sunrise.test", cls.solo.tenant, "Bimpe", "Ola")
        cls.solo_pupil = cls.student(
            cls, tenant=cls.solo.tenant, branch=cls.solo_branch, first="Bimpe",
            last="Ola", number="SUN/1", user=cls.solo_pupil_user,
        )
        cls.solo_parent_user = account("parent@sunrise.test", cls.solo.tenant, "Kola", "Ola")
        cls.solo_parent = cls.guardian(
            cls, tenant=cls.solo.tenant, name="Mr. Kola Ola", email="parent@sunrise.test",
            phone="08081112233",
        )
        cls.solo_parent.user = cls.solo_parent_user
        cls.solo_parent.save(update_fields=["user"])
        cls.link(cls, cls.solo_pupil, cls.solo_parent)

    def reload(self, user):
        return User.objects.get(pk=user.pk)

    def api(self, user):
        return TenantAPIClient(user)


class PolicyAnswersTests(ClosurePolicyFixture):
    def test_a_pupil_asks_the_school_and_is_placed_at_their_branch(self):
        policy = closure_policy(self.chiamaka_user)
        self.assertEqual(
            (policy.mode, policy.branch_id, policy.kind),
            (MODE_REQUEST, self.lekki.pk, "pupil"),
        )

    def test_a_pupil_at_a_one_branch_school_is_placed_at_that_branch(self):
        policy = closure_policy(self.solo_pupil_user)
        self.assertEqual((policy.mode, policy.branch_id), (MODE_REQUEST, self.solo_branch.pk))

    def test_a_parent_closes_at_once(self):
        for user in (self.mother_user, self.father_user, self.solo_parent_user):
            with self.subTest(user=user.email):
                self.assertEqual(closure_policy(user).mode, MODE_CLOSE)
                self.assertEqual(closure_policy(user).kind, "parent")

    def test_children_at_one_branch_place_the_parent_there(self):
        self.assertEqual(closure_policy(self.father_user).branch_id, self.ikeja.pk)
        self.assertEqual(closure_policy(self.solo_parent_user).branch_id, self.solo_branch.pk)

    def test_children_at_two_branches_place_the_parent_nowhere(self):
        self.assertIsNone(closure_policy(self.mother_user).branch_id)

    def test_a_child_who_has_left_does_not_move_the_parent_to_their_branch(self):
        self.assertEqual(closure_policy(self.mixed_user).branch_id, self.lekki.pk)

    def test_a_parent_of_leavers_stays_with_the_branch_their_children_left(self):
        self.assertEqual(closure_policy(self.leaver_user).branch_id, self.ikeja.pk)

    def test_a_parent_with_no_child_linked_is_placed_nowhere_and_still_closes(self):
        policy = closure_policy(self.childless_user)
        self.assertEqual((policy.mode, policy.branch_id), (MODE_CLOSE, None))

    def test_a_teacher_who_is_also_a_parent_is_not_offered_it(self):
        self.assertIsNone(closure_policy(self.teacher))

    def test_a_member_of_staff_and_a_school_administrator_are_not_offered_it(self):
        self.assertIsNone(closure_policy(self.admin))
        self.assertIsNone(closure_policy(self.lekki_head))

    def test_a_pupil_who_holds_a_role_is_not_offered_it(self):
        role = make_role(self.school, name="Prefect", key="prefect")
        make_assignment(self.school, self.chiamaka_user, role, branch=self.lekki)
        self.assertIsNone(closure_policy(self.chiamaka_user))

    def test_the_policy_is_answered_through_the_account_engine(self):
        data = self.api(self.mother_user).get(ME).json()["data"]
        self.assertEqual(data, {"eligible": True, "mode": "close", "pending": None})
        data = self.api(self.chiamaka_user).get(ME).json()["data"]
        self.assertEqual(data["mode"], "request")
        data = self.api(self.teacher).get(ME).json()["data"]
        self.assertFalse(data["eligible"])


class ParentClosesTests(ClosurePolicyFixture):
    def test_the_account_closes_and_the_guardian_record_is_untouched(self):
        before = Guardian.all_objects.values().get(pk=self.mother.pk)
        response = self.api(self.mother_user).post(ME, {"confirm": True}, format="json")
        self.assertEqual(response.status_code, 200, response.content)

        self.assertEqual(self.reload(self.mother_user).status, User.Status.DEACTIVATED)
        after = Guardian.all_objects.values().get(pk=self.mother.pk)
        self.assertEqual(after, before)
        self.mother.refresh_from_db()
        self.assertEqual(self.mother.user_id, self.mother_user.pk)
        self.assertEqual(self.mother.phone, "08021112233")
        self.assertEqual(self.mother.student_links.count(), 2)

    def test_the_school_reads_it_in_the_branch_it_belongs_to(self):
        self.api(self.father_user).post(ME, {"confirm": True}, format="json")
        row = self.api(self.admin).get(LIST).json()["data"][0]
        self.assertEqual(
            (row["user_name"], row["kind"], row["status"], row["branch_name"]),
            ("Tunde Ade", "parent", "closed", "Ikeja"),
        )
        lekki_rows = self.api(self.lekki_head).get(LIST).json()["data"]
        self.assertEqual(lekki_rows, [])

    def test_a_parent_of_two_branches_is_read_only_by_the_whole_school(self):
        self.api(self.mother_user).post(ME, {"confirm": True}, format="json")
        self.assertEqual(len(self.api(self.admin).get(LIST).json()["data"]), 1)
        self.assertEqual(self.api(self.lekki_head).get(LIST).json()["data"], [])

    def test_at_a_one_branch_school_the_branch_administrator_reads_it(self):
        self.api(self.solo_parent_user).post(ME, {"confirm": True}, format="json")
        rows = self.api(self.solo_admin).get(LIST).json()["data"]
        self.assertEqual([row["kind"] for row in rows], ["parent"])

    def test_a_teacher_who_is_a_parent_keeps_their_working_login(self):
        response = self.api(self.teacher).post(ME, {"confirm": True}, format="json")
        self.assertEqual(response.status_code, 403)
        self.assertEqual(self.reload(self.teacher).status, User.Status.ACTIVE)


class PupilAsksTests(ClosurePolicyFixture):
    def test_the_request_goes_to_the_pupils_branch_and_the_sign_in_keeps_working(self):
        response = self.api(self.chiamaka_user).post(ME, {"confirm": True}, format="json")
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.json()["data"]["status"], "requested")
        self.assertEqual(self.reload(self.chiamaka_user).status, User.Status.ACTIVE)

        record = AccountClosureRequest.all_objects.get(user=self.chiamaka_user)
        self.assertEqual((record.branch_id, record.kind), (self.lekki.pk, "pupil"))
        self.assertEqual(len(self.api(self.lekki_head).get(LIST).json()["data"]), 1)

    def test_the_school_closes_it_and_the_pupils_record_is_kept(self):
        self.api(self.chiamaka_user).post(ME, {"confirm": True}, format="json")
        record = AccountClosureRequest.all_objects.get(user=self.chiamaka_user)
        response = self.api(self.lekki_head).post(f"{LIST}{record.pk}/close/")
        self.assertEqual(response.status_code, 200, response.content)

        self.assertEqual(self.reload(self.chiamaka_user).status, User.Status.DEACTIVATED)
        self.chiamaka.refresh_from_db()
        self.assertEqual(self.chiamaka.user_id, self.chiamaka_user.pk)
        self.assertEqual(self.chiamaka.first_name, "Chiamaka")

    def test_a_pupil_at_ikeja_is_out_of_reach_of_lekkis_administrator(self):
        ikeja_user = account("dayo@brightfield.test", self.tenant, "Dayo", "Ade")
        self.dayo.user = ikeja_user
        self.dayo.save(update_fields=["user"])
        self.api(ikeja_user).post(ME, {"confirm": True}, format="json")
        record = AccountClosureRequest.all_objects.get(user=ikeja_user)
        response = self.api(self.lekki_head).post(f"{LIST}{record.pk}/close/")
        self.assertEqual(response.status_code, 404)
        self.assertEqual(self.reload(ikeja_user).status, User.Status.ACTIVE)

    def test_sunrises_request_is_invisible_to_brightfield(self):
        self.api(self.solo_pupil_user).post(ME, {"confirm": True}, format="json")
        record = AccountClosureRequest.all_objects.get(user=self.solo_pupil_user)
        self.assertEqual(self.api(self.admin).get(LIST).json()["data"], [])
        self.assertEqual(
            self.api(self.admin).post(f"{LIST}{record.pk}/close/").status_code, 404,
        )
        self.assertEqual(len(self.api(self.solo_admin).get(LIST).json()["data"]), 1)

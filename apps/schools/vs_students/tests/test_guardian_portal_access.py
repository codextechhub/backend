"""A guardian's own sign-in account for the parent portal, and the alternate phone.

Brightfield has two branches, Lekki and Ikeja, and Sunrise has one. Mrs Adaeze
Nwosu has Chiamaka at Lekki and Kemi at Ikeja, so she is one parent with one
account whichever branch is asked. Mrs Ngozi Eze teaches at Brightfield and her
daughter attends it, so she already has a school account, which an invitation
must link to rather than duplicate. Mrs Adaeze Nwosu's address is also a parent
at Sunrise, where she is somebody else's account entirely.
"""
from __future__ import annotations

from schools.vs_students.constants import StudentStatus
from schools.vs_students.models import Guardian
from schools.vs_students.services import guardian_portal_access as access
from vs_audit.models import AuditActionType, AuditEvent
from vs_rbac.models import PermissionScope
from vs_rbac.tests.helpers import (
    install_declared_fields,
    make_assignment,
    make_permission,
    make_role,
    make_role_permission,
    make_school_admin,
    set_field_access,
)
from vs_user.models import User, UserInvitation

from .base import STUDENT_KEYS, StudentsFixture

PORTAL_KEY = "school.portal_access.issue"
ALT_PHONE_KEY = "school.guardians.alt_phone"
EMAIL_KEY = "school.guardians.email"
PORTAL_ACCESS_KEY = "school.guardians.portal_access"
MOTHER_EMAIL = "adaeze.nwosu@example.ng"
TEACHER_EMAIL = "ngozi.eze@brightfield.test"


class GuardianPortalFixture(StudentsFixture):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        key = make_permission(PORTAL_KEY, scope=PermissionScope.TENANT)
        make_role_permission(cls.role, key)
        make_role_permission(cls.solo_role, key)

        # Reads the record and nothing else: may see the access block, not change it.
        view_role = make_role(cls.school, name="Viewer", key="viewer")
        make_role_permission(view_role, cls.permissions["school.students.view"])
        cls.viewer = make_school_admin(
            None, email="viewer@brightfield.test", tenant=cls.tenant,
        )
        make_assignment(cls.school, cls.viewer, view_role, branch=None)

        # One parent, two children at two branches.
        cls.mother = cls.guardian(
            cls, name="Mrs. Adaeze Nwosu", email=MOTHER_EMAIL, phone="08021112233",
        )
        cls.chiamaka = cls.student(cls, first="Chiamaka", last="Nwosu", number="BFS/2025/0142")
        cls.kemi = cls.student(
            cls, first="Kemi", last="Nwosu", number="BFS/2025/0143", branch=cls.ikeja,
        )
        cls.link(cls, cls.chiamaka, cls.mother)
        cls.link(cls, cls.kemi, cls.mother)

        # A parent whose only child is at Ikeja.
        cls.ikeja_parent = cls.guardian(
            cls, name="Mr. Tunde Ade", email="tunde.ade@example.ng", phone="08031112233",
        )
        cls.link(cls, cls.student(
            cls, first="Dayo", last="Ade", number="BFS/2025/0200", branch=cls.ikeja,
        ), cls.ikeja_parent)

        # A teacher at the school whose own daughter attends it.
        cls.teacher_role = make_role(cls.school, name="Teacher", key="teacher")
        cls.teacher = make_school_admin(
            None, email=TEACHER_EMAIL, tenant=cls.tenant,
            first_name="Ngozi", last_name="Eze",
        )
        make_assignment(cls.school, cls.teacher, cls.teacher_role, branch=cls.lekki)
        cls.teacher_guardian = cls.guardian(
            cls, name="Mrs. Ngozi Eze", email=TEACHER_EMAIL, phone="08041112233",
        )
        cls.link(cls, cls.student(
            cls, first="Amara", last="Eze", number="BFS/2025/0210",
        ), cls.teacher_guardian)

        # Nothing to send an invitation to, and nothing to show a parent.
        cls.no_email = cls.guardian(
            cls, name="Mr. Segun Bello", email="", phone="08051112233",
        )
        cls.link(cls, cls.student(
            cls, first="Tola", last="Bello", number="BFS/2025/0220",
        ), cls.no_email)
        cls.no_child = cls.guardian(
            cls, name="Mrs. Bisi Okoro", email="bisi.okoro@example.ng", phone="08061112233",
        )
        cls.gone_child = cls.guardian(
            cls, name="Mrs. Ify Obi", email="ify.obi@example.ng", phone="08071112233",
        )
        cls.link(cls, cls.student(
            cls, first="Ola", last="Obi", number="BFS/2025/0230",
            status=StudentStatus.WITHDRAWN,
        ), cls.gone_child)

        # The same address at the other school, a different person's account.
        cls.solo_mother = cls.guardian(
            cls, tenant=cls.solo.tenant, name="Mrs. Adaeze Nwosu",
            email=MOTHER_EMAIL, phone="08099990000",
        )
        cls.link(cls, cls.student(
            cls, tenant=cls.solo.tenant, branch=cls.solo_branch, first="Uche",
            last="Nwosu", number="SUN/1",
        ), cls.solo_mother)

    def url_name(self, reissue=False):
        return "guardian-portal-access-reissue" if reissue else "guardian-portal-access"

    def issue(self, guardian, user=None):
        return self.post(user or self.admin, self.url_name(), pk=guardian.pk)

    def activate(self, guardian):
        """Stand in for the parent following their invitation link."""
        guardian.refresh_from_db()
        user = guardian.user
        user.set_password("Parent-pass-2026")
        user.status = User.Status.ACTIVE
        user.is_active = True
        user.save()
        return user


class IssueSecurityTests(GuardianPortalFixture):
    def test_without_the_key_every_route_is_refused(self):
        for method, name in (
            (self.post, self.url_name()),
            (self.delete, self.url_name()),
            (self.post, self.url_name(reissue=True)),
        ):
            with self.subTest(route=name, method=method.__name__):
                response = method(self.nobody, name, pk=self.mother.pk)
                self.assertEqual(response.status_code, 403)
        self.mother.refresh_from_db()
        self.assertIsNone(self.mother.user_id)

    def test_a_reader_may_see_where_access_stands_but_not_change_it(self):
        self.assertEqual(
            self.get(self.viewer, self.url_name(), pk=self.mother.pk).status_code, 200,
        )
        self.assertEqual(self.issue(self.mother, self.viewer).status_code, 403)

    def test_another_schools_admin_cannot_reach_the_guardian(self):
        for method, name in (
            (self.get, self.url_name()),
            (self.post, self.url_name()),
            (self.delete, self.url_name()),
            (self.post, self.url_name(reissue=True)),
        ):
            with self.subTest(route=name, method=method.__name__):
                response = method(self.solo_admin, name, pk=self.mother.pk)
                self.assertEqual(response.status_code, 404)
        self.mother.refresh_from_db()
        self.assertIsNone(self.mother.user_id)

    def test_a_branch_head_cannot_act_on_a_parent_whose_children_are_elsewhere(self):
        self.assertEqual(self.issue(self.ikeja_parent, self.lekki_head).status_code, 404)
        self.assertEqual(
            self.delete(self.lekki_head, self.url_name(), pk=self.ikeja_parent.pk).status_code,
            404,
        )
        self.ikeja_parent.refresh_from_db()
        self.assertIsNone(self.ikeja_parent.user_id)

    def test_a_branch_head_may_act_on_a_parent_with_a_child_in_reach(self):
        response = self.issue(self.mother, self.lekki_head)
        self.assertEqual(response.status_code, 201, response.data)


class IssueTests(GuardianPortalFixture):
    def test_issuing_creates_one_school_level_account_with_no_role(self):
        before = User.objects.count()
        response = self.issue(self.mother)
        self.assertEqual(response.status_code, 201, response.data)
        self.mother.refresh_from_db()
        user = self.mother.user
        self.assertEqual(User.objects.count(), before + 1)
        self.assertEqual(user.email, MOTHER_EMAIL)
        self.assertEqual(user.tenant_id, self.tenant.pk)
        self.assertIsNone(user.branch_id)
        self.assertEqual(user.status, User.Status.PENDING)
        self.assertFalse(user.is_active)
        self.assertFalse(user.tenant_role_assignments.exists())
        self.assertFalse(user.is_staff or user.is_superuser)
        self.assertTrue(UserInvitation.objects.filter(user=user, is_used=False).exists())
        self.assertEqual(response.data["data"]["status"], "invited")
        self.assertEqual(response.data["data"]["sign_in_identifiers"]["email"], MOTHER_EMAIL)

    def test_the_account_is_named_after_the_guardian(self):
        access.issue(self.mother, actor=self.admin)
        self.mother.refresh_from_db()
        self.assertEqual(self.mother.user.first_name, self.mother.first_name)
        self.assertEqual(self.mother.user.last_name, self.mother.last_name)
        self.assertTrue(self.mother.user.first_name)

    def test_the_invitation_goes_to_the_guardians_own_address_only(self):
        from vs_user.tasks import _recipients_for

        access.issue(self.mother, actor=self.admin)
        self.mother.refresh_from_db()
        registered, extras = _recipients_for(self.mother.user)
        self.assertEqual([u.email for u in registered], [MOTHER_EMAIL])
        self.assertEqual(extras, [])

    def test_a_parent_with_children_at_two_branches_has_one_account(self):
        access.issue(self.mother, actor=self.admin)
        self.assertEqual(User.objects.filter(tenant=self.tenant, email=MOTHER_EMAIL).count(), 1)
        self.assertEqual(self.issue(self.mother).status_code, 409)
        self.assertEqual(User.objects.filter(tenant=self.tenant, email=MOTHER_EMAIL).count(), 1)
        self.mother.refresh_from_db()
        self.assertIsNone(self.mother.user.branch_id)

    def test_the_same_address_at_two_schools_is_two_independent_accounts(self):
        access.issue(self.mother, actor=self.admin)
        access.issue(self.solo_mother, actor=self.solo_admin)
        self.mother.refresh_from_db()
        self.solo_mother.refresh_from_db()
        self.assertNotEqual(self.mother.user_id, self.solo_mother.user_id)
        self.assertEqual(self.mother.user.tenant_id, self.tenant.pk)
        self.assertEqual(self.solo_mother.user.tenant_id, self.solo.tenant.pk)
        self.assertIsNone(self.solo_mother.user.branch_id)
        self.assertEqual(User.objects.filter(email=MOTHER_EMAIL).count(), 2)

    def test_a_guardian_without_an_email_is_refused(self):
        before = User.objects.count()
        response = self.issue(self.no_email)
        self.assertEqual(response.status_code, 422)
        self.assertEqual(response.data["error"]["code"], "PORTAL_GUARDIAN_NO_EMAIL")
        self.assertEqual(User.objects.count(), before)

    def test_a_guardian_with_no_child_on_the_roll_is_refused(self):
        before = User.objects.count()
        for guardian in (self.no_child, self.gone_child):
            with self.subTest(guardian=guardian.full_name):
                response = self.issue(guardian)
                self.assertEqual(response.status_code, 422)
                self.assertEqual(
                    response.data["error"]["code"], "PORTAL_GUARDIAN_NO_CHILD_ON_ROLL",
                )
        self.assertEqual(User.objects.count(), before)

    def test_the_email_another_guardian_account_holds_is_refused(self):
        other = User.objects.create_user(
            "shared@example.ng", tenant=self.tenant, first_name="Some", last_name="One",
            status=User.Status.ACTIVE, is_active=True,
        )
        Guardian.all_objects.filter(pk=self.no_child.pk).update(email="shared@example.ng")
        Guardian.all_objects.filter(pk=self.gone_child.pk).update(user=other)
        self.link(self.student(first="Kayode", last="Okoro", number="BFS/2025/0240"),
                  self.no_child)
        response = self.issue(self.no_child)
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.data["error"]["code"], "PORTAL_EMAIL_IN_USE")
        self.assertNotIn("shared@example.ng", response.data["message"])

    def test_a_pupils_own_sign_in_is_never_linked_as_a_parent(self):
        pupil = User.objects.create_user(
            "chiamaka@example.ng", tenant=self.tenant, branch=self.lekki,
            first_name="Chiamaka", last_name="Nwosu",
            status=User.Status.ACTIVE, is_active=True,
        )
        type(self.chiamaka).all_objects.filter(pk=self.chiamaka.pk).update(user=pupil)
        Guardian.all_objects.filter(pk=self.mother.pk).update(email="chiamaka@example.ng")
        response = self.issue(self.mother)
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.data["error"]["code"], "PORTAL_ACCOUNT_NOT_USABLE")
        self.assertIsNone(Guardian.all_objects.get(pk=self.mother.pk).user_id)

    def test_a_closed_staff_account_is_not_reopened_as_a_parent(self):
        User.objects.filter(pk=self.teacher.pk).update(status=User.Status.DEACTIVATED, is_active=False)
        response = self.issue(self.teacher_guardian)
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.data["error"]["code"], "PORTAL_ACCOUNT_NOT_USABLE")
        self.teacher.refresh_from_db()
        self.assertEqual(self.teacher.status, User.Status.DEACTIVATED)

    def test_the_audit_names_the_guardian_and_never_the_contact_details(self):
        access.issue(self.mother, actor=self.admin)
        event = AuditEvent.objects.filter(
            action_type=AuditActionType.STUDENT_PORTAL_ACCESS_ISSUED,
            entity_type="Guardian", entity_id=str(self.mother.pk),
        ).get()
        self.assertEqual(event.entity_label, self.mother.full_name)
        self.assertNotIn(MOTHER_EMAIL, event.summary)
        self.assertNotIn(self.mother.phone, event.summary)


class TeacherWhoIsAlsoAParentTests(GuardianPortalFixture):
    def test_her_existing_account_is_linked_not_duplicated(self):
        before = User.objects.filter(tenant=self.tenant).count()
        response = self.issue(self.teacher_guardian)
        self.assertEqual(response.status_code, 201, response.data)
        self.teacher_guardian.refresh_from_db()
        self.assertEqual(self.teacher_guardian.user_id, self.teacher.pk)
        self.assertEqual(User.objects.filter(tenant=self.tenant).count(), before)
        self.assertEqual(response.data["data"]["status"], "active")

    def test_her_staff_account_is_unchanged_and_no_invitation_is_sent(self):
        password_hash = self.teacher.password
        access.issue(self.teacher_guardian, actor=self.admin)
        self.teacher.refresh_from_db()
        self.assertEqual(self.teacher.status, User.Status.ACTIVE)
        self.assertTrue(self.teacher.is_active)
        self.assertEqual(self.teacher.password, password_hash)
        self.assertEqual(self.teacher.tenant_role_assignments.count(), 1)
        self.assertFalse(UserInvitation.objects.filter(user=self.teacher).exists())

    def test_asking_again_answers_active_and_creates_nothing(self):
        access.issue(self.teacher_guardian, actor=self.admin)
        before = User.objects.count()
        response = self.issue(self.teacher_guardian)
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["data"]["status"], "active")
        self.assertEqual(User.objects.count(), before)

    def test_revoking_unlinks_her_and_leaves_her_staff_sign_in_working(self):
        access.issue(self.teacher_guardian, actor=self.admin)
        response = self.delete(self.admin, self.url_name(), pk=self.teacher_guardian.pk)
        self.assertEqual(response.status_code, 200, response.data)
        self.assertIn("also used for something else", response.data["message"])
        self.assertEqual(response.data["data"]["status"], "none")
        self.teacher_guardian.refresh_from_db()
        self.assertIsNone(self.teacher_guardian.user_id)
        self.teacher.refresh_from_db()
        self.assertEqual(self.teacher.status, User.Status.ACTIVE)
        self.assertTrue(self.teacher.is_active)
        self.assertEqual(self.teacher.tenant_role_assignments.count(), 1)

    def test_a_guardian_already_linked_to_her_account_reads_active(self):
        Guardian.all_objects.filter(pk=self.teacher_guardian.pk).update(user=self.teacher)
        self.teacher_guardian.refresh_from_db()
        self.assertEqual(access.state_of(self.teacher_guardian), "active")


class RevokeAndReissueTests(GuardianPortalFixture):
    def test_revoking_a_parent_only_account_closes_it_and_keeps_the_link(self):
        access.issue(self.mother, actor=self.admin)
        user = self.activate(self.mother)
        response = self.delete(self.admin, self.url_name(), pk=self.mother.pk)
        self.assertEqual(response.status_code, 200, response.data)
        self.assertIn("has been closed", response.data["message"])
        self.assertEqual(response.data["data"]["status"], "revoked")
        user.refresh_from_db()
        self.assertEqual(user.status, User.Status.DEACTIVATED)
        self.assertFalse(user.is_active)
        self.mother.refresh_from_db()
        self.assertEqual(self.mother.user_id, user.pk)

    def test_revoking_with_nothing_to_take_away_is_refused(self):
        response = self.delete(self.admin, self.url_name(), pk=self.mother.pk)
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.data["error"]["code"], "PORTAL_ACCESS_NOT_GIVEN")

    def test_a_returning_parent_gets_the_same_account_back(self):
        access.issue(self.mother, actor=self.admin)
        user = self.activate(self.mother)
        access.revoke(self.mother, actor=self.admin)
        before = User.objects.count()
        response = self.issue(self.mother)
        self.assertEqual(response.status_code, 201, response.data)
        self.mother.refresh_from_db()
        self.assertEqual(self.mother.user_id, user.pk)
        self.assertEqual(self.mother.user.status, User.Status.PENDING)
        self.assertEqual(User.objects.count(), before)
        self.assertTrue(UserInvitation.objects.filter(user=user, is_used=False).exists())

    def test_revoking_is_audited_without_the_contact_details(self):
        access.issue(self.mother, actor=self.admin)
        access.revoke(self.mother, actor=self.admin)
        event = AuditEvent.objects.filter(
            action_type=AuditActionType.STUDENT_PORTAL_ACCESS_REVOKED,
            entity_type="Guardian", entity_id=str(self.mother.pk),
        ).get()
        self.assertNotIn(MOTHER_EMAIL, event.summary)

    def test_reissuing_rotates_the_link_for_an_invited_parent(self):
        access.issue(self.mother, actor=self.admin)
        self.mother.refresh_from_db()
        old = UserInvitation.objects.get(user=self.mother.user).token_hash
        response = self.post(self.admin, self.url_name(reissue=True), pk=self.mother.pk)
        self.assertEqual(response.status_code, 200, response.data)
        self.assertNotEqual(UserInvitation.objects.get(user=self.mother.user).token_hash, old)
        self.assertTrue(AuditEvent.objects.filter(
            action_type=AuditActionType.STUDENT_PORTAL_INVITE_RESENT,
            entity_type="Guardian", entity_id=str(self.mother.pk),
        ).exists())

    def test_reissuing_needs_an_invitation_that_is_waiting(self):
        none_yet = self.post(self.admin, self.url_name(reissue=True), pk=self.mother.pk)
        self.assertEqual(none_yet.status_code, 409)
        self.assertEqual(none_yet.data["error"]["code"], "PORTAL_INVITE_NOT_PENDING")
        access.issue(self.mother, actor=self.admin)
        self.activate(self.mother)
        activated = self.post(self.admin, self.url_name(reissue=True), pk=self.mother.pk)
        self.assertEqual(activated.status_code, 409)


class PortalAccessFieldTests(GuardianPortalFixture):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        install_declared_fields("school.guardians")
        role = make_role(cls.school, name="No portal state", key="no_portal_state")
        for key in STUDENT_KEYS + (PORTAL_KEY,):
            perm = cls.permissions.get(key) or make_permission(key, scope=PermissionScope.TENANT)
            make_role_permission(role, perm)
        set_field_access(role, PORTAL_ACCESS_KEY, read=False, write=False)
        cls.hidden = make_school_admin(None, email="hidden@brightfield.test", tenant=cls.tenant)
        make_assignment(cls.school, cls.hidden, role, branch=None)

        no_email = make_role(cls.school, name="No guardian email", key="no_guardian_email")
        for key in STUDENT_KEYS:
            make_role_permission(no_email, cls.permissions[key])
        set_field_access(no_email, EMAIL_KEY, read=False, write=False)
        cls.no_email_reader = make_school_admin(
            None, email="noemail@brightfield.test", tenant=cls.tenant,
        )
        make_assignment(cls.school, cls.no_email_reader, no_email, branch=None)

    def test_the_record_carries_the_state_for_a_caller_who_may_read_it(self):
        self.assertEqual(
            self.get(self.admin, "guardian-detail", pk=self.mother.pk).data["data"]["portal_access"],
            "none",
        )
        access.issue(self.mother, actor=self.admin)
        self.assertEqual(
            self.get(self.admin, "guardian-detail", pk=self.mother.pk).data["data"]["portal_access"],
            "invited",
        )

    def test_a_caller_who_may_not_read_it_gets_no_state_and_no_block(self):
        access.issue(self.mother, actor=self.admin)
        record = self.get(self.hidden, "guardian-detail", pk=self.mother.pk).data["data"]
        self.assertNotIn("portal_access", record)
        block = self.get(self.hidden, self.url_name(), pk=self.mother.pk)
        self.assertEqual(block.status_code, 200)
        self.assertEqual(block.data["data"], {})

    def test_the_email_in_the_block_follows_the_email_switch(self):
        access.issue(self.mother, actor=self.admin)
        shown = self.get(self.admin, self.url_name(), pk=self.mother.pk).data["data"]
        self.assertEqual(shown["sign_in_identifiers"]["email"], MOTHER_EMAIL)
        held_back = self.get(
            self.no_email_reader, self.url_name(), pk=self.mother.pk,
        ).data["data"]
        self.assertEqual(held_back["status"], "invited")
        self.assertNotIn("email", held_back["sign_in_identifiers"])
        self.assertNotIn(MOTHER_EMAIL, str(held_back))

    def test_the_state_cannot_be_written_through_the_record(self):
        response = self.patch(
            self.admin, "guardian-detail", {"portal_access": "active"}, pk=self.mother.pk,
        )
        self.mother.refresh_from_db()
        self.assertIsNone(self.mother.user_id)
        self.assertEqual(response.status_code, 403, response.data)

    def test_the_student_guardian_list_carries_the_state_beside_each_guardian(self):
        access.issue(self.mother, actor=self.admin)
        rows = self.get(self.admin, "student-guardians", pk=self.chiamaka.pk).data["data"]
        self.assertEqual(rows[0]["guardian"]["portal_access"], "invited")


class AltPhoneTests(GuardianPortalFixture):
    """The alternate phone is an ordinary guardian detail under its own switches."""

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        install_declared_fields("school.guardians")
        cls.callers = {}
        for mode, read, write in (
            ("hidden", False, False), ("read_only", True, False), ("writable", True, True),
        ):
            role = make_role(cls.school, name=f"Alt {mode}", key=f"alt_{mode}")
            for key in STUDENT_KEYS:
                make_role_permission(role, cls.permissions[key])
            set_field_access(role, ALT_PHONE_KEY, read=read, write=write)
            user = make_school_admin(
                None, email=f"alt.{mode}@brightfield.test", tenant=cls.tenant,
            )
            make_assignment(cls.school, user, role, branch=None)
            cls.callers[mode] = user
        Guardian.all_objects.filter(pk=cls.mother.pk).update(alt_phone="08123456789")

    def link_body(self, **extra):
        return {
            "full_name": "Mr. Emeka Obi", "phone": "08077770000",
            "relationship": "FATHER", **extra,
        }

    def test_a_caller_who_may_read_it_sees_it_on_every_guardian_payload(self):
        for mode in ("read_only", "writable"):
            with self.subTest(caller=mode):
                user = self.callers[mode]
                detail = self.get(user, "guardian-detail", pk=self.mother.pk).data["data"]
                self.assertEqual(detail["alt_phone"], "08123456789")
                directory = self.get(user, "guardian-list", {"search": "Adaeze"}).data["data"]
                rows = directory["results"] if isinstance(directory, dict) else directory
                self.assertEqual(rows[0]["alt_phone"], "08123456789")
                nested = self.get(user, "student-guardians", pk=self.chiamaka.pk).data["data"]
                self.assertEqual(nested[0]["guardian"]["alt_phone"], "08123456789")

    def test_a_caller_who_may_not_read_it_never_meets_it(self):
        user = self.callers["hidden"]
        detail = self.get(user, "guardian-detail", pk=self.mother.pk).data["data"]
        self.assertNotIn("alt_phone", detail)
        directory = self.get(user, "guardian-list", {"search": "Adaeze"}).data["data"]
        rows = directory["results"] if isinstance(directory, dict) else directory
        self.assertNotIn("alt_phone", rows[0])
        nested = self.get(user, "student-guardians", pk=self.chiamaka.pk).data["data"]
        self.assertNotIn("alt_phone", nested[0]["guardian"])
        self.assertNotIn("08123456789", str(nested))

    def test_a_read_only_caller_cannot_change_it(self):
        for name, response in (
            ("edit", self.patch(
                self.callers["read_only"], "guardian-detail",
                {"alt_phone": "08999999999"}, pk=self.mother.pk,
            )),
            ("link", self.post(
                self.callers["read_only"], "student-guardians",
                self.link_body(alt_phone="08999999999"), pk=self.chiamaka.pk,
            )),
        ):
            with self.subTest(route=name):
                self.assertEqual(response.status_code, 403, response.data)
        self.mother.refresh_from_db()
        self.assertEqual(self.mother.alt_phone, "08123456789")
        self.assertFalse(Guardian.objects.filter(full_name="Mr. Emeka Obi").exists())

    def test_a_form_that_echoes_the_unchanged_value_is_accepted(self):
        response = self.patch(
            self.callers["read_only"], "guardian-detail",
            {"alt_phone": "08123456789", "occupation": "Trader"}, pk=self.mother.pk,
        )
        self.assertEqual(response.status_code, 200, response.data)

    def test_a_caller_who_may_not_see_it_cannot_set_it(self):
        response = self.patch(
            self.callers["hidden"], "guardian-detail",
            {"alt_phone": "08999999999"}, pk=self.mother.pk,
        )
        self.assertEqual(response.status_code, 403, response.data)
        self.mother.refresh_from_db()
        self.assertEqual(self.mother.alt_phone, "08123456789")

    def test_a_writable_caller_corrects_it(self):
        response = self.patch(
            self.callers["writable"], "guardian-detail",
            {"alt_phone": "08999999999"}, pk=self.mother.pk,
        )
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["data"]["alt_phone"], "08999999999")
        self.mother.refresh_from_db()
        self.assertEqual(self.mother.alt_phone, "08999999999")

    def test_it_can_be_cleared(self):
        response = self.patch(
            self.callers["writable"], "guardian-detail", {"alt_phone": ""}, pk=self.mother.pk,
        )
        self.assertEqual(response.status_code, 200, response.data)
        self.mother.refresh_from_db()
        self.assertEqual(self.mother.alt_phone, "")

    def test_it_is_held_to_the_length_of_a_phone_number(self):
        response = self.patch(
            self.callers["writable"], "guardian-detail",
            {"alt_phone": "0" * 33}, pk=self.mother.pk,
        )
        self.assertEqual(response.status_code, 400, response.data)

    def test_a_new_guardian_on_the_link_form_keeps_it(self):
        response = self.post(
            self.callers["writable"], "student-guardians",
            self.link_body(alt_phone="08066660000"), pk=self.kemi.pk,
        )
        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(response.data["data"]["alt_phone"], "08066660000")
        self.assertEqual(
            Guardian.objects.get(full_name="Mr. Emeka Obi").alt_phone, "08066660000",
        )

    def test_enrolling_with_it_keeps_it(self):
        body = self.enrolment_body()
        body["guardians"][0]["alt_phone"] = "08155550000"
        response = self.post(self.admin, "student-list", body)
        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(
            Guardian.objects.get(phone="08115550177").alt_phone, "08155550000",
        )

    def test_it_is_not_how_two_records_are_matched(self):
        """Only the first phone joins a new row to a guardian already held."""
        body = self.link_body(alt_phone="08123456789", phone="08000000001")
        response = self.post(self.callers["writable"], "student-guardians", body, pk=self.kemi.pk)
        self.assertEqual(response.status_code, 201, response.data)
        self.assertNotEqual(response.data["data"]["id"], self.mother.pk)

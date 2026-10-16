"""A pupil's own sign-in account: giving it, signing in with it, taking it away.

Brightfield has two branches, Lekki and Ikeja, and Sunrise has one. Chiamaka
Nwosu (BFS/2025/0142) is at Lekki and has an email of her own; Tunde Bakare
(BFS/2025/0143) is at Lekki and has none, so his invitation reaches only his
mother and he signs in with his admission number. The Lekki head reaches Lekki
pupils and nobody at Ikeja.
"""
from __future__ import annotations

from django.core.cache import cache
from rest_framework.test import APIClient

from schools.vs_students.constants import Relationship, StudentStatus
from schools.vs_students.services import portal_access
from schools.vs_students.services.status import transition
from vs_rbac.models import PermissionScope
from vs_rbac.tests.helpers import make_permission, make_role_permission
from vs_user.models import LoginSession, User, UserInvitation

from .base import StudentsFixture

PASSWORD = "Pupil-pass-2026"
LOGIN_URL = "/v1/user/auth/login/"
RESET_URL = "/v1/user/auth/password/reset/request/"
PORTAL_KEY = "school.portal_access.issue"


class PortalFixture(StudentsFixture):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        key = make_permission(PORTAL_KEY, scope=PermissionScope.TENANT)
        make_role_permission(cls.role, key)
        make_role_permission(cls.solo_role, key)

        cls.mother = cls.guardian(
            cls, name="Mrs. Adaeze Nwosu", email="adaeze.nwosu@example.ng",
            phone="08021112233",
        )
        cls.chiamaka = cls.student(
            cls, number="BFS/2025/0142", email="chiamaka.n@example.ng",
        )
        cls.link(cls, cls.chiamaka, cls.mother, relationship=Relationship.MOTHER)
        cls.place(cls, cls.chiamaka)

        cls.tunde_mother = cls.guardian(
            cls, name="Mrs. Bisi Bakare", email="bisi.bakare@example.ng",
            phone="08031112233",
        )
        cls.tunde = cls.student(cls, first="Tunde", last="Bakare", number="BFS/2025/0143")
        cls.link(cls, cls.tunde, cls.tunde_mother, relationship=Relationship.MOTHER)

        cls.ikeja_pupil = cls.student(
            cls, first="Kemi", last="Ade", number="BFS/2025/0200", branch=cls.ikeja,
        )
        cls.link(cls, cls.ikeja_pupil, cls.guardian(cls, name="Mr. Ade", email="ade@example.ng"))

    def activate(self, student):
        """Stand in for the pupil following their invitation link."""
        student.refresh_from_db()
        user = student.user
        user.set_password(PASSWORD)
        user.status = User.Status.ACTIVE
        user.is_active = True
        user.save()
        return user

    def sign_in(self, identifier, *, tenant="brightfield", password=PASSWORD):
        cache.clear()
        return APIClient().post(
            LOGIN_URL, {"identifier": identifier, "password": password, "tenant": tenant},
            format="json",
        )


class IssueTests(PortalFixture):
    def test_without_the_key_access_is_refused(self):
        response = self.post(self.nobody, "student-portal-access", pk=self.chiamaka.pk)
        self.assertEqual(response.status_code, 403)
        self.chiamaka.refresh_from_db()
        self.assertIsNone(self.chiamaka.user_id)

    def test_a_branch_head_cannot_reach_another_branchs_pupil(self):
        response = self.post(self.lekki_head, "student-portal-access", pk=self.ikeja_pupil.pk)
        self.assertEqual(response.status_code, 404)

    def test_another_schools_admin_cannot_reach_the_pupil(self):
        response = self.post(self.solo_admin, "student-portal-access", pk=self.chiamaka.pk)
        self.assertEqual(response.status_code, 404)

    def test_issuing_creates_an_account_with_no_role_and_an_invitation(self):
        response = self.post(self.admin, "student-portal-access", pk=self.chiamaka.pk)
        self.assertEqual(response.status_code, 201, response.data)
        self.chiamaka.refresh_from_db()
        user = self.chiamaka.user
        self.assertEqual(user.email, "chiamaka.n@example.ng")
        self.assertEqual(user.tenant_id, self.tenant.pk)
        self.assertEqual(user.branch_id, self.lekki.pk)
        self.assertEqual(user.status, User.Status.PENDING)
        self.assertFalse(user.tenant_role_assignments.exists())
        self.assertTrue(UserInvitation.objects.filter(user=user, is_used=False).exists())
        self.assertEqual(response.data["data"]["status"], "invited")

    def test_a_pupil_without_email_gets_an_account_with_no_address(self):
        self.post(self.admin, "student-portal-access", pk=self.tunde.pk)
        self.tunde.refresh_from_db()
        self.assertIsNone(self.tunde.user.email)

    def test_the_invitation_reaches_the_pupil_and_their_primary_guardian(self):
        from vs_user.tasks import _recipients_for

        portal_access.issue(self.chiamaka, actor=self.admin)
        self.chiamaka.refresh_from_db()
        registered, extras = _recipients_for(self.chiamaka.user)
        self.assertEqual(registered, [self.chiamaka.user])
        self.assertEqual([a.email for a in extras], ["adaeze.nwosu@example.ng"])

    def test_a_pupil_without_email_is_invited_through_their_guardian_only(self):
        from vs_user.tasks import _recipients_for

        portal_access.issue(self.tunde, actor=self.admin)
        self.tunde.refresh_from_db()
        registered, extras = _recipients_for(self.tunde.user)
        self.assertEqual(registered, [])
        self.assertEqual([a.email for a in extras], ["bisi.bakare@example.ng"])

    def test_no_admission_number_and_no_email_is_refused(self):
        pupil = self.student(first="Ngozi", last="Eze")
        self.link(pupil, self.guardian(name="Mr. Eze", email="eze@example.ng"))
        response = self.post(self.admin, "student-portal-access", pk=pupil.pk)
        self.assertEqual(response.status_code, 422)
        self.assertEqual(response.data["error"]["code"], "PORTAL_NO_WAY_TO_SIGN_IN")

    def test_nowhere_to_send_the_invitation_is_refused(self):
        pupil = self.student(first="Ngozi", last="Eze", number="BFS/2025/0300")
        response = self.post(self.admin, "student-portal-access", pk=pupil.pk)
        self.assertEqual(response.status_code, 422)
        self.assertEqual(response.data["error"]["code"], "PORTAL_NO_DELIVERY_ADDRESS")

    def test_an_email_another_account_uses_is_refused(self):
        pupil = self.student(
            first="Ada", last="Obi", number="BFS/2025/0301", email="adaeze@brightfield.test",
        )
        response = self.post(self.admin, "student-portal-access", pk=pupil.pk)
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.data["error"]["code"], "PORTAL_EMAIL_IN_USE")
        self.assertNotIn("adaeze@brightfield.test", response.data["message"])

    def test_issuing_twice_is_refused(self):
        portal_access.issue(self.chiamaka, actor=self.admin)
        response = self.post(self.admin, "student-portal-access", pk=self.chiamaka.pk)
        self.assertEqual(response.status_code, 409)

    def test_a_pupil_off_the_roll_cannot_be_given_access(self):
        pupil = self.student(
            first="Ola", last="Ade", number="BFS/2025/0302", status=StudentStatus.WITHDRAWN,
        )
        response = self.post(self.admin, "student-portal-access", pk=pupil.pk)
        self.assertEqual(response.status_code, 422)

    def test_a_role_cannot_be_granted_to_an_account_without_email(self):
        from django.core.exceptions import ValidationError
        from vs_rbac.tests.helpers import make_assignment

        portal_access.issue(self.tunde, actor=self.admin)
        self.tunde.refresh_from_db()
        with self.assertRaises(ValidationError):
            make_assignment(self.school, self.tunde.user, self.role, branch=None)

    def test_reading_access_needs_only_the_view_key(self):
        portal_access.issue(self.chiamaka, actor=self.admin)
        response = self.get(self.admin, "student-portal-access", pk=self.chiamaka.pk)
        self.assertEqual(response.status_code, 200)
        block = response.data["data"]
        self.assertEqual(block["status"], "invited")
        self.assertEqual(block["sign_in_identifiers"]["admission_number"], "BFS/2025/0142")

    def test_the_profile_says_where_access_stands(self):
        portal_access.issue(self.chiamaka, actor=self.admin)
        response = self.get(self.admin, "student-detail", pk=self.chiamaka.pk)
        self.assertEqual(response.data["data"]["portal_access"], "invited")


class SignInTests(PortalFixture):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        portal_access.issue(cls.chiamaka, actor=cls.admin)
        portal_access.issue(cls.tunde, actor=cls.admin)

    def setUp(self):
        self.activate(self.chiamaka)
        self.activate(self.tunde)

    def test_an_admission_number_signs_the_pupil_in(self):
        response = self.sign_in("BFS/2025/0142")
        self.assertEqual(response.status_code, 200, response.data)

    def test_the_admission_number_ignores_case(self):
        self.assertEqual(self.sign_in("bfs/2025/0142").status_code, 200)

    def test_a_pupil_with_email_may_also_use_it(self):
        self.assertEqual(self.sign_in("chiamaka.n@example.ng").status_code, 200)

    def test_a_pupil_with_no_email_signs_in_by_admission_number(self):
        self.assertEqual(self.sign_in("BFS/2025/0143").status_code, 200)

    def test_another_school_does_not_know_the_number(self):
        self.assertNotEqual(self.sign_in("BFS/2025/0142", tenant="sunrise").status_code, 200)

    def test_a_number_without_an_account_answers_like_a_wrong_password(self):
        unknown = self.sign_in("BFS/2025/0200")
        wrong = self.sign_in("BFS/2025/0142", password="not-the-password")
        self.assertEqual(unknown.status_code, wrong.status_code)
        self.assertEqual(unknown.data.get("error", {}).get("code"), wrong.data.get("error", {}).get("code"))

    def test_a_staff_number_equal_to_an_admission_number_signs_nobody_in(self):
        from schools.vs_staff.models import StaffProfile

        import datetime as dt

        StaffProfile.all_objects.create(
            tenant=self.tenant, user=self.admin, branch=self.lekki,
            job_title="Bursar", staff_number="BFS/2025/0142",
            hire_date=dt.date(2021, 9, 6),
        )
        self.assertNotEqual(self.sign_in("BFS/2025/0142").status_code, 200)

    def test_a_revoked_pupil_cannot_sign_in_and_their_sessions_end(self):
        self.assertEqual(self.sign_in("BFS/2025/0142").status_code, 200)
        self.delete(self.admin, "student-portal-access", pk=self.chiamaka.pk)
        self.assertNotEqual(self.sign_in("BFS/2025/0142").status_code, 200)
        self.assertFalse(
            LoginSession.all_objects.filter(user=self.chiamaka.user, is_active=True).exists()
        )

    def test_leaving_the_roll_closes_the_account(self):
        transition(
            self.chiamaka, StudentStatus.WITHDRAWN, actor=self.admin, reason="Family moved",
        )
        self.chiamaka.user.refresh_from_db()
        self.assertEqual(self.chiamaka.user.status, User.Status.DEACTIVATED)
        self.assertNotEqual(self.sign_in("BFS/2025/0142").status_code, 200)

    def test_a_suspension_leaves_the_account_alone(self):
        transition(
            self.chiamaka, StudentStatus.SUSPENDED, actor=self.admin, reason="Fighting",
        )
        self.chiamaka.user.refresh_from_db()
        self.assertEqual(self.chiamaka.user.status, User.Status.ACTIVE)

    def test_a_returning_pupil_gets_the_same_account_back(self):
        user_id = self.chiamaka.user_id
        portal_access.revoke(self.chiamaka, actor=self.admin)
        portal_access.issue(self.chiamaka, actor=self.admin)
        self.chiamaka.refresh_from_db()
        self.assertEqual(self.chiamaka.user_id, user_id)
        self.assertEqual(self.chiamaka.user.status, User.Status.PENDING)


class ResetTests(PortalFixture):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        portal_access.issue(cls.tunde, actor=cls.admin)

    def setUp(self):
        self.activate(self.tunde)

    def test_a_reset_by_admission_number_is_issued_for_the_pupil(self):
        from vs_user.models import PasswordResetRequest

        cache.clear()
        response = APIClient().post(
            RESET_URL, {"identifier": "BFS/2025/0143", "tenant": "brightfield"}, format="json",
        )
        self.assertEqual(response.status_code, 200)
        self.assertTrue(PasswordResetRequest.objects.filter(user=self.tunde.user).exists())

    def test_an_unknown_number_answers_the_same(self):
        cache.clear()
        response = APIClient().post(
            RESET_URL, {"identifier": "BFS/2025/9999", "tenant": "brightfield"}, format="json",
        )
        self.assertEqual(response.status_code, 200)


class BulkTests(PortalFixture):
    def test_a_class_is_given_access_and_refusals_do_not_stop_the_rest(self):
        self.place(self.tunde)
        response = self.post(
            self.admin, "student-portal-access-bulk", {"school_class": self.shared_class.pk},
        )
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["data"]["issued"], 2)

    def test_a_pupil_who_already_has_access_is_reported_not_reinvited(self):
        portal_access.issue(self.chiamaka, actor=self.admin)
        response = self.post(
            self.admin, "student-portal-access-bulk",
            {"student_ids": [self.chiamaka.pk, self.tunde.pk]},
        )
        rows = {r["student"]: r for r in response.data["data"]["results"]}
        self.assertTrue(rows[self.chiamaka.pk]["already"])
        self.assertEqual(response.data["data"]["issued"], 1)

    def test_bulk_without_the_key_is_refused(self):
        response = self.post(
            self.nobody, "student-portal-access-bulk", {"student_ids": [self.tunde.pk]},
        )
        self.assertEqual(response.status_code, 403)


class PortalRulesTests(PortalFixture):
    """Whether students may change their own photograph, set by the school."""

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        settings_key = make_permission("school.settings.update", scope=PermissionScope.TENANT)
        make_role_permission(cls.role, settings_key)

    def test_reading_needs_only_the_student_view_key(self):
        response = self.get(self.admin, "student-portal-rules")
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.data["data"]["photo_self_edit"])
        self.assertEqual(self.get(self.nobody, "student-portal-rules").status_code, 403)

    def test_a_whole_school_admin_turns_photo_self_edit_off(self):
        response = self.put(self.admin, "student-portal-rules", {"photo_self_edit": False})
        self.assertEqual(response.status_code, 200, response.data)
        self.assertFalse(self.get(self.admin, "student-portal-rules").data["data"]["photo_self_edit"])

    def test_a_branch_bound_admin_cannot_change_it(self):
        response = self.put(self.lekki_head, "student-portal-rules", {"photo_self_edit": False})
        self.assertEqual(response.status_code, 403)
        self.assertTrue(self.get(self.admin, "student-portal-rules").data["data"]["photo_self_edit"])

    def test_one_schools_choice_never_reaches_another(self):
        self.put(self.admin, "student-portal-rules", {"photo_self_edit": False})
        theirs = self.get(self.solo_admin, "student-portal-rules").data["data"]
        self.assertTrue(theirs["photo_self_edit"])

"""Signing in with a staff ID, and correcting a staff member's sign-in email.

A staff ID is the school's own label, unique inside one school and nowhere
else, so it signs somebody in only at the school the page names. Brightfield
numbers Chukwuemeka Eze BFS/STF/0012; Sunrise has never heard of that number.

Correcting an email on an account that has not been activated reissues the
invitation, so a link sent to a mistyped address stops working.
"""
from __future__ import annotations

from django.core.cache import cache
from rest_framework.test import APIClient

from vs_rbac.tests.helpers import (
    install_declared_fields,
    make_assignment,
    make_role,
    make_role_permission,
    make_school_admin,
    set_field_access,
)
from vs_user.models import AuthAttempt, User, UserInvitation
from vs_user.services.invitation import InvitationService

from ..models import StaffProfile
from .base import ALL_KEYS, StaffFixture

PASSWORD = "testpass123"
LOGIN_URL = "/v1/user/auth/login/"


class StaffIdSignInTests(StaffFixture):
    def sign_in(self, identifier, *, tenant="brightfield", password=PASSWORD):
        # The login throttle counts per client; each attempt here is its own.
        cache.clear()
        body = {"identifier": identifier, "password": password}
        if tenant is not None:
            body["tenant"] = tenant
        return APIClient().post(LOGIN_URL, body, format="json")

    def test_a_staff_id_signs_its_owner_in_at_their_own_school(self):
        response = self.sign_in("BFS/STF/0012")
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["data"]["user"]["email"], self.eze.user.email)

    def test_the_staff_id_match_ignores_case_and_surrounding_space(self):
        response = self.sign_in("  bfs/stf/0012 ")
        self.assertEqual(response.status_code, 200, response.data)

    def test_the_same_box_still_takes_an_email_address(self):
        response = self.sign_in(self.eze.user.email)
        self.assertEqual(response.status_code, 200, response.data)

    def test_a_staff_id_is_refused_at_a_school_that_does_not_hold_it(self):
        response = self.sign_in("BFS/STF/0012", tenant="sunrise")
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.data["error"]["code"], "INVALID_CREDENTIALS")

    def test_a_staff_id_is_refused_when_no_school_is_named(self):
        response = self.sign_in("BFS/STF/0012", tenant=None)
        self.assertEqual(response.status_code, 401)

    def test_a_wrong_password_is_refused_as_bad_credentials(self):
        response = self.sign_in("BFS/STF/0012", password="not-it")
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.data["error"]["code"], "INVALID_CREDENTIALS")

    def test_two_numbers_differing_only_in_case_sign_nobody_in(self):
        self.make_staff(
            "okon@brightfield.test", "Emem", "Okon", staff_number="bfs/stf/0012",
        )
        response = self.sign_in("BFS/STF/0012")
        self.assertEqual(response.status_code, 401)

    def test_the_attempt_log_records_the_number_as_typed(self):
        self.sign_in("BFS/STF/0012", password="not-it")
        attempt = AuthAttempt.objects.order_by("-id").first()
        self.assertEqual(attempt.email_entered, "BFS/STF/0012")
        self.assertEqual(attempt.user_id, self.eze.user_id)

    def test_an_identifier_and_an_email_together_take_the_email(self):
        cache.clear()
        response = APIClient().post(LOGIN_URL, {
            "email": self.eze.user.email, "identifier": "nonsense",
            "password": PASSWORD, "tenant": "brightfield",
        }, format="json")
        self.assertEqual(response.status_code, 200, response.data)


class StaffEmailCorrectionTests(StaffFixture):
    EMAIL_KEY = "school.teachers.email"

    def invited(self, email, staff_number):
        """A member of staff invited but not yet activated."""
        staff = self.make_staff(email, "Funke", "Adeyemi", staff_number=staff_number)
        staff.user.status = User.Status.PENDING
        staff.user.save(update_fields=["status"])
        InvitationService.create(staff.user, invited_by=self.admin)
        return staff

    def test_correcting_an_invited_address_kills_the_old_link(self):
        staff = self.invited("funke@gmial.test", "BFS/STF/0003")
        before = UserInvitation.objects.get(user=staff.user).token_hash

        response = self.patch(
            self.admin, "staff-account-email", {"email": "funke@gmail.test"},
            pk=staff.pk,
        )

        self.assertEqual(response.status_code, 200, response.data)
        staff.user.refresh_from_db()
        self.assertEqual(staff.user.email, "funke@gmail.test")
        after = UserInvitation.objects.get(user=staff.user)
        self.assertNotEqual(after.token_hash, before)
        self.assertFalse(after.is_used)

    def test_an_activated_account_keeps_its_used_invitation_alone(self):
        InvitationService.create(self.eze.user, invited_by=self.admin)
        invitation = UserInvitation.objects.get(user=self.eze.user)
        invitation.consume()

        response = self.patch(
            self.admin, "staff-account-email", {"email": "eze.new@brightfield.test"},
            pk=self.eze.pk,
        )

        self.assertEqual(response.status_code, 200, response.data)
        invitation.refresh_from_db()
        self.assertTrue(invitation.is_used)
        self.assertEqual(
            UserInvitation.objects.get(user=self.eze.user).token_hash,
            invitation.token_hash,
        )

    def test_a_role_with_the_email_read_only_cannot_change_it(self):
        install_declared_fields("school.teachers")
        role = make_role(self.school, name="Records clerk", key="records_clerk")
        for key in ALL_KEYS:
            make_role_permission(role, self.permissions[key])
        set_field_access(role, self.EMAIL_KEY, read=True, write=False)
        clerk = make_school_admin(
            None, email="clerk@brightfield.test", tenant=self.tenant,
        )
        make_assignment(self.school, clerk, role, branch=None)

        response = self.patch(
            clerk, "staff-account-email", {"email": "eze.new@brightfield.test"},
            pk=self.eze.pk,
        )

        self.assertEqual(response.status_code, 403, response.data)
        self.assertEqual(
            StaffProfile.all_objects.get(pk=self.eze.pk).user.email,
            "eze@brightfield.test",
        )

    def test_the_history_shows_the_change_and_its_note_but_not_the_addresses(self):
        response = self.patch(
            self.admin, "staff-account-email",
            {"email": "eze.new@brightfield.test", "note": "Mistyped at invite"},
            pk=self.eze.pk,
        )
        self.assertEqual(response.status_code, 200, response.data)

        history = self.get(self.admin, "staff-history", pk=self.eze.pk)
        self.assertEqual(history.status_code, 200, history.data)
        entries = [
            entry for entry in history.data["data"]["entries"]
            if entry["kind"] == "account" and entry["event"] == "EMAIL_CHANGED"
        ]
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["note"], "Mistyped at invite")
        self.assertEqual(entries[0]["actor"]["id"], self.admin.pk)
        self.assertNotIn("eze@brightfield.test", str(history.data))
        self.assertNotIn("eze.new@brightfield.test", str(history.data))

    def test_an_invited_persons_history_shows_the_reissued_invitation(self):
        staff = self.invited("funke@gmial.test", "BFS/STF/0003")
        self.patch(
            self.admin, "staff-account-email", {"email": "funke@gmail.test"},
            pk=staff.pk,
        )
        history = self.get(self.admin, "staff-history", pk=staff.pk)
        events = [
            entry["event"] for entry in history.data["data"]["entries"]
            if entry["kind"] == "account"
        ]
        self.assertIn("EMAIL_CHANGED", events)
        self.assertIn("INVITATION_SENT", events)

    def test_another_schools_account_events_never_reach_this_history(self):
        self.patch(
            self.solo_admin, "staff-account-email", {"email": "bola.new@sunrise.test"},
            pk=self.solo_staff.pk,
        )
        history = self.get(self.admin, "staff-history", pk=self.eze.pk)
        self.assertEqual(
            [e for e in history.data["data"]["entries"] if e["kind"] == "account"], [],
        )


class StaffIdPasswordResetTests(StaffFixture):
    """A reset asked for by staff ID goes to the email on file, at that school only."""

    URL = "/v1/user/auth/password/reset/request/"

    def ask(self, identifier, *, tenant="brightfield"):
        cache.clear()
        return APIClient().post(
            self.URL, {"identifier": identifier, "tenant": tenant}, format="json",
        )

    def resets_for(self, user):
        from vs_user.models import PasswordResetRequest

        return PasswordResetRequest.objects.filter(user=user).count()

    def test_a_staff_id_issues_a_reset_for_its_owner(self):
        response = self.ask("bfs/stf/0012")
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(self.resets_for(self.eze.user), 1)

    def test_a_staff_id_at_the_wrong_school_issues_nothing_and_says_the_same(self):
        here = self.ask("BFS/STF/0012")
        elsewhere = self.ask("BFS/STF/0012", tenant="sunrise")
        self.assertEqual(elsewhere.status_code, 200)
        self.assertEqual(elsewhere.data, here.data)
        self.assertEqual(self.resets_for(self.eze.user), 1)

    def test_an_unknown_staff_id_answers_exactly_like_a_known_one(self):
        known = self.ask("BFS/STF/0012")
        unknown = self.ask("BFS/STF/9999")
        self.assertEqual(unknown.status_code, known.status_code)
        self.assertEqual(unknown.data, known.data)

    def test_the_same_box_still_takes_an_email_address(self):
        response = self.ask(self.eze.user.email)
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(self.resets_for(self.eze.user), 1)

    def test_an_empty_box_is_refused(self):
        response = self.ask("  ")
        self.assertEqual(response.status_code, 400)

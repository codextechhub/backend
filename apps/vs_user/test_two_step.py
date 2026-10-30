"""Two-step sign-in (``services.two_step``): who is asked, and every way through.

Bright Star has a finance officer who can raise bills, a bursary clerk who can
only look at them, a teacher, and a head who runs the school. CodeX staff are
asked whatever their role. A school that turns the setting off asks nobody.
"""
from __future__ import annotations

import base64
from unittest import mock

from django.conf import settings
from django.core.cache import cache
from django.test import SimpleTestCase, TestCase
from rest_framework.test import APIClient

from core.test_utils import TenantAPIClient
from vs_config.models import ConfigurationDefinition
from vs_config.services.resolution import set_value
from vs_rbac.tests.helpers import (
    make_assignment,
    make_branch,
    make_permission,
    make_role,
    make_role_permission,
    make_school,
    make_school_admin,
    make_staff_user,
)

from . import totp
from .models import LoginSession, TwoStepAuthenticator, TwoStepChallenge, TwoStepRecoveryCode
from .services import two_step
from .tests import make_cx_user

PASSWORD = "Str0ng!pass123"
AUTH = "/v1/user/auth"


class CodeTests(SimpleTestCase):
    """The codes match RFC 6238's published values, and one code works once."""

    def test_the_rfc_test_vectors(self):
        secret = base64.b32encode(b"12345678901234567890").decode()
        for seconds, code in {59: "287082", 1111111109: "081804", 2000000000: "279037"}.items():
            self.assertEqual(totp.code_at(secret, seconds // totp.PERIOD), code)

    def test_a_code_is_good_for_its_step_and_the_one_either_side(self):
        secret = totp.new_secret()
        now = 1_800_000_000
        step = totp.current_step(now)
        for drift in (-1, 0, 1):
            self.assertEqual(totp.matching_step(secret, totp.code_at(secret, step + drift), now=now), step + drift)
        self.assertIsNone(totp.matching_step(secret, totp.code_at(secret, step + 2), now=now))

    def test_a_used_step_is_refused(self):
        secret = totp.new_secret()
        now = 1_800_000_000
        step = totp.current_step(now)
        self.assertIsNone(totp.matching_step(secret, totp.code_at(secret, step), after=step, now=now))

    def test_spaces_are_ignored_and_anything_else_refused(self):
        secret = totp.new_secret()
        code = totp.code_at(secret, totp.current_step())
        self.assertIsNotNone(totp.matching_step(secret, f"{code[:3]} {code[3:]}"))
        for junk in ("", "12345", "1234567", "abcdef"):
            self.assertIsNone(totp.matching_step(secret, junk))


class WhoIsAskedTests(SimpleTestCase):
    def test_administrators_and_whoever_moves_money(self):
        self.assertTrue(two_step.runs_or_pays({"school.settings.update"}))
        self.assertTrue(two_step.runs_or_pays({"school.administrators.view"}))
        self.assertTrue(two_step.runs_or_pays({"school.roles.assign"}))
        self.assertTrue(two_step.runs_or_pays({"finance.invoice.create"}))
        self.assertTrue(two_step.runs_or_pays({"payments.collection.refund"}))

    def test_not_a_teacher_nor_someone_who_only_reads_the_books(self):
        self.assertFalse(two_step.runs_or_pays({"school.profile.view", "academics.class.view"}))
        self.assertFalse(two_step.runs_or_pays({"finance.invoice.view", "payments.collection.view"}))
        self.assertFalse(two_step.runs_or_pays(set()))


def code_now(user) -> str:
    authenticator = TwoStepAuthenticator.objects.get(user=user)
    return totp.code_at(authenticator.secret, totp.current_step())


class SignInFlowTests(TestCase):
    """Through the API, as the sign-in page drives it."""

    @classmethod
    def setUpTestData(cls):
        cls.cx = make_cx_user(email="ada@codex.test", password=PASSWORD)

    def setUp(self):
        cache.clear()
        self.client = APIClient()

    def sign_in(self, email="ada@codex.test", tenant="codex"):
        return self.client.post(
            f"{AUTH}/login/", {"email": email, "password": PASSWORD, "tenant": tenant}, format="json",
        )

    def set_up(self):
        """Sign in for the first time and set up the authenticator; the confirm answer."""
        challenge = self.sign_in().json()["error"]["challenge"]
        self.client.post(f"{AUTH}/two-step/set-up/", {"challenge": challenge}, format="json")
        return self.client.post(
            f"{AUTH}/two-step/set-up/confirm/",
            {"challenge": challenge, "code": code_now(self.cx)}, format="json",
        )

    def test_a_correct_password_alone_signs_nobody_in(self):
        response = self.sign_in()

        self.assertEqual(response.status_code, 403, response.content)
        error = response.json()["error"]
        self.assertEqual(error["code"], "TWO_STEP_SET_UP_REQUIRED")
        self.assertTrue(error["challenge"].startswith("ts_"))
        self.assertNotIn("access", response.json().get("data") or {})
        self.assertFalse(response.cookies)
        self.assertFalse(LoginSession.objects.filter(user=self.cx).exists())

    def test_setting_up_signs_in_and_hands_out_ten_backup_codes(self):
        challenge = self.sign_in().json()["error"]["challenge"]
        started = self.client.post(f"{AUTH}/two-step/set-up/", {"challenge": challenge}, format="json")
        self.assertEqual(started.status_code, 200, started.content)
        self.assertTrue(started.json()["data"]["otpauth_uri"].startswith("otpauth://totp/CodeX:ada@codex.test?"))

        confirmed = self.client.post(
            f"{AUTH}/two-step/set-up/confirm/",
            {"challenge": challenge, "code": code_now(self.cx)}, format="json",
        )

        self.assertEqual(confirmed.status_code, 200, confirmed.content)
        data = confirmed.json()["data"]
        self.assertTrue(data["access"])
        self.assertEqual(len(data["recovery_codes"]), 10)
        self.assertIn(settings.AUTH_REFRESH_COOKIE_NAME, confirmed.cookies)
        self.assertTrue(LoginSession.objects.filter(user=self.cx, is_active=True).exists())

    def test_once_set_up_the_next_sign_in_asks_for_a_code(self):
        self.set_up()
        response = self.sign_in()
        self.assertEqual(response.json()["error"]["code"], "TWO_STEP_REQUIRED")

        challenge = response.json()["error"]["challenge"]
        # The code that finished setting up was used; wait for a fresh step.
        with mock.patch("vs_user.totp.time.time", return_value=(totp.current_step() + 1) * totp.PERIOD):
            verified = self.client.post(
                f"{AUTH}/two-step/verify/",
                {"challenge": challenge, "code": totp.code_at(
                    TwoStepAuthenticator.objects.get(user=self.cx).secret, totp.current_step() + 1,
                )},
                format="json",
            )
        self.assertEqual(verified.status_code, 200, verified.content)
        self.assertTrue(verified.json()["data"]["access"])

    def test_the_code_that_was_just_used_is_refused(self):
        self.set_up()
        challenge = self.sign_in().json()["error"]["challenge"]

        response = self.client.post(
            f"{AUTH}/two-step/verify/", {"challenge": challenge, "code": code_now(self.cx)}, format="json",
        )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["error"]["code"], "TWO_STEP_CODE_INVALID")

    def test_five_wrong_codes_end_the_challenge_and_count_towards_the_lockout(self):
        self.set_up()
        challenge = self.sign_in().json()["error"]["challenge"]
        answers = [
            self.client.post(
                f"{AUTH}/two-step/verify/", {"challenge": challenge, "code": "000000"}, format="json",
            ).json()["error"]["code"]
            for _ in range(6)
        ]

        self.assertEqual(answers[:4], ["TWO_STEP_CODE_INVALID"] * 4)
        self.assertEqual(answers[4:], ["TWO_STEP_CHALLENGE_INVALID"] * 2)
        self.assertGreaterEqual(self.cx.lockout.failure_count, 5)

    def test_a_backup_code_works_once(self):
        codes = self.set_up().json()["data"]["recovery_codes"]

        first = self.client.post(
            f"{AUTH}/two-step/verify/",
            {"challenge": self.sign_in().json()["error"]["challenge"], "recovery_code": codes[0].upper()},
            format="json",
        )
        again = self.client.post(
            f"{AUTH}/two-step/verify/",
            {"challenge": self.sign_in().json()["error"]["challenge"], "recovery_code": codes[0]},
            format="json",
        )

        self.assertEqual(first.status_code, 200, first.content)
        self.assertEqual(again.status_code, 400)
        self.assertEqual(TwoStepRecoveryCode.objects.filter(user=self.cx, used_at__isnull=True).count(), 9)

    def test_an_expired_challenge_is_refused(self):
        self.set_up()
        challenge = self.sign_in().json()["error"]["challenge"]
        TwoStepChallenge.objects.filter(user=self.cx).update(expires_at="2020-01-01T00:00:00Z")

        response = self.client.post(
            f"{AUTH}/two-step/verify/", {"challenge": challenge, "code": code_now(self.cx)}, format="json",
        )

        self.assertEqual(response.json()["error"]["code"], "TWO_STEP_CHALLENGE_INVALID")

    def test_a_challenge_cannot_be_used_to_set_up_over_an_existing_authenticator(self):
        self.set_up()
        challenge = self.sign_in().json()["error"]["challenge"]

        response = self.client.post(f"{AUTH}/two-step/set-up/", {"challenge": challenge}, format="json")

        self.assertEqual(response.json()["error"]["code"], "TWO_STEP_CHALLENGE_INVALID")

    def test_a_suspended_account_is_not_signed_in_by_a_right_code(self):
        self.set_up()
        challenge = self.sign_in().json()["error"]["challenge"]
        type(self.cx).objects.filter(pk=self.cx.pk).update(status="SUSPENDED")

        with mock.patch("vs_user.totp.time.time", return_value=(totp.current_step() + 1) * totp.PERIOD):
            response = self.client.post(
                f"{AUTH}/two-step/verify/",
                {"challenge": challenge, "code": totp.code_at(
                    TwoStepAuthenticator.objects.get(user=self.cx).secret, totp.current_step() + 1,
                )},
                format="json",
            )

        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["error"]["code"], "ACCOUNT_SUSPENDED")


class SchoolRulesTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.school = make_school(slug="bright-star", name="Bright Star")
        cls.branch = make_branch(cls.school)
        cls.tenant = cls.school.tenant

        def person(email, *keys):
            user = make_staff_user(cls.branch, email=email, password=PASSWORD)
            if keys:
                role = make_role(cls.school, name=email)
                for key in keys:
                    make_role_permission(role, make_permission(key))
                make_assignment(cls.school, user, role)
            return user

        cls.officer = person("officer@bright.test", "finance.invoice.create")
        cls.clerk = person("clerk@bright.test", "finance.invoice.view")
        cls.teacher = person("teacher@bright.test")
        cls.head = person("head@bright.test", "school.settings.update")

    def setUp(self):
        cache.clear()

    def asked(self, user):
        response = APIClient().post(
            f"{AUTH}/login/",
            {"email": user.email, "password": PASSWORD, "tenant": "bright-star"},
            format="json",
        )
        self.assertIn(response.status_code, (200, 403), response.content)
        return response.status_code == 403 and response.json()["error"]["code"].startswith("TWO_STEP")

    def turn_off(self):
        definition, _ = ConfigurationDefinition.objects.get_or_create(
            key=two_step.SETTING_KEY,
            defaults={
                "label": "Two-step", "value_type": "BOOLEAN", "default_value": True,
                "allowed_scopes": ["platform", "school"],
            },
        )
        set_value(definition=definition, value=False, actor=self.head, tenant=self.tenant)

    def test_by_default_the_head_and_the_finance_officer_are_asked(self):
        self.assertTrue(self.asked(self.head))
        self.assertTrue(self.asked(self.officer))

    def test_the_teacher_and_the_clerk_who_only_reads_are_not(self):
        self.assertFalse(self.asked(self.teacher))
        self.assertFalse(self.asked(self.clerk))

    def test_a_school_that_turns_it_off_asks_nobody(self):
        self.turn_off()
        self.assertFalse(self.asked(self.head))
        self.assertFalse(self.asked(self.officer))

    def test_codex_staff_are_asked_whatever_a_school_decides(self):
        self.turn_off()
        self.assertTrue(two_step.required_for(make_cx_user(email="bola@codex.test", password=PASSWORD)))


class ResetTests(TestCase):
    """A lost phone: another administrator clears it; nobody clears their own."""

    @classmethod
    def setUpTestData(cls):
        cls.admin = make_cx_user(email="admin@codex.test", password=PASSWORD)
        make_assignment(
            cls.admin.tenant, cls.admin,
            _role_with(cls.admin.tenant, "platform.team.update"),
        )
        cls.colleague = make_cx_user(email="tunde@codex.test", password=PASSWORD)

    def give_authenticator(self, user):
        TwoStepAuthenticator.objects.create(user=user, secret=totp.new_secret(), confirmed_at="2026-10-01T00:00:00Z")
        TwoStepRecoveryCode.objects.create(user=user, code_hash="x")

    def test_an_administrator_resets_a_colleague(self):
        self.give_authenticator(self.colleague)

        response = TenantAPIClient(user=self.admin).post(f"/v1/user/{self.colleague.pk}/two-step/reset/")

        self.assertEqual(response.status_code, 200, response.content)
        self.assertFalse(TwoStepAuthenticator.objects.filter(user=self.colleague).exists())
        self.assertFalse(TwoStepRecoveryCode.objects.filter(user=self.colleague).exists())

    def test_nobody_resets_their_own(self):
        self.give_authenticator(self.admin)

        response = TenantAPIClient(user=self.admin).post(f"/v1/user/{self.admin.pk}/two-step/reset/")

        self.assertEqual(response.status_code, 403)
        self.assertTrue(TwoStepAuthenticator.objects.filter(user=self.admin).exists())

    def test_without_the_key_it_is_refused(self):
        self.give_authenticator(self.admin)

        response = TenantAPIClient(user=self.colleague).post(f"/v1/user/{self.admin.pk}/two-step/reset/")

        self.assertEqual(response.status_code, 403)
        self.assertTrue(TwoStepAuthenticator.objects.filter(user=self.admin).exists())


class MyCodesTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.cx = make_cx_user(email="ada@codex.test", password=PASSWORD)
        cls.secret = totp.new_secret()
        TwoStepAuthenticator.objects.create(user=cls.cx, secret=cls.secret, confirmed_at="2026-10-01T00:00:00Z")

    def setUp(self):
        cache.clear()

    def test_my_status(self):
        data = TenantAPIClient(user=self.cx).get(f"{AUTH}/me/two-step/").json()["data"]
        self.assertEqual(data, {"set_up": True, "required": True, "recovery_codes_left": 0})

    def test_new_backup_codes_need_a_code_from_the_app(self):
        client = TenantAPIClient(user=self.cx)
        refused = client.post(f"{AUTH}/me/two-step/recovery-codes/", {"code": "000000"}, format="json")
        made = client.post(
            f"{AUTH}/me/two-step/recovery-codes/",
            {"code": totp.code_at(self.secret, totp.current_step())}, format="json",
        )

        self.assertEqual(refused.status_code, 400)
        self.assertEqual(made.status_code, 200, made.content)
        self.assertEqual(len(made.json()["data"]["recovery_codes"]), 10)


def _role_with(tenant, key):
    role = make_role(tenant, name=f"Holds {key}")
    make_role_permission(role, make_permission(key))
    return role

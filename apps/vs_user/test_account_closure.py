"""Closing your own account: the rule, the review and the web page.

Meridian Academy has two branches, Ikeja and Lekki, and Harbour Primary has
one. Mrs Okafor is a parent at Ikeja whose account closes the moment she
confirms, and Tunde is a pupil at Lekki whose request waits for the school.
A policy registered here stands in for the product, as the device session
tests do, so the engine is proved without importing any product app.
"""
from __future__ import annotations

import datetime as dt
from unittest import mock

from django.core import mail
from django.core.cache import cache
from django.core.management import call_command
from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIRequestFactory, force_authenticate
from rest_framework_simplejwt.token_blacklist.models import OutstandingToken

from core.test_utils import TenantAPIClient
from vs_audit.models import AuditEvent
from vs_notifications.models import NativePushDevice, PushSubscription
from vs_rbac.models import FieldDefinition
from vs_rbac.tests.helpers import (
    install_declared_fields,
    make_assignment,
    make_branch,
    make_permission,
    make_role,
    make_role_permission,
    make_school,
    make_school_admin,
    make_vision_user,
    set_field_access,
)
from vs_user.models import AccountClosureLink, AccountClosureRequest, LoginSession, User
from vs_user.services import account_closure as closures
from vs_user.services import device_sessions
from vs_user.services.account_closure import (
    MODE_CLOSE,
    MODE_REQUEST,
    PERM_UPDATE,
    PERM_VIEW,
    PERSON_FIELD,
    ClosurePolicy,
)
from vs_user.views.account_closure import AccountClosureView

PASSWORD = "Str0ng!pass123"
ME = "/v1/user/me/account-closure/"
LIST = "/v1/user/account-closures/"
LINK_REQUEST = "/v1/user/account-closure/request/"
LINK_CONFIRM = "/v1/user/account-closure/confirm/"
SEND_TASK = "vs_user.tasks.send_account_closure_email_task.delay"
Status = AccountClosureRequest.Status


def person(email, tenant, first, last="Okafor", branch=None):
    return make_school_admin(
        branch, email=email, tenant=tenant, password=PASSWORD,
        first_name=first, last_name=last,
    )


class ClosureFixture(TestCase):
    """Meridian (two branches), Harbour (one), Greenfield (a stranger)."""

    @classmethod
    def setUpTestData(cls):
        cls.meridian = make_school(slug="meridian", name="Meridian Academy")
        cls.tenant = cls.meridian.tenant
        cls.ikeja = make_branch(cls.meridian, name="Ikeja Branch")
        cls.lekki = make_branch(cls.meridian, name="Lekki Branch", is_main=False)
        cls.harbour = make_school(slug="harbour", name="Harbour Primary")
        cls.harbour_tenant = cls.harbour.tenant
        cls.main = make_branch(cls.harbour, name="Main Branch")
        cls.greenfield = make_school(slug="greenfield", name="Greenfield School")
        cls.green_tenant = cls.greenfield.tenant
        cls.green_branch = make_branch(cls.greenfield, name="Main Branch")

        cls.view_key = make_permission(PERM_VIEW)
        cls.update_key = make_permission(PERM_UPDATE)

        cls.parent = person("ada@meridian.test", cls.tenant, "Ada")
        cls.pupil = person("tunde@meridian.test", cls.tenant, "Tunde")
        cls.ikeja_pupil = person("bola@meridian.test", cls.tenant, "Bola")
        cls.unplaced_pupil = person("kemi@meridian.test", cls.tenant, "Kemi")
        cls.solo_pupil = person("sade@harbour.test", cls.harbour_tenant, "Sade")
        cls.solo_unplaced = person("yemi@harbour.test", cls.harbour_tenant, "Yemi")
        cls.green_pupil = person("femi@greenfield.test", cls.green_tenant, "Femi")
        cls.staff = person("teacher@meridian.test", cls.tenant, "Chidi", "Eze")

        cls.reviewer, cls.reviewer_role = cls.reviewer_of(
            "head@meridian.test", cls.meridian, branch=None,
        )
        cls.ikeja_reviewer, _ = cls.reviewer_of(
            "ikeja.head@meridian.test", cls.meridian, branch=cls.ikeja,
        )
        cls.lekki_reviewer, _ = cls.reviewer_of(
            "lekki.head@meridian.test", cls.meridian, branch=cls.lekki,
        )
        cls.reader, _ = cls.reviewer_of(
            "reader@meridian.test", cls.meridian, branch=None, keys=(cls.view_key,),
        )
        cls.solo_reviewer, _ = cls.reviewer_of(
            "head@harbour.test", cls.harbour, branch=cls.main,
        )
        cls.green_reviewer, _ = cls.reviewer_of(
            "head@greenfield.test", cls.greenfield, branch=None,
        )
        nobody_role = make_role(cls.meridian, name="Nobody")
        cls.nobody = person("nobody@meridian.test", cls.tenant, "Nobody", "Holds")
        make_assignment(cls.meridian, cls.nobody, nobody_role, branch=None)

    @classmethod
    def reviewer_of(cls, email, school, *, branch, keys=None):
        user = person(email, school.tenant, "Head", "Reviewer")
        role = make_role(school, name=f"Reviewer {email}")
        for key in keys or (cls.view_key, cls.update_key):
            make_role_permission(role, key)
        make_assignment(school, user, role, branch=branch)
        return user, role

    def setUp(self):
        cache.clear()
        self.policies = {
            self.parent.pk: ClosurePolicy(MODE_CLOSE, self.ikeja.pk, "parent"),
            self.pupil.pk: ClosurePolicy(MODE_REQUEST, self.lekki.pk, "pupil"),
            self.ikeja_pupil.pk: ClosurePolicy(MODE_REQUEST, self.ikeja.pk, "pupil"),
            self.unplaced_pupil.pk: ClosurePolicy(MODE_REQUEST, None, "pupil"),
            self.solo_pupil.pk: ClosurePolicy(MODE_REQUEST, self.main.pk, "pupil"),
            self.solo_unplaced.pk: ClosurePolicy(MODE_REQUEST, None, "pupil"),
            self.green_pupil.pk: ClosurePolicy(MODE_REQUEST, self.green_branch.pk, "pupil"),
        }
        closures.register_account_closure_policy(
            "test", lambda user: self.policies.get(user.pk),
        )
        self.addCleanup(closures._providers.pop, "test", None)

    def api(self, user, tenant=None):
        return TenantAPIClient(user, tenant)

    def ask(self, user, **body):
        return self.api(user).post(ME, {"confirm": True, **body}, format="json")

    def waiting(self, user, branch, **extra):
        return AccountClosureRequest.all_objects.create(
            tenant=user.tenant, user=user, branch=branch, kind="pupil",
            channel="app", status=Status.PENDING, **extra,
        )

    def reload(self, user):
        return User.objects.get(pk=user.pk)


class WhatThePersonSeesTests(ClosureFixture):
    def test_a_parent_is_offered_deleting_at_once(self):
        data = self.api(self.parent).get(ME).json()["data"]
        self.assertEqual(data, {"eligible": True, "mode": "close", "pending": None})

    def test_a_pupil_is_offered_a_request(self):
        data = self.api(self.pupil).get(ME).json()["data"]
        self.assertEqual(data, {"eligible": True, "mode": "request", "pending": None})

    def test_a_member_of_staff_is_offered_nothing(self):
        data = self.api(self.staff).get(ME).json()["data"]
        self.assertEqual(data, {"eligible": False, "mode": None, "pending": None})

    def test_the_state_needs_a_signed_in_person(self):
        from rest_framework.test import APIClient

        self.assertEqual(APIClient().get(ME).status_code, 401)

    def test_a_waiting_request_shows_when_it_was_asked(self):
        self.ask(self.pupil)
        data = self.api(self.pupil).get(ME).json()["data"]
        self.assertTrue(data["eligible"])
        self.assertIsNotNone(data["pending"]["requested_at"])


class WhoMayDeleteTests(ClosureFixture):
    def test_staff_are_refused_with_a_pointer_to_their_administrator(self):
        response = self.ask(self.staff)
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["code"], "ACCOUNT_CLOSURE_NOT_OFFERED")
        self.assertIn("administrator", response.json()["message"])
        self.assertEqual(self.reload(self.staff).status, User.Status.ACTIVE)
        self.assertFalse(AccountClosureRequest.all_objects.exists())

    def test_a_platform_account_is_refused(self):
        operator = make_vision_user(email="operator@codex.test")
        response = TenantAPIClient(operator).post(ME, {"confirm": True}, format="json")
        self.assertEqual(response.status_code, 403)
        self.assertEqual(self.reload(operator).status, User.Status.ACTIVE)

    def test_a_missing_or_false_confirmation_closes_nothing(self):
        for body in ({}, {"confirm": False}):
            with self.subTest(body=body):
                response = self.api(self.parent).post(ME, body, format="json")
                self.assertEqual(response.status_code, 400)
        self.assertEqual(self.reload(self.parent).status, User.Status.ACTIVE)
        self.assertFalse(AccountClosureRequest.all_objects.exists())

    def test_an_account_viewed_as_another_person_is_never_closed(self):
        request = APIRequestFactory().post(ME, {"confirm": True}, format="json")
        force_authenticate(request, user=self.parent)
        request.impersonation_session = object()
        response = AccountClosureView.as_view()(request)
        self.assertEqual(response.status_code, 403)
        self.assertEqual(self.reload(self.parent).status, User.Status.ACTIVE)


class ParentClosesAtOnceTests(ClosureFixture):
    def test_the_account_closes_and_the_trail_names_the_person(self):
        response = self.ask(self.parent)
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.json()["data"], {"status": "closed"})

        parent = self.reload(self.parent)
        self.assertEqual(parent.status, User.Status.DEACTIVATED)
        self.assertFalse(parent.is_active)
        self.assertFalse(parent.has_usable_password())

        record = AccountClosureRequest.all_objects.get(user=parent)
        self.assertEqual(record.status, Status.CLOSED)
        self.assertEqual(record.channel, "app")
        self.assertEqual(record.kind, "parent")
        self.assertEqual(record.branch_id, self.ikeja.pk)
        self.assertEqual(record.decided_by_id, parent.pk)
        self.assertIsNotNone(record.decided_at)

    def test_the_audit_trail_records_the_closure_by_the_person(self):
        self.ask(self.parent)
        event = AuditEvent._base_manager.get(
            entity_id=str(self.parent.pk), metadata__auth_event="ACCOUNT_DEACTIVATED",
        )
        self.assertEqual(event.actor_user_id, self.parent.pk)
        self.assertEqual(event.metadata["origin"], "SELF_SERVICE")

    def test_the_person_can_no_longer_sign_in_or_use_the_token_they_held(self):
        client = self.api(self.parent)
        self.ask(self.parent)
        login = self.api(self.parent).post(
            "/v1/user/auth/login/",
            {"email": self.parent.email, "password": PASSWORD, "tenant": "meridian"},
            format="json",
        )
        self.assertNotEqual(login.status_code, 200)
        self.assertIn(client.get(ME).status_code, (401, 403))

    def test_every_session_and_refresh_credential_ends_device_sessions_included(self):
        policy = device_sessions.DeviceSessionPolicy(
            lifetime=dt.timedelta(days=30), app_path="/parent/",
        )
        session, refresh, _access = device_sessions.issue(self.parent, policy=policy)
        browser = LoginSession.all_objects.create(user=self.parent, tenant=self.tenant)
        self.ask(self.parent)

        self.assertFalse(LoginSession.all_objects.filter(user=self.parent, is_active=True).exists())
        browser.refresh_from_db()
        self.assertEqual(browser.end_reason, "ACCOUNT_CLOSED")
        with self.assertRaises(device_sessions.DeviceSessionRefused):
            device_sessions.refresh(session.device_key, refresh)
        outstanding = OutstandingToken.objects.filter(user=self.parent)
        self.assertTrue(outstanding.exists())
        self.assertFalse(outstanding.filter(blacklistedtoken__isnull=True).exists())

    def test_every_push_device_is_forgotten_and_only_theirs(self):
        stranger = self.pupil
        for user, key in ((self.parent, "browser-key"), (stranger, "stranger-key")):
            PushSubscription.all_objects.create(
                tenant=self.tenant, user=user, endpoint=f"https://push.test/{key}",
                endpoint_hash=f"hash-{key}", p256dh="p", auth="a", device_key=key,
            )
            NativePushDevice.all_objects.create(
                tenant=self.tenant, user=user, platform="android", app_id="app.test",
                token=f"token-{key}", token_hash=f"token-hash-{key}", device_key="",
            )
        self.ask(self.parent)

        self.assertFalse(PushSubscription.all_objects.filter(user=self.parent).exists())
        self.assertFalse(NativePushDevice.all_objects.filter(user=self.parent).exists())
        self.assertEqual(PushSubscription.all_objects.filter(user=stranger).count(), 1)
        self.assertEqual(NativePushDevice.all_objects.filter(user=stranger).count(), 1)

    def test_an_administrator_can_reopen_the_account(self):
        from vs_user.services.user import UserStatusService

        self.ask(self.parent)
        UserStatusService.reactivate(self.reload(self.parent), self.reviewer)
        self.assertEqual(self.reload(self.parent).status, User.Status.ACTIVE)

    def test_asking_twice_does_not_close_it_twice(self):
        self.ask(self.parent)
        again = self.ask(self.parent)
        self.assertIn(again.status_code, (401, 403))
        self.assertEqual(AccountClosureRequest.all_objects.filter(user=self.parent).count(), 1)


class PupilAsksTheSchoolTests(ClosureFixture):
    def test_the_request_waits_and_the_sign_in_keeps_working(self):
        response = self.ask(self.pupil)
        self.assertEqual(response.status_code, 200, response.content)
        data = response.json()["data"]
        self.assertEqual(data["status"], "requested")
        self.assertIsNotNone(data["requested_at"])

        pupil = self.reload(self.pupil)
        self.assertEqual(pupil.status, User.Status.ACTIVE)
        self.assertTrue(pupil.has_usable_password())
        login = self.api(pupil).post(
            "/v1/user/auth/login/",
            {"email": pupil.email, "password": PASSWORD, "tenant": "meridian"},
            format="json",
        )
        self.assertEqual(login.status_code, 200, login.content)
        record = AccountClosureRequest.all_objects.get(user=pupil)
        self.assertEqual(
            (record.status, record.branch_id, record.kind, record.channel),
            (Status.PENDING, self.lekki.pk, "pupil", "app"),
        )

    def test_a_second_request_returns_the_one_already_waiting(self):
        first = self.ask(self.pupil).json()["data"]
        second = self.ask(self.pupil).json()["data"]
        self.assertEqual(first["requested_at"], second["requested_at"])
        self.assertEqual(AccountClosureRequest.all_objects.filter(user=self.pupil).count(), 1)

    def test_closing_it_ends_the_sign_in_and_records_who_decided(self):
        record = self.waiting(self.pupil, self.lekki)
        response = self.api(self.reviewer).post(f"{LIST}{record.pk}/close/")
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.json()["data"]["status"], "closed")

        pupil = self.reload(self.pupil)
        self.assertEqual(pupil.status, User.Status.DEACTIVATED)
        self.assertFalse(pupil.has_usable_password())
        record.refresh_from_db()
        self.assertEqual(
            (record.status, record.decided_by_id), (Status.CLOSED, self.reviewer.pk),
        )
        event = AuditEvent._base_manager.get(
            entity_id=str(pupil.pk), metadata__auth_event="ACCOUNT_DEACTIVATED",
        )
        self.assertEqual(event.actor_user_id, self.reviewer.pk)
        self.assertEqual(event.metadata["origin"], "TENANT_REVIEW")

    def test_declining_it_leaves_the_account_active_and_lets_them_ask_again(self):
        record = self.waiting(self.pupil, self.lekki)
        response = self.api(self.reviewer).post(f"{LIST}{record.pk}/decline/")
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(self.reload(self.pupil).status, User.Status.ACTIVE)
        record.refresh_from_db()
        self.assertEqual(record.status, Status.DECLINED)
        self.assertIsNone(
            self.api(self.pupil).get(ME).json()["data"]["pending"],
        )
        self.assertEqual(self.ask(self.pupil).json()["data"]["status"], "requested")
        self.assertEqual(AccountClosureRequest.all_objects.filter(user=self.pupil).count(), 2)

    def test_a_decided_request_cannot_be_decided_again(self):
        record = self.waiting(self.pupil, self.lekki)
        self.api(self.reviewer).post(f"{LIST}{record.pk}/decline/")
        for verb in ("close", "decline"):
            with self.subTest(verb=verb):
                response = self.api(self.reviewer).post(f"{LIST}{record.pk}/{verb}/")
                self.assertEqual(response.status_code, 409)
                self.assertEqual(response.json()["code"], "ACCOUNT_CLOSURE_NOT_PENDING")

    def test_a_stale_request_cannot_close_an_account_that_became_staff(self):
        record = self.waiting(self.pupil, self.lekki)
        del self.policies[self.pupil.pk]
        response = self.api(self.reviewer).post(f"{LIST}{record.pk}/close/")
        self.assertEqual(response.status_code, 409)
        self.assertEqual(self.reload(self.pupil).status, User.Status.ACTIVE)
        record.refresh_from_db()
        self.assertEqual(record.status, Status.PENDING)


class ReviewReachTests(ClosureFixture):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.at_lekki = cls.waiting_for(cls.pupil, cls.lekki)
        cls.at_ikeja = cls.waiting_for(cls.ikeja_pupil, cls.ikeja)
        cls.unplaced = cls.waiting_for(cls.unplaced_pupil, None)
        cls.at_harbour = cls.waiting_for(cls.solo_pupil, cls.main)
        cls.harbour_unplaced = cls.waiting_for(cls.solo_unplaced, None)
        cls.at_greenfield = cls.waiting_for(cls.green_pupil, cls.green_branch)

    @classmethod
    def waiting_for(cls, user, branch):
        return AccountClosureRequest.all_objects.create(
            tenant=user.tenant, user=user, branch=branch, kind="pupil",
            channel="app", status=Status.PENDING,
        )

    def ids(self, user, **params):
        response = self.api(user).get(LIST, params or None)
        self.assertEqual(response.status_code, 200, response.content)
        return {row["id"] for row in response.json()["data"]}

    def test_someone_without_the_view_key_is_refused(self):
        self.assertEqual(self.api(self.nobody).get(LIST).status_code, 403)
        self.assertEqual(self.api(self.parent).get(LIST).status_code, 403)

    def test_deciding_needs_the_update_key(self):
        for verb in ("close", "decline"):
            with self.subTest(verb=verb):
                response = self.api(self.reader).post(f"{LIST}{self.at_lekki.pk}/{verb}/")
                self.assertEqual(response.status_code, 403)
        self.assertEqual(self.reload(self.pupil).status, User.Status.ACTIVE)

    def test_a_whole_tenant_reviewer_reads_every_branch_and_never_another_tenants(self):
        self.assertEqual(
            self.ids(self.reviewer),
            {self.at_lekki.pk, self.at_ikeja.pk, self.unplaced.pk},
        )

    def test_a_branch_reviewer_reads_only_their_own_branch(self):
        self.assertEqual(self.ids(self.lekki_reviewer), {self.at_lekki.pk})
        self.assertEqual(self.ids(self.ikeja_reviewer), {self.at_ikeja.pk})

    def test_a_request_with_no_branch_is_read_only_by_a_whole_tenant_reviewer(self):
        self.assertIn(self.unplaced.pk, self.ids(self.reviewer))
        self.assertNotIn(self.unplaced.pk, self.ids(self.lekki_reviewer))
        self.assertNotIn(self.unplaced.pk, self.ids(self.ikeja_reviewer))

    def test_at_a_one_branch_tenant_every_request_is_the_branchs_own(self):
        self.assertEqual(
            self.ids(self.solo_reviewer),
            {self.at_harbour.pk, self.harbour_unplaced.pk},
        )

    def test_a_branch_reviewer_cannot_decide_another_branchs_request(self):
        for verb in ("close", "decline"):
            with self.subTest(verb=verb):
                response = self.api(self.ikeja_reviewer).post(
                    f"{LIST}{self.at_lekki.pk}/{verb}/",
                )
                self.assertEqual(response.status_code, 404)
        self.assertEqual(self.reload(self.pupil).status, User.Status.ACTIVE)
        self.at_lekki.refresh_from_db()
        self.assertEqual(self.at_lekki.status, Status.PENDING)

    def test_a_branch_reviewer_decides_their_own_branchs_request(self):
        response = self.api(self.lekki_reviewer).post(f"{LIST}{self.at_lekki.pk}/close/")
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(self.reload(self.pupil).status, User.Status.DEACTIVATED)

    def test_another_tenants_request_answers_not_found(self):
        for verb in ("close", "decline"):
            with self.subTest(verb=verb):
                response = self.api(self.reviewer).post(
                    f"{LIST}{self.at_greenfield.pk}/{verb}/",
                )
                self.assertEqual(response.status_code, 404)
        self.assertEqual(self.reload(self.green_pupil).status, User.Status.ACTIVE)

    def test_a_reviewer_cannot_reach_another_tenant_by_asserting_it(self):
        response = self.api(self.green_reviewer, "meridian").get(LIST)
        self.assertNotEqual(response.status_code, 200)

    def test_the_list_filters_by_status_newest_first_and_pages(self):
        self.at_lekki.status = Status.DECLINED
        self.at_lekki.save(update_fields=["status"])
        self.assertEqual(
            self.ids(self.reviewer, status="pending"), {self.at_ikeja.pk, self.unplaced.pk},
        )
        self.assertEqual(self.ids(self.reviewer, status="declined"), {self.at_lekki.pk})
        self.assertEqual(self.api(self.reviewer).get(LIST, {"status": "gone"}).status_code, 400)

        body = self.api(self.reviewer).get(LIST, {"page_size": 2}).json()
        self.assertEqual(body["pagination"]["totalItems"], 3)
        self.assertEqual(len(body["data"]), 2)
        stamps = [row["requested_at"] for row in body["data"]]
        self.assertEqual(stamps, sorted(stamps, reverse=True))

    def test_a_row_carries_the_person_the_decision_and_the_branch(self):
        self.api(self.reviewer).post(f"{LIST}{self.at_lekki.pk}/decline/")
        row = self.api(self.reviewer).get(LIST, {"status": "declined"}).json()["data"][0]
        self.assertEqual(row["user_name"], "Tunde Okafor")
        self.assertEqual(row["user_email"], "tunde@meridian.test")
        self.assertEqual(row["decided_by_name"], "Head Reviewer")
        self.assertEqual(row["branch_name"], "Lekki Branch")
        self.assertEqual(row["status"], "declined")

    def test_the_list_costs_the_same_however_many_rows_there_are(self):
        from django.db import connection
        from django.test.utils import CaptureQueriesContext

        with CaptureQueriesContext(connection) as few:
            self.api(self.reviewer).get(LIST)
        for number in range(5):
            extra = person(f"extra{number}@meridian.test", self.tenant, f"Extra{number}")
            self.waiting(extra, self.lekki)
        with CaptureQueriesContext(connection) as many:
            self.api(self.reviewer).get(LIST)
        self.assertEqual(len(few), len(many))


class FieldAccessTests(ClosureFixture):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.keys = install_declared_fields("school.account_closures")
        cls.blind, cls.blind_role = cls.reviewer_of(
            "blind@meridian.test", cls.meridian, branch=None,
        )
        set_field_access(cls.blind_role, PERSON_FIELD, read=False, write=False)
        cls.record = AccountClosureRequest.all_objects.create(
            tenant=cls.tenant, user=cls.pupil, branch=cls.lekki, kind="pupil",
            channel="app", status=Status.CLOSED, decided_at=timezone.now(),
            decided_by=cls.reviewer,
        )

    def row(self, user):
        response = self.api(user).get(LIST)
        self.assertEqual(response.status_code, 200, response.content)
        return response.json()["data"][0]

    def test_every_field_starts_open_to_read_and_closed_to_write(self):
        rows = FieldDefinition.objects.filter(
            resource__module_id="school", resource__name="account_closures",
        )
        self.assertEqual(rows.count(), 7)
        for field in rows:
            with self.subTest(field=field.key):
                self.assertFalse(field.sensitive)
                self.assertFalse(field.writable)
                self.assertEqual(field.default_access, {"read": True, "write": False})

    def test_a_reviewer_with_no_switch_reads_the_whole_row(self):
        row = self.row(self.reviewer)
        for name in ("user_id", "user_name", "user_email", "kind", "channel", "status",
                     "requested_at", "decided_at", "decided_by_id", "decided_by_name"):
            self.assertIn(name, row)

    def test_a_switched_off_person_disappears_under_every_name(self):
        row = self.row(self.blind)
        for name in ("user_id", "user_name", "user_email"):
            self.assertNotIn(name, row)
        self.assertEqual(row["status"], "closed")
        self.assertNotIn("Tunde", str(row))

    def test_the_decision_response_hides_the_person_too(self):
        waiting = self.waiting(self.ikeja_pupil, self.ikeja)
        data = self.api(self.blind).post(f"{LIST}{waiting.pk}/decline/").json()["data"]
        for name in ("user_id", "user_name", "user_email"):
            self.assertNotIn(name, data)
        self.assertEqual(data["status"], "declined")

    def test_a_reviewer_told_by_notification_is_not_told_who_when_the_person_is_hidden(self):
        with mock.patch("vs_notifications.notify.send_notification") as send:
            with self.captureOnCommitCallbacks(execute=True):
                self.ask(self.pupil)
        labels = {
            call.kwargs["context"]["person_label"]: {u.pk for u in call.kwargs["recipients"]}
            for call in send.call_args_list
        }
        self.assertIn(self.blind.pk, labels["Someone"])
        self.assertNotIn(self.blind.pk, labels["Tunde Okafor"])
        self.assertIn(self.reviewer.pk, labels["Tunde Okafor"])


class TellingTheReviewersTests(ClosureFixture):
    def alerted(self, user):
        with mock.patch("vs_notifications.notify.send_notification") as send:
            with self.captureOnCommitCallbacks(execute=True):
                response = self.ask(user)
        self.assertEqual(response.status_code, 200, response.content)
        events = {call.args[0] for call in send.call_args_list}
        recipients = {
            u.pk for call in send.call_args_list for u in call.kwargs["recipients"]
        }
        return events, recipients, send

    def test_a_request_at_lekki_reaches_lekki_and_whole_tenant_holders_only(self):
        events, recipients, _ = self.alerted(self.pupil)
        self.assertEqual(events, {"user.deletion_requested"})
        self.assertEqual(
            recipients,
            {self.reviewer.pk, self.reader.pk, self.lekki_reviewer.pk},
        )

    def test_an_account_that_closed_at_once_reaches_its_own_branchs_reviewers(self):
        events, recipients, send = self.alerted(self.parent)
        self.assertEqual(events, {"user.account_deleted"})
        self.assertEqual(
            recipients,
            {self.reviewer.pk, self.reader.pk, self.ikeja_reviewer.pk},
        )
        context = send.call_args.kwargs["context"]
        self.assertEqual(context["person_label"], "Ada Okafor")
        self.assertEqual(context["kind"], "parent")

    def test_a_request_with_no_branch_reaches_whole_tenant_holders_only(self):
        _events, recipients, _ = self.alerted(self.unplaced_pupil)
        self.assertEqual(recipients, {self.reviewer.pk, self.reader.pk})

    def test_another_tenants_reviewers_are_never_told(self):
        _events, recipients, _ = self.alerted(self.pupil)
        self.assertNotIn(self.green_reviewer.pk, recipients)
        self.assertNotIn(self.solo_reviewer.pk, recipients)

    def test_asking_again_does_not_alert_twice(self):
        self.alerted(self.pupil)
        _events, recipients, _ = self.alerted(self.pupil)
        self.assertEqual(recipients, set())

    def test_a_failing_alert_never_undoes_the_closure(self):
        with mock.patch(
            "vs_notifications.notify.send_notification", side_effect=RuntimeError("down"),
        ):
            with self.captureOnCommitCallbacks(execute=True):
                response = self.ask(self.parent)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.reload(self.parent).status, User.Status.DEACTIVATED)


class TheAlertsAsReviewersReadThemTests(ClosureFixture):
    """The templates, rendered for real."""

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        call_command("seed_notification_templates", verbosity=0)

    def test_the_headlines_name_who_and_what(self):
        from vs_notifications.models import Notification

        with self.captureOnCommitCallbacks(execute=True):
            self.ask(self.pupil)
            self.ask(self.parent)
        subjects = set(
            Notification.objects.filter(recipient=self.reviewer, channel="in_app")
            .values_list("subject", flat=True)
        )
        self.assertEqual(subjects, {
            "Tunde Okafor asked to delete their account",
            "Ada Okafor deleted their parent account",
        })


class PublicPageTests(ClosureFixture):
    def request_link(self, **body):
        return self.client_anonymous().post(LINK_REQUEST, body, format="json")

    def client_anonymous(self):
        from rest_framework.test import APIClient

        return APIClient()

    def asked_for(self, **body):
        cache.clear()
        with mock.patch(SEND_TASK) as send:
            with self.captureOnCommitCallbacks(execute=True):
                response = self.request_link(**body)
        return response, send

    def test_every_answer_is_the_same_whether_or_not_an_account_exists(self):
        shapes = {
            "unknown": {"email": "nobody@nowhere.test", "tenant": "meridian"},
            "eligible": {"email": self.parent.email, "tenant": "meridian"},
            "pupil": {"email": self.pupil.email, "tenant": "meridian"},
            "staff": {"email": self.staff.email, "tenant": "meridian"},
            "wrong tenant": {"email": self.parent.email, "tenant": "greenfield"},
            "no tenant": {"email": self.parent.email},
            "unknown identifier": {"identifier": "ADM/0001", "tenant": "meridian"},
        }
        answers = {}
        for name, body in shapes.items():
            response, _ = self.asked_for(**body)
            answers[name] = (response.status_code, response.json())
        self.assertEqual(len(set(map(str, answers.values()))), 1, answers)
        self.assertEqual(answers["eligible"][0], 200)

    def test_mail_is_sent_only_for_an_eligible_active_account(self):
        sent = {}
        for name, body in {
            "parent": {"email": self.parent.email, "tenant": "meridian"},
            "pupil": {"email": self.pupil.email, "tenant": "meridian"},
            "staff": {"email": self.staff.email, "tenant": "meridian"},
            "unknown": {"email": "nobody@nowhere.test", "tenant": "meridian"},
            "wrong tenant": {"email": self.parent.email, "tenant": "greenfield"},
        }.items():
            _, send = self.asked_for(**body)
            sent[name] = send.call_count
        self.assertEqual(
            sent, {"parent": 1, "pupil": 1, "staff": 0, "unknown": 0, "wrong tenant": 0},
        )

    def test_a_closed_account_is_sent_nothing(self):
        self.ask(self.parent)
        _, send = self.asked_for(email=self.parent.email, tenant="meridian")
        self.assertEqual(send.call_count, 0)

    def test_only_the_digest_of_the_token_is_stored(self):
        _, send = self.asked_for(email=self.parent.email, tenant="meridian")
        token = send.call_args.kwargs["token"]
        self.assertTrue(token.startswith("ac_"))
        link = AccountClosureLink.objects.get(user=self.parent)
        self.assertNotIn(token, link.token_hash)
        self.assertEqual(len(link.token_hash), 64)

    def test_asking_again_retires_the_earlier_link(self):
        _, first = self.asked_for(email=self.parent.email, tenant="meridian")
        _, second = self.asked_for(email=self.parent.email, tenant="meridian")
        self.assertEqual(
            self.confirm(first.call_args.kwargs["token"]).status_code, 400,
        )
        self.assertEqual(
            self.confirm(second.call_args.kwargs["token"]).status_code, 200,
        )

    def test_a_request_with_neither_email_nor_identifier_is_refused(self):
        self.assertEqual(self.request_link(tenant="meridian").status_code, 400)

    def test_the_request_is_throttled(self):
        cache.clear()
        codes = [
            self.request_link(email="nobody@nowhere.test", tenant="meridian").status_code
            for _ in range(5)
        ]
        self.assertEqual(codes[:3], [200, 200, 200])
        self.assertIn(429, codes[3:])

    def confirm(self, token):
        return self.client_anonymous().post(LINK_CONFIRM, {"token": token}, format="json")

    def token_for(self, user):
        _, send = self.asked_for(email=user.email, tenant="meridian")
        return send.call_args.kwargs["token"]

    def test_following_the_link_closes_a_parent_at_once(self):
        token = self.token_for(self.parent)
        response = self.confirm(token)
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.json()["data"], {"status": "closed"})
        self.assertEqual(self.reload(self.parent).status, User.Status.DEACTIVATED)
        record = AccountClosureRequest.all_objects.get(user=self.parent)
        self.assertEqual((record.channel, record.status), ("web", Status.CLOSED))

    def test_following_the_link_sends_a_pupil_to_the_school(self):
        token = self.token_for(self.pupil)
        data = self.confirm(token).json()["data"]
        self.assertEqual(data["status"], "requested")
        self.assertEqual(self.reload(self.pupil).status, User.Status.ACTIVE)
        record = AccountClosureRequest.all_objects.get(user=self.pupil)
        self.assertEqual((record.channel, record.status), ("web", Status.PENDING))

    def test_a_link_works_once(self):
        token = self.token_for(self.pupil)
        self.assertEqual(self.confirm(token).status_code, 200)
        again = self.confirm(token)
        self.assertEqual(again.status_code, 400)
        self.assertEqual(again.json()["code"], "ACCOUNT_CLOSURE_LINK_INVALID")
        self.assertEqual(AccountClosureRequest.all_objects.filter(user=self.pupil).count(), 1)

    def test_a_link_expires(self):
        token = self.token_for(self.pupil)
        later = timezone.now() + closures.LINK_LIFETIME + dt.timedelta(minutes=1)
        with mock.patch("django.utils.timezone.now", return_value=later):
            response = self.confirm(token)
        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.reload(self.pupil).status, User.Status.ACTIVE)
        self.assertFalse(AccountClosureRequest.all_objects.filter(user=self.pupil).exists())

    def test_a_link_still_works_just_before_it_expires(self):
        token = self.token_for(self.pupil)
        almost = timezone.now() + closures.LINK_LIFETIME - dt.timedelta(minutes=1)
        with mock.patch("django.utils.timezone.now", return_value=almost):
            self.assertEqual(self.confirm(token).status_code, 200)

    def test_unknown_foreign_and_malformed_tokens_are_refused_alike(self):
        reset_style = "pr_" + "x" * 43
        for token in ("", "ac_nope", reset_style, "x" * 20):
            with self.subTest(token=token):
                self.assertEqual(self.confirm(token).status_code, 400)

    def test_a_link_for_an_account_that_since_became_staff_does_nothing(self):
        token = self.token_for(self.parent)
        del self.policies[self.parent.pk]
        response = self.confirm(token)
        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.reload(self.parent).status, User.Status.ACTIVE)
        self.assertTrue(AccountClosureLink.objects.get(user=self.parent).used_at is None)

    def test_the_confirmation_is_throttled(self):
        cache.clear()
        codes = [self.confirm("ac_nope").status_code for _ in range(12)]
        self.assertIn(429, codes)


@override_settings(SCHOOL_APP_BASE_URL="https://xvs.example.test")
class TheEmailTests(ClosureFixture):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        call_command("seed_notification_templates", verbosity=0)

    def mail_for(self, user):
        from rest_framework.test import APIClient

        mail.outbox.clear()
        with self.captureOnCommitCallbacks(execute=True):
            APIClient().post(
                LINK_REQUEST, {"email": user.email, "tenant": "meridian"}, format="json",
            )
        return list(mail.outbox)

    def test_the_link_points_at_the_tenants_own_app_and_carries_the_token(self):
        sent = self.mail_for(self.parent)
        self.assertEqual(len(sent), 1)
        body = sent[0].body
        self.assertIn("https://meridian.xvs.example.test/account/delete?token=ac_", body)
        self.assertNotIn("__XVS_ACCOUNT_CLOSURE_TOKEN__", body)
        self.assertIn("closes straight away", body)
        self.assertEqual(sent[0].to, [self.parent.email])

    def test_the_stored_notification_never_holds_the_token(self):
        from vs_notifications.models import Notification

        sent = self.mail_for(self.parent)
        token = sent[0].body.split("token=")[1].split()[0]
        stored = Notification.objects.get(
            recipient=self.parent, event_type__key="user.account_closure_link",
        )
        self.assertNotIn(token, stored.body)
        self.assertNotIn(token, stored.html_body)

    def test_a_pupils_mail_says_the_school_decides(self):
        sent = self.mail_for(self.pupil)
        self.assertEqual(len(sent), 1)
        self.assertIn("goes to Meridian Academy", sent[0].body)
        self.assertNotIn("closes straight away", sent[0].body)

    def test_no_mail_leaves_for_an_account_that_cannot_delete(self):
        self.assertEqual(self.mail_for(self.staff), [])

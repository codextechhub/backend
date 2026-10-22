"""The platform tops up its provider balance from its own bank, with two approvals.

Codex pays N2,000,000 from its Zenith account into Paystack. Ngozi, a Codex
finance operator, records it and puts it forward; Chioma and Bola, Codex
approvers, approve it, and only Bola's approval books it: Dr provider balance,
Cr Zenith, in Codex's own books. Ngozi approving it herself, or Chioma approving
both steps, books nothing. The daily held-ledger check disagrees by N2,000,000
from the day Paystack receives the money until the top-up is booked, and agrees
after. Nobody at a school records, sees or approves a top-up.
"""
from __future__ import annotations

import datetime

from rest_framework.exceptions import ValidationError

from core.test_utils import TenantAPIClient
from vs_config.clock import tenant_today
from vs_finance.models import Account, JournalLine
from vs_rbac.tests.helpers import (
    install_declared_fields,
    make_branch,
    set_field_access,
)
from vs_workflow.constants import GroupMemberKind
from vs_workflow.constants import WorkflowInstanceStatus as InstanceStatus
from vs_workflow.constants import WorkflowStageAction as ActionEnum
from vs_workflow.exceptions import RequesterCannotApproveError
from vs_workflow.models import (
    WorkflowApproverGroup,
    WorkflowApproverGroupMember,
    WorkflowInstance,
    WorkflowStageAction,
)
from vs_workflow.services import actions as wf_actions

from . import held, held_reconciliation, topups
from .approvals import ensure_topup_approval_template
from .constants import (
    WF_SETTLEMENT_APPROVER_GROUP,
    PaymentAuditAction,
    ProviderTopUpStatus,
)
from .exceptions import PaymentStateError
from .models import PaymentEvent, ProviderBalanceTopUp
from .tests_custody_held import _HeldFixture

BASE = "/v1/payments/platform/provider-topups/"
TWO_MILLION = 200_000_000


class _TopUpFixture(_HeldFixture):
    """Codex's books with a Zenith account, an operator, a viewer and two approvers."""

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        codex = cls.platform.tenant
        cls.codex = codex
        cls.today = tenant_today(codex)
        cls.zenith = cls.bank(cls.platform, "1141", "Codex Zenith", cls.lagos, collection=False)
        cls.operator = cls.grant(
            cls.user_for(codex, "ngozi@codex.test"),
            "payments.platform_topup.view", "payments.platform_topup.create",
            "payments.platform_topup.submit", tenant=codex, role_key="topup-ops")
        cls.viewer = cls.grant(
            cls.user_for(codex, "viewer@codex.test"),
            "payments.platform_topup.view", tenant=codex, role_key="topup-viewer")
        cls.unkeyed = cls.user_for(codex, "nobody@codex.test")
        ensure_topup_approval_template(codex)
        group = WorkflowApproverGroup.all_objects.get(tenant=codex, code=WF_SETTLEMENT_APPROVER_GROUP)
        cls.group = group
        cls.approvers = []
        for email in ("chioma@codex.test", "bola@codex.test"):
            # Staff standing: a role with no keys, so the group seat is theirs.
            user = cls.grant(cls.user_for(codex, email), tenant=codex,
                             role_key=f"staff-{email.split('@')[0]}")
            WorkflowApproverGroupMember.objects.create(
                group=group, kind=GroupMemberKind.USER, user=user)
            cls.approvers.append(user)
        cls.bursar = cls.grant(
            cls.user_for(cls.tenant, "bursar@corona.test"),
            "payments.report.view", "payments.payout_batch.submit",
            tenant=cls.tenant, role_key="corona-bursar")
        cls.solo_user = cls.grant(
            cls.user_for(cls.solo_tenant, "solo@single.test"),
            "payments.report.view", tenant=cls.solo_tenant, role_key="solo-bursar")

    # -- helpers --------------------------------------------------------------- #

    def body(self, **overrides):
        data = {
            "amount": TWO_MILLION, "bank_account": self.zenith.pk,
            "topped_up_on": self.today.isoformat(), "reference": "PSK-TOPUP-1",
            "note": "Funds the platform's own payouts.",
        }
        data.update(overrides)
        return data

    def record(self, **overrides):
        response = TenantAPIClient(user=self.operator).post(BASE, self.body(**overrides), format="json")
        self.assertEqual(response.status_code, 201, response.data)
        return ProviderBalanceTopUp.objects.get(pk=response.data["data"]["id"])

    def submit(self, topup, user=None):
        response = TenantAPIClient(user=user or self.operator).post(
            f"{BASE}{topup.pk}/submit/", {}, format="json")
        self.assertEqual(response.status_code, 200, response.data)
        return WorkflowInstance.all_objects.for_document(topup).get()

    def book(self, topup):
        """Record's draft put forward and approved by both approvers; returns the booked row."""
        instance = self.submit(topup)
        for approver in self.approvers:
            wf_actions.record_action(instance.id, approver, ActionEnum.APPROVED)
        topup.refresh_from_db()
        return topup

    def platform_total(self, code):
        """The platform's debit balance on ``code``; nothing before the account exists."""
        account = Account.objects.filter(entity=self.platform, code=code).first()
        if account is None:
            return 0
        lines = JournalLine.objects.filter(account=account, entry__status__in=("POSTED", "REVERSED"))
        return sum(line.debit - line.credit for line in lines)


class RecordATopUpTests(_TopUpFixture):
    """A top-up is written down as a draft that books nothing."""

    def test_recording_one_makes_a_draft_in_the_platforms_books_and_books_nothing(self):
        books_before = self.platform_total("1127")

        topup = self.record()

        self.assertEqual(topup.status, ProviderTopUpStatus.DRAFT)
        self.assertEqual((topup.entity_id, topup.branch_id), (self.platform.pk, self.lagos.pk))
        self.assertEqual((topup.amount, topup.bank_account_id, topup.created_by_id),
                         (TWO_MILLION, self.zenith.pk, self.operator.pk))
        self.assertIsNone(topup.journal_id)
        self.assertEqual(self.platform_total("1127"), books_before)
        event = PaymentEvent.objects.get(action=PaymentAuditAction.PROVIDER_TOPUP_CREATED)
        self.assertEqual(event.metadata["amount"], TWO_MILLION)
        self.assertEqual(event.actor_user_id, self.operator.pk)

    def test_the_response_has_the_shape_the_screen_reads(self):
        response = TenantAPIClient(user=self.operator).post(BASE, self.body(), format="json")

        data = response.data["data"]
        self.assertEqual(data["status"], "DRAFT")
        self.assertEqual(data["amount"], TWO_MILLION)
        self.assertEqual(data["amount_naira"], "₦2,000,000.00")
        self.assertEqual((data["bank_account"], data["bank_account_name"]),
                         (self.zenith.pk, "Codex Zenith"))
        self.assertEqual((data["approval_state"], data["approval_returned"]), ("NOT_SUBMITTED", False))
        self.assertEqual((data["branch"], data["branch_name"]), (self.lagos.pk, self.lagos.name))
        self.assertEqual(data["workflow_instance_id"], None)
        self.assertIsNone(data["journal_id"])
        self.assertEqual(data["_read_only_fields"], [])

    def test_an_amount_must_be_positive(self):
        client = TenantAPIClient(user=self.operator)
        for amount in (0, -500, "lots"):
            with self.subTest(amount=amount):
                response = client.post(BASE, self.body(amount=amount), format="json")
                self.assertEqual(response.status_code, 400, response.data)
        self.assertFalse(ProviderBalanceTopUp.objects.exists())

    def test_the_amount_is_refused_by_the_service_too(self):
        draft = ProviderBalanceTopUp(
            entity=self.platform, branch=self.lagos, provider="PAYSTACK", amount=0,
            topped_up_on=self.today, bank_account=self.zenith)
        with self.assertRaises(ValidationError) as caught:
            topups.validate_topup(draft)
        self.assertIn("amount", caught.exception.detail)

    def test_the_bank_account_must_belong_to_the_platform_and_be_active(self):
        client = TenantAPIClient(user=self.operator)
        elsewhere = client.post(BASE, self.body(bank_account=self.lekki_bank.pk), format="json")
        self.assertEqual(elsewhere.status_code, 400, elsewhere.data)
        self.assertIn("bank_account", elsewhere.data["error"]["detail"])

        self.zenith.is_active = False
        self.zenith.save(update_fields=["is_active"])
        inactive = client.post(BASE, self.body(), format="json")
        self.assertEqual(inactive.status_code, 400, inactive.data)
        self.assertFalse(ProviderBalanceTopUp.objects.exists())

    def test_the_day_cannot_be_in_the_future(self):
        tomorrow = (self.today + datetime.timedelta(days=1)).isoformat()
        response = TenantAPIClient(user=self.operator).post(
            BASE, self.body(topped_up_on=tomorrow), format="json")
        self.assertEqual(response.status_code, 400, response.data)

    def test_the_same_transfer_is_recorded_once_unless_the_first_was_rejected(self):
        first = self.record()
        again = TenantAPIClient(user=self.operator).post(BASE, self.body(), format="json")
        self.assertEqual(again.status_code, 400, again.data)

        ProviderBalanceTopUp.objects.filter(pk=first.pk).update(status=ProviderTopUpStatus.REJECTED)
        self.record()
        self.assertEqual(ProviderBalanceTopUp.objects.count(), 2)

    def test_a_draft_can_be_corrected_until_it_is_put_forward(self):
        topup = self.record()
        client = TenantAPIClient(user=self.operator)

        response = client.patch(f"{BASE}{topup.pk}/", {"amount": 150_000_000, "note": "Corrected."},
                                format="json")
        self.assertEqual(response.status_code, 200, response.data)
        topup.refresh_from_db()
        self.assertEqual((topup.amount, topup.note), (150_000_000, "Corrected."))

        self.submit(topup)
        late = client.patch(f"{BASE}{topup.pk}/", {"amount": 1}, format="json")
        self.assertEqual(late.status_code, 409, late.data)

    def test_the_list_and_detail_read_what_was_recorded(self):
        first = self.record()
        self.record(reference="PSK-TOPUP-2", amount=100_000_000)
        client = TenantAPIClient(user=self.viewer)

        listed = client.get(BASE)
        self.assertEqual(listed.status_code, 200, listed.data)
        self.assertEqual([row["reference"] for row in listed.data["data"]],
                         ["PSK-TOPUP-2", "PSK-TOPUP-1"])
        none_posted = client.get(f"{BASE}?status=POSTED")
        self.assertEqual(none_posted.data["data"], [])
        self.assertEqual(client.get(f"{BASE}?status=NONSENSE").status_code, 400)
        detail = client.get(f"{BASE}{first.pk}/")
        self.assertEqual(detail.data["data"]["reference"], "PSK-TOPUP-1")
        self.assertEqual(client.get(f"{BASE}999999/").status_code, 404)


class TopUpAccessTests(_TopUpFixture):
    """Only Codex staff holding the keys reach top-ups, and no school ever does."""

    def test_school_users_of_a_multi_branch_and_a_single_branch_school_are_refused(self):
        topup = self.record()
        for user in (self.bursar, self.solo_user):
            client = TenantAPIClient(user=user)
            with self.subTest(school=user.tenant.slug):
                self.assertEqual(client.get(BASE).status_code, 403)
                self.assertEqual(client.post(BASE, self.body(), format="json").status_code, 403)
                self.assertEqual(client.get(f"{BASE}{topup.pk}/").status_code, 403)
                self.assertEqual(client.patch(f"{BASE}{topup.pk}/", {"note": "x"},
                                              format="json").status_code, 403)
                self.assertEqual(client.post(f"{BASE}{topup.pk}/submit/", {},
                                             format="json").status_code, 403)
        self.assertEqual(ProviderBalanceTopUp.objects.count(), 1)

    def test_a_platform_user_without_the_keys_is_refused(self):
        topup = self.record()
        client = TenantAPIClient(user=self.unkeyed)
        self.assertEqual(client.get(BASE).status_code, 403)
        self.assertEqual(client.post(BASE, self.body(reference="X"), format="json").status_code, 403)
        self.assertEqual(client.post(f"{BASE}{topup.pk}/submit/", {}, format="json").status_code, 403)

    def test_each_key_opens_only_its_own_action(self):
        topup = self.record()
        viewer = TenantAPIClient(user=self.viewer)
        self.assertEqual(viewer.get(BASE).status_code, 200)
        self.assertEqual(viewer.post(BASE, self.body(reference="Y"), format="json").status_code, 403)
        self.assertEqual(viewer.post(f"{BASE}{topup.pk}/submit/", {}, format="json").status_code, 403)

        creator = self.grant(self.user_for(self.codex, "creator@codex.test"),
                             "payments.platform_topup.create", tenant=self.codex, role_key="topup-creator")
        client = TenantAPIClient(user=creator)
        self.assertEqual(client.get(BASE).status_code, 403)
        self.assertEqual(client.post(f"{BASE}{topup.pk}/submit/", {}, format="json").status_code, 403)

    def test_a_platform_staff_member_bound_to_one_branch_reaches_only_that_branchs_top_ups(self):
        abuja = make_branch(self.codex, name="Abuja Branch", is_main=False)
        abuja_bank = self.bank(self.platform, "1142", "Codex Abuja GTB", abuja, collection=False)
        lagos_only = self.grant(
            self.user_for(self.codex, "lagos@codex.test"),
            "payments.platform_topup.view", "payments.platform_topup.create",
            "payments.platform_topup.submit", tenant=self.codex, role_key="lagos-ops",
            branch=self.lagos)
        everywhere = TenantAPIClient(user=self.operator)
        mine = self.record(reference="LAGOS-1")
        theirs = everywhere.post(BASE, self.body(reference="ABUJA-1", bank_account=abuja_bank.pk),
                                 format="json")
        self.assertEqual(theirs.status_code, 201, theirs.data)
        self.assertEqual(theirs.data["data"]["branch"], abuja.pk)

        client = TenantAPIClient(user=lagos_only)
        listed = client.get(BASE)
        self.assertEqual([row["reference"] for row in listed.data["data"]], ["LAGOS-1"])
        self.assertEqual(client.get(f"{BASE}{theirs.data['data']['id']}/").status_code, 404)
        self.assertEqual(client.post(f"{BASE}{theirs.data['data']['id']}/submit/", {},
                                     format="json").status_code, 404)
        refused = client.post(BASE, self.body(reference="ABUJA-2", bank_account=abuja_bank.pk),
                              format="json")
        self.assertEqual(refused.status_code, 403, refused.data)
        self.assertFalse(ProviderBalanceTopUp.objects.filter(reference="ABUJA-2").exists())
        self.assertEqual(client.get(f"{BASE}{mine.pk}/").status_code, 200)


class TopUpBankAccountPickerTests(_TopUpFixture):
    """The form's picker lists the platform's own active bank accounts and nobody else's."""

    URL = BASE + "bank-accounts/"

    def test_it_lists_the_platforms_active_accounts_without_account_numbers(self):
        inactive = self.bank(self.platform, "1143", "Codex Old", self.lagos, collection=False)
        inactive.is_active = False
        inactive.save(update_fields=["is_active"])

        response = TenantAPIClient(user=self.operator).get(self.URL)

        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["data"], [{
            "id": self.zenith.pk, "name": "Codex Zenith", "bank_name": "Codex Zenith",
            "branch_id": self.lagos.pk, "branch_name": self.lagos.name,
            "currency": self.platform.base_currency.code,
        }])

    def test_a_school_banks_are_never_listed(self):
        names = {row["name"] for row in TenantAPIClient(user=self.operator).get(self.URL).data["data"]}
        self.assertNotIn(self.lekki_bank.name, names)

    def test_school_users_and_platform_users_without_the_create_key_are_refused(self):
        for user in (self.bursar, self.solo_user, self.unkeyed, self.viewer):
            with self.subTest(user=user.email):
                self.assertEqual(TenantAPIClient(user=user).get(self.URL).status_code, 403)

    def test_a_branch_bound_caller_gets_only_their_branchs_accounts(self):
        abuja = make_branch(self.codex, name="Abuja Branch", is_main=False)
        self.bank(self.platform, "1142", "Codex Abuja GTB", abuja, collection=False)
        lagos_only = self.grant(
            self.user_for(self.codex, "lagos@codex.test"), "payments.platform_topup.create",
            tenant=self.codex, role_key="lagos-creator", branch=self.lagos)

        names = [row["name"] for row in TenantAPIClient(user=lagos_only).get(self.URL).data["data"]]

        self.assertEqual(names, ["Codex Zenith"])

    def test_a_role_that_may_not_read_the_bank_account_field_is_refused(self):
        from vs_rbac.models import TenantRoleTemplate

        install_declared_fields("payments.platform_topup")
        role = TenantRoleTemplate.objects.get(tenant=self.codex, key="topup-ops")
        set_field_access(role, "payments.platform_topup.bank_account", read=False, write=False)

        self.assertEqual(TenantAPIClient(user=self.operator).get(self.URL).status_code, 403)


class TopUpApprovalTests(_TopUpFixture):
    """Two distinct Codex approvers besides the requester book it, and nobody else can."""

    def test_the_second_approval_books_dr_provider_balance_cr_the_bank(self):
        topup = self.record()
        instance = self.submit(topup)
        self.assertEqual(instance.tenant_id, self.platform.tenant_id)
        self.assertEqual(instance.template.code, "provider-topup")
        self.assertEqual(instance.document_type, "payments.provider_topup")
        topup.refresh_from_db()
        self.assertEqual(topup.status, ProviderTopUpStatus.PENDING_APPROVAL)

        first, second = self.approvers
        wf_actions.record_action(instance.id, first, ActionEnum.APPROVED)
        topup.refresh_from_db()
        self.assertEqual((topup.status, topup.journal_id), (ProviderTopUpStatus.PENDING_APPROVAL, None))
        self.assertEqual(self.platform_total("1127"), 0)

        wf_actions.record_action(instance.id, second, ActionEnum.APPROVED)

        topup.refresh_from_db()
        self.assertEqual(topup.status, ProviderTopUpStatus.POSTED)
        self.assertIsNotNone(topup.posted_at)
        entry = topup.journal
        self.assertEqual((entry.status, entry.date, entry.branch_id, entry.entity_id),
                         ("POSTED", self.today, self.lagos.pk, self.platform.pk))
        lines = {(line.account.code, line.debit, line.credit) for line in entry.lines.select_related("account")}
        self.assertEqual(lines, {("1127", TWO_MILLION, 0), (self.zenith.gl_account.code, 0, TWO_MILLION)})
        self.assertEqual(self.platform_total("1127"), TWO_MILLION)
        self.assertEqual(self.balance(self.zenith.gl_account), -TWO_MILLION)
        event = PaymentEvent.objects.get(action=PaymentAuditAction.PROVIDER_TOPUP_POSTED)
        self.assertEqual((event.actor_user_id, event.metadata["amount"], event.metadata["journal_id"]),
                         (second.pk, TWO_MILLION, entry.pk))

    def test_the_booking_takes_the_day_of_the_top_up(self):
        earlier = self.today - datetime.timedelta(days=3)
        if earlier.month != self.today.month:
            earlier = self.today.replace(day=1)
        topup = self.book(self.record(topped_up_on=earlier.isoformat()))
        self.assertEqual(topup.journal.date, earlier)

    def test_the_person_who_put_it_forward_cannot_approve_it(self):
        topup = self.record()
        instance = self.submit(topup)
        WorkflowApproverGroupMember.objects.create(
            group=self.group, kind=GroupMemberKind.USER, user=self.operator)

        with self.assertRaises(RequesterCannotApproveError):
            wf_actions.record_action(instance.id, self.operator, ActionEnum.APPROVED)

        topup.refresh_from_db()
        self.assertEqual(topup.status, ProviderTopUpStatus.PENDING_APPROVAL)

    def test_one_person_approving_both_steps_books_nothing(self):
        topup = self.record()
        instance = self.submit(topup)
        chioma = self.approvers[0]
        wf_actions.record_action(instance.id, chioma, ActionEnum.APPROVED)

        with self.assertRaises(topups.TopUpApprovalError):
            wf_actions.record_action(instance.id, chioma, ActionEnum.APPROVED)

        topup.refresh_from_db()
        self.assertEqual((topup.status, topup.journal_id), (ProviderTopUpStatus.PENDING_APPROVAL, None))
        self.assertEqual(self.platform_total("1127"), 0)

    def test_a_proxied_vote_counts_for_the_person_at_the_keyboard(self):
        """Ngozi voting as Chioma and as Bola is one person, and she is the requester."""
        topup = self.record()
        instance = self.submit(topup)
        stage_instance = instance.stage_instances.order_by("pk").first()
        for approver in self.approvers:
            WorkflowStageAction.objects.create(
                stage_instance=stage_instance, actor=approver, proxied_by=self.operator,
                action=ActionEnum.APPROVED, attempt=stage_instance.attempt)
        self.assertEqual(topups.distinct_approvers(instance), 0)

        tola = self.user_for(self.codex, "tola@codex.test")
        WorkflowStageAction.objects.create(
            stage_instance=stage_instance, actor=tola, action=ActionEnum.APPROVED,
            attempt=stage_instance.attempt)
        self.assertEqual(topups.distinct_approvers(instance), 1)
        with self.assertRaises(topups.TopUpApprovalError):
            topups.require_distinct_approvers(instance)

    def test_a_rejection_ends_it_without_booking_and_frees_the_reference(self):
        topup = self.record()
        instance = self.submit(topup)

        wf_actions.record_action(instance.id, self.approvers[0], ActionEnum.REJECTED, comment="Not ours.")

        topup.refresh_from_db()
        self.assertEqual((topup.status, topup.journal_id), (ProviderTopUpStatus.REJECTED, None))
        self.assertEqual(self.platform_total("1127"), 0)
        self.assertTrue(PaymentEvent.objects.filter(
            action=PaymentAuditAction.PROVIDER_TOPUP_REJECTED, actor_user=self.approvers[0]).exists())
        self.record()  # The reference is free again.

    def test_putting_it_forward_twice_returns_the_same_approval(self):
        topup = self.record()
        first = self.submit(topup)
        again = self.submit(topup)
        self.assertEqual(first.pk, again.pk)
        self.assertEqual(WorkflowInstance.all_objects.for_document(topup).count(), 1)

    def test_a_booked_top_up_cannot_be_put_forward_again(self):
        topup = self.book(self.record())
        response = TenantAPIClient(user=self.operator).post(
            f"{BASE}{topup.pk}/submit/", {}, format="json")
        self.assertEqual(response.status_code, 409, response.data)

    def test_a_day_with_no_open_period_is_refused_before_anyone_is_asked(self):
        topup = self.record(topped_up_on=datetime.date(2001, 1, 15).isoformat())
        response = TenantAPIClient(user=self.operator).post(
            f"{BASE}{topup.pk}/submit/", {}, format="json")
        self.assertGreaterEqual(response.status_code, 400)
        topup.refresh_from_db()
        self.assertEqual(topup.status, ProviderTopUpStatus.DRAFT)
        self.assertFalse(WorkflowInstance.all_objects.for_document(topup).exists())

    def test_a_top_up_cannot_be_booked_without_going_through_approval(self):
        topup = self.record()
        with self.assertRaises(PaymentStateError):
            topups.post_topup(topup, actor_user=self.operator)
        self.assertIsNone(ProviderBalanceTopUp.objects.get(pk=topup.pk).journal_id)

    def test_an_unstaffed_route_parks_the_top_up_rather_than_booking_it(self):
        WorkflowApproverGroupMember.objects.filter(group=self.group).delete()
        topup = self.record()

        instance = self.submit(topup)

        topup.refresh_from_db()
        self.assertEqual(topup.status, ProviderTopUpStatus.PENDING_APPROVAL)
        self.assertNotEqual(instance.status, InstanceStatus.APPROVED)
        self.assertIsNone(topup.journal_id)


class TopUpReconciliationTests(_TopUpFixture):
    """The daily check disagrees by a top-up until it is booked, then agrees."""

    def setUp(self):
        super().setUp()
        from unittest.mock import patch

        notify = patch.object(held_reconciliation, "_notify_operators", return_value=1)
        notify.start()
        self.addCleanup(notify.stop)

    def test_the_check_disagrees_by_the_top_up_before_it_is_booked_and_agrees_after(self):
        self.paid(self.books, self.adeyemi, 180_000, fee=2_000)
        self.fake.balances["NGN"] = 178_000
        self.assertTrue(held_reconciliation.reconcile_held_ledger().agrees)

        self.fake.balances["NGN"] = 178_000 + TWO_MILLION  # Paystack has the money.
        topup = self.record()
        before = held_reconciliation.reconcile_held_ledger()
        self.assertEqual((before.difference, before.agrees, before.topped_up),
                         (TWO_MILLION, False, 0))

        self.book(topup)
        after = held_reconciliation.reconcile_held_ledger()

        self.assertEqual(
            (after.provider_balance, after.books_balance, after.difference, after.agrees),
            (178_000 + TWO_MILLION, 178_000 + TWO_MILLION, 0, True))
        self.assertEqual((after.topped_up, after.provider_account, after.held_total),
                         (TWO_MILLION, 178_000 + TWO_MILLION, 178_000))

    def test_a_booked_top_up_paystack_never_received_is_a_disagreement(self):
        self.paid(self.books, self.adeyemi, 180_000, fee=2_000)
        self.fake.balances["NGN"] = 178_000
        self.book(self.record())

        row = held_reconciliation.reconcile_held_ledger()

        self.assertEqual((row.difference, row.agrees), (-TWO_MILLION, False))

    def test_the_platforms_own_share_follows_the_real_balance_and_the_check_agrees(self):
        """After a top-up the platform may spend it; schools' money stays untouched."""
        self.paid(self.books, self.adeyemi, 180_000, fee=2_000)
        self.fake.balances["NGN"] = 178_000 + TWO_MILLION
        self.book(self.record())

        self.assertEqual(held.own_share_available(self.fake), TWO_MILLION)
        row = held_reconciliation.reconcile_held_ledger()
        self.assertTrue(row.agrees)
        self.assertEqual(held.held_balance(self.lekki.pk), 178_000)

    def test_a_top_up_whose_journal_was_reversed_no_longer_counts(self):
        from vs_finance.models import JournalEntry

        topup = self.book(self.record())
        self.assertEqual(topups.topped_up_total("PAYSTACK"), TWO_MILLION)
        JournalEntry.objects.filter(pk=topup.journal_id).update(status="REVERSED")
        self.assertEqual(topups.topped_up_total("PAYSTACK"), 0)

    def test_the_platform_staff_list_of_checks_carries_the_top_up(self):
        self.fake.balances["NGN"] = TWO_MILLION
        self.book(self.record())
        held_reconciliation.reconcile_held_ledger()
        staff = self.grant(self.user_for(self.codex, "recon@codex.test"),
                           "payments.platform_settlement.view", tenant=self.codex, role_key="recon")

        response = TenantAPIClient(user=staff).get("/v1/payments/platform/held-reconciliations/")

        self.assertEqual(response.status_code, 200, response.data)
        row = response.data["data"][0]
        self.assertEqual((row["topped_up"], row["agrees"]), (TWO_MILLION, True))


class TopUpFieldAccessTests(_TopUpFixture):
    """The registered fields of a top-up follow the caller's switches, and open by default."""

    KEYS = ["payments.platform_topup." + name for name in (
        "amount", "topped_up_on", "bank_account", "provider", "reference", "note", "status",
        "journal", "created_by")]

    def role_of(self, user):
        from vs_rbac.models import TenantRoleTemplate

        return TenantRoleTemplate.objects.get(tenant=self.codex, key="topup-ops")

    def test_every_field_is_open_to_a_role_with_no_switches(self):
        install_declared_fields("payments.platform_topup")
        topup = self.record()

        data = TenantAPIClient(user=self.operator).get(f"{BASE}{topup.pk}/").data["data"]

        for name in ("amount", "amount_naira", "topped_up_on", "bank_account", "bank_account_name",
                     "provider", "reference", "note", "status", "approval_state",
                     "approval_returned", "journal_id", "journal_number", "posted_at",
                     "created_by", "created_by_name"):
            with self.subTest(name=name):
                self.assertIn(name, data)

    def test_a_field_the_role_may_not_read_is_absent_whatever_name_it_travels_under(self):
        install_declared_fields("payments.platform_topup")
        topup = self.record()
        set_field_access(self.role_of(self.operator), *self.KEYS[:6], read=False, write=False)
        client = TenantAPIClient(user=self.operator)

        detail = client.get(f"{BASE}{topup.pk}/").data["data"]
        listed = client.get(BASE).data["data"][0]
        submitted = client.post(f"{BASE}{topup.pk}/submit/", {}, format="json").data["data"]

        hidden = ("amount", "amount_naira", "topped_up_on", "bank_account", "bank_account_name",
                  "provider", "reference", "note")
        for payload in (detail, listed, submitted):
            for name in hidden:
                self.assertNotIn(name, payload)
        for name in ("status", "approval_state", "approval"):
            self.assertIn(name, submitted if name == "approval" else detail)

    def test_the_approval_block_is_withheld_from_a_role_that_may_not_read_the_status(self):
        install_declared_fields("payments.platform_topup")
        topup = self.record()
        set_field_access(self.role_of(self.operator), self.KEYS[6], read=False, write=False)

        data = TenantAPIClient(user=self.operator).post(
            f"{BASE}{topup.pk}/submit/", {}, format="json").data["data"]

        for name in ("status", "approval_state", "approval_returned", "approval"):
            self.assertNotIn(name, data)

    def test_a_field_the_role_may_not_write_is_refused_on_correction_but_free_on_create(self):
        install_declared_fields("payments.platform_topup")
        set_field_access(self.role_of(self.operator), self.KEYS[0], read=True, write=False)
        client = TenantAPIClient(user=self.operator)

        created = client.post(BASE, self.body(), format="json")
        self.assertEqual(created.status_code, 201, created.data)
        topup_id = created.data["data"]["id"]
        self.assertIn("amount", created.data["data"]["_read_only_fields"])

        refused = client.patch(f"{BASE}{topup_id}/", {"amount": 1_000}, format="json")
        self.assertEqual(refused.status_code, 403, refused.data)
        self.assertEqual(ProviderBalanceTopUp.objects.get(pk=topup_id).amount, TWO_MILLION)
        allowed = client.patch(f"{BASE}{topup_id}/", {"note": "Fine."}, format="json")
        self.assertEqual(allowed.status_code, 200, allowed.data)

    def test_the_server_owned_fields_can_never_be_written(self):
        topup = self.record()
        response = TenantAPIClient(user=self.operator).patch(
            f"{BASE}{topup.pk}/", {"status": "POSTED", "journal_id": 1, "created_by": self.viewer.pk},
            format="json")
        self.assertEqual(response.status_code, 200, response.data)
        topup.refresh_from_db()
        self.assertEqual((topup.status, topup.journal_id, topup.created_by_id),
                         (ProviderTopUpStatus.DRAFT, None, self.operator.pk))

"""Finance names people by their names, not their email addresses.

Ngozi Ibekwe keeps Lagoon View's petty cash and posts its journals. The petty
cash screen and the finance audit trail both read "Ngozi Ibekwe"; the audit
trail keeps her email beside the name so an auditor can tell two people of one
name apart. Somebody with no name on their account still reads as their email.
"""
from django.contrib.auth import get_user_model
from django.test import TestCase

from vs_finance.models import FinanceAuditLog, PettyCashFund
from vs_finance.serializers import FinanceAuditLogSerializer, PettyCashFundSerializer
from vs_workflow.tests.test_actions import _platform_tenant


class PeopleNamedTests(TestCase):
    def setUp(self):
        users = get_user_model().objects
        self.ngozi = users.create_user(
            email="ngozi.named@test.com", tenant=_platform_tenant(), first_name="Ngozi", last_name="Ibekwe",
        )
        self.nameless = users.create_user(
            email="nameless@test.com", tenant=_platform_tenant(), first_name="Placeholder", last_name="Name",
        )
        users.filter(pk=self.nameless.pk).update(first_name="", last_name="")
        self.nameless.refresh_from_db()

    def test_a_user_answers_for_their_full_name(self):
        self.assertEqual(self.ngozi.get_full_name(), "Ngozi Ibekwe")
        self.assertEqual(self.ngozi.get_short_name(), "Ngozi")

    def test_the_petty_cash_keeper_is_named(self):
        fund = PettyCashFund(custodian=self.ngozi)
        self.assertEqual(PettyCashFundSerializer().get_custodian_label(fund), "Ngozi Ibekwe")

    def test_the_audit_trail_names_the_actor_and_keeps_the_email(self):
        log = FinanceAuditLog(actor=self.ngozi)
        serializer = FinanceAuditLogSerializer()
        self.assertEqual(serializer.get_actor_name(log), "Ngozi Ibekwe")
        self.assertEqual(serializer.fields["actor"].to_representation(log.actor.email), "ngozi.named@test.com")

    def test_somebody_with_no_name_reads_as_their_email(self):
        self.assertEqual(FinanceAuditLogSerializer().get_actor_name(FinanceAuditLog(actor=self.nameless)), "nameless@test.com")
        self.assertIsNone(FinanceAuditLogSerializer().get_actor_name(FinanceAuditLog()))

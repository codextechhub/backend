"""What a bursar reads is worded from the screens, never from the code.

A refusal names a month as "September 2026", not ``2026-09 [CLOSED]``; a close
check as "Payables agree with the ledger", not ``ap_reconciled``; a status as
"posted", not ``POSTED``; a note as "Credit note", not "Credit note (reduces
AR)". The codes stay in the error payload, where a screen keys on them. And the
held-ledger tolerance an operator types is naira, as every amount shown is.
"""
from __future__ import annotations

from unittest import mock

from django.apps import apps as django_apps
from django.test import SimpleTestCase, TestCase

from .close import ChecklistItem, failures_sentence
from .constants import CreditNoteKind, DocumentStatus
from .wording import code_words, state_word, words_for_code


class CloseChecksReadInWordsTests(SimpleTestCase):

    def test_a_failed_check_is_named_by_its_title_and_what_it_found(self):
        items = [
            ChecklistItem(name="ap_reconciled", title="Payables agree with the ledger",
                          passed=False, detail="Sub-ledger ₦10.00 against control ₦12.00."),
            ChecklistItem(name="trial_balance_balanced", passed=False,
                          detail="Debits and credits differ by ₦2.00."),
        ]

        sentence = failures_sentence(items)

        self.assertEqual(
            sentence,
            "Payables agree with the ledger: Sub-ledger ₦10.00 against control ₦12.00; "
            "Trial balance balances: Debits and credits differ by ₦2.00",
        )
        self.assertNotIn("_", sentence)

    def test_a_check_with_no_title_still_reads_as_words(self):
        self.assertEqual(ChecklistItem(name="some_new_check", passed=True).label,
                         "Some new check")


class StatusesReadInWordsTests(SimpleTestCase):

    def test_a_status_reads_as_its_label(self):
        class Doc:
            status = DocumentStatus.PENDING_APPROVAL

            def get_status_display(self):
                return DocumentStatus(self.status).label

        self.assertEqual(state_word(Doc()), "pending approval")
        self.assertEqual(code_words(DocumentStatus, "POSTED"), "posted")
        self.assertEqual(words_for_code("PENDING_APPROVAL"), "pending approval")

    def test_a_note_kind_carries_no_accounting_note(self):
        self.assertEqual(CreditNoteKind.CREDIT.label, "Credit note")
        self.assertEqual(CreditNoteKind.DEBIT.label, "Debit note")


class HeldToleranceIsInNairaTests(TestCase):

    def test_the_tolerance_typed_in_naira_is_compared_in_kobo(self):
        from vs_payments import held_reconciliation

        with mock.patch("vs_config.conf.get_config", return_value=250):
            self.assertEqual(held_reconciliation.tolerance(), 25_000)
        self.assertEqual(held_reconciliation.TOLERANCE_KEY, "payments.held_reconciliation_tolerance")

    def test_moving_the_setting_to_naira_keeps_every_value_set_and_never_tightens_it(self):
        import importlib

        from vs_config.models import ConfigurationDefinition, ConfigurationValue

        migration = importlib.import_module("vs_config.migrations.0016_held_tolerance_in_naira")
        ConfigurationDefinition.objects.filter(key__startswith="payments.held_reconciliation").delete()
        definition = ConfigurationDefinition.objects.create(
            key=migration.OLD_KEY, label=migration.LABEL, description=migration.KOBO_DESCRIPTION,
            value_type="INTEGER", default_value=0, validation_rules={"min": 0, "max": 100_000_000},
            allowed_scopes=["platform"],
        )
        ConfigurationValue.all_objects.create(definition=definition, value=150, scope_key="platform")

        migration.forwards(django_apps, None)

        definition.refresh_from_db()
        self.assertEqual(definition.key, migration.NEW_KEY)
        self.assertNotIn("Kobo", definition.description)
        self.assertIn("Naira", definition.description)
        self.assertEqual(ConfigurationValue.all_objects.get(definition=definition).value, 2)

        migration.backwards(django_apps, None)

        definition.refresh_from_db()
        self.assertEqual(definition.key, migration.OLD_KEY)
        self.assertEqual(ConfigurationValue.all_objects.get(definition=definition).value, 200)

    def test_the_catalogue_says_naira_to_a_person(self):
        from vs_config.management.commands.seed_config_catalogue import (
            DEFINITIONS, SCHOOL_SCOPED_DEFINITIONS,
        )

        for key, label, description, *_ in DEFINITIONS + SCHOOL_SCOPED_DEFINITIONS:
            with self.subTest(key=key):
                self.assertNotIn("kobo", f"{label} {description}".lower())

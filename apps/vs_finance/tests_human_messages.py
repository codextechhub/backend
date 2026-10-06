"""What a bursar reads is worded from the screens, never from the code.

A refusal names a month as "September 2026", not ``2026-09 [CLOSED]``; a close
check as "AP reconciled (what is owed to suppliers)", not ``ap_reconciled``: the
checklist is an accountant's screen, so the term comes with its plain words, while
what each check found is said in plain words alone. A status reads as
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
from .wording import agrees, code_words, counted, state_word, words_for_code


class CloseChecksReadInWordsTests(SimpleTestCase):

    def test_a_failed_check_is_named_by_its_title_and_what_it_found(self):
        items = [
            ChecklistItem(name="ap_reconciled", title="AP reconciled (what is owed to suppliers)",
                          passed=False, detail="Sub-ledger ₦10.00 against control ₦12.00."),
            ChecklistItem(name="trial_balance_balanced", passed=False,
                          detail="Debits and credits differ by ₦2.00."),
        ]

        sentence = failures_sentence(items)

        self.assertEqual(
            sentence,
            "AP reconciled (what is owed to suppliers): Sub-ledger ₦10.00 against control "
            "₦12.00; Trial balance agrees (debits equal credits): Debits and credits differ "
            "by ₦2.00",
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


#: Accounting terms a bursar has no reason to know: a check title may name one
#: only with its plain words beside it, and what a check found never does.
CLOSE_JARGON = ("GR/IR", "sub-ledger", "control ", "clearing", "checksum", "trial balance",
                "AR ", "AP ", "deferred income", "inter-branch")


class CloseChecklistSpeaksPlainlyTests(SimpleTestCase):

    def assert_plain(self, text):
        for word in CLOSE_JARGON:
            self.assertNotIn(word.lower(), text.lower())

    def assert_paired(self, title):
        """A title is plain words, or a term followed by its plain words in brackets."""
        term, _, gloss = title.partition(" (")
        if gloss:
            self.assertTrue(gloss.endswith(")"), title)
            self.assert_plain(gloss)
        else:
            self.assert_plain(term)

    def test_finance_check_titles_pair_each_term_with_plain_words(self):
        from .close import CHECK_TITLES

        for name, title in CHECK_TITLES.items():
            with self.subTest(check=name):
                self.assert_paired(title)
        self.assertEqual(CHECK_TITLES["ar_reconciled"], "AR reconciled (what customers owe)")

    def test_contributed_check_titles_pair_each_term_with_plain_words(self):
        from types import SimpleNamespace

        from vs_procurement import close_checks

        ap = SimpleNamespace(is_reconciled=True, subledger_total=0, control_total=0)
        with mock.patch("vs_procurement.reports.reconcile_ap", return_value=ap), \
                mock.patch("vs_procurement.reports.grir_balance", return_value=0), \
                mock.patch("vs_procurement.models.Vendor.objects") as vendors:
            vendors.filter.return_value.exists.return_value = True
            ap_item = close_checks.ap_reconciled(object(), object())
            grir_item = close_checks.grir_explained(object(), object())

        self.assertEqual(ap_item.label, "AP reconciled (what is owed to suppliers)")
        self.assertEqual(grir_item.label, "GR/IR explained (goods received, not yet billed)")
        for item in (ap_item, grir_item):
            self.assert_paired(item.label)

    def test_goods_received_but_not_billed_reads_in_plain_words(self):
        from vs_procurement import close_checks

        details = {}
        for balance in (0, 1_500_000, -250_000):
            with mock.patch("vs_procurement.reports.grir_balance", return_value=balance), \
                    mock.patch("vs_procurement.models.Vendor.objects") as vendors:
                vendors.filter.return_value.exists.return_value = True
                item = close_checks.grir_explained(object(), object())
            self.assertEqual(item.label, "GR/IR explained (goods received, not yet billed)")
            self.assert_plain(item.detail)
            details[balance] = item.detail
        self.assertIn("₦15,000.00", details[1_500_000])
        self.assertIn("₦2,500.00", details[-250_000])

    def test_the_ledger_agreement_checks_read_in_plain_words(self):
        from types import SimpleNamespace

        from vs_procurement import close_checks

        ap = SimpleNamespace(is_reconciled=False, subledger_total=1_000, control_total=1_200)
        with mock.patch("vs_procurement.reports.reconcile_ap", return_value=ap), \
                mock.patch("vs_procurement.models.Vendor.objects") as vendors:
            vendors.filter.return_value.exists.return_value = True
            item = close_checks.ap_reconciled(object(), object())
        self.assert_plain(item.detail)
        self.assertIn("₦10.00", item.detail)
        self.assertIn("₦12.00", item.detail)


class CountsReadAsWordsTests(SimpleTestCase):

    def test_a_count_and_its_noun_agree(self):
        self.assertEqual(counted(1, "depreciation charge"), "1 depreciation charge")
        self.assertEqual(counted(3, "depreciation charge"), "3 depreciation charges")
        self.assertEqual(counted(0, "row"), "0 rows")
        self.assertEqual(counted(2, "person", "people"), "2 people")
        self.assertEqual(counted(1_250, "invoice"), "1,250 invoices")
        self.assertEqual(agrees(1, "is", "are"), "is")
        self.assertEqual(agrees(2, "is", "are"), "are")

    def test_no_finance_procurement_or_payments_sentence_hedges_a_plural(self):
        import pathlib
        import re

        root = pathlib.Path(__file__).resolve().parent.parent
        hedged = []
        for app in ("vs_finance", "vs_procurement", "vs_payments"):
            for path in (root / app).rglob("*.py"):
                if "migrations" in path.parts or path.name.startswith("test"):
                    continue
                for number, line in enumerate(path.read_text().splitlines(), 1):
                    if re.search(r'f["\'].*[a-z}]\((e?s)\)', line):
                        hedged.append(f"{path.relative_to(root)}:{number}")
        self.assertEqual(hedged, [])


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

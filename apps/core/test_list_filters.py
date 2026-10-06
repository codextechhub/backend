"""A list's word filter reads any case and refuses a word it does not have."""
from __future__ import annotations

from unittest import mock

from django.db.models import Q
from django.test import SimpleTestCase
from rest_framework.exceptions import ValidationError

from core.list_filters import filter_by_word, word_value


class WordValueTests(SimpleTestCase):

    def test_a_word_is_matched_in_any_case_and_answered_in_the_lists_own(self):
        self.assertEqual(word_value(" draft ", ("DRAFT", "PAID")), "DRAFT")
        self.assertEqual(word_value("Overdue", ("draft", "overdue")), "overdue")
        self.assertIsNone(word_value("", ("DRAFT",)))
        self.assertIsNone(word_value(None, ("DRAFT",)))

    def test_a_word_the_list_does_not_have_is_refused_naming_the_words_it_takes(self):
        with self.assertRaises(ValidationError) as refused:
            word_value("VOIDED", ("DRAFT", "PAID"), param="display_status")
        self.assertEqual(
            str(refused.exception.detail["display_status"]), "Use one of DRAFT, PAID.",
        )


class FilterByWordTests(SimpleTestCase):

    def test_a_word_applies_its_rule_and_no_word_leaves_the_rows_alone(self):
        qs = mock.MagicMock()
        rules = {"PAID": Q(payment_status="PAID"), "OPEN": lambda rows: rows.exclude(pk=1)}

        self.assertIs(filter_by_word(qs, "", rules), qs)
        filter_by_word(qs, "paid", rules)
        qs.filter.assert_called_once_with(rules["PAID"])
        filter_by_word(qs, "Open", rules)
        qs.exclude.assert_called_once_with(pk=1)
        with self.assertRaises(ValidationError):
            filter_by_word(qs, "nonsense", rules)

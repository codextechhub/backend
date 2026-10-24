"""The side of a customer's account balance is written in words, not by a sign."""
from django.test import SimpleTestCase

from vs_finance.money import account_balance_words


class AccountBalanceWordsTests(SimpleTestCase):
    """Tunde owes 100,000; Ada paid ahead and is 20,000 in credit; Bola is square."""

    def test_a_positive_balance_is_owing(self):
        self.assertEqual(account_balance_words(10_000_000), "₦100,000.00 owing")

    def test_a_negative_balance_is_in_credit_without_a_minus_sign(self):
        self.assertEqual(account_balance_words(-2_000_000), "₦20,000.00 in credit")

    def test_zero_is_the_bare_amount(self):
        self.assertEqual(account_balance_words(0), "₦0.00")

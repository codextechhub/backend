"""Validate live payment-provider credentials end-to-end, without touching the ledger.

This talks to the **real** provider API (use Paystack *test mode* keys) to prove your
keys, base URL and network path actually work before you wire
the UI or go live. It deliberately does NOT create any ``CollectionIntent`` / ``Payment``
rows — it is a pure connectivity check against the provider, so it is safe to run against
a production database.

Two modes:

* **initialize** (default): create a hosted checkout and print the ``checkout_url`` you
  can open in a browser to complete a test payment. Prints the merchant ``reference`` so
  you can verify it afterwards.
* **verify** (``--verify <reference>``): poll the provider for the status of a reference
  produced by an earlier run (e.g. after you paid the test checkout).

Usage::

    manage.py payments_smoketest                          # default provider, ₦100 checkout
    manage.py payments_smoketest --provider PAYSTACK --amount 50000 --email you@test.com
    manage.py payments_smoketest --verify CXP-ABC123...   # check a reference's status

Money is in **kobo** (integer minor units) — ``--amount 10000`` == ₦100.00. Never pass a
float. Keys are read from settings (``PAYSTACK_SECRET_KEY`` etc.); nothing is hard-coded.
"""
from __future__ import annotations

import json

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from vs_payments.constants import DEFAULT_CURRENCY
from vs_payments.exceptions import ProviderError, ProviderNotConfiguredError
from vs_payments.providers import registry
from vs_payments.services import _new_reference


class Command(BaseCommand):
    help = (
        "Validate live payment-provider credentials by creating (or verifying) a test "
        "checkout against the real provider API. Does not touch the ledger."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--provider",
            default=None,
            help="Provider name (PAYSTACK). Defaults to PAYMENTS_DEFAULT_PROVIDER.",
        )
        parser.add_argument(
            "--amount",
            type=int,
            default=10_000,
            help="Amount in kobo (integer). Default 10000 = ₦100.00.",
        )
        parser.add_argument(
            "--currency",
            default=DEFAULT_CURRENCY,
            help=f"ISO currency code. Default {DEFAULT_CURRENCY}.",
        )
        parser.add_argument(
            "--email",
            default="smoketest@codexng.com",
            help="Customer email for the checkout (test value is fine).",
        )
        parser.add_argument(
            "--name",
            default="Smoke Test",
            help="Customer name for the checkout.",
        )
        parser.add_argument(
            "--narration",
            default="vs_payments smoke test",
            help="Narration / description shown on the checkout.",
        )
        parser.add_argument(
            "--verify",
            dest="verify_reference",
            default=None,
            help="Instead of creating a checkout, verify the status of this reference.",
        )

    def handle(self, *args, **options):
        provider_name = (options["provider"] or "").upper() or None

        try:
            provider = registry.get_provider(provider_name)
        except ProviderNotConfiguredError as exc:
            raise CommandError(
                f"{exc.message}\n"
                "Set the provider keys in your environment/settings before running this "
                "(e.g. PAYSTACK_SECRET_KEY=sk_test_...). See the credential-sourcing guide."
            )

        resolved = provider_name or getattr(settings, "PAYMENTS_DEFAULT_PROVIDER", "PAYSTACK").upper()
        self.stdout.write(self.style.MIGRATE_HEADING(f"Provider: {provider.name or resolved}"))

        if options["verify_reference"]:
            self._verify(provider, options["verify_reference"])
        else:
            self._initialize(provider, options)

    # -- modes -------------------------------------------------------------- #
    def _initialize(self, provider, options):
        amount = options["amount"]
        if amount <= 0:
            raise CommandError("--amount must be a positive integer (kobo).")
        reference = _new_reference()
        callback_url = getattr(settings, "PAYMENTS_CALLBACK_URL", "")

        self.stdout.write(f"Initializing checkout for reference {reference} …")
        try:
            result = provider.create_checkout(
                reference=reference,
                amount=amount,
                currency=options["currency"],
                customer_email=options["email"],
                customer_name=options["name"],
                narration=options["narration"],
                callback_url=callback_url,
            )
        except ProviderError as exc:
            self._fail_provider(exc)

        naira = amount / 100
        self.stdout.write(self.style.SUCCESS("Checkout created — your credentials work."))
        self.stdout.write(f"  reference          : {result.reference}")
        self.stdout.write(f"  provider_reference : {result.provider_reference or '(none)'}")
        self.stdout.write(f"  amount             : {amount} kobo (₦{naira:,.2f} {options['currency']})")
        self.stdout.write(f"  status             : {result.status}")
        self.stdout.write(self.style.HTTP_INFO(f"  checkout_url       : {result.checkout_url or '(none)'}"))
        self.stdout.write(
            "\nOpen the checkout_url in a browser to complete a test payment, then run:\n"
            f"  manage.py payments_smoketest --provider {provider.name or ''} "
            f"--verify {result.reference}"
        )

    def _verify(self, provider, reference):
        self.stdout.write(f"Verifying reference {reference} …")
        try:
            result = provider.verify_collection(reference=reference)
        except ProviderError as exc:
            self._fail_provider(exc)

        style = self.style.SUCCESS if result.paid else self.style.WARNING
        self.stdout.write(style(f"Status: {result.status}"))
        self.stdout.write(f"  reference          : {result.reference}")
        self.stdout.write(f"  provider_reference : {result.provider_reference or '(none)'}")
        self.stdout.write(f"  amount             : {result.amount} kobo ({result.currency})")
        self.stdout.write(f"  paid               : {result.paid}")
        if result.raw:
            self.stdout.write("  raw                : " + json.dumps(result.raw)[:500])

    # -- helpers ------------------------------------------------------------ #
    def _fail_provider(self, exc: ProviderError):
        detail = exc.message
        if getattr(exc, "provider_code", None):
            detail += f" (provider_code={exc.provider_code})"
        raise CommandError(
            f"Provider call failed: {detail}\n"
            "Check that the secret key is correct, the account is active, and the base URL "
            "is reachable over HTTPS."
        )

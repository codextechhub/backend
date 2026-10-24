"""The invoice, receipt and statement PDFs are HTML templates, rendered on the server.

Acme Ltd is billed 1,075 (1,000 of tuition plus 75 of VAT) and pays 400 of it.
Whether the bursar prints that invoice, emails it or Acme downloads it, the page
is the same template: the letterhead, the totals box and the bank block. The
render reaches nothing outside the package, and a server without WeasyPrint's
libraries still sends the document, in the plain layout, with an error logged.
"""
from __future__ import annotations

import datetime
import re
import types
import urllib.request
from unittest import mock

from django.core.files.base import ContentFile
from django.core.files.storage import default_storage
from django.test import TestCase, override_settings

from vs_finance.models import Account, BankAccount, Payment
from vs_finance.receivables import post_invoice, post_payment

from .tests import _ARFixtureMixin


def has_weasyprint() -> bool:
    from vs_finance.pdf import _weasyprint

    return _weasyprint() is not None

# A one-pixel PNG, enough for a logo that must be read from storage.
PIXEL_PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d4948445200000001000000010806000000"
    "1f15c4890000000d49444154789c6360f8cf000000030101005b1d1f5e0000000049454e44ae426082"
)



def pdf_bytes(pdf: bytes) -> bytes:
    """A PDF's raw bytes with every Flate stream inflated beside them.

    WeasyPrint packs the document's metadata (its Producer) and font
    dictionaries into compressed object streams, so a search of the raw bytes
    finds them on some renders and not others. Searching what a reader would
    decompress asks what the document says, not how it was packed.
    """
    import zlib

    inflated = []
    for match in re.finditer(rb"stream\r?\n(.*?)\r?\nendstream", pdf, re.S):
        try:
            inflated.append(zlib.decompress(match.group(1)))
        except zlib.error:
            continue
    return pdf + b"".join(inflated)

class _DocumentFixture(_ARFixtureMixin):

    @classmethod
    def setUpTestData(cls):
        cls.entity, _, cls.customer, cls.vat = cls.build_ar()
        cls.invoice = cls.make_invoice(
            cls.entity, cls.customer, lines=[("4100", 1, 100000, cls.vat)],
        )
        post_invoice(cls.invoice)

    def pay(self, amount):
        payment = Payment.objects.create(
            entity=self.entity, customer=self.customer, payment_date=datetime.date(2026, 1, 15),
            amount=amount, deposit_account=Account.objects.get(entity=self.entity, code="1100"),
        )
        post_payment(payment)
        payment.refresh_from_db()
        self.invoice.refresh_from_db()
        return payment

    def rendered_html(self, render, *args, **kwargs):
        """The HTML ``render`` hands to WeasyPrint, captured by a stand-in for the library.

        The stand-in keeps these tests about the template, and running, on a machine
        without Pango.
        """
        html = mock.MagicMock()
        html.return_value.write_pdf.return_value = b"%PDF-captured"
        stand_in = types.SimpleNamespace(
            HTML=html, CSS=mock.MagicMock(), FontConfiguration=mock.MagicMock(),
            URLFetcher=type("URLFetcher", (), {"__init__": lambda self, **kwargs: None}),
        )
        with mock.patch("vs_finance.pdf._weasyprint", return_value=stand_in):
            self.assertEqual(render(*args, **kwargs), b"%PDF-captured")
        return html.call_args.kwargs["string"]


@mock.patch("vs_finance.pdf._embedded_logo", return_value="")
class InvoicePdfIsTheTemplateTests(_DocumentFixture, TestCase):

    def test_the_pdf_is_the_printable_invoice(self, _logo):
        from vs_finance.pdf import invoice_pdf

        html = self.rendered_html(invoice_pdf, self.invoice)

        self.assertIn("Tax Invoice", html)
        self.assertIn(self.invoice.document_number, html)
        self.assertIn("Acme Ltd", html)
        self.assertIn("₦1,075.00", html)

    def test_nothing_paid_prints_no_paid_row(self, _logo):
        from vs_finance.pdf import invoice_pdf

        html = self.rendered_html(invoice_pdf, self.invoice)

        self.assertNotIn("Amount paid", html)
        self.assertNotIn("Credits and adjustments", html)

    def test_a_payment_prints_the_paid_row(self, _logo):
        from vs_finance.pdf import invoice_pdf

        self.pay(40000)
        html = self.rendered_html(invoice_pdf, self.invoice)

        self.assertIn("Amount paid", html)
        self.assertIn("₦400.00", html)
        self.assertIn("₦675.00", html)

    def test_a_write_off_prints_the_credits_row_so_the_totals_reconcile(self, _logo):
        from vs_finance.credit_notes import write_off_invoice
        from vs_finance.pdf import invoice_pdf

        self.pay(40000)
        write_off_invoice(self.invoice, amount=17500, write_off_date=datetime.date(2026, 1, 20))
        self.invoice.refresh_from_db()
        html = self.rendered_html(invoice_pdf, self.invoice)

        # 1,075 total, 400 paid, 175 written off: 500 left to pay.
        self.assertIn("Credits and adjustments", html)
        self.assertIn("− ₦175.00", html)
        self.assertIn("₦500.00", html)

    def test_no_collection_account_prints_no_empty_bank_block(self, _logo):
        from vs_finance.pdf import invoice_pdf

        html = self.rendered_html(invoice_pdf, self.invoice)

        self.assertNotIn("Payment by bank transfer", html)

    def test_the_collection_account_is_printed(self, _logo):
        from vs_finance.pdf import invoice_pdf

        BankAccount.objects.create(
            entity=self.entity, name="Collections Account", bank_name="GTBank",
            account_number="0123456789", is_active=True, is_primary_collection=True,
            gl_account=Account.objects.get(entity=self.entity, code="1100"),
        )
        html = self.rendered_html(invoice_pdf, self.invoice)

        self.assertIn("Payment by bank transfer", html)
        self.assertIn("GTBank", html)
        self.assertIn("0123456789", html)

    def test_the_senders_note_replaces_the_narration(self, _logo):
        from vs_finance.pdf import invoice_pdf

        self.invoice.narration = "Term 1 fees"
        self.invoice.save(update_fields=["narration"])
        html = self.rendered_html(invoice_pdf, self.invoice, note="Please pay by Friday")

        self.assertIn("Please pay by Friday", html)
        self.assertNotIn("Term 1 fees", html)

    def test_the_narration_prints_when_there_is_no_note(self, _logo):
        from vs_finance.pdf import invoice_pdf

        self.invoice.narration = "Term 1 fees"
        self.invoice.save(update_fields=["narration"])
        html = self.rendered_html(invoice_pdf, self.invoice)

        self.assertIn("Term 1 fees", html)

    def test_a_note_is_escaped(self, _logo):
        from vs_finance.pdf import invoice_pdf

        html = self.rendered_html(invoice_pdf, self.invoice, note='<img src="http://x">')

        self.assertNotIn('<img src="http://x">', html)
        self.assertIn("&lt;img", html)


@mock.patch("vs_finance.pdf._embedded_logo", return_value="")
class ReceiptPdfIsTheTemplateTests(_DocumentFixture, TestCase):

    def test_the_pdf_is_the_printable_receipt(self, _logo):
        from vs_finance.pdf import receipt_pdf

        payment = self.pay(40000)
        html = self.rendered_html(receipt_pdf, payment)

        self.assertIn("Payment received", html)
        self.assertIn(payment.document_number, html)
        self.assertIn(self.invoice.document_number, html)
        self.assertIn("Four hundred naira only", html)

    def test_the_senders_note_is_printed(self, _logo):
        from vs_finance.pdf import receipt_pdf

        payment = self.pay(40000)
        html = self.rendered_html(receipt_pdf, payment, note="Thank you, Mrs Bello")

        self.assertIn("Thank you, Mrs Bello", html)

    def test_a_balance_still_owing_is_labelled_dr(self, _logo):
        from vs_finance.pdf import receipt_pdf

        html = self.rendered_html(receipt_pdf, self.pay(40000))

        self.assertIn("Account balance after this payment", html)
        self.assertIn("\u20a6675.00 owing", html)

    def test_a_balance_in_credit_is_labelled_cr_with_no_minus_sign(self, _logo):
        from vs_finance.pdf import receipt_pdf

        html = self.rendered_html(receipt_pdf, self.pay(200000))

        self.assertIn("in credit", html)
        self.assertNotIn("\u20a6-", html)


@mock.patch("vs_finance.pdf._embedded_logo", return_value="")
class StatementPdfIsTheTemplateTests(_DocumentFixture, TestCase):
    """Acme's statement: billed 1,075 on 10 January, paid 400 on the 15th, 675 to close."""

    def test_the_pdf_is_the_statement_in_the_document_design(self, _logo):
        from vs_finance.pdf import statement_pdf

        payment = self.pay(40000)
        html = self.rendered_html(statement_pdf, self.customer, end_date=datetime.date(2026, 1, 31))

        self.assertIn("Statement", html)
        self.assertIn("Of account", html)
        self.assertIn("Acme Ltd", html)
        self.assertIn(self.invoice.document_number, html)
        self.assertIn(payment.document_number, html)
        self.assertIn("₦1,075.00", html)
        self.assertIn("₦675.00", html)
        self.assertIn("Closing balance", html)

    def test_a_period_after_the_activity_carries_it_forward(self, _logo):
        from vs_finance.pdf import statement_pdf

        self.pay(40000)
        html = self.rendered_html(
            statement_pdf, self.customer,
            start_date=datetime.date(2026, 2, 1), end_date=datetime.date(2026, 2, 28),
        )

        self.assertIn("There was no activity on this account during the period.", html)
        self.assertIn("Balance brought forward", html)
        self.assertNotIn(self.invoice.document_number, html)
        self.assertIn("₦675.00", html)

    def test_the_senders_note_is_printed(self, _logo):
        from vs_finance.pdf import statement_pdf

        html = self.rendered_html(statement_pdf, self.customer, note="Fees for Term 2 are now due")

        self.assertIn("Fees for Term 2 are now due", html)

    def test_no_collection_account_prints_no_empty_bank_block(self, _logo):
        from vs_finance.pdf import statement_pdf

        html = self.rendered_html(statement_pdf, self.customer)

        self.assertNotIn("Payment by bank transfer", html)


@override_settings(SCHOOL_APP_BASE_URL="https://xvs.example.test",
                   PLATFORM_PAY_BASE_URL="https://pay.example.test")
class InvoiceQrCodeTests(_DocumentFixture, TestCase):
    """The code on the invoice opens its pay page, which shows a settled invoice as paid."""

    def context(self):
        from vs_finance.documents import invoice_document_context

        self.invoice.refresh_from_db()
        return invoice_document_context(self.invoice)["invoice"]

    def test_an_owing_invoice_carries_a_code_to_pay(self):
        inv = self.context()

        self.assertIn("/pay/", inv["pay_url"])
        self.assertTrue(inv["qr_image"].startswith("data:image/svg+xml;base64,"))
        self.assertEqual(inv["qr_label"], "Scan to pay")

    def test_a_paid_invoice_carries_a_code_to_view_it(self):
        self.pay(107500)
        inv = self.context()

        self.assertTrue(inv["qr_image"])
        self.assertEqual(inv["qr_label"], "Scan to view")

    def test_the_printed_invoice_shows_the_code_and_its_label(self):
        from vs_finance.documents import render_invoice_document_html

        html = render_invoice_document_html(self.invoice)

        self.assertIn('class="qr-code" src="data:image/svg+xml;base64,', html)
        self.assertIn("Scan to pay", html)

    @override_settings(SCHOOL_APP_BASE_URL="", PLATFORM_PAY_BASE_URL="")
    def test_without_a_pay_page_no_code_is_printed(self):
        from vs_finance.documents import render_invoice_document_html

        inv = self.context()
        html = render_invoice_document_html(self.invoice)

        self.assertEqual(inv["qr_image"], "")
        self.assertNotIn("qr-code", html.split("</style>")[1])


class LogoIsEmbeddedTests(TestCase):

    def test_a_stored_logo_is_embedded_as_a_data_uri(self):
        from vs_finance.pdf import _embedded_logo

        name = default_storage.save("branding/crest.png", ContentFile(PIXEL_PNG))
        self.addCleanup(default_storage.delete, name)

        self.assertTrue(_embedded_logo({"logo_name": name}).startswith("data:image/png;base64,"))

    def test_an_address_without_a_stored_file_is_not_fetched(self):
        from vs_finance.pdf import _embedded_logo

        self.assertEqual(_embedded_logo({"logo": "https://example.com/logo.png", "logo_name": ""}), "")

    def test_a_stored_file_that_is_not_an_image_is_not_embedded(self):
        from vs_finance.pdf import _embedded_logo

        self.assertEqual(_embedded_logo({"logo_name": "branding/notes.html"}), "")

    def test_a_missing_file_prints_the_crest(self):
        from vs_finance.pdf import _embedded_logo

        with self.assertLogs("vs_finance.pdf", "WARNING"):
            self.assertEqual(_embedded_logo({"logo_name": "branding/gone.png"}), "")


class RenderReachesNothingOutsideTheDocumentTests(TestCase):

    def setUp(self):
        if not has_weasyprint():
            self.skipTest("WeasyPrint's native libraries are not installed")
        from vs_finance.pdf import FONT_DIR, _local_only_fetcher, _weasyprint

        self.fetcher = _local_only_fetcher(_weasyprint().URLFetcher)
        self.font = (FONT_DIR / "NotoSerif-Regular.ttf").as_uri()

    def test_a_remote_address_is_refused(self):
        for url in ("http://169.254.169.254/latest/meta-data/", "https://example.com/logo.png",
                    "ftp://example.com/x"):
            with self.subTest(url=url), self.assertRaises(ValueError):
                self.fetcher.fetch(url)

    def test_a_file_outside_the_bundled_fonts_is_refused(self):
        from vs_finance.pdf import FONT_DIR

        for path in (FONT_DIR.parent / "pdf.py", FONT_DIR / ".." / "pdf.py", "/etc/passwd"):
            url = path.as_uri() if hasattr(path, "as_uri") else f"file://{path}"
            with self.subTest(url=url), self.assertRaises(ValueError):
                self.fetcher.fetch(url)

    def test_the_bundled_font_and_a_data_uri_are_read(self):
        font = self.fetcher.fetch(self.font)
        self.addCleanup(font.close)
        self.assertGreater(len(font.read()), 1000)
        data = self.fetcher.fetch("data:text/plain;base64,aGk=")
        self.addCleanup(data.close)
        self.assertEqual(data.read(), b"hi")


class RealRenderTests(_DocumentFixture, TestCase):

    def setUp(self):
        if not has_weasyprint():
            self.skipTest("WeasyPrint's native libraries are not installed")

    def test_invoice_renders_a_pdf_with_weasyprint(self):
        from vs_finance.pdf import invoice_pdf

        pdf = invoice_pdf(self.invoice)

        self.assertTrue(pdf.startswith(b"%PDF"))
        self.assertIn(b"WeasyPrint", pdf_bytes(pdf))
        # The bundled Noto Serif embeds under the family the PDF stylesheet declares.
        self.assertIn(b"Document-Serif", pdf_bytes(pdf))

    def test_statement_renders_a_pdf_with_weasyprint(self):
        from vs_finance.pdf import statement_pdf

        self.pay(40000)
        pdf = statement_pdf(self.customer)

        self.assertTrue(pdf.startswith(b"%PDF"))
        self.assertIn(b"WeasyPrint", pdf_bytes(pdf))

    def test_receipt_renders_a_pdf_with_weasyprint(self):
        from vs_finance.pdf import receipt_pdf

        pdf = receipt_pdf(self.pay(40000))

        self.assertTrue(pdf.startswith(b"%PDF"))
        self.assertIn(b"WeasyPrint", pdf_bytes(pdf))

    def test_a_remote_image_in_the_page_is_not_requested(self):
        from vs_finance.pdf import invoice_pdf

        self.invoice.narration = "See https://example.com"
        self.invoice.save(update_fields=["narration"])
        real_open, requested = urllib.request.OpenerDirector.open, []

        def spy(director, url, *args, **kwargs):
            requested.append(getattr(url, "full_url", url))
            return real_open(director, url, *args, **kwargs)

        with mock.patch.object(urllib.request.OpenerDirector, "open", spy), \
                mock.patch("vs_finance.pdf._embedded_logo", return_value="http://10.0.0.1/x.png"):
            self.assertTrue(invoice_pdf(self.invoice).startswith(b"%PDF"))

        from vs_finance.pdf import FONT_DIR

        # Inline data: images (the pay link's QR code) are read in memory, and the
        # bundled fonts from disk; nothing else may be opened.
        fonts = FONT_DIR.as_uri() + "/"
        self.assertTrue(requested, "the bundled fonts are read through the fetcher")
        self.assertFalse([url for url in requested if "10.0.0.1" in url])
        self.assertEqual(
            [url for url in requested if not url.startswith(("data:", fonts))], [])


class WithoutWeasyPrintTests(_DocumentFixture, TestCase):
    """A server missing Pango still sends the document, and says so in the error log."""

    def without_weasyprint(self):
        return mock.patch("vs_finance.pdf._weasyprint", return_value=None)

    def test_invoice_falls_back_to_the_plain_layout(self):
        from vs_finance.pdf import invoice_pdf

        with self.without_weasyprint(), self.assertLogs("vs_finance.pdf", "ERROR") as logs:
            pdf = invoice_pdf(self.invoice)

        self.assertTrue(pdf.startswith(b"%PDF"))
        self.assertNotIn(b"WeasyPrint", pdf_bytes(pdf))
        self.assertIn("invoice_document.html", logs.output[0])

    def test_statement_falls_back_to_the_plain_layout(self):
        from vs_finance.pdf import statement_pdf

        self.pay(40000)
        with self.without_weasyprint(), self.assertLogs("vs_finance.pdf", "ERROR") as logs:
            pdf = statement_pdf(self.customer, note="Term 2")

        self.assertTrue(pdf.startswith(b"%PDF"))
        self.assertNotIn(b"WeasyPrint", pdf_bytes(pdf))
        self.assertIn("statement_document.html", logs.output[0])

    def test_receipt_falls_back_to_the_plain_layout(self):
        from vs_finance.pdf import receipt_pdf

        payment = self.pay(40000)
        with self.without_weasyprint(), self.assertLogs("vs_finance.pdf", "ERROR"):
            pdf = receipt_pdf(payment)

        self.assertTrue(pdf.startswith(b"%PDF"))
        self.assertNotIn(b"WeasyPrint", pdf_bytes(pdf))


class WeasyPrintLoadsOnceTests(TestCase):
    """A failed load is not retried: the third retry in one process crashes it."""

    def test_a_failed_load_is_attempted_once_and_remembered(self):
        """Exercised on a fresh cache of the same loader, so the process's own answer stands."""
        import builtins
        import functools

        from vs_finance.pdf import _weasyprint

        loader = functools.cache(_weasyprint.__wrapped__)
        real_import, attempts = builtins.__import__, []

        def refuse(name, *args, **kwargs):
            if name == "weasyprint" or name.startswith("weasyprint."):
                attempts.append(name)
                raise OSError("cannot load library 'libpango-1.0-0'")
            return real_import(name, *args, **kwargs)

        with mock.patch("builtins.__import__", side_effect=refuse), \
                self.assertLogs("vs_finance.pdf", "ERROR"):
            for _ in range(5):
                self.assertIsNone(loader())

        self.assertEqual(len(attempts), 1)

    def test_the_loader_keeps_its_answer(self):
        from vs_finance.pdf import _weasyprint

        self.assertIs(_weasyprint(), _weasyprint())
        self.assertGreaterEqual(_weasyprint.cache_info().hits, 1)

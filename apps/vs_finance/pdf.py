"""Branded PDF copies of the customer-facing finance documents.

These are the files attached to a customer email and the copies a parent or pupil
downloads from their app, and the payslip and tax summary an employee is sent.

**Every document is an HTML template rendered to PDF.** WeasyPrint renders
``vs_finance/invoice_document.html``, ``receipt_document.html`` and
``statement_document.html``; the first two are the templates a member of staff prints
from the browser, so Mrs Bello sees one design whether the bursar printed her invoice,
emailed it or she downloaded it herself, and her statement shares their letterhead.
The payslip and the tax summary (``payslip_document.html``,
``tax_summary_document.html``) share a second design: a sans-serif page with a
tinted summary band, set in the Hanken Grotesk and IBM Plex Mono faces bundled in
``fonts/``, with its footer drawn in the page margin so it sits at the foot of every
page. Rendering is sealed off from the network: the school's logo is read from storage
and embedded, the typefaces are the bundled ones (so the page does not depend on what
a server has installed), and every other address in the page is refused rather than
fetched. Where WeasyPrint's native libraries are missing the document still goes out,
drawn by the ReportLab layout below, and the failure is logged as an error.

The plain fallback layouts are drawn with ReportLab. Three rules shaped them:

* **One source of truth for the content.** Every renderer is driven by the *same*
  context dict the printable HTML uses (:func:`vs_finance.documents.invoice_document_context`,
  :func:`~vs_finance.documents.receipt_document_context`,
  :func:`vs_finance.payslips.payslip_context`), so a change to how an
  amount or a line is presented reaches the PDF without anyone remembering to. The
  renderers here decide layout only; they never compute a figure.
* **One look across the ReportLab documents.** They share a letterhead, a party
  block, a table style and a footer rather than each inventing their own.
* **No second money formatter.** The contexts arrive pre-formatted through
  ``format_naira``; nothing here parses an amount back into a number.

The generic ``ReportTable`` PDF in :mod:`vs_finance.exports` stays where it is: it is
an internal report dump (landscape, dark header, alternating rows), which is right for
a trial balance and wrong for a document going to a paying customer.
"""
from __future__ import annotations

import base64
import functools
import io
import logging
import mimetypes
from html import escape
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import unquote, urlparse

from django.core.files.storage import default_storage
from django.template.loader import render_to_string

from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_RIGHT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import (
    KeepTogether,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)

# House palette, kept in step with the console's finance screens.
INK = colors.HexColor("#101828")
MUTED = colors.HexColor("#667085")
RULE = colors.HexColor("#D5DFEA")
BAND = colors.HexColor("#F4F7FB")
CONTENT_WIDTH = 178 * mm

logger = logging.getLogger(__name__)

FONT_DIR = Path(__file__).resolve().parent / "fonts"

# The bundled faces, as (file, weight, style). The templates use regular, bold and italic.
_FACES = (
    ("NotoSerif-Regular.ttf", 400, "normal"),
    ("NotoSerif-Bold.ttf", 700, "normal"),
    ("NotoSerif-Italic.ttf", 400, "italic"),
)

# The payslip and tax summary faces, as (file, family, weight).
_PAYROLL_FACES = (
    ("HankenGrotesk-Regular.ttf", "Hanken Grotesk", 400),
    ("HankenGrotesk-Medium.ttf", "Hanken Grotesk", 500),
    ("HankenGrotesk-SemiBold.ttf", "Hanken Grotesk", 600),
    ("HankenGrotesk-Bold.ttf", "Hanken Grotesk", 700),
    ("IBMPlexMono-Regular.ttf", "IBM Plex Mono", 400),
    ("IBMPlexMono-Medium.ttf", "IBM Plex Mono", 500),
)


def _styles():
    sheet = getSampleStyleSheet()
    sheet.add(ParagraphStyle(name="DocTitle", parent=sheet["Title"], fontSize=16, leading=19, textColor=INK, alignment=TA_RIGHT))
    sheet.add(ParagraphStyle(name="Issuer", parent=sheet["Title"], fontSize=13, leading=16, alignment=0, textColor=INK))
    sheet.add(ParagraphStyle(name="Small", parent=sheet["BodyText"], fontSize=8.5, leading=11, textColor=INK))
    sheet.add(ParagraphStyle(name="Muted", parent=sheet["BodyText"], fontSize=8.5, leading=11, textColor=MUTED))
    sheet.add(ParagraphStyle(name="Right", parent=sheet["BodyText"], fontSize=9, leading=11, alignment=TA_RIGHT, textColor=INK))
    sheet.add(ParagraphStyle(name="Label", parent=sheet["BodyText"], fontSize=7.5, leading=10, textColor=MUTED))
    sheet.add(ParagraphStyle(name="Centre", parent=sheet["BodyText"], fontSize=8.5, leading=11, alignment=TA_CENTER, textColor=MUTED))
    return sheet


def _bare(value) -> str:
    """A money string with the currency stripped, for a column that names it once.

    Repeating "NGN" in every cell of a six-column ledger costs about 11mm per column
    on A4 portrait, which is the difference between a figure fitting on one line and
    wrapping mid-number. Accounting documents solve this by putting the currency in
    the heading, so the cells carry only the figure.
    """
    return str(value or "").replace("₦", "").strip()


def _text(value) -> str:
    """Escape for reportlab's mini-HTML, keep line breaks, and spell out the currency.

    The naira sign has no glyph in any font reportlab can rely on - not the built-in
    Helvetica, not the bundled Vera - so a ₦ reaches the customer as a black box. The
    document contexts are shared with the HTML documents, which render ₦ correctly, so
    the substitution happens here at the PDF boundary rather than by giving the PDF its
    own money formatter. "NGN" is what the purchase-order PDF already prints, so the
    two consoles' attachments agree.
    """
    return escape(str(value or "")).replace("₦", "NGN ").replace("\n", "<br/>")


def _letterhead(issuer, doc_label, doc_number, styles):
    """Issuer identity on the left, what this document is on the right."""
    contact = " · ".join(
        part for part in (issuer.get("email"), issuer.get("phone"), issuer.get("website")) if part
    )
    left = [Paragraph(f"<b>{_text(issuer.get('name'))}</b>", styles["Issuer"])]
    if issuer.get("tag"):
        left.append(Paragraph(_text(issuer["tag"]), styles["Muted"]))
    if issuer.get("address"):
        left.append(Paragraph(_text(issuer["address"]), styles["Small"]))
    if contact:
        left.append(Paragraph(_text(contact), styles["Muted"]))

    # "Statement of account" is twice the width of "Invoice"; shrink rather than wrap
    # the label across two lines beside the issuer's name.
    title_style = styles["DocTitle"] if len(doc_label) <= 12 else ParagraphStyle(
        name="DocTitleLong", parent=styles["DocTitle"], fontSize=12, leading=15,
    )
    right = [Paragraph(f"<b>{_text(doc_label.upper())}</b>", title_style)]
    if doc_number:
        right.append(Paragraph(f"<b>{_text(doc_number)}</b>", styles["Right"]))

    table = Table([[left, right]], colWidths=[CONTENT_WIDTH * 0.62, CONTENT_WIDTH * 0.38])
    table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), BAND),
        ("BOX", (0, 0), (-1, -1), 0.7, RULE),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 10),
        ("RIGHTPADDING", (0, 0), (-1, -1), 10),
        ("TOPPADDING", (0, 0), (-1, -1), 9),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 9),
    ]))
    return table


def _party_and_meta(customer, meta, styles):
    """Who the document is for, beside its dates and references."""
    left = [Paragraph("BILL TO", styles["Label"]),
            Paragraph(f"<b>{_text(customer.get('customer_name'))}</b>", styles["Small"])]
    for key in ("customer_code", "address", "email", "phone"):
        if customer.get(key):
            left.append(Paragraph(_text(customer[key]), styles["Muted"]))

    rows = [[Paragraph(_text(label), styles["Label"]),
             Paragraph(f"<b>{_text(value)}</b>", styles["Right"])]
            for label, value in meta if value]
    right = Table(rows, colWidths=[CONTENT_WIDTH * 0.19, CONTENT_WIDTH * 0.19]) if rows else Paragraph("", styles["Small"])
    if rows:
        right.setStyle(TableStyle([
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("LEFTPADDING", (0, 0), (-1, -1), 0),
            ("RIGHTPADDING", (0, 0), (-1, -1), 0),
            ("TOPPADDING", (0, 0), (-1, -1), 2),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 2),
        ]))

    table = Table([[left, right]], colWidths=[CONTENT_WIDTH * 0.58, CONTENT_WIDTH * 0.42])
    table.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 0),
        ("RIGHTPADDING", (0, 0), (-1, -1), 0),
    ]))
    return table


def _grid(columns, rows, widths, styles, *, numeric_from=1):
    """A bordered table with a banded header; columns from ``numeric_from`` right-aligned.

    Alignment is set on the paragraph style, not with a TableStyle ALIGN rule: ALIGN
    positions a *cell's* content, and a Paragraph then lays its own text out to its
    own alignment inside that box, so money set this way stays left-aligned however
    the table is styled. Every figure in these documents is a Paragraph, so the style
    is the only lever that works.
    """
    def cell_style(index, *, header=False):
        if index >= numeric_from:
            return styles["Right"]
        return styles["Small"]

    head = [Paragraph(f"<b>{_text(c)}</b>", cell_style(i, header=True)) for i, c in enumerate(columns)]
    body = [
        [cell if hasattr(cell, "wrap") else Paragraph(_text(cell), cell_style(i))
         for i, cell in enumerate(row)]
        for row in rows
    ]
    table = Table([head] + body, colWidths=widths, repeatRows=1)
    table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), BAND),
        ("LINEBELOW", (0, 0), (-1, 0), 0.7, RULE),
        ("LINEBELOW", (0, 1), (-1, -1), 0.35, RULE),
        ("BOX", (0, 0), (-1, -1), 0.7, RULE),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 7),
        ("RIGHTPADDING", (0, 0), (-1, -1), 7),
        ("TOPPADDING", (0, 0), (-1, -1), 5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
    ]))
    return table


def _totals(rows, styles, *, emphasis_last=True):
    """A right-aligned totals stack sitting under a table."""
    data = [[Paragraph(_text(label), styles["Right"]),
             Paragraph(f"<b>{_text(value)}</b>", styles["Right"])] for label, value in rows]
    table = Table(data, colWidths=[CONTENT_WIDTH * 0.28, CONTENT_WIDTH * 0.22], hAlign="RIGHT")
    style = [
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("TOPPADDING", (0, 0), (-1, -1), 3),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
        ("RIGHTPADDING", (0, 0), (-1, -1), 7),
    ]
    if emphasis_last and data:
        last = len(data) - 1
        style += [
            ("LINEABOVE", (0, last), (-1, last), 0.7, INK),
            ("BACKGROUND", (0, last), (-1, last), BAND),
        ]
    table.setStyle(TableStyle(style))
    return table


def _pay_to(issuer, styles):
    """The entity's collection account, so a customer can act on the document."""
    bank = issuer.get("bank") or {}
    if not any(bank.get(k) for k in ("bank_name", "account_name", "account_number")):
        return None
    line = " · ".join(part for part in (
        bank.get("account_name"), bank.get("bank_name"), bank.get("account_number"),
    ) if part)
    body = Table([[Paragraph("PAY TO", styles["Label"])], [Paragraph(f"<b>{_text(line)}</b>", styles["Small"])]],
                 colWidths=[CONTENT_WIDTH])
    body.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), BAND),
        ("BOX", (0, 0), (-1, -1), 0.7, RULE),
        ("LEFTPADDING", (0, 0), (-1, -1), 10),
        ("RIGHTPADDING", (0, 0), (-1, -1), 10),
        ("TOPPADDING", (0, 0), (-1, -1), 6),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
    ]))
    return body


def _payer_transfer_block(inv, styles):
    """Where a customer asked to pay by transfer pays, or the sentence that stands in for it."""
    transfer = inv.get("payer_transfer")
    if transfer:
        line = " · ".join(part for part in (
            transfer.get("account_name"), transfer.get("bank_name"), transfer.get("account_number"),
        ) if part)
        body = f"<b>{_text(line)}</b><br/>Transfers to this account are matched to this invoice automatically."
    elif inv.get("payer_note"):
        body = _text(inv["payer_note"])
    else:
        return None
    return KeepTogether([
        Paragraph("PAY BY BANK TRANSFER", styles["Label"]),
        Paragraph(body, styles["Small"]),
    ])


def _note_block(note, styles):
    if not note:
        return None
    return KeepTogether([
        Paragraph("NOTE", styles["Label"]),
        Paragraph(_text(note), styles["Small"]),
    ])


def _build(story, *, title) -> bytes:
    buffer = io.BytesIO()
    document = SimpleDocTemplate(
        buffer, pagesize=A4, title=title,
        leftMargin=16 * mm, rightMargin=16 * mm, topMargin=15 * mm, bottomMargin=15 * mm,
    )
    document.build(story)
    return buffer.getvalue()


# --------------------------------------------------------------------------- #
# HTML documents rendered to PDF                                              #
# --------------------------------------------------------------------------- #

def _pdf_stylesheet() -> str:
    """Page and typeface rules layered over a document template's own CSS.

    The templates name Hoefler Text, Garamond and Georgia, none of which a Linux
    server has, so the PDF swaps in the bundled serif through the templates'
    ``--serif`` and ``--label`` tokens. The page margin moves from the ``.page``
    box to ``@page`` so that a second page of invoice lines keeps its margins.
    WeasyPrint reads this sheet as the reader's own, which loses to the
    template's rules, so every rule here is ``!important``. The ``.page`` box is
    laid out as plain blocks: the flex column that pins a
    printed page's footer to its foot spreads the sections apart in WeasyPrint
    and pushes the footer onto a page of its own. The header row carries the
    navy fill as well as its cells, so no seam shows between them.
    """
    faces = "".join(
        f'@font-face{{font-family:"Document Serif";src:url("{(FONT_DIR / name).as_uri()}");'
        f"font-weight:{weight};font-style:{style};}}"
        for name, weight, style in _FACES
    )
    return faces + """
      :root{ --serif:"Document Serif",serif !important; --label:"Document Serif",serif !important; }
      @page{ size:A4 portrait !important; margin:14mm 14mm 12mm !important; }
      html,body{ background:#fff !important; padding:0 !important; }
      .page{ display:block !important; width:auto !important; min-height:0 !important;
        padding:0 !important; margin:0 !important; box-shadow:none !important; }
      .footer{ margin-top:24px !important; }
      table.items thead tr, table.alloc thead tr{ background:var(--navy) !important; }
    """


def _payroll_stylesheet() -> str:
    """Faces and page rules for the payslip and the tax summary.

    The typefaces are declared here, as absolute ``file:`` addresses, because a
    stylesheet inside the template has no way to name the bundled files. The page
    is A4 with the design's margins plus room beneath the content for the footer,
    which the template marks as the running element ``page-foot``: it is drawn in
    the bottom margin of every page, anchored to the foot of the sheet. A flex
    column with an auto margin above the footer would instead spread the sections
    apart in WeasyPrint, and a long payslip could not continue onto a second page.

    Every page after the first carries a slim header in the top margin: the
    running element ``page-head`` on the left, naming whose document it is, and
    the page count on the right. The words come from the template, escaped there,
    and never from this string, so a name containing a quote or a backslash cannot
    break the stylesheet. The first page already opens with the document's own
    header, so ``:first`` clears the margin boxes there.
    """
    faces = "".join(
        f'@font-face{{font-family:"{family}";src:url("{(FONT_DIR / name).as_uri()}");'
        f"font-weight:{weight};font-style:normal;}}"
        for name, family, weight in _PAYROLL_FACES
    )
    return faces + """
      @page{ size:A4 portrait; margin:56px 56px 100px;
        @bottom-left{ content:element(page-foot); width:682px; vertical-align:bottom;
          padding-bottom:44px; }
        @top-left{ content:element(page-head); width:482px; vertical-align:bottom;
          padding-bottom:10px; border-bottom:1px solid #e3e8e7; margin-bottom:8px;
          font:400 11px "Hanken Grotesk"; color:#5b6b6a; }
        @top-right{ content:"Page " counter(page) " of " counter(pages); width:200px;
          vertical-align:bottom; text-align:right; padding-bottom:10px;
          border-bottom:1px solid #e3e8e7; margin-bottom:8px;
          font:400 11px "Hanken Grotesk"; color:#5b6b6a; } }
      @page :first{
        @top-left{ content:none; border:0; }
        @top-right{ content:none; border:0; } }
    """


def _embedded_logo(issuer) -> str:
    """The issuer's stored logo as a ``data:`` URI, or ``""`` to draw the initials crest.

    The HTML document carries a URL signed for whoever opened it, and an emailed
    document has no reader to sign for, so the PDF reads the bytes from storage
    instead. A platform logo is an address typed into Platform Settings rather than
    a stored file; it is not fetched, so CodeX's documents print the crest.
    """
    name = issuer.get("logo_name") or ""
    mime = mimetypes.guess_type(name)[0] or ""
    if not name or not mime.startswith("image/"):
        return ""
    try:
        with default_storage.open(name, "rb") as handle:
            data = handle.read()
    except Exception:  # A missing file prints the crest rather than failing the document.
        logger.warning("Finance document logo %s could not be read", name, exc_info=True)
        return ""
    return f"data:{mime};base64,{base64.b64encode(data).decode('ascii')}"


@functools.cache
def _weasyprint():
    """WeasyPrint's renderer classes, or None where its native libraries are missing.

    Loaded once per process and the answer kept. A failed load leaves libraries
    half opened, and a third attempt in the same process crashes it outright, so
    a server without Pango must not try again on every document.
    """
    try:
        from weasyprint import CSS, HTML
        from weasyprint.text.fonts import FontConfiguration
        from weasyprint.urls import URLFetcher
    except (ImportError, OSError):
        logger.exception("WeasyPrint could not load; finance documents use the plain layout")
        return None
    return SimpleNamespace(HTML=HTML, CSS=CSS, FontConfiguration=FontConfiguration,
                           URLFetcher=URLFetcher)


def _local_only_fetcher(base):
    """A WeasyPrint fetcher that reads ``data:`` URIs and the bundled fonts, nothing else.

    The page is built from text a school typed (names, narrations, addresses) and is
    rendered on the server, so nothing in it may make the server request an address:
    not a remote image, not an internal service, not another file on disk. ``base``
    is WeasyPrint's ``URLFetcher``.
    """
    class LocalOnlyFetcher(base):
        def fetch(self, url, headers=None):
            parsed = urlparse(url)
            if parsed.scheme == "data" or (
                parsed.scheme == "file"
                and Path(unquote(parsed.path)).resolve().parent == FONT_DIR
            ):
                return super().fetch(url, headers)
            raise ValueError(f"{url!r} is not fetched into a finance document")

    return LocalOnlyFetcher(allowed_protocols=("data", "file"))


def _engine_for(template_name: str):
    """WeasyPrint's renderer classes, or ``None`` after logging that ``template_name`` cannot use them.

    A server without the native libraries logs an error naming the template, and the
    caller draws the plain layout instead.
    """
    engine = _weasyprint()
    if engine is None:
        logger.error("WeasyPrint is unavailable; %s went out in the plain layout", template_name)
    return engine


def _template_pdf(engine, template_name: str, context: dict, stylesheet: str) -> bytes:
    """``template_name`` rendered with ``context`` and laid out under ``stylesheet``."""
    html = render_to_string(template_name, context)
    fonts = engine.FontConfiguration()
    fetcher = _local_only_fetcher(engine.URLFetcher)
    return engine.HTML(string=html, url_fetcher=fetcher).write_pdf(
        stylesheets=[engine.CSS(string=stylesheet, font_config=fonts, url_fetcher=fetcher)],
        font_config=fonts,
    )


def _html_document_pdf(template_name: str, context: dict, *, note: str) -> bytes | None:
    """Render a printable document template to PDF, or ``None`` when WeasyPrint cannot load.

    ``note`` is the sender's covering note on an emailed copy; the templates print it
    in place of the invoice's narration.
    """
    engine = _engine_for(template_name)
    if engine is None:
        return None
    issuer = {**context["issuer"], "logo": _embedded_logo(context["issuer"])}
    return _template_pdf(
        engine, template_name, {**context, "issuer": issuer, "note": note}, _pdf_stylesheet(),
    )


def invoice_pdf(invoice, *, note: str = "") -> bytes:
    """The invoice a customer is emailed or downloads, in the printable design."""
    from .documents import invoice_document_context

    context = invoice_document_context(invoice)
    pdf = _html_document_pdf("vs_finance/invoice_document.html", context, note=note)
    return pdf if pdf is not None else _plain_invoice_pdf(invoice, context, note=note)


def receipt_pdf(payment, *, note: str = "") -> bytes:
    """The receipt a customer is emailed or downloads, in the printable design."""
    from .documents import receipt_document_context

    context = receipt_document_context(payment)
    pdf = _html_document_pdf("vs_finance/receipt_document.html", context, note=note)
    return pdf if pdf is not None else _plain_receipt_pdf(context, note=note)


# --------------------------------------------------------------------------- #
# Invoice and receipt, plain layout                                           #
# --------------------------------------------------------------------------- #

def _plain_invoice_pdf(invoice, context, *, note: str = "") -> bytes:
    """The invoice drawn with ReportLab, for a server without WeasyPrint's libraries."""
    issuer, customer, inv = context["issuer"], context["customer"], context["invoice"]
    styles = _styles()

    # Money columns are sized so a seven-figure amount stays on one line.
    widths = [CONTENT_WIDTH * 0.40, CONTENT_WIDTH * 0.08, CONTENT_WIDTH * 0.17,
              CONTENT_WIDTH * 0.14, CONTENT_WIDTH * 0.21]
    rows = []
    for line in inv["line_items"]:
        description = f"<b>{_text(line['description'])}</b>"
        if line.get("sub"):
            description += f"<br/><font color='#667085'>{_text(line['sub'])}</font>"
        rows.append([
            Paragraph(description, styles["Small"]),
            line["quantity"], _bare(line["unit_price"]),
            # A treatment label is not an amount, so it must survive the currency strip.
            line["tax_amount"] if line.get("tax_label") else _bare(line["tax_amount"]),
            _bare(line["net_amount"]),
        ])

    story = [
        _letterhead(issuer, "Invoice", inv["document_number"], styles),
        Spacer(1, 6 * mm),
        _party_and_meta(customer, [
            ("Invoice date", inv["invoice_date"]),
            ("Due date", inv["due_date"]),
            ("Reference", inv.get("reference")),
        ], styles),
        Spacer(1, 6 * mm),
        _grid(["Description", "Qty", "Unit price (NGN)", "Tax", "Amount (NGN)"], rows, widths, styles),
        Spacer(1, 4 * mm),
    ]

    # Worked from the kobo integers, not by parsing the formatted strings back.
    # An invoice's balance can fall without a payment - a credit note, concession or
    # write-off does it - so "Paid" alone leaves the customer unable to reconcile the
    # total against the balance. Name that difference instead of hiding it.
    from .money import format_naira

    adjustments = int(invoice.total) - int(invoice.amount_paid) - int(invoice.balance_due)
    totals = [("Subtotal", inv["subtotal"])]
    if inv.get("has_tax"):
        totals.append(("Tax", inv["tax_total"]))
    totals.append(("Total", inv["total"]))
    if int(invoice.amount_paid):
        totals.append(("Paid", inv["amount_paid"]))
    if adjustments:
        totals.append(("Credits and adjustments", format_naira(adjustments)))
    totals.append(("Balance due", inv["balance_due"]))
    story.append(_totals(totals, styles))

    for block in (
        _note_block(note or inv.get("narration"), styles), _pay_to(issuer, styles),
        _payer_transfer_block(inv, styles),
    ):
        if block is not None:
            story.extend([Spacer(1, 6 * mm), block])

    return _build(story, title=f"Invoice {inv['document_number']}")


def _plain_receipt_pdf(context, *, note: str = "") -> bytes:
    """The receipt drawn with ReportLab, for a server without WeasyPrint's libraries."""
    issuer, customer, rcp = context["issuer"], context["customer"], context["receipt"]
    styles = _styles()

    story = [
        _letterhead(issuer, "Receipt", rcp["document_number"], styles),
        Spacer(1, 6 * mm),
        _party_and_meta(customer, [
            ("Date", rcp["payment_date"]),
            ("Method", rcp["method"]),
            ("Reference", rcp.get("provider_reference")),
        ], styles),
        Spacer(1, 6 * mm),
    ]

    received = Table([
        [Paragraph("AMOUNT RECEIVED", styles["Label"]), Paragraph(f"<b>{_text(rcp['amount'])}</b>", styles["Right"])],
        [Paragraph(_text(rcp["amount_in_words"]), styles["Muted"]), Paragraph(_text(rcp["settled_stamp"]), styles["Right"])],
    ], colWidths=[CONTENT_WIDTH * 0.6, CONTENT_WIDTH * 0.4])
    received.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), BAND),
        ("BOX", (0, 0), (-1, -1), 0.7, RULE),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 10),
        ("RIGHTPADDING", (0, 0), (-1, -1), 10),
        ("TOPPADDING", (0, 0), (-1, -1), 7),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 7),
    ]))
    story.append(received)

    # An unallocated receipt has no settlement rows; say so rather than printing
    # an empty table the customer has to interpret.
    if rcp["allocations"]:
        widths = [CONTENT_WIDTH * 0.40, CONTENT_WIDTH * 0.30, CONTENT_WIDTH * 0.30]
        rows = []
        for alloc in rcp["allocations"]:
            label = f"<b>{_text(alloc['invoice_ref'])}</b>"
            if alloc.get("sub"):
                label += f"<br/><font color='#667085'>{_text(alloc['sub'])}</font>"
            rows.append([Paragraph(label, styles["Small"]),
                         _bare(alloc["amount_applied"]), _bare(alloc["invoice_balance_after"])])
        story.extend([
            Spacer(1, 6 * mm),
            _grid(["Settled against", "Applied (NGN)", "Balance after (NGN)"], rows, widths, styles),
        ])
    else:
        story.extend([
            Spacer(1, 5 * mm),
            Paragraph("This receipt is held as credit on your account and has not yet been "
                      "applied to a specific invoice.", styles["Muted"]),
        ])

    story.extend([Spacer(1, 4 * mm), _totals([("Account balance after this payment", rcp["customer_balance_after"])], styles)])

    note_block = _note_block(note, styles)
    if note_block is not None:
        story.extend([Spacer(1, 6 * mm), note_block])

    return _build(story, title=f"Receipt {rcp['document_number']}")


# --------------------------------------------------------------------------- #
# Statement of account                                                        #
# --------------------------------------------------------------------------- #

def statement_pdf(customer, *, start_date=None, end_date=None, note: str = "",
                  scope=None) -> bytes:
    """The statement of account a customer is emailed, in the printable design.

    ``scope`` narrows it to a reader's branches, as
    :func:`vs_finance.reports.customer_statement` does; ``None`` is the whole account.
    """
    from .documents import statement_document_context

    context = statement_document_context(
        customer, start_date=start_date, end_date=end_date, scope=scope,
    )
    pdf = _html_document_pdf("vs_finance/statement_document.html", context, note=note)
    return pdf if pdf is not None else _plain_statement_pdf(context, note=note)


def _plain_statement_pdf(context, *, note: str = "") -> bytes:
    """The statement drawn with ReportLab, for a server without WeasyPrint's libraries."""
    issuer, customer, stmt = context["issuer"], context["customer"], context["statement"]
    styles = _styles()

    widths = [CONTENT_WIDTH * 0.13, CONTENT_WIDTH * 0.17, CONTENT_WIDTH * 0.26,
              CONTENT_WIDTH * 0.14, CONTENT_WIDTH * 0.14, CONTENT_WIDTH * 0.16]
    rows = [[row["date"], row["document_number"], row["description"],
             _bare(row["debit"]), _bare(row["credit"]), _bare(row["balance"])]
            for row in stmt["rows"]]

    story = [
        _letterhead(issuer, "Statement of account", "", styles),
        Spacer(1, 6 * mm),
        _party_and_meta(customer, [
            ("Period", stmt["period"]),
            ("Opening balance", stmt["opening_balance"]),
        ], styles),
        Spacer(1, 6 * mm),
    ]

    if rows:
        story.append(_grid(["Date", "Document", "Description", "Debit (NGN)", "Credit (NGN)", "Balance (NGN)"],
                           rows, widths, styles, numeric_from=3))
    else:
        story.append(Paragraph("There was no activity on this account during the period.", styles["Muted"]))

    story.extend([
        Spacer(1, 4 * mm),
        _totals([
            ("Opening balance", stmt["opening_balance"]),
            ("Total charges", stmt["total_debits"]),
            ("Total payments and credits", stmt["total_credits"]),
            ("Closing balance", stmt["closing_balance"]),
        ], styles),
    ])

    for block in (_note_block(note, styles), _pay_to(issuer, styles)):
        if block is not None:
            story.extend([Spacer(1, 6 * mm), block])

    story.extend([
        Spacer(1, 8 * mm),
        Paragraph("This statement reflects activity recorded up to the period end date shown above.",
                  styles["Centre"]),
    ])

    return _build(story, title=f"Statement of account {stmt['customer_code']}")


# --------------------------------------------------------------------------- #
# Payslip and yearly tax summary                                              #
# --------------------------------------------------------------------------- #

def _previous_heading(brought) -> str:
    """The heading over a previous employer's figures, naming the employer where known."""
    name = (brought.get("employer_name") or "").strip()
    return f"Earlier this tax year with {name}" if name else "Earlier this tax year elsewhere"


PAYSLIP_TEMPLATE = "vs_finance/payslip_document.html"
TAX_SUMMARY_TEMPLATE = "vs_finance/tax_summary_document.html"


def _bare_figures(figures: dict, *, keep=()) -> dict:
    """``figures`` with the currency stripped from every value except those named in ``keep``."""
    return {k: v if k in keep else _bare(v) for k, v in figures.items()}


def _payslip_template_context(context) -> dict:
    """The payslip context as its template prints it: money bare, the currency named in headings.

    The template does no arithmetic and no formatting. Everything it needs beyond the
    payslip context is the heading over a previous employer's table.
    """
    def rows(items):
        return [{**row, "amount": _bare(row["amount"])} for row in items]

    brought = context.get("brought_forward")
    opening = context.get("opening")
    return {
        **context,
        "gross": _bare(context["gross"]),
        "total_deductions": _bare(context["total_deductions"]),
        "net": _bare(context["net"]),
        "employer_total": _bare(context["employer_total"]),
        "earnings": rows(context["earnings"]),
        "deductions": rows(context["deductions"]),
        "employer": rows(context["employer"]),
        "ytd": _bare_figures(context["ytd"]),
        "opening": _bare_figures(opening) if opening else None,
        "brought_forward": _bare_figures(brought, keep=("employer_name",)) if brought else None,
        "brought_heading": _previous_heading(brought) if brought else "",
    }


def payslip_pdf(context) -> bytes:
    """Render one person's payslip from :func:`vs_finance.payslips.payslip_context`.

    The year to date is this employer's; a previous employer's figures the
    PAYE counted print in their own table beneath it. The branch line shows only
    where the tenant owns several branches (``show_branch``).
    """
    engine = _engine_for(PAYSLIP_TEMPLATE)
    if engine is None:
        return _plain_payslip_pdf(context)
    return _template_pdf(
        engine, PAYSLIP_TEMPLATE, _payslip_template_context(context), _payroll_stylesheet(),
    )


def _tax_summary_template_context(summary) -> dict:
    """The tax summary as its template prints it: kobo formatted as bare naira figures."""
    from .money import format_naira

    def naira(figures: dict, keys) -> dict:
        return {key: _bare(format_naira(figures[key])) for key in keys}

    figures = ("gross", "taxable_pay", "paye", "pension")
    opening, brought = summary.get("opening"), summary.get("brought_forward")
    return {
        "year": summary["year"],
        "issuer": summary["issuer"],
        "employee_name": summary["employee_name"],
        "tax_id": summary["tax_id"],
        "tax_states": ", ".join(summary["tax_states"]),
        "months": [
            {"label": row["period_label"] or row["pay_date"], **naira(row, (*figures, "net"))}
            for row in summary["months"]
        ],
        "opening": naira(opening, figures) if opening else None,
        "totals": naira(summary["totals"], (*figures, "net")),
        "brought_forward": naira(brought, figures) if brought else None,
        "brought_heading": _previous_heading(brought) if brought else "",
    }


def tax_summary_pdf(summary) -> bytes:
    """Render a person's yearly tax summary from :func:`vs_finance.payslips.tax_summary`.

    This employer's own months from before its payroll ran here head the
    table, inside the totals; their net is left blank, as it is not known. A
    previous employer's figures print in their own table beneath.
    """
    engine = _engine_for(TAX_SUMMARY_TEMPLATE)
    if engine is None:
        return _plain_tax_summary_pdf(summary)
    return _template_pdf(
        engine, TAX_SUMMARY_TEMPLATE, _tax_summary_template_context(summary), _payroll_stylesheet(),
    )


# --------------------------------------------------------------------------- #
# Payslip and tax summary, plain layout                                       #
# --------------------------------------------------------------------------- #

def _facts(pairs, styles):
    """A two-column block of labelled facts, skipping the empty ones."""
    rows = [[Paragraph(_text(label), styles["Label"]), Paragraph(f"<b>{_text(value)}</b>", styles["Small"])]
            for label, value in pairs if value]
    if not rows:
        return Spacer(1, 1)
    table = Table(rows, colWidths=[CONTENT_WIDTH * 0.28, CONTENT_WIDTH * 0.72])
    table.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("TOPPADDING", (0, 0), (-1, -1), 2),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 2),
    ]))
    return table


def _plain_payslip_pdf(context) -> bytes:
    """The payslip drawn with ReportLab, for a server without WeasyPrint's libraries.

    The year to date is this employer's; a previous employer's figures the
    PAYE counted print in their own table beneath it.
    """
    styles = _styles()
    issuer = {"name": context["issuer"]}
    story = [
        _letterhead(issuer, "Payslip", context["document_number"], styles),
        Spacer(1, 5 * mm),
        _facts([
            ("Employee", context["employee_name"]),
            ("Pay period", context["period_label"]),
            ("Pay date", context["pay_date"]),
            ("Branch", context["branch"]),
            ("Tax ID", context["tax_id"]),
            ("PAYE state", context["tax_state"]),
            ("Pension administrator", context["pfa"]),
            ("Pension PIN", context["pension_pin"]),
        ], styles),
        Spacer(1, 5 * mm),
    ]
    widths = [CONTENT_WIDTH * 0.7, CONTENT_WIDTH * 0.3]
    earnings = [[row["name"], _bare(row["amount"])] for row in context["earnings"]]
    if earnings:
        story += [_grid(["Earnings", "Amount (NGN)"], earnings, widths, styles), Spacer(1, 3 * mm)]
    deductions = [[row["name"], _bare(row["amount"])] for row in context["deductions"]]
    story += [
        _grid(["Deductions", "Amount (NGN)"], deductions or [["None", "0.00"]], widths, styles),
        Spacer(1, 3 * mm),
        _totals([
            ("Gross pay", context["gross"]),
            ("Total deductions", context["total_deductions"]),
            ("Net pay", context["net"]),
        ], styles),
    ]
    employer = [[row["name"], _bare(row["amount"])] for row in context["employer"]]
    if employer:
        story += [
            Spacer(1, 5 * mm),
            _grid(["Paid by your employer on top of your pay", "Amount (NGN)"], employer, widths, styles),
        ]
    ytd, opening = context["ytd"], context.get("opening")
    ytd_rows = [
        ["Gross pay", _bare(ytd["gross"])], ["PAYE", _bare(ytd["paye"])],
        ["Pension", _bare(ytd["pension"])],
        ["Net pay through this payroll" if opening else "Net pay", _bare(ytd["net"])],
    ]
    if opening:
        ytd_rows += [
            ["Of which gross brought forward from before this payroll", _bare(opening["gross"])],
            ["Of which PAYE brought forward from before this payroll", _bare(opening["paye"])],
        ]
    story += [
        Spacer(1, 5 * mm),
        _grid(["Year to date", "Amount (NGN)"], ytd_rows, widths, styles),
    ]
    brought = context.get("brought_forward")
    if brought:
        story += [
            Spacer(1, 3 * mm),
            _grid([_previous_heading(brought), "Amount (NGN)"], [
                ["Gross pay", _bare(brought["gross"])],
                ["Taxable pay", _bare(brought["taxable_pay"])],
                ["PAYE", _bare(brought["paye"])], ["Pension", _bare(brought["pension"])],
            ], widths, styles),
        ]
    story += [
        Spacer(1, 6 * mm),
        Paragraph(_text(
            f"PAYE: {context['paye_source']}"
            + (f" ({context['tax_table']})" if context["tax_table"] else "") + "."
        ), styles["Centre"]),
    ]
    return _build(story, title=f"Payslip {context['document_number']}")


def _plain_tax_summary_pdf(summary) -> bytes:
    """The yearly tax summary drawn with ReportLab, for a server without WeasyPrint's libraries.

    This employer's own months from before its payroll ran here head the
    table, inside the totals; their net is left blank, as it is not known. A
    previous employer's figures print in their own table beneath.
    """
    from .money import format_naira

    styles = _styles()
    story = [
        _letterhead({"name": summary["issuer"]}, "Tax summary", str(summary["year"]), styles),
        Spacer(1, 5 * mm),
        _facts([
            ("Employee", summary["employee_name"]),
            ("Tax year", str(summary["year"])),
            ("Tax ID", summary["tax_id"]),
            ("PAYE state", ", ".join(summary["tax_states"])),
        ], styles),
        Spacer(1, 5 * mm),
    ]
    rows = [
        [row["period_label"] or row["pay_date"], _bare(format_naira(row["gross"])),
         _bare(format_naira(row["taxable_pay"])), _bare(format_naira(row["paye"])),
         _bare(format_naira(row["pension"])), _bare(format_naira(row["net"]))]
        for row in summary["months"]
    ]
    totals, opening = summary["totals"], summary.get("opening")
    if opening:
        rows.insert(0, [
            "Before this payroll", _bare(format_naira(opening["gross"])),
            _bare(format_naira(opening["taxable_pay"])), _bare(format_naira(opening["paye"])),
            _bare(format_naira(opening["pension"])), "",
        ])
    rows.append([
        "Total", _bare(format_naira(totals["gross"])), _bare(format_naira(totals["taxable_pay"])),
        _bare(format_naira(totals["paye"])), _bare(format_naira(totals["pension"])),
        _bare(format_naira(totals["net"])),
    ])
    widths = [CONTENT_WIDTH * 0.20] + [CONTENT_WIDTH * 0.16] * 5
    story.append(_grid(
        ["Month", "Gross (NGN)", "Taxable (NGN)", "PAYE (NGN)", "Pension (NGN)", "Net (NGN)"],
        rows, widths, styles,
    ))
    brought = summary.get("brought_forward")
    if brought:
        half = [CONTENT_WIDTH * 0.7, CONTENT_WIDTH * 0.3]
        story += [
            Spacer(1, 5 * mm),
            _grid([_previous_heading(brought), "Amount (NGN)"], [
                ["Gross pay", _bare(format_naira(brought["gross"]))],
                ["Taxable pay", _bare(format_naira(brought["taxable_pay"]))],
                ["PAYE", _bare(format_naira(brought["paye"]))],
                ["Pension", _bare(format_naira(brought["pension"]))],
            ], half, styles),
        ]
    return _build(story, title=f"Tax summary {summary['year']}")

"""Branded PDF copies of the customer-facing finance documents.

These are the files attached to a customer email. Three rules shaped the module:

* **One source of truth for the content.** Every renderer is driven by the *same*
  context dict the printable HTML uses (:func:`vs_finance.documents.invoice_document_context`,
  :func:`~vs_finance.documents.receipt_document_context`), so a change to how an
  amount or a line is presented reaches the PDF without anyone remembering to. The
  renderers here decide layout only; they never compute a figure.
* **One look across the three.** A customer can receive an invoice, a receipt and a
  statement from the same business in one week, so they share a letterhead, a party
  block, a table style and a footer rather than each inventing their own.
* **No second money formatter.** The contexts arrive pre-formatted through
  ``format_naira``; nothing here parses an amount back into a number.

The generic ``ReportTable`` PDF in :mod:`vs_finance.exports` stays where it is: it is
an internal report dump (landscape, dark header, alternating rows), which is right for
a trial balance and wrong for a document going to a paying customer.
"""
from __future__ import annotations

import io
from html import escape

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
# Invoice                                                                     #
# --------------------------------------------------------------------------- #

def invoice_pdf(invoice, *, note: str = "") -> bytes:
    """Render the branded invoice a customer receives by email."""
    from .documents import invoice_document_context

    context = invoice_document_context(invoice)
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
            # "Exempt" is not an amount, so it must survive the currency strip.
            line["tax_amount"] if line.get("is_exempt") else _bare(line["tax_amount"]),
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

    for block in (_note_block(note or inv.get("narration"), styles), _pay_to(issuer, styles)):
        if block is not None:
            story.extend([Spacer(1, 6 * mm), block])

    return _build(story, title=f"Invoice {inv['document_number']}")


# --------------------------------------------------------------------------- #
# Receipt                                                                     #
# --------------------------------------------------------------------------- #

def receipt_pdf(payment, *, note: str = "") -> bytes:
    """Render the branded receipt a customer receives by email."""
    from .documents import receipt_document_context

    context = receipt_document_context(payment)
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

    story.extend([Spacer(1, 4 * mm), _totals([("Account balance", rcp["customer_balance_after"])], styles)])

    note_block = _note_block(note, styles)
    if note_block is not None:
        story.extend([Spacer(1, 6 * mm), note_block])

    return _build(story, title=f"Receipt {rcp['document_number']}")


# --------------------------------------------------------------------------- #
# Statement of account                                                        #
# --------------------------------------------------------------------------- #

def statement_pdf(customer, *, start_date=None, end_date=None, note: str = "") -> bytes:
    """Render a statement of account for ``customer`` over the given period."""
    from .documents import _customer_block, _issuer_block
    from .money import format_naira
    from .reports import customer_statement

    statement = customer_statement(customer, start_date=start_date, end_date=end_date)
    issuer = _issuer_block(customer.entity)
    styles = _styles()

    widths = [CONTENT_WIDTH * 0.13, CONTENT_WIDTH * 0.17, CONTENT_WIDTH * 0.26,
              CONTENT_WIDTH * 0.14, CONTENT_WIDTH * 0.14, CONTENT_WIDTH * 0.16]
    rows = [[
        str(entry.date),
        entry.document_number,
        entry.description or entry.doc_type,
        _bare(format_naira(entry.debit)) if entry.debit else "",
        _bare(format_naira(entry.credit)) if entry.credit else "",
        _bare(format_naira(entry.balance)),
    ] for entry in statement.entries]

    period = f"{statement.start_date or 'inception'} to {statement.end_date}"
    story = [
        _letterhead(issuer, "Statement of account", "", styles),
        Spacer(1, 6 * mm),
        _party_and_meta(_customer_block(customer), [
            ("Period", period),
            ("Opening balance", format_naira(statement.opening_balance)),
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
            ("Opening balance", format_naira(statement.opening_balance)),
            ("Total charges", format_naira(statement.total_debits)),
            ("Total payments and credits", format_naira(statement.total_credits)),
            ("Closing balance", format_naira(statement.closing_balance)),
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

    return _build(story, title=f"Statement of account {statement.customer_code}")

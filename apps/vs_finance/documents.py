"""Context builders for the branded, printable invoice & receipt documents.

These turn a posted :class:`~vs_finance.models.Invoice` / :class:`~vs_finance.models.Payment`
into a flat ``dict`` the standalone print templates (``vs_finance/invoice_document.html``,
``receipt_document.html``) render. Money is kept as integer kobo everywhere inside the
backend and formatted to naira strings only here, at the display edge, via
:func:`vs_finance.money.format_naira` / :func:`vs_finance.money.to_naira`. Dates
are written here too, in the entity's tenant's date format
(:mod:`vs_config.display`), so a printed invoice reads its dates the way the
school's screens do. Each document is built when it is asked for, so it follows
the setting in force at that moment.

Issuer identity comes from the entity's originating school (platform books have none,
so those fields fall back to blanks); the 'pay to' bank is the collection account of
the document's branch (:func:`primary_collection_account`).
"""
from __future__ import annotations

from django.template.loader import render_to_string

from core.media import signed_url
from vs_config.display import format_date

from .constants import TaxTreatment
from .money import account_balance_words, format_naira, naira_in_words
from .pay_links import invoice_pay_url
from .payer_options import payer_payment_options


# Render a document template to HTML.
def render_document_html(template_name: str, context: dict, *, request=None) -> str:
    """Render a printable finance document template to HTML.

    The same templates serve two readers. A member of staff opens the HTML in a
    browser and prints it (the templates carry ``@media print`` / ``@page A4``
    rules); :mod:`vs_finance.pdf` renders the identical HTML to the PDF a
    customer is emailed or downloads. One template is one design, so the
    printed copy and the attached copy cannot drift apart.
    """
    return render_to_string(template_name, context, request=request)  # Delegate rendering to Django templates.


# Resolve the bank account printed on finance documents.
def primary_collection_account(entity, branch=None):
    """Return the :class:`BankAccount` a document of ``branch`` prints as "pay to", or None.

    Money paid against a branch's invoice belongs in that branch's bank, so the
    document prints that branch's account and never another's: a Lekki invoice
    naming Ikeja's GTBank would send the Adeyemis' fees into Ikeja's books. In
    order, the branch's flagged collection account, then its first active account,
    where an account not yet given a branch counts as the branch's only at a
    tenant with one branch (:func:`vs_rbac.scoping.same_transaction_branch`) and
    otherwise only after every account of the branch's own. ``None`` when the
    branch has no account to print.

    With no ``branch`` (a document not yet given one) the entity's flagged
    account, else its first active one.
    """
    from vs_rbac.scoping import transaction_branch_match_q

    from .models import BankAccount

    qs = BankAccount.objects.filter(entity=entity)
    branch_id = getattr(branch, "pk", branch)
    if branch_id is None:
        return (
            qs.filter(is_primary_collection=True).order_by("id").first()
            or qs.filter(is_active=True).order_by("id").first()
        )
    own = qs.filter(transaction_branch_match_q(entity.tenant_id, branch_id))
    unbranched_last = ("branch_id", "id")  # Nulls sort last, so the branch's own account wins.
    return (
        own.filter(is_primary_collection=True).order_by(*unbranched_last).first()
        or own.filter(is_active=True).order_by(*unbranched_last).first()
    )


# --------------------------------------------------------------------------- #
# Shared blocks                                                               #
# --------------------------------------------------------------------------- #

def issuer_name(entity) -> str:
    """The name the issuer of ``entity``'s documents goes by, and nothing else about it.

    The same choice :func:`_issuer_block` makes for ``name`` (the school's name,
    the platform's configured name, or the ledger entity's), without the pay-to
    bank or logo lookups, for a screen that names the payee and no more.
    """
    school = getattr(entity.tenant, "school_profile", None)
    if school is not None:
        return school.name
    if entity.is_platform:
        from vs_config.platform_settings import get_platform_profile

        return get_platform_profile().get("name") or entity.name
    return entity.name


def _issuer_block(entity, *, branch=None) -> dict:
    """Letterhead identity for the document's issuer - *the entity keeping the books*.

    The invoice/receipt is a generic document, so the issuer adapts to who is billing:

    * A **school-owned** entity prints the *school's* own branding (name, motto, logo,
      address, website) - a school billing its parents shows its own letterhead.
    * The **platform (CodeX)** entity prints the audited Platform Settings identity,
      with ``PLATFORM_ISSUER`` deployment values as fallback. When CodeX bills a
      school (its customer), the school sees CodeX's details.
    * Any other school-less entity falls back to the ledger entity's name.

    The pay-to bank is the collection account of the document's branch
    (:func:`primary_collection_account`) regardless of which identity is used.

    The account number in that block is a registered field
    (``finance.bankaccount.account_number``), and this render is the declared
    exception to filtering it by the caller: the document is addressed to the
    customer who has to pay into the account, not to the member of staff who
    pressed Send, so it is built in system context. An invoice missing its
    account number cannot be paid, and one that differed by whoever sent it
    would not be the same document twice.
    """
    from vs_rbac.field_enforcement import assert_system_surface

    assert_system_surface("vs_finance.documents._issuer_block")

    school = getattr(entity.tenant, "school_profile", None)  # A school-sourced entity brands from its school.
    branch_email = getattr(branch, "email", "") if branch is not None else ""  # Optional branch contact email.
    branch_address = getattr(branch, "address", "") if branch is not None else ""  # Optional branch address.
    logo = ""  # Default: no logo unless a source provides one.
    # The storage name behind that logo, when it is one of ours. Callers that
    # need the bytes rather than a browser URL (the public RFQ page serves the
    # logo itself, to a vendor who has no account) take this instead of picking
    # the name back out of the URL string.
    logo_name = ""
    email = branch_email  # Default issuer email is the branch email (overridden below).
    phone = ""  # Default: no phone unless a source provides one.

    if school is not None:  # School billing its own customers → the school's letterhead.
        branding = getattr(school, "branding", None)  # Branding is the logo/theme relation, may be absent.
        if branding is not None and getattr(branding, "logo", None):  # Only when a logo is actually set.
            try:  # Storage backends can fail to build a URL.
                # Signed for whoever is reading the document: the browser fetches
                # this <img src> with their own session, so an unsigned path would
                # simply render broken.
                logo_name = branding.logo.name  # The storage name behind it.
                logo = signed_url(logo_name)  # Use the school logo URL.
            except Exception:  # pragma: no cover - storage without a URL
                logo = ""  # Fall back to no logo on URL failure.
                logo_name = ""
        name = school.name  # Issuer name is the school name.
        tag = getattr(school, "motto", "") or ""  # Tagline is the school motto if present.
        address = branch_address or getattr(school, "address", "") or ""  # Branch address wins, else school address.
        website = getattr(school, "website", "") or ""  # School website if present.
    elif entity.is_platform:  # CodeX billing its customers (the schools) → CodeX's own identity.
        from vs_config.platform_settings import get_platform_profile

        # Admin-saved identity wins, with the deployment environment retained
        # as the fallback for fields that have not been configured yet.
        issuer = get_platform_profile()
        name = issuer.get("name") or entity.name  # Configured CodeX name, else the ledger entity name.
        tag = issuer.get("tagline", "") or ""  # CodeX tagline.
        address = issuer.get("address", "") or branch_address  # CodeX address, else any branch address.
        website = issuer.get("website", "") or ""  # CodeX website.
        logo = issuer.get("logo_url", "") or ""  # CodeX logo URL (a URL, not an ImageField).
        email = issuer.get("email", "") or branch_email  # CodeX billing email, else branch email.
        phone = issuer.get("phone", "") or ""  # CodeX phone.
    else:  # Other school-less (e.g. product) entities: just the ledger entity name.
        name = entity.name  # No branding source, so use the entity name.
        tag = ""  # No tagline available.
        address = branch_address  # Only a branch address (if any) is available.
        website = ""  # No website available.

    bank = primary_collection_account(entity, branch)  # Pay-to bank is the branch's collection account.
    bank_block = {  # Template-friendly bank block; blanks when no account is configured.
        "bank_name": getattr(bank, "bank_name", "") or "",  # Bank name.
        "account_name": getattr(bank, "name", "") or "",  # Account name (the BankAccount.name).
        "account_number": getattr(bank, "account_number", "") or "",  # Account number.
    } if bank is not None else {"bank_name": "", "account_name": "", "account_number": ""}  # No-account fallback.

    return {  # Flat issuer structure consumed by the document templates.
        "name": name,  # Issuer display name.
        "tag": tag,  # Motto/tagline.
        "logo": logo,  # Logo URL, signed for the current reader (may be blank).
        "logo_name": logo_name,  # Storage name, for callers that serve the bytes.
        "address": address,  # Mailing address.
        "email": email,  # Contact email.
        "phone": phone,  # Contact phone.
        "website": website,  # Website URL.
        "bank": bank_block,  # Pay-to bank details.
    }


def _printed_date(day, entity) -> str:
    """*day* in the entity's tenant's date format, or "-" when there is none."""
    return format_date(day, entity.tenant) or "-"


def statement_period(statement, tenant) -> str:
    """A statement's period as printed: "1 Sep 2026 to 30 Sep 2026".

    A statement with no start runs from the account's first entry, which it
    calls "inception".
    """
    start = format_date(statement.start_date, tenant) or "inception"
    return f"{start} to {format_date(statement.end_date, tenant)}"


# Build customer identity block for invoice/receipt templates.
def _customer_block(customer) -> dict:
    """The bill-to block: the customer's own contact details on a document addressed to them.

    The billing address, email and phone are registered fields of
    ``finance.customer``, and this render is a declared exception to filtering
    them by the caller (:data:`vs_rbac.field_enforcement.SYSTEM_SURFACES`): the
    document is addressed to the customer, so it reads the same whoever
    prints or sends it.
    """
    from vs_rbac.field_enforcement import assert_system_surface

    assert_system_surface("vs_finance.documents._customer_block")

    return {  # Return a template-friendly customer structure.
        "customer_name": customer.name,  # Customer display name.
        "customer_code": customer.code,  # Customer account/reference code.
        "email": customer.billing_email or "",  # Billing email or blank.
        "phone": customer.billing_phone or "",  # Billing phone or blank.
        "address": customer.billing_address or "",  # Billing address or blank.
    }


# Convert invoice payment status to template CSS token.
def _payment_status_badge(payment_status: str) -> str:
    """Map an invoice payment status to the template's badge CSS class."""
    from .constants import InvoicePaymentStatus

    if payment_status == InvoicePaymentStatus.PAID:  # Fully settled invoices use paid styling.
        return "paid"
    if payment_status == InvoicePaymentStatus.PARTIAL:  # Part-settled invoices use partial styling.
        return "partial"
    return "unpaid"  # UNPAID (and overdue+unpaid) both read as 'unpaid'


def customer_position(entity, customer) -> tuple[int, int]:
    """The customer's AR position as ``(owing, credit)`` in kobo, from one ledger read.

    ``owing`` is the gross unsettled balance of open invoices and debit notes and
    is never reduced by credit; ``credit`` is the unapplied credit the entity
    holds for the customer. Both come from the AR ledger already backing the
    customer drawer (:func:`vs_finance.views_ar._customer_ledger`), whole entity.
    """
    from .views_ar import _customer_ledger

    led = _customer_ledger(entity, [customer.id]).get(customer.id, {})
    return int(led.get("outstanding", 0)), int(led.get("credit", 0))


def customer_net_after(entity, customer) -> int:
    """The customer's net AR position (kobo; positive = owes) after current postings.

    Net is owing less credit, as :func:`customer_position` reports them.
    """
    owing, credit = customer_position(entity, customer)
    return owing - credit


# --------------------------------------------------------------------------- #
# Invoice document                                                            #
# --------------------------------------------------------------------------- #

def _vat_label(line):
    """What the VAT column prints instead of an amount, or ``None`` to print the amount.

    The line's tax code says how the supply is treated: an exempt or zero-rated
    code prints its treatment, and a line with no code prints "No VAT" rather
    than claiming an exemption nobody chose. A standard-rated line prints its tax.
    """
    if not line.tax_code_id:
        return "No VAT"
    treatment = line.tax_code.treatment
    if treatment == TaxTreatment.STANDARD:
        return None
    return TaxTreatment(treatment).label


def _qr_image(url: str) -> str:
    """``url`` as a QR code, an SVG ``data:`` URI an ``<img>`` shows, or "" for no url.

    Drawn on the server with ReportLab, so the printed page and the PDF carry the
    same code without fetching anything, and the image is markup the template
    never has to mark safe.
    """
    import base64

    from reportlab.graphics import renderSVG
    from reportlab.graphics.barcode.qr import QrCodeWidget
    from reportlab.graphics.shapes import Drawing

    if not url:
        return ""
    widget = QrCodeWidget(url, barLevel="M")
    left, bottom, right, top = widget.getBounds()
    size = 74
    drawing = Drawing(size, size, transform=[size / (right - left), 0, 0, size / (top - bottom), 0, 0])
    drawing.add(widget)
    svg = renderSVG.drawToString(drawing)
    return "data:image/svg+xml;base64," + base64.b64encode(svg.encode("utf-8")).decode("ascii")


# Build printable invoice template context.
def invoice_document_context(invoice) -> dict:
    """Build the render context for the branded invoice document.

    A customer the books' owner asks to pay by bank transfer
    (:mod:`vs_finance.payer_options`) gets no pay-online link or QR code: the
    block carries their transfer account (``payer_transfer``) or, while they have
    none, the owner's sentence (``payer_note``).
    """
    entity = invoice.entity  # Invoice entity drives issuer and bank details.
    lines = list(invoice.lines.select_related("tax_code", "cost_center").order_by("line_no", "id"))

    line_items = []  # Template-ready line item dictionaries.
    for ln in lines:  # Convert each invoice line for display.
        # A blank/whole quantity prints as an integer; fractional quantities keep dp.  # Improves document readability.
        qty = ln.quantity  # Decimal quantity from the invoice line.
        qty_str = str(int(qty)) if qty == qty.to_integral_value() else f"{qty.normalize()}"  # Format whole vs fractional quantity.
        sub = ln.cost_center.name if ln.cost_center_id else ""  # Optional cost-center subtitle.
        line_items.append({  # Append template line item.
            "description": ln.description or ln.revenue_account.name,  # Prefer explicit description.
            "sub": sub,  # Secondary line text.
            "quantity": qty_str,  # Display quantity.
            "unit_price": format_naira(ln.unit_price),  # Display unit price.
            "tax_amount": _vat_label(ln) or format_naira(ln.tax_amount),  # Tax amount or treatment.
            "tax_label": _vat_label(ln),  # Treatment printed in place of an amount.
            "is_exempt": bool(ln.tax_code_id) and ln.tax_code.treatment == TaxTreatment.EXEMPT,
            "net_amount": format_naira(ln.net_amount),  # Display net line amount.
        })

    # Worked from the kobo integers, never by parsing formatted strings back.
    adjustments = int(invoice.total) - int(invoice.amount_paid) - int(invoice.balance_due)
    # The public pay page, which shows a settled invoice as paid rather than
    # asking for money, so the printed code stays right after the invoice is paid.
    options = payer_payment_options(invoice.customer)
    card_allowed = options is None or options.card_allowed
    pay_url = invoice_pay_url(invoice) if card_allowed else ""
    payer_transfer, payer_note = None, ""
    if not card_allowed:
        account = options.transfer_account
        payer_transfer = (
            {"bank_name": account.bank_name, "account_number": account.account_number,
             "account_name": account.account_name}
            if account is not None else None
        )
        payer_note = "" if account is not None else options.no_account_message

    return {  # Return full invoice document context.
        "issuer": _issuer_block(entity, branch=invoice.branch),  # Letterhead and pay-to block.
        "customer": _customer_block(invoice.customer),  # Bill-to block.
        "invoice": {  # Invoice-specific template values.
            "document_number": invoice.document_number,  # Invoice number.
            "invoice_date": _printed_date(invoice.invoice_date, entity),  # Display invoice date.
            "due_date": _printed_date(invoice.due_date, entity),  # Display due date.
            "reference": invoice.reference or "",  # External/reference text.
            "narration": invoice.narration or "",  # Narrative text.
            "payment_status": invoice.payment_status,  # Raw payment status.
            "payment_status_badge": _payment_status_badge(invoice.payment_status),  # CSS badge token.
            "line_items": line_items,  # Prepared line item list.
            "subtotal": format_naira(invoice.subtotal),  # Display subtotal.
            "tax_total": format_naira(invoice.tax_total),  # Display total tax.
            "has_tax": invoice.tax_total > 0,  # Template flag for tax rows.
            "total": format_naira(invoice.total),  # Display gross total.
            "amount_paid": format_naira(invoice.amount_paid),  # Display paid amount.
            "has_paid": int(invoice.amount_paid) > 0,  # Template flag for the paid row.
            # What lowered the balance other than a payment: a credit note,
            # concession or write-off. Without this row a customer cannot
            # reconcile Total less Paid against Balance due.
            "adjustments": ("\u2212\u00a0" if adjustments > 0 else "+\u00a0") + format_naira(abs(adjustments)),
            "has_adjustments": adjustments != 0,
            "balance_due": format_naira(invoice.balance_due),  # Display outstanding balance.
            # The public pay page. The printable invoice reaches the same
            # customer the email does, so its "Pay online" control has to be a
            # real destination rather than a dead chip.
            "pay_url": pay_url,
            "qr_image": _qr_image(pay_url),
            "qr_label": "Scan to pay" if invoice.balance_due > 0 else "Scan to view",
            "payer_transfer": payer_transfer,  # Where the customer pays when card is not offered.
            "payer_note": payer_note,  # What stands in place of that account while there is none.
        },
    }


# Render an invoice document to HTML.
def render_invoice_document_html(invoice, *, request=None) -> str:
    """The invoice as HTML for its reader.

    A reader whose role may not read a virtual account's number or name
    (``payments.virtual_account``) is shown the document without the customer's
    transfer account, or without its name. The PDF and email, which are sent to
    the customer and built for nobody, carry the account whoever sends them.
    """
    context = invoice_document_context(invoice)  # Build invoice context.
    _withhold_payer_transfer(context["invoice"], request)
    return render_document_html(  # Delegate shared HTML rendering.
        "vs_finance/invoice_document.html",  # Invoice template path.
        context,
        request=request,  # Pass request for context processors/static absolute paths.
    )


def _withhold_payer_transfer(invoice_block: dict, request) -> None:
    """Drop from *invoice_block* what *request*'s caller may not read of the customer's transfer account."""
    from vs_rbac.field_enforcement import can_read

    transfer = invoice_block.get("payer_transfer")
    if transfer is None:
        return
    if not can_read(request, "payments.virtual_account.account_number"):
        invoice_block["payer_transfer"] = None
    elif not can_read(request, "payments.virtual_account.account_name"):
        transfer["account_name"] = ""


# --------------------------------------------------------------------------- #
# Receipt document                                                            #
# --------------------------------------------------------------------------- #

# Resolve PSP reference for gateway-backed receipts.
def _provider_reference(payment) -> str:
    """The PSP transaction ref for a gateway receipt, best-effort (blank if none)."""
    try:  # Payments app may be optional in some environments.
        from vs_payments.models import CollectionIntent
    except ImportError:  # pragma: no cover - vs_payments optional
        return ""
    intent = CollectionIntent.objects.filter(payment=payment).order_by("-id").first()
    return intent.provider_reference if intent is not None else ""  # Return provider ref or blank.


# Build printable receipt template context.
def receipt_document_context(payment) -> dict:
    """Build the render context for the branded receipt document."""
    from .constants import PaymentMethod

    entity = payment.entity  # Payment entity drives issuer and bank details.
    allocations = []  # Template-ready settlement rows.
    for alloc in payment.allocations.select_related("invoice").order_by("id"):
        inv = alloc.invoice  # Linked invoice for this allocation.
        allocations.append({  # Append allocation display row.
            "invoice_ref": inv.document_number,  # Settled invoice number.
            "sub": inv.narration or inv.reference or "",  # Secondary invoice text.
            "amount_applied": format_naira(alloc.amount),  # Display amount applied to invoice.
            # The balance frozen when this slice was applied; today's for older rows.
            "invoice_balance_after": format_naira(
                alloc.invoice_balance_after if alloc.invoice_balance_after is not None
                else inv.balance_due
            ),
        })

    try:  # Convert stored method value to human label when enum knows it.
        method_label = PaymentMethod(payment.method).label  # Django choices enum label.
    except ValueError:  # Unknown/custom method values fall back to raw string.
        method_label = payment.method  # Preserve stored payment method.

    return {  # Return full receipt document context.
        "issuer": _issuer_block(entity, branch=payment.branch),  # Letterhead and pay-to block.
        "customer": _customer_block(payment.customer),  # Receipt customer block.
        "receipt": {  # Receipt-specific template values.
            "document_number": payment.document_number,  # Receipt number.
            "payment_date": _printed_date(payment.payment_date, entity),  # Display payment date.
            "method": method_label,  # Human-readable payment method.
            "provider_reference": _provider_reference(payment),  # Gateway provider reference when present.
            "amount": format_naira(payment.amount),  # Display receipt amount.
            "amount_in_words": naira_in_words(payment.amount),  # Display amount in words.
            "allocations": allocations,  # Prepared allocation rows.
            # Frozen at posting; worked out now only for receipts posted before that.
            "customer_balance_after": account_balance_words(
                payment.customer_balance_after if payment.customer_balance_after is not None
                else customer_net_after(entity, payment.customer),
            ),
            "settled_stamp": "Received",  # Static stamp text for the template.
        },
    }


# Render a receipt document to HTML.
def render_receipt_document_html(payment, *, request=None) -> str:
    return render_document_html(  # Delegate shared HTML rendering.
        "vs_finance/receipt_document.html",  # Receipt template path.
        receipt_document_context(payment),  # Build receipt context.
        request=request,  # Pass request for context processors/static absolute paths.
    )


# --------------------------------------------------------------------------- #
# Statement of account                                                        #
# --------------------------------------------------------------------------- #

def statement_document_context(customer, *, start_date=None, end_date=None, scope=None) -> dict:
    """Build the render context for the branded statement of account.

    ``scope`` narrows the statement to a reader's branches, as
    :func:`vs_finance.reports.customer_statement` does; ``None`` is the whole
    account. A day with nothing charged or paid prints blank rather than a zero,
    so the column a movement falls in reads at a glance.
    """
    from .reports import customer_statement

    statement = customer_statement(customer, start_date=start_date, end_date=end_date, scope=scope)
    tenant = customer.entity.tenant
    return {
        "issuer": _issuer_block(customer.entity, branch=customer.branch),
        "customer": _customer_block(customer),
        "statement": {
            "customer_code": statement.customer_code,
            "period": statement_period(statement, tenant),
            "statement_date": format_date(statement.end_date, tenant),
            "opening_balance": format_naira(statement.opening_balance),
            "rows": [
                {
                    "date": format_date(entry.date, tenant),
                    "document_number": entry.document_number,
                    "description": entry.description or entry.doc_type,
                    "debit": format_naira(entry.debit) if entry.debit else "",
                    "credit": format_naira(entry.credit) if entry.credit else "",
                    "balance": format_naira(entry.balance),
                }
                for entry in statement.entries
            ],
            "total_debits": format_naira(statement.total_debits),
            "total_credits": format_naira(statement.total_credits),
            "closing_balance": format_naira(statement.closing_balance),
        },
    }

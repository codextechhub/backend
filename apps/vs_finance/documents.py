"""Context builders for the branded, printable invoice & receipt documents.

These turn a posted :class:`~vs_finance.models.Invoice` / :class:`~vs_finance.models.Payment`
into a flat ``dict`` the standalone print templates (``vs_finance/invoice_document.html``,
``receipt_document.html``) render. Money is kept as integer kobo everywhere inside the
backend and formatted to naira strings only here, at the display edge, via
:func:`vs_finance.money.format_naira` / :func:`vs_finance.money.to_naira`.

Issuer identity comes from the entity's originating school (platform books have none,
so those fields fall back to blanks); the 'pay to' bank is the entity's primary
collection account (:func:`primary_collection_account`).
"""
from __future__ import annotations

from django.template.loader import render_to_string

from .money import format_naira, naira_in_words


# Render a document template to HTML.
def render_document_html(template_name: str, context: dict, *, request=None) -> str:
    """Render a printable finance document template to HTML.

    PDF is produced client-side: the frontend opens this print-ready HTML (the
    templates carry ``@media print`` / ``@page A4`` rules) and the browser's
    print-to-PDF renders it. There is no server-side PDF path, so nothing here
    depends on native rendering libraries.
    """
    return render_to_string(template_name, context, request=request)  # Delegate rendering to Django templates.


# Resolve the bank account printed on finance documents.
def primary_collection_account(entity):
    """Return the entity's primary fee-collection :class:`BankAccount`, or a fallback.

    Preference order: the account flagged ``is_primary_collection`` → the first active
    account → ``None``. This is what the invoice/receipt print as the 'pay to' bank.
    """
    from .models import BankAccount

    qs = BankAccount.objects.filter(entity=entity)
    return (  # Prefer explicit primary account, fallback to first active account.
        qs.filter(is_primary_collection=True).first()
        or qs.filter(is_active=True).order_by("id").first()
    )


# --------------------------------------------------------------------------- #
# Shared blocks                                                               #
# --------------------------------------------------------------------------- #

def _issuer_block(entity, *, branch=None) -> dict:
    """Letterhead identity for the document's issuer — *the entity keeping the books*.

    The invoice/receipt is a generic document, so the issuer adapts to who is billing:

    * A **school-owned** entity prints the *school's* own branding (name, motto, logo,
      address, website) — a school billing its parents shows its own letterhead.
    * The **platform (CodeX)** entity prints CodeX's identity from ``PLATFORM_ISSUER``
      settings — when CodeX bills a school (its customer), the school sees CodeX's
      details.
    * Any other school-less entity falls back to the ledger entity's name.

    The pay-to bank is always the entity's primary collection account regardless of
    which identity is used.
    """
    school = getattr(entity.tenant, "school_profile", None)  # A school-sourced entity brands from its school.
    branch_email = getattr(branch, "email", "") if branch is not None else ""  # Optional branch contact email.
    branch_address = getattr(branch, "address", "") if branch is not None else ""  # Optional branch address.
    logo = ""  # Default: no logo unless a source provides one.
    email = branch_email  # Default issuer email is the branch email (overridden below).
    phone = ""  # Default: no phone unless a source provides one.

    if school is not None:  # School billing its own customers → the school's letterhead.
        branding = getattr(school, "branding", None)  # Branding is the logo/theme relation, may be absent.
        if branding is not None and getattr(branding, "logo", None):  # Only when a logo is actually set.
            try:  # Storage backends can fail to build a URL.
                logo = branding.logo.url  # Use the school logo URL.
            except Exception:  # pragma: no cover - storage without a URL
                logo = ""  # Fall back to no logo on URL failure.
        name = school.name  # Issuer name is the school name.
        tag = getattr(school, "motto", "") or ""  # Tagline is the school motto if present.
        address = branch_address or getattr(school, "address", "") or ""  # Branch address wins, else school address.
        website = getattr(school, "website", "") or ""  # School website if present.
    elif entity.is_platform:  # CodeX billing its customers (the schools) → CodeX's own identity.
        from django.conf import settings  # PLATFORM_ISSUER is deploy-configured, read lazily.

        issuer = getattr(settings, "PLATFORM_ISSUER", {}) or {}  # CodeX letterhead config (may be partly blank).
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

    bank = primary_collection_account(entity)  # Pay-to bank is the entity's primary collection account.
    bank_block = {  # Template-friendly bank block; blanks when no account is configured.
        "bank_name": getattr(bank, "bank_name", "") or "",  # Bank name.
        "account_name": getattr(bank, "name", "") or "",  # Account name (the BankAccount.name).
        "account_number": getattr(bank, "account_number", "") or "",  # Account number.
    } if bank is not None else {"bank_name": "", "account_name": "", "account_number": ""}  # No-account fallback.

    return {  # Flat issuer structure consumed by the document templates.
        "name": name,  # Issuer display name.
        "tag": tag,  # Motto/tagline.
        "logo": logo,  # Logo URL.
        "address": address,  # Mailing address.
        "email": email,  # Contact email.
        "phone": phone,  # Contact phone.
        "website": website,  # Website URL.
        "bank": bank_block,  # Pay-to bank details.
    }


# Build customer identity block for invoice/receipt templates.
def _customer_block(customer) -> dict:
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


# Compute customer net receivable after current postings.
def _customer_net_after(entity, customer) -> int:
    """The customer's net AR position (kobo; positive = owes) after current postings.

    Reuses the AR ledger already backing the customer drawer
    (:func:`vs_finance.views_ar._customer_ledger`): net = outstanding − credit.
    """
    from .views_ar import _customer_ledger

    led = _customer_ledger(entity, [customer.id]).get(customer.id, {})
    return int(led.get("outstanding", 0)) - int(led.get("credit", 0))


# --------------------------------------------------------------------------- #
# Invoice document                                                            #
# --------------------------------------------------------------------------- #

# Build printable invoice template context.
def invoice_document_context(invoice) -> dict:
    """Build the render context for the branded invoice document."""
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
            "tax_amount": format_naira(ln.tax_amount) if ln.tax_code_id else "Exempt",  # Display tax or exemption.
            "is_exempt": ln.tax_code_id is None,  # Boolean for template styling.
            "net_amount": format_naira(ln.net_amount),  # Display net line amount.
        })

    return {  # Return full invoice document context.
        "issuer": _issuer_block(entity, branch=invoice.branch),  # Letterhead and pay-to block.
        "customer": _customer_block(invoice.customer),  # Bill-to block.
        "invoice": {  # Invoice-specific template values.
            "document_number": invoice.document_number,  # Invoice number.
            "invoice_date": invoice.invoice_date.isoformat() if invoice.invoice_date else "—",  # Display invoice date.
            "due_date": invoice.due_date.isoformat() if invoice.due_date else "—",  # Display due date.
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
            "balance_due": format_naira(invoice.balance_due),  # Display outstanding balance.
            "qr_payload": invoice.document_number,  # QR payload currently uses document number.
        },
    }


# Render an invoice document to HTML.
def render_invoice_document_html(invoice, *, request=None) -> str:
    return render_document_html(  # Delegate shared HTML rendering.
        "vs_finance/invoice_document.html",  # Invoice template path.
        invoice_document_context(invoice),  # Build invoice context.
        request=request,  # Pass request for context processors/static absolute paths.
    )


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
            "invoice_balance_after": format_naira(inv.balance_due),  # Display invoice balance after allocation.
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
            "payment_date": payment.payment_date.isoformat() if payment.payment_date else "—",  # Display payment date.
            "method": method_label,  # Human-readable payment method.
            "provider_reference": _provider_reference(payment),  # Gateway provider reference when present.
            "amount": format_naira(payment.amount),  # Display receipt amount.
            "amount_in_words": naira_in_words(payment.amount),  # Display amount in words.
            "allocations": allocations,  # Prepared allocation rows.
            "customer_balance_after": format_naira(_customer_net_after(entity, payment.customer)),  # Display net AR after receipt.
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

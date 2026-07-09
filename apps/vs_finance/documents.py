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
from __future__ import annotations  # Defer annotation evaluation during app import.

from django.template.loader import render_to_string  # Renders printable document templates.

from .money import format_naira, naira_in_words  # Display formatting for integer-kobo amounts.


class DocumentRenderUnavailable(Exception):  # Signals optional document rendering dependency failure.
    """Raised when an optional document renderer dependency is unavailable."""


def render_document_html(template_name: str, context: dict, *, request=None) -> str:  # Render a document template to HTML.
    """Render a printable finance document template to HTML."""
    return render_to_string(template_name, context, request=request)  # Delegate rendering to Django templates.


def render_document_pdf(html: str, *, base_url: str | None = None) -> bytes:  # Convert rendered document HTML to PDF bytes.
    """Render document HTML to PDF using WeasyPrint, imported lazily.

    WeasyPrint depends on native libraries in many environments. Keeping the import
    here prevents missing libraries from breaking Django startup or unrelated tests.
    """
    try:  # WeasyPrint is optional and may fail on hosts without native libraries.
        from weasyprint import HTML  # Import lazily to avoid breaking unrelated app startup.
    except Exception as exc:  # pragma: no cover - exact failure depends on host libs
        raise DocumentRenderUnavailable("PDF rendering is unavailable.") from exc
    try:  # Rendering itself can fail due to native or template asset issues.
        return HTML(string=html, base_url=base_url).write_pdf()  # Render HTML into PDF bytes.
    except Exception as exc:  # pragma: no cover - renderer/native-library failure
        raise DocumentRenderUnavailable("PDF rendering failed.") from exc


def primary_collection_account(entity):  # Resolve the bank account printed on finance documents.
    """Return the entity's primary fee-collection :class:`BankAccount`, or a fallback.

    Preference order: the account flagged ``is_primary_collection`` → the first active
    account → ``None``. This is what the invoice/receipt print as the 'pay to' bank.
    """
    from .models import BankAccount  # Local import avoids model import cycles.

    qs = BankAccount.objects.filter(entity=entity)  # Restrict bank accounts to the document entity.
    return (  # Prefer explicit primary account, fallback to first active account.
        qs.filter(is_primary_collection=True).first()  # Primary collection account when configured.
        or qs.filter(is_active=True).order_by("id").first()  # Stable fallback active account.
    )


# --------------------------------------------------------------------------- #
# Shared blocks                                                               #
# --------------------------------------------------------------------------- #

def _issuer_block(entity, *, branch=None) -> dict:  # Build school/entity identity block for document headers.
    """Letterhead identity for ``entity`` — school-derived, blanks for platform books."""
    school = entity.source_school  # School is the branding source when present.
    logo = ""  # Default to no logo.
    branch_email = getattr(branch, "email", "") if branch is not None else ""  # Branch email overrides blank.
    branch_address = getattr(branch, "address", "") if branch is not None else ""  # Branch address overrides school address.

    if school is not None:  # School-backed entities print school branding.
        branding = getattr(school, "branding", None)  # Branding relation may not exist.
        if branding is not None and getattr(branding, "logo", None):  # Logo is optional.
            try:  # Storage backends can fail to produce a URL.
                logo = branding.logo.url  # Use logo URL when available.
            except Exception:  # pragma: no cover - storage without a URL
                logo = ""  # Keep logo blank when URL resolution fails.
        name = school.name  # Display school name.
        tag = getattr(school, "motto", "") or ""  # Display optional school motto.
        address = branch_address or getattr(school, "address", "") or ""  # Prefer branch address, fallback to school address.
        website = getattr(school, "website", "") or ""  # Display optional school website.
    else:  # Platform/product entities have no school branding.
        name = entity.name  # Display ledger entity name.
        tag = ""  # No school motto exists.
        address = branch_address  # Only branch address is available.
        website = ""  # No school website exists.

    bank = primary_collection_account(entity)  # Resolve pay-to bank details.
    bank_block = {  # Template-friendly bank block.
        "bank_name": getattr(bank, "bank_name", "") or "",  # Bank name or blank.
        "account_name": getattr(bank, "name", "") or "",  # Account name or blank.
        "account_number": getattr(bank, "account_number", "") or "",  # Account number or blank.
    } if bank is not None else {"bank_name": "", "account_name": "", "account_number": ""}  # Blank block when no account.

    return {  # Return a flat issuer structure for templates.
        "name": name,  # Issuer display name.
        "tag": tag,  # Motto/tagline.
        "logo": logo,  # Logo URL.
        "address": address,  # Mailing address.
        "email": branch_email,  # Branch contact email.
        "phone": "",  # Phone is currently not sourced.
        "website": website,  # Website URL.
        "bank": bank_block,  # Pay-to bank details.
    }


def _customer_block(customer) -> dict:  # Build customer identity block for invoice/receipt templates.
    return {  # Return a template-friendly customer structure.
        "customer_name": customer.name,  # Customer display name.
        "customer_code": customer.code,  # Customer account/reference code.
        "email": customer.billing_email or "",  # Billing email or blank.
        "phone": customer.billing_phone or "",  # Billing phone or blank.
        "address": customer.billing_address or "",  # Billing address or blank.
    }


def _payment_status_badge(payment_status: str) -> str:  # Convert invoice payment status to template CSS token.
    """Map an invoice payment status to the template's badge CSS class."""
    from .constants import InvoicePaymentStatus  # Local import avoids wider import dependencies.

    if payment_status == InvoicePaymentStatus.PAID:  # Fully settled invoices use paid styling.
        return "paid"
    if payment_status == InvoicePaymentStatus.PARTIAL:  # Part-settled invoices use partial styling.
        return "partial"
    return "unpaid"  # UNPAID (and overdue+unpaid) both read as 'unpaid'


def _customer_net_after(entity, customer) -> int:  # Compute customer net receivable after current postings.
    """The customer's net AR position (kobo; positive = owes) after current postings.

    Reuses the AR ledger already backing the customer drawer
    (:func:`vs_finance.views_ar._customer_ledger`): net = outstanding − credit.
    """
    from .views_ar import _customer_ledger  # Reuse the AR drawer ledger calculation.

    led = _customer_ledger(entity, [customer.id]).get(customer.id, {})  # Fetch ledger summary for one customer.
    return int(led.get("outstanding", 0)) - int(led.get("credit", 0))  # Net open receivable minus credit.


# --------------------------------------------------------------------------- #
# Invoice document                                                            #
# --------------------------------------------------------------------------- #

def invoice_document_context(invoice) -> dict:  # Build printable invoice template context.
    """Build the render context for the branded invoice document."""
    entity = invoice.entity  # Invoice entity drives issuer and bank details.
    lines = list(invoice.lines.select_related("tax_code", "cost_center").order_by("line_no", "id"))  # Load invoice lines in print order.

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


def render_invoice_document_html(invoice, *, request=None) -> str:  # Render an invoice document to HTML.
    return render_document_html(  # Delegate shared HTML rendering.
        "vs_finance/invoice_document.html",  # Invoice template path.
        invoice_document_context(invoice),  # Build invoice context.
        request=request,  # Pass request for context processors/static absolute paths.
    )


def render_invoice_document_pdf(invoice, *, request=None) -> bytes:  # Render an invoice document to PDF.
    html = render_invoice_document_html(invoice, request=request)  # Render HTML first.
    base_url = request.build_absolute_uri("/") if request is not None else None  # Resolve relative assets when request exists.
    return render_document_pdf(html, base_url=base_url)  # Convert HTML to PDF bytes.


# --------------------------------------------------------------------------- #
# Receipt document                                                            #
# --------------------------------------------------------------------------- #

def _provider_reference(payment) -> str:  # Resolve PSP reference for gateway-backed receipts.
    """The PSP transaction ref for a gateway receipt, best-effort (blank if none)."""
    try:  # Payments app may be optional in some environments.
        from vs_payments.models import CollectionIntent  # Gateway collection intent model.
    except ImportError:  # pragma: no cover - vs_payments optional
        return ""
    intent = CollectionIntent.objects.filter(payment=payment).order_by("-id").first()  # Latest intent for this payment.
    return intent.provider_reference if intent is not None else ""  # Return provider ref or blank.


def receipt_document_context(payment) -> dict:  # Build printable receipt template context.
    """Build the render context for the branded receipt document."""
    from .constants import PaymentMethod  # Local import for payment method labels.

    entity = payment.entity  # Payment entity drives issuer and bank details.
    allocations = []  # Template-ready settlement rows.
    for alloc in payment.allocations.select_related("invoice").order_by("id"):  # Walk allocations in stable order.
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


def render_receipt_document_html(payment, *, request=None) -> str:  # Render a receipt document to HTML.
    return render_document_html(  # Delegate shared HTML rendering.
        "vs_finance/receipt_document.html",  # Receipt template path.
        receipt_document_context(payment),  # Build receipt context.
        request=request,  # Pass request for context processors/static absolute paths.
    )


def render_receipt_document_pdf(payment, *, request=None) -> bytes:  # Render a receipt document to PDF.
    html = render_receipt_document_html(payment, request=request)  # Render HTML first.
    base_url = request.build_absolute_uri("/") if request is not None else None  # Resolve relative assets when request exists.
    return render_document_pdf(html, base_url=base_url)  # Convert HTML to PDF bytes.

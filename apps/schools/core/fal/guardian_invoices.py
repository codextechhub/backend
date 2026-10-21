"""Each new bill for a pupil is sent to the pupil's primary guardian.

When a fee is raised for a pupil, by billing a class or one pupil at a time,
the primary guardian is told: in the parent app and by push when they have a
parent app account, and by email with the invoice as a PDF either way. It is
the rule results follow (:mod:`schools.vs_gradebook.services.publishing`): the
primary guardian is the parent the school deals with, and a second parent sees
the bill in their own parent app without being sent it.

The finance engine knows nothing of guardians. Its own copy of a posted
invoice goes to the customer's billing email, which a pupil's finance record
does not carry unless somebody typed one. Where somebody typed the primary
guardian's own address there, that copy is the guardian's email and this one
is not sent, so nobody receives the same bill twice.

An opening balance carried into the books is not a new bill and is not sent,
as the finance engine does not send its own copy of one. A billing preview
rolls back and posts nothing, so it sends nothing.

The PDF is rendered off the request (:func:`schools.core.fal.tasks.send_guardian_invoice`),
because billing a class posts one invoice per pupil.
"""
from __future__ import annotations

import logging
import uuid

logger = logging.getLogger(__name__)

EVENT_GUARDIAN_INVOICE_ISSUED = "billing.guardian_invoice_issued"


def on_invoice_posted(sender, invoice, **kwargs):
    """Queue the guardian's copy of a pupil's new bill.

    Connected in :meth:`schools.core.fal.apps.FalConfig.ready`; runs after the
    posting committed. Only the cheap checks are made here; the task repeats
    them against the stored invoice.
    """
    from vs_finance.constants import InvoiceSource

    from .contracts import SOURCE_TYPE_STUDENT
    from .tasks import send_guardian_invoice

    if invoice.customer.source_type != SOURCE_TYPE_STUDENT or invoice.source == InvoiceSource.OPENING:
        return
    send_guardian_invoice.delay(invoice.pk)


def send_to_primary_guardian(invoice_id) -> list[str]:
    """Send the primary guardian the invoice. Returns the notification ids, or [] for none.

    Never raises: a guardian's copy that cannot go is logged, and the bill
    stands whether or not anybody was told of it.
    """
    try:
        return _send(invoice_id)
    except Exception:
        logger.exception("The guardian's copy of invoice %s could not be sent.", invoice_id)
        return []


def _send(invoice_id) -> list[str]:
    from django.core.files.base import ContentFile
    from django.core.files.storage import default_storage

    from schools.vs_notices.links import parent_app_url
    from schools.vs_students.models import Student, StudentGuardian
    from vs_config.display import format_date
    from vs_finance.constants import DocumentStatus, InvoiceSource
    from vs_finance.money import to_naira
    from vs_finance.models import Invoice
    from vs_finance.pdf import invoice_pdf
    from vs_notifications.notify import UnregisteredRecipient, send_notification
    from vs_rbac.scoping import only_branch_id_or_several

    from .contracts import SOURCE_TYPE_STUDENT, student_pk
    from .transfer_accounts import _pupil_customer

    invoice = (
        Invoice.objects.select_related("customer", "entity__tenant", "branch")
        .prefetch_related("lines__tax_code", "lines__cost_center", "lines__revenue_account")
        .filter(pk=invoice_id).first()
    )
    if invoice is None or invoice.status != DocumentStatus.POSTED or invoice.source == InvoiceSource.OPENING:
        return []
    if invoice.customer.source_type != SOURCE_TYPE_STUDENT or _pupil_customer(invoice.customer_id) is None:
        return []

    tenant = invoice.entity.tenant
    student = Student.all_objects.select_related("branch").get(pk=student_pk(invoice.customer.source_id))
    link = (
        StudentGuardian.all_objects.filter(student=student, tenant=tenant, is_primary=True)
        .select_related("guardian__user").first()
    )
    if link is None:
        return []
    guardian = link.guardian
    guardian_user = guardian.user if guardian.user_id and guardian.user.may_sign_in else None
    email = ((guardian_user.email if guardian_user else "") or guardian.email or "").strip()
    billing_email = (invoice.customer.billing_email or "").strip().lower()
    if billing_email and billing_email == email.lower():
        return []
    if guardian_user is None and not email:
        return []

    branch = invoice.branch or student.branch
    filename = f"Invoice-{invoice.document_number}.pdf"
    storage_name = default_storage.save(
        f"guardian-invoices/{tenant.slug}/{uuid.uuid4().hex}/{filename}",
        ContentFile(invoice_pdf(invoice)),
    )
    context = {
        "student_first_name": student.first_name,
        "invoice_number": invoice.document_number,
        "invoice_amount": f"{to_naira(int(invoice.total or 0)):,.2f}",
        "due_date": format_date(invoice.due_date, tenant) or "-",
        "tenant_name": tenant.name,
        "branch_name": branch.name if branch and only_branch_id_or_several(tenant) is None else "",
        "has_parent_app": guardian_user is not None,
        "parent_app_link": parent_app_url(tenant, f"fees?child={student.pk}") if guardian_user else "",
    }
    return send_notification(
        event_key=EVENT_GUARDIAN_INVOICE_ISSUED, context=context,
        recipients=[guardian_user] if guardian_user else [],
        unregistered_recipients=None if guardian_user else [
            UnregisteredRecipient(email=email, name=guardian.full_name)],
        tenant=tenant, branch=branch,
        metadata={
            "invoice_id": invoice.pk,
            "student_id": student.pk,
            "document_number": invoice.document_number,
            "attachments": [{
                "name": filename, "storage_name": storage_name, "content_type": "application/pdf",
            }],
        },
    ) or []

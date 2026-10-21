"""A pupil's bills and statements reach the pupil's primary guardian.

The primary guardian is the parent the school deals with, the rule results
follow (:mod:`schools.vs_gradebook.services.publishing`); a second parent sees
the bill in their own parent app without being sent it.

Two halves, so the finance engine never learns what a guardian is:

* **Email.** :func:`primary_guardian_contact` is registered with
  :mod:`vs_finance.document_contacts`, so every invoice and statement emailed
  for a pupil, automatically when it posts or by Email invoice, also reaches the
  primary guardian with its PDF, greeted by their own name. The finance engine
  records the send with the rest of the document's history.
* **The parent app.** :func:`on_invoice_posted` tells a primary guardian with a
  parent app account of each new bill in the app, with a push that opens the
  child's Fees (``billing.guardian_invoice_issued``). The push names no figure.

An opening balance carried into the books is not a new bill and is not
announced. A billing preview rolls back and posts nothing, so it sends nothing.
"""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

EVENT_GUARDIAN_INVOICE_ISSUED = "billing.guardian_invoice_issued"

#: The Field Access key guarding a guardian's address wherever a send shows it.
GUARDIAN_EMAIL_FIELD = "school.guardians.email"


def _primary_link(customer):
    """The pupil's primary guardian link for a pupil's customer, or None."""
    from schools.vs_students.models import StudentGuardian

    from .contracts import SOURCE_TYPE_STUDENT, student_pk
    from .transfer_accounts import _pupil_customer

    if customer.source_type != SOURCE_TYPE_STUDENT or _pupil_customer(customer.pk) is None:
        return None
    return (
        StudentGuardian.all_objects.filter(
            student_id=student_pk(customer.source_id), tenant_id=customer.entity.tenant_id,
            is_primary=True,
        )
        .select_related("guardian__user", "student").first()
    )


def _guardian_email(guardian) -> str:
    user = guardian.user if guardian.user_id else None
    return ((user.email if user else "") or guardian.email or "").strip()


def primary_guardian_contact(customer):
    """The pupil's primary guardian as a document contact, for a pupil's customer."""
    from vs_finance.document_contacts import DocumentContact

    link = _primary_link(customer)
    if link is None:
        return []
    email = _guardian_email(link.guardian)
    if not email:
        return []
    return [DocumentContact(
        email=email, name=link.guardian.full_name, field=GUARDIAN_EMAIL_FIELD,
        label=f"{link.student.first_name}'s primary guardian",
    )]


def on_invoice_posted(sender, invoice, **kwargs):
    """Queue the parent app notice of a pupil's new bill.

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


def tell_primary_guardian(invoice_id) -> list[str]:
    """Tell the primary guardian of a new bill in their parent app. Returns notification ids.

    Never raises: a notice that cannot go is logged, and the bill stands
    whether or not anybody was told of it.
    """
    try:
        return _tell(invoice_id)
    except Exception:
        logger.exception("The guardian's notice of invoice %s could not be sent.", invoice_id)
        return []


def _tell(invoice_id) -> list[str]:
    from schools.vs_notices.links import parent_app_url
    from vs_config.display import format_date
    from vs_finance.constants import DocumentStatus, InvoiceSource
    from vs_finance.money import to_naira
    from vs_finance.models import Invoice
    from vs_notifications.notify import send_notification
    from vs_rbac.scoping import only_branch_id_or_several

    invoice = (
        Invoice.objects.select_related("customer__entity", "entity__tenant", "branch")
        .filter(pk=invoice_id).first()
    )
    if invoice is None or invoice.status != DocumentStatus.POSTED or invoice.source == InvoiceSource.OPENING:
        return []
    link = _primary_link(invoice.customer)
    if link is None:
        return []
    guardian = link.guardian
    user = guardian.user if guardian.user_id and guardian.user.may_sign_in else None
    if user is None:
        return []

    tenant, student = invoice.entity.tenant, link.student
    branch = invoice.branch or student.branch
    return send_notification(
        event_key=EVENT_GUARDIAN_INVOICE_ISSUED,
        context={
            "student_first_name": student.first_name,
            "invoice_number": invoice.document_number,
            "invoice_amount": f"{to_naira(int(invoice.total or 0)):,.2f}",
            "due_date": format_date(invoice.due_date, tenant) or "-",
            "tenant_name": tenant.name,
            "branch_name": branch.name if branch and only_branch_id_or_several(tenant) is None else "",
            "parent_app_link": parent_app_url(tenant, f"fees?child={student.pk}"),
        },
        recipients=[user], tenant=tenant, branch=branch,
        metadata={"invoice_id": invoice.pk, "student_id": student.pk,
                  "document_number": invoice.document_number},
    ) or []

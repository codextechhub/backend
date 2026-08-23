"""Attachment endpoints for vendor bills and vendor payments.

Both resources are the same three operations against different parents, so the shared
base below resolves the owning document through ``_document_or_404`` - the same
entity-and-branch choke point every other procurement detail endpoint uses - and each
subclass supplies only its queryset, permission prefix, and not-found wording.

Reading an attachment list needs the document's ``view`` verb; adding or removing one
needs a dedicated ``attach`` verb rather than ``update``. ``update`` is the wrong gate
twice over: it is refused on a posted document, and posted documents are exactly when
the supplier's paper arrives - and it conflates rewriting a bill's amounts with filing
its evidence, which are not the same authority.
"""
from __future__ import annotations

from rest_framework.exceptions import MethodNotAllowed, ValidationError

from core.response import success_response
from vs_finance.views import resolve_entity

from .. import attachments as attachment_service
from ..models import VendorInvoice, VendorPayment
from .base import _ProcBase, _document_or_404


class _AttachmentBase(_ProcBase):
    """Resolve the owning document under entity/branch isolation, then act on its files."""

    #: ``procurement.<resource>`` - the two verbs are derived from it.
    permission_prefix = ""
    not_found_message = "No such document in this entity."

    @property
    def rbac_permission(self):
        """Read the evidence with the document's view verb; change it with attach."""
        verb = "view" if self.request.method == "GET" else "attach"
        return f"{self.permission_prefix}.{verb}"

    def _queryset(self, entity):
        raise NotImplementedError

    def _document(self, request, pk):
        entity = resolve_entity(request)
        return _document_or_404(
            request, self._queryset(entity), pk, self.not_found_message,
        )

    @staticmethod
    def _reject_id_mismatch(attachment_id, *, required):
        """Both routes resolve to this view, so each verb must police its own shape.

        Without this, a DELETE on the collection URL (or a POST on the detail URL)
        reaches a handler missing a positional argument and answers 500 instead of 405.
        """
        if required and attachment_id is None:
            raise MethodNotAllowed("DELETE", detail="Name the attachment to remove.")
        if not required and attachment_id is not None:
            raise MethodNotAllowed(
                "POST", detail="This action addresses the document, not one attachment.",
            )

    def get(self, request, pk, attachment_id=None):
        """List the document's attachments."""
        self._reject_id_mismatch(attachment_id, required=False)
        document = self._document(request, pk)
        return success_response(
            "Attachments retrieved.",
            data={"attachments": attachment_service.serialize_attachments(document)},
        )

    def post(self, request, pk, attachment_id=None):
        """Attach one file (multipart ``file``, optional ``caption``)."""
        self._reject_id_mismatch(attachment_id, required=False)
        document = self._document(request, pk)
        upload = request.FILES.get("file")
        if upload is None:
            raise ValidationError({"file": "A file is required."})
        row = attachment_service.add_attachment(
            document, upload,
            caption=request.data.get("caption", ""),
            actor_user=request.user,
        )
        return success_response("Attachment uploaded.", data=row, status=201)

    def delete(self, request, pk, attachment_id=None):
        """Remove one attachment from this document."""
        self._reject_id_mismatch(attachment_id, required=True)
        document = self._document(request, pk)
        attachment_service.remove_attachment(document, attachment_id)
        # The document was loaded with its attachments prefetched, so that cache still
        # holds the row we just deleted; re-read the relation rather than echo it back.
        return success_response(
            "Attachment removed.",
            data={"attachments": attachment_service.serialize_attachments(
                self._document(request, pk),
            )},
        )


class VendorInvoiceAttachmentView(_AttachmentBase):
    """GET (list) / POST (upload) / DELETE the supplier's own bill against ours.

    docstring-name: Vendor invoice attachments
    """

    permission_prefix = "procurement.vendor_invoice"
    not_found_message = "No such vendor invoice in this entity."

    def _queryset(self, entity):
        return VendorInvoice.objects.filter(entity=entity).prefetch_related(
            "attachments__uploaded_by",
        )


class VendorPaymentAttachmentView(_AttachmentBase):
    """GET (list) / POST (upload) / DELETE the receipt proving the money moved.

    docstring-name: Vendor payment attachments
    """

    permission_prefix = "procurement.vendor_payment"
    not_found_message = "No such vendor payment in this entity."

    def _queryset(self, entity):
        return VendorPayment.objects.filter(entity=entity).prefetch_related(
            "attachments__uploaded_by",
        )

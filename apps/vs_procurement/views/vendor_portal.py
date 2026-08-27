"""Public vendor quotation form and buyer controls for RFQ invitations."""
from __future__ import annotations

from django.db import transaction
from django.http import HttpResponse
from django.utils import timezone
from rest_framework import serializers, status
from rest_framework.exceptions import NotFound, ValidationError
from rest_framework.parsers import FormParser, MultiPartParser
from rest_framework.permissions import AllowAny
from rest_framework.views import APIView

from core.response import success_response
from core.models import StoredFile
from vs_finance.views import resolve_entity

from .. import vendor_portal
from ..constants import QuotationStatus, RfqInvitationStatus, RfqStatus
from ..models import RequestForQuotation, RfqAmendment, RfqInvitation, VendorQuotationAttachment
from ..serializers import RfqDetailSerializer
from .base import _ProcBase, _require_lines, _text


def _session(request) -> str:
    return str(request.headers.get("X-RFQ-Session") or "")


class _PublicRfqView(APIView):
    authentication_classes = []
    permission_classes = [AllowAny]
    tenant_param_required = False
    throttle_scope = "rfq_portal"

    def verified(self, request, token):
        return vendor_portal.invitation_from_session(token, _session(request))


class PublicRfqPreviewView(_PublicRfqView):
    def get(self, request, token):
        return success_response("Invitation retrieved.", data=vendor_portal.preview(token))


class PublicRfqLogoView(_PublicRfqView):
    """Serve the buying school's crest to a vendor who has no account.

    The vendor is authorised by holding a valid invitation token, and the file
    is chosen by the invitation rather than named by the caller - so there is no
    file reference here for anyone to tamper with or replay elsewhere. That is
    why this route can be public while ``/media/`` cannot.

    It takes the storage name straight from the issuer block. It used to slice
    it back out of the logo's URL, which quietly assumed that URL would stay a
    bare path for ever; it is signed and user-bound now, and there is no user.
    """

    def get(self, request, token):
        invitation = vendor_portal.invitation_from_token(token)
        issuer = vendor_portal._issuer_block(invitation.rfq.entity)
        name = str(issuer.get("logo_name") or "")
        if not name:
            raise NotFound("Brand logo not found.")
        row = StoredFile.objects.filter(name=name, revoked_at__isnull=True).first()
        if row is None:
            raise NotFound("Brand logo not found.")
        response = HttpResponse(bytes(row.content), content_type=row.content_type or "image/png")
        response["Content-Length"] = row.size
        response["Cache-Control"] = "private, max-age=3600"
        response["X-Content-Type-Options"] = "nosniff"
        return response


class PublicRfqRequestCodeView(_PublicRfqView):
    throttle_scope = "rfq_verification"

    def post(self, request, token):
        vendor_portal.request_verification_code(token, request.data.get("email", ""))
        return success_response("A verification code has been sent to the invitation email.")


class PublicRfqVerifyCodeView(_PublicRfqView):
    throttle_scope = "rfq_verification"

    def post(self, request, token):
        session_token, payload = vendor_portal.verify_code(
            token, request.data.get("email", ""), request.data.get("code", ""),
        )
        return success_response(
            "Email verified.", data={"session_token": session_token, "form": payload},
        )


class PublicRfqFormView(_PublicRfqView):
    def get(self, request, token):
        invitation, _ = self.verified(request, token)
        return success_response("Quotation form retrieved.", data=vendor_portal.form_payload(invitation))

    def patch(self, request, token):
        invitation, email = self.verified(request, token)
        return success_response(
            "Draft saved.", data=vendor_portal.save_draft(invitation, email, request.data),
        )


class PublicRfqSubmitView(_PublicRfqView):
    def post(self, request, token):
        invitation, email = self.verified(request, token)
        return success_response(
            "Quotation submitted.", data=vendor_portal.submit(invitation, email, token),
        )


class PublicRfqReviseView(_PublicRfqView):
    def post(self, request, token):
        invitation, _ = self.verified(request, token)
        return success_response(
            "Quotation reopened for revision.", data=vendor_portal.revise(invitation),
        )


class PublicRfqAcknowledgeView(_PublicRfqView):
    def post(self, request, token):
        invitation, _ = self.verified(request, token)
        return success_response(
            "RFQ amendment acknowledged.",
            data=vendor_portal.acknowledge_amendment(invitation),
        )


class PublicRfqDeclineView(_PublicRfqView):
    def post(self, request, token):
        invitation, _ = self.verified(request, token)
        return success_response(
            "RFQ declined.", data=vendor_portal.decline(invitation, request.data.get("reason", "")),
        )


class PublicRfqAttachmentView(_PublicRfqView):
    parser_classes = [MultiPartParser, FormParser]

    def post(self, request, token):
        invitation, email = self.verified(request, token)
        upload = request.FILES.get("file")
        if upload is None:
            raise ValidationError({"file": "Choose a file to upload."})
        return success_response(
            "Attachment uploaded.", data=vendor_portal.add_attachment(invitation, email, upload),
            status=status.HTTP_201_CREATED,
        )


class PublicRfqAttachmentDownloadView(_PublicRfqView):
    def get(self, request, token, attachment_id):
        invitation, _ = self.verified(request, token)
        attachment = VendorQuotationAttachment.objects.filter(
            pk=attachment_id, quotation__rfq=invitation.rfq,
            quotation__vendor=invitation.vendor,
        ).first()
        if attachment is None:
            raise NotFound("Attachment not found.")
        response = HttpResponse(attachment.file.read(), content_type=attachment.content_type)
        response["Content-Length"] = attachment.size
        response["X-Content-Type-Options"] = "nosniff"
        disposition = "inline" if attachment.content_type.startswith("image/") else "attachment"
        response["Content-Disposition"] = f'{disposition}; filename="{attachment.original_name}"'
        return response


class RfqInvitationResendView(_ProcBase):
    rbac_permission = "procurement.rfq.issue"

    def post(self, request, pk, invitation_id):
        entity = resolve_entity(request)
        invitation = RfqInvitation.objects.select_related(
            "rfq__entity__tenant", "vendor",
        ).filter(pk=invitation_id, rfq_id=pk, rfq__entity=entity).first()
        if invitation is None:
            raise NotFound("No such RFQ invitation in this entity.")
        vendor_portal.prepare_invitation(invitation)
        return success_response("Invitation resent.")


class RfqInvitationExtendView(_ProcBase):
    rbac_permission = "procurement.rfq.issue"

    @transaction.atomic
    def post(self, request, pk, invitation_id):
        entity = resolve_entity(request)
        invitation = RfqInvitation.objects.select_for_update().select_related(
            "rfq__entity__tenant", "vendor",
        ).filter(pk=invitation_id, rfq_id=pk, rfq__entity=entity).first()
        if invitation is None:
            raise NotFound("No such RFQ invitation in this entity.")
        deadline = serializers.DateTimeField().run_validation(request.data.get("deadline"))
        if deadline <= timezone.now():
            raise ValidationError({"deadline": "The extension must end in the future."})
        invitation.extended_deadline = deadline
        if invitation.status == RfqInvitationStatus.EXPIRED:
            invitation.status = RfqInvitationStatus.OPENED
        invitation.save(update_fields=["extended_deadline", "status", "updated_at"])
        raw = vendor_portal.make_invitation_token(invitation)
        for recipient in invitation.recipients.all():
            context = vendor_portal._recipient_context(invitation, recipient, raw)
            transaction.on_commit(lambda i=invitation, r=recipient, c=context: vendor_portal._safe_notify(
                event_key="procurement.rfq_deadline_extended", context=c,
                invitation=i, recipients=[r],
            ))
        return success_response("Vendor deadline extended.", data={
            "deadline": deadline, "deadline_display": vendor_portal.format_deadline(invitation),
        })


class RfqAmendmentCreateView(_ProcBase):
    rbac_permission = "procurement.rfq.issue"

    @transaction.atomic
    def post(self, request, pk):
        entity = resolve_entity(request)
        rfq = RequestForQuotation.objects.select_for_update().select_related(
            "entity__tenant",
        ).filter(pk=pk, entity=entity).first()
        if rfq is None:
            raise NotFound("No such RFQ in this entity.")
        if rfq.rfq_status != RfqStatus.ISSUED:
            raise ValidationError({"rfq": "Only an issued RFQ can be amended."})
        summary = _text(request.data.get("summary"), "summary", 500, required=True)
        response_required = bool(request.data.get("response_required", True))
        if "lines" in request.data and not response_required:
            raise ValidationError({
                "response_required": "A specification change must require a new vendor response.",
            })
        rfq.version += 1
        if "deadline" in request.data:
            rfq.response_due_at = serializers.DateTimeField().run_validation(request.data.get("deadline"))
            rfq.response_due_date = rfq.response_due_at.astimezone(
                vendor_portal._entity_timezone(entity),
            ).date()
        rfq.save(update_fields=["version", "response_due_at", "response_due_date", "updated_at"])
        if "lines" in request.data:
            from .orders import _write_rfq_lines
            _write_rfq_lines(entity, rfq, _require_lines(request.data), preserve_history=True)
        amendment = RfqAmendment.objects.create(
            rfq=rfq, version=rfq.version, summary=summary,
            response_required=response_required, published_at=timezone.now(),
            created_by=request.user,
        )
        invitations = list(
            rfq.invitations.select_for_update().select_related("vendor").prefetch_related("recipients")
        )
        for invitation in invitations:
            raw = vendor_portal.make_invitation_token(invitation)
            if response_required:
                quote = invitation.rfq.quotations.filter(
                    vendor=invitation.vendor, quotation_status=QuotationStatus.SUBMITTED,
                ).order_by("-id").first()
                if quote:
                    quote.quotation_status = QuotationStatus.DRAFT
                    quote.save(update_fields=["quotation_status", "updated_at"])
                    invitation.status = RfqInvitationStatus.DRAFTING
                    invitation.save(update_fields=["status", "updated_at"])
            for recipient in invitation.recipients.all():
                context = vendor_portal._recipient_context(invitation, recipient, raw) | {
                    "rfq_version": rfq.version,
                    "amendment_summary": amendment.summary,
                    "response_required": "Yes" if response_required else "No",
                }
                transaction.on_commit(lambda i=invitation, r=recipient, c=context: vendor_portal._safe_notify(
                    event_key="procurement.rfq_amended", context=c,
                    invitation=i, recipients=[r],
                ))
        from .orders import _rfq_detail_queryset
        detail = _rfq_detail_queryset(entity).get(pk=rfq.pk)
        return success_response(
            "RFQ amendment published.",
            data=RfqDetailSerializer(detail, context={"request": request}).data,
        )

"""Secure external vendor workflow for RFQ quotation collection."""
from __future__ import annotations

import hashlib
import json
import logging
import secrets
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from django.conf import settings
from django.contrib.auth.hashers import check_password, make_password
from django.core import signing
from django.core.serializers.json import DjangoJSONEncoder
from django.db import transaction
from django.utils import timezone
from rest_framework.exceptions import NotFound, ValidationError

from core.uploads import validate_upload
from vs_config.conf import get_config
from vs_finance.documents import _issuer_block
from vs_notifications.notify import UnregisteredRecipient, send_notification

from . import sourcing
from .constants import QuotationLineResponse, QuotationStatus, RfqInvitationStatus, RfqStatus
from .models import (
    RequestForQuotation,
    RfqInvitation,
    RfqInvitationRecipient,
    RfqInvitationSession,
    RfqInvitationVerification,
    VendorContact,
    VendorQuotation,
    VendorQuotationAttachment,
    VendorQuotationLine,
    VendorQuotationSubmission,
)
from .serializers import VendorPortalQuotationSerializer

logger = logging.getLogger(__name__)
MAX_ATTACHMENT_BYTES = 500 * 1024
MAX_ATTACHMENTS_PER_REVISION = 5
SESSION_HOURS = 24
CODE_MINUTES = 10
CODE_MAX_ATTEMPTS = 5


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _raw_token() -> str:
    return secrets.token_urlsafe(32)


def make_invitation_token(invitation: RfqInvitation) -> str:
    """Create a reproducible HMAC-signed link without storing a usable secret."""
    return signing.dumps(
        {"invitation": invitation.pk, "version": invitation.token_version},
        salt="vs_procurement.vendor_rfq",
        compress=True,
    )


def _entity_timezone(entity) -> ZoneInfo:
    name = get_config("display.timezone", "UTC", tenant=entity.tenant)
    try:
        return ZoneInfo(str(name or "UTC"))
    except ZoneInfoNotFoundError:
        return ZoneInfo("UTC")


def ensure_exact_deadline(rfq: RequestForQuotation) -> None:
    """Populate the exact deadline from a date-only input at 23:59:59 local time."""
    if rfq.response_due_at is not None or rfq.response_due_date is None:
        return
    local = datetime.combine(rfq.response_due_date, time(23, 59, 59), _entity_timezone(rfq.entity))
    rfq.response_due_at = local.astimezone(ZoneInfo("UTC"))
    rfq.save(update_fields=["response_due_at", "updated_at"])


def format_deadline(invitation: RfqInvitation) -> str:
    deadline = invitation.deadline
    if deadline is None:
        return "No deadline"
    return deadline.astimezone(_entity_timezone(invitation.rfq.entity)).strftime("%d %b %Y, %I:%M %p %Z")


def invitation_url(raw_token: str) -> str:
    return f"{str(settings.FRONTEND_BASE_URL).rstrip('/')}/vendor/rfq/{raw_token}"


def _safe_notify(*, event_key: str, context: dict, invitation: RfqInvitation, recipients=None) -> bool:
    """Keep sourcing durable if an external email provider is unavailable."""
    targets = recipients if recipients is not None else list(invitation.recipients.all())
    invited_emails = {
        str(email).strip().lower()
        for email in invitation.recipients.values_list("email", flat=True)
        if str(email).strip()
    }
    bcc = [
        str(email).strip()
        for email in getattr(settings, "PROCUREMENT_VENDOR_EMAIL_BCC", [])
        if str(email).strip() and str(email).strip().lower() not in invited_emails
    ]
    try:
        send_notification(
            event_key=event_key,
            context=context,
            recipients=[],
            tenant=invitation.rfq.entity.tenant,
            unregistered_recipients=[
                UnregisteredRecipient(email=row.email, name=row.name) for row in targets
            ],
            metadata={
                "rfq_id": invitation.rfq_id,
                "invitation_id": invitation.pk,
                "bcc": bcc,
            },
        )
        return True
    except Exception:
        logger.exception("Could not queue %s for RFQ invitation %s", event_key, invitation.pk)
        return False


def _recipient_context(invitation: RfqInvitation, recipient, raw_token: str) -> dict:
    issuer = _issuer_block(invitation.rfq.entity)
    return {
        "recipient_name": recipient.name or invitation.vendor.name,
        "issuer_name": issuer["name"],
        "vendor_name": invitation.vendor.name,
        "rfq_number": invitation.rfq.document_number,
        "rfq_title": invitation.rfq.title or "Request for quotation",
        "deadline": format_deadline(invitation),
        "invitation_url": invitation_url(raw_token),
    }


@transaction.atomic
def prepare_invitation(invitation: RfqInvitation, *, rotate_token: bool = True) -> str:
    """Snapshot active contacts, mint a token, and queue one email per recipient."""
    invitation = RfqInvitation.objects.select_for_update().select_related(
        "rfq__entity__tenant", "rfq__entity__base_currency", "vendor",
    ).get(pk=invitation.pk)
    ensure_exact_deadline(invitation.rfq)
    if rotate_token and invitation.status != RfqInvitationStatus.PENDING:
        invitation.token_version += 1
        invitation.sessions.filter(revoked_at__isnull=True).update(revoked_at=timezone.now())
    raw = make_invitation_token(invitation)
    contacts = list(invitation.vendor.contacts.filter(
        is_active=True, receives_rfqs=True,
    ).order_by("-is_primary", "id"))
    if not contacts and invitation.vendor.email:
        contact, _ = VendorContact.objects.get_or_create(
            vendor=invitation.vendor,
            email=invitation.vendor.email,
            defaults={"name": invitation.vendor.name, "is_primary": True},
        )
        contacts = [contact]
    invitation.recipients.all().delete()
    recipients = [
        RfqInvitationRecipient.objects.create(
            invitation=invitation, contact=contact,
            name=contact.name or invitation.vendor.name, email=contact.email,
        )
        for contact in contacts
    ]
    invitation.status = RfqInvitationStatus.SENT if recipients else RfqInvitationStatus.PENDING
    invitation.save(update_fields=["token_version", "status", "updated_at"])
    for recipient in recipients:
        context = _recipient_context(invitation, recipient, raw)
        transaction.on_commit(lambda i=invitation, r=recipient, c=context: _safe_notify(
            event_key="procurement.rfq_invitation", context=c, invitation=i, recipients=[r],
        ))
    return raw


def rotate_invitation_token(invitation: RfqInvitation) -> str:
    """Mint a replacement public link without changing recipients or sending mail."""
    invitation.token_version += 1
    invitation.save(update_fields=["token_version", "updated_at"])
    invitation.sessions.filter(revoked_at__isnull=True).update(revoked_at=timezone.now())
    return make_invitation_token(invitation)


def prepare_rfq_invitations(rfq: RequestForQuotation) -> None:
    for invitation in rfq.invitations.select_related("rfq__entity__tenant", "vendor"):
        prepare_invitation(invitation)


def invitation_from_token(raw_token: str, *, mark_opened: bool = False) -> RfqInvitation:
    if not raw_token:
        raise NotFound("This invitation link is invalid.")
    try:
        payload = signing.loads(raw_token, salt="vs_procurement.vendor_rfq")
        invitation_id = int(payload["invitation"])
        token_version = int(payload["version"])
    except (signing.BadSignature, KeyError, TypeError, ValueError):
        raise NotFound("This invitation link is invalid.")
    invitation = RfqInvitation.objects.select_related(
        "rfq__entity__tenant", "rfq__entity__base_currency", "vendor",
    ).filter(pk=invitation_id, token_version=token_version).first()
    if invitation is None or invitation.status == RfqInvitationStatus.REVOKED:
        raise NotFound("This invitation link is invalid.")
    if mark_opened and invitation.opened_at is None:
        invitation.opened_at = timezone.now()
        if invitation.status in (RfqInvitationStatus.PENDING, RfqInvitationStatus.SENT):
            invitation.status = RfqInvitationStatus.OPENED
        invitation.save(update_fields=["opened_at", "status", "updated_at"])
    return invitation


def is_deadline_passed(invitation: RfqInvitation) -> bool:
    return invitation.deadline is not None and timezone.now() > invitation.deadline


def preview(raw_token: str) -> dict:
    invitation = invitation_from_token(raw_token)
    issuer = _issuer_block(invitation.rfq.entity)
    quotation = _quotation(invitation)
    return {
        "issuer_name": issuer["name"],
        "logo_url": f"/v1/procurement/public/rfqs/{raw_token}/logo/" if issuer.get("logo") else "",
        "vendor_name": invitation.vendor.name,
        "rfq_number": invitation.rfq.document_number,
        "deadline": invitation.deadline,
        "deadline_display": format_deadline(invitation),
        "expired": is_deadline_passed(invitation),
        "has_submission": bool(quotation and quotation.submissions.exists()),
        "status": invitation.status,
    }


def is_live_recipient(invitation: RfqInvitation, email: str) -> bool:
    """Check the vendor's CURRENT contact list, not the invitation snapshot.

    ``RfqInvitationRecipient`` rows are a point-in-time record of who was mailed and
    only refresh when a buyer resends. Treating them as the authorisation source let a
    contact who had been deactivated, opted out of RFQs, or deleted outright keep
    requesting codes and reusing live sessions. Authorisation is therefore always read
    from :class:`VendorContact`, and the snapshot stays purely a delivery record.
    """
    return invitation.vendor.contacts.filter(
        email__iexact=str(email or "").strip(),
        is_active=True, receives_rfqs=True,
    ).exists()


def request_verification_code(raw_token: str, email: str) -> None:
    invitation = invitation_from_token(raw_token, mark_opened=True)
    normalized = str(email or "").strip().lower()
    recipient = invitation.recipients.filter(email__iexact=normalized).first()
    if recipient is None or not is_live_recipient(invitation, normalized):
        raise ValidationError({"email": "That email is not registered for this invitation."})
    code = f"{secrets.randbelow(1_000_000):06d}"
    RfqInvitationVerification.objects.filter(
        invitation=invitation, email__iexact=normalized, consumed_at__isnull=True,
    ).update(consumed_at=timezone.now())
    RfqInvitationVerification.objects.create(
        invitation=invitation, email=normalized, code_hash=make_password(code),
        expires_at=timezone.now() + timedelta(minutes=CODE_MINUTES),
    )
    context = _recipient_context(invitation, recipient, raw_token) | {
        "verification_code": code,
        "expiry_minutes": CODE_MINUTES,
    }
    transaction.on_commit(lambda: _safe_notify(
        event_key="procurement.rfq_verification_code", context=context,
        invitation=invitation, recipients=[recipient],
    ))


def verify_code(raw_token: str, email: str, code: str) -> tuple[str, dict]:
    invitation = invitation_from_token(raw_token)
    normalized = str(email or "").strip().lower()
    verification = RfqInvitationVerification.objects.filter(
        invitation=invitation, email__iexact=normalized,
        consumed_at__isnull=True, expires_at__gt=timezone.now(),
    ).order_by("-id").first()
    if verification is None or verification.attempts >= CODE_MAX_ATTEMPTS:
        raise ValidationError({"code": "The verification code is invalid or expired."})
    verification.attempts += 1
    verification.save(update_fields=["attempts", "updated_at"])
    if not check_password(str(code or ""), verification.code_hash):
        raise ValidationError({"code": "The verification code is invalid or expired."})
    verification.consumed_at = timezone.now()
    verification.save(update_fields=["consumed_at", "updated_at"])
    raw_session = _raw_token()
    now = timezone.now()
    RfqInvitationSession.objects.create(
        invitation=invitation, email=normalized, token_hash=_digest(raw_session),
        expires_at=now + timedelta(hours=SESSION_HOURS), last_seen_at=now,
    )
    return raw_session, form_payload(invitation)


def invitation_from_session(raw_token: str, raw_session: str) -> tuple[RfqInvitation, str]:
    invitation = invitation_from_token(raw_token)
    session = RfqInvitationSession.objects.filter(
        invitation=invitation, token_hash=_digest(raw_session or ""),
        revoked_at__isnull=True, expires_at__gt=timezone.now(),
    ).first()
    if session is None:
        raise ValidationError({"session": "Your verification has expired. Request a new email code."})
    # Revoking a contact must end access already in flight, not just block the next
    # code request - otherwise a withdrawn contact keeps up to 24 hours of write access.
    if not is_live_recipient(invitation, session.email):
        session.revoked_at = timezone.now()
        session.save(update_fields=["revoked_at", "updated_at"])
        raise ValidationError(
            {"session": "Your access to this invitation has been withdrawn by the buyer."},
        )
    session.last_seen_at = timezone.now()
    session.save(update_fields=["last_seen_at", "updated_at"])
    return invitation, session.email


def _quotation(invitation: RfqInvitation, *, create: bool = False) -> VendorQuotation | None:
    """Resolve the portal's own workspace for this invitation.

    Scoped to ``vendor_managed`` on purpose: a buyer may also key a quotation in by
    hand for the same vendor and RFQ, and that row is theirs. Without this filter the
    newest-first pick would hand the buyer's document to the external vendor to edit.
    """
    if create:
        RfqInvitation.objects.select_for_update().get(pk=invitation.pk)
    quote = invitation.rfq.quotations.filter(
        vendor=invitation.vendor, vendor_managed=True,
    ).order_by("-id").first()
    if quote is None and create:
        quote = VendorQuotation.objects.create(
            entity=invitation.rfq.entity, rfq=invitation.rfq, vendor=invitation.vendor,
            # The vendor portal is an external, token-authenticated surface with no
            # branch context of its own; the RFQ it answers is the only source.
            branch_id=invitation.rfq.branch_id,
            vendor_managed=True,
            quote_date=timezone.localdate(), currency=invitation.rfq.entity.base_currency,
            subtotal=0, tax_total=0, total=0,
        )
    return quote


def _locked_invitation(invitation: RfqInvitation) -> RfqInvitation:
    """Reload the RFQ and invitation under one consistent write lock order."""
    RequestForQuotation.objects.select_for_update().get(pk=invitation.rfq_id)
    return RfqInvitation.objects.select_for_update().select_related(
        "rfq__entity__tenant", "rfq__entity__base_currency", "vendor",
    ).get(pk=invitation.pk)


def _active_lines(invitation: RfqInvitation):
    return invitation.rfq.lines.filter(is_active=True).order_by("line_no", "id")


def form_payload(invitation: RfqInvitation) -> dict:
    issuer = _issuer_block(invitation.rfq.entity)
    quote = _quotation(invitation)
    latest_submission = quote.submissions.order_by("-revision").first() if quote else None
    expired = is_deadline_passed(invitation)
    return {
        "issuer": {
            **{key: issuer.get(key, "") for key in ("name", "tag", "address", "email", "phone", "website")},
            "logo_url": f"/v1/procurement/public/rfqs/{make_invitation_token(invitation)}/logo/" if issuer.get("logo") else "",
        },
        "vendor": {"name": invitation.vendor.name, "code": invitation.vendor.code},
        "rfq": {
            "number": invitation.rfq.document_number,
            "title": invitation.rfq.title,
            "notes": invitation.rfq.notes,
            "version": invitation.rfq.version,
            "deadline": invitation.deadline,
            "deadline_display": format_deadline(invitation),
            "currency": invitation.rfq.entity.base_currency_id,
            "lines": [
                {"id": line.id, "line_no": line.line_no, "description": line.description,
                 "quantity": str(line.quantity)} for line in _active_lines(invitation)
            ],
            "amendments": [
                {"version": row.version, "summary": row.summary,
                 "response_required": row.response_required, "published_at": row.published_at}
                for row in invitation.rfq.amendments.all()
            ],
        },
        "invitation": {
            "status": invitation.status,
            "expired": expired,
            "can_edit": not expired and bool(quote is None or quote.quotation_status == QuotationStatus.DRAFT),
            "can_revise": not expired and bool(quote and quote.quotation_status == QuotationStatus.SUBMITTED),
            "decline_reason": invitation.decline_reason,
            "acknowledged_version": invitation.acknowledged_version,
            "requires_acknowledgement": invitation.acknowledged_version < invitation.rfq.version,
        },
        "quotation": VendorPortalQuotationSerializer(quote).data if quote else None,
        "latest_submission": latest_submission.snapshot if latest_submission else None,
        "submission_revision": latest_submission.revision if latest_submission else 0,
        "attachments": [
            {"id": row.id, "name": row.original_name, "content_type": row.content_type,
             "size": row.size, "revision": row.revision}
            for row in (quote.attachments.order_by("revision", "id") if quote else [])
        ],
    }


def _money(value, field: str) -> int:
    try:
        amount = int(value or 0)
    except (TypeError, ValueError):
        raise ValidationError({field: "Enter an amount in the smallest currency unit."})
    if amount < 0:
        raise ValidationError({field: "Amount cannot be negative."})
    return amount


@transaction.atomic
def save_draft(invitation: RfqInvitation, email: str, body: dict) -> dict:
    invitation = _locked_invitation(invitation)
    if is_deadline_passed(invitation):
        raise ValidationError({"deadline": "The quotation deadline has passed."})
    if invitation.rfq.rfq_status != RfqStatus.ISSUED:
        raise ValidationError({"rfq": "This RFQ is no longer accepting responses."})
    quote = _quotation(invitation, create=True)
    if quote.quotation_status != QuotationStatus.DRAFT:
        raise ValidationError({"quotation": "Request a revision before editing a submitted quotation."})
    quote.quote_date = timezone.localdate()
    if "valid_until" in body:
        raw_valid_until = body.get("valid_until")
        try:
            quote.valid_until = date.fromisoformat(str(raw_valid_until)) if raw_valid_until else None
        except ValueError:
            raise ValidationError({"valid_until": "Enter a valid date."})
        if quote.valid_until and quote.valid_until < quote.quote_date:
            raise ValidationError({"valid_until": "Valid-until date cannot be before today."})
    if "lead_time_days" in body:
        value = body.get("lead_time_days")
        try:
            quote.lead_time_days = None if value in (None, "") else max(0, min(int(value), 3650))
        except (TypeError, ValueError):
            raise ValidationError({"lead_time_days": "Enter a whole number of days."})
    quote.reference = str(body.get("reference", quote.reference) or "").strip()[:64]
    quote.notes = str(body.get("notes", quote.notes) or "").strip()[:255]
    quote.save(update_fields=[
        "quote_date", "valid_until", "lead_time_days", "reference", "notes", "updated_at",
    ])
    if "lines" in body:
        active = {line.pk: line for line in _active_lines(invitation)}
        rows = body.get("lines")
        if not isinstance(rows, list):
            raise ValidationError({"lines": "Expected a list of quotation lines."})
        quote.lines.all().delete()
        seen = set()
        for position, row in enumerate(rows, start=1):
            try:
                rfq_line_id = int(row.get("rfq_line"))
            except (TypeError, ValueError):
                raise ValidationError({"lines": "Every response must identify an RFQ line."})
            if rfq_line_id not in active or rfq_line_id in seen:
                raise ValidationError({"lines": "Each current RFQ line may be answered once."})
            seen.add(rfq_line_id)
            rfq_line = active[rfq_line_id]
            response_type = str(row.get("response_type") or QuotationLineResponse.QUOTED)
            if response_type not in QuotationLineResponse.values:
                raise ValidationError({"response_type": "Choose quoted, alternative, or no-bid."})
            quantity = Decimal(str(row.get("quantity") or rfq_line.quantity))
            if quantity <= 0:
                raise ValidationError({"quantity": "Quantity must be greater than zero."})
            price = 0 if response_type == QuotationLineResponse.NO_BID else _money(
                row.get("unit_price"), "unit_price",
            )
            VendorQuotationLine.objects.create(
                quotation=quote, rfq_line=rfq_line,
                alternative_for=rfq_line if response_type == QuotationLineResponse.ALTERNATIVE else None,
                response_type=response_type, line_no=position,
                description=str(row.get("description") or rfq_line.description).strip()[:255],
                expense_account=rfq_line.expense_account, quantity=quantity,
                unit_price=price, tax_code=rfq_line.tax_code,
                net_amount=0, tax_amount=0,
            )
    sourcing.price_quotation(quote)
    now = timezone.now()
    if invitation.draft_started_at is None:
        invitation.draft_started_at = now
    invitation.status = RfqInvitationStatus.DRAFTING
    invitation.save(update_fields=["draft_started_at", "status", "updated_at"])
    return form_payload(invitation)


def _snapshot(quote: VendorQuotation, revision: int, invitation: RfqInvitation) -> dict:
    data = dict(VendorPortalQuotationSerializer(quote).data)
    data["revision"] = revision
    data["rfq_version"] = invitation.rfq.version
    data["submitted_at"] = timezone.now().isoformat()
    data["attachments"] = [
        {"id": row.id, "name": row.original_name, "content_type": row.content_type,
         "size": row.size}
        for row in quote.attachments.filter(revision=revision).order_by("id")
    ]
    return json.loads(json.dumps(data, cls=DjangoJSONEncoder))


@transaction.atomic
def submit(invitation: RfqInvitation, email: str, raw_token: str) -> dict:
    invitation = _locked_invitation(invitation)
    if is_deadline_passed(invitation):
        raise ValidationError({"deadline": "The quotation deadline has passed."})
    if invitation.acknowledged_version < invitation.rfq.version:
        raise ValidationError({"amendment": "Acknowledge the latest RFQ amendment before submitting."})
    quote = _quotation(invitation)
    if quote is None or quote.quotation_status != QuotationStatus.DRAFT:
        raise ValidationError({"quotation": "There is no editable draft to submit."})
    active_ids = set(_active_lines(invitation).values_list("id", flat=True))
    answered_ids = set(quote.lines.filter(rfq_line_id__in=active_ids).values_list("rfq_line_id", flat=True))
    if active_ids != answered_ids:
        raise ValidationError({"lines": "Answer every requested line before submitting."})
    sourcing.submit_quotation(quote)
    revision = (quote.submissions.order_by("-revision").values_list("revision", flat=True).first() or 0) + 1
    now = timezone.now()
    snapshot = _snapshot(quote, revision, invitation)
    VendorQuotationSubmission.objects.create(
        quotation=quote, revision=revision, rfq_version=invitation.rfq.version,
        submitted_at=now, submitted_by_email=email, snapshot=snapshot,
    )
    invitation.status = RfqInvitationStatus.SUBMITTED
    invitation.submitted_at = now
    invitation.save(update_fields=["status", "submitted_at", "updated_at"])
    recipient = invitation.recipients.filter(email__iexact=email).first()
    if recipient:
        context = _recipient_context(invitation, recipient, raw_token) | {
            "quotation_number": quote.document_number,
            "revision": revision,
            "submitted_at": now.astimezone(_entity_timezone(invitation.rfq.entity)).strftime("%d %b %Y, %I:%M %p %Z"),
        }
        transaction.on_commit(lambda: _safe_notify(
            event_key="procurement.quotation_receipt", context=context,
            invitation=invitation, recipients=[recipient],
        ))
    return form_payload(invitation)


@transaction.atomic
def revise(invitation: RfqInvitation) -> dict:
    invitation = _locked_invitation(invitation)
    if is_deadline_passed(invitation):
        raise ValidationError({"deadline": "The quotation deadline has passed."})
    quote = _quotation(invitation)
    if quote is None or quote.quotation_status != QuotationStatus.SUBMITTED:
        raise ValidationError({"quotation": "Only a submitted quotation can be revised."})
    quote.quotation_status = QuotationStatus.DRAFT
    quote.save(update_fields=["quotation_status", "updated_at"])
    invitation.status = RfqInvitationStatus.DRAFTING
    invitation.save(update_fields=["status", "updated_at"])
    return form_payload(invitation)


@transaction.atomic
def acknowledge_amendment(invitation: RfqInvitation) -> dict:
    invitation = _locked_invitation(invitation)
    invitation.acknowledged_version = invitation.rfq.version
    invitation.save(update_fields=["acknowledged_version", "updated_at"])
    return form_payload(invitation)


@transaction.atomic
def decline(invitation: RfqInvitation, reason: str) -> dict:
    invitation = _locked_invitation(invitation)
    if is_deadline_passed(invitation):
        raise ValidationError({"deadline": "The quotation deadline has passed."})
    quote = _quotation(invitation)
    if quote and quote.quotation_status == QuotationStatus.SUBMITTED:
        raise ValidationError({"quotation": "Request a revision before declining this RFQ."})
    invitation.status = RfqInvitationStatus.DECLINED
    invitation.declined_at = timezone.now()
    invitation.decline_reason = str(reason or "").strip()[:500]
    invitation.save(update_fields=["status", "declined_at", "decline_reason", "updated_at"])
    return form_payload(invitation)


def validate_attachment(upload) -> tuple[str, str]:
    """Portal policy on top of the shared upload check: the same file types, a
    deliberately tighter 500KB ceiling because this endpoint is public and
    token-authenticated rather than RBAC-gated."""
    return validate_upload(
        upload,
        max_bytes=MAX_ATTACHMENT_BYTES,
        size_message="Each attachment must be 500KB or smaller.",
        type_message="Upload a PDF, PNG, JPG, JPEG, or WebP file.",
    )


@transaction.atomic
def add_attachment(invitation: RfqInvitation, email: str, upload) -> dict:
    invitation = _locked_invitation(invitation)
    if is_deadline_passed(invitation):
        raise ValidationError({"deadline": "The quotation deadline has passed."})
    quote = _quotation(invitation, create=True)
    if quote.quotation_status != QuotationStatus.DRAFT:
        raise ValidationError({"quotation": "Submitted attachments are read-only."})
    revision = (quote.submissions.order_by("-revision").values_list("revision", flat=True).first() or 0) + 1
    if quote.attachments.filter(revision=revision).count() >= MAX_ATTACHMENTS_PER_REVISION:
        raise ValidationError({"file": "A quotation may have up to five attachments."})
    name, content_type = validate_attachment(upload)
    VendorQuotationAttachment.objects.create(
        quotation=quote, revision=revision, file=upload, original_name=name,
        content_type=content_type, size=upload.size, uploaded_by_email=email,
    )
    return form_payload(invitation)

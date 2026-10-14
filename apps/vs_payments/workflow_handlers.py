"""vs_workflow handler for approval-gating bulk payout batches.

A :class:`~vs_payments.models.PayoutBatch` disburses money *out* to one or more
beneficiaries, so it is a high-risk cash-out path. Every payout enters this handler,
including a single payout represented as a one-line batch. Provider submission happens
only from terminal approval; a missing template fails closed before this callback.

Unlike a finance document, a payout batch is **not** a GL posting - approval gates
the *provider submission* (:func:`vs_payments.services.submit_payout_batch`), which
dispatches the batch's pending instructions to the PSP. The batch's own
``PayoutBatchStatus`` has no approval states, so we track the approval phase in
``metadata["approval_status"]`` and leave the batch ``DRAFT`` until it is approved
and submitted (at which point the service moves it to ``PROCESSING``).

Approval and dispatch are deliberately **not** the same transaction: see
:meth:`PayoutBatchApprovalHandler.on_approved`. Approval commits, and only then is the
provider called - from a worker, off ``transaction.on_commit``.

Auto-discovered by the engine on startup via ``autodiscover_modules("workflow_handlers")``.
"""
from __future__ import annotations

import logging
from urllib.parse import urlencode

from django.db import transaction

from vs_workflow.constants import DocumentAudience
from vs_workflow.constants import WorkflowStageAction as StageActionEnum
from vs_workflow.exceptions import InvalidInstanceStateError, ReversalNotAllowedError
from vs_workflow.handlers import BaseWorkflowHandler, register_handler
from vs_workflow.presentation import document_details, fields_section, table_section

logger = logging.getLogger("vs_payments.workflow_handlers")  # Diagnostics for the dispatch hand-off.


def _masked_account(number: str) -> str:
    """Keep only the final four account digits in the approval snapshot."""
    text = str(number or "")
    return f"•••• {text[-4:]}" if text else "-"


# Support the post-approval dispatch hand-off.
def _enqueue_dispatch(batch_id, instance_id, actor_id) -> None:
    """Hand an approved batch to a worker (local import avoids a task-discovery cycle)."""
    from .tasks import dispatch_payout_batch

    try:
        dispatch_payout_batch.delay(batch_id, instance_id, actor_id)  # Inline under ALWAYS_EAGER.
    except Exception:  # noqa: BLE001 - the approval is already committed; do not lose the batch.
        # Never let a broker problem propagate out of an on_commit hook: the approval is
        # durable, the batch is findable, and the sweep task re-dispatches it.
        logger.exception("Could not enqueue dispatch for payout batch %s.", batch_id)


@register_handler("payments.payout_batch")
class PayoutBatchApprovalHandler(BaseWorkflowHandler):
    """Approval handler for a bulk :class:`~vs_payments.models.PayoutBatch`."""
    noun = "Payout batch"

    # Both kinds of tenant disburse. A school pays its own vendors, is provisioned
    # with an approval path for doing so, and assembles a batch against its own
    # entity through the same endpoint the platform uses.
    audience = DocumentAudience.ALL

    allows_continue_without_approval = False

    # --- helpers ------------------------------------------------------------ #
    @property
    def document_model(self):  # Concrete model the engine's object_id points at.
        from .models import PayoutBatch
        return PayoutBatch

    def _load(self, instance):  # Row-lock the batch for a mutation.
        return self.document_model.objects.select_for_update().get(pk=instance.document_object_id)

    def _final_approver(self, instance):
        """The user whose approving vote completed the workflow (the checker).

        The engine's ``on_approved`` context does not carry the acting user, so we read
        it back from the immutable action log - the most recent non-reversed APPROVED
        vote on this instance, visible in the same transaction that recorded it. Returns
        ``None`` when no human voted; the dispatch service refuses that state.
        """
        from vs_workflow.models import WorkflowStageAction

        action = (
            WorkflowStageAction.objects
            .filter(stage_instance__instance=instance,
                    action=StageActionEnum.APPROVED,
                    reversed_at__isnull=True, is_reversal_of__isnull=True)
            .select_related("actor")
            .order_by("-acted_at", "-id")
            .first()
        )
        return action.actor if action is not None else None

    def _set_approval_status(self, instance, value):  # Record the approval phase in metadata.
        with transaction.atomic():
            batch = self._load(instance)
            meta = dict(batch.metadata or {})  # Copy so we never mutate in place.
            meta["approval_status"] = value  # PENDING_APPROVAL / APPROVED / DRAFT.
            batch.metadata = meta
            batch.save(update_fields=["metadata", "updated_at"])

    # --- engine entry points ------------------------------------------------ #
    def hidden_document_ids(self, user, tenant):
        """The batches of another branch, or of none yet, kept from a branch-bound ``user``.

        A batch is a transaction naming the one branch its lines pay from, so it is
        read exclusively, as the payout screens read it
        (:class:`vs_payments.reach.PaymentsReach`): a batch paid from Lekki's bank is
        absent from an Ikeja-only approver's inbox, and approving or rejecting it
        answers 404. The engine reads an instance with no branch as the school's,
        which is wrong for a batch not yet given one, so this answers instead.
        """
        from vs_workflow.services.visibility import documents_outside_transaction_reach

        return documents_outside_transaction_reach(
            self.document_model.objects.filter(entity__tenant=tenant), user, tenant,
        )

    def resolve_default_template_code(self, document) -> str:
        """The platform's settlement route for a settlement batch, the standard ladder otherwise."""
        from .constants import WF_SETTLEMENT_TEMPLATE_CODE, PayoutPurpose

        if document.purpose == PayoutPurpose.SETTLEMENT:
            return WF_SETTLEMENT_TEMPLATE_CODE
        return "standard"

    def validate_document(self, document, requested_by) -> None:
        """Reject anything that could not actually be submitted.

        Run at submission time so a doomed batch is refused before approvers spend
        effort: it must be a DRAFT batch that still has at least one PENDING
        instruction to dispatch.
        """
        from .constants import PayoutBatchStatus, PayoutStatus

        if document.status != PayoutBatchStatus.DRAFT:  # Only a draft batch can be gated.
            raise InvalidInstanceStateError("Only a draft payout batch can be submitted for approval.")
        if not document.instructions.filter(status=PayoutStatus.PENDING).exists():  # Nothing to dispatch.
            raise InvalidInstanceStateError("This batch has no pending instructions to submit.")

    def get_document_summary(self, document) -> dict:
        from vs_finance.money import format_naira

        return {  # Curated snapshot for the approval screen.
            "title": document.reference,  # The batch reference.
            "subtitle": "Bulk payout batch",  # Human label.
            "fields": [
                {"label": "Items", "value": str(document.item_count)},  # Number of beneficiaries.
                {"label": "Total", "value": format_naira(document.total_amount)},  # Total disbursed.
            ],
            "link": f"/finance/payments/batches?{urlencode({'document': document.pk, 'entity': document.entity.code})}",
        }

    def get_document_details(self, document) -> dict:
        """Show each beneficiary without storing complete account numbers.

        The beneficiary and the account are registered fields of
        ``payments.payout``, so the two columns carry their keys and an
        approver whose roles cannot read those fields is shown the batch
        without them. A line that pays a vendor restates the vendor's banking
        as well, so its columns also carry the vendor's matching bank keys
        (:mod:`vs_payments.payout_access`). Those lines sit in a table of their
        own whenever the batch also holds lines to the school's own bank, because
        a column has one set of keys. The account is truncated to its last
        four digits here whoever reads it: an approval document is kept for as
        long as the approval is, and a complete account number does not need
        to be.
        """
        from vs_finance.money import format_naira

        from .payout_access import beneficiary_access, is_vendor_payout

        instructions = list(document.instructions.order_by("id"))
        groups = [
            (vendor_backed, [row for row in instructions if is_vendor_payout(row) == vendor_backed])
            for vendor_backed in (True, False)
        ]
        groups = [(vendor_backed, rows) for vendor_backed, rows in groups if rows] or [(False, [])]
        titles = {True: "Vendor beneficiaries", False: "Other beneficiaries"}

        def table(vendor_backed, rows):
            return table_section(
                titles[vendor_backed] if len(groups) > 1 else "Beneficiaries",
                [
                    ("beneficiary", "Beneficiary",
                     beneficiary_access("beneficiary_name", vendor_backed=vendor_backed)),
                    ("account", "Account",
                     beneficiary_access("beneficiary_account_number", vendor_backed=vendor_backed)),
                    ("amount", "Amount"),
                    ("narration", "Narration"),
                ],
                [
                    {
                        "beneficiary": row.beneficiary_name,
                        "account": _masked_account(row.beneficiary_account_number),
                        "amount": format_naira(row.amount),
                        "narration": row.narration or "-",
                    }
                    for row in rows
                ],
            )

        return document_details(
            fields_section("Batch details", [
                ("Provider", document.get_provider_display()),
                ("Narration", document.narration),
                ("Currency", getattr(document.currency, "code", "") or "-"),
                ("Source account", str(document.source_account or "-")),
            ]),
            *[table(vendor_backed, rows) for vendor_backed, rows in groups],
        )

    def on_submitted(self, instance, context) -> None:
        self._set_approval_status(instance, "PENDING_APPROVAL")  # Batch stays DRAFT, awaiting approval.

    def on_approved(self, instance, context) -> None:
        """Final approval records the decision; the provider is called after commit.

        This callback runs inside ``record_action``'s atomic block, and a bank transfer
        is not something that block can roll back. Dispatching from here would mean
        that a deadlock or a later failure anywhere in the approval transaction could
        undo the batch back to an undispatched DRAFT *after* the provider had already
        moved the money - money gone, no local record of it, and the next run free to
        send it again.

        So approval only marks the batch, and ``transaction.on_commit`` hands the
        dispatch to a worker once that decision is durably committed. If the enqueue
        never lands, ``vs_payments.dispatch_undispatched_payout_batches`` finds the
        batch and sends it.

        What *does* stay in this transaction is every check that costs nothing: stale
        vendor bank details, drifted totals, too few distinct human votes. Those still
        refuse the approval outright, so the approver is told why while they are still
        looking at the screen - rather than approving a batch that could only ever fail
        in a worker ten minutes later.
        """
        from .services import validate_payout_batch_dispatch

        batch = self._load(instance)  # Row-locked batch.
        validate_payout_batch_dispatch(batch, approved_instance=instance)  # Sends nothing.
        meta = dict(batch.metadata or {})
        meta["approval_status"] = "APPROVED"  # Record that approval completed.
        batch.metadata = meta
        batch.save(update_fields=["metadata", "updated_at"])

        # Read the deciding vote here: the action row is visible in this transaction.
        approver = self._final_approver(instance)
        batch_id = batch.pk
        # Stringify the ids: the broker serializer is JSON, and a WorkflowInstance pk
        # is a UUID. The service re-reads both, so string lookups are equivalent.
        instance_id = str(instance.pk)
        actor_id = str(approver.pk) if approver is not None else None
        transaction.on_commit(
            lambda: _enqueue_dispatch(batch_id, instance_id, actor_id),
        )

    def on_rejected(self, instance, context) -> None:
        self._set_approval_status(instance, "DRAFT")  # Back to a plain draft.

    def on_returned(self, instance, context) -> None:
        self._set_approval_status(instance, "DRAFT")  # Requester amends and resubmits.

    def validate_reversal(self, instance, context) -> None:
        """Refuse the reversal once any instruction has been claimed for the provider.

        For every other document type, undoing an approval is the whole of the
        change. A payout is different: approval hands the batch to a worker, and
        from the moment an instruction is claimed the money is either with the
        provider or on its way to the beneficiary. Reopening the approval then
        produces a batch that reads as awaiting a decision while it is being paid,
        and an administrator looking at "In progress" has no way to know the
        transfer already left. A payout that has gone out is unwound as a payout,
        by settling or reversing the instructions themselves.

        Locking the instructions is what makes this hold rather than usually hold.
        A dispatch claiming a row takes the same lock and re-checks the approval
        inside that claim, so the two serialise: a reversal that arrives first
        stops the send, and a send that starts first refuses the reversal.
        """
        from .constants import PayoutBatchStatus, PayoutStatus

        batch = self._load(instance)  # Row-locked batch.
        statuses = set(  # Locks every instruction, not only the ones read back.
            batch.instructions.select_for_update().values_list("status", flat=True)
        )
        if (statuses - {PayoutStatus.PENDING}
                or batch.status != PayoutBatchStatus.DRAFT
                or batch.submitted_at is not None):
            raise ReversalNotAllowedError(
                "This payout batch has already been sent to the provider, so its "
                "approval cannot be undone. Settle or reverse the payouts "
                "themselves instead.",
                batch_reference=batch.reference,
                batch_status=batch.status,
            )

    def on_action_reversed(self, instance, context) -> None:
        """Return an undispatched batch to the queue it was approved out of.

        ``validate_reversal`` has already refused anything that reached the
        provider, so what is left is a batch still sitting on pending
        instructions. Its ``approval_status`` marker still says APPROVED, and the
        sweep that re-dispatches lost hand-offs selects on exactly that marker;
        only the instance status stops it. Putting the marker back keeps the two
        records agreeing, so a batch whose approval was undone does not depend on
        a single check to stay unsent.
        """
        self._set_approval_status(instance, "PENDING_APPROVAL")  # Awaiting a decision again.


def _topup_key(name: str) -> str:
    """The Field Access key of the registered field ``name`` of ``payments.platform_topup``."""
    return f"payments.platform_topup.{name}"


@register_handler("payments.provider_topup")
class ProviderTopUpApprovalHandler(BaseWorkflowHandler):
    """Approval handler for a :class:`~vs_payments.models.ProviderBalanceTopUp`.

    The platform pays its own money from its bank into its provider balance, so
    only the platform raises one (``audience``) and two platform people approve
    it. The booking happens inside ``on_approved`` and nowhere else: a posting
    that fails there (a closed month, an inactive account) rolls the approving
    vote back and leaves the stage waiting, with the reason in front of the
    approver, so nothing is ever approved without being booked. The same hook
    refuses a decision that does not rest on two real people besides the
    requester (:func:`vs_payments.topups.require_distinct_approvers`), whatever
    steps the route holds.
    """

    noun = "Provider balance top-up"
    audience = DocumentAudience.PLATFORM
    allows_continue_without_approval = False

    @property
    def document_model(self):
        from .models import ProviderBalanceTopUp

        return ProviderBalanceTopUp

    def resolve_default_template_code(self, document) -> str:
        from .constants import WF_TOPUP_TEMPLATE_CODE

        return WF_TOPUP_TEMPLATE_CODE

    def _load(self, instance):
        """Row-lock the top-up an instance points at."""
        return self.document_model.objects.select_for_update().get(pk=instance.document_object_id)

    def _actor(self, instance, action):
        """The person behind the latest live ``action`` on ``instance``, or None."""
        from vs_workflow.models import WorkflowStageAction

        row = (WorkflowStageAction.objects
               .filter(stage_instance__instance=instance, action=action,
                       reversed_at__isnull=True, is_reversal_of__isnull=True)
               .select_related("actor").order_by("-acted_at", "-id").first())
        return row.actor if row is not None else None

    def _set_status(self, instance, status) -> None:
        with transaction.atomic():
            topup = self._load(instance)
            topup.status = status
            topup.save(update_fields=["status", "updated_at"])

    def hidden_document_ids(self, user, tenant):
        """The top-ups of branches outside a branch-bound ``user``'s reach.

        A top-up is a transaction naming the branch of the bank account it was
        paid from, so it is read exclusively as every transaction is.
        """
        from vs_workflow.services.visibility import documents_outside_transaction_reach

        return documents_outside_transaction_reach(
            self.document_model.objects.filter(entity__tenant=tenant), user, tenant,
        )

    def validate_document(self, document, requested_by) -> None:
        """Refuse anything but a draft whose figures the platform's books can take.

        Also refused while an earlier request for the same top-up is open, since a
        top-up an approver returned is resumed from the approvals screen and a
        second request would put two in front of the approvers.
        """
        from vs_finance.approvals import APPROVAL_PENDING, approval_states

        from . import topups
        from .constants import ProviderTopUpStatus

        if document.status != ProviderTopUpStatus.DRAFT:
            raise InvalidInstanceStateError("Only a draft top-up can be submitted for approval.")
        if document.pk and approval_states([document])[document.pk] == APPROVAL_PENDING:
            raise InvalidInstanceStateError(
                "This top-up already has an approval request open. Resume it from your "
                "approvals, or withdraw it first.")
        topups.preflight(document)

    def get_document_summary(self, document) -> dict:
        from vs_finance.money import format_naira
        from vs_workflow.presentation import summary_fields

        return {
            "title": f"Provider top-up {document.pk}",
            "subtitle": "Provider balance top-up",
            "fields": summary_fields([
                ("Amount", format_naira(document.amount), _topup_key("amount")),
                ("Date", document.topped_up_on.isoformat(), _topup_key("topped_up_on")),
                ("Paid from", document.bank_account.name, _topup_key("bank_account")),
            ]),
            "link": f"/payments/provider-top-ups?{urlencode({'document': document.pk})}",
        }

    def get_document_details(self, document) -> dict:
        from vs_finance.money import format_naira

        return document_details(fields_section("Top-up details", [
            ("Provider", document.get_provider_display(), _topup_key("provider")),
            ("Amount", format_naira(document.amount), _topup_key("amount")),
            ("Date", document.topped_up_on, _topup_key("topped_up_on")),
            ("Paid from", document.bank_account.name, _topup_key("bank_account")),
            ("Reference", document.reference or "-", _topup_key("reference")),
            ("Note", document.note or "-", _topup_key("note")),
        ]))

    def on_submitted(self, instance, context) -> None:
        from .constants import ProviderTopUpStatus

        self._set_status(instance, ProviderTopUpStatus.PENDING_APPROVAL)

    def on_resubmitted(self, instance, context) -> None:
        """Check a returned top-up again, as submitting it checks, before it goes back to its approvers."""
        from . import topups
        from .constants import ProviderTopUpStatus

        topup = self._load(instance)
        if topup.status != ProviderTopUpStatus.DRAFT:
            raise InvalidInstanceStateError("Only a draft top-up can have its request resumed.")
        topups.preflight(topup)
        topup.status = ProviderTopUpStatus.PENDING_APPROVAL
        topup.save(update_fields=["status", "updated_at"])

    def on_approved(self, instance, context) -> None:
        """Require two distinct approvers, then book the top-up as the approver who completed the decision."""
        from . import topups
        from .constants import ProviderTopUpStatus

        topups.require_distinct_approvers(instance)
        topup = self._load(instance)
        topup.status = ProviderTopUpStatus.APPROVED
        topup.save(update_fields=["status", "updated_at"])
        topups.post_topup(
            topup, actor_user=self._actor(instance, StageActionEnum.APPROVED) or instance.requested_by)

    def on_rejected(self, instance, context) -> None:
        from . import audit
        from .constants import PaymentAuditAction, ProviderTopUpStatus

        self._set_status(instance, ProviderTopUpStatus.REJECTED)
        topup = self._load(instance)
        audit.record(
            action=PaymentAuditAction.PROVIDER_TOPUP_REJECTED, entity=topup.entity,
            provider=topup.provider, reference=(topup.reference or f"TOPUP-{topup.pk}")[:64],
            actor_user=self._actor(instance, StageActionEnum.REJECTED),
            message="A provider balance top-up was rejected.",
            metadata={"topup_id": topup.pk, "amount": int(topup.amount), "branch_id": topup.branch_id},
        )

    def on_returned(self, instance, context) -> None:
        from .constants import ProviderTopUpStatus

        self._set_status(instance, ProviderTopUpStatus.DRAFT)

    def on_withdrawn(self, instance, context) -> None:
        self._back_to_draft(instance)

    def on_cancelled(self, instance, context) -> None:
        self._back_to_draft(instance)

    def _back_to_draft(self, instance) -> None:
        """Hand a top-up whose request ended without a decision back to its requester as a draft."""
        from .constants import ProviderTopUpStatus

        with transaction.atomic():
            topup = self._load(instance)
            if topup.status == ProviderTopUpStatus.PENDING_APPROVAL:
                topup.status = ProviderTopUpStatus.DRAFT
                topup.save(update_fields=["status", "updated_at"])

    def validate_reversal(self, instance, context) -> None:
        """Refuse once the top-up is booked: the journal is then undone in the books, not here."""
        from .constants import ProviderTopUpStatus

        topup = self._load(instance)
        if topup.status in (ProviderTopUpStatus.POSTED, ProviderTopUpStatus.APPROVED):
            raise ReversalNotAllowedError(
                "This top-up has already been booked, so its approval cannot be undone. "
                "Reverse its journal in the platform's books instead.",
                document_status=topup.status,
            )

    def on_action_reversed(self, instance, context) -> None:
        """Put a top-up whose rejection or return was undone back in front of its approvers."""
        from . import topups
        from .constants import ProviderTopUpStatus

        with transaction.atomic():
            topup = self._load(instance)
            if topup.status in (ProviderTopUpStatus.DRAFT, ProviderTopUpStatus.REJECTED):
                if context.get("was_returned"):
                    topups.preflight(topup)
                topup.status = ProviderTopUpStatus.PENDING_APPROVAL
                topup.save(update_fields=["status", "updated_at"])

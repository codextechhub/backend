"""Which gateway records a caller may read or act on: their branches' and the school-wide.

No gateway table carries a branch of its own. Each record hangs on a finance or
procurement row that does, and takes its reach from that row, with the same
inclusive reading finance gives those rows (the caller's own branches plus the
school-wide ones, :func:`vs_rbac.scoping.branch_scope` with
``include_shared=True``):

* a **collection** on its customer and on the invoice it settles; either may be
  absent, and an absent one restricts nothing;
* a **virtual account** on its customer;
* a **payout** on the vendor it pays (a loose ``vendor_source_id``, since this app
  does not hard-FK procurement); a payout naming no vendor is school-wide;
* a **payout batch** on every line in it: one line paying a vendor outside reach
  withholds the whole batch, because its detail lists every line;
* a **gateway action** (the transactions log) on the record whose reference it
  carries, and a **webhook event** on the collection or payout it matched;
* a **bank statement line** (settlement reconciliation) on its bank account.

Every payments list, detail, summary and status change starts from
:class:`PaymentsReach`, never from ``Model.objects``, so a new view cannot forget
the narrowing; ``tests_branch_reach`` asserts ``views.py`` holds no other route to
these tables. A row outside reach is therefore absent from a list, uncounted in a
summary, and a 404 on a detail or an action, exactly like a row that does not
exist. A caller who covers the whole school is not narrowed at all and gets the
same querysets, and the same SQL, as an entity filter alone.
"""
from __future__ import annotations

from django.db.models import CharField, Exists, OuterRef, Q
from django.db.models.fields.json import KeyTextTransform
from django.db.models.functions import Cast
from rest_framework.exceptions import NotFound

from vs_rbac.scoping import BranchScope, branch_scope, branch_scope_for_user

from .models import (
    CollectionIntent,
    PaymentEvent,
    PayoutBatch,
    PayoutInstruction,
    VirtualAccount,
    WebhookEvent,
)

#: The ``vendor_source_type`` a vendor-backed payout records.
VENDOR_SOURCE = "vs_procurement.Vendor"


class PaymentsReach:
    """The gateway records of one entity that one caller may see, per table.

    Build it with :meth:`for_request` in a view or :meth:`for_user` where only a
    user is at hand (an export). Each method returns a fresh entity-scoped
    queryset already narrowed, ready for further filters, aggregates or a pk.
    """

    __slots__ = ("entity", "scope")

    def __init__(self, entity, scope: BranchScope):
        if not scope.include_shared:
            raise ValueError("Gateway records read branches inclusively.")
        self.entity = entity
        self.scope = scope

    @classmethod
    def for_request(cls, request, entity) -> "PaymentsReach":
        return cls(entity, branch_scope(request, include_shared=True))

    @classmethod
    def for_user(cls, user, entity) -> "PaymentsReach":
        return cls(entity, branch_scope_for_user(
            user, include_shared=True, tenant=getattr(entity, "tenant", None)))

    @property
    def is_narrowed(self) -> bool:
        return self.scope.is_narrowed

    # -- the reach of each table, as a Q over its own rows ---------------------- #

    def _collection_q(self):
        return self.scope.q("customer__") & self.scope.q("invoice__")

    def _virtual_account_q(self):
        return self.scope.q("customer__")

    def _hidden_vendors(self):
        from vs_procurement.models import Vendor

        return (
            Vendor.objects.filter(entity=self.entity).exclude(self.scope.q())
            .annotate(_pk_text=Cast("pk", CharField()))
        )

    def _payout_hidden(self):
        """True for a payout whose vendor another branch keeps to itself."""
        vendor = self._hidden_vendors().filter(_pk_text=OuterRef("vendor_source_id"))
        return Q(vendor_source_type=VENDOR_SOURCE) & Exists(vendor)

    # -- the out-of-reach rows, for the tables that hang on them ---------------- #

    def _hidden_collections(self):
        return CollectionIntent.objects.filter(entity=self.entity).exclude(self._collection_q())

    def _hidden_payouts(self):
        return PayoutInstruction.objects.filter(entity=self.entity).filter(self._payout_hidden())

    def _hidden_virtual_accounts(self):
        return VirtualAccount.objects.filter(entity=self.entity).exclude(self._virtual_account_q())

    def hidden_batches(self):
        """The batches of this entity withheld from this caller, for a reader outside views.

        The approval inbox reads a batch through the workflow engine rather than
        through :meth:`batches`, and asks this for the ones to leave out (see
        :meth:`vs_payments.workflow_handlers.PayoutBatchApprovalHandler.hidden_document_ids`).
        """
        return PayoutBatch.objects.filter(entity=self.entity).filter(
            Exists(self._hidden_payouts().filter(batch=OuterRef("pk"))))

    # -- the querysets every view starts from ----------------------------------- #

    def collections(self):
        qs = CollectionIntent.objects.filter(entity=self.entity)
        return qs.filter(self._collection_q()) if self.is_narrowed else qs

    def virtual_accounts(self):
        qs = VirtualAccount.objects.filter(entity=self.entity)
        return qs.filter(self._virtual_account_q()) if self.is_narrowed else qs

    def payouts(self):
        qs = PayoutInstruction.objects.filter(entity=self.entity)
        return qs.exclude(self._payout_hidden()) if self.is_narrowed else qs

    def batches(self):
        qs = PayoutBatch.objects.filter(entity=self.entity)
        if not self.is_narrowed:
            return qs
        return qs.exclude(Exists(self._hidden_payouts().filter(batch=OuterRef("pk"))))

    def events(self):
        """The transactions log, less every action on a record outside reach.

        An action names its record by ``reference``: a collection's, a payout's or a
        batch's own, or a virtual account's provider reference. A virtual account's
        actions also carry ``metadata.virtual_account_id``, which is what attributes
        its creation, whose reference is the one-off request sent to the provider.
        An action naming no record (a rejected initiation that never wrote one)
        stays visible.
        """
        qs = PaymentEvent.objects.filter(entity=self.entity)
        if not self.is_narrowed:
            return qs
        ref = OuterRef("reference")
        return qs.annotate(
            _virtual_account=KeyTextTransform("virtual_account_id", "metadata"),
        ).exclude(
            Exists(self._hidden_collections().filter(reference=ref))
            | Exists(self._hidden_payouts().filter(reference=ref))
            | Exists(self.hidden_batches().filter(reference=ref))
            | Exists(self._hidden_virtual_accounts().exclude(provider_reference="")
                     .filter(provider_reference=ref))
            | Exists(self._hidden_virtual_accounts().annotate(_pk_text=Cast("pk", CharField()))
                     .filter(_pk_text=OuterRef("_virtual_account")))
        )

    def webhooks(self):
        """Provider events matched to this entity's collections or payouts in reach.

        A webhook event carries no entity of its own - it is a raw provider event,
        stored before anything knows what it concerns - so its tenancy and its
        branch both come from the record it matched. An event matched to nothing is
        never here, because showing one tenant an unattributable reference would
        leak another tenant's transaction; those are read at platform scope by
        :func:`vs_payments.views._unattributed_webhooks`, CX staff only.
        """
        qs = WebhookEvent.objects.filter(
            Q(collection__entity=self.entity) | Q(payout__entity=self.entity))
        if not self.is_narrowed:
            return qs
        return qs.exclude(
            Exists(self._hidden_collections().filter(pk=OuterRef("collection_id")))
            | Exists(self._hidden_payouts().filter(pk=OuterRef("payout_id")))
        )

    def bank_lines(self, qs):
        """Narrow bank statement lines to the bank accounts in reach, as finance does."""
        return self.scope.filter(qs, "bank_account__")

    # -- single rows ------------------------------------------------------------ #

    @staticmethod
    def get_or_404(qs, pk, message):
        """One row of a reach queryset by pk, or the 404 of a row that does not exist."""
        row = qs.filter(pk=pk).first()
        if row is None:
            raise NotFound(message)
        return row

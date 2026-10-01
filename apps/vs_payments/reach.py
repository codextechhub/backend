"""Which gateway records a caller may read or act on: their own branches'.

Every gateway record names the branch whose money it is, in its own ``branch``
column, and is read exclusively as every transaction is
(:func:`vs_rbac.scoping.transaction_branch_scope`): a branch-bound caller reaches
the records of their own branches only, and never one not yet given a branch. A
whole-school caller reaches them all, and can see what still needs a branch.

The column is set when the record is created, and it is the only thing reach
reads:

* a **collection** is the branch of the invoice it pays, else of its customer,
  else of the account it is deposited into
  (:func:`vs_payments.services.collection_branch_id`). The Okafor family is
  filed under Ikeja and pays a Lekki invoice online; the money is Lekki's, so
  Lekki's clerk reaches that collection and Ikeja's does not;
* a **virtual account** is the branch its deposits belong to
  (:func:`vs_payments.services.virtual_account_branch_id`);
* a **payout** is the branch whose bank account the money leaves
  (:func:`vs_payments.services.payout_branch_id`), whoever it pays. A payout
  from Lekki's bank is Lekki's, to a vendor filed under Ikeja or one every
  branch shares alike, so Lekki's clerk reaches it and Ikeja's does not;
* a **payout batch** is the one branch every line in it pays from, since a
  batch whose lines leave two branches' banks is refused when it is assembled
  (:func:`vs_payments.services.create_payout_batch`);
* a **gateway action** (the transactions log) is reached with the record whose
  reference it carries, and a **webhook event** with the collection or payout
  it matched;
* a **bank statement line** (settlement reconciliation) is reached with its
  bank account;
* a **held settlement** is the branch it pays. The money is held in the
  platform's books, but the settlement row sits in the tenant's: its ``entity``
  is the tenant's books, which record the money arriving, so a tenant reads its
  own settlements by entity as it reads its gateway records, and never another
  tenant's;
* a **held balance** is the branch whose money the platform holds. It carries a
  tenant rather than an entity, so it is read by the entity's tenant.

Every payments list, detail, summary and status change starts from
:class:`PaymentsReach`, never from ``Model.objects``, so a new view cannot forget
the narrowing; ``tests_branch_reach`` asserts ``views.py`` and ``views_custody.py``
hold no other route to these tables. A row outside reach is therefore absent from a list, uncounted in a
summary, and a 404 on a detail or an action, exactly like a row that does not
exist. A caller who covers the whole school is not narrowed at all and gets the
same querysets, and the same SQL, as an entity filter alone.
"""
from __future__ import annotations

from django.db.models import CharField, Exists, OuterRef, Q
from django.db.models.fields.json import KeyTextTransform
from django.db.models.functions import Cast
from rest_framework.exceptions import NotFound

from vs_rbac.scoping import (
    BranchScope, transaction_branch_scope, transaction_branch_scope_for_user,
)

from .models import (
    CollectionIntent,
    HeldBalance,
    HeldSettlement,
    PaymentEvent,
    PayoutBatch,
    PayoutInstruction,
    VirtualAccount,
    WebhookEvent,
)


class PaymentsReach:
    """The gateway records of one entity that one caller may see, per table.

    Build it with :meth:`for_request` in a view or :meth:`for_user` where only a
    user is at hand (an export). Each method returns a fresh entity-scoped
    queryset already narrowed, ready for further filters, aggregates or a pk.
    """

    __slots__ = ("entity", "scope")

    def __init__(self, entity, scope: BranchScope):
        if scope.is_narrowed and scope.include_shared:
            raise ValueError("Gateway records read branches exclusively.")
        self.entity = entity
        self.scope = scope

    @classmethod
    def for_request(cls, request, entity) -> "PaymentsReach":
        return cls(entity, transaction_branch_scope(request))

    @classmethod
    def for_user(cls, user, entity) -> "PaymentsReach":
        return cls(entity, transaction_branch_scope_for_user(
            user, tenant=getattr(entity, "tenant", None)))

    @property
    def is_narrowed(self) -> bool:
        return self.scope.is_narrowed

    def _out_of_reach(self, model):
        """This entity's rows of a gateway table that this caller does not reach.

        An unbranched row is among them, because ``exclude`` keeps a NULL that
        the exclusive ``branch_id IN (...)`` does not match.
        """
        return model.objects.filter(entity=self.entity).exclude(self.scope.q())

    # -- the querysets every view starts from ----------------------------------- #

    def collections(self):
        return self.scope.filter(CollectionIntent.objects.filter(entity=self.entity))

    def virtual_accounts(self):
        return self.scope.filter(VirtualAccount.objects.filter(entity=self.entity))

    def payouts(self):
        return self.scope.filter(PayoutInstruction.objects.filter(entity=self.entity))

    def batches(self):
        return self.scope.filter(PayoutBatch.objects.filter(entity=self.entity))

    def held_settlements(self):
        """The platform's settlements paying this tenant's branches what it held for them.

        A settlement's ``entity`` is the tenant's books, which record the money
        arriving, not the platform's books that send it. The entity filter alone
        therefore keeps every other tenant's settlements out, as it does for the
        gateway records. A settlement always names the branch it pays, so a
        branch-bound caller reaches their own branches' settlements and no other.
        """
        return self.scope.filter(HeldSettlement.objects.filter(entity=self.entity))

    def held_balances(self):
        """What the platform holds for each of this tenant's branches in reach.

        A held balance is one branch's money, so a branch-bound caller reaches
        their own branches' balances and never another branch's, nor a total
        that includes one.
        """
        return self.scope.filter(HeldBalance.objects.filter(tenant_id=self.entity.tenant_id))

    def branches(self):
        """The tenant's branches this caller reaches: every one for a whole-school caller."""
        from vs_tenants.models import Branch

        qs = Branch.all_objects.filter(tenant_id=self.entity.tenant_id)
        if not self.is_narrowed:
            return qs
        return qs.filter(pk__in=sorted(self.scope.branch_ids))

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
        virtual_accounts = self._out_of_reach(VirtualAccount)
        return qs.annotate(
            _virtual_account=KeyTextTransform("virtual_account_id", "metadata"),
        ).exclude(
            Exists(self._out_of_reach(CollectionIntent).filter(reference=ref))
            | Exists(self._out_of_reach(PayoutInstruction).filter(reference=ref))
            | Exists(self._out_of_reach(PayoutBatch).filter(reference=ref))
            | Exists(virtual_accounts.exclude(provider_reference="").filter(provider_reference=ref))
            | Exists(virtual_accounts.annotate(_pk_text=Cast("pk", CharField()))
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
        return qs.filter(
            (Q(collection__isnull=True) | self.scope.q("collection__"))
            & (Q(payout__isnull=True) | self.scope.q("payout__"))
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

"""Shared request parsing, entity-safe resolvers, and the RBAC-gated base view.

The helpers in this module are deliberately the narrow waist of procurement's
write API: endpoints pass user-supplied references through them before a model
or posting service sees the value.  Keeping tenant checks, money units, and
numeric bounds here makes those invariants consistent across document types.
"""
from __future__ import annotations

import datetime
from decimal import Decimal, InvalidOperation

from rest_framework.exceptions import NotFound, ValidationError
from rest_framework.views import APIView

from vs_rbac.permissions import HasRBACPermission, IsAuthenticatedAndActive
from vs_rbac.scoping import BranchScope, branch_scope
from vs_rbac.scoping import caller_branch_ids as _rbac_caller_branch_ids
from vs_rbac.scoping import inherited_branch_id as _rbac_inherited_branch_id
from vs_rbac.scoping import raised_branch as _rbac_raised_branch
from vs_rbac.scoping import resolve_branch as _rbac_resolve_branch
from vs_rbac.scoping import sole_caller_branch as _rbac_sole_caller_branch

from ..models import (
    Vendor,
)



# --------------------------------------------------------------------------- #
# Shared resolution helpers                                                   #
# --------------------------------------------------------------------------- #

def _resolve_account(entity, ref, field):
    """Resolve a GL account by **code** (e.g. "2100") or id within ``entity``.

    Codes in the Chart of Accounts are numeric strings, so we match on code *first*
    and only fall back to a primary-key lookup - otherwise "2100" would be mistaken
    for a row id. Returns ``None`` when ``ref`` is blank.
    """
    if ref in (None, ""):
        return None
    from vs_finance.models import Account

    qs = Account.objects.filter(entity=entity)
    acc = qs.filter(code=str(ref)).first()
    if acc is None and str(ref).isdigit():
        acc = qs.filter(pk=int(ref)).first()
    if acc is None:
        raise ValidationError({field: f"No account '{ref}' in this entity."})
    return acc


def _resolve_tax(entity, ref, field="tax_code"):
    """Resolve an optional tax code without allowing a cross-entity reference."""
    if ref in (None, ""):
        return None
    from vs_finance.models import TaxCode

    qs = TaxCode.objects.filter(entity=entity)
    tc = qs.filter(code=str(ref)).first()
    if tc is None and str(ref).isdigit():
        tc = qs.filter(pk=int(ref)).first()
    if tc is None:
        raise ValidationError({field: f"No tax code '{ref}' in this entity."})
    return tc


def _resolve_currency(entity, ref, field="currency"):
    """Resolve an optional global currency by canonical, case-insensitive code."""
    if ref in (None, ""):
        return None
    from vs_finance.models import Currency

    cur = Currency.objects.filter(code=str(ref).upper()).first()
    if cur is None:
        raise ValidationError({field: f"No currency '{ref}'."})
    return cur


def _resolve_cost_center(entity, ref, field="cost_center"):
    """Resolve an optional active cost centre by id/code inside ``entity``."""
    if ref in (None, ""):
        return None
    from vs_finance.models import CostCenter

    qs = CostCenter.objects.filter(entity=entity, is_active=True)
    cost_center = (
        qs.filter(pk=int(ref)).first() if str(ref).isdigit()
        else qs.filter(code=str(ref)).first()
    )
    if cost_center is None:
        raise ValidationError({field: "No such active cost centre in this entity."})
    return cost_center


# --------------------------------------------------------------------------- #
# Branch sub-scope                                                            #
# --------------------------------------------------------------------------- #
#
# Every finance document already carries an optional ``branch`` sub-scope inside
# its entity (``vs_finance.FinanceDocument.branch``).  These helpers are the
# single place procurement decides what goes in it, so the rules below hold for
# every document type rather than one endpoint at a time:
#
#   * a document that *starts* a chain captures the branch the person raising it
#     works in (:func:`_raised_branch`);
#   * a document that *continues* a chain takes the branch from its source
#     document and nothing else (:func:`_inherited_branch_id`);
#
#     Both of those rules were procurement's and are now the platform's, in
#     :mod:`vs_rbac.scoping`, because finance needed the identical two.  What is
#     left here are one-line adapters that supply ``entity.tenant`` - procurement
#     is entity-scoped, the rules are tenant-scoped - and procurement's own
#     reading of a null branch.  No policy lives in them; a second copy of "which
#     branch does this belong to" is exactly how two modules come to disagree
#     about the same school.
#
#   * every read narrows to the caller's branch, whether it is a list, a KPI
#     total, an analytics report or a single document.  One rule
#     (:class:`_BranchScope`) is rendered two ways: as a ``Q`` for a single
#     model (:func:`_branch_q`, and the wrappers :func:`_branch_scoped`,
#     :func:`_branch_visible`, :func:`_document_or_404`), and as the scope object
#     itself for a service that spans several models (:func:`_branch_scope`).
#
# An absent branch is a real, valid answer - the document belongs to the entity
# as a whole - and is never coerced or rejected.  A tenant with no branches at
# all therefore behaves exactly as it did before: every value here stays ``None``.
#
# Which branches a caller is entitled to is *not* decided here.  That answer is
# the platform's, given once by :func:`vs_rbac.scoping.caller_branch_ids` and
# rendered by :class:`vs_rbac.scoping.BranchScope`; procurement only chooses the
# exclusive reading of a null branch (see :class:`_BranchScope`) and adds the
# ``?branch=`` request filter on top.


#: The branches this caller may work in, or ``None`` for the whole entity.
#:
#: This was procurement's own function until the same answer was needed outside
#: procurement; it now lives in :mod:`vs_rbac.scoping` beside the grant lookup it
#: reads, and the name is kept here only so procurement's own call sites read as
#: they always did. It is the same object, not a copy - there is exactly one
#: implementation of "whose rows?" on the platform.
_caller_branch_ids = _rbac_caller_branch_ids


def _sole_caller_branch(request, entity):
    """:func:`vs_rbac.scoping.sole_caller_branch` for this entity's owning tenant."""
    return _rbac_sole_caller_branch(request, entity.tenant)


def _resolve_branch_reference(entity, ref, field="branch"):
    """:func:`vs_rbac.scoping.resolve_branch` for this entity's owning tenant.

    Procurement is entity-scoped and the rule is tenant-scoped; supplying
    ``entity.tenant`` is the whole of what this adds.
    """
    return _rbac_resolve_branch(entity.tenant, ref, field)


def _raised_branch(request, entity, body, *, field="branch"):
    """:func:`vs_rbac.scoping.raised_branch` for this entity's owning tenant.

    Procurement takes the strict reading of the ambiguous case: a caller bound to
    several branches who names none is asked which, rather than having the
    purchase filed against the entity as a whole.  That is the shared default, so
    it is not passed - see :func:`vs_rbac.scoping.raised_branch` for why the other
    reading exists and which kinds of row take it.
    """
    return _rbac_raised_branch(request, entity.tenant, body, field=field)


#: The branch id a downstream document takes from the source it continues.
#:
#: The same object as :func:`vs_rbac.scoping.inherited_branch_id`, not a copy -
#: procurement's exclusive reading of a null branch *is* that function's default,
#: so there is nothing here to adapt.  A document raised for the entity as a whole
#: is a scope of its own that a branch-pinned storekeeper is not in, so they may
#: not continue that chain either; that is the same reading :class:`_BranchScope`
#: takes on the read side, and the two must agree or a caller could be shown a
#: document they may not build on.  Finance spells out the opposite reading at its
#: own call site, for the opposite reason.
_inherited_branch_id = _rbac_inherited_branch_id


def _branch_filter_lookups(request, entity=None, params=None, *, field="branch"):
    """The ``?branch=`` **request filter**, resolved into plain lookup/value pairs.

    This is only the half of the rule the caller asks for.  The half they are *held*
    to - which branches their grants entitle them to at all - is
    :func:`vs_rbac.scoping.caller_branch_ids`, and the two are ANDed by every
    renderer below.

    A caller who is not bound to any branch sees the whole entity and may narrow it
    with ``?branch=<id>``, or with ``?branch=none`` for the documents raised for the
    entity as a whole.  Because the two halves are ANDed, a bound caller asking for
    somebody else's branch gets an empty answer rather than that branch's rows, while
    one asking for a branch that *is* theirs narrows to it.  An unknown branch is a 400
    rather than a silent empty page, so the filter cannot be used to probe ids in
    another tenant.

    ``params`` may be omitted (detail reads take no filter input); the result is then
    empty, which filters nothing at all.
    """
    raw = str((params.get(field) if params else "") or "").strip()
    if not raw:
        return {}
    if raw.lower() in ("none", "null"):
        return {f"{field}__isnull": True}
    return {field: _resolve_branch_reference(entity, raw, field)}


class _BranchScope:
    """One caller's branch narrowing plus their ``?branch=`` filter, per relation path.

    A list filters one model, so a single ``Q`` is enough for it.  A report service
    aggregates several models that reach ``branch`` by different routes (a payment
    allocation through ``payment__``, a receipt line through ``grn__``), so handing it a
    pre-built ``Q`` would force it to re-derive the rule for every other path - which is
    exactly the drift Round 3 removed.  It is handed this instead: the same resolved
    answer, re-rendered per path.

    The grant half is the platform-wide :class:`vs_rbac.scoping.BranchScope`, asked for
    in its **exclusive** form.  That is procurement's deliberate reading and not the
    platform default: a purchase raised with no branch belongs to the school as a
    whole, which is a scope of its own that a branch-pinned storekeeper is not in.
    A branch-pinned caller therefore does not inherit school-wide spend, matching
    :func:`_inherited_branch_id`, which refuses to let them continue an entity-wide
    chain either.  Elsewhere on the platform a null branch means *shared with every
    branch* and stays visible; see :class:`vs_rbac.scoping.BranchScope`.

    ``is_narrowed`` reports whether the caller is looking at less than the whole entity.
    It is deliberately the *only* thing that turns branch-specific fields on in a report
    payload, so an unbound caller and a tenant with no branches keep byte-identical
    responses.
    """

    __slots__ = ("_grant", "_lookups", "_field")

    def __init__(self, grant: BranchScope, lookups, field="branch"):
        self._grant = grant
        self._lookups = lookups
        self._field = field

    def q(self, prefix=""):
        """The narrowing as a ``Q``, with ``prefix`` naming the route to ``branch``."""
        from django.db.models import Q

        return self._grant.q(prefix, field=self._field) & Q(
            **{f"{prefix}{name}": value for name, value in self._lookups.items()}
        )

    @property
    def is_narrowed(self) -> bool:
        """True when this caller sees less than the whole entity."""
        return self._grant.is_narrowed or bool(self._lookups)


def _branch_scope(request, entity=None, params=None, *, field="branch") -> _BranchScope:
    """Resolve the caller's branch narrowing once, for a service that spans models.

    Resolving here rather than per queryset also means ``?branch=`` is validated once
    per request, so an unknown branch is one 400 rather than a different error depending
    on which population the service happened to filter first.
    """
    return _BranchScope(
        # ``include_shared=False``: see :class:`_BranchScope` for why procurement
        # reads an absent branch as a scope of its own rather than as shared.
        branch_scope(request, include_shared=False),
        _branch_filter_lookups(request, entity, params, field=field),
        field,
    )


def _branch_q(request, entity=None, params=None, *, field="branch", prefix=""):
    """The branch narrowing one caller is under, as a reusable ``Q``.

    The single expression of "which documents is this caller looking at", so a
    list, its KPI header and the dashboard cannot drift apart: they all filter on
    this, differing only in ``prefix`` when the branch is reached through a
    relation (``purchase_order__``).  See :func:`_branch_lookups` for the rule it
    renders.
    """
    return _branch_scope(request, entity, params, field=field).q(prefix)


def _branch_scoped(request, entity, qs, params, *, field="branch"):
    """Narrow a document list to the caller's branch, then to ``?branch=``."""
    return qs.filter(_branch_q(request, entity, params, field=field))


def _branch_visible(request, qs, *, field="branch", prefix=""):
    """Narrow any queryset to the branch the caller is entitled to work in.

    The read half of the branch rule with no request input at all: it answers
    "may this caller touch this row", which is what a detail read, an action, or
    a KPI aggregate needs.  An unbound caller is unaffected, so a tenant with no
    branches gets the identical queryset it did before.
    """
    return qs.filter(_branch_q(request, field=field, prefix=prefix))


def _document_or_404(request, qs, pk, message, *, field="branch", prefix=""):
    """Fetch one document by pk inside the caller's entity *and* branch, or 404.

    The choke point every procurement detail and action endpoint goes through.
    Entity scoping alone leaves a branch-bound caller able to read and act on a
    neighbouring branch's document by guessing its id, and doing that check
    per-endpoint guarantees the next endpoint forgets it.

    A row in another branch is reported with exactly the same "no such document"
    message as a row that does not exist, so the endpoint is not an id-discovery
    channel; ``qs`` must already be entity-scoped, which keeps tenant isolation
    where it has always been.
    """
    row = _branch_visible(request, qs, field=field, prefix=prefix).filter(pk=pk).first()
    if row is None:
        raise NotFound(message)
    return row


def _resolve_vendor(entity, ref):
    """Resolve a required vendor by id or code, always inside ``entity``.

    A reference belonging to another tenant is reported exactly like a missing
    reference so the API cannot be used to discover cross-tenant vendor ids.
    """
    if ref in (None, ""):
        raise ValidationError({"vendor": "A vendor is required."})
    qs = Vendor.objects.filter(entity=entity)
    vendor = (
        qs.filter(pk=int(ref)).first() if str(ref).isdigit()
        else qs.filter(code=str(ref)).first() or qs.filter(code=str(ref).upper()).first()
    )
    if vendor is None:
        raise ValidationError({"vendor": f"No vendor '{ref}' in this entity."})
    return vendor


def _date(value, field, *, required=False):
    """Parse an optional ISO calendar date; reject ambiguous locale formats."""
    if value in (None, ""):
        if required:
            raise ValidationError({field: "An ISO date (YYYY-MM-DD) is required."})
        return None
    try:
        return datetime.date.fromisoformat(str(value))
    except ValueError:
        raise ValidationError({field: "Expected an ISO date (YYYY-MM-DD)."})


def _dec(value, field):
    """Parse a decimal value for legacy callers that apply their own bounds."""
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError):
        raise ValidationError({field: "Expected a number."})


#: Upper bound implied by the line models' ``DecimalField(max_digits=14, decimal_places=4)``.
_MAX_QTY = Decimal("9999999.9999")


def _quantity(value, field):
    """A strictly positive, finite quantity within the line model's 14,4 precision.

    Sourcing-specific hardening (NaN/inf/zero/negative are all real payloads a client
    can send). Deliberately *not* folded into :func:`_dec` - other document types rely
    on ``_dec`` accepting any parseable number, and this change must not alter them.
    """
    try:
        qty = Decimal(str(value))
    except (InvalidOperation, TypeError):
        raise ValidationError({field: "Expected a number."})
    if not qty.is_finite():  # Rejects NaN and ±Infinity, which Decimal will happily parse.
        raise ValidationError({field: "Quantity must be a finite number."})
    if qty <= 0:
        raise ValidationError({field: "Quantity must be greater than zero."})
    if qty > _MAX_QTY:
        raise ValidationError({field: "Quantity is too large."})
    if qty != qty.quantize(Decimal("0.0001")):
        raise ValidationError({field: "Quantity cannot have more than four decimal places."})
    return qty


def _nonneg_qty(value, field):
    """A non-negative, finite quantity within the line model's 14,4 precision.

    Used for master-data thresholds (``reorder_level`` / ``reorder_qty``) where zero is a
    legitimate value but NaN/inf/negative are not. Kept separate from :func:`_dec` (which
    other document types rely on accepting any parseable number) and from :func:`_quantity`
    (which rejects zero).
    """
    try:
        qty = Decimal(str(value))
    except (InvalidOperation, TypeError):
        raise ValidationError({field: "Expected a number."})
    if not qty.is_finite():  # Rejects NaN and ±Infinity, which Decimal will happily parse.
        raise ValidationError({field: "Expected a finite number."})
    if qty < 0:
        raise ValidationError({field: "Cannot be negative."})
    if qty > _MAX_QTY:
        raise ValidationError({field: "Value is too large."})
    if qty != qty.quantize(Decimal("0.0001")):
        raise ValidationError({field: "Value cannot have more than four decimal places."})
    return qty


def _signed_qty(value, field):
    """A signed, non-zero, finite quantity within the 14,4 precision (a stock delta).

    A stock adjustment must move the count, so zero is rejected here; the sign carries the
    direction (``+`` write-up, ``−`` shrinkage). NaN/inf are rejected the same way.
    """
    try:
        qty = Decimal(str(value))
    except (InvalidOperation, TypeError):
        raise ValidationError({field: "Expected a number."})
    if not qty.is_finite():  # Rejects NaN and ±Infinity, which Decimal will happily parse.
        raise ValidationError({field: "Expected a finite number."})
    if qty == 0:
        raise ValidationError({field: "The adjustment must change the quantity."})
    if abs(qty) > _MAX_QTY:
        raise ValidationError({field: "Value is too large."})
    if qty != qty.quantize(Decimal("0.0001")):
        raise ValidationError({field: "Value cannot have more than four decimal places."})
    return qty


def _strict_kobo(value, field):
    """Coerce to non-negative **integer** kobo, rejecting float/bool naira mistakes.

    Stricter than :func:`_money` (which silently truncates a float): a JSON float or
    bool must never cross the integer-kobo boundary by coercion.
    """
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValidationError({field: "Expected a whole integer amount in kobo."})
    if value < 0:
        raise ValidationError({field: "Amount cannot be negative."})
    if value > 9_223_372_036_854_775_807:  # Fits BIGINT - the MoneyField storage width.
        raise ValidationError({field: "Amount is too large."})
    return value


def _text(value, field, max_length, *, required=False):
    """Trim and length-bound a free-text field before it reaches the model."""
    text = str(value or "").strip()
    if required and not text:
        raise ValidationError({field: "This field is required."})
    if len(text) > max_length:
        raise ValidationError({field: f"Ensure this field has no more than {max_length} characters."})
    return text


def _lead_time_days(value, field="lead_time_days"):
    """Optional integer delivery lead time, bounded to a sane 0–3650 day window."""
    if value in (None, ""):
        return None
    if isinstance(value, bool):
        raise ValidationError({field: "Expected a whole number of days."})
    try:
        days = int(value)
    except (TypeError, ValueError):
        raise ValidationError({field: "Expected a whole number of days."})
    if days < 0 or days > 3650:
        raise ValidationError({field: "Lead time must be between 0 and 3650 days."})
    return days


def _resolve_expense_account(entity, ref, field="expense_account"):
    """Resolve an active, postable **EXPENSE** account in ``entity`` (or ``None``).

    Sourcing line accounts must be genuinely postable expense accounts - the same rule
    the catalog/category defaults enforce - so an award cannot carry a header/income/
    inactive account onto the resulting PO line.
    """
    account = _resolve_account(entity, ref, field)
    if account is None:
        return None
    from vs_finance.constants import AccountType

    if not (account.account_type == AccountType.EXPENSE and account.is_active and account.is_postable):
        raise ValidationError({field: "Select an active, postable EXPENSE account in this entity."})
    return account


def _resolve_asset_account(entity, ref, field="inventory_account"):
    """Resolve an active, postable **ASSET** account in ``entity`` (or ``None``).

    A stock item's inventory-value must be carried in a genuinely postable balance-sheet
    asset account - the same active/postable rule :func:`_resolve_expense_account` enforces
    for expenses - so an item can never carry its value onto a header/liability/inactive
    account.
    """
    account = _resolve_account(entity, ref, field)
    if account is None:
        return None
    from vs_finance.constants import AccountType

    if not (account.account_type == AccountType.ASSET and account.is_active and account.is_postable):
        raise ValidationError({field: "Select an active, postable ASSET account in this entity."})
    return account


def _money(value, field):
    """Coerce to non-negative integer kobo.

    Procurement and finance persist money in the smallest currency unit.  API
    callers must therefore send kobo, never formatted naira strings.
    """
    try:
        amount = int(value)
    except (TypeError, ValueError):
        raise ValidationError({field: "Expected an integer amount in kobo."})
    if amount < 0:
        raise ValidationError({field: "Amount cannot be negative."})
    return amount


def _kobo(amount):
    """Render a kobo amount as {kobo, naira} for report payloads."""
    from vs_finance.money import format_naira
    return {"kobo": amount, "naira": format_naira(amount)}


def _require_lines(body):
    """Return a non-empty document-line array or raise a field-level error."""
    lines = body.get("lines")
    if not lines or not isinstance(lines, list):
        raise ValidationError({"lines": "At least one line is required."})
    return lines


class _ProcBase(APIView):
    """Common procurement API base enforcing active-user authentication and RBAC."""

    # Each concrete view supplies the resource/verb permission consumed by
    # HasRBACPermission; entity isolation is then applied in every endpoint query.
    permission_classes = [IsAuthenticatedAndActive & HasRBACPermission]

    def paginate(self, request, qs, serializer_cls, *, page_context=None, **ser_kwargs):
        """List response through the platform's XVSPagination envelope ({pagination, data}).
        Fixed page size 25 (override per-request with ?page_size=, capped at 100).

        ``page_context`` is an optional callable receiving the materialised page and
        returning extra serializer context. It exists so a derived field that needs one
        query for the whole page (rather than one per row) can be resolved after
        pagination has bounded the row set, without each list view re-implementing
        pagination.
        """
        from core.pagination import XVSPagination

        paginator = XVSPagination()
        # Bound list reads before serialization; serializers may traverse
        # preloaded relations supplied by their endpoint-specific queryset.
        paginator.page_size = 25
        page = paginator.paginate_queryset(qs, request, view=self)
        if page_context is not None:
            ser_kwargs["context"] = {**ser_kwargs.get("context", {}), **page_context(page)}
        return paginator.get_paginated_response(serializer_cls(page, many=True, **ser_kwargs).data)

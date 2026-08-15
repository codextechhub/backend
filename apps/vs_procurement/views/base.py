"""Shared request parsing, entity-safe resolvers, and the RBAC-gated base view.

The helpers in this module are deliberately the narrow waist of procurement's
write API: endpoints pass user-supplied references through them before a model
or posting service sees the value.  Keeping tenant checks, money units, and
numeric bounds here makes those invariants consistent across document types.
"""
from __future__ import annotations

import datetime
from decimal import Decimal, InvalidOperation

from rest_framework.exceptions import NotFound, PermissionDenied, ValidationError
from rest_framework.views import APIView

from vs_rbac.permissions import HasRBACPermission, IsAuthenticatedAndActive

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
#   * every read narrows to the caller's branch, whether it is a list, a KPI
#     total, an analytics report or a single document.  One rule
#     (:func:`_branch_lookups`) is rendered two ways: as a ``Q`` for a single
#     model (:func:`_branch_q`, and the wrappers :func:`_branch_scoped`,
#     :func:`_branch_visible`, :func:`_document_or_404`), and as a
#     :class:`_BranchScope` for a service that spans several models
#     (:func:`_branch_scope`).
#
# An absent branch is a real, valid answer - the document belongs to the entity
# as a whole - and is never coerced or rejected.  A tenant with no branches at
# all therefore behaves exactly as it did before: every value here stays ``None``.


def _caller_branch_ids(request):
    """The branches this caller may work in, or ``None`` for the whole entity.

    Branch context is **not** carried by a header or a query parameter.  It is
    derived from what the caller has actually been granted, by the one function
    that also decides whether they may open the screen at all
    (:func:`vs_rbac.scoping.visible_branch_ids`) - so "may I?" and "whose rows?"
    cannot give different answers.  DRF's ``request.user`` is the *effective*
    user, so this stays correct through impersonation.

    A frozenset is the answer rather than one branch because a person can hold
    "Storekeeper at Ikeja" *and* "Storekeeper at Lekki", which the single
    ``vs_user.User.branch`` column this used to read cannot express.  That column
    is still the fallback for a caller whose access is whole-tenant, so a tenant
    that has never used branch-scoped grants behaves exactly as it did.

    Resolved against the caller's **own** tenant: branch grants only exist there,
    and cross-tenant reads are refused by entity scoping, which is untouched.
    """
    user = getattr(request, "user", None)
    if user is None or not getattr(user, "is_authenticated", False):
        return None

    from vs_rbac.scoping import visible_branch_ids

    return visible_branch_ids(user, getattr(user, "tenant", None))


def _sole_caller_branch(request, entity):
    """The one branch a caller works in, or ``None`` when that is not a single branch.

    Used only where a *default* is needed (raising a document without naming a
    branch).  It is never used to decide what a caller may reach: answering
    ``None`` for a caller entitled to two branches would read as "unbound", and
    unbound means the whole entity.
    """
    ids = _caller_branch_ids(request)
    if ids is None or len(ids) != 1:
        return None
    return _resolve_branch_reference(entity, next(iter(ids)))


def _resolve_branch_reference(entity, ref, field="branch"):
    """Resolve a branch id inside ``entity``'s owning tenant, or ``None`` when blank.

    A branch belonging to another tenant is reported exactly like an unknown one,
    so the parameter cannot be used to discover ids outside the caller's tenant.

    The rule itself lives with the model it protects (vs_schools); this wrapper
    only says which tenant a procurement caller is entitled to, so every app that
    accepts a branch answers an unknown reference the same way.
    """
    from vs_tenants.references import resolve_branch_reference

    return resolve_branch_reference(entity.tenant, ref, field)


def _raised_branch(request, entity, body, *, field="branch"):
    """The branch a newly raised document belongs to.

    A caller bound to one branch always raises for that branch; naming a
    different one is refused rather than silently retargeted.  A caller bound to
    several must say which of *theirs* it is, because there is no longer an
    obvious default - and naming one outside their set is refused exactly as a
    single-branch caller's would be.  A caller who is not bound at all may name
    any branch belonging to this entity's tenant, or leave it out - leaving it
    out means the purchase belongs to the entity as a whole and is a valid
    answer, not missing data.
    """
    ids = _caller_branch_ids(request)
    raw = body.get(field) if hasattr(body, "get") else None
    if ids is None:
        return _resolve_branch_reference(entity, raw, field)
    if request.user.tenant_id != entity.tenant_id:
        # A caller's grants live in their own tenant, so the caller's tenant is an
        # exact, query-free proxy for the tenant their branches belong to.
        # Unreachable through the API (entity resolution already pins the caller's
        # tenant), but fail closed rather than write a foreign tenant's branch.
        raise PermissionDenied("Your branch does not belong to this entity.")
    if not ids:
        # Every branch they were granted has since been suspended or closed.
        raise PermissionDenied("You are not assigned to a branch that can raise this.")
    if raw in (None, ""):
        own = _sole_caller_branch(request, entity)
        if own is None:
            raise ValidationError(
                {field: "Name the branch this is for; you work in more than one."},
            )
        return own
    chosen = _resolve_branch_reference(entity, raw, field)
    if chosen is None or chosen.pk not in ids:
        raise PermissionDenied("You can only raise documents for your own branch.")
    return chosen


def _inherited_branch_id(request, *sources, field="branch"):
    """The branch id a downstream document takes from the source(s) it continues.

    The chain decides, not the request: once a source document exists its branch
    is the answer, and no request body, header, or ``?branch=`` may override it.
    Sources that disagree (a payment settling invoices from two branches) resolve
    to the entity as a whole.  The only check left is that the caller is entitled
    to work in the resulting scope at all - a branch-bound user may not continue
    another branch's chain, nor an entity-wide one.
    """
    known = {getattr(s, f"{field}_id") for s in sources if s is not None}
    branch_id = known.pop() if len(known) == 1 else None
    ids = _caller_branch_ids(request)
    if ids is not None and branch_id not in ids:
        raise PermissionDenied("This document belongs to another branch.")
    return branch_id


def _branch_lookups(request, entity=None, params=None, *, field="branch"):
    """The caller's branch narrowing, resolved once into plain lookup/value pairs.

    This is the actual rule; :func:`_branch_q` and :class:`_BranchScope` are only two
    renderings of it.  Keeping the *decision* here and the *relation path* out of it is
    what lets a list, a KPI header and a report aggregate agree even though each reaches
    the branch column by a different route.

    A caller bound to one or more branches only ever sees those branches' documents -
    branch narrows *within* the entity and can never widen what the entity scope already
    allows.  A caller who is not bound to any sees the whole entity and may narrow it
    with ``?branch=<id>``, or with ``?branch=none`` for the documents raised for the
    entity as a whole.  The pairs are ANDed by every renderer, so a bound caller asking
    for somebody else's branch gets an empty answer rather than that branch's rows, while
    one asking for a branch that *is* theirs narrows to it; the three lookup names used
    here are distinct, so no term can overwrite another.  An unknown branch is a 400
    rather than a silent empty page, so the filter cannot be used to probe ids in
    another tenant.

    ``params`` may be omitted (detail reads take no filter input); the result is then
    empty for an unbound caller, which filters nothing at all.
    """
    lookups = {}
    ids = _caller_branch_ids(request)
    if ids is not None:
        # ``__in`` rather than an equality term: a caller may hold grants for
        # several branches, and an empty set is a real answer (every branch they
        # were granted has been withdrawn) that must show nothing rather than
        # everything.
        lookups[f"{field}_id__in"] = tuple(sorted(ids))
    raw = str((params.get(field) if params else "") or "").strip()
    if not raw:
        return lookups
    if raw.lower() in ("none", "null"):
        lookups[f"{field}__isnull"] = True
        return lookups
    lookups[field] = _resolve_branch_reference(entity, raw, field)
    return lookups


class _BranchScope:
    """One caller's branch narrowing, renderable against any relation path.

    A list filters one model, so a single ``Q`` is enough for it.  A report service
    aggregates several models that reach ``branch`` by different routes (a payment
    allocation through ``payment__``, a receipt line through ``grn__``), so handing it a
    pre-built ``Q`` would force it to re-derive the rule for every other path - which is
    exactly the drift Round 3 removed.  It is handed this instead: the same resolved
    answer, re-rendered per path.

    ``is_narrowed`` reports whether the caller is looking at less than the whole entity.
    It is deliberately the *only* thing that turns branch-specific fields on in a report
    payload, so an unbound caller and a tenant with no branches keep byte-identical
    responses.
    """

    __slots__ = ("_lookups",)

    def __init__(self, lookups):
        self._lookups = lookups

    def q(self, prefix=""):
        """The narrowing as a ``Q``, with ``prefix`` naming the route to ``branch``."""
        from django.db.models import Q

        return Q(**{f"{prefix}{name}": value for name, value in self._lookups.items()})

    @property
    def is_narrowed(self) -> bool:
        """True when this caller sees less than the whole entity."""
        return bool(self._lookups)


def _branch_scope(request, entity=None, params=None, *, field="branch") -> _BranchScope:
    """Resolve the caller's branch narrowing once, for a service that spans models.

    Resolving here rather than per queryset also means ``?branch=`` is validated once
    per request, so an unknown branch is one 400 rather than a different error depending
    on which population the service happened to filter first.
    """
    return _BranchScope(_branch_lookups(request, entity, params, field=field))


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

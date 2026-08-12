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
#   * a document that *starts* a chain captures the branch from the person
#     raising it (:func:`_raised_branch`);
#   * a document that *continues* a chain takes the branch from its source
#     document and nothing else (:func:`_inherited_branch_id`);
#   * every read narrows to the caller's branch, whether it is a list, a KPI
#     total or a single document (:func:`_branch_q`, and the three wrappers
#     :func:`_branch_scoped`, :func:`_branch_visible`, :func:`_document_or_404`).
#
# An absent branch is a real, valid answer - the document belongs to the entity
# as a whole - and is never coerced or rejected.  A tenant with no branches at
# all therefore behaves exactly as it did before: every value here stays ``None``.


def _caller_branch(request):
    """The branch the caller is bound to, or ``None`` when they are not bound to one.

    Branch context is **not** carried by a header or a query parameter.  The
    authoritative source is the effective user's own ``branch`` assignment
    (``vs_user.User.branch``): it is fixed when the account is provisioned, it is
    validated against the user's tenant on save, and DRF's ``request.user`` is the
    *effective* user, so it stays correct through impersonation.  Note that
    ``request.branch`` - which ``vs_rbac`` reads when scoring a permission - is
    never populated by any middleware, so it must not be trusted to carry scope.
    """
    user = getattr(request, "user", None)
    if user is None or not getattr(user, "is_authenticated", False):
        return None
    return getattr(user, "branch", None)


def _resolve_branch_reference(entity, ref, field="branch"):
    """Resolve a branch id inside ``entity``'s owning tenant, or ``None`` when blank.

    A branch belonging to another tenant is reported exactly like an unknown one,
    so the parameter cannot be used to discover ids outside the caller's tenant.
    """
    if ref in (None, ""):
        return None
    from vs_schools.models import Branch

    raw = str(ref)
    # Reject a non-numeric or out-of-range id here rather than let it reach the
    # database, where an oversized bigint is a server error instead of a 400.
    if raw.isdigit() and int(raw) <= 9_223_372_036_854_775_807:
        # all_objects deliberately: the explicit tenant filter is the security
        # boundary, and it must not depend on ambient request-local tenant state.
        branch = Branch.all_objects.filter(
            school__tenant=entity.tenant, pk=int(raw),
        ).first()
    else:
        branch = None
    if branch is None:
        raise ValidationError({field: "No such branch in this tenant."})
    return branch


def _raised_branch(request, entity, body, *, field="branch"):
    """The branch a newly raised document belongs to.

    A caller bound to a branch always raises for that branch; naming a different
    one is refused rather than silently retargeted.  A caller who is not bound to
    a branch may name one belonging to this entity's tenant, or leave it out -
    leaving it out means the purchase belongs to the entity as a whole and is a
    valid answer, not missing data.
    """
    own = _caller_branch(request)
    raw = body.get(field) if hasattr(body, "get") else None
    if own is None:
        return _resolve_branch_reference(entity, raw, field)
    if request.user.tenant_id != entity.tenant_id:
        # ``User.save`` refuses a branch outside the user's own tenant, so the
        # caller's tenant is an exact, query-free proxy for their branch's tenant.
        # Unreachable through the API (entity resolution already pins the caller's
        # tenant), but fail closed rather than write a foreign tenant's branch.
        raise PermissionDenied("Your branch does not belong to this entity.")
    if raw not in (None, "") and str(raw) != str(own.pk):
        raise PermissionDenied("You can only raise documents for your own branch.")
    return own


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
    own = _caller_branch(request)
    if own is not None and branch_id != own.pk:
        raise PermissionDenied("This document belongs to another branch.")
    return branch_id


def _branch_q(request, entity=None, params=None, *, field="branch", prefix=""):
    """The branch narrowing one caller is under, as a reusable ``Q``.

    The single expression of "which documents is this caller looking at", so a
    list, its KPI header and the dashboard cannot drift apart: they all filter on
    this, differing only in ``prefix`` when the branch is reached through a
    relation (``purchase_order__``).

    A caller bound to a branch only ever sees that branch's documents - branch
    narrows *within* the entity and can never widen what the entity scope already
    allows.  A caller who is not bound to one sees the whole entity and may narrow
    it with ``?branch=<id>``, or with ``?branch=none`` for the documents raised
    for the entity as a whole.  Both terms are ANDed, so a bound caller asking for
    somebody else's branch gets an empty answer rather than that branch's rows.
    An unknown branch is a 400 rather than a silent empty page, so the filter
    cannot be used to probe ids in another tenant.

    ``params`` may be omitted (detail reads take no filter input); the returned
    ``Q`` is then empty for an unbound caller, which filters nothing at all.
    """
    from django.db.models import Q

    query = Q()
    own = _caller_branch(request)
    if own is not None:
        query &= Q(**{f"{prefix}{field}_id": own.pk})
    raw = str((params.get(field) if params else "") or "").strip()
    if not raw:
        return query
    if raw.lower() in ("none", "null"):
        return query & Q(**{f"{prefix}{field}__isnull": True})
    return query & Q(**{
        f"{prefix}{field}": _resolve_branch_reference(entity, raw, field),
    })


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

"""Shared request parsing, entity-safe resolvers, and the RBAC-gated base view.

The helpers in this module are deliberately the narrow waist of procurement's
write API: endpoints pass user-supplied references through them before a model
or posting service sees the value.  Keeping tenant checks, money units, and
numeric bounds here makes those invariants consistent across document types.
"""
from __future__ import annotations

import datetime
from decimal import Decimal, InvalidOperation

from rest_framework.exceptions import ValidationError
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
    and only fall back to a primary-key lookup — otherwise "2100" would be mistaken
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
    can send). Deliberately *not* folded into :func:`_dec` — other document types rely
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
    if value > 9_223_372_036_854_775_807:  # Fits BIGINT — the MoneyField storage width.
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

    Sourcing line accounts must be genuinely postable expense accounts — the same rule
    the catalog/category defaults enforce — so an award cannot carry a header/income/
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
    asset account — the same active/postable rule :func:`_resolve_expense_account` enforces
    for expenses — so an item can never carry its value onto a header/liability/inactive
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

    def paginate(self, request, qs, serializer_cls, **ser_kwargs):
        """List response through the platform's XVSPagination envelope ({pagination, data}).
        Fixed page size 25 (override per-request with ?page_size=, capped at 100)."""
        from core.pagination import XVSPagination

        paginator = XVSPagination()
        # Bound list reads before serialization; serializers may traverse
        # preloaded relations supplied by their endpoint-specific queryset.
        paginator.page_size = 25
        page = paginator.paginate_queryset(qs, request, view=self)
        return paginator.get_paginated_response(serializer_cls(page, many=True, **ser_kwargs).data)


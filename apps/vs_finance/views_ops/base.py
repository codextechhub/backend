"""Shared request-parsing helpers and the RBAC-gated base view.
"""
from __future__ import annotations

import datetime
from decimal import Decimal, InvalidOperation

from rest_framework.exceptions import ValidationError
from rest_framework.views import APIView

from vs_rbac.permissions import HasRBACPermission, IsAuthenticatedAndActive

from ..models import (
    Account,
    BankAccount,
    CostCenter,
    Currency,
    Dimension,
    FiscalYear,
    TaxCode,
)



# --------------------------------------------------------------------------- #
# Shared resolution + coercion helpers (mirror the procurement conventions)   #
# --------------------------------------------------------------------------- #

# Resolve account reference from request data.
def _resolve_account(entity, ref, field, *, required=False):
    """Resolve a GL account by **code** (e.g. "1100") or id within ``entity``.

    Codes are numeric strings, so match on code first, then fall back to a pk lookup.
    Returns ``None`` for a blank ``ref`` unless ``required``.
    """
    if ref in (None, ""):  # Blank input means no account unless required.
        if required:  # Required account missing.
            raise ValidationError({field: "An account (code or id) is required."})
        return None
    qs = Account.objects.filter(entity=entity)
    acc = qs.filter(code=str(ref)).first()
    if acc is None and str(ref).isdigit():  # Numeric refs may be primary keys.
        acc = qs.filter(pk=int(ref)).first()
    if acc is None:  # Reject cross-entity or missing account refs.
        raise ValidationError({field: f"No account '{ref}' in this entity."})
    return acc  # Return resolved account.


# Resolve tax code reference from request data.
def _resolve_tax(entity, ref, field="tax_code"):
    if ref in (None, ""):  # Tax is optional in most finance line payloads.
        return None
    qs = TaxCode.objects.filter(entity=entity)
    tc = qs.filter(code=str(ref)).first()
    if tc is None and str(ref).isdigit():  # Numeric refs may be ids.
        tc = qs.filter(pk=int(ref)).first()
    if tc is None:  # Reject missing/cross-entity tax refs.
        raise ValidationError({field: f"No tax code '{ref}' in this entity."})
    return tc  # Return resolved tax code.


# Resolve cost-center reference from request data.
def _resolve_cost_center(entity, ref, field="cost_center"):
    if ref in (None, ""):  # Cost center is optional.
        return None
    qs = CostCenter.objects.filter(entity=entity)
    cc = qs.filter(code=str(ref)).first()
    if cc is None and str(ref).isdigit():  # Numeric refs may be ids.
        cc = qs.filter(pk=int(ref)).first()
    if cc is None:  # Reject missing/cross-entity cost center refs.
        raise ValidationError({field: f"No cost centre '{ref}' in this entity."})
    return cc  # Return resolved cost center.


# Normalize a request value into a unique string list.
def _str_list(raw, field):
    """Coerce ``raw`` into a list of non-empty, stripped, de-duplicated strings.

    Used for a :class:`~vs_finance.models.Dimension`'s ``allowed_values``. ``None``
    yields ``[]``; anything that is not a list (or holds blank entries) is rejected.
    Order is preserved so the first occurrence of each value wins.
    """
    if raw in (None, ""):  # Blank means no allowed values.
        return []
    if not isinstance(raw, (list, tuple)):  # Only array-like payloads are accepted.
        raise ValidationError({field: "Expected a list of values."})
    seen, out = set(), []  # Track duplicates while preserving first-seen order.
    for item in raw:  # Normalize each supplied value.
        val = str(item).strip()  # Coerce to stripped string.
        if not val:  # Blank values are invalid.
            raise ValidationError({field: "Values cannot be blank."})
        if val not in seen:  # Keep only first occurrence.
            seen.add(val)  # Remember value.
            out.append(val)  # Preserve order.
    return out  # Return cleaned unique list.


# Validate analytical dimensions map.
def _resolve_dimensions(entity, raw, field="dimensions"):
    """Validate an analytical ``{axis_code: value}`` map for ``entity``.

    Mirrors :func:`_resolve_cost_center`: ``None``/``""``/``{}`` yield an empty map.
    Each key must be a registered, active :class:`~vs_finance.models.Dimension` code,
    and each value must be a non-empty string listed in that axis's ``allowed_values``
    (an axis with no values defined yet accepts none). Returns the cleaned map to
    store verbatim on the journal line's ``dimensions`` JSON.
    """
    if raw in (None, ""):  # Blank dimensions become empty map.
        return {}
    if not isinstance(raw, dict):  # Dimensions must be an object/map.
        raise ValidationError({field: "Expected a map of {axis: value}."})
    if not raw:  # Empty map is valid.
        return {}

    allowed = {  # Active dimension code -> allowed values.
        d.code: set(d.allowed_values or [])  # Store allowed values as a set for membership tests.
        for d in Dimension.objects.filter(entity=entity, is_active=True)
    }
    cleaned = {}  # Cleaned dimensions map for storage.
    for axis, value in raw.items():  # Validate each supplied axis/value.
        axis = str(axis)  # Dimension axis codes are strings.
        if axis not in allowed:  # Axis must exist and be active.
            raise ValidationError(
                {field: f"No active dimension '{axis}' in this entity."})
        val = str(value).strip()  # Normalize value.
        if not val:  # Dimension values cannot be blank.
            raise ValidationError({field: f"Dimension '{axis}' needs a value."})
        if val not in allowed[axis]:  # Value must be preconfigured for that axis.
            permitted = ", ".join(sorted(allowed[axis])) or "(none defined)"  # Human-readable allowed values.
            raise ValidationError(
                {field: f"'{val}' is not an allowed value for '{axis}'. "
                        f"Allowed: {permitted}."})
        cleaned[axis] = val  # Store cleaned axis value.
    return cleaned  # Return storage-ready dimensions map.


# Resolve currency code from request data.
def _resolve_currency(ref, field="currency"):
    if ref in (None, ""):  # Currency is optional in many payloads.
        return None
    cur = Currency.objects.filter(code=str(ref).upper()).first()
    if cur is None:  # Reject unknown currency codes.
        raise ValidationError({field: f"No currency '{ref}'."})
    return cur  # Return resolved currency.


# Resolve bank account by id or name.
def _resolve_bank_account(entity, ref, field="bank_account", *, required=True):
    """Resolve a bank account by id or name within ``entity``."""
    if ref in (None, ""):  # Blank input means missing bank account.
        if required:  # Most payment endpoints require a bank account.
            raise ValidationError({field: "A bank account (id or name) is required."})
        return None
    qs = BankAccount.objects.filter(entity=entity)
    ba = (  # Resolve by id for numeric refs, otherwise by name.
        qs.filter(pk=int(ref)).first() if str(ref).isdigit()
        else qs.filter(name=str(ref)).first()
    )
    if ba is None:  # Reject missing/cross-entity bank refs.
        raise ValidationError({field: f"No bank account '{ref}' in this entity."})
    return ba  # Return resolved bank account.


# Resolve fiscal year by label or id.
def _resolve_fiscal_year(entity, ref, field="fiscal_year"):
    """Resolve a fiscal year by its ``year`` label (preferred) or id within ``entity``."""
    if ref in (None, ""):  # Fiscal year is required for these endpoints.
        raise ValidationError({field: "A fiscal_year (year or id) is required."})
    qs = FiscalYear.objects.filter(entity=entity)
    fy = qs.filter(year=int(ref)).first() if str(ref).isdigit() else None
    if fy is None and str(ref).isdigit():  # Numeric refs can also be primary keys.
        fy = qs.filter(pk=int(ref)).first()
    if fy is None:  # Reject missing/cross-entity fiscal years.
        raise ValidationError({field: f"No fiscal year '{ref}' in this entity."})
    return fy  # Return resolved fiscal year.


# Parse optional/required ISO date.
def _date(value, field, *, required=False):
    if value in (None, ""):  # Blank date.
        if required:  # Required date missing.
            raise ValidationError({field: "An ISO date (YYYY-MM-DD) is required."})
        return None
    try:  # Parse strict ISO date.
        return datetime.date.fromisoformat(str(value))
    except ValueError:  # Invalid date format.
        raise ValidationError({field: "Expected an ISO date (YYYY-MM-DD)."})


# Parse request value as Decimal.
def _dec(value, field):
    try:  # Decimal constructor can reject invalid strings/types.
        return Decimal(str(value))
    except (InvalidOperation, TypeError):  # Invalid numeric input.
        raise ValidationError({field: "Expected a number."})


# Parse non-negative integer kobo.
def _money(value, field):
    """Coerce to non-negative integer kobo, rejecting floats-as-naira mistakes."""
    try:  # int() rejects non-integer strings and missing values.
        amount = int(value)  # Normalize to integer kobo.
    except (TypeError, ValueError):  # Invalid money input.
        raise ValidationError({field: "Expected an integer amount in kobo."})
    if amount < 0:  # Non-negative money parser rejects negative amounts.
        raise ValidationError({field: "Amount cannot be negative."})
    return amount  # Return integer kobo.


# Parse signed integer kobo.
def _signed_money(value, field):
    """Coerce to a *signed* integer kobo (bank lines can be negative outflows)."""
    try:  # Signed amounts still must be integers.
        return int(value)  # Return signed integer kobo.
    except (TypeError, ValueError):  # Invalid signed money input.
        raise ValidationError({field: "Expected an integer amount in kobo (may be negative)."})


# Extract and validate required line array.
def _require_lines(body):
    lines = body.get("lines")
    if not lines or not isinstance(lines, list):  # Lines must be a non-empty list.
        raise ValidationError({"lines": "At least one line is required."})
    return lines  # Return validated line list.


# Parse integer request value.
def _int(value, field, *, required=False, minimum=None):
    if value in (None, ""):  # Blank integer input.
        if required:  # Required integer missing.
            raise ValidationError({field: "An integer is required."})
        return None
    try:  # Normalize numeric string/int to int.
        out = int(value)  # Parsed integer.
    except (TypeError, ValueError):  # Invalid integer input.
        raise ValidationError({field: "Expected an integer."})
    if minimum is not None and out < minimum:  # Enforce optional lower bound.
        raise ValidationError({field: f"Must be ≥ {minimum}."})
    return out  # Return parsed integer.


# Parse common truthy/falsey request values.
def _bool(value, default=False):
    if value in (None, ""):  # Blank input uses caller default.
        return default
    if isinstance(value, bool):  # Native bool passes through.
        return value
    return str(value).lower() in ("1", "true", "yes", "on")  # Recognize common truthy strings.


# Shared base class for finance operational endpoints.
class _FinanceBase(APIView):
    permission_classes = [IsAuthenticatedAndActive & HasRBACPermission]  # Require active auth and RBAC permission.

    # Paginate a queryset with platform envelope.
    def paginate(self, request, qs, serializer_cls, **ser_kwargs):
        """List response via the platform's XVSPagination envelope ({pagination, data}).
        Fixed page size 25 (override per-request with ?page_size=, capped at 100)."""
        from core.pagination import XVSPagination

        paginator = XVSPagination()  # Instantiate platform paginator.
        paginator.page_size = 25  # Default finance page size.
        page = paginator.paginate_queryset(qs, request, view=self)  # Slice queryset for current request.
        return paginator.get_paginated_response(serializer_cls(page, many=True, **ser_kwargs).data)  # Serialize and wrap page.


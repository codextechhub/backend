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

def _resolve_account(entity, ref, field, *, required=False):
    """Resolve a GL account by **code** (e.g. "1100") or id within ``entity``.

    Codes are numeric strings, so match on code first, then fall back to a pk lookup.
    Returns ``None`` for a blank ``ref`` unless ``required``.
    """
    if ref in (None, ""):
        if required:
            raise ValidationError({field: "An account (code or id) is required."})
        return None
    qs = Account.objects.filter(entity=entity)
    acc = qs.filter(code=str(ref)).first()
    if acc is None and str(ref).isdigit():
        acc = qs.filter(pk=int(ref)).first()
    if acc is None:
        raise ValidationError({field: f"No account '{ref}' in this entity."})
    return acc


def _resolve_tax(entity, ref, field="tax_code"):
    if ref in (None, ""):
        return None
    qs = TaxCode.objects.filter(entity=entity)
    tc = qs.filter(code=str(ref)).first()
    if tc is None and str(ref).isdigit():
        tc = qs.filter(pk=int(ref)).first()
    if tc is None:
        raise ValidationError({field: f"No tax code '{ref}' in this entity."})
    return tc


def _resolve_cost_center(entity, ref, field="cost_center"):
    if ref in (None, ""):
        return None
    qs = CostCenter.objects.filter(entity=entity)
    cc = qs.filter(code=str(ref)).first()
    if cc is None and str(ref).isdigit():
        cc = qs.filter(pk=int(ref)).first()
    if cc is None:
        raise ValidationError({field: f"No cost centre '{ref}' in this entity."})
    return cc


def _str_list(raw, field):
    """Coerce ``raw`` into a list of non-empty, stripped, de-duplicated strings.

    Used for a :class:`~vs_finance.models.Dimension`'s ``allowed_values``. ``None``
    yields ``[]``; anything that is not a list (or holds blank entries) is rejected.
    Order is preserved so the first occurrence of each value wins.
    """
    if raw in (None, ""):
        return []
    if not isinstance(raw, (list, tuple)):
        raise ValidationError({field: "Expected a list of values."})
    seen, out = set(), []
    for item in raw:
        val = str(item).strip()
        if not val:
            raise ValidationError({field: "Values cannot be blank."})
        if val not in seen:
            seen.add(val)
            out.append(val)
    return out


def _resolve_dimensions(entity, raw, field="dimensions"):
    """Validate an analytical ``{axis_code: value}`` map for ``entity``.

    Mirrors :func:`_resolve_cost_center`: ``None``/``""``/``{}`` yield an empty map.
    Each key must be a registered, active :class:`~vs_finance.models.Dimension` code,
    and each value must be a non-empty string listed in that axis's ``allowed_values``
    (an axis with no values defined yet accepts none). Returns the cleaned map to
    store verbatim on the journal line's ``dimensions`` JSON.
    """
    if raw in (None, ""):
        return {}
    if not isinstance(raw, dict):
        raise ValidationError({field: "Expected a map of {axis: value}."})
    if not raw:
        return {}

    allowed = {
        d.code: set(d.allowed_values or [])
        for d in Dimension.objects.filter(entity=entity, is_active=True)
    }
    cleaned = {}
    for axis, value in raw.items():
        axis = str(axis)
        if axis not in allowed:
            raise ValidationError(
                {field: f"No active dimension '{axis}' in this entity."})
        val = str(value).strip()
        if not val:
            raise ValidationError({field: f"Dimension '{axis}' needs a value."})
        if val not in allowed[axis]:
            permitted = ", ".join(sorted(allowed[axis])) or "(none defined)"
            raise ValidationError(
                {field: f"'{val}' is not an allowed value for '{axis}'. "
                        f"Allowed: {permitted}."})
        cleaned[axis] = val
    return cleaned


def _resolve_currency(ref, field="currency"):
    if ref in (None, ""):
        return None
    cur = Currency.objects.filter(code=str(ref).upper()).first()
    if cur is None:
        raise ValidationError({field: f"No currency '{ref}'."})
    return cur


def _resolve_bank_account(entity, ref, field="bank_account", *, required=True):
    """Resolve a bank account by id or name within ``entity``."""
    if ref in (None, ""):
        if required:
            raise ValidationError({field: "A bank account (id or name) is required."})
        return None
    qs = BankAccount.objects.filter(entity=entity)
    ba = (
        qs.filter(pk=int(ref)).first() if str(ref).isdigit()
        else qs.filter(name=str(ref)).first()
    )
    if ba is None:
        raise ValidationError({field: f"No bank account '{ref}' in this entity."})
    return ba


def _resolve_fiscal_year(entity, ref, field="fiscal_year"):
    """Resolve a fiscal year by its ``year`` label (preferred) or id within ``entity``."""
    if ref in (None, ""):
        raise ValidationError({field: "A fiscal_year (year or id) is required."})
    qs = FiscalYear.objects.filter(entity=entity)
    fy = qs.filter(year=int(ref)).first() if str(ref).isdigit() else None
    if fy is None and str(ref).isdigit():
        fy = qs.filter(pk=int(ref)).first()
    if fy is None:
        raise ValidationError({field: f"No fiscal year '{ref}' in this entity."})
    return fy


def _date(value, field, *, required=False):
    if value in (None, ""):
        if required:
            raise ValidationError({field: "An ISO date (YYYY-MM-DD) is required."})
        return None
    try:
        return datetime.date.fromisoformat(str(value))
    except ValueError:
        raise ValidationError({field: "Expected an ISO date (YYYY-MM-DD)."})


def _dec(value, field):
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError):
        raise ValidationError({field: "Expected a number."})


def _money(value, field):
    """Coerce to non-negative integer kobo, rejecting floats-as-naira mistakes."""
    try:
        amount = int(value)
    except (TypeError, ValueError):
        raise ValidationError({field: "Expected an integer amount in kobo."})
    if amount < 0:
        raise ValidationError({field: "Amount cannot be negative."})
    return amount


def _signed_money(value, field):
    """Coerce to a *signed* integer kobo (bank lines can be negative outflows)."""
    try:
        return int(value)
    except (TypeError, ValueError):
        raise ValidationError({field: "Expected an integer amount in kobo (may be negative)."})


def _require_lines(body):
    lines = body.get("lines")
    if not lines or not isinstance(lines, list):
        raise ValidationError({"lines": "At least one line is required."})
    return lines


def _int(value, field, *, required=False, minimum=None):
    if value in (None, ""):
        if required:
            raise ValidationError({field: "An integer is required."})
        return None
    try:
        out = int(value)
    except (TypeError, ValueError):
        raise ValidationError({field: "Expected an integer."})
    if minimum is not None and out < minimum:
        raise ValidationError({field: f"Must be ≥ {minimum}."})
    return out


def _bool(value, default=False):
    if value in (None, ""):
        return default
    if isinstance(value, bool):
        return value
    return str(value).lower() in ("1", "true", "yes", "on")


class _FinanceBase(APIView):
    permission_classes = [IsAuthenticatedAndActive & HasRBACPermission]

    def paginate(self, request, qs, serializer_cls, **ser_kwargs):
        """List response via the platform's XVSPagination envelope ({pagination, data}).
        Fixed page size 25 (override per-request with ?page_size=, capped at 100)."""
        from core.pagination import XVSPagination

        paginator = XVSPagination()
        paginator.page_size = 25
        page = paginator.paginate_queryset(qs, request, view=self)
        return paginator.get_paginated_response(serializer_cls(page, many=True, **ser_kwargs).data)



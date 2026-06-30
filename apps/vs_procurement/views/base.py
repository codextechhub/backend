"""Shared request-parsing helpers and the RBAC-gated base view.
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
    if ref in (None, ""):
        return None
    from vs_finance.models import Currency

    cur = Currency.objects.filter(code=str(ref).upper()).first()
    if cur is None:
        raise ValidationError({field: f"No currency '{ref}'."})
    return cur


def _resolve_vendor(entity, ref):
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


def _kobo(amount):
    """Render a kobo amount as {kobo, naira} for report payloads."""
    from vs_finance.money import format_naira
    return {"kobo": amount, "naira": format_naira(amount)}


def _require_lines(body):
    lines = body.get("lines")
    if not lines or not isinstance(lines, list):
        raise ValidationError({"lines": "At least one line is required."})
    return lines


class _ProcBase(APIView):
    permission_classes = [IsAuthenticatedAndActive & HasRBACPermission]

    def paginate(self, request, qs, serializer_cls, **ser_kwargs):
        """List response through the platform's XVSPagination envelope ({pagination, data}).
        Fixed page size 25 (override per-request with ?page_size=, capped at 100)."""
        from core.pagination import XVSPagination

        paginator = XVSPagination()
        paginator.page_size = 25
        page = paginator.paginate_queryset(qs, request, view=self)
        return paginator.get_paginated_response(serializer_cls(page, many=True, **ser_kwargs).data)



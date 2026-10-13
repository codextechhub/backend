"""The result of verifying the sealed figures, as a caller's Field Access lets them read it.

The verification endpoint recomputes every current seal from the ledger and
reports the verdict, each seal's identity, checksum and line counts, and the
balances that moved. Each of those is a registered field of ``finance.seal``;
a field the caller may not read is absent from the response.

``ok`` is the verdict wherever it appears, on the whole result and on each
seal checked, so one switch withholds both: a caller who may not read whether
the closed figures match is not told by a per-seal flag either. The balances
that moved (``differences``, each with its branch, account, sealed figures and
current figures) and the sentence that describes them (``summary``) travel
together, since the sentence counts the very balances the list names.
"""
from __future__ import annotations

from rest_framework import serializers

from vs_rbac.field_enforcement import FieldAccessMixin

RESOURCE = "finance.seal"


def _value():
    """Whatever the verification holds under this name, read-only."""
    return serializers.JSONField(read_only=True)


class SealCheckSerializer(FieldAccessMixin, serializers.Serializer):
    """One seal that was checked."""

    field_resource = RESOURCE

    seal_id = _value()
    label = _value()
    kind = _value()
    fiscal_year = _value()
    period_id = _value()
    sealed_at = _value()
    seal_checksum = _value()
    line_count = _value()
    line_count_now = _value()
    ok = _value()
    seal_intact = _value()
    lines_match = _value()
    summary = _value()
    differences = _value()


class SealVerificationSerializer(FieldAccessMixin, serializers.Serializer):
    """The whole result of verifying a set of books' sealed figures."""

    field_resource = RESOURCE

    ok = _value()
    checked = _value()
    mismatches = _value()
    chain_breaks = _value()
    checks = SealCheckSerializer(many=True, read_only=True)

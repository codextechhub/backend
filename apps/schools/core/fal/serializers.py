"""Request and response shapes for the FAL's HTTP surface.

The FAL's own contracts are frozen dataclasses, deliberately: they are a Python
boundary and were designed before anything spoke HTTP to them. These serializers
are the only place that shape is turned into JSON, so the dataclasses stay the
single source of truth and no view hand-rolls a dict.
"""
from __future__ import annotations

from rest_framework import serializers


class LinkTermSerializer(serializers.Serializer):
    """Attach a fee structure to a session, and optionally to one term in it."""

    session = serializers.IntegerField(min_value=1)
    term = serializers.IntegerField(min_value=1, required=False, allow_null=True)


class GenerateInvoicesSerializer(serializers.Serializer):
    """Bill a named cohort from a fee structure.

    ``students`` is required and must be non-empty. There is deliberately no
    "bill everyone" default: the neutral engine's own batch-generate bills every
    active customer, and that is exactly the behaviour a school must never get
    by omission. A cohort is named or nothing is billed.
    """

    students = serializers.ListField(
        child=serializers.CharField(max_length=64, allow_blank=False),
        allow_empty=False,
        max_length=2000,
    )
    dry_run = serializers.BooleanField(default=False)

    def validate_students(self, value):
        # A caller who names the same child twice means to bill them once. The
        # bridge is idempotent per student, so this only keeps the counts in the
        # preview honest.
        seen, unique = set(), []
        for ref in value:
            if ref not in seen:
                seen.add(ref)
                unique.append(ref)
        return unique


def link_payload(link) -> dict:
    return {
        "fee_structure": link.fee_structure_ref,
        "session": link.session_ref,
        "term": link.term_ref,
        "entity": link.entity_ref,
        "session_label": link.session_label,
        "term_label": link.term_label,
    }


def generation_payload(result) -> dict:
    """Shape an InvoiceGenerationResult.

    ``dry_run`` is echoed rather than inferred from an empty invoice list,
    because a real run that billed nobody (every student already billed) and a
    preview are different events and a bursar must be able to tell them apart.
    """
    return {
        "fee_structure": result.fee_structure_ref,
        "dry_run": result.dry_run,
        # A Period is (session, term), not a date range. DateRange is the one
        # with start/end, and reading those here raised AttributeError, which
        # the API handler turned into a bare 500 with no cause.
        "period": {
            "session": result.period.session_ref,
            "term": result.period.term_ref,
        },
        "invoices_created": list(result.invoices_created),
        "students_to_bill": list(result.students_to_bill),
        "students_skipped": list(result.students_skipped),
        "total_billed": result.total_billed,
        "counts": {
            "to_bill": len(result.students_to_bill),
            "skipped": len(result.students_skipped),
            "created": len(result.invoices_created),
        },
    }

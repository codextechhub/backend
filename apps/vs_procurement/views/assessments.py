"""Vendor assessments — point-in-time scorecards (list + create, immutable).

Viewing rides ``procurement.report.view`` (it feeds the Vendor Performance report);
recording one needs the dedicated ``procurement.vendor_assessment.create`` key.
"""
from __future__ import annotations

import datetime

from rest_framework.exceptions import ValidationError

from core.response import success_response
from vs_finance.views import resolve_entity

from ..models import VendorAssessment
from .base import _ProcBase, _date, _resolve_vendor, _text


def _score(value, field):
    """A whole 0–100 criterion score (rejects bool/float/out-of-range)."""
    if isinstance(value, bool) or value is None:
        raise ValidationError({field: "Expected a whole number from 0 to 100."})
    try:
        score = int(value)
    except (TypeError, ValueError):
        raise ValidationError({field: "Expected a whole number from 0 to 100."})
    if score < 0 or score > 100:
        raise ValidationError({field: "Score must be between 0 and 100."})
    return score


def _assessor_name(user):
    if user is None:
        return None
    return getattr(user, "full_name", "") or user.get_full_name() or user.email


def _assessment_json(a):
    """Serialize one assessment: raw scores + the computed overall_score/grade."""
    return {
        "id": a.id,
        "vendor_id": a.vendor_id,
        "vendor_code": a.vendor.code,
        "vendor_name": a.vendor.name,
        "assessment_date": str(a.assessment_date),
        "assessor": _assessor_name(a.assessor),
        "on_time_delivery": a.on_time_delivery,
        "quality_acceptance": a.quality_acceptance,
        "invoice_accuracy": a.invoice_accuracy,
        "responsiveness": a.responsiveness,
        "overall_score": a.overall_score,
        "grade": a.grade,
        "notes": a.notes,
    }


class VendorAssessmentListCreateView(_ProcBase):
    """GET (list) / POST (create) vendor assessments.

    docstring-name: Vendor assessments
    """

    @property
    def rbac_permission(self):
        # Create is gated on the sensitive assessment key; listing rides report.view.
        return "procurement.vendor_assessment.create" if self.request.method == "POST" \
            else "procurement.report.view"

    def get(self, request):
        entity = resolve_entity(request)
        qs = (
            VendorAssessment.objects
            .filter(entity=entity)
            .select_related("vendor", "assessor")
        )
        vendor_ref = request.query_params.get("vendor")
        if vendor_ref:
            # _resolve_vendor is entity-scoped — a foreign vendor 404s rather than leaking.
            qs = qs.filter(vendor=_resolve_vendor(entity, vendor_ref))
        # Model Meta already orders newest-first (-assessment_date, -id).
        return success_response(
            "Vendor assessments retrieved.",
            data=[_assessment_json(a) for a in qs],
        )

    def post(self, request):
        entity = resolve_entity(request)
        body = request.data
        # Entity-scoped vendor resolution rejects assessing another entity's vendor.
        vendor = _resolve_vendor(entity, body.get("vendor"))
        assessment = VendorAssessment.objects.create(
            entity=entity, vendor=vendor, assessor=request.user,
            assessment_date=_date(body.get("assessment_date"), "assessment_date")
            or datetime.date.today(),
            on_time_delivery=_score(body.get("on_time_delivery"), "on_time_delivery"),
            quality_acceptance=_score(body.get("quality_acceptance"), "quality_acceptance"),
            invoice_accuracy=_score(body.get("invoice_accuracy"), "invoice_accuracy"),
            responsiveness=_score(body.get("responsiveness"), "responsiveness"),
            notes=_text(body.get("notes", ""), "notes", 2000),
        )
        assessment.vendor = vendor  # attach so the serializer avoids a re-query
        return success_response(
            "Vendor assessment recorded.",
            data=_assessment_json(assessment),
            status=201,
        )

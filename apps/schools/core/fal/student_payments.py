"""Whether a school's pupils may pay their own fees, and the smallest part payment.

Fees are normally paid by a family; a pupil paying their own invoice in the
student portal is the fallback for a family that cannot pay through their own
app. Whether a school offers that fallback at all, and the smallest part
payment it accepts, are the school's choices, each a ``vs_config`` setting
with a default:

* ``fees.student_payment_enabled`` (default on): off removes the Pay action
  from every pupil's fees screen, and a payment started anyway is refused.
* ``fees.min_part_payment`` (whole Naira, default ₦1,000): a part payment below
  it is refused; paying the whole balance is always allowed, however small.
  Stored in Naira because a person types it, as every money setting is; the
  route and the portal speak kobo, as every amount crossing the API does.

Staff read them with ``school.fees.view`` and change them with
``school.fees.update`` and a whole-school reach, as the fee due date.
"""
from __future__ import annotations

from dataclasses import dataclass

from django.db import transaction
from rest_framework import serializers
from rest_framework.views import APIView

from core.response import success_response
from vs_rbac.permissions import HasRBACPermission, IsAuthenticatedAndActive
from vs_rbac.scoping import assert_caller_may_configure

CFG_STUDENT_PAYMENT_ENABLED = "fees.student_payment_enabled"
CFG_MIN_PART_PAYMENT = "fees.min_part_payment"

DEFAULT_MIN_PART_PAYMENT = 100_000  # ₦1,000.00 in kobo.
_KOBO_PER_NAIRA = 100


@dataclass(frozen=True)
class StudentPaymentRules:
    enabled: bool = True
    min_part_payment: int = DEFAULT_MIN_PART_PAYMENT

    def as_dict(self) -> dict:
        return {"enabled": self.enabled, "min_part_payment": self.min_part_payment}


def read_student_payment_rules(tenant) -> StudentPaymentRules:
    """The school's two choices, with each default applied."""
    from vs_config.models import ConfigurationDefinition
    from vs_config.services.resolution import resolve_value

    found = {}
    for definition in ConfigurationDefinition.objects.filter(
        key__in=(CFG_STUDENT_PAYMENT_ENABLED, CFG_MIN_PART_PAYMENT), is_active=True,
    ):
        found[definition.key], _ = resolve_value(definition, tenant=tenant)
    enabled = found.get(CFG_STUDENT_PAYMENT_ENABLED, True)
    naira = found.get(CFG_MIN_PART_PAYMENT, DEFAULT_MIN_PART_PAYMENT // _KOBO_PER_NAIRA)
    try:
        minimum = max(0, int(naira)) * _KOBO_PER_NAIRA
    except (TypeError, ValueError):
        minimum = DEFAULT_MIN_PART_PAYMENT
    return StudentPaymentRules(enabled=enabled is not False, min_part_payment=minimum)


class StudentPaymentRulesSerializer(serializers.Serializer):
    enabled = serializers.BooleanField(
        required=False,
        error_messages={"invalid": "Say whether students may pay their own fees: true or false."},
    )
    min_part_payment = serializers.IntegerField(
        required=False, min_value=0, max_value=100_000_000,
        error_messages={
            "invalid": "Give the smallest part payment in kobo, as a whole number.",
            "min_value": "The smallest part payment cannot be below zero.",
            "max_value": "The smallest part payment cannot be above ₦1,000,000.00.",
        },
    )

    def validate_min_part_payment(self, value):
        if value % _KOBO_PER_NAIRA:
            raise serializers.ValidationError(
                "Give the smallest part payment in whole naira, such as ₦1,000.00.",
            )
        return value
    reason = serializers.CharField(required=False, allow_blank=True, max_length=200)


class StudentPaymentRulesView(APIView):
    """GET, PATCH /v1/school-finance/settings/student-payments/

    Whether pupils may pay their own fees in the student portal, and the
    smallest part payment (kobo) the school accepts. PATCH takes either or
    both. Reading needs ``school.fees.view``; changing needs
    ``school.fees.update`` and a whole-school reach.

    docstring-name: Student payment rules
    """

    permission_classes = [IsAuthenticatedAndActive & HasRBACPermission]

    @property
    def rbac_permission(self):
        return "school.fees.update" if self.request.method == "PATCH" else "school.fees.view"

    def get(self, request):
        return success_response(data=read_student_payment_rules(request.tenant).as_dict())

    @transaction.atomic
    def patch(self, request):
        from vs_config.models import ConfigurationDefinition
        from vs_config.services.resolution import resolve_value, set_value

        assert_caller_may_configure(
            request.user, request.tenant,
            message="Only a school-wide administrator can change whether students may pay their own fees.",
        )
        writer = StudentPaymentRulesSerializer(data=request.data)
        writer.is_valid(raise_exception=True)
        data = writer.validated_data
        why = (data.get("reason") or "").strip() or "Student payment rules set from Finance settings."
        kobo = data.get("min_part_payment")
        wanted = {
            CFG_STUDENT_PAYMENT_ENABLED: data.get("enabled"),
            CFG_MIN_PART_PAYMENT: kobo // _KOBO_PER_NAIRA if kobo is not None else None,
        }
        for key, value in wanted.items():
            if value is None:
                continue
            definition = ConfigurationDefinition.objects.filter(key=key, is_active=True).first()
            if definition is None:
                continue
            current, _ = resolve_value(definition, tenant=request.tenant)
            if current != value:
                set_value(definition=definition, value=value, actor=request.user,
                          tenant=request.tenant, reason=why)
        return success_response(
            "Student payment rules saved.",
            data=read_student_payment_rules(request.tenant).as_dict(),
        )

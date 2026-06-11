"""Dunning: policies, stages, notices.
"""
from __future__ import annotations

from django.db import models

from ..constants import (
    DocType,
    DunningChannel,
    DunningNoticeStatus,
)
from ..money import MoneyField
from .core import TimeStampedModel, LedgerEntity, FinanceDocument
from .ar import Customer, Invoice

# ---------------------------------------------------------------------------
# AR — dunning / automated payment reminders
# ---------------------------------------------------------------------------
#
# A dunning policy is a ladder of stages keyed by how many days an invoice is overdue.
# Generating a run scans the entity's open invoices, matches the *highest* stage each
# crosses, and emits a DunningNotice (idempotent per invoice+level). Notices never post
# to the GL — vs_finance only tracks the reminder lifecycle; an outer notifications
# service dispatches PENDING notices through the recorded channel.


class DunningPolicy(TimeStampedModel):
    """A named ladder of escalating reminder stages for an entity's overdue receivables."""

    entity = models.ForeignKey(
        LedgerEntity, on_delete=models.PROTECT, related_name="dunning_policies",
    )
    name = models.CharField(max_length=120)
    is_active = models.BooleanField(default=True)
    is_default = models.BooleanField(
        default=False,
        help_text="The policy a dunning run uses when none is named. At most one per entity.",
    )

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["entity", "name"], name="uniq_finance_dunning_policy_name",
            ),
            models.UniqueConstraint(
                fields=["entity"], condition=models.Q(is_default=True),
                name="uniq_finance_dunning_policy_default",
            ),
        ]
        indexes = [models.Index(fields=["entity", "is_active"])]
        ordering = ["entity", "name"]
        verbose_name_plural = "dunning policies"

    def __str__(self) -> str:
        return f"{self.name} ({self.entity.code})"


class DunningStage(TimeStampedModel):
    """One rung of a :class:`DunningPolicy` — fires once an invoice is ``min_days_overdue``."""

    policy = models.ForeignKey(
        DunningPolicy, on_delete=models.CASCADE, related_name="stages",
    )
    level = models.PositiveSmallIntegerField(help_text="1-based escalation order.")
    name = models.CharField(max_length=80, help_text="e.g. 'First reminder', 'Final notice'.")
    min_days_overdue = models.PositiveSmallIntegerField(
        help_text="Days past due an invoice must be before this stage applies.",
    )
    channel = models.CharField(
        max_length=8, choices=DunningChannel.choices, default=DunningChannel.EMAIL,
    )
    message = models.TextField(
        blank=True, default="",
        help_text="Reminder text/template copied onto each notice raised at this stage.",
    )

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["policy", "level"], name="uniq_finance_dunning_stage_level",
            ),
        ]
        indexes = [models.Index(fields=["policy", "min_days_overdue"])]
        ordering = ["policy", "level"]

    def __str__(self) -> str:
        return f"L{self.level} {self.name} (≥{self.min_days_overdue}d)"


class DunningNotice(FinanceDocument):
    """A single reminder raised for an overdue invoice at a given escalation level.

    A communications overlay — it never posts to the GL. ``level`` snapshots the stage
    that fired and ``amount_due`` the invoice balance when generated; the notice is keyed
    uniquely per (invoice, level) so re-running a policy never duplicates a reminder the
    customer already received at that rung.
    """

    DOC_TYPE = DocType.DUNNING_NOTICE

    customer = models.ForeignKey(
        Customer, on_delete=models.PROTECT, related_name="dunning_notices",
    )
    invoice = models.ForeignKey(
        Invoice, on_delete=models.PROTECT, related_name="dunning_notices",
    )
    policy = models.ForeignKey(
        DunningPolicy, on_delete=models.PROTECT, related_name="notices",
        null=True, blank=True,
    )
    stage = models.ForeignKey(
        DunningStage, on_delete=models.SET_NULL, related_name="notices",
        null=True, blank=True,
    )
    level = models.PositiveSmallIntegerField(help_text="Escalation level this notice fired at.")
    notice_date = models.DateField(help_text="The 'as at' date the run was generated for.")
    days_overdue = models.PositiveSmallIntegerField(default=0)
    amount_due = MoneyField(help_text="Invoice balance outstanding when generated, in kobo.")
    channel = models.CharField(
        max_length=8, choices=DunningChannel.choices, default=DunningChannel.EMAIL,
    )
    message = models.TextField(blank=True, default="")
    notice_status = models.CharField(
        max_length=10, choices=DunningNoticeStatus.choices,
        default=DunningNoticeStatus.PENDING,
    )
    sent_at = models.DateTimeField(null=True, blank=True)

    class Meta(FinanceDocument.Meta):
        constraints = FinanceDocument.Meta.constraints + [
            models.UniqueConstraint(
                fields=["invoice", "level"], name="uniq_finance_dunning_notice_invoice_level",
            ),
        ]
        indexes = [
            models.Index(fields=["entity", "notice_status"]),
            models.Index(fields=["customer"]),
            models.Index(fields=["invoice"]),
            models.Index(fields=["entity", "notice_date"]),
        ]

    def __str__(self) -> str:
        return f"{self.document_number or 'DUN?'} L{self.level} {self.invoice_id} ({self.notice_status})"



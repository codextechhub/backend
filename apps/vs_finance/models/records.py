"""Records kept to prove the books: the sealed figures of each closed period and year."""
from __future__ import annotations

from django.conf import settings
from django.db import models
from django.utils import timezone

from .core import LedgerEntity
from .gl import FiscalPeriod, FiscalYear

__all__ = ["LedgerSeal"]


class LedgerSeal(models.Model):
    """The figures of a period or a fiscal year as they stood when it was sealed.

    Written each time a month is closed or locked and each time a year is
    closed (:mod:`vs_finance.seals`). It holds two things:

    * ``balances``: the closing balance of every account, branch by branch, in
      kobo, as ``{"<branch id>": {"<account id>": [debit, credit]}}``, where an
      empty branch key holds lines that name no branch at a tenant with several.
      A month's balances are the cumulative ledger through its last day,
      **before year-end closing entries**: a later year close moves income into
      Retained Earnings without changing any month already sealed. A year's
      balances include its own closing entries and every earlier year's.
    * ``lines_checksum``: a SHA-256 over every ledger line the seal covers (the
      month's lines, or all of the year's), with ``line_count``.

    Each seal names the entity's previous seal, and ``seal_checksum`` is a
    SHA-256 over its own figures and the previous seal's checksum, so the seals
    form one chain per set of books: altering or removing any seal breaks every
    seal after it. Rows are append-only, refused in Python here and at the
    database by triggers.

    Reopening a period leaves its seals in place. Only the latest seal of a
    period or year that is still closed is checked against the ledger
    (:func:`vs_finance.seals.verify_entity`); a reopened period is resealed when
    it closes again.
    """

    class Kind(models.TextChoices):
        PERIOD_CLOSED = "PERIOD_CLOSED", "Period closed"
        PERIOD_LOCKED = "PERIOD_LOCKED", "Period locked"
        YEAR_CLOSED = "YEAR_CLOSED", "Fiscal year closed"

    entity = models.ForeignKey(
        LedgerEntity, on_delete=models.PROTECT, related_name="ledger_seals",
    )
    fiscal_year = models.ForeignKey(
        FiscalYear, on_delete=models.PROTECT, related_name="ledger_seals",
    )
    period = models.ForeignKey(
        FiscalPeriod, on_delete=models.PROTECT, related_name="ledger_seals",
        null=True, blank=True, help_text="Blank for a seal of the whole fiscal year.",
    )
    kind = models.CharField(max_length=16, choices=Kind.choices)
    sealed_at = models.DateTimeField(default=timezone.now, editable=False)
    sealed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT,
        related_name="finance_ledger_seals", null=True, blank=True,
    )
    line_count = models.PositiveIntegerField(default=0)
    lines_checksum = models.CharField(max_length=64)
    balances = models.JSONField(default=dict)
    previous = models.OneToOneField(
        "self", on_delete=models.PROTECT, related_name="next_seal",
        null=True, blank=True,
    )
    seal_checksum = models.CharField(max_length=64)

    class Meta:
        indexes = [
            models.Index(fields=["entity", "period"], name="vs_finance_seal_period_idx"),
            models.Index(fields=["entity", "fiscal_year"], name="vs_finance_seal_year_idx"),
        ]
        ordering = ["-id"]

    def __str__(self) -> str:
        return f"{self.kind} {self.period_id or self.fiscal_year_id} [{self.seal_checksum[:12]}]"

    def save(self, *args, **kwargs):
        if self.pk is not None:
            raise ValueError("LedgerSeal rows are immutable and cannot be updated.")
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValueError("LedgerSeal rows are immutable and cannot be deleted.")

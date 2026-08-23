"""Date each AR settlement so an "as at" report can be rebuilt from the sub-ledger.

An allocation row said WHICH invoice a receipt or credit note settled, but never
WHEN that settlement took effect. Aging, statements and reconciliation therefore had
no choice but to read today's balances, so a September payment silently changed what
a June report said and no historical figure could be reproduced twice.

``effective_date`` is the date of the journal that credited AR for the settlement -
not when the row was written. Anchoring on the journal is what lets the sub-ledger
reconstruction and the GL agree at every point on the timeline.

The unique constraints go at the same time, and for the same reason. A row could
previously accumulate: settle an invoice from a receipt today and again next month
and the two tranches merged into one row, which could then honestly carry neither
date. Rows are now one immutable event each, matching how the void services already
describe them.

Backfill is a RECONSTRUCTION, not a record: the true effective date was never
captured, so existing rows get max(crediting document date, settled document date),
which is what the services would have stamped had the column existed. It is exact
wherever a pair was settled in a single run - which, on the data this shipped
against, was every row.
"""

from django.db import migrations, models


def _reconstruct_effective_dates(apps, schema_editor):
    """Date historical allocations as max(crediting document, settled document)."""
    for model_name, credit_attr, credit_field, target_attr, target_field in (
        ("PaymentAllocation", "payment", "payment_date", "invoice", "invoice_date"),
        ("CreditNoteAllocation", "note", "note_date", "invoice", "invoice_date"),
        ("DebitNoteAllocation", "payment", "payment_date", "note", "note_date"),
    ):
        model = apps.get_model("vs_finance", model_name)
        updates = []
        for row in model.objects.select_related(credit_attr, target_attr).all():
            credit_date = getattr(getattr(row, credit_attr), credit_field, None)
            target_date = getattr(getattr(row, target_attr), target_field, None)
            candidates = [d for d in (credit_date, target_date) if d is not None]
            if not candidates:
                continue
            row.effective_date = max(candidates)
            updates.append(row)
        if updates:
            model.objects.bulk_update(updates, ["effective_date"], batch_size=500)


def _clear_effective_dates(apps, schema_editor):
    """Reverse cleanly; the column itself is dropped by the schema operations."""
    for model_name in ("PaymentAllocation", "CreditNoteAllocation", "DebitNoteAllocation"):
        apps.get_model("vs_finance", model_name).objects.update(effective_date=None)


class Migration(migrations.Migration):
    dependencies = [
        ("vs_finance", "0018_alter_financeauditlog_action"),
    ]

    operations = [
        migrations.RemoveConstraint(
            model_name="creditnoteallocation",
            name="uniq_finance_cnalloc_note_invoice",
        ),
        migrations.RemoveConstraint(
            model_name="debitnoteallocation",
            name="uniq_finance_dnalloc_payment_note",
        ),
        migrations.RemoveConstraint(
            model_name="paymentallocation",
            name="uniq_finance_alloc_payment_invoice",
        ),
        migrations.AddField(
            model_name="creditnoteallocation",
            name="effective_date",
            field=models.DateField(
                blank=True,
                help_text="Accounting date this settlement took effect - the date of the journal that credited AR for it. Null only on rows predating the column, where it is reconstructed as max(note date, invoice date).",
                null=True,
            ),
        ),
        migrations.AddField(
            model_name="debitnoteallocation",
            name="effective_date",
            field=models.DateField(
                blank=True,
                help_text="Accounting date this settlement took effect - the date of the journal that credited AR for it. Null only on rows predating the column, where it is reconstructed as max(receipt date, note date).",
                null=True,
            ),
        ),
        migrations.AddField(
            model_name="paymentallocation",
            name="effective_date",
            field=models.DateField(
                blank=True,
                help_text="Accounting date this settlement took effect - the date of the journal that credited AR for it. Null only on rows predating the column, where it is reconstructed as max(receipt date, invoice date).",
                null=True,
            ),
        ),
        migrations.AddIndex(
            model_name="creditnoteallocation",
            index=models.Index(
                fields=["effective_date"], name="vs_finance__effecti_003406_idx"
            ),
        ),
        migrations.AddIndex(
            model_name="debitnoteallocation",
            index=models.Index(
                fields=["effective_date"], name="vs_finance__effecti_338b0b_idx"
            ),
        ),
        migrations.AddIndex(
            model_name="paymentallocation",
            index=models.Index(
                fields=["effective_date"], name="vs_finance__effecti_09e34c_idx"
            ),
        ),
        migrations.RunPython(_reconstruct_effective_dates, _clear_effective_dates),
    ]

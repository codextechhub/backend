"""Attribute customer refunds to the credit lots they were paid out of.

A refund debits the customer-credit liability (2140) as one lump, and nothing
recorded *which* credit it drained. The originating receipt therefore went on
reporting its cash as unapplied and available after it had been handed back —
two parts of the system disagreeing about whether the money was still there.

Schema adds the attribution rows plus the denormalised ``refunded_amount`` each
lot needs so list filters and KPI aggregates stay expressible in SQL. The data
step then backfills existing posted refunds, oldest credit first, so the new
read model is correct the moment the migration lands rather than after a
separate command someone has to remember to run.

The backfill deliberately ignores accounting dates. Refunds already in the books
include ones that were backdated before their credit existed — that is the very
defect this change prevents going forward — and re-litigating history here would
leave those refunds unattributed and the balances overstated. Attribution is
about *which* credit was spent, not whether spending it was allowed.
"""

import django.db.models.deletion
import django.utils.timezone
import vs_finance.money
from django.db import migrations, models


def _attribute_existing_refunds(apps, schema_editor):
    """Draw every posted refund from its customer's credit lots, oldest lot first."""
    Payment = apps.get_model("vs_finance", "Payment")
    CreditNote = apps.get_model("vs_finance", "CreditNote")
    Refund = apps.get_model("vs_finance", "Refund")
    RefundAllocation = apps.get_model("vs_finance", "RefundAllocation")

    refunds = list(
        Refund.objects.filter(status="POSTED").order_by("customer_id", "refund_date", "id")
    )
    if not refunds:
        return

    customer_ids = {refund.customer_id for refund in refunds}
    # One lot list per customer: posted receipts and posted CREDIT notes, oldest first,
    # each holding whatever it never allocated to an invoice.
    lots_by_customer = {}
    for payment in Payment.objects.filter(
        customer_id__in=customer_ids, status="POSTED",
    ).only("id", "customer_id", "payment_date", "amount", "allocated_amount"):
        remaining = int(payment.amount) - int(payment.allocated_amount)
        if remaining > 0:
            lots_by_customer.setdefault(payment.customer_id, []).append(
                [payment.payment_date, payment.id, "RECEIPT", payment.id, remaining],
            )
    for note in CreditNote.objects.filter(
        customer_id__in=customer_ids, status="POSTED", kind="CREDIT",
    ).only("id", "customer_id", "note_date", "total", "allocated_amount"):
        remaining = int(note.total) - int(note.allocated_amount)
        if remaining > 0:
            lots_by_customer.setdefault(note.customer_id, []).append(
                [note.note_date, note.id, "CREDIT_NOTE", note.id, remaining],
            )
    for lots in lots_by_customer.values():
        lots.sort(key=lambda lot: (lot[0], lot[1]))

    rows, drained_payments, drained_notes = [], {}, {}
    for refund in refunds:
        outstanding = int(refund.amount)
        for lot in lots_by_customer.get(refund.customer_id, []):
            if outstanding <= 0:
                break
            take = min(lot[4], outstanding)
            if take <= 0:
                continue
            lot[4] -= take
            outstanding -= take
            if lot[2] == "RECEIPT":
                rows.append(RefundAllocation(refund_id=refund.id, payment_id=lot[3], amount=take))
                drained_payments[lot[3]] = drained_payments.get(lot[3], 0) + take
            else:
                rows.append(RefundAllocation(refund_id=refund.id, note_id=lot[3], amount=take))
                drained_notes[lot[3]] = drained_notes.get(lot[3], 0) + take
        if outstanding > 0:
            # No identifiable credit left to point at. Inventing a source would be
            # worse than leaving it unattributed, so record it loudly instead.
            print(
                f"  ! Refund {refund.document_number or refund.id}: {outstanding} kobo "
                f"could not be matched to any receipt or credit note.",
            )

    RefundAllocation.objects.bulk_create(rows, batch_size=500)
    for payment_id, amount in drained_payments.items():
        Payment.objects.filter(pk=payment_id).update(refunded_amount=amount)
    for note_id, amount in drained_notes.items():
        CreditNote.objects.filter(pk=note_id).update(refunded_amount=amount)


def _unattribute_existing_refunds(apps, schema_editor):
    """Drop the attribution so the migration is cleanly reversible."""
    apps.get_model("vs_finance", "RefundAllocation").objects.all().delete()
    apps.get_model("vs_finance", "Payment").objects.update(refunded_amount=0)
    apps.get_model("vs_finance", "CreditNote").objects.update(refunded_amount=0)


class Migration(migrations.Migration):
    dependencies = [
        ("vs_finance", "0010_bank_statement_correction_audit_action"),
    ]

    operations = [
        migrations.AddField(
            model_name="creditnote",
            name="refunded_amount",
            field=vs_finance.money.MoneyField(
                help_text="Portion of a CREDIT note's unapplied value already paid back out as a customer refund, in kobo. Maintained by RefundAllocation."
            ),
        ),
        migrations.AddField(
            model_name="payment",
            name="refunded_amount",
            field=vs_finance.money.MoneyField(
                help_text="Portion of this receipt's unapplied cash already paid back out as a customer refund, in kobo. Maintained by RefundAllocation."
            ),
        ),
        migrations.CreateModel(
            name="RefundAllocation",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                (
                    "created_at",
                    models.DateTimeField(
                        default=django.utils.timezone.now, editable=False
                    ),
                ),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "amount",
                    vs_finance.money.MoneyField(
                        help_text="Amount of the refund drawn from this lot, in kobo."
                    ),
                ),
                (
                    "note",
                    models.ForeignKey(
                        blank=True,
                        help_text="The CREDIT note whose unapplied value funded this slice of the refund.",
                        null=True,
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="refund_allocations",
                        to="vs_finance.creditnote",
                    ),
                ),
                (
                    "payment",
                    models.ForeignKey(
                        blank=True,
                        help_text="The receipt whose unapplied cash funded this slice of the refund.",
                        null=True,
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="refund_allocations",
                        to="vs_finance.payment",
                    ),
                ),
                (
                    "refund",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="allocations",
                        to="vs_finance.refund",
                    ),
                ),
            ],
            options={
                "ordering": ["refund", "id"],
                "indexes": [
                    models.Index(
                        fields=["refund"], name="vs_finance__refund__154cbc_idx"
                    ),
                    models.Index(
                        fields=["payment"], name="vs_finance__payment_513f40_idx"
                    ),
                    models.Index(
                        fields=["note"], name="vs_finance__note_id_7df596_idx"
                    ),
                ],
                "constraints": [
                    models.UniqueConstraint(
                        condition=models.Q(("payment__isnull", False)),
                        fields=("refund", "payment"),
                        name="uniq_finance_refalloc_refund_payment",
                    ),
                    models.UniqueConstraint(
                        condition=models.Q(("note__isnull", False)),
                        fields=("refund", "note"),
                        name="uniq_finance_refalloc_refund_note",
                    ),
                    models.CheckConstraint(
                        condition=models.Q(("amount__gte", 0)),
                        name="ck_finance_refalloc_non_negative",
                    ),
                    models.CheckConstraint(
                        condition=models.Q(
                            models.Q(
                                ("note__isnull", True), ("payment__isnull", False)
                            ),
                            models.Q(
                                ("note__isnull", False), ("payment__isnull", True)
                            ),
                            _connector="OR",
                        ),
                        name="ck_finance_refalloc_one_source",
                    ),
                ],
            },
        ),
        migrations.RunPython(
            _attribute_existing_refunds, _unattribute_existing_refunds,
        ),
    ]

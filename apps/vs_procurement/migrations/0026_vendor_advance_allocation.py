"""Date each AP settlement, and attach later advance draw-downs to their journals.

A vendor payment now splits its debit at source: AP is debited only for what the
payment settles, and money paid ahead of a bill goes to the 1240 vendor-advance
asset. That makes allocation a GL event rather than a purely sub-ledger one, and two
things follow.

``effective_date`` records **when** a settlement debited AP. An allocation row used to
say only which bill a payment settled, which was enough while the payment journal moved
the whole gross on its own date. Now that applying an advance raises its own journal -
dated at the later of the payment and the bill - a row without a date puts settlements
on the timeline before the ledger moved, and every "as at" aging or reconciliation
between the two dates disagrees with the GL.

The unique constraint goes at the same time, and for the same reason. A row could
previously accumulate: settle a bill from a payment today and again next month and the
two tranches merged into one row, which could then honestly carry neither date. Rows are
now one immutable event each, exactly as :class:`vs_finance.models.PaymentAllocation`
became in ``vs_finance.0019``.

:class:`VendorAdvanceAllocationJournal` keeps each reclassification journal attached to
the payment that owns it, so reversing the payment can unwind all of its GL effects
rather than only the original disbursement.

Backfill is a RECONSTRUCTION, not a record: the true effective date was never captured,
so existing rows get max(payment date, bill date), which is what the service would have
stamped had the column existed. Under the pre-split behaviour every settlement was
debited to AP by the payment journal itself, so on this data the reconstruction is exact
wherever the bill was not newer than the payment - and a payment could not settle a
newer bill, because the date guard already refused it.
"""

import django.db.models.deletion
import django.utils.timezone
import vs_finance.money
from django.db import migrations, models


def _reconstruct_effective_dates(apps, schema_editor):
    """Date historical vendor-payment allocations as max(payment date, bill date)."""
    model = apps.get_model("vs_procurement", "VendorPaymentAllocation")
    updates = []
    for row in model.objects.select_related("payment", "vendor_invoice").all():
        candidates = [
            date for date in (
                getattr(row.payment, "payment_date", None),
                getattr(row.vendor_invoice, "invoice_date", None),
            ) if date is not None
        ]
        if not candidates:
            continue
        row.effective_date = max(candidates)
        updates.append(row)
    if updates:
        model.objects.bulk_update(updates, ["effective_date"], batch_size=500)


def _clear_effective_dates(apps, schema_editor):
    """Reverse cleanly; the column itself is dropped by the schema operations."""
    apps.get_model("vs_procurement", "VendorPaymentAllocation").objects.update(
        effective_date=None,
    )


class Migration(migrations.Migration):
    dependencies = [
        ("vs_finance", "0020_alter_financeaccountmapping_key"),
        ("vs_procurement", "0025_procurement_stages_route_by_branch"),
    ]

    operations = [
        migrations.CreateModel(
            name="VendorAdvanceAllocationJournal",
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
                        help_text="Vendor advance reclassified to AP, in kobo."
                    ),
                ),
            ],
            options={
                "ordering": ["journal_id", "id"],
            },
        ),
        migrations.RemoveConstraint(
            model_name="vendorpaymentallocation",
            name="uniq_proc_alloc_payment_invoice",
        ),
        migrations.AddField(
            model_name="vendorpaymentallocation",
            name="effective_date",
            field=models.DateField(
                blank=True,
                help_text="Accounting date this settlement took effect - the date of the journal that debited AP for it. Null only on rows predating the column, where it is reconstructed as max(payment date, bill date).",
                null=True,
            ),
        ),
        migrations.AddIndex(
            model_name="vendorpaymentallocation",
            index=models.Index(
                fields=["effective_date"], name="vs_procurem_effecti_9666cc_idx"
            ),
        ),
        migrations.AddField(
            model_name="vendoradvanceallocationjournal",
            name="journal",
            field=models.OneToOneField(
                on_delete=django.db.models.deletion.PROTECT,
                related_name="vendor_advance_allocation",
                to="vs_finance.journalentry",
            ),
        ),
        migrations.AddField(
            model_name="vendoradvanceallocationjournal",
            name="payment",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.PROTECT,
                related_name="advance_allocation_journals",
                to="vs_procurement.vendorpayment",
            ),
        ),
        migrations.RunPython(_reconstruct_effective_dates, _clear_effective_dates),
    ]

from django.db import migrations, models
from django.db.models import F, Q


def approve_posted_payments(apps, schema_editor):
    VendorPayment = apps.get_model("vs_procurement", "VendorPayment")
    # Historical posted payments predate workflow enforcement but already moved GL/AP.
    VendorPayment.objects.filter(status="POSTED").update(approval_state="APPROVED")


class Migration(migrations.Migration):
    dependencies = [("vs_procurement", "0006_backfill_grn_draft_values")]

    operations = [
        migrations.AddField(
            model_name="vendorpayment",
            name="approval_state",
            field=models.CharField(
                choices=[
                    ("NOT_SUBMITTED", "Not submitted"), ("PENDING", "Pending approval"),
                    ("APPROVED", "Approved"), ("REJECTED", "Rejected"),
                ],
                default="NOT_SUBMITTED", max_length=16,
                help_text="Payment-approval state driven by vs_workflow (overlay; not ledger status).",
            ),
        ),
        migrations.RunPython(approve_posted_payments, migrations.RunPython.noop),
        migrations.AddIndex(
            model_name="vendorpayment",
            index=models.Index(fields=["entity", "approval_state"], name="proc_pay_entity_approval_idx"),
        ),
        migrations.AddConstraint(
            model_name="vendorpayment",
            constraint=models.CheckConstraint(condition=Q(gross_amount__gt=0), name="ck_proc_payment_gross_positive"),
        ),
        migrations.AddConstraint(
            model_name="vendorpayment",
            constraint=models.CheckConstraint(condition=Q(wht_amount__gte=0) & Q(wht_amount__lte=F("gross_amount")), name="ck_proc_payment_wht_within_gross"),
        ),
        migrations.AddConstraint(
            model_name="vendorpayment",
            constraint=models.CheckConstraint(condition=Q(allocated_amount__gte=0) & Q(allocated_amount__lte=F("gross_amount")), name="ck_proc_payment_alloc_within_gross"),
        ),
    ]

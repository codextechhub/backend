"""Keep one vendor RFQ together while each participating branch receives its own order."""

import django.db.models.deletion
import django.utils.timezone
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("vs_finance", "0001_initial"),
        ("vs_procurement", "0044_superseded_evidence"),
        ("vs_tenants", "0010_remove_branch__type"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="SharedSourcingGroup",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("created_at", models.DateTimeField(default=django.utils.timezone.now, editable=False)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("awarded_quotation", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name="shared_sourcing_awards", to="vs_procurement.vendorquotation")),
                ("created_by", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name="shared_sourcing_groups_created", to=settings.AUTH_USER_MODEL)),
                ("entity", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="shared_sourcing_groups", to="vs_finance.ledgerentity")),
                ("rfq", models.OneToOneField(on_delete=django.db.models.deletion.PROTECT, related_name="shared_sourcing_group", to="vs_procurement.requestforquotation")),
            ],
        ),
        migrations.CreateModel(
            name="SharedSourcingAllocation",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("created_at", models.DateTimeField(default=django.utils.timezone.now, editable=False)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("quantity", models.DecimalField(decimal_places=4, max_digits=14)),
                ("group", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="allocations", to="vs_procurement.sharedsourcinggroup")),
                ("requisition_line", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="shared_sourcing_allocations", to="vs_procurement.purchaserequisitionline")),
                ("rfq_line", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="shared_allocations", to="vs_procurement.rfqline")),
            ],
            options={
                "constraints": [
                    models.UniqueConstraint(fields=("requisition_line",), name="uniq_shared_sourcing_source_line"),
                    models.CheckConstraint(condition=models.Q(("quantity__gt", 0)), name="shared_sourcing_allocation_quantity_positive"),
                ],
            },
        ),
        migrations.CreateModel(
            name="SharedSourcingOrder",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("created_at", models.DateTimeField(default=django.utils.timezone.now, editable=False)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("branch", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="shared_sourcing_orders", to="vs_tenants.branch")),
                ("group", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="orders", to="vs_procurement.sharedsourcinggroup")),
                ("purchase_order", models.OneToOneField(on_delete=django.db.models.deletion.PROTECT, related_name="shared_sourcing_order", to="vs_procurement.purchaseorder")),
            ],
            options={
                "constraints": [models.UniqueConstraint(fields=("group", "branch"), name="uniq_shared_sourcing_group_branch")],
            },
        ),
    ]

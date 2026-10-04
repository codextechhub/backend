"""Keep a separate close state for every branch's fiscal-period books."""

import django.db.models.deletion
import django.utils.timezone
from django.conf import settings
from django.db import migrations, models


def seed_branch_periods(apps, schema_editor):
    Period = apps.get_model("vs_finance", "FiscalPeriod")
    BranchPeriod = apps.get_model("vs_finance", "BranchFiscalPeriod")
    Branch = apps.get_model("vs_tenants", "Branch")
    for period in Period.objects.select_related("entity").filter(is_closing=False).iterator():
        branches = Branch._base_manager.filter(
            tenant_id=period.entity.tenant_id,
        ).values_list("pk", flat=True)
        BranchPeriod.objects.bulk_create([
            BranchPeriod(
                period_id=period.pk, branch_id=branch_id, status=period.status,
                closed_at=period.closed_at, closed_by_id=period.closed_by_id,
            ) for branch_id in branches
        ])


def seed_branch_years(apps, schema_editor):
    FiscalYear = apps.get_model("vs_finance", "FiscalYear")
    BranchYear = apps.get_model("vs_finance", "BranchFiscalYear")
    Branch = apps.get_model("vs_tenants", "Branch")
    for fiscal_year in FiscalYear.objects.select_related("entity").iterator():
        branches = Branch._base_manager.filter(
            tenant_id=fiscal_year.entity.tenant_id,
        ).values_list("pk", flat=True)
        BranchYear.objects.bulk_create([
            BranchYear(
                fiscal_year_id=fiscal_year.pk, branch_id=branch_id,
                status=fiscal_year.status,
            ) for branch_id in branches
        ])


class Migration(migrations.Migration):
    dependencies = [
        ("vs_finance", "0060_record_retention_seals_and_archive"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="BranchFiscalPeriod",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("created_at", models.DateTimeField(default=django.utils.timezone.now, editable=False)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("status", models.CharField(choices=[("OPEN", "Open"), ("SOFT_CLOSED", "Soft Closed"), ("CLOSED", "Closed"), ("LOCKED", "Locked")], default="OPEN", max_length=12)),
                ("closed_at", models.DateTimeField(blank=True, null=True)),
                ("branch", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, to="vs_tenants.branch")),
                ("closed_by", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name="finance_branch_periods_closed", to=settings.AUTH_USER_MODEL)),
                ("period", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="branch_statuses", to="vs_finance.fiscalperiod")),
            ],
            options={
                "indexes": [models.Index(fields=["branch", "status"], name="vs_finance__branch__0f9f8f_idx")],
                "constraints": [models.UniqueConstraint(fields=("period", "branch"), name="uniq_finance_branch_period")],
            },
        ),
        migrations.CreateModel(
            name="BranchFiscalYear",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("created_at", models.DateTimeField(default=django.utils.timezone.now, editable=False)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("status", models.CharField(choices=[("OPEN", "Open"), ("SOFT_CLOSED", "Soft Closed"), ("CLOSED", "Closed"), ("LOCKED", "Locked")], default="OPEN", max_length=12)),
                ("closed_at", models.DateTimeField(blank=True, null=True)),
                ("branch", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, to="vs_tenants.branch")),
                ("closed_by", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name="finance_branch_years_closed", to=settings.AUTH_USER_MODEL)),
                ("fiscal_year", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="branch_statuses", to="vs_finance.fiscalyear")),
            ],
            options={
                "indexes": [models.Index(fields=["branch", "status"], name="vs_finance__branch__8791f9_idx")],
                "constraints": [models.UniqueConstraint(fields=("fiscal_year", "branch"), name="uniq_finance_branch_fiscal_year")],
            },
        ),
        migrations.RunPython(seed_branch_periods, migrations.RunPython.noop),
        migrations.RunPython(seed_branch_years, migrations.RunPython.noop),
    ]

"""Pay brought forward into a tax year, and the two payroll settings that go with it.

Adds :class:`~vs_finance.models.PayBroughtForward`, at most one row of each
kind (a previous employer's months, or this employer's own months before its
payroll ran here) per salary record per tax year, and the tenant's
``previous_pay_required`` (off) and ``payroll_moved_here_on`` (empty) payroll
settings. Nothing changes on its own: with no rows and the defaults, PAYE is
worked out exactly as before.
"""

import django.db.models.deletion
import django.utils.timezone
import vs_finance.money
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("vs_finance", "0058_payer_payments"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.AddField(
            model_name="financepayrollsettings",
            name="payroll_moved_here_on",
            field=models.DateField(
                blank=True,
                help_text="For a tenant that ran payroll elsewhere earlier in a tax year: a day in the first payroll month run on these books. Staff first paid in that month are its own staff carried over, not mid-year joiners.",
                null=True,
            ),
        ),
        migrations.AddField(
            model_name="financepayrollsettings",
            name="previous_pay_required",
            field=models.BooleanField(
                default=False,
                help_text="Refuse to pay somebody first paid after January of a tax year until their earlier pay that year is recorded (zeros where there was none).",
            ),
        ),
        migrations.CreateModel(
            name="PayBroughtForward",
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
                ("tax_year", models.PositiveSmallIntegerField()),
                (
                    "source",
                    models.CharField(
                        choices=[
                            ("PREVIOUS_EMPLOYER", "Previous employer"),
                            ("THIS_EMPLOYER", "This employer, before payroll ran here"),
                        ],
                        default="PREVIOUS_EMPLOYER",
                        max_length=20,
                    ),
                ),
                (
                    "employer_name",
                    models.CharField(blank=True, default="", max_length=160),
                ),
                (
                    "evidence_reference",
                    models.CharField(
                        blank=True,
                        default="",
                        help_text="Where the figures come from: a previous employer's tax card number, or a note.",
                        max_length=120,
                    ),
                ),
                (
                    "gross_amount",
                    vs_finance.money.MoneyField(
                        help_text="Gross pay of the months brought forward, in kobo."
                    ),
                ),
                (
                    "taxable_pay",
                    vs_finance.money.MoneyField(
                        help_text="Its taxable part before reliefs, in kobo."
                    ),
                ),
                (
                    "paye_amount",
                    vs_finance.money.MoneyField(
                        help_text="PAYE deducted in those months, in kobo."
                    ),
                ),
                (
                    "pension_amount",
                    vs_finance.money.MoneyField(
                        help_text="Employee pension deducted in those months, in kobo."
                    ),
                ),
                (
                    "nhf_amount",
                    vs_finance.money.MoneyField(
                        help_text="NHF deducted in those months, in kobo."
                    ),
                ),
                (
                    "created_by",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="finance_pay_brought_forward_created",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
                (
                    "salary",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="pay_brought_forward",
                        to="vs_finance.employeesalary",
                    ),
                ),
                (
                    "updated_by",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="finance_pay_brought_forward_updated",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
            ],
            options={
                "ordering": ["salary", "-tax_year", "source"],
                "constraints": [
                    models.UniqueConstraint(
                        fields=("salary", "tax_year", "source"),
                        name="uniq_finance_pay_brought_forward",
                    )
                ],
            },
        ),
    ]

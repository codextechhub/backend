"""Every fiscal year gets a closing period, and year-end journals move into it.

The year-end close used to post into the year's last month. Every report that
read that month or that year then saw the whole year's income and expense
cancelled: the full-year P&L read zero, the last month read minus a year, and
the next year's prior-year column was empty. The closing period (period 13, one
day long on the year's last day, CLOSED from the start) keeps the close apart
from the months.

The data step creates the closing period for every existing year and moves any
closing journal already posted, with the reversal that reopening a year posts,
into it. Each moved journal's amounts leave the ``AccountBalance`` rows of the
month it sat in and join the closing period's, and its date becomes the year's
last day, so the aggregates still equal the journal lines they summarise. The
reverse step moves nothing back; it only lets the schema step unwind.
"""

from django.conf import settings
from django.db import migrations, models
from django.db.models import F, Max, Q

CLOSING_PERIOD_NO = 13
POSTED_STATUSES = ("POSTED", "REVERSED")


def _closing_period(FiscalPeriod, year):
    existing = FiscalPeriod.objects.filter(fiscal_year=year, is_closing=True).first()
    if existing is not None:
        return existing
    highest = FiscalPeriod.objects.filter(fiscal_year=year).aggregate(n=Max("period_no"))["n"] or 0
    return FiscalPeriod.objects.create(
        entity_id=year.entity_id, fiscal_year=year, is_closing=True,
        period_no=max(CLOSING_PERIOD_NO, highest + 1),
        name=f"FY{year.year} closing",
        start_date=year.end_date, end_date=year.end_date, status="CLOSED",
    )


def _shift_balances(AccountBalance, entry, source_period_id, target_period_id):
    for line in entry.lines.all():
        if source_period_id is not None:
            AccountBalance.objects.filter(
                account_id=line.account_id, period_id=source_period_id,
            ).update(
                debit_total=F("debit_total") - line.debit,
                credit_total=F("credit_total") - line.credit,
            )
        balance, _ = AccountBalance.objects.get_or_create(
            account_id=line.account_id, period_id=target_period_id,
        )
        AccountBalance.objects.filter(pk=balance.pk).update(
            debit_total=F("debit_total") + line.debit,
            credit_total=F("credit_total") + line.credit,
        )


def add_closing_periods(apps, schema_editor):
    """Create each year's closing period and move its year-end journals into it."""
    FiscalYear = apps.get_model("vs_finance", "FiscalYear")
    FiscalPeriod = apps.get_model("vs_finance", "FiscalPeriod")
    JournalEntry = apps.get_model("vs_finance", "JournalEntry")
    AccountBalance = apps.get_model("vs_finance", "AccountBalance")

    for year in FiscalYear.objects.all().iterator():
        closing = _closing_period(FiscalPeriod, year)
        entries = JournalEntry.objects.filter(
            Q(closes_fiscal_year=year) | Q(reverses__closes_fiscal_year=year),
        ).exclude(period_id=closing.pk)
        for entry in entries:
            if entry.status in POSTED_STATUSES:
                _shift_balances(AccountBalance, entry, entry.period_id, closing.pk)
            JournalEntry.objects.filter(pk=entry.pk).update(
                period_id=closing.pk, date=year.end_date,
            )


class Migration(migrations.Migration):
    dependencies = [
        ("vs_finance", "0033_a_closing_journal_belongs_to_its_year"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.AddField(
            model_name="fiscalperiod",
            name="is_closing",
            field=models.BooleanField(
                default=False,
                help_text="The year's closing period: only the year-end closing journals post here.",
            ),
        ),
        migrations.AddConstraint(
            model_name="fiscalperiod",
            constraint=models.UniqueConstraint(
                condition=models.Q(("is_closing", True)),
                fields=("fiscal_year",),
                name="uniq_finance_closing_period_per_year",
            ),
        ),
        migrations.RunPython(add_closing_periods, migrations.RunPython.noop),
    ]

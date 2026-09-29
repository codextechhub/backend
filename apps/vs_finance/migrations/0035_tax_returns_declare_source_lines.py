"""Tax returns declare source lines, split by branch, and own their remittances.

The schema step adds the declaration link (``TaxFilingLine``), the per-branch
shares and the remittance rows, the carried-forward credit on a return, the
``TAX`` journal source, and a treatment on tax codes held at a zero rate unless
standard.

The data step brings existing books onto that model:

* The starter VAT codes (standard, zero rated, exempt) are added to every entity
  whose chart has the VAT accounts, leaving any code a tenant already has alone.
  The exempt code needs no accounts, so every entity gets it, and every existing
  fee item with no tax code then takes its entity's exempt code (not undone on
  reverse).
* Every journal the tax module posted before (a return's netting/penalty journal,
  and each remittance named in the finance audit trail) takes the ``TAX`` source,
  so none of them is ever read as a source line.
* Each remittance found in the audit trail becomes a ``TaxRemittance`` row linked
  to its journal, marked reversed where the journal was reversed.
* Each existing return gets one share holding its own figures and payments, under
  its branch, or the tenant's only branch.
* Each filed or paid return declares the source lines dated inside its own period.
  Lines dated before an obligation's first filed return are declared by that
  return too: they belong to months handled before the books were kept here, and
  leaving them undeclared would put them all on the next return as late items.
  Lines in a gap between filed returns stay undeclared and appear on the next
  return under the month they are dated in.

The reverse step gives the retagged journals their earlier sources back so the
schema step can unwind.
"""

import datetime

import django.db.models.deletion
import django.utils.timezone
import vs_finance.money
from django.conf import settings
from django.db import migrations, models

POSTED_STATUSES = ("POSTED", "REVERSED")
FILED_STATUSES = ("FILED", "PAID")
STARTER_TAX_CODES = (
    ("VAT-STD", "VAT 7.5%", 750, "STANDARD", "2200", "1300", True),
    ("VAT-ZERO", "VAT zero rated", 0, "ZERO_RATED", "2200", "1300", True),
    ("VAT-EXEMPT", "VAT exempt", 0, "EXEMPT", None, None, False),
)


def _seed_tax_codes(apps):
    Account = apps.get_model("vs_finance", "Account")
    LedgerEntity = apps.get_model("vs_finance", "LedgerEntity")
    TaxCode = apps.get_model("vs_finance", "TaxCode")
    for entity in LedgerEntity.objects.all():
        accounts = {a.code: a for a in Account.objects.filter(entity=entity, code__in=("2200", "1300"))}
        for code, name, rate, treatment, collected, paid, recoverable in STARTER_TAX_CODES:
            if (collected and collected not in accounts) or (paid and paid not in accounts):
                continue
            TaxCode.objects.get_or_create(
                entity=entity, code=code,
                defaults={
                    "name": name, "rate_bps": rate, "treatment": treatment,
                    "is_recoverable": recoverable,
                    "collected_account": accounts.get(collected) if collected else None,
                    "paid_account": accounts.get(paid) if paid else None,
                },
            )


def exempt_blank_fee_items(apps, schema_editor):
    """Give every fee item with no tax code its entity's exempt VAT code.

    Runs after the starter codes are seeded, which always makes the exempt code
    (it needs no accounts), so old items read like new ones: a blank code is no
    longer a silent stand-in for "exempt".
    """
    FeeItem = apps.get_model("vs_finance", "FeeItem")
    TaxCode = apps.get_model("vs_finance", "TaxCode")
    entity_ids = (
        FeeItem.objects.filter(tax_code__isnull=True)
        .values_list("structure__entity_id", flat=True).distinct()
    )
    for entity_id in list(entity_ids):
        exempt = TaxCode.objects.filter(entity_id=entity_id, code="VAT-EXEMPT").first()
        if exempt is None:
            continue
        FeeItem.objects.filter(
            structure__entity_id=entity_id, tax_code__isnull=True,
        ).update(tax_code=exempt)


def _only_branch(Branch, tenant_id, cache):
    if tenant_id not in cache:
        ids = list(Branch._default_manager.filter(tenant_id=tenant_id).values_list("pk", flat=True)[:2])
        cache[tenant_id] = (ids[0] if len(ids) == 1 else None, bool(ids))
    return cache[tenant_id]


def _remittances_from_audit(apps, filing):
    """``(journal, amount)`` for every remittance the audit trail records on ``filing``."""
    FinanceAuditLog = apps.get_model("vs_finance", "FinanceAuditLog")
    JournalEntry = apps.get_model("vs_finance", "JournalEntry")
    rows = FinanceAuditLog.objects.filter(
        entity_id=filing.entity_id, action="TAX_FILING_PAID", status="SUCCESS",
        target_type="TaxFiling", target_id=str(filing.pk),
    ).order_by("created_at", "id")
    found = []
    for row in rows:
        journal_id = (row.metadata or {}).get("journal_id")
        journal = JournalEntry.objects.filter(pk=journal_id).first() if journal_id else None
        if journal is not None:
            found.append((journal, int((row.metadata or {}).get("amount") or 0)))
    return found


def _declare_window(apps, filing, start, end, rule, *, late):
    JournalLine = apps.get_model("vs_finance", "JournalLine")
    TaxFilingLine = apps.get_model("vs_finance", "TaxFilingLine")
    obligation = filing.obligation
    roles = {obligation.liability_account_id: "PAYABLE"}
    if obligation.recoverable_account_id:
        roles[obligation.recoverable_account_id] = "RECOVERABLE"
    lines = (
        JournalLine.objects.filter(
            entry__entity_id=filing.entity_id, entry__status__in=POSTED_STATUSES,
            account_id__in=list(roles), entry__date__lte=end, tax_declaration__isnull=True,
        )
        .exclude(entry__source="TAX")
        .exclude(entry__reverses__source="TAX")
    )
    if start is not None:
        lines = lines.filter(entry__date__gte=start)
    only, _ = rule
    if filing.branch_id is not None:
        own = models.Q(entry__branch_id=filing.branch_id)
        if only == filing.branch_id:
            own |= models.Q(entry__branch_id__isnull=True)
        lines = lines.filter(own)
    made = [
        TaxFilingLine(
            filing=filing, journal_line_id=line_id, role=roles[account_id],
            branch_id=branch_id or only, is_late=late,
        )
        for line_id, account_id, branch_id in lines.values_list("id", "account_id", "entry__branch_id")
    ]
    TaxFilingLine.objects.bulk_create(made)
    return len(made)


def bring_returns_onto_source_lines(apps, schema_editor):
    Branch = apps.get_model("vs_tenants", "Branch")
    BankAccount = apps.get_model("vs_finance", "BankAccount")
    JournalEntry = apps.get_model("vs_finance", "JournalEntry")
    TaxFiling = apps.get_model("vs_finance", "TaxFiling")
    TaxFilingShare = apps.get_model("vs_finance", "TaxFilingShare")
    TaxRemittance = apps.get_model("vs_finance", "TaxRemittance")

    _seed_tax_codes(apps)

    branch_rules = {}
    first_filed = set()
    filings = (
        TaxFiling.objects.select_related("obligation", "entity")
        .order_by("obligation_id", "period_start", "id")
    )
    for filing in filings:
        rule = _only_branch(Branch, filing.entity.tenant_id, branch_rules)
        if filing.filing_journal_id:
            JournalEntry.objects.filter(pk=filing.filing_journal_id).update(source="TAX")

        share = TaxFilingShare.objects.create(
            filing=filing, branch_id=filing.branch_id or rule[0],
            gross_liability=filing.gross_liability, recoverable_amount=filing.recoverable_amount,
            adjustment_amount=filing.adjustment_amount, amount_due=filing.amount_due,
            amount_paid=filing.amount_paid, payment_status=filing.payment_status,
        )
        for journal, amount in _remittances_from_audit(apps, filing):
            JournalEntry.objects.filter(pk=journal.pk).update(source="TAX")
            credit = journal.lines.filter(credit__gt=0).values_list("account_id", flat=True).first()
            bank = BankAccount.objects.filter(gl_account_id=credit).first() if credit else None
            if bank is None or TaxRemittance.objects.filter(journal=journal).exists():
                continue
            reversal = JournalEntry.objects.filter(reverses=journal).first()
            TaxRemittance.objects.create(
                filing=filing, share=share, branch_id=journal.branch_id, bank_account=bank,
                pay_date=journal.date, amount=amount or journal.lines.aggregate(
                    t=models.Sum("credit"))["t"] or 0,
                journal=journal, reversal_journal=reversal,
                reversed_at=reversal.date if reversal else None,
                created_by_id=journal.created_by_id,
            )

        if filing.filing_status not in FILED_STATUSES:
            continue
        declared = 0
        if filing.obligation_id not in first_filed:
            first_filed.add(filing.obligation_id)
            declared += _declare_window(
                apps, filing, None, filing.period_start - datetime.timedelta(days=1),
                rule, late=True,
            )
        declared += _declare_window(apps, filing, filing.period_start, filing.period_end, rule, late=False)
        share.line_count = declared
        share.save(update_fields=["line_count"])
        TaxFiling.objects.filter(pk=filing.pk).update(declared_line_count=declared)


def restore_sources(apps, schema_editor):
    JournalEntry = apps.get_model("vs_finance", "JournalEntry")
    TaxRemittance = apps.get_model("vs_finance", "TaxRemittance")
    remitted = list(TaxRemittance.objects.values_list("journal_id", flat=True))
    JournalEntry.objects.filter(source="TAX", pk__in=remitted).update(source="BANK")
    JournalEntry.objects.filter(source="TAX").update(source="CLOSING")



class Migration(migrations.Migration):
    dependencies = [
        ("vs_finance", "0034_a_closing_period_per_fiscal_year"),
        ("vs_tenants", "0010_remove_branch__type"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="TaxFilingLine",
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
                    "role",
                    models.CharField(
                        choices=[
                            ("PAYABLE", "Tax payable"),
                            ("RECOVERABLE", "Recoverable input tax"),
                        ],
                        max_length=12,
                    ),
                ),
                ("is_late", models.BooleanField(default=False)),
            ],
            options={
                "ordering": ["filing", "id"],
            },
        ),
        migrations.CreateModel(
            name="TaxFilingShare",
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
                    "branch_pending",
                    models.BooleanField(
                        default=False,
                        help_text="Lines whose entries carry no branch at a tenant with several.",
                    ),
                ),
                (
                    "gross_liability",
                    vs_finance.money.MoneyField(
                        help_text="This branch's net tax on its payable lines, in kobo."
                    ),
                ),
                (
                    "recoverable_amount",
                    vs_finance.money.MoneyField(
                        help_text="This branch's net recoverable input tax, in kobo."
                    ),
                ),
                (
                    "brought_forward_credit",
                    vs_finance.money.MoneyField(
                        help_text="This branch's credit brought forward, in kobo."
                    ),
                ),
                (
                    "adjustment_amount",
                    vs_finance.money.MoneyField(
                        help_text="This branch's part of the penalty, in kobo."
                    ),
                ),
                (
                    "amount_due",
                    vs_finance.money.MoneyField(
                        help_text="What this branch remits, in kobo."
                    ),
                ),
                (
                    "amount_paid",
                    vs_finance.money.MoneyField(
                        help_text="Remitted so far, net of reversals, in kobo."
                    ),
                ),
                (
                    "carried_forward_credit",
                    vs_finance.money.MoneyField(
                        help_text="This branch's credit left for the next return, in kobo."
                    ),
                ),
                (
                    "payment_status",
                    models.CharField(
                        choices=[
                            ("UNPAID", "Unpaid"),
                            ("PARTIAL", "Partially Paid"),
                            ("PAID", "Paid"),
                        ],
                        default="UNPAID",
                        max_length=8,
                    ),
                ),
                ("line_count", models.PositiveIntegerField(default=0)),
            ],
            options={
                "ordering": ["filing", "branch_pending", "branch__name", "id"],
            },
        ),
        migrations.CreateModel(
            name="TaxRemittance",
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
                ("pay_date", models.DateField()),
                (
                    "amount",
                    vs_finance.money.MoneyField(help_text="Amount remitted, in kobo."),
                ),
                ("reversed_at", models.DateField(blank=True, null=True)),
                (
                    "reversal_reason",
                    models.CharField(blank=True, default="", max_length=255),
                ),
            ],
            options={
                "ordering": ["filing", "pay_date", "id"],
            },
        ),
        migrations.AddField(
            model_name="taxcode",
            name="treatment",
            field=models.CharField(
                choices=[
                    ("STANDARD", "Standard rated"),
                    ("ZERO_RATED", "Zero rated"),
                    ("EXEMPT", "Exempt"),
                ],
                default="STANDARD",
                help_text="Standard rated, zero rated or exempt. Only a standard code carries a rate.",
                max_length=12,
            ),
        ),
        migrations.AddField(
            model_name="taxfiling",
            name="brought_forward_credit",
            field=vs_finance.money.MoneyField(
                help_text="Credit carried forward by the previous return and used here, in kobo."
            ),
        ),
        migrations.AddField(
            model_name="taxfiling",
            name="carried_forward_credit",
            field=vs_finance.money.MoneyField(
                help_text="Credit this return leaves for the next one, in kobo."
            ),
        ),
        migrations.AddField(
            model_name="taxfiling",
            name="credit_from",
            field=models.OneToOneField(
                blank=True,
                help_text="The earlier filed return whose carried-forward credit this one uses.",
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="credit_carried_into",
                to="vs_finance.taxfiling",
            ),
        ),
        migrations.AddField(
            model_name="taxfiling",
            name="declared_line_count",
            field=models.PositiveIntegerField(
                default=0,
                help_text="Source lines on the return (claimed once it is filed).",
            ),
        ),
        migrations.AddField(
            model_name="taxfiling",
            name="late_items",
            field=models.JSONField(
                blank=True,
                default=list,
                help_text="Late source lines grouped by the month they are dated in.",
            ),
        ),
        migrations.AddField(
            model_name="taxfiling",
            name="late_line_count",
            field=models.PositiveIntegerField(
                default=0,
                help_text="Source lines dated before period_start, recorded late.",
            ),
        ),
        migrations.AlterField(
            model_name="financeauditlog",
            name="action",
            field=models.CharField(
                choices=[
                    ("JOURNAL_POSTED", "Journal posted"),
                    ("JOURNAL_REVERSED", "Journal reversed"),
                    ("JOURNAL_POST_REJECTED", "Journal posting rejected"),
                    ("INVOICE_POSTED", "Invoice posted"),
                    ("INVOICE_CANCELLED", "Invoice cancelled"),
                    ("INVOICE_REVERSED", "Invoice reversed"),
                    ("INVOICE_WRITTEN_OFF", "Invoice written off (bad debt)"),
                    ("INVOICE_PAY_LINK_REVOKED", "Invoice pay link revoked"),
                    ("PAYMENT_POSTED", "Payment posted"),
                    ("PAYMENT_ALLOCATED", "Payment allocated"),
                    ("PAYMENT_REVERSED", "Payment reversed"),
                    ("CREDIT_NOTE_POSTED", "Credit note posted"),
                    ("CREDIT_NOTE_ALLOCATED", "Credit note allocated"),
                    ("DEBIT_NOTE_POSTED", "Debit note posted"),
                    ("CREDIT_NOTE_REVERSED", "Credit/debit note reversed"),
                    ("REFUND_POSTED", "Customer refund posted"),
                    ("REFUND_REVERSED", "Customer refund reversed"),
                    ("PAYMENT_PLAN_ACTIVATED", "Installment plan activated"),
                    ("PAYMENT_PLAN_COMPLETED", "Installment plan completed"),
                    ("PAYMENT_PLAN_CANCELLED", "Installment plan cancelled"),
                    ("CONCESSION_POSTED", "Concession / discount / waiver posted"),
                    ("CONCESSION_REVERSED", "Concession / discount / waiver reversed"),
                    ("DUNNING_RUN_GENERATED", "Dunning run generated"),
                    ("DUNNING_NOTICE_SENT", "Dunning notice marked sent"),
                    ("DUNNING_NOTICE_CANCELLED", "Dunning notice cancelled"),
                    ("PERIOD_CLOSED", "Period closed"),
                    ("PERIOD_REOPENED", "Period re-opened"),
                    ("ACCOUNT_CREATED", "Account created"),
                    ("ACCOUNT_UPDATED", "Account updated"),
                    ("FINANCE_SETTINGS_UPDATED", "Finance settings updated"),
                    (
                        "FIN_DOCUMENT_SETTINGS_UPDATED",
                        "Finance document settings updated",
                    ),
                    ("FIN_BANK_SETTINGS_UPDATED", "Finance banking settings updated"),
                    (
                        "FIN_CALENDAR_SETTINGS_UPDATED",
                        "Finance calendar settings updated",
                    ),
                    ("PROCUREMENT_SETTINGS_UPDATED", "Procurement settings updated"),
                    ("REQUISITION_APPROVED", "Requisition approved"),
                    (
                        "SPEND_APPROVAL_OVERRIDDEN",
                        "Spend approval released by override (no review)",
                    ),
                    ("RFQ_ISSUED", "Request for quotation issued"),
                    ("RFQ_CANCELLED", "Request for quotation cancelled"),
                    ("RFQ_CLOSED", "Request for quotation closed without award"),
                    ("QUOTATION_SUBMITTED", "Vendor quotation submitted"),
                    ("QUOTATION_AWARDED", "Vendor quotation awarded → PO"),
                    ("QUOTATION_REJECTED", "Vendor quotation rejected"),
                    ("VENDOR_CONTRACT_ACTIVATED", "Vendor contract activated"),
                    ("VENDOR_CONTRACT_RENEWED", "Vendor contract renewed"),
                    ("VENDOR_CONTRACT_TERMINATED", "Vendor contract terminated"),
                    ("CONTRACT_MILESTONE_COMPLETED", "Contract milestone completed"),
                    ("PURCHASE_ORDER_APPROVED", "Purchase order approved"),
                    ("PURCHASE_ORDER_CANCELLED", "Purchase order cancelled"),
                    ("PO_EMAIL_SCHEDULED", "Purchase order email scheduled"),
                    ("PO_EMAIL_QUEUED", "Purchase order email queued"),
                    ("PO_EMAIL_SENT", "Purchase order email sent"),
                    ("PO_EMAIL_FAILED", "Purchase order email failed"),
                    ("PO_EMAIL_CANCELLED", "Purchase order email cancelled"),
                    ("DOC_EMAIL_QUEUED", "Customer document email queued"),
                    ("DOC_EMAIL_SENT", "Customer document email sent"),
                    ("DOC_EMAIL_FAILED", "Customer document email failed"),
                    ("GRN_POSTED", "Goods receipt posted"),
                    ("GRN_POST_REJECTED", "Goods receipt posting rejected"),
                    ("VENDOR_INVOICE_MATCHED", "Vendor invoice matched"),
                    ("VENDOR_INVOICE_APPROVED", "Vendor invoice approved (workflow)"),
                    ("VENDOR_INVOICE_POSTED", "Vendor invoice posted"),
                    ("VENDOR_INVOICE_POST_REJECTED", "Vendor invoice posting rejected"),
                    ("VENDOR_PAYMENT_POSTED", "Vendor payment posted"),
                    ("VENDOR_PAYMENT_POST_REJECTED", "Vendor payment posting rejected"),
                    ("VENDOR_PAYMENT_ALLOCATED", "Vendor payment allocated"),
                    ("STOCK_RECEIVED", "Stock received (perpetual inventory)"),
                    ("STOCK_ISSUED", "Stock issued"),
                    ("STOCK_ISSUE_REJECTED", "Stock issue rejected"),
                    ("STOCK_ADJUSTED", "Stock adjusted"),
                    ("STOCK_ADJUST_REJECTED", "Stock adjustment rejected"),
                    ("BANK_STATEMENT_CORRECTED", "Bank statement corrected"),
                    ("BANK_RECONCILED", "Bank statement reconciled"),
                    ("BANK_CHARGE_POSTED", "Bank charge posted"),
                    ("EXPENSE_CLAIM_POSTED", "Expense claim posted"),
                    ("EXPENSE_CLAIM_POST_REJECTED", "Expense claim posting rejected"),
                    ("EXPENSE_CLAIM_SETTLED", "Expense claim settled"),
                    ("EXPENSE_CLAIM_VOIDED", "Expense claim voided"),
                    (
                        "PETTY_CASH_ESTABLISHED",
                        "Petty cash fund established / topped up",
                    ),
                    ("PETTY_CASH_VOUCHER_POSTED", "Petty cash voucher posted"),
                    ("PETTY_CASH_VOUCHER_REJECTED", "Petty cash voucher rejected"),
                    ("PETTY_CASH_VOUCHER_VOIDED", "Petty cash voucher voided"),
                    ("PETTY_CASH_REPLENISHED", "Petty cash fund replenished"),
                    ("PAYROLL_POSTED", "Payroll run posted"),
                    ("PAYROLL_POST_REJECTED", "Payroll run posting rejected"),
                    ("PAYROLL_PAID", "Payroll run disbursed"),
                    ("PAYROLL_CANCELLED", "Payroll run cancelled / voided"),
                    ("BUDGET_APPROVED", "Budget approved"),
                    ("BUDGET_DELETED", "Budget deleted"),
                    ("ASSET_ACQUIRED", "Fixed asset acquired"),
                    ("DEPRECIATION_POSTED", "Depreciation posted"),
                    ("ASSET_DISPOSED", "Fixed asset disposed"),
                    ("PERIOD_LOCKED", "Period locked"),
                    ("FISCAL_YEAR_OPENED", "Fiscal year opened"),
                    ("FISCAL_YEAR_CLOSED", "Fiscal year closed"),
                    ("FISCAL_YEAR_REOPENED", "Fiscal year re-opened"),
                    (
                        "FISCAL_CALENDAR_WARNED",
                        "Finance staff warned the fiscal calendar is running out",
                    ),
                    ("TAX_FILING_PREPARED", "Tax filing prepared"),
                    ("TAX_FILING_FILED", "Tax filing submitted to authority"),
                    ("TAX_FILING_UNFILED", "Tax filing un-filed (reverted to draft)"),
                    ("TAX_FILING_PAID", "Tax filing paid / remitted"),
                    ("TAX_FILING_REJECTED", "Tax filing action rejected"),
                    ("TAX_REMITTANCE_REVERSED", "Tax remittance reversed"),
                ],
                max_length=32,
            ),
        ),
        migrations.AlterField(
            model_name="journalentry",
            name="source",
            field=models.CharField(
                choices=[
                    ("MANUAL", "Manual"),
                    ("SALES", "Sales / AR"),
                    ("PURCHASE", "Purchase / AP"),
                    ("BANK", "Bank / Cash"),
                    ("PAYROLL", "Payroll"),
                    ("CLOSING", "Period Close"),
                    ("OPENING", "Opening Balance"),
                    ("FX", "FX Revaluation"),
                    ("SYSTEM", "System"),
                    ("TAX", "Tax return"),
                ],
                default="MANUAL",
                max_length=12,
            ),
        ),
        migrations.AlterField(
            model_name="taxfiling",
            name="amount_due",
            field=vs_finance.money.MoneyField(
                help_text="max(gross − recoverable − brought forward, 0) + adjustment, in kobo."
            ),
        ),
        migrations.AlterField(
            model_name="taxfiling",
            name="amount_paid",
            field=vs_finance.money.MoneyField(
                help_text="Remitted so far, net of reversed remittances, in kobo."
            ),
        ),
        migrations.AlterField(
            model_name="taxfiling",
            name="filing_journal",
            field=models.ForeignKey(
                blank=True,
                help_text="The single netting/penalty journal of a return filed before returns were split by branch; a branch share holds its own journal.",
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="tax_filing_postings",
                to="vs_finance.journalentry",
            ),
        ),
        migrations.AlterField(
            model_name="taxfiling",
            name="gross_liability",
            field=vs_finance.money.MoneyField(
                help_text="Net tax on the declared payable lines, in kobo."
            ),
        ),
        migrations.AlterField(
            model_name="taxfiling",
            name="recoverable_amount",
            field=vs_finance.money.MoneyField(
                help_text="Net input tax on the declared recoverable lines, in kobo."
            ),
        ),
        migrations.AddConstraint(
            model_name="taxcode",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    ("treatment", "STANDARD"), ("rate_bps", 0), _connector="OR"
                ),
                name="ck_finance_taxcode_rate_only_when_standard",
            ),
        ),
        migrations.AddField(
            model_name="taxfilingline",
            name="branch",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="finance_tax_filing_lines",
                to="vs_tenants.branch",
            ),
        ),
        migrations.AddField(
            model_name="taxfilingline",
            name="filing",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.CASCADE,
                related_name="declared_lines",
                to="vs_finance.taxfiling",
            ),
        ),
        migrations.AddField(
            model_name="taxfilingline",
            name="journal_line",
            field=models.OneToOneField(
                on_delete=django.db.models.deletion.PROTECT,
                related_name="tax_declaration",
                to="vs_finance.journalline",
            ),
        ),
        migrations.AddField(
            model_name="taxfilingshare",
            name="branch",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="finance_tax_filing_shares",
                to="vs_tenants.branch",
            ),
        ),
        migrations.AddField(
            model_name="taxfilingshare",
            name="filing",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.CASCADE,
                related_name="shares",
                to="vs_finance.taxfiling",
            ),
        ),
        migrations.AddField(
            model_name="taxfilingshare",
            name="filing_journal",
            field=models.OneToOneField(
                blank=True,
                help_text="The netting/penalty journal posted for this branch at filing.",
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="tax_filing_share",
                to="vs_finance.journalentry",
            ),
        ),
        migrations.AddField(
            model_name="taxremittance",
            name="bank_account",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.PROTECT,
                related_name="tax_remittances",
                to="vs_finance.bankaccount",
            ),
        ),
        migrations.AddField(
            model_name="taxremittance",
            name="branch",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="finance_tax_remittances",
                to="vs_tenants.branch",
            ),
        ),
        migrations.AddField(
            model_name="taxremittance",
            name="created_by",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="finance_tax_remittances",
                to=settings.AUTH_USER_MODEL,
            ),
        ),
        migrations.AddField(
            model_name="taxremittance",
            name="filing",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.PROTECT,
                related_name="remittances",
                to="vs_finance.taxfiling",
            ),
        ),
        migrations.AddField(
            model_name="taxremittance",
            name="journal",
            field=models.OneToOneField(
                on_delete=django.db.models.deletion.PROTECT,
                related_name="tax_remittance",
                to="vs_finance.journalentry",
            ),
        ),
        migrations.AddField(
            model_name="taxremittance",
            name="reversal_journal",
            field=models.OneToOneField(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="tax_remittance_reversal",
                to="vs_finance.journalentry",
            ),
        ),
        migrations.AddField(
            model_name="taxremittance",
            name="share",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.PROTECT,
                related_name="remittances",
                to="vs_finance.taxfilingshare",
            ),
        ),
        migrations.AddIndex(
            model_name="taxfilingline",
            index=models.Index(
                fields=["filing"], name="vs_finance__filing__6f9330_idx"
            ),
        ),
        migrations.AddIndex(
            model_name="taxfilingshare",
            index=models.Index(
                fields=["filing"], name="vs_finance__filing__5010f2_idx"
            ),
        ),
        migrations.AddIndex(
            model_name="taxremittance",
            index=models.Index(
                fields=["filing"], name="vs_finance__filing__625083_idx"
            ),
        ),
        migrations.RunPython(bring_returns_onto_source_lines, restore_sources),
        migrations.RunPython(exempt_blank_fee_items, migrations.RunPython.noop),
    ]

"""Sealed figures of closed periods and years, archived years, and new audit actions.

* ``LedgerSeal`` holds each closed or locked period's and each closed year's
  balances and line checksum (:mod:`vs_finance.seals`). Its rows are proof, so
  BEFORE UPDATE and BEFORE DELETE triggers refuse every write after the insert.
* ``FiscalYear.archived_at`` / ``archived_by`` mark a closed year put away
  (:mod:`vs_finance.archive`). Nullable, so every existing year reads as not
  archived and nothing is backfilled.
* ``FinanceAuditLog.action`` gains the archive, evidence and retention
  actions; a choices change, with no effect on the table.

The triggers are PostgreSQL only, which is what every environment runs.
Reversible: the reverse drops the triggers, then the columns and the table.
"""

import django.db.models.deletion
import django.utils.timezone
from django.conf import settings
from django.db import migrations, models

TABLE = "vs_finance_ledgerseal"

PG_FORWARD = f"""
CREATE OR REPLACE FUNCTION vs_finance_ledgerseal_block() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION 'LedgerSeal rows are append-only and cannot be updated or deleted.';
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS vs_finance_ledgerseal_no_update ON {TABLE};
CREATE TRIGGER vs_finance_ledgerseal_no_update
    BEFORE UPDATE ON {TABLE}
    FOR EACH ROW EXECUTE FUNCTION vs_finance_ledgerseal_block();

DROP TRIGGER IF EXISTS vs_finance_ledgerseal_no_delete ON {TABLE};
CREATE TRIGGER vs_finance_ledgerseal_no_delete
    BEFORE DELETE ON {TABLE}
    FOR EACH ROW EXECUTE FUNCTION vs_finance_ledgerseal_block();
"""

PG_REVERSE = f"""
DROP TRIGGER IF EXISTS vs_finance_ledgerseal_no_update ON {TABLE};
DROP TRIGGER IF EXISTS vs_finance_ledgerseal_no_delete ON {TABLE};
DROP FUNCTION IF EXISTS vs_finance_ledgerseal_block();
"""


def install_triggers(apps, schema_editor):
    if schema_editor.connection.vendor == "postgresql":
        schema_editor.execute(PG_FORWARD)


def drop_triggers(apps, schema_editor):
    if schema_editor.connection.vendor == "postgresql":
        schema_editor.execute(PG_REVERSE)


class Migration(migrations.Migration):
    dependencies = [
        ("vs_finance", "0059_pay_brought_forward"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.AddField(
            model_name="fiscalyear",
            name="archived_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="fiscalyear",
            name="archived_by",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="finance_fiscal_years_archived",
                to=settings.AUTH_USER_MODEL,
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
                    ("CUSTOMER_UPDATED", "Customer updated"),
                    ("CUSTOMER_DEACTIVATED", "Customer deactivated"),
                    ("CUSTOMER_REACTIVATED", "Customer reactivated"),
                    ("CUSTOMER_OPENING_POSTED", "Customer opening invoice posted"),
                    ("CREDIT_TRANSFER_POSTED", "Customer credit transfer posted"),
                    ("CREDIT_TRANSFER_REVERSED", "Customer credit transfer reversed"),
                    ("DEFERRED_INCOME_RELEASED", "Deferred income released"),
                    ("DEFERRED_RELEASE_REVERSED", "Deferred income release reversed"),
                    ("PROVISION_POSTED", "Doubtful-debt provision posted"),
                    ("WRITE_OFF_RECOVERED", "Written-off debt recovered"),
                    ("DEPOSIT_RELEASED", "Customer deposit released"),
                    ("DEPOSITS_FORFEITED", "Unclaimed deposits forfeited"),
                    ("RECEIPT_PARKED_AS_CREDIT", "Receipt parked as customer credit"),
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
                    (
                        "FIN_RECEIVABLES_SETTINGS_UPDATED",
                        "Finance receivables settings updated",
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
                    ("VENDOR_INVOICE_VOIDED", "Vendor invoice voided"),
                    (
                        "VENDOR_CREDIT_NOTE_APPROVED",
                        "Vendor credit note approved (workflow)",
                    ),
                    ("VENDOR_CREDIT_NOTE_POSTED", "Vendor credit note posted"),
                    ("VENDOR_CREDIT_NOTE_ALLOCATED", "Vendor credit note allocated"),
                    ("VENDOR_CREDIT_NOTE_VOIDED", "Vendor credit note voided"),
                    ("GOODS_RETURNED", "Goods returned to vendor"),
                    ("VENDOR_OPENING_BILL_POSTED", "Vendor opening bill posted"),
                    ("STOCK_RECEIVED", "Stock received (perpetual inventory)"),
                    ("STOCK_ISSUED", "Stock issued"),
                    ("STOCK_ISSUE_REJECTED", "Stock issue rejected"),
                    ("STOCK_ADJUSTED", "Stock adjusted"),
                    ("STOCK_ADJUST_REJECTED", "Stock adjustment rejected"),
                    ("BANK_STATEMENT_CORRECTED", "Bank statement corrected"),
                    ("BANK_RECONCILED", "Bank statement reconciled"),
                    ("BANK_CHARGE_POSTED", "Bank charge posted"),
                    ("BANK_TRANSACTION_POSTED", "Bank transaction posted"),
                    ("BANK_TRANSACTION_VOIDED", "Bank transaction voided"),
                    ("BANK_TRANSFER_POSTED", "Transfer between own accounts posted"),
                    ("BANK_TRANSFER_VOIDED", "Transfer between own accounts voided"),
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
                    (
                        "FIN_PAYROLL_SETTINGS_UPDATED",
                        "Finance payroll settings updated",
                    ),
                    ("SALARY_CREATED", "Employee salary added"),
                    ("SALARY_CHANGED", "Employee salary changed"),
                    ("SALARY_DEACTIVATED", "Employee salary deactivated"),
                    ("PAYE_OVERRIDE_CHANGED", "PAYE override set or cleared"),
                    ("SALARY_STRUCTURE_CHANGED", "Salary structure changed"),
                    ("PAYROLL_DEDUCTION_CHANGED", "Payroll deduction changed"),
                    ("PAYSLIPS_ISSUED", "Payslips issued"),
                    ("INTER_BRANCH_REQUESTED", "Inter-branch transfer requested"),
                    ("INTER_BRANCH_SENT", "Inter-branch transfer sent"),
                    ("INTER_BRANCH_DECLINED", "Inter-branch transfer request declined"),
                    (
                        "INTER_BRANCH_CONFIRMED",
                        "Inter-branch transfer arrival confirmed",
                    ),
                    ("INTER_BRANCH_VOIDED", "Inter-branch transfer voided"),
                    ("HELD_RECEIPT_POSTED", "Receipt held for another branch"),
                    ("HELD_RECEIPT_VOIDED", "Receipt held for another branch voided"),
                    ("RECHARGE_POSTED", "Shared cost recharged to other branches"),
                    ("RECHARGE_VOIDED", "Shared cost recharge voided"),
                    (
                        "RECEIVABLE_TRANSFERRED",
                        "Open receivable moved to another branch",
                    ),
                    ("INCOME_GIVEN_BACK", "Income held at another branch given back"),
                    ("SHARED_COST_RULE_CHANGED", "Shared cost rule changed"),
                    ("STOCK_TRANSFERRED", "Stock moved between stores"),
                    ("PETTY_CASH_RETURN_POSTED", "Petty cash returned to the bank"),
                    ("PETTY_CASH_RETURN_VOIDED", "Petty cash return voided"),
                    ("PETTY_CASH_FUND_CLOSED", "Petty cash fund closed"),
                    ("PETTY_CASH_FUND_REOPENED", "Petty cash fund reopened"),
                    ("PETTY_CASH_FUND_UPDATED", "Petty cash fund details changed"),
                    (
                        "PETTY_CASH_VOUCHER_CANCELLED",
                        "Draft petty cash voucher cancelled",
                    ),
                    (
                        "PAYER_PAYMENT_POSTED",
                        "Payment split across a payer's customers",
                    ),
                    ("PAYER_PAYMENT_VOIDED", "Payer payment voided"),
                    ("PAYER_LINK_CHANGED", "Customers a payer pays for changed"),
                    ("FISCAL_YEAR_ARCHIVED", "Fiscal year archived"),
                    ("FISCAL_YEAR_UNARCHIVED", "Fiscal year unarchived"),
                    ("ATTACHMENT_ADDED", "Evidence file attached"),
                    ("ATTACHMENT_SUPERSEDED", "Evidence file superseded"),
                    ("ATTACHMENT_REMOVED", "Evidence file removed from a draft"),
                    ("RETENTION_SETTINGS_UPDATED", "Record retention settings updated"),
                ],
                max_length=32,
            ),
        ),
        migrations.CreateModel(
            name="LedgerSeal",
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
                    "kind",
                    models.CharField(
                        choices=[
                            ("PERIOD_CLOSED", "Period closed"),
                            ("PERIOD_LOCKED", "Period locked"),
                            ("YEAR_CLOSED", "Fiscal year closed"),
                        ],
                        max_length=16,
                    ),
                ),
                (
                    "sealed_at",
                    models.DateTimeField(
                        default=django.utils.timezone.now, editable=False
                    ),
                ),
                ("line_count", models.PositiveIntegerField(default=0)),
                ("lines_checksum", models.CharField(max_length=64)),
                ("balances", models.JSONField(default=dict)),
                ("seal_checksum", models.CharField(max_length=64)),
                (
                    "entity",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="ledger_seals",
                        to="vs_finance.ledgerentity",
                    ),
                ),
                (
                    "fiscal_year",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="ledger_seals",
                        to="vs_finance.fiscalyear",
                    ),
                ),
                (
                    "period",
                    models.ForeignKey(
                        blank=True,
                        help_text="Blank for a seal of the whole fiscal year.",
                        null=True,
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="ledger_seals",
                        to="vs_finance.fiscalperiod",
                    ),
                ),
                (
                    "previous",
                    models.OneToOneField(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="next_seal",
                        to="vs_finance.ledgerseal",
                    ),
                ),
                (
                    "sealed_by",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="finance_ledger_seals",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
            ],
            options={
                "ordering": ["-id"],
                "indexes": [
                    models.Index(
                        fields=["entity", "period"], name="vs_finance_seal_period_idx"
                    ),
                    models.Index(
                        fields=["entity", "fiscal_year"],
                        name="vs_finance_seal_year_idx",
                    ),
                ],
            },
        ),
        migrations.RunPython(install_triggers, drop_triggers),
    ]

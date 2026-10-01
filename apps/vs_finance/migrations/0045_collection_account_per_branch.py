"""Each branch names its own collection account, and a bank account can carry a provider subaccount.

The one-per-entity rule on ``is_primary_collection`` becomes one per branch (and one
among the accounts not yet given a branch). A set of books holds at most one flagged
account under the old rule, so the account flagged today keeps its flag, now as its
own branch's collection account, and nothing is unflagged.
"""

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("vs_finance", "0044_receivables_accruals_data"),
        ("vs_tenants", "0010_remove_branch__type"),
    ]

    operations = [
        migrations.RemoveConstraint(
            model_name="bankaccount",
            name="uniq_finance_primary_collection_per_entity",
        ),
        migrations.AddField(
            model_name="bankaccount",
            name="gateway_subaccount_code",
            field=models.CharField(
                blank=True,
                default="",
                help_text="The payment provider's subaccount that settles online payments straight into this account.",
                max_length=64,
            ),
        ),
        migrations.AddField(
            model_name="bankaccount",
            name="gateway_subaccount_provider",
            field=models.CharField(
                blank=True,
                default="",
                help_text="The payment provider that issued the subaccount.",
                max_length=16,
            ),
        ),
        migrations.AlterField(
            model_name="bankaccount",
            name="is_primary_collection",
            field=models.BooleanField(
                default=False,
                help_text="The branch's fee-collection account - the one printed as 'pay to' on its customer invoices/receipts. At most one per branch.",
            ),
        ),
        migrations.AlterField(
            model_name="financeaccountmapping",
            name="key",
            field=models.CharField(
                choices=[
                    ("CASH_BANK", "Cash and bank"),
                    ("ACCOUNTS_RECEIVABLE", "Accounts receivable"),
                    ("ACCOUNTS_PAYABLE", "Accounts payable"),
                    ("CUSTOMER_CREDIT", "Customer credit"),
                    ("VENDOR_ADVANCE", "Vendor advances"),
                    ("GRIR_CLEARING", "GR/IR clearing"),
                    ("OUTPUT_VAT", "Output VAT"),
                    ("WHT_PAYABLE", "WHT payable"),
                    ("RETAINED_EARNINGS", "Retained earnings"),
                    ("BAD_DEBT_EXPENSE", "Bad debt expense"),
                    ("BANK_CHARGES", "Bank charges"),
                    ("INVENTORY_ASSET", "Inventory asset"),
                    ("INVENTORY_ADJUSTMENT", "Inventory adjustment"),
                    ("PURCHASE_PRICE_VARIANCE", "Purchase price variance"),
                    ("DEFERRED_INCOME", "Deferred income"),
                    ("DEPOSITS_HELD", "Customer deposits held"),
                    ("DOUBTFUL_DEBT_ALLOWANCE", "Allowance for doubtful debts"),
                    ("BAD_DEBT_RECOVERED", "Bad debts recovered"),
                    ("FORFEITED_DEPOSIT_INCOME", "Forfeited deposits income"),
                    ("GATEWAY_CLEARING", "Gateway clearing"),
                ],
                max_length=32,
            ),
        ),
        migrations.AddConstraint(
            model_name="bankaccount",
            constraint=models.UniqueConstraint(
                condition=models.Q(
                    ("branch__isnull", False), ("is_primary_collection", True)
                ),
                fields=("entity", "branch"),
                name="uniq_finance_primary_collection_per_branch",
            ),
        ),
        migrations.AddConstraint(
            model_name="bankaccount",
            constraint=models.UniqueConstraint(
                condition=models.Q(
                    ("branch__isnull", True), ("is_primary_collection", True)
                ),
                fields=("entity",),
                name="uniq_finance_primary_collection_unbranched",
            ),
        ),
    ]

"""The database refuses to delete a finance document once it has left draft.

``core.retention`` already refuses every ORM delete of a kept document
(:mod:`vs_finance.retention`: kept once it leaves draft). SQL that bypasses
the ORM, a mistaken ``DELETE`` in a console or a raw query in code, met
nothing. These triggers close that at the database:

* every finance document table refuses ``DELETE`` of a row whose status is
  not ``DRAFT``;
* a ``POSTED`` or ``REVERSED`` row may not move back to ``DRAFT``,
  ``PENDING_APPROVAL`` or ``APPROVED``, which would otherwise be the way round
  the first rule;
* journal lines and invoice lines refuse ``DELETE`` while their document is
  past draft.

The database does not know the retention period, so a document stays
undeletable here after its period ends; removing expired books is a deliberate
operation that will need its own switch. Procurement's document tables get the
same triggers in ``vs_procurement`` 0049. TRUNCATE (the test runner's flush) is
not a DELETE and is unaffected. PostgreSQL only, which every environment runs.
"""

from django.db import migrations

from ._kept_documents import (
    FUNCTIONS,
    document_triggers,
    drop_document_triggers,
    line_triggers,
)

#: This app's FinanceDocument tables. ``vs_finance.tests_kept_documents`` fails
#: when a new document table has no trigger.
TABLES = (
    "vs_finance_banktransaction",
    "vs_finance_banktransfer",
    "vs_finance_concession",
    "vs_finance_creditnote",
    "vs_finance_customercredittransfer",
    "vs_finance_doubtfuldebtprovision",
    "vs_finance_dunningnotice",
    "vs_finance_expenseclaim",
    "vs_finance_fixedasset",
    "vs_finance_heldforbranchreceipt",
    "vs_finance_interbranchrecharge",
    "vs_finance_interbranchtransfer",
    "vs_finance_invoice",
    "vs_finance_journalentry",
    "vs_finance_payerpayment",
    "vs_finance_payment",
    "vs_finance_paymentplan",
    "vs_finance_payrollrun",
    "vs_finance_pettycashreturn",
    "vs_finance_pettycashvoucher",
    "vs_finance_refund",
    "vs_finance_taxfiling",
    "vs_finance_writeoffrequest",
)

#: (line table, its document table, the column naming the document)
LINES = (
    ("vs_finance_journalline", "vs_finance_journalentry", "entry_id"),
    ("vs_finance_invoiceline", "vs_finance_invoice", "invoice_id"),
)

def install(apps, schema_editor):
    if schema_editor.connection.vendor != "postgresql":
        return
    schema_editor.execute(FUNCTIONS, params=None)
    for table in TABLES:
        schema_editor.execute(document_triggers(table), params=None)
    for line in LINES:
        schema_editor.execute(line_triggers(*line), params=None)


def uninstall(apps, schema_editor):
    if schema_editor.connection.vendor != "postgresql":
        return
    for table in TABLES:
        schema_editor.execute(drop_document_triggers(table), params=None)
    for table, _, _ in LINES:
        schema_editor.execute(f"DROP TRIGGER IF EXISTS {table}_kept_no_delete ON {table};")
    schema_editor.execute("""
DROP FUNCTION IF EXISTS xvs_kept_document_no_delete();
DROP FUNCTION IF EXISTS xvs_kept_document_no_unpost();
DROP FUNCTION IF EXISTS xvs_kept_line_no_delete();
""")


class Migration(migrations.Migration):
    dependencies = [
        ("vs_finance", "0077_account_purposes"),
    ]

    operations = [migrations.RunPython(install, uninstall)]

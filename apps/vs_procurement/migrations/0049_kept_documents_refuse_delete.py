"""Procurement's document tables refuse to delete a row once it has left draft.

The same triggers ``vs_finance`` 0078 puts on the finance documents, using the
functions it creates: a non-draft row cannot be deleted, and a ``POSTED`` or
``REVERSED`` row cannot move back to draft, pending or approved.
"""

from django.db import migrations

from vs_finance.migrations import _kept_documents as kept

TABLES = (
    "vs_procurement_goodsreceivednote",
    "vs_procurement_goodsreturn",
    "vs_procurement_purchaseorder",
    "vs_procurement_purchaserequisition",
    "vs_procurement_requestforquotation",
    "vs_procurement_vendorcreditnote",
    "vs_procurement_vendorinvoice",
    "vs_procurement_vendorpayment",
    "vs_procurement_vendorquotation",
)


def install(apps, schema_editor):
    if schema_editor.connection.vendor != "postgresql":
        return
    for table in TABLES:
        schema_editor.execute(kept.document_triggers(table), params=None)


def uninstall(apps, schema_editor):
    if schema_editor.connection.vendor != "postgresql":
        return
    for table in TABLES:
        schema_editor.execute(kept.drop_document_triggers(table), params=None)


class Migration(migrations.Migration):
    dependencies = [
        ("vs_procurement", "0048_requisition_submit_is_not_restricted"),
        ("vs_finance", "0078_kept_documents_refuse_delete"),
    ]

    operations = [migrations.RunPython(install, uninstall)]

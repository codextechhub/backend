"""Stamp every fee bill already raised with the billing period it was raised for.

A fee run now stamps each invoice with its period (``Invoice.billing_period`` and
``billing_period_label``), fixed once the invoice posts, and every period report
reads the stamp. Bills raised before that carry no stamp, so they are given one
here, once, from how their fee structure is linked today: the only record of their
period that exists. Their billing key gains the period too (``FEE:<code>@<period>``),
matching what a run writes, so a run for the same period still finds them and a
run for a later period does not mistake them for its own.

Only bills with no stamp yet are touched, so re-running changes nothing. The
reverse clears what this wrote.
"""
from django.db import migrations


def _period_key(session_id, term_id):
    return f"S{session_id}" if term_id is None else f"S{session_id}-T{term_id}"


def _label(link):
    session = link.session
    return session.name if link.term_id is None else f"{link.term.name} {session.name}"


def forwards(apps, schema_editor):
    Invoice = apps.get_model("vs_finance", "Invoice")
    FeeStructureTermLink = apps.get_model("fal", "FeeStructureTermLink")

    for link in FeeStructureTermLink.objects.select_related(
            "fee_structure", "session", "term").iterator():
        reference = f"FEE:{link.fee_structure.code}"
        key = _period_key(link.session_id, link.term_id)
        bills = Invoice.objects.filter(
            entity_id=link.fee_structure.entity_id, reference=reference, billing_period="",
        )
        bills.filter(billing_key=reference).update(billing_key=f"{reference}@{key}")
        bills.update(billing_period=key, billing_period_label=_label(link)[:128])


def backwards(apps, schema_editor):
    Invoice = apps.get_model("vs_finance", "Invoice")

    for invoice in Invoice.objects.filter(reference__startswith="FEE:").exclude(
            billing_period="").iterator():
        if invoice.billing_key == f"{invoice.reference}@{invoice.billing_period}":
            invoice.billing_key = invoice.reference
        invoice.billing_period = ""
        invoice.billing_period_label = ""
        invoice.save(update_fields=["billing_key", "billing_period", "billing_period_label"])


class Migration(migrations.Migration):

    dependencies = [
        ("fal", "0002_schoolfeeduepolicy"),
        ("vs_finance", "0041_ar_guards_data"),
    ]

    operations = [
        migrations.RunPython(forwards, backwards),
    ]

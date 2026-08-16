"""Bring the invoice and receipt emails in line with what they now actually send.

Two things changed under these templates:

* vs_finance attaches a PDF copy of the document, which the body never mentioned.
* The same events now carry every invoice and receipt the finance console sends -
  a school billing a guardian, and the platform billing a school - so copy that
  opened "Dear Parent/Guardian" and spoke of "your child's school fees" was wrong
  for a growing share of the mail going out.

Only rows still holding the exact shipped default are rewritten. A template someone
has edited is left alone: their wording is a deliberate choice, and the seeder's
``--overwrite`` (which would discard it) is far too blunt to run for a copy change.
Templates flagged ``html_is_custom`` are skipped outright.

Reversible: the reverse operation restores the previous default to any row still
holding the new one.
"""
from django.db import migrations

OLD_INVOICE_SUBJECT = "New fee invoice - {{ customer_name }}"
OLD_INVOICE_BODY = (
    "Dear Parent/Guardian,\n\n"
    "A new invoice has been issued for your child's school fees.\n\n"
    "Bill to: {{ customer_name }}\n"
    "Invoice number: {{ invoice_number }}\n"
    "Amount due: ₦{{ invoice_amount }}\n"
    "Due date: {{ due_date }}\n\n"
    "Pay online: {{ payment_link }}\n\n"
    "{{ school_name }} via CodeX Vision"
)
NEW_INVOICE_SUBJECT = "Invoice {{ invoice_number }} from {{ issuer_name }}"
NEW_INVOICE_BODY = (
    "Hello {{ customer_name }},\n\n"
    "An invoice has been issued to your account. A PDF copy is attached.\n\n"
    "Invoice number: {{ invoice_number }}\n"
    "Amount: ₦{{ invoice_amount }}\n"
    "Due date: {{ due_date }}\n\n"
    "{{ note }}\n\n"
    "Pay online: {{ payment_link }}\n\n"
    "{{ issuer_name }} via CodeX Vision"
)

OLD_RECEIPT_SUBJECT = "Payment confirmed - {{ customer_name }}"
OLD_RECEIPT_BODY = (
    "Dear Parent/Guardian,\n\n"
    "We have received your payment. Thank you.\n\n"
    "Bill to: {{ customer_name }}\n"
    "Invoice number: {{ invoice_number }}\n"
    "Amount paid: ₦{{ amount_paid }}\n"
    "Payment date: {{ payment_date }}\n"
    "Receipt number: {{ receipt_number }}\n\n"
    "{{ school_name }} via CodeX Vision"
)
NEW_RECEIPT_SUBJECT = "Receipt {{ receipt_number }} from {{ issuer_name }}"
NEW_RECEIPT_BODY = (
    "Hello {{ customer_name }},\n\n"
    "We have received your payment. Thank you. A PDF receipt is attached.\n\n"
    "Receipt number: {{ receipt_number }}\n"
    "Amount paid: ₦{{ amount_paid }}\n"
    "Payment date: {{ payment_date }}\n"
    "Applied to invoice: {{ invoice_number }}\n\n"
    "{{ note }}\n\n"
    "{{ issuer_name }} via CodeX Vision"
)

CHANGES = [
    ("billing.invoice_issued", OLD_INVOICE_SUBJECT, OLD_INVOICE_BODY, NEW_INVOICE_SUBJECT, NEW_INVOICE_BODY),
    ("billing.payment_received", OLD_RECEIPT_SUBJECT, OLD_RECEIPT_BODY, NEW_RECEIPT_SUBJECT, NEW_RECEIPT_BODY),
]


def _normalise(value: str) -> str:
    """Compare ignoring dash style.

    Seeded rows predate the house rule against em dashes, so a template nobody has
    touched can still read "New fee invoice — ..." while the shipped default in
    source now reads "- ". Comparing the raw strings finds nothing and the migration
    silently does nothing, which is how this was nearly missed. Only the dash is
    normalised; every other character still has to match exactly, so a genuinely
    edited template is still left alone.
    """
    return (value or "").replace("—", "-").replace("–", "-")


def _apply(apps, changes):
    Template = apps.get_model("vs_notifications", "NotificationTemplate")
    updated = 0
    for event_key, from_subject, from_body, to_subject, to_body in changes:
        rows = Template.objects.filter(
            event_type__key=event_key, channel="email", html_is_custom=False,
        )
        for row in rows:
            if _normalise(row.subject) != _normalise(from_subject):
                continue
            if _normalise(row.body) != _normalise(from_body):
                continue
            row.subject = to_subject
            row.body = to_body
            # Leave html_body to the model's own save(), which regenerates the
            # standard layout from the new body whenever html_is_custom is False.
            row.save()
            updated += 1
    return updated


def forwards(apps, schema_editor):
    _apply(apps, CHANGES)


def backwards(apps, schema_editor):
    _apply(apps, [
        (key, new_subject, new_body, old_subject, old_body)
        for key, old_subject, old_body, new_subject, new_body in CHANGES
    ])


class Migration(migrations.Migration):

    dependencies = [
        ("vs_notifications", "0006_store_email_html_in_the_database"),
    ]

    operations = [
        migrations.RunPython(forwards, backwards),
    ]

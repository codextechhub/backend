"""Greet whoever a receipt, debit note or credit note email reaches by their own name.

The same email reaches a customer at their billing address and, for a pupil,
the pupil's primary guardian, whom the school names
(:mod:`vs_finance.document_contacts`). The guardian is greeted by their own
name and told whose receipt or note it is, rather than being addressed as
the child. A customer reading their own email reads it as before.

Only the email body is touched, and only while it still matches the copy it
shipped with: a body somebody has written is kept. The notes first shipped
with an em dash in their heading, later a hyphen, so either reads as shipped.

The shared-design HTML of these emails, and of the invoice and statement
emails 0022 reworded, is then composed again from the body, because a
migration's model does not rebuild it on save as the real model does. A
template whose HTML a person wrote (``html_is_custom``) is left alone.
"""

from importlib import import_module

from django.db import migrations

#: event key -> the email body to adopt, and the shipped body it replaces.
TEMPLATES = {
    'billing.payment_received': {
        "body": 'Hello {% if recipient_name %}{{ recipient_name }}{% else %}{{ customer_name }}{% endif %},\n\nWe have received {% if recipient_name and not recipient_is_customer %}a payment for {{ customer_name }}{% else %}your payment{% endif %}. Thank you. A PDF receipt is attached.\n\nReceipt number: {{ receipt_number }}\nAmount paid: ₦{{ amount_paid }}\nPayment date: {{ payment_date }}\nApplied to invoice: {{ invoice_number }}\n\n{{ note }}\n\n{{ issuer_name }} via {{ email_brand }}',
        "previous_body": 'Hello {{ customer_name }},\n\nWe have received your payment. Thank you. A PDF receipt is attached.\n\nReceipt number: {{ receipt_number }}\nAmount paid: ₦{{ amount_paid }}\nPayment date: {{ payment_date }}\nApplied to invoice: {{ invoice_number }}\n\n{{ note }}\n\n{{ issuer_name }} via {{ email_brand }}',
    },
    'billing.debit_note_issued': {
        "body": 'DEBIT NOTE - ADDITIONAL CHARGE\n\nHello {% if recipient_name %}{{ recipient_name }}{% else %}{{ customer_name }}{% endif %},\n\n{{ summary }}\n\nAdditional amount charged: ₦{{ note_amount }}\nDebit note: {{ note_number }}\nDate issued: {{ note_date }}\nRelated invoice: {{ related_invoice }}\nReason: {{ reason }}\n\nBEFORE THIS NOTE\n{{ previous_balance_label }}: ₦{{ previous_balance_amount }}\n\nCURRENT POSITION\n{{ current_balance_label }}: ₦{{ current_balance_amount }}\n\n{{ action_title }}\n{{ action_message }}\n\nIf anything in this adjustment is unclear, please contact the finance team before making payment.\n\n{{ issuer_name }} via {{ email_brand }}',
        "previous_body": 'DEBIT NOTE - ADDITIONAL CHARGE\n\nHello {{ customer_name }},\n\n{{ summary }}\n\nAdditional amount charged: ₦{{ note_amount }}\nDebit note: {{ note_number }}\nDate issued: {{ note_date }}\nRelated invoice: {{ related_invoice }}\nReason: {{ reason }}\n\nBEFORE THIS NOTE\n{{ previous_balance_label }}: ₦{{ previous_balance_amount }}\n\nCURRENT POSITION\n{{ current_balance_label }}: ₦{{ current_balance_amount }}\n\n{{ action_title }}\n{{ action_message }}\n\nIf anything in this adjustment is unclear, please contact the finance team before making payment.\n\n{{ issuer_name }} via {{ email_brand }}',
    },
    'billing.credit_note_issued': {
        "body": 'CREDIT NOTE - ACCOUNT CREDIT\n\nHello {% if recipient_name %}{{ recipient_name }}{% else %}{{ customer_name }}{% endif %},\n\n{{ summary }}\n\nAmount credited: ₦{{ note_amount }}\nCredit note: {{ note_number }}\nDate issued: {{ note_date }}\nRelated invoice: {{ related_invoice }}\nReason: {{ reason }}\n\nBEFORE THIS NOTE\n{{ previous_balance_label }}: ₦{{ previous_balance_amount }}\n\nCURRENT POSITION\n{{ current_balance_label }}: ₦{{ current_balance_amount }}\n\n{{ action_title }}\n{{ action_message }}\n\nIf anything in this adjustment is unclear, please contact the finance team before making payment.\n\n{{ issuer_name }} via {{ email_brand }}',
        "previous_body": 'CREDIT NOTE - ACCOUNT CREDIT\n\nHello {{ customer_name }},\n\n{{ summary }}\n\nAmount credited: ₦{{ note_amount }}\nCredit note: {{ note_number }}\nDate issued: {{ note_date }}\nRelated invoice: {{ related_invoice }}\nReason: {{ reason }}\n\nBEFORE THIS NOTE\n{{ previous_balance_label }}: ₦{{ previous_balance_amount }}\n\nCURRENT POSITION\n{{ current_balance_label }}: ₦{{ current_balance_amount }}\n\n{{ action_title }}\n{{ action_message }}\n\nIf anything in this adjustment is unclear, please contact the finance team before making payment.\n\n{{ issuer_name }} via {{ email_brand }}',
    },
}


# The invoice and statement emails 0022 reworded, tried again here with the
# heading dash read either way, for a database whose copy 0022 did not match.
TEMPLATES.update(
    import_module("vs_notifications.migrations.0022_finance_emails_greet_their_reader").TEMPLATES,
)

#: Every email whose greeting changed, here or in 0022, for the HTML rebuild.
REWORDED = tuple(TEMPLATES)


def _compose_html(apps):
    """Compose the shared-design HTML of every reworded email from its body again."""
    from vs_notifications.services.layout import EMAIL_BRAND_PLACEHOLDER, compose_email_html

    Template = apps.get_model("vs_notifications", "NotificationTemplate")
    for template in Template.objects.filter(
        event_type__key__in=REWORDED, channel="email", html_is_custom=False,
    ):
        template.html_body = compose_email_html(
            subject=template.subject, body=template.body, cta_label=template.cta_label,
            cta_url=template.cta_url, brand=EMAIL_BRAND_PLACEHOLDER, as_template=True,
        )
        template.save(update_fields=["html_body", "updated_at"])


def _as_shipped(body) -> str:
    """*body* with the heading dash the notes first shipped with read as a hyphen."""
    return (body or "").replace("\u2014", "-")


def greet_the_reader(apps, schema_editor):
    Template = apps.get_model("vs_notifications", "NotificationTemplate")
    for event_key, definition in TEMPLATES.items():
        for template in Template.objects.filter(event_type__key=event_key, channel="email"):
            if _as_shipped(template.body) == definition["previous_body"]:
                template.body = definition["body"]
                template.save(update_fields=["body", "updated_at"])
    _compose_html(apps)


def restore_the_shipped_copy(apps, schema_editor):
    Template = apps.get_model("vs_notifications", "NotificationTemplate")
    for event_key, definition in TEMPLATES.items():
        for template in Template.objects.filter(event_type__key=event_key, channel="email"):
            if template.body == definition["body"]:
                template.body = definition["previous_body"]
                template.save(update_fields=["body", "updated_at"])
    _compose_html(apps)


class Migration(migrations.Migration):
    dependencies = [
        ("vs_notifications", "0022_finance_emails_greet_their_reader"),
    ]

    operations = [
        migrations.RunPython(greet_the_reader, restore_the_shipped_copy),
    ]

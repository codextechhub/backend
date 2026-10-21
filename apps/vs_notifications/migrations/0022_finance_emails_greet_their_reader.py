"""Greet whoever an invoice or statement email reaches by their own name.

The same email reaches a customer at their billing address and, for a pupil,
the pupil's primary guardian, whom the school names
(:mod:`vs_finance.document_contacts`). The guardian is greeted by their own
name and told whose invoice or statement it is, rather than being addressed as
the child. A customer reading their own email reads it as before.

Only the email body is touched, and only while it still matches the copy it
shipped with: a body somebody has written is kept.
"""

from django.db import migrations

#: event key -> the email body to adopt, and the shipped body it replaces.
TEMPLATES = {
    'billing.invoice_issued': {
        "body": 'Hello {% if recipient_name %}{{ recipient_name }}{% else %}{{ customer_name }}{% endif %},\n\nAn invoice has been issued {% if recipient_name and not recipient_is_customer %}for {{ customer_name }}{% else %}to your account{% endif %}. A PDF copy is attached.\n\nInvoice number: {{ invoice_number }}\nAmount: ₦{{ invoice_amount }}\nDue date: {{ due_date }}\n\n{{ note }}\n\nPay online: {{ payment_link }}\n\n{{ issuer_name }} via {{ email_brand }}',
        "previous_body": 'Hello {{ customer_name }},\n\nAn invoice has been issued to your account. A PDF copy is attached.\n\nInvoice number: {{ invoice_number }}\nAmount: ₦{{ invoice_amount }}\nDue date: {{ due_date }}\n\n{{ note }}\n\nPay online: {{ payment_link }}\n\n{{ issuer_name }} via {{ email_brand }}',
    },
    'billing.statement_issued': {
        "body": 'Hello {% if recipient_name %}{{ recipient_name }}{% else %}{{ customer_name }}{% endif %},\n\n{% if recipient_name and not recipient_is_customer %}The statement of account for {{ customer_name }}{% else %}Your statement of account{% endif %} is attached as a PDF.\n\nPeriod: {{ period_start }} to {{ period_end }}\nOpening balance: ₦{{ opening_balance }}\nCharges in period: ₦{{ total_charges }}\nPayments and credits in period: ₦{{ total_payments }}\nClosing balance: ₦{{ closing_balance }}\n\n{{ note }}\n\nIf anything on this statement looks wrong, please contact the finance team before making payment.\n\n{{ issuer_name }} via {{ email_brand }}',
        "previous_body": 'Hello {{ customer_name }},\n\nYour statement of account is attached as a PDF.\n\nPeriod: {{ period_start }} to {{ period_end }}\nOpening balance: ₦{{ opening_balance }}\nCharges in period: ₦{{ total_charges }}\nPayments and credits in period: ₦{{ total_payments }}\nClosing balance: ₦{{ closing_balance }}\n\n{{ note }}\n\nIf anything on this statement looks wrong, please contact the finance team before making payment.\n\n{{ issuer_name }} via {{ email_brand }}',
    },
}


def greet_the_reader(apps, schema_editor):
    Template = apps.get_model("vs_notifications", "NotificationTemplate")
    for event_key, definition in TEMPLATES.items():
        for template in Template.objects.filter(event_type__key=event_key, channel="email"):
            if template.body == definition["previous_body"]:
                template.body = definition["body"]
                template.save(update_fields=["body", "updated_at"])


def restore_the_shipped_copy(apps, schema_editor):
    Template = apps.get_model("vs_notifications", "NotificationTemplate")
    for event_key, definition in TEMPLATES.items():
        for template in Template.objects.filter(event_type__key=event_key, channel="email"):
            if template.body == definition["body"]:
                template.body = definition["previous_body"]
                template.save(update_fields=["body", "updated_at"])


class Migration(migrations.Migration):
    dependencies = [
        ("vs_notifications", "0021_push_channel"),
    ]

    operations = [
        migrations.RunPython(greet_the_reader, restore_the_shipped_copy),
    ]

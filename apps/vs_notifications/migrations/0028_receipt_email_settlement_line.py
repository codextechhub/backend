"""Let the receipt email say where the money went, including when it is held as credit.

The receipt email carried an "Applied to invoice: {{ invoice_number }}" line for
every payment. A payment held wholly as credit has no invoice, so the label
printed with nothing after it, and a payment only partly applied said nothing of
the rest. The line is now ``{{ settlement_summary }}``: the invoices the payment
was applied to when there are any, and the amount held as credit when some or all
of it is (see :func:`vs_finance.documents.receipt_settlement_lines`).

Only the email body is touched, and only that one line of it: a body somebody has
written keeps everything else they wrote. The shared-design HTML of every
standard email is then composed again. A template whose HTML a person wrote
(``html_is_custom``) is left alone.
"""
from django.db import migrations

OLD_LINE = "Applied to invoice: {{ invoice_number }}"
NEW_LINE = "{{ settlement_summary }}"


def _swap(apps, old, new):
    from vs_notifications.services.layout import (
        EMAIL_BRAND_LOGO_PLACEHOLDER,
        EMAIL_BRAND_PLACEHOLDER,
        compose_email_html,
    )

    Template = apps.get_model("vs_notifications", "NotificationTemplate")
    changed = False
    for template in Template.objects.filter(
        event_type__key="billing.payment_received", channel="email",
    ):
        if old in template.body:
            template.body = template.body.replace(old, new)
            template.save(update_fields=["body", "updated_at"])
            changed = True
    if not changed:
        return

    updates = []
    for template in Template.objects.filter(channel="email", html_is_custom=False).iterator():
        template.html_body = compose_email_html(
            subject=template.subject,
            body=template.body,
            cta_label=template.cta_label,
            cta_url=template.cta_url,
            brand=EMAIL_BRAND_PLACEHOLDER,
            brand_logo_url=EMAIL_BRAND_LOGO_PLACEHOLDER,
            as_template=True,
        )
        updates.append(template)
    if updates:
        Template.objects.bulk_update(updates, ["html_body"], batch_size=50)


def adopt_settlement_line(apps, schema_editor):
    _swap(apps, OLD_LINE, NEW_LINE)


def restore_invoice_line(apps, schema_editor):
    _swap(apps, NEW_LINE, OLD_LINE)


class Migration(migrations.Migration):
    dependencies = [
        ("vs_notifications", "0027_invoice_email_payment_instructions"),
    ]

    operations = [
        migrations.RunPython(adopt_settlement_line, restore_invoice_line),
    ]

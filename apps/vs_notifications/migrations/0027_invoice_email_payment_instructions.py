"""Let the invoice email say how to pay when the link is not the way.

The invoice email carried a "Pay online" line and button for every customer.
A customer the books' owner asks to pay by bank transfer (see
:mod:`vs_finance.payer_options`) is told the account to pay into in that line's
place, so the line is now ``{{ payment_instructions }}``: the "Pay online" line
as before where card is offered, the transfer account where it is not. The
button is shown only when the link is not empty.

Only the email body is touched, and only that one line of it: a body somebody
has written keeps everything else they wrote. The shared-design HTML of every
standard email is then composed again, because the button now vanishes with an
empty link. A template whose HTML a person wrote (``html_is_custom``) is left
alone.
"""
from django.db import migrations

OLD_LINE = "Pay online: {{ payment_link }}"
NEW_LINE = "{{ payment_instructions }}"


def adopt_payment_instructions(apps, schema_editor):
    from vs_notifications.services.layout import (
        EMAIL_BRAND_LOGO_PLACEHOLDER,
        EMAIL_BRAND_PLACEHOLDER,
        compose_email_html,
    )

    Template = apps.get_model("vs_notifications", "NotificationTemplate")
    for template in Template.objects.filter(
        event_type__key="billing.invoice_issued", channel="email",
    ):
        if OLD_LINE in template.body:
            template.body = template.body.replace(OLD_LINE, NEW_LINE)
            template.save(update_fields=["body", "updated_at"])

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


def restore_pay_online_line(apps, schema_editor):
    Template = apps.get_model("vs_notifications", "NotificationTemplate")
    for template in Template.objects.filter(
        event_type__key="billing.invoice_issued", channel="email",
    ):
        if NEW_LINE in template.body:
            template.body = template.body.replace(NEW_LINE, OLD_LINE)
            template.save(update_fields=["body", "updated_at"])


class Migration(migrations.Migration):
    dependencies = [
        ("vs_notifications", "0026_push_subscription_last_seen"),
    ]

    operations = [
        migrations.RunPython(adopt_payment_instructions, restore_pay_online_line),
    ]

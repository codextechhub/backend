"""Let an audience sign the email header with its mark alone.

Standard email documents are composed again so the words beside the header
mark are left out when ``email_mark_only`` is set at render time, and the
mark's square turns white behind a logo. A school's email then shows its crest
with nothing beside it, while platform email keeps its name and "Secure
notification". Staff-authored HTML (``html_is_custom=True``) keeps its markup
exactly as written.
"""
from django.db import migrations


def compose_standard_email_headers(apps, schema_editor):
    Template = apps.get_model("vs_notifications", "NotificationTemplate")
    from vs_notifications.services.layout import (
        EMAIL_BRAND_LOGO_PLACEHOLDER,
        EMAIL_BRAND_PLACEHOLDER,
        compose_email_html,
    )

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


def noop(apps, schema_editor):
    """The previous generated markup is not user-authored data."""


class Migration(migrations.Migration):
    dependencies = [
        ("vs_notifications", "0023_receipt_and_note_emails_greet_their_reader"),
    ]

    operations = [
        migrations.RunPython(compose_standard_email_headers, noop),
    ]

"""Refresh standard email rows onto the current shared design.

Only platform-maintained HTML is regenerated. Staff-authored templates keep
their markup exactly as written and continue to opt out of shared design
changes through ``html_is_custom=True``.
"""

from django.db import migrations


def refresh_standard_email_design(apps, schema_editor):
    Template = apps.get_model("vs_notifications", "NotificationTemplate")
    from vs_notifications.services.layout import (
        EMAIL_BRAND_PLACEHOLDER,
        compose_email_html,
    )

    updates = []
    templates = Template.objects.filter(
        channel="email",
        html_is_custom=False,
    ).iterator()
    for template in templates:
        template.html_body = compose_email_html(
            subject=template.subject,
            body=template.body,
            cta_label=template.cta_label,
            cta_url=template.cta_url,
            brand=EMAIL_BRAND_PLACEHOLDER,
            as_template=True,
        )
        updates.append(template)

    if updates:
        Template.objects.bulk_update(updates, ["html_body"], batch_size=50)


def noop(apps, schema_editor):
    """The previous generated markup is not user-authored data."""


class Migration(migrations.Migration):
    dependencies = [
        ("vs_notifications", "0011_move_notification_ownership_to_recipient"),
    ]

    operations = [
        migrations.RunPython(refresh_standard_email_design, noop),
    ]

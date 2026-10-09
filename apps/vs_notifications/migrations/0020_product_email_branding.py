"""Use the CodeX or XVS product identity in standard notification email.

Templates keep ``email_brand`` as a recipient-time placeholder because the same
event can notify platform staff and school-product users. Standard HTML is
regenerated so existing rows gain the product logo slot and the current copy.
Staff-authored HTML remains untouched.
"""
from django.db import migrations


def apply_product_email_branding(apps, schema_editor):
    Template = apps.get_model("vs_notifications", "NotificationTemplate")
    from vs_notifications.services.layout import (
        EMAIL_BRAND_LOGO_PLACEHOLDER,
        EMAIL_BRAND_PLACEHOLDER,
        compose_email_html,
    )

    updates = []
    for template in Template.objects.all().iterator():
        changed = False
        for field in ("subject", "body", "cta_label"):
            value = getattr(template, field, "") or ""
            revised = value.replace("CodeX Vision", EMAIL_BRAND_PLACEHOLDER)
            if revised != value:
                setattr(template, field, revised)
                changed = True

        if template.channel == "email" and not template.html_is_custom:
            template.html_body = compose_email_html(
                subject=template.subject,
                body=template.body,
                cta_label=template.cta_label,
                cta_url=template.cta_url,
                brand=EMAIL_BRAND_PLACEHOLDER,
                brand_logo_url=EMAIL_BRAND_LOGO_PLACEHOLDER,
                as_template=True,
            )
            changed = True

        if changed:
            updates.append(template)

    if updates:
        Template.objects.bulk_update(
            updates,
            ["subject", "body", "cta_label", "html_body"],
            batch_size=50,
        )


def noop(apps, schema_editor):
    """Generated branding and retired product wording are not restored."""


class Migration(migrations.Migration):
    dependencies = [
        ("vs_notifications", "0019_branch_notification_settings"),
    ]

    operations = [
        migrations.RunPython(apply_product_email_branding, noop),
    ]

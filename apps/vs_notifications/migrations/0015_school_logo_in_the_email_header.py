"""Put the sender's own logo in the email header, where "CV" always was.

Every standard email signed itself with the platform's initials. A parent
opening a fee reminder from Holy Cross saw a "CV" badge beside the words "Holy
Cross College" - the name was the school's and the mark was a product they were
never told about.

Regenerating rather than patching the stored markup: the document is composed
by ``compose_email_html``, so the only honest way to change it is to compose it
again. Only platform-maintained HTML is touched. Staff-authored templates keep
their markup exactly as written and go on opting out through
``html_is_custom=True``.
"""

from django.db import migrations


def add_the_sender_logo(apps, schema_editor):
    Template = apps.get_model("vs_notifications", "NotificationTemplate")
    from vs_notifications.services.layout import (
        EMAIL_BRAND_LOGO_PLACEHOLDER,
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
            # Carried into the stored document as an {% if %}, not an <img>: a
            # sender with no logo must keep the initials rather than render
            # src="", which every mail client draws as a broken image.
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
        ("vs_notifications", "0014_refine_password_security_emails"),
    ]

    operations = [
        migrations.RunPython(add_the_sender_logo, noop),
    ]

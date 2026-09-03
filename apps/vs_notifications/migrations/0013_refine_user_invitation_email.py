"""Refresh the standard user invitation copy.

Only the platform-maintained email is changed. A staff-authored invitation
keeps its subject, body, action, and HTML exactly as written.
"""

from django.db import migrations


SUBJECT = (
    "{% if inviter_name %}{{ inviter_name }} invited you to "
    "{{ tenant_name }}{% else %}Your invitation to {{ tenant_name }}{% endif %}"
)
BODY = (
    "Hello {{ user_first_name }},\n\n"
    "{% if inviter_name %}{{ inviter_name }} invited you to join "
    "{{ tenant_name }} on XVision System.{% else %}You have been invited "
    "to join {{ tenant_name }} on XVision System.{% endif %}\n\n"
    "INVITATION DETAILS\n"
    "Name: {{ user_full_name }}\n"
    "Workspace: {{ tenant_name }}\n"
    "Link expires: {{ expiry_days }} days after this email\n\n"
    "Set a password to activate your account. This invitation link can only "
    "be used once.\n\n"
    "{{ invitation_url }}\n\n"
    "If you did not expect this invitation, ignore this email or contact the "
    "administrator who invited you."
)
CTA_LABEL = "Set up your account"
CTA_URL = "{{ invitation_url }}"


def refine_standard_invitation(apps, schema_editor):
    Template = apps.get_model("vs_notifications", "NotificationTemplate")
    from vs_notifications.services.layout import (
        EMAIL_BRAND_PLACEHOLDER,
        compose_email_html,
    )

    template = Template.objects.filter(
        event_type__key="user.invited",
        channel="email",
        html_is_custom=False,
    ).first()
    if template is None:
        return

    template.subject = SUBJECT
    template.body = BODY
    template.cta_label = CTA_LABEL
    template.cta_url = CTA_URL
    template.html_body = compose_email_html(
        subject=SUBJECT,
        body=BODY,
        cta_label=CTA_LABEL,
        cta_url=CTA_URL,
        brand=EMAIL_BRAND_PLACEHOLDER,
        as_template=True,
    )
    template.save(
        update_fields=["subject", "body", "cta_label", "cta_url", "html_body"]
    )


def noop(apps, schema_editor):
    """The previous platform-maintained copy is not user-authored data."""


class Migration(migrations.Migration):
    dependencies = [
        ("vs_notifications", "0012_refresh_standard_email_design"),
    ]

    operations = [
        migrations.RunPython(refine_standard_invitation, noop),
    ]

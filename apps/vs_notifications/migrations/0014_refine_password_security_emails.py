"""Refresh the standard password-reset and account-lock email copy.

Only platform-maintained markup is changed. Staff-authored versions keep
their subject, body, action, and HTML exactly as written.
"""

from django.db import migrations


PASSWORD_RESET_SUBJECT = (
    "{% if origin == 'ADMIN' %}{{ sender_name }} requested a password "
    "reset for {{ tenant_name }}{% else %}Reset your password for "
    "{{ tenant_name }}{% endif %}"
)
PASSWORD_RESET_BODY = (
    "Hello {{ user_first_name }},\n\n"
    "{% if origin == 'ADMIN' %}{{ sender_name }} requested a password reset "
    "for your {{ tenant_name }} account.{% else %}We received a request to "
    "reset the password for your {{ tenant_name }} account.{% endif %}\n\n"
    "PASSWORD RESET DETAILS\n"
    "Account: {{ user_email }}\n"
    "Workspace: {{ tenant_name }}\n"
    "Link expires: {{ expires_at }}\n\n"
    "Use the secure link to choose a new password. It can only be used once, "
    "and requesting another reset will make this link stop working.\n\n"
    "{{ reset_url }}\n\n"
    "{% if origin == 'ADMIN' %}If you do not recognize this request or the "
    "person who sent it, contact your administrator before using the link."
    "{% else %}If you did not request this reset, ignore this email and "
    "consider asking your administrator to review recent account activity."
    "{% endif %}\n\n"
    "Your current password remains unchanged until this link is used successfully."
)
PASSWORD_RESET_CTA_LABEL = "Choose a new password"
PASSWORD_RESET_CTA_URL = "{{ reset_url }}"

ACCOUNT_LOCKED_SUBJECT = "Security alert: your {{ tenant_name }} account is locked"
ACCOUNT_LOCKED_BODY = (
    "Hello {{ user_name }},\n\n"
    "We locked your {{ tenant_name }} account after repeated failed sign-in "
    "attempts.\n\n"
    "SECURITY DETAILS\n"
    "Workspace: {{ tenant_name }}\n"
    "Locked at: {{ locked_at }}\n\n"
    "Sign-in is blocked until an administrator restores access. Review the "
    "unlock steps or contact your administrator.\n\n"
    "{{ unlock_instructions_link }}\n\n"
    "If these sign-in attempts were not yours, contact your administrator "
    "immediately and ask them to review the account activity."
)
ACCOUNT_LOCKED_CTA_LABEL = "Review unlock steps"
ACCOUNT_LOCKED_CTA_URL = "{{ unlock_instructions_link }}"


def refine_standard_security_emails(apps, schema_editor):
    Template = apps.get_model("vs_notifications", "NotificationTemplate")
    from vs_notifications.services.layout import (
        EMAIL_BRAND_PLACEHOLDER,
        compose_email_html,
    )

    definitions = (
        (
            "user.password_reset",
            PASSWORD_RESET_SUBJECT,
            PASSWORD_RESET_BODY,
            PASSWORD_RESET_CTA_LABEL,
            PASSWORD_RESET_CTA_URL,
        ),
        (
            "user.account_locked",
            ACCOUNT_LOCKED_SUBJECT,
            ACCOUNT_LOCKED_BODY,
            ACCOUNT_LOCKED_CTA_LABEL,
            ACCOUNT_LOCKED_CTA_URL,
        ),
    )

    for event_key, subject, body, cta_label, cta_url in definitions:
        template = Template.objects.filter(
            event_type__key=event_key,
            channel="email",
            html_is_custom=False,
        ).first()
        if template is None:
            continue

        template.subject = subject
        template.body = body
        template.cta_label = cta_label
        template.cta_url = cta_url
        template.html_body = compose_email_html(
            subject=subject,
            body=body,
            cta_label=cta_label,
            cta_url=cta_url,
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
        ("vs_notifications", "0013_refine_user_invitation_email"),
    ]

    operations = [
        migrations.RunPython(refine_standard_security_emails, noop),
    ]

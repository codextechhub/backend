from email.utils import formataddr

from django.conf import settings
from django.core.mail import EmailMultiAlternatives


def build_from_email(display_name: str | None = None) -> str:
    """
    Return a formatted From address using the configured sender address.

    The domain/address comes from DEFAULT_FROM_EMAIL; only the display
    name is swapped out.  Falls back to the original DEFAULT_FROM_EMAIL
    display name when none is supplied.

    Examples:
        build_from_email("Chidera Divine-gift")
        → "Chidera Divine-gift <system@codexng.com>"

        build_from_email()
        → "CodeX System <system@codexng.com>"
    """
    from vs_config.runtime_settings import get_integration_settings

    integration_settings = get_integration_settings()
    default_name = integration_settings["email_sender_name"]
    address = integration_settings["email_sender_address"]
    return formataddr((display_name or default_name or 'CodeX System', address))


def send_email(
    *,
    subject: str,
    plain_message: str,
    html_message: str | None = None,
    recipient_list: list[str],
    from_email: str | None = None,
    cc: list[str] | None = None,
) -> None:
    """
    Central email sender for the platform.

    Automatically attaches any addresses listed in settings.EMAIL_CC so
    every outgoing email gets the same CC list (useful for monitoring /
    testing). Clear EMAIL_CC in the environment to disable.

    ``html_message`` is optional: when provided the message is sent multipart
    (plain body + HTML alternative); when omitted (or empty) a plain-text-only
    email is sent. Existing keyword callers that always pass html_message keep
    working unchanged.
    """
    from_email = from_email or build_from_email()
    cc = cc or getattr(settings, 'EMAIL_CC', [])

    msg = EmailMultiAlternatives(
        subject=subject,
        body=plain_message,
        from_email=from_email,
        to=recipient_list,
        cc=cc,
    )
    if html_message:
        msg.attach_alternative(html_message, 'text/html')
    msg.send()

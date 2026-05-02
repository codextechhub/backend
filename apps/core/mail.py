from email.utils import formataddr, parseaddr

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
    default_name, address = parseaddr(settings.DEFAULT_FROM_EMAIL)
    return formataddr((display_name or default_name or 'CodeX System', address))


def send_email(
    *,
    subject: str,
    plain_message: str,
    html_message: str,
    recipient_list: list[str],
    from_email: str | None = None,
) -> None:
    """
    Central email sender for the platform.

    Automatically attaches any addresses listed in settings.EMAIL_CC so
    every outgoing email gets the same CC list (useful for monitoring /
    testing). Clear EMAIL_CC in the environment to disable.
    """
    from_email = from_email or build_from_email()
    cc = getattr(settings, 'EMAIL_CC', [])

    msg = EmailMultiAlternatives(
        subject=subject,
        body=plain_message,
        from_email=from_email,
        to=recipient_list,
        cc=cc,
    )
    msg.attach_alternative(html_message, 'text/html')
    msg.send()

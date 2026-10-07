"""Error reporting to Sentry, with personal data kept out of what it receives.

This platform holds children's records and parents' contact details, so the
reports carry the failure and not the people in it: no request bodies, no
cookies or credentials, no signed-in user, no local variables from the failing
frame, and no log lines as breadcrumbs. What remains is the exception, the
stack, the route and the release, which is what finding the fault needs.

Reporting is off unless a deployment names a DSN, and a DSN without an
environment name is refused: a staging error filed under production (or the
reverse) is a report nobody can trust.
"""
import sentry_sdk
from django.core.exceptions import ImproperlyConfigured
from sentry_sdk.integrations.celery import CeleryIntegration
from sentry_sdk.integrations.django import DjangoIntegration
from sentry_sdk.integrations.logging import LoggingIntegration

#: Request headers that identify or authenticate the caller.
_SENSITIVE_HEADERS = frozenset({
    "authorization", "cookie", "set-cookie", "x-csrftoken", "x-rfq-session",
    "x-forwarded-for", "cf-connecting-ip", "true-client-ip",
})


def scrub_event(event, hint):
    """Remove from an outgoing event everything that can name a person."""
    request = event.get("request")
    if request:
        request.pop("data", None)
        request.pop("cookies", None)
        request.pop("query_string", None)
        request.pop("env", None)
        headers = request.get("headers")
        if headers:
            request["headers"] = {
                name: value for name, value in headers.items()
                if name.lower() not in _SENSITIVE_HEADERS
            }
    event.pop("user", None)
    return event


def init_sentry(dsn: str, environment: str, release: str = "") -> bool:
    """Start reporting when ``dsn`` is set; return whether it started."""
    if not dsn:
        return False
    if not environment:
        raise ImproperlyConfigured(
            "SENTRY_ENVIRONMENT must name this deployment (for example "
            "'production' or 'staging') whenever SENTRY_DSN is set."
        )
    sentry_sdk.init(
        dsn=dsn,
        environment=environment,
        release=release or None,
        integrations=[
            DjangoIntegration(),
            CeleryIntegration(),
            # Log lines are not breadcrumbs: messages can quote a record.
            LoggingIntegration(level=None, event_level="ERROR"),
        ],
        send_default_pii=False,
        include_local_variables=False,
        max_request_body_size="never",
        traces_sample_rate=0.0,
        before_send=scrub_event,
    )
    return True

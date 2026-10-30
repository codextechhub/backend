"""Error reporting to Sentry, with personal data kept out of what it receives.

This platform holds children's records and parents' contact details, so the
reports carry the failure and not the people in it: no request bodies, no
cookies or credentials, no signed-in user, no local variables from the failing
frame, and no breadcrumbs (the trail of queries and log lines before the
failure, which can quote a record). What remains is the exception, the
stack, the route and the release, which is what finding the fault needs.

The words of the failure are scrubbed too. A duplicate guardian fails with
"Key (email)=(ada.okeye@gmail.com) already exists", and a log line can quote a
phone number, so every exception message, log message and its arguments, the
event's extra data and the request URL go through the same redaction the logs
and task records use (:mod:`core.redaction`) before they leave.

Reporting is off unless a deployment names a DSN, and a DSN without an
environment name is refused: a staging error filed under production (or the
reverse) is a report nobody can trust.
"""
import sentry_sdk
from django.core.exceptions import ImproperlyConfigured
from sentry_sdk.integrations.celery import CeleryIntegration
from sentry_sdk.integrations.django import DjangoIntegration
from sentry_sdk.integrations.logging import LoggingIntegration

from core.redaction import redact_payload, redact_text

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
        if request.get("url"):
            request["url"] = redact_text(request["url"])
    event.pop("user", None)

    for exception in (event.get("exception") or {}).get("values") or []:
        exception["value"] = redact_text(exception.get("value"))
    logentry = event.get("logentry")
    if logentry:
        for key in ("message", "formatted"):
            logentry[key] = redact_text(logentry.get(key))
        if logentry.get("params"):
            logentry["params"] = redact_payload(logentry["params"])
    if isinstance(event.get("message"), str):
        event["message"] = redact_text(event["message"])
    if event.get("extra"):
        event["extra"] = redact_payload(event["extra"])
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
            # Log lines are not events below ERROR: messages can quote a record.
            LoggingIntegration(level=None, event_level="ERROR"),
        ],
        send_default_pii=False,
        include_local_variables=False,
        max_breadcrumbs=0,
        max_request_body_size="never",
        traces_sample_rate=0.0,
        before_send=scrub_event,
    )
    return True

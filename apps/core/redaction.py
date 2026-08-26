"""Central redaction for anything a background task stores or logs.

Why this exists
---------------
A failed task records what went wrong, and what went wrong is routinely made
of somebody's personal data. Postgres is the clearest case: a duplicate
guardian email does not fail with "duplicate email", it fails with

    duplicate key value violates unique constraint "vs_user_user_email_key"
    DETAIL:  Key (email)=(ada.okeye@gmail.com) already exists.

and an SMTP refusal stringifies the recipient's address into the exception
message. Left alone, that text is copied verbatim into
``BackgroundJob.error`` and from there into every backup, every log line and
every API that serialises the column - including the school-facing one at
``/v1/user/me/tasks/``.

Redacting at each call site would be endless: every task in the platform can
fail, and new ones arrive weekly. So the scrub happens at the two choke points
every task passes through - ``core.tasks_base.TrackedTask._finish`` on the way
into the database, and :class:`RedactingLogFilter` on the way into the log
stream - and this module is what both of them call.

What is NOT lost
----------------
The unredacted text is not thrown away. ``TrackedTask._finish`` writes it to
:class:`core.models.TaskDiagnostic`, which no list endpoint serialises, which
requires ``platform.tasks.view_sensitive`` to read, and which records an audit
event naming whoever read it. Redaction moves the raw text to a controlled
surface; it does not destroy the debugging trail.

Deliberately conservative
-------------------------
These patterns over-redact rather than under-redact. A 12-digit invoice
reference will be masked along with a bank account number, because the cost of
masking a reference is that an operator opens the diagnostic record, and the
cost of missing an account number is that it sits in a backup for a year.
"""
from __future__ import annotations

import logging
import re

#: What replaces a matched value. Distinct per kind so a reader can tell what
#: was removed and decide whether the diagnostic record is worth opening.
EMAIL_MASK = "[email redacted]"
PHONE_MASK = "[phone redacted]"
DIGITS_MASK = "[number redacted]"
VALUE_MASK = "[redacted]"

# Postgres embeds the offending row value in the DETAIL line of a constraint
# violation: `Key (email)=(ada.okeye@gmail.com) already exists.` The column
# name is diagnostic and stays; the value is the payload and goes. Handled
# before the narrower patterns so a value of any shape is caught, including
# the ones no other rule here recognises (a guardian's name, a home address).
_PG_DETAIL_KEY = re.compile(
    r"(?P<head>Key\s*\([^)]*\)\s*=\s*\()(?P<value>[^)]*)(?P<tail>\))",
    re.IGNORECASE,
)

_EMAIL = re.compile(
    r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}",
)

# International (+234...) and local (080...) forms, 7 to 15 digits with the
# separators people actually type. Bounded by non-digits so it cannot eat a
# longer number that the digit rule below should own.
_PHONE = re.compile(
    r"(?<!\d)(?:\+\d{1,3}[\s\-]?)?(?:\(\d{1,4}\)[\s\-]?)?\d{3}[\s\-]?\d{3,4}[\s\-]?\d{2,4}(?!\d)",
)

# Bank accounts (10 digits in Nigeria), BVN (11), NIN (11), card PANs (16).
# Runs of 8+ digits are not something a redacted error message needs.
_LONG_DIGITS = re.compile(r"(?<!\d)\d{8,}(?!\d)")

#: Mapping keys whose VALUE is replaced wholesale, whatever it looks like.
#: Matched as a substring of the lowercased key, so ``guardian_email`` and
#: ``billing_address`` are both caught.
_SENSITIVE_KEY_PARTS = (
    "email", "mail",
    "phone", "msisdn", "mobile", "telephone",
    "address",
    "password", "secret", "token", "api_key", "apikey",
    "authorization", "auth_header", "credential",
    "account_number", "account_no", "accountnumber",
    "bvn", "nin", "iban", "swift", "sort_code",
    "card", "pan", "cvv", "ssn",
    "dob", "date_of_birth", "birth_date",
)

#: Keys whose value is replaced only when matched EXACTLY. These words appear
#: inside perfectly innocent keys - ``task_name``, ``template_name``,
#: ``file_name`` - so a substring rule here would redact the very fields the
#: task monitor exists to show.
_SENSITIVE_KEY_EXACT = frozenset({
    "name", "full_name", "first_name", "last_name", "middle_name",
    "surname", "guardian", "guardian_name", "parent_name",
    "next_of_kin", "recipient", "recipients", "to", "cc", "bcc",
})


# Decide whether a mapping key names something that must never be stored.
def is_sensitive_key(key) -> bool:
    """True when *key* names a field whose value is replaced wholesale."""
    if not isinstance(key, str):
        return False
    lowered = key.strip().lower()
    if lowered in _SENSITIVE_KEY_EXACT:
        return True
    return any(part in lowered for part in _SENSITIVE_KEY_PARTS)


# Strip personal data out of one free-text blob (an error message, a traceback).
def redact_text(text):
    """Return *text* with emails, phone numbers and long digit runs masked.

    Non-strings are returned untouched so callers can pass a value through
    without type-checking it first. The rules run widest-first: the Postgres
    DETAIL payload is removed whole before the narrower patterns look at what
    is left, because that payload can hold personal data of a shape no regex
    here would otherwise recognise.
    """
    if not isinstance(text, str) or not text:
        return text

    redacted = _PG_DETAIL_KEY.sub(
        lambda m: f"{m.group('head')}{VALUE_MASK}{m.group('tail')}", text,
    )
    redacted = _EMAIL.sub(EMAIL_MASK, redacted)
    # Long digit runs before phones: an account number is the more damaging of
    # the two and the phone pattern would otherwise claim part of it.
    redacted = _LONG_DIGITS.sub(DIGITS_MASK, redacted)
    redacted = _PHONE.sub(PHONE_MASK, redacted)
    return redacted


# Strip personal data out of a structured task result before it is stored.
def redact_payload(value, _depth: int = 0):
    """Recursively redact a JSON-shaped task return value.

    A key naming something sensitive loses its value entirely; every other
    string is passed through :func:`redact_text`. Numbers and booleans are
    left alone - a row count is the whole point of a task result.

    ``_depth`` guards against a self-referential structure; past 12 levels the
    branch is replaced rather than walked, because a task result deep enough to
    hit that is not one anybody is reading off a screen.
    """
    if _depth > 12:
        return VALUE_MASK

    if isinstance(value, dict):
        return {
            key: (
                VALUE_MASK if is_sensitive_key(key)
                else redact_payload(item, _depth + 1)
            )
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [redact_payload(item, _depth + 1) for item in value]
    if isinstance(value, str):
        return redact_text(value)
    return value


# Apply the same scrub to everything on its way into the log stream.
class RedactingLogFilter(logging.Filter):
    """Redact log records in place, wherever they were emitted from.

    Installed on every handler rather than on the loggers this module knows
    about. The email that reaches ``BackgroundJob.error`` reaches the log
    stream by an entirely separate route - ``logger.warning(..., exc_info=True)``
    in a dozen apps, plus Celery's own traceback printing - and gating only
    the known callers would leave every future one open. A filter on the
    handler is the one place all of them meet.

    Three things are scrubbed, and all three are load-bearing:

    * ``record.msg``, the format string;
    * ``record.args``, because ``logger.info("delivering to %s", address)``
      keeps the address there until formatting happens;
    * the formatted exception, which is where the interesting leak actually
      lives. ``logger.warning(..., exc_info=True)`` carries the exception
      MESSAGE, and the exception message is exactly what Postgres and smtplib
      fill with personal data. Scrubbing only msg and args would have left
      every traceback in the stream untouched.

    The exception is formatted here and cached on ``record.exc_text``, which
    is the attribute :class:`logging.Formatter` reuses instead of formatting
    the traceback again. That is what makes one filter cover both the JSON
    formatter and the plain one.
    """

    def filter(self, record):
        if isinstance(record.msg, str):
            record.msg = redact_text(record.msg)

        if isinstance(record.args, dict):
            record.args = {
                key: (
                    VALUE_MASK if is_sensitive_key(key) else redact_payload(item)
                )
                for key, item in record.args.items()
            }
        elif isinstance(record.args, tuple):
            record.args = tuple(redact_payload(item) for item in record.args)

        if record.exc_info:
            if not record.exc_text:
                record.exc_text = logging.Formatter().formatException(record.exc_info)
            record.exc_text = redact_text(record.exc_text)

        if record.stack_info:
            record.stack_info = redact_text(record.stack_info)

        return True

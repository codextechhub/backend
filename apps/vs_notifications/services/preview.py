# =============================================================================
# vs_notifications / services / preview.py
#
# Two helpers that make a template self-describing, so the editor needs no
# hand-maintained variable list and no hand-typed sample data:
#
#   template_variables()  - the {{ names }} a template actually uses.
#   sample_context()      - believable stand-in values for those names, so a
#                           preview shows the real visual on first open.
#
# Sample values are for LOOKING at a template. They are never dispatched and
# never touch a Notification record.
# =============================================================================

from __future__ import annotations

import re


# {{ variable }} and {{ variable|filter:"arg" }} - the name is what we want.
_VARIABLE_RE = re.compile(r"{{\s*([a-zA-Z_][a-zA-Z0-9_]*)")
# {% if variable %} / {% if variable == 'X' %} / {% for x in variable %}
_TAG_RE = re.compile(r"{%\s*(?:if|elif|ifchanged)\s+([a-zA-Z_][a-zA-Z0-9_]*)")
_FOR_RE = re.compile(r"{%\s*for\s+[a-zA-Z_][a-zA-Z0-9_]*\s+in\s+([a-zA-Z_][a-zA-Z0-9_]*)")

# Django template keywords that are not context variables.
_NOT_VARIABLES = {"True", "False", "None", "not", "and", "or", "in", "is"}


# Collect the context variable names one or more template strings reference.
def template_variables(*texts: str) -> list[str]:
    """
    Return the sorted, de-duplicated variable names used across `texts`.

    Deliberately a scan, not a full parse: it must survive half-written copy in
    the editor (a preview of a broken template is more useful than an error),
    and the names are only used for documentation and sample data.
    """
    found: set[str] = set()
    for text in texts:
        if not text:
            continue
        for pattern in (_VARIABLE_RE, _TAG_RE, _FOR_RE):
            found.update(pattern.findall(text))
    return sorted(name for name in found if name not in _NOT_VARIABLES)


# ---------------------------------------------------------------------------
# Sample values
#
# Rules are tried in order; the first whose predicate matches the variable name
# wins. Keep the specific names above the generic suffixes.
# ---------------------------------------------------------------------------

_EXACT_SAMPLES = {
    "school_name":      "Corona Secondary School",
    "issuer_name":      "Corona Secondary School",
    "entity_name":      "Corona Secondary School",
    "tenant_name":      "Corona Secondary School",
    "entity_code":      "CSS-MAIN",
    "branch_name":      "Main Branch",
    "class_name":       "JSS 2 Blue",
    "session_name":     "2026/2027",
    "customer_name":    "Mr. Adeola Bakare",
    "vendor_name":      "Bluecrest Supplies Ltd",
    "origin":           "SELF",
    "expiry_days":      7,
    "expiry_hours":     24,
    "expiry_minutes":   15,
    "verification_code": "482913",
    "revision":         2,
    "reason":           "Correction to an earlier charge",
    "summary":          "your account balance has been adjusted",
    "window_minutes":   30,
    "total_steps":      6,
    "step_number":      4,
}

_SUFFIX_SAMPLES = (
    (("_url", "_link"),                 "https://xvs.codexng.com/example"),
    (("_email",),                       "sample.person@example.com"),
    (("_at", "_date", "deadline"),      "15 Aug 2026, 10:30"),
    (("_amount", "amount", "_total",
      "total", "balance", "price"),     "125,000.00"),
    (("_count", "count", "rows"),       12),
    (("_number", "_no", "_id",
      "reference", "_code", "code",
      "_invoice", "invoice"),           "DOC-2026-0417"),
    (("_name", "name"),                 "Jane Doe"),
    (("_title", "title", "label",
      "_type", "subject"),              "Sample item"),
    (("_message", "message", "body",
      "comment", "description",
      "_summary"),                      "A short line of explanatory text."),
    (("_status", "status", "priority",
      "category"),                      "Open"),
)


# Build stand-in values so a preview renders without the caller typing anything.
def sample_context(variables: list[str], overrides: dict | None = None) -> dict:
    """
    Return {variable: believable value} for every name in `variables`.

    `overrides` (the caller's own sample values) always win, and any extra key
    they supply is kept - previewing with one real value and the rest filled in
    is the common case.
    """
    context: dict = {}
    for name in variables:
        context[name] = _sample_value(name)
    context.update(overrides or {})
    return context


def _sample_value(name: str):
    if name in _EXACT_SAMPLES:
        return _EXACT_SAMPLES[name]
    # A boolean-shaped name must render as a boolean, or {% if %} branches in
    # the template would take the wrong path and the preview would lie.
    if name.startswith(("is_", "has_", "can_", "should_")):
        return True
    lowered = name.lower()
    for suffixes, value in _SUFFIX_SAMPLES:
        if any(
            lowered.endswith(suffix) if suffix.startswith("_") else lowered == suffix
            for suffix in suffixes
        ):
            return value
    return name.replace("_", " ").capitalize()

# =============================================================================
# vs_notifications / services / layout.py
#
# The ONE HTML shell every email notification is rendered into.
#
# A notification template carries plain text and (optionally) a call-to-action.
# The visual - header, typography, detail tables, button, footer - comes from
# here, not from markup pasted into each template. That is the whole point:
#   * an author writes a message, not an HTML document;
#   * every email looks the same because there is one place it is styled;
#   * a template that never carried html_body still ships a proper email.
#
# compose_email_html() reads the ALREADY RENDERED plain body (post {{ }}
# substitution) and infers structure from how the text is written:
#
#   Label: value      (two or more in a row)   -> a details table
#   - item / • item                            -> a bulleted list
#   ALL CAPS SHORT LINE                        -> a section heading
#   ─────── / ━━━━━━━                          -> a horizontal rule
#   anything else                              -> a paragraph
#
# Everything is escaped and then urlized, so a rendered value containing
# markup can never inject HTML into the email (see urlize(autoescape=True) -
# render.py deliberately renders templates with autoescape OFF because admins
# author the copy, so escaping has to happen HERE, at the boundary).
#
# Layout rules for email clients: tables, inline styles, one 600px column,
# no external assets, no <style> block (Gmail strips much of it anyway).
# =============================================================================

from __future__ import annotations

import re

from django.utils.html import urlize


# Shown in the header strip when the context carries no issuer/school name.
BRAND_FALLBACK = "CodeX Vision"

# Context keys consulted, in order, for the sender name in the header.
_BRAND_CONTEXT_KEYS = ("issuer_name", "school_name", "entity_name", "tenant_name")

# Palette - kept in sync with the console's neutral scale.
_INK      = "#101828"
_BODY     = "#475467"
_MUTED    = "#667085"
_FAINT    = "#98a2b3"
_LINE     = "#eaecf0"
_PANEL    = "#f9fafb"
_PAGE     = "#f4f6f8"
_ACCENT   = "#2f6fed"
_HEADER   = "#0f2747"

_FONT = (
    "-apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, "
    "'Helvetica Neue', Arial, sans-serif"
)

# A line made only of rule characters, e.g. the ━━━ separators in the seeds.
_RULE_RE = re.compile(r"^[\s\-=_~━─—]{3,}$")
# "Label: value" - the label is short, single-clause, and carries no URL scheme.
_DETAIL_RE = re.compile(r"^ {0,3}([^:/<>]{1,40}?) *: +(\S.*?) *$")
_BULLET_RE = re.compile(r"^ *[-*•] +(\S.*?) *$")


# A whole line that is nothing but one Django placeholder.
_PLACEHOLDER_RE = re.compile(r"^\s*{[{%].*[}%]}\s*$")
# Any Django variable or tag, non-greedy so adjacent tags stay separate.
_TAG_RE = re.compile(r"{{.*?}}|{%.*?%}", re.DOTALL)


def _is_placeholder(value: str) -> bool:
    """True when the value is a Django expression rather than a literal."""
    return bool(_PLACEHOLDER_RE.match(value or ""))


class _TagGuard:
    """
    Hides Django tags from the HTML escaper and puts them back afterwards.

    Composing a TEMPLATE document means the text still contains {{ vars }} and
    {% if %} blocks. Those must reach the stored markup byte-for-byte: escaping
    turns the quotes in {% if origin == 'ADMIN' %} into &#x27; and the tag stops
    working, silently, at the point an email is being sent to a real person.

    Each tag is swapped for a token made of characters no escaper touches, and
    restored once the document is assembled. Inert when active=False, so the
    per-recipient path (where tags are long gone) pays nothing.
    """

    # Letters and digits only: survives HTML escaping, urlize, and _clean.
    _TOKEN = "zZnotifytagZz{}zZ"

    def __init__(self, active: bool):
        self.active = active
        self.tags: list[str] = []
        # Identical tags share one token, so a bare {{ url }} line in the body
        # still compares equal to the CTA destination and can be dropped.
        self._tokens: dict[str, str] = {}

    def mask(self, text: str) -> str:
        if not self.active or not text:
            return text

        def swap(match):
            tag = match.group(0)
            if tag not in self._tokens:
                self.tags.append(tag)
                self._tokens[tag] = self._TOKEN.format(len(self.tags) - 1)
            return self._tokens[tag]

        return _TAG_RE.sub(swap, text)

    def unmask(self, text: str) -> str:
        for index, tag in enumerate(self.tags):
            text = text.replace(self._TOKEN.format(index), tag)
        return text


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

# Wrap one rendered message in the standard email shell.
def compose_email_html(
    *,
    subject: str,
    body: str,
    cta_label: str = "",
    cta_url: str = "",
    brand: str = "",
    as_template: bool = False,
    placeholder_cta: bool = False,
) -> str:
    """
    Build the full HTML document for one email notification.

    Two callers, two modes:

      as_template=False (default) - subject/body are already RENDERED for one
        recipient. Used as the safety net when a template carries no stored
        markup. Destinations must be absolute http(s) to become a button.

      as_template=True - subject/body still contain {{ placeholders }}, and the
        result is the reusable document stored on NotificationTemplate.html_body.
        Django tag syntax is protected from HTML escaping, and a placeholder
        destination is kept because its real value only exists at dispatch.

    Args:
        subject:         Subject line. Doubles as the headline.
        body:            Plain-text body. Structure is inferred from it.
        cta_label:       Button label. Ignored when cta_url is empty.
        cta_url:         Button destination. Empty means no button.
        brand:           Sender name for the header strip. Falls back to the platform.
        as_template:     Compose a reusable template document (see above).
        placeholder_cta: Keep a non-http destination (a {{ variable }}).

    Returns:
        A complete <!doctype html> document.
    """
    keep_placeholders = as_template or placeholder_cta
    # Swap Django tags for inert tokens before anything is escaped: escaping the
    # quotes inside {% if origin == 'ADMIN' %} would silently break the tag when
    # the stored document is later rendered.
    guard = _TagGuard(active=as_template)
    raw_cta_url = (cta_url or "").strip()

    subject = guard.mask(subject or "")
    body = guard.mask(body or "")
    cta_label = guard.mask(cta_label or "").strip() or "Open in CodeX Vision"
    cta_url = guard.mask(raw_cta_url)

    # Only absolute http(s) destinations become buttons. A javascript: value
    # must never be clickable, and a relative path cannot resolve in a mail
    # client anyway - dropping it beats shipping a dead button. A template
    # document is exempt: its destination is a placeholder by definition.
    if not (keep_placeholders and _is_placeholder(raw_cta_url)):
        if not raw_cta_url.lower().startswith(("http://", "https://")):
            cta_url = ""

    brand = _clean(brand) or BRAND_FALLBACK
    headline = _clean(subject) or brand
    content = _blocks_to_html(body or "", skip_url=cta_url)
    button = _button_html(cta_label, cta_url) if cta_url else ""
    sent_by = (
        "This message was sent automatically by CodeX Vision."
        if brand == BRAND_FALLBACK
        else f"This message was sent by {_text(brand)} through CodeX Vision."
    )

    return guard.unmask(f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="color-scheme" content="light">
<title>{_text(headline)}</title>
</head>
<body style="margin:0;padding:0;background-color:{_PAGE};font-family:{_FONT};color:{_INK};">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" \
style="width:100%;border-collapse:collapse;background-color:{_PAGE};">
<tr><td align="center" style="padding:32px 12px;">
<table role="presentation" width="600" cellpadding="0" cellspacing="0" \
style="width:100%;max-width:600px;border-collapse:separate;background-color:#ffffff;\
border:1px solid #e4e7ec;border-radius:12px;overflow:hidden;">
<tr><td style="padding:22px 32px;background-color:{_HEADER};">
<div style="color:#ffffff;font-size:13px;font-weight:600;opacity:.85;">{_text(brand)}</div>
<div style="color:#ffffff;font-size:21px;line-height:1.35;font-weight:700;padding-top:6px;">\
{_text(headline)}</div>
</td></tr>
<tr><td style="padding:28px 32px;">
{content}{button}
</td></tr>
<tr><td style="padding:18px 32px;background-color:{_PANEL};border-top:1px solid {_LINE};">
<p style="margin:0;color:{_FAINT};font-size:12px;line-height:1.5;">\
{sent_by} Please do not reply to this email.</p>
</td></tr>
</table>
</td></tr>
</table>
</body>
</html>
""")


# Resolve the header's sender name from the caller-supplied context.
def brand_from_context(context: dict) -> str:
    """Return the first non-empty branding value in the context, else ""."""
    for key in _BRAND_CONTEXT_KEYS:
        value = _clean(str(context.get(key) or "")) if context else ""
        if value:
            return value
    return ""


# ---------------------------------------------------------------------------
# Block rendering
# ---------------------------------------------------------------------------

# Convert one rendered plain-text body into the shell's inner HTML.
def _blocks_to_html(body: str, skip_url: str = "") -> str:
    """
    Render the body block by block, and each block run by run.

    Runs matter because the seeded copy mixes shapes inside one block - a
    heading line sitting directly on top of its "Label: value" rows is the
    common case - and treating the whole block as one shape would flatten
    that back into a paragraph.

    skip_url drops the lines that only repeat the CTA destination: the button
    already carries it, and a naked URL under its own button reads like a bug.
    """
    html_parts: list[str] = []

    for block in _split_blocks(body):
        lines = [line.strip() for line in block if not _RULE_RE.match(line)]
        lines = [line for line in lines if line]
        if not lines:
            html_parts.append(_rule_html())
            continue

        for kind, run in _runs(lines):
            if kind == "detail":
                pairs = [(m.group(1), m.group(2)) for m in map(_DETAIL_RE.match, run)]
                # One "Label: value" on its own is a sentence, not a table.
                if len(pairs) == 1:
                    if skip_url and pairs[0][1] == skip_url:
                        continue
                    html_parts.append(_paragraph_html(run))
                else:
                    html_parts.append(_details_html(pairs, skip_url=skip_url))
            elif kind == "bullet":
                html_parts.append(_list_html(
                    [m.group(1) for m in map(_BULLET_RE.match, run)]
                ))
            elif kind == "heading":
                html_parts.extend(_heading_html(line) for line in run)
            else:
                if skip_url and run == [skip_url]:
                    continue
                html_parts.append(_paragraph_html(run))

    return "\n".join(part for part in html_parts if part)


# Group consecutive lines of the same shape inside one block.
def _runs(lines: list[str]) -> list[tuple[str, list[str]]]:
    runs: list[tuple[str, list[str]]] = []
    for line in lines:
        kind = _line_kind(line)
        if runs and runs[-1][0] == kind:
            runs[-1][1].append(line)
        else:
            runs.append((kind, [line]))
    return runs


def _line_kind(line: str) -> str:
    # Detail beats heading: "TOTAL: ₦5,000" is a value, not a section title,
    # even though it happens to be written in capitals.
    if _BULLET_RE.match(line):
        return "bullet"
    if _DETAIL_RE.match(line):
        return "detail"
    if _is_heading(line):
        return "heading"
    return "text"


# Group the body's lines into blank-line separated blocks.
def _split_blocks(body: str) -> list[list[str]]:
    blocks: list[list[str]] = []
    current: list[str] = []
    for raw_line in body.replace("\r\n", "\n").split("\n"):
        if raw_line.strip():
            current.append(raw_line)
        elif current:
            blocks.append(current)
            current = []
    if current:
        blocks.append(current)
    return blocks


# Decide whether a lone line is a section heading rather than a sentence.
def _is_heading(line: str) -> bool:
    if len(line) > 60 or line.endswith((".", "?", "!", ",", ":")):
        return False
    letters = [char for char in line if char.isalpha()]
    return len(letters) >= 3 and line == line.upper()


def _paragraph_html(lines: list[str]) -> str:
    text = "<br>".join(_text(line) for line in lines)
    return (
        f'<p style="margin:0 0 16px;color:{_BODY};font-size:15px;line-height:1.65;">'
        f"{text}</p>"
    )


def _heading_html(line: str) -> str:
    return (
        f'<h2 style="margin:24px 0 10px;color:{_INK};font-size:14px;font-weight:700;'
        f'letter-spacing:.6px;text-transform:uppercase;">{_text(line)}</h2>'
    )


def _rule_html() -> str:
    return (
        f'<div style="height:1px;background-color:{_LINE};margin:20px 0;'
        f'font-size:0;line-height:0;">&nbsp;</div>'
    )


def _list_html(items: list[str]) -> str:
    rows = "".join(
        f'<li style="margin:0 0 6px;">{_text(item)}</li>' for item in items
    )
    return (
        f'<ul style="margin:0 0 16px;padding-left:20px;color:{_BODY};'
        f'font-size:15px;line-height:1.6;">{rows}</ul>'
    )


# Render a run of "Label: value" lines as a two-column details table.
def _details_html(pairs: list[tuple[str, str]], skip_url: str = "") -> str:
    rows = []
    for label, value in pairs:
        if skip_url and value.strip() == skip_url:
            continue
        rows.append(
            f'<tr>'
            f'<td style="padding:9px 0;color:{_MUTED};font-size:14px;'
            f'border-bottom:1px solid {_LINE};vertical-align:top;">{_text(label)}</td>'
            f'<td align="right" style="padding:9px 0 9px 20px;color:{_INK};font-size:14px;'
            f'font-weight:600;border-bottom:1px solid {_LINE};vertical-align:top;">'
            f'{_text(value)}</td>'
            f'</tr>'
        )
    if not rows:
        return ""
    return (
        '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" '
        'style="width:100%;border-collapse:collapse;margin:4px 0 20px;">'
        + "".join(rows)
        + "</table>"
    )


def _button_html(label: str, url: str) -> str:
    return (
        '<table role="presentation" cellpadding="0" cellspacing="0" '
        'style="margin:22px 0 4px;border-collapse:collapse;">'
        f'<tr><td style="border-radius:8px;background-color:{_ACCENT};">'
        f'<a href="{_attr(url)}" style="display:inline-block;padding:13px 28px;'
        f'color:#ffffff;font-size:15px;font-weight:600;text-decoration:none;'
        f'border-radius:8px;">{_text(label)}</a>'
        "</td></tr></table>"
        f'<p style="margin:14px 0 0;color:{_FAINT};font-size:12px;line-height:1.5;'
        'word-break:break-all;">Or paste this link into your browser: '
        f'{_text(url)}</p>'
    )


# ---------------------------------------------------------------------------
# Escaping helpers
# ---------------------------------------------------------------------------

# Escape rendered content and linkify any URLs inside it.
def _text(value: str) -> str:
    """
    The single escaping choke point for everything that reaches the HTML shell.

    urlize(autoescape=True) escapes the text first and only then turns bare
    URLs into anchors, so a value like "<script>" is inert while
    "https://..." stays clickable.
    """
    return urlize(value or "", trim_url_limit=None, nofollow=True, autoescape=True)


# Escape a value used inside an HTML attribute (no linkifying).
def _attr(value: str) -> str:
    return (
        (value or "")
        .replace("&", "&amp;")
        .replace('"', "&quot;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


def _clean(value: str) -> str:
    return " ".join((value or "").split())

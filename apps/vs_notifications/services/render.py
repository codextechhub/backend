# =============================================================================
# vs_notifications / services / render.py
#
# Handles rendering of NotificationTemplate bodies and subjects using
# Django's template engine with the caller-supplied context dict.
#
# Called by dispatch.py before writing Notification records.
# Called by the template preview endpoint.
# =============================================================================

from django.template import Context, Template
from django.template.exceptions import TemplateSyntaxError, TemplateDoesNotExist

from ..constants import ChannelChoices
from ..exceptions import InvalidTemplateSyntaxError, TemplateRenderError
from .layout import BRAND_FALLBACK, brand_from_context, compose_email_html


# Validate template text before admins can save it.
def validate_template_syntax(text: str, field: str = "body") -> None:
    """
    Validate that `text` is parseable as a Django template.

    Raises InvalidTemplateSyntaxError if the syntax is invalid.
    Called on NotificationTemplate save (both create and update).

    Args:
        text:   The raw template string to validate.
        field:  Which model field is being validated ("body" or "subject").
                Included in the exception so the API can return a field-level error.
    """
    try:
        Template(text)
    except TemplateSyntaxError as exc:
        raise InvalidTemplateSyntaxError(
            message=f"Template {field} contains invalid syntax: {exc}",
            field=field,
        ) from exc


# Render one template fragment at dispatch or preview time.
def render_template(template_text: str, context: dict, autoescape: bool = False) -> str:
    """
    Render a raw template string with the given context dict.

    Returns the rendered string.
    Raises TemplateRenderError if rendering fails at runtime (e.g. a filter
    applied to a variable raises an exception).

    Unlike validate_template_syntax, this catches runtime errors that only
    surface when the context is applied - not just syntax errors.

    Args:
        template_text:  Raw template string (body or subject).
        context:        Dict of variables to inject. Unknown variables render
                        as empty string (Django default - string_if_invalid="").
        autoescape:     MUST be True whenever the output is HTML. The markup is
                        the template, but the VALUES dropped into it come from
                        the calling module and can carry anything a user typed -
                        a vendor name, a rejection reason, a ticket comment. Off
                        (the default) for subject and plain-text bodies, where
                        escaping would put &amp; in front of a reader.
    """
    try:
        t = Template(template_text)
        return t.render(Context(context, autoescape=autoescape))
    except (TemplateSyntaxError, TemplateDoesNotExist) as exc:
        # Syntax errors that slipped through validation (e.g. dynamic content)
        raise TemplateRenderError(
            message=f"Template rendering failed due to syntax error: {exc}"
        ) from exc
    except Exception as exc:
        raise TemplateRenderError(
            message=f"Template rendering failed: {exc}"
        ) from exc


# Render all stored template fragments for one notification record.
def render_notification_template(notification_template, context: dict) -> tuple[str, str, str]:
    """
    High-level helper used by the dispatch service and the preview endpoint.

    Renders subject, plain body, and the HTML body from a NotificationTemplate
    instance. Returns (subject, body, html_body).

    HTML is produced for the EMAIL channel only, from template.html_body - the
    stored markup, rendered with the recipient's context. That column always
    holds what will be delivered: standard templates have it regenerated from
    the shared layout on save, hand-edited ones keep exactly what an admin
    wrote. What the console shows is therefore what the recipient receives.

    The shared layout is still called here as a SAFETY NET, for a row written by
    a path that bypassed NotificationTemplate.save() (bulk_create, a historical
    data migration) and so has no markup yet.

    In-app records never carry HTML: the feed renders subject/body itself.

    A render failure of any fragment is raised as TemplateRenderError so the
    caller treats the whole record as FAILED - an email that would ship a
    broken alternative is not worth sending.

    Args:
        notification_template:  A NotificationTemplate model instance.
        context:                The caller-supplied context dict.
    """
    # Standard stored HTML carries {{ email_brand }} so tenant or issuer
    # branding remains dynamic instead of being frozen when the row is saved.
    # Copying keeps the caller's dict untouched.
    context = dict(context or {})
    context.setdefault("email_brand", brand_from_context(context) or BRAND_FALLBACK)

    rendered_subject = (
        render_template(notification_template.subject, context)
        if notification_template.subject
        else ""
    )
    rendered_body = render_template(notification_template.body, context)

    if notification_template.channel != ChannelChoices.EMAIL:
        return rendered_subject, rendered_body, ""

    stored_html = getattr(notification_template, "html_body", "")
    if stored_html:
        # autoescape=True: the stored markup is trusted (staff wrote it), the
        # values substituted into it are not.
        return (
            rendered_subject,
            rendered_body,
            render_template(stored_html, context, autoescape=True),
        )

    rendered_html = compose_email_html(
        subject=rendered_subject,
        body=rendered_body,
        cta_label=render_template(
            getattr(notification_template, "cta_label", "") or "", context,
        ),
        cta_url=render_template(
            getattr(notification_template, "cta_url", "") or "", context,
        ),
        brand=brand_from_context(context),
    )
    return rendered_subject, rendered_body, rendered_html

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

from ..exceptions import InvalidTemplateSyntaxError, TemplateRenderError


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


def render_template(template_text: str, context: dict) -> str:
    """
    Render a raw template string with the given context dict.

    Returns the rendered string.
    Raises TemplateRenderError if rendering fails at runtime (e.g. a filter
    applied to a variable raises an exception).

    Unlike validate_template_syntax, this catches runtime errors that only
    surface when the context is applied — not just syntax errors.

    Args:
        template_text:  Raw template string (body or subject).
        context:        Dict of variables to inject. Unknown variables render
                        as empty string (Django default — string_if_invalid="").
    """
    try:
        t = Template(template_text)
        return t.render(Context(context, autoescape=False))
    except (TemplateSyntaxError, TemplateDoesNotExist) as exc:
        # Syntax errors that slipped through validation (e.g. dynamic content)
        raise TemplateRenderError(
            message=f"Template rendering failed due to syntax error: {exc}"
        ) from exc
    except Exception as exc:
        raise TemplateRenderError(
            message=f"Template rendering failed: {exc}"
        ) from exc


def render_notification_template(notification_template, context: dict) -> tuple[str, str]:
    """
    High-level helper used by the dispatch service.

    Renders both subject and body from a NotificationTemplate instance.
    Returns (rendered_subject, rendered_body).

    If subject is empty (e.g. for in-app templates), it is returned as "".

    Args:
        notification_template:  A NotificationTemplate model instance.
        context:                The caller-supplied context dict.
    """
    rendered_subject = (
        render_template(notification_template.subject, context)
        if notification_template.subject
        else ""
    )
    rendered_body = render_template(notification_template.body, context)
    return rendered_subject, rendered_body

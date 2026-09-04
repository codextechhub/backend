"""Refresh the six standard onboarding email templates.

Only platform-maintained templates are changed. Staff-authored subject lines,
messages, actions, and HTML remain exactly as written.
"""

from django.db import migrations


TEMPLATES = {
    "onboarding.step_completed": {
        "subject": (
            "{{ school_name }} onboarding: step {{ step_number }} of "
            "{{ total_steps }} complete"
        ),
        "body": (
            "{{ completed_by_name }} completed an onboarding step for "
            "{{ school_name }}.\n\n"
            "PROGRESS DETAILS\n"
            "Completed item: {{ step_name }}\n"
            "Progress: Step {{ step_number }} of {{ total_steps }}\n"
            "Completed by: {{ completed_by_name }}\n\n"
            "Open onboarding to review the checklist and continue with the "
            "remaining steps."
        ),
    },
    "onboarding.go_live_ready": {
        "subject": "{{ school_name }} is ready to request go-live",
        "body": (
            "All required onboarding steps are complete for {{ school_name }}.\n\n"
            "READINESS DETAILS\n"
            "Workspace: {{ school_name }}\n"
            "Status: Ready for review\n"
            "Completed by: {{ completed_by_name }}\n\n"
            "Your school can now submit a go-live request. Open onboarding, "
            "confirm your preferred go-live date, acknowledge the go-live terms, "
            "and submit the request for platform review."
        ),
    },
    "onboarding.go_live_reviewed": {
        "subject": (
            "{% if decision == 'rejected' %}Action needed: {{ school_name }} "
            "go-live request was not approved{% else %}{{ school_name }} "
            "go-live request approved{% endif %}"
        ),
        "body": (
            "Your go-live request for {{ school_name }} was reviewed.\n\n"
            "DECISION DETAILS\n"
            "Decision: {{ decision }}\n"
            "Reviewed by: {{ reviewed_by_name }}\n"
            "Reviewed at: {{ reviewed_at_display }}\n"
            "{% if rejection_reason %}Reason: {{ rejection_reason }}\n{% endif %}"
            "\n{% if decision == 'rejected' %}Return to onboarding, address the "
            "reason above, and submit a new request when you are ready."
            "{% else %}The request was approved. Your activation confirmation "
            "shows when the workspace became live.{% endif %}"
        ),
    },
    "onboarding.activated": {
        "subject": "{{ school_name }} is live on CodeX Vision",
        "body": (
            "Onboarding is complete for {{ school_name }}.\n\n"
            "ACTIVATION DETAILS\n"
            "Workspace: {{ school_name }}\n"
            "Activated at: {{ go_live_at_display }}\n"
            "Status: Live\n\n"
            "Your team can now sign in and use the areas enabled for your school "
            "and permitted by their roles."
        ),
    },
    "onboarding.expiry_warning": {
        "subject": (
            "Action required by {{ expires_on_display }}: finish {{ school_name }} "
            "onboarding"
        ),
        "body": (
            "Your onboarding window for {{ school_name }} is close to expiring.\n\n"
            "DEADLINE DETAILS\n"
            "Workspace: {{ school_name }}\n"
            "Deadline: {{ expires_on_display }}\n"
            "Time remaining: {{ days_remaining }} "
            "{% if days_remaining == 1 %}day{% else %}days{% endif %}\n"
            "Open for: {{ pending_days }} days\n\n"
            "Finish every required onboarding step and submit your go-live request "
            "before the deadline. If the deadline passes, the school is suspended "
            "and its users cannot sign in until platform staff restore it."
        ),
    },
    "onboarding.stale_report": {
        "subject": (
            "Onboarding follow-up: {{ ageing_count }} ageing, "
            "{{ expired_count }} suspended"
        ),
        "body": (
            "This report shows schools that need platform follow-up.\n\n"
            "REPORT SUMMARY\n"
            "Ageing schools: {{ ageing_count }}\n"
            "Recently suspended: {{ expired_count }}\n"
            "Ageing threshold: {{ stale_after_days }} days\n"
            "Report window: {{ window_days }} days\n\n"
            "SCHOOLS STILL ONBOARDING\n"
            "{{ ageing_list }}\n\n"
            "RECENTLY SUSPENDED\n"
            "{{ expired_list }}\n\n"
            "A suspended school can be returned to onboarding, which gives it "
            "a fresh {{ expiry_days }} days."
        ),
    },
}


def refine_standard_onboarding_emails(apps, schema_editor):
    Template = apps.get_model("vs_notifications", "NotificationTemplate")
    from vs_notifications.services.layout import (
        EMAIL_BRAND_LOGO_PLACEHOLDER,
        EMAIL_BRAND_PLACEHOLDER,
        compose_email_html,
    )

    for event_key, definition in TEMPLATES.items():
        template = Template.objects.filter(
            event_type__key=event_key,
            channel="email",
            html_is_custom=False,
        ).first()
        if template is None:
            continue

        template.subject = definition["subject"]
        template.body = definition["body"]
        template.cta_label = ""
        template.cta_url = ""
        template.html_body = compose_email_html(
            subject=definition["subject"],
            body=definition["body"],
            brand=EMAIL_BRAND_PLACEHOLDER,
            brand_logo_url=EMAIL_BRAND_LOGO_PLACEHOLDER,
            as_template=True,
        )
        template.save(
            update_fields=[
                "subject",
                "body",
                "cta_label",
                "cta_url",
                "html_body",
            ]
        )


def noop(apps, schema_editor):
    """The previous platform-maintained copy is not user-authored data."""


class Migration(migrations.Migration):
    dependencies = [
        ("vs_notifications", "0015_school_logo_in_the_email_header"),
    ]

    operations = [
        migrations.RunPython(refine_standard_onboarding_emails, noop),
    ]

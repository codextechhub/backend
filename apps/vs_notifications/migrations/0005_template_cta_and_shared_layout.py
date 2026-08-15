# =============================================================================
# vs_notifications / 0005_template_cta_and_shared_layout
#
# Moves email templates onto the shared HTML shell (services/layout.py).
#
# Schema: a template gains cta_label + cta_url, so "the message has a button"
# stops being something an author expresses by hand-writing a <table> of
# markup.
#
# Data: the five platform-authored html_body blobs (invitation, password
# reset, credit note, debit note, vendor purchase order) are cleared, because
# the shell now renders those emails from their plain bodies and their new CTA.
# Only these known keys are touched - any other template carrying custom HTML
# keeps it and keeps overriding the shell.
#
# Reverse re-adds nothing: the previous inline markup lives in migration 0004
# and in git history, and re-instating it would undo the point of the change.
# =============================================================================

from django.db import migrations, models


# (event key, channel) -> (cta_label, cta_url)
TEMPLATE_CTAS = {
    ("user.invited", "email"): ("Activate your account", "{{ invitation_url }}"),
    ("user.password_reset", "email"): ("Reset your password", "{{ reset_url }}"),
    ("user.account_locked", "email"): (
        "Unlock your account", "{{ unlock_instructions_link }}",
    ),
    ("billing.invoice_issued", "email"): ("Pay online", "{{ payment_link }}"),
    ("procurement.rfq_invitation", "email"): (
        "Open the quotation form", "{{ invitation_url }}",
    ),
    ("procurement.rfq_reminder", "email"): (
        "Submit your quotation", "{{ invitation_url }}",
    ),
    ("procurement.quotation_receipt", "email"): (
        "View your receipt", "{{ invitation_url }}",
    ),
    ("procurement.rfq_amended", "email"): (
        "Review the amendment", "{{ invitation_url }}",
    ),
    ("procurement.rfq_deadline_extended", "email"): (
        "Open the quotation form", "{{ invitation_url }}",
    ),
}

# Templates whose html_body was written by the platform, not by staff.
PLATFORM_AUTHORED_HTML = [
    "user.invited",
    "user.password_reset",
    "billing.credit_note_issued",
    "billing.debit_note_issued",
    "procurement.purchase_order_issued",
]


def adopt_shared_layout(apps, schema_editor):
    Template = apps.get_model("vs_notifications", "NotificationTemplate")

    for (event_key, channel), (label, url) in TEMPLATE_CTAS.items():
        Template.objects.filter(
            event_type__key=event_key, channel=channel, cta_url="",
        ).update(cta_label=label, cta_url=url)

    Template.objects.filter(
        event_type__key__in=PLATFORM_AUTHORED_HTML, channel="email",
    ).exclude(html_body="").update(html_body="")


def noop(apps, schema_editor):
    """Reverse is a no-op - see the module docstring."""


class Migration(migrations.Migration):
    dependencies = [
        ("vs_notifications", "0004_purchase_order_vendor_email"),
        # The RFQ templates are seeded by a vs_procurement migration; depend on
        # it so this data step cannot run first and miss them on a fresh install.
        ("vs_procurement", "0020_requestforquotation_response_due_at_and_more"),
    ]

    operations = [
        migrations.AddField(
            model_name="notificationtemplate",
            name="cta_label",
            field=models.CharField(
                blank=True,
                default="",
                help_text="Button text for the email's call to action, e.g. 'Activate your account'. Only used when cta_url is set. Supports {{ variable }}.",
                max_length=80,
            ),
        ),
        migrations.AddField(
            model_name="notificationtemplate",
            name="cta_url",
            field=models.CharField(
                blank=True,
                default="",
                help_text="Button destination, normally a single {{ variable }} such as {{ invitation_url }}. Empty means the email carries no button. Non-http(s) values are dropped at render time.",
                max_length=500,
            ),
        ),
        migrations.AlterField(
            model_name="notificationtemplate",
            name="html_body",
            field=models.TextField(
                blank=True,
                default="",
                help_text="Escape hatch: bespoke HTML for the email channel. Leave EMPTY for the standard layout, which is what almost every template should use. When set it replaces the shared shell completely, so the message stops inheriting future design changes. Ignored for in-app.",
            ),
        ),
        migrations.RunPython(adopt_shared_layout, noop),
    ]

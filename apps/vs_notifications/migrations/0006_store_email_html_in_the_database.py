# =============================================================================
# vs_notifications / 0006_store_email_html_in_the_database
#
# Moves the email markup INTO the database.
#
# 0005 took hand-written HTML out of every template and had the shared layout
# compose it at send time. That bought one consistent design and cost the thing
# an administrator actually needs: somewhere to see the markup and change it.
# This migration puts it back, without giving up the consistency:
#
#   * every active email template gets its html_body filled with the standard
#     layout, {{ placeholders }} intact, so it is real, readable, editable HTML;
#   * html_is_custom says who maintains it. False means the row is regenerated
#     from the layout on every save (see NotificationTemplate.save), so an
#     untouched template still follows the platform design. True means someone
#     edited the markup and it is preserved verbatim.
#
# Reverse empties the column again: the render path falls back to composing at
# send time, which is exactly the 0005 behaviour.
# =============================================================================

from django.db import migrations, models


def _compose(template):
    """Build the standard document for one template row (historical-model safe)."""
    from vs_notifications.services.layout import compose_email_html

    return compose_email_html(
        subject=template.subject,
        body=template.body,
        cta_label=template.cta_label,
        cta_url=template.cta_url,
        as_template=True,
    )


def store_email_html(apps, schema_editor):
    Template = apps.get_model("vs_notifications", "NotificationTemplate")

    # Historical models have no save() override, so the markup is written here
    # explicitly. Only email rows carry markup; in-app renders subject/body.
    updates = []
    for template in Template.objects.filter(channel="email").iterator():
        if template.html_body:
            # Markup that survived 0005 was written by hand - keep it, and say so.
            template.html_is_custom = True
        else:
            template.html_body = _compose(template)
            template.html_is_custom = False
        updates.append(template)

    if updates:
        Template.objects.bulk_update(updates, ["html_body", "html_is_custom"], batch_size=50)

    Template.objects.filter(channel="in_app").exclude(html_body="").update(html_body="")


def clear_email_html(apps, schema_editor):
    """Back to composing at send time (the 0005 behaviour)."""
    Template = apps.get_model("vs_notifications", "NotificationTemplate")
    Template.objects.filter(channel="email", html_is_custom=False).update(html_body="")


class Migration(migrations.Migration):
    dependencies = [
        ("vs_notifications", "0005_template_cta_and_shared_layout"),
    ]

    operations = [
        migrations.AddField(
            model_name="notificationtemplate",
            name="html_is_custom",
            field=models.BooleanField(
                default=False,
                help_text=(
                    "False: html_body is maintained by the platform layout and refreshed "
                    "whenever the message changes. True: someone edited the markup by hand, "
                    "so it is preserved verbatim and no longer inherits design changes. "
                    "Clear it to restore the standard design."
                ),
            ),
        ),
        migrations.AlterField(
            model_name="notificationtemplate",
            name="html_body",
            field=models.TextField(
                blank=True,
                default="",
                help_text=(
                    "The email HTML exactly as it will be delivered, with {{ variable }} "
                    "placeholders still in it. Regenerated from the shared layout on every "
                    "save while html_is_custom is False. Unused for in-app."
                ),
            ),
        ),
        migrations.RunPython(store_email_html, clear_email_html),
    ]

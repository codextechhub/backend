"""Escalation, and what every existing ticket meant before it existed.

Adding the column is the easy half. The hard half is that the platform desk is
about to list escalated tickets only, and every ticket already in the table was
filed by a school straight to CodeX - that was the only thing the support form
could do. Left null they would all read as "still the school's problem" and
vanish from the desk that has been working them.

So they are backfilled as escalated at the moment they were created, by whoever
raised them, which is what they have always meant. Reversible: unsetting the
columns puts the table back exactly as it was.
"""

import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models
from django.db.models import F


def mark_existing_tickets_escalated(apps, schema_editor):
    """Every pre-existing ticket was addressed to CodeX. Say so explicitly."""
    Ticket = apps.get_model("vs_tickets", "Ticket")
    # Platform-tenant tickets are CodeX's own and were never escalated to
    # anybody; the desk sees them because they belong to it, not because they
    # were sent up.
    (
        Ticket.objects.filter(escalated_at__isnull=True)
        .exclude(tenant__kind="PLATFORM")
        .update(escalated_at=F("created_at"), escalated_by=F("requester"))
    )


def unmark(apps, schema_editor):
    Ticket = apps.get_model("vs_tickets", "Ticket")
    Ticket.objects.update(escalated_at=None, escalated_by=None)


class Migration(migrations.Migration):
    dependencies = [
        ("vs_tenants", "0008_alter_branch_name"),
        ("vs_tickets", "0007_guideanalyticsevent"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.AddField(
            model_name="ticket",
            name="escalated_at",
            field=models.DateTimeField(blank=True, db_index=True, null=True),
        ),
        migrations.AddField(
            model_name="ticket",
            name="escalated_by",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="escalated_tickets",
                to=settings.AUTH_USER_MODEL,
            ),
        ),
        migrations.AddIndex(
            model_name="ticket",
            index=models.Index(
                fields=["escalated_at", "status"], name="vs_tickets__escalat_66a3ed_idx"
            ),
        ),
        migrations.RunPython(mark_existing_tickets_escalated, unmark),
    ]

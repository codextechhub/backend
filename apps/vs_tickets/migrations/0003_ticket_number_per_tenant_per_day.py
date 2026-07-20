"""Renumber tickets to <SLUG>-CX<YYMMDD><n>, counted per tenant per day.

The old ``TicketSequence`` was a single global daily counter (behind the
``TCK-YYYYMMDD-NNNN`` format). The new scheme counts per (tenant, day), so the
counters are reset: existing rows are stale and dropped. Already-issued ticket
numbers live on ``Ticket.ticket_number`` and are left untouched — only new
tickets use the new format.
"""
import django.db.models.deletion
from django.db import migrations, models


def clear_sequences(apps, schema_editor):
    # Old global counters no longer apply; per-tenant rows are created on demand.
    apps.get_model("vs_tickets", "TicketSequence").objects.all().delete()


class Migration(migrations.Migration):

    dependencies = [
        ("vs_tenants", "0001_initial"),
        ("vs_tickets", "0002_initial"),
    ]

    operations = [
        # Widen the number column for <SLUG>-CX<YYMMDD><n> (slugs up to 80 chars).
        migrations.AlterField(
            model_name="ticket",
            name="ticket_number",
            field=models.CharField(editable=False, max_length=100, unique=True),
        ),
        # Reset the counters, then re-key them by (tenant, day). The table is
        # emptied first so the new non-null FK is added to an empty table
        # (added nullable, then tightened) with no backfill default needed.
        migrations.RunPython(clear_sequences, migrations.RunPython.noop),
        migrations.AlterField(
            model_name="ticketsequence",
            name="date",
            field=models.DateField(),
        ),
        migrations.AddField(
            model_name="ticketsequence",
            name="tenant",
            field=models.ForeignKey(
                null=True,
                on_delete=django.db.models.deletion.CASCADE,
                related_name="ticket_sequences",
                to="vs_tenants.tenant",
            ),
        ),
        migrations.AlterField(
            model_name="ticketsequence",
            name="tenant",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.CASCADE,
                related_name="ticket_sequences",
                to="vs_tenants.tenant",
            ),
        ),
        migrations.AlterUniqueTogether(
            name="ticketsequence",
            unique_together={("tenant", "date")},
        ),
    ]

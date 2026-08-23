from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion
import django.utils.timezone


def subscribe_existing_commenters(apps, schema_editor):
    TicketComment = apps.get_model("vs_tickets", "TicketComment")
    TicketSubscription = apps.get_model("vs_tickets", "TicketSubscription")
    pairs = (
        TicketComment.objects.order_by()
        .values_list("ticket_id", "author_id")
        .distinct()
    )
    batch = []
    for ticket_id, author_id in pairs.iterator(chunk_size=2000):
        batch.append(TicketSubscription(
            ticket_id=ticket_id,
            user_id=author_id,
            source="COMMENTED",
        ))
        if len(batch) == 2000:
            TicketSubscription.objects.bulk_create(batch, ignore_conflicts=True)
            batch = []
    if batch:
        TicketSubscription.objects.bulk_create(batch, ignore_conflicts=True)


class Migration(migrations.Migration):

    dependencies = [
        ("vs_tickets", "0005_retarget_branch_to_vs_tenants"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="TicketSubscription",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                (
                    "created_at",
                    models.DateTimeField(
                        default=django.utils.timezone.now,
                        editable=False,
                    ),
                ),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "source",
                    models.CharField(
                        choices=[("COMMENTED", "Commented"), ("MANUAL", "Manual")],
                        default="MANUAL",
                        max_length=20,
                    ),
                ),
                ("muted_at", models.DateTimeField(blank=True, null=True)),
                (
                    "ticket",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="subscriptions",
                        to="vs_tickets.ticket",
                    ),
                ),
                (
                    "user",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="ticket_subscriptions",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
            ],
            options={"db_table": "vs_tickets_subscription"},
        ),
        migrations.AddConstraint(
            model_name="ticketsubscription",
            constraint=models.UniqueConstraint(
                fields=("ticket", "user"),
                name="unique_ticket_subscription_user",
            ),
        ),
        migrations.AddIndex(
            model_name="ticketsubscription",
            index=models.Index(
                fields=["ticket", "muted_at"],
                name="vs_tickets__ticket__8497a0_idx",
            ),
        ),
        migrations.AddIndex(
            model_name="ticketsubscription",
            index=models.Index(
                fields=["user", "muted_at"],
                name="vs_tickets__user_id_d9dfa2_idx",
            ),
        ),
        migrations.RunPython(
            subscribe_existing_commenters,
            migrations.RunPython.noop,
        ),
    ]

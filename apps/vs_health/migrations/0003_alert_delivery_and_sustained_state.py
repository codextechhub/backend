import django.db.models
import vs_health.models.incidents
from django.db import migrations, models
from django.utils import timezone


DELIVERY_CHANNEL = "email_and_in_app"


def prepare_alert_state(apps, schema_editor):
    """Normalize delivery routing and collapse legacy duplicate firing rows."""
    AlertRule = apps.get_model("vs_health", "AlertRule")
    Alert = apps.get_model("vs_health", "Alert")
    Incident = apps.get_model("vs_health", "Incident")

    AlertRule.objects.exclude(channel=DELIVERY_CHANNEL).update(
        channel=DELIVERY_CHANNEL,
    )

    duplicate_rule_ids = (
        Alert.objects.filter(status="firing")
        .values("rule_id")
        .annotate(total=models.Count("id"))
        .filter(total__gt=1)
        .values_list("rule_id", flat=True)
    )
    resolved_at = timezone.now()
    for rule_id in duplicate_rule_ids.iterator():
        firing_ids = list(
            Alert.objects.filter(rule_id=rule_id, status="firing")
            .order_by("-fired_at")
            .values_list("id", flat=True)
        )
        stale_alerts = Alert.objects.filter(id__in=firing_ids[1:])
        stale_incident_ids = list(
            stale_alerts.exclude(incident_id=None)
            .values_list("incident_id", flat=True)
        )
        stale_alerts.update(
            status="resolved",
            resolved_at=resolved_at,
        )
        for incident_id in stale_incident_ids:
            still_firing = Alert.objects.filter(
                incident_id=incident_id,
                status="firing",
            ).exists()
            if not still_firing:
                Incident.objects.filter(
                    id=incident_id,
                    source="auto",
                ).exclude(status="resolved").update(
                    status="resolved",
                    resolved_at=resolved_at,
                    updated_at=resolved_at,
                )


class Migration(migrations.Migration):
    dependencies = [
        ("vs_health", "0002_initial"),
    ]

    operations = [
        migrations.AddField(
            model_name="alertrule",
            name="breach_started_at",
            field=models.DateTimeField(
                blank=True,
                help_text="First consecutive breaching evaluation in the current run.",
                null=True,
            ),
        ),
        migrations.RunPython(prepare_alert_state, migrations.RunPython.noop),
        migrations.AlterField(
            model_name="alertrule",
            name="channel",
            field=models.CharField(
                choices=[("email_and_in_app", "Email and in-app")],
                default="email_and_in_app",
                help_text="Destinations used when this rule starts firing.",
                max_length=20,
            ),
        ),
        migrations.AlterField(
            model_name="incident",
            name="code",
            field=models.CharField(
                default=vs_health.models.incidents.generate_incident_code,
                help_text="UUID-backed human reference, e.g. 'INC-7B92D0B515F24D1C'.",
                max_length=20,
                unique=True,
            ),
        ),
        migrations.AddConstraint(
            model_name="alert",
            constraint=models.UniqueConstraint(
                condition=django.db.models.Q(("status", "firing")),
                fields=("rule",),
                name="uq_health_rule_firing_alert",
            ),
        ),
    ]

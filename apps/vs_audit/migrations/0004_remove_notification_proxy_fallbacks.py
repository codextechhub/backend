from django.db import migrations
from django.db.models import Count, Max, Min


TECHNICAL_NOTIFICATION_PATHS = (
    "/v1/notify/mark-read/",
    "/v1/notify/mark-all-read/",
    "/v1/notify/acknowledge-route/",
)


def remove_notification_proxy_fallbacks(apps, schema_editor):
    AuditEvent = apps.get_model("vs_audit", "AuditEvent")
    EntityAuditTrail = apps.get_model("vs_audit", "EntityAuditTrail")

    noisy_events = AuditEvent.objects.filter(
        action_type="PROXY_CHANGE",
        metadata__path__in=TECHNICAL_NOTIFICATION_PATHS,
    )
    affected_entities = list(
        noisy_events.values_list("entity_type", "entity_id").distinct()
    )
    noisy_events.delete()

    # EntityAuditTrail is a cached rollup, so keep it consistent after the
    # targeted data cleanup instead of leaving inflated action counts behind.
    for entity_type, entity_id in affected_entities:
        aggregate = AuditEvent.objects.filter(
            entity_type=entity_type,
            entity_id=entity_id,
        ).aggregate(
            event_count=Count("id"),
            first_event_at=Min("event_at"),
            last_event_at=Max("event_at"),
        )
        trail = EntityAuditTrail.objects.filter(
            entity_type=entity_type,
            entity_id=entity_id,
        )
        if aggregate["event_count"]:
            trail.update(**aggregate)
        else:
            trail.delete()


class Migration(migrations.Migration):
    dependencies = [
        ("vs_audit", "0003_remove_impersonated_request_history"),
    ]

    operations = [
        migrations.RunPython(
            remove_notification_proxy_fallbacks,
            reverse_code=migrations.RunPython.noop,
        ),
    ]

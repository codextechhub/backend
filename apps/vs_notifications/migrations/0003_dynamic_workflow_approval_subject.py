from django.db import migrations


DYNAMIC_SUBJECT = "{{ document_type_title }} Approved"


def _historical_subjects(apps, *, blank_only):
    Notification = apps.get_model("vs_notifications", "Notification")
    WorkflowInstance = apps.get_model("vs_workflow", "WorkflowInstance")

    queryset = Notification._base_manager.filter(
        event_type__key="workflow.final_approved",
        channel="in_app",
    )
    if blank_only:
        queryset = queryset.filter(subject="")
    notifications = list(queryset)
    instance_ids = {
        str(notification.metadata.get("workflow_instance_id"))
        for notification in notifications
        if isinstance(notification.metadata, dict)
        and notification.metadata.get("workflow_instance_id")
    }
    instances = {
        str(instance.id): instance
        for instance in WorkflowInstance._base_manager.filter(id__in=instance_ids)
    }

    updates = []
    for notification in notifications:
        metadata = notification.metadata if isinstance(notification.metadata, dict) else {}
        instance = instances.get(str(metadata.get("workflow_instance_id")))
        if instance is None:
            continue
        summary = instance.document_summary if isinstance(instance.document_summary, dict) else {}
        document_type = str(summary.get("subtitle") or "").strip()
        if not document_type:
            document_type = instance.document_type.replace("_", " ").title()
        document_type_title = " ".join(
            word if word.isupper() else word.capitalize()
            for word in document_type.split()
        )
        expected_subject = f"{document_type_title} Approved"
        if blank_only:
            notification.subject = expected_subject
            updates.append(notification)
        elif notification.subject == expected_subject:
            updates.append(notification)
    return Notification, updates


def add_dynamic_approval_subject(apps, schema_editor):
    NotificationTemplate = apps.get_model("vs_notifications", "NotificationTemplate")
    NotificationTemplate.objects.filter(
        event_type__key="workflow.final_approved",
        channel="in_app",
        subject="",
    ).update(subject=DYNAMIC_SUBJECT)
    Notification, updates = _historical_subjects(apps, blank_only=True)
    if updates:
        Notification._base_manager.bulk_update(updates, ["subject"], batch_size=500)


def remove_dynamic_approval_subject(apps, schema_editor):
    NotificationTemplate = apps.get_model("vs_notifications", "NotificationTemplate")
    NotificationTemplate.objects.filter(
        event_type__key="workflow.final_approved",
        channel="in_app",
        subject=DYNAMIC_SUBJECT,
    ).update(subject="")
    Notification, updates = _historical_subjects(apps, blank_only=False)
    generated_ids = [notification.id for notification in updates]
    if generated_ids:
        Notification._base_manager.filter(id__in=generated_ids).update(subject="")


class Migration(migrations.Migration):
    dependencies = [
        ("vs_notifications", "0002_initial"),
        ("vs_workflow", "0002_seed_platform_user_creation_template"),
    ]

    operations = [
        migrations.RunPython(
            add_dynamic_approval_subject,
            remove_dynamic_approval_subject,
        ),
    ]

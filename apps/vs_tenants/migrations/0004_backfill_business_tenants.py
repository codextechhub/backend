from django.db import migrations


def forwards(apps, schema_editor):
    Tenant = apps.get_model("vs_tenants", "Tenant")
    codex = Tenant.objects.get(slug="codex", kind="PLATFORM")

    BackgroundJob = apps.get_model("core", "BackgroundJob")
    for row in BackgroundJob.objects.select_related("owner", "school").all().iterator():
        tenant_id = getattr(row.owner, "tenant_id", None) or getattr(row.school, "tenant_id", None) or codex.pk
        BackgroundJob.objects.filter(pk=row.pk).update(tenant_id=tenant_id)

    Ticket = apps.get_model("vs_tickets", "Ticket")
    for row in Ticket.objects.select_related("requester").all().iterator():
        Ticket.objects.filter(pk=row.pk).update(tenant_id=row.requester.tenant_id)

    ImportBatch = apps.get_model("vs_import_data", "ImportBatch")
    for row in ImportBatch.objects.select_related("uploaded_by", "school").all().iterator():
        tenant_id = getattr(row.school, "tenant_id", None) or row.uploaded_by.tenant_id
        ImportBatch.objects.filter(pk=row.pk).update(tenant_id=tenant_id)

    Template = apps.get_model("vs_workflow", "WorkflowTemplate")
    for row in Template.objects.select_related("school").all().iterator():
        Template.objects.filter(pk=row.pk).update(tenant_id=getattr(row.school, "tenant_id", None))
    Instance = apps.get_model("vs_workflow", "WorkflowInstance")
    for row in Instance.objects.select_related("school", "requested_by").all().iterator():
        tenant_id = getattr(row.school, "tenant_id", None) or row.requested_by.tenant_id
        Instance.objects.filter(pk=row.pk).update(tenant_id=tenant_id)
    Delegation = apps.get_model("vs_workflow", "ApprovalDelegation")
    for row in Delegation.objects.select_related("school", "delegator").all().iterator():
        tenant_id = getattr(row.school, "tenant_id", None) or row.delegator.tenant_id
        Delegation.objects.filter(pk=row.pk).update(tenant_id=tenant_id)

    Setting = apps.get_model("vs_notifications", "NotificationSetting")
    for row in Setting.objects.select_related("school").all().iterator():
        Setting.objects.filter(pk=row.pk).update(tenant_id=getattr(row.school, "tenant_id", None))
    Notification = apps.get_model("vs_notifications", "Notification")
    for row in Notification.objects.select_related("school", "recipient").all().iterator():
        tenant_id = getattr(row.school, "tenant_id", None) or getattr(row.recipient, "tenant_id", None) or codex.pk
        Notification.objects.filter(pk=row.pk).update(tenant_id=tenant_id)

    Rule = apps.get_model("vs_audit", "ComplianceRule")
    for row in Rule.objects.select_related("school").all().iterator():
        Rule.objects.filter(pk=row.pk).update(tenant_id=getattr(row.school, "tenant_id", None))


def backwards(apps, schema_editor):
    for app_label, model_name in (
        ("core", "BackgroundJob"), ("vs_tickets", "Ticket"),
        ("vs_import_data", "ImportBatch"), ("vs_workflow", "WorkflowTemplate"),
        ("vs_workflow", "WorkflowInstance"), ("vs_workflow", "ApprovalDelegation"),
        ("vs_notifications", "NotificationSetting"), ("vs_notifications", "Notification"),
        ("vs_audit", "ComplianceRule"),
    ):
        apps.get_model(app_label, model_name).objects.update(tenant=None)


class Migration(migrations.Migration):
    dependencies = [
        ("vs_tenants", "0003_rename_vs_tenants_kind_status_idx_vs_tenants__kind_580eca_idx"),
        ("core", "0004_backgroundjob_tenant"),
        ("vs_tickets", "0003_ticket_tenant"),
        ("vs_import_data", "0003_importbatch_tenant"),
        ("vs_workflow", "0002_approvaldelegation_tenant_workflowinstance_tenant_and_more"),
        ("vs_notifications", "0005_notification_tenant_notificationsetting_tenant"),
        ("vs_audit", "0004_compliancerule_tenant"),
    ]
    operations = [migrations.RunPython(forwards, backwards)]

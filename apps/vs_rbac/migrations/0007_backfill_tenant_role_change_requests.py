"""Backfill TenantRoleChangeRequest / TenantRoleChangeDeltaItem from the legacy
School and Platform role-change-request tables.

Idempotent (update_or_create keyed on stable natural fields) and reversible
(the backwards pass drops only the backfilled tenant rows). Legacy rows whose
school has no tenant are skipped defensively, mirroring migration 0004.
"""
from django.db import migrations


def _resolve_role(Role, tenant_id, legacy_role_id):
    return Role.objects.filter(tenant_id=tenant_id, key=str(legacy_role_id)).first()


def forwards(apps, schema_editor):
    Tenant = apps.get_model("vs_tenants", "Tenant")
    Role = apps.get_model("vs_rbac", "TenantRoleTemplate")
    SchoolRCR = apps.get_model("vs_rbac", "SchoolRoleChangeRequest")
    SchoolDelta = apps.get_model("vs_rbac", "SchoolRoleChangeDeltaItem")
    PlatformRCR = apps.get_model("vs_rbac", "PlatformRoleChangeRequest")
    PlatformDelta = apps.get_model("vs_rbac", "PlatformRoleChangeDeltaItem")
    TenantRCR = apps.get_model("vs_rbac", "TenantRoleChangeRequest")
    TenantDelta = apps.get_model("vs_rbac", "TenantRoleChangeDeltaItem")

    codex = Tenant.objects.filter(slug="codex", kind="PLATFORM").first()

    # ── School role change requests ────────────────────────────────────────
    for old in SchoolRCR.objects.select_related("school").all().iterator():
        if not old.school_id or not old.school.tenant_id:
            continue
        tenant_id = old.school.tenant_id
        role = _resolve_role(Role, tenant_id, old.target_role_id)
        if role is None:
            continue

        new_req, _ = TenantRCR.objects.update_or_create(
            tenant_id=tenant_id,
            target_role=role,
            requested_by_id=old.requested_by_id,
            submitted_at=old.submitted_at,
            defaults={
                "status": old.status,
                "justification": old.justification,
                "reviewer_id": old.reviewer_id,
                "reviewer_notes": old.reviewer_notes,
                "decided_at": old.decided_at,
                "impact_summary": old.impact_summary,
                "created_at": old.created_at,
            },
        )
        for d in SchoolDelta.objects.filter(request_id=old.pk).iterator():
            TenantDelta.objects.update_or_create(
                request=new_req,
                permission_id=d.permission_id,
                operation=d.operation,
            )

    # ── Platform role change requests ──────────────────────────────────────
    if codex is not None:
        for old in PlatformRCR.objects.all().iterator():
            role = _resolve_role(Role, codex.pk, old.target_role_id)
            if role is None:
                continue

            new_req, _ = TenantRCR.objects.update_or_create(
                tenant_id=codex.pk,
                target_role=role,
                requested_by_id=old.requested_by_id,
                submitted_at=old.submitted_at,
                defaults={
                    "status": old.status,
                    "justification": old.justification,
                    "reviewer_id": old.reviewer_id,
                    "reviewer_notes": old.reviewer_notes,
                    "decided_at": old.decided_at,
                    "impact_summary": old.impact_summary,
                    "created_at": old.created_at,
                },
            )
            for d in PlatformDelta.objects.filter(request_id=old.pk).iterator():
                TenantDelta.objects.update_or_create(
                    request=new_req,
                    permission_id=d.permission_id,
                    operation=d.operation,
                )


def backwards(apps, schema_editor):
    apps.get_model("vs_rbac", "TenantRoleChangeDeltaItem").objects.all().delete()
    apps.get_model("vs_rbac", "TenantRoleChangeRequest").objects.all().delete()


class Migration(migrations.Migration):
    dependencies = [
        ("vs_rbac", "0006_tenantrolechangerequest_tenantrolechangedeltaitem_and_more"),
    ]
    operations = [migrations.RunPython(forwards, backwards)]

"""Cut the session/security logs and Position.default_role over to the tenant.

Follows the per-app tenant cutover precedent:
  AddField tenant/default_role_tenant (nullable) → RunPython backfill
  (idempotent + reversible) → RemoveField the legacy school / PlatformRole FK.

  * LoginSession.school     → LoginSession.tenant  (from school.tenant)
  * AuthAttempt.school      → AuthAttempt.tenant   (from school.tenant)
  * AuthEventLog.school     → AuthEventLog.tenant  (from school.tenant)
  * Position.default_role   → FK(TenantRoleTemplate) resolved by
                              key=str(old_platform_role_pk) on the codex tenant
                              (the mapping vs_rbac.0004 backfill established).
"""
from django.db import migrations, models
import django.db.models.deletion


def forwards(apps, schema_editor):
    LoginSession = apps.get_model("vs_user", "LoginSession")
    AuthAttempt = apps.get_model("vs_user", "AuthAttempt")
    AuthEventLog = apps.get_model("vs_user", "AuthEventLog")
    Position = apps.get_model("vs_user", "Position")
    Tenant = apps.get_model("vs_tenants", "Tenant")
    Role = apps.get_model("vs_rbac", "TenantRoleTemplate")

    # ── Session / security logs: school → school.tenant ──────────────────────
    for model in (LoginSession, AuthAttempt, AuthEventLog):
        rows = (
            model.objects.select_related("school")
            .filter(tenant__isnull=True, school__isnull=False)
            .iterator()
        )
        for row in rows:
            model.objects.filter(pk=row.pk).update(tenant_id=row.school.tenant_id)

    # ── Position.default_role: PlatformRoleTemplate slug → TenantRoleTemplate ──
    codex = Tenant.objects.filter(slug="codex", kind="PLATFORM").first()
    if codex is not None:
        for pos in Position.objects.filter(default_role__isnull=False).iterator():
            role = Role.objects.filter(
                tenant=codex, key=str(pos.default_role_id),
            ).first()
            if role is not None:
                Position.objects.filter(pk=pos.pk).update(
                    default_role_tenant_id=role.pk,
                )


def backwards(apps, schema_editor):
    # The legacy columns are re-created empty by the reversed RemoveField ops;
    # we only need to clear the tenant-side columns so the AddField reversal can
    # drop them cleanly. (Data is not round-tripped, matching the repo precedent.)
    LoginSession = apps.get_model("vs_user", "LoginSession")
    AuthAttempt = apps.get_model("vs_user", "AuthAttempt")
    AuthEventLog = apps.get_model("vs_user", "AuthEventLog")
    Position = apps.get_model("vs_user", "Position")
    LoginSession.objects.update(tenant=None)
    AuthAttempt.objects.update(tenant=None)
    AuthEventLog.objects.update(tenant=None)
    Position.objects.update(default_role_tenant=None)


class Migration(migrations.Migration):

    dependencies = [
        ("vs_user", "0005_remove_user_persona"),
        ("vs_tenants", "0002_backfill_tenants"),
        ("vs_rbac", "0004_backfill_tenant_rbac"),
    ]

    operations = [
        migrations.AddField(
            model_name="loginsession",
            name="tenant",
            field=models.ForeignKey(
                blank=True, null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="login_sessions", to="vs_tenants.tenant",
            ),
        ),
        migrations.AddField(
            model_name="authattempt",
            name="tenant",
            field=models.ForeignKey(
                blank=True, null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="auth_attempts", to="vs_tenants.tenant",
            ),
        ),
        migrations.AddField(
            model_name="autheventlog",
            name="tenant",
            field=models.ForeignKey(
                blank=True, null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="auth_events", to="vs_tenants.tenant",
            ),
        ),
        migrations.AddField(
            model_name="position",
            name="default_role_tenant",
            field=models.ForeignKey(
                blank=True, null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="default_for_positions", to="vs_rbac.tenantroletemplate",
            ),
        ),
        migrations.RunPython(forwards, backwards),
        migrations.RemoveField(model_name="loginsession", name="school"),
        migrations.RemoveField(model_name="authattempt", name="school"),
        migrations.RemoveField(model_name="autheventlog", name="school"),
        migrations.RemoveField(model_name="position", name="default_role"),
        migrations.RenameField(
            model_name="position",
            old_name="default_role_tenant",
            new_name="default_role",
        ),
    ]

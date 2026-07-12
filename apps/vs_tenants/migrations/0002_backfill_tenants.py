from django.db import migrations
from django.utils import timezone


CODEX_SLUG = "codex"


def forwards(apps, schema_editor):
    Tenant = apps.get_model("vs_tenants", "Tenant")
    School = apps.get_model("vs_schools", "School")
    User = apps.get_model("vs_user", "User")

    if School.objects.filter(slug=CODEX_SLUG).exists():
        raise RuntimeError("Existing school slug 'codex' conflicts with the reserved platform tenant.")

    codex, _ = Tenant.objects.get_or_create(
        slug=CODEX_SLUG,
        defaults={
            "name": "CodeX",
            "kind": "PLATFORM",
            "status": "ACTIVE",
            "activated_at": timezone.now(),
        },
    )
    for school in School.objects.all().iterator():
        tenant, _ = Tenant.objects.get_or_create(
            slug=school.slug,
            defaults={
                "name": school.name,
                "kind": "SCHOOL",
                "status": "ACTIVE" if school.status == "ACTIVE" else school.status,
                "activated_at": school.activated_at,
                "deactivated_at": school.deactivated_at,
            },
        )
        if school.tenant_id != tenant.pk:
            School.objects.filter(pk=school.pk).update(tenant_id=tenant.pk)

    persona_map = {
        "STUDENT": "STUDENT",
        "PARENT": "PARENT",
        "STAFF": "STAFF",
        "SCHOOL_ADMIN": "STAFF",
        "BRANCH_ADMIN": "STAFF",
    }
    for user in User.objects.select_related("school").all().iterator():
        tenant_id = codex.pk if user.user_type == "CX_STAFF" else getattr(user.school, "tenant_id", None)
        User.objects.filter(pk=user.pk).update(
            tenant_id=tenant_id,
            persona=persona_map.get(user.user_type, ""),
        )


def backwards(apps, schema_editor):
    School = apps.get_model("vs_schools", "School")
    User = apps.get_model("vs_user", "User")
    School.objects.update(tenant=None)
    User.objects.update(tenant=None, persona="")


class Migration(migrations.Migration):
    dependencies = [
        ("vs_tenants", "0001_initial"),
        ("vs_schools", "0003_school_tenant"),
        ("vs_user", "0003_user_tenant_persona"),
    ]
    operations = [migrations.RunPython(forwards, backwards)]

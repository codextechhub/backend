from django.db import migrations


REQUIRED_ADMIN_ROLE_TEMPLATES = (
    {
        "key": "school_admin",
        "name": "School Admin",
        "scope": "institution",
        "tier": "A",
        "description": (
            "Primary administrator for a school. Provisioned automatically "
            "during school onboarding."
        ),
    },
    {
        "key": "branch_admin",
        "name": "Branch Admin",
        "scope": "branch",
        "tier": "A",
        "description": "Administrative manager of one branch.",
    },
)


def seed_required_admin_role_templates(apps, schema_editor):
    """Ensure school onboarding has the two role templates it requires.

    Existing rows are deliberately preserved. Operators may have refined their
    descriptions or deactivated a template intentionally, and a migration must
    not silently reverse either decision. A missing row is the fresh-install
    gap this migration closes.
    """
    PrebuiltRoleTemplate = apps.get_model("vs_rbac", "PrebuiltRoleTemplate")

    for role in REQUIRED_ADMIN_ROLE_TEMPLATES:
        PrebuiltRoleTemplate.objects.get_or_create(
            key=role["key"],
            defaults={
                "name": role["name"],
                "scope": role["scope"],
                "tier": role["tier"],
                "description": role["description"],
                "is_active": True,
            },
        )


class Migration(migrations.Migration):
    dependencies = [
        ("vs_rbac", "0010_permissionregistryrevision"),
        ("vs_schools", "0009_remove_branchprimaryadmin_role_label_and_more"),
    ]

    operations = [
        migrations.RunPython(
            seed_required_admin_role_templates,
            migrations.RunPython.noop,
        ),
    ]

"""
Migrate RBAC models from branch-scoped to institution-scoped.

Steps:
1. Add new institution FK columns (nullable)
2. Data migration: populate institution_id from branch.institution_id
3. Remove old branch FK columns
4. Rename constraints and indexes
"""
import django.db.models.deletion
import django.db.models.functions.text
from django.db import migrations, models


def populate_institution_from_branch(apps, schema_editor):
    """Copy branch.institution_id into the new institution_id column."""
    RoleTemplate = apps.get_model("vs_rbac", "RoleTemplate")
    UserRoleAssignment = apps.get_model("vs_rbac", "UserRoleAssignment")
    RoleChangeRequest = apps.get_model("vs_rbac", "RoleChangeRequest")
    Branch = apps.get_model("vs_institutions", "Branch")

    # Build branch_id -> institution_id mapping
    branch_map = dict(Branch.objects.values_list("id", "institution_id"))

    for Model in [RoleTemplate, UserRoleAssignment, RoleChangeRequest]:
        for obj in Model.objects.all():
            obj.institution_id = branch_map.get(obj.branch_id)
            obj.save(update_fields=["institution_id"])


def reverse_populate(apps, schema_editor):
    """Reverse: copy institution back to branch (picks the institution's first branch)."""
    RoleTemplate = apps.get_model("vs_rbac", "RoleTemplate")
    UserRoleAssignment = apps.get_model("vs_rbac", "UserRoleAssignment")
    RoleChangeRequest = apps.get_model("vs_rbac", "RoleChangeRequest")
    Branch = apps.get_model("vs_institutions", "Branch")

    # For reverse, assign to the institution's main branch (is_main=True) or first branch
    inst_to_branch = {}
    for branch in Branch.objects.order_by("-is_main", "id"):
        if branch.institution_id not in inst_to_branch:
            inst_to_branch[branch.institution_id] = branch.id

    for Model in [RoleTemplate, UserRoleAssignment, RoleChangeRequest]:
        for obj in Model.objects.all():
            obj.branch_id = inst_to_branch.get(obj.institution_id)
            obj.save(update_fields=["branch_id"])


class Migration(migrations.Migration):

    dependencies = [
        ("vs_rbac", "0002_initial"),
        ("vs_institutions", "0001_initial"),
    ]

    operations = [
        # -----------------------------------------------------------------
        # Step 1: Remove old indexes and constraints that reference 'branch'
        # -----------------------------------------------------------------

        # RoleTemplate indexes
        migrations.RemoveIndex(
            model_name="roletemplate",
            name="vs_rbac_rol_branch__c3cd99_idx",
        ),
        migrations.RemoveIndex(
            model_name="roletemplate",
            name="vs_rbac_rol_branch__7aaaaf_idx",
        ),
        migrations.RemoveConstraint(
            model_name="roletemplate",
            name="uq_role_name_per_branch_ci",
        ),

        # RoleChangeRequest index
        migrations.RemoveIndex(
            model_name="rolechangerequest",
            name="vs_rbac_rol_branch__6604ed_idx",
        ),

        # UserRoleAssignment indexes + constraint
        migrations.RemoveIndex(
            model_name="userroleassignment",
            name="vs_rbac_use_branch__a75dcd_idx",
        ),
        migrations.RemoveIndex(
            model_name="userroleassignment",
            name="vs_rbac_use_branch__688b84_idx",
        ),
        migrations.RemoveConstraint(
            model_name="userroleassignment",
            name="uq_active_assignment_user_role_branch",
        ),

        # -----------------------------------------------------------------
        # Step 2: Add institution FK columns (nullable initially)
        # -----------------------------------------------------------------
        migrations.AddField(
            model_name="roletemplate",
            name="institution",
            field=models.ForeignKey(
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="role_templates_new",
                to="vs_institutions.institution",
            ),
        ),
        migrations.AddField(
            model_name="userroleassignment",
            name="institution",
            field=models.ForeignKey(
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="role_assignments_new",
                to="vs_institutions.institution",
            ),
        ),
        migrations.AddField(
            model_name="rolechangerequest",
            name="institution",
            field=models.ForeignKey(
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="role_change_requests_new",
                to="vs_institutions.institution",
            ),
        ),

        # -----------------------------------------------------------------
        # Step 3: Data migration
        # -----------------------------------------------------------------
        migrations.RunPython(
            populate_institution_from_branch,
            reverse_code=reverse_populate,
        ),

        # -----------------------------------------------------------------
        # Step 4: Make institution NOT NULL
        # -----------------------------------------------------------------
        migrations.AlterField(
            model_name="roletemplate",
            name="institution",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.PROTECT,
                related_name="role_templates",
                to="vs_institutions.institution",
            ),
        ),
        migrations.AlterField(
            model_name="userroleassignment",
            name="institution",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.PROTECT,
                related_name="role_assignments",
                to="vs_institutions.institution",
            ),
        ),
        migrations.AlterField(
            model_name="rolechangerequest",
            name="institution",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.PROTECT,
                related_name="role_change_requests",
                to="vs_institutions.institution",
            ),
        ),

        # -----------------------------------------------------------------
        # Step 5: Remove old branch FK columns
        # -----------------------------------------------------------------
        migrations.RemoveField(
            model_name="roletemplate",
            name="branch",
        ),
        migrations.RemoveField(
            model_name="userroleassignment",
            name="branch",
        ),
        migrations.RemoveField(
            model_name="rolechangerequest",
            name="branch",
        ),

        # -----------------------------------------------------------------
        # Step 6: Re-add indexes and constraints with institution
        # -----------------------------------------------------------------
        migrations.AddIndex(
            model_name="roletemplate",
            index=models.Index(
                fields=["institution", "status"],
                name="vs_rbac_rol_institu_status_idx",
            ),
        ),
        migrations.AddIndex(
            model_name="roletemplate",
            index=models.Index(
                fields=["institution", "is_locked"],
                name="vs_rbac_rol_institu_locked_idx",
            ),
        ),
        migrations.AddConstraint(
            model_name="roletemplate",
            constraint=models.UniqueConstraint(
                django.db.models.functions.text.Lower("name"),
                models.F("institution"),
                name="uq_role_name_per_institution_ci",
            ),
        ),

        migrations.AddIndex(
            model_name="rolechangerequest",
            index=models.Index(
                fields=["institution", "status", "submitted_at"],
                name="vs_rbac_rcr_institu_status_idx",
            ),
        ),

        migrations.AddIndex(
            model_name="userroleassignment",
            index=models.Index(
                fields=["institution", "user", "assignment_status"],
                name="vs_rbac_ura_institu_user_idx",
            ),
        ),
        migrations.AddIndex(
            model_name="userroleassignment",
            index=models.Index(
                fields=["institution", "role", "assignment_status"],
                name="vs_rbac_ura_institu_role_idx",
            ),
        ),
        migrations.AddConstraint(
            model_name="userroleassignment",
            constraint=models.UniqueConstraint(
                condition=models.Q(("assignment_status", "ACTIVE")),
                fields=("institution", "user", "role"),
                name="uq_active_assignment_user_role_institution",
            ),
        ),
    ]

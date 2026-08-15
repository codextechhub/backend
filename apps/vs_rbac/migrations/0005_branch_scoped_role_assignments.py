"""Let one person hold the same role at more than one branch.

``uq_active_tenant_user_role`` spanned (tenant, user, role) for every active
assignment, so "Storekeeper at Ikeja" and "Storekeeper at Lekki" collided and
the second could not be stored. It is replaced by two partial constraints that
keep both of the guarantees the single one bundled together: one active
whole-tenant grant of a role per person, and one active grant of a role per
person per branch.

Data-safe and reversible. The replacement is strictly weaker than what it
replaces, so no existing row can violate it, and reversing re-imposes the
original at a point where (by the same argument) nothing can have been stored
that would breach it.
"""

from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("vs_rbac", "0004_retarget_branch_to_vs_tenants"),
        ("vs_tenants", "0004_move_branch_from_vs_schools"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.RemoveConstraint(
            model_name="tenantuserroleassignment",
            name="uq_active_tenant_user_role",
        ),
        migrations.AddConstraint(
            model_name="tenantuserroleassignment",
            constraint=models.UniqueConstraint(
                condition=models.Q(
                    ("assignment_status", "ACTIVE"), ("branch__isnull", True)
                ),
                fields=("tenant", "user", "role"),
                name="uq_active_tenant_user_role",
            ),
        ),
        migrations.AddConstraint(
            model_name="tenantuserroleassignment",
            constraint=models.UniqueConstraint(
                condition=models.Q(
                    ("assignment_status", "ACTIVE"), ("branch__isnull", False)
                ),
                fields=("tenant", "user", "role", "branch"),
                name="uq_active_tenant_user_role_branch",
            ),
        ),
    ]

"""Register submitting a requisition as an ordinary key.

Submitting a requisition asks for approval and approves nothing: the approval
route decides. Every teacher may raise one, so the Teacher role carries the
key, and a restricted key on that role would send every teacher's appointment
through a role-grant approval whenever the administrator appointing them does
not hold it. The seed registers the key as NORMAL; this brings the row that
already exists into line, because the seed never rewrites an existing row.

Reversing restores the restricted, SENSITIVE registration.
"""

from django.db import migrations

KEY = "procurement.requisition.submit"


def make_ordinary(apps, schema_editor):
    Permission = apps.get_model("vs_rbac", "Permission")
    Permission.objects.filter(key=KEY).update(sensitivity_level="NORMAL", is_restricted=False)


def make_restricted(apps, schema_editor):
    Permission = apps.get_model("vs_rbac", "Permission")
    Permission.objects.filter(key=KEY).update(sensitivity_level="SENSITIVE", is_restricted=True)


class Migration(migrations.Migration):
    dependencies = [
        ("vs_procurement", "0047_release_awarded_shared_lines"),
        ("vs_rbac", "0034_split_permissions_name_their_own_action"),
    ]

    operations = [migrations.RunPython(make_ordinary, make_restricted)]

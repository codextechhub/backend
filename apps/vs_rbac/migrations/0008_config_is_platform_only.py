"""Two corrections to what a school is offered when it edits its own roles.

The roles screen shipped for schools in M09, and it is the first surface that
ever showed a school administrator the permission registry. It showed her
nineteen configuration permissions and a family of keys captioned "Invite new
Vision team members". Both were wrong, and wrong in different ways.

**``config.*`` was misclassified.** Every key in that module is CodeX deciding
what a school HAS: which capabilities are on, which entitlements are granted,
what the security and integration settings are. Migration 0007 swept every
non-``platform`` module to ``TENANT`` as a group, and this one went with them.
The write endpoints were never actually reachable - ``vs_config.views`` marks
them ``platform_methods``, so the refusal came from a second guard rather than
from the scope - but a permission a tenant may hold is a permission the picker
offers, and a tenant that could hold ``config.entitlement.manage`` is one row of
enforcement away from granting itself the modules it has not bought. No school
role holds any of the nineteen, so nothing is taken away.

**Five keys wrote a global table from inside a tenant.** The registry has a
handful of tables with no tenant column at all - one row set shared by every
school on the platform - and a write key on one of those must never be
tenant-holdable. These were:

* ``communication.notification_templates.configure``. The one that was not
  theoretical. Its ViewSet scopes nothing (``get_queryset`` says so out loud:
  "global catalogue records, not school-scoped rows") and carries no platform
  guard, so a school that granted itself the key could read and rewrite the
  message templates every other school receives - the billing invoice email
  among them. Verified against a live tenant before this was written.
* ``finance.currency.create`` and ``finance.fxrate.create``. Both views describe
  themselves as "**global** reference data (no entity)" and both POST into a
  table with no tenant column and no platform guard.
* ``import.templates.create`` and ``import.templates.manage``. Already refused
  by an ``_is_platform`` check in the view, so this is the same rule restated in
  the column the picker reads - a school is no longer offered a box the save
  would refuse.

The matching ``.view`` keys deliberately stay tenant-holdable: a school has to
read the currency list and the template list to use either.

**``platform.team.*`` was worded from one side of the platform.** It is NOT
reclassified here, and that is deliberate: schools genuinely hold these keys.
``vs_user.account_scope`` says so in as many words - "Amaka administers Bright
Star and holds ``platform.team.suspend``, which is how she suspends her own
leavers" - and the tenant boundary is drawn by ``administrable_users``, not by
the key. What was wrong was the caption. A school admin choosing what her bursar
may do was reading "Invite new Vision team members", which describes CodeX's
staff console, not hers. The descriptions are rewritten to be true for whoever
holds the key.

Reversible: 0007's classification and the old captions are restored exactly.
"""
from django.db import migrations


CONFIG_MODULE = "config"

#: Write keys on tables that have no tenant column, so one school's edit lands
#: on every school. Reads on the same tables stay tenant-holdable.
GLOBAL_WRITE_KEYS = [
    "communication.notification_templates.configure",
    "finance.currency.create",
    "finance.fxrate.create",
    "import.templates.create",
    "import.templates.manage",
]

#: key -> (old description, new description)
TEAM_CAPTIONS = {
    "platform.team.view": (
        "View Vision team members",
        "View staff accounts",
    ),
    "platform.team.create": (
        "Invite new Vision team members",
        "Invite new staff members",
    ),
    "platform.team.update": (
        "Edit a team member profile",
        "Edit a staff member's profile",
    ),
    "platform.team.delete": (
        "Permanently remove a team member",
        "Permanently remove a staff account",
    ),
    "platform.team.suspend": (
        "Suspend a team member account",
        "Suspend a staff account",
    ),
}

TEAM_RESOURCE_LABEL = ("Vision staff team management", "Staff account management")


def _apply(apps, schema_editor, *, forward):
    Permission = apps.get_model("vs_rbac", "Permission")
    PermissionResource = apps.get_model("vs_rbac", "PermissionResource")

    Permission.objects.filter(module_id=CONFIG_MODULE).update(
        scope="PLATFORM" if forward else "TENANT",
    )
    Permission.objects.filter(key__in=GLOBAL_WRITE_KEYS).update(
        scope="PLATFORM" if forward else "TENANT",
    )

    for key, (old, new) in TEAM_CAPTIONS.items():
        Permission.objects.filter(key=key).update(
            description=new if forward else old,
        )

    old_label, new_label = TEAM_RESOURCE_LABEL
    PermissionResource.objects.filter(module_id="platform", name="team").update(
        description=new_label if forward else old_label,
    )


def forward(apps, schema_editor):
    _apply(apps, schema_editor, forward=True)


def backward(apps, schema_editor):
    _apply(apps, schema_editor, forward=False)


class Migration(migrations.Migration):

    dependencies = [
        ("vs_rbac", "0007_classify_permission_scope"),
    ]

    operations = [
        migrations.RunPython(forward, backward),
    ]

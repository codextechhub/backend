"""Take back ``tickets.ticket.assign``, which no school can act on.

Every ticket assignee is CodeX support staff: ``assign_ticket`` refuses anybody
else, because an assignee is a resolver and a school does not resolve tickets
on the platform's desk. So assigning is CodeX deciding which of its own people
works a thing, a rota decision that needs to know who is on leave, who already
carries the case, and what else is in their queue.

A school's say is escalation. That is the decision they are placed to make -
whether this is beyond them - and it is already the decision that hands CodeX
the ticket. Naming the person on the other side is not a second half of it.

The key was offered to a school's roles screen all the same, and it did more
than disappoint. Assignment reached further than escalation while asking less:
an assignee is a participant, so naming one admitted CodeX to the thread and
its internal notes, and ``tickets.ticket.assign`` is a key a school could grant
itself while escalating takes ``tickets.ticket.manage``.

No seeded school role has ever held it - ``seed_ticket_permissions`` attaches
view, comment and attachment defaults, plus update, manage and report viewing
for the two admin roles - so any row taken here is a hand-made grant that
governs nothing. The grants go first: the scope guard refuses a platform key on
a tenant role, and rows already sitting there would make those roles
unsaveable.
"""
from django.db import migrations

ASSIGN = "tickets.ticket.assign"


def take_it_back(apps, schema_editor):
    Permission = apps.get_model("vs_rbac", "Permission")
    RolePermission = apps.get_model("vs_rbac", "TenantRolePermission")
    PrebuiltRolePermission = apps.get_model("vs_rbac", "PrebuiltRolePermission")

    RolePermission.objects.filter(
        permission_id=ASSIGN, role__tenant__kind="SCHOOL",
    ).delete()
    # Prebuilt defaults are copied into every school that adopts the role, so a
    # platform key here would be a fleet-wide grant rather than one school's.
    PrebuiltRolePermission.objects.filter(permission_id=ASSIGN).delete()

    Permission.objects.filter(key=ASSIGN).update(scope="PLATFORM")


class Migration(migrations.Migration):
    dependencies = [
        ("vs_rbac", "0018_keys_a_school_was_offered_and_cannot_use"),
    ]
    operations = [
        migrations.RunPython(take_it_back, migrations.RunPython.noop),
    ]

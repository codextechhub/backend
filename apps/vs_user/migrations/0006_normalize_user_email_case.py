"""Fold every stored email address to lowercase, and hold it there.

Phase 2 of the per-tenant email work. ``UserManager._create_user`` called
Django's ``normalize_email``, which lowercases only the domain, and neither
``User.save()`` nor ``User.clean()`` touched the address at all. PostgreSQL
unique indexes are case sensitive, so ``Ada@gmail.com`` and ``ada@gmail.com``
could sit in this table side by side while every lookup asked for
``email__iexact`` and took ``.first()``.

The repair refuses rather than guesses
--------------------------------------
Two rows that fold to one address are two people, or one person with two
accounts, and which of those it is - and what should happen to them - is a
human decision. This migration therefore reports and aborts instead of merging,
renaming or dropping anything. Nothing is written before the check passes.

It refuses on two shapes, for two different reasons:

* **Same tenant.** This is the case Phase 3's per-tenant constraint will forbid
  outright. Somebody must decide which account survives.
* **Different tenants.** Phase 3 will make this legal, but it is not legal yet:
  ``User.email`` is still ``unique=True`` platform-wide, so lowercasing the
  pair would fail as an IntegrityError halfway through. Better to name the two
  addresses than to hand an operator a constraint violation.

Reversibility
-------------
The data step is irreversible in the only sense that matters: the original
capitalisation is gone and no reverse can invent it. It is written as a no-op
reverse rather than a raised error so that unapplying the constraint below is
possible, and it is safe to run again at any time - on already-folded data it
finds nothing to do and writes nothing.

Adding ``ck_user_email_lowercase`` in the same migration is what makes the
repair permanent. Without it the table is clean for exactly as long as nobody
runs an ``UPDATE`` by hand.
"""

from django.db import migrations, models
from django.db.models import Count
from django.db.models.functions import Lower

TABLE = "vs_users_user"


def _flush_deferred_constraints(schema_editor):
    """Drain the deferred trigger queue for the user table.

    Django creates foreign keys as DEFERRABLE INITIALLY DEFERRED, so writing
    rows inside a migration can leave trigger events pending, and PostgreSQL
    then refuses ``ALTER TABLE ... ADD CONSTRAINT`` on that table for the rest
    of the transaction. ``check_constraints`` is Django's vendor-neutral way to
    force them immediate, so this needs no PostgreSQL branch. Same pattern as
    ``vs_schools/migrations/0003_branch_tenant.py``.
    """
    schema_editor.connection.check_constraints(table_names=[TABLE])


def _collisions(User):
    """Every address held by more than one row once case is folded away.

    Returns a list of ``(folded_address, [(pk, tenant_id, stored_email), ...])``
    ordered by address then pk, so the report reads the same on every replica.

    While ``User.email`` is still globally unique a group can only be case
    variants of one address - two identical strings cannot both be stored. The
    grouping is written against ``Lower(email)`` regardless, because that is
    the property Phase 3 actually constrains.
    """
    folded = [
        row["folded"]
        for row in (
            User.objects.annotate(folded=Lower("email"))
            .values("folded")
            .annotate(n=Count("id"))
            .filter(n__gt=1)
            .order_by("folded")
        )
    ]
    if not folded:
        return []

    rows = (
        User.objects.annotate(folded=Lower("email"))
        .filter(folded__in=folded)
        .order_by("folded", "pk")
        .values_list("folded", "pk", "tenant_id", "email")
    )
    grouped = {address: [] for address in folded}
    for address, pk, tenant_id, email in rows:
        grouped[address].append((pk, tenant_id, email))
    return [(address, grouped[address]) for address in folded]


def _refusal_report(collisions):
    """Name every colliding row so an operator can act without a database shell."""
    lines = [
        f"Refusing to normalise email case: {len(collisions)} address(es) are "
        f"held by more than one user once case is folded away.",
        "",
        "No row was modified. Each of these is a decision about which account "
        "is real, and a migration must not make it by merging, renaming or "
        "deleting anyone. Resolve them by hand, then run migrate again.",
        "",
    ]
    for address, rows in collisions:
        tenants = [tenant_id for _pk, tenant_id, _email in rows]
        scope = (
            "same tenant" if len(set(tenants)) == 1
            else "different tenants - still blocked by User.email unique=True"
        )
        lines.append(f"  {address}  ({scope})")
        for pk, tenant_id, email in rows:
            lines.append(f"      user pk={pk} tenant={tenant_id} email={email!r}")
        lines.append("")
    return "\n".join(lines)


def normalize_email_case(apps, schema_editor):
    User = apps.get_model("vs_user", "User")

    collisions = _collisions(User)
    if collisions:
        raise RuntimeError(_refusal_report(collisions))

    # Folded by the database, not by Python: one statement, and the same
    # LOWER() the constraint below will hold the column to from now on.
    updated = User.objects.exclude(email=Lower("email")).update(email=Lower("email"))
    if updated:
        _flush_deferred_constraints(schema_editor)


class Migration(migrations.Migration):

    dependencies = [
        ("vs_user", "0005_retarget_branch_to_vs_tenants"),
    ]

    operations = [
        migrations.RunPython(
            normalize_email_case,
            # Nothing to undo: the original capitalisation is unrecoverable.
            migrations.RunPython.noop,
        ),
        migrations.AddConstraint(
            model_name="user",
            constraint=models.CheckConstraint(
                condition=models.Q(email=Lower("email")),
                name="ck_user_email_lowercase",
            ),
        ),
    ]

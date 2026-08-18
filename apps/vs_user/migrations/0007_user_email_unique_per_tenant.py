"""Narrow email uniqueness from the whole platform to one tenant.

Phase 3 of the per-tenant email work. ``User.email`` was ``unique=True``, so
one real address could be a login at exactly one customer of this platform.
Ada Okoye has a child at Bright Star and another at Greenfield and uses
``ada@gmail.com`` at both; Greenfield literally could not create her an
account, and the refusal itself told Greenfield that somebody, somewhere, held
that address. The global index is dropped and replaced with
``uq_user_email_per_tenant`` on ``(tenant_id, email)``.

Why the plain columns and not ``Lower(email)``
----------------------------------------------
``ck_user_email_lowercase`` (migration 0006) holds every stored address to its
folded form, so a two-column constraint on the raw columns is case-insensitive
in effect: the row that would defeat it cannot be written. The plain form also
leaves an index on ``(tenant_id, email)`` that ordinary equality lookups can
use, where an expression index on ``lower(email)`` would serve neither those
nor Django's ``__iexact`` (which compiles to ``UPPER()`` on PostgreSQL).

The verification does not trust its predecessor
-----------------------------------------------
0006 refused to run while any address collided, so on any database that ran it
in order there is nothing here to find. This migration checks anyway - a
migration is a permanent artifact and will meet databases nobody has surveyed,
restored dumps, and branches merged out of order. It refuses and names the rows
rather than letting ``ADD CONSTRAINT`` fail with a message that identifies
nothing.

Both directions refuse, for opposite reasons
--------------------------------------------
* **Forward**: two users in ONE tenant sharing an address. Which of them is the
  real account is a human decision, so this reports and aborts instead of
  merging, renaming or deleting anyone.
* **Reverse**: two TENANTS sharing an address. That is legal once this
  migration has been applied, and illegal again the moment global uniqueness is
  restored, so the reverse would otherwise die inside ``ALTER TABLE`` with an
  IntegrityError naming an index rather than the accounts. It names them.

The operation order is what makes the reverse check work. Migrations unapply in
reverse order, so the ``RunPython`` sits BETWEEN the field change and the
constraint: forward it runs after the global index is dropped and before the
per-tenant one is added, and backward it runs after the per-tenant one is
dropped and before the global one is restored. Either way the check is the last
thing that happens before the constraint that depends on it.
"""

from django.db import migrations, models
from django.db.models import Count
from django.db.models.functions import Lower

TABLE = "vs_users_user"


def _flush_deferred_constraints(schema_editor):
    """Drain the deferred trigger queue for the user table.

    Django creates foreign keys as DEFERRABLE INITIALLY DEFERRED, so any row
    written earlier in this transaction can leave trigger events pending, and
    PostgreSQL then refuses ``ALTER TABLE ... ADD CONSTRAINT`` on that table
    for the rest of it. This migration writes nothing itself, so the queue is
    normally empty and this is a single cheap statement; it is here because
    that failure has already bitten this codebase twice (see
    ``vs_schools/migrations/0003_branch_tenant.py`` and ``vs_user/0006``) and
    because a squash would put a repair and this ``AddConstraint`` in one
    transaction. ``check_constraints`` is Django's vendor-neutral way to force
    the events immediate, so this needs no PostgreSQL branch.
    """
    schema_editor.connection.check_constraints(table_names=[TABLE])


def _collisions(User, *, per_tenant):
    """Rows whose folded address repeats inside the scope being made unique.

    ``per_tenant=True`` groups by ``(tenant_id, lower(email))`` - the scope of
    the constraint being added. ``per_tenant=False`` groups by
    ``lower(email)`` alone - the scope of the platform-wide uniqueness a
    reverse would restore.

    Grouping on ``Lower(email)`` rather than the raw column is deliberate: it
    makes this check independent of whether 0006 and
    ``ck_user_email_lowercase`` are in place, which is what "do not assume the
    predecessor ran" means here. A pair that differs only in case is refused
    too, because the uniqueness being asked for is case-insensitive.

    Returns ``[(folded_address, [(pk, tenant_id, stored_email), ...]), ...]``
    ordered by address then pk, so the report reads the same on every replica.
    """
    scope = ["folded", "tenant_id"] if per_tenant else ["folded"]
    repeated = {
        tuple(group[key] for key in scope)
        for group in (
            User.objects.annotate(folded=Lower("email"))
            .values(*scope)
            .annotate(n=Count("id"))
            .filter(n__gt=1)
        )
    }
    if not repeated:
        return []

    addresses = sorted({group[0] for group in repeated})
    rows = (
        User.objects.annotate(folded=Lower("email"))
        .filter(folded__in=addresses)
        .order_by("folded", "pk")
        .values_list("folded", "pk", "tenant_id", "email")
    )

    grouped = {}
    for address, pk, tenant_id, email in rows:
        key = (address, tenant_id) if per_tenant else (address,)
        if key not in repeated:
            continue  # shares the address, but not inside a colliding scope
        grouped.setdefault(address, []).append((pk, tenant_id, email))
    return [(address, grouped[address]) for address in addresses]


def _report(headline, remedy, collisions):
    """Name every colliding row so an operator can act without a database shell."""
    lines = [headline, "", remedy, ""]
    for address, rows in collisions:
        lines.append(f"  {address}")
        for pk, tenant_id, email in rows:
            lines.append(f"      user pk={pk} tenant={tenant_id} email={email!r}")
        lines.append("")
    return "\n".join(lines)


def refuse_same_tenant_duplicates(apps, schema_editor):
    """Forward: no tenant may hold one address twice."""
    User = apps.get_model("vs_user", "User")

    collisions = _collisions(User, per_tenant=True)
    if collisions:
        raise RuntimeError(_report(
            f"Refusing to add uq_user_email_per_tenant: {len(collisions)} "
            f"address(es) are held by more than one user inside a single tenant.",
            "No row was modified. Each of these is a decision about which "
            "account is real, and a migration must not make it by merging, "
            "renaming or deleting anyone. Resolve them by hand, then run "
            "migrate again.",
            collisions,
        ))

    _flush_deferred_constraints(schema_editor)


def refuse_cross_tenant_duplicates(apps, schema_editor):
    """Reverse: no address may be held by two tenants if it must go global again."""
    User = apps.get_model("vs_user", "User")

    collisions = _collisions(User, per_tenant=False)
    if collisions:
        raise RuntimeError(_report(
            f"Refusing to restore platform-wide uniqueness on User.email: "
            f"{len(collisions)} address(es) are held by accounts at more than "
            f"one tenant.",
            "This is exactly what per-tenant uniqueness was introduced to "
            "allow, so unapplying it means deciding which of these accounts "
            "stops existing - a decision a migration must not make. Resolve "
            "them by hand, then unapply again.",
            collisions,
        ))

    _flush_deferred_constraints(schema_editor)


class Migration(migrations.Migration):

    dependencies = [
        ("vs_user", "0006_normalize_user_email_case"),
    ]

    operations = [
        migrations.AlterField(
            model_name="user",
            name="email",
            field=models.EmailField(max_length=254),
        ),
        migrations.RunPython(
            refuse_same_tenant_duplicates,
            refuse_cross_tenant_duplicates,
        ),
        migrations.AddConstraint(
            model_name="user",
            constraint=models.UniqueConstraint(
                fields=("tenant", "email"), name="uq_user_email_per_tenant"
            ),
        ),
    ]

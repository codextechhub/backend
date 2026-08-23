"""Remove ``User.user_type``, and rekey the branch rule onto the tenant.

A persona column can disagree with reality, and nothing here could ever detect
the disagreement. A row marked ``STUDENT`` with no student record anywhere was
writable and undetectable. Deriving each fact from the record that carries it
makes the contradiction unrepresentable instead of merely unlikely.

What the column was actually holding, value by value:

``STUDENT`` and ``PARENT``
    Read by no line of code outside the enum that declared them. Pure data with
    no reader. Being a parent is having a guardian record, not a column value.

``STAFF``
    The residue of the two admin personas retired in 0008. It meant "not
    platform staff", which is to say it meant nothing on its own.

``CX_STAFF``
    The same fact as "this account is on the PLATFORM tenant", recorded a
    second time with nothing holding the two copies together. At the time of
    writing: 43 users on the platform tenant, all ``CX_STAFF``; 43 ``CX_STAFF``
    users, all on the platform tenant; no exceptions in either direction - and
    no constraint, trigger or check that would have noticed one. The hedging in
    ``vs_tickets.services.visibility``, which filtered on BOTH columns, is the
    honest record of how much that correspondence was trusted.

The branch rule, which is the part that needed care
---------------------------------------------------
``ck_vision_staff_no_branch`` said ``user_type='CX_STAFF' -> branch IS NULL``.
It meant "a user of the PLATFORM tenant holds no branch", and it was allowed to
say ``user_type`` instead only because the two agreed. Dropping the column
without moving the rule would have deleted a real guarantee.

A CheckConstraint cannot state it. The tenant's ``kind`` lives in another
table, and a CHECK constraint is evaluated per row and may not contain a
subquery - on PostgreSQL or anywhere else. Django refuses first: a relational
lookup inside a CheckConstraint raises ``FieldError: Joined field references
are not permitted in this query``. So the rule moves to triggers, which is the
only mechanism that can enforce a cross-table invariant in the database, and it
stays in the database rather than being downgraded to a Python check.

Two triggers, because the pair (user's tenant kind, user's branch) can be
broken from either side:

``vs_user_platform_no_branch``
    On ``vs_users_user``. Refuses an INSERT or UPDATE that leaves a branch on a
    user whose tenant is PLATFORM. The direct replacement for the constraint.

``vs_tenants_platform_kind_no_branch_users``
    On ``vs_tenants_tenant``. Refuses flipping a tenant to PLATFORM while any
    of its users still holds a branch. Without it the same broken state is one
    UPDATE away on the other table, and the guarantee would be only half kept.

Both raise SQLSTATE 23514 (``check_violation``), the same class the constraint
raised, so any caller that was catching an IntegrityError still catches this.

Note what is deliberately NOT enforced here: nothing says a tenant user MUST
have a branch. A NULL branch means "across the whole tenant" - a first-class
value the academic structure and procurement documents already use - and it
never meant "no branches exist".

The uid constraints
-------------------
There were two, and they were never two rules. ``unique_uid_per_tenant`` held
uid unique per tenant for non-CX rows; ``unique_uid_vision_staff`` held it
unique globally for CX rows. Every platform user lives in the one PLATFORM
tenant, so "unique among CX staff" and "unique within the platform tenant" pick
out exactly the same rows - the second was the first one spelled differently,
because the persona was doing the tenant's job. One unconditional
``unique_uid_per_tenant`` states it for everybody, and the allocator in
``User.save()`` now has one branch instead of two.

The forward direction verifies this before it relies on it, and refuses rather
than letting the constraint fail with a message about a column.

Reversibility, honestly
-----------------------
**The values are gone and cannot come back.** Dropping a column drops its data;
there is no shadow copy, and this migration deliberately does not make one. A
reverse re-adds the column and can honestly fill in exactly one value:

* every user on a PLATFORM-kind tenant was ``CX_STAFF``, and that is recoverable
  because it is the correspondence this whole migration rests on;
* every other user becomes ``STAFF``, which is what 0008 had already collapsed
  every tenant persona into - except that a ``STUDENT`` or a ``PARENT`` row also
  becomes ``STAFF``, because after the drop a pupil and a bursar are the same
  row in every column that remains.

So reverse-then-forward is a round trip, and forward-then-reverse is not: it
loses the STUDENT/PARENT distinction for good.

That is acceptable, and it is worth being precise about why rather than waving
at it. Nothing read those two values - not one line outside the enum - so no
behaviour depends on getting them back. They were never populated by any
production path either: the only writer was the dev seeder. And a restore that
needs the real values has the pre-migration backup, which is where the values
actually live. What a reverse must not do is invent them, and it does not.

The reverse also cannot fail safely if a branchless platform user somehow
exists - the re-added CheckConstraint would reject it - so it checks first and
refuses with the offending rows named, rather than dying halfway.
"""

from django.db import migrations, models

USER_TABLE = "vs_users_user"
TENANT_TABLE = "vs_tenants_tenant"

# How many offending rows to name in a refusal before saying "and N more". A
# refusal has to be actionable, and a dump of ten thousand ids is not. Same
# figure, and same reasoning, as 0008.
_SAMPLE = 20


# --------------------------------------------------------------------------- #
# The branch rule, as triggers                                                 #
# --------------------------------------------------------------------------- #

PG_INSTALL_TRIGGERS = f"""
CREATE OR REPLACE FUNCTION vs_user_platform_no_branch() RETURNS trigger AS $$
DECLARE
    tenant_kind text;
BEGIN
    IF NEW.branch_id IS NULL THEN
        RETURN NEW;
    END IF;
    SELECT kind INTO tenant_kind
      FROM {TENANT_TABLE} WHERE id = NEW.tenant_id;
    IF tenant_kind = 'PLATFORM' THEN
        RAISE EXCEPTION
            'ck_platform_user_no_branch: a user of a PLATFORM tenant must not be assigned to a branch (user %, tenant %, branch %).',
            NEW.email, NEW.tenant_id, NEW.branch_id
            USING ERRCODE = 'check_violation';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS vs_user_platform_no_branch ON {USER_TABLE};
CREATE TRIGGER vs_user_platform_no_branch
    BEFORE INSERT OR UPDATE OF tenant_id, branch_id ON {USER_TABLE}
    FOR EACH ROW EXECUTE FUNCTION vs_user_platform_no_branch();


CREATE OR REPLACE FUNCTION vs_tenants_platform_kind_no_branch_users()
RETURNS trigger AS $$
DECLARE
    offending integer;
BEGIN
    IF NEW.kind <> 'PLATFORM' THEN
        RETURN NEW;
    END IF;
    SELECT count(*) INTO offending
      FROM {USER_TABLE}
     WHERE tenant_id = NEW.id AND branch_id IS NOT NULL;
    IF offending > 0 THEN
        RAISE EXCEPTION
            'ck_platform_user_no_branch: tenant % cannot become PLATFORM while % of its users still hold a branch.',
            NEW.slug, offending
            USING ERRCODE = 'check_violation';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS vs_tenants_platform_kind_no_branch_users ON {TENANT_TABLE};
CREATE TRIGGER vs_tenants_platform_kind_no_branch_users
    BEFORE INSERT OR UPDATE OF kind ON {TENANT_TABLE}
    FOR EACH ROW EXECUTE FUNCTION vs_tenants_platform_kind_no_branch_users();
"""

PG_DROP_TRIGGERS = f"""
DROP TRIGGER IF EXISTS vs_user_platform_no_branch ON {USER_TABLE};
DROP FUNCTION IF EXISTS vs_user_platform_no_branch();
DROP TRIGGER IF EXISTS vs_tenants_platform_kind_no_branch_users ON {TENANT_TABLE};
DROP FUNCTION IF EXISTS vs_tenants_platform_kind_no_branch_users();
"""

# MySQL/MariaDB can express the same thing, and vs_config's immutability
# triggers set the precedent for carrying both. A trigger body there cannot be
# replaced in one statement, hence the explicit drops.
MYSQL_USER_INSERT = f"""
CREATE TRIGGER vs_user_platform_no_branch_ins BEFORE INSERT ON {USER_TABLE}
FOR EACH ROW
BEGIN
    IF NEW.branch_id IS NOT NULL
       AND (SELECT kind FROM {TENANT_TABLE} WHERE id = NEW.tenant_id) = 'PLATFORM'
    THEN
        SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT =
            'ck_platform_user_no_branch: a user of a PLATFORM tenant must not be assigned to a branch.';
    END IF;
END
"""
MYSQL_USER_UPDATE = MYSQL_USER_INSERT.replace(
    "vs_user_platform_no_branch_ins BEFORE INSERT",
    "vs_user_platform_no_branch_upd BEFORE UPDATE",
)
MYSQL_TENANT_UPDATE = f"""
CREATE TRIGGER vs_tenants_platform_kind_upd BEFORE UPDATE ON {TENANT_TABLE}
FOR EACH ROW
BEGIN
    IF NEW.kind = 'PLATFORM'
       AND EXISTS (SELECT 1 FROM {USER_TABLE}
                    WHERE tenant_id = NEW.id AND branch_id IS NOT NULL)
    THEN
        SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT =
            'ck_platform_user_no_branch: tenant cannot become PLATFORM while its users hold a branch.';
    END IF;
END
"""
MYSQL_TRIGGER_NAMES = (
    "vs_user_platform_no_branch_ins",
    "vs_user_platform_no_branch_upd",
    "vs_tenants_platform_kind_upd",
)


def install_branch_triggers(apps, schema_editor):
    vendor = schema_editor.connection.vendor
    if vendor == "postgresql":
        schema_editor.execute(PG_INSTALL_TRIGGERS, params=None)
    elif vendor == "mysql":
        for name in MYSQL_TRIGGER_NAMES:
            schema_editor.execute(f"DROP TRIGGER IF EXISTS {name}", params=None)
        schema_editor.execute(MYSQL_USER_INSERT, params=None)
        schema_editor.execute(MYSQL_USER_UPDATE, params=None)
        schema_editor.execute(MYSQL_TENANT_UPDATE, params=None)


def drop_branch_triggers(apps, schema_editor):
    vendor = schema_editor.connection.vendor
    if vendor == "postgresql":
        schema_editor.execute(PG_DROP_TRIGGERS, params=None)
    elif vendor == "mysql":
        for name in MYSQL_TRIGGER_NAMES:
            schema_editor.execute(f"DROP TRIGGER IF EXISTS {name}", params=None)


# --------------------------------------------------------------------------- #
# Data checks and the reverse fill                                             #
# --------------------------------------------------------------------------- #

def _describe(rows):
    """Render a sample of (pk, email, extra) tuples for a refusal message."""
    shown = ", ".join(f"#{pk} {email} ({extra})" for pk, email, extra in rows[:_SAMPLE])
    if len(rows) > _SAMPLE:
        shown += f", and {len(rows) - _SAMPLE} more"
    return shown


def verify_before_drop(apps, schema_editor):
    """Refuse before anything is dropped, so a refusal leaves the table intact.

    Two things have to hold before the column can go, and both are cheap to ask
    while it is still there.
    """
    User = apps.get_model("vs_user", "User")

    # 1. The correspondence this migration rests on. If a CX_STAFF row sits
    #    outside the platform tenant, or a platform-tenant row is not CX_STAFF,
    #    then the two facts had already drifted - and dropping the column would
    #    silently decide which of them was right.
    strays = list(
        User.objects.filter(user_type="CX_STAFF")
        .exclude(tenant__kind="PLATFORM")
        .values_list("pk", "email", "tenant__slug")
    )
    if strays:
        raise RuntimeError(
            "Refusing to drop user_type: "
            f"{len(strays)} CX_STAFF row(s) are not on a PLATFORM tenant, so "
            "'CX_STAFF' and 'belongs to the platform tenant' are not the same "
            "set here and dropping the column would pick one of them without "
            "being told to. Move each account to the right tenant, or correct "
            f"its type, then re-run. Rows: {_describe(strays)}"
        )

    intruders = list(
        User.objects.filter(tenant__kind="PLATFORM")
        .exclude(user_type="CX_STAFF")
        .values_list("pk", "email", "user_type")
    )
    if intruders:
        raise RuntimeError(
            "Refusing to drop user_type: "
            f"{len(intruders)} row(s) live on a PLATFORM tenant without being "
            "CX_STAFF. After the drop they become platform staff by definition, "
            "which is a promotion this migration will not perform silently. "
            "Move them to the tenant they belong to, or set their type, then "
            f"re-run. Rows: {_describe(intruders)}"
        )

    # 2. The uid constraints merge into one. Two rows sharing (tenant, uid) are
    #    legal today if exactly one of them is CX_STAFF, and would break the
    #    unconditional constraint - as an IntegrityError naming an index rather
    #    than the accounts involved.
    from django.db.models import Count

    clashes = (
        User.objects.values("tenant_id", "uid")
        .exclude(uid=None)
        .annotate(n=Count("id"))
        .filter(n__gt=1)
    )
    clashing = [(c["tenant_id"], c["uid"]) for c in clashes]
    if clashing:
        rows = list(
            User.objects.filter(uid__in=[uid for _, uid in clashing])
            .values_list("pk", "email", "uid")
        )
        raise RuntimeError(
            "Refusing to drop user_type: "
            f"{len(clashing)} (tenant, uid) pair(s) are held by more than one "
            "account. That is legal only while the two uid constraints are "
            "split by persona, and this migration merges them into one. "
            f"Renumber the duplicates, then re-run. Rows: {_describe(rows)}"
        )


def noop_reverse_check(apps, schema_editor):
    """Nothing to undo: verify_before_drop only reads."""


def noop_forward(apps, schema_editor):
    """Nothing to do on the way forward. The reverse is the whole point here.

    Paired with ``restore_user_type`` and placed immediately before the
    RemoveField so that, on the way back, Django re-adds the column first and
    this operation's reverse is what fills it.
    """


def restore_user_type(apps, schema_editor):
    """Reverse fill. Recovers CX_STAFF honestly; everyone else becomes STAFF.

    STUDENT and PARENT do not come back, and cannot: after the forward drop a
    pupil, a guardian and a bursar are the same row in every remaining column.
    Guessing would be worse than leaving them as STAFF, which is what 0008 had
    already made every tenant persona anyway. Nothing reads these values, so
    nothing behaves differently for the loss - but anybody reversing this
    should know it happened, which is what this docstring is for.
    """
    User = apps.get_model("vs_user", "User")

    # The re-added ck_vision_staff_no_branch rejects a CX_STAFF row that holds
    # a branch. The forward triggers made that impossible, but a reverse that
    # ran after those triggers were dropped, or against a restored dump, could
    # meet one - and finding out from a half-applied AddConstraint is the worst
    # place to find out.
    stuck = list(
        User.objects.filter(tenant__kind="PLATFORM")
        .exclude(branch=None)
        .values_list("pk", "email", "branch_id")
    )
    if stuck:
        raise RuntimeError(
            "Refusing to restore user_type: "
            f"{len(stuck)} platform-tenant user(s) hold a branch. They would "
            "become CX_STAFF rows that violate ck_vision_staff_no_branch when "
            "it is re-added. Clear each one's branch first, then re-run. "
            f"Rows: {_describe(stuck)}"
        )

    User.objects.filter(tenant__kind="PLATFORM").update(user_type="CX_STAFF")
    User.objects.exclude(tenant__kind="PLATFORM").update(user_type="STAFF")


class Migration(migrations.Migration):
    dependencies = [
        ("vs_tenants", "0007_branch_type_and_lifecycle_blanks"),
        ("vs_user", "0008_drop_admin_user_types"),
    ]

    operations = [
        # Read-only. Refuses on a database where the two facts had already
        # drifted, before anything is dropped.
        migrations.RunPython(verify_before_drop, noop_reverse_check),

        # The rule leaves the constraint and arrives as triggers. Django
        # replays operations in reverse order on the way back, so the triggers
        # are dropped after restore_user_type has repopulated the column and
        # before the constraint is re-added - the only order in which either
        # direction works.
        migrations.RemoveConstraint(
            model_name="user", name="ck_vision_staff_no_branch",
        ),
        migrations.RunPython(install_branch_triggers, drop_branch_triggers),

        # Two conditional uid constraints become one unconditional one.
        migrations.RemoveConstraint(
            model_name="user", name="unique_uid_per_tenant",
        ),
        migrations.RemoveConstraint(
            model_name="user", name="unique_uid_vision_staff",
        ),
        migrations.AddConstraint(
            model_name="user",
            constraint=models.UniqueConstraint(
                fields=("tenant", "uid"), name="unique_uid_per_tenant",
            ),
        ),

        # The index led on a column that is about to stop existing.
        migrations.RemoveIndex(
            model_name="user", name="vs_users_us_tenant__2ea349_idx",
        ),
        migrations.AddIndex(
            model_name="user",
            index=models.Index(
                fields=["tenant", "status"], name="vs_users_us_tenant__d36ea7_idx",
            ),
        ),

        # Forward: the column goes. Reverse: RemoveField is re-added by Django
        # as the field it was, and restore_user_type below fills it.
        migrations.RunPython(noop_forward, restore_user_type),
        migrations.RemoveField(model_name="user", name="user_type"),
    ]

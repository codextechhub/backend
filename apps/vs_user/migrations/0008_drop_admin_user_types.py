"""Retire the SCHOOL_ADMIN and BRANCH_ADMIN personas.

A staff member is a staff member. A principal and a teacher are both staff, and
what separates them is the permissions in their role - so a persona that says
"admin" was a second, weaker copy of something RBAC already recorded properly.
``user_type`` is documented as inert and drives no authorization decision (the
only two permission classes that ever read it, ``IsSchoolAdmin`` and
``IsBranchAdmin``, were deleted before this migration), so nothing here changes
what anybody may do. It changes what the schema claims about them.

Both old values become ``STAFF``:

* a former ``SCHOOL_ADMIN`` keeps its NULL branch, which now means "posted
  across the whole tenant" - the same first-class value the academic structure
  and procurement documents already use;
* a former ``BRANCH_ADMIN`` keeps its branch, which is all it ever contributed
  beyond ``STAFF``.

``ck_branch_required_for_branch_level_users`` goes with them. Its only purpose
was to make ``SCHOOL_ADMIN`` the one persona permitted to be branchless; with
the personas gone, whole-tenant reach belongs to any tenant user, and is carried
by the role assignment's branch rather than by the account's.

Reversibility, honestly
-----------------------
The forward direction throws information away, and only half of it can be
recovered.

``SCHOOL_ADMIN`` is recoverable: before this migration a branchless tenant user
could not exist - the dropped constraint forbade it - so every branchless
``STAFF`` row on the way back must have been a ``SCHOOL_ADMIN``.

``BRANCH_ADMIN`` is not. After this runs, a former branch admin and a classroom
teacher at the same campus are the same row in every column. Turning every
branch-bound ``STAFF`` back into ``BRANCH_ADMIN`` would promote every teacher in
the tenant, so the reverse leaves them as ``STAFF`` rather than guess. Anyone
reversing this needs to know that, which is why it is written here and shouted
in the reverse function's own docstring.

Both directions refuse rather than guess when they meet a row they cannot
classify.
"""

import django.db.models.deletion
from django.db import migrations, models

# The four personas that survive.
SURVIVING_TYPES = ("CX_STAFF", "STAFF", "STUDENT", "PARENT")

RETIRED_TYPES = ("SCHOOL_ADMIN", "BRANCH_ADMIN")

# How many offending rows to name in a refusal before saying "and N more". A
# refusal has to be actionable, and a dump of ten thousand ids is not.
_SAMPLE = 20


def _describe(rows):
    """Render a sample of (pk, email, user_type, branch_id) tuples for a message."""
    shown = ", ".join(
        f"#{pk} {email} ({user_type}, branch={branch_id})"
        for pk, email, user_type, branch_id in rows[:_SAMPLE]
    )
    if len(rows) > _SAMPLE:
        shown += f", and {len(rows) - _SAMPLE} more"
    return shown


def _rows(queryset):
    return list(queryset.values_list("pk", "email", "user_type", "branch_id"))


def retire_admin_personas(apps, schema_editor):
    User = apps.get_model("vs_user", "User")

    # Refuse before writing anything, so a refusal leaves the table untouched.

    # A BRANCH_ADMIN with no branch claims authority over a campus it cannot
    # name. The dropped constraint made it impossible through the ORM, but a
    # bulk_create, a data migration or a hand-typed UPDATE goes round it, and
    # this migration is the wrong place to decide whether such an account meant
    # the whole school or one campus somebody forgot to set.
    branchless_branch_admins = _rows(
        User.objects.filter(user_type="BRANCH_ADMIN", branch__isnull=True)
    )
    if branchless_branch_admins:
        raise RuntimeError(
            "Refusing to retire the admin personas: "
            f"{len(branchless_branch_admins)} BRANCH_ADMIN row(s) carry no branch, "
            "so it cannot be told whether they meant one campus or the whole "
            "school. Set each one's branch, or change it to SCHOOL_ADMIN if it "
            "was always school-wide, then re-run. Rows: "
            f"{_describe(branchless_branch_admins)}"
        )

    # A CX_STAFF row cannot be one of the retired personas, but a stray value
    # from an old fixture or a hand-written INSERT can be anything at all, and
    # silently leaving it behind under a narrowed choices list is how a value
    # that no code understands survives into production.
    unclassifiable = _rows(
        User.objects.exclude(user_type__in=SURVIVING_TYPES + RETIRED_TYPES)
    )
    if unclassifiable:
        raise RuntimeError(
            "Refusing to retire the admin personas: "
            f"{len(unclassifiable)} row(s) carry a user_type this migration does "
            f"not recognise. Expected one of {SURVIVING_TYPES + RETIRED_TYPES}. "
            f"Rows: {_describe(unclassifiable)}"
        )

    # Both retired values mean the same thing now, and each keeps the branch it
    # already had: NULL stays NULL and means school-wide, a branch stays put.
    User.objects.filter(user_type__in=RETIRED_TYPES).update(user_type="STAFF")


def restore_admin_personas(apps, schema_editor):
    """Reverse: restores SCHOOL_ADMIN only. BRANCH_ADMIN cannot come back.

    A branch-bound STAFF row is indistinguishable from a former BRANCH_ADMIN,
    so every one of them is left as STAFF. Reversing this migration and then
    re-applying it is therefore not a round trip for those accounts - and it
    does not need to be, because nothing reads the value.
    """
    User = apps.get_model("vs_user", "User")

    # Before the forward migration a branchless tenant user could not exist, so
    # anything branchless and non-CX here is either a restored SCHOOL_ADMIN or
    # an account created after the personas were retired.
    branchless = User.objects.exclude(user_type="CX_STAFF").filter(branch__isnull=True)

    # STAFF is the only one the re-added constraint will accept back as
    # SCHOOL_ADMIN. A branchless STUDENT or PARENT is legal now and was not
    # before, and there is no persona to put it under: turning a child into a
    # school administrator to satisfy a constraint would be worse than stopping.
    stuck = _rows(branchless.exclude(user_type="STAFF"))
    if stuck:
        raise RuntimeError(
            "Refusing to restore the admin personas: "
            f"{len(stuck)} row(s) have no branch and are not STAFF, so they "
            "cannot be re-expressed under the old personas and would violate "
            "ck_branch_required_for_branch_level_users when it is re-added. "
            f"Give each one a branch first, then re-run. Rows: {_describe(stuck)}"
        )

    branchless.filter(user_type="STAFF").update(user_type="SCHOOL_ADMIN")


class Migration(migrations.Migration):
    dependencies = [
        ("vs_tenants", "0007_branch_type_and_lifecycle_blanks"),
        ("vs_user", "0007_user_email_unique_per_tenant"),
    ]

    operations = [
        # First, because the conversion writes branchless STAFF rows that this
        # constraint forbids. On the way back Django replays the operations in
        # reverse, so the constraint is re-added *after* restore_admin_personas
        # has put the branchless rows back under SCHOOL_ADMIN - which is the
        # only order in which either direction can succeed.
        migrations.RemoveConstraint(
            model_name="user",
            name="ck_branch_required_for_branch_level_users",
        ),
        migrations.RunPython(retire_admin_personas, restore_admin_personas),
        migrations.AlterField(
            model_name="user",
            name="branch",
            field=models.ForeignKey(
                blank=True,
                help_text='The one branch this person is posted to. NULL means "across the whole tenant" for a tenant user, and is the only legal value for Vision Staff, who belong to no tenant branch at all.',
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="users",
                to="vs_tenants.branch",
            ),
        ),
        migrations.AlterField(
            model_name="user",
            name="user_type",
            field=models.CharField(
                choices=[
                    ("CX_STAFF", "CX Staff"),
                    ("STAFF", "Staff"),
                    ("STUDENT", "Student"),
                    ("PARENT", "Parent/Guardian"),
                ],
                help_text="Inert domain marker for the person's persona. Migrates into the future profile models and MUST NEVER drive authorization - all access decisions run through tenant RBAC, not this field.",
                max_length=32,
            ),
        ),
    ]

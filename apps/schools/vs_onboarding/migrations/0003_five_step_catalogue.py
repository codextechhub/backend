"""Collapse the checklist to the five steps the approved design draws.

A product decision (2026-08-22), not a cleanup. The design shows five cards and
the catalog carried seven, so a school was reading a checklist its own design
did not describe. Three changes, and two of them lose something worth naming.

**FIRST_ADMIN and ROLE_BASELINE become one row, DEFAULT_ROLES.** Nothing stops
being checked: the merged condition is refused unless there is a working
administrator AND the role they hold grants something, and the refusal still
names which half failed. What is lost is the ability to be half done - a school
whose administrator has accepted but whose role is empty now shows one
outstanding card rather than one done and one not.

**SET_OF_BOOKS goes entirely, and this one is a real loss.** Books are
provisioned at school creation on a best-effort basis, and that step was the
thing that surfaced a school whose books silently failed - blocking go-live
until somebody looked. Without it such a school now goes live and finds out in
Finance instead. The check itself (``vs_finance.LedgerEntity`` exists for the
tenant) is untouched and still available; it is no longer asked before go-live.

**The merged row takes the weaker of the two statuses.** DONE on the new card
asserts both facts, so it may only be inherited where both rows were DONE.
Anything else lands on IN_PROGRESS if either row had been started, and
NOT_STARTED otherwise. Inheriting DONE from FIRST_ADMIN alone would mark a
school as having confirmed a role baseline nobody ever confirmed.

Readiness is deliberately not recomputed here, following 0002. Removing a
required step can make a school READY, and that transition is meant to notify
the people waiting on it; a migration runs outside the request cycle and cannot
send that. The next control-room read, task transition or go-live submission
re-evaluates and announces it.

The forward step deletes and rewrites rows, so it has no true inverse. Reversing
restores the column's choices and leaves the data as it stands.
"""
from django.db import migrations, models

#: The catalog after this migration. Written out rather than imported: a
#: migration has to keep working when constants.py moves on again.
ORDER_AFTER = {
    "DEFAULT_ROLES": 1,
    "SCHOOL_METADATA": 2,
    "ACADEMIC_STRUCTURE": 3,
    "INITIAL_DATA": 4,
    "STAFF_INVITATIONS": 5,
}

TITLES_AFTER = {
    "DEFAULT_ROLES": "Confirm Default Roles & RBAC",
    "SCHOOL_METADATA": "School Metadata Setup",
    "ACADEMIC_STRUCTURE": "Academic Structure",
    "INITIAL_DATA": "Upload Initial Datasets",
    "STAFF_INVITATIONS": "Add Staff & Invitations",
}

DONE = "DONE"
IN_PROGRESS = "IN_PROGRESS"
NOT_STARTED = "NOT_STARTED"


def _merged_status(first_admin, role_baseline) -> str:
    """The weaker of the two, because DONE now asserts both facts."""
    statuses = {row.status for row in (first_admin, role_baseline) if row}
    if statuses == {DONE}:
        return DONE
    if statuses & {DONE, IN_PROGRESS}:
        return IN_PROGRESS
    return NOT_STARTED


def collapse_to_five(apps, schema_editor):
    OnboardingTask = apps.get_model("vs_onboarding", "OnboardingTask")

    # Merge per tenant. Doing it in bulk would have to guess which pairs belong
    # together; the tenant is what makes a pair a pair.
    tenant_ids = (
        OnboardingTask.objects
        .filter(key__in=["FIRST_ADMIN", "ROLE_BASELINE"])
        .values_list("tenant_id", flat=True)
        .distinct()
    )
    for tenant_id in list(tenant_ids):
        rows = {
            row.key: row
            for row in OnboardingTask.objects.filter(
                tenant_id=tenant_id, key__in=["FIRST_ADMIN", "ROLE_BASELINE"],
            )
        }
        first_admin = rows.get("FIRST_ADMIN")
        role_baseline = rows.get("ROLE_BASELINE")
        keeper = first_admin or role_baseline
        if keeper is None:
            continue

        keeper.key = "DEFAULT_ROLES"
        keeper.title = TITLES_AFTER["DEFAULT_ROLES"]
        keeper.is_required = True
        keeper.order_index = ORDER_AFTER["DEFAULT_ROLES"]
        keeper.status = _merged_status(first_admin, role_baseline)
        if keeper.status != DONE:
            keeper.completed_at = None
        keeper.save()

        # The other half of the pair, now represented by the keeper.
        OnboardingTask.objects.filter(
            tenant_id=tenant_id, key="ROLE_BASELINE",
        ).exclude(pk=keeper.pk).delete()

    # A required row whose key is no longer in the catalog would block go-live
    # for ever: readiness counts required rows, not catalog entries.
    OnboardingTask.objects.filter(key="SET_OF_BOOKS").delete()

    for key, order_index in ORDER_AFTER.items():
        OnboardingTask.objects.filter(key=key).exclude(
            order_index=order_index, title=TITLES_AFTER[key],
        ).update(order_index=order_index, title=TITLES_AFTER[key])


class Migration(migrations.Migration):

    dependencies = [
        ("vs_onboarding", "0002_remove_branch_setup_task"),
    ]

    operations = [
        migrations.RunPython(collapse_to_five, migrations.RunPython.noop),
        migrations.AlterField(
            model_name="onboardingtask",
            name="key",
            field=models.CharField(
                choices=[
                    ("DEFAULT_ROLES", "Default roles and RBAC"),
                    ("SCHOOL_METADATA", "School metadata"),
                    ("ACADEMIC_STRUCTURE", "Academic structure"),
                    ("INITIAL_DATA", "Initial data"),
                    ("STAFF_INVITATIONS", "Staff invitations"),
                ],
                max_length=40,
            ),
        ),
    ]

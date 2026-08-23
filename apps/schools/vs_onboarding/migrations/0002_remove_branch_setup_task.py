"""Retire the BRANCH_SETUP step and close the gap it leaves in the checklist.

The step was conditional on ``School.operates_branches``, a stored flag saying
whether a school ran more than one site. The flag is gone (vs_schools 0008) and
the honest predicate in its place was the branch count, which every school
satisfies from the moment it is created. That makes the step complete before
the school can ever see it, so it is removed rather than kept as a box that
arrives already ticked.

Two things have to happen together, and doing either alone would leave the
platform worse off than before:

* the rows go. A school that was provisioned with BRANCH_SETUP holds a
  *required* task whose key is no longer in the catalog. Readiness counts
  required rows, not catalog entries, so leaving one behind would block that
  school's go-live for ever with a step no screen explains and no condition can
  clear.
* the remaining rows are renumbered to match the catalog, which now runs 1 to
  7 rather than 2 to 8. ``order_index`` is never exposed by the API, so this
  changes no payload; it keeps the stored copies equal to the catalog they are
  copies of, which is the invariant provisioning is written against.

Readiness is deliberately *not* recomputed here. A school whose last blocking
step was BRANCH_SETUP becomes READY, and that transition is meant to notify the
people waiting on it. A migration cannot send that (it runs outside the request
cycle and before ``on_commit`` means anything useful), so the state is left to
the next evaluation, which happens on the very next control-room read, task
transition or go-live submission. Stamping READY in here would suppress the
announcement rather than deliver it.

The forward step deletes rows, so it has no true inverse; reversing this
migration restores the column's choices and leaves the data as it stands.
"""
from django.db import migrations, models

#: The catalog's order after BRANCH_SETUP was removed. Written out rather than
#: imported, because a migration must keep working when constants.py moves on
#: again, and rather than shifted by one, because a subtraction only lands if
#: every row really did start where this migration assumes it did.
ORDER_AFTER = {
    "FIRST_ADMIN": 1,
    "ROLE_BASELINE": 2,
    "SCHOOL_METADATA": 3,
    "SET_OF_BOOKS": 4,
    "ACADEMIC_STRUCTURE": 5,
    "INITIAL_DATA": 6,
    "STAFF_INVITATIONS": 7,
}


def drop_branch_setup_tasks(apps, schema_editor):
    OnboardingTask = apps.get_model("vs_onboarding", "OnboardingTask")

    OnboardingTask.objects.filter(key="BRANCH_SETUP").delete()
    for key, order_index in ORDER_AFTER.items():
        OnboardingTask.objects.filter(key=key).exclude(
            order_index=order_index,
        ).update(order_index=order_index)


class Migration(migrations.Migration):

    dependencies = [
        ("vs_onboarding", "0001_initial"),
    ]

    operations = [
        migrations.RunPython(
            drop_branch_setup_tasks,
            migrations.RunPython.noop,
        ),
        migrations.AlterField(
            model_name="onboardingtask",
            name="key",
            field=models.CharField(
                choices=[
                    ("FIRST_ADMIN", "First administrator"),
                    ("ROLE_BASELINE", "Role baseline"),
                    ("SCHOOL_METADATA", "School metadata"),
                    ("SET_OF_BOOKS", "Set of books"),
                    ("ACADEMIC_STRUCTURE", "Academic structure"),
                    ("INITIAL_DATA", "Initial data"),
                    ("STAFF_INVITATIONS", "Staff invitations"),
                ],
                max_length=40,
            ),
        ),
    ]

"""Subject stops belonging to a year; where it is TAUGHT still does.

Levels, classes and subjects were all given a year in 0005. That was right for
two of the three. A subject is catalogue - Mathematics is Mathematics - and a
copy of it per year could only be tied back to its other years by name, which
breaks the first time a school tidies "Mathematics" to "Maths". The per-year
fact was already recorded elsewhere: an offering points at a level, and a level
belongs to exactly one year.

So this merges each school's per-year copies back into one row and re-points
every offering at the survivor. The survivor is the OLDEST copy, so its id is
the one anything that already pointed at a subject was most likely holding.

What can be lost: where two copies of the same subject disagreed on
description, department, branch or is_core, the survivor's values win. Nothing
about where it was taught is lost - that lives on the offerings, which all
carry over.
"""
from django.db import migrations, models
from django.db.models.functions import Lower


def merge_the_copies(apps, schema_editor):
    Subject = apps.get_model("vs_academics", "Subject")
    SubjectOffering = apps.get_model("vs_academics", "SubjectOffering")

    seen = {}
    for subject in Subject.objects.order_by("tenant_id", "id"):
        key = (subject.tenant_id, subject.name.strip().lower())
        keeper = seen.get(key)
        if keeper is None:
            seen[key] = subject
            continue
        # An offering is (subject, level) and a level is in one year, so a
        # collision here would mean two copies in the SAME year - which the old
        # constraint already forbade. Skipped rather than assumed away.
        taken = set(
            SubjectOffering.objects.filter(subject_id=keeper.id)
            .values_list("level_id", flat=True)
        )
        SubjectOffering.objects.filter(subject_id=subject.id).exclude(
            level_id__in=taken,
        ).update(subject_id=keeper.id)
        SubjectOffering.objects.filter(subject_id=subject.id).delete()
        subject.delete()

    # Codes were unique per YEAR, so two subjects that never met can now clash.
    # Suffixed rather than refused: a migration that stops on a school's data
    # leaves them unable to deploy, and a code is a label rather than a key.
    used = {}
    for subject in Subject.objects.order_by("tenant_id", "id"):
        key = (subject.tenant_id, (subject.code or "").strip().lower())
        if not key[1] or key not in used:
            used[key] = subject.id
            continue
        for n in range(2, 100):
            candidate = f"{subject.code[:18]}{n}"
            if (subject.tenant_id, candidate.lower()) not in used:
                subject.code = candidate
                subject.save(update_fields=["code"])
                used[(subject.tenant_id, candidate.lower())] = subject.id
                break


class Migration(migrations.Migration):
    """Data only.

    Split from the schema change because Postgres refuses to ALTER a table
    with pending trigger events - deleting the duplicate rows and dropping the
    column in one transaction fails on the second half. Two migrations, two
    transactions, and the merge is committed before the column goes.
    """

    dependencies = [
        ("vs_academics", "0005_academic_year_owns_the_structure"),
    ]

    operations = [
        migrations.RunPython(merge_the_copies, migrations.RunPython.noop),
    ]

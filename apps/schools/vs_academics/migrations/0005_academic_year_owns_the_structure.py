"""Levels, subjects and classes belong to an academic year.

Departments and programmes stay shared: they are the spine a school keeps, and
"Sciences" is the same department in 2026 and 2027. What a school rebuilds each
year is the content hanging off that spine - the levels it runs, the subjects it
teaches and the classes pupils sit in - so those three carry the year.

Three steps, because a non-null column cannot be added to populated tables:
add it nullable, fill it in, then close it. The backfill puts every existing row
in the school's ACTIVE year, which is the only year those rows can have meant -
they were written by a school that had exactly one live year and no way to say
otherwise.

A school holding structure and no session at all is given one. That is not
inventing data: it HAS been running a year, it simply had nowhere to record it,
and the alternative is deleting its structure or leaving a null that means
"belongs to every year" - the ambiguity this migration exists to remove.
"""
from __future__ import annotations

import datetime as dt

from django.db import migrations, models
import django.db.models.deletion
from django.db.models.functions import Lower


def _year_label(today):
    """The academic year a date falls in, Nigerian convention: Sept to July."""
    start = today.year if today.month >= 9 else today.year - 1
    return f"{start}/{start + 1}"


def fill_in_the_year(apps, schema_editor):
    AcademicSession = apps.get_model("vs_academics", "AcademicSession")
    Level = apps.get_model("vs_academics", "Level")
    Subject = apps.get_model("vs_academics", "Subject")
    SchoolClass = apps.get_model("vs_academics", "SchoolClass")

    today = dt.date.today()
    tenants = set()
    for model in (Level, Subject, SchoolClass):
        tenants.update(model.objects.values_list("tenant_id", flat=True).distinct())

    for tenant_id in tenants:
        session = (
            AcademicSession.objects.filter(tenant_id=tenant_id, status="ACTIVE").first()
            or AcademicSession.objects.filter(tenant_id=tenant_id)
            .order_by("-start_date").first()
        )
        if session is None:
            label = _year_label(today)
            session = AcademicSession.objects.create(
                tenant_id=tenant_id,
                name=label,
                start_date=dt.date(int(label[:4]), 9, 1),
                end_date=dt.date(int(label[:4]) + 1, 7, 31),
                status="ACTIVE",
                is_school_wide=True,
            )
        for model in (Level, Subject, SchoolClass):
            model.objects.filter(tenant_id=tenant_id).update(session=session)


def unfill(apps, schema_editor):
    """Reversible: the column goes, so nothing has to be put back."""


class Migration(migrations.Migration):

    dependencies = [
        ("vs_academics", "0004_alter_subjectoffering_level"),
    ]

    operations = [
        # ── 1. Add it nullable so existing rows survive the ALTER ──────────
        migrations.AddField(
            model_name="level",
            name="session",
            field=models.ForeignKey(
                null=True, on_delete=django.db.models.deletion.PROTECT,
                related_name="levels", to="vs_academics.academicsession",
            ),
        ),
        migrations.AddField(
            model_name="subject",
            name="session",
            field=models.ForeignKey(
                null=True, on_delete=django.db.models.deletion.PROTECT,
                related_name="subjects", to="vs_academics.academicsession",
            ),
        ),
        migrations.AddField(
            model_name="schoolclass",
            name="session",
            field=models.ForeignKey(
                null=True, on_delete=django.db.models.deletion.PROTECT,
                related_name="classes", to="vs_academics.academicsession",
            ),
        ),

        # ── 2. Fill it in ─────────────────────────────────────────────────
        migrations.RunPython(fill_in_the_year, unfill),

        # ── 3. Close it. A null year would mean "every year", which is the
        #      ambiguity this whole change removes. ─────────────────────────
        migrations.AlterField(
            model_name="level",
            name="session",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.PROTECT,
                related_name="levels", to="vs_academics.academicsession",
            ),
        ),
        migrations.AlterField(
            model_name="subject",
            name="session",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.PROTECT,
                related_name="subjects", to="vs_academics.academicsession",
            ),
        ),
        migrations.AlterField(
            model_name="schoolclass",
            name="session",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.PROTECT,
                related_name="classes", to="vs_academics.academicsession",
            ),
        ),

        # ── 4. The names come back every September, so uniqueness is per
        #      YEAR rather than for ever. ───────────────────────────────────
        migrations.RemoveConstraint(model_name="level", name="uq_academic_level_name"),
        migrations.RemoveConstraint(model_name="level", name="uq_academic_level_code"),
        migrations.RemoveConstraint(model_name="level", name="uq_academic_level_order"),
        migrations.RemoveConstraint(model_name="subject", name="uq_academic_subject_name"),
        migrations.RemoveConstraint(model_name="subject", name="uq_academic_subject_code"),
        migrations.RemoveConstraint(model_name="schoolclass", name="uq_academic_class_code"),

        migrations.AddConstraint(
            model_name="level",
            constraint=models.UniqueConstraint(
                Lower("name"), "program", "session", name="uq_academic_level_name",
            ),
        ),
        migrations.AddConstraint(
            model_name="level",
            constraint=models.UniqueConstraint(
                Lower("code"), "program", "session", name="uq_academic_level_code",
            ),
        ),
        migrations.AddConstraint(
            model_name="level",
            constraint=models.UniqueConstraint(
                fields=["program", "session", "order_index"],
                name="uq_academic_level_order",
            ),
        ),
        migrations.AddConstraint(
            model_name="subject",
            constraint=models.UniqueConstraint(
                Lower("name"), "tenant", "session", name="uq_academic_subject_name",
            ),
        ),
        migrations.AddConstraint(
            model_name="subject",
            constraint=models.UniqueConstraint(
                Lower("code"), "tenant", "session", name="uq_academic_subject_code",
            ),
        ),
        migrations.AddConstraint(
            model_name="schoolclass",
            constraint=models.UniqueConstraint(
                Lower("code"), "tenant", "session", name="uq_academic_class_code",
            ),
        ),
    ]

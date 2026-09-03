"""Drop the three declared seat capacities from a school's package setup.

``student_capacity``, ``teacher_capacity`` and ``admin_capacity`` were typed
into the last step of school registration, checked once against the chosen
plan's own maximums, stored, and shown on the school detail page. Nothing ever
compared them to how many students, teachers or admins a school actually had -
there is no code path that reads one to refuse anything - so a school
registered with a 500-student capacity enrolled its 501st with nothing in the
way. Three columns that read like a limit and were a note.

A ``PackagePlan`` keeps ``max_students`` and its siblings. Those describe the
PLAN, are set by CodeX rather than typed per school, and are the numbers a
commercial limit would eventually be built from.

Irreversible in the way that matters: reversing recreates the columns, not the
numbers that were in them. They were declarations rather than measurements, so
there is nothing to recompute them from and nothing that would read them.
"""

from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [
        ("vs_schools", "0010_seed_required_admin_role_templates"),
    ]

    operations = [
        migrations.RemoveField(
            model_name="schoolpackagesetup",
            name="admin_capacity",
        ),
        migrations.RemoveField(
            model_name="schoolpackagesetup",
            name="student_capacity",
        ),
        migrations.RemoveField(
            model_name="schoolpackagesetup",
            name="teacher_capacity",
        ),
    ]

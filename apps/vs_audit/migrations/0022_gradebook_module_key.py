"""The audit module list gains GRADEBOOK, for the assessment setup and results a school keeps.

A choice list change only; no column or row changes.
"""

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("vs_audit", "0021_attendance_module_key"),
    ]

    operations = [
        migrations.AlterField(
            model_name="auditevent",
            name="module_key",
            field=models.CharField(
                choices=[
                    ("ONBOARDING", "Onboarding"),
                    ("IDENTITY", "Identity & Auth"),
                    ("USER", "User Management"),
                    ("RBAC", "Roles & Permissions"),
                    ("IMPORT", "Data Import"),
                    ("CONFIG", "System Configuration"),
                    ("FINANCE", "Finance"),
                    ("PROCUREMENT", "Procurement"),
                    ("SCHOOL", "School Management"),
                    ("ACADEMICS", "Academic Structure"),
                    ("STUDENT", "Student Management"),
                    ("STAFF", "Staff Management"),
                    ("ATTENDANCE", "Attendance Management"),
                    ("GRADEBOOK", "Gradebook and Assessments"),
                    ("BRANCH", "Branch Management"),
                    ("EXPORTS", "Export Centre"),
                    ("SYSTEM", "System"),
                    ("PLATFORM", "Platform Operations"),
                    ("WORKFLOW", "Approvals & Workflow"),
                ],
                db_index=True,
                max_length=40,
            ),
        ),
        migrations.AlterField(
            model_name="compliancerule",
            name="module_key",
            field=models.CharField(
                blank=True,
                choices=[
                    ("ONBOARDING", "Onboarding"),
                    ("IDENTITY", "Identity & Auth"),
                    ("USER", "User Management"),
                    ("RBAC", "Roles & Permissions"),
                    ("IMPORT", "Data Import"),
                    ("CONFIG", "System Configuration"),
                    ("FINANCE", "Finance"),
                    ("PROCUREMENT", "Procurement"),
                    ("SCHOOL", "School Management"),
                    ("ACADEMICS", "Academic Structure"),
                    ("STUDENT", "Student Management"),
                    ("STAFF", "Staff Management"),
                    ("ATTENDANCE", "Attendance Management"),
                    ("GRADEBOOK", "Gradebook and Assessments"),
                    ("BRANCH", "Branch Management"),
                    ("EXPORTS", "Export Centre"),
                    ("SYSTEM", "System"),
                    ("PLATFORM", "Platform Operations"),
                    ("WORKFLOW", "Approvals & Workflow"),
                ],
                help_text="Optional: restrict rule to one module.",
                max_length=40,
            ),
        ),
    ]

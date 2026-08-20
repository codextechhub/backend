"""Drop the vestigial ``role_label`` column from both primary-admin links.

The column defaulted to the literal strings "SCHOOL_ADMIN" and "BRANCH_ADMIN",
which were persona names on the retired ``User.user_type``. Since that field
was removed the defaults named values no enum has, and the column was written
in five places and read by none: nothing branched on it, filtered on it, or
granted from it. ``provision_admin_user`` takes its role from the RBAC
template key, never from here.

It was redundant regardless: ``school_role``/``branch_role`` on the same rows
already carry the human job title ("IT Head", "Head Teacher") and the RBAC
role carries the authority. Re-defaulting it to a new string would only
re-mint a persona in free text.

Reversible: ``RemoveField`` re-adds the column from migration state, so a
backwards run restores it with its original definition (blank, defaulted).
The values it held were constants, so nothing unrecoverable is lost.
"""


from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [
        ("vs_schools", "0008_remove_school_operates_branches"),
    ]

    operations = [
        migrations.RemoveField(
            model_name="branchprimaryadmin",
            name="role_label",
        ),
        migrations.RemoveField(
            model_name="schoolprimaryadmin",
            name="role_label",
        ),
    ]

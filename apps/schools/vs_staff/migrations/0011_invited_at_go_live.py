"""Staff imported during setup read "Invited at go-live" until the school opens.

The employment status (and the employment log's two status columns) gain
``AWAITING_GO_LIVE``: somebody imported while their school was still being set
up, whose invitation goes out when it goes live. Nothing existing changes
value. Reversible: the choice is withdrawn; a record still holding it keeps the
stored value, which the reversed code does not offer, so take the school live
or revoke those invitations first.
"""
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("vs_staff", "0010_hire_approval_and_leave_allowance"),
    ]

    operations = [
        migrations.AlterField(
            model_name="staffemploymentevent",
            name="from_status",
            field=models.CharField(
                blank=True,
                choices=[
                    ("PENDING_APPROVAL", "Awaiting approval"),
                    ("AWAITING_GO_LIVE", "Invited at go-live"),
                    ("INVITED", "Invited"),
                    ("ACTIVE", "Active"),
                    ("ON_LEAVE", "On Leave"),
                    ("SUSPENDED", "Suspended"),
                    ("RESIGNED", "Resigned"),
                    ("TERMINATED", "Terminated"),
                ],
                default="",
                max_length=16,
            ),
        ),
        migrations.AlterField(
            model_name="staffemploymentevent",
            name="to_status",
            field=models.CharField(
                choices=[
                    ("PENDING_APPROVAL", "Awaiting approval"),
                    ("AWAITING_GO_LIVE", "Invited at go-live"),
                    ("INVITED", "Invited"),
                    ("ACTIVE", "Active"),
                    ("ON_LEAVE", "On Leave"),
                    ("SUSPENDED", "Suspended"),
                    ("RESIGNED", "Resigned"),
                    ("TERMINATED", "Terminated"),
                ],
                max_length=16,
            ),
        ),
        migrations.AlterField(
            model_name="staffprofile",
            name="employment_status",
            field=models.CharField(
                choices=[
                    ("PENDING_APPROVAL", "Awaiting approval"),
                    ("AWAITING_GO_LIVE", "Invited at go-live"),
                    ("INVITED", "Invited"),
                    ("ACTIVE", "Active"),
                    ("ON_LEAVE", "On Leave"),
                    ("SUSPENDED", "Suspended"),
                    ("RESIGNED", "Resigned"),
                    ("TERMINATED", "Terminated"),
                ],
                default="INVITED",
                max_length=16,
            ),
        ),
    ]

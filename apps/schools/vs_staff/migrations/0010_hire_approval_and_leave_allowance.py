"""A hire can wait for approval, and a leave request records how far past its allowance it goes.

* ``StaffProfile.employment_status`` (and the employment log's two status
  columns) gain ``PENDING_APPROVAL``, "Awaiting approval": a person added at a
  school that approves each hire before inviting it.
* ``StaffProfile.invite_on_approval`` says whether that approval sends the
  invitation email, default yes.
* ``LeaveRequest.over_allowance_by``: days past the leave type's allowance for
  the session, default 0, which is what every existing request reads as.

Nothing existing changes value. Reversible: the two columns are dropped and the
choice is withdrawn; a record still awaiting approval keeps the stored value,
which the reversed code does not offer, so approve or reject those first.
"""
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("vs_staff", "0009_staff_settings"),
    ]

    operations = [
        migrations.AddField(
            model_name="leaverequest",
            name="over_allowance_by",
            field=models.PositiveSmallIntegerField(default=0),
        ),
        migrations.AddField(
            model_name="staffprofile",
            name="invite_on_approval",
            field=models.BooleanField(default=True),
        ),
        migrations.AlterField(
            model_name="staffemploymentevent",
            name="from_status",
            field=models.CharField(
                blank=True,
                choices=[
                    ("PENDING_APPROVAL", "Awaiting approval"),
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

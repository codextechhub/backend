"""Nobody is stored as On Leave any more; it is what a row reads as.

Going on leave stopped being a decision an administrator records about
somebody's employment and became what an approved leave request means while its
dates are running. The serializer derives it at read time, so the column no
longer needs to hold it - and must not, because a stored value has no way back:
approval is an event and code can hang off it, but a leave ENDING is not one and
there is no scheduler here to notice. A row set On Leave in August stays On
Leave the following March.

So every row still holding it moves to ACTIVE, which is what it means: they work
here. Whether they are away today is answered by their leave, and answered again
every time the row is read.

**The employment EVENTS are left exactly as they are.** They are the log of what
was decided at the time, under the rule in force at the time, and rewriting them
would make the trail disagree with itself - the record would say she was never
moved to On Leave when a screen once showed that she had been. The value stays
in the vocabulary for their sake.
"""
from django.db import migrations


def read_rather_than_stored(apps, schema_editor):
    StaffProfile = apps.get_model("vs_staff", "StaffProfile")
    StaffProfile.objects.filter(employment_status="ON_LEAVE").update(
        employment_status="ACTIVE",
    )


def back_to_stored(apps, schema_editor):
    """Restore the column from the leave that was running when this ran.

    Not a true inverse and cannot be one: the rows that were ON_LEAVE without a
    leave request behind them are indistinguishable afterwards from anybody else
    who is ACTIVE. This puts back the ones the evidence supports, which is the
    honest half, and is here so the migration is reversible in a development
    database rather than because anybody should go back.
    """
    from django.utils import timezone

    StaffProfile = apps.get_model("vs_staff", "StaffProfile")
    LeaveRequest = apps.get_model("vs_staff", "LeaveRequest")
    today = timezone.localdate()
    running = LeaveRequest.objects.filter(
        status="APPROVED", start_date__lte=today, end_date__gte=today,
    ).values_list("staff_id", flat=True)
    StaffProfile.objects.filter(
        pk__in=list(running), employment_status="ACTIVE",
    ).update(employment_status="ON_LEAVE")


class Migration(migrations.Migration):

    dependencies = [
        ("vs_staff", "0002_bind_staff_files"),
    ]

    operations = [
        migrations.RunPython(read_rather_than_stored, back_to_stored),
    ]

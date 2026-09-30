"""Every leave request already filed names the person taking the leave.

The approval engine keeps, on each request, the person it is about, so the
administrators' list can be filtered by them without opening any document. A
leave request states it at submission from here on; this fills it in for the
requests filed before, from the member of staff each leave row names. The
engine does not know what a leave request is, which is why this lives here and
not in the engine's own migration.
"""
from django.db import migrations

LEAVE_DOCUMENT_TYPE = "schools.leave_request"


def name_the_person_on_leave(apps, schema_editor):
    LeaveRequest = apps.get_model("vs_staff", "LeaveRequest")
    WorkflowInstance = apps.get_model("vs_workflow", "WorkflowInstance")

    person_on_leave = {
        str(pk): user_id
        for pk, user_id in LeaveRequest.objects.values_list("pk", "staff__user_id")
    }
    pending = WorkflowInstance.objects.filter(
        document_type=LEAVE_DOCUMENT_TYPE, request_for__isnull=True,
    ).only("pk", "document_object_id")
    changed = []
    for instance in pending.iterator():
        user_id = person_on_leave.get(instance.document_object_id)
        if user_id is not None:
            instance.request_for_id = user_id
            changed.append(instance)
    WorkflowInstance.objects.bulk_update(changed, ["request_for"], batch_size=500)


class Migration(migrations.Migration):
    dependencies = [
        ("vs_staff", "0012_no_server_day_default"),
        ("vs_workflow", "0022_approvers_change_while_a_request_waits"),
    ]

    operations = [
        migrations.RunPython(name_the_person_on_leave, migrations.RunPython.noop),
    ]

"""Say the task's name plainly instead of quoting it.

Both bodies wrapped ``{{ label }}`` in single quotes, which reads as scare
quotes once a real name is substituted in:

    Your background task 'Confirm Default Roles & RBAC' finished successfully.

Nothing else in the product quotes a name that way - console-fe's toasts read
``Security settings saved`` and ``Platform profile saved`` - and the quotes are
worst exactly where these appear, in a notification tray where the reader is
skimming.

``task.failed`` also SHOUTED, which is the other half of the same problem.

A migration rather than a re-seed because the template seeder is
``get_or_create`` by default: it leaves an existing row alone so that an
administrator's own edits are never overwritten. That is the right default and
it means shipped environments would otherwise keep the old wording forever.

Only rewrites a body that still matches the old text exactly, so anybody who
HAS edited these keeps their version. Reversible.
"""
from django.db import migrations


#: event key -> (old body, new body)
COPY = {
    "task.completed": (
        "Your background task '{{ label }}' finished successfully.",
        "{{ label }} finished successfully.",
    ),
    "task.failed": (
        "Your background task '{{ label }}' FAILED. {{ error }}",
        "{{ label }} did not finish. {{ error }}",
    ),
}


def _apply(apps, schema_editor, *, forward):
    NotificationTemplate = apps.get_model("vs_notifications", "NotificationTemplate")

    for key, (old, new) in COPY.items():
        want, replacement = (old, new) if forward else (new, old)
        NotificationTemplate.objects.filter(
            event_type__key=key, body=want,
        ).update(body=replacement)


def forward(apps, schema_editor):
    _apply(apps, schema_editor, forward=True)


def backward(apps, schema_editor):
    _apply(apps, schema_editor, forward=False)


class Migration(migrations.Migration):

    dependencies = [
        ("vs_notifications", "0008_seed_notification_event_type_registry"),
    ]

    operations = [
        migrations.RunPython(forward, backward),
    ]

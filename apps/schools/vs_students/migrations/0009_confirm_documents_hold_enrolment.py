"""The confirmation documents' description says they hold a direct enrolment too.

``applicants.documents.required_to_confirm`` holds a child off the roll until
the documents are on their record: an applicant at confirmation, a child
enrolled directly in the same save, and every row of the student import, which
comes in as an applicant. The description a school reads beside the setting
said that a direct enrolment is not checked, which is no longer true.

The description is replaced only where it still reads as ``0008`` wrote it, so
one edited since is left alone. Reversible: the old description is put back
under the same condition.
"""
from django.db import migrations

KEY = "applicants.documents.required_to_confirm"

AS_WRITTEN = (
    "The documents an applicant must have on their record before they can "
    "be confirmed as enrolled, on every route that confirms one. Empty "
    "means confirming never waits for a document. Enrolling a child "
    "directly, without saving them as an applicant first, is not checked."
)

AS_IT_NOW_READS = (
    "The documents a child must have on their record before joining the roll: "
    "an applicant before being confirmed as enrolled, on every route that "
    "confirms one, and a child enrolled directly, whose documents are sent "
    "with the enrolment. At a school with any, the student import brings each "
    "row in as an applicant. Empty means nothing waits for a document."
)


def _swap(apps, old, new):
    definition_model = apps.get_model("vs_config", "ConfigurationDefinition")
    definition_model.objects.filter(key=KEY, description=old).update(description=new)


def forwards(apps, schema_editor):
    _swap(apps, AS_WRITTEN, AS_IT_NOW_READS)


def backwards(apps, schema_editor):
    _swap(apps, AS_IT_NOW_READS, AS_WRITTEN)


class Migration(migrations.Migration):
    dependencies = [
        ("vs_students", "0008_admission_stages"),
    ]

    operations = [migrations.RunPython(forwards, backwards)]

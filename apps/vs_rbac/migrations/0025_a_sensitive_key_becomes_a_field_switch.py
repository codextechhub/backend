"""Turn today's field guarding permission keys into Field Access switches.

Field level security lives in permission keys checked inside serializers. Field
Access replaces them with per-role Read and Write switches, and release day must
change nobody's access, so the switches have to say exactly what the keys say
before anything starts reading them.

Nothing is enforced yet and nothing is removed. Every key stays registered,
granted and obeyed; this writes the rows the next slice will start reading, so
that switching enforcement on is a change of mechanism and not of access.

What it writes, and where the mapping comes from, is
:mod:`vs_rbac.field_conversion`:

* a ``RoleFieldAccess`` row for every tenant role whose answer differs from the
  field's default, including the explicit Write off rows a field with an open
  default needs;
* a ``PrebuiltRoleFieldAccess`` row for each Codex default, so a school created
  afterwards starts where an existing one lands;
* a ``UserFieldAccessOverride`` for each personal permission exception on a
  converted key, carrying its mode, expiry and author.

Running it again writes nothing: a row already present is left as it is, so an
administrator's own decision survives a rerun. Reversing it recomputes the same
plan and deletes those rows, and deletes the exceptions by the reason the
conversion gives them.
"""
from django.db import migrations

from vs_rbac.field_conversion import reverse_conversion, run_conversion


def convert(apps, schema_editor):
    run_conversion(apps=apps)


def unconvert(apps, schema_editor):
    reverse_conversion(apps=apps)


class Migration(migrations.Migration):
    dependencies = [
        ("vs_rbac", "0024_a_field_can_be_set_as_a_record_is_created"),
    ]
    operations = [
        migrations.RunPython(convert, unconvert),
    ]

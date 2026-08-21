"""Drop ``EntityAuditTrail``'s stored rollup. The three columns were a lie.

``event_count``, ``first_event_at`` and ``last_event_at`` were maintained by a
``register_event`` that only ever incremented. Nothing ever decremented, so what
a platform reviewer read was a high-water mark. Measured in ``cx_db`` on the day
this was written: 889 trails, 11 disagreeing with the events beneath them,
``User:1`` storing 1690 against 399 real events, and 10 trails describing
entities with no events at all.

**The numbers are dropped, not repaired**, and that is the point. Migration 0003
in this same app deleted every ``IMPERSONATED_REQUEST`` event and left the
counters standing - that is where most of ``User:1``'s 1291 phantom events came
from - and those events are gone, so there is nothing to recount them from. Even
where a recount were possible it would only reset the clock: the next deletion
starts the drift again, which is why migration 0004, one migration later on this
same table, had to carry 25 lines of hand-written recount of its own. A total
that must be re-derived after every deletion is a total nobody should read.

The counters are computed from ``AuditEvent`` at read time now, in one grouped
query per page, over the ``(entity_type, entity_id, event_at)`` index that
migration 0002 put on that table. See ``vs_audit.scoping.visible_trail_counters``.

Data safety: no ``AuditEvent`` row is touched, and the catalogue rows themselves
- which entities have been audited, and what each is called - are kept intact.
Only the three derived columns and the index on ``last_event_at`` go.

Reversibility: Django reverses this to ``AddField``, restoring the schema with
``event_count = 0`` and both timestamps NULL. The old values do not come back,
and should not: they were never reconcilable with the table beneath them.
"""

from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [
        ("vs_audit", "0010_alter_auditevent_action_type"),
    ]

    operations = [
        migrations.RemoveIndex(
            model_name="entityaudittrail",
            name="vs_audit_en_last_ev_64561f_idx",
        ),
        migrations.RemoveField(
            model_name="entityaudittrail",
            name="event_count",
        ),
        migrations.RemoveField(
            model_name="entityaudittrail",
            name="first_event_at",
        ),
        migrations.RemoveField(
            model_name="entityaudittrail",
            name="last_event_at",
        ),
    ]

"""Every class's class teachers so far, read back from the audit trail.

Each change of class teacher has been audited with the holder before and after
(``metadata.from_staff_id`` and ``to_staff_id``), so the terms already served
can be rebuilt rather than starting the history empty. A holder already in
place before the first audited change gets a term with an unknown start.

The trail is trusted only where it agrees with itself and with the class. If one
change starts from somebody other than the holder the change before left, or
the last holder it names is not the class's class teacher, a change was made
without being audited, and every date it would give is suspect: that class keeps only its
current holder, with an unknown start. A person whose staff record no longer
exists cannot be named, so a class whose trail names one is treated the same
way.
"""
from django.db import migrations


def rebuild_terms(apps, schema_editor):
    SchoolClass = apps.get_model("vs_academics", "SchoolClass")
    StaffProfile = apps.get_model("vs_staff", "StaffProfile")
    ClassTeacherTerm = apps.get_model("vs_staff", "ClassTeacherTerm")
    AuditEvent = apps.get_model("vs_audit", "AuditEvent")

    staff_ids = set(StaffProfile.objects.values_list("pk", flat=True))
    events = {}
    trail = (
        AuditEvent.objects.filter(entity_type="SchoolClass", metadata__class_teacher=True)
        .order_by("event_at", "id")
        .values_list("entity_id", "event_at", "actor_user_id", "metadata")
    )
    for entity_id, event_at, actor_id, metadata in trail.iterator():
        events.setdefault(entity_id, []).append((event_at, actor_id, metadata or {}))

    classes = SchoolClass.objects.filter(
        pk__in=[int(pk) for pk in events if pk.isdigit()],
    ) | SchoolClass.objects.filter(class_teacher__isnull=False)

    terms = []
    for school_class in classes.distinct().iterator():
        rebuilt, holder, broken = [], None, False
        for event_at, actor_id, metadata in events.get(str(school_class.pk), []):
            before, after = metadata.get("from_staff_id"), metadata.get("to_staff_id")
            if rebuilt or holder:
                broken |= (holder or {}).get("staff_id") != before
            if holder is None and before is not None:
                holder = {"staff_id": before, "started_at": None, "started_by_id": None}
            if holder is not None:
                rebuilt.append({**holder, "ended_at": event_at, "ended_by_id": actor_id})
                holder = None
            if after is not None:
                holder = {"staff_id": after, "started_at": event_at, "started_by_id": actor_id}

        current = school_class.class_teacher_id
        named = [term["staff_id"] for term in rebuilt] + ([holder["staff_id"]] if holder else [])
        trusted = (
            not broken
            and (holder or {}).get("staff_id") == current
            and set(named) <= staff_ids
        )
        if not trusted:
            rebuilt = []
            holder = (
                {"staff_id": current, "started_at": None, "started_by_id": None}
                if current is not None else None
            )
        if holder is not None:
            rebuilt.append({**holder, "ended_at": None, "ended_by_id": None})
        terms.extend(
            ClassTeacherTerm(
                tenant_id=school_class.tenant_id, school_class_id=school_class.pk, **term,
            )
            for term in rebuilt
        )

    ClassTeacherTerm.objects.bulk_create(terms, batch_size=500)


class Migration(migrations.Migration):
    dependencies = [
        ("vs_staff", "0015_class_teacher_terms"),
        ("vs_audit", "0024_messages_module_key"),
    ]

    operations = [
        migrations.RunPython(rebuild_terms, migrations.RunPython.noop),
    ]

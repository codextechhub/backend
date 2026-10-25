"""The Class teacher switch, as other modules' payloads apply it.

``academics.classes.class_teacher`` is declared and enforced on the class's own
serializers (:mod:`.field_access`). The same person is also named on staff
screens other modules build: the attendance hub, report and register header
(``class_teacher_name``), the staff register's role line, and the gradebook
hub, broadsheet and approvals list (``class_teacher``). A serializer enforces
one resource, so those carry :class:`ClassTeacherReadGate` instead, and a
hand-built payload asks :func:`may_read_class_teacher`.

Two things are deliberately outside it. A person's own designation: a
teacher's own Today list and results hub name the classes they look after, and
hiding that would hide their own work from them, as the leave switches never
hide a person's own leave. And documents for families: a report card prints the
class teacher who signed it, and the student and parent portals show what was
published. Families hold no role for a switch to reach, and a document already
issued must read the same when printed again.
"""
from __future__ import annotations

from vs_rbac.field_enforcement import can_read

#: The switch, under its full registry key.
CLASS_TEACHER_FIELD = "academics.classes.class_teacher"


def may_read_class_teacher(request) -> bool:
    """Whether the request's reader may read who a class's class teacher is."""
    return can_read(request, CLASS_TEACHER_FIELD)


class ClassTeacherReadGate:
    """Removes the class teacher from a serializer of another module.

    ``class_teacher_names`` lists the keys that carry the person in this
    payload. They are removed, not blanked: a null already means the class has
    nobody, and a hidden name must not read as an empty class.
    """

    class_teacher_names: tuple[str, ...] = ("class_teacher",)

    def to_representation(self, instance):
        data = super().to_representation(instance)
        if not may_read_class_teacher(self.context.get("request")):
            for name in self.class_teacher_names:
                data.pop(name, None)
        return data

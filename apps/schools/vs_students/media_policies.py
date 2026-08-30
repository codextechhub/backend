"""Who may read a student's photograph or a document held against them.

There is no default policy: a file whose owning model has registered nothing is
not served at all, because the alternative is that adding a ``FileField``
silently publishes it to every account on the platform. So this module is what
makes these files readable, and it is deliberately narrow.

The file's own tenant column settles the boundary between schools before this
runs. What is left is the boundary **inside** a school, which that column cannot
see: a birth certificate belongs to one child at one branch, and a caller pinned
to another branch may not read it even though they hold a valid session at the
same school.
"""
from __future__ import annotations

from core.media import register_policy

from .models import Student, StudentDocument


def _may_read_student_file(request, student) -> bool:
    from vs_rbac.permissions import has_permission

    from .constants import PERM_VIEW
    from .services.scoping import scope_students

    tenant = getattr(request, "tenant", None)
    user = getattr(request, "user", None)
    if tenant is None or user is None or student.tenant_id != getattr(tenant, "pk", None):
        return False
    if not has_permission(user, PERM_VIEW, tenant=tenant):
        return False
    # The branch check, made against the same scoped queryset the list uses so
    # a photograph cannot be reachable by a caller the profile is not.
    return scope_students(
        Student.objects.filter(tenant=tenant, pk=student.pk), user, tenant,
    ).exists()


def _may_read_document(request, doc) -> bool:
    return _may_read_student_file(request, doc.student)


def register() -> None:
    register_policy(Student, _may_read_student_file)
    register_policy(StudentDocument, _may_read_document)

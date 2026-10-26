"""Who may read a student's or a guardian's photograph, or a document held
against a child.

There is no default policy: a file whose owning model has registered nothing is
not served at all, because the alternative is that adding a ``FileField``
silently publishes it to every account on the platform. So this module is what
makes these files readable, and it is deliberately narrow.

The file's own tenant column settles the boundary between schools before this
runs. What is left is the boundary **inside** a school, which that column cannot
see: a birth certificate belongs to one child at one branch, and a caller pinned
to another branch may not read it even though they hold a valid session at the
same school.

Three kinds of caller read a child's file, as independent alternatives: staff
holding the student view key within their branch scope, the pupil themselves,
and a guardian of the pupil (``is_portal_guardian_of``, the parent portal's own
rule). The last two reach the passport photograph only. A guardian's reach is
the link to the child, so a teacher who is a parent at another branch reads
that child as a parent while her staff scope still decides every other pupil.
"""
from __future__ import annotations

from core.media import register_policy

from .models import Guardian, Student, StudentDocument
from .services.guardians import is_portal_guardian_of


def _request_parts(request, student):
    """The request's tenant and user, or None when the caller is not at the pupil's school."""
    tenant = getattr(request, "tenant", None)
    user = getattr(request, "user", None)
    if tenant is None or user is None or student.tenant_id != getattr(tenant, "pk", None):
        return None
    return tenant, user


def _may_read_as_staff(request, student) -> bool:
    """Staff holding the student view key, within their branch scope."""
    from vs_rbac.permissions import has_permission

    from .constants import PERM_VIEW
    from .services.scoping import scope_students

    parts = _request_parts(request, student)
    if parts is None:
        return False
    tenant, user = parts
    if not has_permission(user, PERM_VIEW, tenant=tenant):
        return False
    # The branch check, made against the same scoped queryset the list uses so
    # a photograph cannot be reachable by a caller the profile is not.
    return scope_students(
        Student.objects.filter(tenant=tenant, pk=student.pk), user, tenant,
    ).exists()


def _may_read_student_file(request, student) -> bool:
    """The pupil, a guardian the parent portal shows this pupil to, or staff in scope.

    The three are independent alternatives. A teacher who is also the parent
    of a pupil at another branch reads that one pupil as a parent, and her
    staff branch limits still decide every other pupil.
    """
    parts = _request_parts(request, student)
    if parts is None:
        return False
    tenant, user = parts
    if _is_own_record(user, student):
        return True
    if is_portal_guardian_of(user, student, tenant):
        return True
    return _may_read_as_staff(request, student)


def _is_own_record(user, student) -> bool:
    """Whether *user* is the pupil this record belongs to, signed in as themselves."""
    return bool(
        student.user_id is not None
        and getattr(user, "pk", None) == student.user_id
        and getattr(user, "is_active", False)
    )


def _may_read_document(request, doc) -> bool:
    """Staff by the student rules; the pupil and their guardians, the photograph only.

    A pupil sees their own face in the portal, and a parent sees their child's
    in the parent app. Every other document held against a child (a birth
    certificate, a medical letter) is the school's file about the child, not
    something either reads through a portal: for those, only staff in scope
    are admitted, and a pupil is never admitted.
    """
    from .constants import DocumentType

    if doc.document_type == DocumentType.PASSPORT_PHOTO:
        return _may_read_student_file(request, doc.student)
    if _is_own_record(getattr(request, "user", None), doc.student):
        return False
    return _may_read_as_staff(request, doc.student)


def _may_read_guardian_photo(request, guardian) -> bool:
    """A guardian is reachable by whoever reaches one of their children.

    **The branch check cannot be made on the guardian.** The row carries no
    branch on purpose - one household serves siblings at two branches of one
    school - so there is no column here to compare against a caller's binding.
    The answer that IS available is the one the guardian page itself gives:
    which of this person's children does this caller see? A Lekki administrator
    reaching none of them has no business with the face either.

    Written against ``wards_queryset``, the same call the page uses, so the
    photograph can never be readable by somebody the record is not.

    The guardian themselves, signed in to the parent app as the account linked
    to this record, reads their own photograph and no other guardian's.
    """
    from vs_rbac.permissions import has_permission

    from .constants import PERM_VIEW
    from .services.guardians import wards_queryset

    tenant = getattr(request, "tenant", None)
    user = getattr(request, "user", None)
    if tenant is None or user is None:
        return False
    if guardian.tenant_id != getattr(tenant, "pk", None):
        return False
    if (
        guardian.user_id is not None
        and getattr(user, "pk", None) == guardian.user_id
        and getattr(user, "is_active", False)
    ):
        return True
    if not has_permission(user, PERM_VIEW, tenant=tenant):
        return False
    return wards_queryset(guardian, user, tenant).exists()


def register() -> None:
    register_policy(Student, _may_read_student_file)
    register_policy(StudentDocument, _may_read_document)
    register_policy(Guardian, _may_read_guardian_photo)

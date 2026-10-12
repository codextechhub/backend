"""Which class a caller may open, and which one they may take the register of.

**Opening.** A class of another school is a 404, as is a class in a branch the
caller cannot see. A class shared across the school (no branch) is visible to
everyone, as any shared record is, but a register must belong to one branch:
at a school with one branch the register is that branch's, and at a school with
several the class has to be given a branch first (:class:`ClassHasNoBranch`).

**Taking.** The permission keys answer whether somebody may take registers at
all; this module answers which class. The holder of ``mark_any`` takes the
register of any class they can open. The holder of only ``mark`` takes the
register of a class they are the class teacher of, which is a fact about the
class and so cannot be a permission key.

**Asking for a correction** follows the same shape: a holder of ``mark_any``
asks for any class they can open, and a teacher holding only
``attendance.correction.create`` asks for the class they lead.
"""
from __future__ import annotations

from rest_framework.exceptions import NotFound

from schools.vs_academics.models import SchoolClass
from vs_rbac.evaluator import has_permission
from vs_rbac.scoping import only_branch_id_or_several, visible_branch_ids
from vs_tenants.models import Branch

from ..constants import PERM_REGISTER_MARK, PERM_REGISTER_MARK_ANY
from ..exceptions import ClassHasNoBranch, NotYourClass


def open_class(tenant, user, class_id) -> tuple[SchoolClass, Branch]:
    """The class and the branch its register belongs to, for a caller who may open it.

    Raises 404 for a class that is not this school's or sits in a branch the
    caller cannot see, and :class:`ClassHasNoBranch` for a shared class at a
    school with several branches.
    """
    school_class = (
        SchoolClass.objects.filter(tenant=tenant, pk=class_id)
        .select_related("session", "branch", "class_teacher", "class_teacher__user")
        .first()
    )
    if school_class is None:
        raise NotFound("No such class at this school.")

    visible = visible_branch_ids(user, tenant)
    if school_class.branch_id is not None:
        if visible is not None and school_class.branch_id not in visible:
            raise NotFound("No such class at this school.")
        return school_class, school_class.branch

    only = only_branch_id_or_several(tenant)
    if only is None:
        raise ClassHasNoBranch()
    branch = Branch.all_objects.get(pk=only)
    if visible is not None and branch.pk not in visible:
        raise NotFound("No such class at this school.")
    return school_class, branch


def may_take_any(user, tenant) -> bool:
    """Whether *user* holds the key to take any class's register."""
    return has_permission(user, PERM_REGISTER_MARK_ANY, tenant=tenant)


def assert_may_take(user, tenant, school_class) -> bool:
    """Refuse a caller who may not take *school_class*'s register.

    Returns whether the caller holds ``mark_any``, which later rules (who may
    still change a submitted register) need and which is one query to find out.
    """
    if may_take_any(user, tenant):
        return True
    teacher = school_class.class_teacher
    leads = teacher is not None and teacher.user_id == user.pk
    if leads and has_permission(user, PERM_REGISTER_MARK, tenant=tenant):
        return False
    raise NotYourClass(
        f"You can take the register only for the class you lead. Ask an "
        f"administrator to take {school_class.name}'s register for you.",
    )


def assert_may_request_correction(user, tenant, school_class) -> None:
    """Refuse a caller who may not ask for a correction to *school_class*'s register.

    The key to ask for corrections at all is the view's to check. Which class
    is decided here: any class the caller can open for a holder of
    ``mark_any``, otherwise the class they lead.
    """
    if may_take_any(user, tenant):
        return
    teacher = school_class.class_teacher
    if teacher is not None and teacher.user_id == user.pk:
        return
    raise NotYourClass(
        f"You can ask for a correction only to the register of the class you "
        f"lead. Ask an administrator to ask for one in {school_class.name}.",
    )

"""The child a bill is about, as a Dynamic Role can test them.

A refund, a credit note and a concession are raised against an AR customer, and
a school's AR customer is a pupil on the roll: the ledger records the link as
the loose ``source_type``/``source_id`` pair rather than a foreign key, because
the ledger must not import this app. Read the other way - which this module
does - that pair is how a school's approval rules can say "send JSS1's refunds
to the JSS1 coordinator".

The workflow engine may not import this app either, so the area, its fields and
the way it is reached are all registered here at startup, through the engine's
autodiscovery of ``workflow_conditions`` modules. A document that reaches no
child, or a school with no roll imported yet, simply has no student facts, and
a condition on them is not true rather than an error.
"""
from vs_workflow.conditions.context import register_area_resolver
from vs_workflow.conditions.fields import (
    ConditionArea, ConditionField, register_area, register_area_field,
)
from vs_workflow.constants import ConditionFieldType

from .constants import Gender, StudentStatus

#: The document types raised against an AR customer, which is the only path
#: from a document to a child. Naming them here keeps the engine's catalogue
#: honest: a rule about a student is offered on these and refused on a leave
#: request, which reaches no child at all.
STUDENT_DOCUMENT_TYPES = ("finance.refund", "finance.credit_note", "finance.concession")

AREA = register_area(ConditionArea(
    "student", "The student it is about", STUDENT_DOCUMENT_TYPES, order=10))

register_area_field(ConditionField(
    "student.class_name", "Their class", AREA.key, ConditionFieldType.TEXT))
register_area_field(ConditionField(
    "student.level_name", "Their year group", AREA.key, ConditionFieldType.TEXT))
register_area_field(ConditionField(
    "student.status", "Their status", AREA.key, ConditionFieldType.CHOICE,
    tuple(StudentStatus.choices)))
register_area_field(ConditionField(
    "student.gender", "Their gender", AREA.key, ConditionFieldType.CHOICE,
    tuple(Gender.choices)))
register_area_field(ConditionField(
    "student.branch", "Their branch", AREA.key, ConditionFieldType.BRANCH))


def _student_for(document, tenant):
    """The pupil an AR document is about, or None.

    Scoped by tenant, and that is not decoration: ``Customer.source_id`` is a
    loose string, so a school that imported receivables before its roll can
    hold a reference that means nothing here while another school's pupil
    genuinely has that primary key. The FAL makes the same check for the same
    reason (``adapters.django_finance._class_labels``), one page at a time;
    this is the one-document case the engine needs.
    """
    from schools.core.fal.contracts import SOURCE_TYPE_STUDENT

    from .models import Student

    customer = getattr(document, "customer", None)
    if customer is None:
        return None
    if customer.source_type != SOURCE_TYPE_STUDENT or not customer.source_id:
        return None
    try:
        student_id = int(customer.source_id)
    except (TypeError, ValueError):
        return None
    return Student.all_objects.filter(
        pk=student_id, tenant=tenant,
    ).select_related("branch").first()


def _placement(student):
    """The class the child sits in now: their active enrolment, newest session."""
    from .models import ClassEnrolment

    return (
        ClassEnrolment.all_objects
        .filter(student=student, is_active=True, tenant_id=student.tenant_id)
        .order_by("-session__start_date")
        .select_related("school_class", "school_class__level")
        .first()
    )


def student_facts(document, tenant) -> dict:
    """What a rule may read about the child this document is about."""
    student = _student_for(document, tenant)
    if student is None:
        return {}
    placement = _placement(student)
    school_class = getattr(placement, "school_class", None)
    return {
        "id": str(student.pk),
        "class_name": getattr(school_class, "name", "") or "",
        "level_name": getattr(getattr(school_class, "level", None), "name", "") or "",
        "status": student.status,
        "gender": student.gender,
        "branch": str(student.branch_id) if student.branch_id else None,
    }


register_area_resolver(AREA.key, student_facts)

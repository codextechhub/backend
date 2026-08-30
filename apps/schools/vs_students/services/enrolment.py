"""Creating a student, in one atomic call.

Enrol and "save as applicant" are the same operation with one flag, not two
endpoints, so one serializer validates both and neither can drift into
accepting what the other refuses. An applicant takes no admission number, no
class and no admission date, and carries the level they applied for instead.

If placement fails after the student row is written, nothing persists. The
notification is dispatched after commit, never inside the transaction - and
today it dispatches nothing at all, because the recipient of ``student.enrolled``
cannot be resolved while there is no Staff model. That is stated rather than
stubbed.

FRD M11 v2.4 FR-002 and FR-003.
"""
from __future__ import annotations

from django.db import transaction
from django.db.models.functions import Lower
from django.utils import timezone

from vs_audit.models import AuditActionType, AuditModuleKey
from vs_audit.services import emit_audit_event

from ..constants import StudentStatus
from ..exceptions import DuplicateStudent, DuplicateStudentNumber
from ..models import Student
from . import guardians as guardian_service
from .policy import assert_number_allowed
from .status import transition

#: Fields that are not text and must not be coerced to "" when absent.
_NON_TEXT = frozenset({"date_of_birth"})

#: Fields an enrolment copies straight onto the record.
PERSONAL_FIELDS = (
    "first_name", "middle_name", "last_name", "date_of_birth", "gender",
    "nationality", "state_of_origin", "address", "phone", "email",
    "previous_school", "blood_group", "allergies", "conditions",
    "emergency_contact_name", "emergency_contact_phone",
)


def assert_number_free(tenant, number, *, exclude_pk=None):
    """Per school, case-insensitively, and only when there is a number.

    A blank never collides with anything, including another blank, which is
    what the conditional constraint permits and an unconditional one would not.
    """
    number = (number or "").strip()
    if not number:
        return number
    qs = Student.objects.filter(tenant=tenant).annotate(
        _n=Lower("student_number"),
    ).filter(_n=number.lower())
    if exclude_pk:
        qs = qs.exclude(pk=exclude_pk)
    clash = qs.first()
    if clash is not None:
        raise DuplicateStudentNumber(
            f"{clash.full_name} already holds {number} at this school.",
            student=clash.pk,
        )
    return number


def assert_not_duplicate(tenant, *, first_name, last_name, date_of_birth, confirmed=False):
    """Advisory only. Two real children can share a name and a birthday.

    Case-insensitive on purpose: a duplicate differing only in capitalisation
    is exactly the one a case-sensitive filter misses and a registrar creates.
    """
    if confirmed:
        return
    clash = (
        Student.objects.filter(tenant=tenant, date_of_birth=date_of_birth)
        .annotate(_f=Lower("first_name"), _l=Lower("last_name"))
        .filter(_f=(first_name or "").lower(), _l=(last_name or "").lower())
        .first()
    )
    if clash is not None:
        raise DuplicateStudent(
            f"{clash.full_name} is already on the roll with this name and date "
            f"of birth. Confirm that this is a different child to continue.",
            student=clash.pk, student_number=clash.student_number or None,
        )


@transaction.atomic
def enrol(
    *, tenant, actor, branch, data, guardian_rows, as_applicant=False,
    school_class=None, allow_over_capacity=False, confirm_duplicate=False,
    documents=None,
):
    """Create a student and everything that must exist with them.

    Returns the Student. Raises before writing anything if any rule refuses.
    """
    from .documents import attach
    from .placement import place

    guardian_service.assert_guardian_set(guardian_rows)
    assert_not_duplicate(
        tenant,
        first_name=data["first_name"], last_name=data["last_name"],
        date_of_birth=data["date_of_birth"], confirmed=confirm_duplicate,
    )

    number = ""
    if not as_applicant:
        number = assert_number_allowed(tenant, data.get("student_number", ""))
        number = assert_number_free(tenant, number)

    personal = {
        field: (data[field] if field in _NON_TEXT else (data.get(field) or ""))
        for field in PERSONAL_FIELDS
    }
    student = Student.objects.create(
        tenant=tenant, branch=branch,
        student_number=number,
        status=StudentStatus.APPLICANT,
        enrolment_date=(
            timezone.localdate() if as_applicant
            else (data.get("enrolment_date") or timezone.localdate())
        ),
        applied_for=data.get("applied_for") if as_applicant else None,
        applied_on=timezone.localdate() if as_applicant else None,
        created_by=actor,
        **personal,
    )

    for row in guardian_rows:
        guardian = row.get("guardian")
        if guardian is None:
            guardian, _ = guardian_service.upsert_guardian(
                tenant,
                full_name=row.get("full_name", ""), phone=row.get("phone", ""),
                email=row.get("email", ""), occupation=row.get("occupation", ""),
                address=row.get("address", "") or data.get("address", ""),
            )
        guardian_service.link(
            student, guardian, relationship=row["relationship"],
            is_primary=bool(row.get("is_primary")), actor=actor,
        )

    for doc in documents or []:
        attach(
            student, document_type=doc["document_type"], upload=doc["file"],
            actor=actor,
        )

    if as_applicant:
        emit_audit_event(
            module_key=AuditModuleKey.STUDENT,
            action_type=AuditActionType.CREATE,
            entity_type="Student", entity_id=str(student.pk),
            entity_label=student.full_name,
            tenant=tenant, actor_user=actor,
            summary=f"{student.full_name} saved as an applicant.",
        )
        return student

    # Confirmed, then placed. Two log rows, deliberately: "confirmed on the
    # 8th, started on the 11th" is a real distinction and a school is asked for
    # it. FR-002's postconditions name both.
    transition(
        student, StudentStatus.ENROLLED, actor=actor,
        reason=f"Enrolled{f' as {number}' if number else ''}.",
        effective_date=student.enrolment_date,
    )
    if school_class is not None:
        place(
            student, school_class, actor=actor,
            effective_date=student.enrolment_date,
            allow_over_capacity=allow_over_capacity,
        )

    emit_audit_event(
        module_key=AuditModuleKey.STUDENT,
        action_type=AuditActionType.STUDENT_ENROLLED,
        entity_type="Student", entity_id=str(student.pk),
        entity_label=student.full_name,
        tenant=tenant, actor_user=actor,
        summary=(
            f"{student.full_name} enrolled"
            + (f" as {number}" if number else "")
            + (f" in {school_class.name}." if school_class else " with no class yet.")
        ),
        metadata={
            "student_number": number, "branch": branch.name,
            "class": school_class.name if school_class else None,
        },
    )
    # student.enrolled is deliberately NOT dispatched. Its recipient is the
    # class teacher, there is no Staff model to resolve one, and sending it to
    # nobody would be worse than not sending it. FRD v2.4 FR-002, "not decided
    # here", and section 14 decision 8.
    student.refresh_from_db()
    return student


@transaction.atomic
def confirm_applicant(student, *, actor, reason="", effective_date=None, number=None):
    """APPLICANT to ENROLLED. Placement is a separate act and reaches ACTIVE."""
    if number is not None:
        value = assert_number_allowed(student.tenant, number)
        value = assert_number_free(student.tenant, value, exclude_pk=student.pk)
        student.student_number = value
        student.save(update_fields=["student_number", "updated_at"])
    return transition(
        student, StudentStatus.ENROLLED, actor=actor,
        reason=reason or "Application confirmed.",
        effective_date=effective_date,
    )

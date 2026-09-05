"""Adding somebody to a school's staff.

One act, one transaction: the account, the invitation, the role grant, the staff
record, its first employment event, and whatever qualifications, documents and
teaching duties the form carried. The design's Add screen is six steps and a
single save, so a person whose third qualification is refused must not be left
existing with two.

**The account is not created here.** ``UserCreationService`` already resolves
the target tenant, resolves the role inside it, scopes the email-uniqueness
check to it and sends the invitation, and the live staff endpoint already calls
it. A second creation path would be a second set of rules about who may be
created where, and the two would drift.

FRD M12 v2.1, FR-001.
"""
from __future__ import annotations

from django.db import transaction

from ..constants import EmploymentStatus
from . import audit, employment


@transaction.atomic
def create_profile(*, tenant, user, actor, staff_number="", job_title="",
                   employment_type="", hire_date=None, branch=None,
                   middle_name="", date_of_birth=None, photo=None):
    """The staff record for an account that has just been created.

    Called inside the same transaction as the account, never before it: there is
    no user-less staff record and this module must not be able to produce one.

    The posting is mirrored onto ``User.branch`` so the identity layer's own
    fallback narrowing keeps agreeing with the record.
    """
    from ..models import StaffProfile

    profile = StaffProfile(
        tenant=tenant, user=user, branch=branch,
        staff_number=(staff_number or "").strip(), job_title=job_title or "",
        employment_type=employment_type or "",
        employment_status=EmploymentStatus.INVITED,
        hire_date=hire_date, middle_name=middle_name or "",
        date_of_birth=date_of_birth, created_by=actor,
    )
    if photo is not None:
        profile.photo = photo
    profile.full_clean(exclude=["photo"])
    profile.save()

    if branch is not None and user.branch_id != branch.pk:
        user.branch = branch
        user.save(update_fields=["branch", "updated_at"])

    employment.record_creation(profile, actor=actor)
    audit.emit_profile_created(profile, actor=actor)
    return profile


@transaction.atomic
def attach_qualifications(profile, rows, *, actor):
    """Qualification rows submitted with the form.

    Typed as given. Nothing checks a qualification against anything, so no row
    carries a verified state and no endpoint may add one.
    """
    from ..models import StaffQualification

    created = []
    for row in rows or ():
        created.append(StaffQualification.objects.create(
            tenant=profile.tenant, staff=profile,
            qualification=row["qualification"],
            institution=row.get("institution", "") or "",
            year_obtained=row.get("year_obtained"),
            note=row.get("note", "") or "",
            created_by=actor,
        ))
    return created


@transaction.atomic
def attach_documents(profile, rows, *, actor):
    """Files submitted with the form. Stored as uploaded, checked by nobody."""
    from ..models import StaffDocument

    created = []
    for row in rows or ():
        created.append(StaffDocument.objects.create(
            tenant=profile.tenant, staff=profile,
            document_type=row["document_type"], title=row["title"],
            file=row["file"], uploaded_by=actor,
        ))
    return created


@transaction.atomic
def attach_teaching(profile, *, subjects, classes, session, actor):
    """The Add screen's subjects-by-classes grid, written as assignments.

    Every pairing the form selected, as a lead where the pairing has none and as
    an assistant where it already has one. Filling the grid must not silently
    displace a colleague who already owns a class, which is the same rule
    :func:`services.teaching.assign` enforces when the pairing is named
    directly.
    """
    from ..constants import TeachingPart
    from . import teaching

    if not subjects or not classes or session is None:
        return []

    created = []
    for school_class in classes:
        for subject in subjects:
            held = teaching.current_lead(
                profile.tenant, session, school_class, subject,
            )
            part = TeachingPart.ASSISTANT if held is not None else TeachingPart.LEAD
            created.append(teaching.assign(
                tenant=profile.tenant, staff=profile, school_class=school_class,
                subject=subject, session=session, part=part, actor=actor,
            ))
    return created

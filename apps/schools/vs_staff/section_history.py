"""Safe, section-specific changes to a staff record.

Record versions hold complete snapshots, including fields that a profile may
not expose. Only named display fields leave through this module. Free-text
notes, file paths, audit metadata and internal ids stay private.
"""
from __future__ import annotations

from django.db.models import F, Q, Window
from django.db.models.functions import Lag
from rest_framework.exceptions import ValidationError

from vs_history.models import RecordVersion
from vs_history.registry import owner_key

from .models import LeaveRequest, StaffDocument, StaffProfile, StaffQualification, TeachingAssignment
from .services.visibility import GROUP_CONTACT, GROUP_LEAVE, GROUP_RECORDS, GROUP_ROLES, GROUP_TEACHING

PAGE_SIZE = 12

SECTION_GROUPS = {
    "overview": GROUP_CONTACT,
    "teaching": GROUP_TEACHING,
    "access": GROUP_ROLES,
    "qualifications": GROUP_RECORDS,
    "documents": GROUP_RECORDS,
    "leave": GROUP_LEAVE,
}

PROFILE_FIELDS = {
    "middle_name": "Middle name",
    "date_of_birth": "Date of birth",
    "job_title": "Job title",
    "employment_type": "Employment type",
    "hire_date": "Hire date",
    "exit_date": "Last working day",
    "employment_status": "Employment status",
    "branch_id": "Home branch",
    "additional_postings": "Posting",
}
USER_FIELDS = {
    "first_name": "First name", "last_name": "Last name",
    "phone": "Phone", "gender": "Gender",
}
SECTION_MODELS = {
    "overview": (StaffProfile, PROFILE_FIELDS),
    "teaching": (TeachingAssignment, {
        "session_id": "School year", "school_class_id": "Class",
        "subject_id": "Subject", "part": "Teaching part",
    }),
    "access": (None, {
        "role_id": "Role", "branch_id": "Role branch",
        "assignment_status": "Grant status",
    }),
    "qualifications": (StaffQualification, {
        "qualification": "Qualification", "institution": "Institution",
        "year_obtained": "Year obtained",
    }),
    "documents": (StaffDocument, {
        "document_type": "Document type", "title": "Title", "file": "File",
    }),
    "leave": (LeaveRequest, {
        "leave_type": "Leave type", "start_date": "Start date",
        "end_date": "End date", "days": "Days", "status": "Status",
    }),
}


def _model(section):
    if section == "access":
        from vs_rbac.models import TenantUserRoleAssignment

        return TenantUserRoleAssignment
    return SECTION_MODELS[section][0]


def _names(tenant, section, versions):
    """Resolve only the foreign keys on this page, inside this school."""
    ids = {"branch": set(), "class": set(), "subject": set(), "session": set(), "role": set()}
    keys = {
        "branch_id": "branch", "school_class_id": "class",
        "subject_id": "subject", "session_id": "session", "role_id": "role",
    }
    for row in versions:
        for data in (row.previous_data or {}, row.data or {}):
            for field, bucket in keys.items():
                if data.get(field):
                    ids[bucket].add(data[field])
            if section == "overview":
                ids["branch"].update(data.get("additional_postings") or [])

    from vs_tenants.models import Branch
    from schools.vs_academics.models import AcademicSession, SchoolClass, Subject
    from vs_rbac.models import TenantRoleTemplate

    sources = {
        "branch": (Branch, "name"), "class": (SchoolClass, "name"),
        "subject": (Subject, "name"), "session": (AcademicSession, "name"),
        "role": (TenantRoleTemplate, "name"),
    }
    names = {}
    for bucket, (model, label) in sources.items():
        names[bucket] = dict(
            model._base_manager.filter(tenant=tenant, pk__in=ids[bucket])
            .values_list("pk", label)
        ) if ids[bucket] else {}
    return names


def _display(section, field, value, names):
    if field == "branch_id" and value is None:
        return "School-wide"
    if value in (None, ""):
        return None
    if field == "file":
        return "Stored file"
    if field == "additional_postings":
        return ", ".join(names["branch"].get(pk, "Unavailable branch") for pk in value) or "No additional branches"
    lookup = {
        "branch_id": "branch", "school_class_id": "class",
        "subject_id": "subject", "session_id": "session", "role_id": "role",
    }.get(field)
    if lookup:
        return names[lookup].get(value, "Unavailable")
    if field == "part":
        return {"LEAD": "Main teacher", "ASSISTANT": "Assisting"}.get(value, "Other")
    if field == "assignment_status":
        return {"ACTIVE": "Granted", "REVOKED": "Withdrawn"}.get(value, "Other")
    if field == "employment_status":
        from .constants import EmploymentStatus

        return dict(EmploymentStatus.choices).get(value, "Other")
    if field == "document_type":
        from .constants import DocumentType

        return dict(DocumentType.choices).get(value, "Other")
    if field == "leave_type":
        from .constants import LeaveType

        return dict(LeaveType.choices).get(value, "Other")
    if field == "status" and section == "leave":
        from .constants import LeaveStatus

        return dict(LeaveStatus.choices).get(value, "Other")
    if field == "employment_type":
        return str(value).replace("_", " ").title()
    if field == "gender":
        return {"MALE": "Male", "FEMALE": "Female", "OTHER": "Other"}.get(value, str(value))
    return str(value)


def _title(section, data, names, record_type):
    if section == "overview":
        return "Name and contact" if record_type == "vs_user.user" else "Profile details"
    if section == "teaching":
        subject = _display(section, "subject_id", data.get("subject_id"), names)
        school_class = _display(section, "school_class_id", data.get("school_class_id"), names)
        return f"{subject or 'Subject'} in {school_class or 'class'}"
    if section == "qualifications":
        return data.get("qualification") or "Qualification"
    if section == "documents":
        return data.get("title") or "Document"
    if section == "leave":
        return _display(section, "leave_type", data.get("leave_type"), names) or "Leave"
    return _display(section, "role_id", data.get("role_id"), names) or "Role"


def section_changes(*, tenant, staff, section, page, visible_fields=None, before=None):
    """Return one dated page without exposing complete stored snapshots.

    The reader has already been admitted for this section. For overview,
    ``visible_fields`` comes from the detail serializer, so profile visibility
    and Field Access also remove a field's history.
    """
    try:
        page = int(page)
    except (TypeError, ValueError) as exc:
        raise ValidationError({"page": "Choose a positive page number."}) from exc
    if page < 1 or page > 1000:
        raise ValidationError({"page": "Choose a page from 1 to 1000."})

    model = _model(section)
    record_type = model._meta.label_lower
    fields = SECTION_MODELS[section][1]
    user_fields = USER_FIELDS if section == "overview" else {}
    if section == "overview" and visible_fields is not None:
        fields = {
            name: label for name, label in fields.items()
            if (name == "additional_postings" and "posting_branch_ids" in visible_fields)
            or (name == "branch_id" and "branch_name" in visible_fields)
            or name in visible_fields
        }
        user_fields = {
            name: label for name, label in user_fields.items()
            if name in visible_fields
        }
    if not fields and not user_fields:
        return {"entries": [], "next_page": None}

    if section == "overview":
        scope = (
            Q(record_type=record_type, record_id=str(staff.pk))
            | Q(record_type="vs_user.user", record_id=str(staff.user_id))
        )
    elif section == "access":
        scope = Q(owners__contains=[owner_key("vs_user.user", staff.user_id)])
    else:
        scope = Q(owners__contains=[owner_key(StaffProfile._meta.label_lower, staff.pk)])

    changed = Q(is_deleted=True) | Q(is_baseline=True)
    for field in set(fields) | (set(user_fields) if section == "overview" else set()):
        changed |= Q(changed__contains=[field])
    rows = RecordVersion.objects.filter(tenant=tenant).filter(scope).filter(changed)
    if section != "overview":
        rows = rows.filter(record_type=record_type)
    if before is not None:
        rows = rows.filter(recorded_at__lt=before.moment)
    rows = rows.annotate(previous_data=Window(
        expression=Lag("data"),
        partition_by=[F("record_type"), F("record_id")],
        order_by=[F("recorded_at").asc(), F("id").asc()],
    )).select_related("actor").order_by("-recorded_at", "-id")
    start = (page - 1) * PAGE_SIZE
    versions = list(rows[start:start + PAGE_SIZE + 1])
    names = _names(tenant, section, versions)
    entries = []
    for row in versions[:PAGE_SIZE]:
        row_fields = user_fields if row.record_type == "vs_user.user" else fields
        old = row.data if row.is_deleted else (row.previous_data or {})
        new = {} if row.is_deleted else row.data
        changes = []
        for name, label in row_fields.items():
            if old.get(name) == new.get(name):
                continue
            if name == "file" and old.get(name) and new.get(name):
                before_value, after_value = "Previous stored file", "Replacement stored file"
            else:
                before_value = _display(section, name, old[name], names) if name in old else None
                after_value = _display(section, name, new[name], names) if name in new else None
            if before_value != after_value:
                changes.append({"field": label, "before": before_value, "after": after_value})
        if not changes:
            continue
        actor = row.actor
        entries.append({
            "id": row.pk,
            "at": row.recorded_at,
            "title": _title(section, row.data, names, row.record_type),
            "action": "Recorded" if row.is_baseline else (
                "Removed" if row.is_deleted else "Added" if row.previous_data is None else "Changed"
            ),
            "actor": " ".join(part for part in (actor.first_name, actor.last_name) if part).strip() if actor else None,
            "changes": changes,
        })
    return {"entries": entries, "next_page": page + 1 if len(versions) > PAGE_SIZE else None}

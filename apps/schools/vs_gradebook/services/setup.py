"""Assessment setup: structures, grading scales and the principal's remark templates.

A structure and a scale are the same kind of thing to manage: a named record
for a programme (and optionally one branch) with an ordered list of rows under
it, a status that moves DRAFT, IN_USE, RETIRED, and a rule that it cannot be put
in use unless its rows are sound. :class:`_Kind` states what differs (the
model, the child model, the soundness check, how a lock is found) and the
functions here do the rest once, so the two behave identically.

**Status.** A new one is a DRAFT. Putting a draft in use retires the one in use
for the same programme and branch, in one transaction, retiring first so the
database's one-in-use rule is never broken. A class is assessed by whichever is
in use when it is first marked (:mod:`.resolve`).

**Locks.** A structure is *used by a published result* when a class-term that
has published points at it; a scale is when a published student's snapshot
carries its id. Such a record cannot be edited, deleted or put in use, because
what a family was given must stay explainable. Copying is always allowed, and
putting the copy in use retires the old one without touching what was
published with it. A retired record cannot be edited either.

**Editing what is in use.** An in-use structure or scale may be edited while
nothing published rests on it, and must still be sound afterwards. A structure
fixed on a class-term that has marks is also refused when the edit would
invalidate a mark: a maximum lowered below a mark already entered, or a
component with marks removed (:class:`~..exceptions.WouldInvalidateMarks`,
naming the sheet). Adding a component or reordering is allowed.

**Rows keep their identity.** An edit sends the whole list; a row carrying an
``id`` of the record is updated in place, a row without one is new, and a row
the list leaves out is removed. A component keeps its id so the scores under it
stay attached.

**Branch.** A null branch applies to every branch. The branch of a record in use
cannot be moved, since it would silently change which classes it grades.
Every write is audited under the ``GRADEBOOK`` module with the programme and
branch named in words.
"""
from __future__ import annotations

from dataclasses import dataclass
from types import SimpleNamespace

from django.db import transaction
from django.db.models import Count, Max, Q
from rest_framework.exceptions import NotFound

from schools.vs_academics.models import Program
from vs_audit.models import AuditActionType
from vs_rbac.scoping import only_branch_id_or_several
from vs_tenants.references import find_branch_in_tenant

from ..constants import (
    DEFAULT_REMARK_TEMPLATES,
    SETUP_NAME_MAX,
    SetupStatus,
    StoredStage,
)
from ..exceptions import (
    NotADraft,
    RemarkBands,
    ScaleGap,
    StructureTotal,
    UnknownProgram,
    UnknownRow,
    UsedByPublishedResult,
    WouldInvalidateMarks,
    UnknownBranch,
)
from ..models import (
    AssessmentComponent,
    AssessmentStructure,
    ClassTerm,
    GradeBand,
    GradingScale,
    RemarkTemplate,
    Score,
    StudentTermResult,
)
from . import audit
from .compute import Check, scale_check, structure_check, structure_total


# ── What differs between a structure and a scale ───────────────────────────

@dataclass(frozen=True)
class _Kind:
    model: type
    child_model: type
    child_attr: str
    child_fk: str
    noun: str
    entity_type: str
    check: object
    refusal: type
    #: ``{field on the child model: key in a request row}``, in write order.
    child_fields: tuple

    def children(self, obj) -> list:
        return list(getattr(obj, self.child_attr).all())

    def verdict(self, children) -> Check:
        return self.check(children)


STRUCTURE = _Kind(
    model=AssessmentStructure, child_model=AssessmentComponent,
    child_attr="components", child_fk="structure",
    noun="assessment structure", entity_type="AssessmentStructure",
    check=structure_check, refusal=StructureTotal,
    child_fields=("name", "short_name", "max_score", "is_exam"),
)
SCALE = _Kind(
    model=GradingScale, child_model=GradeBand,
    child_attr="bands", child_fk="scale",
    noun="grading scale", entity_type="GradingScale",
    check=scale_check, refusal=ScaleGap,
    child_fields=("min_score", "grade", "remark", "is_fail"),
)

#: What a row of the request may omit, and the value it then takes.
_CHILD_DEFAULTS = {"is_exam": False, "is_fail": False, "remark": ""}


# ── Reading ────────────────────────────────────────────────────────────────

def is_multi_branch(tenant) -> bool:
    """Whether the school has several branches, where a record's branch is worth showing."""
    return only_branch_id_or_several(tenant) is None


def _queryset(kind, tenant, scope_q):
    return (
        kind.model.objects.filter(scope_q, tenant=tenant)
        .select_related("program", "branch")
        .prefetch_related(kind.child_attr)
    )


def get_record(kind, tenant, scope_q, pk):
    """The record *pk* of *tenant* the caller's branches can see; anything else is a 404."""
    found = _queryset(kind, tenant, scope_q).filter(pk=pk).first()
    if found is None:
        raise NotFound(f"No such {kind.noun} at this school.")
    return found


def locks(kind, tenant, ids) -> dict:
    """``{id: sentence}`` for each of *ids* that a published result rests on.

    The sentence names a published class, and how many others if there are
    several, so the screen can say who is holding the record still.
    """
    if not ids:
        return {}
    if kind is STRUCTURE:
        rows = (
            ClassTerm.objects.filter(
                tenant=tenant, structure_id__in=ids, stage=StoredStage.PUBLISHED,
            )
            .values_list("structure_id", "school_class__name", "term__name")
            .order_by("school_class__name", "term__start_date")
        )
    else:
        rows = (
            StudentTermResult.objects.filter(
                tenant=tenant, class_term__stage=StoredStage.PUBLISHED,
                snapshot__scale_id__in=list(ids),
            )
            .values_list(
                "snapshot__scale_id", "class_term__school_class__name",
                "class_term__term__name",
            )
            .distinct()
            .order_by("class_term__school_class__name", "class_term__term__name")
        )
    by_id: dict[int, list] = {}
    for record_id, class_name, term_name in rows:
        by_id.setdefault(int(record_id), []).append((class_name, term_name))
    out = {}
    for record_id, used in by_id.items():
        first_class, first_term = used[0]
        others = len({name for name, _ in used}) - 1
        also = f" and {others} other class{'es' if others != 1 else ''}" if others else ""
        out[record_id] = (
            f"{first_class}{also} published {first_term} results made with this "
            f"{kind.noun.split()[-1]}, so it can no longer be changed. Copy it to "
            f"start a new draft."
        )
    return out


def payload(kind, record, *, locked: dict, multi_branch: bool) -> dict:
    """One record as the setup screens read it, with its children, verdict and lock."""
    children = kind.children(record)
    verdict = kind.verdict(children)
    reason = locked.get(record.pk)
    out = {
        "id": record.pk,
        "name": record.name,
        "program": record.program_id,
        "program_name": record.program.name,
        "branch": record.branch_id,
        "status": record.status,
        "copied_from": record.copied_from_id,
        "locked": reason is not None,
        "lock_reason": reason,
        "created_at": record.created_at,
    }
    if multi_branch:
        out["scope_label"] = record.branch.name if record.branch_id else "School-wide"
        out["is_shared"] = record.branch_id is None
    rows = [
        {"id": child.pk, "order": child.order,
         **{field: getattr(child, field) for field in kind.child_fields}}
        for child in children
    ]
    check = {"ok": verdict.ok, "message": verdict.message}
    if kind is STRUCTURE:
        out["components"] = rows
        out["total"] = structure_total(children)
        out["total_check"] = check
    else:
        out["bands"] = rows
        out["check"] = check
    return out


def list_records(kind, tenant, scope_q, *, program=None, status=None) -> list:
    """The records the caller can see, as payloads, by programme then age.

    ``program`` is a programme id and ``status`` a :class:`SetupStatus` value;
    either narrows the list.
    """
    queryset = _queryset(kind, tenant, scope_q)
    if program not in (None, ""):
        queryset = queryset.filter(program_id=program)
    if status not in (None, ""):
        queryset = queryset.filter(status=status)
    records = list(queryset.order_by("program__order_index", "program__name", "-pk"))
    held = locks(kind, tenant, [r.pk for r in records])
    multi = is_multi_branch(tenant)
    return [payload(kind, r, locked=held, multi_branch=multi) for r in records]


def record_payload(kind, tenant, record) -> dict:
    """One record, re-read from the database, as the setup screens read it."""
    fresh = (
        kind.model.objects.filter(tenant=tenant, pk=record.pk)
        .select_related("program", "branch").prefetch_related(kind.child_attr).get()
    )
    return payload(
        kind, fresh, locked=locks(kind, tenant, [fresh.pk]), multi_branch=is_multi_branch(tenant),
    )


# ── Resolving what a body names ────────────────────────────────────────────

def _program(tenant, program_id) -> Program:
    program = Program.objects.filter(tenant=tenant, pk=program_id).first()
    if program is None:
        raise UnknownProgram(field="program")
    return program


def _branch(tenant, branch_id):
    if branch_id is None:
        return None
    branch = find_branch_in_tenant(tenant, branch_id)
    if branch is None:
        raise UnknownBranch(field="branch")
    return branch


def _where(record) -> str:
    """The programme and branch of *record* in words, for the audit trail."""
    place = record.branch.name if record.branch_id else "every branch"
    return f"{record.program.name}, {place}"


def _record_audit(kind, tenant, user, record, summary, action_type=AuditActionType.UPDATE, **meta):
    audit.record(
        tenant=tenant, user=user, branch=record.branch, entity_type=kind.entity_type,
        entity_id=record.pk, entity_label=record.name, summary=summary,
        action_type=action_type, program=record.program_id, status=record.status, **meta,
    )


def _described(kind, children) -> list:
    """The rows as short strings for the trail: ``CA 1 20`` or ``A1 from 75``."""
    if kind is STRUCTURE:
        return [f"{c.name} {c.max_score}" + (" (exam)" if c.is_exam else "") for c in children]
    return [f"{c.grade} from {c.min_score}" + (" (fail)" if c.is_fail else "") for c in children]


# ── Writing the rows ───────────────────────────────────────────────────────

def _row_values(kind, row) -> dict:
    return {
        field: row[field] if field in row else _CHILD_DEFAULTS[field]
        for field in kind.child_fields
    }


def _replace_children(kind, tenant, record, rows) -> bool:
    """Make *record*'s rows match *rows*, keeping the identity of those that carry an id.

    Returns whether anything changed. Raises :class:`~..exceptions.UnknownRow`
    when a row names an id that is not one of the record's own.
    """
    existing = {child.pk: child for child in kind.children(record)}
    named = [row["id"] for row in rows if row.get("id") is not None]
    if any(pk not in existing for pk in named) or len(named) != len(set(named)):
        raise UnknownRow()

    changed = False
    keep = set(named)
    removed = set(existing) - keep
    if removed and kind is STRUCTURE:
        # A blank cell says nothing, so it must not stop its column going.
        Score.objects.filter(
            tenant=tenant, component_id__in=removed, mark__isnull=True, marker__isnull=True,
        ).delete()
    for pk in removed:
        existing[pk].delete()
        changed = True
    for order, row in enumerate(rows, start=1):
        values = {**_row_values(kind, row), "order": order}
        pk = row.get("id")
        if pk is None:
            kind.child_model.objects.create(
                tenant=tenant, **{kind.child_fk: record}, **values,
            )
            changed = True
            continue
        child = existing[pk]
        dirty = [f for f, v in values.items() if getattr(child, f) != v]
        if dirty:
            for field in dirty:
                setattr(child, field, values[field])
            child.save(update_fields=[*dirty, "updated_at"])
            changed = True
    return changed


def _standin(kind, rows) -> list:
    """The request rows as objects with a stored row's attributes, so a verdict can be read before anything is written."""
    return [SimpleNamespace(**_row_values(kind, row)) for row in rows]


# ── Marks that an edit would orphan ────────────────────────────────────────

def _sheet_name(sheet) -> str:
    klass = sheet.class_term.school_class.name
    return f"{klass} {sheet.subject.name} ({sheet.class_term.term.name})"


def assert_edit_keeps_marks(tenant, structure, rows) -> None:
    """Refuse an edit that would leave a mark already entered without a column that holds it.

    Looks at the class-terms fixed on *structure* that are not published (a
    published one is refused earlier). A component the rows drop that has any
    mark or marker under it, and a component whose new maximum is below its
    highest mark, are refused, naming one sheet affected.
    """
    components = {c.pk: c for c in structure.components.all()}
    new_max = {row["id"]: row["max_score"] for row in rows if row.get("id") is not None}

    cells = Score.objects.filter(
        tenant=tenant, component__structure=structure,
    ).exclude(sheet__class_term__stage=StoredStage.PUBLISHED).filter(
        Q(mark__isnull=False) | Q(marker__isnull=False),
    )
    figures = {
        row["component_id"]: row
        for row in cells.values("component_id").annotate(top=Max("mark"), entered=Count("pk"))
    }
    for pk, component in components.items():
        seen = figures.get(pk)
        if seen is None:
            continue
        sheet = (
            cells.filter(component_id=pk).select_related(
                "sheet__class_term__school_class", "sheet__class_term__term", "sheet__subject",
            ).first().sheet
        )
        if pk not in new_max:
            raise WouldInvalidateMarks(
                f"{_sheet_name(sheet)} already has marks in {component.name}, so it "
                f"cannot be removed. Keep it, or clear those marks first.",
                field="components", sheet=sheet.pk,
            )
        top = seen["top"]
        if top is not None and new_max[pk] < top:
            over = cells.filter(component_id=pk, mark__gt=new_max[pk]).select_related(
                "sheet__class_term__school_class", "sheet__class_term__term", "sheet__subject",
            ).first().sheet
            raise WouldInvalidateMarks(
                f"{_sheet_name(over)} already has a mark of {top} in {component.name}, "
                f"which is above the new maximum of {new_max[pk]}. Keep the maximum at "
                f"{top} or more, or correct those marks first.",
                field="components", sheet=over.pk,
            )


# ── Create, edit, delete ───────────────────────────────────────────────────

@transaction.atomic
def create_record(kind, tenant, user, body) -> object:
    """A new DRAFT from a validated body (``name``, ``program``, ``branch``, rows)."""
    program = _program(tenant, body["program"])
    branch = _branch(tenant, body.get("branch"))
    record = kind.model.objects.create(
        tenant=tenant, program=program, branch=branch, name=body["name"],
        status=SetupStatus.DRAFT, created_by=user,
    )
    _replace_children(kind, tenant, record, body[kind.child_attr])
    record = _queryset(kind, tenant, Q()).get(pk=record.pk)
    _record_audit(
        kind, tenant, user, record,
        f"A draft {kind.noun}, {record.name}, was created for {_where(record)}.",
        AuditActionType.CREATE, rows=_described(kind, kind.children(record)),
    )
    return record


@transaction.atomic
def update_record(kind, tenant, user, record, body):
    """Apply a validated edit: any of ``name``, ``branch`` and the rows. Only what differs is written.

    Refused for a record a published result rests on, for a retired one, and
    for a branch change on one in use. An edit to one in use must leave it
    sound, and a structure's must keep every mark already entered.
    """
    held = locks(kind, tenant, [record.pk])
    if record.pk in held:
        raise UsedByPublishedResult(held[record.pk])
    if record.status == SetupStatus.RETIRED:
        raise NotADraft(
            f"This {kind.noun} was replaced and can no longer be changed. Copy it "
            f"to start a new draft.",
        )

    rows = body.get(kind.child_attr)
    if "branch" in body:
        branch = _branch(tenant, body["branch"])
    else:
        branch = record.branch
    branch_changed = (branch.pk if branch else None) != record.branch_id
    if branch_changed and record.status == SetupStatus.IN_USE:
        raise NotADraft(
            f"The branch of a {kind.noun} in use cannot be moved. Copy it, change "
            f"the copy's branch, and put the copy in use.",
            field="branch",
        )

    if rows is not None and record.status == SetupStatus.IN_USE:
        verdict = kind.verdict(_standin(kind, rows))
        if not verdict.ok:
            raise kind.refusal(verdict.message, field=kind.child_attr)
        if kind is STRUCTURE:
            assert_edit_keeps_marks(tenant, record, rows)

    before_rows = _described(kind, kind.children(record))
    changes = {}
    if "name" in body and body["name"] != record.name:
        changes["name"] = [record.name, body["name"]]
        record.name = body["name"]
    if branch_changed:
        changes["branch"] = [record.branch.name if record.branch_id else None,
                             branch.name if branch else None]
        record.branch = branch
    if changes:
        record.save()
    rows_changed = False
    if rows is not None:
        rows_changed = _replace_children(kind, tenant, record, rows)
    if not changes and not rows_changed:
        return record

    record = _queryset(kind, tenant, Q()).get(pk=record.pk)
    if rows_changed:
        changes[kind.child_attr] = [before_rows, _described(kind, kind.children(record))]
    _record_audit(
        kind, tenant, user, record,
        f"The {kind.noun} {record.name} for {_where(record)} was edited.", changes=changes,
    )
    return record


@transaction.atomic
def delete_record(kind, tenant, user, record) -> None:
    """Delete a DRAFT. Anything that has been in use is kept, because results refer to it."""
    if record.status != SetupStatus.DRAFT:
        raise NotADraft(
            f"Only a draft can be deleted. This {kind.noun} has been in use, so it "
            f"stays on record.",
        )
    summary = f"The draft {kind.noun} {record.name} for {_where(record)} was deleted."
    entity_id, label, branch = record.pk, record.name, record.branch
    program = record.program_id
    record.delete()
    audit.record(
        tenant=tenant, user=user, branch=branch, entity_type=kind.entity_type,
        entity_id=entity_id, entity_label=label, summary=summary,
        action_type=AuditActionType.DELETE, program=program,
    )


@transaction.atomic
def copy_record(kind, tenant, user, record):
    """A new DRAFT with the same programme, branch and rows, remembering where it came from.

    Allowed whatever the record's status or lock: copying is how a locked or
    retired one is changed.
    """
    name = f"{record.name} (copy)"
    clone = kind.model.objects.create(
        tenant=tenant, program=record.program, branch=record.branch,
        name=name[:SETUP_NAME_MAX], status=SetupStatus.DRAFT, copied_from=record,
        created_by=user,
    )
    for child in kind.children(record):
        values = {field: getattr(child, field) for field in kind.child_fields}
        kind.child_model.objects.create(
            tenant=tenant, **{kind.child_fk: clone}, order=child.order, **values,
        )
    clone = _queryset(kind, tenant, Q()).get(pk=clone.pk)
    _record_audit(
        kind, tenant, user, clone,
        f"A draft {kind.noun}, {clone.name}, was copied from {record.name}.",
        AuditActionType.CREATE, copied_from=record.pk,
    )
    return clone


@transaction.atomic
def activate_record(kind, tenant, user, record):
    """Put a DRAFT in use, retiring the one in use for the same programme and branch.

    Refused for anything that is not a draft (a record a published result rests
    on says so), and for a draft whose rows are not sound, with the reason the
    editor shows. The retired record is untouched apart from its status, so what
    was published with it stays explainable.
    """
    held = locks(kind, tenant, [record.pk])
    if record.pk in held:
        raise UsedByPublishedResult(held[record.pk])
    if record.status != SetupStatus.DRAFT:
        state = "already in use" if record.status == SetupStatus.IN_USE else "retired"
        raise NotADraft(
            f"Only a draft can be put in use, and this {kind.noun} is {state}. "
            f"Copy it to start a new draft.",
        )
    verdict = kind.verdict(kind.children(record))
    if not verdict.ok:
        raise kind.refusal(verdict.message, field=kind.child_attr)

    same_place = Q(branch=record.branch) if record.branch_id else Q(branch__isnull=True)
    previous = list(
        kind.model.objects.select_for_update(of=("self",)).filter(
            same_place, tenant=tenant, program=record.program, status=SetupStatus.IN_USE,
        ).select_related("program", "branch")
    )
    for old in previous:
        old.status = SetupStatus.RETIRED
        old.save(update_fields=["status", "updated_at"])
        _record_audit(
            kind, tenant, user, old,
            f"The {kind.noun} {old.name} for {_where(old)} was retired because "
            f"{record.name} replaced it.",
            replaced_by=record.pk,
        )
    record.status = SetupStatus.IN_USE
    record.save(update_fields=["status", "updated_at"])
    _record_audit(
        kind, tenant, user, record,
        f"The {kind.noun} {record.name} was put in use for {_where(record)}.",
        replaced=[old.pk for old in previous],
    )
    return _queryset(kind, tenant, Q()).get(pk=record.pk)


# ── The principal's remark templates ───────────────────────────────────────

def ensure_default_templates(tenant) -> None:
    """Give the school the four default templates if it has none, on its first read."""
    if RemarkTemplate.objects.filter(tenant=tenant).exists():
        return
    RemarkTemplate.objects.bulk_create(
        [
            RemarkTemplate(tenant=tenant, band_min=band_min, text=text, order=order)
            for order, (band_min, text) in enumerate(DEFAULT_REMARK_TEMPLATES, start=1)
        ],
        ignore_conflicts=True,
    )


def templates_payload(tenant) -> dict:
    """The school's remark templates, highest band first, seeding the defaults on first read."""
    ensure_default_templates(tenant)
    rows = RemarkTemplate.objects.filter(tenant=tenant).order_by("-band_min")
    return {"templates": [{"band_min": r.band_min, "text": r.text} for r in rows]}


def _assert_remark_bands(rows) -> None:
    mins = [row["band_min"] for row in rows]
    if len(set(mins)) != len(mins):
        repeated = sorted({m for m in mins if mins.count(m) > 1})[0]
        raise RemarkBands(
            f"Two remarks start at {repeated}. Give each remark its own lowest average.",
            field="templates",
        )
    if 0 not in mins:
        raise RemarkBands(
            "Add a remark that starts at 0, so an average below the others still has one.",
            field="templates",
        )


@transaction.atomic
def replace_templates(tenant, user, rows) -> dict:
    """Replace the whole list of remark templates.

    Each lowest average is different and one is 0. Only a change is written and
    audited; sending the list as it already stands changes nothing.
    """
    _assert_remark_bands(rows)
    ensure_default_templates(tenant)
    existing = {t.band_min: t for t in RemarkTemplate.objects.filter(tenant=tenant)}
    before = {m: t.text for m, t in existing.items()}
    after = {row["band_min"]: row["text"] for row in rows}
    if before == after:
        return templates_payload(tenant)

    for band_min, template in existing.items():
        if band_min not in after:
            template.delete()
    for order, row in enumerate(sorted(rows, key=lambda r: -r["band_min"]), start=1):
        template = existing.get(row["band_min"])
        if template is None:
            RemarkTemplate.objects.create(
                tenant=tenant, band_min=row["band_min"], text=row["text"], order=order,
            )
        elif template.text != row["text"] or template.order != order:
            template.text, template.order = row["text"], order
            template.save(update_fields=["text", "order", "updated_at"])
    audit.record(
        tenant=tenant, user=user, branch=None, entity_type="RemarkTemplate",
        entity_id=tenant.pk, entity_label="Principal's remark templates",
        summary="The principal's remark templates were changed.",
        before={str(m): t for m, t in sorted(before.items(), reverse=True)},
        after={str(m): t for m, t in sorted(after.items(), reverse=True)},
    )
    return templates_payload(tenant)

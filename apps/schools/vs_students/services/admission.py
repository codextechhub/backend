"""A school's own applicant rules: its admission stages, and the documents it confirms on.

**Stages.** A school names the steps its applications pass through, in its own
order (``AdmissionStage``). A stage is where an APPLICANT stands and nothing
more: the statuses and their transitions are the same at every school, and a
school that has named no stages confirms applicants on the spot exactly as
every school always has.

**Offers.** An offer stage gives the family a number of days to accept.
Entering one sets ``offer_expires_on`` to the school's today plus those days,
unless the move names its own date, and leaving it clears the date. Nothing
acts on the date when it passes: :func:`offer_expired` answers on every read,
against the school's own calendar day, and a person decides whether to extend
the offer, move the applicant on or reject them. There is no scheduled job.

**Documents.** ``applicants.documents.required_to_confirm`` names the documents
a child must have on their record before joining the roll: an applicant before
:func:`~.enrolment.confirm_applicant` confirms them, and a child enrolled
directly in the same save that enrols them. At a school with such a list the
student import brings every row in as an applicant (``imports.py``). Its
default is none, the behaviour every school had before it could choose. It is
read and cleaned like the enrolment rules (``rules.py``): a value stored by
hand that names an unknown document drops that document rather than refusing
every confirmation at the school.

**Writes.** The PUT is the whole set: stages are created, renamed, reordered and
removed in one transaction, each change audited against the stage, and the
documents list goes through ``set_value`` and is audited as
``config.value.updated``. A stage that still holds applicants cannot be
removed. Saving the screen unchanged writes and audits nothing.
"""
from __future__ import annotations

import datetime
from dataclasses import dataclass, field

from django.db import transaction
from django.db.models import Count
from rest_framework.exceptions import NotFound, ValidationError

from vs_audit.models import AuditActionType, AuditModuleKey
from vs_audit.services import emit_audit_event
from vs_config.clock import branch_today
from vs_config.display import format_date

from ..constants import CFG_CONFIRM_DOCUMENTS, DocumentType, StudentStatus
from ..exceptions import DocumentsMissing, NotAnApplicant, StudentSettingNotRegistered
from ..models import AdmissionStage, Student, StudentDocument
from .rules import resolve_many


def _documents(value) -> tuple:
    """The stored list, cleaned to known types, in the checklist's own order."""
    if not isinstance(value, (list, tuple)):
        return ()
    wanted = {str(v) for v in value}
    return tuple(v for v in DocumentType.values if v in wanted)


def confirm_documents(tenant) -> tuple:
    """The documents an applicant at *tenant* must hold before being confirmed."""
    if tenant is None:
        return ()
    found = resolve_many((CFG_CONFIRM_DOCUMENTS,), tenant=tenant)
    value, _ = found.get(CFG_CONFIRM_DOCUMENTS, ([], None))
    return _documents(value)


def _in_words(labels) -> str:
    """"a", "a and b", "a, b and c"."""
    labels = list(labels)
    if len(labels) == 1:
        return labels[0]
    return f"{', '.join(labels[:-1])} and {labels[-1]}"


def _refuse_missing(required, held, sentence):
    """Raise ``DOCUMENTS_MISSING`` for the documents of *required* not in *held*.

    *sentence* is called with the missing documents in words ("birth
    certificate and transfer certificate") and the verb that agrees with them,
    and returns the message. ``missing`` lists them in the checklist's order,
    so a screen can offer to attach each one.
    """
    missing = [value for value in required if value not in held]
    if not missing:
        return
    labels = dict(DocumentType.choices)
    words = _in_words(labels[value].lower() for value in missing)
    verb = "is" if len(missing) == 1 else "are"
    raise DocumentsMissing(
        sentence(words, verb),
        missing=[{"value": value, "label": labels[value]} for value in missing],
    )


def assert_confirm_documents(student):
    """Refuse to confirm *student* while a document the school requires is missing.

    Checked before anything else about the confirmation, so a refusal writes
    nothing: no number is issued and no status changes.
    """
    required = confirm_documents(student.tenant)
    if not required:
        return
    held = set(
        StudentDocument.objects.filter(
            tenant=student.tenant, student=student, document_type__in=required,
        ).values_list("document_type", flat=True),
    )
    _refuse_missing(
        required, held,
        lambda words, verb: (
            f"{student.full_name} cannot be confirmed until the {words} {verb} "
            f"on their record."
        ),
    )


def assert_enrolment_documents(tenant, *, name, document_types):
    """Refuse to enrol a child directly while a document the school requires is not sent.

    Enrolling straight onto the roll skips the applicant stage, so it is held
    to the same list a confirmation is: the child named *name* is refused
    unless every required document is among *document_types*, the files sent
    with the enrolment. Called before anything is written. Saving the child as
    an applicant is never held to it, because the documents are what the
    applicant stage waits for.
    """
    required = confirm_documents(tenant)
    if not required:
        return
    _refuse_missing(
        required, set(document_types),
        lambda words, verb: (
            f"{name} cannot be enrolled until the {words} {verb} attached."
        ),
    )


def offer_expired(student, today: datetime.date | None) -> bool:
    """Whether *student*'s offer has run out: an applicant past its last day.

    False for anybody who is no longer an applicant, whatever the date says,
    because a confirmed or rejected child's offer is history and not a
    decision waiting on anyone.
    """
    return bool(
        today is not None
        and student.status == StudentStatus.APPLICANT
        and student.offer_expires_on is not None
        and student.offer_expires_on < today
    )


# ── the rules ───────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class AdmissionRules:
    stages: list = field(default_factory=list)
    required_documents_to_confirm: tuple = ()

    def as_dict(self) -> dict:
        """The body of ``GET /v1/students/admission-rules/``.

        ``document_types`` is the choice the screen offers, the same list the
        enrolment rules carry.
        """
        return {
            "stages": self.stages,
            "required_documents_to_confirm": list(self.required_documents_to_confirm),
            "document_types": [
                {"value": value, "label": label}
                for value, label in DocumentType.choices
            ],
        }


def _stage_row(stage, applicants: int) -> dict:
    return {
        "id": stage.pk, "name": stage.name, "position": stage.position,
        "is_offer": stage.is_offer, "offer_valid_days": stage.offer_valid_days,
        "applicants": applicants,
    }


def read_admission_rules(tenant, *, students=None) -> AdmissionRules:
    """The school's stages in order, each with its applicants, and the documents.

    *students* is the queryset of students the caller may count, already
    narrowed to their branches; with none, the whole school. Four queries
    whatever the school.
    """
    stages = list(AdmissionStage.objects.filter(tenant=tenant).order_by("position", "id"))
    counts = {}
    if stages:
        base = students if students is not None else Student.objects.filter(tenant=tenant)
        counts = {
            row["admission_stage"]: row["n"]
            for row in base.filter(
                status=StudentStatus.APPLICANT, admission_stage__isnull=False,
            ).order_by().values("admission_stage").annotate(n=Count("id", distinct=True))
        }
    return AdmissionRules(
        stages=[_stage_row(stage, counts.get(stage.pk, 0)) for stage in stages],
        required_documents_to_confirm=confirm_documents(tenant),
    )


def _audit_stage(tenant, actor, action_type, stage, summary, **extra):
    emit_audit_event(
        module_key=AuditModuleKey.STUDENT, action_type=action_type,
        entity_type="AdmissionStage", entity_id=str(stage.pk),
        entity_label=stage.name, tenant=tenant, actor_user=actor,
        summary=summary, **extra,
    )


def _applicant_sentence(count: int, stage_name: str) -> str:
    noun, verb = ("applicant", "is") if count == 1 else ("applicants", "are")
    return (
        f"{count} {noun} {verb} at {stage_name}. Move them to another stage "
        f"before removing it."
    )


@transaction.atomic
def write_admission_rules(
    tenant, actor, *, stages, required_documents_to_confirm, reason="",
):
    """Store the school's stages and its documents. The caller has validated each item.

    *stages* is the ordered list the screen saved: the order is the position,
    an item with an ``id`` updates that stage, one without creates a stage, and
    a stage left out is removed. Refused, with nothing written, when an ``id``
    is not one of this school's stages or when a stage left out still holds
    applicants. A removed stage leaves the confirmed and rejected records that
    passed through it with no stage.

    The school's existing stages are locked for the length of the write, so
    two admins editing them at once apply one after the other, and a move into
    a stage being removed waits for the removal and then finds no stage.
    """
    existing = {
        stage.pk: stage
        for stage in AdmissionStage.objects.select_for_update().filter(tenant=tenant)
    }
    seen: set[int] = set()
    for item in stages:
        pk = item.get("id")
        if pk is None:
            continue
        if pk not in existing:
            raise ValidationError({
                "stages": [
                    f"Stage {pk} is not one of this school's admission stages.",
                ],
            })
        if pk in seen:
            raise ValidationError({"stages": [f"Stage {pk} is listed twice."]})
        seen.add(pk)

    removed = sorted(
        (stage for pk, stage in existing.items() if pk not in seen),
        key=lambda stage: (stage.position, stage.pk),
    )
    if removed:
        held = dict(
            Student.all_objects.filter(
                tenant=tenant, status=StudentStatus.APPLICANT,
                admission_stage__in=removed,
            ).order_by().values_list("admission_stage").annotate(n=Count("id")),
        )
        for stage in removed:
            if held.get(stage.pk):
                raise ValidationError({
                    "stages": [_applicant_sentence(held[stage.pk], stage.name)],
                })

    why = (reason or "").strip() or "Admission stages set from Applicant settings."
    for stage in removed:
        _audit_stage(
            tenant, actor, AuditActionType.DELETE, stage,
            f"Admission stage {stage.name} removed. Reason: {why}",
            before_data=_stage_row(stage, 0),
        )
        stage.delete()

    # A rename can take a name another stage is giving up in the same save, so
    # every renamed stage steps aside first and the case-insensitive unique
    # name never sees the two at once.
    renamed = [
        existing[item["id"]] for item in stages
        if item.get("id") is not None
        and existing[item["id"]].name.casefold() != item["name"].casefold()
    ]
    for stage in renamed:
        AdmissionStage.all_objects.filter(pk=stage.pk).update(name=f"~{stage.pk}~")

    for position, item in enumerate(stages, start=1):
        wanted = {
            "name": item["name"], "position": position,
            "is_offer": item["is_offer"],
            "offer_valid_days": item.get("offer_valid_days") if item["is_offer"] else None,
        }
        pk = item.get("id")
        if pk is None:
            stage = AdmissionStage.all_objects.create(tenant=tenant, **wanted)
            _audit_stage(
                tenant, actor, AuditActionType.CREATE, stage,
                f"Admission stage {stage.name} added. Reason: {why}",
                metadata=_stage_row(stage, 0),
            )
            continue
        stage = existing[pk]
        changed = {
            key: {"from": getattr(stage, key), "to": value}
            for key, value in wanted.items() if getattr(stage, key) != value
        }
        if not changed:
            continue
        for key, value in wanted.items():
            setattr(stage, key, value)
        stage.save(update_fields=[*wanted, "updated_at"])
        _audit_stage(
            tenant, actor, AuditActionType.UPDATE, stage,
            f"Admission stage {stage.name} changed. Reason: {why}",
            diff_data=changed,
        )

    _write_documents(tenant, actor, required_documents_to_confirm, why)
    return read_admission_rules(tenant)


def _write_documents(tenant, actor, documents, why):
    from vs_config.models import ConfigurationDefinition
    from vs_config.services.resolution import resolve_value, set_value

    definition = ConfigurationDefinition.objects.filter(
        key=CFG_CONFIRM_DOCUMENTS, is_active=True,
    ).first()
    if definition is None:
        raise StudentSettingNotRegistered(key=CFG_CONFIRM_DOCUMENTS)
    value = list(_documents(documents))
    current, _ = resolve_value(definition, tenant=tenant)
    if current == value:
        return
    set_value(
        definition=definition, value=value, actor=actor, tenant=tenant, reason=why,
    )


# ── moving an applicant ─────────────────────────────────────────────────────

def _stage_label(stage) -> dict | None:
    return None if stage is None else {"id": stage.pk, "name": stage.name}


@transaction.atomic
def move_to_stage(student, stage, *, actor, offer_expires_on=None, reason=""):
    """Put an applicant at *stage*, or at no stage when it is None.

    Entering a different stage dates the entry today. An offer stage's last
    day is *offer_expires_on* when the move names one, otherwise today plus
    the stage's days, or none where the stage gives no number of days; any
    other stage clears it. Moving an applicant to the stage they are already
    at keeps the day they entered it and resets the offer, which is how an
    expired offer is extended. A move that changes nothing writes nothing.

    The student row is locked and re-read, so a confirmation racing the move
    cannot leave an enrolled child holding a stage it moved into afterwards.
    The stage is locked too, so a settings save removing it either sees this
    applicant and refuses, or has already removed it and the move answers 404.
    """
    if stage is not None:
        stage = AdmissionStage.all_objects.select_for_update().filter(
            pk=stage.pk, tenant_id=student.tenant_id,
        ).first()
        if stage is None:
            raise NotFound("No such admission stage at this school.")
    locked = Student.all_objects.select_for_update().get(pk=student.pk)
    if locked.status != StudentStatus.APPLICANT:
        raise NotAnApplicant(
            f"{locked.full_name} is {StudentStatus(locked.status).label.lower()}, "
            f"so there is no admission stage to move. Stages apply to "
            f"applicants only.",
            status=locked.status,
        )

    today = branch_today(locked.tenant, locked.branch_id)
    is_offer = stage is not None and stage.is_offer
    if offer_expires_on is not None:
        if not is_offer:
            raise ValidationError({
                "offer_expires_on": ["Only an offer stage has a last day to accept."],
            })
        if offer_expires_on < today:
            raise ValidationError({
                "offer_expires_on": ["The last day to accept cannot be in the past."],
            })

    same = locked.admission_stage_id == (stage.pk if stage is not None else None)
    if is_offer:
        expires = offer_expires_on or (
            today + datetime.timedelta(days=stage.offer_valid_days)
            if stage.offer_valid_days else None
        )
    else:
        expires = None
    entered = locked.stage_entered_on if same else (today if stage is not None else None)

    if same and expires == locked.offer_expires_on and entered == locked.stage_entered_on:
        return locked

    before = locked.admission_stage
    locked.admission_stage = stage
    locked.stage_entered_on = entered
    locked.offer_expires_on = expires
    locked.save(update_fields=[
        "admission_stage", "stage_entered_on", "offer_expires_on", "updated_at",
    ])

    reason = (reason or "").strip()
    from_name = before.name if before is not None else "no stage"
    to_name = stage.name if stage is not None else "no stage"
    until = format_date(expires, locked.tenant)
    if not same:
        summary = f"{locked.full_name} moved from {from_name} to {to_name}."
        if expires is not None:
            summary += f" Offer open until {until}."
    elif expires is not None:
        summary = f"{locked.full_name}'s offer at {to_name} is open until {until}."
    else:
        summary = f"{locked.full_name}'s offer at {to_name} has no last day."
    if reason:
        summary += f" Reason: {reason}"
    emit_audit_event(
        module_key=AuditModuleKey.STUDENT, action_type=AuditActionType.UPDATE,
        entity_type="Student", entity_id=str(locked.pk),
        entity_label=locked.full_name, tenant=locked.tenant, actor_user=actor,
        summary=summary,
        metadata={
            "from": _stage_label(before), "to": _stage_label(stage),
            "offer_expires_on": expires.isoformat() if expires else None,
            "reason": reason,
        },
    )
    return locked

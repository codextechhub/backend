"""The school's reasons for excusing an absence.

**The defaults are written lazily.** Every school gets Sick, Medical
appointment, Family matter, Religious observance, Travel, Representing the
school and Other the first time attendance needs its reason list, not when the
school is created. That way a school provisioned before attendance existed has
them exactly as a school created afterwards does, with no onboarding hook and
no data migration to keep in step with the list.

The defaults are written only for a school with **no reason codes at all**.
A school that has retired every default and added its own list is not given
the defaults back, and a school that has renamed one is not given a duplicate.

**A reason is never deleted.** A mark that used it keeps it, so the school's
list only grows: a reason is added, renamed, retired (kept on every mark that
used it, offered on no new one) and restored. Each change is recorded in the
audit trail against the reason. The list is the school's own, shared by every
branch, so a change binds all of them.

``used_this_term`` counts the marks that carry a reason in the term the
attendance screens open on (the one containing today, else the latest), whether
or not their register has been submitted.
"""
from __future__ import annotations

from django.db import IntegrityError, transaction
from django.db.models import Count, Max
from django.utils import timezone
from rest_framework.exceptions import NotFound

from vs_config.clock import tenant_today
from vs_audit.models import AuditActionType

from ..constants import DEFAULT_REASON_CODES
from ..exceptions import (
    DuplicateReasonCode,
    LastLiveReasonCode,
    ReasonCodeRequired,
    ReasonCodeRetired,
)
from ..models import ReasonCode, StudentMark
from . import audit
from .terms import default_term, terms_to_show


def ensure_default_reason_codes(tenant) -> bool:
    """Give *tenant* the default reason codes if it has none. Idempotent.

    Returns True when codes were written. Safe under concurrent callers: two
    requests racing to seed the same school both try, and the unique name per
    school keeps the first one's rows.
    """
    if ReasonCode.all_objects.filter(tenant=tenant).exists():
        return False
    with transaction.atomic():
        ReasonCode.all_objects.bulk_create(
            [
                ReasonCode(
                    tenant=tenant, name=name, requires_note=requires_note,
                    order_index=index,
                )
                for index, (name, requires_note) in enumerate(DEFAULT_REASON_CODES, start=1)
            ],
            ignore_conflicts=True,
        )
    return True


def live_reason_code(tenant, reason_id) -> ReasonCode:
    """The reason code *reason_id* of *tenant*, refusing a missing or retired one.

    A code of another school answers as a code that does not exist: the caller
    learns nothing about another school's list.
    """
    if reason_id in (None, ""):
        raise ReasonCodeRequired(field="reason_code")
    code = ReasonCode.all_objects.filter(tenant=tenant, pk=reason_id).first()
    if code is None:
        raise ReasonCodeRequired(
            "There is no such reason. Choose one from the list.", field="reason_code",
        )
    if code.retired_at is not None:
        raise ReasonCodeRetired(field="reason_code")
    return code



def _used_this_term(tenant) -> dict:
    """``{reason_id: marks}`` in the term the screens open on, in one grouped query."""
    today = tenant_today(tenant)
    term = default_term(terms_to_show(tenant, today), today)
    if term is None:
        return {}
    return dict(
        StudentMark.objects.filter(
            tenant=tenant, reason_code__isnull=False,
            register__session=term.session,
            register__date__range=(term.start_date, term.end_date),
        )
        .values_list("reason_code_id").annotate(n=Count("pk")).order_by(),
    )


def reason_code_rows(tenant, *, active_only: bool = False, only_ids=None) -> list:
    """The school's reasons in the order a drawer offers them, with how often each was used.

    Retired reasons are included unless *active_only*, which is what a drawer
    offering a reason for a new mark asks for.
    """
    ensure_default_reason_codes(tenant)
    codes = ReasonCode.all_objects.filter(tenant=tenant).order_by("order_index", "id")
    if active_only:
        codes = codes.filter(retired_at__isnull=True)
    if only_ids is not None:
        codes = codes.filter(pk__in=list(only_ids))
    used = _used_this_term(tenant)
    return [
        {
            "id": code.pk,
            "name": code.name,
            "counts_as_authorised": code.counts_as_authorised,
            "requires_note": code.requires_note,
            "retired": code.retired_at is not None,
            "used_this_term": used.get(code.pk, 0),
        }
        for code in codes
    ]


def reason_code_row(tenant, code: ReasonCode) -> dict:
    """One reason as the list shows it."""
    return reason_code_rows(tenant, only_ids=[code.pk])[0]


def get_reason_code(tenant, reason_id) -> ReasonCode:
    """The reason *reason_id* of *tenant*; another school's answers as one that does not exist."""
    code = ReasonCode.all_objects.filter(tenant=tenant, pk=reason_id).first()
    if code is None:
        raise NotFound("No such reason at this school.")
    return code


def _assert_name_free(tenant, name: str, *, excluding=None) -> None:
    taken = ReasonCode.all_objects.filter(tenant=tenant, name__iexact=name)
    if excluding is not None:
        taken = taken.exclude(pk=excluding.pk)
    if taken.exists():
        raise DuplicateReasonCode(
            f"There is already a reason called {taken.first().name}. "
            f"Choose a different name.",
            field="name",
        )


def _record(tenant, user, code, summary, action_type=AuditActionType.UPDATE, **metadata):
    audit.record(
        tenant=tenant, user=user, branch=None, entity_type="ReasonCode",
        entity_id=code.pk, entity_label=code.name, summary=summary,
        action_type=action_type, **metadata,
    )


def create_reason_code(
    tenant, user, *, name: str, counts_as_authorised: bool = True,
    requires_note: bool = False,
) -> ReasonCode:
    """Add a reason to the end of the school's list. The name is unique, ignoring case."""
    ensure_default_reason_codes(tenant)
    _assert_name_free(tenant, name)
    try:
        with transaction.atomic():
            last = ReasonCode.all_objects.filter(tenant=tenant).aggregate(
                last=Max("order_index"),
            )["last"]
            code = ReasonCode.all_objects.create(
                tenant=tenant, name=name, counts_as_authorised=counts_as_authorised,
                requires_note=requires_note, order_index=(last or 0) + 1,
            )
    except IntegrityError:
        raise DuplicateReasonCode(field="name") from None
    _record(
        tenant, user, code, f"The reason {code.name} was added to the excused-absence list.",
        AuditActionType.CREATE, requires_note=requires_note,
        counts_as_authorised=counts_as_authorised,
    )
    return code


def update_reason_code(tenant, user, code: ReasonCode, changes: dict) -> ReasonCode:
    """Rename a reason or change its flags. Only what differs is written and audited."""
    changed = {
        field: value for field, value in changes.items()
        if getattr(code, field) != value
    }
    if not changed:
        return code
    before = code.name
    if "name" in changed:
        _assert_name_free(tenant, changed["name"], excluding=code)
    try:
        with transaction.atomic():
            for field, value in changed.items():
                setattr(code, field, value)
            code.save(update_fields=[*changed, "updated_at"])
    except IntegrityError:
        raise DuplicateReasonCode(field="name") from None
    parts = []
    if "name" in changed:
        parts.append(f"renamed from {before} to {code.name}")
    if "requires_note" in changed:
        parts.append("now asks for a note" if code.requires_note else "no longer asks for a note")
    if "counts_as_authorised" in changed:
        parts.append(
            "now counts as authorised" if code.counts_as_authorised
            else "no longer counts as authorised"
        )
    _record(
        tenant, user, code, f"The reason {code.name} was changed: {'; '.join(parts)}.",
        **{f"new_{field}": value for field, value in changed.items()},
    )
    return code


def retire_reason_code(tenant, user, code: ReasonCode) -> ReasonCode:
    """Stop offering a reason on new marks. Marks that used it keep it. Idempotent.

    The last reason still offered cannot be retired: with none, no absence
    could be excused. The school's live reasons are locked while the check is
    made, so two administrators retiring the last two reasons at once cannot
    both succeed.
    """
    with transaction.atomic():
        live = list(
            ReasonCode.all_objects.select_for_update()
            .filter(tenant=tenant, retired_at__isnull=True).values_list("pk", flat=True),
        )
        if code.pk not in live:
            return code
        if len(live) == 1:
            raise LastLiveReasonCode()
        code.retired_at = timezone.now()
        code.save(update_fields=["retired_at", "updated_at"])
    _record(
        tenant, user, code,
        f"The reason {code.name} was retired. Marks that used it keep it; "
        f"no new mark is offered it.",
    )
    return code


def restore_reason_code(tenant, user, code: ReasonCode) -> ReasonCode:
    """Offer a retired reason on new marks again. Idempotent."""
    if code.retired_at is None:
        return code
    code.retired_at = None
    code.save(update_fields=["retired_at", "updated_at"])
    _record(tenant, user, code, f"The reason {code.name} was restored and is offered again.")
    return code

"""The school's own staff-number rule, and a branch's own where it has one.

A staff number is the school's to format: Brightfield numbers its staff
``BFS/STF/0012`` and Corona ``CSS-T-117``. So the rule lives in four
``vs_config`` definitions (required, pattern, hint, auto-issue), exactly as
the admission-number rule does in ``vs_students.services.policy``, and it
resolves through platform, school and branch scope with the same audit trail.
A school that has configured nothing gets ``required=False``, no pattern and
no automatic numbers, which is the permissive behaviour a staff number has
always had.

**A branch's rule replaces the school's whole, never key by key**, for the
reason the admission rule gives: a branch that set a pattern and inherited the
school's hint would tell its registrar to type a format it then refuses. The
branch that governs a person is their main posting (``StaffProfile.branch``);
somebody posted school-wide follows the school's rule.

**The number is also a sign-in identifier** (``vs_user.serializers`` accepts a
staff ID in place of an email), so two rules are stricter than for admission
numbers. It stays unique across the school whatever rule issued it, compared
without case as the database constraint compares it. And a number issued
automatically is never one anybody at the school has held before, including
somebody whose number was later changed: :func:`suggest_number` skips every
number the record's history has seen, so an old sign-in identifier is never
handed to a new person.

A rule is checked when a number is written: on the Add form, on the import and
on an edit that changes the number. A number stored before a pattern was set
is never re-checked and never becomes invalid.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from rest_framework.exceptions import ValidationError

from ..constants import (
    CFG_NUMBER_AUTO_ISSUE,
    CFG_NUMBER_HINT,
    CFG_NUMBER_PATTERN,
    CFG_NUMBER_REQUIRED,
    NUMBER_POLICY_KEYS,
)
from ..exceptions import StaffSettingNotRegistered

#: Where a rule came from: the branch's own, the school's, or nobody's.
SOURCE_BRANCH = "branch"
SOURCE_SCHOOL = "school"
SOURCE_DEFAULT = "default"

TAKEN = "Somebody at this school already has that staff ID."


@dataclass(frozen=True)
class StaffNumberPolicy:
    required: bool = False
    pattern: str = ""
    hint: str = ""
    auto_issue: bool = False
    source: str = SOURCE_DEFAULT

    def as_dict(self):
        return {
            "required": self.required, "pattern": self.pattern,
            "hint": self.hint, "auto_issue": self.auto_issue,
            "source": self.source,
        }


def read_policy(tenant, branch=None) -> StaffNumberPolicy:
    """The rule that governs *branch*'s staff, or the school's without one.

    Resolved as a unit: the branch's four values when it has a rule of its
    own, otherwise the school's, each of which falls back to the platform and
    then the definition's default. A platform value reads as
    ``source="default"``, because to a school it is the rule it has not chosen.
    """
    from .rules import resolve_many

    if tenant is None:
        return StaffNumberPolicy()
    found = resolve_many(NUMBER_POLICY_KEYS, tenant=tenant, branch=branch)
    branch_key = f"branch:{branch.pk}" if branch is not None else None
    school_key = f"tenant:{tenant.pk}"

    def scope_of(key):
        row = found.get(key, (None, None))[1]
        return row.scope_key if row is not None else None

    scopes = {scope_of(key) for key in NUMBER_POLICY_KEYS}
    own = branch_key is not None and branch_key in scopes

    def value(key, default):
        resolved, row = found.get(key, (None, None))
        if own and (row is None or row.scope_key != branch_key):
            # A branch's rule is whole: a value it does not hold is "none".
            return default
        return default if resolved is None else resolved

    if own:
        source = SOURCE_BRANCH
    elif school_key in scopes:
        source = SOURCE_SCHOOL
    else:
        source = SOURCE_DEFAULT
    return StaffNumberPolicy(
        required=bool(value(CFG_NUMBER_REQUIRED, False)),
        pattern=str(value(CFG_NUMBER_PATTERN, "") or ""),
        hint=str(value(CFG_NUMBER_HINT, "") or ""),
        auto_issue=bool(value(CFG_NUMBER_AUTO_ISSUE, False)),
        source=source,
    )


def compile_pattern(pattern: str):
    """*pattern* compiled anchored, or a 400 on ``pattern`` when it does not compile."""
    from core.numbering import anchored_pattern

    try:
        return anchored_pattern(pattern)
    except re.error as exc:
        raise ValidationError({
            "pattern": f"That pattern is not a valid expression: {exc}.",
        }) from exc


def refusal(tenant, number: str, *, policy: StaffNumberPolicy, exclude_pk=None) -> str:
    """Why *number* cannot be written under *policy*, or "" when it can.

    A blank number is refused only by a rule that requires one; issuing a
    number for a blank is :func:`settle_number`'s decision, not this one's.
    """
    from .numbers import staff_number_taken

    value = (number or "").strip()
    if not value:
        if policy.required:
            return (
                policy.hint
                or "This school requires a staff number for every member of staff."
            )
        return ""
    compiled = compile_pattern(policy.pattern)
    if compiled is not None and not compiled.match(value):
        return policy.hint or "That staff number is not in this school's format."
    if staff_number_taken(tenant, value, exclude_pk=exclude_pk):
        return TAKEN
    return ""


def settle_number(tenant, number, *, branch=None, exclude_pk=None,
                  issue=True, policy=None) -> str:
    """The staff number to store for a new or edited record, or a 400 on ``staff_number``.

    A number typed is checked against the rule for *branch*. A blank one is
    given the next in the series where the rule issues numbers and *issue* is
    set; the tenant row is locked first so two adds at once are handed
    different numbers, and the unique constraint stays the final guard. A
    blank the rule requires and cannot issue (the school has no number to
    continue from yet) is refused with a sentence saying so.
    """
    policy = policy or read_policy(tenant, branch)
    value = (number or "").strip()
    if not value and issue and policy.auto_issue:
        from django.db import transaction

        from vs_tenants.models import Tenant

        with transaction.atomic():
            Tenant.objects.select_for_update().filter(pk=tenant.pk).first()
            value = suggest_number(tenant, policy=policy, branch=branch)
        if not value and policy.required:
            raise ValidationError({"staff_number": (
                "This school issues staff numbers automatically but has none to "
                "continue from yet. Type this person's number, and the next "
                "will follow it."
            )})
    message = refusal(tenant, value, policy=policy, exclude_pk=exclude_pk)
    if message:
        raise ValidationError({"staff_number": message})
    return value


def held_numbers(tenant, head: str) -> set[str]:
    """Every number starting *head* that anybody at the school holds or has held.

    Lower-cased, as the unique constraint compares them. The history is read
    as well as the table, so a number changed away from somebody is still
    "taken" for the purpose of issuing a new one.
    """
    from vs_history.models import RecordVersion

    from ..history import STAFF
    from ..models import StaffProfile

    current = StaffProfile.all_objects.filter(
        tenant=tenant, staff_number__istartswith=head,
    ).values_list("staff_number", flat=True)
    past = RecordVersion.objects.filter(
        tenant=tenant, record_type=STAFF,
        data__staff_number__istartswith=head,
    ).values_list("data__staff_number", flat=True)
    return {n.lower() for n in (*current, *past) if isinstance(n, str) and n}


def suggest_number(tenant, *, policy=None, branch=None, skip=()) -> str:
    """The next staff number in the series, or "" when there is none to continue.

    Read from the numbers the school has issued, never from the pattern: the
    most recently added person's number has its trailing digits incremented
    (``core.numbering``). The series is the branch's when the branch has a rule
    of its own, and the school's otherwise. "" when the school has numbered
    nobody yet, when the latest number does not end in digits, or when its
    successor fails the school's own pattern.

    A suggestion, not a reservation: :func:`settle_number` is what issues one,
    under a lock.
    """
    from core.numbering import split_series, successor

    from ..models import StaffProfile

    policy = policy or read_policy(tenant, branch)
    series = StaffProfile.all_objects.filter(tenant=tenant).exclude(staff_number="")
    if branch is not None and policy.source == SOURCE_BRANCH:
        series = series.filter(branch=branch)
    latest = (
        series.order_by("-created_at", "-pk")
        .values_list("staff_number", flat=True)
        .first()
    )
    parts = split_series(latest or "")
    if parts is None:
        return ""
    head, digits = parts
    taken = held_numbers(tenant, head) | {n.lower() for n in skip}
    return successor(head, digits, taken=taken, compiled=compile_pattern(policy.pattern))


def write_policy(tenant, actor, *, required, pattern, hint, auto_issue=None,
                 branch=None, reason=""):
    """Set the school's rule, or *branch*'s own. Refuses an uncompilable pattern.

    For a branch all four values are written, because a branch's rule is
    whole; ``auto_issue`` left as None keeps whatever that scope reads today.
    An empty pattern or hint removes that scope's row, since vs_config stores
    no empty string: for the school it falls back to the default, and for a
    branch it reads as "none".
    """
    from django.db import transaction

    from vs_config.services.resolution import clear_value, resolve_value, set_value

    compile_pattern(pattern)
    if auto_issue is None:
        auto_issue = read_policy(tenant, branch).auto_issue
    wanted = {
        CFG_NUMBER_REQUIRED: bool(required), CFG_NUMBER_PATTERN: pattern or "",
        CFG_NUMBER_HINT: (hint or "").strip(), CFG_NUMBER_AUTO_ISSUE: bool(auto_issue),
    }
    where = "a branch's" if branch is not None else "the school's"
    why = (reason or "").strip() or f"Staff number rule set from {where} Staff settings."
    scope_key = f"branch:{branch.pk}" if branch is not None else f"tenant:{tenant.pk}"
    with transaction.atomic():
        for key, value in wanted.items():
            definition = _definition(key)
            current, row = resolve_value(definition, tenant=tenant, branch=branch)
            held_here = row is not None and row.scope_key == scope_key
            if isinstance(value, str) and not value.strip():
                if held_here:
                    clear_value(
                        definition=definition, actor=actor, tenant=tenant,
                        branch=branch, reason=why,
                    )
                continue
            if held_here and current == value:
                continue
            set_value(
                definition=definition, value=value, actor=actor, tenant=tenant,
                branch=branch, reason=why,
            )
    return read_policy(tenant, branch)


def reset_branch_policy(tenant, branch, actor, *, reason=""):
    """Remove *branch*'s own rule, so its staff follow the school's again."""
    from django.db import transaction

    from vs_config.services.resolution import clear_value

    why = (reason or "").strip() or (
        "A branch's staff number rule removed; it follows the school's again."
    )
    with transaction.atomic():
        for key in NUMBER_POLICY_KEYS:
            clear_value(
                definition=_definition(key), actor=actor, tenant=tenant,
                branch=branch, reason=why,
            )
    return read_policy(tenant, branch)


def _definition(key):
    from vs_config.models import ConfigurationDefinition

    definition = ConfigurationDefinition.objects.filter(key=key, is_active=True).first()
    if definition is None:
        raise StaffSettingNotRegistered(key=key)
    return definition

"""The school's own admission-number rule, and a branch's own where it has one.

The design validates an admission number against ``BFS/YYYY/NNNN`` and refuses
anything else, which is right for Brightfield and wrong for the platform.
Corona Secondary School numbers its students ``CSS-24-0117``; under a
hard-coded Brightfield pattern it could register no child at all.

So the rule is the school's, not the column's. It lives in four ``vs_config``
definitions (required, pattern, hint, auto-issue), which already resolve
through platform, school and branch scope and already have a write surface and
an audit trail. A school that has configured nothing gets ``required=False``,
no pattern and no automatic numbers, which is exactly the permissive behaviour
the column had before this existed.

**A branch's rule replaces the school's whole, never key by key.** Lekki
numbering ``LEK/0001`` and Ikeja following the school's ``CSS-24-0117`` are two
series; a branch that set a pattern and inherited the school's hint would tell
its registrar to type a format it then refuses. So a branch either has all four
values of its own (``source="branch"``) or follows the school's rule entirely.
Writing a branch's rule writes all four at branch scope, and an empty pattern
or hint there means "none" rather than "the school's": vs_config stores no
empty string, so the branch row is simply absent, and the two booleans, which
are always stored, are what mark the branch as having a rule at all.

Admission numbers stay unique across the whole school whichever rule issued
them; that is the database's constraint and no branch rule loosens it.

FRD M11 v2.4 section 7.7 and FR-019.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from vs_config.models import ConfigurationDefinition

from ..constants import (
    ADMISSION_POLICY_KEYS,
    CFG_ADM_AUTO_ISSUE,
    CFG_ADM_HINT,
    CFG_ADM_PATTERN,
    CFG_ADM_REQUIRED,
)
from ..exceptions import (
    AdmissionNumberFormat,
    AdmissionNumberRequired,
    AdmissionPolicyNotRegistered,
    InvalidAdmissionPattern,
)

#: Where a rule came from: the branch's own, the school's, or nobody's.
SOURCE_BRANCH = "branch"
SOURCE_SCHOOL = "school"
SOURCE_DEFAULT = "default"


@dataclass(frozen=True)
class AdmissionPolicy:
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


def read_policy(tenant, branch=None) -> AdmissionPolicy:
    """The rule that governs *branch*'s students, or the school's without one.

    Resolved as a unit (see the module docstring): the branch's four values
    when it has a rule of its own, otherwise the school's, each of which falls
    back to the platform and then the definition's default as every setting
    does. A platform value reads as ``source="default"``, because to a school
    it is the rule it has not chosen.
    """
    from .rules import resolve_many

    found = resolve_many(ADMISSION_POLICY_KEYS, tenant=tenant, branch=branch)
    branch_key = f"branch:{branch.pk}" if branch is not None else None
    school_key = f"tenant:{tenant.pk}" if tenant is not None else None

    def scope_of(key):
        row = found.get(key, (None, None))[1]
        return row.scope_key if row is not None else None

    scopes = {scope_of(key) for key in ADMISSION_POLICY_KEYS}
    own = branch_key is not None and branch_key in scopes

    def value(key, default):
        resolved, row = found.get(key, (None, None))
        if own and (row is None or row.scope_key != branch_key):
            # The branch's rule is whole: a value it does not hold is "none".
            return default
        return default if resolved is None else resolved

    if own:
        source = SOURCE_BRANCH
    elif school_key is not None and school_key in scopes:
        source = SOURCE_SCHOOL
    else:
        source = SOURCE_DEFAULT
    return AdmissionPolicy(
        required=bool(value(CFG_ADM_REQUIRED, False)),
        pattern=str(value(CFG_ADM_PATTERN, "") or ""),
        hint=str(value(CFG_ADM_HINT, "") or ""),
        auto_issue=bool(value(CFG_ADM_AUTO_ISSUE, False)),
        source=source,
    )


def compile_pattern(pattern: str):
    """Compile *pattern* anchored, refusing one that does not compile.

    Anchored by the service and not by the school, so a school cannot write one
    that matches a substring of a longer number: ``BFS/2025/0142XYZ`` must fail
    a school whose rule is ``BFS/\\d{4}/\\d{4}``.
    """
    from core.numbering import anchored_pattern

    try:
        return anchored_pattern(pattern)
    except re.error as exc:
        raise InvalidAdmissionPattern(
            f"That pattern is not a valid expression: {exc}.",
        ) from exc


def assert_number_allowed(
    tenant, number: str, *, policy: AdmissionPolicy | None = None, branch=None,
):
    """Check a supplied admission number against the rule for *branch*.

    *policy* is that rule already read. On write only. A number stored before a pattern was set is never
    re-validated and never becomes invalid; section 14, decision 17 records
    that as a choice rather than an oversight.
    """
    policy = policy or read_policy(tenant, branch)
    value = (number or "").strip()

    if not value:
        if policy.required:
            raise AdmissionNumberRequired(
                policy.hint
                or "This school requires an admission number for every student.",
                hint=policy.hint,
            )
        return value

    compiled = compile_pattern(policy.pattern)
    if compiled is not None and not compiled.match(value):
        raise AdmissionNumberFormat(
            policy.hint
            or "That admission number is not in this school's format.",
            hint=policy.hint, pattern=policy.pattern,
        )
    return value


def write_policy(
    tenant, actor, *, required=None, pattern=None, hint=None, auto_issue=None,
    branch=None,
):
    """Set the school's rule, or *branch*'s own. Refuses an uncompilable pattern.

    A pattern that does not compile must be refused here rather than discovered
    at the next enrolment, when it would look like a broken enrolment form.

    For the school, a value left as None is left as it is. For a branch, all
    four are written, because a branch's rule is whole: a value left as None
    keeps whatever the branch reads today, which for a branch with no rule yet
    is the school's.
    """
    from django.db import transaction

    from vs_config.services.resolution import clear_value, set_value

    if pattern is not None:
        compile_pattern(pattern)

    wanted = {
        CFG_ADM_REQUIRED: required, CFG_ADM_PATTERN: pattern,
        CFG_ADM_HINT: hint, CFG_ADM_AUTO_ISSUE: auto_issue,
    }
    if branch is not None:
        current = read_policy(tenant, branch).as_dict()
        for key, field in (
            (CFG_ADM_REQUIRED, "required"), (CFG_ADM_PATTERN, "pattern"),
            (CFG_ADM_HINT, "hint"), (CFG_ADM_AUTO_ISSUE, "auto_issue"),
        ):
            if wanted[key] is None:
                wanted[key] = current[field]

    where = "a branch's" if branch is not None else "the school's"
    with transaction.atomic():
        for key, value in wanted.items():
            if value is None:
                continue
            definition = _definition(key)

            # An empty string is not a value vs_config will store: it treats
            # one as "unset", and set_value refuses it outright. So clearing a
            # pattern means REMOVING this layer's row, not writing "" over it.
            # For the school that falls back to the platform default; for a
            # branch it reads as "none", because a branch's rule is whole.
            if isinstance(value, str) and not value.strip():
                clear_value(
                    definition=definition, actor=actor, tenant=tenant,
                    branch=branch,
                    reason=f"Admission number rule cleared from {where} Student settings.",
                )
                continue

            set_value(
                definition=definition, value=value, actor=actor, tenant=tenant,
                branch=branch,
                reason=f"Admission number policy set from {where} Student settings.",
            )
    return read_policy(tenant, branch)


def reset_branch_policy(tenant, branch, actor):
    """Remove *branch*'s own rule, so its students follow the school's again."""
    from django.db import transaction

    from vs_config.services.resolution import clear_value

    with transaction.atomic():
        for key in ADMISSION_POLICY_KEYS:
            clear_value(
                definition=_definition(key), actor=actor, tenant=tenant,
                branch=branch,
                reason="A branch's admission number rule removed; it follows "
                       "the school's again.",
            )
    return read_policy(tenant, branch)


def _definition(key):
    definition = ConfigurationDefinition.objects.filter(
        key=key, is_active=True,
    ).first()
    if definition is None:
        # The seeder has not run. Said with a code of its own rather than
        # silently storing nothing and reporting success.
        raise AdmissionPolicyNotRegistered(key=key)
    return definition


def suggest_number(
    tenant, *, policy: AdmissionPolicy | None = None, branch=None, skip=(),
) -> str:
    """The next admission number, derived from the ones this school already issues.

    **Read from the school's own numbers, never from the pattern.** The design
    pre-fills ``BFS/YYYY/NNNN`` by counting up inside it, which is right for
    Brightfield and wrong for the platform: Corona numbers its students
    ``CSS-24-0117``, and a regular expression cannot be inverted into "the next
    one" in the general case anyway. So this takes the number the registrar
    most recently issued and increments its trailing run of digits, which works
    for both without knowing either format:

        BFS/2025/0142  ->  BFS/2025/0143
        CSS-24-0117    ->  CSS-24-0118

    Zero padding is preserved, so 0099 becomes 0100 rather than 100, and the
    width only grows when the digits genuinely overflow it.

    Returns ``""`` - meaning "no suggestion" - rather than guessing when:

    * the school has issued nothing yet, so there is no series to continue;
    * the most recent number does not END in digits, so there is no successor to
      read - note the anchor: "BFS/2025/A" must not become "BFS/2026/A";
    * the successor does not satisfy the school's own pattern, which is what
      happens at a year boundary when the year is inside the number. Offering
      ``BFS/2025/0143`` in the 2026 session would be a confident wrong answer,
      and an empty box the registrar fills in is better than a plausible one
      they do not check.

    **The series is the branch's when the branch has a rule of its own.** Lekki
    numbering ``LEK/0041`` beside the school's ``CSS-24-0117`` is a second
    series, and continuing whichever number was issued last anywhere would
    offer Lekki a CSS number its own pattern refuses. A branch following the
    school's rule shares the school's series. Either way a number already held
    anywhere in the school is skipped, because numbers are unique school-wide,
    and so is anything in *skip*: numbers the caller has just lost to another
    enrolment.

    The caller must still validate: this suggests, it does not reserve, and two
    registrars enrolling at once can be handed the same number. The unique
    constraint is what actually prevents the collision.
    """
    from core.numbering import split_series, successor

    from ..models import Student

    policy = policy or read_policy(tenant, branch)
    series = Student.objects.filter(tenant=tenant)
    if branch is not None and policy.source == SOURCE_BRANCH:
        series = series.filter(branch=branch)
    latest = (
        series.exclude(student_number="")
        .order_by("-created_at", "-pk")
        .values_list("student_number", flat=True)
        .first()
    )
    if not latest:
        return ""

    # A number that does not end in digits has no successor to read.
    parts = split_series(latest)
    if parts is None:
        return ""

    head, digits = parts
    # Without case, as the unique constraint compares them.
    taken = {
        n.lower() for n in
        Student.objects.filter(tenant=tenant, student_number__istartswith=head)
        .values_list("student_number", flat=True)
    } | {n.lower() for n in skip}
    return successor(
        head, digits, taken=taken, compiled=compile_pattern(policy.pattern),
    )

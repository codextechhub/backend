"""The school's own admission-number rule.

The design validates an admission number against ``BFS/YYYY/NNNN`` and refuses
anything else, which is right for Brightfield and wrong for the platform.
Corona Secondary School numbers its students ``CSS-24-0117``; under a
hard-coded Brightfield pattern it could register no child at all.

So the rule is the school's, not the column's. It lives in three
``vs_config`` definitions, which already resolve through platform and school
scope and already have a write surface and an audit trail. A school that has
configured nothing gets ``required=False`` and no pattern, which is exactly the
permissive behaviour the column had before this existed.

FRD M11 v2.4 section 7.7 and FR-019.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from vs_config.conf import get_config
from vs_config.models import ConfigurationDefinition

from ..constants import CFG_ADM_HINT, CFG_ADM_PATTERN, CFG_ADM_REQUIRED
from ..exceptions import (
    AdmissionNumberFormat,
    AdmissionNumberRequired,
    AdmissionPolicyNotRegistered,
    InvalidAdmissionPattern,
)


@dataclass(frozen=True)
class AdmissionPolicy:
    required: bool = False
    pattern: str = ""
    hint: str = ""

    def as_dict(self):
        return {"required": self.required, "pattern": self.pattern, "hint": self.hint}


def read_policy(tenant) -> AdmissionPolicy:
    return AdmissionPolicy(
        required=bool(get_config(CFG_ADM_REQUIRED, default=False, tenant=tenant)),
        pattern=str(get_config(CFG_ADM_PATTERN, default="", tenant=tenant) or ""),
        hint=str(get_config(CFG_ADM_HINT, default="", tenant=tenant) or ""),
    )


def compile_pattern(pattern: str):
    """Compile *pattern* anchored, refusing one that does not compile.

    Anchored by the service and not by the school, so a school cannot write one
    that matches a substring of a longer number: ``BFS/2025/0142XYZ`` must fail
    a school whose rule is ``BFS/\\d{4}/\\d{4}``.
    """
    if not pattern:
        return None
    body = pattern
    if body.startswith("^"):
        body = body[1:]
    if body.endswith("$") and not body.endswith("\\$"):
        body = body[:-1]
    try:
        return re.compile(f"^(?:{body})$")
    except re.error as exc:
        raise InvalidAdmissionPattern(
            f"That pattern is not a valid expression: {exc}.",
        ) from exc


def assert_number_allowed(tenant, number: str, *, policy: AdmissionPolicy | None = None):
    """Check a supplied admission number against the school's own rule.

    On write only. A number stored before a pattern was set is never
    re-validated and never becomes invalid; section 14, decision 17 records
    that as a choice rather than an oversight.
    """
    policy = policy or read_policy(tenant)
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


def write_policy(tenant, actor, *, required=None, pattern=None, hint=None):
    """Set the school's rule. Refuses an uncompilable pattern before storing it.

    A pattern that does not compile must be refused here rather than discovered
    at the next enrolment, when it would look like a broken enrolment form.
    """
    from vs_config.services.resolution import set_value

    if pattern is not None:
        compile_pattern(pattern)

    from vs_config.services.resolution import clear_value

    for key, value in (
        (CFG_ADM_REQUIRED, required),
        (CFG_ADM_PATTERN, pattern),
        (CFG_ADM_HINT, hint),
    ):
        if value is None:
            continue
        definition = ConfigurationDefinition.objects.filter(
            key=key, is_active=True,
        ).first()
        if definition is None:
            # The seeder has not run. Said with a code of its own rather than
            # silently storing nothing and reporting success - which is what a
            # school would otherwise see, followed by every enrolment ignoring
            # the rule they had just set.
            raise AdmissionPolicyNotRegistered(key=key)

        # An empty string is not a value vs_config will store: it treats one as
        # "unset", and set_value refuses it outright. So clearing a pattern
        # means REMOVING the school's row and falling back to the platform
        # default, not writing "" over it. Without this a school that ever set
        # a pattern could never take it off again, and the refusal it saw would
        # be about a string rather than about its own rule.
        if isinstance(value, str) and not value.strip():
            clear_value(
                definition=definition, actor=actor, tenant=tenant,
                reason="Admission number rule cleared from Student Management.",
            )
            continue

        set_value(
            definition=definition, value=value, actor=actor, tenant=tenant,
            reason="Admission number policy set from Student Management.",
        )
    return read_policy(tenant)


#: Bounded so a pathological roll cannot spin: if twenty consecutive successors
#: are all taken, the school is not numbering the way this reads it and no
#: suggestion is better than a wrong one.
_SUGGEST_TRIES = 20


def suggest_number(tenant, *, policy: AdmissionPolicy | None = None) -> str:
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

    The caller must still validate: this suggests, it does not reserve, and two
    registrars enrolling at once can be handed the same number. The unique
    constraint is what actually prevents the collision.
    """
    from ..models import Student

    policy = policy or read_policy(tenant)
    latest = (
        Student.objects.filter(tenant=tenant)
        .exclude(student_number="")
        .order_by("-created_at")
        .values_list("student_number", flat=True)
        .first()
    )
    if not latest:
        return ""

    # Anchored to the END, not merely the last run of digits anywhere. Without
    # the anchor "BFS/2025/A" matched the YEAR and suggested "BFS/2026/A" - a
    # confident wrong answer of exactly the kind this function exists to avoid.
    # A number that does not end in digits has no successor we can read.
    match = re.search(r"(\d+)$", latest)
    if match is None:
        return ""

    head, digits = latest[: match.start(1)], match.group(1)
    compiled = compile_pattern(policy.pattern)
    taken = set(
        Student.objects.filter(tenant=tenant, student_number__startswith=head)
        .values_list("student_number", flat=True),
    )

    value = int(digits)
    for _ in range(_SUGGEST_TRIES):
        value += 1
        # Grow the width only on a real overflow: 0099 -> 0100, not 100.
        candidate = f"{head}{str(value).zfill(len(digits))}"
        if candidate in taken:
            continue
        if compiled is not None and not compiled.match(candidate):
            return ""
        return candidate
    return ""

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

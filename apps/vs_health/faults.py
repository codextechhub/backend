"""Incidents for configuration faults found outside the alert engine.

A threshold breach arrives through ``AlertRule`` and ``Alert``, which carry an
observed value and the threshold it crossed. A configuration fault has neither.
It is a deployment or a data arrangement that is simply wrong: there is no metric
to compare, no value to quote, and it stays wrong until somebody changes it.
Hanging one on an ``AlertRule`` would mean inventing a rule, a metric and a
threshold that do not exist, so a fault opens an ``Incident`` directly.
``Alert.incident`` is nullable, so an incident standing on its own is a shape the
model already supports and the health console already lists.

The caller is typically a code path that runs on every request the fault
affects, so deduplication is the point of this module rather than a refinement
of it. ``Incident.fault_key`` holds a stable identity for the fault, and a key
that already names an unresolved incident opens nothing and appends nothing to
that incident's timeline. Appending would cost a write per request and bury the
opening entry that says what to do, which is the same failure as opening a
second incident, only harder to see.
"""
from __future__ import annotations

import logging

from .models import Incident, Severity

logger = logging.getLogger(__name__)


def report_configuration_fault(
    *,
    fault_key: str,
    title: str,
    summary: str,
    severity: int = Severity.SEV2,
    affected_tenant_count: int = 0,
    who: str = "Configuration check",
) -> Incident | None:
    """Open an incident for *fault_key*, unless one is already unresolved.

    Returns the incident it opened, or ``None`` when the fault is already on the
    board, which is the ordinary answer on a path that runs per request.

    ``fault_key`` is the caller's own stable identity for one instance of one
    fault, meaning one subject's broken arrangement rather than the class of
    fault, and it is matched exactly. No serializer exposes it, so an operator
    renaming an incident, reassigning its services or editing its summary cannot
    break the match and turn a per-request path back into one incident per
    request. Resolving is the supported way to let the next occurrence open a
    fresh incident: a resolved incident keeps its key and stops matching.

    Two requests arriving together can both find nothing and both open one. That
    is a burst of two rather than a flood of thousands, and it is preferred here
    to holding a lock on a read path.
    """
    if not fault_key:
        raise ValueError("A configuration fault needs a fault_key to deduplicate on.")

    already_open = (
        Incident.objects.filter(fault_key=fault_key)
        .exclude(status=Incident.Status.RESOLVED)
        .exists()
    )
    if already_open:
        return None

    incident = Incident.objects.create(
        fault_key=fault_key,
        title=title[:255],
        severity=severity,
        status=Incident.Status.INVESTIGATING,
        source=Incident.Source.AUTO,
        owner_label=who,
        team="Platform",
        summary=summary,
        affected_tenant_count=affected_tenant_count,
    )
    incident.add_event(kind="opened", who=who, text=summary)
    logger.error(
        "Configuration fault %s opened incident %s.", fault_key, incident.code,
    )
    return incident

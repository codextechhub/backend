"""Privacy-safe product analytics for the Console how-to system.

This pipeline answers whether guides help people complete work. It is not an
audit trail: events are disposable, carry no actor or business-record identity,
and are always aggregated before they leave the backend.
"""
from __future__ import annotations

import datetime
import logging
import re
import unicodedata

from django.db.models import Count
from django.utils import timezone

from .constants import GuideAnalyticsEventName

logger = logging.getLogger(__name__)

RETENTION_DAYS = 180

# Search analytics must help editors without retaining arbitrary user text,
# so only words in this closed vocabulary survive: "invite Ada Okafor" is
# stored as "invite [redacted]".
SAFE_SEARCH_TERMS = frozenset({
    "account", "activate", "activation", "add", "admin", "allocate", "analytics",
    "approval", "approve", "asset", "assign", "audit", "balance", "bank", "batch",
    "branch", "budget", "build", "cancel", "card", "cash", "category", "change",
    "claim", "close", "collection", "complete", "concession", "configure", "console",
    "contract", "create", "credit", "customer", "data", "delete", "denied",
    "department", "download", "dunning", "edit", "email", "entity", "error",
    "expense", "export", "failed", "fee", "file", "finance", "forgot", "gateway",
    "guide", "health", "help", "import", "integration", "inventory", "invitation",
    "invite", "invoice", "item", "journal", "lock", "login", "manage", "match",
    "notification", "onboarding", "organogram", "password", "pay", "payment",
    "payroll", "permission", "permissioned", "plan", "post", "procurement", "profile",
    "proxy", "purchase", "queue", "receipt", "reconcile", "refund", "report", "reset",
    "resolve", "review", "rfq", "role", "school", "security", "session", "settings",
    "settlement", "sign", "signin", "staff", "statement", "stock", "submit", "support",
    "task", "tax", "template", "tenant", "ticket", "transaction", "transfer", "trial",
    "unlock", "upload", "user", "vendor", "virtual", "walkthrough", "webhook",
    "workflow", "write", "writeoff",
})

_TOKEN_RE = re.compile(r"[a-z]+")
_REDACTED = "[redacted]"


def sanitise_search_query(value: str) -> str:
    """Return useful task words while refusing arbitrary user-entered values."""

    normalised = unicodedata.normalize("NFKC", str(value or "")).lower()
    tokens = _TOKEN_RE.findall(normalised)[:12]
    safe: list[str] = []
    for token in tokens:
        replacement = token if token in SAFE_SEARCH_TERMS else _REDACTED
        if not safe or safe[-1] != replacement:
            safe.append(replacement)
    return " ".join(safe)[:160]


def record(*, payload: dict):
    """Write one validated event without ever blocking the user's guide flow."""

    try:
        from .models import GuideAnalyticsEvent

        values = dict(payload)
        values.pop("result_count", None)
        if values.get("name") == GuideAnalyticsEventName.SEARCH_NO_RESULTS:
            values["search_query"] = sanitise_search_query(values.pop("query", ""))
        else:
            values.pop("query", None)
        return GuideAnalyticsEvent.objects.create(**values)
    except Exception:  # pragma: no cover - telemetry never blocks product work
        logger.warning("Guide analytics event failed to record.", exc_info=True)
        return None


def summary(*, since=None) -> dict:
    """Return bounded aggregates only, never analytics rows or tenant splits."""

    from .models import GuideAnalyticsEvent

    since = since or (timezone.now() - datetime.timedelta(days=30))
    events = GuideAnalyticsEvent.objects.filter(occurred_at__gte=since)

    totals = {
        name: events.filter(name=name).count()
        for name in GuideAnalyticsEventName.values
    }
    guide_rows = list(
        events.exclude(guide_id="")
        .values("guide_id", "name", "outcome")
        .annotate(count=Count("id"))
        .order_by("guide_id", "name", "outcome")
    )
    per_guide: dict[str, dict] = {}
    for row in guide_rows:
        guide = per_guide.setdefault(row["guide_id"], {
            "guide_id": row["guide_id"],
            "views": 0,
            "completions": 0,
            "helpful": 0,
            "not_helpful": 0,
            "outdated_reports": 0,
            "walkthrough_exits": 0,
            "walkthrough_finishes": 0,
        })
        name = row["name"]
        outcome = row["outcome"]
        count = row["count"]
        if name == GuideAnalyticsEventName.GUIDE_VIEWED:
            guide["views"] += count
        elif name == GuideAnalyticsEventName.GUIDE_COMPLETED:
            guide["completions"] += count
        elif name == GuideAnalyticsEventName.HELPFUL_VOTED:
            guide["helpful" if outcome == "helpful" else "not_helpful"] += count
        elif name == GuideAnalyticsEventName.OUTDATED_REPORTED:
            guide["outdated_reports"] += count
        elif name == GuideAnalyticsEventName.WALKTHROUGH_EXITED:
            guide["walkthrough_exits"] += count
            if outcome == "finished":
                guide["walkthrough_finishes"] += count

    no_result_searches = list(
        events.filter(name=GuideAnalyticsEventName.SEARCH_NO_RESULTS)
        .exclude(search_query="")
        .values("search_query", "route_pattern")
        .annotate(count=Count("id"))
        .order_by("-count", "search_query")[:25]
    )
    walkthrough_exits = list(
        events.filter(name=GuideAnalyticsEventName.WALKTHROUGH_EXITED)
        .values("guide_id", "walkthrough_id", "step_id", "outcome")
        .annotate(count=Count("id"))
        .order_by("-count", "guide_id", "step_id")[:50]
    )

    return {
        "since": since.date().isoformat(),
        "totals": totals,
        "guides": sorted(per_guide.values(), key=lambda row: row["guide_id"]),
        "no_result_searches": no_result_searches,
        "walkthrough_exits": walkthrough_exits,
    }


def prune(*, now=None) -> int:
    """Delete telemetry older than the disposable retention window."""

    from .models import GuideAnalyticsEvent

    cutoff = (now or timezone.now()) - datetime.timedelta(days=RETENTION_DAYS)
    deleted, _ = GuideAnalyticsEvent.objects.filter(occurred_at__lt=cutoff).delete()
    return deleted

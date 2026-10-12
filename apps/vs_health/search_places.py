"""The platform health screens the search bar can route to.

Every place reads with ``platform.health.view``, the platform's own key, so
only platform staff see them, in the console.
"""
from __future__ import annotations

from vs_search.registry import ACTION, Place, register

HEALTH_VIEW = "platform.health.view"

#: (screen, label, description, keywords).
SCREENS = (
    ("health.command_center", "Health command center",
     "Every service's state, open incidents and recent deployments.", ("health", "status")),
    ("health.uptime", "Uptime", "Uptime monitors and their history.", ("health", "downtime")),
    ("health.api", "API and endpoints", "Request rates, errors and response times.",
     ("health", "latency", "errors")),
    ("health.jobs", "Jobs and queues", "Background jobs and their queues.",
     ("health", "tasks", "workers")),
    ("health.incidents", "Incidents and alerts", "Open and past incidents, and the alerts behind them.",
     ("health", "outage", "alerts")),
    ("health.tenants", "Tenant health", "How each school's service is running.",
     ("health", "schools")),
    ("health.slos", "Service level objectives", "Targets and how much error budget is left.",
     ("health", "slo", "error budget")),
)

register(*(
    Place(
        kind=ACTION, key=screen, label=label, description=description,
        keywords=keywords, screen=screen, permission=HEALTH_VIEW,
    )
    for screen, label, description, keywords in SCREENS
))

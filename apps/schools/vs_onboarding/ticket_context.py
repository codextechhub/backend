"""What a support ticket may carry about a school's onboarding.

Registered from :meth:`schools.vs_onboarding.apps.VsOnboardingConfig.ready`
into ``vs_tickets``, the same way school datasets are published to the Export
Centre. The direction matters: ``vs_tickets`` is a domain-neutral platform app
and must never import anything under ``apps/schools/``, so the module that owns
the vocabulary is the module that publishes it.

Two keys, both closed vocabularies taken straight from this module's own
constants:

``onboarding_task_key``
    Which checklist step the person was on, from the catalog.
``onboarding_readiness_state``
    Where the school stood against the go-live gate.

Closed is the whole point. A support ticket is read by staff outside the
tenant, and a context field that accepted free text would be a place for a
school's own words about a named person to end up in a queue that was never
scoped to them. A task key cannot carry that; there are eight of them and they
are all constants.

The third fact the owner allowed, the product area, needs nothing from here:
``Onboarding`` is one of the platform-wide ``product_area`` values declared in
``vs_tickets`` itself, alongside Finance and Exports.
"""
from __future__ import annotations

from .constants import ReadinessState, TaskKey


def register_ticket_context() -> None:
    """Publish this module's ticket-context keys. Called once from ready()."""
    from vs_tickets.context import register_context_choice_field

    register_context_choice_field(
        "onboarding_task_key",
        choices=TaskKey.values,
        description="The onboarding checklist step the ticket was raised from.",
    )
    register_context_choice_field(
        "onboarding_readiness_state",
        choices=ReadinessState.values,
        description="The school's go-live readiness when the ticket was raised.",
    )

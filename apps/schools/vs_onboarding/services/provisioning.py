"""FR-001: give a tenant its control room.

Provisioning is idempotent by construction. Re-running it adds catalog entries
that are missing and touches nothing that already exists, including a task's
status: a school that has completed four steps and then gains a fifth from a
catalog change must not lose the four.

``is_required`` and ``order_index`` are read from the catalog and never from a
request. Whether a *conditional* entry exists at all is decided here too, via
``CatalogEntry.applies``, so the control room shows no empty column for a step
a school could never fill in. Every entry in the catalog is unconditional
today; the seam is still the only route by which a task row is created.
"""
from __future__ import annotations

import logging

from django.db import transaction

from vs_audit.models import AuditActionType

from ..constants import TASK_CATALOG
from ..exceptions import OnboardingTenantNotSchool
from ..models import OnboardingProgress, OnboardingTask
from . import effects
from .scoping import rows_for

logger = logging.getLogger("vs_onboarding")


def applicable_catalog(tenant, school) -> list:
    """The catalog entries this particular school has."""
    return [entry for entry in TASK_CATALOG if entry.applies(tenant, school)]


def provision_onboarding(tenant, *, actor=None) -> OnboardingProgress:
    """Create (or top up) the progress row and task set for ``tenant``.

    Returns the progress row. Raises
    :class:`~schools.vs_onboarding.exceptions.OnboardingTenantNotSchool` for a
    tenant that is not a school, because onboarding is a school flow and a
    platform or organization tenant given a school's checklist would be a set
    of steps nobody can ever complete.
    """
    from vs_tenants.models import Tenant

    if getattr(tenant, "kind", None) != Tenant.Kind.SCHOOL:
        raise OnboardingTenantNotSchool()

    school = effects.school_of(tenant)
    if school is None:
        raise OnboardingTenantNotSchool(
            "This tenant has no school profile, so onboarding cannot be set up "
            "for it.",
        )

    with transaction.atomic():
        progress, progress_created = OnboardingProgress.all_objects.get_or_create(
            tenant=tenant,
        )

        existing = set(
            rows_for(OnboardingTask, tenant).values_list("key", flat=True)
        )
        added = []
        for entry in applicable_catalog(tenant, school):
            if entry.key in existing:
                continue
            OnboardingTask.all_objects.create(
                tenant=tenant,
                key=entry.key,
                title=entry.title,
                is_required=entry.is_required,
                order_index=entry.order_index,
            )
            # Track it as present immediately: a catalog that ever grew two
            # entries with the same key would otherwise take the unique
            # constraint out on the second one, turning an editing slip into a
            # failed provisioning for every school.
            existing.add(entry.key)
            added.append(entry.key)

    # One event per provisioning call, including a top-up that added nothing:
    # "we looked and there was nothing to add" is a fact worth being able to
    # find later.
    effects.record(
        tenant=tenant,
        action_type=AuditActionType.ONBOARDING_PROVISIONED,
        entity_type="OnboardingProgress",
        entity_id=progress.pk,
        actor=actor,
        entity_label=school.name,
        diff_data={
            "created": progress_created,
            "tasks_added": [str(key) for key in added],
        },
    )
    return progress


def provision_onboarding_for_school(school, *, actor=None):
    """Best-effort provisioning from the school-creation path. Never raises.

    A school, its tenant, its first administrator, its branches and its
    entitlements are worth far more than its checklist, and the checklist can
    be rebuilt at any time by calling this again. So a failure here is caught,
    logged loudly enough to be found, and does not take the school down with
    it.

    The nested ``transaction.atomic()`` is the point, and catching alone would
    not be enough. School creation runs inside its own atomic block; a
    *database* failure inside that block leaves the transaction aborted, every
    later statement fails too, and the outer commit takes the school with it.
    The inner block opens a savepoint, so a failure here rolls back only the
    onboarding rows. (A plain Python exception would leave the connection
    healthy either way, which is why the test for this forces a failed
    statement rather than a raised object.)
    """
    try:
        with transaction.atomic():
            return provision_onboarding(school.tenant, actor=actor)
    except Exception:
        logger.exception(
            "Could not provision onboarding for school %s (tenant %s). The "
            "school was created without a control room; re-run provisioning "
            "for it via POST /v1/onboarding/provision/.",
            school.slug, getattr(school.tenant, "slug", school.tenant_id),
        )
        return None

# vs_config/signals.py
#
# Django signals for the vs_config module.
#
# Module 6 is primarily driven by explicit service calls rather than signals,
# because configuration changes are always initiated by a known actor through
# a specific API endpoint. Signals are used here only for cross-module
# integration points where vs_config must react to events owned by other apps.
#
# Current signals:
#
#   post_save on vs_schools.Branch (created=True)
#     → Seed default (all-disabled) feature flags for the new branch.
#       Called automatically during branch provisioning so that the
#       flag panel for a new branch is never empty.
#
# To register these handlers, VsConfigConfig.ready() must import this module.
# That is already wired up in apps.py.

import logging

from django.db.models.signals import post_save
from django.dispatch import receiver

logger = logging.getLogger(__name__)


@receiver(post_save, sender="vs_schools.Branch")
def seed_default_flags_on_branch_create(sender, instance, created, **kwargs):
    """
    Automatically seeds all-disabled BranchFeatureFlag records for every flag
    in FLAG_REGISTRY when a new Branch is created.

    This ensures the flag panel in the Vision Admin Console is never empty for
    a newly created branch — all flags appear as 'Disabled' from day one.

    No ConfigurationChangeLog entries are written for seeding. Default state
    is not a meaningful configuration change.
    """
    if not created:
        return

    try:
        from .services.flags import FlagService
        FlagService.seed_default_flags(instance, actor=None)
    except Exception:
        logger.exception(
            "vs_config: failed to seed default flags for branch %s.",
            instance.pk,
        )

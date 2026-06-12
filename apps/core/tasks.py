"""Core housekeeping tasks."""
from __future__ import annotations

from datetime import timedelta

from celery import shared_task
from django.utils import timezone


@shared_task
def prune_background_jobs_task(days: int = 90) -> dict:
    """Delete finished BackgroundJob rows older than *days*.

    Keeps the queue table bounded. Only terminal rows are pruned — anything
    QUEUED/RUNNING stays regardless of age (a stuck row is a signal, not noise).
    """
    from core.models import BackgroundJob

    cutoff = timezone.now() - timedelta(days=days)
    deleted, _ = BackgroundJob.objects.filter(
        status__in=[BackgroundJob.Status.SUCCEEDED, BackgroundJob.Status.FAILED],
        created_at__lt=cutoff,
    ).delete()
    return {"pruned": deleted, "older_than_days": days}

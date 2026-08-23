from celery import shared_task


@shared_task(name="vs_tickets.prune_guide_analytics")
def prune_guide_analytics_task():
    """Nightly: enforce the disposable guide-analytics retention boundary."""

    from .analytics import RETENTION_DAYS, prune

    return {
        "deleted": prune(),
        "older_than_days": RETENTION_DAYS,
    }

"""Celery tasks for schools. The work lives in ``services``; these are the entry points."""
from celery import shared_task

from .services.creation import TASK_NAME, run_school_creation


@shared_task(name=TASK_NAME, bind=True)
def create_school_task(self, *, payload, actor_id):
    """Create a school from a validated console payload. See ``services.creation``."""
    return run_school_creation(payload=payload, actor_id=actor_id, job_id=self.request.id)

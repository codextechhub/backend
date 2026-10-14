"""Helpers for tests and demo data that exercise posting and paying a payroll run.

A payroll run is posted only once its approval route has approved it. A test about
posting, statutory figures or payment is not about that route, so it hands the
run over in the state a finished approval leaves it: :func:`approve_for_posting`.
Tests about the route itself run the real thing (submit, then an approver
deciding), and the demo-data commands call this where they build a run directly.
"""
from __future__ import annotations

from .constants import DocumentStatus


def approve_for_posting(run):
    """Leave ``run`` approved, as a finished approval does, without going through the engine. Returns it."""
    run.status = DocumentStatus.APPROVED
    run.save(update_fields=["status", "updated_at"])
    return run

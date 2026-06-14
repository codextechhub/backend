"""Shared abstract base for vs_health models."""
from __future__ import annotations

from django.db import models
from django.utils import timezone


class TimeStampedModel(models.Model):
    """Immutable created / auto-updated timestamps (matches core conventions)."""

    created_at = models.DateTimeField(default=timezone.now, editable=False)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        abstract = True

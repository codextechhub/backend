"""The stored history of the records a school may be asked about later.

A :class:`RecordVersion` is a copy of one row's tracked fields as it stood
from ``recorded_at`` until the next version of the same row. Reading a record
"as at" a date is therefore one indexed lookup: the latest version recorded
before the end of that day.

Versions are only ever added. A deleted row gains a final version marked
``is_deleted``, so a list rebuilt for an earlier date still carries the row
that existed then.

**A record's history starts at its earliest version, and never earlier.** The
first version of a row that existed before tracking began is a baseline
(``is_baseline``), written by ``manage.py baseline_record_history`` on the day
tracking reached it. No version is ever reconstructed from anything else: a
value the platform did not record is not a value it can vouch for, so a date
before a record's first version has no answer rather than a guessed one.
"""
from __future__ import annotations

from django.conf import settings
from django.contrib.postgres.indexes import GinIndex
from django.db import models
from django.utils import timezone


class RecordVersion(models.Model):
    """One row's tracked fields, as they stood from ``recorded_at`` onwards.

    ``record_type`` is the tracked model's label (``vs_students.student``) and
    ``record_id`` its primary key as text. ``owners`` names the records whose
    pages list this row, as ``"<record_type>:<id>"`` strings: a guardian link
    belongs on the student's page and on the guardian's, so it names both, and
    either page finds it with one ``owners__contains`` lookup.

    ``changed`` lists the fields that differ from the version before, so a
    reader can tell what an edit touched without comparing two copies. A
    baseline and a first version list every field.

    ``actor`` is the person the audit trail attributes the change to, which is
    the real actor during a proxy session. It is empty for system writes.
    """

    tenant = models.ForeignKey(
        "vs_tenants.Tenant", on_delete=models.CASCADE, null=True, blank=True,
        related_name="+",
    )
    record_type = models.CharField(max_length=64)
    record_id = models.CharField(max_length=64)
    owners = models.JSONField(default=list, blank=True)
    recorded_at = models.DateTimeField(default=timezone.now)
    is_baseline = models.BooleanField(default=False)
    is_deleted = models.BooleanField(default=False)
    data = models.JSONField(default=dict)
    changed = models.JSONField(default=list, blank=True)
    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True,
        blank=True, related_name="+",
    )

    class Meta:
        indexes = [
            models.Index(
                fields=["record_type", "record_id", "recorded_at"],
                name="vs_hist_record_time",
            ),
            GinIndex(fields=["owners"], name="vs_hist_owners_gin"),
        ]
        ordering = ["record_type", "record_id", "recorded_at", "id"]

    def __str__(self) -> str:
        state = "deleted" if self.is_deleted else "version"
        return f"{self.record_type}:{self.record_id} {state} @ {self.recorded_at:%Y-%m-%d %H:%M}"


class TrackingStart(models.Model):
    """When history began for one tracked model, as a whole.

    A single record's history starts at its own first version, but a list has
    no single record to ask. A person's field exceptions on 3 March are the
    rows that existed that day, and an empty answer is only true once tracking
    had reached the model: before that, an exception lifted on 2 March leaves
    no version at all. So a list read as at a day checks this start as well as
    its owner's, and refuses a day before it.

    Written by ``baseline_record_history`` the first time it meets a model:
    the model's earliest version when it has any, otherwise the moment the
    command ran.
    """

    record_type = models.CharField(max_length=64, unique=True)
    started_at = models.DateTimeField()

    def __str__(self) -> str:
        return f"{self.record_type} tracked since {self.started_at:%Y-%m-%d %H:%M}"

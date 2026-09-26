"""Give every tracked row that has no history a first version, dated now.

    python manage.py baseline_record_history
    python manage.py baseline_record_history --check

A row created while its model is tracked gets its first version from the save
that created it. A row that already existed when tracking reached its model has
none, and this command writes one: a baseline, dated when the command ran,
which is the day that record's history starts. Nothing earlier is inferred.

It also stamps each tracked model's :class:`~vs_history.models.TrackingStart`
the first time it meets the model, which is what a list read as at a day checks.

Idempotent: a row that already has any version is left alone, so the deploy
runs it every time and it does work only for rows it has not seen. ``--check``
counts those rows and writes nothing, and exits non-zero when there are any.
"""
from __future__ import annotations

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from django.db.models import Min

from vs_history.models import RecordVersion, TrackingStart
from vs_history.registry import all_specs, snapshot

BATCH = 1000


class Command(BaseCommand):
    help = "Write a baseline version for every tracked row that has no history."

    def add_arguments(self, parser):
        parser.add_argument(
            "--check", action="store_true",
            help="Count rows without a history and write nothing.",
        )

    def handle(self, *args, check=False, **options):
        missing_total = 0
        for spec in all_specs():
            if not check:
                self._stamp_start(spec)
            seen = set(
                RecordVersion.objects.filter(record_type=spec.record_type)
                .values_list("record_id", flat=True).distinct()
            )
            rows = spec.model._base_manager.all().order_by("pk")
            if spec.m2m:
                rows = rows.prefetch_related(*spec.m2m)
            missing = [row for row in rows.iterator(chunk_size=BATCH) if str(row.pk) not in seen]
            missing_total += len(missing)
            if check or not missing:
                self.stdout.write(f"{spec.record_type}: {len(missing)} without history")
                continue
            now = timezone.now()
            versions = []
            for row in missing:
                data = snapshot(spec, row)
                versions.append(RecordVersion(
                    tenant_id=getattr(row, "tenant_id", None),
                    record_type=spec.record_type,
                    record_id=str(row.pk),
                    owners=spec.owner_keys(row),
                    recorded_at=now,
                    is_baseline=True,
                    data=data,
                    changed=sorted(data),
                ))
            with transaction.atomic():
                RecordVersion.objects.bulk_create(versions, batch_size=BATCH)
            self.stdout.write(f"{spec.record_type}: baselined {len(missing)}")
        if check and missing_total:
            raise CommandError(f"{missing_total} tracked rows have no history.")

    @staticmethod
    def _stamp_start(spec):
        """Record when tracking reached *spec*, once."""
        if TrackingStart.objects.filter(record_type=spec.record_type).exists():
            return
        first = RecordVersion.objects.filter(
            record_type=spec.record_type,
        ).aggregate(first=Min("recorded_at"))["first"]
        TrackingStart.objects.get_or_create(
            record_type=spec.record_type,
            defaults={"started_at": first or timezone.now()},
        )

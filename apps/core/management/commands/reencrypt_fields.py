"""Re-encrypt every encrypted column with the first key in FIELD_ENCRYPTION_KEYS.

The rotation step of :mod:`core.encryption`: put the new key first, keep the
old one after it, deploy, run this, then remove the old key. Every value is
read (any listed key opens it) and written back (the first key seals it),
and so is every stored history version of a tracked model.

    python manage.py reencrypt_fields
"""
from __future__ import annotations

from django.apps import apps
from django.core.management.base import BaseCommand
from django.db import transaction

from core.encryption import decrypt, encrypt
from core.fields import EncryptedTextField


class Command(BaseCommand):
    help = "Re-encrypt every encrypted column, and its history, with the first key."

    def handle(self, *args, **options):
        from vs_history.models import RecordVersion
        from vs_history.registry import spec_for

        for model in apps.get_models():
            fields = [f.name for f in model._meta.concrete_fields if isinstance(f, EncryptedTextField)]
            if not fields:
                continue
            rows = 0
            for row in model._base_manager.values("pk", *fields).iterator():
                values = {name: row[name] for name in fields if row[name] not in (None, "")}
                if values:
                    with transaction.atomic():
                        model._base_manager.filter(pk=row["pk"]).update(**values)
                    rows += 1
            versions = 0
            spec = spec_for(model)
            if spec is not None and spec.sealed_names:
                for version in RecordVersion.objects.filter(record_type=spec.record_type).iterator():
                    data = dict(version.data)
                    for name in spec.sealed_names:
                        if data.get(name) not in (None, ""):
                            data[name] = encrypt(decrypt(data[name]))
                    if data != version.data:
                        RecordVersion.objects.filter(pk=version.pk).update(data=data)
                        versions += 1
            self.stdout.write(f"{model._meta.label}: {rows} rows, {versions} history versions.")

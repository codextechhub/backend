"""Prove the Field Access switches say what today's permission keys say.

The conversion claims that after it runs, every person reads and writes
exactly the fields they read and write now. This command is how that claim is
checked on real data rather than on a fixture::

    python manage.py verify_field_access_conversion --snapshot
    # run the conversion migration
    python manage.py verify_field_access_conversion --compare

``--snapshot`` walks every active user and records, per converted field, what
the old keys give them: the field level guards in
:mod:`vs_rbac.field_conversion`, plus, for a write no field level guard
decides, the endpoint key that actually decides it today
(:data:`vs_rbac.field_conversion.WRITE_REACHED_BY`). ``--compare`` recomputes
the same users through :func:`vs_rbac.field_evaluator.get_field_access` and
fails, listing every difference under the user it belongs to.

Differences the conversion is allowed to produce are declared, one by one,
in :data:`vs_rbac.field_conversion.ACCEPTED_DIFFERENCES`: the deliberate
tightening on a bank account number and the others that share its cause. They
are printed as accepted rather than dropped, so a run always says what it
forgave. Anything else fails the command.

Two things the snapshot deliberately leaves out, because the map leaves them
out too: the owner rule that lets a staff member read their own payroll bank
details whatever their roles say, and the export gate, which decides taking
data out of the building in a file rather than seeing it on a screen.
"""
from __future__ import annotations

import json
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

from vs_rbac.field_conversion import (
    ACCEPTED_DIFFERENCES,
    READ,
    WRITE,
    WRITE_REACHED_BY,
    converted_field_keys,
    gate_index,
)

#: Where a snapshot lands when the caller names no file.
DEFAULT_PATH = Path("field_access_conversion_snapshot.json")

#: The snapshot layout, so a file written by an older build is refused rather
#: than compared against a shape it does not have.
FORMAT_VERSION = 2


def _accepted_index() -> dict:
    return {
        (entry.field_key, entry.access): entry
        for entry in ACCEPTED_DIFFERENCES
    }


def old_access(user, field, gates, held_keys) -> tuple[bool, bool]:
    """What today's keys give *user* on *field*.

    Reading is open where no key guards it. Writing is decided by the field
    level guard where one exists, and otherwise by the endpoint the value is
    written through, which is what makes "can overwrite a number it cannot
    see" a real state rather than a theoretical one.
    """
    read_key = gates.get(READ)
    write_key = gates.get(WRITE)
    can_read = read_key in held_keys if read_key else True
    if write_key:
        can_write = write_key in held_keys
    else:
        reached_by = WRITE_REACHED_BY.get(field.key)
        can_write = (
            any(key in held_keys for key in reached_by) if reached_by else True
        )
    return can_read, bool(field.writable and can_write)


class Command(BaseCommand):
    help = "Record, then check, that Field Access switches preserve today's access."

    def add_arguments(self, parser):
        parser.add_argument(
            "--snapshot", action="store_true",
            help="Record what today's permission keys give every active user.",
        )
        parser.add_argument(
            "--compare", action="store_true",
            help="Recompute through the field evaluator and fail on any unlisted difference.",
        )
        parser.add_argument(
            "--path", default=str(DEFAULT_PATH),
            help=f"Snapshot file to write or read (default: {DEFAULT_PATH}).",
        )
        parser.add_argument(
            "--tenant", default=None,
            help="Limit the walk to one tenant slug.",
        )

    def handle(self, *args, **options):
        if options["snapshot"] == options["compare"]:
            raise CommandError("Choose exactly one of --snapshot and --compare.")
        path = Path(options["path"])
        if options["snapshot"]:
            return self._snapshot(path, options["tenant"])
        return self._compare(path)

    # ------------------------------------------------------------------
    # Walking the users
    # ------------------------------------------------------------------

    def _users(self, tenant_slug):
        from vs_user.models import User

        users = User.objects.filter(status="ACTIVE", tenant__isnull=False)
        if tenant_slug:
            users = users.filter(tenant__slug=tenant_slug)
        return users.select_related("tenant").order_by("pk")

    def _fields(self):
        """The converted fields that exist and are active, by key."""
        from vs_rbac.models import FieldDefinition

        rows = FieldDefinition.objects.filter(
            key__in=converted_field_keys(), is_active=True,
        )
        return {row.key: row for row in rows}

    def _snapshot(self, path: Path, tenant_slug):
        from vs_rbac.evaluator import ANY_BRANCH, _active_role_ids, get_effective_permissions
        from vs_rbac.permissions import is_vision_super_admin

        fields = self._fields()
        if not fields:
            raise CommandError(
                "No converted field is registered. Run sync_field_registry first."
            )
        gates = gate_index()
        users = {}
        for user in self._users(tenant_slug).iterator():
            held = get_effective_permissions(user, tenant=user.tenant)
            bypass = is_vision_super_admin(user)
            roleless = not _active_role_ids(user, user.tenant, ANY_BRANCH).exists()
            states = {}
            for key, field in fields.items():
                if bypass:
                    states[key] = [True, bool(field.writable)]
                    continue
                read, write = old_access(user, field, gates.get(key, {}), held)
                states[key] = [read, write]
            users[str(user.pk)] = {"roleless": roleless, "fields": states}

        payload = {
            "format": FORMAT_VERSION,
            "tenant": tenant_slug,
            "fields": sorted(fields),
            "users": users,
        }
        path.write_text(json.dumps(payload, indent=1, sort_keys=True))
        self.stdout.write(self.style.SUCCESS(
            f"Recorded {len(users)} user(s) over {len(fields)} field(s) in {path}."
        ))

    def _compare(self, path: Path):
        from vs_rbac.field_evaluator import get_field_access
        from vs_user.models import User

        if not path.exists():
            raise CommandError(f"No snapshot at {path}. Run --snapshot first.")
        payload = json.loads(path.read_text())
        if payload.get("format") != FORMAT_VERSION:
            raise CommandError(
                f"Snapshot at {path} is format {payload.get('format')}, "
                f"and this command reads format {FORMAT_VERSION}. Take a new one."
            )

        accepted_index = _accepted_index()
        blocking: list[str] = []
        accepted: list[str] = []
        checked = 0
        for user_id, recorded in sorted(payload["users"].items(), key=lambda item: item[0]):
            user = User.objects.filter(pk=user_id).select_related("tenant").first()
            if user is None:
                blocking.append(f"user {user_id}: in the snapshot, gone from the database")
                continue
            checked += 1
            access = get_field_access(user, tenant=user.tenant)
            label = f"{user.email} ({user_id})"
            for field_key, (was_read, was_write) in sorted(recorded["fields"].items()):
                for access_name, before, now in (
                    (READ, was_read, access.can_read(field_key)),
                    (WRITE, was_write, access.can_write(field_key)),
                ):
                    if before == now:
                        continue
                    line = (
                        f"{label}: {field_key} {access_name} "
                        f"{'on' if before else 'off'} -> {'on' if now else 'off'}"
                    )
                    entry = accepted_index.get((field_key, access_name))
                    allowed = (
                        entry is not None
                        and entry.old == before
                        and entry.new == now
                        and (not entry.only_without_roles or recorded.get("roleless"))
                    )
                    (accepted if allowed else blocking).append(
                        f"{line}  [{entry.reason}]" if allowed else line
                    )

        for line in accepted:
            self.stdout.write(self.style.WARNING(f"  accepted  {line}"))
        if blocking:
            for line in blocking:
                self.stdout.write(self.style.ERROR(f"  CHANGED   {line}"))
            raise CommandError(
                f"{len(blocking)} access change(s) the conversion is not allowed to make, "
                f"over {checked} user(s)."
            )
        self.stdout.write(self.style.SUCCESS(
            f"{checked} user(s) read and write exactly what they did before"
            + (f", with {len(accepted)} declared difference(s)." if accepted else ".")
        ))

"""
Management command: delete_user
================================
Hard-deletes one or more users and every trace of their existence.
Intended for local testing only - never expose this as an API endpoint.

Relationship handling is fully automatic - no need to update this command
when new models are added. Django's _meta.related_objects is used to discover
all FK relationships to User at runtime:

  PROTECT  → deleted explicitly before the user, so the delete isn't blocked.
  SET_NULL → deleted explicitly so no orphaned audit/log records remain.
  CASCADE  → handled automatically by Django when the user is deleted.

Usage
-----
    python manage.py delete_user --email user@example.com
    python manage.py delete_user --email alice@x.com bob@x.com
    python manage.py delete_user --email alice@x.com bob@x.com --force
    python manage.py delete_user --email ada@gmail.com --tenant_id bright-star

An address is unique PER TENANT, not platform-wide, so one address can name
two unrelated accounts. Without --tenant_id (a tenant id or slug) this command
refuses an ambiguous address and prints the tenants it belongs to, rather than
hard-deleting whichever row came back first.

Note
----
    Render gives you a shell into your running service. Two ways:

    Option 1 - Render Dashboard Shell
    1. Go to your web service on render.com
    2. Click the Shell tab
    3. Run it directly:
    python manage.py delete_user --email user@example.com --force

    Option 2 - Render CLI
    render ssh <your-service-name>
    # then inside:
    python manage.py delete_user --email user@example.com --force

    The command will run against whatever DATABASE_URL / DB env vars Render has configured for that service - so it hits the live Render DB.
"""

from django.core.management.base import BaseCommand, CommandError
from django.db import models, transaction

from vs_tenants.references import find_tenant
from vs_user.email_normalization import normalize_email


def _resolve_tenant(ref):
    """Turn a ``--tenant_id`` argument into a Tenant, or None when not given."""
    if ref in (None, ""):
        return None
    tenant = find_tenant(ref)
    if tenant is None:
        raise CommandError(f"No tenant found for --tenant_id {ref!r} (id or slug).")
    return tenant


def _related_querysets(user):
    """
    Inspect all FK/O2O relationships pointing at the User model and return
    two lists of (label, queryset) pairs:

      protect_qs  - on_delete=PROTECT: must be deleted before user.delete()
      set_null_qs - on_delete=SET_NULL: orphaned records; deleted for full wipe
    """
    protect_qs = []
    set_null_qs = []

    for rel in user._meta.related_objects:
        on_delete  = rel.on_delete
        field_name = rel.field.name
        related_model = rel.related_model
        label = f"{related_model._meta.app_label}.{related_model.__name__}.{field_name}"

        qs = related_model._default_manager.filter(**{field_name: user})

        if on_delete is models.PROTECT:
            protect_qs.append((label, qs))
        elif on_delete is models.SET_NULL:
            set_null_qs.append((label, qs))

    return protect_qs, set_null_qs


class Command(BaseCommand):
    help = "Hard-delete one or more users and all traces of their work. Local testing only."

    def add_arguments(self, parser):
        parser.add_argument(
            "--email",
            nargs="+",
            required=True,
            metavar="EMAIL",
            help="One or more email addresses to delete.",
        )
        parser.add_argument(
            "--tenant_id",
            metavar="TENANT",
            help=(
                "Tenant that owns the account(s): its numeric id or its slug. "
                "An address may now be a login at more than one tenant, so it "
                "no longer identifies one account on its own."
            ),
        )
        parser.add_argument(
            "--force",
            action="store_true",
            help="Skip confirmation prompt.",
        )

    def handle(self, *args, **options):
        from django.contrib.auth import get_user_model
        User = get_user_model()

        emails = [normalize_email(e) for e in options["email"]]
        tenant = _resolve_tenant(options.get("tenant_id"))

        # Resolve all emails - collect not-found ones to report at the end.
        #
        # ``.get(email=...)`` on its own is no longer a question with one
        # answer: ada@gmail.com is a legitimate login at Bright Star AND at
        # Greenfield, so the unscoped form raises MultipleObjectsReturned, and
        # this command hard-deletes a user and every trace of their work. It
        # refuses rather than guessing, and names both tenants so the operator
        # can say which one they meant.
        users, not_found, ambiguous = [], [], []
        for email in emails:
            matches = list(
                User.objects.select_related("tenant__school_profile")
                .filter(email=email, **({"tenant": tenant} if tenant else {}))
                .order_by("tenant_id")
            )
            if not matches:
                not_found.append(email)
            elif len(matches) > 1:
                ambiguous.append((email, matches))
            else:
                users.append(matches[0])

        if ambiguous:
            lines = []
            for email, matches in ambiguous:
                where = ", ".join(
                    f"{getattr(u.tenant, 'slug', '?')} (--tenant_id {u.tenant_id})"
                    for u in matches
                )
                lines.append(f"  {email} exists at: {where}")
            raise CommandError(
                "These addresses exist at more than one tenant. Re-run with "
                "--tenant_id to say which account you mean:\n" + "\n".join(lines)
            )

        if not users:
            raise CommandError("None of the provided emails were found.")

        self.stdout.write(f"\n  {len(users)} user(s) to delete:\n")
        for u in users:
            self.stdout.write(
                f"    • {u.full_name or '-'} <{u.email}>"
                f"  [{u.tenant.kind} | {u.status}"
                f"{' | ' + s.name if (s := getattr(u.tenant, 'school_profile', None)) else ''}]\n"
            )

        if not options["force"]:
            confirm = input("\n  Delete all of the above and ALL their data? Type YES to confirm: ")
            if confirm.strip() != "YES":
                self.stdout.write(self.style.WARNING("Aborted."))
                return

        total_counts: dict[str, int] = {}

        for user in users:
            with transaction.atomic():
                self._delete_one(user, total_counts)

        self.stdout.write(self.style.SUCCESS(f"\n✅  {len(users)} user(s) deleted.\n"))
        for label, count in total_counts.items():
            if count:
                self.stdout.write(f"    {label}: {count} deleted")

        if not_found:
            self.stdout.write(self.style.WARNING(
                f"\n  ⚠️  {len(not_found)} email(s) not found (skipped):\n" +
                "\n".join(f"    • {e}" for e in not_found)
            ))
        self.stdout.write("")

    def _delete_one(self, user, counts: dict):
        protect_qs, set_null_qs = _related_querysets(user)

        # ── 1. PROTECT relations - delete first so user.delete() isn't blocked ──
        for label, qs in protect_qs:
            n, _ = qs.delete()
            counts[label] = counts.get(label, 0) + n

        # ── 2. SET_NULL relations - delete for full trace wipe ─────────────────
        for label, qs in set_null_qs:
            n, _ = qs.delete()
            counts[label] = counts.get(label, 0) + n

        # ── 3. JWT outstanding tokens ──────────────────────────────────────────
        try:
            from rest_framework_simplejwt.token_blacklist.models import OutstandingToken
            n, _ = OutstandingToken.objects.filter(user=user).delete()
            key = "simplejwt.OutstandingToken"
            counts[key] = counts.get(key, 0) + n
        except Exception as exc:
            self.stdout.write(self.style.WARNING(f"  JWT cleanup skipped for {user.email}: {exc}"))

        # ── 4. Delete the user (CASCADE handles everything else) ───────────────
        user.delete()
        self.stdout.write(f"  Deleted: {user.email}")

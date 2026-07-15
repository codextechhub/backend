"""Seed the view-only "Consultant" platform role on the codex tenant.

The role holds every active ``*.view`` permission and nothing else. The
seeder re-syncs on every run (build.sh runs it on each deploy): newly
registered view permissions are granted automatically, and any non-view
grant that crept in is stripped, so the role's read-only contract holds.
The role is locked so it can't be edited from the console — edits would be
silently undone by the next deploy anyway.
"""
from django.core.management.base import BaseCommand

from vs_rbac.models import Permission, TenantRolePermission, TenantRoleTemplate
from vs_tenants.models import Tenant

ROLE_KEY = "xvs_consultant"
ROLE_NAME = "Consultant"
ROLE_DESCRIPTION = (
    "Read-only platform role for consultants and reviewers. Holds every "
    "view permission and nothing else; kept in sync automatically on deploy."
)


class Command(BaseCommand):
    help = (
        "Seed the view-only Consultant platform role (codex tenant): grants all "
        "active *.view permissions, strips everything else. Idempotent — safe every deploy."
    )

    def handle(self, *args, **options):
        codex = Tenant.objects.filter(slug="codex", kind=Tenant.Kind.PLATFORM).first()
        if codex is None:
            self.stdout.write(self.style.WARNING(
                "  ⚠  Codex platform tenant not found — run migrations first. Skipping."
            ))
            return

        role, created = TenantRoleTemplate.objects.update_or_create(
            tenant=codex,
            key=ROLE_KEY,
            defaults={
                "name": ROLE_NAME,
                "description": ROLE_DESCRIPTION,
                "status": TenantRoleTemplate.Status.ACTIVE,
                "is_system_role": True,
                "is_locked": True,
            },
        )

        granted = 0
        for perm in Permission.objects.filter(action_id="view", is_active=True):
            _, link_created = TenantRolePermission.objects.get_or_create(
                role=role,
                permission=perm,
                defaults={"granted": True, "granted_by": None},
            )
            if link_created:
                granted += 1

        # The role's contract is view-only — anything else is stripped so a
        # manual grant can't widen it between deploys.
        stripped, _ = (
            TenantRolePermission.objects.filter(role=role)
            .exclude(permission__action_id="view")
            .delete()
        )

        total = TenantRolePermission.objects.filter(role=role).count()
        self.stdout.write(self.style.SUCCESS(
            f"  {ROLE_KEY}: {'created' if created else 'exists'}; "
            f"+{granted} new view grant(s), -{stripped} non-view grant(s), {total} total."
        ))

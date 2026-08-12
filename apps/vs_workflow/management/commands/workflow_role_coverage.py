"""Report (and optionally create) the roles that central workflow stages name.

A central template is published once, without a tenant, and names its approver
by role key. That key is resolved inside whichever tenant raises the document,
so a tenant with no such role resolves to nobody. Where the stage is set to
auto-skip, the request then sails through unapproved - silently. This command
makes that gap visible before it costs anything.

    python manage.py workflow_role_coverage            # report
    python manage.py workflow_role_coverage --create   # create missing roles

``--create`` only creates the role, inactive of nobody's doing: it assigns no
one. An unassigned role still resolves to nobody, so the report keeps listing
it under "no holders" until an admin assigns someone. That is deliberate -
this command should never invent approval authority.
"""
from django.core.management.base import BaseCommand
from django.db import transaction


class Command(BaseCommand):
    help = "Report tenants missing a role that a central workflow stage names."

    def add_arguments(self, parser):
        parser.add_argument(
            "--create", action="store_true",
            help="Create the missing roles (without assigning anyone to them).",
        )

    @transaction.atomic
    def handle(self, *args, **options):
        from vs_rbac.models import TenantRoleTemplate, TenantUserRoleAssignment
        from vs_tenants.models import Tenant
        from vs_workflow.constants import ApproverSource
        from vs_workflow.models import WorkflowStage, WorkflowStageDynamicRule

        create = options["create"]

        # Every role key a central (tenant-less) template depends on.
        central_stages = WorkflowStage.objects.filter(
            template__tenant__isnull=True, retired_at__isnull=True,
        ).select_related("template")

        wanted = {}   # role_key -> list of "template:stage" using it
        for stage in central_stages:
            if stage.approver_source == ApproverSource.ROLE and stage.approver_role_key:
                wanted.setdefault(stage.approver_role_key, []).append(
                    f"{stage.template.code}:{stage.code}")
        for rule in WorkflowStageDynamicRule.objects.filter(
                stage__template__tenant__isnull=True,
                stage__retired_at__isnull=True).select_related("stage__template"):
            if rule.role_key:
                wanted.setdefault(rule.role_key, []).append(
                    f"{rule.stage.template.code}:{rule.stage.code} rule {rule.order + 1}")

        if not wanted:
            self.stdout.write(self.style.SUCCESS(
                "  No central workflow stages name a role. Nothing to check.\n"))
            return

        self.stdout.write(self.style.MIGRATE_HEADING(
            f"\n  {len(wanted)} role key(s) referenced by central workflow stages:\n"))
        for key, used_by in sorted(wanted.items()):
            self.stdout.write(f"    {key}  ({', '.join(used_by)})")

        tenants = list(Tenant.objects.filter(status=Tenant.Status.ACTIVE))
        missing_total = unassigned_total = created_total = 0

        self.stdout.write(self.style.MIGRATE_HEADING(
            f"\n  Checking {len(tenants)} active tenant(s)...\n"))

        for tenant in tenants:
            problems = []
            for key in sorted(wanted):
                role = TenantRoleTemplate.objects.filter(
                    tenant=tenant, key=key,
                    status=TenantRoleTemplate.Status.ACTIVE).first()
                if role is None:
                    if create:
                        TenantRoleTemplate.objects.create(
                            tenant=tenant, key=key,
                            name=key.replace("-", " ").replace("_", " ").title(),
                            description="Created by workflow_role_coverage for a "
                                        "central approval stage. Assign someone to it.",
                            status=TenantRoleTemplate.Status.ACTIVE,
                        )
                        created_total += 1
                        problems.append(f"{key}: created, nobody assigned yet")
                        unassigned_total += 1
                    else:
                        missing_total += 1
                        problems.append(f"{key}: no such role")
                    continue

                holders = TenantUserRoleAssignment.objects.filter(
                    tenant=tenant, role=role,
                    assignment_status=TenantUserRoleAssignment.AssignmentStatus.ACTIVE,
                    user__is_active=True,
                ).count()
                if holders == 0:
                    unassigned_total += 1
                    problems.append(f"{key}: role exists but nobody holds it")

            if problems:
                self.stdout.write(self.style.WARNING(f"  {tenant.slug}"))
                for p in problems:
                    self.stdout.write(f"      - {p}")

        self.stdout.write("")
        if created_total:
            self.stdout.write(self.style.SUCCESS(f"  Created {created_total} role(s)."))
        if missing_total:
            self.stdout.write(self.style.ERROR(
                f"  {missing_total} missing role(s). Re-run with --create, or point "
                "those stages somewhere else."))
        if unassigned_total:
            self.stdout.write(self.style.ERROR(
                f"  {unassigned_total} role(s) with no holders. Approvals routed to "
                "them resolve to nobody, and a stage set to auto-skip will pass the "
                "request through unapproved."))
        if not missing_total and not unassigned_total:
            self.stdout.write(self.style.SUCCESS(
                "  Every tenant can staff every central approval stage.\n"))

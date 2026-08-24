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

Two failure modes, not one
    **No holders** is the dangerous one: the stage resolves to nobody, and an
    auto-skip stage passes the request through unapproved.

    **Exactly one holder** is the quiet one, and this command used to report it
    as fine. A requester may never approve their own submission, so a role held
    by one person has *zero* eligible approvers for anything that person raises.
    The document parks with nobody able to release it. That is the safe failure
    rather than a silent pass, but a tenant can sit in it indefinitely believing
    it is staffed, because "the role has a holder" is true and useless.

    The distinction matters most in small tenants, where the sole holder is
    often also the person raising everything.
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
        missing_total = unassigned_total = created_total = sole_total = 0

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

                held_by = TenantUserRoleAssignment.objects.filter(
                    tenant=tenant, role=role,
                    assignment_status=TenantUserRoleAssignment.AssignmentStatus.ACTIVE,
                    user__is_active=True,
                ).select_related("user")
                # People, not grants. One person may hold the role at two
                # branches, and counting rows would report two holders where
                # there is one - turning the hard block below into silence.
                holders = held_by.values("user_id").distinct().count()
                if holders == 0:
                    unassigned_total += 1
                    problems.append(f"{key}: role exists but nobody holds it")
                elif holders == 1:
                    # Not a warning about redundancy: it is a hard block. Exclude
                    # the sole holder as requester and the eligible set is empty,
                    # so anything they raise parks and no one can release it.
                    sole_total += 1
                    who = held_by.first().user.email
                    problems.append(
                        f"{key}: only {who} holds it, so anything they raise has "
                        f"nobody to approve it")

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
        if sole_total:
            self.stdout.write(self.style.WARNING(
                f"  {sole_total} role(s) held by exactly one person. Those roles are "
                "staffed on paper, but a requester cannot approve their own "
                "submission, so anything the sole holder raises parks with nobody "
                "able to release it. Add a second holder."))
        if not missing_total and not unassigned_total and not sole_total:
            self.stdout.write(self.style.SUCCESS(
                "  Every tenant can staff every central approval stage, with a "
                "second pair of eyes available whoever raises the document.\n"))

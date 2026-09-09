"""Narrow school-wide role grants to the branch their holder is posted to.

A one-off correction. The Add staff form used to treat "not chosen" and "across
the whole school" as the same value, and the whole school was the first option
in the list, so anybody registered without an administrator noticing the field
below the role picker was granted access to every branch's records. The form
now follows the posting; this repairs the rows written before it did.

**What it will not touch**, because each of these has no branch to narrow to or
was chosen deliberately:

- a grant already pinned to a branch;
- a grant whose holder has no staff record at all - the school administrator's
  own account is one of these, and narrowing it would lock them out of their
  own school;
- a grant whose holder is posted across the whole school - a registrar belongs
  to the school, and there is no one branch that is the right answer for her;
- a grant that is revoked, or whose role is not active.

Dry by default. Nothing is written without ``--apply``, and every change is
printed either way, so the run that writes says exactly what the rehearsal did.
"""
from __future__ import annotations

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from vs_audit.models import AuditActionType, AuditModuleKey
from vs_rbac.audit import record_rbac_audit
from vs_rbac.models import TenantUserRoleAssignment
from vs_tenants.models import Tenant

from ...models import StaffProfile


class Command(BaseCommand):
    help = "Pin school-wide role grants to the branch their holder is posted to."

    def add_arguments(self, parser):
        parser.add_argument(
            "--school",
            required=True,
            help="Slug of the school to repair. Required: this changes who can "
                 "see what, so it is never run across every tenant by accident.",
        )
        parser.add_argument(
            "--apply",
            action="store_true",
            help="Write the changes. Without it the command only reports.",
        )

    def handle(self, *args, **options):
        slug = options["school"]
        apply_it = options["apply"]

        tenant = Tenant.objects.filter(slug=slug).first()
        if tenant is None:
            raise CommandError(f"No school with slug {slug!r}.")

        # The posting lives on the staff record, so a grant is only narrowable
        # when its holder has one AND it names a single branch.
        posting = {
            profile.user_id: profile
            for profile in StaffProfile.objects.filter(tenant=tenant)
            .select_related("branch", "user")
        }

        grants = (
            TenantUserRoleAssignment.objects.filter(
                tenant=tenant,
                branch__isnull=True,
                assignment_status=TenantUserRoleAssignment.AssignmentStatus.ACTIVE,
                role__status="ACTIVE",
            )
            .select_related("role", "user")
            .order_by("user__email", "role__name")
        )

        changes, skipped = [], []
        for grant in grants:
            profile = posting.get(grant.user_id)
            if profile is None:
                skipped.append((grant, "no staff record, so no posting to follow"))
                continue
            if profile.branch_id is None:
                skipped.append((grant, "posted across the whole school"))
                continue
            # A holder who already has the same role pinned to that branch would
            # end up with two identical grants. Reported rather than merged: one
            # of them is somebody's deliberate act and this command should not
            # guess which.
            clash = TenantUserRoleAssignment.objects.filter(
                tenant=tenant,
                user_id=grant.user_id,
                role_id=grant.role_id,
                branch_id=profile.branch_id,
                assignment_status=TenantUserRoleAssignment.AssignmentStatus.ACTIVE,
            ).exists()
            if clash:
                skipped.append((grant, f"already holds this role at {profile.branch}"))
                continue
            changes.append((grant, profile.branch))

        self.stdout.write(
            self.style.MIGRATE_HEADING(
                f"\n{tenant.name}: {len(changes)} to narrow, {len(skipped)} left alone\n"
            )
        )
        for grant, branch in changes:
            role = grant.role.name or grant.role.key
            self.stdout.write(
                f"  narrow   {grant.user.email:42} {role:22} whole school -> {branch.name}"
            )
        for grant, why in skipped:
            role = grant.role.name or grant.role.key
            self.stdout.write(
                self.style.WARNING(f"  keep     {grant.user.email:42} {role:22} {why}")
            )

        if not apply_it:
            self.stdout.write(
                self.style.NOTICE(
                    "\nNothing written. Run again with --apply to make it so.\n"
                )
            )
            return

        with transaction.atomic():
            for grant, branch in changes:
                grant.branch = branch
                grant.save(update_fields=["branch", "updated_at"])
                # The post_save signal speaks for assignment and revocation and
                # says nothing about a reach change, so this is written by hand.
                # Somebody will ask later why a teacher stopped seeing the other
                # branch, and the answer has to be somewhere.
                record_rbac_audit(
                    module_key=AuditModuleKey.RBAC,
                    action_type=AuditActionType.ROLE_CHANGED,
                    entity_type="User",
                    entity_id=str(grant.user_id),
                    entity_label=grant.user.email,
                    summary=(
                        f"Reach of '{grant.role.name or grant.role.key}' narrowed "
                        f"from the whole school to {branch.name}"
                    ),
                    diff_data={"branch": {"before": None, "after": branch.name}},
                    metadata={
                        "assignment_id": str(grant.pk),
                        "tenant_id": str(tenant.pk),
                        "role_id": str(grant.role_id),
                        "source": "narrow_role_reach",
                    },
                )

        self.stdout.write(
            self.style.SUCCESS(f"\n{len(changes)} grants narrowed.\n")
        )

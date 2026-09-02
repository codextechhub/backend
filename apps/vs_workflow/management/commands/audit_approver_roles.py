"""List every tenant role sitting on a key the approval engine resolves.

``workflow_role_coverage`` answers "which stages have nobody to approve them".
This answers the other half: **who is currently able to approve, and is that
because the platform provisioned them or because somebody typed the name in.**

The distinction exists because a tenant role's key is slugified from the name its
creator typed. A role named "Payout Approver" produces the key
``payout-approver``, which is the key the seeded payout ladder resolves - so
before ``is_system_role`` gated the resolver, creating that role and assigning it
was enough to put somebody on the approver list for every payout the school
raised, holding no payments permission at all.

The resolver now requires the flag, so an unflagged row confers nothing. That
makes it safe, and invisible - which is why this command exists. An unflagged row
is one of two things and they need different answers:

* **a coverage gap somebody filled honestly** - the seeds never gave this tenant
  the role, an administrator created it so approvals could run, and people have
  been approving through it. Re-run provisioning for that tenant (or set the flag)
  or its ladder now parks;
* **the thing the change exists to stop.** Look at who created it and who holds
  it, then remove it.

Nothing here writes. Deciding which of the two a row is takes a person.

    manage.py audit_approver_roles              every tenant
    manage.py audit_approver_roles --tenant corona
    manage.py audit_approver_roles --unflagged-only
"""
from __future__ import annotations

from django.core.management.base import BaseCommand
from django.db.models import Count


class Command(BaseCommand):
    help = "List tenant roles holding a workflow approver key, flagged or not."

    def add_arguments(self, parser):
        parser.add_argument(
            "--tenant", default="", help="Limit to one tenant slug.",
        )
        parser.add_argument(
            "--unflagged-only", action="store_true",
            help="Show only roles that confer nothing (the ones needing a decision).",
        )

    def handle(self, *args, **options):
        from vs_rbac.models import TenantRoleTemplate, TenantUserRoleAssignment
        from vs_workflow.services.roles import reserved_role_keys

        keys = reserved_role_keys()
        if not keys:
            self.stdout.write(
                "No workflow stage names an approver role key, so nothing is reserved."
            )
            return

        roles = (
            TenantRoleTemplate.objects
            .filter(key__in=sorted(keys))
            .select_related("tenant", "created_by")
            .order_by("tenant__slug", "key")
        )
        if options["tenant"]:
            roles = roles.filter(tenant__slug=options["tenant"].strip().lower())
        if options["unflagged_only"]:
            roles = roles.filter(is_system_role=False)

        roles = list(roles)
        if not roles:
            self.stdout.write("No tenant role sits on a reserved approver key.")
            return

        # One query for the holder counts rather than one per role.
        holders: dict[int, int] = {}
        for role_id, count in (
            TenantUserRoleAssignment.objects
            .filter(
                role__in=roles,
                assignment_status=TenantUserRoleAssignment.AssignmentStatus.ACTIVE,
            )
            .values_list("role_id")
            .annotate(n=Count("id"))
        ):
            holders[role_id] = count

        unflagged = 0
        self.stdout.write(
            f"{len(keys)} reserved approver key(s); {len(roles)} tenant role(s) on them.\n"
        )
        for role in roles:
            held = holders.get(role.pk, 0)
            if role.is_system_role:
                mark = self.style.SUCCESS("provisioned")
                note = ""
            else:
                unflagged += 1
                mark = self.style.WARNING("NOT PROVISIONED")
                creator = getattr(role.created_by, "email", None)
                note = (
                    # No creator means provisioning made it before the flag
                    # existed, so it is legitimate and the backfill migration
                    # simply has not run against this database yet.
                    "  no creator recorded - run migrate to backfill"
                    if creator is None
                    else f"  created via the API by {creator} - REVIEW"
                )
            self.stdout.write(
                f"  {role.tenant.slug:<20} {role.key:<38} {mark}  "
                f"{held} holder(s){note}"
            )

        if unflagged:
            self.stdout.write(
                self.style.WARNING(
                    f"\n{unflagged} role(s) sit on an approver key without being "
                    f"provisioned. Their holders cannot approve. Decide per row: "
                    f"re-run provisioning if the ladder genuinely needs it, or "
                    f"remove the role."
                )
            )
        else:
            self.stdout.write("\nEvery role on a reserved key was provisioned.")

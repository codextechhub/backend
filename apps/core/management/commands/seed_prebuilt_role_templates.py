from django.core.exceptions import ValidationError
from django.core.management.base import BaseCommand
from django.db import transaction

from vs_rbac.models import Permission, PrebuiltRolePermission, PrebuiltRoleTemplate


# Keys a template claims by prefix rather than by name.
#
# Resolved against the Permission table on every run, so a template that owns a
# whole module keeps owning it as the module grows. Writing the keys out instead
# would mean a finance permission added next quarter silently missing from the
# role that is supposed to hold everything, and nobody finding out until a
# bursar cannot do the new thing.
#
# Platform-scoped keys are skipped: PrebuiltRolePermission refuses them, and
# rightly, because a default here is copied into every school that adopts the
# template and a platform key would be a fleet-wide grant.
PERMISSION_PREFIXES = {
    "finance_admin": ["finance."],
    "procurement_admin": ["procurement."],
}


# Templates that no longer exist, and what happened to them. Applied before the
# upsert below so a re-seed cannot leave both the old and the new row standing.
RETIRED_ROLES = [
    # Bursar carried no permissions and split the money work in two along a line
    # no school actually staffs: one person raises the bills and another closes
    # the books. Finance Admin is the single finance role now.
    {"key": "bursar", "action": "delete"},
    # Finance Manager IS Finance Admin, renamed rather than replaced so that a
    # school which already adopted it keeps the role it has - a tenant's copy is
    # an independent row, but the library should not carry two names for one job.
    {"key": "finance_manager", "action": "rename", "to": "finance_admin"},
]


PREBUILT_ROLES = [
    {
        "key": "school_admin",
        "name": "School Admin",
        "scope": "institution",
        "tier": "A",
        "description": "Primary administrator for a single school. Provisioned automatically when a school is onboarded; manages branches, staff, and school-wide settings.",
    },
    {
        "key": "branch_admin",
        "name": "Branch Admin",
        "scope": "branch",
        "tier": "A",
        "description": "Administrative manager of a single branch.",
    },
    {
        "key": "finance_admin",
        "name": "Finance Admin",
        "scope": "institution",
        "tier": "A",
        "description": (
            "Runs the whole of the school's money, across every branch: sets what a "
            "term costs, raises the bills, takes payment, chases what is late, pays "
            "staff and suppliers, posts to the ledger and closes the books. "
            "This template carries EVERY finance permission, restricted ones "
            "included - posting journals, voiding a payroll run, writing off an "
            "invoice and creating a supplier payment all arrive together. It is "
            "meant for the one person who genuinely does all of it at a small "
            "school. Where a second pair of eyes is wanted on adjustments or "
            "payouts, assign the approver roles to somebody else and do not give "
            "this one out twice."
        ),
    },
    {
        "key": "procurement_admin",
        "name": "Procurement Admin",
        "scope": "institution",
        # Tier B, unlike Finance Admin: every school runs money, not every school
        # runs a purchasing function.
        "tier": "B",
        "description": (
            "Runs the whole of the school's buying, across every branch: raises "
            "requisitions and purchase orders, books in deliveries, records "
            "supplier invoices and pays them, keeps the vendor list and the "
            "catalogue, runs sourcing and holds the stock. "
            "This template carries EVERY procurement permission, restricted ones "
            "included - approving a requisition, posting a goods receipt and "
            "creating a supplier payment all arrive together. Where a second "
            "pair of eyes is wanted on approvals, assign the approver roles to "
            "somebody else and do not give this one out twice."
        ),
    },
    {
        "key": "teacher",
        "name": "Teacher",
        "scope": "branch",
        "tier": "B",
        "description": "Teaching staff member scoped to a branch. Sensible default role for STAFF-type users invited as teachers.",
    },
]


class Command(BaseCommand):
    help = (
        "Seed the PrebuiltRoleTemplate library, retire the templates that no "
        "longer exist, and attach the defaults of any template that owns a "
        "module by prefix (see PERMISSION_PREFIXES)."
    )

    def add_arguments(self, parser):
        parser.add_argument("--dry-run", action="store_true", help="Print actions without writing.")
        parser.add_argument("--reset", action="store_true", help="Delete all prebuilt roles and re-seed. Dev only.")

    def handle(self, *args, **options):
        dry_run = options["dry_run"]
        reset = options["reset"]

        if reset:
            if dry_run:
                self.stdout.write(self.style.WARNING("--reset ignored in --dry-run mode."))
            else:
                deleted_perms, _ = PrebuiltRolePermission.objects.all().delete()
                deleted_roles, _ = PrebuiltRoleTemplate.objects.all().delete()
                self.stdout.write(self.style.WARNING(f"Reset: deleted {deleted_roles} roles, {deleted_perms} permission links."))

        self._retire(dry_run)
        created, updated = self._upsert(dry_run)
        attached, refused = self._attach_defaults(dry_run)

        self.stdout.write(self.style.SUCCESS(
            f"\nDone. created={created} updated={updated} defaults_attached={attached}"
        ))
        for line in refused:
            self.stdout.write(self.style.WARNING(f"  skipped: {line}"))

    def _retire(self, dry_run):
        """Apply RETIRED_ROLES, so a re-seed never leaves two rows for one job.

        A rename edits the key in place rather than creating the new row and
        deleting the old one. The row carries nothing a school depends on - a
        tenant's copy is an independent record with its own key - but the
        library's own history reads better when the row that WAS Finance Manager
        is the row that IS Finance Admin.
        """
        for entry in RETIRED_ROLES:
            row = PrebuiltRoleTemplate.objects.filter(key=entry["key"]).first()
            if row is None:
                continue

            if entry["action"] == "delete":
                held = PrebuiltRolePermission.objects.filter(prebuilt_role=row).count()
                if dry_run:
                    self.stdout.write(f"  [dry-run] Would delete template {entry['key']} ({held} defaults)")
                    continue
                row.delete()
                self.stdout.write(self.style.WARNING(f"  Retired: {entry['key']} deleted ({held} defaults went with it)"))
                continue

            target = entry["to"]
            if PrebuiltRoleTemplate.objects.filter(key=target).exists():
                # The rename already happened on an earlier run; the old row is
                # a leftover rather than the one to carry forward.
                if dry_run:
                    self.stdout.write(f"  [dry-run] Would delete leftover {entry['key']} ({target} already exists)")
                    continue
                row.delete()
                self.stdout.write(self.style.WARNING(f"  Retired: leftover {entry['key']} deleted, {target} already present"))
                continue

            if dry_run:
                self.stdout.write(f"  [dry-run] Would rename template {entry['key']} -> {target}")
                continue
            row.key = target
            row.save(update_fields=["key"])
            self.stdout.write(self.style.WARNING(f"  Renamed: {entry['key']} -> {target}"))

    def _upsert(self, dry_run):
        created = updated = 0
        for data in PREBUILT_ROLES:
            key = data["key"]
            if dry_run:
                self.stdout.write(f"  [dry-run] Would upsert PrebuiltRoleTemplate key={key}")
                continue

            _, was_created = PrebuiltRoleTemplate.objects.update_or_create(
                key=key,
                defaults={
                    "name": data["name"],
                    "description": data.get("description", ""),
                    "scope": data["scope"],
                    "tier": data["tier"],
                    "is_active": True,
                },
            )
            created += was_created
            updated += not was_created
            self.stdout.write(f"  {'Created' if was_created else 'Updated'}: {key}")
        return created, updated

    def _attach_defaults(self, dry_run):
        """Give each prefix-owning template every key under its prefixes.

        Additive: a key already attached is left alone, and a key that is no
        longer under any prefix is NOT removed, because a default that a school
        has already adopted lives in that school's own role and taking it out of
        the library would not take it back anyway.

        A key the model refuses is reported rather than swallowed. Two finance
        keys are platform-scoped (`finance.currency.create`, `finance.fxrate.create`)
        and belong to nobody's school; a silent skip would leave somebody
        wondering later why "all of finance" was two short.
        """
        attached = 0
        refused = []

        for key, prefixes in PERMISSION_PREFIXES.items():
            template = PrebuiltRoleTemplate.objects.filter(key=key).first()
            if template is None:
                refused.append(f"{key}: no such template")
                continue

            wanted = Permission.objects.none()
            for prefix in prefixes:
                wanted = wanted | Permission.objects.filter(key__startswith=prefix)
            wanted = wanted.order_by("key")

            held = set(
                PrebuiltRolePermission.objects
                .filter(prebuilt_role=template)
                .values_list("permission_id", flat=True)
            )
            missing = [p for p in wanted if p.key not in held]

            if dry_run:
                self.stdout.write(
                    f"  [dry-run] Would attach {len(missing)} defaults to {key} "
                    f"({len(held)} already held)"
                )
                continue

            # Counted per template. Sharing the running totals across templates
            # made procurement's line report finance's two refusals as its own.
            added = 0
            turned_away = 0
            for permission in missing:
                try:
                    with transaction.atomic():
                        PrebuiltRolePermission.objects.create(
                            prebuilt_role=template, permission=permission,
                        )
                    added += 1
                except ValidationError as error:
                    turned_away += 1
                    refused.append(f"{key}: {permission.key} ({error.messages[0]})")

            attached += added
            self.stdout.write(
                f"  Defaults on {key}: {added} attached, "
                f"{len(held)} already held, {turned_away} refused"
            )

        return attached, refused

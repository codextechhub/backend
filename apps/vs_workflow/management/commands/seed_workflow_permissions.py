"""Seed vs_workflow permission keys and the roles that hold them (idempotent).

Run order:
    python manage.py seed_actions                 # adds submit, cancel, reverse
    python manage.py create_superuser             # ensures xvs_super_admin + xvs_platform_admin
    python manage.py seed_prebuilt_role_templates # ensures the school role library
    python manage.py seed_workflow_permissions

Safe to re-run: every write is idempotent.

**The school defaults live here, in the library, not in a migration.** A school
reaches the workflow module through its own roles, and which role holds which
key is a decision about the product. A migration that writes it straight into
every tenant's roles answers only for the tenants standing at the moment it
runs: the library is what every school created afterwards is built from, so a
decision recorded only in the migration is a decision new schools never get.

That is not hypothetical. It happened to exactly these keys. The grants were
backfilled into tenant roles and never added to the library, so schools created
after the backfill got a School Admin holding no ``workflow.*`` key at all - and
because the workflow write keys are restricted, such a school
could not grant them to itself either. It could raise the change request and
nobody in the building could approve it.

So this command owns both halves and keeps them in step: it writes the library
defaults, and a key the library gains here reaches the school roles that already
exist, once, at that moment (:mod:`vs_rbac.library_growth`). A key a school
later took off one of its roles is not put back on the next run.
"""
from django.core.management.base import BaseCommand
from django.db import transaction


# (resource_name, resource_description, [(action_name, description, is_restricted), ...])
WORKFLOW_RESOURCES = [
    (
        "template",
        "Workflow template definitions",
        [
            ("update", "Update workflow templates",                            True),
            ("publish", "Publish workflow templates",                          True),
            ("view",   "View workflow templates (read-only)",                 False),
        ],
    ),
    (
        "instance",
        "Workflow approval instances",
        [
            # No "submit" key. Submission is not a workflow-engine surface: each
            # module gates its own submit endpoint with its own key
            # (finance.creditnote.submit, procurement.requisition.submit), because
            # only the owning module can say which documents a caller may address.
            ("view",   "View workflow instances and their stage history",     False),
            ("cancel", "Cancel a workflow instance (admin override)",         True),
        ],
    ),
    (
        "group",
        "Workflow approver groups",
        [
            ("create", "Create approver groups", True),
            ("update", "Edit approver groups and their members", True),
            ("delete", "Delete approver groups", True),
            ("view",   "View approver groups and their resolved members",             False),
        ],
    ),
    (
        "action",
        "Workflow stage actions",
        [
            ("reverse", "Reverse a recorded approver action (admin override)", True),
        ],
    ),
    (
        "approvers",
        "Who approves requests already under way",
        [
            ("assign", "Change who approves requests, and set delegations for other people", True),
        ],
    ),
]

PLATFORM_ROLE_IDS = ["xvs_super_admin", "xvs_platform_admin"]
_PLATFORM_ROLE_NAMES = {"xvs_super_admin": "XVS Super Admin", "xvs_platform_admin": "XVS Platform Admin"}


#: Which school role holds which workflow key, by prebuilt library key.
#:
#: The split follows who is answerable for what. School Admin gets the write
#: keys because deciding who signs off the school's money is the head's call
#: and there is nobody else in a school to make it. That includes moving a
#: request that is already waiting to somebody else when its approver is away. Finance Admin and
#: Procurement Admin read the rules governing their own documents, since seeing
#: which ladder governs a purchase order is part of running procurement and
#: changing it is not. Branch Admin sees instances only: a branch admin answers
#: questions about documents in flight and configures nothing.
#:
#: The group and template write keys are restricted. That bars them
#: from a permission group, not from a role - the restriction exists so a key
#: cannot be handed out by attaching a group, and these are direct role grants.
SCHOOL_ROLE_DEFAULTS = {
    "school_admin": [
        "workflow.template.view", "workflow.template.update",
        "workflow.template.publish",
        "workflow.group.view", "workflow.group.create",
        "workflow.group.update", "workflow.group.delete",
        "workflow.instance.view", "workflow.instance.cancel",
        "workflow.approvers.assign",
    ],
    "finance_admin": [
        "workflow.template.view", "workflow.instance.view", "workflow.group.view",
    ],
    "procurement_admin": [
        "workflow.template.view", "workflow.instance.view", "workflow.group.view",
    ],
    "branch_admin": ["workflow.instance.view"],
}

class Command(BaseCommand):
    help = "Seed vs_workflow permission keys and grant them to platform admin roles."

    @transaction.atomic
    def handle(self, *args, **options):
        from vs_rbac.models import (
            Permission,
            PermissionAction,
            PermissionModule,
            PermissionResource,
            TenantRolePermission,
            TenantRoleTemplate,
            PermissionScope,
        )
        from vs_tenants.models import Tenant

        self.stdout.write(self.style.MIGRATE_HEADING("\n  Seeding workflow permissions...\n"))

        module, created = PermissionModule.objects.get_or_create(
            name="workflow",
            defaults={"description": "Approval workflow engine permissions", "is_active": True},
        )
        if created:
            self.stdout.write("  Created module: workflow")

        created_count = 0
        all_perms = []

        for resource_name, resource_desc, actions in WORKFLOW_RESOURCES:
            resource, _ = PermissionResource.objects.get_or_create(
                module=module,
                name=resource_name,
                defaults={"description": resource_desc, "is_active": True},
            )

            for action_name, description, is_restricted in actions:
                action = PermissionAction.objects.filter(name=action_name).first()
                if not action:
                    self.stdout.write(self.style.WARNING(
                        f"  ⚠  Action '{action_name}' not found - run seed_actions first."
                    ))
                    continue

                # Key is auto-generated by Permission.save() as module.resource.action
                expected_key = f"workflow.{resource_name}.{action_name}"

                perm = Permission.objects.filter(key=expected_key).first()
                if perm:
                    self.stdout.write(f"    {expected_key} (exists)")
                else:
                    perm = Permission(
                        module=module,
                        resource=resource,
                        action=action,
                        description=description,
                        is_restricted=is_restricted,
                        sensitivity_level="SENSITIVE" if is_restricted else "NORMAL",
                        is_active=True,
                        scope=PermissionScope.TENANT,
                    )
                    perm.save()
                    created_count += 1
                    self.stdout.write(f"  + {perm.key}")

                all_perms.append(perm)

        # ── Grant to platform roles (codex tenant) ─────────────────────────────

        self.stdout.write(self.style.MIGRATE_HEADING("\n  Granting to platform roles...\n"))

        codex = Tenant.objects.filter(slug="codex", kind=Tenant.Kind.PLATFORM).first()
        if codex is None:
            self.stdout.write(self.style.WARNING(
                "  ⚠  Codex platform tenant not found - run migrations first; grants skipped."
            ))
        else:
            for role_id in PLATFORM_ROLE_IDS:
                role, _ = TenantRoleTemplate.objects.get_or_create(
                    tenant=codex,
                    key=role_id,
                    defaults={
                        "name": _PLATFORM_ROLE_NAMES.get(role_id, role_id),
                        "status": "ACTIVE",
                        "is_system_role": True,
                        "is_locked": True,
                    },
                )
                granted = 0
                for perm in all_perms:
                    _, link_created = TenantRolePermission.objects.get_or_create(
                        role=role,
                        permission=perm,
                        defaults={"granted": True, "granted_by": None},
                    )
                    if link_created:
                        granted += 1

                self.stdout.write(
                    self.style.SUCCESS(f"  {role_id}: granted {granted} new permission(s).")
                    if granted else
                    f"  {role_id}: all permissions already assigned."
                )

        # ── School role library, and the schools already built from it ────────

        self._seed_school_library()

        self.stdout.write(self.style.SUCCESS(
            f"\n  Done. {created_count} new permission(s) created, "
            f"{len(all_perms)} total workflow keys registered.\n"
        ))

    def _seed_school_library(self):
        """Attach the school defaults to the prebuilt role library, and grow its copies.

        The library is what every school created from here on is provisioned
        with. A key a library role gains here also reaches the copies every
        school already holds, per-branch copies and the hyphenated spelling
        included (:func:`vs_rbac.library_growth.tenant_copies`), at that moment
        and never again (:func:`vs_rbac.library_growth.attach_defaults`). A
        school that shaped its own roles keeps what it has: a key it took off
        stays off and a key it refused stays refused.

        A key the registry does not have is skipped rather than created: the
        block above owns the vocabulary.
        """
        from vs_rbac.library_growth import attach_defaults
        from vs_rbac.models import PrebuiltRoleTemplate

        self.stdout.write(self.style.MIGRATE_HEADING(
            "\n  Attaching school defaults to the prebuilt role library...\n"
        ))

        for prebuilt_key, keys in SCHOOL_ROLE_DEFAULTS.items():
            prebuilt = PrebuiltRoleTemplate.objects.filter(key=prebuilt_key).first()
            if prebuilt is None:
                self.stdout.write(self.style.WARNING(
                    f"  ⚠  Prebuilt role '{prebuilt_key}' not found - run "
                    "seed_prebuilt_role_templates first; its defaults were skipped."
                ))
                continue

            attached, grown = attach_defaults(
                prebuilt, keys, branch_copies=True, tenant_kind="SCHOOL",
            )
            self.stdout.write(
                self.style.SUCCESS(
                    f"  {prebuilt_key}: attached {len(attached)} new default(s), "
                    f"{grown} grant(s) added to schools' copies."
                )
                if attached else
                f"  {prebuilt_key}: defaults already attached."
            )

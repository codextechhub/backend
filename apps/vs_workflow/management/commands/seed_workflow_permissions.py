"""Seed vs_workflow permission keys and the roles that hold them (idempotent).

Run order:
    python manage.py seed_actions                 # adds submit, cancel, reverse
    python manage.py create_superuser             # ensures xvs_super_admin + xvs_platform_admin
    python manage.py seed_prebuilt_role_templates # ensures the school role library
    python manage.py seed_workflow_permissions

Safe to re-run - all operations use get_or_create.

**The school defaults live here, in the library, not in a migration.** A school
reaches the workflow module through its own roles, and which role holds which
key is a decision about the product. A migration that writes it straight into
every tenant's roles answers only for the tenants standing at the moment it
runs: the library is what every school created afterwards is built from, so a
decision recorded only in the migration is a decision new schools never get.

That is not hypothetical. It happened to exactly these keys. The grants were
backfilled into tenant roles and never added to the library, so schools created
after the backfill got a School Admin holding no ``workflow.*`` key at all - and
because ``template.manage`` and ``group.manage`` are restricted, such a school
could not grant them to itself either. It could raise the change request and
nobody in the building could approve it.

So this command owns both halves and keeps them in step: it writes the library
defaults, and it grants the same keys to the school roles that already exist.
"""
from django.core.management.base import BaseCommand
from django.db import transaction
from django.db.models import Q


# (resource_name, resource_description, [(action_name, description, is_restricted), ...])
WORKFLOW_RESOURCES = [
    (
        "template",
        "Workflow template definitions",
        [
            ("manage", "Create, update, and publish workflow templates",      True),
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
            ("manage", "Create, edit, and delete approver groups and their members", True),
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
]

PLATFORM_ROLE_IDS = ["xvs_super_admin", "xvs_platform_admin"]
_PLATFORM_ROLE_NAMES = {"xvs_super_admin": "XVS Super Admin", "xvs_platform_admin": "XVS Platform Admin"}


#: Which school role holds which workflow key, by prebuilt library key.
#:
#: The split follows who is answerable for what. School Admin gets the manage
#: keys, because deciding who signs off the school's money is the head's call
#: and there is nobody else in a school to make it. Finance Admin and
#: Procurement Admin read the rules governing their own documents, since seeing
#: which ladder governs a purchase order is part of running procurement and
#: changing it is not. Branch Admin sees instances only: a branch admin answers
#: questions about documents in flight and configures nothing.
#:
#: ``group.manage`` and ``template.manage`` are restricted keys. That bars them
#: from a permission group, not from a role - the restriction exists so a key
#: cannot be handed out by attaching a group, and these are direct role grants.
SCHOOL_ROLE_DEFAULTS = {
    "school_admin": [
        "workflow.template.view", "workflow.template.manage",
        "workflow.group.view", "workflow.group.manage",
        "workflow.instance.view", "workflow.instance.cancel",
    ],
    "finance_admin": [
        "workflow.template.view", "workflow.instance.view", "workflow.group.view",
    ],
    "procurement_admin": [
        "workflow.template.view", "workflow.instance.view", "workflow.group.view",
    ],
    "branch_admin": ["workflow.instance.view"],
}

#: Tenant-side spellings of each library key.
#:
#: A school's copy of a library role does not always carry the library's key.
#: ``adopt_console_admin_roles`` created the finance and procurement copies with
#: hyphens, and branch-scoped roles take a per-branch suffix
#: (``branch_admin-37``) so several branches can each hold their own. Matching
#: on the library key alone would leave every one of those unsynced.
_TENANT_ROLE_PREFIXES = {
    "school_admin": ["school_admin"],
    "finance_admin": ["finance_admin", "finance-admin"],
    "procurement_admin": ["procurement_admin", "procurement-admin"],
    "branch_admin": ["branch_admin"],
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
        self._sync_school_tenant_roles()

        self.stdout.write(self.style.SUCCESS(
            f"\n  Done. {created_count} new permission(s) created, "
            f"{len(all_perms)} total workflow keys registered.\n"
        ))

    def _seed_school_library(self):
        """Attach the school defaults to the prebuilt role library.

        This is what every school created from here on is provisioned with.
        """
        from vs_rbac.models import (
            Permission, PrebuiltRolePermission, PrebuiltRoleTemplate,
        )

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

            attached = 0
            for key in keys:
                # A key the registry does not have is skipped rather than
                # created: the block above owns the vocabulary, and inventing a
                # row here would give it a description nobody wrote.
                if not Permission.objects.filter(key=key).exists():
                    continue
                _, created = PrebuiltRolePermission.objects.get_or_create(
                    prebuilt_role=prebuilt, permission_id=key,
                )
                attached += bool(created)

            self.stdout.write(
                self.style.SUCCESS(f"  {prebuilt_key}: attached {attached} new default(s).")
                if attached else
                f"  {prebuilt_key}: defaults already attached."
            )

    def _sync_school_tenant_roles(self):
        """Grant the same keys to the school roles that already exist.

        Additive only. A school that has shaped its own roles keeps what it has:
        this adds the keys the role was always meant to carry and removes
        nothing, because a school's own decision about its access is not this
        command's to overwrite.
        """
        from vs_rbac.models import (
            Permission, TenantRolePermission, TenantRoleTemplate,
        )

        self.stdout.write(self.style.MIGRATE_HEADING(
            "\n  Granting to the school roles that already exist...\n"
        ))

        for prebuilt_key, keys in SCHOOL_ROLE_DEFAULTS.items():
            known = list(
                Permission.objects.filter(key__in=keys).values_list("key", flat=True)
            )
            if not known:
                continue

            query = Q()
            for prefix in _TENANT_ROLE_PREFIXES[prebuilt_key]:
                # The bare key and its per-branch copies, and nothing whose name
                # merely begins the same way: "finance-admin-assistant" is a role
                # a school invented, not a copy of the library's.
                query |= Q(key=prefix) | Q(key__regex=rf"^{prefix}-\d+$")

            roles = TenantRoleTemplate.objects.filter(
                query, tenant__kind="SCHOOL",
            ).select_related("tenant")

            granted = 0
            touched = 0
            for role in roles:
                before = granted
                for key in known:
                    # ``granted`` defaults True on create and is left alone on a
                    # row that exists: a school that deliberately denied a key
                    # keeps its denial.
                    _, created = TenantRolePermission.objects.get_or_create(
                        role=role, permission_id=key, defaults={"granted": True},
                    )
                    granted += bool(created)
                touched += granted > before

            self.stdout.write(
                self.style.SUCCESS(
                    f"  {prebuilt_key}: {granted} new grant(s) across {touched} role(s)."
                )
                if granted else
                f"  {prebuilt_key}: every school role already holds these."
            )

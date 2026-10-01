"""Seed vs_finance permission keys and grant them to platform roles (idempotent).

Registers every ``finance.<resource>.<action>`` key enforced by the vs_finance
views into the RBAC Permission registry, tagged with a sensitivity level, and
grants them to the platform admin roles.

Run order::

    python manage.py seed_actions                 # canonical action verbs
    python manage.py create_superuser             # platform roles
    python manage.py seed_finance_permissions

Safe to re-run - all operations are idempotent.
"""
from django.core.management.base import BaseCommand
from django.db import transaction

MODULE_NAME = "finance"
MODULE_DESCRIPTION = "General ledger, receivables, banking, payroll, tax and reporting."
MODULE_LABEL = "Finance"
PLATFORM_ROLE_IDS = ["xvs_super_admin", "xvs_platform_admin"]
_PLATFORM_ROLE_NAMES = {"xvs_super_admin": "XVS Super Admin", "xvs_platform_admin": "XVS Platform Admin"}

# sensitivity → whether the permission must flow through approvals / audit
_RESTRICTED = {"SENSITIVE", "CRITICAL"}

# (resource_name, resource_label, [(action, sensitivity), ...])
# sensitivity: NORMAL (reads / master data) | SENSITIVE (state change) | CRITICAL (money / ledger-irreversible)
FINANCE_RESOURCES = [
    ("entity",       "ledger entities",        [("view", "NORMAL"), ("create", "SENSITIVE")]),
    ("settings",     "finance settings",       [("view", "NORMAL"), ("update", "SENSITIVE")]),
    ("account",      "chart-of-accounts",      [("view", "NORMAL"), ("create", "SENSITIVE"), ("update", "SENSITIVE")]),
    ("costcenter",   "cost centers",           [("view", "NORMAL"), ("create", "NORMAL")]),
    ("dimension",    "reporting dimensions",   [("view", "NORMAL"), ("create", "NORMAL")]),
    ("currency",     "currencies",             [("view", "NORMAL"), ("create", "NORMAL")]),
    ("fxrate",       "FX rates",               [("view", "NORMAL"), ("create", "NORMAL")]),
    ("taxcode",      "tax codes",              [("view", "NORMAL"), ("create", "NORMAL")]),
    # ``force_close`` closes a period, or a year, over its own checks; it is not
    # the ordinary close with a flag, so it does not ride on ``close``.
    ("period",       "accounting periods",     [("view", "NORMAL"), ("create", "SENSITIVE"),
                                                ("close", "CRITICAL"), ("force_close", "CRITICAL"),
                                                ("reopen", "CRITICAL"), ("lock", "CRITICAL")]),
    # Reopening a closed year takes its whole result back out of Retained Earnings.
    ("fiscalyear",   "fiscal years",           [("reopen", "CRITICAL")]),
    ("journal",      "journal entries",        [("view", "NORMAL"), ("post", "CRITICAL"), ("reverse", "CRITICAL"),
                                                # ``submit`` hands a draft to the approval engine.
                                                # No approver keys. Who may approve is decided by the
                                                # workflow stage - a role, a group, a dynamic rule or an
                                                # organogram position - never by a permission. A key here
                                                # would be ticked by somebody expecting it to grant
                                                # approval and would grant nothing.
                                                ("submit", "SENSITIVE")]),
    ("directentry",  "direct entries",         [("view", "NORMAL"), ("post", "CRITICAL")]),
    # email_statement sends a customer their own account position, so it is a
    # disclosure of financial data to an outside party, not a read.
    # ``import_opening`` carries in the customer bills unpaid when the books began,
    # each dated as the original, with no approval route: the key is the control.
    ("customer",     "customers / payers",     [("view", "NORMAL"), ("create", "SENSITIVE"), ("update", "SENSITIVE"),
                                                ("email_statement", "SENSITIVE"),
                                                ("import_opening", "CRITICAL")]),
    # Moving one customer's credit to another. Always approval-gated: there is no
    # post key, because approval is the only route to the ledger.
    ("credittransfer", "customer credit transfers", [("view", "NORMAL"), ("create", "SENSITIVE"),
                                                ("submit", "SENSITIVE"), ("reverse", "CRITICAL")]),
    ("feestructure", "fee structures",         [("view", "NORMAL"), ("create", "SENSITIVE"),
                                                ("edit", "SENSITIVE"), ("generate", "CRITICAL")]),
    # email on invoice/payment sends the document to the customer. Separate from
    # .view because reading a document internally and putting it in a customer's
    # inbox are different acts with different blast radius.
    ("invoice",      "customer invoices",      [("view", "NORMAL"), ("create", "SENSITIVE"),
                                                ("writeoff", "SENSITIVE"), ("reverse", "CRITICAL"),
                                                ("email", "SENSITIVE")]),
    ("payment",      "customer receipts",      [("view", "NORMAL"), ("create", "CRITICAL"),
                                                ("allocate", "SENSITIVE"), ("reverse", "CRITICAL"),
                                                ("email", "SENSITIVE")]),
    ("report",       "financial reports",      [("view", "NORMAL")]),
    ("audit",        "finance audit logs",     [("view", "SENSITIVE")]),
    # Who reads the account number itself is a Field Access switch on the role,
    # not a key here.
    ("bankaccount",  "bank accounts",          [("view", "NORMAL"), ("create", "SENSITIVE"),
                                                ("update", "SENSITIVE"), ("import", "SENSITIVE"),
                                                ("reconcile", "SENSITIVE")]),
    # Money in or out of a bank account with no customer or supplier behind it.
    ("banktransaction", "bank transactions",   [("view", "NORMAL"), ("create", "CRITICAL"),
                                                ("reverse", "CRITICAL")]),
    # Money between two of one branch's own bank accounts.
    ("banktransfer", "transfers between own accounts", [("view", "NORMAL"), ("create", "CRITICAL"),
                                                         ("reverse", "CRITICAL")]),
    # Between two branches: money lent or forwarded, a customer's open balance
    # moved, a shared cost recharged. ``request`` is the receiving branch asking;
    # ``transfer`` is the sending branch sending (routed through
    # ``finance.inter_branch_transfer``) or declining, forwarding a held receipt,
    # or moving a customer's open balance; ``confirm`` is the receiving branch
    # saying the money arrived; ``recharge`` splits a cost and keeps the tenant's
    # shared-cost rules.
    ("interbranch",  "inter-branch transfers", [("view", "NORMAL"), ("request", "SENSITIVE"),
                                                ("transfer", "CRITICAL"), ("confirm", "SENSITIVE"),
                                                ("recharge", "CRITICAL"), ("reverse", "CRITICAL")]),
    ("budget",       "budgets",                [("view", "NORMAL"), ("create", "SENSITIVE"),
                                                ("edit", "SENSITIVE"), ("approve", "SENSITIVE"),
                                                ("delete", "SENSITIVE")]),
    # ``submit`` hands a draft to the approval engine. Both types are gated above a
    # threshold by the seeded ladder, so the submit key is the ordinary route for a
    # large waiver or note and the post key only reaches the ledger below it.
    ("concession",   "concessions",            [("view", "NORMAL"), ("create", "SENSITIVE"),
                                                ("submit", "SENSITIVE"),
                                                ("post", "SENSITIVE"), ("reverse", "CRITICAL")]),
    ("creditnote",   "credit/debit notes",     [("view", "NORMAL"), ("create", "SENSITIVE"),
                                                ("submit", "SENSITIVE"),
                                                ("post", "CRITICAL"), ("allocate", "SENSITIVE"),
                                                ("reverse", "CRITICAL")]),
    ("dunning",      "dunning notices",        [("view", "NORMAL"), ("generate", "SENSITIVE"),
                                                ("send", "SENSITIVE"), ("create", "SENSITIVE"),
                                                ("update", "SENSITIVE")]),
    ("expenseclaim", "expense claims",         [("view", "NORMAL"), ("create", "NORMAL"),
                                                ("post", "SENSITIVE"), ("settle", "CRITICAL")]),
    ("fixedasset",   "fixed assets",           [("view", "NORMAL"), ("create", "SENSITIVE"),
                                                ("acquire", "SENSITIVE"), ("depreciate", "SENSITIVE"),
                                                ("dispose", "CRITICAL")]),
    ("paymentplan",  "payment plans",          [("view", "NORMAL"), ("create", "NORMAL"),
                                                ("activate", "SENSITIVE"), ("cancel", "SENSITIVE")]),
    # Reaching the payroll screens at all is what these keys decide. Whether a
    # holder sees the figures on a line, or on a salary row, is a Field Access
    # switch on the role.
    ("payrollrun",   "payroll runs",           [("view", "SENSITIVE"), ("create", "SENSITIVE"),
                                                ("post", "CRITICAL"), ("pay", "CRITICAL")]),
    # The salary roster / structures (master data behind a run) get their own resource so
    # editing them is not conflated with running payroll. Every verb is SENSITIVE - even
    # listing exposes who earns what.
    ("salary",       "employee salaries & structures", [("view", "SENSITIVE"), ("create", "SENSITIVE"),
                                                ("update", "SENSITIVE"), ("delete", "SENSITIVE")]),
    # The fund/float (master data) and the voucher (a spend document) are distinct
    # resources - mirroring how every other finance document (invoice, expenseclaim …)
    # gets its own resource - so each verb is unambiguous.
    # ``return`` counts the tin and banks the cash above a lowered float; ``close``
    # banks the whole tin and stops the fund; ``reopen`` brings a closed fund back;
    # ``reverse`` voids a return while its bank side is unmatched.
    ("pettycash",        "petty cash funds",    [("view", "NORMAL"), ("create", "SENSITIVE"),
                                                ("update", "SENSITIVE"), ("establish", "SENSITIVE"),
                                                ("replenish", "SENSITIVE"), ("return", "SENSITIVE"),
                                                ("close", "SENSITIVE"), ("reopen", "SENSITIVE"),
                                                ("reverse", "CRITICAL")]),
    ("pettycashvoucher", "petty cash vouchers", [("view", "NORMAL"), ("create", "SENSITIVE"),
                                                ("post", "SENSITIVE")]),
    ("refund",       "customer refunds",       [("view", "NORMAL"), ("create", "SENSITIVE"), ("post", "CRITICAL"),
                                                ("reverse", "CRITICAL"),
                                                # ``submit`` hands a draft to the approval engine.
                                                # No approver keys. Who may approve is decided by the
                                                # workflow stage - a role, a group, a dynamic rule or an
                                                # organogram position - never by a permission. A key here
                                                # would be ticked by somebody expecting it to grant
                                                # approval and would grant nothing.
                                                ("submit", "SENSITIVE")]),
    # Bad-debt write-offs are now a first-class approvable document (WriteOffRequest);
    # the existing finance.invoice.writeoff key still gates the invoice entry point.
    # ``reverse`` recovers a written-off debt from a later receipt: it reinstates the
    # written-off amount and books recovery income.
    ("writeoff",     "bad-debt write-offs",    [("view", "NORMAL"), ("create", "SENSITIVE"),
                                                ("post", "CRITICAL"), ("submit", "SENSITIVE"),
                                                ("reverse", "CRITICAL")]),
    # ``run`` releases deferred income due to revenue for every branch at once;
    # ``reverse`` undoes a month's releases while it is open.
    ("deferredincome", "deferred income",      [("view", "NORMAL"), ("run", "CRITICAL"),
                                                ("reverse", "CRITICAL")]),
    # A provision run sets the allowance for doubtful debts for every branch and is
    # approved like the other receivable adjustments.
    ("provision",    "doubtful-debt provisions", [("view", "NORMAL"), ("create", "SENSITIVE"),
                                                ("submit", "SENSITIVE"), ("post", "CRITICAL")]),
    # ``settle`` returns a customer's deposits as credit or sets them against their
    # bills; ``run`` takes deposits unclaimed past the limit to income.
    ("deposit",      "customer deposits",      [("view", "NORMAL"), ("settle", "CRITICAL"),
                                                ("run", "CRITICAL")]),
    ("tax",          "tax filings",            [("view", "NORMAL"), ("file", "SENSITIVE"),
                                                ("pay", "CRITICAL"), ("create", "SENSITIVE"),
                                                ("update", "SENSITIVE")]),
    # The national payroll data every tenant's payroll is priced on: each tax
    # year's PAYE table, the states PAYE is remitted to and the pension fund
    # administrators. It is law and the regulator's register, maintained by
    # CodeX for everybody, so both keys are platform-scoped and the views also
    # require platform staff. Reading needs no key of its own.
    ("statutory",    "national payroll tax data", [("create", "CRITICAL"), ("update", "CRITICAL")]),
]


class Command(BaseCommand):
    help = "Seed vs_finance permission keys and grant them to platform admin roles."

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

        self.stdout.write(self.style.MIGRATE_HEADING(f"\n  Seeding {MODULE_NAME} permissions...\n"))

        # ── Defensively ensure every action verb the spec needs exists ────────
        # seed_actions owns the canonical descriptions and normally runs first;
        # get_or_create never overwrites an existing row, so this is just a
        # safety net for standalone invocation.
        needed_actions = {a for _, _, acts in FINANCE_RESOURCES for a, _ in acts}
        for name in sorted(needed_actions):
            _, created = PermissionAction.objects.get_or_create(
                name=name,
                defaults={
                    "description": f"Auto-registered action verb '{name}'.",
                    "is_active": True,
                },
            )
            if created:
                self.stdout.write(
                    f"  + action '{name}' (auto-registered - run seed_actions for full description)"
                )

        # ── Module bucket ─────────────────────────────────────────────────────
        from vs_rbac.models import sentence_label

        module, created = PermissionModule.objects.get_or_create(
            name=MODULE_NAME,
            defaults={"description": MODULE_DESCRIPTION, "is_active": True},
        )
        self.stdout.write(f"  module '{MODULE_NAME}' " + ("created" if created else "exists"))
        # Fill a blank label only, so an administrator's wording stands.
        if not module.label:
            PermissionModule.objects.filter(pk=module.pk, label="").update(label=MODULE_LABEL)

        # ── Resources + permission keys ───────────────────────────────────────
        created_perms = 0
        all_perms = []
        for resource_name, resource_label, actions in FINANCE_RESOURCES:
            resource, _ = PermissionResource.objects.get_or_create(
                module=module,
                name=resource_name,
                defaults={
                    "description": f"{resource_label.capitalize()} ({MODULE_NAME}).",
                    "is_active": True,
                },
            )
            if not resource.label:
                PermissionResource.objects.filter(pk=resource.pk, label="").update(
                    label=sentence_label(resource_label),
                )
            for action_name, sensitivity in actions:
                action = PermissionAction.objects.get(name=action_name)
                expected_key = f"{MODULE_NAME}.{resource_name}.{action_name}"
                verb = action_name.replace("_", " ")

                perm = Permission.objects.filter(key=expected_key).first()
                if perm is None:
                    perm = Permission(
                        module=module,
                        resource=resource,
                        action=action,
                        description=f"{verb.capitalize()} {resource_label}.",
                        sensitivity_level=sensitivity,
                        is_restricted=sensitivity in _RESTRICTED,
                        is_active=True,
                        # Currencies and FX rates are global reference data -
                        # both views say "**global** reference data (no entity)"
                        # in their own docstrings, and both POST straight into a
                        # table with no tenant column and no platform guard. So
                        # CREATING one is platform-only. Reading stays tenant-
                        # holdable: a school's finance module needs the list.
                        #
                        # A set of books is the same shape of decision. CodeX
                        # gives a school its books when the school is created,
                        # and a school keeps one set: it never creates a second,
                        # the school app does not route the screen that would,
                        # and the settings nav hides the section for it. Reading
                        # stays tenant-holdable, because a school's finance
                        # module shows which entity it is working in.
                        scope=(
                            PermissionScope.PLATFORM
                            if expected_key in (
                                "finance.currency.create",
                                "finance.fxrate.create",
                                "finance.entity.create",
                                "finance.statutory.create",
                                "finance.statutory.update",
                            )
                            else PermissionScope.TENANT
                        ),
                    )
                    perm.save()
                    created_perms += 1
                    self.stdout.write(f"  + {perm.key}  [{sensitivity}]")
                all_perms.append(perm)

        # ── Grant every key to the platform admin roles (codex tenant) ────────
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
                    f"  {role_id}: granted {granted} new key(s)." if granted
                    else f"  {role_id}: all keys already assigned."
                )

        self.stdout.write(self.style.SUCCESS(
            f"\n  Done. {created_perms} new permission(s), {len(all_perms)} total "
            f"'{MODULE_NAME}' keys registered.\n"
        ))

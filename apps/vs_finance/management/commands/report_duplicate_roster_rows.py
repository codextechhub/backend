"""List the people with more than one active salary roster row, and install the rule once there are none.

    python manage.py report_duplicate_roster_rows                     # every tenant
    python manage.py report_duplicate_roster_rows --tenant corona     # one tenant, by slug
    python manage.py report_duplicate_roster_rows --install           # create the index when clean

Each member of staff is paid in full by one branch, so a person has at most one
active roster row (:class:`~vs_finance.models.EmployeeSalary`). A database that
held duplicates when that rule arrived has the rule's index left out, so the
deploy neither failed nor merged anybody's pay. This lists each such person
with every active row: books, branch owning it today, gross pay and when it
was added, so an administrator can move the person onto one row and take the
others off the payroll.

Reads only, unless ``--install`` is given and nobody is listed: it then
creates the index (it does nothing when the index is already there). The
application refuses a new duplicate with or without the index.
"""
from __future__ import annotations

from vs_finance.wording import counted

from collections import defaultdict

from django.core.management.base import BaseCommand, CommandError
from django.db import connection
from django.db.models import Count

from vs_finance.models import EmployeeSalary


def duplicate_rows(tenant_slug=None):
    """``{employee_id: [rows]}`` for every person with more than one active row."""
    active = EmployeeSalary.objects.filter(is_active=True, employee__isnull=False)
    if tenant_slug:
        active = active.filter(entity__tenant__slug=tenant_slug)
    people = (
        active.values("employee_id").annotate(rows=Count("id")).filter(rows__gt=1)
        .values_list("employee_id", flat=True)
    )
    out = defaultdict(list)
    for row in active.filter(employee_id__in=list(people)).select_related(
        "entity__tenant", "employee",
    ).prefetch_related("versions").order_by("employee_id", "id"):
        out[row.employee_id].append(row)
    return out


def _constraint():
    return next(
        c for c in EmployeeSalary._meta.constraints
        if c.name == "uniq_active_roster_row_per_person"
    )


def index_installed() -> bool:
    with connection.cursor() as cursor:
        cursor.execute("SELECT 1 FROM pg_indexes WHERE indexname = %s", [_constraint().name])
        return cursor.fetchone() is not None


class Command(BaseCommand):
    help = "List people with more than one active salary roster row (read-only unless --install)."

    def add_arguments(self, parser):
        parser.add_argument("--tenant", help="Tenant slug (default: every tenant).")
        parser.add_argument(
            "--install", action="store_true",
            help="Create the one-row-per-person index when nobody is listed.",
        )

    def handle(self, *args, **opts):
        from vs_config.clock import tenant_today
        from vs_tenants.models import Branch

        found = duplicate_rows(opts.get("tenant"))
        owning = {
            row.pk: row.branch_on(tenant_today(row.entity.tenant))
            for rows in found.values() for row in rows
        }
        names = dict(Branch.objects.filter(pk__in=set(owning.values())).values_list("pk", "name"))
        for rows in found.values():
            person, tenant = rows[0].employee, rows[0].entity.tenant
            self.stdout.write(f"{person.email} [{tenant.slug}]: {len(rows)} active rows")
            for row in rows:
                self.stdout.write(
                    f"  row {row.pk}: books {row.entity.code}, "
                    f"branch {names.get(owning[row.pk], 'none')}, "
                    f"gross {row.gross_amount} kobo, added {row.created_at:%Y-%m-%d}"
                )
        self.stdout.write(f"{counted(len(found), 'person', 'people')} with more than one active row.")

        installed = index_installed()
        if not opts["install"]:
            self.stdout.write(
                "The one-row-per-person index is "
                + ("installed." if installed else "not installed; resolve the rows, then rerun with --install.")
            )
            return
        if installed:
            self.stdout.write("The one-row-per-person index is already installed.")
            return
        if opts.get("tenant"):
            raise CommandError("--install checks every tenant; run it without --tenant.")
        if found:
            raise CommandError("Resolve the rows listed above before installing the index.")
        with connection.schema_editor() as editor:
            editor.add_constraint(EmployeeSalary, _constraint())
        self.stdout.write(self.style.SUCCESS("Installed the one-row-per-person index."))

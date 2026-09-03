"""Who may import what.

The import engine was built for CodeX operations - provisioning schools and
hiring CX staff in bulk - and later opened to schools so a school could load its
own data during onboarding. Nothing was added to say which datasets belong to
which side, so every caller was offered every template.

That is not a small gap. Verified against a running server on 2026-08-23, as
the school administrator of Brightfield Schools:

* Selecting **Schools Master Import** and importing one row created a whole new
  school AND a new tenant (``kind=SCHOOL status=PENDING``). A customer could
  provision tenants on the platform.
* Selecting **Branches Master Import** created a branch, a branch administrator
  and a branch-scoped role - none of which a school may create through the
  branch endpoints, which are all platform-only. The upload was a way around a
  permission the API refuses at the front door.
* Selecting **CX Users Master Import** created a user inside the ``codex``
  PLATFORM tenant with ``status=PENDING_APPROVAL``, submitted into CodeX's own
  staff-approval workflow - a request indistinguishable from a real internal
  hire. One approval away from a school holding a CodeX platform account.

The batch uploaded, validation returned zero issues, and ``start-import`` was
accepted. There was no guard at any layer to hit.

The root cause is that ``dataset_type`` carried no notion of ownership, so this
module gives it one. A dataset is platform-only or it is not, stated once, and
the three layers that can act on a dataset all ask here:

    1. the template list, so a school is never offered one;
    2. batch creation, so a crafted request naming the template is refused;
    3. the executor, so anything that reaches a row handler is refused too.

Three layers rather than one because they fail differently: the list is a
courtesy, the create is the rule, and the executor is what catches a batch built
before the rule existed, or reached by a path nobody has thought of yet.

**A new dataset type must be classified here.** ``platform_only`` fails closed
for anything it does not recognise, so a dataset added to
``DatasetTypeChoices`` and forgotten here is withheld from schools rather than
handed to them. ``test_every_dataset_type_is_classified`` fails until it is
added deliberately.
"""
from __future__ import annotations

from .models import DatasetTypeChoices

#: Datasets that act on CodeX's own records rather than a school's.
#:
#: ``schools`` provisions tenants; ``cx_users`` hires CodeX staff. Neither is a
#: school's data in any sense, and neither handler has a tenant to scope to -
#: ``import_cx_users_row`` deliberately forces the platform tenant as its
#: target, which is correct for a CodeX operator and catastrophic for anyone
#: else.
PLATFORM_ONLY_DATASETS: frozenset[str] = frozenset({
    DatasetTypeChoices.SCHOOLS,
    DatasetTypeChoices.CX_USERS,
    # Reconciliation data, loaded by whoever runs the ledger. Not something a
    # school is asked for during onboarding, and not on this checklist.
    DatasetTypeChoices.BANK_STATEMENTS,
    # Listed rather than left to the fail-closed default, because this one is a
    # DECISION and not an oversight. A branch looks like a school's own data;
    # creating one is CodeX's. See TENANT_DATASETS below for why.
    DatasetTypeChoices.BRANCHES,
})

#: Datasets a school may import for itself.
#:
#: A dataset earns a place here on three counts, and a new one has to be
#: argued the same way:
#:
#: 1. *The school already creates these by hand, through an API that is its
#:    own.* For calendar events that is ``academics.calendar.create``, a
#:    school key held by school roles, and the events endpoint accepts
#:    exactly what the file carries. For students it is
#:    ``school.students.create`` and ``POST /v1/students/``.
#: 2. *The handler creates nothing but the school's own rows.* A
#:    ``CalendarEvent`` in the uploading tenant's own year with its audience
#:    rows; a Student, a Guardian and their link inside the uploading tenant.
#:    No tenant, no account, no role, no permission grant.
#: 3. *There is a real reason to do it in bulk.* A year's calendar is thirty
#:    to sixty dated entries a school already keeps in a spreadsheet, and a
#:    school arriving on the platform brings hundreds of children it holds
#:    the same way. Typing either in one at a time is not a migration path.
#:
#: ``branches`` is deliberately absent, and it is the sharpest argument for
#: classifying datasets rather than assuming. A branch looks like a school's
#: own data, and it is, but creating one is not a school's to do: every view
#: in ``vs_schools/views/branch.py`` demands ``platform.branches.create`` or
#: ``.update``, which is PLATFORM-scoped and held by no school role, so a
#: live school administrator posting to the branch endpoint is refused
#: outright. An unclassified import engine asks none of that, and uploading
#: a branches CSV would create the branch, a branch administrator account
#: and a branch-scoped role: a school that cannot create one branch by
#: asking could create twenty by uploading a spreadsheet.
#:
#: A school still administers the branches it HAS. ``school.branches.view``
#: and ``.manage`` are its own keys; opening and editing branches is not.
#:
#: The remaining onboarding datasets, staff and parents, have no template
#: and no model to import into. When one lands, adding it here is the only
#: change needed.
TENANT_DATASETS: frozenset[str] = frozenset({
    DatasetTypeChoices.CALENDAR_EVENTS,
    DatasetTypeChoices.STUDENTS,
})


def platform_only(dataset_type: str | None) -> bool:
    """True when only a CodeX caller may import this dataset.

    Fails CLOSED. An unrecognised dataset type is treated as platform-only,
    because the failure that matters is the one where a new dataset is added and
    nobody thinks about who owns it: withholding it from schools is a bug report,
    handing it to them is what this module exists to prevent.
    """
    if not dataset_type:
        return True
    return dataset_type not in TENANT_DATASETS


def caller_is_platform(user) -> bool:
    """True when this user belongs to the CodeX platform tenant."""
    return getattr(getattr(user, "tenant", None), "kind", None) == "PLATFORM"


def may_import(user, dataset_type: str | None) -> bool:
    """The whole rule, in one place, for all three layers to ask."""
    return caller_is_platform(user) or not platform_only(dataset_type)


#: What a refused caller is told. Deliberately says nothing about what the
#: dataset does or that CodeX uses it.
REFUSAL_MESSAGE = "That import template is not available to your school."

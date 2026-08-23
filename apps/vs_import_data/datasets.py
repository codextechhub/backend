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
})

#: Datasets a school may import for itself.
#:
#: ``branches`` is a school's own campus list - the one thing on the onboarding
#: checklist that can genuinely be uploaded today. ``bank_statements`` is
#: reconciliation data belonging to the school's own ledger entity.
TENANT_DATASETS: frozenset[str] = frozenset({
    DatasetTypeChoices.BRANCHES,
    DatasetTypeChoices.BANK_STATEMENTS,
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

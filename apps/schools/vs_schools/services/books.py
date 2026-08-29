"""Giving a school its set of books at the moment the school is created.

Books used to arrive only through ``POST /v1/finance/entities/``, a platform-only
endpoint, so a school existed for as long as somebody remembered to go and
provision them. Adding books later means going back and repairing every school
created before, which is why every school now gets them at creation regardless of
whether the finance capability is entitled.

This module is the school side of that bridge, and it is deliberately thin: it
picks a free entity code for the school and asks the **Finance Abstraction
Layer** for its books. The translation from school facts (name, slug, currency)
into the tenant and entity terms :mod:`vs_finance` understands now happens inside
the FAL, which is where this module's previous docstring said it belonged.

What that buys, concretely: this module no longer knows that a school's books are
keyed by its tenant, that ``LedgerEntity.save`` falls back to the Codex platform
tenant when nobody passes one, or that a usable set of books needs a chart of
accounts and open fiscal periods as well as a row. It asks for books; the FAL
knows what books are made of, and it is the only place that has to be corrected
when finance changes its mind again.

The one-primary-per-school rule is also the FAL's now: it refuses to guess when a
tenant somehow holds two candidate sets of books, rather than quietly returning
the older one.

**Provisioning is best effort.** A school, its tenant, its first administrator,
its branches and its entitlements are worth far more than its books, and books
can be created afterwards by ``manage.py provision_school_books``. So a failure
here is caught, logged loudly enough to be found, and does not take the school
down with it.
"""
from __future__ import annotations

import logging
import re

from django.db import transaction

logger = logging.getLogger(__name__)

#: ``LedgerEntity.code`` is a globally unique, uppercase, 16-character column.
ENTITY_CODE_MAX_LENGTH = 16


def derive_entity_code(school) -> str:
    """Pick a free ``LedgerEntity.code`` for ``school``, deterministically.

    The school's slug is the readable source (``corona-secondary`` becomes
    ``CORONASECONDARY``), but the entity column is globally unique across every
    tenant on the platform and only sixteen characters, while a slug may be
    eighty. So the stem is stripped to letters and digits, truncated, and then
    numbered on collision.

    Two schools with similar names is ordinary rather than exceptional, so a
    collision must never raise: the numbered candidates are followed by one
    built from the school's own primary key, which is unique by construction.
    """
    from vs_finance.models import LedgerEntity

    stem = re.sub(r"[^A-Z0-9]", "", (school.slug or school.name or "").upper())
    stem = stem[:ENTITY_CODE_MAX_LENGTH] or "SCHOOL"

    candidates = [stem]
    candidates += [
        stem[: ENTITY_CODE_MAX_LENGTH - len(str(n))] + str(n)
        for n in range(2, 100)
    ]
    candidates.append(f"SCH{school.pk}"[:ENTITY_CODE_MAX_LENGTH])

    # One query for the whole candidate list rather than one probe per attempt.
    taken = set(
        LedgerEntity.objects
        .filter(code__in=candidates)
        .values_list("code", flat=True)
    )
    for candidate in candidates:
        if candidate not in taken:
            return candidate
    return candidates[-1]  # the unique constraint is the final guard


def provision_books_for_school(school):
    """Provision (or find) the school's set of books. Never raises.

    Returns the ``LedgerEntity`` on success and ``None`` when provisioning
    failed, in which case the failure is logged with the tenant slug so the
    school can be found and repaired with ``manage.py provision_school_books``.

    The nested ``transaction.atomic()`` is the whole point of this function.
    School creation runs inside its own atomic block, and a *database* failure
    inside that block leaves the transaction aborted: catching the exception is
    not enough, because every later statement fails too and the outer commit
    takes the school, its tenant, its admin user and its branches down with it.
    The inner block opens a savepoint, so the failure rolls back only the books
    work and the outer transaction survives to commit everything else.

    Note the qualifier. A plain Python exception raised and caught leaves the
    connection perfectly healthy, so a test that forces one would pass against a
    bare ``try``/``except`` and prove nothing. ``tests_books.py`` forces a failed
    statement instead, which is what the savepoint is actually for.
    """
    from vs_finance.models import LedgerEntity

    from schools.core.fal import get_entity_resolver

    try:
        with transaction.atomic():
            handle = get_entity_resolver().provision_entity(
                school.pk,
                code=derive_entity_code(school),
                name=school.name,
                # A school that named no currency gets the platform default,
                # which is what the finance model would have applied anyway.
                base_currency=school.currency or "NGN",
            ).unwrap()
            # The FAL returns a handle, deliberately: a DTO cannot be used to
            # reach back into finance state. The operator command and the tests
            # want the row itself, so it is read back here. This lookup is the
            # only finance model reference left in this module, and it is a read.
            return LedgerEntity.objects.get(pk=handle.entity_ref)
    except Exception:
        logger.exception(
            "Could not provision a set of books for school %s (tenant %s). "
            "The school was created without books; run "
            "'manage.py provision_school_books --school %s' to repair it.",
            school.slug, getattr(school.tenant, "slug", school.tenant_id),
            school.slug,
        )
        return None

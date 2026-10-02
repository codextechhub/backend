"""Records a tenant must keep, and the one place every deletion asks about them.

Accounting law makes a tenant keep its books for years: every journal, bill,
receipt, payslip and tax return, the evidence filed behind them, and the trail
of who did what. A record like that must survive every way the platform can
remove a row: a DELETE endpoint, a service tidying up, a management command
run against the live database, and a cascade from deleting a user, a customer,
a vendor or a branch. Asking each of those paths to remember is how a path gets
missed, so the question is asked here, once, for all of them.

How it works:

* A domain app registers each model it keeps, with a *policy*: a function that
  takes one row and answers a :class:`Hold` (kept until a date, and what it is)
  or ``None`` (free to delete). The domain decides what is kept and for how
  long; this module knows nothing about money, documents or fiscal years.
* Registering connects a ``pre_delete`` receiver for that model. Django's
  deletion collector sends ``pre_delete`` for every row it removes, whether the
  delete started at the row itself, a queryset, or a cascade several hops
  away, so a held row refuses every ORM deletion path with
  :class:`RetentionError`, and the whole delete rolls back. Connecting per
  model (never globally) keeps Django's fast delete for every other model.
* Stored file bytes (:class:`core.models.StoredFile`) are kept with their
  owner. A file whose owning row is held cannot be deleted, and the paths that
  retire a file (:func:`core.media.revoke`, a replaced upload in
  :mod:`core.binding`) close its URL but keep its bytes
  (:func:`retire_files`).

What this does not cover is SQL that bypasses the ORM. The ledger's own proof
against that is the sealed figures of each closed period (``vs_finance.seals``),
and the audit trails are refused at the database by triggers.
"""
from __future__ import annotations

import datetime
from dataclasses import dataclass

from django.db.models.signals import pre_delete


@dataclass(frozen=True)
class Hold:
    """Why a row is kept: until when, and what it is, in words a person reads."""

    until: datetime.date
    what: str


class RetentionError(Exception):
    """A delete refused because the row is inside its retention period.

    Duck-typed for ``core.exceptions.custom_exception_handler``: an API caller
    receives a 409 with ``RECORD_RETAINED`` and the date the hold ends.
    """

    error_code = "RECORD_RETAINED"
    http_status = 409

    def __init__(self, hold: Hold):
        self.hold = hold
        self.message = (
            f"{hold.what[:1].upper()}{hold.what[1:]} is a record the law requires "
            f"to be kept until {hold.until.isoformat()}, so it cannot be deleted."
        )
        self.extra = {"retained_until": hold.until.isoformat()}
        super().__init__(self.message)


#: model class -> policy(instance) -> Hold | None
_POLICIES: dict = {}


def register(model, policy) -> None:
    """Keep rows of ``model`` while ``policy(row)`` answers a :class:`Hold`.

    Idempotent per model: registering again replaces the policy, so a
    ``ready()`` that runs twice does not stack receivers.
    """
    model = model._meta.concrete_model
    _POLICIES[model] = policy
    pre_delete.connect(
        _refuse_held, sender=model, weak=False,
        dispatch_uid=f"core.retention.{model._meta.label_lower}",
    )


def registered_models() -> list:
    """The models a policy covers, for tests and diagnostics."""
    return list(_POLICIES)


def hold_on(instance):
    """The :class:`Hold` on ``instance``, or ``None`` when it may be deleted."""
    if instance is None:
        return None
    policy = _POLICIES.get(type(instance)._meta.concrete_model)
    return policy(instance) if policy is not None else None


def assert_may_delete(instance) -> None:
    """Refuse with :class:`RetentionError` when ``instance`` is held."""
    hold = hold_on(instance)
    if hold is not None:
        raise RetentionError(hold)


def _refuse_held(sender, instance, **kwargs):
    assert_may_delete(instance)


# --------------------------------------------------------------------------- #
# Stored files follow their owner                                              #
# --------------------------------------------------------------------------- #

def owner_hold(stored_file):
    """The hold on the row a stored file is evidence for, if any.

    Only owners of a registered model are loaded, so retiring a school logo or
    an import spreadsheet costs no extra query.
    """
    content_type_id = getattr(stored_file, "owner_content_type_id", None)
    if not content_type_id or not stored_file.owner_object_id:
        return None
    from django.contrib.contenttypes.models import ContentType

    model = ContentType.objects.get_for_id(content_type_id).model_class()
    if model is None or model._meta.concrete_model not in _POLICIES:
        return None
    owner = model._base_manager.filter(pk=stored_file.owner_object_id).first()
    return hold_on(owner)


def retire_files(rows) -> int:
    """Close the URL of each stored file in ``rows``; drop the bytes unless held.

    A file stops being current when its record is deleted or its upload is
    replaced. For an ordinary file the bytes go with it. For evidence behind a
    held record they stay: the row is marked revoked, so nothing serves it, and
    the bytes remain for an audit to recover. Returns the rows retired.
    """
    from django.utils import timezone

    rows = rows.filter(revoked_at__isnull=True)
    held = [
        row.pk for row in rows.only("pk", "owner_content_type", "owner_object_id")
        if owner_hold(row) is not None
    ]
    now = timezone.now()
    count = rows.exclude(pk__in=held).update(revoked_at=now, content=b"", size=0)
    if held:
        count += rows.model.objects.filter(pk__in=held).update(revoked_at=now)
    return count


def connect() -> None:
    """Keep stored files whose owner is held; called from ``CoreConfig.ready``."""
    from .models import StoredFile

    register(StoredFile, owner_hold)

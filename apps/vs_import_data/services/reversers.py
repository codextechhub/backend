"""Reversing one imported row, by the model that row actually created.

An import row records two things about what it made: ``target_model``, the name
of the model, and ``target_object_pk``, its primary key. The rollback used to
read only the second and assume the first was always ``School``.

That assumption was never true. Three datasets are importable - ``schools``,
``branches`` and ``cx_users`` - and they create ``School``, ``Branch`` and
``User`` rows respectively. All three tables use ``BigAutoField``, so their id
sequences run through the same small integers independently: id 12 names a
school, a branch and a user at the same time, in three different tables.

So rolling back a four-row branches import for Greenfield College, whose rows
created Branch ids 9 to 12, ran ``School.objects.filter(pk=...).delete()`` four
times. Greenfield's campuses stayed exactly where they were, Bright Star School
(School id 12, imported in March) was deleted along with its package setup, and
the job was stamped "rolled back successfully, 4 rows reverted".

The fix is this module. Reversal is dispatched on ``target_model`` through an
explicit registry, and a model with no reverser is REFUSED rather than guessed
at - the same fail-closed rule ``datasets.py`` applies to dataset ownership, for
the same reason: the failure that matters is the one where a new dataset is
added and nobody thinks about how to undo it.

Three further rules apply to every reverser here:

**An id alone is not an identity.** ``target_object_pk`` is a ``CharField`` on a
row that outlives the object it names; ids are also reused after a delete. Each
reverser therefore re-checks the natural key the row recorded - the school's
slug, the branch's name, the user's email - against the object the id resolves
to, and refuses when they disagree.

**Ownership is checked against the row, not the batch.** All three datasets are
platform-only (see ``datasets.py``), so ``ImportBatch.tenant`` is CodeX's own
tenant and never the school the row created. Scoping a reversal to the batch's
tenant would therefore be scoping it to the wrong tenant entirely. What the row
does carry is the school it named, and that is what a branch is checked against.

**Refusing is a normal outcome, not an error.** A reverser that will not act
raises ``RollbackRefused`` with a reason the operator can read. The caller
records it, counts it separately, and does not claim the row was reverted.
"""
from __future__ import annotations

from dataclasses import dataclass

from django.db import transaction
from django.db.models import ProtectedError

# =========================================================
# Outcomes
# =========================================================
REVERTED = "reverted"
REFUSED = "refused"
FAILED = "failed"
#: Reversed by an earlier rollback of the same job, so this run left it alone.
SKIPPED = "skipped"


class RollbackRefused(Exception):
    """This row will not be reversed, and nothing has been changed."""


@dataclass(frozen=True)
class ReversalOutcome:
    """What happened to one row, in a form the rollback record can store."""

    row_number: int
    target_model: str
    target_object_pk: str
    status: str
    message: str

    @property
    def was_reverted(self) -> bool:
        return self.status == REVERTED

    def as_dict(self) -> dict:
        return {
            "row_number": self.row_number,
            "target_model": self.target_model,
            "target_object_pk": self.target_object_pk,
            "status": self.status,
            "message": self.message,
        }


# =========================================================
# Shared helpers
# =========================================================
def _payload(row_result) -> dict:
    payload = row_result.normalized_payload
    return payload if isinstance(payload, dict) else {}


def _field(row_result, key: str) -> str:
    return (_payload(row_result).get(key) or "").strip()


def _numeric_pk(row_result) -> int:
    """The recorded primary key, or a refusal.

    Every model reversed here has an integer primary key. A non-numeric value
    is a row written before ``School.pk`` moved off the slug (B23), and there is
    no longer any way to tell which of the three models it belonged to, so it is
    refused rather than matched on a guess.
    """
    ref = str(row_result.target_object_pk or "").strip()
    if not ref.isdigit():
        raise RollbackRefused(
            f"Recorded reference '{ref}' is not a numeric primary key, so the "
            "record it names cannot be identified."
        )
    return int(ref)


def _census(instance, allowed_labels: frozenset[str]) -> list[str]:
    """Related rows pointing at ``instance`` from outside ``allowed_labels``.

    Reads every reverse relation the model has, so a model added to the platform
    later is reported rather than ignored: it will not be in the allowlist, so
    its rows make the reversal refuse until somebody classifies it deliberately.
    """
    found: list[str] = []

    for rel in instance._meta.related_objects:
        label = rel.related_model._meta.label
        if label in allowed_labels:
            continue

        accessor = rel.get_accessor_name()
        if rel.one_to_one:
            if getattr(instance, accessor, None) is not None:
                found.append(f"{label} (1)")
            continue

        count = getattr(instance, accessor).count()
        if count:
            found.append(f"{label} ({count})")

    return sorted(found)


def _contacts_of(*admin_links) -> set[int]:
    """Contact ids behind the admin links about to be deleted.

    Collected BEFORE the delete, because the links are what point at them.
    """
    return {link.contact_id for link in admin_links if link is not None}


def _sweep_orphaned_contacts(contact_ids: set[int]) -> None:
    """Delete contact cards nothing points at any more.

    ``ContactInfo`` is a stand-alone card shared by the school-admin and
    branch-admin links, and both hold it with PROTECT. Deleting a branch
    cascades its link and leaves the card behind: Greenfield's second campus is
    rolled back and 'Chidi Okonkwo, chidi@greenfield.test' stays in the table
    for ever, pointed at by nothing.

    Only cards with no remaining reference go. A contact reused as the school's
    own administrator is still in use, so it stays.
    """
    if not contact_ids:
        return

    from schools.vs_schools.models import ContactInfo

    ContactInfo.objects.filter(
        pk__in=contact_ids,
        primary_admin_for_branches__isnull=True,
        primary_admin_for_schools__isnull=True,
    ).delete()


# =========================================================
# Schools
# =========================================================
#: Relations to ``Tenant`` that creating a school writes by itself.
#:
#: Verified by running ``import_schools_row`` against an empty database: one
#: branch, the document sequence, the school profile, onboarding progress and
#: its tasks, the tenant's role templates, the audit trail of the creation, the
#: seeded workflow templates, the finance ledger entity, and the admin accounts
#: the school was created with (plus their role assignments and invitations).
#:
#: Anything else means the tenant has been USED since the import - an academic
#: session, a ticket, a login attempt, an export, a second import - and the
#: teardown refuses. The list is an allowlist rather than a denylist so a model
#: added to the platform next month refuses by default.
_TENANT_CREATION_FOOTPRINT: frozenset[str] = frozenset({
    "vs_tenants.Branch",
    "vs_tenants.TenantDocumentSequence",
    "vs_schools.School",
    "vs_onboarding.OnboardingProgress",
    "vs_onboarding.OnboardingTask",
    "vs_rbac.TenantRoleTemplate",
    "vs_rbac.TenantUserRoleAssignment",
    "vs_audit.AuditEvent",
    "vs_workflow.WorkflowTemplate",
    "vs_finance.LedgerEntity",
    "vs_user.User",
    "vs_notifications.Notification",
})

#: Relations to ``Branch`` that creating a branch writes by itself: its
#: lifecycle event and its primary-admin link.
_BRANCH_CREATION_FOOTPRINT: frozenset[str] = frozenset({
    "vs_tenants.BranchLifecycle",
    "vs_schools.BranchPrimaryAdmin",
})


def _assert_tenant_untouched(tenant) -> None:
    """Refuse unless the tenant is still exactly as the import left it.

    Rolling back a school means deleting the tenant, and a tenant is where
    everything a school owns hangs. Bright Star is imported on Monday, its
    administrator signs in on Tuesday and enrols the first term's classes, and
    on Wednesday somebody rolls the March import back to tidy up a duplicate
    row. The rollback must not take Tuesday with it.
    """
    from django.contrib.auth import get_user_model

    strangers = _census(tenant, _TENANT_CREATION_FOOTPRINT)
    if strangers:
        raise RollbackRefused(
            "The school has records the import did not create "
            f"({', '.join(strangers)}), so deleting it would destroy them."
        )

    branches = list(tenant.branches.all())
    if len(branches) > 1:
        raise RollbackRefused(
            f"The school now has {len(branches)} branches; the import created "
            "one, so the others would be destroyed."
        )

    for branch in branches:
        branch_strangers = _census(branch, _BRANCH_CREATION_FOOTPRINT)
        if branch_strangers:
            raise RollbackRefused(
                f"Branch '{branch.name}' has records the import did not create "
                f"({', '.join(branch_strangers)})."
            )

    users = get_user_model().objects.filter(tenant=tenant)
    in_use = users.exclude(last_login=None).first()
    if in_use is not None:
        raise RollbackRefused(
            f"'{in_use.email}' has signed in, so the school is in use."
        )

    activated = users.filter(status=get_user_model().Status.ACTIVE).first()
    if activated is not None:
        raise RollbackRefused(
            f"'{activated.email}' has an active account, so the school is live."
        )


def _tear_down_tenant(tenant) -> None:
    """Delete the school's tenant and everything the import created with it.

    Ordered because almost every relation to ``Tenant`` is PROTECT: the school,
    its people and its sites have to go before the tenant they hang off. A
    ``ProtectedError`` at any point means the census above missed something, so
    it becomes a refusal rather than a half-finished teardown.
    """
    from django.contrib.auth import get_user_model

    contact_ids: set[int] = set()

    try:
        school = getattr(tenant, "school_profile", None)
        if school is not None:
            contact_ids |= _contacts_of(getattr(school, "primary_admin", None))
            # Cascades the package setup.
            school.delete()

        # Cascades role assignments, invitations and permission overrides.
        get_user_model().objects.filter(tenant=tenant).delete()

        # Cascades each branch's lifecycle events and primary-admin link.
        for branch in tenant.branches.all():
            contact_ids |= _contacts_of(getattr(branch, "primary_admin", None))
        tenant.branches.all().delete()

        handled = {"vs_schools.School", "vs_user.User", "vs_tenants.Branch"}
        for rel in tenant._meta.related_objects:
            label = rel.related_model._meta.label
            if label in handled or label not in _TENANT_CREATION_FOOTPRINT:
                continue

            accessor = rel.get_accessor_name()
            if rel.one_to_one:
                related = getattr(tenant, accessor, None)
                if related is not None:
                    related.delete()
                continue

            getattr(tenant, accessor).all().delete()

        tenant.delete()
        _sweep_orphaned_contacts(contact_ids)
    except ProtectedError as exc:
        protected = {obj._meta.label for obj in exc.protected_objects}
        raise RollbackRefused(
            "The school could not be removed because other records depend on it "
            f"({', '.join(sorted(protected))})."
        ) from exc


def reverse_school(row_result, *, initiated_by=None) -> str:
    from schools.vs_schools.models import School

    pk = _numeric_pk(row_result)

    school = School.objects.select_for_update().filter(pk=pk).first()
    if school is None:
        raise RollbackRefused(
            f"No school with id {pk} exists; nothing was deleted."
        )

    slug = _field(row_result, "slug")
    name = _field(row_result, "name")
    if slug:
        if school.slug != slug:
            raise RollbackRefused(
                f"School id {pk} is '{school.slug}', not '{slug}' as the row "
                "recorded, so it is not the school this row created."
            )
    elif name:
        if school.name != name:
            raise RollbackRefused(
                f"School id {pk} is named '{school.name}', not '{name}' as the "
                "row recorded, so it is not the school this row created."
            )
    else:
        raise RollbackRefused(
            "The row recorded neither a slug nor a name, so the school it "
            "created cannot be identified."
        )

    tenant = school.tenant
    _assert_tenant_untouched(tenant)
    _tear_down_tenant(tenant)

    return f"School '{school.name}' and its tenant were deleted."


# =========================================================
# Branches
# =========================================================
def _school_named_by(row_result):
    """The school this branch row was imported into, as the row recorded it."""
    from schools.vs_schools.models import School

    slug = _field(row_result, "school_slug")
    if slug:
        school = School.objects.filter(slug=slug).first()
        if school is None:
            raise RollbackRefused(f"No school found with slug '{slug}'.")
        return school

    code = _field(row_result, "school_code")
    if code:
        school = School.objects.filter(code=code).first()
        if school is None:
            raise RollbackRefused(f"No school found with code '{code}'.")
        return school

    # The batch was school-scoped, so the row carried no school of its own.
    return getattr(row_result.job.import_batch, "school", None)


def _delete_imported_branch_admin(branch, row_result) -> None:
    """Remove the administrator account the branch import created with it.

    Refuses instead when that account has been used: an address is easy to
    mistype, and the account the row names may be one a real person now signs
    in with.
    """
    from django.contrib.auth import get_user_model
    from vs_user.email_normalization import normalize_email

    email = _field(row_result, "branch_admin_email")
    if not email:
        return

    User = get_user_model()
    admin = User.objects.filter(
        tenant=branch.tenant, email=normalize_email(email),
    ).first()
    if admin is None:
        return

    if admin.last_login is not None:
        raise RollbackRefused(
            f"Branch administrator '{admin.email}' has signed in, so the "
            "branch is in use."
        )

    if admin.status == User.Status.ACTIVE:
        raise RollbackRefused(
            f"Branch administrator '{admin.email}' has an active account, so "
            "the branch is in use."
        )

    admin.delete()


def reverse_branch(row_result, *, initiated_by=None) -> str:
    from vs_tenants.models import Branch

    pk = _numeric_pk(row_result)

    branch = Branch.all_objects.select_for_update().filter(pk=pk).first()
    if branch is None:
        raise RollbackRefused(
            f"No branch with id {pk} exists; nothing was deleted."
        )

    name = _field(row_result, "name")
    if not name:
        raise RollbackRefused(
            "The row recorded no branch name, so the branch it created cannot "
            "be identified."
        )
    if branch.name != name:
        raise RollbackRefused(
            f"Branch id {pk} is named '{branch.name}', not '{name}' as the row "
            "recorded, so it is not the branch this row created."
        )

    school = _school_named_by(row_result)
    if school is None:
        raise RollbackRefused(
            "The row does not say which school it imported into, so ownership "
            "of the branch cannot be verified."
        )
    if branch.tenant_id != school.tenant_id:
        raise RollbackRefused(
            f"Branch id {pk} belongs to a different school than the row "
            "recorded, so it is not the branch this row created."
        )

    if branch.is_main:
        raise RollbackRefused(
            f"Branch '{branch.name}' is now the school's main branch; deleting "
            "it would leave the school without one."
        )

    strangers = _census(branch, _BRANCH_CREATION_FOOTPRINT)
    if strangers:
        raise RollbackRefused(
            f"Branch '{branch.name}' has records the import did not create "
            f"({', '.join(strangers)}), so deleting it would destroy them."
        )

    _delete_imported_branch_admin(branch, row_result)

    contact_ids = _contacts_of(getattr(branch, "primary_admin", None))

    try:
        branch.delete()
        _sweep_orphaned_contacts(contact_ids)
    except ProtectedError as exc:
        protected = {obj._meta.label for obj in exc.protected_objects}
        raise RollbackRefused(
            f"Branch '{branch.name}' could not be removed because other records "
            f"depend on it ({', '.join(sorted(protected))})."
        ) from exc

    return f"Branch '{branch.name}' was deleted."


# =========================================================
# CX users
# =========================================================
#: Statuses a CX user imported by ``cx_users`` may still be reversed from. The
#: dataset creates them PENDING_APPROVAL and submits them to CodeX's staff
#: approval workflow; once approved the account is a real member of staff, and
#: unwinding that is an HR decision rather than an import rollback.
_REVERSIBLE_USER_STATUSES = frozenset({
    "DRAFT",
    "PENDING_APPROVAL",
    "REJECTED",
})


def _cancel_open_approval(user, initiated_by) -> None:
    """Terminate the approval request the import opened for this user.

    Without this the workflow instance outlives its document: the CX staff
    approval queue keeps showing "Chidi Okonkwo - platform user creation" with
    nothing behind it, and approving it raises on a user that no longer exists.
    """
    from django.contrib.contenttypes.models import ContentType
    from vs_workflow.models import WorkflowInstance
    from vs_workflow.services import actions

    content_type = ContentType.objects.get_for_model(type(user))
    instances = WorkflowInstance.objects.filter(
        document_content_type=content_type,
        document_object_id=str(user.pk),
    )

    for instance in instances:
        if instance.is_terminal:
            continue
        if initiated_by is None:
            raise RollbackRefused(
                f"'{user.email}' has an open approval request and the rollback "
                "has no initiator to cancel it on behalf of."
            )
        actions.cancel(
            instance.id,
            admin=initiated_by,
            reason="Import rolled back.",
        )


def reverse_user(row_result, *, initiated_by=None) -> str:
    from django.contrib.auth import get_user_model
    from vs_user.email_normalization import normalize_email

    User = get_user_model()
    pk = _numeric_pk(row_result)

    user = User.objects.select_for_update().filter(pk=pk).first()
    if user is None:
        raise RollbackRefused(f"No user with id {pk} exists; nothing was deleted.")

    email = _field(row_result, "email")
    if not email:
        raise RollbackRefused(
            "The row recorded no email address, so the user it created cannot "
            "be identified."
        )
    if user.email != normalize_email(email):
        raise RollbackRefused(
            f"User id {pk} is '{user.email}', not '{email}' as the row "
            "recorded, so it is not the user this row created."
        )

    if user.last_login is not None:
        raise RollbackRefused(f"'{user.email}' has signed in, so the account is in use.")

    if user.status not in _REVERSIBLE_USER_STATUSES:
        raise RollbackRefused(
            f"'{user.email}' is {user.get_status_display().lower()}; only an "
            "account still awaiting approval can be reversed by rollback."
        )

    _cancel_open_approval(user, initiated_by)

    try:
        user.delete()
    except ProtectedError as exc:
        protected = {obj._meta.label for obj in exc.protected_objects}
        raise RollbackRefused(
            f"'{user.email}' could not be removed because other records depend "
            f"on it ({', '.join(sorted(protected))})."
        ) from exc

    return f"User '{email}' was deleted."


# =========================================================
# Calendar events
# =========================================================
def reverse_calendar_event(row_result, *, initiated_by=None) -> str:
    """Delete one imported calendar entry, with the audience rows under it.

    One delete, not a teardown: audience rows CASCADE from the event by design,
    because an audience row has no meaning without the event it narrows. So
    unlike a school or a branch there is no census to take here - there is
    nothing an event owns that a school could have put there afterwards.

    What there IS, and what this refuses on, is a thing pointing the other way.
    An exam period that has had an exam timetable built against it is no longer
    just a date: deleting it would take the timetable with it. The events API
    refuses that same delete for the same reason, so a rollback that went ahead
    would be a way round a rule the API holds.
    """
    from schools.vs_calendar.models import CalendarEvent

    pk = _numeric_pk(row_result)

    event = CalendarEvent.all_objects.select_for_update().filter(pk=pk).first()
    if event is None:
        raise RollbackRefused(
            f"No calendar entry with id {pk} exists; nothing was deleted."
        )

    name = _field(row_result, "name")
    if not name:
        raise RollbackRefused(
            "The row recorded no event name, so the entry it created cannot be "
            "identified."
        )
    if event.name.casefold() != name.casefold():
        raise RollbackRefused(
            f"Calendar entry id {pk} is named '{event.name}', not '{name}' as "
            "the row recorded, so it is not the entry this row created."
        )

    # Ownership is checked against the batch here, and that is correct for this
    # dataset and wrong for the three above it. Those are platform-only, so
    # their batch belongs to CodeX and never to the school the row created.
    # This one is a school importing into itself, so the batch's tenant IS the
    # owner, and checking it is what stops a rollback reaching across schools.
    batch_tenant_id = getattr(
        getattr(getattr(row_result, "job", None), "import_batch", None),
        "tenant_id", None,
    )
    if batch_tenant_id is not None and event.tenant_id != batch_tenant_id:
        raise RollbackRefused(
            f"Calendar entry id {pk} belongs to a different school than the "
            "import that recorded it, so it was left alone."
        )

    if event.exams.exists():
        raise RollbackRefused(
            f"'{event.name}' now holds an exam timetable, so deleting it would "
            "destroy the timetable with it."
        )

    try:
        event.delete()
    except ProtectedError as exc:
        protected = {obj._meta.label for obj in exc.protected_objects}
        raise RollbackRefused(
            f"'{event.name}' could not be removed because other records depend "
            f"on it ({', '.join(sorted(protected))})."
        ) from exc

    return f"'{event.name}' was removed from the calendar."


# =========================================================
# Dispatch
# =========================================================
#: The whole dispatch table. A ``target_model`` absent from here is refused.
_REVERSERS = {
    "School": reverse_school,
    "Branch": reverse_branch,
    "User": reverse_user,
    "CalendarEvent": reverse_calendar_event,
}


def reverse_row(row_result, *, initiated_by=None) -> ReversalOutcome:
    """Reverse one imported row, or say why it was not reversed.

    Each row runs in its own savepoint, so a refusal or a failure leaves the
    rows already reversed in place and changes nothing of its own.
    """
    target_model = (row_result.target_model or "").strip()
    target_pk = str(row_result.target_object_pk or "").strip()

    def outcome(status: str, message: str) -> ReversalOutcome:
        return ReversalOutcome(
            row_number=row_result.row_number,
            target_model=target_model,
            target_object_pk=target_pk,
            status=status,
            message=message,
        )

    # Only a row that CREATED a record has a record to delete. Every handler
    # today creates or skips, but an update handler would record the model and
    # pk of a row it merely edited - and deleting that is the same class of
    # mistake as deleting the wrong model: the import did not make it.
    from ..models import ImportRowActionChoices

    if row_result.action != ImportRowActionChoices.CREATE:
        return outcome(
            REFUSED,
            f"Row {row_result.row_number} was recorded as "
            f"'{row_result.action}', not a creation, so there is nothing to "
            "delete.",
        )

    reverser = _REVERSERS.get(target_model)
    if reverser is None:
        return outcome(
            REFUSED,
            f"No rollback is defined for {target_model or 'an unrecorded model'}, "
            "so the row was left alone.",
        )

    try:
        with transaction.atomic():
            return outcome(REVERTED, reverser(row_result, initiated_by=initiated_by))
    except RollbackRefused as exc:
        return outcome(REFUSED, str(exc))
    except Exception as exc:  # noqa: BLE001 - reported, not swallowed
        return outcome(FAILED, f"{type(exc).__name__}: {exc}")

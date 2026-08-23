"""The onboarding vocabulary: states, task keys and the canonical task catalog.

The catalog is a code constant, deliberately, and not a table. Provisioning
reads it, so adding a step is a code change plus a re-provision and two
environments cannot quietly disagree about what onboarding consists of. Nothing
in the request body ever sets ``is_required`` or ``order_index``: they come from
here or they do not come at all.
"""
from __future__ import annotations

from django.db import models


class ReadinessState(models.TextChoices):
    """Where the tenant stands against the go-live gate.

    ``LIVE`` is a projection, not the source of truth. That a school is live is
    ``School.status == ACTIVE`` and ``Tenant.status == ACTIVE``; this column
    exists so the control room can render the gate without joining to either.
    """

    NOT_READY = "NOT_READY", "Not ready"
    READY = "READY", "Ready"
    PENDING_APPROVAL = "PENDING_APPROVAL", "Pending approval"
    LIVE = "LIVE", "Live"


class TaskStatus(models.TextChoices):
    NOT_STARTED = "NOT_STARTED", "Not started"
    IN_PROGRESS = "IN_PROGRESS", "In progress"
    DONE = "DONE", "Done"
    SKIPPED = "SKIPPED", "Skipped"


class GoLiveStatus(models.TextChoices):
    PENDING = "PENDING", "Pending"
    APPROVED = "APPROVED", "Approved"
    REJECTED = "REJECTED", "Rejected"
    ACTIVATED = "ACTIVATED", "Activated"
    FAILED = "FAILED", "Failed"


class TaskKey(models.TextChoices):
    # Five steps, matching the approved design one for one.
    #
    # There is no BRANCH_SETUP: vs_onboarding 0002 removed it, because every
    # school is created with its main branch and a step that is complete before
    # the school can see it is not a step.
    #
    # There is no FIRST_ADMIN or ROLE_BASELINE either. They were two rows over
    # one subject - is there a working administrator, and does the role they
    # hold grant anything - and the design presents that subject as one card.
    # Both facts are still checked; DEFAULT_ROLES is refused unless both hold.
    #
    # There is no SET_OF_BOOKS. Removed by decision (2026-08-22) to match the
    # design's five. Books are still provisioned at school creation and are
    # still best effort, so a school whose books silently failed now discovers
    # it in Finance rather than on this checklist. See migration 0003.
    DEFAULT_ROLES = "DEFAULT_ROLES", "Default roles and RBAC"
    SCHOOL_METADATA = "SCHOOL_METADATA", "School metadata"
    ACADEMIC_STRUCTURE = "ACADEMIC_STRUCTURE", "Academic structure"
    INITIAL_DATA = "INITIAL_DATA", "Initial data"
    STAFF_INVITATIONS = "STAFF_INVITATIONS", "Staff invitations"


#: The edges a task may travel. Anything absent is refused rather than ignored,
#: so a client cannot walk a task backwards into a state the control room has
#: no way to render. Asking for the status a task already holds is a separate,
#: friendlier refusal (409) and is not modelled here.
#:
#: One rule deliberately does *not* live here: a required task may not be
#: skipped at all. That is a fact about the task rather than about the edge -
#: the very same edge is legal for an optional task - so it is refused in
#: ``services.tasks.transition_task`` with its own code
#: (REQUIRED_TASK_NOT_SKIPPABLE), not by deleting SKIPPED from this table.
ALLOWED_TASK_TRANSITIONS: dict[str, frozenset[str]] = {
    TaskStatus.NOT_STARTED: frozenset({
        TaskStatus.IN_PROGRESS, TaskStatus.SKIPPED, TaskStatus.DONE,
    }),
    TaskStatus.IN_PROGRESS: frozenset({TaskStatus.DONE, TaskStatus.SKIPPED}),
    TaskStatus.SKIPPED: frozenset({TaskStatus.IN_PROGRESS, TaskStatus.DONE}),
    # Reopen. A completed task returns to work, never straight to skipped:
    # skipping something already done is not a thing a school means to say.
    TaskStatus.DONE: frozenset({TaskStatus.IN_PROGRESS}),
}


class CatalogEntry:
    """One canonical onboarding step.

    ``applies_to`` answers "does this school have this step at all?", which is a
    different question from ``is_required``. A school that cannot ever perform a
    step does not have an optional step it may ignore, it has no such step at
    all: the control room must not show a column the school will never fill in.
    No entry uses it today, and the honest reason is that the one that did (the
    branch step) turned out to apply to every school, so it stopped being a step
    rather than becoming an unconditional one. The seam stays because the
    question it answers is real and provisioning already routes through it.

    ``is_required`` means the step must be DONE before the school goes live and
    that it cannot be skipped either. There is no third setting for "required
    but deferrable": a required step the school may set aside is an optional
    step, and the catalog says so by marking it optional.
    """

    __slots__ = ("key", "title", "is_required", "order_index", "applies_to")

    def __init__(self, key, title, is_required, order_index, applies_to=None):
        self.key = key
        self.title = title
        self.is_required = is_required
        self.order_index = order_index
        self.applies_to = applies_to

    def applies(self, tenant, school) -> bool:
        if self.applies_to is None:
            return True
        return bool(self.applies_to(tenant, school))


#: The canonical catalog, in display order, and the five cards the approved
#: design draws. Titles are the design's, verbatim, because they are what the
#: school reads and the API is what supplies them.
TASK_CATALOG: tuple[CatalogEntry, ...] = (
    CatalogEntry(
        key=TaskKey.DEFAULT_ROLES,
        title="Confirm Default Roles & RBAC",
        is_required=True,
        order_index=1,
    ),
    CatalogEntry(
        key=TaskKey.SCHOOL_METADATA,
        title="School Metadata Setup",
        is_required=True,
        order_index=2,
    ),
    CatalogEntry(
        key=TaskKey.ACADEMIC_STRUCTURE,
        title="Academic Structure",
        is_required=True,
        order_index=3,
    ),
    CatalogEntry(
        key=TaskKey.INITIAL_DATA,
        title="Upload Initial Datasets",
        is_required=False,
        order_index=4,
    ),
    CatalogEntry(
        key=TaskKey.STAFF_INVITATIONS,
        title="Add Staff & Invitations",
        is_required=False,
        order_index=5,
    ),
)


# ── Permission keys ────────────────────────────────────────────────────────
# Named here so views and the seeder cannot drift apart on a typo.

PERM_PROGRESS_VIEW = "onboarding.progress.view"
PERM_PROGRESS_CREATE = "onboarding.progress.create"
PERM_TASK_UPDATE = "onboarding.task.update"
PERM_GO_LIVE_SUBMIT = "onboarding.go_live.submit"
PERM_GO_LIVE_VIEW = "onboarding.go_live.view"
PERM_GO_LIVE_APPROVE = "onboarding.go_live.approve"
PERM_GO_LIVE_REJECT = "onboarding.go_live.reject"
#: Return a suspended school to onboarding. Platform staff only: the school it
#: is used on cannot authenticate at all while it is suspended.
PERM_PROGRESS_REACTIVATE = "onboarding.progress.reactivate"


# ── Notification event keys ────────────────────────────────────────────────

EVENT_STEP_COMPLETED = "onboarding.step_completed"
EVENT_GO_LIVE_READY = "onboarding.go_live_ready"
EVENT_GO_LIVE_REVIEWED = "onboarding.go_live_reviewed"
EVENT_ACTIVATED = "onboarding.activated"
EVENT_STALE_REPORT = "onboarding.stale_report"
EVENT_EXPIRY_WARNING = "onboarding.expiry_warning"


# ── Lifecycle windows ──────────────────────────────────────────────────────
# Every window is a product decision, named here so the commands, the services
# and the tests all read the same number.

#: Go-live request history is kept for a year, with one exception the retention
#: service documents: the request that actually took a school live is never
#: deleted, whatever its age.
GO_LIVE_HISTORY_RETENTION_DAYS = 365

#: A school that has been PENDING for this long has abandoned its onboarding
#: and is suspended. Measured from ``Tenant.pending_since``, never from
#: ``Tenant.created_at``: a reinstated school starts its 90 days again.
ONBOARDING_EXPIRY_DAYS = 90

#: How long before expiry the school is warned. The warning is sent once per
#: pending spell, recorded in ``Tenant.expiry_warned_at``, and never becomes a
#: precondition for expiry: a school that could not be warned still expires on
#: time, because the alternative is a school that lives for ever because an
#: email failed.
ONBOARDING_EXPIRY_WARNING_DAYS = 14

#: How long a school may be onboarding before it appears on the operator list.
#: Comfortably short of expiry, because the point of the list is to chase a
#: school while chasing it can still help.
STALE_ONBOARDING_AFTER_DAYS = 30

#: How far back the operator list looks for schools the sweep has just expired.
#: One reporting cycle, so a school appears on exactly one list as expired.
STALE_REPORT_WINDOW_DAYS = 14

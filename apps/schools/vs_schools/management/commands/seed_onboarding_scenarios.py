"""Build a cast of schools, one per onboarding state.

A design prototype shows states one tenant cannot be in at once - ready and
rejected and live and never-provisioned - by using several mock tenants. This
command does the same thing with real rows, so every one of those states can be
opened in the running app rather than imagined.

Eight schools, each parked somewhere different:

    brightfield-lekki  Not ready, mid-progress, one step skipped
    st-monicas         Ready, go-live form open
    holy-cross         Pending approval, waiting on CodeX
    grace-fields       Rejected, with a reason, ready to resubmit
    crescent-model     Activation failed, with a failure reference
    lagoon-view        Live, control room read-only
    new-dawn           Never provisioned, no checklist at all
    riverbank          Not ready, inside the 14-day expiry warning

Every state is driven through the real services wherever a service exists, so
what you see is what a school gets. Two are fixtures and say so below.

Idempotent: re-running tops schools up and leaves states that already match.

    python manage.py seed_onboarding_scenarios
    python manage.py seed_onboarding_scenarios --only st-monicas

Run ``seed_all_permissions`` first. Never run against production.
"""
from __future__ import annotations

import uuid
from datetime import timedelta

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from ...dev.fixtures import DEFAULT_PASSWORD, build_school

#: slug -> (display name, admin first/last, runs branches)
CAST = {
    "brightfield-lekki": ("Brightfield Schools", ("Adaeze", "Okonkwo"), True),
    "st-monicas": ("St. Monica's Academy", ("Ikenna", "Nwachukwu"), False),
    "holy-cross": ("Holy Cross College", ("Ngozi", "Eze"), False),
    "grace-fields": ("Grace Fields Academy", ("Tunde", "Bakare"), False),
    "crescent-model": ("Crescent Model School", ("Halima", "Yusuf"), False),
    "lagoon-view": ("Lagoon View Academy", ("Emeka", "Obi"), True),
    "new-dawn": ("New Dawn Academy", ("Bisi", "Adeyemi"), False),
    "riverbank": ("Riverbank Schools", ("Chidi", "Nwosu"), True),
}

#: The required steps a school must close before the gate opens. Read from the
#: catalog rather than listed, so a step added there is driven here too.
def _required_keys():
    from schools.vs_onboarding.constants import TASK_CATALOG

    return [entry.key for entry in TASK_CATALOG if entry.is_required]


class Command(BaseCommand):
    help = "Build one school per onboarding state (idempotent, dev only)."

    def add_arguments(self, parser):
        parser.add_argument(
            "--only",
            help="Build a single scenario by slug, rather than the whole cast.",
        )
        parser.add_argument("--password", default=DEFAULT_PASSWORD)

    def handle(self, *args, **options):
        only = options.get("only")
        password = options["password"]

        if only and only not in CAST:
            raise CommandError(
                f"No scenario '{only}'. Known: {', '.join(sorted(CAST))}."
            )

        reviewer = self._platform_reviewer()
        if reviewer is None:
            self.stdout.write(self.style.WARNING(
                "  !  No Codex platform user found, so the approved, rejected "
                "and live scenarios cannot be reviewed by anybody. Run "
                "create_superuser first; the rest of the cast still builds."
            ))

        slugs = [only] if only else list(CAST)
        for slug in slugs:
            name, admin_name, branches = CAST[slug]
            self.stdout.write(self.style.MIGRATE_HEADING(f"\n  {name} ({slug})"))
            try:
                with transaction.atomic():
                    summary = self._build(slug, name, admin_name, branches,
                                          password, reviewer)
            except RuntimeError as error:
                raise CommandError(str(error)) from error
            self.stdout.write(f"    → {summary}")

        self.stdout.write(self.style.SUCCESS(
            f"\n  Cast ready. Every school signs in with {password}:\n"
            + "".join(
                f"    {slug + '.localhost:5199':32} admin@{slug}.example.com\n"
                for slug in slugs
            )
        ))

    # ── the states ───────────────────────────────────────────────────────────

    def _build(self, slug, name, admin_name, branches, password, reviewer):
        from schools.vs_onboarding.constants import ReadinessState, TaskStatus
        from schools.vs_onboarding.models import OnboardingProgress

        # "Never provisioned" is the one school built without a control room.
        # It has to be reachable: the state endpoint answers a specific
        # not-found for it, and the app must tell that apart from "you have not
        # started yet", which is a full checklist of Not started steps.
        provisioned = slug != "new-dawn"
        # A live school is created active; the rest are pending.
        live = slug == "lagoon-view"

        built = build_school(
            slug=slug, name=name, password=password, live=live,
            extra_branch=branches, admin_name=admin_name,
            with_onboarding=provisioned, log=self._quiet,
        )
        for note in built.notes:
            self.stdout.write(self.style.WARNING(f"    !  {note}"))

        if not provisioned:
            # Belt and braces: an earlier run may have provisioned it.
            OnboardingProgress.all_objects.filter(tenant=built.tenant).delete()
            return "no checklist - the state endpoint answers ONBOARDING_NOT_PROVISIONED"

        if slug == "brightfield-lekki":
            self._set_tasks(built, {
                "DEFAULT_ROLES": TaskStatus.DONE,
                "SCHOOL_METADATA": TaskStatus.IN_PROGRESS,
                "STAFF_INVITATIONS": TaskStatus.SKIPPED,
            })
            return "mid-progress, one step skipped, gate blocked"

        if slug == "riverbank":
            self._set_tasks(built, {"DEFAULT_ROLES": TaskStatus.DONE})
            self._age_pending(built, days=80)
            return "not ready, 10 days left in the onboarding window"

        # Terminal states are checked BEFORE anything is driven, because
        # re-running must not walk a finished school forward again. Without
        # this, lagoon-view tried to submit a second request and was refused
        # with ONBOARDING_ALREADY_LIVE, and crescent-model grew a fresh failed
        # request on every run.
        settled = self._already_settled(slug, built)
        if settled:
            return settled

        # Everything below needs the gate open first.
        self._close_required(built)

        if slug == "st-monicas":
            self._set_tasks(built, {"INITIAL_DATA": TaskStatus.SKIPPED})
            return "ready - the go-live form is open"

        request = self._submit(built)

        if slug == "holy-cross":
            return f"pending approval - request #{request.pk} awaiting review"

        if reviewer is None:
            return "ready (needs a Codex reviewer to go further)"

        if slug == "grace-fields":
            return self._reject(built, request, reviewer)
        if slug == "crescent-model":
            return self._fail(built, request)
        if slug == "lagoon-view":
            return self._activate(built, request, reviewer)

        return ReadinessState(
            OnboardingProgress.all_objects.get(tenant=built.tenant).readiness_state
        ).label

    # ── helpers ──────────────────────────────────────────────────────────────

    def _quiet(self, *_args, **_kwargs):
        """Swallow the builder's per-row chatter; the caller prints a summary."""

    def _already_settled(self, slug, built):
        """A summary when this school has already reached its scenario's end.

        The three scenarios that end somewhere final - live, failed, rejected -
        cannot be driven twice. Live refuses a second request outright, and the
        other two would quietly stack another row onto the history on every run,
        so a seeder meant to be idempotent would slowly invent a school that had
        been rejected nine times.
        """
        from schools.vs_onboarding.constants import GoLiveStatus, ReadinessState
        from schools.vs_onboarding.models import GoLiveRequest, OnboardingProgress

        progress = OnboardingProgress.all_objects.filter(tenant=built.tenant).first()
        if progress is None:
            return None

        def latest(status):
            return (
                GoLiveRequest.all_objects
                .filter(tenant=built.tenant, status=status)
                .order_by("-created_at")
                .first()
            )

        if slug == "lagoon-view" and progress.readiness_state == ReadinessState.LIVE:
            return "already live - the control room is read-only"

        if slug == "crescent-model":
            failed = latest(GoLiveStatus.FAILED)
            if failed:
                return f"activation already failed - reference {failed.failure_reference}"

        if slug == "grace-fields" and latest(GoLiveStatus.REJECTED):
            return "already rejected, with a reason, and ready to resubmit"

        return None

    def _platform_reviewer(self):
        from django.contrib.auth import get_user_model
        from vs_tenants.models import Tenant

        return (
            get_user_model().objects
            .filter(tenant__kind=Tenant.Kind.PLATFORM, is_active=True)
            .order_by("pk")
            .first()
        )

    def _set_tasks(self, built, wanted):
        """Move tasks through the real service, skipping ones already there.

        ``transition_task`` refuses a no-op with 409 and refuses DONE when the
        platform can see the thing is not done, so a scenario that cannot be
        reached honestly fails loudly here rather than being faked.
        """
        from schools.vs_onboarding.exceptions import OnboardingError
        from schools.vs_onboarding.models import OnboardingTask
        from schools.vs_onboarding.services.tasks import transition_task

        current = {
            task.key: task.status
            for task in OnboardingTask.all_objects.filter(tenant=built.tenant)
        }
        for key, status in wanted.items():
            if current.get(key) == status:
                continue
            try:
                transition_task(built.tenant, key, status, actor=built.admin)
            except OnboardingError as error:
                self.stdout.write(self.style.WARNING(
                    f"    !  {key} -> {status}: {error}"
                ))

    def _close_required(self, built):
        from schools.vs_onboarding.constants import TaskStatus

        self._set_tasks(built, {key: TaskStatus.DONE for key in _required_keys()})

    def _age_pending(self, built, *, days):
        """Push the pending clock back so the expiry warning is live.

        Written straight onto the tenant because there is no service for "make
        this school older" - the sweep reads these columns and this is the only
        way to put a school inside the warning window without waiting 80 days.
        """
        from vs_tenants.models import Tenant

        Tenant.objects.filter(pk=built.tenant.pk).update(
            pending_since=timezone.now() - timedelta(days=days),
            expiry_warned_at=timezone.now(),
        )

    def _submit(self, built):
        from schools.vs_onboarding.constants import GoLiveStatus
        from schools.vs_onboarding.models import GoLiveRequest
        from schools.vs_onboarding.services import go_live

        pending = GoLiveRequest.all_objects.filter(
            tenant=built.tenant, status=GoLiveStatus.PENDING,
        ).first()
        if pending:
            return pending
        return go_live.submit_go_live(
            built.tenant,
            actor=built.admin,
            preferred_go_live_at=timezone.now() + timedelta(days=7),
            note="Staff training finishes the week before.",
            acknowledged=True,
        )

    def _reject(self, built, request, reviewer):
        from schools.vs_onboarding.services import go_live

        go_live.reject_go_live(
            built.tenant, request.pk, actor=reviewer,
            rejection_reason=(
                "Your second term ends after the December break. Correct the "
                "term dates and send the request again."
            ),
        )
        return "rejected, with a reason, and ready to resubmit"

    def _activate(self, built, request, reviewer):
        from schools.vs_onboarding.services import go_live

        go_live.approve_go_live(built.tenant, request.pk, actor=reviewer)
        return "live - the control room is read-only and the full app is open"

    def _fail(self, built, request):
        """A failed activation, written as a fixture rather than driven.

        Deliberate, and the only state here that is not produced by its own
        service: activation fails when something inside it breaks, and there is
        no supported way to ask it to break. The row is written the way the
        service writes it - status FAILED, a correlation reference, no reviewer
        and no reason, readiness back to READY so the school can try again -
        because a failed activation rolls everything back and is emphatically
        not a rejection.
        """
        from schools.vs_onboarding.constants import GoLiveStatus, ReadinessState
        from schools.vs_onboarding.models import GoLiveRequest, OnboardingProgress

        GoLiveRequest.all_objects.filter(pk=request.pk).update(
            status=GoLiveStatus.FAILED,
            failure_reference=uuid.uuid4().hex,
            reviewed_by=None,
            reviewed_at=timezone.now(),
            rejection_reason="",
        )
        OnboardingProgress.all_objects.filter(tenant=built.tenant).update(
            readiness_state=ReadinessState.READY,
        )
        reference = GoLiveRequest.all_objects.get(pk=request.pk).failure_reference
        return f"activation failed - reference {reference}"

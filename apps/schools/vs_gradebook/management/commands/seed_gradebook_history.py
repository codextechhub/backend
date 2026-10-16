"""Give the gradebook a school year behind the current one, so history has something to show.

``seed_gradebook_scenarios`` builds the current term. What it cannot build is
the year before: the student seeder places pupils in the active year only, the
year before is archived, and an archived year takes no placement and no
teaching, as it should. So the term-on-term movers, a last-term report card with
the year block, and a Results tab with more than one year have nothing to read.

This command replays the year before as the school would have run it, for the
pupils who came through it:

* **Who.** The pupils now in Lagoon View's JSS2 A were last year's JSS1 A, the
  promotion they actually came through. They are placed in last year's JSS1 A
  from the first day of that year, through the students module's own enrolment
  writer, and the placement is closed as PROMOTED on the last day of the year,
  as a year-end promotion leaves it.
* **What.** Each term of that year is brought to PUBLISHED through the same
  steps ``seed_gradebook_scenarios`` uses for the current term (teaching from
  the staff module, marks, comments, submission, approval and publication from
  the gradebook's services), with the clock pinned to the end of each term in
  turn, oldest first, so its dates are that term's. The third term is the last of its year, so its
  cards carry the year block. Each term's marks drift by their own few points
  from the current term's, so the pupils rose and fell across the year and
  against this one.
* **The closed year.** For the length of the run the year is marked DRAFT
  rather than ARCHIVED, which is what lets placements and teaching be written,
  and then put back exactly as it was, archived at the same moment, whatever
  happens in between. DRAFT and not ACTIVE: activating it would archive the
  current year, and every screen would read last year as the running one. For
  those minutes the year reads as a draft in the academic structure list.

Once JSS2 A is published this term (it is seeded as returned to its class
teacher, so a tester resubmits and publishes it), the analytics' term-on-term
table lists every one of its pupils against last year's Third Term.

**Idempotent.** A pupil already placed last year is left alone, and each term's
class is brought to PUBLISHED only if it is not already, so a second run prints
``Created this run: 0``.

**Nobody is told.** A year that has finished is not news: the run sends no
notification at all, in-app or by email, so no guardian hears today about a
report card from last year. The results are released, and their email state
records that nothing was sent.

    python manage.py seed_gradebook_history

Run ``seed_gradebook_scenarios`` first. Never run against production.

Fill a school onboarded through the app instead with ``--school <slug> --like
<school above> --admin-email <its admin>`` (see ``schools.vs_schools.dev.targets``).
"""
from __future__ import annotations

import datetime as dt
from contextlib import contextmanager
from types import SimpleNamespace
from unittest import mock

from django.core.management.base import BaseCommand, CommandError

from schools.vs_schools.dev.targets import add_target_arguments, quiet, resolve_targets
from schools.vs_academics.models import AcademicSession, AcademicTerm, SchoolClass, SessionStatus
from schools.vs_gradebook.models import ClassTerm
from schools.vs_gradebook.constants import StoredStage
from schools.vs_gradebook.services import book, resolve
from schools.vs_students.constants import EnrolmentOutcome
from schools.vs_students.models import ClassEnrolment
from schools.vs_students.services.placement import write_enrolment
from vs_config.clock import tenant_today

from .seed_gradebook_scenarios import PLANS, PUBLISHED, ClassPlan
from .seed_gradebook_scenarios import Command as ScenarioSeeder

#: Each school replayed: the role whose class's pupils came through last year,
#: and the class teacher of last year's class.
HISTORY = {"lagoon-view": ("class returned", "fashola")}


@contextmanager
def year_open_for_replay(session):
    """Mark an archived *session* DRAFT for the block, and put it back exactly as it was after."""
    status, archived_at = session.status, session.archived_at
    AcademicSession.all_objects.filter(pk=session.pk).update(
        status=SessionStatus.DRAFT, archived_at=None,
    )
    session.refresh_from_db()
    try:
        yield
    finally:
        AcademicSession.all_objects.filter(pk=session.pk).update(
            status=status, archived_at=archived_at,
        )
        session.refresh_from_db()


@contextmanager
def clock_at(day):
    """Pin every clock this process reads to noon on *day*.

    The school's own clock decides what "today" is for a term; Django's decides
    the moment a sheet was submitted, a class published and a result released.
    Both are pinned, so last year's report cards read as released at the end of
    last year's term and not on the day the seed ran.
    """
    instant = dt.datetime.combine(day, dt.time(12), tzinfo=dt.timezone.utc)
    with mock.patch("vs_config.clock.timezone", SimpleNamespace(now=lambda: instant)), \
            mock.patch("django.utils.timezone.now", lambda: instant):
        yield


class Command(BaseCommand):
    help = "Replay the school year before the current one for the pupils who came through it."

    def add_arguments(self, parser):
        add_target_arguments(parser)

    def handle(self, *args, **options):
        targets = resolve_targets(options, tuple(HISTORY))
        self.seeder = ScenarioSeeder(stdout=self.stdout, stderr=self.stderr)
        self.seeder.created = 0
        with quiet():
            for target in targets:
                self._replay(target)
        self.stdout.write(self.style.SUCCESS(f"\nCreated this run: {self.seeder.created}"))

    def _replay(self, target):
        slug, recipe = target.slug, target.recipe
        seeder = self.seeder
        school = seeder._school(slug)
        tenant = school.tenant
        year = AcademicSession.objects.filter(tenant=tenant, status=SessionStatus.ACTIVE).first()
        today = tenant_today(tenant)
        term = resolve.current_term(tenant, today)
        if year is None or term is None:
            raise CommandError(f"{slug} has no running term. Run seed_gradebook_scenarios first.")
        previous = AcademicSession.all_objects.filter(
            tenant=tenant, start_date__lt=year.start_date,
        ).order_by("-start_date").first()
        if previous is None:
            raise CommandError(f"{slug} has no year before {year.name} to replay.")

        s = SimpleNamespace(
            slug=slug, recipe=recipe, school=school, tenant=tenant,
            admin=seeder._admin(tenant, target),
            branches=list(tenant.branches.order_by("-is_main", "code")),
            year=year, term=term, today=today, people={},
        )
        self.stdout.write(self.style.MIGRATE_HEADING(
            f"\n{school.name} ({slug}): replaying {previous.name} ({previous.status.lower()})",
        ))
        seeder._people(s)
        s.cast = seeder._pick_classes(s)
        role, teacher = HISTORY[recipe]
        if role not in {plan.label for plan in PLANS[recipe]}:
            raise CommandError(f"{slug} has no {role!r} role in seed_gradebook_scenarios to replay.")
        source, home = s.cast[role]
        last = self._counterpart(tenant, previous, source, home)
        pupils = [entry.student for entry in book.rolls_for(tenant, term, [(source, home)])[source.pk]]
        terms = list(AcademicTerm.all_objects.filter(session=previous).order_by("order_index", "start_date", "pk"))
        if not terms:
            raise CommandError(f"{previous.name} has no terms to replay.")

        with year_open_for_replay(previous):
            self._place(s, previous, last, pupils)
            for past in terms:
                self._publish(s, previous, past, last, home, teacher)
        self.stdout.write(
            f"  {previous.name} is {previous.status.lower()} again. {last.name} of {previous.name} "
            f"holds {len(pupils)} pupils, now in {source.name}.",
        )

    @staticmethod
    def _counterpart(tenant, previous, source, home):
        """Last year's class of the level below *source*'s, in the same branch and arm."""
        level = source.level
        below = (
            SchoolClass.all_objects.filter(
                tenant=tenant, session=previous, is_active=True, branch=home,
                level__program__code=level.program.code,
                level__order_index=level.order_index - 1,
            )
            .select_related("level", "branch")
            .order_by("name")
        )
        arm = source.name.split()[-1]
        found = next((c for c in below if c.name.split()[-1] == arm), None) or below.first()
        if found is None:
            raise CommandError(
                f"{previous.name} has no class the level below {source.name} in {home.name} "
                f"to have held its pupils.",
            )
        return found

    def _place(self, s, previous, last, pupils):
        """Put each pupil in last year's class from its first day, closed as promoted on its last."""
        placed = set(
            ClassEnrolment.objects.filter(
                tenant=s.tenant, session=previous, student__in=pupils,
            ).values_list("student_id", flat=True)
        )
        fresh = [pupil for pupil in pupils if pupil.pk not in placed]
        ended = dt.datetime.combine(previous.end_date, dt.time(17), tzinfo=dt.timezone.utc)
        for pupil in fresh:
            write_enrolment(
                student=pupil, school_class=last, intended_year=previous, actor=s.admin,
                effective_date=previous.start_date, outcome=EnrolmentOutcome.PROMOTED,
                is_active=False, ended_at=ended,
            )
        if fresh:
            self.seeder._made(
                f"{len(fresh)} pupils placed in {last.name} for {previous.name}, promoted at its end",
            )

    def _publish(self, s, previous, past, last, home, teacher):
        """Bring last year's class to PUBLISHED for *past*, on the school's clock at that term's end."""
        done = ClassTerm.objects.filter(
            tenant=s.tenant, term=past, school_class=last, stage=StoredStage.PUBLISHED,
        ).exists()
        if done:
            return
        label = f"{last.name}, {past.name} of {previous.name}"
        plan = ClassPlan(
            label, 0, last.level.program.code, teacher, PUBLISHED, drift=True, drift_key=past.name,
        )
        replay = SimpleNamespace(**vars(s))
        replay.year, replay.term, replay.today = previous, past, past.end_date
        replay.cast = {**s.cast, label: (last, home)}
        with clock_at(past.end_date), mock.patch(
            "schools.vs_gradebook.services.publishing.send_notification", return_value=[],
        ):
            self.seeder._class(replay, plan)
            self.seeder._made(f"{label} published")


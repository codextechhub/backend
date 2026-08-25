"""Build academic structure for two shapes of school, so it can be looked at.

A screen cannot be checked against an endpoint that returns nothing, and the
two shapes this module has to serve cannot both exist in one tenant:

    brightfield-lekki   Two branches. A shared curriculum, plus a department, a
                        programme and a subject that belong to one branch only,
                        so every scope chip and every branch filter has
                        something real behind it.
    st-monicas          One branch. Every row shared, which is what a
                        single-branch school writes, and the case where the
                        whole branch dimension must recede from the responses.
    holy-cross          Two branches AND LIVE. The other two are still
                        onboarding, so neither can reach anything gated on a
                        live tenant - the Export Centre most of all. This is the
                        one to drive the whole module against.

Everything is driven through the real services - ``activate_session``,
``set_branches``, the branch scope helpers - rather than by writing rows that
look right. A state that cannot be reached honestly fails loudly here instead
of being faked into existence and believed later.

Idempotent: re-running tops each school up and leaves what already matches.

    python manage.py seed_academic_scenarios
    python manage.py seed_academic_scenarios --only st-monicas

Run ``seed_onboarding_scenarios`` first, which builds the schools themselves.
Never run against production.
"""
from __future__ import annotations

import datetime as dt

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from schools.vs_academics.models import (
    AcademicSession,
    AcademicTerm,
    Department,
    Level,
    Program,
    SchoolClass,
    SessionStatus,
    Subject,
    SubjectOffering,
)
from schools.vs_academics.services.sessions import (
    activate_session,
    set_branches,
    validate_terms,
)

CAST = ("brightfield-lekki", "st-monicas", "holy-cross")

#: Three years, so the list has an archived, a live and a draft one to show.
YEARS = (
    ("2025/2026", dt.date(2025, 9, 8), dt.date(2026, 7, 17), "archived"),
    ("2026/2027", dt.date(2026, 8, 17), dt.date(2027, 7, 16), "active"),
    ("2027/2028", dt.date(2027, 9, 6), dt.date(2028, 7, 14), "draft"),
)

#: name, code, department (None where a school runs it outside any), levels
#:
#: Two of the four carry a department deliberately. A department nothing points
#: at can always be deleted, so a seed where NO programme is mapped cannot reach
#: the refusal that says otherwise - and the screen that has to explain it is
#: then untestable. Nursery and Primary stay unmapped, which is also true of
#: most schools, so both sides of the delete are reachable.
PROGRAMMES = (
    ("Nursery", "NUR", None, ("Nursery 1", "Nursery 2")),
    ("Primary", "PRI", None, tuple(f"Primary {n}" for n in range(1, 7))),
    ("Junior Secondary", "JSS", "Sciences", ("JSS1", "JSS2", "JSS3")),
    ("Senior Secondary", "SSS", "Sciences", ("SSS1", "SSS2", "SSS3")),
)

DEPARTMENTS = (
    ("Sciences", "SCI"), ("Arts", "ART"),
    ("Commercial", "COM"), ("Languages", "LAN"),
)

#: name, code, department, core, the levels it is offered at
SUBJECTS = (
    ("Mathematics", "MTH", "Sciences", True, "PRI+JSS+SSS"),
    ("English Language", "ENG", "Languages", True, "ALL"),
    ("Basic Science", "BSC", "Sciences", True, "JSS"),
    ("Civic Education", "CVE", "Arts", True, "JSS+SSS"),
    ("Further Mathematics", "FMT", "Sciences", False, "SSS"),
    ("Economics", "ECO", "Commercial", False, "SSS"),
)


def _terms_for(name, start, end):
    """Three terms that sit inside the year and do not overlap.

    Proportional to the year's own length rather than fixed, so a school with a
    different calendar still gets a coherent set instead of terms that spill
    out of their session.
    """
    span = (end - start).days
    third = span // 3
    out = []
    for i, label in enumerate(("First Term", "Second Term", "Third Term")):
        t_start = start + dt.timedelta(days=i * third)
        t_end = start + dt.timedelta(days=(i + 1) * third - 21)
        out.append({
            "name": label, "order_index": i + 1,
            "start_date": t_start, "end_date": min(t_end, end),
        })
    out[-1]["end_date"] = end
    return out


class Command(BaseCommand):
    help = "Seed academic structure for the multi-branch and single-branch cases."

    def add_arguments(self, parser):
        parser.add_argument("--only", help="One school slug from the cast.")

    def handle(self, *args, **options):
        only = options.get("only")
        slugs = [only] if only else list(CAST)
        if only and only not in CAST:
            raise CommandError(
                f"{only!r} is not in the cast. Pick one of: {', '.join(CAST)}",
            )
        for slug in slugs:
            self._seed(slug)

    def _school(self, slug):
        from schools.vs_schools.models import School

        school = School.objects.filter(slug=slug).first()
        if school is None:
            raise CommandError(
                f"No school {slug!r}. Run seed_onboarding_scenarios first - it "
                f"builds the schools this command hangs structure off.",
            )
        return school

    @transaction.atomic
    def _seed(self, slug):
        school = self._school(slug)
        tenant = school.tenant
        branches = list(tenant.branches.order_by("-is_main", "code"))
        multi = len(branches) > 1
        secondary = branches[1] if multi else None

        self.stdout.write(
            f"\n  {school.name} ({slug}) - "
            f"{len(branches)} branch{'es' if multi else ''}",
        )

        year = self._years(tenant)
        depts = self._departments(tenant, secondary)
        # Structure hangs off ONE year - the live one. A school's other years
        # are its history and its plan; seeding all three identically would
        # make "switch the year" look like it does nothing, which is the very
        # thing the year column exists to fix.
        levels = self._programmes(tenant, secondary, depts, year)
        self._classes(tenant, levels, branches, multi, year)
        self._subjects(tenant, depts, levels, secondary, year)
        self._history(tenant, year)

        self.stdout.write(self.style.SUCCESS(
            f"    done: {Program.all_objects.filter(tenant=tenant).count()} programmes, "
            f"{Level.all_objects.filter(tenant=tenant).count()} levels, "
            f"{SchoolClass.all_objects.filter(tenant=tenant).count()} classes, "
            f"{Subject.all_objects.filter(tenant=tenant).count()} subjects",
        ))

    # ── years ──────────────────────────────────────────────────────────────
    def _years(self, tenant):
        """Builds the three years and returns the LIVE one."""
        live = None
        for name, start, end, state in YEARS:
            session = AcademicSession.all_objects.filter(
                tenant=tenant, name=name,
            ).first()
            if session is None:
                session = AcademicSession.all_objects.create(
                    tenant=tenant, name=name, start_date=start, end_date=end,
                )
                terms = _terms_for(name, start, end)
                # Through the real validator, so a calendar this command gets
                # wrong fails here rather than becoming data nobody trusts.
                validate_terms(session, terms)
                AcademicTerm.all_objects.bulk_create([
                    AcademicTerm(tenant=tenant, session=session, **t) for t in terms
                ])
                set_branches(session, tenant, [])       # the whole school

            if state == "active":
                if session.status != SessionStatus.ACTIVE:
                    activate_session(session, tenant)
                live = session
            elif state == "archived" and session.status != SessionStatus.ARCHIVED:
                from schools.vs_academics.services.sessions import archive_session

                archive_session(session, tenant)
        self.stdout.write(f"    years: {len(YEARS)}")
        return live

    def _history(self, tenant, live):
        """Give last year a structure of its own, through the real rollover.

        So switching the pill to 2025/2026 shows that year's classes rather
        than an empty screen - the honest-history half of the change. Next
        year is left EMPTY on purpose: it is the state the "copy a year
        forward" flow exists for, and a seed that filled it would hide the one
        screen worth testing.
        """
        from schools.vs_academics.services.rollover import (
            NothingToCopy,
            TargetYearNotEmpty,
            roll_forward,
        )

        past = (
            AcademicSession.all_objects.filter(
                tenant=tenant, status=SessionStatus.ARCHIVED,
            )
            .order_by("start_date")
            .first()
        )
        if past is None or live is None:
            return
        try:
            written = roll_forward(tenant, source=live, target=past)
        except (TargetYearNotEmpty, NothingToCopy):
            return                                  # already seeded, or nothing yet
        self.stdout.write(
            f"    {past.name}: {written['levels']} levels, "
            f"{written['classes']} classes, {written['subjects']} subjects",
        )

    # ── catalogue ──────────────────────────────────────────────────────────
    def _departments(self, tenant, secondary):
        out = {}
        for name, code in DEPARTMENTS:
            out[name], _ = Department.all_objects.get_or_create(
                tenant=tenant, name=name, defaults={"code": code},
            )
        if secondary is not None:
            # One branch-only department, so the scope chip and the branch
            # filter have a row that is genuinely not school-wide.
            out["General Studies"], _ = Department.all_objects.get_or_create(
                tenant=tenant, name="General Studies",
                defaults={"code": "GST", "branch": secondary},
            )
        return out

    def _programmes(self, tenant, secondary, depts, year):
        levels = {}
        for index, (name, code, dept, level_names) in enumerate(PROGRAMMES):
            program, created = Program.all_objects.get_or_create(
                tenant=tenant, name=name,
                defaults={
                    "code": code, "order_index": index,
                    "department": depts.get(dept) if dept else None,
                },
            )
            # Top up a programme seeded before this column was filled in, so a
            # re-run fixes an existing dev database rather than only a fresh one.
            if not created and dept and program.department_id is None:
                program.department = depts.get(dept)
                program.save(update_fields=["department", "updated_at"])
            for order, level_name in enumerate(level_names, start=1):
                level, _ = Level.all_objects.get_or_create(
                    tenant=tenant, session=year, program=program, name=level_name,
                    defaults={
                        "code": level_name.replace(" ", "").upper()[:20],
                        "order_index": order,
                    },
                )
                levels[level_name] = level

        if secondary is not None:
            # A whole programme one branch runs and the other does not - the
            # arrangement the nullable branch column exists for.
            vocational, _ = Program.all_objects.get_or_create(
                tenant=tenant, name="Vocational",
                defaults={"code": "VOC", "order_index": 9, "branch": secondary},
            )
            for order, level_name in enumerate(("Vocational 1", "Vocational 2"), 1):
                level, _ = Level.all_objects.get_or_create(
                    tenant=tenant, session=year, program=vocational, name=level_name,
                    defaults={
                        "code": f"VOC{order}", "order_index": order,
                        "branch": secondary,
                    },
                )
                levels[level_name] = level
        return levels

    def _classes(self, tenant, levels, branches, multi, year):
        """Arms for the secondary levels, spread across the branches."""
        made = 0
        for level_name in ("JSS1", "JSS2", "SSS1", "SSS2"):
            level = levels.get(level_name)
            if level is None:
                continue
            for i, arm in enumerate(("A", "B")):
                # A single-branch school writes every class school-wide, which
                # is the shape the receding dimension has to be tested against.
                branch = branches[i % len(branches)] if multi else None
                name = f"{level_name} {arm}"
                if SchoolClass.all_objects.filter(
                    tenant=tenant, level=level, name=name,
                ).exists():
                    continue
                SchoolClass.all_objects.create(
                    tenant=tenant, session=year, level=level, name=name,
                    code=f"{level.code}-{arm}"[:20], arm=arm, branch=branch,
                    capacity=30,
                )
                made += 1
        self.stdout.write(f"    classes: +{made}")

    def _subjects(self, tenant, depts, levels, secondary, year):
        groups = {
            "ALL": list(levels),
            "PRI+JSS+SSS": [n for n in levels if n.startswith(("Primary", "JSS", "SSS"))],
            "JSS": [n for n in levels if n.startswith("JSS")],
            "JSS+SSS": [n for n in levels if n.startswith(("JSS", "SSS"))],
            "SSS": [n for n in levels if n.startswith("SSS")],
        }
        for name, code, dept, core, group in SUBJECTS:
            subject, created = Subject.all_objects.get_or_create(
                tenant=tenant, session=year, name=name,
                defaults={
                    "code": code, "is_core": core,
                    "department": depts.get(dept),
                },
            )
            if not created:
                continue
            SubjectOffering.all_objects.bulk_create([
                SubjectOffering(tenant=tenant, subject=subject, level=levels[n])
                for n in groups[group] if n in levels
            ])

        if secondary is not None:
            # A subject one branch teaches and the other does not.
            yoruba, created = Subject.all_objects.get_or_create(
                tenant=tenant, session=year, name="Yoruba",
                defaults={
                    "code": "YOR", "is_core": False, "branch": secondary,
                    "department": depts.get("Languages"),
                },
            )
            if created:
                SubjectOffering.all_objects.bulk_create([
                    SubjectOffering(tenant=tenant, subject=yoruba, level=levels[n])
                    for n in groups["JSS"] if n in levels
                ])

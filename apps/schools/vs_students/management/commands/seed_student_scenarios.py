"""Put students on the roll, so every screen in this module has something behind it.

A screen cannot be checked against an endpoint that returns nothing, and the
states this module has to show cannot all exist in one school:

    brightfield-lekki   Two branches. A full roll with children at both, one
                        guardian standing for siblings across the branch
                        boundary, a class near capacity, applicants waiting, a
                        suspension, a withdrawal, a transfer out and a
                        rejection - so every status chip, every filter and
                        every exception path has a real row behind it.
    st-monicas          One branch, still onboarding. The single-branch shape
                        on a school that has NOT gone live.
    holy-cross          Two branches, pending approval. The multi-branch shape
                        with a full roll behind it.
    sunrise-academy     One branch AND LIVE. The recede case: the only place
                        the rule that the branch dimension disappears at a
                        one-branch school can actually be seen, because every
                        student route answers 403 TENANT_NOT_LIVE to a school
                        that is still onboarding.
    lagoon-view         Two branches AND LIVE. The branch-filter case. The
                        onboarding cast parks holy-cross at "pending approval"
                        rather than live, so without this pair a clean reseed
                        left NOTHING in this module reachable at all - the two
                        rolls with children were both closed. These two are the
                        schools to drive the module against.

**Everything goes through the real services.** Enrolment, placement, every
status change and the promotion run are all called the way the API calls them,
so a state that cannot be reached honestly fails loudly here rather than being
written directly and believed later. That is the whole point of the command: a
roll assembled by writing rows would happily contain a suspended student with
no placement, which the state machine forbids.

Idempotent: re-running tops each school up and leaves what already matches.

    python manage.py seed_student_scenarios
    python manage.py seed_student_scenarios --only st-monicas

Run ``seed_onboarding_scenarios`` and ``seed_academic_scenarios`` first - this
one hangs children off the classes they build. Never run against production.
"""
from __future__ import annotations

import datetime as dt

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from schools.vs_academics.models import AcademicSession, SchoolClass, SessionStatus
from schools.vs_students.constants import (
    Gender,
    Relationship,
    StudentStatus,
)
from schools.vs_students.models import Guardian, Student
from schools.vs_students.services import enrolment as enrolment_service
from schools.vs_students.services import guardians as guardian_service
from schools.vs_students.services.placement import place
from schools.vs_students.services.status import transition

CAST = (
    "brightfield-lekki", "st-monicas", "holy-cross",
    "sunrise-academy", "lagoon-view",
)

#: Split by gender so an honorific is never derived independently of the name.
#: Picking a title and a name from two lists produced "Mrs. James Eze".
FEMALE_FIRSTS = [
    "Adaeze", "Chinelo", "Sade", "Nkechi", "Bola", "Ifeoma", "Amaka",
    "Folake", "Ebere", "Halima", "Zainab", "Ngozi", "Grace", "Esther",
]
MALE_FIRSTS = [
    "Obinna", "Yemi", "Uche", "Segun", "Chuka", "Tunde", "Kunle", "Musa",
    "Bashir", "Femi", "Emeka", "Daniel", "Victor", "Samuel",
]
SURNAMES = [
    "Adeyemi", "Okoro", "Balogun", "Eze", "Ibrahim", "Nwachukwu", "Ojo",
    "Danjuma", "Afolabi", "Chukwu", "Okafor", "Adeleke", "Bello", "Nwankwo",
]
STATES = ["Anambra", "Lagos", "Kano", "Ogun", "Enugu", "Kaduna", "Oyo", "Delta"]


class Command(BaseCommand):
    help = "Seed students, guardians and their movements for three shapes of school."

    def add_arguments(self, parser):
        parser.add_argument("--only", help="One school slug from the cast.")

    def handle(self, *args, **options):
        only = options.get("only")
        if only and only not in CAST:
            raise CommandError(
                f"{only!r} is not in the cast. Pick one of: {', '.join(CAST)}",
            )
        for slug in ([only] if only else list(CAST)):
            self._seed(slug)

    def _school(self, slug):
        from schools.vs_schools.models import School

        school = School.objects.filter(slug=slug).first()
        if school is None:
            raise CommandError(
                f"No school {slug!r}. Run seed_onboarding_scenarios first - it "
                f"builds the schools this command hangs children off.",
            )
        return school

    def _actor(self, tenant):
        from vs_user.models import User

        user = User.objects.filter(tenant=tenant).order_by("pk").first()
        if user is None:
            raise CommandError(
                f"{tenant.slug} has no user to act as. Run "
                f"seed_onboarding_scenarios first.",
            )
        return user

    @transaction.atomic
    def _seed(self, slug):
        school = self._school(slug)
        tenant = school.tenant
        actor = self._actor(tenant)
        branches = list(tenant.branches.order_by("-is_main", "code"))
        multi = len(branches) > 1

        year = AcademicSession.objects.filter(
            tenant=tenant, status=SessionStatus.ACTIVE,
        ).first()
        if year is None:
            raise CommandError(
                f"{slug} has no active session. Run seed_academic_scenarios "
                f"first - a student is placed into a year, and there is none.",
            )
        classes = list(
            SchoolClass.objects.filter(
                tenant=tenant, session=year, is_active=True,
            ).select_related("level", "branch").order_by("level__order_index", "name"),
        )
        if not classes:
            raise CommandError(
                f"{slug} has no classes in {year}. Run seed_academic_scenarios "
                f"first.",
            )

        self.stdout.write(
            f"\n  {school.name} ({slug}) - "
            f"{len(branches)} branch{'es' if multi else ''}, {len(classes)} classes",
        )

        made = self._roll(tenant, actor, branches, classes, year)
        self._siblings(tenant, actor, made)
        self._applicants(tenant, actor, branches, classes)
        self._movements(tenant, actor, made)

        self.stdout.write(self.style.SUCCESS(
            f"    done: "
            f"{Student.all_objects.filter(tenant=tenant).count()} students, "
            f"{Guardian.all_objects.filter(tenant=tenant).count()} guardians, "
            f"{self._by_status(tenant)}",
        ))

    def _by_status(self, tenant):
        counts = {}
        for row in Student.all_objects.filter(tenant=tenant).values_list(
            "status", flat=True,
        ):
            counts[row] = counts.get(row, 0) + 1
        return ", ".join(f"{n} {k.lower()}" for k, n in sorted(counts.items()))

    # ── the roll ───────────────────────────────────────────────────────────

    def _roll(self, tenant, actor, branches, classes, year):
        """Fill the classes to plausible loads.

        A thirty-seat class holding three students is not a school, and it makes
        every capacity state unreachable: the near-capacity, full and
        over-capacity paths all need a real load behind them before anyone can
        see whether they work.
        """
        made = []
        for index, school_class in enumerate(classes):
            capacity = school_class.capacity or 30
            wanted = max(4, int(capacity * (0.9 if index % 3 == 0 else 0.6)))
            branch = school_class.branch or branches[index % len(branches)]

            existing = Student.all_objects.filter(
                tenant=tenant, enrolments__school_class=school_class,
                enrolments__is_active=True,
            ).count()
            for n in range(existing, wanted):
                student = self._one(
                    tenant, actor, branch, school_class, year, index * 100 + n,
                )
                if student is not None:
                    made.append(student)
        return made

    @staticmethod
    def _household(last):
        """Every fact about a household, derived from the household's own key.

        A guardian here is SHARED by every student with this surname - that
        sharing is the relationship the Guardians screen exists to show. But
        Three of the guardian's fields must not be derived from whichever child
        happened to be enrolled first, or derived non-deterministically:

          * the honorific and the initial came from that child's seed, while
            the RELATIONSHIP on each link came from each child's own seed. So
            the Adeleke household's "Mrs. T. Adeleke" was linked as the FATHER
            of her second child - exactly the "Mrs. James Eze" defect the
            first-name pools at the top of this file were split to prevent,
            arriving by a different route.
          * the phone came from ``hash(last)``, which Python randomises per
            process, so a command that advertises idempotence gave the
            household a different number on every run.

        Deriving all four from the surname alone makes the household one
        consistent person, and the same person tomorrow.
        """
        slot = sum(ord(c) for c in last)
        female = slot % 2 == 0
        pool = FEMALE_FIRSTS if female else MALE_FIRSTS
        return {
            "honorific": "Mrs." if female else "Mr.",
            "initial": pool[slot % len(pool)][0],
            "relationship": (
                Relationship.MOTHER if female else Relationship.FATHER
            ),
            "phone": f"0806555{slot % 10000:04d}",
        }

    def _one(self, tenant, actor, branch, school_class, year, seed):
        female = seed % 2 == 0
        first = (FEMALE_FIRSTS if female else MALE_FIRSTS)[
            seed % len(FEMALE_FIRSTS if female else MALE_FIRSTS)
        ]
        last = SURNAMES[(seed // 3) % len(SURNAMES)]
        born = 2026 - (10 + (seed % 6))

        if Student.all_objects.filter(
            tenant=tenant, first_name=first, last_name=last,
            date_of_birth=dt.date(born, 1 + seed % 12, 1 + seed % 28),
        ).exists():
            return None

        # Household facts, keyed on the surname the household is keyed on.
        home = self._household(last)
        return enrolment_service.enrol(
            tenant=tenant, actor=actor, branch=branch,
            data={
                "first_name": first, "middle_name": "", "last_name": last,
                "date_of_birth": dt.date(born, 1 + seed % 12, 1 + seed % 28),
                "gender": Gender.FEMALE if female else Gender.MALE,
                "nationality": "Nigerian",
                "state_of_origin": STATES[seed % len(STATES)],
                "address": f"{seed % 90 + 1} Admiralty Way, Lekki, Lagos",
                "phone": "", "email": "", "previous_school": "",
                "blood_group": "", "allergies": "", "conditions": "",
                "emergency_contact_name": f"{home['honorific']} {last}",
                "emergency_contact_phone": f"0803555{seed % 10000:04d}",
                "student_number": "",
            },
            guardian_rows=[{
                # One guardian per household, keyed on the surname: students
                # sharing a surname share a guardian rather than each minting
                # their own, which is the relationship the Guardians screen
                # exists to show.
                "full_name": f"{home['honorific']} {home['initial']}. {last}",
                "phone": home["phone"],
                "email": f"{last.lower()}.household@example.ng",
                "relationship": home["relationship"],
                "is_primary": True,
            }],
            school_class=school_class,
            # The roll is being filled deliberately, so an over-capacity class
            # is a state to demonstrate rather than a refusal to work around.
            allow_over_capacity=True,
            confirm_duplicate=True,
        )

    # ── the relationships and the states ───────────────────────────────────

    def _siblings(self, tenant, actor, made):
        """One guardian standing for two children at two different branches.

        The case a school-level Guardian exists for, and the one a
        student-scoped guardian could not express. Skipped in a single-branch
        school, where it would prove nothing.
        """
        by_branch = {}
        for student in made:
            by_branch.setdefault(student.branch_id, []).append(student)
        if len(by_branch) < 2:
            return

        first, second = (rows[0] for rows in list(by_branch.values())[:2])
        guardian, _ = guardian_service.upsert_guardian(
            tenant, full_name="Mrs. Patricia Okafor",
            phone="08065550130", email="patricia.okafor@example.ng",
            occupation="Trader",
            address="17 Bisola Durosinmi Etti Drive, Lekki, Lagos",
        )
        for student in (first, second):
            if not student.guardian_links.filter(guardian=guardian).exists():
                guardian_service.link(
                    student, guardian, relationship=Relationship.AUNT,
                    is_primary=False, actor=actor,
                )

    def _applicants(self, tenant, actor, branches, classes):
        """The front of the lifecycle, and the two ends it can reach."""
        levels = [c.level for c in classes if c.level_id]
        if not levels:
            return
        wanted = [
            ("Zainab", "Yusuf", Gender.FEMALE, None),
            ("Ifeanyi", "Chukwu", Gender.MALE, None),
            ("Halima", "Sani", Gender.FEMALE, StudentStatus.REJECTED),
        ]
        for index, (first, last, gender, then) in enumerate(wanted):
            if Student.all_objects.filter(
                tenant=tenant, first_name=first, last_name=last,
            ).exists():
                continue
            student = enrolment_service.enrol(
                tenant=tenant, actor=actor, branch=branches[index % len(branches)],
                data={
                    "first_name": first, "middle_name": "", "last_name": last,
                    "date_of_birth": dt.date(2013, 3 + index, 9 + index),
                    "gender": gender, "nationality": "Nigerian",
                    "state_of_origin": STATES[index], "address": "",
                    "phone": "", "email": "", "previous_school": "",
                    "blood_group": "", "allergies": "", "conditions": "",
                    "emergency_contact_name": "", "emergency_contact_phone": "",
                    "applied_for": levels[index % len(levels)],
                },
                guardian_rows=[{
                    "full_name": f"Mrs. {first[0]}. {last}",
                    "phone": f"0811555{index:04d}",
                    "email": f"{last.lower()}.applicant@example.ng",
                    "relationship": Relationship.MOTHER, "is_primary": True,
                }],
                as_applicant=True, confirm_duplicate=True,
            )
            if then == StudentStatus.REJECTED:
                transition(
                    student, StudentStatus.REJECTED, actor=actor,
                    reason="Places for that level were filled.",
                )

    def _movements(self, tenant, actor, made):
        """A suspension, a withdrawal and a transfer out, on real students.

        Each goes through the state machine, so the log rows, the released
        seats and the audit events are the ones the API would have written.
        """
        candidates = [
            s for s in made if s.status == StudentStatus.ACTIVE
        ]
        if len(candidates) < 4:
            return

        suspended, withdrawn, transferred, unplaced = candidates[:4]

        if suspended.status == StudentStatus.ACTIVE:
            transition(
                suspended, StudentStatus.SUSPENDED, actor=actor,
                reason="Repeated absence without notice.",
            )
        if withdrawn.status == StudentStatus.ACTIVE:
            transition(
                withdrawn, StudentStatus.WITHDRAWN, actor=actor,
                reason="Family relocated to Abuja.",
            )
        if transferred.status == StudentStatus.ACTIVE:
            transition(
                transferred, StudentStatus.TRANSFERRED, actor=actor,
                reason="Parent request.",
                destination_school="Greenfield Academy, Abuja",
            )

        # One student on the roll with no class, so "Classes and transfers"
        # has something to place and the nav badge has a number in it.
        unplaced.enrolments.filter(is_active=True).update(
            is_active=False, ended_at=dt.datetime.now(dt.timezone.utc),
        )
        unplaced.status = StudentStatus.ENROLLED
        unplaced.save(update_fields=["status", "updated_at"])

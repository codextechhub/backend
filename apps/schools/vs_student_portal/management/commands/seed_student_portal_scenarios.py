"""Give pupils portal access and post notices, so the portal has something to show.

    lagoon-view       Two branches and live. The branch shows in the portal.
    sunrise-academy   One branch and live. The branch recedes.

At each school, from the pupils already on the roll:

    active     a pupil who has activated their account and can sign in with
               their admission number (and their email, where they have one);
    invited    a pupil whose invitation is waiting;
    revoked    a pupil whose access was taken away;

and four notices: a published school-wide one, a published one to the active
pupil's class (which they have read), a withdrawn one and a draft.

**Everything goes through the real services**: access is issued, activated
through the invitation token and revoked exactly as the API does it, and the
notices are drafted, published and withdrawn by the school's administrator, so
a state the services refuse fails here rather than being written in.

Idempotent: re-running leaves what already matches.

    python manage.py seed_student_portal_scenarios

Run ``seed_student_scenarios`` and ``seed_school_permissions`` first. Never run
against production.
"""
from __future__ import annotations

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

CAST = ("lagoon-view", "sunrise-academy")

#: The password the activated pupil signs in with, long enough for the password policy.
PASSWORD = "Pupil@School2026"

NOTICES = (
    ("Mid-term break begins Oct 25", "School closes on Friday 24 October and "
     "reopens on Monday 3 November. Enjoy the break.", "SCHOOL", "PUBLISHED"),
    ("PTA meeting Saturday", "All parents and guardians are invited to the "
     "PTA meeting this Saturday at 10am in the school hall.", "CLASS", "PUBLISHED"),
    ("Inter-house sports postponed", "Inter-house sports will not hold this "
     "week. A new date will be announced.", "SCHOOL", "WITHDRAWN"),
    ("Library books due back", "Return every borrowed library book before the "
     "end of term.", "SCHOOL", "DRAFT"),
)


class Command(BaseCommand):
    help = "Give pupils portal access in every state and post notices for them to read."

    def handle(self, *args, **options):
        for slug in CAST:
            self._seed(slug)

    def _school(self, slug):
        from schools.vs_schools.models import School

        school = School.objects.filter(slug=slug).first()
        if school is None:
            raise CommandError(f"No school {slug!r}. Run seed_onboarding_scenarios first.")
        return school

    def _admin(self, tenant):
        from schools.vs_notices.services.notices import reach_for
        from vs_user.models import User

        for user in User.objects.filter(tenant=tenant, status=User.Status.ACTIVE).order_by("pk"):
            if reach_for(tenant, user).whole:
                return user
        raise CommandError(
            f"{tenant.slug} has nobody who may post a whole-school notice. Run "
            f"seed_school_permissions first.",
        )

    @transaction.atomic
    def _seed(self, slug):
        from schools.vs_students.constants import ON_ROLL
        from schools.vs_students.exceptions import PortalAccessError
        from schools.vs_students.models import Student
        from schools.vs_students.services import portal_access

        school = self._school(slug)
        tenant = school.tenant
        admin = self._admin(tenant)
        pupils = list(
            Student.objects.filter(tenant=tenant, status__in=ON_ROLL)
            .filter(enrolments__is_active=True, enrolments__session__status="ACTIVE")
            .distinct().order_by("pk")
        )
        roles = {}
        for pupil in pupils:
            if len(roles) == 3:
                break
            state = portal_access.state_of(pupil)
            wanted = ("active", "invited", "revoked")[len(roles)]
            if state == "none":
                self._ensure_number(pupil)
                try:
                    portal_access.issue(pupil, actor=admin)
                except PortalAccessError:
                    continue
                pupil.refresh_from_db()
            elif state not in ("invited", wanted):
                continue
            roles[wanted] = pupil
        if len(roles) < 3:
            raise CommandError(f"{slug} has too few pupils who can be given access.")

        self._activate(roles["active"])
        if portal_access.state_of(roles["revoked"]) in ("invited", "active"):
            portal_access.revoke(roles["revoked"], actor=admin)

        self._notices(tenant, admin, roles["active"])

        active = roles["active"]
        self.stdout.write(self.style.SUCCESS(
            f"  {school.name}: {active.full_name} signs in as "
            f"{active.student_number}{' or ' + active.user.email if active.user.email else ''} "
            f"with {PASSWORD}; {roles['invited'].full_name} is invited; "
            f"{roles['revoked'].full_name} was revoked.",
        ))

    def _ensure_number(self, pupil):
        """Give a pupil without one the school's next admission number.

        A pupil signs in with their admission number, so one without a number
        and without an email cannot be given access: the refusal tells a school
        to number them first, and this does what the school would.
        """
        from schools.vs_students.services.enrolment import assert_number_free
        from schools.vs_students.services.policy import read_policy, suggest_number

        if pupil.student_number:
            return
        tenant = pupil.tenant
        candidate = suggest_number(
            tenant, policy=read_policy(tenant, pupil.branch), branch=pupil.branch,
        )
        serial = pupil.pk
        while True:
            candidate = candidate or f"{tenant.slug[:3].upper()}/2026/{serial:04d}"
            try:
                assert_number_free(tenant, candidate)
                break
            except Exception:
                candidate, serial = "", serial + 1000
        pupil.student_number = candidate
        pupil.save(update_fields=["student_number", "updated_at"])

    def _activate(self, pupil):
        """Follow the pupil's invitation link with the fixture password."""
        from vs_user.models import User
        from vs_user.services.invitation import InvitationService

        pupil.refresh_from_db()
        if pupil.user.status == User.Status.ACTIVE:
            return
        _, token = InvitationService.create(user=pupil.user, invited_by=None)
        InvitationService.activate(token, PASSWORD)

    def _notices(self, tenant, admin, reader):
        from schools.vs_notices.models import Notice
        from schools.vs_notices.services import notices as service

        enrolment = service.pupil_enrolment(reader)
        for title, body, kind, status in NOTICES:
            if Notice.objects.filter(tenant=tenant, title=title).exists():
                continue
            audience = (
                [{"kind": "CLASS", "id": enrolment.school_class_id}]
                if kind == "CLASS" else [{"kind": "SCHOOL"}]
            )
            notice = service.create(
                tenant, actor=admin, title=title, body=body, audience=audience,
                publish_now=status != "DRAFT",
            )
            if status == "WITHDRAWN":
                service.withdraw(notice, actor=admin)
            if kind == "CLASS":
                service.mark_read(notice, reader.user)

"""Put people on the staff list, so every screen in this module has something behind it.

A screen cannot be checked against an endpoint that returns nothing, and the
states this module has to show cannot all exist in one person, let alone one
school:

    holy-cross          Two branches and live, with the deepest academic
                        structure of the four and a ``teacher`` role template
                        of its own. **This is the school to drive the module
                        against.** It is the only live multi-branch school in
                        the cast, which is the shape the module is designed for:
                        a posting that means something, a reach that can be
                        wider than it, and enough classes and offerings for the
                        coverage grid to be more than a handful of cells.
    brightfield-lekki   Two branches, still onboarding. The full cast of
                        PEOPLE: every employment status, every account state
                        that disagrees with one, and a registrar posted
                        school-wide. Its role catalogue has no ``teacher`` in
                        it, so everybody here is granted School Admin by the
                        fallback below - which makes it the wrong school for
                        reading a role column and the right one for the
                        pre-live narrowing at a school with two branches.
    sunrise-academy     One branch and live. The recede case: the only place the
                        rule that the branch dimension disappears at a
                        one-branch school can actually be seen.
    st-monicas          One branch, still onboarding. The pre-live shape, where
                        the role picker narrows to the two admin roles and the
                        teaching, leave and lifecycle surfaces are closed.

**Everything goes through the real services.** The account, the invitation and
the grant come from ``UserCreationService``; every employment transition goes
through ``services.employment``; every teaching duty through
``services.teaching``. So a state that cannot be reached honestly fails loudly
here rather than being written directly and believed later. That is the whole
point: a staff list assembled by writing rows would happily contain somebody
Resigned with no employment event behind it, which the service forbids and every
history screen would then render as a person who left for no reason.

Two states are deliberately built by hand and each says so at the point it
happens: an account LOCKED by failed sign-ins, because faking three bad
passwords is worse than setting the column; and leave APPROVED, because
approving it honestly needs a second person to vote and the point of the row is
the screen rather than the ladder.

Idempotent: re-running tops each school up and leaves what already matches.

    python manage.py seed_staff_scenarios
    python manage.py seed_staff_scenarios --only sunrise-academy

Run ``seed_onboarding_scenarios`` and ``seed_academic_scenarios`` first: this one
hangs people off the schools the first builds and the classes the second does.
Never run against production.
"""
from __future__ import annotations

import datetime as dt

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from schools.vs_staff.services import employment

#: The password every seeded person accepts their invitation with.
#:
#: Not ``vs_schools.dev.fixtures.DEFAULT_PASSWORD``, which is eleven characters
#: and no longer satisfies the platform's own twelve-character policy. Nothing
#: else notices, because every other fixture sets a password directly and only
#: activation validates one. This seeder accepts invitations for real, so it is
#: the first thing to meet the rule, and it uses a password that passes rather
#: than a shared constant that does not.
SEED_PASSWORD = "SchoolStaff@2026"

#: Ordered so the first entry is the one to drive the module against, and so
#: that a bare run builds the richest school before the narrower ones.
CAST = ("holy-cross", "brightfield-lekki", "sunrise-academy", "st-monicas")

#: Split by gender so an honorific is never derived independently of the name.
#: Picking a title and a name from two lists produces "Mrs. James Eze".
PEOPLE = [
    # (first, last, gender, job title, employment status, posting index)
    ("Chukwuemeka", "Eze", "MALE", "Lead Teacher", "ACTIVE", 0),
    ("Funke", "Adeyemi", "FEMALE", "Bursar", "ACTIVE", 0),
    ("Tunde", "Bakare", "MALE", "Teacher", "ON_LEAVE", 1),
    ("Ngozi", "Okafor", "FEMALE", "Teacher", "ACTIVE", 0),
    ("Samuel", "Adeyemo", "MALE", "Teacher", "INVITED", 1),
    # No posting at all: across the whole school, which is a first-class value
    # and the row every branch roster has to show under School-wide.
    ("Adaeze", "Nwankwo", "FEMALE", "Registrar", "ACTIVE", None),
    ("Kola", "Ayanwale", "MALE", "Teacher", "RESIGNED", 0),
    ("Ibrahim", "Sule", "MALE", "Branch Administrator", "ACTIVE", 1),
    ("Gbenga", "Fashola", "MALE", "Teacher", "SUSPENDED", 1),
    ("Rukayat", "Bello", "FEMALE", "Procurement Officer", "TERMINATED", 0),
    ("Chinwe", "Obiora", "FEMALE", "Teacher", "ACTIVE", 0),
    ("Emeka", "Nnamdi", "MALE", "Teacher", "ACTIVE", 1),
]

QUALIFICATIONS = {
    "Eze": [("B.Sc. Mathematics", "University of Nigeria, Nsukka", 2010),
            ("PGDE", "National Teachers' Institute", 2013)],
    "Adeyemi": [("B.Sc. Accounting", "University of Lagos", 2006),
                ("ICAN (Chartered)", "Institute of Chartered Accountants of Nigeria", 2011)],
    "Bakare": [("B.Ed. Integrated Science", "Obafemi Awolowo University", 2014)],
    "Nwankwo": [("B.Sc. Public Administration", "Ahmadu Bello University", 2008)],
}


class Command(BaseCommand):
    help = "Seed staff, their duties and their leave for three shapes of school."

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

    # ── lookups ────────────────────────────────────────────────────────────

    def _school(self, slug):
        from schools.vs_schools.models import School

        school = School.objects.filter(slug=slug).first()
        if school is None:
            raise CommandError(
                f"No school {slug!r}. Run seed_onboarding_scenarios first - it "
                f"builds the schools this command hangs people off.",
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

    def _role(self, tenant, key):
        from vs_rbac.models import TenantRoleTemplate

        return TenantRoleTemplate.objects.filter(
            tenant=tenant, key=key, status="ACTIVE",
        ).first()

    # ── the work ───────────────────────────────────────────────────────────

    @transaction.atomic
    def _seed(self, slug):
        from schools.vs_academics.models import (
            AcademicSession,
            SchoolClass,
            SessionStatus,
            SubjectOffering,
        )

        from ...approvals import ensure_tenant_approval_templates
        from ...models import StaffProfile

        school = self._school(slug)
        tenant = school.tenant
        actor = self._actor(tenant)
        branches = list(tenant.branches.order_by("-is_main", "code"))
        if not branches:
            raise CommandError(f"{slug} has no branch, which cannot happen.")

        # Every school gets its own leave ladder. There is no platform
        # fallback for leave, so a school that skipped this has nothing behind
        # it. Provisioning creates the approver group EMPTY, so a request filed
        # before somebody is nominated parks rather than being approved unseen.
        ensure_tenant_approval_templates(tenant, created_by=actor)

        made = 0
        for first, last, gender, job_title, status, branch_index in PEOPLE:
            email = f"{first}.{last}@{slug}.test".lower()
            if StaffProfile.objects.filter(
                tenant=tenant, user__email=email,
            ).exists():
                continue
            branch = (
                None if branch_index is None
                else branches[min(branch_index, len(branches) - 1)]
            )
            profile = self._invite(
                tenant=tenant, actor=actor, first=first, last=last,
                gender=gender, job_title=job_title, branch=branch,
                email=email, number=made + 1, slug=slug,
            )
            if profile is None:
                continue
            self._drive_to(profile, status, actor)
            self._add_qualifications(profile, last, actor)
            made += 1

        self._lock_one_account(tenant)
        self._appoint_leave_approver(tenant, actor)
        self._teach(tenant, actor)
        self._record_leave(tenant, actor)

        self.stdout.write(self.style.SUCCESS(
            f"{slug}: {made} added, "
            f"{StaffProfile.objects.filter(tenant=tenant).count()} on the list.",
        ))

    def _invite(self, *, tenant, actor, first, last, gender, job_title, branch,
                email, number, slug):
        """One person, through the same services the endpoint calls."""
        from types import SimpleNamespace

        from vs_user.models import User
        from vs_user.serializers import UserCreateSerializer
        from vs_user.services.user import UserCreationService

        from ...services import creation

        # Teacher where the school has one, and School Admin where it does not.
        # A school builds its own catalogue, so the prebuilt teacher template is
        # not guaranteed to be there, and a seeder that insisted on it would
        # refuse to put anybody on the list at all. The cost is visible and is
        # named in the docstring: at a school without it, every row reads School
        # Admin, so that school is the wrong one to read a role column at.
        role = self._role(tenant, "teacher") or self._role(tenant, "school_admin")
        if role is None:
            raise CommandError(
                f"{slug} has no role to invite anybody into. Run "
                f"seed_all_permissions and seed_onboarding_scenarios first.",
            )
        # UserCreateSerializer reads the ACTOR off the request, which is how it
        # decides the owning tenant. A management command has no request, so it
        # gets the same stand-in the import executor uses for the same reason.
        request = SimpleNamespace(user=actor, tenant=tenant)
        payload = UserCreateSerializer(
            data={
                "first_name": first, "last_name": last, "email": email,
                "gender": gender, "role": role.key,
                "branch": str(branch.pk) if branch else None,
            },
            context={"request": request},
        )
        if not payload.is_valid():
            self.stdout.write(self.style.WARNING(
                f"  skipped {email}: {payload.errors}",
            ))
            return None
        user = UserCreationService.create_pending(
            payload.validated_data, actor,
        )
        UserCreationService.finalize_invitation(user=user, requested_by=actor)
        prefix = "".join(word[0] for word in slug.split("-")).upper()
        return creation.create_profile(
            tenant=tenant, user=user, actor=actor,
            staff_number=f"{prefix}/STF/{number:04d}",
            job_title=job_title, employment_type="FULL_TIME",
            hire_date=dt.date(2021, 9, 6), branch=branch,
        )

    def _drive_to(self, profile, status, actor):
        """Walk somebody to their state through the real transitions.

        INVITED stays where creation left it, which is the honest shape: the
        account is PENDING and the record says Invited, and they agree.

        Everybody else ACCEPTS THEIR INVITATION FIRST, for real: a fresh token
        is issued and activation is driven through the identity service, which
        sets the password, promotes the account and fires the signal this module
        listens to. Calling the promotion directly instead would move the record
        to Active while the account stayed PENDING, and the next transition
        would be refused with "Cannot suspend a PENDING account" - which is the
        module's own rule catching a seed that cheated, and is exactly why the
        seed does not.
        """
        from vs_user.services.invitation import InvitationService

        from ...constants import EmploymentStatus

        if status == EmploymentStatus.INVITED:
            return

        invitation, token = InvitationService.create(
            user=profile.user, invited_by=actor,
        )
        InvitationService.activate(token, SEED_PASSWORD)
        profile.refresh_from_db()

        if status == EmploymentStatus.ACTIVE:
            return

        employment.change_status(
            profile, to_status=status, actor=actor,
            effective_date=dt.date(2025, 9, 15),
            reason={
                EmploymentStatus.ON_LEAVE: "Study leave",
                EmploymentStatus.SUSPENDED: "Pending an internal review",
                EmploymentStatus.RESIGNED: "Moving abroad",
                EmploymentStatus.TERMINATED: "Gross misconduct",
            }.get(status, ""),
            last_working_day=(
                dt.date(2025, 12, 18)
                if status in (
                    EmploymentStatus.RESIGNED, EmploymentStatus.TERMINATED,
                ) else None
            ),
        )

    def _lock_one_account(self, tenant):
        """One account LOCKED while its owner is plainly employed.

        The one state in this command written directly, and it says so here.
        Reaching it honestly means three failed sign-ins against a live request
        cycle, which a seeder cannot stage and which would prove nothing about
        this module anyway. What the row is for is the directory: the account
        flag chip and the Locked accounts count both need somebody to be locked
        and employed at once, which is the pair a school is most likely to read
        as a suspension.
        """
        from vs_user.models import User

        from ...constants import EmploymentStatus
        from ...models import StaffProfile

        target = (
            StaffProfile.objects.filter(
                tenant=tenant, employment_status=EmploymentStatus.ACTIVE,
                user__status=User.Status.ACTIVE,
            )
            .order_by("pk")
            .last()
        )
        if target is None:
            return
        target.user.status = User.Status.LOCKED
        target.user.save(update_fields=["status", "updated_at"])

    def _add_qualifications(self, profile, last, actor):
        from ...services import creation

        rows = QUALIFICATIONS.get(last)
        if not rows:
            return
        creation.attach_qualifications(
            profile,
            [
                {
                    "qualification": title, "institution": institution,
                    "year_obtained": year,
                }
                for title, institution, year in rows
            ],
            actor=actor,
        )

    def _appoint_leave_approver(self, tenant, actor):
        """Somebody has to be in the group, or every request parks.

        Provisioning creates the group empty on purpose. A seeded school with
        nobody nominated would demonstrate the parking rule and nothing else, so
        the school's own administrator is put in it here, which is what a real
        school does on its first day.

        Added as a USER rather than as a ROLE, because the point of the seed is
        that somebody concrete can be seen approving something. A real school is
        more likely to nominate a role, and the group takes either.
        """
        from vs_workflow.models import (
            WorkflowApproverGroup,
            WorkflowApproverGroupMember,
        )

        from ...constants import LEAVE_APPROVER_GROUP_CODE

        group = WorkflowApproverGroup.all_objects.filter(
            tenant=tenant, code=LEAVE_APPROVER_GROUP_CODE,
        ).first()
        if group is None or group.members.exists():
            return
        WorkflowApproverGroupMember.objects.create(
            group=group, kind="USER", user=actor,
        )

    def _teach(self, tenant, actor):
        """Cover some subjects, leave one with only assistants, leave one bare.

        All three states on purpose: the coverage grid has to be able to show a
        pairing that is covered, one that is being taught by assistants with
        nobody owning the marks, and one nobody teaches at all. The middle one is
        the state a screen is most likely to render wrongly, because it looks
        covered until you ask who is responsible.
        """
        from schools.vs_academics.models import (
            AcademicSession,
            SchoolClass,
            SessionStatus,
            SubjectOffering,
        )

        from ...constants import EmploymentStatus, TeachingPart
        from ...models import StaffProfile
        from ...services import teaching

        year = AcademicSession.objects.filter(
            tenant=tenant, status=SessionStatus.ACTIVE,
        ).first()
        if year is None:
            self.stdout.write(self.style.WARNING(
                "  no active year, so no teaching duties. Run "
                "seed_academic_scenarios first.",
            ))
            return

        classes = list(
            SchoolClass.objects.filter(tenant=tenant, session=year, is_active=True)
            .select_related("level").order_by("level__order_index", "name")[:3],
        )
        teachers = list(
            StaffProfile.objects.filter(
                tenant=tenant, employment_status=EmploymentStatus.ACTIVE,
            ).order_by("pk"),
        )
        if not classes or not teachers:
            return

        for index, school_class in enumerate(classes):
            subjects = [
                offering.subject for offering in
                SubjectOffering.objects.filter(
                    tenant=tenant, level_id=school_class.level_id,
                ).select_related("subject").order_by("subject__name")[:3]
            ]
            for position, subject in enumerate(subjects):
                # The third subject of the first class is left with nobody, and
                # the second is left with an assistant and no lead.
                if index == 0 and position == 2:
                    continue
                part = (
                    TeachingPart.ASSISTANT
                    if index == 0 and position == 1
                    else TeachingPart.LEAD
                )
                teacher = teachers[(index + position) % len(teachers)]
                try:
                    teaching.assign(
                        tenant=tenant, staff=teacher, school_class=school_class,
                        subject=subject, session=year, part=part, actor=actor,
                    )
                except Exception:
                    # A pairing whose lead is already taken by an earlier run.
                    continue
            if index == 0:
                teaching.set_class_teacher(
                    school_class, teachers[0], actor=actor, tenant=tenant,
                )

    def _record_leave(self, tenant, actor):
        """One approved absence and one still waiting, so both chips are real.

        The approved one is written directly and this is the second of the two
        places the command does that. Approving honestly needs a second person to
        vote through the engine, and the point of this row is that the Leave tab
        and the directory's on-leave warning have something to render, not that
        the ladder works: ``test_leave.py`` proves the ladder.
        """
        from ...constants import EmploymentStatus, LeaveStatus
        from ...models import LeaveRequest, StaffProfile
        from ...services import leave as leave_service

        on_leave = StaffProfile.objects.filter(
            tenant=tenant, employment_status=EmploymentStatus.ON_LEAVE,
        ).first()
        if on_leave is None or on_leave.leave_requests.exists():
            return

        today = timezone.localdate()
        LeaveRequest.objects.create(
            tenant=tenant, staff=on_leave, leave_type="STUDY",
            start_date=today - dt.timedelta(days=20),
            end_date=today + dt.timedelta(days=40),
            days=60, status=LeaveStatus.APPROVED,
            decided_at=timezone.now(), requested_by=actor,
            note="Part-time M.Ed. residency.",
        )

        # And one filed the honest way, which lands PENDING and shows an
        # approver something to decide.
        active = StaffProfile.objects.filter(
            tenant=tenant, employment_status=EmploymentStatus.ACTIVE,
        ).exclude(user=actor).first()
        if active is not None and not active.leave_requests.exists():
            try:
                leave_service.file_request(
                    staff=active, leave_type="ANNUAL",
                    start_date=today + dt.timedelta(days=60),
                    end_date=today + dt.timedelta(days=70),
                    note="Christmas break.", actor=actor,
                )
            except Exception as error:
                self.stdout.write(self.style.WARNING(
                    f"  leave not filed: {error}",
                ))

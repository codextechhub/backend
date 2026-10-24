"""Give parents portal accounts in every state, with children to look at.

    lagoon-view       Two branches and live. The branch shows in the portal.
    sunrise-academy   One branch and live. The branch recedes.

At lagoon-view, from the pupils already on the roll:

    Okafor     Mrs Patricia Okafor, the guardian the student seeder links to
               children at different branches, active, with two attending
               children at two branches (any further child the student seeder
               linked, such as a suspended one, stays linked and is listed).
               One child owes
               the term's bill (part-paid at the desk), the other has paid it
               in full. Each has a bank transfer account number of their own,
               on the fake provider where it is on, so a deposit into either
               can be simulated. A published school-wide notice reaches both children
               and a published class notice reaches the one child in that
               class. The child with a released result, where one has it, is
               given an absent, a late and an excused day this term;
    teacher    a class teacher who is also the guardian of a pupil there. The
               guardian record is linked to the account the teacher already
               has, so the teacher signs in once, with the staff password, and
               one account serves both roles;
    invited    a guardian whose invitation is waiting and who has not signed
               in.

At sunrise-academy:

    one child  a guardian of a single pupil, active, with the term's bill
               part-paid at the desk and the rest owing.

A state a school cannot be put in through its own data is said so and
skipped: a school whose books cannot be opened bills nobody, and a pupil with
no released result shows none.

**Everything goes through the real services**: guardians are found or made
with ``upsert_guardian``, linked with ``link``, given access with
``guardian_portal_access.issue`` and activated through the invitation token
as the API does, so a state the services refuse fails here rather than being
written in. Bills, desk payments, registers and notices come from the student
portal seeder's own steps, so a parent sees the same ledger, the same
registers and the same notices a pupil does. A parent-only account has no
branch, no role and no permission: what it reaches is its guardian link.

Idempotent: re-running leaves what already matches and prints the same
summary.

    python manage.py seed_parent_portal_scenarios

Run the other seeders first, ending with ``seed_student_portal_scenarios``
(see ``reseed-dev.sh``). Never run against production.

Fill a school onboarded through the app instead of the cast with
``--school <slug> --like <cast slug>`` (see ``schools.vs_schools.dev.targets``).
"""
from __future__ import annotations

from io import StringIO

from django.core.management import call_command
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from schools.vs_schools.dev.targets import add_target_arguments, quiet, resolve_targets
from schools.vs_student_portal.management.commands.seed_student_portal_scenarios import (
    DESK_ACCOUNT,
    FEE_STRUCTURE,
    STAFF_PASSWORD,
    Command as StudentPortalSeed,
)

CAST = ("lagoon-view", "sunrise-academy")

#: The password every activated parent signs in with, long enough for the
#: password policy and the parents' counterpart of the pupils' password.
PARENT_PASSWORD = "Parent@School2026"

#: A school-wide notice and a class notice, both published to parents.
SCHOOL_NOTICE = (
    "Parents' evening on Thursday",
    "Parents and guardians are invited to meet each child's teacher on "
    "Thursday from 4pm in the school hall.",
)
CLASS_NOTICE = (
    "Permission slips for the class trip",
    "Please return the signed permission slip for the class trip to the "
    "class teacher by Friday.",
)

#: The guardians this seeder makes where the school has none to use:
#: ``(first, last, phone, email local part, relationship)``.
OKAFOR = ("Patricia", "Okafor", "08065550130", "patricia.okafor", "MOTHER")
SINGLE = ("Emeka", "Nnadi", "08065550201", "emeka.nnadi", "FATHER")
INVITED = ("Ifeoma", "Danjuma", "08065550202", "ifeoma.danjuma", "MOTHER")
TEACHER_PHONE = "08065550203"


class Command(BaseCommand):
    help = "Give parents portal accounts in every state and children to look at."

    def add_arguments(self, parser):
        add_target_arguments(parser)

    def handle(self, *args, **options):
        self.pupils = StudentPortalSeed(stdout=self.stdout, stderr=self.stderr)
        with quiet():
            for target in resolve_targets(options, CAST):
                self.mail_domain = (
                    "example.ng" if target.slug == target.recipe else "example.com"
                )
                lines = self._seed(target)
                lines[self.okafor_end:self.okafor_end] = self._transfer_accounts()
                for line in lines:
                    self.stdout.write(line)
        self.stdout.write(self._how_to_sign_in())

    def _how_to_sign_in(self) -> str:
        return (
            "\nSign in at the school's own address (<slug>.localhost, on the school "
            "app's dev port) with the email and password above, then open the "
            "parent app at /parent/. Parents take the parent password, the "
            "teacher the staff password."
        )

    # ── one school ─────────────────────────────────────────────────────────

    @transaction.atomic
    def _seed(self, target) -> list[str]:
        from schools.vs_schools.models import School

        school = School.objects.filter(slug=target.slug).first()
        if school is None:
            raise CommandError(f"No school {target.slug!r}. Run seed_onboarding_scenarios first.")
        tenant = school.tenant
        admin = self.pupils._admin(tenant)
        branches = list(tenant.branches.order_by("-is_main", "code"))
        self.slug, self.tenant, self.admin = target.slug, tenant, admin
        self.roll = self._roll(tenant)
        self.taken: set[int] = set()
        self.accounts_owed: list = []
        self.books = self._books(school)

        lines = [self.style.SUCCESS(
            f"\n  {school.name} ({school.slug}): {len(branches)} "
            f"branch{'es' if len(branches) > 1 else ''}",
        )]
        self.okafor_end = len(lines)
        if target.recipe == "lagoon-view":
            lines += self._okafor(branches)
            self.okafor_end = len(lines)  # Her children's account numbers print here.
            lines += self._teacher()
            lines += self._invited()
        else:
            lines += self._single()
        return lines

    def _skipped(self, why):
        self.stdout.write(self.style.WARNING(f"  {self.slug}: skipped, {why}."))

    # ── the roll and the books ─────────────────────────────────────────────

    def _roll(self, tenant):
        """The pupils placed in a class this year and attending, lowest id first."""
        from schools.vs_students.constants import StudentStatus
        from schools.vs_students.models import Student

        return list(
            Student.all_objects.filter(
                tenant=tenant, status=StudentStatus.ACTIVE,
                enrolments__is_active=True, enrolments__session__status="ACTIVE",
            ).select_related("branch").distinct().order_by("pk"),
        )

    def _books(self, school) -> bool:
        """Whether the school's books hold the starter fee structure, opening them if need be.

        A school with no books has them provisioned, and one whose books lack
        the starter structure is given it, both as the operator commands do.
        Books that cannot be opened mean nobody can be billed, and that is said.
        """
        from vs_finance.models import FeeStructure, LedgerEntity

        from schools.vs_schools.services.books import provision_books_for_school

        entity = (
            LedgerEntity.objects.filter(tenant=school.tenant).order_by("pk").first()
            or provision_books_for_school(school)
        )
        if entity is None:
            self._skipped("the school's books could not be opened, so nobody is billed")
            return False
        if not FeeStructure.objects.filter(
            entity=entity, code=FEE_STRUCTURE, is_active=True,
        ).exists():
            call_command("seed_finance_ar_demo", entity=entity.code, stdout=StringIO())
        return True

    # ── guardians and their children ───────────────────────────────────────

    def _guardian(self, *, first, last, phone, email):
        """The school's guardian record for this person, made through the service if new."""
        from schools.vs_students.services.guardians import upsert_guardian

        guardian, _ = upsert_guardian(
            self.tenant, first_name=first, last_name=last, phone=phone, email=email,
        )
        if (guardian.email or "").lower() != email.lower():
            raise CommandError(
                f"{self.slug}: the phone {phone} already belongs to another guardian, "
                f"so {first} {last} cannot be given it.",
            )
        return guardian

    def _linked(self, guardian) -> list:
        """The guardian's children who are placed in a class this year, lowest id first."""
        placed = {pupil.pk for pupil in self.roll}
        from schools.vs_students.models import Student

        return [
            pupil for pupil in Student.all_objects.filter(
                tenant=self.tenant, guardian_links__guardian=guardian,
            ).select_related("branch").order_by("pk")
            if pupil.pk in placed
        ]

    def _link(self, guardian, pupil, relationship):
        from schools.vs_students.services.guardians import link

        if not pupil.guardian_links.filter(guardian=guardian).exists():
            link(
                pupil, guardian, relationship=relationship, is_primary=False,
                actor=self.admin,
            )
        self.taken.add(pupil.pk)

    def _unused(self, *, not_branches=()):
        """The first pupil on the roll no seeded guardian uses, outside the given branches."""
        for pupil in self.roll:
            if pupil.pk not in self.taken and pupil.branch_id not in not_branches:
                return pupil
        raise CommandError(f"{self.slug} has too few pupils to give a parent a child.")

    def _children(self, guardian, relationship, *, wanted, branches=False) -> list:
        """The guardian's children, linking pupils from the roll until there are *wanted*.

        With *branches*, the children are at different branches: one per
        branch, taken from those already linked before any is added.
        """
        have, seen = [], set()
        for pupil in self._linked(guardian):
            if len(have) < wanted and (not branches or pupil.branch_id not in seen):
                have.append(pupil)
                seen.add(pupil.branch_id)
        self.taken.update(pupil.pk for pupil in have)
        while len(have) < wanted:
            pupil = self._unused(not_branches=seen if branches else ())
            self._link(guardian, pupil, relationship)
            have.append(pupil)
            seen.add(pupil.branch_id)
        return sorted(have, key=lambda pupil: pupil.pk)

    def _access(self, guardian, *, active):
        """Issue the guardian access if they have none, and activate it when *active*.

        Returns the guardian as the database now holds them.
        """
        from schools.vs_students.models import Guardian
        from schools.vs_students.services import guardian_portal_access as access

        guardian = Guardian.all_objects.select_related("user").get(pk=guardian.pk)
        if access.state_of(guardian) in (access.NONE, access.REVOKED):
            access.issue(guardian, actor=self.admin)
            guardian = Guardian.all_objects.select_related("user").get(pk=guardian.pk)
        if active:
            self._activate(guardian)
            guardian = Guardian.all_objects.select_related("user").get(pk=guardian.pk)
        return guardian

    def _activate(self, guardian):
        """Follow the guardian's invitation link with the parents' password."""
        from vs_user.models import User
        from vs_user.services.invitation import InvitationService

        if guardian.user.status == User.Status.ACTIVE:
            return
        _, token = InvitationService.create(user=guardian.user, invited_by=None)
        InvitationService.activate(token, PARENT_PASSWORD)

    # ── the money, the registers and the notices ───────────────────────────

    def _bill(self, pupil):
        """Bill the pupil for the term and part-pay it at the desk; the invoice, or None."""
        if not self.books:
            return None
        return self.pupils._bill_and_part_pay(self.slug, self.tenant, self.admin, pupil)

    def _settle(self, pupil, invoice):
        """Pay what the pupil still owes on the invoice, at the desk, once.

        The receipt carries a reference of its own, so a rerun finds it and
        posts nothing.
        """
        from vs_config.clock import branch_today
        from vs_finance.models import Account, Payment
        from vs_finance.receivables import post_payment

        from schools.vs_student_portal.fees import fee_summary

        if invoice is None:
            return
        reference = f"PARENT-SEED-SETTLE-{pupil.pk}"
        if Payment.objects.filter(entity=invoice.entity, reference=reference).exists():
            return
        row = next(
            (r for r in fee_summary(pupil)["invoices"] if r["invoice_ref"] == invoice.pk),
            None,
        )
        if row is None or row["balance"] <= 0:
            return
        payment = Payment.objects.create(
            entity=invoice.entity, customer=invoice.customer, branch=invoice.branch,
            payment_date=branch_today(self.tenant, pupil.branch_id), amount=row["balance"],
            method="BANK_TRANSFER",
            deposit_account=Account.objects.get(entity=invoice.entity, code=DESK_ACCOUNT),
            reference=reference, created_by=self.admin,
        )
        post_payment(payment, actor_user=self.admin, allocations=[(invoice, row["balance"])])

    def _transfer_accounts(self) -> list[str]:
        """Give each of Mrs Okafor's billed children their account number, and name it.

        Issued by the student seeder's own step, on the fake provider and
        outside the seeding transaction, as the payment engine requires; a child
        who already has an account keeps it. A parent of two then sees two
        numbers, each beside its own child's name.
        """
        from vs_payments.constants import VirtualAccountStatus
        from vs_payments.models import VirtualAccount
        from vs_payments.providers.registry import fake_provider_enabled

        owed = [(pupil, invoice) for pupil, invoice in self.accounts_owed if invoice is not None]
        if owed and not fake_provider_enabled():
            self._skipped("no transfer accounts: the fake payment provider is off here")
            return []
        lines = []
        for pupil, invoice in owed:
            self.pupils._transfer_account({"tenant": self.tenant, "invoice": invoice})
            account = (
                VirtualAccount.objects.filter(
                    customer=invoice.customer, status=VirtualAccountStatus.ACTIVE,
                ).order_by("-created_at", "-pk").first()
            )
            if account is not None:
                lines.append(
                    f"             {pupil.full_name}: transfer account "
                    f"{account.account_number} ({account.bank_name}).",
                )
        return lines

    def _balance(self, pupil):
        """What the pupil owes on open bills and charges (never net of credit), or ``None`` without books."""
        from schools.vs_student_portal.fees import fee_summary

        return fee_summary(pupil)["owing"] if self.books else None

    def _has_released_result(self, pupil) -> bool:
        from schools.vs_student_portal.results import term_choices

        return any(term["state"] == "released" for term in term_choices(pupil))

    def _notice(self, spec, audience):
        """Publish the notice once, by the school's administrator, and return it."""
        from schools.vs_notices.models import Notice
        from schools.vs_notices.services import notices as service

        title, body = spec
        notice = Notice.objects.filter(tenant=self.tenant, title=title).first()
        if notice is None:
            notice = service.create(
                self.tenant, actor=self.admin, title=title, body=body,
                audience=audience, publish_now=True,
            )
        return notice

    # ── the shapes ─────────────────────────────────────────────────────────

    def _okafor(self, branches) -> list[str]:
        first, last, phone, local, relationship = OKAFOR
        guardian = self._guardian(
            first=first, last=last, phone=phone, email=f"{local}@{self.mail_domain}",
        )
        if len(branches) < 2:
            raise CommandError(f"{self.slug} has one branch, so there is no second to give a child.")
        children = self._children(guardian, relationship, wanted=2, branches=True)
        guardian = self._access(guardian, active=True)

        invoices = {pupil.pk: self._bill(pupil) for pupil in children}
        balances = {pupil.pk: self._balance(pupil) for pupil in children}
        paid = next((p for p in children if balances[p.pk] == 0), children[-1])
        owing = next(p for p in children if p.pk != paid.pk)
        if self.books:
            self._settle(paid, invoices[paid.pk])
        self.accounts_owed = [(pupil, invoices[pupil.pk]) for pupil in children]

        shown = next((p for p in children if self._has_released_result(p)), None)
        if shown is None:
            self._skipped(f"none of {first}'s children has a released result")
        self.pupils._days_to_note(self.slug, self.tenant, self.admin, shown or owing)

        reached = self._notices(children, owing if shown is None else shown)
        lines = [
            f"    Okafor   Mrs {first} {last}: {guardian.user.email} / {PARENT_PASSWORD}.",
        ]
        for pupil in children:
            lines.append(self._child_line(
                pupil, shown=pupil is shown, reached=pupil.pk in reached,
            ))
        for pupil in self._others(guardian, children):
            lines.append(
                f"             also linked: {pupil.full_name}{self._at(pupil)}, "
                f"{pupil.get_status_display().lower()}, left as the student seeder made it.",
            )
        return lines

    def _others(self, guardian, children) -> list:
        """The guardian's other children the portal also shows, such as a suspended one."""
        from schools.vs_students.constants import PORTAL_STATUSES
        from schools.vs_students.models import Student

        return list(
            Student.all_objects.filter(
                tenant=self.tenant, guardian_links__guardian=guardian,
                status__in=PORTAL_STATUSES,
            ).exclude(pk__in=[pupil.pk for pupil in children]).select_related("branch").order_by("pk"),
        )

    def _notices(self, children, class_child) -> set[int]:
        """Publish a school-wide notice and one class notice; the children the class notice reaches."""
        from schools.vs_notices.services import notices as service

        self._notice(SCHOOL_NOTICE, [{"kind": "SCHOOL"}])
        enrolment = service.pupil_enrolment(class_child)
        notice = self._notice(
            CLASS_NOTICE, [{"kind": "CLASS", "id": enrolment.school_class_id}],
        )
        placements = service.pupil_placements(children)
        reached = {
            pupil.pk for pupil in service.children_addressed(notice, children, placements)
        }
        if len(reached) != 1:
            self._skipped(
                "the class notice reaches "
                f"{'both children' if len(reached) > 1 else 'neither child'}, "
                "so it does not tell one child's class from the other's",
            )
        return reached

    def _teacher(self) -> list[str]:
        """A class teacher who is also a guardian, on the one account she has."""
        from schools.vs_academics.models import SchoolClass
        from schools.vs_students.models import Guardian
        from schools.vs_students.services import guardian_portal_access as access
        from vs_user.models import User

        klass = (
            SchoolClass.all_objects.filter(
                tenant=self.tenant, session__status="ACTIVE",
                class_teacher__user__status=User.Status.ACTIVE,
            ).select_related("class_teacher__user").order_by("pk").first()
        )
        if klass is None:
            self._skipped("no class has an active teacher with an account")
            return []
        user = klass.class_teacher.user
        guardian = self._guardian(
            first=user.first_name, last=user.last_name, phone=TEACHER_PHONE, email=user.email,
        )
        pupil = self._children(guardian, "LEGAL_GUARDIAN", wanted=1)[0]
        guardian = self._access(guardian, active=False)
        accounts = User.objects.filter(tenant=self.tenant, email__iexact=user.email).count()
        if guardian.user_id != user.pk or accounts != 1:
            raise CommandError(
                f"{self.slug}: {user.full_name} should have one account, the one she "
                f"teaches with, but the parent portal found {accounts}.",
            )
        return [
            f"    teacher  {user.full_name}, teacher of {klass.name}, is also the guardian "
            f"of {pupil.full_name}{self._at(pupil)}: {user.email} / {STAFF_PASSWORD} "
            f"(the account used for teaching, now also a parent's; {access.state_of(guardian)}).",
        ]

    def _invited(self) -> list[str]:
        first, last, phone, local, relationship = INVITED
        guardian = self._guardian(
            first=first, last=last, phone=phone, email=f"{local}@{self.mail_domain}",
        )
        pupil = self._children(guardian, relationship, wanted=1)[0]
        guardian = self._access(guardian, active=False)
        return [
            f"    invited  Mrs {first} {last}, guardian of {pupil.full_name}{self._at(pupil)}: "
            f"{guardian.user.email}, invitation waiting, not yet signed in.",
        ]

    def _single(self) -> list[str]:
        first, last, phone, local, relationship = SINGLE
        guardian = self._guardian(
            first=first, last=last, phone=phone, email=f"{local}@{self.mail_domain}",
        )
        pupil = self._children(guardian, relationship, wanted=1)[0]
        guardian = self._access(guardian, active=True)
        self._bill(pupil)
        return [
            f"    one child  Mr {first} {last}: {guardian.user.email} / {PARENT_PASSWORD}.",
            self._child_line(pupil, shown=False, reached=False),
        ]

    # ── what each child shows ──────────────────────────────────────────────

    def _at(self, pupil) -> str:
        """The pupil's branch where the school has several, else nothing."""
        return f" ({pupil.branch.name})" if self.tenant.branches.count() > 1 else ""

    def _child_line(self, pupil, *, shown, reached) -> str:
        from schools.vs_notices.services.notices import pupil_enrolment

        balance = self._balance(pupil)
        bill = "not billed" if balance is None else (
            f"owes ₦{balance / 100:,.2f}" if balance > 0 else "fees paid in full"
        )
        enrolment = pupil_enrolment(pupil)
        return (
            f"             {pupil.full_name}{self._at(pupil)}, "
            f"{enrolment.school_class.name if enrolment else 'no class'}: {bill}"
            f"{'; result released' if shown else ''}"
            f"{'; reached by the class notice' if reached else ''}."
        )

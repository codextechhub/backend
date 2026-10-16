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

Then the pupils the portal's screens are checked against, each active:

    full       the first child of a family, chosen for the richest results the
               school has released (a whole year with its year-end block where
               there is one), with an email of their own so they can sign in
               with it, billed for the term, part-paid at the desk, and with a
               card payment started in the portal and still being confirmed;
    sibling    the next child of the same primary guardian, signing in by
               admission number only, owing nothing: the two share a phone;
    held       a pupil whose result was held when their class's results were
               published, where the school has one.

And one member of staff, so the finance settings the pupils' payments follow
can be changed end to end:

    bursar     Kemi Afolabi, the school's Finance Admin with no branch, who
               covers every branch, signing in with the staff password.

A state a school cannot be put in through its own data is said so and skipped:
sunrise-academy has no fee structure to bill from and no held result.

**Everything goes through the real services**: access is issued, activated
through the invitation token and revoked exactly as the API does it, and the
notices are drafted, published and withdrawn by the school's administrator, so
a state the services refuse fails here rather than being written in. The bill
is raised by the finance layer's cohort run, the desk payment is posted as a
bursar's receipt, and the card payment is started with the fake provider (on
only in development settings), after the rest has committed, because a payment
may not be started inside a transaction.

Idempotent: re-running leaves what already matches.

    python manage.py seed_student_portal_scenarios

Run ``seed_student_scenarios``, ``seed_school_permissions`` and
``seed_gradebook_scenarios`` (with ``seed_gradebook_history``) first. Never run
against production.
"""
from __future__ import annotations

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

CAST = ("lagoon-view", "sunrise-academy")

#: The password the activated pupil signs in with, long enough for the password policy.
PASSWORD = "Pupil@School2026"

#: The password every seeded member of staff signs in with, as the staff seeder's.
STAFF_PASSWORD = "SchoolStaff@2026"

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

#: The fee structure the full pupil is billed from, the finance module's starter one.
FEE_STRUCTURE = "FS-TERM1"

#: The bank account the desk payment is deposited into.
DESK_ACCOUNT = "1100"

#: The card payment left being confirmed, in kobo (₦5,000.00).
PENDING_CARD = 5_000_00


class Command(BaseCommand):
    help = "Give pupils portal access in every state and post notices for them to read."

    def handle(self, *args, **options):
        for slug in CAST:
            cast = self._seed(slug)
            self._pending_card(cast)
            self._report(slug, cast)
            self._bursar(cast["tenant"], cast["admin"])

    @transaction.atomic
    def _bursar(self, tenant, admin):
        """A whole-school Finance Admin, granted the way a school grants a role.

        The grant goes through the staff seeder's ``seat``: through
        :func:`vs_rbac.services.grant_role` with no branch, and where the
        administrator may not grant it outright, through the school's own
        grant approval, voted on by an approver on its live stage. A grant
        nobody could approve stops the seed rather than being written in.
        """
        from vs_rbac.models import TenantRoleTemplate, TenantUserRoleAssignment
        from vs_user.models import User

        email = f"kemi.afolabi@{tenant.slug}.example.com"
        user = User.objects.filter(tenant=tenant, email=email).first()
        if user is None:
            user = User.objects.create_user(
                email=email, password=STAFF_PASSWORD, tenant=tenant,
                first_name="Kemi", last_name="Afolabi", status=User.Status.ACTIVE,
            )
        role = TenantRoleTemplate.objects.filter(
            tenant=tenant, key__in=("finance_admin", "finance-admin"),
        ).first()
        if role is None:
            self._skipped(tenant.slug, "the school has no Finance Admin role")
            return
        held = TenantUserRoleAssignment.objects.filter(
            tenant=tenant, user=user, role=role, branch__isnull=True,
            assignment_status="ACTIVE",
        ).exists()
        if not held:
            from schools.vs_staff.management.commands.seed_staff_scenarios import (
                Command as StaffSeed,
            )

            seed = StaffSeed(stdout=self.stdout)
            if seed.seat(tenant=tenant, user=user, role=role, actor=admin) is not None:
                raise CommandError(
                    f"{tenant.slug}: the grant of Finance Admin to Kemi Afolabi is "
                    f"waiting for an approval nobody could give.",
                )
        self.stdout.write(
            f"    bursar   Kemi Afolabi (Finance Admin, every branch): {email} / {STAFF_PASSWORD}.",
        )

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

        cast = {"tenant": tenant, "admin": admin, "active": active, "invoice": None}
        family = self._family(pupils, taken=set(p.pk for p in roles.values()))
        if family is None:
            self._skipped(slug, "no two pupils on the roll share a primary guardian")
        else:
            full, sibling = family
            self._own_email(full, slug)
            for pupil in family:
                self._give_access(pupil, admin)
            cast.update(full=full, sibling=sibling)
            cast["invoice"] = self._bill_and_part_pay(slug, tenant, admin, full)
            self._days_to_note(slug, tenant, admin, full)
        held = self._held(tenant, taken=set(p.pk for p in roles.values()) | set(
            p.pk for p in family or ()
        ))
        if held is None:
            self._skipped(slug, "no pupil's result was held at publication")
        else:
            self._give_access(held, admin)
            cast["held"] = held
        return cast

    def _skipped(self, slug, why):
        self.stdout.write(self.style.WARNING(f"  {slug}: skipped, {why}."))

    def _family(self, pupils, *, taken):
        """The first two children of one primary guardian, the elder with the richest results.

        A shared phone holding two pupils is what the portal's pupil switcher is
        for, so the full pupil comes with a sibling. Families are ranked by what
        their first child's results can show: a whole released year with its
        year-end block first, then any released term, then none; the lowest id
        breaks a tie, so a rerun picks the same family.
        """
        from schools.vs_students.models import StudentGuardian

        from ...results import session_results, term_result

        children = {}
        for pupil in pupils:
            if pupil.pk in taken:
                continue
            guardian = (
                StudentGuardian.objects.filter(student=pupil, is_primary=True)
                .values_list("guardian_id", flat=True).first()
            )
            if guardian is not None:
                children.setdefault(guardian, []).append(pupil)

        def richness(kids):
            first = kids[0]
            if session_results(first)["cumulative"]:
                return 2
            return 1 if term_result(first, None)["state"] == "released" else 0

        families = [kids[:2] for kids in children.values() if len(kids) >= 2]
        if not families:
            return None
        return max(families, key=lambda kids: (richness(kids), -kids[0].pk))

    def _held(self, tenant, *, taken):
        """An on-roll pupil whose result was published held, or None."""
        from schools.vs_gradebook.constants import StoredStage
        from schools.vs_gradebook.models import StudentTermResult
        from schools.vs_students.constants import ON_ROLL

        row = (
            StudentTermResult.objects.filter(
                tenant=tenant, class_term__stage=StoredStage.PUBLISHED,
                released_at__isnull=True, snapshot__isnull=False,
                student__status__in=ON_ROLL,
            )
            .exclude(student_id__in=taken)
            .select_related("student").order_by("student_id").first()
        )
        return row.student if row is not None else None

    def _own_email(self, pupil, slug):
        """Give the full pupil an email of their own, so they can sign in with it too.

        Set before access is first issued, because the account takes its sign-in
        address from the record then. A pupil who already has an account keeps
        the address it was made with.
        """
        if pupil.email or pupil.user_id:
            return
        local = "".join(
            ch for ch in f"{pupil.first_name}.{pupil.last_name}".lower()
            if ch.isalnum() or ch == "."
        )
        pupil.email = f"{local}@{slug}.example.com"
        pupil.save(update_fields=["email", "updated_at"])

    def _give_access(self, pupil, admin):
        """Issue access where the pupil has none (or had it taken away), then activate it."""
        from schools.vs_students.services import portal_access

        if portal_access.state_of(pupil) in ("none", "revoked"):
            self._ensure_number(pupil)
            portal_access.issue(pupil, actor=admin)
        self._activate(pupil)
        pupil.refresh_from_db()

    def _bill_and_part_pay(self, slug, tenant, admin, pupil):
        """Bill the pupil for the term and post a bursar's receipt for part of it.

        The bill comes from the finance layer's cohort run, as a school's own fee
        run raises it, which skips a pupil already billed for the term. The
        receipt is posted against that bill and carries a reference of its own,
        so a rerun finds it and posts nothing. Returns the bill.
        """
        from schools.core.fal import registry
        from schools.vs_gradebook.services.resolve import current_term
        from vs_config.clock import branch_today
        from vs_finance.models import Account, FeeStructure, Invoice, LedgerEntity, Payment
        from vs_finance.receivables import post_payment

        from ...fees import fee_summary

        entity = LedgerEntity.objects.filter(tenant=tenant).first()
        structure = (
            FeeStructure.objects.filter(entity=entity, code=FEE_STRUCTURE, is_active=True).first()
            if entity is not None else None
        )
        if structure is None or not structure.items.exists():
            self._skipped(slug, f"the books have no {FEE_STRUCTURE} fee structure to bill from")
            return None
        today = branch_today(tenant, pupil.branch_id)
        term = current_term(tenant, today)
        bridge = registry.get_fee_term_bridge()
        bridge.link_term(structure.pk, term.session_id, term.pk).unwrap()
        bridge.generate_cohort_invoices(
            structure.pk, (str(pupil.pk),), raiser_ref=admin.pk,
        ).unwrap()

        bill = next(iter(fee_summary(pupil)["invoices"]), None)
        if bill is None:
            self._skipped(slug, f"the cohort run raised no bill for {pupil.full_name}")
            return None
        invoice = Invoice.objects.select_related("customer").get(pk=bill["invoice_ref"])
        reference = f"PORTAL-SEED-DESK-{pupil.pk}"
        if not Payment.objects.filter(entity=entity, reference=reference).exists():
            amount = max(1_000_00, bill["total"] * 2 // 5 // 1_000_00 * 1_000_00)
            payment = Payment.objects.create(
                entity=entity, customer=invoice.customer, branch=invoice.branch,
                payment_date=today, amount=amount, method="BANK_TRANSFER",
                deposit_account=Account.objects.get(entity=entity, code=DESK_ACCOUNT),
                reference=reference, created_by=admin,
            )
            post_payment(payment, actor_user=admin, allocations=[(invoice, amount)])
        return invoice

    def _days_to_note(self, slug, tenant, admin, pupil):
        """Give the full pupil an absent, a late and an excused day this term.

        Taken as a school takes a register: on three of the term's past school
        days that have no register yet, the class is marked present with the
        pupil absent (with a note), late (with an arrival time and a note) and
        excused (with a reason and a note), and each register is submitted. A
        pupil who already has a day worth a note is left alone, so a rerun
        takes no register; a term without three such days is skipped.
        """
        import datetime as dt

        from schools.vs_attendance.constants import Mark
        from schools.vs_attendance.models import ReasonCode
        from schools.vs_attendance.services import marks
        from schools.vs_attendance.services.reasons import ensure_default_reason_codes
        from schools.vs_attendance.services.registers import load_context
        from schools.vs_attendance.services.student_tab import pupil_attendance

        body = pupil_attendance(tenant, pupil)
        if body["notes"]:
            return
        today = body["today"]["date"] if body["today"] else None
        open_days = [
            row["date"] for row in body["days"]
            if row["state"] == "NOT_TAKEN" and (today is None or row["date"] < today)
        ]
        enrolment = pupil.enrolments.filter(is_active=True, session__status="ACTIVE").first()
        if len(open_days) < 3 or enrolment is None:
            self._skipped(slug, f"{pupil.full_name} has no three untaken school days this term")
            return
        ensure_default_reason_codes(tenant)
        # The first live reason that needs no note of its own ("Sick" by default).
        reason = (
            ReasonCode.objects.filter(tenant=tenant, retired_at__isnull=True, requires_note=False)
            .order_by("order_index", "pk").first()
        )
        plan = [
            (open_days[-3], {"mark": Mark.ABSENT, "note": "Unwell. A parent phoned the school."}),
            (open_days[-2], {"mark": Mark.LATE, "arrived_at": dt.time(8, 20), "note": "The school bus was late."}),
            (open_days[-1], {"mark": Mark.EXCUSED, "reason_code_id": reason.pk, "note": "Sent home with a fever."}),
        ]
        class_id = enrolment.school_class_id
        for day, spec in plan:
            marks.set_mark(load_context(tenant, admin, class_id, day), admin, pupil.pk, **spec)
            marks.fill_present(load_context(tenant, admin, class_id, day), admin)
            marks.submit(load_context(tenant, admin, class_id, day), admin)

    def _pending_card(self, cast):
        """Start a card payment in the portal for the full pupil and leave it unconfirmed.

        Started with the fake provider, which never confirms on its own, and
        outside the seeding transaction, as the payment engine requires. A
        rerun finds the pending payment and starts no second one.
        """
        from vs_payments.constants import CollectionOrigin, CollectionStatus
        from vs_payments.models import CollectionIntent
        from vs_payments.services import initiate_collection

        invoice, pupil = cast["invoice"], cast.get("full")
        if invoice is None or pupil is None:
            return
        if CollectionIntent.objects.filter(
            invoice=invoice, origin=CollectionOrigin.PORTAL,
            status__in=(CollectionStatus.PENDING, CollectionStatus.PROCESSING),
        ).exists():
            return
        initiate_collection(
            entity=invoice.entity, amount=PENDING_CARD, customer=invoice.customer,
            invoice=invoice, provider="FAKE",
            payer_email=pupil.user.email or invoice.customer.billing_email or "",
            payer_name=pupil.full_name, actor_user=pupil.user,
            origin=CollectionOrigin.PORTAL,
        )

    def _report(self, slug, cast):
        """Say who to sign in as and what each pupil's portal shows, read through the portal itself."""
        from ...fees import fee_summary, payment_history
        from ...results import session_results, term_result
        from ...services import timetable

        for role in ("active", "full", "sibling", "held"):
            pupil = cast.get(role)
            if pupil is None:
                continue
            pupil.refresh_from_db()
            sign_in = pupil.student_number + (f" or {pupil.user.email}" if pupil.user.email else "")
            result = term_result(pupil, None)["state"]
            year = session_results(pupil)
            fees = fee_summary(pupil)
            payments = payment_history(pupil)
            self.stdout.write(
                f"    {role:8} {pupil.full_name}: {sign_in} / {PASSWORD}. "
                f"Results {result}"
                f"{', year-end block' if year.get('cumulative') else ''}; "
                f"timetable {'published' if timetable(pupil)['published'] else 'unpublished'}; "
                f"balance ₦{fees['balance'] / 100:,.2f}; "
                f"{sum(p['status'] == 'CONFIRMED' for p in payments)} confirmed and "
                f"{sum(p['status'] != 'CONFIRMED' for p in payments)} pending payments.",
            )

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

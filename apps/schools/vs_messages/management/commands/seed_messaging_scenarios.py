"""Put the messaging module into every state, so each screen has something to show.

    lagoon-view   Two branches and live, with parents and pupils who can sign in.
    holy-cross    Two branches and live, with staff only: no family or pupil has
                  an account there, so those two scenarios are said and skipped.

At each school, from the people already there:

    direct     two members of staff who can message each other, with the last
               message still unread by one of them;
    group      a staff group owned by a branch leader, a member added after it
               had been talking (who reads only what is said from then on) and
               a message the late member can see;
    broadcast  a broadcast that takes private replies, one recipient having
               replied to its sender, and a one-way broadcast;
    family     a parent and the class teacher (or whoever the school lets reach
               that child), with a message the parent has not read;
    pupil      a pupil and a member of staff who may reach them, which the
               pupil's primary guardian can read but not answer, and which a
               leader holding the welfare key can inspect.

The leaders are never made members of what they inspect, so the oversight
screens have conversations to open.

**Everything goes through the real services**: a conversation is started,
written in, joined and replied to exactly as the API does it, so a state the
rules refuse fails here and is said, not written in. Alerts are held back
(``alerts_suppressed``), so seeding wakes no device and sends no email.

Idempotent: a conversation is found again by its pair or by the token of its
first message, and its scripted messages are written only once.

    python manage.py seed_messaging_scenarios

Run ``seed_parent_portal_scenarios`` and ``seed_student_portal_scenarios``
first. Never run against production.
"""
from __future__ import annotations

import uuid

from django.core.management.base import BaseCommand, CommandError

from schools.vs_messages.constants import (
    PERM_BROADCAST_SEND,
    PERM_GROUP_CREATE,
    PERM_SEND,
    PERM_VIEW,
    PERM_WELFARE_INSPECT,
    SURFACE_PARENT,
    SURFACE_STAFF,
    SURFACE_STUDENT,
    ConversationKind,
    ReplyMode,
)
from schools.vs_messages.exceptions import MessagesError
from schools.vs_messages.models import Conversation, Message
from schools.vs_messages.services import (
    broadcasts,
    config,
    conversations,
    groups,
    messages,
    people,
    visibility,
)
from schools.vs_messages.tasks import alerts_suppressed
from schools.vs_schools.dev.targets import add_target_arguments, quiet, resolve_targets

CAST = ("lagoon-view", "holy-cross")

#: The passwords the other development seeders give their accounts, tried in
#: this order to say how each person signs in.
KNOWN_PASSWORDS = (
    "School@2025", "SchoolStaff@2026", "Parent@School2026", "Pupil@School2026",
)

GROUP_NAME = "Staff room"
PRIVATE_BROADCAST = "Staff meeting on Friday"
ONE_WAY_BROADCAST = "Reminder: submit your scheme of work"
_NAMESPACE = uuid.UUID("5e1bb6d0-4f4e-4a53-b1a1-6d0f5f3c9a10")


def _token(slug: str, name: str) -> uuid.UUID:
    """A fixed token per school and scenario, so a rerun finds the conversation it made."""
    return uuid.uuid5(_NAMESPACE, f"{slug}:{name}")


class Command(BaseCommand):
    help = "Seed staff, family and pupil conversations, groups and broadcasts for development."

    def add_arguments(self, parser):
        add_target_arguments(parser)

    def handle(self, *args, **options):
        with quiet(), alerts_suppressed():
            for target in resolve_targets(options, CAST):
                for line in self._seed(target.slug):
                    self.stdout.write(line)

    # ── one school ─────────────────────────────────────────────────────────

    def _seed(self, slug) -> list[str]:
        from schools.vs_schools.models import School

        school = School.objects.select_related("tenant").filter(slug=slug).first()
        if school is None:
            raise CommandError(f"No school {slug!r}. Run seed_onboarding_scenarios first.")
        self.slug, self.tenant = slug, school.tenant
        self.settings = config.load_settings(self.tenant)
        self.cast: list[tuple] = []
        self.said: list[str] = []
        self.accounts = {}

        self.staff = self._staff()
        self.leader = self._holder(PERM_WELFARE_INSPECT)
        self.writers = {u.pk for u in self._holders(PERM_SEND)}
        sends_broadcasts = {u.pk for u in self._holders(PERM_BROADCAST_SEND)}
        creators = [
            u for u in self._holders(PERM_GROUP_CREATE)
            if u.pk in self.staff and u.pk in sends_broadcasts and u.pk in self.writers
        ]
        self.owner = next((u for u in creators if u != self.leader), None) or (
            creators[0] if creators else None
        )

        self._direct()
        self._group()
        self._broadcasts()
        self._family()
        self._pupil()
        return self._report(school)

    def _skip(self, scenario, why):
        self.said.append(f"  {scenario}: skipped, {why}.")

    # ── finding people ─────────────────────────────────────────────────────

    def _staff(self) -> dict:
        """``{user id: user}`` of current staff who can take part in messaging."""
        ids = set(
            people.key_holders(self.tenant, PERM_VIEW).filter(
                pk__in=people.current_staff_user_ids(self.tenant),
            ).values_list("pk", flat=True)
        )
        from vs_user.models import User

        return {
            u.pk: u for u in User.objects.filter(pk__in=ids)
            .select_related("staff_profile").order_by("pk")
        }

    def _holders(self, key) -> list:
        from vs_user.models import User

        return list(User.objects.filter(pk__in=people.key_holders(self.tenant, key)).order_by("pk"))

    def _holder(self, key):
        """The first holder of *key*, preferring the school's administrator account."""
        holders = self._holders(key)
        admin = f"admin@{self.slug}.example.com"
        return next((u for u in holders if u.email == admin), None) or (holders[0] if holders else None)

    def _contacts_of(self, user) -> list:
        """The staff *user* may address, as users, lowest id first."""
        return [
            p.user for p in people.staff_contacts(self.tenant, user).order_by("user_id")
        ]

    def _writer(self, who):
        """Whether *who* may write in staff conversations."""
        return who.pk in self.writers

    def _say(self, conversation, user, text, surface=SURFACE_STAFF):
        access = visibility.resolve(self.tenant, user, conversation.pk, surface=surface)
        message, _ = messages.send_message(access, sender=user, body=text)
        return message

    def _read(self, conversation, user, surface=SURFACE_STAFF):
        messages.mark_read(
            visibility.resolve(self.tenant, user, conversation.pk, surface=surface), user,
        )

    def _unscripted(self, conversation) -> bool:
        return not Message.objects.filter(conversation=conversation).exists()

    def _people(self, user, role, where):
        self.cast.append((role, user, where))
        return user

    # ── the scenarios ──────────────────────────────────────────────────────

    def _direct(self):
        for a in self.staff.values():
            if a == self.leader:
                continue
            for b in self._contacts_of(a):
                if b == self.leader or not self._writer(a) or not self._writer(b):
                    continue
                try:
                    conversation, _, _ = conversations.start_staff_direct(
                        self.tenant, a, recipient_id=b.pk,
                    )
                except MessagesError:
                    continue
                if self._unscripted(conversation):
                    self._say(conversation, a, "Good morning. Have you finished the mid-term marks yet?")
                    self._say(conversation, b, "Almost. I will send them across before closing.")
                    self._read(conversation, a)
                    self._say(conversation, b, "Done, they are in the shared folder now.")
                self._people(a, "staff conversation, one message unread", "school app, Messages")
                self._people(b, "staff conversation, wrote the last message", "school app, Messages")
                return
        self._skip("direct", "no two members of staff can message each other")

    def _group(self):
        owner = self.owner
        if owner is None:
            return self._skip("group", "no member of staff may create groups")
        contacts = [u for u in self._contacts_of(owner) if u != self.leader and self._writer(u)]
        if len(contacts) < 2:
            return self._skip("group", "the group owner reaches fewer than two other staff")
        members, late = contacts[:2], (contacts[2] if len(contacts) > 2 else None)
        branch = owner.staff_profile.branch_id
        try:
            group, created = groups.create_group(
                self.tenant, owner, name=GROUP_NAME, branch_id=branch,
                member_ids=[m.pk for m in members],
                body="Welcome to the staff room. Timetable changes will be posted here.",
                client_token=_token(self.slug, GROUP_NAME),
            )
            if created:
                self._say(group, members[0], "Thanks. Is the Friday assembly still on?")
                self._say(group, owner, "Yes, assembly is on at 8am.")
                if late is not None:
                    groups.add_members(groups.group_access(self.tenant, owner, group.pk), owner, [late.pk])
                    self._say(group, owner, f"{late.first_name}, welcome. You see messages from here on.")
        except MessagesError as exc:
            return self._skip("group", exc.message)
        self._people(owner, "group owner", "school app, Messages")
        self._people(members[0], "group member (sees everything)", "school app, Messages")
        if late is not None:
            self._people(late, "group member added later (sees only after joining)", "school app, Messages")

    def _broadcasts(self):
        owner = self.owner
        if owner is None:
            return self._skip("broadcast", "no member of staff may send broadcasts")
        recipients = [u for u in self._contacts_of(owner) if u != self.leader and self._writer(u)]
        if len(recipients) < 2:
            return self._skip("broadcast", "the sender reaches fewer than two other staff")
        branch = owner.staff_profile.branch_id
        try:
            private, created = broadcasts.send_broadcast(
                self.tenant, owner, title=PRIVATE_BROADCAST,
                body="Staff meeting on Friday at 2pm in the library. Please reply if you cannot come.",
                recipient_ids=[r.pk for r in recipients[:3]], branch_id=branch,
                reply_mode=ReplyMode.PRIVATE_REPLIES,
                client_token=_token(self.slug, PRIVATE_BROADCAST),
            )
            if created:
                broadcasts.reply_privately(
                    self.tenant, recipients[0], private.pk,
                    body="I will be on duty then. Can I send my notes instead?",
                )
            broadcasts.send_broadcast(
                self.tenant, owner, title=ONE_WAY_BROADCAST,
                body="Submit your scheme of work for next term by Monday.",
                recipient_ids=[r.pk for r in recipients[:3]], branch_id=branch,
                reply_mode=ReplyMode.ONE_WAY,
                client_token=_token(self.slug, ONE_WAY_BROADCAST),
            )
        except MessagesError as exc:
            return self._skip("broadcast", exc.message)
        self._people(owner, "broadcast sender (reads the reply)", "school app, Messages")
        self._people(recipients[0], "broadcast recipient who replied", "school app, Messages")
        self._people(recipients[1], "broadcast recipient (cannot see the reply)", "school app, Messages")

    def _guardians(self):
        """Guardians with a working account, Mrs Patricia Okafor first."""
        from schools.vs_students.models import Guardian

        rows = list(
            Guardian.objects.filter(
                tenant=self.tenant, user__isnull=False, user__is_active=True,
            ).select_related("user").order_by("pk")
        )
        rows.sort(key=lambda g: g.user.email != "patricia.okafor@example.ng")
        return rows

    def _reaching(self, child):
        """The member of staff who answers for *child* and the label the family reads."""
        reachable = people.staff_reachable_for_child(self.tenant, child, self.settings)
        ranked = sorted(
            reachable.items(), key=lambda kv: (not kv[1].startswith("Class teacher"), kv[0]),
        )
        for user_id, label in ranked:
            user = self.staff.get(user_id)
            if user is not None and user != self.leader:
                return user, label
        return None, ""

    def _family(self):
        from schools.vs_students.constants import PORTAL_STATUSES
        from schools.vs_students.models import Student

        for guardian in self._guardians():
            children = Student.objects.filter(
                tenant=self.tenant, status__in=PORTAL_STATUSES,
                guardian_links__guardian=guardian,
            ).order_by("pk")
            for child in children:
                staff, label = self._reaching(child)
                if staff is None:
                    continue
                try:
                    conversation, _, _ = conversations.start_family_from_staff(
                        self.tenant, staff, kind=ConversationKind.FAMILY,
                        student_id=child.pk, guardian_id=guardian.pk,
                    )
                except MessagesError:
                    continue
                if self._unscripted(conversation):
                    self._say(conversation, staff,
                              f"Good afternoon. {child.first_name} did very well in this week's test.")
                    self._say(conversation, guardian.user,
                              "Thank you, that is good to hear.", surface=SURFACE_PARENT)
                    self._read(conversation, staff)
                    self._say(conversation, staff,
                              "Please remember the parents' meeting on Thursday.")
                self._people(staff, f"teacher in a family conversation ({label})", "school app, Messages")
                self._people(guardian.user, f"parent of {child.first_name}, one message unread",
                             "parent app, Messages")
                return
        self._skip("family", "no parent with an account has a child a member of staff may reach")

    def _pupil(self):
        from schools.vs_students.constants import PORTAL_STATUSES
        from schools.vs_students.models import Student, StudentGuardian

        pupils = list(
            Student.objects.filter(
                tenant=self.tenant, status__in=PORTAL_STATUSES,
                user__isnull=False, user__is_active=True,
            ).select_related("user").order_by("pk")
        )
        watched = set(
            StudentGuardian.objects.filter(
                tenant=self.tenant, is_primary=True, guardian__user__isnull=False,
                guardian__user__is_active=True, student__in=pupils,
            ).values_list("student_id", flat=True)
        )
        pupils.sort(key=lambda p: p.pk not in watched)
        for pupil in pupils:
            staff, label = self._reaching(pupil)
            if staff is None:
                continue
            try:
                conversation, _, _ = conversations.start_family_from_staff(
                    self.tenant, staff, kind=ConversationKind.PUPIL, student_id=pupil.pk,
                )
            except MessagesError:
                continue
            if self._unscripted(conversation):
                self._say(conversation, staff, "Hello. Please bring your project to class on Monday.")
                self._say(conversation, pupil.user, "Yes sir, I will.", surface=SURFACE_STUDENT)
                self._read(conversation, staff)
                self._say(conversation, staff, "Thank you. See you then.")
            self._people(staff, f"teacher in a pupil conversation ({label})", "school app, Messages")
            self._people(pupil.user, f"pupil {pupil.first_name}, one message unread",
                         "student app, Messages")
            primary = StudentGuardian.objects.filter(
                student=pupil, is_primary=True, guardian__user__isnull=False,
            ).select_related("guardian__user").first()
            if primary is not None:
                self._people(primary.guardian.user,
                             f"primary guardian of {pupil.first_name} (reads it, cannot reply)",
                             "parent app, Messages")
            return
        self._skip("pupil", "no pupil with an account has a member of staff who may reach them")

    # ── what to show ───────────────────────────────────────────────────────

    def _how_to_sign_in(self, user) -> str:
        for password in KNOWN_PASSWORDS:
            if user.check_password(password):
                return password
        return "(password not known to this seeder)"

    def _identity(self, user) -> str:
        """The address a person signs in with: their email, else a pupil's admission number."""
        if user.email:
            return user.email
        from schools.vs_students.models import Student

        pupil = Student.objects.filter(tenant=self.tenant, user=user).first()
        return f"admission number {pupil.student_number}" if pupil else f"user {user.pk}"

    def _report(self, school) -> list[str]:
        lines = [self.style.SUCCESS(f"\n  {school.name} ({school.slug})")]
        lines += self.said
        seen = set()
        if self.leader is not None:
            self.cast.append((
                "leader: inspects groups, broadcasts and pupil conversations, exports",
                self.leader, "school app, Messages > Oversight",
            ))
        for role, user, where in self.cast:
            key = (user.pk, role)
            if key in seen:
                continue
            seen.add(key)
            lines.append(f"    {self._identity(user)} / {self._how_to_sign_in(user)}")
            lines.append(f"        {where}: {role}")
        n = Conversation.objects.filter(tenant=self.tenant).count()
        lines.append(f"    ({n} conversations at this school)")
        lines.append(
            "    Sign in at the school's own address (<slug>.localhost, on the school app's dev "
            "port); parents and pupils then open /parent/ or /student/."
        )
        return lines

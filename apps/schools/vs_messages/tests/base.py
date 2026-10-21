"""The school shapes every messaging test stands on.

Brightfield has two branches (Lekki and Ikeja); Sunrise has one, so the branch
dimension recedes there. At Brightfield:

* ``admin`` reaches the whole school and holds every messaging key; the Lekki
  head holds the same keys pinned to Lekki.
* Mrs. Funke Adeyemi leads JSS1 B at Lekki. Mr. Ibrahim Musa teaches Mathematics
  to JSS1 B. Mrs. Tola Bankole leads JSS1 C at Ikeja. Mrs. Ngozi Okoro is the
  Lekki school office and Mr. Segun Ola the Ikeja one. Mr. Yinka Reader holds
  the view key and nothing else.
* Tunde Bello (JSS1 B, Lekki) has a pupil account and two guardians with
  accounts: his mother Mrs. Funke Bello (not the primary) and his father
  Mr. Kunle Bello (primary). Chiamaka Eze (JSS1 A, Lekki) has a pupil account
  and a guardian with an account. Kemi Pupil is at Ikeja.

At Sunrise, Mrs. Ada Sunday leads a class that Bola Ade attends; his father
Mr. Bayo Ade has an account.
"""
from __future__ import annotations

import datetime as dt

from schools.vs_messages.constants import (
    PERM_BROADCAST_INSPECT,
    PERM_BROADCAST_SEND,
    PERM_EXPORT,
    PERM_FAMILY_OFFICE,
    PERM_FAMILY_SEND,
    PERM_GROUP_CREATE,
    PERM_GROUP_INSPECT,
    PERM_SEND,
    PERM_VIEW,
    PERM_WELFARE_INSPECT,
)
from schools.vs_messages.services import (
    broadcasts,
    conversations,
    groups,
    messages,
    visibility,
)
from schools.vs_students.constants import Relationship
from schools.vs_students.models import Guardian, Student
from schools.vs_students.services import portal_access
from schools.vs_students.tests.base import StudentsFixture
from vs_rbac.models import PermissionScope
from vs_rbac.tests.helpers import (
    make_assignment,
    make_permission,
    make_role,
    make_role_permission,
    make_school_admin,
)
from vs_user.models import User

ALL_MESSAGE_KEYS = (
    PERM_VIEW, PERM_SEND, PERM_EXPORT, PERM_FAMILY_SEND, PERM_FAMILY_OFFICE,
    PERM_GROUP_CREATE, PERM_BROADCAST_SEND, PERM_GROUP_INSPECT,
    PERM_BROADCAST_INSPECT, PERM_WELFARE_INSPECT,
)
TEACHER_KEYS = (PERM_VIEW, PERM_SEND, PERM_FAMILY_SEND)
OFFICE_KEYS = (PERM_VIEW, PERM_SEND, PERM_FAMILY_SEND, PERM_FAMILY_OFFICE)


def audit_events(action):
    """The audit events of one messaging action type."""
    from vs_audit.models import AuditEvent

    return AuditEvent.objects.filter(module_key="MESSAGES", action_type=action)


class MessagesFixture(StudentsFixture):
    """Builds Brightfield, Sunrise, their staff, pupils, guardians and conversations."""
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        from schools.vs_academics.models import (
            Level,
            Program,
            SchoolClass,
            Subject,
            SubjectOffering,
        )
        from schools.vs_staff.constants import TeachingPart
        from schools.vs_staff.models import TeachingAssignment

        cls.keys = {
            key: make_permission(key, scope=PermissionScope.TENANT) for key in ALL_MESSAGE_KEYS
        }
        for role in (cls.role, cls.solo_role):
            for key in ALL_MESSAGE_KEYS:
                make_role_permission(role, cls.keys[key])

        cls.teacher_role = make_role(cls.school, name="Teacher", key="teacher")
        cls.office_role = make_role(cls.school, name="School office", key="office")
        cls.reader_role = make_role(cls.school, name="Reader", key="reader")
        for key in TEACHER_KEYS:
            make_role_permission(cls.teacher_role, cls.keys[key])
        for key in OFFICE_KEYS:
            make_role_permission(cls.office_role, cls.keys[key])
        make_role_permission(cls.reader_role, cls.keys[PERM_VIEW])

        # Everyone who can be messaged is staff: an account with a staff record.
        cls.admin_staff = cls.make_staff(cls.admin, cls.school, None, "Proprietor")
        cls.head_staff = cls.make_staff(cls.lekki_head, cls.school, cls.lekki, "Branch head")
        cls.funke = cls.person(
            "funke@brightfield.test", "Funke", "Adeyemi", cls.teacher_role, cls.lekki, "Class teacher",
        )
        cls.ibrahim = cls.person(
            "ibrahim@brightfield.test", "Ibrahim", "Musa", cls.teacher_role, cls.lekki, "Maths teacher",
        )
        cls.tola = cls.person(
            "tola@brightfield.test", "Tola", "Bankole", cls.teacher_role, cls.ikeja, "Class teacher",
        )
        cls.ngozi = cls.person(
            "ngozi@brightfield.test", "Ngozi", "Okoro", cls.office_role, cls.lekki, "Bursar",
        )
        cls.segun = cls.person(
            "segun@brightfield.test", "Segun", "Ola", cls.office_role, cls.ikeja, "Secretary",
        )
        cls.reader = cls.person(
            "yinka@brightfield.test", "Yinka", "Reader", cls.reader_role, cls.lekki, "Clerk",
        )
        cls.lekki_class.class_teacher = cls.funke.staff_profile
        cls.lekki_class.save(update_fields=["class_teacher"])
        cls.ikeja_class.class_teacher = cls.tola.staff_profile
        cls.ikeja_class.save(update_fields=["class_teacher"])

        cls.maths = Subject.all_objects.create(tenant=cls.tenant, name="Mathematics", code="MTH")
        SubjectOffering.all_objects.create(tenant=cls.tenant, subject=cls.maths, level=cls.jss1)
        TeachingAssignment.all_objects.create(
            tenant=cls.tenant, staff=cls.ibrahim.staff_profile, school_class=cls.lekki_class,
            subject=cls.maths, session=cls.year, part=TeachingPart.LEAD,
        )

        # Pupils with portal accounts, and their guardians with accounts.
        cls.tunde = cls.pupil("Tunde", "Bello", "0143", cls.lekki, cls.lekki_class)
        cls.chiamaka = cls.pupil("Chiamaka", "Eze", "0142", cls.lekki, cls.shared_class)
        cls.kemi = cls.pupil("Kemi", "Pupil", "0200", cls.ikeja, cls.ikeja_class)
        cls.mother = cls.parent("funke.bello@example.ng", "Funke", "Bello")
        cls.father = cls.parent("kunle.bello@example.ng", "Kunle", "Bello")
        cls.eze = cls.parent("ada.eze@example.ng", "Ada", "Eze")
        cls.link(cls, cls.tunde, cls.mother, primary=False, relationship=Relationship.MOTHER)
        cls.link(cls, cls.tunde, cls.father, primary=True, relationship=Relationship.FATHER)
        cls.link(cls, cls.chiamaka, cls.eze, primary=True, relationship=Relationship.MOTHER)

        # Sunrise: one branch.
        cls.solo_role_teacher = make_role(cls.solo, name="Teacher", key="teacher")
        for key in TEACHER_KEYS:
            make_role_permission(cls.solo_role_teacher, cls.keys[key])
        cls.solo_head_staff = cls.make_staff(cls.solo_admin, cls.solo, None, "Head")
        cls.ada = cls.person(
            "ada@sunrise.test", "Ada", "Sunday", cls.solo_role_teacher, cls.solo_branch,
            "Class teacher", school=cls.solo,
        )
        cls.bola = cls.student(
            cls, first="Bola", last="Ade", number="SUN/0001",
            tenant=cls.solo.tenant, branch=cls.solo_branch,
        )
        cls.bayo = cls.parent("bayo.ade@example.ng", "Bayo", "Ade", tenant=cls.solo.tenant)
        cls.link(cls, cls.bola, cls.bayo, relationship=Relationship.FATHER)
        program = Program.all_objects.create(tenant=cls.solo.tenant, name="Primary", code="PRY")
        level = Level.all_objects.create(
            tenant=cls.solo.tenant, program=program, session=cls.solo_year,
            name="P1", code="P1", order_index=1,
        )
        cls.solo_class = SchoolClass.all_objects.create(
            tenant=cls.solo.tenant, level=level, session=cls.solo_year, name="P1 A",
            code="P1A", arm="A", capacity=30, branch=None,
            class_teacher=cls.ada.staff_profile,
        )
        cls.place(cls, cls.bola, cls.solo_class, session=cls.solo_year)
        cls.bola_user = make_school_admin(
            None, email="bola@sunrise.test", tenant=cls.solo.tenant, first_name="Bola", last_name="Ade",
        )
        Student.all_objects.filter(pk=cls.bola.pk).update(user=cls.bola_user)
        cls.bola.refresh_from_db()

    # ── people ──

    @classmethod
    def make_staff(cls, user, school, branch, title):
        from schools.vs_staff.models import StaffProfile

        return StaffProfile.all_objects.create(
            tenant=school.tenant, user=user, branch=branch, job_title=title,
            hire_date=dt.date(2021, 9, 6),
        )

    @classmethod
    def person(cls, email, first, last, role, branch, title, *, school=None):
        school = school or cls.school
        user = make_school_admin(
            None, email=email, tenant=school.tenant, first_name=first, last_name=last,
        )
        make_assignment(school, user, role, branch=branch)
        cls.make_staff(user, school, branch, title)
        return User.objects.select_related("staff_profile").get(pk=user.pk)

    @classmethod
    def pupil(cls, first, last, number, branch, school_class):
        pupil = cls.student(
            cls, first=first, last=last, number=f"BFS/2025/{number}", branch=branch,
            email=f"{first.lower()}@example.ng",
        )
        cls.place(cls, pupil, school_class)
        portal_access.issue(pupil, actor=cls.admin)
        pupil.refresh_from_db()
        User.objects.filter(pk=pupil.user_id).update(status=User.Status.ACTIVE, is_active=True)
        pupil.user.refresh_from_db()
        return pupil

    @classmethod
    def parent(cls, email, first, last, *, tenant=None):
        user = make_school_admin(
            None, email=email, tenant=tenant or cls.tenant, first_name=first, last_name=last,
        )
        return Guardian.all_objects.create(
            tenant=tenant or cls.tenant, first_name=first, last_name=last,
            full_name=f"{first} {last}", phone="08110000000", email=email, user=user,
        )

    # ── conversations ──

    @classmethod
    def direct(cls, a, b, body="Good morning"):
        conversation, _, _ = conversations.start_staff_direct(
            cls.tenant, a, recipient_id=b.pk, body=body,
        )
        return conversation

    @classmethod
    def group(cls, owner, members, name="Maths team", body="Welcome", branch=...):
        """A group the owner makes, for their own branch unless *branch* says otherwise."""
        if branch is ...:
            branch = owner.staff_profile.branch
        group, _ = groups.create_group(
            cls.tenant, owner, name=name, branch_id=branch.pk if branch else None,
            member_ids=[m.pk for m in members], body=body,
        )
        return group

    @classmethod
    def broadcast(cls, sender, recipients, title="Staff meeting", body="Friday at 2pm",
                  reply_mode="PRIVATE_REPLIES", branch=...):
        """A broadcast for the sender's own branch unless *branch* says otherwise."""
        if branch is ...:
            branch = sender.staff_profile.branch
        broadcast, _ = broadcasts.send_broadcast(
            cls.tenant, sender, title=title, body=body, branch_id=branch.pk if branch else None,
            recipient_ids=[r.pk for r in recipients], reply_mode=reply_mode,
        )
        return broadcast

    @classmethod
    def say(cls, conversation, user, text):
        """Store *text* as *user*'s message, as the services do for a sender already checked."""
        message, _ = messages.post_message(conversation, user, text)
        return message

    @classmethod
    def family_chat(cls, staff, child=None, guardian=None, body="Good morning"):
        child = child or cls.tunde
        guardian = guardian or cls.mother
        conversation, _, _ = conversations.start_family_from_staff(
            cls.tenant, staff, kind="FAMILY", student_id=child.pk, guardian_id=guardian.pk,
            body=body,
        )
        return conversation

    @classmethod
    def pupil_chat(cls, staff, child=None, body="Good morning"):
        child = child or cls.tunde
        conversation, _, _ = conversations.start_family_from_staff(
            cls.tenant, staff, kind="PUPIL", student_id=child.pk, body=body,
        )
        return conversation

    def access(self, user, conversation, surface="staff"):
        return visibility.resolve(self.tenant, user, conversation.pk, surface=surface)

    # ── HTTP ──

    def results(self, response):
        return response.data["data"]

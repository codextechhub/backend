"""Who reads the reason a pupil's standing changed.

The reason is free text a member of staff writes about a child, so it is a
registered field of ``school.students`` and sensitive: a role reads it only
where the school has turned the Read switch on. It reaches a client in three
places, and all are held to the same switch - the ``suspension`` block of a
profile, where the key is left out rather than sent empty, the rows of the
status history, and the status entries of the record history tab. The reason
an applicant moved between admission stages is held to the same switch, on
the record history tab's entries for those moves.

Everything else about the suspension answers either way. When a pupil is
expected back is the fact a register needs, and withholding it would stop a
class teacher knowing whether to mark a child absent.

What the pupil's family is told is a separate decision, taken once per
suspension by the person suspending the pupil (``send_reason``). The two do not
constrain each other in either direction, which is the pair of cases
:class:`ReasonOnScreenAndInTheNoticeTests` pins.

Both shapes of school: Brightfield runs two branches and Sunrise runs one. A
switch is held by a role, and a role belongs to one school, so Sunrise reads
nothing from a switch Brightfield turned on.
"""
from __future__ import annotations

import datetime as dt

from vs_config.clock import branch_today
from vs_rbac.tests.helpers import (
    install_declared_fields,
    make_assignment,
    make_role,
    make_role_permission,
    make_school_admin,
    set_field_access,
)

from ..constants import Gender, StudentStatus
from ..field_access import STATUS_REASON_FIELD
from ..models import Student, StudentStatusLog
from .base import StudentsFixture

#: What each school's member of staff wrote, different per school so a reader
#: cannot pass a test by reading the other school's record.
BRIGHTFIELD_REASON = "Fighting in the dining hall on Tuesday."
SUNRISE_REASON = "Repeated absence without notice."


class _ReasonFixture(StudentsFixture):
    """A reader with the switch off and one with it on, at each school.

    The readers hold ``school.students.view`` and nothing else, because the
    question is what a switch does to somebody who may open the record at all.
    The one with the switch off holds no row for the field, which is what a
    role looks like the day the field is declared: sensitive means the default
    does not apply, so no row is Read off.
    """

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        install_declared_fields("school.students")

        cls.teacher = cls.reader(
            cls.school, "Class teacher", "teacher", "teacher@brightfield.test",
            switch_on=False,
        )
        cls.counsellor = cls.reader(
            cls.school, "Counsellor", "counsellor", "counsellor@brightfield.test",
            switch_on=True,
        )
        cls.solo_teacher = cls.reader(
            cls.solo, "Class teacher", "teacher", "teacher@sunrise.test",
            switch_on=False,
        )
        cls.solo_counsellor = cls.reader(
            cls.solo, "Counsellor", "counsellor", "counsellor@sunrise.test",
            switch_on=True,
        )

        cls.pupil = cls.suspended(
            cls.tenant, cls.lekki, "Chiamaka", "Nwosu", BRIGHTFIELD_REASON,
        )
        cls.solo_pupil = cls.suspended(
            cls.solo.tenant, cls.solo_branch, "Amaka", "Obi", SUNRISE_REASON,
        )

        #: ``(school, reader who may not read it, reader who may, pupil, words)``
        cls.cases = (
            ("Brightfield", cls.teacher, cls.counsellor, cls.pupil,
             BRIGHTFIELD_REASON),
            ("Sunrise", cls.solo_teacher, cls.solo_counsellor, cls.solo_pupil,
             SUNRISE_REASON),
        )

    @classmethod
    def reader(cls, school, name, key, email, *, switch_on):
        role = make_role(school, name=name, key=f"{key}_{school.slug}")
        make_role_permission(role, cls.permissions["school.students.view"])
        if switch_on:
            set_field_access(role, STATUS_REASON_FIELD, read=True, write=False)
        user = make_school_admin(None, email=email, tenant=school.tenant)
        make_assignment(school, user, role, branch=None)
        return user

    @classmethod
    def suspended(cls, tenant, branch, first, last, reason, *, return_date=None):
        """A pupil serving a suspension, with the history row behind it.

        Written directly rather than through ``transition``, because these
        tests are about reading the record and the service's own behaviour is
        covered where the service is.
        """
        pupil = Student.all_objects.create(
            tenant=tenant, branch=branch, first_name=first, last_name=last,
            date_of_birth=dt.date(2013, 4, 18), gender=Gender.FEMALE,
            status=StudentStatus.SUSPENDED,
            enrolment_date=branch_today(tenant, branch),
        )
        StudentStatusLog.all_objects.create(
            tenant=tenant, student=pupil,
            from_status=StudentStatus.ACTIVE, to_status=StudentStatus.SUSPENDED,
            reason=reason, effective_date=dt.date(2026, 3, 9),
            return_date=return_date or dt.date(2026, 3, 12),
        )
        return pupil

    # ── reads ──────────────────────────────────────────────────────────────

    def profile(self, reader, pupil):
        response = self.get(reader, "student-detail", pk=pupil.pk)
        self.assertEqual(response.status_code, 200, response.data)
        return response.data["data"]

    def history(self, reader, pupil):
        response = self.get(reader, "student-status-history", pk=pupil.pk)
        self.assertEqual(response.status_code, 200, response.data)
        rows = response.data["data"]
        return rows["results"] if isinstance(rows, dict) else rows


class StatusReasonSwitchTests(_ReasonFixture):
    """The switch decides the reason, and decides nothing else."""

    def test_a_reader_without_the_switch_gets_no_reason_in_either_place(self):
        for school, closed, _, pupil, words in self.cases:
            with self.subTest(school=school):
                suspension = self.profile(closed, pupil)["suspension"]
                self.assertNotIn("reason", suspension)

                rows = self.history(closed, pupil)
                self.assertEqual(len(rows), 1)
                self.assertNotIn("reason", rows[0])
                self.assertNotIn(words, str(rows[0]))

    def test_a_reader_with_the_switch_gets_the_reason_in_both_places(self):
        for school, _, open_to, pupil, words in self.cases:
            with self.subTest(school=school):
                self.assertEqual(
                    self.profile(open_to, pupil)["suspension"]["reason"], words,
                )
                self.assertEqual(self.history(open_to, pupil)[0]["reason"], words)

    def test_the_rest_of_the_suspension_answers_without_the_reason(self):
        """A register still has to know whether to expect the child.

        Withholding the words may not cost a class teacher the dates, or the
        pupil is marked absent on the day the school told them to come back.
        """
        for school, closed, open_to, pupil, _ in self.cases:
            with self.subTest(school=school):
                withheld = self.profile(closed, pupil)["suspension"]
                full = self.profile(open_to, pupil)["suspension"]
                self.assertEqual(withheld["effective_date"], dt.date(2026, 3, 9))
                self.assertEqual(withheld["return_date"], dt.date(2026, 3, 12))
                self.assertEqual(withheld["due_back"], full["due_back"])
                self.assertEqual(
                    set(full) - set(withheld), {"reason"},
                )

    def test_the_history_keeps_every_other_column_for_a_closed_reader(self):
        row = self.history(self.teacher, self.pupil)[0]
        self.assertEqual(row["to_status"], StudentStatus.SUSPENDED)
        # A history row is a serializer's render, so its dates are strings,
        # where the profile's hand-built block carries date objects.
        self.assertEqual(str(row["effective_date"]), "2026-03-09")
        self.assertEqual(str(row["return_date"]), "2026-03-12")
        self.assertIn("to_label", row)

    def test_one_schools_switch_does_not_open_another_schools_record(self):
        """A switch belongs to a role, and a role belongs to one school.

        Brightfield's counsellor role has the reason open. Sunrise's own class
        teacher must still read nothing, or a school would be inheriting a
        decision another school's administrator made.
        """
        self.assertEqual(
            self.profile(self.counsellor, self.pupil)["suspension"]["reason"],
            BRIGHTFIELD_REASON,
        )
        self.assertNotIn(
            "reason", self.profile(self.solo_teacher, self.solo_pupil)["suspension"],
        )


class ReasonOnScreenAndInTheNoticeTests(_ReasonFixture):
    """``send_reason`` and the Read switch answer different questions.

    One is what this pupil's family is told about this suspension; the other is
    which of the school's roles read the record afterwards. Neither may move
    the other, so both directions are pinned here.
    """

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        from vs_notifications.services.seed import (
            seed_notification_templates,
            seed_platform_settings,
        )

        seed_notification_templates()
        seed_platform_settings()

        cls.mother = cls.guardian_for(
            cls.tenant, "Mrs. Ngozi Nwosu", "ngozi.nwosu@example.ng",
        )

    @classmethod
    def guardian_for(cls, tenant, name, email):
        from ..constants import Relationship
        from ..models import Guardian, StudentGuardian

        guardian = Guardian.all_objects.create(
            tenant=tenant, full_name=name, email=email, phone="08035550101",
        )
        StudentGuardian.all_objects.create(
            tenant=tenant, student=cls.pupil, guardian=guardian,
            relationship=Relationship.MOTHER, is_primary=True,
        )
        return guardian

    def suspend(self, pupil, *, reason, send_reason):
        from ..services.status import transition

        pupil.status = StudentStatus.ACTIVE
        pupil.save(update_fields=["status"])
        return transition(
            pupil, StudentStatus.SUSPENDED, actor=self.admin, reason=reason,
            send_reason=send_reason,
        )

    def notice_bodies(self):
        from vs_notifications.models import Notification

        from ..services.suspension_notice import EVENT_KEY

        return " ".join(
            Notification.all_objects.filter(event_type__key=EVENT_KEY)
            .values_list("body", flat=True)
        )

    def test_a_family_may_be_told_a_reason_a_teacher_may_not_read(self):
        """Brightfield suspends Chiamaka and tells her mother exactly why.

        Her class teacher's role does not have the field open, so the words
        reach the mother and not the screen the teacher opens.
        """
        words = "Fighting in the dining hall on Tuesday."

        self.suspend(self.pupil, reason=words, send_reason=True)

        self.assertIn(words, self.notice_bodies())
        self.assertNotIn(
            "reason", self.profile(self.teacher, self.pupil)["suspension"],
        )

    def test_a_reason_withheld_from_the_family_still_reaches_an_open_role(self):
        """The reverse: the school keeps the words off the family's notice.

        The counsellor's role has the field open, so the record still carries
        them for the people who have to act on it.
        """
        words = "Under investigation after a complaint from another parent."

        self.suspend(self.pupil, reason=words, send_reason=False)

        self.assertNotIn(words, self.notice_bodies())
        self.assertEqual(
            self.profile(self.counsellor, self.pupil)["suspension"]["reason"], words,
        )


class RecordHistoryReasonTests(_ReasonFixture):
    """The profile's History tab holds the reason to the same switch.

    The tab merges the status log with the audit trail, and a line of text is
    out of any switch's reach, so the reason never travels inside ``text``. A
    closed reader sees that a pupil was suspended, when and by whom, and never
    the words; an open reader gets the words as ``reason`` beside the text.

    Each pupil also carries an audit row whose summary ends in the reason,
    which is how the trail can hold a status move: the trail is immutable, so
    such a row is read for as long as the school keeps it.
    """

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        from vs_audit.models import AuditActionType, AuditModuleKey
        from vs_audit.services import emit_audit_event

        for _, _, _, pupil, words in cls.cases:
            emit_audit_event(
                module_key=AuditModuleKey.STUDENT,
                action_type=AuditActionType.STUDENT_SUSPENDED,
                entity_type="Student", entity_id=str(pupil.pk),
                entity_label=pupil.full_name, tenant=pupil.tenant,
                actor_user=None,
                summary=(
                    f"{pupil.full_name} moved from Active to Suspended on "
                    f"09/03/2026. Reason: {words}"
                ),
                metadata={"from": "ACTIVE", "to": "SUSPENDED"},
            )

    def record_history(self, reader, pupil):
        response = self.get(reader, "student-history", pk=pupil.pk)
        self.assertEqual(response.status_code, 200, response.data)
        rows = response.data["data"]
        return rows["results"] if isinstance(rows, dict) else rows

    def test_a_closed_reader_sees_the_moves_without_the_reason(self):
        for school, closed, _, pupil, words in self.cases:
            with self.subTest(school=school):
                entries = self.record_history(closed, pupil)
                self.assertEqual(len(entries), 2)
                for entry in entries:
                    self.assertNotIn("reason", entry)
                    self.assertNotIn(words, str(entry))
                    self.assertIn("Suspended", entry["text"])

    def test_an_open_reader_gets_the_reason_beside_the_text(self):
        for school, _, open_to, pupil, words in self.cases:
            with self.subTest(school=school):
                entries = self.record_history(open_to, pupil)
                self.assertEqual(len(entries), 2)
                for entry in entries:
                    self.assertEqual(entry["reason"], words)
                    self.assertNotIn(words, entry["text"])

    def test_a_move_made_on_the_status_route_keeps_the_reason_out_of_the_summary(self):
        """The reason travels in the audit event's metadata, not its summary.

        The summary is what every surface that prints the trail shows, and
        none of them can apply a switch to part of a sentence.
        """
        from vs_audit.models import AuditEvent

        from ..services.status import transition

        words = "Left the school gate during lessons."
        pupil = self.solo_pupil
        pupil.status = StudentStatus.ACTIVE
        pupil.save(update_fields=["status"])
        transition(
            pupil, StudentStatus.SUSPENDED, actor=self.solo_admin, reason=words,
        )

        event = AuditEvent.objects.filter(
            entity_type="Student", entity_id=str(pupil.pk),
        ).order_by("-event_at", "-id").first()
        self.assertNotIn(words, event.summary)
        self.assertEqual(event.metadata["reason"], words)

        self.assertNotIn(words, str(self.record_history(self.solo_teacher, pupil)))
        opened = [
            entry for entry in self.record_history(self.solo_counsellor, pupil)
            if entry.get("reason") == words
        ]
        self.assertEqual(len(opened), 2)


class AdmissionStageReasonTests(_ReasonFixture):
    """An applicant's stage move holds its reason to the same switch.

    Moving an applicant between a school's admission stages is a step in the
    decision whose last step is a status move (confirmed or rejected), and the
    reason typed for it is the same kind of words about the same child, so the
    History tab serves it as ``reason`` only to a reader whose role has Read
    on ``school.students.status_reason``.

    Each applicant carries one stage move made through the service, and one
    audit row written the way the trail can hold an older stage move: the
    reason at the end of the summary as well as in its metadata.
    """

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        from vs_audit.models import AuditActionType, AuditModuleKey
        from vs_audit.services import emit_audit_event

        from ..models import AdmissionStage
        from ..services.admission import move_to_stage

        cls.applicant_cases = []
        for school, closed, open_to, branch, admin, first, last, words, old in (
            ("Brightfield", cls.teacher, cls.counsellor, cls.lekki, cls.admin,
             "Tunde", "Bakare", "Parents could not pay the fees.",
             "Did not sit the entrance exam."),
            ("Sunrise", cls.solo_teacher, cls.solo_counsellor, cls.solo_branch,
             cls.solo_admin, "Ifeoma", "Eze", "A sibling is already waitlisted.",
             "Birth certificate does not match the form."),
        ):
            tenant = branch.tenant
            interview = AdmissionStage.all_objects.create(
                tenant=tenant, name="Interview", position=1,
            )
            waitlist = AdmissionStage.all_objects.create(
                tenant=tenant, name="Waitlisted", position=2,
            )
            applicant = Student.all_objects.create(
                tenant=tenant, branch=branch, first_name=first, last_name=last,
                date_of_birth=dt.date(2014, 6, 2), gender=Gender.MALE,
                status=StudentStatus.APPLICANT,
                enrolment_date=branch_today(tenant, branch),
            )
            emit_audit_event(
                module_key=AuditModuleKey.STUDENT,
                action_type=AuditActionType.UPDATE,
                entity_type="Student", entity_id=str(applicant.pk),
                entity_label=applicant.full_name, tenant=tenant, actor_user=None,
                summary=(
                    f"{applicant.full_name} moved from no stage to Interview. "
                    f"Reason: {old}"
                ),
                metadata={
                    "from": None,
                    "to": {"id": interview.pk, "name": "Interview"},
                    "offer_expires_on": None, "reason": old,
                },
            )
            applicant.admission_stage = interview
            applicant.save(update_fields=["admission_stage"])
            move_to_stage(applicant, waitlist, actor=admin, reason=words)
            cls.applicant_cases.append(
                (school, closed, open_to, applicant, words, old),
            )

    def record_history(self, reader, pupil):
        response = self.get(reader, "student-history", pk=pupil.pk)
        self.assertEqual(response.status_code, 200, response.data)
        rows = response.data["data"]
        return rows["results"] if isinstance(rows, dict) else rows

    def test_a_closed_reader_sees_the_stage_moves_without_the_reason(self):
        for school, closed, _, applicant, words, old in self.applicant_cases:
            with self.subTest(school=school):
                entries = self.record_history(closed, applicant)
                self.assertEqual(len(entries), 2)
                for entry in entries:
                    self.assertNotIn("reason", entry)
                    self.assertNotIn(words, str(entry))
                    self.assertNotIn(old, str(entry))
                    self.assertIn("moved from", entry["text"])

    def test_an_open_reader_gets_each_reason_beside_the_text(self):
        for school, _, open_to, applicant, words, old in self.applicant_cases:
            with self.subTest(school=school):
                entries = self.record_history(open_to, applicant)
                self.assertEqual(
                    sorted(entry.get("reason") for entry in entries),
                    sorted([words, old]),
                )
                for entry in entries:
                    self.assertNotIn("Reason:", entry["text"])

    def test_an_older_row_is_cut_where_its_reason_began(self):
        _, _, open_to, applicant, _, old = self.applicant_cases[0]
        entry = next(
            e for e in self.record_history(open_to, applicant)
            if e.get("reason") == old
        )
        self.assertEqual(
            entry["text"], f"{applicant.full_name} moved from no stage to Interview.",
        )

    def test_a_stage_move_keeps_the_reason_out_of_its_summary(self):
        from vs_audit.models import AuditEvent

        for school, _, _, applicant, words, _ in self.applicant_cases:
            with self.subTest(school=school):
                event = AuditEvent.objects.filter(
                    entity_type="Student", entity_id=str(applicant.pk),
                ).order_by("-event_at", "-id").first()
                self.assertEqual(
                    event.summary,
                    f"{applicant.full_name} moved from Interview to Waitlisted.",
                )
                self.assertEqual(event.metadata["reason"], words)

    def test_an_ordinary_edit_never_carries_a_reason(self):
        """Only a stage move is read for one, whatever an edit's summary says."""
        from vs_audit.models import AuditActionType, AuditModuleKey
        from vs_audit.services import emit_audit_event

        _, _, open_to, applicant, _, _ = self.applicant_cases[0]
        emit_audit_event(
            module_key=AuditModuleKey.STUDENT, action_type=AuditActionType.UPDATE,
            entity_type="Student", entity_id=str(applicant.pk),
            entity_label=applicant.full_name, tenant=applicant.tenant,
            actor_user=None, summary=f"{applicant.full_name}'s record updated.",
            diff_data={"phone": {"from": "", "to": "08035550101"}},
        )
        edits = [
            e for e in self.record_history(open_to, applicant)
            if e["text"].endswith("record updated.")
        ]
        self.assertEqual(len(edits), 1)
        self.assertNotIn("reason", edits[0])

    def test_one_schools_reader_cannot_open_another_schools_applicant(self):
        _, _, open_to, _, _, _ = self.applicant_cases[0]
        _, _, _, other, _, _ = self.applicant_cases[1]
        response = self.get(open_to, "student-history", pk=other.pk)
        self.assertEqual(response.status_code, 404)


class AuditLogReasonTests(_ReasonFixture):
    """The platform audit log holds a status or stage reason to the same switch.

    The trail is append-only, so nothing is rewritten: the words are hidden on
    read. An older row whose summary ends in ``" Reason: ..."`` is cut there,
    and a newer row's ``metadata["reason"]`` is left out of the detail, for a
    reader whose roles do not grant Read on ``school.students.status_reason``.
    A reader whose roles grant it sees the row as it was written.

    Every reader here holds ``platform.audit.view`` and ``.export`` so the
    question is only what the switch does. Two CodeX operators read across
    schools: support staff whose platform role has no switch, and one whose
    platform role has Read turned on in CodeX's own Field Access.
    """

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        from vs_audit.models import AuditActionType, AuditModuleKey
        from vs_audit.services import emit_audit_event
        from vs_rbac.models import TenantRoleTemplate
        from vs_rbac.tests.helpers import make_permission
        from vs_tenants.models import Tenant

        audit_keys = [
            make_permission("platform.audit.view"),
            make_permission("platform.audit.export"),
        ]
        for school in (cls.school, cls.solo):
            for key in ("teacher", "counsellor"):
                role = TenantRoleTemplate.objects.get(
                    tenant=school.tenant, key=f"{key}_{school.slug}",
                )
                for permission in audit_keys:
                    make_role_permission(role, permission)

        codex = Tenant.objects.get(slug="codex", kind=Tenant.Kind.PLATFORM)
        cls.codex_support = cls.codex_reader(
            codex, "codex_support", "support@codex.test", audit_keys, switch_on=False,
        )
        cls.codex_privacy = cls.codex_reader(
            codex, "codex_privacy", "privacy@codex.test", audit_keys, switch_on=True,
        )

        cls.words = {}
        cls.events = {}
        for school, pupil in (("Brightfield", cls.pupil), ("Sunrise", cls.solo_pupil)):
            words = {
                "status_old": f"{school}: fought in the dining hall.",
                "status_new": f"{school}: left the gate during lessons.",
                "stage_old": f"{school}: did not sit the entrance exam.",
                "stage_new": f"{school}: parents could not pay the fees.",
            }
            stage_meta = {
                "from": None, "to": {"id": 1, "name": "Interview"},
                "offer_expires_on": None,
            }
            name = pupil.full_name

            def emit(action, summary, metadata, pupil=pupil):
                return emit_audit_event(
                    module_key=AuditModuleKey.STUDENT, action_type=action,
                    entity_type="Student", entity_id=str(pupil.pk),
                    entity_label=pupil.full_name, tenant=pupil.tenant,
                    actor_user=None, summary=summary, metadata=metadata,
                )

            cls.events[school] = {
                "status_old": emit(
                    AuditActionType.STUDENT_SUSPENDED,
                    f"{name} moved from Active to Suspended. "
                    f"Reason: {words['status_old']}",
                    {"from": "ACTIVE", "to": "SUSPENDED"},
                ),
                "status_new": emit(
                    AuditActionType.STUDENT_SUSPENDED,
                    f"{name} moved from Active to Suspended.",
                    {"from": "ACTIVE", "to": "SUSPENDED",
                     "reason": words["status_new"]},
                ),
                "stage_old": emit(
                    AuditActionType.UPDATE,
                    f"{name} moved from no stage to Interview. "
                    f"Reason: {words['stage_old']}",
                    {**stage_meta, "reason": words["stage_old"]},
                ),
                "stage_new": emit(
                    AuditActionType.UPDATE,
                    f"{name} moved from no stage to Interview.",
                    {**stage_meta, "reason": words["stage_new"]},
                ),
                "unrelated": emit_audit_event(
                    module_key=AuditModuleKey.STUDENT,
                    action_type=AuditActionType.UPDATE,
                    entity_type="Guardian", entity_id="9",
                    entity_label="Mrs. Ngozi Nwosu", tenant=pupil.tenant,
                    actor_user=None,
                    summary="Guardian unlinked. Reason: requested by the family.",
                    metadata={"reason": "requested by the family."},
                ),
            }
            cls.words[school] = words

        #: ``(school, reader who may not read it, reader who may)``
        cls.readers = (
            ("Brightfield", cls.teacher, cls.counsellor),
            ("Sunrise", cls.solo_teacher, cls.solo_counsellor),
        )

    @classmethod
    def codex_reader(cls, codex, key, email, permissions, *, switch_on):
        role = make_role(codex, name=key, key=key)
        for permission in permissions:
            make_role_permission(role, permission)
        if switch_on:
            set_field_access(role, STATUS_REASON_FIELD, read=True, write=False)
        user = make_school_admin(None, email=email, tenant=codex)
        make_assignment(codex, user, role, branch=None)
        return user

    # ── reads ──────────────────────────────────────────────────────────────

    def listed(self, reader, **params):
        response = self.get(
            reader, "audit-event-list", {"page_size": 100, **params},
        )
        self.assertEqual(response.status_code, 200, response.data)
        rows = response.data.get("data", response.data)
        if isinstance(rows, dict):
            rows = rows.get("results", rows)
        return {row["id"]: row for row in rows}

    def detail(self, reader, event):
        response = self.get(reader, "audit-event-detail", id=event.id)
        self.assertEqual(response.status_code, 200, response.data)
        return response.data.get("data", response.data)

    def summary(self, reader, event):
        return self.listed(reader)[str(event.id)]["summary"]

    # ── a school's own readers ─────────────────────────────────────────────

    def test_a_closed_reader_lists_every_move_without_its_reason(self):
        for school, closed, _ in self.readers:
            with self.subTest(school=school):
                rows = self.listed(closed)
                for words in self.words[school].values():
                    self.assertNotIn(words, str(rows))
                events = self.events[school]
                pupil = events["status_old"].entity_label
                self.assertEqual(
                    rows[str(events["status_old"].id)]["summary"],
                    f"{pupil} moved from Active to Suspended.",
                )
                self.assertEqual(
                    rows[str(events["stage_old"].id)]["summary"],
                    f"{pupil} moved from no stage to Interview.",
                )

    def test_a_closed_reader_opens_each_move_without_its_reason(self):
        for school, closed, _ in self.readers:
            for kind in ("status_old", "status_new", "stage_old", "stage_new"):
                with self.subTest(school=school, kind=kind):
                    row = self.detail(closed, self.events[school][kind])
                    self.assertNotIn(self.words[school][kind], str(row))
                    self.assertNotIn("reason", row["metadata"] or {})

    def test_an_open_reader_sees_each_row_as_it_was_written(self):
        for school, _, open_to in self.readers:
            with self.subTest(school=school):
                events, words = self.events[school], self.words[school]
                rows = self.listed(open_to)
                self.assertEqual(
                    rows[str(events["status_old"].id)]["summary"],
                    events["status_old"].summary,
                )
                self.assertIn(words["stage_old"], rows[str(events["stage_old"].id)]["summary"])
                for kind in ("status_new", "stage_new"):
                    self.assertEqual(
                        self.detail(open_to, events[kind])["metadata"]["reason"],
                        words[kind],
                    )

    def test_a_row_no_rule_names_is_never_cut(self):
        """Only a status or stage move about a pupil is read for a reason."""
        for school, closed, _ in self.readers:
            with self.subTest(school=school):
                event = self.events[school]["unrelated"]
                self.assertEqual(self.summary(closed, event), event.summary)
                self.assertEqual(
                    self.detail(closed, event)["metadata"]["reason"],
                    "requested by the family.",
                )

    def test_the_entity_trail_holds_the_same_line(self):
        for school, closed, open_to in self.readers:
            with self.subTest(school=school):
                event = self.events[school]["status_old"]
                path = {"entity_type": "Student", "entity_id": event.entity_id}
                hidden = self.get(closed, "entity-audit-trail-detail", **path)
                self.assertEqual(hidden.status_code, 200, hidden.data)
                for words in self.words[school].values():
                    self.assertNotIn(words, str(hidden.data))
                shown = self.get(open_to, "entity-audit-trail-detail", **path)
                self.assertIn(self.words[school]["status_old"], str(shown.data))

    def test_an_export_carries_what_the_screen_shows(self):
        import csv
        import io

        from django.core.files.storage import default_storage

        from vs_audit.models import AuditExportJob

        for school, closed, open_to in self.readers:
            for reader, sees in ((closed, False), (open_to, True)):
                with self.subTest(school=school, sees=sees):
                    created = self.post(
                        reader, "audit-export-list",
                        {"filter_payload": {"entity_type": "Student"}},
                    )
                    self.assertEqual(created.status_code, 201, created.data)
                    job = AuditExportJob.objects.get(pk=created.data["data"]["id"])
                    with default_storage.open(job.file_path) as handle:
                        text = handle.read().decode("utf-8-sig")
                    rows = list(csv.reader(io.StringIO(text)))
                    self.assertEqual(len(rows) - 1, 4)
                    old = self.words[school]["status_old"]
                    self.assertEqual(old in text, sees)

    def test_a_search_never_matches_words_inside_a_hidden_reason(self):
        """Searching the trail for a reason's words is reading it.

        ``entrance exam`` occurs only in the reason of each school's older stage
        move. A closed reader's search finds nothing, so the result cannot say
        which pupil the words were written about; the words the row shows
        still match; an open reader finds the row.
        """
        for school, closed, open_to in self.readers:
            with self.subTest(school=school):
                stage_old = str(self.events[school]["stage_old"].id)
                self.assertNotIn(stage_old, self.listed(closed, search="entrance exam"))
                self.assertIn(stage_old, self.listed(closed, search="no stage to Interview"))
                self.assertIn(stage_old, self.listed(open_to, search="entrance exam"))

    # ── the Export Centre ──────────────────────────────────────────────────

    def export_centre(self, reader, *, search=""):
        """Run an ``audit.events`` export as *reader* the way the worker runs it."""
        import datetime as dt_

        from django.core.files.storage import default_storage
        from django.utils import timezone

        from vs_exports import services

        today = timezone.now().date()
        filters = [{
            "id": "event_at",
            "start": (today - dt_.timedelta(days=7)).isoformat(),
            "end": (today + dt_.timedelta(days=1)).isoformat(),
        }]
        if search:
            filters.append({"id": "search", "value": search})
        run, _ = services.trigger_quick_run(
            config={
                "dataset_key": "audit.events", "columns": ["event_at", "summary"],
                "filters": filters, "format": "csv",
            },
            entity=None, tenant=reader.tenant, actor=reader, queue=False,
        )
        from vs_exports.models import ExportFile

        services.execute_run(run.pk)
        run.refresh_from_db()
        file = ExportFile.objects.filter(run=run).first()
        self.assertIsNotNone(file, f"{run.status}: {run.failure_message}")
        with default_storage.open(file.storage_name, "rb") as handle:
            return handle.read().decode("utf-8-sig")

    def test_an_export_centre_file_carries_what_the_screen_shows(self):
        for school, closed, open_to in self.readers:
            with self.subTest(school=school):
                hidden = self.export_centre(closed)
                for words in self.words[school].values():
                    self.assertNotIn(words, hidden)
                self.assertIn("moved from no stage to Interview.", hidden)
                shown = self.export_centre(open_to)
                self.assertIn(self.words[school]["status_old"], shown)
                self.assertIn(self.words[school]["stage_old"], shown)

    def test_an_export_centre_search_never_matches_a_hidden_reason(self):
        for school, closed, open_to in self.readers:
            with self.subTest(school=school):
                pupil = self.events[school]["stage_old"].entity_label
                self.assertNotIn(pupil, self.export_centre(closed, search="entrance exam"))
                self.assertIn(pupil, self.export_centre(open_to, search="entrance exam"))

    # ── CodeX ──────────────────────────────────────────────────────────────

    def test_codex_support_reads_every_school_without_the_reasons(self):
        rows = self.listed(self.codex_support, entity_type="Student")
        for school in ("Brightfield", "Sunrise"):
            with self.subTest(school=school):
                for words in self.words[school].values():
                    self.assertNotIn(words, str(rows))
                row = self.detail(self.codex_support, self.events[school]["stage_new"])
                self.assertNotIn("reason", row["metadata"])

    def test_a_codex_role_codex_opened_reads_them(self):
        rows = self.listed(self.codex_privacy, entity_type="Student")
        for school in ("Brightfield", "Sunrise"):
            with self.subTest(school=school):
                self.assertIn(self.words[school]["status_old"], str(rows))
                row = self.detail(self.codex_privacy, self.events[school]["stage_new"])
                self.assertEqual(row["metadata"]["reason"], self.words[school]["stage_new"])

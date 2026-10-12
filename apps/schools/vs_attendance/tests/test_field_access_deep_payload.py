"""No attendance field reaches a caller who may read none, at any depth.

Every declared surface of ``attendance.register`` and of ``attendance.correction``
is rendered on real records whose fields are all filled in (a late arrival, an
excused reason, a note, a sent notice, a submitted register with a changed row,
a decided correction and one still waiting) as a caller whose role has every
field of that resource switched off. The whole payload is walked, so the history
nested in a register, the hub and a student's tab is held to the same switches as
the register itself.

The hub and the student's tab each carry correction entries beside register
fields, rendered by the serializer of the correction resource. Those entries are
governed by the correction switches, and each key of one that shares a name with
a register field is listed below with that reason.

Both shapes of school: Brightfield has two branches and Sunrise has one.
"""
from __future__ import annotations

import datetime as dt

from vs_rbac.tests.deep_payload import DeepPayloadChecks, Sample

from ..constants import ChangeVia, CorrectionStatus, NoticeState
from ..models import ClassRegister, CorrectionRequest, MarkChange, ReasonCode, StudentMark
from ..services import corrections, hub
from ..services.reasons import ensure_default_reason_codes, reason_code_rows
from ..services.report import build_report
from ..services.registers import load_context, register_payload
from ..services.student_tab import student_tab
from .base import NOW, TODAY, AttendanceFixture

_SURFACE = "schools.vs_attendance.serializers."

#: Correction entries render beside register fields on the hub and the tab.
_CORRECTION_KEYS = (
    "from_mark", "to_mark", "arrived_at", "reason_code", "reason_label", "note", "status",
)


class AttendanceDeepPayloadTests(DeepPayloadChecks, AttendanceFixture):
    resources = frozenset({"attendance.register"})
    covers = frozenset({
        _SURFACE + "RegisterSerializer",
        _SURFACE + "RegisterRowSerializer",
        _SURFACE + "MarkChangeSerializer",
        _SURFACE + "MarkWriteSerializer",
        _SURFACE + "HubSerializer",
        _SURFACE + "HubAbsentRowSerializer",
        _SURFACE + "TeacherHubSerializer",
        _SURFACE + "TeacherCardSerializer",
        _SURFACE + "StudentAttendanceSerializer",
        _SURFACE + "StudentDaySerializer",
        _SURFACE + "StudentAbsenceSerializer",
        _SURFACE + "StudentChangeSerializer",
        _SURFACE + "ReportSerializer",
        _SURFACE + "ReportRangeSerializer",
        _SURFACE + "ReportClassRowSerializer",
        _SURFACE + "ReasonCodeSerializer",
    })
    allowlist = {
        **{
            (_SURFACE + "HubSerializer", f"pending_corrections_preview.{key}"):
                "The first correction waiting, rendered by the correction serializer "
                "and governed by attendance.correction."
            for key in _CORRECTION_KEYS
        },
        **{
            (_SURFACE + "StudentAttendanceSerializer", f"history[].{key}"):
                "A decided correction in the student's history, rendered by the "
                "correction serializer and governed by attendance.correction."
            for key in _CORRECTION_KEYS
        },
    }

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        ensure_default_reason_codes(cls.tenant)
        sick = ReasonCode.objects.get(tenant=cls.tenant, name="Sick")

        register = ClassRegister.objects.create(
            tenant=cls.tenant, school_class=cls.jss1a, date=TODAY, branch=cls.lekki,
            session=cls.year, term=cls.first_term, submitted_at=NOW,
            submitted_by=cls.admin, on_behalf_of=cls.eze_staff,
        )
        base = {"tenant": cls.tenant, "register": register, "marked_by": cls.eze}
        late = StudentMark.objects.create(
            student=cls.tunde, mark="L", arrived_at=dt.time(8, 40),
            note="Bus broke down", **base,
        )
        cls.excused = StudentMark.objects.create(
            student=cls.kelechi, mark="E", reason_code=sick, note="Fever", **base,
        )
        absent = StudentMark.objects.create(
            student=cls.chiamaka, mark="A", note="Phoned in",
            notice_state=NoticeState.SENT, notice_sent_at=NOW, **base,
        )
        MarkChange.objects.create(
            tenant=cls.tenant, mark=absent, from_mark="P", to_mark="A",
            changed_by=cls.admin, via=ChangeVia.SAME_DAY, note="Phoned in",
        )
        cls.records = [(cls.tenant, cls.jss1a, late, "brightfield")]

        solo_register = ClassRegister.objects.create(
            tenant=cls.solo, school_class=cls.solo_class, date=TODAY,
            branch=cls.solo_branch, session=cls.solo_class.session,
            submitted_at=NOW, submitted_by=cls.solo_teacher,
        )
        solo_mark = StudentMark.objects.create(
            tenant=cls.solo, register=solo_register, student=cls.solo_pupil, mark="A",
            note="Phoned in", notice_state=NoticeState.SENT, notice_sent_at=NOW,
            marked_by=cls.solo_teacher,
        )
        MarkChange.objects.create(
            tenant=cls.solo, mark=solo_mark, from_mark="P", to_mark="A",
            changed_by=cls.solo_teacher, via=ChangeVia.SAME_DAY, note="Phoned in",
        )
        cls.records.append((cls.solo, cls.solo_class, solo_mark, "sunrise"))
        cls.waiting = _correct(cls.tenant, absent, cls.eze, sick, CorrectionStatus.PENDING)
        cls.decided = _correct(
            cls.tenant, absent, cls.eze, sick, CorrectionStatus.APPROVED, decider=cls.admin,
        )
        cls.solo_waiting = _correct(
            cls.solo, solo_mark, cls.solo_teacher, None, CorrectionStatus.PENDING,
        )
        cls.solo_decided = _correct(
            cls.solo, solo_mark, cls.solo_teacher, None, CorrectionStatus.REJECTED,
            decider=cls.solo_admin,
        )
        cls.tabbed = [
            (cls.tenant, cls.admin, cls.chiamaka, cls.jss1a, True),
            (cls.solo, cls.solo_admin, cls.solo_pupil, cls.solo_class, False),
        ]

    def samples(self):
        samples = []
        for tenant, school_class, mark, _ in self.records:
            admin = self.admin if tenant.pk == self.tenant.pk else self.solo_admin
            ctx = load_context(tenant, admin, school_class.pk, TODAY)
            payload = register_payload(ctx)
            samples += [
                Sample(_SURFACE + "RegisterSerializer", payload, tenant=tenant),
                Sample(_SURFACE + "RegisterRowSerializer", payload["rows"], many=True, tenant=tenant),
                Sample(_SURFACE + "MarkChangeSerializer", payload["history"], many=True, tenant=tenant),
                Sample(_SURFACE + "MarkWriteSerializer", mark, tenant=tenant),
            ]
            samples += self.hub_samples(tenant, admin)
            samples += self.report_samples(tenant, admin)
        for tenant, viewer, student, school_class, several in self.tabbed:
            tab = student_tab(tenant, viewer, student.pk)
            changes = [e for e in tab["history"] if e["kind"] == "change"]
            samples += [
                Sample(_SURFACE + "StudentAttendanceSerializer", tab, tenant=tenant),
                Sample(_SURFACE + "StudentDaySerializer", tab["days"], many=True, tenant=tenant),
                Sample(_SURFACE + "StudentAbsenceSerializer", tab["absences"], many=True, tenant=tenant),
                Sample(_SURFACE + "StudentChangeSerializer", changes, many=True, tenant=tenant),
            ]
        return samples

    def report_samples(self, tenant, admin):
        body = build_report(tenant, admin)
        return [
            Sample(_SURFACE + "ReportSerializer", body, tenant=tenant),
            Sample(_SURFACE + "ReportRangeSerializer", body["range"], tenant=tenant),
            Sample(_SURFACE + "ReportClassRowSerializer", body["by_class"], many=True, tenant=tenant),
            Sample(
                _SURFACE + "ReasonCodeSerializer", reason_code_rows(tenant), many=True,
                tenant=tenant,
            ),
        ]

    def hub_samples(self, tenant, admin):
        teacher = self.eze if tenant.pk == self.tenant.pk else self.solo_teacher
        body = hub.admin_hub(tenant, admin, on_date=TODAY, can_decide=True)
        mine = hub.teacher_hub(tenant, teacher, on_date=TODAY)
        return [
            Sample(_SURFACE + "HubSerializer", body, tenant=tenant),
            Sample(_SURFACE + "HubAbsentRowSerializer", body["absent"], many=True, tenant=tenant),
            Sample(_SURFACE + "TeacherHubSerializer", mine, tenant=tenant),
            Sample(_SURFACE + "TeacherCardSerializer", mine["classes"], many=True, tenant=tenant),
        ]


def _correct(tenant, mark, requester, reason, status, *, decider=None):
    return CorrectionRequest.objects.create(
        tenant=tenant, mark=mark, from_mark=mark.mark, to_mark="P" if mark.mark != "P" else "A",
        reason_code=reason, note="Marked in the wrong register", requested_by=requester,
        requested_at=NOW, status=status, decided_by=decider,
        decided_at=NOW if decider is not None else None,
        decision_note="Seen the letter" if decider is not None else "",
    )


class AttendanceCorrectionDeepPayloadTests(DeepPayloadChecks, AttendanceFixture):
    resources = frozenset({"attendance.correction"})
    covers = frozenset({
        _SURFACE + "CorrectionSerializer",
        _SURFACE + "CorrectionCreateSerializer",
        _SURFACE + "CorrectionDecisionSerializer",
        _SURFACE + "StudentCorrectionSerializer",
    })

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        ensure_default_reason_codes(cls.tenant)
        sick = ReasonCode.objects.get(tenant=cls.tenant, name="Sick")
        register = ClassRegister.objects.create(
            tenant=cls.tenant, school_class=cls.jss1a, date=TODAY, branch=cls.lekki,
            session=cls.year, term=cls.first_term, submitted_at=NOW, submitted_by=cls.eze,
        )
        absent = StudentMark.objects.create(
            tenant=cls.tenant, register=register, student=cls.chiamaka, mark="A",
            marked_by=cls.eze,
        )
        cls.waiting = _correct(cls.tenant, absent, cls.eze, sick, CorrectionStatus.PENDING)
        cls.decided = _correct(
            cls.tenant, absent, cls.eze, sick, CorrectionStatus.APPROVED, decider=cls.admin,
        )
        solo_register = ClassRegister.objects.create(
            tenant=cls.solo, school_class=cls.solo_class, date=TODAY, branch=cls.solo_branch,
            session=cls.solo_class.session, submitted_at=NOW, submitted_by=cls.solo_teacher,
        )
        solo_mark = StudentMark.objects.create(
            tenant=cls.solo, register=solo_register, student=cls.solo_pupil, mark="A",
            marked_by=cls.solo_teacher,
        )
        cls.solo_waiting = _correct(
            cls.solo, solo_mark, cls.solo_teacher, None, CorrectionStatus.PENDING,
        )
        cls.solo_decided = _correct(
            cls.solo, solo_mark, cls.solo_teacher, None, CorrectionStatus.REJECTED,
            decider=cls.solo_admin,
        )

    def samples(self):
        samples = []
        for tenant, viewer, student, correction, several in (
            (self.tenant, self.admin, self.chiamaka, self.waiting, True),
            (self.solo, self.solo_admin, self.solo_pupil, self.solo_waiting, False),
        ):
            rows = [
                corrections.correction_row(row, several_branches=several, viewer=viewer)
                for row in corrections.corrections_visible_to(viewer, tenant, only_own=False)
            ]
            tab = student_tab(tenant, viewer, student.pk)
            entries = [e for e in tab["history"] if e["kind"] == "correction"]
            samples += [
                Sample(_SURFACE + "CorrectionSerializer", rows, many=True, tenant=tenant),
                Sample(
                    _SURFACE + "CorrectionCreateSerializer",
                    {
                        "register_class_id": 1, "date": TODAY, "student_id": student.pk,
                        "to_mark": "L", "reason_code_id": 1, "note": "Marked in error",
                        "arrived_at": dt.time(8, 30),
                    },
                    tenant=tenant,
                ),
                Sample(
                    _SURFACE + "CorrectionDecisionSerializer",
                    {"decision_note": "Seen the letter"}, tenant=tenant,
                ),
                Sample(_SURFACE + "StudentCorrectionSerializer", entries, many=True, tenant=tenant),
            ]
        return samples

"""The register fields an administrator may restrict per role.

One resource, ``attendance.register``, covers everything a register shows about
a child's day. A resource permission answers whether somebody may open the
register; these switches answer which of its fields they read and change.

**The marks.** The mark, the arrival time of a late child and the reason an
absence is excused each have their own Read and Write switch. The mark travels
under three names: ``mark`` on a row, and ``from_mark`` and ``to_mark`` on the
history of a change, and one switch covers all three. The reason travels as its
id and as the name of the reason, and one switch covers both.

**The note is open like any ordinary field.** It is the teacher's own working
note beside a mark ("fever", "phoned in"), written by whoever takes the
register and corrected by whoever may mark, so it reads and writes by default
for every role that can open the register. A school that wants it closed to a
role switches it off for that role like any other field. The history of a
change carries the note as well and follows the same switch.

**Server-owned fields are read-only.** The guardian notice state is the outcome
of an email the system sent. The counts and the submit preview are derived from
the marks. The register status is derived from the counts. Who took the
register, when, and on whose behalf is an audit fact. None of them can be
written, and each has a Read switch so a school can keep a derived figure from a
role that may not read the marks it is derived from.

A name printed on a register is the student's name in a class list, which is a
field of the student records, not of the register.

**The same switches reach the hubs and the student's tab.** The administrator's
hub, the teacher's hub and the Attendance tab of a student's record show marks,
reasons, notes, arrival times, notice states, counts and register statuses
under names of their own, and each name is registered against the field it
carries so one switch governs it wherever it is printed: the hub's ``kpis`` and
a card's ``student_count`` are counts, the hub's ``not_taken`` list is the
status of registers, and a card's ``submitted_at`` and ``submitted_by_name`` are
who took the register. The tab's day cells send a ``state`` that says only
whether a day was marked, not taken, closed, a suspension or before the pupil
joined, and keep the mark in ``mark``, so hiding the mark never hides which
days the school was open.

**The reports are figures, so they follow the counts switch.** The weekly trend,
the by-level table and the chronic list are made of rates and totals, and the
chronic list is itself a statement about a rate (who falls below the line), so
each is left out whole when the counts are hidden. One class's chronic list,
asked for by the class's own teacher, is the same list narrowed to the class and
follows the same switch. A class in the by-class table
carries its figures under ``counts`` and loses only those, so a role that may
read the register but not the totals still sees which classes there are. The
count of school days in the range, and how many marks used each excused reason,
are counts as well. The list of days on which a register was not taken is the
register's status.

**The settings and the school's list of reasons are configuration, not a
register.** The four settings, and the name and flags of an excused reason, are
the school's own choices, shared by every branch. They are read with
``attendance.register.view`` and changed with ``attendance.settings.update``
by a caller whose reach is the whole school, as the calendar's rooms, bell
schedule and rules and the academic structure are. They carry no record of any
child, so they have no switch of their own.

**Lessons are a resource of their own,** ``attendance.lesson``. A lesson register
is a different record from the morning register: a child can be present in the
morning and absent from a lesson, and a role may be given one and not the other.
It has its own switches for the lesson mark, the arrival time and the note
(each readable and writable), for the morning mark shown beside a lesson row, for
a lesson's status and counts, and for who took it. The morning mark is the class
register's mark under another name, so it follows both switches: a role that may
not read the mark on the register never sees it beside a lesson either. The
lists of students who missed a lesson (the hub's list, a student's lesson
absences) follow the lesson mark, because each row says the lesson mark was
Absent, and the figures (the hub's count, the report's table) follow the lesson
mark and the lesson counts.

**The staff register is a resource of its own,** ``attendance.staff``. It records
what an administrator saw of a colleague, a different record from a child's day.
The mark, the arrival time and the note each have a Read and a Write switch;
the group's status, its counts and who submitted it are derived or kept by the
system and are read only. A staff member's name and number on a row are left
open, as a student's name and number are on a class register: a register is
unusable without names. What a row borrows from a staff record obeys Staff
Management's own switches rather than a copy of them: the job title in the
role line follows ``school.teachers.job_title``, and the exit block (the status
and last working day of somebody about to leave) follows
``school.teachers.employment_status`` for the status and
``school.teachers.exit_date`` for the day, each part sent only to a caller who
may read its field and the whole block left out when neither can be read.
The leave a locked row carries comes from the leave records, which have no field
switch in Staff Management: it is sent only to a caller who holds
``school.leave.view`` and the key is absent for anyone else. Nothing about a person's sign-in (a lockout, a suspended
account) is a field here or anywhere on the register.

**Corrections are a resource of their own,** ``attendance.correction``. A
request to change a mark after its day has closed names the mark asked for, why,
and a note, and a decision carries its own note. The mark asked for, the reason,
the note, the arrival time and the decision note are written by whoever files or
decides, and each has a Read and a Write switch. Who filed it, who decided it,
when, and the mark it started from are facts the system keeps, so they are read
only. A role that may read the register but not corrections sees a student's
history without the decided corrections, and the other way round.
"""
from vs_rbac.field_registry import FieldSpec, register_fields

_TENANT = "TENANT"

#: The surfaces that emit the register's fields. Each carries the field-access
#: mixin and is rendered by the deep payload case.
SURFACES = (
    "schools.vs_attendance.serializers.RegisterSerializer",
    "schools.vs_attendance.serializers.RegisterRowSerializer",
    "schools.vs_attendance.serializers.MarkChangeSerializer",
    "schools.vs_attendance.serializers.MarkWriteSerializer",
    "schools.vs_attendance.serializers.HubSerializer",
    "schools.vs_attendance.serializers.HubAbsentRowSerializer",
    "schools.vs_attendance.serializers.TeacherHubSerializer",
    "schools.vs_attendance.serializers.TeacherCardSerializer",
    "schools.vs_attendance.serializers.StudentAttendanceSerializer",
    "schools.vs_attendance.serializers.StudentDaySerializer",
    "schools.vs_attendance.serializers.StudentAbsenceSerializer",
    "schools.vs_attendance.serializers.StudentChangeSerializer",
    "schools.vs_attendance.serializers.ReportSerializer",
    "schools.vs_attendance.serializers.ReportRangeSerializer",
    "schools.vs_attendance.serializers.ReportClassRowSerializer",
    "schools.vs_attendance.serializers.ClassChronicSerializer",
    "schools.vs_attendance.serializers.ReasonCodeSerializer",
)

#: The surfaces that emit a lesson register's fields. Those the hubs, the
#: student's tab and the report nest are rendered by the register-resource
#: surface that holds them.
LESSON_SURFACES = (
    "schools.vs_attendance.serializers.LessonRegisterSerializer",
    "schools.vs_attendance.serializers.LessonRowSerializer",
    "schools.vs_attendance.serializers.LessonChangeSerializer",
    "schools.vs_attendance.serializers.LessonMarkWriteSerializer",
    "schools.vs_attendance.serializers.LessonListRowSerializer",
    "schools.vs_attendance.serializers.LessonCardSerializer",
    "schools.vs_attendance.serializers.LessonMissedRowSerializer",
    "schools.vs_attendance.serializers.LessonAbsenceSerializer",
)

#: The surfaces that emit a staff register's fields.
STAFF_SURFACES = (
    "schools.vs_attendance.serializers.StaffGroupSerializer",
    "schools.vs_attendance.serializers.StaffRowSerializer",
    "schools.vs_attendance.serializers.StaffMarkWriteSerializer",
)

#: The surfaces that emit a correction request's fields.
CORRECTION_SURFACES = (
    "schools.vs_attendance.serializers.CorrectionSerializer",
    "schools.vs_attendance.serializers.CorrectionCreateSerializer",
    "schools.vs_attendance.serializers.CorrectionDecisionSerializer",
    "schools.vs_attendance.serializers.StudentCorrectionSerializer",
)


def register():
    """Publish the register's field declarations to the Field Access registry."""
    register_fields(
        "attendance",
        "register",
        surfaces=SURFACES,
        fields=(
            FieldSpec("mark", "Mark", group="Marks", scope=_TENANT, sort_order=10,
                      api_names=("mark", "from_mark", "to_mark"),
                      description="Present, late, absent or excused, on a row and "
                                  "in the history of a change."),
            FieldSpec("arrived_at", "Arrival time", group="Marks", scope=_TENANT,
                      sort_order=20,
                      description="When a late student arrived."),
            FieldSpec("reason_code", "Excused reason", group="Marks", scope=_TENANT,
                      sort_order=30, api_names=("reason_code", "reason_label"),
                      description="Why an absence is excused, from the school's "
                                  "own list of reasons."),
            FieldSpec("note", "Mark note", group="Marks", scope=_TENANT,
                      sort_order=40,
                      description="The teacher's working note beside a mark, such "
                                  "as \"phoned in\"."),
            FieldSpec("notice_state", "Guardian notice state", group="Guardian notices",
                      scope=_TENANT, sort_order=10, writable=False,
                      api_names=("notice_state", "notice_sent_at"),
                      description="Whether the absence email reached the guardian."),
            FieldSpec("counts", "Register counts", group="Summary", scope=_TENANT,
                      sort_order=10, writable=False,
                      api_names=("counts", "submit_preview", "kpis", "student_count",
                                 "trend", "by_level", "chronic", "school_days",
                                 "used_this_term"),
                      description="Present, late, absent, excused and unmarked "
                                  "totals, the attendance rate on the hub, a "
                                  "student's term and the reports (weekly trend, "
                                  "by level, below the threshold), how many "
                                  "guardians submitting would email, and how "
                                  "many marks used each excused reason."),
            FieldSpec("status", "Register status", group="Summary", scope=_TENANT,
                      sort_order=20, writable=False,
                      api_names=("status", "not_taken"),
                      description="Not started, in progress, ready, submitted or "
                                  "closed, and the classes whose register has "
                                  "not been taken."),
            FieldSpec("taken_by", "Who took the register", group="Summary",
                      scope=_TENANT, sort_order=30, writable=False,
                      api_names=("taken_by", "submitted_at", "submitted_by_name"),
                      description="Who submitted the register, when, and on whose "
                                  "behalf."),
        ),
    )
    register_fields(
        "attendance",
        "correction",
        surfaces=CORRECTION_SURFACES,
        fields=(
            FieldSpec("from_mark", "Mark before", group="Request", scope=_TENANT,
                      sort_order=10, writable=False,
                      description="The mark the student had when the correction "
                                  "was asked for."),
            FieldSpec("to_mark", "Mark asked for", group="Request", scope=_TENANT,
                      sort_order=20,
                      description="Present, late, absent or excused."),
            FieldSpec("arrived_at", "Arrival time", group="Request", scope=_TENANT,
                      sort_order=30,
                      description="When a student asked to be marked late "
                                  "arrived."),
            FieldSpec("reason_code", "Reason", group="Request", scope=_TENANT,
                      sort_order=40, api_names=("reason_code", "reason_label"),
                      description="Why the correction is asked for, from the "
                                  "school's own list of reasons."),
            FieldSpec("note", "Note", group="Request", scope=_TENANT,
                      sort_order=50,
                      description="What happened, in the words of whoever asked."),
            FieldSpec("requested_by", "Who asked", group="Request", scope=_TENANT,
                      sort_order=60, writable=False,
                      api_names=("requested_by_name", "requested_at"),
                      description="Who filed the correction and when."),
            FieldSpec("status", "Status", group="Decision", scope=_TENANT,
                      sort_order=10, writable=False,
                      description="Waiting, approved or rejected."),
            FieldSpec("decided_by", "Who decided", group="Decision", scope=_TENANT,
                      sort_order=20, writable=False,
                      api_names=("decided_by_name", "decided_at"),
                      description="Who approved or rejected the correction and "
                                  "when."),
            FieldSpec("decision_note", "Decision note", group="Decision",
                      scope=_TENANT, sort_order=30,
                      description="Why the correction was approved or rejected."),
        ),
    )
    register_fields(
        "attendance",
        "lesson",
        surfaces=LESSON_SURFACES,
        fields=(
            FieldSpec("mark", "Lesson mark", group="Marks", scope=_TENANT, sort_order=10,
                      api_names=("mark", "from_mark", "to_mark"),
                      description="Present, late or absent from a lesson, on a row "
                                  "and in the history of a change."),
            FieldSpec("arrived_at", "Lesson arrival time", group="Marks", scope=_TENANT,
                      sort_order=20,
                      description="When a student who was late to a lesson arrived."),
            FieldSpec("note", "Lesson note", group="Marks", scope=_TENANT,
                      sort_order=30,
                      description="The teacher's working note beside a lesson mark."),
            FieldSpec("morning_mark", "Morning mark beside a lesson", group="Marks",
                      scope=_TENANT, sort_order=40, writable=False,
                      description="The student's mark in the class register of the "
                                  "same day, shown beside the lesson mark. A role "
                                  "that may not read the mark on the class register "
                                  "never sees it here either."),
            FieldSpec("status", "Lesson register status", group="Summary", scope=_TENANT,
                      sort_order=10, writable=False,
                      description="Not started, in progress, ready, submitted or "
                                  "closed, for a lesson on the lessons list, a "
                                  "teacher's day and the lesson register."),
            FieldSpec("counts", "Lesson register counts", group="Summary", scope=_TENANT,
                      sort_order=20, writable=False,
                      description="Present, late, absent and unmarked totals of a "
                                  "lesson register, and with the lesson mark the "
                                  "figures of lessons missed on the hub and in "
                                  "the report."),
            FieldSpec("taken_by", "Who took the lesson register", group="Summary",
                      scope=_TENANT, sort_order=30, writable=False,
                      api_names=("taken_by", "submitted_at", "submitted_by_name"),
                      description="Who submitted the lesson register and when."),
        ),
    )
    register_fields(
        "attendance",
        "staff",
        surfaces=STAFF_SURFACES,
        fields=(
            FieldSpec("mark", "Staff mark", group="Marks", scope=_TENANT, sort_order=10,
                      description="Present, late or absent, on a member of staff's "
                                  "row."),
            FieldSpec("arrived_at", "Staff arrival time", group="Marks", scope=_TENANT,
                      sort_order=20,
                      description="When a member of staff who was late arrived."),
            FieldSpec("note", "Staff mark note", group="Marks", scope=_TENANT,
                      sort_order=30,
                      description="The administrator's working note beside a "
                                  "staff mark."),
            FieldSpec("status", "Staff register status", group="Summary", scope=_TENANT,
                      sort_order=10, writable=False,
                      description="Not started, in progress, ready, submitted or "
                                  "closed, for a branch's staff register."),
            FieldSpec("counts", "Staff register counts", group="Summary", scope=_TENANT,
                      sort_order=20, writable=False,
                      description="Present, late, absent, on leave and unmarked "
                                  "totals of a branch's staff register."),
            FieldSpec("taken_by", "Who took the staff register", group="Summary",
                      scope=_TENANT, sort_order=30, writable=False,
                      api_names=("submitted_at", "submitted_by_name"),
                      description="Who submitted the staff register and when."),
        ),
    )

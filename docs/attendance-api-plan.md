# M15 Attendance Management - API plan

**Design:** `docs/designs/M15 Attendance (standalone).html` - 9 screens plus two
phone frames, 18 collections, 33 actions, 1 field write.
**FRD:** none. `docs/frd/functional-requirements/` has no `15-*` folder. The
MRD (v2.105) lists Module 15 as Planned: "student and staff attendance with
corrections, reason codes, alerts, and summaries". This plan is the first
draft of the M15 FRD.
**Backend today:** nothing. No app, no model, no endpoint, no permission key.
`vs_rbac.capability_map` already names `attendance` as a capability with no
permission module, and `seed_actions` already carries the `mark` verb.

> **Approved 11 October 2026.**
>
> 1. **Two builds.** Build 1: daily register, corrections, settings and reason
>    codes, the student's Attendance tab, the admin and teacher hubs, reminders,
>    reports and exports. Build 2: lesson registers (R18-R21, the missed-lesson
>    KPI, cards and report table) and the staff register (R22-R23).
> 2. **A staff member with no branch cannot be marked** until they are given a
>    home branch. The staff register refuses the row with a sentence naming
>    the fix. Applies in build 2.
> 3. **Corrections are decided in this module**, in one step, by a holder of
>    `attendance.correction.approve`. No workflow template.
> 4. **The day closes on a school setting**, `attendance.edit_window_days`,
>    default 0 (same day only).
>
> Until the M15 FRD v1.0 is written (queued in `todo.md`, Documents owed),
> this plan is the contract the build is checked against.

---

## 1. The headline

1. **Everything is Absent, and that is the honest size of it.** No screen in
   this design is a flag away from working. It is a new app,
   `apps/schools/vs_attendance/`, and it lives under `apps/schools/` because
   every noun in it (class, student, guardian, term, lesson) is a school noun.
2. **Its foundations all exist.** Classes and class teachers (M13), placement
   and suspension (M11, with `as_at` history), primary guardians with email
   (M11), terms (M13), closed days (`CalendarEvent.closes_school`, M14),
   published timetables and slot teachers (M14), approved leave and exit dates
   (M12), guardian email (`vs_notifications`), CSV (`vs_exports`), settings
   (`vs_config`). Attendance reads every one of them and owns none.
3. **RBAC has no "only my own class" scope.** A key answers whether you may
   take registers, not which. The design needs both: Mr. Eze takes JSS1 A, the
   registrar takes any class on a teacher's behalf. So the module splits the
   key (section 4) and enforces "your own class" as a domain rule.

---

## 2. Screen → data → endpoint

All paths sit under `/v1/attendance/`. Every row is **Absent**. Shared chrome
(branch switcher, term pills) is **Served** by `GET /v1/tenants/branches/` and
`GET /v1/academics/sessions/` with its terms.

| Screen | What it reads / writes | Endpoint |
|---|---|---|
| **Today (admin hub)** | 6-7 KPIs (rate, absent, late, excused, not taken, corrections waiting, missed a lesson); not-taken table with Remind; absent-today table with reason and guardian notice state; missed-a-lesson cards; first pending correction | `GET today/?date=&branch=` · `POST registers/{class}/{date}/remind/` |
| **Today (teacher hub)** | "My registers today": own class's morning register + own lessons, each with status | `GET today/mine/?date=` |
| **Holiday view** | Date is closed and why | carried in every day-scoped response as `closed: {reason}` |
| **Daily register** | Class, teacher, count, status chip, taken-by / on-behalf line, roster rows (name, adm no., mark, arrival time, reason, note, suspended lock), change history, submit preview (how many guardians will be emailed) | `GET registers/{class}/{date}/` · `PUT …/marks/{student}/` · `POST …/fill-present/` · `POST …/submit/` |
| **Excused / Late drawers** | Reason codes (active only), arrival time, note; "Other" needs a note | payload of `PUT …/marks/{student}/` |
| **Request a correction drawer** | New mark, reason, note | `POST corrections/` |
| **Lesson registers list** | Today's lessons in levels with lesson marking on: period, class, subject, teacher, marked state | `GET lessons/?date=&branch=` |
| **Lesson register** | Roster with the morning mark beside each row; P / L / A only | `GET lessons/{slot}/{date}/` · `PUT …/marks/{student}/` · `POST …/submit/` |
| **Corrections** | Waiting queue (student, class, adm no., date, from → to, reason, note, requested by/at); decided list (approved/rejected, by, at) | `GET corrections/?status=` · `POST corrections/{id}/approve/` · `POST corrections/{id}/reject/` |
| **Staff register** | Staff grouped by branch plus School-wide; role; P / L / A; on-leave rows locked with a link to the leave record; notes (exit date, arrival time) | `GET staff-registers/{date}/?branch=` · `PUT …/marks/{staff}/` · `POST …/fill-present/` · `POST …/submit/` |
| **Student record, Attendance tab** | Term counts (present/late, absent, excused, not taken, days open), rate, a cell per school day, absences with reason and notice state, lesson absences, every changed mark | `GET students/{id}/?term=` |
| **Summaries & reports** | Range note; weekly trend; by class; by level; chronic list below threshold; registers not taken; lessons missed; three CSV exports | `GET reports/?term=&branch=` · exports via `vs_exports` |
| **Attendance settings** | Lesson marking per level; reason codes (list, add, retire, restore, usage count); guardian notices on/off; chronic threshold | `GET/PATCH settings/` · `GET/POST reason-codes/` · `POST reason-codes/{id}/retire/` · `…/restore/` |
| **Phone frames** | Same register and today reads | no extra endpoints |
| **Nav badge** | Pending corrections count | `pending_corrections` in `today/` |

---

## 3. FRD delta (this is the FRD's first draft)

There is no FRD to disagree with, so the whole design is list 1. These are the
rules the screens commit the backend to, written as requirements. Each is
something a screen states in its own copy, which is why none is a judgement
call.

### 3.1 Requirements the design implies

**Marks and the day**
- R1. A mark is one of Present, Late, Absent, Excused. Late counts as present
  and is also counted separately.
- R2. Late carries an arrival time and an optional note. Excused carries a
  reason code and a note, required when the code is "Other".
- R3. The rate is present (including late) over present + absent + excused.
  Days with no register are excluded from the denominator, never counted as
  absences. Holidays and closed days are blank.
- R4. A day closed by a calendar event (`closes_school`) has no register due
  and none is reported missing, for the whole school or the branch the event
  names.
- R5. A suspended student appears on the register, locked, and is kept out of
  every count. Status is read as at the register's date.
- R6. A register lists the students placed in the class on that date. A class
  with nobody placed shows "no register to take".

**Taking a register**
- R7. A register cannot be submitted with an unmarked row. "Everyone else
  present" fills only unmarked, unlocked rows and submits nothing.
- R8. Submitting records who and when. When the submitter is not the class
  teacher, the register says it was taken on their behalf, and why when the
  class teacher is on approved leave.
- R9. After submission and until the day closes, the submitter can still change
  a row. Every change is kept as history ("changed from Absent to Late by …").
- R10. Once the day closes, a mark changes only through a correction request
  decided by an attendance administrator. The original mark is never deleted.
- R11. A teacher takes the register of the class they lead and marks the
  lessons they teach. A holder of the "any class" key takes any class's
  register in their branches.

**Guardian notices**
- R12. On submission, one email per absent child per day goes to the primary
  guardian, and only when the school has notices on. Late and Excused send
  nothing. A child already noticed that day is not noticed again.
- R13. A guardian with no email is recorded as "No email on file", visible on
  the hub and the student record.
- R14. The submit step previews how many guardians will be emailed.
- R15. A same-day change away from Absent, and an approved correction, email a
  correction notice to the guardian who received the absence notice.

**Corrections**
- R16. A correction names the mark, the requested mark, a reason code and a
  note. Approval changes the mark and keeps the old one in history; rejection
  leaves the mark and keeps the request and decision on the student's record.
- R17. The hub and the nav show how many corrections are waiting.

**Lessons**
- R18. Lesson marking is a per-level switch. Off means a class in that level
  has one register a day.
- R19. A class has lessons to mark only when its timetable is published; the
  lessons of a day are its timetable slots for that weekday.
- R20. A lesson register is P / L / A, sends nothing to anyone, and shows each
  student's morning mark beside the row.
- R21. A student present in the morning and absent from a lesson is listed as
  "missed a lesson". Neither mark overwrites the other, and it is not counted
  as an absence.

**Staff**
- R22. The staff register records what an administrator saw; there is no
  clock-in. Marks are P / L / A, with an arrival time for Late.
- R23. A person on approved leave that day is filled in as On leave and cannot
  be marked over. Staff absences notify nobody.

**Reminders**
- R24. "Remind" sends the class teacher an in-app and email reminder for a
  register not yet taken, once per class per day, and the row then reads
  "Reminded".

**Reports**
- R25. Reports cover one term: weekly trend, by class, by level, chronic
  absence, registers not taken, lessons missed. All are drawn only from
  registers taken.
- R26. The chronic list holds students below the school's threshold. The
  threshold is the school's number (85, 90 or 95 in the design).
- R27. By class, chronic absence and registers not taken export as CSV.

**Settings**
- R28. Reason codes are the school's own list. A code can be added, retired
  (kept on marks that used it, offered on no new mark) and restored. Each
  shows how many marks used it this term.
- R29. Guardian notices, lesson marking and the threshold are school-wide
  settings that apply to every branch.

**Branch**
- R30. At a school with one branch the branch column becomes Level and the
  switcher is absent; at several, the hub, staff register and reports filter by
  branch.

### 3.2 Things the design shows that the code cannot serve as drawn

- **D1. "Account locked after three failed sign-ins. Not an attendance fact."**
  The staff register shows a person's sign-in lockout. That is `vs_user`
  security state, and putting it on a register any branch admin can open leaks
  it. Proposed: do not serve it. The note exists in the design to say it is
  *not* an attendance fact, so leaving it out matches the design's intent.
- **D2. "Add a code: whether it counts as an authorised absence."** The toast
  promises a second attribute that no screen reads or uses in any count.
  Proposed: store `counts_as_authorised` (default yes) so the add form can
  carry it, and leave it out of every calculation until a screen uses it.
- **D3. Prototype control "Date".** The design switches dates freely. A
  register for a future date cannot be taken: refused with a sentence.

### 3.3 Backend-only requirements no screen shows (for you to wave through)

- Permission keys and a 403 per verb (section 4); another tenant's register
  answers 404.
- An audit event for every submit, change, correction decision and settings
  change.
- Field Access registration for the mark note, excused reason, arrival time,
  guardian email on the student record, and staff notes (section 7).
- Register branch is the class's branch. A class with no branch at a school
  with several is refused with "Give this class a branch before taking its
  register".

**The FRD needs a version before the backend is built: M15 v1.0, a new
document**, written from section 3 once you approve. The MRD gets a minor bump
(Module 15 Planned → In progress, backend Partial) after the build.

---

## 4. Permission keys (new module `attendance`, mapped to capability `attendance`)

| Key | Verb | Default roles |
|---|---|---|
| `attendance.register.view` | read registers, hub, student tab | admin, branch admin, teacher |
| `attendance.register.mark` | take your own class / lesson | teacher |
| `attendance.register.mark_any` | take any class's register | admin, branch admin |
| `attendance.correction.create` | request a correction | admin, branch admin, teacher |
| `attendance.correction.approve` | decide one | admin, branch admin |
| `attendance.staff.view` / `attendance.staff.mark` | staff register | admin, branch admin |
| `attendance.report.view` / `attendance.report.export` | reports, CSV | admin, branch admin |
| `attendance.settings.update` | settings, reason codes | admin |
| `attendance.register.remind` | send a reminder | admin, branch admin |

Seeded in `seed_school_permissions` with a backfill phase for tenants
provisioned before the keys existed. No `pending_tenant_surface`: attendance is
a live-school activity, and a school that has not gone live has no students
placed.

---

## 5. Models (all `_Owned`, tenant-scoped)

| Model | Holds |
|---|---|
| `ReasonCode` | name, `counts_as_authorised`, `retired_at`, order. Seeded with the design's seven per tenant. |
| `ClassRegister` | class, date, branch (from the class), session, term, `submitted_at`, `submitted_by`, `on_behalf_of`. Unique (class, date). |
| `StudentMark` | register, student, mark, `arrived_at`, reason code, note, marked by/at, `notice_state` (sent / no email / off / none), `notice_sent_at`. Unique (register, student). |
| `MarkChange` | append-only: mark, from, to, by, at, how (same day / correction). |
| `CorrectionRequest` | mark, from, to, reason code, note, requested by/at, status, decided by/at, decision note. |
| `LessonRegister` / `LessonMark` | timetable slot, date, submitted by/at; P / L / A. |
| `StaffDay` / `StaffMark` | branch, date, submitted; staff, mark, arrival time. |
| `RegisterReminder` | class, date, sent by/at. Unique (class, date). |

Settings in `vs_config`, tenant-scoped: `attendance.guardian_notices` (on),
`attendance.chronic_threshold` (90), `attendance.lesson_marking_levels` (level
codes, empty), `attendance.edit_window_days` (0, meaning same day only).
Lesson marking is stored by level **code** because `Level` rows belong to a
session; a switch on the row would reset every school year.

Notification events: `attendance.absence_notice` (guardian, unregistered
recipient when the guardian has no login), `attendance.correction_notice`,
`attendance.register_reminder` (teacher).

Export datasets: `attendance.by_class`, `attendance.chronic`,
`attendance.not_taken`.

---

## 6. Not ours, and dead API

- **Student profile tabs** other than Attendance belong to M11. We serve one
  tab's read.
- **"View leave record"** links to M12's leave detail, which exists.
- **Dashboards (M25)** can read `today/` later; nothing is built for them now.
- **Dead API:** none, there is no existing API in this namespace.

---

## 7. Field Access

Registered in `vs_attendance/field_access.py`:

| Field | Read | Write | Note |
|---|---|---|---|
| Mark | yes | yes | the register itself |
| Arrival time | yes | yes | |
| Excused reason | yes | yes | |
| Mark note | yes | yes | carries things like "fever", health-adjacent |
| Guardian notice state | yes | no | server-owned |
| Guardian name and email (student tab, hub) | yes | no | aliases the M11 guardian fields; hidden there means hidden here |
| Correction note / decision note | yes | yes | |
| Staff mark, staff note | yes | yes / no | exit date and leave reason come from M12 and obey its switches |
| Rates and counts | yes | no | derived |

---

## 8. Open decisions

1. **Build order.** All nine screens in one build, or the daily register,
   corrections, settings, student tab, hub and reports first, with lesson
   registers and the staff register as a second build.
2. **School-wide staff on the staff register.** Mrs. Adaeze Nwankwo (Registrar)
   has no branch. Whose register is her mark on?
3. **Corrections: in this module, or through the workflow engine.**
4. **When a day closes.** Same day only, as a school setting.

---

## 9. As built (build 1)

Build 1 shipped as specified above, with these differences. The FRD is to be
written from this section where it disagrees with sections 2-7.

**Routes** (all under `/v1/attendance/`): `registers/<class>/<date>/`,
`…/marks/<student>/`, `…/fill-present/`, `…/submit/`, `…/remind/`;
`corrections/`, `corrections/<id>/approve/`, `corrections/<id>/reject/`;
`today/`, `today/mine/`; `students/<id>/`; `reports/`, `reports/export/`;
`settings/`; `reason-codes/`, `reason-codes/<id>/`, `…/retire/`, `…/restore/`.

**Differences from the plan, all deliberate:**

1. **Exports download straight away** (`reports/export/?table=by_class|chronic|not_taken&export=csv|xlsx|pdf`)
   rather than queueing in the Export Centre, whose datasets must be
   querysets. They reuse the finance report renderer and the report service,
   so a file and the screen cannot disagree. The design's "Export queued" toast
   becomes an immediate download.
2. **Two new action verbs**, `mark_any` and `remind`, were added to
   `seed_actions`.
3. **Settings and reason codes need a whole-school reach.** A branch-bound
   caller holding `attendance.settings.update` is refused
   (`SHARED_RECORD_READ_ONLY`), as the calendar's settings are.
4. **A correction is refused as stale** (`CORRECTION_STALE`) if the mark moved
   after the request was filed, and a requester may not decide their own
   (`SELF_DECISION`).
5. **The last live reason code cannot be retired** (`LAST_LIVE_REASON_CODE`).
6. **A change to Absent after submission emails the guardian** the same day,
   and a child already noticed that day by another register is not noticed
   again. An approved correction to Absent sends no absence notice: the day is
   over.
7. **Student day cells** carry `state` (MARKED, NOT_TAKEN, CLOSED, SUSPENDED,
   NOT_ON_ROLL) and the mark separately, so hiding the mark hides it
   everywhere. "School opened" counts the days the pupil was expected.
8. **Closed days honour calendar-event audiences**, so a closure for one level
   does not close another.
9. **The register note is an ordinary field**, not sensitive: it is the
   teacher's working note and stays correctable by whoever may mark.
10. **Attendance has its own audit module**, `ATTENDANCE`.

**Not built (build 2):** lesson registers (R18-R21) and the staff register
(R22-R23, plus decision 2). The lesson-marking switch is stored and validated
but nothing reads it yet.

**Decided after build 1:** a student's name and number on a register, the Today
screens, the student tab and the report files are not governed by the student
record's name switches. A register is unusable without names: a teacher could
not tell two Emekas apart.

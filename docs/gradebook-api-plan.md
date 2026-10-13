# M16 Gradebook & Assessments - API plan

**Design:** `docs/designs/M16 Gradebook (standalone).html` - 13 screens (hub,
teacher hub, score sheet, broadsheet, moderation, approvals and publishing,
fee holds, report cards and release, report card, a student's results, who
takes what, performance analytics, assessment setup), a phone frame, three
drawers and a confirm modal. 56 collections, 51 actions, 2 field writes,
422 bindings.
**Brief:** `~/Downloads/August_Sprint/M16_Gradebook_Assessments_Design_Prompt.md`
(outside git). It records decisions the markup cannot show, and they are
carried below where the design is silent: holds are only ever placed by
hand; the guardian email carries nothing from the result; ties share a
position and the next is skipped; scores are whole marks.
**FRD:** none. `docs/frd/functional-requirements/` has no `16-*` folder. The
MRD (v2.105) lists Module 16 as V2, Planned, "No mounted domain app", with ten
capabilities (assessment types and weighting, assessment schedules, score
entry and bulk upload, validation and moderation, grading scales,
CA and examination totals, teacher comments and approvals, report-card
generation, publication and locking, analytics). **This plan is the first
draft of the M16 FRD.**
**Backend today:** nothing. No app, model, endpoint or permission key.
`vs_config` already sells `gradebook` as a module with empty Core / Plus /
Advanced bands, `vs_rbac.capability_map` names it as a capability with no
permission module, and `seed_actions` already describes the `enter`,
`approve` and `publish` verbs with results in mind.

> **Approved 13 October 2026**, every decision as recommended:
>
> 1. **Who takes what belongs to M13** (`vs_academics`), as a record of the
>    class per term, read by the gradebook and by any module that needs it.
> 2. **A head is named on the academic department** (M13). A sheet is
>    moderated when its class's programme moderates and its subject's
>    department has a head; that head moderates it.
> 3. **An amendment carries the corrected marks.** On approval only the
>    amended student's card is re-issued (a new version, the old kept); every
>    classmate keeps the position and class figures they were published with.
> 4. **"Cannot sign in" shows only to a hub reader who can manage staff
>    accounts**; everyone else sees the entered count.
> 5. **Two builds.** Build 1: setup, sheets, moderation, compiling, approval
>    and publishing, holds, release, report cards, the student tab, the hubs
>    and reminders, with B2, B3, B4 and B6. Build 2: amendments, analytics and
>    exports, bulk upload (B7, B8), the cumulative third-term card and its
>    promotion line (B1, B5).
>
> The section 3.4 backend-only items are accepted as written. Until the M16
> FRD v1.0 is written (queued in `todo.md`, Documents owed), this plan is the
> contract the build is checked against.

---

## 1. The headline

1. **Everything is Absent.** No screen is a flag away from working. It is a
   new app, `apps/schools/vs_gradebook/`, mounted at `/v1/gradebook/`, under
   `apps/schools/` because every noun in it (class, subject, term, student,
   guardian, class teacher) is a school noun.
2. **Most of what a result hangs on exists.** Classes, levels, programmes and
   departments (M13); terms with dates (M13); subject offerings marked core
   or not (M13); the main teacher and assistants of a subject in a class
   (M12, `TeachingAssignment` LEAD / ASSISTANT); the class teacher (M13,
   set from M12); the class roll as at a date (M11, via attendance's
   `roster_as_at`); published exam papers with date and sitting (M14,
   `ExamSlot`); the primary guardian and their email (M11); a teacher's exit
   date and whether they can sign in (M12, `vs_user`); guardian and staff
   email (`vs_notifications`); settings (`vs_config`); the export renderer
   (`vs_finance.exports`). The gradebook reads every one and owns none.
3. **Four facts the screens need have no source today**, and they are the
   real decisions in this plan (section 3.2): which student takes an
   elective, who heads a department, how long a family's oldest invoice has
   been unpaid, and a planned results day. Two more exist only behind
   functions scoped to a signed-in user (a pupil's attendance totals, a
   class's days with no register) and need a small system-side read.
4. **The approval chain is decided inside the module**, as attendance
   corrections are. The brief already says why: the workflow engine routes to
   roles, and steps 1 and 3 belong to a person named on the class (the main
   teacher, the class teacher). Moderation and the principal's decision use
   keys plus a domain rule, not workflow templates.

---

## 2. Screen → data → endpoint

All paths sit under `/v1/gradebook/`. Every row is **Absent**. Shared chrome
(branch switcher, term pills) is **Served** by `GET /v1/tenants/branches/`
and `GET /v1/academics/sessions/` with its terms.

| Screen | What it reads / writes | Endpoint |
|---|---|---|
| **Results hub (admin, principal, bursar)** | Term title, planned results day, term end; 5 KPIs (sheets in of due, classes compiled, awaiting approval, published, held); by-class table (class, branch or programme, subjects in, stage, class teacher); late sheets with reason and Remind | `GET hub/?term=&branch=` · `POST sheets/{class}/{subject}/remind/?term=` |
| **Teacher hub** | My sheets (class · subject, entered of roster, exam paper, state); my duties (compile as class teacher, moderate as head of department) | `GET hub/mine/?term=` |
| **Score sheet** | Header (subject, class, term, main teacher, assistants, exam paper, status, elective note with takers of roll), returned banner (by, note), subject tabs, progress, save state, grid of components × students, computed total, %, grade, remark, obtainable note | `GET sheets/{class}/{subject}/?term=` · `PUT …/scores/` (batch of cells, autosave) · `POST …/submit/` |
| **Bulk upload** | Download the sheet with the class in it; upload; check every row; confirm | `GET …/template/` · `POST …/import/` (preview) · `POST …/import/{token}/confirm/` |
| **Phone sheet** | Same sheet, one student at a time | same endpoints |
| **Broadsheet** | Students × subjects totals, gaps with teacher named, average, position (ranking schools), compile state, attendance warning, submit gate | `GET classes/{class}/broadsheet/?term=` · `POST classes/{class}/submit/?term=` |
| **Compile drawer** | Subject totals and grades, overall average, the term's attendance, conduct ratings, class teacher's comment | `GET classes/{class}/students/{student}/compile/?term=` · `PUT` same |
| **Moderation** | Moderated sheets: average, highest and lowest, pass rate, marked of roster, comparison with other arms; approve, return with a note | `GET moderation/?term=&branch=` · `POST sheets/{class}/{subject}/approve/` · `…/return/` |
| **Approvals & publishing** | Compiled classes (awaiting, published), meta, held students and who held them; return with note; principal's comments; publish confirmation counts | `GET approvals/?term=&branch=` · `POST classes/{class}/return/` · `GET/PUT classes/{class}/principal-comments/` · `GET classes/{class}/publish/preview/` · `POST classes/publish/` |
| **Amendments** | Pending and decided amendments; raise one; approve | `GET/POST amendments/` · `POST amendments/{id}/approve/` · `…/reject/` |
| **Fee holds** | Students in this term's classes, class, balance and oldest unpaid from Finance with an as-of time, hold state and who placed it, hold and lift; no scores | `GET holds/?term=&class=&branch=` · `POST holds/` · `POST holds/{id}/lift/` |
| **Report cards & release** | Per class: student, primary guardian, result state, email state, held count; print class | `GET release/{class}/?term=` · `GET report-cards/{class}/?term=` (every released card) |
| **Report card** | Letterhead, facts, subject table with components, class stats, totals, position, attendance, ratings and key, both comments with names, grading key, next term begins | `GET report-cards/{class}/{student}/?term=` |
| **A student's results** | Header, terms with avg, position, release state, subject rows, calm line for unpublished; history of amendments and holds | `GET students/{id}/results/` |
| **Who takes what** | Non-core subjects of the class's level, takers of roll per subject, the grid | `GET electives/{class}/?term=` · `PUT electives/{class}/?term=` (see Q1 for where it lives) |
| **Performance analytics** | By subject and class; same subject across arms; term on term by student; top of each class; two exports | `GET analytics/?term=&branch=` · `GET analytics/export/?table=subjects\|movers&export=csv\|xlsx` |
| **Assessment setup** | Structures per programme (components, max, kind, total, lock), scales per programme (bands, check, lock), report card options, principal's remark templates | `GET/POST structures/` · `GET/PATCH structures/{id}/` · `POST …/copy/` · `POST …/activate/` · same four for `scales/` · `GET/PATCH settings/` · `GET/PUT remark-templates/` |

---

## 3. FRD delta (this is the FRD's first draft)

There is no FRD to disagree with, so the whole design is list 1. Each
requirement below is something a screen, or the brief behind it, states in
its own copy.

### 3.1 Requirements the design implies

**Setup**
- R1. An **assessment structure** belongs to a programme: components, each
  with a name, a short name, a whole-number maximum and whether it is the
  examination (at most one is). The maxima total 100; a structure that does
  not cannot be put in use.
- R2. A **grading scale** belongs to a programme: bands, each with a lowest
  mark, a grade, a remark and whether it is a fail. The lowest band starts at
  0 and every band's lowest mark is distinct, so the bands cover 0 to 100
  with no gap and no overlap; a scale that does not is refused with the
  reason the editor shows. Pass rate means the share of students not in a
  fail band.
- R3. Structures and scales apply to the whole school by default (a shared
  record, null branch). The API accepts one branch, and a class uses its
  branch's own structure or scale where one exists, else the school-wide one.
  The design draws no "Applies to" field: see B4.
- R4. A structure or scale used by a published result is read-only. "Copy to
  edit" makes a draft copy; putting the copy in use replaces the old one for
  every class-term that has no score yet. The published result keeps what it
  was published with.
- R5. A class-term's structure is fixed the first time a score is entered for
  that class in that term, so every sheet of one class shares its columns. An
  edit to a structure in use but not yet published is refused when it would
  invalidate an entered score (a maximum lowered below a mark, a component
  with marks removed), naming the sheet.
- R6. **Report card options** are school-wide settings: class average,
  highest and lowest (on); position in class (on); conduct and skills ratings
  (on); the 1 to 5 key (on). The trait list is a setting too, defaulting to
  the design's ten, with no screen to edit it yet.
- R7. **Principal's remark templates**, one per band of overall average,
  editable: 70 and above, 55 to 69, 40 to 54, below 40, with the design's four
  sentences as defaults.
- R8. **Moderation** is a school setting naming the programmes that moderate
  (empty means none). A school that moderates nothing has no moderation menu.

**Who takes what**
- R9. A subject is core where its level offering says so, else where the
  subject says so (M13's existing rule). Core subjects are everyone's. For a
  non-core subject, the students who take it are recorded per class per
  term. A student not recorded is on no sheet, no average and no position
  for it. The grid shows each subject's "N of M".
- R10. It is set by the class's class teacher or a holder of the any-class
  key, and once a sheet for that subject has a score, removing a taker who has
  marks is refused naming them.

**Score sheets**
- R11. A sheet is one subject for one class for one term. A class's due
  sheets are every core subject its level offers plus every non-core subject
  with at least one taker.
- R12. The header names the main teacher (the LEAD assignment), the
  assistants, and the examination paper's date and sitting from the
  published exam slot for that class and subject in the term, or "-" where
  there is none.
- R13. The students on a sheet are those placed in the class as at the
  earlier of today and the term's end, and who take the subject. A suspended
  student is on it.
- R14. A cell is blank, a whole mark from 0 to the component's maximum, ABS
  or Excused. A mark over the maximum is refused in its cell naming the
  maximum ("Max 20"). A blank is not a zero.
- R15. ABS counts as zero and its maximum stays in what the student could
  obtain. Excused leaves the component out of what they could obtain. Total
  is the sum of marks; percentage is total over obtainable; grade and remark
  come from the band the unrounded percentage falls in. A row with a blank
  has no total, grade or remark. A row with every component excused has no
  grade and is left out of the student's average.
- R16. Where a component is excused, the row says so: "61 out of 80
  obtainable, graded at 76%".
- R17. The main teacher enters. Assistants, the class teacher and the
  moderating head read. A sheet saves as it is typed. It cannot be submitted
  while a cell is blank; submitting locks it for the teacher.
- R18. A returned sheet reopens for the main teacher with the returner's name
  and note shown above the grid. Nothing is deleted.

**Moderation**
- R19. A submitted sheet goes to moderation when its class's programme
  moderates and its subject's department has a head (see Q2); otherwise it is
  in. The submit confirmation says which.
- R20. The moderation queue shows each sheet with its main teacher, marked of
  roster, class average (mean percentage, 2 dp), highest and lowest totals,
  pass rate, status, and the other arms in the term that offer the same
  subject. The head approves (the sheet reads Approved and is in) or returns
  it with a note, which is required. Only a Submitted sheet can be decided.

**Compiling the class**
- R21. The broadsheet shows every student against every subject's total, "-"
  for a subject the student does not take and "·" for one not yet in, then
  overall average (2 dp), position where the school ranks, and whether the
  child's comment is written. A subject not in is named with its teacher.
- R22. Overall average is the mean of the percentages of the subjects the
  student takes whose sheet is in.
- R23. Position is within the class, by overall average, highest first.
  Averages equal to two decimal places share a position and the next is
  skipped: 1st, 2nd, 3rd, 3rd, 5th. "Of N" is the number on the class roll.
  A school that does not rank shows no position anywhere, including the
  report card, the student tab and analytics.
- R24. The broadsheet warns when the class has school days in the term so far
  with no register, and how many (from M15).
- R25. Compiling a child shows their subjects, overall average and the term's
  attendance (times opened, present, absent, from M15), the ratings where the
  school uses them (1 to 5 per trait), and the class teacher's comment, which
  is required to save. Next and previous step through the roll.
- R26. The class teacher submits the class once every due sheet is in and
  every student has a comment. It then awaits the principal.
- R27. A class's stage is Entering (a sheet is not in), Compiling (all in),
  Awaiting approval, or Published. A class returned by the principal is back
  in Compiling with the note shown on the broadsheet.

**Approval and publishing**
- R28. The approvals queue lists classes awaiting approval and published
  ones, each naming its held students and who held them. The approver
  returns a class to its class teacher with a required note, writes the
  principal's comments, and publishes.
- R29. Principal's comments are written per class per band: the approver
  picks the band's template and edits it. A student's card prints the
  comment for the band their overall average falls in, signed with the
  approver's name.
- R30. The publish confirmation names how many results will be released, how
  many guardians will be emailed, how many have no email on file, and each
  held student and who held them.
- R31. Publishing locks every score, comment and rating in the class and
  freezes each student's figures, grade, remark and position as published. A
  scale changed later changes nothing that was published. A change after
  publication is an amendment.
- R32. On publishing, every result not held is released, and each released
  child's primary guardian is emailed: the child's first name, the term, the
  school and branch with the school's phone, and that the report card is
  ready to collect from the school. No score, grade, position, comment,
  attachment or link. A guardian who is a user here also gets it in-app.
  Siblings get one email each. No email on file is recorded as such, never
  as notified.

**Fee holds**
- R33. The holds list shows the students in this term's classes with their
  class, balance and oldest unpaid date from Finance, labelled with the time
  it was read, and their hold state, who placed it, when and why. It never
  shows a score and never sorts or suggests by who "should" be held.
- R34. A hold needs a reason and can be placed until the student's result is
  released. A released result cannot be held, and the row says so rather
  than offering the action. A held result is approved and locked like the
  others; it is simply not released, printed or emailed.
- R35. Lifting a hold takes an optional note. If the class is published,
  lifting releases the result and sends the guardian email then.
- R36. No result is ever held automatically.

**Release and report cards**
- R37. The release list shows, per class, each student's primary guardian,
  result state (Not published, Held, Released) and email state (Emailed with
  date and time, Held - nothing sent, No email on file), and how many are held
  and left out.
- R38. A report card carries the school's name, logo and branch address with
  the school's phone; the term and session; the student's name, admission
  number, class and the number in class; per subject the component scores
  (ABS and Exc printed as themselves), total, grade, remark and, where the
  school shows them, the class average (1 dp), highest and lowest; total
  obtained and obtainable, overall average and position where the school
  ranks; times school opened, present and absent; the ratings and the key
  where the school uses them; the class teacher's comment and name; the
  principal's comment and name; the grading key; and "Next term begins" from
  the next term's start date, left off when there is none.
- R39. Printing a class returns every released student's card; held ones are
  left out and counted.
- R40. A student's Results tab lists every term with a published result for
  them, each with overall average, position where ranked, released date or
  "Published, not yet released", and per subject total, grade and remark. A
  term with nothing published is one calm line. History lists amendments
  (reason, who raised, who approved) and holds placed and lifted, with dates;
  nothing in it is deleted.

**The hubs**
- R41. The results hub shows the term, its planned results day and its end,
  the five counts, each class with subjects in, stage and class teacher, and
  every sheet not in with its teacher and the reason the platform knows:
  entered of roster, no main teacher assigned, the teacher's last working
  day, returned by whom, and (see Q4) that the teacher cannot sign in.
- R42. Remind sends the main teacher an in-app and email reminder now, once
  per sheet per day, and the row then reads Reminded. A teacher who cannot be
  reached is refused with a sentence saying why.
- R43. The teacher hub lists the sheets the caller is main teacher of, with
  entered of roster, exam paper and state, and their other duties: compiling
  the class they lead, and the moderation queue of the department they head.

**Amendments**
- R44. A published result changes only by amendment: what is corrected (a
  subject and a student), a reason, and the principal's approval. The
  published version is kept beside the new one. If the result was released,
  approval emails "an updated report card is ready". See Q3 for what the
  amendment carries and what it changes for classmates.

**Analytics**
- R45. Analytics read published results only. By subject and class: average,
  grade spread, pass rate, number marked. The same subject across the arms
  that offer it, with the gap between highest and lowest. Term on term by
  student: change in overall average against their most recent earlier
  published term; a student without one is not listed. Top of each class
  where the school ranks. The two tables export.

**Branch**
- R46. A branch-bound reader sees only their branches' classes. A whole-school
  reader filters by branch. At a school with one branch nothing carries a
  branch, and the class meta reads the programme instead.

### 3.2 Things the design shows that the code cannot serve as drawn

- **D1. "Account locked, so she cannot sign in."** That is `vs_user`
  security state. Attendance faced the same line and chose not to serve it.
  Here it is the reason a sheet is late and the design links it to Staff
  Management, so it is worth serving to someone who can fix it. **Q4.**
- **D2. The amendment drawer carries no corrected marks.** It picks a subject
  and a student and asks for a reason, and nowhere shows the new score, so
  the principal approves a reason, not a change. **Q3.**
- **D3. "Results to be released Tue 16 Dec."** Nothing stores a planned
  results day. Proposed: M14 gains a "Results day" calendar event type (one
  choice value), the hub prints the term's one, and the line is left off
  where there is none.
- **D4. "Oldest unpaid: 104 days."** The finance layer's `debtors()` port
  computes the oldest due date and then throws it away into an ageing bucket
  (0-30, 31-60, ...). Proposed: add `oldest_unpaid_on` to `DebtorRow`, an
  additive change to the FAL contract. Students with nothing owed are absent
  from that port and read as ₦0.00.
- **D5. Attendance figures.** A pupil's term counts and a class's days with no
  register come only from attendance functions scoped to a signed-in user.
  Proposed: one system-side read in `vs_attendance` returning both for a
  class and term, used by the broadsheet, compile drawer and report card.
- **D6. "Head of Sciences."** An academic department has no head. The staff
  organogram has department heads, but nothing ties an organogram node to an
  academic department. **Q2.**
- **D7. "Who takes what."** No record holds it. The design says "the FRD will
  decide which module owns it". **Q1.**
- **D8. Past terms in the prototype show grades only.** That is the
  prototype's own note ("nothing in this prototype holds last term's
  sheets"). A term published in XVS has its totals and serves them; a term
  from before the school joined XVS has nothing, and the term-on-term table
  is empty until a second term is published.

### 3.3 What the brief asks for and the design does not draw

The design outranks the brief, so none of these is assumed. Each is cheap in
the API unless marked.

- **B1. Third-term cumulative report card**: each subject's first-, second-
  and third-term totals beside the annual average. The portal designs show it
  as well. *A second layout; not small.*
- **B2. The principal returns to one subject's teacher**, not only to the
  class teacher.
- **B3. Publish several classes at once.**
- **B4. "Applies to" on structures and scales** (whole school or one branch).
  R3 builds it in the API; the screen draws no field.
- **B5. A promotion line on a third-term card**, only where M11's promotion
  run recorded an outcome. The gradebook never decides promotion.
- **B6. Fee holds filter by class and by branch.**
- **B7. Bulk upload checks**: unknown admission number, a student not in the
  class or not taking the subject, a mark over the maximum, and a row that
  would replace a score already entered. Nothing saved until confirmed.
- **B8. Analytics export as Excel** as well as CSV.

### 3.4 Backend-only requirements no screen shows (for you to wave through)

- A key per verb and a 403 without it; another tenant's class, sheet or
  student answers 404.
- An audit event for every submit, return, moderation decision, class
  submission, publication, hold, lift, amendment and settings change.
- Field Access registration (section 7).
- An approver may **reject** an amendment with a note, so a wrong one can be
  closed without changing anything.
- The main teacher gets an in-app notice when their sheet is returned, and
  the class teacher when their class is returned. Otherwise nobody learns of
  a return until they open the screen.
- The result's branch is the class's branch.
- No `pending_tenant_surface`: results are a live-school activity.
- **For M28 (Parent Portal):** its FR-006 says a guardian sees *published*
  results. The gradebook's word for what a family may see is *released*:
  published and not held. M28 should read released, or a held child's
  result shows in the portal while the paper card is withheld.

**The FRD needs a version before the backend is built: M16 v1.0, a new
document** (folder `16-gradebook-and-assessments`), written from section 3
once you approve. Per the documents queue rule it is queued in `todo.md`
rather than written in this session. The MRD gets a minor bump after the
build (Module 16 Planned to In progress, backend Partial, `vs_gradebook` in
ownership).

---

## 4. Permission keys (new module `gradebook`, mapped to capability `gradebook`)

Prebuilt school roles with defaults are School Admin, Branch Admin and
Teacher. There is no prebuilt Principal or Bursar: the brief's principal is
"a role Brightfield built for itself", and Finance Admin replaced Bursar. So
the approver's keys default to School Admin, and a school grants them to the
Principal role it builds.

| Key | Verb | Default roles |
|---|---|---|
| `gradebook.hub.view` | results hub, late list | school admin, branch admin |
| `gradebook.sheet.view` | read sheets (teachers: only their own, by rule) | school admin, branch admin, teacher |
| `gradebook.sheet.enter` | enter and submit as main teacher | teacher |
| `gradebook.sheet.enter_any` | enter any sheet in your branches | nobody (a school may grant it) |
| `gradebook.sheet.import` | bulk upload | teacher, school admin |
| `gradebook.sheet.remind` | Remind | school admin, branch admin |
| `gradebook.moderation.decide` | approve or return (heads only, by rule) | teacher |
| `gradebook.class.compile` | comments, ratings, submit the class (class teacher only, by rule) | teacher |
| `gradebook.class.publish` | approvals queue, return, principal's comments, publish | school admin |
| `gradebook.electives.update` | who takes what (class teacher by rule, or any class) | school admin, branch admin, teacher |
| `gradebook.hold.view` / `gradebook.hold.manage` | fee holds | school admin, finance admin / finance admin |
| `gradebook.release.view` | release list, report cards, print | school admin, branch admin |
| `gradebook.result.view` | a student's Results tab | school admin, branch admin |
| `gradebook.amendment.create` / `.approve` | raise / decide | school admin, branch admin / school admin |
| `gradebook.analytics.view` / `.export` | analytics, CSV | school admin, branch admin |
| `gradebook.setup.view` / `.update` | structures, scales, options, templates | school admin, branch admin / school admin |

Moderation, holds, bulk upload and analytics each sit on their own
resource, so the brief's "sold by depth" banding is a seed-data change later
rather than a code change. Seeded in `seed_school_permissions` with its
backfill phase; Finance Admin needs a defaults column it does not have yet.

---

## 5. Models, settings, events (all tenant-scoped)

| Model | Holds |
|---|---|
| `AssessmentStructure` / `AssessmentComponent` | programme, branch (null = school-wide), status (draft, in use, retired), copied from; components: name, short, max, is exam, order |
| `GradingScale` / `GradeBand` | programme, branch, status, copied from; bands: lowest mark, grade, remark, is fail, order |
| `ClassTerm` | class, term, branch, structure, scale, stage, submitted by/at, returned by/at/note, published by/at |
| `ScoreSheet` | class-term, subject, status, submitted, moderated (by/at), returned (by/at/note), saved at. Unique (class-term, subject) |
| `Score` | sheet, student, component, mark or marker (ABS, EXC), entered by/at. Unique (sheet, student, component) |
| `ElectiveTaker` | class, term, subject, student (owner per Q1) |
| `StudentTermResult` | class-term, student, class teacher's comment (by/at), ratings, the published snapshot (subject rows, totals, average, position, grade, remark, principal comment), version, released at, email state and time |
| `PrincipalComment` | class-term, band, text, by/at |
| `ResultHold` | term, student, reason, placed by/at, lifted by/at, lift note |
| `Amendment` | student term result, subject, reason, before and after, status, raised by/at, decided by/at, decision note |
| `SheetReminder` | sheet, date, sent by/at. Unique (sheet, date) |
| `RemarkTemplate` | band floor, text |

Settings in `vs_config`, school-wide: `gradebook.show_class_stats`,
`gradebook.rank_in_class`, `gradebook.conduct_ratings`,
`gradebook.rating_key`, `gradebook.rating_traits`,
`gradebook.moderated_programs` (programme codes, empty).

Notification events: `gradebook.sheet_reminder` (teacher),
`gradebook.sheet_returned` and `gradebook.class_returned` (in-app),
`gradebook.report_card_ready` and `gradebook.report_card_updated` (guardian;
unregistered recipient when the guardian has no login).

Exports download straight away through `vs_finance.exports.render`, as
attendance's do. Report cards are served as data; the screen renders and the
browser prints, as invoices and receipts are printed today.

---

## 6. Not ours

| Module | What we need | Size |
|---|---|---|
| M13 Academic Structure | Who takes what (if Q1 lands there); a head on the department (if Q2 lands there) | a model, a field |
| M14 Calendar | A "Results day" event type (D3) | one choice value |
| M15 Attendance | A system-side read of term counts and days not taken (D5) | one service |
| Finance (FAL) | `oldest_unpaid_on` on `DebtorRow` (D4) | an additive contract field |
| M11 Students | Hosts the Results tab; we serve its one read. Other tabs are M11's | none |
| M27 / M28 Portals | Read released results later; M28 FR-006 says "published" and should say "released" | a wording fix to their FRD |
| M25 Dashboards | Can read `hub/` later; nothing built for them now | none |

**Dead API:** none. There is no existing API in this namespace.

---

## 7. Field Access

Registered in `vs_gradebook/field_access.py`. Every field below is included,
default open.

| Field | Read | Write | Note |
|---|---|---|---|
| Component mark (with ABS / Exc) | yes | yes | the sheet itself |
| Total, percentage, grade, remark | yes | no | derived |
| Class average, highest, lowest | yes | no | derived; also governs moderation and analytics averages |
| Overall average | yes | no | derived |
| Position in class | yes | no | derived; its own switch so a school can hide it from a role |
| Conduct ratings | yes | yes | |
| Class teacher's comment | yes | yes | |
| Principal's comment | yes | yes | |
| Return note, amendment reason, decision note | yes | yes | |
| Hold reason, lift note | yes | yes | |
| Balance, oldest unpaid | yes | no | read from Finance; sensitive |
| Guardian name and email | yes | no | aliases the M11 guardian switches; hidden there means hidden here |
| Release email state | yes | no | server-owned |

Student names and admission numbers on sheets follow the existing student
name switches. The printed report card is a document for the family and
stays unfiltered, as invoices do.

---

## 8. Decisions

**Q1. Who owns "Who takes what"?** (a) M13 Academic Structure, as a record
of the class (recommended), or (b) this module.

**Q2. Who moderates?** (a) A head named on the academic department, set in
Academic Structure (recommended), or (b) anyone holding the moderation key,
across every moderated subject.

**Q3. What an amendment carries and what it changes.** The corrected marks
are entered in the amendment (recommended), and on approval only the
amended student's card is re-issued; classmates keep the position and class
figures that were published (recommended), or every card whose figures move
is re-issued.

**Q4. "Cannot sign in" on the late list.** Shown only to a caller who can
manage staff accounts (recommended), shown to every hub reader, or never.

**Q5. Build order and the brief's extras.** Build 1: setup, who takes what,
sheets, moderation, compiling, approval and publishing, holds, release,
report cards, the student tab, the hubs and reminders, with B2, B3, B4, B6.
Build 2: amendments, analytics and exports, bulk upload (B7, B8), and the
cumulative third-term card with its promotion line (B1, B5).

---

## 9. Build 1 engineering spec

The contract the build agents follow. Where it names a key or a code it is
binding; where it says "follow X" the existing module is the model.

### 9.1 App and conventions

- App `apps/schools/vs_gradebook/`, label `vs_gradebook`, mounted at
  `v1/gradebook/` in `apps/apps/urls.py`, installed beside `vs_attendance`.
  Layout follows `vs_attendance`: `constants.py`, `exceptions.py`
  (`GradebookError` with `error_code`, `default_message`, `http_status`),
  `models.py` (an `_Owned` base with tenant), `serializers.py`,
  `field_access.py` (registered from `AppConfig.ready`), `search_places.py`,
  `services/`, `views/` (a `GradebookViewMixin` like
  `vs_attendance/views/base.py`), `urls.py`, `tests/` with a `base.py`
  fixture modelled on `vs_attendance/tests/base.py` (Brightfield with Lekki
  and Ikeja branches, Sunrise with one branch and the other tenant in
  isolation tests, a fixed school clock).
- The school's today is `vs_config.clock.tenant_today` / `branch_today`,
  never `date.today()`.
- A term is `?term=<id>`; absent, it is the current term by date
  (`vs_academics.services.reads.term_state`); a term of another tenant is
  404.
- Envelope `success_response`; every view has a `docstring-name:` line.
- Branch reach: `vs_rbac.scoping.visible_branch_ids` and the class's branch,
  as `vs_attendance/services/scope.py` does. A class outside the caller's
  branches, or another tenant's, is 404.
- The class roll as at a date is `schools.vs_students.services.roster`
  (`roster_as_at`). Elective takers are `schools.vs_academics.services.electives`.
  The department head is `Department.head`.

### 9.2 Keys (as built; the plan's verbs are mapped to existing actions)

| Key | Default |
|---|---|
| `gradebook.hub.view` | school_admin, branch_admin |
| `gradebook.sheet.view` | school_admin, branch_admin, teacher |
| `gradebook.sheet.enter` | teacher |
| `gradebook.sheet.enter_any` (new action verb `enter_any`) | none |
| `gradebook.sheet.remind` | school_admin, branch_admin |
| `gradebook.moderation.approve` | teacher |
| `gradebook.class.submit` | teacher |
| `gradebook.class.publish` | school_admin |
| `gradebook.hold.view` | school_admin, finance_admin |
| `gradebook.hold.update` | finance_admin |
| `gradebook.release.view` | school_admin, branch_admin |
| `gradebook.result.view` | school_admin, branch_admin |
| `gradebook.setup.view` | school_admin, branch_admin |
| `gradebook.setup.update` | school_admin |

Seeded in `seed_school_permissions` (with its phase-2 backfill). Finance
Admin gains a defaults column there. `vs_rbac.capability_map` maps module
`gradebook` to capability `gradebook`.

Domain rules on top of the keys (a 403 domain error naming the rule):
- enter / submit a sheet: the caller is the sheet's main teacher (the LEAD
  `TeachingAssignment` for the class's session, class and subject), or holds
  `sheet.enter_any`.
- read a sheet with only `sheet.view` and no whole-branch reach: main teacher,
  an assistant, the class teacher, or the head of the subject's department.
  Holders of `hub.view` read every sheet in their branches.
- moderate: the caller heads the subject's department.
- compile and submit a class: the caller is the class teacher.
- setup writes need a whole-school reach (a branch-bound caller is refused,
  as attendance settings are).
- "Cannot sign in" on the late list is included only when the caller holds
  `school.administrators.reactivate` (the key on staff account unlock).

### 9.3 Models

```
AssessmentStructure  tenant, program, branch (null), name, status DRAFT|IN_USE|RETIRED,
                     copied_from, created_by/at
AssessmentComponent  structure, name, short_name, max_score (1-100), is_exam, order
GradingScale         tenant, program, branch (null), name, status DRAFT|IN_USE|RETIRED,
                     copied_from, created_by/at
GradeBand            scale, min_score (0-100), grade, remark, is_fail, order
ClassTerm            tenant, school_class, term, branch, structure (null until the first
                     mark), stage AWAITING|PUBLISHED|null, submitted_by/at,
                     returned_by/at/note, published_by/at. Unique (class, term)
ScoreSheet           tenant, class_term, subject, status NOT_STARTED|IN_PROGRESS|SUBMITTED|
                     APPROVED|RETURNED, needs_moderation, submitted_by/at,
                     moderated_by/at, returned_by/at/note, saved_at. Unique (class_term, subject)
Score                sheet, student, component, mark (int null), marker ABS|EXC|null,
                     entered_by/at. Unique (sheet, student, component)
StudentTermResult    class_term, student, teacher_comment, comment_by/at, ratings (JSON
                     trait -> 1..5), snapshot (JSON, null until published), version,
                     released_at, email_state SENT|NO_EMAIL|HELD|null, emailed_at.
                     Unique (class_term, student)
PrincipalComment     class_term, band_min, text, by/at. Unique (class_term, band_min)
ResultHold           tenant, term, student, reason, placed_by/at, lifted_by/at, lift_note;
                     one open hold per (term, student)
SheetReminder        sheet, on_date, sent_by/at. Unique (sheet, on_date)
RemarkTemplate       tenant, band_min, text, order. Unique (tenant, band_min)
```

Settings (`vs_config` `SCHOOL_SCOPED_DEFINITIONS`):
`gradebook.show_class_stats` (true), `gradebook.rank_in_class` (true),
`gradebook.conduct_ratings` (true), `gradebook.rating_key` (true),
`gradebook.rating_traits` (the design's ten), `gradebook.moderated_programs`
(programme codes, empty). Remark templates are seeded on first read with the
design's four.

### 9.4 Rules the services own

- **Structure and scale in force for a class**: the IN_USE one for the
  class's level's programme and the class's branch, else the IN_USE one with
  a null branch. None: 409 `NO_STRUCTURE` / `NO_SCALE`, "This class's
  programme has no assessment structure in use. Set one up in Assessment
  setup." A `ClassTerm` fixes its structure at its first mark (R5). The scale
  is read live until publication, then frozen in the snapshot.
- **Putting a draft in use** retires the IN_USE one for the same programme and
  branch. A structure must total 100 and have at most one examination
  component (`STRUCTURE_TOTAL`); a scale must start at 0 with distinct
  minimums and at least one non-fail band (`SCALE_GAP`). Any edit, retire or
  activation touching a structure or scale referenced by a published
  `ClassTerm` (structure FK, or scale id in a snapshot) is refused
  (`USED_BY_PUBLISHED_RESULT`); copy is always allowed. An edit to a
  structure fixed on an unpublished `ClassTerm` is refused when it would
  invalidate an entered mark (`WOULD_INVALIDATE_MARKS`, naming the sheet).
- **Due sheets** of a class-term: every core offering of the class's level,
  plus every non-core offering with at least one taker. A sheet row is
  created on first write; a read of a missing one answers NOT_STARTED.
- **Sheet roll**: `roster_as_at(class, min(today, term.end))`, filtered to
  takers for a non-core subject. Marks of students no longer on the roll are
  kept but not shown or counted.
- **Computation** (pure functions, `SimpleTestCase`-tested): as R14-R16. A
  cell write is whole marks only (`NOT_A_WHOLE_MARK`), 0..max (`OVER_MAX`,
  "Max 20"). Percentage unrounded for grading, shown to 2 dp. Overall
  average R22. Position R23 with ties compared at 2 dp.
- **Sheet states**: first mark makes IN_PROGRESS. Submit needs no blank cell
  on the roll (`BLANKS_REMAIN`, with the count); it sets SUBMITTED, and
  `needs_moderation` when the class's programme code is in
  `gradebook.moderated_programs` and the subject's department has a head.
  A sheet is **in** when SUBMITTED and not `needs_moderation`, or APPROVED.
  Writes are refused on SUBMITTED / APPROVED sheets (`SHEET_LOCKED`) and on
  any sheet of an AWAITING or PUBLISHED class-term (`CLASS_LOCKED`).
  Return (moderator, or the approver per B2) needs a note (`NOTE_REQUIRED`)
  and sets RETURNED; the teacher's next submit clears the note.
- **Class stage**: PUBLISHED or AWAITING when stored, otherwise COMPILING when
  every due sheet is in, else ENTERING; `returned` is shown while a return
  note stands. Submit needs every due sheet in (`SUBJECTS_MISSING`) and a
  comment for every student on the roll (`COMMENTS_MISSING`). The approver
  returns a class to its class teacher (stage cleared, note kept) or one
  sheet to its main teacher (B2: that sheet RETURNED, class stage cleared).
- **Publish** (one class or several, B3), each in its own transaction, only
  from AWAITING (`NOT_AWAITING`): build every roll student's snapshot (subject
  rows with component cells, total, obtainable, percentage, grade, remark,
  class average, highest and lowest of totals; totals; overall average;
  position and roll size; attendance counts from
  `vs_attendance.services.term_summary.class_term_summary`; ratings; class
  teacher comment and name; principal comment for the band and approver
  name; grading key; next term start), version 1, scale id. Then release
  every student without an open hold: `released_at`, guardian email, email
  state. Held students get `email_state=HELD`.
- **Guardian email** `gradebook.report_card_ready`: primary guardian through
  `vs_attendance.services.notices.primary_guardians` (account user when the
  guardian has one, else an unregistered recipient), context only: child's
  first name, term and session names, school name, branch name, school phone.
  No email: `NO_EMAIL`. One per child.
- **Holds**: list = roll students of every class in the caller's branches for
  the term, paginated, filters `class`, `branch`, ordered by class then name,
  with the FAL `debtors` row (balance, `oldest_unpaid_on`) for the page's
  students and `as_of` timestamp; students absent from `debtors` owe ₦0.00.
  Place: reason required, refused once released (`ALREADY_RELEASED`, "A
  released result cannot be held."), refused when already held. Lift: optional
  note; when the class-term is published, releases and emails then.
- **Reminder** `gradebook.sheet_reminder` in-app and email to the main teacher;
  once per sheet per school day (`ALREADY_REMINDED`); refused when there is
  no main teacher (`NO_MAIN_TEACHER`) or they cannot sign in or are off the
  roll (`TEACHER_UNREACHABLE`, saying why). Events `gradebook.sheet_returned`
  (main teacher) and `gradebook.class_returned` (class teacher), in-app.
- **Late reasons**, in order: returned by whom; no main teacher; last working
  day (`StaffProfile.exit_date`, when set); cannot sign in (only for the key
  in 9.2); then "N of M entered".
- **Audit** every submit, return, moderation decision, class submit,
  publish, release, hold, lift, reminder and setup change, the way
  `vs_attendance/services/audit.py` does.

### 9.5 Agent split

1. **Foundation**: app, models, migration, constants, exceptions, keys and
   seeding, capability map, settings definitions, the setup endpoints
   (structures, scales, settings, remark templates), the pure computation
   module and the resolution services (structure and scale in force, due
   sheets, sheet roll, main teacher), Field Access registration for setup.
2. **Sheets**: sheet read and batch write, submit, moderation queue, approve,
   return, reminders, both hubs, notifications.
3. **Class to card**: broadsheet, compile, class submit, approvals queue,
   returns, principal comments, publish preview and publish, release, holds,
   report cards, the student tab, search places, Field Access for every read.
4. **Seed scenarios**: `seed_gradebook_scenarios` through the real services.

---

## 10. As built (build 1)

Build 1 shipped as sections 3 and 9 specify, with the differences below. The
FRD is written from this section where it disagrees with earlier ones.

**Routes** (all under `/v1/gradebook/`): `settings/`; `structures/`,
`structures/<id>/`, `…/copy/`, `…/activate/`; the same for `scales/`;
`remark-templates/`; `sheets/<class>/<subject>/`, `…/scores/`, `…/submit/`,
`…/approve/`, `…/return/`, `…/remind/`; `moderation/`; `hub/`, `hub/mine/`;
`classes/<class>/broadsheet/`, `classes/<class>/students/<student>/compile/`,
`classes/<class>/submit/`, `classes/<class>/return/`,
`classes/<class>/principal-comments/`, `classes/<class>/publish/preview/`,
`classes/publish/`; `approvals/`; `holds/`, `holds/<id>/lift/`;
`release/<class>/`; `report-cards/<class>/`, `report-cards/<class>/<student>/`;
`students/<student>/results/`. Who takes what is M13's:
`/v1/academics/classes/<class>/electives/`.

**Outside the gradebook, in the same change**

1. **M13**: `ElectiveTaker` (who takes a non-core subject, per class per
   term) with its grid endpoint and keys `academics.electives.view` /
   `.update`; the class teacher, or a holder of `academics.classes.assign`
   over the class's branch, may set it (teachers hold `classes.update` by
   default, so that key could not tell a class teacher from any teacher).
   `Department.head` with its Field Access switch. A removal guard registry,
   which the gradebook uses to refuse dropping a taker who has marks.
2. **M11**: the as-at class roll moved from attendance to
   `schools.vs_students.services.roster`, shared by attendance, academics and
   the gradebook.
3. **M14**: event type `RESULTS_DAY` (does not close the school) and
   `results_day(tenant, term, branch)`. A calendar settings save that omits
   an event type now keeps that type's stored value, so a client that
   predates a new type keeps working.
4. **M15**: `class_term_summary(tenant, class, term)`, a system read of each
   pupil's term counts and the class's days with no register. The report
   and the student tab now share one counting engine with it.
5. **FAL**: `DebtorRow.oldest_unpaid_on`, contract 1.1.6 (additive).

**Differences from sections 3 and 9, all deliberate**

1. Keys use existing verbs: `gradebook.moderation.approve` (not `decide`),
   `gradebook.class.submit` (not `compile`), `gradebook.hold.update` (not
   `manage`). One new verb, `enter_any`.
2. Activating a copy of a locked structure or scale retires the locked one;
   only edit, delete and activation of the locked record itself are refused.
3. A submitted sheet waiting for its head is on the late list with its own
   reason (`AWAITING_MODERATION`), so sheets due less sheets in equals the
   late count.
4. Where a school does not rank, `position` and `position_of` are absent from
   every payload, not null. Ratings and traits likewise when ratings are off.
5. The guardian email's template variables are `tenant_name` and
   `tenant_phone` (engine vocabulary). The publish answer's `emailed` counts
   guardians with an address; a send that fails leaves the email state empty
   and the release list reads it `NOT_SENT`, never sent.
6. Report cards are served as data from the frozen snapshot, with a live
   letterhead; the screen renders and the browser prints. The letterhead
   names the branch only at a school with several.
7. Returning one subject (B2) clears the class's own return note; the note
   travels on the sheet.
8. Field Access: the guardian on the Results tab and release list is one
   field, `guardian`, cut down further by the Student Management name and
   email switches (the stricter wins). Comments carry their signer's name
   under the same switch. All gradebook resources sit in the gradebook Core
   band until the commercial banding is decided.

**Found and fixed along the way**

1. The attendance class chronic list answered a branch-pinned reader with
   another branch's class name (200 with an empty list); it now answers 404.
   Its serializer was also missing from the Field Access surfaces registry.

**Found, not fixed (see the closing report)**

1. A hold on a student no longer on any visible class roll cannot be seen or
   lifted from the holds list.
2. `seed_attendance_scenarios` fails with `SelfDecision` after
   `seed_staff_scenarios` has run (the class teacher it files corrections as
   is then the admin who decides them).
3. The student seeder leaves two senior classes empty, so demo data for them
   is thin.

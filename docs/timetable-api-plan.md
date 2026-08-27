# M14 Academic Calendar & Timetables - API plan

**Design:** `docs/designs/Academic_Calendar.html` - 8 screens, 44 collections,
79 actions, 29 field writes, 622 bindings.
**Brief:** `M14_Academic_Calendar_Timetables_Design_Prompt.md` - the prompt the
design was generated from.
**FRD:** `M14_Academic_Calendar_Timetables_FRD_v3.0.1.docx` - 17 requirements,
7 models, 19 endpoints, ~284k characters. Not in the repository.
**Backend today:** nothing. No app, no model, no endpoint.

> **Approved 26 August 2026.**
>
> 1. **Scope: build the module.** Placement and routes now follow the FRD, not
>    this plan's first draft - see §3.
> 2. **Teachers: derived from RBAC.** See §5, which is now the largest
>    correction against the FRD rather than a judgement call.
> 3. **The FRD exists** and is a genuine build specification. This document is
>    the delta against it, not a replacement for it.

---

## 1. The headline

The FRD is a better specification than the plan I wrote before reading it, and
where the two disagree about the backend it is right and I have taken its
answer. It has three problems, all of the same kind: **it was verified against a
tree that no longer exists.**

1. **`user_type` is gone.** The FRD's §4.8 defines a teacher as a
   `vs_user.User` "whose `user_type` is STAFF". That column was dropped by
   `vs_user` migration `0009_drop_user_type`. §4.8, three rows of §3.5, decision
   17 and business rule 5 of FR-013 all rest on a field that no longer exists.
   §5 below is the replacement, and it is the decision already taken.
2. **FR-007 forbids exactly what the hub screen shows.** The overview
   requirement is unchanged from v2.3, when the timetable half was deferred, and
   it still says the overview must carry "no teacher clash, no room clash" and
   no timetable count. v3.0 restored the timetable half and never came back to
   it. The design's hub shows both. See §4.1.
3. **Four endpoints the design needs are not in the FRD's nineteen**, and one of
   them - the class timetable's publish state - has nowhere to be stored at all.
   See §4.2.

Everything else in the FRD stands and this plan defers to it.

---

## 2. Screen → data → endpoint

Buckets: **Served** (works today) · **Absent** (no endpoint). Nothing in this
module is Closed or Wrong shape, because nothing exists to be either.

Endpoints below are the FRD's own paths. Rows marked **[+]** are additions this
reconciliation proposes; rows marked **[!]** contradict the FRD as written.

### Chrome - every screen

| Shows | Endpoint | Bucket |
|---|---|---|
| Branch pill and menu | `GET /v1/tenants/branches/` | **Served** |
| Session pill and menu, archived read-only state | `GET /v1/academics/sessions/` | **Served** |
| Command palette - "searches screens, not records" | none | **Served** (client-side) |

### Screen 1 - Hub

| Shows | Endpoint | Bucket |
|---|---|---|
| Hero: session name, range, % elapsed, term pills | `GET /v1/academics/sessions/` | **Served** |
| Counts: terms defined, events this term | `GET /v1/academics/calendar/overview/` (FR-007) | **Absent** |
| Counts: **classes timetabled, rooms** | same call | **Absent [!]** - FR-007 forbids these |
| Next up: 4 upcoming events with type chip | same call (FR-007 `next_up`) | **Absent** |
| Alerts: session has no terms, event outside every term, term outside session, terms overlap | same call | **Absent** |
| Alerts: **unresolved clashes, class with no timetable** | same call | **Absent [!]** - FR-007 forbids these |

### Screen 2 - Calendar & Events

| Shows | Endpoint | Bucket |
|---|---|---|
| Event rows: name, type, dates, term or "Outside every term", scope chip, closed mark | `GET /v1/academics/calendar/events/` (FR-001) | **Absent** |
| Filters: search, type, term, scope | same, query params | **Absent** |
| Add / edit drawer: name, type, start, end, applies-to, description, school-closed | `POST` / `PATCH /v1/academics/calendar/events/<id>/` | **Absent** |
| View drawer, incl. whether an exam timetable exists inside an exam period | `GET /v1/academics/calendar/events/<id>/` | **Absent** |
| Delete | `DELETE /v1/academics/calendar/events/<id>/` | **Absent** |
| *(no UI)* audience narrowing to levels/classes | FR-003, `CalendarEventAudience` | **Absent** - see §4.3 |

### Screen 3 - Term Calendar View

| Shows | Endpoint | Bucket |
|---|---|---|
| Session timeline, term bands, today marker | `GET /v1/academics/sessions/` or FR-006 `/calendar/year/` | **Served** |
| Month grid, day cells, event chips, closed shading | `GET /v1/academics/calendar/events/` | **Absent** (same endpoint as screen 2) |
| Same three filters; click a day to add | same | **Absent** |

No month endpoint. `monthCells` regroups rows the screen already holds.

### Screen 4 - Rooms

| Shows | Endpoint | Bucket |
|---|---|---|
| Cards and rows: name, code, type, branch, capacity, status | `GET /v1/academics/timetable/rooms/` (FR-011) | **Absent** |
| `rc.usage` - "3 lessons · 1 exam paper" | same, annotated | **Absent [+]** - not in FR-011 |
| Filters: search, type, branch, status | same | **Absent** |
| Add / edit / toggle active | `POST`, `PATCH /v1/academics/timetable/rooms/<id>/` | **Absent** |
| Delete, refused when in use | `DELETE /v1/academics/timetable/rooms/<id>/` → 409 `PROTECTED_REFERENCE` | **Absent** |

### Screen 5 - Bell Schedule

| Shows | Endpoint | Bucket |
|---|---|---|
| Day tabs, proportional day strip, "Friday uses its own schedule" line | `GET /v1/academics/timetable/periods/` (FR-012) | **Absent** |
| Table: order, label, time, type, applies-on, scope | same | **Absent** |
| Add / edit / delete period | `POST`, `PATCH`, `DELETE /v1/academics/timetable/periods/<id>/` | **Absent** |

### Screen 6 - Class Timetables

| Shows | Endpoint | Bucket |
|---|---|---|
| The grid: periods × days, each cell subject/teacher/room | `GET /v1/academics/timetable/classes/<class_id>/` (FR-013) | **Absent** |
| Fill / edit / clear one cell | `POST` / `PATCH` / `DELETE /v1/academics/timetable/slots/<id>/` | **Absent** |
| Save a whole edited grid | `PUT /v1/academics/timetable/classes/<class_id>/` | **Absent** |
| Clash panel, with cross-branch redaction | `data.warnings` on write; grid read (FR-014) | **Absent** |
| Publish / Republish, blocked by clashes | `POST /v1/academics/timetable/classes/<class_id>/publish/` (FR-017) | **Absent** |
| **Status chip: Not started / Draft / Published** | nowhere to store it | **Absent [!]** - see §4.2.1 |
| **Class picker with per-class lesson count, status, clash flag** | no endpoint | **Absent [+]** - see §4.2.2 |
| **Duplicate from another class**, with preview and options | no endpoint | **Absent [+]** - see §4.2.3 |
| Clear the whole grid | `PUT` with an empty slot list | **Absent** |
| Export / print | `vs_exports` dataset - see §6 | **Absent** |

### Screen 7 - Teacher Timetables

| Shows | Endpoint | Bucket |
|---|---|---|
| Read-only week grid: subject, class, room | `GET /v1/academics/timetable/teachers/<user_id>/` (FR-015) | **Absent** |
| Stats: teaching periods, free, busiest day, branches | same | **Absent** |
| **Teacher picker with per-teacher lesson count and clash flag** | no endpoint | **Absent [+]** - see §4.2.4 |

### Screen 8 - Exam Scheduling

| Shows | Endpoint | Bucket |
|---|---|---|
| Exam-period header, read from the calendar event | `GET /v1/academics/exams/` (FR-016) | **Absent** |
| Paper rows: date, sitting, class, subject, room, invigilator | `GET /v1/academics/exams/<id>/slots/` | **Absent** |
| Add / edit / remove paper | `POST`, `PATCH`, `DELETE .../slots/<slot_id>/` | **Absent** |
| Clash panel and count | `data.warnings` (FR-014) | **Absent** |
| Publish, blocked by every clash incl. invisible ones | `POST /v1/academics/exams/<id>/publish/` (FR-017) | **Absent** |
| *(no UI)* creating the `Exam` row itself | `POST /v1/academics/exams/` | **Absent** - see §4.3.5 |

---

## 3. What this plan takes from the FRD unchanged

Everything in this section replaces what my first draft said. The FRD is right
and I was wrong on each of them.

| Question | The FRD's answer, adopted |
|---|---|
| App name | `apps/schools/vs_calendar/`, app label `vs_calendar`, INSTALLED_APPS `"schools.vs_calendar"` after `"schools.vs_academics"`. It keeps the name although it now owns the timetable too. |
| Routes | Three includes under `/v1/academics/`: `calendar/`, `timetable/`, `exams/` - **mounted before** `v1/academics/` or every one of them 404s inside M13's urlconf. A test must assert all three resolve. |
| Permission keys | `academics.calendar.{view,create,update,manage}` - **already seeded**, reused. Plus a new resource `academics.timetable` with `{view,create,update,manage,publish}`. The `publish` verb is already seeded (`seed_actions.py:43`) and its description already names timetables. |
| Models | `CalendarEvent`, `CalendarEventAudience`, `Room`, `Period`, `TimetableSlot`, `Exam`, `ExamSlot`. |
| `Room.branch` | **Non-null.** The only non-null branch column in the schools product, and correctly so: a room is a place and a place is at one branch. My draft had it nullable, which would have produced rooms belonging to everywhere. In a single-branch school it is filled from the only branch and never shown. |
| `Period` | Stores `order_index`, ISO `day_of_week` 1-7 nullable, `period_type`, and a service-level overlap check under a row lock - not an exclusion constraint, which appears nowhere else in this repo. |
| A day's own periods **replace** the everyday ones | Wholesale override, matching the design's line verbatim. |
| `TimetableSlot` | Exactly **one** unique constraint, `(session, school_class, day_of_week, period)`. **No unique constraint over teacher or room** - a clash must be storable to be shown in red. A test asserts against the migration state so adding one fails loudly. |
| Clash detection | Three rules. Teacher and class queries run **over the whole tenant**, deliberately wider than the caller's read scope. Room needs no widening - a room is at one branch by construction. |
| Cross-branch disclosure | The clash is always reported; the detail is reduced. Never a branch id in a reduced warning, so the parameter cannot map another branch's grid. |
| Publish | Recomputes the clash rules rather than reading a flag - a clash is a relationship between two rows and a cached flag is a cache with no invalidation. |
| `TIMETABLE_SPANS_BRANCHES` | One class's slots must use rooms at one branch. **My draft missed this entirely** and it is a real defect it would have shipped: a school-wide class is visible to both branch admins, so two of them can each start building JSS1 A's grid in their own rooms. |
| `Exam` anchoring | `PROTECT` FK to an `EXAM_PERIOD` `CalendarEvent`; session, dates and branch scope read from it and never copied. |
| Exam clash asymmetry | Class-in-two-sittings refused by unique constraint; room and invigilator warn. |
| Audit | `AuditModuleKey.ACADEMICS` **already exists** (`vs_audit/models.py:68`). Add one action type, `ACADEMIC_TIMETABLE_PUBLISHED`. |
| Notifications | **None.** No `academics.*` event type, none registered, none emitted. Publishing tells nobody. |
| Refusal codes | `SLOT_PERIOD_NOT_TEACHING`, `NOT_A_TEACHING_USER`, `ROOM_BRANCH_CONFLICT`, `TIMETABLE_SPANS_BRANCHES`, `PERIOD_OVERLAP`, `PERIOD_ORDER_CONFLICT`, `EXAM_EVENT_NOT_EXAM_PERIOD`, `EXAM_OUTSIDE_EXAM_PERIOD`, `TIMETABLE_HAS_CLASHES`, plus the platform's own `PROTECTED_REFERENCE` and `DUPLICATE`. My draft invented `ROOM_IN_USE` and `NO_BELL_SCHEDULE`; the platform already answers the first as `PROTECTED_REFERENCE`. |
| Pagination | Lists paginate; `/current/`, `/year/`, `/overview/`, the class grid and the teacher grid do not. A grid is bounded by the week, not by the school. |
| No auto-generation | Absent, not stubbed and not disabled. No generate endpoint of any kind. |

**One thing I got right and should not lose:** two of FR-007's four alerts,
`TERM_OUTSIDE_SESSION` and `TERM_DATES_OVERLAP`, cannot be produced through the
API - M13 refuses both. My draft said drop them. **The FRD is right to keep
them** and says why: rows arrive by import, by fixture and by migration, and a
quietly malformed school year produces a calendar that is wrong everywhere and
blamed nowhere. They are read-only observations. That correction is adopted.

---

## 4. The delta - where the three disagree

### 4.1 FR-007 forbids what the hub screen shows

**FR-007, "What the overview must not carry":** *no classes-with-a-complete-
timetable count, no scheduled-exams count, no teacher clash, no room clash and
no incomplete-timetable warning* - with acceptance criterion 5 asserting the
response "contains no key naming a period, a timetable, a slot or an exam
schedule".

**The design's hub shows:** a "Classes timetabled" count, a "Rooms" count, an
alert reading "3 unresolved timetable clashes - a teacher or room is
double-booked. Publishing is blocked until they are fixed", and an alert per
class with no timetable.

**This is stale, not a disagreement about the product.** FR-001 to FR-010 are
carried unchanged from v2.3, when the timetable half was deferred; v3.0 restored
it and never revisited FR-007. Its own justification says so - a timetable
figure would be "a figure with nothing behind it, and a zero would read as a
real and alarming number rather than an absent feature". Once the timetable
exists there is something behind it.

**The design wins. FR-007 gains four things:**

- `classes_timetabled` - a count of classes holding at least one slot;
- `rooms` - a count of rooms in the caller's branch scope;
- alert `TIMETABLE_HAS_CLASHES` with the count and the slot ids;
- alert `CLASS_HAS_NO_TIMETABLE` naming each class.

**Two prohibitions survive and must be carried into the revision**, because the
design honours both: no *complete*-timetable count (the design counts classes
timetabled, never classes finished) and no scheduled-exams count. The
distinction is the whole of FR-007's reasoning and it is worth keeping.

Acceptance criterion 5 is deleted rather than amended.

### 4.2 Four endpoints the design needs and the FRD's nineteen do not have

#### 4.2.1 A class timetable's publish state has nowhere to live

The sharpest gap in the FRD. FR-017 publishes a class timetable, "sets a state
and stamps a time", and audits it with `entity_type` `"SchoolClass"`. But §6
declares five new timetable models and **only `Exam` carries `status` and
`published_at`**. `TimetableSlot` carries neither, and there is no `Timetable`
table - §6.11 argues at length that there must not be one.

So as specified, `POST /timetable/classes/<id>/publish/` has nothing to write
to. The design needs it in two places: the status chip (`Not started` / `Draft`
/ `Published`), and the class picker, which shows every class's status at once.

The state cannot go on `SchoolClass`: that is M13's model, and a class outlives
a session while its timetable does not - the same class is Draft next year.

**Proposal: `ClassTimetable`**, in this module, `(tenant, session, school_class)`
unique, carrying `status` (`DRAFT` | `PUBLISHED`), `published_at` and
`created_by`. Absent means "Not started", which is the design's third state and
is otherwise unrepresentable. Slots keep pointing at session and class as they
do now; this row is publication state only, so §6.11's argument is untouched -
it objects to a *branch* on the grid, not to a publication record.

Two rules the design supplies and the FRD does not: **editing a published grid
returns it to Draft**, and **duplicating into a class marks the target Draft**.
The first also closes half of the FRD's own decision 18, which records "a
published class timetable may still be edited and simply stops matching what was
published" as "a real gap".

#### 4.2.2 The class picker

The design's picker lists every class with its lesson count, its status and
whether it holds a clash. M13's `/v1/academics/classes/` gives names only.

**Proposal: `GET /v1/academics/timetable/classes/`** - one row per visible
class: id, name, branch, slot count, status, `has_clash`. Bounded by the number
of classes, computed in a fixed number of queries.

#### 4.2.3 Duplicate a grid from another class

A whole drawer in the design - source picker listing only classes that hold
lessons, keep-teachers and keep-rooms toggles, a preview of every lesson to be
copied, a count of lessons skipped because the target does not run that period,
and a warning that copying replaces the target's whole grid. The FRD has no
duplicate path at all.

`PUT` on the class route could carry a client-computed grid, but the skipped-
period rule and the preview both need the target's bell schedule resolved
server-side, and a client that computes it will compute it differently.

**Proposal: `POST /v1/academics/timetable/classes/<class_id>/duplicate/`** with
`{source_class_id, keep_teachers, keep_rooms}` and `?preview=1` returning what
would be copied and what would be skipped without writing. Replaces the target's
grid entirely, in one transaction, one audit event, and marks it Draft.

Note what this produces and why the FRD already allows it: copying without
teachers or rooms creates slots with null teacher and null room, which
`TimetableSlot` permits by design. The design's publish gate refuses those
separately from clashes, naming each one - which is a **second refusal reason
FR-017 does not have**. Add `TIMETABLE_INCOMPLETE`, checked before
`TIMETABLE_HAS_CLASHES`, in that order, because that is the order the design
checks them and the more actionable message goes first.

#### 4.2.4 The teacher picker

FR-015 serves one teacher's grid at `/teachers/<user_id>/`. The design needs the
list: every teacher, alphabetical, each with a lesson count and a clash flag.

**Proposal: `GET /v1/academics/timetable/teachers/`** - id, display name, lesson
count this session, `has_clash`. No email address, per FR-015's rule. **No
annotation beyond those**, which the brief, the FRD's §3.5 and the design all
independently insist on.

### 4.3 Where the design and the FRD disagree, and what happens

| # | Disagreement | Resolution |
|---|---|---|
| 1 | **FR-003 audience narrowing.** The FRD requires `CalendarEventAudience` and a whole requirement for narrowing an event to particular levels or classes. The brief asks for it too. **The design's event drawer has seven fields and none of them is it.** | **Needs your decision - §7.** Unlike everything else here, this is not obviously the design's to win: the design may simply not have drawn it. |
| 2 | **`PERIOD_ORDER_CONFLICT` is unreachable.** The FRD stores `order_index` and refuses an order that disagrees with the times. The design's period drawer has label, start, end, type, applies-on and applies-to - **no order field**. A user cannot supply an order, so it must be server-assigned from the times, and the refusal can never fire through the API. | Keep the column, keep the constraint, assign the value server-side. The refusal stays for imports and fixtures, exactly as FR-007's two dead alerts do. Do not put an order field on the form to make the code reachable. |
| 3 | **Weekend periods.** The FRD's `day_of_week` accepts 1-7. The design's picker offers Every day and Monday to Friday. | Keep 1-7 in the column; the form offering five is a client choice and a Saturday school is a real thing. No change. |
| 4 | **Exam creation.** The FRD requires `POST /v1/academics/exams/` to create a named `Exam` before any paper. The design never creates one - it reads the exam period from the calendar and goes straight to "Add paper". | The endpoint stays. The screen resolves it: on first paper, look up the `Exam` for that event and create it if absent, named after the event. The FRD's model is right; the design correctly refuses to make a school name the same thing twice. |
| 5 | **Room delete message.** The FRD answers 409 `PROTECTED_REFERENCE` with the platform's generic detail. The design's refusal reads: *"This room already holds 3 lessons. Deactivate it instead - it will stop appearing in pickers and everything already scheduled here stays intact."* | Keep the platform's code, carry the design's sentence. A refusal renders verbatim under the control that caused it. |
| 6 | **Hub progress.** The FRD returns term progress in days and in **teaching days** (FR-004). The design's hero shows "% of the session elapsed". | Both. The FRD's fields are backend-only extras the screen may ignore, and the teaching-day figure is the one thing the closed-days flag is for. |
| 7 | **Drag-and-drop, and a "Manage session" hub action.** Both in the brief, neither in the design. | Dropped. No backend consequence either way. |

### 4.4 Backend-only, and correctly absent from every screen

The brief settles this: *"Do not add permission logic or role-gating to the
prototype: access control is handled on the backend."* So none of these is a
design gap - permission keys per verb with prebuilt-role defaults **and** the
backfill phase; an audit row per write and one per bulk write rather than one
per row; `pending_tenant_surface = True` on all nineteen-plus views; 404 rather
than 403 across tenants; `created_by` never serialised as an email address.

---

## 5. The teacher, which is the largest correction

**FRD §4.8 is dead.** It defines a teacher as a `vs_user.User` "whose
`user_type` is STAFF", quotes that field's help text in full, and spends a page
arguing that reading it does not violate the instruction that it must never
drive authorization. The column was dropped by `vs_user` migration
`0009_drop_user_type`; `0008_drop_admin_user_types` retired `SCHOOL_ADMIN` and
`BRANCH_ADMIN` first. `apps/vs_user/models.py:160` now reads: *"There is
deliberately no `UserType`."*

Five places in the FRD fall with it: §4.8 entire; three rows of §3.5; FR-013
business rule 5 and its acceptance criterion 4; FR-015 acceptance criterion 7;
and decision 17.

**The replacement, already decided:** a teacher is a `User` of the tenant with
an ACTIVE `TenantUserRoleAssignment` to a `TenantRoleTemplate` whose `key` is
`teacher` - the same anchor `seed_school_permissions` uses for its own backfill.
`TimetableSlot.teacher` and `ExamSlot.invigilator` stay FKs to
`settings.AUTH_USER_MODEL` exactly as specified. `NOT_A_TEACHING_USER` keeps its
code and its status; only the predicate behind it changes.

**Three of the FRD's own problems this fixes**, which is the part worth noticing:

- **§3.5, "Offer every person who actually teaches"** says a STAFF filter "omits
  exactly the people a Nigerian private school is most likely to have teaching
  alongside an administrative title: the principal, the vice-principal and the
  heads of department", and parks it as open decision 17. A role grant is
  additive, so Brightfield's principal who takes SSS3 Further Maths on
  Wednesdays is given the teacher role alongside her admin one and appears.
  **Decision 17 closes.**
- **§3.5, "Record that one person teaches at two branches"** says `User.branch`
  is one FK and cannot express two. `TenantUserRoleAssignment` can: the same
  role at two branches is two active rows, and its unique constraints are split
  precisely so that "Storekeeper at Ikeja" and "Storekeeper at Lekki" are both
  storable. The migration that dropped `user_type` says it outright - whole-
  tenant reach "is carried by the role assignment's branch rather than by the
  account's". **Decision 20 gets a real answer.**
- **FR-013's tenant-wide picker** was argued from `User.branch` being a single
  FK. That argument is gone but the conclusion survives and strengthens: the
  picker is tenant-wide because the clash query is, and FR-014 is what makes it
  safe. Acceptance criterion 7 needs rewording, not deleting.

**What it still gets wrong, stated plainly:** a teacher who has not been given a
login does not appear, and neither does one whose role assignment nobody made.
Both are real. It is good enough to build all three timetable screens on and it
is not a staffing register - and §3.5's honest framing of that is the model to
follow when the FRD is revised.

---

## 6. Export

The FRD's §9 lists no export endpoint and my draft invented three. **Both are
wrong for this repo.** Exports go through `vs_exports`: the owning app publishes
an `export_datasets.py` registered from its `AppConfig.ready`, plus a
`ScreenBinding` per list screen so "export what this table is showing" carries
the screen's own filters. `schools.vs_academics.export_datasets` is the worked
example, including the inclusive branch read this module needs.

Datasets: events, rooms, periods, one class's grid, one teacher's grid, an exam
schedule. Each narrowed through `narrow_to_caller_branches`, so an export and
the list it mirrors cannot answer differently.

---

## 7. Calendar event audience - decided: build it

**Decision, 27 August 2026: FR-003 is built, and the API carries the pieces the
screen needs to populate it.** The design's event drawer gains an audience
field; this module supplies the field, its options and its filter.

That means four things beyond the model itself:

1. `audience` on the event write serializer - a list of level ids and class ids,
   empty meaning the event applies to everyone in its branch scope;
2. `audience` on the event read serializer, resolved to names so a row can say
   who it covers without a second call;
3. an options read for the picker - the levels and classes of the session, in
   the caller's branch scope, which M13's `/v1/academics/structure/tree/`
   already serves and which this module therefore does not duplicate;
4. the audience narrowing applied to the teaching-day count (FR-004), which is
   the half that actually goes wrong without it.

**The case for building it.** Brightfield's Lekki branch holds Primary Speech
Day on 14 November. Primary 4 A is off timetable; JSS1 A and JSS1 B are not. With
branch scope alone the event is either the whole of Lekki - so JSS1's teachers
see a closure that is not theirs and the teaching-day count is wrong for both
classes - or it is not recorded at all and the primary teachers turn up.

**What it costs.** A join table, a write path on every event create and edit,
and a filter on every calendar read including the month grid and the overview.
Retro-fitting it after a school has a term of events is far more expensive, which
is why it is being done now rather than deferred.

---

## 8. Build order

1. **Room and Period** (FR-011, FR-012) - depend on nothing outside this module,
   and screens 4 and 5 go live together.
2. **CalendarEvent** (FR-001 to FR-004, FR-008) - unblocks screens 2 and 3.
3. **Overview and year reads** (FR-005 to FR-007) - the hub, with §4.1's four
   additions.
4. **TimetableSlot, `ClassTimetable`, the clash service** (FR-013, FR-014) plus
   the class picker and duplicate from §4.2 - screen 6.
5. **Teacher reads** (FR-015) plus the picker list - screen 7, no new writes.
6. **Exam and ExamSlot** (FR-016) - screen 8.
7. **Publish** (FR-017) for both kinds, with `TIMETABLE_INCOMPLETE` before
   `TIMETABLE_HAS_CLASHES`.
8. **Export datasets** and the scenario seeder.

Per endpoint, before it is done: 403 without the key; another tenant's row
answers 404; a PENDING tenant reaches it; happy path; every filter branch; and
the empty-list shape, which `success_response` coerces `[]` to `{}`.

Four tests specific to this module that are easy to skip, three of them the
FRD's own acceptance criteria and worth quoting into the code:

- **a single-branch school and a multi-branch school** - half these rules only
  exist in one of them;
- **the clash query is not narrowed by branch** - the test that fails if
  somebody adds `visible_branch_ids` to the clash service, which is the
  reversal that looks like tightening security;
- **the redaction asymmetry** - a clash the caller cannot see blocks publish and
  names neither the other class, nor the other room, nor any branch id;
- **no unique constraint over teacher or room**, asserted against the migration
  state, so adding one is a failing test rather than a silent product change.

## 9. Seeder

One tenant per state, driven through the real services: a multi-branch school
mid-term with a draft grid, three seeded clashes including one cross-branch, and
a draft exam timetable; a single-branch school with a published grid and no exam
period; an archived session, read-only. Run it twice - a seeder that is not
idempotent invents data.

## 10. Documents to revise, and what else is stale

**The M14 FRD → v3.1** (minor: behaviour, contracts and a current gap change).
§4.1's four additions to FR-007 and the deletion of its acceptance criterion 5;
§4.2's `ClassTimetable` model and four endpoints; `TIMETABLE_INCOMPLETE`; §5's
replacement of §4.8 entire, with §3.5's three rows rewritten and decisions 17
and 20 closed; §6's export section; and §7's outcome either way. It is also
worth recording that the FRD's premise "Neither this module nor M13 is built" is
no longer true - M13 shipped, so the build order in §5.4 is satisfied.

**The MRD → next version.** Three things, none of them M14's:

1. **Module 13 is recorded as `Backend: Planned` / `Code: No mounted domain
   app`** in both the module index and its own entry. `schools.vs_academics` has
   been mounted at `/v1/academics/` since commit `f980683`, has 15 test modules
   and an FRD at v2.7. MRD v2.35 was committed at `24c7985`, well after.
2. **Module 14's capability list includes "Calendar export and notification
   hooks".** The FRD (§7.3) and the brief both forbid notifications outright:
   there is no `academics.*` event type, no student record and no guardian
   record, so publication tells nobody. The capability should be split - export
   stays, notification hooks go, with the reason.
3. **`academics.calendar.*` is seeded, grouped and used by nothing**, and so is
   `academics.classes.assign`. A school administrator can grant "Academic
   Calendar" today and it grants access to nothing. This module closes the first;
   the second is worth a line.

One trivial sweep: `apps/schools/vs_schools/urls.py:45` says "campuses", against
the vocabulary rule in `CLAUDE.md`. The FRD had the same drift and v3.0.1 exists
solely to fix it - three of its change-log rows still carry the old word.

---

## 11. What shipped, and where it diverged from the spec

Written after the build, from the build. `schools.vs_calendar`: 8 models, 25
endpoints, 144 tests. Suites green: `schools.vs_calendar` 144,
`schools.vs_academics` 258, `core` 113, `vs_audit` 96, `vs_exports` 176.

### 11a. Built as planned

The seven FRD models plus `ClassTimetable`; all nineteen FRD endpoints plus the
four from §4.2; `academics.timetable` with five keys, prebuilt defaults and the
backfill; `ACADEMIC_TIMETABLE_PUBLISHED`; the three URL mounts ahead of M13's,
with a test asserting each resolves; FR-007's four additions; the RBAC-derived
teacher directory; five export datasets with screen bindings; and
`seed_timetable_scenarios`.

### 11b. Corrections to the FRD the build forced

**These are cases where the specification is wrong, not where the build was
expedient**, and each has to reach FRD v3.1.

1. **`Period` needs a `session`, and §6.8 gives it none.** Without one a school
   editing its bell schedule for 2026/2027 silently rewrites the 2025/2026 grid
   that was published to parents, because every slot of every year points at the
   same period rows. That contradicts the module's own rule that an archived
   year is read-only. `Period.session` is non-null here.
2. **§6.8's single `uq_period_order` does not hold.** `branch` and
   `day_of_week` are both nullable and PostgreSQL treats NULL as distinct, so
   one index over the four columns does not stop two school-wide everyday
   periods sharing an order - which is the exact defect the constraint exists to
   prevent. Four partial constraints, one per nullability combination. This is
   the same trap §6.7 correctly identifies for `SchoolClass` and then does not
   apply to `Period`.
3. **Three refusals the FRD states as rules and gives no code.** FR-013 rule 3
   (the period's day must match) is `SLOT_PERIOD_WRONG_DAY`; FR-016 rule 6 (a
   published exam refuses writes) is `EXAM_PUBLISHED_READ_ONLY`; FR-003's
   audience needs `EVENT_AUDIENCE_OUT_OF_SCOPE`, because a Lekki event narrowed
   to an Ikeja class shows on Ikeja's calendar because of the class and not on
   it because of the branch.
4. **`order_index` is server-assigned.** Written past the end of the day and
   renumbered in the same transaction, because computing the real position up
   front collides with the row already holding it - an 07:30 assembly wants
   index 1 and Period 1 has it. `PERIOD_ORDER_CONFLICT` survives for imports and
   fixtures, unreachable through the API, which is the same standing FR-007's
   two defensive alerts have.

### 11c. Where the build was expedient and the spec was right

Nothing. No rule was softened to make a test pass.

### 11d. Still open

- **`vs_rbac` carries two pre-existing failures**,
  `test_payments_serializers_wired` and
  `test_procurement_vendor_serializer_wired`. Confirmed present at HEAD with
  this work stashed, in files this change never touches. Not this module's.
- **`success_response` no longer coerces `[]` to `{}`** - that was fixed
  upstream, so the empty-list shape is a plain list. Earlier drafts of this plan
  said otherwise.

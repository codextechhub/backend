# M13 Academic Structure - API plan from the design

**Design:** `docs/designs/Academic_Structure.html` (bundled export, 133 conditional
blocks, 564 bindings, 36 collections, 91 actions, 22 inputs)
**FRD:** `M13_Academic_Structure_FRD_v2.5.1.docx` (latest across both filename
patterns; `v2.5` is the previous minor, `v2.5.1` the patch above it)
**Code checked at:** `main`, commit `60cb18c`, working tree as it stands

The module is unbuilt. There is no `apps/schools/vs_academics/`, no
`/v1/academics/` mount in `apps/apps/urls.py`, and no `schools.vs_academics` in
`INSTALLED_APPS`. The `academics` permission module is live with three resources
(`session`, `calendar`, `classes`) and thirteen keys; `structure` and `subject`
are not seeded.

So almost every row below is **Absent**. That is expected, and it is exactly why
the two rows that are not absent matter: one flag on an existing view unblocks a
scope control on eight screens, and one existing serializer starts lying the day
this module ships.

---

## 0. Decisions taken (product owner, 25 August 2026)

| # | Question | Answer | Consequence |
|---|---|---|---|
| 1 | Codes unique per kind, or across the catalogue? | **Per kind.** | The FRD's rule stands unchanged. The design's clash check is wrong and is a frontend correction, not a backend requirement. §7.1 below is closed. |
| 2 | Who sets a class's capacity? | **Anyone holding the key.** | `capacity` is a normal writable field on class create and update, gated by `academics.classes.create` / `.update`. The design has no field for it - that is a design gap the frontend must close. §7.2 closed. |
| 3 | Who wires promotion? | **As recommended.** Build `next_level`, the cycle guard and `cross_program` in the API now; no screen writes them in this release. | FR-005's progression stands as written. Until a screen exists every level is terminal, so M11 must not assume promotion is wired. §7.3 closed. |
| 4 | Session branch scope | **A session applies school-wide, or to a named set of branches.** | Delta 1.1 confirmed. `SessionBranch` join table; empty set means school-wide. |
| 5 | Un-archiving a year | **An archived year can be made active again, and no branch may hold two active sessions at once.** | Delta 1.2 confirmed **and widened**: the one-ACTIVE rule moves from per tenant to per branch. See §0.2. |
| 6 | May a branch break away from a running school-wide year? | **Yes.** Activating a branch's session narrows the year that covered it rather than being refused. | §0.1. Replaces the strict reading this plan first carried. `uq_academic_session_one_active` is withdrawn; narrowing, and the archive-when-emptied rule, are new. |

### 0.1  One active session per branch, and a branch may break away

FRD §6.1 holds one ACTIVE session per tenant with a partial unique constraint on
`tenant`. That constraint is withdrawn. The rule is now per branch, and
activating a branch-scoped session **narrows** whichever session already covers
those branches rather than being refused (product owner, 25 August 2026).

**The rule.** No branch may be in two ACTIVE sessions at once. Activating a
session takes every branch it covers away from whatever covered them before.

> Brightfield is running the school-wide **2026/2027**. In January the Lekki head
> puts Lekki on a British-style year and activates **2027 Lekki**.
>
> Lekki moves. 2026/2027 stops covering Lekki and carries on covering Ikeja,
> which never notices. Nothing is archived and nobody is interrupted.

**What the incumbent becomes, and why it is an include list.** A school-wide
session names no branches, because empty means everywhere. To stop covering
Lekki it has to start naming the branches it does cover, so it becomes an
explicit `{Ikeja}`. That is the shape the design already renders: the session
detail screen shows `dtBranches` as chips and the drawer offers "Selected
campuses". An "everywhere except Lekki" session would print "Applies school-wide"
on a screen where that is no longer true, and the drawer could not edit it -
picking "The whole school" clears the list.

**The consequence of that, stated plainly.** Automatic cover for a new campus
survives only until a school splits its calendar.

> Brightfield never splits. The 2026/2027 session names no branches. Yaba Campus
> opens in February and is inside the running year from the day it exists.
>
> Brightfield splits Lekki off in January, so 2026/2027 now names `{Ikeja}`. Yaba
> opens in February and is in **no** active session.

The second case is not a bug to paper over. Once a school is deliberately running
two calendars, there is no right answer to guess for a new campus, and guessing
either one is worse than asking. So it is surfaced rather than defaulted: a
branch in no active session is reported on the overview, and the school picks.

**The three guards.**

- **Constraint A (database).** At most one ACTIVE **school-wide** session per
  tenant: partial unique on `AcademicSession(tenant)` where `status='ACTIVE'` and
  the session names no branches.
- **Constraint B (database).** At most one ACTIVE session per branch: a
  denormalised `session_status` on `SessionBranch`, with a partial unique
  constraint on `SessionBranch(branch)` where `session_status='ACTIVE'`.
- **Step C (service, inside the activation transaction and under the same
  `select_for_update` as the promotion).** The narrowing. Neither constraint can
  see across the school-wide/branch-scoped boundary, so this is where it happens,
  and it runs before A and B are tested rather than instead of them.

**Step C in full.**

1. Work out which branches the incoming session covers. A school-wide session
   covers every branch of the tenant.
2. For each ACTIVE session that covers any of them, remove those branches. A
   school-wide incumbent is converted to an explicit list of the branches it
   keeps.
3. **If that empties an incumbent, archive it**, with its terms, through the
   existing archive path. A session covering no branches is not a school year.
   Brightfield has two campuses, both break away, and 2026/2027 ends.
4. Activate the incoming session, un-archiving it and its terms if it was
   archived (§0.2).
5. One `ACADEMIC_SESSION_ACTIVATED` audit event, plus one
   `ACADEMIC_SESSION_NARROWED` **NEW** per incumbent that changed shape, naming
   the branches that moved and where they went. Without it a school-wide year
   silently changes meaning with no trail.

**The reverse direction is the same rule.** Activating a school-wide session
covers every branch, so it takes them all back: every ACTIVE branch-scoped
session empties and is archived. That is the "bring everyone onto one calendar"
action, and it is the one that needs the loudest confirmation, because it ends
several years at once.

**The design's confirmation copy is now wrong** and is a frontend delta. The
modal says "Only one session can be active at a time, so 2026/2027 will stop
being active." For a breakaway that is false: 2026/2027 does not stop, it
narrows. It needs to name what moves and what stays, the way the department
narrowing modal already does: "Lekki Campus will move to 2027 Lekki. Ikeja
Campus stays on 2026/2027."

### 0.2  Re-activating an archived year un-archives its terms

These two rules collide as written, and the collision makes the new route
impossible rather than merely awkward.

FR-003 rule 7 archives every term of a session when that session is archived.
FR-002 rule 4 refuses to activate a session holding an archived term, 409
`SESSION_HAS_ARCHIVED_TERM`. So an ARCHIVED session has archived terms by
definition, and re-activating it would be refused every single time, naming every
term it has. The route would be dead on arrival.

**Resolution: activation clears `archived_at` on the session and on every one of
its terms, in the same transaction.** This is the only reading consistent with
the rest of the document. FRD §6.2 says a term has no lifecycle of its own apart
from its session and carries no status column for exactly that reason; terms
archive with their session, so they un-archive with it too. The invariant FR-002
rule 4 exists to protect - an ACTIVE session never holds an archived term - is
preserved rather than withdrawn, and the two revisions v2.4 and v2.5 spent
closing that trap stay closed.

The alternative was to withdraw FR-002 rule 4. It was rejected: it reopens the
exact state those revisions removed, and it would put the archived-term filter
back into every downstream reader of the year, where forgetting it is silent.

---

## 1. Screen -> data -> endpoint

| # | Screen | What it reads | What it writes | Endpoint | Bucket |
|---|---|---|---|---|---|
| 1 | **Overview** (hero + spine + six-row list) | Active session name, range, term states, % elapsed; counts of programs, levels, classes, subjects, departments, sessions | - | `GET /v1/academics/overview/` | **Absent** |
| 2 | **Overview - tree view** | Session -> Program -> Level -> Class -> Subject, each row with a "contains" count, a kind and a scope chip | - | `GET /v1/academics/structure/tree/` | **Absent** |
| 3 | **Sessions & Terms** (cards + table + timeline) | name, start, end, status, terms inline (name/start/end), scope label, term count | create session (+ terms), edit, activate, archive | `GET,POST /sessions/`, `GET,PATCH /sessions/<id>/`, `POST /sessions/<id>/activate/`, `POST /sessions/<id>/archive/` | **Absent** |
| 4 | **Session detail** | session header, scope chips, terms with state, calendar events per term | add event, delete event | terms: `GET,POST /sessions/<id>/terms/`, `PATCH,DELETE /terms/<id>/` — **events: M14, not ours** | **Absent** + **Not ours** |
| 5 | **Departments** (cards + table) | name, code, description, scope, status, program count | create, edit, delete, narrow scope | `GET,POST /departments/`, `GET,PATCH,DELETE /departments/<id>/` | **Absent** |
| 6 | **Programs & Levels** (accordion) | program name/code/scope/meta, its levels inline with code, scope and class count | create program, edit, delete; add level, bulk levels, edit level, delete level | `GET,POST /programs/`, `GET,PATCH,DELETE /programs/<id>/`, `GET,POST /programs/<id>/levels/`, `POST /programs/<id>/levels/bulk/`, `GET,PATCH,DELETE /levels/<id>/` | **Absent** |
| 7 | **Classes & Arms** (cards + table) | name, code, level, arm, scope, status, subject count | create, edit, generate arms, archive, **restore** | `GET,POST /classes/`, `POST /classes/generate-arms/`, `GET,PATCH /classes/<id>/`, `POST /classes/<id>/archive/`, `POST /classes/<id>/restore/` | **Absent** |
| 8 | **Subjects** (cards + table) | name, code, department, core/elective, offered-at summary, level count, scope | create (+ offerings), edit, delete | `GET,POST /subjects/`, `GET,PATCH,DELETE /subjects/<id>/`, `PUT /subjects/<id>/offerings/` | **Absent** |
| 9 | **Assignments** | nothing - deliberately empty, disabled CTA | - | none. M11 / M12 | **Not ours** |
| 10 | **Branch pill** (sidebar) + every "Applies to" scope control | the school's branches: id, code, name | - | `GET /v1/i/me/branches/` | **Closed** |
| 11 | **Export buttons** (5 screens) | - | starts an export | Export Centre datasets registered from `VsAcademicsConfig.ready` | **Absent** |
| 12 | **Duplicate code / name warning** in every drawer | whether a code or name is taken anywhere in the tenant, **including branches the caller cannot see**, and by what | - | see §5 - refusal detail or a check endpoint | **Absent** |
| 13 | Nav: Dashboard, Settings, palette entries for Students/Teachers/Branches | - | - | other modules | **Not ours** |

---

## 2. FRD delta

This is the section that changes a document rather than the code. The design is
the curated artefact and it outranks the FRD, so every item in list 1 is a
requirement the FRD must gain, not a question.

**Verdict: the FRD needs a new version before the backend is built.** The delta
is not cosmetic - it adds a table, two routes, changes one delete rule and
closes two of the ten open decisions. Recommended increment: **minor, v2.6**
(behaviour, contracts, acceptance and one additive model change; the functional
baseline, module taxonomy and endpoint base are untouched, so it is not major).

### List 1 - the design implies it, the FRD lacks or contradicts it

**1.1 A session applies school-wide or to a named set of branches.**
The drawer's session form carries "Applies to: The whole school / Selected
campuses" with a multi-select; the list carries a scope label; the session detail
carries scope chips; the session list is filtered by the branch pill
(`sessionInBranch`). FRD §6 says `AcademicSession` carries no branch column and
argues that two branches on different sessions would break the one-ACTIVE
constraint.

That argument does not apply to what the design actually asks for. The design
keeps the dates on the session - "The dates above apply everywhere this session
runs. A branch cannot keep its own term dates" - and its `setActive` handler
demotes whichever session is active across the whole tenant. So this is
*applicability*, not a second calendar, and `uq_academic_session_one_active` on
`tenant` survives unchanged.

**Confirmed by decision 4.** Shape: a `SessionBranch` through table (`tenant`,
`session`, `branch`, `session_status`, unique on session+branch), empty set
meaning school-wide, exactly as a null branch means school-wide everywhere else.
`AcademicSession` still carries no branch column; FRD §6's row for it changes
from "No" to "No column, a join table".

The FRD's reasoning for having no column at all was that two branches on
different sessions would break the one-ACTIVE-per-tenant constraint. Decisions 5
and 6 accept that consequence and take it deliberately: two branches on different
years is now a supported arrangement, `uq_academic_session_one_active` is
withdrawn, and §0.1 is what replaces it.

**1.2 An archived session can be made active again.**
The row menu computes `canSetActive: status !== "active"`, so it is offered on
DRAFT **and** ARCHIVED rows, and confirming it sets that session ACTIVE and
archives the incumbent. FRD FR-002 refuses an ARCHIVED session with 409
`SESSION_ARCHIVED_READ_ONLY`, and FRD §13 decision 12 asks whether a session may
be un-archived at all. **The design answers decision 12: yes, by activation**, and the product owner
confirmed it (decision 5). FR-002's precondition changes from "status is DRAFT"
to "status is not ACTIVE", and activation clears `archived_at` on the session
**and on every one of its terms** - see §0.2, without which the route is refused
every time it is called. FRD §13 decision 12 closes.

**1.3 A class can be restored.**
`cc.onRestore` / `cr.onRestore`, gated on `canRestore: status === "archived"`,
sets the class active again with no confirmation modal. The FRD has archive
(`is_active=False`) and nothing that reverses it. Add `POST
/classes/<id>/restore/` under `academics.classes.manage`, with its own audit
action type.

**1.4 A department cannot be deleted while programs are mapped to it.**
The design refuses with a blocking modal: "Cannot delete Sciences. 3 programs are
mapped to this department. Move them to another department first, then delete
this one." FRD FR-004 rule 4 says the opposite - the FK is `SET_NULL`, so the
delete succeeds and detaches - and FRD §11.2 has a test asserting the
detachment. **The design answers half of decision 4:** department -> programs is
a refusal. It says nothing about department -> subjects.

Cheapest honest shape: keep `SET_NULL` on the column (so no data is destroyed by
a race) and add a service guard that counts referencing programs and raises
409 `PROTECTED_REFERENCE` with `{Program: n}`, which is the body the design's
modal already renders.

**1.5 Every list carries counts the FRD does not specify.**
Department row: program count ("None" / "1 program" / "n programs").
Level row: class count. Class card: subject count, derived from the subjects
offered at that class's level. Subject row: level count and an "offered at"
summary that collapses a run into `JSS1-SSS3`. Session row: term count, plus the
terms inline so the card can draw its pills. All of these must be annotated in
the queryset, not computed per row.

**1.6 An overview endpoint.**
The hero needs the active session with its terms and their derived state, plus a
percentage elapsed; the spine and the six-row list need counts across five
models. Composing that client-side is six list calls for numbers. One
`GET /v1/academics/overview/` under `academics.structure.view`.

**1.7 The tree goes two levels deeper than FR-008 describes.**
FR-008 returns programs, their levels, and per-level class and subject counts,
with classes only at `depth=full`. The design's tree is Session -> Program ->
Level -> Class -> **Subject**, where the leaf rows are the individual subjects
offered at that class's level with Core/Elective in the "Contains" column and
their own scope chip. FR-008's response and its bounded-query rule both need
rewriting for five levels, not three.

**1.8 Filters the FRD does not name.**
`search` on all five lists (name and code). `branch` on department, program,
class, subject **and session** lists. `level` on classes. `is_core`
(all/core/elective) on subjects. `is_active` (all/active/archived) on departments
and classes. FRD §9's note only promises `is_active`.

**1.9 The duplicate refusal is UI text and must name the clashing row.**
The design renders two sentences verbatim under the field: an inline one ("This
code is already in use in this school.") and an explanation that names the
clashing row, what kind of thing it is, and **the branch it sits at, even when
the signed-in user cannot see that branch** - "Mathematics is already in use by
Yoruba at Ikeja Campus. Codes and names are unique across the whole school,
including campuses you do not have access to, so the same code cannot exist
twice." The platform's generic 400 `DUPLICATE` from the unique-constraint
handler carries none of that. See §5 for the two ways to serve it, and §7 for
the one question this raises.

**1.10 Narrowing a shared row to one branch is a supported edit.**
The design guards it with a confirmation modal ("Ikeja Campus will stop seeing
Sciences") but allows it. The FRD never says whether `branch` is patchable. It
must be, for an unnarrowed caller.

**1.11 Export Centre datasets for sessions, departments, programs, classes and
subjects.** Five Export buttons. FRD §12 lists the Export Centre as a thing M13
"will be expected to do" and writes no requirement for it.

**1.12 FR-012's dependency note is stale.** It says the onboarding task sits at
`order_index` 6. In `apps/schools/vs_onboarding/constants.py:137-142` the
`ACADEMIC_STRUCTURE` entry is required and at `order_index` **3**. The
requirement holds; the number does not.

### List 2 - the design asks for what the code cannot serve

Nothing. The one thing the design shows that this repo cannot answer is the
calendar - and that is another module's to build, not a capability the platform
lacks. It is §6.

### List 3 - the FRD wants what no screen shows

These are reported, not designed around. Two of them are load-bearing and need a
decision; the rest are legitimately backend-only.

| FRD item | Where | Assessment |
|---|---|---|
| **`SchoolClass.capacity`** | FR-006 rule 2, §6.6 | **Settled by decision 2: it is writable by anyone holding the class key.** The API carries it on create, update, list and detail. The design has no field for it, so this is a gap the frontend must close before capacity means anything - flagged, not designed around. |
| **`Level.next_level`** and the whole progression graph | FR-005 Progression, §6.5 | **Settled by decision 3: built now, unwired for now.** FR-005 stands as written, cycle guard and `LEVEL_CROSS_PROGRAM` included. No screen writes it in this release, so every level is terminal until one does, and M11 must not read a null `next_level` as a school that has finished wiring promotion. |
| `Program.order_index`, `Level.order_index` | §6.4, §6.5 | Backend-only, fine. Bulk creation assigns them; nothing reorders. |
| `AcademicTerm.order_index` and `TERM_ORDER_CONFLICT` | FR-003 rules 3 and 5 | Backend-only, fine. The design's term editor is an ordered list, so the index is the row position; the refusal still earns its place against a direct API caller. |
| `is_active` on Department, Program, Level, Subject | §6.3-§6.7 | Half-used. The department list has an "Archived" status filter and nothing that sets the state - the design deletes these rows instead. Keep the column and the filter; note that only classes have a UI that writes it. |
| FR-012 pending-tenant surface | FR-012 | Backend-only and correct. Keep exactly as written. |
| FR-009 archived-session read-only guard | FR-009 | Backend-only, and the design agrees with it: the read-only banner covers the whole screen when an archived session is selected. |
| `academics.classes.assign` | §3.7 | Seeded here, enforced by M11. No screen. Correct. |
| `cross_program=true` on `next_level` | FR-005 | Falls with `next_level`. |

---

## 3. Endpoints to open

One row, and it is the cheapest work in this plan.

| View | File | Change | Key | Who holds it |
|---|---|---|---|---|
| `MyBranchListView` | `apps/schools/vs_schools/views/my_branches.py:63` | add `pending_tenant_surface = True` | `school.branches.view` (unchanged) | already granted to every school administrator |

**Why.** Every "Applies to" control in the design needs the school's branch list,
and so does the sidebar branch pill. That list is served by
`GET /v1/i/me/branches/`, whose docstring says plainly: *"It is NOT on the
pending-tenant surface. Branches are a live-school screen; during onboarding the
control room is the whole app."* That reasoning was right for a branches screen
and wrong for this one, because academic structure is built **before** go-live -
the onboarding catalogue makes it a required task.

Brightfield Schools signs up with a Lekki and an Ikeja campus. Mrs Okonkwo, still
PENDING, reaches required task 3, "Academic Structure", and adds a General
Studies department for Ikeja only. She picks "One campus" and the dropdown is
empty, because `/v1/i/me/branches/` answered 403 `TENANT_NOT_LIVE`. She cannot
scope anything to a campus until the school is live, and the school cannot go
live until she finishes the task.

The detail route (`MyBranchDetailView`) does not need opening - no screen in this
design reads one branch.

Tests: a PENDING tenant reaches the list; the same call with the attribute
removed answers 403 `TENANT_NOT_LIVE`; an ACTIVE tenant is unaffected.

---

## 4. Endpoints to add

All under `/v1/academics/`, trailing slashes, `?tenant=<slug>`, the
`success_response` envelope, `XVSPagination` on every list, and
`pending_tenant_surface = True` on every view (FR-012). New rows against FRD §9
are marked **NEW**.

### Sessions and terms

| Method | Path | Key | Payload / filters | Refusals |
|---|---|---|---|---|
| GET | `/sessions/` | `academics.session.view` | `search`, `status`, `branch` **NEW** | - |
| POST | `/sessions/` | `academics.session.create` | `name`, `start_date`, `end_date`, `branch_ids[]` **NEW**, `terms[]` **NEW** (nested, name + dates) | 422 `INVALID_DATE_RANGE`, 400 `DUPLICATE`, 422 `TERM_OUTSIDE_SESSION`, 422 `TERM_DATES_OVERLAP`, 422 `TERM_ORDER_CONFLICT` |
| GET | `/sessions/<id>/` | `academics.session.view` | - | 404 cross-tenant |
| PATCH | `/sessions/<id>/` | `academics.session.update` | as POST; nested `terms[]` replaces the set **NEW** | 409 `SESSION_ARCHIVED_READ_ONLY`; ACTIVE may not move `start_date` |
| POST | `/sessions/<id>/activate/` | `academics.session.manage` | - | Narrows or archives whatever covered its branches (§0.1); 200 no-op if already active. **No longer refuses ARCHIVED** - it un-archives the session and its terms (§0.2). `ACTIVE_SESSION_EXISTS` is withdrawn: activation no longer collides, it displaces |
| POST | `/sessions/<id>/archive/` | `academics.session.manage` | - | archives every term in the same transaction |
| GET, POST | `/sessions/<id>/terms/` | `.view` / `.create` | `name`, `order_index`, dates | as POST `/sessions/` |
| PATCH, DELETE | `/terms/<id>/` | `.update` / `.manage` | - | 409 `TERM_SESSION_NOT_DRAFT`, 409 `SESSION_ARCHIVED_READ_ONLY` (guard runs first) |

The nested `terms[]` on session create is the design's shape, not a convenience:
the drawer has one Save button, per-term inline date validation, and a term list
that cannot be persisted before the session exists. Building it as
POST-then-N-POSTs means a half-created year whenever the second call fails.
The flat `/sessions/<id>/terms/` route stays - it is what the session detail
screen adds a term through.

### Departments

| Method | Path | Key | Notes |
|---|---|---|---|
| GET | `/departments/` | `academics.structure.view` | `search`, `is_active`, `branch`; annotates `program_count` **NEW** |
| POST | `/departments/` | `academics.structure.create` | `name`, `code` (generated when omitted), `description`, `branch` |
| GET, PATCH | `/departments/<id>/` | `.view` / `.update` | `branch` is patchable (delta 1.10) |
| DELETE | `/departments/<id>/` | `.manage` | **409 `PROTECTED_REFERENCE` `{Program: n}` when programs reference it** (delta 1.4) |

### Programs and levels

| Method | Path | Key | Notes |
|---|---|---|---|
| GET | `/programs/` | `academics.structure.view` | `search`, `branch`; **nests `levels[]`** with each level's `class_count` **NEW** - the screen is an accordion, so a flat list means one call per program |
| POST | `/programs/` | `.create` | `name`, `code`, `department`, `branch`, `order_index` |
| GET, PATCH, DELETE | `/programs/<id>/` | `.view` / `.update` / `.manage` | delete blocked by PROTECT -> 409 `PROTECTED_REFERENCE` `{Level: n}` |
| GET, POST | `/programs/<id>/levels/` | `.view` / `.create` | branch defaults from the parent program (design locks the control and says why) |
| POST | `/programs/<id>/levels/bulk/` | `.create` | `names[]`, one branch for the batch; 422 `DUPLICATE_IN_BATCH` names every offender, nothing created |
| GET, PATCH, DELETE | `/levels/<id>/` | `.view` / `.update` / `.manage` | delete blocked by PROTECT -> 409 `PROTECTED_REFERENCE` `{SchoolClass: n}` |

### Classes

| Method | Path | Key | Notes |
|---|---|---|---|
| GET | `/classes/` | `academics.classes.view` | `search`, `level`, `is_active`, `branch`; annotates `subject_count` **NEW** |
| POST | `/classes/` | `.create` | `level`, `arm`, `name`, `code`, `branch`, `capacity` |
| POST | `/classes/generate-arms/` | `.create` | `level`, `arms[]`, `branch`; skips labels already taken; idempotent |
| GET, PATCH | `/classes/<id>/` | `.view` / `.update` | |
| POST | `/classes/<id>/archive/` | `.manage` | `is_active=False` |
| POST | `/classes/<id>/restore/` | `.manage` | **NEW** (delta 1.3) |

### Subjects

| Method | Path | Key | Notes |
|---|---|---|---|
| GET | `/subjects/` | `academics.subject.view` | `search`, `is_core`, `branch`; annotates `level_count` and the offered-at summary **NEW** |
| POST | `/subjects/` | `.create` | `name`, `code`, `department`, `is_core`, `branch`, **`level_ids[]`** **NEW** - the drawer creates the subject and its offerings in one Save |
| GET, PATCH, DELETE | `/subjects/<id>/` | `.view` / `.update` / `.manage` | delete cascades offerings |
| PUT | `/subjects/<id>/offerings/` | `.update` | complete replacement set; a foreign level id 404s the whole call |

### Structure reads

| Method | Path | Key | Notes |
|---|---|---|---|
| GET | `/overview/` | `academics.structure.view` | **NEW** (delta 1.6). Active session + terms + elapsed %, and six counts |
| GET | `/structure/tree/` | `academics.structure.view` | `depth`, `branch`. **Five levels** (delta 1.7). Not paginated |

### Permission keys to seed

Two new resources on the existing `academics` module in
`core/management/commands/seed_school_permissions.py`, plus their
`RESOURCE_DESCRIPTIONS` rows, plus the matching entries in
`school-fe/src/permissions/index.ts` (the seeder's own comment requires
lockstep):

`academics.structure.view|create|update|manage`, `academics.subject.view|create|update|manage`,
with the holders in FRD §7.1. The seeder backfills tenants provisioned before
the keys existed, so adding rows to its table is the whole change.

### Audit

`AuditModuleKey.ACADEMICS` plus `ACADEMIC_SESSION_ACTIVATED`,
`ACADEMIC_SESSION_ARCHIVED`, `ACADEMIC_TERM_ARCHIVED`, `ACADEMIC_CLASS_ARCHIVED`,
`ACADEMIC_CLASS_RESTORED` **NEW**, `ACADEMIC_SESSION_NARROWED` **NEW**, `ACADEMIC_STRUCTURE_BULK_CREATED` - registered
in the same change that first emits them, or the trail is silently empty.

---

## 5. Shapes to change

**5.1 `SchoolBranchSerializer.classes_count` starts lying the day M13 ships.**
`apps/schools/vs_schools/serializers.py:1844` returns a hard `None` for
`classes_count`, and the docstring says why: *"There is no Student, Teacher or
Class model in the product yet, so a number here would be invented... the day
those models land this becomes an annotation without the response shape changing
under any client already reading it."* M13 lands `SchoolClass`. Annotate it in
`MyBranchListView.get_queryset()` in the same change.

Consumers of the old shape: `GET /v1/i/me/branches/` (list and detail) and
whatever the branches screen renders. The shape does not change - `null` becomes
an integer - which is what the docstring planned for.

**5.2 The `DUPLICATE` refusal needs a body the drawer can render.**
Two options; pick one before building.

- **(a) Enrich the refusal.** The create and update serializers do the uniqueness
  check themselves against `all_objects` for the tenant, and raise a domain
  exception carrying `error_code="DUPLICATE"` and an `extra` naming the clashing
  row's kind, name and branch. The platform's constraint-driven 400 `DUPLICATE`
  stays as the backstop for a race. One round trip, no new route.
- **(b) A check endpoint.** `GET /v1/academics/name-check/?kind=&field=&value=`.
  Cheap to build, but it is a second place the uniqueness rule lives, and the
  save path still has to refuse.

**Recommendation: (a).** The design's inline warning fires on blur, so the client
can afford to learn about the clash from the failed save it already has to
handle, and one rule in one place is the repo's standing preference.

---

## 6. Not ours

**M14 Academic Calendar and Timetables** - the session detail screen's whole
lower half. Each term renders its events (`dt.events`), with name, a date or a
date range, a category chip drawn from Term / Exam / Holiday / PTA / Results, a
delete control, and an "Add event to <term>" button that opens a drawer with
name, category, start and end. `academics.calendar.view|create|update|manage` are
already seeded and used by nothing.

What we need from M14, and what the frontend should park until it exists:
- `GET /v1/calendar/events/?session=<id>` returning events with the term they
  fall in (M14 derives the term from the event's dates and stores no FK)
- `POST /v1/calendar/events/` with name, category, start, end, session
- `DELETE /v1/calendar/events/<id>/`

M14 is specified as v3.0.1 and unbuilt. Do not invent these shapes here.

**M11 Student Management / M12 Staff Management** - the Assignments screen. It is
deliberately empty, both CTAs are `disabled`, and the design says so in its own
copy: *"This screen deliberately shows nothing rather than sample names."* No API
at all. `academics.classes.assign` is seeded here and enforced there.

**Other modules** - Dashboard, Settings, and the palette's Students, Teachers,
Administrators and Branches entries, which the design routes to `null` on purpose.

---

## 7. Questions

**All three are answered.** They are kept below in one line each so the reasoning
in §0 has something to point back at; the worked examples that produced them are
in the git history of this file.

1. **Codes unique per kind, or across the catalogue?** -> **Per kind.** The FRD's
   rule is unchanged. The design's cross-pool check is a frontend correction.
   One detail for v2.6 to state once: a level's name and code are unique **within
   its program** (FRD §6.5), not across the tenant, which FRD §5.3 words loosely.
2. **Who sets a class's capacity?** -> **Anyone holding the class key.** Writable
   field; the drawer needs one.
3. **Who wires promotion?** -> **Build it, leave it unwired.** FR-005 stands; no
   screen writes `next_level` in this release.

Two consequences arose from the answers rather than from these questions, and
both are settled in §0: the one-ACTIVE rule moves to per branch and a branch may
break away from a running school-wide year (§0.1, decision 6), and re-activation
must un-archive the session's terms or the new route is refused every time
(§0.2).

## 8. Dead API

None in this module's namespace - the namespace does not exist yet.

The audit in the other direction is §2 list 3: `capacity`, `next_level` and
`cross_program` are specified, will be built, and have no caller in this design.
Two of those are questions above rather than dead code.

---

## 9. Build order

1. ~~**The flag.** `pending_tenant_surface` on `MyBranchListView`.~~ **Done**,
   25 August 2026, with four tests including the removal case. Uncommitted.
2. ~~**The FRD.** v2.6 from §0 and §2.~~ **Done**, 25 August 2026. It closes
   decision 12, narrows decision 4, replaces `uq_academic_session_one_active`
   with three guards, rewrites FR-002, and adds FR-013 (session branch scope and
   narrowing), FR-014 (the reads the screens make) and FR-015 (export datasets).
   Nine models, 61 pages, awaiting review.
3. **Seeders and registry.** The two permission resources, the audit module key
   and the six action types. Nothing emits or gates correctly until these exist,
   and an unregistered audit action type fails silently.
4. **App, models, migration.** Eight models plus the `SessionBranch` join table,
   carrying the two partial unique constraints from §0.1.
5. **Sessions and terms**, then **departments**, **programs and levels**,
   **classes**, **subjects** - each with its 403, its cross-tenant 404, its
   PENDING-tenant case, its filters and its empty-list shape.
6. **Tree and overview**, with `assertNumQueries`.
7. **`classes_count`** on the branches serializer (§5.1).
8. **Export datasets** from `AppConfig.ready`.
9. **Seeder scenarios** - one multi-branch school and one single-branch school,
   driven through the real services, so the receding branch dimension is provable.

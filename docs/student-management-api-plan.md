# Student Management (M11) - API plan

> **Status: built.** This plan was approved and carried out. The module is
> `apps/schools/vs_students`, mounted at `/v1/students/` and `/v1/guardians/`,
> with 151 tests passing. The specification is M11 FRD v2.5 and the roadmap
> entry is MRD v2.47. What follows is the plan as approved; §10 records what
> the build changed about it.

**Design:** `docs/designs/Student_Management.html` (Brightfield Schools prototype,
9 screens, 5 drawers, a 7-step import wizard and a command palette).
**FRD:** `M11_Student_Management_FRD_v2.3.1.docx` (14 requirements, 6 models,
23 endpoints).
**Codebase:** commit `155c525`.

The headline: **there is no `schools/vs_students` app and no route under
`/v1/students/`.** Every screen in this design is Absent. Nothing is Closed and
nothing is the Wrong Shape inside this module, because this module does not
exist. The only pre-existing work is in two engine apps (`vs_import_data`,
`vs_exports`) and in M13, which has already shipped the `Level.next_level` and
`Level.is_terminal` columns the FRD named as FR-010's blocker.

So this is a module build, and the plan's value is not the bucket - it is the
**FRD delta**, which is large. The design asks for two statuses, five student
fields, five medical fields, a document store, a promotion outcome and eight
endpoints the FRD does not carry.

---

## 1. Screen -> data -> endpoint

| # | Screen | Reads | Writes | Bucket |
|---|---|---|---|---|
| 1 | **Student Directory** (`isDirectory`) | `dirRows` / `studentCards` (list + cards view), `statusBar` (8-status distribution), `capacityRows` (3 fullest classes), `statTotal` / `statActive` / `statApplicants` / `statUnassigned`, `classFilterOptions`, `levelFilterOptions`, `statusFilterOptions`, `dirPages` | `bulkAssign`, `bulkStatus`, `bulkPromotion`, `exportDirectory` | Absent |
| 2 | **Enrol student** (`isEnrol`) | `entryClassOptions`, `genderOptions`, `stateOptions`, `bloodOptions`, `guardMatches`, `docRows`, `capWarnText` | `submitEnrol`, `submitApplicant`, `attachPhoto`, `dr.onToggle` (attach document) | Absent |
| 3 | **Bulk import** (`isImport`) + wizard (`importOpen`) | `requiredColumns` (12), `batchRows` (history), `mapRows` (column mapping), `issueRows` / `badRows` (validation), `reviewLines`, `valTotal` / `valErrs` / `valWarns` / `valImportable` | `startImportRun`, `downloadTemplate`, `downloadErrors`, `abandonImport` | Engine exists, `students` dataset Absent |
| 4 | **Student profile** (`isProfile`) | `bioRows`, `contactRows`, `admissionRows` (Overview); `guardianCards` + `gc.siblings` (Guardians); `academicRows`, `subjectRows`, `trailRows` (Academic); `medicalRows` (Medical); `docRowsProfile` (Documents); `auditRows` (History); `lifeSteps` | `pEdit`, `pStatusChange`, `pTransfer`, `pPrint`, `addGuardianToProfile`, `gc.onRemove` | Absent |
| 5 | **Guardians list** (`isGuardians`) | `guardianRowsList` (name, phone, ward count, ward names, siblings flag), `gdPages` | - | Absent |
| 6 | **Guardian detail** (`isGuardianDetail`) | `gdPhone`, `gdEmail`, `gdOccupation`, `gdAddress`, `gdWards` (adm, class, status, relationship, primary) | `gdLinkAnother` | Absent |
| 7 | **Classes & transfers** (`isAssign`) | `unRows` (unassigned), `rosterRows` + `rosterSeatLine` + `rosterPctWidth` (class roster), `assignTargetOptions`, `rosterOptions` | `runAssign` (bulk), `rr.onMove` | Absent |
| 8 | **Applicants** (`isApplicants`) | `appCards` (age, applied-for, applied-on, guardian), `recentCards` (enrolled, not yet active), `rejectedCards` | `ac2.onEnrol`, `ac2.onReject`, `rc2.onActivate` | Absent |
| 9 | **Promotion** (`isPromotion`, 4 steps) | `mapRowsProm` (level mapping + TERMINAL), `promGroups` + `pg2.rows` (per-class, per-student outcome), `excRows` (exceptions), `cPromote` / `cRepeat` / `cGraduate` / `cHold` | `promConfirm` (run), `pr2.pick*`, `pg2.setAll*` | Absent |
| D1 | **Link guardian drawer** (`dwIsLink`) | `lgMatches`, `lgPickedWards`, `lgRelOptions` | `dwSave` | Absent |
| D2 | **Link child drawer** (`dwIsChild`) | `chMatches` | `dwSave` | Absent |
| D3 | **Edit drawer** (`dwIsEdit`) | bio / contact / medical sections, `edChangeLine` (diff preview) | `dwSave` | Absent |
| D4 | **Change status drawer** (`dwIsStatus`) | `stOptions` (allowed transitions only), `stImpact`, `stShowDest` | `dwSave` | Absent |
| D5 | **Transfer drawer** (`dwIsTransfer`) | `trDestOptions` (with seats), `trReasonOptions`, `trOverText` | `dwSave` | Absent |
| P | **Command palette** (`paletteOpen`) | `studentHits` (name + admission no.) | - | Absent |
| N | Branch pill, session pill | `branchMenu`, `sessionMenu` | - | **Served** by `/v1/i/me/branches/` and `/v1/academics/sessions/` |
| N | Class / level / subject lists | `entryClassOptions`, `levelFilterOptions`, `subjectRows` | - | **Served** by `/v1/academics/` |

---

## 2. FRD delta

The FRD is thorough and its security spine (branch scoping, 404-not-403, the
partial constraints, the pending-tenant rule) needs no change. The delta is
almost entirely **the design asking for things the FRD does not carry**, which
is the expected shape: the FRD was written from the code and the design was
drawn from the screens.

### 2.1 The design implies it, the FRD lacks it (the FRD is wrong)

**Model gaps**

1. **Two statuses are missing.** The design's fixed palette carries eight:
   `applicant, enrolled, active, graduated, transferred, withdrawn, suspended,
   **rejected**` and `**transferred**`. The FRD's `StudentStatus` has six. The
   Applicants screen's **Reject** button and the status drawer's **Transferred**
   option (which requires a *Destination school* free-text field) have no state
   to write. `ALLOWED` must gain `APPLICANT -> REJECTED` and
   `ACTIVE -> TRANSFERRED`, both terminal.
2. **`Student.applied_for` and `Student.applied_on`.** The Applicants board shows
   the level applied for and the date applied. The FRD's `APPLICANT` carries no
   application data at all, so an applicant created by any route is a student
   record with blank everything.
3. **Five student fields with no column:** `nationality`, `state_of_origin`,
   `phone`, `email`, `photograph`. All are on the enrol form and on the profile's
   Bio and Contact panels.
4. **Medical is five fields, not one.** The design collects and displays
   `blood_group`, `allergies`, `conditions`, `emergency_contact_name`,
   `emergency_contact_phone` as separate labelled rows. The FRD has a single
   `medical_notes` TextField gated on `school.students.view_sensitive`. One
   free-text field cannot serve five labelled rows, and the gating is wrong at
   this granularity: a school needs the emergency phone reachable by whoever
   picks up the phone, while allergies and conditions are the sensitive part.
5. **`Guardian.occupation` and `Guardian.address`.** Both are displayed - on the
   profile's guardian card (`gc.occupation`) and on the guardian detail
   (`gdOccupation`, `gdAddress`). Neither is on the FRD's Guardian.
6. **Student documents.** Five types with required flags - Birth certificate\*,
   Previous report card, Passport photograph\*, Transfer certificate,
   Immunisation record - attachable at enrolment, and shown on the profile as
   *Attached* with a date and a **View** link or *Not on file*. There is no model,
   no endpoint and no FRD requirement. `core.StoredFile` and `MediaView` already
   exist to hold and serve the bytes.
7. **`effective_date` on every status change and every transfer.** Both drawers
   mark it required. `StudentStatusLog` carries `changed_at` only, which is when
   the system recorded it, not when it takes effect for the school. The same
   column is needed on `ClassEnrolment`.
8. **A transfer reason, from a fixed list.** `Parent request, Stream change,
   Class balancing, Behaviour, Academic placement, Other`, required. FR-006 has
   no reason at all, so a class move is the one record-changing act in the module
   that would carry no explanation.
9. **`repeat` is a fourth promotion outcome.** The design's per-student control is
   *Promote / Repeat / Graduate / Hold*. A repeat writes a **new active enrolment
   in the same class for the next session** - it is a placement, not a no-op, and
   it is not "Hold". FR-010 has no such outcome.
10. **`ClassEnrolment.outcome`.** The profile's class-history trail shows
    `Promoted / Graduated / Current` per session. Nothing on the enrolment row
    records why it ended.

**Endpoint gaps**

11. **`GET /v1/students/summary/`** - the directory header. Total, active today,
    applicants awaiting enrolment, unassigned count, the distribution across all
    eight statuses, and the three fullest classes with `used/capacity` and a
    "nearest capacity" note. No FRD requirement covers any of it, and computing
    it client-side means paging the whole roll.
12. **`GET /v1/academics/classes/<id>/roster/`** (or `/v1/students/?class=`)
    with `seats_used` / `capacity`. The Classes & transfers screen's roster tab
    needs it; M13's class endpoints cannot serve it because the enrolment row is
    this module's.
13. **`GET /v1/guardians/`** - the Guardians screen. Searchable, paginated, each
    row carrying ward count, ward names and a siblings flag. The FRD has
    `GET /v1/guardians/<id>/students/` and no list.
14. **`GET /v1/guardians/<id>/`** - the guardian detail read. The FRD declares
    PATCH and DELETE on that path and no GET.
15. **`GET /v1/students/<id>/class-history/`** - the promotion trail.
16. **`GET /v1/students/<id>/history/`** - the profile's *Record history* tab is
    wider than the status log: it shows status changes, class assignments,
    guardian links **and** field edits, each with actor and timestamp
    (`au.kind` is one of `status | class | guardian | edit`). `status-history`
    covers a quarter of it; the rest is in `vs_audit` and needs a per-student
    read.
17. **`GET /v1/students/<id>/subjects/`** - the profile's Subjects panel, resolved
    from M13's `SubjectOffering` for the student's level.
18. **Bulk routes.** The directory's selection bar offers *Assign class*,
    *Change status* and *Start promotion* over N students, and the Classes screen
    assigns a multi-select in one action (`runAssign`). The FRD has per-student
    routes only, so the screen would fire N requests and have no answer for a
    partial failure.
19. **`POST /v1/students/<id>/reject/`** - the Applicants screen's Reject.
20. **Import history on this module's screen.** The design lists every batch this
    school has run, with the file name, an outcome chip, "36 created, 2 skipped"
    and an error-report download. The engine holds the batches; the FRD says only
    "the existing `/v1/import/` surface" and never says the module's own screen
    lists them.

**Behaviour gaps**

21. **`unplaced` is not the design's "unassigned".** The design counts students
    with no class **and** status in `{enrolled, active}` - that count drives a
    nav badge and the Classes screen. The FRD's `/v1/students/unplaced/` is
    "students with no active enrolment", which sweeps in applicants, withdrawn
    and graduated students and would make the badge wrong.
22. **The promotion preview returns a level mapping.** `from-class -> to-class`
    with a TERMINAL flag, resolved against real classes. The FRD's preview
    returns "counts per category and the flagged students with their reasons".
23. **Promotion exceptions have a vocabulary, and class-wide causes collapse to
    one row.** The design names four causes with their sentences - terminal class,
    no class at the next level, suspended, no class assigned - and states a
    class-wide cause once rather than per student. That copy is API output.
24. **Refusals that are messages, not errors.** Opening *Transfer* on a graduated
    student, or *Change status* on a terminal status, produces a sentence
    ("Ahmed is graduated, so there is no class to move.") rather than a form.
    These need domain refusals whose message is the sentence.
25. **The import template is missing `branch`.** The design's 12 columns omit it,
    but the design also carries a **branch switcher** - so this is a
    single-branch prototype simplification in a multi-branch product, and
    FR-012's rule ("`branch` is required of a school with more than one branch")
    is right. Keep the column. Same for `guardian_email`, which FR-012 rule 4
    matches guardians on and the design's template does not carry.

### 2.2 The design and the FRD contradict each other (the design stands)

| # | The design | The FRD | Resolution |
|---|---|---|---|
| 26 | Capacity at enrolment and at transfer is a **warning**: *"You can still enrol, but the class will be over capacity."* / *"...You can still do it."* | 422 `CLASS_AT_CAPACITY`, overridable only by a caller holding `school.students.manage` (FR-002 r3, FR-006 r3) | **Design stands.** Keep `CLASS_AT_CAPACITY` for the *unacknowledged* case and let the screen's warning map to `allow_over_capacity=true`, but the override must not require `manage` - the design shows it to whoever is enrolling. |
| 27 | `withdrawn -> enrolled`, with the impact line *"The student is put back on the roll and will need a class assigned."* | `WITHDRAWN -> ACTIVE` | **Design stands**, and it is also more coherent with the FRD's own FR-009 r2, which requires a new placement. Readmission reaches ENROLLED; placement then reaches ACTIVE. |
| 28 | Eight relationships: Father, Mother, Uncle, Aunt, Grandparent, Legal guardian, Sibling, Other | Five: MOTHER, FATHER, GUARDIAN, GRANDPARENT, OTHER | **Design stands.** Eight choices. |
| 29 | Six statuses on the state machine, eight in the palette | Six | Covered by delta 1. |
| 30 | Admission number is **required** at enrolment, validated against a format, and unique | *"Nothing generates it, no format is enforced, and it is not required"* (§7.1) | **Question for you - see §7.** The design's format is `BFS/YYYY/NNNN`, which is Brightfield's own. Following the design literally special-cases one tenant. |

### 2.3 The design asks for what the code cannot serve

31. **"Guardians are notified by email"** - printed in the suspension impact
    panel. There is no `student.suspended` notification event type, and the FRD
    (§8.3, decision 8) states plainly that no guardian is notified of anything by
    this module because no guardian-facing event exists and inventing one is a
    product decision. The sentence is on screen either way, so it is either a
    promise the platform does not keep or an event type somebody has to decide.
32. **"Billing stops at the effective date"** - printed in the withdrawal impact
    panel. `vs_finance` has no student and no fee assignment to stop; the FAL's
    `StudentCustomerPort` maps a student to an AR customer and has no fee
    schedule behind it. This module may not add one (FRD §7.6). The screen tells
    a school something that will not happen.

Both are copy on a confirmation panel, so a school reads them at exactly the
moment it is deciding. They go in the FRD's refusal list.

### 2.4 The FRD requires it, no screen shows it (backend-only - wave through)

33. `pending_tenant_surface` on the import surface only, `TENANT_NOT_LIVE`
    elsewhere (FR-013) - backend-only, keep.
34. `BRANCH_CHANGE_NOT_SUPPORTED` on the edit route (FR-004 r4) - backend-only,
    keep.
35. Export Centre sensitive-field auditing and the exclusion of medical data from
    the dataset (FR-014) - backend-only, keep.
36. `school.students.view_sensitive` gating - the design shows the Medical tab
    with no gate, but a field-level gate is invisible by design. Keep, narrowed
    per delta 4.
37. **`confirm_duplicate=true` (FR-002 r2) has no screen.** The enrol form has no
    duplicate prompt, so a 409 `DUPLICATE_STUDENT` is a dead end the user cannot
    clear. Either the design needs the prompt or the check becomes advisory-only.
    **This one is a real gap, not backend-only.**
38. **`previous_school`** - on the FRD's model, on no screen and in no form. Keep
    the column (the import template fills it), but nothing in the UI reads it.

---

## 3. Endpoints to open

**None.** No view in this module exists to open. The one adjacent item:

- `vs_import_data`'s views already declare `pending_tenant_surface`, so FR-013's
  import half is satisfied the moment the `students` dataset type exists. No flag
  to add.

---

## 4. Endpoints to add

All under `/v1/students/` and `/v1/guardians/`, `?tenant=<slug>` required,
`success_response` envelope, `XVSPagination` on every list, and **no**
`pending_tenant_surface` on any of them (FR-013).

### 4.1 The FRD's own list, unchanged

| Verb | Path | Key | FR |
|---|---|---|---|
| GET | `/v1/students/` | `school.students.view` | FR-001 |
| POST | `/v1/students/` | `school.students.create` + `academics.classes.assign` | FR-002 |
| GET | `/v1/students/<id>/` | `school.students.view` | FR-001 |
| PATCH | `/v1/students/<id>/` | `school.students.update` | FR-004 |
| GET | `/v1/students/unplaced/` | `school.students.view` | FR-001 (redefined, delta 21) |
| POST | `/v1/students/<id>/confirm/` | `school.students.update` | FR-003 |
| POST | `/v1/students/<id>/assign-class/` | `academics.classes.assign` | FR-006 |
| POST | `/v1/students/<id>/withdraw/` | `school.students.manage` | FR-007 |
| POST | `/v1/students/<id>/suspend/` | `school.students.manage` | FR-008 |
| POST | `/v1/students/<id>/reactivate/` | `school.students.manage` | FR-009 |
| GET | `/v1/students/<id>/status-history/` | `school.students.view` | FR-011 |
| GET, POST | `/v1/students/<id>/guardians/` | `view` / `update` | FR-005 |
| PATCH, DELETE | `/v1/guardians/<id>/` | `school.students.update` | FR-005 |
| GET | `/v1/guardians/<id>/students/` | `school.students.view` | FR-005 |
| POST | `/v1/students/promotions/preview/` | `school.students.manage` | FR-010 |
| POST | `/v1/students/promotions/` | `manage` + `academics.classes.assign` | FR-010 |
| GET | `/v1/students/promotions/<id>/` | `school.students.manage` | FR-010 |

### 4.2 New, from the design

| Verb | Path | Key | Payload / returns | Refusals | Delta |
|---|---|---|---|---|---|
| GET | `/v1/students/summary/` | `school.students.view` | counts by status, `active_today`, `applicants`, `unassigned`, top-3 fullest classes with `used`/`capacity` | - | 11 |
| GET | `/v1/students/<id>/class-history/` | `school.students.view` | session, class, `outcome` | - | 15 |
| GET | `/v1/students/<id>/history/` | `school.students.view` | merged status + class + guardian + edit events, actor, timestamp | - | 16 |
| GET | `/v1/students/<id>/subjects/` | `school.students.view` | subjects at the student's current level | - | 17 |
| GET | `/v1/students/<id>/documents/` | `school.students.view` | type, label, required, attached, `uploaded_at`, media url | - | 6 |
| POST | `/v1/students/<id>/documents/` | `school.students.update` | `type`, file | `VALIDATION_ERROR` on an unknown type | 6 |
| DELETE | `/v1/students/<id>/documents/<doc_id>/` | `school.students.update` | - | 404 cross-tenant | 6 |
| POST | `/v1/students/<id>/reject/` | `school.students.update` | `reason`, `effective_date` | `INVALID_STATUS_TRANSITION` unless APPLICANT | 19 |
| POST | `/v1/students/<id>/transfer-out/` | `school.students.manage` | `destination_school`, `reason`, `effective_date` | `REASON_REQUIRED`, `DESTINATION_REQUIRED` | 1 |
| POST | `/v1/students/bulk/assign-class/` | `academics.classes.assign` | `student_ids[]`, `class_id`, `allow_over_capacity` | per-row results | 18 |
| POST | `/v1/students/bulk/status/` | `school.students.manage` | `student_ids[]`, `to_status`, `reason`, `effective_date` | per-row results | 18 |
| GET | `/v1/guardians/` | `school.students.view` | search, ward count, ward names, siblings flag, paginated | - | 13 |
| GET | `/v1/guardians/<id>/` | `school.students.view` | phone, email, occupation, address, wards | 404 cross-tenant | 14 |
| GET | `/v1/students/search/` | `school.students.view` | palette: name + admission no., `limit` capped | - | palette |
| GET | `/v1/academics/classes/<id>/roster/` | `academics.classes.view` + `school.students.view` | roster rows, `seats_used`, `capacity` | 404 cross-tenant | 12 |

📌 The roster route lives under `/v1/academics/` because that is where the class
is, but it is served by this module's queryset. Registering it from
`vs_academics` would make an M13 view import `vs_students`; mount it here and
route it under the academics prefix, or accept `/v1/students/?class=<id>` plus a
seats block on the class detail. **Second option is simpler and I would take it
unless you want the roster URL to read like a class URL.**

### 4.3 Models to add

Beyond the FRD's six: **`StudentDocument`** (tenant, student, `doc_type`,
`stored_file` -> `core.StoredFile`, `uploaded_by`, `uploaded_at`), and the
columns in deltas 2-5, 7, 8, 10.

### 4.4 Permission keys

Two new, exactly as the FRD says: `school.students.import` (SENSITIVE,
`school_admin`) and `school.students.export` (SENSITIVE, `school_admin`,
`branch_admin`). Both go in `apps/core/management/commands/seed_school_permissions.py`
beside the five that are already there, with the backfill phase. No new verb -
`import` and `export` are already seeded actions. **Do not** re-register
`academics.classes.assign`; it is at `seed_school_permissions.py:140`.

### 4.5 Audit action types

`STUDENT_ENROLLED`, `STUDENT_CLASS_ASSIGNED`, `STUDENT_CLASS_TRANSFERRED`,
`STUDENT_WITHDRAWN`, `STUDENT_SUSPENDED`, `STUDENT_REACTIVATED`,
`STUDENT_GRADUATED`, `STUDENT_PROMOTION_RUN`, `STUDENT_GUARDIAN_LINKED`,
`STUDENT_GUARDIAN_UNLINKED` - **none exists in `AuditActionType` today.** Plus
`STUDENT_REJECTED` and `STUDENT_TRANSFERRED_OUT` for delta 1.

---

## 5. Shapes to change

Two, both in engine apps, both with existing consumers.

1. **`vs_import_data.models.DatasetTypeChoices`** gains `STUDENTS = "students"`,
   with `import_students_row` beside `import_schools_row` / `import_branches_row`
   / `import_cx_users_row`, one branch in `execute_dataset_handler` and one in
   `_validate_dataset_specific_rules`, plus an `ImportTemplate` data migration.
   Consumers: `ImportBatch.dataset_type`, `ImportTemplate.dataset_type`, and the
   `execute_import` bank-statement diversion, which students must **not** take.
2. **`vs_import_data/permissions.py:8` `HasImportBatchRBACPermission`** - its
   fallback is hard-coded to `finance.bankaccount.import` and a
   `BANK_STATEMENTS` batch (lines 27-41). As written, `school.students.import`
   cannot stand in for the generic `import.batches.*` keys, so a school admin
   holding the module key is refused. Fix: a `{dataset_type: permission_key}`
   registry the fallback reads. Consumer: the bank-statement wizard, which must
   keep working through the registry's first entry.

---

## 6. Not ours

| Module | What we need | State |
|---|---|---|
| **M13 Academic Structure** | `SchoolClass`, `AcademicSession`, `Level.next_level`, `Level.is_terminal`, `SubjectOffering`, `SchoolClass.capacity` | **All shipped.** FR-010's stated blocker is gone. |
| **M12 Staff Management** | The recipient of `student.enrolled` | Not built. FR-002 dispatches nothing; keep it that way. |
| **M28 Parent Portal** | Guardian login, invitation, revocation | Not this module's. Three platform changes stand in front of it (tenant-aware sign-in, per-tenant `User.email` uniqueness, PARENT in the branch-constraint exemption). |
| **vs_finance / FAL** | Anything that would make delta 32's "billing stops" true | `StudentCustomerPort` exists; no fee schedule exists. |

---

## 7. The admission number: settled

**Delta 30 - the admission number.**

The design makes it required at enrolment and validates it against
`BFS/YYYY/NNNN`, refusing anything else with *"Use the BFS/YYYY/NNNN format."*
The FRD says the opposite: optional, no format, nothing generates it.

The design wins on the rule that the design outranks the FRD. But `BFS/` is
Brightfield's own prefix, and CLAUDE.md says nothing may be special-cased to one
tenant's arrangement.

> Corona Secondary School numbers its students `CSS-24-0117`. Bright Star School
> writes `2025/JSS1/09`. Under a hard-coded `BFS/YYYY/NNNN` regex, both schools
> get *"Use the BFS/YYYY/NNNN format"* on every single enrolment and cannot
> register a child at all. Under the FRD's rule, Brightfield's registrar types
> `BFS/2025/O142` with a letter O and nobody notices until the fees run.

So the honest reading of the design is **a per-school admission-number setting**:
required or optional, with an optional regex the school sets once, defaulting to
optional-and-unvalidated so a school that has not set one behaves exactly as the
FRD describes. That is one small model on `vs_config` or on the school profile,
and it makes the design's screen literally true for Brightfield without breaking
anyone else.

**DECIDED (approved):** the per-school admission-number setting. Required-or-not
plus an optional pattern the school sets once, defaulting to
optional-and-unvalidated so a school that has not configured one behaves exactly
as FRD v2.3.1 §7.1 describes.

---

## 8. Dead API

None. There is no endpoint in this module's namespace that no screen calls,
because there is no endpoint in this module's namespace.

Going the other way, three FRD requirements have no screen and are noted in
§2.4: `confirm_duplicate` (delta 37 - a real gap, the 409 has no way out),
`previous_school` (delta 38 - written by import, read by nothing), and
`BRANCH_CHANGE_NOT_SUPPORTED` (backend-only, correct as is).

---

## 9. Does the FRD need a new version?

**Yes - v2.4, a minor version.** The delta changes the model set (two statuses,
one new model, twelve columns), the endpoint list (fifteen new routes), the
state machine's transition table, the audit vocabulary and two error contracts.
That is behaviour of record, not presentation, so it is a minor bump and not a
patch. Sections 7, 8.2, 9 (FR-005, FR-006, FR-010, FR-011), 10, 11 and 12 all
move; sections 13 and 14 move too - FR-010's M13 dependency is now satisfied and
should be struck, and deltas 31 and 32 join section 14 as product questions.


---

## 10. What the build changed about this plan

Five things the plan did not know, all found by building it. Each went the same
way - the code was right and the plan was wrong or silent.

1. **A student photograph and a student document are `FileField`s, not foreign
   keys to `core.StoredFile`.** Only a real `FileField` is walked by
   `core.binding`, so only one gets its stored row bound to the record, retired
   when it is replaced, and refused to another tenant by `core.media.authorize`.
   A bare foreign key skips all three, and the birth certificate is then served
   to anyone signed in who has ever seen the URL.

2. **The promotion target is resolved across sessions by level CODE, not by
   `next_level`.** Levels and classes belong to a year and a new year is seeded
   from the old one keeping each code, so `next_level` points at the *same*
   year's next level. Following the FRD literally would have placed the whole
   cohort back into the year it had just left, with every row looking valid.
   This is the single most important correction in the build.

3. **The two-key routes cannot declare both keys.** `rbac_permission` is
   any-of, deliberately, so listing `school.students.create` and
   `academics.classes.assign` together admits a caller holding either alone.
   The second key is checked explicitly, before validation, so the refusal is a
   403.

4. **Clearing an admission-number rule is a deletion, not an empty write.**
   `vs_config` treats an empty string as unset and refuses to store one, so a
   school that had set a pattern could never take it off.

5. **Three platform registries had to be told about the module**, each guarded
   by a test that fails until it is: the export dataset needs a locked identity
   column, the two new permission keys need a permission-group bundle, and the
   two new `FileField`s need a binding entry. None of the three is mentioned in
   any FRD, and each fails as something other than what it is.

One defect was fixed in an engine app rather than worked around:
`HasImportBatchRBACPermission` hard-coded its fallback to bank statements, so
`school.students.import` could never reach the wizard however it was granted.
It now reads a `{dataset_type: permission_key}` registry that domain apps write
into from `AppConfig.ready`.

---

## 11. Student settings

A school sets its own rules for students in the Settings console. Every rule
lives in `vs_config` as a school-scoped definition, so it inherits
platform, school and (for admission numbers) branch values the way every other
setting does, and every write is audited as `config.value.updated` or
`config.value.cleared`. Every default is the behaviour every school had before
it could choose, so a school that has saved nothing is treated exactly as
before.

The definitions are declared by `vs_students` migration 0006 and by
`seed_config_catalogue`:

| Key | Type | Default | Scopes |
|---|---|---|---|
| `students.age.min_years` | INTEGER 0-99 | 2 | platform, school |
| `students.age.max_years` | INTEGER 0-99 | 25 | platform, school |
| `students.documents.required` | JSON list | `["BIRTH_CERTIFICATE"]` | platform, school |
| `students.enrolment.required_fields` | JSON list | `[]` | platform, school |
| `students.capacity.mode` | CHOICE WARN, HARD, OFF | WARN | platform, school |
| `students.capacity.default` | INTEGER 1-500 | none (no limit) | platform, school |
| `students.admission_number.auto_issue` | BOOLEAN | false | platform, school, branch |
| `students.admission_number.required`, `.pattern`, `.hint` | as before | as before | now also branch |

### 11.1 Enrolment rules: `GET, PUT /v1/students/enrolment-rules/`

GET needs `school.students.view` (the enrolment form renders from it); PUT
needs `school.settings.update`. The school is `request.tenant`; nothing in the
request names another.

```json
{
  "min_age_years": 2, "max_age_years": 25,
  "required_documents": ["BIRTH_CERTIFICATE"],
  "document_types": [{"value": "BIRTH_CERTIFICATE", "label": "Birth certificate"}, "... every DocumentType"],
  "required_fields": [],
  "optional_fields": [{"value": "nationality", "label": "Nationality"}, "... the ten below"],
  "capacity_mode": "WARN",
  "default_capacity": null
}
```

`optional_fields` is the ten enrolment fields a school may make required:
nationality, state of origin, home address, student phone, student email,
previous school, emergency contact, emergency phone, blood group and middle
name, each labelled as the enrol form labels it. Allergies and
conditions are left out on purpose: for most children the answer is "none".

PUT takes every rule every time, plus an optional `reason`, and answers with
the GET body. Refusals are 400 `REQUEST_ERROR`, keyed on the field:
`0 <= min < max <= 99`, the documents a subset of the document types, the
fields a subset of `optional_fields`, the default class size null or 1 to 500.
Saving an unchanged value writes nothing and audits nothing.

### 11.2 What each rule changes

- **Age.** `ages.date_of_birth_problem` reads the school's range, on the
  enrolment form, the edit route and the import alike. The refusal names the
  school's bounds ("this school enrols students from 4").
- **Required documents.** The checklist's `required` flags and
  `missing_required` read the school's list. Still a prompt, never a gate.
- **Required fields.** Enrolment (and saving an applicant) refuses a blank
  required field, keyed on it: "Nationality is required at this school." An
  edit refuses only a field it sends blank, so a record older than the rule is
  never blocked. The import refuses a row per required field **it has a column
  for** (middle name, address, previous school); a required field with no
  import column cannot be supplied by any file and is filled in afterwards.
- **Capacity.** WARN is today's rule (422 `CLASS_AT_CAPACITY` until
  `allow_over_capacity`). HARD refuses with 422 `CLASS_FULL` whatever the
  caller sends, on one placement, enrolment, reactivation, a bulk assignment
  (the whole selection, before anyone is placed) and a promotion run (with the
  preview's `classes` list). An import under HARD refuses each row past the
  last seat at validation, before anything is written. OFF checks nothing and
  the promotion preview's `over_capacity` is empty. The promotion preview
  carries `capacity_mode`.

  ```json
  {"success": false,
   "message": "JSS1 B holds 2 of 2 seats, and this school does not put classes over capacity.",
   "error": {"code": "CLASS_FULL",
             "detail": {"school_class": 12, "capacity": 2, "used": 2, "adding": 1}}}
  ```
- **Default class size.** A class created without a capacity (the class form,
  or a null capacity there, and "generate arms") gets it. Editing a class never
  applies it, and the academic-structure import keeps "blank means no limit".

### 11.3 Admission numbers per branch: `/v1/students/admission-number-policy/`

GET, PUT and DELETE take an optional `?branch=<id>`, which must be this
school's branch and one the caller can see, or the answer is 404. GET needs
`school.students.view`; PUT and DELETE need `school.students.update`.

```json
{"required": true, "pattern": "IKJ/\\d{4}", "hint": "Use IKJ/NNNN.",
 "auto_issue": true, "source": "branch", "suggestion": "IKJ/0008"}
```

`source` is `branch` when the branch holds its own rule, `school` when the
school has set one, and `default` otherwise. **A branch's rule replaces the
school's whole**: PUT with a branch writes all four values at branch scope, and
an empty pattern or hint there means "none", not "the school's". `auto_issue`
may be left out of a PUT, which keeps the value in force. DELETE with a branch
removes its rule so it follows the school's again; DELETE with no branch is a
400 keyed on `branch`. PUT and DELETE answer with the GET body.

Enrolment, confirmation, an edit to the number and the import all use the rule
of the student's branch. The suggestion continues the branch's own series when
it has a rule, the school's otherwise. Numbers stay unique across the whole
school, without case.

**Automatic numbers.** With `auto_issue` on, a number left blank at enrolment
or at applicant confirmation (not when saving an applicant) is the suggestion.
A collision with another enrolment, whether seen before the insert or at the
unique constraint, moves to the next suggestion, up to five times; five in a
row is a 409 `DUPLICATE_STUDENT_NUMBER` asking to save again. When there is no
suggestion (no series yet, or the next number would break the pattern) the
rule applies as if automatic numbers were off: required refuses, optional
leaves the number blank. The student import follows the same rule: a blank
number passes validation where the child's branch issues numbers and a next one
can be worked out, and each row is given the next number in turn when written.

## 12. Guardian settings

A school sets its own guardian rules in the Settings console, beside the
student settings of section 11 and in the same way: four school-scoped
`vs_config` definitions, declared by `vs_students` migration 0007 and by
`seed_config_catalogue`, read through `services/guardian_rules.py`, every write
audited as `config.value.updated`. Every default is the behaviour every school
had before it could choose.

| Key | Type | Default | Scopes |
|---|---|---|---|
| `guardians.min_per_student` | INTEGER 1-4 | 1 | platform, school |
| `guardians.email_required` | BOOLEAN | false | platform, school |
| `guardians.matching` | CHOICE EMAIL_THEN_PHONE, EMAIL_ONLY | EMAIL_THEN_PHONE | platform, school |
| `guardians.relationships.extra` | JSON list of labels | `[]` | platform, school |

A value stored by hand at the platform layer is cleaned on read: a minimum out
of range reads as 1, an unknown mode as EMAIL_THEN_PHONE, and the added
relationships lose blanks, repeats, overlong labels and fixed ones.

### 12.1 Guardian rules: `GET, PUT /v1/students/guardian-rules/`

GET needs `school.students.view` (the enrolment form and the guardian drawer
render from it); PUT needs `school.settings.update`. The school is
`request.tenant`; nothing in the request names another.

```json
{
  "min_per_student": 1,
  "email_required": false,
  "matching": "EMAIL_THEN_PHONE",
  "matching_options": [
    {"value": "EMAIL_THEN_PHONE", "label": "Email, then phone"},
    {"value": "EMAIL_ONLY", "label": "Email only"}
  ],
  "extra_relationships": ["Sponsor", "Driver"],
  "relationships": [
    {"value": "MOTHER", "label": "Mother"}, "... the fixed ones except OTHER",
    {"value": "Sponsor", "label": "Sponsor"}, {"value": "Driver", "label": "Driver"},
    {"value": "OTHER", "label": "Other"}
  ]
}
```

`relationships` is the whole list a relationship picker offers: the fixed
choices except OTHER in their own order, then the school's own (whose value is
the label, because that is what every write path accepts), then OTHER last.

PUT takes `min_per_student`, `email_required`, `matching` and
`extra_relationships` every time, plus an optional `reason`, and answers with
the GET body. The added relationships are each trimmed, 1 to 30 characters,
distinct ignoring case, and not a fixed code or label ignoring case; at most
10. Refusals are 400 `REQUEST_ERROR` keyed on the field, in sentences:

```json
{"success": false,
 "message": "min_per_student: A school can ask for at most 4 guardians for every child.; ...",
 "error": {"code": "REQUEST_ERROR",
           "detail": {"min_per_student": ["A school can ask for at most 4 guardians for every child."],
                      "matching": ["Choose EMAIL_THEN_PHONE or EMAIL_ONLY for how guardians are matched."],
                      "extra_relationships": ["'Mother' is already a relationship every school has."]}}}
```

The other `extra_relationships` sentences: "A school can add up to 10
relationships of its own.", "A relationship needs a name.", "'...' is longer
than 30 characters.", "'...' is listed twice." Saving an unchanged value writes
nothing and audits nothing.

### 12.2 What each rule changes

- **Minimum guardians.** `assert_guardian_set` reads the school's minimum at
  enrolment and when saving an applicant. None at all stays 422
  `GUARDIAN_REQUIRED` (its message names the minimum where it is above one);
  some but too few is a 400 keyed on `guardians`: "This school asks for 2
  guardians for every child." Unlinking refuses to take a child on the roll
  below the minimum, as 422 `GUARDIAN_REQUIRED`: "This school asks for 2
  guardians for every child, and Chiamaka has 2. Link another before removing
  this one." (the last guardian keeps its "only guardian" sentence). A child
  not on the roll can be left with none. The student import carries one
  guardian per child, so with a minimum above one every row imports with a
  warning naming it, and the others are added afterwards. Applicant
  confirmation does not re-check the minimum.
- **Email required.** A new guardian needs an email on enrolment (keyed
  `guardians[i].email`), the link endpoint (keyed `email`) and the guardians
  import (an error on the row): "A guardian email is required at this
  school." `upsert_guardian` refuses the same at the point it would create the
  row, so no caller can skip it. Linking a guardian the school already holds
  with no email is allowed, whether named by id or by a phone the school
  matches on. An edit may not blank an email a guardian has (keyed `email`);
  a guardian who never had one can still be edited. The student import
  refuses any row whose guardian email is blank.
- **Matching.** `match_existing` takes the school's mode, and every caller
  follows it: enrolment, the link endpoint, both imports (including the
  student import's "already uses that contact" warning, which says nothing of
  a phone under EMAIL_ONLY). Under EMAIL_ONLY two families sharing a landline
  stay two guardians; under EMAIL_THEN_PHONE they are one, as before.
- **The school's own relationships.** Every write path that takes a
  relationship (enrolment rows, the link endpoint, the relink PATCH, the
  student import, the guardians import) accepts a fixed code or label, or one
  of the school's labels, ignoring case. A school label is stored as
  `relationship = "OTHER"` with `relationship_detail` holding the label as the
  school spelled it (a new `StudentGuardian` column, 30 characters, blank for
  every existing link). Anything else is refused on `relationship` ("'Neighbour'
  is not a relationship this school records. Pick one from the list, or add it
  in Settings, Guardians."), except in the two imports, which import it as
  Other with a warning as before. The relink PATCH validates its value
  like every other path. Removing a label from the school's list leaves
  stored links reading as they were written.
- **`relationship_label`** is the detail where one is stored, else the fixed
  label. It is carried by every read payload that carries a relationship: the
  student's guardians list (`GET /v1/students/<id>/guardians/`, live and
  `?as_at`), the guardian's wards (`GET /v1/guardians/<id>/`, live and
  `?as_at`), and the guardians import's row results (`normalized_payload`).
- **Template guidance.** `vs_import_data` migration 0022 rewords both
  templates, swapping only sentences that still read as written, reversibly:
  matching is "email first and phone second, or on email alone where your
  school has chosen that", a required guardian email is named among what is
  refused, the school's own relationships are recognised, and the students
  template names the per-row minimum warning.

## 13. Applicant settings

A school sets its own applicant rules in the Settings console, beside the
student and guardian settings: the **admission stages** its applications pass
through, in its own order, and the **documents** an applicant must hold before
being confirmed. A school that names no stages and requires no documents
admits exactly as every school always has.

A stage is where an APPLICANT stands and nothing more. The statuses APPLICANT,
ENROLLED and REJECTED and every transition between them are unchanged, and the
stage does not gate confirmation. One or more stages may be **offer** stages,
each with a number of days the family has to accept. When that window passes
the applicant is flagged `offer_expired` for a person to decide (extend the
offer, move them on, or reject them): nothing is rejected automatically, there
is no scheduled job, and "expired" is worked out on every read against the
school's own today (`vs_config.clock.tenant_today`).

### 13.1 Storage

- `AdmissionStage` (tenant-owned like the module's other rows): `name` (up to
  40 characters, unique per school ignoring case), `position` (from 1, the
  school's order), `is_offer`, `offer_valid_days` (1 to 365, only on an offer
  stage; an offer stage may leave it empty, and its offers then have no last
  day unless a move names one).
- `Student` gains `admission_stage` (nullable, SET_NULL), `stage_entered_on`
  and `offer_expires_on`. Entering an offer stage sets `offer_expires_on` to
  today plus the stage's days, unless the move names its own date; entering
  any other stage, or none, clears it. Confirming or rejecting leaves all three
  as the record of where the application ended.
- The documents are the school-scoped `vs_config` key
  `applicants.documents.required_to_confirm` (JSON list, default `[]`,
  platform and school scopes), declared by `vs_students` migration 0008 and by
  `seed_config_catalogue`. A value stored by hand is cleaned on read: an
  unknown document is dropped.

Migration 0008 is reversible: going back removes the definition and its
values, the three columns and every stage.

### 13.2 Applicant rules: `GET, PUT /v1/students/admission-rules/`

GET needs `school.students.view` (the Applicants board renders its columns
from it); PUT needs `school.settings.update`. The school is `request.tenant`.

```json
{
  "stages": [
    {"id": 3, "name": "Entrance exam", "position": 1, "is_offer": false, "offer_valid_days": null, "applicants": 4},
    {"id": 4, "name": "Offer", "position": 2, "is_offer": true, "offer_valid_days": 14, "applicants": 2}
  ],
  "required_documents_to_confirm": ["BIRTH_CERTIFICATE"],
  "document_types": [
    {"value": "BIRTH_CERTIFICATE", "label": "Birth certificate"},
    {"value": "REPORT_CARD", "label": "Previous report card"},
    {"value": "PASSPORT_PHOTO", "label": "Passport photograph"},
    {"value": "TRANSFER_CERTIFICATE", "label": "Transfer certificate"},
    {"value": "IMMUNISATION", "label": "Immunisation record"}
  ]
}
```

`applicants` counts the current APPLICANTs at each stage that the caller can
see: narrowed to their branches, and to `?branch=` at a school with more than
one. `document_types` is the same list the enrolment rules carry.

PUT takes the whole set every time and answers with the GET body:

```json
{
  "stages": [
    {"id": 3, "name": "Entrance exam", "is_offer": false, "offer_valid_days": null},
    {"name": "Interview", "is_offer": false},
    {"id": 4, "name": "Offer", "is_offer": true, "offer_valid_days": 14}
  ],
  "required_documents_to_confirm": ["BIRTH_CERTIFICATE"],
  "reason": "optional, for the audit trail"
}
```

The list order is the position. An item with an `id` updates that stage, one
without creates a stage, and a stage left out is removed (confirmed and
rejected records that passed through it are left with no stage). Names are
trimmed. Two stages may swap names in one save. Every stage change is audited
against `AdmissionStage` (CREATE, UPDATE with a diff, DELETE), the documents as
`config.value.updated`; saving the screen unchanged writes and audits nothing.

Refusals are 400 `REQUEST_ERROR`, keyed on the field, one sentence each, and
write nothing:

```json
{"success": false,
 "message": "3 applicants are at Interview. Move them to another stage before removing it.",
 "error": {"code": "REQUEST_ERROR",
           "detail": {"stages": ["3 applicants are at Interview. Move them to another stage before removing it."]}}}
```

| Refusal | Field | Sentence |
|---|---|---|
| a name listed twice, ignoring case | `stages` | `'interview' is listed twice.` |
| an empty name | `stages` | `Stage 2 needs a name.` |
| a name over 40 characters | `stages` | `'...' is longer than 40 characters.` |
| more than 12 stages | `stages` | `A school can name up to 12 admission stages.` |
| offer days outside 1 to 365 | `stages` | `An offer at 'Offer' can stay open for 1 to 365 days.` |
| offer days on a stage that is not an offer | `stages` | `'Interview' is not an offer stage, so it has no number of days to accept.` |
| removing a stage that holds applicants | `stages` | `3 applicants are at Interview. Move them to another stage before removing it.` (`1 applicant is at ...` for one) |
| an id that is not one of this school's stages | `stages` | `Stage 17 is not one of this school's admission stages.` |
| an id listed twice | `stages` | `Stage 3 is listed twice.` |
| an unknown document type | `required_documents_to_confirm` | `'PASSPORT' is not a document this school can ask for.` |

The applicants counted for a removal are the whole school's, whatever the
caller's branches, because a removal is school-wide.

### 13.3 Moving an applicant: `POST /v1/students/<id>/stage/`

Needs `school.students.update`, the key `/confirm/` and `/reject/` use.

```json
{"stage": 4, "offer_expires_on": "2026-04-30", "reason": "Passed the interview."}
```

`stage` is required and may be `null` (no stage); `offer_expires_on` and
`reason` are optional. Entering a different stage dates `stage_entered_on`
today. Moving an applicant to the stage they are already at keeps
`stage_entered_on` and resets the offer, which is how an expired offer is
extended. A move that changes nothing writes nothing. The answer is the
student's directory row (section 13.4). The move is audited on the student as
UPDATE, with `metadata` `{"from": {"id", "name"} | null, "to": {"id", "name"} |
null, "offer_expires_on", "reason"}`.

Refusals:

- A stage that is not this school's: 404 `{"success": false, "message": "No
  such admission stage at this school.", "error": {"code": "REQUEST_ERROR",
  "detail": {"detail": "No such admission stage at this school."}}}`. A student
  the caller cannot see is the module's usual 404.
- A student who is not an applicant: 422
  `{"success": false, "message": "Chiamaka Nwosu is active, so there is no
  admission stage to move. Stages apply to applicants only.", "error":
  {"code": "NOT_AN_APPLICANT", "detail": {"status": "ACTIVE"}}}`.
- 400 on `offer_expires_on`: "The last day to accept cannot be in the past."
  and "Only an offer stage has a last day to accept." (a date named with a
  non-offer stage or with no stage).
- 400 on `stage` when it is missing: "Say which stage to move the applicant
  to, or null for none."

### 13.4 Applicant rows

Every student list row and profile (the directory, `/unplaced/`, a class
roster, a guardian's students, the stage move's answer, and the profile, live
and `?as_at`) carries:

```json
{"admission_stage": 4, "admission_stage_name": "Offer",
 "stage_entered_on": "2026-03-10", "offer_expires_on": "2026-03-24",
 "offer_expired": false}
```

`admission_stage_name` is `""` with no stage. `offer_expired` is true when
`offer_expires_on` is before the school's today and the student is still an
APPLICANT; a profile read `?as_at` judges it on that day instead. The list
reads the stage with `select_related`, so a page costs the same number of
queries however many applicants have stages.

The directory takes `?stage=<id>` (applicants at that stage) and
`?stage=none` (applicants at no stage). Either narrows to APPLICANTs, so a
confirmed child still carrying the stage they were confirmed from is not
listed. An id that is not one of this school's stages, or is not a number, is
a 400: `{"stage": ["No such admission stage at this school."]}`.

### 13.5 Confirming

Every route that confirms an applicant (`/confirm/`, and APPLICANT to ENROLLED
on `/status/` and `/bulk/status/`, which hand the move to `confirm_applicant`)
refuses while any of `required_documents_to_confirm` is not on the student's
record, before anything else is written (no admission number is issued):

```json
{"success": false,
 "message": "Tunde Bello cannot be confirmed until the birth certificate and transfer certificate are on their record.",
 "error": {"code": "DOCUMENTS_MISSING",
           "detail": {"missing": [{"value": "BIRTH_CERTIFICATE", "label": "Birth certificate"},
                                  {"value": "TRANSFER_CERTIFICATE", "label": "Transfer certificate"}]}}}
```

HTTP 422. Enrolling a child directly, without saving them as an applicant
first, is not held to this list, and neither is the student import.

# M12 Staff Management - the API behind the design

Read from `docs/designs/Staff_Management.html` (9 screens, 7 drawers, 79
collections, 122 actions) and reconciled against
`M12_Staff_Management_FRD_v2.0.docx` and the code at `main`.

The FRD says of itself, on its control page, that it "must be revised once a
design exists: no M12 design has been produced yet, only the brief that will
produce one", and its section 14 decision 1 names producing the design as the
step rather than the question. The design now exists. This document is the
answer to that decision.

Three things changed in the repository since the FRD was verified (34a08fd /
6d94ae3) and all three make the module cheaper:

- **M13 is built.** `AcademicSession`, `AcademicTerm`, `Level`, `SchoolClass`,
  `Subject` and `SubjectOffering` all exist in `schools.vs_academics`, with a
  full route set. The FRD's one hard build-order blocker is gone and the
  recommended two-phase split is no longer needed.
- **FR-014 is already fixed.** `apps/vs_rbac/tests/test_multi_branch_assignment.py`
  is 30 tests covering exactly the FRD's requirement. One person holds one role
  at two branches today.
- **The import engine has five school datasets**, not zero. Students, academic
  structure, subjects, guardians and calendar events all landed. The FRD's claim
  that `TENANT_DATASETS` is empty is stale, and staff is now the sixth instance
  of a pattern rather than the first.

What has not changed: **there is still no staff record.** No `vs_staff` app, no
`StaffProfile`, no `SchoolClass.class_teacher`, no `AuditModuleKey.STAFF`, and
`school.teachers.assign` / `school.leave.*` are still unseeded.

---

## 1. Screen to data to endpoint

| Screen | What it reads / writes | Bucket |
|---|---|---|
| **Shell nav** - branch pill, session pill, invited count, gap count, command palette | branches, sessions; counts of PENDING invitations and coverage gaps; staff search by name | Branches/sessions **served**; both counts and the staff search **absent** |
| **1. Directory** `isDirectory` | 25-row page: name, initials, job title, staff ID, role, posting, employment chip, teaching load, account flag. Header: total, "N currently employed", per-status bar and breakdown, "with teaching duties", "locked accounts", per-branch (or per-role) side panel. Filters: search, role, posting, employment status, account status. Bulk select to posting or role | Endpoint **exists and is closed to teachers** (gated `school.administrators.view`, which `teacher` does not hold; `school.teachers.view` does). Payload is **wrong shape** - eight narrow fields, no job title, staff ID, employment status, posting id, load or counts. Everything behind it is **absent** |
| **2. Profile** `isProfile` - 7 tabs | Header (initials, name, staff ID, job title, role, posting, employment + account chips, unlock, resend). Lifecycle strip. Overview: bio, contact, employment, tenure, read-only salary from Finance. Assignments: posting, derived reach with the role behind each branch, teaching duties with co-teachers, class-teacher line. Access: grants with reach and grant metadata. Quals. Docs. Leave. History (employment + account, told apart) | **Absent** |
| **3. Add staff** `isAdd` - 6 steps | Bio, employment, role + reach, teaching (subjects x classes), qualification rows, document rows. Writes account + invitation + grant + profile + quals + docs + assignments | POST **exists**, **wrong shape** - it takes the account fields only. Steps 2 and 4-6 are **absent** |
| **4. Invitation sent** `isInvitedDone` | Email, role, channel, expiry copy; resend, view profile, add another | Derived from the create response - **absent** as a payload |
| **5. Invitations** `isInvited` | PENDING accounts with role, staff ID, posting, sent date. Resend / **Mark accepted** / View / **Revoke** | List is **wrong shape** (filterable from the existing endpoint but missing staff ID and posting). Resend **served**. Mark accepted and Revoke are **absent and contested** - see 2.2 |
| **6. Posting & reach** `isPosting` | Per branch, three labelled groups (posted here / reaching here through a role / school-wide), each row with why, role, load, employment. Bulk move posting | **Absent** |
| **7. Teaching duties** `isAssignments` | Coverage grid: class x subject, lead + assistants per cell, "No lead", "Nobody". By-teacher list with owned classes. Class-teacher table. Reported timetable clashes. Assign drawer with promote/demote | **Absent**, and the clash panel is **not ours** (M14) |
| **8. Roles & access** `isRoles` | Role catalogue: role, holder count, holder faces, reach summary; filter all / in use. Revoked grants with reason and metadata. Role preview drawer: summary, permission areas, holders, withdrawn-from | **Served** by `vs_rbac` (`tenants/<slug>/roles/`, `role-assignments/`, `permission-catalogue/`) - the roles list already counts holders not grants. **Wrong shape** only in that it is mounted under `/v1/rbac/tenants/<slug>/` and keyed on the tenant slug, while every other screen here is on `/v1/i/` and takes no identifier |
| **9. Bulk import** `isImport` | Template with typed columns and required flags, XLSX **and** CSV download, upload, column mapping, validation counts, issue list, bad-row table, error report, review, confirm, progress, done with created/skipped, skipped-rows report, batch history | Engine **served** end to end - every wizard step maps to a live route. The **staff dataset is absent** (six places, per FRD 8.4) |
| **Drawers** - status, role grant, bulk role, assign, class teacher, posting, role preview | Employment transition with effect copy, cover list; grant a role with reach; grant one role to a selection; subjects x classes x lead/assistant; set class teacher; move posting with warning | **Absent** except the role grant, which is **served** by RBAC |

**Dead API**: none in this module's namespace - there is no namespace yet. Two
adjacent surfaces are dead and this module is what makes them live:
`school.administrators.update` / `.suspend` / `.reactivate` are seeded,
SENSITIVE, granted to `school_admin`, bundled into a permission group, and
reach no view in the repository; and `EmployeeSalary.employee` is a nullable
foreign key that the finance serializer never lists and nothing populates.

---

## 2. The FRD delta

**A new FRD version is warranted.** This is not a wording pass: the design adds
a column to a table the FRD specifies, a second kind of gap to a computed
endpoint, two write actions with no requirement behind them, and four promises
the platform cannot keep. Version **2.1** (minor - behaviour, contracts,
acceptance and the refusal list change; the functional baseline and the
document's structure do not).

### 2.1 The design implies it, the FRD lacks it

These become requirements. The design decided them.

1. **A teaching assignment has a part: lead or assistant.** The design's
   `TEACH_ROLE` is `{lead, assistant}`, every seeded assignment carries `tRole`,
   the drawer offers "Their part", and the row actions are "Make lead" and
   "Step back". The FRD's `TeachingAssignment` has no such column. It needs one,
   plus **at most one lead per (session, class, subject)** - the design's
   `adLeadTakenText` and `leadOf()` both assume it - while assistants stay
   unbounded. The existing unique constraint stays as it is.
2. **Coverage has two kinds of gap.** The FRD's FR-017 counts pairings with no
   assignment. The design counts those *and* `leadGaps()` - a pairing that has
   assistants but no lead - and its own comment says why: "A pair with only
   assistants is covered, but nobody owns the marks. That is a different problem
   from nobody teaching it." The headline reads "2 pairs have nobody, 1 has no
   lead". Both counts are computable and both must be returned.
3. **Directory counts the FRD does not list.** FR-002 returns total, by
   employment status and by branch. The design also shows: staff **with at least
   one teaching assignment**; **locked accounts** (an account-status count, not
   an employment one); and, at a single-branch school, a **by-role** breakdown
   where the branch breakdown would be. The last is the multi-branch rule
   working correctly - the dimension recedes and something useful takes its
   place - and it needs a second aggregate, not a hidden one.
4. **Teaching load per directory row.** `r.loadLabel`, counted from assignment
   rows. The FRD has `?teaching=true|false` as a filter but never returns the
   count.
5. **An account flag per directory row.** `r.showAcctFlag` / `r.acctFlagLabel` -
   a chip where the account state disagrees with the employment state, which is
   how Mrs. Okafor reads as employed and locked at once. FR-002 has account
   status as a filter only.
6. **The branch roster's per-row payload.** FR-010 specifies three labelled
   groups; the design's rows also carry `why` ("Posted at Ikeja Branch"), the
   role that reaches, the teaching load and the employment chip.
7. **A role catalogue view of the school.** Role, holder count, holder
   identities, reach summary, in-use filter, revoked grants with reason and
   actor, and a per-role preview of what it reaches. RBAC serves the parts; the
   FRD never says what this module's screen needs from it, so nothing guarantees
   the shape.
8. **Bulk role grant.** `bulkRole` grants one role at one reach to a selection,
   reports who already holds it, and leaves existing roles untouched. The FRD
   has bulk posting only.
9. **Create-time qualifications, documents and teaching duties.** Add steps 4, 5
   and 6 write them in the same act as the account. The FRD makes each a
   separate endpoint used after creation. The create must accept them, and the
   whole thing stays one transaction.
10. **Template download in XLSX and CSV**, with a per-column required flag and
    note, and a **skipped-rows report** distinct from the validation error
    report.
11. **Command-palette staff search** - name plus a meta line, across the school.
12. **Document types as the design names them**: CV, Degree certificate,
    Professional certificate, National ID, Other. The FRD's list is CV,
    CERTIFICATE, IDENTIFICATION, CONTRACT, REFERENCE, OTHER. The design splits
    certificate in two and drops contract and reference.
13. **A lifecycle strip** on the profile: Invited then Active as the ordinary
    path, and an explicit "off path" line for the other four statuses rather
    than a step that pretends they are stages.

### 2.2 The design asks for what the code cannot serve

Named, not dropped. Each needs a decision.

1. **Timetable clashes on the teaching-duties screen.** The panel is real UI
   with a REPORTED badge and two rows, one of them deliberately reduced because
   the other lesson is at a branch the reader cannot see. **M14 is unbuilt** -
   there is no `TimetableSlot`, no `Period`, no clash rule, and nothing to
   report. The design's own note is right about the direction ("a clash is a
   fact about two timetable slots"), but the source does not exist. The screen
   ships with an empty panel until M14, or the panel is held back.
2. **"The account stays usable until the last working day, then closes."**
   `ACCOUNT_EFFECT.resigned` says it in the drawer and the seeded history says it
   again. Nothing closes it. There is no periodic task registry, and the FRD
   refuses a self-changing status in section 3.4 and again in decision 5. Today
   the sentence is a promise the server does not keep: Mr. Ayanwale's last day is
   18 December 2025 and on 19 December he can still sign in, indefinitely, until
   somebody remembers.
3. **Leave statuses Approved, Pending and Completed.** The design's `LV` map has
   all three; the FRD specifies RECORDED and CANCELLED and explicitly refuses to
   reserve the others, because "reserving a value nothing writes is how a
   half-built feature looks finished". Nothing in the platform approves leave -
   the workflow engine has no leave document type. And "Completed" is derived
   from dates, which the FRD refuses twice. The design's own note admits the
   first half ("Approval is handled outside this module, so there are no approve
   or reject controls here") without saying who does it.
4. **"Mark accepted" on the Invitations screen.** `iv.onActivate` moves somebody
   from Invited to Active by administrator action. FR-003 and FR-008 both forbid
   it, and the code cannot honestly do it: activation is the person setting their
   first password through a single-use link, which is what promotes the account
   from PENDING to ACTIVE. An administrator flipping the employment status alone
   produces somebody who reads Active and cannot sign in.

### 2.3 The FRD requires it, the design does not show it

Reported, not built around. My read of each:

1. **Leave create, edit and cancel** (FR-013's write half, and the whole of
   `school.leave.manage`). The design's Leave tab is read-only - there is no add,
   edit or cancel control anywhere in 122 actions. **This is a real gap, not a
   backend-only rule**: leave rows appear on the profile and nothing in the
   module puts them there. Either a screen is missing or leave is genuinely
   somebody else's, and 2.2.3 is the same question from the other side.
2. **A person reading and editing their own record**, their own documents and
   their own leave (FR-004 rule 1, FR-006 rule 4, FR-007 rule 3). No screen -
   this is the self-service surface, and it is legitimately backend-only.
3. **Permission overrides on the profile** (FR-005 rule 3), shown only to a
   holder of `school.user_overrides.view`. The design's Access tab shows grants
   and revocations and no overrides. Worth confirming: an exception only its
   author can see is the thing that rule exists to prevent.
4. **`EmployeeSalary.employee` write path** (FR-015). The design consumes the
   figure and never sets the link. Backend-only, and it is the fix that makes the
   figure trustworthy.
5. **The pending-tenant surface split** (FR-018). The design carries the pre-live
   role narrowing (`showPreLive`, `rgShowPreLive`) but not the closed screens.
   Backend-only.
6. **Export Centre dataset** (decision 10) and **entitlement gating**
   (decision 11). No screen, still undecided, still not blocking.

### 2.4 Contradictions - the design stands, but these are worth a word

1. **`VOLUNTEER` employment type.** The FRD adds it deliberately as "the Nigerian
   private-school shape"; the design's picker offers Full-time, Part-time,
   Contract only. A picker omission is weaker evidence than an assertion, so I
   would keep the enum and add the option rather than drop the value.
2. **ON_LEAVE to SUSPENDED.** The FRD allows it; the design's `TRANSITIONS` map
   omits it. Suspending somebody who is on leave is a real event.
3. **The coverage universe.** The design crosses every class with every subject;
   the FRD uses `SubjectOffering` crossed with the classes at that level, which
   is narrower and correct - a prototype with four classes and three subjects
   cannot tell the difference. Build the FRD's.

---

## 3. Endpoints to open

Cheapest work in the plan, and it is one line each.

| What | Change | Who gets it |
|---|---|---|
| `GET /v1/i/me/staff/` | `rbac_permission` moves from `school.administrators.view` to `school.teachers.view` | Unblocks `teacher`, who holds the teachers key and not the administrators one, and cannot read the directory today |
| `POST /v1/i/me/staff/` | moves from `school.administrators.create` to `school.teachers.create` | `branch_admin` gains it (holds `.create` on teachers, not on administrators) |
| `POST /v1/i/me/staff/<id>/resend/` | unchanged, `school.administrators.create` | per FRD |

Three keys to seed, with prebuilt-role defaults **and a backfill phase** for
tenants provisioned before today: `school.teachers.assign` (SENSITIVE;
school_admin, branch_admin), `school.leave.view` (NORMAL; school_admin,
branch_admin - not teacher), `school.leave.manage` (SENSITIVE; school_admin,
branch_admin). Plus two descriptions: resource `(school, teachers)` becomes
"Staff records", group "Teaching Staff Records" becomes "Staff Records", and a
new branch-scopable "Staff Leave" group. `school-fe/src/permissions/index.ts`
changes in the same pull request.

`AuditModuleKey.STAFF` and five action types
(`STAFF_EMPLOYMENT_STATUS_CHANGED`, `STAFF_POSTING_CHANGED`,
`STAFF_TEACHING_ASSIGNED`, `STAFF_TEACHING_UNASSIGNED`, `STAFF_LEAVE_RECORDED`)
registered in the same change that first emits them - `emit_audit_event` never
raises, so an unregistered key is a silently empty trail.

---

## 4. Endpoints to add

New app `schools.vs_staff`, mounted at `/v1/i/me/staff/` beside `vs_schools`.
Literal segments (`posting/`, `teaching/coverage/`, `qualifications/<id>/`,
`documents/<id>/`, `leave/<id>/`, `teaching/<id>/`) declared **before**
`<int:pk>/` or the pk pattern swallows them.

Six models per FRD section 7, with the two changes from 2.1:
`TeachingAssignment.part` (LEAD / ASSISTANT, default LEAD) with a partial unique
constraint on `(tenant, session, school_class, subject)` where `part=LEAD`; and
`StaffDocument.document_type` carrying the design's five values.
`SchoolClass.class_teacher` is added to `vs_academics` as an additive nullable
FK to `StaffProfile`, which M13 v2.5.1 reserved.

| Method and path | Key | Serves |
|---|---|---|
| `GET, PATCH /v1/i/me/staff/<id>/` | view / update | Profile header, Overview, Add-edit |
| `POST /v1/i/me/staff/<id>/status/` | `school.teachers.manage` | Status drawer, with the cover list named |
| `GET /v1/i/me/staff/<id>/history/` | view | History tab, both halves |
| `POST /v1/i/me/staff/<id>/account/suspend/` | `school.administrators.suspend` | Account actions |
| `POST /v1/i/me/staff/<id>/account/reactivate/` | `school.administrators.reactivate` | Account actions |
| `POST /v1/i/me/staff/<id>/account/unlock/` | `school.administrators.reactivate` | Profile Unlock button |
| `PATCH /v1/i/me/staff/<id>/account/email/` | `school.administrators.update` | Account actions |
| `POST /v1/i/me/staff/posting/` | `school.teachers.update` | Bulk move, and the drawer |
| `GET /v1/i/me/staff/roster/` | view | Posting & reach, three labelled groups |
| `GET, POST /v1/i/me/staff/<id>/qualifications/` | view / update | Quals tab |
| `PATCH, DELETE /v1/i/me/staff/qualifications/<id>/` | update | Quals tab |
| `GET, POST /v1/i/me/staff/<id>/documents/` | view / update | Docs tab |
| `DELETE /v1/i/me/staff/documents/<id>/` | update | Docs tab |
| `GET, POST /v1/i/me/staff/<id>/leave/` | `school.leave.view` / `.manage` | Leave tab - **pending 2.2.3** |
| `PATCH, DELETE /v1/i/me/staff/leave/<id>/` | `school.leave.manage` | Leave tab - **pending 2.2.3** |
| `GET, POST /v1/i/me/staff/<id>/teaching/` | view / `school.teachers.assign` | Assign drawer |
| `PATCH /v1/i/me/staff/teaching/<id>/` | `school.teachers.assign` | Make lead / Step back |
| `DELETE /v1/i/me/staff/teaching/<id>/` | `school.teachers.assign` | Remove |
| `PUT /v1/i/me/staff/teaching/class-teacher/` | `school.teachers.assign` | Class-teacher table and drawer |
| `GET /v1/i/me/staff/teaching/coverage/` | view | Coverage grid, both gap kinds |
| `GET /v1/i/me/staff/search/` | view | Command palette |

Refusals, all with a sentence written for the person reading it:
`INVALID_STATUS_TRANSITION` (naming both), `REASON_REQUIRED`,
`LAST_WORKING_DAY_REQUIRED`, `ACCOUNT_NOT_ELIGIBLE`, `INVALID_DATE_RANGE`,
`SESSION_ARCHIVED`, `BRANCH_NOT_IN_SERVICE`, `LEAD_ALREADY_SET`, plus the
platform's existing `NOT_FOUND`, `DUPLICATE`, `TENANT_NOT_LIVE` and
`PROTECTED_REFERENCE`. Warnings, not refusals, in `data.warnings`:
`LEAVE_OVERLAP`, the posting move that leaves assignments behind, and the
person on leave today whose status is not ON_LEAVE.

Open before go-live: directory, create-and-invite, resend, record read and
edit, posting, import, search. Closed: teaching, coverage, leave, lifecycle.

---

## 5. Shapes to change

| What | Change | Consumers to follow |
|---|---|---|
| `SchoolStaffSerializer` | Add job title, staff ID, employment status, posting id and label, school-wide flag, teaching load, account flag, invitation state | `tests_staff_endpoint.py`; the onboarding checklist screen; `school-fe` staff list |
| `SchoolStaffListCreateView.school_users()` | Queryset narrows from every user the tenant owns to users that **have a `StaffProfile`** | Same. This is the narrowing the view's own docstring asks for, and `Guardian.user` now exists, so the day it matters has arrived |
| `SchoolStaffListCreateView.list()` | Add the counts block beside the page and `role_options` | Same |
| `UserCreateSerializer` path in `create()` | Accept the profile, quals, docs and teaching payloads; one transaction | `tests_staff_endpoint.py`; platform account create must be unaffected |
| `EmployeeSalarySerializer` | Accept an optional staff reference on create and update; set `employee` and `name` from it | `vs_finance` payroll tests; no existing row changes, no backfill |
| `vs_import_data` | Sixth dataset per FRD 8.4 | `tests_dataset_ownership.py::test_the_school_datasets_are_the_five_a_school_arrives_with` - **update the count, keep the guard** |

---

## 6. Not ours

- **M14 Calendar & Timetables** - the clash panel on the teaching-duties screen,
  and "View their timetable" on the profile. We need `TimetableSlot` to point at
  `TeachingAssignment` rather than repeat its teacher, which is what M14 v3.0.1
  section 12 already asks for. Until M14 ships the panel has no source.
- **M04 RBAC** - the whole Roles & access screen and the role drawers. Served,
  under `/v1/rbac/tenants/<slug>/`. Nothing to build; the frontend needs the
  slug, which the session already carries.

---

## 7. What I need decided

Four, and only the first two change what gets built.

**Leave (2.2.3 and 2.3.1).** The design shows Approved / Pending / Completed and
no way to create, edit or cancel. The FRD specifies Recorded / Cancelled with a
full write surface. These cannot both ship. Concretely: Mr. Bakare's study leave
runs to 30 November. Under the FRD a school administrator types it in, it reads
Recorded, and the profile shows it. Under the design it reads Approved, and
nobody in this module approved it, because nothing anywhere can. My
recommendation is the FRD's vocabulary with the design's screen: Recorded and
Cancelled, a write surface added to the Leave tab, and `school.leave.manage`
reaching something. If leave should genuinely be applied for and approved, that
is a workflow document type and a different piece of work.

**"Mark accepted" (2.2.4).** Mrs. Okonkwo invites Mr. Adeyemo on 2 October. He
does not click the link. On 20 October she presses Mark accepted. He now reads
Active on every screen in the school and still cannot sign in, because his
account is PENDING and only the link sets a first password. My recommendation is
to drop the action and keep Resend, which is the thing that actually helps.

**The resignation promise (2.2.2).** "The account stays usable until the last
working day, then closes" - nothing closes it. Either the sentence changes to
say an administrator closes it, or we build the scheduled job, which the FRD
refuses on the grounds that a status changing itself overwrites its own log.
Cheapest honest fix is the wording plus a directory warning once the date has
passed.

**The clash panel (2.2.1).** Ship the teaching-duties screen with an empty clash
panel and a note, or hold the panel until M14. I would ship it empty: the panel
is correct, it just has nothing to say yet.

---

## 8. Build order once approved

1. Keys, groups, descriptions, audit module key and action types, with the
   backfill phase. Move the two `rbac_permission` values on the live view.
2. `schools.vs_staff`: six models, `SchoolClass.class_teacher`, migrations.
3. Directory and create - the narrowed queryset, the widened serializer, the
   counts, the transactional create with quals, docs and teaching.
4. Profile: record, roles and reach, quals, docs, history.
5. Lifecycle and the account actions, which is where the two statuses finally
   have to agree.
6. Posting, the roster, the bulk move.
7. Teaching: assignments with the lead/assistant part, the class teacher, the
   coverage endpoint with both gap kinds.
8. Leave, per the decision above.
9. The staff import dataset, six places, and the guard test updated not removed.
10. `EmployeeSalary.employee`, and the read-only figure on the profile.
11. Seeder: a Brightfield-shaped tenant driven through the real services, one
    person per employment state, one per account state, two branch-pinned grants
    of one role, a lead gap and a coverage gap. Run it twice.

Tests per endpoint, security first: 403 without the key, 404 for another
tenant's row, a PENDING tenant genuinely reaching the open surfaces and refused
the closed ones, the empty-list shape, and `assertNumQueries` on the directory
page.

# M27 Student Portal - API plan

**Design:** `docs/designs/Student Portal - Desktop.html` and
`Student Portal - Mobile.html`. One prototype in two frames, sharing one mock
data module (`portal-data.js`, identical in both files): 10 screens (sign in,
home, timetable, results, attendance, assignments, notices, messages, fees,
profile) plus the pay flow, a receipt, a term report card and an annual report
card. Desktop: 21 collections, 47 actions, 7 field writes, 321 bindings.
Mobile adds only frame navigation (day tabs, a "More" sheet, back buttons).
**Brief:** `~/Downloads/August_Sprint/M27_Student_Portal_Design_Prompt.md`
(outside git). The design moved the brief's mock data from First Term to the
end of the session (Third Term, 8 July 2026, all three terms published), which
is why it shows a full-session results tab and an annual report card the brief
never asked for. The design stands.
**Owner decision, 15 October 2026 (chat):** a student signs in on the school's
own sign-in page, the same one staff use, with **their admission number or
their email**. There is no separate portal sign-in. This outranks the design's
"Use the email address your school invited you with" copy.
**FRD:** none. `docs/frd/functional-requirements/` has no `27-*` folder. MRD
v2.106 lists Module 27 as V1, Backend Planned, In use Not started, "No mounted
portal app", ten capabilities, and one Needs Attention line: "Do not expose
portal endpoints until student-domain authorization and ownership rules
exist." **This plan is the first draft of the M27 FRD.**
**Backend today:** no portal app, no student account, no student route.
`vs_rbac.capability_map` names `student_portal` as a capability with no
permission module.

> **Approved 15 October 2026**, with these decisions:
>
> 1. **A pupil activates like everyone else**, through the ordinary invitation
>    link. The link goes to the pupil's own email when the record has one and
>    to the primary guardian's email. A pupil with an email signs in with it or
>    with the admission number; a pupil without one has an account with no
>    email (`User.email` becomes nullable) and signs in with the admission
>    number only. Forgot password delivers the same way as the invitation.
> 2. **A pupil may start a payment**, behind a per-school setting
>    (`fees.student_payment_enabled`, default on) and a per-school minimum part
>    payment (`fees.min_part_payment`, default ₦1,000.00).
> 3. **Notices are built now; Messages are built by Module 32; Assignments
>    wait** (owner decision, 16 October 2026, revising the 15 October one to
>    build all three). No staff design prompt is issued for Notices or
>    Assignments. Notices are built from the student design, with the staff
>    side written here (section 13). Messages are built by Module 32
>    (`schools.vs_messages`) at `/v1/portal/student/messages/...`, designed in
>    `docs/messaging/messaging-design.md`, and they implement the 15 October
>    2026 owner decision: no staff-pupil thread is private, and every one is
>    readable by the pupil's primary guardian and, read-only and audited, by
>    staff holding the welfare oversight key (`school.message_welfare.inspect`).
> 4. **The promotion pass mark is a per-school staff setting**
>    (`students.promotion_pass_mark`) shown to staff on the promotion screen,
>    for information only: it decides nothing. The student portal does not
>    show it; the promotion tile shows the decision alone.
>
> The section 3.4 backend-only items are accepted as written. Until the M27
> FRD v1.0 is written (queued in `todo.md`, Documents owed), this plan is the
> contract the build is checked against.

---

## 1. The headline

1. **A student cannot sign in today, and nothing can tell whose record a
   signed-in student is.** `Student` (`schools/vs_students/models.py:85`) has
   no link to a `vs_user.User`. Nothing creates an account for a pupil. The
   sign-in box reads anything without an `@` as a staff number. This is the
   foundation every screen stands on, and it is M11 and M03 work, not portal
   work. The MRD's one Needs Attention line for this module is exactly this.
2. **Most of the reads exist, but only as staff screens.** The class timetable
   grid (M14), a student's term attendance (M15), the released report card and
   its cumulative block (M16) and the FAL's `fee_status` / `invoices_for`
   (M17) carry nearly every number the screens show. Each sits behind a staff
   key and a branch scope that would show a pupil every other pupil in their
   branch, and each carries staff-only fields (hold reasons, clash warnings,
   correction history, guardian blocks). The portal needs its own narrow
   reads over the same services, not new data.
3. **Three screens have no module at all:** Messages, Notices and
   Assignments. No model, no app, no MRD module owns direct messages,
   announcements or homework. MRD v2.106 records that conversation threads
   were removed from Module 8's scope because nothing implements them, and
   Module 29 "Tasks & Assignments" is CodeX's internal tracker, not homework.
   These are three new modules, and whether to build them now is decision 3.
   Since then (21 October 2026) Messages are built as Module 32
   (`schools.vs_messages`, MRD v2.109) behind `/v1/portal/student/messages/...`
   and Notices are built; only Assignments remain unowned and parked.
4. **Paying is the one place the design reverses a recorded decision.** The
   FAL's port design says the student portal gets the read port only and is
   "structurally incapable" of starting a payment (`schools/core/fal/ports.py:48`).
   The design and the brief make a student's fallback payment a feature.
   Decision 2.

---

## 2. Screen -> data -> endpoint

New paths sit under `/v1/portal/student/` in a new app
`apps/schools/vs_student_portal/` (school nouns throughout, so it lives under
`apps/schools/`). Every portal path names no student: the student is always
the caller, so there is no identifier to guess.

| Screen | What it reads / writes | Bucket | Endpoint |
|---|---|---|---|
| **Sign in** | Admission number or email, password, school from the subdomain; forgot password | **Wrong shape** (identifier) + **Absent** (account) | `POST /v1/user/auth/login/` (exists; learns admission numbers) · reset routes **Served** |
| **Shell** | Student name, initials or photo, class, admission number, school name, branch (recedes at a one-branch school), session and term; unread badges | **Absent** | `GET me/` · `GET /v1/notify/unread-count/` **Served** |
| **Home** | Greeting and date; today's classes with now / next / done; today's attendance mark and term rate; fee balance, status, open invoice title and due date; latest notice; unread messages; assignments due | **Absent** (composed) | `GET me/home/` |
| **Timetable** | Today's classes with teacher and room, next class; the week grid (days x periods, subject, teacher, room, times, break rows), today's column | **Wrong shape** (staff grid has it, plus drafts and warnings) | `GET me/timetable/` |
| **Results: this term** | Term average, class average, position "6th of 32", subject count, assessment and term; per subject: teacher, CA /30, exam /70, total %, grade, remark; chart by subject | **Wrong shape** (report card snapshot) + two missing facts | `GET me/results/?term=` |
| **Results: full session** | Cumulative average and position, class average, terms published "3 of 3", promotion line, pass mark; per term: average, position, class average; per subject: three term scores, cumulative, grade, change on last term; session remark, signer, next session date | **Wrong shape** (cumulative block) + three missing facts | `GET me/results/session/` |
| **Term report card** | Letterhead, student facts, components, subject rows, average and position, class teacher's remark, attendance line, next term begins, signer; Download | **Wrong shape** | `GET me/results/report-card/?term=` |
| **Annual report card** | All three terms on one sheet, cumulative, promotion decision; Download | **Wrong shape** | `GET me/results/report-card/?term=<third>` (the cumulative card) |
| **Attendance** | Term rate, present / absent / late counts, days opened; month calendars of daily marks; "days to note" with absent and late notes | **Wrong shape** (late notes missing, staff fields present) | `GET me/attendance/?term=` |
| **Fees** | Term fee, paid, balance, status; the open invoice (title, number, due date, lines, total); payment history (date, amount, payer, method, reference, status, receipt or "no receipt yet") | **Wrong shape** (FAL read lacks number and due date) + **Absent** (per-student history with pending) | `GET me/fees/` · `GET me/fees/payments/` |
| **Pay** | Full or part amount, remaining after; method; hand-off to the gateway; success, pending, failed | **Absent** (decision 2) | `POST me/fees/invoices/{ref}/pay/` · status **Served**: `GET /v1/payments/public/collections/{reference}/status/` |
| **Receipt** | Receipt number, date, reference, payer, class and admission number, invoice and part or final, amount, method, balance after this payment; Download PDF | **Wrong shape** (balance after is live, no PDF route) | `GET me/fees/payments/{ref}/receipt/` (JSON) and `?format=pdf` |
| **Profile** | Read-only: full name, admission number, class and session, date of birth, school email, primary guardian with relationship and phone. Editable: phone, personal email, photo | **Absent** | `GET/PATCH me/profile/` · `POST/DELETE me/profile/photo/` |
| **Notices** | Feed of school-wide and class notices (title, date, scope, body, unread); open one; mark read; mark all read | **Absent, no owning module** | decision 3 |
| **Messages** | Threads with a teacher or the office (name, role, last message, time, unread); a thread; send | **Built by Module 32** (`schools.vs_messages`) | `GET, POST /v1/portal/student/messages/conversations/`, `conversations/<id>/messages/`, `conversations/<id>/read/`, `unread-count/`, `contacts/`; see `docs/messaging/messaging-design.md` |
| **Assignments** | List (subject, title, teacher, due, status, due-soon); detail with teacher's attachments; submit text and or one file once; submitted and graded states (score of total, feedback) | **Absent, no owning module** | decision 3 |

**Served today once a student has an account:** the notification inbox
(`/v1/notify/`, already scoped to `recipient=request.user`, no key), password
reset and change, sign out.

---

## 3. FRD delta (this is the FRD's first draft)

### 3.1 Requirements the design implies

Identity and access

- **R1. A student is an ordinary school account linked to exactly one student
  record.** `Student.user`: a nullable one-to-one to `vs_user.User`, same
  tenant on both sides, unique. The account holds no role and no permission
  key, the way a guardian's does (M28 FR-001): what a student may reach is
  computed from the link, fresh on every request.
- **R2. Sign-in accepts the admission number.** The school's one sign-in box
  takes an email, a staff ID or an admission number. A value without `@` is
  looked up as a staff number and as an admission number inside the named
  school; exactly one match signs in, and two matches (a school whose staff
  and pupil numbering collide) sign nobody in, indistinguishably from a wrong
  password. Forgot password accepts the same identifier.
- **R3. A school gives a pupil portal access, and takes it away.** Staff with
  a new key `school.students.portal_access` issue access per pupil or for a
  whole class at once, see who has it and who has signed in, and revoke it.
  How the first password reaches a pupil is decision 1. Withdrawal,
  graduation and transfer out deactivate the account in the same transaction
  as the status change.
- **R4. After sign-in the school app can tell a student from staff.**
  `GET me/` answers 200 with the student's header for a linked account and 404
  for anyone else, the pattern `GET /v1/i/me/staff/mine/` already uses. The
  sign-in response is not changed: `vs_user` is an engine app and does not
  learn the word student.
- **R5. Every portal read is the caller's own record and nothing wider.** No
  portal path carries a student identifier. A staff account that also holds a
  student link (a teacher who is also a pupil is not a real case, but a test
  account is) reaches the portal through the link and staff screens through
  its grants, and the two never mix.

Home and timetable

- **R6. Home is composed, and a block that cannot be read says so.** Each
  block (today's classes, today's mark, fees, latest notice, unread, due work)
  is read from its owning module; one that fails renders as unavailable, never
  as zero. M28 FR-003 states the same rule for guardians.
- **R7. The timetable shows the published grid of the pupil's current
  class**, with period times, subject, teacher's display name, room name, and
  break rows, and a today block with now / next / done computed server-side
  in the school's timezone. An unpublished timetable is an empty state, never
  a draft. Clash warnings and slot ids are not returned.

Results

- **R8. A student reads released results only**: published and not held,
  latest version (the M16 contract M28 already carries). A held term shows a
  plain withheld state with a sentence telling the pupil to ask the school
  office; it carries no balance and no reason.
- **R9. The term view carries each subject's teacher.** The snapshot has no
  subject teacher today; the design shows one on every result card and session
  row. M16 freezes the main teacher's display name into the snapshot at
  publication.
- **R10. The term view carries the class's overall average.** The snapshot has
  per-subject class averages only (and only when `show_class_stats` is on).
  M16 freezes the class's overall average at publication, behind the same
  setting; when the setting is off, the figure recedes from the screen.
- **R11. Position appears only where the school ranks** (`gradebook.rank_in_class`).
  A school that does not rank shows no position tile, not a dash.
- **R12. The full-session view is the cumulative block of the latest released
  term**, plus each released term's own average, position and class average,
  plus the change on last term per subject. "Terms published 2 of 3" is read
  from the session's terms and the pupil's released results.
- **R13. Report cards download as PDF.** See section 3.2 for how.

Attendance

- **R14. The attendance view lists late days as well as absences** in "days to
  note", each with its note and arrival time, and excused absences with their
  reason. Today the late note is stored and returned nowhere.
- **R15. "Today's mark" on Home is today's cell of the current term**, or
  "not taken yet" before the register is submitted.

Fees

- **R16. The fee read carries the invoice number, invoice date and due date.**
  `InvoiceView` (FAL contract) gains `document_number`, `invoice_date`,
  `due_date`; the model has them and `_invoice_view` drops them.
- **R17. Payment history is per pupil and includes payments still being
  confirmed.** Confirmed receipts and pending gateway collections, newest
  first; a failed or abandoned attempt is not listed (the design says "your
  invoice hasn't changed").
- **R18. Each payment says who paid and how.** `CollectionIntent` records the
  paying account and its display name ("Mrs. Adaeze Nwosu", "Chiamaka Nwosu
  (student)"), and the channel Paystack reports at confirmation (card, bank
  transfer, USSD) instead of the generic `ONLINE`. A receipt booked by a
  bursar names no payer and shows its recorded method.
- **R19. A receipt's "balance after this payment" is the balance at the time
  of the payment**, frozen when the receipt is booked. Today it is computed
  when the receipt is rendered, so a February receipt reprinted in July shows
  July's balance. This is a defect on the staff receipt as well, and is fixed
  at the booking for both.
- **R20. A student downloads their own receipt as a PDF.** `receipt_pdf`
  exists and is used only as an email attachment.
- **R21. The pay flow** (if decision 2 is yes): full balance, or a part amount
  between a school-set minimum and the balance; the portal returns a checkout
  address and nothing else; success, pending and failed come from the existing
  public status read. The minimum part payment is a per-school setting
  (`fees.min_part_payment`, default ₦1,000.00 as the design shows), because a
  school may choose differently.
- **R22. The pupil who paid is told when the payment is confirmed**, in-app
  and by email ("we'll let you know in-app and by email"). Today
  `billing.payment_received` writes an in-app row with no recipient for anyone
  and emails only the billing address. The fix resolves the paying account
  from the collection, so a guardian paying online is told as well.

Profile

- **R23. A student edits their own phone, personal email and photo, and
  nothing else.** The school email shown is the sign-in email on the account
  (read-only). The record's `phone` and `email` become the pupil's own contact
  fields. Every edit is audited with the pupil as actor. Whether pupils may
  change their photo is a per-school setting (`students.portal_photo_self_edit`,
  default on), because the photo is printed on cards and some schools will
  want it fixed.

### 3.2 Things the design shows that the code cannot serve as drawn

- **"Pass mark 50%" on the promotion tile.** No pass mark exists. Promotion is
  recorded by Student Management (`ClassEnrolment.outcome`) and the gradebook
  only reads it; nothing compares an average with a threshold. Showing "68.1%
  against the 50% pass mark" would state a rule the school never applied.
  Decision 4.
- **A separate "class teacher's remark for the session".** The gradebook keeps
  one remark per term. The third-term remark is on the cumulative card, so the
  plan serves it as the session remark (the design's wording reads naturally
  for it). Not a new field. Flagged so it can be overridden.
- **A server-made report-card PDF.** The gradebook serves report cards as data
  for the browser to print, by design (M16 build 1). The plan keeps that: the
  portal renders the served card and "Download PDF" prints it to PDF in the
  browser. Receipts differ because a server PDF already exists.
- **"Payment of ₦20,000 received" with method Card / Bank transfer / USSD on a
  receipt recorded by the bursar.** A cash or bank-lodgement receipt has no
  gateway channel. Those rows show their recorded method.

### 3.3 What the MRD lists and the design does not draw

- **Document download** (MRD capability). Covered by report cards and receipts.
  Other school documents (a pupil's uploaded admission documents) are not
  shown and are left out.
- **Secure session and device management.** Sign-out is served; a "your
  devices" list is not drawn and no self-service session list exists for any
  account. Backend-only for now: the admin session list already covers pupils.
- **Age-appropriate privacy controls.** Not drawn. The plan's answer is
  structural: a pupil sees no other pupil, no guardian's email, no balance
  reason, no staff note; the photo self-edit is a school setting. Proposed as
  the requirement text rather than a screen.
- **Notification inbox.** Not drawn as its own screen; the notices screen and
  badges stand in for it. The inbox route is served and the frontend can add
  it later without backend work.

### 3.4 Backend-only requirements no screen shows (for you to wave through)

- **Every portal refusal is 404**, so a pupil cannot learn whether another
  record exists.
- **The portal is closed to a school that has not gone live**
  (`pending_tenant_surface` stays at its closed default).
- **Portal access is audited**: issue, revoke and every self-edit.
- **Sign-in by admission number is throttled exactly as sign-in by email.**
- **A pupil account is deactivated, not deleted, when they leave**, so the
  link history survives.
- **No portal path writes to money.** Pay starts a collection intent only; the
  payments module confirms and books, as M28 FR-013 requires.

---

## 4. Endpoints to open

None. Every existing read is a staff screen whose branch scope and fields are
wrong for a pupil. Opening any of them to a student role would show the pupil
their whole branch. The portal calls their services, not their views.

## 5. Endpoints to add

All portal views: `IsAuthenticatedAndActive` plus `IsLinkedStudent`
(resolves `request.user` to its active `Student` in `request.tenant`, else
404). No permission key, by the same decision M28 records for guardians. A
`docstring-name:` line on every view; `success_response` envelope.

| Path | Verb | Payload | Refusals |
|---|---|---|---|
| `/v1/portal/student/me/` | GET | - | 404 `NOT_A_STUDENT` |
| `me/home/` | GET | - | per-block `unavailable` |
| `me/timetable/` | GET | - | 200 empty `published: false` |
| `me/attendance/?term=` | GET | term defaults to current | 404 unknown term |
| `me/results/?term=` | GET | term defaults to latest released | 200 `state: withheld / not_published` |
| `me/results/session/?session=` | GET | | 200 `state: not_published` |
| `me/results/report-card/?term=` | GET | | 404 not released |
| `me/fees/` | GET | - | 503 `FINANCE_UNAVAILABLE` |
| `me/fees/payments/` | GET | paginated | 503 |
| `me/fees/payments/{ref}/receipt/` | GET | `?format=pdf` | 404 not this pupil's or not settled |
| `me/fees/invoices/{ref}/pay/` | POST | `{amount_kobo}` | 404 not theirs; 400 `AMOUNT_BELOW_MINIMUM`, `AMOUNT_ABOVE_BALANCE`, `INVOICE_NOT_PAYABLE`; 403 `STUDENT_PAYMENT_OFF` (school setting); 503 `PROVIDER_UNAVAILABLE` |
| `me/profile/` | GET, PATCH | `{phone, email}` | 400 per field |
| `me/profile/photo/` | POST, DELETE | multipart, 5 MB, png/jpg/webp | 403 `PHOTO_SELF_EDIT_OFF` |
| `/v1/students/{pk}/portal-access/` | GET, POST, DELETE | | key `school.students.portal_access` |
| `/v1/students/portal-access/` | POST (bulk) | `{class, session}` or `{students: []}` | same key |

Refusal sentences are written for the pupil, for example
`AMOUNT_BELOW_MINIMUM`: "The smallest part payment your school accepts is
₦1,000.00. Enter ₦1,000.00 or more, or pay the full balance."

**Permission key:** `school.students.portal_access` (M11 module), granted by
default to `school_admin` and `branch_admin`, with a backfill phase in
`seed_students_permissions` for schools provisioned before it existed.

## 6. Shapes to change, and their consumers

| Change | Owner | Consumers to update |
|---|---|---|
| Sign-in identifier resolves admission numbers; the staff-number lookup moves behind a registry the school apps register into, so `vs_user` stops importing `schools.vs_staff` (an engine-boundary breach today at `vs_user/services/sign_in_scope.py:182`) | M03 `vs_user` + M11 + M12 | `LoginRequestSerializer`, password-reset request serializer, sign-in tests |
| `Student.user` one-to-one | M11 | student serializers (add `has_portal_access`, `portal_last_sign_in`), roll exports, status transitions |
| Snapshot gains subject teacher and class overall average | M16 | report card serializer, staff Results tab (both gain the fields), amendment re-issue |
| `InvoiceView` gains number and dates | FAL | gradebook holds read (ignores the new fields) |
| `CollectionIntent` records payer account and confirmed channel | M18 `vs_payments` | collection serializers, pay-link checkout, M28 bridge |
| Receipt balance-after frozen at booking | M17/M19 `vs_finance` | staff receipt HTML and PDF, receipt email |
| `billing.payment_received` addresses the paying account | M08 / `vs_finance` | receipt email path |

## 7. Not ours to invent

Messages are built by Module 32 (`schools.vs_messages`, design in
`docs/messaging/messaging-design.md`), which owns their endpoints, so this plan
does not invent them. Notices are built (section 13). Assignments still have no
owning module and stay parked: if built, they become their own module with its
own FRD and MRD entry, designed from the staff side as well (a teacher has to
set homework and mark it, and none of those staff screens are designed). The
portal plan does not invent their endpoints.

## 8. Dead API

Nothing in the portal namespace exists. Two built-but-unrouted pieces gain
their first caller: `FinanceReadPort.fee_status` / `invoices_for` (the read)
and, if decision 2 is yes, a student twin of `ParentPaymentBridgePort`.

## 9. Field Access

The portal is the case `FieldAccessMixin.owner_rule` exists for: the person a
record is about reads their own fields regardless of role switches, because a
pupil holds no role. Every portal serializer is a fixed, narrow shape that
never carries a staff-only field, so no staff switch can leak through it.
Registered in `vs_student_portal/field_access.py` as owner-visible, with the
writable ones marked: `phone`, `email`, `photo` (write); everything else read
only and server-owned. The new staff-side field `has_portal_access` (M11) gets
its own read switch under the student record.

## 10. Decisions

1. How does a pupil get their first password? (section 3.1 R3)
2. May a pupil start a payment at all? (reverses the FAL port decision)
3. Messages, Notices, Assignments: build now as new modules, or park?
   (Answered: Notices built; Messages built by Module 32 on 21 October 2026;
   Assignments parked.)
4. The pass mark on the promotion tile.

## 11. Build order (after approval)

1. **Foundation (M11, M03):** `Student.user`, portal-access issue and revoke,
   admission-number sign-in with the registry fix, `GET me/`. Nothing else is
   reachable until this ships.
2. **Reads:** profile and self-edit, timetable, attendance, results and cards,
   fees and payment history, receipts with the frozen balance and the PDF.
3. **Money (if decision 2):** student pay bridge, payer and channel capture,
   the minimum-part-payment setting, payer notification.
4. **New modules (if decision 3):** each from its own plan. Messages: built
   by Module 32, `docs/messaging/messaging-design.md`. Notices: built
   (section 13). Assignments: parked.
5. **Seeder:** `seed_student_portal_scenarios`, driven through the real
   services: Chiamaka with every term released, a classmate with a held third
   term, a pupil with no portal access, one with an unpublished timetable, one
   part-paid with one pending collection, at a one-branch and a two-branch
   school.

---

## 13. As built (foundation and notices)

This section records what shipped and overrides the sections above where they
differ.

### 13.1 Pupil accounts and sign-in (M11, M03)

- `Student.user` is a nullable one-to-one to the account (PROTECT, so the
  sign-in history cannot be deleted from under the record). An account holds no
  role: `TenantUserRoleAssignment` refuses a role for an account without an
  email.
- `User.email` is nullable. Only an account created through
  `User.objects.create_user_without_email` may lack one, and the model refuses
  a missing address for platform staff and Django staff or superusers.
- The sign-in box resolves a non-email identifier through a registry in
  `vs_user.services.sign_in_identifiers`. `schools.vs_staff` registers the
  staff-number resolver and `schools.vs_students` the admission-number one,
  each from `AppConfig.ready()`, so `vs_user` no longer imports a school app.
  Exactly one account across every resolver signs in; a staff number and an
  admission number that collide sign nobody in, refused like a wrong password.
- An invitation or password-reset link for a pupil's account goes to the
  pupil's own email (where they have one) and to their primary guardian's,
  through the same registry's delivery-address providers.
- The key is `school.portal_access.issue` (not `school.students.portal_access`:
  the platform's verbs have no `portal_access`). Defaults: school admin and
  branch admin. Banded under the Student Portal module at Core.
- Routes: `GET/POST/DELETE /v1/students/{id}/portal-access/`,
  `POST /v1/students/{id}/portal-access/reissue/`,
  `POST /v1/students/portal-access/` (named pupils or a class's roll). Reading
  needs only `school.students.view`.
- Leaving the roll (withdrawn, transferred, graduated, rejected) closes the
  account in the same transaction as the status change. A suspension leaves
  it alone. A pupil given access again gets the same account back.
- Field Access: `school.students.portal_access` (read-only) governs the access
  block and the profile's `portal_access` state; the sign-in identifiers inside
  the block follow the student-number and email switches.
- A pupil may read their own passport photograph and no other document held
  against them (`media_policies`).

### 13.2 The portal app

`schools.vs_student_portal`, mounted at `/v1/portal/student/`, owns no table.
`IsLinkedStudent` admits the account linked to an on-roll pupil at the
request's school and answers 404 `NOT_A_STUDENT` to anybody else. `GET me/`
answers as section 3.1 R4 specifies; `branch` is omitted and `show_branch` is
false at a one-branch school.

### 13.3 Notices (new module, `schools.vs_notices`)

- A notice has a title (150), a message (5,000) and one or more audience rows:
  the whole school, a branch, a level or a class of the active year. Drafted,
  published once, corrected (a published notice may change its words, which
  stamps `edited_at`, but not its audience) and withdrawn.
- Keys: `school.notices.view` (school admin, branch admin, teacher),
  `school.notices.post` (school admin, branch admin: any audience inside their
  branch reach; the whole school and levels need a whole-school reach),
  `school.class_notices.post` (teacher and school admin: the classes the holder
  teaches or leads this year, and only notices they wrote).
- Staff routes under `/v1/notices/`: list (`?status=`, `?search=`), create
  (with `"publish": true` to publish at once), detail with read counts, PATCH,
  `publish/`, `withdraw/`, `audiences/` (what the caller may address).
- Portal routes: `GET notices/` (feed with `unread_count`),
  `GET notices/{id}/`, `POST notices/{id}/read/`, `POST notices/read-all/`.
  The pupil's notice carries no poster, status or read count.
- Publishing notifies every audience pupil with a working account, in-app and
  by email (`notices.published`), from a task queued after the commit. A
  notice withdrawn before the task runs tells nobody. There is no "email too"
  switch: every event on the platform goes to both channels.
- Read counts are taken against every on-roll pupil in the audience, account
  or not.
- Field Access: `school.notices.title`, `.body`, `.audience` (read and write,
  open on create; `audience` also governs `audience_label`), `.status`,
  `.publication`, `.reads` (read-only). A correction is judged by the Write
  switches; the pupil's feed is allowlisted, since a pupil holds no role.

### 13.4 Seeder

`seed_student_portal_scenarios` (after `seed_student_scenarios`,
`seed_school_permissions` and the gradebook seeders): at lagoon-view (two
branches) and sunrise-academy (one branch), one pupil active (signs in by
admission number with `Pupil@School2026`), one invited, one revoked, and four
notices (school-wide published, class published and read, withdrawn, draft).

Then, all active, the pupils the screens are checked against:

- **full:** the first child of the family whose results show the most (a
  whole released year with its year-end block where there is one), with an
  email of their own, billed `FS-TERM1` for the term, part-paid by a bursar's
  receipt, and with a ₦5,000.00 card payment started in the portal (fake
  provider) and still being confirmed;
- **sibling:** the same primary guardian's next child, admission number only,
  owing nothing: the shared-phone pair;
- **held:** a pupil whose result was held when their class's results were
  published.

A state a school's own data cannot reach is skipped with a sentence:
sunrise-academy has no fee structure and no held result. The command ends by
printing each pupil's sign-in and what their portal shows, read through the
portal's own reads. Idempotent.

### 13.5 The promotion pass mark (decision 4)

`students.promotion.pass_mark` (school scope, a whole percentage 1 to 100,
empty by default) sits with the school's promotion rules: read on
`GET /v1/students/promotion-rules/` as `pass_mark` and in the promotion
preview's `rules`, written on the same PUT (optional; absent leaves it, null
clears it) under `school.settings.update` by a whole-school caller. It decides
no promotion. No portal route carries it, and the pupil's promotion tile will
show the decision alone.

### 13.6 The pupil's reads: profile, timetable, attendance, results

All under `/v1/portal/student/`, behind `IsLinkedStudent`, each reading the
owning module's service for the admitted pupil (never an id from the request).

- `profile/` GET: `managed_by_school` (name, admission number, class, year,
  date of birth, school email = the sign-in address, primary guardian's name,
  relationship and phone) and `editable` (`phone`, `email`), plus `photo_url`
  and `photo_editable`. PATCH `phone` and/or `email`: a phone of 7 to 15 digits
  in any country's writing (no national format is assumed), a valid email;
  refusals are `PROFILE_FIELD_INVALID` keyed by `field`. Changing the record's
  email leaves the sign-in address alone. Each change is audited with the pupil
  as actor. Nothing else on the record can be written.
- `profile/photo/` POST (multipart `file`) and DELETE, behind the school
  setting `students.portal.photo_self_edit` (default on), else 403
  `PHOTO_SELF_EDIT_OFF`. Staff read and set it on
  `GET/PUT /v1/students/portal-rules/` (`school.settings.update`, whole-school
  reach).
- `timetable/`: the class's grid only when published (`published: false`
  otherwise, including while a published grid is being edited); lessons carry
  subject, teacher's name and room; `today` marks each period done, now, next
  or later in the branch's time zone.
- `attendance/?term=`: counts and rate, a cell per school day, `notes` for
  absent, excused and late days (reason, the register's note, arrival time),
  and `today`. No guardian, notice state or correction history. The note staff
  type on a register is shown to the pupil, as the design shows it.
- `results/?term=`: the released term (latest by default) with each subject's
  teacher, component marks, total, grade and remark; overall average; position
  only where the school ranks; class average only where it shows class
  figures. `state` is `released`, `withheld` (a sentence, no figures, no
  reason) or `not_published`.
- `results/session/?session=`: each term's average, position and class
  average, and, from the year's last released card, every subject across the
  year, the annual average and position, the promotion decision (read live)
  and the session remark (the last term's class teacher remark).
- `results/report-card/?term=`: the gradebook's own card for a released term;
  404 for a held or unpublished one. The last term's card is the annual card.

The gradebook snapshot now records each subject's main teacher (inside
`subject`, so an amendment keeps it) and, where the school shows class figures,
the class's overall average (`class_average`). Cards published before this
carry neither, and the portal omits them rather than inventing them.

### 13.7 Fees, payments, receipts and the pay flow

Under `/v1/portal/student/`, through the finance abstraction layer only
(`FinanceReadPort.fee_status` and a new `StudentPaymentPort`, contract 1.1.7).

- `fees/`: balance, billed, paid, and each invoice with its number, title,
  dates, lines, total, paid, balance and status (`Owing`, `Part-paid`,
  `Paid`), all in kobo; `can_pay` and `min_part_payment` are the school's
  rules. 503 `FINANCE_UNAVAILABLE` when the books cannot be read.
- `fees/payments/`: confirmed receipts and online payments still being
  confirmed, newest first, each with how it was paid (card, bank transfer,
  USSD as the provider reported it; a school-recorded payment shows its own
  method) and who started it (the account; empty for a payment the school
  recorded).
- `fees/invoices/{ref}/pay/` POST `{amount}` (kobo): the whole balance, or a
  part of at least `fees.min_part_payment` (default ₦1,000.00), while
  `fees.student_payment_enabled` (default on) allows it. Refusals:
  `STUDENT_PAYMENT_OFF` 403, `AMOUNT_BELOW_MINIMUM` / `AMOUNT_ABOVE_BALANCE` /
  `PAYMENT_AMOUNT_INVALID` 400, `INVOICE_NOT_PAYABLE` 409, 404 for an invoice
  not on the pupil's account, `PROVIDER_UNAVAILABLE` 503. Answers
  `{checkout_url, reference, amount, balance_after}`; the status is read on the
  public collection status route by `reference`. The collection is started
  with the `PORTAL` origin and the pupil's account as its actor.
- `fees/payments/{payment_ref}/receipt/`: the receipt as JSON, or `?as=pdf`
  (not `format`, which DRF reserves) for the PDF the receipt email attaches.
- Staff set the two rules on `GET/PATCH /v1/school-finance/settings/student-payments/`
  (`school.fees.view` / `school.fees.update`, whole-school reach).

Engine changes the flow needed, which also fix staff receipts:

- **A receipt's "balance after" is frozen when it posts**
  (`PaymentAllocation.invoice_balance_after`, `Payment.customer_balance_after`);
  every receipt surface (HTML, PDF, email, portal) reads the frozen figure and
  falls back to today's only for receipts posted before it was recorded. A
  February receipt reprinted in July now says what was owed in February.
- **A gateway payment records how it was paid** (`CollectionIntent.paid_via`,
  from Paystack's `channel` at confirmation).
- **A `PORTAL` collection tells the account that started it** when it is
  confirmed (`billing.payment_received`, in-app and by email, once the booking
  commits). A collection staff started tells them nothing.

### 13.8 Home (composed, R6)

`GET /v1/portal/student/home/` (not `me/home/`: the portal routes sit directly
under `/v1/portal/student/`). One read with the school date and five blocks,
each read through the same function its own portal route uses, inside a
savepoint of its own:

- `today`: `published` and today's periods with `done` / `now` / `next` /
  `later`, from the timetable read (13.6).
- `attendance`: the term, today's cell (`state` is `NOT_TAKEN` until the
  register is in, R15) with `mark_label`, and the term's `rate`.
- `fees`: `balance` (kobo), `status` (`OWING`, `CLEAR` or `IN_CREDIT`), the
  open invoice falling due first and how many are open, and `can_pay`.
- `notices`: the newest notice addressed to the pupil, with `unread`, and the
  unread count.
- `notifications`: the pupil's unread in-app notification count, from the same
  helper as the bell (`vs_notifications.services.inbox.unread_in_app_count`).

Every block carries `available`. A block whose read raises is logged and
answered `{"available": false, "message": ...}` with a sentence to show, never
a zero, and the others still arrive. Assignments have no block because they
are not built. Messages are built by Module 32, and the home does not yet carry
a messages block: the pupil's unread count is
`GET /v1/portal/student/messages/unread-count/`.

### 13.9 Student app sessions (installable app, owner decisions of 16 October)

The student app is its own project (`student-fe`) served at `/student/` on the
school's address, installable on a phone. A pupil stays signed in for the
school's `students.portal.session_days` (1 to 90, default 30) between uses, and
one phone holds several pupils side by side.

The account engine has **device sessions** (`vs_user.services.device_sessions`),
offered only to accounts a product's registered policy answers for; the
students app registers one for any account linked to a pupil, with
`app_path` `/student/`. Staff keep the one-day browser session.

- `POST /v1/user/auth/login/` with `session: "device"`: answers `account` (a
  random key) and sets the credential in its own HttpOnly cookie
  `device_session_<account>` (Secure, SameSite Strict, path `/v1/user/auth/`,
  lives the policy's days). 403 `DEVICE_SESSION_NOT_OFFERED` for an account
  with no app, once its password is proven, before anything is issued. A
  browser sign-in answers `device_app: {path}` (null for staff).
- `POST /v1/user/auth/device/refresh/` `{account}`: rotates that account's
  credential and starts its days again; 401 `TOKEN_EXPIRED` / `TOKEN_REVOKED`
  / `TOKEN_INVALID` clears its cookie. The credential rotated away from last is
  still answered for `DEVICE_SESSION_REFRESH_GRACE_SECONDS` (120) with the
  credential the session is on, so a lost answer or two windows refreshing at
  once both end holding the same working credential. An older credential, or
  that one after the window, ends the session (`CREDENTIAL_REUSED`) and answers
  401 `TOKEN_REVOKED`. 409 `TOKEN_STALE` is no longer answered.
- `POST /v1/user/auth/device/sign-out/` `{account}`: ends that account on that
  device only; always 200.
- `POST /v1/user/auth/device/hand-over/`: turns a browser session (after a
  sign-in on the ordinary page answered `device_app`) into a device session;
  answers `{account, access, app_path}` and clears the browser cookie.
- Taking away portal access, leaving the roll, suspension of the account and
  force logout end device sessions as they end any other. The outstanding-token
  row carries the credential's real expiry, so the blacklist is not flushed
  under a credential with weeks to run.
- Staff set the days on `GET/PUT /v1/students/portal-rules/` (`session_days`).
- The activation and password-reset previews answer `sign_in_as` (the email,
  else the admission number) and `device_app`, so a pupil with no email is told
  what to sign in with and sent on to the student app.

### 13.9.1 Promotion opens at the year's end (owner decision of 16 October)

A promotion run moves each pupil out of their class at once, so it is refused
with 409 `PROMOTION_TOO_EARLY` (`detail.opens_on`) until the year being left
has ended its last term; the preview is open at any time and answers
`run_opens_on` and `can_run`. In the holiday after a run the portal shows the
pupil's next-year class: `me/` answers `student.class_is_next_year` and names
that year in `session`.

### 13.10 Still to build from this plan

`results/session/` also answers `terms_total` (the year's terms) and each
released term's `released_at`; in the cumulative block each subject's
`annual_grade` (its annual average on the class's scale in force, null where
the programme has none) and the class's `class_average` for the year (the
mean of its released terms' class averages, only where the school shows
class figures).

A card checkout started in the portal returns the pupil to
`<school host>/student/fees/paid?reference=...` (`STUDENT_APP_BASE_URL` names
another host only for a separate dev server); the page polls the public
collection status by that reference.

Bank transfer (FAL contract 1.1.8, `StudentPaymentPort.transfer_account`,
`open_transfer_account`, `transfer_deposits`): `GET fees/transfer-account/`
answers `ISSUED` (bank, number, name, `as_of`), `NONE` or `UNAVAILABLE` with a
sentence; `POST` issues the pupil's permanent virtual account for their own AR
account (one, under a lock on the customer; 10 an hour), or returns it.
`GET fees/transfer-account/deposits/?since=<as_of>` lists transfers since then,
`PENDING` as soon as the provider reports one and `CONFIRMED` with its receipt
once booked. Both need the school to let pupils pay. No minimum applies.

The account is issued without anybody asking when the pupil's first bill
posts (owner decision of 17 October: one permanent account per pupil, issued
at the first bill rather than at enrolment, the `POST` kept for anyone
missed). `vs_finance` sends `invoice_posted` once a posting commits; the FAL
(`schools.core.fal.transfer_accounts`) queues `fal.issue_transfer_account`
for a pupil's AR account with no active account, and the task issues through
`open_transfer_account`, so the bill and the `POST` share one lock and one
account. It does nothing where pupils may not pay, where the provider is off
or issues no dedicated accounts, or where the branch is not set up to take
payments directly. The account is opened with the AR account's billing
email, else the email of the pupil's primary guardian (owner decision of 18
October), which goes to the provider and is copied nowhere. Because the
provider keeps one customer per email across the whole platform, an email
that already opened another pupil's active account (a sibling, or a child at
another school) is not used again: that pupil's account is opened without it,
as the `POST` opens one, so every child has a number from the first bill.
Where a detail the provider needs is still missing (Paystack: a name and an
email), it issues nothing and records a `VIRTUAL_ACCOUNT_NOT_ISSUED` payment
event naming it, once per account. A provider that does not answer is retried five times, waiting
1 to 16 minutes, and then recorded the same way; nothing is stored until the
provider answers, so the screen shows `NONE`, never a half-made account.
Where tasks run inline (`CELERY_TASK_ALWAYS_EAGER`), there is one attempt.

A transfer into any customer's virtual account names no bill. Under Finance
settings > Banking and cash, `apply_transfers_automatically` (default on,
owner decision of 16 October) settles the customer's open bills as it is
booked, in the `default_receipt_allocation_strategy` order, its own branch's
bills only; any excess waits as credit. Off, every transfer waits as credit
for somebody to allocate, as before.

The home screen shows the school's own name and crest. The school's host
passes `/student/manifest.webmanifest` through to
`GET /v1/portal/student/app/<slug>/manifest.webmanifest` (public, cached an
hour): the school's name, a short name that fits under an icon, and icons cut
from its crest at `.../app/<slug>/icons/<name>` (192, 512, maskable 512 and
the 180 Apple icon, the crest on white), addressed relative to the manifest
(`icons/icon-192.png`) so they resolve through the school's own address
(`/student/app/<slug>/...`, passed through to the API) and straight from the
API alike. A school with no crest keeps the
app's own XVS icons at `/student/icons/`.

Push (owner decisions of 16 October: pywebpush, no figures on a locked
screen). `push` is a third notification channel beside in-app and email, with
its own per-event switch (school, then branch) and its own templates that say
what happened and never a figure. A device subscribes with
`POST /v1/notify/push/subscriptions/` (`{endpoint, keys, account?}`, the key
from `GET /v1/notify/push/key/`); one made with a device-session `account` is
deleted when that session signs out, and each push to it starts with the
recipient's first name. A recipient with no device gets no push record. Push
templates: a new notice ("New notice: <title>"), a confirmed payment
("Payment confirmed", no amount) and the new `gradebook.result_released`, which
tells a pupil with an account that their term's result is ready (in-app and
push). Keys: `WEB_PUSH_VAPID_PUBLIC_KEY` / `_PRIVATE_KEY` / `WEB_PUSH_CONTACT`
per environment; without them push is off.

Messages are built by Module 32 (`docs/messaging/messaging-design.md`); Assignments wait for their turn. For the installable app: applying transfers to open bills
(per-school switch, default on), and push.

### 13.10 Messages (Module 32, 21 October 2026)

Messages are built by Module 32, `schools.vs_messages`, and mounted in this
portal by one `include` line at `/v1/portal/student/messages/`. The contract and
the owner decisions are in `docs/messaging/messaging-design.md`; this note
records only what touches the pupil.

- The pupil starts a conversation with one of their own teachers (the class
  teacher, the teachers of their class this year while
  `messages.family_reach_subject_teachers` is on) or the school office, and
  reads and writes in their own conversations. Contacts and rows carry a
  label ("Class teacher, JSS1 B", the subjects taught, "School office").
- The 15 October decision is implemented: no staff-pupil conversation is
  private. Every one is readable, read-only, by the pupil's primary guardian
  (who is not alerted to it), and by staff holding
  `school.message_welfare.inspect` within reach of the child's branch, each
  opening of which is audited. The list and the contacts page carry
  `oversight_notice`: "Your parent or guardian and the school can read your
  messages."
- `messages.pupils_enabled` (default on) switches pupil messaging: off, existing
  conversations stay readable and nobody starts or writes in one.
- A new message alerts the pupil through Module 8 (`messages.new_message_pupil`,
  in-app, email and push; no message text); a push opens
  `/student/messages/<id>`, and reading the conversation clears its alert.
- The pupil's payload is a fixed narrow shape: no Field Access switch of a staff
  role reaches it.

# Parent portal (M28): API plan

The data behind the parent portal design, which endpoint serves each screen,
and what is built for it. This document is the contract the build is checked
against; section 9 records what shipped and overrides the rest where they
differ. The M28 FRD revision it implies is queued in `todo.md` (Documents
owed), not written here.

Inputs:

- Design: `docs/designs/Parent Portal - Desktop.html` (10 screens, 376
  bindings, 20 collections, 51 actions, 8 field writes).
- FRD: `docs/frd/functional-requirements/28-parent-portal/` v1.2 (14 October
  2026), the release family. The August sprint file
  `M28_Parent_Guardian_Portal_FRD_v2.0.docx` in the sprint folder is an older,
  separate family and is not the base.
- Code: the pupil portal `schools.vs_student_portal` (shipped), the FAL
  (`schools.core.fal`: `GuardianLinkPort`, `ParentPaymentBridgePort`,
  `StudentPaymentPort`, `FinanceReadPort.invoices_for` / `combined_balance`),
  M11 `Guardian` / `StudentGuardian`, `schools.vs_notices`.

## Owner decisions (19 October 2026), binding

1. A parent holds no permission key and no role. They are admitted because a
   `Guardian` row in the asserted tenant has `user = request.user`; anybody
   else gets 404. This is FRD v1.2 FR-001 and the FAL decision of 4 July 2026.
2. A parent edits their own email, phone, alternate phone (new
   `Guardian.alt_phone`) and address through a portal route that writes M11's
   Guardian row through M11's own service.
3. Giving a guardian an account (issue, resend, revoke) is Student Management
   (M11), mirroring pupil portal access, under `school.portal_access.issue`.
   A teacher who is also a parent keeps one account.
4. Assignments are deferred; they have no owning module. Messages are built
   by Module 32 (`schools.vs_messages`, 21 October 2026) at
   `/v1/portal/parent/messages/...`, designed in
   `docs/messaging/messaging-design.md`.
5. "Pay all" across children is one checkout per child, one after another.
   The payments engine charges one child's account per checkout; a single
   split checkout would be a Payments-module build of its own.
6. Parents are told when a notice reaching one of their children is
   published (in-app and email), as a per-school setting, default on.

## 1. Screen, data, endpoint

| Screen | Data | Endpoint | Bucket |
|---|---|---|---|
| Login | email, password; school from the subdomain | platform sign-in (tenant-aware) | Served |
| Shell and child switcher | parent name, children (name, class, branch where several), unread counts | `GET me/` | Absent, built |
| Home | per-child cards (fee status, attendance rate, average, top results), combined outstanding, latest notice, unread counts, follow-ups | `GET home/` | Absent, built (follow-ups wait, decision 4; the messages count is served by Module 32's `messages/unread-count/` and is not yet a home block) |
| Timetable | one child's week and today | `GET children/<id>/timetable/` | Wrong scope: reuses the pupil service |
| Attendance | one child's term counts, grid, notes | `GET children/<id>/attendance/` | Wrong scope: reuses |
| Results | term result, year, report card | `GET children/<id>/results/`, `results/session/`, `results/report-card/` | Wrong scope: reuses |
| Assignments | rows, status, graded feedback | none | Absent, not ours (decision 4) |
| Fees | combined and per-child totals, invoices with lines, history with receipts, pay single / pay all, card / transfer / USSD, outcome states | `GET fees/`, `GET fees/payments/`, receipt, `POST fees/invoices/<ref>/pay/`, per-child transfer account | Wrong scope plus one aggregator |
| Notices | feed across the children, unread, read / read all, which child each is for | `GET notices/` and three siblings | Wrong scope: `guardian_notices` beside `pupil_notices` |
| Messages | threads with teachers, compose | `GET, POST messages/conversations/` and siblings, `messages/contacts/`, `messages/unread-count/` | Built by Module 32, `schools.vs_messages` (decision 4) |
| Profile | linked children; editable email, phone, alternate phone, address | `GET, PATCH profile/` | Absent, built (decision 2) |

All routes live under `/v1/portal/parent/`, with a trailing slash and
`?tenant=<slug>`. None declares `pending_tenant_surface`. A child id outside
the caller's own children answers the same 404 as one that does not exist.

## 2. FRD delta against v1.2

### Design shows it, FRD lacks it (the FRD moves)

- **Notices feed** (a school notice addressed to a child's school, branch,
  level or class appears in the parent's feed, with read marks). FR-011 speaks
  of notifications only.
- **Parent edits their own contact details**, including the new alternate
  phone, through M11's service. FRD section 6 says the portal persists
  nothing, which stays true: the write lands on M11's row.
- **How a guardian gets an account** (issue, resend, revoke) is silent in the
  FRD; it is M11's, decision 3.
- **Pay all** is one checkout per child in sequence (decision 5); FR-009's
  "may be paid together" reads as one balance shown together, not one charge.
- **A payment's outcome** is followed by reference (success, pending,
  failed), and the account that started it is told when it is confirmed, as
  for a pupil. The parent bridge returns the reference, not only the
  checkout address.
- **Three ways to pay** on one screen: card through the gateway checkout,
  bank transfer into the child's own permanent account (one per pupil,
  owner decision of 17 October 2026), USSD through the gateway checkout.
- **Home blocks** each answer available or unavailable on their own, never a
  zero, as FR-003 already requires.
- **Parents are told of published notices**, setting default on (decision 6).

### Design asks for what the code cannot serve

- One checkout across two children: decided as one checkout per child.
- Messages: served by Module 32 (`/v1/portal/parent/messages/`), not by this
  module. Assignments: no owning module; deferred.

### FRD requires it, design does not show it (kept, unchanged)

- FR-010 Consent and documents: Specified, no screen.
- FR-011 Notification preferences: no screen; the inbox counter is the
  platform's own.
- FR-012 Revocation: works through M11 removing the link; no portal screen.
- FR-013 The portal never books money: unchanged.

### Statuses that move

FR-005 and FR-006 are no longer blocked: attendance (M15) and the gradebook
(M16) exist. FR-002, FR-003, FR-004, FR-005, FR-006, FR-007 move to
Implemented as section 9 records.

## 3. Not ours

- Messages module (parent and teacher threads): Module 32,
  `schools.vs_messages` (21 October 2026); the parent routes are
  `/v1/portal/parent/messages/...`. See `docs/messaging/messaging-design.md`.
- Assignments module: no owner; would sit with the gradebook.
- A single checkout split across children: Payments (M18) and the FAL.

## 4. Build order

1. M11: `Guardian.alt_phone` (Field Access registered), guardian portal
   access service and routes on `/v1/guardians/<pk>/portal-access/`.
2. Portal app: admittance, `me/`, child profile, timetable, attendance,
   results, notices.
3. Profile read and edit; fees (combined view, history, receipts, pay one
   invoice, per-child transfer accounts); home; the notice setting and the
   parent notification.
4. Seed: a one-branch school and a three-branch school with a parent of two
   children at two branches, and a teacher who is also a parent.

## 9. As built

This section is the contract where it differs from the sections above.

### 9.1 Commits

| Commit | What landed |
|---|---|
| `40a7acae` | Portal app, admittance, `me/`, child record, timetable, attendance, results, notices |
| `ad24f7aa` | M11: guardian portal accounts, `Guardian.alt_phone` |
| `0f4a35b7` | Parents told of published notices (`notices.tell_parents`) |
| `d3526087` | Fees, history, receipts, invoices, pay, per-child transfer account, home; FAL contract 1.2.0 |
| `b6574e3c` | Profile read and edit |

### 9.2 Routes under `/v1/portal/parent/`

| Verb | Path | Notes |
|---|---|---|
| GET | `me/` | parent, school, session, term, `show_branch`, children; 404 `NOT_A_PARENT` for anybody else |
| GET, PATCH | `profile/` | PATCH takes `phone`, `alt_phone`, `address` only; any other key 400 by name; `email_editable: false` |
| GET | `home/` | per-child blocks (attendance, fees, results, today) and family blocks (fees, notices, notifications), each `available` on its own |
| GET | `children/<id>/` | record; other guardians by name and relationship only |
| GET | `children/<id>/timetable/`, `attendance/?term=`, `results/?term=`, `results/session/?session=`, `results/report-card/?term=` | the pupil portal's own reads |
| GET, POST | `children/<id>/fees/transfer-account/` | the child's permanent account; not gated on the pupils-may-pay rule; POST throttled |
| GET | `children/<id>/fees/transfer-account/deposits/?since=` | |
| GET | `fees/` | per child (`available` each), `combined` (withheld while any child is unreadable), `min_part_payment`, `pay_all` {total, steps, complete} |
| GET | `fees/payments/` | every child's history merged, newest first; 503 if any child is unreadable |
| GET | `fees/payments/<ref>/receipt/?as=pdf` | found on the parent's children; 404 otherwise |
| GET | `fees/invoices/<ref>/?as=pdf` | invoice JSON or PDF; 404 otherwise |
| POST | `fees/invoices/<ref>/pay/` | `{amount}` kobo; 201 `{checkout_url, reference, amount, balance_after, child}`; refusals as the pupil route |
| GET | `notices/`, `notices/<id>/`; POST `notices/<id>/read/`, `notices/read-all/` | union across the children, each notice naming the children it is for |

Card checkouts return to `/parent/fees/paid?reference=` on the school's host
(`PARENT_APP_BASE_URL` overrides the host). Status is read on the public
collection status route by `reference`.

### 9.3 Student Management (M11)

- `GET, POST, DELETE /v1/guardians/<id>/portal-access/`, `POST
  .../reissue/`, under `school.portal_access.issue` (description widened to
  pupils and guardians). The account is school-level (no branch, no role). An
  existing account at the school's address is linked, never duplicated; a
  pupil's own sign-in is never linked (409 `PORTAL_ACCOUNT_NOT_USABLE`).
  Revoke closes a parent-only account and keeps the link (`revoked`), and only
  unlinks an account used for anything else (`none`).
- `Guardian.alt_phone`; Field Access switches `school.guardians.alt_phone`
  (read, write) and `school.guardians.portal_access` (read-only).

### 9.4 Differences from sections 1 to 4

- Parent payments go through `ParentPaymentBridgePort`, which now takes the
  actor (PORTAL origin, so the parent is told on confirmation) and returns a
  `Checkout` with our reference; `entity_ref` is optional.
  `StudentPaymentPort.invoice_pdf` is new. FAL contract 1.2.0.
- The school's smallest part payment (`fees.min_part_payment`) applies to
  parents; the pupils-may-pay switch does not. The shared amount rule refuses
  a fractional or boolean amount for pupils too.
- The parent's email is read-only (owner decision, 19 October 2026).
- Home has no assignments, follow-ups or messages block, and no home route
  answers for them. Messages exist (Module 32) and serve their own unread
  count, but the home does not yet carry the block; assignments and follow-ups
  are not built.
- Parent reads are not audited, as pupil reads are not; profile edits are
  audited through `update_guardian`, payments by the payments module.
- The parent notification's in-app destination is `/notices/{id}` inside the
  parent app; the parent app's router must match it.

### 9.5 Messages (Module 32, 21 October 2026)

Messages are built by Module 32, `schools.vs_messages`, and mounted in this
portal by one `include` line at `/v1/portal/parent/messages/`. The contract and
the owner decisions are in `docs/messaging/messaging-design.md`; this note
records only what touches a parent.

- A parent lists their family conversations and, read-only, the pupil
  conversations of children they are primary guardian of (the 15 October 2026
  decision that no staff-pupil thread is private, implemented). A guardian who
  is not primary cannot open a pupil conversation. A watched conversation
  answers 409 `CONVERSATION_READ_ONLY` to a write, and opening it changes
  nothing for anyone and alerts nobody.
- A parent starts a conversation about one linked child with a member of staff
  from `GET contacts/?child=<id>`: the class teacher, the teachers of the class
  while `messages.family_reach_subject_teachers` is on, and the school office
  for the child's branch. A child that is not theirs answers this portal's own
  404. `messages.families_enabled` (default on) switches parent messaging.
- A new message alerts the parent through Module 8
  (`messages.new_message_parent`, in-app, email and push; no message text); a
  push opens `/parent/messages/<id>`, and reading the conversation clears its
  alert.
- Access is re-derived on every request: unlinking the guardian, unsetting
  `Guardian.user` or taking the child off the roll ends it at once.
- The parent's payload is a fixed narrow shape: no Field Access switch of a
  staff role reaches it, so a teacher who is also a parent keeps her own child's
  conversation.

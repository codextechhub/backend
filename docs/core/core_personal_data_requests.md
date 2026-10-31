# core_personal_data_requests

How a school answers a person's request for a copy of their data, or for it to
be erased. The privacy policy promises pupils, parents and staff both "where
the law allows" (Nigeria Data Protection Act 2023); the school is the
controller and answers, and XVS gives it the two tools below. Owner's
decisions of 31 Oct 2026.

## The copy

| Route | Key |
|---|---|
| `GET /v1/students/<id>/personal-data/` | `school.personal_data.export` |
| `GET /v1/guardians/<id>/personal-data/` | `school.personal_data.export` |
| `GET /v1/i/me/staff/<id>/personal-data/` | `school.personal_data.export` |

One JSON file (`personal-data-<kind>-<id>.json`), `format: xvs.personal-data.v1`,
grouped into sections, the person's own record first:

- **Pupil:** record (medical fields included), parents and guardians (names and
  relationship only), classes, status changes, branch moves, documents on file,
  daily and lesson attendance, term results, scores, score corrections, results
  held back, elective subjects, messages, fees account (invoices and payments),
  sign-in account.
- **Guardian:** record, children, messages, sign-in account.
- **Staff:** record, employment changes, positions, qualifications, documents,
  leave, teaching assignments, terms as class teacher, departments and classes
  led, attendance, pay (salary records, payroll lines, pay bank account),
  sign-in account.

Everything is included whatever the reader's Field Access: the person is
entitled to all of it, and the key is the gate. Files are listed by name and
date, not embedded. A sign-in account shows its details, devices and sign-in
attempts, never the password hash, session keys or recovery codes. Each
download is an audit entry (`PERSONAL_DATA_EXPORTED`).

**Nothing is left out by forgetting.** Each app registers its own sections
(`apps/<app>/personal_data.py`, `core.personal_data.register`). A test walks
every model with a link to a pupil, a guardian or a staff record and fails on
one that no section covers and no `not_exported` entry explains
(`schools/vs_students/tests/test_personal_data_requests.py`).

## Erasing on request

| Route | Key | Who can be erased |
|---|---|---|
| `POST /v1/students/<id>/erase/` | `school.personal_data.erase` | a pupil who has left (withdrawn, graduated, transferred or rejected) |
| `POST /v1/guardians/<id>/erase/` | `school.personal_data.erase` | a guardian with no child still at the school |
| `POST /v1/i/me/staff/<id>/erase/` | `school.personal_data.erase` | someone off the roll (resigned or terminated) |

Body: `{"reason": "..."}`, required: whose request it is and when it came in.
It runs exactly the erasure the nightly job runs 6 years after someone leaves
(`core_record_retention.md`): names, results, attendance, books and payslips
stay; contact details, health notes, documents and the sign-in go. The audit
entry (`PERSONAL_DATA_ERASED`) keeps the name as it was and the reason.

Erasing a pupil who was a parent's last child at the school makes that parent
due for the nightly erasure, which runs the same night; the response names
them.

## Keys

Both keys are in `seed_school_permissions` and given to the school
administrator role by default; any other role only if the school grants it.
`export` is SENSITIVE and `erase` CRITICAL, so neither travels through a
permission group. School-fe: `P.EXPORT_PERSONAL_DATA` (102518) and
`P.ERASE_PERSONAL_DATA` (102531).

## In school-fe

The pupil, guardian and staff profiles carry "Download their data" and "Erase
their details" (`src/components/people/personal-data-actions.tsx`), each shown
to a holder of its key. Erase opens a sheet saying what goes and what stays and
asks for the reason before it runs.

## Not here yet

Whole-school export and deletion when a school leaves XVS (the data
processing agreement, clause 9): its own checklist item.

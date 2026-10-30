# core_record_retention

How long XVS keeps personal records, and the nightly job that deletes them once
their period is over, or erases the person 6 years after they leave. The periods are the ones the privacy policy states in its
Annex A ("What XVS holds"); `apps/core/pruning.py`'s `SCHEDULE` is that table in
code, and a period that changes in the policy changes there, once.

Not to be confused with `apps/core/retention.py`, which does the opposite: it
refuses to delete the records the law makes a school keep (the books). The two
meet at one point: a row held by `core.retention` refuses this job's delete too,
and the job reports that period as stopped rather than forcing it.

---

## 1. What is deleted, and when

| Key | Period | What goes | What stays whatever its age |
|---|---|---|---|
| `sign_in` | 1 year | Sign-in attempts (email typed, IP, browser), sessions not seen for a year, two-step challenges, password-reset and account-closure links (all by expiry), web-push browsers and phones not seen for a year | Nothing: a session lives at most 90 days and every link or code expires within a day |
| `refresh_tokens` | at expiry | Expired refresh tokens and their blacklist rows (what `flushexpiredtokens` does) | Unexpired tokens |
| `notifications` | 1 year | In-app notifications and emails, sent or failed | An email still `PENDING` (a stuck send is a fault to see) |
| `payment_webhooks` | 1 year | Provider webhooks `PROCESSED` or `IGNORED` | A webhook still `RECEIVED` (recovery retries it, the unbooked-money alerts read it) |
| `messages` | 3 years after the last message | The conversation, its messages and participants | A broadcast while any reply to it is still within its period |
| `support_tickets` | 2 years after closing | The ticket, its comments, attachments (stored bytes emptied by the file signals), subscriptions and change log | A ticket `RESOLVED` but never closed |

Each kind is pruned by a function in the app that owns it
(`vs_user/pruning.py`, `vs_notifications/pruning.py`, `vs_payments/pruning.py`,
`schools/vs_messages/pruning.py`, `vs_tickets/pruning.py`), because only that
app knows what "still in use" means for its rows.

## 2. People, 6 years after they leave

Erasure, not deletion: the person's row stays so the records the school must
keep still point at someone, and everything else about them is blanked
(`apps/core/erasure.py`, with each product's own `erasure.py`). The owner
decided on 30 Oct 2026 that pupils and staff keep their names, because results
and payslips carry them; a parent's name is replaced.

| Key | Who | Left on | Kept | Blanked or deleted |
|---|---|---|---|---|
| `former_pupils_and_parents` | Pupils WITHDRAWN, GRADUATED, TRANSFERRED or REJECTED | The status change's `effective_date` (else when the record last changed) | Name, admission number, status, branch, the birth year (the date becomes 1 January), results, attendance, enrolments, the name on the fees account | Gender, nationality, state of origin, address, phone, email, photo, previous school, health notes, emergency contact; documents deleted with their files; the reason and new school on status changes; conversations about them; the fees account's billing contact |
| (same) | Parents | When their last child is erased (6 years after unlinked, if they have no child) | Links to their children | Name becomes "Former parent"; phone, email, occupation, address, photo; family conversations |
| `former_staff` | School staff RESIGNED or TERMINATED | `exit_date` (else the off-roll change, else when the record last changed) | Name, staff number, job, dates of service, teaching, class and attendance records, leave type and dates | Date of birth, photo; qualifications and documents deleted with their files; reasons and notes on employment changes; leave notes; the pay bank account (payments made keep their own copy) |
| `former_codex_staff` | CodeX staff EXITED | `date_exited` | Name, staff number, job | Personal details, next of kin, bank details |

Each person's sign-in is erased with them unless another record still stands
behind it (a former teacher who is a parent there keeps hers):
`vs_user.erasure.erase_account` closes it as "Delete my account" does, then
clears the phone, sign-in identifiers and gender and replaces the email with
`erased-<id>@erased.invalid` (some accounts must have one; `.invalid` never
delivers). Products name the records that hold an account with
`register_account_holder`.

The edit history is cleaned the same way: `scrub_history` blanks the same
fields in every stored copy and keeps the copies, so a class list rebuilt for
an earlier date still lists every pupil; a deleted document or qualification
loses its copies. `erased_at` on `Student`, `Guardian`, `StaffProfile` and
`PlatformStaffProfile` marks who is done, so a second night finds nobody.
Each person is erased in their own transaction; one that fails is logged,
counted as failed and tried again the next night, and never holds back the
people after them.

## 3. What is not deleted yet

- **The audit trail** (`vs_audit.AuditEvent`), which also holds every sign-in
  event (`vs_user.services.audit.log_auth_event`). The database refuses every
  delete there by trigger (`vs_audit/migrations/0019`), and
  `core/tests_append_only.py` holds the triggers in place. Keeping it for a set
  period needs a trigger change of its own.
- **Record edit history** (`vs_history.RecordVersion`). The "as at a date" views
  read a record's oldest version as the day its history began
  (`vs_history/as_at.py`), so pruning needs a new baseline written first.
- **Revoked stored files.** A deleted ticket attachment's bytes are emptied, but
  the `core.StoredFile` row stays.

## 4. How it runs

- Beat entry `core-prune-past-retention`, nightly at 03:15
  (`apps/celery.py`), task `core.prune_past_retention`
  (`core/tasks.py`), tracked like every task as a `BackgroundJob` whose result
  is the report.
- `core.pruning.run()` takes each period in turn. One that fails is logged and
  reported as `stopped`; the others still run.
- Rows go 1,000 at a time, each batch in its own transaction, so a first run on
  a large table never holds one long lock, and a run stopped halfway carries on
  the next night.
- Deletes go through the ORM, so every signal fires (file revocation, the
  `core.retention` hold).

By hand, on any environment:

```
python manage.py prune_past_retention --dry-run   # count, delete nothing
python manage.py prune_past_retention
```

Run the dry run before the first deploy that carries the job, to see the size
of the first night's work.

## 5. Tests

`apps/core/tests_pruning.py`: for every kind, a row past its period goes and one
inside it stays, and each "stays whatever its age" rule above has its own case;
plus the dry run, a second run finding nothing, batching, one period failing
without stopping the others, the beat entry and the command.
`apps/core/tests_erasure.py`: each kind of person erased past 6 years and kept
before it, what is kept (names, results, the name on the books, leave dates)
and what goes (in the live row and in every history copy), a parent kept while
a child is still at school, a former teacher who is still a parent keeping her
sign-in, and one failing erasure not holding back the next.

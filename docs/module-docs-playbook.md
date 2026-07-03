# Module-docs playbook — how this initiative runs

The repeatable method behind `docs/finance/` (16 slice reports, July 2026). Any new
session continuing this work — **next: `vs_payments`, then `vs_procurement`** —
follows this file. It is the source of truth for process; per-slice content truth is
always the code.

## Mission & status

Document every backend module as **per-subject slice reports** so future programmers
can trace endpoints → calculations → output shapes without reading the code cold.

- ✅ `vs_finance` — complete: 16 slices in `docs/finance/`, every gotcha fixed or
  explicitly justified in each doc's §8.
- ⏭ `vs_payments` → `docs/payments/`: `payment_collections` (collections + virtual
  accounts), `payment_settlement` (payouts, batches, settlement reconciliation),
  `payment_webhooks_providers` (webhook handling + OPay/Paystack adapters), plus the
  movements feed if it doesn't fit cleanly in settlement.
- ⏭ `vs_procurement` → `docs/procurement/`: `procurement_master_data` (categories,
  vendors, catalog, contracts), `procurement_sourcing` (requisitions, RFQs,
  quotations/awards), `procurement_p2p_chain` (PO → GRN → vendor invoice → vendor
  payment), `procurement_inventory` (stock items/movements), `procurement_reports`
  (AP aging, GR/IR, spend, vendor performance).

## The loop (per slice)

1. **Trace the real code** — models, service functions, views (rbac keys + request
   bodies actually read), serializers (exposed fields, FLS), URLs, enums, seeds.
   Never write from memory of "how it should work".
2. **Write the doc** from `docs/finance/_report_template.md` (11 sections). The three
   sections that catch recurring errors: §3 *only the fields the view actually
   reads*, §5 *formula → the function that computes it*, §6 *what survives posting*.
   Cite `file:line` on every calc/posting/field claim. §8 lists gotchas honestly.
3. **Commit the doc** (docs-only commit, message style `docs(payments): …`).
4. **Gotcha briefing** — explain every §8 item to the user in *simple, non-technical
   terms*, sorted into: recommend-fix / judgment call / justified-by-design, each
   with a one-line verdict. Obvious wrong-money/crash bugs: fix immediately without
   asking. Everything else: wait for the user's picks.
5. **Fixes** — see Working mode below. After fixes land: flip the doc's §8 items to
   ✅ with the how, update `todo.md` (Done entry), commit.

## Working mode (conductor)

- **Fable (the main session) never writes feature code.** It orchestrates, briefs,
  and QAs. Docs, analysis, todo/memory edits, and git commits are Fable's domain.
- **Code changes go to an Opus-high subagent** via the Agent tool with a meticulous
  brief: exact files, behaviors, guards, migration expectations, named tests, test
  command, "DO NOT COMMIT", and a required report format. Use ONE sequential agent
  whenever fixes share `constants.py` or the migrations directory (parallel agents
  collide on migration numbering).
- **QA on return** (non-negotiable): `git status --short` must match the brief;
  review risky hunks line-by-line; run the full suite YOURSELF (don't trust the
  agent's line); check `makemigrations --check --dry-run` and re-run seeds. Defects
  go back to the same agent via SendMessage with a precise correction. Only then
  sync docs and commit.
- Bulk/token-heavy chores (computer use, mass analysis) may go to cheaper models.

## Conventions that bit us (learn once)

- **Stage files explicitly — never `git add -A`** (it once swept the user's
  unrelated in-progress work into a commit).
- Commit **directly to `main`**, do **not push** (user pushes). Trailer:
  `Co-Authored-By:` line per the harness rules.
- Tests: from `apps/`, `python manage.py test <targets>
  --settings=apps.settings.local --noinput` (Postgres). **Never run two test
  processes concurrently** (shared test DB → phantom failures). Suite baseline at
  handoff: **282 green** (`vs_finance core`); payments/procurement have their own
  tests — establish their baseline first.
- Money is integer kobo everywhere. Pagination is the `XVSPagination`
  `{pagination, data}` envelope (page 25, `?page_size=` ≤ 100). Response-shape
  changes are **frontend-visible** — always flag them in the report/commit.
- RBAC: every view has an `rbac_permission`; keys live in per-app
  `seed_*_permissions.py`; canonical verbs in `core/…/seed_actions.py`. New
  documents get their **own resource** (see the pettycash/salary splits). FLS
  (`FieldSecurityMixin.read_permissions`) masks sensitive fields — payments already
  uses `payments.payout.view_sensitive` (beneficiary masking in the movements feed).
- The finance posting engine is the reference for money-touching QA: balanced-or-
  rejected, closed-period guards, corrections by reversal (never edit posted
  history), audit row in the same commit, durable rejection rows.
- `todo.md` at repo root: Undone/Done ledger — add fix batches to Done with detail.

## Session-start checklist for the next session

1. Read this file + `docs/finance/_report_template.md`; skim one finished example
   (`docs/finance/finance_banking_reconciliation.md` is the richest).
2. `git log --oneline -10` and `git status` — note any user commits since; if the
   user changed vs_payments/vs_procurement, study those commits first (the user
   sometimes asks "study my commit and sync docs").
3. Establish the module's test baseline (`python manage.py test vs_payments
   --settings=apps.settings.local --noinput`).
4. Start with `payment_collections`, one slice at a time, per the loop above.

# AGENTS.md — backend

## Pre-ship review (`ship-check`)

When I say **`ship-check`** (or "run the ship-check") on a change, answer these
four questions about the code you just wrote — honestly and specifically, not as
a rubber stamp. Point at real files/lines, name concrete risks, and if the answer
to 1 or 2 is "no", say so and propose the fix. Don't claim "secure/efficient"
without naming *what* makes it so.

1. **Did you build this in the most secure way?**
   - `rbac_permission` (or equivalent authz) on every new view, and the right
     verb (view vs create/update/generate). Entity/tenant scoping via the
     standard resolver — can a caller read/write another tenant's rows by
     changing a pk or `?entity=`?
   - What does the serializer expose? Flag raw `JSONField`/metadata, PII,
     secrets, internal ids. Apply FLS where the field is sensitive.
   - Input validation, mass-assignment, and injection surface.

2. **Did you build this in the most efficient way?**
   - Query cost: N+1 (`select_related`/`prefetch_related`), missing indexes for
     the filter/order columns, unbounded querysets, pagination where lists grow.
   - Transactions/locking correct and no wider than needed; no redundant writes.
   - Is there a simpler implementation that does the same job?

3. **What regressions could this introduce?**
   - Migrations (reversible? data-safe?), changed response shapes, permission
     keys that must be seeded/assigned, signals/side-effects, shared services.
   - List the blast radius explicitly; "none" needs justifying.

4. **What tests do we need before we ship it?**
   - Security-critical first: permission-denied (403) and cross-tenant isolation.
   - Then happy path + every filter/branch + the empty-list response shape
     (`success_response` coerces `[]` → `{}`).
   - Name the tests; if you added some, say which cases are still uncovered.

Finish with a one-line **verdict**: ship / fix-first, and the single most
important thing to do before shipping.

## Module documentation initiative

When asked to continue the module docs (or anything touching `docs/finance/`,
`docs/payments/`, `docs/procurement/`): **read `docs/module-docs-playbook.md`
first and follow it exactly.** It defines the slice-report loop (trace →
template → commit → gotcha briefing → user picks → fixes), the conductor
working mode (main session orchestrates + QAs; Opus-high subagents write all
feature code; agents never commit), and the conventions (stage files
explicitly — never `git add -A`; commit to main, don't push; one sequential
agent when fixes share constants.py/migrations; run the test suite yourself
after agent work). Template: `docs/finance/_report_template.md`. Status and
next slices live at the top of the playbook.

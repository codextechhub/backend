# AGENTS.md - backend

## What this codebase is - and what XVS is

**This repo is a multi-domain platform, not a schools application.** The engine
apps - `vs_finance`, `vs_procurement`, `vs_payments`, `vs_rbac`, `vs_workflow`,
`vs_notifications`, `vs_audit`, `core` - are deliberately **domain-neutral**.
They know about entities, customers, invoices, vendors, roles and approvals;
they know nothing about schools. `vs_health` (VIGIL) is already a second domain
standing on the same foundation, and there will be more.

**XVS is the first product built on that platform** - the schools product.
Corona Secondary School is simply its first tenant.

Two rules follow, and they are separate.

**1. Keep the engines domain-neutral.** School concepts - students, guardians,
classes, terms, sessions - live in the school apps (`apps/schools/`) and reach
the engines through the FAL, which lives under `apps/schools/` and not in
`core/`, because it is school-specific by design. If you find yourself adding a
`student` or `term` field to a generic app, stop: that leak is exactly what the
FAL exists to prevent.

**The engines must not import `vs_schools`**, or anything else under
`apps/schools/`. The site primitive is `vs_tenants.Branch`, owned directly by
`Tenant`: reach it as `row.branch` or `tenant.branches`, never as
`branch.school.tenant`. `Branch` has no `school` column. If an engine needs a
school-only fact, it belongs behind the FAL, not behind an import.

**2. Build for every school, not the first one.** Nothing may be special-cased
to one tenant's arrangement. If a feature only works because of how the first
school happens to be set up, it is not finished. The section below on branches
is the most common instance of this.

## Holistic problem solving

When the user asks for a problem to be fixed, trace it to its root cause and
fix the shared source of the failure where practical. Review adjacent flows,
callers, and equivalent modules for the same failure mode; do not stop at a
one-off patch that only hides the reported symptom. Keep the work within the
requested scope, preserve established behaviour, and add regression coverage
at the lowest shared boundary so future instances are prevented.

## Verification follows the current change

Verification is triggered by work performed in the **current request**, not by
pre-existing changes in the worktree or work completed in an earlier request.

- Git-only and read-only requests - for example inspect, explain, diagnose,
  stage, commit, branch, push, or report status - do not authorize rerunning
  tests or other verification unless the user explicitly asks for it.
- If the current request changes no code, do not run tests merely for
  reassurance. Use only the read-only checks needed to complete the request.
- When the current request changes code, run checks proportionate to that
  change. Do not expand a narrow task into a broad test-suite run without a
  concrete risk or an explicit user request.
- Documentation-only changes do not require application tests; validate only
  the documentation or formatting affected, when such validation exists.

## The word "school" belongs to the schools folder

School concepts - students, guardians, classes, terms, sessions - live in
`apps/schools/`. Outside that folder, say **tenant**.

This covers naming, not just models: parameter names, serializer fields,
constants, local variables and JSON body keys. `LoginService.login(..., tenant=...)`,
never `school=...`, because `vs_user` is an engine app and the engines must stay
domain-neutral.

Prose may still mention a school where it explains where a value comes from -
"the tenant slug the frontend takes from the school's subdomain" is good
documentation. The rule bans school vocabulary in identifiers, not in
explanations.

## Every school has at least one branch

XVS is multi-tenant. Corona Secondary School is simply the first tenant, and
nothing may be special-cased to one tenant's arrangement.

**A school is created with a main branch and can never have none**, so every user,
document and record can always be given one. Do not write code that handles a
branchless school; that shape does not exist.

**A school with exactly one branch still needs the dimension to recede.** One
branch is the common case, and a switcher with a single entry, a column repeating
the same value on every row, or a filter with one option are all noise. Where a
school has one branch the control is absent, not disabled. Where it has several,
branch appears wherever it changes meaning.

**A null branch means "shared across the school", never "no branches exist".**
That is a deliberate, first-class value - see academic structure and procurement
documents - and it keeps that meaning however many branches a school has.

Test more than one shape of school. A single-branch test proves nothing about a
multi-branch one.

## Testing strategy

A test run exists to answer a question about the change in front of you. Run
the smallest set of tests that answers it, and say which set that was.

### Never run the whole suite by reflex

Do not run the entire repository suite automatically after a change. Escalate
one step at a time, and stop at the first step that covers the change:

1. **The tests for the code you changed.** Before choosing, look at the files
   you changed and at what imports them
   (`grep -rn "from vs_finance.posting import" apps/`). Run the specific test
   class or module by its dotted path:
   `manage.py test vs_finance.tests_ledger_lock.HandJournalLockTests`. Seconds.
2. **The affected app**, once those pass: `manage.py test vs_finance`.
3. **Each dependent app**, when the change can reach it: a shared service, a
   serializer another app embeds, a signal, a constant or choice used
   elsewhere, a seed command other apps' tests call. One app per command.
4. **The full suite**, only at a checkpoint: before finishing a substantial
   feature, before merging a significant branch, before a release or deploy,
   after a change to a shared foundation (below), or when the user asks. CI
   runs it on every push to `main` and every pull request.

**Shared foundations** justify step 3, and sometimes step 4, even for a small
edit: `core/` (authentication, permission base classes, middleware, storage,
jobs, the test runner), `vs_rbac` (the evaluator, permission classes, grants,
field access), `vs_tenants` (tenant and branch resolution and isolation),
`vs_user` (login, tokens, sessions), `apps/settings/`, a migration or model
change several apps read, a seed command used across apps, and the shared test
helpers (`core/test_utils.py`, `vs_rbac/tests/helpers.py`,
`schools/core/fal/testing.py`, the `tests/base.py` modules). Even there, run
the targeted tests first and escalate only once they pass.

Documentation-only changes need no test run. Read-only and git-only requests
never trigger one (see **Verification follows the current change**).

### Say what you ran

Every summary names the scope that was run and quotes its `Ran N tests` line:

> Validation: `vs_rbac.tests.test_grant_reach` (18 tests) and the `vs_rbac`
> app (942 tests) passed. The full suite was not run: the change is confined
> to how grants are serialized.

Never write "all tests pass" unless the full suite ran in this session and
its `Ran N tests` line is quoted.

### Fixing a bug

1. Write a test that reproduces the bug, or find the one that should have
   caught it, and watch it fail.
2. Fix the code. A test that exposes a real defect is answered by fixing the
   code, never by changing the expected value to match it.
3. Run that test, then its class or module, then the app, and escalate only
   as above.

### Writing tests

- **Tests in proportion to risk.** Security first: the permission-denied
  (403) case, cross-tenant isolation, and more than one shape of school (one
  branch and several). Then the happy path and each branch of the logic. Do
  not add a test that repeats coverage an existing test already gives.
- **The cheapest test that gives the same confidence.** A rule in a service,
  validator or calculation is tested by calling it. An API test is for what
  only the API layer does: authentication, permissions, routing, status codes,
  the response contract. A test that touches no database is a
  `SimpleTestCase`, which fails loudly if it ever does.
- **Build shared fixtures in `setUpTestData`, never in `setUp`.** Django
  rolls every test back to the class fixture and deep-copies class attributes
  per test, so tests stay isolated while the fixture is built once. `setUp` is
  only for what must be fresh per test: API clients, `mock.patch`,
  `override_settings`, clearing thread-local request context, values read off
  a moving clock. Seed commands (`seed_actions`, `seed_*_permissions`,
  `seed_config_catalogue`) cost up to a second each, because every row they
  write emits an audit event: run them once per class, never per test.
- **The smallest object graph that proves the point.** A test about an
  invoice needs an entity, a customer and a period, not a provisioned school
  with staff and a workflow.
- **Authenticate without a login round-trip.** `core.test_utils.TenantAPIClient`
  mints a real JWT and asserts the tenant, so requests take the production
  authentication path. Only authentication tests go through the login
  endpoint.
- **No network, no sleeping, no large files.** The runner refuses any
  connection to a host other than this machine, so fake or patch every
  external boundary (payment providers go through
  `vs_payments.providers.http.request_json`). Move time by patching the clock,
  never with `time.sleep`. Import tests use the smallest file that shows the
  behaviour.
- **`TransactionTestCase` only for real commits**: `on_commit`, row locks,
  concurrent writers. It flushes every table after each test. Set
  `serialized_rollback = True` (enforced by
  `core/test_transaction_test_cases.py`) and tag the class `slow`.
- **Never delete a meaningful regression test to save time.** Make it cheaper
  instead. A test may be removed only when the behaviour it protects no longer
  exists, or another test already asserts exactly the same thing; say which in
  the commit.

### Running tests on this machine

```bash
cd apps && DB_NAME=cx_myslice ../cx/bin/python manage.py test vs_finance --settings=apps.settings.local --noinput
```

- **The test database comes from a cached template.** The runner
  (`core/suite_runner.py`) migrates once per distinct set of migrations, which
  takes about four minutes, saves the result as a PostgreSQL template, and
  clones it in about two seconds on every later run. The template is keyed on
  the migration files and the project modules they import, so a changed
  migration always gets a fresh one, and every run starts from a pristine copy.
  `--keepdb` is therefore unnecessary: do not use it, since it reuses the last
  run's database rather than a clean one. `--fresh-db` migrates from scratch,
  for a change whose point is that migrations replay (CI's migrate step does
  this on every push).
- **One test process at a time per session, and a unique `DB_NAME`.**
  Sessions otherwise share `test_cx_db`. The runner refuses to replace a test
  database another run has open (`TestDatabaseInUse`), which is the signal to
  pick another name, not to retry.
- **`--parallel 4` only for a full-suite checkpoint, and only on a quiet
  machine.** The suite is safe to run in parallel (it passes in about 8
  minutes that way, against about 30 in one process), but this box has run out
  of memory with several suites and a parallel run going at once (exit 144,
  roughly 16 MB free). Check `pgrep -fl "manage.py test"` first, and never use
  it for a single app. CI runs the suite this way on every push. A failing
  test in a worker is reported by name thanks to `tblib`; if a parallel run
  ever stops with "cannot pickle 'traceback'", the venv is missing it
  (`pip install -r requirements.txt`).
- **In a worktree, use the absolute path to the venv.** `./cx` is gitignored,
  so it does not exist there, and a relative path produces **empty output with
  a zero exit code**, which reads exactly like a passing run with no summary.
  Copy `apps/.env` into the worktree as well.
- **An exit code alone proves nothing.** Quote the `Ran N tests` line. If it
  is missing, the run did not finish and must be repeated.
- **Iterate with `--exclude-tag=slow`, verify without it** when the change
  touches migrations, the branch code allocator or transaction behaviour. The
  `slow` classes rewind the real migration graph or race real transactions:
  the branch migrations in `schools.vs_schools`, the user-type and
  action-token migrations in `vs_user`, and the `TransactionTestCase`
  concurrency tests. Tag new slow classes the same way; the tag is inherited.
- **A migration test subclasses `core.migration_testing.RewoundSchemaTestCase`.**
  It rewinds its app once per class, inside the transaction the class already
  holds, and PostgreSQL's rollback puts the schema back. The rewind is cached
  like the main schema: the first class to rewind an app to a given migration
  pays for it (a rewind of `vs_user` takes over five minutes) and saves the
  result as a template, and every later class and run with the same migrations
  clones it in about a second. Never rewind in `setUp` and replay in
  `tearDown` under `TransactionTestCase`: that costs a rewind and a full
  replay per test, and it had taken `vs_user` to 29 minutes.
- **See where the time goes** with `--slowest 20`, which lists the slowest
  tests and classes after the run (a class's "setup" is its `setUpTestData`;
  not available under `--parallel`),
  or `--timing-report path.json` for every measurement.
- **Output is buffered**: a passing test's prints are discarded and a failing
  test's appear in its report. `--no-buffer` prints straight through; `--pdb`
  implies it.
- **The full suite** is the same command with no label (about 30 minutes in
  one process). Run it only at the checkpoints above.

## Pre-ship review (`ship-check`)

When I say **`ship-check`** (or "run the ship-check") on a change, answer these
four questions about the code you just wrote - honestly and specifically, not as
a rubber stamp. Point at real files/lines, name concrete risks, and if the answer
to 1 or 2 is "no", say so and propose the fix. Don't claim "secure/efficient"
without naming *what* makes it so.

1. **Did you build this in the most secure way?**
   - `rbac_permission` (or equivalent authz) on every new view, and the right
     verb (view vs create/update/generate). Entity/tenant scoping via the
     standard resolver - can a caller read/write another tenant's rows by
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

## Wrapping up: report in plain words

When you finish a task - a build, an investigation, a document, a round of
decisions - close with a plain-language breakdown rather than a wall of prose.
Short numbered lines, one point each, ordinary words. Assume I am reading it tired.

Use **only** the sections that actually apply, and **skip the ones that don't** -
an empty heading is worse than no heading, and never pad a section to fill it out.

- **What you now have** - the finished things, one line each. Only if something was
  produced.
- **What you decided** - decisions taken and locked, one line each. Only if
  decisions were actually made.
- **What was fixed** - resolved defects, written in the past tense. Include only
  when knowing the original cause is useful and it has not already been explained.
- **What still needs attention** - only defects, risks, or incomplete work that
  remain after the task. Omit this section entirely when nothing remains.
- **Where to go next** - the order of the next steps, and which of them are
  unblocked right now.

That list is closed. Do not invent a heading for something that does not fit one
of them: put it under the heading it belongs to, and if it belongs under none of
them, leave it out of the breakdown entirely. A section the user did not ask for
is one they must decode before they can tell whether it needs them.

How to write it:

- Plain words beat precise jargon. "Purchases can approve themselves" lands;
  "`skip_if_no_approvers` permits terminal auto-approval" does not.
- Size things honestly in both directions - say when something feared turns out to
  be a one-line fix, and say when something small turns out to be load-bearing.
- Put the worst finding where it cannot be missed, even if that breaks the order.
- Never place resolved problems under a heading that suggests they remain broken.
  When all reported defects were fixed, say so plainly and omit any unresolved-
  findings section.
- Keep file/line references out of the breakdown; they belong in `todo.md` and in
  the detail above it.
- Don't re-explain what I already know from the conversation.

## Asking, suggesting and disputing: use a real example

When you need a decision from the user, **ask the question directly**. Do not bury
it in a paragraph, do not quietly answer it yourself and move on, and do not hand
over a list of considerations in place of the question.

Then **show the consequence with a real example** - named people, a named school,
a specific sequence of events. The example is what makes a choice obvious, so it
is not decoration and it is not optional.

This applies equally to three things:

- **questions** - what needs deciding;
- **suggestions** - something you think should be done;
- **disputes** - something you think is wrong, including a decision already taken.

Write the example the way it would actually happen:

> Bright Star School enrols Tunde and the admin mistypes his mother's address as
> `adaokeye@gmail.com`. That address belongs to a stranger who already has an
> account, because her own daughter attends Greenfield. If an attached link shows
> the full record straight away, she opens her app and sees Tunde's class, his
> fees, his home address and his father's phone number.

Not:

> Attached links may expose PII to an incorrect recipient where the email address
> is mistyped.

The second one is true and nobody can act on it. Abstractions hide the size of a
thing in both directions - they make a small risk sound alarming and a serious one
sound routine. A concrete case is the only way it can be weighed.

Keep it short. One example, the shortest one that still shows the consequence.
Where a choice has two sides, show the bad case **and** the good case, not only
the side you favour.

## Module documentation initiative

When asked to continue the module docs (or anything touching `docs/finance/`,
`docs/payments/`, `docs/procurement/`): **read `docs/module-docs-playbook.md`
first and follow it exactly.** It defines the slice-report loop (trace →
template → commit → gotcha briefing → user picks → fixes), the conductor
working mode (main session orchestrates + QAs; Opus-high subagents write all
feature code; agents never commit), and the conventions (stage files
explicitly - never `git add -A`; commit to main, don't push; one sequential
agent when fixes share constants.py/migrations; run the test suite yourself
after agent work). Template: `docs/finance/_report_template.md`. Status and
next slices live at the top of the playbook.

## Comments: short inline, the story in the docstring

An inline comment is a label, not an explanation. Keep it to one short line that
names what the next line or block does. If the point takes more than that to
make, it does not belong inline: move it into the docstring of the module,
class, function or method it concerns.

The docstring is where the reasoning lives. Write it there once, properly, and
let the code below stay clean.

### Write for a stranger reading it years from now

Every comment and docstring is permanent documentation. It has to read the same
way to somebody who has never seen this branch, this milestone or this
conversation. Describe the code as it is, in the present tense, and let it
stand on its own.

That rules out:

- milestone, sprint, wave and ticket names - `M16`, `wave 3`, `the RBAC sprint`;
- change narration - "added", "changed", "moved here", "now returns", "used to";
- notes aimed at a reviewer - "note that", "as discussed", "for now",
  "temporary until we", "so you can see it working";
- time references - "recently", "since the refactor", "will be removed later".

Not this:

```python
# M16 config added here so the setting is enabled on the preview screen
preview_enabled = resolve_flag(tenant, "notification_preview")
```

This:

```python
def render_preview(template, tenant):
    """Render a notification exactly as its recipient will receive it.

    Branding, locale and channel settings resolve from the tenant rather than
    from the request, so an admin checking a template sees what the recipient
    sees.
    """
    preview_enabled = resolve_flag(tenant, "notification_preview")
```

The second version says more, and it stays true and useful long after the
milestone that prompted it is forgotten.

### What a docstring should carry

Say what the thing is for, and what a caller needs to know that the signature
does not already tell them: the invariant it keeps, the scope it applies to,
the condition that makes it behave differently, the reason behind a choice that
looks odd. Do not restate the parameter list in prose, and do not turn the
docstring into a history of the file.

This applies to every comment written anywhere in the codebase, tests included,
and to every comment already sitting beside code being changed: bring it up to
this standard rather than leaving it as found.

## Writing punctuation

Do not use em dashes (Unicode U+2014) anywhere in source code, comments,
documentation, tests, or user-facing copy. Use a comma, colon, parentheses, or
an ordinary hyphen (`-`), whichever reads most naturally.

## XVS requirements documentation maintenance

The XVS requirements documents in `docs/frd/` are living release artifacts, not
static references. They have two distinct document families:

- The **Module Requirements Document (MRD)** is the cross-module tracker in
  `docs/frd/module-requirements/`. New files use
  `XVS_Module_Requirements_Document_v*.docx`; historical files may use
  `XVS_Module_FR_Breakdown_v*.docx`.
- A **Functional Requirements Document (FRD)** is the detailed contract for one
  module. Each available module FRD lives in its own folder under
  `docs/frd/functional-requirements/`. New files use
  `*Functional_Requirements_Document_v*.docx`; historical files may use
  `*_FRD_v*.docx`.

For every completed backend change that can alter product behaviour, inspect the
latest MRD before asking to commit. Select the latest semantic version across the
new and historical filename patterns, ignore Office lock files such as
`~$*.docx`, and read the document before deciding what must change. Then identify
every affected module and inspect the latest existing FRD in each affected module
folder. Do not create a missing module FRD automatically. Report that it is
missing and wait for the user to request its creation.

### When an update is required

Create a new MRD version when completed work changes a documented capability,
module status, integration state, application ownership, dependency, known
limitation, priority gap, or recommended build order. This includes a bug fix
that resolves or changes an item under **Needs Attention** or **Priority Gaps**.

Create a new version of each affected existing FRD when completed work changes
module behaviour, a functional requirement, acceptance criteria, actor or
permission rules, tenant or branch scope, workflow or lifecycle behaviour, data
relationships, API or validation contracts, dependencies, audit or notification
effects, a known limitation, **Needs Attention**, or MRD traceability.

Pure refactors, test-only changes, formatting, and internal maintenance that do
not change product behaviour do not require document churn. State that the latest
MRD and affected existing FRDs were checked and why no version change was needed.

### How to revise the documents

1. Start from the latest document in that family. Preserve its visual system,
   headers, footer, watermark, module number, and prior version file. Never
   overwrite or rename a previous version.
2. Recheck the affected code and adjacent flows. In the MRD, update the module
   index, backend and integration states, capability table, code ownership,
   dependencies, and capability count wherever evidence changed. In an FRD,
   update requirements, acceptance, workflows, data and API contracts,
   operational evidence, and traceability wherever evidence changed.
3. Treat **Needs Attention** as current state, not history:
   - remove an item only when implementation and relevant verification fully
     resolve it;
   - rewrite it when the risk is only partly resolved or has changed shape;
   - add newly discovered material gaps, but do not pad the section with optional
     ideas or speculative features.
4. Reconcile MRD and affected FRDs, but version them independently. Their module
   status, capability names, current limitations, and traceability must agree.
5. Reconcile each document's control page, contents, status summaries, current
   gaps, traceability or global priorities, and change log after editing.
6. Do not carry revision-specific cleanup sections forward mechanically. Replace
   them with a delta against the immediately previous version only when useful;
   otherwise remove them and rely on the change log.
7. Do not infer frontend delivery, deployment, production adoption, or data
   migration from backend evidence. Keep backend completion and integration
   readiness separate.
8. Use the document creation and render workflow for `.docx` files. Render every
   page and correct clipping, stale version labels, split headings, orphaned
   notes, broken tables, stale contents, and inconsistent page numbers before
   presenting the documents.

### Version selection

- For the MRD, use a patch version for document-only corrections, presentation,
  wording, or metadata that does not change roadmap meaning. Use a minor version
  when functionality, module or integration status, ownership, a current gap, or
  priorities change. Use a major version only for a deliberate restructuring of
  the module taxonomy, numbering, status model, or product architecture baseline.
- For an FRD, use a patch version for document-only corrections, presentation,
  wording, or metadata. Use a minor version when behaviour, acceptance, status,
  contracts, dependencies, traceability, or a current gap changes. Use a major
  version only for a deliberate rewrite of the module's functional baseline or
  document structure.
- Derive the next version from the latest file and its document-control record.
  Update the filename, cover, document control, contents where relevant, source
  references, and change log together.

### Review and commit gate

Generate revised MRD and FRD versions after implementation and verification,
then give the user the code summary and new documents for review. Do not stage or
commit the implementation or generated documents until the user approves them,
unless the user explicitly waives this review gate. After approval, stage only
the intended files, never use `git add -A`, and never include Office lock files,
rendered PDFs, page images, or other render artifacts in the commit.

## Vocabulary: it is a **branch**, never a campus

A school site is a **branch**. That is the word the data model uses
(`Branch`, `branch_id`, `branch__isnull=True`), the word the API returns
(`branch`, `branch_name`, `scope_label`), and the word the product uses on
screen.

Never write "campus" - not in UI copy, not in comments, not in variable names,
not in commit messages, not in docs. A design prototype or a mockup that says
"campus" is using the wrong word: translate it to branch as you build. The same
goes for "site" and "location" when a branch is meant.

| Say | Not |
| --- | --- |
| Ikeja Branch | Ikeja Campus |
| All branches | All campuses |
| School-wide | Applies to the whole school (fine), "every campus" (not) |
| This branch runs the class | This campus runs the class |
| Branch admin | Campus admin |

The one exception is quoted third-party text - an error message from an
external system, or a school's own words in a support ticket. Quote those
verbatim and do not silently correct them.

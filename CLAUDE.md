# CLAUDE.md - backend

## What this codebase is - and what XVS is

**This repo is a multi-domain platform, not a schools application.** The engine
apps - `vs_finance`, `vs_procurement`, `vs_payments`, `vs_rbac`, `vs_workflow`,
`vs_notifications`, `vs_audit`, `core` - are deliberately **domain-neutral**. They
know about entities, customers, invoices, vendors, roles and approvals; they know
nothing about schools. `vs_health` (VIGIL) is already a second domain standing on
the same foundation, and there will be more.

**XVS is the first product built on that platform** - the schools product.

Two rules follow, and they are separate:

1. **Keep the engines domain-neutral.** School concepts - students, guardians,
   classes, terms, sessions - live in the school apps (`apps/schools/`) and reach
   the engines through the FAL (which lives under `apps/schools/`, not in `core/`:
   it is school-specific by design). Never leak school vocabulary into
   `vs_finance` and friends. That leak is precisely what the FAL exists to
   prevent; if you find yourself adding a `student` or `term` field to a generic
   app, stop.
   **The engines must not import `vs_schools`** (or anything under
   `apps/schools/`). The site primitive is `vs_tenants.Branch`, owned directly by
   `Tenant`: reach it as `row.branch` or `tenant.branches`, never as
   `branch.school.tenant`. If an engine needs a school-only fact, it belongs
   behind the FAL, not behind an import.

   **The word "school" belongs to `apps/schools/`.** Outside that folder, say
   **tenant**: in parameter names, serializer fields, constants, variables and
   JSON body keys alike. `LoginService.login(..., tenant=...)`, never
   `school=...`, because `vs_user` is an engine app. Prose may still mention a
   school where it explains where a value comes from ("the tenant slug the
   frontend takes from the school's subdomain") - the ban is on identifiers, not
   on explanations.

2. **Within XVS, build for every school, not the first one.** XVS is multi-tenant;
   Corona Secondary School (at `xvs.codexng.com`) is simply the first tenant.
   Nothing may be special-cased to one tenant's arrangement - if a feature only
   works because of how the first school happens to be set up, it isn't finished.

   **Every school has at least one branch.** A school is created with a main
   branch and can never have none, so every user, document and record can always
   be given one. Do not write code that handles a branchless school; that shape
   does not exist.

   **A school with exactly one branch still needs the dimension to recede.** One
   branch is the common case, and a switcher with a single entry, a column that
   repeats the same value on every row, or a filter with one option are all noise.
   Where a school has one branch, the control is absent, not disabled. Where it
   has several, branch appears wherever it changes meaning.

   **A null branch means "shared across the school", never "no branches exist".**
   That is a deliberate, first-class value (see academic structure and procurement
   documents), and it keeps its meaning however many branches a school has.

   Test more than one shape of school - a single-branch test proves nothing about
   a multi-branch one.

## Running the test suite on this machine

This box cannot run two suites at once. A parallel run from another session
starved a running suite until the OS killed it (exit 144, with the machine down
to roughly 16 MB free). It is not a code failure and retrying the same way just
repeats it.

- **Run one app at a time**, not several app labels in one command, and **never
  `--parallel`**. Sequential runs survive contention; combined ones get killed.
- **Always pass a unique `DB_NAME`** - for example
  `cd apps && DB_NAME=cx_myslice ../cx/bin/python manage.py test <one_app> --settings=apps.settings.local --noinput`.
  Sessions otherwise share `test_cx_db`, and one recreating it mid-run makes
  another report phantom failures - 204 of them, once.
- **In a worktree, use the absolute path to the venv.** `./cx` is gitignored, so
  it does not exist there, and a relative path produces **empty output with a
  zero exit code** - which reads exactly like a passing run with no summary.
- Treat an exit code alone as insufficient evidence. Quote the `Ran N tests` line.
  If it is missing, the run did not finish and must be repeated.
- **Iterate with the fast form, verify with the full one.** Two
  `TransactionTestCase` classes in `schools.vs_schools` are tagged `slow`
  (`BranchCodeAllocationConcurrencyTests` and `_MigrationHarness` and its
  subclasses). They flush and rebuild the database around every test and re-run
  the migration graph, and they are why that app takes ~18 minutes when the
  other 171 tests take 30 seconds. While working, run
  `--exclude-tag=slow`. **The run you report must be the full one**, without
  the flag: the excluded classes are exactly the ones exercising migrations and
  the branch code allocator, so a change touching either would slip past the
  fast form. Do not use `--keepdb` for the run you report either - it reuses a
  stale schema and can pass against code it no longer matches, which is the
  failure that looks like success. Tag new slow classes the same way; the tag
  is inherited, so a base class needs marking once.
- **Run one test class or method** with the dotted path
  (`manage.py test schools.vs_schools.tests_update_endpoints.SchoolSlugUpdateTests`)
  when you are working inside a single file. Seconds, not minutes.

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
     (`success_response` keeps `[]` a list; only an absent payload becomes `{}`).
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
- **What we found wrong in the code** - real defects and gaps, grouped under short
  themes once there are more than about four. **Only if there are findings** - if
  nothing is wrong, leave this out entirely rather than writing "nothing found".
- **Where to go next** - the order of the next steps, and which of them are
  unblocked right now.

That list is closed. Do not invent a heading for something that does not fit one
of them: put it under the heading it belongs to, and if it belongs under none of
them, leave it out of the breakdown entirely. A section I did not ask for is one
I have to decode before I can tell whether it needs me.

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

When you need a decision from me, **ask the question directly**. Do not bury it in
a paragraph, do not quietly answer it yourself and move on, and do not hand me a
list of considerations in place of the question.

Then **show me the consequence with a real example** - named people, a named
school, a specific sequence of events. The example is what makes a choice
obvious, so it is not decoration and it is not optional.

This applies equally to three things:

- **questions** - what you need me to decide;
- **suggestions** - something you think we should do;
- **disputes** - something you think is wrong, including something I decided.

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
sound routine. A concrete case is the only way I can weigh it.

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

## Fixing problems: root cause, not symptom

When I ask you to fix a problem, treat the reported issue as one *instance* of
a potentially wider defect - fix it holistically:

1. **Trace it to its source.** Ask why the bug exists - a wrong assumption, a
   missing invariant, a fragile pattern - not just where it surfaced.
2. **Fix the class, not the case.** If the same root cause can bite elsewhere
   (other views, serializers, services, callers of the same helper), fix it at
   the choke point they all share, or sweep the other occurrences in the same
   change.
3. **Name the root.** In the summary/commit, state the underlying cause and
   where else it applied, so the fix is reviewable as a class-fix, not a patch.

A fix that only silences the reported symptom while the source remains is not
done - that includes suppressing errors, special-casing one caller, or adding
a guard where the real problem is upstream. The goal is that future problems
from the same source never happen.

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

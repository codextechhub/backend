# AGENTS.md - backend

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

## Writing punctuation

Do not use em dashes (Unicode U+2014) anywhere in source code, comments,
documentation, tests, or user-facing copy. Use a comma, colon, parentheses, or
an ordinary hyphen (`-`), whichever reads most naturally.

## XVS feature breakdown maintenance

The XVS module tracker in `docs/frd/` is a living release artifact, not a static
reference. For every completed backend change, inspect the latest
`XVS_Module_FR_Breakdown_v*.docx` before asking to commit. Select the latest
semantic version, ignore Office lock files such as `~$*.docx`, and read the
document before deciding what must change.

### When an update is required

Create a new document version when the completed work changes any documented
capability, module status, integration state, application ownership, dependency,
known limitation, priority gap, or recommended build order. This includes a bug
fix that resolves or changes an item under **Needs Attention** or **Priority
Gaps**. Pure refactors, test-only changes, formatting, and internal maintenance
that do not change product behaviour do not require document churn, but state
that the latest breakdown was checked and why no update was needed.

### How to revise it

1. Start from the latest document and preserve its visual system, headers,
   footer, watermark, module numbers, and prior version file. Never overwrite or
   rename the previous version.
2. Recheck the affected code and its adjacent flows. Update the module index,
   backend and integration states, capability table, code ownership, dependencies,
   and feature count wherever the evidence changed.
3. Treat **Needs Attention** as current state, not history:
   - remove an item only when the completed implementation and relevant
     verification fully resolve it;
   - rewrite it when the risk is only partly resolved or has changed shape;
   - add newly discovered material gaps, but do not pad the section with optional
     ideas or speculative features.
4. Reconcile the global sections after every affected-module edit: document
   control, contents, module index, priority gaps, recommended build order, and
   change log must agree with the module tables.
5. Do not carry revision-specific cleanup sections forward mechanically. For
   example, `Removed or Relocated v2.2 Claims` belongs to v2.3 history. In a later
   version, replace it with a delta against the immediately previous version only
   when that delta is useful; otherwise remove it and rely on the change log.
6. Do not mark frontend delivery, deployment, production adoption, or data
   migration complete from backend evidence alone. Keep backend completion and
   integration readiness separate.
7. Use the document creation and render workflow for `.docx` files. Render every
   page and correct clipping, stale version labels, split headings, orphaned
   notes, broken tables, stale contents, and inconsistent page numbers before
   presenting the document.

### Version selection

- Use a patch version such as `2.3.1` for document-only corrections, wording,
  presentation, or metadata that does not change roadmap meaning.
- Use a minor version such as `2.4` when functionality is added or removed, a
  module or integration status changes, a **Needs Attention** item is resolved or
  added, ownership moves, or priorities change.
- Use a major version such as `3.0` only for a deliberate restructuring of the
  module taxonomy, numbering, status model, or product architecture baseline.
- Derive the next version from the latest file and its document-control record.
  Update the filename, cover, document control, contents where relevant, and
  change log together.

### Review and commit gate

Generate the revised breakdown after implementation and verification, then give
the user the code summary and new document for review. Do not stage or commit the
implementation or generated breakdown until the user approves them, unless the
user explicitly waives this review gate. After approval, stage only the intended
files, never use `git add -A`, and never include Office lock files or render
artifacts in the commit.

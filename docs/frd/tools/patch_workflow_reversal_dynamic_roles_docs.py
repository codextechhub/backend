#!/usr/bin/env python3
"""Version the Module 7 FRD for reversal, role-change ladders and named Dynamic Roles.

Reversing an approval rewrote the engine's own rows and stopped there. It never
asked the module owning the document whether the decision could still be undone,
so a payout batch the provider had already paid could read as awaiting a
decision, and it never unwound the ladder, so the stages after the reopened one
kept their approved rows and the instance could not be finished. The reversal
now runs through a handler contract and rolls the later stages back.

Around that sit six smaller changes to the same engine: a template whose steps
are all retired counts as unconfigured; a role change is decided by a workflow
ladder whose requester may decide their own; activating a shared template's
Dynamic Role step records the rule's key; the approver preview looks its sample
requester up inside the caller's tenant; a school's School Admin is provisioned
with the workflow keys from the role library; and a tenant can build named
Dynamic Roles whose rules read the document and the person who raised it.

The FRD is reconciled to MRD v2.76, which gains one Module 7 capability entry
for named Dynamic Roles.

    python tools/patch_workflow_reversal_dynamic_roles_docs.py
"""

from __future__ import annotations

import argparse
import copy
import re
from pathlib import Path

from docx import Document
from docx.oxml.ns import qn
from docx.table import Table
from docx.text.paragraph import Paragraph

from generate_requirements_documents import (
    assert_no_em_dash,
    shrink_inherited_media,
    update_extended_title,
    write_cell,
)

REVIEW_DATE = "11 September 2026"
SHORT_DATE = "11 Sep 2026"
MRD_VERSION = "2.76"
CODE_BASELINE = "Backend worktree at 13675db, 11 September 2026"

M07_DIR = "07-workflow-and-approval-engine"
M07_STEM = "XVS_M07_Workflow_and_Approval_Engine_Functional_Requirements_Document"
SOURCE, TARGET = "1.8", "1.9"

#: The version whose Needs Attention box still carries the document's styling:
#: a bold amber heading paragraph over one paragraph per bullet.
GAPS_STYLE_SOURCE = "1.7"

#: Requirement table rows, by position.
REQUIREMENT_ROW, EVIDENCE_ROW, ACCEPTANCE_ROW, LIMIT_ROW = 1, 2, 3, 4

#: Size of a requirement table's value cells, as the generator writes them.
FR_SIZE = 8.5


# ── shared docx helpers ──────────────────────────────────────────────────────

def replace_cell(cell, text: str, **kwargs) -> None:
    while len(cell.paragraphs) > 1:
        paragraph = cell.paragraphs[-1]
        paragraph._p.getparent().remove(paragraph._p)
    write_cell(cell, text, **kwargs)


def set_run_text(paragraph, text: str) -> None:
    """Rewrite a paragraph in place, keeping the formatting of its first run."""
    if not paragraph.runs:
        raise ValueError("Paragraph carries no run to inherit formatting from")
    paragraph.runs[0].text = text
    for run in paragraph.runs[1:]:
        run.text = ""


def rewrite_cell(cell, text: str) -> None:
    """Rewrite a one-paragraph cell, keeping the formatting it already has.

    Grid tables mix bold labels, plain values and header fills, so writing a
    fresh run would have to restate each of them. Keeping the first run does not.
    """
    while len(cell.paragraphs) > 1:
        paragraph = cell.paragraphs[-1]
        paragraph._p.getparent().remove(paragraph._p)
    set_run_text(cell.paragraphs[0], text)


def replace_cover_version(table, target: str) -> None:
    """Set the cover's version label, which is one run inside the cover table."""
    for paragraph in table.rows[0].cells[0].paragraphs:
        for run in paragraph.runs:
            if "Version:" in run.text:
                run.text = re.sub(r"Version:\s*[\d.]+", f"Version: {target}", run.text)
                return
    raise ValueError(f"No cover version label found (expected to set {target}).")


def replace_control_value(table, label: str, value: str) -> None:
    for row in table.rows:
        if row.cells[0].text.strip() == label:
            replace_cell(row.cells[1], value, size=9)
            return
    raise ValueError(f"Control row not found: {label}")


def replace_body_paragraph(doc, prefix: str, text: str) -> None:
    for paragraph in doc.paragraphs:
        if paragraph.text.strip().startswith(prefix):
            set_run_text(paragraph, text)
            return
    raise ValueError(f"Body paragraph not found: {prefix}")


def find_row(table, label: str):
    """The row whose first cell reads exactly ``label``.

    Exact rather than a prefix, because the data-model table holds names that
    begin one another: WorkflowStage, WorkflowStageApprover and
    WorkflowStageApproverOverride.
    """
    for row in table.rows:
        if row.cells[0].text.strip() == label:
            return row
    raise ValueError(f"Row not found: {label}")


def insert_row_before(table, label: str, values: list[str]):
    """Clone the row named ``label`` into the slot above it and fill the copy."""
    anchor = find_row(table, label)
    clone = copy.deepcopy(anchor._tr)
    anchor._tr.addprevious(clone)
    for row in table.rows:
        if row._tr is clone:
            for cell, value in zip(row.cells, values):
                rewrite_cell(cell, value)
            return row
    raise ValueError(f"Inserted row above {label} not found")


def append_row(table, values: list[str]):
    template = table.rows[-1]
    template._tr.addnext(copy.deepcopy(template._tr))
    row = table.rows[-1]
    for cell, value in zip(row.cells, values):
        rewrite_cell(cell, value)
    return row


def prepend_change_log(table, version: str, summary: str) -> None:
    template = table.rows[1]
    template._tr.addprevious(copy.deepcopy(template._tr))
    row = table.rows[1]
    replace_cell(row.cells[0], version, size=8)
    replace_cell(row.cells[1], SHORT_DATE, size=8)
    replace_cell(row.cells[2], summary, size=8)


def finish(doc, output: Path, title: str, version: str) -> None:
    doc.core_properties.title = title
    doc.core_properties.version = version
    output.parent.mkdir(parents=True, exist_ok=True)
    doc.save(str(output))
    update_extended_title(output, title)
    shrink_inherited_media(output)
    assert_no_em_dash(output)


def require_fr(table, label: str):
    if not table.rows[0].cells[0].text.strip().startswith(label):
        raise ValueError(f"Table is not {label}")
    return table


def require_header(table, text: str, *, row_one: str | None = None):
    """Refuse to edit a table that is not the one its index was read as."""
    if not table.rows[0].cells[0].text.strip().startswith(text):
        raise ValueError(f"Table header is not {text!r}")
    if row_one is not None and not table.rows[1].cells[0].text.strip().startswith(row_one):
        raise ValueError(f"First row of {text!r} is not {row_one!r}")
    return table


def set_fr(table, row_index: int, label: str, text: str) -> None:
    row = table.rows[row_index]
    if row.cells[0].text.strip() != label:
        raise ValueError(f"Row {row_index} is {row.cells[0].text.strip()!r}, not {label!r}")
    replace_cell(row.cells[1], text, size=FR_SIZE)


# ── Module 7 content ─────────────────────────────────────────────────────────

SUPPORTING_APPS = (
    "vs_rbac, vs_user, vs_tenants, vs_notifications, vs_audit; handlers "
    "registered by vs_finance, vs_procurement, vs_payments, vs_rbac (role "
    "changes), vs_user (user creation) and the school staff app (leave)"
)

SCOPE_RESOLUTION = (
    "Deciding which people may act on a stage: by role, by named approver group, "
    "by a named Dynamic Role or a stage's own rules reading the document and the "
    "person who raised it, or by climbing the organogram."
)

SCOPE_LIFECYCLE = (
    "Submit, approve, reject, return, resubmit, withdraw, cancel, and the "
    "administrative reversal of a recorded decision, which the module owning the "
    "document must agree to."
)

ACTOR_TENANT_ADMIN = (
    "Publishes tenant templates, maintains approver groups and Dynamic Roles, and "
    "repoints an individual step to a different approver. A school's School Admin "
    "is provisioned with the template and group manage keys from the role library."
)

ACTOR_REQUESTER = (
    "Submits a document, withdraws it, and resubmits after it is returned. Never "
    "approves their own submission, except a role change, whose handler permits "
    "it (FR-012)."
)

ACTOR_WORKFLOW_ADMIN = (
    "Cancels an instance or reverses a recorded decision, both of which are "
    "audited overrides. A reversal is refused where the module owning the "
    "document reports the decision already acted on (FR-019)."
)

GATE_PREVIEW_SCOPE = "Request tenant; the sample requester must belong to it"

GATE_DYNAMIC_ROLES = [
    "Dynamic Roles",
    "workflow.group.manage to write; workflow.group.view or "
    "workflow.template.manage to read, list fields, and preview",
    "Request tenant",
]

RULE_SELF_APPROVAL = [
    "The requester is never an approver, with one declared exception",
    "Self-approval is excluded once, centrally, for every approver source, and "
    "again when a vote is recorded. A document type may lift it only on its own "
    "handler; role changes are the one type that does.",
]

RULE_REVERSAL = [
    "A decision is undone only with the owning module's agreement",
    "Reversal asks the document's handler before writing anything and tells it "
    "afterwards in the same transaction. A document type with no registered "
    "handler cannot be reversed.",
]

FR004_LIMIT = (
    "Field paths in route conditions, inclusion conditions, and a stage's own "
    "rules are not checked against the document type, so such a condition may "
    "reference a field the document does not carry. That resolves to None at run "
    "time rather than failing. A named Dynamic Role's conditions are checked "
    "against the fields its document types declare (FR-032)."
)

FR005_EVIDENCE = (
    "A ROLE stage names a role by key and the engine reads that role's active "
    "assignments in the tenant that raised the request, so one definition serves "
    "every tenant that holds the key. Only a role carrying the provisioning mark, "
    "is_system_role, is resolved: ensure_approver_role sets it, and the resolver "
    "requires it. Provisioning gives a tenant approver groups rather than approver "
    "roles, so a ROLE stage names either a key a tenant chose or a role every "
    "school is provisioned with: the role-change ladder's single stage names "
    "school_admin. Migrations stop every live step naming a provisioned approver "
    "role that nobody holds, finding the keys from the published steps rather than "
    "from a written list. A step a document ran through is retired and kept as "
    "evidence, one nothing ran through is deleted with its role, and a role "
    "somebody holds is left alone."
)

FR007_EVIDENCE = (
    "ApproverSource.DYNAMIC_ROLE on a stage that names no Dynamic Role evaluates "
    "the stage's ordered WorkflowStageDynamicRule rows against the document; the "
    "first match names the role, which then resolves exactly as FR-005 does. A "
    "rule with no condition is the fallback and must be last. A stage may instead "
    "name one of the tenant's Dynamic Roles (FR-032), and publishing it that way "
    "clears the stage's own rules. A shared template cannot carry a DYNAMIC_ROLE "
    "stage in either form: publishing refuses it."
)

FR007_ACCEPTANCE = (
    "Publishing rejects a rule set with no rules, an unknown role, or rules placed "
    "after the fallback where they could never fire. The matched rule, its role "
    "key, and the full evaluation trace are written to the activation audit entry. "
    "The key is read from the rule itself, so a rule on a shared template "
    "published before that refusal, which stores the key without a role row, is "
    "recorded rather than failing the submission (DynamicRoleResolveTests)."
)

FR007_LIMIT = (
    "Rules read the document alone, as it stands when the stage activates; a "
    "document edited after submission is not re-evaluated within the same "
    "attempt. Routing on the person who raised the document needs a named Dynamic "
    "Role."
)

FR009_ACCEPTANCE = (
    "The role-change ladder is published this way: one tenant-less template whose "
    "single ROLE stage names school_admin serves every school, and each school "
    "resolves its own School Admin holders. The platform tenant's role changes run "
    "on a second template owned by that tenant and naming xvs_platform_admin, which "
    "keeps it off every school's template list. Procurement's shared platform row "
    "also resolves per tenant, and carries no live steps (FR-031)."
)

FR009_LIMIT = (
    "A tenant with no active role of that key resolves to no approver, and the "
    "stage parks, because a stage skips only when it is published to do so "
    "(FR-015). workflow_role_coverage reports these gaps (FR-021). A shared "
    "template cannot use a Dynamic Role or rules of its own, so a central step "
    "names one role for every tenant."
)

FR011_EVIDENCE = (
    "WorkflowStageApprover rows are written at activation and are not amended "
    "while the stage runs. Every later eligibility check reads that snapshot rather "
    "than re-querying roles. A stage the engine activates again on the same "
    "attempt, because a reversal rolled it back, is a fresh run: its snapshot is "
    "replaced rather than added to."
)

FR011_ACCEPTANCE = (
    "A role change after activation does not add or remove an approver for that "
    "attempt. A returned and resubmitted instance activates a new attempt with a "
    "fresh snapshot, and a stage re-entered after a reversal lists each approver "
    "once, so a unanimous stage can still reach its threshold "
    "(ReversalUnwindTests)."
)

FR012_REQUIREMENT = (
    "A requester can never approve their own submission, whichever approver source "
    "the stage uses, unless the module that owns the document type declares "
    "otherwise on its handler."
)

FR012_EVIDENCE = (
    "resolve_approvers removes the requester once, after the source-specific "
    "branch, and drops any delegation that would hand them the stage; "
    "record_action refuses the requester again when they vote, so a snapshot cannot "
    "let them through. Both read allows_requester_self_approval from the "
    "document's handler. It is False on the base handler and True for "
    "rbac.role_change alone, because in most schools the one person who administers "
    "roles is also the only one who can approve a change, and excluding that person "
    "leaves the stage with nobody on it."
)

FR012_ACCEPTANCE = (
    "A requester who also holds the approving role is absent from the frozen list; "
    "where they were the only holder, the stage resolves to nobody and the "
    "no-approver policy applies. On a role change the requester stays eligible and "
    "can decide, a second administrator is resolved alongside them, and the "
    "exemption reaches no other document type (RoleChangeLadderTests)."
)

FR012_LIMIT = (
    "On a role change, a second pair of eyes depends on the school's own practice: "
    "the stage advances on any one approval, so the requester may decide their own "
    "request even where a second administrator is eligible. The audit answers who "
    "signed off their own request by comparing the requester with the acting "
    "approver."
)

FR014_ACCEPTANCE = (
    "A duplicate live vote by the same actor in the same attempt is rejected by a "
    "database constraint, so a tally cannot be inflated. A reversed vote is not "
    "live, so an approver whose vote was reversed may vote again on the same "
    "attempt (ReverseActionTests)."
)

FR019_REQUIREMENT = (
    "An administrator can reverse a decision without erasing it, and only where "
    "the module that owns the document agrees the decision has not already been "
    "acted on."
)

FR019_EVIDENCE = (
    "reverse_action asks the document's handler through validate_reversal before "
    "writing anything, then voids the vote: the original gains reversed_at, "
    "reversed_by, and a reason, and a reversal row is appended. Where the reversed "
    "vote is what decided its stage, the stage reopens, every stage activated "
    "after it returns to PENDING with its live votes voided and its snapshot "
    "cleared, a decided instance returns to IN_PROGRESS, and the handler's "
    "on_action_reversed puts the document back inside the same transaction. A vote "
    "the stage did not need is voided and nothing else moves. Payouts refuse once "
    "any instruction is claimed for the provider, and dispatch re-checks the "
    "approval under the same row lock; finance refuses once the document has moved "
    "past draft or pending approval; procurement refuses a purchase order whose "
    "vendor email is pending or sent; user creation refuses once the account has "
    "left pending approval; role changes refuse once the request is decided. Leave "
    "returns a decided request to pending. A document type with no registered "
    "handler cannot be reversed. The view returns 404 for an action belonging to "
    "another tenant."
)

FR019_ACCEPTANCE = (
    "The original decision remains readable, tallies count only unreversed "
    "actions, and an approver whose vote was reversed can vote again. A refusal "
    "leaves the approval, its stages, and its votes as they were, and a handler "
    "that fails while putting its document back rolls the whole reversal back. A "
    "vote from an attempt its stage has since run past cannot be reversed. The "
    "ACTION_REVERSED audit entry names the reopened stage and every stage rolled "
    "back. ReverseActionTests, ReverseActionUnknownHandlerTests, and "
    "ReversalUnwindTests cover the engine; PayoutBatchApprovalTests, "
    "JournalApprovalWorkflowTests, WorkflowApprovalTests, "
    "UserCreationReversalTests, and the leave DecisionTests cover the refusals and "
    "the documents put back."
)

FR019_LIMIT = (
    "The base handler allows every reversal, so a document is protected only where "
    "its module implements the check. Procurement checks purchase orders alone: a "
    "vendor invoice or vendor payment that was approved and then posted can still "
    "have its approval reversed, which returns its approval state to pending while "
    "the posted document and its journal stand. A rejected, withdrawn, or "
    "cancelled instance cannot be reversed."
)

FR020_EVIDENCE = (
    "The preview endpoint accepts any approver source and, for a stage's own "
    "rules, a sample document, or a saved Dynamic Role by code with a sample "
    "carrying the amount, branch, and fields a real document would. It returns the "
    "resolved people and, for rules, which rule matched with the per-rule "
    "evaluation trace. The sample requester is looked up inside the caller's "
    "tenant, because resolution runs in the requester's tenant and would otherwise "
    "answer with another school's approvers."
)

FR020_ACCEPTANCE = (
    "The preview runs the engine's own resolution, so it cannot disagree with a "
    "later activation. An unknown role key or Dynamic Role is reported rather than "
    "silently returning nobody. A requester id from another tenant, or a malformed "
    "one, is answered as not found (PreviewRequesterScopeTests), and a saved "
    "Dynamic Role previews through the matching activation uses "
    "(DynamicRolePreviewTests)."
)

FR020_LIMIT = (
    "The preview evaluates a posted configuration or a saved Dynamic Role, not a "
    "stored stage, so it cannot preview an existing stage by reference."
)

FR021_ACCEPTANCE = (
    "The command distinguishes a missing role from an unassigned one, and --create "
    "never assigns anybody, so it cannot invent approval authority. The sole-holder "
    "line is a block rather than a note about redundancy: the requester is "
    "filtered out of the eligible set, so where one person holds the approving "
    "role, everything they raise parks with nobody able to release it. Counting "
    "people is what keeps that line firing for somebody who holds the role at two "
    "branches."
)

FR021_LIMIT = (
    "It is a command, not a scheduled check, and nothing blocks a deploy when "
    "coverage is incomplete. It checks every central stage against every active "
    "tenant without regard to the document type the stage serves, so it looks for "
    "school_admin in the platform tenant too, whose role changes run on a ladder "
    "of its own, and it reports a sole School Admin as blocking role changes, "
    "although that person may decide their own."
)

FR025_EVIDENCE = (
    "WorkflowAuditLog rows are written for submission, activation, each vote, "
    "skips with their reason, route evaluation, returns, reversals, and terminal "
    "outcomes. Dynamic role activations additionally record the matched rule and "
    "its trace, and for a named Dynamic Role its code and the target the rule "
    "chose. A reversal's entry names the stage it reopened and every stage it "
    "rolled back."
)

FR031_EVIDENCE = (
    "submit_for_approval raises ApprovalNotConfiguredError (409, "
    "APPROVAL_NOT_CONFIGURED) when the resolved template has no live stages, after "
    "rolling back the pending flip so a refused submit writes nothing. A retired "
    "stage is history rather than configuration, so a template holding only "
    "retired stages is refused the same way instead of being routed past each of "
    "them to an APPROVED outcome. A caller may retry with an explicit "
    "confirmation, which terminates the instance as approved and records who "
    "confirmed and why through the POSTED_WITHOUT_APPROVAL audit action. The "
    "finance direct-post gate applies the same live-stage rule at its own boundary "
    "through approval_unconfigured."
)

FR031_ACCEPTANCE = (
    "A tenant that has published no ladder of its own resolves to the shared "
    "platform template, which carries no live steps, and its first submission is "
    "refused with a named reason rather than approved. A ladder whose every step "
    "is retired is refused the same way. The same submission sent with a "
    "confirmation succeeds and leaves an audit event naming the confirmer. An "
    "empty ladder is never treated as consent. StagelessTemplateSubmissionTests "
    "covers the bare ladder, the all-retired ladder, the write-free refusal, the "
    "recorded confirmation, and the error code."
)

FR032_HEADING = "FR-032 Build and Use Named Dynamic Roles"
FR032_HEADER = "FR-032 | Implemented"

FR032 = {
    REQUIREMENT_ROW: (
        "A tenant administrator can build a named set of ordered rules once, "
        "deciding who approves from the document and the person who raised it, and "
        "pick it by name in any stage of the tenant's own templates."
    ),
    EVIDENCE_ROW: (
        "WorkflowDynamicRole holds the tenant's code, name, the document types it "
        "serves (none means any), and an active flag; WorkflowDynamicRoleRule holds "
        "its ordered rules, each sending to a role's holders, a named person, or an "
        "approver group, with a check constraint tying the one target to its kind. "
        "services.dynamic_roles.validate_rules checks every save against the fields "
        "those document types offer: the branch every document has, the amount "
        "where every type served carries one, the document type where several are "
        "served, the requester's id, roles, and branch, a type's own fields declared "
        "on its handler, and requester facts a domain app registers, the school "
        "staff app supplying job title and contract. It refuses an unknown field, "
        "an operator the field cannot use, an amount that is not whole kobo, a "
        "choice off the list, a branch, person, group, or role outside the tenant, "
        "a role the engine would not nominate, anything other than comparisons "
        "joined by and, and a set without its Otherwise rule last. A stage names "
        "one through dynamic_role_code, and publishing refuses one that is "
        "inactive, another tenant's, or not set up for the template's document "
        "type. Activation matches the rules against the document and the requester "
        "and records the Dynamic Role, the matched target, and the per-rule trace."
    ),
    ACCEPTANCE_ROW: (
        "An edit reaches every stage using the Dynamic Role at its next activation, "
        "with no republish. A deactivated one resolves to nobody, so its stages park "
        "rather than route on retired rules, and the requester is removed from its "
        "answer as from every source. Deleting one that any stage names returns 409 "
        "DYNAMIC_ROLE_IN_USE, its code cannot change, and a document type a live "
        "stage still uses cannot be dropped. Reads need workflow.group.view or "
        "workflow.template.manage and writes need workflow.group.manage, each "
        "confined to the caller's tenant. DynamicRoleAccessTests, "
        "DynamicRoleRuleCheckTests, DynamicRoleApiTests, DynamicRoleResolutionTests, "
        "RequesterFactsTests, PublishWithDynamicRoleTests, and "
        "DynamicRolePreviewTests cover these."
    ),
    LIMIT_ROW: (
        "A shared template cannot use a Dynamic Role, by decision: each school "
        "builds its own. Conditions join comparisons with and only, so an or is "
        "written as a second rule sending to the same place. A requester-facts "
        "provider that fails contributes nothing and its conditions read as false, "
        "so the document falls through to a later rule, at worst the Otherwise "
        "rule, and the failure is only logged."
    ),
}

LIFECYCLE_RETURNED = (
    "IN_PROGRESS on resubmission, at a higher attempt, or when the vote that "
    "returned it is reversed."
)

LIFECYCLE_APPROVED = (
    "Terminal, unless an administrator reverses a vote the outcome depended on and "
    "the owning module agrees; the instance then returns to IN_PROGRESS."
)

ACTIVATION_PARAGRAPH = (
    "Activation is the moment the engine commits to a set of approvers. It "
    "evaluates the inclusion condition, resolves approvers through the stage's "
    "source or the tenant's override, writes the frozen snapshot, records the "
    "activation with its evidence, and notifies the people on the snapshot. A "
    "stage that resolves to nobody is skipped, or activated with an empty "
    "snapshot, according to its own policy. The second is a parked document, which "
    "FR-027 and FR-028 govern. A stage activated again on the same attempt, "
    "because a reversal rolled it back, replaces its snapshot rather than adding "
    "to it."
)

ORDER_SOURCE = (
    "Otherwise the stage's approver source resolves: role, approver group, a named "
    "Dynamic Role or the stage's own rules, or organogram."
)

ORDER_REQUESTER = (
    "The requester is removed, unless the document's handler permits "
    "self-approval, which only role changes do."
)

MODEL_STAGE = (
    "One step: kind, order, approver source and its configuration, including a "
    "named Dynamic Role, and advance and rejection rules.",
    "Soft-retired on republish, never deleted by it. A retired step neither routes "
    "nor counts as configuration, and one a document ran through is kept as "
    "evidence.",
)

MODEL_STAGE_RULE = (
    "An ordered condition and the role key it selects, owned by a stage that names "
    "no Dynamic Role.",
    "Replaced wholesale on republish, and cleared when the stage names a Dynamic "
    "Role.",
)

MODEL_DYNAMIC_ROLE = [
    "WorkflowDynamicRole",
    "A tenant's named rule set choosing who approves, the document types it "
    "serves, and an active flag. The code is unique in the tenant and fixed once "
    "created.",
    "Refused while any stage names it; stages hold it with PROTECT.",
]

MODEL_DYNAMIC_ROLE_RULE = [
    "WorkflowDynamicRoleRule",
    "One ordered rule: a condition, or none for the Otherwise rule, and exactly one "
    "target, a role, a person, or an approver group, matched to its kind by check "
    "constraint.",
    "Replaced wholesale on every save; cascades with its Dynamic Role; role, "
    "person, and group targets are protected.",
]

MODEL_SNAPSHOT_DELETION = (
    "Cascades with the stage instance. Replaced when a reversal rolls the stage "
    "back and it runs again on the same attempt."
)

MODEL_ACTION = (
    "One recorded decision or reversal. One live vote per actor and attempt; a "
    "reversed vote is not live."
)

API_TEMPLATE_DETAIL = (
    "Template detail with active stages, routes, dynamic rules, and each stage's "
    "named Dynamic Role with its rules."
)

API_PREVIEW = (
    "Resolve approvers for a configuration, or a saved Dynamic Role by code, and a "
    "sample requester from the caller's tenant, without saving."
)

API_DYNAMIC_ROLES = [
    ["GET, POST /dynamic-roles/",
     "List Dynamic Roles, filterable by active state, search, and document type, "
     "and create one with its rules."],
    ["GET, PATCH, DELETE /dynamic-roles/{id}/",
     "Read, edit, or remove one. Rules are written as a whole ordered list; one "
     "that any stage names cannot be deleted."],
    ["GET /dynamic-roles/fields/",
     "The fields a Dynamic Role serving the given document types may test, the "
     "document types it can serve, and the approving roles a rule may send to."],
    ["POST /dynamic-roles/preview/",
     "Try unsaved rules for a requester in the caller's tenant and a sample "
     "document, with the checks saving runs."],
]

API_REVERSE = "Administrator reverses a decision, which the document's own module may refuse."

ERROR_REQUESTER = (
    "The requester tried to approve a document whose handler does not permit "
    "self-approval."
)

ERROR_REVERSAL = [
    "REVERSAL_NOT_ALLOWED",
    "The reversal has no reason, targets a reversal or an already reversed vote, "
    "finds the instance rejected, withdrawn, or cancelled or its stage run again "
    "since, has no registered handler, or the owning module refuses it.",
    "422",
]

ERROR_DYNAMIC_ROLE_IN_USE = [
    "DYNAMIC_ROLE_IN_USE",
    "Deleting a Dynamic Role a stage still names.",
    "409",
]

DEP_MODULE_4 = (
    "Supplies role definitions and assignments. The engine reads active "
    "assignments per tenant and never writes them. A role permission change is "
    "itself a document routed here: its rbac.role_change handler applies the change "
    "on approval, and it is the one type that lets the requester decide their own."
)

DEP_CONSUMERS = (
    "Register document handlers and consume the approval outcome. Each handler "
    "also says whether a decision may still be reversed and puts its document back "
    "after one: payouts refuse once an instruction is claimed for the provider, "
    "finance once the document has posted, procurement once a purchase order's "
    "vendor email is pending or sent. Procurement's central ladder is published by "
    "this engine's publish service."
)

DEP_SEEDING = (
    "seed_workflow_permissions registers the seven workflow permission keys and "
    "grants them to the platform administrator roles. It also attaches the school "
    "defaults to the prebuilt role library, which every school created afterwards "
    "is provisioned from, and grants the same keys to the school roles that "
    "already exist, adding only: School Admin holds every workflow key except "
    "reversal, Finance Admin and Procurement Admin read templates, groups, and "
    "instances, and Branch Admin reads instances. A key a school denied stays "
    "denied."
)

DEP_DOMAIN_APPS = [
    "Domain apps, including the school staff app",
    "Declare on their handlers the fields of their own document types a Dynamic "
    "Role may test, and register requester facts only they can read through "
    "workflow_conditions autodiscovery, since the engine may not import them. The "
    "school staff app supplies job title and contract type, and leave requests "
    "declare leave type and days.",
]

NEEDS_LEAD = (
    "These are current risks and gaps, not history. The controlled-spend hole v1.0 "
    "recorded here is closed, and so is the unsafe default that could reopen it. "
    "What is left is the release the submitter holds, the one document type whose "
    "requester may decide it, approvals that can still be reversed after their "
    "document has posted, and the operational gaps below."
)

GAPS = [
    "FURTHER GAPS",
    "• Continue-without-approval remains a submitter-owned release for handlers "
    "that permit it. Payouts and role changes forbid it, but the generic default "
    "is permissive and other document types still rely on audit rather than a "
    "second reviewer. The same holds for confirming past an unconfigured route.",
    "• A role change may be decided by the person who raised it. The stage completes "
    "on any one approval and the requester is eligible, so even at a school with a "
    "second administrator the requester can approve their own restricted grant "
    "alone, with nobody else looking. The ladder records who approved, and Module "
    "4's audit marks the change as self-approved.",
    "• Reversal is guarded only where the owning module implements the check, and "
    "the base handler allows every reversal. Procurement guards purchase orders "
    "alone, so a vendor invoice or vendor payment that was approved and then "
    "posted can have its approval reversed: its approval state returns to pending "
    "while the posted document and its journal stand.",
    "• The parked-document repair runs on read, not on a schedule, so a parked "
    "document nobody opens stays parked.",
    "• No timeouts, escalation timers, or reminders for a stage that has been "
    "waiting.",
    "• No template versioning. A republish changes the definition future "
    "activations use.",
    "• Six of the ten defined notification event keys are not wired.",
    "• Route graphs are not checked for cycles at publish.",
    "• Route, inclusion, and stage-owned rule conditions are not validated against "
    "the document type. A named Dynamic Role's conditions are.",
]

TRACE_ROWS = {
    "Finance workflow handlers": "FR-016, FR-019",
    "Procurement workflow handlers": "FR-009, FR-016, FR-019",
    "Payment and payout workflow handlers": "FR-016, FR-019",
    "Approver preview and rule tracing": "FR-020, FR-032",
}

TRACE_NEW_ROW = [
    "Named Dynamic Roles reading the document and who raised it",
    "FR-032, FR-007, FR-020",
]

TRACEABILITY_LEAD = (
    f"Module 7 carries 27 capability entries in MRD v{MRD_VERSION}. Each maps to the "
    "requirements below; the named Dynamic Role entry is new in that version."
)

CHANGE_SUMMARY = (
    "Records a reversal that asks before it undoes, named Dynamic Roles, and four "
    "smaller engine changes. Reversing an approval rewrote the engine's rows and "
    "stopped: it never asked the module owning the document whether the decision "
    "could still be undone, so a payout batch the provider had already paid could "
    "read as in progress, and it never unwound the ladder, so a reversed approver "
    "could not vote again and the stages after the reopened one kept their "
    "approved rows and left the instance stuck. reverse_action now asks the handler "
    "first and tells it afterwards in one transaction, reopens only a stage the "
    "reversed vote decided, rolls every later stage back to pending, and replaces "
    "rather than doubles a re-entered stage's snapshot; payouts, finance, purchase "
    "orders, user creation, and role changes refuse once their decision has been "
    "acted on. A template whose only steps were retired read as configured and "
    "was routed past each retired step to an approved outcome; submission and the "
    "finance direct-post gate now count live steps only. A tenant can build named "
    "Dynamic Roles, ordered rules reading the document and the person who raised "
    "it, and pick them by name in any stage of its own templates, with every field, "
    "value, and target checked on save. Role changes are decided by a ladder whose "
    "requester may decide their own request, the only document type with that "
    "exemption. "
    "The approver preview looks its sample requester up inside the caller's "
    "tenant, where it had answered with another school's approvers, a shared "
    "template's Dynamic Role step records its rule's key instead of failing "
    "activation, and a school's School Admin is provisioned with the workflow "
    "manage keys. FR-004, FR-005, FR-007, FR-009, FR-011, FR-012, FR-014, FR-019, "
    "FR-020, FR-021, FR-025, and FR-031 are restated, FR-032 is added, and "
    "sections 1, 3, 5, 6, 7, 8, 9, and 10 are updated. Two stale statements are "
    "corrected: FR-009 said failing closed was still open, which v1.5 closed, and "
    "the traceability lead counted one entry fewer than its table; the FR-015 "
    "and FR-031 headers and the Needs Attention box regain the styling the rest "
    "of the document uses, and a requirement's header row no longer strands at "
    "the foot of a page. Module 7 remains Backend Complete and In use Complete, with "
    "twenty-seven capability entries in MRD v2.76. Verified by ReverseActionTests, "
    "ReversalUnwindTests, StagelessTemplateSubmissionTests, the seven Dynamic Role "
    "test classes, RoleChangeLadderTests, PreviewRequesterScopeTests, and "
    "SchoolWorkflowRoleDefaultsTests, and by the reversal tests in payments, "
    "finance, procurement, identity, and leave; the vs_workflow suite ran 314 "
    "tests OK at 6611856, a count that predates the preview, activation-audit, and "
    "Dynamic Role changes. Backend evidence only; nothing here is deployed."
)


# ── structural edits ─────────────────────────────────────────────────────────

def rebuild_gaps(cell, style_cell, lines: list[str]) -> None:
    """Rewrite the Needs Attention box as a heading paragraph over its bullets.

    The paragraphs are taken from ``style_cell``, the same box in an earlier
    version, so the heading keeps its bold amber run and each bullet its own
    paragraph. Line ``i`` reuses paragraph ``i``; bullets beyond what the source
    carried clone its last bullet, never the heading.
    """
    source = [copy.deepcopy(p._p) for p in style_cell.paragraphs]
    if len(source) < 2:
        raise ValueError("Style source must carry a heading and at least one bullet")
    for paragraph in list(cell.paragraphs):
        paragraph._p.getparent().remove(paragraph._p)
    tc = cell._tc
    tc.append(source[0])
    bullets = source[1:]
    for index in range(len(lines) - 1):
        tc.append(bullets[index] if index < len(bullets) else copy.deepcopy(bullets[-1]))
    for paragraph, line in zip(cell.paragraphs, lines):
        set_run_text(paragraph, line)
    if len(cell.paragraphs) != len(lines):
        raise ValueError("Needs Attention box does not hold one paragraph per line")


def strip_bookmarks(element) -> None:
    for tag in ("w:bookmarkStart", "w:bookmarkEnd"):
        for node in element.findall(".//" + qn(tag)):
            node.getparent().remove(node)


def body_neighbour(doc, element, offset: int):
    children = list(doc.element.body.iterchildren())
    return children[children.index(element) + offset]


def restore_header_colour(target_table, style_table) -> None:
    """Give a requirement header the run formatting of a correctly styled one."""
    target = target_table.rows[0].cells[0].paragraphs[0].runs[0]._r
    style = style_table.rows[0].cells[0].paragraphs[0].runs[0]._r
    if style.rPr is None:
        raise ValueError("Style header carries no run properties")
    properties = copy.deepcopy(style.rPr)
    if target.rPr is not None:
        target.remove(target.rPr)
    target.insert(0, properties)


def normalise_requirement_headers(doc, style_table) -> None:
    """Style every requirement header by the state it reports, and keep it with its body.

    A header's shading and colour are its status, so a block restated to
    Implemented has to change colour with its words, and one written with a
    fresh run loses the colour altogether. Each header whose status reads
    exactly Implemented takes ``style_table``'s fill and run. Every header row
    also keeps with the row below it, so a page never ends on a requirement's
    header with its body on the next page.
    """
    style_shading = style_table.rows[0].cells[0]._tc.tcPr.find(qn("w:shd"))
    if style_shading is None:
        raise ValueError("Style header carries no shading")
    for table in doc.tables:
        header = table.rows[0].cells[0]
        text = header.text.strip()
        if not re.match(r"FR-\d{3} \| ", text):
            continue
        for paragraph in header.paragraphs:
            paragraph.paragraph_format.keep_with_next = True
        if text.split("|", 1)[1].strip() != "Implemented":
            continue
        shading = header._tc.tcPr.find(qn("w:shd"))
        if shading is None:
            header._tc.tcPr.append(copy.deepcopy(style_shading))
        else:
            shading.set(qn("w:fill"), style_shading.get(qn("w:fill")))
        restore_header_colour(table, style_table)


def add_requirement(doc, *, template_table, after_table, spacer, heading: str,
                    header: str, values: dict) -> Table:
    """Clone a requirement block and place it after ``after_table``.

    ``template_table`` must already be in the state the new block reports, since
    the header's shading and colour are the status. Its heading paragraph comes
    with it, bookmarks removed so the copy does not claim the original's anchor.
    ``spacer`` is the plain empty paragraph that separates requirement blocks; it
    is checked to carry no page break before it is copied.
    """
    heading_element = body_neighbour(doc, template_table._tbl, -1)
    if not Paragraph(heading_element, doc).style.name.startswith("Heading"):
        raise ValueError("No heading directly above the template requirement")
    if Paragraph(spacer, doc).text.strip() or 'w:type="page"' in spacer.xml:
        raise ValueError("Spacer is not a plain empty paragraph")

    new_spacer = copy.deepcopy(spacer)
    new_heading = copy.deepcopy(heading_element)
    strip_bookmarks(new_heading)
    new_table = copy.deepcopy(template_table._tbl)
    after_table._tbl.addnext(new_table)
    after_table._tbl.addnext(new_heading)
    after_table._tbl.addnext(new_spacer)

    set_run_text(Paragraph(new_heading, doc), heading)
    table = Table(new_table, after_table._parent)
    set_run_text(table.rows[0].cells[0].paragraphs[0], header)
    for row_index, text in values.items():
        replace_cell(table.rows[row_index].cells[1], text, size=FR_SIZE)
    return table


def patch_m07(source: Path, style_source: Path, output: Path) -> None:
    doc = Document(str(source))
    style_doc = Document(str(style_source))
    title = f"XVS M07 Workflow and Approval Engine Functional Requirements Document v{TARGET}"

    # Bind every table before anything is inserted.
    t = doc.tables
    cover, control = t[0], t[1]
    scope = require_header(t[3], "Area")
    actors = require_header(t[6], "Actor")
    gates = require_header(t[7], "Surface")
    rules = require_header(t[8], "Rule")
    fr004 = require_fr(t[12], "FR-004")
    fr005 = require_fr(t[13], "FR-005")
    fr007 = require_fr(t[15], "FR-007")
    fr009 = require_fr(t[17], "FR-009")
    fr011 = require_fr(t[19], "FR-011")
    fr012 = require_fr(t[20], "FR-012")
    fr014 = require_fr(t[22], "FR-014")
    fr019 = require_fr(t[27], "FR-019")
    fr020 = require_fr(t[28], "FR-020")
    fr021 = require_fr(t[29], "FR-021")
    fr025 = require_fr(t[33], "FR-025")
    fr030 = require_fr(t[38], "FR-030")
    fr031 = require_fr(t[39], "FR-031")
    lifecycle = require_header(t[40], "State")
    order = require_header(t[41], "Step")
    model = require_header(t[42], "Model")
    api_config = require_header(t[43], "Method and path", row_one="GET /templates/")
    api_instances = require_header(t[44], "Method and path", row_one="GET /instances/")
    errors = require_header(t[45], "Code")
    dependencies = require_header(t[46], "Dependency")
    gaps = require_header(t[47], "FURTHER GAPS")
    trace = require_header(t[48], "MRD capability")
    change_log = require_header(t[49], "Version")
    style_gaps = require_header(style_doc.tables[46], "FURTHER GAPS")

    # Cover and document control.
    replace_cover_version(cover, TARGET)
    replace_control_value(control, "Version", TARGET)
    replace_control_value(control, "Review date", REVIEW_DATE)
    replace_control_value(control, "Code baseline", CODE_BASELINE)
    replace_control_value(
        control, "Source MRD", f"XVS Module Requirements Document v{MRD_VERSION} | Module 7",
    )
    replace_control_value(control, "Supporting apps", SUPPORTING_APPS)

    # Section 1: scope.
    rewrite_cell(find_row(scope, "Approver resolution").cells[1], SCOPE_RESOLUTION)
    rewrite_cell(find_row(scope, "Decision lifecycle").cells[1], SCOPE_LIFECYCLE)

    # Section 3: actors, gates, ownership rules.
    rewrite_cell(find_row(actors, "Tenant administrator").cells[1], ACTOR_TENANT_ADMIN)
    rewrite_cell(find_row(actors, "Requester").cells[1], ACTOR_REQUESTER)
    rewrite_cell(find_row(actors, "Workflow administrator").cells[1], ACTOR_WORKFLOW_ADMIN)
    rewrite_cell(find_row(gates, "Approver preview").cells[2], GATE_PREVIEW_SCOPE)
    insert_row_before(gates, "Instance list and detail", GATE_DYNAMIC_ROLES)
    self_approval = find_row(rules, "The requester is never an approver")
    rewrite_cell(self_approval.cells[0], RULE_SELF_APPROVAL[0])
    rewrite_cell(self_approval.cells[1], RULE_SELF_APPROVAL[1])
    append_row(rules, RULE_REVERSAL)

    # Section 4: requirements.
    set_fr(fr004, LIMIT_ROW, "Current limit", FR004_LIMIT)
    set_fr(fr005, EVIDENCE_ROW, "Current evidence", FR005_EVIDENCE)
    set_fr(fr007, EVIDENCE_ROW, "Current evidence", FR007_EVIDENCE)
    set_fr(fr007, ACCEPTANCE_ROW, "Acceptance", FR007_ACCEPTANCE)
    set_fr(fr007, LIMIT_ROW, "Current limit", FR007_LIMIT)
    set_fr(fr009, ACCEPTANCE_ROW, "Acceptance", FR009_ACCEPTANCE)
    set_fr(fr009, LIMIT_ROW, "Current limit", FR009_LIMIT)
    set_fr(fr011, EVIDENCE_ROW, "Current evidence", FR011_EVIDENCE)
    set_fr(fr011, ACCEPTANCE_ROW, "Acceptance", FR011_ACCEPTANCE)
    set_fr(fr012, REQUIREMENT_ROW, "Requirement", FR012_REQUIREMENT)
    set_fr(fr012, EVIDENCE_ROW, "Current evidence", FR012_EVIDENCE)
    set_fr(fr012, ACCEPTANCE_ROW, "Acceptance", FR012_ACCEPTANCE)
    set_fr(fr012, LIMIT_ROW, "Current limit", FR012_LIMIT)
    set_fr(fr014, ACCEPTANCE_ROW, "Acceptance", FR014_ACCEPTANCE)
    set_fr(fr019, REQUIREMENT_ROW, "Requirement", FR019_REQUIREMENT)
    set_fr(fr019, EVIDENCE_ROW, "Current evidence", FR019_EVIDENCE)
    set_fr(fr019, ACCEPTANCE_ROW, "Acceptance", FR019_ACCEPTANCE)
    set_fr(fr019, LIMIT_ROW, "Current limit", FR019_LIMIT)
    set_fr(fr020, EVIDENCE_ROW, "Current evidence", FR020_EVIDENCE)
    set_fr(fr020, ACCEPTANCE_ROW, "Acceptance", FR020_ACCEPTANCE)
    set_fr(fr020, LIMIT_ROW, "Current limit", FR020_LIMIT)
    set_fr(fr021, ACCEPTANCE_ROW, "Acceptance", FR021_ACCEPTANCE)
    set_fr(fr021, LIMIT_ROW, "Current limit", FR021_LIMIT)
    set_fr(fr025, EVIDENCE_ROW, "Current evidence", FR025_EVIDENCE)
    set_fr(fr031, EVIDENCE_ROW, "Current evidence", FR031_EVIDENCE)
    set_fr(fr031, ACCEPTANCE_ROW, "Acceptance", FR031_ACCEPTANCE)
    restore_header_colour(fr031, fr030)

    # FR-031 sits glued to FR-030; every other requirement is separated from the
    # one above it by the same plain spacer that follows FR-031.
    spacer = body_neighbour(doc, fr031._tbl, 1)
    fr031_heading = body_neighbour(doc, fr031._tbl, -1)
    if body_neighbour(doc, fr031_heading, -1) is not fr030._tbl:
        raise ValueError("FR-031 heading no longer follows FR-030 directly")
    fr031_heading.addprevious(copy.deepcopy(spacer))

    # Section 5: lifecycle, activation, resolution order.
    rewrite_cell(find_row(lifecycle, "RETURNED").cells[2], LIFECYCLE_RETURNED)
    rewrite_cell(find_row(lifecycle, "APPROVED").cells[2], LIFECYCLE_APPROVED)
    replace_body_paragraph(doc, "Activation is the moment", ACTIVATION_PARAGRAPH)
    rewrite_cell(find_row(order, "2").cells[1], ORDER_SOURCE)
    rewrite_cell(find_row(order, "3").cells[1], ORDER_REQUESTER)

    # Section 6: data model.
    stage_row = find_row(model, "WorkflowStage")
    rewrite_cell(stage_row.cells[1], MODEL_STAGE[0])
    rewrite_cell(stage_row.cells[2], MODEL_STAGE[1])
    stage_rule_row = find_row(model, "WorkflowStageDynamicRule")
    rewrite_cell(stage_rule_row.cells[1], MODEL_STAGE_RULE[0])
    rewrite_cell(stage_rule_row.cells[2], MODEL_STAGE_RULE[1])
    insert_row_before(model, "WorkflowStageApproverOverride", MODEL_DYNAMIC_ROLE)
    insert_row_before(model, "WorkflowStageApproverOverride", MODEL_DYNAMIC_ROLE_RULE)
    rewrite_cell(find_row(model, "WorkflowStageApprover").cells[2], MODEL_SNAPSHOT_DELETION)
    rewrite_cell(find_row(model, "WorkflowStageAction").cells[1], MODEL_ACTION)

    # Section 7: routes and typed errors.
    rewrite_cell(find_row(api_config, "GET /templates/{id}/").cells[1], API_TEMPLATE_DETAIL)
    rewrite_cell(
        find_row(api_config, "POST /templates/preview-approvers/").cells[1], API_PREVIEW,
    )
    for values in API_DYNAMIC_ROLES:
        insert_row_before(api_config, "GET, POST /stage-approvers/", values)
    rewrite_cell(
        find_row(api_instances, "POST /actions/{action_id}/reverse/").cells[1], API_REVERSE,
    )
    rewrite_cell(find_row(errors, "REQUESTER_CANNOT_APPROVE").cells[1], ERROR_REQUESTER)
    append_row(errors, ERROR_REVERSAL)
    append_row(errors, ERROR_DYNAMIC_ROLE_IN_USE)

    # Section 8: dependencies.
    rewrite_cell(find_row(dependencies, "Module 4, Roles & Permissions").cells[1], DEP_MODULE_4)
    rewrite_cell(
        find_row(dependencies, "Modules 17 to 24, finance and procurement").cells[1],
        DEP_CONSUMERS,
    )
    rewrite_cell(find_row(dependencies, "Seeding").cells[1], DEP_SEEDING)
    append_row(dependencies, DEP_DOMAIN_APPS)

    # Section 9: needs attention.
    replace_body_paragraph(doc, "These are current risks and gaps", NEEDS_LEAD)
    rebuild_gaps(gaps.rows[0].cells[0], style_gaps.rows[0].cells[0], GAPS)

    # Section 10: traceability.
    for capability, requirements in TRACE_ROWS.items():
        rewrite_cell(find_row(trace, capability).cells[1], requirements)
    append_row(trace, TRACE_NEW_ROW)
    replace_body_paragraph(doc, "Module 7 carries", TRACEABILITY_LEAD)

    # FR-032, cloned from FR-030 because that header carries the Implemented styling.
    add_requirement(
        doc, template_table=fr030, after_table=fr031, spacer=spacer,
        heading=FR032_HEADING, header=FR032_HEADER, values=FR032,
    )

    normalise_requirement_headers(doc, fr030)
    prepend_change_log(change_log, TARGET, CHANGE_SUMMARY)
    finish(doc, output, title, TARGET)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", default=Path(__file__).resolve().parents[1])
    args = parser.parse_args()
    directory = Path(args.root) / "functional-requirements" / M07_DIR
    patch_m07(
        directory / f"{M07_STEM}_v{SOURCE}.docx",
        directory / f"{M07_STEM}_v{GAPS_STYLE_SOURCE}.docx",
        directory / f"{M07_STEM}_v{TARGET}.docx",
    )
    print(f"Wrote {M07_DIR} v{TARGET}")


if __name__ == "__main__":
    main()

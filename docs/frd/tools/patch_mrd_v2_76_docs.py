#!/usr/bin/env python3
"""Cut MRD v2.76: the tracker reconciled to the module FRDs versioned at 13675db.

Every module FRD whose behaviour moved since its last version is brought up to
the backend at commit 13675db, and this revision carries what moved into the
tracker: capability entries added or restated, Needs Attention and Current
Decision boxes rewritten as current state, per-module counts, the capability
total, the priority gaps and build order where a gap closed or changed shape,
the capability-delta section and the change log.

The content constants below are filled from the module revisions, and the
script refuses to run while any of them is still empty, so a placeholder can
never reach a published document.

    python tools/patch_mrd_v2_76_docs.py
"""

from __future__ import annotations

import argparse
import copy
from pathlib import Path

from docx import Document
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.table import Table
from docx.text.paragraph import Paragraph

from generate_requirements_documents import (
    BLUE,
    assert_no_em_dash,
    rebuild_table,
    shrink_inherited_media,
    update_extended_title,
    write_cell,
)


MRD_SOURCE, MRD_TARGET = "2.75", "2.76"
REVIEW_DATE, SHORT_DATE = "11 September 2026", "11 Sep 2026"

#: Table positions in v2.75. Bound once, before any edit, because an inserted
#: table would shift every later index.
COVER, CONTROL, CONTENTS, MODULE_INDEX = 0, 1, 2, 5
PRIORITY_GAPS, DELTA, BUILD_ORDER, CHANGE_LOG = 75, 76, 77, 78

#: Each module's capability grid. Its last row is the module's Needs Attention
#: or Current Decision box.
CAPABILITIES = {
    1: 8, 2: 10, 3: 12, 4: 14, 5: 16, 6: 18, 7: 20, 8: 23, 9: 25, 10: 27,
    11: 30, 12: 32, 13: 34, 14: 36, 15: 38, 16: 40, 17: 43, 18: 45, 19: 47,
    20: 49, 21: 52, 22: 54, 23: 56, 24: 58, 25: 61, 26: 63, 27: 65, 28: 67,
    29: 70, 30: 72, 31: 74,
}

DELTA_WIDTHS = [1.85, 1.35, 3.85]


# ── content, filled from the module revisions ────────────────────────────────

MRD_SOURCE_SCOPE = (
    "Every module document reconciled to the backend at 13675db: the role-change ladder, "
    "named Dynamic Roles, the roles CodeX ships with a new school, plan depth enforced "
    "everywhere, ID-card sign-in, and branch narrowing across finance, procurement and "
    "academics (11 September 2026)"
)

MRD_CONTENTS_NOTE = (
    "Seven capability entries, one status move, and the gaps four modules were not carrying"
)

MRD_INTRO = (
    "This revision brings the tracker level with the module documents, every one of which was "
    "read against the backend at 13675db. Seven capability entries are added across six "
    "modules, Staff Management moves to In use Partial because three modules now read the "
    "staff record, and four modules record a gap they were not carrying: an approval reversal "
    "the module owning the document is never asked about, a role change its own requester may "
    "decide, stock readable across branches when no location is named, and a fee that falls "
    "due on a different day depending on the route that raised it."
)

MRD_CHANGE_SUMMARY = (
    "Brings the tracker level with the module documents, twenty-two of which were read against "
    "the backend at commit 13675db and revised. Seven capability entries are added, taking the "
    "platform from 493 to 500 across 31 modules: the roles CodeX ships with every school and "
    "the grants a lower plan tier takes back (Module 1), platform staff ID-card sign-in by a "
    "random, revocable card key (Module 3), the refusal to retire the last way back into role "
    "administration (Module 4), named Dynamic Roles whose rules read the document and who "
    "raised it (Module 7), a school's fee due-date policy (Module 17), and the refusal to "
    "reverse a payout approval once an instruction has reached the provider (Module 18). Staff "
    "Management moves to In use Partial: school creation writes a staff record for every "
    "administrator, a class points at one for its teacher, and the workflow engine reads its "
    "facts, while Timetable still points at the account. A role change is decided by a ladder "
    "rather than by a grant ceiling; the plan gate ships enforced in every environment and a "
    "downgrade takes back the grants the new tier does not reach; and reversing an approval "
    "asks the module that owns the document whether it has been acted on. Four modules record "
    "a gap they were not carrying: a role change may be decided by the person who raised it "
    "even where a second administrator could decide it (Modules 4 and 7), an approved vendor "
    "invoice or vendor payment that has posted can still have its approval reversed (Modules 7 "
    "and 23), stock balances and movements are readable across branches when no location is "
    "named (Module 24), and a term's fees fall due on a different day depending on the route "
    "that raised them (Module 17). Module 29's tracker is recorded as CodeX's own, its keys "
    "platform-scoped and off a school's roles screen. Backend evidence only: nothing here "
    "claims frontend delivery, deployment or production adoption."
)

MRD_DELTA_ROWS = [
    ["Module 1 capabilities", "21 to 23",
     "The roles CodeX ships with every school, and the grants a lower plan tier takes back, "
     "become entries of their own. Both shipped earlier and no capability described them."],
    ["Module 3 capabilities", "17 to 18",
     "Platform staff ID-card sign-in by a random, revocable card key. The card carries a key "
     "held only by platform staff rather than an address, the preview and a card sign-in "
     "resolve it inside the platform tenant alone, and rotation kills the old card."],
    ["Module 4 capabilities", "21 to 22",
     "Refusing to retire the last way back into role administration. It is a rule a school "
     "meets on the roles screen, with a refusal of its own, as the reserved approval-role keys "
     "are."],
    ["Module 7 capabilities", "26 to 27",
     "Named Dynamic Roles, whose rules read the document and who raised it, become an entry of "
     "their own rather than sitting inside document-driven approvers."],
    ["Module 17 capabilities", "19 to 20",
     "A school's fee due-date policy decides when a term's fees fall due. Cohort billing "
     "applies it; the module's own generation route does not, which the module records as a "
     "gap."],
    ["Module 18 capabilities", "23 to 24",
     "A payout approval can no longer be reversed once an instruction has been claimed for the "
     "provider, because the money may already have moved."],
    ["Module 12 status", "In use Not started to Partial",
     "Three modules now read the staff record: school creation writes one for every "
     "administrator, a class points at it for its teacher, and the workflow engine's Dynamic "
     "Roles read its facts. Module 14 still points at the account, so this is Partial rather "
     "than Complete. Backend stays Partial."],
    ["Role permission changes", "Decided by a ladder, not by a grant ceiling",
     "A restricted permission added to a role the editor holds is raised as a request rather "
     "than saved, and one School Admin stage decides it. The requester stays eligible, so a "
     "school with one administrator can still make the change and it is recorded as "
     "self-approved."],
    ["Action reversal", "Confirmed by the module that owns the document",
     "Reversing an approval asks the owning module whether the document has been acted on. "
     "Finance answers for posted documents and procurement for a released order; vendor "
     "invoices and vendor payments are not asked, which Modules 7 and 23 now record."],
    ["Plan depth", "Enforced in every environment",
     "The configuration catalogue ships the enforcement switch on, so an environment built "
     "from it enforces the plan as the running ones do, and moving a school down a tier takes "
     "back the role grants the new tier no longer reaches."],
    ["Module 29 scope", "CodeX's own tracker, off a school's roles screen",
     "todo.task.view, manage and assign are platform-scoped: one accountable item owned by "
     "exactly one CodeX staff member. A school was being offered keys that governed nothing it "
     "has."],
    ["Module FRDs", "Twenty-two revised",
     "M01 v1.23, M03 v1.12, M04 v1.18, M05 v1.3, M06 v1.1, M07 v1.9, M08 v1.10, M09 v2.11, "
     "M10 v1.3, M11 v2.9, M12 v2.3, M13 v2.10, M14 v3.3, M17 v1.5, M18 v1.10, M19 v1.9, "
     "M20 v1.4, M21 v1.4, M22 v1.8, M23 v1.9, M24 v1.2 and M31 v1.6. Module 29 has no FRD."],
]

#: Module number -> changes. Keys, all optional:
#:   add        [(anchor bullet text, new bullet text)] - the new bullet is
#:              cloned into the cell holding the anchor, so it keeps the grid's
#:              run formatting.
#:   restate    [(current bullet text, new bullet text)]
#:   attention  [heading, bullet, ...] - rewrites the box in the grid's last
#:              row, reusing its paragraphs positionally.
#:   blurb      the module's description paragraph.
#:   count      the module's capability entries in the module index.
#:   status     {"Phase" | "Backend" | "In use" | "Code": value} - the status
#:              line above the grid; Backend and In use also move the module
#:              index, so the two never disagree.
#:   box_edits  [("replace", start of a line, new line) | ("append", new line)]
#:              - edits the box line by line, so unchanged bullets are carried
#:              rather than retyped.
#:   blurb_edits [(old text, new text)] - a substring of the description,
#:              which must occur exactly once.
MODULE_CHANGES: dict[int, dict] = {
    1: {
        "count": 23,
        "add": [
            ("▸  A school's plan, read and changed from the console",
             "▸  The roles CodeX ships, provisioned with every school"),
            ("▸  The roles CodeX ships, provisioned with every school",
             "▸  A plan downgrade takes back the role grants the new tier does not reach"),
        ],
        "blurb_edits": [(
            "administrators or branches a school may have. ",
            "administrators or branches a school may have. A new school receives the roles "
            "CodeX ships, School Admin, Teacher, Finance Admin and Procurement Admin for the "
            "whole school and a Branch Admin for each branch, and every administrator is given "
            "a staff record; a branch role names its branch only once the school has two. "
            "Moving a school down a plan tier takes back the role grants the new tier no longer "
            "reaches. The crest alone is public by the school's address, for the sign-in page, "
            "the console and the email header; every other read of a school's files stays "
            "signed for one reader. ",
        )],
        "box_edits": [
            ("replace", "• School lifecycle is still incomplete.",
             "• School lifecycle is still incomplete. Implemented School and onboarding status "
             "transitions close active impersonation sessions transactionally, session start "
             "takes the same Tenant lock, and platform staff can take a live school out of "
             "service and back through the service switch. Suspension and reactivation for "
             "commercial reasons and recoverable soft delete remain missing, and ordinary "
             "login-session revocation is still undefined."),
            ("replace", "• A plan no longer caps a school's size.",
             "• A plan no longer caps a school's size. The student, teacher and administrator "
             "ceilings were stored and read by nothing, and the branch ceiling was enforced on "
             "both creation paths, so the cheapest tier refused a proprietor a fourth branch "
             "they had planned for months. All four are removed and size is priced rather than "
             "fenced, which leaves the depth wall as the only thing that moves a school up a "
             "tier."),
        ],
    },
    3: {
        "count": 18,
        "add": [(
            "▸  Tenant and role-grant boundaries enforced on account creation",
            "▸  Platform staff ID-card sign-in by a random, revocable card key",
        )],
        "blurb_edits": [(
            "as the dedicated assignment endpoints.",
            "as the dedicated assignment endpoints. Platform staff may sign in with the random, "
            "revocable key printed on their ID card in place of an address; every other key is "
            "refused with one identical answer. The account list defaults to the caller's own "
            "tenant, so a CodeX screen asks explicitly for a school's accounts, while acting on "
            "one account by id keeps a platform operator's reach across every tenant.",
        )],
    },
    4: {
        "count": 22,
        "add": [(
            "▸  A role builder that offers only what the plan allows",
            "▸  A school cannot retire the last way back into role administration",
        )],
        "box_edits": [
            ("replace", "• No material capability gap was identified",
             "• A role change is decided by a ladder rather than by a grant ceiling. A "
             "restricted permission added to a role the editor holds is raised as a request in "
             "the same transaction, one School Admin stage decides it, and the engine's "
             "approval applies the delta. The requester stays eligible, which is deliberate: in "
             "most schools the person who administers roles is the only one who could approve "
             "the change, and the decision is recorded as self-approved. The stage completes on "
             "one approval even where a school has a second administrator, so a second pair of "
             "eyes depends on the school's own practice rather than on the ladder."),
            ("replace", "• A role granted for the whole school now reaches the whole school.",
             "• A role granted for the whole school reaches the whole school. The branch "
             "narrowing and the permission gate are resolved from the same grants, so a Finance "
             "Officer for the whole school is no longer narrowed to the one branch their staff "
             "record happens to name while the gate lets them act at the others. Writes follow "
             "reads: what they raise without naming a branch is filed school-wide."),
            ("append",
             "• A school cannot lock itself out of role administration, and is not offered what "
             "its plan does not reach. Retiring the last role that can administer roles is "
             "refused with a reason of its own; the role builder lists only the permissions the "
             "school's plan reaches, and moving down a tier takes back the grants the new tier "
             "does not."),
        ],
    },
    5: {
        "attention": [
            "CURRENT DECISION",
            "• No material capability gap was identified within this module's stated backend "
            "scope.",
            "• The trail is confined to its reader's tenant and narrowable to one customer "
            "within it. Every writing module records the tenant, by passing it or by inheriting "
            "the tenant the request asserted, and events that belong to no customer stay "
            "reachable rather than hidden. A tenant's own audit officer reads only that "
            "tenant's rows on every surface, and the tenant roster on the filter drawer is "
            "served only to platform callers, so it cannot become a customer list.",
        ],
    },
    6: {
        "box_edits": [
            ("replace", "• Module 6 remains Backend Complete",
             "• Module 6 remains Backend Complete and In use Complete with twenty-six "
             "capability entries, reconciled to M06 FRD v1.1."),
            ("replace", "• Configuration is reserved to the platform tenant.",
             "• Configuration is reserved to the platform tenant. All nineteen config.* keys "
             "are PLATFORM-scoped, which the RBAC grant guard enforces independently of how a "
             "school composes its roles, and the enforcement switch the plan gate reads is "
             "platform-scoped for the same reason. The catalogue ships that switch on, so an "
             "environment built from it enforces the plan as the running ones do, while a "
             "database with no catalogue at all still reads the gate's own default and does not "
             "refuse a paying school. The one school-facing route carries no permission key at "
             "all and is read-only."),
        ],
    },
    7: {
        "count": 27,
        "add": [(
            "▸  Tenant-composed approver groups, with no role provisioned to fill them",
            "▸  Named Dynamic Roles reading the document and who raised it",
        )],
        "status": {
            "Code": "vs_workflow; finance, procurement, payments, role-change, "
                    "user-creation and leave handlers",
        },
        "blurb": (
            "Reusable multi-stage approvals with role, approver-group, named Dynamic Role, "
            "document-driven and organogram approvers, branching rules, delegations, actions, "
            "and operational queues. A stage nobody can approve parks rather than "
            "auto-skipping, by default and not only where a seed remembered to say so, and "
            "reversing a recorded decision first asks the module that owns the document "
            "whether it has been acted on."
        ),
        "attention": [
            "NEEDS ATTENTION",
            "• An approval route with no live steps is refused rather than treated as "
            "approval. A tenant that has not built its ladder resolves to the shared platform "
            "row, which carries no live steps, and a ladder whose every step is retired counts "
            "as having none. Submission raises a named refusal that the submitter may confirm "
            "past, recorded against whoever gives it.",
            "• Continue-without-approval remains available only where a document handler "
            "permits it. Payouts and role changes forbid it; permitted document types still "
            "rely on audit rather than a second reviewer.",
            "• A role change may be decided by the person who raised it, the one document type "
            "with that exemption, and its stage completes on any one approval. An administrator "
            "can therefore give their own role a restricted permission with nobody else "
            "looking, even where the school has a second administrator; the ladder and "
            "Module 4's audit record that they did.",
            "• Reversal is guarded only where the owning module implements the check. "
            "Procurement guards purchase orders alone, so an approved vendor invoice or vendor "
            "payment that has already posted can still have its approval reversed, leaving a "
            "posted document that reads as awaiting approval.",
            "• Timeouts, escalation timers, and template versioning remain future "
            "enhancements, not completed features.",
        ],
    },
    8: {
        "blurb_edits": [(
            "Standard emails resolve issuer, entity, or tenant branding and use one polished, "
            "email-client-safe layout.",
            "Standard emails resolve the issuer's, entity's or tenant's name and logo, refuse a "
            "logo address that is not absolute http or https, and use one polished, "
            "email-client-safe layout. A finished import job's notice links to its batch, and "
            "visiting the batch acknowledges it.",
        )],
    },
    12: {
        "status": {"In use": "Partial"},
        "blurb_edits": [(
            "176 tests. Documented by M12 FRD v2.1",
            "An approved leave request covering today makes the person read On Leave, with "
            "the date they are back, without anybody setting it, and a school's administrators "
            "have staff records like everybody else. 218 staff tests. Documented by M12 FRD "
            "v2.3",
        )],
        "box_edits": [
            ("replace", "• Two of the ten capabilities above are not evidenced",
             "• One of the ten capabilities above is not evidenced and one is only partly "
             "evidenced, which with field-level access is why this module is Partial rather "
             "than Complete. Department, position and manager assignment: the organogram this "
             "was meant to reuse is a single platform-global tree whose PositionAssignment "
             "refuses any non-platform user, so a school's staff cannot hold a seat in it and "
             "no reporting line exists; no second organization structure was built. "
             "Availability indicators are evidenced by the day and not by the hour: an "
             "approved leave request covering today makes the person read On Leave with the "
             "date they are back, computed on every read, and nothing states whether somebody "
             "is free at a given hour, because no contract records hours."),
            ("append",
             "• A role granted with a new member of staff reaches the whole school unless the "
             "caller names a branch for it. The staff import never does, and the Add staff "
             "endpoint does only when role_branch is sent, so a teacher posted to Ikeja and "
             "added without it can read every branch's records. One school's rows written "
             "this way were repaired by a one-off command; the default that wrote them is "
             "unchanged."),
        ],
    },
    10: {
        "box_edits": [
            ("append",
             "• Rollback reverses what CodeX imports and calendar events, and nothing else. A "
             "school's students, staff, guardians, classes and subjects have no reverser, so "
             "rows imported in error are corrected through the module that owns them rather "
             "than unwound here."),
        ],
    },
    11: {
        "blurb_edits": [(
            "Documented by M11 FRD v2.5",
            "A guardian is found from the palette by name or by a ward's name, with no contact "
            "details in the result, and guardians arrive in bulk as one row per "
            "guardian-and-child link. Documented by M11 FRD v2.9",
        )],
    },
    14: {
        "blurb_edits": [(
            "Documented by M14 FRD v3.1",
            "The calendar is the first dataset a school may import for itself, and every list "
            "reads a branch lens, inclusive of school-wide rows, with a teacher's week never "
            "narrowed. Documented by M14 FRD v3.3",
        )],
    },
    17: {
        "count": 20,
        "add": [(
            "▸  Fee structure linked to an academic session and term",
            "▸  School fee due-date policy",
        )],
        "box_edits": [
            ("append",
             "• A school's fee due-date policy decides when a term's fees fall due, and cohort "
             "billing through the finance abstraction layer applies it. A structure billed "
             "through this module's own generation route takes the caller's date or the "
             "entity's payment terms instead, so the same term's fees can fall due on different "
             "days depending on which route raised them."),
        ],
    },
    18: {
        "count": 24,
        "add": [(
            "▸  Provider confirmation outside the booking transaction",
            "▸  Approval reversal refused once a payout has reached the provider",
        )],
        "box_edits": [
            ("append",
             "• A batch's approval can no longer be reversed once any instruction in it has "
             "been claimed for the provider, because that money may already have moved. No "
             "route cancels the instructions still pending, so a batch found to be wrong after "
             "its first transfer goes on to send the rest when the worker or the recovery sweep "
             "reaches them."),
        ],
    },
    19: {
        "box_edits": [
            ("append",
             "• The direct-post routes for journals and expense claims refuse a post only while "
             "a stage of the resolved ladder would apply. Where the ladder has no steps the "
             "document posts with no confirmation and no record that it went unapproved, "
             "although the same document sent through submit is refused."),
        ],
    },
    20: {
        "box_edits": [
            ("append",
             "• The approval requirement a read reports does not signal a ladder with no steps. "
             "Such a row reads as needing no approval, and its post is still refused until "
             "somebody confirms, so a client choosing between Post and Submit from that field "
             "alone meets a refusal it was not warned of."),
        ],
    },
    23: {
        "box_edits": [
            ("append",
             "• Reversing the approval behind a posted bill or payment is not refused. Only a "
             "purchase order already released to its vendor answers the engine's reversal "
             "check, so a posted payment's approval can be sent back to pending while the "
             "ledger says it was paid. Required completion: refuse the reversal once a bill or "
             "payment has posted."),
        ],
    },
    24: {
        "box_edits": [
            ("append",
             "• The balance list, the movement ledger, the item list and its summary narrow "
             "only when a location is named. Asked without one they report every store the "
             "entity holds, so a storekeeper pinned to one branch, who cannot open another "
             "branch's store by id, can still read its balances and every movement at it by "
             "leaving the filter off."),
        ],
    },
    29: {
        "blurb": (
            "Task management for CodeX's own accountability tracker, with assignment, "
            "filtering, overdue visibility, and personal and team dashboards. One accountable "
            "item is owned by exactly one CodeX staff member, and the keys are platform-scoped, "
            "so a school's roles screen does not offer them."
        ),
        "box_edits": [
            ("append",
             "• The tracker is CodeX's own rather than a school feature. todo.task.view, manage "
             "and assign are platform-scoped, so a school choosing what its bursar may do is no "
             "longer offered keys that govern nothing it has."),
        ],
    },
    31: {
        "attention": [
            "CURRENT DECISION",
            "• Module 31 remains Backend Complete and In use Complete with seventeen capability "
            "entries, reconciled to M31 FRD v1.6.",
            "• A school's ticket is the school's own until the school escalates it. The "
            "platform desk spans CodeX's own tickets, the ones a school sent up, and anything a "
            "support user is personally on, rather than everything every tenant files.",
            "• Naming a ticket's owner is the platform desk's. Every assignee is CodeX support "
            "staff, so choosing among them is CodeX's rota and tickets.ticket.assign is "
            "platform-scoped.",
            "• Two narrow gaps are recorded in M31 FRD v1.6: the desk's ticket export carries "
            "CodeX's own tickets only, because every export run is scoped to the tenant it was "
            "requested in; and an agent holding the manage key only through a permission group "
            "can be assigned a ticket but is never told of a new or escalated one.",
        ],
    },
}

#: Body paragraphs outside any grid, keyed by the start of their current text
#: -> full new text. One module's box is written as paragraphs, not a table.
BODY_CHANGES: dict[str, str] = {
    "• A teacher is identified by a role grant rather than by a staff record": (
        "• A teacher is identified by a role grant rather than by a staff record. The staff "
        "record now exists and a class names its teacher from it, but a timetable slot still "
        "points at the account and the teacher picker still reads teacher role grants, so "
        "somebody who has not been given a login, or whose teacher role nobody assigned, cannot "
        "be put on a timetable. Module 12 owns the record this should read; nothing in this "
        "module should grow a staff table of its own in the meantime."
    ),
}

#: Priority-gap rows, keyed by the text of their Gap cell -> {column: text}.
GAP_CHANGES: dict[str, dict[int, str]] = {
    "School lifecycle controls": {
        2: "Add commercial suspend and reactivate distinct from the operator service switch, "
           "recoverable soft delete, and define session, authentication and downstream-record "
           "behaviour. An abandoned onboarding suspends a school and platform staff can return "
           "it, and a live school can already be taken out of service and back, so what is "
           "missing is the commercial lifecycle rather than all of it.",
    },
}

#: Build-order rows, keyed by their step number -> {column: text}.
BUILD_ORDER_CHANGES: dict[str, dict[int, str]] = {}


# ── helpers ──────────────────────────────────────────────────────────────────

def replace_cell(cell, text: str, **kwargs) -> None:
    """Rewrite a single-line cell, dropping any extra paragraphs it carried."""
    while len(cell.paragraphs) > 1:
        paragraph = cell.paragraphs[-1]
        paragraph._p.getparent().remove(paragraph._p)
    write_cell(cell, text, **kwargs)


def retitle(paragraph, text: str) -> None:
    """Rewrite a paragraph in place, keeping the formatting of its first run."""
    runs = paragraph.runs
    if not runs:
        paragraph.add_run(text)
        return
    runs[0].text = text
    for run in runs[1:]:
        run.text = ""


def set_lines(cell, lines: list[str]) -> None:
    """Rewrite a heading-plus-bullets cell without flattening it.

    Paragraph i is reused for line i, so the bold coloured heading stays the
    heading and every bullet keeps bullet formatting. Overflow clones the LAST
    paragraph (a bullet), never the first, which would turn each new line into
    a copy of the heading.
    """
    paragraphs = list(cell.paragraphs)
    for i, line in enumerate(lines):
        if i < len(paragraphs):
            retitle(paragraphs[i], line)
        else:
            last = cell.paragraphs[-1]
            clone = copy.deepcopy(last._p)
            last._p.addnext(clone)
            retitle(Paragraph(clone, cell), line)
    for extra in list(cell.paragraphs)[len(lines):]:
        extra._p.getparent().remove(extra._p)


BOX_HEADINGS = ("NEEDS ATTENTION", "CURRENT DECISION", "CURRENT CONTROL")


def set_box(cell, lines: list[str]) -> None:
    """Rewrite a module's Needs Attention or Current Decision box.

    The boxes do not share one shape. Most hold a bold heading paragraph and a
    paragraph per bullet, some hold the heading and every bullet in one run
    separated by line breaks, and a few are one bold run throughout. Each line
    becomes its own paragraph cloned from what the box already holds, so the
    font and size stay the box's own, and the heading is then made bold in the
    box's heading colour with every bullet plain black. A row that is not a box
    is refused rather than overwritten, because one grid ends on a capability
    row instead.
    """
    from docx.shared import RGBColor

    if not cell.paragraphs[0].text.strip().startswith(BOX_HEADINGS):
        raise ValueError(f"Not a Needs Attention or Current Decision box: {cell.text[:60]!r}")
    if not lines[0].startswith(BOX_HEADINGS):
        raise ValueError(f"A box starts with its heading: {lines[0]!r}")
    colour = "2E5495"
    first = cell.paragraphs[0].runs[0] if cell.paragraphs[0].runs else None
    if first is not None and first.bold and first.font.color and first.font.color.type:
        colour = str(first.font.color.rgb)
    set_lines(cell, lines)
    for i, paragraph in enumerate(cell.paragraphs):
        for run in paragraph.runs:
            run.bold = i == 0
            run.font.color.rgb = RGBColor.from_string(colour if i == 0 else "000000")


def box_lines(cell) -> list[str]:
    """A box's heading and bullets, one entry each, whatever its layout."""
    lines = [line.strip() for p in cell.paragraphs for line in p.text.split("\n") if line.strip()]
    if not lines or lines[0] not in BOX_HEADINGS:
        raise ValueError(f"Box heading is not on a line of its own: {lines[:1]!r}")
    return lines


def edit_lines(lines: list[str], edits) -> list[str]:
    """Apply ("replace", start, new) and ("append", new) edits to box lines.

    A replace names the start of exactly one existing line, so an edit can
    never land on the wrong bullet or silently miss.
    """
    lines = list(lines)
    for edit in edits:
        if edit[0] == "replace":
            _, start, new = edit
            hits = [i for i, line in enumerate(lines) if line.startswith(start)]
            if len(hits) != 1:
                raise ValueError(f"{start!r} starts {len(hits)} box lines")
            lines[hits[0]] = new
        elif edit[0] == "append":
            lines.append(edit[1])
        else:
            raise ValueError(f"Unknown box edit: {edit[0]!r}")
    return lines


def find_bullet(table, text: str):
    for row in table.rows:
        for cell in row.cells:
            for paragraph in cell.paragraphs:
                if paragraph.text.strip() == text.strip():
                    return cell, paragraph
    raise ValueError(f"Capability bullet not found: {text!r}")


def add_bullet(table, anchor: str, text: str) -> None:
    """Place a new capability bullet directly after the anchor bullet.

    A grid cell holds its bullets either as a paragraph each or as one run whose
    lines are separated by breaks, and both shapes occur in the same document.
    A bullet of its own is cloned, so the copy keeps the run formatting; a
    bullet inside a run is inserted as another line of that run, which keeps it
    for the same reason.
    """
    try:
        cell, paragraph = find_bullet(table, anchor)
    except ValueError:
        paragraph = None
    if paragraph is not None:
        clone = copy.deepcopy(paragraph._p)
        paragraph._p.addnext(clone)
        retitle(Paragraph(clone, cell), text)
        return
    for row in table.rows:
        for cell in row.cells:
            for paragraph in cell.paragraphs:
                lines = paragraph.text.split("\n")
                if not any(line.strip() == anchor.strip() for line in lines):
                    continue
                if len(paragraph.runs) != 1:
                    raise ValueError(f"Anchor line sits in {len(paragraph.runs)} runs: {anchor!r}")
                at = next(i for i, line in enumerate(lines) if line.strip() == anchor.strip())
                lines.insert(at + 1, text)
                paragraph.runs[0].text = "\n".join(lines)
                return
    raise ValueError(f"Capability bullet not found: {anchor!r}")


def set_status(table, label: str, value: str) -> None:
    """Rewrite one "Label: value" cell of a module's status line.

    The bold label run is rewritten as well as the value run, because a cell
    whose value drifted into the label run would otherwise keep a bold stale
    value beside the new one.
    """
    for cell in table.rows[0].cells:
        paragraph = cell.paragraphs[0]
        if paragraph.text.strip().startswith(f"{label}:"):
            runs = paragraph.runs
            runs[0].text = f"{label}: "
            if len(runs) > 1:
                runs[1].text = value
                for run in runs[2:]:
                    run.text = ""
            else:
                run = copy.deepcopy(runs[0]._r)
                runs[0]._r.addnext(run)
                paragraph.runs[1].text = value
                paragraph.runs[1].bold = None
            return
    raise ValueError(f"Status cell not found: {label!r}")


def replace_cover_version(table, source: str, target: str) -> None:
    """Rewrite the version on the cover, preferring the labelled "Version: x" run.

    The bare number is the fallback, not the first try, because a date or a
    count elsewhere on the cover can contain it too.
    """
    runs = [run for paragraph in table.rows[0].cells[0].paragraphs for run in paragraph.runs]
    for needle, value in ((f"Version: {source}", f"Version: {target}"), (source, target)):
        for run in runs:
            if needle in run.text:
                run.text = run.text.replace(needle, value)
                return
    raise ValueError(f"Cover version not found: {source}")


def keep_rows_whole(table) -> None:
    from docx.oxml import OxmlElement
    from docx.oxml.ns import qn

    for row in table.rows:
        properties = row._tr.get_or_add_trPr()
        if properties.find(qn("w:cantSplit")) is None:
            properties.append(OxmlElement("w:cantSplit"))


def prepend_change_log(table, version: str, date: str, summary: str) -> None:
    template = table.rows[1]
    template._tr.addprevious(copy.deepcopy(template._tr))
    row = table.rows[1]
    replace_cell(row.cells[0], version, size=8)
    replace_cell(row.cells[1], date, size=8)
    replace_cell(row.cells[2], summary, size=8)


def module_blurbs(doc) -> dict[int, Paragraph]:
    """Each module's description: the first paragraph after its status table."""
    blurbs, pending, seen_table = {}, None, False
    for element in doc.element.body.iterchildren():
        tag = element.tag.split("}")[1]
        if tag == "p":
            text = Paragraph(element, doc).text.strip()
            if text.startswith("Module ") and ":" in text[:11]:
                pending, seen_table = int(text.split(":")[0].split()[1]), False
            elif pending is not None and seen_table and text:
                blurbs[pending] = Paragraph(element, doc)
                pending = None
        elif tag == "tbl" and pending is not None:
            seen_table = True
    return blurbs


def row_keyed(table, column: int, key: str):
    for row in table.rows:
        if row.cells[column].text.strip() == key:
            return row
    raise ValueError(f"Row not found: {key!r}")


def require_filled() -> None:
    missing = [name for name, value in (
        ("MRD_SOURCE_SCOPE", MRD_SOURCE_SCOPE),
        ("MRD_CONTENTS_NOTE", MRD_CONTENTS_NOTE),
        ("MRD_INTRO", MRD_INTRO),
        ("MRD_CHANGE_SUMMARY", MRD_CHANGE_SUMMARY),
        ("MRD_DELTA_ROWS", MRD_DELTA_ROWS),
    ) if not value]
    if missing:
        raise SystemExit(f"Refusing to cut v{MRD_TARGET}: unfilled {', '.join(missing)}")


# ── the revision ─────────────────────────────────────────────────────────────

def patch_mrd(source: Path, output: Path, *, check: bool = False) -> None:
    """Write v2.76 from v2.75, or with ``check`` only validate the layout."""
    if not check:
        require_filled()
    doc = Document(str(source))
    tables = doc.tables
    cover, control, contents = tables[COVER], tables[CONTROL], tables[CONTENTS]
    index, gaps, delta = tables[MODULE_INDEX], tables[PRIORITY_GAPS], tables[DELTA]
    build, log = tables[BUILD_ORDER], tables[CHANGE_LOG]
    grids: dict[int, Table] = {m: tables[i] for m, i in CAPABILITIES.items()}

    # Refuse a source whose layout has moved rather than edit the wrong cells.
    assert delta.rows[0].cells[0].text.strip().startswith(f"v{MRD_SOURCE} capability delta")
    assert log.rows[0].cells[0].text.strip() == "Version"
    assert index.rows[0].cells[5].text.strip() == "Entries"
    statuses: dict[int, Table] = {m: tables[i - 1] for m, i in CAPABILITIES.items()}
    for module, grid in grids.items():
        assert grid.rows[0].cells[0].text.strip().startswith("▸"), module
        assert statuses[module].rows[0].cells[0].text.strip().startswith("Phase:"), module

    if check:
        blurbs = module_blurbs(doc)
        missing = sorted(set(grids) - set(blurbs))
        cover_runs = [r.text for p in cover.rows[0].cells[0].paragraphs for r in p.runs
                      if MRD_SOURCE in r.text]
        total = sum(int(row.cells[5].text.strip()) for row in index.rows[1:])
        print(f"layout OK: {len(grids)} capability grids, {len(blurbs)} module blurbs"
              + (f", NO blurb for {missing}" if missing else ""))
        print(f"cover runs naming {MRD_SOURCE}: {cover_runs}")
        print(f"index total: {total}")
        return

    replace_cover_version(cover, MRD_SOURCE, MRD_TARGET)

    for row in control.rows:
        label = row.cells[0].text.strip()
        if label == "Version":
            replace_cell(row.cells[1], MRD_TARGET, size=9)
        elif label == "Review date":
            replace_cell(row.cells[1], REVIEW_DATE, size=9)
        elif label == "Source scope":
            replace_cell(row.cells[1], MRD_SOURCE_SCOPE, size=9)

    for row in contents.rows:
        if row.cells[0].text.strip().startswith("5."):
            replace_cell(row.cells[0], f"5. v{MRD_TARGET} Capability Delta",
                         size=9, bold=True, color=BLUE)
            replace_cell(row.cells[1], MRD_CONTENTS_NOTE, size=9)

    blurbs = module_blurbs(doc)
    for paragraph in doc.paragraphs:
        text = paragraph.text.strip()
        if text == f"5. v{MRD_SOURCE} Capability Delta":
            retitle(paragraph, f"5. v{MRD_TARGET} Capability Delta")
        elif text.startswith("This revision") and len(text) > 80:
            retitle(paragraph, MRD_INTRO)

    for module, changes in MODULE_CHANGES.items():
        grid = grids[module]
        for current, new in changes.get("restate", []):
            _, paragraph = find_bullet(grid, current)
            retitle(paragraph, new)
        for anchor, new in changes.get("add", []):
            add_bullet(grid, anchor, new)
        if changes.get("attention"):
            set_box(grid.rows[-1].cells[0], changes["attention"])
        if changes.get("box_edits"):
            box = grid.rows[-1].cells[0]
            set_box(box, edit_lines(box_lines(box), changes["box_edits"]))
        if changes.get("blurb"):
            retitle(blurbs[module], changes["blurb"])
        for old, new in changes.get("blurb_edits", []):
            text = blurbs[module].text
            if text.count(old) != 1:
                raise ValueError(f"Module {module} description holds {old!r} {text.count(old)} times")
            retitle(blurbs[module], text.replace(old, new))
        index_columns = {"Backend": 3, "In use": 4}
        for label, value in changes.get("status", {}).items():
            set_status(statuses[module], label, value)
            if label in index_columns:
                replace_cell(row_keyed(index, 0, str(module)).cells[index_columns[label]], value,
                             size=7, alignment=WD_ALIGN_PARAGRAPH.CENTER)
        if changes.get("count") is not None:
            replace_cell(row_keyed(index, 0, str(module)).cells[5], str(changes["count"]),
                         size=7, alignment=WD_ALIGN_PARAGRAPH.CENTER)

    for start, text in BODY_CHANGES.items():
        hits = [p for p in doc.paragraphs if p.text.strip().startswith(start)]
        if len(hits) != 1:
            raise ValueError(f"{start!r} starts {len(hits)} body paragraphs")
        retitle(hits[0], text)

    # The total is the index's own sum, never a number typed in beside it.
    total = sum(int(row.cells[5].text.strip()) for row in index.rows[1:])
    for row in control.rows:
        if row.cells[0].text.strip() == "Capability entries":
            replace_cell(row.cells[1], str(total), size=9)

    for gap, columns in GAP_CHANGES.items():
        row = row_keyed(gaps, 1, gap)
        for column, text in columns.items():
            replace_cell(row.cells[column], text, size=8)

    for step, columns in BUILD_ORDER_CHANGES.items():
        row = row_keyed(build, 0, step)
        for column, text in columns.items():
            replace_cell(row.cells[column], text, size=8)

    rebuild_table(delta, [f"v{MRD_TARGET} capability delta", "Decision", "Evidence"],
                  MRD_DELTA_ROWS, DELTA_WIDTHS)
    keep_rows_whole(delta)

    prepend_change_log(log, MRD_TARGET, SHORT_DATE, MRD_CHANGE_SUMMARY)

    title = f"XVS Module Requirements Document v{MRD_TARGET}"
    doc.core_properties.title = title
    doc.core_properties.version = MRD_TARGET
    output.parent.mkdir(parents=True, exist_ok=True)
    doc.save(str(output))
    update_extended_title(output, title)
    shrink_inherited_media(output)
    assert_no_em_dash(output)
    print(f"Wrote MRD v{MRD_TARGET}: {total} capability entries")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", default=Path(__file__).resolve().parents[1])
    parser.add_argument("--check", action="store_true",
                        help="Validate the layout assumptions; write nothing.")
    args = parser.parse_args()
    folder = Path(args.root) / "module-requirements"
    target = folder / f"XVS_Module_Requirements_Document_v{MRD_TARGET}.docx"
    source = folder / f"XVS_Module_Requirements_Document_v{MRD_SOURCE}.docx"
    if args.check:
        patch_mrd(source, target, check=True)
        return
    if target.exists():
        raise SystemExit(f"{target.name} already exists; never overwrite a version")
    patch_mrd(source, target)


if __name__ == "__main__":
    main()

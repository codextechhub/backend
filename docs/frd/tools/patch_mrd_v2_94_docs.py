#!/usr/bin/env python3
"""Cut MRD v2.94: the tracker reconciled to twenty-five module FRDs of 27 and 28 September 2026.

The module FRDs revised from the Documents owed queue (todo.md) are read as the
evidence, and this revision carries into the tracker what they now state:

* every module in the batch names its new FRD version, in its description and
  in any box that names the version it is reconciled to;
* Module 1 gains one capability entry, a school's own sign-in security and
  payroll scope (M01 FR-022, which M01 v1.29 records as untraced), so Module 1
  holds 25 entries and the platform 512;
* Module 4's Field Access entry is widened to every registered field, because
  M04 v1.29 maps the registry, the per-role switches, the exceptions and their
  enforcement to it and records that its wording names person records alone;
  the count stays at 23;
* the gaps the FRDs close leave: the P2 priority gap on branch financial
  statements and the already-closed P1 on academic structure behind
  onboarding, Module 19's statements and direct-post items, Module 7's
  named-post item, Module 12's field-exception sentence and its
  grant-restricted-fields reason for Partial, and Module 20's one-threshold
  item; the gaps the FRDs add arrive in the module boxes;
* current architecture gains the branch write and money rule, the empty
  approval route a school's books arrive with, and where the client address
  comes from;
* procurement's dates are the school's own day, which closes for procurement
  the dates the school's time zone left on the UTC day;
* Module 6's box counts the config.* keys at twenty-three, the number
  ConfigPermissions.ALL and seed_config_permissions hold, where it said
  nineteen.

No module status moves. Queue entries for later versions (D51 to D55, D61 and
D64 to D66) are not folded in, and nothing here describes them.

The MRD starts from the newest version in its folder and is written at the next
free number; the script refuses to run if a newer version has appeared, and
``finish`` refuses to overwrite one.

    python tools/patch_mrd_v2_94_docs.py
"""
from __future__ import annotations

import copy

from docx import Document

import patch_mrd_v2_79_docs as mrd_tools
from patch_document_type_labels_docs import require_newest
from patch_field_error_message_docs import insert_bullet_after
from patch_record_history_docs import ROOT, finish, keep_format, replace_cell
from patch_restricted_grant_ladder_docs import (
    assert_absent_outside_log,
    box_of,
    edit_blurb,
    edit_box,
)
from patch_school_creation_job_docs import append_bullet
from patch_school_organogram_docs import drop_box_line, table_headed_prefix
from patch_staff_id_and_auth_events_docs import normalise_change_log, repair_ooxml

REVIEW_DATE, SHORT_DATE = "28 September 2026", "28 Sep 2026"
MRD_SOURCE, MRD_TARGET = "2.93", "2.94"
TOTAL_BEFORE, TOTAL_AFTER = 511, 512

CODE_BASELINE = (
    "Backend main at 232c45a9, 28 September 2026, which carries the finance pass (merged "
    "32194627), the branch bank-ledger and payments naming rules (merged 864468ba) and the "
    "payments reach pass (merged 0ea6a97c). The student, guardian, admission-stage, "
    "time-zone and school-wide settings committed the same day are outside this revision. "
    "The school app's integrations named here are committed and pending release"
)

#: Each module in the batch and the FRD version this revision reconciles it to.
FRD_VERSIONS = {
    1: "1.29", 3: "1.19", 4: "1.29", 5: "1.7", 6: "1.5", 7: "1.22", 8: "1.13", 9: "2.13",
    10: "1.6", 11: "2.12", 12: "2.14", 13: "2.12", 14: "3.5", 17: "1.7", 18: "1.13",
    19: "1.12", 20: "1.6", 21: "1.6", 22: "1.14", 23: "1.14", 24: "1.5", 25: "1.2",
    26: "1.3", 30: "1.3", 31: "1.10",
}


def documented(module: int) -> str:
    return f"Documented by M{module:02d} FRD v{FRD_VERSIONS[module]}."


# ── current architecture ─────────────────────────────────────────────────────

ARCH_BRANCH_WRITES = (
    "• A branch-bound caller reads the school-wide rows beside their own branch's and changes "
    "a row only when every branch it belongs to is theirs; anything else is refused 403 "
    "SHARED_RECORD_READ_ONLY, and every row carries can_manage. Money follows the same reach: "
    "a bank account or bank ledger a write names must be one the caller's branches reach, and "
    "a branch's document is paid from, or deposited into, its own branch's account or a "
    "school-wide one."
)
ARCH_EMPTY_ROUTES = (
    "• Who approves a school's documents is the school's own answer. Its books arrive holding "
    "one approval route per approvable document type, published with no steps, and no "
    "approver groups. A document meeting a route with no steps is refused "
    "APPROVAL_NOT_CONFIGURED and goes through only where its type allows somebody to confirm "
    "it without approval, recorded against them; a route with a live step is never bypassed."
)
ARCH_CLIENT_ADDRESS = (
    "• The client address is decided once per request: from Cloudflare's CF-Connecting-IP "
    "header, then True-Client-IP, on Render, and from the peer address elsewhere. "
    "X-Forwarded-For, which the caller writes, is never read, so every per-address throttle, "
    "sign-in record, lockout and download log reads an address the caller cannot choose."
)


# ── module descriptions and boxes ────────────────────────────────────────────

M01_BLURB = (
    "A live school sets its own sign-in security, tightening and never loosening what the "
    "platform allows, and its payroll scope, under school.settings keys, and its currency and "
    "term structure are fixed once it has been live. " + documented(1)
)
M01_BULLET = "▸  A school's own sign-in security and payroll scope, set from its settings"

M03_BLURB = (
    " A lockout is a window on its own record rather than a stored status: it releases "
    "itself, one unlock clears it and never a suspension, and a completed reset ends it. Every "
    "throttle, sign-in record and lockout reads a client address the caller cannot write, and "
    "the sign-in response and /auth/me/ say which branches the person reaches. " + documented(3)
)

M04_FIELD_ACCESS_OLD = "▸  Field Access over whole person records, with composite names"
M04_FIELD_ACCESS_NEW = (
    "▸  Field Access over every registered field, by role switch and one-person exception, "
    "with composite names"
)
M04_BLURB = (
    "Restrictable fields sit in the same module and resource tree as the permissions; each "
    "role has a Read and a Write switch on each, with one-person exceptions, and every surface "
    "leaves out a field its caller may not read and refuses a write they may not make with 403 "
    "field_write_denied. A branch-bound administrator grants roles and changes shared rows only "
    "inside their own branches, and a dataset's import key stands in for the import wizard's "
    "keys and, past it, only for what its dataset declares. " + documented(4)
)
M04_LIBRARY_OLD = "A branch role's name follows its branch, re-applied from the branch's own save."
M04_LIBRARY_NEW = (
    M04_LIBRARY_OLD + " A library key the school may not hold is dropped and logged rather than "
    "refusing the copy, and a key reclassified as platform-only is taken back from every "
    "tenant surface, the library included."
)

M05_BLURB = (
    " The five Field Access action types are registered and the RBAC audit refuses a type the "
    "central trail does not register, notification settings and template changes are "
    "recorded, and the client address in an event is one the caller could not choose. "
    + documented(5)
)

M06_BLURB = (
    " A screen that decides many blocks at once asks which of a set of keys the school's plan "
    "reaches, each given the verdict the door would give it alone, so a dashboard leaves out a "
    "block the plan does not reach rather than refusing it. " + documented(6)
)
M06_GAP_OLD = (
    "a setting a school ought to own for itself has no route, and one would have to be "
    "introduced deliberately."
)
M06_GAP_NEW = (
    "no config.* key is one a school can hold, so a setting a school owns reaches it through a "
    "route of the module it belongs to, under that module's own keys, as Module 1's sign-in "
    "security and payroll scope do."
)

M07_BLURB = (
    "A school may also name one post on its own chart in a specific-post stage or an approver "
    "group, a suspended holder is passed over, and one tenant-wide switch stops the approval "
    "emails. Journals, expense claims and direct entries meet a route with no steps as every "
    "other finance document does. " + documented(7)
)
M07_EMPTY_OLD = "recorded against whoever gives it."
M07_EMPTY_NEW = (
    "recorded against whoever gives it. A school's books arrive holding such a route for every "
    "approvable type, and the direct-post routes for journals, expense claims and direct "
    "entries answer it the same way."
)

M08_BLURB = (
    " Which emails fire is the school's, and a branch sets its own for the events that name a "
    "branch, resolved branch, then school, then platform; the wording of every message is the "
    "platform's. " + documented(8)
)
M08_AUDIT = (
    "• Every notification settings change and every template create or edit is recorded as a "
    "configuration change in the audit trail. A branch administrator changes only their own "
    "branches' settings."
)

M09_BLURB = (
    " A new school's books hold one approval route per approvable document type with no steps "
    "and no approver groups, so until the school builds its own a document is refused as "
    "unconfigured and goes through only when somebody confirms it. Before go-live a school "
    "reaches what each view declares: its onboarding, its own profile apart from its name, "
    "sign-in address and code, its support desk bar assignment, and the how-to guides. "
    + documented(9)
)

M10_BLURB = (
    " A module's own import key opens the wizard and, past it, only what its dataset declares: "
    "a bank statement's key rolls back its own statements, and every other route refuses it. "
    "The uploaded file and its rows follow the role's field switches, and the staff template "
    "asks whether to send each invitation. " + documented(10)
)
M10_ROLLBACK_OLD = "what CodeX imports and calendar events, and nothing else."
M10_ROLLBACK_NEW = (
    "what CodeX imports, calendar events and a bank statement through finance, and nothing "
    "else."
)

M11_BLURB = (
    "A child's medical fields are decided by the role's switches, enrolment is judged by them "
    "as an edit is, the admission-number rule and the age bound are the server's, and a "
    "promotion that would overfill a class waits until it is told to. Documented by M11 FRD "
    "v2.12"
)

M12_BLURB = (
    "A branch administrator reads the school-wide people and changes only those posted to "
    "their own branches, and a school-wide class's teaching duties are a school-wide "
    "administrator's. At a live school a new member of staff starts as Teacher, a lockout is "
    "read from its own record rather than stored, a staff ID is unique at its school whatever "
    "its case, and the staff import asks whether to send each invitation. " + documented(12)
)
M12_PARTIAL = (
    "• One of the capabilities above is only partly evidenced, and it is why this module is "
    "Partial rather than Complete. Availability indicators are evidenced by the day and not by "
    "the hour: an approved leave request covering today makes the person read On Leave with "
    "the date they are back, computed on every read, and nothing states whether somebody is "
    "free at a given hour, because no contract records hours. Department, position and "
    "manager assignment is evidenced: a school keeps its own organogram of posts, and a "
    "person's line manager is the holder of the post above."
)
M12_FIELDS = (
    "• Field-level access covers the staff record: the name, personal, employment and "
    "photograph fields each have a Read and a Write switch per role, enforced on every staff "
    "surface and in every copy of the email on the record, full_name is rebuilt from the "
    "parts a reader may read, and a past view applies the same switches and the field "
    "exceptions of that day."
)
M12_RESTRICTED = (
    "• Adding a person with a restricted role is refused rather than routed for approval. At a "
    "live school the staff create grants the Teacher role and nothing else, so it meets the "
    "refusal only where the school has given Teacher a restricted permission the adder does "
    "not hold; before go-live the adder chooses School Admin or Branch Admin. Either way the "
    "sentence says to add the person without the role and grant it from their profile, where "
    "it waits for approval. Module 4 carries the item."
)

M13_BLURB = (
    " A branch administrator reads the school-wide rows beside their own branch's and changes "
    "a row only when every branch it belongs to is theirs: a session is judged by the branches "
    "it covers and a term by its session. " + documented(13)
)

M14_BLURB = (
    "An exam timetable publishes over a shared hall or an invigilator between two rooms, which "
    "stay warnings, while a class sitting two papers at once is still refused. Shared rows are "
    "read-only to a branch administrator: an exam is judged by its exam period and a lesson or "
    "paper by its class. " + documented(14)
)

M17_BLURB = (
    " A school bills its fees through the cohort route, which names the children: one outside "
    "the caller's branches answers 404, and a structure carrying a branch is that branch's "
    "price list, refusing a child at another with 409 WRONG_BRANCH. The engine's own route is "
    "bounded the same way, and a receipt deposits only into its own branch's bank ledger or a "
    "school-wide one. " + documented(17)
)
M17_ALL_ACTIVE = (
    "• The engine's own generation route still takes all_active. It bills only the active "
    "customers in the caller's reach and a branch structure's branch, and the console uses it "
    "for its own books, but a school caller holding the generate key can reach it over the "
    "API, and all_active from a school-wide structure bills every active family in reach "
    "whatever class the structure was for."
)

M18_BLURB = (
    " The Fake provider exists only where a setting turns it on, which the staging settings "
    "Render runs do not; a webhook event settles only records of the provider that signed it, "
    "and the public receiver is throttled per client address before the signature is read. "
    "Every payments read and status change stays within the caller's branches, a collection "
    "or virtual account deposits into its customer's branch's bank ledger or a school-wide "
    "one, and new books publish the payout route with no steps. " + documented(18)
)
M18_FAKE = (
    "• Records made against the Fake provider before a deployment switched it off still list "
    "and read, but verifying or confirming them is refused and no route closes them, so an "
    "open one stays open."
)
M18_UNTRACEABLE = (
    "• A transactions-log entry that names no gateway record stays visible to every branch, "
    "and a payment request naming an invoice but no customer is held to no branch for its "
    "deposit account."
)

M19_BLURB = (
    " A branch-bound reader's statements, chart balances and account activity are built from "
    "their branches' journals and the school-wide entries, AR aging narrows as its lists do, "
    "and the statutory pack is the school's filing alone. A branch keeps a budget of its own. "
    "The finance dashboard reads as three views, each block behind its key, the reader's "
    "branches and the school's plan. A bank account or bank ledger a posting names stays "
    "within the caller's branches, and journals, expense claims and direct entries confirm a "
    "route with no steps. " + documented(19)
)
M19_CLOSE = (
    "• A period close runs the payables checks a dependent app registers, so it cannot seal a "
    "period over an unreconciled AP position while reporting success. It can still be forced "
    "past a blocking failure, audited as forced; whether forcing needs a permission of its own "
    "is undecided."
)
M19_STATEMENTS = (
    "• Every branch-bearing ledger list, summary and detail read narrows to the caller's "
    "branches, and so do the trial balance, income statement, balance sheet and the other "
    "statements, built from the reader's branches' journals and the school-wide entries, so "
    "each still balances. The statutory pack is the school's filing and is refused to a "
    "branch-bound reader."
)
M19_TWO_BOOKS = (
    "• A school holding two active sets of books is refused on every finance and procurement "
    "read the school layer makes and reported to the console as one incident, but nobody is "
    "notified, and the incident stays open until an operator resolves it."
)

M20_BLURB = (
    " The refunds and write-offs list opens on either key, each kind of row to its own holder, "
    "and counts pending as the receivables dashboard does. A batch confirms like a single post "
    "and stays within the caller's branches, and a refund pays from its own branch's bank "
    "account or a school-wide one. " + documented(20)
)
M20_ROUTE = (
    "• A school's books arrive holding one route per adjustment type with no steps, so every "
    "refund, write-off, concession and credit note is refused at post until the school adds "
    "steps or somebody confirms, recorded against them. The default ladder, gating refunds and "
    "write-offs at any size and a concession or credit note at or above its threshold, is "
    "published only for a tenant that asks for it, and each type carries its own conditions."
)
M20_VOID = (
    "• An approval cannot be withdrawn once the adjustment has posted; the correction is a "
    "void, which leaves both postings visible. A write-off has no void, so a mistaken "
    "write-off cannot be undone through the platform."
)
M20_PORT_OLD = "to the future student and guardian domains."
M20_PORT_NEW = (
    "to the future student and guardian domains. No school-layer write port exists for "
    "concessions, so a school screen granting a waiver calls finance directly with the "
    "finance customer id."
)

M21_BLURB = (
    " A vendor's contact, tax and bank fields are decided per role by Field Access switches, "
    "and every date that means today, a milestone's completion, an assessment's default date, "
    "a contract's lapse and the renewals list, is the school's own day, its own time zone's, "
    "Africa/Lagos by default. " + documented(21)
)
M21_BANK_OLD = "The bank code is protected with the other bank fields"
M21_BANK_NEW = "The bank code is governed with the other bank fields by the role's switches"

M22_BLURB = (
    "The Procurement overview and its Spend & suppliers view open to any procurement key, each "
    "block behind its own key, the reader's branches and the school's plan. Every procurement "
    "date that means today is the school's own day. " + documented(22)
)
M22_ROUTE = (
    "• Approval fails closed for controlled spend and nothing is imposed to achieve it. A "
    "school's books arrive holding one route per procurement document type with no steps and "
    "no approver group, so a submitted document is refused with APPROVAL_NOT_CONFIGURED until "
    "the school builds its steps, and every submit takes confirm_without_approval and a reason "
    "to send it on unreviewed, recorded against the confirmer; confirming never bypasses a "
    "live step. The default two-step ladder is published only by a command, and the "
    "one-click default-ladder route is gone."
)
M22_FAL = (
    "• The finance abstraction layer's procurement submit cannot pass the confirmation, so a "
    "school module submitting against a route with no steps is refused with no way through. "
    "It is harmless only while those actions are not exposed over HTTP."
)

M23_BLURB = (
    " A purchase order, vendor invoice and vendor payment submit against a route with no steps "
    "as a requisition does, confirmed by name. A vendor payment names its bank account within "
    "the caller's branches and is paid from an account of its bills' branch or a school-wide "
    "one, rules it keeps on edit and on post, and every payables date that means today is the "
    "school's own day. " + documented(23)
)
M23_BRANCH_OLD = "a branch-bound caller can neither settle nor read another branch's bill."
M23_BRANCH_NEW = (
    "a branch-bound caller can neither settle nor read another branch's bill. A vendor "
    "payment's bills resolve within the caller's reach on create, edit and post, and its bank "
    "account is its bills' branch's or a school-wide one."
)

M24_BLURB = (
    " An issue can name the cost centre the stock went to, the Stock & receiving view reads "
    "stock for the caller's stores, and a restock requisition is drafted from what is low. "
    "Issues, adjustments and the restock draft are dated by the school's own day. "
    + documented(24)
)

M25_KEY_OLD = "gated on that domain's own key"
M25_KEY_NEW = "opened by any key of that domain with each block behind its own key"
M25_PLAN = (
    "• A finance or procurement dashboard block needs the school's plan as well as the "
    "reader's key: a block below the school's band is absent from the payload rather than "
    "refused. Pending has one definition, shared by the refunds and write-offs list and the "
    "receivables dashboard, and the approvals waiting on the reader count credit and debit "
    "notes."
)

M26_READ = (
    "• The download log records the client address the request was decided to come from. A "
    "column naming a field under Field Access is written only when the run's owner may read "
    "that field and is otherwise reported as omitted, and the payments collections and "
    "payouts datasets narrow to the exporting caller's branches."
)

M30_FAULT = (
    "A configuration fault found outside the alert engine, such as a school holding two active "
    "sets of books, opens one incident, deduplicated on its fault key. It has no alert or rule "
    "behind it, reaches the console only, sends no notification and stays open until an "
    "operator resolves it."
)

M31_PENDING = (
    "• A school that has not gone live works its own support desk, its list, threads, "
    "replies, attachments, triage, escalation and dashboard counts, and records how-to guide "
    "events; assigning an owner stays the platform desk's. A school's missed guide search "
    "keeps its task words while a name is still redacted."
)

BUILD_ORDER_OLD = (
    "Branch scoping of procurement and the finance ledgers is done; what is left there is the "
    "financial statements."
)
BUILD_ORDER_NEW = (
    "Branch scoping of procurement, the finance ledgers, the financial statements and the "
    "payments records is done."
)


# ── delta, contents and change log ───────────────────────────────────────────

MRD_CONTENTS_NOTE = "Twenty-five module FRDs reconciled; one capability added"
MRD_INTRO = (
    "This revision reconciles the tracker to the twenty-five module FRDs revised on 27 and 28 "
    "September 2026: a school's own settings, Field Access as built, branch reach on writes "
    "and money, the empty approval route a school's books arrive with, and procurement on the "
    "school's own day."
)
MRD_DELTA_ROWS = [
    ["A school's own settings", "One Module 1 entry added",
     "A live school sets its sign-in security, tightening only, and its payroll scope under "
     "school.settings keys; its currency and term structure lock at go-live. Module 1 holds 25 "
     "entries and the platform 512."],
    ["Field Access", "Module 4 entry widened",
     "The entry covers every registered field: the registry in the permission tree, a Read and "
     "Write switch per role, one-person exceptions, and enforcement on every surface. The "
     "count stays at 23."],
    ["Branch reach on writes and money", "Current architecture",
     "A branch-bound caller changes a shared row only when every branch it belongs to is "
     "theirs. Bank accounts, bank ledgers and payments records stay within the caller's "
     "branches, and a branch's document uses its own branch's money."],
    ["Branch financial statements", "P2 gap closed",
     "A branch-bound reader's statements, chart balances and account activity come from their "
     "branches' journals and the school-wide entries; the statutory pack is the school's alone."],
    ["Onboarding's academic structure gap", "Closed P1 leaves",
     "Marked closed since Module 13 was mounted, so it leaves Priority Gaps as closed items do."],
    ["Empty approval routes", "Current architecture",
     "Books arrive with one route per approvable type and no steps. Procurement submits, "
     "journals, expense claims and direct entries confirm past it by name; payouts never do."],
    ["Named-post approvals", "Module 7 gap closed",
     "A specific-post stage and a group's position member name a post on the school's own "
     "chart; a suspended holder is passed over."],
    ["Module 12 status", "Partial for one reason",
     "Availability by the hour alone keeps Module 12 Partial. Every staff field is under Field "
     "Access, and field exceptions are kept in the record history."],
    ["Client address", "Decided once per request",
     "Cloudflare's header on Render, never X-Forwarded-For, for every throttle, sign-in "
     "record, lockout and download log."],
    ["Payments provider", "Fake gated; webhooks bound",
     "The Fake provider is off outside development and test, a webhook settles only its own "
     "provider's records, and the receiver is throttled per client address."],
    ["Dashboards", "Plan-aware blocks",
     "A block needs the school's plan as well as the reader's key and is absent, not refused, "
     "below the band; pending has one definition."],
    ["Procurement dates", "The school's own day",
     "Every procurement date that means today reads the school's day (e987ba7f, 5071e156), "
     "which closes for procurement the dates the school's time zone (480a4c87) left on the "
     "UTC day."],
    ["Module FRDs", "Twenty-five reconciled",
     "M01 v1.29, M03 v1.19, M04 v1.29, M05 v1.7, M06 v1.5, M07 v1.22, M08 v1.13, M09 v2.13, "
     "M10 v1.6, M11 v2.12, M12 v2.14, M13 v2.12, M14 v3.5, M17 v1.7, M18 v1.13, M19 v1.12, "
     "M20 v1.6, M21 v1.6, M22 v1.14, M23 v1.14, M24 v1.5, M25 v1.2, M26 v1.3, M30 v1.3, "
     "M31 v1.10."],
]
MRD_CHANGE_SUMMARY = (
    "Reconciles the tracker to twenty-five module FRDs revised on 27 and 28 September 2026. "
    "Module 1 gains one entry, a school's own sign-in security and payroll scope, so it holds "
    "25 and the platform 512. Module 4's Field Access entry covers every registered field, by "
    "role switch and one-person exception, at the same count. Current architecture gains the "
    "branch write and money rule, the empty approval route a school's books arrive with, and "
    "the client address decided once per request. Closed: the P2 gap on branch financial "
    "statements, Module 19's statements and direct-post items, Module 7's named-post item, "
    "Module 20's one-threshold item, and Module 12's field-exception sentence and its "
    "grant-restricted-fields reason for Partial. Added to the boxes: the engine's all_active "
    "route (M17), Fake-provider rows and untraceable log entries (M18), a forced close and the "
    "two-books incident (M19), the missing concession write port (M20) and the school layer's "
    "unconfirmable submit (M22). Every procurement date that means today is the school's own "
    "day, closing that gap for procurement. Module 6's box counts the config.* keys at "
    "twenty-three, where it said nineteen. The P1 academic structure behind onboarding, "
    "already marked closed, leaves Priority Gaps. The v2.93 row's 510 is left as written; "
    "the index it described added to 511. Every module in the batch names its FRD version; "
    "statuses do not move. Backend main at 232c45a9 with 32194627, 864468ba and 0ea6a97c. "
    "Backend evidence; the school app's integrations are committed and pending release; no "
    "deployment claim."
)


def blurb_append(doc, module: int, tail: str) -> None:
    """Add sentences to a module description that names no FRD version yet."""
    blurb = mrd_tools.module_blurbs(doc)[module]
    if "Documented by" in blurb.text:
        raise ValueError(f"Module {module}'s description already names an FRD version")
    mrd_tools.retitle(blurb, blurb.text.rstrip() + tail)


#: Box heading colours, read from the boxes that already carry them.
HEADING_COLOURS = {"CURRENT DECISION": "355B9D", "NEEDS ATTENTION": "2E5495"}

#: Modules started on a fresh page, so a heading, a box or a last bullet is
#: never left alone at the foot or head of a page. The build order starts a
#: page for the same reason, so its last two steps do not stand alone.
MODULE_PAGE_BREAKS = (5, 14, 20, 26, 28)


def restore_flat_box(cell) -> None:
    """Give a box held as one plain run a bold coloured heading and a paragraph per line.

    Two boxes, Module 4's and Module 22's, reached this version as one run of
    plain text with line breaks, so the heading printed like a bullet. They are
    rebuilt in the shape every other box has.
    """
    from docx.shared import RGBColor

    lines = mrd_tools.box_lines(cell)
    mrd_tools.set_box(cell, lines)
    for run in cell.paragraphs[0].runs:
        run.font.color.rgb = RGBColor.from_string(HEADING_COLOURS[lines[0]])


def patch_mrd() -> None:
    folder = ROOT / "module-requirements"
    require_newest(str(folder / "XVS_Module_Requirements_Document_v*.docx"), MRD_SOURCE)
    doc = Document(str(folder / f"XVS_Module_Requirements_Document_v{MRD_SOURCE}.docx"))
    tables = doc.tables
    cover, control, contents, index = tables[0], tables[1], tables[2], tables[5]
    gaps = table_headed_prefix(doc, "Pri.")
    build_order = next(t for t in tables if t.rows[0].cells[0].text.strip() == "1"
                       and "security and lifecycle" in t.rows[0].cells[1].text)
    delta = table_headed_prefix(doc, f"v{MRD_SOURCE} capability delta")
    log = next(t for t in tables if t.rows[0].cells[0].text.strip() == "Version"
               and len(t.rows[0].cells) == 3)
    assert index.rows[0].cells[5].text.strip() == "Entries"
    total_before = sum(int(r.cells[5].text.strip()) for r in index.rows[1:])
    if total_before != TOTAL_BEFORE:
        raise ValueError(f"Capability total is {total_before}, expected {TOTAL_BEFORE}")

    # Cover, control page and contents.
    mrd_tools.replace_cover_version(cover, MRD_SOURCE, MRD_TARGET)
    for r in control.rows:
        label = r.cells[0].text.strip()
        if label == "Version":
            replace_cell(r.cells[1], MRD_TARGET, size=9)
        elif label == "Review date":
            replace_cell(r.cells[1], REVIEW_DATE, size=9)
        elif label == "Source scope":
            replace_cell(r.cells[1], CODE_BASELINE, size=9)
        elif label == "Capability entries":
            replace_cell(r.cells[1], str(TOTAL_AFTER), size=9)
    for r in contents.rows:
        if r.cells[0].text.strip().startswith("5."):
            keep_format(r.cells[0], f"5. v{MRD_TARGET} Capability Delta")
            keep_format(r.cells[1], MRD_CONTENTS_NOTE)
    for paragraph in doc.paragraphs:
        text = paragraph.text.strip()
        if (text.startswith("5. ") and paragraph.style is not None
                and paragraph.style.name.startswith("Heading")):
            mrd_tools.retitle(paragraph, f"5. v{MRD_TARGET} Capability Delta")

    # Current architecture.
    insert_bullet_after(doc, "• Branch reach follows the role grant.", ARCH_BRANCH_WRITES)
    insert_bullet_after(doc, ARCH_BRANCH_WRITES[:40], ARCH_EMPTY_ROUTES)
    insert_bullet_after(doc, "• A refusal says why in its message.", ARCH_CLIENT_ADDRESS)

    # Module 1: one capability entry, its count, and the description.
    if index.rows[1].cells[1].text.strip() != "School & Branch Management":
        raise ValueError("Index row 1 is not Module 1")
    if index.rows[1].cells[5].text.strip() != "24":
        raise ValueError("Module 1 no longer has 24 entries")
    keep_format(index.rows[1].cells[5], "25")
    overflow = tables[mrd_tools.CAPABILITIES[1]].rows[7].cells[2]
    if "School creation runs as a tracked background job" not in overflow.text:
        raise ValueError("Module 1 capability overflow cell moved")
    append_bullet(overflow, M01_BULLET, size=8.5)
    edit_blurb(doc, 1, "Documented by M01 FRD v1.28.", M01_BLURB)

    blurb_append(doc, 3, M03_BLURB)

    # Module 4: the Field Access entry is widened at the same count.
    mrd_tools.replace_bullet(tables[mrd_tools.CAPABILITIES[4]], M04_FIELD_ACCESS_OLD,
                             M04_FIELD_ACCESS_NEW)
    edit_blurb(doc, 4, "Documented by M04 FRD v1.28.", M04_BLURB)
    edit_box(box_of(doc, 4, "CURRENT DECISION"), [
        ("sub", "• Fresh school installations carry", M04_LIBRARY_OLD, M04_LIBRARY_NEW),
    ])

    blurb_append(doc, 5, M05_BLURB)

    blurb_append(doc, 6, M06_BLURB)
    edit_box(box_of(doc, 6, "CURRENT DECISION"), [
        ("sub", "• Module 6 remains", "M06 FRD v1.3", "M06 FRD v1.5"),
        ("sub", "• Six current gaps", M06_GAP_OLD, M06_GAP_NEW),
        ("sub", "• Configuration is reserved to the platform tenant.",
         "All nineteen config.* keys", "All twenty-three config.* keys"),
    ])

    edit_blurb(doc, 7, "Documented by M07 FRD v1.19.", M07_BLURB)
    drop_box_line(box_of(doc, 7, "NEEDS ATTENTION"),
                  "• A school cannot route an approval to one named post")
    edit_box(box_of(doc, 7, "NEEDS ATTENTION"), [
        ("sub", "• An approval route with no live steps", M07_EMPTY_OLD, M07_EMPTY_NEW),
    ])

    blurb_append(doc, 8, M08_BLURB)
    edit_box(box_of(doc, 8, "NEEDS ATTENTION"), [
        ("sub", "• Event-specific in-app subjects", "M08 FRD v1.11", "M08 FRD v1.13"),
        ("append", M08_AUDIT),
    ])

    blurb_append(doc, 9, M09_BLURB)

    blurb_append(doc, 10, M10_BLURB)
    edit_box(box_of(doc, 10, "NEEDS ATTENTION"), [
        ("sub", "• ImportNotification rows", "M10 FRD v1.4", "M10 FRD v1.6"),
        ("sub", "• Rollback reverses", M10_ROLLBACK_OLD, M10_ROLLBACK_NEW),
    ])

    edit_blurb(doc, 11, "Documented by M11 FRD v2.11", M11_BLURB)

    edit_blurb(doc, 12, "Documented by M12 FRD v2.12.", M12_BLURB)
    edit_box(box_of(doc, 12, "NEEDS ATTENTION"), [
        ("replace", "• One of the ten capabilities above", M12_PARTIAL),
        ("replace", "• Field-level access covers the staff record", M12_FIELDS),
        ("replace", "• Adding a person with a restricted role", M12_RESTRICTED),
    ])

    blurb_append(doc, 13, M13_BLURB)
    edit_blurb(doc, 14, "Documented by M14 FRD v3.3.", M14_BLURB)

    blurb_append(doc, 17, M17_BLURB)
    edit_box(box_of(doc, 17, "NEEDS ATTENTION"), [("append", M17_ALL_ACTIVE)])

    blurb_append(doc, 18, M18_BLURB)
    edit_box(box_of(doc, 18, "CURRENT CONTROL"), [
        ("append", M18_FAKE), ("append", M18_UNTRACEABLE),
    ])

    blurb_append(doc, 19, M19_BLURB)
    drop_box_line(box_of(doc, 19, "NEEDS ATTENTION"),
                  "• The direct-post routes for journals and expense claims")
    edit_box(box_of(doc, 19, "NEEDS ATTENTION"), [
        ("replace", "• A period close now runs the payables checks", M19_CLOSE),
        ("replace", "• Every branch-bearing ledger list", M19_STATEMENTS),
        ("append", M19_TWO_BOOKS),
    ])

    blurb_append(doc, 20, M20_BLURB)
    edit_box(box_of(doc, 20, "NEEDS ATTENTION"), [
        ("replace", "• Refunds and write-offs are gated", M20_ROUTE),
        ("replace", "• The threshold is one figure per tenant", M20_VOID),
        ("sub", "• Connect concessions", M20_PORT_OLD, M20_PORT_NEW),
    ])

    blurb_append(doc, 21, M21_BLURB)
    edit_box(box_of(doc, 21, "CURRENT DECISION"), [
        ("sub", "• Verified vendor bank details", M21_BANK_OLD, M21_BANK_NEW),
    ])

    edit_blurb(doc, 22, "Documented by M22 FRD v1.13.", M22_BLURB)
    edit_box(box_of(doc, 22, "NEEDS ATTENTION"), [
        ("replace", "• Approval fails closed for controlled spend", M22_ROUTE),
        ("append", M22_FAL),
    ])

    blurb_append(doc, 23, M23_BLURB)
    edit_box(box_of(doc, 23, "NEEDS ATTENTION"), [
        ("sub", "• Branch scoping of the purchase chain", M23_BRANCH_OLD, M23_BRANCH_NEW),
    ])

    blurb_append(doc, 24, M24_BLURB)

    blurb_append(doc, 25, " " + documented(25))
    edit_box(box_of(doc, 25, "NEEDS ATTENTION"), [
        ("sub", "• Reconciled to M25 FRD", "M25 FRD v1.0", "M25 FRD v1.2"),
        ("sub", "• Reconciled to M25 FRD", M25_KEY_OLD, M25_KEY_NEW),
        ("after", "• Reconciled to M25 FRD", M25_PLAN),
    ])

    blurb_append(doc, 26, " " + documented(26))
    edit_box(box_of(doc, 26, "CURRENT DECISION"), [
        ("sub", "• Module 26 remains", "M26 FRD v1.1", "M26 FRD v1.3"),
        ("append", M26_READ),
    ])

    blurb_append(doc, 30, " " + documented(30))
    edit_box(box_of(doc, 30, "CURRENT DECISION"), [
        ("sub", "Module 30 remains", "M30 FRD v1.1", "M30 FRD v1.3"),
        ("append", M30_FAULT),
    ])

    blurb_append(doc, 31, " " + documented(31))
    edit_box(box_of(doc, 31, "CURRENT DECISION"), [
        ("sub", "• Module 31 remains", "M31 FRD v1.8", "M31 FRD v1.10"),
        ("append", M31_PENDING),
    ])

    # Layout: two flat boxes regain their heading, and three modules start a page.
    restore_flat_box(box_of(doc, 4, "CURRENT DECISION"))
    restore_flat_box(box_of(doc, 22, "NEEDS ATTENTION"))
    mrd_tools.keep_module_boxes_with_their_modules(doc, MODULE_PAGE_BREAKS)
    build_heading = [p for p in doc.paragraphs
                     if p.text.strip() == "6. Recommended Next Build Order"]
    if len(build_heading) != 1:
        raise ValueError(f"{len(build_heading)} build order headings found")
    build_heading[0].paragraph_format.page_break_before = True

    # Priority gaps and build order.
    # A closed gap leaves Priority Gaps rather than staying listed as closed.
    for title in ("Branch narrowing on the financial statements",
                  "Academic structure behind onboarding"):
        closed = [r for r in gaps.rows if r.cells[1].text.strip() == title]
        if len(closed) != 1:
            raise ValueError(f"{len(closed)} {title!r} gap rows found")
        gaps._tbl.remove(closed[0]._tr)
    step = next(r for r in build_order.rows if r.cells[0].text.strip() == "6")
    if step.cells[2].text.count(BUILD_ORDER_OLD) != 1:
        raise ValueError("Build order step 6 no longer names the financial statements")
    keep_format(step.cells[2], step.cells[2].text.replace(BUILD_ORDER_OLD, BUILD_ORDER_NEW))

    total_after = sum(int(r.cells[5].text.strip()) for r in index.rows[1:])
    if total_after != TOTAL_AFTER:
        raise ValueError(f"Capability total after edit is {total_after}, expected {TOTAL_AFTER}")

    # Delta section.
    mrd_tools.rebuild_table(delta, [f"v{MRD_TARGET} capability delta", "Decision", "Evidence"],
                            MRD_DELTA_ROWS, mrd_tools.DELTA_WIDTHS)
    mrd_tools.keep_rows_whole(delta)
    intro = [p for p in doc.paragraphs
             if p.text.strip().startswith("This revision") and len(p.text) > 80]
    if len(intro) != 1:
        raise ValueError(f"{len(intro)} delta introductions found")
    mrd_tools.retitle(intro[0], MRD_INTRO)

    # The closing maintenance rule travels with the last two version rows.
    rule = log.rows[-1]
    if not rule.cells[0].text.strip().startswith("MAINTENANCE RULE"):
        raise ValueError("The change log no longer ends on its maintenance rule")
    for tail in log.rows[-3:-1]:
        for cell in tail.cells:
            for paragraph in cell.paragraphs:
                paragraph.paragraph_format.keep_with_next = True

    # Copied from the latest version row, so the new row reads at the same size.
    log.rows[1]._tr.addprevious(copy.deepcopy(log.rows[1]._tr))
    for cell, text in zip(log.rows[1].cells, (MRD_TARGET, SHORT_DATE, MRD_CHANGE_SUMMARY)):
        keep_format(cell, text)

    assert_absent_outside_log(
        doc,
        "Documented by M01 FRD v1.28", "Documented by M04 FRD v1.28",
        "Documented by M07 FRD v1.19", "M11 FRD v2.11", "Documented by M12 FRD v2.12",
        "Documented by M14 FRD v3.3", "Documented by M22 FRD v1.13",
        "reconciled to M06 FRD v1.3", "M08 FRD v1.11", "M10 FRD v1.4", "M25 FRD v1.0,",
        "M26 FRD v1.1", "M30 FRD v1.1", "M31 FRD v1.8",
        "Field exceptions are not yet kept", "Field Access over whole person records",
        "The trial balance, income statement and balance sheet are not narrowed",
        "A school cannot route an approval to one named post",
        "The threshold is one figure per tenant",
        "Branch narrowing on the financial statements", "Academic structure behind onboarding",
        "what is left there is the financial statements",
        "which with field-level access is why",
        "All nineteen config.* keys",
    )
    repair_ooxml(doc)
    normalise_change_log(doc)
    finish(doc, folder / f"XVS_Module_Requirements_Document_v{MRD_TARGET}.docx",
           f"XVS Module Requirements Document v{MRD_TARGET}", MRD_TARGET)


def main() -> None:
    patch_mrd()


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""Version the Module 12 FRD against the staff code at 13675db.

Four behaviours moved after v2.2 was issued, and each changes a contract the
document states:

- On Leave stopped being a status anybody sets. An approved leave request
  covering today makes an Active person read On Leave, with the date they are
  back, and the stored column keeps the five values logged transitions write.
  A reversed leave decision returns the request to pending.
- A school's administrators became members of staff. Module 1's provisioning
  writes their record, a record for an account already in use starts Active,
  and a backfill gave one to every granted account that had none. Nobody may
  move their own employment record or suspend their own account.
- The branch roster keeps people who have left, names the role behind every
  reach, and lets school-wide people be given a branch; the bulk move refuses
  anybody who has left. A class and a subject named in a body are narrowed by
  the caller's branches, teaching parts read Main teacher and Assisting, and
  search matches a full name in either order.
- The leave document and the staff record supply condition fields to the
  workflow engine's Dynamic Roles.

The document was also still written as a specification in the places that
state current state: its control page said the module was not built, and its
dependency table, permission table, import lead and API table said the rows
this module creates did not exist. Those are corrected against the same
baseline, and a traceability section is added because the tracker carries ten
capability entries for this module and the document had nothing to reconcile
them against.

This document family carries its own formatting (Calibri, 9 point, slate text
in tables), which the shared ``write_cell`` helper does not reproduce, so every
edit here rewrites the text of an existing run and keeps its formatting. Three
rows the v2.2 patch wrote in the shared helper's typeface are restyled from a
native sibling row. The change log lists revisions oldest first, so the new row
is appended.

    python tools/patch_staff_reconciliation_docs.py
"""

from __future__ import annotations

import argparse
import copy
import re
from pathlib import Path

from docx import Document
from docx.table import Table
from docx.text.paragraph import Paragraph

from generate_requirements_documents import (
    assert_no_em_dash,
    shrink_inherited_media,
    update_extended_title,
)

REVIEW_DATE = "11 September 2026"
SHORT_DATE = "11 Sep 2026"
MRD_VERSION = "2.76"
CODE_BASELINE = "Backend worktree at 13675db, 11 September 2026"

DIRECTORY = "12-staff-management"
STEM = "XVS_M12_Staff_Management_Functional_Requirements_Document"
SOURCE, TARGET = "2.2", "2.3"


# ── helpers that keep this document's own formatting ────────────────────────

def set_run_text(paragraph, text: str) -> None:
    """Rewrite a paragraph in place, keeping the formatting of its first run."""
    if not paragraph.runs:
        raise ValueError(f"Paragraph carries no run: {paragraph.text[:60]!r}")
    paragraph.runs[0].text = text
    for run in paragraph.runs[1:]:
        run.text = ""


def set_cell_text(cell, text: str) -> None:
    """Rewrite a single-paragraph cell, keeping its run formatting."""
    for extra in cell.paragraphs[1:]:
        extra._p.getparent().remove(extra._p)
    set_run_text(cell.paragraphs[0], text)


def replace_in_cell(cell, old: str, new: str) -> None:
    text = cell.text
    if old not in text:
        raise ValueError(f"Fragment not found in cell: {old[:60]!r}")
    set_cell_text(cell, text.replace(old, new, 1))


def append_to_cell(cell, tail: str) -> None:
    set_cell_text(cell, cell.text.rstrip() + tail)


def replace_in_paragraph(paragraph, old: str, new: str) -> None:
    text = paragraph.text
    if old not in text:
        raise ValueError(f"Fragment not found in paragraph: {old[:60]!r}")
    set_run_text(paragraph, text.replace(old, new, 1))


def set_callout_text(paragraph, body: str) -> None:
    """Rewrite a callout, keeping its leading symbol and spacing."""
    prefix = re.match(r"^\S+\s+", paragraph.text).group(0)
    set_run_text(paragraph, prefix + body)


def unique_cells(row):
    seen, cells = set(), []
    for cell in row.cells:
        if id(cell._tc) not in seen:
            seen.add(id(cell._tc))
            cells.append(cell)
    return cells


def table_with_header(doc, *header: str):
    """The one table whose first row starts with these cell texts."""
    matches = [
        table for table in doc.tables
        if [c.text.strip() for c in unique_cells(table.rows[0])][: len(header)]
        == list(header)
    ]
    if len(matches) != 1:
        raise ValueError(f"Expected one table headed {header}, found {len(matches)}")
    return matches[0]


def table_with_row(doc, first_cell: str):
    """The one table holding a row whose first cell is exactly ``first_cell``."""
    matches = [
        table for table in doc.tables
        if any(row.cells[0].text.strip() == first_cell for row in table.rows)
    ]
    if len(matches) != 1:
        raise ValueError(f"Expected one table with row {first_cell!r}, found {len(matches)}")
    return matches[0]


def requirement(doc, label: str):
    """The requirement table whose banner starts with ``label``."""
    matches = [
        table for table in doc.tables
        if table.rows[0].cells[0].text.strip().startswith(label + "  ")
    ]
    if len(matches) != 1:
        raise ValueError(f"Expected one requirement table {label}, found {len(matches)}")
    return matches[0]


def row_where(table, first: str, *, exact: bool = False):
    for row in table.rows:
        text = row.cells[0].text.strip()
        if (text == first) if exact else text.startswith(first):
            return row
    raise ValueError(f"Row not found: {first[:60]!r}")


def value_cell(table, label: str):
    """The value cell of a label/value row in a requirement or control table."""
    return row_where(table, label, exact=True).cells[1]


def row_for(table, tr):
    return next(row for row in table.rows if row._tr is tr)


def insert_row_after(table, anchor, values: list[str]):
    new_tr = copy.deepcopy(anchor._tr)
    anchor._tr.addnext(new_tr)
    row = row_for(table, new_tr)
    for cell, value in zip(unique_cells(row), values):
        set_cell_text(cell, value)
    return row


def restyle_row(table, row, template):
    """Rebuild ``row`` from a native sibling so it takes the table's typeface."""
    values = [cell.text for cell in unique_cells(row)]
    new_tr = copy.deepcopy(template._tr)
    row._tr.addprevious(new_tr)
    row._tr.getparent().remove(row._tr)
    rebuilt = row_for(table, new_tr)
    for cell, value in zip(unique_cells(rebuilt), values):
        set_cell_text(cell, value)
    return rebuilt


def body_paragraph(doc, prefix: str):
    matches = [p for p in doc.paragraphs if p.text.strip().startswith(prefix)]
    if len(matches) != 1:
        raise ValueError(f"Expected one paragraph {prefix[:50]!r}, found {len(matches)}")
    return matches[0]


def callout(doc, fragment: str):
    """A callout paragraph, found by text after its leading symbol."""
    matches = [p for p in doc.paragraphs if fragment in p.text[:120]]
    if len(matches) != 1:
        raise ValueError(f"Expected one callout {fragment!r}, found {len(matches)}")
    return matches[0]


def insert_paragraph_after(anchor, text: str):
    new_p = copy.deepcopy(anchor._p)
    anchor._p.addnext(new_p)
    paragraph = Paragraph(new_p, anchor._parent)
    set_run_text(paragraph, text)
    return paragraph


def assert_native(cell) -> None:
    font = cell.paragraphs[0].runs[0].font.name
    if font != "Calibri":
        raise ValueError(f"Template row is not in the document's typeface: {font}")


# ── control page ─────────────────────────────────────────────────────────────

CONTROL_STATUS = (
    "Build specification, revised against the M12 design and reconciled against "
    "the build. Version 2.0 was written from the corrected design prompt and "
    "recorded on this page that it must be revised once a design existed; the "
    "design exists, and section 2.4 is that revision. The module is built, as "
    "schools.vs_staff mounted at /v1/i/me/staff/, and where the code has moved "
    "past what this document specified, the requirements, refusals, acceptance "
    "criteria, dependencies and traceability record what the code does."
)

CONTROL_VERIFIED = (
    f"{CODE_BASELINE}, and docs/designs/Staff_Management.html, which is nine "
    "screens, seven drawers and seventy-nine collections. What version 2.3 "
    "changes was checked against the code at that baseline. The rest keeps the "
    "evidence it was written with, so a statement in sections 2 to 4 that M14 or "
    "this module is unbuilt describes the platform the specification was first "
    "written against."
)

CONTROL_SOURCE_MRD = f"XVS Module Requirements Document v{MRD_VERSION} | Module 12"


# ── sections 2 to 8 ──────────────────────────────────────────────────────────

CORRECTION_ON_LEAVE = (
    "Half of that is right, and it is the half the code takes. ON_LEAVE is read "
    "and never stored: an ACTIVE person reads as On Leave while an approved leave "
    "request covers today, computed on every read by one queryset expression "
    "that the directory row, its employment filter and its header count share, "
    "and reads Active again the day after the leave ends with nothing written "
    "either time. No scheduled task exists or is needed, because nothing is "
    "stored that would have to be taken back, and the stored column keeps the "
    "five values logged transitions set, so the reading never overwrites the "
    "log. Migration 0003 moved every stored ON_LEAVE to ACTIVE and left the "
    "employment events as they were."
)

CORRECTION_ON_LEAVE_EVIDENCE = (
    "apps/schools/vs_staff/services/leave.py, on_leave_expression and "
    "on_leave_until_expression; apps/schools/vs_staff/serializers.py, "
    "display_employment_status; apps/schools/vs_staff/migrations/"
    "0003_on_leave_is_read_not_written.py"
)

IMPOSSIBLE_SELF_CHANGING_WHY = (
    "Half of it needs no scheduler and is done; the other half needs one and is "
    "not. Leave is read rather than stored, so an ACTIVE person reads as On "
    "Leave on the first day of approved leave and reads Active again the day "
    "after it ends, with nothing written either time. A last working day is "
    "different: passing it has to close an account, which is a write, and there "
    "is no periodic task registry in this repository to make it. The status "
    "drawer says so rather than promising otherwise: \"Their account stays "
    "usable after their last working day. Close it yourself when they have "
    "finished handing over.\" Mr. Ayanwale's last working day is 18 December; "
    "on 19 December he can still sign in, and will until an administrator "
    "closes his account. Section 14, decision 5."
)

IMPOSSIBLE_SELF_CHANGING_INSTEAD = (
    "The On Leave chip and the date the person is back, derived from approved "
    "leave rather than set by anybody. An explicit, logged resignation, and an "
    "account an administrator closes. Nothing warns when a last working day has "
    "passed and the account is still open."
)

IMPOSSIBLE_CLASH_WHY = (
    "M14 is built and finds a clash when a slot is placed, over the whole tenant "
    "and on its own screens, which is where a clash belongs: it is a fact about "
    "two timetable slots, not about an assignment. This module cannot report "
    "one, because nothing joins a teaching assignment to a slot. "
    "TimetableSlot.teacher points at the account, and M14's teacher picker "
    "offers active accounts holding the teacher role, so a slot never names the "
    "assignment it delivers."
)

IMPOSSIBLE_CLASH_INSTEAD = (
    "Nothing from this module, whose endpoints carry no clash field. A school "
    "sees a clash on M14's timetable screens, when the slot that causes it is "
    "placed."
)

VOCABULARY_VALUES = (
    "INVITED, ACTIVE, SUSPENDED, RESIGNED, TERMINATED, stored. ON_LEAVE, read "
    "and never stored: an ACTIVE person reads as ON_LEAVE while approved leave "
    "covers today."
)

VOCABULARY_ENFORCED = (
    "A transition table in a service, with every move written to "
    "StaffEmploymentEvent. ON_LEAVE is not a target in it; the serializer "
    "derives it on every read from approved leave."
)

VOCABULARY_WHO = (
    "An administrator, deliberately, through FR-008, and never on their own "
    "record; the invited person, once, by activating (FR-003); and approved "
    "leave, which changes what the row reads as and never the column."
)

MAPPING_LEAVE = [
    "Approved leave covering today, and its end",
    "Nothing, and nothing is written. Going on leave does not close a login, "
    "and a teacher on maternity leave still reads her school's calendar. The "
    "row reads On Leave while an approved request covers today and reads Active "
    "again when it ends, and the stored status stays ACTIVE throughout.",
    "Not a transition, and not offered in the status drawer. FR-008 refuses "
    "ON_LEAVE as a target.",
]

MAPPING_RESIGNED_SAYS = (
    "\"Their account stays usable after their last working day. Close it "
    "yourself when they have finished handing over.\" Plus the assignments that "
    "will need cover, named."
)

PROFILE_EMPLOYMENT_STATUS = (
    "CharField(max_length=16, choices=EmploymentStatus, default=INVITED). "
    "Stores INVITED, ACTIVE, SUSPENDED, RESIGNED or TERMINATED. ON_LEAVE stays "
    "among the choices, because employment events written before migration "
    "0003 name it and the directory's filter and header count speak it, but no "
    "row stores it: the serializer's display_employment_status reads ON_LEAVE "
    "for an ACTIVE person while approved leave covers today. A new record starts "
    "at INVITED while its account has not been accepted and at ACTIVE when it "
    "has, which is what lets an administrator's existing account be given a "
    "record without reading as an unaccepted invitation. Section 5 is what it is "
    "and is not. Written only by record creation, FR-003's activation hook and "
    "the FR-008 service."
)

LEAVE_STATUS_FIELD = (
    "CharField(max_length=12, choices=LeaveStatus, default=PENDING). PENDING, "
    "APPROVED, REJECTED, CANCELLED. Written by the workflow handler's callbacks "
    "and by the cancel service, and by no serializer: a status a form can set is "
    "a status that disagrees with the instance that decided it. on_approved and "
    "on_rejected settle a PENDING request; on_withdrawn and on_cancelled cancel "
    "one; on_action_reversed returns an APPROVED or REJECTED request to PENDING "
    "when the vote behind it is reversed, so a profile never shows leave nobody "
    "currently approves, and leaves a CANCELLED request cancelled. Completed is "
    "NOT a value here: it is derived from the end date, and a derived value must "
    "never be stored twice."
)

LEAVE_DECIDED_AT_FIELD = (
    "DateTimeField(null=True, blank=True). Stamped by the callback that writes a "
    "decision and cleared when that decision is reversed. Who decided it is the "
    "workflow instance's, read through it rather than copied, because a second "
    "copy of an approver's name is a second thing to keep in step."
)

LEAVE_INDEXES_OLD = (
    "(tenant, start_date, end_date) for FR-013's warning, and (tenant, status) "
    "for the approval queue count."
)
LEAVE_INDEXES_NEW = (
    "(tenant, start_date, end_date), and (tenant, status) for the approval queue "
    "count."
)

TEACHING_PART_FIELD = (
    "CharField(max_length=10, choices=TeachingPart, default=LEAD). LEAD or "
    "ASSISTANT, labelled Main teacher and Assisting. The main teacher is the one "
    "who enters the subject's results for that class; a class teacher looks "
    "after the class itself, and a school can have either without the other, "
    "which is why neither label says lead. The design distinguishes the two "
    "throughout, and the coverage grid shows a main teacher and the people "
    "assisting separately. A pairing with people assisting and no main teacher "
    "is taught and unaccounted for, which is a different problem from nobody "
    "teaching it, and FR-017 counts the two separately. Default LEAD because the "
    "ordinary case is one teacher who carries the subject."
)

KEY_STATES = {
    "school.teachers.assign": ("Seeded", None),
    "school.leave.view": ("Seeded", None),
    "school.leave.manage": ("Seeded", None),
    "school.administrators.update": (
        "Exists, used",
        "Change an account's email: FR-009, the endpoint these three keys were "
        "seeded for.",
    ),
    "school.administrators.suspend": ("Exists, used", "Suspend an account: FR-009."),
    "school.administrators.reactivate": (
        "Exists, used", "Reactivate or unlock an account: FR-009.",
    ),
    "school.leave.apply": ("Seeded", None),
    "school.staff_records.view / .update": ("Seeded", None),
}

KEYS_NOTE = (
    "Three keys a school already held reached nothing until this module was "
    "built. school.administrators.update, .suspend and .reactivate were seeded, "
    "SENSITIVE, granted to school_admin and bundled into the School "
    "Administrator Accounts group, and no view declared any of them, so a school "
    "could invite somebody and never suspend them while the box in its role "
    "builder said it could. FR-009's four account routes declare them, which "
    "closes for the other three verbs of the resource the gap commit 8536589 "
    "closed for creation."
)

IMPORT_LEAD = (
    "The import engine is finished and the staff dataset is built: STAFF is in "
    "TENANT_DATASETS beside the five school datasets section 2.4 records, and "
    "its row handling and validation live in schools.vs_staff.imports. The "
    "table below is the six places it touched. One needed a second pass. An "
    "uploaded row is keyed by the file's column headings and the row resolver "
    "reads target fields, so the validator translates each row through the "
    "template's column map before resolving it, the way the students dataset "
    "does, and names an issue by the heading the reader sees; without that, a "
    "template filled in unchanged failed every row as blank. An imported "
    "person's role is granted the way FR-001's is when role_branch is absent: "
    "school-wide, whatever branch the file names for their posting."
)


# ── section 9, the requirements ──────────────────────────────────────────────

FR001_GRANT_OLD = "a TenantUserRoleAssignment for the named role;"
FR001_GRANT_NEW = (
    "a TenantUserRoleAssignment for the named role, school-wide unless "
    "role_branch names a branch or the role template is tied to one;"
)
FR001_CARRIED_OLD = "any qualifications, documents and teaching assignments the form carried;"
FR001_CARRIED_NEW = "any qualifications and teaching assignments the form carried;"

FR001_RULES_TAIL = (
    " (7) The grant is school-wide unless role_branch names a branch, in which "
    "case the one grant is moved onto it rather than a second being added, or "
    "the role template is itself tied to a branch. The posting does not narrow "
    "it, so a teacher posted to Ikeja and invited without role_branch reaches "
    "every branch's records. (8) The same create_profile service gives a record "
    "to every administrator Module 1 provisions: a school's primary "
    "administrator, each branch administrator named when the school is "
    "created, and one added through the standalone branch endpoint. It runs "
    "inside that provisioning's savepoint, so an administrator and their record "
    "are made together or not at all; the job title is the one the school typed "
    "on the admin link, the posting is the account's own branch, and somebody "
    "who already has a record keeps it. A record starts at INVITED while its "
    "account has not been accepted and at ACTIVE when it has."
)

FR001_ATOMIC_OLD = (
    "and any qualifications, documents and teaching assignments submitted with "
    "the form are one transaction."
)
FR001_ATOMIC_NEW = (
    "and any qualifications and teaching assignments submitted with the form "
    "are one transaction. Documents are uploaded to the record afterwards, "
    "through FR-007."
)

FR002_FILTERS = (
    "?search= over first name, middle name, last name, email and staff number, "
    "matched loosely and ranked by core.search: the whole query as a substring, "
    "then every word as the start of a word in any order, so a full name typed "
    "surname first finds the person, then the letters of the query in order. "
    "Hits come back tightest match first and then newest first. "
    "?employment_status= filters on what the row reads as: ON_LEAVE returns "
    "ACTIVE people whose approved leave covers today, and every other value "
    "excludes them, so the facets are disjoint. ?account_status=, a SEPARATE "
    "filter and never merged with the first; ?branch=, resolved through "
    "resolve_branch_reference so a foreign id cannot be probed, or school for "
    "the people posted school-wide, and ignored at a school with one branch; "
    "?role=, by role key; ?teaching=true|false, which reads whether the person "
    "holds a teaching assignment in the active session and is labelled as that "
    "rather than as a persona."
)

FR002_COUNTS = (
    "Returned beside the page, because a directory that needs two calls to draw "
    "its header shows the header late. Total, and how many of them are "
    "currently employed, which is not the same number once somebody has "
    "resigned. By employment status as each row reads, so an ACTIVE person on "
    "approved leave today counts under On Leave and nowhere else and the bar "
    "still sums to the total. By branch, at a school with more than one, and BY "
    "ROLE at a school with exactly one, because the design puts the role "
    "breakdown where the branch breakdown would be rather than leaving a panel "
    "that repeats one value on every row. How many hold at least one teaching "
    "assignment. And how many accounts are LOCKED, which is an account-status "
    "count sitting beside employment ones and must be labelled as such. Every "
    "figure counts people distinctly over the list's own scoped queryset with "
    "its display ordering removed, so neither a teaching duty nor a sort order "
    "can inflate or split a count."
)

FR002_RULES = (
    "(1) The queryset is the users of this tenant that HAVE a StaffProfile, "
    "which replaces the earlier filter of every user the tenant owns: "
    "vs_students.Guardian carries a user foreign key, and a staff list must not "
    "become the roll. A school's own administrators are on it, because Module "
    "1's provisioning writes their record, and migration 0005 gave one to every "
    "account at a school holding an active role grant and none, which covers the "
    "administrators and everybody invited before this module existed. (2) A "
    "draft or a refused creation is never listed: no profile is written for "
    "either, and migration 0005 skipped DRAFT and REJECTED accounts. (3) Each "
    "row carries the teaching load as a count of assignments in the active "
    "session; an account flag where the account state disagrees with the "
    "employment state, which is how somebody reads as employed and locked at "
    "once; display_employment_status beside the stored employment_status, "
    "reading ON_LEAVE for an ACTIVE person while approved leave covers today; "
    "on_leave_until, the latest end date among the approved requests covering "
    "today and null otherwise, so the chip can say when somebody is back and "
    "never names a date that has passed; and on_roll, false for somebody who has "
    "resigned or been terminated. (4) select_related on user and branch, "
    "prefetch_related on the role assignments with their roles and on the "
    "invitation, named once beside the list serializer and shared with the "
    "roster, and the assignment count and the on-leave reading annotated rather "
    "than computed per row. (5) An empty directory is an empty LIST and not an "
    "empty object: success_response returns {} only for a genuinely absent "
    "payload, and a caller doing data.map() on the first day of a school with "
    "one person in it must not crash."
)

FR003_OLD = (
    "(3) A user with no StaffProfile activates exactly as before and this hook "
    "does nothing; a school's first administrator, provisioned before this "
    "module exists, is that case."
)
FR003_NEW = (
    "(3) A user with no StaffProfile activates exactly as before and this hook "
    "does nothing; an account the platform creates for itself is that case. A "
    "school's administrators are not: Module 1's provisioning gives them a "
    "record at INVITED, so their activation promotes it like anybody else's, "
    "and a record written for an account already in use starts at ACTIVE, so it "
    "never reads as an unaccepted invitation."
)

FR008_TRANSITIONS = (
    "INVITED to ACTIVE, by FR-003 only and never by an administrator. ACTIVE to "
    "SUSPENDED, RESIGNED or TERMINATED. SUSPENDED to ACTIVE, RESIGNED or "
    "TERMINATED. RESIGNED and TERMINATED are terminal. ON_LEAVE is not a target "
    "anywhere: approved leave is what puts somebody there, at read time "
    "(FR-013). It survives as a source, to ACTIVE, SUSPENDED, RESIGNED or "
    "TERMINATED, so a record stored under the earlier rule could be moved off "
    "it; migration 0003 moved every such record to ACTIVE, so none remains. "
    "Anything else is refused as 422 INVALID_STATUS_TRANSITION naming both "
    "statuses."
)

FR008_EFFECT_OLD = "and ON_LEAVE and RESIGNED touch nothing."
FR008_EFFECT_NEW = (
    "and RESIGNED touches nothing, so its confirmation tells the administrator "
    "to close the account once the person has handed over."
)

FR008_RULES_TAIL = (
    " (8) Nobody moves their own record. A transition aimed at the caller's own "
    "record is refused with 422 CANNOT_ACT_ON_SELF before anything is written, "
    "and the status drawer's read offers no moves on it and carries a note "
    "saying why: every move offered from Active closes the login or ends the "
    "job, and a school with one administrator could lock itself out with one "
    "confirmation. The same administrator may still move a colleague."
)

FR009_RULES_TAIL = (
    " (6) Suspending your own account is refused with 422 CANNOT_ACT_ON_SELF, "
    "for the reason FR-008 refuses a move on your own record. Reactivating, "
    "unlocking and changing the email of your own account carry no such "
    "refusal."
)

FR010_ROSTER_OLD = (
    "(6) The roster is a separate read, GET /v1/i/me/staff/roster/?branch=, and "
    "returns the three groups labelled: posted here, reaching here through a "
    "role, and school-wide. Each row carries why it is in its group, the role "
    "that reaches, the teaching load and the employment status, and only the "
    "first group is movable."
)
FR010_ROSTER_NEW = (
    "(6) The roster is a separate read, GET /v1/i/me/staff/roster/?branch=, and "
    "returns three labelled groups, each with a note: posted here, reaching here "
    "through a role, and school-wide. It keeps people who have left, marked "
    "on_roll false and drawn as finished, because it is the only screen that "
    "answers who was ever at a branch. Each row is the directory row. A "
    "reaching row also carries via_roles, naming each role that carries the "
    "person there and whether it is pinned to this branch or school-wide, "
    "because unpinning the first stops them reaching this branch while "
    "narrowing the second takes them off every other branch's roster as well. "
    "Posted here and school-wide are movable, the second because giving a base "
    "to somebody who has none is the same act as moving one; reaching here is "
    "not, and its change_it says to open the row and change the role. "
    "Everybody's reach is read in one query, so the roster costs the same "
    "however many staff it lists. (7) The bulk move refuses anybody in the "
    "selection who has resigned or been terminated, with 422 STAFF_HAS_LEFT "
    "naming each of them, and moves nobody. The single-record PATCH does not "
    "apply that check, so a leaver's posting can still be changed from their "
    "record."
)

FR011_PRECONDITIONS = (
    "M13 is built. The class, the subject and the session all exist in this "
    "tenant and are resolved inside it. The class is narrowed by the caller's "
    "visible branches, inclusive of school-wide classes, on the assignment "
    "route and the class-teacher route alike, and the subject is narrowed the "
    "same way on the assignment route, so a branch administrator can neither "
    "staff another branch's class nor teach a class a subject another branch "
    "owns. Either answers 404, and a school-wide class or subject stays every "
    "branch's to staff. The session is not archived."
)
FR011_PART_OLD = (
    "Every assignment carries a part, LEAD or ASSISTANT, and at most one lead "
    "exists"
)
FR011_PART_NEW = (
    "Every assignment carries a part, LEAD or ASSISTANT, labelled Main teacher "
    "and Assisting, and at most one lead exists"
)

FR013_STATUS_OLD = (
    "(4) The status is written only by the handler's on_approved and "
    "on_rejected callbacks and by the cancel service. Completed is derived from "
    "the end date at read time and is never stored."
)
FR013_STATUS_NEW = (
    "(4) The status is written only by the workflow handler's callbacks and by "
    "the cancel service. A reversed decision returns an APPROVED or REJECTED "
    "request to PENDING and clears decided_at, because the status follows the "
    "instance; a CANCELLED request stays cancelled. This module writes no audit "
    "event of its own for a reversal, and the engine's reversal entry is its "
    "record. Completed is derived from the end date at read time and is never "
    "stored."
)
FR013_LEAVE_OLD = (
    "(9) An approved request does not change employment_status. Going on leave "
    "is FR-008's transition and a school may do either, both or neither; the "
    "directory warning in FR-002 is what connects them."
)
FR013_LEAVE_NEW = (
    "(9) An approved request is what puts somebody on leave, and it writes "
    "nothing to their employment record. An ACTIVE person whose approved "
    "request covers today reads as ON_LEAVE, with the latest end date among "
    "such requests as on_leave_until, and reads ACTIVE again the day after it "
    "ends; somebody suspended, resigned or terminated reads as what they are. "
    "Reversing the approval removes the reading at once, because the request "
    "is PENDING again."
)
FR013_RULES_TAIL = (
    " (10) The leave document declares two fields a school's own Dynamic Role "
    "may test, the leave type and the days requested, and the staff record "
    "supplies two facts about the requester, their job title and their "
    "contract type, read from their record at this school and absent for "
    "anybody without one. The ladder this module provisions tests none of them "
    "and sends every request to the Leave Approvers group; a school that wants "
    "sick leave over five days to go to its head teacher builds that rule "
    "itself. A job title is compared exactly as the school typed it, so a rule "
    "on Head Teacher does not match Head teacher."
)

FR017_HEADLINE_OLD = (
    "The headline reads, for example, two pairs have nobody and one has no lead."
)
FR017_HEADLINE_NEW = (
    "The headline names the subject of each clause in the words a school uses, "
    "for example \"2 subjects have no teacher at all and 1 subject is taught "
    "with no main teacher\"."
)

FR021_RULES = (
    "(1) Matches first name, middle name, last name, email and staff number "
    "through core.search, the same matcher over the same five fields FR-002's "
    "?search= reads, so the palette and the directory can never disagree about "
    "who exists. A full name typed in either order finds the person, as does the "
    "start of each name or the letters of a name in order, and hits come back "
    "tightest match first, then by name. (2) Scoped to the tenant and then "
    "narrowed by the caller's visible branches, inclusive of school-wide "
    "people, exactly as the directory is: a palette that found somebody a "
    "branch admin cannot open would be a search that leaks a roster. (3) "
    "Capped at ten, because it renders in a dropdown and a hundred rows is a "
    "scroll nobody reads. (4) Each hit carries the id, the name, one meta line, "
    "which is the job title or else the staff number, and the stored employment "
    "status, and no email address: the palette is the most casually visible "
    "surface in the module, although an address can be typed to find somebody. "
    "(5) A query shorter than two characters returns an empty list rather than "
    "the whole school. (6) An empty result is [] and not an error, because "
    "typing a name that is not there is the ordinary case."
)


# ── sections 10 to 14 ────────────────────────────────────────────────────────

API_LEAD_OLD = "Two of these endpoints are live today and are marked as such."
API_LEAD_NEW = (
    "Every route below is built; the first three existed before this module and "
    "moved onto its keys."
)

API_ROWS = {
    "GET /v1/i/me/staff/": (
        "FR-002. Moved from school.administrators.view when this module was "
        "built, and lists staff records rather than every account the tenant "
        "owns."
    ),
    "POST /v1/i/me/staff/": (
        "FR-001. Moved from school.administrators.create when this module was "
        "built, and writes the staff record in the same transaction."
    ),
    "POST /v1/i/me/staff/<id>/resend/": (
        "FR-003. Moved with the create rather than staying on "
        "school.administrators.create. Resending is the same act as inviting "
        "aimed at the same account: leaving it behind would let a branch admin "
        "invite somebody and then be refused when they tried to chase them, "
        "which is the split-key defect FR-009 exists to close, reappearing one "
        "endpoint along."
    ),
}

VALIDATION_AFTER_LOCKED = [
    ["An employment transition to ON_LEAVE", "422",
     "INVALID_STATUS_TRANSITION. On Leave is read from approved leave and never "
     "set."],
    ["A lifecycle move on your own record, or a suspension of your own account",
     "422", "CANNOT_ACT_ON_SELF. A colleague records it."],
]
VALIDATION_AFTER_POSTING = [
    ["A bulk posting move naming somebody who has resigned or been terminated",
     "422", "STAFF_HAS_LEFT, naming each of them. Nobody is moved."],
]

ACCEPT_AFTER_TERMINAL = [
    "Nobody can move their own employment record or suspend their own account: "
    "both answer 422 CANNOT_ACT_ON_SELF and change nothing, the status drawer "
    "offers no moves on your own record and says why, and the same "
    "administrator may still suspend a colleague "
    "(NobodyEndsTheirOwnEmploymentTests).",
    "ON_LEAVE is refused as a transition target and is not offered in the "
    "status drawer (TwoStatusesTests.test_on_leave_is_not_a_move_anybody_can_make).",
]
ACCEPT_FR014_OLD = "This is FR-014 and it fails today."
ACCEPT_FR014_NEW = (
    "This is FR-014, delivered and pinned by "
    "apps/vs_rbac/tests/test_multi_branch_assignment.py."
)
ACCEPT_AFTER_ROSTER = [
    "The roster keeps somebody who has left, marked on_roll false, keeps a "
    "suspended colleague on roll, names the role and its kind on every reaching "
    "row, lets school-wide people be given a branch, and costs the same number "
    "of queries however many staff it lists; a bulk move naming somebody who "
    "has left is refused with their name and moves nobody (PostingTests).",
    "A branch administrator at Lekki cannot make anybody the class teacher of "
    "an Ikeja class or teach a Lekki class a subject Ikeja owns, and still "
    "staffs school-wide classes and subjects (BranchReachTests).",
]
ACCEPT_AFTER_CREATION = [
    "Every administrator Module 1 provisions has a staff record: a school's "
    "primary administrator is on the staff list, a branch administrator is "
    "posted to their branch, one person holding two administrator posts has "
    "one record, and a record written for an account already in use reads "
    "Active (AnAdministratorIsAMemberOfStaffTests and "
    "AnIncumbentsRecordReadsActiveTests, in vs_schools). Migration 0005 gives a "
    "record to every granted account that had none and to nobody else, "
    "starting where the account stands and dated when it was created "
    "(BackfillTests).",
]
ACCEPT_ON_LEAVE = (
    "An approved leave request covering today makes an ACTIVE person read On "
    "Leave, with its end date as on_leave_until and the later date where two "
    "overlap, leaves the stored status ACTIVE, and reads Active again once the "
    "leave has ended; the directory's On Leave filter and its header count "
    "agree with the row (TwoStatusesTests)."
)
ACCEPT_AFTER_LEAVE_ENGINE = [
    "Reversing an approval returns the request to PENDING and clears "
    "decided_at (DecisionTests.test_reversing_the_decision_puts_the_request_back_to_pending).",
]
ACCEPT_AFTER_SEARCH = [
    "A full name typed in either order, the start of each name or a few "
    "letters in order finds the person, a real match outranks a coincidence, "
    "and a stray regular-expression character is matched literally rather than "
    "failing the request (apps/core/test_search.py).",
]

DEPENDENCY_LEAD = (
    "Version 2.0 opened this section with rows that stopped part of the build, "
    "and version 2.1 with rows this document would create. The build has "
    "happened: every row this module was to create exists, and each says so "
    "below. What remains open is either a fact nobody has decided or a module "
    "that points at an account where it could point at a staff record."
)

DEPENDENCY_ROWS = {
    "M13's SchoolClass.class_teacher column": (None, None, (
        "DECLARED. A nullable foreign key from SchoolClass to StaffProfile, "
        "SET_NULL, related_name classes_led, written by FR-011's class-teacher "
        "route. It points at the staff record rather than the account, because "
        "employment rather than a login is what answers whether a class teacher "
        "still works here."
    )),
    "M14's TimetableSlot": (None, "Nothing here. FR-011 is what M14 should point at.", (
        "BUILT, and pointing at the account. TimetableSlot.teacher is a foreign "
        "key to the user, and M14's teacher picker offers active accounts "
        "holding the teacher role, school-wide, rather than the people holding "
        "a teaching assignment. So the agreement section 2.3 records is still a "
        "specification rather than a migration, a slot can name somebody with "
        "no assignment for that class, and M14 is the consumer this module is "
        "still waiting for."
    )),
    "AuditModuleKey.STAFF and five action types": (
        "AuditModuleKey.STAFF and its action types", None, (
            "REGISTERED. AuditModuleKey.STAFF and all seven action types in "
            "section 8.2 exist in vs_audit, so every audit call this module "
            "makes has a vocabulary to land in."
        )),
    "Three permission keys": ("The new permission keys", None, (
        "SEEDED. school.teachers.assign, school.leave.view, .manage and .apply "
        "exist with the defaults section 8.1 gives, beside "
        "school.staff_records.view and .update, and each is sold inside the "
        "teachers capability (decision 11)."
    )),
    "The staff import dataset": (None, None, (
        "BUILT. STAFF is a school dataset, and its validator reads each row "
        "through the template's column map, so a template filled in unchanged "
        "validates clean (ValidationReadsTheFileTests and RowRefusalTests). An "
        "imported person's role is granted school-wide unless the role template "
        "is tied to a branch: the file carries a branch for the posting and "
        "nothing for the reach."
    )),
    "A populated EmployeeSalary.employee": (None, None, (
        "SETTABLE, and not backfilled. The salary create and update accept an "
        "employee reference, resolved as an account inside the entity's own "
        "tenant, and take the name from it where none is given; the staff "
        "directory row carries user_id for the bursar's screen to send. Every "
        "salary row written before the link existed stays null and nothing "
        "infers one, so a staff record with no linked salary row is still the "
        "normal case."
    )),
    "A capability for staff": (None, None, (
        "BANDED. vs_rbac.permission_bands sells school.teachers, "
        "school.staff_records and school.leave inside the teachers capability, "
        "with the register at Core and qualifications, documents and leave at "
        "Plus. Section 14, decision 11."
    )),
}
DEPENDENCY_LEAVE_TAIL = (
    " A school's own ladder may name a Dynamic Role instead, which can test the "
    "leave type, the days requested and the requester's job title and contract "
    "type (FR-013)."
)

DECISION_TITLE_TAIL = (
    " A job title is also something approval routing reads: a Dynamic Role may "
    "test the requester's job title, and it compares the text exactly as typed, "
    "so free text makes Head Teacher and Head teacher two different titles to a "
    "rule as well as to a report."
)
DECISION_EXIT_OLD = (
    "The third is what this document specifies, and the warning is what version "
    "2.1 adds in the meantime."
)
DECISION_EXIT_NEW = (
    "The third is what the code does: the drawer's confirmation tells the "
    "administrator to close the account themselves, and nothing warns when the "
    "date has passed."
)
DECISION_CAPABILITY = (
    "ANSWERED in part, by depth pricing. The staff keys are sold inside the "
    "capability catalogue's teachers module: school.teachers at Core, and "
    "school.staff_records and school.leave at Plus, so a school buys "
    "qualifications, documents and leave as depth on top of the register. What "
    "a school holding no teachers capability may still do is the entitlement "
    "layer's answer rather than this module's, and M13 and M14 carry the same "
    "question for their own surfaces."
)


# ── section 15, MRD traceability, and the change log ────────────────────────

TRACE_HEADING = "15. MRD Traceability"
TRACE_LEAD = (
    f"MRD v{MRD_VERSION} records Module 12 as Staff Management, Phase V1, "
    "Backend Partial, In use Partial, code schools.vs_staff mounted at "
    "/v1/i/me/staff/, with ten capability entries. Each maps below to the "
    "requirements that deliver it and its state at the code baseline. Three "
    "requirements trace to no entry, bulk import (FR-016), bulk role grant "
    "(FR-019) and staff search (FR-021), because the tracker lists no search or "
    "bulk capability for this module."
)
TRACE_HEADER = ["MRD capability", "Requirements", "State at 13675db"]
TRACE_ROWS = [
    ["Staff profile linked to identity", "FR-001, FR-003, FR-004",
     "Implemented. One record per account, written in the account's own "
     "transaction by the staff create, the staff import and Module 1's "
     "administrator provisioning; migration 0005 gave one to every account at a "
     "school holding an active grant and none. Activation promotes INVITED to "
     "ACTIVE in the same transaction."],
    ["Employment status and effective dates", "FR-008, FR-012",
     "Implemented. Five stored statuses moved by logged transitions carrying an "
     "effective date, a reason and a last working day where one is required. On "
     "Leave is read from approved leave rather than stored. Nobody moves their "
     "own record."],
    ["School and branch assignments", "FR-010, FR-014",
     "Implemented. One posting or school-wide, mirrored onto the account; reach "
     "derived from grants, including the same role at two branches. The branch "
     "roster reads three labelled groups, keeps people who have left as "
     "finished rows and names the role behind every reach. A bulk move refuses "
     "anybody who has left; the single-record edit does not."],
    ["Department, position, and manager assignment", "None",
     "Not evidenced. The organogram is one platform-global tree whose "
     "PositionAssignment refuses any non-platform user, so a school's staff "
     "cannot hold a seat and no reporting line exists. Nothing here works "
     "around it."],
    ["Teaching and non-teaching classifications", "FR-002, FR-011, FR-017",
     "Implemented. Whether somebody teaches is whether they hold a teaching "
     "assignment, never a flag. An assignment is a main teacher or assisting, "
     "its class and subject are narrowed by the caller's branches, and coverage "
     "counts the two kinds of gap."],
    ["Qualification and document records", "FR-006, FR-007",
     "Implemented under school.staff_records.view and .update, sold at Plus. "
     "No verified state and no expiry."],
    ["Leave and availability indicators", "FR-013, FR-002",
     "Partial. Leave is applied for and decided on the workflow engine, and an "
     "approved request covering today makes the row read On Leave with the "
     "date the person is back. Whether somebody is free at a given hour is not "
     "evidenced, because no contract records hours."],
    ["Staff lifecycle and offboarding", "FR-008, FR-009, FR-020",
     "Implemented with limits. Termination deactivates the account and "
     "revocation closes an unused invitation; a resignation leaves the account "
     "open for an administrator to close, and nothing closes it or warns when "
     "the last working day passes."],
    ["Payroll linkage without duplicate employee records", "FR-015",
     "Implemented, without a backfill. A salary row can name the account it "
     "pays, resolved inside the school's own tenant, and the staff row carries "
     "user_id to send; rows written before the link stay unlinked."],
    ["Audit history and field-level access", "FR-012",
     "Partial. Every write emits a STAFF audit event and the profile history "
     "reads employment and account events together; a reversed leave decision "
     "leaves only the engine's entry. Field-level access is not applied: no "
     "field on the staff record is restricted by grant the way the pay figures "
     "are."],
]
TRACE_NOTE = (
    "Module 12 stays Backend Partial for three reasons the table shows: no "
    "reporting line exists for a school's staff, availability is known by the "
    "day and not by the hour, and no field on the staff record is restricted by "
    "grant. In use is Partial rather than Not started because other modules "
    "read this one: Module 1's administrator provisioning writes the staff "
    "record, Module 13's class teacher points at it, and the workflow engine's "
    "Dynamic Roles read the job title and contract type it holds. Module 14 is "
    "the consumer still missing, because its timetable points at the account "
    "and picks teachers by role."
)

MINOR_NOTE = (
    "Why version 2.3 is minor. The functional baseline is intact: the same six "
    "models, the same two status vocabularies, the same posting-and-reach "
    "separation and the same four boundaries. What changes is behaviour and its "
    "contracts: where On Leave comes from, who is given a record, who may act "
    "on their own, what the roster returns, and what a Dynamic Role may read. "
    "Section 15 is new because the tracker carries ten capability entries for "
    "this module and the document had no table that reconciled against them; "
    "it adds traceability and changes no requirement."
)

DESIGN_NOTE_OLD = (
    "The design's transition map omits ON_LEAVE to SUSPENDED; FR-008 keeps it, "
    "because suspending somebody who is on leave is a real event."
)
DESIGN_NOTE_NEW = (
    "The design's transition map omits a move from On Leave to Suspended, and "
    "the code needs none: somebody on leave is stored Active, so suspending "
    "them is the ACTIVE to SUSPENDED move, which is a real event."
)

CHANGE_SUMMARY = (
    "Reconciles the document with the staff code at 13675db. On Leave is read, "
    "not stored: an approved leave request covering today makes an Active "
    "person read On Leave with the date they are back, and nothing moves them "
    "there or back, because a leave ending fires no event and nothing here runs "
    "on a schedule. Migration 0003 moved every stored On Leave to Active, the "
    "lifecycle refuses it as a target, and the directory's row, filter and "
    "header count read one expression. A reversed leave decision returns the "
    "request to pending. A school's administrators are members of staff: "
    "Module 1's provisioning writes their record, a record for an account "
    "already in use starts Active, and migration 0005 gave one to every granted "
    "account that had none. Nobody can move their own employment record or "
    "suspend their own account (CANNOT_ACT_ON_SELF). The branch roster keeps "
    "people who have left as finished rows, names the role behind every reach, "
    "lets school-wide people be given a branch and costs a fixed number of "
    "queries; a bulk move refuses anybody who has left (STAFF_HAS_LEFT). "
    "Teaching parts read Main teacher and Assisting. A class teacher and a "
    "taught subject are narrowed by the caller's branches. Search finds a full "
    "name typed in either order, in the directory and the palette alike. The "
    "leave document and the staff record give Dynamic Roles four condition "
    "fields. A one-off command, since removed, narrowed eleven teacher grants "
    "at holy-cross from the whole school to their holder's posting and left "
    "eight alone, by the record of the commit that removed it. The control "
    "page, the permission table, sections 8.4, 10 and 13 and section 3.4's "
    "clash and last-working-day rows stop describing the module as unbuilt; "
    "FR-001 records how a grant's reach is chosen and that the form carries no "
    "documents; FR-021's empty result is a list; section 15 adds MRD "
    "traceability; three rows set in the wrong typeface are restyled. "
    "schools.vs_staff ran 218 tests OK at 6611856, before the Dynamic Role "
    "fields, which no test exercises. Backend evidence only; nothing here is "
    "deployed."
)


# ═════════════════════════════════════════════════════════════════════════════

def patch(source: Path, output: Path) -> None:
    doc = Document(str(source))
    title = f"XVS M12 Staff Management Functional Requirements Document v{TARGET}"

    # Every table this revision touches, bound by content before anything is
    # inserted, because an insertion shifts every later index.
    control = table_with_header(doc, "Document Type")
    corrections = table_with_header(doc, "What version 1.0 said")
    impossible = table_with_header(doc, "What is asked for")
    vocabulary = table_with_header(doc, "", "Employment status", "Account status")
    mapping = table_with_header(doc, "Employment transition")
    profile = table_with_row(doc, "employment_status")
    leave = table_with_row(doc, "leave_type")
    teaching = table_with_row(doc, "part")
    keys_47 = table_with_header(doc, "Key", "Default holders")
    keys_81 = table_with_header(doc, "Key", "State")
    api = table_with_header(doc, "Method and path")
    validation = table_with_header(doc, "Condition", "Status", "Code")
    dependencies = table_with_header(doc, "Dependency", "Needed by")
    decisions = table_with_header(doc, "Question")
    change_log = table_with_header(doc, "Version", "Date", "Summary")
    fr = {label: requirement(doc, label) for label in (
        "FR-001", "FR-002", "FR-003", "FR-008", "FR-009", "FR-010", "FR-011",
        "FR-013", "FR-017", "FR-021",
    )}
    trace_table_xml = copy.deepcopy(dependencies._tbl)

    # Every body paragraph this revision touches, bound the same way.
    dependency_heading = body_paragraph(doc, "13. Dependencies That Do Not Exist Yet")
    dependency_lead = body_paragraph(doc, "Version 2.0 opened this section")
    change_heading = body_paragraph(doc, "15. Change Log")
    keys_note = callout(doc, "Three keys a school already holds reach nothing")
    import_lead = body_paragraph(doc, "The import engine is finished")
    api_lead = body_paragraph(doc, "Everything under /v1/i/")
    leave_indexes = body_paragraph(doc, "Indexes: (tenant, staff, start_date)")
    terminal = body_paragraph(doc, "A terminal status is terminal")
    fr014 = body_paragraph(doc, "A person with two branch-pinned grants")
    roster_bullet = body_paragraph(doc, "A roster returns three labelled groups")
    creation_bullet = body_paragraph(doc, "Creating a person writes an account")
    on_leave_bullet = body_paragraph(doc, "A leave record whose dates cover today")
    leave_engine = body_paragraph(doc, "A leave request is created at PENDING")
    search_bullet = body_paragraph(doc, "The staff search returns the same people")
    decisions_note = callout(doc, "Nothing in this list blocks the build")
    minor_note = callout(doc, "Why version 2.1 is minor")
    design_note = callout(doc, "Three smaller places where the design")

    # ── control page ──
    set_cell_text(value_cell(control, "Version"), TARGET)
    set_cell_text(value_cell(control, "Date"), REVIEW_DATE)
    set_cell_text(value_cell(control, "Supersedes"), f"v{SOURCE}")
    set_cell_text(value_cell(control, "Status"), CONTROL_STATUS)
    set_cell_text(value_cell(control, "Verified against"), CONTROL_VERIFIED)
    insert_row_after(
        control, row_where(control, "Supersedes", exact=True),
        ["Source MRD", CONTROL_SOURCE_MRD],
    )
    # v2.2 wrote these values in the shared helper's typeface; rebuild them from
    # a row the document drew itself.
    native = row_where(control, "Module", exact=True)
    assert_native(native.cells[1])
    for label in ("Version", "Date", "Supersedes", "Source MRD"):
        restyle_row(control, row_where(control, label, exact=True), native)

    # ── sections 2 to 8 ──
    row = row_where(corrections, "ON_LEAVE is derived from active LeaveRecord dates")
    set_cell_text(row.cells[1], CORRECTION_ON_LEAVE)
    set_cell_text(row.cells[2], CORRECTION_ON_LEAVE_EVIDENCE)

    row = row_where(impossible, "An employment status that changes itself")
    set_cell_text(row.cells[1], IMPOSSIBLE_SELF_CHANGING_WHY)
    set_cell_text(row.cells[2], IMPOSSIBLE_SELF_CHANGING_INSTEAD)
    row = row_where(impossible, "A timetable clash, on the teaching-duties screen")
    set_cell_text(row.cells[1], IMPOSSIBLE_CLASH_WHY)
    set_cell_text(row.cells[2], IMPOSSIBLE_CLASH_INSTEAD)

    set_cell_text(row_where(vocabulary, "Values", exact=True).cells[1], VOCABULARY_VALUES)
    set_cell_text(
        row_where(vocabulary, "How it is enforced", exact=True).cells[1],
        VOCABULARY_ENFORCED,
    )
    set_cell_text(row_where(vocabulary, "Who changes it", exact=True).cells[1], VOCABULARY_WHO)

    row = row_where(mapping, "ACTIVE to ON_LEAVE, and back", exact=True)
    for cell, value in zip(row.cells, MAPPING_LEAVE):
        set_cell_text(cell, value)
    set_cell_text(
        row_where(mapping, "ACTIVE or ON_LEAVE to SUSPENDED", exact=True).cells[0],
        "ACTIVE to SUSPENDED, including somebody who reads On Leave",
    )
    row = row_where(mapping, "ACTIVE, ON_LEAVE or SUSPENDED to RESIGNED", exact=True)
    set_cell_text(row.cells[0], "ACTIVE or SUSPENDED to RESIGNED")
    set_cell_text(row.cells[2], MAPPING_RESIGNED_SAYS)
    set_cell_text(
        row_where(mapping, "ACTIVE, ON_LEAVE or SUSPENDED to TERMINATED", exact=True).cells[0],
        "ACTIVE or SUSPENDED to TERMINATED",
    )

    set_cell_text(
        row_where(profile, "employment_status", exact=True).cells[1],
        PROFILE_EMPLOYMENT_STATUS,
    )
    set_cell_text(row_where(leave, "status", exact=True).cells[1], LEAVE_STATUS_FIELD)
    set_cell_text(row_where(leave, "decided_at", exact=True).cells[1], LEAVE_DECIDED_AT_FIELD)
    replace_in_paragraph(leave_indexes, LEAVE_INDEXES_OLD, LEAVE_INDEXES_NEW)
    set_cell_text(row_where(teaching, "part", exact=True).cells[1], TEACHING_PART_FIELD)

    # The two permission rows v2.2 wrote in the shared helper's typeface.
    template = keys_47.rows[len(keys_47.rows) - 2]
    assert_native(template.cells[0])
    restyle_row(keys_47, keys_47.rows[len(keys_47.rows) - 1], template)
    template = keys_81.rows[len(keys_81.rows) - 2]
    assert_native(template.cells[0])
    restyle_row(keys_81, keys_81.rows[len(keys_81.rows) - 1], template)

    for key, (state, use) in KEY_STATES.items():
        row = row_where(keys_81, key, exact=True)
        set_cell_text(row.cells[1], state)
        if use is not None:
            set_cell_text(row.cells[4], use)
    set_callout_text(keys_note, KEYS_NOTE)
    set_run_text(import_lead, IMPORT_LEAD)

    # ── section 9 ──
    cell = value_cell(fr["FR-001"], "Postconditions")
    replace_in_cell(cell, FR001_GRANT_OLD, FR001_GRANT_NEW)
    replace_in_cell(cell, FR001_CARRIED_OLD, FR001_CARRIED_NEW)
    append_to_cell(value_cell(fr["FR-001"], "Business rules"), FR001_RULES_TAIL)
    replace_in_cell(value_cell(fr["FR-001"], "Atomicity"), FR001_ATOMIC_OLD, FR001_ATOMIC_NEW)

    set_cell_text(value_cell(fr["FR-002"], "Filters"), FR002_FILTERS)
    set_cell_text(value_cell(fr["FR-002"], "Counts"), FR002_COUNTS)
    set_cell_text(value_cell(fr["FR-002"], "Business rules"), FR002_RULES)

    replace_in_cell(value_cell(fr["FR-003"], "Business rules"), FR003_OLD, FR003_NEW)

    set_cell_text(value_cell(fr["FR-008"], "Allowed transitions"), FR008_TRANSITIONS)
    cell = value_cell(fr["FR-008"], "Business rules")
    replace_in_cell(cell, FR008_EFFECT_OLD, FR008_EFFECT_NEW)
    append_to_cell(cell, FR008_RULES_TAIL)

    append_to_cell(value_cell(fr["FR-009"], "Business rules"), FR009_RULES_TAIL)
    cell = value_cell(fr["FR-009"], "Refusals")
    set_cell_text(
        cell, cell.text.rstrip().rstrip(".")
        + "; 422 CANNOT_ACT_ON_SELF for a suspension of the caller's own account.",
    )

    replace_in_cell(value_cell(fr["FR-010"], "Business rules"), FR010_ROSTER_OLD, FR010_ROSTER_NEW)
    cell = value_cell(fr["FR-010"], "Refusals")
    set_cell_text(
        cell, cell.text.rstrip().rstrip(".")
        + "; 422 STAFF_HAS_LEFT from the bulk move, naming everybody selected who "
        "has left.",
    )

    set_cell_text(value_cell(fr["FR-011"], "Preconditions"), FR011_PRECONDITIONS)
    replace_in_cell(value_cell(fr["FR-011"], "Business rules"), FR011_PART_OLD, FR011_PART_NEW)

    cell = value_cell(fr["FR-013"], "Business rules")
    replace_in_cell(cell, FR013_STATUS_OLD, FR013_STATUS_NEW)
    replace_in_cell(cell, FR013_LEAVE_OLD, FR013_LEAVE_NEW)
    append_to_cell(cell, FR013_RULES_TAIL)

    replace_in_cell(
        value_cell(fr["FR-017"], "Business rules"), FR017_HEADLINE_OLD, FR017_HEADLINE_NEW,
    )
    set_cell_text(value_cell(fr["FR-021"], "Business rules"), FR021_RULES)

    # ── sections 10 to 12 ──
    replace_in_paragraph(api_lead, API_LEAD_OLD, API_LEAD_NEW)
    for path, text in API_ROWS.items():
        set_cell_text(row_where(api, path, exact=True).cells[2], text)

    anchor = row_where(validation, "An employment transition to LOCKED")
    for values in VALIDATION_AFTER_LOCKED:
        anchor = insert_row_after(validation, anchor, values)
    anchor = row_where(validation, "A posting move for somebody with assignments")
    for values in VALIDATION_AFTER_POSTING:
        anchor = insert_row_after(validation, anchor, values)

    anchor = terminal
    for text in ACCEPT_AFTER_TERMINAL:
        anchor = insert_paragraph_after(anchor, text)
    replace_in_paragraph(fr014, ACCEPT_FR014_OLD, ACCEPT_FR014_NEW)
    anchor = roster_bullet
    for text in ACCEPT_AFTER_ROSTER:
        anchor = insert_paragraph_after(anchor, text)
    anchor = creation_bullet
    for text in ACCEPT_AFTER_CREATION:
        anchor = insert_paragraph_after(anchor, text)
    set_run_text(on_leave_bullet, ACCEPT_ON_LEAVE)
    anchor = leave_engine
    for text in ACCEPT_AFTER_LEAVE_ENGINE:
        anchor = insert_paragraph_after(anchor, text)
    anchor = search_bullet
    for text in ACCEPT_AFTER_SEARCH:
        anchor = insert_paragraph_after(anchor, text)

    # ── sections 13 and 14 ──
    set_run_text(dependency_lead, DEPENDENCY_LEAD)
    for first, (name, needed, state) in DEPENDENCY_ROWS.items():
        row = row_where(dependencies, first, exact=True)
        if name is not None:
            set_cell_text(row.cells[0], name)
        if needed is not None:
            set_cell_text(row.cells[1], needed)
        set_cell_text(row.cells[2], state)
    append_to_cell(
        row_where(dependencies, "A leave approval template and its approver group").cells[2],
        DEPENDENCY_LEAVE_TAIL,
    )

    append_to_cell(row_where(decisions, "2. Is a job title").cells[1], DECISION_TITLE_TAIL)
    replace_in_cell(
        row_where(decisions, "5. What happens on the last working day?").cells[1],
        DECISION_EXIT_OLD, DECISION_EXIT_NEW,
    )
    set_cell_text(
        row_where(decisions, "11. Is staff management entitlement-gated?").cells[1],
        DECISION_CAPABILITY,
    )

    # ── section 15, cloned from section 13's heading, lead, table and a callout ──
    heading = copy.deepcopy(dependency_heading._p)
    lead = copy.deepcopy(dependency_lead._p)
    note = copy.deepcopy(decisions_note._p)
    for element in (heading, lead, trace_table_xml, note):
        change_heading._p.addprevious(element)
    set_run_text(Paragraph(heading, change_heading._parent), TRACE_HEADING)
    set_run_text(Paragraph(lead, change_heading._parent), TRACE_LEAD)
    set_callout_text(Paragraph(note, change_heading._parent), TRACE_NOTE)

    trace = Table(trace_table_xml, change_heading._parent)
    for cell, value in zip(unique_cells(trace.rows[0]), TRACE_HEADER):
        set_cell_text(cell, value)
    template = trace.rows[1]
    for extra in list(trace.rows)[2:]:
        extra._tr.getparent().remove(extra._tr)
    anchor = template
    for values in TRACE_ROWS:
        anchor = insert_row_after(trace, anchor, values)
    template._tr.getparent().remove(template._tr)

    set_run_text(change_heading, "16. Change Log")

    # ── change log, oldest first ──
    native = change_log.rows[len(change_log.rows) - 2]
    assert_native(native.cells[0])
    restyle_row(change_log, change_log.rows[len(change_log.rows) - 1], native)
    insert_row_after(
        change_log, change_log.rows[len(change_log.rows) - 1],
        [TARGET, SHORT_DATE, CHANGE_SUMMARY],
    )
    set_callout_text(minor_note, MINOR_NOTE)
    replace_in_paragraph(design_note, DESIGN_NOTE_OLD, DESIGN_NOTE_NEW)

    doc.core_properties.title = title
    doc.core_properties.version = TARGET
    output.parent.mkdir(parents=True, exist_ok=True)
    doc.save(str(output))
    update_extended_title(output, title)
    shrink_inherited_media(output)
    assert_no_em_dash(output)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", default=Path(__file__).resolve().parents[1])
    args = parser.parse_args()
    directory = Path(args.root) / "functional-requirements" / DIRECTORY
    output = directory / f"{STEM}_v{TARGET}.docx"
    if output.exists():
        raise SystemExit(f"{output.name} already exists; refusing to overwrite it")
    patch(directory / f"{STEM}_v{SOURCE}.docx", output)
    print(f"Wrote {DIRECTORY} v{TARGET}")


if __name__ == "__main__":
    main()

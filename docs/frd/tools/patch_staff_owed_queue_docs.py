#!/usr/bin/env python3
"""Cut M12 v2.14: the staff document catches up with the queued staff changes.

Several behaviour changes to Module 12 were committed while other revisions of
this document were being written, and none of them reached it. Each is checked
against the committed code (git show) and written in, not copied from the queue:

* 3e4e79af: a lockout is not a status. LOCKED is reported from the lockout row
  by ``User.account_state`` and never stored, so the directory's locked count
  and its ``?account_status=`` filter follow the row and stay disjoint; unlock
  clears the row and never the status, and a completed reset ends a lockout.
* 3189e0cb: the staff import's Send Invitation column, a person held back being
  PENDING with an unsent invitation, ``invitation_email_status`` on the list,
  and ``school.staff.import``.
* 196e300a, 4767f637, cf9cdbad: the staff personal details join Field Access,
  the switches decide what every staff surface shows and accepts, and the
  email is filtered in the account block nested in a record too.
* b0989d5d: at a live school a new member of staff starts as Teacher; a role or
  role_branch asking for anything else is refused with 400.
* 33473d77: a branch administrator reads shared staff and changes only their
  own (FR-025, new), the branch dimension is judged per viewer, and a
  school-wide class's teaching duties and class teacher are a school-wide
  administrator's.
* 13bd0da0: a second main teacher is refused in the screen's words.
* 447a6f0e: a staff ID is unique at its school whatever its case (migration
  0008), checked by one function on add, edit and import.
* e420d602: field exceptions are kept in history, so FR-022's last rule is no
  longer true.

Section 8.4's claim that an imported grant reaches the whole school is
corrected with them: it follows the row's branch, as FR-016 already said.

The MRD is not revised here. The traceability follows MRD v2.94, which keeps
Module 12 Partial for hourly availability alone: the staff record read as at an
earlier date is mapped to FR-022, which the table had left without a row, and
postings take the MRD's label, school and equal branch postings.

    python tools/patch_staff_owed_queue_docs.py
"""
from __future__ import annotations

from docx import Document

import patch_mrd_v2_79_docs as mrd_tools
from patch_document_type_labels_docs import require_newest
from patch_record_history_docs import (
    ROOT,
    add_fr,
    finish,
    frd_path,
    keep_format,
    log_change,
    set_control,
)
from patch_restricted_grant_ladder_docs import (
    append_to,
    assert_absent_outside_log,
    row_labelled,
    insert_row_after,
    table_headed,
)
from patch_staff_id_and_auth_events_docs import (
    edit_cell,
    edit_paragraph,
    edit_value,
    fr_table,
    insert_after,
    normalise_change_log,
    paragraph_starting,
    repair_ooxml,
    row,
    row_starting,
    set_value,
)

import patch_record_history_docs

REVIEW_DATE = "27 September 2026"
#: The MRD version the traceability follows.
MRD_VERSION = "2.94"
BASELINE = "e12e7fbf"
CODE_BASELINE = (
    f"Backend main at {BASELINE}, committed code only, 27 September 2026: the lockout, "
    "staff import, Field Access, starting role, branch write scope, main teacher and "
    "case-blind staff ID changes are all in it"
)

patch_record_history_docs.REVIEW_DATE = REVIEW_DATE

M12_DIR = "12-staff-management"
M12_STEM = "XVS_M12_Staff_Management_Functional_Requirements_Document"
M12_SOURCE, M12_TARGET = "2.13", "2.14"


# ── FR-025, new ──────────────────────────────────────────────────────────────

FR025_HEADING = "FR-025  A branch administrator changes only their own branch's people"
FR025_HEADER = "FR-025  A Branch Administrator Changes Only Their Own Branch's People"
FR025 = [
    ("Description",
     "Let a branch administrator read everybody who works at their branch, the school-wide "
     "people included, and change only the people who belong to their branches alone."),
    ("Permission",
     "Each route's own key; this requirement adds none. It narrows what a key already allows "
     "for a caller whose reach is a set of branches, and never touches a whole-tenant caller."),
    ("Scoping",
     "Reading stays inclusive (FR-002): a branch-bound caller reads their own branches' people "
     "and the school-wide ones. Changing does not: they may change a record only when every "
     "posting of that person sits inside their branches (services.scoping.caller_manages, over "
     "vs_rbac.scoping.caller_may_change). A school-wide person, or one also posted to a branch "
     "the caller does not cover, is read-only to them. A person still changes their own record "
     "under the self-edit rules of FR-004."),
    ("Business rules",
     "(1) Every staff write refuses a read-only person with 403 SHARED_RECORD_READ_ONLY: the "
     "record PATCH, the posting, the lifecycle move, the four account actions, the resend, the "
     "invitation revoke, qualifications, documents, leave filed or changed for somebody else, "
     "an organogram appointment, the bulk posting move and the bulk role grant, where one "
     "read-only person refuses the whole selection and is named. 403 rather than 404, because "
     "the caller can already open the record. (2) A posting stays inside the caller's branches: "
     "naming another branch, or school-wide, answers 403 BRANCH_OUTSIDE_REACH. A new person "
     "added with no posting by a caller who covers exactly one branch is filed under it. "
     "(3) Every directory, roster and record row carries can_manage, so a screen can leave out "
     "a control the server would refuse. (4) The branch dimension is judged per viewer: "
     "multi_branch on the staff endpoints is false for somebody who works in one branch, "
     "whether the school has one branch or several, so branch_name and posted_school_wide read "
     "null, the branch filter is ignored, the header breaks down by role instead of by branch, "
     "and the roster answers 404. Imports still read the school-level rule, because a file's "
     "branch column is a fact about the school. (5) The roster answers only for a branch the "
     "caller works in, and any other answers 404, the same as an unknown branch. (6) Role "
     "grants keep the same line through Module 4's grant reach rule: a branch-bound caller "
     "grants, changes, replaces or revokes only a grant whose reach is non-empty and inside "
     "their branches, held by somebody posted only there, so a school-wide grant from them is "
     "refused, on the role-assignment endpoints and on the staff bulk grant alike, with 400 on "
     "the branch field. (7) A teaching duty and a class teacher are judged by the class rather "
     "than by the person (FR-011), so a branch administrator may give their own class a "
     "school-wide teacher whose record stays read-only to them."),
    ("Acceptance",
     "(1) A branch administrator at Ikeja opens the school-wide registrar and every write on "
     "her answers 403 SHARED_RECORD_READ_ONLY; somebody also posted to Lekki is read-only too; "
     "their own branch's people stay editable; a school-wide administrator still manages the "
     "registrar; and the directory marks which rows the viewer may change "
     "(SharedRecordsAreReadOnlyTests). (2) Moving somebody to another branch, making them "
     "school-wide, claiming the school-wide registrar for one branch, or moving a posting "
     "through the record edit is refused, and a new person is filed under the caller's branch "
     "(PostingsStayInsideTheCallersBranchesTests). (3) A school-wide grant from a branch "
     "administrator, or any grant to the school-wide registrar, is refused, and a grant pinned "
     "to their own branch is allowed (RoleGrantsStayInsideTheCallersBranchesTests). (4) A "
     "one-branch viewer gets no posting fields and no roster, and a viewer covering two "
     "branches reads only their rosters (PostingDimensionIsPerViewerTests, in "
     "schools.vs_staff.tests.test_branch_write_scope)."),
]


# ── Field Access over the staff record (FR-023) ──────────────────────────────

FR023_RULES = (
    " (6) The personal details are the date of birth, gender, phone and email: the date of "
    "birth is a column on the staff record and the other three are read from the account, and "
    "a switch covers the name the client receives either way. (7) The resource reads Staff in "
    "the access tree, though its keys say teachers, because the register covers the bursar "
    "and the registrar too. Every field is declared open, so no role lost a field the day the "
    "switches began to decide. (8) The switches decide on every staff surface: the directory "
    "row, the record, the create and update responses, and the account block nested in a "
    "record, which carries the email a second time. A field a role may not read is absent, "
    "with no placeholder. One it may read and not change is named in _read_only_fields. A "
    "submitted field it may not write is refused 403 field_write_denied naming every such "
    "field, and nothing is saved. An unchanged value echoed back by a caller who may read it, "
    "and an empty value on create, are dropped rather than refused. (9) The first name, last "
    "name and email are open on create, because every new account needs them: a role without "
    "Write on the email still adds a person, and afterwards the email changes only through "
    "FR-009, which asks the same switch. The switches a caller holds reach the client as "
    "field_access on /me and the login response (Module 4)."
)
FR023_ACCEPTANCE = (
    " (4) A registrar with the email hidden still adds a member of staff, and the record, its "
    "account block included, carries the address nowhere for them, while an administrator "
    "with it open reads both copies (StaffEmailFieldAccessTests)."
)


# ── Section 11 refusals ──────────────────────────────────────────────────────

NEW_REFUSALS = [
    ["A role_branch sent to the staff create at a live school", "400",
     "Field error on role_branch: the grant reaches as far as the posting, and a role "
     "administrator changes it afterwards. Nobody is created."],
    ["A staff field the caller's Field Access may not write, on create or update", "403",
     "field_write_denied, naming every such field. Existing, platform-wide. Nothing is saved "
     "(FR-023)."],
    ["A branch-bound caller changing somebody their branches do not wholly hold, or the "
     "teaching duties or class teacher of a school-wide class", "403",
     "SHARED_RECORD_READ_ONLY. The caller can already read the row, so refusing to name it "
     "hides nothing (FR-011, FR-025)."],
    ["A branch-bound caller posting somebody to another branch, or school-wide", "403",
     "BRANCH_OUTSIDE_REACH, naming the branch (FR-025)."],
    ["A branch-bound caller granting a role that reaches outside their branches, or the "
     "whole school", "400",
     "Field error on branch, from Module 4's grant reach rule (FR-019, FR-025)."],
    ["A roster asked for a branch the caller does not work in", "404",
     "NOT_FOUND, the same answer as an unknown branch (FR-025)."],
]


M12_SUMMARY = (
    "Minor revision. Brings eight queued staff changes into the document, each checked "
    "against the committed code. A lockout is not a status: LOCKED is reported from the "
    "lockout row and never stored, the directory's locked count and account_status filter "
    "follow the row and empty themselves when the window passes, unlock clears the row and "
    "leaves a suspension alone, and a completed reset ends a lockout (section 5.1, FR-002, "
    "FR-009). The staff import carries a Send Invitation column, No creating the person with "
    "an unsent invitation the staff list reports as invitation_email_status, and the staff "
    "dataset has a key of its own, school.staff.import (FR-016, section 8). A role's Field "
    "Access switches decide which staff fields it reads and writes on every staff surface, "
    "the account block's copy of the email included, and the personal details and the email's "
    "open-on-create rule are written down (FR-023). At a live school a new member of staff "
    "starts on the teacher role, reaching as far as their posting, and a role or role_branch "
    "asking for anything else is refused with 400, where this document said 422 (FR-001). A "
    "branch administrator reads the school-wide people and changes only those posted to their "
    "branches alone, answered 403 SHARED_RECORD_READ_ONLY or BRANCH_OUTSIDE_REACH, with "
    "can_manage on every row and the branch dimension judged per viewer (FR-025, new). A "
    "school-wide class's teaching duties and class teacher are a school-wide administrator's "
    "(FR-011), and a second main teacher is refused in the screen's words. A staff ID is "
    "unique at its school whatever its case, by uq_staff_number_per_tenant_ci (migration "
    "0008), checked by one function on the add, the edit and the import, which also refuses "
    "one ID on two rows of a file (447a6f0e; section 7.1, FR-001, FR-004, FR-016). Field exceptions "
    "are kept in history and read as at a day on Module 4's endpoint (FR-022). Also "
    "corrected: section 8.4 said an imported grant reached the whole school, where it "
    "follows the row's branch, and that an upload is never filed under a branch, where the "
    "upload view files it through raised_branch; FR-013 and the leave routes named "
    "school.leave.cancel for filing and correcting somebody else's leave, which is "
    "school.leave.update, and .update for cancelling, which is school.leave.cancel, now in "
    "the section 8.1 key table; and a duplicate email and a duplicate staff number are field "
    "errors at 400, not 422 (FR-001, section 11). Section 13 records that school.staff.import "
    f"sits in no plan band. Documented from committed code at {BASELINE}. The test "
    "evidence is each change's own recorded run (schools.vs_staff 250 tests at 3189e0cb, 267 "
    "at b0989d5d, 288 at 33473d77, 384 at 447a6f0e), and no suite was rerun for this revision. The "
    f"traceability follows MRD v{MRD_VERSION}, which keeps Module 12 Partial for hourly "
    "availability alone: the staff record read as at an earlier date is mapped to FR-022, "
    "which the table had left without a row, so its eleven entries have eleven rows, and "
    "postings take the MRD's label, school and equal branch postings. Nothing here claims "
    "deployment."
)

#: The eleventh entry, which the table had left without a row.
M12_AS_AT_TRACE = [
    "Staff record read as at an earlier date",
    "FR-022",
    "Implemented. A staff profile and every tab on it read as they stood at the end of an "
    "earlier day, from the versions vs_history keeps for the record, its postings, the "
    "account, the role grants and the permission exceptions. A day before the history starts "
    "answers 409 HISTORY_NOT_KEPT, and Field Access applies to a past view as to the present, "
    "with the field exceptions of that day.",
]


def patch_m12() -> None:
    require_newest(str(ROOT / "functional-requirements" / M12_DIR / f"{M12_STEM}_v*.docx"),
                   M12_SOURCE)
    doc = Document(str(frd_path(M12_DIR, M12_STEM, M12_SOURCE)))
    set_control(doc, "Version", M12_TARGET)
    set_control(doc, "Date", REVIEW_DATE)
    set_control(doc, "Supersedes", f"v{M12_SOURCE}")
    set_control(doc, "Verified against", CODE_BASELINE)
    set_control(doc, "Source MRD", f"XVS Module Requirements Document v{MRD_VERSION}")

    # Section 3.4: a restricted role cannot even be named on a live school's create.
    refusals_34 = table_headed(doc, "What is asked for", "Why it cannot be done",
                               "What a school gets instead")
    restricted = row_starting(refusals_34, "Adding a person with a restricted role")
    edit_cell(restricted.cells[1], "Creation has no approval ladder behind it,",
              "At a live school the create grants only the starting role, Teacher, and "
              "refuses any other (FR-001); while the school is PENDING it grants School Admin "
              "or Branch Admin. Creation has no approval ladder behind it,")
    edit_cell(restricted.cells[2], "Add the person with a role that carries no restricted "
              "permission,",
              "Add the person, who starts as Teacher at a live school,")

    # Section 4.2 and 4.3: the branch dimension per viewer, and reading against changing.
    edit_paragraph(doc, "A school with exactly one branch sees the dimension recede",
                   "and no gap where any of them were.",
                   "and no gap where any of them were. The dimension is judged per viewer: "
                   "somebody who works in one branch of a school that has several sees it "
                   "recede in the same way, because whether a person they read is posted to "
                   "their branch or school-wide changes nothing they can do there (FR-025).")
    insert_after(doc, "Holding a key is not the same as being scoped to a row.",
                 "Reading a row and changing it are scoped differently. Reading is inclusive: "
                 "a branch-bound caller reads their own branches' rows and the shared ones. "
                 "Changing is not: vs_rbac.scoping.caller_may_change lets a branch-bound "
                 "caller change a row only when every branch it belongs to is theirs, so a "
                 "shared row, or one that also belongs to a branch they do not cover, is "
                 "read-only to them and a write answers 403 SHARED_RECORD_READ_ONLY. A "
                 "whole-tenant caller changes anything they can read. FR-025 applies it to the "
                 "staff record and FR-011 to teaching duties.")

    # Section 4.7: the import keys.
    school_keys = table_headed(doc, "Key", "Default holders", "Use in version 2.1")
    edit_cell(row_starting(school_keys, "import.templates.view").cells[-1],
              "FR-016 needs no new import key, only a dataset the keys can reach.",
              "The staff dataset also has a key of its own, school.staff.import, which stands "
              "in for the wizard's engine keys on a staff batch (section 8.1, FR-016).")

    # Section 5.1: the account half, with the lockout as a row rather than a status.
    vocab = table_headed(doc, "", "Employment status", "Account status")
    edit_cell(row(vocab, "Values").cells[2],
              "DRAFT, PENDING_APPROVAL, PENDING (Pending Activation), ACTIVE, SUSPENDED, "
              "LOCKED (security), DEACTIVATED, REJECTED.",
              "DRAFT, PENDING_APPROVAL, PENDING (Pending Activation), ACTIVE, SUSPENDED, "
              "DEACTIVATED, REJECTED, stored. LOCKED (security), reported and never stored: "
              "User.account_state reads LOCKED while the account's lockout window is running, "
              "and the stored status underneath is untouched.")
    edit_cell(row(vocab, "How it is enforced").cells[2],
              "PASSWORD_STATUSES is {ACTIVE, PENDING, LOCKED, SUSPENDED}.",
              "PASSWORD_STATUSES is {ACTIVE, PENDING, LOCKED, SUSPENDED}. A lockout is a "
              "separate, timed refusal at sign-in, made by the AccountLockout row, which ends "
              "by itself when locked_until passes.")
    edit_cell(row(vocab, "Who changes it").cells[2],
              "The identity services: activation, suspension, reactivation, lockout, password "
              "reset.",
              "The identity services: activation, suspension, reactivation and password reset. "
              "A lockout writes no status: it is a row with an expiry, cleared early only by an "
              "unlock or a completed password reset, and never by reactivation.")
    edit_paragraph(doc, "📌  LOCKED is the value most often mistaken",
                   "It is a security lockout after failed sign-in attempts, cleared by a reset.",
                   "It is a security lockout after failed sign-in attempts, and it is never "
                   "stored: AccountLockout.locked_until holds the moment it ends, the window is "
                   "fixed when it opens so further guesses cannot push it out, and "
                   "User.account_state reports LOCKED over the stored status only while that "
                   "moment is ahead. It ends by itself, or earlier by an unlock or a completed "
                   "password reset, and a suspension underneath it survives both.")

    # Section 6.3: the dimension recedes per viewer.
    edit_paragraph(doc, "At a school with one branch, none of this is rendered.",
                   "Not disabled: absent.",
                   "Not disabled: absent. The same holds for a viewer who works in one branch "
                   "of a school with several (FR-025).")

    # Section 8.1: the staff import key.
    keys = table_headed(doc, "Key", "State", "Sensitivity", "Default holders", "Use")
    insert_row_after(row_starting(keys, "school.staff_records.view"), [
        "school.staff.import", "Seeded", "SENSITIVE", "school_admin, branch_admin",
        "Load staff from a spreadsheet: FR-016. A resource of its own, school.staff, because "
        "a key minted new can say what it governs, and bundled in Staff Records beside the "
        "register keys. branch_admin holds it because a staff file adds people to a branch the "
        "uploader already staffs one at a time, where a roll import rewrites children's "
        "records across the school.",
    ])
    edit_paragraph(doc, "This module needs eight keys",
                   "and five more for the organogram, a resource of its own because every "
                   "member of staff reads it.",
                   "five more for the organogram, a resource of its own because every member "
                   "of staff reads it, and school.staff.import for the staff file, which "
                   "governs the import alone.")

    # Section 8.4: the import's grant and its template.
    edit_paragraph(doc, "The import engine is finished and the staff dataset is built",
                   "An imported person's role is granted the way FR-001's is when role_branch "
                   "is absent: school-wide, whatever branch the file names for their posting.",
                   "An imported person's role reaches the branch the row posts them to, "
                   "through the same choke point FR-001 uses, and a row leaving the branch "
                   "blank is granted across the school. A Send Invitation column decides, row "
                   "by row, whether the invitation email goes out (FR-016).")
    places = table_headed(doc, "Where", "What changes")
    keep_format(
        row_starting(places, "core/management/commands/seed_import.py").cells[-1],
        "A staff_master_v1 template with typed columns: First Name, Middle Name, Last Name, "
        "Email, Phone, Gender, Staff ID, Job Title, Employment Type, Hire Date, Branch, Role "
        "and Send Invitation. Required: First Name, Last Name, Email and Role. The Role column "
        "names a role inside THIS school, by key or by name, resolved per row, never a "
        "platform role. build.sh runs seed_import on every deploy, so a template that gains a "
        "column gains it wherever the build runs; a renamed template code is warned about "
        "rather than retired.")

    # Section 9 and FR-001: the starting role.
    edit_paragraph(doc, "Twenty-four requirements.", "Twenty-four requirements.",
                   "Twenty-five requirements.")
    fr001 = fr_table(doc, "FR-001")
    edit_value(fr001, "Preconditions",
               "A role is named and is active in this school's catalogue.",
               "At a live school the catalogue carries an active role whose key is teacher, "
               "which every new member of staff starts with; while the school is PENDING a "
               "role is named and is School Admin or Branch Admin.")
    edit_value(fr001, "Postconditions",
               "a TenantUserRoleAssignment for the named role, reaching the branch the person "
               "is posted to unless the role template is tied to a branch of its own or "
               "role_branch names one, and reaching the whole school where role_branch is the "
               "word school;",
               "a TenantUserRoleAssignment for the teacher role at a live school, or for the "
               "named administrator role while the school is PENDING, reaching the branch the "
               "person is posted to unless the role template is tied to a branch of its own, "
               "or, while PENDING, role_branch names one or is the word school;")
    rules = row(fr001, "Business rules").cells[-1]
    edit_cell(rules, "(3) A role is required; there is no invite-now-decide-later.",
              "(3) Nobody is created without a role, and at a live school the adder does not "
              "choose it: school.teachers.create is not school.roles.assign, so the key that "
              "adds somebody does not decide what they may reach. Everybody starts on the "
              "school's teacher role, matched on its key because the name is the school's to "
              "rename, and a role administrator widens, narrows or replaces it afterwards "
              "(FR-005, FR-019). A body naming the teacher role is accepted; one naming any "
              "other role, or carrying any role_branch, is refused with 400 on that field "
              "rather than ignored, and creates nobody. A live school with no active teacher "
              "role is refused with 400 on role, naming the role to restore, rather than "
              "creating an account that reaches nothing. The list response carries "
              "starting_role: the value teacher and the school's own name for it at a live "
              "school, and null while PENDING, which is how an Add form knows it need not "
              "ask.")
    edit_cell(rules, "The narrowing lifts at go-live.",
              "A PENDING POST that names no role is refused with 400: Choose School Admin or "
              "Branch Admin for them. At go-live the choice itself ends and rule (3) applies.")
    edit_cell(rules, "(6) The posting is written to StaffProfile.branch and mirrored to "
              "User.branch in the same transaction.",
              "(6) The posting is written to StaffProfile.branch and mirrored to User.branch "
              "in the same transaction. A branch-bound caller posts only inside their own "
              "branches, and one who covers exactly one branch and names no posting files the "
              "person under it (FR-025).")
    edit_cell(rules, "role_branch names a branch to pin the grant there instead",
              "While the school is PENDING, role_branch names a branch to pin the grant there "
              "instead")
    edit_cell(rules, "carries the word school to ask for the whole school deliberately.",
              "carries the word school to ask for the whole school deliberately. At a live "
              "school role_branch is refused (rule 3), and a different reach is a role "
              "administrator's change afterwards.")
    edit_value(fr001, "Refusals",
               "422 on the role field for a role this school cannot assign, worded as the "
               "onboarding rule while the tenant is PENDING and as an invalid role afterwards;",
               "400 on the role field for a role this school cannot assign: while the tenant "
               "is PENDING, no role or one other than School Admin and Branch Admin, worded as "
               "the onboarding rule; at a live school, any role but the teacher role, or no "
               "active teacher role to start from; 400 on role_branch for any role_branch at a "
               "live school;")

    # FR-002: the lockout, the invitation's email status, and the viewer's scope.
    fr002 = fr_table(doc, "FR-002")
    append_to(fr002, "Scoping",
              " Reading is not changing: a school-wide person, or one also posted elsewhere, is "
              "listed read-only to a branch-bound caller, and each row's can_manage says which "
              "(FR-025).")
    edit_value(fr002, "Filters",
               "?account_status=, a SEPARATE filter and never merged with the first;",
               "?account_status=, a SEPARATE filter and never merged with the first, read as "
               "each row's account state: LOCKED finds whoever a lockout holds at this moment "
               "and every other value excludes them, so these facets are disjoint too;")
    edit_value(fr002, "Filters", "and ignored at a school with one branch;",
               "and ignored for a viewer who works in one branch, whether the school has one "
               "branch or several;")
    edit_value(fr002, "Counts",
               "By branch, at a school with more than one, and BY ROLE at a school with "
               "exactly one,",
               "By branch for a viewer who works in more than one branch, and BY ROLE for one "
               "who works in a single branch, whether the school has one branch or several,")
    edit_value(fr002, "Counts",
               "which is an account-status count sitting beside employment ones and must be "
               "labelled as such.",
               "which is an account-status count sitting beside employment ones and must be "
               "labelled as such. It counts running lockouts, read from the lockout row, so it "
               "empties itself when a window passes with nobody clearing anything.")
    edit_value(fr002, "Business rules",
               "and on_roll, false for somebody who has resigned or been terminated.",
               "and on_roll, false for somebody who has resigned or been terminated; "
               "account_status, the stored status with a running lockout laid over it "
               "(User.account_state), so the column, its chip and the filter name the same "
               "people; invitation_email_status, read off the invitation already prefetched "
               "and null where there is none, which is how a person imported with Send "
               "Invitation set to No is told apart from one who was emailed (FR-016); and "
               "can_manage (FR-025).")
    edit_value(fr002, "Business rules",
               "prefetch_related on the role assignments with their roles and on the "
               "invitation,",
               "prefetch_related on the role assignments with their roles, on the invitation "
               "and on the lockout,")

    # FR-009: one unlock.
    fr009 = fr_table(doc, "FR-009")
    edit_value(fr009, "Business rules",
               "a school confusing the two is a school that thinks its teacher was suspended.",
               "a school confusing the two is a school that thinks its teacher was suspended. "
               "Unlock is the one implementation the platform endpoint, this action and the "
               "lockout console all call: it clears the lockout row and never writes the "
               "status, so unlocking somebody who is also suspended leaves them suspended, and "
               "it is refused with 422 ACCOUNT_NOT_ELIGIBLE only when there is nothing to "
               "clear.")

    # FR-010: postings inside the caller's branches.
    fr010 = fr_table(doc, "FR-010")
    append_to(fr010, "Business rules",
              " (7) A branch-bound caller moves only people whose every posting is inside their "
              "branches, and only to their own branches; the roster answers only for a branch "
              "they work in (FR-025).")
    append_to(fr010, "Refusals",
              " 403 BRANCH_OUTSIDE_REACH for a branch-bound caller naming another branch or "
              "school-wide, and 403 SHARED_RECORD_READ_ONLY naming anybody in a bulk move who "
              "is read-only to them; 404 for a roster at a branch the caller does not work "
              "in.")

    # FR-011: a school-wide class, and the main teacher refusal.
    fr011 = fr_table(doc, "FR-011")
    edit_value(fr011, "Preconditions",
               "Either answers 404, and a school-wide class or subject stays every branch's to "
               "staff.",
               "Either answers 404. A school-wide class is read by every branch and changed by "
               "none of them: a branch-bound caller giving it a teaching duty, changing or "
               "removing one of its duties, or naming its class teacher is refused with 403 "
               "SHARED_RECORD_READ_ONLY, and a school-wide administrator staffs it. A "
               "school-wide subject may still be taught in a branch's own class.")
    edit_value(fr011, "Business rules",
               "is refused with 422 LEAD_ALREADY_SET naming the current lead,",
               "is refused with 422 LEAD_ALREADY_SET in the screen's words, naming the current "
               "main teacher and saying to move them to assisting first,")
    append_to(fr011, "Business rules",
              " (6) A duty and a class teacher are judged by the class and not by the person, "
              "so a branch administrator may give their own class a school-wide teacher whose "
              "record stays read-only to them (FR-025).")

    # FR-016: the staff file.
    fr016 = fr_table(doc, "FR-016")
    set_value(fr016, "Permission",
              "The seven engine keys school_admin already holds open the whole wizard, "
              "uploading included: import.templates.view, import.batches.view / .create / "
              ".run / .import, import.validations.view and import.jobs.view. The staff dataset "
              "also has a key of its own, school.staff.import, SENSITIVE, held by school_admin "
              "and branch_admin. It is registered with the engine for the staff dataset only, "
              "where it stands in for the wizard's engine keys on a staff batch the caller's "
              "branches reach: reading it, its file, issues and jobs, validating, importing "
              "and abandoning it. It never stands in for rolling back, deleting or rewriting a "
              "batch, and uploading a new file stays on the engine's batch key.")
    edit_value(fr016, "Business rules",
               "(2) The role column is a TenantRoleTemplate key inside THIS school, resolved "
               "per row, and a platform role key is not resolvable.",
               "(2) The Role column names a TenantRoleTemplate inside THIS school, by key or "
               "by name, resolved per row, and a platform role key is not resolvable. The file "
               "still chooses each person's role, where a single add grants the teacher role "
               "only (FR-001): the two paths disagree until the import follows the same rule.")
    edit_value(fr016, "Business rules",
               "(6) Invitations are queued for every created row, and the summary says how "
               "many.",
               "(6) A Send Invitation column takes Yes or No, and the spellings a school types "
               "for them (Y, True and 1, N, False and 0). Yes, a blank cell, or a file without "
               "the column invites as before. No creates the account and the staff record and "
               "parks a real invitation, unsent: the person is PENDING with an invitation whose "
               "email status is PENDING, not a separate state, and the existing resend sends "
               "it later. An answer that is neither warns and invites. Each row's result says "
               "whether that person was emailed, and the staff list reports "
               "invitation_email_status so a screen can find the people held back.")

    # FR-018: go-live ends the choice.
    fr018 = fr_table(doc, "FR-018")
    edit_value(fr018, "Business rules",
               "(2) The narrowing lifts at go-live with no further action.",
               "(2) At go-live the adder stops choosing a role: everybody added starts on the "
               "teacher role (FR-001).")

    # FR-019: the bulk grant stays inside the caller's branches.
    fr019 = fr_table(doc, "FR-019")
    edit_value(fr019, "Business rules",
               "so a branch admin cannot widen their reach by selecting rows they could not "
               "otherwise see.",
               "so a branch admin cannot widen their reach by selecting rows they could not "
               "otherwise see. A branch-bound caller may grant only a role whose reach sits "
               "inside their branches, never a school-wide one, and only to people posted "
               "there alone: anybody else in the selection refuses the whole grant with 403 "
               "SHARED_RECORD_READ_ONLY naming them, and a reach outside their branches "
               "answers 400 on the branch field (FR-025).")
    edit_value(fr019, "Refusals",
               "422 naming the role where the school's catalogue does not carry it or it is "
               "inactive.",
               "400 on the role field where the school's catalogue does not carry the role or "
               "it is inactive.")
    append_to(fr019, "Refusals",
              " 403 SHARED_RECORD_READ_ONLY naming a selected person a branch-bound caller may "
              "read and not change; 400 on the branch field for a reach outside their "
              "branches.")

    # FR-022: field exceptions now have a history.
    fr022 = fr_table(doc, "FR-022")
    edit_value(fr022, "Business rules",
               "(11) Field exceptions are not kept in history, so a past view does not show "
               "them.",
               "(11) Field exceptions are kept in history by vs_rbac, and Module 4's field "
               "exception list reads them as at a day, with no comparison against the role "
               "because a role's switches keep no history; the staff profile carries none of "
               "its own.")

    # FR-023: the whole contract.
    fr023 = fr_table(doc, "FR-023")
    append_to(fr023, "Description",
              " A role's switches decide what it reads and writes of each field, on every "
              "staff surface.")
    append_to(fr023, "Business rules", FR023_RULES)
    append_to(fr023, "Acceptance", FR023_ACCEPTANCE)

    add_fr(doc, FR025_HEADING, FR025_HEADER, FR025)

    # Section 10: routes.
    routes = table_headed(doc, "Method and path", "Permission", "Requirement")
    append_to(routes, "GET /v1/i/me/staff/",
              " The page carries counts, role_options, starting_role and multi_branch beside "
              "it.")
    edit_value(routes, "POST /v1/i/me/staff/",
               "role_branch is optional and decides how far the role grant reaches:",
               "At a live school the grant is the teacher role, reaching as far as the "
               "posting, and a role or role_branch asking for anything else is refused with "
               "400. While the school is PENDING, role is School Admin or Branch Admin and "
               "role_branch is optional and decides how far the role grant reaches:")
    edit_value(routes, "PATCH /v1/i/me/staff/teaching/<id>/",
               "The part: make lead, or step back.",
               "The part: make main, or move to assisting.")

    # Section 11: refusals.
    refusals = table_headed(doc, "Condition", "Status", "Code")
    role_refusal = row_starting(refusals, "A role this school cannot assign")
    for cell, text in zip(role_refusal.cells, [
        "A role this school cannot assign on a staff create: while PENDING, none or one other "
        "than School Admin and Branch Admin; once live, any but the teacher role, or no active "
        "teacher role to start from",
        "400",
        "Field error on role, in the words of the rule broken. Nobody is created.",
    ]):
        keep_format(cell, text)
    edit_cell(row_starting(refusals, "Promoting an assistant").cells[-1],
              "LEAD_ALREADY_SET, naming the current lead so the caller demotes deliberately.",
              "LEAD_ALREADY_SET, naming the current main teacher and saying to move them to "
              "assisting first, so the caller demotes deliberately.")
    anchor = refusals.rows[-1]
    for values in NEW_REFUSALS:
        anchor = insert_row_after(anchor, values)

    # Section 12: acceptance.
    insert_after(doc, "A branch admin pinned to Ikeja lists staff",
                 "A branch admin pinned to Ikeja reads the school-wide registrar and changes "
                 "nothing about her: every write answers 403 SHARED_RECORD_READ_ONLY, postings "
                 "stay inside Ikeja, and her row carries can_manage false (FR-025).")
    edit_paragraph(doc, "A person whose account is LOCKED has an unchanged employment status",
                   "and cannot be moved to LOCKED by any transition.",
                   "and cannot be moved to LOCKED by any transition. The directory's locked "
                   "count and its account_status filter follow the lockout row, so both empty "
                   "themselves when the window passes with nobody clearing anything, and "
                   "somebody locked out this minute is not also listed as ACTIVE "
                   "(DirectoryTests).")
    mrd_tools.retitle(
        paragraph_starting(doc, "A branch administrator at Lekki cannot make anybody the class "
                                "teacher"),
        "A branch administrator at Lekki cannot make anybody the class teacher of an Ikeja "
        "class or teach a Lekki class a subject Ikeja owns; naming the class teacher of a "
        "school-wide class, or giving it a teaching duty, answers 403 SHARED_RECORD_READ_ONLY; "
        "and their own class stays theirs to staff (BranchReachTests).")
    mrd_tools.retitle(
        paragraph_starting(doc, "A teacher added at Ikeja with role_branch left out"),
        "At a live school a new member of staff starts as Teacher without anybody choosing it, "
        "granted at Ikeja when posted to Ikeja and across the school when posted school-wide; "
        "naming the teacher role is accepted; naming any other role, or sending role_branch as "
        "the word school or as Lekki, is refused with 400 and creates nobody; a school with no "
        "active teacher role is told which role to restore; and the list names the starting "
        "role. Asserted at a school with two branches and at a school with one "
        "(AGrantFollowsThePostingTests).")
    edit_paragraph(doc, "At a tenant with exactly one branch, the posting field is absent",
                   "because a single-branch test proves nothing about the other shape.",
                   "because a single-branch test proves nothing about the other shape. The same "
                   "holds per viewer: somebody who works in one branch of a larger school reads "
                   "branch_name and posted_school_wide as null and is answered 404 by the "
                   "roster (PostingDimensionIsPerViewerTests).")
    edit_paragraph(doc, "A PENDING tenant may invite school_admin and branch_admin",
                   "Both halves read one queryset.",
                   "Both halves read one queryset. A PENDING POST naming no role is refused and "
                   "asks for School Admin or Branch Admin "
                   "(SchoolStaffEndpointTests.test_a_pending_school_must_choose_an_admin_role).")
    edit_paragraph(doc, "At a single-branch tenant the directory's side breakdown",
                   "asserted on both shapes.",
                   "asserted on both shapes. A viewer who works in one branch of a multi-branch "
                   "school reads the role breakdown too (FR-025).")
    edit_paragraph(doc, "A staff import of 200 rows",
                   "and writes an audit event per created row.",
                   "and writes an audit event per created row. A row saying No to Send "
                   "Invitation is created and not emailed, a blank cell or a missing column "
                   "invites, and the staff list tells the two apart (SendInvitationColumnTests, "
                   "HeldBackPeopleAreVisibleOnTheStaffListTests).")

    # Section 13: dependencies.
    deps = table_headed(doc, "Dependency", "Needed by", "Current state")
    edit_cell(row_starting(deps, "The new permission keys").cells[-1],
              "beside school.staff_records.view and .update,",
              "beside school.staff_records.view and .update and school.staff.import (FR-016), "
              "which no band names,")
    append_to(deps, "The staff import dataset",
              " A Send Invitation column lets a school hold a person's email back, and a person "
              "held back is PENDING with an unsent invitation.")

    # Section 15: traceability.
    trace = table_headed(doc, "MRD capability", "Requirements", "State at ce538c98")
    keep_format(trace.rows[0].cells[2], f"State at {BASELINE}")
    profile = row(trace, "Staff profile linked to identity")
    keep_format(profile.cells[2], profile.cells[2].text.rstrip() +
                " At a live school a new member of staff starts on the teacher role, reaching "
                "as far as their posting; the adder chooses neither.")
    insert_row_after(profile, M12_AS_AT_TRACE)
    assignments = row(trace, "School and branch assignments")
    keep_format(assignments.cells[0], "School and equal branch postings")
    keep_format(assignments.cells[1], "FR-010, FR-014, FR-025")
    keep_format(assignments.cells[2], assignments.cells[2].text.rstrip() +
                " A branch administrator reads the school-wide people and changes only those "
                "posted to their branches alone, and posts and grants only inside them.")
    teaching = row(trace, "Teaching and non-teaching classifications")
    edit_cell(teaching.cells[2],
              "its class and subject are narrowed by the caller's branches,",
              "its class and subject are narrowed by the caller's branches, a school-wide "
              "class is staffed only by a school-wide administrator,")
    audit = row(trace, "Audit history and field-level access")
    edit_cell(audit.cells[2], "and the whole record's fields are switchable per role.",
              "and the whole record's fields are switchable per role, with the switches "
              "enforced on every staff surface.")
    edit_paragraph(doc, "📌  Module 12 stays Backend Partial",
                   "Module 12 stays Backend Partial for two reasons the table shows: "
                   "availability is known by the day and not by the hour, and no field on the "
                   "staff record is restricted by grant.",
                   f"MRD v{MRD_VERSION} keeps Module 12 at Backend Partial for one reason: "
                   "availability is known by the day and not by the hour. Every field on the "
                   "staff record is under Field Access, and a role's switches decide what it "
                   "reads and writes on every staff surface (FR-023).")
    edit_paragraph(doc, f"📌  MRD v{MRD_VERSION} keeps Module 12",
                   "The third, that no reporting line existed for a school's staff, is closed "
                   "by FR-024.",
                   "That no reporting line existed for a school's staff, once a second reason, "
                   "is closed by FR-024.")
    edit_paragraph(doc, "MRD v2.93 records Module 12 as Staff Management", "MRD v2.93",
                   f"MRD v{MRD_VERSION}")
    if len(trace.rows) - 1 != 11:
        raise ValueError(f"Module 12 traceability holds {len(trace.rows) - 1} rows, not 11")

    # ── Corrections found while checking the queue ───────────────────────────

    # The leave keys: filing and correcting for somebody else is .update, and
    # cancelling is .cancel (views/leave.py).
    fr013 = fr_table(doc, "FR-013")
    edit_value(fr013, "Permission", "school.leave.cancel for somebody else",
               "school.leave.update for somebody else")
    edit_value(fr013, "Permission", "PATCH and DELETE school.leave.cancel.",
               "PATCH school.leave.update, DELETE school.leave.cancel.")
    keep_format(row(routes, "GET, POST /v1/i/me/staff/<id>/leave/").cells[1],
                "school.leave.view / .apply for your own or .update for somebody else's, "
                "according to the method")
    leave_detail = row(routes, "PATCH, DELETE /v1/i/me/staff/leave/<id>/")
    keep_format(leave_detail.cells[1], "school.leave.update / .cancel")
    keep_format(leave_detail.cells[2],
                "FR-013. PATCH corrects a pending request under .update; DELETE cancels it "
                "under .cancel.")
    leave_update = row_starting(keys, "school.leave.update")
    edit_cell(leave_update.cells[-1],
              "Record leave on somebody's behalf, correct a pending request and cancel one: "
              "FR-013.",
              "Record leave on somebody's behalf and correct a pending request: FR-013.")
    insert_row_after(leave_update, [
        "school.leave.cancel", "Seeded", "SENSITIVE", "school_admin, branch_admin",
        "Cancel a leave request, your own or anybody's: FR-013. Separate from .update, so "
        "correcting a request and withdrawing it can be given to different people. Like "
        ".update, it decides nothing: approval is the workflow engine's.",
    ])

    # Field errors are 400, not 422: core.exceptions renders a validation error
    # at 400, and the staff number test asserts it.
    edit_value(fr001, "Refusals",
               "422 with the error on the email field for a duplicate at this school;",
               "400 with the error on the email field for a duplicate at this school;")
    for start in ("A duplicate email at THIS school", "A duplicate staff number at this school"):
        keep_format(row_starting(refusals, start).cells[1], "400")

    # Section 8.4: an upload is filed by branch.
    mrd_tools.retitle(
        paragraph_starting(doc, "ℹ  One live gap in the engine will affect the staff import"),
        "ℹ  How an upload is filed by branch. The upload view files a new batch through "
        "vs_rbac.scoping.raised_branch: a caller bound to one branch stamps it with that "
        "branch, a caller covering several who names none files it as shared across the "
        "school, and a whole-tenant caller may name one or leave it school-wide. A "
        "branch-bound reader then sees their own branches' batches and the shared ones. For a "
        "staff import the batch's branch files the upload and nothing more: where each person "
        "is posted comes from the file's Branch column.")

    # Section 13: the staff import key sits in no plan band.
    append_to(deps, "A capability for staff",
              " LIMIT: school.staff.import sits in no band, so every school holds it whatever "
              "it pays, where school.students.import is sold with bulk import. In practice the "
              "upload still needs the engine's own batch key, which is sold at Plus, so the "
              "key alone opens no staff import below Plus; whether it should be banded with "
              "bulk import is undecided.")

    # A staff ID is unique whatever its case (447a6f0e, migration 0008).
    keep_format(row_labelled(doc, "staff_number").cells[-1],
                row_labelled(doc, "staff_number").cells[-1].text.replace(
                    "Unique per tenant among non-empty values, by a partial unique constraint,",
                    "Unique per tenant among non-empty values whatever their case, by a partial "
                    "unique constraint on the lowercased number, as admission numbers are,").replace(
                    "and two numbers at one school differing only in case sign nobody in "
                    "(M03 FR-024).",
                    "the same comparison the constraint makes (M03 FR-024)."))
    mrd_tools.retitle(
        paragraph_starting(doc, "Constraints: UniqueConstraint on (tenant, staff_number)"),
        "Constraints: UniqueConstraint on (Lower(staff_number), tenant) with condition "
        "~Q(staff_number=''), named uq_staff_number_per_tenant_ci, so BS/stf/0001 and "
        "BS/STF/0001 are one ID. Migration 0008 put it in place of the case-sensitive "
        "uq_staff_number_per_tenant after refusing, by name, any school already holding two IDs "
        "that differ only in case; reversing it restores the old constraint. The add form, the "
        "edit endpoint and the import all ask services.numbers.staff_number_taken first, so "
        "each names the field rather than failing at the constraint. UniqueConstraint on "
        "(user,) is the OneToOne itself.")
    edit_value(fr001, "Business rules",
               "(5) The staff number, if given, is unique within the tenant;",
               "(5) The staff number, if given, is unique within the tenant whatever its case;")
    append_to(fr_table(doc, "FR-004"), "Refusals",
              " 400 on staff_number for an ID somebody else at this school already holds, in "
              "any case.")
    edit_value(fr016, "Business rules",
               "(7) Pass the User object to the audit emitter rather than an id; section 4.5.",
               "(7) Pass the User object to the audit emitter rather than an id; section 4.5. "
               "(8) A staff ID is compared without case against the school's existing staff, "
               "and one ID on two rows of the same file refuses the second row as "
               "duplicate_in_file under the Staff ID column.")
    keep_format(row_starting(refusals, "A duplicate staff number at this school").cells[0],
                "A duplicate staff number at this school, in any case, on create or edit")
    insert_after(doc, "Creating a person writes an account, an invitation",
                 "A staff ID another person at the school holds in another case is refused on "
                 "create and on edit, and one ID on two rows of a file refuses the second row "
                 "(test_a_staff_number_differing_only_in_case_is_the_same_number, "
                 "test_editing_to_another_person_s_number_in_other_case_is_refused, "
                 "test_one_staff_id_twice_in_a_file_is_refused_on_the_second_row).")

    # The three notes closing the document travel as one block, so the last is
    # never left alone on a page of its own.
    for start in ("ℹ  Why version 2.5 is minor.", "ℹ  One disagreement between this document"):
        paragraph_starting(doc, start).paragraph_format.keep_with_next = True

    log_change(doc, M12_TARGET, M12_SUMMARY)
    assert_absent_outside_log(
        doc,
        "State at ce538c98",
        "School and branch assignments",
        "tracker's second reason",
        "MRD v2.93",
        "no field on the staff record is restricted by grant",
        "stays every branch's to staff",
        "make lead, or step back",
        "there is no invite-now-decide-later",
        "school-wide, whatever branch the file names",
        "No new import key",
        "Field exceptions are not kept in history",
        "Twenty-four requirements",
        "SUSPENDED, LOCKED (security), DEACTIVATED",
        "Invitations are queued for every created row",
        "422 on the role field",
        "PATCH and DELETE school.leave.cancel",
        "ImportBatch.branch is always NULL",
        "422 with the error on the email field",
        "correct a pending request and cancel one",
        "named uq_staff_number_per_tenant.",
        "differing only in case sign nobody in",
    )
    repair_ooxml(doc)
    normalise_change_log(doc)
    finish(doc, frd_path(M12_DIR, M12_STEM, M12_TARGET),
           f"{M12_STEM.replace('_', ' ')} v{M12_TARGET}", M12_TARGET)


def main() -> None:
    patch_m12()


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""Cut MRD v2.93, M12 v2.12, M07 v1.19 and M03 v1.18.1: a school draws its own organogram.

What changed in the backend, and therefore in the documents:

* schools.vs_staff keeps a school's own organogram (0e023329): org units tiered
  division, department and team, each school-wide or one branch's; posts that
  report to other posts along a solid line and take their unit's branch;
  effective-dated appointments of staff to posts, one current primary each; and
  dotted lines between posts. Fourteen routes under
  /v1/i/me/staff/organogram/, closed before go-live, behind five new keys,
  school.organogram.{view,create,update,delete,assign}. Every member of staff
  reads the whole school's chart; a branch administrator writes only their own
  branch's part; another school's id is 404. Resigning, termination and a
  revoked invitation end a person's appointments, so the post shows vacant and
  its reports stay under it for whoever is appointed next. The staff record
  carries its holder's place on the chart.
* An ORGANOGRAM approval stage climbs the school's own chart for a school
  requester (direct manager, N levels up, department head), registered from
  vs_staff so the engine imports no school app. A specific-post stage still
  reaches nobody for a school, because it points at the platform's posts.
* A staff photograph and a staff document are handed out as links signed for
  the reader (01badc15). They had been the storage's bare /media/ path, which
  the media view refuses, so every staff photograph fell back to initials and
  every document link failed.

M12 carried the organogram as its one capability not evidenced; it is now
implemented, and the module stays Partial for its other two reasons. M12's
acceptance criterion that a document payload carries "only the media path" is
the rule the media fix had to break, and is rewritten. Three stale claims are
corrected on the way: M12's requirement count, its refusal row saying a person
cannot be posted to two branches, and M03's limit calling an empty approver list
the engine's unsafe auto-skip default, which M07 v1.5 retired.

M04 was checked and left: the five keys band through the existing resource map
(teachers_core) and no RBAC rule changed.

    python tools/patch_school_organogram_docs.py
"""
from __future__ import annotations

import copy

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
    replace_cell,
    set_control,
    set_cover_version,
)
from patch_restricted_grant_ladder_docs import (
    append_to,
    assert_absent_outside_log,
    box_of,
    edit_blurb,
    edit_box,
    insert_row_after,
    row_labelled,
    table_headed,
)
from patch_staff_id_and_auth_events_docs import (
    edit_cell,
    edit_paragraph,
    fr_table,
    insert_after,
    normalise_change_log,
    paragraph_starting,
    remove_row,
    repair_ooxml,
    row,
)

import patch_record_history_docs

REVIEW_DATE, SHORT_DATE = "27 September 2026", "27 Sep 2026"
MRD_SOURCE, MRD_TARGET = "2.92", "2.93"
CODE_BASELINE = (
    "Backend main at 01badc15, which carries the school organogram (0e023329) and the signed "
    "staff photo and document links, 27 September 2026. The school app's organogram screens "
    "are committed (school-fe 403a90f) and pending release"
)
TEST_EVIDENCE = (
    "Verified by the schools.vs_staff suite (359 tests, 38 of them the organogram's and 3 the "
    "signed links'), vs_workflow (430), vs_rbac (892) and core's seeding tests (23), each app "
    "run on its own and all passing, and by driving the chart, the Manage screen and a staff "
    "profile against the running backend at desktop and phone widths. Nothing here claims "
    "deployment."
)

patch_record_history_docs.REVIEW_DATE = REVIEW_DATE


# ── M12 Staff Management ─────────────────────────────────────────────────────

M12_DIR = "12-staff-management"
M12_STEM = "XVS_M12_Staff_Management_Functional_Requirements_Document"
M12_SOURCE, M12_TARGET = "2.11", "2.12"

M12_FR024 = [
    ("Description",
     "Keep the school's own organogram and let every member of staff read it: org units tiered "
     "division, department and team; posts that report to other posts along a solid line; "
     "effective-dated appointments of staff to posts; and dotted lines between posts. The "
     "lines connect posts rather than people, so a post whose holder leaves stays on the "
     "chart, vacant, with its reports still under it, and whoever is appointed next inherits "
     "them."),
    ("Permission",
     "school.organogram.view reads the chart: units, posts, the post tree, current "
     "appointments and dotted lines. .create, .update and .delete write units, posts and "
     "dotted lines; .assign appoints somebody to a post and ends an appointment. The "
     "summary, vacancies and appointment history, with their dates, answer to "
     "school.teachers.update, because every teacher holds school.teachers.view and leave and "
     "suspension counts and the size of the establishment are an administrator's. "
     "/v1/i/me/staff/organogram/, closed before go-live."),
    ("Scoping",
     "The tenant first, so another school's unit, post, appointment or dotted line answers "
     "404 on read and write and an id in a body names nothing. Reads are not narrowed by "
     "branch: a Lekki teacher reads Ikeja's part of the chart, because knowing who one's "
     "manager's manager is does not depend on where one is based. Writes are: a unit is "
     "school-wide or one branch's, a post takes its unit's branch, and a branch-bound caller "
     "changes only its own branch's rows, 403 on a school-wide one, with every row carrying "
     "can_manage. Appointing also needs the caller to manage the person."),
    ("Business rules",
     "(1) A division is top level, a department sits under a division and a team under a "
     "department; a code is unique in the school and carries its tier's prefix. (2) A child "
     "unit is never wider than its parent: under a branch unit it takes that branch, under a "
     "school-wide one it may narrow to one. (3) A post reports to a school-wide post or one "
     "in its own branch, never another branch's, with no loops; a dotted line keeps the same "
     "rule. (4) A person may be appointed to a school-wide post, or to a branch post where "
     "they are posted or are themselves school-wide. (5) One current primary appointment per "
     "person: a new primary closes the old one on the day it starts. An appointment may be "
     "acting cover, and a secondary appointment sits beside the primary. (6) A holder is an "
     "open appointment whose person is not invited, resigned or terminated. Headcount is not "
     "enforced on appointment. (7) Resigning, termination and a revoked invitation end every "
     "open appointment on the exit date. (8) A unit or post in use is not deleted: 409 with "
     "the blocking counts. Moving a unit or post to another branch is refused while somebody "
     "appointed there is not posted to it. (9) Chart payloads carry a person's name, job "
     "title and photograph and no email, phone, pay or leave. The staff record carries its "
     "holder's post, unit, line manager and whether they are acting."),
    ("Acceptance",
     "(1) Another school's rows are 404 on read and write and never appear in a list or as a "
     "tree root. (2) A teacher reads the chart, including another branch's part, and is "
     "refused every write, the summary, the vacancies and the history. (3) A branch "
     "administrator at Ikeja cannot create, change or remove a Lekki or school-wide unit or "
     "post, cannot appoint a Lekki-only person, and draws Ikeja's part. (4) Resigning ends the "
     "appointment and the post keeps its reports. (5) The tree keeps a subtree whose manager "
     "post is inactive. (6) No chart payload carries an email. (7) The tree and the lists "
     "cost a bounded number of queries. (8) An empty chart answers empty lists "
     "(schools.vs_staff.tests.test_organogram, 38 tests)."),
]

M12_KEY_ROWS = [
    ["school.organogram.view", "New", "NORMAL", "school_admin, branch_admin, teacher",
     "Read the school's organogram: FR-024. Granted to teacher because every member of staff "
     "reads the chart."],
    ["school.organogram.create / .update / .delete / .assign", "New", "NORMAL",
     "school_admin, branch_admin",
     "Write units, posts and dotted lines, and appoint staff to posts: FR-024. A branch "
     "administrator's writes are narrowed to their own branch by the views, not by the key."],
]

M12_ROUTE_ROWS = [
    ["GET, POST /v1/i/me/staff/organogram/nodes/; GET, PATCH, DELETE .../nodes/<id>/",
     "school.organogram.view / .create / .update / .delete",
     "FR-024. Org units, filterable by kind, parent, branch and search. A delete with units or "
     "posts under it answers 409 with the counts."],
    ["GET, POST /v1/i/me/staff/organogram/positions/; GET, PATCH, DELETE .../positions/<id>/",
     "school.organogram.view / .create / .update / .delete",
     "FR-024. Posts with their holders, vacancy and open seats. A post with appointment "
     "history or posts reporting to it is not deleted."],
    ["GET /v1/i/me/staff/organogram/positions/tree/ and .../positions/vacancies/",
     "school.organogram.view; vacancies school.teachers.update",
     "FR-024. The post tree nested along the solid line, ?root= for a subtree; the posts with "
     "an open seat."],
    ["GET, POST /v1/i/me/staff/organogram/assignments/; GET .../current/ and .../mine/; POST "
     ".../assignments/<id>/close/",
     "school.teachers.update to read history; school.organogram.assign to write; "
     "current and mine school.organogram.view",
     "FR-024. Appointment history with dates; who holds which post now and whether acting, "
     "with no dates; the caller's own history; ending an appointment."],
    ["GET, POST /v1/i/me/staff/organogram/matrix-reports/; GET, DELETE .../matrix-reports/<id>/",
     "school.organogram.view / .create / .delete",
     "FR-024. Dotted lines, one per pair of posts."],
    ["GET /v1/i/me/staff/organogram/summary/", "school.teachers.update",
     "FR-024. Active staff, departments, posts, seats filled and vacant, acting, on leave and "
     "suspended."],
]

M12_REFUSAL_ROWS = [
    ["Another school's org unit, post, appointment or dotted line", "404", "NOT_FOUND"],
    ["A branch-bound caller writing a school-wide or another branch's unit, post or dotted line",
     "403", "Not a code. The sentence names the branch rule."],
    ["A unit or post deleted while units, posts or appointment history depend on it", "409",
     "Not a code. The blocking counts are in error.detail."],
    ["A post reporting to another branch's post, a reporting loop, or a tier out of order", "400",
     "Field error naming the rule."],
    ["Appointing somebody who is not posted to the post's branch", "400",
     "Field error naming the branch."],
]

M12_TRACE = (
    f"MRD v{MRD_TARGET} records Module 12 as Staff Management, Phase V1, Backend Partial, In "
    "use Partial, code schools.vs_staff mounted at /v1/i/me/staff/, with eleven capability "
    "entries. Each maps below to the requirements that deliver it and its state at the code "
    "baseline. Three requirements trace to no entry, bulk import (FR-016), bulk role grant "
    "(FR-019) and staff search (FR-021), because the tracker lists no search or bulk "
    "capability for this module."
)

M12_STATUS_NOTE = (
    "📌  Module 12 stays Backend Partial for two reasons the table shows: availability is known "
    "by the day and not by the hour, and no field on the staff record is restricted by grant. "
    "The third, that no reporting line existed for a school's staff, is closed by FR-024. In "
    "use is Partial rather than Not started because other modules read this one: Module 1's "
    "administrator provisioning writes the staff record, Module 13's class teacher points at "
    "it, and the workflow engine's Dynamic Roles read the job title and contract type it "
    "holds and its organogram stages climb the school's chart. Module 14 is the consumer "
    "still missing, because its timetable points at the account and picks teachers by role."
)

M12_SUMMARY = (
    "Minor revision. Adds FR-024: a school keeps its own organogram, org units tiered "
    "division, department and team, each school-wide or one branch's, posts that report to "
    "posts, effective-dated appointments and dotted lines, under /v1/i/me/staff/organogram/ "
    "behind five new keys, school.organogram.view reaching every role and the writes the two "
    "administrator roles. Every member of staff reads the whole chart; a branch "
    "administrator writes only their own branch's part; the summary and history need "
    "school.teachers.update, because every teacher holds the directory key. Resigning, "
    "termination and revocation end a person's appointments (FR-008, FR-020), and the record "
    "names its holder's post and line manager (FR-004). The traceability row Department, "
    "position, and manager assignment moves from not evidenced to implemented, and the "
    "module stays Partial for its two other reasons. A staff photograph and document are now "
    "links signed for the reader: they were the bare media path, which the media view "
    "refuses, so the section 7.4 rule and the section 12.1 criterion that required that path "
    "are rewritten (FR-007). Also corrected: the requirement count, the refusal row saying a "
    "person cannot be posted to two branches, which equal postings answer, and the "
    f"traceability paragraph now names MRD v{MRD_TARGET}, where it named v2.89. "
    + TEST_EVIDENCE
)


def patch_m12() -> None:
    require_newest(str(ROOT / "functional-requirements" / M12_DIR / f"{M12_STEM}_v*.docx"),
                   M12_SOURCE)
    doc = Document(str(frd_path(M12_DIR, M12_STEM, M12_SOURCE)))
    set_control(doc, "Version", M12_TARGET)
    set_control(doc, "Date", REVIEW_DATE)
    set_control(doc, "Supersedes", f"v{M12_SOURCE}")
    set_control(doc, "Source MRD", f"XVS Module Requirements Document v{MRD_TARGET}")
    set_control(doc, "Verified against", CODE_BASELINE)

    # Section 3.4: one refusal corrected, one added.
    refusals_34 = table_headed(doc, "What is asked for", "Why it cannot be done",
                               "What a school gets instead")
    remove_row(refusals_34, "A person posted to two branches.")
    insert_row_after(refusals_34.rows[-1], [
        "Routing an approval to a named post on the school's organogram.",
        "A specific-post stage and an approver group's position member point at the platform's "
        "posts, which a school's staff cannot hold, so for a school they reach nobody and the "
        "document parks. Module 7 records it.",
        "Route by direct manager, levels up or department head, which climb the school's own "
        "chart (FR-024), or by an approver group naming the people or roles.",
    ])

    # Section 7: the models, and how a document or photograph is handed out.
    edit_paragraph(doc, "Six models, all in schools.vs_staff",
                   "Six models, all in schools.vs_staff,",
                   "Ten models, all in schools.vs_staff: the six below hold the staff record "
                   "and the four organogram models are FR-024's. All")
    edit_paragraph(doc, "📌  A staff document is the most sensitive payload",
                   "the serializer emits the media path rather than a signed or guessable "
                   "direct link.",
                   "the serializer emits a link signed for the reader through core.media, "
                   "bound to them and expiring, never the storage's bare path, which the media "
                   "view refuses, and never a direct storage URL. The photograph is served the "
                   "same way.")

    # Section 8.1: the keys.
    keys = table_headed(doc, "Key", "State", "Sensitivity", "Default holders", "Use")
    anchor = keys.rows[-1]
    for values in M12_KEY_ROWS:
        anchor = insert_row_after(anchor, values)
    edit_paragraph(doc, "This module needs eight keys",
                   "This module needs eight keys and four of them already exist.",
                   "This module needs eight keys for the staff record, four of which already "
                   "existed, and five more for the organogram, a resource of its own because "
                   "every member of staff reads it.")

    # Section 9: the requirements.
    edit_paragraph(doc, "Twenty-one requirements.", "Twenty-one requirements.",
                   "Twenty-four requirements.")
    append_to(fr_table(doc, "FR-004"), "Response",
              " Plus organogram: the person's current primary post, its unit, the holder of "
              "the post above as line manager, and whether they are acting, or null when they "
              "hold no post (FR-024).")
    append_to(fr_table(doc, "FR-008"), "Postconditions",
              " A move to RESIGNED or TERMINATED ends every open organogram appointment on the "
              "exit date, so the post reads vacant and keeps its reports (FR-024).")
    append_to(fr_table(doc, "FR-020"), "Business rules",
              " Revoking also ends any organogram appointment the person was given while "
              "invited (FR-024).")
    append_to(fr_table(doc, "FR-007"), "Description",
              " A document's file_url, like the photograph's photo_url, is a link signed for "
              "the reader and absolute to the API.")
    add_fr(doc, "FR-024  The school's organogram",
           "FR-024  The School's Organogram", M12_FR024)

    # Section 10: routes and the ordering trap.
    routes = table_headed(doc, "Method and path", "Permission", "Requirement")
    anchor = routes.rows[-1]
    for values in M12_ROUTE_ROWS:
        anchor = insert_row_after(anchor, values)
    edit_paragraph(doc, "Ordering trap.",
                   "/v1/i/me/staff/qualifications/<id>/ must all",
                   "/v1/i/me/staff/qualifications/<id>/ and everything under "
                   "/v1/i/me/staff/organogram/ must all")

    # Section 11: refusals.
    refusals = table_headed(doc, "Condition", "Status", "Code")
    anchor = refusals.rows[-1]
    for values in M12_REFUSAL_ROWS:
        anchor = insert_row_after(anchor, values)

    # Section 12: acceptance.
    paragraph = paragraph_starting(doc, "A staff document is never reachable without "
                                        "authentication")
    mrd_tools.retitle(paragraph,
                      "A staff document or photograph is never reachable without "
                      "authentication, and the serialised payload carries a link signed for "
                      "the reader and absolute to the API, never the storage's bare path and "
                      "never a direct storage URL (MediaLinkTests).")
    insert_after(doc, "A crafted POST naming another tenant on the create route",
                 "The organogram answers another school's unit, post, appointment or dotted "
                 "line 404 on read and write; a teacher reads the whole chart and is refused "
                 "every write, the summary, the vacancies and the history; and a branch "
                 "administrator writes only their own branch's part (FR-024).")
    insert_after(doc, "A bulk posting change of five people writes five audit events.",
                 "Resigning ends a person's organogram appointment on the exit date, and the "
                 "post keeps the posts that report to it (FR-024).")
    insert_after(doc, "The coverage endpoint is paginated and capped",
                 "The organogram tree and its lists cost a bounded number of queries whatever "
                 "the size of the chart (QueryCostTests).")

    # Section 14: decision 2 narrows.
    decision = row_starting_cell(doc, "2. Is a job title free text or a catalogue?")
    edit_cell(decision.cells[1],
              "CodeX's own side already has the richer answer, an organogram of Positions, and "
              "it is platform-only. Decide whether a school gets a title list, and if so who "
              "maintains it.",
              "A school now has posts of its own on its organogram (FR-024), curated by "
              "whoever manages the chart, and the job title stays free text beside them. "
              "Decide whether the job title should follow the person's primary post, and "
              "whether a Dynamic Role should test the post rather than the typed title.")

    # Section 15: traceability.
    trace = table_headed(doc, "MRD capability", "Requirements", "State at c9031ad7")
    # Every other row still holds at the new baseline; the organogram row is new there.
    keep_format(trace.rows[0].cells[2], "State at 01badc15")
    target = row(trace, "Department, position, and manager assignment")
    keep_format(target.cells[1], "FR-024")
    keep_format(target.cells[2],
                "Implemented. A school's own organogram of units, posts, appointments and "
                "dotted lines, each unit school-wide or one branch's; every member of staff "
                "reads it and a branch administrator writes only their branch's part. The "
                "line manager is the holder of the post above, and the workflow engine climbs "
                "it for direct manager, levels up and department head.")
    mrd_tools.retitle(paragraph_starting(doc, "MRD v2.89 records Module 12"), M12_TRACE)
    mrd_tools.retitle(paragraph_starting(doc, "📌  Module 12 stays Backend Partial"),
                      M12_STATUS_NOTE)

    log_change(doc, M12_TARGET, M12_SUMMARY)
    assert_absent_outside_log(doc, "MRD v2.89", "Twenty-one requirements",
                              "no reporting line exists for a school",
                              "only the media path", "rather than a signed or guessable")
    repair_ooxml(doc)
    normalise_change_log(doc)
    finish(doc, frd_path(M12_DIR, M12_STEM, M12_TARGET),
           f"{M12_STEM.replace('_', ' ')} v{M12_TARGET}", M12_TARGET)


def row_starting_cell(doc, start: str):
    hits = [r for t in doc.tables for r in t.rows if r.cells[0].text.strip().startswith(start)]
    if len(hits) != 1:
        raise ValueError(f"{start!r} starts {len(hits)} rows")
    return hits[0]


# ── M07 Workflow & Approval Engine ───────────────────────────────────────────

M07_DIR = "07-workflow-and-approval-engine"
M07_STEM = "XVS_M07_Workflow_and_Approval_Engine_Functional_Requirements_Document"
M07_SOURCE, M07_TARGET = "1.18", "1.19"

M07_SUMMARY = (
    "Minor revision. An organogram stage now resolves for a school requester: DIRECT_MANAGER, "
    "N_LEVELS_UP and DEPARTMENT_HEAD climb the school's own chart, which Module 12 keeps and "
    "registers for SCHOOL tenants through register_tenant_organogram, so the engine imports "
    "no school app. The climb stays inside the instance's tenant and a platform requester "
    "still climbs the platform chart. SPECIFIC_POSITION still reaches nobody for a school, "
    "because the stage points at the platform's posts, and an approver group's position "
    "member is a platform post in the same way; both park rather than skip. FR-008's "
    "evidence, acceptance and limit, the organogram's owner, the tenant-local authority rule, "
    "the Module 12 dependency and the further gaps follow. "
    f"MRD v{MRD_TARGET}. " + TEST_EVIDENCE
)


def patch_m07() -> None:
    require_newest(str(ROOT / "functional-requirements" / M07_DIR / f"{M07_STEM}_v*.docx"),
                   M07_SOURCE)
    doc = Document(str(frd_path(M07_DIR, M07_STEM, M07_SOURCE)))
    set_cover_version(doc, M07_SOURCE, M07_TARGET)
    set_control(doc, "Version", M07_TARGET)
    set_control(doc, "Review date", REVIEW_DATE)
    set_control(doc, "Code baseline", CODE_BASELINE)
    set_control(doc, "Source MRD", f"XVS Module Requirements Document v{MRD_TARGET}")
    edit_cell(row_labelled(doc, "Supporting apps").cells[-1],
              "and the school staff app (leave)",
              "and the school staff app (leave, and the school organogram a school "
              "requester's organogram stage climbs)")

    keep_format(row_labelled(doc, "The organogram itself").cells[-1],
                "Module 3, Identity, Team & Organogram, for CodeX's own chart; Module 12, Staff "
                "Management, for a school's, which it registers with this engine for SCHOOL "
                "tenants. Positions and reporting lines are read, not maintained, here.")
    keep_format(row_labelled(doc, "Approval authority is tenant-local").cells[-1],
                "Every resolved approver is filtered to the tenant that raised the request, "
                "including organogram seats. A school requester climbs the school's own chart "
                "inside the instance's tenant, and a platform requester the platform's.")

    fr008 = fr_table(doc, "FR-008")
    keep_format(row(fr008, "Current evidence").cells[-1],
                "ApproverSource.ORGANOGRAM resolves DIRECT_MANAGER, N_LEVELS_UP, "
                "DEPARTMENT_HEAD or SPECIFIC_POSITION relative to the requester. A platform "
                "requester climbs OrganogramService. Any other requester climbs the chart "
                "registered for their tenant's kind through register_tenant_organogram, which "
                "Module 12 calls for SCHOOL tenants with StaffOrganogramService, inside the "
                "instance's tenant; the engine imports no school app.")
    append_to(fr008, "Acceptance",
              " A vacant manager post parks rather than reaching further up. A school climb "
              "never crosses into another school (WorkflowClimbTests), and a school requester "
              "never climbs the platform chart.")
    keep_format(row(fr008, "Current limit").cells[-1],
                "SPECIFIC_POSITION points at the platform's posts, so for a school requester it "
                "reaches nobody and the document parks; an approver group's POSITION member is "
                "a platform post in the same way. A tenant kind with no chart registered "
                "resolves to nobody, logs a warning and parks.")

    insert_row_after(row_labelled(doc, "Module 3, Identity & Organogram"), [
        "Module 12, Staff Management",
        "Supplies a school's own organogram: registers StaffOrganogramService for SCHOOL tenants "
        "through register_tenant_organogram from its app config, so an organogram stage climbs "
        "a school requester's posts without this engine importing the school app.",
    ])

    edit_box(table_headed_prefix(doc, "FURTHER GAPS").rows[0].cells[0], [
        ("append", "• A school cannot route an approval to one named post on its organogram. A "
         "specific-post stage and an approver group's position member point at the platform's "
         "posts, so for a school they reach nobody and the document parks; direct manager, "
         "levels up and department head climb the school's chart. Naming a school post needs "
         "the stage and the group to point at either chart, and the shared template builder "
         "to offer a school its own posts."),
    ])

    edit_paragraph(doc, "Module 7 carries 28 capability entries in MRD",
                   "in MRD v2.91.", f"in MRD v{MRD_TARGET}.")
    paragraph = paragraph_starting(doc, "Module 7 carries 28 capability entries in MRD")
    mrd_tools.retitle(paragraph, paragraph.text.rstrip() + (
        " A school requester climbing the school's own chart strengthens the organogram-based "
        "approver resolution entry without changing the count."))

    log_change(doc, M07_TARGET, M07_SUMMARY)
    assert_absent_outside_log(doc, "A tenant whose people are not placed in it resolves to "
                                   "nobody", "MRD v2.91")
    repair_ooxml(doc)
    normalise_change_log(doc)
    finish(doc, frd_path(M07_DIR, M07_STEM, M07_TARGET),
           f"{M07_STEM.replace('_', ' ')} v{M07_TARGET}", M07_TARGET)


def table_headed_prefix(doc, start: str):
    hits = [t for t in doc.tables if t.rows[0].cells[0].text.strip().startswith(start)]
    if len(hits) != 1:
        raise ValueError(f"{start!r} heads {len(hits)} tables")
    return hits[0]


# ── M03 Identity, Team & Organogram ──────────────────────────────────────────

M03_DIR = "03-identity-team-and-organogram"
M03_STEM = "XVS_M03_Identity_Team_and_Organogram_Functional_Requirements_Document"
M03_SOURCE, M03_TARGET = "1.18", "1.18.1"

M03_SUMMARY = (
    "Patch revision, wording only; this module's code and behaviour are unchanged. The owner "
    "of school staff records was recorded as a future staff domain with a school's own org "
    "chart not built here; Module 12 now keeps both, a school's organogram included, and "
    "this module's organogram stays CX-only by design. FR-020's limit called an empty "
    "approver list the engine's unsafe auto-skip default; Module 7 retired that default at "
    "its v1.5, and an empty list parks the document. The reconciliation box and source now "
    f"name MRD v{MRD_TARGET}, where they named v2.87."
)


def patch_m03() -> None:
    require_newest(str(ROOT / "functional-requirements" / M03_DIR / f"{M03_STEM}_v*.docx"),
                   M03_SOURCE)
    doc = Document(str(frd_path(M03_DIR, M03_STEM, M03_SOURCE)))
    set_cover_version(doc, M03_SOURCE, M03_TARGET)
    set_control(doc, "Version", M03_TARGET)
    set_control(doc, "Review date", REVIEW_DATE)
    set_control(doc, "Source MRD", f"XVS Module Requirements Document v{MRD_TARGET}")

    keep_format(row_labelled(doc, "School staff records").cells[-1],
                "Module 12, Staff Management (schools.vs_staff), which keeps the school staff "
                "record and a school's own organogram of units, posts and appointments. "
                "PlatformStaffProfile and this module's organogram stay CX-only by design and "
                "carry no school or branch field.")
    edit_cell(row(fr_table(doc, "FR-020"), "Current limit").cells[-1],
              "An empty list is exactly what the engine's auto-skip default acts on, which is "
              "the unsafe default the MRD carries as a P1 against Module 7.",
              "An empty list parks the document rather than skipping the stage, which has "
              "been the engine's default since M07 v1.5.")
    edit_box(table_headed_prefix(doc, "MRD RECONCILIATION").rows[0].cells[0], [
        ("sub", "• MRD v2.87 lists Module 3", "MRD v2.87", f"MRD v{MRD_TARGET}"),
    ])
    edit_paragraph(doc, "MRD v2.87 records Module 3", "MRD v2.87", f"MRD v{MRD_TARGET}")

    log_change(doc, M03_TARGET, M03_SUMMARY)
    assert_absent_outside_log(doc, "The future staff domain", "unsafe default the MRD carries",
                              "MRD v2.87")
    repair_ooxml(doc)
    normalise_change_log(doc)
    finish(doc, frd_path(M03_DIR, M03_STEM, M03_TARGET),
           f"{M03_STEM.replace('_', ' ')} v{M03_TARGET}", M03_TARGET)


# ── MRD ──────────────────────────────────────────────────────────────────────

MRD_CONTENTS_NOTE = "A school draws its own organogram"
MRD_INTRO = (
    "This revision records that a school keeps its own organogram, that the workflow engine "
    "climbs it for a school requester, and that a staff photograph or document is handed out "
    "as a link that opens."
)
MRD_CHANGE_SUMMARY = (
    "A school draws its own organogram. Module 12 keeps org units tiered division, "
    "department and team, each school-wide or one branch's, posts that report to posts, "
    "effective-dated appointments of staff to posts and dotted lines, behind five new "
    "school.organogram keys; every member of staff reads the whole chart and a branch "
    "administrator writes only their branch's part. Department, position and manager "
    "assignment moves from not evidenced to implemented; Module 12 stays Partial. An "
    "organogram approval stage climbs the school's chart for a school requester, where it "
    "had resolved to nobody; a specific-post stage still does, and Module 7 carries it. The "
    "Module 3 item saying positions cannot reach the staff domain is closed: its chart stays "
    "CX-only by design. A staff photograph and document are links signed for the reader; "
    "they had been bare paths the media view refuses. Capability entries unchanged at 510 "
    "across 31 modules; statuses do not move. M03 v1.18.1, M07 v1.19 and M12 v2.12. Backend "
    "evidence; the school app's screens are committed and pending release; no deployment "
    "claim."
)
MRD_DELTA_ROWS = [
    ["School organogram", "Built in Module 12",
     "Org units tiered division, department and team, each school-wide or one branch's; posts "
     "reporting to posts; effective-dated appointments; dotted lines. Five school.organogram "
     "keys; the chart is read by every role."],
    ["Department, position and manager assignment", "Not evidenced to implemented",
     "A person's line manager is the holder of the post above. Module 12 stays Partial for "
     "availability by the hour and grant-restricted fields."],
    ["Organogram approval stages", "Resolve for a school requester",
     "Direct manager, levels up and department head climb the school's chart inside the "
     "instance's tenant. A specific-post stage still reaches nobody for a school."],
    ["Platform organogram", "Stays CX-only",
     "Module 3's item that positions cannot reach the staff domain is closed by decision: the "
     "school's chart is its own, and the platform's carries no school field."],
    ["Staff photographs and documents", "Links that open",
     "Signed for the reader and absolute to the API, as student photographs already were; they "
     "had been bare paths the media view refuses."],
    ["Module FRDs", "Three revised", "M12 v2.12, M07 v1.19, M03 v1.18.1. M04 checked and left."],
]
MRD_M07_GAP = (
    "• A school cannot route an approval to one named post on its organogram. A specific-post "
    "stage and an approver group's position member point at the platform's posts, so for a "
    "school they reach nobody and the document parks. Direct manager, levels up and "
    "department head climb the school's own chart. M07 FRD v1.19 records it."
)
MRD_M12_ATTENTION = (
    "• One of the ten capabilities above is only partly evidenced, which with field-level "
    "access is why this module is Partial rather than Complete. Availability indicators are "
    "evidenced by the day and not by the hour: an approved leave request covering today makes "
    "the person read On Leave with the date they are back, computed on every read, and "
    "nothing states whether somebody is free at a given hour, because no contract records "
    "hours. Department, position and manager assignment is evidenced: a school keeps its own "
    "organogram of posts, and a person's line manager is the holder of the post above."
)


def drop_box_line(cell, start: str) -> None:
    """Remove the one line of a heading-and-bullets box that starts with ``start``."""
    lines = [line.strip() for p in cell.paragraphs for line in p.text.split("\n")
             if line.strip()]
    hits = [i for i, line in enumerate(lines) if line.startswith(start)]
    if len(hits) != 1:
        raise ValueError(f"{start!r} starts {len(hits)} box lines")
    del lines[hits[0]]
    if len(cell.paragraphs) == 1 and len(cell.paragraphs[0].runs) == 1:
        cell.paragraphs[0].runs[0].text = "\n".join(lines)
    else:
        mrd_tools.set_lines(cell, lines)


def patch_mrd() -> None:
    folder = ROOT / "module-requirements"
    require_newest(str(folder / "XVS_Module_Requirements_Document_v*.docx"), MRD_SOURCE)
    doc = Document(str(folder / f"XVS_Module_Requirements_Document_v{MRD_SOURCE}.docx"))
    tables = doc.tables
    cover, control, contents, index = tables[0], tables[1], tables[2], tables[5]
    delta = table_headed_prefix(doc, f"v{MRD_SOURCE} capability delta")
    log = next(t for t in doc.tables if t.rows[0].cells[0].text.strip() == "Version"
               and len(t.rows[0].cells) == 3)
    assert index.rows[0].cells[5].text.strip() == "Entries"
    total_before = sum(int(r.cells[5].text.strip()) for r in index.rows[1:])

    mrd_tools.replace_cover_version(cover, MRD_SOURCE, MRD_TARGET)
    for r in control.rows:
        label = r.cells[0].text.strip()
        if label == "Version":
            replace_cell(r.cells[1], MRD_TARGET, size=9)
        elif label == "Review date":
            replace_cell(r.cells[1], REVIEW_DATE, size=9)
        elif label == "Source scope":
            replace_cell(r.cells[1], CODE_BASELINE, size=9)
    for r in contents.rows:
        if r.cells[0].text.strip().startswith("5."):
            keep_format(r.cells[0], f"5. v{MRD_TARGET} Capability Delta")
            keep_format(r.cells[1], MRD_CONTENTS_NOTE)
    for paragraph in doc.paragraphs:
        text = paragraph.text.strip()
        if (text.startswith("5. ") and paragraph.style is not None
                and paragraph.style.name.startswith("Heading")):
            mrd_tools.retitle(paragraph, f"5. v{MRD_TARGET} Capability Delta")

    drop_box_line(box_of(doc, 3, "NEEDS ATTENTION"), "• Positions cannot be connected")

    edit_blurb(doc, 7, "Documented by M07 FRD v1.17.",
               "An organogram stage climbs a school requester's own chart, kept by Module 12, "
               "for direct manager, levels up and department head. Documented by M07 FRD "
               "v1.19.")
    edit_box(box_of(doc, 7, "NEEDS ATTENTION"), [("append", MRD_M07_GAP)])

    edit_blurb(doc, 12, "Documented by M12 FRD v2.11.",
               "A school keeps its own organogram of units, posts, appointments and dotted "
               "lines, each unit school-wide or one branch's: every member of staff reads the "
               "whole chart, a branch administrator draws only their branch's part, and "
               "leaving ends a person's appointments so the post keeps its reports. A staff "
               "photograph and document are links signed for the reader. Documented by M12 "
               "FRD v2.12.")
    edit_box(box_of(doc, 12, "NEEDS ATTENTION"), [
        ("replace", "• One of the ten capabilities above is not evidenced", MRD_M12_ATTENTION),
    ])

    total_after = sum(int(r.cells[5].text.strip()) for r in index.rows[1:])
    if total_after != total_before:
        raise ValueError(f"Capability total moved from {total_before} to {total_after}")

    mrd_tools.rebuild_table(delta, [f"v{MRD_TARGET} capability delta", "Decision", "Evidence"],
                            MRD_DELTA_ROWS, mrd_tools.DELTA_WIDTHS)
    mrd_tools.keep_rows_whole(delta)
    intro = [p for p in doc.paragraphs
             if p.text.strip().startswith("This revision") and len(p.text) > 80]
    if len(intro) != 1:
        raise ValueError(f"{len(intro)} delta introductions found")
    mrd_tools.retitle(intro[0], MRD_INTRO)

    # Copied from the latest version row, so the new row reads at the same size.
    log.rows[1]._tr.addprevious(copy.deepcopy(log.rows[1]._tr))
    for cell, text in zip(log.rows[1].cells, (MRD_TARGET, SHORT_DATE, MRD_CHANGE_SUMMARY)):
        keep_format(cell, text)
    assert_absent_outside_log(doc, "Positions cannot be connected", "Documented by M07 FRD v1.17",
                              "no reporting line exists; no second organization")
    repair_ooxml(doc)
    normalise_change_log(doc)
    finish(doc, folder / f"XVS_Module_Requirements_Document_v{MRD_TARGET}.docx",
           f"XVS Module Requirements Document v{MRD_TARGET}", MRD_TARGET)


def main() -> None:
    patch_m12()
    patch_m07()
    patch_m03()
    patch_mrd()


if __name__ == "__main__":
    main()

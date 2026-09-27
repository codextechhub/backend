#!/usr/bin/env python3
"""Cut M12 v2.13 and M07 v1.20: a suspended member of staff keeps their post.

What changed in the backend (ce538c98), and therefore in the documents:

* The organogram's holder rule required an active account, and suspension
  closes the account, so a suspended person's seat read vacant for as long as
  the suspension lasted. A holder is now anybody in an open appointment who
  still works here, suspended or not, so the chart, the vacancies, the summary
  and a person's line manager keep them in the seat; each holder carries
  is_suspended, true while their employment or their account is suspended, and
  the school app marks them.
* The workflow engine's climbs ask a narrower rule that also requires an active
  account, because a suspended person cannot sign in to decide anything: a
  direct-manager stage whose manager is suspended parks, and a department-head
  stage walks on to the next unit's head.

The MRD was checked and left at v2.93: no capability, status, ownership, gap or
priority moves, and it names neither rule.

    python tools/patch_suspended_holder_docs.py
"""
from __future__ import annotations

from docx import Document

import patch_mrd_v2_79_docs as mrd_tools
from patch_document_type_labels_docs import require_newest
from patch_record_history_docs import (
    ROOT,
    finish,
    frd_path,
    keep_format,
    log_change,
    set_control,
    set_cover_version,
)
from patch_restricted_grant_ladder_docs import append_to, assert_absent_outside_log, table_headed
from patch_staff_id_and_auth_events_docs import (
    edit_cell,
    fr_table,
    normalise_change_log,
    paragraph_starting,
    repair_ooxml,
    row,
)

import patch_record_history_docs

REVIEW_DATE = "27 September 2026"
CODE_BASELINE = (
    "Backend main at ce538c98, where a suspended member of staff keeps their organogram post, "
    "27 September 2026. The school app marks them (school-fe a735e37), pending release"
)
TEST_EVIDENCE = (
    "Verified by the schools.vs_staff suite (362 tests, three of them this change's) and "
    "vs_workflow (430), each run on its own and all passing, and by drawing a suspended "
    "teacher in their post on the running app. Nothing here claims deployment."
)

patch_record_history_docs.REVIEW_DATE = REVIEW_DATE


# ── M12 Staff Management ─────────────────────────────────────────────────────

M12_DIR = "12-staff-management"
M12_STEM = "XVS_M12_Staff_Management_Functional_Requirements_Document"
M12_SOURCE, M12_TARGET = "2.12", "2.13"

M12_SUMMARY = (
    "Minor revision. A suspended member of staff keeps their organogram post. The holder rule "
    "had required an active account, and suspension closes it, so a teacher suspended for a "
    "week showed their post as vacant, one to fill. A holder is now anybody in an open "
    "appointment who still works here; each carries is_suspended, true while their "
    "employment or their account is suspended, and the chart marks them. The seat counts as "
    "filled. The workflow engine passes a suspended holder over as an approver, since they "
    "cannot sign in (M07 v1.20). FR-024's rules and acceptance follow. " + TEST_EVIDENCE
)


def patch_m12() -> None:
    require_newest(str(ROOT / "functional-requirements" / M12_DIR / f"{M12_STEM}_v*.docx"),
                   M12_SOURCE)
    doc = Document(str(frd_path(M12_DIR, M12_STEM, M12_SOURCE)))
    set_control(doc, "Version", M12_TARGET)
    set_control(doc, "Date", REVIEW_DATE)
    set_control(doc, "Supersedes", f"v{M12_SOURCE}")
    set_control(doc, "Verified against", CODE_BASELINE)

    fr024 = fr_table(doc, "FR-024")
    edit_cell(row(fr024, "Business rules").cells[-1],
              "(6) A holder is an open appointment whose person is not invited, resigned or "
              "terminated.",
              "(6) A holder is an open appointment whose person is not invited, resigned or "
              "terminated. A suspended person still holds the post and is marked "
              "is_suspended, true while their employment or their account is suspended, and "
              "their seat counts as filled; they are passed over as an approver, since they "
              "cannot sign in (M07 FR-008).")
    edit_cell(row(fr024, "Business rules").cells[-1],
              "(9) Chart payloads carry a person's name, job title and photograph",
              "(9) Chart payloads carry a person's name, job title, photograph and whether "
              "they are suspended,")
    edit_cell(row(fr024, "Acceptance").cells[-1],
              "(8) An empty chart answers empty lists",
              "(8) A suspended person keeps the post, marked, and the post is not a vacancy "
              "(test_a_suspended_person_keeps_their_post_marked_suspended). (9) An empty "
              "chart answers empty lists")
    edit_cell(row(fr024, "Acceptance").cells[-1],
              "(schools.vs_staff.tests.test_organogram, 38 tests)",
              "(schools.vs_staff.tests.test_organogram, 41 tests)")

    trace = table_headed(doc, "MRD capability", "Requirements", "State at 01badc15")
    keep_format(trace.rows[0].cells[2], "State at ce538c98")

    log_change(doc, M12_TARGET, M12_SUMMARY)
    assert_absent_outside_log(doc, "State at 01badc15")
    repair_ooxml(doc)
    normalise_change_log(doc)
    finish(doc, frd_path(M12_DIR, M12_STEM, M12_TARGET),
           f"{M12_STEM.replace('_', ' ')} v{M12_TARGET}", M12_TARGET)


# ── M07 Workflow & Approval Engine ───────────────────────────────────────────

M07_DIR = "07-workflow-and-approval-engine"
M07_STEM = "XVS_M07_Workflow_and_Approval_Engine_Functional_Requirements_Document"
M07_SOURCE, M07_TARGET = "1.19", "1.20"

M07_SUMMARY = (
    "Minor revision. A suspended holder is passed over by an organogram stage for a school "
    "requester: Module 12 now keeps a suspended member of staff in their post on the chart, "
    "and the climb asks for holders whose account is active, since a suspended person cannot "
    "sign in to decide. A direct-manager stage whose manager is suspended parks; a "
    "department-head stage walks on to the next unit's head. FR-008's evidence and "
    "acceptance follow; the MRD was checked and needs no new version. " + TEST_EVIDENCE
)


def patch_m07() -> None:
    require_newest(str(ROOT / "functional-requirements" / M07_DIR / f"{M07_STEM}_v*.docx"),
                   M07_SOURCE)
    doc = Document(str(frd_path(M07_DIR, M07_STEM, M07_SOURCE)))
    set_cover_version(doc, M07_SOURCE, M07_TARGET)
    set_control(doc, "Version", M07_TARGET)
    set_control(doc, "Review date", REVIEW_DATE)
    set_control(doc, "Code baseline", CODE_BASELINE)

    fr008 = fr_table(doc, "FR-008")
    append_to(fr008, "Current evidence",
              " A school climb asks for holders whose account is active: Module 12 keeps a "
              "suspended member of staff in their post on the chart, and passes them over "
              "here, because they cannot sign in to decide.")
    append_to(fr008, "Acceptance",
              " A suspended manager is passed over: a direct-manager stage parks and a "
              "department-head stage walks on to the next unit's head "
              "(test_a_suspended_manager_is_passed_over_as_an_approver).")

    log_change(doc, M07_TARGET, M07_SUMMARY)
    repair_ooxml(doc)
    normalise_change_log(doc)
    finish(doc, frd_path(M07_DIR, M07_STEM, M07_TARGET),
           f"{M07_STEM.replace('_', ' ')} v{M07_TARGET}", M07_TARGET)


def main() -> None:
    patch_m12()
    patch_m07()


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""Cut M07 v1.21: a school routes an approval to a named post, and one switch
decides whether a tenant's approvals notify anybody.

What changed in the backend, and therefore in the document:

* A SPECIFIC_POSITION stage and an approver group's POSITION member name one
  post on the chart the owning tenant uses: a CX seat for a central template or
  the platform tenant, a post on the tenant's own chart otherwise, held by id
  because the engine imports no school app (ac0b4c3e). A code naming no post on
  that chart is refused 400 UNKNOWN_POSITION. Module 12 refuses to delete a post
  a live step or group names, and a CX seat a stage names is now protected
  (ef5a7558), where deleting it had silently emptied the stage.
* One tenant-wide switch, workflow.notifications.enabled, read and set at
  /v1/workflow/notification-settings/, stops all four wired events when off
  (78e0ceb7). Each event names its tenant, CodeX's own included (72a36086), and
  the document's branch.

The MRD was checked and left at v2.93: the named-post gap it tracks for Module 7
closes in its next version, which waits on the batch of FRDs in review.

    python tools/patch_named_post_and_notification_switch_docs.py
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
from patch_restricted_grant_ladder_docs import (
    append_to,
    assert_absent_outside_log,
    insert_row_after,
    row_labelled,
)
from patch_school_organogram_docs import drop_box_line, table_headed_prefix
from patch_staff_id_and_auth_events_docs import (
    edit_cell,
    fr_table,
    normalise_change_log,
    paragraph_starting,
    repair_ooxml,
    row,
)

import patch_record_history_docs

REVIEW_DATE = "28 September 2026"
CODE_BASELINE = (
    "Backend main at ef5a7558, where a stage or an approver group names a post on a school's "
    "own chart and a CX seat a stage names cannot be deleted, 28 September 2026. The school "
    "app's approver picker lists the school's posts (school-fe afb08c1), pending release"
)
TEST_EVIDENCE = (
    "Verified by the vs_workflow suite (442 tests), vs_workflow with vs_notifications (665) "
    "and with vs_user (866), and schools.vs_staff (422), each run on its own and all passing. "
    "Nothing here claims deployment."
)

patch_record_history_docs.REVIEW_DATE = REVIEW_DATE

M07_DIR = "07-workflow-and-approval-engine"
M07_STEM = "XVS_M07_Workflow_and_Approval_Engine_Functional_Requirements_Document"
M07_SOURCE, M07_TARGET = "1.20", "1.21"

M07_SUMMARY = (
    "Minor revision. A school can route an approval to one named post. A SPECIFIC_POSITION "
    "stage and an approver group's POSITION member name a post on the chart their tenant "
    "uses: a CX seat for a central template or the platform tenant, a post on the tenant's "
    "own chart otherwise, held by id because the engine imports no school app. A code that "
    "names no post there is refused 400 UNKNOWN_POSITION, and a CX seat is never a fallback "
    "for a school. Module 12 refuses to delete a post a live step or group names, and a CX "
    "seat a stage names is now protected, where deleting it had emptied the stage and parked "
    "every document reaching it. Separately, one tenant-wide switch, "
    "workflow.notifications.enabled at GET and PATCH /notification-settings/, read under "
    "template view and changed under template update, stops all four wired events when off. "
    "Each event names the tenant it is about, CodeX's own included, which it had not, and "
    "the document's branch. FR-006, FR-008, FR-022 and FR-024, the endpoint gates, routes, "
    "typed errors, deletion rules, the Module 8 and Module 12 dependencies, the further gaps "
    "and traceability follow. The MRD was checked and stays at v2.93 until its next "
    "version. " + TEST_EVIDENCE
)


def patch_m07() -> None:
    require_newest(str(ROOT / "functional-requirements" / M07_DIR / f"{M07_STEM}_v*.docx"),
                   M07_SOURCE)
    doc = Document(str(frd_path(M07_DIR, M07_STEM, M07_SOURCE)))
    set_cover_version(doc, M07_SOURCE, M07_TARGET)
    set_control(doc, "Version", M07_TARGET)
    set_control(doc, "Review date", REVIEW_DATE)
    set_control(doc, "Code baseline", CODE_BASELINE)
    edit_cell(row_labelled(doc, "Supporting apps").cells[-1],
              "the school organogram a school requester's organogram stage climbs",
              "the school organogram a school's organogram stages climb and name posts on")

    # 3. Endpoint gates
    insert_row_after(row_labelled(doc, "Dynamic Roles"), [
        "Notification switch",
        "workflow.template.view to read; workflow.template.update to change",
        "Request tenant",
    ])

    # 4. Requirements
    fr006 = fr_table(doc, "FR-006")
    keep_format(row(fr006, "Current evidence").cells[-1],
                "WorkflowApproverGroup holds member rows of kind USER, ROLE, or POSITION, "
                "enforced by a database check constraint. A POSITION member names exactly one "
                "post on the chart its group's tenant uses, as FR-008 describes for a stage: a "
                "CX seat for the platform tenant, a post on the tenant's own chart otherwise. "
                "Resolution unions the named people, every active holder of each member role, "
                "and the current holders of each member position who can act, then "
                "de-duplicates.")
    append_to(fr006, "Acceptance",
              " A school group naming a post resolves its holders able to act "
              "(test_a_group_naming_a_post_resolves_its_holders_able_to_act).")

    fr008 = fr_table(doc, "FR-008")
    append_to(fr008, "Current evidence",
              " A SPECIFIC_POSITION stage names one post on the chart its template's tenant "
              "uses: a CX seat for a central template or the platform tenant, and a post on "
              "the tenant's own chart otherwise, stored by id as organogram_tenant_position_id "
              "and looked up by the code the caller types. It resolves to that post's holders "
              "whose account is active, inside the instance's tenant.")
    append_to(fr008, "Acceptance",
              " A school stage naming a post reaches its active holder, and one naming no "
              "post reaches nobody (test_specific_position_reaches_the_named_posts_active_"
              "holder, test_specific_position_naming_no_post_reaches_nobody). A code naming "
              "no post on the row's chart is refused 400 UNKNOWN_POSITION, and a CX seat's "
              "code is never a fallback for a tenant row, nor the reverse (BindingTests).")
    keep_format(row(fr008, "Current limit").cells[-1],
                "A central template's SPECIFIC_POSITION stage names a CX seat, so for a school "
                "requester it reaches nobody and the document parks; a school names its own "
                "post by publishing its own version of the template, or by repointing the step "
                "to an approver group holding the post (FR-010). A tenant kind with no chart "
                "registered cannot name a post, resolves an organogram climb to nobody, logs a "
                "warning and parks.")

    fr022 = fr_table(doc, "FR-022")
    append_to(fr022, "Acceptance",
              " A position member binds a post on the group tenant's own chart and never "
              "another school's (test_a_group_member_binds_a_school_post_and_not_another_"
              "schools). A post a live step or group names cannot be deleted: Module 12 answers "
              "409 ORGANOGRAM_IN_USE for a school's post (test_a_post_a_step_or_a_group_names_"
              "is_not_deleted), and a CX seat a stage names is protected, answering 409 "
              "PROTECTED_REFERENCE (test_a_seat_a_stage_names_cannot_be_deleted).")

    fr024 = fr_table(doc, "FR-024")
    keep_format(row(fr024, "Current evidence").cells[-1],
                "Routing emits stage activated to the new approvers, and returned, rejected, "
                "and final approved to the requester. The tenant's own switch comes first: "
                "workflow.notifications.enabled, a school-scoped configuration value read and "
                "set at /notification-settings/, stops all four events for that tenant when "
                "off, and a tenant that has chosen nothing is notified. Behind it a template's "
                "notification_events dictionary can still suppress an event: an empty one "
                "notifies on every wired event, and once any key is set a missing key means "
                "off. Each event names the tenant it is about, a school or CodeX's own, and "
                "the document's branch, whose notification settings Module 8 applies. A "
                "successful instance detail read also acknowledges the caller's unread in-app "
                "notices for that instance, including both approval and submission event "
                "families.")
    append_to(fr024, "Acceptance",
              " A tenant that turns notifications off is told nothing, one that has chosen "
              "nothing is told, and switching back on notifies again "
              "(test_a_school_that_turns_notifications_off_is_told_nothing, "
              "test_a_school_that_has_chosen_nothing_is_still_told, "
              "test_switching_them_back_on_notifies_again). An activation notice names the "
              "tenant it is about (test_stage_activation_notifies_approvers).")

    # 6. Data model
    append_to_row = row_labelled(doc, "WorkflowStage").cells[-1]
    keep_format(append_to_row, append_to_row.text.rstrip() +
                " A CX seat it names is protected; a post on a tenant's own chart is held by "
                "id, and the app keeping that chart refuses to delete one a live step names.")
    edit_cell(row_labelled(doc, "WorkflowApproverGroupMember").cells[-1],
              "role and position targets are protected.",
              "role and CX seat targets are protected; a post on a tenant's own chart is held "
              "by id, and the app keeping that chart refuses to delete one a group names.")

    # 7. API and typed errors
    insert_row_after(row_labelled(doc, "GET, PATCH, DELETE /stage-approvers/{id}/"), [
        "GET, PATCH /notification-settings/",
        "Read or set whether the tenant's approvals notify anybody, as {\"enabled\": true or "
        "false}. Anything but a boolean is refused with 400.",
    ])
    anchor = insert_row_after(row_labelled(doc, "DYNAMIC_ROLE_IN_USE"), [
        "UNKNOWN_POSITION",
        "A position code names no post on the chart the stage's or group's tenant uses, or "
        "that tenant has no chart.",
        "400",
    ])
    insert_row_after(anchor, [
        "NOTIFICATION_SETTING_NOT_REGISTERED",
        "The notification switch is saved before its configuration definition is seeded.",
        "422",
    ])

    # 8. Dependencies
    keep_format(row_labelled(doc, "Module 12, Staff Management").cells[-1],
                "Supplies a school's own organogram: registers StaffOrganogramService for "
                "SCHOOL tenants through register_tenant_organogram from its app config, so an "
                "organogram stage climbs a school requester's posts, and a stage or an approver "
                "group names one of the school's posts, without this engine importing the "
                "school app. It refuses to delete a post a live step or group names, asking "
                "position_references.")
    append_to_row = row_labelled(doc, "Module 8, Notifications").cells[-1]
    keep_format(append_to_row, append_to_row.text.rstrip() +
                " Each event names the tenant it is about and the document's branch.")

    # 9. Needs Attention
    drop_box_line(table_headed_prefix(doc, "FURTHER GAPS").rows[0].cells[0],
                  "• A school cannot route an approval to one named post")

    # 10. Traceability
    paragraph = paragraph_starting(doc, "Module 7 carries 28 capability entries in MRD")
    mrd_tools.retitle(paragraph, paragraph.text.rstrip() + (
        " A school naming its own post strengthens the organogram-based resolution and named "
        "approver group entries, and the tenant's notification switch the workflow "
        "notifications entry, without changing the count."))

    log_change(doc, M07_TARGET, M07_SUMMARY)
    assert_absent_outside_log(doc, "SPECIFIC_POSITION points at the platform's posts",
                              "A school cannot route an approval to one named post")
    repair_ooxml(doc)
    normalise_change_log(doc)
    finish(doc, frd_path(M07_DIR, M07_STEM, M07_TARGET),
           f"{M07_STEM.replace('_', ' ')} v{M07_TARGET}", M07_TARGET)


def main() -> None:
    patch_m07()


if __name__ == "__main__":
    main()

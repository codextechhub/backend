#!/usr/bin/env python3
"""Cut M06 v1.5: keys_within_plan, the bulk form of the plan gate's refusal.

What changed in the backend, and therefore in the document:

* vs_rbac.plan_gate.keys_within_plan answers, of a set of permission keys,
  which ones the door would let a tenant use, giving each key the verdict
  plan_refusal would give it alone and reading this module's bulk evaluator
  through plan_reader (4af5d331). The finance and procurement dashboards build
  their reader from it, so a block whose key the school's plan does not reach
  is left out rather than refused. Module 25 carries the dashboard rule; this
  document records the evaluation it reads.

Needs Attention item 1, FR-022's limit, and the actor and dependency rows for
every other module said a school owns no setting. A school owns several (bf517f8b,
480a4c87, and the workflow notification switch): no config.* key is one it can
hold, and each of its settings reaches it through its own module's route and
keys, stored through this module's services. The three are reframed that way,
as MRD v2.94 words the gap. The config.* keys are counted at twenty-three, the
number ConfigPermissions.ALL and seed_config_permissions hold, where the
document said nineteen.

The MRD was checked and left at v2.93, whose Module 6 entries already cover
effective-capability evaluation; no capability is added.

    python tools/patch_plan_aware_dashboard_config_docs.py
"""
from __future__ import annotations

from docx import Document

import patch_mrd_v2_79_docs as mrd_tools
from patch_backend_owned_permission_registry_docs import keep_rows_whole
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
    row_labelled,
    table_headed,
)
from patch_staff_id_and_auth_events_docs import (
    edit_cell,
    edit_paragraph,
    edit_value,
    fr_table,
    normalise_change_log,
    paragraph_starting,
    repair_ooxml,
)

import patch_record_history_docs

REVIEW_DATE = "28 September 2026"
MRD_VERSION = "2.93"
CODE_BASELINE = (
    "Backend main at 27f1da0f, 28 September 2026, holding the finance pass merged at "
    "32194627, where a dashboard block needs the school's plan as well as the reader's key "
    "(4af5d331)"
)
TEST_EVIDENCE = (
    "Traced from the code at 27f1da0f; KeysWithinPlanTests in vs_rbac, and "
    "FinanceDashboardPlanTests and ProcurementDashboardPlanTests beside the dashboards, cover "
    "the bulk answer and were not re-run for this revision. Backend evidence only; nothing "
    "here is deployed."
)

patch_record_history_docs.REVIEW_DATE = REVIEW_DATE

M06_DIR = "06-configuration-and-capability"
M06_STEM = "XVS_M06_Configuration_and_Capability_Management_Functional_Requirements_Document"
M06_SOURCE, M06_TARGET = "1.4", "1.5"

M06_SUMMARY = (
    "Minor revision. Records keys_within_plan in vs_rbac's plan gate beside plan_refusal. "
    "The door asks plan_refusal of the keys one route needs; a screen that decides many "
    "blocks at once asks keys_within_plan, which returns the subset of a set of keys the "
    "door would let the tenant use, each key given the verdict plan_refusal would give it "
    "alone and read from this module's bulk evaluator through plan_reader, so the whole set "
    "costs a fixed handful of queries. Every key survives while enforcement is off or the "
    "school is unprovisioned. The finance and procurement dashboards build their reader "
    "from it, so a block whose key the school's plan does not reach is left out rather than "
    f"refused (Module 25). FR-014, FR-016, Section 5.3, the vs_rbac dependency and "
    f"traceability to MRD v{MRD_VERSION} at twenty-six capabilities follow. Needs Attention "
    "item 1, FR-022's limit and the actor and dependency rows for every other module no longer "
    "say a school owns no "
    "setting: no config.* key is one a school can hold, and the settings a school owns, its "
    "sign-in security, payroll scope and display time zone (Module 1) and its "
    "approval-notification switch (Module 7), reach it through its own module's routes and "
    "keys and are stored through this module's services (bf517f8b, 480a4c87). The config.* "
    "keys are counted at twenty-three, as ConfigPermissions and seed_config_permissions hold "
    "them, where this document said nineteen. " + TEST_EVIDENCE
)

#: Needs Attention item 1: no configuration key is a school's, and a school's own
#: settings arrive through the module each belongs to.
M06_GAP1 = (
    "No configuration key a school may hold",
    "Every one of the twenty-three configuration keys is PLATFORM-scoped, so no configuration "
    "route is a school's: all a school reaches here is the read in FR-015, which carries no "
    "key at all. A setting a school owns reaches it through a route of the module it belongs "
    "to, under that module's own keys, and is stored through this module's services with "
    "their validation and audit. A school sets its sign-in security and payroll scope that way "
    "(Module 1 FR-022, school.settings.view and school.settings.update), its display time "
    "zone, display.timezone, at /v1/i/me/settings/display/ under the same keys, and whether "
    "its approvals send emails at /v1/workflow/notification-settings/ under Module 7's "
    "template keys.",
    "Give each further setting a school owns the same shape: a definition that allows school "
    "scope here, and a route and keys in the module it belongs to. No config.* key becomes "
    "one a school can hold.",
)
M06_FR022_LIMIT_OLD = (
    "The reservation is total. A setting a school ought to own for itself has no route today "
    "and would need a tenant-scoped key introduced deliberately."
)
M06_FR022_LIMIT_NEW = (
    "The reservation covers every configuration route. A setting a school owns reaches it "
    "through a route of the module it belongs to, under that module's own keys, and is stored "
    "through this module's services, as Module 1's sign-in security, payroll scope and display "
    "time zone and Module 7's approval-notification switch are (Needs Attention, item 1)."
)
M06_ACTOR_OLD = "Read a resolved value or an effective capability. Never write either."
M06_ACTOR_NEW = (
    "Read a resolved value or an effective capability. A module that owns a school's setting "
    "also stores that value through this module's services, from a route and under keys of "
    "its own, as Module 1's school settings and Module 7's approval-notification switch do; "
    "none writes a capability."
)
M06_ACTOR_KEYS_OLD = "conf.get_config and the capability services."
M06_ACTOR_KEYS_NEW = (
    "conf.get_config and the capability services, and set_value, or save_security_settings "
    "for sign-in security, for a school's own setting."
)
M06_OTHERS_OLD = "None writes configuration."
M06_OTHERS_NEW = (
    "None writes through this module's routes; a module that owns a school's setting stores "
    "it through this module's services from a route of its own, as Module 1's school "
    "settings and Module 7's approval-notification switch do."
)


#: ConfigPermissions.ALL, which seed_config_permissions registers, holds 23 keys.
KEY_COUNT_EDITS = (
    ("The nineteen config.* keys, all PLATFORM-scoped.",
     "The twenty-three config.* keys, all PLATFORM-scoped."),
    ("All nineteen keys in ConfigPermissions are PLATFORM-scoped",
     "All twenty-three keys in ConfigPermissions are PLATFORM-scoped"),
    ("Enforces the nineteen PLATFORM-scoped keys",
     "Enforces the twenty-three PLATFORM-scoped keys"),
)


def fix_key_count(doc, actors) -> None:
    """Count the config.* keys as the code holds them: twenty-three, not nineteen."""
    platform = [r for r in actors.rows if r.cells[0].text.strip() == "CodeX platform staff"]
    if len(platform) != 1:
        raise ValueError(f"{len(platform)} actor rows for CodeX platform staff")
    edit_cell(platform[0].cells[2], *KEY_COUNT_EDITS[0])
    edit_value(fr_table(doc, "FR-022"), "Current evidence", *KEY_COUNT_EDITS[1])
    edit_cell(row_labelled(doc, "vs_rbac").cells[-1], *KEY_COUNT_EDITS[2])


def patch_m06() -> None:
    require_newest(str(ROOT / "functional-requirements" / M06_DIR / f"{M06_STEM}_v*.docx"),
                   M06_SOURCE)
    doc = Document(str(frd_path(M06_DIR, M06_STEM, M06_SOURCE)))
    set_cover_version(doc, M06_SOURCE, M06_TARGET)
    if f"Version: {M06_TARGET}" not in doc.tables[0].rows[0].cells[0].text:
        raise ValueError("The cover does not carry the new version")
    set_control(doc, "Version", M06_TARGET)
    set_control(doc, "Review date", REVIEW_DATE)
    set_control(doc, "Code baseline", CODE_BASELINE)
    set_control(doc, "Source MRD",
                f"XVS Module Requirements Document v{MRD_VERSION} | Module 6, twenty-six "
                "capability entries")

    # 4. Requirements
    fr014 = fr_table(doc, "FR-014")
    append_to(fr014, "Current evidence",
              " So does keys_within_plan, which the finance and procurement dashboards decide "
              "their blocks from (Section 5.3).")
    fr016 = fr_table(doc, "FR-016")
    append_to(fr016, "Acceptance",
              " A dashboard reads the same rule through keys_within_plan, so an unprovisioned "
              "school's reader keeps every block their role gives them.")

    # 5.3 From a plan to a refusal
    opener = paragraph_starting(doc, "The chain crosses three modules")
    mrd_tools.retitle(opener, opener.text.rstrip() + (
        " Module 4 answers in two shapes. plan_refusal is the door's: of the keys one route "
        "needs, it says why the plan refuses them all, or nothing when one is allowed. "
        "keys_within_plan, beside it in vs_rbac/plan_gate.py, is its bulk form for a screen "
        "that decides many blocks at once: it returns the subset of a set of keys the door "
        "would let the tenant use, each key given the verdict plan_refusal would give it "
        "alone, read from this module's bulk evaluator through plan_reader so the whole set "
        "costs a fixed handful of queries. Every key survives while enforcement is off or the "
        "school is unprovisioned, and so does a key with no active permission row or no "
        "capability behind it; a banded key survives only where the school's depth reaches "
        "its band. The finance and procurement dashboards build their reader from it, so a "
        "block whose key the plan does not reach is left out of the payload rather than "
        "refused, which is Module 25's rule."))

    # 3.1 Permission matrix: a module owning a school's setting stores it.
    actors = table_headed(doc, "Actor", "May do", "Governed by")
    other = [r for r in actors.rows if r.cells[0].text.strip() == "Every other module"]
    if len(other) != 1:
        raise ValueError(f"{len(other)} actor rows for every other module")
    edit_cell(other[0].cells[1], M06_ACTOR_OLD, M06_ACTOR_NEW)
    edit_cell(other[0].cells[2], M06_ACTOR_KEYS_OLD, M06_ACTOR_KEYS_NEW)

    # The config.* key count, as ConfigPermissions and seed_config_permissions hold it.
    fix_key_count(doc, actors)

    # 4. Requirements: FR-022's limit names where a school's own settings arrive.
    edit_value(fr_table(doc, "FR-022"), "Current limit", M06_FR022_LIMIT_OLD,
               M06_FR022_LIMIT_NEW)

    # 8. Dependencies
    rbac = row_labelled(doc, "vs_rbac").cells[-1]
    keep_format(rbac, rbac.text.rstrip() + (
        " Its plan gate reads this module's evaluation in two shapes, plan_refusal for the "
        "door and keys_within_plan for a screen deciding many keys at once, the dashboards "
        "among them."))
    others = [r for r in table_headed(doc, "Dependency", "Contract").rows
              if r.cells[0].text.strip() == "Every other module"]
    if len(others) != 1:
        raise ValueError(f"{len(others)} dependency rows for every other module")
    edit_cell(others[0].cells[-1], M06_OTHERS_OLD, M06_OTHERS_NEW)

    # 9. Needs Attention: item 1 is reframed rather than removed.
    gaps = table_headed(doc, "Pri.", "Current gap", "Detail", "Required completion")
    first = [r for r in gaps.rows if r.cells[0].text.strip() == "1"]
    if len(first) != 1 or first[0].cells[1].text.strip() != "No setting a school may own":
        raise ValueError("Needs Attention item 1 is not the school-setting gap")
    for cell, text in zip(first[0].cells[1:], M06_GAP1):
        keep_format(cell, text)

    # 10. Traceability
    edit_paragraph(doc, "Module 6 of XVS Module Requirements Document", "v2.79",
                   f"v{MRD_VERSION}")
    paragraph = paragraph_starting(doc, "Module 6 of XVS Module Requirements Document")
    mrd_tools.retitle(paragraph, paragraph.text.rstrip() + (
        " The dashboards deciding their blocks through keys_within_plan read effective-"
        "capability evaluation without changing the count."))

    log_change(doc, M06_TARGET, M06_SUMMARY)
    # A change-log row never breaks across a page.
    log = [t for t in doc.tables if t.rows[0].cells[0].text.strip() == "Version"
           and len(t.columns) == 3]
    if len(log) != 1:
        raise ValueError(f"{len(log)} change logs found")
    keep_rows_whole(log[0])
    retired_site_word = "cam" + "pus"
    assert_absent_outside_log(doc, retired_site_word, retired_site_word.capitalize(),
                              "MRD v2.79", "Document v2.79", "No setting a school may own",
                              "has no route today", "None writes configuration.",
                              "Never write either.", "nineteen")
    repair_ooxml(doc)
    normalise_change_log(doc)
    finish(doc, frd_path(M06_DIR, M06_STEM, M06_TARGET),
           f"{M06_STEM.replace('_', ' ')} v{M06_TARGET}", M06_TARGET)


def main() -> None:
    patch_m06()


if __name__ == "__main__":
    main()

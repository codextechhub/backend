#!/usr/bin/env python3
"""Version the MRD and the FRDs for the plan gate going live.

v2.66 recorded a gate that was built and switched off, reading a map nobody had
drawn. This records the rest: four keys split so a line could be sold at the
depth it was designed at, every key banded, the switch on, and each existing
school moved onto the depth its plan pays for.

    python tools/patch_gate_live_docs.py
"""

from __future__ import annotations

import argparse
import copy
from pathlib import Path

from docx import Document

from generate_requirements_documents import (
    BLACK,
    BLUE,
    assert_no_em_dash,
    rebuild_table,
    shrink_inherited_media,
    update_extended_title,
    write_cell,
)

REVIEW_DATE = "7 September 2026"
CHANGE_DATE = "7 Sep 2026"

MRD_SOURCE_VERSION = "2.66"
MRD_TARGET_VERSION = "2.67"

MRD_SOURCE_SCOPE = (
    "Backend change bringing the plan gate into service. Four permission keys "
    "that each gated two depths were split, every key was banded, the "
    "enforcement switch was turned on, and each existing school was moved onto "
    "the depth its plan pays for (7 September 2026)"
)

MRD_CHANGE_SUMMARY = (
    "Brought the plan gate into service. The previous revision built it and "
    "left it switched off, reading a permission-to-band map that did not yet "
    "exist; this draws the map, splits the keys that could not carry one, and "
    "turns the switch on. Four keys each gated two things the price list sells "
    "at different depths and so could be banded at neither: exams shared the "
    "class timetable's keys, staff qualifications shared the register's, "
    "promoting the whole roll shared the key that moves one child between "
    "branches, and spend analysis shared the key for a category insight. Each "
    "is now two keys, and a migration carried every existing grant onto the "
    "new one at role, group, prebuilt-template and personal-override level, "
    "denies included, so no holder lost anything in the split. All 410 keys "
    "then took a band, and the eight new ones were placed in permission "
    "bundles: without that a school administrator could not have granted them "
    "to anybody, and the keys would have existed unreachable. Vendors is "
    "folded into Procurement's own Core band and archived, because two "
    "capability rows at the same depth of the same module can never be told "
    "apart. Approval depth left the price list: how many rounds a ladder has "
    "describes a school's own shape and is workflow configuration no key could "
    "gate. Three defects closed on the way: the plans carried no depth, so "
    "every school created after the gate went live would have reached "
    "everything; the capability catalogue had no way to retire a row that "
    "stopped meaning anything; and the development seeder granted modules with "
    "no depth, leaving seeded schools behaving unlike any school the product "
    "makes. Verified by 495 RBAC, 506 procurement, 135 core and 94 "
    "configuration tests. Backend evidence only; nothing here is deployed."
)

M01_NEW_CAPABILITY = "▸  Plan re-applied to a school already trading"
M06_NEW_CAPABILITY = "▸  Capability retirement without deletion"

M01_ATTENTION = (
    "\n• Every school already trading has been moved onto the depth its plan "
    "pays for, through the same service school creation uses. The plans "
    "themselves carried no depth until now, which would have left every school "
    "created after the gate went live reaching everything: a chain of correct "
    "parts with one link missing, and indistinguishable from the gate working."
)

M04_DECISION = (
    "\n• The gate is in service. Every permission now names the band that "
    "governs it, the switch is on, and a school is refused a key its plan does "
    "not reach with a code of its own. Four keys that each gated two depths "
    "were split first, because a key covering one line sold at Plus and "
    "another sold at Advanced can be banded at neither; a migration carried "
    "every grant of the old key onto the new one, denies included, so the "
    "split took nothing away from anybody holding it."
    "\n• The eight new keys were placed in permission bundles at the same "
    "time. A key in no bundle is a key no school administrator can grant, so "
    "it would have existed and been unreachable. They are bundled by "
    "authority rather than by price: promoting the roll joins the acts that "
    "touch every child at once, and exams and staff certificates are each "
    "their own bundle because the person who builds a timetable or edits a "
    "staff list is not thereby given them."
)

M06_DECISION = (
    "\n• A capability can now be retired without being deleted. The catalogue "
    "could add and update but never archive, so a row that stopped describing "
    "anything sellable stayed active for ever. Vendors was the first: it was a "
    "module, then briefly a Core band of Procurement, and a second Core band "
    "beside Procurement's own was a distinction nothing could act on, because "
    "a school reaching one always reached the other. Its keys moved to "
    "Procurement's Core band and the row is archived, not removed, since "
    "entitlements, overrides and audit history point at it."
)

DELTA_ROWS = [
    ["Keys that gated two depths", "Split",
     "Exams left the class timetable's keys, staff qualifications left the "
     "register's, promotion left the key that transfers one child, and spend "
     "analysis left the key for a category insight."],
    ["Grants across a split", "Carried",
     "A migration copied every role grant, group grant, prebuilt default and "
     "personal override onto the new key, denies included, so nobody lost "
     "access to something they could do the day before."],
    ["The permission-to-band map", "Drawn and seeded",
     "All 410 keys answered: 125 Core, 144 Plus, 31 Advanced, 63 platform-only "
     "and 9 that must never be banded because they are role decisions."],
    ["The enforcement switch", "On",
     "Platform scope only, so a school cannot disable its own gate. A school "
     "with no package grants is unprovisioned rather than unentitled and is "
     "never refused."],
    ["Schools already trading", "Moved onto their plan",
     "Each existing school now holds grants carrying its plan's depth and its "
     "subscription expiry, so the wall is real rather than latent."],
    ["Vendors as a module", "Retired",
     "Folded into Procurement's Core band and archived. Two capability rows at "
     "the same depth of one module can never be told apart."],
    ["Approval depth", "Off the price list",
     "How many rounds a ladder has describes a school's own shape, and is "
     "workflow configuration rather than anything a permission key can gate."],
    ["What a school pays", "Still not built",
     "No rate, no floor, no billing unit and no term headcount. Depth decides "
     "what a school reaches; nothing yet decides its invoice."],
]

PRICING_GAP = (
    "Put a rate, a floor and a billing unit on the plan, snapshot each term's "
    "headcount from the student register, and invoice the school through the "
    "platform ledger entity. Depth now decides what a school reaches and is "
    "enforced; nothing decides what it pays. With the size ceilings removed "
    "the headcount is the only remaining way a larger school pays more. The "
    "Premium rate is provisional: its Advanced band is 31 keys, and most of "
    "what the design puts there sits in modules nobody has built."
)


def replace_cell(cell, text, **kwargs):
    while len(cell.paragraphs) > 1:
        paragraph = cell.paragraphs[-1]
        paragraph._p.getparent().remove(paragraph._p)
    write_cell(cell, text, **kwargs)


def retitle(paragraph, text):
    runs = paragraph.runs
    if not runs:
        paragraph.add_run(text)
        return
    runs[0].text = text
    for run in runs[1:]:
        run.text = ""


def replace_cover_version(table, source, target):
    for paragraph in table.rows[0].cells[0].paragraphs:
        for run in paragraph.runs:
            if source in run.text:
                run.text = run.text.replace(source, target)


def update_control_table(table, *, version, source_scope, entries=None,
                         mrd_baseline=None):
    for row in table.rows:
        label = row.cells[0].text.strip()
        if label == "Version":
            replace_cell(row.cells[1], version, size=9)
        elif label == "Review date":
            replace_cell(row.cells[1], REVIEW_DATE, size=9)
        elif label == "Source scope":
            replace_cell(row.cells[1], source_scope, size=9)
        elif label == "Capability entries" and entries:
            replace_cell(row.cells[1], entries, size=9)
        elif label == "MRD baseline" and mrd_baseline:
            replace_cell(row.cells[1], mrd_baseline, size=9)


def prepend_change_log(table, version, date, summary):
    template = table.rows[1]
    new_tr = copy.deepcopy(template._tr)
    template._tr.addprevious(new_tr)
    row = table.rows[1]
    replace_cell(row.cells[0], version, size=8)
    replace_cell(row.cells[1], date, size=8)
    replace_cell(row.cells[2], summary, size=8)


def append_to_cell(cell, addition, *, size):
    replace_cell(cell, cell.text.rstrip() + addition, size=size)


def append_capability(table, entry, *, size=8.5):
    """Add one entry to the last populated cell, not as a new half-empty row.

    These tables read as two columns of bullets. A new row carrying one entry
    leaves a blank half beside it, which reads as a missing line rather than a
    tidy list.
    """
    cell = table.rows[-2].cells[1]
    replace_cell(cell, cell.text.rstrip() + "\n" + entry, size=size)


def patch_mrd(source: Path, output: Path) -> None:
    doc = Document(str(source))
    doc.core_properties.title = (
        f"XVS Module Requirements Document v{MRD_TARGET_VERSION}"
    )
    doc.core_properties.version = MRD_TARGET_VERSION

    replace_cover_version(doc.tables[0], MRD_SOURCE_VERSION, MRD_TARGET_VERSION)
    update_control_table(
        doc.tables[1], version=MRD_TARGET_VERSION,
        source_scope=MRD_SOURCE_SCOPE, entries="488",
    )

    for row in doc.tables[2].rows:
        if row.cells[0].text.strip().startswith("5."):
            replace_cell(
                row.cells[0], f"5. v{MRD_TARGET_VERSION} Capability Delta",
                size=9, bold=True, color=BLUE,
            )
            replace_cell(
                row.cells[1],
                "The plan gate in service, and the keys split to let it read",
                size=9,
            )

    for row in doc.tables[5].rows:
        number = row.cells[0].text.strip()
        if number == "1":
            replace_cell(row.cells[5], "20", size=8.5)
        elif number == "6":
            replace_cell(row.cells[5], "25", size=8.5)

    for paragraph in doc.paragraphs:
        text = paragraph.text.strip()
        if text == f"5. v{MRD_SOURCE_VERSION} Capability Delta":
            retitle(paragraph, f"5. v{MRD_TARGET_VERSION} Capability Delta")
        elif text.startswith("This revision records the plan a school pays for"):
            retitle(
                paragraph,
                "This revision records the gate coming into service. The keys "
                "that could not carry a band were split, every key was banded, "
                "the switch went on, and each school already trading was moved "
                "onto the depth its plan pays for. What a school is charged is "
                "still not here, and is still a gap.",
            )

    append_capability(doc.tables[8], M01_NEW_CAPABILITY)
    append_to_cell(doc.tables[8].rows[-1].cells[0], M01_ATTENTION, size=8.2)

    append_to_cell(doc.tables[14].rows[-1].cells[0], M04_DECISION, size=8.2)

    append_capability(doc.tables[18], M06_NEW_CAPABILITY)
    append_to_cell(doc.tables[18].rows[-1].cells[0], M06_DECISION, size=8.2)

    for row in doc.tables[75].rows:
        if row.cells[1].text.strip().startswith("Subscription pricing"):
            replace_cell(row.cells[2], PRICING_GAP, size=8.2)

    rebuild_table(
        doc.tables[76],
        [f"v{MRD_TARGET_VERSION} capability delta", "Decision", "Evidence"],
        DELTA_ROWS, [1.75, 0.95, 4.57], font_size=8.2,
    )

    prepend_change_log(
        doc.tables[78], MRD_TARGET_VERSION, CHANGE_DATE, MRD_CHANGE_SUMMARY,
    )

    output.parent.mkdir(parents=True, exist_ok=True)
    doc.save(str(output))
    update_extended_title(
        output, f"XVS Module Requirements Document v{MRD_TARGET_VERSION}"
    )
    shrink_inherited_media(output)
    assert_no_em_dash(output)


M04_SOURCE_VERSION = "1.14"
M04_TARGET_VERSION = "1.15"

M04_SOURCE_SCOPE = (
    "Backend change splitting four permission keys that each gated two depths, "
    "banding every key, and bringing the plan gate into service "
    "(7 September 2026)"
)

M04_CHANGE_SUMMARY = (
    "Brings the plan gate into service and gives it something to read. Four "
    "keys each gated one line the price list sells at one depth and another it "
    "sells deeper, so neither could be banded: exams shared the class "
    "timetable's keys, staff qualifications shared the register's, promoting "
    "the whole roll shared the key that transfers one child, and spend "
    "analysis shared the key for a category insight. Each is now two keys, and "
    "migration 0016 carried every grant of the old key onto the new one at "
    "role, group, prebuilt-template and personal-override level, denies "
    "included, because a split silently removes access from everybody holding "
    "the key. The registry gained a promote action and four resources. All 410 "
    "keys then took a band, recorded on Permission.capability by a repeatable "
    "seeder that also clears a key withdrawn from the price list, and the "
    "enforcement switch was turned on. The eight new keys were placed in "
    "permission bundles in the same change: a key in no bundle cannot be "
    "granted by any school administrator, so it would have existed and been "
    "unreachable. FR-025 moves from disabled to in service. Verified by 495 "
    "RBAC tests, including a request-level check that a bursar whose role "
    "carries the key is still refused by her school's plan. Backend evidence "
    "only; nothing here is deployed."
)

M04_FR025_UPDATES = {
    "FR-025": "FR-025 | Implemented",
    "Current evidence":
        "Permission.capability records the capability governing each key; null "
        "means the key is core and every school holds it, and "
        "vs_rbac.capability_map remains the fallback for keys nobody has "
        "classified. vs_rbac.permission_bands holds the map itself and "
        "seed_permission_bands writes it, banding all 410 keys and clearing "
        "any key withdrawn from the price list on a re-run. "
        "plan_gate.plan_refusal runs inside HasRBACPermission after the role "
        "check has passed and never before it, evaluates the capability "
        "through vs_config's effective_capability, and raises "
        "PlanUpgradeRequired naming the module, the depth needed and the depth "
        "held. platform.entitlements.enforce is on, at platform scope only so "
        "a school holding config.value.update cannot switch off its own gate, "
        "and a tenant holding no PACKAGE entitlement is treated as "
        "unprovisioned rather than unentitled and is never refused.",
    "Limit":
        "Only rbac_permission is gated: a view gated solely by "
        "rbac_group_permission or by HasAnyModuleAccess passes untouched. "
        "Enforcement is one platform switch rather than one per school, so a "
        "staged rollout would need a platform-writable per-tenant value.",
}

M04_SPLIT_ROWS = [
    ["academics.exam.view / create / update / manage / publish",
     "Split from academics.timetable.*",
     "Exams are sold a depth below the weekly class timetable. While they "
     "shared keys, banding shallow gave exam scheduling away and banding deep "
     "took the class timetable with it."],
    ["school.staff_records.view / update",
     "Split from school.teachers.view / update",
     "Reading a staff directory and reading somebody's certificates are sold "
     "at different depths, and expose different things."],
    ["school.students.promote",
     "Split from school.students.manage",
     "Promoting the whole roll and moving one child between branches are not "
     "the same act and are not sold together."],
    ["procurement.analytics.view",
     "Split from procurement.report.view",
     "Spend analysis, AP and GRIR aging and vendor performance are the "
     "analytical tail; a category or catalogue insight is the shallow end."],
]

M04_RECONCILIATION = (
    "MRD RECONCILIATION\n"
    "• MRD v2.67 lists Module 4 as Roles & Permissions (RBAC), Phase V1, "
    "Backend Complete, In use Complete, code vs_rbac, with twenty capability "
    "entries. The module number, name, phase, states and ownership agree with "
    "this revision.\n"
    "• The count is unchanged at this revision. Splitting a key and banding "
    "the registry make the two entries added at v2.66 true rather than adding "
    "a product surface: the gate promised at v2.66 now reads a real map and "
    "answers, which is the same capability delivered rather than a new one."
)


def patch_m04(source: Path, output: Path) -> None:
    doc = Document(str(source))
    doc.core_properties.title = (
        "XVS M04 Roles and Permissions RBAC Functional Requirements "
        f"Document v{M04_TARGET_VERSION}"
    )
    doc.core_properties.version = M04_TARGET_VERSION

    models = doc.tables[37]
    refusals = doc.tables[41]
    trace = doc.tables[45]
    reconciliation = doc.tables[46]
    change_log = doc.tables[47]
    fr025 = doc.tables[33]

    replace_cover_version(doc.tables[0], M04_SOURCE_VERSION, M04_TARGET_VERSION)
    update_control_table(
        doc.tables[1], version=M04_TARGET_VERSION,
        source_scope=M04_SOURCE_SCOPE,
        mrd_baseline=(
            f"XVS Module Requirements Document v{MRD_TARGET_VERSION}, Module 4, "
            "twenty capability entries"
        ),
    )

    for row in fr025.rows:
        label = row.cells[0].text.strip().split("|")[0].strip()
        if label in M04_FR025_UPDATES:
            target = 0 if label == "FR-025" else 1
            replace_cell(
                row.cells[target], M04_FR025_UPDATES[label],
                size=8.5 if label == "FR-025" else 8.2,
                bold=label == "FR-025",
                color=BLUE if label == "FR-025" else BLACK,
            )

    append_to_cell(
        models.rows[2].cells[2],
        " The band it names is written by seed_permission_bands from "
        "vs_rbac.permission_bands, and cleared again if the key leaves the "
        "price list.",
        size=8.2,
    )

    # The four splits, recorded where a reader looks for permission contracts.
    insert_row_after(
        refusals, len(refusals.rows) - 1,
        ["A key held through a group grant or HasAnyModuleAccess alone",
         "Served. The plan gate reads rbac_permission only, so neither route "
         "is gated; both are rare and reach data that is meaningless without a "
         "module already reached through a gated route."],
        size=8.2,
    )

    anchor = len(trace.rows) - 1
    for values in [
        ["Permission mapped to the capability that governs it", "FR-025",
         "Implemented. All 410 keys carry a verdict: 125 Core, 144 Plus, 31 "
         "Advanced, 63 platform-only and 9 never banded."],
        ["Plan depth gate with its own refusal", "FR-025",
         "Implemented and in service. The switch is on and a school is refused "
         "under PLAN_UPGRADE_REQUIRED once its plan is applied."],
    ]:
        # Replace the two rows added at v2.66 rather than duplicating them.
        for row in trace.rows:
            if row.cells[0].text.strip() == values[0]:
                for idx, value in enumerate(values):
                    replace_cell(row.cells[idx], value, size=8.2)
                break

    replace_cell(reconciliation.rows[0].cells[0], M04_RECONCILIATION, size=8.2)
    prepend_change_log(
        change_log, M04_TARGET_VERSION, CHANGE_DATE, M04_CHANGE_SUMMARY,
    )

    output.parent.mkdir(parents=True, exist_ok=True)
    doc.save(str(output))
    update_extended_title(
        output,
        "XVS M04 Roles and Permissions RBAC Functional Requirements "
        f"Document v{M04_TARGET_VERSION}",
    )
    shrink_inherited_media(output)
    assert_no_em_dash(output)


def insert_row_after(table, index, values, *, size=8.2):
    template = table.rows[index]
    new_tr = copy.deepcopy(template._tr)
    template._tr.addnext(new_tr)
    row = table.rows[index + 1]
    for idx, value in enumerate(values):
        replace_cell(row.cells[idx], value, size=size)
    return row


M21_SOURCE_VERSION, M21_TARGET_VERSION = "1.2", "1.3"
M22_SOURCE_VERSION, M22_TARGET_VERSION = "1.6", "1.7"

M21_SOURCE_SCOPE = (
    "Backend change moving vendor analytics onto a key of its own and folding "
    "vendor work into Procurement's own depth bands (7 September 2026)"
)
M22_SOURCE_SCOPE = (
    "Backend change splitting procurement analytics from reporting, and "
    "withdrawing approval depth from the price list (7 September 2026)"
)

M21_CHANGE_SUMMARY = (
    "Records how vendor work is sold. Vendor insights, performance scoring and "
    "assessments moved from procurement.report.view to a new "
    "procurement.analytics.view, because one key covered both a category "
    "insight and a spend analysis and those are sold at different depths; the "
    "shallow insights keep the reporting key. Migration 0016 carried every "
    "grant of the reporting key onto the analytics key, denies included, so no "
    "holder lost a report. The vendors capability is retired and archived: "
    "vendor work is the shallow end of Procurement rather than a separate "
    "purchase, and a second Core band beside Procurement's own was a "
    "distinction nothing could act on, since a school reaching one always "
    "reached the other. The register, contacts and categories are Procurement "
    "Core; catalogue items and vendor bills and payments are Plus; assessments "
    "and performance are Advanced. No model, route, refusal code or audit "
    "action changed. Verified by 506 procurement tests. Backend evidence only; "
    "nothing here is deployed."
)

M22_CHANGE_SUMMARY = (
    "Records how procurement is sold, and removes a line that could not be. "
    "Spend analysis, AP and GRIR aging and cycle-time reporting moved from "
    "procurement.report.view to a new procurement.analytics.view, so the "
    "analytical tail and the shallow insights can be sold at different depths; "
    "migration 0016 carried every grant across, denies included. Approval "
    "depth left the price list altogether: the deck sold single-step approval "
    "at Core and multi-round at Plus, and no permission key can express that, "
    "because the number of rounds is workflow template configuration and "
    "procurement.approval.manage gates the queue whether the ladder has one "
    "step or four. Every school gets the approval engine at any ladder length, "
    "which is the honest reading: a chain's depth describes a school's own "
    "shape rather than a feature it buys. Requisitions, purchase orders and "
    "goods receipt are Core; RFQs, quotations, contracts, stock and vendor "
    "bills are Plus. No model, route, refusal code or audit action changed. "
    "Verified by 506 procurement tests. Backend evidence only; nothing here is "
    "deployed."
)

M21_ACTOR_ROW = [
    "Vendor analyst",
    "Read per-vendor spend, fulfilment and payment performance, and record "
    "assessments.",
    "procurement.analytics.view and procurement.vendor_assessment.create. Both "
    "sit at Procurement Advanced; the category and catalogue insights beside "
    "them stay on procurement.report.view at Plus.",
]

M22_ACTOR_ROW = [
    "Spend analyst",
    "Read spend analysis, AP and GRIR aging, and cycle-time reporting.",
    "procurement.analytics.view, at Procurement Advanced. Split from "
    "procurement.report.view so the analytical tail is not sold with a "
    "category insight.",
]

M21_DEPENDENCY_ROW = [
    "Module 6, Configuration & Capability Management",
    "Holds the depth this module is sold at. Vendor work has no capability of "
    "its own: the register, contacts and categories answer to Procurement's "
    "Core band, catalogue items and vendor bills to Plus, and assessments and "
    "performance to Advanced. The former vendors capability is archived rather "
    "than deleted, because entitlements, overrides and audit history point at "
    "it.",
]

M22_DEPENDENCY_ROW = [
    "Module 6, Configuration & Capability Management",
    "Holds the depth this module is sold at. Requisitions, approvals, purchase "
    "orders and goods receipt answer to Procurement's Core band whatever the "
    "approval ladder's length; sourcing, contracts, stock and vendor bills to "
    "Plus; spend analysis and vendor performance to Advanced.",
]


def patch_simple_frd(source, output, *, title, version, source_scope,
                     change_summary, actor_table, actor_row, dependency_table,
                     dependency_row, change_log_table, source_version):
    """Version one of the FRDs built on the M01/M04 page furniture."""
    doc = Document(str(source))
    doc.core_properties.title = title
    doc.core_properties.version = version

    actors = doc.tables[actor_table]
    dependencies = doc.tables[dependency_table]
    change_log = doc.tables[change_log_table]

    replace_cover_version(doc.tables[0], source_version, version)
    update_control_table(
        doc.tables[1], version=version, source_scope=source_scope,
        mrd_baseline=(
            f"XVS Module Requirements Document v{MRD_TARGET_VERSION}"
        ),
    )
    insert_row_after(actors, len(actors.rows) - 1, actor_row)
    insert_row_after(dependencies, len(dependencies.rows) - 1, dependency_row)
    prepend_change_log(change_log, version, CHANGE_DATE, change_summary)

    output.parent.mkdir(parents=True, exist_ok=True)
    doc.save(str(output))
    update_extended_title(output, title)
    shrink_inherited_media(output)
    assert_no_em_dash(output)


M11_SOURCE_VERSION, M11_TARGET_VERSION = "2.7", "2.8"
M12_SOURCE_VERSION, M12_TARGET_VERSION = "2.1", "2.2"
M14_SOURCE_VERSION, M14_TARGET_VERSION = "3.1", "3.2"

M11_SUMMARY = (
    "Promotion takes a key of its own. Running a promotion used to require "
    "school.students.manage, the same key that transfers one child between "
    "branches, so promoting the whole roll and moving one pupil could never be "
    "sold or granted apart. school.students.promote now gates the preview, the "
    "run and the batch read, alongside academics.classes.assign which the run "
    "still needs to write placements. Migration 0016 carried every grant of "
    "the manage key onto the new one, denies included, so nobody who could "
    "promote yesterday is refused today, and a promote action joins the "
    "canonical vocabulary. The key is placed in the Student Bulk Data bundle "
    "rather than Student Records, next to import and export: those are the "
    "three acts in this module that touch every child at once, and a registrar "
    "trusted to enrol one pupil at a time is not thereby trusted to move the "
    "whole roll up a year. It is sold at Plus. No model, route, refusal code "
    "or audit action changed. Verified by 274 student tests. Backend evidence "
    "only; nothing here is deployed."
)

M12_SUMMARY = (
    "Staff certificates and documents take keys of their own. Qualifications "
    "and uploaded documents were gated by school.teachers.view and .update, "
    "the same keys as the staff directory, so reading a colleague's "
    "certificates could not be separated from reading the staff list. "
    "school.staff_records.view and .update now gate them. Migration 0016 "
    "carried every grant of the register keys onto the new ones, denies "
    "included, so nobody lost a record they could read yesterday. They are "
    "placed in a Staff Qualifications & Documents bundle rather than added to "
    "Staff Records, because a certificate and a personal document expose more "
    "than a directory entry does and somebody who may edit the staff list is "
    "not thereby given them. They are sold at Plus, beside leave; the register "
    "itself stays Core. No model, route, refusal code or audit action changed. "
    "Verified by 180 staff tests. Backend evidence only; nothing here is "
    "deployed."
)

M14_SUMMARY = (
    "Exams take keys of their own. Exams, exam slots and exam publication were "
    "gated by academics.timetable.view, .create, .update, .manage and "
    ".publish, the same five keys as the weekly class timetable, so exam "
    "scheduling could not be sold or granted apart from it: banding the "
    "timetable key shallow gave exams away, and banding it deep took the class "
    "timetable with it. academics.exam.* now gates every exam route. Migration "
    "0016 carried every grant of the timetable keys onto the matching exam "
    "key, denies included. They are placed in an Exams bundle of their own "
    "rather than added to Timetables & Rooms, because a school that wants "
    "somebody building the weekly timetable does not thereby want them setting "
    "the exam schedule; that bundle's description no longer claims exams. "
    "Exams are sold at Advanced and the class timetable at Plus, which is what "
    "the design always said and what the shared keys made impossible. No "
    "model, route, refusal code or audit action changed. Verified by 225 "
    "calendar tests. Backend evidence only; nothing here is deployed."
)

M11_KEY_ROWS = {
    5: ["school.students.promote", "SENSITIVE", "school_admin",
        "Preview and run a promotion, and read a batch (FR-011). Split from "
        ".manage so promoting the roll is not the key that moves one child."],
    23: ["school.students.promote", "SENSITIVE", "school_admin",
         "Run the end-of-year promotion. In the Student Bulk Data bundle with "
         "import and export, and sold at Plus."],
}

M12_KEY_ROWS = {
    10: ["school.staff_records.view / .update",
         "school_admin, branch_admin",
         "Read and maintain qualifications, certificates and documents. Split "
         "from school.teachers.view / .update so a certificate is not read by "
         "everybody who may read the directory. Sold at Plus."],
    23: ["school.staff_records.view / .update", "New", "NORMAL / SENSITIVE",
         "school_admin, branch_admin",
         "Qualifications, certificates and documents, in their own permission "
         "bundle and sold at Plus."],
}

M14_KEY_ROWS = {
    7: ["academics.exam.view / .create / .update / .manage / .publish",
        "school_admin, branch_admin (manage: school_admin; view also teacher)",
        "Every exam route. Split from academics.timetable.* so exam scheduling "
        "is sold at Advanced while the class timetable stays at Plus."],
    28: ["academics.exam.view / .create / .update / .manage / .publish",
         "school_admin, branch_admin (manage: school_admin; view also teacher)",
         "Exams, exam slots and publication, in an Exams bundle of their own."],
}


def keep_rows_whole(table):
    """Stop a row splitting across a page break.

    A permission row that breaks leaves its first three cells blank at the top
    of the next page, which reads as a missing key rather than a continuation.
    The rows here are a line or two, so nothing is tall enough to need the
    break.
    """
    from docx.oxml.ns import qn
    from docx.oxml import OxmlElement

    for row in table.rows:
        properties = row._tr.get_or_add_trPr()
        if properties.find(qn("w:cantSplit")) is None:
            properties.append(OxmlElement("w:cantSplit"))


def append_change_log(table, version, date, summary):
    """Append, because this family lists its revisions oldest first."""
    template = table.rows[-1]
    new_tr = copy.deepcopy(template._tr)
    template._tr.addnext(new_tr)
    row = table.rows[-1]
    replace_cell(row.cells[0], version, size=8)
    replace_cell(row.cells[1], date, size=8)
    replace_cell(row.cells[2], summary, size=8)


def patch_keyed_frd(source, output, *, title, version, source_version,
                    key_rows, change_log_table, summary):
    """Version one of the FRDs built on the M11/M12/M14 page furniture."""
    doc = Document(str(source))
    doc.core_properties.title = title
    doc.core_properties.version = version

    tables = {index: doc.tables[index] for index in key_rows}
    change_log = doc.tables[change_log_table]

    for row in doc.tables[0].rows:
        label = row.cells[0].text.strip()
        if label == "Version":
            replace_cell(row.cells[1], version, size=9)
        elif label == "Date":
            replace_cell(row.cells[1], REVIEW_DATE, size=9)
        elif label == "Supersedes":
            replace_cell(row.cells[1], f"v{source_version}", size=9)

    for index, values in key_rows.items():
        table = tables[index]
        insert_row_after(table, len(table.rows) - 1, values)
        keep_rows_whole(table)

    append_change_log(change_log, version, CHANGE_DATE, summary)

    output.parent.mkdir(parents=True, exist_ok=True)
    doc.save(str(output))
    update_extended_title(output, title)
    shrink_inherited_media(output)
    assert_no_em_dash(output)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", default=Path(__file__).resolve().parents[1])
    args = parser.parse_args()
    root = Path(args.root)
    mrd = root / "module-requirements"
    patch_mrd(
        mrd / f"XVS_Module_Requirements_Document_v{MRD_SOURCE_VERSION}.docx",
        mrd / f"XVS_Module_Requirements_Document_v{MRD_TARGET_VERSION}.docx",
    )
    print(f"Wrote MRD v{MRD_TARGET_VERSION}")

    m04 = root / "functional-requirements" / "04-roles-and-permissions-rbac"
    patch_m04(
        m04 / ("XVS_M04_Roles_and_Permissions_RBAC_Functional_Requirements_"
               f"Document_v{M04_SOURCE_VERSION}.docx"),
        m04 / ("XVS_M04_Roles_and_Permissions_RBAC_Functional_Requirements_"
               f"Document_v{M04_TARGET_VERSION}.docx"),
    )
    print(f"Wrote M04 FRD v{M04_TARGET_VERSION}")

    m21 = root / "functional-requirements" / "21-vendor-management"
    patch_simple_frd(
        m21 / f"XVS_M21_Vendor_Management_Functional_Requirements_Document_v{M21_SOURCE_VERSION}.docx",
        m21 / f"XVS_M21_Vendor_Management_Functional_Requirements_Document_v{M21_TARGET_VERSION}.docx",
        title=f"XVS M21 Vendor Management Functional Requirements Document v{M21_TARGET_VERSION}",
        version=M21_TARGET_VERSION, source_scope=M21_SOURCE_SCOPE,
        change_summary=M21_CHANGE_SUMMARY, actor_table=6, actor_row=M21_ACTOR_ROW,
        dependency_table=24, dependency_row=M21_DEPENDENCY_ROW,
        change_log_table=28, source_version=M21_SOURCE_VERSION,
    )
    print(f"Wrote M21 FRD v{M21_TARGET_VERSION}")

    m22 = root / "functional-requirements" / "22-procurement-and-requisitions"
    patch_simple_frd(
        m22 / f"XVS_M22_Procurement_and_Requisitions_Functional_Requirements_Document_v{M22_SOURCE_VERSION}.docx",
        m22 / f"XVS_M22_Procurement_and_Requisitions_Functional_Requirements_Document_v{M22_TARGET_VERSION}.docx",
        title=f"XVS M22 Procurement and Requisitions Functional Requirements Document v{M22_TARGET_VERSION}",
        version=M22_TARGET_VERSION, source_scope=M22_SOURCE_SCOPE,
        change_summary=M22_CHANGE_SUMMARY, actor_table=6, actor_row=M22_ACTOR_ROW,
        dependency_table=30, dependency_row=M22_DEPENDENCY_ROW,
        change_log_table=34, source_version=M22_SOURCE_VERSION,
    )
    print(f"Wrote M22 FRD v{M22_TARGET_VERSION}")

    fr = root / "functional-requirements"
    for folder, stem, src, tgt, keys, log, summary in [
        ("11-student-management", "XVS_M11_Student_Management_Functional_Requirements_Document",
         M11_SOURCE_VERSION, M11_TARGET_VERSION, M11_KEY_ROWS, 54, M11_SUMMARY),
        ("12-staff-management", "XVS_M12_Staff_Management_Functional_Requirements_Document",
         M12_SOURCE_VERSION, M12_TARGET_VERSION, M12_KEY_ROWS, 51, M12_SUMMARY),
        ("14-timetable-and-calendar", "XVS_M14_Academic_Calendar_and_Timetables_Functional_Requirements_Document",
         M14_SOURCE_VERSION, M14_TARGET_VERSION, M14_KEY_ROWS, 52, M14_SUMMARY),
    ]:
        patch_keyed_frd(
            fr / folder / f"{stem}_v{src}.docx",
            fr / folder / f"{stem}_v{tgt}.docx",
            title=f"{stem.replace('_', ' ')} v{tgt}",
            version=tgt, source_version=src, key_rows=keys,
            change_log_table=log, summary=summary,
        )
        print(f"Wrote {stem.split('_')[1]} FRD v{tgt}")


if __name__ == "__main__":
    main()

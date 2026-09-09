#!/usr/bin/env python3
"""Version the MRD, M04, M09 and M10 for surfaces that follow the plan.

The gate refused what the screens had already offered. A role builder computed
availability from a module-level map and offered a Basic school 148 keys the
gate would refuse; the onboarding checklist offered an upload step to schools
whose plan cannot import; and a school had no way to read its own plan at all,
because every configuration key is platform-scoped. This records the four
changes that close those, and the refusal wording that stopped naming tiers to
the people who cannot act on them.

    python tools/patch_plan_scoped_surfaces_docs.py
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

REVIEW_DATE = "8 September 2026"
CHANGE_DATE = "8 Sep 2026"

MRD_SOURCE, MRD_TARGET = "2.68", "2.69"
M01_SOURCE, M01_TARGET = "1.20", "1.21"
M04_SOURCE, M04_TARGET = "1.16", "1.17"
M09_SOURCE, M09_TARGET = "2.8", "2.9"
M10_SOURCE, M10_TARGET = "1.1", "1.2"

MRD_SOURCE_SCOPE = (
    "Backend change making every school-facing surface read the plan the gate "
    "enforces, moving bulk import onto every plan, and removing the module "
    "selection from school creation (9 September 2026)"
)

MRD_CHANGE_SUMMARY = (
    "Closed the gap between what the product offers and what it allows, and "
    "corrected two things the gap had hidden. The gate went into service "
    "asking which band of a module a school reaches while the role builder "
    "went on asking whether the school had the module at all, which every "
    "school does: on a Basic school, 148 of 342 tenant keys were offered as "
    "available and refused the moment anybody used one. Both now resolve a "
    "permission's capability through one function, and each catalogue entry "
    "carries the band and the depth so a dimmed control can say what it is "
    "waiting on. A school could not read its own plan at all, because every "
    "configuration key is platform-scoped and the grant guard would refuse to "
    "give one to a school role; a self-scoped capability read now answers it "
    "without a key, for the caller's own tenant only. Refusals stopped naming "
    "tiers, because a bursar refused mid-task has no use for our pricing "
    "vocabulary; the depth still travels as structured data. "
    "Bulk import moves from Plus to Core. Every import key answers to that "
    "one band, so pricing it above the cheapest plan took the whole engine "
    "away from a Basic school rather than only the upload: its onboarding "
    "step rendered with an empty template table, because the template list "
    "itself was refused, and the school could see neither what to upload nor "
    "why it could not. Loading a roll from a spreadsheet is how a school "
    "arrives, not something it grows into. And school creation no longer asks "
    "which modules to enable: the plan grants every module and sets the depth, "
    "so the list narrowed nothing and only invited an operator to believe they "
    "had turned Procurement off. The field, its validation, the wizard step "
    "and the bulk-import column are all gone, and a payload still carrying one "
    "is ignored rather than refused. Two defects fell out of the work: the "
    "gate's fallback passed a surrogate row id where the map expects a "
    "resource slug, so every key not yet carrying a capability silently "
    "resolved to none, and a refusal read 'Finance: Plus is part of Plus depth "
    "in Finance'. Verified by 513 RBAC, 104 configuration, 178 onboarding, "
    "61 import, 152 core and 332 school tests, and by 710 school and 827 "
    "console frontend tests. Backend evidence only; nothing here is deployed."
)

MRD_INTRO = (
    "This revision records the product catching up with its own gate, and two "
    "corrections the catching-up exposed. What a school is offered, what its "
    "checklist asks of it and what it can read about its own plan now all "
    "answer the question the gate answers; loading data is on every plan "
    "rather than above the cheapest one; and school creation stopped asking a "
    "question whose answer changed nothing."
)

MRD_DELTA_ROWS = [
    ["The role builder's availability flag", "Repointed at the gate",
     "Picker and gate resolve a permission's capability through one function. "
     "Offered-but-refused went from 148 keys to none on a Basic school; of 342 "
     "tenant keys, 165 are offered and 177 refused, and the two now partition "
     "the set."],
    ["Why a control is dimmed", "Returned as data",
     "Each catalogue entry carries the band, its depth label and a sentence. "
     "A drawer can group by depth and say what a box is waiting on rather than "
     "greying it silently."],
    ["A school reading its own plan", "Opened, self-scoped",
     "GET /v1/config/my-capabilities/ answers for the caller's own tenant with "
     "no permission key. Every config.* key is platform-scoped, so no school "
     "role held one and none could be granted."],
    ["Tier names in a refusal", "Removed",
     "A refusal names the product and says to contact CodeX. Depth remains on "
     "the response as band and depth_label for the console and the builder."],
    ["Bulk import", "Moved to Core",
     "Every import key answers to this one band, so pricing it at Plus took "
     "the engine away from a Basic school entirely: the template list was "
     "refused and its onboarding step rendered empty. Loading a roll from a "
     "file is how a school arrives."],
    ["Module selection at school creation", "Removed",
     "The plan grants every module and sets the depth, so the list narrowed "
     "nothing and invited an operator to think they had turned Procurement "
     "off. Field, validation, wizard step and import column all gone; a "
     "payload still carrying one is ignored, not refused."],
    ["The onboarding upload step", "Conditional on the plan",
     "The catalog entry answers the plan through a seam built for it and never "
     "used. With bulk import at Core every school is given the card today; the "
     "seam is what keeps that true if it ever moves again."],
    ["A plan change mid-onboarding", "Tops up the checklist",
     "An upgrade adds the step it opens. Provisioning adds and never removes, "
     "so a downgrade cannot take away a step a school has already completed."],
    ["The frontend permission registry", "Brought level",
     "Thirteen keys the backend grants school roles had no name in the app, "
     "which hides a screen from a school entitled to it with nothing failing."],
    ["What a school pays", "Still not built",
     "No rate, no floor, no billing unit and no term. Unchanged by this "
     "revision and still the gap between a priced product and an invoiced one."],
]

M04_DECISION = (
    "\n• The picker and the gate answer through one function. They asked "
    "the availability question separately: the builder whether the school had "
    "Finance, which every school does, and the gate which band of it. A Basic "
    "school was offered 148 keys that refused the first person to use one, "
    "and the administrator who ticked them had no way to tell which of the two "
    "screens was wrong. Anything the catalogue marks available, the gate "
    "allows; the reverse is deliberately not promised, because a permission "
    "may be withheld for reasons that have nothing to do with a plan."
    "\n• A refusal names the product, never the tier. Core, Plus and "
    "Advanced are how CodeX prices the product, and a bursar refused mid-task "
    "learns a word from our price list rather than what to do next. The depth "
    "still travels, as band and depth_label on the response, for the console "
    "and the role builder that group by it."
)

M06_DECISION = (
    "\n• A school can read its own plan. Every config.* key is "
    "platform-scoped, so no school role held one and the grant guard would "
    "refuse to give one, which left every school screen asking a role "
    "question instead: always yes for an administrator, and silent about what "
    "the school bought. A self-scoped read now answers it with no permission "
    "key, for the caller's own tenant only and reporting states rather than "
    "the entitlement rows behind them. It is open before go-live, because a "
    "school's navigation is first drawn while it is still pending."
    "\n• Bulk import is Core rather than Plus. It is one band for the whole "
    "platform, so its depth is not a fact about one screen: every import key "
    "answers to it, and pricing it above the cheapest plan closed the template "
    "list, the batch history and the upload together. A school arriving with "
    "four hundred children in a spreadsheet had no way in, on the plan most "
    "schools start on, at the one moment the product asks them to load data. "
    "Data export stays at Plus, which is the reverse case: a school that has "
    "already loaded its records is asking to take them out in bulk."
)

M09_DECISION = (
    "\n• The checklist offers only steps the plan can perform. Uploading "
    "initial datasets is the entry that uses the conditional seam, which had "
    "existed since the catalog was written and had never been used. Three "
    "states would each read as 'cannot import' if asked carelessly, and only "
    "one should cost a school a card: an unseeded catalogue and a school whose "
    "plan was never applied both keep it. Required steps are untouched, so no "
    "plan can leave a school unable to reach go-live. Bulk import now sits at "
    "Core, so every school is given the card today; the seam is what keeps the "
    "checklist honest if that ever moves again."
)

M10_DECISION = (
    "\n• Reaching this engine is a plan question as well as a role one, "
    "and the plan now answers yes for everybody. An administrator holds the "
    "import keys whether or not the school bought bulk import, so the role "
    "question alone opened the door, ran the wizard and refused the upload; "
    "the surfaces that lead here read the capability too. Priced at Plus that "
    "closed the whole engine to a Basic school, template list included, which "
    "is why the band moved to Core."
)

M01_DECISION_MODULES = (
    "\n• School creation stopped asking which modules to enable. The plan "
    "grants every module and sets how deep the school reaches into each, so "
    "the list narrowed nothing: the same school came out of the wizard "
    "whichever boxes were ticked. The question was worse than useless, because "
    "an operator who left Procurement unticked had every reason to believe "
    "they had turned it off. The serializer field, its validation, the wizard "
    "step and the bulk-import template column are all gone. A payload still "
    "carrying a module list is ignored rather than refused, so a frontend that "
    "has not caught up keeps working."
)

# ── M04 ──────────────────────────────────────────────────────────────────────

M04_SOURCE_SCOPE = (
    "Backend change pointing the tenant permission catalogue at the same "
    "capability resolution the plan gate enforces, returning the band behind "
    "each entry, and removing tier names from a refusal (8 September 2026)"
)

M04_CHANGE_SUMMARY = (
    "Corrects FR-025 where it was enforced but not reflected. The tenant "
    "permission catalogue computed availability from a module-level map that "
    "asks whether a school has Finance, which every school does, while the "
    "gate asks which band of Finance: on a Basic school 148 of 342 tenant keys "
    "were offered as available and refused on use, and a role saved cleanly "
    "with every one of them ticked. Both now read plan_gate.plan_reader, which "
    "resolves a permission's capability once and evaluates it through the bulk "
    "evaluator the navigation already uses, so a picker cannot drift from the "
    "gate again and the catalogue costs 9 queries rather than 137. Each entry "
    "carries capability, band, depth_label and a sentence, so a dimmed control "
    "can say what it is waiting on. The refusal itself no longer names a tier: "
    "it names the product and says to contact CodeX, because the person "
    "refused mid-task cannot act on our pricing vocabulary. Two defects closed "
    "with it: the capability fallback passed the resource's surrogate id where "
    "the map expects its slug, so every key not yet carrying a capability "
    "resolved to none, and the refusal read 'Finance: Plus is part of Plus "
    "depth in Finance'. The failing check was written first and watched to "
    "fail on all 148. Verified by 511 RBAC tests, including 9 that assert "
    "nothing the catalogue offers is refused. Backend evidence only; nothing "
    "here is deployed."
)

M04_FR025_UPDATES = {
    "Acceptance":
        "A caller whose role carries the key and whose school reaches the "
        "depth is served. A caller whose role carries the key and whose school "
        "does not reach the depth receives 403 PLAN_UPGRADE_REQUIRED naming "
        "the product and no tier. A caller whose role does not carry the key "
        "receives the ordinary role refusal under its own code, so the two are "
        "distinguishable by a client and by the person reading the screen. "
        "Nothing the tenant permission catalogue marks available is refused by "
        "the gate, for any plan.",
    "Limit":
        "Only rbac_permission is gated: a view gated solely by "
        "rbac_group_permission or by HasAnyModuleAccess passes untouched. "
        "Enforcement is one platform switch rather than one per school, so a "
        "staged rollout would need a platform-writable per-tenant value. The "
        "catalogue deliberately does not read that switch: it reports what the "
        "school bought whether or not refusals are being issued, so it is the "
        "stricter of the two while the switch is off and never the looser.",
}

M04_API_ROW = [
    "GET /rbac/tenants/{slug}/permission-catalogue/",
    "The keys this tenant may grant, grouped by module, each with the "
    "capability and band that govern it, the depth label, whether the plan "
    "reaches it and a sentence when it does not. Role view keys.",
]

M04_CONDITION_ROWS = [
    ["A catalogue entry the school's plan does not reach",
     "Returned with available false, the band that governs it, its depth "
     "label and a sentence naming the product. The drawer disables it rather "
     "than hiding it, so an administrator can see what the school is not on."],
    ["A catalogue read by a school with no package grants at all",
     "Everything offered. No grants is 'not provisioned', not 'bought "
     "nothing', and a school created before grants were written reliably has "
     "none."],
]

M04_TRACE_ROW = [
    "Role builder offers only what the gate allows",
    "FR-025",
    "Implemented. Picker and gate resolve a permission's capability through "
    "one function; offered-but-refused is zero on Basic, Standard and Premium.",
]

# ── M09 ──────────────────────────────────────────────────────────────────────

M09_SOURCE_SCOPE = (
    "Backend change scoping the onboarding checklist to the steps a school's "
    "plan can perform, and topping it up when a plan changes mid-onboarding "
    "(8 September 2026)"
)

M09_CHANGE_SUMMARY = (
    "Corrects FR-001 for the one entry that does not apply to every school. "
    "Uploading initial datasets is sold by depth, and a school whose plan did "
    "not reach it was still given the card: the administrator pressed it, "
    "chose a student roll and met the refusal there, having been told nothing "
    "on the way in. CatalogEntry.applies_to has existed for this since the "
    "catalog was written and had never been used; the entry now answers the "
    "plan through it. The safe direction is pinned by test: an unseeded "
    "capability catalogue and a school whose plan was never applied both keep "
    "the card, because only a school that genuinely cannot perform the step "
    "should lose it. Bulk import has since moved to Core, so every school is "
    "given the card today and the seam is what keeps the checklist honest if "
    "that ever changes. A plan change mid-onboarding re-runs provisioning, so "
    "an upgrade adds the step it opens; provisioning adds and never removes, "
    "so a downgrade cannot take away a step already completed, and the top-up "
    "is skipped once a school is live. Required steps are untouched and "
    "readiness counts only those, so no plan can leave a school unable to "
    "reach go-live. The checklist table is also corrected: it still listed "
    "FIRST_ADMIN, ROLE_BASELINE and SET_OF_BOOKS, which the catalog has not "
    "carried since the design collapsed the first two into one card and the "
    "third was removed by decision. Verified by 178 onboarding tests, 7 of "
    "them written for this. Backend evidence only; nothing here is deployed."
)

M09_CHECKLIST_ROWS = [
    ["DEFAULT_ROLES", "Yes",
     "An active user of the tenant holds an active whole-tenant school_admin "
     "role, and that role carries permissions. Both facts are checked; the "
     "step is refused unless both hold."],
    ["SCHOOL_METADATA", "Yes",
     "Name, slug, code, ownership type, term structure and currency are set on "
     "the school's own record."],
    ["ACADEMIC_STRUCTURE", "Yes",
     "The mounted Module 13 records satisfy the required academic shape for "
     "the tenant."],
    ["INITIAL_DATA", "No",
     "An import batch for the tenant succeeded in full. Present only for a "
     "school whose plan reaches bulk import: a school that cannot perform the "
     "step is not given an optional one, it is given no such step."],
    ["STAFF_INVITATIONS", "No",
     "Somebody beyond the first administrator has an account or an open "
     "invitation in the tenant."],
]

M09_FR001_UPDATES = {
    "Current evidence":
        "provision_onboarding reads TASK_CATALOG in code, creates what is "
        "missing and touches nothing that exists, and writes one "
        "ONBOARDING_PROVISIONED audit event per call. One entry is "
        "conditional: uploading initial datasets is present only for a school "
        "whose plan reaches bulk import, answered through "
        "CatalogEntry.applies_to. The predicate answers yes for anything it "
        "cannot be sure of, so an unseeded capability catalogue and a school "
        "whose plan was never applied both keep the card. Changing a school's "
        "plan re-runs provisioning unless the school is already live, so an "
        "upgrade adds the step it opens.",
    "Acceptance":
        "Provisioning twice produces one progress row and one row per "
        "applicable key and resets no status. Adding a catalog entry and "
        "re-provisioning creates only the new task. A school on a plan without "
        "bulk import has no INITIAL_DATA row and every required row. A school "
        "created through the API or the importer answers GET "
        "/v1/onboarding/state/ without a further call.",
}

# ── M10 ──────────────────────────────────────────────────────────────────────

M10_SOURCE_SCOPE = (
    "Backend change making bulk import a product a school is sold rather than "
    "a permission an administrator holds, so a school without it is never "
    "offered the door, the checklist card or the upload (8 September 2026)"
)

M10_CHANGE_SUMMARY = (
    "Records that reaching this engine is a plan question as well as a role "
    "one, and that the plan had been answering no for most schools. An "
    "administrator holds import.batches.* whether or not their school bought "
    "bulk import, because holding a key is a role question, so the navigation "
    "opened the door and the onboarding checklist offered the card; three "
    "surfaces now ask the plan as well, and the refusal no longer names a "
    "depth the reader cannot act on. Priced at Plus, that closed far more than "
    "the upload: every import key answers to the one bulk_import band, so a "
    "Basic school was refused the template list itself and its onboarding step "
    "rendered with an empty table, showing neither what to upload nor why. The "
    "band is now Core, and loading a roll from a spreadsheet is on every plan. "
    "The schools template also loses its Enabled Modules column, which "
    "validated a value that decided nothing and could refuse a whole school "
    "row over a module key; the seeder retires it, along with three capacity "
    "columns left behind when plan ceilings were removed. Nothing changed in "
    "what an import does or who may run one: FR-001 through FR-017 are "
    "unaltered. Verified by 61 import, 178 onboarding and 152 core tests, and "
    "by 710 frontend tests. Backend evidence only; nothing here is deployed."
)

M10_FR001_UPDATES = {
    "Current evidence": None,  # filled from the document, appended to
}



STALE_DATASET_BULLET_MRD = (
    "No dataset a school may import for itself exists yet. Students, staff "
    "and parents have no template and no model, so the school-facing import "
    "screen shows an empty table. The onboarding checklist step it serves is "
    "optional, so readiness and go-live are not blocked."
)

FRESH_DATASET_BULLET_MRD = (
    "Six datasets a school may import for itself exist and are seeded: "
    "students, staff, guardians, academic structure, subjects and calendar "
    "events. What has none is historical records, which the onboarding screen "
    "shows greyed, and fees and timetables, which have no dataset type and no "
    "handler. A school on any plan reaches the six, since bulk import sits at "
    "Core."
)

STALE_DATASET_BULLET_M10 = (
    "No dataset a school may import exists. TENANT_DATASETS is empty because "
    "students, staff and parents have no template and no model, so the "
    "school-facing import step shows an empty table."
)

FRESH_DATASET_BULLET_M10 = (
    "Three things a school might reasonably bring have no dataset: historical "
    "records, fee structures and timetables. None has a dataset type or a "
    "handler, so the onboarding screen shows historical records greyed and "
    "does not mention the other two. The six that do exist - students, staff, "
    "guardians, academic structure, subjects and calendar events - are seeded "
    "and reachable on every plan."
)

def replace_in_cell(cell, old, new):
    """Swap a phrase inside a cell without rewriting the cell.

    Rewriting a cell writes one plainly formatted run, which is fine for a
    data cell and destroys a styled one: the Needs Attention boxes open with a
    coloured, bold banner run, and rebuilding the cell turns that banner into
    body text. This edits paragraph by paragraph and keeps each paragraph's
    first run, so the banner survives its own paragraph being rewritten.
    """
    for paragraph in cell.paragraphs:
        if old not in paragraph.text:
            continue
        runs = paragraph.runs
        if not runs:
            continue
        runs[0].text = paragraph.text.replace(old, new)
        for run in runs[1:]:
            run.text = ""


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


def insert_row_after(table, index, values, *, size=8.2, styled=()):
    template = table.rows[index]
    new_tr = copy.deepcopy(template._tr)
    template._tr.addnext(new_tr)
    row = table.rows[index + 1]
    for idx, value in enumerate(values):
        emphasised = idx in styled
        replace_cell(
            row.cells[idx], value, size=size,
            bold=emphasised, color=BLUE if emphasised else BLACK,
        )
    return row


def keep_rows_whole(table):
    from docx.oxml import OxmlElement
    from docx.oxml.ns import qn

    for row in table.rows:
        properties = row._tr.get_or_add_trPr()
        if properties.find(qn("w:cantSplit")) is None:
            properties.append(OxmlElement("w:cantSplit"))


def append_capability(table, entry, *, size=8.5):
    cell = table.rows[-2].cells[1]
    replace_cell(cell, cell.text.rstrip() + "\n" + entry, size=size)


def patch_mrd(source: Path, output: Path) -> None:
    doc = Document(str(source))
    doc.core_properties.title = f"XVS Module Requirements Document v{MRD_TARGET}"
    doc.core_properties.version = MRD_TARGET

    replace_cover_version(doc.tables[0], MRD_SOURCE, MRD_TARGET)
    update_control_table(
        doc.tables[1], version=MRD_TARGET, source_scope=MRD_SOURCE_SCOPE,
        entries="492",
    )
    for row in doc.tables[2].rows:
        if row.cells[0].text.strip().startswith("5."):
            replace_cell(
                row.cells[0], f"5. v{MRD_TARGET} Capability Delta",
                size=9, bold=True, color=BLUE,
            )
            replace_cell(
                row.cells[1],
                "Every school-facing surface reads the plan the gate enforces",
                size=9,
            )
    # Module entry counts: one new capability each in Modules 4, 6 and 9.
    #
    # Every count is rewritten, not only the three that change. Earlier
    # revisions edited this column with the default left-aligned 8.5pt run, so
    # the rows they touched stand out from the rest of a column that is
    # centred at 7pt; writing them all restores one column.
    from docx.enum.text import WD_ALIGN_PARAGRAPH

    new_counts = {"4": "21", "6": "26", "9": "21"}
    for row in doc.tables[5].rows[1:]:
        number = row.cells[0].text.strip()
        count = new_counts.get(number, row.cells[5].text.strip())
        replace_cell(
            row.cells[5], count, size=7,
            alignment=WD_ALIGN_PARAGRAPH.CENTER,
        )

    for paragraph in doc.paragraphs:
        text = paragraph.text.strip()
        if text == f"5. v{MRD_SOURCE} Capability Delta":
            retitle(paragraph, f"5. v{MRD_TARGET} Capability Delta")
        elif text.startswith("This revision records the console catching up"):
            retitle(paragraph, MRD_INTRO)

    append_capability(
        doc.tables[14], "▸  A role builder that offers only what the plan allows",
    )
    append_capability(
        doc.tables[18], "▸  A school reading the capabilities its own plan reaches",
    )
    append_capability(
        doc.tables[25], "▸  A checklist scoped to the steps the plan can perform",
    )

    append_to_cell(doc.tables[8].rows[-1].cells[0], M01_DECISION_MODULES, size=8.2)
    append_to_cell(doc.tables[14].rows[-1].cells[0], M04_DECISION, size=8.2)
    append_to_cell(doc.tables[18].rows[-1].cells[0], M06_DECISION, size=8.2)
    append_to_cell(doc.tables[25].rows[-1].cells[0], M09_DECISION, size=8.2)
    for row in doc.tables[27].rows:
        replace_in_cell(
            row.cells[0], STALE_DATASET_BULLET_MRD, FRESH_DATASET_BULLET_MRD,
        )
    append_to_cell(doc.tables[27].rows[-1].cells[0], M10_DECISION, size=8.2)

    # The delta table is replaced rather than extended: it still carried the
    # v2.67 heading and that revision's rows, and a delta describes the
    # revision it sits in.
    rebuild_table(
        doc.tables[76],
        [f"v{MRD_TARGET} capability delta", "Decision", "Evidence"],
        MRD_DELTA_ROWS,
        [1.85, 1.15, 4.05],
    )
    keep_rows_whole(doc.tables[76])

    prepend_change_log(doc.tables[78], MRD_TARGET, CHANGE_DATE, MRD_CHANGE_SUMMARY)

    output.parent.mkdir(parents=True, exist_ok=True)
    doc.save(str(output))
    update_extended_title(output, f"XVS Module Requirements Document v{MRD_TARGET}")
    shrink_inherited_media(output)
    assert_no_em_dash(output)


def patch_m04(source: Path, output: Path) -> None:
    doc = Document(str(source))
    title = (
        "XVS M04 Roles and Permissions RBAC Functional Requirements "
        f"Document v{M04_TARGET}"
    )
    doc.core_properties.title = title
    doc.core_properties.version = M04_TARGET

    fr025 = doc.tables[33]
    tenant_api = doc.tables[40]
    conditions = doc.tables[41]
    trace = doc.tables[45]
    reconciliation = doc.tables[46]
    change_log = doc.tables[47]

    replace_cover_version(doc.tables[0], M04_SOURCE, M04_TARGET)
    update_control_table(
        doc.tables[1], version=M04_TARGET, source_scope=M04_SOURCE_SCOPE,
        mrd_baseline=(
            f"XVS Module Requirements Document v{MRD_TARGET}, Module 4, "
            "twenty-one capability entries"
        ),
    )

    for row in fr025.rows:
        label = row.cells[0].text.strip()
        if label in M04_FR025_UPDATES:
            replace_cell(row.cells[1], M04_FR025_UPDATES[label], size=8.2)
        elif label == "Current evidence":
            # The stale half is replaced rather than contradicted: the cell
            # promised a refusal naming both depths, which is exactly what
            # stopped being true.
            evidence = row.cells[1].text.rstrip().replace(
                "naming the module, the depth needed and the depth held",
                "naming the product and no tier",
            )
            replace_cell(
                row.cells[1],
                evidence + (
                    " The tenant permission catalogue resolves the same "
                    "capability through the same function, "
                    "plan_gate.plan_reader, which evaluates through the bulk "
                    "evaluator the navigation already uses; each entry returns "
                    "capability, band, depth_label and a sentence when the plan "
                    "does not reach it."
                ),
                size=8.2,
            )

    insert_row_after(tenant_api, len(tenant_api.rows) - 1, M04_API_ROW)
    keep_rows_whole(tenant_api)

    for row in conditions.rows:
        # Matched on a fragment free of apostrophes: the document uses
        # typographic ones, so a straight quote here silently matches nothing.
        condition = row.cells[0].text.strip()
        if condition.startswith("A key the caller") and "does not reach" in condition:
            replace_cell(
                row.cells[1],
                "403 PLAN_UPGRADE_REQUIRED, naming the product and no tier. "
                "Deliberately not the role refusal: the person who can act on "
                "it is the proprietor, not the school administrator. The band "
                "and its depth travel as structured data for the console.",
                size=8.2,
            )

    anchor = len(conditions.rows) - 1
    for values in M04_CONDITION_ROWS:
        insert_row_after(conditions, anchor, values)
        anchor += 1
    keep_rows_whole(conditions)

    insert_row_after(trace, len(trace.rows) - 1, M04_TRACE_ROW)
    keep_rows_whole(trace)

    replace_cell(
        reconciliation.rows[0].cells[0],
        reconciliation.rows[0].cells[0].text.rstrip().replace(
            f"MRD v{MRD_SOURCE}", f"MRD v{MRD_TARGET}"
        ).replace(
            "with twenty capability entries", "with twenty-one capability entries"
        ) + (
            "\n• The count rises by one. A role builder that offers only "
            "what the plan allows is a product surface in its own right: "
            "before it, an administrator could compose a role from keys the "
            "gate would refuse and neither screen said which was wrong."
        ),
        size=8.2,
    )
    prepend_change_log(change_log, M04_TARGET, CHANGE_DATE, M04_CHANGE_SUMMARY)

    output.parent.mkdir(parents=True, exist_ok=True)
    doc.save(str(output))
    update_extended_title(output, title)
    shrink_inherited_media(output)
    assert_no_em_dash(output)


def patch_m09(source: Path, output: Path) -> None:
    doc = Document(str(source))
    title = (
        "XVS M09 School Onboarding Functional Requirements "
        f"Document v{M09_TARGET}"
    )
    doc.core_properties.title = title
    doc.core_properties.version = M09_TARGET

    fr001 = doc.tables[10]
    checklist = doc.tables[31]
    change_log = doc.tables[41]

    replace_cover_version(doc.tables[0], M09_SOURCE, M09_TARGET)
    update_control_table(
        doc.tables[1], version=M09_TARGET, source_scope=M09_SOURCE_SCOPE,
        mrd_baseline=(
            f"XVS Module Requirements Document v{MRD_TARGET}, Module 9, "
            "twenty-one capability entries"
        ),
    )

    for row in fr001.rows:
        label = row.cells[0].text.strip()
        if label in M09_FR001_UPDATES:
            replace_cell(row.cells[1], M09_FR001_UPDATES[label], size=8.2)

    rebuild_table(
        checklist,
        ["Key", "Required", "Completed when"],
        M09_CHECKLIST_ROWS,
        [1.55, 0.85, 4.65],
    )
    keep_rows_whole(checklist)

    prepend_change_log(change_log, M09_TARGET, CHANGE_DATE, M09_CHANGE_SUMMARY)

    output.parent.mkdir(parents=True, exist_ok=True)
    doc.save(str(output))
    update_extended_title(output, title)
    shrink_inherited_media(output)
    assert_no_em_dash(output)


def patch_m10(source: Path, output: Path) -> None:
    doc = Document(str(source))
    title = (
        "XVS M10 Bulk Data Import Functional Requirements "
        f"Document v{M10_TARGET}"
    )
    doc.core_properties.title = title
    doc.core_properties.version = M10_TARGET

    fr001 = doc.tables[8]
    dependencies = doc.tables[29]
    change_log = doc.tables[32]

    replace_cover_version(doc.tables[0], M10_SOURCE, M10_TARGET)
    update_control_table(
        doc.tables[1], version=M10_TARGET, source_scope=M10_SOURCE_SCOPE,
        mrd_baseline=(
            f"XVS Module Requirements Document v{MRD_TARGET}, Module 10, "
            "sixteen capability entries"
        ),
    )

    for row in fr001.rows:
        if row.cells[0].text.strip() == "Current evidence":
            append_to_cell(
                row.cells[1],
                " Reaching this engine at all is a second question, asked of "
                "the plan rather than the role: an administrator holds the "
                "import keys whether or not the school bought bulk import, so "
                "the navigation door, the onboarding card and the upload "
                "control each declare the capability alongside the permission. "
                "That capability is bulk_import, and it sits at Core, so every "
                "plan reaches this engine.",
                size=8.2,
            )

    # Traceability had drifted from the MRD it cites: it named v2.30, claimed
    # fifteen capabilities and listed fifteen, while the MRD carries sixteen.
    for paragraph in doc.paragraphs:
        if paragraph.text.strip().startswith(
            "Module 10 of XVS Module Requirements Document"
        ):
            retitle(
                paragraph,
                f"Module 10 of XVS Module Requirements Document v{MRD_TARGET} "
                "lists sixteen capabilities. Each maps to the requirements "
                "above.",
            )

    trace = doc.tables[31]
    insert_row_after(
        trace, len(trace.rows) - 1,
        ["Authorised, expiring access to uploaded source files",
         "FR-015",
         "Implemented. The source file stays retrievable to a holder of "
         "import.batches.view and is removed on its stated schedule."],
        styled=(0,),
    )
    keep_rows_whole(trace)

    for row in doc.tables[30].rows:
        replace_in_cell(
            row.cells[0], STALE_DATASET_BULLET_M10, FRESH_DATASET_BULLET_M10,
        )
        replace_in_cell(
            row.cells[0],
            "The onboarding checklist item it serves is optional, so "
            "readiness and go-live are not blocked; it simply cannot be "
            "completed through this module.",
            "The onboarding checklist item it serves is optional, and is "
            "absent altogether for a school whose plan does not reach bulk "
            "import, so readiness and go-live are not blocked either way.",
        )

    insert_row_after(
        dependencies, len(dependencies.rows) - 1,
        ["Module 6 - Configuration & Capability",
         "Decides whether this engine is reachable at all. bulk_import is a "
         "band of the platform module, so a school's plan depth answers it, "
         "and the surfaces that lead here read it before offering the door."],
    )
    keep_rows_whole(dependencies)

    prepend_change_log(change_log, M10_TARGET, CHANGE_DATE, M10_CHANGE_SUMMARY)

    output.parent.mkdir(parents=True, exist_ok=True)
    doc.save(str(output))
    update_extended_title(output, title)
    shrink_inherited_media(output)
    assert_no_em_dash(output)


# ── M01 ──────────────────────────────────────────────────────────────────────

M01_SOURCE_SCOPE = (
    "Backend change removing the module selection from school creation, so a "
    "plan is the whole of what a school is sold (9 September 2026)"
)

M01_CHANGE_SUMMARY = (
    "Corrects FR-007 by deleting the question it had stopped needing. School "
    "creation accepted a list of modules to enable, and had accepted it "
    "without effect since the plan began granting every module at its own "
    "depth: the same school came out of creation whichever boxes were ticked. "
    "The list was worse than inert, because an operator who left Procurement "
    "unticked had every reason to believe they had turned it off. "
    "enabled_modules is gone from the write serializer, from the creation "
    "path, from the bulk-import template and from its validation, which could "
    "refuse a whole school row over a module key that decided nothing. The "
    "read side keeps the name and now carries the depth beside each module, "
    "because what differs between two schools is the depth and not the length "
    "of the list. A payload still carrying a module list is ignored rather "
    "than refused, so a caller that has not caught up keeps working. The "
    "schools import template loses that column and three capacity columns left "
    "behind when plan ceilings were removed. Verified by 332 school, 61 import "
    "and 152 core tests, and by 827 console frontend tests. Backend evidence "
    "only; nothing here is deployed."
)

M01_FR007_UPDATES = {
    "Requirement":
        "School creation selects an active PackagePlan and records the "
        "subscription expiry, and the plan grants every module at the depth it "
        "reaches. There is no module selection: a plan is the whole of what a "
        "school is sold. No plan caps how many students, staff, administrators "
        "or Branches a School may have.",
}


def patch_m01(source: Path, output: Path) -> None:
    doc = Document(str(source))
    title = (
        "XVS M01 School and Branch Management Functional Requirements "
        f"Document v{M01_TARGET}"
    )
    doc.core_properties.title = title
    doc.core_properties.version = M01_TARGET

    fr007 = doc.tables[15]
    change_log = doc.tables[43]

    replace_cover_version(doc.tables[0], M01_SOURCE, M01_TARGET)
    update_control_table(
        doc.tables[1], version=M01_TARGET, source_scope=M01_SOURCE_SCOPE,
        mrd_baseline=(
            f"XVS Module Requirements Document v{MRD_TARGET}, Module 1, "
            "twenty-one capability entries"
        ),
    )

    for row in fr007.rows:
        label = row.cells[0].text.strip()
        if label in M01_FR007_UPDATES:
            replace_cell(row.cells[1], M01_FR007_UPDATES[label], size=8.2)
        elif label == "Current evidence":
            # The stale sentence is replaced, not appended to: it says the
            # field is accepted, which is what stopped being true.
            evidence = row.cells[1].text.rstrip().replace(
                "The enabled_modules list a client still sends is accepted and "
                "narrows nothing, so a frontend that has not caught up keeps "
                "working.",
                "The write serializer no longer carries enabled_modules, and "
                "neither does the bulk-import template or its validation. A "
                "payload still supplying one is ignored rather than refused, "
                "so a caller that has not caught up keeps working. The read "
                "side keeps the field and carries each module's depth beside "
                "it, which is the half that differs between two schools.",
            )
            replace_cell(row.cells[1], evidence, size=8.2)
        elif label == "Acceptance":
            replace_cell(
                row.cells[1],
                row.cells[1].text.rstrip().replace(
                    "including one the client did not name",
                    "there being no way to name one",
                ),
                size=8.2,
            )

    append_to_cell(doc.tables[6].rows[0].cells[0], M01_DECISION_MODULES, size=8.2)
    prepend_change_log(change_log, M01_TARGET, CHANGE_DATE, M01_CHANGE_SUMMARY)

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
        mrd / f"XVS_Module_Requirements_Document_v{MRD_SOURCE}.docx",
        mrd / f"XVS_Module_Requirements_Document_v{MRD_TARGET}.docx",
    )
    print(f"Wrote MRD v{MRD_TARGET}")

    fr = root / "functional-requirements"

    m01 = fr / "01-school-and-branch-management"
    stem01 = "XVS_M01_School_and_Branch_Management_Functional_Requirements_Document"
    patch_m01(m01 / f"{stem01}_v{M01_SOURCE}.docx", m01 / f"{stem01}_v{M01_TARGET}.docx")
    print(f"Wrote M01 FRD v{M01_TARGET}")

    m04 = fr / "04-roles-and-permissions-rbac"
    stem04 = "XVS_M04_Roles_and_Permissions_RBAC_Functional_Requirements_Document"
    patch_m04(m04 / f"{stem04}_v{M04_SOURCE}.docx", m04 / f"{stem04}_v{M04_TARGET}.docx")
    print(f"Wrote M04 FRD v{M04_TARGET}")

    m09 = fr / "09-school-onboarding"
    stem09 = "XVS_M09_School_Onboarding_Functional_Requirements_Document"
    patch_m09(m09 / f"{stem09}_v{M09_SOURCE}.docx", m09 / f"{stem09}_v{M09_TARGET}.docx")
    print(f"Wrote M09 FRD v{M09_TARGET}")

    m10 = fr / "10-bulk-data-import"
    stem10 = "XVS_M10_Bulk_Data_Import_Functional_Requirements_Document"
    patch_m10(m10 / f"{stem10}_v{M10_SOURCE}.docx", m10 / f"{stem10}_v{M10_TARGET}.docx")
    print(f"Wrote M10 FRD v{M10_TARGET}")


if __name__ == "__main__":
    main()

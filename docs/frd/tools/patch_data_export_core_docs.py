#!/usr/bin/env python3
"""Version the MRD for data export moving from Plus to Core.

The second half of a pair. Bulk import moved to Core in v2.69 because loading
a roll from a spreadsheet is how a school arrives; this records the same
argument read backwards, which is that a school unable to take its records out
is a school unable to leave.

Module 6 owns the band and Module 26 owns the product, and neither has an FRD
folder under docs/frd/functional-requirements. Both are therefore recorded in
the MRD alone, which is reported rather than worked around: creating a module
FRD is the user's call.

    python tools/patch_data_export_core_docs.py
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

REVIEW_DATE = "9 September 2026"
CHANGE_DATE = "9 Sep 2026"

MRD_SOURCE, MRD_TARGET = "2.70", "2.71"

SOURCE_SCOPE = (
    "Backend change moving data export from Plus to Core, so every school "
    "reaches the whole of the cross-cutting platform module whatever it pays "
    "(9 September 2026)"
)

INTRO = (
    "This revision finishes the pair the previous one started. Getting a "
    "school's records in and getting them out are the same promise read in "
    "two directions, and both are now on every plan."
)

DELTA_ROWS = [
    ["Data export and reporting", "Moved to Core",
     "Taking a filtered screen out as a file, saving an export to run again "
     "and scheduling the ones a school needs regularly. Priced at Plus, the "
     "only door out of the platform was a paid feature."],
    ["The platform module", "Wholly on every plan",
     "Email alerts were already Core and bulk import moved there in v2.69. "
     "With data export joining them the cross-cutting services are no longer "
     "sold by depth at all."],
    ["What a school pays", "Still not built",
     "No rate, no floor, no billing unit and no term. Two bands leaving the "
     "priced surface makes the commercial gap smaller, not closed."],
]

M06_DECISION = (
    "\n• Data export is Core rather than Plus, which puts the whole "
    "platform module on every plan. It is the mirror of the bulk import "
    "argument: a school that cannot get its records out is a school that "
    "cannot leave, and pricing the only door out is not a depth decision but "
    "a lock-in one. It is also ordinary work rather than a management "
    "capability - a bursar taking a filtered debtor list out as a file, a "
    "registrar handing over a roll - and none of that belongs above the "
    "cheapest plan. Both moved bands are pinned by test, so putting either "
    "back is a deliberate act rather than a seeder edit nobody notices."
)

M26_DECISION = (
    "\n• Every school reaches this module in full. The export engine was "
    "gated on the data_export band at Plus, so a school on the cheapest plan "
    "could read a screen and not take it away. The band is now Core: the "
    "catalogue, saved definitions, runs, files, schedules and the activity "
    "log answer to every plan. What still governs an export is the reader's "
    "role and the field-level rules on sensitive columns, which is a "
    "different question and unchanged."
)

CHANGE_SUMMARY = (
    "Moved data export from Plus to Core, finishing what the previous "
    "revision started with bulk import. The two are one promise read in "
    "opposite directions: a school that cannot load its records is one that "
    "cannot start, and a school that cannot take them out is one that cannot "
    "leave. Pricing the only door out above the cheapest plan made leaving a "
    "paid feature, and it also priced ordinary work - a bursar taking a "
    "filtered debtor list out as a file, a registrar handing the ministry a "
    "roll, an administrator saving the export they run every term. Every "
    "export key answers to the one data_export band apart from the gate "
    "deciding whether restricted columns may leave at all, which carries no "
    "band and is core to every school already, so this one move opens the "
    "catalogue, saved definitions, runs, files, schedules and the activity "
    "log together. Email alerts were already Core and bulk import "
    "moved there in v2.69, so the cross-cutting platform module is now wholly "
    "outside the depth model. What governs an export is unchanged and is a "
    "different question: the reader's role, and the field-level rules that "
    "decide whether restricted columns may leave at all. Module 6 owns the "
    "band and Module 26 owns the product; neither has an FRD, so both are "
    "recorded here. Verified by 514 RBAC and 105 configuration tests, "
    "including one pinning each moved band. Backend evidence only; nothing "
    "here is deployed."
)


STALE_EXPORT_SENTENCE = (
    " Data export stays at Plus, which is the reverse case: a school that "
    "has already loaded its records is asking to take them out in bulk."
)


def replace_in_cell(cell, old, new):
    """Swap a phrase without rewriting the cell and losing its styled banner."""
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


def update_control_table(table, *, version, source_scope):
    for row in table.rows:
        label = row.cells[0].text.strip()
        if label == "Version":
            replace_cell(row.cells[1], version, size=9)
        elif label == "Review date":
            replace_cell(row.cells[1], REVIEW_DATE, size=9)
        elif label == "Source scope":
            replace_cell(row.cells[1], source_scope, size=9)


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


def keep_rows_whole(table):
    from docx.oxml import OxmlElement
    from docx.oxml.ns import qn

    for row in table.rows:
        properties = row._tr.get_or_add_trPr()
        if properties.find(qn("w:cantSplit")) is None:
            properties.append(OxmlElement("w:cantSplit"))


def patch_mrd(source: Path, output: Path) -> None:
    doc = Document(str(source))
    doc.core_properties.title = f"XVS Module Requirements Document v{MRD_TARGET}"
    doc.core_properties.version = MRD_TARGET

    replace_cover_version(doc.tables[0], MRD_SOURCE, MRD_TARGET)
    update_control_table(
        doc.tables[1], version=MRD_TARGET, source_scope=SOURCE_SCOPE,
    )
    for row in doc.tables[2].rows:
        if row.cells[0].text.strip().startswith("5."):
            replace_cell(
                row.cells[0], f"5. v{MRD_TARGET} Capability Delta",
                size=9, bold=True, color=BLUE,
            )
            replace_cell(
                row.cells[1],
                "Data export joins bulk import on every plan",
                size=9,
            )

    for paragraph in doc.paragraphs:
        text = paragraph.text.strip()
        if text == f"5. v{MRD_SOURCE} Capability Delta":
            retitle(paragraph, f"5. v{MRD_TARGET} Capability Delta")
        elif text.startswith("This revision") and len(text) > 80:
            # Matched loosely on purpose: each revision writes its own opener,
            # so pinning the previous one's first three words leaves the delta
            # section introduced by the change before it.
            retitle(paragraph, INTRO)

    # v2.69 recorded that data export stayed at Plus, which is exactly what
    # this revision reverses; left in place it would contradict the bullet
    # appended directly below it.
    replace_in_cell(doc.tables[18].rows[-1].cells[0], STALE_EXPORT_SENTENCE, "")
    append_to_cell(doc.tables[18].rows[-1].cells[0], M06_DECISION, size=8.2)
    append_to_cell(doc.tables[63].rows[-1].cells[0], M26_DECISION, size=8.2)

    rebuild_table(
        doc.tables[76],
        [f"v{MRD_TARGET} capability delta", "Decision", "Evidence"],
        DELTA_ROWS,
        [1.85, 1.15, 4.05],
    )
    keep_rows_whole(doc.tables[76])

    prepend_change_log(doc.tables[78], MRD_TARGET, CHANGE_DATE, CHANGE_SUMMARY)

    output.parent.mkdir(parents=True, exist_ok=True)
    doc.save(str(output))
    update_extended_title(output, f"XVS Module Requirements Document v{MRD_TARGET}")
    shrink_inherited_media(output)
    assert_no_em_dash(output)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", default=Path(__file__).resolve().parents[1])
    args = parser.parse_args()
    mrd = Path(args.root) / "module-requirements"
    patch_mrd(
        mrd / f"XVS_Module_Requirements_Document_v{MRD_SOURCE}.docx",
        mrd / f"XVS_Module_Requirements_Document_v{MRD_TARGET}.docx",
    )
    print(f"Wrote MRD v{MRD_TARGET}")


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""Move Module 25 to In use Complete, and make the FRD say the same thing.

Adoption is not something backend evidence can establish, so this is recorded
on the product owner's statement that the dashboards are in use across
accounts rather than inferred from code. Backend stays Partial: the academic
half of the school still has no dashboard, and nothing has changed about that.

The FRD gains a patch version for one word. It said "the module is Partial",
which was unambiguous while both columns read Partial and is not any more.

    python tools/patch_m25_in_use_complete_docs.py
"""

from __future__ import annotations

import argparse
import copy
from pathlib import Path

from docx import Document
from docx.oxml import OxmlElement
from docx.oxml.ns import qn

from generate_requirements_documents import (
    BLUE,
    assert_no_em_dash,
    rebuild_table,
    shrink_inherited_media,
    update_extended_title,
    write_cell,
)

REVIEW_DATE = "9 September 2026"
CHANGE_DATE = "9 Sep 2026"

MRD_SOURCE, MRD_TARGET = "2.73", "2.74"
M25_SOURCE, M25_TARGET = "1.0", "1.1"

SOURCE_SCOPE = (
    "Product statement moving Module 25 to In use Complete, on the owner's "
    "report that dashboards are in use across accounts. Backend state "
    "unchanged (9 September 2026)"
)

INTRO = (
    "This revision changes one status on the product owner's statement rather "
    "than on backend evidence. Module 25's dashboards are in use across "
    "accounts; the academic half of them is still unbuilt, and its backend "
    "state is unchanged."
)

DELTA_ROWS = [
    ["Module 25 In use", "Partial to Complete",
     "Recorded on the product owner's report that dashboards are in use "
     "across accounts. Adoption is not something backend evidence can "
     "establish, so the source of this change is stated rather than implied."],
    ["Module 25 Backend", "Unchanged at Partial",
     "The academic half of the school still has no dashboard, because the "
     "modules that would produce one are planned rather than built. Nothing "
     "about that has changed."],
    ["M25 FRD", f"Patched to v{M25_TARGET}",
     "One sentence said 'the module is Partial', which was unambiguous while "
     "both columns read Partial. It now names the backend explicitly, so the "
     "two documents cannot be read as disagreeing."],
]

CHANGE_SUMMARY = (
    "Moved Module 25 to In use Complete. The dashboards are in use across "
    "accounts, which is the product owner's report rather than anything the "
    "backend can demonstrate, and the source is recorded here because this "
    "document otherwise reads as evidence-based throughout. Backend stays "
    "Partial and for the same reason as before: academic, attendance, student "
    "and guardian indicators are blocked by the modules that would produce "
    "them, nothing fabricates a figure in their place, and no requirement in "
    "M25 FRD v1.0 changed. The FRD is patched to v1.1 for one word: it said "
    "the module was Partial, which was unambiguous while both columns read "
    "Partial and would now read as a contradiction. No capability was added "
    "and the count stays at 492 across 31 modules. Documentation only; no "
    "code was touched."
)

M25_STALE_SENTENCE = (
    "The module is Partial, and the reason is worth stating plainly: the "
    "operational and financial half of the school has dashboards and the "
    "academic half does not, because the modules that would produce academic "
    "figures are planned rather than built."
)
M25_FRESH_SENTENCE = (
    "The module's backend is Partial, and the reason is worth stating "
    "plainly: the operational and financial half of the school has dashboards "
    "and the academic half does not, because the modules that would produce "
    "academic figures are planned rather than built. What exists is in use; "
    "what is missing was never built."
)

M25_FRD_CHANGE = (
    "Patch. Names the backend explicitly where the document said 'the module "
    "is Partial'. That was unambiguous while the MRD carried Partial in both "
    "columns; with Module 25 now recorded as In use Complete it would read as "
    "a contradiction. No requirement, acceptance, limit or traceability entry "
    "changed."
)


#: The MRD's own status fills. Amber is Partial, green is Complete, and a cell
#: whose text moves between them has to move its fill with it or the colour
#: goes on saying what the words no longer do.
AMBER = "FBF1D7"
GREEN = "E3F3E9"


def set_fill(cell, colour):
    properties = cell._tc.get_or_add_tcPr()
    shading = properties.find(qn("w:shd"))
    if shading is None:
        shading = OxmlElement("w:shd")
        properties.append(shading)
    shading.set(qn("w:val"), "clear")
    shading.set(qn("w:color"), "auto")
    shading.set(qn("w:fill"), colour)


def replace_cell(cell, text, **kwargs):
    while len(cell.paragraphs) > 1:
        paragraph = cell.paragraphs[-1]
        paragraph._p.getparent().remove(paragraph._p)
    write_cell(cell, text, **kwargs)


def replace_in_cell(cell, old, new):
    """Swap a phrase without rewriting the cell and losing its styling.

    Reports whether it matched, because a caller changing a status word also
    has to change the fill that colours it.
    """
    changed = False
    for paragraph in cell.paragraphs:
        if old not in paragraph.text:
            continue
        runs = paragraph.runs
        if not runs:
            continue
        runs[0].text = paragraph.text.replace(old, new)
        for run in runs[1:]:
            run.text = ""
        changed = True
    return changed


def replace_in_paragraph(paragraph, old, new):
    if old not in paragraph.text:
        return False
    runs = paragraph.runs
    if not runs:
        return False
    runs[0].text = paragraph.text.replace(old, new)
    for run in runs[1:]:
        run.text = ""
    return True


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


def update_control_table(table, *, version, source_scope=None):
    for row in table.rows:
        label = row.cells[0].text.strip()
        if label == "Version":
            replace_cell(row.cells[1], version, size=9)
        elif label == "Review date":
            replace_cell(row.cells[1], REVIEW_DATE, size=9)
        elif label == "Source scope" and source_scope:
            replace_cell(row.cells[1], source_scope, size=9)


def prepend_change_log(table, version, date, summary, *, size=8):
    template = table.rows[1]
    new_tr = copy.deepcopy(template._tr)
    template._tr.addprevious(new_tr)
    row = table.rows[1]
    replace_cell(row.cells[0], version, size=size)
    replace_cell(row.cells[1], date, size=size)
    replace_cell(row.cells[2], summary, size=size)


def keep_rows_whole(table):
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
                row.cells[1], "Module 25 is in use across accounts", size=9,
            )

    for paragraph in doc.paragraphs:
        text = paragraph.text.strip()
        if text == f"5. v{MRD_SOURCE} Capability Delta":
            retitle(paragraph, f"5. v{MRD_TARGET} Capability Delta")
        elif text.startswith("This revision") and len(text) > 80:
            retitle(paragraph, INTRO)

    # The module's own status band. Backend is deliberately untouched.
    band = doc.tables[60].rows[0]
    for cell in band.cells:
        if replace_in_cell(cell, "In use: Partial", "In use: Complete"):
            set_fill(cell, GREEN)

    # The module index carries the same pair and must not drift from it.
    from docx.enum.text import WD_ALIGN_PARAGRAPH

    for row in doc.tables[5].rows:
        if row.cells[0].text.strip() == "25":
            cell = row.cells[4]
            # Matched to the column it sits in: centred at 7pt, on the green
            # every other Complete carries.
            replace_cell(
                cell, "Complete", size=7,
                alignment=WD_ALIGN_PARAGRAPH.CENTER,
            )
            set_fill(cell, GREEN)

    # The gap list says why the module is Partial. With one column now
    # Complete, name the column.
    replace_in_cell(
        doc.tables[61].rows[-1].cells[0],
        "and that is why this module is Partial.",
        "and that is why this module's backend is Partial.",
    )

    rebuild_table(
        doc.tables[76],
        [f"v{MRD_TARGET} capability delta", "Decision", "Evidence"],
        DELTA_ROWS,
        [1.85, 1.35, 3.85],
    )
    keep_rows_whole(doc.tables[76])

    prepend_change_log(doc.tables[78], MRD_TARGET, CHANGE_DATE, CHANGE_SUMMARY)

    output.parent.mkdir(parents=True, exist_ok=True)
    doc.save(str(output))
    update_extended_title(output, f"XVS Module Requirements Document v{MRD_TARGET}")
    shrink_inherited_media(output)
    assert_no_em_dash(output)


def patch_m25_frd(source: Path, output: Path) -> None:
    doc = Document(str(source))
    title = (
        "XVS M25 Dashboards and Operational Analytics Functional Requirements "
        f"Document v{M25_TARGET}"
    )
    doc.core_properties.title = title
    doc.core_properties.version = M25_TARGET

    replace_cover_version(doc.tables[0], M25_SOURCE, M25_TARGET)
    for row in doc.tables[1].rows:
        label = row.cells[0].text.strip()
        if label == "Version":
            replace_cell(row.cells[1], M25_TARGET, size=9)
        elif label == "Review date":
            replace_cell(row.cells[1], REVIEW_DATE, size=9)
        elif label == "Source MRD":
            replace_cell(
                row.cells[1],
                f"XVS Module Requirements Document v{MRD_TARGET} | Module 25, "
                "twelve capability entries",
                size=9,
            )

    replaced = False
    for paragraph in doc.paragraphs:
        if replace_in_paragraph(paragraph, M25_STALE_SENTENCE, M25_FRESH_SENTENCE):
            replaced = True
            break
    if not replaced:
        raise SystemExit("M25 FRD: the sentence to patch was not found")

    prepend_change_log(
        doc.tables[-1], M25_TARGET, REVIEW_DATE, M25_FRD_CHANGE, size=8.2,
    )

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

    folder = root / "functional-requirements" / "25-dashboards-and-analytics"
    stem = (
        "XVS_M25_Dashboards_and_Operational_Analytics_Functional_Requirements_"
        "Document"
    )
    patch_m25_frd(
        folder / f"{stem}_v{M25_SOURCE}.docx",
        folder / f"{stem}_v{M25_TARGET}.docx",
    )
    print(f"Wrote M25 FRD v{M25_TARGET}")


if __name__ == "__main__":
    main()

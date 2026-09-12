#!/usr/bin/env python3
"""Cut MRD v2.78: the tracker reconciled to the procurement cancellation work.

Every module FRD whose behaviour moved since its last version is brought up to
the backend at commit 19a50b3, and this revision carries what moved into the
tracker: capability entries added or restated, Needs Attention and Current
Decision boxes rewritten as current state, per-module counts, the capability
total, the priority gaps and build order where a gap closed or changed shape,
the capability-delta section and the change log.

The content constants below are filled from the module revisions, and the
script refuses to run while any of them is still empty, so a placeholder can
never reach a published document.

    python tools/patch_mrd_v2_78_docs.py
"""

from __future__ import annotations

import argparse
import copy
from pathlib import Path

from docx import Document
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.table import Table
from docx.text.paragraph import Paragraph

from generate_requirements_documents import (
    BLUE,
    assert_no_em_dash,
    rebuild_table,
    shrink_inherited_media,
    update_extended_title,
    write_cell,
)


MRD_SOURCE, MRD_TARGET = "2.77", "2.78"
REVIEW_DATE, SHORT_DATE = "12 September 2026", "12 Sep 2026"

#: Table positions in v2.75. Bound once, before any edit, because an inserted
#: table would shift every later index.
COVER, CONTROL, CONTENTS, MODULE_INDEX = 0, 1, 2, 5
PRIORITY_GAPS, DELTA, BUILD_ORDER, CHANGE_LOG = 75, 76, 77, 78

#: Each module's capability grid. Its last row is the module's Needs Attention
#: or Current Decision box.
CAPABILITIES = {
    1: 8, 2: 10, 3: 12, 4: 14, 5: 16, 6: 18, 7: 20, 8: 23, 9: 25, 10: 27,
    11: 30, 12: 32, 13: 34, 14: 36, 15: 38, 16: 40, 17: 43, 18: 45, 19: 47,
    20: 49, 21: 52, 22: 54, 23: 56, 24: 58, 25: 61, 26: 63, 27: 65, 28: 67,
    29: 70, 30: 72, 31: 74,
}

DELTA_WIDTHS = [1.85, 1.35, 3.85]


# ── content, filled from the module revisions ────────────────────────────────

MRD_SOURCE_SCOPE = (
    "The requisition's reversal rule rewritten to ask what its orders did, and a "
    "purchase-order cancellation route for the dead end it pointed at, at b1b270d "
    "(12 September 2026)"
)

MRD_CONTENTS_NOTE = (
    "One capability entry added, and the dead end the last revision left standing"
)

MRD_INTRO = (
    "This revision records one capability and the dead end that made it necessary. "
    "v2.77 recorded that a requisition's approval could not be undone once an order had "
    "been raised from it, and the refusal a buyer met told them to cancel that order. No "
    "route cancelled one, so a requisition whose order was cancelled, or whose vendor "
    "stopped trading, stayed approved for ever with nothing standing against it. The rule "
    "now asks what a requisition's orders did rather than whether one was ever raised, and "
    "cancelling an order is something a buyer can reach, with a reason on the record."
)

MRD_CHANGE_SUMMARY = (
    "Closes the dead end v2.77 left standing. The requisition's reversal refusal asked "
    "whether a purchase order had ever been raised from it, so an order raised and then "
    "cancelled blocked its requisition for ever, and the refusal told the buyer to cancel "
    "an order that was already cancelled. It asks what those orders did now: goods received "
    "refuse whatever state their order is in, because stock on the shelf and the liability "
    "behind it are not handed back by cancelling the order they arrived against; an order "
    "still live refuses as a commitment to a vendor; and a requisition whose every order was "
    "cancelled without a delivery is reversible. Cancelling a purchase order becomes a route "
    "a buyer can reach, on the verb that already writes to an order so no school edits its "
    "roles first, with a written reason required and four refusals of its own: goods "
    "received, a vendor bill standing against the order or one of its lines, an approval "
    "still in flight, and an order already closed. That becomes a capability entry of its "
    "own, taking Module 23 from 24 to 25 and the platform from 501 to 502 across 31 modules. "
    "A cancelled order still reads approved, because it was, and the ledger status carries "
    "the cancellation. The vendor is told nothing, which Module 23 now records twice so no "
    "reader infers the supplier was informed. M07 advances to v1.11, M22 to v1.10 and M23 to "
    "v1.12. Backend evidence only: nothing here claims frontend delivery, deployment or "
    "production adoption."
)

MRD_DELTA_ROWS = [
    ["Module 23 capabilities", "24 to 25",
     "Cancelling a purchase order with a recorded reason becomes an entry of its own. The "
     "refusal that sent a buyer to cancel an order was pointing at nothing, because no route "
     "closed one."],
    ["Requisition reversal", "Asks what its orders did",
     "It asked whether an order had ever been raised, so a cancelled order blocked its "
     "requisition for ever. Goods received refuse whatever state their order is in now, an "
     "order still live refuses, and a requisition whose every order was cancelled without a "
     "delivery is reversible."],
    ["Purchase-order cancellation", "A route a buyer can reach",
     "On the verb that already writes to an order, so nobody edits a role before following "
     "the instruction they were already being given. A written reason is required, and goods "
     "received, a standing vendor bill, an approval in flight and an order already closed "
     "each refuse in their own words."],
    ["A cancelled order", "Still reads approved",
     "It genuinely was approved, and rewriting that would lose the fact that somebody "
     "authorised the spend. The ledger status carries the cancellation, and the order leaves "
     "the pipeline the vendor is fulfilling."],
    ["The vendor", "Is told nothing",
     "This module has one vendor-facing order message and no cancellation notice, so a "
     "supplier who received an order learns of its withdrawal when a person rings them. The "
     "refusals protect the ledger; nothing protects the supplier's expectations except that "
     "call, which M23 v1.12 records twice."],
    ["Module FRDs", "Three revised",
     "M07 v1.11, M22 v1.10 and M23 v1.12, all against b1b270d."],
]

#: Module number -> changes; see the previous revision's script for the shape.
MODULE_CHANGES: dict[int, dict] = {
    7: {
        "attention": [
            "NEEDS ATTENTION",
            "• An approval route with no live steps is refused rather than treated as approval. "
            "A tenant that has not built its ladder resolves to the shared platform row, which "
            "carries no live steps, and a ladder whose every step is retired counts as having "
            "none. Submission raises a named refusal that the submitter may confirm past, "
            "recorded against whoever gives it.",
            "• Continue-without-approval remains available only where a document handler "
            "permits it. Payouts and role changes forbid it; permitted document types still "
            "rely on audit rather than a second reviewer.",
            "• A role change may be decided by the person who raised it, the one document type "
            "with that exemption, and its stage completes on any one approval. An administrator "
            "can therefore give their own role a restricted permission with nobody else "
            "looking, even where the school has a second administrator; the ladder and "
            "Module 4's audit record that they did.",
            "• Reversal is answered by the module that owns the document, and every registered "
            "finance, procurement, payment, user-creation and role-change type answers it: "
            "procurement refuses a requisition while an order raised from it still stands or "
            "took delivery, an order that reached its vendor or was received against, and a "
            "bill or a payment that has posted. The base handler still allows every reversal, "
            "so a leave request, and any document type registered later, is guarded by nothing "
            "until its own handler answers.",
            "• Timeouts, escalation timers, and template versioning remain future enhancements, "
            "not completed features.",
        ],
    },
    23: {
        "count": 25,
        "add": [(
            "▸  Approval reversal refused once an order, bill or payment has been acted on",
            "▸  Purchase-order cancellation with a recorded reason",
        )],
        "blurb_edits": [(
            "An emailed purchase order now presents structured order, delivery, payment, "
            "attachment, note, and buyer-contact details.",
            "An emailed purchase order presents structured order, delivery, payment, "
            "attachment, note, and buyer-contact details, and a commitment nobody will fulfil "
            "can be cancelled with a recorded reason.",
        )],
        "attention": [
            "NEEDS ATTENTION",
            "• Bills with no purchase order are refused by default: nothing three-way matches "
            "them, so approval would be their only control. The setting remains available for "
            "an entity that genuinely bills without orders.",
            "• Branch scoping of the purchase chain is finished. A purchase order, goods "
            "receipt, vendor bill and vendor payment each carry the branch they were raised at "
            "or inherit it from the document they continue, every list, queue and summary "
            "narrows to the caller's branches, and a branch-bound caller can neither settle nor "
            "read another branch's bill.",
            "• Reversing an approval is refused once the document has been acted on. Each type "
            "answers for what its own approval released: a requisition while an order raised "
            "from it still stands or has taken delivery, an order once it reached the vendor or "
            "goods were received against it, and a bill or a payment once its status has left "
            "draft or pending approval, which is what posting does to it. A requisition whose "
            "every order was cancelled without a delivery is reversible, because that approval "
            "released nothing that outlives it.",
            "• Cancelling a purchase order tells the vendor nothing. This module has one "
            "vendor-facing order message, the order itself, so a supplier who received an order "
            "and then has it cancelled learns of the withdrawal only when somebody rings them. "
            "The refusals protect the ledger, since goods received or a bill that is not itself "
            "cancelled both stop the cancellation, but nothing protects the supplier's "
            "expectations except a person.",
        ],
    },
}

#: Body paragraphs outside any grid, keyed by the start of their current text.
BODY_CHANGES: dict[str, str] = {}

#: Priority-gap rows, keyed by the text of their Gap cell -> {column: text}.
GAP_CHANGES: dict[str, dict[int, str]] = {}

#: Build-order rows, keyed by their step number -> {column: text}.
BUILD_ORDER_CHANGES: dict[str, dict[int, str]] = {}


# ── helpers ──────────────────────────────────────────────────────────────────

def replace_cell(cell, text: str, **kwargs) -> None:
    """Rewrite a single-line cell, dropping any extra paragraphs it carried."""
    while len(cell.paragraphs) > 1:
        paragraph = cell.paragraphs[-1]
        paragraph._p.getparent().remove(paragraph._p)
    write_cell(cell, text, **kwargs)


def retitle(paragraph, text: str) -> None:
    """Rewrite a paragraph in place, keeping the formatting of its first run."""
    runs = paragraph.runs
    if not runs:
        paragraph.add_run(text)
        return
    runs[0].text = text
    for run in runs[1:]:
        run.text = ""


def set_lines(cell, lines: list[str]) -> None:
    """Rewrite a heading-plus-bullets cell without flattening it.

    Paragraph i is reused for line i, so the bold coloured heading stays the
    heading and every bullet keeps bullet formatting. Overflow clones the LAST
    paragraph (a bullet), never the first, which would turn each new line into
    a copy of the heading.
    """
    paragraphs = list(cell.paragraphs)
    for i, line in enumerate(lines):
        if i < len(paragraphs):
            retitle(paragraphs[i], line)
        else:
            last = cell.paragraphs[-1]
            clone = copy.deepcopy(last._p)
            last._p.addnext(clone)
            retitle(Paragraph(clone, cell), line)
    for extra in list(cell.paragraphs)[len(lines):]:
        extra._p.getparent().remove(extra._p)


BOX_HEADINGS = ("NEEDS ATTENTION", "CURRENT DECISION", "CURRENT CONTROL")


def set_box(cell, lines: list[str]) -> None:
    """Rewrite a module's Needs Attention or Current Decision box.

    The boxes do not share one shape. Most hold a bold heading paragraph and a
    paragraph per bullet, some hold the heading and every bullet in one run
    separated by line breaks, and a few are one bold run throughout. Each line
    becomes its own paragraph cloned from what the box already holds, so the
    font and size stay the box's own, and the heading is then made bold in the
    box's heading colour with every bullet plain black. A row that is not a box
    is refused rather than overwritten, because one grid ends on a capability
    row instead.
    """
    from docx.shared import RGBColor

    if not cell.paragraphs[0].text.strip().startswith(BOX_HEADINGS):
        raise ValueError(f"Not a Needs Attention or Current Decision box: {cell.text[:60]!r}")
    if not lines[0].startswith(BOX_HEADINGS):
        raise ValueError(f"A box starts with its heading: {lines[0]!r}")
    colour = "2E5495"
    first = cell.paragraphs[0].runs[0] if cell.paragraphs[0].runs else None
    if first is not None and first.bold and first.font.color and first.font.color.type:
        colour = str(first.font.color.rgb)
    set_lines(cell, lines)
    for i, paragraph in enumerate(cell.paragraphs):
        for run in paragraph.runs:
            run.bold = i == 0
            run.font.color.rgb = RGBColor.from_string(colour if i == 0 else "000000")


def box_lines(cell) -> list[str]:
    """A box's heading and bullets, one entry each, whatever its layout."""
    lines = [line.strip() for p in cell.paragraphs for line in p.text.split("\n") if line.strip()]
    if not lines or lines[0] not in BOX_HEADINGS:
        raise ValueError(f"Box heading is not on a line of its own: {lines[:1]!r}")
    return lines


def edit_lines(lines: list[str], edits) -> list[str]:
    """Apply ("replace", start, new) and ("append", new) edits to box lines.

    A replace names the start of exactly one existing line, so an edit can
    never land on the wrong bullet or silently miss.
    """
    lines = list(lines)
    for edit in edits:
        if edit[0] == "replace":
            _, start, new = edit
            hits = [i for i, line in enumerate(lines) if line.startswith(start)]
            if len(hits) != 1:
                raise ValueError(f"{start!r} starts {len(hits)} box lines")
            lines[hits[0]] = new
        elif edit[0] == "append":
            lines.append(edit[1])
        elif edit[0] == "remove":
            _, start = edit
            hits = [i for i, line in enumerate(lines) if line.startswith(start)]
            if len(hits) != 1:
                raise ValueError(f"{start!r} starts {len(hits)} box lines")
            del lines[hits[0]]
        else:
            raise ValueError(f"Unknown box edit: {edit[0]!r}")
    return lines


def find_bullet(table, text: str):
    for row in table.rows:
        for cell in row.cells:
            for paragraph in cell.paragraphs:
                if paragraph.text.strip() == text.strip():
                    return cell, paragraph
    raise ValueError(f"Capability bullet not found: {text!r}")


def add_bullet(table, anchor: str, text: str) -> None:
    """Place a new capability bullet directly after the anchor bullet.

    A grid cell holds its bullets either as a paragraph each or as one run whose
    lines are separated by breaks, and both shapes occur in the same document.
    A bullet of its own is cloned, so the copy keeps the run formatting; a
    bullet inside a run is inserted as another line of that run, which keeps it
    for the same reason.
    """
    try:
        cell, paragraph = find_bullet(table, anchor)
    except ValueError:
        paragraph = None
    if paragraph is not None:
        clone = copy.deepcopy(paragraph._p)
        paragraph._p.addnext(clone)
        retitle(Paragraph(clone, cell), text)
        return
    for row in table.rows:
        for cell in row.cells:
            for paragraph in cell.paragraphs:
                lines = paragraph.text.split("\n")
                if not any(line.strip() == anchor.strip() for line in lines):
                    continue
                if len(paragraph.runs) != 1:
                    raise ValueError(f"Anchor line sits in {len(paragraph.runs)} runs: {anchor!r}")
                at = next(i for i, line in enumerate(lines) if line.strip() == anchor.strip())
                lines.insert(at + 1, text)
                paragraph.runs[0].text = "\n".join(lines)
                return
    raise ValueError(f"Capability bullet not found: {anchor!r}")


def set_status(table, label: str, value: str) -> None:
    """Rewrite one "Label: value" cell of a module's status line.

    The bold label run is rewritten as well as the value run, because a cell
    whose value drifted into the label run would otherwise keep a bold stale
    value beside the new one.
    """
    for cell in table.rows[0].cells:
        paragraph = cell.paragraphs[0]
        if paragraph.text.strip().startswith(f"{label}:"):
            runs = paragraph.runs
            runs[0].text = f"{label}: "
            if len(runs) > 1:
                runs[1].text = value
                for run in runs[2:]:
                    run.text = ""
            else:
                run = copy.deepcopy(runs[0]._r)
                runs[0]._r.addnext(run)
                paragraph.runs[1].text = value
                paragraph.runs[1].bold = None
            return
    raise ValueError(f"Status cell not found: {label!r}")


def replace_bullet(table, current: str, new: str) -> None:
    """Rewrite one capability bullet, whichever way the grid keeps it.

    A bullet is either a paragraph of its own or one line of a run it shares with
    its neighbours, and both shapes occur in the same grid. The line case is
    rewritten inside the run for the reason :func:`add_bullet` clones rather than
    creates: the formatting belongs to the run, so leaving the text there keeps it.
    """
    try:
        _, paragraph = find_bullet(table, current)
    except ValueError:
        paragraph = None
    if paragraph is not None:
        retitle(paragraph, new)
        return
    for row in table.rows:
        for cell in row.cells:
            for paragraph in cell.paragraphs:
                lines = paragraph.text.split("\n")
                if not any(line.strip() == current.strip() for line in lines):
                    continue
                if len(paragraph.runs) != 1:
                    raise ValueError(f"Bullet sits in {len(paragraph.runs)} runs: {current!r}")
                at = next(i for i, line in enumerate(lines) if line.strip() == current.strip())
                lines[at] = new
                paragraph.runs[0].text = "\n".join(lines)
                return
    raise ValueError(f"Capability bullet not found: {current!r}")


def replace_cover_version(table, source: str, target: str) -> None:
    """Rewrite the version on the cover, preferring the labelled "Version: x" run.

    The bare number is the fallback, not the first try, because a date or a
    count elsewhere on the cover can contain it too.
    """
    runs = [run for paragraph in table.rows[0].cells[0].paragraphs for run in paragraph.runs]
    for needle, value in ((f"Version: {source}", f"Version: {target}"), (source, target)):
        for run in runs:
            if needle in run.text:
                run.text = run.text.replace(needle, value)
                return
    raise ValueError(f"Cover version not found: {source}")


def keep_rows_whole(table) -> None:
    from docx.oxml import OxmlElement
    from docx.oxml.ns import qn

    for row in table.rows:
        properties = row._tr.get_or_add_trPr()
        if properties.find(qn("w:cantSplit")) is None:
            properties.append(OxmlElement("w:cantSplit"))


def prepend_change_log(table, version: str, date: str, summary: str) -> None:
    template = table.rows[1]
    template._tr.addprevious(copy.deepcopy(template._tr))
    row = table.rows[1]
    replace_cell(row.cells[0], version, size=8)
    replace_cell(row.cells[1], date, size=8)
    replace_cell(row.cells[2], summary, size=8)


def module_blurbs(doc) -> dict[int, Paragraph]:
    """Each module's description: the first paragraph after its status table."""
    blurbs, pending, seen_table = {}, None, False
    for element in doc.element.body.iterchildren():
        tag = element.tag.split("}")[1]
        if tag == "p":
            text = Paragraph(element, doc).text.strip()
            if text.startswith("Module ") and ":" in text[:11]:
                pending, seen_table = int(text.split(":")[0].split()[1]), False
            elif pending is not None and seen_table and text:
                blurbs[pending] = Paragraph(element, doc)
                pending = None
        elif tag == "tbl" and pending is not None:
            seen_table = True
    return blurbs


def row_keyed(table, column: int, key: str):
    for row in table.rows:
        if row.cells[column].text.strip() == key:
            return row
    raise ValueError(f"Row not found: {key!r}")


def require_filled() -> None:
    missing = [name for name, value in (
        ("MRD_SOURCE_SCOPE", MRD_SOURCE_SCOPE),
        ("MRD_CONTENTS_NOTE", MRD_CONTENTS_NOTE),
        ("MRD_INTRO", MRD_INTRO),
        ("MRD_CHANGE_SUMMARY", MRD_CHANGE_SUMMARY),
        ("MRD_DELTA_ROWS", MRD_DELTA_ROWS),
    ) if not value]
    if missing:
        raise SystemExit(f"Refusing to cut v{MRD_TARGET}: unfilled {', '.join(missing)}")


# ── the revision ─────────────────────────────────────────────────────────────

def patch_mrd(source: Path, output: Path, *, check: bool = False) -> None:
    """Write v2.76 from v2.75, or with ``check`` only validate the layout."""
    if not check:
        require_filled()
    doc = Document(str(source))
    tables = doc.tables
    cover, control, contents = tables[COVER], tables[CONTROL], tables[CONTENTS]
    index, gaps, delta = tables[MODULE_INDEX], tables[PRIORITY_GAPS], tables[DELTA]
    build, log = tables[BUILD_ORDER], tables[CHANGE_LOG]
    grids: dict[int, Table] = {m: tables[i] for m, i in CAPABILITIES.items()}

    # Refuse a source whose layout has moved rather than edit the wrong cells.
    assert delta.rows[0].cells[0].text.strip().startswith(f"v{MRD_SOURCE} capability delta")
    assert log.rows[0].cells[0].text.strip() == "Version"
    assert index.rows[0].cells[5].text.strip() == "Entries"
    statuses: dict[int, Table] = {m: tables[i - 1] for m, i in CAPABILITIES.items()}
    for module, grid in grids.items():
        assert grid.rows[0].cells[0].text.strip().startswith("▸"), module
        assert statuses[module].rows[0].cells[0].text.strip().startswith("Phase:"), module

    if check:
        blurbs = module_blurbs(doc)
        missing = sorted(set(grids) - set(blurbs))
        cover_runs = [r.text for p in cover.rows[0].cells[0].paragraphs for r in p.runs
                      if MRD_SOURCE in r.text]
        total = sum(int(row.cells[5].text.strip()) for row in index.rows[1:])
        print(f"layout OK: {len(grids)} capability grids, {len(blurbs)} module blurbs"
              + (f", NO blurb for {missing}" if missing else ""))
        print(f"cover runs naming {MRD_SOURCE}: {cover_runs}")
        print(f"index total: {total}")
        return

    replace_cover_version(cover, MRD_SOURCE, MRD_TARGET)

    for row in control.rows:
        label = row.cells[0].text.strip()
        if label == "Version":
            replace_cell(row.cells[1], MRD_TARGET, size=9)
        elif label == "Review date":
            replace_cell(row.cells[1], REVIEW_DATE, size=9)
        elif label == "Source scope":
            replace_cell(row.cells[1], MRD_SOURCE_SCOPE, size=9)

    for row in contents.rows:
        if row.cells[0].text.strip().startswith("5."):
            replace_cell(row.cells[0], f"5. v{MRD_TARGET} Capability Delta",
                         size=9, bold=True, color=BLUE)
            replace_cell(row.cells[1], MRD_CONTENTS_NOTE, size=9)

    blurbs = module_blurbs(doc)
    for paragraph in doc.paragraphs:
        text = paragraph.text.strip()
        if text == f"5. v{MRD_SOURCE} Capability Delta":
            retitle(paragraph, f"5. v{MRD_TARGET} Capability Delta")
        elif text.startswith("This revision") and len(text) > 80:
            retitle(paragraph, MRD_INTRO)

    for module, changes in MODULE_CHANGES.items():
        grid = grids[module]
        for current, new in changes.get("restate", []):
            replace_bullet(grid, current, new)
        for anchor, new in changes.get("add", []):
            add_bullet(grid, anchor, new)
        if changes.get("attention"):
            set_box(grid.rows[-1].cells[0], changes["attention"])
        if changes.get("box_edits"):
            box = grid.rows[-1].cells[0]
            set_box(box, edit_lines(box_lines(box), changes["box_edits"]))
        if changes.get("blurb"):
            retitle(blurbs[module], changes["blurb"])
        for old, new in changes.get("blurb_edits", []):
            text = blurbs[module].text
            if text.count(old) != 1:
                raise ValueError(f"Module {module} description holds {old!r} {text.count(old)} times")
            retitle(blurbs[module], text.replace(old, new))
        index_columns = {"Backend": 3, "In use": 4}
        for label, value in changes.get("status", {}).items():
            set_status(statuses[module], label, value)
            if label in index_columns:
                replace_cell(row_keyed(index, 0, str(module)).cells[index_columns[label]], value,
                             size=7, alignment=WD_ALIGN_PARAGRAPH.CENTER)
        if changes.get("count") is not None:
            replace_cell(row_keyed(index, 0, str(module)).cells[5], str(changes["count"]),
                         size=7, alignment=WD_ALIGN_PARAGRAPH.CENTER)

    for start, text in BODY_CHANGES.items():
        hits = [p for p in doc.paragraphs if p.text.strip().startswith(start)]
        if len(hits) != 1:
            raise ValueError(f"{start!r} starts {len(hits)} body paragraphs")
        retitle(hits[0], text)

    # The total is the index's own sum, never a number typed in beside it.
    total = sum(int(row.cells[5].text.strip()) for row in index.rows[1:])
    for row in control.rows:
        if row.cells[0].text.strip() == "Capability entries":
            replace_cell(row.cells[1], str(total), size=9)

    for gap, columns in GAP_CHANGES.items():
        row = row_keyed(gaps, 1, gap)
        for column, text in columns.items():
            replace_cell(row.cells[column], text, size=8)

    for step, columns in BUILD_ORDER_CHANGES.items():
        row = row_keyed(build, 0, step)
        for column, text in columns.items():
            replace_cell(row.cells[column], text, size=8)

    rebuild_table(delta, [f"v{MRD_TARGET} capability delta", "Decision", "Evidence"],
                  MRD_DELTA_ROWS, DELTA_WIDTHS)
    keep_rows_whole(delta)

    prepend_change_log(log, MRD_TARGET, SHORT_DATE, MRD_CHANGE_SUMMARY)

    title = f"XVS Module Requirements Document v{MRD_TARGET}"
    doc.core_properties.title = title
    doc.core_properties.version = MRD_TARGET
    output.parent.mkdir(parents=True, exist_ok=True)
    doc.save(str(output))
    update_extended_title(output, title)
    shrink_inherited_media(output)
    assert_no_em_dash(output)
    print(f"Wrote MRD v{MRD_TARGET}: {total} capability entries")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", default=Path(__file__).resolve().parents[1])
    parser.add_argument("--check", action="store_true",
                        help="Validate the layout assumptions; write nothing.")
    args = parser.parse_args()
    folder = Path(args.root) / "module-requirements"
    target = folder / f"XVS_Module_Requirements_Document_v{MRD_TARGET}.docx"
    source = folder / f"XVS_Module_Requirements_Document_v{MRD_SOURCE}.docx"
    if args.check:
        patch_mrd(source, target, check=True)
        return
    if target.exists():
        raise SystemExit(f"{target.name} already exists; never overwrite a version")
    patch_mrd(source, target)


if __name__ == "__main__":
    main()

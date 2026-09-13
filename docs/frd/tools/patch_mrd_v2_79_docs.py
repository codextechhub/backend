#!/usr/bin/env python3
"""Cut MRD v2.79: the tracker reconciled to the reversal contract and the
settlement report.

Every module FRD whose behaviour moved since its last version is brought up to
the backend at commit 5678e86, and this revision carries what moved into the
tracker: capability entries added or restated, Needs Attention and Current
Decision boxes rewritten as current state, per-module counts, the capability
total, the priority gaps and build order where a gap closed or changed shape,
the capability-delta section and the change log.

The content constants below are filled from the module revisions, and the
script refuses to run while any of them is still empty, so a placeholder can
never reach a published document.

    python tools/patch_mrd_v2_79_docs.py
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


MRD_SOURCE, MRD_TARGET = "2.78", "2.79"
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

# These modules otherwise leave their Needs Attention row alone on the next page.
MODULE_PAGE_BREAKS = (15, 24)


# ── content, filled from the module revisions ────────────────────────────────

MRD_SOURCE_SCOPE = (
    "The engine's reversal contract made mandatory, and the settlement report carried by "
    "every write that narrows a school's reach, at 8919f61 and 5678e86 (12 September 2026)"
)

MRD_CONTENTS_NOTE = "Two contracts finished, and the limits that closed with them"

MRD_INTRO = (
    "This revision records two contracts finished rather than begun, so no capability is "
    "added and the platform stays at 502 entries. The engine has asked the module that owns "
    "a document whether an approval has already had an effect since v2.76, but the question "
    "carried a permissive default: a type that never answered was reversed with its effect "
    "still standing, and silence read exactly like a type whose approval genuinely releases "
    "nothing. A school's role grants have followed its reach since v2.77, but one write that "
    "narrows that reach settled nothing at all, and another settled with nowhere to say what "
    "it could not settle. What changes here is that two rules the tracker already recorded "
    "now hold everywhere they claimed to."
)

MRD_CHANGE_SUMMARY = (
    "Finishes two contracts the tracker had already recorded. A document type that has not "
    "said what its approval releases no longer registers: the refusal fires while the apps "
    "load, the base handler refuses by default behind it, and a type whose approval releases "
    "nothing declares that in as many words. All fourteen registered types answer, "
    "procurement's four answer individually rather than through one permissive default, and "
    "leave requests gain the rule they never had, since approval releases the absence itself: "
    "leave that has not started is reversible, leave running or already taken is refused, and "
    "the remedy is to cancel the request, where the person reads as present again. On the "
    "configuration side, clearing a school's own entitlement layer now settles its role grants "
    "like every other write that narrows a reach, which matters because a tenant's own row "
    "wins over the platform row while it exists, so deleting it returned the school to the "
    "house depth with its people still holding keys it could no longer reach. Both entitlement "
    "routes now name the roles they could not settle, in the same field and the same sentence "
    "the plan endpoints use, and the report is an argument of the settlement itself with no "
    "default, so a settle site cannot perform one without saying where its report goes. One "
    "limit is recorded rather than hidden: the entitlement POST carries no depth field, so it "
    "cannot shallow a module and its report is empty by construction until one exists. No "
    "capability is added, no status moves, and the platform stays at 502 across 31 modules. "
    "M04 advances to v1.20, M06 to v1.3, M07 to v1.12 and M12 to v2.5. Backend evidence only: "
    "nothing here claims frontend delivery, deployment or production adoption."
)

MRD_DELTA_ROWS = [
    ["Approval reversal", "Declared, or the type does not register",
     "The engine's question had a permissive default, so a type that never answered was "
     "reversed with its effect still standing and nothing said so. A handler that declares "
     "nothing now fails while the apps load, the base default refuses behind it, and a type "
     "whose approval releases nothing says so in as many words."],
    ["Leave", "Cannot be undone once the absence has begun",
     "Approval releases the absence itself: nobody is stored as On Leave, the directory "
     "derives it from an approved request covering today, and cover is arranged from what it "
     "reads. Leave not yet started is reversible; leave running or already taken is refused, "
     "and the request is cancelled instead, where the person reads as present again."],
    ["Clearing a school's own entitlement", "Settles its roles like every other narrowing",
     "A tenant's own entitlement row wins over the platform row while it exists, so deleting "
     "it returns the school to the house depth. That write settled nothing at all, leaving "
     "people holding keys the school could no longer reach."],
    ["A write that narrows a school's reach", "Says what it could not settle",
     "The configuration console settled a school's roles with nowhere to report a role it "
     "could not save, so one screen told an operator plainly what another did not. Both "
     "entitlement routes now carry the same field and the same sentence the plan screens use."],
    ["The report itself", "An argument of the settlement, with no default",
     "A settle site cannot perform a settlement without saying where its report goes, which is "
     "what stops the next one being forgotten. The entitlement POST carries no depth field, so "
     "it cannot shallow a module and its report stays empty until one exists."],
    ["Module FRDs", "Four revised",
     "M04 v1.20, M06 v1.3, M07 v1.12 and M12 v2.5, all against 5678e86."],
]

#: Module number -> changes; see the previous revision's script for the shape.
MODULE_CHANGES: dict[int, dict] = {
    4: {
        "box_edits": [
            ("replace", "• A role that cannot be settled is reported, never enforced.",
             "• A role that cannot be settled is reported, never enforced. When a school's "
             "reach narrows, a role whose kept key depends on one being taken away is left "
             "exactly as it was, named in the response of the write that settled it and "
             "recorded in the audit trail under a source of its own, while every other role in "
             "the school is settled. Every write that narrows a school's reach reports that way "
             "now, the configuration console's two entitlement routes included, so no operator "
             "is told only that the grant was saved. That refusal used to abort the whole "
             "billing write, so a school could stay on Premium in the product while its invoice "
             "said Standard. Nothing retries it: the refusal is deterministic, nothing here "
             "runs on a schedule, and the next settlement of that school's reach tries the role "
             "again."),
        ],
    },
    6: {
        "blurb_edits": [(
            "takes the role grants beyond the new depth with it, through vs_rbac,",
            "takes the role grants beyond the new depth with it and names any role it could not "
            "settle, through vs_rbac,",
        )],
        "box_edits": [
            ("replace", "• Module 6 remains Backend Complete",
             "• Module 6 remains Backend Complete and In use Complete with twenty-six "
             "capability entries, reconciled to M06 FRD v1.3."),
            ("replace", "• A write that narrows what a school reaches settles",
             "• A write that narrows what a school reaches settles that school's role grants "
             "and says what it could not settle. Entitlements decide what the product offers "
             "and role grants are what a customer handed its own people, and the two were kept "
             "in step only by the plan change, so an uplift withdrawn here, an uplift that "
             "lapsed, and a module shallowed from the console all left roles holding keys the "
             "school could no longer reach. Every write that settles now names the roles it "
             "could not save in its own response, clearing a school's own entitlement layer "
             "settles like the rest, and the one limit left is that the entitlement POST "
             "carries no depth field, so it cannot shallow a module at all."),
        ],
    },
    7: {
        "blurb_edits": [(
            "whether it has been acted on.",
            "whether it has been acted on, which every document type must answer before it may "
            "register.",
        )],
        "box_edits": [
            ("replace", "• Reversal is answered by the module that owns the document",
             "• Reversal is answered by the module that owns the document, and a document type "
             "that has not said what its approval releases does not register: the refusal fires "
             "while the apps load, and the base handler refuses by default behind it. All "
             "fourteen registered types answer, and leave refuses once an absence has begun. "
             "What is taken on trust is a type's own word that its approval releases nothing."),
        ],
    },
    12: {
        "blurb_edits": [
            ("225 staff tests.", "232 staff tests."),
            ("Documented by M12 FRD v2.4", "Documented by M12 FRD v2.5"),
            ("under an approver group the school composes.",
             "under an approver group the school composes, and an approval can no longer be "
             "undone once the absence has begun."),
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


def keep_module_boxes_with_their_modules(doc, modules: tuple[int, ...]) -> None:
    """Start selected modules on a fresh page so their closing box stays attached.

    A capability grid ends with the module's Needs Attention or Current Decision
    row. When a short module starts in the remaining space beneath its predecessor,
    Word can move only that closing row to the next page. Moving the module heading
    instead keeps the qualification beside the capabilities it limits.
    """
    for module in modules:
        prefix = f"Module {module}:"
        matches = [p for p in doc.paragraphs if p.text.strip().startswith(prefix)]
        if len(matches) != 1:
            raise ValueError(f"{prefix!r} starts {len(matches)} module headings")
        matches[0].paragraph_format.page_break_before = True


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
    keep_module_boxes_with_their_modules(doc, MODULE_PAGE_BREAKS)

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

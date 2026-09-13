#!/usr/bin/env python3
"""Record the notification identity, route, and read-state contracts.

The notification engine gives every in-app message a meaningful subject,
derives both links and read acknowledgement from one destination table, and
lets successful record reads clear only the matching notices owned by the
caller. Export notices use the saved definition or dataset name instead of a
generic result label. The relevant module FRDs advance independently and the
MRD records the cross-module contract without adding a capability.

    python tools/patch_notification_read_contract_docs.py
"""

from __future__ import annotations

import argparse
import copy
from pathlib import Path

from docx import Document
from docx.table import _Row

from generate_requirements_documents import (
    assert_no_em_dash,
    shrink_inherited_media,
    update_extended_title,
)
from patch_mrd_v2_79_docs import (
    CAPABILITIES,
    box_lines,
    edit_lines,
    keep_module_boxes_with_their_modules,
    keep_rows_whole,
    module_blurbs,
    rebuild_table,
    replace_cell,
    replace_cover_version,
    retitle,
    set_box,
)


REVIEW_DATE = "13 September 2026"
SHORT_DATE = "13 Sep 2026"
MRD_VERSION = "2.80"
CODE_BASELINE = "Backend commit 73c46350, 13 September 2026"
DEPLOYMENT = (
    "Backend and committed frontend evidence only; nothing here claims deployment "
    "or production adoption."
)


def set_run_text(paragraph, text: str) -> None:
    """Rewrite a paragraph while retaining the formatting of its first run."""
    if not paragraph.runs:
        paragraph.add_run(text)
        return
    paragraph.runs[0].text = text
    for run in paragraph.runs[1:]:
        run.text = ""


def set_cell_text(cell, text: str) -> None:
    """Rewrite a one-paragraph cell without replacing its paragraph style."""
    for paragraph in cell.paragraphs[1:]:
        paragraph._p.getparent().remove(paragraph._p)
    set_run_text(cell.paragraphs[0], text)


def append_cell_text(cell, tail: str) -> None:
    set_cell_text(cell, cell.text.strip() + tail)


def unique_cells(row) -> list:
    seen, cells = set(), []
    for cell in row.cells:
        if cell._tc in seen:
            continue
        seen.add(cell._tc)
        cells.append(cell)
    return cells


def find_table(doc, header: str, *, contains: str | None = None):
    for table in doc.tables:
        if not table.rows[0].cells[0].text.strip().startswith(header):
            continue
        if contains is None or any(
            row.cells[0].text.strip().startswith(contains) for row in table.rows
        ):
            return table
    raise ValueError(f"Table not found: {header!r} containing {contains!r}")


def find_row(table, prefix: str, *, column: int = 0):
    rows = [
        row for row in table.rows
        if row.cells[column].text.strip().startswith(prefix)
    ]
    if len(rows) != 1:
        raise ValueError(f"Expected one row starting {prefix!r}, found {len(rows)}")
    return rows[0]


def find_row_exact(table, text: str, *, column: int = 0):
    rows = [row for row in table.rows if row.cells[column].text.strip() == text]
    if len(rows) != 1:
        raise ValueError(f"Expected one row equal to {text!r}, found {len(rows)}")
    return rows[0]


def field(table, label: str):
    return find_row(table, label).cells[1]


def clone_row(table, anchor, values: list[str], *, before: bool = False):
    new_tr = copy.deepcopy(anchor._tr)
    if before:
        anchor._tr.addprevious(new_tr)
    else:
        anchor._tr.addnext(new_tr)
    row = _Row(new_tr, table)
    cells = unique_cells(row)
    if len(cells) != len(values):
        raise ValueError(f"Row has {len(cells)} cells, {len(values)} values given")
    for cell, value in zip(cells, values):
        set_cell_text(cell, value)
    return row


def find_change_log(doc):
    for table in doc.tables:
        cells = [cell.text.strip() for cell in unique_cells(table.rows[0])]
        if len(cells) == 3 and cells[0] == "Version" and cells[1].startswith("Date"):
            return table
    raise ValueError("Change log not found")


def prepend_change_log(doc, version: str, summary: str) -> None:
    table = find_change_log(doc)
    clone_row(table, table.rows[1], [version, SHORT_DATE, summary], before=True)


def replace_box_line(cell, prefix: str, replacement: str) -> None:
    """Replace one line in a line-break callout without flattening its layout."""
    paragraph = cell.paragraphs[0]
    lines = paragraph.text.split("\n")
    hits = [index for index, line in enumerate(lines) if line.startswith(prefix)]
    if len(hits) != 1:
        raise ValueError(f"Expected one callout line starting {prefix!r}, found {len(hits)}")
    lines[hits[0]] = replacement
    set_run_text(paragraph, "\n".join(lines))


def append_box_line(cell, line: str) -> None:
    paragraph = cell.paragraphs[0]
    set_run_text(paragraph, paragraph.text.rstrip() + "\n" + line)


def replace_body_fragment(doc, old: str, new: str) -> None:
    matches = [paragraph for paragraph in doc.paragraphs if old in paragraph.text]
    if len(matches) != 1:
        raise ValueError(f"Expected one body paragraph containing {old!r}, found {len(matches)}")
    set_run_text(matches[0], matches[0].text.replace(old, new))


def replace_cell_fragment(cell, old: str, new: str) -> None:
    matches = [paragraph for paragraph in cell.paragraphs if old in paragraph.text]
    if len(matches) != 1:
        raise ValueError(f"Expected one cell paragraph containing {old!r}, found {len(matches)}")
    set_run_text(matches[0], matches[0].text.replace(old, new))


def update_control(doc, version: str, module: int) -> None:
    control = find_table(doc, "Document", contains="Code baseline")
    set_cell_text(field(control, "Version"), version)
    set_cell_text(field(control, "Review date"), REVIEW_DATE)
    set_cell_text(field(control, "Code baseline"), CODE_BASELINE)
    set_cell_text(
        field(control, "Source MRD"),
        f"XVS Module Requirements Document v{MRD_VERSION} | Module {module}",
    )


def hold_rows_whole(doc) -> None:
    """Keep table rows intact so a requirement does not split across pages."""
    from docx.oxml import OxmlElement
    from docx.oxml.ns import qn

    for table in doc.tables:
        for row in table.rows:
            properties = row._tr.get_or_add_trPr()
            if properties.find(qn("w:cantSplit")) is None:
                properties.append(OxmlElement("w:cantSplit"))


def finish(doc, output: Path, title: str, version: str) -> None:
    hold_rows_whole(doc)
    doc.core_properties.title = title
    doc.core_properties.version = version
    output.parent.mkdir(parents=True, exist_ok=True)
    doc.save(str(output))
    update_extended_title(output, title)
    shrink_inherited_media(output)
    assert_no_em_dash(output)


def patch_m07(source: Path, output: Path) -> None:
    doc = Document(str(source))
    fr = find_table(doc, "FR-024")
    api = find_table(doc, "Method and path", contains="GET /instances/")
    dependencies = find_table(doc, "Dependency")

    replace_cover_version(doc.tables[0], "1.12", "1.13")
    update_control(doc, "1.13", 7)
    replace_body_fragment(doc, "MRD v2.79", "MRD v2.80")
    append_cell_text(
        field(fr, "Current evidence"),
        " A successful instance detail read also acknowledges the caller's unread "
        "in-app notices for that instance, including both approval and submission "
        "event families.",
    )
    append_cell_text(
        field(fr, "Acceptance"),
        " Reading an instance clears only matching rows owned by the caller; another "
        "recipient's notices and notices for another instance remain unread.",
    )
    append_cell_text(
        find_row(api, "GET /instances/{id}/").cells[1],
        " A successful scoped read acknowledges the caller's matching instance notices.",
    )
    append_cell_text(
        find_row(dependencies, "Module 8").cells[1],
        " It also owns the shared destination family used when an instance read "
        "acknowledges the caller's matching notices.",
    )
    prepend_change_log(
        doc,
        "1.13",
        "Records the server-side read acknowledgement for a workflow instance. A "
        "successful scoped detail read clears the caller's unread in-app approval "
        "and submission notices for that instance, while other recipients and other "
        "instances remain untouched. FR-024, the instance route and the Module 8 "
        f"dependency are reconciled. No capability is added. {DEPLOYMENT}",
    )
    finish(
        doc,
        output,
        "XVS M07 Workflow and Approval Engine Functional Requirements Document v1.13",
        "1.13",
    )


def patch_m08(source: Path, output: Path) -> None:
    doc = Document(str(source))
    fr001 = find_table(doc, "FR-001")
    fr005 = find_table(doc, "FR-005")
    fr010 = find_table(doc, "FR-010")
    data = find_table(doc, "Model")
    api = find_table(doc, "Method and path", contains="GET /notify/")
    dependencies = find_table(doc, "Dependency")
    gaps = find_table(doc, "FURTHER GAPS")

    replace_cover_version(doc.tables[0], "1.10", "1.11")
    update_control(doc, "1.11", 8)
    replace_body_fragment(doc, "MRD v2.76", "MRD v2.80")
    append_cell_text(
        field(fr001, "Current evidence"),
        " import.completed, import.failed and user.account_locked remain inactive. "
        "The import engine reports runs through task.completed or task.failed, and "
        "the account lock flow emits no notification event.",
    )
    append_cell_text(
        field(fr001, "Acceptance"),
        " Inactive import and account-lock keys are absent from the active catalogue "
        "and are refused if called directly.",
    )
    append_cell_text(
        field(fr005, "Current evidence"),
        " Every in-app template has its own subject. Migration 0018 fills 41 blank "
        "subjects and replaces a body only where it still equals the old shared "
        "default, preserving staff-authored copy. A feed row whose stored subject is "
        "blank falls back to the event label.",
    )
    append_cell_text(
        field(fr005, "Acceptance"),
        " InAppSubjectCopyTests and MigrationCopyTests cover event-specific titles, "
        "the label fallback and preservation of customized subject and body text.",
    )
    set_cell_text(
        field(fr010, "Current evidence"),
        "The feed is ordered unread first, then newest first within each group, and "
        "the unread count drives the badge. One RECORD_DESTINATIONS table defines "
        "both the action URL and the filters used to acknowledge a record route, so "
        "the link and the clear rule cannot drift. It covers ticket, workflow, export "
        "run and import batch records. Health incidents and ToDo tasks have no "
        "dedicated frontend record route, but their successful API detail read, or a "
        "ToDo read or toggle, clears the matching notice. Successful export run detail "
        "and authorized file download, ticket detail, workflow instance detail and "
        "import batch detail do the same. Every clear is one scoped update over the "
        "recipient's own unread in-app rows. Module index routes are destinations only "
        "and acknowledge nothing: /finance, /procurement, /team-management, "
        "/me/security, /export/files, /data-imports/batches and /tasks. The finance "
        "destination matches the \"billing.\" and \"payments.\" prefixes; the old "
        "\"finance.\" prefix matched no registered event. POST "
        "/v1/notify/acknowledge-route/ returns both "
        "updated_count and the caller's live unread_count, which lets the badge use the "
        "server's answer after a record read has already cleared rows.",
    )
    set_cell_text(
        field(fr010, "Acceptance"),
        "Route-table tests prove action URLs and acknowledgement filters stay paired, "
        "module indexes clear nothing, foreign recipients and unrelated records stay "
        "unread, and the response carries the live unread total. Record-read tests "
        "cover export, ticket, workflow, import, health and ToDo. A refused or hidden "
        "record access clears nothing, including a rejected export download and a "
        "ticket answered as 404.",
    )
    set_cell_text(
        field(fr010, "Current limit"),
        "A notice with no record destination, including payments unbooked-receipt "
        "digests and onboarding notices, is cleared manually from the inbox. Health "
        "incidents and ToDo tasks "
        "have no dedicated frontend detail page, so their automatic clear is available "
        "only through their API read or action. Visiting their module index clears "
        "nothing.",
    )
    append_cell_text(
        find_row(data, "NotificationTemplate").cells[2],
        " The subject is the in-app title as well as the email subject; an in-app row "
        "with no stored subject falls back to its event label.",
    )
    append_cell_text(
        find_row_exact(data, "Notification").cells[2],
        " Its read state can be changed directly from the inbox or by an authorized "
        "read of the record the notice names.",
    )
    set_cell_text(
        find_row(api, "POST /notify/acknowledge-route/").cells[1],
        "Acknowledge the caller's unread in-app rows covered by a visited record route. "
        "Module indexes acknowledge nothing. Returns {updated_count, unread_count}.",
    )
    anchor = find_row(dependencies, "Module 31")
    clone_row(
        dependencies,
        anchor,
        [
            "Modules 7, 10, 26 and 29",
            "Supply workflow instances, import batches, export runs and ToDo tasks. "
            "Their successful record reads call the shared acknowledgement service; "
            "ToDo completion-review notices carry todo_task_id metadata.",
        ],
        before=True,
    )
    append_cell_text(
        find_row(dependencies, "Module 30").cells[1],
        " An authorized incident detail read acknowledges the operator's matching "
        "in-app alert notice, even though the notification has no frontend action URL.",
    )
    append_cell_text(
        find_row(dependencies, "Module 31").cells[1],
        " An authorized ticket detail read acknowledges the caller's matching ticket "
        "notices.",
    )
    append_box_line(
        gaps.rows[0].cells[0],
        "• The registry holds seven billing events, but only six have in-app templates: "
        "billing.statement_issued is email-only. Five active in-app-capable customer "
        "events are sent only to unregistered addresses and dispatch skips in-app for "
        "them; billing.refund_processed is inactive and has no emitter. The six in-app "
        "templates remain for a future registered-customer portal.",
    )
    append_box_line(
        gaps.rows[0].cells[0],
        "• import.completed and import.failed remain inactive because import runs use "
        "the generic task events. user.account_locked remains inactive and un-emitted.",
    )
    append_box_line(
        gaps.rows[0].cells[0],
        "• Recordless notices still need a manual inbox action. Health incidents and "
        "ToDo tasks clear only through API reads or actions because neither has a "
        "dedicated frontend detail page.",
    )
    prepend_change_log(
        doc,
        "1.11",
        "Records event-specific in-app subjects, one destination table shared by "
        "links and acknowledgement, server-side clearing on successful record reads, "
        "the {updated_count, unread_count} route response, ToDo record metadata and "
        "export result naming. It also states the remaining recordless, health, ToDo, "
        "import, account-lock and billing delivery limits precisely. Console ff84fc4 "
        "and school-app 448cac2 consume the contract, but their commits are not proof "
        f"of deployment. Module 8 stays at 22 capability entries. {DEPLOYMENT}",
    )
    finish(
        doc,
        output,
        "XVS M08 Notifications and Delivery Functional Requirements Document v1.11",
        "1.11",
    )


def patch_m10(source: Path, output: Path) -> None:
    doc = Document(str(source))
    fr = find_table(doc, "FR-014")
    api = find_table(doc, "Route", contains="GET, POST /batches/")
    dependencies = find_table(doc, "Module", contains="Module 8")
    gaps = find_table(doc, "NEEDS ATTENTION")

    replace_cover_version(doc.tables[0], "1.3", "1.4")
    update_control(doc, "1.4", 10)
    replace_body_fragment(
        doc,
        "Module Requirements Document v2.76",
        "Module Requirements Document v2.80",
    )
    append_cell_text(
        field(fr, "Current evidence"),
        " A successful scoped batch detail read acknowledges the caller's matching "
        "generic task notice. import.completed and import.failed remain inactive and "
        "are not the events this path uses.",
    )
    append_cell_text(
        field(fr, "Acceptance"),
        " Reading a batch clears only the caller's task notice for that batch; another "
        "batch and another recipient remain unread.",
    )
    append_cell_text(
        find_row(api, "GET, PATCH, DELETE /batches/{id}/").cells[1],
        " A successful GET acknowledges the caller's matching Module 8 task notice.",
    )
    append_cell_text(
        find_row(dependencies, "Module 8").cells[1],
        " The successful batch detail read clears that caller-owned notice through "
        "Module 8's shared acknowledgement service.",
    )
    cell = gaps.rows[0].cells[0]
    set_box(
        cell,
        edit_lines(
            box_lines(cell),
            [(
                "replace",
                "• The module's own import notifications",
                "• The module's own import notifications are not dispatched through "
                "Module 8, and their feed requires a platform key, so a school "
                "administrator never sees the validation or rollback notice. The import "
                "run itself reaches the uploader's bell as task.completed or task.failed, "
                "links to the batch, and clears when that batch is read. import.completed "
                "and import.failed remain inactive. A rollback run in the background still "
                "reaches nobody's bell.",
            )],
        ),
    )
    prepend_change_log(
        doc,
        "1.4",
        "Records that an authorized batch detail read clears the caller's matching "
        "generic task notice, and distinguishes that live path from the inactive "
        "import.completed and import.failed events and the module's inaccessible own "
        "rows. FR-014, the batch route, dependency and current gap are reconciled. "
        f"No capability is added. {DEPLOYMENT}",
    )
    finish(
        doc,
        output,
        "XVS M10 Bulk Data Import Functional Requirements Document v1.4",
        "1.4",
    )


def patch_m26(source: Path, output: Path) -> None:
    doc = Document(str(source))
    fr = find_table(doc, "FR-006")
    api = find_table(doc, "Method and path", contains="GET /exports/runs/")
    dependencies = find_table(doc, "Dependency")

    replace_cover_version(doc.tables[0], "1.0", "1.1")
    update_control(doc, "1.1", 26)
    replace_body_fragment(
        doc,
        "Module Requirements Document v2.72",
        "Module Requirements Document v2.80",
    )
    append_cell_text(
        field(fr, "Current evidence"),
        " The completion notice names the saved definition when there is one, "
        "otherwise the dataset, and falls back to the run reference. A trailing "
        "'export' or 'exports' is removed because the template supplies that word. "
        "This display choice does not rewrite the run's frozen name.",
    )
    append_cell_text(
        field(fr, "Acceptance"),
        " ExportNotificationNameTests covers saved, quick and fallback naming, "
        "including suffix removal. An authorized run detail read or file download "
        "clears the caller's matching notice; a refused download clears nothing.",
    )
    append_cell_text(
        find_row(api, "GET /exports/runs/ and /{pk}/").cells[1],
        " A successful detail read acknowledges the caller's matching run notice.",
    )
    append_cell_text(
        find_row(api, "GET /exports/files/{pk}/download/").cells[1],
        " A permitted download acknowledges the matching run notice only after "
        "authorization succeeds.",
    )
    clone_row(
        dependencies,
        dependencies.rows[-1],
        [
            "Module 8, Notifications & Delivery",
            "Names completion notices from the saved definition or dataset, links them "
            "to the run, and clears the caller's notice after an authorized run read or "
            "file download.",
        ],
    )
    prepend_change_log(
        doc,
        "1.1",
        "Records human export completion names and authorized record-read "
        "acknowledgement. Saved definitions win, quick runs use their dataset, a "
        "trailing export suffix is removed, and the run reference is the final "
        "fallback. Run detail and permitted download clear the caller's matching "
        "notice; a refused download does not. FR-006, the routes and Module 8 "
        f"dependency are reconciled. No capability is added. {DEPLOYMENT}",
    )
    finish(
        doc,
        output,
        "XVS M26 Reporting and Exports Functional Requirements Document v1.1",
        "1.1",
    )


def patch_m30(source: Path, output: Path) -> None:
    doc = Document(str(source))
    decision = find_table(doc, "CURRENT MODULE DECISION")
    fr = find_table(doc, "FR-008")
    api = find_table(doc, "Endpoint", contains="GET /v1/health/incidents/")
    dependencies = find_table(doc, "Dependency")
    evidence = find_table(doc, "INSPECTED EVIDENCE")

    replace_cover_version(doc.tables[0], "1.0", "1.1")
    update_control(doc, "1.1", 30)
    replace_body_fragment(doc, "MRD v2.43", "MRD v2.80")
    replace_cell_fragment(
        decision.rows[0].cells[0],
        "Module 30 remains Backend Complete and Integration Complete in MRD v2.43.",
        "Module 30 remains Backend Complete and In use Complete in MRD v2.80. "
        "This is evidence alignment, not a claim of deployment.",
    )
    append_cell_text(
        field(fr, "Current evidence"),
        " A successful scoped incident detail read acknowledges the operator's "
        "matching unread in-app alert notice.",
    )
    append_cell_text(
        field(fr, "Acceptance"),
        " The incident read clears only the caller's matching notice. Another "
        "operator's row and another incident stay unread.",
    )
    append_cell_text(
        find_row(api, "GET /v1/health/incidents/{id}/").cells[1],
        "; acknowledges the caller's matching in-app alert notice after the scoped read",
    )
    append_cell_text(
        find_row(dependencies, "Module 8").cells[1],
        " An incident has no frontend notification route, but its authorized API "
        "detail read clears the caller's matching row.",
    )
    append_box_line(
        decision.rows[0].cells[0],
        "The notification has no dedicated frontend incident page. Automatic clear "
        "therefore happens only when the incident detail API is read; the Health index "
        "acknowledges nothing.",
    )
    append_box_line(
        evidence.rows[0].cells[0],
        "IncidentReadClearsNotificationTests covers caller ownership, incident matching "
        "and the API-only acknowledgement path. The suite was not rerun for this "
        "documentation-only revision.",
    )
    prepend_change_log(
        doc,
        "1.1",
        "Records that a successful incident detail API read clears only the caller's "
        "matching in-app alert notice. No dedicated frontend incident page exists, and "
        "the Health index clears nothing. FR-008, the read route, Module 8 dependency, "
        f"current decision and evidence boundary are reconciled. {DEPLOYMENT}",
    )
    finish(
        doc,
        output,
        "XVS M30 System Health and Monitoring Functional Requirements Document v1.1",
        "1.1",
    )


def patch_m31(source: Path, output: Path) -> None:
    doc = Document(str(source))
    fr002 = find_table(doc, "FR-002")
    fr010 = find_table(doc, "FR-010")
    api = find_table(doc, "Method and path", contains="GET, POST /support/tickets/")
    dependencies = find_table(doc, "Dependency")

    replace_cover_version(doc.tables[0], "1.7", "1.8")
    update_control(doc, "1.8", 31)
    replace_body_fragment(doc, "MRD v2.77", "MRD v2.80")
    append_cell_text(
        field(fr002, "Current evidence"),
        " After that visibility check succeeds, a detail read acknowledges the "
        "caller's matching unread in-app ticket notices.",
    )
    append_cell_text(
        field(fr002, "Acceptance"),
        " A hidden ticket still answers 404 and clears nothing; another recipient and "
        "another ticket remain unread.",
    )
    append_cell_text(
        field(fr010, "Current evidence"),
        " Every ticket event belongs to one shared destination family, so its link and "
        "its record-read acknowledgement resolve the same ticket identifier.",
    )
    append_cell_text(
        field(fr010, "Acceptance"),
        " TicketReadClearsNotificationTests proves the authorized read clears the "
        "caller's matching row and a hidden 404 clears nothing.",
    )
    append_cell_text(
        find_row(api, "GET, PATCH /support/tickets/{id}/").cells[1],
        " A successful GET acknowledges the caller's matching ticket notices.",
    )
    append_cell_text(
        find_row(dependencies, "Module 8").cells[1],
        " All ticket event keys share the ticket record destination, and a successful "
        "detail read clears only the caller's matching notices.",
    )
    prepend_change_log(
        doc,
        "1.8",
        "Records that every ticket event shares one record destination and that an "
        "authorized ticket detail read clears only the caller's matching unread in-app "
        "notices. A hidden ticket still answers 404 and clears nothing. FR-002, FR-010, "
        f"the route and Module 8 dependency are reconciled. {DEPLOYMENT}",
    )
    finish(
        doc,
        output,
        "XVS M31 Support Tickets Functional Requirements Document v1.8",
        "1.8",
    )


def update_mrd_control(control) -> None:
    for row in control.rows:
        label = row.cells[0].text.strip()
        if label == "Version":
            replace_cell(row.cells[1], "2.80", size=9)
        elif label == "Review date":
            replace_cell(row.cells[1], REVIEW_DATE, size=9)
        elif label == "Source scope":
            replace_cell(
                row.cells[1],
                "Notification identity, record destinations and server-side read "
                "acknowledgement at backend 73c46350; console ff84fc4 and school app "
                "448cac2 as committed consumer evidence, 13 September 2026",
                size=9,
            )


def patch_mrd(source: Path, output: Path) -> None:
    doc = Document(str(source))
    tables = doc.tables
    cover, control, contents = tables[0], tables[1], tables[2]
    delta, log = tables[76], tables[78]
    grids = {module: tables[index] for module, index in CAPABILITIES.items()}
    blurbs = module_blurbs(doc)

    if not delta.rows[0].cells[0].text.strip().startswith("v2.79 capability delta"):
        raise ValueError("MRD v2.79 delta table moved")
    replace_cover_version(cover, "2.79", "2.80")
    update_mrd_control(control)
    for row in contents.rows:
        if row.cells[0].text.strip().startswith("5."):
            replace_cell(row.cells[0], "5. v2.80 Capability Delta", size=9, bold=True)
            replace_cell(
                row.cells[1],
                "Notification identity, record destinations, and the read that closes them",
                size=9,
            )

    for paragraph in doc.paragraphs:
        text = paragraph.text.strip()
        if text == "5. v2.79 Capability Delta":
            retitle(paragraph, "5. v2.80 Capability Delta")
        elif text.startswith("This revision") and len(text) > 80:
            retitle(
                paragraph,
                "This revision records one notification contract across the modules that "
                "own the records a message names. Every in-app template has a useful title; "
                "one destination table drives both links and acknowledgement; and a "
                "successful scoped record read clears only the caller's matching unread "
                "rows. Module indexes clear nothing. Export results use the definition or "
                "dataset people recognize, and the badge receives the live unread total. "
                "No capability is added, so the platform remains at 502 entries.",
            )

    blurb_tails = {
        7: " Reading an instance acknowledges the caller's matching approval and submission notices.",
        8: " Each in-app message has an event-specific title, and one record-destination table drives both its link and its read acknowledgement. Module indexes acknowledge nothing.",
        10: " Reading a batch acknowledges the uploader's matching generic task notice; the module's own import rows remain inaccessible to a school administrator.",
        26: " Completion notices use the saved definition or dataset name, and an authorized run read or download acknowledges the caller's matching notice.",
        29: " Completion-review notices carry the task identifier and clear when the task is read or toggled through the API; no dedicated frontend task page exists.",
        30: " An incident detail API read acknowledges the operator's matching notice; there is no dedicated frontend incident page.",
        31: " An authorized ticket read acknowledges the caller's matching notice after visibility succeeds.",
    }
    for module, tail in blurb_tails.items():
        retitle(blurbs[module], blurbs[module].text.rstrip() + tail)

    box_edits = {
        7: [("append", "• Reading an instance acknowledges only the caller's matching approval and submission notices; another recipient and another instance remain unread. M07 FRD v1.13 records the contract.")],
        8: [
            ("append", "• Event-specific in-app subjects, one shared record-destination table and server-side record-read acknowledgement are reconciled to M08 FRD v1.11. /finance matches the \"billing.\" and \"payments.\" prefixes; every module index route acknowledges nothing."),
            ("append", "• Recordless notices remain manual. Health incidents and ToDo tasks clear only through their API read or action because neither has a dedicated frontend detail page."),
            ("append", "• The seven billing events include one email-only event and six in-app templates. Five active in-app-capable customer events currently reach unregistered addresses only, so their in-app rows are skipped; the sixth is inactive and un-emitted."),
        ],
        10: [("replace", "• Import notifications are the module's own rows", "• ImportNotification rows still do not reach a school administrator. The import run itself reaches the uploader's bell as task.completed or task.failed, links to the batch and clears when that batch is read. import.completed and import.failed remain inactive. M10 FRD v1.4 records the distinction.")],
        26: [
            ("replace", "• Module 26 remains Backend Complete", "• Module 26 remains Backend Complete and In use Complete with sixteen capability entries, reconciled to M26 FRD v1.1."),
            ("append", "• Export completion notices use the saved definition or dataset name, remove a repeated export suffix, and clear after an authorized run read or download. A refused download clears nothing."),
        ],
        29: [("append", "• A completion-review notice carries todo_task_id and clears when that task is read or toggled through the API. The module index acknowledges nothing, and there is no Module 29 FRD or dedicated frontend task detail page.")],
        30: [
            ("replace", "Module 30 remains Backend Complete", "Module 30 remains Backend Complete and In use Complete with 13 capability entries, reconciled to M30 FRD v1.1."),
            ("append", "An authorized incident detail API read clears the operator's matching alert notice. There is no dedicated frontend incident page, and the Health index acknowledges nothing."),
        ],
        31: [
            ("replace", "• Module 31 remains Backend Complete", "• Module 31 remains Backend Complete and In use Complete with seventeen capability entries, reconciled to M31 FRD v1.8."),
            ("append", "• Every ticket event shares one record destination. An authorized ticket read clears only the caller's matching notices; a hidden 404 clears nothing."),
        ],
    }
    for module, edits in box_edits.items():
        cell = grids[module].rows[-1].cells[0]
        set_box(cell, edit_lines(box_lines(cell), edits))

    rebuild_table(
        delta,
        ["v2.80 capability delta", "Decision", "Evidence"],
        [
            ["In-app identity", "Each event has its own title", "Migration 0018 fills 41 blank subjects, preserves staff edits, and feed serialization falls back to the event label for any blank subject."],
            ["Record destinations", "One table drives links and clears", "Ticket, workflow, export and import record families share action and acknowledgement rules. /finance matches the \"billing.\" and \"payments.\" prefixes; module indexes remain destinations only and clear nothing."],
            ["Record reads", "Authorized reads acknowledge", "Export run detail and permitted download, ticket, workflow, import and health detail, and ToDo read or toggle clear only the caller's matching unread in-app rows."],
            ["Export result", "Name what people recognize", "The saved definition wins, a quick run uses its dataset, a repeated export suffix is removed, and the run reference is the fallback."],
            ["Badge contract", "Return the live unread total", "POST /v1/notify/acknowledge-route/ returns {updated_count, unread_count}, so a consumer does not infer the badge after a record read has already cleared rows."],
            ["Current limits", "Manual and API-only paths stay visible", "Recordless notices need a manual inbox action. Health and ToDo have no dedicated frontend detail page. Import and account-lock event keys remain inactive."],
            ["Module FRDs", "Six revised; one remains missing", "M07 v1.13, M08 v1.11, M10 v1.4, M26 v1.1, M30 v1.1 and M31 v1.8. No Module 29 ToDo FRD exists, so none is created."],
        ],
        [1.85, 1.35, 3.85],
    )
    keep_rows_whole(delta)
    change = (
        "Records event-specific in-app subjects, one shared destination table for "
        "links and acknowledgement, successful record reads that clear only the "
        "caller's matching unread rows, human export result names, ToDo task metadata "
        "and the live unread-count response. Current limits stay explicit: recordless "
        "notices need manual action; Health and ToDo have API-only clears; import and "
        "account-lock event keys remain inactive; and the billing registry's seven "
        "events yield six in-app templates whose present customer dispatch path does "
        "not create a registered recipient row. M07, M08, M10, M26, M30 and M31 advance. "
        "No Module 29 FRD exists, and M02, M03 and M17 make no affected claim, so no new "
        "versions are created for them. Console ff84fc4 and school app 448cac2 are "
        "committed consumer evidence, not deployment evidence. The finance package tag "
        "v0.7.3 at f8d76b9 and consumer pins 69bedff and 41c88a8 are dependency evidence "
        "only and do not implement this notification contract. No capability is added; "
        "the platform remains at 502 entries."
    )
    template = log.rows[1]
    template._tr.addprevious(copy.deepcopy(template._tr))
    row = log.rows[1]
    replace_cell(row.cells[0], "2.80", size=8)
    replace_cell(row.cells[1], SHORT_DATE, size=8)
    replace_cell(row.cells[2], change, size=8)
    keep_module_boxes_with_their_modules(doc, (15, 24))

    title = "XVS Module Requirements Document v2.80"
    doc.core_properties.title = title
    doc.core_properties.version = "2.80"
    output.parent.mkdir(parents=True, exist_ok=True)
    doc.save(str(output))
    update_extended_title(output, title)
    shrink_inherited_media(output)
    assert_no_em_dash(output)


DOCUMENTS = [
    (
        "module-requirements/XVS_Module_Requirements_Document_v2.79.docx",
        "module-requirements/XVS_Module_Requirements_Document_v2.80.docx",
        patch_mrd,
    ),
    (
        "functional-requirements/07-workflow-and-approval-engine/XVS_M07_Workflow_and_Approval_Engine_Functional_Requirements_Document_v1.12.docx",
        "functional-requirements/07-workflow-and-approval-engine/XVS_M07_Workflow_and_Approval_Engine_Functional_Requirements_Document_v1.13.docx",
        patch_m07,
    ),
    (
        "functional-requirements/08-notifications-and-delivery/XVS_M08_Notifications_and_Delivery_Functional_Requirements_Document_v1.10.docx",
        "functional-requirements/08-notifications-and-delivery/XVS_M08_Notifications_and_Delivery_Functional_Requirements_Document_v1.11.docx",
        patch_m08,
    ),
    (
        "functional-requirements/10-bulk-data-import/XVS_M10_Bulk_Data_Import_Functional_Requirements_Document_v1.3.docx",
        "functional-requirements/10-bulk-data-import/XVS_M10_Bulk_Data_Import_Functional_Requirements_Document_v1.4.docx",
        patch_m10,
    ),
    (
        "functional-requirements/26-reporting-and-exports/XVS_M26_Reporting_and_Exports_Functional_Requirements_Document_v1.0.docx",
        "functional-requirements/26-reporting-and-exports/XVS_M26_Reporting_and_Exports_Functional_Requirements_Document_v1.1.docx",
        patch_m26,
    ),
    (
        "functional-requirements/30-system-health-and-monitoring/XVS_M30_System_Health_and_Monitoring_Functional_Requirements_Document_v1.0.docx",
        "functional-requirements/30-system-health-and-monitoring/XVS_M30_System_Health_and_Monitoring_Functional_Requirements_Document_v1.1.docx",
        patch_m30,
    ),
    (
        "functional-requirements/31-support-tickets/XVS_M31_Support_Tickets_Functional_Requirements_Document_v1.7.docx",
        "functional-requirements/31-support-tickets/XVS_M31_Support_Tickets_Functional_Requirements_Document_v1.8.docx",
        patch_m31,
    ),
]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", default=Path(__file__).resolve().parents[1])
    parser.add_argument("--check", action="store_true")
    parser.add_argument(
        "--resume",
        action="store_true",
        help="Skip targets already written by an interrupted generation pass.",
    )
    args = parser.parse_args()
    root = Path(args.root)
    pairs = [(root / source, root / target, patcher) for source, target, patcher in DOCUMENTS]
    for source, target, _ in pairs:
        if not source.exists():
            raise SystemExit(f"Missing source: {source}")
        if target.exists() and not args.resume:
            raise SystemExit(f"Refusing to overwrite: {target}")
    if args.check:
        print(f"layout inputs OK: {len(pairs)} documents")
        return
    for source, target, patcher in pairs:
        if target.exists():
            print(f"Kept {target.relative_to(root)}")
            continue
        patcher(source, target)
        print(f"Wrote {target.relative_to(root)}")


if __name__ == "__main__":
    main()

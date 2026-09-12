#!/usr/bin/env python3
"""Version the Module 5, 17, 18, 19 and 20 FRDs against the backend at 13675db.

Each of these documents had been advanced by narrow patches that recorded one
change at a time, so code that landed between them was never reconciled. This
script records what the code does now.

Module 5 learns that every audit read is confined to its reader's tenant through
one scoping rule, that the trail rollup is gone and its counts are computed from
the events a reader may see, that an actor is normalised before it is read, and
that the vocabulary and the writers have grown.

Module 17 records the optional, derived fee structure code, the due date
generation never leaves empty, the finance abstraction layer's routes, the
school fee due policy, the branch rule on the invoice and receipt email routes,
and the tenant application address the pay link is built on.

Module 18 records the reversal contract over payout approval, corrects what a
confirmation against a stageless ladder can do, and stops describing approver
roles that provisioning no longer creates.

Module 19 records bank accounts narrowed by branch when reached by id, statement
imports filed under their account's branch, the reversal contract for journals
and expense claims, the optional employee link on a salary row, entity creation
as a platform act, and a gap in the direct-post routes.

Module 20 records the stageless confirmation on every adjustment post route, the
reversal contract, the refund method as a Dynamic Role field, and the branch rule
it had never stated.

    python tools/patch_finance_audit_reconciliation_docs.py [--only M17]
"""

from __future__ import annotations

import argparse
import copy
import re
from pathlib import Path

from docx import Document
from docx.table import Table
from docx.text.paragraph import Paragraph

from generate_requirements_documents import (
    assert_no_em_dash,
    shrink_inherited_media,
    update_extended_title,
)

REVIEW_DATE = "11 September 2026"
SHORT_DATE = "11 Sep 2026"
MRD_VERSION = "2.76"
CODE_BASELINE = "Backend worktree at 13675db, 11 September 2026"


# ── shared docx helpers ──────────────────────────────────────────────────────

def set_run_text(paragraph, text: str) -> None:
    """Rewrite a paragraph in place, keeping the formatting of its first run."""
    if not paragraph.runs:
        raise ValueError("Paragraph carries no run to inherit formatting from")
    paragraph.runs[0].text = text
    for run in paragraph.runs[1:]:
        run.text = ""


def set_cell_text(cell, text: str) -> None:
    """Rewrite a cell as one paragraph, keeping its first run's formatting.

    Rewriting through the first run rather than rebuilding the paragraph keeps
    the cell's font, size, colour, margins and borders exactly as the document
    drew them, which a rebuilt cell does not.
    """
    for paragraph in cell.paragraphs[1:]:
        paragraph._p.getparent().remove(paragraph._p)
    set_run_text(cell.paragraphs[0], text)


def replace_in_cell(cell, old: str, new: str) -> None:
    if len(cell.paragraphs) != 1:
        raise ValueError("replace_in_cell expects a single-paragraph cell")
    text = cell.text
    if old not in text:
        raise ValueError(f"Text not found in cell: {old[:60]}")
    set_cell_text(cell, text.replace(old, new))


def append_to_cell(cell, tail: str) -> None:
    set_cell_text(cell, cell.text.strip() + tail)


def replace_cover_version(table, target: str) -> None:
    """Set the cover's version label to ``target``, whatever it currently reads.

    A cover can carry a stale label while Document Control is right, so the
    label is found by its own word rather than by the version being left.
    """
    cell = table.rows[0].cells[0]
    for paragraph in cell.paragraphs:
        for run in paragraph.runs:
            if "Version:" in run.text:
                run.text = re.sub(r"Version:\s*[\d.]+", f"Version: {target}", run.text)
                if f"Version: {target}" not in cell.text:
                    raise ValueError("Cover version label did not take the new value")
                return
    raise ValueError(f"No cover version label found (expected to set {target})")


def replace_control_value(table, label: str, value: str) -> None:
    for row in table.rows:
        if row.cells[0].text.strip() == label:
            set_cell_text(row.cells[1], value)
            return
    raise ValueError(f"Control row not found: {label}")


def update_control(doc, version: str, module: int) -> None:
    control = doc.tables[1]
    replace_control_value(control, "Version", version)
    replace_control_value(control, "Review date", REVIEW_DATE)
    replace_control_value(control, "Code baseline", CODE_BASELINE)
    replace_control_value(
        control, "Source MRD",
        f"XVS Module Requirements Document v{MRD_VERSION} | Module {module}",
    )


def replace_body_paragraph(doc, prefix: str, text: str) -> None:
    for paragraph in doc.paragraphs:
        if paragraph.text.strip().startswith(prefix):
            set_run_text(paragraph, text)
            return
    raise ValueError(f"Body paragraph not found: {prefix}")


def replace_in_paragraph(doc, prefix: str, old: str, new: str) -> None:
    for paragraph in doc.paragraphs:
        if paragraph.text.strip().startswith(prefix):
            if old not in paragraph.text:
                raise ValueError(f"Text not found in paragraph: {old[:60]}")
            set_run_text(paragraph, paragraph.text.replace(old, new))
            return
    raise ValueError(f"Body paragraph not found: {prefix}")


def find_row(table, prefix: str, column: int = 0):
    for row in table.rows:
        if row.cells[column].text.strip().startswith(prefix):
            return row
    raise ValueError(f"Row not found: {prefix}")


def insert_row_before(table, prefix: str, values: list[str]):
    """Clone the row named by ``prefix`` and write ``values`` into the copy."""
    anchor = find_row(table, prefix)
    anchor._tr.addprevious(copy.deepcopy(anchor._tr))
    # The clone carries the anchor's text, so it is now the first match.
    clone = find_row(table, prefix)
    for cell, value in zip(clone.cells, values):
        set_cell_text(cell, value)
    return clone


def append_row(table, values: list[str]):
    template = table.rows[-1]
    template._tr.addnext(copy.deepcopy(template._tr))
    row = table.rows[-1]
    for cell, value in zip(row.cells, values):
        set_cell_text(cell, value)
    return row


def prepend_change_log(table, version: str, summary: str) -> None:
    template = table.rows[1]
    template._tr.addprevious(copy.deepcopy(template._tr))
    row = table.rows[1]
    set_cell_text(row.cells[0], version)
    set_cell_text(row.cells[1], SHORT_DATE)
    set_cell_text(row.cells[2], summary)


def require_fr(table, label: str):
    if not table.rows[0].cells[0].text.strip().startswith(label):
        raise ValueError(f"Table is not {label}")
    return table


def fr_cell(table, label: str):
    """The value cell of one labelled row of a requirement table."""
    for row in table.rows[1:]:
        if row.cells[0].text.strip() == label:
            return row.cells[1]
    raise ValueError(f"Requirement row not found: {label}")


def append_bullet(table, text: str) -> None:
    """Add a bullet to a callout by cloning its last paragraph."""
    cell = table.rows[0].cells[0]
    last = cell.paragraphs[-1]
    clone = copy.deepcopy(last._p)
    last._p.addnext(clone)
    set_run_text(Paragraph(clone, cell), text)


def rewrite_callout(table, lines: list[str]) -> None:
    """Rewrite a callout positionally: paragraph i takes line i.

    The first paragraph is the bold coloured heading and the rest are bullets,
    so an added line clones the last bullet rather than the heading.
    """
    cell = table.rows[0].cells[0]
    for index, line in enumerate(lines):
        paragraphs = cell.paragraphs
        if index < len(paragraphs):
            set_run_text(paragraphs[index], line)
        else:
            last = paragraphs[-1]
            clone = copy.deepcopy(last._p)
            last._p.addnext(clone)
            set_run_text(Paragraph(clone, cell), line)
    for paragraph in cell.paragraphs[len(lines):]:
        paragraph._p.getparent().remove(paragraph._p)


def rebuild_callout_from(target, template, lines: list[str]) -> None:
    """Replace a callout's cell with a copy of a sibling callout's, then fill it.

    Used where an earlier revision flattened a callout into one plain paragraph
    and repainted its border: the sibling still carries the heading, bullet and
    border formatting the box was drawn with.
    """
    old_tc = target.rows[0].cells[0]._tc
    new_tc = copy.deepcopy(template.rows[0].cells[0]._tc)
    old_tc.getparent().replace(old_tc, new_tc)
    rewrite_callout(target, lines)


def add_fr_after(doc, after_table, fr: dict, *, heading: str):
    """Clone the requirement block ending at ``after_table`` and fill the copy.

    The heading and the table are deep copies of the block they follow, so the
    new requirement inherits its status styling, borders and widths. Clone only
    from a block already in the state the new one should show.
    """
    body = doc.element.body
    children = list(body.iterchildren())
    anchor = children.index(after_table._tbl)
    heading_element = None
    for index in range(anchor - 1, max(anchor - 4, -1), -1):
        candidate = children[index]
        if candidate.tag.endswith("}p") and Paragraph(candidate, doc).style.name.startswith("Heading"):
            heading_element = candidate
            break
    if heading_element is None:
        raise ValueError("No heading found above the requirement table")

    new_tbl = copy.deepcopy(after_table._tbl)
    after_table._tbl.addnext(new_tbl)
    new_heading = copy.deepcopy(heading_element)
    after_table._tbl.addnext(new_heading)
    set_run_text(Paragraph(new_heading, doc), heading)

    table = Table(new_tbl, after_table._parent)
    set_run_text(table.rows[0].cells[0].paragraphs[0], fr["header"])
    labels = {
        "Requirement": fr["requirement"],
        "Current evidence": fr["evidence"],
        "Acceptance": fr["acceptance"],
        "Current limit": fr["limit"],
    }
    written = set()
    for row in table.rows[1:]:
        label = row.cells[0].text.strip()
        if label in labels:
            set_cell_text(row.cells[1], labels[label])
            written.add(label)
    if written != set(labels):
        raise ValueError(f"Requirement rows missing: {set(labels) - written}")
    return table


def assert_clean(doc) -> None:
    text = "\n".join(p.text for p in doc.paragraphs)
    for table in doc.tables:
        text += "\n" + "\n".join(cell.text for row in table.rows for cell in row.cells)
    # Spelled in parts: the repository vocabulary test reads every file, and a
    # guard that wrote the retired word would be an offender itself.
    retired_site_word = "cam" + "pus"
    if re.search(retired_site_word, text, re.IGNORECASE):
        raise ValueError(f"The retired word {retired_site_word!r} is in the document")
    if "—" in text:
        raise ValueError("An em dash appears in the document")


def keep_requirement_headers_with_body(doc) -> None:
    """Keep each requirement's status bar on the same page as its first row.

    The heading above a requirement already keeps with the table, but the status
    bar did not keep with the row beneath it, so a block could end a page as a
    heading and a coloured bar with its whole body on the next page.
    """
    for table in doc.tables:
        if re.match(r"^FR-\d{3}\s*\|", table.rows[0].cells[0].text.strip()):
            for cell in table.rows[0].cells:
                for paragraph in cell.paragraphs:
                    paragraph.paragraph_format.keep_with_next = True


def finish(doc, output: Path, title: str, version: str) -> None:
    keep_requirement_headers_with_body(doc)
    assert_clean(doc)
    doc.core_properties.title = title
    doc.core_properties.version = version
    output.parent.mkdir(parents=True, exist_ok=True)
    doc.save(str(output))
    update_extended_title(output, title)
    shrink_inherited_media(output)
    assert_no_em_dash(output)


# ═════════════════════════════════════════════════════════════════════════════
# Module 5 - Audit & Activity Logging
# ═════════════════════════════════════════════════════════════════════════════

M05_DIR = "05-audit-and-activity-logging"
M05_STEM = "XVS_M05_Audit_and_Activity_Logging_Functional_Requirements_Document"
M05_SOURCE, M05_TARGET = "1.2", "1.3"

M05_PURPOSE = (
    "Module 5 is the platform's shared memory of who did what. A module calls one "
    "helper with an actor, an action, a target and a snapshot; this module validates "
    "the vocabulary, attributes the actor, writes an append-only row, records the "
    "object in a catalogue of audited entities, and serves the result to "
    "investigators through the Event Explorer. Its organising commitment is that "
    "recording a change may never endanger the change: the helper writes inside its "
    "own savepoint and never raises, so the worst an audit failure can cost is the "
    "audit row. Its second is that a reader sees only the rows that are theirs: every "
    "read, count and export answers to one scoping rule, and only a caller whose home "
    "tenant is the platform reads across tenants."
)

M05_SCOPE_TRAILS = (
    "A catalogue row per audited object holding its current label. Its event count "
    "and first and last event times are computed at read time from the events the "
    "reader may see."
)

M05_SCOPE_DASHBOARD = (
    "Counts, daily severity, module breakdown, sign-in outcomes, and a critical-event "
    "heatmap, each over the reader's own rows."
)

M05_CONTEXT_OLD = (
    "School and branch changes, identity and authentication events, bulk imports, "
    "onboarding and proxied changes have no second log:"
)
M05_CONTEXT_NEW = (
    "School and branch changes, identity and authentication events, bulk imports, "
    "onboarding, proxied changes, the student roll, staff records, academic "
    "structure and the calendar, and a document posted with no approval steps "
    "configured have no second log of that act:"
)

M05_BOUNDARIES = (
    "Reading the trail is governed by the key and bounded by the row. Every Event "
    "Explorer route, the entity trails and the security dashboard require "
    "platform.audit.view, exporting requires platform.audit.export, and configuring "
    "compliance rules requires platform.audit.manage. The two self-service routes "
    "require no key at all, because their querysets are pinned to the requesting "
    "user: one returns events where the caller is the actor, the other events where "
    "the caller is the target and somebody else acted. The view and export keys are "
    "deliberately tenant-holdable, because an audit officer inside a tenant "
    "legitimately holds them for that tenant, so holding one says nothing about whose "
    "rows the holder may read. One scoping rule in vs_audit.scoping says that, and "
    "every surface reads it. tenant_event_predicate matches the rows that belong to a "
    "tenant: those whose tenant column names it, and the older rows written with no "
    "tenant that recorded the tenant's id in their metadata at the time. "
    "audit_scope_predicate applies it to every caller except one whose home tenant is "
    "the platform, who reads across tenants because that is what the console is for. "
    "The gate is the kind of the caller's home tenant, which no grant and no ?tenant= "
    "assertion can change, so a CodeX staffer proxied as a tenant's account is "
    "confined to that tenant. The Event Explorer, the event detail route, the trail "
    "catalogue and its detail page, the dashboard and the audit export read through "
    "that rule, and the Export Centre dataset reads the tenant predicate directly, so "
    "the screen and the file cannot disagree about which rows are a tenant's. "
    "tenant_slug is a narrowing a platform reader chooses, not the boundary. The "
    "escalation that made this matter is closed at its source: Permission and "
    "PermissionGroup carry a declared scope of PLATFORM or TENANT, a PLATFORM-scoped "
    "key granted inside a non-platform tenant is refused on the override path and on "
    "the role path alike, and platform.audit.manage is platform-scoped, so no tenant "
    "role configures compliance rules. The tenant roster served by filter-options is "
    "gated on the caller's tenant kind for the same reason, so a tenant's audit "
    "officer is never handed CodeX's customer list."
)

M05_ACTOR_PLATFORM_VIEW = (
    "platform.audit.view, held by a caller whose home tenant is the platform. The "
    "whole platform is in range, and tenant_slug narrows it to one customer."
)
M05_ACTOR_PLATFORM_EXPORT = (
    "Queue a CSV export of the filtered stream and read every tenant's export history."
)
M05_ACTOR_TENANT_OFFICER = [
    "A tenant's audit officer",
    "Read, count and export only their own tenant's trail, and read their tenant's "
    "export history.",
    "platform.audit.view and platform.audit.export, which are tenant-holdable. Every "
    "read is confined to the tenant's rows, and another tenant's event or trail "
    "answers 404.",
]
M05_ACTOR_COMPLIANCE_TAIL = " The key is platform-scoped, so no tenant role holds it."

M05_FR002_EVIDENCE = (
    "AuditEvent.save calls full_clean before every insert, so the choice constraints "
    "on module_key, action_type, severity, status and actor_type are enforced in the "
    "application as well as by the column definitions. clean also refuses an event "
    "with no entity_type or entity_id, and refuses a user-attributed event carrying "
    "neither an actor row nor an actor label. AuditActionType registers 96 actions "
    "across generic writes, identity, import, RBAC, proxy sessions, the Export Centre, "
    "school onboarding, academic structure and the timetable, the student roll, staff "
    "records, approval and platform operations, and AuditModuleKey registers 17 "
    "modules. The count recorded at v1.2 was 73: fourteen student events, seven staff "
    "events, ACADEMIC_TIMETABLE_PUBLISHED and POSTED_WITHOUT_APPROVAL have been "
    "registered since, with the STUDENT, STAFF and WORKFLOW module keys."
)
M05_FR002_ACCEPTANCE_TAIL = (
    " StagelessTemplateSubmissionTests in vs_workflow asserts that a confirmed "
    "submission against a ladder with no steps writes its POSTED_WITHOUT_APPROVAL row."
)
M05_FR002_LIMIT_TAIL = (
    " POSTED_WITHOUT_APPROVAL shows the cost: it was emitted before it was "
    "registered, and every confirmation until then left a log line and no row."
)

M05_FR003_ACCEPTANCE_TAIL = (
    " A User, an id given as a string or an integer, an unknown id, an unusable value "
    "and no actor at all each still write the event, which ActorResolutionTests in "
    "tests_actor_resolution.py proves by asserting the row exists."
)
M05_FR003_LIMIT = (
    "actor_user and effective_user are foreign keys to the user model, so an actor "
    "who is not a User row cannot be recorded as one. Service accounts, scheduled "
    "jobs and external systems are representable only as a SYSTEM event carrying "
    "actor_label text, which no filter matches except free-text search. Whatever a "
    "caller passes as the actor is normalised before anything reads it: a User is "
    "kept, an id is resolved to its User, and anything unresolvable is recorded as no "
    "actor rather than losing the event, which is what happened to every branch the "
    "importer created while it passed the actor as a string."
)

M05_FR004_LIMIT = (
    "_SUMMARY_TEMPLATES covers 39 of the 96 registered actions. The rest, including "
    "every Export Centre, onboarding, student and staff action, fall through to the "
    "generic sentence unless the caller supplies its own."
)

M05_FR006_LIMIT = (
    "The rule is a convention for callers, not a constraint this module can enforce: "
    "entity_id is a free text column and any caller may still write a mutable or "
    "tenant-local value into it. Because the trail has no tenant column, a collision "
    "would merge two tenants' objects into one catalogue row whose label is whichever "
    "was written last. Each reader still sees only the events their scope admits, so "
    "the collision would show a reader the other tenant's label but never its events."
)

M05_FR007_EVIDENCE = (
    "The helper reads the trail through get_or_create and overwrites entity_label only "
    "when the incoming label differs, so the steady state, an object whose label has "
    "not moved, writes nothing to the trail table, and a rename costs one update."
)
M05_FR007_ACCEPTANCE = (
    "A renamed object's trail shows the new name on the next event. The trail stores "
    "no counts, so a label refresh cannot disturb them."
)

M05_FR008_LIMIT_OLD = (
    "Rows written before 19 August 2026 keep a NULL tenant and are not backfilled: one "
    "subset is exactly recoverable, because log_auth_event and the RBAC signals "
    "recorded tenant_id in metadata at the time, and recovering it was offered and "
    "declined rather than overlooked."
)
M05_FR008_LIMIT_NEW = (
    "Rows written before 19 August 2026 keep a NULL tenant and are not backfilled. "
    "Where the writer recorded the tenant's id in metadata at the time, which "
    "log_auth_event and the RBAC signals did, the scoping rule matches the row through "
    "that id, so a tenant still reads its own older sign-ins and role changes; "
    "finance, procurement, payments, imports and tickets recorded no id, so their "
    "older rows stay platform-only, which is the safe direction to be wrong in."
)

M05_FR011_EVIDENCE = (
    "The detail route returns the event with its before snapshot, its diff and its "
    "metadata. The entity-trail detail route takes an entity type and id and returns "
    "the catalogue row with every event for that pair the caller may read, newest "
    "first, and takes the count and the first and last event times from those same "
    "rows. The trail list names an object only when the caller can read at least one "
    "of its events, is filterable by entity type and searchable across id and label, "
    "and is ordered by the caller's own most recent readable event, computed in SQL "
    "and tiebroken on type and id so paging cannot repeat or skip a row. Its counters "
    "come from one grouped query per page."
)
M05_FR011_ACCEPTANCE = (
    "A trail with a readable event returns its catalogue row and its events in one "
    "response, and the header agrees with the list beneath it. A trail that does not "
    "exist, or holds no event the caller may read, answers 404 rather than an empty "
    "success, so an enumerable type and id cannot be walked to learn what another "
    "tenant has audited. EntityAuditTrailTenantIsolationTests, EntityTrailCounterTests, "
    "EntityTrailCounterQueryCostTests, RetiredTrailRollupTests and "
    "EntityTrailOrderingTests cover the boundary, the counts, the one-query page, the "
    "counts after a bulk delete and the ordering."
)
M05_FR011_LIMIT = (
    "The trail detail response is not paginated: it serialises every readable event "
    "for the object in one payload, so a heavily audited object returns an unbounded "
    "response. The list's ordering subquery probes the event index once per catalogue "
    "row and grows with the catalogue. The list serialiser resolves user targets "
    "through one bulk query to avoid an N+1, but the trail detail route does not use "
    "that path and so does not resolve them at all."
)

M05_FR013_EVIDENCE_TAIL = (
    " Every series is taken over the rows the caller may read: events through the "
    "shared scoping rule, sign-in sessions and attempts through their tenant-aware "
    "managers, and lockouts and proxy sessions through their tenant, so a tenant's "
    "dashboard counts only its own."
)
M05_FR013_ACCEPTANCE_TAIL = (
    " AuditDashboardTenantIsolationTests proves a tenant's dashboard counts only its "
    "own events."
)
M05_FR013_LIMIT = (
    "The heatmap loads every critical event of the last 30 days that the caller may "
    "read and buckets it in Python so that each timestamp can be converted to local "
    "time, which is a row-by-row loop rather than an aggregate. The dashboard takes no "
    "tenant_slug, unlike the Explorer beside it (FR-008), so a platform reader cannot "
    "narrow it to one customer. The windows are fixed constants in the view."
)

M05_FR015_REPLACEMENTS = [
    (
        "MediaView is deliberately not used: it authorises by knowledge of the storage "
        "name, which core.storage's own docstring calls a capability URL, and its "
        "StoredFile row carries no owner, tenant or expiry, which is right for a school "
        "logo and wrong for a CSV of every actor, target and summary the filters allowed.",
        "MediaView is deliberately not used: it decides by a stored file's tenant and by "
        "the read policy its owning record registers, and an audit CSV has neither, "
        "because a background job writes it with no tenant in context and it answers to "
        "the filters that produced it rather than to any one row, so MediaView would "
        "refuse it outright.",
    ),
    (
        "Export history itself is deliberately not scoped to the requester: the list and "
        "detail routes serve every job, because platform.audit.export is a platform key "
        "and a school role never carries one.",
        "Export history is scoped through the requester: a caller whose home tenant is "
        "the platform reads every job, and anybody else reads the jobs requested by "
        "people in their own tenant, so a second audit officer can see that the first "
        "took the trail out, while the download stays the requester's own. The rows "
        "written into the file pass through the same scoping rule as the Explorer.",
    ),
    (
        "Separately, the app registers an audit.events dataset with the Export Centre "
        "under the same key, tenant-scoped, capped at 500,000 rows, with a required date "
        "range and the actor email marked sensitive.",
        "Separately, the app registers an audit.events dataset with the Export Centre "
        "under the same key, capped at 500,000 rows, with a required date range and the "
        "actor email marked sensitive. It reads the tenant predicate directly and never "
        "takes the platform widening, so a file covers the caller's own organisation and "
        "agrees with the screen about which rows are that organisation's.",
    ),
]
M05_FR015_ACCEPTANCE_TAIL = (
    " AuditExportTenantIsolationTests and ExportCentreDatasetScopeTests prove that a "
    "tenant's export and dataset hold only its rows, and that the dataset and the "
    "Explorer select the same set through the same predicate."
)

M05_STEP7 = (
    "The entity is recorded in the trail catalogue, and its label refreshed if it "
    "changed.",
    "One catalogue row per audited object holds its label; its counts and times are "
    "read off the events when somebody asks.",
)

M05_MODEL_TRAIL = (
    "The catalogue row for one audited object.",
    "Unique on entity type and id, with no tenant column and no counters. Holds the "
    "current label only; the event count and first and last event times are computed "
    "at read time from the events the reader may see, because a stored rollup only "
    "ever grew. Written only by the helper.",
)

M05_API_LEAD = (
    "Routes are mounted under /v1/audit/. Every Explorer, trail, dashboard and export "
    "route reads through one scoping rule: a caller whose home tenant is the platform "
    "reads across tenants, and anybody else reads only their own tenant's rows. Every "
    "route returns the shared success envelope; list routes paginate at 25 rows. An "
    "empty list is returned as an empty list; the envelope coerces only a missing "
    "payload to an empty object."
)

M05_ROUTE_EDITS_71 = [
    ("GET /audit/events/", "The filtered, paginated event list, confined to the "
                            "caller's rows. platform.audit.view."),
    ("GET /audit/events/{id}/", "One event with its before snapshot, diff and "
                                "metadata. Another tenant's event answers 404. "
                                "platform.audit.view."),
    ("GET /audit/dashboard-summary/", "Counters, severity series, module breakdown, "
                                      "sign-in series and critical heatmap, over the "
                                      "caller's rows. platform.audit.view."),
]
M05_ROUTE_EDITS_72 = [
    ("GET /audit/entity-trails/", "The catalogue of audited objects the caller can "
                                  "read an event on, filterable by type and "
                                  "searchable by id or label. platform.audit.view."),
    ("GET /audit/entity-trails/{entity_type}/{entity_id}/",
     "One catalogue row plus every readable event for that object. 404 when no trail "
     "exists or none of its events is readable. platform.audit.view."),
    ("GET, POST /audit/exports/", "Export history for the caller's tenant, or every "
                                  "tenant's for a platform caller; or queue a CSV "
                                  "export of the filtered stream. platform.audit.export."),
]
M05_ERROR_TRAIL = (
    "An entity trail that has never been written, or holds no event the caller may read"
)
M05_ERROR_FOREIGN_EVENT = [
    "Another tenant's event, reached by id",
    "404, exactly as an unknown id. Whether the event exists is not the caller's "
    "business.",
]

M05_WRITERS_LEAD = (
    "Sixteen surfaces write through emit_audit_event. Four of them keep their own "
    "authoritative log and treat this stream as a mirror; for the other twelve this "
    "stream is the only record of most of what they emit. Every one of the sixteen "
    "records the tenant, by one of two routes: a writer that knows the answer passes "
    "it, and the rest inherit the tenant the request asserted. The column below says "
    "which. A row that still lands with no tenant does so because it belongs to no "
    "customer - a sweep, a management command, a seeder, or a platform actor "
    "asserting ?tenant=codex while working on somebody else's school - and those rows "
    "are reachable from the Explorer as tenant_slug=__none__ rather than being lost "
    "behind it."
)
M05_WRITER_M2 = (
    "Proxy session started and ended, against the session row, under PLATFORM or "
    "SCHOOL depending on who initiated it, and TASK_DIAGNOSTIC_VIEWED when a platform "
    "operator reads a failed task's unredacted text, filed against the tenant whose "
    "task failed."
)
M05_WRITER_M7 = [
    "M7 Workflow & Approval Engine (vs_workflow.services.resolution)",
    "A document sent or posted with no approval steps configured, as "
    "POSTED_WITHOUT_APPROVAL under the WORKFLOW module, naming the person who "
    "confirmed and their reason. A direct post has no workflow instance to hang it "
    "on, so for that act this stream is the only record.",
    "Yes, passed by the caller",
]
M05_WRITERS_SCHOOL = [
    [
        "M11 Student Management (schools.vs_students.services)",
        "Enrolment, guardian links and unlinks, promotion runs, documents attached and "
        "removed, and the roll's other creates and edits, under the STUDENT module. No "
        "second log.",
        "Yes",
    ],
    [
        "M12 Staff Management (schools.vs_staff.services.audit)",
        "Profile creation, employment status and posting changes, teaching assigned "
        "and unassigned, leave recorded and decided, and an invitation revoked, under "
        "the STAFF module. Employment changes also enter the staff record's own "
        "append-only history; the rest have no second log.",
        "Yes",
    ],
    [
        "M13 Academic Structure (schools.vs_academics)",
        "Session activation, archiving and narrowing, term archiving, bulk structure "
        "creation, and ordinary creates, edits and deletes, under the ACADEMICS "
        "module. No second log.",
        "Yes",
    ],
    [
        "M14 Timetable & Calendar (schools.vs_calendar.views)",
        "Timetable publication and calendar, examination, room and period changes, "
        "under the ACADEMICS module. No second log.",
        "Yes",
    ],
]
M05_WRITER_M31_TENANT = "Yes, passed from the ticket rather than inherited"

M05_VERIFY_ISOLATION = (
    "•  Cross-tenant isolation on every read. A holder of platform.audit.view inside "
    "one tenant is proved confined to that tenant on the event list, the event detail "
    "route, the trail catalogue and its detail page, the dashboard, the audit export "
    "and the Export Centre dataset, and the dataset and the Explorer are proved to "
    "select the same rows through the same predicate. The export download route is "
    "bounded by the requester, and a holder of the key in another tenant is proved to "
    "receive 404 and no bytes. The tenant filter is proved too: tenant_slug returns "
    "one customer's rows and no other customer's, an unknown slug is a 400 and not an "
    "empty page, __none__ reaches the platform layer, and a holder of the key inside a "
    "tenant that is not the platform is served an empty tenant roster rather than "
    "CodeX's customer list."
)

M05_GAP_DROPPED = (
    "The helper's contract is that it never raises, and it keeps that contract by "
    "swallowing every failure into logger.error and returning None. For the twelve "
    "writers with no second log, that is a permanently lost fact with no counter, no "
    "dead-letter record and no alert. It has happened: the approval engine's "
    "confirmation event was emitted before it was registered, and each confirmation "
    "until then left only a log line. Add at minimum a metric so a systematic failure "
    "is visible."
)
M05_GAP_VOLUME = (
    "One unbounded response, one growing sort and one row-by-row aggregation",
    "The entity-trail detail route serialises every readable event for an object with "
    "no pagination. The trail catalogue is ordered by a subquery that probes the event "
    "index once per catalogue row, so its cost grows with the catalogue. Free-text "
    "search is seven icontains clauses with no supporting index. The dashboard's "
    "critical heatmap loads 30 days of critical events and buckets them in Python so "
    "each timestamp can be localised. All four are fine at current volume and none of "
    "them is fine at audit volume.",
    "FR-011, FR-009, FR-013",
)
M05_GAP_TESTS = (
    "Compliance rules and the append-only overrides are untested",
    "The suite covers the filter contract, the tenant filter and ambient inheritance, "
    "proxy attribution, the export's storage, authorisation and failure states, "
    "tenant isolation on every read surface including the dashboard and the Export "
    "Centre dataset, the trail counters and their one-query cost, the counts after a "
    "bulk delete, the catalogue's ordering, and actor normalisation. Still untested: "
    "the compliance-rule routes, and the save and delete overrides that hold the "
    "append-only guarantee. The regression test that proves the savepoint contract "
    "also still lives in the schools suite rather than here.",
    "FR-016, FR-005, FR-001",
)
M05_REMOVAL_LAST = (
    "•  Nothing here is history. Every item above is the current state of the code "
    "at 13675db."
)

M05_TRACE_LEAD = (
    f"MRD v{MRD_VERSION} records Module 5 as Phase V1, Backend Complete, In use "
    "Complete, with thirteen capability entries and the note that no material "
    "capability gap was identified within the module's stated backend scope. Each "
    "entry maps below to the requirement that controls it, with the current state of "
    "that capability in the code."
)
M05_TRACE_EDITS = [
    ("Audit dashboard summary", None,
     "Implemented with limits. Every figure is the reader's own; a platform reader "
     "cannot narrow it to one customer, and the heatmap aggregates in Python."),
    ("Entity-level audit trails", None,
     "Implemented with limits. Keying is stable, a trail is listed only for a reader "
     "with an event on it, and its counts are computed from the readable events; the "
     "detail response is unpaginated."),
    ("Export-job status and file retrieval", None,
     "Implemented. Status is served, history is the reader's tenant's, and an "
     "authorised download route returns the file to the requester and 404 to anybody "
     "else."),
    ("Tenant and platform visibility controls", "FR-008, FR-011, FR-013, FR-015, and section 2.2",
     "Implemented. Every read surface answers to one scoping rule: a tenant's audit "
     "officer holding the view or export key reads, counts and exports only their own "
     "tenant's rows, older rows included where their writer recorded the tenant's id, "
     "and only a caller whose home tenant is the platform reads across tenants. "
     "tenant_slug narrows a platform reader to one customer and __none__ reaches the "
     "platform layer, and the tenant roster is withheld from anybody else. Rows "
     "written before 19 August 2026 by a writer that recorded no tenant id stay "
     "platform-only."),
]
M05_RECONCILIATION = [
    "MRD RECONCILIATION",
    f"•  MRD v{MRD_VERSION} lists Module 5 as Backend Complete and In use Complete with "
    "thirteen capabilities and no material capability gap. This FRD does not dispute "
    "Complete for the module's stated backend scope: every listed capability has a "
    "backend path.",
    "•  The tenant half of the visibility controls is a boundary rather than a filter: "
    "a tenant's own audit officer reads only their tenant's rows on every surface, so "
    "this revision moves that entry from Implemented with limits to Implemented. The "
    "vocabulary and the writers are recounted at 96 action types, 17 module keys and "
    "sixteen writing surfaces.",
    f"•  No capability is added and the count stays at thirteen. MRD v{MRD_VERSION} "
    "records the trail as confined to its reader's tenant as well as narrowable to one "
    "customer, so the two documents agree; they remain versioned separately and each "
    "on its own decision.",
]

M05_CHANGE_SUMMARY = (
    "Reconciled the audit engine with the backend at 13675db, which the narrow "
    "revisions since v1.0 had not done. Every audit read is confined to its reader's "
    "tenant: platform.audit.view and platform.audit.export are tenant-holdable, the "
    "event queryset was unbounded, and a tenant's audit officer could list, open by "
    "id, count and export every other tenant's trail, walking /entity-trails/User/{id}/ "
    "one integer at a time. One scoping rule in vs_audit.scoping serves the Explorer, "
    "event detail, the trail catalogue and its detail page, the dashboard, the audit "
    "export and the Export Centre dataset; only a caller whose home tenant is the "
    "platform reads across tenants, and older rows that recorded the tenant's id in "
    "metadata are matched through it. Section 2.2, the actor table, FR-008, FR-011, "
    "FR-013, FR-015, section 7, section 8.2 and traceability described that stream as "
    "unscoped by design and are rewritten. The trail rollup is retired: its stored "
    "counts only ever grew and had drifted from the events beneath them, so a trail is "
    "a catalogue row with a label and its counts are computed from the events its "
    "reader may see (FR-006, FR-007, FR-011, sections 5.1 and 6). FR-003 records that "
    "an actor is normalised before it is read, which is what stopped branch creation "
    "through the importer leaving no trail. FR-015 stops describing the media route as "
    "a capability URL. FR-002 moves the vocabulary from 73 to 96 action types and 17 "
    "module keys, FR-004's denominator follows, and section 8.1 gains the five writers "
    "it had not listed: approval confirmations, the student roll, staff records, "
    "academic structure and the calendar, while the tickets row records the tenant it "
    "passes. Needs Attention is restated: the dropped-event risk covers twelve "
    "sole-record writers, the volume item names the catalogue's growing sort, and the "
    "untested-surface item names what is still untested rather than a count. "
    "Traceability is reconciled to MRD v2.76 and the visibility entry moves to "
    "Implemented; the count stays at thirteen. Backend evidence only; nothing here is "
    "deployed."
)


def patch_m05(source: Path, output: Path) -> None:
    doc = Document(str(source))
    T = list(doc.tables)  # Bound before anything is inserted.
    title = (
        "XVS M05 Audit and Activity Logging Functional Requirements Document "
        f"v{M05_TARGET}"
    )

    replace_cover_version(T[0], M05_TARGET)
    update_control(doc, M05_TARGET, 5)

    replace_body_paragraph(doc, "Module 5 is the platform's shared memory", M05_PURPOSE)
    replace_in_cell(
        find_row(T[3], "The logging service").cells[1],
        "writes the event and updates the rollup",
        "writes the event and records the object in the trail catalogue",
    )
    set_cell_text(find_row(T[3], "Entity trails").cells[1], M05_SCOPE_TRAILS)
    set_cell_text(find_row(T[3], "The security dashboard").cells[1], M05_SCOPE_DASHBOARD)
    replace_in_paragraph(doc, "The stream has two kinds", M05_CONTEXT_OLD, M05_CONTEXT_NEW)
    replace_body_paragraph(doc, "Reading anybody's trail is a platform decision", M05_BOUNDARIES)

    actors = T[6]
    platform_rows = [row for row in actors.rows if row.cells[0].text.strip() == "CodeX platform staff"]
    if len(platform_rows) != 2:
        raise ValueError("Expected two CodeX platform staff rows in the actor table")
    set_cell_text(platform_rows[0].cells[2], M05_ACTOR_PLATFORM_VIEW)
    set_cell_text(platform_rows[1].cells[1], M05_ACTOR_PLATFORM_EXPORT)
    insert_row_before(actors, "Compliance administrator", M05_ACTOR_TENANT_OFFICER)
    append_to_cell(find_row(actors, "Compliance administrator").cells[2], M05_ACTOR_COMPLIANCE_TAIL)

    fr002 = require_fr(T[8], "FR-002")
    set_cell_text(fr_cell(fr002, "Current evidence"), M05_FR002_EVIDENCE)
    append_to_cell(fr_cell(fr002, "Acceptance"), M05_FR002_ACCEPTANCE_TAIL)
    append_to_cell(fr_cell(fr002, "Current limit"), M05_FR002_LIMIT_TAIL)

    fr003 = require_fr(T[9], "FR-003")
    append_to_cell(fr_cell(fr003, "Acceptance"), M05_FR003_ACCEPTANCE_TAIL)
    set_cell_text(fr_cell(fr003, "Current limit"), M05_FR003_LIMIT)

    set_cell_text(fr_cell(require_fr(T[10], "FR-004"), "Current limit"), M05_FR004_LIMIT)
    set_cell_text(fr_cell(require_fr(T[12], "FR-006"), "Current limit"), M05_FR006_LIMIT)

    fr007 = require_fr(T[13], "FR-007")
    set_cell_text(fr_cell(fr007, "Current evidence"), M05_FR007_EVIDENCE)
    set_cell_text(fr_cell(fr007, "Acceptance"), M05_FR007_ACCEPTANCE)

    replace_in_cell(
        fr_cell(require_fr(T[14], "FR-008"), "Current limit"),
        M05_FR008_LIMIT_OLD, M05_FR008_LIMIT_NEW,
    )

    fr011 = require_fr(T[17], "FR-011")
    set_cell_text(fr_cell(fr011, "Current evidence"), M05_FR011_EVIDENCE)
    set_cell_text(fr_cell(fr011, "Acceptance"), M05_FR011_ACCEPTANCE)
    set_cell_text(fr_cell(fr011, "Current limit"), M05_FR011_LIMIT)

    fr013 = require_fr(T[19], "FR-013")
    append_to_cell(fr_cell(fr013, "Current evidence"), M05_FR013_EVIDENCE_TAIL)
    append_to_cell(fr_cell(fr013, "Acceptance"), M05_FR013_ACCEPTANCE_TAIL)
    set_cell_text(fr_cell(fr013, "Current limit"), M05_FR013_LIMIT)

    fr015 = require_fr(T[21], "FR-015")
    for old, new in M05_FR015_REPLACEMENTS:
        replace_in_cell(fr_cell(fr015, "Current evidence"), old, new)
    append_to_cell(fr_cell(fr015, "Acceptance"), M05_FR015_ACCEPTANCE_TAIL)

    step7 = find_row(T[23], "7")
    set_cell_text(step7.cells[1], M05_STEP7[0])
    set_cell_text(step7.cells[2], M05_STEP7[1])

    trail_model = find_row(T[25], "EntityAuditTrail")
    set_cell_text(trail_model.cells[1], M05_MODEL_TRAIL[0])
    set_cell_text(trail_model.cells[2], M05_MODEL_TRAIL[1])

    replace_body_paragraph(doc, "Routes are mounted under /v1/audit/", M05_API_LEAD)
    for table, edits in ((T[26], M05_ROUTE_EDITS_71), (T[27], M05_ROUTE_EDITS_72)):
        for route, purpose in edits:
            rows = [row for row in table.rows if row.cells[0].text.strip() == route]
            if len(rows) != 1:
                raise ValueError(f"Route row not found exactly once: {route}")
            set_cell_text(rows[0].cells[1], purpose)
    errors = T[28]
    set_cell_text(find_row(errors, "An entity trail that has never been written").cells[0], M05_ERROR_TRAIL)
    insert_row_before(errors, "An entity trail that has never been written", M05_ERROR_FOREIGN_EVENT)

    replace_body_paragraph(doc, "Eleven surfaces write through", M05_WRITERS_LEAD)
    writers = T[30]
    set_cell_text(find_row(writers, "M2 XVision Admin Console").cells[1], M05_WRITER_M2)
    set_cell_text(find_row(writers, "M31 Support Tickets").cells[2], M05_WRITER_M31_TENANT)
    insert_row_before(writers, "M9 School Onboarding", M05_WRITER_M7)
    for values in M05_WRITERS_SCHOOL:
        insert_row_before(writers, "M19 Finance", values)

    replace_body_paragraph(doc, "•  Cross-tenant isolation", M05_VERIFY_ISOLATION)

    gaps = T[31]
    set_cell_text(find_row(gaps, "A dropped event leaves nothing", column=1).cells[2], M05_GAP_DROPPED)
    volume = find_row(gaps, "Two unbounded responses", column=1)
    for cell, value in zip(volume.cells[1:], M05_GAP_VOLUME):
        set_cell_text(cell, value)
    untested = find_row(gaps, "Most of the module's own surface", column=1)
    for cell, value in zip(untested.cells[1:], M05_GAP_TESTS):
        set_cell_text(cell, value)
    removal = T[32].rows[0].cells[0].paragraphs
    if not removal[-1].text.strip().startswith("•  This is a v1.0 baseline"):
        raise ValueError("Removal rule's last line is not the baseline line")
    set_run_text(removal[-1], M05_REMOVAL_LAST)

    replace_body_paragraph(doc, "MRD v2.34 records Module 5", M05_TRACE_LEAD)
    trace = T[33]
    for capability, coverage, state in M05_TRACE_EDITS:
        row = find_row(trace, capability)
        if coverage:
            set_cell_text(row.cells[1], coverage)
        set_cell_text(row.cells[2], state)
    rewrite_callout(T[34], M05_RECONCILIATION)

    prepend_change_log(T[35], M05_TARGET, M05_CHANGE_SUMMARY)
    finish(doc, output, title, M05_TARGET)


# ═════════════════════════════════════════════════════════════════════════════
# Module 17 - Billing & Invoicing
# ═════════════════════════════════════════════════════════════════════════════

M17_DIR = "17-billing-and-invoicing"
M17_STEM = "XVS_M17_Billing_and_Invoicing_Functional_Requirements_Document"
M17_SOURCE, M17_TARGET = "1.4", "1.5"

M17_SCOPE_DOMAIN = (
    "The student and guardian domains, reached only through the finance abstraction "
    "layer (FR-011, FR-014). Nothing in the receivable core knows what a class or a "
    "term is, and that is the design."
)

M17_FR001_LIMIT = (
    "A customer carries a loose source type and id naming the record it was opened "
    "for, which the finance abstraction layer uses to find or open the account for a "
    "child it bills (FR-011). Nothing in the receivable core reads that reference, so "
    "the link lives on the school side of the boundary."
)

M17_FR002_EVIDENCE = (
    "A fee structure holds fee items as a template. Generation materialises one "
    "invoice per selected customer and posts each through the ordinary receivables "
    "path, so it raises the usual journal and appears everywhere invoices already do. "
    "A structure can also be duplicated. Its code is optional on both routes: a code "
    "the caller types is honoured exactly, and a structure created or duplicated "
    "without one is given a code derived from its name by FeeStructure.generate_code, "
    "upper-cased and hyphenated, unique within the entity, suffixed -2, -3 on a clash "
    "and trimmed so the result always fits the 32-character column. A duplicate "
    "derives its code from its new name, which defaults to the source's name with "
    "(copy) appended. The code matters because every invoice a structure raises "
    "carries it as the FEE:<code> reference, which is what a statement shows and what "
    "makes a repeated run skip a customer already billed. fees.generate_invoices never "
    "leaves a due date empty: a caller that supplies none gets the invoice date plus "
    "the entity's default_invoice_due_days, because a null due date is never overdue "
    "and would sit outside every ageing bucket and dunning run."
)
M17_FR002_ACCEPTANCE = (
    "There is no second, quieter way for a fee structure to become money owed: "
    "generation is the only one, and it uses the same posting service as a "
    "hand-raised invoice. A typed code the entity already holds is refused with 400 "
    "rather than replaced, so an invoice never carries a reference the tenant did not "
    "choose, while two structures of the same name receive distinct derived codes "
    "rather than a refusal. FinanceAPITests covers the derived code, the suffix, the "
    "honoured and still-refused typed code, the column bound and the duplicate "
    "(test_a_structure_created_without_a_code_gets_one_from_its_name, "
    "test_a_second_structure_of_the_same_name_is_suffixed_not_refused, "
    "test_a_code_the_caller_typed_is_still_honoured_and_still_refused, "
    "test_a_generated_code_never_outgrows_its_column, "
    "test_a_duplicate_without_a_code_derives_one_too), and "
    "test_generated_fee_invoices_always_carry_a_due_date proves no generated invoice "
    "is left without a due date."
)

M17_FR011_EVIDENCE_TAIL = (
    " Both steps are served at /v1/school-finance/: POST "
    "fee-structures/{id}/link-term/ under finance.feestructure.edit, and POST "
    "fee-structures/{id}/generate-invoices/ under finance.feestructure.generate, which "
    "takes a named, non-empty list of students and a dry_run flag. A dry run executes "
    "the real generation inside a transaction and rolls it back, so the preview is "
    "priced by the code that posts; it answers 200 and a real run 201. The structure "
    "is resolved inside the caller's tenant and branches, so another tenant's or "
    "another branch's answers 404. Each bill carries the due date the school's fee due "
    "policy resolves (FR-014), and is raised in the name of the effective user, so a "
    "CodeX operator acting inside a school's session raises it as that school's act."
)
M17_FR011_ACCEPTANCE = (
    "Billing a structure that names no term is refused rather than raising invoices "
    "nobody can attribute to a period. A term that fee structures still bill cannot be "
    "deleted; the database refuses it. A repeated run bills nobody twice and reports "
    "who was skipped, and a child named twice is billed once. A reference the bridge "
    "cannot turn into a customer stops the run with a typed refusal instead of being "
    "dropped from it silently, which is how a school under-bills and never finds out. "
    "The refusals are 409 TERM_NOT_LINKED for an unlinked structure, 400 "
    "INVALID_TERM_LINK for a session or term that does not fit, 400 "
    "CUSTOMER_NOT_PROVISIONED, 409 ENTITY_NOT_PROVISIONED where the school has no books "
    "yet, and 503 FINANCE_UNAVAILABLE where the finance port cannot answer. "
    "FalRouteTests in schools/core/fal/tests/test_route.py covers the permission "
    "gates, tenant scoping, the unlinked refusal, a dry run that bills nobody, a real "
    "run whose second run skips, an empty cohort and a child named twice; "
    "GenerationPreviewTests proves a billed cohort carries a due date and records who "
    "raised it."
)

M17_FR012_EVIDENCE_TAIL = (
    " The page's host is the tenant's own application address, which the shared "
    "application-address helper in vs_tenants builds at call time by inserting the "
    "tenant's slug as a subdomain of the configured base address; the platform's own "
    "books use a fixed pay subdomain instead. A base configured without a scheme "
    "yields no address rather than one missing the slug, so the link is left off the "
    "document rather than pointing somewhere wrong. PayerHostTests in "
    "tests_pay_links.py covers two tenants, a local development host and the "
    "platform's books."
)

M17_FR013_EVIDENCE_TAIL = (
    " The invoice and receipt email routes, which preview the message and its "
    "recipients and send it, resolve their document through the same narrowing, and "
    "the statement email resolves its customer through the customer resolver, so a "
    "document outside the caller's branches can be neither previewed nor sent."
)
M17_FR013_ACCEPTANCE_TAIL = (
    " DocumentEmailNarrowsTests in tests_branch_scope.py proves another branch's "
    "invoice and receipt cannot be previewed or sent, while the caller's own and an "
    "invoice with no branch still preview."
)

M17_FR014 = {
    "header": "FR-014 | Implemented",
    "requirement": (
        "A school must be able to say when its fee bills fall due in terms of its own "
        "calendar, and every bill it raises for a cohort must carry that date, without "
        "this module learning what a term is."
    ),
    "evidence": (
        "SchoolFeeDuePolicy lives in the finance abstraction layer, one row per tenant "
        "with no branch column, because a term's fees fall due on the same day at every "
        "branch. Its basis is TERM_END, SESSION_END, MONTH_END or DAYS_AFTER with a day "
        "count; a school that has never set one bills on TERM_END, and the day count "
        "defaults to 30. resolve_due_date turns the basis into a date against the fee "
        "structure's own term link, falling back from the term's end to the session's "
        "for a structure linked to a whole session, and from any unknown date to the "
        "day count, so a rule always produces a deadline. A resolved date earlier than "
        "the bill's own date is lifted to the bill date, so a late run is payable at "
        "once rather than overdue on arrival. Cohort billing (FR-011) hands "
        "fees.generate_invoices the resolved date as a plain date. GET and PATCH "
        "/v1/school-finance/settings/fee-due-policy/ read and change the rule under "
        "school.fees.view and school.fees.manage, and the read previews the date each "
        "basis would put on a bill raised today, priced by the function that bills."
    ),
    "acceptance": (
        "A school that has never opened the setting reads the default it is already "
        "billing by rather than an empty body. An unknown basis is refused with 400 "
        "INVALID_BASIS, and a day count outside 0 to 365 with 400 INVALID_DAYS_AFTER. "
        "The rule is read and written only inside the caller's own tenant. "
        "DueDateResolutionTests and FeeDuePolicyEndpointTests in "
        "schools/core/fal/tests/test_fee_due_policy.py cover each basis, the session "
        "fallback, a leap-year month end, the lift to the bill date, the default read, "
        "the per-option preview, a change, the 365-day bound, a reader who cannot "
        "write, and another school reading this one's rule."
    ),
    "limit": (
        "The policy reaches bills raised through cohort billing only. This module's "
        "own generation route (FR-002) takes a caller-supplied date or the entity's "
        "default payment terms, so a school billing from that route gets a date its "
        "own rule did not choose. Changing the rule dates future bills and leaves bills "
        "already raised as they were."
    ),
}

M17_MODEL_FEE_STRUCTURE = (
    "Becomes money owed only through generation. Its code is unique within the "
    "entity, derived from the name when none is typed, and carried on every invoice it "
    "raises as FEE:<code>."
)
M17_MODEL_DUE_POLICY = [
    "SchoolFeeDuePolicy",
    "When a school's fee bills fall due.",
    "Held by the finance abstraction layer, one per tenant with no branch column; "
    "resolved to a plain date before this module sees it (FR-014).",
]

M17_API_LEAD = (
    "Routes are mounted under /v1/finance/ and require an asserted entity, with two "
    "deliberate exceptions: the public pay routes in 7.1 carry no session at all and "
    "are authorised by the signed token in the path, and the finance abstraction "
    "layer's routes are mounted under /v1/school-finance/ and scope to the caller's "
    "tenant and branches. The routes below are Module 17's; the ledger routes belong "
    "to Module 19 and the adjustment routes to Module 20."
)
M17_ROUTES_71 = [
    ["POST /v1/school-finance/fee-structures/{id}/link-term/",
     "Tie a fee structure to an academic session and term."],
    ["POST /v1/school-finance/fee-structures/{id}/generate-invoices/",
     "Bill a named cohort, or preview the run with dry_run."],
    ["GET, PATCH /v1/school-finance/settings/fee-due-policy/",
     "When the school's fee bills fall due, with each option's date previewed."],
]
M17_ROUTES_72 = [
    ["GET, POST /invoices/{id}/email/, /payments/{id}/email/, /customers/{id}/statement-email/",
     "GET previews the recipients and lists earlier deliveries; POST sends."],
]
M17_ERRORS = [
    ["A fee structure code the caller typed that the entity already holds",
     "400 on code. A typed code is never swapped for a derived one."],
    ["Billing a cohort from a structure with no term link",
     "409 TERM_NOT_LINKED."],
    ["A fee due basis off the list, or a day count outside 0 to 365",
     "400 INVALID_BASIS or INVALID_DAYS_AFTER."],
]
M17_DEPENDENCY_FAL = (
    "Owns the fee-structure-to-term link, cohort billing and its routes, and the "
    "school fee due policy, and is the only way a school module reaches this one. It "
    "holds the school vocabulary so this module does not have to, and hands this "
    "module a plain due date rather than a rule about terms."
)
M17_GAP_DUE_POLICY = (
    "• The school fee due policy is applied only by cohort billing through the finance "
    "abstraction layer. A structure billed through this module's own generation route "
    "takes the caller's date or the entity's default payment terms, so the same term's "
    "fees can fall due on different days depending on which route raised them."
)
M17_TRACE_ROW = ["School fee due-date policy", "FR-014"]
M17_TRACE_LEAD = (
    f"Module 17 carries 20 capability entries in MRD v{MRD_VERSION}. Each maps to the "
    "requirements below."
)

M17_CHANGE_SUMMARY = (
    "Reconciled billing with the backend at 13675db. A fee structure's code is "
    "optional on create and duplicate: one the caller types is honoured and a clash on "
    "it is still refused, and one left out is derived from the name, unique within the "
    "entity and bounded to the column (FR-002). Generation never leaves a due date "
    "empty, because a null due date is never overdue and fell outside every ageing "
    "bucket and dunning run. The finance abstraction layer's routes, which this "
    "document had not recorded, are added to FR-011 and section 7: linking a structure "
    "to a term, and billing a named cohort with a dry run that executes the real "
    "generation and rolls it back. New FR-014 records the school fee due policy those "
    "bills carry, with a floor that makes a late run payable at once rather than "
    "overdue on arrival, and its limit that this module's own generation route does "
    "not apply it, which is also a further gap. FR-013 gains the invoice and receipt "
    "email routes, which resolved their document without the branch rule, so a bursar "
    "at one branch could preview and send another branch's invoice. FR-012 records "
    "that the pay page's address comes from the shared tenant application helper and "
    "is left off rather than built without the slug when the base carries no scheme. "
    "The cover had read Version 1.3 since v1.4 and now matches Document Control, and "
    "section 1.2 and FR-001 stop calling the student domain future. Traceability is "
    "reconciled to MRD v2.76 with one new entry, taking Module 17 from 19 to 20. "
    "Backend evidence only; nothing here is deployed."
)


def patch_m17(source: Path, output: Path) -> None:
    doc = Document(str(source))
    T = list(doc.tables)  # Bound before anything is inserted.
    title = (
        "XVS M17 Billing and Invoicing Functional Requirements Document "
        f"v{M17_TARGET}"
    )

    replace_cover_version(T[0], M17_TARGET)
    update_control(doc, M17_TARGET, 17)

    set_cell_text(find_row(T[4], "Who the customer is").cells[1], M17_SCOPE_DOMAIN)
    set_cell_text(fr_cell(require_fr(T[7], "FR-001"), "Current limit"), M17_FR001_LIMIT)

    fr002 = require_fr(T[8], "FR-002")
    set_cell_text(fr_cell(fr002, "Current evidence"), M17_FR002_EVIDENCE)
    set_cell_text(fr_cell(fr002, "Acceptance"), M17_FR002_ACCEPTANCE)

    fr011 = require_fr(T[17], "FR-011")
    append_to_cell(fr_cell(fr011, "Current evidence"), M17_FR011_EVIDENCE_TAIL)
    set_cell_text(fr_cell(fr011, "Acceptance"), M17_FR011_ACCEPTANCE)

    append_to_cell(fr_cell(require_fr(T[18], "FR-012"), "Current evidence"), M17_FR012_EVIDENCE_TAIL)

    fr013 = require_fr(T[19], "FR-013")
    append_to_cell(fr_cell(fr013, "Current evidence"), M17_FR013_EVIDENCE_TAIL)
    append_to_cell(fr_cell(fr013, "Acceptance"), M17_FR013_ACCEPTANCE_TAIL)

    set_cell_text(find_row(T[22], "FeeStructure, FeeItem").cells[2], M17_MODEL_FEE_STRUCTURE)
    append_row(T[22], M17_MODEL_DUE_POLICY)

    replace_body_paragraph(doc, "All routes are mounted under /v1/finance/", M17_API_LEAD)
    for values in M17_ROUTES_71:
        append_row(T[23], values)
    for values in M17_ROUTES_72:
        append_row(T[24], values)
    for values in M17_ERRORS:
        append_row(T[25], values)

    set_cell_text(find_row(T[26], "Finance abstraction layer").cells[1], M17_DEPENDENCY_FAL)
    append_bullet(T[28], M17_GAP_DUE_POLICY)

    trace = T[29]
    set_cell_text(find_row(trace, "Cohort invoice generation").cells[1], "FR-011, FR-014")
    append_row(trace, M17_TRACE_ROW)
    replace_body_paragraph(doc, "Module 17 carries", M17_TRACE_LEAD)

    prepend_change_log(T[30], M17_TARGET, M17_CHANGE_SUMMARY)

    # Inserted last so no earlier edit reads a shifted table index.
    add_fr_after(doc, fr013, M17_FR014, heading="FR-014 Let a School Set When Its Fee Bills Fall Due")
    finish(doc, output, title, M17_TARGET)


# ═════════════════════════════════════════════════════════════════════════════
# Module 18 - Payments & Collections
# ═════════════════════════════════════════════════════════════════════════════

M18_DIR = "18-payments-and-collections"
M18_STEM = "XVS_M18_Payments_and_Collections_Functional_Requirements_Document"
M18_SOURCE, M18_TARGET = "1.9", "1.10"

M18_ACTOR_CHECKER = (
    "Membership of the payout-approver and payout-senior-approver approver groups, "
    "which the tenant composes. No permission key confers approval."
)

M18_FR015_EVIDENCE = (
    "The default ladder has an always-on payout checker and a conditional senior "
    "checker at 50,000,000 kobo. Both stages set skip_if_no_approvers=False. Tenant "
    "provisioning creates the payout-approver and payout-senior-approver approver "
    "groups empty and publishes the ladder naming them, and the data migration adds "
    "the senior stage only to the exact shipped one-stage shape."
)
M18_FR015_ACCEPTANCE = (
    "An unstaffed applicable stage parks. Adding somebody to the approver group a "
    "stage names makes it actionable. Custom ladders are left untouched. Payout "
    "handlers advertise that continue-without-approval is unavailable and reject the "
    "release endpoint with a typed 409."
)

M18_FR020_LIMIT = (
    "Resolving is not approving. The platform row supplies routing and no steps, so "
    "submitting a batch from an entity relying on it is refused with 409 "
    "APPROVAL_NOT_CONFIGURED until the entity publishes its own ladder. The payout "
    "routes accept confirm_without_approval with a reason, as every submit route does, "
    "but a confirmation cannot release a payout: it ends the approval with no human "
    "vote, and the approval-time check that requires one distinct checker, or two at "
    "the high-value threshold, then refuses the batch with PAYOUT_APPROVAL_REQUIRED, "
    "so nothing is sent. That outcome is read from the code; no test submits a "
    "confirmed batch against a ladder with no steps. A tenant ladder supplies steps "
    "but no people: each stage stays blocked until somebody joins the approver group "
    "it names."
)

M18_FR022 = {
    "header": "FR-022 | Implemented",
    "requirement": (
        "Reversing a payout approval must not reopen a batch whose money has already "
        "gone to the provider, and a reversal and a dispatch arriving together must "
        "not both succeed."
    ),
    "evidence": (
        "Module 7 asks the owning handler before it writes any reversal and tells it "
        "afterwards inside the same transaction. PayoutBatchApprovalHandler."
        "validate_reversal row-locks the batch and every one of its instructions, and "
        "refuses with REVERSAL_NOT_ALLOWED, naming the batch and its status, once any "
        "instruction has left PENDING, the batch has left DRAFT, or the batch has been "
        "submitted. on_action_reversed returns the batch's approval marker to "
        "PENDING_APPROVAL, so the recovery sweep, which selects on that marker, leaves "
        "it alone. _dispatch_transfer re-validates the approved instance inside the "
        "same claim that locks the instruction, so whichever of a reversal and a send "
        "takes the lock first, the other sees the finished state."
    ),
    "acceptance": (
        "A reversal after an instruction has been claimed is refused and changes "
        "nothing; a payout that has gone out is unwound as a payout, not as an "
        "approval. A reversal before dispatch returns the batch to the approval queue, "
        "and the batch can be approved and dispatched again. A dispatch arriving after "
        "a reversal sends nothing, and a reversal landing mid-dispatch stops the send "
        "it beat. PayoutBatchApprovalTests covers each case: "
        "test_reversal_is_refused_once_the_batch_has_reached_the_provider, "
        "test_reversal_before_dispatch_returns_the_batch_to_the_queue, "
        "test_the_recovery_sweep_leaves_a_reversed_approval_alone, "
        "test_a_dispatch_that_arrives_after_a_reversal_sends_nothing, "
        "test_a_reversal_landing_mid_dispatch_stops_the_send_it_beat and "
        "test_a_reversed_batch_can_be_approved_and_dispatched_again."
    ),
    "limit": (
        "The refusal covers the whole batch. Once any instruction has been claimed, "
        "the approval of the rest cannot be withdrawn either, and no route cancels the "
        "instructions still pending, so withdrawing the approval cannot stop the rest "
        "of a half-sent batch. See Section 9."
    ),
}

M18_ERRORS = [
    ["Submitting a batch whose resolved ladder has no steps",
     "409 APPROVAL_NOT_CONFIGURED without confirm_without_approval, and nothing is "
     "written. With it, the approval-time vote check still refuses the batch with "
     "PAYOUT_APPROVAL_REQUIRED."],
    ["Reversing a payout approval once any instruction has been claimed for the provider",
     "422 REVERSAL_NOT_ALLOWED from the workflow engine; the approval and the batch "
     "are unchanged."],
]

M18_LIFECYCLE_TAIL = (
    " An approval can be reversed only while every instruction is still pending and "
    "the batch is still a draft; the reversal returns the batch to the approval queue, "
    "and once any instruction has been claimed the reversal is refused (FR-022)."
)

M18_DEPENDENCY_WORKFLOW_TAIL = (
    " It asks this module before reversing a payout approval, and this module refuses "
    "once any instruction has been claimed."
)
M18_DEPENDENCY_RBAC = (
    "Supplies the permission keys the views enforce. It supplies no approving role "
    "and no approve key: who may approve a payout is decided by the workflow stage, and "
    "the seeded stages name approver groups the tenant composes."
)

M18_CONTROL = [
    "CURRENT PAYOUT CONTROL - FAIL CLOSED",
    "• Approval is not optional by route. Every single or bulk payout enters Module 7 "
    "and only the terminal callback may ask the provider to transfer.",
    "• New books receive tenant policy, whose stages name approver groups created "
    "empty. The shared platform row carries no steps, so an entity that has published "
    "no ladder of its own is refused rather than approved, a confirmation cannot stand "
    "in for the missing human vote, and payouts forbid continuing without approval "
    "outright.",
    "• payout_approval_health names any active entity that still cannot resolve a "
    "standard ladder and fails the release check. Missing approver appointments are a "
    "separate parked-state health concern.",
    "• The default threshold is N500,000: one checker below it, and a distinct senior "
    "checker at or above it.",
    "• Approval and the transfer are separate transactions. The approving vote commits "
    "before the provider is called, so a rollback during approval cannot leave money "
    "sent, and a guard refuses any provider call made with a transaction open.",
    "• An instruction is claimed before its own send and only a clean provider "
    "rejection marks it failed, so no retry re-sends it and no timeout records moved "
    "money as a failure.",
    "• The recovery sweep finishes any approved batch that still holds an unsent "
    "instruction, whether or not some of its transfers have already gone, and moves no "
    "money against a batch whose status is terminal.",
    "• An approval cannot be reversed once any instruction has been claimed for the "
    "provider, and a reversal and a send take the same row lock, so the approval "
    "screen never reads as undecided while the money is being paid.",
]
M18_GAP_HALF_SENT = (
    "• Once any instruction in a batch has been claimed for the provider, the batch's "
    "approval can no longer be reversed and no route cancels the instructions still "
    "pending, so a batch found to be wrong after its first transfer goes on to send "
    "the rest when the worker or the recovery sweep reaches them."
)
M18_TRACE_ROW = ["Approval reversal refused once a payout has reached the provider", "FR-022"]
M18_TRACE_LEAD = (
    f"Module 18 carries 24 capability entries in MRD v{MRD_VERSION}. Each maps to the "
    "requirements below."
)

M18_CHANGE_SUMMARY = (
    "Recorded the reversal contract over payout approval. Reversing an approval "
    "reopened the workflow without asking the module that owned the document, so an "
    "administrator could reverse a batch whose provider had already sent the money and "
    "leave the approval screen reading In progress while the vendor held the funds. "
    "New FR-022 records that the payout handler refuses a reversal once any "
    "instruction has been claimed, locks every instruction to decide it, and returns "
    "an undispatched batch's approval marker to pending so the recovery sweep leaves "
    "it alone, and that the transfer re-validates the approval inside its own claim, "
    "so a reversal and a send serialise. Its limit, that nothing stops the rest of a "
    "half-sent batch, is also a further gap. FR-020's limit is corrected: it said an "
    "entity relying on the stageless platform row may proceed on a recorded "
    "confirmation, but a confirmation ends the approval with no human vote and the "
    "approval-time check still refuses the batch, so no payout leaves on a "
    "confirmation alone. FR-015 and the Module 4 dependency stop describing approver "
    "roles, which provisioning no longer creates, and the actor table gives the "
    "approver groups their real codes. The typed errors gain APPROVAL_NOT_CONFIGURED "
    "and REVERSAL_NOT_ALLOWED, the fail-closed control box has its heading, bullets and "
    "border restored as the document drew them, and traceability is reconciled to MRD "
    "v2.76 with one new entry, taking Module 18 from 23 to 24. Backend evidence only; "
    "nothing here is deployed."
)


def patch_m18(source: Path, output: Path) -> None:
    doc = Document(str(source))
    T = list(doc.tables)  # Bound before anything is inserted.
    title = (
        "XVS M18 Payments and Collections Functional Requirements Document "
        f"v{M18_TARGET}"
    )

    replace_cover_version(T[0], M18_TARGET)
    update_control(doc, M18_TARGET, 18)

    set_cell_text(find_row(T[6], "Payout checker").cells[2], M18_ACTOR_CHECKER)

    fr015 = require_fr(T[23], "FR-015")
    set_cell_text(fr_cell(fr015, "Current evidence"), M18_FR015_EVIDENCE)
    set_cell_text(fr_cell(fr015, "Acceptance"), M18_FR015_ACCEPTANCE)
    set_cell_text(fr_cell(require_fr(T[24], "FR-020"), "Current limit"), M18_FR020_LIMIT)

    for values in M18_ERRORS:
        append_row(T[35], values)

    replace_in_paragraph(
        doc, "A payout instruction is created",
        "Confirmation remains idempotent and books the vendor payment once.",
        "Confirmation remains idempotent and books the vendor payment once."
        + M18_LIFECYCLE_TAIL,
    )

    dependencies = T[36]
    append_to_cell(find_row(dependencies, "Module 7").cells[1], M18_DEPENDENCY_WORKFLOW_TAIL)
    set_cell_text(find_row(dependencies, "Module 4").cells[1], M18_DEPENDENCY_RBAC)

    rebuild_callout_from(T[37], T[38], M18_CONTROL)
    append_bullet(T[38], M18_GAP_HALF_SENT)

    append_row(T[39], M18_TRACE_ROW)
    replace_body_paragraph(doc, "Module 18 carries", M18_TRACE_LEAD)

    prepend_change_log(T[40], M18_TARGET, M18_CHANGE_SUMMARY)

    fr021 = require_fr(T[22], "FR-021")
    add_fr_after(doc, fr021, M18_FR022, heading="FR-022 Refuse to Undo an Approval Once the Money Has Moved")
    finish(doc, output, title, M18_TARGET)


# ═════════════════════════════════════════════════════════════════════════════
# Module 19 - Finance & Accounting
# ═════════════════════════════════════════════════════════════════════════════

M19_DIR = "19-finance-and-accounting"
M19_STEM = "XVS_M19_Finance_and_Accounting_Functional_Requirements_Document"
M19_SOURCE, M19_TARGET = "1.8", "1.9"

M19_FR003_EVIDENCE_TAIL = (
    " Where a journal went through approval, Module 7 asks the finance approval "
    "handler before reversing a decision and tells it afterwards in the same "
    "transaction. The handler refuses with REVERSAL_NOT_ALLOWED once the document has "
    "left draft or pending approval, because the engine can withdraw its record of an "
    "approval and cannot withdraw a posted entry, and undoing a rejection or a return "
    "puts the document back to pending approval rather than leaving an editable draft "
    "of something still under review. The same handler base serves expense claims and "
    "the four adjustment types in Module 20."
)
M19_FR003_ACCEPTANCE_TAIL = (
    " JournalApprovalWorkflowTests proves a reversal is refused once the journal has "
    "posted, and that reversing a stage that has not posted returns the journal to the "
    "approval queue."
)
M19_FR007_EVIDENCE_TAIL = (
    " A statement uploaded through the import wizard is filed under its account's "
    "branch rather than the uploader's, so the statements of an account with no branch "
    "stay unassigned however they arrive, and a branch's collection account keeps its "
    "statements to that branch even when somebody covering several branches uploads "
    "one."
)
M19_FR007_ACCEPTANCE_TAIL = (
    " BankStatementImportWizardTests proves the batch takes the branch of the account "
    "it continues, and that an account with no branch keeps its statements without one."
)
M19_FR016_LIMIT_TAIL = (
    " Creating a set of books is a platform act: finance.entity.create is registered "
    "with platform scope, so no tenant role can hold it and a tenant works in the books "
    "it was given when it was created, while reading the entity list stays "
    "tenant-holdable."
)
M19_FR019_EVIDENCE_TAIL = (
    " Banking is held to the same rule: every route that reaches a bank account by id, "
    "including rename, statement import, auto-reconcile, split match and "
    "reconciliation completion, resolves it through one narrowed resolver, and "
    "statements and statement lines take their account's branch, so an account the "
    "list withholds cannot be read or worked on by naming its id."
)
M19_FR019_ACCEPTANCE_TAIL = (
    " An account, statement or line outside the caller's branches answers 404 exactly "
    "as an unknown id does, and the account with no branch stays reachable from every "
    "branch. BankAccountReachedByIdNarrowsTests in tests_branch_scope.py proves each "
    "route refuses another branch's account and its statements and lines, still "
    "answers for the caller's own, and that the list and the detail route agree."
)
M19_FR021_EVIDENCE_TAIL = (
    " A salary row may also name the employee's account: employee takes a user id, "
    "resolves it only inside the entity's own tenant and refuses any other with a field "
    "error, and a row created or linked with no name typed takes its name from that "
    "account. The link is optional, because a tenant may pay somebody with no account, "
    "such as a contractor, and it is not backfilled, so the roster returns employee_id "
    "as null for every row written before it."
)
M19_FR021_LIMIT_TAIL = " No finance test exercises the employee link."
M19_MODEL_SALARY = (
    "Branch nullable and not backfilled; a branch payroll run reads it exclusively. "
    "The employee's user account is an optional link inside the same tenant, also not "
    "backfilled (FR-021)."
)
M19_ERRORS = [
    ["Opening a bank account, statement or statement line outside the caller's branches",
     "404, identical to an unknown id."],
    ["Reversing the approval of a journal or expense claim that has already posted",
     "422 REVERSAL_NOT_ALLOWED from the workflow engine. The correction is a reversing "
     "entry."],
    ["Naming another tenant's user as the employee on a salary row",
     "Refused with a field error on employee."],
]
M19_DEPENDENCY_WORKFLOW = (
    "Runs approval where a template exists for a finance document type. The submit "
    "response carries the parked warning when nobody can approve; submitting against a "
    "ladder with no steps is refused with APPROVAL_NOT_CONFIGURED unless the submitter "
    "confirms and is recorded; and reversing a decision is refused once the document "
    "has posted."
)
M19_GAP_DIRECT_POST = (
    "• The direct-post routes for journals and expense claims refuse a post only while "
    "a stage of the resolved ladder would apply. Where the ladder has no steps the "
    "document posts with no confirmation and no POSTED_WITHOUT_APPROVAL record, "
    "although the same document sent through submit is refused, and Module 20's four "
    "adjustment types ask for the confirmation on both routes."
)
M19_TRACE_LEAD = (
    f"Module 19 carries 26 capability entries in MRD v{MRD_VERSION}. Each maps to the "
    "requirements below."
)
M19_TRACE_CLOSE = (
    f"All 26 entries are traced above. Module number, name, phase, backend and in-use "
    f"states and code ownership agree with MRD v{MRD_VERSION}. The MRD's Module 19 "
    "note, Module 4's FRD and this document agree on what is open: the branch "
    "narrowing has landed on every branch-bearing read and write here, bank accounts "
    "reached by id among them, and the financial statements are the one surface it has "
    "not reached."
)

M19_CHANGE_SUMMARY = (
    "Reconciled the ledger with the backend at 13675db. Bank accounts reached by id "
    "were not narrowed to the caller's branches although the list was, so a bursar "
    "covering one branch could read, rename, import onto and reconcile another "
    "branch's account by typing its id; every account, statement and statement-line "
    "route resolves through one narrowed resolver and answers 404 as for an unknown "
    "id, which FR-019 records. FR-007 records that a statement import is filed under "
    "its account's branch, where every batch had carried none. FR-003 records the "
    "reversal contract: an approval decision on a journal or expense claim can be "
    "withdrawn only before posting, and undoing a rejection or return puts the "
    "document back to pending approval. FR-021 records the optional link from a salary "
    "row to the employee's account, resolved only inside the entity's tenant, and that "
    "no finance test covers it. FR-016 records that creating a set of books is a "
    "platform act. A new further gap records that the journal and expense-claim direct "
    "posts do not ask for the confirmation a ladder with no steps requires, where the "
    "adjustment routes and every submit route do. Traceability is reconciled to MRD "
    "v2.76; its lead had named 25 entries and its closing note 24 while the table "
    "carries 26. Backend evidence only; nothing here is deployed."
)


def patch_m19(source: Path, output: Path) -> None:
    doc = Document(str(source))
    T = list(doc.tables)  # Bound before anything is inserted.
    title = (
        "XVS M19 Finance and Accounting Functional Requirements Document "
        f"v{M19_TARGET}"
    )

    replace_cover_version(T[0], M19_TARGET)
    update_control(doc, M19_TARGET, 19)

    fr003 = require_fr(T[11], "FR-003")
    append_to_cell(fr_cell(fr003, "Current evidence"), M19_FR003_EVIDENCE_TAIL)
    append_to_cell(fr_cell(fr003, "Acceptance"), M19_FR003_ACCEPTANCE_TAIL)

    fr007 = require_fr(T[15], "FR-007")
    append_to_cell(fr_cell(fr007, "Current evidence"), M19_FR007_EVIDENCE_TAIL)
    append_to_cell(fr_cell(fr007, "Acceptance"), M19_FR007_ACCEPTANCE_TAIL)

    append_to_cell(fr_cell(require_fr(T[24], "FR-016"), "Current limit"), M19_FR016_LIMIT_TAIL)

    fr019 = require_fr(T[27], "FR-019")
    append_to_cell(fr_cell(fr019, "Current evidence"), M19_FR019_EVIDENCE_TAIL)
    append_to_cell(fr_cell(fr019, "Acceptance"), M19_FR019_ACCEPTANCE_TAIL)

    fr021 = require_fr(T[29], "FR-021")
    append_to_cell(fr_cell(fr021, "Current evidence"), M19_FR021_EVIDENCE_TAIL)
    append_to_cell(fr_cell(fr021, "Current limit"), M19_FR021_LIMIT_TAIL)

    set_cell_text(find_row(T[33], "EmployeeSalary").cells[2], M19_MODEL_SALARY)
    for values in M19_ERRORS:
        append_row(T[37], values)
    set_cell_text(find_row(T[38], "Module 7").cells[1], M19_DEPENDENCY_WORKFLOW)
    statements_gap = [
        paragraph for paragraph in T[39].rows[0].cells[0].paragraphs
        if "MRD v2.36" in paragraph.text
    ]
    if len(statements_gap) != 1:
        raise ValueError("Statements gap MRD reference not found exactly once")
    set_run_text(
        statements_gap[0],
        statements_gap[0].text.replace("MRD v2.36", f"MRD v{MRD_VERSION}"),
    )
    append_bullet(T[41], M19_GAP_DIRECT_POST)

    replace_body_paragraph(doc, "Module 19 carries", M19_TRACE_LEAD)
    replace_body_paragraph(doc, "All 24 entries are traced above", M19_TRACE_CLOSE)

    prepend_change_log(T[43], M19_TARGET, M19_CHANGE_SUMMARY)
    finish(doc, output, title, M19_TARGET)


# ═════════════════════════════════════════════════════════════════════════════
# Module 20 - Adjustments & Concessions
# ═════════════════════════════════════════════════════════════════════════════

M20_DIR = "20-adjustments-and-concessions"
M20_STEM = "XVS_M20_Adjustments_and_Concessions_Functional_Requirements_Document"
M20_SOURCE, M20_TARGET = "1.3", "1.4"

M20_SCOPE_ELIGIBILITY = (
    "The student and guardian domains. A scholarship here is a concession with a "
    "kind, not an entitlement calculation."
)

M20_FR008_EVIDENCE_TAIL = (
    " A ladder that resolves with no steps is refused rather than posted: the post "
    "routes for refunds, write-offs, concessions and credit notes, and the direct "
    "invoice write-off, answer 409 APPROVAL_NOT_CONFIGURED unless the body carries "
    "confirm_without_approval with an optional reason, and a confirmed post writes "
    "POSTED_WITHOUT_APPROVAL to the platform audit trail against the person who "
    "confirmed; the submit route refuses and records the same way. An approval "
    "decision can be reversed in Module 7 only while the adjustment is a draft or "
    "pending approval: once it has posted the finance handler refuses with "
    "REVERSAL_NOT_ALLOWED, and undoing a rejection or a return puts it back to pending "
    "approval. A refund offers its method as a field a tenant's named Dynamic Role may "
    "route on, so refunds paid by different methods can go to different approvers."
)
M20_FR008_ACCEPTANCE_TAIL = (
    " No permission key confers approval of an adjustment; who approves is the stage's "
    "answer. StagelessTemplateSubmissionTests in vs_workflow proves the submit route "
    "refuses a ladder with no steps, writes nothing, and records whoever confirms; no "
    "finance test exercises the confirmation on the direct post routes. The reversal "
    "refusal is proved on the shared finance handler base by "
    "JournalApprovalWorkflowTests, and the refund method field by the Dynamic Role "
    "tests in vs_workflow."
)
M20_FR010_ACCEPTANCE = (
    "Below the threshold the direct post proceeds and the adjustment reaches the "
    "ledger; at or above it the post is refused and the submit route applies the "
    "adjustment approver group and then the senior one. Neither stage may auto-skip, so "
    "an unstaffed ladder parks the adjustment rather than posting it, and the submit "
    "response's approval block says nobody can yet approve it."
)
M20_FR011_ACCEPTANCE = (
    "The value a read reports and the outcome of posting that same document agree "
    "while the resolved ladder has steps: a row that reports no approval requirement "
    "posts, and one that reports a requirement is refused at the post endpoint and "
    "accepted at the submit endpoint. A ladder with no steps is the exception: its "
    "rows report no requirement, and their post still asks for a confirmation (FR-008)."
)
M20_FR011_LIMIT = (
    "It answers a structural question only: whether a stage would apply, not whether "
    "anybody can yet approve at that stage. A gated adjustment can therefore be "
    "submitted and then park, which is the designed outcome and is described by the "
    "approval block rather than by this field."
)
M20_LIFECYCLE = [
    ("PENDING", "APPROVED and postable, or rejected or returned to DRAFT. A reversed "
                "decision brings it back to PENDING."),
    ("POSTED", "VOID. The approval behind it can no longer be reversed."),
]
M20_API_LEAD = (
    "All routes are mounted under /v1/finance/ and require an asserted entity. The "
    "routes below are Module 20's. Every adjustment read carries a read-only approval "
    "requirement (FR-011) alongside the fields listed here. Every list and detail read "
    "narrows to the caller's branches, reading an adjustment with no branch as shared "
    "across the tenant, and a new adjustment takes its branch from the invoice or "
    "customer it adjusts rather than from whoever raises it, under the rule Module 19 "
    "records in FR-019 and FR-020."
)
M20_ERRORS = [
    ["Posting an adjustment whose resolved ladder has no steps, without confirm_without_approval",
     "409 APPROVAL_NOT_CONFIGURED. With the confirmation it posts, and the confirmation "
     "is recorded against the person who gave it."],
    ["Reversing the approval of an adjustment that has already posted",
     "422 REVERSAL_NOT_ALLOWED from the workflow engine. The correction is a void."],
]
M20_DEPENDENCIES = [
    ("Module 7", "Runs the refund and write-off approvals, and concession and "
                 "credit-note approvals at or above the threshold, where a template "
                 "exists for the document type. It refuses a ladder with no steps "
                 "unless the submitter confirms, asks this module before reversing a "
                 "decision, and lets a tenant's named Dynamic Role route a refund on "
                 "its method."),
    ("Module 4", "Supplies the keys each view enforces. No key confers approval."),
    ("Module 5", "Receives the adjustment history, and the POSTED_WITHOUT_APPROVAL "
                 "record of every adjustment posted on a confirmation."),
]
M20_CONTROL = [
    "CURRENT ADJUSTMENT CONTROL",
    "• Refunds and write-offs are gated by a published ladder at any size. A concession "
    "or credit note is gated at or above the configurable threshold, and below it posts "
    "on one person's permission, which is the intended trade for small goodwill.",
    "• A ladder with no steps is approval undecided, not approval-free. Every direct "
    "post route and the submit route refuse it with APPROVAL_NOT_CONFIGURED, and a "
    "confirmation lets the adjustment through only with the person's name recorded "
    "against it.",
    "• An approval decision cannot be withdrawn once the adjustment has posted; the "
    "correction is a void, which leaves both postings visible.",
    "• The threshold is one figure per tenant across both types, so a concession and a "
    "credit note cannot yet be gated at different sizes.",
]
M20_GAP_OPT_IN = (
    "• Approval is opt-in by template even for refunds and write-offs: where no "
    "template resolves at all, neither a tenant ladder nor a shared row, they post "
    "directly with no confirmation. Books created since ladders are published with "
    "them always resolve one."
)
M20_GAP_ELIGIBILITY = (
    "• Concession and refund eligibility is not connected to the student and guardian "
    "records, so who qualifies is decided outside the platform and recorded here as a "
    "decision. Tracked against Modules 11 and 20."
)
M20_GAP_READ_FIELD = (
    "• The approval requirement a read reports does not signal a ladder with no steps. "
    "Such a row reads as needing no approval, and its post is still refused until "
    "somebody confirms, so a client choosing between Post and Submit from that field "
    "alone meets a refusal it was not warned of."
)
M20_TRACE_LEAD = (
    f"Module 20 carries 14 capability entries in MRD v{MRD_VERSION}. Each maps to the "
    "requirements below."
)

M20_CHANGE_SUMMARY = (
    "Reconciled adjustments with the backend at 13675db. FR-008 records three changes "
    "to the approval path: a ladder with no steps is refused on every direct post and "
    "on submit with APPROVAL_NOT_CONFIGURED unless somebody confirms, and the "
    "confirmation is written to the audit trail under their name; an approval "
    "decision can be reversed only before the adjustment posts, after which the "
    "correction is a void; and a refund's method is a field a tenant's named Dynamic "
    "Role may route on. No key confers approval. Section 7 records the branch rule this "
    "module had never stated: adjustment reads narrow to the caller's branches and a "
    "new adjustment takes its branch from the invoice or customer it adjusts. FR-011's "
    "acceptance is corrected, because a row that reports no approval requirement is "
    "still asked for a confirmation when its ladder has no steps, and that becomes a "
    "further gap. The resolved-threshold box carried since v1.2 is replaced with the "
    "current control, FR-010 and FR-011 stop naming a role to fill, the scope table and "
    "further gaps stop calling the student domain future, and the typed errors gain "
    "the two new refusals. Traceability is reconciled to MRD v2.76 with the count "
    "unchanged at 14. Backend evidence only; nothing here is deployed."
)


def patch_m20(source: Path, output: Path) -> None:
    doc = Document(str(source))
    T = list(doc.tables)  # Bound before anything is inserted.
    title = (
        "XVS M20 Adjustments and Concessions Functional Requirements Document "
        f"v{M20_TARGET}"
    )

    replace_cover_version(T[0], M20_TARGET)
    update_control(doc, M20_TARGET, 20)

    set_cell_text(find_row(T[4], "Student-facing eligibility rules").cells[1], M20_SCOPE_ELIGIBILITY)

    fr008 = require_fr(T[15], "FR-008")
    append_to_cell(fr_cell(fr008, "Current evidence"), M20_FR008_EVIDENCE_TAIL)
    append_to_cell(fr_cell(fr008, "Acceptance"), M20_FR008_ACCEPTANCE_TAIL)

    set_cell_text(fr_cell(require_fr(T[16], "FR-010"), "Acceptance"), M20_FR010_ACCEPTANCE)

    fr011 = require_fr(T[18], "FR-011")
    set_cell_text(fr_cell(fr011, "Acceptance"), M20_FR011_ACCEPTANCE)
    set_cell_text(fr_cell(fr011, "Current limit"), M20_FR011_LIMIT)

    for state, leaves_to in M20_LIFECYCLE:
        set_cell_text(find_row(T[19], state).cells[2], leaves_to)

    replace_body_paragraph(doc, "All routes are mounted under /v1/finance/", M20_API_LEAD)
    for values in M20_ERRORS:
        append_row(T[24], values)
    for module, contract in M20_DEPENDENCIES:
        set_cell_text(find_row(T[25], module).cells[1], contract)

    rewrite_callout(T[26], M20_CONTROL)
    gaps = T[27].rows[0].cells[0].paragraphs
    if not gaps[1].text.startswith("• Approval is opt-in") or not gaps[5].text.startswith("• Concessions"):
        raise ValueError("Further-gaps bullets are not where expected")
    set_run_text(gaps[1], M20_GAP_OPT_IN)
    set_run_text(gaps[5], M20_GAP_ELIGIBILITY)
    append_bullet(T[27], M20_GAP_READ_FIELD)

    replace_body_paragraph(doc, "Module 20 carries", M20_TRACE_LEAD)
    set_cell_text(find_row(T[28], "Void and reversal controls").cells[1], "FR-007, FR-008")

    prepend_change_log(T[29], M20_TARGET, M20_CHANGE_SUMMARY)
    finish(doc, output, title, M20_TARGET)


# ═════════════════════════════════════════════════════════════════════════════

MODULES = {
    "M05": (M05_DIR, M05_STEM, M05_SOURCE, M05_TARGET, patch_m05),
    "M17": (M17_DIR, M17_STEM, M17_SOURCE, M17_TARGET, patch_m17),
    "M18": (M18_DIR, M18_STEM, M18_SOURCE, M18_TARGET, patch_m18),
    "M19": (M19_DIR, M19_STEM, M19_SOURCE, M19_TARGET, patch_m19),
    "M20": (M20_DIR, M20_STEM, M20_SOURCE, M20_TARGET, patch_m20),
}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", default=Path(__file__).resolve().parents[1])
    parser.add_argument("--only", choices=sorted(MODULES), action="append")
    args = parser.parse_args()
    root = Path(args.root) / "functional-requirements"

    for key in args.only or sorted(MODULES):
        folder, stem, source, target, patch = MODULES[key]
        directory = root / folder
        patch(
            directory / f"{stem}_v{source}.docx",
            directory / f"{stem}_v{target}.docx",
        )
        print(f"Wrote {folder} v{target}")


if __name__ == "__main__":
    main()

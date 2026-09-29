#!/usr/bin/env python3
"""Cut M10 v1.6: a module's own import key opens the wizard and nothing past it.

What changed in the backend (e02e9304), and therefore in the document:

* HasImportBatchRBACPermission let a dataset's own import key stand in for any
  engine key a view named, so a registrar holding school.students.import could
  roll back, delete, edit or resolve issues on a students batch and read the
  audit and notification feeds. The key now stands in only for the wizard keys
  (_WIZARD_KEYS), and any other route refuses it with 403 before the batch is
  looked up. Upload is unchanged. The school app offers Roll back only to the
  rollback key's holders and names no seven-day window (school-fe a9fbdde).

The same version carries the other queue entries owed to M10 (todo.md,
"Documents owed"): the staff dataset's own key and the Send Invitation column
(D4, 3189e0cb), the engine's fields read by role switch (D10, 4767f637), and
staff IDs compared without case on the staff import (D37, 447a6f0e), the
uploaded file and its rows following the role's field switches on every route
that serves them (D46, 2e446e27), and the bank-statement key rolling back its
own statement imports, finishing inside the request (D50, a8e0e4d0,
e1f42256). D3, D6 and D7 were checked and need
nothing in M10.

M04 was checked by this revision's writer: it does not document the dataset-key
stand-in, and its queue entries are written separately. The MRD is not touched.

    python tools/patch_dataset_import_key_docs.py
"""
from __future__ import annotations

from docx import Document

from patch_document_type_labels_docs import require_newest
from patch_record_history_docs import (
    ROOT,
    add_fr,
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
    edit_box,
    table_headed,
)
from patch_staff_id_and_auth_events_docs import (
    edit_cell,
    edit_paragraph,
    edit_value,
    fr_table,
    normalise_change_log,
    repair_ooxml,
    row,
    set_status,
)

import patch_record_history_docs

REVIEW_DATE = "28 September 2026"
CODE_BASELINE = (
    "Backend main at e1f42256, 28 September 2026, read for the changes named here, the "
    "school's own enrolment rules (ba33b90d) not yet among them. It carries e02e9304, where a module's own "
    "import key opens the wizard, case-blind staff IDs (447a6f0e), the file download and issue "
    "values held to the role's field switches (2e446e27), and the bank-statement key's rollback "
    "of its own statement imports (a8e0e4d0), which finishes inside the request whatever the "
    "statement's length (e1f42256). The school app's wizard offers Roll back to the "
    "rollback key's holders and, on a bank statement, to the statement key's holder (school-fe "
    "a9fbdde, a855385), committed, pending release"
)
MRD_VERSION = "2.93"

patch_record_history_docs.REVIEW_DATE = REVIEW_DATE

M10_DIR = "10-bulk-data-import"
M10_STEM = "XVS_M10_Bulk_Data_Import_Functional_Requirements_Document"
M10_SOURCE, M10_TARGET = "1.5", "1.6"

WIZARD_KEYS = (
    "import.batches.view, batches.create, batches.run, batches.import, validations.view "
    "and jobs.view"
)
PAST_THE_WIZARD = (
    "rollback, the rollback history, delete, edit, issue resolution and the audit and "
    "notification feeds"
)

FR018_HEADING = ("FR-018  A Module's Own Import Key Opens the Wizard, and Past It Only What Its "
                 "Dataset Declares")
FR018 = [
    ("Requirement", (
        "A registrar or a bursar must be able to take their own module's file through the "
        "wizard without holding the engine's keys, and that must not also hand them the "
        "actions that unwind, erase or administer an import already written, unless the owning "
        "module declares the one action its dataset cannot be corrected without."
    )),
    ("Current evidence", (
        "HasImportBatchRBACPermission first asks for the key the view names. Failing that, a "
        "module's own import key, registered by the owning app through "
        "register_dataset_import_key, stands in where the view names one of the wizard "
        f"keys: {WIZARD_KEYS}. That covers reading a batch, its file, its issues and their "
        "export, and its jobs; validating; starting the import; and cancelling, which names "
        "batches.create and itself confines a caller without batches.update or "
        "batches.delete to batches they uploaded. A registration may also declare extra engine "
        "keys past the wizard that its own key covers (_DATASET_EXTRA_ENGINE_KEYS), and a "
        "declared key counts only against a batch of that dataset. Every school dataset "
        "declares none. Bank statements declare import.rollbacks.run and nothing else, because "
        "finance refuses to edit a bulk-imported statement and corrects it by rolling the "
        "import back and importing the file again: a bursar holding finance.bankaccount.import "
        "rolls back her own statement import, and never a students batch, even holding the "
        "students key as well. Finance's own guard still decides the rollback, and refuses "
        "once any line of the statement has been matched, ignored or adjusted (FR-016). A view "
        "naming a key no dataset declares (batches.update, batches.delete, validations.update, "
        "rollbacks.view, audit.view or notifications.view) refuses the module key before the "
        "batch is looked up, so the answer is 403 whether or not the id exists; rollback by a "
        "key that does not cover it is refused 403 after the lookup, and a batch outside the "
        "caller's reach answers the same 403. Where the key does stand in, "
        "the batch is resolved against the asserted tenant and the caller's branches and "
        "must be of the key's own dataset, and a bank statement must also belong to the "
        "tenant's own books. The registered keys are school.students.import for students and "
        "guardians, school.staff.import for staff, academics.structure.import for academic "
        "structure and subjects, and finance.bankaccount.import for bank statements. The "
        "upload is not part of the stand-in: POST /batches/ has no batch yet to read a "
        "dataset from and asks for import.batches.create. A school corrects an import of its "
        "own datasets by uploading a fixed file. The school app matches: it offers an import "
        "only to a reader who can upload, check and load the file, and offers Roll back to a "
        "holder of import.rollbacks.run and, on a bank statement, to a holder of "
        "finance.bankaccount.import, naming no time limit because the server sets none "
        "(school-fe a9fbdde, a855385, committed, pending release)."
    )),
    ("Acceptance", (
        "A registrar holding only school.students.import validates, starts, reads, downloads "
        "and cancels her own students batch. She is refused 403 on rollback, delete, edit, "
        "issue resolution, the rollback history and the audit and notification feeds, and "
        "the job, the batch and the issue are left as they were; deleting an id that does not "
        "exist answers the same 403. A holder of import.rollbacks.run rolls back, with or "
        "without the dataset key. The same students key held at another school opens nothing "
        "of this school's, and that school's own rollback key answers 404 here. "
        "tests_dataset_key_actions holds all of it (15 tests), and "
        "ImportBatchModuleKeyBranchScopeTests the branch half. In "
        "tests_bank_statement_key_rollback, BankKeyRollsBackItsStatementTests proves a "
        "holder of finance.bankaccount.import alone rolls back a published statement, lines "
        "and all, and is still refused once a line has been acted on; "
        "BankKeyStopsAtTheRollbackTests that she is refused delete, the rollback history and "
        "the audit and notification feeds, and cannot roll back a students batch even holding "
        "the students key; BankKeyCrossTenantTests that the key held at another school rolls "
        "back nothing here; and RegisterDatasetImportKeyTests that the school datasets declare "
        "nothing past the wizard and bank statements the rollback alone."
    )),
    ("Current limit", (
        "No prebuilt school role carries import.rollbacks.run, so a school cannot unwind an "
        "import of its students, guardians, staff, structure or subjects itself, which is the "
        "intent. The rollback history stays closed to the statement key, and the bursar has no "
        "need of it: a statement rollback finishes inside the request, however many lines the "
        "statement has (FR-017). The stand-in opens routes, not fields: a school role holding "
        "a dataset key and no Field Access switches reads a batch without its preview and a "
        "run without its per-row detail, because only finance's key carries an owner rule "
        "(FR-019)."
    )),
]

FR019_HEADING = "FR-019  What a File Carried Is Read by Role Switch"
FR019 = [
    ("Requirement", (
        "What a spreadsheet said, and what the engine made of it, is somebody's personal "
        "details. Who reads it back must be a school's decision per role, and a field a role "
        "may not read must be absent rather than masked."
    )),
    ("Current evidence", (
        "field_access.py registers the engine's fields with the Field Access registry: a "
        "template's validation_rules, platform-scoped and sensitive; a job's and a row "
        "result's row_payload, normalized_payload, execution_summary, error_details, "
        "last_error_code and last_error_message, sensitive and not writable, because the "
        "engine produces them; and a batch's file and preview_rows, sensitive. The template, "
        "batch, job and row-result detail serializers are FieldAccessMixin surfaces, so a "
        "field the caller's role switches cannot read is left out of the response, with no "
        "placeholder and no list of what was withheld. The switches replaced the keys that "
        "used to gate these reads, and the conversion opened them on every role holding those "
        "keys, so nobody's access changed when they took over. A finance officer holding "
        "finance.bankaccount.import reads a statement batch's payloads through an owner rule, "
        "because the wizard has to show them the lines they have just uploaded. The routes that "
        "serve part of a batch without rendering its detail ask the detail serializer's own "
        "rule (FieldAccessMixin.can_read_field, through batch_field_readable), owner rule "
        "included, so they cannot drift from it. /batches/{id}/download/ asks it of the file "
        "and refuses 403 field_read_denied, naming the field, before the file is looked up, so "
        "the refusal says nothing about whether the batch holds one. The issue detail, the "
        "validate response and the issues CSV ask it of preview_rows: for a caller who may not "
        "read the rows, the issue detail leaves out raw_value, normalized_value and metadata, "
        "the validate response leaves out each issue's raw_value, and the CSV leaves out its "
        "Raw Value column, while the row, column, code and message of every issue stay, "
        "because they are what fixing the file needs. The school app's wizard shows no preview "
        "to a reader whose role may not read it (school-fe 35124d1, committed)."
    )),
    ("Acceptance", (
        "A caller whose role has every field of import.templates, import.jobs and "
        "import.batches switched off receives template, batch, job and row-result payloads, "
        "nested ones included, carrying none of those fields, in a school with two branches "
        "and in one with a single branch. ImportDeepPayloadTests walks each whole response. "
        "BatchRoutesFollowTheDetailsSwitchesTests proves the file downloads while its switch is "
        "on and is refused field_read_denied while it is off, that the detail and the download "
        "agree, that the refusal is the same whether or not a file is held, that the statement "
        "importer's owner rule opens the file as it opens the detail, that the rows switch "
        "does not decide the file, and that the issue detail, the issues export and a "
        "validation run quote cells only while the rows switch is on."
    )),
    ("Current limit", (
        "Only finance's key carries an owner rule; the school datasets' keys carry none, so a "
        "registrar reads her own upload's rows only where her role's switches allow it."
    )),
]

M10_SUMMARY = (
    "Minor revision. A module's own import key opens the wizard, and past it only what its "
    "dataset declares (backend e02e9304, a8e0e4d0). HasImportBatchRBACPermission had let "
    "school.students.import, school.staff.import, academics.structure.import and "
    "finance.bankaccount.import stand in for any engine key, so a registrar could roll back, "
    "delete, edit or resolve issues on her own dataset's batches and read the audit and "
    "notification feeds. The key now stands in for the six wizard keys, and every other route "
    "refuses it with 403. A registration may declare extra engine keys its key covers, counted "
    "only against a batch of its own dataset: the school datasets declare none, so a school "
    "corrects those imports by uploading a fixed file, and bank statements declare the "
    "rollback alone, so a bursar who imported the wrong statement rolls it back herself where "
    "finance's own guard allows and imports it again; that rollback is one delete and always "
    "finishes inside the request, never queued for its line count (e1f42256; FR-017, FR-018). "
    "New FR-018, with the actor table, 2.2, "
    "FR-011, FR-012, FR-014, 5.3, Section 7 and the dependencies following; the school app "
    "offers Roll back to the rollback key's holders and, on a bank statement, to the statement "
    "key's holder, and names no seven-day window (school-fe a9fbdde, a855385, committed, "
    "pending release). Also brought up to date: the staff dataset's own key and the staff "
    "template's Send Invitation column (3189e0cb; FR-002, FR-007); the engine's fields read by "
    "role switch (4767f637; new FR-019), with the file download refused 403 field_read_denied "
    "where the Uploaded file switch is off and the issue detail, the validate response and the "
    "issues CSV leaving out cell values where the Preview rows switch is off (2e446e27; "
    "FR-015, FR-019, Section 7); and staff IDs compared without case, in the database and in "
    "the file (447a6f0e; FR-007). 5.3 names a main branch where it said main site. "
    "Traceability reads MRD v2.93. Written from the code and the tests at e1f42256, not from a "
    "fresh run; nothing here claims deployment."
)


def actor_row(table, may_do_start: str):
    """The one actor row whose 'May do' cell starts with ``may_do_start``."""
    hits = [r for r in table.rows[1:] if r.cells[1].text.strip().startswith(may_do_start)]
    if len(hits) != 1:
        raise ValueError(f"{may_do_start!r} starts {len(hits)} actor rows")
    return hits[0]


def dependency_row(table, module: str):
    hits = [r for r in table.rows[1:] if r.cells[0].text.strip() == module]
    if len(hits) != 1:
        raise ValueError(f"{module!r} labels {len(hits)} dependency rows")
    return hits[0]


def patch_scope(doc) -> None:
    edit_paragraph(
        doc, "Two boundaries govern every request.",
        "The gate that lets a module's own import key stand in for the generic ones narrows "
        "the same way before it admits anybody.",
        "The gate that lets a module's own import key stand in for the engine's wizard keys "
        "narrows the same way before it admits anybody.",
    )
    edit_paragraph(
        doc, "Two boundaries govern every request.",
        "which is the same answer as a batch that does not exist.",
        "which is the same answer as a batch that does not exist. A caller the gate admits "
        "only through a module's own import key meets its 403 instead, given equally for an "
        "id that does not exist.",
    )


def patch_actors(doc) -> None:
    actors = table_headed(doc, "Actor", "May do", "Governed by")

    holder = actor_row(actors, "Load that module's dataset through the wizard")
    keep_format(holder.cells[1], (
        "Take that module's file through the wizard without the engine's keys: read the "
        "batch, its file, its issues and its jobs, validate it, start the import, and abandon "
        "it before it runs. Past the wizard, only what the dataset declares: a school corrects "
        "an import of its own datasets by uploading a fixed file, and a bursar rolls back a "
        "bank statement she imported in error."
    ))
    keep_format(holder.cells[2], (
        "school.students.import for students and guardians, school.staff.import for staff, "
        "academics.structure.import for academic structure and subjects, "
        "finance.bankaccount.import for bank statements. The key stands in only for "
        f"{WIZARD_KEYS}, and only on a batch of its own dataset in the caller's tenant and "
        f"branches. {PAST_THE_WIZARD[0].upper()}{PAST_THE_WIZARD[1:]} need the engine's own "
        "key, apart from rollback of a bank-statement batch, which finance.bankaccount.import "
        "also covers (FR-018)."
    ))

    correct = actor_row(actors, "Correct a bad file by uploading a corrected one")
    append_to_cell(correct.cells[2], " A module's own import key does not stand in for either.")

    support = actor_row(actors, "Roll a completed run back")
    edit_cell(support.cells[2],
              "Withheld from schools: a rollback unwinds data that is already live.",
              "No prebuilt school role carries either, and a school dataset's own import key "
              "does not stand in for them: a rollback unwinds data that is already live. "
              "finance.bankaccount.import rolls back a bank-statement batch and reads no "
              "rollback history (FR-018). The school app offers Roll back to a holder of "
              "import.rollbacks.run and, on a bank statement, to the statement key's holder.")

    feeds = actor_row(actors, "Read the import audit feed")
    append_to_cell(feeds.cells[2], " A module's own import key does not stand in for either.")


def append_to_cell(cell, tail: str) -> None:
    keep_format(cell, cell.text.rstrip() + tail)


def patch_requirements(doc) -> None:
    fr002 = fr_table(doc, "FR-002")
    edit_value(fr002, "Current evidence",
               "a renamed code leaves the old template answering beside the new one.",
               "a renamed code leaves the old template answering beside the new one. The "
               "staff template's Send Invitation column takes Yes or No; a blank or "
               "unrecognised answer counts as Yes, the unrecognised one with a warning. "
               "build.sh runs seed_import after every migrate, so a column added to a "
               "template reaches an environment when it is next built.")

    fr007 = fr_table(doc, "FR-007")
    edit_value(fr007, "Current evidence",
               "and a member of staff is created through the same user creation and "
               "invitation a single add uses.",
               "and a member of staff is created through the same user creation a single add "
               "uses and invited the way it invites, unless the row's Send Invitation says "
               "No: then the account and the staff record are created and the invitation is "
               "held, unsent, for the ordinary resend to send later, and the row result says "
               "the person was added rather than invited.")
    append_to(fr007, "Acceptance",
              " The staff import's SendInvitationColumnTests and "
              "TheEngineRunsTheWholeFileTests prove a file that invites one person and holds "
              "another back, with a result line that does not claim the held-back person was "
              "invited; its tests also refuse a staff ID already held in another case, and the "
              "second of two rows carrying one ID.")
    edit_value(fr007, "Current limit",
               "staff on email within the school, and a student number repeated in one file is "
               "refused.",
               "staff on email within the school and on staff ID compared without case, so "
               "BS/STF/0001 and bs/stf/0001 are one ID, as the school's constraint and the add "
               "form also hold. A staff ID repeated in one file, whatever its case, is refused "
               "on the later row under the Staff ID column, and a student number repeated in "
               "one file is refused.")

    fr015 = fr_table(doc, "FR-015")
    edit_value(fr015, "Current evidence",
               "The batch file download route is gated on import.batches.view and resolves",
               "The batch file download route is gated on import.batches.view and on the "
               "batch's Uploaded file switch (FR-019), and resolves")
    append_to(fr015, "Acceptance",
              " A caller whose role has the Uploaded file switch off is refused 403 "
              "field_read_denied, whether or not the batch holds a file.")

    fr017 = fr_table(doc, "FR-017")
    append_to(fr017, "Current evidence",
              " A bank-statement rollback is the exception (rolls_back_in_one_step, e1f42256): it "
              "is one delete of the statement and its lines rather than a reversal per row, so "
              "its line count says nothing about its cost, and the endpoint never queues it "
              "unless the caller asks. The bursar is answered once the statement is gone, "
              "without needing the rollback history her key cannot read.")
    append_to(fr017, "Acceptance",
              " A sixty-line statement rolls back inside the request "
              "(test_a_long_statement_rolls_back_in_the_request).")

    fr011 = fr_table(doc, "FR-011")
    edit_value(fr011, "Current evidence",
               "The gate that lets a module's own import key stand in for the generic ones "
               "narrows by branch as well,",
               "The gate that lets a module's own import key stand in for the engine's wizard "
               "keys narrows by branch as well,")

    fr012 = fr_table(doc, "FR-012")
    edit_value(fr012, "Current evidence",
               "Deletion is separately gated and refuses outright",
               "Deletion is separately gated behind import.batches.delete, which a module's "
               "own import key does not stand in for, and refuses outright")

    fr014 = fr_table(doc, "FR-014")
    edit_value(fr014, "Current limit",
               "their feed requires a platform key,",
               "their feed requires import.notifications.view, which no prebuilt school role "
               "carries and a module's own import key does not stand in for,")

    add_fr(doc, FR018_HEADING, "FR-018 | Implemented", FR018)
    add_fr(doc, FR019_HEADING, "FR-019 | Implemented", FR019)


def patch_workflow_and_api(doc) -> None:
    edit_paragraph(
        doc, "Rollback is available for a job that succeeded",
        "the keys are withheld from every school role, because by the time a rollback is "
        "asked for, the records are live.",
        "no prebuilt school role carries the keys, and a school dataset's own import key does "
        "not stand in for them, because by the time a rollback is asked for, the records are "
        "live. Bank statements are the exception: finance.bankaccount.import rolls back a "
        "statement batch, because finance corrects a bulk-imported statement only by rolling "
        "it back and importing it again, and finance's own rollback refuses once any line has "
        "been acted on (FR-016, FR-018). The school app offers Roll back to a holder of "
        "import.rollbacks.run and, on a bank statement, to the statement key's holder, and "
        "names no deadline, because the server sets none.",
    )
    edit_paragraph(doc, "Rollback is available for a job that succeeded",
                   "if it is not the school's main site", "if it is not the school's main branch")
    edit_paragraph(
        doc, "Refusals are consistent across the surface",
        "a missing key answers 403, and a request that breaks a lifecycle rule answers 400 "
        "with the rule stated.",
        "a missing key answers 403, and a request that breaks a lifecycle rule answers 400 "
        "with the rule stated. A caller holding only a module's own import key is refused "
        f"403 on {PAST_THE_WIZARD}, the same whether or not the batch exists; the one "
        "exception is rollback of a bank-statement batch by finance.bankaccount.import "
        "(FR-018). A registered field the caller's roles cannot read is absent from the "
        "response rather than masked, and a route whose whole answer is that field, the file "
        "download, is refused 403 field_read_denied (FR-019).",
    )
    routes = table_headed(doc, "Route", "Purpose", "Key")
    for route, cell, old, new in (
        ("GET /batches/{id}/download/", 1, "Download the uploaded file.",
         "Download the uploaded file, or 403 field_read_denied where the Uploaded file switch "
         "keeps it from the caller (FR-019)."),
        ("POST /batches/{id}/validate/", 1, "Run or re-run validation.",
         "Run or re-run validation. Each issue quotes its cell only to a reader of the rows "
         "(FR-019)."),
        ("GET /batches/{id}/issues/export/", 1, "Download the findings as a file.",
         "Download the findings as a file, without the Raw Value column for a caller who may "
         "not read the rows (FR-019)."),
        ("GET /batches/{id}/issues/{id}/", 1, "Read one finding.",
         "Read one finding, with the cells it quotes only to a reader of the rows (FR-019)."),
        ("POST /batches/{id}/jobs/{id}/rollback/", 2, "rollbacks.run",
         "rollbacks.run, or finance.bankaccount.import on a bank-statement batch"),
    ):
        target = row(routes, route).cells[cell]
        if target.text.strip() != old:
            raise ValueError(f"{route!r} reads {target.text!r}, not {old!r}")
        keep_format(target, new)


def patch_dependencies(doc) -> None:
    deps = table_headed(doc, "Module", "How this engine depends on it")
    edit_cell(dependency_row(deps, "Module 4, Roles & Permissions").cells[-1],
              "Holds every key this module names, and the restricted classification on "
              "templates.update, batches.delete and batches.import.",
              "Holds every key this module names, the restricted classification on "
              "templates.update, batches.delete, batches.import and rollbacks.run, the "
              "Field Access registry and role switches FR-019 reads, and the rule "
              "(FieldAccessMixin.can_read_field) and 403 field_read_denied refusal the file "
              "download asks.")
    edit_cell(dependency_row(deps, "Module 12, Staff Management").cells[-1],
              "It registers no key of its own, so the generic import keys govern it.",
              "It registers school.staff.import, which stands in for the engine's wizard keys "
              "on a staff batch, and owns the Send Invitation choice that holds an invitation "
              "back.")
    append_to_cell(dependency_row(deps, "Module 19, Finance & Accounting").cells[-1],
                   " Its key, finance.bankaccount.import, stands in for the wizard keys on a "
                   "statement batch and, because finance corrects a bulk-imported statement "
                   "only by rolling it back, for rollback of a statement batch as well, and "
                   "not for delete. Each statement in a bank account's list names the batch "
                   "and job that roll it back.")
    edit_paragraph(
        doc, "Operational evidence:",
        "This revision was written from the code",
        "The dataset-key boundary is held by tests_dataset_key_actions, which takes a "
        "students batch through the wizard on the students key alone and refuses every action "
        "past it, and tests_bank_statement_key_rollback, which lets the statement key roll "
        "back its own statement and nothing else; the engine's field switches by "
        "ImportDeepPayloadTests, and the download and issue routes that follow them by "
        "BatchRoutesFollowTheDetailsSwitchesTests. This revision was written from the code",
    )


def patch_attention(doc) -> None:
    hits =[t for t in doc.tables if t.rows[0].cells[0].text.strip().startswith("NEEDS ATTENTION")]
    if len(hits) != 1:
        raise ValueError(f"NEEDS ATTENTION heads {len(hits)} tables")
    box = hits[0].rows[0].cells[0]
    edit_box(box, [
        ("sub", "• Rollback reverses what CodeX imports and calendar entries",
         "and calendar entries, and nothing else.",
         "and calendar entries, and a bank statement through finance (FR-016), and nothing "
         "else."),
        ("sub", "• The module's own import notifications are not dispatched",
         "and their feed requires a platform key,",
         "and their feed requires import.notifications.view, which no prebuilt school role "
         "carries and a module's own import key does not stand in for,"),
    ])


def patch_traceability(doc) -> None:
    set_control(doc, "Source MRD", f"XVS Module Requirements Document v{MRD_VERSION} | Module 10")
    edit_paragraph(doc, "Module 10 of XVS Module Requirements Document",
                   "Document v2.80 lists", f"Document v{MRD_VERSION} lists")
    trace = table_headed(doc, "MRD capability", "FRD requirement", "State")
    for capability, old, new in (
        ("Batch detail, deletion, and cancellation", "FR-012", "FR-012, FR-018"),
        ("Source-file download", "FR-015", "FR-015, FR-019"),
        ("Issue resolution and issue export", "FR-004, FR-005", "FR-004, FR-005, FR-018"),
        ("Import-job status tracking", "FR-006", "FR-006, FR-019"),
        ("Batch rollback", "FR-008, FR-009, FR-010, FR-016",
         "FR-008, FR-009, FR-010, FR-016, FR-018"),
    ):
        cell = row(trace, capability).cells[1]
        if cell.text.strip() != old:
            raise ValueError(f"{capability!r} maps to {cell.text!r}, not {old!r}")
        keep_format(cell, new)
    edit_paragraph(
        doc, "The MRD carries five needs-attention items for Module 10",
        doc_text_between(doc, "The MRD carries five needs-attention items for Module 10"),
        "The MRD carries six needs-attention items for Module 10. Five are stated in Section "
        "9: the datasets a school still cannot bring, rollback that cannot reverse a school's "
        "own datasets, notifications that do not reach Module 8, a run that cannot be "
        "stopped, and an unbounded file size. The sixth, that reaching this engine is a plan "
        "question as well as a role one, describes settled behaviour that FR-001 records: "
        "bulk import sits at Core and every plan reaches it. Section 9 adds three the MRD does "
        "not carry, because they are narrower than the roadmap tracks: a validation run in "
        "flight is not interrupted by cancelling its batch, the two rollback paths report in "
        "different shapes, and an archived batch keeps its file readable. Each is a boundary "
        "of a capability the module delivers.",
    )


def doc_text_between(doc, start: str) -> str:
    hits = [p for p in doc.paragraphs if p.text.strip().startswith(start)]
    if len(hits) != 1:
        raise ValueError(f"{start!r} starts {len(hits)} paragraphs")
    return hits[0].text


def keep_dependency_table_opening_together(doc) -> None:
    """Section 8's header row and first two dependencies stay on one page.

    Without it the heading, the header row and the first dependency can end a
    page on their own while the rest of the table starts the next.
    """
    deps = table_headed(doc, "Module", "How this engine depends on it")
    for table_row in deps.rows[:2]:
        for cell in table_row.cells:
            for paragraph in cell.paragraphs:
                paragraph.paragraph_format.keep_with_next = True


def patch_m10() -> None:
    require_newest(str(ROOT / "functional-requirements" / M10_DIR / f"{M10_STEM}_v*.docx"),
                   M10_SOURCE)
    doc = Document(str(frd_path(M10_DIR, M10_STEM, M10_SOURCE)))
    set_cover_version(doc, M10_SOURCE, M10_TARGET)
    set_control(doc, "Version", M10_TARGET)
    set_control(doc, "Review date", REVIEW_DATE)
    set_control(doc, "Code baseline", CODE_BASELINE)

    patch_scope(doc)
    patch_actors(doc)
    patch_requirements(doc)
    patch_workflow_and_api(doc)
    patch_dependencies(doc)
    patch_attention(doc)
    patch_traceability(doc)

    keep_dependency_table_opening_together(doc)
    log_change(doc, M10_TARGET, M10_SUMMARY)
    assert_absent_outside_log(
        doc,
        "stand in for the generic ones",
        "registers no key of its own",
        "requires a platform key",
        "withheld from every school role",
        "Withheld from schools",
        "v2.80",
        "Version: 1.5",
        "no self-service way back",
        "hides a batch's file from its detail but not from its",
        "main site",
        "Nothing past the wizard",
    )
    repair_ooxml(doc)
    normalise_change_log(doc)
    finish(doc, frd_path(M10_DIR, M10_STEM, M10_TARGET),
           f"{M10_STEM.replace('_', ' ')} v{M10_TARGET}", M10_TARGET)


def main() -> None:
    patch_m10()


if __name__ == "__main__":
    main()

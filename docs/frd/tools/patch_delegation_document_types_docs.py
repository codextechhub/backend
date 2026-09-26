#!/usr/bin/env python3
"""Cut M07 FRD v1.18: a delegation's document type is chosen from the tenant's own types.

What changed in the backend, and therefore in the document:

* GET /workflow/delegations/document-types/ lists the document types the
  tenant raises, each with its human name, ordered by that name.
* ApprovalDelegationSerializer.validate refuses a document_type that is neither
  blank nor one the tenant raises, with 400 under document_type. Blank still
  covers every type, and the value is trimmed. The engine matches the type
  exactly, so a mistyped code saved a delegation that never applied, and a
  school could save a platform-only type.

The MRD was checked and not revised: it records approval delegation as a
capability and delegations as carrying a document type label, and neither
statement changes. M07 carries no Needs Attention item about the delegation
type being free text, so none is removed.

Starts from the newest M07 in its folder and refuses to run if a newer one has
appeared; ``finish`` refuses to overwrite an existing version.

    python tools/patch_delegation_document_types_docs.py
"""
from __future__ import annotations

from docx import Document

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
    insert_row_after,
    row_labelled,
)
from patch_staff_id_and_auth_events_docs import (
    fr_table,
    normalise_change_log,
    repair_ooxml,
)

import patch_record_history_docs

REVIEW_DATE = "26 September 2026"
MRD_BASELINE = "2.91"
CODE_BASELINE = (
    "Backend main at 65a31144 with the delegation document type check in its working tree, "
    "26 September 2026. The FinPro delegations screen's integration is implemented and "
    "pending release"
)
TEST_EVIDENCE = (
    "Verified by the vs_workflow suite without its slow-tagged tests (429 tests, all "
    "passing), five of them DelegationDocumentTypeTests. The FinPro delegations screen's "
    "integration is implemented and pending release; nothing here claims deployment."
)

# log_change writes the module's own review date, read from this module.
patch_record_history_docs.REVIEW_DATE = REVIEW_DATE

M07_DIR = "07-workflow-and-approval-engine"
M07_STEM = "XVS_M07_Workflow_and_Approval_Engine_Functional_Requirements_Document"
M07_SOURCE, M07_TARGET = "1.17", "1.18"

REFUSAL = "Choose a document type from the list, or leave it as all types."

M07_SUMMARY = (
    "Minor revision. A delegation narrowed to one document type must name a type the tenant "
    "raises. GET /delegations/document-types/ lists those types by name, ordered by name, "
    "and the delegation serializer trims the value and refuses anything else with 400 under "
    "document_type; blank still covers every type. The engine matches the type exactly, so "
    "a mistyped code such as leave.requests had saved a delegation that applied to nothing, "
    "and a school could save a platform-only type. FR-013, the endpoint gates and the "
    "delegation contracts are updated. The MRD was checked and needs no new version. "
    + TEST_EVIDENCE
)


def patch_m07() -> None:
    require_newest(str(ROOT / "functional-requirements" / M07_DIR / f"{M07_STEM}_v*.docx"),
                   M07_SOURCE)
    doc = Document(str(frd_path(M07_DIR, M07_STEM, M07_SOURCE)))
    set_cover_version(doc, M07_SOURCE, M07_TARGET)
    set_control(doc, "Version", M07_TARGET)
    set_control(doc, "Review date", REVIEW_DATE)
    set_control(doc, "Code baseline", CODE_BASELINE)
    set_control(doc, "Source MRD", f"XVS Module Requirements Document v{MRD_BASELINE}")

    fr013 = fr_table(doc, "FR-013")
    append_to(fr013, "Requirement",
              " A delegation narrowed to one document type names a type the tenant actually "
              "raises, chosen from a list rather than typed.")
    append_to(fr013, "Current evidence",
              " GET /delegations/document-types/ lists the types the tenant raises "
              "(handlers_raised_by), each as a value and its document_type_label, ordered by "
              "label. ApprovalDelegationSerializer.validate trims document_type and refuses "
              "one that is neither blank nor among those types, with 400 under document_type: "
              f"\"{REFUSAL}\" Blank still covers every type. The engine matches the type "
              "exactly, so a mistyped code such as leave.requests would save a delegation that "
              "never applied, and a school could save a type only the platform raises.")
    append_to(fr013, "Acceptance",
              " DelegationDocumentTypeTests: the list is named and sorted and leaves out "
              "platform-only types for a school; a listed type saves; a mistyped code is "
              "refused and nothing is saved; a platform-only type is refused to a school; and "
              "blank still means every type.")
    append_to(fr013, "Current limit",
              " A delegation saved before the check with a type the tenant does not raise is "
              "left as it was and still applies to nothing; nothing reports or sweeps such "
              "rows.")

    gate = row_labelled(doc, "Delegations").cells[-1]
    keep_format(gate, gate.text.rstrip() + "; the document types a delegation may cover are "
                "listed to any member")

    create = row_labelled(doc, "GET, POST /delegations/")
    keep_format(create.cells[-1], create.cells[-1].text.rstrip() + (
        " A document_type that is neither blank nor a type the tenant raises is refused with "
        "400 under document_type."))
    insert_row_after(create, [
        "GET /delegations/document-types/",
        "The document types a delegation may be narrowed to: those the tenant raises, each as "
        "value and label, ordered by label. Open to any signed-in active member of the tenant.",
    ])

    log_change(doc, M07_TARGET, M07_SUMMARY)
    assert_absent_outside_log(doc, "MRD v2.89")
    repair_ooxml(doc)
    normalise_change_log(doc)
    finish(doc, frd_path(M07_DIR, M07_STEM, M07_TARGET),
           f"{M07_STEM.replace('_', ' ')} v{M07_TARGET}", M07_TARGET)


def main() -> None:
    patch_m07()


if __name__ == "__main__":
    main()

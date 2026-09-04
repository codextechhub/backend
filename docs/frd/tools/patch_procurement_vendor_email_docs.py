#!/usr/bin/env python3
"""Version the MRD and Modules 8, 22, and 23 FRDs for vendor email changes."""

from __future__ import annotations

import argparse
import copy
from pathlib import Path

from docx import Document

from generate_requirements_documents import (
    BLUE,
    assert_no_em_dash,
    rebuild_table,
    shrink_inherited_media,
    update_extended_title,
    write_cell,
    write_paragraph,
)


REVIEW_DATE = "4 September 2026"
SHORT_DATE = "4 Sep 2026"
MRD_SOURCE_VERSION = "2.62"
MRD_TARGET_VERSION = "2.63"
NOTIFICATION_TESTS = 146
PROCUREMENT_TESTS = 506


def replace_cell(cell, text: str, **kwargs) -> None:
    while len(cell.paragraphs) > 1:
        paragraph = cell.paragraphs[-1]
        paragraph._p.getparent().remove(paragraph._p)
    write_cell(cell, text, **kwargs)


def replace_paragraph(paragraph, text: str, **kwargs) -> None:
    write_paragraph(paragraph, text, **kwargs)


def replace_cover_versions(table, versions: tuple[str, ...], target: str) -> None:
    for paragraph in table.rows[0].cells[0].paragraphs:
        for run in paragraph.runs:
            for source in versions:
                if source in run.text:
                    run.text = run.text.replace(source, target)


def replace_control_value(table, label: str, value: str) -> None:
    for row in table.rows:
        if row.cells[0].text.strip() == label:
            replace_cell(row.cells[1], value, size=9)
            return
    raise ValueError(f"Control row not found: {label}")


def prepend_change_log(table, version: str, summary: str) -> None:
    template = table.rows[1]
    new_tr = copy.deepcopy(template._tr)
    template._tr.addprevious(new_tr)
    row = table.rows[1]
    replace_cell(row.cells[0], version, size=8)
    replace_cell(row.cells[1], SHORT_DATE, size=8)
    replace_cell(row.cells[2], summary, size=8)


def append_styled_row(table, values: list[str], *, size: float = 8.2) -> None:
    template = table.rows[-1]
    new_tr = copy.deepcopy(template._tr)
    template._tr.addnext(new_tr)
    row = table.rows[-1]
    for cell, value in zip(row.cells, values):
        replace_cell(cell, value, size=size)


def finish(doc, output: Path, title: str) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    doc.save(str(output))
    update_extended_title(output, title)
    shrink_inherited_media(output)
    assert_no_em_dash(output)


def test_evidence() -> str:
    return (
        f"Verified by {NOTIFICATION_TESTS} notification tests and "
        f"{PROCUREMENT_TESTS} procurement tests."
    )


def patch_mrd(source: Path, output: Path) -> None:
    doc = Document(str(source))
    title = f"XVS Module Requirements Document v{MRD_TARGET_VERSION}"
    doc.core_properties.title = title
    doc.core_properties.version = MRD_TARGET_VERSION
    replace_cover_versions(doc.tables[0], (MRD_SOURCE_VERSION,), MRD_TARGET_VERSION)
    replace_control_value(doc.tables[1], "Version", MRD_TARGET_VERSION)
    replace_control_value(doc.tables[1], "Review date", REVIEW_DATE)
    replace_control_value(
        doc.tables[1],
        "Source scope",
        "Backend change refining seven procurement vendor emails across Modules 8, 22, and 23. "
        "The family now gives vendors structured request, deadline, submission, and purchase-order "
        "details; distinguishes a receipt from an award; and separates amendments that require a new "
        "response from information-only changes. Signed RFQ links and verification codes are now "
        "stored as inert markers and substituted only by the delivery worker. Migration 0017 refreshes "
        "only platform-maintained templates. " + test_evidence() +
        " Backend evidence only; nothing here is deployed.",
    )

    replace_cell(
        doc.tables[2].rows[5].cells[0],
        f"5. v{MRD_TARGET_VERSION} Capability Delta",
        size=9,
        bold=True,
        color=BLUE,
    )
    replace_cell(
        doc.tables[2].rows[5].cells[1],
        "Clearer vendor mail with delivery-only RFQ secrets",
        size=9,
    )

    for paragraph in doc.paragraphs:
        text = paragraph.text.strip()
        if text.startswith("Event-driven in-app and email delivery"):
            replace_paragraph(
                paragraph,
                "Event-driven in-app and email delivery. A record is owned by the tenant of the "
                "recipient who reads it, and the tenant an event is about is kept separately as its "
                "origin. Transactional operational alarms bypass delivery preferences. Standard "
                "emails resolve issuer, entity, or tenant branding and use one polished, email-client-safe "
                "layout. Invitation, password-reset, onboarding, and all seven procurement vendor "
                "messages use purpose-specific copy. Purchase orders identify their attached PDF and "
                "buyer contact; RFQ mail identifies the request, deadline, action, and outcome. Signed "
                "RFQ links and verification codes, like account invitation and reset credentials, remain "
                "inert in notification history and are replaced only in the delivery worker. Migrations "
                "refresh platform-maintained markup while preserving every staff-authored override. This "
                "is a notification engine, not a person-to-person chat product.",
                size=9,
                space_after=5,
            )
        elif text.startswith("Requisition, budget-check, sourcing"):
            replace_paragraph(
                paragraph,
                "Requisition, budget-check, sourcing, quotation, award, and approval flows are "
                "implemented. Five purpose-specific vendor messages cover RFQ invitation, verification, "
                "reminder, amendment or deadline changes, and the submitted quotation receipt. The "
                "signed link and one-time code are absent from durable notification history.",
                size=9,
                space_after=5,
            )
        elif text.startswith("The purchase-to-pay chain is implemented"):
            replace_paragraph(
                paragraph,
                "The purchase-to-pay chain is implemented from purchase order through receipt, matching, "
                "vendor invoice, payment, and accounting integration. An emailed purchase order now "
                "presents structured order, delivery, payment, attachment, note, and buyer-contact details.",
                size=9,
                space_after=5,
            )
        elif text == f"5. v{MRD_SOURCE_VERSION} Capability Delta":
            replace_paragraph(
                paragraph,
                f"5. v{MRD_TARGET_VERSION} Capability Delta",
                size=17,
                bold=True,
                space_before=15,
                space_after=8,
            )
        elif text.startswith("This revision improves the existing onboarding notification"):
            replace_paragraph(
                paragraph,
                "This revision improves the existing notification, supplier invitation, quotation "
                "capture, and purchase-order delivery capabilities shared by Modules 8, 22, and 23. It "
                "adds no route, permission, event, or capability entry. Seven vendor emails now present "
                "the real deadline, action, attachment, or outcome, and the two RFQ credentials no longer "
                "enter durable history.",
                size=9,
                space_after=5,
            )

    rebuild_table(
        doc.tables[76],
        [f"v{MRD_TARGET_VERSION} capability delta", "Decision", "Evidence"],
        [
            [
                "Seven vendor messages",
                "Purpose-specific",
                "Purchase-order delivery and the RFQ invitation, code, reminder, receipt, amendment, "
                "and extension each show the details and action their recipient needs.",
            ],
            [
                "RFQ credentials",
                "Delivery only",
                "The signed invitation token and verification code render as inert markers in stored "
                "history and are replaced with their raw values only immediately before email delivery.",
            ],
            [
                "Vendor outcomes",
                "Made explicit",
                "The quotation receipt says it is neither acceptance nor award. Amendment mail branches "
                "between required resubmission and an information-only review.",
            ],
            [
                "Existing customized templates",
                "Preserved",
                "Migration 0017 changes only html_is_custom=false rows and keeps every staff-authored "
                "subject, message, action, and HTML document.",
            ],
        ],
        [1.75, 0.95, 4.57],
        font_size=8.2,
    )
    prepend_change_log(
        doc.tables[78],
        MRD_TARGET_VERSION,
        "Refined all seven procurement vendor emails across Modules 8, 22, and 23. Purchase-order mail "
        "now structures the order, delivery, payment, attachment, buyer note, and contact details. RFQ "
        "invitation and reminder mail names the request, deadline, and submit-or-decline action. The "
        "verification message keeps its code out of the subject and explains the ten-minute limit. The "
        "quotation receipt explicitly says receipt is not acceptance or award. Amendment mail separates "
        "required resubmission from an information-only change, and deadline-extension mail confirms the "
        "existing secure link remains valid. Signed RFQ links and verification codes are now stored only "
        "as inert markers and replaced immediately before delivery. Migration 0017 updates only standard "
        "rows. Module states, ownership, capability counts, priority gaps, and build order do not change. "
        + test_evidence() + " Backend evidence only; nothing here is deployed.",
    )
    finish(doc, output, title)


def patch_m08(source: Path, output: Path) -> None:
    source_version, target_version = "1.8", "1.9"
    doc = Document(str(source))
    title = f"XVS M08 Notifications and Delivery Functional Requirements Document v{target_version}"
    doc.core_properties.title = title
    doc.core_properties.version = target_version
    replace_cover_versions(doc.tables[0], (source_version,), target_version)
    replace_control_value(doc.tables[1], "Version", target_version)
    replace_control_value(doc.tables[1], "Review date", REVIEW_DATE)
    replace_control_value(
        doc.tables[1],
        "Code baseline",
        "Commit e881a6b plus the procurement vendor email refinements, delivery-only RFQ secrets, "
        "migration 0017, and passing Module 8 and procurement suites under review (4 September 2026)",
    )
    replace_control_value(
        doc.tables[1],
        "Source MRD",
        f"XVS Module Requirements Document v{MRD_TARGET_VERSION} | Module 8",
    )
    replace_control_value(
        doc.tables[1],
        "Supporting apps",
        "vs_tenants, vs_rbac, vs_user, vs_config, vs_health, vs_procurement, schools/vs_onboarding, core",
    )

    for paragraph in doc.paragraphs:
        text = paragraph.text.strip()
        if text.startswith("Module 8 is the platform's one way"):
            replace_paragraph(
                paragraph,
                "Module 8 is the platform's one way of telling somebody something happened. A domain "
                "module raises a named event with a context; this module decides which channels may carry "
                "it, renders the message, writes the record, queues the email, and keeps the outcome. "
                "Standard email uses one tenant-aware layout. Invitation, password-reset, onboarding, and "
                "all seven procurement vendor messages now use purpose-specific copy. The procurement "
                "family covers an attached purchase order and the RFQ invitation, verification code, "
                "reminder, submission receipt, amendment, and deadline extension. The signed RFQ link and "
                "verification code are deliberate one-time-secret exceptions: history stores inert markers "
                "and only the delivery worker sees the raw values.",
                size=9,
                space_after=5,
            )
        elif text.startswith("Module 8 carries 22 capability entries"):
            replace_paragraph(
                paragraph,
                f"Module 8 carries 22 capability entries in MRD v{MRD_TARGET_VERSION}. Each maps to the "
                "requirements below. Refining the seven procurement templates and protecting their two "
                "RFQ credentials strengthens the existing template, preview, secret-safe rendering, and "
                "delivery capabilities without changing the count.",
                size=9,
                space_after=5,
            )

    fr005 = doc.tables[11]
    replace_cell(
        fr005.rows[2].cells[1],
        "The composed markup is stored on the template with placeholders intact and dispatch renders that "
        "column. Ordinary context values are persisted in rendered form. Account invitation and reset "
        "tokens, signed vendor RFQ links, and vendor verification codes use inert markers in the rendered "
        "Notification row. The delivery worker receives their raw values separately and substitutes them "
        "in memory immediately before SMTP. Standard HTML removes a duplicated visible action URL when a "
        "button and fallback link already carry it.",
        size=8.5,
    )
    replace_cell(
        fr005.rows[3].cells[1],
        "Every active email template holds its normal placeholders. Editing stored markup changes the next "
        "mail. Tests prove each one-time value is absent from stored subject, body, HTML, and metadata while "
        "SMTP receives the working value. The procurement integration test proves both the signed RFQ token "
        "and ten-minute verification code follow that delivery-only path.",
        size=8.5,
    )
    replace_cell(
        fr005.rows[4].cells[1],
        "Stored and delivered strings deliberately differ at one-time credential markers. This is the "
        "security exception to history matching mail byte for byte; all non-secret rendered content remains "
        "the same. In-app templates hold no markup.",
        size=8.5,
    )

    fr006 = doc.tables[12]
    replace_cell(
        fr006.rows[2].cells[1],
        "A template carries a flag saying who maintains its markup. While it is false the markup is "
        "regenerated from the shared layout on every save; the first hand edit claims it, and clearing the "
        "flag restores the standard design. Standard markup carries domain-neutral brand placeholders. "
        "Migrations 0012 through 0016 refreshed the shared design and earlier email families. Migration "
        "0017 refines all seven standard procurement vendor email rows. Every migration filters "
        "html_is_custom=false.",
        size=8.5,
    )
    replace_cell(
        fr006.rows[3].cells[1],
        "Changing a standard template changes its markup; changing a hand-edited one does not. Saving "
        "regenerated standard markup is not treated as a hand edit. Every design migration changes only "
        "html_is_custom=false. A customized account, onboarding, or procurement vendor template keeps its "
        "subject, body, action, and HTML verbatim.",
        size=8.5,
    )

    fr008 = doc.tables[14]
    replace_cell(
        fr008.rows[2].cells[1],
        "Preview renders through the same path dispatch uses and needs no payload: sample values are "
        "generated from the template's placeholders. Exact procurement samples cover order and quotation "
        "numbers, request title, deadline, submission time, delivery and payment terms, buyer details, the "
        "verification lifetime, amendment version and response requirement. Preview accepts unsaved editor "
        "content and returns the markup alongside the rendered copy.",
        size=8.5,
    )
    replace_cell(
        fr008.rows[3].cells[1],
        "A preview of any active template returns a complete HTML document and writes nothing. Supplying "
        "context overrides generated samples for those keys only. Every standard procurement vendor "
        "template renders without unresolved variables, including both amendment branches and an optional "
        "purchase-order note or buyer email.",
        size=8.5,
    )

    append_styled_row(
        doc.tables[28],
        [
            "Modules 22 and 23, Procurement",
            "Raise seven transactional vendor emails. RFQ callers pass signed links and verification "
            "codes as delivery-only replacements; purchase-order delivery supplies the attached PDF and "
            "order details. Existing context keys remain available to customized templates.",
        ],
    )

    prepend_change_log(
        doc.tables[33],
        target_version,
        "Refines all seven standard procurement vendor templates and closes a credential leak in delivery "
        "history. Purchase-order mail identifies its PDF, key terms, optional buyer note, and contact. RFQ "
        "mail names the request, deadline, available action, submission revision, amendment requirement, or "
        "new deadline. The quotation receipt explicitly separates receipt from acceptance or award. Signed "
        "RFQ links and verification codes now enter Notification history only as inert markers and are "
        "substituted by the worker immediately before SMTP. Migration 0017 updates only platform-maintained "
        "rows and preserves staff-authored content. Updates scope, FR-005, FR-006, FR-008, procurement "
        "dependencies, and MRD traceability. Module 8 remains Backend Complete and In use Complete with 22 "
        "capabilities. " + test_evidence() + " Backend evidence only; nothing here is deployed.",
    )
    finish(doc, output, title)


def patch_m22(source: Path, output: Path) -> None:
    source_version, target_version = "1.4", "1.5"
    doc = Document(str(source))
    title = f"XVS M22 Procurement and Requisitions Functional Requirements Document v{target_version}"
    doc.core_properties.title = title
    doc.core_properties.version = target_version
    replace_cover_versions(doc.tables[0], (source_version,), target_version)
    replace_control_value(doc.tables[1], "Version", target_version)
    replace_control_value(doc.tables[1], "Review date", REVIEW_DATE)
    replace_control_value(
        doc.tables[1],
        "Code baseline",
        "Commit e881a6b plus purpose-specific vendor RFQ mail and delivery-only RFQ secrets under review "
        "(4 September 2026)",
    )
    replace_control_value(
        doc.tables[1],
        "Source MRD",
        f"XVS Module Requirements Document v{MRD_TARGET_VERSION} | Module 22",
    )

    for paragraph in doc.paragraphs:
        text = paragraph.text.strip()
        if text.startswith("A school may or may not have branches"):
            replace_paragraph(
                paragraph,
                "Every school has at least one branch. A requisition may carry a branch or none, where none "
                "means school-wide, and which it carries follows the raiser's role grants rather than their "
                "staff record. Somebody granted at one branch stamps it; somebody granted at several who "
                "names none is refused and told to name one; somebody holding the role for the whole school "
                "files the request school-wide. A branch-scoped approval stage resolves people appointed at "
                "that branch plus those appointed tenant-wide. Reads follow the same answer and read a null "
                "branch exclusively: a school-wide purchase is not shown to staff pinned to one branch.",
                size=9,
                space_after=5,
            )
        elif text.startswith("Module 22 carries 16 capability entries"):
            replace_paragraph(
                paragraph,
                f"Module 22 carries 16 capability entries in MRD v{MRD_TARGET_VERSION}. Each maps to the "
                "requirements below. Purpose-specific RFQ mail and delivery-only vendor credentials "
                "strengthen supplier invitations and quotation capture without changing the count.",
                size=9,
                space_after=5,
            )

    fr012 = doc.tables[19]
    replace_cell(
        fr012.rows[2].cells[1],
        "A tokenised public form serves one RFQ, and the vendor verifies an emailed ten-minute code before "
        "editing. Invitation, code, reminder, amendment, deadline-extension, and submission-receipt mail "
        "state the request, deadline, action, and outcome. A receipt says it is neither acceptance nor an "
        "award. The signed token and verification code render into stored notification history as inert "
        "markers and are substituted only by the delivery worker. The vendor can submit, revise, acknowledge, "
        "decline, and attach documents after verification.",
        size=8.5,
    )
    replace_cell(
        fr012.rows[3].cells[1],
        "The routes are the only unauthenticated surface in the module and are scoped by the token to one "
        "invitation. Possession of the token and emailed code proves access. Neither raw value appears in a "
        "Notification row, while the outgoing email receives the working value. Required and information-only "
        "amendments render distinct instructions.",
        size=8.5,
    )
    replace_cell(
        fr012.rows[4].cells[1],
        "The token and code arrive through the same mailbox, so the code is not an independent second factor. "
        "Security still rests on control of the invited address.",
        size=8.5,
    )

    replace_cell(
        doc.tables[24].rows[3].cells[1],
        "Invited vendors receive a secure request with its deadline, open the tokenised form, and verify an "
        "emailed code. Reminders, amendments, and deadline extensions reuse the same signed access path.",
        size=8.2,
    )
    replace_cell(
        doc.tables[24].rows[4].cells[1],
        "Quotations are submitted as firm offers, a read-only receipt is emailed, and the offers are compared.",
        size=8.2,
    )
    replace_cell(
        doc.tables[30].rows[7].cells[1],
        "Delivers the six RFQ vendor messages through the shared layout. Signed links and verification codes "
        "are passed as delivery-only replacements, so durable history contains inert markers rather than "
        "credentials.",
        size=8.2,
    )

    prepend_change_log(
        doc.tables[34],
        target_version,
        "Refines the six vendor messages around the public RFQ flow. Invitations and reminders name the "
        "request, deadline, and submit-or-decline action. Verification mail keeps the ten-minute code out of "
        "the subject. Submission receipts say receipt is not acceptance or award. Amendment mail distinguishes "
        "a required resubmission from an information-only change, and extension mail confirms the signed link "
        "remains valid. The link and code are now stored as inert Notification markers and replaced only at "
        "delivery. Corrects Section 2.2 to the platform rule that every school has at least one branch while a "
        "null document branch means school-wide. Updates FR-012, the sourcing lifecycle, Module 8 dependency, "
        "and MRD traceability. Module 22 remains Backend Complete and In use Complete with 16 capabilities. "
        + test_evidence() + " Backend evidence only; nothing here is deployed.",
    )
    finish(doc, output, title)


def patch_m23(source: Path, output: Path) -> None:
    source_version, target_version = "1.7", "1.8"
    doc = Document(str(source))
    title = f"XVS M23 Purchase Orders Delivery and AP Functional Requirements Document v{target_version}"
    doc.core_properties.title = title
    doc.core_properties.version = target_version
    replace_cover_versions(doc.tables[0], ("1.6", source_version), target_version)
    replace_control_value(doc.tables[1], "Version", target_version)
    replace_control_value(doc.tables[1], "Review date", REVIEW_DATE)
    replace_control_value(
        doc.tables[1],
        "Code baseline",
        "Commit e881a6b plus the purpose-specific purchase-order vendor email and migration 0017 under "
        "review (4 September 2026)",
    )
    replace_control_value(
        doc.tables[1],
        "Source MRD",
        f"XVS Module Requirements Document v{MRD_TARGET_VERSION} | Module 23",
    )

    for paragraph in doc.paragraphs:
        text = paragraph.text.strip()
        if text.startswith("Module 23 is the half of procure-to-pay"):
            replace_paragraph(
                paragraph,
                "Module 23 is the half of procure-to-pay that touches the books. It commits the entity with "
                "a purchase order, sends that order to the vendor with a durable delivery record, recognises "
                "cost when goods arrive, matches the supplier's bill, creates the payable, and settles it. "
                "The purchase-order email now presents its attachment, order and delivery terms, optional "
                "buyer note, and contact as one purpose-specific message.",
                size=9,
                space_after=5,
            )
        elif text.startswith("Module 23 carries 22 capability entries"):
            replace_paragraph(
                paragraph,
                f"Module 23 carries 22 capability entries in MRD v{MRD_TARGET_VERSION}. Each maps to the "
                "requirements below. Clearer purchase-order vendor mail strengthens delivery and status "
                "tracking without changing the count.",
                size=9,
                space_after=5,
            )

    fr002 = doc.tables[8]
    replace_cell(
        fr002.rows[2].cells[1],
        "The order email is scheduled, rendered and delivered through a durable path with a preview endpoint, "
        "a per-delivery record, and an explicit retry. The message names the order, order date, expected "
        "delivery, total, address, payment terms, attached PDF, optional buyer note, and buyer contact. It has "
        "no button because this flow supplies a document rather than a vendor portal action.",
        size=8.5,
    )
    replace_cell(
        fr002.rows[3].cells[1],
        "A failed send is visible and retryable without re-issuing the order. A standard render contains the "
        "PDF instruction and omits the note or email cleanly when the buyer supplied neither. Migration 0017 "
        "updates only platform-maintained templates.",
        size=8.5,
    )

    append_styled_row(
        doc.tables[27],
        [
            "Module 8, Notifications & Delivery",
            "Renders and delivers the purchase-order email through the shared tenant-aware layout, records "
            "its outcome, carries the PDF attachment metadata, and preserves staff-authored template overrides.",
        ],
    )

    prepend_change_log(
        doc.tables[31],
        target_version,
        "Refines the standard purchase-order vendor email without changing the delivery workflow. The message "
        "now presents order date, expected delivery, total, address, payment terms, attached PDF, optional "
        "buyer note, and buyer contact in a clear order. It deliberately has no button because the vendor's "
        "action is to review the attached order, not open an internal product route. Migration 0017 updates "
        "only the platform-maintained row and preserves staff-authored content. Also corrects the inherited "
        "cover version, which still displayed 1.6 while Document Control already said 1.7. Updates purpose, "
        "FR-002, the Module 8 dependency, and MRD traceability. Module 23 remains Backend Complete and In use "
        "Complete with 22 capabilities. " + test_evidence() +
        " Backend evidence only; nothing here is deployed.",
    )
    finish(doc, output, title)


def main() -> None:
    parser = argparse.ArgumentParser()
    for name in ("mrd", "m08", "m22", "m23"):
        parser.add_argument(f"--{name}-source", type=Path, required=True)
        parser.add_argument(f"--{name}-output", type=Path, required=True)
    args = parser.parse_args()
    patch_mrd(args.mrd_source, args.mrd_output)
    patch_m08(args.m08_source, args.m08_output)
    patch_m22(args.m22_source, args.m22_output)
    patch_m23(args.m23_source, args.m23_output)
    for output in (args.mrd_output, args.m08_output, args.m22_output, args.m23_output):
        print(output)


if __name__ == "__main__":
    main()

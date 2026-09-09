#!/usr/bin/env python3
"""Version the Module 1, 3 and 9 FRDs for invitation hand-off recovery.

The invitation email is enqueued from a transaction.on_commit callback, so the
hand-off to the broker happens after its caller has returned. Every caller wrote
its record of "the invitation went out" before that hand-off was attempted, and
the helper caught the broker's refusal and logged it, so a school created while
the broker was unreachable reported its principal's invitation as SENT with no
task, no email, and no way for anything to find out.

Module 3 now records a refused hand-off as a failed delivery on the invitation
row, publishes the outcome, and re-sends on a beat schedule. Module 1's
administrator link is written from that settled outcome instead of from the
intention to send. Module 9's first-administrator limit stops saying a delivery
failure waits for an operator.

Traceability in Modules 1 and 9 is reconciled to MRD v2.74, which lists one
capability entry that neither table had a row for. The MRD needs no new version:
no module status, capability count, ownership, dependency, limitation or gap in
it moves, and recovery of a hand-off sits inside the delivery capability that
Module 3 already carries, exactly as the payout recovery sweep sits inside
Module 18's payout capability.

    python tools/patch_invitation_dispatch_recovery_docs.py
"""

from __future__ import annotations

import argparse
import copy
from pathlib import Path

from docx import Document

from generate_requirements_documents import (
    assert_no_em_dash,
    shrink_inherited_media,
    update_extended_title,
    write_cell,
)

REVIEW_DATE = "9 September 2026"
SHORT_DATE = "9 Sep 2026"
MRD_VERSION = "2.74"
CODE_BASELINE = "Backend worktree at 9 September 2026"


# ── shared docx helpers ──────────────────────────────────────────────────────

def replace_cell(cell, text: str, **kwargs) -> None:
    while len(cell.paragraphs) > 1:
        paragraph = cell.paragraphs[-1]
        paragraph._p.getparent().remove(paragraph._p)
    write_cell(cell, text, **kwargs)


def set_run_text(paragraph, text: str) -> None:
    """Rewrite a paragraph in place, keeping the formatting of its first run."""
    if not paragraph.runs:
        raise ValueError("Paragraph carries no run to inherit formatting from")
    paragraph.runs[0].text = text
    for run in paragraph.runs[1:]:
        run.text = ""


def replace_cover_version(table, source: str, target: str) -> None:
    """Rewrite the version on the cover, which is one run of one title block."""
    for paragraph in table.rows[0].cells[0].paragraphs:
        for run in paragraph.runs:
            if f"Version: {source}" in run.text:
                run.text = run.text.replace(f"Version: {source}", f"Version: {target}")
                return
    raise ValueError(f"Cover version not found: {source}")


def replace_control_value(table, label: str, value: str) -> None:
    for row in table.rows:
        if row.cells[0].text.strip() == label:
            replace_cell(row.cells[1], value, size=9)
            return
    raise ValueError(f"Control row not found: {label}")


def replace_body_paragraph(doc, prefix: str, text: str) -> None:
    for paragraph in doc.paragraphs:
        if paragraph.text.strip().startswith(prefix):
            set_run_text(paragraph, text)
            return
    raise ValueError(f"Body paragraph not found: {prefix}")


def find_row(table, prefix: str):
    for row in table.rows:
        if row.cells[0].text.strip().startswith(prefix):
            return row
    raise ValueError(f"Row not found: {prefix}")


def insert_row_before(table, prefix: str, values: list[str], *, size: float):
    """Clone the row named by ``prefix`` and write ``values`` into the copy."""
    anchor = find_row(table, prefix)
    anchor._tr.addprevious(copy.deepcopy(anchor._tr))
    # The clone carries the anchor's text, so it is now the first match.
    clone = find_row(table, prefix)
    for cell, value in zip(clone.cells, values):
        replace_cell(cell, value, size=size)
    return clone


def append_row(table, values: list[str], *, size: float):
    template = table.rows[-1]
    template._tr.addnext(copy.deepcopy(template._tr))
    row = table.rows[-1]
    for cell, value in zip(row.cells, values):
        replace_cell(cell, value, size=size)
    return row


def prepend_change_log(table, version: str, summary: str) -> None:
    template = table.rows[1]
    template._tr.addprevious(copy.deepcopy(template._tr))
    row = table.rows[1]
    replace_cell(row.cells[0], version, size=8)
    replace_cell(row.cells[1], SHORT_DATE, size=8)
    replace_cell(row.cells[2], summary, size=8)


def finish(doc, output: Path, title: str, version: str) -> None:
    doc.core_properties.title = title
    doc.core_properties.version = version
    output.parent.mkdir(parents=True, exist_ok=True)
    doc.save(str(output))
    update_extended_title(output, title)
    shrink_inherited_media(output)
    assert_no_em_dash(output)


def require_fr(table, label: str):
    if not table.rows[0].cells[0].text.strip().startswith(label):
        raise ValueError(f"Table is not {label}")
    return table


# ═════════════════════════════════════════════════════════════════════════════
# Module 1 - School & Branch Management
# ═════════════════════════════════════════════════════════════════════════════

M01_DIR = "01-school-and-branch-management"
M01_STEM = "XVS_M01_School_and_Branch_Management_Functional_Requirements_Document"
M01_SOURCE, M01_TARGET = "1.21", "1.22"

#: FR-004 and FR-006 requirement tables, and the row each field sits on.
M01_FR004_TABLE, M01_FR006_TABLE = 12, 14
EVIDENCE_ROW, ACCEPTANCE_ROW, LIMIT_ROW = 2, 3, 4

#: Creation sequence, data model, dependencies, traceability, change log.
M01_SEQUENCE_TABLE, M01_MODEL_TABLE = 30, 33
M01_DEPENDENCY_TABLE, M01_TRACEABILITY_TABLE, M01_CHANGE_LOG_TABLE = 39, 42, 43

M01_FR004_EVIDENCE = (
    "SchoolPrimaryAdmin, ContactInfo, User, TenantUserRoleAssignment, "
    "InvitationService, and the Celery invitation request form the path. "
    "provision_admin_user rolls its partial work back in an inner savepoint, "
    "logs the server-side cause, and raises AdminProvisioningError. That error "
    "escapes SchoolCreateSerializer's outer transaction, so a failure removes "
    "the School, Tenant, Branches, contacts, users, grants, invitation records, "
    "and any administrator provisioned earlier in the nested request. The "
    "school_admin template is guaranteed on fresh installations by an idempotent "
    "school-app data migration that preserves existing rows. The invitation "
    "email is queued through vs_user queue_invitation_email, which defers the "
    "job until the outermost creation transaction commits. Provisioning leaves "
    "the administrator link QUEUED, because the hand-off to the broker happens "
    "after it has returned and it cannot know the outcome: the "
    "settle_primary_admin_invite receiver writes SENT when the broker takes the "
    "job and FAILED when it refuses, and writes SENT again when Module 3's "
    "recovery re-send gets through. Stamping SENT inside the transaction "
    "reported an invitation that no task existed for and no email carried "
    "whenever the broker was unreachable."
)

M01_FR004_ACCEPTANCE = (
    "A valid unused email produces a School-scoped inactive user, whole-tenant "
    "school_admin assignment, invitation record, and an administrator link that "
    "reads SENT once the invitation email has been accepted for delivery, or "
    "FAILED when it has not. A 201 response means those records exist. Any "
    "provisioning exception returns 503 ADMIN_PROVISIONING_FAILED and leaves no "
    "parent School or related creation records. Later email delivery or "
    "activation can still fail and is handled by invitation operations and "
    "Module 9 readiness. ProvisioningInviteWaitsForCommitTests proves nothing is "
    "queued while the invitation is uncommitted, that the queued job names the "
    "committed row, and that a rolled-back creation queues no email at all. "
    "TheAdminLinkNeverClaimsAnInvitationNobodySentTests proves the link reads "
    "FAILED when the broker refuses the hand-off, that the account and its grant "
    "survive that refusal, and that a successful re-send returns the link to SENT."
)

M01_FR004_LIMIT = (
    "No transactional creation gap remains. Notification event types and "
    "templates must still be seeded for delivery, and the administrator must "
    "still activate the invitation before Module 9 marks FIRST_ADMIN complete. "
    "The link settles after the creation response has been built, so it reads "
    "QUEUED for the life of that request; the create response does not carry it, "
    "and the School and Branch detail endpoints that do are read later."
)

M01_FR006_EVIDENCE = (
    "BranchPrimaryAdmin and the shared admin-provisioning service create the "
    "current link. Reused emails remain idempotent for the account and "
    "invitation and receive a distinct Branch-scoped grant at every named "
    "Branch. The branch_admin template is guaranteed on fresh installations by "
    "the same idempotent school-app migration. A provisioning exception now "
    "escapes the inner savepoint and rolls back the standalone Branch or the "
    "entire nested School creation transaction. Both Branch paths share that "
    "provisioning service, so a Branch administrator's invitation email is "
    "queued on the same commit rather than from inside the savepoint that may "
    "still roll the Branch back, and the Branch link takes SENT or FAILED from "
    "the same settled hand-off rather than from the request to make it."
)

M01_FR006_ACCEPTANCE = (
    "A valid administrator is linked to the target Branch and tenant with a "
    "Branch-scoped role assignment and an invitation state that reports whether "
    "an email is on its way rather than whether one was asked for. An address "
    "named at more than one Branch produces one account and invitation and one "
    "grant at each Branch, and the single settled hand-off marks every link "
    "naming that address. A failed standalone provisioning attempt returns 503 "
    "and removes only the new Branch transaction; a nested failure removes the "
    "whole new School transaction."
)

M01_SEQUENCE_OUTCOME = (
    "Return 503 and roll back creation if required administrator provisioning "
    "fails. Leave the administrator link QUEUED until the email hand-off "
    "settles, so nothing records an invitation as sent before one has been "
    "accepted for delivery. Keep downstream delivery and activation observable "
    "as later lifecycle steps."
)

M01_SCHOOL_LINK_CONTRACT = (
    "One-to-one School; no persona column, so the record cannot disagree with "
    "the role assignment that grants the authority; invite status is written "
    "from the settled email hand-off rather than from the intention to send one"
)

M01_BRANCH_LINK_CONTRACT = (
    "One-to-one Branch; no persona column and the same settled-hand-off rule, "
    "for the same reasons"
)

M01_VS_USER_CONTRACT = (
    "Create pending users and invitation records; request activation email "
    "delivery and report whether the broker accepted the request. Creation "
    "guarantees the durable request records, not successful downstream delivery "
    "or activation."
)

M01_ADMIN_CAPABILITY_STATE = (
    "Implemented. Required administrator creation is atomic with its parent; a "
    "reused address named at more than one posting receives every scoped grant. "
    "The administrator link records whether the invitation email was accepted "
    "for delivery rather than merely asked for. Delivery and activation remain "
    "separate lifecycle steps."
)

#: The branding-files row carried a verbatim copy of the audit-evidence row
#: above it, which describes audit events and says nothing about reading a logo.
M01_BRANDING_CAPABILITY_STATE = (
    "Implemented; the logo is bound to the school's branding record and to that "
    "school's tenant, so it is served to anybody signed in to the school, which "
    "is what a sidebar, a favicon and a letterhead need, and to nobody outside "
    "it. A branding row whose school has moved tenant stops serving to the old "
    "one, which the file's tenant column alone cannot see. The URL is signed for "
    "one reader and expires."
)

M01_REAPPLIED_PLAN_ROW = [
    "Plan re-applied to a school already trading",
    "FR-007",
    "Implemented; apply_plans re-grants a School already trading through the "
    "same service creation uses, so a School created with a plan and a School "
    "moved onto one cannot end up with different grants.",
]

M01_TRACEABILITY_LEAD = (
    f"This table reconciles every Module 1 capability entry in MRD v{MRD_VERSION} "
    "to the controlling functional requirement in this FRD. All twenty-one "
    "entries are represented; the re-applied plan entry, which the tracker lists "
    "and this table had no row for, is given one. Recording the administrator "
    "link from the settled email hand-off strengthens the existing administrator "
    "capability without adding a product surface or changing the count."
)

M01_CHANGE_SUMMARY = (
    "Stops the administrator link claiming an invitation nobody sent. The link "
    "was stamped SENT inside the creation transaction, before the invitation "
    "email had been handed to a broker at all, so a School created while the "
    "broker was unreachable reported the principal's invitation as sent: no task "
    "existed, no email arrived, and the first administrator could not activate "
    "the School. Provisioning now leaves the link QUEUED and the settled hand-off "
    "writes SENT or FAILED, through a receiver on the outcome vs_user publishes, "
    "so the record follows the email rather than the intention to send one. A "
    "refused hand-off is recorded as a failed delivery on the invitation itself "
    "and re-sent automatically by Module 3, which returns the link to SENT. "
    "FR-004 and FR-006, the creation sequence, both admin-link data contracts, "
    "the vs_user dependency and MRD traceability are updated, and the "
    "traceability table gains the re-applied plan row the tracker lists. The "
    "branding-files row is corrected: it carried a verbatim copy of the "
    "audit-evidence row above it, which describes audit events and said nothing "
    "about who may read a school's logo. "
    "Verified by 335 school and 375 identity tests; thirteen migration-harness "
    "failures in that count are pre-existing and unrelated. Backend evidence "
    "only; nothing here is deployed."
)


def patch_m01(source: Path, output: Path) -> None:
    doc = Document(str(source))
    title = (
        "XVS M01 School and Branch Management Functional Requirements "
        f"Document v{M01_TARGET}"
    )

    replace_cover_version(doc.tables[0], M01_SOURCE, M01_TARGET)
    control = doc.tables[1]
    replace_control_value(control, "Version", M01_TARGET)
    replace_control_value(control, "Review date", REVIEW_DATE)
    replace_control_value(control, "Code baseline", CODE_BASELINE)
    replace_control_value(
        control, "Source MRD",
        f"XVS Module Requirements Document v{MRD_VERSION} | Module 1",
    )

    fr004 = require_fr(doc.tables[M01_FR004_TABLE], "FR-004")
    replace_cell(fr004.rows[EVIDENCE_ROW].cells[1], M01_FR004_EVIDENCE, size=8.5)
    replace_cell(fr004.rows[ACCEPTANCE_ROW].cells[1], M01_FR004_ACCEPTANCE, size=8.5)
    replace_cell(fr004.rows[LIMIT_ROW].cells[1], M01_FR004_LIMIT, size=8.5)

    fr006 = require_fr(doc.tables[M01_FR006_TABLE], "FR-006")
    replace_cell(fr006.rows[EVIDENCE_ROW].cells[1], M01_FR006_EVIDENCE, size=8.5)
    replace_cell(fr006.rows[ACCEPTANCE_ROW].cells[1], M01_FR006_ACCEPTANCE, size=8.5)

    sequence = doc.tables[M01_SEQUENCE_TABLE]
    replace_cell(
        find_row(sequence, "6. Evidence").cells[2], M01_SEQUENCE_OUTCOME, size=8,
    )

    model = doc.tables[M01_MODEL_TABLE]
    replace_cell(
        find_row(model, "SchoolPrimaryAdmin").cells[2],
        M01_SCHOOL_LINK_CONTRACT, size=8,
    )
    replace_cell(
        find_row(model, "BranchPrimaryAdmin").cells[2],
        M01_BRANCH_LINK_CONTRACT, size=8,
    )

    replace_cell(
        find_row(doc.tables[M01_DEPENDENCY_TABLE], "vs_user").cells[2],
        M01_VS_USER_CONTRACT, size=8,
    )

    traceability = doc.tables[M01_TRACEABILITY_TABLE]
    replace_cell(
        find_row(traceability, "Create first School and Branch administrators").cells[2],
        M01_ADMIN_CAPABILITY_STATE, size=8,
    )
    replace_cell(
        find_row(traceability, "Authorised, expiring access to school branding files").cells[2],
        M01_BRANDING_CAPABILITY_STATE, size=8,
    )
    insert_row_before(
        traceability, "A school's plan, read and changed",
        M01_REAPPLIED_PLAN_ROW, size=8,
    )
    replace_body_paragraph(
        doc, "This table reconciles every Module 1 capability", M01_TRACEABILITY_LEAD,
    )

    prepend_change_log(
        doc.tables[M01_CHANGE_LOG_TABLE], M01_TARGET, M01_CHANGE_SUMMARY,
    )
    finish(doc, output, title, M01_TARGET)


# ═════════════════════════════════════════════════════════════════════════════
# Module 3 - Identity, Team & Organogram
# ═════════════════════════════════════════════════════════════════════════════

M03_DIR = "03-identity-team-and-organogram"
M03_STEM = "XVS_M03_Identity_Team_and_Organogram_Functional_Requirements_Document"
M03_SOURCE, M03_TARGET = "1.10", "1.11"

#: FR-011, the invitation sequence, the data model, the gaps table, the
#: traceability table, the reconciliation box and the change log.
M03_FR011_TABLE, M03_SEQUENCE_TABLE, M03_MODEL_TABLE = 17, 29, 32
M03_GAPS_TABLE, M03_TRACEABILITY_TABLE = 38, 40
M03_RECONCILIATION_TABLE, M03_CHANGE_LOG_TABLE = 41, 42

M03_FR011_EVIDENCE_TAIL = (
    " queue_invitation_email also records what became of the hand-off, because "
    "it runs after its caller has returned and no caller can see it fail: a "
    "broker that refuses the job marks the invitation's delivery FAILED, keeping "
    "the exception's type alone so a broker URL and its credentials never reach "
    "the column, and publishes the outcome on invitation_dispatch_settled for "
    "modules that hold their own record of the invitation. "
    "retry_failed_invitation_emails re-sends every FAILED invitation that is "
    "unused and unexpired, on a fifteen-minute beat schedule and up to five "
    "attempts, issuing a new token because only the digest of the lost one was "
    "ever stored. PENDING is deliberately not swept, being what an invitation "
    "looks like while its worker has yet to run."
)

M03_FR011_ACCEPTANCE_TAIL = (
    " ABrokerOutageMustNotLookLikeASentInvitationTests proves a refused hand-off "
    "is recorded as a failed delivery rather than logged and forgotten, that the "
    "recorded reason carries no broker credentials, that it cannot overwrite a "
    "delivery outcome already judged, that the sweep re-sends with a new token "
    "and kills the old one, that a used, expired or still in-flight invitation "
    "is left alone, and that the attempt count survives each re-send so the cap "
    "is reachable."
)

M03_FR011_LIMIT = (
    "Nothing expires an invitation in the background; is_expired is computed on "
    "read, so a stale row remains and the account stays PENDING until an "
    "administrator resends or changes it. Creating the invitation also creates "
    "the platform staff profile as a side effect. Recovering a lost hand-off "
    "depends on beat running the fifteen-minute sweep, so where beat does not "
    "run the invitation is delayed rather than lost. After five failed attempts "
    "a row is reported in the sweep's log rather than retried, because a mailbox "
    "that does not exist is not a bad moment, and nothing yet tells an "
    "administrator that one of theirs has stopped being tried."
)

M03_SEQUENCE_EFFECT_TAIL = (
    " A hand-off the broker refuses is recorded as a failed delivery on the same "
    "row and re-sent by the recovery sweep with a fresh token."
)

M03_MODEL_NOTE_TAIL = (
    " The delivery status is the row's own answer to whether an email is on its "
    "way, and is what the recovery sweep selects on."
)

M03_GAP_93 = (
    "expire_stale_login_sessions exists but is only called when somebody opens a "
    "session list, so between visits a row for an expired token reads as an "
    "active session. Used and expired PasswordResetRequest rows are never "
    "deleted and the table only grows. An expired invitation is computed on read "
    "and leaves the account PENDING for ever. vs_user now holds one beat entry, "
    "which re-sends invitations whose email reached nobody; none of the "
    "retention housekeeping above is on it. Add that housekeeping, or state the "
    "retention contract deliberately."
)

M03_INVITATION_CAPABILITY_STATE = (
    "Implemented with limits. Each row owns a separate hashed iv_ token and "
    "resend kills the old URL. The email names the inviter, tenant workspace, "
    "expiry and one-time use, with working plain-text and HTML paths. A hand-off "
    "the broker refuses is recorded on the row and re-sent by a scheduled sweep "
    "with a fresh token, and the outcome is published for modules keeping their "
    "own copy of it. Expiry is computed on read and no job sweeps stale rows."
)

M03_RECONCILIATION = (
    "MRD RECONCILIATION\n"
    f"• MRD v{MRD_VERSION} lists Module 3 as Backend Partial and In use Complete "
    "with seventeen capabilities. This revision records recovery of a lost "
    "invitation hand-off inside the existing invitation capability, without "
    "adding a product surface or changing the count.\n"
    "• A refused hand-off is a recorded failed delivery on the invitation rather "
    "than a log line, and the scheduled re-send issues a new token because only "
    "the digest of the lost one was stored.\n"
    "• Partial remains correct because privileged MFA and the existing tenant "
    "lifecycle, lockout, retention, audit-model and organogram gaps remain. The "
    "retention gap is narrowed and not closed: vs_user now has a beat entry, and "
    "none of the expiry housekeeping is on it.\n"
    "• Backend evidence only. Neither document claims deployment, production "
    "adoption, or data migration completion."
)

M03_TRACEABILITY_LEAD = (
    f"MRD v{MRD_VERSION} records Module 3 as Identity, Team & Organogram, Phase "
    "V1, Backend Partial, In use Complete, code ownership vs_user, with "
    "seventeen capability entries. Each entry maps below."
)

M03_CHANGE_SUMMARY = (
    "Records recovery of an invitation email whose hand-off to the broker was "
    "refused. The enqueue runs after the transaction that created the invitation "
    "has committed, and therefore after its caller has returned, so a broker "
    "outage produced an account that had been invited and an email nobody sent, "
    "with a log line as the only trace. The refusal is now written onto the "
    "invitation as a failed delivery, carrying the exception's type alone so a "
    "broker URL and its credentials cannot reach a database column, and "
    "published on invitation_dispatch_settled for modules keeping their own "
    "record. retry_failed_invitation_emails re-sends every failed, unused and "
    "unexpired invitation on a fifteen-minute schedule, up to five attempts, "
    "with a new token because only the digest of the lost one was stored; an "
    "invitation still in flight is left alone. FR-011, the invitation sequence, "
    "the UserInvitation data contract, gap 9.3, MRD traceability and the "
    "reconciliation box are updated. Module 3 remains Backend Partial and In use "
    "Complete with seventeen capabilities. Verified by 375 identity and 335 "
    "school tests; thirteen migration-harness failures in that count are "
    "pre-existing and unrelated. Backend evidence only; nothing here is deployed."
)


def append_cell_text(cell, tail: str, *, size: float) -> None:
    replace_cell(cell, cell.text.strip() + tail, size=size)


def patch_m03(source: Path, output: Path) -> None:
    doc = Document(str(source))
    title = (
        "XVS M03 Identity Team and Organogram Functional Requirements "
        f"Document v{M03_TARGET}"
    )

    replace_cover_version(doc.tables[0], M03_SOURCE, M03_TARGET)
    control = doc.tables[1]
    replace_control_value(control, "Version", M03_TARGET)
    replace_control_value(control, "Review date", REVIEW_DATE)
    replace_control_value(control, "Code baseline", CODE_BASELINE)
    replace_control_value(
        control, "Source MRD",
        f"XVS Module Requirements Document v{MRD_VERSION} | Module 3",
    )

    fr011 = require_fr(doc.tables[M03_FR011_TABLE], "FR-011")
    append_cell_text(fr011.rows[EVIDENCE_ROW].cells[1], M03_FR011_EVIDENCE_TAIL, size=8.5)
    append_cell_text(
        fr011.rows[ACCEPTANCE_ROW].cells[1], M03_FR011_ACCEPTANCE_TAIL, size=8.5,
    )
    replace_cell(fr011.rows[LIMIT_ROW].cells[1], M03_FR011_LIMIT, size=8.5)

    append_cell_text(
        find_row(doc.tables[M03_SEQUENCE_TABLE], "6").cells[2],
        M03_SEQUENCE_EFFECT_TAIL, size=8,
    )
    append_cell_text(
        find_row(doc.tables[M03_MODEL_TABLE], "UserInvitation").cells[2],
        M03_MODEL_NOTE_TAIL, size=8,
    )

    gaps = doc.tables[M03_GAPS_TABLE]
    for row in gaps.rows:
        if row.cells[1].text.strip().startswith("9.3 Nothing sweeps"):
            replace_cell(row.cells[2], M03_GAP_93, size=7.5)
            break
    else:
        raise ValueError("Gap 9.3 row not found")

    replace_cell(
        find_row(doc.tables[M03_TRACEABILITY_TABLE], "Invitation creation, delivery").cells[2],
        M03_INVITATION_CAPABILITY_STATE, size=8,
    )
    replace_body_paragraph(doc, "MRD v2.70 records Module 3", M03_TRACEABILITY_LEAD)
    set_run_text(
        doc.tables[M03_RECONCILIATION_TABLE].rows[0].cells[0].paragraphs[0],
        M03_RECONCILIATION,
    )

    prepend_change_log(
        doc.tables[M03_CHANGE_LOG_TABLE], M03_TARGET, M03_CHANGE_SUMMARY,
    )
    finish(doc, output, title, M03_TARGET)


# ═════════════════════════════════════════════════════════════════════════════
# Module 9 - School Onboarding
# ═════════════════════════════════════════════════════════════════════════════

M09_DIR = "09-school-onboarding"
M09_STEM = "XVS_M09_School_Onboarding_Functional_Requirements_Document"
M09_SOURCE, M09_TARGET = "2.9", "2.10"

#: The creation boundary box, FR-006, the traceability table and the change log.
M09_BOUNDARY_TABLE, M09_FR006_TABLE = 4, 15
M09_TRACEABILITY_TABLE, M09_CHANGE_LOG_TABLE = 40, 41

M09_BOUNDARY = (
    "CURRENT CREATION, READINESS, AND COMMUNICATION BOUNDARY\n"
    "• Module 1 returns 503 and rolls the whole new School transaction back when "
    "its required administrator cannot be provisioned. Module 9 receives no "
    "half-created School to repair.\n"
    "• FIRST_ADMIN still checks later invitation activation and account or "
    "assignment liveness. An invitation email that never reached the broker is "
    "re-sent by Module 3 rather than waiting for an operator to notice. "
    "ROLE_BASELINE still checks that the School administrator role grants "
    "authority before go-live.\n"
    "• A School completes the seven-step catalog and submits a request. Platform "
    "staff alone approve, reject, activate, and reinstate; the ready email now "
    "states that hand-off accurately.\n"
    "• Six email and in-app events cover progress, readiness, review, "
    "activation, expiry warning, and platform follow-up. Human-readable local "
    "dates sit beside the older machine-readable context for customized "
    "templates.\n"
    "• Onboarding expiry and reinstatement still use the shared locked Tenant "
    "transition service. Suspension closes active impersonation sessions in the "
    "same transaction, and reinstatement never revives them."
)

M09_FR006_EVIDENCE_TAIL = (
    " Module 1 leaves the administrator link QUEUED until the invitation email "
    "hand-off settles, and Module 3 re-sends an invitation the broker refused, "
    "so a broker outage during creation delays FIRST_ADMIN rather than stranding "
    "it behind an email nobody knew was missing."
)

M09_FR006_LIMIT = (
    "This module does not retry Module 1 creation because no half-created School "
    "remains. A refused email hand-off is retried by Module 3's scheduled "
    "re-send, so what still needs the invitation operations is an invitation "
    "that was delivered and not acted on. Activation remains the administrator's "
    "own step, and FIRST_ADMIN cannot complete without it."
)

M09_PLAN_SCOPED_ROW = [
    "A checklist scoped to the steps the plan can perform",
    "FR-001, FR-003",
]

M09_TRACEABILITY_LEAD = (
    f"Module 9 carries 21 capability entries in MRD v{MRD_VERSION}. Each maps to "
    "the requirements below; the plan-scoped checklist entry, which the tracker "
    "lists and this table had no row for, is given one. Automatic re-sending of "
    "a refused invitation hand-off strengthens the existing first-administrator "
    "readiness capability without adding an event or changing the count."
)

M09_CHANGE_SUMMARY = (
    "Reconciles first-administrator readiness with automatic recovery of a "
    "refused invitation hand-off. Module 1 leaves the administrator link QUEUED "
    "until the invitation email has been accepted for delivery, and Module 3 "
    "re-sends one the broker refused, so a School created during a broker outage "
    "has its FIRST_ADMIN step delayed rather than stranded behind an email "
    "nobody knew was missing. FR-006's evidence and limit and the creation, "
    "readiness and communication boundary are updated, and the traceability "
    "table gains the plan-scoped checklist row the tracker lists. No code in "
    "this module changed. Backend evidence only; nothing here is deployed."
)


def patch_m09(source: Path, output: Path) -> None:
    doc = Document(str(source))
    title = (
        f"XVS M09 School Onboarding Functional Requirements Document v{M09_TARGET}"
    )

    replace_cover_version(doc.tables[0], M09_SOURCE, M09_TARGET)
    control = doc.tables[1]
    replace_control_value(control, "Version", M09_TARGET)
    replace_control_value(control, "Review date", REVIEW_DATE)
    replace_control_value(control, "Code baseline", CODE_BASELINE)
    replace_control_value(
        control, "Source MRD",
        f"XVS Module Requirements Document v{MRD_VERSION} | Module 9",
    )
    replace_control_value(
        control, "Supersedes", f"v{M09_SOURCE} and all earlier versions, retained unchanged",
    )

    set_run_text(
        doc.tables[M09_BOUNDARY_TABLE].rows[0].cells[0].paragraphs[0], M09_BOUNDARY,
    )

    fr006 = require_fr(doc.tables[M09_FR006_TABLE], "FR-006")
    append_cell_text(fr006.rows[EVIDENCE_ROW].cells[1], M09_FR006_EVIDENCE_TAIL, size=8.5)
    replace_cell(fr006.rows[LIMIT_ROW].cells[1], M09_FR006_LIMIT, size=8.5)

    append_row(doc.tables[M09_TRACEABILITY_TABLE], M09_PLAN_SCOPED_ROW, size=8)
    replace_body_paragraph(doc, "Module 9 carries", M09_TRACEABILITY_LEAD)

    prepend_change_log(
        doc.tables[M09_CHANGE_LOG_TABLE], M09_TARGET, M09_CHANGE_SUMMARY,
    )
    finish(doc, output, title, M09_TARGET)


# ═════════════════════════════════════════════════════════════════════════════

def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", default=Path(__file__).resolve().parents[1])
    args = parser.parse_args()
    root = Path(args.root) / "functional-requirements"

    for folder, stem, source, target, patch in (
        (M01_DIR, M01_STEM, M01_SOURCE, M01_TARGET, patch_m01),
        (M03_DIR, M03_STEM, M03_SOURCE, M03_TARGET, patch_m03),
        (M09_DIR, M09_STEM, M09_SOURCE, M09_TARGET, patch_m09),
    ):
        directory = root / folder
        patch(
            directory / f"{stem}_v{source}.docx",
            directory / f"{stem}_v{target}.docx",
        )
        print(f"Wrote {folder} v{target}")


if __name__ == "__main__":
    main()

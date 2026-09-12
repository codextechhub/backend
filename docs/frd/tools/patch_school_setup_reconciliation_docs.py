#!/usr/bin/env python3
"""Version the Module 1, 8 and 9 FRDs against the backend at 13675db.

Module 1 had been revised only in narrow patches since v1.19, so several
behaviours that changed underneath it were never written down, and one
statement it carried had stopped being true:

* a new School receives the roles CodeX ships, a Branch role names its Branch
  only once the School has two, a head teacher named on their own Branch is
  granted there, and every administrator is given a staff record;
* moving a School down a plan tier takes back the role grants the new tier no
  longer reaches;
* the crest is public by the School's address, which contradicts the
  requirement that said it is served to nobody outside the School, and the
  console reads it the same way;
* the Branch type is gone from the model and every serializer;
* the School's own profile, logo and Branch routes and the operator service
  switch exist and section 7 never listed them.

Module 9 still described a seven-step checklist and two steps the catalog
merged into one card, and did not record what Module 1 now hands it at
creation. Module 8 had not recorded the link from a finished import job's
notice to its batch, the sender's logo in the email header with the check on
its address, or the ticket escalation event.

Each document is read from its latest version and written beside it as the
next minor version; no earlier version is touched. Traceability is set to MRD
v2.76, which is cut after these documents.

    python tools/patch_school_setup_reconciliation_docs.py [--modules 1 8 9]
"""

from __future__ import annotations

import argparse
import copy
from pathlib import Path

from docx import Document
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.text.paragraph import Paragraph

from generate_requirements_documents import (
    VERY_PALE_BLUE,
    WHITE,
    assert_no_em_dash,
    set_cell_shading,
    shrink_inherited_media,
    update_extended_title,
    write_cell,
)

REVIEW_DATE = "11 September 2026"
SHORT_DATE = "11 Sep 2026"
MRD_VERSION = "2.76"
CODE_BASELINE = "Backend worktree at 13675db, 11 September 2026"


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


def cell_size(cell, default: float) -> float:
    """The point size the cell's text is set in, so a rewrite keeps it."""
    for paragraph in cell.paragraphs:
        for run in paragraph.runs:
            if run.text.strip() and run.font.size:
                return run.font.size.pt
    return default


def rewrite(cell, text: str, *, default: float = 8.0) -> None:
    replace_cell(cell, text, size=cell_size(cell, default))


def append_text(cell, tail: str, *, default: float = 8.0) -> None:
    rewrite(cell, cell.text.strip() + tail, default=default)


def swap_text(cell, old: str, new: str, *, default: float = 8.0) -> None:
    text = cell.text
    if old not in text:
        raise ValueError(f"Text not found in cell: {old[:60]}")
    rewrite(cell, text.replace(old, new).strip(), default=default)


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


def find_paragraph(doc, prefix: str):
    for paragraph in doc.paragraphs:
        if paragraph.text.strip().startswith(prefix):
            return paragraph
    raise ValueError(f"Body paragraph not found: {prefix}")


def replace_body_paragraph(doc, prefix: str, text: str) -> None:
    set_run_text(find_paragraph(doc, prefix), text)


def insert_paragraph_after(paragraph, text: str) -> None:
    """Clone a body paragraph, so the new one carries its style and indent."""
    clone = copy.deepcopy(paragraph._p)
    paragraph._p.addnext(clone)
    set_run_text(Paragraph(clone, paragraph._parent), text)


def find_row(table, prefix: str, *, col: int = 0, exact: bool = False):
    for row in table.rows:
        text = row.cells[col].text.strip()
        if (text == prefix) if exact else text.startswith(prefix):
            return row
    raise ValueError(f"Row not found: {prefix}")


def clone_row(table, anchor, values: list[str], *, before: bool = True):
    """Copy ``anchor`` beside itself and write ``values`` into the copy."""
    sizes = [cell_size(cell, 8.0) for cell in anchor.cells]
    clone = copy.deepcopy(anchor._tr)
    (anchor._tr.addprevious if before else anchor._tr.addnext)(clone)
    row = next(r for r in table.rows if r._tr is clone)
    for cell, value, size in zip(row.cells, values, sizes):
        replace_cell(cell, value, size=size)
    return row


def restripe(table) -> None:
    """Alternate white and pale-blue body rows, as the generator lays them out."""
    for index, row in enumerate(table.rows[1:]):
        for cell in row.cells:
            set_cell_shading(cell, VERY_PALE_BLUE if index % 2 else WHITE)


#: Row properties that must follow ``w:cantSplit`` in a ``w:trPr``.
_AFTER_CANT_SPLIT = ("trHeight", "tblHeader", "tblCellSpacing", "jc", "hidden")


def forbid_row_split(row) -> None:
    """Keep one table row on a single page, at its place in the schema order."""
    tr_pr = row._tr.get_or_add_trPr()
    if tr_pr.find(qn("w:cantSplit")) is not None:
        return
    cant_split = OxmlElement("w:cantSplit")
    for name in _AFTER_CANT_SPLIT:
        later = tr_pr.find(qn(f"w:{name}"))
        if later is not None:
            later.addprevious(cant_split)
            return
    tr_pr.append(cant_split)


def keep_rows_together(table) -> None:
    """Keep a short table on one page, and so with the heading above it."""
    for row in table.rows[:-1]:
        for cell in row.cells:
            for paragraph in cell.paragraphs:
                paragraph.paragraph_format.keep_with_next = True


def separate_from_previous_table(table) -> None:
    """Put an empty paragraph between two tables that Word would render as one.

    Two tables with nothing between them are joined on the page, so a repeating
    header on the first is drawn again above the second wherever it breaks.
    """
    previous = table._tbl.getprevious()
    if previous is None or previous.tag != qn("w:tbl"):
        return
    spacer = OxmlElement("w:p")
    properties = OxmlElement("w:pPr")
    spacing = OxmlElement("w:spacing")
    spacing.set(qn("w:before"), "0")
    spacing.set(qn("w:after"), "0")
    properties.append(spacing)
    spacer.append(properties)
    table._tbl.addprevious(spacer)


def prepend_change_log(table, version: str, summary: str) -> None:
    template = table.rows[1]
    template._tr.addprevious(copy.deepcopy(template._tr))
    row = table.rows[1]
    replace_cell(row.cells[0], version, size=8)
    replace_cell(row.cells[1], SHORT_DATE, size=8)
    replace_cell(row.cells[2], summary, size=8)


def rewrite_box(table, edit) -> None:
    """Rewrite a one-paragraph callout whose lines are separated by breaks."""
    paragraph = table.rows[0].cells[0].paragraphs[0]
    lines = paragraph.text.split("\n")
    set_run_text(paragraph, "\n".join(edit(lines)))


def require_fr(table, label: str):
    if not table.rows[0].cells[0].text.strip().startswith(label):
        raise ValueError(f"Table is not {label}")
    return table


def finish(doc, output: Path, title: str, version: str) -> None:
    doc.core_properties.title = title
    doc.core_properties.version = version
    output.parent.mkdir(parents=True, exist_ok=True)
    doc.save(str(output))
    update_extended_title(output, title)
    shrink_inherited_media(output)
    assert_no_em_dash(output)


EVIDENCE_ROW, ACCEPTANCE_ROW, LIMIT_ROW = 2, 3, 4


# ═════════════════════════════════════════════════════════════════════════════
# Module 1 - School & Branch Management
# ═════════════════════════════════════════════════════════════════════════════

M01_DIR = "01-school-and-branch-management"
M01_STEM = "XVS_M01_School_and_Branch_Management_Functional_Requirements_Document"
M01_SOURCE, M01_TARGET = "1.22", "1.23"

#: Every table this revision edits, bound before any row is inserted.
M01_TABLES = {
    "decision": 6, "gates": 8, "reset_box": 32,
    "FR-001": 9, "FR-004": 12, "FR-006": 14, "FR-007": 15, "FR-008": 16,
    "FR-009": 17, "FR-010": 18, "FR-013": 21, "FR-015": 23,
    "sequence": 30, "model": 33, "school_api": 35, "branch_api": 36,
    "validation": 38, "dependencies": 39, "needs": 40, "traceability": 42,
    "change_log": 43,
}

M01_SUPPORTING_APPS = (
    "vs_tenants, vs_config, vs_rbac, vs_user, vs_audit, vs_admin_console, "
    "vs_import_data, vs_finance, schools/vs_onboarding, schools/vs_staff, "
    "schools/core/fal, core"
)

M01_DECISION_COUNT = (
    "• Module 1 remains Backend Partial and In use Complete in MRD v2.76, with "
    "twenty-three capability entries."
)

M01_DECISION_BLOCKER = (
    "• The defining blocker remains broader School lifecycle control. Suspension "
    "and reactivation for commercial reasons, recoverable deletion, normal "
    "login-session revocation, and the handling of closed or deleted Schools and "
    "Branches remain open. The operator service switch takes a live School out "
    "of service and back, and does nothing more."
)

M01_DECISION_ADDITIONS = [
    "• A School opens with the roles CodeX ships. Creation provisions School "
    "Admin, Teacher, Finance Admin and Procurement Admin for the whole School and "
    "a Branch Admin for each Branch, each copied from the prebuilt library as it "
    "stands, so two Schools created days apart hold the same set. A Branch role "
    "names its Branch only once the School has a second one: where a School has "
    "one Branch the dimension recedes.",
    "• A lower plan tier takes the role grants with it. Moving a School down a "
    "plan revokes every role grant the new tier does not reach, through the "
    "audited role-access service, so what a School may do and what its roles say "
    "it may do stay one fact. Permission groups the School composed are left as "
    "they are.",
    "• The crest is public, deliberately. The sign-in page at a School's own "
    "address has no reader by definition and is where somebody decides whether "
    "they are at their own School, so the crest alone is served by that address "
    "to anybody. The route lists nothing and answers a School with no crest "
    "exactly as it answers an address nobody holds. The console reads the same "
    "address; the School's own profile still hands its people a URL signed for "
    "one reader.",
]

M01_NEW_GATES = [
    ["School's own profile and logo", "school.profile.view / school.profile.update",
     "The asserted tenant's School only"],
    ["School's own Branches", "school.branches.view",
     "The asserted tenant's Branches, read-only"],
    ["Public School crest", "None; throttled at 240 an hour",
     "One School's crest, by its address"],
    ["Take a School out of service or back", "IsVisionStaff and platform.schools.manage",
     "Route School; ACTIVE to INACTIVE and back"],
]

#: The ownership bullets in section 3.3, kept with each other and their heading.
M01_OWNERSHIP_BULLETS = (
    "• School has one protected Tenant",
    "• Branch belongs to exactly one Tenant",
    "• Platform actors and background jobs",
)

M01_FR001_EVIDENCE = (
    "SchoolCreateSerializer and School.save() create the School and Tenant "
    "atomically, together with at least one Branch. Required School and Branch "
    "administrators are inside that same transaction, and so are the roles CodeX "
    "ships: School Admin, Teacher, Finance Admin and Procurement Admin for the "
    "whole School, and a Branch Admin for each Branch. vs_rbac "
    "provision_role_from_prebuilt makes each one from the prebuilt role library, "
    "get-or-create on its key so a School never holds two copies, and copies the "
    "library's permissions onto it as they stand at creation. A School created "
    "before creation provisioned the whole set receives the missing roles from "
    "vs_rbac 0017, which leaves a role the School already has untouched. The set "
    "of books and Module 9 onboarding control room remain best effort inside "
    "their own savepoints because neither is the credential needed to operate "
    "the new School."
)

M01_FR001_ACCEPTANCE = (
    "A 201 response persists one School, one protected Tenant, at least one "
    "Branch with exactly one main, the four whole-School roles and a Branch Admin "
    "role for each Branch, and every required administrator account, scoped role "
    "assignment, invitation record, and admin link. A missing Branch is rejected "
    "before writes. Any required administrator provisioning failure returns 503 "
    "and leaves none of those creation records behind. "
    "ANewSchoolGetsTheRolesCodeXShipsTests proves the full set arrives without "
    "anybody running a command afterwards."
)

M01_FR001_LIMIT = (
    "Books and onboarding control-room provisioning are still best effort. A "
    "books failure is repaired by command, and an onboarding-control failure is "
    "repaired by re-provisioning. Neither failure produces a false claim that a "
    "required administrator exists. Teacher, Finance Admin and Procurement Admin "
    "arrive only where the prebuilt library holds them: the school-app migration "
    "guarantees the School Admin and Branch Admin templates on a fresh "
    "installation, nothing guarantees the other three, and a missing one is "
    "skipped without a warning. A role copies the library once, when it is made, "
    "so a key added to the library later reaches an existing School only through "
    "a command that syncs it, as seed_workflow_permissions does for the approval "
    "keys."
)

M01_FR004_EVIDENCE_TAIL = (
    " Provisioning also writes the administrator's staff record inside the same "
    "savepoint, so a School's own administrators are on its staff list: the job "
    "title is the words the creation form collected on the admin link, the "
    "posting is the account's own Branch, a record opened for a new account "
    "starts Invited and one for an account already in use starts Active, and a "
    "person who already holds a record is not given a second. The activation "
    "link in the invitation email lands on the School's own app, under /accounts "
    "at its subdomain, because vs_tenants account_link_base builds it; the "
    "Console, where a school account cannot sign in, is reserved for platform "
    "staff."
)

M01_FR004_ACCEPTANCE_TAIL = (
    " AnAdministratorIsAMemberOfStaffTests proves the School administrator is on "
    "the staff list, a Branch administrator is posted to their Branch, the "
    "record starts Invited and says so in its history, and one person holding "
    "two postings has one record; AnIncumbentsRecordReadsActiveTests proves a "
    "record written for an account already in use reads Active."
)

M01_FR004_LIMIT_TAIL = (
    " The activation link is built from SCHOOL_APP_BASE_URL, which defaults to "
    "the production host, so an environment that does not set it sends its "
    "Schools' people there; a blank value makes the invitation task raise rather "
    "than fall back to a Console link."
)

M01_FR006_EVIDENCE_TAIL = (
    " provision_admin_user runs for every named Branch administrator, including "
    "the School administrator named on a Branch in the same request: it finds "
    "the account inside the tenant and adds the Branch grant without a second "
    "account or invitation, so a head teacher who also runs the School's only Branch "
    "holds both jobs. Each Branch's role is its own copy of Branch Admin, keyed "
    "by the Branch. Its name carries the Branch only once the School has more "
    "than one, and the moment a second Branch is provisioned every sibling still "
    "named for no Branch is renamed to say which Branch it runs."
)

M01_FR006_ACCEPTANCE_TAIL = (
    " OnePersonWearingSeveralHatsTests proves a head teacher named as the only "
    "Branch's administrator holds both grants from one account and one "
    "invitation, and that one person running the School and every Branch is "
    "granted at each posting. ANewSchoolGetsTheRolesCodeXShipsTests proves a "
    "one-Branch School's role does not name its Branch and that a second Branch "
    "makes every sibling name its own."
)

M01_FR006_LIMIT = (
    "Standalone Branch creation still declares primary admin input optional "
    "while the create path requires it. The previous partial-success and "
    "reconciliation gap is closed. A Branch role's name is copied from the "
    "Branch when the role is made and does not follow a later rename, so a "
    "renamed Branch keeps its old name on its role until somebody edits the role."
)

M01_FR007_EVIDENCE_TAIL = (
    " change_plan then takes back every role grant the new tier does not reach. "
    "revoke_grants_the_plan_no_longer_reaches runs after the entitlements are "
    "written, because it asks what the School can now reach; it reads each "
    "granted permission through vs_rbac plan_reader, the function the plan gate "
    "and the permission picker also use, and writes each affected role's "
    "remaining keys through set_role_access with source plan_downgrade, so every "
    "revocation takes the role's lock, bumps its version and leaves an audit "
    "entry naming the plan change. Permission groups the School composed are "
    "left as they are, and their keys meet the same gate when they resolve. A "
    "plan change on a School still onboarding also re-runs Module 9 "
    "provisioning, best effort, so an upgrade adds any checklist step the new "
    "plan opens."
)

M01_FR007_ACCEPTANCE_TAIL = (
    " MovingDownATierTakesTheGrantsWithItTests proves a downgrade revokes the "
    "grants the new tier cannot reach, leaves alone those it still reaches, and "
    "records the revocation against the plan change."
)

M01_FR007_LIMIT_TAIL = (
    " Revocation runs on a plan change only. Withdrawing an uplift, an uplift "
    "lapsing, and apply_plans re-applying a plan leave any role grant beyond the "
    "depth that remains in place: refused by the plan gate, hidden from the "
    "picker, and back in force if the depth returns."
)

M01_FR008_EVIDENCE_TAIL = (
    " The list carries each School's logo, and it and the detail's branding "
    "block both hand the console the public crest address rather than a /media/ "
    "path, because a /media/ read is refused whenever the file's tenant differs "
    "from the reader's, which for a platform operator is every School (FR-009). "
    "The list reads each School's main Branch from the prefetch it already "
    "makes, so its query count does not grow with the number of Schools. Detail "
    "also returns app_url, where the School's own people sign in: derived rather "
    "than stored, by inserting the slug as a subdomain of SCHOOL_APP_BASE_URL, "
    "so a School registered a minute ago already has one, and blank rather than "
    "half-built when the slug or the setting is missing."
)

M01_FR008_ACCEPTANCE_TAIL = (
    " ConsoleSchoolLogoTests proves the list and detail hand the console a logo "
    "address it can fetch with no credentials, that a School with no logo gets "
    "an empty value, and that the logo column costs no query per School. "
    "SchoolAppUrlTests proves the address inserts the slug, keeps a development "
    "port, and is blank for a missing slug, a missing setting or a host with no "
    "scheme."
)

M01_FR008_LIMIT_LEAD = (
    "app_url follows SCHOOL_APP_BASE_URL, which defaults to the production host, "
    "so an environment that does not set it shows its operators production "
    "addresses. Nothing else is outstanding."
)

M01_FR009_LOGO_FROM = "The uploaded logo is bound to the school's branding record"

M01_FR009_LOGO = (
    "The uploaded logo is bound to the school's branding record and to the "
    "school's own tenant, so reading it back is answered by that binding rather "
    "than by knowledge of its file name. The School's own profile hands its "
    "people a URL signed for one reader that expires. The crest alone is also "
    "public, deliberately: GET /v1/i/public/schools/{slug}/logo/ serves it with "
    "no session, because the sign-in page at the School's own address has no "
    "reader by definition and is where somebody decides whether they are at "
    "their own School. That route never lists Schools; performs the lookup "
    "sign-in performs, so a tenant that cannot sign in cannot be probed; picks "
    "the bytes from the address's own branding row, so a caller cannot point it "
    "at another School's storage; answers a School with no crest, an address "
    "nobody holds, a School that cannot sign in and the platform tenant with the "
    "same 404; lets a shared cache hold the image for an hour; and is throttled "
    "at 240 requests an hour. The console reads the same public address "
    "(FR-008), and the invitation, password-reset and onboarding emails sign "
    "their header with it (Module 8). A School's own administrator reads and "
    "edits the profile through /v1/i/me/profile/, on school.profile.view and "
    "school.profile.update, through a subclass of this serializer that drops the "
    "address and branding fields, and sets or clears the logo through the "
    "multipart /v1/i/me/profile/logo/, which is audited as a configuration "
    "change on the School's trail."
)

M01_FR009_ACCEPTANCE = (
    "A material supported change is saved and the endpoint returns the "
    "refreshed School detail payload. PublicSchoolLogoTests proves the crest is "
    "served with no session and may be cached publicly, that a School with no "
    "crest and an address that is not a School answer alike, that a tenant which "
    "cannot sign in cannot be probed, that the platform tenant is not a School, "
    "and that one School's address never serves another's crest. "
    "SchoolProfileEndpointTests proves a pending School reads and writes its own "
    "profile, that name, slug and code are shown and never accepted, and that a "
    "Branch administrator may read and not change it; SchoolLogoEndpointTests "
    "proves a logo is set, refused when it is not an image or is too large, "
    "cleared idempotently, refused to a Branch administrator and recorded."
)

M01_FR009_LIMIT = (
    "A branding-only request to the platform update endpoint is still rejected "
    "as 'No changes detected' before branding is upserted, because the change "
    "counter runs over direct School fields and branding is popped off the "
    "payload before it is counted (Section 9); a School's own administrator can "
    "change the logo alone through /v1/i/me/profile/logo/. The display name is "
    "not writable at all (Section 9). Favicon and broader theme fields do not "
    "exist. The crest is readable by anybody who knows a School's address, which "
    "is its purpose; what the public route withholds is whether an address "
    "belongs to a School at all."
)

M01_FR010_TYPE_FROM = (
    "so a Branch whose name or type was left blank by a path outside these "
    "serializers"
)
M01_FR010_TYPE_TO = (
    "so a Branch with a column left blank by a path outside these serializers"
)

M01_FR010_EVIDENCE_TAIL = (
    " A Branch carries no type. The free-text descriptor nothing read is dropped "
    "from the model (vs_tenants 0010), from the five serializers that carried it "
    "and from the read-only branch_type alias, so no Branch payload returns one "
    "and a request still sending one has the key ignored; what a Branch teaches "
    "is recorded, plurally, by the academic programmes run at it (Module 13). A "
    "School's own people read their Branches, read-only, through "
    "/v1/i/me/branches/ and /v1/i/me/branches/{code}/ on school.branches.view, "
    "scoped to the asserted tenant with no School identifier to tamper with. The "
    "list is on the pending-tenant surface, because academic structure is built "
    "before go-live and scopes records to a Branch; the detail read is not."
)

M01_FR010_ACCEPTANCE_TAIL = (
    " MyBranchesTests proves a School reads its own Branches main first, cannot "
    "see another School's, receives 404 for a code it does not have, and has no "
    "way to write; PendingSchoolBranchesTests proves a School that is not yet "
    "live reads the list while the detail stays shut."
)

M01_FR013_EVIDENCE_TAIL = (
    " The School service switch and the School's own logo route write to the "
    "same School trail, keyed on the primary key."
)

M01_FR015_EVIDENCE_TAIL = (
    " Platform staff take a live School out of service and back through POST "
    "/v1/i/{slug}/service-state/, on IsVisionStaff and platform.schools.manage. "
    "School.change_service_state moves ACTIVE to INACTIVE with a required "
    "reason, or INACTIVE back to ACTIVE, and refuses every other edge, so a "
    "School still onboarding or suspended is left to Module 9. It writes through "
    "the same save and therefore the same Tenant transition, so every account at "
    "the School is refused sign-in the moment it commits; it leaves every Branch "
    "status as it was, so a return restores the arrangement the School had, main "
    "Branch included; and it is audited on the School's trail with the reason in "
    "the summary."
)

M01_FR015_ACCEPTANCE_TAIL = (
    " SchoolServiceStateTests proves a school account cannot switch a School "
    "off, that taking a School out of service stops its users signing in and "
    "returning it lets them back, that the Branches are left exactly as they "
    "were, that the reason is required on the way out and written into the "
    "trail, and that a School still onboarding or suspended is refused here."
)

M01_FR015_LIMIT = (
    "Beyond that service switch there is no suspension or reactivation for "
    "commercial reasons, no DELETED state, no soft-delete field, and no guarded "
    "delete service. Normal LoginSession rows and their tokens are not revoked "
    "by a transition, although a non-authenticable Tenant still blocks their "
    "requests. Direct School deletion leaves its Tenant and every Branch "
    "standing with no School identity."
)

M01_SEQUENCE_SCHOOL = (
    "Create branding, the four whole-School roles CodeX ships, and the required "
    "primary School administrator link, account, staff record, grant and "
    "invitation."
)

M01_SEQUENCE_BRANCHES = (
    "Create the submitted Branches, initial lifecycle rows, a Branch Admin role "
    "for each, primary admin links, staff records, grants, and invitations. At "
    "least one is always present and exactly one is main. A person already "
    "provisioned in the request is granted at the new posting rather than "
    "skipped."
)

M01_BRANDING_CONTRACT = (
    "One-to-one School; logo only. Read by the School's people through a URL "
    "signed for one reader, and by anybody through the public crest route at "
    "the School's address"
)

M01_SCHOOL_LIST_PURPOSE = (
    "Paginated School list, search, filters, ordering, stable IDs, and each "
    "School's logo"
)
M01_SCHOOL_DETAIL_PURPOSE = (
    "School detail, the School ID, where the School's own app is served, and "
    "nested records"
)
M01_PLAN_CHANGE_PURPOSE = (
    "Move the school onto another plan and re-grant every module in the same "
    "transaction, taking back the role grants the new tier does not reach. The "
    "plan it is already on is refused."
)

M01_NEW_SCHOOL_ROUTES = [
    ["GET", "/v1/i/public/schools/{slug}/logo/", "None; throttled at 240 an hour",
     "The School's crest, before sign-in. The same 404 for no crest, an address "
     "nobody holds, a School that cannot sign in and the platform tenant"],
    ["GET, PATCH", "/v1/i/me/profile/", "school.profile.view / school.profile.update",
     "The School's own profile, the fields it still has to fill, and those it "
     "may edit"],
    ["POST, DELETE", "/v1/i/me/profile/logo/", "school.profile.update",
     "Set or clear the School's own logo, multipart, audited on the School's "
     "trail"],
]

M01_SERVICE_STATE_ROUTE = [
    "POST", "/v1/i/{slug}/service-state/", "IsVisionStaff and platform.schools.manage",
    "Take a live School out of service with a reason, or return it; Branch "
    "statuses untouched",
]

M01_NEW_BRANCH_ROUTES = [
    ["GET", "/v1/i/me/branches/", "school.branches.view",
     "The School's own Branches, read-only, main first then by code; open before "
     "go-live"],
    ["GET", "/v1/i/me/branches/{code}/", "school.branches.view",
     "One of the School's own Branches; a code it does not have answers 404"],
]

M01_NESTED_BRANCH_RULE = (
    "At least one Branch is required and an empty list is refused; exactly one "
    "main Branch, with a lone Branch promoted to main; unique names in payload. "
    "A Branch has no type, and a request still sending one has the key ignored"
)

M01_FAL_DIRECTION = "Outbound provisioning and read"
M01_FAL_CONTRACT = (
    "Provision and resolve a school's set of books. It is the only place this "
    "module's school vocabulary meets the finance engines, and the reason this "
    "module holds no finance model beyond reading back the entity it was given."
)

M01_TENANTS_CONTRACT = (
    "Create one Tenant per School; provide ambient tenant context and SC "
    "document numbering; derive where a School's own app is served, from "
    "SCHOOL_APP_BASE_URL, for the detail app_url and for every school user's "
    "activation and reset link."
)

M01_RBAC_CONTRACT = (
    "Evaluate platform permissions; provision the roles CodeX ships with every "
    "School and the School and Branch administrator assignments, each role "
    "copying the prebuilt library's permissions when it is made; take back the "
    "grants a lower plan tier no longer reaches through the audited role-access "
    "service. Fresh installations receive the school_admin and branch_admin "
    "prebuilt templates from the school-app migration, while existing or "
    "deactivated rows are preserved; teacher, finance_admin and procurement_admin "
    "come from seed_prebuilt_role_templates."
)

M01_STAFF_DEPENDENCY = [
    "schools/vs_staff", "Outbound write",
    "Write the staff record for every administrator provisioned, inside the "
    "provisioning savepoint, so an administrator is on the School's staff list; "
    "a person who already holds one is not given a second.",
]

M01_VERIFICATION_ANCHOR = "• A configuration reset refused for an arbitrary token"
M01_VERIFICATION_BULLET = (
    "• The roles CodeX ships arriving with a new School, a Branch role naming "
    "its Branch only once there are two, a head teacher who runs the only "
    "Branch holding both grants, every administrator on the staff list, a plan "
    "downgrade taking back what the new tier cannot reach, the public crest "
    "answering an unknown address exactly as a School with no crest, and the "
    "service switch leaving every Branch as it was."
)

M01_P0_LIFECYCLE = (
    "Add suspension and reactivation for commercial reasons, recoverable soft "
    "deletion, guarded hard deletion, normal login-session effects, and audit "
    "evidence for them. Onboarding expiry and manual reinstatement cover the "
    "onboarding case, and platform staff can take a live School out of service "
    "and back through the service switch; each closes active impersonation "
    "sessions transactionally."
)

M01_BRANDING_GAP_TITLE = "A branding-only update on the platform endpoint is refused"
M01_BRANDING_GAP = (
    "Accept a request to the platform update endpoint that changes only the "
    "logo. It is rejected as 'No changes detected' before branding is upserted, "
    "because the change counter runs over direct School fields and branding is "
    "popped off the payload before it is counted. A School's own administrator "
    "is not blocked, because /v1/i/me/profile/logo/ sets the logo on its own; "
    "the refusal stands on the console's path. The audit half of this item is "
    "resolved and has left with it: School update and configuration reset emit "
    "before/after evidence, a slug move is raised to WARNING and named in its "
    "summary, and an audit failure can no longer roll the School's own change "
    "back."
)

M01_BRANDING_RECORD_STATE = (
    "Partial; logo only, and the platform update endpoint still refuses a "
    "logo-only change, which the School's own logo route accepts"
)

M01_ADMIN_CAPABILITY_STATE = (
    "Implemented. Required administrator creation is atomic with its parent; one "
    "person named at more than one posting, the School administrator's own "
    "Branch included, receives every scoped grant from one account and one "
    "invitation, and every administrator is given a staff record. The "
    "administrator link records whether the invitation email was accepted for "
    "delivery rather than merely asked for, and the activation link lands on the "
    "School's own app. Delivery and activation remain separate lifecycle steps."
)

M01_BRANDING_FILES_STATE = (
    "Implemented; the logo is bound to the school's branding record and to that "
    "school's tenant. The School's own people read it through a URL signed for "
    "one reader that expires. The crest alone is public by the School's address, "
    "for the sign-in page, the console and the email header: the route lists "
    "nothing, reads only that address's own branding row, and answers a School "
    "with no crest exactly as it answers an address nobody holds."
)

M01_NEW_CAPABILITIES = [
    ["The roles CodeX ships, provisioned with every school", "FR-001, FR-006",
     "Implemented; School Admin, Teacher, Finance Admin and Procurement Admin for "
     "the whole School and a Branch Admin for each Branch, copied from the "
     "prebuilt library when each is made. A Branch role names its Branch only "
     "once the School has two. Teacher, Finance Admin and Procurement Admin "
     "depend on the library being seeded."],
    ["A plan downgrade takes back the role grants the new tier does not reach",
     "FR-007",
     "Implemented; every revocation goes through the audited role-access service "
     "naming the plan change, and the School's permission groups are untouched. "
     "Withdrawing or outliving an uplift, and re-applying a plan, do not revoke."],
]

M01_TRACEABILITY_LEAD = (
    f"This table reconciles every Module 1 capability entry in MRD v{MRD_VERSION} "
    "to the controlling functional requirement in this FRD. All twenty-three "
    "entries are represented. Two are new: the roles CodeX ships with every "
    "School, and the role grants a lower plan tier takes back. The administrator "
    "entry now covers every posting one person holds and the staff record each "
    "administrator is given, and the branding-files entry records that the "
    "crest alone is public by the School's address."
)

M01_CHANGE_SUMMARY = (
    "Reconciles Module 1 with the School creation, plan and branding work the "
    "narrow revisions since v1.19 left out. A new School received two of the "
    "five roles CodeX ships, School Admin and a Branch Admin per Branch, and the "
    "other three only if somebody later ran a command, so Schools created days "
    "apart held different sets; creation now provisions School Admin, Teacher, "
    "Finance Admin and Procurement Admin for the whole School and a Branch Admin "
    "for each Branch, and a Branch role names its Branch only once the School "
    "has a second one, renaming its siblings at that moment. vs_tenants 0009 "
    "renames the Branches an old console default named with a word the product "
    "does not use, and the Branch Admin roles derived from them, to Main Branch. "
    "A head teacher named as their own Branch's administrator in the same "
    "request had that link stamped SENT and no Branch grant written; they now "
    "hold both from one account and one invitation. Every administrator is "
    "written a staff record at provisioning, so a School's own administrators "
    "are on its staff list, and the activation link lands on the School's own "
    "app rather than on the Console. Moving a School down a plan tier left its "
    "roles holding keys the new tier no longer reached, refused at the door and "
    "invisible to the administrator who would remove them; the plan change now "
    "takes them back through the audited role-access service and leaves "
    "permission groups alone. FR-009 said the crest is served to nobody outside "
    "the School, which stopped being true when the sign-in page began reading it "
    "by the School's address; the public route, the console's use of it, "
    "app_url, and the School's own profile, logo and Branch routes are recorded, "
    "as is the operator service switch, which section 7 never listed. The Branch "
    "type is gone from the model and every serializer, and both passages that "
    "still called it optional are corrected. The finance-layer dependency row, "
    "which carried a copy of the vs_tenants contract, is corrected. FR-001, "
    "FR-004, FR-006 through FR-010, FR-013 and FR-015, the module decision, the "
    "endpoint gates, the creation sequence, the data model, all three endpoint "
    "tables, "
    "the validation rules, the dependencies, the verification list, two Needs "
    "Attention items and MRD traceability are updated. Module 1 stays Backend "
    "Partial and In use Complete, with twenty-three capability entries against "
    f"MRD v{MRD_VERSION}. Covered by the tests named in each requirement; the "
    "suites were not re-run for this revision. Backend evidence only; nothing "
    "here is deployed."
)


def patch_m01(source: Path, output: Path) -> None:
    doc = Document(str(source))
    title = (
        "XVS M01 School and Branch Management Functional Requirements "
        f"Document v{M01_TARGET}"
    )
    t = {name: doc.tables[index] for name, index in M01_TABLES.items()}

    replace_cover_version(doc.tables[0], M01_SOURCE, M01_TARGET)
    control = doc.tables[1]
    replace_control_value(control, "Version", M01_TARGET)
    replace_control_value(control, "Review date", REVIEW_DATE)
    replace_control_value(control, "Code baseline", CODE_BASELINE)
    replace_control_value(
        control, "Source MRD",
        f"XVS Module Requirements Document v{MRD_VERSION} | Module 1",
    )
    replace_control_value(control, "Supporting apps", M01_SUPPORTING_APPS)

    def edit_decision(lines):
        if not lines[1].startswith("• Module 1 remains Backend Partial"):
            raise ValueError("Decision box count line not found")
        if not lines[3].startswith("• The defining blocker remains"):
            raise ValueError("Decision box blocker line not found")
        if not lines[4].startswith("• The existing creation, administrator"):
            raise ValueError("Decision box unchanged-controls line not found")
        # The unchanged-controls line named a Branch ceiling that no longer
        # exists and described one revision rather than the module, so it goes.
        return [
            lines[0], M01_DECISION_COUNT, lines[2], M01_DECISION_BLOCKER,
            *lines[5:], *M01_DECISION_ADDITIONS,
        ]

    rewrite_box(t["decision"], edit_decision)
    separate_from_previous_table(t["decision"])
    forbid_row_split(t["decision"].rows[0])
    forbid_row_split(t["reset_box"].rows[0])
    for prefix in M01_OWNERSHIP_BULLETS:
        find_paragraph(doc, prefix).paragraph_format.keep_with_next = True

    anchor = find_row(t["gates"], "Reset School configuration")
    for values in M01_NEW_GATES:
        clone_row(t["gates"], anchor, values)
    restripe(t["gates"])

    fr001 = require_fr(t["FR-001"], "FR-001")
    rewrite(fr001.rows[EVIDENCE_ROW].cells[1], M01_FR001_EVIDENCE)
    rewrite(fr001.rows[ACCEPTANCE_ROW].cells[1], M01_FR001_ACCEPTANCE)
    rewrite(fr001.rows[LIMIT_ROW].cells[1], M01_FR001_LIMIT)

    fr004 = require_fr(t["FR-004"], "FR-004")
    append_text(fr004.rows[EVIDENCE_ROW].cells[1], M01_FR004_EVIDENCE_TAIL)
    append_text(fr004.rows[ACCEPTANCE_ROW].cells[1], M01_FR004_ACCEPTANCE_TAIL)
    append_text(fr004.rows[LIMIT_ROW].cells[1], M01_FR004_LIMIT_TAIL)

    fr006 = require_fr(t["FR-006"], "FR-006")
    append_text(fr006.rows[EVIDENCE_ROW].cells[1], M01_FR006_EVIDENCE_TAIL)
    append_text(fr006.rows[ACCEPTANCE_ROW].cells[1], M01_FR006_ACCEPTANCE_TAIL)
    rewrite(fr006.rows[LIMIT_ROW].cells[1], M01_FR006_LIMIT)

    fr007 = require_fr(t["FR-007"], "FR-007")
    append_text(fr007.rows[EVIDENCE_ROW].cells[1], M01_FR007_EVIDENCE_TAIL)
    append_text(fr007.rows[ACCEPTANCE_ROW].cells[1], M01_FR007_ACCEPTANCE_TAIL)
    append_text(fr007.rows[LIMIT_ROW].cells[1], M01_FR007_LIMIT_TAIL)

    fr008 = require_fr(t["FR-008"], "FR-008")
    append_text(fr008.rows[EVIDENCE_ROW].cells[1], M01_FR008_EVIDENCE_TAIL)
    append_text(fr008.rows[ACCEPTANCE_ROW].cells[1], M01_FR008_ACCEPTANCE_TAIL)
    swap_text(fr008.rows[LIMIT_ROW].cells[1], "None outstanding.", M01_FR008_LIMIT_LEAD)

    fr009 = require_fr(t["FR-009"], "FR-009")
    evidence = fr009.rows[EVIDENCE_ROW].cells[1]
    text = evidence.text
    cut = text.find(M01_FR009_LOGO_FROM)
    if cut < 0:
        raise ValueError("FR-009 logo passage not found")
    rewrite(evidence, text[:cut] + M01_FR009_LOGO)
    rewrite(fr009.rows[ACCEPTANCE_ROW].cells[1], M01_FR009_ACCEPTANCE)
    rewrite(fr009.rows[LIMIT_ROW].cells[1], M01_FR009_LIMIT)

    fr010 = require_fr(t["FR-010"], "FR-010")
    evidence = fr010.rows[EVIDENCE_ROW].cells[1]
    swap_text(evidence, M01_FR010_TYPE_FROM, M01_FR010_TYPE_TO)
    append_text(evidence, M01_FR010_EVIDENCE_TAIL)
    append_text(fr010.rows[ACCEPTANCE_ROW].cells[1], M01_FR010_ACCEPTANCE_TAIL)

    fr013 = require_fr(t["FR-013"], "FR-013")
    append_text(fr013.rows[EVIDENCE_ROW].cells[1], M01_FR013_EVIDENCE_TAIL)

    fr015 = require_fr(t["FR-015"], "FR-015")
    append_text(fr015.rows[EVIDENCE_ROW].cells[1], M01_FR015_EVIDENCE_TAIL)
    append_text(fr015.rows[ACCEPTANCE_ROW].cells[1], M01_FR015_ACCEPTANCE_TAIL)
    rewrite(fr015.rows[LIMIT_ROW].cells[1], M01_FR015_LIMIT)

    rewrite(find_row(t["sequence"], "3. School records").cells[1], M01_SEQUENCE_SCHOOL)
    rewrite(find_row(t["sequence"], "4. Branches").cells[1], M01_SEQUENCE_BRANCHES)

    rewrite(find_row(t["model"], "SchoolBranding").cells[2], M01_BRANDING_CONTRACT)

    school_api = t["school_api"]
    rewrite(find_row(school_api, "/v1/i/", col=1, exact=True).cells[3],
            M01_SCHOOL_LIST_PURPOSE)
    detail = find_row(school_api, "/v1/i/{slug}/", col=1, exact=True)
    rewrite(detail.cells[3], M01_SCHOOL_DETAIL_PURPOSE)
    plan_change = next(
        row for row in school_api.rows
        if row.cells[0].text.strip() == "PATCH"
        and row.cells[1].text.strip() == "/v1/i/{slug}/plan/"
    )
    rewrite(plan_change.cells[3], M01_PLAN_CHANGE_PURPOSE)
    list_row = find_row(school_api, "/v1/i/", col=1, exact=True)
    for values in M01_NEW_SCHOOL_ROUTES:
        clone_row(school_api, list_row, values)
    # The new routes sit above the School detail row, in the order of the URLconf.
    for values in M01_NEW_SCHOOL_ROUTES:
        row = find_row(school_api, values[1], col=1, exact=True)
        detail._tr.addprevious(row._tr)
    clone_row(school_api, school_api.rows[-1], M01_SERVICE_STATE_ROUTE, before=False)
    restripe(school_api)

    branch_api = t["branch_api"]
    for values in M01_NEW_BRANCH_ROUTES:
        clone_row(branch_api, branch_api.rows[-1], values, before=False)
    restripe(branch_api)

    rewrite(find_row(t["validation"], "Nested Branches").cells[1], M01_NESTED_BRANCH_RULE)

    dependencies = t["dependencies"]
    fal = find_row(dependencies, "Finance abstraction layer")
    rewrite(fal.cells[1], M01_FAL_DIRECTION)
    rewrite(fal.cells[2], M01_FAL_CONTRACT)
    rewrite(find_row(dependencies, "vs_tenants", exact=True).cells[2], M01_TENANTS_CONTRACT)
    rewrite(find_row(dependencies, "vs_rbac", exact=True).cells[2], M01_RBAC_CONTRACT)
    clone_row(dependencies, find_row(dependencies, "vs_config", exact=True),
              M01_STAFF_DEPENDENCY)
    restripe(dependencies)

    insert_paragraph_after(
        find_paragraph(doc, M01_VERIFICATION_ANCHOR), M01_VERIFICATION_BULLET,
    )

    needs = t["needs"]
    rewrite(find_row(needs, "P0", exact=True).cells[2], M01_P0_LIFECYCLE)
    branding_gap = find_row(needs, "A branding-only School update is refused", col=1)
    rewrite(branding_gap.cells[1], M01_BRANDING_GAP_TITLE)
    rewrite(branding_gap.cells[2], M01_BRANDING_GAP)

    traceability = t["traceability"]
    rewrite(find_row(traceability, "School branding and logo record").cells[2],
            M01_BRANDING_RECORD_STATE)
    rewrite(find_row(traceability, "Create first School and Branch administrators").cells[2],
            M01_ADMIN_CAPABILITY_STATE)
    rewrite(find_row(traceability, "Authorised, expiring access to school branding files").cells[2],
            M01_BRANDING_FILES_STATE)
    for values in M01_NEW_CAPABILITIES:
        clone_row(traceability, traceability.rows[-1], values, before=False)
    restripe(traceability)
    replace_body_paragraph(
        doc, "This table reconciles every Module 1 capability", M01_TRACEABILITY_LEAD,
    )

    prepend_change_log(t["change_log"], M01_TARGET, M01_CHANGE_SUMMARY)
    finish(doc, output, title, M01_TARGET)


# ═════════════════════════════════════════════════════════════════════════════
# Module 8 - Notifications & Delivery
# ═════════════════════════════════════════════════════════════════════════════

M08_DIR = "08-notifications-and-delivery"
M08_STEM = "XVS_M08_Notifications_and_Delivery_Functional_Requirements_Document"
M08_SOURCE, M08_TARGET = "1.9", "1.10"

M08_TABLES = {
    "scope": 3, "FR-001": 7, "FR-006": 12, "FR-007": 13, "FR-010": 16,
    "FR-013": 19, "FR-014": 20, "feed_api": 25, "dependencies": 28, "traceability": 32,
    "change_log": 33,
}

M08_EMAIL_DESIGN_SCOPE = (
    "One shared, email-client-safe layout that composes the visual from the "
    "message text, resolves the sender's name and logo at delivery, and allows a "
    "per-template override to replace it."
)

M08_FR001_EVIDENCE_TAIL = (
    " ticket.escalated is registered for Module 31 with in-app and email "
    "templates. It fires when a school hands one of its own tickets up to CodeX "
    "and is addressed to the platform desk."
)

M08_FR006_EVIDENCE_TAIL = (
    " The header mark resolves from the same key family as the sender name, in "
    "the same order: issuer_logo_url, school_logo_url, entity_logo_url and "
    "tenant_logo_url, then the layout's own brand_logo_url, so a caller that "
    "names a School as the sender brands its mark with the same vocabulary and "
    "the two cannot describe different senders. Where no usable logo is "
    "supplied the header keeps the platform initials. Invitation, "
    "password-reset and onboarding callers supply the School's public crest "
    "address, built from API_PUBLIC_BASE_URL."
)

M08_FR006_ACCEPTANCE_TAIL = (
    " StoredTemplateMarkTests proves one stored document signs each School with "
    "its own mark and falls back to the initials where a School has none; "
    "ContextKeyFamilyTests proves the logo travels under the same names as the "
    "sender, in the same order, and that render places the resolved logo where "
    "the stored document reads it."
)

M08_FR007_EVIDENCE_TAIL = (
    " A logo address is checked where it is resolved as well as where a "
    "document is composed. Stored markup carries the logo placeholder inside the "
    "image source and is substituted at send time, so a check made only at "
    "composition never runs on the path that delivers mail; anything but an "
    "absolute http or https address, including a javascript: or data: value, a "
    "relative path and a protocol-relative one, is refused and the header keeps "
    "the platform initials."
)

M08_FR007_ACCEPTANCE_TAIL = (
    " EmailBrandMarkTests proves only an absolute http or https address becomes "
    "an image and that a URL cannot break out of the image attribute; "
    "ContextKeyFamilyTests proves a value that cannot load is refused rather "
    "than written."
)

M08_FR010_EVIDENCE_TAIL = (
    " Each record carries an action_url built from its event and metadata, and "
    "acknowledging a route marks read what that destination covers. A finished "
    "background job's task.completed or task.failed notice is linked through "
    "the job's own kind and target rather than its event key, which names only "
    "the outcome: the task tracker records the target on the job and passes kind "
    "and target as metadata, an import or its rollback opens its batch page, and "
    "visiting that page acknowledges the job notices about that batch. A job "
    "kind with no record page of its own is left unlinked rather than pointed at "
    "a list."
)

M08_FR010_ACCEPTANCE_TAIL = (
    " BackgroundJobActionUrlTests proves an import job and its rollback link to "
    "the same batch and that the batch route acknowledges only the notices about "
    "that batch."
)

M08_FR010_LIMIT = (
    "Only an import and its rollback have a record page for a job notice to "
    "open; every other job kind's notice is unlinked."
)

M08_FR014_EVIDENCE_TAIL = (
    " Ticket escalation, raised by a school and addressed to the platform desk "
    "through the same support recipients, is owned the same way, so the "
    "escalating school's delivery history never returns the desk's copies."
)

M08_ACKNOWLEDGE_ROUTE = (
    "Mark read what the visited destination covers, including a finished import "
    "job's notices when its batch page is visited."
)

M08_TRACKER_CONTRACT = (
    "Records every Celery job and applies the default importance policy before "
    "raising task.completed or task.failed, and carries the job's kind and the "
    "record it is about so the notice can link to it."
)

M08_TICKETS_DEPENDENCY = [
    "Module 31, Support Tickets",
    "Raises the ticket events, including ticket.escalated when a school hands "
    "one of its own tickets up to CodeX. The platform desk's copies are owned by "
    "the desk's own tenant.",
]

M08_TRACEABILITY_LEAD = (
    f"Module 8 carries 22 capability entries in MRD v{MRD_VERSION}. Each maps to "
    "the requirements below. Linking a finished import job's notice to its "
    "batch strengthens notification acknowledgement, the sender's logo in the "
    "header and the check on its address strengthen HTML email rendering and "
    "value escaping, and ticket escalation adds an event to the existing "
    "catalogue, without changing the count."
)

M08_CHANGE_SUMMARY = (
    "Records three changes that reached the notification engine after v1.9. A "
    "finished background job's notice reported an outcome with no way to reach "
    "it; the task tracker now records the record a job is about, and an import "
    "or its rollback links its task.completed or task.failed notice to the batch "
    "page, which acknowledges those notices when visited, while a job kind with "
    "no record page stays unlinked. The standard email header signed every "
    "message with the platform initials beside a School's name, because the "
    "logo was read from one internal key almost no caller passed; it now "
    "resolves from the same key family as the sender name, and the address is "
    "checked where it is resolved, since stored markup substitutes it inside the "
    "image source at send time and the composition-time check never ran on the "
    "delivery path, so a javascript: or data: value reached the attribute. "
    "ticket.escalated is registered for Module 31 with in-app and email "
    "templates and is addressed to the platform desk. Scope, FR-001, FR-006, "
    "FR-007, FR-010, FR-014, the acknowledge-route contract, the task-tracker "
    "and Module 31 dependencies and MRD traceability are updated. Module 8 "
    "remains Backend Complete and In use Complete with 22 capabilities against "
    f"MRD v{MRD_VERSION}. Covered by BackgroundJobActionUrlTests and the "
    "brand-mark tests in tests_email_brand_mark; the suites were not re-run for "
    "this revision. Backend evidence only; nothing here is deployed."
)


def patch_m08(source: Path, output: Path) -> None:
    doc = Document(str(source))
    title = (
        "XVS M08 Notifications and Delivery Functional Requirements Document "
        f"v{M08_TARGET}"
    )
    t = {name: doc.tables[index] for name, index in M08_TABLES.items()}

    replace_cover_version(doc.tables[0], M08_SOURCE, M08_TARGET)
    control = doc.tables[1]
    replace_control_value(control, "Version", M08_TARGET)
    replace_control_value(control, "Review date", REVIEW_DATE)
    replace_control_value(control, "Code baseline", CODE_BASELINE)
    replace_control_value(
        control, "Source MRD",
        f"XVS Module Requirements Document v{MRD_VERSION} | Module 8",
    )

    rewrite(find_row(t["scope"], "The email design").cells[1], M08_EMAIL_DESIGN_SCOPE)

    fr001 = require_fr(t["FR-001"], "FR-001")
    append_text(fr001.rows[EVIDENCE_ROW].cells[1], M08_FR001_EVIDENCE_TAIL, default=8.5)

    fr006 = require_fr(t["FR-006"], "FR-006")
    append_text(fr006.rows[EVIDENCE_ROW].cells[1], M08_FR006_EVIDENCE_TAIL, default=8.5)
    append_text(fr006.rows[ACCEPTANCE_ROW].cells[1], M08_FR006_ACCEPTANCE_TAIL, default=8.5)

    fr007 = require_fr(t["FR-007"], "FR-007")
    append_text(fr007.rows[EVIDENCE_ROW].cells[1], M08_FR007_EVIDENCE_TAIL, default=8.5)
    append_text(fr007.rows[ACCEPTANCE_ROW].cells[1], M08_FR007_ACCEPTANCE_TAIL, default=8.5)

    fr010 = require_fr(t["FR-010"], "FR-010")
    append_text(fr010.rows[EVIDENCE_ROW].cells[1], M08_FR010_EVIDENCE_TAIL, default=8.5)
    append_text(fr010.rows[ACCEPTANCE_ROW].cells[1], M08_FR010_ACCEPTANCE_TAIL, default=8.5)
    if fr010.rows[LIMIT_ROW].cells[1].text.strip() != "None.":
        raise ValueError("FR-010 limit is no longer 'None.'")
    rewrite(fr010.rows[LIMIT_ROW].cells[1], M08_FR010_LIMIT, default=8.5)

    # FR-010 grows by a paragraph, which strands FR-013's heading and status row
    # at the foot of a page unless the short table travels as one piece.
    keep_rows_together(require_fr(t["FR-013"], "FR-013"))

    fr014 = require_fr(t["FR-014"], "FR-014")
    append_text(fr014.rows[EVIDENCE_ROW].cells[1], M08_FR014_EVIDENCE_TAIL, default=8.5)

    rewrite(find_row(t["feed_api"], "POST /notify/acknowledge-route/").cells[1],
            M08_ACKNOWLEDGE_ROUTE)

    dependencies = t["dependencies"]
    rewrite(find_row(dependencies, "core task tracker").cells[1], M08_TRACKER_CONTRACT)
    clone_row(dependencies, find_row(dependencies, "Modules 22 and 23"),
              M08_TICKETS_DEPENDENCY)

    traceability = t["traceability"]
    rewrite(find_row(traceability, "HTML email rendering").cells[1],
            "FR-005, FR-006, FR-007")
    replace_body_paragraph(doc, "Module 8 carries", M08_TRACEABILITY_LEAD)

    prepend_change_log(t["change_log"], M08_TARGET, M08_CHANGE_SUMMARY)
    finish(doc, output, title, M08_TARGET)


# ═════════════════════════════════════════════════════════════════════════════
# Module 9 - School Onboarding
# ═════════════════════════════════════════════════════════════════════════════

M09_DIR = "09-school-onboarding"
M09_STEM = "XVS_M09_School_Onboarding_Functional_Requirements_Document"
M09_SOURCE, M09_TARGET = "2.10", "2.11"

M09_TABLES = {
    "boundary": 4, "out_of_scope": 6, "FR-005": 14, "FR-006": 15,
    "journey": 26, "dependencies": 34, "change_log": 41,
}

M09_PURPOSE_NOTE = (
    "Version 2.11 corrects the checklist to the five steps the catalog carries, "
    "four for a School whose plan does not reach bulk import, and names the "
    "DEFAULT_ROLES card where the first-administrator and role-baseline checks "
    "sit together. It also records what Module 1 hands this module at creation: "
    "the roles CodeX ships and a staff record for each administrator."
)

M09_BOUNDARY = (
    "CURRENT CREATION, READINESS, AND COMMUNICATION BOUNDARY\n"
    "• Module 1 returns 503 and rolls the whole new School transaction back when "
    "its required administrator cannot be provisioned. Module 9 receives no "
    "half-created School to repair.\n"
    "• Module 1 creates the roles CodeX ships with every School, School Admin, "
    "Teacher, Finance Admin and Procurement Admin for the whole School and a "
    "Branch Admin for each Branch, and a staff record for each administrator. "
    "DEFAULT_ROLES reads only the School Admin role and its whole-tenant "
    "assignment, so neither changes what it checks.\n"
    "• DEFAULT_ROLES still checks later invitation activation, account or "
    "assignment liveness, and that the School Admin role grants authority before "
    "go-live. An invitation email that never reached the broker is re-sent by "
    "Module 3 rather than waiting for an operator to notice.\n"
    "• A School completes the five-step catalog, which drops to four only for a "
    "School whose plan does not reach bulk import, and submits a request. "
    "Platform staff alone approve, reject, activate, and reinstate; the ready "
    "email states that hand-off accurately.\n"
    "• Six email and in-app events cover progress, readiness, review, "
    "activation, expiry warning, and platform follow-up. Human-readable local "
    "dates sit beside the older machine-readable context for customized "
    "templates.\n"
    "• Onboarding expiry and reinstatement still use the shared locked Tenant "
    "transition service. Suspension closes active impersonation sessions in the "
    "same transaction, and reinstatement never revives them."
)

M09_OUT_OF_SCOPE_AREA = (
    "Creating the school, its tenant, its branches, its roles, its first "
    "administrator and its package"
)
M09_OUT_OF_SCOPE_OWNER = (
    "Module 1, School and Branch Management. Onboarding verifies the result and "
    "never re-creates it. The required administrator, the roles CodeX ships and "
    "each administrator's staff record are part of the School transaction, and "
    "creation is refused when administrator provisioning fails."
)

M09_FR005_EVIDENCE_FROM = " and renumbers the remaining seven to 1-7."
M09_FR005_EVIDENCE_TO = (
    ", and 0003 merges the two administrator steps into DEFAULT_ROLES and "
    "removes the books step, so the catalog's five steps carry order indexes 1 "
    "to 5."
)
M09_FR005_ACCEPTANCE_FROM = "carry order indexes 1 to 7."
M09_FR005_ACCEPTANCE_TO = "carry the catalog's order indexes, 1 to 5."

M09_FR006_EVIDENCE = (
    "Module 1 raises ADMIN_PROVISIONING_FAILED and rolls the School transaction "
    "back when required administrator provisioning fails. DEFAULT_ROLES, the "
    "Confirm Default Roles & RBAC card, is refused unless both of its checks "
    "hold: an active tenant user holding an active whole-tenant school_admin "
    "assignment with no Branch, and the tenant's school_admin role carrying at "
    "least one granted permission. They were separate steps, FIRST_ADMIN and "
    "ROLE_BASELINE, until vs_onboarding 0003 merged each School's pair, taking "
    "the weaker of the two statuses so no School is marked as having confirmed a "
    "baseline nobody confirmed, and the refusal names whichever half failed. "
    "Module 1 also provisions Teacher, Finance Admin, Procurement Admin and a "
    "Branch Admin for each Branch at creation, and writes each administrator's "
    "staff record; neither changes what DEFAULT_ROLES reads. Module 1 leaves the "
    "administrator link QUEUED until the invitation email hand-off settles, and "
    "Module 3 re-sends an invitation the broker refused, so a broker outage "
    "during creation delays DEFAULT_ROLES rather than stranding it behind an "
    "email nobody knew was missing."
)

M09_FR006_ACCEPTANCE = (
    "A failed creation attempt leaves no School or onboarding state. After a "
    "successful creation, an unactivated invitation, later-disabled account or "
    "assignment, Branch-pinned administrator, or empty school_admin role keeps "
    "DEFAULT_ROLES incomplete and blocks go-live."
)

M09_FR006_LIMIT = (
    "This module does not retry Module 1 creation because no half-created School "
    "remains. A refused email hand-off is retried by Module 3's scheduled "
    "re-send, so what still needs the invitation operations is an invitation "
    "that was delivered and not acted on. Activation remains the administrator's "
    "own step, and DEFAULT_ROLES cannot complete without it."
)

M09_JOURNEY_CREATION = (
    "School, tenant, at least one Branch, the roles CodeX ships, and the "
    "required administrator account, staff record, scoped role assignment, and "
    "invitation record exist, all committed together. The School and tenant are "
    "PENDING. Books and the onboarding control room are best effort inside their "
    "own savepoints."
)

M09_MODULE1_CONTRACT = (
    "Creates the School, Tenant, at least one Branch, the roles CodeX ships, the "
    "required administrator and a staff record for each administrator, and "
    "package. Supplies SUSPENDED and routes the paired Tenant lifecycle through "
    "the shared transition service, so expiry written through School.status "
    "carries the same transactional proxy shutdown."
)

M09_TRACEABILITY_LEAD = (
    f"Module 9 carries 21 capability entries in MRD v{MRD_VERSION}. Each maps to "
    "the requirements below. The roles and staff records Module 1 provisions at "
    "creation change nothing this module checks, so no entry or count changes."
)

M09_CHANGE_SUMMARY = (
    "Brings the document back to the checklist the code runs and records what "
    "Module 1 now hands over at creation. The creation boundary still described "
    "a seven-step catalog and FR-006 still named FIRST_ADMIN and ROLE_BASELINE "
    "as steps, although vs_onboarding 0003 collapsed the catalog to five and "
    "merged those two checks into the one DEFAULT_ROLES card, which is refused "
    "unless both hold; FR-005's order indexes are corrected to match. Module 1 "
    "now provisions the roles CodeX ships with every School, School Admin, "
    "Teacher, Finance Admin and Procurement Admin for the whole School and a "
    "Branch Admin for each Branch, grants a head teacher who also runs the "
    "School's only Branch at both postings, and writes a staff record for each "
    "administrator; DEFAULT_ROLES reads only the School Admin role and its "
    "whole-tenant assignment, so none of that changes what it checks. The "
    "purpose note, the creation boundary, section 1.2, FR-005, FR-006, the "
    "creation workflow, the Module 1 dependency and MRD traceability are "
    "updated. Module 9 stays Backend Complete and In use Partial with "
    f"twenty-one capability entries against MRD v{MRD_VERSION}. No code in this "
    "module changed. Backend evidence only; nothing here is deployed."
)


def patch_m09(source: Path, output: Path) -> None:
    doc = Document(str(source))
    title = (
        f"XVS M09 School Onboarding Functional Requirements Document v{M09_TARGET}"
    )
    t = {name: doc.tables[index] for name, index in M09_TABLES.items()}

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

    replace_body_paragraph(doc, "Version 2.8 makes the six onboarding", M09_PURPOSE_NOTE)

    def edit_boundary(lines):
        if not lines[0].startswith("CURRENT CREATION, READINESS"):
            raise ValueError("Boundary box heading not found")
        return M09_BOUNDARY.split("\n")

    rewrite_box(t["boundary"], edit_boundary)

    creating = find_row(t["out_of_scope"], "Creating the school")
    rewrite(creating.cells[0], M09_OUT_OF_SCOPE_AREA)
    rewrite(creating.cells[1], M09_OUT_OF_SCOPE_OWNER)

    fr005 = require_fr(t["FR-005"], "FR-005")
    swap_text(fr005.rows[EVIDENCE_ROW].cells[1], M09_FR005_EVIDENCE_FROM,
              M09_FR005_EVIDENCE_TO, default=8.5)
    swap_text(fr005.rows[ACCEPTANCE_ROW].cells[1], M09_FR005_ACCEPTANCE_FROM,
              M09_FR005_ACCEPTANCE_TO)

    fr006 = require_fr(t["FR-006"], "FR-006")
    rewrite(fr006.rows[EVIDENCE_ROW].cells[1], M09_FR006_EVIDENCE, default=8.5)
    rewrite(fr006.rows[ACCEPTANCE_ROW].cells[1], M09_FR006_ACCEPTANCE, default=8.5)
    rewrite(fr006.rows[LIMIT_ROW].cells[1], M09_FR006_LIMIT, default=8.5)

    rewrite(find_row(t["journey"], "1", exact=True).cells[2], M09_JOURNEY_CREATION)
    rewrite(find_row(t["dependencies"], "Module 1, School and Branch Management").cells[1],
            M09_MODULE1_CONTRACT)

    replace_body_paragraph(doc, "Module 9 carries", M09_TRACEABILITY_LEAD)

    prepend_change_log(t["change_log"], M09_TARGET, M09_CHANGE_SUMMARY)
    finish(doc, output, title, M09_TARGET)


# ═════════════════════════════════════════════════════════════════════════════

PATCHES = {
    "1": (M01_DIR, M01_STEM, M01_SOURCE, M01_TARGET, patch_m01),
    "8": (M08_DIR, M08_STEM, M08_SOURCE, M08_TARGET, patch_m08),
    "9": (M09_DIR, M09_STEM, M09_SOURCE, M09_TARGET, patch_m09),
}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", default=Path(__file__).resolve().parents[1])
    parser.add_argument("--modules", nargs="*", default=sorted(PATCHES))
    args = parser.parse_args()
    root = Path(args.root) / "functional-requirements"

    for module in args.modules:
        folder, stem, source, target, patch = PATCHES[module]
        directory = root / folder
        patch(
            directory / f"{stem}_v{source}.docx",
            directory / f"{stem}_v{target}.docx",
        )
        print(f"Wrote {folder} v{target}")


if __name__ == "__main__":
    main()

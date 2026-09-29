#!/usr/bin/env python3
"""Cut M03 v1.19, M05 v1.7 and M26 v1.3: the client IP address, and the queue each owes.

What changed in the backend, and therefore in the documents:

* The client IP address is decided once per request (15d9ac3d). Every reader
  had taken the left of X-Forwarded-For, which the caller writes, so a made-up
  value bought a fresh allowance from each per-IP throttle, landed in sign-in
  history, lockouts, identity events and the export download log, and, when it
  was not an address at all, failed the lockout's write so the wrong password
  went uncounted. core.client_ip.ClientIPMiddleware, first in MIDDLEWARE,
  writes REMOTE_ADDR from settings.CLIENT_IP_HEADERS (CF-Connecting-IP, then
  True-Client-IP, in the staging settings Render runs), DRF throttles key on
  REMOTE_ADDR (NUM_PROXIES 0), and one get_client_ip serves every record.
* A lockout is not a status (3e4e79af): AccountLockout is the only record, a
  window is fixed when it opens and releases itself, one unlock clears it and
  never the status, and a completed reset clears it.
* The sign-in response and /auth/me/ carry branch_reach (f6359e25).
* A past CX staff profile names the organisation of that day (e420d602), and
  the audit vocabulary holds the five Field Access types, guarded by a source
  scan and by record_rbac_audit refusing an unregistered type.
* A school or branch tightens its own security values (bf517f8b).
* Notification settings and template changes are audited (570410e1).
* A role's Field Access switches decide account fields and export columns
  (4767f637).

M26 also records D67 (6c4853b0 and a992a8e6, merged 0ea6a97c): the payments
collections and payouts datasets narrow to the exporting caller's branches, and
a scope with no caller narrows nothing. M26 alone carries the later review date
and baseline that entry brings.

M05's FR-005 also stops describing the lookup AuditEvent.save no longer makes
(c0ddd633), and M03's FR-016 stops naming a test class that does not exist.
M05's traceability holds one row per MRD entry: FR-018 maps to entity-level
audit trails, where it had a row of its own under the visibility label.

The MRD is not touched by this script.

    python tools/patch_client_ip_identity_audit_exports_docs.py
"""
from __future__ import annotations

from docx import Document

from patch_document_type_labels_docs import require_newest
from patch_record_history_docs import (
    ROOT,
    add_fr,
    append_rows,
    finish,
    frd_path,
    keep_format,
    log_change,
    set_control,
    set_cover_version,
    table_with_header,
)
from patch_restricted_grant_ladder_docs import (
    append_to,
    assert_absent_outside_log,
    edit_box,
    insert_row_after,
    retitle_paragraph,
    table_headed,
)
from patch_school_organogram_docs import table_headed_prefix
from patch_staff_id_and_auth_events_docs import (
    edit_cell,
    edit_paragraph,
    edit_value,
    fr_table,
    insert_after,
    normalise_change_log,
    remove_row_containing,
    renumber,
    repair_ooxml,
    row,
    set_status,
    set_value,
)

import patch_record_history_docs

REVIEW_DATE = "27 September 2026"
MRD_VERSION = "2.93"
CODE_BASELINE = (
    "Backend main at e12e7fbf, which carries the client IP address decided once (15d9ac3d), "
    "27 September 2026"
)

patch_record_history_docs.REVIEW_DATE = REVIEW_DATE


def newest_is(folder: str, stem: str, source: str) -> None:
    require_newest(str(ROOT / "functional-requirements" / folder / f"{stem}_v*.docx"), source)


def open_source(folder: str, stem: str, source: str, target: str):
    newest_is(folder, stem, source)
    doc = Document(str(frd_path(folder, stem, source)))
    set_cover_version(doc, source, target)
    set_control(doc, "Version", target)
    set_control(doc, "Review date", REVIEW_DATE)
    set_control(doc, "Code baseline", CODE_BASELINE)
    return doc


def close(doc, folder: str, stem: str, target: str, summary: str) -> None:
    log_change(doc, target, summary)
    repair_ooxml(doc)
    normalise_change_log(doc)
    finish(doc, frd_path(folder, stem, target), f"{stem.replace('_', ' ')} v{target}", target)


#: Where the address comes from, said the same way in all three documents.
ADDRESS_SOURCE = (
    "The client IP address is decided once per request by core.client_ip: on Render, the "
    "address Cloudflare saw connect (CF-Connecting-IP, then True-Client-IP, both of which "
    "Cloudflare overwrites); elsewhere, the peer address. X-Forwarded-For is never read, "
    "because the caller writes it."
)


# ── M03 Identity, Team & Organogram ──────────────────────────────────────────

M03_DIR = "03-identity-team-and-organogram"
M03_STEM = "XVS_M03_Identity_Team_and_Organogram_Functional_Requirements_Document"
M03_SOURCE, M03_TARGET = "1.18.1", "1.19"

M03_FR026 = [
    ("Requirement", (
        "Every throttle, record and lockout that keys on where a request came from must read a "
        "client IP address the caller cannot choose. A forwarding header is the caller's to "
        "write: an address read from one gives a fresh throttle allowance to every made-up "
        "value, puts any address the caller likes into another person's sign-in history, and, "
        "when the value is not an address at all, fails the lockout's write so the wrong "
        "password goes uncounted."
    )),
    ("Current evidence", (
        "core.client_ip.ClientIPMiddleware runs first in MIDDLEWARE and writes the address into "
        "REMOTE_ADDR once per request. settings.CLIENT_IP_HEADERS names the headers a trusted "
        "edge writes, in order: the staging settings, which render.yaml runs for the web "
        "service and the worker, name CF-Connecting-IP and then True-Client-IP, which "
        "Cloudflare, in front of every Render service, overwrites with the address that "
        "connected to it. A value that does not parse as an IP address is ignored and the next "
        "header tried; with none valid the peer address stands. Everywhere else the setting is "
        "empty and the peer address is the answer. X-Forwarded-For is never read. DRF's "
        "throttles key on REMOTE_ADDR alone (NUM_PROXIES 0), so the login, password_reset, "
        "activation, login_preview and card throttles each bound one real caller. "
        "core.client_ip.get_client_ip, which this module's audit, auth and password services "
        "import, reads the same value into AuthAttempt, AccountLockout.last_failure_ip, "
        "LoginSession, PasswordResetRequest's requesting address and the ip_address in every "
        "identity event's metadata. It is always a valid address, so the lockout's write "
        "cannot be failed by what the caller sends."
    )),
    ("Acceptance", (
        "LoginClientAddressTests: a new X-Forwarded-For on every sign-in buys no new "
        "allowance, the third attempt at two a minute answering 429; a malformed forwarding "
        "header still counts toward the lockout, and the lockout and the attempt record the "
        "peer address; behind Cloudflare the session and the attempt record CF-Connecting-IP "
        "and not a forwarded value. core's client-address tests prove a forged forwarding "
        "header changes nothing, the Cloudflare headers are ignored where no edge is trusted, "
        "True-Client-IP is the fallback, an invalid value falls through to the next header or "
        "the peer, an IPv6 address is normalised, and the middleware runs first; the "
        "deployment settings test proves the staging settings trust exactly the two "
        "Cloudflare headers."
    )),
    ("Current limit", (
        "The trust is a fact about the deployment, held in its settings module. A deployment "
        "behind a different proxy must name that proxy's header, or every caller shares the "
        "proxy's address and so one throttle budget, and a whole school's sign-ins would be "
        "refused at five a minute. On Render a request carrying neither Cloudflare header as a "
        "valid address is keyed on Render's internal peer address, shared with every other "
        "such request. Rows recorded before this baseline hold the address the caller sent and "
        "are not corrected."
    )),
]

M03_FR027 = [
    ("Requirement", (
        "A client offering a branch choice must know which branches the signed-in person may "
        "work in, not which branches the school runs, or it offers a choice the server then "
        "ignores."
    )),
    ("Current evidence", (
        "The sign-in response and GET /auth/me/ carry branch_reach, built by "
        "vs_rbac.scoping.branch_reach_payload from the visible_branch_ids every branch-scoped "
        "read uses. It has three shapes: whole_tenant true with an empty branch_ids when "
        "nothing narrows the person; whole_tenant false with branch_ids naming exactly the "
        "branches their grants reach; and whole_tenant false with an empty branch_ids when "
        "every granted branch has been withdrawn, which means no branch rows at all, never the "
        "whole school. While a platform operator is proxying, /auth/me/ is answered for the "
        "proxied person, so branch_reach describes the target and not the operator. The branch "
        "list endpoint still answers which branches the school runs."
    )),
    ("Acceptance", (
        "BranchReachPayloadTests: a whole-school grant reads as the whole tenant; grants at "
        "Ikeja and Lekki out of three branches name exactly those two; a withdrawn branch "
        "leaves an empty reach rather than the whole tenant; /auth/me/ carries the reader's "
        "own reach. vs_rbac's override tests pin the /auth/me/ key set with branch_reach in it."
    )),
    ("Current limit", (
        "branch_reach is read when the response is built. A grant changed afterwards reaches "
        "the client at its next /auth/me/ or sign-in."
    )),
]

M03_TEST_EVIDENCE = (
    "Evidence: the client-address tests ran at a5bae05e, core 20 and vs_user 3, all OK. The "
    "other changes were verified when they were committed, each app on its own: the lockout "
    "by vs_user 405, schools.vs_staff 234 and vs_rbac 549; branch reach by vs_user 414 and "
    "vs_rbac 848, one pinned key set corrected and its class rerun; the organisation as at a "
    "day by vs_user 421 and vs_rbac 868; the school's security settings by "
    "schools.vs_schools 412 and vs_config 117; all OK. Nothing here claims deployment."
)

M03_SUMMARY = (
    "Minor revision. Adds FR-026: every throttle, record and lockout that keys on where a "
    "request came from reads a client IP address the caller cannot write. core.client_ip "
    "decides it once per request, from Cloudflare's CF-Connecting-IP and then True-Client-IP "
    "on Render and from the peer address elsewhere, and nothing reads X-Forwarded-For. Before "
    "this a made-up header bought a fresh throttle allowance, wrote any address into sign-in "
    "history, and, when it was not an address, failed the lockout's write so the wrong "
    "password went uncounted. Adds FR-027: the sign-in response and /auth/me/ carry "
    "branch_reach in three shapes, describing the proxied person while proxying. Records that "
    "a lockout is not a status: AccountLockout is its only record, User.account_state reports "
    "LOCKED over the stored status while the window runs, the window is fixed when it opens "
    "and releases itself, one unlock clears the lockout and never the status, a completed "
    "reset clears it, and vs_user migration 0012 freed the accounts already stuck. Gap 9.2 is "
    "removed and gaps 9.3 to 9.7 become 9.2 to 9.6; FR-010 moves from Partial to Implemented "
    "with limits. A past CX staff profile names the unit, department, division and line "
    "manager of that day (FR-023), and the P3 gap that read them from today is removed. The "
    "account security and invitation facts on an account are Field Access switches on "
    "platform.team, and /auth/me/ carries field_access. A school or branch may tighten its "
    "own security values (section 1.2). Corrects FR-016's acceptance, which named a test "
    "class that does not exist, and FR-002's gap reference. FR-024 and section 7.4 stop "
    "saying two staff numbers may differ only in case: Module 12 now refuses the pair "
    "(447a6f0e). " + M03_TEST_EVIDENCE
)


def patch_m03() -> None:
    doc = open_source(M03_DIR, M03_STEM, M03_SOURCE, M03_TARGET)

    # 1.1 and 1.2
    scope = table_with_header(doc, "Area")
    edit_value(scope, "Sign-in", "it resolves only inside the platform tenant.",
               "it resolves only inside the platform tenant. The response names the branches "
               "the person may work in (FR-027).")
    set_value(scope, "Brute force", (
        "Every attempt recorded as AuthAttempt whether or not the address exists; a per-account "
        "failure counter and a lockout window read from configuration, which releases itself "
        "when it ends; and throttles keyed on a client IP address the caller cannot write "
        "(FR-026)."))
    set_value(scope, "Account lifecycle", (
        "Draft, pending approval, pending activation, active, suspended, deactivated, rejected - "
        "with the transitions allowed between them and the session effect of each. Locked is "
        "reported over any of them while a lockout runs, and is never stored."))
    elsewhere = table_with_header(doc, "Not owned here")
    append_to(elsewhere, "Lockout thresholds and expiry windows",
              " A school, or one of its branches, may tighten any of these five and the proxy "
              "idle timeout below its parent and never loosen one, through Module 1's "
              "/v1/i/me/settings/security/ under school.settings.view and school.settings.update. "
              "No config.* key is school-holdable.")

    # 3 Actors
    actors = table_headed(doc, "Actor", "May do", "Governed by")
    edit_cell(row(actors, "Anybody with the address or card").cells[-1],
              "Rate limited by throttle scope,",
              "Rate limited by throttle scope, keyed on a client IP address the caller cannot "
              "write (FR-026),")
    append_to(actors, "Tenant administrator",
              " Which account security and invitation facts they see on an account (password "
              "last changed, last signed in, invited by, and the invitation email's status and "
              "expiry) is the Field Access read switch on each of those platform.team fields, "
              "all five sensitive, so closed unless a role switches them on, and none writable.")

    # FR-002, FR-008, FR-024: references
    edit_value(fr_table(doc, "FR-002"), "Current limit", "See gap 9.7.", "See gap 9.5.")
    edit_value(fr_table(doc, "FR-008"), "Current evidence",
               "one on the caller's IP address (login_preview and login)",
               "one on the caller's IP address as FR-026 decides it (login_preview and login)")
    fr024 = fr_table(doc, "FR-024")
    edit_value(fr024, "Current limit", "and the IP throttle bound it",
               "and the IP throttle (FR-026) bound it")
    edit_value(fr024, "Current evidence",
               "The match ignores case; stored uniqueness is case-sensitive, so two numbers "
               "differing only in case are ambiguous and sign nobody in.",
               "The match ignores case, and so does the uniqueness Module 12 keeps "
               "(uq_staff_number_per_tenant_ci), so a school cannot hold two numbers differing "
               "only in case; a number matching two rows would still sign nobody in.")
    edit_value(fr024, "Acceptance",
               "two Brightfield numbers differing only in case sign nobody in;",
               "a second Brightfield number differing only in case cannot be created;")

    # FR-010: a lockout is one row
    fr010 = fr_table(doc, "FR-010")
    set_status(doc, fr010, "Implemented with limits")
    set_value(fr010, "Current evidence", (
        "AccountLockout is one row per user holding failure_count, locked_until, locked_reason "
        "and the last failing time and client IP address, and it is the only record of a "
        "lockout: no status is written. User.account_state reports LOCKED over the stored status "
        "while locked_until is in the future, so a lockout ends when its window does with nobody "
        "touching a row. register_failure increments under select_for_update and opens a window "
        "once failed_login_threshold is reached, both values read from configuration per tenant "
        "and branch. The window is fixed when it opens: a further wrong password inside it is "
        "counted and does not push the expiry out, and a closed window is spent, so the next "
        "failure starts a fresh count. Opening a window emits ACCOUNT_LOCKED with a null actor. "
        "A lockout ends no session, because the person guessing holds none and ending one would "
        "only reach the legitimate holder. record_attempt writes an AuthAttempt row outside "
        "every transaction so it survives the raised refusal, and is called on the scope "
        "failure, the school-binding failure, the bad password, the block and the success "
        "alike. Both rows take the client IP address from FR-026, which is always a valid "
        "address, so a malformed header can no longer fail the lockout's write and leave the "
        "wrong password uncounted."))
    set_value(fr010, "Acceptance", (
        "The lockout write and the attempt write are in separate atomic blocks from the "
        "caller's so neither can be rolled back by an exception handler. A successful sign-in "
        "clears the counter under a row lock. FailedAttemptAuditTests covers the failure path. "
        "LockoutOpensAndClosesTests proves the threshold locks, the right password is refused "
        "inside the window, the window passing is the whole of the release, a further wrong "
        "password does not push it back, and a spent window gives back the whole allowance. "
        "ALockoutDoesNotEndALiveSessionTests proves a signed-in teacher keeps working. "
        "LoginClientAddressTests proves a malformed forwarding header still counts."))
    set_value(fr010, "Current limit", (
        "The counter is per account; password spraying across many accounts is bounded only by "
        "the DRF throttle scope, keyed per client IP address (FR-026). No user.account_locked "
        "notification is dispatched, and the Module 8 catalogue event remains inactive."))

    # FR-013: a completed reset clears a lockout
    fr013 = fr_table(doc, "FR-013")
    edit_value(fr013, "Current evidence",
               "writes the password and consumes the row in one transaction.",
               "writes the password and consumes the row in one transaction. Confirmation also "
               "clears any lockout on the account, whatever its status, because the credential "
               "being guessed no longer exists; a PENDING account becomes ACTIVE and a SUSPENDED "
               "one stays suspended.")
    append_to(fr013, "Acceptance",
              " APasswordResetEndsALockoutTests proves a completed reset clears the lockout.")

    # FR-015, FR-017: the recorded address
    edit_value(fr_table(doc, "FR-015"), "Current evidence",
               "LoginSession stores the refresh token's JTI, the address, the agent",
               "LoginSession stores the refresh token's JTI, the client IP address (FR-026), "
               "the agent")
    edit_value(fr_table(doc, "FR-017"), "Current evidence",
               "and the IP address, agent, tenant slug",
               "and the client IP address (FR-026), agent, tenant slug")

    # FR-016: status is what an administrator decided
    fr016 = fr_table(doc, "FR-016")
    edit_value(fr016, "Current evidence",
               "User._sync_is_active derives the Django flag from status, with LOCKED as the "
               "temporary exception.",
               "User._sync_is_active derives the Django flag from status, the same way for every "
               "status. LOCKED is never stored: account_state reports it over the stored status "
               "while a lockout runs (FR-010). UserStatusService.unlock is the one unlock, called "
               "by the platform endpoint, the school's account action and the lockout console "
               "alike; it clears the lockout row and never touches status, so a suspended "
               "account that was also guessed at stays suspended. Module 12's directory count, "
               "chip and ?account_status= filter read the same lockout_in_force predicate, so "
               "Locked and every stored status filter to disjoint sets. vs_user migration 0012 "
               "freed the accounts that had carried LOCKED, sorting them by evidence into "
               "ACTIVE, PENDING or SUSPENDED.")
    set_value(fr016, "Acceptance", (
        "The status-gate suite proves every status against may_sign_in and may_hold_password, "
        "and that the login, reset, activation and request gates agree (StatusModelIsTotalTests, "
        "SignInStatusGateTests, RequestGateTests). DRAFT, PENDING_APPROVAL and REJECTED cannot "
        "receive a password or sign in. ALockoutDoesNotTouchTheAccountTests proves a suspension "
        "survives being guessed at; AnAdministratorStillDecidesTests proves every unlock route "
        "clears the same state and leaves the status alone; "
        "TheMigrationFreesTheAccountsAlreadyStuckTests covers migration 0012."))
    set_value(fr016, "Current limit", (
        "There is no operator-level suspend of all accounts for a tenant, which is the "
        "school-lifecycle gap shared by Modules 1, 2 and 3."))

    # FR-023: the organisation of that day
    fr023 = fr_table(doc, "FR-023")
    append_to(fr023, "Current evidence",
              " The organisation around the seat is composed for the same day: the seat is the "
              "primary one PositionAssignment shows held that day, a tenure ending on a day being "
              "over that day; the unit, department and division are that seat's unit and its "
              "ancestors as each stood; and the line manager is whoever held the seat it "
              "reported to. OrgNode and Position are tracked for their shape, and "
              "vs_history.TrackingStart records when tracking reached them.")
    append_to(fr023, "Acceptance",
              " OrganisationAsAtTests: the line manager is whoever held the seat that day and the "
              "department the one the seat sat in; before the organisation's history the unit "
              "and manager are left empty; the live profile still reads today's organisation.")
    set_value(fr023, "Limit", (
        "Before the organisation's tracking start the unit, department, division and line "
        "manager are null and as_at.organisation_history_starts names the first day they are "
        "known; none is filled in from today's organisation. The seat is still named then, by "
        "its current title, since its own history does not reach back. A photograph replaced "
        "since is not shown. Correcting a school account's name is governed by no platform "
        "switch."))

    add_fr(doc, "FR-026  Take the Client IP Address From a Source the Caller Cannot Write",
           "FR-026 | Implemented", M03_FR026)
    add_fr(doc, "FR-027  Tell a Session Which Branches Its Person May Work In",
           "FR-027 | Implemented", M03_FR027)
    set_status(doc, fr_table(doc, "FR-026"), "Implemented")
    set_status(doc, fr_table(doc, "FR-027"), "Implemented")

    # 5.2 and 5.3
    order = table_headed(doc, "Order", "Check", "Why it is here and not elsewhere")
    edit_cell(row(order, "4").cells[-1], "After the password on purpose.",
              "After the password on purpose. A running window refuses as ACCOUNT_LOCKED "
              "whatever the status; one that has passed refuses nothing.")
    keep_format(row(order, "5").cells[-1],
                "Only ACTIVE is admitted: SIGN_IN_STATUSES is an allowlist, so a status added "
                "later signs nobody in until it is admitted. PENDING, SUSPENDED and DEACTIVATED "
                "receive their own refusal, as does a row still carrying LOCKED; DRAFT, "
                "PENDING_APPROVAL and REJECTED receive the generic INVALID_CREDENTIALS response.")
    statuses = table_headed(doc, "Status", "Reached from", "Sign-in", "Sessions")
    keep_format(row(statuses, "ACTIVE").cells[1],
                "Activation, reactivation, or a reset completed from PENDING. Unlocking does not "
                "change a status.")
    locked = row(statuses, "LOCKED")
    keep_format(locked.cells[1],
                "Never stored. Reported by User.account_state over any status while a lockout "
                "window runs, and no longer once it ends.")
    keep_format(locked.cells[2],
                "Refused: ACCOUNT_LOCKED until the window passes; then the stored status decides.")
    keep_format(locked.cells[3],
                "Left alone, and still usable: the person guessing holds none, so ending one "
                "would only reach the legitimate holder.")

    # 6 Data model
    model = table_headed(doc, "Model", "Holds", "Notes")
    append_to(model, "User",
              " status is what an administrator decided; LOCKED is never stored in it, and "
              "account_state reports it while a lockout runs.")
    edit_cell(row(model, "LoginSession").cells[-1], "Carries the refresh JTI, the address,",
              "Carries the refresh JTI, the client IP address (FR-026),")
    append_to(model, "AuthAttempt", " The client IP address is the one FR-026 decides.")
    keep_format(row(model, "AccountLockout").cells[-1],
                "OneToOne, and the only record of a lockout. Failure count, locked_until, reason, "
                "and the last failing time and client IP address (FR-026). is_locked_now() is a "
                "comparison against the clock, and lockout_in_force() is the same comparison for "
                "a queryset, which Module 12's directory count, chip and ?account_status= filter "
                "read.")
    edit_cell(row(model, "PasswordResetRequest").cells[-1], "requesting address and agent",
              "requesting client IP address (FR-026) and agent")
    edit_cell(row(model, "PlatformStaffProfile").cells[-1],
              "employment and three payroll fields gated by FLS.",
              "employment and three payroll fields, each under a Field Access switch (FR-018, "
              "FR-023).")

    # 7 Routes and answers
    auth_routes, account_routes = [t for t in doc.tables
                                   if t.rows[0].cells[0].text.strip() == "Method and path"][:2]
    edit_value(auth_routes, "POST /auth/login/",
               "Returns access and session context,",
               "Returns access and session context, including field_access and branch_reach "
               "(FR-027),")
    edit_value(auth_routes, "POST /auth/login/",
               "AllowAny, throttled per IP address",
               "AllowAny, throttled per client IP address (FR-026)")
    keep_format(row(account_routes, "GET /auth/me/").cells[-1],
                "Own profile, effective permissions, tenant and school context, field_access "
                "(which fields the person may not see or change, by module and resource) and "
                "branch_reach (FR-027). Authentication only; while proxying, answered for the "
                "proxied person.")
    answers = table_with_header(doc, "Condition")
    staff_number_row = row(answers, "A staff number the named school does not hold, a staff "
                           "number with no tenant, or two numbers at that school differing only "
                           "in case")
    keep_format(staff_number_row.cells[0],
                "A staff number the named school does not hold, or a staff number with no tenant")
    append_rows(answers, [[
        "A staff profile field the caller's role may not change, or may not read, sent on a "
        "write",
        "403 field_write_denied naming every such field, and nothing is saved. A field the "
        "caller may not read is absent from every response, with no placeholder.",
    ]])

    # 8 Dependencies and verification
    deps = table_with_header(doc, "Dependency")
    append_to(deps, "Module 6, Configuration & Capability Management",
              " A school sets its own values, stricter only, through Module 1's school settings.")
    append_to(deps, "core",
              " Also decides the client IP address every throttle and record here reads "
              "(core.client_ip, FR-026).")
    edit_paragraph(doc, "vs_user ran 417 tests OK",
                   paragraph_text(doc, "vs_user ran 417 tests OK").split(". The client")[0],
                   "For this revision the client-address tests ran at a5bae05e, core 20 and "
                   "vs_user 3, all OK; the lockout, branch reach and as-at changes were "
                   "verified when committed, vs_user 405, 414 and 421 tests, each run on its "
                   "own, all OK")
    edit_paragraph(doc, "For this revision the client-address tests",
                   "For this revision the school client's typecheck",
                   "At v1.17 the school client's typecheck")
    retitle_paragraph(doc, "•  The lockout ordering proves",
                      "•  The lockout ordering proves the locked state is not disclosed before "
                      "the password is known, and the lockout is one row: its window releases "
                      "itself, is not pushed back, spends its count, leaves the status and a live "
                      "session alone, every unlock route clears the same state, a completed reset "
                      "clears it, and migration 0012 freed the accounts already stuck. Covered by "
                      "LoginLockoutOracleTests and the lockout expiry suite.")
    insert_after(doc, "•  Staff ID sign-in and reset at the naming school only",
                 "•  The client IP address behind every throttle and record: a forged forwarding "
                 "header buys no allowance and reaches no record, a malformed one still counts "
                 "toward the lockout, and behind Cloudflare the connecting address is recorded. "
                 "Covered by LoginClientAddressTests and core's client-address tests.")
    insert_after(doc, "•  The client IP address behind every throttle and record",
                 "•  branch_reach in its three shapes and on /auth/me/, and a past CX staff "
                 "profile's organisation as it stood. Covered by BranchReachPayloadTests and "
                 "OrganisationAsAtTests.")

    # 9 Needs Attention
    attention = table_with_header(doc, "Pri.")
    remove_row_containing(attention, "9.2 A temporary lockout is permanent in effect")
    remove_row_containing(attention, "A past CX staff view reads the organisation")
    for old, new in (("9.3 ", "9.2 "), ("9.4 ", "9.3 "), ("9.5 ", "9.4 "), ("9.6 ", "9.5 "),
                     ("9.7 ", "9.6 ")):
        renumber(attention, old, new)
    edit_paragraph(doc, "Three structural questions remain explicit.",
                   "which gap 9.5 records", "which gap 9.4 records")
    edit_paragraph(doc, "Three structural questions remain explicit.",
                   "which gap 9.3 records", "which gap 9.2 records")
    edit_paragraph(doc, "Three structural questions remain explicit.",
                   "which gap 9.6 records", "which gap 9.5 records")
    edit_value(fr_table(doc, "FR-017"), "Current limit", "See gap 9.5.", "See gap 9.4.")

    # 10 Traceability
    trace = table_headed(doc, "MRD Module 3 capability", "FRD coverage", "Current state")
    for capability, coverage, state in (
        ("Tenant-aware email and password login", "FR-006, FR-009, FR-024, FR-026, FR-027", (
            "Implemented. The tenant is resolved before the account and the lookup scoped to "
            "it, and a sign-in naming no tenant is refused. A school's staff may sign in with "
            "their staff ID, resolved inside that school only. Throttles key on a client IP "
            "address the caller cannot write, and the response names the branches the person "
            "may work in.")),
        ("Login-attempt tracking and account lockout", "FR-010, FR-026", (
            "Implemented with limits. A lockout is one row whose window is fixed when it opens "
            "and releases itself, counted against a client IP address the caller cannot write; "
            "no lockout notification is dispatched.")),
        ("Suspend, reactivate, and unlock user accounts", "FR-016", (
            "Implemented with limits. Transitions and targets are scoped, and a platform "
            "operator reaches an account on any tenant by id. Unlocking clears the lockout and "
            "never the status; there is no tenant-wide suspend.")),
        ("Platform staff profile read as at an earlier date", "FR-018, FR-023", (
            "Implemented with limits. The profile, its account and the organisation around the "
            "seat read as they stood; before the organisation's history starts the unit and "
            "line manager are left empty rather than taken from today.")),
    ):
        target = row(trace, capability)
        keep_format(target.cells[1], coverage)
        keep_format(target.cells[2], state)
    edit_box(table_headed_prefix(doc, "MRD RECONCILIATION").rows[0].cells[0], [
        ("sub", "• Partial remains correct",
         "the existing tenant lifecycle, lockout, retention, audit-model and organogram gaps "
         "remain.", "the existing tenant lifecycle, retention and audit-model gaps remain."),
        ("after", "• Partial remains correct",
         "• The client IP address, branch reach and a lockout that releases itself sit inside "
         "existing capabilities and add none."),
    ])

    assert_absent_outside_log(
        doc, "gap 9.7", "9.2 A temporary", "does not restore ACTIVE", "never releases",
        "with LOCKED as the temporary exception", "AccountStatusDecisionTableTests",
        "read from today's organisation", "reads as it stands", "gated by FLS",
        "from LOCKED or PENDING", "refused at the RBAC layer", "lockout, retention",
        "stored uniqueness is case-sensitive", "differing only in case sign nobody in",
    )
    close(doc, M03_DIR, M03_STEM, M03_TARGET, M03_SUMMARY)


def paragraph_text(doc, start: str) -> str:
    hits = [p.text for p in doc.paragraphs if p.text.strip().startswith(start)]
    if len(hits) != 1:
        raise ValueError(f"{start!r} starts {len(hits)} paragraphs")
    return hits[0]


# ── M05 Audit & Activity Logging ─────────────────────────────────────────────

M05_DIR = "05-audit-and-activity-logging"
M05_STEM = "XVS_M05_Audit_and_Activity_Logging_Functional_Requirements_Document"
M05_SOURCE, M05_TARGET = "1.6", "1.7"

M05_TEST_EVIDENCE = (
    "Evidence: the vocabulary guards were verified by their commit's runs (vs_audit 96 and "
    "vs_rbac 868 tests), the notification audit by vs_notifications (189), and the "
    "client-address tests ran at a5bae05e (core 20, vs_user 3, OK). Nothing here claims "
    "deployment."
)

M05_SUMMARY = (
    "Minor revision. The five Field Access action types are registered (vs_audit 0016), so a "
    "switch changed or reset, a personal exception created or lifted and the field catalogue "
    "sync reach this stream instead of being dropped: the Needs Attention item for "
    "FIELD_REGISTRY_SYNCED is removed and the vocabulary is recounted at 101 action types. "
    "record_rbac_audit refuses an unregistered type and a source scan fails on an unregistered "
    "literal, so FR-002's limit narrows to a type computed at run time. Notification settings "
    "and template changes are recorded as CONFIG_CHANGED with no second log, making seventeen "
    "writing surfaces, thirteen of them with this stream as their only record. Where the "
    "client IP address in an event's metadata comes from is stated: core.client_ip, never a "
    "forwarding header. FR-005 stops describing a lookup AuditEvent.save no longer makes. The "
    "reconciliation names MRD v2.93. The traceability table holds one row per entry, fourteen: "
    "a staff record's account history (FR-018) had a second row under the visibility "
    "controls label and is mapped to entity-level audit trails, and the record history entry "
    "takes the MRD's wording. " + M05_TEST_EVIDENCE
)


def patch_m05() -> None:
    doc = open_source(M05_DIR, M05_STEM, M05_SOURCE, M05_TARGET)
    set_control(doc, "Source MRD", f"XVS Module Requirements Document v{MRD_VERSION}")

    scope = table_with_header(doc, "Area")
    set_value(scope, "Change snapshots", (
        "The before state, a field-level diff, and free-form metadata such as the client IP "
        "address, agent and correlation ids. " + ADDRESS_SOURCE))
    edit_paragraph(doc, "The stream has two kinds of writer",
                   "academic structure and the calendar, and a document posted",
                   "academic structure and the calendar, notification settings and templates, "
                   "and a document posted")

    fr002 = fr_table(doc, "FR-002")
    edit_value(fr002, "Current evidence", "AuditActionType registers 96 actions",
               "AuditActionType registers 101 actions")
    edit_value(fr002, "Current evidence", "and the timetable, the student roll",
               "and the timetable, Field Access, the student roll")
    edit_value(fr002, "Current evidence",
               "ACADEMIC_TIMETABLE_PUBLISHED and POSTED_WITHOUT_APPROVAL have been registered "
               "since, with the STUDENT, STAFF and WORKFLOW module keys.",
               "ACADEMIC_TIMETABLE_PUBLISHED and POSTED_WITHOUT_APPROVAL have been registered "
               "since, with the STUDENT, STAFF and WORKFLOW module keys, and so have "
               "FIELD_ACCESS_CHANGED, FIELD_ACCESS_RESET, FIELD_OVERRIDE_CREATED, "
               "FIELD_OVERRIDE_LIFTED and FIELD_REGISTRY_SYNCED (vs_audit 0016). "
               "record_rbac_audit refuses a type the vocabulary does not hold before writing its "
               "durable row, and permission-group create, update and delete are recorded as "
               "CREATE, UPDATE and DELETE on entity_type permission_group rather than as dotted "
               "names.")
    append_to(fr002, "Acceptance",
              " LiteralActionTypesAreRegisteredTests scans every module outside tests and "
              "migrations for a string passed as action_type to emit_audit_event or "
              "record_rbac_audit, and fails on one the vocabulary does not hold; "
              "RecordRbacAuditRefusesUnknownTypesTests proves an unregistered RBAC type raises "
              "and writes nothing.")
    set_value(fr002, "Current limit", (
        "The refusal at the model is still silent: the helper swallows its own failures, so an "
        "unregistered type that reaches it produces no event and no error at the call site. Two "
        "checks stand in front of it, the source scan and record_rbac_audit's refusal, and a "
        "type computed at run time rather than written as a literal passes the first. "
        "POSTED_WITHOUT_APPROVAL and the five Field Access types show the cost: each was "
        "emitted before it was registered, and until then left a log line and no row."))

    edit_value(fr_table(doc, "FR-005"), "Current evidence",
               "AuditEvent.save raises if a row with that primary key already exists,",
               "AuditEvent.save refuses any instance loaded from the database, and a new "
               "instance carrying an existing primary key is refused by the primary key itself, "
               "so no lookup is made on insert,")

    writers = table_with_header(doc, "Module and code")
    edit_cell(row(writers, "M3 Identity & Auth (vs_user.services.audit)").cells[1],
              "An email change carries the administrator's note when one is given.",
              "An email change carries the administrator's note when one is given. The client "
              "IP address in metadata is the one core.client_ip decides for the request (Module "
              "3 FR-026), never a forwarding header.")
    edit_cell(row(writers, "M4 Roles & Permissions (vs_rbac.audit, signals, services)").cells[1],
              "role change request.",
              "role change request, and the five Field Access actions. An action type the "
              "vocabulary does not hold is refused before either row is written.")
    insert_row_after(row(writers, "M7 Workflow & Approval Engine (vs_workflow.services.resolution)"), [
        "M8 Notifications & Delivery (vs_notifications.services.audit)",
        "A notification setting changed at the platform, a tenant or a branch layer, one event "
        "per event type and channel whose stored value changed, and a template created or "
        "edited with its changed columns before and after, as CONFIG_CHANGED under the CONFIG "
        "module. Resending a stored value, or an edit that changes nothing, records nothing. No "
        "second log: the row keeps only who changed it last and when.",
        "Yes for a setting, passed by the caller, and none at the platform layer; a template, "
        "which is global, takes the request's",
    ])
    edit_paragraph(doc, "Sixteen surfaces write through emit_audit_event.",
                   "Sixteen surfaces write through emit_audit_event. Four of them keep their own "
                   "authoritative log and treat this stream as a mirror; for the other twelve",
                   "Seventeen surfaces write through emit_audit_event. Four of them keep their "
                   "own authoritative log and treat this stream as a mirror; for the other "
                   "thirteen")
    edit_paragraph(doc, "Seventeen surfaces write through emit_audit_event.",
                   "Every one of the sixteen records", "Every one of the seventeen records")

    insert_after(doc, "•  Every registered action type emitted once",
                 "•  Every literal action type handed to emit_audit_event or record_rbac_audit "
                 "is registered, by a scan of the source, and record_rbac_audit refuses an "
                 "unregistered one and writes nothing.")
    insert_after(doc, "•  Every literal action type handed to emit_audit_event",
                 "•  A notification setting or template change writes one CONFIG_CHANGED event "
                 "per real change and none for a resend that changes nothing "
                 "(NotificationChangeAuditTests).")

    attention = table_with_header(doc, "Pri.")
    remove_row_containing(attention, "sync_field_registry writes an action type")
    edit_cell(row_containing(attention, "A dropped event leaves nothing").cells[2],
              "For the twelve writers with no second log", "For the thirteen writers with no "
              "second log")
    edit_box(table_headed_prefix(doc, "REMOVAL RULE").rows[0].cells[0], [
        ("sub", "•  Nothing here is history.", "the current state of the code at 13675db.",
         "the current state of the code at e12e7fbf."),
    ])

    trace = table_headed(doc, "MRD Module 5 capability", "FRD coverage", "Current state")
    append_to_cell(row(trace, "Request metadata and correlation context").cells[2],
                   " The client IP address in it is one the caller could not choose.")
    fold_staff_account_history(trace)
    keep_format(row(trace, "Record history read as at an earlier day").cells[0],
                "Record history, read as at an earlier day")
    edit_paragraph(doc, "MRD v2.87 records Module 5", "MRD v2.87", f"MRD v{MRD_VERSION}")
    edit_box(table_headed_prefix(doc, "MRD RECONCILIATION").rows[0].cells[0], [
        ("sub", "•  MRD v2.87 lists Module 5", "MRD v2.87", f"MRD v{MRD_VERSION}"),
        ("sub", "•  The tenant half",
         "recounted at 96 action types, 17 module keys and sixteen writing surfaces",
         "recounted at 101 action types, 17 module keys and seventeen writing surfaces"),
        ("sub", "•  Record history, read as at an earlier day,",
         "taking the count to fourteen.",
         "taking the count to fourteen. A staff record's account history (FR-018) is one "
         "person's trail read under the staff module's key, so it maps to the entity-level "
         "audit trails entry and the table holds one row per entry."),
    ])

    assert_absent_outside_log(
        doc, "96 action", "sixteen writing", "the other twelve", "twelve writers",
        "13675db", "MRD v2.87", "a convention held by comments",
        "raises if a row with that primary key already exists",
        "sync_field_registry writes an action type",
    )
    close(doc, M05_DIR, M05_STEM, M05_TARGET, M05_SUMMARY)


def row_containing(table, text: str):
    hits = [r for r in table.rows if any(text in c.text for c in r.cells)]
    if len(hits) != 1:
        raise ValueError(f"{text!r} is in {len(hits)} rows")
    return hits[0]


def append_to_cell(cell, tail: str) -> None:
    keep_format(cell, cell.text.rstrip() + tail)


#: What FR-018 adds to the entity-level trail entry it maps to.
M05_STAFF_HISTORY = (
    " A school staff record's history reads that one person's account events under "
    "school.teachers.view, inside the tenant, returning the event, time, actor and an email "
    "change's note and no other metadata (FR-018)."
)


def fold_staff_account_history(trace) -> None:
    """Map FR-018 to the entity-level trail entry and drop the row that repeated a label.

    FR-018 arrived as a second row labelled with the visibility entry, so the
    table held fifteen rows for the MRD's fourteen entries. A staff record's
    account history is one person's trail, read under the staff module's key,
    which is the entity-level audit trails entry.
    """
    copies = [r for r in trace.rows
              if r.cells[0].text.strip() == "Tenant and platform visibility controls"]
    if len(copies) != 2 or copies[1].cells[1].text.strip() != "FR-018":
        raise ValueError("Expected the visibility label twice, the second mapping FR-018")
    copies[1]._tr.getparent().remove(copies[1]._tr)
    trails = row(trace, "Entity-level audit trails")
    keep_format(trails.cells[1], "FR-006, FR-007, FR-011, FR-018")
    append_to_cell(trails.cells[2], M05_STAFF_HISTORY)
    if len(trace.rows) - 1 != 14:
        raise ValueError(f"Module 5 traceability holds {len(trace.rows) - 1} rows, not 14")


# ── M26 Reporting & Exports ──────────────────────────────────────────────────

M26_DIR = "26-reporting-and-exports"
M26_STEM = "XVS_M26_Reporting_and_Exports_Functional_Requirements_Document"
M26_SOURCE, M26_TARGET = "1.2", "1.3"

M26_REVIEW_DATE = "28 September 2026"
M26_CODE_BASELINE = (
    "Backend main at 7860ea86, 28 September 2026, which carries the payments datasets narrowed "
    "to the exporting caller's branches (6c4853b0 and a992a8e6, merged at 0ea6a97c) and the "
    "client IP address decided once (15d9ac3d)"
)
M26_TEST_EVIDENCE = (
    "Evidence: ExportDownloadTests and FieldAccessGatesAColumnTests ran at a5bae05e, 13 "
    "tests, OK; test_the_exports_hold_only_rows_in_reach in vs_payments was read at 7860ea86 "
    "and not re-run. Nothing here claims deployment."
)

M26_SUMMARY = (
    "Minor revision. The download log records the client IP address each attempt came from, "
    "and says where it comes from: core.client_ip decides it once per request, from "
    "Cloudflare's CF-Connecting-IP and then True-Client-IP on Render and from the peer address "
    "elsewhere, and X-Forwarded-For is never read. Before this the log recorded whatever the "
    "caller wrote in that header (FR-013, sections 5.2, 6, 7 and 8). A "
    "column naming a field's Field Access key is offered and written only when the run's "
    "owner may read that field, and is otherwise reported as a FIELD_FORBIDDEN omission, "
    "beside the extra key for restricted columns (FR-014, sections 1.2, 5.1, 7.1 and 8). The "
    "payments collections and payouts datasets narrow to the exporting caller's branches, a "
    "collection by its customer and invoice and a payout by its vendor, and a scope with no "
    "caller narrows nothing (6c4853b0, a992a8e6; FR-016 and section 8). The "
    "control page and traceability name MRD v2.93, whose sixteen Module 26 entries are "
    "unchanged. " + M26_TEST_EVIDENCE
)


def patch_m26() -> None:
    doc = open_source(M26_DIR, M26_STEM, M26_SOURCE, M26_TARGET)
    set_control(doc, "Source MRD", f"XVS Module Requirements Document v{MRD_VERSION} | Module 26")
    set_control(doc, "Review date", M26_REVIEW_DATE)
    set_control(doc, "Code baseline", M26_CODE_BASELINE)

    fr016 = fr_table(doc, "FR-016")
    append_to(fr016, "Current evidence",
              " The rows a dataset yields are narrowed by the query its domain registers, many "
              "through narrow_to_caller_branches. The payments datasets, gateway collections and "
              "payout instructions (Module 18), narrow to the exporting caller's branches by the "
              "rule the payments screens use: a collection by its customer and by the invoice it "
              "settles, a payout by the vendor it pays, a payout naming no vendor being "
              "school-wide, and a caller covering the whole school narrowed not at all. A scope "
              "with no caller, a system-triggered estimate or the catalogue's own checks, "
              "narrows nothing, as narrow_to_caller_branches does for every other dataset: no "
              "caller is not a caller with no branches.")
    append_to(fr016, "Acceptance",
              " test_the_exports_hold_only_rows_in_reach in Module 18's tests_branch_reach.py "
              "proves a branch clerk's collections and payouts exports hold only her branch's "
              "and the school-wide rows, and that a scope with no caller holds every row.")

    retitle_paragraph(doc, "Whether a field is sensitive.",
                      "Whether a field is sensitive, and who may read it. Both belong to the "
                      "module that owns the field; this engine applies them to each column and "
                      "adds the extra key for restricted columns.")

    fr013 = fr_table(doc, "FR-013")
    append_to(fr013, "Current evidence",
              " Each attempt records the downloader, the moment, the outcome, any refusal "
              "reason and the client IP address it came from. " + ADDRESS_SOURCE)
    append_to(fr013, "Acceptance",
              " A download sent with a forged X-Forwarded-For is logged against the peer "
              "address (test_the_log_records_the_peer_address_not_a_forwarded_for_claim).")

    fr014 = fr_table(doc, "FR-014")
    append_to(fr014, "Current evidence",
              " A column may also name the Field Access key of the field it carries "
              "(Field.access); eleven do, the vendor contact, tax and bank columns, the payout "
              "beneficiary columns and a pupil's enrolment date. A column whose field the run's "
              "owner may not read is not offered by the builder and not written to the file, and "
              "is reported under the same FIELD_FORBIDDEN omission as a restricted one, so a file "
              "is never the way round a switch that hides a field on screen. A scheduled run is "
              "judged as its owner. The two gates are separate, and both must be yes.")
    append_to(fr014, "Acceptance",
              " FieldAccessGatesAColumnTests: the builder never offers a column the caller cannot "
              "read and offers it once the switch is on; asked for anyway, the file leaves it out "
              "and says why; the export key alone does not open a hidden column.")

    order_tables = [t for t in doc.tables if t.rows[0].cells[0].text.strip() == "Step"]
    produce, download = order_tables[0], order_tables[1]
    edit_cell(row(produce, "2").cells[-1],
              "Sensitive columns are included only with the extra key.",
              "Sensitive columns are included only with the extra key, and any column only if "
              "the run's owner may read its field.")
    append_to_cell(row(download, "2").cells[-1],
                   " Each attempt carries the client IP address the request could not choose.")

    model = table_headed(doc, "Model", "Holds", "Safety contract")
    keep_format(row(model, "ExportDownload").cells[1],
                "Every download attempt: who, when, the outcome, any refusal reason and the "
                "client IP address.")
    keep_format(row(model, "ExportDownload").cells[2],
                "Refusals are logged as well as successes. The IP address is the one "
                "core.client_ip decides, never a forwarding header.")

    routes = table_with_header(doc, "Method and path")
    keep_format(row(routes, "GET /exports/files/{pk}/downloads/").cells[-1],
                "Who tried, when, from which client IP address, and whether they were allowed.")
    refusals = table_with_header(doc, "Condition")
    insert_row_after(row(refusals, "A restricted column without the sensitive key"), [
        "A column whose field the caller's role may not read",
        "Not offered by the builder. Asked for anyway, the file is produced without it and "
        "reports a FIELD_FORBIDDEN omission.",
    ])

    deps = table_with_header(doc, "Dependency")
    append_to(deps, "Module 4, Roles & Permissions",
              " Its Field Access switches decide which linked columns the run's owner may read.")
    append_to(deps, "Every domain registering a dataset",
              " Payments narrows its collections and payouts datasets to the exporting caller's "
              "branches (Module 18 FR-026).")
    insert_row_after(row(deps, "vs_audit"), [
        "core",
        "Decides the client IP address the download log records (core.client_ip).",
    ])
    edit_paragraph(doc, "The module's own suite covers the run lifecycle",
                   "Backend evidence only; nothing here is deployed.",
                   "It also proves a column hidden by Field Access is neither offered nor written "
                   "until its switch is on, and that the download log records the peer address "
                   "rather than a forwarded claim. Backend evidence only; nothing here is "
                   "deployed.")
    edit_paragraph(doc, "Module 26 of XVS Module Requirements Document v2.80",
                   "v2.80", f"v{MRD_VERSION}")

    assert_absent_outside_log(doc, "v2.80", "this engine enforces the extra key.")
    # The change log row carries M26's own review date; M03 and M05 keep theirs.
    patch_record_history_docs.REVIEW_DATE = M26_REVIEW_DATE
    try:
        close(doc, M26_DIR, M26_STEM, M26_TARGET, M26_SUMMARY)
    finally:
        patch_record_history_docs.REVIEW_DATE = REVIEW_DATE


def main() -> None:
    patch_m03()
    patch_m05()
    patch_m26()


if __name__ == "__main__":
    main()

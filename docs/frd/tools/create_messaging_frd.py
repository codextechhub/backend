#!/usr/bin/env python3
"""Create the Module 32 Messaging and Conversations FRD and reconcile the MRD to it.

Two documents come out of one run: the first Module 32 FRD, built on the page
furniture of an existing FRD, and the next MRD, patched from the latest one.

``REQUIREMENT_STATUS`` below holds the status of every requirement. Backend
requirements are Implemented on backend evidence. The three frontend
requirements move with the frontends: flip a value and rerun the script, and the
FRD, its status key, its traceability and the MRD text all follow.

Usage::

    ./cx/bin/python docs/frd/tools/create_messaging_frd.py \\
        --reference-frd docs/frd/functional-requirements/28-parent-portal/XVS_M28_Parent_Portal_Functional_Requirements_Document_v1.3.docx \\
        --frd-output docs/frd/functional-requirements/32-messaging-and-conversations/XVS_M32_Messaging_and_Conversations_Functional_Requirements_Document_v1.0.docx \\
        --mrd-source docs/frd/module-requirements/XVS_Module_Requirements_Document_v2.108.docx \\
        --mrd-output docs/frd/module-requirements/XVS_Module_Requirements_Document_v2.109.docx
"""

from __future__ import annotations

import argparse
import copy
from pathlib import Path

from docx import Document
from docx.oxml.ns import qn

from create_health_frd import (
    add_callout as _add_callout,
    add_real_bullets,
    assert_vocabulary,
)
from generate_requirements_documents import (
    AMBER,
    BLUE,
    GREEN,
    GREY,
    PALE_AMBER,
    PALE_BLUE,
    PALE_GREEN,
    PALE_GREY,
    VERY_PALE_BLUE,
    add_body,
    add_cover,
    add_heading,
    add_metadata_table,
    add_page_break,
    add_table,
    assert_no_em_dash,
    remove_body_content,
    set_headers,
    set_table_widths,
    shrink_inherited_media,
    update_extended_title,
    write_cell,
)

REVIEW_DATE = "21 October 2026"
CHANGE_DATE = "21 Oct 2026"
MRD_SOURCE_VERSION = "2.108"
MRD_TARGET_VERSION = "2.109"
FRD_VERSION = "1.0"
CODE_BASELINE = (
    "Backend main at 408bb201 plus the uncommitted schools/vs_messages app and its "
    "seams in vs_notifications, vs_audit, vs_config, vs_rbac and core (21 October 2026)"
)

BACKEND = "Implemented"
FRONTEND_REVIEW = "Implemented (frontend, pending owner review)"
SPECIFIED = "Specified"

#: The status of every requirement. Backend requirements are Implemented on
#: backend evidence. FR-024 to FR-026 are the three frontends: Implemented
#: (frontend, pending owner review) only where the screens exist in the app's
#: source, Specified otherwise. Flip a value and rerun to follow the frontends.
REQUIREMENT_STATUS = {
    "FR-001": BACKEND,
    "FR-002": BACKEND,
    "FR-003": BACKEND,
    "FR-004": BACKEND,
    "FR-005": BACKEND,
    "FR-006": BACKEND,
    "FR-007": BACKEND,
    "FR-008": BACKEND,
    "FR-009": BACKEND,
    "FR-010": BACKEND,
    "FR-011": BACKEND,
    "FR-012": BACKEND,
    "FR-013": BACKEND,
    "FR-014": BACKEND,
    "FR-015": BACKEND,
    "FR-016": BACKEND,
    "FR-017": BACKEND,
    "FR-018": BACKEND,
    "FR-019": BACKEND,
    "FR-020": BACKEND,
    "FR-021": BACKEND,
    "FR-022": BACKEND,
    "FR-023": BACKEND,
    "FR-024": FRONTEND_REVIEW,
    "FR-025": FRONTEND_REVIEW,
    "FR-026": FRONTEND_REVIEW,
}

STATUS_STYLE = {
    BACKEND: (PALE_GREEN, GREEN),
    FRONTEND_REVIEW: (PALE_AMBER, AMBER),
    SPECIFIED: (PALE_GREY, GREY),
}

T = "schools.vs_messages.tests."

REQUIREMENTS = [
    {
        "id": "FR-001",
        "title": "Hold Six Conversation Kinds, Each With a Fixed Shape",
        "requirement": (
            "A conversation is exactly one of STAFF_DIRECT, FAMILY, PUPIL, GROUP, BROADCAST or "
            "BROADCAST_REPLY, and each kind carries only the columns that belong to it. Starting a "
            "pair conversation is find-or-create on a pair key that is unique within the school."
        ),
        "evidence": (
            "Conversation.kind is held to its shape by the ck_conversation_shape check constraint: a "
            "direct conversation names no child, guardian or broadcast; a family conversation names a "
            "child and a guardian; a pupil conversation names a child only; a group and a broadcast "
            "carry a title; a reply names its broadcast. uq_conversation_pair_key makes the pair key "
            "unique per tenant, and open_conversation settles a simultaneous pair of creates by reading "
            "the winner's row after the IntegrityError. Tests: " + T + "test_models (ShapeTests), "
            + T + "test_staff_api (StartTests)."
        ),
        "acceptance": (
            "A row mixing the columns of two kinds cannot be stored. Two taps, two devices or a retried "
            "request make one conversation. The same pair key may exist at another school, and two "
            "groups may share a name."
        ),
        "limit": "None.",
    },
    {
        "id": "FR-002",
        "title": "Keep Staff Direct Conversations Private to Their Two Participants",
        "requirement": (
            "Only the two people in a staff direct conversation can open it. No role holds a route that "
            "reads it, and a school leader has no general inspection override."
        ),
        "evidence": (
            "visibility.staff_conversations joins the caller's own open participation. The three "
            "inspection surfaces cover GROUP, BROADCAST and PUPIL only, so a STAFF_DIRECT id never "
            "resolves on them, and no key opens it. Tests: " + T + "test_visibility (StaffVisibilityTests), "
            + T + "test_oversight (test_family_and_staff_conversations_never_resolve_here), "
            + T + "test_staff_api (KeyTests)."
        ),
        "acceptance": (
            "A third member of staff, a school administrator holding every inspection key, and another "
            "school's administrator all receive the same 404 CONVERSATION_NOT_FOUND. A member of staff "
            "who leaves the roll or whose account is deactivated loses the conversation on the next request."
        ),
        "limit": "None. The absence of a leader override is an owner decision of 21 October 2026.",
    },
    {
        "id": "FR-003",
        "title": "Hold Family Conversations About One Linked Child",
        "requirement": (
            "A guardian and a member of staff may hold a conversation about exactly one child linked to "
            "that guardian. Either end may start it. There is one conversation per guardian, member of "
            "staff and child."
        ),
        "evidence": (
            "The pair key is family:<guardian>:<staff>:<child>. start_from_guardian requires that "
            "Guardian.user is the caller, that a StudentGuardian link exists, that the child is on the "
            "roll (PORTAL_STATUSES) and that the staff member is one the family may reach. "
            "start_family_from_staff requires the child to be inside the caller's family reach and the "
            "guardian's account to be active. Tests: " + T + "test_family (StaffStartsTests, "
            "GuardianStartsTests), " + T + "test_portal_parent (StartTests)."
        ),
        "acceptance": (
            "A guardian who is not the child's, a guardian without an account, a child off the roll and "
            "staff outside reach all answer the same 400 MESSAGE_RECIPIENT_UNAVAILABLE. Two guardians of "
            "one child make two conversations with one teacher, and neither reads the other's."
        ),
        "limit": (
            "A guardian-to-staff conversation has no inspection route. The owner decisions give oversight "
            "to pupil conversations, groups and broadcasts only."
        ),
    },
    {
        "id": "FR-004",
        "title": "Hold Pupil Conversations and Give the Primary Guardian a Read-Only View",
        "requirement": (
            "A pupil and a member of staff may hold a conversation. Every pupil conversation is readable "
            "by that pupil's primary guardian, read-only. The pupil is told plainly who else can read it."
        ),
        "evidence": (
            "PUPIL conversations use the pair key pupil:<child>:<staff>. visibility.guardian_conversations "
            "adds the pupil conversations of children whose StudentGuardian row is primary for the caller, "
            "marked watching: no write, no unread count, no alert, no participant row and no watermark. "
            "The student list carries OVERSIGHT_NOTICE. Tests: " + T + "test_visibility "
            "(FamilyVisibilityTests), " + T + "test_portal_parent (ListTests, ReadingAndWritingTests), "
            + T + "test_portal_student."
        ),
        "acceptance": (
            "The pupil reads and writes their own conversation. The primary guardian reads the whole "
            "history and a write answers 409 CONVERSATION_READ_ONLY; read/ returns unread_count 0 and "
            "changes nothing for anyone. A non-primary guardian receives 404. Changing the primary "
            "guardian moves the view on the next request. A parent's own conversation about the pupil is "
            "never visible to the pupil."
        ),
        "limit": "None.",
    },
    {
        "id": "FR-005",
        "title": "Allow Audited Welfare Inspection of Pupil Conversations",
        "requirement": (
            "Staff holding school.message_welfare.inspect, with the child's branch inside their reach, may "
            "read any pupil conversation read-only without joining it. Every opening is recorded."
        ),
        "evidence": (
            "visibility.inspectable judges a pupil conversation by the child's current branch. "
            "oversight.record_opening writes one CONVERSATION_INSPECTED event, carrying kind, surface and "
            "what (detail or messages), on every call to the detail or messages route, whether or not the "
            "inspector also takes part. Inspection never moves a watermark or clears an alert. List rows "
            "carry no message text. Tests: " + T + "test_oversight (WelfareTests)."
        ),
        "acceptance": (
            "A holder in reach reads the conversation and sees read_only and inspection true. Each opening "
            "is one audit event with no message text. A refused inspection writes no event. A holder whose "
            "reach excludes the child's branch, and a holder of a different inspection key, receive the "
            "same 404 as for a missing id."
        ),
        "limit": "Staff direct and guardian-to-staff conversations never resolve on an inspection route.",
    },
    {
        "id": "FR-006",
        "title": "Run Staff Groups With Owners, Names and Leaving",
        "requirement": (
            "Staff holding school.message_groups.create may create a staff group and become its owner. "
            "Only an owner may rename the group, add members, remove a member or make another owner. "
            "Anyone may leave. Groups hold staff only."
        ),
        "evidence": (
            "groups.create_group requires current staff, members who are staff the creator may address in "
            "the group's branch, and a name of 1 to 150 characters; a school-wide group needs a "
            "whole-school reach (403 MESSAGE_BRANCH_OUT_OF_REACH). Creation is idempotent on the "
            "client_token of its first message. A member's attempt at an owner action answers 403 "
            "GROUP_OWNER_ONLY, and the last owner cannot leave while others remain (409 GROUP_NEEDS_OWNER). "
            "Creation, rename and membership changes are audited. Tests: " + T + "test_groups "
            "(CreateTests, OwnerRuleTests)."
        ),
        "acceptance": (
            "A group is made with its creator as owner and up to 1,000 members, all in reach. A retried "
            "create returns the first group. A branch-bound creator cannot make a school-wide group or a "
            "group for another branch. Removing someone who is not in the group answers "
            "404 GROUP_MEMBER_NOT_FOUND. Guardians and pupils cannot be members."
        ),
        "limit": "Leaving a group is not a change of members that the Write switch can refuse.",
    },
    {
        "id": "FR-007",
        "title": "Enforce the Later-Join Boundary and End Access on Removal",
        "requirement": (
            "A person added to an existing group reads only messages whose id is greater than the group's "
            "newest message id at the moment they were added. A removed or departed member loses all access, "
            "including to what was said while they were a member. Adding them again starts a new membership "
            "that again begins afresh."
        ),
        "evidence": (
            "Participant.after_message_id is set to the group's last_message_id when a member is added, and "
            "visibility.visible_messages reads id greater than that value. Removal closes the period "
            "(left_at) so the person no longer resolves the conversation. The boundary governs the "
            "messages, the preview, the unread count, search, the CSV export and alerts. Tests: " + T
            + "test_groups (LateJoinerTests), " + T + "test_export (AccessTests), " + T + "test_alerts "
            "(WhoIsAlertedTests)."
        ),
        "acceptance": (
            "A message whose id equals the boundary is not visible and the next one is. A new member has no "
            "preview, no unread count and no search hit for the past. A removed member receives 404 and "
            "cannot export. A member added again sees only what is said after returning. An inspector "
            "reads all of it without joining."
        ),
        "limit": "None.",
    },
    {
        "id": "FR-008",
        "title": "Allow Audited Group Inspection Without Joining",
        "requirement": (
            "Staff holding school.message_groups.inspect may read any group in reach, read-only and without "
            "joining it. A group with no branch needs a whole-school reach. Every opening is audited."
        ),
        "evidence": (
            "The oversight list and detail routes resolve the GROUP surface; the group's branch must be "
            "inside the inspector's reach (visibility.inspectable). Rows list the owners and a member "
            "count, and the full member list is on the detail as members. Each detail or messages call "
            "writes CONVERSATION_INSPECTED. Tests: " + T + "test_oversight (GroupAndBroadcastListTests), "
            + T + "test_groups (LateJoinerTests)."
        ),
        "acceptance": (
            "A branch head sees the groups of their branch only. A school-wide group needs a whole-school "
            "reach. The inspector reads the group from its first message regardless of the later-join "
            "boundary. Oversight lists are paginated, an empty list is a list, and a one-branch school "
            "shows no branch on a row."
        ),
        "limit": "None.",
    },
    {
        "id": "FR-009",
        "title": "Send Staff Broadcasts With a Fixed Reply Setting",
        "requirement": (
            "Staff holding school.message_broadcasts.send may send one message to 1 to 1,000 staff. The "
            "sender sets the broadcast to one-way or private replies when sending, and the setting never "
            "changes. Recipients cannot write in the broadcast. A recipient sees how many people it went "
            "to, not who; the sender sees the list."
        ),
        "evidence": (
            "broadcasts.send_broadcast requires current staff, a title, a body, recipients the sender may "
            "address in the broadcast's branch, drops the sender's own id silently, and defaults an absent "
            "or unknown reply_mode to ONE_WAY. The detail carries recipients for the sender and "
            "recipient_count for everyone, both behind the Broadcast recipients switch. Tests: " + T
            + "test_broadcasts (SendTests, RecipientViewTests, OneWayTests)."
        ),
        "acceptance": (
            "Only staff in reach can be recipients; out-of-reach and nonexistent ids are refused alike. "
            "A thousand recipients is the most and an empty list is refused. A broadcast row never leaks "
            "the count as member_count. The sender may follow up in their own broadcast when they hold "
            "school.messages.send. A retry with the same token sends once."
        ),
        "limit": "A broadcast's reply setting is fixed when it is sent. This is a build decision awaiting owner confirmation.",
    },
    {
        "id": "FR-010",
        "title": "Keep Broadcast Replies Private and Out of Inspection",
        "requirement": (
            "A recipient's reply goes to the broadcast's sender alone. No other recipient and no inspector "
            "can open it, and inspecting a broadcast never exposes the replies."
        ),
        "evidence": (
            "broadcasts.reply_privately finds or creates a BROADCAST_REPLY conversation keyed "
            "reply:<broadcast>:<recipient> between the recipient and the sender. It answers 409 "
            "BROADCAST_ONE_WAY on a one-way broadcast. No inspection surface resolves BROADCAST_REPLY, "
            "and a broadcast inspection reads the broadcast's own messages only. Tests: " + T
            + "test_broadcasts (PrivateReplyTests)."
        ),
        "acceptance": (
            "A second reply from the same recipient lands in the same thread. Only the sender and that "
            "recipient see the reply. The sender has the reply unread in the broadcast tab. The sender "
            "cannot reply to their own broadcast. An inspector of the broadcast never reaches a reply."
        ),
        "limit": "None.",
    },
    {
        "id": "FR-011",
        "title": "Pass Every Read Through One Gate With One Not-Found Answer",
        "requirement": (
            "Every route, the unread count, search, the export and the alert recipient list decide access "
            "through services.visibility. A conversation at another school, one that does not exist and "
            "one the caller may not reach answer the same status, code and body."
        ),
        "evidence": (
            "visibility.resolve returns an Access or raises the module's single ConversationNotFound "
            "(404, CONVERSATION_NOT_FOUND). No view filters conversations itself. A malformed id raises "
            "the same refusal. Tests: " + T + "test_staff_api (IsolationTests), " + T + "test_visibility, "
            + T + "test_portal_parent (AccessTests), " + T + "test_portal_student (AccessTests), " + T
            + "test_export (AccessTests)."
        ),
        "acceptance": (
            "Changing a pk to another school's conversation, to a missing id or to a conversation the "
            "caller is not in returns identical responses. Another school's administrator reaches none "
            "of our routes and lists only their own inbox."
        ),
        "limit": "None.",
    },
    {
        "id": "FR-012",
        "title": "Decide Contact Reach and Branch Reach in Bulk",
        "requirement": (
            "Staff may start a conversation with, add to a group or address in a broadcast current staff "
            "holding school.messages.view whose posting is inside their branch reach or school-wide. A "
            "family may reach a child's class teacher, the teachers of the child's class this year (a "
            "school setting) and the child's branch's school office, each of whom also holds "
            "school.family_messages.send and school.messages.view. The same rule read the other way says "
            "which children a member of staff may start a conversation about."
        ),
        "evidence": (
            "people.staff_contacts, people.staff_reachable_for_child and people.family_children resolve "
            "key holders in one pass through resolve_users_with_permission and never check candidates one "
            "by one. Staff reach is judged by StaffProfile.branch; null is school-wide. Group and "
            "broadcast members narrow to the group's branch. A person out of reach and a person who does "
            "not exist look the same. Tests: " + T + "test_staff_api (ContactTests), " + T + "test_family "
            "(FamilyContactTests, OneBranchSchoolTests), " + T + "test_groups."
        ),
        "acceptance": (
            "A branch head reaches their branch and the school-wide staff; a whole-school reach sees "
            "everyone. The office key without a branch covering the child reaches nothing. A multi-branch "
            "school shows the branch and a one-branch school does not. A class teacher sees their class "
            "with its guardians."
        ),
        "limit": "Additional staff postings are not consulted; only the staff record's branch counts.",
    },
    {
        "id": "FR-013",
        "title": "End Access Immediately When the Link That Grants It Ends",
        "requirement": (
            "Nothing about access is stored in a token or on a participant row. Deactivating an account, "
            "taking a member of staff off the roll, removing the guardian's link to the child, unsetting "
            "Guardian.user, or taking the child off the roll ends access on the next request."
        ),
        "evidence": (
            "visibility re-derives access from the current links on every request: an active account "
            "with a StaffProfile on the roll, an existing StudentGuardian link with a working guardian "
            "account, a pupil account linked through Student.user, and a child whose status the portal "
            "serves. A conversation whose staff end has left reads no longer at the school for the "
            "family; one whose family end cannot be reached reads read-only for staff. Tests: " + T
            + "test_visibility, " + T + "test_portal_parent (AccessTests), " + T + "test_portal_student "
            "(AccessTests), " + T + "test_staff_api (StartTests)."
        ),
        "acceptance": (
            "Each of the five triggers makes the next request answer 404, with no cleanup job and no "
            "participant edit. A suspended member of staff is still on the roll. A staff member who left "
            "makes the family conversation read-only, with a sentence the family can be shown."
        ),
        "limit": "A leaver's family and pupil conversations are not handed to a successor.",
    },
    {
        "id": "FR-014",
        "title": "Count Unread Messages and Keep Read State Monotonic",
        "requirement": (
            "Unread counts are per conversation and in total, and the read watermark only ever moves "
            "forward."
        ),
        "evidence": (
            "visibility.unread_counts answers a total and a count per kind group (direct, family, group, "
            "broadcast) in one aggregate query, excluding the caller's own messages and anything before "
            "the join point. mark_read moves last_read_message_id through Greatest in the database, "
            "clamped to the newest visible message, and clears the conversation's bell entries. An "
            "inspector or a primary guardian has no watermark. Tests: " + T + "test_visibility "
            "(UnreadTests), " + T + "test_staff_api (ReadTests, QueryCountTests)."
        ),
        "acceptance": (
            "Counting costs one query. Reading clears the count and never goes backwards; up_to marks "
            "only that far and is clamped; a bad up_to is refused. Reading does not move the other end's "
            "watermark. The inbox and a message page cost the same for one row and for many."
        ),
        "limit": "None.",
    },
    {
        "id": "FR-015",
        "title": "Page Messages by Cursor and Make Sends and Starts Idempotent",
        "requirement": (
            "Messages are served newest first by cursor, and a retried send or start never makes a second "
            "message or conversation. A person may send 20 messages a minute, one budget across every route "
            "that stores a message."
        ),
        "evidence": (
            "The messages route takes before and limit (1 to 100, default 50) and answers results, "
            "has_more and next_before. A message is unique per conversation, sender and client_token, and "
            "a repeated token answers 200 with the stored message. post_message serialises on the "
            "conversation row. Group and broadcast creation are idempotent on their first message's token. "
            "Lists use the shared page envelope and an empty list is []. A message_send throttle of 20 a "
            "minute, keyed by person and school, counts every POST that can store a message (staff start, "
            "send, group, broadcast and broadcast reply, and the parent and student start and send "
            "routes); a GET or any other method is never counted, because the inbox polls. A refusal is "
            "429 MESSAGE_RATE_LIMITED with a plain sentence and error.detail.retry_after seconds. Tests: "
            + T + "test_staff_api (MessageTests, StartTests), " + T + "test_broadcasts, " + T
            + "test_groups, " + T + "test_portal_parent (StartTests), " + T + "test_send_limit."
        ),
        "acceptance": (
            "The same token from someone else is a different message. Sending updates the conversation "
            "and the sender's watermark. A body is 1 to 4,000 characters after trimming. A retried send "
            "alerts once. The 21st send in a minute is refused and stores nothing, reading stays open, "
            "two people never share a budget, and sending works again when the minute has passed."
        ),
        "limit": "None.",
    },
    {
        "id": "FR-016",
        "title": "Alert Through Module 8 After Commit and Never Carry the Message",
        "requirement": (
            "Each stored message queues an alert after its transaction commits. An alert says who wrote "
            "and where, never what was said, in any channel or stored row. One alert covers a run of "
            "messages until the conversation is read. A failure to alert never loses a message."
        ),
        "evidence": (
            "tasks.queue_message_alert registers transaction.on_commit and queues "
            "vs_messages.dispatch_message_alert. The task computes alert_recipients, calls "
            "send_notification per group in a try block that logs, and passes only the conversation id "
            "as metadata. The twelve templates (four events by in-app, email and push) carry no body "
            "variable. alerts_suppressed lets a seeder store messages without alerting. Tests: " + T
            + "test_alerts (NoBodyTests, WhatEachPersonHearsTests, WhoIsAlertedTests, FailureTests, "
            "OneTokenOnlyTests)."
        ),
        "acceptance": (
            "No stored notification, push payload or email carries the text. The second message before "
            "reading sends no second alert; after reading the next one alerts again. A failing "
            "notification, a down broker and a vanished message each leave the message stored and the "
            "send answered. A removed member, a deactivated account, a staff member off the roll, a "
            "family whose link was removed and the watching primary guardian are not alerted."
        ),
        "limit": "A broker outage loses the alert, not the message, and nothing redispatches it.",
    },
    {
        "id": "FR-017",
        "title": "Word Alerts by Each Recipient's Field Access and Open the Right Conversation",
        "requirement": (
            "A staff recipient is told a sender or group name only if their own Field Access lets them "
            "read it. Every alert opens its conversation in the recipient's own app, and reading the "
            "conversation clears its alerts."
        ),
        "evidence": (
            "tasks._staff_wording judges each staff recipient with get_field_access and groups recipients "
            "who read the same wording into one send, in batches of 200. Hidden People gives a colleague, "
            "a parent or a pupil; a hidden group or broadcast name gives a staff group or a broadcast. "
            "Parents and pupils hold no role and always get names. The CONVERSATION record family routes "
            "the prefix messages. to /messages/{id}; the parent push opens /parent/messages/<id> and the "
            "pupil push /student/messages/<id>. Tests: " + T + "test_alerts (FieldAccessInAlertsTests, "
            "DestinationTests)."
        ),
        "acceptance": (
            "Staff with the same rights share one send. A hidden broadcast sender reads A colleague sent "
            "a broadcast. Reading a conversation on any surface clears the bell entries for that "
            "conversation only. The push layer knows each destination by event."
        ),
        "limit": "None.",
    },
    {
        "id": "FR-018",
        "title": "Export a Conversation as a Guarded CSV and Audit It",
        "requirement": (
            "A participant holding school.messages.export may download the messages they can read as a "
            "CSV. Each column follows the Field Access switch of what it carries, a cell that would run "
            "as a spreadsheet formula is written as text, and every download is audited without its text."
        ),
        "evidence": (
            "services.export writes sent_at, sender and message, oldest first, in the school's own date "
            "format, only for messages visible_messages returns, so a later-joined member's file starts "
            "at their join point. A column the caller may not read is left out; none readable answers "
            "403 field_read_denied. A leading =, +, -, @, tab or carriage return gets an apostrophe. One "
            "CONVERSATION_EXPORTED event records the count and the column names. Tests: " + T
            + "test_export (ExportTests, AccessTests, FieldAccessTests)."
        ),
        "acceptance": (
            "A caller without the key is refused, a holder who is not in the conversation receives the "
            "404, and a parent or pupil has no export. A refused export leaves no event. The audit event "
            "holds no message text."
        ),
        "limit": "Only a participant exports. An inspector has no export route.",
    },
    {
        "id": "FR-019",
        "title": "Publish Ten Permission Keys With Default Roles",
        "requirement": (
            "Ten school keys govern taking part, the staff end of family conversations, groups, "
            "broadcasts, export and the three inspections. They sit in the Core band and are seeded with "
            "default roles."
        ),
        "evidence": (
            "seed_school_permissions registers the keys and their defaults, seed_actions adds the verbs "
            "inspect and office, and permission_bands places the five resources in the Core band. Section "
            "3.1 lists every key. Tests: core.test_seed_school_permissions (key counts and role "
            "defaults), " + T + "test_staff_api (KeyTests)."
        ),
        "acceptance": (
            "School administrators hold all ten, branch administrators six, teachers three and finance "
            "administrators two. A caller with no keys is refused everywhere. The three inspection keys "
            "are separate: the key for a kind is the key that opens it."
        ),
        "limit": "The keys must stay in lockstep with the staff app's permission list.",
    },
    {
        "id": "FR-020",
        "title": "Let a School Switch Family and Pupil Messaging and Subject-Teacher Reach",
        "requirement": (
            "Three whole-school settings decide whether parents may message staff, whether pupils may "
            "message staff, and whether a family may reach every teacher of the child's class."
        ),
        "evidence": (
            "seed_config_catalogue registers messages.families_enabled, messages.pupils_enabled and "
            "messages.family_reach_subject_teachers, each BOOLEAN with default true. Switched off, "
            "existing conversations stay readable and every start and write answers 409 "
            "CONVERSATION_READ_ONLY with a sentence for that audience; the portal contact list reports "
            "can_start false with the reason. Tests: " + T + "test_family, " + T + "test_portal_parent "
            "(StartTests), " + T + "test_portal_student (ReadingAndWritingTests, ContactsAndStartTests), "
            + T + "test_visibility (FamilyVisibilityTests)."
        ),
        "acceptance": (
            "The pupil switch does not touch parents and the family switch does not touch pupils. Without "
            "subject teachers only the class teacher and the school office remain reachable."
        ),
        "limit": "The settings are whole-school; none is per branch.",
    },
    {
        "id": "FR-021",
        "title": "Govern Message Fields With the school.messages Field Access Resource",
        "requirement": (
            "Every user-visible business field of a conversation has a Field Access decision, enforced on "
            "every payload, nested name, search, export and alert that carries it."
        ),
        "evidence": (
            "field_access.register publishes eight fields on the school.messages resource across ten "
            "serializer surfaces, all open by default. Section 8 lists them. A hidden field leaves every "
            "payload including nested and alternate names; a hidden Message drops body matches from "
            "search; Write off refuses the write with FieldWriteDenied on every create path. Tests: " + T
            + "test_field_access, " + T + "test_field_access_deep_payload, vs_rbac.tests.test_field_registry."
        ),
        "acceptance": (
            "Every field is open by default. Hidden People leaves every name in every payload and closes "
            "the staff contact list. Title and People are open on create. Sent times and Read status "
            "cannot be written. Oversight follows the same switches."
        ),
        "limit": "Portal payloads are a fixed narrow shape that no role switch reaches, because parents and pupils hold no role.",
    },
    {
        "id": "FR-022",
        "title": "Audit Inspections, Exports, Group Changes and Broadcasts",
        "requirement": (
            "Inspecting, exporting, creating or renaming a group, changing its members and sending a "
            "broadcast are recorded under the MESSAGES audit module. No event carries a message body."
        ),
        "evidence": (
            "services.audit.record emits one event with entity_type conversation, the conversation id and "
            "its display name as the label. Six action types exist (section 9.1). Individual messages are "
            "not audited because they are the record. Tests: " + T + "test_oversight, " + T
            + "test_groups (creating and membership changes are audited), " + T + "test_broadcasts, "
            + T + "test_export (the export is audited without the text)."
        ),
        "acceptance": (
            "Each inspection opening is exactly one event; a rename, a membership change and a broadcast "
            "are one event each; summaries name who did what to which conversation and never the text."
        ),
        "limit": "None.",
    },
    {
        "id": "FR-023",
        "title": "Delete Nothing and Detach People Safely",
        "requirement": (
            "No route deletes a conversation, a message or a membership row. A user removed from the "
            "platform leaves the message and the conversation in place."
        ),
        "evidence": (
            "Messages are never edited or deleted. Participant rows close with left_at. Message.sender, "
            "Conversation.created_by and Participant.removed_by are SET_NULL, and the student, guardian "
            "and tenant references are PROTECT. Participant.user cascades, so removing an account removes "
            "only its membership rows and never a message. No view declares a delete for a message or a "
            "conversation."
        ),
        "acceptance": (
            "A message sent by a user who is later removed keeps its text and shows a sender the reader "
            "can still label as someone. A closed membership never blocks a new one."
        ),
        "limit": (
            "There is no retention window and no way to withdraw a message sent in error. Both are "
            "outside this release."
        ),
    },
    {
        "id": "FR-024",
        "title": "Give Staff an Inbox, Conversation Screens and Oversight Screens",
        "requirement": (
            "The school app offers staff an inbox with the open conversation beside it, drawers to start "
            "a message, a group or a broadcast, a people sheet for a group or broadcast, an export, and "
            "read-only oversight screens for leaders. Hidden fields disappear completely and read-only "
            "fields are never submitted."
        ),
        "evidence_review": (
            "school-fe src/pages/protected/messages (index, inbox-list, conversation-view, "
            "message-thread, message-composer, new-message, new-group and new-broadcast drawers, "
            "people-sheet, contact and family pickers, oversight), the messages API slice under "
            "src/redux/services/messages, the unread hook, routes /messages and /messages/oversight and "
            "the permission list in src/permissions. Frontend evidence only; its tests are "
            "messages.test.tsx, messages-copy.test.ts, messages-api.test.ts and messages-permissions.test.ts."
        ),
        "evidence_specified": "The backend contract is implemented. No staff screen exists in the school app's source.",
        "acceptance": (
            "New offers Message, Group and Broadcast only to a role holding the key for each. The inbox "
            "refreshes while the tab is in front. A read-only conversation shows the server's reason. "
            "Name, preview, child line, time, unread badge and branch are drawn only when Field Access "
            "lets the viewer read them."
        ),
        "limit_review": (
            "Awaiting owner review. At the review date the screens are uncommitted in the school app's "
            "repository. Frontend delivery is recorded separately from the backend: nothing here claims "
            "deployment or production use."
        ),
        "limit_specified": "No screen is built. Specified means the requirement is agreed and unbuilt.",
    },
    {
        "id": "FR-025",
        "title": "Give Parents Conversation Screens in the Parent App",
        "requirement": (
            "The parent app lists a parent's family conversations and, read-only, the pupil conversations "
            "of children they are primary guardian of; opens and writes in a conversation; starts one by "
            "choosing a child and then a reachable member of staff with their label; says plainly when a "
            "conversation is read-only or when the school has switched parent messaging off; and opens "
            "the right conversation from a push."
        ),
        "evidence_review": (
            "parent-fe src/pages (messages, messages-thread, messages-new, messages-row, "
            "messages-composer and messages.css), src/redux/services/messages-api.ts, src/lib/messages.ts, "
            "the routes /messages, /messages/new and /messages/:id in src/App.tsx, and a Messages "
            "destination in the shell navigation that carries the unread count. The backend contract is "
            "FR-003, FR-004, FR-013 and FR-020. Frontend evidence only; its tests are "
            "src/pages/messages.test.tsx and src/lib/messages.test.ts."
        ),
        "evidence_specified": (
            "The backend contract is implemented (FR-003, FR-004, FR-013, FR-020). No parent screen "
            "exists in the parent app's source. This requirement is the contract for that work."
        ),
        "acceptance": (
            "The child filter narrows the list. A watched pupil conversation shows its reason and no "
            "composer. A disabled start shows can_start_reason. A push opens /parent/messages/<id>. "
            "Staff contact details beyond the label are never shown."
        ),
        "limit_review": (
            "Awaiting owner review. At the review date the screens are uncommitted in the parent app's "
            "repository and still changing. Nothing here claims deployment or production use."
        ),
        "limit_specified": "No screen is built. Specified means the requirement is agreed and unbuilt.",
    },
    {
        "id": "FR-026",
        "title": "Give Pupils Conversation Screens in the Student App",
        "requirement": (
            "The student app lists a pupil's conversations, opens and writes in them, starts one with a "
            "teacher of the pupil's class or the school office, always shows that the parent or guardian "
            "and the school can read the messages, and opens the right conversation from a push."
        ),
        "evidence_review": (
            "student-fe src/pages (messages, messages-box, messages-thread, messages-new and "
            "messages.css), src/redux/services/messages-api.ts, src/lib/client-token.ts, the routes "
            "/messages, /messages/new and /messages/:id in src/App.tsx, a Messages destination in the "
            "shell navigation, and the push service worker, which opens only a /student/messages/<id> "
            "address for a message push. The backend contract is FR-004, FR-013 and FR-020. Frontend "
            "evidence only; its test is src/pages/messages.test.tsx."
        ),
        "evidence_specified": (
            "The backend contract is implemented (FR-004, FR-013, FR-020). The student app has no "
            "messages code in its source. This requirement is the contract for that work."
        ),
        "acceptance": (
            "The oversight notice is visible beside the list. A switched-off school shows the reason and "
            "no composer. A push opens /student/messages/<id>. A pupil never sees a parent's conversation "
            "about them."
        ),
        "limit_review": (
            "Awaiting owner review. At the review date the screens are uncommitted in the student app's "
            "repository and still changing. Nothing here claims deployment or production use."
        ),
        "limit_specified": "No screen is built. Specified means the requirement is agreed and unbuilt.",
    },
]


def keep_together(table) -> None:
    """Keep every row of *table* with the next, so the table moves to a new page whole."""
    for row in table.rows[:-1]:
        for cell in row.cells:
            for paragraph in cell.paragraphs:
                paragraph.paragraph_format.keep_with_next = True


def add_callout(doc, title, lines, *, kind="info"):
    """A callout whose title and bullets never separate across a page."""
    table = _add_callout(doc, title, lines, kind=kind)
    cell = table.cell(0, 0)
    for paragraph in cell.paragraphs[:-1]:
        paragraph.paragraph_format.keep_with_next = True
    return table


def status_of(requirement_id: str) -> str:
    return REQUIREMENT_STATUS[requirement_id]


def frontend_summary() -> str:
    """One sentence on the three frontends, derived from the status dict."""
    names = {"FR-024": "the staff app", "FR-025": "the parent app", "FR-026": "the student app"}
    built = [n for k, n in names.items() if REQUIREMENT_STATUS[k] != SPECIFIED]
    unbuilt = [n for k, n in names.items() if REQUIREMENT_STATUS[k] == SPECIFIED]
    parts = []
    if built:
        many = len(built) > 1
        parts.append(
            f"{_join(built)} {'have their' if many else 'has its'} screens and "
            f"{'await' if many else 'awaits'} owner review"
        )
    if unbuilt:
        parts.append(f"{_join(unbuilt)} {'is' if len(unbuilt) == 1 else 'are'} not built")
    return "; ".join(parts)


def frontend_gap() -> tuple[str, str]:
    """The Needs Attention row for frontend delivery, worded for what is built."""
    unbuilt = [n for k, n in (("FR-025", "parent"), ("FR-026", "student")) if REQUIREMENT_STATUS[k] == SPECIFIED]
    if REQUIREMENT_STATUS["FR-024"] == SPECIFIED:
        unbuilt.insert(0, "staff")
    tail = (
        " Add the unread-messages block the designs show to both portals' home/ responses, which "
        "would move In use to Complete."
    )
    if unbuilt:
        return (
            "Frontend delivery is incomplete",
            f"The owner reviews the apps that are built; the {_join(unbuilt)} "
            f"{'app is' if len(unbuilt) == 1 else 'apps are'} built to FR-024 to FR-026." + tail,
        )
    return (
        "Frontend delivery awaits review",
        "The owner reviews the staff, parent and student apps, whose screens are uncommitted in their "
        "repositories and nothing is deployed." + tail,
    )


def _join(items: list[str]) -> str:
    if len(items) <= 1:
        return "".join(items)
    return ", ".join(items[:-1]) + " and " + items[-1]


def add_requirement(doc, requirement: dict) -> None:
    """One requirement block, styled by the status held in REQUIREMENT_STATUS."""
    status = status_of(requirement["id"])
    fill, color = STATUS_STYLE[status]
    suffix = "specified" if status == SPECIFIED else "review"

    def pick(field: str) -> str:
        return requirement.get(f"{field}_{suffix}", requirement.get(field, ""))

    add_heading(doc, f"{requirement['id']}  {requirement['title']}", level=2)
    table = doc.add_table(rows=1, cols=2)
    set_table_widths(table, [1.48, 5.79])
    merged = table.rows[0].cells[0].merge(table.rows[0].cells[1])
    write_cell(merged, f"{requirement['id']} | {status}", size=9, bold=True, color=color, fill=fill)
    for label, value in (
        ("Requirement", requirement["requirement"]),
        ("Current evidence", pick("evidence")),
        ("Acceptance", requirement["acceptance"]),
        ("Current limit", pick("limit")),
    ):
        row = table.add_row()
        write_cell(row.cells[0], label, size=8.3, bold=True, color=BLUE, fill=VERY_PALE_BLUE)
        write_cell(row.cells[1], value, size=8.3, fill=WHITE_FILL)
    keep_together(table)


WHITE_FILL = "FFFFFF"


def add_status_key(doc) -> None:
    rows = [
        [BACKEND, "The stated backend path is present in the inspected code. No frontend, deployment or adoption is implied."],
        [FRONTEND_REVIEW, "The screens exist in the app's source and await the owner's review. Nothing is claimed as deployed."],
        [SPECIFIED, "The requirement is agreed and nothing is built. It is a contract for the work, not a record of it."],
    ]
    table = add_table(doc, ["State", "Meaning"], rows, [2.35, 4.92], font_size=8.5)
    for idx, name in enumerate((BACKEND, FRONTEND_REVIEW, SPECIFIED), start=1):
        fill, color = STATUS_STYLE[name]
        cell = table.rows[idx].cells[0]
        write_cell(cell, name, size=8.5, bold=True, color=color, fill=fill)


PPR_ORDER = [
    "pStyle", "keepNext", "keepLines", "pageBreakBefore", "framePr", "widowControl", "numPr",
    "suppressLineNumbers", "pBdr", "shd", "tabs", "suppressAutoHyphens", "kinsoku", "wordWrap",
    "overflowPunct", "topLinePunct", "autoSpaceDE", "autoSpaceDN", "bidi", "adjustRightInd",
    "snapToGrid", "spacing", "ind", "contextualSpacing", "mirrorIndents", "suppressOverlap", "jc",
    "textDirection", "textAlignment", "textboxTightWrap", "outlineLvl", "divId", "cnfStyle", "rPr",
    "sectPr", "pPrChange",
]
TCPR_ORDER = [
    "cnfStyle", "tcW", "gridSpan", "hMerge", "vMerge", "tcBorders", "shd", "noWrap", "tcMar",
    "textDirection", "tcFitText", "vAlign", "hideMark", "headers", "cellIns", "cellDel",
    "cellMerge", "tcPrChange",
]


def _sort_children(element, order: list[str]) -> None:
    """Put an element's children in schema order, keeping the order of equals."""
    rank = {qn(f"w:{name}"): index for index, name in enumerate(order)}
    children = list(element)
    ordered = sorted(children, key=lambda child: rank.get(child.tag, len(order)))
    if ordered != children:
        for child in children:
            element.remove(child)
        for child in ordered:
            element.append(child)


def normalise_schema_order(doc) -> None:
    """Order paragraph and cell properties as the schema requires and give every shading a value.

    The shared table helpers append properties in the order they are set, which Word
    tolerates and a schema check does not.
    """
    body = doc.element.body
    for ppr in body.iter(qn("w:pPr")):
        _sort_children(ppr, PPR_ORDER)
    for tcpr in body.iter(qn("w:tcPr")):
        _sort_children(tcpr, TCPR_ORDER)
    for shd in body.iter(qn("w:shd")):
        if shd.get(qn("w:val")) is None:
            shd.set(qn("w:val"), "clear")


def build_frd(reference_path: Path, output_path: Path) -> None:
    doc = Document(str(reference_path))
    remove_body_content(doc)
    title = f"XVS M32 Messaging and Conversations Functional Requirements Document v{FRD_VERSION}"
    doc.core_properties.title = title
    doc.core_properties.subject = "Code-aligned functional requirements for XVS Module 32"
    doc.core_properties.author = "CodeX Team"
    doc.core_properties.version = FRD_VERSION
    set_headers(doc, "CodeX | Messaging & Conversations | Functional Requirements Document (FRD)")

    add_cover(
        doc,
        family="Functional Requirements Document",
        title="Module 32: Messaging & Conversations",
        subtitle="XVision Systems | Conversations between staff, families and pupils",
        version=FRD_VERSION,
    )

    add_heading(doc, "Document Control", level=1)
    add_metadata_table(
        doc,
        [
            ("Document", "Functional Requirements Document (FRD)"),
            ("Module", "M32 | Messaging & Conversations"),
            ("Version", FRD_VERSION),
            ("Review date", REVIEW_DATE),
            ("Code baseline", CODE_BASELINE),
            ("Source MRD", f"XVS Module Requirements Document v{MRD_TARGET_VERSION} | Module 32"),
            ("Primary app", "schools.vs_messages"),
            (
                "Supporting apps",
                "schools.vs_students, schools.vs_staff, schools.vs_academics, schools.vs_parent_portal, "
                "schools.vs_student_portal, vs_notifications, vs_audit, vs_config, vs_rbac, vs_user, vs_tenants, core",
            ),
            (
                "Status",
                "Backend built and uncommitted. Backend requirements are Implemented on backend evidence; "
                "frontend delivery is recorded separately.",
            ),
            ("Owner", "CodeX Team"),
        ],
    )
    add_callout(
        doc,
        "Evidence boundary",
        [
            "Implemented means the stated backend path is present in the inspected, uncommitted code. It does not prove frontend completion, deployment, production adoption, worker availability or delivery by an external provider.",
            "Implemented (frontend, pending owner review) means the screens exist in the app's source and the owner has not yet reviewed them. Specified means the requirement is agreed and nothing is built.",
            "The MRD and this FRD have independent versions. A change can require one or both documents to advance.",
        ],
        kind="info",
    )
    add_page_break(doc)

    add_heading(doc, "Table of Contents", level=1)
    add_table(
        doc,
        ["Section", "Purpose"],
        [
            ["Document Control", "Version, ownership, source baseline, and evidence rules"],
            ["1. Purpose and Scope", "What the module owns, the owner decisions and the build decisions awaiting confirmation"],
            ["2. Context and Status Model", "Requirement states and the six conversation kinds"],
            ["3. Actors, Permissions, and Ownership", "The ten keys, default roles, tenant and branch scope"],
            ["4. Functional Requirements", "Testable behavior, evidence, acceptance, and limits"],
            ["5. Workflows and Lifecycle Rules", "Sending, membership, alerting and revocation"],
            ["6. Data Model and Relationships", "Conversations, memberships, messages and what is never stored"],
            ["7. API and Validation Contracts", "Staff, oversight, parent and student routes and refusals"],
            ["8. Field Access", "The school.messages resource, its eight fields and its exemptions"],
            ["9. Audit, Alerts and Settings", "Audit events, Module 8 alerts and the three school settings"],
            ["10. Dependencies and Operational Evidence", "Modules relied on, deploy preconditions and test evidence"],
            ["11. Needs Attention", "Current gaps only"],
            ["12. Not in this Release", "What the first release deliberately leaves out"],
            ["13. MRD Traceability", "All Module 32 capability mappings"],
            ["14. Change Log", "Independent FRD revision history"],
        ],
        [2.35, 4.92],
        font_size=8.5,
    )
    add_body(
        doc,
        "Use the Word Navigation pane to jump between headings. This contents page is intentionally static for reliable headless rendering.",
        size=8.5,
        color=GREY,
    )
    add_page_break(doc)

    # ---- 1. Purpose and scope -------------------------------------------------
    add_heading(doc, "1. Purpose and Scope", level=1)
    add_body(
        doc,
        "Module 32 is the conversation layer of the schools product. It lets current staff talk to each other, in staff groups and by broadcast, and lets a guardian or a pupil talk to the staff who answer for a child. It stores the conversations, memberships and messages, decides on every request who may read or write them, and asks Module 8 to tell people that something was said. It lives in schools.vs_messages because it knows guardians and pupils, and it reaches the engines only through their public seams.",
    )
    add_heading(doc, "1.1 In Scope", level=2)
    add_real_bullets(
        doc,
        [
            "Staff direct conversations, family conversations about a linked child, pupil conversations, staff groups, staff broadcasts and private broadcast replies.",
            "One visibility service that gates every read, count, search, export and alert, and answers a missing, foreign or inaccessible conversation with one 404.",
            "Audited read-only inspection of groups, broadcasts and pupil conversations for leaders holding the matching key.",
            "Unread counts, monotonic read state, cursor paging, idempotent sends and starts, a guarded CSV export, and Field Access on every message field.",
            "Four alert events delivered by Module 8, ten permission keys, three school settings and six audit action types.",
        ],
    )
    add_heading(doc, "1.2 Out of Scope and Relocated Responsibilities", level=2)
    add_table(
        doc,
        ["Not owned here", "Owner"],
        [
            ["Delivery of in-app, email and push alerts, templates, preferences and push registration", "Module 8. Module 32 raises the events and carries no message text into them."],
            ["Accounts, sign-in and device sessions for staff, guardians and pupils", "Module 3. A guardian or pupil holds no role."],
            ["Permission keys, roles, branch reach and Field Access evaluation", "Module 4. Module 32 registers its keys and fields and calls the evaluator."],
            ["Audit storage and the audit module vocabulary", "Module 5. Module 32 writes events and adds one module key and six action types."],
            ["The pupil, guardian and primary-guardian records and the roll", "Module 11."],
            ["Staff records, employment status and teaching assignments", "Module 12."],
            ["Classes, class teachers and academic sessions", "Module 13."],
            ["The parent and student apps' sign-in, shell and other screens", "Modules 28 and 27. They mount the message routes inside their own prefixes."],
            ["Assignments and homework", "Unbuilt and unowned. Assignments stay parked."],
        ],
        [2.9, 4.37],
        font_size=8.2,
    )
    add_heading(doc, "1.3 Owner Decisions of 21 October 2026", level=2)
    add_body(
        doc,
        "These eight decisions are recorded and not reopened. They explain why the module has the shape it has.",
    )
    add_real_bullets(
        doc,
        [
            "The first release has staff-to-staff, guardian-to-staff and pupil-to-staff direct conversations, staff-only group chats and staff-only broadcasts.",
            "A staff-to-staff conversation is visible only to its two participants. A school leader has no general inspection override.",
            "A guardian-to-staff conversation concerns one child linked to that guardian. A pupil-to-staff conversation concerns the signed-in pupil.",
            "Every pupil-to-staff conversation is readable by that pupil's primary guardian. Staff holding the welfare oversight key may inspect it read-only, and every inspection is audited. This keeps the 15 October 2026 student portal decision intact.",
            "Group chats contain staff only. An authorised school leader may inspect a group read-only without joining it, and each inspection is audited.",
            "Broadcasts target staff only. The sender sets each broadcast to one-way or replies allowed. A reply goes privately to the sender and other recipients never see it. An authorised leader may inspect the broadcast itself, audited, and that never exposes the private replies.",
            "Guardians and pupils cannot join groups or receive broadcasts.",
            "The first release is text only. Attachments, scheduled sending and SMS are later work, and SMS is never planned: the channels are in-app, email and push.",
        ],
    )
    add_page_break(doc)
    add_heading(doc, "1.4 Build Decisions Awaiting Owner Confirmation", level=2)
    add_callout(
        doc,
        "Decisions taken during the build",
        [
            "Who a family can reach about a child: the class teacher this year, the teachers with a teaching assignment for the class this year (a school setting, default on), and the child's branch's school office, being staff holding school.family_messages.office with the branch in reach. Each must also hold the send and view keys and be current staff.",
            "Staff may start family conversations too, under the same reach rule read the other way.",
            "Guardian-to-staff conversations have no inspection route, because the owner decisions give oversight to pupil conversations, groups and broadcasts only.",
            "Removal from a group ends all access to it, including what was said while the person was a member. Re-adding starts a new membership that sees only what is said afterwards.",
            "A broadcast's reply setting is fixed when it is sent.",
            "A broadcast recipient sees how many people it went to, not who. The sender sees the full list.",
            "A primary guardian reads a pupil's conversation read-only and is not alerted to it. Their opening it changes nothing the pupil or staff see.",
        ],
        kind="attention",
    )

    # ---- 2. Context and status ------------------------------------------------
    add_heading(doc, "2. Context and Status Model", level=1)
    add_body(
        doc,
        "A conversation is a pair, a group or a broadcast. Who may read it is never stored on it: a participant row is a membership period and the place a person's read watermark lives, and whether that person may still read the conversation is derived on every request from the links that matter at that moment. Removing the link ends the access without anybody tidying a row.",
    )
    add_heading(doc, "2.1 Requirement Status", level=2)
    add_status_key(doc)
    add_heading(doc, "2.2 The Six Conversation Kinds", level=2)
    add_table(
        doc,
        ["Kind", "Who is in it", "Branch", "Who may read it"],
        [
            ["STAFF_DIRECT", "Two current staff", "None", "The two participants only"],
            ["FAMILY", "One guardian and one member of staff, about one linked child", "The child's current branch, read at the time", "The two participants. No inspection route"],
            ["PUPIL", "One pupil and one member of staff", "The child's current branch, read at the time", "The two participants, the pupil's primary guardian read-only, and welfare inspectors read-only"],
            ["GROUP", "Staff only: owners and members", "One branch, or school-wide when null", "Members from their join point, and group inspectors read-only"],
            ["BROADCAST", "One sender and 1 to 1,000 staff recipients", "One branch, or school-wide when null", "The sender and recipients, and broadcast inspectors read-only"],
            ["BROADCAST_REPLY", "One recipient and the broadcast's sender", "Follows the broadcast", "The two participants only. Never an inspector"],
        ],
        [1.4, 2.3, 1.55, 2.02],
        font_size=8.0,
    )
    add_callout(
        doc,
        "Current module decision",
        [
            "Module 32 is Backend Complete in MRD v" + MRD_TARGET_VERSION + " with fourteen capability entries, and In use Partial because the pupil and parent home/ responses carry no unread-messages block.",
            "The module is built and uncommitted. Nothing in this document is deployed.",
            "Frontend delivery is recorded apart from the backend: " + frontend_summary() + ".",
        ],
        kind="attention",
    )
    add_page_break(doc)

    # ---- 3. Actors and permissions -------------------------------------------
    add_heading(doc, "3. Actors, Permissions, and Ownership", level=1)
    add_table(
        doc,
        ["Actor", "May do", "Gate or contract"],
        [
            ["Current member of staff", "Read and write in conversations they take part in; start direct, family and pupil conversations within reach; reply privately to a broadcast.", "An active account with a StaffProfile on the roll, plus the keys in section 3.1."],
            ["Group owner", "Rename a group, add or remove members, make another owner.", "The OWNER membership role, checked in the service after the key."],
            ["School leader (inspector)", "Read a group, a broadcast or a pupil conversation without joining it, read-only, audited.", "The inspection key for that kind, with the conversation's branch in reach."],
            ["Guardian", "List, read, write and start family conversations about a linked child.", "Guardian.user is the caller and the link to the child exists. No key and no role."],
            ["Primary guardian", "Read a pupil's conversations read-only.", "A StudentGuardian row with is_primary for the caller. No key and no role."],
            ["Pupil", "List, read, write and start conversations with their own teachers and the school office.", "Student.user is the caller. No key and no role."],
            ["Module 8 workers", "Deliver the four alert events.", "A queued task after commit. Never carries a message."],
        ],
        [1.45, 3.2, 2.62],
        font_size=8.0,
    )
    add_heading(doc, "3.1 Permission Keys and Default Roles", level=2)
    add_table(
        doc,
        ["Key", "Sensitivity", "Default roles", "What it opens"],
        [
            ["school.messages.view", "Normal", "School admin, branch admin, teacher, finance admin", "The staff inbox: conversations the holder takes part in"],
            ["school.messages.send", "Normal", "Same four", "Start a staff direct conversation, write in conversations the holder takes part in, reply privately to a broadcast"],
            ["school.messages.export", "Sensitive", "School admin", "Download a conversation the holder takes part in"],
            ["school.family_messages.send", "Normal", "School admin, branch admin, teacher", "The staff end of family and pupil conversations"],
            ["school.family_messages.office", "Normal", "School admin, branch admin", "Be the school office contact for families at the branches in reach"],
            ["school.message_groups.create", "Normal", "School admin, branch admin", "Create a staff group; the creator becomes its owner"],
            ["school.message_broadcasts.send", "Normal", "School admin, branch admin", "Send a broadcast to staff"],
            ["school.message_groups.inspect", "Sensitive", "School admin", "Read any group in reach, read-only, audited"],
            ["school.message_broadcasts.inspect", "Sensitive", "School admin", "Read any broadcast in reach, read-only, audited, never its replies"],
            ["school.message_welfare.inspect", "Sensitive", "School admin", "Read any pupil conversation in reach, read-only, audited"],
        ],
        [2.2, 0.8, 1.7, 2.57],
        font_size=7.8,
    )
    add_body(
        doc,
        "The keys sit in the Core band with no capability gate, and add the verbs inspect and office to the action vocabulary. The reserved communication keys in Module 8 govern notification templates and delivery and gate nothing here.",
        size=8.5,
        color=GREY,
    )
    add_heading(doc, "3.2 Tenant and Branch Scope", level=2)
    add_real_bullets(
        doc,
        [
            "Every row carries its tenant and is read through the tenant-aware manager. Every route takes ?tenant=<slug> and a trailing slash. A school that has not gone live is closed, because it has nobody to message.",
            "A group or broadcast belongs to one branch or to the whole school (null). A family or pupil conversation takes the child's current branch at read time and stores none.",
            "A school-wide group or broadcast needs a whole-school reach to create. Inspection needs the conversation's branch inside the inspector's reach, and a null-branch group or broadcast needs a whole-school reach.",
            "At a school with one branch the branch dimension is absent from every row, not disabled. At a school with several it appears only on groups and broadcasts.",
            "Replying inside an existing conversation needs no reach. Only starting and addressing do.",
        ],
    )
    add_page_break(doc)

    # ---- 4. Functional requirements ------------------------------------------
    add_heading(doc, "4. Functional Requirements", level=1)
    add_body(
        doc,
        "Each requirement records the required behavior, the inspected evidence, the acceptance boundary and the current limit. Status is current state, not revision history. Test names are modules under schools.vs_messages.tests unless a path says otherwise.",
    )
    for requirement in REQUIREMENTS:
        add_requirement(doc, requirement)

    # ---- 5. Workflows ---------------------------------------------------------
    add_heading(doc, "5. Workflows and Lifecycle Rules", level=1)
    add_heading(doc, "5.1 Starting and Sending", level=2)
    keep_together(add_table(
        doc,
        ["Step", "What happens", "Safety rule"],
        [
            ["1", "The caller names the other end (a user, or a child and a guardian).", "A person out of reach and a person who does not exist answer the same 400."],
            ["2", "The service finds or creates the conversation on its pair key.", "Two taps make one conversation; a simultaneous pair is settled by the unique constraint."],
            ["3", "A conversation started without a message is listed for its starter only.", "The other end sees it once something they can read exists."],
            ["4", "The first or later message is stored under a lock on the conversation row.", "A repeated client_token returns the stored message with 200."],
            ["5", "last_message is updated and the sender's own watermark moves to the new message.", "A person never has their own words counted as unread."],
            ["6", "After commit, an alert task is queued.", "Alert failure never changes the answer to the send."],
        ],
        [0.6, 3.5, 3.17],
        font_size=8.0,
    ))
    add_heading(doc, "5.2 Group Membership", level=2)
    keep_together(add_table(
        doc,
        ["Event", "Effect on the membership", "What the person reads"],
        [
            ["Group created", "Creator becomes OWNER; initial members open at after_message_id 0.", "Everything said in the group."],
            ["Member added", "A new row opens with after_message_id set to the group's newest message id.", "Only messages with a greater id."],
            ["Member removed or leaves", "The row closes (left_at) and removed_by records the owner, or null on leaving.", "Nothing, including earlier messages."],
            ["Member added again", "A new row opens at the then-current newest message id.", "Only what is said after returning."],
            ["Last owner leaves", "Refused with 409 GROUP_NEEDS_OWNER while others remain.", "Unchanged."],
        ],
        [1.6, 3.5, 2.17],
        font_size=8.0,
    ))
    add_heading(doc, "5.3 Revocation", level=2)
    keep_together(add_table(
        doc,
        ["Trigger", "Who loses what", "When"],
        [
            ["Account deactivated", "Everything for that account; also left out of contact lists and alerts.", "Next request, by the active-account gate."],
            ["Staff member off the roll", "Every staff conversation; family and pupil conversations become read-only for the other end.", "Next request."],
            ["Guardian unlinked from the child", "That guardian's family conversations about the child, and a primary guardian's read-only view.", "Next request."],
            ["Guardian.user unset", "The guardian's access, and the staff end sees the conversation read-only.", "Next request."],
            ["Pupil or child off the roll", "The pupil's and every guardian's access to that child's conversations.", "Next request."],
            ["Primary guardian changed", "The read-only view moves to the new primary guardian.", "Next request."],
        ],
        [1.8, 3.6, 1.87],
        font_size=8.0,
    ))

    # ---- 6. Data model --------------------------------------------------------
    add_heading(doc, "6. Data Model and Relationships", level=1)
    add_table(
        doc,
        ["Model", "Purpose", "Key contract"],
        [
            ["Conversation", "One pair, group or broadcast", "kind, title (150), nullable branch, student, guardian, broadcast and reply_mode; pair_key unique per tenant; last_message and last_message_at kept in the sending transaction"],
            ["Participant", "One membership period of one account", "role, joined_at, after_message_id, left_at, removed_by, last_read_message_id; one open period per person per conversation"],
            ["Message", "One text message", "body 1 to 4,000 characters, sender SET_NULL, optional client_token unique per conversation and sender"],
            ["Audit events", "Inspections, exports, group changes, broadcasts", "Written to Module 5 under the MESSAGES key, never as tables of this module"],
            ["Notification records", "Alerts", "Owned by Module 8; metadata holds the conversation id only"],
        ],
        [1.4, 2.0, 3.87],
        font_size=8.0,
    )
    add_heading(doc, "6.1 Data Safety Decisions", level=2)
    add_callout(
        doc,
        "Message data rules",
        [
            "Nothing is hard-deleted by any route. Users are SET_NULL on messages, creators and removers and cascade only on their own membership row; students, guardians and tenants are PROTECT.",
            "An alert, a notification row, a push payload and an audit event never carry message text.",
            "Index (conversation, id) serves the message page, (user, left_at) and (conversation, left_at) serve participation, and a conditional index on (sender, client_token) finds the group or broadcast a retried create already made.",
            "The check constraint holds each kind to its columns, so a malformed conversation cannot be stored through any path.",
        ],
        kind="info",
    )

    # ---- 7. API ---------------------------------------------------------------
    add_heading(doc, "7. API and Validation Contracts", level=1)
    add_body(
        doc,
        "Every success uses the shared envelope. Lists use XVSPagination (success, message, pagination with currentPage, pageSize, totalItems, totalPages, next and previous, and data), and an empty list is data []. Message lists are a cursor. Refusals carry an error_code.",
    )
    add_heading(doc, "7.1 Staff Routes at /v1/messages/", level=2)
    add_table(
        doc,
        ["Route", "Key", "Contract"],
        [
            ["GET conversations/", "messages.view", "Inbox by newest activity; kind (direct, family, group, broadcast), unread, search, page"],
            ["POST conversations/", "messages.send or family_messages.send", "Find-or-create a STAFF_DIRECT, FAMILY or PUPIL conversation and send the first message; 201 new, 200 existing"],
            ["GET conversations/{id}/", "messages.view", "The row plus participants, members or recipients as Field Access allows"],
            ["GET conversations/{id}/messages/", "messages.view", "Cursor page, newest first; before, limit 1 to 100"],
            ["POST conversations/{id}/messages/", "messages.send or family_messages.send", "Send; 201, or 200 on a repeated token; 409 CONVERSATION_READ_ONLY with the reason"],
            ["POST conversations/{id}/read/", "messages.view", "Move the watermark forward; clears the conversation's alerts"],
            ["GET conversations/{id}/export/", "messages.export", "conversation-<id>.csv of what the caller may read"],
            ["GET unread-count/", "messages.view", "total, direct, family, group and broadcast"],
            ["GET contacts/?kind=staff", "messages.send", "Staff the caller may address; paged; 403 field_read_denied when People is hidden"],
            ["GET contacts/?kind=family", "family_messages.send", "Pupils the caller may reach with their guardians; paged"],
            ["POST groups/", "message_groups.create", "name, branch, members, optional body and token; creator is owner"],
            ["PATCH groups/{id}/", "messages.send, owner", "Rename"],
            ["POST groups/{id}/members/", "messages.send, owner", "Add members; each starts at the group's newest message id"],
            ["DELETE groups/{id}/members/{user_id}/", "messages.view, owner or self", "Remove or leave"],
            ["POST groups/{id}/members/{user_id}/owner/", "messages.send, owner", "Make a current member an owner"],
            ["POST broadcasts/", "message_broadcasts.send", "title, body, reply_mode, branch, recipients, token"],
            ["POST broadcasts/{id}/reply/", "messages.send", "A recipient's private reply; 409 BROADCAST_ONE_WAY on a one-way broadcast"],
        ],
        [2.35, 1.75, 3.17],
        font_size=7.7,
    )
    add_heading(doc, "7.2 Oversight Routes at /v1/messages/oversight/", level=2)
    add_table(
        doc,
        ["Route", "Key", "Contract"],
        [
            ["GET groups/", "message_groups.inspect", "Groups in reach with owners, member count, message count and last activity; no message text"],
            ["GET broadcasts/", "message_broadcasts.inspect", "Broadcasts in reach with sender and recipient count"],
            ["GET pupil-conversations/", "message_welfare.inspect", "Pupil conversations in reach with the child and class"],
            ["GET conversations/{id}/", "The key for that kind", "Detail, read_only and inspection true; one audit event per call"],
            ["GET conversations/{id}/messages/", "The key for that kind", "Cursor page of messages; one audit event per call"],
        ],
        [2.35, 1.75, 3.17],
        font_size=7.9,
    )
    add_heading(doc, "7.3 Parent Routes at /v1/portal/parent/messages/", level=2)
    add_table(
        doc,
        ["Route", "Contract"],
        [
            ["GET conversations/", "?child, search, unread, page. Family conversations and read-only pupil conversations of children the caller is primary guardian of; top-level unread_count"],
            ["POST conversations/", "child, staff, optional body and token; find-or-create; 201 or 200"],
            ["GET conversations/{id}/", "One row"],
            ["GET, POST conversations/{id}/messages/", "Cursor page, or send; a watched conversation answers 409 CONVERSATION_READ_ONLY"],
            ["POST conversations/{id}/read/", "conversation and unread_count; changes nothing on a watched conversation"],
            ["GET unread-count/", "total, own family conversations only"],
            ["GET contacts/", "?child required. Reachable staff with labels, can_start and can_start_reason; a child that is not theirs is the portal's 404"],
        ],
        [2.55, 4.72],
        font_size=8.0,
    )
    add_heading(doc, "7.4 Student Routes at /v1/portal/student/messages/", level=2)
    add_body(
        doc,
        "The same routes without the child parameter. The list also carries unread_count and the oversight_notice 'Your parent or guardian and the school can read your messages.' Contacts are the pupil's own teachers and the school office, and the contacts page carries the notice too. Admission is each portal's own PORTAL_PERMISSIONS; there are no keys.",
        size=8.8,
    )
    add_heading(doc, "7.5 Row and Refusal Contracts", level=2)
    add_table(
        doc,
        ["Item", "Contract"],
        [
            ["Staff row", "id, kind, kind_label, title, display_name, counterpart, child, member_count, last_message, last_activity_at, unread_count, reply_mode, read_only, can_send, read_only_reason, role and, only at a multi-branch school on a group or broadcast, branch"],
            ["Portal row", "id, kind (FAMILY or PUPIL), kind_label, display_name, counterpart (user_id, name, label), last_message, last_activity_at, unread_count, read_only, can_send, read_only_reason; a parent's row adds child. Nothing else"],
            ["Message", "id, body, sender (user_id, name), sent_at, mine"],
            ["404 CONVERSATION_NOT_FOUND", "Missing, foreign or inaccessible conversation; one body for all three"],
            ["409 CONVERSATION_READ_ONLY", "Write refused, with the sentence the person can be shown"],
            ["409 BROADCAST_ONE_WAY, 409 GROUP_NEEDS_OWNER", "A reply to a one-way broadcast; the last owner leaving a group with members"],
            ["403 GROUP_OWNER_ONLY, 403 MESSAGE_BRANCH_OUT_OF_REACH", "A member attempting an owner action; a branch or school-wide choice outside reach"],
            ["400 MESSAGE_RECIPIENT_UNAVAILABLE", "A person out of reach, not there or without an account, in the same words"],
            ["400 MESSAGE_BODY_REQUIRED, MESSAGE_BODY_TOO_LONG, MESSAGE_TITLE_INVALID, MESSAGE_RECIPIENTS_INVALID", "Empty or over-long text, a missing or over-long name, an empty or oversized recipient list"],
            ["403 field_read_denied", "A route whose whole response is a hidden field, such as the staff contact list or unread-count"],
        ],
        [2.4, 4.87],
        font_size=7.8,
    )

    # ---- 8. Field Access ------------------------------------------------------
    add_heading(doc, "8. Field Access", level=1)
    add_body(
        doc,
        "Resource school.messages, tenant scope, every field open by default. Stored or submitted fields have separate Read and Write control; computed fields are read-only. The registry also names the ten serializer surfaces that carry them.",
    )
    add_table(
        doc,
        ["Field", "Label", "Access", "Open on create", "Registered API names"],
        [
            ["body", "Message", "Read and Write", "No", "body, preview"],
            ["title", "Group or broadcast name", "Read and Write", "Yes", "title, name"],
            ["participants", "People in the conversation", "Read and Write", "Yes", "counterpart, participants, sender, sender_name, members, recipient, guardian"],
            ["recipients", "Broadcast recipients", "Read and Write", "No", "recipients, recipient_count"],
            ["child", "Pupil the conversation is about", "Read and Write", "No", "child, student"],
            ["reply_mode", "Broadcast reply setting", "Read and Write", "No", "reply_mode"],
            ["sent_at", "Sent times", "Read only", "Not writable", "sent_at, last_activity_at"],
            ["read_status", "Read status and unread counts", "Read only", "Not writable", "unread_count"],
        ],
        [1.05, 1.75, 1.0, 0.85, 2.62],
        font_size=7.8,
    )
    add_heading(doc, "8.1 Rules the Registration Settles", level=2)
    add_real_bullets(
        doc,
        [
            "Write off on Message refuses sending; on Broadcast recipients, addressing a broadcast; on Child, naming a child on a new family or pupil conversation; on Broadcast reply setting, choosing it (an unspecified broadcast is one way). Title and People are open on create so whoever may make a group names it and chooses its first members; the Write switch then decides who may rename it or change its members.",
            "A hidden field leaves every payload completely, including nested and alternate names. A hidden Message drops body matches from search, previews and the export's message column. Hidden People removes the sender column, every name, name search and the staff contact list.",
            "display_name has no switch of its own. It follows Group or broadcast name where it is a title and People in the conversation where it is a person, decided in code.",
            "A label never carries a hidden name: a guardian reads Mother of Tunde Bello only while the Child switch is readable, otherwise Mother.",
            "No switch, recorded as no switch: identifier or transport: kind and kind_label (identifiers), branch (owner decision of 12 October 2026: no branch switches), ids, mine, can_send, read_only, role, member_count (a count of children) and cursor values.",
            "Portal exemption: parents and pupils hold no role, so switches do not reach them. Their payloads are a fixed narrow shape, so a teacher who is also a parent never has her own child's conversation blanked by her staff role.",
        ],
        size=8.6,
    )

    # ---- 9. Audit, alerts, settings ------------------------------------------
    add_page_break(doc)
    add_heading(doc, "9. Audit, Alerts and Settings", level=1)
    add_heading(doc, "9.1 Audit Events", level=2)
    add_table(
        doc,
        ["Module key and action type", "Written when", "Metadata"],
        [
            ["MESSAGES / CONVERSATION_INSPECTED", "Every call to an oversight detail or messages route", "kind, surface, what (detail or messages)"],
            ["MESSAGES / CONVERSATION_EXPORTED", "A participant downloads a conversation", "message_count, columns"],
            ["MESSAGES / MESSAGE_GROUP_CREATED", "A group is created", "members"],
            ["MESSAGES / MESSAGE_GROUP_RENAMED", "An owner renames a group", "kind"],
            ["MESSAGES / MESSAGE_GROUP_MEMBERS_CHANGED", "Members added, removed, left, or an owner made", "added, removed, left or owner"],
            ["MESSAGES / MESSAGE_BROADCAST_SENT", "A broadcast is sent", "recipients, reply_mode"],
        ],
        [2.6, 2.6, 2.07],
        font_size=8.0,
    )
    add_body(
        doc,
        "Every summary names who did what to which conversation, with the display name as the label, and never carries a message body. Individual messages are not audited: they are the record.",
        size=8.6,
    )
    add_heading(doc, "9.2 Alerts Delivered by Module 8", level=2)
    add_table(
        doc,
        ["Event", "To", "Subject", "Body"],
        [
            ["messages.new_message", "Staff", "New message from <sender>, or New message in <group or broadcast title>", "Open Messages to read it."],
            ["messages.new_broadcast", "Staff", "<Sender> sent a broadcast (the broadcast's first message only)", "Open Messages to read it."],
            ["messages.new_message_parent", "Guardian", "New message from <staff name> about <child first name>", "Open the parent app to read it."],
            ["messages.new_message_pupil", "Pupil", "New message from <staff name>", "Open the app to read it."],
        ],
        [1.9, 0.7, 3.0, 1.67],
        font_size=7.9,
    )
    add_real_bullets(
        doc,
        [
            "All four events carry in-app, email and push templates, 12 in all. The email names the school and links the conversation; it carries no message text.",
            "The record family is CONVERSATION with one destination for the prefix messages. and route /messages/{id}. Reading a conversation on any surface acknowledges every alert about it.",
            "The parent push opens /parent/messages/<id>, the pupil push /student/messages/<id>, and a staff push keeps the recipient's own app. A push naming no conversation opens the app's Messages screen.",
            "A recipient who joined after a message, or has already read it by the time a late worker runs, is not alerted to it. A primary guardian only watching is never alerted.",
        ],
        size=8.6,
    )
    add_heading(doc, "9.3 School Settings", level=2)
    add_table(
        doc,
        ["Setting", "Default", "Meaning"],
        [
            ["messages.families_enabled", "On", "Parents may message staff about their children. Off: existing conversations stay readable and nobody starts or writes in one"],
            ["messages.pupils_enabled", "On", "Pupils may message staff. Off: as above for pupil conversations"],
            ["messages.family_reach_subject_teachers", "On", "A family may reach every teacher of the child's class this year, not only the class teacher and the school office"],
        ],
        [2.55, 0.75, 3.97],
        font_size=8.0,
    )

    # ---- 10. Dependencies -----------------------------------------------------
    add_page_break(doc)
    add_heading(doc, "10. Dependencies and Operational Evidence", level=1)
    add_table(
        doc,
        ["Dependency", "Contract"],
        [
            ["Module 3 Identity", "Active accounts, Student.user and Guardian.user links, tenant-scoped sign-in. Staff, guardians and pupils all sign in through it."],
            ["Module 4 RBAC", "The ten keys, the verbs, the Core band, branch reach (visible_branch_ids), bulk key-holder resolution and Field Access evaluation."],
            ["Module 5 Audit", "The MESSAGES module key and six action types, written through emit_audit_event."],
            ["Module 6 Configuration", "The three school settings, read with the student rules resolver."],
            ["Module 8 Notifications", "Four event types, twelve templates, the CONVERSATION record family and destination, acknowledgement, and push destinations."],
            ["Module 11 Students", "The pupil, guardian and StudentGuardian records, the primary-guardian flag, the roll and class enrolment."],
            ["Module 12 Staff", "StaffProfile, employment status (the roll) and teaching assignments."],
            ["Module 13 Academic Structure", "Classes, class teachers and academic sessions used by family reach."],
            ["Modules 27 and 28", "The pupil and parent portals mount the message routes and own their admission rules."],
        ],
        [2.05, 5.22],
        font_size=8.0,
    )
    add_heading(doc, "10.1 Operational Preconditions", level=2)
    add_real_bullets(
        doc,
        [
            "Run the migrations for vs_messages and the vs_audit module-key migration before the app serves traffic.",
            "Seed the verbs and the school permissions, then the configuration catalogue, then the notification event types before their templates, and run sync_field_registry so the school.messages fields exist.",
            "Run Celery workers for alert delivery. Without one, messages are saved and no one is told. Web Push also needs the VAPID keys.",
            "seed_messaging_scenarios is for development databases only. It writes through the real services with alerts held back.",
        ],
        size=8.6,
    )
    add_heading(doc, "10.2 Verification Evidence", level=2)
    add_callout(
        doc,
        "Inspected evidence",
        [
            "The module's own files define 369 test methods in 14 modules under schools/vs_messages/tests: models, visibility, staff API, family, groups, broadcasts, oversight, export, alerts, Field Access, the deep-payload check, the parent portal, the student portal and the seed scenarios. Two shared deep-payload checks from vs_rbac.tests.deep_payload run with them.",
            "On 21 October 2026 manage.py test schools.vs_messages ran 386 tests in 158 seconds and passed (OK). The seed and registry checks in core.test_seed_school_permissions, vs_rbac.tests.test_field_registry and vs_notifications.tests_in_app_subjects were read but not run by this revision, and the full repository suite was not run.",
            "The evidence is backend only. It does not prove a live broker, worker, push service or inbox.",
        ],
        kind="info",
    )

    # ---- 11. Needs Attention --------------------------------------------------
    add_page_break(doc)
    add_heading(doc, "11. Needs Attention", level=1)
    add_table(
        doc,
        ["Priority", "Current gap", "Required completion", "FR"],
        [
            ["P1", "Seven build decisions are unconfirmed", "The owner confirms or changes the decisions in section 1.4: family reach, staff-started family conversations, no guardian-to-staff inspection, removal ending all access, a fixed reply setting, a recipient count without names, and the primary guardian's silent read-only view.", "FR-003, FR-007, FR-009, FR-012"],
            ["P1", "Nothing is committed or deployed", "Commit the module, run the migrations and seeds in section 10.1, and keep a Celery worker running. Until then no school has the module and no alert is delivered.", "FR-016"],
            ["P2", frontend_gap()[0], frontend_gap()[1], "FR-024 to FR-026"],
            ["P2", "Staff reach follows the staff record's branch only", "Consult additional postings when judging staff reach, or record that a home branch is the whole answer.", "FR-012"],
            ["P2", "A leaver's family conversations are not handed over", "Decide how a family conversation moves to the child's new class teacher, or the family must start a new conversation and the old one stays read-only with a departed teacher.", "FR-013"],
            ["P2", "A lost alert is not redispatched", "A broker outage loses the alert, never the message. Add a redispatch or a reconciliation pass if timely alerts matter more than the cost.", "FR-016"],
        ],
        [0.6, 1.65, 3.9, 1.12],
        font_size=7.7,
    )
    add_callout(
        doc,
        "Removal rule",
        [
            "Remove a gap only when the implementation and its verification fully resolve it, and rewrite it when it is only partly resolved.",
            "Do not infer frontend completion, deployment, worker uptime, push delivery or inbox arrival from backend code evidence.",
        ],
        kind="attention",
    )

    # ---- 12. Not in this release ---------------------------------------------
    add_heading(doc, "12. Not in this Release", level=1)
    add_real_bullets(
        doc,
        [
            "Attachments and files of any kind. The first release is text only, by owner decision.",
            "Editing or deleting a message, reactions, typing and online presence.",
            "Scheduled sending.",
            "Guardian or pupil groups, and inspection of guardian-to-staff conversations.",
            "SMS. It is never planned: the channels are in-app, email and push.",
            "Assignments and homework, which stay parked and unowned.",
        ],
        size=8.8,
    )

    # ---- 13. Traceability -----------------------------------------------------
    add_page_break(doc)
    add_heading(doc, "13. MRD Traceability", level=1)
    add_body(
        doc,
        f"Module 32 carries 14 capability entries in MRD v{MRD_TARGET_VERSION}. Each maps to the controlling requirements below. The three frontend requirements are not capability entries: the MRD does not infer frontend delivery from backend evidence.",
    )
    add_table(
        doc,
        ["MRD Module 32 capability", "Requirements", "Current state"],
        [
            ["Staff direct conversations, private to the two participants", "FR-001, FR-002, FR-011", "Implemented"],
            ["Family conversations about a linked child", "FR-003, FR-012, FR-013", "Implemented"],
            ["Pupil conversations with a primary guardian read-only view", "FR-004, FR-005", "Implemented"],
            ["Staff groups with owners and a later-join boundary", "FR-006, FR-007", "Implemented"],
            ["Staff broadcasts with one-way or private replies", "FR-009, FR-010", "Implemented"],
            ["Audited oversight of groups, broadcasts and pupil conversations", "FR-005, FR-008, FR-010, FR-022", "Implemented"],
            ["One visibility gate and immediate revocation", "FR-011, FR-013", "Implemented"],
            ["Contact reach by branch, class and school office", "FR-012", "Implemented"],
            ["Inbox, search, unread counts and read state", "FR-014", "Implemented"],
            ["Idempotent sends and starts with cursor paging", "FR-015", "Implemented"],
            ["Body-free alerts by in-app, email and push", "FR-016, FR-017", "Implemented"],
            ["Conversation CSV export with a spreadsheet-formula guard", "FR-018", "Implemented"],
            ["Field Access over messages", "FR-021", "Implemented"],
            ["Permissions, default roles and school settings", "FR-019, FR-020, FR-023", "Implemented"],
            ["Staff, parent and student screens", "FR-024, FR-025, FR-026", frontend_state_text()],
        ],
        [3.35, 2.15, 1.77],
        font_size=7.8,
    )

    # ---- 14. Change log -------------------------------------------------------
    add_heading(doc, "14. Change Log", level=1)
    change_log = add_table(
        doc,
        ["Version", "Date", "Summary"],
        [
            [
                FRD_VERSION,
                CHANGE_DATE,
                f"First functional baseline for Module 32. Records the eight owner decisions of 21 October 2026 and the seven build decisions awaiting confirmation, the six conversation kinds, the single visibility gate, revocation, groups and broadcasts, audited inspection, alerts through Module 8, the CSV export, ten keys, three settings, the school.messages Field Access resource, and traceability to MRD v{MRD_TARGET_VERSION}. Backend evidence only; nothing here is committed or deployed.",
            ],
        ],
        [0.75, 1.15, 5.37],
        font_size=8.0,
    )
    row = change_log.add_row()
    merged = row.cells[0].merge(row.cells[1]).merge(row.cells[2])
    write_cell(
        merged,
        "MAINTENANCE RULE | Version this FRD independently. Update it when Module 32 behavior, acceptance, permissions, data contracts, APIs, alert behavior, Field Access, dependencies, status or current gaps change. Preserve every prior version.",
        size=8,
        bold=True,
        color=BLUE,
        fill=PALE_BLUE,
    )
    set_table_widths(change_log, [0.75, 1.15, 5.37])

    normalise_schema_order(doc)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    doc.save(str(output_path))
    update_extended_title(output_path, title)
    shrink_inherited_media(output_path)
    assert_no_em_dash(output_path)
    assert_vocabulary(output_path)


def frontend_state_text() -> str:
    states = {REQUIREMENT_STATUS[k] for k in ("FR-024", "FR-025", "FR-026")}
    if states == {SPECIFIED}:
        return "Specified; backend contract implemented"
    if states == {FRONTEND_REVIEW}:
        return FRONTEND_REVIEW
    return "Mixed: " + frontend_summary()


# ---------------------------------------------------------------------------
# MRD
# ---------------------------------------------------------------------------

W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
AMBER_FILL = "FBF1D7"
GREEN_FILL = "E3F3E9"

MODULE32_CODE = (
    "Code: schools.vs_messages, mounted at /v1/messages/; portal routes in schools.vs_messages mounted "
    "inside /v1/portal/parent/messages/ and /v1/portal/student/messages/; shared seams in vs_notifications, "
    "vs_audit, vs_config, vs_rbac and core"
)

MODULE32_DESCRIPTION = (
    "Conversations between the people of one school: private staff-to-staff direct conversations, "
    "guardian-to-staff conversations about a linked child, pupil-to-staff conversations that the pupil's "
    "primary guardian reads read-only, staff groups with owners and a later-join boundary, and staff "
    "broadcasts with one-way or private replies. One visibility service gates every read, count, search, "
    "export and alert, re-derives access on every request, and answers a missing, foreign or inaccessible "
    "conversation with the same 404. Leaders may inspect groups, broadcasts and pupil conversations "
    "read-only, every opening is audited, and an inspection never exposes broadcast replies. Module 8 "
    "delivers the new-message alerts without carrying any message text. Documented by M32 FRD v"
    + FRD_VERSION + "."
)

MODULE32_CAPABILITIES = [
    "Staff direct conversations, private to the two participants",
    "Family conversations about a linked child",
    "Pupil conversations with a primary guardian read-only view",
    "Staff groups with owners and a later-join boundary",
    "Staff broadcasts with one-way or private replies",
    "Audited oversight of groups, broadcasts and pupil conversations",
    "One visibility gate and immediate revocation",
    "Contact reach by branch, class and school office",
    "Inbox, search, unread counts and monotonic read state",
    "Idempotent sends and starts with cursor paging",
    "Body-free alerts by in-app, email and push",
    "Conversation CSV export with a spreadsheet-formula guard",
    "Field Access over messages, names, recipients and times",
    "Ten permission keys, default roles and three school settings",
]

MRD_CHANGE_SUMMARY = (
    "Adds Module 32 Messaging & Conversations: staff direct conversations, family and pupil "
    "conversations, staff groups with a later-join boundary, staff broadcasts with private replies, "
    "audited oversight, one visibility gate with immediate revocation, body-free alerts through Module 8, "
    "a guarded CSV export, ten keys, three settings and the school.messages Field Access resource. "
    "Modules rise from 31 to 32 and capability entries from 513 to 527. Module 8 now delivers the four "
    "new-message alerts and no longer carries conversation threads in its Needs Attention; Modules 27 "
    "and 28 stop parking Messages and point at Module 32. Adds one P2 priority gap for the unread-messages "
    "block missing from both portals' home/ responses. Repairs what v2.108 left drifted: the Module 27 and 28 header rows "
    "(stale Planned and Not started cells), this change log's header row and the blue styling its own "
    "entry took, the Document Control label colours, and the banding and Partial colours of the module "
    "index rows. Reconciled to the new Module 32 FRD v" + FRD_VERSION + "; the Module 8, 27, "
    "28, 4 and 5 FRDs are queued for revision. Verified by schools.vs_messages (386 tests, OK); the full repository suite was not run. "
    "Backend evidence only; the module is uncommitted and nothing here is deployed. " + "Frontends: " + frontend_summary() + "."
)


def p_text(p) -> str:
    return "".join(t.text or "" for t in p.iter(W + "t"))


def set_paragraph_text(paragraph, text: str) -> None:
    """Write *text* into the paragraph's first run and drop the rest, keeping its formatting."""
    runs = paragraph.runs
    if runs:
        runs[0].text = text
        for run in runs[1:]:
            run._r.getparent().remove(run._r)
    else:
        paragraph.add_run(text)


def set_cell_lines(cell, lines: list[str]) -> None:
    """Rewrite a multi-paragraph cell positionally.

    Paragraph i holds line i. A line beyond the existing paragraphs clones the
    last paragraph; surplus paragraphs are removed. Cloning the first would
    turn every bullet into the bold heading.
    """
    paragraphs = list(cell.paragraphs)
    while len(paragraphs) < len(lines):
        clone = copy.deepcopy(paragraphs[-1]._p)
        paragraphs[-1]._p.addnext(clone)
        paragraphs = list(cell.paragraphs)
    for paragraph in paragraphs[len(lines):]:
        paragraph._p.getparent().remove(paragraph._p)
    for paragraph, line in zip(cell.paragraphs, lines):
        set_paragraph_text(paragraph, line)


def replace_once(paragraph, old: str, new: str) -> None:
    """Replace *old* inside one run, so a bold lead-in survives; collapse only when it spans runs."""
    if old not in paragraph.text:
        raise ValueError(f"Expected text not found: {old[:60]!r}")
    for run in paragraph.runs:
        if old in run.text:
            run.text = run.text.replace(old, new, 1)
            return
    set_paragraph_text(paragraph, paragraph.text.replace(old, new, 1))


def set_label_value(paragraph, label: str, value: str) -> None:
    """A bold label run followed by a plain value run, as a module header cell has them."""
    runs = paragraph.runs
    if len(runs) >= 2:
        runs[0].text = label
        runs[1].text = value
        for run in runs[2:]:
            run._r.getparent().remove(run._r)
    else:
        set_paragraph_text(paragraph, label + value)


def find_paragraph(doc, startswith: str):
    for paragraph in doc.paragraphs:
        if paragraph.text.strip().startswith(startswith):
            return paragraph
    raise ValueError(f"Paragraph not found: {startswith[:60]!r}")


def cell_paragraph_containing(table, needle: str):
    for tc in table._tbl.iter(W + "tc"):
        for p in tc.iter(W + "p"):
            if needle in p_text(p):
                from docx.text.paragraph import Paragraph

                return Paragraph(p, table)
    raise ValueError(f"Cell text not found: {needle[:60]!r}")


def set_fill(cell, fill: str) -> None:
    shd = cell._tc.tcPr.find(qn("w:shd"))
    shd.set(qn("w:fill"), fill)


def rebuild_module_header(table, backend: str, in_use: str, code: str, *, template) -> None:
    """Give *table* the four cells of a healthy module header, copied from *template*.

    A header whose first cell holds the whole line and whose other cells still
    show an earlier state is replaced by a copy of a well-formed one.
    """
    fresh = copy.deepcopy(template._tbl)
    table._tbl.addprevious(fresh)
    table._tbl.getparent().remove(table._tbl)
    from docx.table import Table

    new = Table(fresh, table._parent)
    cells = new.rows[0].cells
    fill = {"Complete": GREEN_FILL, "Partial": AMBER_FILL}
    texts = [("Phase: ", "V1"), ("Backend: ", backend), ("In use: ", in_use), ("Code: ", code)]
    for index, (label, value) in enumerate(texts):
        paragraph = cells[index].paragraphs[0]
        if index == 2:
            set_paragraph_text(paragraph, f"{label}{value}")
        else:
            set_label_value(paragraph, label, value)
    set_fill(cells[1], fill[backend])
    set_fill(cells[2], fill[in_use])
    return new


def normalise_index_rows(table, texts: dict) -> None:
    """Rebuild every data row of the module index from a healthy one.

    Odd rows copy row 1 (white) and even rows copy row 2 (banded), so a row whose
    alignment, size or Partial colour had drifted comes back to the same look.
    Backend and In use cells take the green or amber of their word.
    """
    exemplars = {1: copy.deepcopy(table.rows[1]._tr), 0: copy.deepcopy(table.rows[2]._tr)}
    fills = {"Complete": GREEN_FILL, "Partial": AMBER_FILL}
    for number in range(1, len(table.rows)):
        old = table.rows[number]._tr
        fresh = copy.deepcopy(exemplars[number % 2])
        old.addprevious(fresh)
        old.getparent().remove(old)
        row = table.rows[number]
        for column, (cell, text) in enumerate(zip(row.cells, texts[number])):
            set_paragraph_text(cell.paragraphs[0], text)
            if column in (3, 4):
                set_fill(cell, fills[text])


def whiten_labels(table) -> None:
    """Document-control labels sit on dark blue: write them white and bold in every row."""
    from docx.shared import RGBColor

    for row in table.rows:
        if row.cells[0].text.strip().upper().startswith("EVIDENCE BOUNDARY"):
            continue
        for paragraph in row.cells[0].paragraphs:
            for run in paragraph.runs:
                run.bold = True
                run.font.color.rgb = RGBColor.from_string("FFFFFF")


def patch_mrd(source_path: Path, output_path: Path) -> None:
    doc = Document(str(source_path))
    # Bind every table by its position in the source before anything is inserted.
    T = list(doc.tables)
    body = doc.element.body
    doc.core_properties.title = f"XVS Module Requirements Document v{MRD_TARGET_VERSION}"
    doc.core_properties.version = MRD_TARGET_VERSION

    # Cover and document control.
    for paragraph in T[0]._tbl.iter(W + "p"):
        for t in paragraph.iter(W + "t"):
            if t.text and MRD_SOURCE_VERSION in t.text:
                t.text = t.text.replace(MRD_SOURCE_VERSION, MRD_TARGET_VERSION)
    control = T[1]
    whiten_labels(control)
    for row in control.rows:
        label = row.cells[0].text.strip()
        value = row.cells[1]
        if label == "Version":
            set_paragraph_text(value.paragraphs[0], MRD_TARGET_VERSION)
        elif label == "Review date":
            set_paragraph_text(value.paragraphs[0], REVIEW_DATE)
        elif label == "Source scope":
            set_paragraph_text(
                value.paragraphs[0],
                "Backend main at 408bb201 plus the uncommitted Module 32 build, 21 October 2026. "
                "Backend evidence only; no frontend or deployment is claimed.",
            )
        elif label == "Modules":
            set_paragraph_text(value.paragraphs[0], "32")
        elif label == "Capability entries":
            set_paragraph_text(value.paragraphs[0], "527")

    # Contents.
    for row in T[2].rows:
        first = row.cells[0].text.strip()
        if first.startswith("2. Module Index"):
            set_paragraph_text(row.cells[1].paragraphs[0], "All 32 modules with backend and in-use states")
        elif first.startswith("5. Current Capability Contracts"):
            set_paragraph_text(
                row.cells[1].paragraphs[0],
                "Current contracts by area; messaging added as Module 32 with 14 capability entries",
            )

    replace_once(
        find_paragraph(doc, "XVision is a multi-tenant school-management platform"),
        "31-module roadmap", "32-module roadmap",
    )
    replace_once(
        cell_paragraph_containing(T[4], "Notifications deliver in-app and email events"),
        "Notifications deliver in-app and email events. They do not currently provide internal chat or SMS.",
        "Notifications deliver in-app, email and push events. Conversations between people belong to Module 32 and are not part of notification delivery; there is no SMS.",
    )
    replace_once(
        find_paragraph(doc, "vs_schools, vs_onboarding, vs_academics"),
        "The schools product also mounts vs_attendance and vs_gradebook;",
        "The schools product also mounts vs_attendance, vs_gradebook and vs_messages (at v1/messages/, with its portal routes mounted inside the student and parent portals);",
    )

    # Module index: add row 32, then give every row the banding and status colours of a healthy one.
    index_table = T[5]
    last = index_table.rows[-1]._tr
    last.addnext(copy.deepcopy(last))
    texts = {}
    for number, row in enumerate(index_table.rows):
        if number:
            texts[number] = [cell.text.strip() for cell in row.cells]
    texts[32] = ["32", "Messaging & Conversations", "V1", "Complete", "Partial", "14"]
    normalise_index_rows(index_table, texts)

    # Tier 6 description.
    tier6 = T[59]
    set_paragraph_text(
        tier6.rows[0].cells[0].paragraphs[1],
        "Operational dashboards, reusable export delivery, the student and parent apps, and the conversations they share with staff.",
    )

    # Module 8: delivers alerts, does not own conversations.
    replace_once(
        find_paragraph(doc, "Event-driven in-app and email delivery."),
        "This is a notification engine, not a person-to-person chat product.",
        "This is a notification engine, not a person-to-person chat product: conversations belong to Module 32, and this module delivers its four new-message alerts (record family CONVERSATION, in-app, email and push) without carrying any message text.",
    )
    needs = cell_paragraph_containing(
        T[23], "Conversation threads, replies, attachments, muting, SMS, and future-send scheduling"
    )
    set_paragraph_text(
        needs,
        "• Conversation threads and replies are owned by Module 32, which this module serves with four alert events, the CONVERSATION record family and their push destinations; the M08 FRD revision for them is queued. Attachments, muting, SMS, and future-send scheduling remain outside the completed scope because no current backend implements them.",
    )

    # Modules 27 and 28: repair the header rows, stop parking Messages.
    healthy_header = T[62]
    new27 = rebuild_module_header(
        T[64], "Partial", "Partial", "schools.vs_student_portal, mounted at /v1/portal/student/",
        template=healthy_header,
    )
    new28 = rebuild_module_header(
        T[66], "Partial", "Partial", "schools.vs_parent_portal, mounted at /v1/portal/parent/",
        template=healthy_header,
    )
    del new27, new28
    replace_once(
        find_paragraph(doc, "Mounted pupil self-service backend"),
        "Messages and Assignments remain unbuilt. Documented by M27 FRD v1.0.",
        "Messages are built by Module 32 and mounted here at /v1/portal/student/messages/; Assignments remain unbuilt. Documented by M27 FRD v1.0, which predates Module 32 and is queued for revision.",
    )
    replace_once(
        cell_paragraph_containing(T[65], "Messages and Assignments have no owning module"),
        "Messages and Assignments have no owning module and are not built.",
        "Messages are owned by Module 32 and mounted at /v1/portal/student/messages/, with the pupil's primary guardian reading them read-only; Assignments have no owning module and are not built.",
    )
    replace_once(
        find_paragraph(doc, "Mounted parent self-service backend"),
        "Messages and Assignments remain unbuilt. Documented by M28 FRD v1.3.",
        "Messages are built by Module 32 and mounted here at /v1/portal/parent/messages/; Assignments remain unbuilt. Documented by M28 FRD v1.3, which predates Module 32 and is queued for revision.",
    )
    replace_once(
        cell_paragraph_containing(T[67], "Messages and Assignments are unbuilt"),
        "Messages and Assignments are unbuilt.",
        "Messages are owned by Module 32 and mounted at /v1/portal/parent/messages/, covering family conversations and the read-only view of pupil conversations; Assignments are unbuilt.",
    )

    # Module 32 block, cloned from the healthy Module 26 block and placed after Module 28.
    kids = list(body.iterchildren())
    heading26 = next(k for k in kids if k.tag == W + "p" and p_text(k).startswith("Module 26:"))
    desc26 = T[62]._tbl.getnext()
    spacer = T[63]._tbl.getnext()
    anchor = T[67]._tbl.getnext()  # the blank spacer; the page break is the paragraph after it
    heading = copy.deepcopy(heading26)
    header_tbl = copy.deepcopy(T[62]._tbl)
    desc = copy.deepcopy(desc26)
    caps_tbl = copy.deepcopy(T[63]._tbl)
    new_spacer = copy.deepcopy(spacer)
    anchor.addnext(heading)
    heading.addnext(header_tbl)
    header_tbl.addnext(desc)
    desc.addnext(caps_tbl)
    caps_tbl.addnext(new_spacer)

    from docx.table import Table
    from docx.text.paragraph import Paragraph

    set_paragraph_text(Paragraph(heading, doc._body), "Module 32: Messaging & Conversations")
    header = Table(header_tbl, doc._body)
    cells = header.rows[0].cells
    set_label_value(cells[0].paragraphs[0], "Phase: ", "V1")
    set_label_value(cells[1].paragraphs[0], "Backend: ", "Complete")
    set_paragraph_text(cells[2].paragraphs[0], "In use: Partial")
    set_fill(cells[2], AMBER_FILL)
    set_label_value(cells[3].paragraphs[0], "Code: ", MODULE32_CODE.removeprefix("Code: "))
    set_paragraph_text(Paragraph(desc, doc._body), MODULE32_DESCRIPTION)

    caps = Table(caps_tbl, doc._body)
    capability_rows = list(caps.rows[:-1])
    box_row = caps.rows[-1]
    needed_rows = (len(MODULE32_CAPABILITIES) + 1) // 2
    while len(capability_rows) > needed_rows:
        capability_rows[-1]._tr.getparent().remove(capability_rows[-1]._tr)
        capability_rows.pop()
    for number, row in enumerate(capability_rows):
        for side, cell in enumerate(row.cells[:2]):
            set_paragraph_text(cell.paragraphs[0], "▸ " + MODULE32_CAPABILITIES[number * 2 + side])
    set_cell_lines(
        box_row.cells[0],
        [
            "CURRENT DECISION",
            "• Module 32 is Backend Complete and In use Partial with fourteen capability entries, reconciled to M32 FRD v" + FRD_VERSION + ". The code is built and uncommitted, and nothing here is deployed.",
            "• Conversations were never part of Module 8. Module 8 delivers four new-message events and their templates through the CONVERSATION record family, and no message text appears in any channel or stored notification.",
            "• The eight owner decisions of 21 October 2026 stand: staff direct conversations are visible to their two participants only; every pupil conversation is readable by the primary guardian and inspectable read-only and audited under the welfare key; groups and broadcasts are staff only; text only.",
            "• Seven build decisions await the owner's confirmation and are recorded in M32 FRD v" + FRD_VERSION + " section 1.4: family reach, staff-started family conversations, no guardian-to-staff inspection, removal ending all access to a group, a fixed broadcast reply setting, a recipient count without names, and the primary guardian's silent read-only view.",
            "• In use is Partial because the pupil and parent home/ responses carry no unread-messages block; each portal serves its own unread-count route instead.",
            "• Frontend delivery is separate from the backend: " + frontend_summary() + ". No frontend, deployment or adoption is inferred.",
            "• Verified by schools.vs_messages (386 tests, OK, 21 October 2026); the seed and registry checks in other apps and the full repository suite were not run.",
            "• Field Access: school.messages registers eight fields, open by default; portal payloads are a fixed narrow shape that no role switch reaches. Audit gains the MESSAGES module key and six action types; RBAC gains ten school keys and the verbs inspect and office; configuration gains three school settings.",
            "• Needs Attention is recorded in the FRD: unconfirmed build decisions, nothing committed or deployed, frontend delivery, staff reach by home branch only, no hand-over of a leaver's family conversations, and no redispatch of a lost alert.",
        ],
    )

    # Priority gaps: the unread-messages block missing from both portal homes.
    gaps = T[75]
    template_row = gaps.rows[5]._tr
    new_gap = copy.deepcopy(template_row)
    gaps.rows[5]._tr.addnext(new_gap)
    gap_row = gaps.rows[6]
    gap_values = [
        "P2",
        "Messages missing from the portal homes",
        "Add the unread-messages block the designs show to the pupil and parent home/ responses, reading each portal's messages unread-count, then retire this item. Until then Module 32 reads In use Partial.",
        "M27, M28, M32",
    ]
    for cell, value in zip(gap_row.cells, gap_values):
        set_paragraph_text(cell.paragraphs[0], value)

    # Build order: portals.
    order = T[76]
    for row in order.rows:
        if row.cells[1].text.strip().startswith("Build portals last"):
            cell = row.cells[2]
            set_paragraph_text(
                cell.paragraphs[0],
                cell.paragraphs[0].text
                + " Both portals are now mounted and Messages (Module 32) is built behind them; Assignments remain unbuilt.",
            )
            break
    else:
        raise ValueError("Build portals last row not found")

    # Section 5 wording that said the module taxonomy was 31.
    first_contract = find_paragraph(doc, "The current subscription contract registers every newly created school")
    replace_once(first_contract, "31-module taxonomy", "module taxonomy")
    replace_once(find_paragraph(doc, "• Status and ownership: The affected module statuses"), "31-module taxonomy", "module taxonomy")

    # Section 6.4: this revision's contracts, cloned from 6.3.
    heading63 = next(k for k in body.iterchildren() if k.tag == W + "p" and p_text(k).startswith("6.3 19 October 2026"))
    paras63 = []
    node = heading63.getnext()
    while node is not None and node.tag == W + "p" and not p_text(node).startswith("7. Change Log"):
        paras63.append(node)
        node = node.getnext()
    change_heading = node
    new_heading = copy.deepcopy(heading63)
    change_heading.addprevious(new_heading)
    set_paragraph_text(Paragraph(new_heading, doc._body), "6.4 21 October 2026 Capability Contracts")
    texts64 = [
        "Messaging and Conversations is Module 32. It gives a school staff direct conversations, family conversations about a linked child, pupil conversations the primary guardian reads read-only, staff groups with a later-join boundary and staff broadcasts with private replies. One visibility service decides every read, count, search, export and alert, and access is re-derived on every request.",
        "Module 8 delivers four new-message alerts through the CONVERSATION record family without carrying message text. Module 5 gains the MESSAGES module key and six action types, Module 4 gains ten school keys and the verbs inspect and office, and Module 6 gains three school settings. Messages is no longer parked in Modules 8, 27 or 28.",
        "The module count rises from 31 to 32 and capability entries from 513 to 527. Module 32 is Backend Complete and In use Partial. The FRDs for Modules 8, 27, 28, 4 and 5 are queued for revision. Frontend delivery, deployment and production adoption are not inferred.",
    ]
    for index, text in enumerate(texts64):
        source = paras63[min(index, len(paras63) - 1)]
        clone = copy.deepcopy(source)
        change_heading.addprevious(clone)
        set_paragraph_text(Paragraph(clone, doc._body), text)

    # Change log: restore the header row and ordinary styling, then add this entry.
    log = T[77]
    row_header = log.rows[1]._tr
    row_dup = log.rows[0]._tr
    text108 = [cell.text for cell in log.rows[1].cells]
    data_template = copy.deepcopy(row_dup)
    # 1. Make the header-styled row a real header and move it to the top.
    for cell, label in zip(log.rows[1].cells, ["Version", "Date", "Summary"]):
        paragraph = cell.paragraphs[0]
        set_paragraph_text(paragraph, label)
        run = paragraph.runs[0]
        run.bold = True
        run.font.color.rgb = _white()
    row_dup.addprevious(row_header)
    # 2. The duplicated 2.107 row at the top of the data becomes 2.109.
    rows = list(log.rows)
    set_row_texts(rows[1], [MRD_TARGET_VERSION, REVIEW_DATE, MRD_CHANGE_SUMMARY])
    # 3. A fresh ordinary row carries 2.108 beneath it.
    row_108 = copy.deepcopy(data_template)
    rows[1]._tr.addnext(row_108)
    set_row_texts(log.rows[2], text108)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    doc.save(str(output_path))
    update_extended_title(output_path, f"XVS Module Requirements Document v{MRD_TARGET_VERSION}")
    shrink_inherited_media(output_path)
    assert_no_em_dash(output_path)
    assert_vocabulary(output_path)


def _white():
    from docx.shared import RGBColor

    return RGBColor.from_string("FFFFFF")


def set_row_texts(row, texts: list[str]) -> None:
    for cell, text in zip(row.cells, texts):
        set_paragraph_text(cell.paragraphs[0], text)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--reference-frd", type=Path, required=True)
    parser.add_argument("--frd-output", type=Path, required=True)
    parser.add_argument("--mrd-source", type=Path, required=True)
    parser.add_argument("--mrd-output", type=Path, required=True)
    parser.add_argument(
        "--force", action="store_true",
        help="Overwrite outputs left by an earlier run of this script (a draft not yet committed).",
    )
    args = parser.parse_args()

    for target in (args.frd_output, args.mrd_output):
        if target.exists() and not args.force:
            raise SystemExit(
                f"Refusing to overwrite {target}. A published version is never rewritten; "
                "pass --force only for your own uncommitted draft."
            )

    build_frd(args.reference_frd, args.frd_output)
    patch_mrd(args.mrd_source, args.mrd_output)
    print(args.frd_output)
    print(args.mrd_output)


if __name__ == "__main__":
    main()

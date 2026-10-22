# Messaging and Conversations (Module 32): design and API contract

This is the contract the backend (`schools.vs_messages`), school-fe, parent-fe
and student-fe are built against, and the source the Module 32 FRD is written
from. Where code and this file disagree after the build, section 14 (as built)
wins and the rest is corrected.

## 1. Owner decisions (21 October 2026, recorded, do not reopen)

1. The first release has staff-to-staff, guardian-to-staff and pupil-to-staff
   direct conversations, staff-only group chats and staff-only broadcasts.
2. A staff-to-staff direct conversation is visible only to its two
   participants. A school leader has no general inspection override.
3. A guardian-to-staff conversation concerns one child linked to that guardian.
   A pupil-to-staff conversation concerns the signed-in pupil.
4. Every pupil-to-staff conversation is readable by that pupil's primary
   guardian. Staff holding the welfare oversight key may inspect it read-only,
   and every inspection is audited (student portal plan decision of 15 October
   2026, kept intact).
5. Group chats contain staff only. An authorised school leader may inspect a
   group read-only without joining it; each inspection is audited.
6. Broadcasts target staff only. The sender sets each broadcast to one-way or
   replies allowed. A reply goes privately to the sender; other recipients
   never see it. An authorised leader may inspect the broadcast itself, audited;
   that never exposes the private replies.
7. Guardians and pupils cannot join groups or receive broadcasts.
8. Text only in the first release. Attachments, SMS and scheduled sending are
   later work (SMS is never planned: channels are in-app, email and push).

## 2. Decisions taken in the build (confirmed by the owner, 21 October 2026)

The owner confirmed all seven on 21 October 2026, so they stand beside the
decisions of section 1 and are not reopened.

- **Who a family can reach about a child.** The child's class teacher this
  year, the teachers with a teaching assignment for the child's class this year
  (a school setting, default on), and the child's branch's "school office":
  staff holding `school.family_messages.office` whose branch reach covers the
  child's branch. Every one of them must also hold `school.family_messages.send`
  and `school.messages.view`, and be current staff. The same rule, read the
  other way, says which pupils and guardians a member of staff may start a
  conversation with.
- **Staff may start family conversations too**, under the same reach rule.
- **Guardian-to-staff conversations have no inspection route.** The owner's
  decisions give oversight to pupil conversations, groups and broadcasts only.
- **Removal from a group ends all access to it**, including what was said while
  the person was a member. Re-adding them starts a new membership that again
  sees only what is said after it.
- **A broadcast's reply setting is fixed when it is sent.**
- **A broadcast recipient sees how many people it went to, not who.** The
  sender sees the full recipient list.
- **A primary guardian reads a pupil's conversation read-only** and is not
  alerted to it; their opening it changes nothing the pupil or staff see.

## 3. Where it lives

- App `schools.vs_messages` (label `vs_messages`) under `apps/schools/`. It
  knows guardians and pupils, so it is a school app, not an engine. Engines are
  touched only through their public seams: `vs_notifications` gains event
  types, templates, a record family and routes (domain-neutral words:
  "conversation", "message"); `vs_audit` gains a module key and action types;
  `vs_config` gains three catalogue settings; `core` seeds the keys and a verb.
- Staff routes at `/v1/messages/`. Portal routes inside the existing portal
  apps: `/v1/portal/parent/messages/...` and `/v1/portal/student/messages/...`,
  calling `vs_messages` services. Every route takes `?tenant=<slug>` like its
  neighbours, with a trailing slash.
- `pending_tenant_surface` stays closed: a school that has not gone live has
  nobody to message.

## 4. Data model

All rows carry `tenant` and use `TenantAwareManager` / `all_objects` like
`vs_notices`. Nothing is hard-deleted by any route; users are `SET_NULL`.

### Conversation

| field | notes |
| --- | --- |
| `kind` | `STAFF_DIRECT`, `FAMILY` (guardian and staff), `PUPIL` (pupil and staff), `GROUP`, `BROADCAST`, `BROADCAST_REPLY` |
| `title` | 150. Group name or broadcast title; blank for the other kinds |
| `branch` | nullable. Groups and broadcasts only: the branch it belongs to, null = school-wide. Family and pupil conversations take the child's current branch at read time, never a stored copy |
| `student` | FK `vs_students.Student`, nullable. Set for `FAMILY` and `PUPIL`, null otherwise (check constraint) |
| `guardian` | FK `vs_students.Guardian`, nullable. Set for `FAMILY` only |
| `broadcast` | FK self, nullable. Set for `BROADCAST_REPLY` only: the broadcast replied to |
| `reply_mode` | `ONE_WAY` / `PRIVATE_REPLIES`, blank unless `BROADCAST` |
| `pair_key` | char 120, blank for groups and broadcasts. Unique per tenant when not blank: `staff:<low uid>:<high uid>`, `family:<guardian id>:<staff uid>:<student id>`, `pupil:<student id>:<staff uid>`, `reply:<broadcast id>:<recipient uid>`. Starting a conversation is find-or-create on it, so two taps make one conversation |
| `created_by`, `created_at` | |
| `last_message_id`, `last_message_at` | denormalised, updated in the same transaction as each send |

### Participant (one membership period)

| field | notes |
| --- | --- |
| `conversation`, `user` | |
| `role` | `MEMBER`, `OWNER` (group owner), `SENDER` (broadcast), `RECIPIENT` (broadcast), `STAFF`, `GUARDIAN`, `PUPIL` |
| `joined_at` | |
| `after_message_id` | BigInteger, default 0. The participant sees messages with `id > after_message_id`. Set to the conversation's `last_message_id` when someone is added to an existing group, 0 otherwise |
| `left_at`, `removed_by` | a removed or departed member's row closes; access ends |
| `last_read_message_id`, `last_read_at` | read watermark, only ever moves forward |

Unique `(conversation, user)` where `left_at IS NULL`: one open period per
person. Index `(user, left_at)` and `(conversation, left_at)`.

### Message

| field | notes |
| --- | --- |
| `conversation`, `sender` (SET_NULL), `body` (text, 1 to 4,000 characters after trimming), `created_at` | |
| `client_token` | UUID, nullable. Unique `(conversation, sender, client_token)`. A retried send with the same token returns the stored message with 200 instead of a second one |

Index `(conversation, id)`. Messages are never edited or deleted in this
release.

Inspections, exports, group membership changes and broadcasts are recorded in
`vs_audit` (section 10), never as their own tables.

## 5. Who is current, on every request

Nothing is stamped into tokens. Every read and write re-derives access:

- **Current staff**: an active `User` of the tenant with a `StaffProfile`
  whose `is_on_roll` is true. Off-roll or deactivated staff lose every
  conversation at once.
- **A guardian's access to a FAMILY conversation**: `Guardian.user` is the
  caller at this tenant, the `StudentGuardian` link between that guardian and
  the conversation's student still exists, and the student's status is in
  `PORTAL_STATUSES`. Unlinking, revoking the guardian's account, or the child
  leaving the roll ends it on the next request.
- **A pupil's access to a PUPIL conversation**: `Student.user` is the caller
  and the status is in `PORTAL_STATUSES`.
- **A primary guardian's read-only view of a PUPIL conversation**: a
  `StudentGuardian` with `is_primary=True` links the caller's guardian record
  to the student, student in `PORTAL_STATUSES`. Recomputed every request, so a
  change of primary guardian moves the view at once.
- **Account deactivated** (`is_active=False`): refused by
  `IsAuthenticatedAndActive` before anything else, and excluded from contact
  lists and alerts.

## 6. The visibility service (`services/visibility.py`), the only gate

Every route, the unread count, search, the export and the alert recipients go
through these functions. No view filters conversations itself.

- `staff_conversations(tenant, user)` → open participations of current staff,
  every kind except the portal-only views.
- `guardian_conversations(tenant, user, *, child=None)` → FAMILY
  conversations the caller takes part in as a still-linked guardian, plus PUPIL
  conversations of children whose primary guardian the caller is (marked
  read-only).
- `pupil_conversations(tenant, user)` → PUPIL conversations of the linked
  pupil.
- `resolve(tenant, user, conversation_id, *, surface)` where surface is
  `staff`, `parent`, `student`, or `inspect_group` / `inspect_broadcast` /
  `inspect_welfare`. Returns an `Access(conversation, mode, participant,
  after_message_id, can_send, read_only, reason)`; anything else raises the
  module's one `ConversationNotFound` (404, `CONVERSATION_NOT_FOUND`). A
  conversation at another school, one that does not exist, and one the caller
  may not reach answer the same 404 with the same body.
- `visible_messages(access)` → `Message` queryset: `id > after_message_id` for
  a participant; every message for an inspector and for the primary guardian.
  A broadcast inspection sees the broadcast's own messages only; replies live
  in their own `BROADCAST_REPLY` conversations, which no inspection surface
  resolves.
- `unread_counts(tenant, user, *, surface)` → one aggregate query: messages in
  the caller's visible participations with `id > greatest(last_read, after)`
  and a sender other than the caller.
- `alert_recipients(message)` → open participants other than the sender who
  pass section 5 now, and who had nothing unread in this conversation before
  this message (one alert per conversation until it is read).

`can_send` is true for a current participant, except: a broadcast recipient
(they reply privately instead), a `ONE_WAY` broadcast's reply route, a family
or pupil conversation whose staff participant is no longer current staff (the
family sees it read-only with reason "no longer at the school"), a family
conversation when `messages.families_enabled` is off, a pupil conversation when
`messages.pupils_enabled` is off, and a staff caller without
`school.messages.send` (or `school.family_messages.send` on family and pupil
conversations). Inspectors and the primary guardian view are always read-only.

## 7. Permissions

Verb `inspect` is added to `seed_actions` ("Read a conversation you take no
part in, read-only, with every opening recorded"). All keys are in module
`school`, core band (no capability), and must stay in lockstep with
school-fe `src/permissions/index.ts`.

| key | sensitivity | default roles | what it opens |
| --- | --- | --- | --- |
| `school.messages.view` | NORMAL | school_admin, branch_admin, teacher, finance_admin | the staff inbox: conversations the holder takes part in |
| `school.messages.send` | NORMAL | same four | start a staff direct conversation, write in conversations the holder takes part in, reply privately to a broadcast |
| `school.messages.export` | SENSITIVE | school_admin | download a conversation the holder takes part in |
| `school.family_messages.send` | NORMAL | school_admin, branch_admin, teacher | the staff end of family and pupil conversations |
| `school.family_messages.office` | NORMAL | school_admin, branch_admin | be the "school office" contact for families at the branches in reach |
| `school.message_groups.create` | NORMAL | school_admin, branch_admin | create a staff group (the creator becomes its owner) |
| `school.message_broadcasts.send` | NORMAL | school_admin, branch_admin | send a broadcast to staff |
| `school.message_groups.inspect` | SENSITIVE | school_admin | read any group in reach, read-only, audited |
| `school.message_broadcasts.inspect` | SENSITIVE | school_admin | read any broadcast in reach, read-only, audited, never its replies |
| `school.message_welfare.inspect` | SENSITIVE | school_admin | read any pupil conversation in reach, read-only, audited |

The reserved `communication.*` keys in `vs_notifications/constants.py` govern
notification templates and delivery, not conversations, and gate nothing here.

**Branch reach** uses `vs_rbac.scoping.visible_branch_ids`:
- Staff contacts: current staff holding `school.messages.view` whose
  `StaffProfile.branch` is in the caller's reach, or is null (inclusive read).
  A whole-tenant reach sees everyone. Replying inside an existing conversation
  needs no reach.
- Group members and broadcast recipients: the same set, narrowed to the
  group's or broadcast's branch when one is set. A school-wide (null branch)
  group or broadcast needs a whole-tenant reach to create.
- Inspection: the conversation's branch (the child's current branch for a pupil
  conversation) must be in the inspector's reach; a null-branch group or
  broadcast needs a whole-tenant reach.
- Holders of keys are found in bulk (one query for the set), never one
  permission check per candidate.

## 8. School settings (`vs_config` catalogue, whole tenant, BOOLEAN)

| key | default | meaning |
| --- | --- | --- |
| `messages.families_enabled` | True | Parents may message staff about their children. Off: existing conversations stay readable, nobody starts or writes in one |
| `messages.pupils_enabled` | True | Pupils may message staff. Off: as above for pupil conversations |
| `messages.family_reach_subject_teachers` | True | Families may reach the teachers of the child's class, not only the class teacher and the school office |
| `messages.read_receipts` | True | People see whether their messages have been read. Off: no receipt is computed or served to anybody (section 14.7) |

## 9. API

Every success uses `success_response`; lists use `XVSPagination`
(`count`, `next`, `previous`, `results`) except message lists, which are a
cursor (below). An empty list is `[]`. Errors use the module's exception
classes with `error_code`.

### 9.1 Conversation row (inbox list)

```json
{
  "id": 41, "kind": "FAMILY", "kind_label": "Parent",
  "title": "",
  "display_name": "Mrs Funke Bello",
  "counterpart": {"user_id": 88, "name": "Mrs Funke Bello", "label": "Mother of Tunde Bello"},
  "child": {"id": 12, "name": "Tunde Bello", "class_name": "JSS1 A"},
  "member_count": 2,
  "last_message": {"id": 903, "preview": "Good morning, Tunde will...", "sender_name": "Mrs Funke Bello", "sent_at": "...", "mine": false},
  "last_activity_at": "...",
  "unread_count": 2,
  "reply_mode": "",
  "read_only": false, "can_send": true, "read_only_reason": "",
  "role": "STAFF",
  "branch": {"id": 3, "name": "Ikeja Branch"}
}
```

- `display_name`: the group name or broadcast title, else the counterpart's
  name. `counterpart` is null for groups and broadcasts.
- `preview` is the first 120 characters of the last message the caller may
  see, null when none (a new member of a busy group sees null until someone
  speaks).
- `branch` appears only at a school with more than one branch, and only for
  groups and broadcasts.

### 9.2 Staff routes, `/v1/messages/`

| route | key | notes |
| --- | --- | --- |
| `GET conversations/?kind=&unread=true&search=&page=` | messages.view | inbox, newest activity first. `kind` filters: `direct` (STAFF_DIRECT), `family` (FAMILY + PUPIL), `group`, `broadcast` (BROADCAST + BROADCAST_REPLY). `search` matches display name, child name and the bodies of messages the caller may see |
| `POST conversations/` | messages.send (STAFF_DIRECT) or family_messages.send (FAMILY, PUPIL) | `{"kind": "STAFF_DIRECT", "recipient": <user id>, "body", "client_token"}`, `{"kind": "FAMILY", "student": <id>, "guardian": <id>, ...}`, `{"kind": "PUPIL", "student": <id>, ...}`. Find-or-create on `pair_key`, then sends `body` when given. 201 new, 200 existing |
| `GET conversations/<id>/` | messages.view | the row plus `participants` (current members: `user_id`, `name`, `label`, `role`) for direct and group; for a broadcast the sender sees `recipients` and everyone sees `recipient_count` |
| `GET conversations/<id>/messages/?before=<id>&limit=` | messages.view | newest first, `limit` 1 to 100 (default 50): `{"results": [...], "has_more": bool, "next_before": <id or null>}`. A message: `id`, `body`, `sender` (`user_id`, `name`), `sent_at`, `mine` |
| `POST conversations/<id>/messages/` | messages.send or family_messages.send | `{"body", "client_token"}`. 201, or 200 with the stored message on a repeated token. 409 `CONVERSATION_READ_ONLY` with the reason when `can_send` is false |
| `POST conversations/<id>/read/` | messages.view | `{"up_to": <message id>}` optional, default the newest visible. Moves the watermark forward only, clears the bell entries for the conversation, returns `unread_count` |
| `GET conversations/<id>/export/` | messages.export | CSV of the messages the caller may see: `sent_at`, `sender`, `message`; columns follow Field Access; audited |
| `GET unread-count/` | messages.view | `{"total": n, "direct": n, "family": n, "group": n, "broadcast": n}` |
| `GET contacts/?kind=staff&search=&branch=` | messages.send | staff the caller may start a conversation with (or add to a group, or address in a broadcast): `user_id`, `name`, `label` (job title or role), `branch` (multi-branch only). Paginated |
| `GET contacts/?kind=family&search=` | family_messages.send | pupils the caller may reach with their guardians: `student` (`id`, `name`, `class_name`), `guardians` (`id`, `name`, `relationship`, `has_account`), `pupil_has_account`. Paginated |
| `POST groups/` | message_groups.create | `{"name", "branch": <id or null>, "members": [user ids], "body"?, "client_token"?}`. Creator becomes OWNER. Members must be staff contacts in the group's branch |
| `PATCH groups/<id>/` | owner | `{"name"}` |
| `POST groups/<id>/members/` | owner | `{"members": [user ids]}`; each new member's `after_message_id` is the group's last message id |
| `DELETE groups/<id>/members/<user_id>/` | owner, or the member themself (leave) | closes the period; the last owner cannot leave while other members remain (409 `GROUP_NEEDS_OWNER`) |
| `POST groups/<id>/members/<user_id>/owner/` | owner | makes a current member an owner |
| `POST broadcasts/` | message_broadcasts.send | `{"title", "body", "reply_mode": "ONE_WAY" or "PRIVATE_REPLIES", "branch": <id or null>, "recipients": [user ids], "client_token"}`. 1 to 1,000 recipients, staff contacts only, never the sender |
| `POST broadcasts/<id>/reply/` | messages.send | a recipient's private reply: finds or creates their `BROADCAST_REPLY` conversation with the sender, sends `body`, returns `{"conversation": <row>, "message": <message>}`. 409 `BROADCAST_ONE_WAY` on a one-way broadcast |

### 9.3 Oversight routes, `/v1/messages/oversight/`

| route | key |
| --- | --- |
| `GET groups/?search=` | message_groups.inspect |
| `GET broadcasts/?search=` | message_broadcasts.inspect |
| `GET pupil-conversations/?search=` | message_welfare.inspect |
| `GET conversations/<id>/` and `GET conversations/<id>/messages/?before=` | the key for that conversation's kind |

List rows carry no message text: `id`, `kind`, `display_name`, `child` (pupil
conversations), `participants` names, `member_count` / `recipient_count`,
`branch` (multi-branch only), `last_activity_at`, `message_count`. Detail and
messages carry `read_only: true` and `inspection: true`. **Every** call to the
detail or messages route writes one audit event (`CONVERSATION_INSPECTED`,
metadata: `kind`, `surface`), whether or not the inspector also takes part.
An inspection never moves a read watermark and never clears a bell entry. A
`BROADCAST_REPLY`, `STAFF_DIRECT` or `FAMILY` conversation never resolves here.

### 9.4 Parent routes, `/v1/portal/parent/messages/`

Admitted by the parent portal's own `PORTAL_PERMISSIONS`; no keys.

| route | notes |
| --- | --- |
| `GET conversations/?child=&search=` | the parent's family conversations and, read-only, the pupil conversations of children they are primary guardian of. Same row shape; `counterpart.label` is the staff label ("Class teacher, JSS1 A", "Mathematics", "School office"); pupil rows have `read_only: true`, `read_only_reason: "Tunde's conversation. You can read it but not reply."` |
| `GET conversations/<id>/`, `GET conversations/<id>/messages/?before=` | |
| `POST conversations/<id>/messages/`, `POST conversations/<id>/read/` | own family conversations only |
| `GET contacts/?child=<id>` | the staff the parent may reach about that child, with labels. A child that is not theirs: the portal's 404 |
| `POST conversations/` | `{"child", "staff": <user id>, "body", "client_token"}`; find-or-create |
| `GET unread-count/` | `{"total": n}` (own family conversations only) |

### 9.5 Student routes, `/v1/portal/student/messages/`

Same as the parent routes without `child`. Contacts are the pupil's own
teachers and school office. Every response carries
`"oversight_notice": "Your parent or guardian and the school can read your messages."`
on the list, so the app can say it plainly.

## 10. Audit

`vs_audit` module key `MESSAGES`. Action types: `CONVERSATION_INSPECTED`,
`CONVERSATION_EXPORTED`, `MESSAGE_GROUP_CREATED`, `MESSAGE_GROUP_RENAMED`,
`MESSAGE_GROUP_MEMBERS_CHANGED`, `MESSAGE_BROADCAST_SENT`. Summaries name who
did what to which conversation (`entity_type="conversation"`, id, the display
name as the label) and never carry a message body. Individual messages are not
audited: they are the record.

## 11. Alerts (Module 8 delivers, never stores the conversation)

Each send commits, then `transaction.on_commit` queues
`vs_messages.dispatch_message_alert(message_id)`. The task computes
`alert_recipients`, groups them, and calls `send_notification` per group in a
`try/except` that logs: a failure loses no message and does not stop other
groups. The saved message and the 201 never depend on the task.

| event | to | channels | in-app / push subject | body |
| --- | --- | --- | --- | --- |
| `messages.new_message` | staff | in-app, email, push | `New message from {{ sender_label }}` (direct) / `New message in {{ conversation_label }}` (group) | `Open Messages to read it.` |
| `messages.new_broadcast` | staff | in-app, email, push | `{{ sender_label }} sent a broadcast` | `Open Messages to read it.` |
| `messages.new_message_parent` | parent | in-app, email, push | `New message from {{ sender_label }} about {{ child_first_name }}` | `Open the parent app to read it.` |
| `messages.new_message_pupil` | pupil | in-app, email, push | `New message from {{ sender_label }}` | `Open the app to read it.` |

- **No body, ever,** in any channel or stored notification row. Email says the
  same and carries a link (`{{ conversation_url }}`).
- `sender_label` is the sender's name when the recipient may read Participant
  names (Field Access, evaluated per staff recipient and grouped), else
  "a colleague". `conversation_label` is the group name when they may read the
  name switch, else "a staff group". Portal recipients hold no role and always
  get names.
- Record family `CONVERSATION`, metadata `conversation_id`. Routes: staff
  `/messages/{id}` (school-fe), parent `/messages/{id}` (parent app router),
  pupil `/messages/{id}` (student app router). Push destinations open
  `/parent/messages/<id>` and `/student/messages/<id>`. Opening the
  conversation (`read/`) clears its bell entries.

## 12. Field Access

Resource `school.messages`, every field open by default, TENANT scope.

| field | label | RW | api names |
| --- | --- | --- | --- |
| `body` | Message | RW | `body`, `preview`, `last_message.preview`, the export's `message` column, body search |
| `title` | Group or broadcast name | RW | `title`, `name`, `display_name` when it is a title, alert `conversation_label` |
| `participants` | People in the conversation | RW | `counterpart`, `participants`, `sender`, `sender_name`, `members`, `display_name` when it is a person, the export's `sender` column, alert `sender_label`, name search, group `members` on write, `recipient` on write |
| `recipients` | Broadcast recipients | RW | `recipients`, `recipient_count`, `recipients` on write |
| `child` | Pupil the conversation is about | RW | `child`, `student` on write, child-name search |
| `reply_mode` | Broadcast reply setting | RW | `reply_mode` |
| `sent_at` | Sent times | R | `sent_at`, `last_activity_at`, `last_message.sent_at`, the export's `sent_at` column |
| `read_status` | Read status and unread counts | R | `unread_count`, `unread-count/` totals |
| `read_receipts` | Read receipts | R | `receipt` on a message, `receipts` on a conversation detail, the `receipts` list of a polling answer |

- **Open on create:** `title` and `participants` (whoever may create a group
  names it and chooses its first members; the Write switch decides who may
  rename it or change its members afterwards). `body`, `recipients`, `child`
  and `reply_mode` are not: Write off refuses sending a message, addressing a
  broadcast, naming a child on a new family or pupil conversation, and setting
  the reply mode (a broadcast sent without `reply_mode` is `ONE_WAY`).
- **No switch, recorded:** `kind` / `kind_label` (what sort of conversation:
  identifier), `branch` (owner decision of 12 October 2026: no branch
  switches), ids, `mine`, `can_send`, `read_only`, `role`, `member_count`
  (count of children), cursor values.
- A hidden field leaves every payload completely, including nested and
  alternate names; a hidden `body` drops body matches from search; Write off
  refuses the write (`FieldWriteDenied`) on every create and alternate path.
- Portals: parents and pupils hold no role, so switches do not reach them (as
  for notices); their payloads are a fixed narrow shape.

## 13. Not in this release

Attachments, edit and delete of a message, reactions, typing and online
presence, scheduled sending, guardian or pupil groups, inspection of
guardian-to-staff conversations, SMS (never).

## 14. As built

The staff half of the module (`schools.vs_messages`, slice A) and its second
half (slice B: the parent and student routes, the alert delivery, the CSV export
and the development seed scenarios) are built. Where this section and an earlier
one disagree, this section is right.

### 14.1 Shapes that differ from the text above

- **Paging envelope.** Lists use `core.pagination.XVSPagination` as it is:
  `{"success", "message", "pagination": {"currentPage", "pageSize",
  "totalItems", "totalPages", "next", "previous"}, "data": [...]}`, not
  `count/next/previous/results`. An empty list is `"data": []`.
- **What a start returns.** `POST conversations/` answers
  `{"conversation": <detail row>, "message": <message or null>}` with 201 for a
  new conversation and 200 for an existing one; `POST broadcasts/<id>/reply/`
  answers the same shape. `POST groups/` and `POST broadcasts/` answer the
  detail row. Group and broadcast creation are idempotent on the
  `client_token` of their first message.
- **Row keys.** `branch` is present only at a school with more than one branch
  and only on a group or broadcast row (null when school-wide). `member_count`
  is null on a broadcast row, because the count of a broadcast's people is
  `recipient_count`, which sits behind the Broadcast recipients switch and is
  carried by the detail only.
- **Group routes and keys.** `PATCH groups/<id>/`, `POST groups/<id>/members/`
  and `POST groups/<id>/members/<user_id>/owner/` need `school.messages.send`;
  `DELETE groups/<id>/members/<user_id>/` needs `school.messages.view` so a
  member can always leave. The owner check is in the service after the key.
- **Contacts and Field Access.** The staff contact list needs the People switch
  readable (403 `field_read_denied` otherwise). The family contact list carries
  `student` under the Child switch and `guardians` under the People switch, and
  is refused when neither is readable.
- **`unread-count/`** answers 403 `field_read_denied` when Read status is
  hidden, because the whole response is that field.
- **Oversight list rows.** `participants` names the people who run the
  conversation (a group's owners, a broadcast's sender, a pupil conversation's
  pupil and member of staff) so a list of large groups stays cheap; the full
  member list is on the detail as `members`.

### 14.2 Field Access registration

- `display_name` is not a registered name. Its switch depends on the kind (the
  Group or broadcast name for a group or broadcast, People in the conversation
  for a person), so the serializers decide it in code against the right
  switch.
- The nested names are real keys of their own serializer so the registry can
  prove each reaches a client: `preview`, `sender_name` and `sent_at` of
  `last_message` are the fields of `LastMessageSerializer`, which is a declared
  surface.
- `guardian` (the guardian chosen when staff start a family conversation) is
  registered under People in the conversation, which is also open on create.
- A label never carries a hidden name: a guardian's label reads "Mother of
  Tunde Bello" only while the Child switch is readable, else "Mother".
- Leaving a group is not a change of members the Write switch can refuse;
  removing another member, adding members and making an owner are.

### 14.3 Rules the build settled

- **Who may take part.** Only current staff (an active account with a
  `StaffProfile` on the roll) hold a staff participation, so an administrator
  without a staff record can inspect (with the inspection key and reach) but
  has no inbox. Staff reach is judged by `StaffProfile.branch` (null is
  school-wide); additional postings are not consulted.
- **An empty pair.** A direct, family, pupil or reply conversation started
  without a first message is listed for the person who started it only, until
  a message they can read exists. A group or broadcast is listed as soon as the
  person is in it.
- **Writing.** The first message of a new group or broadcast is stored for the
  creator whatever the send keys say (the creating key governs it) and is
  subject to the Message Write switch. A broadcast's sender may follow up in
  it when they hold `school.messages.send`. A family or pupil conversation
  whose family end can no longer be reached (link removed, child off the roll,
  account closed) is read-only for staff with the reason "This family can no
  longer be reached here."; one whose staff end left is read-only for the
  family with "No longer at the school.".
- **Starting about a child.** The guardian or pupil addressed must have an
  active account, the child must be on the roll and inside the caller's reach,
  and a switched-off family or pupil setting refuses a start with 409
  `CONVERSATION_READ_ONLY`.
- **Broadcast recipients.** The sender's own id is dropped silently; one to
  1,000 other recipients are required. A new group takes 0 to 1,000 members.
- **Verbs.** Besides `inspect`, the action `office` is added to
  `seed_actions`, because `school.family_messages.office` needs a verb of that
  name.
- **Audit.** `CONVERSATION_INSPECTED` carries `kind`, `surface` and `what`
  (`detail` or `messages`); no event carries a message body.

### 14.4 Slice B: portals, alerts, export, seed

**Portal routes.** Parent: `/v1/portal/parent/messages/` and student:
`/v1/portal/student/messages/`, both with `?tenant=<slug>`, mounted by one
`include` line in each portal's `urls.py` (`portal_urls_parent.py`,
`portal_urls_student.py`, views in `portal_views.py`). Admission is each
portal's own `PORTAL_PERMISSIONS`; a child that is not the parent's is the
portal's own 404 (`child_of`).

| route | parent | student |
| --- | --- | --- |
| `GET conversations/` | `?child=&search=&unread=true&page=`; page envelope plus top-level `unread_count` | `?search=&unread=true&page=`; plus `unread_count` and `oversight_notice` |
| `POST conversations/` | `{child, staff, body?, client_token?}` | `{staff, body?, client_token?}` |
| `GET conversations/<id>/` | one row | one row |
| `GET, POST conversations/<id>/messages/` | cursor page / send | same |
| `POST conversations/<id>/read/` | `{conversation, unread_count}` | same |
| `GET unread-count/` | `{total}` | `{total}` |
| `GET contacts/` | `?child=<id>` (required) | none |

- **Row** (both): `id`, `kind` (`FAMILY` or `PUPIL`), `kind_label`,
  `display_name`, `counterpart {user_id, name, label}`, `last_message {id,
  preview, sender_name, sent_at, mine}` or null, `last_activity_at`,
  `unread_count`, `read_only`, `can_send`, `read_only_reason`; a parent's row
  also has `child {id, name, class_name}`. Nothing else: no title, role, branch,
  member count, reply setting or guardian contact. A pupil conversation a
  primary guardian only watches is a `PUPIL` row with `read_only: true`, the
  reason "<First name>'s conversation. You can read it but not reply.", and
  `unread_count: 0`. `counterpart.label` follows the child ("Class teacher,
  JSS1 B", the subjects taught, "School office"), worked out per child on the
  page.
- **Message**: `id`, `body`, `sender {user_id, name}`, `sent_at`, `mine`.
- **Contacts**: a page of `{user_id, name, label}` (class teacher first, school
  office last) plus top-level `can_start` and `can_start_reason` (false and the
  sentence when the school switched that messaging off); the student page also
  carries `oversight_notice`.
- **Start** answers `{conversation, message}` with 201 new, 200 existing. A
  switched-off setting refuses with 409 `CONVERSATION_READ_ONLY` and the
  sentence "Messaging with parents is switched off at this school." (pupils:
  "...with pupils..."). A staff member the family may not reach, and one that
  does not exist, answer the same 400 `MESSAGE_RECIPIENT_UNAVAILABLE`.
- **Watching.** `POST .../messages/` on a watched conversation answers 409
  `CONVERSATION_READ_ONLY`; `read/` on it returns `unread_count: 0` and changes
  nothing (no participant row, no watermark, no bell entry).
- **Deviation: portal serializers do not carry the Field Access mixin.** A
  teacher who is also a parent signs in to the parent app with the same account,
  so a switch her staff role has closed would otherwise blank her own child's
  conversation. `portal_serializers.py` is a fixed narrow shape; its input
  serializers are on the write-path allowlist in `test_field_registry.py` with
  that reason.
- A pupil's rows carry no `child` key (every conversation is about them).

**Alerts.** `tasks.queue_message_alert` registers `transaction.on_commit` and
queues the Celery task `vs_messages.dispatch_message_alert(message_id)`;
`tasks.alerts_suppressed()` skips queueing (the seeder uses it). Every failure
(broker down, one batch refused, a vanished message) is logged and swallowed.
Recipients are `visibility.alert_recipients(message)`, which now also leaves
out a participant who joined after the message and one who has already read it,
so a late worker never alerts about what a person cannot see or has seen.

| event | to | wording |
| --- | --- | --- |
| `messages.new_message` | staff | `New message from <sender>` (direct, family, pupil, reply) or `New message in <group or broadcast title>` |
| `messages.new_broadcast` | staff | `<Sender> sent a broadcast` (the broadcast's first message only) |
| `messages.new_message_parent` | guardian | `New message from <staff name> about <child first name>` |
| `messages.new_message_pupil` | pupil | `New message from <staff name>` |

- A staff recipient is judged on their own Field Access
  (`get_field_access(user, tenant)`), and recipients who read the same wording
  share one `send_notification` call (batches of 200). Hidden People gives "a
  colleague", or "a parent" / "a pupil" when the writer is the family end; a
  hidden Group or broadcast name gives "a staff group" / "a broadcast"; the
  broadcast subject is capitalised ("A colleague sent a broadcast").
- Bodies: in-app and push "Open Messages to read it." (staff), "Open the parent
  app to read it.", "Open the app to read it."; the email names the school and
  carries `conversation_url` as the button and as a plain line. The message text
  is never in any template, context or stored row; `metadata` is
  `{"conversation_id": id}` only.
- Links: staff `<school app>/messages/<id>`, parent `<school host>/parent/messages/<id>`,
  pupil `<school host>/student/messages/<id>` (`links.conversation_url`).
- **Routing.** `RecordFamily.CONVERSATION` with one `RecordDestination` for the
  prefix `messages.` and route `/messages/{id}`. Staff, parent and pupil routes
  are the same string, so one entry covers all four events and the reverse
  clear rule (`/messages/<id>` acknowledges every alert about that
  conversation). The student app's router (`basename="/student"`) follows
  `notices/:id`, i.e. an app-relative `messages/:id`, not the `/portal/...`
  form the school app uses for a pupil's notice, so the pupil route is
  `/messages/{id}`. `read/` on any surface calls
  `tasks.clear_message_alerts`, which is `acknowledge_record(user,
  family=RecordFamily.CONVERSATION, value=id)`.
- **Push.** `VsMessagesConfig.ready()` registers `push.destination`: the parent
  event opens `/parent/messages/<id>`, the pupil event `/student/messages/<id>`
  (the `PARENT_APP_PATH` and `STUDENT_APP_PATH` constants). A staff push keeps
  the default (the recipient's own app).

**Export.** `GET /v1/messages/conversations/<id>/export/` (key
`school.messages.export`): the caller must take part (`resolve(surface=staff)`,
else the same 404 as any other missing id). The file is
`conversation-<id>.csv` with columns `sent_at`, `sender`, `message`, each present
only when the caller may read Sent times, People in the conversation and Message
respectively; none readable is 403 `field_read_denied`. Messages are
`visible_messages(access)` oldest first, so a later-joined member's file starts
at their join. Dates are written with `vs_config.display.format_datetime` in the
school's format. Cells beginning with `=`, `+`, `-`, `@`, tab or carriage
return get a leading apostrophe (there is no shared helper; `services/export.py`
holds the rule). One `CONVERSATION_EXPORTED` audit event is written with
`message_count` and the column names, never text.

**Seed.** `manage.py seed_messaging_scenarios` (`--only`, or `--school` with
`--like`) fills `lagoon-view` and `holy-cross` from the people already there,
idempotently, under `alerts_suppressed()`. A scenario with no people is said and
skipped (`holy-cross` has no parent or pupil accounts, so its family and pupil
scenarios are skipped).

### 14.5 Where the first slice plugs in

- Starting: `services.conversations.start_from_guardian(tenant, user, *,
  student_id, staff_user_id, body, client_token)` and
  `start_from_pupil(tenant, user, *, staff_user_id, body, client_token)`.
- Reading and writing: `services.visibility.guardian_conversations`,
  `pupil_conversations`, `resolve(..., surface="parent" | "student")`,
  `visible_messages(access)`, `unread_counts(..., surface=...)`;
  `services.messages.send_message(access, sender=, body=, client_token=)` and
  `mark_read(access, user, up_to=)`.
- Rows: `services.inbox.prepare(qs, user, surface=)`, `inbox.search`,
  `inbox.counterpart`; the portals render them with `portal_serializers`.
- Contacts for a child: `services.people.staff_reachable_for_child(tenant,
  student, settings)` returns `{user_id: label}`, and
  `people.staff_profiles_by_user` loads their names.

### 14.6 The send limit

Owner decision: **20 messages a minute per person.**

- **Rate.** `message_send` in `REST_FRAMEWORK["DEFAULT_THROTTLE_RATES"]`
  (`apps/settings/base.py`). The test and CI settings zero every rate, so a
  test that needs the limit patches `MessageSendThrottle.THROTTLE_RATES` and
  its `timer` (`tests/test_send_limit.py`).
- **What is counted.** A `POST` to a route that can store a message, one budget
  for all of them on every surface: staff `POST conversations/` (counted with
  or without a first message), `POST conversations/<id>/messages/`,
  `POST groups/`, `POST broadcasts/`, `POST broadcasts/<id>/reply/`, and the
  parent and student `POST conversations/` and
  `POST conversations/<id>/messages/`. The key is the school and the signed-in
  person, so nobody's sends use up anybody else's. A send refused for another
  reason (validation, a missing key) still counts, because the throttle runs
  before the view; a caller without the key for a route is refused 403 before
  the throttle is consulted.
- **What is never counted.** Every `GET` and every other method. The inbox, a
  thread and the unread badge are polled by every open screen, and
  `read/`, the contact lists and the group member routes do not carry the
  throttle at all.
- **How it is applied.** `throttles.SendLimited` is a mixin on the nine views
  that have such a route. It adds `MessageSendThrottle` beside
  `DEFAULT_THROTTLE_CLASSES` and turns a refusal into `MessageRateLimited`.
- **The refusal.** 429, `error.code` `MESSAGE_RATE_LIMITED`, `message` "You are
  sending messages too quickly. Wait a moment and try again.", and
  `error.detail.retry_after` in whole seconds. No other route's 429 changes.
  The client keeps the draft and its `client_token`, shows the sentence and
  lets the next tap send.

### 14.7 Read receipts

Owner decisions (22 October 2026): two ticks in a group or broadcast only when
everyone who could see the message has read it, with "Read by n of m" for the
sender; a whole-school setting, default on; a read-only Field Access switch; no
receipts for a watching parent or an inspector. No model change: a receipt is
computed from the read watermarks that already exist.

- **Setting.** `messages.read_receipts` (BOOLEAN, default True, whole tenant),
  read with the other messaging settings (`MessagingSettings.read_receipts`).
  Off: no receipt is computed or served to anybody at that school.
- **The receipt.** On each message the caller sent:
  `"receipt": {"state": "sent" | "read", "read_count": n, "recipient_count": n}`.
  Messages from others carry no `receipt` key. `services/receipts.py` judges one
  conversation per call from one query: the open participants other than the
  sender, current by the rules of `services/visibility.py` (current staff for a
  staff role, the guardian of a family conversation only while the link to the
  child holds and the child is served by the portal, the pupil of a pupil
  conversation only while on the roll with a working account), whose
  `after_message_id` is below the message. A reader has read it when
  `last_read_message_id >= message id`. `read` when `read_count ==
  recipient_count` and `recipient_count > 0`, otherwise `sent`. A person who
  joined later, left, was removed, was deactivated or is off the roll is not
  counted. A direct kind has one reader; a group or broadcast counts everyone.
  A broadcast's private reply is a separate `BROADCAST_REPLY` conversation with
  its own direct receipts. A conversation with no current reader answers
  `sent` with `0` of `0`.
- **Where it travels.** The staff message list, the send response, the start
  and the broadcast-reply responses (`message.receipt`), and the same on the
  parent and student message lists, sends and starts. Never in an export, an
  alert or an audit event.
- **No receipt at all (the key is absent)** when the setting is off; the caller
  is inspecting (the oversight routes pass no receipt source); the caller is a
  parent watching a pupil conversation; or, on the staff surface, the caller's
  role cannot read the Read receipts field.
- **`receipts` on a conversation detail** (staff and portal) is a boolean the
  apps use to decide whether to draw ticks. It is `false` when the setting is
  off, and for a parent watching a pupil conversation; on the staff surface it
  is absent, like the receipts, when the role cannot read the field. List rows
  do not carry it. The portal flag is on the detail serializer only, which is
  also what start responses return.
- **Polling.** `GET conversations/<id>/messages/?after=<message id>` on the
  staff route and both portal routes answers `{"results": [messages with an id
  greater than after, oldest first, at most 100], "has_more": bool, "receipts":
  [{"id", "state", "read_count", "recipient_count"}, ...]}`. `receipts` covers
  the caller's own messages among the latest 100 visible ones and among the
  results, ordered by id, and is left out whole (not served empty) under the
  same conditions as the message `receipt`. The same visibility gate applies,
  so a late member never receives what came before they joined, and anything
  unreachable is the same 404. A GET never counts against the send limit
  (section 14.6). Without `after` the `before` cursor behaves as before. The
  oversight route ignores `after`.
- **Field Access.** Resource `school.messages`, field `read_receipts` ("Read
  receipts", group Status, tenant scope, read-only, open by default). Its
  `api_names` are `receipt` and `receipts`; the counts inside a receipt travel
  with it and register no name of their own (`recipient_count` already belongs
  to Broadcast recipients, and the registry requires every registered name to be
  a serializer key). The polling route and the send, start and reply responses
  check the switch with `can_read`. The portals hold no role, so the switch does
  not reach them (14.2).
- **Cost.** One query for the conversation's participants per page or poll,
  compared in memory; a polling call adds one more for the latest 100 ids.
  A page costs the same for a direct conversation and a group of forty.


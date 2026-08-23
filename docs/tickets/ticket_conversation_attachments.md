# ticket_conversation_attachments

The thread on a ticket: public replies, support-only internal notes, uploaded
files, the download route that serves them back, and the per-ticket audit trail
that records all of it. The ticket itself is `ticket_lifecycle`; who may see any
of this is `ticket_visibility_permissions`.

Routes: `/v1/support/tickets/<pk>/comments/`, `.../attachments/`,
`.../attachments/<id>/download/`, `.../audit/`.

---

## 1. What it is (and what it is NOT)

- **Two audiences share one table.** `TicketComment.visibility` is `PUBLIC` or
  `INTERNAL` (`constants.py:53`). A public reply is the conversation with the
  person who raised the ticket; an internal note is CX talking to CX on the same
  thread. There is no third state and no per-person addressing.
- **Nothing in the thread can be edited or deleted.** There is no `PATCH`, no
  `DELETE`, and no view for a single comment or attachment other than the file
  download. `updated_at` is serialized on every comment
  (`serializers.py:55`) and can never differ from `created_at`.
- **Attachments are not a document library.** They exist only in the context of
  a ticket, have no title, no version, no folder, and no listing endpoint of
  their own - they arrive embedded in the ticket detail payload
  (`serializers.py:108-113`) and in each comment.
- **The audit route is the ticket's own history, not `vs_audit`.** It reads
  `TicketAuditLog` rows written by this module (`services/audit.py:19-27`). The
  platform stream gets a mirrored copy, but the two are separate reads with
  separate permissions.
- **Internal notes are hidden, not redacted.** A caller who cannot see them gets
  a shorter list, with no marker that anything was removed - and the ticket's
  `comments_count` is annotated to match, so the count does not betray them
  either (`views.py:73-88`).
- **None of these lists is paginated.** Comments, attachments and audit rows all
  return in full, however many there are (§8).

## 2. Domain model

### `TicketComment` (`models.py:161`)

| Field | Notes |
|---|---|
| `ticket` | `CASCADE`, `related_name="comments"` |
| `author` | `PROTECT` - a user who has ever replied cannot be deleted |
| `body` | `TextField`, no ceiling |
| `visibility` | `PUBLIC` / `INTERNAL`, `db_index=True` |

Ordering is `created_at` ascending (oldest first - it is a conversation).
Indexes: `(ticket, visibility)` and `(author, created_at)`. `is_internal` is a
convenience property (`models.py:184-186`).

### `TicketAttachment` (`models.py:192`)

| Field | Notes |
|---|---|
| `ticket` | `CASCADE` |
| `comment` | `CASCADE`, nullable - null means "attached to the ticket, not to a reply" |
| `uploaded_by` | `PROTECT` |
| `file` | `FileField`, `upload_to=ticket_attachment_upload_to` → `ticket-attachments/<ticket_number>/<filename>` (`models.py:22-24`) |
| `original_filename` (255), `content_type` (120), `size` | Display name, verified type, byte count |

Ordering is `-created_at` (newest first). Indexes: `(ticket, created_at)`,
`(uploaded_by, created_at)`.

Storage is `core.storage.DatabaseStorage` - the bytes live in a `StoredFile` row,
not on a disk, and the stored path is given a high-entropy suffix by
`get_available_name`, so the predictable `ticket-attachments/<number>/` prefix is
not itself a guessable URL.

### `TicketAuditLog` (`models.py:223`)

| Field | Notes |
|---|---|
| `ticket` | `CASCADE` |
| `actor` | `SET_NULL` - the history outlives the account |
| `action` | `TicketAuditAction` (`constants.py:59`), `db_index=True` |
| `summary` | A rendered sentence, written at record time |
| `before_data`, `after_data`, `metadata` | Raw `JSONField`s |
| `created_at` | `default=timezone.now`, `editable=False`, indexed |

Not a `TimeStampedModel`: there is no `updated_at`, because an audit row is
never updated.

## 3. Endpoint map

All four are detail routes on `TicketViewSet` and therefore run `get_object()`
first (`views.py:139-149`), which resolves the ticket from `all_objects` and
raises `NotFound` if `can_view_ticket` says no. **A caller who cannot see the
ticket gets a `404` from every route below**, whatever key they hold.

| Method + path | Key | Body | Response |
|---|---|---|---|
| `GET /tickets/<pk>/comments/` | none | - | `TicketCommentSerializer[]`, oldest first |
| `POST /tickets/<pk>/comments/` | none | `{"body": "...", "visibility": "PUBLIC"\|"INTERNAL"}` | `201` + the new comment |
| `POST /tickets/<pk>/attachments/` | none | multipart `file`, optional `comment_id` | `201` + `TicketAttachmentSerializer` |
| `GET /tickets/<pk>/attachments/<id>/download/` | none | - | the bytes, or `404` |
| `GET /tickets/<pk>/audit/` | `tickets.audit.view` | - | `TicketAuditLogSerializer[]`, newest first |

"none" means no RBAC key is declared, not that anyone may call it: the write
paths are gated in the service layer instead (§6), and the read paths by
`get_object`.

Serializer field sets:

| Serializer | Fields |
|---|---|
| `TicketCommentSerializer` (`serializers.py:49`) | `id`, `author`, `body`, `visibility`, `attachments`, `created_at`, `updated_at` |
| `TicketAttachmentSerializer` (`serializers.py:27`) | `id`, `original_filename`, `content_type`, `size`, `url`, `uploaded_by`, `comment_id`, `created_at` |
| `TicketAuditLogSerializer` (`serializers.py:230`) | `id`, `actor`, `action`, `summary`, `before_data`, `after_data`, `metadata`, `created_at` |

`url` is reversed to the download route rather than exposing a storage path
(`serializers.py:38-46`), and is `""` when the file row has no bytes.

## 4. Lifecycle / state machine

There is none. Comments and attachments are append-only:

```text
POST comments/     ──►  a row exists, forever, with the visibility chosen at write time
POST attachments/  ──►  a row exists, forever, pointing at bytes that are never replaced
```

The only thing that *changes* about an existing comment is who can see it - and
that changes with the reader, never with the row. A person who gains
`tickets.internal_note.post` sees every internal note ever written on every
ticket they can open, retroactively.

## 5. Derivations

- **Which comments come back** is decided twice, identically: in the list route
  (`views.py:230-232`) and in the ticket detail serializer
  (`serializers.py:102-106`). Both filter to `PUBLIC` unless
  `can_view_internal_notes` says otherwise.
- **Attachments inherit their comment's visibility.** In the detail serializer,
  attachments whose comment is internal are excluded
  (`serializers.py:108-113`); in the download route, they are a `404`
  (`views.py:285-291`). A file attached directly to the ticket (`comment_id`
  null) is always as visible as the ticket.
- **`_sees_internal` is computed once per detail render**
  (`serializers.py:96-100`) and reused for both comments and attachments, so a
  ticket page does not re-run the RBAC evaluation twice.
- **`capabilities`** on the detail payload (`serializers.py:115-122`) answers
  `can_comment` and `can_attach` for the current reader, so the frontend can
  disable a control instead of discovering a `403` on submit.
- **`content_type` is derived from the file's own bytes**, never from the
  multipart part's declared type (`services/tickets.py:211-217`,
  `core/uploads.py:131-137`). This is load-bearing: the download route serves
  the stored value back as the response `Content-Type`, with inline disposition
  for anything `image/*` (`views.py:296-304`). Trusting the client's claim once
  let a caller upload SVG markup named `.png` declared `image/svg+xml` and have
  it rendered - and executed - in the next reader's browser session.
- **Disposition follows type**: images render inline, everything else downloads
  (`views.py:302`). A missing stored type falls back to `mimetypes.guess_type`
  on the filename and then to `application/octet-stream`.
- **The audit action distinguishes note from reply**: `INTERNAL_NOTE_ADDED`
  versus `COMMENTED` (`services/tickets.py:184-188`), which is what makes
  "show me only the customer-visible history" a filter rather than a join.
- **The comment body is copied into the notification context**
  (`services/notifications.py:154-156`), so an emailed reply carries the text -
  including an internal note's text, to the assignee.

## 6. What writing writes

`add_comment` (`services/tickets.py:171`), in one transaction:

1. checks `can_comment_on_ticket`, and additionally `can_add_internal_note` when
   the visibility is `INTERNAL` (172-176);
2. creates the row;
3. records `COMMENTED` or `INTERNAL_NOTE_ADDED` with
   `metadata = {"comment_id", "visibility"}` - the body is **not** copied into
   the audit row;
4. fires `ticket.commented` (see `ticket_context_integrations` §4).

`add_attachment` (`services/tickets.py:202`), in one transaction:

1. checks `can_attach_to_ticket` (203-204);
2. refuses a comment belonging to another ticket (205-206) - though the view has
   already scoped the lookup to this ticket (`views.py:255-258`);
3. re-validates the upload and takes the verified content type (219-222);
4. creates the row with `original_filename` taken from the uploaded file;
5. records `ATTACHMENT_ADDED` with `metadata = {"attachment_id", "filename"}`;
6. fires `ticket.attachment_added`.

Reading writes nothing. Downloading a file - including a colleague's file on
another tenant's ticket, as CX support routinely does - leaves no record in
`TicketAuditLog` or in `vs_audit`.

## 7. Worked example

```text
POST /v1/support/tickets/4471/comments/?tenant=codex
{"body": "Reproduced on staging - the print handler 500s.", "visibility": "INTERNAL"}
```

```json
{ "success": true, "message": "Comment added successfully.",
  "data": { "id": 9912,
            "author": {"id": 22, "name": "Ada Support", "email": "ada@codex.test",
                       "tenant_kind": "PLATFORM", "role": "Support Engineer"},
            "body": "Reproduced on staging - the print handler 500s.",
            "visibility": "INTERNAL", "attachments": [],
            "created_at": "2026-08-21T09:14:02Z",
            "updated_at": "2026-08-21T09:14:02Z" } }
```

The requester then calls the same thread:

```text
GET /v1/support/tickets/4471/comments/?tenant=bright-star
```

```json
{ "success": true, "message": "Comments retrieved successfully.",
  "data": [ { "id": 9908, "body": "The print button does nothing.", "visibility": "PUBLIC", … } ] }
```

One row, not two, and nothing says a row was withheld. The ticket's
`comments_count` reads `1` for the same caller and `2` for Ada.

An empty thread returns `{"success": true, "message": "Comments retrieved
successfully.", "data": {}}` - an object, not an array, because
`success_response` coerces a falsy `data` to `{}` (`core/response.py:6-11`).
That shape is asserted at `tests.py:634-638`.

## 8. Gotchas / known limitations

Full evidence in **`error/tickets/ticket_code_issues.md`**. The items belonging
to this slice:

- **A requester can attach a file to an internal note.** The view resolves
  `comment_id` scoped to the ticket but never asks whether the caller may *see*
  that comment (`views.py:255-258`, `services/tickets.py:205-206`), so the
  customer can post into the support-only side channel and can probe which
  comment ids on their own ticket are internal (`ticket_code_issues.md` §5).
- **The sanitised filename is computed and thrown away.** `validate_upload`
  returns `(safe_name, content_type)` and the service keeps only the second
  (`services/tickets.py:219-222`), storing the raw upload name instead - so a
  name over 255 characters is a `500`, not a `400`
  (`ticket_code_issues.md` §11).
- **Comments, attachments and audit rows are unpaginated.** A long thread
  returns whole, three times over: the list route, the detail payload, and the
  audit trail (`ticket_code_issues.md` §8).
- **The detail payload re-queries per comment.** `get_comments` selects
  `author` but not `author__tenant`, while `TicketUserSerializer` reads
  `tenant.kind` - so an N-comment ticket costs N extra queries, and the same for
  attachments (`serializers.py:103,109`). The list route gets this right
  (`views.py:227-228`) (`ticket_code_issues.md` §13).
- **The audit trail returns raw `before_data`, `after_data` and `metadata`**
  (`serializers.py:235-238`), including impersonation metadata. It is behind a
  `SENSITIVE` key that no school role is seeded with, so the exposure is to
  platform staff - but nothing filters those blobs.
- **Reading is never audited.** Downloading an attachment writes nothing
  anywhere (`views.py:271-304`).
- **`updated_at` is serialized on comments and is always equal to
  `created_at`** - there is no edit path.
- **`validate_upload` runs twice per upload**, once in the serializer and once
  in the service (`serializers.py:215-227`, `services/tickets.py:219-222`), with
  different wording for the type error.
- **Justified by design:** a hidden ticket is a `404` from every thread route,
  never a `403` (`views.py:146-148`), so existence is not leaked.
- **Justified by design:** internal-note attachments 404 on download rather than
  403 (`views.py:285-291`), for the same reason.

## 9. Permissions & tenant isolation

| Surface | Gate | Seeded to |
|---|---|---|
| Read the thread | ticket visibility only | everyone who can open the ticket |
| Read internal notes | `tickets.internal_note.post`, or being the assignee, or being support (`services/visibility.py:169-177`) | platform roles only |
| Post a public reply | participant, or `tickets.comment.post` (`services/visibility.py:150-156`) | `school_admin`, `branch_admin`, `teacher` |
| Post an internal note | as for reading them | platform roles only |
| Attach a file | participant, or `tickets.attachment.create` (`services/visibility.py:160-165`) | `school_admin`, `branch_admin`, `teacher` |
| Download a file | ticket visibility, plus the note's visibility if it hangs off one | - |
| Read the audit trail | `tickets.audit.view` (`SENSITIVE`) | `xvs_super_admin`, `xvs_platform_admin` only |

**Seeing internal notes and writing them are the same permission**, by
construction: `can_view_internal_notes` simply calls `can_add_internal_note`
(`services/visibility.py:176-177`). That is a deliberate simplification, and it
means there is no read-only internal-note role.

**No school role can read the ticket audit trail.** `SCHOOL_ADMIN_EXTRA_KEYS`
(`management/commands/seed_ticket_permissions.py:23-27`) is `update`, `manage`
and `report.view`; `audit.view` is not in it, and
`test_requester_cannot_view_audit_trail` (`tests.py:535-538`) pins the refusal.

Tenant isolation is entirely inherited from the ticket: every route in this
slice begins with `get_object()`, so nothing here can reach a thread on a ticket
the caller could not open.

## 10. Code map

| File | Responsibility |
|---|---|
| `views.py:222-245` | `comments` - GET list with internal-note filtering, POST create |
| `views.py:248-269` | `attachments` - upload, optional comment binding |
| `views.py:271-304` | `attachment_download` - visibility, content type, disposition |
| `views.py:307-314` | `audit` - the per-ticket trail |
| `services/tickets.py:171-200` | `add_comment` |
| `services/tickets.py:202-238` | `add_attachment` |
| `services/visibility.py:150-184` | `can_comment_on_ticket`, `can_attach_to_ticket`, `can_add_internal_note`, `can_view_internal_notes`, `sees_internal_notes_by_default` |
| `serializers.py:27-56` | Attachment and comment output, including the reversed download URL |
| `serializers.py:88-122` | `TicketDetailSerializer` - embedded thread, files, capabilities |
| `serializers.py:206-227` | Comment and attachment input |
| `core/uploads.py` | `validate_upload`, `TICKET_EXTENSIONS`, `MAX_TICKET_ATTACHMENT_BYTES` (10 MB) |
| `core/storage.py` | `DatabaseStorage` - where the bytes actually live |

## 11. Test coverage & gaps

- `test_internal_notes_hidden_from_requester_but_visible_to_support`
  (`tests.py:412-428`) - the same thread read by both sides, asserted by body.
- `test_internal_note_attachment_hidden_from_requester` (`tests.py:540-560`) -
  the file on an internal note is absent from the requester's payload.
- `test_attachment_download_is_authenticated_and_ticket_scoped`
  (`tests.py:562-580`).
- `test_declared_content_type_cannot_decide_how_a_file_is_served`
  (`tests.py:582-613`) - the SVG-as-PNG case, pinned.
- `test_spreadsheet_and_csv_attachments_are_still_accepted`
  (`tests.py:615-632`).
- `test_empty_comment_list_shape` (`tests.py:634-638`) - the `[]` → `{}`
  coercion.
- `test_requester_cannot_view_audit_trail` (`tests.py:535-538`).
- `test_same_tenant_peer_cannot_list_or_open_another_users_ticket`
  (`tests.py:443-455`) includes the comments route in the `404` assertion.

What the suite does not cover:

1. **Attaching to a comment the caller cannot see** - the defect in §8's first
   item. Nothing today passes an internal note's id as `comment_id` from a
   requester.
2. **`tickets.comment.post` and `tickets.attachment.create` as the *only* route
   in** - every commenting test uses a participant, so the non-participant
   branches of `can_comment_on_ticket` and `can_attach_to_ticket` are unexercised.
3. **The audit route's happy path.** Only the `403` is asserted; nothing asserts
   what a holder actually receives, or that `before_data`/`after_data` say what
   they should.
4. **Oversized and malformed uploads** - the 10 MB refusal, the empty file, and
   a filename longer than 255 characters.
5. **Non-image disposition** - nothing asserts that a PDF downloads rather than
   renders inline.
6. **A file attached directly to the ticket** (`comment_id` null) being visible
   to the requester - only the internal-note case is tested.
7. **Ordering**: comments oldest-first, attachments and audit rows newest-first.

# core_file_storage

Where every uploaded byte on this platform lives, how it gets validated on the
way in, and how it is served back. Four pieces: the `StoredFile` table,
`DatabaseStorage` (wired as the default Django storage), `validate_upload` (the
shared first line of validation), and `MediaView` (the authenticated read).

`MediaView` is mounted at `/media/<path:name>` (`apps/urls.py:45`).

---

## 1. What it is (and what it is NOT)

- **Files live in the database, not on disk.** `STORAGES["default"]` is
  `core.storage.DatabaseStorage` (`apps/settings/base.py:376-380`), so every
  `FileField` and `ImageField` in the repo reads and writes `StoredFile` rows.
  The reasoning is in the model docstring: uploads survive ephemeral-disk
  redeploys, ride along with normal database backups, and need no object-storage
  account (`core/models.py:1-9`).
- **It is scoped to small files on purpose.** The platform accepts spreadsheets,
  images and PDFs; the storage enforces an extension allowlist and a 25 MB
  ceiling as defence in depth (`core/storage.py:36-43`).
- **Access is a capability URL, and the module says so.** `MediaView`
  authenticates the caller but **cannot authorise per file** - a `StoredFile` row
  has no owner, no tenant and no entity. What stops a logged-in user reading
  another tenant's file is that the file's *name* carries 64 bits of entropy and
  is only ever handed to callers already allowed to see the owning record
  (`core/storage.py:15-21`).
- **`validate_upload` is the first line; the storage is the second.**
  `DatabaseStorage._save` raises Django's `ValidationError` from inside `_save`,
  which surfaces as an unhandled 500 rather than a 400 - so every upload
  endpoint needs its own validation, and this is the shared one
  (`core/uploads.py:1-20`).
- **The magic-byte check is defence in depth, not a live fix.** `MediaView`
  already sets `X-Content-Type-Options: nosniff` and forces
  `Content-Disposition: attachment` on everything that is not `image/*`, so an
  HTML payload renamed `.png` is served as a broken image rather than executed.
  The check buys not depending on those two headers staying correct forever
  (`core/uploads.py:16-24`).
- **Nothing here deletes anything.** Django has not deleted files on model delete
  since 1.3, and no sweeper exists, so `StoredFile` rows outlive the records that
  point at them (§8).

## 2. Domain model

### `StoredFile` (`core/models.py:15`)

| Field | Notes |
|---|---|
| `name` | The storage path, **unique** - this is the address and the capability |
| `content` | `BinaryField` - the bytes |
| `content_type` | What `MediaView` will serve it as |
| `size` | Byte count, used for `Content-Length` |
| `created_at` | Indexed |

No owner, no tenant, no entity, no reference back to the record that uploaded it.
That absence is what makes the capability-URL model necessary rather than
optional.

## 3. The upload path

```text
serializer / service
   └─ validate_upload(file, allowed=…, max_bytes=…)      first line, answers 400
         ↓ returns (safe_name, content_type)
   FileField.save → DatabaseStorage.get_available_name()  adds the entropy token
                  → DatabaseStorage._save()               second line, raises 500
                  → StoredFile row
```

### `validate_upload` (`core/uploads.py:208`)

Callers pass their own policy - the allowed extensions and the ceiling
legitimately differ between a public vendor portal and an internal ticket
attachment - and get back `(safe_name, content_type)`.

| Check | Failure |
|---|---|
| `upload is None` | "A file is required." |
| Extension in `allowed` | "Upload one of: …" (or the caller's `type_message`) |
| `size` present, > 0, ≤ `max_bytes` | three distinct messages |
| First 16 bytes match the extension | "The file content does not match its extension." |

Two policies ship with it: `DOCUMENT_EXTENSIONS` (pdf, png, jpg, jpeg, webp) at
5 MB, and `TICKET_EXTENSIONS` (those plus gif, csv, xlsx, xls) at 10 MB
(`core/uploads.py:148-160`).

The returned name is stripped of `"` and `\` and of unprintable characters, and
truncated to 255 - it is the **display** name only. The stored path is chosen by
the model's `upload_to` and then tokenised by the storage.

`_CONTENT_TYPES` (`core/uploads.py:166-176`) is a fixed extension-to-type map,
deliberately not `mimetypes.guess_type` and never the browser's claim: the
content type is what `MediaView` serves the bytes as, so it must follow the
verified magic bytes.

### `_magic_ok` (`core/uploads.py:185-205`)

| Extension | Signature |
|---|---|
| pdf | `%PDF` |
| png | `\x89PNG\r\n\x1a\n` |
| jpg, jpeg | `\xff\xd8\xff` |
| webp | `RIFF` … `WEBP` |
| gif | `GIF87a` or `GIF89a` |
| xlsx | `PK\x03\x04` (any OOXML container) |
| xls | `\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1` (OLE2) |
| csv | **unverifiable, allowed** |
| anything else | **False** |

`csv` is listed in `_UNVERIFIABLE` explicitly so the default stays fail-closed:
an extension nobody has thought about fails rather than sailing through.

### `DatabaseStorage._save` (`core/storage.py:70-89`)

Re-checks the extension against `ALLOWED_EXTENSIONS` and the length against
`MEDIA_DB_MAX_BYTES` (25 MB by default), then `update_or_create`s the row. Its
`content_type` comes from `mimetypes.guess_type(name)` - **not** from the
verified map, so the stored type is derived from the path a second time rather
than carried through from validation (§8).

### `get_available_name` (`core/storage.py:107-120`)

```text
school_logos/crest.png  →  school_logos/crest-9f3c1a70b2e84d55.png
```

`secrets.token_hex(8)` - 64 bits - inserted before the extension, with the
original root kept as a readable prefix. `super().get_available_name` still runs
afterwards as a belt-and-braces uniquifier.

`_clean_name` (`core/storage.py:46-50`) normalises separators, strips a leading
`/`, and raises `SuspiciousOperation` on any `..` segment.

## 4. The read path

`MediaView.get` (`core/views.py:124-138`):

```text
IsAuthenticatedAndActive
   → StoredFile.objects.filter(name=name).first()
   → 404 (error envelope) if absent
   → HttpResponse(bytes, content_type=row.content_type or octet-stream)
        Content-Length: row.size
        X-Content-Type-Options: nosniff
        image/*  → Cache-Control: private, max-age=86400
        anything else → Content-Disposition: attachment; filename="<basename>"
```

The permission class is `IsAuthenticatedAndActive` alone: authenticated, not
SUSPENDED/LOCKED/DEACTIVATED, and on a live tenant. There is no RBAC key, no
tenant filter and no per-file check - by design, per §1.

Note what it does **not** do: no range requests, no ETag, no streaming (the whole
file is materialised in memory), and no rate limit.

## 5. Derivations

- **The capability is the name.** `DatabaseStorage.url` returns
  `/media/<name>` (`core/storage.py:104-105`), and that string is what a
  serializer embeds as a `*_url`. Knowing it is the authorisation.
- **`content_type` is decided twice.** `validate_upload` returns the verified
  type from `_CONTENT_TYPES`, and `_save` independently guesses one from the
  filename. Which one a caller stores depends on the caller: `vs_tickets` keeps
  the verified value on its own `TicketAttachment.content_type` column
  (`vs_tickets/services/tasks.py:219-222`) and serves that, while a plain
  `ImageField` has only the storage's guess.
- **Inline versus download follows the stored type**, so an attacker who could
  get an `image/*` type onto non-image bytes would get them rendered inline -
  which is exactly what the magic-byte check exists to prevent, and why the
  ticket module derives the type from the bytes rather than the multipart
  header.
- **`Cache-Control: private, max-age=86400` on images** means a capability URL,
  once used, sits in the browser cache for a day. Correct for a logo; worth
  knowing for a receipt.
- **The 404 uses `error_response`** (`core/views.py:127`), so a missing file
  answers the platform envelope while a successful read answers raw bytes - the
  only sensible split, and worth noting for a client that parses by content type.

## 6. What it writes

| Operation | Writes |
|---|---|
| Any `FileField.save` | one `StoredFile` row, or an update if the name collides |
| `storage.delete(name)` | deletes the row, when something calls it |
| `MediaView.get` | nothing - no access log, no audit event, no counter |

**Reads are not recorded anywhere.** Fetching a staff photo, an import
spreadsheet or an expense receipt leaves no trace in `vs_audit` or in any local
log.

## 7. Worked example

A school admin uploads a crest through the school-branding endpoint:

```text
POST /v1/i/bright-star/branding/?tenant=bright-star   (multipart, crest.png)

validate_upload(file, allowed=DOCUMENT_EXTENSIONS, max_bytes=5MB)
   extension png ✓   size 84 KB ✓   head starts \x89PNG ✓
   → ("crest.png", "image/png")

FileField.save("school_logos/crest.png", …)
   get_available_name → "school_logos/crest-9f3c1a70b2e84d55.png"
   _save              → StoredFile(name=…, content=…, content_type="image/png",
                                   size=86016)
```

The serializer then returns
`"logo_url": "/media/school_logos/crest-9f3c1a70b2e84d55.png"`.

Any authenticated user who has that string can fetch it:

```text
GET /media/school_logos/crest-9f3c1a70b2e84d55.png
  → 200, image/png, Cache-Control: private, max-age=86400
```

including a user from another tenant, if they somehow obtained the name. Nobody
can obtain it by guessing: the token space is 2^64.

And the two refusals:

```text
POST … (payroll.xlsx renamed to receipt.pdf)
  → 400 {"file": ["The file content does not match its extension."]}

POST … (a 40 MB scan)
  → 400 {"file": ["Each file must be 5MB or smaller."]}
```

The second one only reaches 400 because the endpoint called `validate_upload`.
An endpoint that skips it and lets a 40 MB file reach `_save` gets a `500` with
code `SERVER_ERROR`, because `_save` raises Django's `ValidationError` from
inside storage - the exact problem `core/uploads.py` was written to solve
(`core_code_issues.md` §2).

## 8. Gotchas / known limitations

Full evidence in **`error/core/core_code_issues.md`**.

- **A capability URL cannot be revoked, and nothing deletes the bytes.** Deleting
  the owning record leaves the `StoredFile` row in place and the URL working
  forever; there is no sweeper and no reference count
  (`core_code_issues.md` §1).
- **The capability model is a convention with no enforcement.** "The name is only
  handed to callers allowed to see the owning record" is true of every serializer
  today and is checked by nothing - a single over-broad serializer field turns
  one record's file into a platform-wide read (`core_code_issues.md` §1).
- **Storage validation answers 500, not 400** (`core/storage.py:74-83`), which is
  why every upload endpoint has to remember `validate_upload` first. Two
  endpoints in the repo have historically forgotten (`core_code_issues.md` §2).
- **`_save` re-guesses the content type from the filename** rather than taking
  the verified one, so a `FileField` with no explicit type column stores
  `mimetypes.guess_type`'s answer - the value `MediaView` will later serve the
  bytes as (`core_code_issues.md` §9).
- **`MediaView` materialises the whole file in memory** and supports no range
  requests, so a 25 MB PDF is a 25 MB allocation per request, unthrottled.
- **Reads are never audited**, on a route that serves receipts, payslip scans and
  import spreadsheets.
- **`ALLOWED_EXTENSIONS` and `TICKET_EXTENSIONS` are two lists of the same
  thing**, kept in step by a comment (`core/uploads.py:151-154`).
- **Justified by design:** the entropy token, and keeping the original filename
  root as a readable prefix.
- **Justified by design:** `csv` is explicitly unverifiable rather than
  heuristically checked - inventing "does it contain commas?" would reject valid
  single-column files.
- **Justified by design:** the fixed extension-to-type map instead of the
  browser's claim.
- **Justified by design:** files in the database rather than on an ephemeral
  disk, with the exit route named (point `STORAGES["default"]` at S3 and migrate
  the rows).

## 9. Permissions & tenant isolation

| Surface | Gate |
|---|---|
| Uploading | whatever the owning endpoint requires |
| `GET /media/<name>` | `IsAuthenticatedAndActive`, and knowing the name |
| Deleting | nothing calls it |

**There is no tenant isolation on the read**, and that is the deliberate design
recorded at `core/storage.py:15-21`. The boundary is the unguessability of the
name plus the discipline of every serializer that hands one out.

Two things follow that are worth stating plainly rather than leaving implied:

1. **A file's protection is only as good as the narrowest serializer that
   exposes its URL.** `vs_tickets` avoids the issue entirely by never exposing a
   `/media/` path - it reverses its own ticket-scoped download route and checks
   internal-note visibility there (`vs_tickets/serializers.py:38-46`). That is
   the pattern to copy for anything sensitive.
2. **Once a person has seen a URL, they keep it.** Removing their access to the
   record does not remove their access to the file.

## 10. Code map

| File | Responsibility |
|---|---|
| `core/models.py:15-28` | `StoredFile` |
| `core/storage.py:36-50` | `ALLOWED_EXTENSIONS`, `MAX_BYTES_DEFAULT`, `_clean_name` |
| `core/storage.py:62-105` | `_open`, `_save`, `exists`, `delete`, `size`, `url` |
| `core/storage.py:107-120` | `get_available_name` - the capability token |
| `core/uploads.py:148-182` | The two policies, `_CONTENT_TYPES`, `_UNVERIFIABLE` |
| `core/uploads.py:185-205` | `_magic_ok` |
| `core/uploads.py:208-257` | `validate_upload` |
| `core/views.py:121-138` | `MediaView` |
| `apps/settings/base.py:372-381` | `MEDIA_URL`, `STORAGES`, `MEDIA_DB_MAX_BYTES` |
| `apps/urls.py:45` | The route |

## 11. Test coverage & gaps

`core/tests.py` is the module's best-covered area:

- `DatabaseStorageTests` (`tests.py:26-76`) - the default storage is
  database-backed; save/open round-trips; a CSV is accepted and an `.exe`
  rejected; the size ceiling is enforced; delete removes the row; path traversal
  is blocked; and `test_stored_name_is_unguessable` pins the entropy token.
- `MediaViewTests` (`tests.py:78-108`) - anonymous is 401, an authenticated user
  gets the bytes with the right content type, and a missing name is 404.

What it does not cover:

1. **`validate_upload`, entirely.** The module written to be the shared first
   line of validation - the extension check, all four size branches, and every
   one of the eight magic-byte signatures - has no test in `core`. Its behaviour
   is asserted only indirectly, through `vs_tickets`'
   `test_declared_content_type_cannot_decide_how_a_file_is_served`.
2. **`_magic_ok`'s fail-closed default** - that an unknown extension returns
   False rather than True.
3. **Cross-tenant read.** No test asserts what the design actually is: that a
   user from another tenant *can* fetch a file whose name they know. Pinning it
   would make the capability model a decision rather than an accident.
4. **The `Content-Disposition: attachment` branch** - only the image path is
   asserted, and the attachment header is half of what makes a mislabelled file
   safe.
5. **`nosniff`**, which is the other half.
6. **Orphaned rows** - that deleting a record leaves the file behind.

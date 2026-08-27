# core_file_storage

Where every uploaded byte on this platform lives, how it gets validated on the
way in, and how it is served back. Six pieces: the `StoredFile` table,
`DatabaseStorage` (wired as the default Django storage), `validate_upload` (the
shared first line of validation), `core.binding` (which ties bytes to the record
they belong to), `core.media` (which decides who may read them), and `MediaView`
(the authorised read).

`MediaView` is mounted at `/media/<path:name>` (`apps/urls.py:47`).

---

## 1. What it is (and what it is NOT)

- **Files live in the database, not on disk.** `STORAGES["default"]` is
  `core.storage.DatabaseStorage` (`apps/settings/base.py:376-380`), so every
  `FileField` and `ImageField` in the repo reads and writes `StoredFile` rows.
  The reasoning is in the model docstring: uploads survive ephemeral-disk
  redeploys, ride along with normal database backups, and need no object-storage
  account (`core/models.py`).
- **It is scoped to small files on purpose.** The platform accepts spreadsheets,
  images and PDFs; the storage enforces an extension allowlist and a 25 MB
  ceiling as defence in depth (`core/storage.py:38-45`).
- **Access is authorised per file, on every read.** A `StoredFile` knows its
  tenant, the record it is evidence for, and whether it is still current, and
  `core.media.authorize` refuses unless all of that agrees with the caller
  (`core/media.py`). The URL is signed for one user and expires.
- **The entropy token is defence in depth now, not the access control.** It still
  stops a guessable path like `expense-receipts/receipt.pdf` from being typed in
  (`core/storage.py:133-146`). It never stopped a name that had been handed out
  once from working for ever, for anyone - which is the hole the binding and the
  signature close.
- **`validate_upload` is the first line; the storage is the second.**
  `DatabaseStorage._save` raises Django's `ValidationError` from inside `_save`,
  which surfaces as an unhandled 500 rather than a 400 - so every upload
  endpoint needs its own validation, and this is the shared one
  (`core/uploads.py`).
- **The magic-byte check is defence in depth, not a live fix.** `MediaView`
  already sets `X-Content-Type-Options: nosniff` and forces
  `Content-Disposition: attachment` on everything that is not `image/*`, so an
  HTML payload renamed `.png` is served as a broken image rather than executed.
  The check buys not depending on those two headers staying correct forever
  (`core/uploads.py`).
- **A file is retired when its record is, and how depends on which.** Deleting
  the owning row, or replacing the upload on it, revokes the `StoredFile`: the
  URL closes and the bytes are emptied (`core/binding.py`). *Archiving closes the
  URL and touches nothing else* - the read is refused at `authorize` time while
  the bytes stay whole, because a record is archived precisely so somebody can
  read it later, and emptying its evidence would destroy the thing the archive
  exists to keep. A module whose archived records should keep serving their files
  says so with `serve_when_retired=True` when it registers its policy.

## 2. Domain model

### `StoredFile` (`core/models.py:15`)

| Field | Notes |
|---|---|
| `name` | The storage path, **unique** - the address, no longer the credential |
| `content` | `BinaryField` - the bytes; emptied on revoke |
| `content_type` | What `MediaView` will serve it as |
| `size` | Byte count, used for `Content-Length` |
| `created_at` | Indexed |
| `tenant` | Whose file it is. Stamped by `_save` from the request's tenant context. Null means it was written with no tenant in context, and such a row is never served through `/media/` |
| `created_by` | Who was acting when the bytes were written |
| `owner_content_type` / `owner_object_id` / `owner_field` | The record it is evidence for, and which of that record's file fields points here |
| `revoked_at` | Set when the file stops being current - superseded or its record deleted |

The binding columns are the authorisation input, not bookkeeping. They are what
let the read ask *whose file is this*, *what record is it evidence for*, and *is
it still current* - three questions a bare name and some bytes could not answer,
which is why the name had to serve as the credential before.

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

### `get_available_name` (`core/storage.py:133-146`)

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

`IsAuthenticatedAndActive` is only the first of four gates. `core.media.authorize`
then requires, in order and failing closed at each step:

1. the row is not revoked;
2. the `?t=` signature is valid, unexpired, and was issued **to this caller** -
   so a forwarded link is dead for whoever receives it, and a stale one is dead
   for everybody;
3. the row's `tenant` equals the caller's asserted tenant;
4. the owning record's registered read policy says yes.

**Every refusal is a 404**, including the ones that really mean "not yours". A
403 would confirm that a name exists, which is the one fact a stale or forwarded
link is fishing for.

There is no default policy. A model that registers none is not served at all, so
adding a `FileField` never publishes it by accident - it makes it unreadable
until somebody decides who may read it.

Note what it does **not** do: no range requests, no ETag, no streaming (the whole
file is materialised in memory), and no rate limit.

## 5. Derivations

- **`DatabaseStorage.url` is not the URL a caller gets.** It returns the bare
  `/media/<name>` path and cannot sign, because Django calls it from
  `FieldFile.url`, which has no idea who is asking. Anything handing a URL to a
  caller goes through `core.media.signed_url`, which binds it to a user, stamps
  an expiry, and carries the `?tenant=` assertion so an `<img src>` works without
  the frontend splicing parameters onto it.
- **No identity means no URL, not an open one.** `signed_url` returns `""` rather
  than an unsigned path when it cannot resolve a user. A missing image is a bug
  report; a bearer token is a breach.
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
- **`Cache-Control: private, max-age=86400` on images** means a fetched image
  sits in the browser's own cache for a day, which is what keeps a short URL TTL
  from re-fetching a logo on every render. The cached copy belongs to that
  browser; it is not a URL anyone else can use.
- **The 404 uses `error_response`** (`core/views.py`), so a missing file
  answers the platform envelope while a successful read answers raw bytes - the
  only sensible split, and worth noting for a client that parses by content type.

## 6. What it writes

| Operation | Writes |
|---|---|
| Any `FileField.save` | one `StoredFile` row, stamped with the tenant and actor in context |
| Saving the owning record | binds the row to that record; retires anything the same field pointed at before |
| Deleting the owning record | revokes its rows - URL closed, bytes emptied |
| `storage.delete(name)` | deletes the row outright, when something calls it |
| `core.media.revoke(names)` | revokes explicitly, for records that archive rather than delete |
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

- **An unbound row is refused, deliberately.** "I do not know whose this is" must
  not resolve to "everyone's". Media that predates the binding is rescued by
  `core/migrations/0006_backfill_storedfile_bindings.py`, which walks from each
  record that owns a file to the row holding its bytes - the only direction in
  which the answer exists. What it cannot rescue is an orphan: a `StoredFile` no
  record points at any more stays unbound and unreadable, which is the right
  outcome for a file whose owner was deleted years ago. The migration prints how
  many it bound, skipped and left orphaned.
- **No file-owning model archives yet.** The retirement rule above is written
  against the conventions in use (`archived_at`, `is_archived` and their
  variants) rather than a shared base class, because there isn't one. It is
  enforced the day a file-owning model adopts one, with no further work - which
  is the point of putting it at the choke point rather than in each module.
- **A signature outlives a permission by up to its TTL only for the signature.**
  The record policy is re-evaluated on every read, so withdrawing someone's
  access closes the file immediately; what survives the 15 minutes is the
  signature, not the authorisation.
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
| `GET /media/<name>` | `IsAuthenticatedAndActive`, a signature issued to this caller, the file's own tenant, the owning record still being in service, and that record's policy |
| Deleting | nothing calls it; revocation happens on the owning record instead |

**Tenant isolation is enforced on the read.** A file belongs to a tenant and is
served only to a caller asserting that tenant. Cross-tenant support work goes
through impersonation, which sets the asserted tenant to the school - the
platform's existing audited mechanism for exactly this - rather than through a
platform exemption on the file route.

The registered policies, each owned by the app that owns the record:

| Record | Who may read its file |
|---|---|
| `SchoolBranding.logo` | anyone in the school - it is the sidebar and the letterhead |
| `PlatformStaffProfile.profile_photo` | anyone in the same tenant - it is the organogram |
| `ExpenseClaimLine.receipt` | the claimant, or `finance.expenseclaim.view` scoped to the claim's branch |
| Vendor quotation attachments | `procurement.rfq.view` on the RFQ's entity and branch |
| Vendor invoice attachments | `procurement.vendor_invoice.view`, same scoping |
| Vendor payment attachments | `procurement.vendor_payment.view`, same scoping |

Registration happens in each app's `AppConfig.ready`, so `core` never imports a
domain app - and never imports `apps/schools/` - to find out.

Two things follow that are worth stating plainly:

1. **`vs_tickets` still serves its own files, and should.** It reverses a
   ticket-scoped download route and checks internal-note visibility there
   (`vs_tickets/serializers.py:38-46`). It registers no media policy, so the
   generic route refuses its attachments rather than offering a second way in
   that checks less. Same for audit exports, which have no single owning record
   to ask.
2. **Losing access to the record loses access to the file**, on the next read.
   The policy is re-evaluated every time; nothing is cached in the URL. So is
   the record's own state, so archiving it closes its files without anybody
   having to remember to.

## 10. Code map

| File | Responsibility |
|---|---|
| `core/models.py` | `StoredFile`, including the binding and revocation columns |
| `core/media.py` | The policy registry, signing, `authorize`, `is_retired`, `revoke` |
| `core/binding.py` | Ties bytes to their record; retires superseded and deleted files |
| `core/apps.py` | `CoreConfig.ready` wires the binding onto every model with a file field |
| `core/storage.py` | `ALLOWED_EXTENSIONS`, `_clean_name`, the storage protocol, `get_available_name` |
| `core/uploads.py` | The two policies, `_magic_ok`, `validate_upload` |
| `core/views.py` | `MediaView` |
| `*/media_policies.py` | Each app's answer to "who may read my files" |
| `apps/settings/base.py` | `MEDIA_URL`, `STORAGES`, `MEDIA_DB_MAX_BYTES` |
| `apps/urls.py:47` | The route |

`MEDIA_SIGNED_URL_TTL_SECONDS` (default 900) sets the expiry *window*. Expiries
are rounded to it so the same file keeps the same URL while a page is open - the
browser caches by full URL, and a per-response signature would quietly defeat the
day-long image cache. A URL therefore lives between one and two windows.

## 11. Test coverage & gaps

`core/tests.py` is the module's best-covered area:

- `DatabaseStorageTests` - the default storage is
  database-backed; save/open round-trips; a CSV is accepted and an `.exe`
  rejected; the size ceiling is enforced; delete removes the row; path traversal
  is blocked; and `test_stored_name_is_unguessable` pins the entropy token.
- `MediaBindingTests` - an upload records its tenant, owner and field; replacing
  a logo retires the previous one; deleting the record retires its file.
- `MediaViewTests` - one gate per test, against two real schools: anonymous is
  401; a signed URL serves the school its own logo; a bare path without a
  signature is refused; a forwarded link fails for the person it reaches; an
  expired and a tampered signature are both refused; the bursar who changed
  schools cannot replay her old links; the other school is refused even with a
  signature minted for itself; an unbound row is refused; a model with no
  registered policy is refused; and a revoked file is refused despite a valid
  signature.
- `SignedUrlTests` - the URL binds to the user in context, and no identity yields
  no URL rather than an open one.
- `vs_finance/tests_media_policy.py` - the claimant can reopen her own receipt, a
  colleague in the same school cannot, and finance can. This is the case the
  tenant check can never catch, because all three pass it.

What it does not cover:

1. **`validate_upload`, entirely.** The module written to be the shared first
   line of validation - the extension check, all four size branches, and every
   one of the eight magic-byte signatures - has no test in `core`. Its behaviour
   is asserted only indirectly, through `vs_tickets`'
   `test_declared_content_type_cannot_decide_how_a_file_is_served`.
2. **`_magic_ok`'s fail-closed default** - that an unknown extension returns
   False rather than True.
3. **The other four registered policies** - the two logo/photo ones and the
   three procurement ones - are covered only through their apps' own endpoint
   tests, not directly.
4. **The `Content-Disposition: attachment` branch** - only the image path is
   asserted, and the attachment header is half of what makes a mislabelled file
   safe.
5. **`nosniff`**, which is the other half.
6. **Nothing** - the archive path is covered by `RetiredOwnerTests`, including
   the assertion that most needed making: after archiving, the bytes still equal
   what was uploaded and `revoked_at` is still null.

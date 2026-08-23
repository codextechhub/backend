# core_response_contract

The shape every endpoint on this platform is supposed to answer in, and the four
pieces that produce it: `success_response`, `XVSPagination`, the `core.mixins`
replacements for DRF's generic mixins, and `EnvelopeAutoSchema`, which makes the
generated API docs match. The failure side of the same contract is
`core_error_handling`.

`core` has no URL prefix of its own. Everything here is imported by other
modules, which is what makes a gap in adoption invisible until somebody writes a
client.

---

## 1. What it is (and what it is NOT)

- **One envelope, three keys.** Every successful response is
  `{"success": true, "message": "...", "data": {...}}`
  (`core/response.py:5-11`). A list response adds a fourth, `pagination`
  (`core/pagination.py:13-30`).
- **It is a convention, not a mechanism.** `DEFAULT_RENDERER_CLASSES` is plain
  `JSONRenderer` (`apps/settings/base.py:46-49`); nothing wraps a response the
  view did not wrap itself. A view that returns `Response(serializer.data)` returns
  a bare object and no test or lint notices.
- **The mixins are drop-in replacements, not additions.** `core.mixins` defines
  classes with DRF's own names - `RetrieveModelMixin`, `CreateModelMixin`,
  `UpdateModelMixin`, `DestroyModelMixin` - so a viewset opts in by importing
  from `core.mixins` instead of `rest_framework.mixins`
  (`core/mixins.py:21-97`). Import the wrong one and everything still works,
  just unwrapped.
- **`list` is not in the mixin set, on purpose.** Lists are wrapped by
  `XVSPagination.get_paginated_response`, which every paginated view gets from
  `DEFAULT_PAGINATION_CLASS` without asking (`core/mixins.py:112-113`).
- **`data` is never null.** `success_response` coerces a falsy `data` to `{}`
  (`core/response.py:9`), so an empty list comes back as `{}` rather than `[]` -
  a shape several modules' tests pin explicitly because it surprises clients.
- **`error_response` is not the error path.** It exists for views that want to
  hand-build a refusal; the actual error contract is produced by
  `custom_exception_handler` and has a different shape (`error` rather than
  `data`). See `core_error_handling`.
- **The schema wrapper is documentation only.** `EnvelopeAutoSchema` changes what
  the OpenAPI document says, not what the server sends.

## 2. Domain model

None. This slice owns no model and no table.

## 3. The four pieces

### `core/response.py`

```python
success_response(message, data=None, status=200)
    -> {"success": True, "message": message, "data": data or {}}

error_response(message, error=None, status=400, code=None)
    -> {"success": False, "message": message, "error": error or {}}
       (+ "code" at the TOP level when code is passed)
```

Note where `code` lands: `error_response` puts it beside `error`, while
`custom_exception_handler` puts its code **inside** `error`
(`core/exceptions.py:101-105`). Two error shapes, from two files in the same
package (`core_code_issues.md` §5).

### `core/pagination.py` - `XVSPagination`

`PageNumberPagination` with `?page=`, `?page_size=` and a 100-row ceiling. Its
`get_paginated_response` returns the envelope plus:

```json
{"currentPage": 1, "pageSize": 25, "totalItems": 137,
 "totalPages": 6, "next": "...", "previous": null}
```

`totalPages` is `math.ceil(totalItems / pageSize)`, and the page size read is
`get_page_size(request)` - the *effective* one, so a `?page_size=10` request
reports six pages of ten rather than six of twenty-five.

`get_paginated_response_schema` mirrors the same structure for the docs, which is
how drf-spectacular documents a list correctly without `EnvelopeAutoSchema`
touching it.

### `core/mixins.py`

| Class | Message | Status |
|---|---|---|
| `RetrieveModelMixin` | "Data retrieved successfully." | 200 |
| `CreateModelMixin` | "Created successfully." | 201 |
| `UpdateModelMixin` | "Updated successfully." | 200 |
| `DestroyModelMixin` | "Deleted successfully." | **200**, not 204 |
| `XVSModelViewSetMixin` | all four, combined | - |

Two behaviours worth knowing. `DestroyModelMixin` answers `200` with a body
rather than DRF's `204` (`core/mixins.py:88-96`), because a `204` cannot carry
the envelope. And `UpdateModelMixin` clears `_prefetched_objects_cache` after
saving (`core/mixins.py:70-72`), so a response serialized after an update does
not show a stale prefetched relation.

Each message is a fixed string. A view that wants its own ("Ticket created
successfully.") overrides the method and calls `success_response` directly,
which is what `vs_tickets` does.

### `core/schema.py` - `EnvelopeAutoSchema`

Wired as `DEFAULT_SCHEMA_CLASS` (`apps/settings/base.py:65`). Three jobs:

1. **Wrap 2xx response schemas in the envelope**
   (`core/schema.py:145-163`), skipping anything that already looks enveloped or
   carries a `pagination` block, and swallowing every exception so a schema
   problem can never break generation.
2. **Group endpoints into folders** from `_TAG_MAP`
   (`core/schema.py:40-70`), a prefix-to-name list, most specific first.
3. **Name each operation** from an explicit `docstring-name:` line in the view's
   docstring (`core/schema.py:81-113`), plus a verb suffix for multi-operation
   views. The tag is deliberate so doc names are chosen rather than leaked from
   implementation prose; a view without one falls back to the first meaningful
   docstring line.

## 4. Lifecycle

None - these are pure functions and classes. The only thing that changes over
time is which views use them, and nothing measures that.

## 5. Derivations

- **`data or {}`** is the reason an empty collection serializes as an object.
  `success_response(message, [])` and `success_response(message, None)` produce
  identical bodies, so a client cannot distinguish "no rows" from "nothing to
  say". Several modules pin the shape in a test precisely because of it
  (`vs_tickets/tests.py:634-638`).
- **`totalPages` is 1 for an empty result set**, not 0, because
  `math.ceil(0 / 25)` is 0 and the guard `if page_size else 1` only fires when
  the page size itself is falsy. So an empty list reports `totalItems: 0,
  totalPages: 0` - the ternary protects against a zero page size, not a zero
  count.
- **`_looks_enveloped`** tests for a `success` property
  (`core/schema.py:21-22`), so a serializer that happens to have a field called
  `success` is never wrapped.
- **`_operation_verb`** returns `None` for a single-operation view
  (`core/schema.py:122-123`), so the folder name alone is the operation name -
  which is why `docstring-name:` values read like nouns.

## 6. What it writes

Nothing. No rows, no logs, no metrics.

## 7. Worked example

A view using the mixins:

```python
class TicketViewSet(XVSModelViewSetMixin, viewsets.ModelViewSet):
    """Ticket CRUD plus assignment, transitions, comments and audit.

    docstring-name: ToDo tasks
    """
```

`GET /v1/support/tickets/?tenant=alpha` (paginated list):

```json
{ "success": true, "message": "Data retrieved successfully",
  "pagination": {"currentPage": 1, "pageSize": 25, "totalItems": 3,
                 "totalPages": 1, "next": null, "previous": null},
  "data": [ {...}, {...}, {...} ] }
```

`GET /v1/support/tickets/4471/?tenant=alpha` (detail, through the mixin):

```json
{ "success": true, "message": "Data retrieved successfully.", "data": {...} }
```

`DELETE /v1/todo/tasks/208/?tenant=codex`:

```json
{ "success": true, "message": "Deleted successfully.", "data": {} }
```

`200`, not `204`. And an empty comment thread:

```json
{ "success": true, "message": "Comments retrieved successfully.", "data": {} }
```

`{}`, not `[]`.

Contrast a module that does not use the mixins - `GET
/v1/workflow/instances/<id>/` answers a bare serialized object with no
`success`, no `message` and no `data`, from the same platform
(`core_code_issues.md` §4).

## 8. Gotchas / known limitations

Full evidence in **`error/core/core_code_issues.md`**. The items belonging to
this slice:

- **The envelope is unenforced, and `vs_workflow` does not use it.** Nothing in
  the framework wraps a response, so adoption is per-view and drifts
  (`core_code_issues.md` §4).
- **Two error shapes ship from one package.** `error_response` puts `code` at
  the top level; the exception handler puts it inside `error`
  (`core_code_issues.md` §5).
- **`data or {}` erases the difference between an empty list and no data**, and
  changes the JSON type of `data` depending on how many rows there are
  (`core_code_issues.md` §6).
- **`_TAG_MAP` has drifted from the URL table.** Four mounted prefixes -
  `/v1/onboarding/`, `/v1/exports/`, `/v1/support/`, `/v1/health/` - have no
  entry, so four whole modules land in drf-spectacular's default tagging
  (`core_code_issues.md` §7).
- **`totalPages` is 0 for an empty page**, which a client rendering "page 1 of N"
  has to special-case.
- **`XVSPagination` has no `page_size` default of its own**; it inherits
  `PAGE_SIZE = 25` from settings, so changing the platform default is a settings
  edit rather than a class edit.
- **The `docstring-name:` convention is enforced by nothing.** A view that omits
  it gets the fallback heuristic, and no test asserts that every view has one.
- **Justified by design:** `DestroyModelMixin` answers 200 rather than 204 so the
  envelope survives.
- **Justified by design:** `EnvelopeAutoSchema` swallows every exception rather
  than risking schema generation.

## 9. Permissions & tenant isolation

Neither applies. Nothing here reads a user, a tenant or a permission; the
envelope is applied identically to a platform superuser's response and an
anonymous refusal.

One indirect consequence worth stating: because `success_response` is a plain
function with no knowledge of the caller, **field-level security is never the
envelope's job**. A view that passes a serializer's `.data` in is responsible for
what is in it.

## 10. Code map

| File | Responsibility |
|---|---|
| `core/response.py` | `success_response`, `error_response` |
| `core/pagination.py` | `XVSPagination` - the envelope for lists and its schema |
| `core/mixins.py:21-97` | The four envelope-wrapping mixins |
| `core/mixins.py:105-119` | `XVSModelViewSetMixin` |
| `core/schema.py:21-33` | `_looks_enveloped`, `_envelope` |
| `core/schema.py:40-70` | `_TAG_MAP` - the docs folder map |
| `core/schema.py:73-143` | `EnvelopeAutoSchema` - tags, summaries, verbs |
| `apps/settings/base.py:45-83` | Where all four are wired in |

## 11. Test coverage & gaps

There is **no test file for the response contract**. `core/tests.py` covers
storage and media; `core/test_exceptions.py` covers the error handler;
`core/test_jobs.py` covers background jobs. Nothing tests:

1. **`success_response` and `error_response`** - not the `data or {}` coercion,
   not `code`'s position, not the default status.
2. **`XVSPagination`** - not the envelope, not `totalPages`, not the
   `?page_size=` ceiling, and not the empty-page case.
3. **The four mixins** - not one of the four messages, not the 200-on-delete
   decision, not the prefetch-cache invalidation.
4. **`EnvelopeAutoSchema`** - not the wrapping, not the tag map, not
   `docstring-name:` parsing or the fallback.
5. **That every view uses the envelope.** This is the test that would have caught
   `core_code_issues.md` §4: walk the URL conf, call each 2xx-capable endpoint,
   assert the body has `success`. Nothing like it exists.

Every assertion about the envelope in this repo lives in a domain module's own
tests, asserting it for one endpoint at a time.

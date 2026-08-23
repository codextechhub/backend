# core_error_handling

The other half of the platform's API contract: what a failure looks like, and
which exception becomes which status code. One function,
`custom_exception_handler`, wired as `EXCEPTION_HANDLER` for every DRF view in
the repo. The success half is `core_response_contract`.

---

## 1. What it is (and what it is NOT)

- **It is the last line, not the first.** DRF's own `exception_handler` runs
  first (`core/exceptions.py:94`); this function then intercepts the exception
  types DRF does not understand and re-shapes the ones it does.
- **Every branch answers the same envelope**:
  `{"success": false, "message": "...", "error": {"code": "...", "detail": ...}}`.
  The `code` is a machine-readable token; the `message` is a sentence a person
  can act on.
- **It is type-driven, not message-driven.** Ten branches, each keyed on an
  exception class. Nothing parses a string except `_is_unique_violation`'s
  SQLite fallback.
- **It deliberately turns some database errors into client errors and refuses to
  turn others.** A unique violation is the caller's fault and answers `400`; a
  foreign-key or NOT NULL violation is a server bug and answers a logged `500`
  (`core/exceptions.py:143-157`).
- **It never leaks the blocking rows.** A `ProtectedError` reports model names
  and counts, never the objects, which may live outside the caller's tenant
  (`core/exceptions.py:39-65`).
- **It is not a catch-all for Python errors.** `ValueError`, `TypeError`,
  `KeyError` and `DataError` all fall through to the final branch: a logged
  `500` with code `SERVER_ERROR`. That is why a non-numeric id in a query
  parameter is a 500 in four different modules.
- **There is no correlation id.** The 500 body carries no reference a user could
  quote, and the log line carries no request identifier tying it to the response.

## 2. Domain model

None.

## 3. The branches, in order

`custom_exception_handler(exc, context)` (`core/exceptions.py:91`):

| # | Condition | Status | `code` | Notes |
|---|---|---|---|---|
| 1 | `InvalidToken`, `TokenError` | 401 | `TOKEN_INVALID` | SimpleJWT; message is fixed, detail is the token error |
| 2 | `DjangoValidationError` | 400 | `VALIDATION_ERROR` | `detail` is `{field: [messages]}`; message names the fields |
| 3 | `ProtectedError`, `RestrictedError` | **409** | `PROTECTED_REFERENCE` | Must stay above 4 - both subclass `IntegrityError` |
| 4a | `IntegrityError`, unique violation | 400 | `DUPLICATE` | "A record with these details already exists." |
| 4b | `IntegrityError`, anything else | 500 | `SERVER_ERROR` | Logged with `logger.exception` |
| 5 | Anything with `error_code` **and** `message` attributes | `http_status` or 422 | the exception's own | The duck-typed domain-exception protocol |
| 6 | Any other DRF exception (`response is not None`) | DRF's own | `REQUEST_ERROR` | `detail` is DRF's body verbatim |
| 7 | Everything else | 500 | `SERVER_ERROR` | Logged with `logger.exception` |

Branch 3's position is load-bearing and the code says so: `ProtectedError` and
`RestrictedError` subclass `IntegrityError`, so putting branch 4 first would log
every blocked delete as an opaque 500 - which is what the platform did before.

Branch 5 is the contract every domain module's exception base implements.
`vs_workflow.exceptions.WorkflowError` is the fullest example: `error_code`,
`message`, `http_status` and an `extra` dict that becomes `detail`.

## 4. Lifecycle

None. One call per failed request.

## 5. Derivations

- **`_is_unique_violation`** (`core/exceptions.py:17-36`) is engine-aware, and
  in this order: PostgreSQL's SQLSTATE `23505` from `pgcode` or
  `diag.sqlstate`; MySQL/MariaDB's numeric `1062`; then a lowercase substring
  search of the exception text for SQLite and anything unrecognised. The
  substring test is the fallback, not the primary check.
- **`_blocker_summary`** (`core/exceptions.py:39-65`) counts protected objects by
  model and renders `verbose_name` or `verbose_name_plural` by count, producing
  "2 positions and 1 branch" plus `{"vs_user.position": 2, "vs_tenants.branch": 1}`.
  With no objects at all it answers "other records" and an empty detail rather
  than an empty sentence.
- **`_validation_error_detail`** (`core/exceptions.py:68-79`) prefers
  `message_dict` so field keys survive. The commit note above the branch explains
  why: it used to read `exc.messages`, which flattens a field-keyed error into a
  bare list, so a `full_clean()` on a model with eight editable columns answered
  "This field cannot be blank." and never said which one. Errors with no field -
  every `ValidationError("some text")` raised from a service - collect under
  `NON_FIELD_ERRORS` (`__all__`), which is where Django itself puts them, so a
  caller never has to branch on the shape.
- **`_validation_error_message`** (`core/exceptions.py:82-88`) renders one
  sentence: `"email: Enter a valid email address.; __all__: …"`, with the
  `__all__` prefix suppressed.
- **DRF's bare-list body is handled explicitly** (`core/exceptions.py:177-181`):
  `ValidationError("some text")` renders as `["some text"]`, and reading `.get()`
  off it used to turn a 400 into a 500. A single-element list of one string is
  now unwrapped into the message; a longer list falls back to the generic
  sentence with the list preserved in `detail`.

## 6. What it writes

Two log lines, both on the paths that answer 500:

| Line | When |
|---|---|
| `logger.exception("Non-unique IntegrityError in request")` | branch 4b |
| `logger.exception("Unhandled exception in request")` | branch 7 |

Branch 3 logs at `info` ("Delete blocked by protected references"), because a
blocked delete is the data model working, not a fault.

Nothing is written to `vs_audit`, and no metric is incremented.

## 7. Worked example

A service raises a plain Django validation error:

```python
raise ValidationError({"email": ["Enter a valid email address."],
                       "phone": ["This field cannot be blank."]})
```

```json
{ "success": false,
  "message": "email: Enter a valid email address.; phone: This field cannot be blank.",
  "error": { "code": "VALIDATION_ERROR",
             "detail": {"email": ["Enter a valid email address."],
                        "phone": ["This field cannot be blank."]} } }
```

`400`. A caller can render the message or highlight the two fields from
`detail` - both are available and neither had to be parsed out of the other.

A blocked delete:

```text
DELETE /v1/user/positions/12/?tenant=codex
```

```json
{ "success": false,
  "message": "This record cannot be deleted because 2 position assignments and 1 staff profile still reference it. Remove or reassign them first.",
  "error": { "code": "PROTECTED_REFERENCE",
             "detail": {"vs_users.positionassignment": 2,
                        "vs_users.platformstaffprofile": 1} } }
```

`409`, and nothing about *which* assignments - they may belong to people the
caller cannot see.

A domain exception, from `vs_workflow`:

```json
{ "success": false,
  "message": "You have already voted on this stage.",
  "error": { "code": "DUPLICATE_APPROVER_ACTION", "detail": {} } }
```

`409`, from the class's own `http_status`.

And the one that is not handled:

```text
GET /v1/support/tickets/?tenant=alpha&assignee=abc
```

`ValueError: Field 'id' expected a number but got 'abc'` falls through every
branch to number 7:

```json
{ "success": false, "message": "An unexpected error occurred.",
  "error": { "code": "SERVER_ERROR" } }
```

`500`, a full traceback in the log, and nothing the caller can do except stop
sending that value. Four modules have a filter in this shape
(`core_code_issues.md` §3).

## 8. Gotchas / known limitations

Full evidence in **`error/core/core_code_issues.md`**.

- **`ValueError` from an ORM filter is a 500 everywhere.** The handler has no
  branch for it, so every unvalidated id query parameter in the repo answers
  `SERVER_ERROR` on a non-numeric value - `vs_tickets`, `vs_todo`, `vs_workflow`
  and `vs_config` each carry at least one (`core_code_issues.md` §3).
- **`DataError` is a 500 too.** A value longer than a column - a 300-character
  filename into a `varchar(255)` - is `django.db.utils.DataError`, which is not
  `IntegrityError`, so it takes branch 7 rather than answering 400.
- **The 500 body carries no reference.** A user who hits one has nothing to quote
  and the log line has nothing to correlate against
  (`core_code_issues.md` §8).
- **Branch 5 is duck-typed on two attribute names.** Any exception that happens
  to define `error_code` and `message` is treated as a domain exception and its
  `message` is returned to the caller verbatim - including a third-party
  exception that defines both by coincidence.
- **`_is_unique_violation`'s fallback is a substring search** of the exception
  text, so a non-unique error whose message happens to contain "unique
  constraint" answers 400 `DUPLICATE`.
- **`error_response` and this handler disagree about where `code` goes**
  (`core_code_issues.md` §5).
- **Justified by design:** `ProtectedError` is checked before `IntegrityError`.
- **Justified by design:** only unique violations become 400s; every other
  integrity error is a logged 500, because a FK or NOT NULL violation is a bug
  and pretending otherwise hides it.
- **Justified by design:** protected-reference detail names models and counts,
  never rows.

## 9. Permissions & tenant isolation

Neither applies directly, but two decisions here are security decisions:

1. **`_blocker_summary` withholds the blocking objects.** The counts are safe;
   the rows are not, because a `PROTECT` can be triggered by a row in another
   tenant that the caller may not know exists.
2. **The generic 500 message says nothing.** "An unexpected error occurred." with
   no detail is deliberate: the traceback goes to the log, not to the client.

The one place that reasoning is not applied is branch 5, where a domain
exception's own `message` and `extra` are returned unmodified. That is correct
for the module exceptions in this repo, all of which are written for a caller -
but the handler does not check.

## 10. Code map

| File | Responsibility |
|---|---|
| `core/exceptions.py:17-36` | `_is_unique_violation` - engine-aware |
| `core/exceptions.py:39-65` | `_blocker_summary` |
| `core/exceptions.py:68-88` | `_validation_error_detail`, `_validation_error_message` |
| `core/exceptions.py:91-195` | `custom_exception_handler` - the ten branches |
| `apps/settings/base.py:64` | Where it is wired |

## 11. Test coverage & gaps

`core/test_exceptions.py` is the one part of `core` with focused tests:

- `CustomExceptionHandlerTests` (`test_exceptions.py:16-53`) - a string
  `ValidationError` returns 400 with its own message; a dict error uses the
  `detail` key; field errors fall back to the generic message and keep the
  detail; a multi-item list falls back; a standard `APIException` keeps its
  detail.
- `DjangoValidationErrorEnvelopeTests` (`55-109`) - a field error names its
  field in the message and keeps it in the detail; several fields are all named;
  and a message with no field is not given a fake one.

Together they pin branches 2 and 6 - the two that had real bugs.

What the suite does not cover:

1. **`_is_unique_violation`**, in any of its three engine paths, and the
   400-versus-500 split in branch 4 that depends on it.
2. **`ProtectedError` / `RestrictedError`** - neither the 409, nor the phrase
   built by `_blocker_summary`, nor the empty-objects fallback.
3. **The domain-exception protocol** (branch 5) - the `http_status` default of
   422 and the `extra` passthrough.
4. **`InvalidToken` / `TokenError`** - the 401 shape every unauthenticated client
   sees first.
5. **Branch 7** - that an unhandled `ValueError` answers 500 with no detail, and
   that it is logged. This is the branch four modules hit in production.
6. **Ordering.** Nothing asserts that a `ProtectedError` takes branch 3 rather
   than branch 4 - the exact regression the comment above it warns about.

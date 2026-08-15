# Notifications: inbox order, search, and the template editor

Backend changes landed 2026-08-15. Three things change for the frontend: the
inbox tab order and default sort, inbox/history search, and the template editor
(which can now show the real message instead of a textarea full of HTML).

---

## 1. Inbox tabs: Unread first, All last

`GET /api/v1/notify/` now returns **unread first, newest first within each
group**. Opening the bell or the inbox shows what still needs attention rather
than whatever arrived most recently.

Tab order in the UI is therefore:

| Position | Tab    | Request                  |
| -------- | ------ | ------------------------ |
| 1 (default, selected on open) | Unread | `GET /v1/notify/?is_read=false` |
| 2        | Read   | `GET /v1/notify/?is_read=true`  |
| 3 (last) | All    | `GET /v1/notify/`               |

"All" keeps the unread-first order, so it is never a wall of read messages with
the unread ones buried. The bell badge still comes from
`GET /v1/notify/unread-count/`.

Ordering is `(is_read, -created_at, id)`. The `id` tiebreaker exists because a
dispatch batch writes many records with the same `created_at`; without it, rows
could repeat or disappear between pages.

## 2. Search must go to the server

`GET /v1/notify/?search=<term>` filters on the message subject, the message body
and the event label. **Do not filter the fetched page in the browser** - that is
what made pagination wrong: the page count, `totalItems` and every page after
the first described the unsearched list.

- Combine freely: `?search=invoice&is_read=false&page=2&page_size=20`.
- `pagination.totalItems` / `totalPages` always describe the search result.
- Terms are truncated at 120 characters.
- Search only ever sees the caller's own notifications.

The admin history log takes the same `?search=` (message, event label, recipient
email). History still requires at least one filter, and a search term now counts
as one.

## 3. Template editor: four fields and a live preview

A notification template is no longer "subject + body + a hand-written HTML
document". It is:

| Field       | What it is                                                        |
| ----------- | ----------------------------------------------------------------- |
| `subject`   | Email subject / feed headline                                     |
| `body`      | The message, as plain text                                        |
| `cta_label` | Button text (optional)                                            |
| `cta_url`   | Button destination, normally one variable, e.g. `{{ reset_url }}` |

The email visual - header, detail tables, bullets, section headings, the button,
the footer - is composed by the backend from that text. Structure is inferred
from how the body is written:

```
Hello {{ user_first_name }},              -> paragraph

TICKET DETAILS                            -> section heading (ALL CAPS line)
Reference: {{ ticket_number }}            -> two-column details table
Priority: {{ ticket_priority }}

- Review the request                      -> bulleted list
- Assign an owner
```

Read-only fields the editor should use:

- `variables` - the `{{ names }}` this template actually uses. Show these as the
  insertable chips; there is no separate variable list to maintain.
- `uses_custom_html` - `true` for the rare template that overrode the shared
  layout with its own `html_body`. Show a warning: it no longer inherits design
  changes.

`html_body` is now an escape hatch. Keep it behind an "Advanced" disclosure; the
normal editing path should not expose it.

### Preview

```
GET  /v1/notify/templates/{id}/preview/          # sample data, no payload
POST /v1/notify/templates/{id}/preview/          # {"context": {"user_first_name": "Ngozi"}}
```

Response `data`:

```json
{
  "channel": "email",
  "subject": "You have been invited to Corona Secondary School on XVision System",
  "body": "…plain text…",
  "html_body": "<!doctype html>…",
  "uses_custom_html": false,
  "variables": ["expiry_days", "invitation_url", "school_name", "user_first_name", "user_full_name"],
  "context_used": {"invitation_url": "https://xvs.codexng.com/example", "...": "..."}
}
```

GET needs no payload: every variable is filled with a representative sample
value, so the preview pane can render the real visual the moment a template is
opened. POST overrides any subset; anything not supplied keeps its sample.

Render `html_body` in a **sandboxed iframe**:

```html
<iframe sandbox="" srcdoc="{{ html_body }}" title="Email preview"></iframe>
```

It is returned as a JSON string rather than an HTML response deliberately -
nothing from the preview should ever execute against the API origin. Show
`context_used` next to the preview so it is obvious which values are stand-ins.

For an in-app template `channel` is `in_app` and `html_body` is `""` - preview
it as a feed card using `subject` and `body`.

`GET /v1/notify/templates/?search=` filters on event key, event label, subject
and body.

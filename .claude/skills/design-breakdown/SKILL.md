---
name: design-breakdown
description: Read a Claude Design export (a .dc.html source or a bundled .html) and work out the API behind it - what every screen reads and writes, which endpoints already serve it, which exist but are closed to the caller, which return the wrong shape, and which do not exist at all. Produces an API plan and then builds it. Use when a frontend module is being built from a design and you own the backend it will call.
---

# design-breakdown - turn a design into the API behind it

A design is a specification for data. Every screen reads something, most write
something, and each of those is an endpoint that either exists, exists and is
refused, exists in the wrong shape, or does not exist. Guess which, and you
build the wrong half: usually a new endpoint beside a working one nobody could
reach, because the real problem was a permission key.

This skill reads the prototype, extracts the data behind every screen, and
classifies each against this codebase. Then it builds what is missing.

**The plan comes first and the user approves it.** The classification decides
how big the work is, and the difference between the buckets below is days
against months.

## Inputs

`$ARGUMENTS` = path to the design file (`.dc.html` source, or the bundled
`.html` from the Download button). Fetch it with `DesignSync` if it lives in a
Claude Design project, and read anything in that project's `uploads/` too - the
brief and its update files carry decisions the markup cannot show, and a later
UPDATE file usually overrides the original brief.

## Steps

### 1. Extract the readable source

A bundled export carries the same markup JSON-escaped inside a JavaScript
string. Search it raw and you find nothing, which reads like an empty design
rather than an unescaping problem:

```python
import re

s = open("DESIGN.html", encoding="utf-8", errors="replace").read()
if "<sc-if" in s and s.count('<sc-if value="') == 0:
    for a, b in (("\\u002F", "/"), ("\\u003C", "<"), ("\\u003E", ">"),
                 ("\\n", "\n"), ('\\"', '"'), ("\\'", "'")):
        s = s.replace(a, b)
body = s[s.find("<x-dc>"):] or s
body = re.sub(r"<svg.*?</svg>", "[i]", body, flags=re.S)
open("/tmp/design-source.html", "w").write(body)
print("sc-if blocks:", body.count("<sc-if"))
```

### 2. Extract the data, not the layout

Ignore the styling. What you want is every binding, because each one is a field
some endpoint has to return:

Two traps live in these patterns, and both fail silently by reporting zero:

- **Handlers are named differently in the two file forms.** The `.dc.html`
  source writes `onClick=`; the bundler rewrites it to `sc-camel-on-click=`.
  Match both or a bundled export reports no actions at all.
- **Names are often dotted.** `t.onMark`, `r.onView` and friends are the
  per-row handlers, and `\w+` misses every one of them.

```python
import re
body = open("/tmp/design-source.html").read()

CLICK  = r'(?:onClick|sc-camel-on-click)="\{\{ ([\w.]+) \}\}"'
CHANGE = r'(?:onChange|sc-camel-on-change)="\{\{ ([\w.]+) \}\}"'

reads   = sorted(set(re.findall(r"\{\{ ([\w.]+) \}\}", body)))              # bindings
lists   = sorted(set(re.findall(r'<sc-for list="\{\{ (\w+) \}\}"', body)))  # collections
actions = sorted(set(re.findall(CLICK, body)))                              # writes
edits   = sorted(set(re.findall(CHANGE, body)))                             # field writes
fields  = re.findall(r"<(input|select|textarea)[^>]*>", body)               # inputs

print(len(reads), "bindings |", len(lists), "collections |",
      len(actions), "actions |", len(edits), "field writes |", len(fields), "inputs")
print("collections:", lists)
```

If actions comes back zero on a design with buttons in it, you matched the wrong
attribute name - go and look at one in the source before planning around it.

- **Collections** are list endpoints. Each needs pagination, filters and an
  empty shape.
- **Actions** are writes. Each needs a verb, a permission key and a refusal.
- **Inputs** are the write payload, and their validation is your serializer's.
- **Bindings** are the read payload. A binding with no column behind it is a
  finding, not a field to invent.

### 3. Classify every screen against this codebase

Read the route map in `apps/apps/urls.py` first. Then put each screen's data in
exactly one bucket - and keep the last three apart, because they are hours,
days and months respectively:

| Bucket | What it means here | Work |
|---|---|---|
| **Served** | An endpoint returns this today, in this shape. | none |
| **Closed** | The endpoint exists and the caller is refused. Almost always a missing `pending_tenant_surface` on the view, or a permission key nobody holds. | a flag and a seeder row |
| **Wrong shape** | The endpoint exists and returns different fields, or a different envelope, than the screen binds to. | a serializer change, plus every existing consumer |
| **Absent** | No endpoint. | a module |

For **Absent**, say whether it is this repo's to build or another module's. If
another module's, name it and stop - do not invent its endpoints, because a
frontend built against invented shapes is built twice.

Then audit the other way: endpoints in the module's namespace that no screen
calls. Each is dead API or a gap in the design.

### 4. Design each endpoint the way this codebase does

Before writing anything, know the five conventions the review will check:

1. **Tenant scoping is the view's job.** Scope on `request.tenant` and use the
   module's `rows_for`-style helper. Another tenant's row answers **404, never
   403**, so tenant identifiers cannot be enumerated. An endpoint that takes no
   identifier at all - "my school's profile" - is better than one that takes a
   pk and checks it.
2. **`pending_tenant_surface` is an explicit allowlist and defaults to closed.**
   Anything a school that has not gone live must reach has to declare it. This
   is where "the API exists but the screen 403s" comes from.
3. **A permission key per verb**, seeded in the module's seeder with its
   prebuilt-role defaults AND a backfill phase for tenants provisioned before
   the key existed. Without the backfill the key only ever reaches tenants
   created after today.
4. **Domain refusals carry a code, a sentence and a status** (see
   `vs_tenants/exceptions.py` and `schools/vs_onboarding/exceptions.py`). Phrase
   the sentence as the thing the caller still has to do - the screen shows it
   verbatim, so "This school has no set of books yet. Contact support to have
   them provisioned." beats "condition not met".
5. **`success_response` for the envelope**, and a `docstring-name:` line on the
   view so it lands in the generated docs.

### 5. Write the plan

To `docs/<module>-api-plan.md`:

1. **Screen → data → endpoint**, one row per screen, with its bucket.
2. **Endpoints to open** - the view, the flag, the key, who gets it by default.
3. **Endpoints to add** - path, verb, payload, refusals, permission key.
4. **Shapes to change** - and every existing consumer of the old shape.
5. **Not ours** - named modules, and what we need from them.
6. **Dead API** - endpoints no screen calls.

Summarise in chat and **stop for approval.** Opening a surface and building a
module are not the same decision.

### 6. Build, in this order

1. The **closed** ones first. A flag and a seeder row unblocks a whole screen,
   and it is the cheapest work in the plan.
2. Then **wrong shape**, with its consumers.
3. Then **absent**, one endpoint at a time.

Per endpoint, tests before you call it done, security first - the repo's
ship-check asks for exactly this:

- **permission denied (403)** for a caller without the key;
- **cross-tenant isolation** - another tenant's row answers 404;
- for a pending-surface endpoint, that a PENDING tenant genuinely reaches it,
  which is the bug the flag exists to prevent;
- happy path, every filter branch, and the empty-list response shape.

Then run the module's suite plus anything sharing the serializer.

### 7. Seed something to look at

A screen cannot be verified against an endpoint that returns nothing. If the
design shows several states - ready, rejected, live, empty - add or extend a
management command that builds one tenant per state, driven **through the real
services** so a state that cannot be reached honestly fails loudly instead of
being faked. `seed_onboarding_scenarios` is the worked example.

Run it twice. A seeder that is not idempotent is a seeder that invents data.

## What this skill is careful about

- **"No API" is two different problems.** Closed is a flag; absent is a module.
  Merging them puts a two-day job next to a two-month one in the same plan.
- **A binding is not a field.** The design may bind data nothing stores. Say so
  rather than adding a column to match a mock.
- **Do not build another module's endpoints.** Name the module, state what you
  need, and let the frontend park that screen.
- **The refusal text is UI.** It is rendered verbatim under a form field, so
  write it for the person reading it, not for the log.

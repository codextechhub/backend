# Registering a place for the search bar

The search bar (`GET /v1/search/?q=`, app `vs_search`) answers with people,
screens ("actions") and settings. It has no list of its own. Every app declares
what it owns in a `search_places.py` module at its root, and the first search
imports every installed app's module (`autodiscover_modules("search_places")`).
`vs_search` imports nothing from any other app, and a school app under
`apps/schools/` registers through exactly the same door as an engine.

When you add a screen or a setting, add its place in the same change. A screen
nobody can find from the search bar is a screen the frontend has to hard-code,
which is the thing this registry exists to end.

## A place

```python
from vs_search.registry import ACTION, SETTING, Place, register
```

| Field | Meaning |
| --- | --- |
| `kind` | `ACTION` (a screen) or `SETTING` (one setting on a screen) |
| `key` | Stable dotted id, unique across the product. A second registration raises `DuplicatePlaceKey` |
| `label` | What the hit reads as |
| `description` | One short line, may be empty |
| `keywords` | Extra words a person types that the label does not contain |
| `group` | Heading a setting is listed under, usually its screen's name |
| `screen` | Stable frontend screen key, `<module>.<resource>` (`finance.invoices`, `finance.settings.documents`) |
| `anchor` | The field or section on that screen, empty for the screen itself |
| `permission` | The key the screen's own read endpoint requires; a tuple means any of them; `None` means any signed-in member |
| `capability` | The `vs_config` capability the module answers to, as `vs_rbac/capability_map.py` maps its permission module; `None` where the module is core |
| `condition` | The name of a registered condition the screen also needs, for a screen that exists only sometimes (below); `None` for a screen that always exists for whoever may open it |

`permission` and `capability` decide who sees the place and are never sent to
a client. Take the permission from the view, not from memory: the search bar
must never offer a screen that would refuse the reader who opens it. Where the
frontend asks a stricter key before it opens the screen (the school's settings
area asks `school.settings.view` before any rules screen), name that key, so
the hit never lands on a screen the frontend then hides.

`vs_search.tests.PlaceCatalogueTests` keeps every place honest against the
catalogue the seeds build: each permission key must be a seeded permission,
each capability a seeded capability, and a place that names no capability may
not open to a school key in a module a plan can leave out
(`vs_rbac.capability_map.capability_for`). A typo in a key fails there rather
than silently hiding the screen from everybody.

## Screens that exist only sometimes

Some screens are there for one caller and not another although both hold the
key: the inter-branch screens exist only at a tenant with several branches,
the statutory pack only for a reader whose access covers every branch,
payouts only while a tenant's online payments are held, the to-do board only
for platform staff. Offering one everywhere would put a dead link in front of
a one-branch school, so the place names a **condition**:

```python
from vs_search.registry import ACTION, Place, register, register_condition

def custody_is_held(*, request, tenant, branch, user) -> bool:
    from .constants import CustodyMode
    from .custody import custody_mode
    return tenant is not None and custody_mode(tenant) == CustodyMode.HELD

register_condition("payments.held_custody", custody_is_held)

register(Place(kind=ACTION, key="payments.payouts", label="Payouts",
               screen="payments.payouts", permission="payments.payout.view",
               capability="finance", condition="payments.held_custody"))
```

The rules:

- `vs_search` knows no condition by name. The app that owns the rule
  registers it in its own `search_places.py`, and the predicate calls the
  function the screen's own endpoint or the frontend already uses to decide
  it. Never re-derive the rule: two copies drift, and then the search bar
  offers a screen the screen itself refuses.
- A condition is asked only of a place that passed the permission and
  capability filter and matched the query, and at most once per search
  however many places name it. A search whose matches are all unconditional
  asks none, so a condition may read the database.
- A name is registered once (`DuplicateCondition`), and every condition a
  place names must be registered (`PlaceCatalogueTests` fails on one that
  is not).
- A super administrator is held to conditions too: a screen a one-branch
  school does not have is not there for anybody.
- A screen that needs no key but only a condition (the to-do board, "My
  staff record") sets `permission=None` and names the condition.

The conditions registered today:

| Condition | Registered in | True when | Asks |
| --- | --- | --- | --- |
| `several_branches` | `vs_rbac` | the tenant owns more than one branch | `vs_rbac.scoping.only_branch_id_or_several` |
| `platform_staff` | `vs_rbac` | the caller is on a platform-kind tenant | `vs_rbac.permissions.IsVisionStaff` |
| `finance.whole_tenant_reader` | `vs_finance` | the caller's transactions are not narrowed to some branches | `vs_rbac.scoping.transaction_branch_scope_for_user`, as the statutory pack's endpoint does |
| `finance.member` | `vs_finance` | the caller holds any finance or payments key | `vs_rbac.permissions.holds_any_module_key`, as `HasAnyModuleAccess` does |
| `procurement.member` | `vs_procurement` | the caller holds any procurement key | the same |
| `payments.held_custody` | `vs_payments` | the tenant's custody mode is `HELD` | `vs_payments.custody.custody_mode` |
| `staff.has_record` | `schools.vs_staff` | the caller has a staff record at the tenant | `views.directory.own_staff_record_id`, which "My staff record" answers from |
| `staff.viewer_sees_branches` | `schools.vs_staff` | the staff screens draw branches for this caller | `services.scoping.viewer_sees_branches` |

Every screen once listed here as impossible to register is now registered.

A query matches a place when every word of it is the start of a word in the
label, the keywords or the description. Label matches rank first, then keyword,
then description; ties are alphabetical by label.

## Adding a screen

A new list screen for asset disposals in Finance, read with
`finance.fixedasset.view`:

```python
# vs_finance/search_places.py, in LIST_SCREENS
("asset_disposals", "Asset disposals", "Assets sold or written off.",
 "finance.fixedasset.view"),
```

which `places()` turns into

```python
Place(kind=ACTION, key="finance.asset_disposals", label="Asset disposals",
      description="Assets sold or written off.", screen="finance.asset_disposals",
      permission="finance.fixedasset.view", capability="finance")
```

Then add the screen key and what it routes to in the search section of
`docs/FRONTEND_CHANGES_OWED.md`, so the frontend can route it.

## Adding a setting

**A module-owned setting is derived from the label tables its settings screen
already serves, never written out a second time.** Finance's settings screens
send each setting's label (`vs_finance/settings_history.py`, the
`*_SETTING_FIELDS` tables) and what it drives (`vs_finance/settings_ownership.py`,
the `*_SETTING_CONSUMERS` tables). `vs_finance/search_places.py` builds every
setting place from those two tables, so adding a setting to the screen adds it
to the search bar in the same words, and renaming it renames both.

So adding `default_receipt_footer` to Finance's document settings is:

1. Add it to `DOCUMENT_SETTING_FIELDS` with its label, and to
   `DOCUMENT_SETTING_CONSUMERS` with what it drives, which the screen needs
   anyway.
2. Nothing in `search_places.py`, unless people will type a word the label does
   not contain. Then add it to `EXTRA_KEYWORDS`:

   ```python
   EXTRA_KEYWORDS = {
       "default_invoice_due_days": ("due date", "invoice due", "payment terms"),
       "default_receipt_footer": ("bottom of the receipt",),
   }
   ```

The same pattern holds in `vs_procurement` (`settings.HISTORY_FIELDS` with
`settings_ownership.PROCUREMENT_SETTING_CONSUMERS`) and `vs_notifications`
(`EVENT_TYPE_REGISTRY`). A setting with no label in code gets one in the
module's own table first; do not invent one in `search_places.py`.

Settings held as `vs_config` definitions are contributed from the database
instead (`register_contributor`), because definitions are rows rather than
code. A module whose settings screen saves definitions maps each field name
the screen's endpoint uses to the definition behind it, and
`vs_config.search_places.definition_setting_places` turns that map into
places labelled and described in the definitions' own words:

```python
# schools/vs_attendance/search_places.py
def setting_places():
    return definition_setting_places(
        "attendance.settings", "Attendance settings", KEY_FOR_FIELD,
        permission=PERM_REGISTER_VIEW, capability="attendance",
    )

register_contributor(setting_places)
```

Where the module already keeps that map for its own endpoint (attendance's
`KEY_FOR_FIELD`), reuse it; otherwise write it beside the screen list in
`search_places.py`, from the `CFG_*` constants. A nested field is anchored
with a dot (`leave.allowances`).

`vs_config/search_places.py` itself offers every active definition on the
generic settings screen (`config.settings`), including the ones only the
platform may set, and the fields of the platform's own settings screens
(platform profile, security, integrations). Every `config.*` key is
platform-scoped, so only platform staff see any of them.
`vs_config.search_places.active_definitions()` reads the definitions at most
once a minute per process; call `vs_config.search_places.clear_cache()` in a
test that creates definitions.

## Finding people

A module that owns people registers a provider:

```python
from vs_search.access import caller_may
from vs_search.registry import register_people_provider

def find_students(*, request, tenant, user, query, limit):
    if not caller_may(user, tenant, "school.students.view"):
        return []
    return [{**row, "type": "student", "label": ..., "meta": ..., "screen": "students.record"}
            for row in search_student_hits(request, tenant, query, limit)]

register_people_provider(find_students)
```

The contract:

- Return at most `limit` hits, already scoped to `tenant` and to the caller's
  branches, and `[]` when the caller lacks the module's read key.
  `vs_search.access.caller_may` asks the key and the plan exactly as the
  module's own endpoint is asked at its door.
- Reuse the module's own search function, the one its palette endpoint calls.
  Never write a second matcher: two copies drift, and then the search bar finds
  somebody the directory cannot open.
- Match a query only against the fields the reader may read. A reader who
  cannot see last names and types "Okafor" must find nobody, or a hit with
  the name blanked out still tells them the name is there. Map each lookup
  the matcher searches to the registered field that governs it and narrow
  the list with `vs_rbac.field_enforcement.readable_lookups` before matching;
  with every field hidden, `core.search.search` finds nobody. The palette,
  the search bar and the directory list share one narrowed list
  (`readable_search_fields` in `schools/vs_students/views/students.py` and
  `schools/vs_staff/views/directory.py`, `readable_guardian_search_fields` in
  `schools/vs_students/services/guardians.py`).
- Apply the same Field Access the module's endpoint applies. `request` is
  passed so a serializer can be rendered with it in context. Build `label` and
  `meta` from the filtered row, never from the record, so a hidden field cannot
  travel under another name. A name made of other people's name parts (a
  guardian's wards) follows those people's switches
  (`schools.vs_students.serializers.readable_name`). A person whose every name
  part is hidden is still a hit when the query matched a field the reader may
  read (a staff number), with an empty `label`, exactly as the directory keeps
  their row with an empty name.
- Each hit carries at least `type`, `id`, `label`, `meta` (short secondary text,
  may be empty) and `screen`. Never an email address, a phone number or a
  photograph: the search bar opens over whatever screen the reader is on.
- Providers run in registration order, which follows `INSTALLED_APPS`
  (students, then guardians, then staff). The endpoint asks each for up to ten
  and keeps the first ten in that order.

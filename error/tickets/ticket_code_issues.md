# ticket_code_issues

Everything wrong with `vs_tickets`, in one place, ordered by how much it costs.
Each item states the defect, the evidence, what actually happens to a person, and
the fix. The four slice reports (`ticket_lifecycle`,
`ticket_conversation_attachments`, `ticket_visibility_permissions`,
`ticket_context_integrations`) point here rather than repeating it.

Baseline: the `vs_tickets` suite is **`Ran 45 tests in 37.854s` - OK**
(`cd apps && DB_NAME=cx_tickets_doc ../cx/Scripts/python.exe manage.py test
vs_tickets --settings=apps.settings.local --noinput`). Every item below is
therefore something the suite does not currently catch. Every claim is traced to
a file and line. Nothing here is speculative.

**Status: §1 is FIXED (`373a918`, 26 August 2026); everything else is recorded,
not yet fixed.** The fixed item keeps its original account so the defect stays
readable, with the resolution stated at the top of the section.

---

## Summary

| # | Issue | Severity |
|---|---|---|
| 1 | ~~A new ticket never reaches the support desk's inbox, and the school can read CX's mail instead~~ | **Fixed** |
| 2 | The support desk has three different definitions, and the one that decides who gets told is the weakest | **High** |
| 3 | The Export Centre hands a teacher every ticket in the school | **High** |
| 4 | A school that has not gone live can raise a ticket and then cannot read the answer | **High** |
| 5 | A customer can attach files to an internal note, and can find out which notes are internal | **Medium** |
| 6 | Branch is captured on every ticket and then never used | **Medium** |
| 7 | `Ticket.clean()` never runs on the path that creates tickets | **Medium** |
| 8 | Comments, attachments and audit rows are returned whole, however many there are | **Medium** |
| 9 | Three list filters answer 500 on a value the caller can choose | **Medium** |
| 10 | Ticket creation and file upload are unthrottled | **Medium** |
| 11 | The sanitised filename is computed and thrown away | **Medium** |
| 12 | Two seeded permission keys are checked nowhere | **Low** |
| 13 | The ticket detail payload re-queries once per comment and once per file | **Low** |
| 14 | Assignment tells a caller whether a user id exists | **Low** |
| 15 | The word "school" is in an engine app | **Low** |
| 16 | Smaller defects and dead code | **Low** |

---

## 1. A new ticket never reaches the support desk's inbox, and the school can read CX's mail instead

**FIXED in `373a918` (26 August 2026),** at the engine choke point rather than
here. A notification is now owned by its recipient's own tenant, and the tenant
the event is about is recorded separately as `origin_tenant`
(`vs_notifications/models.py:465`, `services/dispatch.py:173`), so the rows a
ticket raises for platform triage staff belong to the platform: the agent's feed
shows them, and the school's history log does not. Channels resolve per owning
tenant, which closes §1b without marking the ticket events transactional.
Migration `0010_notification_ownership_follows_recipient` moved the rows already
written. `dispatch_ticket_event` still passes `tenant=ticket.tenant` and that is
correct - it is the origin, not the owner
(`vs_tickets/services/notifications.py:123`).

The original account follows, unchanged. Its line references are to the code as it was before the fix; the current ones are above.

**Critical. Root cause is in `vs_notifications`; `vs_tickets` is the proven
instance, and this file is where the ticket-side consequences belong.** See
`error/notifications/notification_code_issues.md` §1 and §2 for the engine-side
account.

### 1a. The rows are filed under the wrong tenant

`dispatch_ticket_event` stamps every ticket notification with the ticket's
tenant:

```python
# services/notifications.py:71-77
NotificationService.send(
    event_key=event_key, context=…, recipients=recipients,
    tenant=ticket.tenant,
    metadata={"ticket_id": ticket.pk, "ticket_number": ticket.ticket_number},
)
```

while the recipients of `ticket.created` are on the platform tenant:

```python
# services/notifications.py:32-44
User.objects.filter(tenant__kind="PLATFORM", status=ACTIVE, …)
```

The in-app feed reads through a `TenantAwareManager`
(`vs_notifications/views.py:133-141`), so its effective filter is
`recipient = me AND channel = in_app AND tenant = the tenant I asserted`. A CX
agent asserts `?tenant=codex`. The rows say `bright-star`.

**What actually happens.** Ngozi at Bright Star raises TK-72608213. Ada, the CX
agent on duty, opens her console at `?tenant=codex`:

- her in-app feed is empty and her unread badge does not move;
- on staging the email is dropped too, because `CELERY_TASK_ALWAYS_EAGER`
  defaults to `True` there (`apps/settings/staging.py`) and the delivery task
  re-fetches its own row through the same manager, finds nothing, logs
  "not found. Skipping." and leaves the row `PENDING` forever;
- meanwhile Bola, Bright Star's school admin, holds
  `communication.message_activity.audit` from the `school_admin` prebuilt, and
  the delivery history log filters on `tenant = request.tenant`. Every
  notification addressed to CX about Bright Star's tickets is stamped
  `bright-star`, so **Bola can read the CX agents' email addresses and the
  message bodies addressed to them**.

### 1b. And the school can switch those notifications off

None of the eight ticket events is marked `is_transactional`
(`vs_notifications/constants.py:198-262`), so all eight are configurable per
tenant, and `resolve_channels` resolves against the tenant the caller passed -
the school's.

`school_admin` and `branch_admin` are seeded
`communication.communication_permissions.enforce`
(`vs_notifications/management/commands/seed_notification_permissions.py:25-29`).
So Bola can `PATCH settings/update/` with
`{"event_type_key": "ticket.created", "channel": "email", "is_enabled": false}`
and **the CX support team stops being emailed about new tickets from Bright
Star**. Nobody at Codex is told, and nothing in the ticket module notices.

### The fix

The engine-side fixes are in the notifications issues file. What belongs here:

1. **Resolve channel settings per recipient, not per event.** A dispatch whose
   audience is CX must not consult the school's settings.
2. **Mark the cross-audience ticket events transactional** -
   `ticket.created` at minimum, and `ticket.commented` /
   `ticket.attachment_added` when they fan out to the queue - which is what the
   registry already does for the vendor-facing procurement events
   (`vs_notifications/constants.py:131-141`).
3. **Split a dispatch whose recipients span tenants** rather than stamping the
   whole batch with one of them.

---

## 2. The support desk has three different definitions, and the one that decides who gets told is the weakest

**High. One question, three hand-written answers.**

### The defect

| Asked by | How | Groups | Role status | Role denies | Personal overrides | `is_active` |
|---|---|---|---|---|---|---|
| `is_support_user` (`services/visibility.py:14-23`) | the evaluator, via `user_has_rbac_permission` | ✓ | ✓ | ✓ | ✓ | n/a |
| `eligible_support_users_qs` (`services/visibility.py:27-73`) | hand-built `Exists` subqueries | ✓ | ✓ | ✓ | **✗** | ✓ |
| `support_recipients` (`services/notifications.py:32-44`) | one `filter()` | **✗** | **✗** | **✗** | **✗** | **✗** |

`support_recipients` reads exactly one path to a grant:

```python
tenant_role_assignments__role__role_permissions__permission_id__in=TRIAGE_PERMISSION_KEYS,
tenant_role_assignments__role__role_permissions__granted=True,
```

No group grants (`role_groups__group__group_permissions`), no `role__status`, no
`granted=False` denies, no personal overrides, and no `is_active`.

### What actually happens

Codex runs its support roles through a permission group, "CX Tier 1", the way
RBAC intends. **Tolu joins Tier 1.** `is_support_user` admits him - the evaluator
reads group grants. `eligible_support_users_qs` lists him in the assignment
picker - it reads group grants too. `support_recipients` does not, so **Tolu is
never notified of a single new ticket**. He can see them if he thinks to look; he
is never told.

Now the other direction. **Ngozi leaves the support team** and an admin denies
her the ticket keys with a personal override rather than unpicking her role.
`is_support_user` refuses her - the console is closed. But
`eligible_support_users_qs` does not read overrides, so **she is still offered as
an assignee**; and `support_recipients` does not either, so **she keeps receiving
an email with the title and requester name of every ticket every school in the
platform raises**.

### Why it exists

Both hand-written queries were written to answer a question the evaluator already
answers, and each was corrected once, separately, when it drifted. The picker's
own comment records one of those corrections
(`services/visibility.py:37-42`): it was widened from `branch__isnull=True` to
`ANY_BRANCH` precisely so that *"somebody the gate admits as a ticket manager has
to appear in the list of people a ticket can be assigned to"*. That is the right
instinct applied to one column and none of the others.

### The fix

Fix the class, not the case. There is already a canonical helper whose docstring
states the rule: `vs_rbac.evaluator.resolve_users_with_permission`
(`vs_rbac/evaluator.py:244-306`) - *"Routing shares `_assignment_branch_q` with
the permission gate so a person this function nominates as an approver cannot be
someone `has_permission` would then refuse."* It honours groups, role denies,
personal ALLOW and DENY overrides, `is_active` and branch scope.

1. Replace `support_recipients` with a union of that helper over
   `TRIAGE_PERMISSION_KEYS` on the platform tenant.
2. Replace `eligible_support_users_qs` with the same call for
   `tickets.ticket.manage`, keeping the `first_name, last_name, email` ordering.
3. Add the test that would have caught both: grant the keys through a group and
   assert all three agree; deny them personally and assert all three agree again.

---

## 3. The Export Centre hands a teacher every ticket in the school

**High. The export contradicts the boundary the API is explicit about.**

### The defect

The API is deliberate that a view grant is *not* school-wide ticket access:

```python
# services/visibility.py:99-102
# A view grant is deliberately not school-wide ticket access: ticket
# conversations can contain personal or operationally sensitive details.
# Participants see their own threads, while same-tenant ticket managers
# are the only non-participants allowed into them.
```

`tickets.ticket.view` appears nowhere in `visible_tickets_qs`. But it is the gate
on the export dataset, whose base queryset is the whole tenant:

```python
# export_datasets.py:30-32
def _tickets(scope):
    return Ticket.all_objects.filter(tenant=scope.tenant)

# export_datasets.py:44-49
key="support.tickets", scope=DatasetScope.TENANT,
permission="tickets.ticket.view", row_cap=200_000,
```

and `tickets.ticket.view` is seeded to **every teacher**:

```python
# management/commands/seed_ticket_permissions.py:17-22
SCHOOL_ROLE_KEYS = ["school_admin", "branch_admin", "teacher"]
SCHOOL_DEFAULT_KEYS = {"tickets.ticket.view", "tickets.comment.post",
                       "tickets.attachment.create"}
```

### What actually happens

Tunde teaches JSS2 at Bright Star. He opens `GET /v1/support/tickets/4471/` -
Ngozi the bursar's ticket - and gets a `404`, correctly: he is not a participant
and holds no `manage`.

He then opens the Export Centre, picks **Support tickets**, sets the required
date window to the year, and downloads a spreadsheet containing every ticket the
school has ever raised: `ticket_number`, `title`, `category`, `priority`,
`status`, `source`, the three timestamps, and `assignee_email`. Ngozi's row is in
it, with the title "Payroll - my own salary line is wrong".

`requester_email` is marked `sensitive=True` (`export_datasets.py:73-75`) and is
withheld unless he also holds `exports.sensitive_field.export`, so he cannot see
*who* raised each one - but the titles are the payload, and the ticket numbers
give him a per-person count. `assignee_email` is **not** marked sensitive, so the
file also carries CX staff addresses out to a teacher.

### Why it exists

The dataset was scoped the way every other tenant dataset is scoped - "rows
belonging to this tenant" - which is right for invoices and wrong for a support
conversation, because tickets are the one tenant-owned record whose read
boundary is narrower than the tenant.

### The fix

1. **Scope the dataset's base to the caller, not the tenant.** `Dataset.base`
   receives a `scope`; the participant rule is
   `visible_tickets_qs(scope.user)` filtered by tenant, which is the same
   function the API and the console card already share.
2. If that is not possible in the catalogue's shape, **gate the dataset on
   `tickets.ticket.manage`** instead, which is the key that genuinely means
   school-wide ticket access, and note in the dataset description that it is a
   manager's export.
3. **Mark `assignee_email` sensitive**, to match `requester_email`.
4. Add the test: a teacher holding `tickets.ticket.view` and no participation
   must not be able to run this dataset, or must receive zero rows from it.

---

## 4. A school that has not gone live can raise a ticket and then cannot read the answer

**High. The one surface opened for pending tenants is opened half way.**

### The defect

```python
# views.py:52-57
# Filing a ticket is the one escalation route a school that has not gone
# live still has … Only the create action: the rest of the desk
# (lists, threads, attachments, assignment) opens at go-live like everything
# else.
pending_tenant_surface = ("create",)
```

`TenantSurfaceAllowed` refuses a PENDING tenant every action not named there and
raises `TenantNotLive` (`vs_rbac/permissions.py:140-157`). `retrieve`, `list` and
`comments` are not named.

### What actually happens

Bright Star is still PENDING; the go-live checklist will not accept payroll setup.
Bola files a ticket from the onboarding screen and gets a `201` with the full
ticket back. Ada at CX replies publicly: *"Please re-save the pay grade, then the
tick will stick."*

`notify_commented` sends Bola the reply by email
(`services/notifications.py:138-139`), and the in-app row resolves to
`/support/tickets/4471` (`vs_notifications/services/routing.py:27-29`). Bola
clicks it and the app refuses her with `TenantNotLive`. She cannot open the
ticket, cannot see the reply in the app, and **cannot reply back** - the comments
route is closed to her too. The escalation route the surface exists for is a
one-way door, and the school is stuck at exactly the moment support is most
needed.

### The fix

Open the caller's own threads, not the desk:

1. Add `retrieve` and `comments` to `pending_tenant_surface`. Both already resolve
   through `get_object()`, which restricts a pending-tenant user to tickets they
   requested - the boundary does not need to be re-invented.
2. Consider `attachments` on the same argument: a school blocked at go-live is
   usually blocked with a screenshot in hand.
3. Leave `list`, `assign`, `transition` and `audit` closed.
4. Add the test: a PENDING tenant's user files a ticket, then reads it and its
   comments.

---

## 5. A customer can attach files to an internal note, and can find out which notes are internal

**Medium. Two checks are made and the one that matters is not.**

### The defect

The view scopes the comment lookup to the ticket, and stops there:

```python
# views.py:253-258
comment_id = serializer.validated_data.get("comment_id")
if comment_id:
    comment = get_object_or_404(TicketComment, pk=comment_id, ticket=ticket)
```

and the service re-checks the same thing:

```python
# services/tickets.py:205-206
if comment is not None and comment.ticket_id != ticket.pk:
    raise ValidationError("Comment does not belong to this ticket.")
```

Neither asks `can_view_internal_notes(actor, ticket)`. Everywhere else in the
module that touches a comment does (`views.py:230-232`,
`serializers.py:102-113`, `views.py:285-291`).

### What actually happens

Ngozi raised TK-72608213. Ada added an internal note on it - comment id 9912 -
saying *"This school is three months behind on fees; check before promising a
fix."* Ngozi's own reply is 9908, and comment ids are sequential integers she can
see one of.

She posts a file with `comment_id: 9912`. It succeeds: `201`, with
`"comment_id": 9912` echoed back. Two consequences:

- **She has written into the support-only side channel.** Her file now hangs off
  an internal note, and the assignee is emailed about it.
- **She has learned that 9912 exists on her ticket** and is not a comment she can
  see - which is exactly the fact the visibility rules exist to withhold. A
  `201` for 9909-9912 and a `404` for 9913 maps the thread's hidden rows.

Her own view of the ticket then hides the file again (it inherits the note's
visibility, `serializers.py:110-112`), so she cannot retrieve what she uploaded.

### The fix

Add the check the rest of the module makes, at the choke point both callers share
- `add_attachment` in `services/tickets.py`:

```python
if comment is not None and comment.visibility == CommentVisibility.INTERNAL \
        and not can_view_internal_notes(actor, ticket):
    raise NotFound("No such comment.")
```

`NotFound`, not `PermissionDenied`, for the same reason `get_object` uses it. Add
the test: a requester posting a file against an internal note's id gets a `404`.

---

## 6. Branch is captured on every ticket and then never used

**Medium. And it breaks the multi-branch rule directly.**

### The defect

`Ticket.branch` exists, is indexed, is validated against the tenant in `clean()`,
is set from the actor at creation (`services/tickets.py:32-33,38`) and is
serialized as `branch` and `branch_name` (`serializers.py:62,79`).

Nothing else reads it. There is no `?branch=` filter (`views.py:89-125`), no
branch column in the dashboard (`views.py:334-347`), no branch term in
`visible_tickets_qs` (`services/visibility.py:82-106`), and no branch narrowing
anywhere in `services/visibility.py`. `_assignment_branch_q` / `ANY_BRANCH` are
used for the *assignee picker* and never for the rows.

Meanwhile `branch_admin` is seeded the same keys as `school_admin`, including
`tickets.ticket.manage` (`seed_ticket_permissions.py:23-27,133-135`), which is
the key that widens visibility to `Q(tenant=user.tenant)`.

### What actually happens

Bright Star has two branches, Ikeja and Yaba. Kemi is the branch admin for Yaba.
She opens the ticket list and sees Ikeja's tickets - including the Ikeja bursar's
ticket about a colleague's salary - because "branch admin" narrows nothing here.
Her dashboard counts them too.

And in the other direction: a school with two branches has no way to answer "how
many open tickets does Ikeja have", because the dimension is on the row and on no
filter.

### The fix

Decide what branch means on a ticket and then apply it consistently:

1. **Narrow `visible_tickets_qs` for a branch-pinned grant**, using
   `vs_rbac.scoping.visible_branch_ids` - the helper the rest of the platform
   uses for exactly this - and keep a null branch visible to everyone in the
   tenant, since null means "shared across the school".
2. **Add `?branch=` to the list**, and a branch breakdown to the dashboard.
3. **Show the control only where it changes meaning.** A school with one branch
   must not see a filter with one option or a column repeating one value.
4. Test both shapes: a one-branch school and a two-branch school.

---

## 7. `Ticket.clean()` never runs on the path that creates tickets

**Medium. The invariant is tested where it is not enforced.**

### The defect

`Ticket.clean()` (`models.py:126-133`) holds three rules: the requester belongs to
the tenant, the branch belongs to the tenant, and an `ASSIGNED` ticket has an
assignee.

`update_ticket`, `assign_ticket` and `transition_ticket` all call `full_clean()`
before saving (`services/tickets.py:79,113,148`). `create_ticket` does not:

```python
# services/tickets.py:44-54
with transaction.atomic():
    ticket = Ticket.objects.create(
        title=title, …, requester=actor, tenant=actor.tenant, branch=branch, …)
```

`TicketBranchTenantGuardTests` (`tests.py:725-786`) asserts all four cases by
calling `.clean()` directly, so the suite is green and the create path is
unguarded.

### What actually happens

It is inert today, because `branch` comes from `actor.branch` and `tenant` from
`actor.tenant`, which agree. It stops being inert the moment they do not: a user
moved between tenants whose `branch` was not cleared, a branch retargeted by a
migration (this module has already had one -
`0005_retarget_branch_to_vs_tenants.py`), or any future caller that passes
`branch=` explicitly - the parameter already exists in the signature
(`services/tickets.py:37`) and is simply never used by the API.

The first such ticket is written with a cross-tenant branch and nothing objects.
Then the *next* edit to that ticket fails: `update_ticket` calls `full_clean()`,
which now raises on a field the editor never touched, and a bursar fixing a typo
in a title gets "Ticket branch must belong to the selected tenant."

### The fix

Call `ticket.full_clean()` before the insert in `create_ticket`, the way the other
three services do, and add the guard test through the service rather than only
against the model.

---

## 8. Comments, attachments and audit rows are returned whole, however many there are

**Medium.**

Four unpaginated lists:

| Where | Code |
|---|---|
| `GET /tickets/<pk>/comments/` | `views.py:226-236` |
| `comments` inside the ticket detail payload | `serializers.py:102-106` |
| `attachments` inside the same payload | `serializers.py:108-113` |
| `GET /tickets/<pk>/audit/` | `views.py:307-314` |

The list route is paginated (`XVSPagination`, `apps/settings/base.py:66-67`);
none of these is.

A long-running escalation - a data import gone wrong, worked over three weeks -
accumulates a hundred replies, forty screenshots and an audit row per action.
Opening it fetches all of it, including `before_data` and `after_data` blobs, in
one response, on every refresh. There is no cap, and `row_cap` on the export does
not apply here.

**Fix:** paginate the comments and audit routes; cap the counts embedded in the
detail payload (the newest N, with a `has_more`), the way a thread view actually
reads.

---

## 9. Three list filters answer 500 on a value the caller can choose

**Medium.**

```python
# views.py:101-114
if value := params.get("assignee"):
    qs = qs.filter(assignee_id=value) if value != "me" else …
if value := params.get("requester"):
    qs = qs.filter(requester_id=value)
if value := params.get("school"):
    qs = qs.filter(tenant__school_profile__id=value)
```

All three are raw strings. `?assignee=abc` makes the ORM raise
`ValueError: Field 'id' expected a number but got 'abc'`, which is not one of the
types `custom_exception_handler` intercepts (`core/exceptions.py:91-195`), so it
falls to the final branch: `500`, code `SERVER_ERROR`, and a logged exception.

The date filters are fine by contrast: `created_at__date__gte="banana"` raises
Django's `ValidationError`, which the handler turns into a `400`
(`core/exceptions.py:116-122`). That asymmetry is the tell - somebody thought
about the dates and not about the ids.

**Fix:** coerce the three id filters with a small helper that answers `400` on a
non-integer, or move the whole filter set into a serializer. A frontend that
sends `?assignee=` (empty) is protected by the walrus, but `?assignee=null` -
which a frontend does send - is a 500.

---

## 10. Ticket creation and file upload are unthrottled

**Medium.**

`DEFAULT_THROTTLE_CLASSES` is `ScopedRateThrottle` alone
(`apps/settings/base.py:68-70`), which does nothing on a view that declares no
`throttle_scope`. No view in `vs_tickets` declares one.

Ticket creation is the module's most open surface: no permission key
(`constants.py:70-72`), and reachable by a tenant that has not gone live
(`views.py:57`). Each creation fans out to `support_recipients()` - every CX
agent, in-app and by email (`services/notifications.py:87-94`). The attachment
route accepts 10 MB per file (`core/uploads.py:40`) into database-backed storage,
with no per-ticket or per-user cap on how many.

One compromised or careless school account can therefore fill every CX agent's
inbox and grow the `StoredFile` table without limit, and nothing rate-limits
either.

**Fix:** a `throttle_scope` on create and on attachments, with rates in
`DEFAULT_THROTTLE_RATES` alongside `login` and `activation`; optionally a cap on
attachments per ticket.

---

## 11. The sanitised filename is computed and thrown away

**Medium.**

`validate_upload` returns `(safe_name, content_type)` and documents the first
value as *"stripped of characters that would break a Content-Disposition header
and truncated to the 255 the model columns hold"* (`core/uploads.py:104-107`).

`add_attachment` keeps only the second, and stores the raw name instead:

```python
# services/tickets.py:219-222
_, verified_content_type = validate_upload(file_obj, …)
…
original_filename=getattr(file_obj, "name", ""),
```

The serializer discards it too (`serializers.py:215-227`).

The name then goes into a 255-character column, into the audit summary
(`services/tickets.py:230-232`) and into the `Content-Disposition` header
(`views.py:299-304`). Django escapes the header, so this is not header injection
- but a 300-character filename is a `DataError` on PostgreSQL, which is neither
`IntegrityError` nor `ValidationError`, so the caller gets a `500` on a file the
validator had already inspected and passed.

**Fix:** use what the helper returns -
`safe_name, verified_content_type = validate_upload(...)` and
`original_filename=safe_name`. One line, and it is the line the helper exists for.

---

## 12. Two seeded permission keys are checked nowhere

**Low, but it misleads every reader of the seeder.**

`tickets.ticket.view` and `tickets.report.view` are created, described, and
attached to school prebuilts (`seed_ticket_permissions.py:18-27,30-42`).

- **`tickets.ticket.view`** appears in no view, no service and no predicate in
  this module. Its only real effect is outside it: the export dataset's gate
  (§3) and the admin console card (`vs_admin_console/overview.py:514-516`).
- **`tickets.report.view`** appears nowhere at all. The dashboard declares no
  `rbac_permission` (`views.py:318-327`), so every authenticated account gets it
  - correctly scoped by `visible_tickets_qs`, but not by the key that was seeded
  to gate it.

Both read as boundaries and are not. §3 is what happens when a reader trusts
`view` to mean what its name says.

**Fix:** either wire them (`report.view` on the dashboard; `view` as the
non-participant read key, if that is what it should mean) or delete them from
`TICKET_RESOURCES` and stop seeding them. Do not leave a key whose only effect is
in another app.

---

## 13. The ticket detail payload re-queries once per comment and once per file

**Low.**

```python
# serializers.py:103
comments = obj.comments.select_related("author").prefetch_related("attachments")
# serializers.py:109
attachments = obj.attachments.select_related("uploaded_by")
```

`TicketUserSerializer` reads `tenant.kind` (`serializers.py:17`), and neither
join includes `__tenant`. So rendering a ticket with 30 comments and 12
attachments costs 42 extra queries.

The comments *route* gets this right - `select_related("author__tenant")` and
`prefetch_related("attachments__uploaded_by__tenant")` (`views.py:227-228`) - and
so does the list queryset (`services/visibility.py:85-87`), where the
`select_related` chain carries a comment explaining why. Only the detail
serializer was missed.

**Fix:** `select_related("author__tenant")` and
`select_related("uploaded_by__tenant")`, plus
`prefetch_related("attachments__uploaded_by__tenant")` on the comments.

---

## 14. Assignment tells a caller whether a user id exists

**Low.**

```python
# serializers.py:194-199
if not User.objects.filter(pk=value).exists():
    raise serializers.ValidationError("No such user.")
```

versus, for an id that does exist but is not support-capable:

```python
# services/tickets.py:98-101
raise ValidationError({"assignee_id": [
    "Tickets can only be assigned to active staff who can manage tickets."]})
```

Two different `400`s. A school admin holding `tickets.ticket.assign` can walk the
integer space and learn which user ids are real across the whole platform - the
existence check is unscoped by tenant.

The same route also hands them the CX roster: `eligible-assignees` returns names,
email addresses and role titles for every support user
(`views.py:198-207`, `serializers.py:15-24`). That is not a defect on its own -
they must pick somebody - but it is worth knowing that the key is seeded to
platform roles only today, and a school role that gains it gets the platform's
staff directory.

Also here: `views.py:190` re-fetches with a bare `User.objects.get(pk=assignee_id)`,
so a row deleted between validation and fetch is a `500`.

**Fix:** answer the same message for both cases, and resolve the assignee from
`eligible_support_users_qs()` (once §2 makes that the canonical set) rather than
from `User.objects`.

---

## 15. The word "school" is in an engine app

**Low as a defect, direct as a rule violation.** `CLAUDE.md`: outside
`apps/schools/`, say **tenant** - in parameter names, serializer fields,
constants, variables and JSON body keys alike.

Three occurrences:

- **`?school=<id>`** on the ticket list (`views.py:111-114`), joining
  `tenant__school_profile__id`. Its own comment calls it a legacy display filter.
- **`Ticket.school` and `Ticket.school_id`** (`models.py:142-148`), reading
  `tenant.school_profile`.
- **`_translate_tickets`** reports `school` as an unmapped export filter
  (`export_datasets.py:133`), which is correct behaviour for a parameter that
  should not exist.

The app is otherwise clean: it imports nothing from `apps/schools/`, and
`test_vs_tickets_does_not_import_the_school_package` (`tests.py:865-882`) pins
that. The leak is through vocabulary and an ORM string path, not an import, so
the test cannot see it. On `vs_health` (VIGIL) or any future domain,
`tenant__school_profile` matches nothing and `Ticket.school` is always `None`.

**Fix:** rename the filter to `?tenant=` … except that name is taken by the
context assertion, which is itself the reason the filter exists. The honest fix
is to delete `?school=` (the list is already scoped to the caller's tenant, so it
is only meaningful to CX support, who should filter by tenant slug) and to drop
the two properties, replacing any caller with `ticket.tenant`.

---

## 16. Smaller defects and dead code

**Low, individually.**

1. **`TicketSequence` (`models.py:27-45`) is dead** - superseded by
   `vs_tenants.TenantDocumentSequence`, retained to avoid a destructive
   migration, read by nothing.
2. **An internal note written by the assignee notifies nobody.**
   `notify_commented` sends internal notes to `[ticket.assignee]`
   (`services/notifications.py:136-137`) and `_unique_recipients` then removes
   the actor (19-28). Second-line notes reach no other agent.
3. **A ticket can be left `IN_PROGRESS` with no owner.** `assign_ticket` returns a
   ticket to `OPEN` only when the status is exactly `ASSIGNED`
   (`services/tickets.py:107-109`), so clearing the assignee of an in-progress
   ticket leaves it live, ownerless, and un-notified.
4. **`route_pattern` refuses any digit** (`serializers.py:171`), so `/v1/...` and
   any route with a number in a segment name can never be recorded. The intent -
   prove record ids were stripped - is right; the test is broader than the
   intent. Prefer rejecting a digit-only path segment.
5. **`register_context_choice_field(description=...)` is accepted and discarded**
   (`context.py:40-77`), and no endpoint exposes the allowlist, so the frontend
   must hardcode the twenty `product_area` values and every registered
   vocabulary.
6. **`validate_upload` runs twice per upload** - serializer and service
   (`serializers.py:215-227`, `services/tickets.py:219-222`) - with different
   wording for the type error, so the message a caller sees depends on which one
   fires first.
7. **`TicketSerializer.Meta.read_only_fields` omits `title`, `category`,
   `priority` and `branch`** (`serializers.py:82-85`). Harmless today because
   `create` and `update` use their own serializers, but it advertises writability
   the class does not grant, and would become real the moment somebody used
   `TicketSerializer` for a write.
8. **`sees_internal_notes_by_default` asks about the caller's tenant**
   (`services/visibility.py:181-184`) while `can_view_internal_notes` asks about
   the ticket's (169-177). The same question, answered against two scopes, for
   the list counts and the detail payload respectively.
9. **`updated_at` is serialized on comments** (`serializers.py:55`) and can never
   differ from `created_at` - there is no edit path.
10. **`description` and comment bodies have no length ceiling** (`models.py:52`,
    `serializers.py:207`), so a paste of an entire log file is accepted, stored,
    copied into the notification context (`services/notifications.py:154-156`)
    and emailed.
11. **A failed dispatch is a `logger.warning` and nothing else**
    (`services/notifications.py:78-82`) - no retry, no dead-letter, no metric.
12. **Reading is never audited.** Downloading another tenant's attachment, or
    reading a ticket thread, writes nothing to `TicketAuditLog` or `vs_audit` -
    which for a cross-tenant support desk is the read most worth recording.
13. **`ticket_number` is not parseable.** `TK-<tenant_id><YYMMDD><n>` with an
    unpadded `n` and a variable-width tenant id (`vs_tenants/numbering.py:39`)
    is unique but cannot be split back into its parts, and does not sort
    chronologically as text.

---

## What the test suite does not know

The suite is green, so every item above is something it does not catch. The four
gaps that matter most:

1. **No test sets `request.tenant`.** Every API test uses
   `force_authenticate`, which skips `TenantJWTAuthentication`, so the ambient
   `TenantAwareManager` is never active and the `?tenant=` assertion is never
   exercised. §1 lives entirely in that gap.
2. **No test asserts what `NotificationService.send` is called with**, except the
   recipient set in one case (`tests.py:328-353`). The `tenant=` argument is
   still not inspected here; §1 is covered instead on the engine side, where
   ownership is decided (`vs_notifications.tests.NotificationOwnershipTests`).
3. **No test grants a ticket key through a permission group or denies one through
   a personal override**, which is what §2 needs.
4. **No test runs the `support.tickets` dataset** and compares its rows with what
   the API shows the same caller, which is what §3 needs.

Then, in decreasing order: no multi-branch school anywhere in the file (§6), no
test of the pending-tenant surface in either direction (§4), no attempt to attach
to a comment the caller cannot see (§5), and no malformed filter values (§9).

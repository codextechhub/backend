# school_provisioning

Everything that has to happen around a new school before anyone can use it:
turning a `ContactInfo` into a real invited `User` with a role, giving the school
a set of books in the finance ledger, handing off to the onboarding control room,
the operator command that repairs a school whose books never arrived, and the one
destructive operation this app exposes.

The school record is `school_records`; sites are `school_branches`; plans and
modules are `school_packages_entitlements`.

Routes covered by this slice, mounted at `/v1/i/` (`apps/urls.py:23`):
`<slug>/reset-config/`. Everything else here is a service or a management
command with no HTTP surface.

Findings for the whole module are collected in
**`error/schools/school_code_issues.md`**; §8 below points at the ones belonging
here rather than repeating them.

---

## 1. What it is (and what it is NOT)

- **Three things are provisioned at school creation, and they fail differently.**
  Admin accounts, books and the onboarding checklist all run inside the school
  creation transaction; all three swallow their own failures; none of them can
  cost you the school. That last property is the design goal, and it is stated at
  each site (`services/admin_provisioning.py:43-46`,
  `services/books.py:16-20`, `serializers.py:1206-1227`).
- **Swallowing a failure means the API still answers 201.** A school created
  without an administrator, without books or without a checklist looks exactly
  like a healthy one from the outside. Books and the checklist have repair paths;
  the administrator does not - see `school_code_issues` §2.
- **An administrator without a role is refused on purpose.** `provision_admin_user`
  raises rather than creating a user it knows cannot do anything: they would
  receive the invitation, activate it, and then be able to do nothing
  (`services/admin_provisioning.py:105-116`).
- **"Already exists" means "already exists in this school's tenant".** The
  idempotency probe is scoped to the tenant, and the scope is the whole point:
  unscoped, it meant "exists anywhere on the platform" and handed the new
  school's admin link the *other* school's account
  (`services/admin_provisioning.py:70-85`).
- **There is no persona.** The service used to take a `user_type` of
  `SCHOOL_ADMIN` or `BRANCH_ADMIN`, then `STAFF`. The reach is `branch` (a
  branch, or `None` for the whole school) and the authority is `role`; a persona
  mirroring two other arguments is a third copy of the truth waiting to disagree
  with them (`services/admin_provisioning.py:47-53`).
- **Every school gets books, entitled to finance or not.** Adding them later
  means going back to repair every school created before that point
  (`serializers.py:1207-1211`).
- **The nested `transaction.atomic()` in the books service is the whole point of
  the function.** Catching the exception is not enough: a *database* failure
  inside the outer block leaves the transaction aborted, so every later statement
  fails too and the outer commit takes the school, its tenant, its admin and its
  branches down with it. The inner block opens a savepoint
  (`services/books.py:79-90`).
- **There is deliberately no endpoint for provisioning books.** It is gated on
  `finance.entity.create`, which a School Admin does not hold and should not: an
  entity becomes the tenant of its own documents and numbering, and a school that
  could mint one at will could mint a second and make the primary-books lookup
  ambiguous (`management/commands/provision_school_books.py:8-12`).
- **`reset-config` is not a reset.** Its docstring says branding, modules and
  localization; it deletes the `SchoolBranding` row and nothing else
  (`serializers.py:1497-1524`).
- **The confirmation token confirms nothing.** It is required to be non-empty and
  is never compared against anything (`serializers.py:1509-1511`).

## 2. Domain model

This slice owns no models. It writes rows belonging to five other apps:

| Model | App | Written by |
|---|---|---|
| `User` | `vs_user` | `provision_admin_user` |
| `UserInvitation` | `vs_user` | `InvitationService.create` |
| `TenantUserRoleAssignment` | `vs_rbac` | `provision_admin_user` |
| `TenantRoleTemplate` (+ permissions) | `vs_rbac` | `provision_role_from_prebuilt`, called from both create serializers |
| `LedgerEntity` and its chart | `vs_finance` | `provision_books` |
| `CapabilityEntitlement` | `vs_config` | `set_entitlement` (see `school_packages_entitlements`) |
| the onboarding checklist | `schools.vs_onboarding` | `provision_onboarding_for_school` |

The two rows it does mutate are `SchoolPrimaryAdmin` and `BranchPrimaryAdmin`
(`school_records` §2, `school_branches` §2): it flips `invite_status` to `SENT`
and stamps `invite_sent_at`.

`InviteStatus.FAILED` exists as a choice and is written by nothing, because
every failure path returns `None` without touching the link
(`services/admin_provisioning.py:170-178`).

## 3. Endpoint map

| Route | Verb | Gate | Body actually read |
|---|---|---|---|
| `/v1/i/<slug>/reset-config/` | POST | `IsVisionSuperAdmin` **only** | `confirmation_token` (required, must equal the school's slug), `reason` (optional) |

That permission list is the whole gate (`views/ops.py:45`). It does **not**
include `IsAuthenticatedAndActive`, so the account-status checks (SUSPENDED /
LOCKED / DEACTIVATED) never run, and `TenantSurfaceAllowed` never runs either -
and DRF's `permission_classes` *replaces* `DEFAULT_PERMISSION_CLASSES` rather
than adding to them (`vs_rbac/permissions.py:123-131`). There is no
`rbac_permission` on the view at all. See `school_code_issues` §5.

`_SchoolOpBaseView` (`views/ops.py:23-40`) is written as a base for several
operations; `SchoolResetConfigView` is its only subclass. It resolves the school
by slug from an unfenced `School.objects.all()`, runs the serializer, refreshes
the row and answers with the full `SchoolDetailSerializer` payload.

Everything else in this slice has no route:

| Entry point | Called from |
|---|---|
| `provision_admin_user` | `BranchCreateSerializer.create`, `SchoolCreateSerializer.create` (twice: school admin, then each branch admin) |
| `provision_books_for_school` | `SchoolCreateSerializer.create` step 6, and the management command |
| `derive_entity_code` | the above, and the command's `--dry-run` output |
| `provision_onboarding_for_school` | `SchoolCreateSerializer.create` step 7 |
| `manage.py provision_school_books` | an operator |

## 4. Lifecycle / state machine

### One administrator, from contact to account

```
  ContactInfo created                       (always, unconditionally)
        │
        ▼
  SchoolPrimaryAdmin / BranchPrimaryAdmin at QUEUED, invite_queued_at stamped
        │
        ▼
  provision_admin_user, inside its own savepoint
        │
        ├── a User already exists at this address IN THIS TENANT
        │        └─► link stamped SENT, no user created, no email sent,
        │            existing user returned, warning logged
        │
        ├── no role template resolves for `role`
        │        └─► ValueError ─► savepoint rolls back ─► caught ─► returns None
        │            link stays QUEUED. School creation continues. API answers 201.
        │
        ├── any other failure (duplicate email race, invitation, broker)
        │        └─► same: rolled back, logged at ERROR, returns None
        │
        └── success
                 User (PENDING, is_active=False)
                 TenantUserRoleAssignment (branch = role_obj.branch)
                 UserInvitation
                 send_invitation_email_task queued
                 link stamped SENT, invite_sent_at stamped
```

The one branch that is neither success nor failure - "somebody at this address
already administers this school" - is a warning, not an error, and it is what
makes the whole path safe to reach twice.

`SchoolCreateSerializer` adds a fourth case of its own for branch admins: if the
branch admin's address equals the school admin's, the link is stamped SENT
directly with no second user and no second email, because it is the same person
(`serializers.py:1119-1123`).

### Books

```
  provision_books_for_school(school)
        │
        ▼  savepoint
  derive_entity_code(school)  ─►  vs_finance.provision_books(reuse_existing=True)
        │                                   │
     success                             failure
        │                                   │
   LedgerEntity                    savepoint rolled back
   returned                        logger.exception with the repair command
                                   returns None
```

`reuse_existing=True` is what makes the call safe to reach twice - at creation,
and again from the backfill command - without ever minting a second entity
(`services/books.py:104-106`).

### The repair command

```
manage.py provision_school_books [--school <slug>]... [--dry-run]
        │
        ▼
  for each school, ordered by slug:
        primary_entity_for(school.tenant) is not None ─► skipped
        --dry-run                                     ─► reports the code it would use
        otherwise                                     ─► provision_books_for_school
                                                          None ─► failed (stderr)
                                                          else ─► provisioned
        │
        ▼
  "provisioned N, skipped M, failed K."   (styled WARNING if any failed)
```

An unknown slug passed to `--school` produces a warning on stderr and is
otherwise ignored (`provision_school_books.py:60-62`).

## 5. Derivations

### The ledger entity code

```python
stem = re.sub(r"[^A-Z0-9]", "", (school.slug or school.name or "").upper())
stem = stem[:16] or "SCHOOL"
candidates = [stem]
candidates += [stem[:16 - len(str(n))] + str(n) for n in range(2, 100)]
candidates.append(f"SCH{school.pk}"[:16])
taken = set(LedgerEntity.objects.filter(code__in=candidates).values_list("code", flat=True))
for candidate in candidates:
    if candidate not in taken:
        return candidate
return candidates[-1]
```

`services/books.py:35-69`. `LedgerEntity.code` is globally unique across every
tenant and only sixteen characters, while a school slug may be eighty - so the
stem is stripped to letters and digits, truncated, and then numbered on
collision.

Two schools with similar names is ordinary rather than exceptional, so a
collision must never raise: the ninety-eight numbered candidates are followed by
one built from the school's own primary key, which is unique by construction.
The whole list is probed in **one** query rather than one probe per attempt.

Worth noting: the `SCH<pk>` fallback is only reached when all ninety-nine earlier
candidates are taken, and the final `return candidates[-1]` hands back that same
pk-based code, so the unique constraint is the final guard.

### Splitting a name

```python
parts = full_name.strip().split(None, 1)
return (parts[0], parts[1]) if len(parts) == 2 else (parts[0] if parts else "", "")
```

`services/admin_provisioning.py:24-27`. `"Ada Okoye"` → `("Ada", "Okoye")`;
`"Ada"` → `("Ada", "")`; `"Ada Nkechi Okoye"` → `("Ada", "Nkechi Okoye")`. A
single-word name is handled; an empty one yields two blanks.

### Which branch the assignment lands on

```python
user     = User.objects.create_user(..., branch=branch, ...)
TenantUserRoleAssignment.objects.create(..., branch=role_obj.branch, ...)
```

`services/admin_provisioning.py:127`, `:138`. The **user's** home posting is the
`branch` argument; the **grant's** scope is the role template's branch. For a
school-level admin both are `None`. For a branch admin, `branch` is the branch
and `role_obj.branch` is the same branch, because
`provision_role_from_prebuilt(tenant=…, branch=branch, prebuilt_key="branch_admin")`
pinned the template to it (`serializers.py:536-541`, `1087-1092`).

That is the one place in the repo where `TenantRoleTemplate.branch` actually
influences anything - and it influences the *assignment's* branch rather than
being read at evaluation time (`error/rbac/rbac_code_issues.md` §10).

### The reset

```python
branding = SchoolBranding.objects.filter(school=school).first()
before_data = {"logo": str(branding.logo) if branding and branding.logo else ""}
SchoolBranding.objects.filter(school=school).delete()
```

`serializers.py:1517-1524`. `before_data` is built by hand rather than through
`AuditDiffService.model_instance_to_dict`, and the reason is recorded: `logo` is
an `ImageField`, `model_to_dict` hands back the `FieldFile` itself, and a
`FieldFile` in a `JSONField` raises inside `emit_audit_event` - which swallows
its own failures, so the whole event would vanish.

The `confirmation_token` is read, stripped and compared, case-insensitively,
to the school's own slug. Anything else is a 400 on the `confirmation_token`
field naming the address the caller has to type. It used to be checked for
emptiness and then discarded, so `"x"` reset whichever school the URL named -
an operator with Bright Star and Greenfield both open reset the wrong one and
nothing in the body had ever said which school it was aimed at.

## 6. What writing writes

### `provision_admin_user`, on the success path

| Row | Detail |
|---|---|
| `User` | `status=PENDING`, `is_active=False`, `is_staff=False`, `password=None`, `role=role_obj.name` (the display name, not the key), `tenant`, `branch`, `invited_by` |
| `TenantUserRoleAssignment` | `tenant`, `user`, `role=role_obj`, `branch=role_obj.branch`, `assigned_by=invited_by`. Written on the existing-user path too, via `get_or_create`, so one person named as admin of two branches in one request is granted at both. |
| `UserInvitation` | via `InvitationService.create(user=user, invited_by=invited_by or user)` |
| A queued Celery task | `send_invitation_email_task.delay(activation_key, _job_owner_id=…, _job_label=…, _job_kind="email", _job_notify=False)` |
| The admin link | `invite_status=SENT`, `invite_sent_at=now` |

`_job_notify=False` is fan-out plumbing: one bell notification per invited row is
spam (`services/admin_provisioning.py:151-153`). A provisioning run with no actor
stays a system row (`owner=None`).

Note the account is created through `User.objects.create_user` directly, not
through `vs_user`'s `UserCreationService` - so none of that service's own
guards, its workflow submission or its `AuthEventLog` entry apply here.

### `provision_books_for_school`

One `LedgerEntity` plus whatever `vs_finance.provision_books` creates behind it
(the chart of accounts, the numbering series). `base_currency` comes from
`school.currency or None`, `kind=LedgerEntity.Kind.TENANT`.

### `POST <slug>/reset-config/`

| Row | Detail |
|---|---|
| `SchoolBranding` | deleted, if present |
| Audit | one `SCHOOL/CONFIG_CHANGED` event at `WARNING`, keyed on `str(school.pk)`, with `metadata={"reason": …}` |

Nothing else is touched: not `CapabilityEntitlement`, not
`SchoolPackageSetup`, not `term_structure`, not `currency`, not the branches.

The audit gap this closed is worth recording, because the same gap existed on
the school update path: both read `actor_id` out of the context and dropped it on
the floor, so a super admin wiping a school's branding left no record of it at
all (`serializers.py:1526-1529`). The actor is resolved with `.get()` and **no
default**, deliberately: `actor_user` is a FK, a string there raises inside
`emit_audit_event`, and that helper swallows its own failures - so a defaulted
`"system"` string would have meant no event at all rather than a
system-attributed one (`serializers.py:1372-1379`).

## 7. Worked example

**1. The happy path.** Greenfield College is created with Ada Okoye as school
admin. `provision_role_from_prebuilt` mints Greenfield's `school_admin` role
template from the seeded prebuilt and copies its default permissions.
`provision_admin_user` finds no existing user at `ada@greenfield.test` in
Greenfield's tenant, resolves `role_obj` by key, creates a PENDING inactive
`User`, writes the role assignment with `branch=None` (the template is
whole-tenant), mints an invitation and queues the email. The link flips to SENT.
Ada gets an email, activates, and can administer the school.

**2. The path this app actually takes when a prebuilt is missing.** If
`seed_prebuilt_role_templates` has not been run - a fresh environment, a partial
restore, a test fixture - `provision_role_from_prebuilt` returns `None`
(`vs_rbac/services.py:68-70`). The caller then passes
`role=branch_admin_role.key if branch_admin_role else ""`
(`serializers.py:563`, `1130`), so `role` is the **empty string**.

Inside `provision_admin_user`, `role_obj` is `None` because
`if role else None` short-circuits on the blank, and the guard fires:

```
ValueError: Refusing to provision head@greenfield.test without a role assignment.
Expected TenantRoleTemplate key='' on tenant greenfield.
```

That message is real: it appears in this app's own test-suite output. The
savepoint rolls back, the outer `except Exception` catches it, logs at ERROR and
returns `None`. School creation continues through books, checklist and audit,
and the endpoint answers **201 Created**.

Greenfield now exists, with a tenant, branches, entitlements, books and a
checklist - and no account anybody can sign in with. The `SchoolPrimaryAdmin`
row sits at QUEUED. There is no endpoint to see that, no endpoint to correct the
address, and no endpoint to re-send the invitation
(`school_code_issues` §2 and §13).

**3. Books fail instead.** Suppose `provision_books` hits a database error.
Without the inner `transaction.atomic()`, the outer transaction would be aborted
and the commit would take the school, its tenant, Ada's account and the branches
with it. With it, only the books work rolls back; the log names the school and
the exact repair command:

```
Could not provision a set of books for school greenfield (tenant greenfield).
The school was created without books; run
'manage.py provision_school_books --school greenfield' to repair it.
```

The distinction matters, and `tests_books.py` respects it: a plain Python
exception raised and caught leaves the connection healthy, so a test that forces
one would pass against a bare `try`/`except` and prove nothing. The test forces a
failed *statement* instead (`services/books.py:86-90`).

**4. The repair.** An operator runs
`manage.py provision_school_books --dry-run`, sees
`+ greenfield: would provision books as GREENFIELD in NGN.`, then runs it for
real. `primary_entity_for` returns `None`, so the school is not skipped;
`derive_entity_code` probes ninety-nine candidates in one query and finds
`GREENFIELD` free. The summary reads `provisioned 1, skipped 46, failed 0.`

**5. The destructive operation.** Greenfield's logo was uploaded to the wrong
school. A Vision super admin calls:

```http
POST /v1/i/greenfield/reset-config/?tenant=codex
{"confirmation_token": "greenfield", "reason": "Logo uploaded to the wrong school."}
```

The token has to be `greenfield`, the slug in the URL; `"x"` is a 400 and so is
`bright-star`, which is the point of it. The `SchoolBranding` row is deleted, a WARNING audit event is written naming the
actor and the reason, and the full school detail comes back. The modules and the
localization the docstring promises to reset are untouched, which in this case is
lucky.

**6. If that super admin's account had been suspended** an hour earlier, the call
would still succeed: `IsVisionSuperAdmin` checks for an ACTIVE
`xvs_super_admin` role assignment and says nothing about the account's own
status, and `IsAuthenticatedAndActive` - the class that reads it - is not on this
view (`school_code_issues` §5).

## 8. Gotchas / known limitations

Recorded in full in **`error/schools/school_code_issues.md`**. The items
belonging to this slice:

| # in that file | One line |
|---|---|
| §2 | **Confirmed by execution.** A school can be created that nobody can sign in to, and the API reports 201 |
| §4 | **Confirmed by execution.** "Reset configuration" accepts any non-empty confirmation token and resets only branding, while its docstring promises three things |
| §5 | The reset endpoint carries neither the account-status gate, the pending-tenant surface gate, nor a permission key |
| §13 | There is no endpoint to view, correct or re-send a primary-admin invitation, so the failure in §2 has no remedy short of a shell |
| §16 | `ContactInfo` rows are created unconditionally, so re-inviting the same person accumulates rows |
| §17 | `InviteStatus.FAILED` is never written by any path, so a failed invite is indistinguishable from one still queued |

Design choices worth stating as choices - the failure isolation here is careful
and mostly right:

- **The nested savepoint in the books service** (`services/books.py:79-90`) is
  the difference between "the school has no books" and "there is no school", and
  the docstring explains why a bare `try`/`except` is not equivalent.
- **`reuse_existing=True`** (`services/books.py:104-106`) makes the call safe to
  reach from creation and from the backfill command.
- **The tenant-scoped existence probe** (`services/admin_provisioning.py:70-85`)
  closed a real cross-tenant leak: an unscoped probe handed Greenfield's admin
  link Bright Star's account, with no exception and no error log.
- **Refusing to create a roleless administrator**
  (`services/admin_provisioning.py:105-116`) is the right call in isolation; it
  is §2 only because the caller then swallows the refusal.
- **No endpoint for provisioning books**
  (`management/commands/provision_school_books.py:8-12`) is deliberate, and the
  reasoning about a second ambiguous entity is sound.
- **`.get()` with no default for the audit actor**
  (`serializers.py:1372-1379`) - a `"system"` string default would have silently
  cost the event.

## 9. Permissions & tenant isolation

- **`reset-config` is the weakest-gated write in the app.**
  `permission_classes = [IsVisionSuperAdmin]` and nothing else
  (`views/ops.py:45`). Compare every other write in this app, which carries
  `IsAuthenticatedAndActive & HasRBACPermission` plus a named key.
- **`IsVisionSuperAdmin` is `is_vision_super_admin(request.user)`**, which as
  `error/rbac/rbac_code_issues.md` §1 records does not check the tenant's kind -
  so a counterfeit `xvs_super_admin` role in a school tenant opens this endpoint
  too, for **any** school's slug, because `_SchoolOpBaseView.queryset` is
  `School.objects.all()`.
- **`provision_admin_user` is not reachable over HTTP** except as a side effect
  of school or branch creation, both of which take PLATFORM-scoped keys.
- **The account it creates is deliberately inert**: `is_active=False`,
  `status=PENDING`, `password=None`. Authority arrives only when the invitation
  is activated.
- **The role assignment goes through `TenantUserRoleAssignment.objects`**, which
  is `vs_rbac`'s `ScopeGuardedManager`, so the platform/tenant scope guard runs
  on this path as it does everywhere else.
- **`provision_books_for_school` crosses into `vs_finance` and carries nothing
  school-shaped with it**: a tenant, a name, a code, a currency and a kind. The
  module docstring is explicit that this is the kind of translation that will
  move into the FAL when it arrives (`services/books.py:10-14`).
- **The management command has no permission layer at all**, correctly - shell
  access is the authorisation.

## 10. Code map

| File | What lives there |
|---|---|
| `schools/vs_schools/services/admin_provisioning.py:24-27` | `_split_name` |
| `schools/vs_schools/services/admin_provisioning.py:32-178` | `provision_admin_user`, the tenant-scoped probe, the roleless refusal, the catch-all |
| `schools/vs_schools/services/books.py:35-69` | `derive_entity_code` |
| `schools/vs_schools/services/books.py:72-116` | `provision_books_for_school` and its savepoint |
| `schools/vs_schools/management/commands/provision_school_books.py` | The backfill command |
| `schools/vs_schools/views/ops.py` | `ActorContextMixin`, `_SchoolOpBaseView`, `SchoolResetConfigView` |
| `schools/vs_schools/serializers.py:1497-1550` | `SchoolResetConfigSerializer` |
| `schools/vs_schools/serializers.py:531-565` | The branch create path's call into provisioning |
| `schools/vs_schools/serializers.py:1035-1132` | The school create path's two calls into provisioning |
| `schools/vs_schools/serializers.py:1206-1227` | Books and onboarding, both best effort |
| `vs_rbac/services.py:54-111` | `provision_role_from_prebuilt`, which returns `None` for a missing prebuilt |
| `vs_finance/provisioning.py` | `provision_books`, `primary_entity_for` |
| `schools/vs_onboarding/services/provisioning.py` | `provision_onboarding_for_school` |
| `vs_user/services/invitation.py` | `InvitationService.create` |
| `vs_user/tasks.py` | `send_invitation_email_task` |

## 11. Test coverage & gaps

Module baseline: see `school_code_issues` for the exact `Ran N tests` line.

Covered for this slice:

- `tests_books.py` (406 lines) - `derive_entity_code`'s stem, truncation,
  numbering and pk fallback; `reuse_existing`; and, importantly, that a failed
  *statement* inside the books service does not abort the school creation
  transaction. That test is the reason the savepoint is trustworthy.
- `tests.py` - the happy path of admin provisioning as part of school creation,
  and the same-address branch-admin shortcut.
- The suite's own output shows the roleless-refusal path firing during a test
  run, which is how §2 was found.

Not covered:

- **No test asserts what the API returns when `provision_admin_user` fails.**
  The failure is exercised incidentally, never asserted, and the assertion that
  is missing - "a 201 was returned and no `User` exists" - is the finding (§2).
- No test calls `reset-config` with a wrong or nonsense token (§4), because there
  is no wrong token.
- No test calls `reset-config` and then asserts modules or localization changed
  (§4).
- No test calls `reset-config` as a suspended super admin (§5).
- No test re-invites the same contact and counts `ContactInfo` rows (§16).
- The management command has no test at all - not its skip logic, not its
  `--dry-run`, not its unknown-slug warning, not its summary line.

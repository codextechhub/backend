# Frontend brief: Field Access and the permission tree

Give this whole file to the frontend agent as its prompt.

---

You are building the frontend for **Field Access** and a reshaped **permission
allocation** screen in two React apps. The backend design is the contract:

`/Users/mac/Documents/Dev-Projects/GitHub/backend/docs/rbac/rbac_field_access_design.md`

Read it first, especially sections 2 (decisions), 9 (API) and 14 (what changes).
Do not invent endpoints, fields or status codes. If the contract lacks something you
need, stop and report the gap instead of working around it.

## The two apps

Both are React 19, TypeScript, Vite and Redux Toolkit (RTK Query). Read each repo's
own `CLAUDE.md` or contributor notes before writing code, and follow its conventions,
components and design system.

| App | Path | Who uses it |
|---|---|---|
| Console | `/Users/mac/Documents/Dev-Projects/GitHub/console-fe` | Codex staff (the platform tenant) |
| School | `/Users/mac/Documents/Dev-Projects/GitHub/school-fe` | school staff |

Every behaviour below applies to **both**. Places to start reading:

- Console: `src/pages/protected/rbac/roles/edit-role.tsx`, `src/redux/services/dashboard/rbac-api.ts`,
  `src/redux/services/rbac/override-api.ts`, `src/components/custom/permission-overrides.tsx`,
  `src/hooks/use-permissions.ts`, `src/components/custom/permission-gate.tsx`,
  `src/permissions/index.ts`, `src/pages/protected/organogram/` (staff payroll).
- School: `src/pages/protected/roles/index.tsx`, `src/pages/protected/staff/drawers/role-drawer.tsx`,
  `src/pages/protected/onboarding/roles.tsx` and `components/role-drawer.tsx`,
  `src/redux/services/roles/roles-api.ts`, `src/hooks/use-permissions.ts`,
  `src/utils/fls.ts`, `src/permissions/index.ts`, `src/pages/protected/students/` (medical fields).

## What the owner wants, in their words, made precise

### 1. Permission allocation by Module → Resource

An admin opens a role and allocates permissions by narrowing **Module → Resource**,
then ticks that resource's permissions, each shown by its readable label ("View
vendors"), never a raw key. Source: `GET /rbac/tenants/<slug>/access-catalogue/`
(`permissions` inside each resource). This replaces the flat
`permission-catalogue/` list everywhere a role's permissions are edited, onboarding
role drawers included.

Keep what the current picker already shows:
- **Unavailable** permissions (`available: false`) stay visible but disabled, with
  `unavailable_reason`.
- **Restricted** permissions (`is_restricted`) still go through a role change request.
  Say so beside them.

### 2. Field Access menu

A **Field Access** menu item, visible only when the actor holds the tenant's
`*.roles.view` key and either `*.field_access.view` or `*.field_access.manage`
(manage unlocks editing):

1. The admin picks a **role**.
2. They narrow **Module → Resource** and can search by field label.
3. Each field shows its label, group heading ("Banking", "Medical"), a sensitive
   marker, and **Read** and **Write** switches.
4. The UI shows whether the value is the **default** or **set for this role**
   (`source`), and offers **Reset to default** per field.
5. Saving sends `PATCH roles/<key>/field-access/` with only the changed fields. Render
   the response, not your local guess.

Switch rules the UI enforces before the backend does:
- Turning **Write ON** turns **Read ON**.
- Turning **Read OFF** turns **Write OFF**.
- A field with `writable: false` has no Write switch.

Changes take effect immediately. There is **no approval step and no "pending" state**
for field access. Do not borrow the role change request UI.

If the admin edits a role they hold themselves, refetch `/user/auth/me/` after saving
so their own screens update.

### 3. One-person exceptions

On the user detail screen, next to today's permission exceptions, add **field
exceptions**: field, Read or Write, Allow or Deny, required reason, optional expiry.
The rules are the same as permission exceptions: no exceptions on yourself (hide the
control on your own profile), and a new exception for the same field and access
replaces the old one. Use `role_state` to explain the effect, for example "The role
hides this. Allowed for this person until 28 Sept."
Endpoints: `users/<id>/field-access-overrides/`. They are guarded by the SAME keys as
today's permission exceptions (`school.user_overrides.view|manage` in a school,
`platform.team_overrides.view|manage` in the console), not by the field access keys,
so show the control beside the permission exception control when the actor also
holds the tenant's `*.roles.view` key. The companion role-view requirement lets the
field picker use `access-catalogue/`; it does not change the exception endpoint guard.
For a school user opened from the console, request that school's catalogue by path
slug and `tenant` assertion rather than showing the platform tenant's field list.

### 4. Every screen obeys the switches

This is the part that must feel automatic.

- **Hidden (Read OFF): the field is not there.** No label, no lock icon, no
  "Restricted" text, no empty space, no table column. An empty section heading
  disappears with its fields.
- **Read-only (Write OFF): the field is shown greyed and disabled.** The user cannot
  type in it, and the form never sends it.
- The backend refusing a write (403 `field_write_denied`) is the safety net for
  direct API use. If a normal screen ever triggers it, that is a frontend bug. Show
  the per-field error rather than a generic toast.

Where the answer comes from:
- **An existing record:** a detail response lists `_read_only_fields`. Hidden fields
  are simply absent. Trust the record over the global map, because it carries rules
  like "your own payroll details are always editable".
- **Create forms and table columns:** `field_access` from the login payload and
  `/user/auth/me/`, keyed by `"module.resource"` with `hidden` and `read_only` name
  lists. An absent resource or name means full access.

Build **one shared hook and one form-field wrapper per app** (for example
`useFieldAccess(resource, record?)` returning `isHidden(name)` and `isReadOnly(name)`)
and use them everywhere. Do not re-implement the check per screen.

Surfaces that must use it, at minimum:
- vendors (contacts and banking),
- finance bank accounts,
- payroll lines and employee salaries,
- virtual accounts, payouts, and the money movements feed (payout rows no longer carry
  `party` and `beneficiary_account` when hidden, instead of `••••`),
- students (blood group, allergies, conditions, enrolment date),
- staff profiles (bank name, account name, account number) in the console organogram,
- user security metadata and invitation details in the console,
- import templates, jobs and batches,
- the Export Centre column picker.

## Things that are added

Four permission keys, which both apps' `src/permissions/index.ts` maps must carry
(school-fe's map is kept in lockstep with the backend seed by hand):
`school.field_access.view`, `school.field_access.manage` (school-fe) and
`platform.field_access.view`, `platform.field_access.manage` (console-fe). Existing
School Admin roles receive the school pair when the backend seeds run.

The two `manage` keys are **restricted** (`is_restricted: true` in the catalogue) and
the two `view` keys are not. The role picker must treat manage like every other
restricted permission. It never appears inside a permission group. Adding it to a
role the editor holds is answered with 409 `RESTRICTED_NEEDS_APPROVAL` and goes to
the role change request flow. Assigning a role that carries it is refused to anyone
who does not hold it themselves. Changing field switches, once someone holds manage,
still needs no approval.

## Things that are removed

1. `_stripped_fields` no longer exists. Delete `school-fe/src/utils/fls.ts` and its
   uses, and the console organogram drawer's use, replacing both with the shared hook.
2. These permission keys no longer exist. Remove them from both
   `src/permissions/index.ts` maps and from any copy that prints them:
   `procurement.vendor.view_sensitive`, `finance.bankaccount.view_sensitive`,
   `finance.payrollrun.view_sensitive`, `payments.virtual_account.view_sensitive`,
   `payments.payout.view_sensitive`, `school.students.view_sensitive`,
   `platform.staff_payroll.view`, `platform.staff_payroll.manage`.
   `platform.tasks.view_sensitive` **stays**.
3. Copy such as "Restricted - requires platform.staff_payroll.view" (console
   `staff-detail.tsx`, `detail-drawer.tsx`, `profile-form.tsx`) goes, because hidden
   means hidden.

## Acceptance, as the owner will test it

1. **Bright Star School, one branch.** The admin opens Field Access, picks
   Storekeeper, goes Procurement → Vendors, and sets Phone to Read ON / Write OFF and
   Bank account number to Read OFF. Mr. Bello (Storekeeper) reloads Ade Stationers: he
   sees Phone greyed, no banking section, and no hint that one exists. The vendor list
   has no bank column. The Export Centre does not offer the bank column.
2. The admin turns Bank account number **Write ON**. Read turns on by itself. Mr. Bello
   refreshes and can edit it.
3. **Greenfield, two branches.** Teachers are given Allergies Read ON at the school.
   A teacher at either branch sees allergies on a pupil's profile. Nothing about
   branches appears on the Field Access screen when a role is not branch-pinned.
4. Mrs. Okafor goes on leave. The admin gives Mr. Bello a **Read ALLOW** exception on
   bank account number, reason "Covering bursar duties", expiring 28 Sept. He sees the
   field. The admin cannot open that control on their own profile.
5. A Codex staff member without payroll access opens a colleague's profile in the
   console: no bank fields. On their **own** profile they see and edit their bank
   details.
6. Permission allocation: the admin opens School Admin, narrows to School → Students,
   and sees "View students", "Enrol students" and so on as sentences, with a disabled
   module row explaining the plan when the school has not bought it.

## Working rules

- The word is **branch**, in code and in copy. Never use a synonym for it, even where a design mockup does.
- No em dashes in code, comments or copy. Use a comma, colon, parentheses or a hyphen.
- Comments are short labels; reasoning goes in the component or hook docblock, written
  for a stranger, with no ticket or milestone names.
- Test with a **single-branch and a multi-branch** tenant, and in **both** apps.
- RELEASE (decision D17): the Field Access screen, field exceptions and the permission
  tree picker are committed to main in each app. Deployment is manual, and these screens
  are meant to reach users with the backend stage 3 release.
- The backend ships in slices (design section 13). Build against the contract, and wire
  a screen to live data only once its slice exists: S1 unlocks permission allocation,
  S2 unlocks the Field Access screen and exceptions, and S3 unlocks hiding and greying
  everywhere. The S3 frontend changes must release together with backend S3.
- Do not commit. When done, report per app: what you built, which screens now use the
  shared hook, anything in the contract you found missing or ambiguous, and the test
  runs you did, with their output.

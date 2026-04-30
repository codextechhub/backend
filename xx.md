# Salesforce Roles, Profiles, and Permissions Architecture: A Granular Technical Reference
Salesforce's access-control model is intentionally layered. It separates **what a user can do**
(object/field/system permissions) from
**which records a user can see**
(record-level access), and then layers further controls (login restrictions, sharing rules, manual sharing,
named credentials) on top. This document walks through every layer in technical detail, using exact Salesforce terminology.
---
## 1. The Three Pillars: Profiles, Permission Sets, and Roles
Salesforce administrators frequently summarize the model with the mnemonic: **"Roles see, Profiles do, Permission Sets add."** That
single line captures the architectural distinction. [DESelect
]
(https://deselect.com/blog/salesforce-roles-vs-profiles-and-permission-sets-
the-complete-2026-guide/)
| Construct | Governs | Cardinality per user | Mandatory? |
|---|---|---|---|
|
**Profile**
| Baseline of what a user *can do* — object CRUD, field-level security, app/tab visibility, record types, page layouts, system
permissions, login hours, login IP ranges, password policies | Exactly **one**
| Yes |
|
**Permission Set**
| Additive grants on top of the profile — same dimensions as a profile (object, field, system, app, Apex class,
Visualforce page, Custom Permission, Custom Metadata Type, Named Credential, External Credential principal) |
**Many**
(limit depends
on edition) | No |
|
**Permission Set Group**
| A bundle of Permission Sets representing a "persona," with optional Muting Permission Sets to subtract
grants |
**Many**
| No |
|
**Role**
| Where the user sits in the **role hierarchy**, which controls *record visibility* upward |
**At most one**
| No (optional) |
A user therefore always has exactly one Profile, optionally one Role, and any number of Permission Sets and Permission Set Groups.
---
## 2. Profiles in Detail
A **Profile** is the baseline configuration for a user. Every user must have exactly one. A profile controls:
- **Object permissions (CRUD + View All / Modify All)**: Create, Read, Edit, Delete, View All, Modify All [Akhil Kulkarni
]
(https://salesforce.fun/2020/04/29/data-security-through-profiles-in-salesforce/) on each standard and custom object.
- **Field-Level Security (FLS)**: For each field, two flags — *Read*
(visible) and *Edit*. If Read is unchecked, the field is invisible across
**detail pages, edit pages, related lists, list views, reports, search results, and the API**. (Page layouts only control visibility on detail/edit
pages, so FLS on the profile/permission set is the authoritative gate.) [Salesforce]
(https://trailhead.salesforce.com/content/learn/modules/data_security/data_security_fields)
- **App, Tab, Record Type, and Page Layout assignments** — including default record type per object.
- **System Permissions** — high-level capabilities such as `Modify All Data`, `View All Data`, `API Enabled`, `Manage Users`, `Author
Apex`, `View Setup and Configuration`, `Run Reports`, `Export Reports`, `Customize Application`.
- **Login Hours** — a weekly schedule of allowed login windows.
- **Login IP Ranges** — start/end IP pairs (IPv4, IPv4 CIDR, IPv6 CIDR) outside of which logins are denied. For Partner User profiles, IP
ranges are limited to five. [Forcetalks
]
(https://www.forcetalks.com/salesforce-topic/how-to-set-the-login-hours-and-login-ip-ranges-to-
the-users-in-salesforce/)
- **Password Policies** — overrides org-wide password policies for users on that profile. [Salesforce]
(https://trailhead.salesforce.com/content/learn/modules/data_security/data_security_org)
- **Apex Class and Visualforce Page Access**, **Service Presence Statuses**, **Connected App OAuth flow access**, etc.
- **User License binding**: a profile is permanently associated with a single User License (e.g., Salesforce, Salesforce Platform,
Customer Community, Partner Community). You cannot reassign a profile to a different license type; you must clone or create a new one.
### Standard Profiles (cannot be deleted, renamed, or have their object permissions edited)
The exact standard profile names Salesforce ships are:
- **System Administrator** — full access; includes `View All Data` and `Modify All Data`, which override sharing settings [Salesforce]
(https://trailhead.salesforce.com/content/learn/modules/data_security/data_security_objects) and field-level security on most objects.
- **Standard User** — broad CRUD on most standard objects but no administrative permissions.
- **Read Only** — read access only.
- **Solution Manager** — Standard User + manage published solutions.
- **Marketing User** — Standard User + Campaign management and import leads.
- **Contract Manager** — Standard User + manage contracts.
- **Minimum Access – Salesforce** — Salesforce's recommended starting baseline. Grants almost nothing; relies entirely on permission
sets to layer access. Salesforce's modern best-practice guidance is to assign this profile to virtually all users and grant everything else
via permission sets.
- **Chatter Free User**, **Chatter External User**, **Chatter Only (Chatter Plus) User** — Chatter-only licenses.
- Customer/Partner Portal and Experience Cloud variants such as **Customer Community User**, **Customer Community Plus User**,
**Customer Community Plus Login User**, **Partner Community User**, **High Volume Customer Portal**, **Authenticated Website**,
etc., each tied to its corresponding license.
### Custom Profiles
Custom profiles are created by **cloning** an existing profile. [Salesforce]
(https://trailhead.salesforce.com/content/learn/modules/data_security/data_security_objects) They can be renamed, edited, and deleted
(provided no users are assigned). Naming conventions in well-architected orgs follow patterns like `Minimum Access – Sales`, `Minimum
Access – Service`, `Integration User – Mulesoft`, `API Only User`. The historical anti-pattern is **profile sprawl** — `Sales User – London`,
`Sales User – London New`, `Sales User – Manager – London v2` — caused by adding minor differences as new clones rather than as
permission sets.
---
## 3. Permission Sets in Detail
A **Permission Set** has the same shape as a profile (object permissions, FLS, system permissions, app/tab/record type access,
Apex/Visualforce access, Custom Permissions, etc.) but with three crucial differences:
1. It is **purely additive**. It can grant permissions; it cannot remove them. If the profile already grants Read on Account, a permission
set cannot revoke it.
2. A user can have **many** permission sets simultaneously.
3. It is not bound to login hours or login IP ranges (those remain profile-only — see §8).
### How they "stack"
The effective permission for a user is the **union** of the profile and all assigned permission sets (and all permission sets contained in
any assigned permission set groups, minus any muted permissions). For example:
- Profile grants Read on Opportunity.
- Permission Set A grants Edit on Opportunity.
- Permission Set B grants Delete on Opportunity.
- Effective: **Read + Edit + Delete** on Opportunity.
There is no "deny" semantic: if any source grants a permission, the user has it.
### Assignment
Permission sets are assigned via the `PermissionSetAssignment` object (visible in the **Permission Set Assignments** related list on
the User record, and on the Permission Set page itself). They can be assigned manually in the UI, in bulk via Data Loader / Apex, or with
**expiration dates**
(Permission Set Expiration, GA since Winter '23) so access auto-expires.
### Permission Set Licenses
Some permission sets require an underlying **Permission Set License**
(PSL) — e.g., Sales Cloud User PSL, Service Cloud User PSL,
CRM User PSL, Identity Connect PSL. The PSL must be assigned to the user before the corresponding permission set can be assigned.
### Naming conventions (best practice)
Profiles tend to be named after roles ("Sales Manager"). Permission sets should be named after **features or tasks**, not personas:
- Functional: `Lead Conversion`, `Campaign Management`, `Knowledge Article Publishing`, `Data Loader Access`
- Object-scoped: `Opportunity – Edit`, `Opportunity – View`, `Case – Manage`
- Prefixed by team/domain: `Sales – CPQ Access`, `Finance – Read Invoice Data`
Permission Set Groups, by contrast, *are* named after personas: `Sales Manager Full`, `Service Agent Enhanced`.
---
## 4. Permission Set Groups
A **Permission Set Group (PSG)** is a collection of permission sets assigned as a single unit. Salesforce introduced them in Spring '20
specifically to scale permission-set-led security models.
Key behaviors:
- Assigning a PSG to a user is equivalent to assigning every permission set inside it. The platform performs a **calculated permission
set** behind the scenes (visible as `PermissionSetGroupComponent` and a calculated state of `Updated`, `Outdated`, `Updating`, or
`Failed`).
- A given permission set can be a member of **multiple** PSGs, enabling reuse.
- PSGs can be assigned to users with **expiration dates**, just like individual permission sets.
### Muting Permission Sets
Inside a PSG you can attach **one** Muting Permission Set, which *subtracts* permissions. Muting:
- Can target the same dimensions as a permission set (object, field, Apex, Visualforce, tab, system permissions).
- Only takes effect inside the PSG; it cannot be assigned to users directly.
- Does **not** revoke permissions granted to the user from other sources (their profile, another permission set, or another PSG). It only
removes permissions that the *same group's* member permission sets would have granted.
- Has metadata representation `<mutingPermissionSets>` inside `PermissionSetGroup`. [SFDC Developers
]
(https://sfdcdevelopers.com/2025/10/13/what-is-the-use-of-muting-permission-set-in-permission-set-group/)
Example: a managed package's permission set in the PSG grants `Modify All` on Account, but you don't want that for this persona — you
create a Muting Permission Set inside the PSG that mutes `Modify All`. Permission dependencies are honored: if you mute `Activate
Orders`, then `Edit Activated Orders` is muted as a dependency.
---
## 5. Roles and the Role Hierarchy
A **Role** is fundamentally different from a profile. It does not control *what a user can do*
Roles are arranged into a tree — the **Role Hierarchy** — and a user occupies at most one node.
; it controls *whose records a user can see*.
### Mechanics
- Each role has a **parent role**
(except the top, which is your org). Salesforce supports up to 500 roles, recommended depth no more
than 10 levels.
- For an object whose Organization-Wide Default (OWD) is **Private**, **Public Read Only**, or **Public Read/Write/Transfer
(Cases/Leads)**, users in a parent role automatically gain access to records owned by users in any child role beneath them — read or
read/write depending on the OWD and the `Grant Access Using Hierarchies` setting.
- Each custom object has a **Grant Access Using Hierarchies** checkbox on its OWD. Standard objects always grant access via
hierarchy and this cannot be disabled. Custom objects can disable it, in which case only the record owner and explicitly-shared users see
the records — the role hierarchy stops conferring visibility.
- The role hierarchy does **not** need to mirror the org chart. It models *who needs to see whose data*. Many job titles can collapse to a
single role (e.g., Senior SE and Junior SE → "Software Engineer").
### Standard Role Naming
Salesforce ships sample roles when you initialize the Role Hierarchy from a template (e.g., "Universal Telco," "Generic"), but there are no
immutable standard roles equivalent to standard profiles. Typical out-of-the-box example roles include `CEO`, `CFO`, `COO`, `VP, North
American Sales`, `VP, International Sales`, `VP, Marketing`, `Director, Direct Sales`, `Sales Manager`, `Sales Rep`. Most orgs replace these
entirely. Custom names usually mirror data-access tiers, e.g., `EMEA – Sales Manager – DACH`, `EMEA – Sales Rep – DACH`.
### What roles do *not* control
Roles cannot grant object-level permissions. Putting a Sales Rep into the CEO role does not let them edit Opportunities if their
profile/permission sets lack Edit on Opportunity — they would just be able to *see* more Opportunity records they can't modify.
---
## 6. Record-Level Access: OWD, Hierarchy, Sharing Rules, Manual Sharing, Teams, Restriction Rules
Object-level permissions answer "can this user touch any record of this object?" Field-level security answers "which fields on a record can
they read or edit?" **Record-level access** answers "which specific records can they see?"
Salesforce evaluates record access through these layers, each of which can only **open up** access (except Restriction Rules and OWD
itself, which restrict):
### 6.1 Organization-Wide Defaults (OWD)
OWD sets the **most restrictive** baseline per object. [Medium
]
(https://amansfdc.medium.com/salesforce-security-mastery-object-
permission-sets-field-and-record-level-strategies-with-owd-fd1c8c28e97c) There are separate `Default Internal Access` and `Default
External Access` settings (the external setting cannot be more permissive than the internal one). Values:
- **Private** — only the record owner and users above them in the role hierarchy.
- **Public Read Only** — everyone can view, but only the owner and superiors can edit.
- **Public Read/Write** — anyone can view and edit.
- **Public Read/Write/Transfer** — only available for Leads and Cases; adds the ability to transfer ownership.
- **Public Full Access** — only for Campaigns; adds Delete and Sharing.
- **Controlled by Parent** — only for objects in master-detail relationships; the child inherits the parent's access.
OWD is the one mechanism (besides Restriction Rules) that genuinely **restricts** access. Sharing rules, hierarchies, and manual
sharing can never be more restrictive than OWD; they can only *grant* access on top of it.
### 6.2 Role Hierarchy
As described in §5, this opens visibility upward. A user inherits access to records owned by anyone in subordinate roles, and the access
mode (Read vs Read/Write) follows the OWD.
### 6.3 Sharing Rules
**Sharing rules** create automatic exceptions to OWD for predictable groups of users. They are defined per object and only apply when
OWD is `Private` or `Public Read Only`. Each rule has three components: *which records*, *with which users*, and *what level of access*
(Read Only or Read/Write).
Two flavors:
- **Owner-based sharing rule** — share records owned by users in a Role, Role + Subordinates, Role + Internal Subordinates, Public
Group, or Territory, with another such group.
- **Criteria-based sharing rule** — share records where field values match a filter (and optionally include "owner is" criteria), with users in
groups/roles/territories. Salesforce supports up to 50 criteria-based sharing rules per object.
- **Guest user sharing rules** — required to share records with unauthenticated guest users on Experience Cloud sites.
Sharing rules cannot make access more restrictive than OWD. A sharing rule that grants Read/Write still requires the user to have Edit
object permission on their profile/permission set; otherwise they get Read-only effective access.
### 6.4 Manual Sharing (`Sharing` button)
For records the OWD makes private, the record owner, anyone above the owner in the hierarchy, and users with `Modify All Data` (or
`Modify All` on the object) can grant one-off Read or Read/Write access to a specific user, public group, role, role + subordinates, or
territory via the **Sharing** button on the record. Manual shares are removed automatically when ownership of the record changes.
### 6.5 Teams (Account, Opportunity, Case)
For these three objects, Salesforce provides **Team** mechanisms (Account Teams, Opportunity Teams, Case Teams) that act like
persistent manual shares. Each team member gets a defined access level on the parent record and optionally on related records.
### 6.6 Programmatic / Apex Managed Sharing
For each shareable object there is a `<Object>Share` table (e.g., `AccountShare`, `OpportunityShare`, `CustomObject__Share`). Apex code
can insert rows here with `RowCause = 'Manual'` or with a custom `Apex Sharing Reason` defined on the object, enabling programmatic
sharing logic that survives owner changes (when using Apex Sharing Reasons).
### 6.7 Restriction Rules (Summer '21 GA)
Restriction Rules **filter down** what a user is allowed to see, even if other mechanisms grant access. They consist of *user criteria*
(which users) plus *record criteria*
(which records those users may see) — a user matching the user criteria can only see records
matching the record criteria. Available on most objects (custom objects, Tasks, Events, Contracts, Time Sheets, etc.). Useful for things
like "Even though Cases are Public Read/Write, an external auditor may only see Cases tagged 'Audit-OK'."
### 6.8 Scoping Rules (Winter '22)
A softer version that controls the *default scope* of records a user sees in list views and reports without removing access altogether —
users can opt back into the full set if they have access.
### 6.9 Implicit Sharing
Salesforce automatically maintains implicit sharing between Accounts and their child Contacts/Opportunities/Cases — e.g., if a user has
access to an Opportunity, they get implicit Read access to the parent Account, and vice versa for the parent-to-child rollup of access.
---
## 7. Interaction Between the Layers
The layers combine according to specific precedence and union rules. Two key principles:
1. **Object permissions are the ceiling for access.** OWD, sharing rules, hierarchies, and manual sharing only work *within* what the
profile/permission sets allow. If the profile says No-Edit on Opportunity, no sharing rule, hierarchy position, or team membership grants
Edit.
2. **Record-level access is the union of all sharing mechanisms**, capped by object-level access, then filtered by FLS for fields and (if
applicable) Restriction Rules.
### Edge cases and "theatrics"
- **Profile grants Edit, OWD is Private, user is not the owner and not above owner in hierarchy:** user sees no records. Object permission
alone does not produce records. They must own a record, be above the owner, get a sharing rule, get a manual share, be on a team, or be
granted access via Apex sharing.
- **Permission set grants more than the profile:** the union wins. The user has the additional access. Salesforce explicitly supports this
and recommends it (the modern "permission-set-led" approach).
- **`View All Data` / `Modify All Data` system permissions** completely bypass record-level sharing for *all* objects. `View All` / `Modify
All` at the object level [Akhil Kulkarni
]
(https://salesforce.fun/2020/04/29/data-security-through-profiles-in-salesforce/) bypasses sharing
for that object only. These trump OWD entirely — but they do **not** bypass field-level security. A user with `View All Data` still cannot
see a field whose FLS is Hidden for them.
- **Field-Level Security takes precedence over View All Data for fields**. Fields hidden via FLS remain hidden even for users with `View All
Data` (they get records and rows, but no column values).
- **Page layout vs FLS**: A field can be on the page layout but FLS-hidden for the user — they will not see it. Conversely, a field with FLS
Read but not on the layout is invisible only on detail/edit pages, but still visible in reports and the API.
- **Sharing rule grants Read/Write, profile grants only Read:** user gets effective Read only.
- **Record owner**: always has full access regardless of sharing settings (subject to object-level deletion permission).
- **`Grant Access Using Hierarchies` disabled on a custom object**: even users above an owner in the role hierarchy lose automatic
access. Only the owner and explicitly-shared users see the records.
- **Profile-bound Login Hours and Login IP Ranges cannot be granted by permission sets.** This is one of the few capabilities still profile-
exclusive.
- **Muting Permission Set inside a PSG only mutes within that group.** It cannot mute permissions granted by the user's profile, by
another permission set assigned outside the PSG, or by another PSG.
- **Sharing recalculation**: changing OWD, role hierarchies, or sharing rules triggers asynchronous sharing recalculation. For very large
orgs Salesforce sends an email when it completes; running queries during recalc can show transient access.
---
## 8. Login Hours, Login IP Ranges, and Named Credentials in the Permission Model
### Login Hours
Set per **Profile** at *Setup → Profiles → [Profile]
→ Login Hours*. A weekly grid (each day Sun–Sat with start/end time) determines
when users on that profile may log in. If a user is mid-session when the window closes, their session is terminated. Times are interpreted
in the org's default time zone. Login Hours are **only on profiles**
; permission sets cannot grant or restrict them.
### Login IP Ranges
Two distinct mechanisms:
1. **Profile-level Login IP Ranges**
(
*Setup → Profiles → [Profile]
→ Login IP Ranges*): hard restriction. A login from outside the listed
ranges is **denied** with no second-factor option. Supports IPv4 start/end pairs and CIDR; Partner User profiles capped at 5 ranges.
2. **Org-wide Trusted IP Ranges**
(
*Setup → Network Access*): softer — logins from trusted IPs skip device activation/identity
verification, but logins from outside still succeed (with verification).
Combined with **Session Settings**
(`Lock sessions to the IP address from which they originated`, `Enforce login IP ranges on every
request`), these provide network-layer security that supplements but is independent of the permissions/sharing model.
### Password Policies
Set at the Profile level (overriding org-wide settings). Include password length, complexity, history, expiration, max invalid attempts,
lockout duration, and answer obscurity for password reset.
### Named Credentials and External Credentials
Named Credentials are how Salesforce manages outbound HTTP callout authentication (replacing hardcoded URLs and credentials in
Apex). The architecture (post Winter '23):
- **External Credential** — defines the *authentication protocol*
(OAuth 2.0, AWS Sig V4, JWT, Basic via Custom Headers, etc.) and one
or more **Principals**
(Named Principal, Per-User Principal, OAuth Per-User Principal). Each Principal stores the actual secret/token in
encrypted secret storage (`User External Credential` for per-user, internal store for named principal).
- **Named Credential** — references an External Credential and adds the endpoint URL, allowed namespaces, and callout options
(compression, generate auth header, allow merge fields).
- **Permission Set Mappings** — on the External Credential, one or more permission sets are mapped to each Principal. **A user can
only authenticate via that Principal at runtime if they hold the mapped permission set.** When a user has multiple mappings, the
`Sequence Number` (lowest wins) determines which Principal is used.
This means callout access is gated by the permission set system — granting a user the right to use an external integration is done by
assigning the permission set mapped on the External Credential. Service Cloud agents might be mapped to a read-only Principal; Service
Cloud managers might be mapped to a delete-capable Principal — analogous to record sharing but for external systems.
End users invoking callouts at runtime also need read access to the `User External Credential` object; new permission sets receive this by
default (configurable).
---
## 9. Worked Example: Sales Rep vs Sales Manager vs Administrator
Assume a sales-driven org with these settings:
- **OWD** for Account: *Private*, Default External: *Private*. Opportunity: *Private*. Lead: *Public Read/Write/Transfer*. Case: *Public
Read Only*.
- **Grant Access Using Hierarchies**: enabled on all standard objects (cannot be disabled) and on relevant custom objects.
### 9.1 Sales Rep
- **Profile**: `Minimum Access – Salesforce` (cloned to `Minimum Access – Sales Cloud` for any tweaks, e.g., default app = Sales).
- **Role**: `Sales Rep – EMEA – DACH` (a leaf role in the hierarchy, child of `Sales Manager – EMEA – DACH`).
- **Permission Sets**
(assigned individually or via PSG `Sales Rep Standard`):
- `Lead – CRUD`
- `Opportunity – CRUD`
- `Account – Read/Edit` (no Delete)
- `Contact – CRUD`
- `Run Reports` and `Export Reports`
- `CPQ User`
- `Sales Console User`
- `Einstein Activity Capture Standard`
- **Permission Set License**: `Sales Cloud User`.
- **Effective access**:
- Sees only Accounts, Contacts, and Opportunities they own (OWD Private + leaf role).
- Sees all Leads (OWD Public Read/Write/Transfer).
- Sees all Cases read-only (OWD Public Read Only).
- Cannot delete Accounts (no permission set grants it).
- Cannot view Setup, run Apex, or export the entire data model.
### 9.2 Sales Manager
- **Profile**: same `Minimum Access – Sales Cloud` — *the profile is identical to the Sales Rep*. The differences are role and permission
sets.
- **Role**: `Sales Manager – EMEA – DACH` (parent of all DACH Sales Rep roles).
- **Permission Set Group**: `Sales Manager Full`, containing:
- All sets from `Sales Rep Standard`
- `Forecasts – Manager` (manage forecasts and view team data)
- `Opportunity – Transfer` (mass transfer ownership)
- `Account – Delete`
- `Sales Cadence – Manage`
- `Reports – Create Folders`
- Optional Muting Permission Set: mutes `Export Reports` because the manager dashboards are suﬃcient and the org's DLP policy
forbids ad-hoc CSV exports for non-admins.
- **Effective access**:
- Inherits visibility of every record owned by any Sales Rep beneath them in the role hierarchy (read and write, because of OWD
interaction with the hierarchy).
- Can transfer Opportunities and delete Accounts.
- Can manage forecasts for the team.
- Cannot export reports (muted).
- Can be granted broader cross-region visibility via an **owner-based sharing rule**: e.g., share Opportunities owned by `Role: Sales
Manager – EMEA – BENELUX and Subordinates` with `Public Group: EMEA Sales Managers Read-Only` for cross-team visibility without
re-parenting roles.
### 9.3 Administrator
- **Profile**: `System Administrator` (standard, includes `View All Data`, `Modify All Data`, `Customize Application`, `Manage Users`,
`Author Apex`, `API Enabled`, etc.). Login IP ranges restricted to the corporate VPN range; login hours 06:00–22:00 to deter middle-of-the-
night account misuse.
- **Role**: top of hierarchy or a dedicated `System Admin` role placed at the top, *or no role at all*
(acceptable if the admin doesn't own
customer-facing records).
- **Permission Sets**: typically minimal because the profile already grants everything, but additional ones may be assigned for managed-
package admin rights (e.g., `CPQ Admin`, `Marketing Cloud Admin Connector`).
- **Effective access**:
- Sees and modifies every record in the org (via `View All Data` / `Modify All Data`, bypassing OWD and sharing).
- Still cannot see fields explicitly hidden by FLS — admins commonly grant themselves those fields explicitly through profile FLS.
- Subject to MFA, login IP ranges, and login hours configured on the profile.
- Access to external systems via Named Credentials only where the admin holds the relevant permission-set mapping on the External
Credential.
### 9.4 Visualizing the data flow for a single Opportunity
When `sales.rep@acme.com` opens an Opportunity owned by another rep in the same DACH team:
1. Object permission check: Sales Rep has Read on Opportunity from `Opportunity – CRUD` permission set → pass.
2. Record visibility check: OWD is Private. The user is not the owner. Are they above the owner in the role hierarchy? No (peers). Is there a
sharing rule? No. Manual share? No. Team membership? No. →
**No access; record not visible.**
3. The same record opened by `sales.manager.dach@acme.com`: object permission Read from PSG → pass. Hierarchy check: manager
is the parent role of the owner →
**Read/Write access granted by hierarchy.**
4. The same record opened by `admin@acme.com`: `View All Data` system permission → bypass sharing entirely →
**Read/Write
access**, except any FLS-hidden fields.
---
## 10. Architectural Best Practices and Pitfalls
- **Permission-set-led model**: Salesforce oﬃcially recommends keeping a small number of restrictive baseline profiles (`Minimum
Access – Salesforce` + a few clones) and putting all granular access in permission sets and PSGs. Salesforce had announced (and then
walked back) the deprecation of permissions on profiles; the strong directional guidance remains permission-set-led.
- **Few profiles, many permission sets**: well-architected orgs typically have 3–6 non-admin profiles and dozens or hundreds of
permission sets named for features, not personas.
- **Profile sprawl**: cloning a profile for every minor variation (per region, per sub-team) yields unmaintainable orgs with 50+ near-
identical profiles.
- **Role ≠ org chart**: roles model data visibility, not reporting lines. Consolidate where possible; recommended ceiling around 40–50
roles for mid-size orgs and a depth ≤ 10.
- **Don't disable Grant Access Using Hierarchies casually** — many sharing rules and team behaviors implicitly assume hierarchical
rollup.
- **Restriction Rules** are the proper way to "subtract" from sharing for select users; do not approximate them by removing object
permissions, which breaks too many things.
- **Audit trail**: review users with `View All Data`, `Modify All Data`, `Author Apex`, `API Enabled`, and `Customize Application` regularly.
Use the **Setup Audit Trail**, **Salesforce Optimizer**, or third-party tools for permission analysis.
- **Integration users**: create a dedicated profile (`Integration User – ESB`) and a dedicated role at the top of the hierarchy (in its own
branch) so the integration "owns" any records it creates without inheriting team visibility.
- **Naming the muting permission set** with a `Mute_` prefix makes intent obvious to future admins.
---
## Summary Cheat-Sheet
| Question | Answer |
|---|---|
| What can the user *do*? | Profile + Permission Sets + PSGs (union, less any in-group muting) |
| Where does the user *sit* for record visibility? | Role (in Role Hierarchy) |
| What is the *baseline* who-sees-what? | OWD per object (Private / Public R/O / Public R-W / etc.) |
| How do peers/cross-teams gain access? | Sharing Rules (owner-based or criteria-based) |
| How do one-off shares happen? | Manual Sharing, Account/Opportunity/Case Teams, Apex Managed Sharing |
| How do I *restrict* below OWD for some users? | Restriction Rules (or Scoping Rules for default scope) |
| How do I gate field visibility? | Field-Level Security (Read/Edit) on profile or permission set |
| How do I gate *when* and *where* users log in? | Login Hours and Login IP Ranges on the Profile (and org Network Access for trusted
IPs) |
| How do I gate *which* external API a user can call? | External Credential Principal mapped to a Permission Set; assign the permission
set to the user |
| What *bypasses* sharing? | `View All Data` / `Modify All Data` (org-wide), `View All` / `Modify All` (per object) — but NOT Field-Level
Security |
| What does a permission set group *add* over individual permission sets? | Bundling for personas + the ability to mute via a Muting
Permission Set |
The mental model to retain: **Profiles are the floor, Permission Sets are stackable additions, Permission Set Groups are persona bundles,
Roles plus OWD plus Sharing Rules govern records, and Restriction/Scoping Rules carve out exceptions on top.** Object permission is
the ceiling for what record-sharing can ever deliver, and FLS is the final field-level filter that even system administrators must respect
unless they explicitly grant themselves the field.
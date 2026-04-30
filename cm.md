# Salesforce Roles and Permissions Architecture: A Comprehensive Technical Reference

## Introduction

Salesforce implements a layered, defense-in-depth security model. Every access decision the platform makes is the composite result of multiple, independently-administered security artifacts evaluated in a defined precedence. Understanding the model requires separating two fundamentally different questions: *What can a user do?* (the "actions" question, governed by Profiles, Permission Sets, and Permission Set Groups) and *Which records can a user see?* (the "visibility" question, governed by Organization-Wide Defaults, the Role Hierarchy, Sharing Rules, Teams, Manual Sharing, Apex Sharing, Restriction Rules, and Scoping Rules). Field-Level Security operates orthogonally to both. Together these form the canonical Salesforce security stack.

This reference documents every layer in granular detail, including the metadata, the precedence rules, and the real-world interactions between them.

---

## 1. The Three Pillars: Profiles, Permission Sets, and Roles

| Pillar | Answers | Cardinality per User | Mandatory? | Granted Access Model |
|---|---|---|---|---|
| **Profile** | What can the user *do*? | Exactly one | Yes — every user has exactly one profile (it is tied to the user license) | Baseline — most restrictive starting point |
| **Permission Set** | What *additional* things can the user do? | Zero, one, or many | No | Additive — pure union with profile |
| **Role** | Which records can the user *see*? | Zero or one | No — roles are optional (a user can have no role) | Hierarchical record-visibility rollup |

A profile and a role are conceptually orthogonal. Profile = capability. Role = visibility. Permission Sets are the modern, additive extension of Profiles. None of these three artifacts directly grants record-level access in isolation; record-level access is governed by Organization-Wide Defaults plus sharing mechanisms (covered in Section 6). Object-level CRUD permissions on a profile or permission set place a *ceiling* on what record-level sharing can deliver.

---

## 2. Profiles in Full Depth

### 2.1 What a Profile Controls

A Profile is a metadata container that defines:

- **Object permissions**: Create, Read, Edit, Delete (CRED/CRUD) for every standard and custom object, plus the supersetting **View All** and **Modify All** object permissions (which bypass sharing for that single object).
- **Field-Level Security (FLS)**: Per-field Read and Edit flags on every field of every object (subject to license/object visibility).
- **App assignments**: Which Lightning apps and Classic apps are visible to / default for the user.
- **Tab settings**: Default On, Default Off, or Tab Hidden for every tab.
- **Record Type assignments**: Which record types the user may select when creating records of an object, and the default record type per object.
- **Page Layout assignments**: Which page layout is shown to the user for each record type / object combination (Classic). In Lightning, Lightning Record Pages are typically governed via the Lightning App Builder activation rather than the profile.
- **System Permissions** (the "user permissions"): a long list of toggles. Key examples include API Enabled, View Setup and Configuration, Modify All Data, View All Data, Author Apex, Manage Users, Customize Application, Schedule Reports, Manage Public Reports, Run Reports, Export Reports, Manage Public List Views, Edit Read Only Fields, Send Outbound Messages, Manage Sharing, View Roles and Role Hierarchy, Reset User Passwords and Unlock Users, Manage Profiles and Permission Sets, View All Users, Password Never Expires, Bulk API Hard Delete, View Encrypted Data, Manage Sandboxes, Manage Connected Apps, Author Apex Lightning Components, Manage Flow, View Real-Time Event Monitoring Data, Customize Application, Manage Translation, Manage Login Access Policies.
- **Login Hours**: Day-of-week / time-of-day windows during which a user assigned this profile may log in. **Profile-exclusive** — cannot be granted by a permission set.
- **Login IP Ranges**: Allowed IPv4/IPv6 ranges (CIDR style start/end). **Profile-exclusive** — cannot be granted by a permission set.
- **Password Policies (per profile)**: Minimum length, complexity requirement, password expiration, password history, max invalid login attempts, lockout effective period, obscure secret answer, require minimum 1 day between password changes. (These override the org-wide password policy for users on this profile.)
- **Session Settings (per profile)**: Session timeout, "lock sessions to IP from which they originated," "require Login IP ranges on every request," and high-assurance session requirements for sensitive operations.
- **Apex Class Access**: Whitelist of Apex classes the user may execute (only enforced for Apex called from anonymous-like / button / Visualforce / public Site contexts; controllers and Apex-defined web services check this).
- **Visualforce Page Access**: Whitelist of Visualforce pages the user may render.
- **Custom Permissions**: Enabled custom permissions used by formula fields, validation rules, and Apex.
- **Connected App Access**: Which connected apps the user may launch.
- **Service Provider (SAML) access**, **Flow access**, **Named Credential references**, **External Data Source references**, **Custom Metadata Type record access**, **Custom Setting definitions** access.

### 2.2 Standard Profiles Salesforce Ships

Out of the box, Salesforce provisions a set of non-deletable Standard Profiles. Which ones appear depends on which licenses and clouds your org has, but the canonical list includes:

- **System Administrator** — full Modify All Data and View All Data plus virtually every system permission.
- **Standard User** — typical end user: read/create/edit/delete on standard CRM objects.
- **Read Only** — read access to standard CRM objects, no create/edit/delete.
- **Solution Manager** — Standard User permissions plus management of published solutions and solution categories.
- **Marketing User** — Standard User plus Manage Campaigns, manage public email templates, import leads.
- **Contract Manager** — Standard User plus management of contracts (activate, approve, edit activated, delete).
- **Minimum Access – Salesforce** — minimal baseline: log in, see Setup but very few actions; designed to be the starting profile in a permission-set-led model.
- **Minimum Access – API Only Integrations** — Spring '24 replacement for the older "Salesforce API Only System Integrations" profile, used with the Salesforce Integration user license. API Enabled and API Only User are TRUE and not editable.
- **Chatter Free User**, **Chatter External User**, **Chatter Moderator User** — for Chatter-only licenses.
- **Customer Community User**, **Customer Community Plus User**, **Customer Community Login User**, **Customer Community Plus Login User** — Experience Cloud customer profiles.
- **Partner Community User**, **Partner Community Login User**, **Gold Partner User**, **Silver Partner User** — Experience Cloud partner profiles.
- **High Volume Customer Portal User / Authenticated Website** — high-volume external users (no role; cannot be in role-based sharing).
- **Cross Org Data Proxy User**, **Salesforce Platform**, **Salesforce Platform One App** — platform-license profiles.
- **Analytics Cloud Integration User**, **Analytics Cloud Security User** — for CRM Analytics.
- **Identity User** and **External Identity User** — for Identity-only licenses.

Standard profiles cannot be edited beyond a small set of fields (default app, tabs, certain assignments) and cannot be deleted.

### 2.3 Custom Profiles

Custom profiles are created by **cloning** an existing standard or custom profile (Setup → Profiles → Clone, or Object Manager / Metadata API). Profiles cannot be created from scratch — they always derive from another profile and inherit its user license. Once created, the admin edits CRUD/FLS, system permissions, app/tab/record type/page-layout assignments, login hours, login IP ranges, and password policies.

**The "profile sprawl" anti-pattern**: Historically organizations created a new custom profile every time a user variant emerged ("Sales Rep – East," "Sales Rep – East – No Discounting," "Sales Rep – East – Senior – No Discounting"). The result is dozens or hundreds of nearly-identical profiles, each requiring independent maintenance whenever a permission must change. Salesforce officially identifies this as an anti-pattern and recommends consolidation onto a small set of minimum-access profiles, with permission sets carrying the deltas.

### 2.4 Modern Guidance: Minimum Access – Salesforce as the Baseline

Salesforce's published recommendation (Trailhead, Admin Best Practices, Architects guidance) is to assign **Minimum Access – Salesforce** (or a clone of it) as the starting profile for nearly all internal users, and then layer all functional access via permission sets and permission set groups. The profile then carries only the genuinely profile-exclusive items: defaults, page-layout assignments, login hours, login IP ranges, and the API Only flag for integration users. This is the **permission-set-led model**.

### 2.5 Profile-Exclusive Items

Despite Salesforce's long-running "permissions on profiles end-of-life" effort (later paused), the following remain **profile-exclusive** and cannot be granted by a permission set:

- **Login Hours**
- **Login IP Ranges**
- **Page Layout assignments** (assignments themselves; the layouts can be edited elsewhere)
- **Default record type per object**
- **Password policies** (per-profile)
- **Default app**
- The **API Only** flag (functionally tied to the user license/profile)

Everything else — object CRUD, FLS, system permissions, app visibility, tab settings, record type *availability*, Apex class access, Visualforce page access, custom permissions, connected app access — can be granted by a permission set.

---

## 3. Permission Sets in Full Depth

### 3.1 The Union/Additive Model

Permission Sets stack on top of the Profile as a pure logical OR (union). If the profile grants a permission, or *any* assigned permission set grants it, the user has it. Permission Sets can never *remove* a permission that the profile grants (subtraction is exclusively the job of Muting Permission Sets within Permission Set Groups — see Section 4). This makes permission sets safe to add: they only ever expand access.

### 3.2 What a Permission Set Can Grant

A permission set can grant essentially the same things a profile can, *except* the profile-exclusive items in §2.5. Specifically:

- Object permissions (Create / Read / Edit / Delete / View All / Modify All)
- Field-Level Security (Read, Edit)
- System (user) permissions
- App and tab settings (visibility)
- Record Type assignments (availability — not the *default*)
- Apex class access
- Visualforce page access
- Custom Permissions
- Connected App access
- **Named Credential / External Credential Principal Access** mappings (see Section 9)
- Custom Metadata Type record access
- Custom Setting definitions access
- Service Presence statuses (Service Cloud)
- Flow access

### 3.3 Permission Set Licenses (PSLs)

A **Permission Set License** is a license SKU that, when assigned to a user, *unlocks the ability* to assign certain permission sets (and the underlying entitlements they reference). PSLs decouple feature licensing from the base User License, enabling fine-grained licensing per user. Examples:

- **Sales Cloud User** PSL — required to assign permission sets that grant Sales Cloud features to a user on a non–Sales Cloud user license.
- **Service Cloud User** PSL — equivalent for Service Cloud.
- **Salesforce API Integration** PSL — provisioned with the Salesforce Integration user license; underpins the integration-user pattern.
- **CRM User** PSL, **Sales Console User** PSL, **Service Console User** PSL.
- **Identity Connect**, **Identity Verification Credentials**, **Einstein Analytics Plus**, **CRM Analytics Plus Admin / User**, **Industries-specific PSLs** (Health Cloud, Financial Services Cloud, Manufacturing Cloud, etc.).

Assignment chain: User License → (optional) Permission Set License → Permission Set → User.

### 3.4 Assignment with Expiration Dates

Permission set and permission set group assignments support an **Assignment Expiration Date**. When the date passes, Salesforce automatically removes the assignment. This is the supported pattern for time-boxed access (contractor stints, temporary elevation for an audit, project access). Expiration applies to permission sets and permission set groups but not to profile assignment.

### 3.5 Naming Conventions

The recommended naming convention is **feature- or capability-based, not persona-based**:

- ✅ `Account_Contact_Edit`, `Opportunity_Discount_Approve`, `Knowledge_Author`, `Reports_Builder`
- ❌ `Sales_Rep`, `Eastern_Sales_Manager`, `John_Smith_Permissions`

Persona names go on Permission Set Groups, which assemble the feature-level permission sets into a job-function package.

---

## 4. Permission Set Groups (PSGs) in Full Depth

### 4.1 What They Are

A **Permission Set Group** bundles multiple permission sets into a single assignable container. Assigning a PSG to a user is equivalent to assigning every contained permission set (with the union semantics of Section 3.1). PSGs solve the "many fine-grained permission sets per user" management problem by allowing the admin to assign a single persona-named PSG (e.g., `Sales_Rep_NA`) that internally aggregates feature permission sets like `Account_Contact_Edit`, `Opportunity_Edit`, `Lead_Convert`, `Reports_Builder`. A permission set may belong to many PSGs simultaneously.

### 4.2 Muting Permission Sets

A **Muting Permission Set** is a special, group-scoped permission set whose role is *subtractive*: it disables (mutes) selected permissions that would otherwise be granted by other permission sets *within the same PSG*. Muting Permission Sets are the only mechanism in Salesforce for subtraction in the permissions model.

**Critical limitations:**

- A muting permission set **only mutes permissions inside its own permission set group**. It cannot mute permissions granted by the user's profile, by other permission sets assigned directly, or by other permission set groups. If the user has the same permission via the profile or another permission set/PSG, the user still has the permission despite the mute.
- A PSG can contain **at most one muting permission set**.
- Muting respects permission dependencies (e.g., muting "View All Data" without addressing dependent permissions can fail or trigger a dependency warning).

**Metadata representation:** The Tooling/Metadata API object is `MutingPermissionSet`. It is structurally a permission set marked as muting and linked to a single `PermissionSetGroup`. It is available in API v46.0 and later.

**Use cases:** Reusing a broad permission set across PSGs while suppressing a few permissions in one PSG (e.g., reuse the `Account_Contact_Full_Access` permission set across multiple PSGs but mute Delete and Modify All in the Sales Processing PSG); temporarily disabling permissions from a managed-package permission set while you wait to roll out a new feature.

### 4.3 The Calculated State

A PSG is not stored as a flat list of permissions — Salesforce **calculates** the effective permission set from the contained permission sets minus the mutes. This calculated artifact has a status visible on the Permission Set Groups list view:

- **Updated** — calculation is current; users may be assigned. *Only PSGs in Updated state can have user assignments inserted.* DML attempting to assign a user to an Outdated PSG fails with `INVALID_CROSS_REFERENCE_KEY: You can only assign users to permission set groups that have the "Updated" status`.
- **Outdated** — a contained permission set has changed since last calculation; recalculation is needed.
- **Updating** — recalculation is in progress (asynchronous).
- **Failed** — recalculation failed (often due to a managed-package permission set update or unsupported configuration); typically requires removing the offending permission set or contacting support.

You can force a recalculation via the UI button on the PSG, the Tooling API, or by saving an unrelated change.

---

## 5. Roles and the Role Hierarchy in Full Depth

### 5.1 What a Role Controls

A **Role** controls **record visibility — never actions**. A user's role determines which records *owned by other users* the user can see, on objects whose Organization-Wide Default is more restrictive than Public Read/Write. Roles are optional; a user without a role still gets full access via OWD/sharing rules but does not participate in hierarchy-based rollup.

### 5.2 The Role Hierarchy and Upward Visibility Rollup

Roles form a tree (each role has one parent, except the topmost). The fundamental rule: **a user automatically inherits read/write access to records owned by users in roles below them in the tree**, on objects that have OWD set to Private or Public Read Only. This is a *visibility rollup upward*; it does not flow sideways or downward.

The tree does not have to mirror the org chart precisely — Salesforce's official guidance is that each role represents a *level of data access*, not a job title. Sibling branches are isolated unless you explicitly bridge them with sharing rules, public groups, teams, or territories.

### 5.3 Grant Access Using Hierarchies

The **Grant Access Using Hierarchies** checkbox on the Sharing Settings page determines whether the role hierarchy applies to a given object:

- For **standard objects** (Account, Contact, Opportunity, Case, Lead, etc.) the checkbox is **always on and cannot be disabled**.
- For **custom objects**, you may **uncheck** Grant Access Using Hierarchies. When unchecked, the role hierarchy stops opening up access on that object — only the record owner, OWD-granted users, sharing-rule recipients, manual shares, Apex shares, and users with **View All / Modify All** (object) or **View All Data / Modify All Data** (system) still see the records. Activities associated with that object remain visible up the hierarchy regardless.

### 5.4 How Roles Differ Fundamentally from Profiles

Roles answer "*which records*"; Profiles answer "*which actions*". A user with profile = System Administrator and *no role* still sees every record (because View All Data bypasses sharing). A user with role = CEO and profile = Read Only sees every record but can edit none. Roles do not grant CRUD; profiles do not grant record visibility.

### 5.5 Standard Role Templates

Salesforce ships a sample role hierarchy template (commonly created in Developer Edition orgs and used in Trailhead) that includes:

- **CEO** (top)
- **CFO** (under CEO)
- **VP of Sales**, **VP of Marketing**, **VP of Customer Service & Support**, **VP of Human Resources**, **VP of Development** (under CEO)
- **Sales Director**, **Marketing Director**
- **Sales Manager** / **Sales Manager Eastern US** / **Sales Manager Western US**
- **Sales Representative** (Eastern, Western, International)
- **Channel Sales Team Manager**
- **Customer Support Manager**, **Customer Support Representative**

These are templates only. Real-world orgs replace them with hierarchies that reflect their actual data-visibility needs.

### 5.6 Limits and Recommended Depth

- **Maximum number of roles**: 500 in legacy orgs (orgs created before Spring '21). Orgs created in **Spring '21 or later** support up to **5,000** roles. The 500 limit can be raised by contacting Salesforce Support in legacy orgs.
- **Recommended maximum depth**: no more than **10 levels** in the hierarchy. Deeper trees degrade sharing-recalculation performance and complicate troubleshooting.

---

## 6. Record-Level Access — All Mechanisms

Record-level access is calculated by combining OWD (the floor), then opening it up via the role hierarchy, sharing rules, teams, manual sharing, Apex sharing, and implicit sharing — and then *narrowing* it via Restriction Rules and *filtering the default view* via Scoping Rules. Object permissions in the profile/permission set act as a **ceiling**: no record-level mechanism can grant more than the user has at the object level.

### 6.1 Organization-Wide Defaults (OWD)

OWD is the baseline for every object: it specifies what the most-restricted user sees for records they do not own. The available settings:

- **Private** — Only the record owner (and users above the owner in the role hierarchy, if Grant Access Using Hierarchies is on) can view, edit, and report.
- **Public Read Only** — All users with object read can view and report, but only the owner / higher-role users / those granted access via sharing can edit.
- **Public Read/Write** — All users with object read can view, edit, and report; only the owner can transfer (most objects) or delete.
- **Public Read/Write/Transfer** — Same as Public Read/Write plus all users can transfer ownership. *Available only on Lead and Case.*
- **Public Full Access** — Public Read/Write/Transfer plus delete. *Available only on Campaign.*
- **Controlled by Parent** — The child record inherits its parent's access. Mandatory on the detail side of a master-detail relationship; optional on Contact (under Account) and on Activity (Task/Event under their related parent).

Activities (Task/Event) have only two OWD options: **Controlled by Parent** and **Private**. The User object has its own OWD model (Private vs. Public Read Only) governing whether users see other users' detail pages.

#### Default Internal Access vs. Default External Access

OWD is split into two columns in modern orgs:

- **Default Internal Access** — applies to internal users (employees, regular Salesforce users).
- **Default External Access** — applies to external users (Experience Cloud customer/partner community users, guest users).

Rule: **Default External Access must be equal to or more restrictive than Default Internal Access.** This separation lets you keep records broadly visible internally (Public Read Only, say) while keeping them Private to community users.

Changing OWD triggers asynchronous **sharing recalculation**; the admin receives an email when complete.

### 6.2 Role Hierarchy Sharing

When OWD < Public Read/Write, the role hierarchy automatically opens up access to records *upward* — users in roles above the owner see (and can edit, on most standard objects) the owner's records. See Section 5 for full details. Disabled per-custom-object via Grant Access Using Hierarchies.

### 6.3 Sharing Rules

Sharing Rules grant *additional* access to groups of users beyond OWD and role hierarchy. They never restrict — only expand. Two flavors:

- **Owner-Based Sharing Rules** — "Records owned by users in [role/territory/public group A] are shared with users in [role/territory/public group B] at access level [Read Only | Read/Write]."
- **Criteria-Based Sharing Rules (CBS)** — "Records where [field] [op] [value] are shared with [group] at [Read Only | Read/Write]." Criteria-based rules cannot use lookup fields, encrypted fields, formula fields, or fields whose values derive from other fields.

**Limits:**

- **Up to 300 sharing rules per object** (combined owner-based and criteria-based).
- **Up to 50 criteria-based sharing rules per object** by default; customers can request increases up to 50 of them out of the 300 (limits have shifted; the canonical default is 50 CBS / 300 total).

**Sharing With Subordinates ("superiors_allowed")**: When you share to a Role, you choose between **Roles** (only that role) and **Roles and Subordinates** (that role and everyone below). The metadata equivalent for older API behavior is the `superiors_allowed` flag on `RoleAndSubordinatesInternal` / `RoleAndSubordinates` group references; sharing rules expose this via the picklist of sharing-target group types: *Role, Role and Subordinates, Role and Internal Subordinates, Role and Internal and Portal Subordinates, Public Group, Queue, Territory, Territory and Subordinates*.

If multiple sharing rules grant a user different access to the same record, the user receives the **most permissive** access.

### 6.4 Manual Sharing

The **Sharing** button on a record (or the equivalent in Lightning record details) lets the record's owner, anyone above the owner in the role hierarchy, and anyone with Modify All on the object (or Modify All Data system) share the single record with another user, role, role + subordinates, public group, or territory at Read Only or Read/Write.

**Critical behavior:** Manual shares are **automatically deleted when the record's owner changes**, or when the access granted is no longer beyond OWD. They do not survive ownership change. (This is one of the principal reasons custom Apex sharing reasons exist — they do survive.)

### 6.5 Account, Opportunity, and Case Teams

Teams are predefined record-collaboration constructs:

- **Account Team** — up to 5 default team members per user, with a Team Role and per-record Account/Contact/Opportunity/Case access (Read Only, Read/Write, or Private). Adding a team member creates AccountShare/OpportunityShare/CaseShare/ContactShare rows with `RowCause = Team`.
- **Opportunity Team** — same pattern at the Opportunity level.
- **Case Team** — predefined Case Team Roles drive automatic share entries on assignment.

Teams produce share records with `RowCause = Team` (or a more specific reason) and are recalculated on team-membership changes.

### 6.6 Apex Managed Sharing

Every object whose OWD is more restrictive than Public Read/Write has an associated **Share object**: `AccountShare`, `OpportunityShare`, `CaseShare`, `ContactShare`, and for custom objects `MyObject__Share`. The Share object has four columns:

- `ParentId` — the shared record
- `UserOrGroupId` — the recipient (User or Group)
- `AccessLevel` — Read, Edit, All
- `RowCause` — why the share exists

Possible `RowCause` values include: `Owner`, `Manual`, `Rule` (sharing rule), `ImplicitChild`, `ImplicitParent`, `Team`, `Territory`, `TerritoryManual`, `TerritoryRule`, `GuestRule`, and **custom Apex sharing reasons** (`MyReason__c` for custom objects).

**Custom Apex Sharing Reasons** are unique to custom objects (max 10 per custom object, only created via Setup or Metadata API on the custom object). They are critical because:

- A share row written with `RowCause = Manual` is **deleted on owner change** (just like UI manual shares).
- A share row written with `RowCause = MyReason__c` (a custom reason) **survives owner change**.
- Only users with **Modify All Data** can insert share rows with custom reasons.

Apex Managed Sharing is the recommended programmatic approach for complex sharing requirements (e.g., "share Loan__c with each User listed on its Participant__c child rows at the access level on the participant"). Trigger the share-row creation via Apex triggers and provide a class registered as the **Apex Sharing Recalculation** for the custom object (max 5 per custom object) so Salesforce can re-run it when the org needs to recompute access.

### 6.7 Restriction Rules (GA Summer '21)

A **Restriction Rule** narrows record visibility — it is the only declarative mechanism in Salesforce that *removes* access. Whereas all other sharing mechanisms can only grant, restriction rules subtract.

How they work: Define a User Criteria (which users the rule applies to, via field on User) and a Record Criteria (a filter on the target object). Users matching the User Criteria can only see records matching the Record Criteria. Records they would otherwise see — via OWD, hierarchy, sharing rules, manual share, etc. — disappear.

**Available objects (as of recent releases):** Tasks, Events, Contracts, Time Sheets, Time Sheet Entries, custom objects, and external objects, plus a steadily expanding list of standard objects. Limits: up to 2 active rules per object in Enterprise/Developer Edition, up to 5 in Performance/Unlimited.

Restriction rules apply to list views, related lists, lookups, search (including SOSL), reports, and SOQL — i.e., the user truly does not have read access to the filtered-out records.

The metadata object is `RestrictionRule` with `EnforcementType = Restrict`.

### 6.8 Scoping Rules (GA Winter '22)

A **Scoping Rule** is structurally similar to a Restriction Rule (same `RestrictionRule` Tooling API object, but with `EnforcementType = Scoping`). The crucial difference: **scoping rules do not restrict access — they only filter the default view**. The user can switch list-view scope ("All Accounts," etc.) and see records outside the rule. The rule's purpose is productivity — reducing noise — not security.

| Aspect | Restriction Rule | Scoping Rule |
|---|---|---|
| `EnforcementType` | `Restrict` | `Scoping` |
| Removes access? | Yes (permanent) | No (filter only) |
| Available in | List Views, Lookups, Related Lists, Search, SOSL, Reports, SOQL | List Views, Reports, SOQL |
| Use case | Hide sensitive records from a team | Default a team's view to relevant records |

Scoping Rules are available on Account, Case, Contact, Event, Lead, Opportunity, Task, and custom objects.

### 6.9 Implicit Sharing (Account ↔ Contact / Opportunity / Case)

Implicit sharing is built-in, system-managed sharing between Accounts and their child Contact, Opportunity, and Case records. It is **not configurable** — you cannot turn it on or off — but it materially affects access.

- **Parent implicit sharing**: If a user has read or read/write on an Opportunity, Case, or Contact, the user **automatically gets read access to the parent Account**, regardless of the Account's OWD. This is enforced by Salesforce's data-access policy: "if you can see a child, you can see the parent." Storage shows up in `AccountShare` with `RowCause = ImplicitParent`.
- **Child implicit sharing**: The Account owner (and users above in the role hierarchy) get access to the Account's child Contacts, Opportunities, and Cases. The level of access (View / Edit / None per child object) is configured at *role creation time* — each role specifies "Contact Access," "Opportunity Access," "Case Access" for accounts owned by users in this role. Storage was historically `ContactShare` / `OpportunityShare` / `CaseShare` with `RowCause = ImplicitChild`.

**Performance change (Winter '24+):** Salesforce **no longer stores `ImplicitChild` share rows** for Cases and Contacts (Spring '23 opt-in / Summer '23 release update) and Opportunities (Summer '23 opt-in). Instead, the platform computes implicit child access **dynamically** on access. Queries against `*Share` with `RowCause = ImplicitChild` return zero rows; use `UserRecordAccess` or `AccountShare` to determine access. This change dramatically improves account-sharing recalculation performance.

Implicit sharing **does not apply to custom objects** or to lookup-based parent relationships — only to the Account ↔ Contact/Opportunity/Case (and a few site/portal) relationships.

---

## 7. Field-Level Security in Full Depth

### 7.1 The Two Flags

For each field on each object, profiles and permission sets carry exactly two flags:

- **Read** — user can see the field's value
- **Edit** — user can change the field's value (implies Read)

The combinations resolve to three effective states:
- **Visible & Editable** (Read + Edit)
- **Read-Only** (Read only)
- **Hidden** (neither)

### 7.2 Where FLS Applies

FLS is enforced on:

- **Detail (record view) pages** — hidden fields disappear; read-only fields render as plain text.
- **Edit pages** — hidden fields are not in the form; read-only fields are disabled.
- **List views** — the column does not render for users without Read.
- **Related lists** — column suppressed.
- **Reports and Dashboards** — fields excluded from results and from the report builder's available-fields list.
- **Search results and lookup hover details** — field excluded.
- **API** (REST/SOAP/Bulk) — `FIELD_INTEGRITY_EXCEPTION` or silently stripped on read; the API enforces FLS unless the integration explicitly bypasses it (see Apex `WITH SECURITY_ENFORCED`, `Security.stripInaccessible`, and `WITH USER_MODE`).
- **SOQL** — the field can still be queried in Apex without `WITH SECURITY_ENFORCED`/`USER_MODE` because Apex by default runs in *system context for FLS* (this is a frequent source of FLS bugs in custom code).

### 7.3 FLS vs. Page Layout — Which Wins

When a field appears on a page layout but FLS hides it, **FLS wins**. The field is not visible on the layout. The opposite is also true: if FLS makes a field read-only, page-layout-level "editable" is overridden to read-only.

The general rule: **the more restrictive setting wins** between page-layout field properties and FLS. FLS is the security layer; the page layout is a presentation layer. You cannot grant access through a page layout — only restrict presentation of an already-FLS-accessible field.

### 7.4 The Hidden State

When FLS = Hidden:
- The field is absent everywhere — not on detail pages, not on edit pages, not in list view columns, not in related lists, not in report results, not in report builder field lists, not in search results, not in API responses (or it is null-stripped depending on the API and version).
- The field is not visible to any formula evaluation that runs in user mode and depends on the user reading it.
- Apex running in system mode (default) **still sees the field** — FLS is not enforced in Apex unless the developer opts in via `WITH SECURITY_ENFORCED`, `WITH USER_MODE`, or `Security.stripInaccessible`.

---

## 8. Interaction Between All Layers — The Theatrics

The most common source of confusion is how these layers compose. The exact rules:

### 8.1 Object Permissions Cap Sharing

Sharing mechanisms **cannot exceed** object-level CRUD. If your profile/permission sets give you Read but not Edit on Opportunity, no sharing rule, manual share, team, Apex share, or hierarchy can give you edit on an Opportunity record. Sharing rules are *additive within the object's CRUD ceiling*.

### 8.2 What View All Data / Modify All Data Bypass

- **Modify All Data** (system permission) — full CRUD on every record of every object, ignoring OWD, role hierarchy, sharing rules, restriction rules, manual sharing. Does NOT bypass FLS — the user still cannot see fields they have FLS=Hidden on. Does NOT bypass record-type-based create constraints. Does NOT bypass scoping-rules in the productivity sense (but the user can override the scope).
- **View All Data** — full read on every record of every object, ignoring sharing. Does NOT bypass FLS.
- **Modify All / View All** (object-level) — same effect but scoped to one object.

Restriction rules **do** apply to users with View All Data / Modify All Data unless those users are explicitly exempted in the rule's user criteria. (This is one of restriction rules' explicit features — they can constrain even highly privileged users.)

### 8.3 Scenario: Profile Grants Edit, OWD is Private, User is Not the Owner

- Profile/Permission Set: Opportunity = Edit (Create + Read + Edit)
- OWD: Opportunity = Private
- User does not own the record, has no role above the owner, no sharing rule applies, no manual share, no team membership, no Apex share.

**Outcome:** The user **cannot see or edit** the record. Object-level Edit grants the *capability* to edit Opportunities they have access to; OWD = Private ensures they have access to none they don't own. They will not even see the record in lists or searches. The "Edit" permission is necessary but not sufficient — record-level access is gated by OWD/sharing.

### 8.4 Scenario: Permission Set Grants More Than the Profile

- Profile: Account = Read
- Permission Set assigned: Account = Edit + Delete

**Outcome:** The user can Read, Edit, and Delete Accounts. The union wins. The profile's "Read only" is not subtractive — it does not block the permission set's grants. This is the foundational property that makes the permission-set-led model work.

### 8.5 No NotActions / Subtraction Concept (Except Muting Permission Sets)

Salesforce has no concept analogous to AWS IAM's NotAction or Azure RBAC's negative permissions. If the user has a permission anywhere in their profile or any assigned permission set, they have it, period. The **only** way to subtract is **Muting Permission Sets within a Permission Set Group**, and even those mute *only within their own PSG* — they cannot mute a permission granted by the profile or by an unrelated permission set.

### 8.6 Login Hours and Login IP Ranges Are Profile-Exclusive

Cannot be granted, expanded, or contracted by a permission set. If you need to gate login by IP for a subset of users, you must either (a) place them on a profile that enforces it, or (b) use the org-wide Network Access Trusted IP Ranges plus per-user IP-based Identity Verification.

### 8.7 Sharing Recalculation: Async, and What Happens to Queries

When you change OWD, sharing rules, role hierarchy, group membership, ownership, or anything that affects share rows, Salesforce kicks off an **asynchronous sharing recalculation**. The recalculation rewrites the relevant `*Share` rows. Behavior during recalc:

- Existing share rows are stable; the recalculation typically processes deltas.
- For very large changes (OWD changes on a high-volume object) you can receive an email when complete.
- Some Setup actions are **blocked** during recalculation (e.g., you cannot modify OWD while a sharing-rule recalc on that object is running, and vice versa). Sharing-rule modifications across Account ↔ Contact/Opportunity/Case lock together because of implicit-sharing recalc dependencies.
- Queries during recalc see the in-progress state — temporarily inconsistent visibility is possible. For this reason, OWD/sharing changes in production should be scheduled outside business hours on busy orgs.

---

## 9. Named Credentials and External Credentials

The modern Salesforce callout authentication model (Winter '23+) splits the older single Named Credential into two artifacts: **External Credential** (auth) and **Named Credential** (endpoint), connected via Permission Set–gated Principals.

### 9.1 External Credential

An External Credential carries the **authentication protocol** and the **principals** (auth identities) used to authenticate.

**Authentication protocols:**
- OAuth 2.0
- AWS Signature Version 4
- Custom (define your own auth header / parameters)
- Basic / Password (via Custom usually)
- JWT, mTLS variants depending on org

**Principal types** (Identity Type):
- **Named Principal** — one shared identity used by every authorized user. Single set of credentials/tokens, shared at the org level.
- **Per-User Principal** — each Salesforce user has their own credentials/tokens (relevant for password-style protocols only on selected versions).
- **OAuth 2.0 Per-User Principal** — each Salesforce user authenticates individually via OAuth and their own access/refresh tokens are stored. The user must complete the OAuth dance once before callouts succeed in their context.
- **Anonymous** — no credentials sent (rare; for unauthenticated endpoints).

Each Principal can carry **Authentication Parameters**, which are encrypted secret values (API keys, client IDs, client secrets, custom headers). They are referenced from Apex / formula contexts via `{!$Credential.<ExternalCredentialName>.<ParameterName>}`.

### 9.2 Named Credential

A Named Credential references exactly one External Credential and adds:

- **URL / endpoint**
- **Allowed namespaces / outbound network access**
- **Generate Authorization Header** flag (whether Salesforce auto-builds the Authorization header from the EC, or you build it via custom headers/formulas)
- **Allow merge fields in HTTP body / header** flags
- **Client Certificate** (for mTLS)

The Named Credential is what Apex code or Flow references in `callout:MyNamedCredential/path`. Authentication is resolved through the linked External Credential at callout time.

### 9.3 Permission Set Mappings on External Credentials

The External Credential exposes a **Permission Set Mapping** related list. Each mapping links a *Principal* of the External Credential to one or more *Permission Sets* (or Permission Set Groups, or Profiles).

**Effect:** Only users with at least one of those mapped permission sets / PSGs assigned can use that principal. This means **callouts via the Named Credential succeed only for authorized users**. If a user has no mapping that matches them, the callout fails with an authentication error. This is the modern way to gate which users can drive an integration.

The Permission Set Mapping is also where **Authentication Parameters** are bound to the principal — secret values are encrypted and stored at the mapping level.

### 9.4 Sequence Number for Multiple Mappings

If a user has multiple permission sets that each appear in different Permission Set Mappings on the same External Credential, Salesforce needs a tiebreaker to choose which principal/parameters to use. Each mapping has a **Sequence Number**; mappings are evaluated in ascending order, and the **lowest-numbered mapping the user matches wins**. This allows admins to express priority — e.g., a high-privilege team uses a more privileged principal even if they're also in a baseline permission set.

---

## 10. Worked Example: Sales Rep vs. Sales Manager vs. System Administrator

Consider an Opportunity record `Acme Q3 Renewal`:
- Owner: Alice (Sales Rep, North America East)
- Amount: $250,000
- Account: Acme Corp (owned by Alice)

Org configuration:
- **OWD**: Opportunity = Private (Internal and External). Account = Public Read Only Internal / Private External.
- **Role Hierarchy**: CEO → VP Sales → Sales Manager NA → Sales Manager NA East → Sales Rep NA East. Grant Access Using Hierarchies = on (standard objects).
- **Profiles**: All three users start on `Minimum Access – Salesforce`.
- **Permission Set Group `Sales_Rep`** assigned to Alice: contains `Account_Edit`, `Opportunity_Edit`, `Lead_Convert`, `Reports_Run`, `Standard_Sales_App`.
- **Permission Set Group `Sales_Manager`** assigned to Bob (Sales Manager NA East): contains everything in `Sales_Rep` plus `Forecast_Manage`, `Opportunity_Discount_Approve`, `Opportunity_Reassign`. Muting Permission Set in this PSG mutes "Delete" on Opportunity (managers cannot delete).
- **System Administrator** profile assigned to Carol (no role).
- **Sharing Rule**: Owner-based — "Opportunities owned by Sales Rep NA East are shared with the Sales Operations public group, Read Only."

Now trace each user against `Acme Q3 Renewal`:

**Alice (Sales Rep NA East, owner of the Opportunity):**
- Profile/PSG: object Edit on Opportunity → permission to edit.
- Record-level: she's the owner → full Read/Write (transfer/delete as the owner).
- FLS: she sees all fields her PSG grants Read on, edits those it grants Edit on.
- Result: **Full edit access** to the Opportunity.

**Bob (Sales Manager NA East, *above* Alice in the hierarchy):**
- Profile/PSG: object Edit on Opportunity (Delete muted by the muting permission set in `Sales_Manager` PSG).
- Record-level: OWD = Private, so by default no one but Alice sees it; but Grant Access Using Hierarchies = on for Opportunity, and Bob's role is above Alice's → Bob automatically receives Read/Write via hierarchy rollup.
- The implicit-parent rule additionally grants Bob Read on the parent Account, although the Account is already Public Read Only.
- Bob can edit but **cannot delete** (muting permission set blocks Delete on Opportunity within the PSG; nothing else grants Delete to Bob, so the mute is effective).
- Result: **Read/Write but not Delete** on the Opportunity, plus implicit Read on Acme Corp.

**Carol (System Administrator, no role):**
- Profile: System Administrator includes **View All Data** and **Modify All Data** system permissions.
- These bypass OWD, hierarchy, sharing rules, restriction rules' default behavior (subject to whether a restriction rule explicitly excludes admins).
- Result: **Full Read/Write/Delete/Transfer** on every Opportunity in the org, including Acme Q3 Renewal. FLS still applies — if any field has Carol as Hidden in her profile/permission sets (rare on the System Administrator profile), she still cannot see it. In practice System Administrator carries Read+Edit on virtually every field.

**A fourth user, Dave (Sales Operations, public group `Sales_Ops`, role = Sales Operations on a sibling branch):**
- Profile/PSG: similar Sales-Ops PSG with Read on Opportunity but not Edit.
- Record-level: not in Alice's branch, but the criteria-based / owner-based sharing rule shares the record at Read Only with the Sales Operations public group → Dave gets Read.
- Result: **Read only** on Acme Q3 Renewal. The sharing rule's Read/Write would be capped by Dave's object-level Read anyway.

---

## 11. Architectural Best Practices

### 11.1 The Permission-Set-Led Model

The modern Salesforce architecture is:
1. **Few profiles** (3–6 minimum-access profiles for distinct user-license categories: e.g., `Minimum Access – Salesforce`, `Minimum Access – Platform`, `Minimum Access – API Only Integrations`, plus community equivalents).
2. **Many small, capability-named permission sets** (`Opportunity_Edit`, `Discount_Approve`, `Knowledge_Author`, `Reports_Builder`).
3. **Permission Set Groups for personas** (`Sales_Rep_NA`, `Service_Agent_T1`, `Solution_Architect`).
4. **Muting Permission Sets** to subtract within PSGs where reuse demands it.
5. **Time-boxed assignments** via Assignment Expiration for any elevated or temporary access.
6. **User Access Policies** (Beta → GA) to automate persona assignment on user create/update.
7. **Custom Permissions** in formula fields and validation rules instead of `$Profile.Name = 'X'` checks (which break when profiles consolidate).

### 11.2 When to Use PSGs vs. Individual Permission Sets

- Use a **single permission set** assignment when the capability is narrow, cross-persona, and is added/removed independently (e.g., `Reports_Export`).
- Use a **Permission Set Group** when a *job function* is a stable bundle of multiple permission sets that should be assigned/revoked atomically. PSGs reduce assignment management cost dramatically as personas evolve.
- Use a **Muting Permission Set** when you want to reuse a broad permission set across multiple PSGs but suppress a few permissions in one PSG.

### 11.3 Integration User Patterns

The recommended pattern (Spring '24+):
1. Use a **Salesforce Integration** user license (5 free per Enterprise/Unlimited/Performance org; 1 in Developer Edition).
2. Assign the **Minimum Access – API Only Integrations** profile (cloned per integration if you need separation). API Enabled and API Only flags are forced TRUE.
3. Create a dedicated **user per integration** (do not share integration users).
4. Stack permission sets / PSGs that grant only the precise objects, fields, Apex classes, and Named Credentials the integration needs.
5. Treat the profile as immutable: keep all permissions in permission sets so you can swap integrations without profile rework.

### 11.4 Role Hierarchy Design Principles

- Model **data visibility**, not the org chart.
- Keep depth ≤ 10.
- Flatten where possible — every additional level multiplies sharing-recalc cost.
- Place high-volume external users (HVPU) outside the role hierarchy entirely; use sharing sets and share groups instead.
- Pair roles with **public groups** for cross-branch sharing rather than bending the hierarchy.
- Pair roles with **territories** if you have a matrix sales structure where users report into multiple managers.

### 11.5 Avoiding Profile Sprawl

Indicators of sprawl: more than ~10 custom profiles for internal users; profiles that differ from each other only by one or two permissions; profile names that encode a person's job ("Senior Eastern Sales Discount Approver"). Cure: consolidate to minimum-access profiles, migrate the deltas to permission sets, retire redundant profiles. Tools: User Access Policies (to migrate users en masse), the User Access Summary and Permission Set Group Summary views, the Permission Set Helper AppExchange package.

---

## 12. Summary of Key Rules and Interactions

1. **Every user has exactly one Profile, zero or one Role, and zero or many Permission Sets / Permission Set Groups.** Profile is mandatory; role and permission sets are optional.
2. **Profile and permission sets compose by union (logical OR) — never subtraction.** The only subtraction mechanism is Muting Permission Sets within a Permission Set Group, and they only mute within their own group.
3. **Profile answers "what can I do"; Role answers "what can I see"; permission sets extend "what can I do."**
4. **Object permissions cap record sharing.** Sharing can never grant more than the object-level CRUD already permits.
5. **OWD is the floor of record visibility.** Sharing rules, hierarchy, manual share, teams, and Apex share open it up; Restriction Rules narrow it; Scoping Rules just filter the default view.
6. **Default External Access ≤ Default Internal Access** in OWD.
7. **Grant Access Using Hierarchies is always on for standard objects, optional for custom objects.**
8. **Maximum 300 sharing rules per object, of which up to 50 may be criteria-based.** Up to 5,000 roles in modern orgs (500 in legacy), recommended hierarchy depth ≤ 10.
9. **Manual shares (and `RowCause = Manual` Apex shares) are deleted on owner change. Custom Apex Sharing Reasons survive owner change.** Up to 10 custom reasons per custom object; only Modify All Data users can write them.
10. **View All Data / Modify All Data bypass OWD, role hierarchy, sharing rules, manual share, and (without explicit exemption) most things — but they do NOT bypass FLS.**
11. **FLS has only Read and Edit. FLS wins over page layout. The Hidden state suppresses the field everywhere it could appear.**
12. **Login Hours, Login IP Ranges, page-layout assignments, default record types, default app, password policies are profile-exclusive.** Permission sets cannot grant or modify them.
13. **Permission Set Groups are calculated.** Status must be **Updated** to assign users; Outdated/Updating/Failed states block assignment. PSG calculation is async.
14. **Account ↔ Contact / Opportunity / Case implicit sharing is automatic and not configurable.** Parent implicit sharing grants Read on the parent Account when a user has access to a child. Child implicit sharing grants the Account owner role-configured access to children. As of Spring '23 / Summer '23, child implicit shares are computed dynamically and no longer materialized as `*Share` rows for Cases, Contacts, and Opportunities.
15. **Restriction Rules (`EnforcementType = Restrict`) actually remove access; Scoping Rules (`EnforcementType = Scoping`) only filter default views.** Both use the same `RestrictionRule` Tooling API object.
16. **Modern callouts use External Credential + Named Credential.** External Credentials carry the auth protocol and principals (Named Principal, Per-User Principal, OAuth Per-User Principal). Named Credentials carry the endpoint and reference an External Credential. **Permission Set Mappings** on the External Credential gate which users may use which principal; **Sequence Number** breaks ties when multiple mappings apply.
17. **Sharing recalculation is asynchronous** and may temporarily yield inconsistent visibility; OWD changes lock related sharing-rule edits and vice versa across Account and its children.
18. **The recommended modern model is permission-set-led**: Minimum Access – Salesforce profile + capability-named permission sets + persona-named Permission Set Groups + (where reused) Muting Permission Sets, with Assignment Expiration for time-boxed access and User Access Policies for automated persona assignment.

This composite of declarative artifacts — Profiles, Permission Sets, Permission Set Groups, Muting Permission Sets, Roles, OWD, Role Hierarchy, Sharing Rules, Manual Shares, Teams, Apex Shares, Restriction Rules, Scoping Rules, Implicit Sharing, FLS, External and Named Credentials — together constitutes the Salesforce roles and permissions architecture. Designed correctly, it scales from small teams to multi-thousand-user enterprise deployments while satisfying least-privilege, audit, and compliance obligations.
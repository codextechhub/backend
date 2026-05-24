# Global Permission Actions

These are the canonical action verbs used as the **suffix** of every permission key (`module.resource.<action>`).
Seed `PermissionAction` records in this exact order — actions have no dependencies on anything else,
so they are always the **first thing you create** in a fresh build.

Each `name` becomes the `PermissionAction.name` primary key and appears verbatim in permission keys.

---

## Core read / write

| name      | description                                                                 |
|-----------|-----------------------------------------------------------------------------|
| `view`    | Read or list records. Required by virtually every other action as a dep.    |
| `create`  | Create a new record.                                                        |
| `update`  | Modify an existing record's fields.                                         |
| `delete`  | Permanently remove a record (hard-delete or irreversible soft-delete).      |
| `manage`  | Full control over a resource — implies view + create + update + delete.     |

---

## Approval & lifecycle

| name         | description                                                              |
|--------------|--------------------------------------------------------------------------|
| `approve`    | Ratify or authorise a submitted record (scores, invoices, leave, etc.).  |
| `reject`     | Decline or push back a submitted record with a reason.                   |
| `publish`    | Make a record visible to its intended audience (results, timetables).    |
| `archive`    | Move a record to an archived / read-only state without hard deletion.    |
| `suspend`    | Temporarily deactivate an account or entity.                             |
| `reactivate` | Restore a previously suspended or deactivated entity.                    |

---

## Data transfer & movement

| name       | description                                                                |
|------------|----------------------------------------------------------------------------|
| `export`   | Download data to CSV, XLSX, or PDF.                                        |
| `import`   | Bulk-upload records from a file.                                           |
| `transfer` | Move a record between owners, branches, or contexts.                       |
| `assign`   | Link a resource to another entity (e.g. student → class, user → route).   |

---

## Specialised write operations

| name         | description                                                              |
|--------------|--------------------------------------------------------------------------|
| `record`     | Log a transaction or event entry (payments, visits, attendance marks).   |
| `enter`      | Input data into a form field or score sheet (assessment scores, grades). |
| `mark`       | Mark attendance, mark a record as done/reviewed.                         |
| `verify`     | Confirm the authenticity of submitted evidence (payment proof, docs).    |
| `reverse`    | Undo or void a previously recorded financial or operational transaction. |
| `confirm`    | Finalise a pending action (confirm enrolment, confirm booking).          |
| `process`    | Move a record through a workflow step (applications, admissions).        |
| `generate`   | Produce a document or report on demand (ID cards, report cards).         |

---

## Communication

| name    | description                                                                   |
|---------|-------------------------------------------------------------------------------|
| `send`  | Dispatch a message — SMS, email, or push notification.                        |
| `post`  | Publish an announcement or bulletin to a board or feed.                       |

---

## Tracking & observation

| name      | description                                                               |
|-----------|---------------------------------------------------------------------------|
| `track`   | Record attendance or presence at an event or location.                    |
| `report`  | Generate a summary or analytics report (distinct from raw data export).   |

---

## Finance-specific

| name      | description                                                               |
|-----------|---------------------------------------------------------------------------|
| `waive`   | Grant a full or partial exemption from a fee or charge.                   |
| `apply`   | Apply for something on behalf of self (leave requests, fee waivers).      |

---

## Library-specific

| name     | description                                                                |
|----------|----------------------------------------------------------------------------|
| `return` | Record the return of a borrowed item.                                      |

---

## Platform / DevOps

| name          | description                                                           |
|---------------|-----------------------------------------------------------------------|
| `impersonate` | Act as another user for audited support diagnostics (platform only).  |
| `trigger`     | Initiate a deployment, job, or pipeline run.                          |
| `run`         | Execute a migration, script, or background task.                      |
| `escalate`    | Escalate a support ticket or incident to a higher tier.               |

---

## Seed order note

When running `PermissionAction.objects.get_or_create(name=...)`, the `name` field is the PK.
Seed all actions before touching `Permission`, `PermissionModule`, or `PermissionResource` —
nothing depends on actions, but `Permission` cannot be created without them.

**Minimum set required to create any permission:**
`view`, `create`, `update`, `delete`, `manage`

Add the rest as you define resources that need them.

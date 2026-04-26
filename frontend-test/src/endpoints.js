// Central API endpoint registry.
// All paths live here — update once, takes effect everywhere.
//
// Root prefix mapping (from apps/urls.py):
//   /v1/user/   → vs_user
//   /v1/i/      → vs_schools
//   /v1/rbac/   → vs_rbac
//   /v1/audit/  → vs_audit
//   /v1/admin/  → vs_admin_console
//   /v1/import/ → vs_import_data  (not yet wired in root urls.py)

export const EP = {

  // ── Authentication ──────────────────────────────────────────────────────────
  AUTH_LOGIN:                        "/v1/user/auth/login/",
  AUTH_LOGOUT:                       "/v1/user/auth/logout/",
  AUTH_REFRESH:                      "/v1/user/auth/token/refresh/",

  // Account activation (UUID-keyed invite flow)
  AUTH_ACTIVATE_PREVIEW:             (key) => `/v1/user/auth/activate/${key}/preview/`,
  AUTH_ACTIVATE:                     (key) => `/v1/user/auth/activate/${key}/`,

  // Password management
  AUTH_PASSWORD_CHANGE:              "/v1/user/auth/password/change/",
  AUTH_PASSWORD_RESET_REQUEST:       "/v1/user/auth/password/reset/request/",
  AUTH_PASSWORD_RESET_PREVIEW:       (key) => `/v1/user/auth/reset-password/${key}/preview/`,
  AUTH_PASSWORD_RESET_CONFIRM:       (key) => `/v1/user/auth/password/reset/${key}/confirm/`,

  // ── Users ───────────────────────────────────────────────────────────────────
  USERS:                             "/v1/user/users/",
  USER:                              (id)  => `/v1/user/users/${id}/`,

  // User actions — NOTE: these are NOT nested under /users/, they sit directly
  // under /v1/user/<user_id>/ per vs_user/urls.py path definitions
  USER_EMAIL_CHANGE:                 (id)  => `/v1/user/${id}/email/change/`,
  USER_INVITE_RESEND:                (id)  => `/v1/user/${id}/invite/resend/`,
  USER_SUSPEND:                      (id)  => `/v1/user/${id}/suspend/`,
  USER_REACTIVATE:                   (id)  => `/v1/user/${id}/reactivate/`,
  USER_UNLOCK:                       (id)  => `/v1/user/${id}/unlock/`,
  USER_PASSWORD_RESET:               (id)  => `/v1/user/${id}/password-reset/`,

  // ── Sessions & Auth Events ──────────────────────────────────────────────────
  SESSIONS:                          "/v1/user/sessions/",
  SESSION:                           (id)  => `/v1/user/sessions/${id}/`,
  AUTH_ATTEMPTS:                     "/v1/user/auth-attempts/",
  AUTH_ATTEMPT:                      (id)  => `/v1/user/auth-attempts/${id}/`,
  ACCOUNT_LOCKOUTS:                  "/v1/user/account-lockouts/",
  ACCOUNT_LOCKOUT:                   (id)  => `/v1/user/account-lockouts/${id}/`,
  AUTH_EVENTS:                       "/v1/user/auth-events/",
  AUTH_EVENT:                        (id)  => `/v1/user/auth-events/${id}/`,

  // ── Schools ─────────────────────────────────────────────────────────────────
  SCHOOLS:                           "/v1/i/",
  SCHOOLS_CREATE:                    "/v1/i/create/",
  SCHOOLS_STATS:                     "/v1/i/stats/",
  PACKAGE_PLANS:                     "/v1/i/package-plans/",
  MODULES:                           "/v1/i/modules/",

  // School record operations
  SCHOOL:                            (slug) => `/v1/i/${slug}/`,
  SCHOOL_UPDATE:                     (slug) => `/v1/i/${slug}/update/`,
  SCHOOL_RESET_CONFIG:               (slug) => `/v1/i/${slug}/reset-config/`,

  // ── Branches ────────────────────────────────────────────────────────────────
  BRANCHES:                          (slug)        => `/v1/i/${slug}/branches/`,
  BRANCHES_CREATE:                   (slug)        => `/v1/i/${slug}/branches/create/`,
  BRANCHES_STATS:                    (slug)        => `/v1/i/${slug}/branches/stats/`,
  BRANCH:                            (slug, code)  => `/v1/i/${slug}/branches/${code}/detail/`,
  BRANCH_UPDATE:                     (slug, code)  => `/v1/i/${slug}/branches/${code}/update/`,
  BRANCH_TRANSITION:                 (slug, code)  => `/v1/i/${slug}/branches/${code}/transition/`,

  // ── RBAC — Vision permissions ───────────────────────────────────────────────
  PERMISSIONS:                       "/v1/rbac/vision/permissions/",
  PERMISSION:                        (key) => `/v1/rbac/vision/permissions/${key}/`,
  PERMISSION_DEPENDENCIES:           "/v1/rbac/vision/permission-dependencies/",
  PERMISSION_DEPENDENCY:             (id)  => `/v1/rbac/vision/permission-dependencies/${id}/`,

  // Vision permission groups (shared across school + platform roles)
  PERMISSION_GROUPS:                 "/v1/rbac/vision/permission-groups/",
  PERMISSION_GROUP:                  (id)  => `/v1/rbac/vision/permission-groups/${id}/`,

  // ── RBAC — School-scoped roles & assignments ────────────────────────────────
  SCHOOL_ROLES:                      (school) => `/v1/rbac/schools/${school}/roles/`,
  SCHOOL_ROLE:                       (school, id) => `/v1/rbac/schools/${school}/roles/${id}/`,
  SCHOOL_ROLE_ASSIGNMENTS:           (school) => `/v1/rbac/schools/${school}/role-assignments/`,
  SCHOOL_ROLE_ASSIGNMENT:            (school, id) => `/v1/rbac/schools/${school}/role-assignments/${id}/`,

  // School → Vision role change requests
  SCHOOL_ROLE_CHANGE_REQUESTS:       (school) => `/v1/rbac/schools/${school}/role-change-requests/`,

  // ── RBAC — Vision review queue (role change requests) ───────────────────────
  VISION_ROLE_CHANGE_REQUESTS:       "/v1/rbac/vision/role-change-requests/",
  VISION_ROLE_CHANGE_REQUEST:        (id)  => `/v1/rbac/vision/role-change-requests/${id}/`,
  VISION_ROLE_CHANGE_REQUEST_DECIDE: (id)  => `/v1/rbac/vision/role-change-requests/${id}/decide/`,

  // ── RBAC — Platform (internal staff) roles & assignments ────────────────────
  PLATFORM_ROLES:                    "/v1/rbac/platform/roles/",
  PLATFORM_ROLE:                     (id)  => `/v1/rbac/platform/roles/${id}/`,
  PLATFORM_ROLE_ASSIGNMENTS:         "/v1/rbac/platform/role-assignments/",
  PLATFORM_ROLE_ASSIGNMENT:          (id)  => `/v1/rbac/platform/role-assignments/${id}/`,
  PLATFORM_CHANGE_REQUESTS:          "/v1/rbac/platform/role-change-requests/",
  PLATFORM_CHANGE_REQUEST:           (id)  => `/v1/rbac/platform/role-change-requests/${id}/`,
  PLATFORM_CHANGE_REQUEST_DECIDE:    (id)  => `/v1/rbac/platform/role-change-requests/${id}/decide/`,

  // ── Audit ───────────────────────────────────────────────────────────────────
  AUDIT_EVENTS:                      "/v1/audit/events/",
  AUDIT_EVENT:                       (id)  => `/v1/audit/events/${id}/`,
  AUDIT_ENTITY_TRAIL:                (entityType, entityId) => `/v1/audit/entity-trails/${entityType}/${entityId}/`,
  AUDIT_EXPORTS:                     "/v1/audit/exports/",
  AUDIT_EXPORT:                      (id)  => `/v1/audit/exports/${id}/`,
  AUDIT_COMPLIANCE_RULES:            "/v1/audit/compliance-rules/",
  AUDIT_COMPLIANCE_RULE:             (id)  => `/v1/audit/compliance-rules/${id}/`,

  // ── Admin Console ───────────────────────────────────────────────────────────
  ADMIN_DASHBOARD:                   "/v1/admin/dashboard/",
  ADMIN_IMPERSONATIONS:              "/v1/admin/impersonations/",
  ADMIN_IMPERSONATION:               (id)  => `/v1/admin/impersonations/${id}/`,

  // ── Data Import  (vs_import_data — not yet wired in root urls.py) ───────────
  IMPORT_TEMPLATES:                  "/v1/import/system-import-templates/",
  IMPORT_TEMPLATE:                   (tid) => `/v1/import/system-import-templates/${tid}/`,
  IMPORT_TEMPLATE_DOWNLOAD:          (tid) => `/v1/import/system-import-templates/${tid}/download/`,

  IMPORT_BATCHES:                    (schoolId) => `/v1/import/schools/${schoolId}/imports/batches/`,
  IMPORT_BATCH:                      (schoolId, batchId) => `/v1/import/schools/${schoolId}/imports/batches/${batchId}/`,
  IMPORT_BATCH_VALIDATE:             (schoolId, batchId) => `/v1/import/schools/${schoolId}/imports/batches/${batchId}/validate/`,
  IMPORT_BATCH_REVALIDATE:           (schoolId, batchId) => `/v1/import/schools/${schoolId}/imports/batches/${batchId}/revalidate/`,
  IMPORT_BATCH_START:                (schoolId, batchId) => `/v1/import/schools/${schoolId}/imports/batches/${batchId}/start-import/`,

  IMPORT_ISSUES:                     (schoolId, batchId) => `/v1/import/schools/${schoolId}/imports/batches/${batchId}/issues/`,
  IMPORT_ISSUE:                      (schoolId, batchId, issueId) => `/v1/import/schools/${schoolId}/imports/batches/${batchId}/issues/${issueId}/`,
  IMPORT_ISSUE_RESOLVE:              (schoolId, batchId, issueId) => `/v1/import/schools/${schoolId}/imports/batches/${batchId}/issues/${issueId}/resolve/`,
  IMPORT_CORRECTIONS:                (schoolId, batchId) => `/v1/import/schools/${schoolId}/imports/batches/${batchId}/corrections/`,

  IMPORT_JOBS:                       (schoolId, batchId) => `/v1/import/schools/${schoolId}/imports/batches/${batchId}/jobs/`,
  IMPORT_JOB:                        (schoolId, batchId, jobId) => `/v1/import/schools/${schoolId}/imports/batches/${batchId}/jobs/${jobId}/`,
  IMPORT_JOB_ROLLBACK:               (schoolId, batchId, jobId) => `/v1/import/schools/${schoolId}/imports/batches/${batchId}/jobs/${jobId}/rollback/`,
  IMPORT_ROLLBACK_RECORDS:           (schoolId, batchId, jobId) => `/v1/import/schools/${schoolId}/imports/batches/${batchId}/jobs/${jobId}/rollbacks/`,

  IMPORT_AUDIT_LOGS:                 (schoolId, batchId) => `/v1/import/schools/${schoolId}/imports/batches/${batchId}/audit-logs/`,
  IMPORT_NOTIFICATIONS:              (schoolId, batchId) => `/v1/import/schools/${schoolId}/imports/batches/${batchId}/notifications/`,
};

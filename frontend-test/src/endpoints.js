// Central API endpoint registry.
// All paths live here — update once, takes effect everywhere.

export const EP = {

  // ── Auth ────────────────────────────────────────────────────────────────────
  AUTH_LOGIN:                      "/v1/user/auth/login/",
  AUTH_LOGOUT:                     "/v1/user/auth/logout/",
  AUTH_REFRESH:                    "/v1/user/auth/token/refresh/",
  AUTH_PASSWORD_RESET:             "/v1/user/auth/password/reset/",

  // ── Users ───────────────────────────────────────────────────────────────────
  USERS:                           "/v1/user/users/",
  USER:                            (id)     => `/v1/user/users/${id}/`,
  USER_SUSPEND:                    (id)     => `/v1/user/users/${id}/suspend/`,
  USER_REACTIVATE:                 (id)     => `/v1/user/users/${id}/reactivate/`,
  USER_UNLOCK:                     (id)     => `/v1/user/users/${id}/unlock/`,

  // ── Invitations ─────────────────────────────────────────────────────────────
  INVITATIONS:                     "/v1/user/invitations/",
  INVITATION_RESEND:               (id)     => `/v1/user/invitations/${id}/resend/`,

  // ── Sessions & Auth Events ──────────────────────────────────────────────────
  SESSIONS:                        "/v1/user/sessions/",
  SESSION:                         (id)     => `/v1/user/sessions/${id}/`,
  AUTH_ATTEMPTS:                   "/v1/user/auth-attempts/",
  ACCOUNT_LOCKOUTS:                "/v1/user/account-lockouts/",
  AUTH_EVENTS:                     "/v1/user/auth-events/",

  // ── Schools (internal) ──────────────────────────────────────────────────────
  SCHOOLS:                         "/v1/i/",
  SCHOOLS_CREATE:                  "/v1/i/create/",
  SCHOOL:                          (slug)   => `/v1/i/${slug}/`,
  SCHOOL_SUBSCRIPTION:             (slug)   => `/v1/i/${slug}/subscription/`,

  // ── Branches (internal) ─────────────────────────────────────────────────────
  BRANCHES:                        (slug)        => `/v1/i/${slug}/branches/`,
  BRANCH_CREATE:                   (slug)        => `/v1/i/${slug}/branches/create/`,
  BRANCH:                          (slug, code)  => `/v1/i/${slug}/branches/${code}/detail/`,
  BRANCH_UPDATE:                   (slug, code)  => `/v1/i/${slug}/branches/${code}/update/`,
  BRANCH_TRANSITION:               (slug, code)  => `/v1/i/${slug}/branches/${code}/transition/`,

  // ── Package plans & modules ─────────────────────────────────────────────────
  PACKAGE_PLANS:                   "/v1/i/package-plans/",
  MODULES:                         "/v1/i/modules/",

  // ── RBAC – Platform staff ───────────────────────────────────────────────────
  PLATFORM_ROLES:                  "/v1/rbac/platform/roles/",
  PLATFORM_ROLE:                   (id)     => `/v1/rbac/platform/roles/${id}/`,
  PLATFORM_ROLE_ASSIGNMENTS:       "/v1/rbac/platform/role-assignments/",
  PLATFORM_ROLE_ASSIGNMENT:        (id)     => `/v1/rbac/platform/role-assignments/${id}/`,
  PLATFORM_CHANGE_REQUESTS:        "/v1/rbac/platform/role-change-requests/",
  PLATFORM_CHANGE_REQUEST_DECIDE:  (id)     => `/v1/rbac/platform/role-change-requests/${id}/decide/`,

  // ── RBAC – Vision (permissions & groups) ────────────────────────────────────
  PERMISSIONS:                     "/v1/rbac/vision/permissions/",
  PERMISSION:                      (key)    => `/v1/rbac/vision/permissions/${key}/`,
  PERMISSION_GROUPS:               "/v1/rbac/vision/permission-groups/",
  PERMISSION_GROUP:                (id)     => `/v1/rbac/vision/permission-groups/${id}/`,

  // ── Audit ───────────────────────────────────────────────────────────────────
  AUDIT_EVENTS:                    "/v1/audit/events/",
  AUDIT_EVENTS_FILTER:             (type)   => `/v1/audit/events/${type ? `?action_type=${type}` : ""}`,
};

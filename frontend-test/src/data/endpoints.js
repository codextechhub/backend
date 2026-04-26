export const GRP = {
  AUTH:   { ic: '🔐', lb: 'Auth' },
  USERS:  { ic: '👤', lb: 'Users' },
  SESS:   { ic: '📡', lb: 'Sessions' },
  INST:   { ic: '🏫', lb: 'Institutions' },
  BRANCH: { ic: '🌿', lb: 'Branches' },
  PKG:    { ic: '📦', lb: 'Packages' },
  RBAC:   { ic: '🛡️', lb: 'RBAC' },
  AUDIT:  { ic: '📋', lb: 'Audit' },
  IMPORT: { ic: '📥', lb: 'Imports' },
  ADMIN:  { ic: '⚙️', lb: 'Admin' },
};

export const EPS = [
  // AUTH
  { g:'AUTH', m:'POST', l:'Login',                  p:'/v1/user/auth/login/',                          b:'{\n  "email": "admin@example.com",\n  "password": "yourpassword"\n}',                               na:true,  rt:'auth'    },
  { g:'AUTH', m:'POST', l:'Refresh token',           p:'/v1/user/auth/token/refresh/',                  b:'{\n  "refresh": ""\n}',                                                                               na:true,  rt:'token'   },
  { g:'AUTH', m:'POST', l:'Logout',                  p:'/v1/user/auth/logout/',                          b:'{\n  "refresh": ""\n}',                                                                                         rt:'action'  },
  { g:'AUTH', m:'POST', l:'Change password',         p:'/v1/user/auth/password/change/',                b:'{\n  "old_password": "",\n  "new_password": ""\n}',                                                             rt:'action'  },
  { g:'AUTH', m:'POST', l:'Request password reset',  p:'/v1/user/auth/password/reset/request/',         b:'{\n  "email": ""\n}',                                                                                 na:true,  rt:'action'  },

  // USERS
  { g:'USERS', m:'GET',   l:'List users',      p:'/v1/user/users/',                                                                                                                                                        rt:'list'    },
  { g:'USERS', m:'GET',   l:'Get user',        p:'/v1/user/users/{user_id}/',                                                                                                                                               rt:'detail'  },
  { g:'USERS', m:'PATCH', l:'Update user',     p:'/v1/user/users/{user_id}/',                           b:'{\n  "first_name": "",\n  "last_name": ""\n}',                                                                   rt:'detail'  },
  { g:'USERS', m:'POST',  l:'Suspend user',    p:'/v1/user/users/{user_id}/suspend/',                   b:'{\n  "reason": ""\n}',                                                                                            rt:'action'  },
  { g:'USERS', m:'POST',  l:'Reactivate user', p:'/v1/user/users/{user_id}/reactivate/',                                                                                                                                    rt:'action'  },
  { g:'USERS', m:'POST',  l:'Unlock user',     p:'/v1/user/users/{user_id}/unlock/',                                                                                                                                        rt:'action'  },
  { g:'USERS', m:'POST',  l:'Reset password',  p:'/v1/user/users/{user_id}/password-reset/',                                                                                                                                rt:'action'  },
  { g:'USERS', m:'POST',  l:'Resend invite',   p:'/v1/user/users/{user_id}/invite/resend/',                                                                                                                                 rt:'action'  },

  // SESSIONS
  { g:'SESS', m:'GET', l:'List sessions',    p:'/v1/user/sessions/',        rt:'list' },
  { g:'SESS', m:'GET', l:'Auth attempts',    p:'/v1/user/auth-attempts/',   rt:'list' },
  { g:'SESS', m:'GET', l:'Account lockouts', p:'/v1/user/account-lockouts/',rt:'list' },
  { g:'SESS', m:'GET', l:'Auth events',      p:'/v1/user/auth-events/',     rt:'list' },

  // INSTITUTIONS
  { g:'INST', m:'GET',   l:'List schools',    p:'/v1/i/schools/',                                                                                                                                                          rt:'list'    },
  { g:'INST', m:'POST',  l:'Create school',   p:'/v1/i/schools/',             b:'{\n  "name": "",\n  "slug": "",\n  "country": "NG",\n  "timezone": "Africa/Lagos"\n}',                                                    rt:'created' },
  { g:'INST', m:'GET',   l:'Get school',      p:'/v1/i/schools/{slug}/',                                                                                                                                                   rt:'detail'  },
  { g:'INST', m:'PATCH', l:'Update school',   p:'/v1/i/schools/{slug}/',       b:'{\n  "name": ""\n}',                                                                                                                     rt:'detail'  },

  // BRANCHES
  { g:'BRANCH', m:'GET',   l:'List branches', p:'/v1/i/branches/',                                   rt:'list'   },
  { g:'BRANCH', m:'GET',   l:'Get branch',    p:'/v1/i/branches/{code}/',                             rt:'detail' },
  { g:'BRANCH', m:'PATCH', l:'Update branch', p:'/v1/i/branches/{code}/',    b:'{\n  "name": ""\n}',  rt:'detail' },

  // PACKAGES
  { g:'PKG', m:'GET', l:'Package plans', p:'/v1/i/package-plans/', rt:'list' },
  { g:'PKG', m:'GET', l:'Modules',       p:'/v1/i/modules/',        rt:'list' },

  // RBAC
  { g:'RBAC', m:'GET',  l:'Permissions',       p:'/v1/rbac/permissions/',                                                                                                                                                   rt:'list'    },
  { g:'RBAC', m:'GET',  l:'Role templates',     p:'/v1/rbac/role-templates/',                                                                                                                                                rt:'list'    },
  { g:'RBAC', m:'POST', l:'Create template',    p:'/v1/rbac/role-templates/',   b:'{\n  "name": "",\n  "description": "",\n  "permission_keys": []\n}',                                                                     rt:'created' },
  { g:'RBAC', m:'GET',  l:'School roles',       p:'/v1/rbac/school-roles/',                                                                                                                                                 rt:'list'    },
  { g:'RBAC', m:'GET',  l:'Role assignments',   p:'/v1/rbac/assignments/',                                                                                                                                                   rt:'list'    },
  { g:'RBAC', m:'POST', l:'Assign role',        p:'/v1/rbac/assignments/',      b:'{\n  "user_id": "",\n  "role_id": ""\n}',                                                                                                 rt:'created' },
  { g:'RBAC', m:'GET',  l:'Change requests',    p:'/v1/rbac/change-requests/',                                                                                                                                               rt:'list'    },
  { g:'RBAC', m:'POST', l:'Approve change req', p:'/v1/rbac/change-requests/{request_id}/approve/',                                                                                                                         rt:'action'  },
  { g:'RBAC', m:'POST', l:'Deny change req',    p:'/v1/rbac/change-requests/{request_id}/deny/',    b:'{\n  "reason": ""\n}',                                                                                               rt:'action'  },

  // AUDIT
  { g:'AUDIT', m:'GET', l:'Audit events',     p:'/v1/audit/events/',            rt:'log'  },
  { g:'AUDIT', m:'GET', l:'Compliance rules', p:'/v1/audit/compliance-rules/',   rt:'list' },
  { g:'AUDIT', m:'GET', l:'Exports',          p:'/v1/audit/exports/',            rt:'list' },

  // IMPORTS
  { g:'IMPORT', m:'GET', l:'System templates', p:'/v1/import/system-templates/', rt:'list' },
  { g:'IMPORT', m:'GET', l:'Import batches',   p:'/v1/import/batches/',           rt:'list' },

  // ADMIN
  { g:'ADMIN', m:'GET', l:'Dashboard',      p:'/v1/admin/dashboard/',      rt:'detail' },
  { g:'ADMIN', m:'GET', l:'Impersonations', p:'/v1/admin/impersonations/',  rt:'list'   },
];

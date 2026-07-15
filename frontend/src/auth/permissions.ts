/**
 * Frontend permissions — mirrors backend ROLE_PERMISSIONS in fpulse/auth/models.py.
 *
 * This is UI gating only; the backend middleware is the real enforcement.
 * The goal here is to hide things users can't use so the UI is clean,
 * and to prevent confusing "403 Forbidden" errors after a click.
 *
 * 4-role hierarchy (low → high):
 *   viewer  → developer  → admin  → super_admin
 *
 * Legacy roles "lead" and "member" are mapped at the normRole() level
 * so existing user records keep working.
 */

export type Role =
  | 'viewer'
  | 'developer'
  | 'member'       // legacy alias → developer
  | 'lead'         // legacy alias → admin
  | 'admin'
  | 'super_admin';

export type Environment = 'dev' | 'prod';

export type Action =
  // dev
  | 'create'
  | 'edit'
  | 'delete'
  | 'execute'
  | 'schedule'
  | 'manage_users'
  | 'manage_projects'
  | 'manage_system'
  // prod
  | 'deploy'
  | 'rollback'
  | 'approve'
  | 'view'
  | 'manage_license';

// Valid PROD permissions — per-project, per-user grants
export const PROD_PERMISSIONS = [
  'can_view_prod',
  'can_run_prod',
  'can_deploy_prod',
  'can_manage_prod_connections',
] as const;

export type ProdPermission = typeof PROD_PERMISSIONS[number];

export const PROD_PERMISSION_LABELS: Record<ProdPermission, string> = {
  can_view_prod: 'View PROD',
  can_run_prod: 'Run in PROD',
  can_deploy_prod: 'Deploy to PROD',
  can_manage_prod_connections: 'Manage PROD Connections',
};

const ROLE_PERMISSIONS: Record<Role, Record<Environment, Action[]>> = {
  super_admin: {
    dev: ['create', 'edit', 'delete', 'execute', 'schedule', 'manage_users', 'manage_projects', 'manage_system'],
    prod: ['deploy', 'rollback', 'approve', 'execute', 'view', 'manage_users', 'manage_system', 'manage_license'],
  },
  admin: {
    dev: ['create', 'edit', 'delete', 'execute', 'schedule', 'manage_users', 'manage_projects'],
    prod: ['deploy', 'rollback', 'approve', 'execute', 'view', 'manage_users'],
  },
  // Legacy: lead → admin permissions
  lead: {
    dev: ['create', 'edit', 'delete', 'execute', 'schedule', 'manage_users', 'manage_projects'],
    prod: ['deploy', 'rollback', 'approve', 'execute', 'view', 'manage_users'],
  },
  developer: {
    dev: ['create', 'edit', 'execute'],
    prod: [],  // No default PROD access — must be granted per project
  },
  // Legacy: member → developer permissions
  member: {
    dev: ['create', 'edit', 'execute'],
    prod: [],
  },
  viewer: {
    dev: ['view'],
    prod: [],  // No default PROD access — must be granted per project
  },
};

const ROLE_LEVEL: Record<Role, number> = {
  viewer: 0,
  developer: 1,
  member: 1,   // legacy → developer
  lead: 3,     // legacy → admin
  admin: 3,
  super_admin: 4,
};

export interface UserLike {
  role?: string;
  environments?: string[];
  prod_permissions?: Record<string, string[]>;
}

function normRole(role?: string): Role {
  const r = (role || 'viewer') as Role;
  return (r in ROLE_LEVEL ? r : 'viewer') as Role;
}

/** Canonical role — maps legacy lead→admin, member→developer */
export function canonicalRole(role?: string): 'super_admin' | 'admin' | 'developer' | 'viewer' {
  const r = normRole(role);
  if (r === 'lead') return 'admin';
  if (r === 'member') return 'developer';
  return r as 'super_admin' | 'admin' | 'developer' | 'viewer';
}

/** Check if a user's role has a specific permission in an environment. */
export function hasPermission(user: UserLike | null | undefined, env: Environment, action: Action): boolean {
  if (!user) return true; // free tier / unauthenticated dev mode — backend decides
  const role = normRole(user.role);
  const perms = ROLE_PERMISSIONS[role]?.[env] || [];
  return perms.includes(action);
}

/** Can this user see / open the Admin page? (role-only check) */
export function canAccessAdmin(user: UserLike | null | undefined): boolean {
  if (!user) return true;
  const role = normRole(user.role);
  return role === 'admin' || role === 'super_admin' || role === 'lead';
}

/**
 * Can this user reach Admin *right now*, given the current environment + tier?
 *
 * Product rule: Admin is a production surface — deployments, audit, and
 * license — and is managed from the PROD environment when available.
 * A Plus admin sitting in DEV should NOT see the Admin link; they have to
 * flip to PROD first. On Free/OSS there is no PROD, so Admin stays available
 * in DEV (otherwise the only admin surface in the product would be
 * unreachable).
 *
 * Role gate (admin / super_admin) is always the outer check.
 */
export function canAccessAdminInEnv(
  user: UserLike | null | undefined,
  env: Environment,
  isPlus: boolean,
): boolean {
  if (!canAccessAdmin(user)) return false;
  if (isPlus && env === 'dev') return false;
  return true;
}

/**
 * Can this user create / delete projects?
 *
 * Projects are an admin concern: admins own them, developers are granted
 * access. This is a role-level check independent of `env`, because
 * project governance doesn't change between DEV and PROD — a developer
 * never creates projects, regardless of which environment they're in.
 */
export function canManageProjects(user: UserLike | null | undefined): boolean {
  if (!user) return true;  // unauthenticated / free-tier fallback
  const role = normRole(user.role);
  return role === 'admin' || role === 'super_admin' || role === 'lead';
}

/**
 * Can the user switch to PROD environment at all?
 *
 * Two gates, both must pass:
 *   1. Role check — admin/super_admin have full PROD access by default.
 *      Developer/viewer need explicit per-project PROD grants.
 *   2. The `environments` allow-list on the user record.
 */
export function canAccessProd(user: UserLike | null | undefined): boolean {
  if (!user) return true;
  const role = canonicalRole(user.role);
  // Admin+ always has PROD access (super_admin is covered by this branch too)
  if (role === 'admin' || role === 'super_admin') return true;
  // Developer/viewer: check if they have any PROD grants at all
  const prodPerms = user.prod_permissions || {};
  const hasAnyProdGrant = Object.values(prodPerms).some(perms => perms.length > 0);
  if (!hasAnyProdGrant) return false;
  // Honour the per-user environment allow-list
  const envs = user.environments;
  if (!envs || envs.length === 0) return true; // unset → grants decide
  return envs.includes('prod');
}

/**
 * Check if a user has a specific PROD permission on a project.
 * Admin/Super Admin always have full PROD access.
 */
export function hasProdPermission(
  user: UserLike | null | undefined,
  projectId: string,
  permission: ProdPermission,
): boolean {
  if (!user) return true; // unauthenticated fallback
  const role = canonicalRole(user.role);
  if (role === 'admin' || role === 'super_admin') return true;
  const prodPerms = user.prod_permissions || {};
  const projectPerms = prodPerms[projectId] || [];
  const wildcardPerms = prodPerms['*'] || [];
  return projectPerms.includes(permission) || wildcardPerms.includes(permission);
}

/** Shorthand: is the user read-only in the given environment? */
export function isReadOnly(user: UserLike | null | undefined, env: Environment): boolean {
  if (!user) return false;
  const role = canonicalRole(user.role);
  if (role === 'viewer') return true;
  // Developers are read-only in PROD unless they have explicit grants
  if (env === 'prod' && role === 'developer') return true;
  return false;
}

/** Numeric role level for comparisons. */
export function roleLevel(role?: string): number {
  return ROLE_LEVEL[normRole(role)] ?? 0;
}

export function roleLabel(role?: string): string {
  const map: Record<string, string> = {
    super_admin: 'Super Admin',
    admin: 'Admin',
    lead: 'Admin',       // legacy display
    developer: 'Developer',
    member: 'Developer',  // legacy display
    viewer: 'Viewer',
  };
  return map[normRole(role)] || 'Viewer';
}

/** The 4 canonical roles for dropdown selectors. */
export const CANONICAL_ROLES = ['viewer', 'developer', 'admin', 'super_admin'] as const;

export const CANONICAL_ROLE_LABELS: Record<string, string> = {
  viewer: 'Viewer',
  developer: 'Developer',
  admin: 'Admin',
  super_admin: 'Super Admin',
};

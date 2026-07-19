/**
 * RoleGate — conditionally render children based on RBAC permission.
 *
 * Reads the current user from localStorage and checks the required action
 * against the active environment. Used around buttons that mutate state
 * (delete, deploy, edit, approve, etc.) so users only see actions they
 * can actually take.
 *
 * Examples:
 *   <RoleGate action="edit">
 *     <button onClick={handleEdit}>Edit</button>
 *   </RoleGate>
 *
 *   <RoleGate action="deploy" env="prod" fallback={<DisabledHint />}>
 *     <DeployButton />
 *   </RoleGate>
 */

import { ReactNode } from 'react';
import { hasPermission, type Action, type Environment, type UserLike } from './permissions';

interface RoleGateProps {
  action: Action;
  env?: Environment;
  children: ReactNode;
  fallback?: ReactNode;
}

function getCurrentUser(): UserLike | null {
  try {
    const raw = localStorage.getItem('fpulse_user');
    return raw ? JSON.parse(raw) : null;
  } catch {
    return null;
  }
}

function getCurrentTier(): 'free' | 'plus' {
  return (localStorage.getItem('fpulse_tier') as 'free' | 'plus') || 'free';
}

function getCurrentEnv(): Environment {
  return (localStorage.getItem('fpulse_env') as Environment) || 'dev';
}

export default function RoleGate({ action, env, children, fallback = null }: RoleGateProps) {
  const tier = getCurrentTier();
  const user = getCurrentUser();

  // Free tier has no RBAC — always allow (backend is the source of truth)
  if (tier !== 'plus' || !user) return <>{children}</>;

  const activeEnv = env ?? getCurrentEnv();
  const allowed = hasPermission(user, activeEnv, action);

  return <>{allowed ? children : fallback}</>;
}

/** Hook variant for inline conditionals where you need a boolean. */
export function useCan(action: Action, env?: Environment): boolean {
  const tier = getCurrentTier();
  const user = getCurrentUser();
  if (tier !== 'plus' || !user) return true;
  const activeEnv = env ?? getCurrentEnv();
  return hasPermission(user, activeEnv, action);
}

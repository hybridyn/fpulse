/**
 * ReadOnlyBanner — shown at the top of PROD pages when the current user
 * has view-only access. Makes it obvious why the action buttons are hidden
 * so developers don't think the UI is broken.
 */

import { isReadOnly, roleLabel, type Environment, type UserLike } from './permissions';

interface Props {
  environment: Environment;
}

function getCurrentUser(): UserLike | null {
  try {
    const raw = localStorage.getItem('fpulse_user');
    return raw ? JSON.parse(raw) : null;
  } catch {
    return null;
  }
}

export default function ReadOnlyBanner({ environment }: Props) {
  const tier = (localStorage.getItem('fpulse_tier') as 'free' | 'plus') || 'free';
  const user = getCurrentUser();

  if (tier !== 'plus' || !user) return null;
  if (!isReadOnly(user, environment)) return null;

  return (
    <div className="px-4 py-2 bg-amber-50 border-b border-amber-200 flex items-center gap-2 shrink-0">
      <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" className="text-amber-600">
        <rect x="3" y="11" width="18" height="11" rx="2" />
        <path d="M7 11V7a5 5 0 0 1 10 0v4" />
      </svg>
      <span className="text-xs font-medium text-amber-800">
        Read-only view · Your role ({roleLabel(user.role)}) can see {environment.toUpperCase()} but cannot make changes here.
      </span>
    </div>
  );
}

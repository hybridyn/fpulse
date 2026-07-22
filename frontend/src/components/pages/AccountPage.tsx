import { useEffect, useState } from 'react';
import { navigateToSubRoute } from '../../router';
import { api } from '../../api/client';
import { useDarkMode } from '../../hooks/useDarkMode';
import { usePageContext } from '../../hooks/usePageContext';
import TierChip from '../shared/TierChip';
import PageHeader from '../shared/PageHeader';

/**
 * AccountPage
 * ─────────────────────────────────────────────────────────────────────────────
 * Self-service account management — distinct from /admin which manages
 * *other* people's accounts. Any logged-in user can reach this page.
 *
 * Sections:
 *   1. Profile        — read most fields, edit display name (whitelisted)
 *   2. Password       — change own password (requires current password)
 *   3. Active sessions — list of own sessions, current one highlighted
 *
 * Why this is its own page (not a Settings tab):
 *   Settings is app-level preferences (theme, timezone, autosave). Account is
 *   identity-level. Mixing them led to "where do I change my password?" UX
 *   complaints in the previous review pass.
 */

interface AccountPageProps {
  /** Optional callback so the parent can refresh its `user` state after a
   *  successful profile edit (e.g. update the avatar initial in the header). */
  onProfileUpdated?: (user: any) => void;
  /** Lets the Plan tab's "Manage license" CTA jump straight to AdminPage
   *  for users who can actually edit the license. */
  onNavigate?: (page: string) => void;
  /** Global env toggle state. Reports tab is only exposed in DEV view —
   *  PROD admins access the same feature from the Admin page. */
  environment?: 'dev' | 'prod';
  tier?: string;
}

type Tab = 'profile' | 'password';

export default function AccountPage({ onProfileUpdated, onNavigate, environment = 'dev', tier = 'free' }: AccountPageProps) {
  const dark = useDarkMode();
  // 2026-06-01: read initial tab from URL hash so deep-links work +
  // back-navigation through tabs works correctly. See click handler
  // below for the matching write side.
  const [tab, setTab] = useState<Tab>(() => {
    try {
      const seg = (window.location.hash || '').split('/')[1];
      if (seg === 'profile' || seg === 'password') return seg;
    } catch { /* SSR */ }
    return 'profile';
  });
  const isDev = environment === 'dev';

  // Sync React state when Back/Forward fires.
  useEffect(() => {
    const onHash = () => {
      const seg = (window.location.hash || '').split('/')[1];
      if (seg === 'profile' || seg === 'password') {
        setTab(seg);
      }
    };
    window.addEventListener('hashchange', onHash);
    return () => window.removeEventListener('hashchange', onHash);
  }, []);

  // ── User state — hydrated from localStorage so the page renders instantly,
  // then refreshed from /auth/me so we never show stale role/env data after
  // an admin has updated the user behind their back.
  const [user, setUser] = useState<any>(() => {
    try { return JSON.parse(localStorage.getItem('fpulse_user') || 'null'); }
    catch { return null; }
  });

  // OSS-4 (2026-05-19) — publish context so the Copilot has a handle on
  // which Account sub-surface the user is viewing. No PII published.
  // Declared after `user` state — referencing it earlier crashes the
  // page with a TDZ error ("Cannot access 'user' before initialization").
  usePageContext({
    page: 'account',
    filters: { tab },
    visible_items: [{
      id: 'account',
      kind: 'account',
      meta: { user_id: user?.id || null, role: user?.role || null },
    }],
  });
  const [loading, setLoading] = useState(false);

  useEffect(() => {
    let cancelled = false;
    api.getMe()
      .then((fresh) => {
        if (cancelled) return;
        setUser(fresh);
        // Keep localStorage in sync so the rest of the app sees the same data
        localStorage.setItem('fpulse_user', JSON.stringify(fresh));
      })
      .catch(() => { /* token may be invalid — let App.tsx handle re-login */ });
    return () => { cancelled = true; };
  }, []);

  // ── Profile tab state ──────────────────────────────────────────────────
  const [editingName, setEditingName] = useState(false);
  const [nameDraft, setNameDraft] = useState('');
  const [profileMsg, setProfileMsg] = useState<{ type: 'ok' | 'err'; text: string } | null>(null);

  const startEditName = () => {
    setNameDraft(user?.name || '');
    setEditingName(true);
    setProfileMsg(null);
  };

  const saveName = async () => {
    if (!nameDraft.trim()) {
      setProfileMsg({ type: 'err', text: 'Name cannot be empty.' });
      return;
    }
    setLoading(true);
    try {
      const res = await api.updateMyProfile({ name: nameDraft.trim() });
      if (res.updated && res.user) {
        setUser(res.user);
        localStorage.setItem('fpulse_user', JSON.stringify(res.user));
        onProfileUpdated?.(res.user);
        setEditingName(false);
        setProfileMsg({ type: 'ok', text: 'Profile updated.' });
      }
    } catch (e: any) {
      setProfileMsg({ type: 'err', text: e?.message || 'Update failed.' });
    }
    setLoading(false);
  };

  // ── Password tab state ─────────────────────────────────────────────────
  const [currentPwd, setCurrentPwd] = useState('');
  const [newPwd, setNewPwd] = useState('');
  const [confirmPwd, setConfirmPwd] = useState('');
  const [pwdMsg, setPwdMsg] = useState<{ type: 'ok' | 'err'; text: string } | null>(null);

  // Tiny strength heuristic — not bulletproof but enough to nudge users
  // toward something that isn't "password". Length is the dominant factor.
  const pwdStrength = (() => {
    if (!newPwd) return { score: 0, label: '', color: 'bg-slate-200' };
    let score = 0;
    if (newPwd.length >= 8) score++;
    if (newPwd.length >= 12) score++;
    if (/[A-Z]/.test(newPwd) && /[a-z]/.test(newPwd)) score++;
    if (/\d/.test(newPwd)) score++;
    if (/[^A-Za-z0-9]/.test(newPwd)) score++;
    const labels = ['Too short', 'Weak', 'Fair', 'Good', 'Strong', 'Very strong'];
    const colors = ['bg-red-400', 'bg-red-400', 'bg-amber-400', 'bg-yellow-400', 'bg-emerald-400', 'bg-emerald-500'];
    return { score, label: labels[score], color: colors[score] };
  })();

  const submitPassword = async (e: React.FormEvent) => {
    e.preventDefault();
    setPwdMsg(null);
    if (newPwd.length < 8) {
      setPwdMsg({ type: 'err', text: 'New password must be at least 8 characters.' });
      return;
    }
    if (newPwd !== confirmPwd) {
      setPwdMsg({ type: 'err', text: 'New passwords do not match.' });
      return;
    }
    if (newPwd === currentPwd) {
      setPwdMsg({ type: 'err', text: 'New password must be different from current.' });
      return;
    }
    setLoading(true);
    try {
      // Use the free-tier self-serve endpoint (/api/auth/me/password).
      // The legacy changeMyPassword() hits the Plus-only path
      // /api/plus/users/change-password, which 404s in OSS — so
      // self-serve password changes silently failed for every user.
      await api.changeMyOwnPassword(currentPwd, newPwd);
      setPwdMsg({ type: 'ok', text: 'Password changed successfully.' });
      setCurrentPwd(''); setNewPwd(''); setConfirmPwd('');
    } catch (e: any) {
      setPwdMsg({ type: 'err', text: e?.message || 'Password change failed.' });
    }
    setLoading(false);
  };

  // ── Plan tab state ─────────────────────────────────────────────────────
  // 2026-05-19 (P2 #2 of PAGE_BY_PAGE_AUDIT.md): the Plan + Sessions tab
  // state (license / licenseLoading / licenseErr / loadLicense and the
  // matching sessions counterparts) was carried as dead state — the
  // setters fired but no UI ever read the values. The Plan / Sessions
  // tabs themselves were removed earlier; the only remaining purpose of
  // these blocks was a pair of `void` suppression statements. Whole
  // section deleted. The `PlanPanel` / `PlanField` / `AccessRow` JSX
  // helpers further down the file are still removed as part of the same
  // pass (see comment near their original positions).

  // ── Render helpers ─────────────────────────────────────────────────────
  if (!user) {
    return (
      <div className="flex-1 p-8 text-center text-slate-500">
        Loading account…
      </div>
    );
  }

  const initial = (user.name || user.email || '?')[0].toUpperCase();
  const TABS: { id: Tab; label: string; icon: React.ReactNode }[] = [
    {
      id: 'profile', label: 'Profile',
      icon: <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><path d="M20 21v-2a4 4 0 0 0-4-4H8a4 4 0 0 0-4 4v2" /><circle cx="12" cy="7" r="4" /></svg>,
    },
    {
      id: 'password', label: 'Password',
      icon: <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><rect x="3" y="11" width="18" height="11" rx="2" /><path d="M7 11V7a5 5 0 0 1 10 0v4" /></svg>,
    },
  ];

  // Env-aware header treatment — every page in the app flips to a dark
  // slate-900 banner with a red PROD chip when environment === 'prod'
  // (see feedback_fpulse_page_header_standard.md). Account page was the
  // one exception; this restores cross-app consistency so an admin who
  // switches to PROD sees the same visual signal everywhere.
  const isProd = environment === 'prod';

  return (
    <div className={`flex-1 overflow-auto ${dark ? 'bg-[#0B1220]' : 'bg-canvas-bg'}`}>
      {/* Header — canonical flush-left. Matches DEV/PROD standard. */}
      <PageHeader
        environment={environment}
        icon={(
          <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" className="text-amber-500">
            <path d="M20 21v-2a4 4 0 0 0-4-4H8a4 4 0 0 0-4 4v2" /><circle cx="12" cy="7" r="4" />
          </svg>
        )}
        title="Account"
        titleAccessory={<TierChip tier={tier} environment={environment} />}
        subtitle={isProd
          ? 'Production environment — account settings apply to all environments.'
          : 'Manage your profile, password, and active sessions.'}
      />
      <div className="w-full max-w-[1100px] mx-auto px-6 py-8">

        {/* Identity card — always visible above the tabs */}
        <div className="rounded-lg border border-slate-200 shadow-sm p-5 mb-6 flex items-center gap-4" style={{ background: dark ? '#111827' : 'linear-gradient(135deg, #FFFFFF 0%, #FAFBFF 100%)' }}>
          <div
            className="w-14 h-14 rounded-2xl flex items-center justify-center text-xl font-bold text-white shadow-sm shrink-0"
            style={{ background: 'linear-gradient(135deg, #F5A623, #D4880A)' }}
          >
            {initial}
          </div>
          <div className="min-w-0 flex-1">
            <div className="text-base font-bold text-slate-800 truncate">{user.name || user.email}</div>
            <div className="text-xs text-slate-500 truncate">{user.email}</div>
            <div className="flex items-center gap-1.5 mt-2 flex-wrap">
              <span className="text-xs font-bold uppercase tracking-wide bg-amber-50 text-amber-700 px-2 py-0.5 rounded border border-amber-200">
                {(user.role || 'developer').replace(/_/g, ' ')}
              </span>
              {(user.environments || []).map((e: string) => (
                <span key={e} className={`text-xs font-bold uppercase tracking-wide px-2 py-0.5 rounded border ${
                  e === 'prod'
                    ? 'bg-red-50 text-red-700 border-red-200'
                    : 'bg-emerald-50 text-emerald-700 border-emerald-200'
                }`}>
                  {e}
                </span>
              ))}
            </div>
          </div>
        </div>

        {/* Tab strip */}
        <div className="flex items-center justify-center gap-1 border-b border-slate-200 mb-6">
          {TABS.map((t) => (
            <button
              key={t.id}
              onClick={() => { navigateToSubRoute('account', t.id); setTab(t.id); }}
              className={`flex items-center gap-2 px-4 py-2.5 text-sm font-semibold transition-colors border-b-2 -mb-px ${
                tab === t.id
                  ? 'border-amber-500 text-amber-700'
                  : 'border-transparent text-slate-500 hover:text-slate-700'
              }`}
            >
              {t.icon}
              {t.label}
            </button>
          ))}
        </div>

        {/* ── Profile tab ── */}
        {tab === 'profile' && (
          <div className="rounded-lg border border-slate-200 shadow-sm p-6 space-y-5" style={{ background: dark ? '#111827' : 'linear-gradient(135deg, #FFFFFF 0%, #FAFBFF 100%)' }}>
            {/* Display name — the only editable field */}
            <div>
              <label className="text-xs font-bold text-slate-500 uppercase tracking-wide">Display name</label>
              {editingName ? (
                <div className="flex items-center gap-2 mt-1.5">
                  <input
                    type="text"
                    value={nameDraft}
                    onChange={(e) => setNameDraft(e.target.value)}
                    className="flex-1 px-3 py-2 text-sm border border-slate-200 rounded-lg focus:ring-2 focus:ring-amber-300 focus:border-amber-300 outline-none"
                    autoFocus
                  />
                  <button
                    onClick={saveName}
                    disabled={loading}
                    className="px-3 py-2 text-xs font-bold text-white bg-amber-500 hover:bg-amber-600 rounded-lg disabled:opacity-50"
                  >
                    Save
                  </button>
                  <button
                    onClick={() => { setEditingName(false); setProfileMsg(null); }}
                    className="px-3 py-2 text-xs font-bold text-slate-600 bg-slate-100 hover:bg-slate-200 rounded-lg"
                  >
                    Cancel
                  </button>
                </div>
              ) : (
                <div className="flex items-center justify-between mt-1.5">
                  <span className="text-sm text-slate-800">{user.name || <span className="italic text-slate-400">— not set —</span>}</span>
                  <button
                    onClick={startEditName}
                    className="text-xs font-semibold text-amber-600 hover:text-amber-700"
                  >
                    Edit
                  </button>
                </div>
              )}
            </div>

            {/* Email — read-only. Changing email is an identity change and
                stays admin-only to prevent account hijack via self-edit. */}
            <ReadOnlyField
              label="Email"
              value={user.email}
              // 2026-05-19 (P2 #8 of PAGE_BY_PAGE_AUDIT.md): OSS Free is a
              // single bootstrap user — there is no admin to contact, so
              // the previous copy was misleading. On Plus the admin path
              // is real; on Free the copy now points to the operator
              // setup flow.
              hint={tier === 'plus'
                ? 'Contact your workspace admin to change your email address.'
                : 'Email is set during initial setup. Re-run the setup CLI to change it.'}
            />

            {/* Role — read-only. Self-edit would be a privilege escalation hole. */}
            <ReadOnlyField
              label="Role"
              value={(user.role || 'developer').replace(/_/g, ' ')}
              hint="Your role determines what you can see and do. Only admins can change roles."
            />

            {/* Allowed environments — read-only chip list */}
            <div>
              <label className="text-xs font-bold text-slate-500 uppercase tracking-wide">Allowed environments</label>
              <div className="flex items-center gap-1.5 mt-1.5">
                {/* `|| ['dev']` only catches null/undefined — an empty array is
                    truthy, so a user with environments:[] would render NO chips.
                    Guard on length so the default DEV chip always shows. */}
                {((user.environments && user.environments.length) ? user.environments : ['dev']).map((e: string) => (
                  <span key={e} className={`text-xs font-bold uppercase tracking-wide px-2 py-1 rounded border ${
                    e === 'prod'
                      ? 'bg-red-50 text-red-700 border-red-200'
                      : 'bg-emerald-50 text-emerald-700 border-emerald-200'
                  }`}>
                    {e}
                  </span>
                ))}
              </div>
              <div className="text-xs text-slate-400 mt-1">
                Switch environments from the top bar. PROD access requires F-Pulse+.
              </div>
            </div>

            {profileMsg && (
              <div className={`text-xs px-3 py-2 rounded-lg ${
                profileMsg.type === 'ok'
                  ? 'bg-emerald-50 text-emerald-700 border border-emerald-200'
                  : 'bg-red-50 text-red-700 border border-red-200'
              }`}>
                {profileMsg.text}
              </div>
            )}
          </div>
        )}

        {/* ── Password tab ── */}
        {tab === 'password' && (
          <div className="rounded-lg border border-slate-200 shadow-sm p-6" style={{ background: dark ? '#111827' : 'linear-gradient(135deg, #FFFFFF 0%, #FAFBFF 100%)' }}>
            <form onSubmit={submitPassword} className="space-y-4 max-w-md">
              <div>
                <label className="text-xs font-bold text-slate-500 uppercase tracking-wide">Current password</label>
                <input
                  type="password"
                  value={currentPwd}
                  onChange={(e) => setCurrentPwd(e.target.value)}
                  required
                  autoComplete="current-password"
                  className="w-full mt-1.5 px-3 py-2 text-sm border border-slate-200 rounded-lg focus:ring-2 focus:ring-amber-300 focus:border-amber-300 outline-none"
                />
              </div>
              <div>
                <label className="text-xs font-bold text-slate-500 uppercase tracking-wide">New password</label>
                <input
                  type="password"
                  value={newPwd}
                  onChange={(e) => setNewPwd(e.target.value)}
                  required
                  minLength={8}
                  autoComplete="new-password"
                  className="w-full mt-1.5 px-3 py-2 text-sm border border-slate-200 rounded-lg focus:ring-2 focus:ring-amber-300 focus:border-amber-300 outline-none"
                />
                {newPwd && (
                  <div className="mt-2">
                    <div className="h-1.5 w-full bg-slate-100 rounded-full overflow-hidden">
                      <div
                        className={`h-full transition-all ${pwdStrength.color}`}
                        style={{ width: `${(pwdStrength.score / 5) * 100}%` }}
                      />
                    </div>
                    <div className="text-xs text-slate-500 mt-1">{pwdStrength.label}</div>
                  </div>
                )}
              </div>
              <div>
                <label className="text-xs font-bold text-slate-500 uppercase tracking-wide">Confirm new password</label>
                <input
                  type="password"
                  value={confirmPwd}
                  onChange={(e) => setConfirmPwd(e.target.value)}
                  required
                  autoComplete="new-password"
                  className="w-full mt-1.5 px-3 py-2 text-sm border border-slate-200 rounded-lg focus:ring-2 focus:ring-amber-300 focus:border-amber-300 outline-none"
                />
              </div>

              {pwdMsg && (
                <div className={`text-xs px-3 py-2 rounded-lg ${
                  pwdMsg.type === 'ok'
                    ? 'bg-emerald-50 text-emerald-700 border border-emerald-200'
                    : 'bg-red-50 text-red-700 border border-red-200'
                }`}>
                  {pwdMsg.text}
                </div>
              )}

              <button
                type="submit"
                disabled={loading}
                className="px-4 py-2 text-sm font-bold text-white bg-amber-500 hover:bg-amber-600 rounded-lg shadow-sm disabled:opacity-50"
              >
                {loading ? 'Updating…' : 'Change password'}
              </button>
            </form>

            <div className="mt-6 pt-4 border-t border-slate-100 text-xs text-slate-400 leading-relaxed max-w-md">
              <strong className="text-slate-500">Tip:</strong> Use a unique passphrase you don't reuse anywhere else.
              Minimum 8 characters; 12 or more is recommended.
            </div>
          </div>
        )}

      </div>
    </div>
  );
}

// Small read-only field helper — used twice on the Profile tab
function ReadOnlyField({ label, value, hint }: { label: string; value: string; hint?: string }) {
  return (
    <div>
      <label className="text-xs font-bold text-slate-500 uppercase tracking-wide">{label}</label>
      <div className="mt-1.5 px-3 py-2 text-sm text-slate-700 bg-slate-50 border border-slate-200 rounded-lg">
        {value}
      </div>
      {hint && <div className="text-xs text-slate-400 mt-1">{hint}</div>}
    </div>
  );
}

// 2026-05-19 (P2 #2 of PAGE_BY_PAGE_AUDIT.md): `PlanPanel`, `PlanField`,
// and `AccessRow` (formerly ~210 lines below this comment) were the
// rendering helpers for the Plan tab. The Plan tab itself was removed
// when the license surface moved to /api/plus/license + Settings; the
// helpers were left orphaned. No caller imported them. Removed.

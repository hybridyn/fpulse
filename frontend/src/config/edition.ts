/**
 * Edition-level feature flags for F-Pulse OSS.
 *
 * F-Pulse OSS is a single-operator product — the sign-in screen says as much
 * ("F-Pulse OSS runs as a single operator"). Multi-workspace selection
 * (switching between tenants, Personal vs. shared scopes, workspace
 * membership) is a Plus capability, so OSS never surfaces a workspace picker
 * and never lands the operator anywhere but the one shared `default`
 * workspace that the schema-v2 migration back-fills onto every install.
 *
 * This is a UI/UX gate only. The backend `workspace_id` spine is untouched:
 * every scoped request still carries `X-Workspace-Id: default`, so the change
 * is fully forward-compatible with Plus. Flip WORKSPACES_ENABLED to `true`
 * (in a Plus build) to restore the switcher and per-user landing.
 *
 * Why it matters: when the switcher was exposed, login preferred the user's
 * (empty) Personal workspace while their pipelines lived in `default`, so the
 * pipeline list looked empty — pipelines appeared to "vanish". Pinning to
 * `default` removes that failure mode entirely.
 */
export const WORKSPACES_ENABLED = false;

/** The single workspace OSS operates in (the v2 back-fill workspace). */
export const DEFAULT_WORKSPACE_ID = 'default';

/**
 * The workspace id to attach to scoped API calls. In OSS this is always
 * `default`, regardless of any stale value a previous build may have written
 * to localStorage — so the operator's pipelines can never be filtered out by
 * a lingering Personal-workspace selection.
 */
export function currentWorkspaceId(): string {
  if (!WORKSPACES_ENABLED) return DEFAULT_WORKSPACE_ID;
  try {
    return localStorage.getItem('fpulse_workspace_id') || DEFAULT_WORKSPACE_ID;
  } catch {
    return DEFAULT_WORKSPACE_ID;
  }
}

import { currentWorkspaceId } from '../config/edition';

const BASE = '/api';

// 2026-05-19 (P1 #14 of PAGE_BY_PAGE_AUDIT.md): centralised backend-reach
// signal. Every successful call clears the offline flag; every network
// (TypeError) failure sets it. App.tsx listens for the dispatched events
// and renders a global "backend unreachable" banner so users get one
// consistent signal instead of a kaleidoscope of per-page silent empties
// and mystery toasts.
let _backendReachable = true;
function emitBackendReachable(reachable: boolean, reason?: string) {
  if (_backendReachable === reachable) return;
  _backendReachable = reachable;
  try {
    window.dispatchEvent(
      new CustomEvent('fpulse:backend-reachable', { detail: { reachable, reason } }),
    );
  } catch {
    /* CustomEvent unsupported — fall through */
  }
}

/**
 * Auth + workspace headers for RAW `fetch` calls that bypass `request()`
 * (streaming/preview endpoints). Mirrors what `request()` attaches — Bearer
 * token + X-Workspace-Id — so those calls don't 401 "Authentication required".
 * Always prefer the `api` client; use this only when a raw fetch is required.
 */
export function authHeaders(extra?: Record<string, string>): Record<string, string> {
  const token = localStorage.getItem('fpulse_token');
  const workspaceId = currentWorkspaceId();
  const csrf = getCsrfCookie();
  return {
    'Content-Type': 'application/json',
    'X-Workspace-Id': workspaceId,
    ...(token ? { Authorization: `Bearer ${token}` } : {}),
    ...(csrf ? { 'X-CSRF-Token': csrf } : {}),
    ...(extra || {}),
  };
}

/**
 * Read the JS-readable CSRF cookie set at login (BFF double-submit). Used to
 * echo `X-CSRF-Token` on state-changing calls so cookie-authenticated
 * requests pass the server's CSRF guard. Empty string if not present.
 */
function getCsrfCookie(): string {
  const m = document.cookie.match(/(?:^|;\s*)fpulse_csrf=([^;]+)/);
  return m ? decodeURIComponent(m[1]) : '';
}

async function request<T>(path: string, options?: RequestInit): Promise<T> {
  const token = localStorage.getItem('fpulse_token');
  const headers: Record<string, string> = {
    'Content-Type': 'application/json',
    ...(options?.headers as Record<string, string> || {}),
  };
  // Attach auth token for RBAC. Dual-auth transition: the bearer still works,
  // and the HttpOnly session cookie is sent automatically (same-origin), so
  // this keeps working as the browser moves onto cookie auth.
  if (token) {
    headers['Authorization'] = `Bearer ${token}`;
  }
  // BFF CSRF: echo the readable CSRF cookie on state-changing requests so a
  // cookie-authenticated call passes the server's double-submit check. Inert
  // for bearer-authed calls (the server exempts them) and when no cookie set.
  const _method = (options?.method || 'GET').toUpperCase();
  if (_method !== 'GET' && _method !== 'HEAD' && _method !== 'OPTIONS') {
    const csrf = getCsrfCookie();
    if (csrf) headers['X-CSRF-Token'] = csrf;
  }
  // Workspace context (schema v2). Every scoped API call carries the
  // current workspace id so the backend can filter projects/pipelines/etc
  // to one tenant. The value is set by the workspace switcher (when one
  // exists) and persists across reloads in localStorage. Until a switcher
  // ships in Stage 2, the default is `default` — the back-fill workspace
  // every legacy install already has from the v2 migration. Routers that
  // don't care about workspaces simply ignore the header.
  const workspaceId = currentWorkspaceId();
  headers['X-Workspace-Id'] = workspaceId;

  let res: Response;
  try {
    res = await fetch(`${BASE}${path}`, {
      ...options,
      headers,
    });
  } catch (networkErr: any) {
    // True TypeError = the browser couldn't reach the server at all
    // (DNS fail / TCP RST / dev backend not running). Distinguish from
    // any error returned in the response body so the global banner only
    // flips on the genuine offline signal.
    emitBackendReachable(false, networkErr?.message || 'Network error');
    throw networkErr;
  }
  emitBackendReachable(true);
  if (!res.ok) {
    const err = await res.json().catch(() => ({ detail: res.statusText }));
    // ── Global 401 handler: stale/expired session → re-login ──
    // 2026-05-19 (P0 #8 of PAGE_BY_PAGE_AUDIT.md): we previously cleared
    // the token and force-reloaded immediately, which silently discarded
    // unsaved canvas state, ConfigPanel drafts, and the Copilot chat. The
    // token is still cleared here (so subsequent calls fail fast), but the
    // actual reload is delegated to a `fpulse:session-expired` window
    // event. App.tsx listens for it, checks the workflow store dirty flag,
    // and either auto-saves before reloading or asks the user. The
    // fallback timeout below guarantees we still reload even when the
    // listener isn't mounted (e.g. catastrophic render crash).
    if (res.status === 401 && token) {
      localStorage.removeItem('fpulse_token');
      localStorage.removeItem('fpulse_user');
      try {
        window.dispatchEvent(new CustomEvent('fpulse:session-expired'));
      } catch {
        /* CustomEvent unavailable — fall through to reload fallback */
      }
      // Fallback: if no listener handled the event within 5s, hard-reload
      // anyway so a broken App.tsx can't strand the user on a bad session.
      window.setTimeout(() => {
        if (localStorage.getItem('fpulse_token')) return; // someone logged back in
        window.location.hash = '#dashboard';
        window.location.reload();
      }, 5000);
      throw new Error('Session expired. Reloading to login...');
    }
    throw new Error(_humanizeApiError(err.detail) || res.statusText);
  }
  return res.json();
}

/**
 * Convert a FastAPI error `detail` field into a readable string.
 *
 * FastAPI lets handlers raise `HTTPException(status, body)` where body
 * can be either a string OR an arbitrary dict. The dict path is
 * idiomatic for structured errors with codes ({"code": "...", "message":
 * "...", "field": "..."}) — but plain `String(detail)` turns the dict
 * into "[object Object]" which is what every error toast in the app
 * was showing until this helper was added (2026-05-28).
 *
 * Heuristics, applied in order:
 *   1. null / undefined / empty → "" (caller falls back to statusText).
 *   2. string → returned as-is.
 *   3. object with `message` → that field (most common: our backfill /
 *      schedule guardrails return `{code, message, ...extras}`).
 *   4. object with `detail` (Pydantic validation responses nest one
 *      level) → recurse on the inner.
 *   5. array of validation items (FastAPI's default `RequestValidationError`
 *      shape) → join the first few `msg` fields with semicolons.
 *   6. last-resort fallback → JSON-stringify so the user at least sees
 *      *something* identifiable, not "[object Object]".
 */
function _humanizeApiError(detail: unknown): string {
  if (detail == null || detail === '') return '';
  if (typeof detail === 'string') return detail;
  if (Array.isArray(detail)) {
    // Pydantic field-level validation errors: take up to 3 msgs.
    const msgs = detail
      .slice(0, 3)
      .map((d: any) =>
        typeof d === 'string'
          ? d
          : (d && typeof d === 'object' && (d.msg || d.message)) || JSON.stringify(d),
      )
      .filter(Boolean);
    return msgs.length > 0 ? msgs.join('; ') : JSON.stringify(detail);
  }
  if (typeof detail === 'object') {
    const d = detail as Record<string, unknown>;
    if (typeof d.message === 'string') return d.message;
    if (d.detail) return _humanizeApiError(d.detail);
    if (typeof d.error === 'string') return d.error as string;
    // Last-resort: JSON. Better than "[object Object]" and the support
    // person can at least copy-paste it into an issue.
    try {
      return JSON.stringify(detail);
    } catch {
      return String(detail);
    }
  }
  return String(detail);
}

// ── Shared public API surface (V8/V9 — 2026-05-26) ─────────────────────
// Until now, `request<T>` was module-internal. Sibling clients
// (`api/agent.ts`, `api/ollama.ts`) re-implement the same fetch + auth
// + workspace-header logic, which means the global 401 handler and the
// backend-reachable signal don't fire on calls routed through them.
//
// Re-exporting `request` as `apiRequest` lets those siblings migrate
// to the central implementation in a follow-up commit (no behavior
// change here — purely additive). The internal name stays `request`
// so existing call sites in this file are untouched.
//
// `ApiError` mirrors the standardized error response shape introduced
// in `backend/fpulse/api/errors.py`. Callers narrowing a `catch` block
// can cast to `ApiError` after JSON-parsing `Error.message` (only the
// `detail` field is guaranteed today; `code` / `field` / `trace_id`
// are best-effort while endpoints migrate to the helper).
export { request as apiRequest };

/**
 * Standardized error response shape returned by endpoints that have
 * migrated to `api_error()` in the backend. `detail` is always present;
 * the rest are progressively populated as endpoints adopt the helper.
 */
export interface ApiError {
  /** Human-readable message safe to surface to the user. */
  detail: string;
  /** Stable machine-readable code from `backend/fpulse/api/errors.py:ErrorCode`. */
  code?: string;
  /** For validation errors — the specific field that failed. */
  field?: string;
  /** Support reference UUID; lets the user quote a short ID for backend log lookup. */
  trace_id?: string;
}

// ── License cache + dedup (module-level) ───────────────────────────────
// Single source of truth for /api/plus/license traffic across the whole
// app. Without this every component that renders a tier-aware chip
// (Dashboard, Admin, Account, Sidebar, App.tsx) would fire its own GET
// on every mount — backend logs showed dozens of /plus/license calls
// per minute on a normal click-around session.

const LICENSE_TTL_MS = 5 * 60 * 1000;

// ─────────────────────────────────────────────────────────────────────────
// Schema Propagation Loop (PR 1) — wire type for the per-step schema
// lookup. `is_source` flips the consumer's behaviour: sources expose
// `self_schema`; non-sources expose `inputs[].schema`.
// ─────────────────────────────────────────────────────────────────────────
export interface SchemaColumn {
  name: string;
  type: string;
  /** Optional — set by the canonical schema layer + the upstream-trivial-
   *  transform synthesizer (e.g. derived_column adds nullable: true).
   *  Older callers can omit. */
  nullable?: boolean;
}
export interface StepSchema {
  columns: SchemaColumn[];
}
export interface UpstreamInputSchema {
  upstream_step_id: string;
  upstream_label: string;
  schema: StepSchema;
}
export interface StepSchemaResponse {
  step_id: string;
  is_source: boolean;
  inputs: UpstreamInputSchema[];
  self_schema?: StepSchema;
  error?: string;
}

interface LicenseCacheEntry {
  data: any;
  ts: number;
}

let _licenseCache: LicenseCacheEntry | null = null;
let _licenseInflight: Promise<any> | null = null;
// Diagnostic — counts every call to _getLicenseCached, and how many
// were served from cache vs hit the network. Inspect via
// `window.__fpulse_lic_diag` in DevTools to prove the cache is live.
const _licDiag = { calls: 0, hits: 0, misses: 0, inflightShared: 0 };

// Negative-cache payload returned when /api/plus/license 404s. The
// endpoint only exists on Plus installs; a 404 is the canonical signal
// that this install is OSS Free, so we synthesize the "no license" shape
// and cache it like a real response. Without this the front-end hits
// the wire on every component mount because the previous `.catch`
// re-threw the error and left `_licenseCache` empty (verified May 11
// 2026: 201 calls / 0 hits / 200 misses on a normal session).
const _NEGATIVE_LICENSE_PAYLOAD: any = {
  tier: 'free',
  active: false,
  source: 'oss-no-license-endpoint',
};

async function _getLicenseCached(): Promise<any> {
  _licDiag.calls += 1;
  // Serve fresh cache without touching the network.
  if (_licenseCache && Date.now() - _licenseCache.ts < LICENSE_TTL_MS) {
    _licDiag.hits += 1;
    return _licenseCache.data;
  }
  // Collapse parallel callers onto a single in-flight promise.
  if (_licenseInflight) {
    _licDiag.inflightShared += 1;
    return _licenseInflight;
  }
  _licDiag.misses += 1;
  _licenseInflight = request<any>('/plus/license')
    .then((data) => {
      _licenseCache = { data, ts: Date.now() };
      return data;
    })
    .catch((err) => {
      // 404 (or any failure) → this install has no Plus license endpoint.
      // Cache the negative result so we don't hammer the backend with
      // a request that will never succeed. The TTL is still honored so
      // a user activating Plus mid-session gets picked up on the next
      // cache window (or immediately via the `fpulse:license-changed`
      // event listener below, which clears the cache).
      const msg = String((err && (err.message || err)) || '');
      const isNotFound = /not\s*found|404/i.test(msg);
      if (isNotFound || !_licenseCache) {
        _licenseCache = { data: _NEGATIVE_LICENSE_PAYLOAD, ts: Date.now() };
        return _NEGATIVE_LICENSE_PAYLOAD;
      }
      // Transient error (network blip, 5xx) — keep returning the
      // last-known good value rather than flipping to OSS-Free.
      return _licenseCache.data;
    })
    .finally(() => {
      _licenseInflight = null;
    });
  return _licenseInflight;
}

// Expose the diagnostic on window for browser console inspection.
// `window.__fpulse_lic_diag` shows: calls (total), hits (cache served),
// misses (network), inflightShared (deduped). If misses == calls,
// the cache isn't loading — likely an old bundle in the browser.
if (typeof window !== 'undefined') {
  (window as any).__fpulse_lic_diag = _licDiag;
}

/** Bust the in-memory license cache. Call after activate/deactivate so
 *  the next getLicenseStatus() goes to the network. */
export function clearLicenseCache(): void {
  _licenseCache = null;
}

// Listen for the cross-component invalidation event dispatched by
// AdminPage activate/deactivate handlers (see App.tsx for the receiver
// that also handles the localStorage-tier cache).
if (typeof window !== 'undefined') {
  window.addEventListener('fpulse:license-changed', () => {
    _licenseCache = null;
  });
}

// Generic helpers (used by new pages that talk to dynamic endpoints)
export const api = {
  get: <T = any>(path: string) => request<T>(path.replace(/^\/api/, '')),
  post: <T = any>(path: string, body?: any) => request<T>(path.replace(/^\/api/, ''), { method: 'POST', body: body ? JSON.stringify(body) : undefined }),
  put: <T = any>(path: string, body?: any) => request<T>(path.replace(/^\/api/, ''), { method: 'PUT', body: body ? JSON.stringify(body) : undefined }),
  // Z22 (2026-05-23) — partial update verb. Used by Storage → Edit
  // managed-table metadata (description + tags). Same body-serialisation
  // contract as post / put.
  patch: <T = any>(path: string, body?: any) => request<T>(path.replace(/^\/api/, ''), { method: 'PATCH', body: body ? JSON.stringify(body) : undefined }),
  delete: <T = any>(path: string) => request<T>(path.replace(/^\/api/, ''), { method: 'DELETE' }),
  /**
   * POST with a non-JSON body (FormData, Blob, URLSearchParams, ...).
   *
   * 2026-05-23 (Y4): added for the Storage page's /api/storage/upload
   * call. The generic `post` helper stringifies the body as JSON, which
   * doesn't work for multipart uploads. This helper bypasses the JSON
   * step while still attaching auth + workspace headers consistently.
   */
  postRaw: async <T = any>(path: string, body: BodyInit) => {
    const token = localStorage.getItem('fpulse_token') || '';
    const workspaceId = currentWorkspaceId();
    const cleanPath = path.startsWith('/api') ? path : `/api${path.startsWith('/') ? '' : '/'}${path}`;
    const res = await fetch(cleanPath, {
      method: 'POST',
      body,
      headers: {
        ...(token ? { Authorization: `Bearer ${token}` } : {}),
        'X-Workspace-Id': workspaceId,
      },
    });
    if (!res.ok) {
      const err = await res.json().catch(() => ({ detail: res.statusText }));
      throw new Error(_humanizeApiError(err.detail) || res.statusText);
    }
    return res.json() as Promise<T>;
  },

  // Workflows
  listWorkflows: (params?: { project_id?: string }) => {
    const qs = new URLSearchParams();
    if (params?.project_id) qs.set('project_id', params.project_id);
    const q = qs.toString();
    return request<any[]>(`/workflows/${q ? `?${q}` : ''}`);
  },
  // Per-workflow recent execution statuses — drives the row-level
  // RunStatusSparkline on the Pipelines page (N4 round 1).
  getRecentStatuses: (workflowIds: string[], limitPerWorkflow: number = 14) => {
    if (workflowIds.length === 0) {
      return Promise.resolve({ version: 1, by_workflow: {}, limit_per_workflow: limitPerWorkflow });
    }
    const qs = new URLSearchParams({
      workflow_ids: workflowIds.join(','),
      limit_per_workflow: String(limitPerWorkflow),
    });
    return request<{
      version: number;
      by_workflow: Record<string, string[]>;
      limit_per_workflow: number;
    }>(`/monitor/recent-statuses?${qs.toString()}`);
  },
  createWorkflow: (name: string, projectId?: string) =>
    request<any>('/workflows/', { method: 'POST', body: JSON.stringify({ name, project_id: projectId }) }),
  getWorkflow: (id: string) => request<any>(`/workflows/${id}`),
  updateWorkflow: (id: string, workflow: any, changeSummary = '') =>
    request<any>(`/workflows/${id}`, {
      method: 'PUT',
      body: JSON.stringify({ workflow, change_summary: changeSummary }),
    }),
  deleteWorkflow: (id: string) =>
    request<any>(`/workflows/${id}`, { method: 'DELETE' }),
  // Schema Propagation Loop (PR 1) — return the column schema flowing
  // INTO `stepId`. ConfigPanel + Data Wrangler use this so column-name
  // dropdowns always show the live, post-transformation column list.
  // POST form lets the editor evaluate unsaved canvas edits.
  getStepSchema: (workflowId: string, stepId: string, unsavedWorkflow?: any) =>
    unsavedWorkflow
      ? request<StepSchemaResponse>(`/workflows/${workflowId}/step/${stepId}/schema`, {
          method: 'POST',
          body: JSON.stringify({ workflow: unsavedWorkflow }),
        })
      : request<StepSchemaResponse>(`/workflows/${workflowId}/step/${stepId}/schema`),
  getWorkflowVersions: (id: string) => request<any[]>(`/workflows/${id}/versions`),
  getWorkflowVersion: (id: string, version: number) => request<any>(`/workflows/${id}?version=${version}`),
  diffWorkflowVersions: (id: string, v1: number, v2: number) =>
    request<any>(`/workflows/${id}/diff?v1=${v1}&v2=${v2}`),
  // Plan stage — preview the diff (steps + edges + connection refs +
  // validator + execution baseline) before saving or submitting for
  // review. `against=deployed` for Submit/Deploy, `latest` for Save.
  planWorkflow: (id: string, workflow: any, against: 'latest' | 'deployed' = 'latest') =>
    request<any>(`/workflows/${id}/plan?against=${against}`, {
      method: 'POST',
      body: JSON.stringify({ workflow }),
    }),
  // Drift detection (admin only)
  driftSummary: () => request<{ info: number; warning: number; critical: number; total: number }>('/admin/drift/summary'),
  driftEvents: (opts?: { itemType?: string; includeResolved?: boolean; limit?: number }) => {
    const qs = new URLSearchParams();
    if (opts?.itemType) qs.set('item_type', opts.itemType);
    if (opts?.includeResolved) qs.set('include_resolved', 'true');
    if (opts?.limit) qs.set('limit', String(opts.limit));
    const q = qs.toString();
    return request<any[]>(`/admin/drift/events${q ? `?${q}` : ''}`);
  },
  driftScan: (maxItems = 5000) =>
    request<{ workspace_id: string; events_recorded: number; by_kind: Record<string, number>; by_severity: Record<string, number> }>(`/admin/drift/scan?max_items=${maxItems}`, { method: 'POST' }),
  driftResolve: (eventId: string, resolution: 'fixed' | 'accepted' | 'dismissed' = 'fixed') =>
    request<{ resolved: boolean }>(`/admin/drift/events/${eventId}/resolve`, {
      method: 'POST',
      body: JSON.stringify({ resolution }),
    }),
  // Deployment & Approval Workflow
  submitForReview: (id: string, snapshotHash?: string) => {
    const qs = snapshotHash ? `?snapshot_hash=${encodeURIComponent(snapshotHash)}` : '';
    return request<any>(`/workflows/${id}/submit-for-review${qs}`, { method: 'POST' });
  },
  approvePipeline: (id: string, notes?: string) =>
    request<any>(`/workflows/${id}/approve${notes ? `?notes=${encodeURIComponent(notes)}` : ''}`, { method: 'POST' }),
  rejectPipeline: (id: string, notes: string) =>
    request<any>(`/workflows/${id}/reject?notes=${encodeURIComponent(notes)}`, { method: 'POST' }),
  deployWorkflow: (id: string, version?: number) =>
    request<any>(`/workflows/${id}/deploy${version ? `?version=${version}` : ''}`, { method: 'POST' }),
  rollbackWorkflow: (id: string, toVersion: number) =>
    request<any>(`/workflows/${id}/rollback?to_version=${toVersion}`, { method: 'POST' }),
  getUpdateReadiness: () => request<any>('/system/update-readiness'),

  // Pipeline Export / Import / Clone
  exportPipeline: (id: string) =>
    request<any>(`/workflows/${id}/export`),
  // Project Export — GET /api/projects/{id}/export returns a JSON project
  // bundle. include_schedules / include_alerts are optional (default on the
  // server). Mirrors exportPipeline.
  exportProject: (
    projectId: string,
    opts?: { includeSchedules?: boolean; includeAlerts?: boolean },
  ) => {
    const params = new URLSearchParams();
    if (opts?.includeSchedules !== undefined) params.set('include_schedules', String(opts.includeSchedules));
    if (opts?.includeAlerts !== undefined) params.set('include_alerts', String(opts.includeAlerts));
    const qs = params.toString();
    return request<any>(`/projects/${projectId}/export${qs ? `?${qs}` : ''}`);
  },
  importPipeline: (pipeline: any, projectId?: string, rename?: string, connectionMap?: Record<string, string>) =>
    request<any>('/workflows/import', {
      method: 'POST',
      body: JSON.stringify({
        pipeline,
        project_id: projectId || 'default',
        rename: rename || '',
        connection_map: connectionMap || {},
      }),
    }),
  // Import a compiled dbt manifest.json (models → SQL Transform nodes,
  // ref()/source() → the pipeline DAG). Returns { id, name, steps_imported,
  // connections_imported, report:{ models, sources, incremental_models, warnings } }.
  importDbt: (manifest: any, projectId?: string, name?: string) =>
    request<any>('/workflows/import-dbt', {
      method: 'POST',
      body: JSON.stringify({
        manifest,
        project_id: projectId || 'default',
        name: name || null,
      }),
    }),
  clonePipeline: (id: string, name?: string) =>
    request<any>(
      `/workflows/${id}/clone${name ? `?name=${encodeURIComponent(name)}` : ''}`,
      { method: 'POST' },
    ),
  preDeployCheck: (id: string) =>
    request<any>(`/workflows/${id}/pre-deploy-check`),

  // Workspaces
  listWorkspaces: () => request<any[]>('/workspaces/'),
  getWorkspace: (id: string) => request<any>(`/workspaces/${id}`),
  createWorkspace: (data: { name: string; slug?: string; domain_allowlist?: string[] }) =>
    request<any>('/workspaces/', { method: 'POST', body: JSON.stringify(data) }),
  updateWorkspace: (id: string, data: any) =>
    request<any>(`/workspaces/${id}`, { method: 'PUT', body: JSON.stringify(data) }),
  deleteWorkspace: (id: string) =>
    request<any>(`/workspaces/${id}`, { method: 'DELETE' }),
  listWorkspaceMembers: (id: string) =>
    request<any[]>(`/workspaces/${id}/members`),
  inviteWorkspaceMember: (wsId: string, data: { user_id?: string; email?: string; role?: string }) =>
    request<any>(`/workspaces/${wsId}/members`, { method: 'POST', body: JSON.stringify(data) }),
  updateWorkspaceMemberRole: (wsId: string, userId: string, role: string) =>
    request<any>(`/workspaces/${wsId}/members/${userId}`, { method: 'PUT', body: JSON.stringify({ role }) }),
  removeWorkspaceMember: (wsId: string, userId: string) =>
    request<any>(`/workspaces/${wsId}/members/${userId}`, { method: 'DELETE' }),

  // Execution
  runWorkflow: (
    id: string,
    fullRun = false,
    environment = 'dev',
    safetyMode: 'live' | 'sample' | 'dry_run' | 'validate_only' = 'live',
    parameterValues?: Record<string, unknown>,
  ) => {
    const qs = `?full_run=${fullRun}&environment=${environment}&safety_mode=${safetyMode}`;
    const body = parameterValues && Object.keys(parameterValues).length > 0
      ? { parameter_values: parameterValues }
      : undefined;
    return request<any>(`/execute/workflow/${id}${qs}`, {
      method: 'POST',
      ...(body ? { body: JSON.stringify(body) } : {}),
    });
  },

  /**
   * Run an unsaved workflow IR directly — no persistence required.
   *
   * Used by the canvas Run/Sample buttons before the user clicks Save,
   * and when the canvas has uncommitted changes vs. the stored workflow.
   * The backend validates the IR, resolves connections by ID against the
   * caller's workspace, then executes. Honours the no-silent-create rule
   * (2026-05-09) — running never adds a row to the Pipelines list.
   */
  runWorkflowEphemeral: (
    workflow: any,
    fullRun = false,
    environment = 'dev',
    safetyMode: 'live' | 'sample' | 'dry_run' | 'validate_only' = 'live',
    parameterValues?: Record<string, unknown>,
  ) => {
    return request<any>('/execute/workflow/ephemeral', {
      method: 'POST',
      body: JSON.stringify({
        workflow,
        full_run: fullRun,
        environment,
        safety_mode: safetyMode,
        parameter_values: parameterValues || {},
      }),
    });
  },
  getSourceInfo: (filePath: string) =>
    request<any>('/workflows/source-info', { method: 'POST', body: JSON.stringify({ file_path: filePath }) }),
  runStep: (workflowId: string, stepId: string) =>
    request<any>(`/execute/workflow/${workflowId}/step/${stepId}`, { method: 'POST' }),
  // Z10 (2026-05-23) — Test Node against an unsaved pipeline: no
  // persistence, no name-prompt friction. The inline workflow IR runs
  // through the same executor as the persisted path; the response
  // shape matches `runStep` so call sites don't branch on the result.
  runStepEphemeral: (workflow: any, stepId: string, previewLimit = 50) =>
    request<any>(`/execute/workflow/ephemeral/step/${stepId}`, {
      method: 'POST',
      body: JSON.stringify({ workflow, preview_limit: previewLimit }),
    }),
  resumeFromStep: (workflowId: string, stepId: string) =>
    request<any>(`/execute/workflow/${workflowId}/step/${stepId}/resume`, { method: 'POST' }),
  resumeWorkflow: (workflowId: string, runId: string) =>
    request<any>(`/execute/workflow/${workflowId}/resume?run_id=${encodeURIComponent(runId)}`, { method: 'POST' }),
  // Move helpers — reassign a resource to a different project.
  // For connections/credentials, pass empty string to make the resource global.
  moveWorkflow: (workflowId: string, targetProjectId: string) =>
    request<{ moved: boolean; project_id: string; version: number }>(
      `/workflows/${workflowId}/move?target_project_id=${encodeURIComponent(targetProjectId)}`,
      { method: 'POST' },
    ),
  moveConnection: (connectionId: string, targetProjectId: string) =>
    request<{ moved: boolean; project_id: string | null }>(
      `/connections/${connectionId}/move?target_project_id=${encodeURIComponent(targetProjectId)}`,
      { method: 'POST' },
    ),
  moveCredential: (credentialId: string, targetProjectId: string) =>
    request<{ moved: boolean; project_id: string | null }>(
      `/credentials/${credentialId}/move?target_project_id=${encodeURIComponent(targetProjectId)}`,
      { method: 'POST' },
    ),
  getCacheSummary: (workflowId: string) =>
    request<any>(`/execute/workflow/${workflowId}/cache`),
  clearCache: (workflowId: string, stepId?: string) =>
    request<any>(`/execute/workflow/${workflowId}/cache${stepId ? `?step_id=${encodeURIComponent(stepId)}` : ''}`, { method: 'DELETE' }),

  // Planner
  generatePlan: (intent: string) =>
    request<any>('/planner/generate', { method: 'POST', body: JSON.stringify({ intent }) }),
  getTemplates: () => request<any>('/planner/templates'),
  useTemplate: (key: string) =>
    request<any>(`/planner/templates/${key}`, { method: 'POST' }),

  // User templates — workspace-scoped library backed by user_templates table.
  // The TemplatesPage merges these with the static built-in catalog so users
  // see "Built-in" + "Yours" in the same gallery. Paths exclude the /api
  // prefix because the request() helper above prepends BASE = '/api'.
  listUserTemplates: () =>
    request<{ templates: any[] }>('/templates/user'),
  createUserTemplate: (body: {
    name: string;
    tagline?: string;
    description?: string;
    category?: string;
    steps: any[];
    connections: any[];
  }) =>
    request<any>('/templates/user', { method: 'POST', body: JSON.stringify(body) }),
  deleteUserTemplate: (id: string) =>
    request<any>(`/templates/user/${id}`, { method: 'DELETE' }),
  chat: (messages: Array<{ role: string; content: string }>) =>
    request<any>('/planner/chat', { method: 'POST', body: JSON.stringify(messages) }),
  /**
   * Natural-language chat anchored to the editor canvas. The LLM gets
   * the canvas snapshot as part of its system prompt and replies in
   * plain prose. When no AI provider is configured the response has
   * `ai_available: false` and the caller falls back to client-side
   * pattern-matched handlers.
   */
  canvasChat: (
    messages: Array<{ role: string; content: string }>,
    canvas: {
      workflow_id?: string | null;
      workflow_name?: string;
      status?: string;
      version?: number;
      nodes: Array<{ id: string; type: string; label?: string; params?: Record<string, any> }>;
      edges: Array<{ source: string; target: string; condition?: string | null }>;
      parameters?: Array<{ name: string; type?: string; default?: any; required?: boolean }>;
      issues?: Array<{ level: string; step_id?: string | null; message: string }>;
    },
  ) =>
    request<{ reply: string; ai_powered: boolean; ai_available: boolean; error?: string }>(
      '/planner/canvas-chat',
      { method: 'POST', body: JSON.stringify({ messages, canvas }) },
    ),
  /**
   * Tool-using AI agent. Same endpoint the FloatingAgentWidget uses —
   * LLM gets a registry of read tools (summarize_pipeline,
   * validate_pipeline, list_executions, etc.) and decides which to
   * call to answer the user. Returns the final natural-language reply
   * + a trace of every tool step + cost info. Honors RBAC + dry-run +
   * idempotency. Strictly preferred over `canvasChat` — operates on
   * live workspace state via tools rather than dumping a snapshot.
   *
   * Returns `no_provider: true` when no LLM is configured; callers
   * fall back to deterministic handlers.
   */
  runAgent: (body: {
    user_intent: string;
    page_context: {
      page: string;
      visible_ids?: string[];
      selected_ids?: string[];
      filters?: Record<string, any>;
      environment?: string;
      visible_items?: Array<{ id: string; name?: string; status?: string; kind?: string; meta?: Record<string, any> }>;
      // 2026-05-22: page-supplied richer payload. Backend sanitizes +
      // budget-caps. See backend/fpulse/ai/context.py:to_extra_context_block.
      extra_context?: Record<string, unknown>;
    };
    // 2026-05-22: rolling conversation memory. recent_turns is the
    // verbatim last N user/assistant pairs; summary compresses older
    // turns. Both optional. Backend caps at 20 turns / 1200-char summary.
    conversation?: {
      recent_turns?: Array<{ role: 'user' | 'assistant'; content: string }>;
      summary?: string;
    };
    // 2026-05-22: routing hint. 'quick' prefers fast-lane shortcuts;
    // 'standard' is the current default; 'deep' skips fast-lane and
    // widens context for hard reasoning questions (with a latency cost
    // — surface that to the user before submitting).
    mode?: 'quick' | 'standard' | 'deep';
    allow_safe_writes?: boolean;
    max_tokens?: number;
    dialogue_state?: Record<string, any>;
  }) =>
    request<{
      run_id: string;
      final_text: string;
      outcome: string;
      iterations: number;
      elapsed_ms: number;
      steps: Array<{
        step_id: string;
        tool_name: string;
        tool_tier: string;
        outcome: string;
        latency_ms: number;
        decision_reason?: string;
      }>;
      tool_results?: any[];
      cost?: { tokens_in: number; tokens_out: number; estimated_usd: number; provider: string; model: string };
      no_provider?: boolean;
      case_file?: Record<string, any>;
    }>('/ai/agent', { method: 'POST', body: JSON.stringify(body) }),

  // Files
  // Legacy: shallow listing of the install's `samples/` dir. Returns
  // `[{ name, size }]`. Kept for back-compat with older nodes that
  // only knew about the sample folder. NEW code should prefer
  // `listStorageFiles` so user-uploaded Storage objects are visible.
  listFiles: () => request<any[]>('/files'),
  // Workspace Storage — the canonical file index (uploaded files +
  // generated reports, all with project/scope metadata). Source nodes
  // (CSV / File / etc.) pick from this list so a user-uploaded file
  // shows up in the dropdown right after upload.
  listStorageFiles: (opts?: { projectId?: string }) => {
    const qs = opts?.projectId ? `?project_id=${encodeURIComponent(opts.projectId)}` : '';
    return request<{ objects: Array<{ id: string; name: string; path: string; size_bytes: number; format: string | null; project_id: string | null; tags: string[] }>; count: number }>(`/storage/files${qs}`);
  },
  // Managed Parquet tables in the workspace. Powers the Managed Table
  // Source / Sink picker so users select an existing schema.table instead
  // of typing the identifier from memory.
  listStorageTables: () =>
    request<{ tables: Array<{ id: string; schema_name: string; name: string; row_count: number | null; column_count: number | null; size_bytes: number; project_id: string | null }>; count: number }>(`/storage/tables`),
  /**
   * Run a read-only SQL query (SELECT / WITH only) against the workspace's
   * managed Parquet tables. Reference tables by `schema.name`, e.g.
   * `SELECT * FROM default.sales`. The backend materialises the referenced
   * tables into an ephemeral DuckDB, locks external access, and returns the
   * columns + rows. Bad SQL / non-SELECT statements come back as an HTTP 400
   * whose `detail` message is surfaced to the caller via the thrown Error.
   */
  storageQuery: (sql: string, limit?: number) =>
    request<{
      columns: Array<{ name: string; type: string }>;
      rows: Array<Record<string, unknown>>;
      row_count: number;
      limit: number;
      truncated: boolean;
      tables_available: string[];
    }>('/storage/query', {
      method: 'POST',
      body: JSON.stringify(limit != null ? { sql, limit } : { sql }),
    }),
  /**
   * Re-index files written directly to disk (e.g. by an external process or
   * a pipeline's raw file sink) that aren't yet in the Storage catalog.
   * Scans the workspace uploads + outputs dirs and creates catalog rows for
   * any orphaned bytes. Returns per-bucket + total counts of newly indexed
   * files.
   */
  storageRescan: () =>
    request<{
      workspace_id: string;
      uploads_indexed: number;
      outputs_indexed: number;
      total_indexed: number;
    }>('/storage/rescan', { method: 'POST' }),
  /**
   * Upload a data file. When `replaces` is the previous file path the
   * same node was using, the backend deletes it after the new upload
   * succeeds — so swapping a node's source file doesn't leave orphans
   * in the data directory.
   */
  uploadFile: async (file: File, options?: { replaces?: string }) => {
    const form = new FormData();
    form.append('file', file);
    // Multipart upload bypasses the JSON request() helper, so we
    // have to attach auth + workspace headers manually or the file
    // would land in the wrong tenant bucket on the backend.
    const token = localStorage.getItem('fpulse_token') || '';
    const workspaceId = currentWorkspaceId();
    const url = new URL(`${BASE}/upload`, window.location.origin);
    if (options?.replaces) {
      url.searchParams.set('replaces', options.replaces);
    }
    const res = await fetch(url.toString().replace(window.location.origin, ''), {
      method: 'POST',
      body: form,
      headers: {
        ...(token ? { Authorization: `Bearer ${token}` } : {}),
        'X-Workspace-Id': workspaceId,
      },
    });
    if (!res.ok) {
      const err = await res.json().catch(() => ({ detail: res.statusText }));
      throw new Error(_humanizeApiError(err.detail) || res.statusText);
    }
    return res.json();
  },

  // Meta
  getNodeTypes: () => request<any[]>('/node-types'),

  // Live {{ }} expression preview (C4). Resolves one expression against a
  // sample row using the SAME backend resolver the executor runs, so the
  // editor preview can't drift from runtime behaviour.
  previewExpression: (body: {
    expression: string;
    sample_row?: Record<string, any> | null;
    vars?: Record<string, any> | null;
    item_index?: number;
    node_samples?: Record<string, any[]> | null;
  }) =>
    request<{ ok: boolean; result?: string; value_type?: string; error?: string }>(
      '/expression/preview',
      { method: 'POST', body: JSON.stringify(body) },
    ),
  health: () => request<any>('/health'),
  aiStatus: () => request<{ ai_available: boolean }>('/planner/ai-status'),

  // Projects
  // 2026-05-22: listProjects now takes optional `include_archived` so
  // the Active and Archived tabs can hit the same endpoint with
  // different filters instead of the old client-side localStorage
  // filter. See backend audit C1.
  listProjects: (opts?: { include_archived?: boolean }) =>
    request<any[]>(
      opts?.include_archived
        ? '/projects/?include_archived=true'
        : '/projects/',
    ),
  projectTree: () => request<any[]>('/projects/tree'),
  createProject: (data: { name: string; description?: string; color?: string; icon?: string; parent_id?: string | null; members?: string[]; metadata?: Record<string, any> }) =>
    request<any>('/projects/', { method: 'POST', body: JSON.stringify(data) }),
  getProject: (id: string) => request<any>(`/projects/${id}`),
  updateProject: (id: string, data: any) =>
    request<any>(`/projects/${id}`, { method: 'PUT', body: JSON.stringify(data) }),
  deleteProject: (id: string) =>
    request<any>(`/projects/${id}`, { method: 'DELETE' }),
  // 2026-05-22: server-side archive/restore. The previous localStorage
  // archive flag was per-browser and unauditable — audit finding C1.
  // archived_at and archived_by are now stamped server-side on archive.
  archiveProject: (id: string) =>
    request<any>(`/projects/${id}/archive`, { method: 'POST' }),
  restoreProject: (id: string) =>
    request<any>(`/projects/${id}/restore`, { method: 'POST' }),
  getProjectPipelines: (id: string) => request<any[]>(`/projects/${id}/pipelines`),

  // Folders (nested inside a project)
  listFolders: (projectId: string) =>
    request<any[]>(`/folders?project_id=${encodeURIComponent(projectId)}`),
  createFolder: (data: { name: string; project_id: string; parent_folder_id?: string | null; description?: string; color?: string; icon?: string }) =>
    request<any>('/folders', { method: 'POST', body: JSON.stringify(data) }),
  getFolder: (id: string) => request<any>(`/folders/${id}`),
  updateFolder: (id: string, data: { name?: string; description?: string; parent_folder_id?: string | null; color?: string; icon?: string }) =>
    request<any>(`/folders/${id}`, { method: 'PATCH', body: JSON.stringify(data) }),
  deleteFolder: (id: string) =>
    request<any>(`/folders/${id}`, { method: 'DELETE' }),
  moveWorkflowsToFolder: (data: { workflow_ids: string[]; folder_id: string | null }) =>
    request<any>('/folders/move-workflows', { method: 'POST', body: JSON.stringify(data) }),

  // Project Approval
  submitProjectForApproval: (id: string) =>
    request<any>(`/projects/${id}/submit-for-approval`, { method: 'POST' }),
  approveProject: (id: string, notes?: string) =>
    request<any>(`/projects/${id}/approve${notes ? `?notes=${encodeURIComponent(notes)}` : ''}`, { method: 'POST' }),
  rejectProject: (id: string, notes: string) =>
    request<any>(`/projects/${id}/reject?notes=${encodeURIComponent(notes)}`, { method: 'POST' }),
  listPendingProjects: () =>
    request<any[]>('/projects/pending-approvals'),

  // Schedules
  listSchedules: (params?: { workflow_id?: string; project_id?: string }) => {
    const qs = new URLSearchParams();
    if (params?.workflow_id) qs.set('workflow_id', params.workflow_id);
    if (params?.project_id) qs.set('project_id', params.project_id);
    const q = qs.toString();
    return request<any[]>(`/schedules/${q ? '?' + q : ''}`);
  },
  createSchedule: (data: any) =>
    request<any>('/schedules/', { method: 'POST', body: JSON.stringify(data) }),
  // 2026-05-22 — idempotent upsert of the "default schedule" per
  // workflow. Used by SaveDialog so repeat saves don't pile up
  // duplicate cron rows. Audit D3.
  upsertDefaultSchedule: (workflowId: string, data: any) =>
    request<any>(`/schedules/by-workflow/${encodeURIComponent(workflowId)}/default`, {
      method: 'PUT',
      body: JSON.stringify(data),
    }),
  updateSchedule: (id: string, data: any) =>
    request<any>(`/schedules/${id}`, { method: 'PUT', body: JSON.stringify(data) }),
  deleteSchedule: (id: string) =>
    request<any>(`/schedules/${id}`, { method: 'DELETE' }),
  toggleSchedule: (id: string) =>
    request<any>(`/schedules/${id}/toggle`, { method: 'POST' }),

  // Alerts
  listAlertRules: (params?: { workflow_id?: string; project_id?: string }) => {
    const qs = new URLSearchParams();
    if (params?.workflow_id) qs.set('workflow_id', params.workflow_id);
    if (params?.project_id) qs.set('project_id', params.project_id);
    const q = qs.toString();
    return request<any[]>(`/alerts/rules${q ? '?' + q : ''}`);
  },
  createAlertRule: (data: any) =>
    request<any>('/alerts/rules', { method: 'POST', body: JSON.stringify(data) }),
  // 2026-05-22 — same idempotent-upsert pattern as schedules. SaveDialog
  // uses this so saving twice doesn't double up the "default" rule.
  upsertDefaultAlertRule: (workflowId: string, data: any) =>
    request<any>(`/alerts/rules/by-workflow/${encodeURIComponent(workflowId)}/default`, {
      method: 'PUT',
      body: JSON.stringify(data),
    }),
  updateAlertRule: (id: string, data: any) =>
    request<any>(`/alerts/rules/${id}`, { method: 'PUT', body: JSON.stringify(data) }),
  deleteAlertRule: (id: string) =>
    request<any>(`/alerts/rules/${id}`, { method: 'DELETE' }),
  testAlert: (id: string) =>
    request<any>(`/alerts/rules/${id}/test`, { method: 'POST' }),
  listAlertLogs: (params?: { workflow_id?: string }) => {
    const qs = new URLSearchParams();
    if (params?.workflow_id) qs.set('workflow_id', params.workflow_id);
    const q = qs.toString();
    return request<any[]>(`/alerts/logs${q ? '?' + q : ''}`);
  },

  // Monitor
  listExecutions: (params?: { workflow_id?: string; project_id?: string }) => {
    const qs = new URLSearchParams();
    if (params?.workflow_id) qs.set('workflow_id', params.workflow_id);
    if (params?.project_id) qs.set('project_id', params.project_id);
    const q = qs.toString();
    return request<any[]>(`/monitor/executions${q ? '?' + q : ''}`);
  },
  getExecution: (id: string) => request<any>(`/monitor/executions/${id}`),

  // ── Step-IO replay (per-step output/input capture for the lineage drawer) ──
  // Backed by the step_outputs store. Outputs are persisted at run-time;
  // inputs are derived server-side by walking the run's IR snapshot.
  getStepOutput: (executionId: string, stepId: string) =>
    request<{
      execution_id: string;
      step_id: string;
      step_index: number;
      step_type: string;
      label: string;
      status: string;
      row_count: number;
      sample_rows: Record<string, any>[];
      sample_bytes: number;
      sample_truncated: boolean;
      sample_pruned: boolean;
      schema: Array<{
        name: string;
        dtype: string;
        nullable?: boolean;
        null_count: number;
        distinct_count: number | null;
        from_sample: boolean;
        sample_size: number;
      }>;
      captured_at: string;
    }>(`/execute/execution/${executionId}/step/${stepId}/output`),
  getStepInput: (executionId: string, stepId: string) =>
    request<{
      execution_id: string;
      step_id: string;
      inputs: Array<{
        source_step_id: string;
        label: string;
        row_count: number;
        sample_rows: Record<string, any>[];
        sample_truncated: boolean;
        sample_pruned: boolean;
        schema: Array<any>;
        missing: boolean;
      }>;
    }>(`/execute/execution/${executionId}/step/${stepId}/input`),
  getExecutionEdges: (executionId: string) =>
    request<{
      execution_id: string;
      edges: Array<{
        from_step: string;
        to_step: string;
        row_count: number;
        from_status: string;
      }>;
    }>(`/execute/execution/${executionId}/edges`),
  exportStepOutput: async (
    executionId: string,
    stepId: string,
    fmt: 'csv' | 'json',
  ): Promise<Blob> => {
    const token = localStorage.getItem('fpulse_token');
    const workspaceId = currentWorkspaceId();
    const res = await fetch(
      `${BASE}/execute/execution/${executionId}/step/${stepId}/output/export?fmt=${fmt}`,
      {
        headers: {
          ...(token ? { Authorization: `Bearer ${token}` } : {}),
          'X-Workspace-Id': workspaceId,
        },
      },
    );
    if (!res.ok) {
      const detail = await res.json().catch(() => ({}));
      throw new Error(detail?.detail || `Export failed (${res.status})`);
    }
    return await res.blob();
  },
  getMonitorStats: (hours?: number) =>
    request<any>(`/monitor/stats${hours ? '?hours=' + hours : ''}`),
  getMultiStats: () => request<any>('/monitor/stats/multi'),

  // 2026-05-22 (audit J2 / L1) — single authoritative dashboard
  // summary endpoint. Replaces the previous 15-API Promise.all
  // chain in DashboardPage. Each section in the response carries
  // its own status (loaded | failed) so the UI can render a
  // per-card warning instead of silently zeroing the count.
  dashboardSummary: (opts?: {
    environment?: 'dev' | 'prod' | 'all';
    project_id?: string;
    hours?: number;
  }) => {
    const qs = new URLSearchParams();
    if (opts?.environment) qs.set('environment', opts.environment);
    if (opts?.project_id) qs.set('project_id', opts.project_id);
    if (opts?.hours) qs.set('hours', String(opts.hours));
    const q = qs.toString();
    return request<{
      version: number;
      generated_at: string;
      scope: { workspace_id: string; environment: string; project_id: string | null; hours: number };
      inventory:  { status: 'loaded' | 'failed' | 'stale'; data: any; error?: string };
      executions: { status: 'loaded' | 'failed' | 'stale'; data: any; error?: string };
      top_failed: { status: 'loaded' | 'failed' | 'stale'; data: any[]; error?: string };
      slowest:    { status: 'loaded' | 'failed' | 'stale'; data: any[]; error?: string };
      pool:       { status: 'loaded' | 'failed' | 'stale'; data: any; error?: string };
      system:     { status: 'loaded' | 'failed' | 'stale'; data: any; error?: string };
    }>(`/dashboard/summary${q ? '?' + q : ''}`);
  },
  getActiveSchedules: () => request<any[]>('/monitor/active-schedules'),
  getFailedPipelines: () => request<any[]>('/monitor/failed'),
  // Read-only summary — safe for developers to call.
  // Returns deployed pipelines, active schedules, 24h run stats, open
  // alerts, and a health score. Every call is audit-logged on the backend.
  getProdGlance: () => request<{
    deployed_pipelines: { count: number; items: any[] };
    active_schedules: { count: number; items: any[] };
    runs_24h: { total: number; success: number; error: number; running: number };
    recent_runs: any[];
    alerts: { open: number; items: any[] };
    health: { success_rate: number; status: 'healthy' | 'degraded' | 'unhealthy' };
    viewer: { role: string; read_only: boolean; environment: string };
  }>('/monitor/prod-glance'),
  getSystemMetrics: () => request<any>('/system/metrics'),
  getResourceAlerts: () => request<any>('/system/resource-alerts'),

  // Auth
  login: (email: string, password: string) =>
    request<any>('/auth/login', { method: 'POST', body: JSON.stringify({ email, password }) }),
  register: (email: string, password: string, name?: string) =>
    request<any>('/auth/register', { method: 'POST', body: JSON.stringify({ email, password, name }) }),
  getMe: () => {
    const token = localStorage.getItem('fpulse_token');
    return request<any>('/auth/me', { headers: { 'Content-Type': 'application/json', Authorization: `Bearer ${token}` } });
  },
  logout: () => {
    const token = localStorage.getItem('fpulse_token');
    return request<any>('/auth/logout', { method: 'POST', headers: { 'Content-Type': 'application/json', Authorization: `Bearer ${token}` } });
  },
  listUsers: () => request<any[]>('/auth/users'),
  inviteUser: (data: { email: string; name?: string; role?: string; projects?: string[]; workspace_id?: string }) =>
    request<any>('/auth/invite', { method: 'POST', body: JSON.stringify(data) }),
  deleteUser: (id: string) => request<any>(`/auth/users/${id}`, { method: 'DELETE' }),
  // Self-service profile endpoints — backed by /auth/me/* on the server.
  // These work for any authenticated user; admin role NOT required.
  updateMyProfile: (data: { name?: string }) =>
    request<{ updated: boolean; user?: any; reason?: string }>(
      '/auth/me/profile',
      { method: 'PUT', body: JSON.stringify(data) },
    ),
  listMySessions: () =>
    request<{ sessions: Array<{ id: string; created_at: string; ip_address: string; machine_id: string; is_current: boolean }>; count: number }>(
      '/auth/me/sessions',
    ),
  changeMyPassword: (current_password: string, new_password: string) =>
    request<{ changed: boolean }>('/plus/users/change-password', {
      method: 'POST',
      body: JSON.stringify({ current_password, new_password }),
    }),

  // Variables
  listVariables: (params?: { scope?: string; project_id?: string }) => {
    const qs = new URLSearchParams();
    if (params?.scope) qs.set('scope', params.scope);
    if (params?.project_id) qs.set('project_id', params.project_id);
    const q = qs.toString();
    return request<any[]>(`/variables/${q ? '?' + q : ''}`);
  },
  createVariable: (data: { key: string; value: string; type?: string; scope?: string; project_id?: string; description?: string }) =>
    request<any>('/variables/', { method: 'POST', body: JSON.stringify(data) }),
  updateVariable: (id: string, data: any) =>
    request<any>(`/variables/${id}`, { method: 'PUT', body: JSON.stringify(data) }),
  deleteVariable: (id: string) =>
    request<any>(`/variables/${id}`, { method: 'DELETE' }),

  // Credentials
  listCredentials: (params?: { type?: string; project_id?: string }) => {
    const qs = new URLSearchParams();
    if (params?.type) qs.set('type', params.type);
    if (params?.project_id) qs.set('project_id', params.project_id);
    const q = qs.toString();
    return request<any[]>(`/credentials/${q ? '?' + q : ''}`);
  },
  createCredential: (data: { name: string; type: string; config?: Record<string, any>; project_id?: string }) =>
    request<any>('/credentials/', { method: 'POST', body: JSON.stringify(data) }),
  updateCredential: (id: string, data: any) =>
    request<any>(`/credentials/${id}`, { method: 'PUT', body: JSON.stringify(data) }),
  deleteCredential: (id: string) =>
    request<any>(`/credentials/${id}`, { method: 'DELETE' }),
  // testCredential removed (May 9 2026) — credentials are pure secret
  // material; "test" only makes sense alongside a host + port + protocol,
  // which lives on the Connection record. The backend endpoint
  // POST /credentials/{id}/test is kept for backwards compatibility but
  // no UI surface calls it. Use api.testConnection(connId) instead.

  // (Removed in PR 5 — May 17 2026: listGalleryTemplates +
  // useGalleryTemplate. The TemplatesPage reads built-in templates
  // straight from `src/templates/catalog.ts`; the backend's
  // `/api/templates` GET endpoints were orphaned. User-template helpers
  // — listUserTemplates / saveUserTemplate / deleteUserTemplate —
  // remain above and still hit `/api/templates/user`.)

  // Connections
  listConnections: (params?: { project_id?: string; scope?: string }) => {
    const qs = new URLSearchParams();
    if (params?.project_id) qs.set('project_id', params.project_id);
    if (params?.scope) qs.set('scope', params.scope);
    const q = qs.toString();
    return request<any[]>(`/connections/${q ? '?' + q : ''}`);
  },
  getConnectionMetadata: () =>
    request<{ types: string[]; categories: Record<string, string[]>; storage_types: string[]; file_formats: string[] }>('/connections/metadata'),
  createConnection: (data: { name: string; type: string; description?: string; config?: Record<string, any>; tags?: string[]; project_id?: string | null; environment?: string | null; capabilities?: string[] }) =>
    request<any>('/connections/', { method: 'POST', body: JSON.stringify(data) }),
  getConnection: (id: string) => request<any>(`/connections/${id}`),
  updateConnection: (id: string, data: { name?: string; description?: string; config?: Record<string, any>; tags?: string[]; project_id?: string | null; environment?: string | null; capabilities?: string[] }) =>
    request<any>(`/connections/${id}`, { method: 'PUT', body: JSON.stringify(data) }),
  deleteConnection: (id: string) => request<any>(`/connections/${id}`, { method: 'DELETE' }),
  testConnection: (id: string, signal?: AbortSignal) => request<any>(`/connections/${id}/test`, { method: 'POST', signal }),
  getConnectionTableColumns: (id: string, table: string, schema?: string) => {
    const qs = new URLSearchParams({ table });
    if (schema) qs.set('schema', schema);
    return request<{
      columns: Array<{ name: string; type: string; nullable: boolean }>;
      schema: string;
      table: string;
    }>(`/connections/${id}/columns?${qs.toString()}`);
  },
  getConnectionCatalog: (id: string) => request<{
    supported: boolean;
    reason: string;
    items: Array<{ name: string; kind: string; parent: string; metadata: Record<string, any> }>;
    parents: string[];
    kinds: string[];
    category?: string;
    auth?: string;
    tier?: string;
  }>(`/connections/${id}/catalog`),
  listConnectionReports: (connId: string) => request<any[]>(`/connections/${connId}/reports`),
  createConnectionReport: (connId: string, data: { name: string; description?: string; query_template: string; parameters?: any[] }) =>
    request<any>(`/connections/${connId}/reports`, { method: 'POST', body: JSON.stringify(data) }),
  deleteConnectionReport: (connId: string, reportId: string) =>
    request<any>(`/connections/${connId}/reports/${reportId}`, { method: 'DELETE' }),
  runConnectionReport: (connId: string, reportId: string, params: Record<string, string>) =>
    request<any>(`/connections/${connId}/reports/${reportId}/run`, { method: 'POST', body: JSON.stringify({ params }) }),

  // Intelligence — Schema Detection
  detectSchema: (filePath: string) =>
    request<any>('/intelligence/detect-schema', { method: 'POST', body: JSON.stringify({ file_path: filePath }) }),
  flattenData: (filePath: string) =>
    request<any>('/intelligence/flatten', { method: 'POST', body: JSON.stringify({ file_path: filePath }) }),
  suggestPipeline: (schema: any) =>
    request<any>('/intelligence/suggest-pipeline', { method: 'POST', body: JSON.stringify({ schema }) }),
  analyzeStep: (workflowId: string, stepId: string) =>
    request<any>(`/intelligence/analyze/${workflowId}/step/${stepId}`),
  optimizeExecution: (workflowId: string) =>
    request<any>(`/intelligence/optimize/${workflowId}`, { method: 'POST' }),
  estimateExecution: (workflowId: string) =>
    request<any>(`/intelligence/estimate/${workflowId}`),

  // AI Assist — interactive node config helper
  aiAssistNode: (data: { stepType: string; params: any; prompt: string; nodeId: string }) =>
    request<any>('/ai/assist-node', { method: 'POST', body: JSON.stringify(data) }),

  // Pipeline Lifecycle
  testWorkflow: (id: string) =>
    request<any>(`/workflows/${id}/test`, { method: 'POST' }),
  publishWorkflow: (id: string) =>
    request<any>(`/workflows/${id}/publish`, { method: 'POST' }),
  revokeWorkflow: (id: string) =>
    request<any>(`/workflows/${id}/revoke`, { method: 'POST' }),
  archiveWorkflow: (id: string) =>
    request<any>(`/workflows/${id}/archive`, { method: 'POST' }),
  restoreWorkflow: (id: string) =>
    request<any>(`/workflows/${id}/restore`, { method: 'POST' }),
  getWorkflowLifecycle: (id: string) =>
    request<any>(`/workflows/${id}/lifecycle`),

  // Pipeline documentation (self-documenting pipelines)
  getWorkflowDocs: (id: string) =>
    request<{ workflow_id: string; filename: string; markdown: string }>(
      `/workflows/${id}/docs?format=json`,
    ),
  updateWorkflowDocs: (
    id: string,
    data: { business_purpose?: string; readme?: string; tags?: string[] },
  ) =>
    request<any>(`/workflows/${id}/docs`, {
      method: 'PATCH',
      body: JSON.stringify(data),
    }),
  // Admin publish policy — is a business purpose required before publishing?
  getPublishPolicy: () =>
    request<{ require_business_purpose: boolean; setting_value: boolean; env_override: boolean }>(
      `/admin/publish-policy`,
    ),
  setPublishPolicy: (requireBusinessPurpose: boolean) =>
    request<any>(`/admin/publish-policy`, {
      method: 'PUT',
      body: JSON.stringify({ require_business_purpose: requireBusinessPurpose }),
    }),

  // Schema Contracts
  listContracts: (workflowId: string) =>
    request<any[]>(`/contracts/${workflowId}`),
  createContract: (data: { workflow_id: string; step_id: string; expected_columns: any[] }) =>
    request<any>('/contracts/', { method: 'POST', body: JSON.stringify(data) }),
  validateContract: (contractId: string, actualSchema: any[]) =>
    request<any>(`/contracts/validate/${contractId}`, { method: 'POST', body: JSON.stringify({ columns: actualSchema }) }),
  checkDrift: (contractId: string) =>
    request<any>(`/contracts/drift/${contractId}`),
  autoCreateContracts: (workflowId: string) =>
    request<any>(`/contracts/auto-create/${workflowId}`, { method: 'POST' }),

  // ── Commercial-extension endpoints (no-op when not installed) ──

  // License — module-level cache + in-flight dedup. Every caller in the
  // app routes through this function, so chatter from Dashboard/Admin/
  // Account/App-mount is collapsed to AT MOST one network call per
  // 5-minute TTL window across the whole app. The earlier App.tsx-local
  // cache only deduped App.tsx's own calls; this fix covers everyone.
  //
  // Cache is busted by:
  //   - clearLicenseCache() — called explicitly after activate/deactivate
  //   - dispatching `fpulse:license-changed` event (App.tsx listens)
  //   - logout (clears localStorage; in-memory cache is per-tab anyway)
  getLicenseStatus: () => _getLicenseCached(),
  activateLicense: (data: { org: string; email: string; seats?: number }) =>
    request<any>('/plus/license/activate', { method: 'POST', body: JSON.stringify(data) }),
  deactivateLicense: () =>
    request<any>('/plus/license/deactivate', { method: 'POST' }),

  // Sandbox — PROD deploy-preview runs against real connections
  // with destinations rewritten to a scratch namespace. All four endpoints
  // require approver/admin role; backend gates them.
  createSandboxRun: (data: { approval_id: string; row_limit?: number; ttl_hours?: number }) =>
    request<any>('/plus/sandbox/runs', { method: 'POST', body: JSON.stringify(data) }),
  getSandboxRun: (runId: string) =>
    request<any>(`/plus/sandbox/runs/${runId}`),
  getSandboxRunOutput: (runId: string) =>
    request<any>(`/plus/sandbox/runs/${runId}/output`),
  deleteSandboxRun: (runId: string) =>
    request<any>(`/plus/sandbox/runs/${runId}`, { method: 'DELETE' }),

  // Approval flow — Gate 1 = existing approveWorkflow.
  // Gate 2 = submit-for-deploy (after sandbox) → approve-deploy.
  submitForDeploy: (workflowId: string, submittedBy?: string) => {
    const qs = submittedBy ? `?submitted_by=${encodeURIComponent(submittedBy)}` : '';
    return request<any>(`/workflows/${workflowId}/submit-for-deploy${qs}`, { method: 'POST' });
  },
  approveDeploy: (workflowId: string, approvedBy?: string, notes: string = '') => {
    const params = new URLSearchParams();
    if (approvedBy) params.set('approved_by', approvedBy);
    if (notes) params.set('notes', notes);
    const qs = params.toString();
    return request<any>(`/workflows/${workflowId}/approve-deploy${qs ? '?' + qs : ''}`, { method: 'POST' });
  },

  // Workspace settings — admin knobs.
  // GET available to any authenticated user; PUT admin-only.
  // Patch shape: only the keys you want to change.
  getWorkspaceSettings: () => request<{ workspace_id: string; settings: Record<string, any> }>('/plus/workspace-settings'),
  updateWorkspaceSettings: (patch: Record<string, any>) =>
    request<{ workspace_id: string; settings: Record<string, any> }>(
      '/plus/workspace-settings', { method: 'PUT', body: JSON.stringify({ patch }) },
    ),

  // Pool allocation — per-workspace logical pool split.
  // GET is open to any authenticated user (read-only KPI cards);
  // PUT is admin-only (slider that rebalances PROD/DEV/burst).
  getPoolAllocation: () => request<any>('/plus/pool/allocation'),
  updatePoolAllocation: (
    data: { prod_reserved_pct: number; dev_reserved_pct: number; burst_pct: number },
  ) => request<any>('/plus/pool/allocation', {
    method: 'PUT', body: JSON.stringify(data),
  }),
  getPoolQueueDepth: () => request<any>('/plus/pool/allocation/queue-depth'),

  // Stop a running workflow execution. Hits the workflow-scoped cancel
  // endpoint which (a) cancels any live registered handles via the
  // ExecutionManager and (b) soft-cancels the execution-store rows so
  // the UI stops showing a stuck "Running" state. Idempotent — returns
  // {cancelled_handles: 0, cancelled_rows: 0} when nothing is running.
  cancelExecution: (workflowId: string) =>
    request<any>(`/workflows/${workflowId}/cancel`, { method: 'POST' }),

  // Pipeline Activate / Deactivate
  // DEV: direct flip via toggleActive.
  // PROD: requestLifecycleToggle creates an approval-pending row;
  //       admin decides via decideLifecycleToggle.
  toggleActive: (workflowId: string, active: boolean, env: 'dev' | 'prod' = 'dev') =>
    request<any>(`/workflows/${workflowId}/toggle-active`, {
      method: 'POST', body: JSON.stringify({ active, env }),
    }),
  requestLifecycleToggle: (workflowId: string, action: 'activate' | 'deactivate', reason: string = '', target_env: 'dev' | 'prod' = 'prod') =>
    request<any>(`/workflows/${workflowId}/request-toggle`, {
      method: 'POST', body: JSON.stringify({ action, target_env, reason }),
    }),
  listPendingLifecycleRequests: () =>
    request<any[]>('/plus/lifecycle-requests'),
  decideLifecycleToggle: (requestId: string, decision: 'approved' | 'rejected', notes: string = '') =>
    request<any>(`/plus/lifecycle-requests/${requestId}/decide`, {
      method: 'POST', body: JSON.stringify({ decision, notes }),
    }),

  // Environments
  listEnvironments: () => request<any[]>('/plus/environments'),
  validateForEnvironment: (env: string, workflowId: string) =>
    request<any>(`/plus/environments/${env}/validate/${workflowId}`, { method: 'POST' }),
  getEnvironmentPolicy: (env: string) =>
    request<any>(`/plus/environments/${env}/policy`),

  // Audit Trail
  queryAuditLog: (params?: { user_id?: string; action?: string; resource_type?: string; since?: string; limit?: number }) => {
    const qs = new URLSearchParams();
    if (params?.user_id) qs.set('user_id', params.user_id);
    if (params?.action) qs.set('action', params.action);
    if (params?.resource_type) qs.set('resource_type', params.resource_type);
    if (params?.since) qs.set('since', params.since);
    if (params?.limit) qs.set('limit', String(params.limit));
    const q = qs.toString();
    return request<any[]>(`/plus/audit${q ? '?' + q : ''}`);
  },
  getAuditStats: () => request<any>('/plus/audit/stats'),
  getAuditMonthly: (months?: number) => {
    const qs = new URLSearchParams();
    if (months) qs.set('months', String(months));
    const q = qs.toString();
    return request<any>(`/plus/audit/monthly${q ? '?' + q : ''}`);
  },
  generateAuditSnapshot: (year: number, month: number) =>
    request<any>(`/plus/audit/snapshot/${year}/${month}`, { method: 'POST' }),
  runAuditRetention: (keepMonths?: number) => {
    const qs = new URLSearchParams();
    if (keepMonths) qs.set('keep_months', String(keepMonths));
    const q = qs.toString();
    return request<any>(`/plus/audit/retention${q ? '?' + q : ''}`, { method: 'POST' });
  },
  listAuditExports: () => request<any[]>('/plus/audit/exports'),
  clearAuditLog: (before?: string) =>
    request<any>('/plus/audit/clear', { method: 'POST', body: JSON.stringify({ before }) }),
  getStage3bDualWriteStatus: () =>
    request<{
      audit_log: Record<string, number>;
      lifecycle_events: Record<string, number>;
      alert_logs: Record<string, number>;
    }>('/plus/stage3b/dual-write-status'),

  // Encryption
  getEncryptionStatus: () => request<any>('/plus/encryption/status'),
  rotateEncryptionKey: () =>
    request<any>('/plus/encryption/rotate-key', { method: 'POST' }),

  // Admin Settings
  getAdminSettings: () => request<any>('/plus/admin/settings'),
  updateAdminSettings: (settings: Record<string, any>) =>
    request<any>('/plus/admin/settings', { method: 'PUT', body: JSON.stringify(settings) }),

  // Signup policy — public endpoint consumed by LoginPage to decide
  // whether to show the Register tab. Returns { allow_self_registration,
  // first_user_bootstrap }. `first_user_bootstrap=true` means the user
  // table is empty and the very first /register call is allowed
  // regardless of the flag (so an operator can create the initial
  // super_admin account on a brand-new instance).
  getSignupPolicy: () => request<any>('/auth/signup-policy'),

  // ── Password policy + recovery ──
  // All public — the LoginPage and Register form call them before any
  // session token exists. The backend mirrors these helpers in
  // fpulse/auth/password_policy.py; the two implementations agree on
  // what counts as a strong password so the UI never shows "Strong"
  // on something the server will reject.
  getPasswordPolicy: () =>
    request<{
      min_length: number;
      require_lower: boolean;
      require_upper: boolean;
      require_digit: boolean;
      require_symbol: boolean;
      block_common: boolean;
      block_email_in_password: boolean;
      rules: string[];
    }>('/auth/password-policy'),
  checkPassword: (password: string, email = '', name = '') =>
    request<{
      ok: boolean;
      score: number;
      label: string;
      failures: string[];
      suggestions: string[];
    }>('/auth/check-password', {
      method: 'POST',
      body: JSON.stringify({ password, email, name }),
    }),
  generateStrongPassword: (length = 20) =>
    request<{ password: string; length: number }>(
      `/auth/generate-password?length=${length}`,
    ),
  // Self-serve change-password — uses the new free-tier-compatible
  // endpoint at /auth/me/password (the existing /plus/users/change-password
  // entries above are Plus-only and we keep them for backwards compat).
  changeMyOwnPassword: (current_password: string, new_password: string) =>
    request<{ changed: boolean }>('/auth/me/password', {
      method: 'POST',
      body: JSON.stringify({ current_password, new_password }),
    }),
  // Admin-only — generates and returns a one-time temp password for the
  // target user. Caller is the admin; the temp password is shown ONCE
  // and should be relayed to the affected user out-of-band.
  adminResetPassword: (userId: string) =>
    request<{ user_id: string; email: string; temp_password: string; message: string }>(
      `/auth/users/${userId}/reset-password`,
      { method: 'POST' },
    ),
  // Public — the LoginPage 'Forgot password?' link posts here. Response
  // is uniform regardless of whether the email exists (anti-enumeration).
  // When the email matches a real user, the response includes a
  // single-use `reset_token` the UI can hand straight to the reset
  // screen without a separate email step — F-Pulse OSS doesn't
  // require SMTP, so the self-serve flow is built on this in-band
  // token handoff. `reset_token` is null when the email didn't match.
  forgotPassword: (email: string) =>
    request<{
      queued: boolean;
      message: string;
      reset_token: string | null;
      expires_at: string | null;
      ttl_seconds: number | null;
    }>('/auth/forgot-password', {
      method: 'POST',
      body: JSON.stringify({ email }),
    }),
  // Public — validates a reset token before the UI shows the "pick a
  // new password" form. 404 means the token is missing / expired / used.
  verifyResetToken: (token: string) =>
    request<{ valid: boolean; email: string; expires_at: string }>(
      `/auth/reset-password/verify/${encodeURIComponent(token)}`,
    ),
  // Public — consume a reset token and set a new password. Same
  // password-strength error shape as /register, so the UI can reuse
  // the same checklist rendering.
  resetPassword: (token: string, newPassword: string) =>
    request<{ reset: boolean; email: string; message: string }>(
      '/auth/reset-password',
      {
        method: 'POST',
        body: JSON.stringify({ token, new_password: newPassword }),
      },
    ),
  // Public — the LoginPage 'Request access' form posts here. Same
  // anti-enumeration uniform response shape.
  requestAccess: (email: string, name: string, reason: string) =>
    request<{ queued: boolean; message: string }>('/auth/request-access', {
      method: 'POST',
      body: JSON.stringify({ email, name, reason }),
    }),
  // Admin-only — pulls the auth queue (forgot-password requests +
  // access requests) so the Admin page can render the pending list.
  getAuthQueue: () =>
    request<{
      forgot_password: Array<{ email: string; user_id?: string; requested_at: string; ip: string }>;
      access_requests: Array<{ email: string; name: string; reason: string; requested_at: string; ip: string }>;
      reset_tokens?: Array<{
        token: string;
        user_id: string;
        email: string;
        created_at: string;
        expires_at: string;
        used: boolean;
        used_at: string | null;
        ip: string;
      }>;
    }>('/auth/auth-queue'),
  // Admin-only — drop one entry from the queue once it's been handled.
  dismissQueueItem: (kind: 'forgot_password' | 'access_requests', email: string) =>
    request<{ dismissed: boolean; remaining: number }>('/auth/auth-queue/dismiss', {
      method: 'POST',
      body: JSON.stringify({ kind, email }),
    }),

  // OIDC / SSO config (admin only). Secret never round-trips: PUT with an
  // empty client_secret leaves the existing one in place.
  getOidcConfig: () => request<any>('/plus/admin/oidc'),
  updateOidcConfig: (cfg: Record<string, any>) =>
    request<any>('/plus/admin/oidc', { method: 'PUT', body: JSON.stringify(cfg) }),

  // Session Management
  getActiveSessions: () => request<any>('/plus/sessions/active'),
  revokeUserSessions: (userId: string) =>
    request<any>(`/plus/sessions/revoke/${userId}`, { method: 'POST' }),

  // User Management (Enhanced)
  listUsersEnhanced: () => request<any>('/plus/users'),
  updateUserRole: (userId: string, role: string, projects?: string[], prodPermissions?: Record<string, string[]>) =>
    request<any>(`/plus/users/${userId}/role`, {
      method: 'PUT',
      body: JSON.stringify({ role, projects, prod_permissions: prodPermissions }),
    }),
  getUserProdPermissions: (userId: string) =>
    request<any>(`/plus/users/${userId}/prod-permissions`),
  updateUserProdPermissions: (userId: string, permissions: Record<string, string[]>) =>
    request<any>(`/plus/users/${userId}/prod-permissions`, {
      method: 'PUT',
      body: JSON.stringify({ permissions }),
    }),
  deactivateUser: (userId: string) =>
    request<any>(`/plus/users/${userId}/deactivate`, { method: 'POST' }),
  activateUser: (userId: string) =>
    request<any>(`/plus/users/${userId}/activate`, { method: 'POST' }),
  changePassword: (currentPassword: string, newPassword: string) =>
    request<any>('/plus/users/change-password', {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        'Authorization': `Bearer ${localStorage.getItem('fpulse_token') || ''}`,
      },
      body: JSON.stringify({ current_password: currentPassword, new_password: newPassword }),
    }),

  // ── Execution Pool (Spark-style admin) ──
  getPoolStatus: () => request<any>('/pool/status'),
  getPoolHistory: (limit = 100) => request<any[]>(`/pool/history?limit=${limit}`),
  cancelPoolJob: (jobId: string) => request<any>(`/pool/cancel/${jobId}`, { method: 'POST' }),
  getPoolConfig: () => request<any>('/pool/config'),
  getConnectionPoolStats: () => request<{
    installed: boolean;
    total_entries: number;
    by_connection: Record<string, number>;
    by_run: Record<string, number>;
    max_per_connection: number;
  }>('/pool/connections'),

  // ── Notifications (in-app bell) ──
  listNotifications: (unreadOnly = false, limit = 50) =>
    request<any[]>(`/notifications/?unread_only=${unreadOnly}&limit=${limit}`),
  getUnreadCount: () =>
    request<{ unread: number }>('/notifications/count'),
  markNotificationRead: (id: string) =>
    request<any>(`/notifications/${id}/read`, { method: 'POST' }),
  markAllNotificationsRead: () =>
    request<any>('/notifications/read-all', { method: 'POST' }),
  deleteNotification: (id: string) =>
    request<{ deleted: boolean }>(`/notifications/${id}`, { method: 'DELETE' }),
  clearNotifications: (onlyRead = false) =>
    request<{ deleted: number }>(`/notifications/?only_read=${onlyRead}`, { method: 'DELETE' }),

  // ── Notification config (admin-only, May 3 2026) ──
  // Reads/writes the workspace-wide config that the watchdog + scheduler
  // consult. Without this round-trip, the SettingsPage UI saves only to
  // browser localStorage and the backend never sees the operator's choices.
  getNotificationConfig: () =>
    request<Record<string, any>>('/notifications/config'),
  putNotificationConfig: (config: Record<string, any>) =>
    request<Record<string, any>>('/notifications/config', {
      method: 'PUT',
      body: JSON.stringify(config),
    }),

  // ── Telemetry consent (admin-only, May 3 2026) ──
  getTelemetryConsent: () =>
    request<{ enabled: boolean; consented_at: string | null }>('/notifications/telemetry/consent'),
  putTelemetryConsent: (enabled: boolean) =>
    request<{ enabled: boolean }>('/notifications/telemetry/consent', {
      method: 'PUT',
      body: JSON.stringify({ enabled }),
    }),

  // ── Connector certification matrix (Gate 3, May 4 2026) ──
  getCertMatrix: () =>
    request<{
      audited_at: string;
      total: number;
      v2_total: number;
      production_total: number;
      by_label: Record<string, number>;
      by_category: Record<string, number>;
      rows: Array<{
        id: string;
        display_name: string;
        category: string;
        vendor: string;
        manifest_version: number;
        depth_score: number;
        depth_label: string;
        validation_status: 'pass' | 'fail' | 'unvalidated';
        issues_count: number;
        streams_count: number;
        last_error?: string;
        // #12 step 1 — capability/role flags for the picker UI.
        // Optional because older clients may receive a backend that
        // hasn't shipped these yet. Frontend chip components fall back
        // to a "capabilities unknown" rendering when absent.
        roles?: Array<'source' | 'sink' | 'action' | 'trigger'>;
        capabilities?: {
          source?: boolean;
          sink?: boolean;
          action?: boolean;
          trigger?: boolean;
          pagination?: boolean;
          incremental?: boolean;
          schema?: boolean;
          test?: boolean;
        };
      }>;
    }>('/connectors/cert-matrix'),
  getCertMatrixDetail: (id: string) =>
    request<any>(`/connectors/cert-matrix/${encodeURIComponent(id)}`),

  // ── Trust posture (Gate 4, May 4 2026) ──
  getTrustPosture: () => request<any>('/trust/posture'),
  getTrustEvalSummary: () => request<any>('/trust/eval-summary'),
  getSupportedModels: () => request<any>('/trust/supported-models'),

  // ── Product knowledge (Layer 2 chat RAG, May 4 2026) ──
  // Status is open; reindex is admin-only and triggers a fresh chunk +
  // embed pass over docs/product_facts/. Idempotent.
  getProductKnowledgeStatus: () =>
    request<{
      ran_at: string | null;
      files: number;
      chunks: number;
      duration_ms: number;
      trigger: 'startup' | 'admin' | null;
      error: string | null;
      facts_dir_exists: boolean;
    }>('/ai/product-knowledge/status'),
  reindexProductKnowledge: () =>
    request<{
      ran_at: string;
      files: number;
      chunks: number;
      duration_ms: number;
      trigger: string;
      error: string | null;
    }>('/ai/product-knowledge/reindex', { method: 'POST' }),

  // ── Backup & Restore ──
  listBackups: (provider: string = 'local') =>
    request<any[]>(`/backup/list?provider=${encodeURIComponent(provider)}`),
  createBackup: (config?: any) =>
    request<any>('/backup/create', { method: 'POST', body: JSON.stringify(config || { provider: 'local' }) }),
  restoreBackup: (backup_key: string, config?: any) =>
    request<any>('/backup/restore', {
      method: 'POST',
      body: JSON.stringify({ backup_key, ...(config ? { config } : {}) }),
    }),
  deleteBackup: (backup_key: string, provider: string = 'local') =>
    request<any>(`/backup/${encodeURIComponent(backup_key)}?provider=${encodeURIComponent(provider)}`, {
      method: 'DELETE',
    }),
  testBackupProvider: (config: any) =>
    request<any>('/backup/test-provider', { method: 'POST', body: JSON.stringify(config) }),

  // ── Backup Schedule & Status ──
  getBackupSettings: () =>
    request<any>('/backup/settings'),
  updateBackupSettings: (settings: any) =>
    request<any>('/backup/settings', { method: 'PUT', body: JSON.stringify(settings) }),
  getBackupStatus: () =>
    request<any>('/backup/status'),
  previewBackup: (backupKey: string, config?: any) =>
    request<any>(`/backup/${encodeURIComponent(backupKey)}/preview`, {
      method: 'POST',
      body: JSON.stringify(config || { provider: 'local' }),
    }),

  // ── Recipes (V2 round 2, 2026-05-26) ──
  // Reusable transform sequences. Backend: /api/recipes. See
  // backend/fpulse/api/recipes.py for endpoint contracts.
  listRecipes: () =>
    request<{ recipes: Array<{
      id: string;
      name: string;
      description: string;
      steps: Array<{ op: string; params: Record<string, unknown>; enabled: boolean; name: string }>;
      tags: string[];
      created_at: string;
      updated_at: string;
    }>; count: number }>('/recipes'),
  createRecipe: (body: {
    name: string;
    description?: string;
    steps?: Array<{ op: string; params?: Record<string, unknown>; enabled?: boolean; name?: string }>;
    tags?: string[];
  }) =>
    request<any>('/recipes', { method: 'POST', body: JSON.stringify(body) }),
  getRecipe: (id: string) => request<any>(`/recipes/${encodeURIComponent(id)}`),
  updateRecipe: (id: string, body: Partial<{
    name: string;
    description: string;
    steps: Array<{ op: string; params?: Record<string, unknown>; enabled?: boolean; name?: string }>;
    tags: string[];
  }>) =>
    request<any>(`/recipes/${encodeURIComponent(id)}`, {
      method: 'PUT',
      body: JSON.stringify(body),
    }),
  deleteRecipe: (id: string) =>
    request<{ deleted: boolean; id: string }>(`/recipes/${encodeURIComponent(id)}`, { method: 'DELETE' }),
  cloneRecipe: (id: string) =>
    request<any>(`/recipes/${encodeURIComponent(id)}/clone`, { method: 'POST' }),
  recipeUsedBy: (id: string) =>
    request<{ recipe_id: string; pipelines: Array<{ id: string; name: string }> }>(
      `/recipes/${encodeURIComponent(id)}/used-by`,
    ),

  // ── Deployments (N10 round 2, 2026-05-26) ──
  // Named (workflow + parameters + schedule + worker_pool) bundles.
  // Backend: /api/deployments. See backend/fpulse/api/deployments.py.
  listDeployments: (workflow_id?: string) => {
    const qs = workflow_id ? `?workflow_id=${encodeURIComponent(workflow_id)}` : '';
    return request<{ deployments: any[]; count: number }>(`/deployments${qs}`);
  },
  createDeployment: (body: {
    workflow_id: string;
    name: string;
    description?: string;
    parameters?: Record<string, unknown>;
    schedule?: { cron: string; timezone?: string } | null;
    worker_pool?: string;
    enabled?: boolean;
    environment?: 'dev' | 'prod';
  }) => request<any>('/deployments', { method: 'POST', body: JSON.stringify(body) }),
  getDeployment: (id: string) => request<any>(`/deployments/${encodeURIComponent(id)}`),
  updateDeployment: (id: string, body: Partial<{
    name: string;
    description: string;
    parameters: Record<string, unknown>;
    schedule: { cron: string; timezone?: string } | null;
    worker_pool: string;
    enabled: boolean;
    environment: 'dev' | 'prod';
  }>) => request<any>(`/deployments/${encodeURIComponent(id)}`, {
    method: 'PUT',
    body: JSON.stringify(body),
  }),
  deleteDeployment: (id: string) =>
    request<{ deleted: boolean; id: string }>(`/deployments/${encodeURIComponent(id)}`, { method: 'DELETE' }),
  runDeployment: (id: string) =>
    request<any>(`/deployments/${encodeURIComponent(id)}/run`, { method: 'POST' }),

  // ── Backfills (2026-05-27) ──
  // Chunked re-execution of a pipeline over a historical date range.
  // Each request expands into N windowed executions on the backend; the
  // API returns immediately with a backfill_id the UI uses to poll
  // progress. See backend/fpulse/backfills/ for the orchestrator.
  createBackfill: (body: {
    pipeline_id: string;
    start_date: string;
    end_date: string;
    window_size: 'daily' | 'weekly' | 'monthly' | 'hourly' | 'custom';
    window_size_hours?: number;
    cursor_param_names?: string[];
    concurrency?: number;
    on_failure?: 'stop' | 'continue' | 'retry_once';
    parameter_values?: Record<string, unknown>;
    acknowledge_side_effects?: boolean;
  }) =>
    request<{
      backfill_id: string;
      pipeline_id: string;
      total_windows: number;
      status: string;
      window_start: string;
      window_end: string;
    }>(`/executions/backfill`, {
      method: 'POST',
      body: JSON.stringify(body),
    }),
  listBackfills: (params?: { pipeline_id?: string }) => {
    const qs = new URLSearchParams();
    if (params?.pipeline_id) qs.set('pipeline_id', params.pipeline_id);
    const q = qs.toString();
    return request<any[]>(`/executions/backfill${q ? '?' + q : ''}`);
  },
  getBackfill: (backfillId: string) =>
    request<{
      backfill: any;
      windows: any[];
    }>(`/executions/backfill/${encodeURIComponent(backfillId)}`),
  cancelBackfill: (backfillId: string) =>
    request<{ cancelled: boolean; backfill_id: string }>(
      `/executions/backfill/${encodeURIComponent(backfillId)}/cancel`,
      { method: 'POST' },
    ),

  // B3 (2026-06-08) — resume a failed / cancelled / partial backfill
  // from the first window that didn't complete successfully. Body is
  // optional; omit from_window to let the backend auto-detect the
  // first non-successful window. See backend/fpulse/api/backfills.py
  // POST /executions/backfill/{id}/resume.
  resumeBackfill: (backfillId: string, fromWindow?: number) =>
    request<{ resumed: boolean; backfill_id: string; from_window: number; skipped_windows: number }>(
      `/executions/backfill/${encodeURIComponent(backfillId)}/resume`,
      {
        method: 'POST',
        body: JSON.stringify(fromWindow === undefined ? {} : { from_window: fromWindow }),
      },
    ),

  // ── Lineage (1.2) ──
  // Runtime lineage = what actually ran on a specific run_id (columns
  // in/out, rows in/out, timing). Distinct from the design-time graph.
  getRuntimeLineage: (runId: string) =>
    request<{
      run_id: string;
      step_runs: Array<{
        id: string; workflow_id: string; run_id: string;
        step_id: string; step_label: string; step_type: string;
        columns_in: string[]; columns_out: string[];
        rows_in: number; rows_out: number;
        started_at: number; completed_at: number; error: string;
      }>;
    }>(`/lineage/runs/${encodeURIComponent(runId)}`),

  listRunsWithLineage: (workflowId: string, limit = 50) =>
    request<{ workflow_id: string; limit: number; runs: string[] }>(
      `/lineage/workflow/${encodeURIComponent(workflowId)}/runs?limit=${limit}`,
    ),

  // Output-to-consumer self-attestation (L3).
  listOutputConsumers: (outputId: string) =>
    request<{
      output_id: string; count: number;
      consumers: Array<{
        id: string; output_id: string; consumer_id: string;
        consumer_type: string; last_read_at: number | null;
        attested_at: number; attested_by: string; notes: string;
      }>;
    }>(`/lineage/consumers?output_id=${encodeURIComponent(outputId)}`),

  registerOutputConsumer: (body: {
    output_id: string; consumer_id: string; consumer_type: string;
    last_read_at?: number; attested_by?: string; notes?: string;
  }) =>
    request<{ recorded: boolean; id: string }>(`/lineage/consumers`, {
      method: 'POST',
      body: JSON.stringify(body),
    }),

  deregisterOutputConsumer: (body: {
    output_id: string; consumer_id: string; consumer_type: string;
  }) =>
    request<{ removed: boolean }>(`/lineage/consumers`, {
      method: 'DELETE',
      body: JSON.stringify(body),
    }),

  // (E4 resume-from-checkpoint already ships via `resumeWorkflow` above
  //  and the "Resume from failed" button in ExecutionsPage.)

  // ── IR Replay (D1 round 1, 2026-05-26) ──
  // Replay a historical execution by re-running its stored IR snapshot.
  // Backend: POST /api/monitor/executions/{id}/replay. The diff endpoint
  // compares two executions step-by-step without re-running.
  replayExecution: (executionId: string) =>
    request<{
      original_id: string;
      replay_id: string;
      ir_sha: string | null;
      status: string;
      diff: {
        status_changed: boolean;
        ir_sha_match: boolean | null;
        duration_delta_ms: number;
        rows_delta: number;
        steps: Array<{
          step_id: string;
          step_name: string;
          a_status: string | null;
          b_status: string | null;
          a_rows: number;
          b_rows: number;
          a_duration_ms: number | null;
          b_duration_ms: number | null;
          changed: boolean;
        }>;
        added_steps: string[];
        removed_steps: string[];
      };
    }>(`/monitor/executions/${encodeURIComponent(executionId)}/replay`, { method: 'POST' }),
  diffExecutions: (aId: string, bId: string) =>
    request<{
      a_id: string;
      b_id: string;
      a_workflow_id: string;
      b_workflow_id: string;
      a_started_at: string | null;
      b_started_at: string | null;
      diff: {
        status_changed: boolean;
        ir_sha_match: boolean | null;
        duration_delta_ms: number;
        rows_delta: number;
        steps: any[];
        added_steps: string[];
        removed_steps: string[];
      };
    }>(`/monitor/executions/${encodeURIComponent(aId)}/diff/${encodeURIComponent(bId)}`),
};

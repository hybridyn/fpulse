/**
 * Canonical auth-field vocabulary (P2-E, 2026-05-18).
 *
 * Today the same auth concept is named 3+ different ways across nodes:
 *   - `bearer_token` (api_source, api_sink)
 *   - `auth_token`   (http_request)
 *   - `token`        (rare, some sinks)
 * …and `password` / `auth_pass` / `smtp_pass` / `sasl_password` for
 * passwords. AI generation breaks, docs drift, dynamic forms can't
 * be schema-driven, refactors are painful.
 *
 * This module establishes ONE canonical name per auth concept:
 *
 *   auth.type            'none' | 'bearer' | 'basic' | 'api_key'
 *   auth.bearer_token    string
 *   auth.username        string
 *   auth.password        string
 *   auth.api_key_header  string  (defaults to 'X-API-Key')
 *   auth.api_key_value   string
 *
 * The actual node params are flat (Zustand store, JSON-serialized to
 * the backend), so `auth.X` is encoded as nested object `auth: {X: ...}`
 * to namespace it cleanly. Migration shims below convert between legacy
 * flat names and the canonical nested shape.
 *
 * **Rollout plan**: new code (notably the upcoming P2-A `<AuthSection />`
 * primitive) reads + writes the canonical shape via these helpers. The
 * `normalizeOnLoad` shim runs once when a workflow loads from the backend
 * and rewrites legacy `bearer_token` → `auth.bearer_token`. The
 * `denormalizeForBackend` shim runs once when a workflow is persisted
 * and rewrites canonical → legacy, so the backend executor (which still
 * reads the old names) keeps working unchanged.
 *
 * The shim is intentionally idempotent — calling it on already-canonical
 * params is a no-op. Calling it on already-legacy params is also a no-op.
 *
 * This file is foundation-only — no current call sites consume it. The
 * value lands when the AuthSection primitive ships.
 */

export type AuthType = 'none' | 'bearer' | 'basic' | 'api_key';

export interface CanonicalAuth {
  type?: AuthType;
  bearer_token?: string;
  username?: string;
  password?: string;
  api_key_header?: string;
  api_key_value?: string;
}

/**
 * Every legacy auth-field name we've seen in the codebase, mapped to
 * its canonical sub-key. Order matters when multiple legacy names map
 * to the same canonical (later entries override earlier — pick the
 * most-recently-used or most-specific). Empty string `auth_type` is
 * never a legacy name; it lives at the top level intentionally.
 */
const LEGACY_TO_CANONICAL: Record<string, keyof CanonicalAuth> = {
  // Token / Bearer
  bearer_token: 'bearer_token',
  auth_token: 'bearer_token',
  token: 'bearer_token',
  // Basic auth
  username: 'username',
  auth_user: 'username',
  smtp_user: 'username',
  password: 'password',
  auth_pass: 'password',
  smtp_pass: 'password',
  sasl_password: 'password',
  // API key
  api_key_header: 'api_key_header',
  api_key_value: 'api_key_value',
  api_key: 'api_key_value',
};

/** Reverse map for `denormalizeForBackend` — picks the most common
 *  legacy name for each canonical key. The backend executor reads
 *  these names (e.g. the http_request node looks for `auth_token` /
 *  `auth_pass`, not the canonical names). Keep this list in sync
 *  with the backend node code when adding new canonical fields. */
const CANONICAL_TO_LEGACY: Record<keyof CanonicalAuth, string> = {
  type: 'auth_type',
  bearer_token: 'bearer_token',
  username: 'username',
  password: 'password',
  api_key_header: 'api_key_header',
  api_key_value: 'api_key_value',
};

const CANONICAL_KEYS: Array<keyof CanonicalAuth> = [
  'type', 'bearer_token', 'username', 'password',
  'api_key_header', 'api_key_value',
];

/**
 * Idempotent migration: rewrites flat legacy fields into a nested
 * `auth` object. If `params.auth` already exists, treats it as the
 * source of truth and only fills in keys that are missing (so a
 * partial migration doesn't lose data).
 *
 *   { bearer_token: 'xyz', timeout: 30 }
 *     → { auth: { type: 'bearer', bearer_token: 'xyz' }, timeout: 30 }
 *     // Note: `type` was inferred from the presence of bearer_token.
 *
 *   { auth_type: 'basic', auth_user: 'u', auth_pass: 'p' }
 *     → { auth: { type: 'basic', username: 'u', password: 'p' } }
 */
export function normalizeOnLoad(params: Record<string, any>): Record<string, any> {
  if (!params || typeof params !== 'object') return params;
  const existing: CanonicalAuth = (params.auth && typeof params.auth === 'object') ? { ...params.auth } : {};
  let touched = false;
  // Pull legacy keys into the canonical sub-object
  for (const [legacy, canonical] of Object.entries(LEGACY_TO_CANONICAL)) {
    if (params[legacy] != null && existing[canonical] == null) {
      (existing as any)[canonical] = params[legacy];
      touched = true;
    }
  }
  // Legacy `auth_type` lives flat too
  if (params.auth_type != null && existing.type == null) {
    existing.type = params.auth_type as AuthType;
    touched = true;
  }
  // Infer auth.type if it's still missing but we have a credential
  if (!existing.type) {
    if (existing.bearer_token) existing.type = 'bearer';
    else if (existing.username || existing.password) existing.type = 'basic';
    else if (existing.api_key_value) existing.type = 'api_key';
  }
  if (!touched && !params.auth) return params;
  // Cast to a mutable Record so the `delete` operations on legacy keys
  // (which TS doesn't see on the narrower union) typecheck. The runtime
  // shape is whatever the original `params` carried plus an `auth` field.
  const out: Record<string, any> = { ...params, auth: existing };
  // Strip the legacy flat keys we just absorbed so the IR stays clean.
  for (const legacy of Object.keys(LEGACY_TO_CANONICAL)) delete out[legacy];
  delete out.auth_type;
  return out;
}

/**
 * Idempotent reverse migration: flattens the `auth` object back into
 * legacy field names the backend executor expects. Called once at
 * save time so the wire format remains backwards-compatible.
 *
 *   { auth: { type: 'bearer', bearer_token: 'xyz' }, timeout: 30 }
 *     → { auth_type: 'bearer', bearer_token: 'xyz', timeout: 30 }
 */
export function denormalizeForBackend(params: Record<string, any>): Record<string, any> {
  if (!params || typeof params !== 'object') return params;
  const auth: CanonicalAuth | undefined = params.auth;
  if (!auth || typeof auth !== 'object') return params;
  const out: Record<string, any> = { ...params };
  delete out.auth;
  for (const k of CANONICAL_KEYS) {
    const v = (auth as any)[k];
    if (v == null) continue;
    const legacy = CANONICAL_TO_LEGACY[k];
    out[legacy] = v;
  }
  return out;
}

/** Returns true if `params` already uses the canonical shape. */
export function isCanonical(params: Record<string, any>): boolean {
  return !!(params && typeof params === 'object' && params.auth && typeof params.auth === 'object');
}

/** Pretty-print for tooltips / dev tools / debugging — never sends
 *  the actual secret values; just lists which fields are populated. */
export function summarizeAuth(auth: CanonicalAuth | undefined): string {
  if (!auth || !auth.type || auth.type === 'none') return 'No auth';
  const parts: string[] = [auth.type];
  if (auth.bearer_token) parts.push('bearer token set');
  if (auth.username) parts.push('username set');
  if (auth.password) parts.push('password set');
  if (auth.api_key_value) parts.push('api key set');
  return parts.join(' · ');
}

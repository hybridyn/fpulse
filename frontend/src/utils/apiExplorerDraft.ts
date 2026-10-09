export interface ApiConnectionDraft { name: string; config: Record<string, string> }
let pending: ApiConnectionDraft | null = null;

// One-shot, in-memory handoff. Never persist tokens, headers, body or query values.
export function stageApiConnection(url: string, authType: string, keyName: string) {
  const parsed = new URL(url);
  if (!['http:', 'https:'].includes(parsed.protocol) || parsed.username || parsed.password) throw new Error('Invalid API URL');
  pending = { name: `${parsed.hostname} API`, config: {
    base_url: parsed.origin + parsed.pathname, auth_type: authType, ssl_verify: 'true',
    ...(authType === 'api_key' ? { api_key_header: keyName } : {}),
    ...(authType === 'api_key_query' ? { api_key_param: keyName } : {}),
  } };
}

export function takeApiConnection(): ApiConnectionDraft | null {
  const draft = pending;
  pending = null;
  return draft;
}

/**
 * Ollama frontend client — status probe + streaming pull.
 *
 * Backend contract: backend/fpulse/api/ollama.py
 *
 * V8/V9 follow-up round 2 (2026-05-26): getOllamaStatus migrated to
 * apiRequest. deleteOllamaModel keeps inline fetch (DELETE endpoint
 * may return empty body — apiRequest assumes JSON). pullOllamaModel
 * keeps inline fetch — it's NDJSON-streamed, apiRequest can't surface
 * a stream body.
 *
 * Pull is NDJSON-streamed: each line is a JSON object with one of these shapes:
 *   {status: "pulling manifest"}
 *   {status: "downloading", digest: "sha256:...", total: N, completed: M}
 *   {status: "verifying sha256 digest"}
 *   {status: "writing manifest"}
 *   {status: "removing any unused layers"}
 *   {status: "success"}
 *   {status: "error", error: "...", message: "..."}    (synthesized by backend on httpx error)
 */
import { apiRequest } from './client';

export interface OllamaModel {
  name: string;
  size: number;
  modified_at: string;
}

export interface OllamaStatus {
  running: boolean;
  models: OllamaModel[];
  url: string;
  error?: string;
}

export interface OllamaPullProgress {
  status: string;
  digest?: string;
  total?: number;
  completed?: number;
  error?: string;
  message?: string;
}

function _headers(): Record<string, string> {
  const token = localStorage.getItem('fpulse_token') || '';
  const workspaceId = localStorage.getItem('fpulse_workspace_id') || 'default';
  const headers: Record<string, string> = {
    'Content-Type': 'application/json',
    'X-Workspace-Id': workspaceId,
  };
  if (token) headers['Authorization'] = `Bearer ${token}`;
  return headers;
}

export async function getOllamaStatus(): Promise<OllamaStatus> {
  // Migrated to apiRequest so the global 401 handler + backend-
  // reachable signal fire. Status probe is best-effort — on any
  // failure (Ollama down, network out, backend gone) return a
  // populated fallback so the UI can show "not running" instead of
  // showing an exception.
  try {
    return await apiRequest<OllamaStatus>('/ai/ollama/status');
  } catch (err: any) {
    return { running: false, models: [], url: '', error: err?.message || 'status_unknown' };
  }
}

/**
 * Stream `ollama pull <model>` progress. Calls `onProgress` for each NDJSON
 * line. Resolves when Ollama emits {status: "success"}, rejects on error.
 *
 * Cancel by passing an AbortSignal; the underlying fetch is aborted and
 * the backend disconnects from Ollama (Ollama itself keeps pulling — that's
 * a feature, not a bug; another client can resume).
 */
export async function deleteOllamaModel(name: string): Promise<void> {
  // Encode the model name in the URL but allow ':' through (it's the
  // tag separator, e.g. 'llama3:8b'). FastAPI's :path converter accepts
  // raw colons, so we only encode characters URL-unsafe outside that.
  const safe = encodeURIComponent(name).replace(/%3A/gi, ':');
  const res = await fetch(`/api/ai/ollama/models/${safe}`, {
    method: 'DELETE',
    headers: _headers(),
  });
  if (!res.ok) {
    const err = await res.json().catch(() => ({ message: `delete_${res.status}` }));
    throw new Error(err.message || `Delete failed: ${res.status}`);
  }
}

export async function pullOllamaModel(
  model: string,
  onProgress: (p: OllamaPullProgress) => void,
  signal?: AbortSignal,
): Promise<void> {
  const res = await fetch('/api/ai/ollama/pull', {
    method: 'POST',
    headers: _headers(),
    body: JSON.stringify({ model }),
    signal,
  });

  if (!res.ok) {
    const err = await res.json().catch(() => ({ message: `pull_${res.status}` }));
    throw new Error(err.message || `Ollama pull failed: ${res.status}`);
  }

  if (!res.body) {
    throw new Error('Pull endpoint returned no stream body');
  }

  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buffer = '';
  let lastError: string | null = null;

  while (true) {
    const { done, value } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    let nlIndex: number;
    while ((nlIndex = buffer.indexOf('\n')) !== -1) {
      const line = buffer.slice(0, nlIndex).trim();
      buffer = buffer.slice(nlIndex + 1);
      if (!line) continue;
      try {
        const event = JSON.parse(line) as OllamaPullProgress;
        if (event.status === 'error') {
          lastError = event.message || event.error || 'Ollama pull error';
        }
        onProgress(event);
      } catch {
        // Tolerate occasional partial lines.
      }
    }
  }

  if (lastError) throw new Error(lastError);
}

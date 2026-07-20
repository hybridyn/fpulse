/**
 * OllamaRecommendationBanner — first-launch nudge that surfaces when:
 *
 *   (a) the active AI provider is Ollama AND no models are installed
 *   (b) the active Ollama model is BELOW the reliable tool-use floor
 *       (<=3B params advertise tool support but can't drive a real
 *       agent loop in practice — they return greetings or empty text
 *       instead of calling tools)
 *   (c) the active Ollama model is too heavy for CPU (>=14B params)
 *       where a tool-using turn takes minutes
 *
 * Recommends qwen2.5:7b (the 2026-05-19 floor revision pick) — full
 * tool support, ~6 GB RAM at Q4_K_M, 30–60 s per turn on CPU at the
 * floor. llama3.1:8b and phi-4 are equally supported alternatives.
 * One-click pull uses the same endpoint AIProviderForm uses.
 *
 * Dismissal is sticky (localStorage key `fpulse_qwen_banner_dismissed`).
 * Does not nag once dismissed; the AI Provider settings page still shows
 * the recommended-models picker for users who want to switch later.
 *
 * 2026-05-21: added the 'below-floor' reason after the user reported a
 * qwen2.5:1.5b install silently returning greetings instead of calling
 * tools. The banner is the first surface that catches them.
 */
import { useEffect, useState } from 'react';
import { getAgentStatus, refreshAgentProvider } from '../api/agent';
import { getOllamaStatus, pullOllamaModel } from '../api/ollama';
import {
  isOllamaTooLargeForCPU,
  isOllamaBelowToolUseFloor,
  OLLAMA_CPU_RECOMMENDATION,
} from '../util/aiModels';

const DISMISS_KEY = 'fpulse_qwen_banner_dismissed';

type BannerReason = 'no-models' | 'below-floor' | 'oversized-model' | null;

export default function OllamaRecommendationBanner() {
  const [reason, setReason] = useState<BannerReason>(null);
  const [activeModel, setActiveModel] = useState<string>('');
  const [pulling, setPulling] = useState(false);
  const [progress, setProgress] = useState<{ completed: number; total: number; status: string } | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [done, setDone] = useState(false);

  useEffect(() => {
    // Honor sticky dismissal
    try {
      if (localStorage.getItem(DISMISS_KEY) === '1') return;
    } catch { /* ignore */ }

    let cancelled = false;
    (async () => {
      try {
        const status = await getAgentStatus();
        if (cancelled) return;
        if (!status.available || status.provider !== 'ollama') {
          // Either no AI configured, or using a cloud provider — banner doesn't apply.
          return;
        }
        // Check installed models. If Ollama isn't running we don't show the
        // banner — AIProviderForm has its own dedicated "Ollama not running"
        // surface that handles that case better.
        const ollama = await getOllamaStatus();
        if (cancelled) return;
        if (!ollama.running) return;

        if (ollama.models.length === 0) {
          setReason('no-models');
          return;
        }
        // Below-floor check FIRST — these models are the silent-failure
        // case the user reported (1.5b returns greetings instead of
        // calling tools). Surface this even if technically tool-capable.
        if (isOllamaBelowToolUseFloor(status.model)) {
          setActiveModel(status.model);
          setReason('below-floor');
          return;
        }
        if (isOllamaTooLargeForCPU(status.model)) {
          setActiveModel(status.model);
          setReason('oversized-model');
          return;
        }
      } catch {
        // Silent — banner is a nudge, not a hard requirement.
      }
    })();

    return () => { cancelled = true; };
  }, []);

  const dismiss = () => {
    try { localStorage.setItem(DISMISS_KEY, '1'); } catch { /* ignore */ }
    setReason(null);
  };

  const handlePull = async () => {
    setPulling(true);
    setError(null);
    setProgress(null);
    try {
      await pullOllamaModel(OLLAMA_CPU_RECOMMENDATION, (p) => {
        if (p.status === 'success') return;
        setProgress({
          completed: p.completed ?? 0,
          total: p.total ?? 0,
          status: p.status || 'downloading',
        });
      });
      // Tell the backend to invalidate its provider autoprobe cache so the
      // newly-installed model becomes the active one without a 5-min wait.
      try { await refreshAgentProvider(); } catch { /* best-effort */ }
      setDone(true);
      // Mark dismissed so we don't re-prompt on next mount.
      try { localStorage.setItem(DISMISS_KEY, '1'); } catch { /* ignore */ }
      // Hide after 3 seconds so the success acknowledgement is visible.
      setTimeout(() => setReason(null), 3000);
    } catch (e: any) {
      setError(e?.message || 'Pull failed. Try from Insights → AI Provider.');
    } finally {
      setPulling(false);
      setProgress(null);
    }
  };

  if (!reason) return null;

  const pct = progress && progress.total > 0
    ? Math.min(100, Math.round((progress.completed / progress.total) * 100))
    : null;

  const message = done
    ? `${OLLAMA_CPU_RECOMMENDATION} installed — switching active model.`
    : reason === 'no-models'
      ? `Ollama is your AI provider but no models are installed. Pull ${OLLAMA_CPU_RECOMMENDATION} (~6 GB) — reliable tool-use floor for the F-Pulse Copilot.`
      : reason === 'below-floor'
        ? `${activeModel} is below the tool-use floor — small models advertise tool support but return greetings instead of calling tools. Pull ${OLLAMA_CPU_RECOMMENDATION} (~6 GB) for reliable agent answers.`
        : `${activeModel} is too heavy for CPU agents (minutes per turn). ${OLLAMA_CPU_RECOMMENDATION} is the recommended floor with reliable tool-use on CPU.`;

  return (
    <div className="shrink-0 px-4 py-2 bg-amber-50 border-b border-amber-200 text-amber-900 text-sm flex items-center gap-3">
      <svg className="w-4 h-4 shrink-0" fill="none" stroke="currentColor" viewBox="0 0 24 24" aria-hidden="true">
        <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M13 16h-1v-4h-1m1-4h.01M21 12a9 9 0 11-18 0 9 9 0 0118 0z" />
      </svg>
      <span className="flex-1 leading-snug">
        {message}
        {pulling && pct !== null && (
          <span className="ml-2 text-amber-700 font-mono">{pct}%</span>
        )}
        {pulling && progress && pct === null && (
          <span className="ml-2 text-amber-700">{progress.status}…</span>
        )}
        {error && <span className="ml-2 text-red-700">{error}</span>}
      </span>
      {!done && (
        <button
          type="button"
          onClick={handlePull}
          disabled={pulling}
          className="px-3 py-1 rounded bg-amber-600 text-white text-xs font-medium hover:bg-amber-700 disabled:opacity-60 disabled:cursor-not-allowed"
        >
          {pulling ? 'Pulling…' : `Install ${OLLAMA_CPU_RECOMMENDATION}`}
        </button>
      )}
      <button
        type="button"
        onClick={dismiss}
        disabled={pulling}
        title="Dismiss"
        aria-label="Dismiss"
        className="text-amber-700 hover:text-amber-900 disabled:opacity-50"
      >
        ✕
      </button>
    </div>
  );
}

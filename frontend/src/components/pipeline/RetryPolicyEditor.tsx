/**
 * RetryPolicyEditor (E2 frontend, 2026-06-08)
 *
 * Controlled form for a workflow's retry policy (the `workflow.retry_policy`
 * IR field). Lets an operator decide whether failed steps retry, how many
 * times, the backoff, and — crucially — WHICH failure classes are worth
 * retrying (data_quality / user_input failures never benefit from retry).
 *
 * Pure controlled component: takes `value` + `onChange`. Mount it in the
 * pipeline Settings tab (one-line follow-up). Verified by transpile +
 * logic review; live render needs the dev server.
 */
import { useCallback } from 'react';

export interface RetryPolicy {
  enabled: boolean;
  max_attempts: number;
  initial_backoff_seconds: number;
  backoff_multiplier: number;
  backoff_max_seconds: number;
  retry_on: string[];
}

export const DEFAULT_RETRY_POLICY: RetryPolicy = {
  enabled: false,
  max_attempts: 3,
  initial_backoff_seconds: 2,
  backoff_multiplier: 2,
  backoff_max_seconds: 60,
  retry_on: ['transient', 'dependency'],
};

const FAILURE_CLASSES: Array<{ key: string; label: string; hint: string }> = [
  { key: 'transient', label: 'Transient', hint: 'timeout / 5xx / lock — retry usually fixes' },
  { key: 'dependency', label: 'Dependency', hint: 'external system down / auth — retry may fix' },
  { key: 'data_quality', label: 'Data quality', hint: "constraint / null — retry won't fix" },
  { key: 'user_input', label: 'User input', hint: "bad config — retry won't fix" },
  { key: 'fatal', label: 'Fatal', hint: 'OOM / disk — retry may worsen' },
  { key: 'unknown', label: 'Unknown', hint: 'unclassified' },
];

interface Props {
  value?: RetryPolicy | null;
  onChange: (next: RetryPolicy) => void;
  dark?: boolean;
}

export default function RetryPolicyEditor({ value, onChange, dark = false }: Props) {
  const policy: RetryPolicy = { ...DEFAULT_RETRY_POLICY, ...(value || {}) };

  const patch = useCallback(
    (p: Partial<RetryPolicy>) => onChange({ ...policy, ...p }),
    [policy, onChange],
  );

  const toggleClass = (key: string) => {
    const next = policy.retry_on.includes(key)
      ? policy.retry_on.filter((k) => k !== key)
      : [...policy.retry_on, key];
    patch({ retry_on: next });
  };

  const label = dark ? 'text-slate-300' : 'text-slate-600';
  const sub = dark ? 'text-slate-500' : 'text-slate-400';
  const inputCls = `px-2 py-1 text-xs rounded border w-20 ${
    dark ? 'bg-slate-800 border-slate-700 text-slate-200' : 'bg-white border-slate-200 text-slate-700'
  }`;

  return (
    <div className="space-y-3">
      <label className="flex items-center gap-2 cursor-pointer">
        <input
          type="checkbox"
          checked={policy.enabled}
          onChange={(e) => patch({ enabled: e.target.checked })}
        />
        <span className={`text-sm font-semibold ${label}`}>Enable workflow retry policy</span>
      </label>
      <p className={`text-xs ${sub}`}>
        When enabled, the executor consults this policy before retrying a
        failed step — so it won't waste attempts on failures a retry can't fix.
        Disabled = per-step retry settings drive everything (current behaviour).
      </p>

      {policy.enabled && (
        <div className="space-y-3 pl-1">
          <div className="flex flex-wrap gap-4">
            <label className="flex flex-col gap-1">
              <span className={`text-xs ${label}`}>Max attempts</span>
              <input
                type="number" min={1} className={inputCls}
                value={policy.max_attempts}
                onChange={(e) => patch({ max_attempts: Math.max(1, Number(e.target.value) || 1) })}
              />
            </label>
            <label className="flex flex-col gap-1">
              <span className={`text-xs ${label}`}>Initial backoff (s)</span>
              <input
                type="number" min={0} step={0.5} className={inputCls}
                value={policy.initial_backoff_seconds}
                onChange={(e) => patch({ initial_backoff_seconds: Math.max(0, Number(e.target.value) || 0) })}
              />
            </label>
            <label className="flex flex-col gap-1">
              <span className={`text-xs ${label}`}>Backoff ×</span>
              <input
                type="number" min={1} step={0.5} className={inputCls}
                value={policy.backoff_multiplier}
                onChange={(e) => patch({ backoff_multiplier: Math.max(1, Number(e.target.value) || 1) })}
              />
            </label>
            <label className="flex flex-col gap-1">
              <span className={`text-xs ${label}`}>Max backoff (s)</span>
              <input
                type="number" min={0} className={inputCls}
                value={policy.backoff_max_seconds}
                onChange={(e) => patch({ backoff_max_seconds: Math.max(0, Number(e.target.value) || 0) })}
              />
            </label>
          </div>

          <div>
            <span className={`text-xs font-semibold ${label}`}>Retry on failure classes</span>
            <div className="mt-1.5 grid grid-cols-1 sm:grid-cols-2 gap-1.5">
              {FAILURE_CLASSES.map((fc) => (
                <label
                  key={fc.key}
                  className="flex items-start gap-2 cursor-pointer"
                  title={fc.hint}
                >
                  <input
                    type="checkbox"
                    className="mt-0.5"
                    checked={policy.retry_on.includes(fc.key)}
                    onChange={() => toggleClass(fc.key)}
                  />
                  <span className="text-xs">
                    <span className={label}>{fc.label}</span>{' '}
                    <span className={sub}>— {fc.hint}</span>
                  </span>
                </label>
              ))}
            </div>
          </div>
        </div>
      )}
    </div>
  );
}

import { useEffect, useState, useCallback } from 'react';
import { api } from '../api/client';
import { toast } from './Toast';
import { navigateTo } from '../router';

/**
 * Pre-publish test card. Wraps the publish lifecycle:
 *   1. Run /test against the saved pipeline.
 *   2. Show per-step pass/fail with row counts and durations.
 *   3. On success → user clicks "Publish Now" → /publish.
 *   4. On failure → user clicks "Edit in Editor" (load + jump to canvas)
 *      or "Retry test" (re-run /test against current state).
 *
 * The backend gates /publish on a passing /test result, so this card
 * is the user-facing way to satisfy that gate without a separate
 * round-trip. Triggered from every Publish entry-point on the
 * Workflows page (card view, table row, detail drawer).
 */

interface StepLog {
  step_id: string;
  step_label?: string;
  status: string;
  rows_processed?: number;
  duration_ms?: number;
  error_message?: string | null;
}

interface TestResultsPayload {
  status: string;
  test_results?: {
    status?: string;
    duration_ms?: number;
    steps_total?: number;
    steps_passed?: number;
    steps_failed?: number;
    error?: string | null;
  };
  execution?: {
    step_logs?: StepLog[];
    error_message?: string | null;
  };
}

type Phase = 'testing' | 'success' | 'failed' | 'documenting' | 'publishing' | 'published';

interface Props {
  open: boolean;
  workflowId: string | null;
  workflowName: string;
  onClose: () => void;
  onPublished?: () => void;
}

export default function PublishTestModal({ open, workflowId, workflowName, onClose, onPublished }: Props) {
  const [phase, setPhase] = useState<Phase>('testing');
  const [result, setResult] = useState<TestResultsPayload | null>(null);
  const [errorMsg, setErrorMsg] = useState<string>('');

  // Documentation gate — a pipeline must state its business purpose before
  // it goes live (unless an admin relaxed the policy org-wide). We capture
  // it here, right before publish, pre-filled with anything already saved.
  const [requirePurpose, setRequirePurpose] = useState(true);
  const [bizPurpose, setBizPurpose] = useState('');
  const [readme, setReadme] = useState('');
  const [tagsText, setTagsText] = useState('');

  const runTest = useCallback(async () => {
    if (!workflowId) return;
    setPhase('testing');
    setResult(null);
    setErrorMsg('');
    try {
      const res = await api.testWorkflow(workflowId) as TestResultsPayload;
      setResult(res);
      setPhase(res?.status === 'success' ? 'success' : 'failed');
    } catch (e: any) {
      setPhase('failed');
      setErrorMsg(e?.message || 'Test request failed');
    }
  }, [workflowId]);

  // Run test automatically when the modal opens for a workflow.
  useEffect(() => {
    if (open && workflowId) runTest();
    if (!open) {
      // Reset on close so re-opening starts fresh.
      setPhase('testing');
      setResult(null);
      setErrorMsg('');
    }
  }, [open, workflowId, runTest]);

  // Load the pipeline's current documentation + the publish policy so the
  // doc step is pre-filled and only enforced when the policy requires it.
  useEffect(() => {
    if (!open || !workflowId) return;
    api.getWorkflow(workflowId).then((r: any) => {
      const wf = r?.workflow || r || {};
      setBizPurpose(wf.business_purpose || '');
      setReadme(wf.readme || '');
      setTagsText(Array.isArray(wf.tags) ? wf.tags.join(', ') : '');
    }).catch(() => {});
    api.getPublishPolicy()
      .then((p: any) => setRequirePurpose(!!p?.require_business_purpose))
      .catch(() => setRequirePurpose(true));  // fail safe: keep it required
  }, [open, workflowId]);

  // The actual publish call. Assumes the doc gate is already satisfied.
  const proceedToPublish = async () => {
    if (!workflowId) return;
    setPhase('publishing');
    try {
      await api.publishWorkflow(workflowId);
      setPhase('published');
      toast.success('Pipeline published', `"${workflowName}" is now live`);
      if (onPublished) onPublished();
      // Close after a beat so the user sees the success state.
      setTimeout(onClose, 800);
    } catch (e: any) {
      setPhase('failed');
      setErrorMsg(e?.message || 'Publish request failed');
    }
  };

  // "Publish Now" after a passing test. If the pipeline has no business
  // purpose yet and the policy requires one, capture documentation first;
  // otherwise publish straight away.
  const handlePublishClick = () => {
    if (requirePurpose && !bizPurpose.trim()) {
      setErrorMsg('');
      setPhase('documenting');
    } else {
      proceedToPublish();
    }
  };

  // Save the documentation fields, then publish. Used from the doc step.
  const handleSaveDocsAndPublish = async () => {
    if (!workflowId) return;
    if (requirePurpose && !bizPurpose.trim()) {
      setErrorMsg('A business purpose is required before publishing.');
      return;
    }
    setPhase('publishing');
    setErrorMsg('');
    try {
      await api.updateWorkflowDocs(workflowId, {
        business_purpose: bizPurpose.trim(),
        readme,
        tags: tagsText.split(',').map((t) => t.trim()).filter(Boolean),
      });
      await api.publishWorkflow(workflowId);
      setPhase('published');
      toast.success('Pipeline published', `"${workflowName}" is now live`);
      if (onPublished) onPublished();
      setTimeout(onClose, 800);
    } catch (e: any) {
      setPhase('documenting');
      setErrorMsg(e?.message || 'Could not save documentation / publish');
    }
  };

  const handleEditInEditor = () => {
    if (!workflowId) return;
    // PipelinesPage is the same SPA as the Editor; hash routing.
    // Pre-load the workflow into the store so the editor renders it.
    api.getWorkflow(workflowId).then((wf) => {
      try {
        // Lazy import to avoid a circular dep through the store.
        import('../stores/workflowStore').then(({ useWorkflowStore }) => {
          useWorkflowStore.getState().loadWorkflow(wf);
          navigateTo('editor');
          onClose();
        });
      } catch {
        navigateTo('editor');
        onClose();
      }
    }).catch(() => {
      navigateTo('editor');
      onClose();
    });
  };

  if (!open) return null;

  const stepLogs = result?.execution?.step_logs || [];
  const summary = result?.test_results;

  return (
    <>
      <div className="fixed inset-0 bg-black/30 z-[60]" onClick={onClose} />
      <div className="fixed inset-0 z-[65] flex items-center justify-center pointer-events-none p-4">
        <div className="pointer-events-auto w-[560px] max-w-[95vw] max-h-[85vh] bg-white rounded-2xl shadow-2xl border border-slate-200/60 flex flex-col overflow-hidden">

          {/* Header */}
          <div className="px-5 py-4 border-b border-slate-200/60 flex items-center justify-between shrink-0">
            <div className="min-w-0">
              <h2 className="text-base font-bold text-slate-800 truncate">Pre-publish test</h2>
              <p className="text-xs text-slate-500 mt-0.5 truncate">{workflowName}</p>
            </div>
            <button onClick={onClose} className="w-8 h-8 rounded-lg flex items-center justify-center text-slate-400 hover:text-slate-600 hover:bg-slate-100">
              <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5">
                <line x1="18" y1="6" x2="6" y2="18" /><line x1="6" y1="6" x2="18" y2="18" />
              </svg>
            </button>
          </div>

          {/* Body */}
          <div className="flex-1 overflow-y-auto p-5 space-y-4">
            {/* Overall status banner */}
            {phase === 'testing' && (
              <div className="flex items-center gap-3 px-4 py-3 rounded-xl border border-blue-200 bg-blue-50">
                <span className="w-4 h-4 border-2 border-blue-300 border-t-blue-600 rounded-full animate-spin shrink-0" />
                <div>
                  <div className="text-sm font-semibold text-blue-700">Running end-to-end test…</div>
                  <div className="text-xs text-blue-600">Executing every step against real connections.</div>
                </div>
              </div>
            )}
            {phase === 'success' && (
              <div className="flex items-center gap-3 px-4 py-3 rounded-xl border border-emerald-200 bg-emerald-50">
                <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" className="text-emerald-600 shrink-0">
                  <polyline points="20 6 9 17 4 12" />
                </svg>
                <div>
                  <div className="text-sm font-semibold text-emerald-700">
                    All {summary?.steps_passed ?? stepLogs.length} step{(summary?.steps_passed ?? stepLogs.length) === 1 ? '' : 's'} passed
                  </div>
                  <div className="text-xs text-emerald-600">
                    Total {summary?.duration_ms ? Math.round(summary.duration_ms) : '—'} ms · ready to publish.
                  </div>
                </div>
              </div>
            )}
            {phase === 'failed' && (
              <div className="flex items-center gap-3 px-4 py-3 rounded-xl border border-red-200 bg-red-50">
                <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" className="text-red-600 shrink-0">
                  <circle cx="12" cy="12" r="10" /><line x1="15" y1="9" x2="9" y2="15" /><line x1="9" y1="9" x2="15" y2="15" />
                </svg>
                <div className="min-w-0">
                  <div className="text-sm font-semibold text-red-700">
                    Test failed{summary?.steps_failed ? ` — ${summary.steps_failed} step${summary.steps_failed === 1 ? '' : 's'} errored` : ''}
                  </div>
                  <div className="text-xs text-red-600 break-words">
                    {errorMsg || summary?.error || result?.execution?.error_message || 'See per-step results below.'}
                  </div>
                </div>
              </div>
            )}
            {phase === 'publishing' && (
              <div className="flex items-center gap-3 px-4 py-3 rounded-xl border border-amber-200 bg-amber-50">
                <span className="w-4 h-4 border-2 border-amber-300 border-t-amber-600 rounded-full animate-spin shrink-0" />
                <div className="text-sm font-semibold text-amber-700">Publishing…</div>
              </div>
            )}
            {phase === 'published' && (
              <div className="flex items-center gap-3 px-4 py-3 rounded-xl border border-emerald-300 bg-emerald-50">
                <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" className="text-emerald-700 shrink-0">
                  <polyline points="20 6 9 17 4 12" />
                </svg>
                <div className="text-sm font-semibold text-emerald-800">Published.</div>
              </div>
            )}

            {/* Documentation step — captures the required business purpose
                (plus optional README / tags) before the pipeline goes live. */}
            {phase === 'documenting' && (
              <div className="space-y-3">
                <div className="flex items-start gap-3 px-4 py-3 rounded-xl border border-indigo-200 bg-indigo-50">
                  <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" className="text-indigo-600 shrink-0 mt-0.5">
                    <path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z" /><polyline points="14 2 14 8 20 8" />
                  </svg>
                  <div>
                    <div className="text-sm font-semibold text-indigo-700">Document this pipeline</div>
                    <div className="text-xs text-indigo-600">
                      A one-line business purpose is required before it goes live. README &amp; tags are optional.
                    </div>
                  </div>
                </div>
                <div>
                  <label className="block text-xs font-semibold text-slate-600 mb-1">
                    Business purpose {requirePurpose && <span className="text-red-500">*</span>}
                  </label>
                  <input
                    value={bizPurpose}
                    onChange={(e) => setBizPurpose(e.target.value)}
                    placeholder="e.g. Load daily orders into the warehouse for finance"
                    autoFocus
                    className="w-full px-3 py-2 text-sm rounded-lg border border-slate-200 focus:border-indigo-400 focus:ring-1 focus:ring-indigo-300 outline-none"
                  />
                </div>
                <div>
                  <label className="block text-xs font-semibold text-slate-600 mb-1">
                    README <span className="text-slate-400 font-normal">(optional, Markdown)</span>
                  </label>
                  <textarea
                    value={readme}
                    onChange={(e) => setReadme(e.target.value)}
                    rows={4}
                    placeholder={'## Runbook\nHow to operate this pipeline…'}
                    className="w-full px-3 py-2 text-sm rounded-lg border border-slate-200 focus:border-indigo-400 focus:ring-1 focus:ring-indigo-300 outline-none font-mono resize-y"
                  />
                </div>
                <div>
                  <label className="block text-xs font-semibold text-slate-600 mb-1">
                    Tags <span className="text-slate-400 font-normal">(optional, comma-separated)</span>
                  </label>
                  <input
                    value={tagsText}
                    onChange={(e) => setTagsText(e.target.value)}
                    placeholder="orders, nightly, finance"
                    className="w-full px-3 py-2 text-sm rounded-lg border border-slate-200 focus:border-indigo-400 focus:ring-1 focus:ring-indigo-300 outline-none"
                  />
                </div>
                {errorMsg && <div className="text-xs text-red-600">{errorMsg}</div>}
              </div>
            )}

            {/* Per-step results */}
            {phase !== 'documenting' && stepLogs.length > 0 && (
              <div className="rounded-xl border border-slate-200 overflow-hidden">
                <div className="px-3 py-2 bg-slate-50 border-b border-slate-200 text-xs font-bold text-slate-500 uppercase tracking-wider">
                  Step results
                </div>
                <ul className="divide-y divide-slate-100">
                  {stepLogs.map((s, i) => {
                    const ok = s.status === 'success';
                    const skipped = s.status === 'skipped';
                    return (
                      <li key={`${s.step_id}-${i}`} className="px-3 py-2 flex items-start gap-3 text-xs">
                        <span className={`w-4 h-4 rounded-full flex items-center justify-center shrink-0 mt-0.5 ${
                          ok ? 'bg-emerald-100 text-emerald-700' :
                          skipped ? 'bg-slate-100 text-slate-500' :
                          'bg-red-100 text-red-700'
                        }`}>
                          {ok ? (
                            <svg width="10" height="10" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="3"><polyline points="20 6 9 17 4 12" /></svg>
                          ) : skipped ? (
                            <span className="text-xs font-bold">—</span>
                          ) : (
                            <svg width="10" height="10" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="3"><line x1="18" y1="6" x2="6" y2="18" /><line x1="6" y1="6" x2="18" y2="18" /></svg>
                          )}
                        </span>
                        <div className="min-w-0 flex-1">
                          <div className="font-semibold text-slate-700 truncate">
                            {s.step_label || s.step_id}
                            <span className="ml-2 text-xs font-normal text-slate-400">{s.step_id}</span>
                          </div>
                          <div className="text-xs text-slate-500 mt-0.5">
                            {ok && (
                              <>{(s.rows_processed ?? 0).toLocaleString()} rows · {Math.round(s.duration_ms ?? 0)} ms</>
                            )}
                            {skipped && <>Skipped</>}
                            {!ok && !skipped && (
                              <span className="text-red-600 break-words">{s.error_message || 'Unknown error'}</span>
                            )}
                          </div>
                        </div>
                      </li>
                    );
                  })}
                </ul>
              </div>
            )}

            {/* Helper note when test couldn't even produce step logs */}
            {phase === 'failed' && stepLogs.length === 0 && (
              <div className="text-xs text-slate-500 px-1">
                The test never reached step execution. Open the editor to inspect the pipeline configuration.
              </div>
            )}
          </div>

          {/* Footer */}
          <div className="px-5 py-3 border-t border-slate-200/60 flex items-center justify-between shrink-0 bg-slate-50">
            <button
              onClick={onClose}
              disabled={phase === 'publishing'}
              className="px-4 py-2 text-xs text-slate-600 hover:text-slate-800 rounded-xl hover:bg-white font-medium disabled:opacity-50"
            >
              {phase === 'published' ? 'Close' : 'Cancel'}
            </button>
            <div className="flex items-center gap-2">
              {phase === 'failed' && (
                <>
                  <button
                    onClick={runTest}
                    className="px-4 py-2 text-xs font-semibold text-slate-700 bg-white border border-slate-200 rounded-xl hover:bg-slate-50"
                  >
                    Retry test
                  </button>
                  <button
                    onClick={handleEditInEditor}
                    className="px-5 py-2 text-xs text-white font-semibold rounded-xl shadow-sm flex items-center gap-1.5"
                    style={{ background: 'linear-gradient(135deg, #3B7DD8, #1E5AAF)' }}
                  >
                    <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5"><path d="M17 3a2.83 2.83 0 1 1 4 4L7.5 20.5 2 22l1.5-5.5L17 3z" /></svg>
                    Edit in Editor
                  </button>
                </>
              )}
              {phase === 'success' && (
                <button
                  onClick={handlePublishClick}
                  className="px-5 py-2 text-xs text-white font-semibold rounded-xl shadow-sm flex items-center gap-1.5"
                  style={{ background: 'linear-gradient(135deg, #f59e0b, #ea580c)' }}
                >
                  <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5"><polygon points="22 2 15 22 11 13 2 9 22 2" /></svg>
                  Publish Now
                </button>
              )}
              {phase === 'documenting' && (
                <button
                  onClick={handleSaveDocsAndPublish}
                  disabled={requirePurpose && !bizPurpose.trim()}
                  className="px-5 py-2 text-xs text-white font-semibold rounded-xl shadow-sm flex items-center gap-1.5 disabled:opacity-50 disabled:cursor-not-allowed"
                  style={{ background: 'linear-gradient(135deg, #f59e0b, #ea580c)' }}
                >
                  <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5"><polygon points="22 2 15 22 11 13 2 9 22 2" /></svg>
                  Save &amp; publish
                </button>
              )}
            </div>
          </div>
        </div>
      </div>
    </>
  );
}

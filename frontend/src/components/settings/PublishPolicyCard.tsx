import { useEffect, useState } from 'react';
import { api } from '../../api/client';

/**
 * Admin control for the publish documentation gate (Settings → Security).
 *
 * By default a pipeline must state a one-line business purpose before it can
 * be published — the publish flow captures it in a modal. This is the single,
 * instance-level escape hatch: an operator can turn the requirement off
 * org-wide. There is deliberately no per-pipeline opt-out, so when the gate is
 * on "every published pipeline states its purpose" stays a real guarantee.
 * Backed by GET/PUT /api/admin/publish-policy.
 */

interface PolicyState {
  require_business_purpose: boolean;
  setting_value?: boolean;
  env_override?: boolean;
}

export default function PublishPolicyCard({ dark = false }: { dark?: boolean }) {
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [forbidden, setForbidden] = useState(false);
  const [enabled, setEnabled] = useState(true);
  const [envOverride, setEnvOverride] = useState(false);
  const [msg, setMsg] = useState<{ kind: 'ok' | 'err'; text: string } | null>(null);

  useEffect(() => {
    let alive = true;
    (async () => {
      try {
        const s = await api.getPublishPolicy();
        if (!alive) return;
        setEnabled(!!s.require_business_purpose);
        setEnvOverride(!!s.env_override);
      } catch (e: any) {
        if (/403|forbidden|admin/i.test(e?.message || '')) setForbidden(true);
      } finally {
        if (alive) setLoading(false);
      }
    })();
    return () => { alive = false; };
  }, []);

  const toggle = async () => {
    if (envOverride || saving) return;
    const next = !enabled;
    setSaving(true);
    setMsg(null);
    try {
      const s = await api.setPublishPolicy(next);
      setEnabled(!!s.require_business_purpose);
      setMsg({
        kind: 'ok',
        text: next
          ? 'On — pipelines need a business purpose to publish.'
          : 'Off — a business purpose is no longer required to publish.',
      });
    } catch (e: any) {
      setMsg({ kind: 'err', text: e?.message || 'Save failed' });
    } finally {
      setSaving(false);
    }
  };

  if (forbidden || loading) return null;

  const cardCls = dark ? 'bg-[#111827] border-white/[0.08]' : 'bg-white border-slate-200';
  const helpCls = `text-xs ${dark ? 'text-slate-400' : 'text-slate-500'}`;

  return (
    <div className={`rounded-xl border shadow-sm p-5 mt-6 ${cardCls}`}>
      <div className="flex items-start justify-between gap-4">
        <div className="min-w-0">
          <h3 className={`text-base font-bold ${dark ? 'text-white' : 'text-slate-800'}`}>
            Require a business purpose to publish
          </h3>
          <p className={`${helpCls} mt-1 max-w-xl`}>
            On by default. When on, the publish flow asks for a one-line business
            purpose (README &amp; tags optional) before a pipeline goes live, so every
            published pipeline is self-documenting. Turning this off removes the
            requirement for the whole instance.
          </p>
        </div>
        <button
          type="button"
          role="switch"
          aria-checked={enabled}
          onClick={toggle}
          disabled={envOverride || saving}
          className={`relative inline-flex h-6 w-11 shrink-0 items-center rounded-full transition-colors disabled:opacity-60 ${
            enabled ? 'bg-amber-500' : (dark ? 'bg-slate-600' : 'bg-slate-300')
          }`}
        >
          <span className={`inline-block h-5 w-5 transform rounded-full bg-white shadow transition-transform ${enabled ? 'translate-x-5' : 'translate-x-0.5'}`} />
        </button>
      </div>

      {envOverride && (
        <div className={`mt-3 text-xs rounded-lg px-3 py-2 ${dark ? 'bg-amber-500/10 text-amber-200' : 'bg-amber-50 text-amber-700'}`}>
          Pinned by the <code>FPULSE_REQUIRE_PIPELINE_PURPOSE</code> environment
          variable — change it there to override this toggle.
        </div>
      )}

      {msg && (
        <div className={`mt-3 text-xs ${msg.kind === 'ok' ? (dark ? 'text-emerald-300' : 'text-emerald-600') : (dark ? 'text-red-300' : 'text-red-600')}`}>
          {msg.text}
        </div>
      )}
    </div>
  );
}

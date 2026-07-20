/**
 * ConsumersPanel (L3 frontend, 2026-06-08)
 *
 * "Who reads this output?" — lists registered downstream consumers of an
 * F-Pulse output and lets an operator register / remove one. Powers the
 * impact-analysis question: if I change this output's schema, what
 * breaks? Self-contained + prop-driven (pass an `outputId`); mount it on
 * a sink/output detail view (one-line follow-up). Verified by transpile
 * + logic review here; live render needs the dev server.
 */
import { useEffect, useState } from 'react';
import { api } from '../../api/client';
import { toast } from '../Toast';

interface Consumer {
  id: string;
  consumer_id: string;
  consumer_type: string;
  last_read_at: number | null;
  attested_at: number;
  attested_by: string;
  notes: string;
}

interface Props {
  outputId: string;
  dark?: boolean;
}

const CONSUMER_TYPES = [
  'snowflake_view',
  'tableau_dashboard',
  'python_notebook',
  'fpulse_pipeline',
  'other',
];

export default function ConsumersPanel({ outputId, dark = false }: Props) {
  const [consumers, setConsumers] = useState<Consumer[]>([]);
  const [loading, setLoading] = useState(false);
  const [showForm, setShowForm] = useState(false);
  const [newId, setNewId] = useState('');
  const [newType, setNewType] = useState(CONSUMER_TYPES[0]);
  const [newNotes, setNewNotes] = useState('');

  const load = () => {
    if (!outputId) return;
    setLoading(true);
    api
      .listOutputConsumers(outputId)
      .then((d) => setConsumers(d.consumers || []))
      .catch((e: any) => toast.error('Load failed', e?.message || 'Could not load consumers'))
      .finally(() => setLoading(false));
  };

  useEffect(load, [outputId]);

  const handleRegister = async () => {
    if (!newId.trim()) {
      toast.error('Consumer ID required', 'Enter an identifier for the consumer.');
      return;
    }
    try {
      await api.registerOutputConsumer({
        output_id: outputId,
        consumer_id: newId.trim(),
        consumer_type: newType,
        notes: newNotes.trim() || undefined,
      });
      toast.info('Consumer registered', `${newId.trim()} now tracked as a consumer.`);
      setNewId('');
      setNewNotes('');
      setShowForm(false);
      load();
    } catch (e: any) {
      toast.error('Register failed', e?.message || 'Backend rejected the registration.');
    }
  };

  const handleRemove = async (c: Consumer) => {
    try {
      await api.deregisterOutputConsumer({
        output_id: outputId,
        consumer_id: c.consumer_id,
        consumer_type: c.consumer_type,
      });
      load();
    } catch (e: any) {
      toast.error('Remove failed', e?.message || 'Backend rejected the removal.');
    }
  };

  const sub = dark ? 'text-slate-400' : 'text-slate-500';
  const inputCls = `px-2 py-1 text-xs rounded border ${
    dark ? 'bg-slate-800 border-slate-700 text-slate-200' : 'bg-white border-slate-200 text-slate-700'
  }`;

  return (
    <div className="space-y-2">
      <div className="flex items-center justify-between">
        <span className={`text-[10px] uppercase tracking-wider ${sub}`}>
          {consumers.length} registered consumer{consumers.length === 1 ? '' : 's'}
        </span>
        <button
          onClick={() => setShowForm((s) => !s)}
          className="px-2 py-1 text-[10px] font-semibold rounded text-violet-700 bg-violet-50 border border-violet-200 hover:bg-violet-100"
        >
          {showForm ? 'Cancel' : '+ Register consumer'}
        </button>
      </div>

      {showForm && (
        <div className={`p-2 rounded border space-y-2 ${dark ? 'border-slate-700 bg-slate-800/50' : 'border-slate-200 bg-slate-50'}`}>
          <input
            className={`${inputCls} w-full`}
            placeholder="consumer id (e.g. snowflake://prod/analytics/orders_view)"
            value={newId}
            onChange={(e) => setNewId(e.target.value)}
          />
          <div className="flex gap-2">
            <select className={inputCls} value={newType} onChange={(e) => setNewType(e.target.value)}>
              {CONSUMER_TYPES.map((t) => (
                <option key={t} value={t}>{t}</option>
              ))}
            </select>
            <input
              className={`${inputCls} flex-1`}
              placeholder="notes (optional)"
              value={newNotes}
              onChange={(e) => setNewNotes(e.target.value)}
            />
          </div>
          <button
            onClick={handleRegister}
            className="px-3 py-1 text-[10px] font-semibold rounded text-white bg-violet-600 hover:bg-violet-700"
          >
            Register
          </button>
        </div>
      )}

      {loading ? (
        <div className={`text-xs ${sub}`}>Loading…</div>
      ) : consumers.length === 0 ? (
        <div className={`text-xs ${sub}`}>
          No consumers registered. Downstream readers self-register so schema
          changes can flag impact.
        </div>
      ) : (
        consumers.map((c) => (
          <div
            key={c.id}
            className={`flex items-center justify-between px-2.5 py-1.5 rounded border text-xs ${
              dark ? 'bg-slate-800 border-slate-700' : 'bg-white border-slate-200'
            }`}
          >
            <div className="min-w-0">
              <div className={`font-mono truncate ${dark ? 'text-slate-200' : 'text-slate-700'}`}>
                {c.consumer_id}
              </div>
              <div className={sub}>
                {c.consumer_type}{c.attested_by ? ` · ${c.attested_by}` : ''}{c.notes ? ` · ${c.notes}` : ''}
              </div>
            </div>
            <button
              onClick={() => handleRemove(c)}
              className="ml-2 px-1.5 py-0.5 text-[10px] rounded text-slate-500 hover:text-red-600 hover:bg-red-50"
              title="Remove this consumer"
            >
              ✕
            </button>
          </div>
        ))
      )}
    </div>
  );
}

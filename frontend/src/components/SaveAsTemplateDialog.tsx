/**
 * SaveAsTemplateDialog — multi-field modal for saving a pipeline as a
 * reusable user template. Triggered from the Pipelines page row actions.
 *
 * Three fields: Name (required + unique), Tagline (one-liner), Description
 * (1-2 sentences). All three feed straight into the user_templates row
 * and surface on the TemplatesPage gallery card.
 */

import { useEffect, useRef, useState } from 'react';
import { api } from '../api/client';
import { toast } from './Toast';

interface Props {
  open: boolean;
  pipelineName: string;
  steps: any[];
  connections: any[];
  existingNames: string[];          // for client-side dup check (case-insensitive)
  onClose: () => void;
  onSaved: (tpl: any) => void;
}

export default function SaveAsTemplateDialog({
  open, pipelineName, steps, connections, existingNames, onClose, onSaved,
}: Props) {
  const [name, setName] = useState(pipelineName);
  const [tagline, setTagline] = useState('');
  const [description, setDescription] = useState('');
  const [category, setCategory] = useState('Custom');
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string>('');
  const nameRef = useRef<HTMLInputElement>(null);

  useEffect(() => {
    if (open) {
      setName(pipelineName);
      setTagline('');
      setDescription('');
      setCategory('Custom');
      setError('');
      setTimeout(() => { nameRef.current?.focus(); nameRef.current?.select(); }, 50);
    }
  }, [open, pipelineName]);

  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') onClose();
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [open, onClose]);

  if (!open) return null;

  const trimmedName = name.trim();
  const lowerExisting = existingNames.map((n) => n.toLowerCase());
  const isDup = trimmedName.length > 0 && lowerExisting.includes(trimmedName.toLowerCase());
  const canSave = trimmedName.length > 0 && !isDup && steps.length > 0;

  const handleSave = async () => {
    if (!canSave) return;
    setSaving(true);
    setError('');
    try {
      const created = await api.createUserTemplate({
        name: trimmedName,
        tagline: tagline.trim(),
        description: description.trim(),
        category: category.trim() || 'Custom',
        steps,
        connections,
      });
      toast.success('Template saved', `"${trimmedName}" added to your library`);
      onSaved(created);
      onClose();
    } catch (e: any) {
      const msg = e?.message || 'Could not save template';
      // Backend 409 → name taken (server-side races with our client-side check)
      if (/already exists/i.test(msg)) {
        setError(`A template named "${trimmedName}" already exists.`);
      } else {
        setError(msg);
      }
    } finally {
      setSaving(false);
    }
  };

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center p-4">
      {/* Backdrop */}
      <div className="absolute inset-0 bg-slate-900/60 backdrop-blur-sm" onClick={onClose} />
      {/* Card */}
      <div className="relative w-full max-w-lg rounded-2xl bg-white shadow-2xl overflow-hidden">
        {/* Gradient header — matches the Templates page brand mark */}
        <div className="relative px-6 py-4 bg-gradient-to-r from-violet-500 via-fuchsia-500 to-emerald-500">
          <h2 className="text-lg font-bold text-white">Save as template</h2>
          <p className="mt-0.5 text-sm text-white/85">
            Add this pipeline to your personal template library.
          </p>
        </div>

        {/* Body */}
        <div className="p-6 space-y-4">
          <div>
            <label className="block text-sm font-semibold text-slate-700 mb-1">
              Name <span className="text-red-500">*</span>
            </label>
            <input
              ref={nameRef}
              type="text"
              value={name}
              onChange={(e) => setName(e.target.value)}
              className={`w-full px-3 py-2 text-sm rounded-lg border focus:outline-none focus:ring-2 ${
                isDup
                  ? 'border-red-300 bg-red-50 focus:ring-red-200'
                  : 'border-slate-300 bg-white focus:ring-violet-200 focus:border-violet-400'
              }`}
              placeholder="e.g. Customer 360 sync"
            />
            {isDup && (
              <p className="mt-1 text-xs text-red-600">
                A template named "{trimmedName}" already exists. Pick a different name.
              </p>
            )}
          </div>

          <div>
            <label className="block text-sm font-semibold text-slate-700 mb-1">
              Tagline <span className="text-slate-400 font-normal">(one-liner shown on the card)</span>
            </label>
            <input
              type="text"
              value={tagline}
              onChange={(e) => setTagline(e.target.value)}
              className="w-full px-3 py-2 text-sm rounded-lg border border-slate-300 bg-white focus:outline-none focus:ring-2 focus:ring-violet-200 focus:border-violet-400"
              placeholder="e.g. Daily customer metrics rollup with Slack alert"
              maxLength={120}
            />
          </div>

          <div>
            <label className="block text-sm font-semibold text-slate-700 mb-1">
              Description <span className="text-slate-400 font-normal">(optional)</span>
            </label>
            <textarea
              value={description}
              onChange={(e) => setDescription(e.target.value)}
              className="w-full px-3 py-2 text-sm rounded-lg border border-slate-300 bg-white focus:outline-none focus:ring-2 focus:ring-violet-200 focus:border-violet-400 resize-y min-h-[64px]"
              placeholder="What does this pipeline do? Who is it for? When would you use it?"
              rows={3}
              maxLength={400}
            />
          </div>

          <div>
            <label className="block text-sm font-semibold text-slate-700 mb-1">
              Category
            </label>
            <input
              type="text"
              value={category}
              onChange={(e) => setCategory(e.target.value)}
              className="w-full px-3 py-2 text-sm rounded-lg border border-slate-300 bg-white focus:outline-none focus:ring-2 focus:ring-violet-200 focus:border-violet-400"
              placeholder="Custom"
            />
            <p className="mt-1 text-xs text-slate-500">
              Used to group your templates in the gallery filter.
            </p>
          </div>

          <div className="rounded-lg bg-slate-50 px-3 py-2.5 text-xs text-slate-600">
            Snapshot of <strong>{steps.length} node{steps.length === 1 ? '' : 's'}</strong>
            {' '}and <strong>{connections.length} connection{connections.length === 1 ? '' : 's'}</strong>
            {' '}will be saved. Future edits to the source pipeline will not change this template.
          </div>

          {error && (
            <div className="rounded-lg border border-red-200 bg-red-50 px-3 py-2 text-sm text-red-700">
              {error}
            </div>
          )}
        </div>

        {/* Footer */}
        <div className="flex items-center justify-end gap-2 px-6 py-4 bg-slate-50 border-t border-slate-200">
          <button
            type="button"
            onClick={onClose}
            disabled={saving}
            className="px-4 py-2 text-sm font-semibold rounded-lg text-slate-700 bg-white border border-slate-300 hover:bg-slate-100 disabled:opacity-50"
          >
            Cancel
          </button>
          <button
            type="button"
            onClick={handleSave}
            disabled={!canSave || saving}
            className="px-5 py-2 text-sm font-bold rounded-lg text-white bg-gradient-to-r from-violet-600 to-fuchsia-600 hover:brightness-110 shadow-sm disabled:opacity-40 disabled:cursor-not-allowed"
          >
            {saving ? 'Saving…' : 'Save template'}
          </button>
        </div>
      </div>
    </div>
  );
}

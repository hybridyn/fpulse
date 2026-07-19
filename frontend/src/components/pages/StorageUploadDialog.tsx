/**
 * Storage upload dialog (Y15 2026-05-23).
 *
 * Replaces the inline file-picker that silently picked the current
 * project context. The dialog asks the user to choose explicitly:
 *
 *   • Scope — Global (workspace-wide) or Project (scoped to one)
 *   • Project — dropdown of the workspace's projects (default = active context)
 *   • Folder — dropdown of folders within the chosen project (root if none)
 *   • Description — optional free-text note
 *   • File — drag-drop OR click-to-browse
 *
 * The folder dropdown only appears when a project is picked.
 * F-Pulse OSS folders are 1-level deep (see backend/fpulse/api/folders.py),
 * so the dropdown is flat — no nested tree needed.
 */

import { useEffect, useState } from 'react';
import { api } from '../../api/client';
import { toast } from '../Toast';

interface Project {
  id: string;
  name: string;
}

interface Folder {
  id: string;
  name: string;
  project_id: string;
}

const ACCEPT =
  '.csv,.tsv,.txt,.json,.ndjson,.jsonl,.parquet,.pq,.xlsx,.xls,.xml';

const INPUT_CLASS =
  'w-full text-sm rounded-lg border border-slate-300 bg-white px-3 py-2 ' +
  'focus:outline-none focus:ring-2 focus:ring-blue-500/40 focus:border-blue-500 ' +
  'transition-colors';

type Scope = 'global' | 'project';

export default function StorageUploadDialog({
  defaultProjectId,
  defaultProjectName,
  onClose,
  onUploaded,
}: {
  defaultProjectId?: string | null;
  defaultProjectName?: string;
  onClose: () => void;
  onUploaded: () => void;
}) {
  // If a project is active in the page context, default the scope to
  // "project". Otherwise default to "global" — feels honest with what
  // the user expects when there's no project context.
  const [scope, setScope] = useState<Scope>(defaultProjectId ? 'project' : 'global');
  const [projectId, setProjectId] = useState<string>(defaultProjectId || '');
  const [folderId, setFolderId] = useState<string>('');
  const [description, setDescription] = useState('');
  const [file, setFile] = useState<File | null>(null);
  const [dragOver, setDragOver] = useState(false);
  const [busy, setBusy] = useState(false);

  const [projects, setProjects] = useState<Project[]>([]);
  const [projectsLoading, setProjectsLoading] = useState(true);
  const [folders, setFolders] = useState<Folder[]>([]);
  const [foldersLoading, setFoldersLoading] = useState(false);

  // Load projects on mount.
  useEffect(() => {
    let alive = true;
    api
      .listProjects()
      .then((rows) => {
        if (!alive) return;
        const list = (rows || []).map((p: any) => ({ id: p.id, name: p.name }));
        setProjects(list);
      })
      .catch((err) => {
        if (!alive) return;
        // Surface real errors — a silent empty list looks identical
        // to "no projects exist" and that's misleading on a fresh
        // 401 or network blip. Toast tells the user why.
        toast.error(`Could not load projects: ${(err as Error).message || err}`);
        setProjects([]);
      })
      .finally(() => {
        if (alive) setProjectsLoading(false);
      });
    return () => {
      alive = false;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // Auto-pick a sensible project when the user flips scope to Project
  // and hasn't picked one yet. Without this, clicking the Project radio
  // looked like "nothing happened" — the dropdown just showed the
  // placeholder "— pick a project —" option, leaving the user wondering
  // whether the click had registered.
  useEffect(() => {
    if (scope !== 'project') return;
    if (projectId) return; // already picked
    if (projects.length === 0) return;
    setProjectId(defaultProjectId || projects[0].id);
  }, [scope, projects, projectId, defaultProjectId]);

  // Load folders whenever the selected project changes.
  useEffect(() => {
    if (scope !== 'project' || !projectId) {
      setFolders([]);
      setFolderId('');
      return;
    }
    let alive = true;
    setFoldersLoading(true);
    api
      .listFolders(projectId)
      .then((rows) => {
        if (!alive) return;
        setFolders((rows || []) as Folder[]);
      })
      .catch(() => {
        if (alive) setFolders([]);
      })
      .finally(() => {
        if (alive) setFoldersLoading(false);
      });
    return () => {
      alive = false;
    };
  }, [scope, projectId]);

  const onPickFile = () => {
    const input = document.createElement('input');
    input.type = 'file';
    input.accept = ACCEPT;
    input.onchange = () => {
      const picked = input.files?.[0];
      if (picked) setFile(picked);
    };
    input.click();
  };

  const onDrop = (e: React.DragEvent) => {
    e.preventDefault();
    setDragOver(false);
    const dropped = e.dataTransfer.files?.[0];
    if (dropped) setFile(dropped);
  };

  const canSubmit =
    !!file && !busy && (scope === 'global' || (scope === 'project' && !!projectId));

  const onSubmit = async () => {
    if (!file) {
      toast.error('Pick a file first.');
      return;
    }
    if (scope === 'project' && !projectId) {
      toast.error('Pick a project or switch the scope to Global.');
      return;
    }
    setBusy(true);
    const form = new FormData();
    form.append('file', file);
    const qs = new URLSearchParams();
    if (scope === 'project') {
      qs.set('project_id', projectId);
      if (folderId) qs.set('folder_id', folderId);
    }
    if (description.trim()) qs.set('description', description.trim());
    const url = qs.toString()
      ? `/api/storage/upload?${qs.toString()}`
      : '/api/storage/upload';
    try {
      await api.postRaw(url, form);
      const scopeLabel =
        scope === 'global'
          ? 'Global'
          : `${projects.find((p) => p.id === projectId)?.name || 'Project'}${
              folderId
                ? ` / ${folders.find((f) => f.id === folderId)?.name || 'folder'}`
                : ''
            }`;
      toast.success(`Uploaded ${file.name} to ${scopeLabel}`);
      onUploaded();
    } catch (err) {
      toast.error(`Upload failed: ${(err as Error).message || err}`);
    } finally {
      setBusy(false);
    }
  };

  const fileSizeLabel = file ? humanBytes(file.size) : '';

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-slate-900/40 backdrop-blur-sm"
      onClick={(e) => {
        if (e.target === e.currentTarget && !busy) onClose();
      }}
    >
      <div className="bg-white rounded-2xl shadow-2xl border border-slate-200/60 w-[540px] max-w-[95vw] overflow-hidden">
        {/* Header */}
        <div className="px-6 py-4 border-b border-slate-200/70 bg-gradient-to-b from-slate-50 to-white">
          <h2 className="text-lg font-bold text-slate-900 flex items-center gap-2">
            <svg
              width="20" height="20" viewBox="0 0 24 24" fill="none"
              stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"
              className="text-blue-500"
            >
              <path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4" />
              <polyline points="17 8 12 3 7 8" />
              <line x1="12" y1="3" x2="12" y2="15" />
            </svg>
            Upload file
          </h2>
          <p className="text-xs text-slate-500 mt-1">
            Choose scope, then pick or drop your file. Allowed formats: CSV, JSON, Parquet,
            Excel, XML.
          </p>
        </div>

        {/* Body */}
        <div className="px-6 py-5 space-y-4">
          {/* Scope */}
          <div>
            <label className="block text-sm font-semibold text-slate-700 mb-1.5">
              Scope
            </label>
            <div className="grid grid-cols-2 gap-2">
              <ScopeRadio
                active={scope === 'global'}
                onClick={() => setScope('global')}
                title="Global"
                subtitle="Visible to every project in this workspace."
                icon={
                  <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                    <circle cx="12" cy="12" r="10" />
                    <line x1="2" y1="12" x2="22" y2="12" />
                    <path d="M12 2a15.3 15.3 0 0 1 4 10 15.3 15.3 0 0 1-4 10 15.3 15.3 0 0 1-4-10 15.3 15.3 0 0 1 4-10z" />
                  </svg>
                }
              />
              <ScopeRadio
                active={scope === 'project'}
                onClick={() => setScope('project')}
                // Never disabled — the click is what surfaces the project
                // dropdown below. Loading/error/empty-state cues live
                // there, not on the radio itself, so the user always
                // gets feedback that the click registered.
                title="Project"
                subtitle={
                  defaultProjectName
                    ? `Default: ${defaultProjectName}`
                    : projectsLoading
                      ? 'Loading projects…'
                      : projects.length === 0
                        ? 'No projects yet — go create one first.'
                        : `${projects.length} project${projects.length === 1 ? '' : 's'} available`
                }
                icon={
                  <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                    <path d="M22 19a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5l2 3h9a2 2 0 0 1 2 2z" />
                  </svg>
                }
              />
            </div>
          </div>

          {/* Project + folder */}
          {scope === 'project' && (
            <>
              <div>
                <label className="block text-sm font-semibold text-slate-700 mb-1.5">
                  Project
                </label>
                {projectsLoading ? (
                  <div className="text-xs text-slate-500 italic">Loading projects…</div>
                ) : projects.length === 0 ? (
                  <div className="text-xs text-amber-700 bg-amber-50 border border-amber-200 rounded-lg px-3 py-2">
                    No projects exist. Switch to Global, or create a project first under Projects.
                  </div>
                ) : (
                  // Auto-pick effect above seeds projectId on scope flip,
                  // so the dropdown always lands on a real value — no
                  // placeholder option needed. The user changes their
                  // mind by picking another row.
                  <select
                    value={projectId}
                    onChange={(e) => setProjectId(e.target.value)}
                    className={INPUT_CLASS}
                  >
                    {projects.map((p) => (
                      <option key={p.id} value={p.id}>
                        {p.name}
                      </option>
                    ))}
                  </select>
                )}
              </div>

              <div>
                <label className="block text-sm font-semibold text-slate-700 mb-1.5">
                  Folder <span className="text-slate-400 font-normal">(optional)</span>
                </label>
                {!projectId ? (
                  <div className="text-xs text-slate-400 italic">
                    Pick a project to load folders.
                  </div>
                ) : foldersLoading ? (
                  <div className="text-xs text-slate-500 italic">Loading folders…</div>
                ) : folders.length === 0 ? (
                  <div className="text-xs text-slate-500 italic">
                    No folders in this project. The file will land at the project root.
                  </div>
                ) : (
                  <select
                    value={folderId}
                    onChange={(e) => setFolderId(e.target.value)}
                    className={INPUT_CLASS}
                  >
                    <option value="">Project root</option>
                    {folders.map((f) => (
                      <option key={f.id} value={f.id}>
                        {f.name}
                      </option>
                    ))}
                  </select>
                )}
              </div>
            </>
          )}

          {/* Description */}
          <div>
            <label className="block text-sm font-semibold text-slate-700 mb-1.5">
              Description <span className="text-slate-400 font-normal">(optional)</span>
            </label>
            <input
              value={description}
              onChange={(e) => setDescription(e.target.value)}
              placeholder="Q1 2026 customer extract"
              className={INPUT_CLASS}
            />
          </div>

          {/* File picker / drop area */}
          <div>
            <label className="block text-sm font-semibold text-slate-700 mb-1.5">File</label>
            <div
              onDragOver={(e) => {
                e.preventDefault();
                setDragOver(true);
              }}
              onDragLeave={() => setDragOver(false)}
              onDrop={onDrop}
              onClick={onPickFile}
              className={`cursor-pointer rounded-lg border-2 border-dashed px-5 py-6 text-center transition-colors ${
                dragOver
                  ? 'border-blue-400 bg-blue-50/60'
                  : file
                    ? 'border-emerald-300 bg-emerald-50/40'
                    : 'border-slate-300 bg-slate-50/40 hover:bg-slate-50'
              }`}
            >
              {file ? (
                <div>
                  <div className="text-sm font-semibold text-slate-900">{file.name}</div>
                  <div className="text-xs text-slate-500 mt-0.5">{fileSizeLabel}</div>
                  <div className="text-xs text-blue-600 mt-2">Click to pick a different file</div>
                </div>
              ) : (
                <div>
                  <div className="text-sm text-slate-600">
                    Drag a file here, or <span className="text-blue-600 font-semibold">click to browse</span>
                  </div>
                  <div className="text-xs text-slate-400 mt-1">
                    CSV, JSON, Parquet, Excel, XML
                  </div>
                </div>
              )}
            </div>
          </div>
        </div>

        {/* Footer */}
        <div className="px-6 py-4 border-t border-slate-200/70 flex justify-end gap-2 bg-slate-50/60">
          <button
            onClick={onClose}
            disabled={busy}
            className="px-4 py-2 text-sm font-medium rounded-lg text-slate-600 hover:bg-slate-200/70 transition-colors disabled:opacity-50"
          >
            Cancel
          </button>
          <button
            onClick={onSubmit}
            disabled={!canSubmit}
            className="px-4 py-2 text-white text-sm font-bold rounded-lg transition-all shadow-sm hover:shadow-md disabled:opacity-50 disabled:cursor-not-allowed"
            style={{ background: 'linear-gradient(135deg, #3B7DD8, #1E5AAF)' }}
          >
            {busy ? 'Uploading…' : 'Upload'}
          </button>
        </div>
      </div>
    </div>
  );
}

function ScopeRadio({
  active,
  onClick,
  title,
  subtitle,
  icon,
  disabled,
}: {
  active: boolean;
  onClick: () => void;
  title: string;
  subtitle: string;
  icon: React.ReactNode;
  disabled?: boolean;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      disabled={disabled}
      className={`flex items-start gap-3 text-left rounded-lg border px-4 py-3 transition-colors ${
        active
          ? 'border-blue-500 bg-blue-50 ring-2 ring-blue-500/30'
          : disabled
            ? 'border-slate-200 bg-slate-50 opacity-50 cursor-not-allowed'
            : 'border-slate-200 bg-white hover:bg-slate-50'
      }`}
    >
      <span className={active ? 'text-blue-600 mt-0.5' : 'text-slate-500 mt-0.5'}>{icon}</span>
      <span className="min-w-0">
        <span className={`block text-sm font-semibold ${active ? 'text-blue-700' : 'text-slate-900'}`}>
          {title}
        </span>
        <span className="block text-xs text-slate-500 mt-0.5">{subtitle}</span>
      </span>
    </button>
  );
}

function humanBytes(n: number): string {
  if (!n) return '0 B';
  const units = ['B', 'KB', 'MB', 'GB'];
  let v = n;
  let unit = 0;
  while (v >= 1024 && unit < units.length - 1) {
    v /= 1024;
    unit += 1;
  }
  return `${v.toFixed(v >= 100 ? 0 : 1)} ${units[unit]}`;
}

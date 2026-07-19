import { useMemo, useState } from 'react';
import { useWorkflowStore } from '../stores/workflowStore';
import { ModuleIcon } from './ModulesPanel';
import {
  CATEGORY_ICONS,
  makeReconciledModulesHook,
  type ModuleItem,
} from './modulesPanelData';
import Icon from './shared/Icon';
// ActivitiesRibbon needs its own MODULES reference to feed the reconciler.
// We import from ModulesPanel only the React component (Fast-Refresh-safe);
// MODULES is declared inside ModulesPanel and not exported, so we
// re-instantiate the hook with an empty palette here. The reconciler will
// fall back to the backend node registry, which is the source of truth
// anyway — ribbon shows whatever the backend reports.
const useReconciledModules = makeReconciledModulesHook([]);

/**
 * Activities Ribbon — horizontal node-picker strip.
 *
 * Replaces the right-side node panel. Lives directly below the editor
 * Toolbar, runs the full width of the canvas. Two rows:
 *   1. Category tabs — horizontal strip with emoji + name + count
 *   2. Node tiles — horizontal scrollable strip for the active category
 *
 * Rationale (Apr 22 2026): the previous vertical accordion on the right
 * was competing with the canvas for width and the category labels got
 * cramped. A horizontal ribbon is the familiar pattern for data
 * engineering tools and gives the canvas its full width back.
 *
 * Drag behaviour unchanged — each tile sets the same
 * `application/fpulse-node` mime so the existing canvas drop handler
 * Just Works. Click-to-insert also unchanged: add the node then fitView.
 */
export default function ActivitiesRibbon() {
  const reconciledModules = useReconciledModules();
  const addNode = useWorkflowStore((s) => s.addNode);
  const rfInstance = useWorkflowStore((s) => s.reactFlowInstance);

  const [search, setSearch] = useState('');
  const [activeCategory, setActiveCategory] = useState<string>('Data Movement');
  const [collapsed, setCollapsed] = useState(false);

  const handleAdd = (type: string) => {
    addNode(type);
    setTimeout(() => rfInstance?.fitView({ padding: 0.3, duration: 300 }), 50);
  };

  // Search mode flattens categories — every matching node across every
  // category appears in the node strip. Browse mode shows only the
  // active category's nodes.
  const nodesToRender = useMemo<ModuleItem[]>(() => {
    if (search.trim()) {
      const q = search.toLowerCase();
      const matches: ModuleItem[] = [];
      for (const cat of reconciledModules) {
        for (const item of cat.items) {
          if (
            item.label.toLowerCase().includes(q) ||
            item.type.toLowerCase().includes(q)
          ) {
            matches.push(item);
          }
        }
      }
      return matches;
    }
    const cat = reconciledModules.find((c) => c.name === activeCategory);
    return cat ? cat.items : [];
  }, [reconciledModules, activeCategory, search]);

  if (collapsed) {
    return (
      <div className="shrink-0 bg-white border-b border-slate-200 px-3 py-1.5 flex items-center gap-2">
        <button
          onClick={() => setCollapsed(false)}
          className="flex items-center gap-1.5 text-xs font-semibold text-slate-600 hover:text-indigo-600 transition-colors"
          title="Show activities ribbon"
        >
          <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round">
            <polyline points="6 9 12 15 18 9" />
          </svg>
          Show nodes
        </button>
      </div>
    );
  }

  return (
    <div className="shrink-0 bg-white border-b border-slate-200 flex flex-col">
      {/* Row 1 — category tabs + search */}
      <div className="flex items-center gap-1 px-3 pt-1.5 pb-1.5 border-b border-slate-100">
        <div className="flex items-center gap-1 overflow-x-auto flex-1 min-w-0 scrollbar-thin">
          {reconciledModules.map((cat) => {
            const ci = CATEGORY_ICONS[cat.name];
            const isActive = !search.trim() && activeCategory === cat.name;
            return (
              <button
                key={cat.name}
                onClick={() => {
                  setActiveCategory(cat.name);
                  setSearch('');
                }}
                className={`shrink-0 flex items-center gap-1.5 px-2.5 py-1.5 rounded-md text-[12px] font-semibold transition-all ${
                  isActive
                    ? 'bg-indigo-50 text-indigo-700 border border-indigo-200 shadow-sm'
                    : 'text-slate-700 hover:text-slate-900 hover:bg-slate-50 border border-transparent'
                }`}
              >
                <span style={{ color: ci?.color }}>
                  {ci?.icon ? <Icon name={ci.icon} size={14} /> : null}
                </span>
                <span>{cat.name}</span>
                <span className={`text-xs font-mono font-bold px-1.5 py-0.5 rounded ${
                  isActive ? 'bg-indigo-100 text-indigo-700' : 'bg-slate-100 text-slate-600'
                }`}>
                  {cat.items.length}
                </span>
              </button>
            );
          })}
        </div>

        {/* Search — narrow to preserve category strip space */}
        <div className="relative shrink-0 ml-2">
          <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="#64748b" strokeWidth="2" className="absolute left-2 top-1/2 -translate-y-1/2">
            <circle cx="11" cy="11" r="8" /><line x1="21" y1="21" x2="16.65" y2="16.65" />
          </svg>
          <input
            type="text"
            value={search}
            onChange={(e) => setSearch(e.target.value)}
            placeholder="Search nodes…"
            className="w-44 pl-7 pr-7 py-1 text-[12px] border border-slate-300 rounded-md outline-none focus:ring-2 focus:ring-indigo-200 focus:border-indigo-400 text-slate-800 placeholder:text-slate-500"
          />
          {search && (
            <button
              onClick={() => setSearch('')}
              className="absolute right-1.5 top-1/2 -translate-y-1/2 text-slate-500 hover:text-slate-700"
            >
              <svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5">
                <line x1="18" y1="6" x2="6" y2="18" /><line x1="6" y1="6" x2="18" y2="18" />
              </svg>
            </button>
          )}
        </div>

        {/* Collapse toggle */}
        <button
          onClick={() => setCollapsed(true)}
          className="shrink-0 ml-1 w-6 h-6 rounded flex items-center justify-center text-slate-500 hover:text-slate-700 hover:bg-slate-100 transition-all"
          title="Hide ribbon"
        >
          <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round">
            <polyline points="18 15 12 9 6 15" />
          </svg>
        </button>
      </div>

      {/* Row 2 — node tile strip (horizontal scroll) */}
      <div className="flex items-center gap-1 px-3 py-2 overflow-x-auto min-h-[76px] scrollbar-thin">
        {nodesToRender.length === 0 ? (
          <div className="text-[12px] text-slate-500 px-2">
            {search.trim() ? `No nodes match "${search}"` : 'No nodes in this category'}
          </div>
        ) : (
          nodesToRender.map((item) => (
            <NodeTile key={item.type} item={item} onAdd={handleAdd} />
          ))
        )}
      </div>
    </div>
  );
}

function NodeTile({
  item,
  onAdd,
}: {
  item: ModuleItem;
  onAdd: (type: string) => void;
}) {
  return (
    <button
      draggable
      onDragStart={(e) => {
        e.dataTransfer.setData('application/fpulse-node', item.type);
        e.dataTransfer.effectAllowed = 'move';
      }}
      onClick={() => onAdd(item.type)}
      className="shrink-0 w-20 flex flex-col items-center gap-1 p-1.5 rounded-lg border border-transparent hover:border-indigo-200 hover:bg-indigo-50/40 hover:shadow-sm transition-all group cursor-grab active:cursor-grabbing active:scale-[0.96]"
      title={`Drag to canvas or click to insert: ${item.label}`}
    >
      <div className="group-hover:scale-110 transition-transform shrink-0">
        <ModuleIcon type={item.type} size={34} />
      </div>
      <span className="text-xs text-slate-700 group-hover:text-indigo-700 font-semibold text-center leading-tight w-full break-words line-clamp-2">
        {item.label}
      </span>
    </button>
  );
}

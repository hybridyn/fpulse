import { useState, useRef, useEffect, memo } from 'react';
import { NodeResizer, type NodeProps, type Node as FlowNode } from '@xyflow/react';

/**
 * Sticky Note — canvas annotation node.
 * Draggable, resizable, editable text with color options.
 */

const COLORS = [
  { name: 'yellow', bg: 'bg-amber-100', border: 'border-amber-300', text: 'text-amber-900', hex: '#fef3c7' },
  { name: 'blue', bg: 'bg-blue-100', border: 'border-blue-300', text: 'text-blue-900', hex: '#dbeafe' },
  { name: 'green', bg: 'bg-emerald-100', border: 'border-emerald-300', text: 'text-emerald-900', hex: '#d1fae5' },
  { name: 'pink', bg: 'bg-pink-100', border: 'border-pink-300', text: 'text-pink-900', hex: '#fce7f3' },
  { name: 'purple', bg: 'bg-violet-100', border: 'border-violet-300', text: 'text-violet-900', hex: '#ede9fe' },
];

// xyflow v12 types Node.data as Record<string, unknown> by default. Without
// a typed Node generic, every `data.<field>` access widens to `{}` and
// trips strict-mode checks. Declare the sticky-note payload shape here so
// `data.text`, `data.colorIdx`, and `data.onUpdate(...)` resolve properly.
type StickyData = {
  text?: string;
  colorIdx?: number;
  onUpdate?: (next: { text: string; colorIdx: number }) => void;
};
type StickyNoteNodeType = FlowNode<StickyData, 'stickyNote'>;

function StickyNoteNode({ data, selected }: NodeProps<StickyNoteNodeType>) {
  const [editing, setEditing] = useState(false);
  const [text, setText] = useState<string>(data.text || 'Add a note...');
  const [colorIdx, setColorIdx] = useState<number>(data.colorIdx || 0);
  const textRef = useRef<HTMLTextAreaElement>(null);

  const color = COLORS[colorIdx % COLORS.length];

  useEffect(() => {
    if (editing && textRef.current) {
      textRef.current.focus();
      textRef.current.select();
    }
  }, [editing]);

  const handleBlur = () => {
    setEditing(false);
    // Store text back to data (React Flow will persist via nodes state)
    if (data.onUpdate) {
      data.onUpdate({ text, colorIdx });
    }
  };

  return (
    <>
      <NodeResizer
        minWidth={120}
        minHeight={80}
        isVisible={selected}
        lineClassName="!border-amber-400"
        handleClassName="!w-2.5 !h-2.5 !bg-amber-400 !border-amber-500"
      />
      <div
        className={`w-full h-full rounded-lg border-2 shadow-md transition-shadow ${color.bg} ${color.border} ${selected ? 'shadow-lg ring-2 ring-amber-300/50' : ''}`}
        style={{ minWidth: 120, minHeight: 80 }}
        onDoubleClick={() => setEditing(true)}
      >
        {/* Color picker trigger */}
        {selected && (
          <div className="absolute -top-7 right-0 flex items-center gap-1 bg-white rounded-lg shadow-sm border border-slate-200 px-1.5 py-1 z-10">
            {COLORS.map((c, i) => (
              <button
                key={c.name}
                onClick={(e) => {
                  e.stopPropagation();
                  setColorIdx(i);
                  if (data.onUpdate) data.onUpdate({ text, colorIdx: i });
                }}
                className={`w-4 h-4 rounded-full border-2 transition-transform ${
                  i === colorIdx ? 'scale-125 border-slate-500' : 'border-transparent hover:scale-110'
                }`}
                style={{ background: c.hex }}
                title={c.name}
              />
            ))}
          </div>
        )}

        {editing ? (
          <textarea
            ref={textRef}
            value={text}
            onChange={(e) => setText(e.target.value)}
            onBlur={handleBlur}
            onKeyDown={(e) => {
              if (e.key === 'Escape') handleBlur();
            }}
            className={`w-full h-full bg-transparent resize-none outline-none p-3 text-xs leading-relaxed ${color.text} font-medium nodrag`}
            placeholder="Type your note..."
          />
        ) : (
          <div className={`w-full h-full p-3 text-xs leading-relaxed ${color.text} font-medium whitespace-pre-wrap overflow-auto cursor-text`}>
            {text || <span className="opacity-50 italic">Double-click to edit...</span>}
          </div>
        )}
      </div>
    </>
  );
}

export default memo(StickyNoteNode);

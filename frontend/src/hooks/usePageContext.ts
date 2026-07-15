/**
 * usePageContext — every page registers its current context so the agent
 * widget can include it in tool calls without per-page glue.
 *
 * Pattern (per project_fpulse_ai_operational_architecture.md "Pass IDs/handles,
 * not payloads" + ai-boundary-contract.md §1 layered context model):
 *
 *   At the top of a page:
 *     usePageContext({
 *       page: 'pipelines.list',
 *       visible_ids: pipelines.map(p => p.id),
 *       selected_ids: selectedIds,
 *       filters: activeFilters,
 *     });
 *
 *   In the AgentChatPanel (single global instance):
 *     const ctx = useCurrentPageContext();
 *     // ctx is the most recently published value, or null if no page registered
 *
 * Implementation is a tiny module-level event emitter — no React Context
 * needed since we only ever care about the latest value, not history.
 * Intentionally NOT persisted to localStorage; context is per-session.
 */

import { useEffect, useState } from 'react';

/**
 * Code-writing context published by editor surfaces (ConfigPanel for
 * transform/SQL nodes, the canvas Editor when a node is selected). Lets
 * the Copilot answer "fix this", "explain this", "rewrite this in dialect X"
 * without the user re-typing the code into the chat.
 *
 * Always optional — most pages don't have an active code surface and
 * leaving these undefined is the right default.
 */
export interface CodeContext {
  /** Workflow / pipeline id that owns the code being edited. */
  workflow_id?: string;
  /** Step / node id whose params are being edited. */
  node_id?: string;
  /** "transform", "execute_sql_task", "filter", etc. */
  node_type?: string;
  /** "sql" | "python" | "expression" — informs the LLM how to read it. */
  language?: 'sql' | 'python' | 'expression';
  /** The full expression / query text the user is editing right now. */
  expression?: string;
  /** Highlighted span (subset of expression). */
  selection?: string;
  /** Most recent validator error on this node, if any. */
  last_error?: string;
}

/**
 * Compact snapshot of one entity rendered on the current page. Sent in
 * `visible_items` so the agent's fast-lane router and single-shot LLM mode
 * can answer questions without a tool call to discover what's on screen.
 *
 * Keep it small — pages cap to 50 items. Use `meta` for page-specific
 * extras (last_run, duration_ms, type, etc.). Never include payload data.
 */
export interface VisibleItem {
  id: string;
  name?: string;
  status?: string;
  kind?: string;
  meta?: Record<string, string | number | boolean | null>;
}

export interface CurrentPageContext {
  page: string;
  visible_ids: string[];
  selected_ids: string[];
  filters: Record<string, unknown>;
  environment: 'dev' | 'prod';
  /** Rich snapshot of currently-visible entities (id + name + status + kind). */
  visible_items?: VisibleItem[];
  /** Set when a code editor surface is active (ConfigPanel SQL / transform). */
  code?: CodeContext;
}

type Listener = (ctx: CurrentPageContext | null) => void;

let _current: CurrentPageContext | null = null;
const _listeners = new Set<Listener>();

function _publish(next: CurrentPageContext | null): void {
  _current = next;
  _listeners.forEach((l) => {
    try {
      l(next);
    } catch {
      // Listener errors must not break other listeners.
    }
  });
}

/**
 * Pages call this near the top of their render. Re-runs whenever any of
 * the inputs change (so visible_ids etc. stay in sync as the user filters).
 */
export function usePageContext(input: {
  page: string;
  visible_ids?: string[];
  selected_ids?: string[];
  filters?: Record<string, unknown>;
  environment?: 'dev' | 'prod';
  /** Rich snapshot of currently-visible entities (capped to 50 by callers). */
  visible_items?: VisibleItem[];
  /** Optional code-editor state (active expression / SQL / language). */
  code?: CodeContext;
}): void {
  const env: 'dev' | 'prod' =
    input.environment ??
    ((localStorage.getItem('fpulse_environment') as 'dev' | 'prod' | null) || 'dev');

  // Stable string keys for dependency tracking — avoids referential-equality
  // re-publishes when callers pass freshly-constructed arrays each render.
  const visibleKey = (input.visible_ids ?? []).join(',');
  const selectedKey = (input.selected_ids ?? []).join(',');
  const filtersKey = JSON.stringify(input.filters ?? {});
  const itemsKey = JSON.stringify(input.visible_items ?? null);
  const codeKey = JSON.stringify(input.code ?? null);

  useEffect(() => {
    const cappedItems = input.visible_items
      ? input.visible_items.slice(0, 50)
      : undefined;
    _publish({
      page: input.page,
      visible_ids: input.visible_ids ?? [],
      selected_ids: input.selected_ids ?? [],
      filters: input.filters ?? {},
      environment: env,
      visible_items: cappedItems,
      code: input.code,
    });
    return () => {
      // On unmount, clear so a stale page doesn't get sent to the agent
      // after the user navigates away.
      if (_current?.page === input.page) {
        _publish(null);
      }
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [input.page, visibleKey, selectedKey, filtersKey, itemsKey, codeKey, env]);
}

/**
 * AgentChatPanel and other consumers use this to read the latest context.
 * Subscribes via useEffect; re-renders on every publish.
 */
export function useCurrentPageContext(): CurrentPageContext | null {
  const [ctx, setCtx] = useState<CurrentPageContext | null>(_current);
  useEffect(() => {
    const listener: Listener = (next) => setCtx(next);
    _listeners.add(listener);
    return () => {
      _listeners.delete(listener);
    };
  }, []);
  return ctx;
}

/**
 * Test helper — synchronously reset the current context.
 */
export function _resetPageContextForTests(): void {
  _publish(null);
}

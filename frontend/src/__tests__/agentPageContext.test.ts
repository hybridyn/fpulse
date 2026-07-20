import { describe, it, expect } from 'vitest';
import { buildPageContextPayload } from '../components/agent/pageContext';
import type { CurrentPageContext } from '../hooks/usePageContext';

/**
 * Locks the global-Copilot wiring that forwards the active code-editor surface
 * (ConfigPanel's published `code`) into `page_context.extra_context`. This was
 * silently dropped before — the panel sent page + IDs but not the expression
 * the user was editing, so "fix this" forced a re-paste. Regressing it is easy
 * (it's one builder), so pin it.
 */

const base = (over: Partial<CurrentPageContext> = {}): CurrentPageContext => ({
  page: 'editor.canvas',
  visible_ids: ['n1', 'n2'],
  selected_ids: ['n1'],
  filters: {},
  environment: 'dev',
  ...over,
});

describe('buildPageContextPayload', () => {
  it('returns a safe default when no page context is registered', () => {
    expect(buildPageContextPayload(null)).toEqual({ page: 'unknown', environment: 'dev' });
  });

  it('carries the base page fields through unchanged', () => {
    const out = buildPageContextPayload(base());
    expect(out.page).toBe('editor.canvas');
    expect(out.visible_ids).toEqual(['n1', 'n2']);
    expect(out.selected_ids).toEqual(['n1']);
    expect(out.environment).toBe('dev');
  });

  it('does NOT attach extra_context when there is no active code surface', () => {
    expect(buildPageContextPayload(base()).extra_context).toBeUndefined();
  });

  it('forwards the active node expression + error into extra_context.active_node', () => {
    const out = buildPageContextPayload(
      base({
        code: {
          node_id: 'transform_7',
          node_type: 'transform',
          language: 'expression',
          expression: 'price * qty AS total',
          last_error: 'unknown column: qty',
        },
      }),
    );
    const active = (out.extra_context as any)?.active_node;
    expect(active).toMatchObject({
      node_id: 'transform_7',
      node_type: 'transform',
      language: 'expression',
      expression: 'price * qty AS total',
      last_error: 'unknown column: qty',
    });
  });

  it('drops undefined/empty code fields so the rendered block stays compact', () => {
    const out = buildPageContextPayload(
      base({
        code: {
          node_id: 'sql_1',
          node_type: 'execute_sql_task',
          language: 'sql',
          expression: 'SELECT 1',
          // workflow_id / selection / last_error intentionally omitted
        },
      }),
    );
    const active = (out.extra_context as any).active_node;
    expect(active).toHaveProperty('expression', 'SELECT 1');
    expect(active).not.toHaveProperty('workflow_id');
    expect(active).not.toHaveProperty('selection');
    expect(active).not.toHaveProperty('last_error');
  });

  it('attaches extra_context for an error-only surface (last_error but no expression yet)', () => {
    const out = buildPageContextPayload(
      base({ code: { node_id: 'transform_2', last_error: 'syntax error near FROM' } }),
    );
    expect((out.extra_context as any).active_node).toMatchObject({
      node_id: 'transform_2',
      last_error: 'syntax error near FROM',
    });
  });
});

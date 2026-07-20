/**
 * Page-context payload builder for the global Copilot panel.
 *
 * Mirrors what the editor canvas already sends through the store, but for the
 * always-on chat (AgentChatPanel): forwards the active code-editor surface —
 * the selected node's SQL / transform / expression text + most-recent validator
 * error, published by ConfigPanel via `usePageContext({ code })` — into
 * `page_context.extra_context`. Without this the Copilot saw the page + visible
 * IDs but NOT the code the user is staring at, so "fix this" / "explain this"
 * forced the user to re-paste the expression into chat.
 *
 * Only signal-bearing fields are sent (undefined dropped) so the rendered block
 * stays compact for local 7B models; the backend sanitizes secrets and
 * budget-caps the block — see backend/fpulse/ai/context.py:to_extra_context_block.
 */
import type { PageContextPayload } from '../../api/agent';
import type { CurrentPageContext } from '../../hooks/usePageContext';

export function buildPageContextPayload(
  pageCtx: CurrentPageContext | null,
): PageContextPayload {
  if (!pageCtx) return { page: 'unknown', environment: 'dev' };
  const base: PageContextPayload = {
    page: pageCtx.page,
    visible_ids: pageCtx.visible_ids,
    selected_ids: pageCtx.selected_ids,
    filters: pageCtx.filters,
    environment: pageCtx.environment,
    visible_items: pageCtx.visible_items,
  };
  const code = pageCtx.code;
  if (code && (code.expression || code.last_error || code.node_id)) {
    const active_node: Record<string, unknown> = {};
    if (code.node_id) active_node.node_id = code.node_id;
    if (code.node_type) active_node.node_type = code.node_type;
    if (code.workflow_id) active_node.workflow_id = code.workflow_id;
    if (code.language) active_node.language = code.language;
    if (code.expression) active_node.expression = code.expression;
    if (code.selection) active_node.selection = code.selection;
    if (code.last_error) active_node.last_error = code.last_error;
    base.extra_context = { active_node };
  }
  return base;
}

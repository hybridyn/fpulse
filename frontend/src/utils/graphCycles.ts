/**
 * Directed-cycle detection for the pipeline graph.
 *
 * Two consumers (2026-06-10 validation-audit fix):
 *   1. Canvas `isValidConnection` — blocks an edge drop that would
 *      close a loop (A→B→C→A), with immediate feedback. Before this,
 *      only direct self-loops were caught at drop time.
 *   2. `validateWorkflow` — safety net for workflows loaded from JSON
 *      or scripted in. Previously NO frontend layer detected indirect
 *      cycles; they reached the backend executor at runtime.
 *
 * Lives in its own module (not nodeArity.ts) so the graph helpers stay
 * dependency-free and tree-shakeable.
 */

export interface EdgeLike {
  source: string;
  target: string;
}

/** Build a source → targets adjacency map from an edge list. */
function buildAdjacency(edges: EdgeLike[]): Map<string, string[]> {
  const adj = new Map<string, string[]>();
  for (const e of edges) {
    if (!e?.source || !e?.target) continue;
    const list = adj.get(e.source);
    if (list) list.push(e.target);
    else adj.set(e.source, [e.target]);
  }
  return adj;
}

/**
 * True when adding `source → target` to `edges` would create a cycle —
 * i.e. `source` is already reachable from `target` by following the
 * existing directed edges. Also true for a direct self-loop.
 */
export function wouldCreateCycle(source: string, target: string, edges: EdgeLike[]): boolean {
  if (!source || !target) return false;
  if (source === target) return true;
  const adj = buildAdjacency(edges);
  const seen = new Set<string>([target]);
  const stack = [target];
  while (stack.length > 0) {
    const cur = stack.pop()!;
    for (const next of adj.get(cur) || []) {
      if (next === source) return true;
      if (!seen.has(next)) {
        seen.add(next);
        stack.push(next);
      }
    }
  }
  return false;
}

/**
 * Returns the set of node ids that sit on at least one directed cycle.
 * Iterative three-color DFS (white/gray/black); a back-edge to a gray
 * node marks every node on the gray path segment as cyclic. Runs in
 * O(V + E) — canvas graphs are tiny, but JSON imports can be arbitrary.
 */
export function findCycleNodeIds(nodeIds: string[], edges: EdgeLike[]): Set<string> {
  const adj = buildAdjacency(edges);
  const idSet = new Set(nodeIds);
  const WHITE = 0, GRAY = 1, BLACK = 2;
  const color = new Map<string, number>();
  for (const id of nodeIds) color.set(id, WHITE);
  const inCycle = new Set<string>();

  for (const root of nodeIds) {
    if (color.get(root) !== WHITE) continue;
    // Stack frames: [nodeId, childIndex]; explicit path for back-edge walks.
    const path: string[] = [];
    const stack: Array<[string, number]> = [[root, 0]];
    color.set(root, GRAY);
    path.push(root);

    while (stack.length > 0) {
      const frame = stack[stack.length - 1];
      const [nodeId] = frame;
      const children = (adj.get(nodeId) || []).filter((c) => idSet.has(c));
      if (frame[1] < children.length) {
        const child = children[frame[1]++];
        const c = color.get(child);
        if (c === GRAY) {
          // Back edge — every node from `child` to the top of the path
          // (inclusive) is on the cycle.
          const start = path.indexOf(child);
          for (let i = start; i < path.length; i++) inCycle.add(path[i]);
        } else if (c === WHITE) {
          color.set(child, GRAY);
          path.push(child);
          stack.push([child, 0]);
        }
      } else {
        color.set(nodeId, BLACK);
        path.pop();
        stack.pop();
      }
    }
  }
  return inCycle;
}

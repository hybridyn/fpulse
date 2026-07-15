import { type Page, expect } from '@playwright/test';

/**
 * Shared helpers for the editor node E2E.
 *
 * Nodes are added by dispatching the SAME drag payload the palette uses
 * (`application/fpulse-node` = the stepType string — see ModulesPanel.tsx /
 * Canvas.tsx onDrop), dropped on the react-flow pane. This is far more robust
 * than dragging palette tiles and works for every registered type.
 */

export async function openEditor(page: Page) {
  await page.goto('/#editor', { waitUntil: 'domcontentloaded' });
  await page.waitForSelector('.react-flow__pane', { timeout: 20_000 });
  await dismissOnboarding(page);
  // Give the palette/registry a beat to load (node-types fetch).
  await page.waitForTimeout(400);
}

/** Fresh sessions show a "Welcome to F-Pulse OSS" wizard whose backdrop
 *  intercepts clicks — dismiss it via its Skip control if present. */
export async function dismissOnboarding(page: Page) {
  const skip = page.getByRole('button', { name: /^skip$/i });
  if (await skip.isVisible({ timeout: 2_500 }).catch(() => false)) {
    await skip.click().catch(() => { /* best-effort */ });
    await page.waitForTimeout(300);
  }
}

/** Drop a node of `type` on the canvas; returns its new node id. */
export async function addNode(page: Page, type: string, offset = 0): Promise<string> {
  const before = await page.$$eval('.react-flow__node', (els) =>
    els.map((n) => n.getAttribute('data-id')),
  );
  // Dispatch the same drag payload the palette uses, on the react-flow pane.
  await page.evaluate(({ t, off }) => {
    const pane = document.querySelector('.react-flow__pane') as HTMLElement | null;
    if (!pane) return;
    const dt = new DataTransfer();
    dt.setData('application/fpulse-node', t);
    const r = pane.getBoundingClientRect();
    // Lay nodes out left-to-right with generous X spacing (so source/target
    // handles never overlap for the drag) but a SMALL Y step, so even the 3rd
    // node (offset 20) stays high in the pane. The old `160 + off*20` put the
    // sink at y≈560 — capped at the pane's bottom edge, where its handles
    // render below the visible area and the filter→sink drag can't land.
    const x = r.left + Math.min(r.width - 220, 200 + off * 30);
    const y = r.top + Math.min(r.height - 140, 140 + off * 8);
    const fire = (name: string) =>
      pane.dispatchEvent(new DragEvent(name, { bubbles: true, cancelable: true, dataTransfer: dt, clientX: x, clientY: y }));
    fire('dragenter'); fire('dragover'); fire('drop');
  }, { t: type, off: offset });
  // Wait for React to actually mount the new node (the drop → setState → render
  // is async — reading synchronously in the same evaluate returns nothing).
  await page.waitForFunction(
    (prev) =>
      Array.from(document.querySelectorAll('.react-flow__node'))
        .some((n) => !prev.includes(n.getAttribute('data-id'))),
    before,
    { timeout: 6_000 },
  ).catch(() => { /* surfaced by the empty-id assertion in the spec */ });
  const after = await page.$$eval('.react-flow__node', (els) =>
    els.map((n) => n.getAttribute('data-id')),
  );
  return (after.find((id) => id && !before.includes(id)) as string) || '';
}

/** Wire an edge from `sourceId`'s output handle to `targetId`'s input handle.
 *  Uses a REAL Playwright mouse drag — react-flow's connection logic doesn't
 *  fire on synthetic events. Handles are standard @xyflow handles
 *  (`.react-flow__handle.source` on the right, `.react-flow__handle.target` on
 *  the left). Place the two nodes far enough apart (addNode offset) that the
 *  handles don't overlap. */
export async function edgeCount(page: Page): Promise<number> {
  return page.$$eval('.react-flow__edge', (els) => els.length);
}

export async function addEdge(page: Page, sourceId: string, targetId: string) {
  const src = page.locator(`.react-flow__node[data-id="${sourceId}"] .react-flow__handle.source`).first();
  const tgt = page.locator(`.react-flow__node[data-id="${targetId}"] .react-flow__handle.target`).first();
  await src.waitFor({ state: 'visible', timeout: 5_000 });
  await tgt.waitFor({ state: 'visible', timeout: 5_000 });
  // Make sure both handles are actually in the viewport and settled before we
  // read their boxes — a handle that just mounted or is scrolled off can
  // report a stale/zero rect, which sends the drag to the wrong spot.
  await src.scrollIntoViewIfNeeded().catch(() => {});
  await tgt.scrollIntoViewIfNeeded().catch(() => {});
  const before = await edgeCount(page);

  // react-flow's connection logic only fires on a REAL mouse drag from the
  // source handle onto the target handle, and the drag can miss (handle just
  // mounted, intermediate move not registered, handles a hair off, or — the
  // usual CI cause — the pointer travels to the target before react-flow has
  // entered "connecting" mode). Verify the edge actually landed and retry with
  // backoff — and THROW if it never connects, instead of silently leaving the
  // target input-less. That silent miss is exactly what made `source → filter`
  // run with "no input data" in the validation pass.
  for (let attempt = 1; attempt <= 5; attempt++) {
    const s = await src.boundingBox();
    const t = await tgt.boundingBox();
    if (!s || !t) throw new Error(`addEdge: handles not found (${sourceId} -> ${targetId})`);
    const sx = s.x + s.width / 2, sy = s.y + s.height / 2;
    const tx = t.x + t.width / 2, ty = t.y + t.height / 2;
    await page.mouse.move(sx, sy);
    await page.mouse.down();
    // Nudge + pause so react-flow registers the drag start and enters
    // "connecting" mode BEFORE the pointer travels to the target. Omitting
    // this is the most common reason the drag is silently dropped under load.
    await page.mouse.move(sx + 4, sy + 4, { steps: 2 });
    await page.waitForTimeout(60);
    await page.mouse.move((sx + tx) / 2, (sy + ty) / 2, { steps: 12 });
    await page.mouse.move(tx, ty, { steps: 12 });
    await page.mouse.move(tx, ty, { steps: 3 }); // settle on target
    await page.waitForTimeout(60);
    await page.mouse.up();
    const landed = await page
      .waitForFunction((n) => document.querySelectorAll('.react-flow__edge').length > n, before, { timeout: 4_000 })
      .then(() => true)
      .catch(() => false);
    if (landed) return;
    await page.waitForTimeout(250 * attempt); // linear backoff between attempts
  }
  throw new Error(`addEdge: edge ${sourceId} -> ${targetId} did not connect after 5 attempts`);
}

/** Open a node's config modal; waits for the config body. The app opens config
 *  on double-click (which selects the node AND dispatches fpulse-node-opened);
 *  we also fire the event directly as a belt-and-suspenders fallback. */
export async function openConfig(page: Page, id: string) {
  // Select the node (synthetic mousedown+click → app's setSelectedNode) then
  // fire the open event the ConfigPanel listens for. Mirrors the sequence that
  // reliably opens the modal; react-flow swallows Playwright's native dblclick.
  await page.evaluate((nid) => {
    const el = document.querySelector(`.react-flow__node[data-id="${nid}"]`);
    if (el) {
      el.dispatchEvent(new MouseEvent('mousedown', { bubbles: true }));
      el.dispatchEvent(new MouseEvent('mouseup', { bubbles: true }));
      el.dispatchEvent(new MouseEvent('click', { bubbles: true }));
    }
    window.dispatchEvent(new CustomEvent('fpulse-node-opened', { detail: { id: nid } }));
  }, id);
  await page.waitForSelector('[data-fpulse-config-body]', { timeout: 10_000 });
}

/** Close the config modal (Escape clears selection → modal closes). */
export async function closeConfig(page: Page) {
  if (await page.locator('[data-fpulse-config-body]').count()) {
    await page.keyboard.press('Escape');
    await page.locator('[data-fpulse-config-body]').first()
      .waitFor({ state: 'detached', timeout: 5_000 }).catch(() => { /* best-effort */ });
  }
}

export function configBody(page: Page) {
  return page.locator('[data-fpulse-config-body]');
}

/** Assert the config rendered cleanly (no React error boundary). */
export async function assertNoErrorBoundary(page: Page) {
  await expect(
    page.getByText(/Something went wrong|component (crashed|error)|ErrorBoundary/i),
  ).toHaveCount(0);
}

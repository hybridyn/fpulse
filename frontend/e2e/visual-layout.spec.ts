import { test, expect } from '@playwright/test';

// Uses the existing authenticated E2E fixture; does not run or save pipelines.
for (const width of [1920, 1440, 1280, 1024, 740, 390]) {
  test(`editor layout at ${width}px`, async ({ page }) => {
    await page.setViewportSize({ width, height: 900 });
    await page.goto('/#editor');
    const toolbar = page.getByTestId('editor-toolbar');
    await expect(toolbar).toBeVisible();
    const layout = await toolbar.evaluate(element => {
      const boxes = Array.from(element.children).map(child => child.getBoundingClientRect());
      return boxes.flatMap((a, i) => boxes.slice(i + 1).map(b =>
        Math.min(a.right, b.right) - Math.max(a.left, b.left) > 1 &&
        Math.min(a.bottom, b.bottom) - Math.max(a.top, b.top) > 1));
    });
    expect(layout.every(overlap => !overlap)).toBe(true);
    const centerOffset = await toolbar.evaluate(element => {
      const header = element.getBoundingClientRect();
      const tabs = element.querySelector('.editor-toolbar-tabs')!.getBoundingClientRect();
      return Math.abs((header.left + header.right - tabs.left - tabs.right) / 2);
    });
    expect(centerOffset).toBeLessThan(1);
    const canvas = await page.getByTestId('editor-canvas-column').boundingBox();
    expect(canvas!.width).toBeGreaterThanOrEqual(width < 640 ? width - 80 : 560);
    if (width < 1280) {
      await page.getByRole('button', { name: 'Expand assistant panel' }).click();
      const after = await page.getByTestId('editor-canvas-column').boundingBox();
      expect(after!.width).toBe(canvas!.width);
      await page.keyboard.press('Escape');
      await expect(page.getByRole('button', { name: 'Expand assistant panel' })).toBeVisible();
    }
    await test.info().attach(`editor-${width}`, { body: await page.screenshot(), contentType: 'image/png' });
  });
}

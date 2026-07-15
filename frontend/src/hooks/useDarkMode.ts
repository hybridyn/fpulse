/**
 * useDarkMode — DARK MODE IS REMOVED in OSS Free.
 *
 * 2026-05-19 (OSS-5 of PAGE_BY_PAGE_AUDIT.md): the previous implementation
 * subscribed to `<html class="dark">` mutations via a MutationObserver
 * installed in every consumer. main.tsx force-removes the dark class on
 * every boot, so the observer never fired with a true value — but each
 * of the 19 consumers paid for its own observer subscription.
 *
 * The hook now returns `false` unconditionally. All consumers compile
 * unchanged; their `dark ? ...A : ...B` branches always pick the light
 * (`...B`) side and the dead `...A` branch is GC'd by the bundler if
 * it's a string literal, or simply never rendered if it's JSX.
 *
 * The `dark:` Tailwind utility classes have since been removed — none remain
 * in the codebase. What's left is the inert dark *infrastructure*: this hook
 * (always false), the `dark` props threaded through components, and the
 * `dark ? A : B` ternaries that always resolve to the light branch. Removing
 * that infrastructure is a separate, larger refactor (it touches every
 * consumer) and isn't necessary for correctness.
 *
 * If you arrived here looking to RE-enable dark mode:
 *   1. Restore the MutationObserver body (git history has the
 *      previous version).
 *   2. Remove the kill-switch in `main.tsx:6-8`.
 *   3. Re-add the GlobalSearch "Toggle Dark Mode" entry (P2 #11
 *      stripped it).
 *   4. Build a real theme picker UI; do not rely on localStorage as
 *      the source of truth (the previous design did exactly that and
 *      drifted from the kill-switch).
 */
export function useDarkMode(): boolean {
  return false;
}

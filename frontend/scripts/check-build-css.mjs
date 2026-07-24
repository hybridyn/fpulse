// Build guard — fail the build if the production CSS is missing Tailwind's
// utilities/base. This catches the broken-build signature that renders the app
// UNSTYLED: a too-small index-*.css with the @tailwind output stripped, which
// happens when Tailwind's `content` globs resolve to the wrong directory (e.g.
// `vite build <path>` invoked from the wrong cwd) and match zero source files,
// so the JIT emits almost no utilities. Runs after `vite build` in the `build`
// script and in CI.
//
// The known-good production build is ~175 KB with the full utility set. The
// broken build that slipped through once was ~27 KB — small enough to be
// obviously wrong, yet it still contained a handful of utilities, so a floor
// alone is not enough. This guard checks THREE things: a hard byte floor, a set
// of utilities the app actually depends on (layout + shape + sizing, not just
// one), and Preflight's base reset.
import { readFileSync } from 'node:fs'
import { dirname, join } from 'node:path'
import process from 'node:process'
import { fileURLToPath } from 'node:url'

// Resolve dist relative to THIS file (frontend/scripts/ → frontend/dist) so the
// guard works no matter which working directory it is invoked from — the very
// failure mode it exists to catch is a build run from the wrong cwd.
const dist = join(dirname(fileURLToPath(import.meta.url)), '..', 'dist')

let html
try {
  html = readFileSync(join(dist, 'index.html'), 'utf8')
} catch {
  console.error('check-build-css: FAIL — dist/index.html not found. Run `vite build` first.')
  process.exit(1)
}

const hrefs = [...html.matchAll(/href="([^"]+\.css)"/g)].map((m) => m[1])
if (hrefs.length === 0) {
  console.error('check-build-css: FAIL — no <link rel="stylesheet"> in dist/index.html.')
  process.exit(1)
}

// A real Tailwind build of this app is ~175 KB. A build with the content globs
// pointed at nothing came out ~27 KB. 60 KB sits well clear of the broken build
// and still leaves generous headroom below the good one.
const MIN_BYTES = 60_000

// Layout, shape, and sizing utilities the login/app shell all rely on. The
// broken build was missing `.flex`, `.rounded-lg`, and `.min-h-screen` while
// still carrying a few colour utilities — so require a representative spread,
// not any single class.
const REQUIRED_UTILITIES = [
  /\.flex\{/,
  /\.grid\{/,
  /\.rounded-lg\{/,
  /\.min-h-screen\{/,
  /--tw-/,
]
const PREFLIGHT = /box-sizing:\s*border-box/

let ok = false
const report = []
for (const href of hrefs) {
  const css = readFileSync(join(dist, href.replace(/^\//, '')), 'utf8')
  const missing = REQUIRED_UTILITIES.filter((re) => !re.test(css)).map((re) => re.source)
  const hasPreflight = PREFLIGHT.test(css)
  report.push(
    `  ${href} — ${css.length}B · missing=[${missing.join(', ') || 'none'}] · preflight=${hasPreflight}`,
  )
  if (missing.length === 0 && hasPreflight && css.length >= MIN_BYTES) ok = true
}

if (!ok) {
  console.error(
    'check-build-css: FAIL — the app stylesheet is missing Tailwind utilities/base.\n' +
      'This is the broken-build signature that renders the app UNSTYLED. The usual\n' +
      "cause is Tailwind's content globs resolving to the wrong directory — build with\n" +
      '`npm --prefix frontend run build` (or from inside frontend/), never `vite build\n' +
      '<path>` from another cwd. Do NOT ship this dist.',
  )
  report.forEach((line) => console.error(line))
  process.exit(1)
}

console.log('check-build-css: OK — Tailwind utilities + base present.')
report.forEach((line) => console.log(line))

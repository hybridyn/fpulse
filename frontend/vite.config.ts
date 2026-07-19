import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';
import { readFileSync, existsSync } from 'fs';
import { resolve } from 'path';

// Port resolution (v2 of the permanent port-conflict fix, 2026-06-06).
//
// Precedence:
//   1. .fpulse/runtime/instance.json   <- written by start.ps1 and is the
//                                         single source of truth when the
//                                         launcher is in play
//   2. VITE_FRONTEND_PORT / VITE_BACKEND_PORT env vars   <- developer
//                                         escape hatch when running
//                                         `npm run dev` directly without
//                                         the launcher
//   3. Hard defaults 5174 / 8001       <- baseline
//
// Why the runtime file exists: hard-coded ports collide (with Postman,
// with another project's Vite, with a previous F-Pulse orphan, ...).
// v1 of the fix offered to auto-kill the conflicting process based on
// command-line matching, which produced false positives (killing an
// unrelated Vite dev server because it happened to have 'vite' in its
// cmdline). v2 instead auto-picks a free port pair, writes the chosen
// pair to .fpulse/runtime/instance.json, and Vite reads from that file
// so the frontend's listen port and its backend proxy target always
// stay in lockstep.
// Is a PID still running? `process.kill(pid, 0)` doesn't actually signal —
// it just probes existence: it returns on a live PID, throws ESRCH when the
// process is gone, and throws EPERM when it exists but we can't signal it
// (still alive). Used to reject a STALE runtime file whose backend already
// died — otherwise Vite proxies /api to a dead port and the whole app looks
// broken even though a live backend is sitting on the default port.
function processAlive(pid: number): boolean {
  if (!Number.isFinite(pid) || pid <= 0) return false;
  try {
    process.kill(pid, 0);
    return true;
  } catch (e: unknown) {
    return (e as NodeJS.ErrnoException)?.code === 'EPERM';
  }
}

function resolvePorts(): { frontend: number; backend: number } {
  const defaults = { frontend: 5174, backend: 8001 };
  // 1. Try the runtime file (sits at <repo-root>/.fpulse/runtime/instance.json,
  //    one level above this config which lives in <repo-root>/frontend/).
  //    ONLY trust it when its backend process is still alive — a stale file
  //    from a previous (now-dead) launch would otherwise point the proxy at a
  //    port nothing is listening on. When stale, fall through to env/defaults,
  //    which the launcher sets correctly for the *current* run.
  try {
    const runtimePath = resolve(__dirname, '..', '.fpulse', 'runtime', 'instance.json');
    if (existsSync(runtimePath)) {
      const raw = readFileSync(runtimePath, 'utf8');
      const inst = JSON.parse(raw);
      const fe = Number(inst.frontend_port);
      const be = Number(inst.backend_port);
      const live = processAlive(Number(inst.backend_pid)) || processAlive(Number(inst.frontend_pid));
      if (fe > 0 && be > 0 && live) {
        return { frontend: fe, backend: be };
      }
      if (fe > 0 && be > 0 && !live) {
        // eslint-disable-next-line no-console
        console.warn(
          `[vite] ignoring stale .fpulse/runtime/instance.json (backend pid ` +
          `${inst.backend_pid} not running); using env/defaults instead. ` +
          `Run \`fpulse doctor --repair\` to clear it.`,
        );
      }
    }
  } catch {
    // Fall through to env / defaults silently.
  }
  // 2. Fall back to env vars (developer manual flow / current launcher run).
  const fe = Number(process.env.VITE_FRONTEND_PORT) || defaults.frontend;
  const be = Number(process.env.VITE_BACKEND_PORT)  || defaults.backend;
  return { frontend: fe, backend: be };
}

const { frontend: FRONTEND_PORT, backend: BACKEND_PORT } = resolvePorts();
const BACKEND_URL = `http://localhost:${BACKEND_PORT}`;

export default defineConfig({
  plugins: [react()],
  build: {
    // 2026-06-08 (reviewer rec #3): the app already route-splits via
    // React.lazy, but with no manualChunks every node_modules dep
    // collapsed into the single entry chunk (~756 KB, over Vite's 500 KB
    // warning). The canvas (@xyflow/react) is the heaviest dep and is
    // only needed on the builder route, so isolating it — plus the React
    // runtime and the icon set — into their own long-cached vendor chunks
    // shrinks the initial entry payload and lets the browser cache the
    // big, rarely-changing libraries across app deploys.
    chunkSizeWarningLimit: 700,
    rollupOptions: {
      output: {
        manualChunks(id) {
          if (!id.includes('node_modules')) return undefined;
          // ReactFlow canvas — large, builder-route only.
          if (id.includes('@xyflow') || id.includes('d3-')) return 'vendor-flow';
          // Icon set — wide surface, changes rarely.
          if (id.includes('lucide-react')) return 'vendor-icons';
          // React runtime — shared by every route, very stable.
          if (
            id.includes('/react/') ||
            id.includes('/react-dom/') ||
            id.includes('/scheduler/') ||
            id.includes('react-reconciler')
          ) {
            return 'vendor-react';
          }
          // Everything else from node_modules.
          return 'vendor';
        },
      },
    },
  },
  server: {
    // Bind to all interfaces so both 'localhost' and 'fpulse.local' resolve.
    // An external Electron shell's WebContentsView connects to localhost:5174 - keeping
    // host: 'fpulse.local' alone made that connection refuse. true === '0.0.0.0' in Vite.
    host: true,
    port: FRONTEND_PORT,
    // 2026-05-29: strictPort fails loudly if the chosen port is already
    // bound instead of silently drifting to +1/+2/... With v2 of the
    // port-conflict fix, the launcher already verified this port was
    // free before writing it to the runtime file, so EADDRINUSE here
    // would indicate a true race (something grabbed the port between
    // verification and bind). strictPort surfaces that race instead of
    // letting Vite drift to a port the backend proxy doesn't know about.
    strictPort: true,
    proxy: {
      '/api': {
        target: BACKEND_URL,
        changeOrigin: true,
      },
      '/ws': {
        target: BACKEND_URL,
        changeOrigin: true,
        ws: true,
      },
    },
  },
});

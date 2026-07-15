/**
 * PanelErrorBoundary — small, contained error boundary intended to wrap a
 * single panel inside the editor shell (ModulesPanel / ChatPanel /
 * ConfigPanel) so that one panel crash doesn't take down the whole app.
 *
 * 2026-05-19 (P1 #13 of PAGE_BY_PAGE_AUDIT.md): the only existing
 * `<ErrorBoundary>` wraps the entire `<App />` body, so any uncaught render
 * exception inside a panel collapses the whole UI to the "Reload Page /
 * Go to Dashboard / Copy Error" fallback. This component's fallback is
 * inline (sits inside the failed panel's column) and offers a Retry that
 * resets the boundary in place — the rest of the shell keeps running.
 *
 * The shared `<ErrorBoundary>` deliberately mounts at the App level (it's
 * the safety net for catastrophic React crashes); per-panel boundaries
 * are the finer-grained layer that catches localised bugs.
 */

import { Component, type ErrorInfo, type ReactNode } from 'react';

interface Props {
  /** Human-readable panel name shown in the fallback ("Config panel", "Copilot dock"). */
  name: string;
  children: ReactNode;
}

interface State {
  hasError: boolean;
  error: Error | null;
}

export default class PanelErrorBoundary extends Component<Props, State> {
  state: State = { hasError: false, error: null };

  static getDerivedStateFromError(error: Error): Partial<State> {
    return { hasError: true, error };
  }

  componentDidCatch(error: Error, errorInfo: ErrorInfo) {
    // eslint-disable-next-line no-console
    console.error(`[F-Pulse PanelErrorBoundary] ${this.props.name}`, error, errorInfo);
  }

  reset = () => this.setState({ hasError: false, error: null });

  render(): ReactNode {
    if (!this.state.hasError) return this.props.children;
    const message = this.state.error?.message || 'Unknown render error';
    return (
      <div
        role="alert"
        className="h-full min-h-[200px] flex items-center justify-center p-6 bg-red-50 border border-red-200 rounded-lg"
      >
        <div className="max-w-sm text-center">
          <div className="mx-auto mb-3 w-10 h-10 rounded-full bg-red-100 flex items-center justify-center">
            <svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="#dc2626" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
              <circle cx="12" cy="12" r="10" />
              <line x1="12" y1="8" x2="12" y2="12" />
              <line x1="12" y1="16" x2="12.01" y2="16" />
            </svg>
          </div>
          <p className="text-sm font-bold text-red-800">{this.props.name} crashed</p>
          <p className="text-xs text-red-700 mt-1 break-words">{message}</p>
          <p className="text-[11px] text-red-600/80 mt-2">
            The rest of the app kept running. Retry to re-render this panel; the bug has been logged to the browser console.
          </p>
          <div className="flex items-center justify-center gap-2 mt-3">
            <button
              onClick={this.reset}
              className="px-3 py-1 text-xs font-semibold rounded-md bg-white text-red-700 border border-red-300 hover:bg-red-100"
            >
              Retry panel
            </button>
            <button
              onClick={() => window.location.reload()}
              className="px-3 py-1 text-xs font-semibold rounded-md bg-red-100 text-red-700 border border-red-300 hover:bg-red-200"
            >
              Reload page
            </button>
          </div>
        </div>
      </div>
    );
  }
}

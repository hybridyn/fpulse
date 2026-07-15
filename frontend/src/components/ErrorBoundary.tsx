import { Component, type ErrorInfo, type ReactNode } from 'react';

interface Props {
  children: ReactNode;
  fallback?: ReactNode;
}

interface State {
  hasError: boolean;
  error: Error | null;
  errorInfo: ErrorInfo | null;
}

class ErrorBoundary extends Component<Props, State> {
  constructor(props: Props) {
    super(props);
    this.state = { hasError: false, error: null, errorInfo: null };
  }

  static getDerivedStateFromError(error: Error): Partial<State> {
    return { hasError: true, error };
  }

  componentDidCatch(error: Error, errorInfo: ErrorInfo) {
    this.setState({ error, errorInfo });
    console.error('[F-Pulse ErrorBoundary]', error, errorInfo);
  }

  handleReload = () => {
    window.location.reload();
  };

  handleGoToDashboard = () => {
    window.location.hash = 'dashboard';
    window.location.reload();
  };

  handleCopyError = () => {
    const { error, errorInfo } = this.state;
    const details = [
      `Error: ${error?.message || 'Unknown error'}`,
      '',
      `Stack: ${error?.stack || 'No stack trace'}`,
      '',
      `Component Stack: ${errorInfo?.componentStack || 'No component stack'}`,
    ].join('\n');
    navigator.clipboard.writeText(details);
  };

  render() {
    if (this.state.hasError) {
      if (this.props.fallback) {
        return this.props.fallback;
      }

      const { error, errorInfo } = this.state;

      return (
        <div className="min-h-screen w-full flex items-center justify-center bg-gray-50 p-4">
          <div className="max-w-lg w-full bg-white rounded-xl shadow-lg p-8 text-center space-y-6">
            <div className="flex justify-center">
              <div className="w-16 h-16 rounded-2xl bg-white overflow-hidden">
                <img src="/fpulse-logo-mark.png" alt="F-Pulse OSS" className="w-full h-full object-cover" />
              </div>
            </div>

            {/* Heading */}
            <div>
              <h1 className="text-xl font-bold text-gray-900">
                Something went wrong
              </h1>
              <p className="mt-1 text-sm text-gray-500">
                F-Pulse encountered an unexpected error.
              </p>
            </div>

            {/* Error details (collapsible) */}
            <details className="text-left">
              <summary className="cursor-pointer text-sm font-medium text-gray-600 hover:text-gray-800 select-none">
                Error Details
              </summary>
              <div className="mt-2 space-y-3">
                <code className="block text-xs bg-gray-100 text-red-600 rounded-lg p-3 break-all whitespace-pre-wrap">
                  {error?.message || 'Unknown error'}
                </code>
                {errorInfo?.componentStack && (
                  <pre className="text-xs bg-gray-100 text-gray-600 rounded-lg p-3 max-h-48 overflow-auto whitespace-pre-wrap">
                    {errorInfo.componentStack}
                  </pre>
                )}
              </div>
            </details>

            {/* Action buttons */}
            <div className="flex flex-col gap-2">
              <button
                onClick={this.handleReload}
                className="w-full px-4 py-2.5 bg-amber-500 hover:bg-amber-600 text-white font-medium rounded-lg transition-colors text-sm"
              >
                Reload Page
              </button>
              <button
                onClick={this.handleGoToDashboard}
                className="w-full px-4 py-2.5 border border-gray-300 text-gray-700 hover:bg-gray-50 font-medium rounded-lg transition-colors text-sm"
              >
                Go to Dashboard
              </button>
              <button
                onClick={this.handleCopyError}
                className="w-full px-4 py-2 text-gray-500 hover:text-gray-700 text-xs font-medium transition-colors"
              >
                Copy Error Details
              </button>
            </div>
          </div>
        </div>
      );
    }

    return this.props.children;
  }
}

export default ErrorBoundary;

import { Component, type ErrorInfo, type ReactNode } from "react";

interface Props {
  children: ReactNode;
  /** Optional custom fallback. Receives the error and a reset() to retry. */
  fallback?: (error: Error, reset: () => void) => ReactNode;
  /** Label for the default fallback heading (e.g. "page", "dashboard"). */
  scope?: string;
}

interface State {
  error: Error | null;
}

/**
 * Catches render-time exceptions so one bad component (e.g. a `.map` on a field
 * the API stopped sending) can't unmount the whole SPA to a blank screen. On a
 * live trading dashboard that would black out the operator's view of open
 * positions, so a contained, recoverable fallback is important.
 *
 * Place one around the routed `<Outlet/>` (keyed by route, so navigating away
 * clears a stuck error while the nav shell stays alive) and one at the top
 * level as a last resort.
 */
export class ErrorBoundary extends Component<Props, State> {
  state: State = { error: null };

  static getDerivedStateFromError(error: Error): State {
    return { error };
  }

  componentDidCatch(error: Error, info: ErrorInfo) {
    // eslint-disable-next-line no-console
    console.error("ErrorBoundary caught an error:", error, info.componentStack);
  }

  reset = () => this.setState({ error: null });

  render() {
    const { error } = this.state;
    if (error) {
      if (this.props.fallback) return this.props.fallback(error, this.reset);
      return <DefaultFallback error={error} reset={this.reset} scope={this.props.scope} />;
    }
    return this.props.children;
  }
}

function DefaultFallback({
  error,
  reset,
  scope,
}: {
  error: Error;
  reset: () => void;
  scope?: string;
}) {
  return (
    <div
      role="alert"
      className="m-4 rounded-lg border border-red-500/40 bg-red-950/30 p-6 text-red-100"
    >
      <h2 className="text-lg font-semibold">
        Something went wrong{scope ? ` in the ${scope}` : ""}.
      </h2>
      <p className="mt-1 text-sm text-red-200/80">
        The view hit an unexpected error. Your data is safe — this only affects
        what's shown here.
      </p>
      <pre className="mt-3 max-h-40 overflow-auto whitespace-pre-wrap rounded bg-black/30 p-2 text-xs text-red-300/90">
        {error.message}
      </pre>
      <div className="mt-4 flex gap-2">
        <button
          onClick={reset}
          className="rounded bg-red-600/80 px-3 py-1.5 text-sm font-medium text-white hover:bg-red-600"
        >
          Try again
        </button>
        <button
          onClick={() => window.location.reload()}
          className="rounded border border-red-400/40 px-3 py-1.5 text-sm font-medium text-red-100 hover:bg-red-900/30"
        >
          Reload page
        </button>
      </div>
    </div>
  );
}

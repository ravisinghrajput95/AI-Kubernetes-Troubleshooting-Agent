import { Component, type ErrorInfo, type ReactNode } from "react";

/**
 * Keeps one bad payload from blanking the application.
 *
 * The backend types `investigation` and `diagnosis` as `dict[str, Any]`, so the
 * TypeScript interfaces are the only contract and nothing on the server side
 * enforces them. Before investigations were addressable that mattered less —
 * you only ever saw a result you had just produced. Now any historical id can
 * be opened directly, including reports written by older versions, so a
 * missing field has to degrade to a message rather than a white screen.
 */
export class ErrorBoundary extends Component<
  { children: ReactNode },
  { error: Error | null }
> {
  state: { error: Error | null } = { error: null };

  static getDerivedStateFromError(error: Error) {
    return { error };
  }

  componentDidCatch(error: Error, info: ErrorInfo) {
    console.error("Unhandled error while rendering", error, info.componentStack);
  }

  render() {
    if (!this.state.error) {
      return this.props.children;
    }

    return (
      <div className="mx-auto max-w-measure px-6 py-16 text-center">
        <h1 className="text-h1">This page could not be displayed</h1>
        <p className="mt-3 text-sm leading-6 text-ink-2">
          The data behind it was not in the shape the console expected. The
          investigation itself is unaffected — its report is still downloadable.
        </p>
        <p className="mt-4 font-mono text-sm text-ink-3">{this.state.error.message}</p>
        <button
          type="button"
          onClick={() => this.setState({ error: null })}
          className="mt-6 rounded-md border border-line bg-raised px-3 py-2 text-sm transition-colors duration-fast hover:border-ink-3 focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-info"
        >
          Try again
        </button>
      </div>
    );
  }
}

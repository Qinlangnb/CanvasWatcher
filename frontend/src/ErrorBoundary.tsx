import { Component, type ErrorInfo, type ReactNode } from "react";

export class ErrorBoundary extends Component<
  { children: ReactNode },
  { error: string | null }
> {
  state: { error: string | null } = { error: null };
  static getDerivedStateFromError(error: Error) {
    return { error: error.message || "The page could not be rendered." };
  }
  componentDidCatch(error: Error, info: ErrorInfo) {
    console.error("Academic Watcher render error", error, info.componentStack);
  }
  render() {
    if (!this.state.error) return this.props.children;
    return (
      <main className="fatal-recovery" role="alert">
        <p className="eyebrow">DISPLAY RECOVERY</p>
        <h1>This view hit an unexpected error</h1>
        <p>{this.state.error}</p>
        <button
          className="primary"
          onClick={() => this.setState({ error: null })}
        >
          Try again
        </button>
        <button className="secondary" onClick={() => location.reload()}>
          Reload Academic Watcher
        </button>
      </main>
    );
  }
}

import React from "react";
import ReactDOM from "react-dom/client";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import App from "./App";
import { ErrorBoundary } from "./ErrorBoundary";
import "./styles.css";
import "./v051.css";
import "./v052.css";
import "./v053.css";
import "./v054.css";
import "./v060.css";
import "./credentials.css";
import "./interaction.css";
import "./v071.css";
import "./responsive.css";

const client = new QueryClient({
  defaultOptions: { queries: { refetchInterval: 60_000, retry: 1 } },
});
ReactDOM.createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <QueryClientProvider client={client}>
      <ErrorBoundary>
        <App />
      </ErrorBoundary>
    </QueryClientProvider>
  </React.StrictMode>,
);

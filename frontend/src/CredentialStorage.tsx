import { useQuery } from "@tanstack/react-query";
import { api } from "./api";

export function useCredentialStorage() {
  return useQuery({
    queryKey: ["credential-storage"],
    queryFn: () => api<{ mode: "memory" | "server_encrypted" }>("/api/settings/credential-storage"),
    staleTime: 60_000,
  });
}

export function CredentialStorageNotice() {
  const query = useCredentialStorage();
  return <p className="form-message" role="status">{query.isError
    ? "Unable to confirm credential storage mode. Reload to check."
    : !query.data ? "Checking credential storage…"
    : query.data.mode === "server_encrypted"
      ? "Server storage enabled: credentials are encrypted and restored after restart. Expired or revoked sessions may still require sign-in. Removing a credential also removes its saved copy."
      : "Memory-only credentials: sign in again after a backend restart."}</p>;
}

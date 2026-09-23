const messages: Record<string, string> = {
  AUTH_LOGIN_TIMEOUT: "Sign-in timed out. No credentials were saved. Please retry.",
  TIMED_OUT: "Sign-in timed out. No credentials were saved. Please retry.",
  AUTH_USER_CANCELLED: "Sign-in cancelled. Your previous credentials were not changed.",
  CANCELLED: "Sign-in cancelled. Your previous credentials were not changed.",
  CANVAS_ACCOUNT_MISMATCH: "This browser is signed in to a different Canvas account. Switch to the account already linked to this source and retry.",
  AUTH_BROKER_UNAVAILABLE: "Start the Local Auth Broker on this computer, then retry.",
  AUTH_LOGIN_FAILED: "Sign-in could not be verified. Check the official browser window and retry; previous credentials were preserved.",
};
export function brokerMessage(reason: string | null | undefined): string {
  return messages[reason ?? ""] ?? "Sign-in could not be completed. Check the Local Auth Broker and retry.";
}

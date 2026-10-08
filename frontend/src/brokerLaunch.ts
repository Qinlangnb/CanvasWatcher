export const BROKER_LAUNCH_URI = "academicwatcher://start";
export const BROKER_START_TIMEOUT_MS = 60_000;
export const BROKER_START_POLL_MS = 1_500;

export function brokerReady(health: { protocol_version?: number; backend_ready?: boolean } | undefined) {
  return health?.protocol_version === 1 && health.backend_ready === true;
}

// URI is fixed; no origin, credential, token or command is taken from the page.
export function launchBroker() {
  window.location.href = BROKER_LAUNCH_URI;
}

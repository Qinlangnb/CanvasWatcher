# Local Auth Broker (protocol v1)

Host component for Academic Watcher V0.7.1. It is not a Docker service and must run on the same desktop as the user. See the [bilingual deployment guide](../DEPLOYMENT.md) for setup from the repository root.

Windows setup from the project directory:

```powershell
py -3.12 -m venv auth-broker/.venv
auth-broker/.venv/Scripts/python -m pip install -e ./auth-broker
auth-broker/.venv/Scripts/python -m playwright install chromium
./start-auth-broker.ps1
```

An installed package can also run with `python -m academic_watcher_auth_broker`; no checkout path is built into the protocol. Port conflicts fail explicitly. Bind is always 127.0.0.1, port 8765 by default. Configure AW_BROKER_PORT, AW_BROKER_BACKEND (an explicit loopback HTTP origin/port), and AW_BROKER_ORIGINS (comma-separated exact frontend origins, no wildcard). If changing the port, set backend AUTH_BROKER_PORT to the same value.

Frontend origins may be explicit HTTPS or HTTP origins (no paths, userinfo or wildcard). The backend callback and listen address remain loopback-only. Exact-origin Private Network Access preflights are supported; browsers may additionally request local-network permission or disallow an insecure remote frontend. Approve an expected browser prompt yourself, use HTTPS or localhost, and never disable browser security to bypass it. The PowerShell launcher accepts `-Origins` for this configuration.

The broker opens a visible, dedicated Chromium profile for official sign-in. Complete SSO/MFA yourself; Academic Watcher does not automate password entry. Do not provide passwords or cookies in chat. Profiles live under OS app data, not the source tree. Clear Canvas browser session removes that dedicated profile and backend browser credential while retaining academic data and other credential methods. Stop the broker with Ctrl+C.

The backend issues a 240-second, provider/instance/profile-scoped single-use ticket. The browser passes that ticket to the broker, which exchanges it directly with the pinned backend for a separate completion capability. Provider cookies move broker → backend only, then are independently verified. UI receives status only. Do not enable HTTP debug/access logging, export browser storage_state files, copy profile directories or expose these loopback services through a public tunnel.

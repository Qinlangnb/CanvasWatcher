# Local Auth Broker (protocol v1)

Host component for Academic Watcher v0.7.2. It is not a Docker service and must run on the same desktop as the browser. See the [bilingual deployment guide](../DEPLOYMENT.md) for setup from the repository root.

## Windows one-click launcher

From the cloned repository directory, install the Broker dependencies and Chromium and register the launcher once:

```powershell
cd "C:\path\to\CanvasWatcher"
py -3.12 -m venv auth-broker/.venv
auth-broker/.venv/Scripts/python.exe -m pip install -e ./auth-broker
auth-broker/.venv/Scripts/python.exe -m playwright install chromium
auth-broker/.venv/Scripts/python.exe -m academic_watcher_auth_broker.install_launcher install
```

After registration, use Settings → Sources → **Start broker**, then **Check broker**. The webpage waits up to 60 seconds for Broker and callback health after `academicwatcher://start` activation. Defaults allow `http://localhost:8080` and `http://127.0.0.1:8080`; Broker port is fixed at `127.0.0.1:8765`, default backend `http://127.0.0.1:8000`. The install command accepts `--backend` (loopback HTTP origin) and `--origins` (comma-separated exact origins) for custom addresses. The browser may ask to open an external application or grant local-network access; confirm expected prompts. Each computer needs its own installation; a phone cannot launch a Windows Broker.

Stop the Broker or remove the launcher registration with the same module (profiles and academic data are retained):

```powershell
auth-broker/.venv/Scripts/python.exe -m academic_watcher_auth_broker.install_launcher stop
auth-broker/.venv/Scripts/python.exe -m academic_watcher_auth_broker.install_launcher uninstall
```

## Manual mode and configuration

An installed package can run manually with `python -m academic_watcher_auth_broker`; no checkout path is built into the protocol. For example, on Windows:

```powershell
$env:AW_BROKER_BACKEND = 'http://127.0.0.1:8000'
$env:AW_BROKER_ORIGINS = 'http://localhost:8080,http://127.0.0.1:8080'
auth-broker/.venv/Scripts/python.exe -m academic_watcher_auth_broker
```

Manual mode uses environment variables, not CLI address flags. It binds to `127.0.0.1`; port8765 is the default. Configure `AW_BROKER_PORT`, `AW_BROKER_BACKEND` (loopback HTTP origin) and `AW_BROKER_ORIGINS` (exact origins, no wildcard/path) as needed. If changing the Broker port, set backend `AUTH_BROKER_PORT` to the same value. One-click mode does not support changing the Broker port.

Linux/macOS do not support Windows launcher registration and continue to use manual Python startup:

```sh
python3.12 -m venv auth-broker/.venv
auth-broker/.venv/bin/python -m pip install -e ./auth-broker
auth-broker/.venv/bin/python -m playwright install chromium
auth-broker/.venv/bin/python -m academic_watcher_auth_broker
```

Frontend origins may be explicit HTTPS or HTTP origins (no paths, userinfo or wildcard) in manual mode. The backend callback and listen address remain loopback-only. Exact-origin Private Network Access preflights are supported; browsers may additionally request local-network permission or disallow an insecure remote frontend. Approve an expected browser prompt yourself, use HTTPS or localhost, and never disable browser security to bypass it.

The broker opens a visible, dedicated Chromium profile for official sign-in. Complete SSO/MFA yourself; Academic Watcher does not automate password entry. Do not provide passwords or cookies in chat. Profiles live under OS app data, not the source tree. Clear Canvas browser session removes that dedicated profile and backend browser credential while retaining academic data and other credential methods. In manual mode, stop the broker with Ctrl+C.

The backend issues a 240-second, provider/instance/profile-scoped single-use ticket. The browser passes that ticket to the broker, which exchanges it directly with the pinned backend for a separate completion capability. Provider cookies move broker → backend only, then are independently verified. UI receives status only. Do not enable HTTP debug/access logging, export browser storage_state files, copy profile directories or expose these loopback services through a public tunnel.

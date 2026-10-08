# Deployment / 部署

## 中文

### 前提与安全边界

需要 Docker Desktop 或 Docker Engine + Compose v2。可选浏览器登录功能还需要宿主机 Python 3.12 与 Playwright Chromium。默认端口：前端 `127.0.0.1:8080`、API `127.0.0.1:8000`、Auth Broker `127.0.0.1:8765`；不要直接开放到公网。首次启动后在 Settings → Sources 添加自己的来源；不要把真实凭据写进 Git。

### Windows PowerShell 快速启动

在克隆的仓库目录执行（把路径换成自己的实际路径）：

```powershell
cd "C:\path\to\CanvasWatcher"
Copy-Item .env.example .env
Copy-Item config/courses.example.yaml config/courses.runtime.yaml
Copy-Item config/file_rules.example.yaml config/file_rules.yaml
Copy-Item config/availability.example.yaml config/availability.yaml
docker compose up -d --build
docker compose ps
```

打开 `http://127.0.0.1:8080`；API 健康检查是 `http://127.0.0.1:8000/api/health`。首次运行会创建 `data/academic_watcher.db` 并执行数据库迁移。课程配置可留空，通过界面添加来源。`.env`、`data/` 和运行配置已被 `.gitignore` 排除。升级前请备份这些文件，尤其是 SQLite 数据库；不要用仓库示例覆盖已有配置。

### 可选浏览器登录（Windows 一键启动）

Auth Broker 在有图形桌面的宿主机上运行，不能放入 Docker 容器。先确保 Docker 后端已运行；一键启动器只管理 Broker，不会启动或停止 Docker。每台要运行 Broker 的 Windows 电脑都需在自己的克隆仓库目录安装一次依赖、Chromium 并注册启动器：

```powershell
cd "C:\path\to\CanvasWatcher"
py -3.12 -m venv auth-broker/.venv
./auth-broker/.venv/Scripts/python.exe -m pip install -e ./auth-broker
./auth-broker/.venv/Scripts/python.exe -m playwright install chromium
./auth-broker/.venv/Scripts/python.exe -m academic_watcher_auth_broker.install_launcher install
```

注册后，在网页 Settings → Sources 选择 **Start broker**，再选择 **Check broker**。`academicwatcher://start` 会请求启动本机 Broker，并等待最多 60 秒检查健康状态。默认前端 Origin 为 `http://localhost:8080` 和 `http://127.0.0.1:8080`；Broker 固定使用 `127.0.0.1:8765`，后端固定使用 `http://127.0.0.1:8000`。浏览器可能询问是否打开外部应用，或请求访问本地网络；仅对预期提示确认。其他电脑需在其本机单独安装；手机不能唤起 Windows 电脑上的 Broker。

升级前先停止旧 Broker（旧手动进程按 Ctrl+C；旧一键安装使用其 stop 命令），再更新安装。若相同协议已指向另一份安装，注册器会拒绝覆盖；请先从旧安装执行 uninstall。不要按端口随意杀死其他程序，也不要关闭浏览器安全确认。

停止 Broker 或移除启动器注册（不会删除 Broker profiles 或学习数据）：

```powershell
./auth-broker/.venv/Scripts/python.exe -m academic_watcher_auth_broker.install_launcher stop
./auth-broker/.venv/Scripts/python.exe -m academic_watcher_auth_broker.install_launcher uninstall
```

一键模式的 Broker 端口固定8765。安装命令可追加 `--backend`（仅 loopback HTTP Origin）和 `--origins`（逗号分隔的精确前端 Origins，不允许 wildcard 或路径）来配置自己的地址。要更改 Broker 端口，请使用手动模式并保持后端 `.env` 的 `AUTH_BROKER_PORT` 与 `AW_BROKER_PORT` 一致。Linux/macOS 不支持 Windows 一键注册，仍使用 Python 手动启动：

自定义前端 Origin 时，还需在后端 `.env` 的 `CORS_ORIGINS` 中设置同样的精确地址并重新启动后端；本机安装命令不会代改 Docker 配置。不要使用 wildcard 或直接暴露公网；公开版的进程内凭据在后端重启后需重新导入。

```sh
python3.12 -m venv auth-broker/.venv
auth-broker/.venv/bin/python -m pip install -e ./auth-broker
auth-broker/.venv/bin/python -m playwright install chromium
auth-broker/.venv/bin/python -m academic_watcher_auth_broker
```

登录弹出的官方页面时自行输入密码/MFA，不要导出 Cookie 或浏览器配置档。手动模式的自定义参数与端口见 [`auth-broker/README.md`](auth-broker/README.md)。

### 日常维护

```powershell
docker compose ps
docker compose logs --tail 80 backend
docker compose down
```

`down` 不带 `-v`；不要删除 `data/`。后端重启会清空仅保存在内存中的 PAT、浏览器会话与 AI Key，需在界面重新登录或导入。AI 与 ntfy 均为可选；启用通知服务可用 `docker compose --profile notifications up -d`，但需自行配置 `NTFY_URL` / `NTFY_TOPIC`。PrairieLearn 接入缺少已选课程的真实验收，不能据此假定所有实例可用。

## English

### Prerequisites and security

Install Docker Desktop or Docker Engine with Compose v2. Browser-based sign-in additionally needs host-side Python 3.12 and Playwright Chromium on a graphical desktop. Default loopback ports are UI `127.0.0.1:8080`, API `127.0.0.1:8000`, and Auth Broker `127.0.0.1:8765`. Do not expose them directly to the internet. Add your own sources in Settings → Sources after startup; never commit real credentials.

### Quick start (Windows PowerShell)

Run these commands in your clone, replacing the example path:

```powershell
cd "C:\path\to\CanvasWatcher"
Copy-Item .env.example .env
Copy-Item config/courses.example.yaml config/courses.runtime.yaml
Copy-Item config/file_rules.example.yaml config/file_rules.yaml
Copy-Item config/availability.example.yaml config/availability.yaml
docker compose up -d --build
docker compose ps
```

Open `http://127.0.0.1:8080`; API health is `http://127.0.0.1:8000/api/health`. First startup creates `data/academic_watcher.db` and runs migrations. The course seed can remain empty while you add sources in the UI. `.env`, `data/`, and per-installation configuration are Git-ignored. Back them up before upgrades, especially the SQLite database; never overwrite existing settings with templates.

On Linux/macOS, use `cp` instead of `Copy-Item` for the four setup copies, then run the same `docker compose` commands.

### Optional browser sign-in (Windows one-click start)

The Auth Broker runs on the graphical host, outside Docker. Start the Docker backend first; the launcher manages only the Broker and never starts or stops Docker. On each Windows computer that will run a Broker, install its dependencies and Chromium and register the launcher once from that computer's clone:

```powershell
cd "C:\path\to\CanvasWatcher"
py -3.12 -m venv auth-broker/.venv
./auth-broker/.venv/Scripts/python.exe -m pip install -e ./auth-broker
./auth-broker/.venv/Scripts/python.exe -m playwright install chromium
./auth-broker/.venv/Scripts/python.exe -m academic_watcher_auth_broker.install_launcher install
```

After registration, select **Start broker** and then **Check broker** in Settings → Sources. `academicwatcher://start` requests the local Broker to start and waits up to 60 seconds for its health check. Default frontend origins are `http://localhost:8080` and `http://127.0.0.1:8080`; the one-click Broker port is fixed at `127.0.0.1:8765` and the backend at `http://127.0.0.1:8000`. The browser may ask to open an external application or grant local-network access; confirm only expected prompts. Install separately on each computer; a phone cannot launch the Broker on a Windows PC.

Before upgrading, stop the previous Broker (Ctrl+C for a manual process, or that installation's stop command). Registration refuses to overwrite a handler owned by another installation; uninstall from the previous installation first. Never kill arbitrary port owners or disable browser security confirmations.

Stop the Broker or remove the launcher registration (these commands retain Broker profiles and academic data):

```powershell
./auth-broker/.venv/Scripts/python.exe -m academic_watcher_auth_broker.install_launcher stop
./auth-broker/.venv/Scripts/python.exe -m academic_watcher_auth_broker.install_launcher uninstall
```

The one-click Broker port is fixed at8765. The install command accepts `--backend` (loopback HTTP origin only) and `--origins` (comma-separated exact frontend origins, no wildcard/path) for your own addresses. A different Broker port requires manual mode; keep `.env` `AUTH_BROKER_PORT` in sync with `AW_BROKER_PORT`. Linux/macOS do not support Windows launcher registration; start the Broker manually with Python:

For a custom frontend origin, also set the matching exact addresses in the backend `.env` `CORS_ORIGINS` and restart the backend. The desktop installer does not modify Docker configuration. Never use wildcards or expose the default services publicly; re-import process-memory credentials after a backend restart.

```sh
python3.12 -m venv auth-broker/.venv
auth-broker/.venv/bin/python -m pip install -e ./auth-broker
auth-broker/.venv/bin/python -m playwright install chromium
auth-broker/.venv/bin/python -m academic_watcher_auth_broker
```

Complete passwords/MFA on the official provider page; do not export cookies or browser profiles. See [`auth-broker/README.md`](auth-broker/README.md) for manual options and port configuration.

### Operations

```powershell
docker compose ps
docker compose logs --tail 80 backend
docker compose down
```

Do not use `down -v` or delete `data/`. Backend restarts clear process-memory PATs, browser credentials, and AI keys; re-authenticate or re-import them through the UI. AI and ntfy are optional. For ntfy, run `docker compose --profile notifications up -d` and configure `NTFY_URL` / `NTFY_TOPIC`. PrairieLearn integration has not been validated with an enrolled course, so compatibility with every instance is not guaranteed.

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

### 可选浏览器登录

Auth Broker 运行在有图形桌面的宿主机，不能放入上述 Docker 容器。仍在仓库目录中执行：

```powershell
py -3.12 -m venv auth-broker/.venv
./auth-broker/.venv/Scripts/python.exe -m pip install -e ./auth-broker
./auth-broker/.venv/Scripts/python.exe -m playwright install chromium
powershell -NoProfile -ExecutionPolicy Bypass -File ./start-auth-broker.ps1
```

最后一行是持续运行的进程，保持该 PowerShell 窗口打开；或者按自己的进程管理方式运行。登录弹出的官方页面时自行输入密码/MFA，不要导出 Cookie 或浏览器配置档。停止 Broker 不会删除学习数据。若换端口，要让 `.env` 中的 `AUTH_BROKER_PORT` 与 Broker 端口一致，并配置对应的精确前端 Origin。

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

### Optional browser sign-in

The Auth Broker runs on the graphical host, outside Docker. From the repository directory:

```powershell
py -3.12 -m venv auth-broker/.venv
./auth-broker/.venv/Scripts/python.exe -m pip install -e ./auth-broker
./auth-broker/.venv/Scripts/python.exe -m playwright install chromium
powershell -NoProfile -ExecutionPolicy Bypass -File ./start-auth-broker.ps1
```

Keep the final process running. On Linux/macOS, use `python3.12 -m venv auth-broker/.venv`, `auth-broker/.venv/bin/python` for install/Chromium, and `auth-broker/.venv/bin/python -m academic_watcher_auth_broker` to run it. Complete passwords/MFA on the official provider page; do not export cookies or browser profiles. If you change the Broker port, update `AUTH_BROKER_PORT` in `.env` and configure the exact permitted frontend origins.

### Operations

```powershell
docker compose ps
docker compose logs --tail 80 backend
docker compose down
```

Do not use `down -v` or delete `data/`. Backend restarts clear process-memory PATs, browser credentials, and AI keys; re-authenticate or re-import them through the UI. AI and ntfy are optional. For ntfy, run `docker compose --profile notifications up -d` and configure `NTFY_URL` / `NTFY_TOPIC`. PrairieLearn integration has not been validated with an enrolled course, so compatibility with every instance is not guaranteed.

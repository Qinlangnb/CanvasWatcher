# Changelog / 更新记录

## Unreleased — Azure multi-user preview / Azure 多用户预览

### English

- Added an invitation-only identity gateway with per-user backend processes, SQLite databases, encrypted credentials, file trees, and quotas. The default local Compose deployment remains single-user.
- Brought source synchronization, assignment attachments, page availability, AI summaries, responsive Today cards, and Broker direct-HTTPS support forward from the private development branch.
- Added generic Linux deployment templates. Domain, owner identity, credentials, database, and server-specific configuration must be supplied by each operator. Fresh multi-user first-admin bootstrapping is not automated in this preview.
- Split the repository README into English and Chinese files.

### 中文

- 新增邀请制身份网关，为每位用户隔离后端进程、SQLite、加密凭据、文件树和配额；默认本地 Compose 仍为单用户。
- 同步私有开发分支中的来源采集、作业附件、Pages 可用性、AI 概括、Today 响应式布局及 Broker 直连 HTTPS 支持。
- 加入通用 Linux 部署模板；域名、管理员身份、凭据、数据库与服务器专属配置须由部署者提供。预览版尚未自动化全新多用户实例的首位管理员初始化。
- 将仓库 README 拆为英文和中文两份。

## v0.7.2

### 中文

- Windows 可一次注册本机 Auth Broker 启动器，之后从 Settings → Sources 选择 Start broker；`academicwatcher://start` 最多等待 60 秒检查 Broker 健康状态。网页不再提供安装说明。
- Broker 启动保持单实例，并只管理其自身创建的子进程；Sources 可检查 Broker health，并修复瞬时刷新响应处理。
- 一键模式默认允许 `http://localhost:8080` 与 `http://127.0.0.1:8080`，使用固定 Broker `127.0.0.1:8765` 和后端 `http://127.0.0.1:8000`。启动器只管理 Broker，不控制 Docker。

### English

- Windows can register the local Auth Broker launcher once, then select Start broker in Settings → Sources. `academicwatcher://start` waits up to 60 seconds for Broker health. Setup instructions are removed from the web UI.
- Broker startup is single-instance and manages only its own child process; Sources can check Broker health, with a fix for transient refresh responses.
- One-click mode allows `http://localhost:8080` and `http://127.0.0.1:8080` by default, with fixed Broker `127.0.0.1:8765` and backend `http://127.0.0.1:8000`. The launcher manages only the Broker, not Docker.

## v0.7.1 — first public release / 首次公开版本

### 中文

- 首次公开发布，提供 Canvas 与课程站点同步、截止日期和任务管理、重要变更展示、学习规划、可选 AI 审核，以及独立的本机 Auth Broker 登录流程。

### English

- First public release, with Canvas and course-site synchronization, deadlines and task management, important-change display, study planning, optional AI review, and a separate host-side Auth Broker sign-in flow.

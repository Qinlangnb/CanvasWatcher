# CanvasWatcher / Academic Watcher

Local-first academic dashboard for Canvas and course websites. It tracks course material, files, deadlines, tasks, study time and changes; optional AI review helps keep homepage notifications relevant. Gradescope and PrairieLearn read-only integrations are included, but PrairieLearn student-course parsing has not been verified against an enrolled course.

本地优先的课程学习看板：同步 Canvas 与课程网站，管理资料、文件、截止日期、任务、学习时间及变更；可选 AI 审核用于减少首页通知噪声。包含 Gradescope 和 PrairieLearn 只读接入，但 PrairieLearn 学生课程解析尚未通过真实已选课程验证。

Version / 版本：**0.7.1**. [Deployment guide / 中英双语部署说明](DEPLOYMENT.md).

The post-release notification hotfix scans beyond historical unapproved critical rows so an approved homepage banner cannot be hidden by a 100-row candidate window. The regression is covered by the 580-test backend suite.

发布后的通知热修复会继续扫描历史未批准的高优先级记录，避免它们占满 100 条候选窗口、遮住已批准的首页通知；后端 580 项测试包含该回归场景。

This is a self-hosted, single-user project. The default Compose configuration binds the web UI, API and optional ntfy service to loopback only. Do not publish these ports directly to the internet. Browser sign-in uses a separate host-side Auth Broker; passwords and MFA stay on the provider's official pages. API tokens and AI keys should be entered through the local UI and are process-memory-only unless explicitly configured otherwise; a backend restart requires re-import.

这是单用户自托管项目。默认 Compose 仅绑定本机回环地址，不应直接暴露到公网。浏览器登录通过独立的宿主机 Auth Broker 完成；密码和 MFA 只在平台官方页面输入。API Token 与 AI Key 建议在本地界面导入并只保留于进程内存；后端重启后需重新导入。

## Source tree / 源码结构

- `backend/`: FastAPI, sync/AI services, Alembic migrations and tests.
- `frontend/`: React/Vite UI and tests.
- `auth-broker/`: host-side loopback browser sign-in helper.
- `config/*.example.yaml`: templates only; real local configuration is ignored by Git.
- `docker-compose.yml`: local source-build deployment.

## License / 许可

The repository is distributed under **GNU GPL version 2**; see [LICENSE](LICENSE). Keep this license and applicable copyright notices when redistributing or modifying the source. Third-party dependencies retain their own licenses. No credentials, browser profiles, databases, downloaded course files or user-specific configuration are part of this repository.

本仓库按 **GNU GPL 第 2 版**发布，全文见 [LICENSE](LICENSE)。再分发或修改源码时请保留该许可及适用的版权声明；第三方依赖仍遵守各自许可。仓库不包含凭据、浏览器配置档、数据库、下载的课程文件或个人配置。

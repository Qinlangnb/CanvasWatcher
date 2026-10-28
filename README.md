# CanvasWatcher / Academic Watcher

[中文说明](README.zh-CN.md) · [Local deployment](DEPLOYMENT.md) · [Multi-user preview](DEPLOYMENT_MULTIUSER.md) · [GPL-2.0](LICENSE)

CanvasWatcher collects course materials, deadlines, tasks, and meaningful changes from Canvas and supported course sites. It offers a Today planner, file history, calendar imports, and optional AI assistance. Source synchronization is read-only; AI write actions require explicit confirmation.

The default Docker Compose deployment is a **single-user, local-first** application. This branch also contains an **invitation-only multi-user preview** with an identity gateway, isolated tenant processes and storage, and a `/watcher` public path. The multi-user deployment needs a separately configured HTTPS reverse proxy and a Linux administrator. It is not enabled by the default Compose command.

![Today dashboard with synthetic courses and tasks](docs/screenshots/today-demo.png)

*A real application screenshot using synthetic demo data; no personal course records or credentials are shown.*

## Start locally

From a clone of this repository, install Docker Engine or Docker Desktop with Compose v2, then run:

```sh
cp .env.example .env
cp config/courses.example.yaml config/courses.runtime.yaml
cp config/file_rules.example.yaml config/file_rules.yaml
cp config/availability.example.yaml config/availability.yaml
docker compose up -d --build
```

Open `http://127.0.0.1:8080`. The API health endpoint is `http://127.0.0.1:8000/api/health`. On Windows, use `Copy-Item` instead of `cp`. See the [deployment guide](DEPLOYMENT.md) for browser sign-in, updates, and backups.

## Multi-user preview

The `identity-gateway/` and `ops/azure/` trees contain the tenant gateway, invitation registration, per-user provisioning, Caddy example, and systemd units used by the multi-user variant. Each tenant receives a separate SQLite database, credential store, file tree, process, and disk quota. Gateway-to-tenant traffic uses a Unix socket and an internal token. The example origin `watcher.example.com` must be replaced with your HTTPS domain before installation.

The included Linux installer is intended for an administrator who can inspect and adapt the systemd, Caddy, storage, and network settings. It requires `/etc/academic-watcher/public.env` to be configured first. `ops/azure/import_owner.py` is specifically for migrating an existing single-user installation and requires explicit owner identity arguments. A fresh multi-user installation has no automated first-admin bootstrap in this preview; use the local Compose deployment unless you are implementing that setup yourself. Do not expose the default Compose ports directly to the internet.

## Features and limits

- Canvas and course-site synchronization, with read-only Gradescope and PrairieLearn adapters. PrairieLearn enrolled-course parsing has not been validated with a real enrolled course.
- Deadline, task, file, announcement, and grade-release views; read-only ICS calendar import.
- Optional AI Chat and summaries. Enabling AI may send selected academic context to the model provider you configure.
- Optional desktop Auth Broker for official browser-based provider sign-in. Passwords and MFA stay on provider pages.
- SQLite migrations, backend tests, frontend tests, and a Docker Compose development path.

The default local deployment keeps credentials in process memory. Linux operators can [enable persistent encrypted storage](DEPLOYMENT.md#optional-persistent-encrypted-credentials-linux); the multi-user preview provisions a separate key and vault per tenant. Back up `.env`, `data/`, and your runtime configuration before upgrading. Never commit tokens, cookies, databases, browser profiles, or course downloads.

## Source tree

| Path | Purpose |
| --- | --- |
| `backend/` | FastAPI, source adapters, synchronization, database migrations, AI services |
| `frontend/` | React and TypeScript interface |
| `auth-broker/` | Desktop browser sign-in helper |
| `identity-gateway/` | Invitation, session, and tenant routing gateway |
| `ops/` | Optional Linux key helper and multi-user deployment templates |
| `ops/azure/` | Example multi-user Linux provisioning and service files |

## License and development

Released under the [GNU General Public License, version 2](LICENSE). Third-party dependencies keep their own licenses. The public history starts after earlier private development; its commits do not cover the full project history. Development involved AI coding agents under human-directed requirements and review.

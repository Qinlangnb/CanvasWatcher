# Multi-user Linux preview

[中文](DEPLOYMENT_MULTIUSER.zh-CN.md) · [Default local deployment](DEPLOYMENT.md)

This is an operator-oriented preview, not a turnkey public installation. It was designed for 2–5 invited users on one Linux host. The default Docker Compose deployment remains single-user. Review the service units, storage quota, outbound policy, and reverse-proxy settings for your host before installation.

## Requirements

- Linux with systemd, Python 3.12, Caddy or an equivalent HTTPS reverse proxy, sufficient disk space for separate 5 GiB tenant images, and administrator access.
- A domain with valid HTTPS. Only ports 80/443 should be public; the gateway binds `127.0.0.1:18780` and tenant APIs use Unix sockets.
- A secure, separate backup of any existing single-user database, downloaded files, credential vault, and keys before migration. Never put those files in Git.

## Prepare the public origin

Replace `watcher.example.com` in `ops/azure/Caddyfile.watcher` with your domain and install that site block in your Caddy configuration. If Cloudflare is used, review `ops/azure/Caddyfile.cloudflare-global` and its current IP ranges. Keep HTTP-01 certificate challenges reachable or configure DNS-01. Verify TLS and the reverse proxy before handling real accounts.

On the Linux host, create the public environment file before running the installer:

```sh
sudo install -d -m 0755 /etc/academic-watcher
sudo install -m 0644 ops/azure/public.env.example /etc/academic-watcher/public.env
sudoedit /etc/academic-watcher/public.env
```

Set `AW_GATEWAY_PUBLIC_ORIGIN` to your exact HTTPS origin, without `/watcher` or a trailing path. The installer rejects the example domain. Gateway and provisioner services both read this file.

## Build and install the code

Build the frontend with `VITE_PUBLIC_BASE=/watcher` and `VITE_ACCOUNT_URL=/watcher/account`. The install script expects one bundle directory containing `backend/`, `identity-gateway/`, `frontend-dist/`, and `ops/azure/`. An operator can prepare that bundle from this repository after installing frontend dependencies:

```sh
cd frontend && npm ci && VITE_PUBLIC_BASE=/watcher VITE_ACCOUNT_URL=/watcher/account npm run build && cd ..
mkdir -p bundle/ops
cp -a backend identity-gateway bundle/
cp -a frontend/dist bundle/frontend-dist
cp -a ops/azure bundle/ops/
sudo sh bundle/ops/azure/install.sh "$PWD/bundle"
```

The installer creates the gateway service and provisioner timer and installs backend dependencies. It does not edit Caddy or initialize the first account. For a migration from an existing single-user deployment, inspect `ops/azure/import_owner.py` and provide `--username` and `--email` explicitly with the isolated migration input. It expects a prepared legacy snapshot and performs privileged writes. A **fresh** multi-user installation has no automatic first-admin bootstrap in this preview.

After configuration, check `aw-gateway`, `aw-provision.timer`, the tenant service, HTTPS login, anonymous API rejection, tenant isolation, downloads, and backup restoration. Do not treat a healthy login page as proof that external Canvas or course-site credentials work; sign in to each source and verify a real sync separately.

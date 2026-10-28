#!/bin/sh
# Idempotent Azure installer for the multi-user Academic Watcher.
# Run as root from an unpacked bundle: sudo sh ops/azure/install.sh <bundle-dir>
# Touches only /opt/academic-watcher, /etc/academic-watcher, aw-* units and users;
# never edits the existing Caddy configuration or unrelated services.
set -eu
BUNDLE=${1:?bundle directory}
OPT=/opt/academic-watcher
ETC=/etc/academic-watcher
[ "$(id -u)" = 0 ] || { echo "run as root" >&2; exit 2; }

export DEBIAN_FRONTEND=noninteractive
dpkg -s python3-venv >/dev/null 2>&1 || apt-get install -y -q python3-venv python3-pip

getent passwd aw-gateway >/dev/null || useradd --system --user-group --home-dir /nonexistent \
  --no-create-home --shell /usr/sbin/nologin aw-gateway

install -d -m 0755 "$OPT" "$OPT/ops" "$OPT/config-templates" "$OPT/frontend"
install -d -m 0755 "$ETC"
if [ ! -f "$ETC/public.env" ]; then
  echo "Create $ETC/public.env from ops/azure/public.env.example before installation" >&2
  exit 2
fi
if ! grep -Eq '^AW_GATEWAY_PUBLIC_ORIGIN=https://[^[:space:]/]+$' "$ETC/public.env" || \
   grep -q 'watcher.example.com' "$ETC/public.env"; then
  echo "Set AW_GATEWAY_PUBLIC_ORIGIN to your real HTTPS domain in $ETC/public.env" >&2
  exit 2
fi
# Bind-mount targets used inside tenant namespaces; stay empty on the host.
install -d -m 0755 /data /config /course_exports

# Code: replace whole trees so removed files do not linger.
rm -rf "$OPT/backend.new" "$OPT/identity-gateway.new" "$OPT/frontend/dist.new"
cp -a "$BUNDLE/backend" "$OPT/backend.new"
cp -a "$BUNDLE/identity-gateway" "$OPT/identity-gateway.new"
cp -a "$BUNDLE/frontend-dist" "$OPT/frontend/dist.new"
for pair in "backend.new:backend" "identity-gateway.new:identity-gateway" "frontend/dist.new:frontend/dist"; do
  new=${pair%%:*}; cur=${pair#*:}
  rm -rf "$OPT/$cur.old"
  [ -e "$OPT/$cur" ] && mv "$OPT/$cur" "$OPT/$cur.old"
  mv "$OPT/$new" "$OPT/$cur"
done
chown -R root:root "$OPT"
chmod -R u=rwX,go=rX "$OPT/backend" "$OPT/identity-gateway" "$OPT/frontend"
install -m 0755 "$BUNDLE/ops/azure/aw_provision.py" "$BUNDLE/ops/azure/import_owner.py" "$BUNDLE/ops/azure/e2e_lifecycle.py" "$OPT/ops/"
install -m 0644 "$BUNDLE"/ops/azure/config-templates/*.yaml "$OPT/config-templates/"

# Backend runtime (using the versions validated for this release) plus Chromium for website sources.
[ -x "$OPT/venv/bin/python" ] || python3 -m venv "$OPT/venv"
"$OPT/venv/bin/pip" install -q --upgrade pip
"$OPT/venv/bin/pip" install -q -c "$BUNDLE/ops/azure/constraints-backend.txt" "$OPT/backend[playwright]"
PLAYWRIGHT_BROWSERS_PATH="$OPT/ms-playwright" "$OPT/venv/bin/playwright" install --with-deps chromium >/dev/null
chmod -R go+rX "$OPT/ms-playwright"

[ -x "$OPT/gateway-venv/bin/python" ] || python3 -m venv "$OPT/gateway-venv"
"$OPT/gateway-venv/bin/pip" install -q --upgrade pip
"$OPT/gateway-venv/bin/pip" install -q -c "$BUNDLE/ops/azure/constraints-gateway.txt" "$OPT/identity-gateway"

# Gateway CSRF/session key: created once, root-only, handed over by LoadCredential.
if [ ! -e "$ETC/gateway-csrf.key" ]; then
  umask 077; head -c 48 /dev/urandom | base64 | tr -d '\n' > "$ETC/gateway-csrf.key"; umask 022
fi
chmod 0600 "$ETC/gateway-csrf.key"

install -m 0644 "$BUNDLE"/ops/azure/systemd/aw-tenant@.service "$BUNDLE"/ops/azure/systemd/aw-gateway.service \
  "$BUNDLE"/ops/azure/systemd/aw-provision.service "$BUNDLE"/ops/azure/systemd/aw-provision.timer /etc/systemd/system/
install -m 0644 "$BUNDLE/ops/azure/systemd/academic-watcher.tmpfiles.conf" /etc/tmpfiles.d/academic-watcher.conf
systemd-tmpfiles --create /etc/tmpfiles.d/academic-watcher.conf
systemctl daemon-reload
systemctl enable aw-gateway.service aw-provision.timer >/dev/null
systemctl restart aw-gateway.service
systemctl start aw-provision.timer
# Pick up new code in already-running tenants.
for unit in $(systemctl list-units --plain --no-legend 'aw-tenant@*.service' | awk '{print $1}'); do
  systemctl restart "$unit"
done
echo "install_ok"

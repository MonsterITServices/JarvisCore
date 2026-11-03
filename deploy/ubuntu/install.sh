#!/usr/bin/env bash
set -Eeuo pipefail

APP_NAME="jarvis"
APP_USER="jarvis"
APP_GROUP="jarvis"
APP_DIR="/opt/jarvis/app"
VENV_DIR="/opt/jarvis/venv"
DATA_DIR="/var/lib/jarvis"
ENV_FILE="/etc/jarvis.env"
SERVICE_FILE="/etc/systemd/system/jarvis.service"
APACHE_SITE="/etc/apache2/sites-available/jarvis.conf"
AUTH_FILE="/etc/apache2/.htpasswd-jarvis"

log() {
  printf '\n[Jarvis] %s\n' "$1"
}

fail() {
  printf '\n[Jarvis] ERROR: %s\n' "$1" >&2
  exit 1
}

if [[ "${EUID}" -ne 0 ]]; then
  fail "Run with sudo: sudo bash deploy/ubuntu/install.sh"
fi

if [[ ! -r /etc/os-release ]]; then
  fail "Cannot identify this operating system."
fi

. /etc/os-release
if [[ "${ID:-}" != "ubuntu" ]]; then
  fail "This installer targets Ubuntu Server."
fi

if [[ "$(uname -m)" != "x86_64" ]]; then
  fail "This installer currently targets x86_64 Ubuntu."
fi

SOURCE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
if [[ ! -f "${SOURCE_DIR}/app.py" || ! -f "${SOURCE_DIR}/requirements.txt" ]]; then
  fail "Run this script from the copied Jarvis repository under deploy/ubuntu/."
fi

if ! command -v python3 >/dev/null 2>&1 || ! python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)'; then
  fail "Python 3.10 or newer is required (Ubuntu 22.04 or newer)."
fi
PYTHON_IS_311="$(python3 -c 'import sys; print(1 if sys.version_info >= (3, 11) else 0)')"

DEFAULT_IP="$(hostname -I 2>/dev/null | awk '{print $1}')"
DEFAULT_IP="${DEFAULT_IP:-192.168.1.50}"
SERVER_IP="${SERVER_IP:-}"
if [[ -z "${SERVER_IP}" ]]; then
  read -r -p "Ubuntu server LAN IPv4 [${DEFAULT_IP}]: " SERVER_IP
  SERVER_IP="${SERVER_IP:-${DEFAULT_IP}}"
fi

DEFAULT_LAN="192.168.1.0/24"
LAN_CIDR="${LAN_CIDR:-}"
if [[ -z "${LAN_CIDR}" ]]; then
  read -r -p "Trusted LAN CIDR [${DEFAULT_LAN}]: " LAN_CIDR
  LAN_CIDR="${LAN_CIDR:-${DEFAULT_LAN}}"
fi

# JARVIS_USE_OLLAMA=0 skips the large local model and uses hosted AI keys added in Settings.
USE_OLLAMA="${JARVIS_USE_OLLAMA:-}"
if [[ -z "${USE_OLLAMA}" ]]; then
  read -r -p "Install the local Ollama model? Choose n to use Gemini/Claude keys only [Y/n]: " answer
  case "${answer:-Y}" in [nN]*) USE_OLLAMA=0 ;; *) USE_OLLAMA=1 ;; esac
fi
OLLAMA_MODEL="${JARVIS_OLLAMA_MODEL:-}"
if [[ "${USE_OLLAMA}" == "1" && -z "${OLLAMA_MODEL}" ]]; then
  read -r -p "Ollama model [qwen3-coder:30b]: " OLLAMA_MODEL
fi
OLLAMA_MODEL="${OLLAMA_MODEL:-qwen3-coder:30b}"

# Existing Cloudflare tunnel hostname (cloudflared itself is never installed or changed here).
PUBLIC_HOST="${JARVIS_PUBLIC_HOST-}"
if [[ -z "${JARVIS_PUBLIC_HOST+x}" ]]; then
  read -r -p "Cloudflare tunnel hostname, blank for none [jarvis.monsterit.co.uk]: " PUBLIC_HOST
  PUBLIC_HOST="${PUBLIC_HOST:-jarvis.monsterit.co.uk}"
fi
if [[ -n "${PUBLIC_HOST}" && ! "${PUBLIC_HOST}" =~ ^[a-zA-Z0-9.-]{1,253}$ ]]; then
  fail "The tunnel hostname may contain letters, numbers, dot, and hyphen only."
fi

AUTH_USER="${JARVIS_WEB_USER:-jarvis}"
if [[ ! "${AUTH_USER}" =~ ^[a-zA-Z0-9._-]{1,64}$ ]]; then
  fail "The web username may contain letters, numbers, dot, underscore, and hyphen only."
fi

python3 - "${SERVER_IP}" "${LAN_CIDR}" <<'PY'
import ipaddress
import sys

try:
    address = ipaddress.ip_address(sys.argv[1])
    network = ipaddress.ip_network(sys.argv[2], strict=False)
except ValueError as error:
    raise SystemExit(f"Invalid server IP or LAN CIDR: {error}")
if address.version != 4 or network.version != 4:
    raise SystemExit("This installer expects IPv4 addresses and CIDRs.")
if address not in network:
    raise SystemExit("The server IP must be inside the trusted LAN CIDR.")
PY

log "Installing Ubuntu packages"
export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get install -y ca-certificates curl build-essential python3 python3-venv python3-dev apache2 apache2-utils

if ! id "${APP_USER}" >/dev/null 2>&1; then
  useradd --system --create-home --home-dir "${DATA_DIR}" --shell /usr/sbin/nologin "${APP_USER}"
fi
install -d -o "${APP_USER}" -g "${APP_GROUP}" -m 0750 "${DATA_DIR}/Projects"
install -d -o "${APP_USER}" -g "${APP_GROUP}" -m 0700 "${DATA_DIR}/.local/share/jarviscore"
install -d -o root -g "${APP_GROUP}" -m 2770 "${APP_DIR}/config"
install -d -o root -g "${APP_GROUP}" -m 0750 "${APP_DIR}"
install -d -o "${APP_USER}" -g "${APP_GROUP}" -m 0750 "${VENV_DIR}"

log "Copying Jarvis application files"
if ! command -v rsync >/dev/null 2>&1; then
  apt-get install -y rsync
fi
rsync -a \
  --exclude='.git/' \
  --exclude='.venv/' \
  --exclude='__pycache__/' \
  --exclude='.jarvis_backups/' \
  --exclude='config/rules.json' \
  "${SOURCE_DIR}/" "${APP_DIR}/"

if [[ ! -f "${APP_DIR}/config/rules.json" ]]; then
  install -o "${APP_USER}" -g "${APP_GROUP}" -m 0660 "${SOURCE_DIR}/config/rules.json" "${APP_DIR}/config/rules.json"
fi
chown -R root:"${APP_GROUP}" "${APP_DIR}"
chown root:"${APP_GROUP}" "${APP_DIR}/config"
chmod 2770 "${APP_DIR}/config"
chown "${APP_USER}":"${APP_GROUP}" "${APP_DIR}/config/rules.json"
chmod 0660 "${APP_DIR}/config/rules.json"

python3 - "${APP_DIR}/config/rules.json" "${DATA_DIR}/Projects" "${LAN_CIDR}" <<'PY'
import json
import os
import sys
import tempfile

rules_path, workspace, lan_cidr = sys.argv[1:]
with open(rules_path, encoding="utf-8") as source:
    rules = json.load(source)
rules.setdefault("security_policy", {})["trusted_networks"] = [lan_cidr]
rules.setdefault("code_workbench", {})["workspace_root"] = workspace
fd, temporary_path = tempfile.mkstemp(prefix="rules-", suffix=".tmp", dir=os.path.dirname(rules_path))
try:
    with os.fdopen(fd, "w", encoding="utf-8") as target:
        json.dump(rules, target, ensure_ascii=False, indent=2)
        target.write("\n")
    os.replace(temporary_path, rules_path)
finally:
    if os.path.exists(temporary_path):
        os.unlink(temporary_path)
PY
chown "${APP_USER}":"${APP_GROUP}" "${APP_DIR}/config/rules.json"
chmod 0660 "${APP_DIR}/config/rules.json"

log "Creating the local Python environment"
if [[ ! -x "${VENV_DIR}/bin/python" ]]; then
  python3 -m venv "${VENV_DIR}"
  chown -R "${APP_USER}":"${APP_GROUP}" "${VENV_DIR}"
fi
runuser -u "${APP_USER}" -- "${VENV_DIR}/bin/pip" install --upgrade pip
runuser -u "${APP_USER}" -- "${VENV_DIR}/bin/pip" install -r "${APP_DIR}/requirements.txt"
if [[ "${PYTHON_IS_311}" == "1" ]]; then
  runuser -u "${APP_USER}" -- "${VENV_DIR}/bin/pip" install github-copilot-sdk \
    || log "Optional GitHub Copilot support could not be installed; Gemini and Claude still work."
fi

if [[ "${USE_OLLAMA}" == "1" ]]; then
if ! command -v ollama >/dev/null 2>&1; then
  log "Installing Ollama from its official installer"
  curl --fail --show-error --location --proto '=https' --tlsv1.2 \
    https://ollama.com/install.sh -o /tmp/jarvis-ollama-install.sh
  bash /tmp/jarvis-ollama-install.sh
  rm -f /tmp/jarvis-ollama-install.sh
fi
systemctl enable --now ollama

log "Downloading the selected local language model: ${OLLAMA_MODEL}"
ollama pull "${OLLAMA_MODEL}"
else
  log "Skipping Ollama; add a Gemini or Claude key in Jarvis Settings."
fi

if [[ ! -f "${ENV_FILE}" ]]; then
  install -o root -g "${APP_GROUP}" -m 0640 /dev/null "${ENV_FILE}"
  cat > "${ENV_FILE}" <<EOF
HOME=${DATA_DIR}
JARVIS_OLLAMA_MODEL=${OLLAMA_MODEL}
JARVIS_KNOWLEDGE_DB=${DATA_DIR}/.local/share/jarviscore/knowledge.sqlite3
BRAVE_SEARCH_API_KEY=
JARVIS_HA_URL=
JARVIS_HA_TOKEN=
EOF
  chown root:"${APP_GROUP}" "${ENV_FILE}"
  chmod 0640 "${ENV_FILE}"
else
  log "Keeping existing ${ENV_FILE}; update JARVIS_OLLAMA_MODEL there if needed."
fi
sed -i '/^JARVIS_PUBLIC_ORIGIN=/d' "${ENV_FILE}"
if [[ -n "${PUBLIC_HOST}" ]]; then
  echo "JARVIS_PUBLIC_ORIGIN=https://${PUBLIC_HOST}" >> "${ENV_FILE}"
fi

log "Installing the systemd service"
cat > "${SERVICE_FILE}" <<EOF
[Unit]
Description=Jarvis local AI assistant
After=network-online.target ollama.service
Wants=network-online.target

[Service]
Type=simple
User=${APP_USER}
Group=${APP_GROUP}
WorkingDirectory=${APP_DIR}
EnvironmentFile=${ENV_FILE}
Environment=HOME=${DATA_DIR}
Environment=JARVIS_KNOWLEDGE_DB=${DATA_DIR}/.local/share/jarviscore/knowledge.sqlite3
ExecStart=${VENV_DIR}/bin/gunicorn --bind 127.0.0.1:5000 --workers 1 --threads 4 --timeout 240 app:app
Restart=on-failure
RestartSec=5
UMask=0077
NoNewPrivileges=true
PrivateTmp=true
ProtectHome=true
ProtectSystem=strict
ReadWritePaths=${APP_DIR}/config ${DATA_DIR}

[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload
systemctl enable --now jarvis

log "Creating Apache LAN-only reverse proxy"
if systemctl is-active --quiet nginx 2>/dev/null; then
  log "Stopping nginx from an earlier install so Apache can use port 80"
  systemctl disable --now nginx
fi
rm -f /etc/nginx/sites-enabled/jarvis
if [[ -f "${AUTH_FILE}" ]]; then
  htpasswd -B "${AUTH_FILE}" "${AUTH_USER}"
else
  htpasswd -cB "${AUTH_FILE}" "${AUTH_USER}"
fi
chown root:www-data "${AUTH_FILE}"
chmod 0640 "${AUTH_FILE}"

cat > "${APACHE_SITE}" <<EOF
<VirtualHost *:80>
    ServerName ${SERVER_IP}
    ServerSignature Off
    LimitRequestBody 27262976
    ProxyPreserveHost On
    ProxyTimeout 240
    ProxyPass / http://127.0.0.1:5000/ timeout=240
    ProxyPassReverse / http://127.0.0.1:5000/
    RequestHeader set X-Forwarded-Proto "http"
    Header always set X-Content-Type-Options "nosniff"
    Header always set X-Frame-Options "DENY"
    Header always set Referrer-Policy "no-referrer"

    <Location />
        AuthType Basic
        AuthName "Jarvis LAN"
        AuthUserFile ${AUTH_FILE}
        <RequireAll>
            Require ip ${LAN_CIDR}
            Require valid-user
        </RequireAll>
    </Location>
</VirtualHost>
EOF
if [[ -n "${PUBLIC_HOST}" ]]; then
  # Existing Cloudflare tunnel: only same-machine connections (cloudflared) may use this no-password host.
  cat >> "${APACHE_SITE}" <<EOF

<VirtualHost *:80>
    ServerName ${PUBLIC_HOST}
    ServerSignature Off
    LimitRequestBody 27262976
    ProxyPreserveHost On
    ProxyTimeout 240
    ProxyPass / http://127.0.0.1:5000/ timeout=240
    ProxyPassReverse / http://127.0.0.1:5000/
    RequestHeader set X-Forwarded-Proto "https"
    Header always set X-Content-Type-Options "nosniff"
    Header always set X-Frame-Options "DENY"
    Header always set Referrer-Policy "no-referrer"

    <Location />
        Require local
    </Location>
</VirtualHost>
EOF
fi
a2enmod proxy proxy_http headers auth_basic authn_file authz_core authz_host >/dev/null
a2dissite 000-default >/dev/null 2>&1 || true
a2ensite jarvis >/dev/null
apache2ctl configtest
systemctl enable apache2
systemctl restart apache2

if command -v ufw >/dev/null 2>&1 && ufw status | grep -q "Status: active"; then
  ufw allow from "${LAN_CIDR}" to any port 80 proto tcp
fi

log "Checking the local Jarvis health endpoint"
healthy=0
for _ in $(seq 1 20); do
  if curl --fail --silent http://127.0.0.1:5000/api/health >/dev/null; then healthy=1; break; fi
  sleep 2
done
if [[ "${healthy}" != "1" ]]; then
  journalctl -u jarvis -n 30 --no-pager || true
  fail "Jarvis did not start. See the log above or run: sudo journalctl -u jarvis -f"
fi

printf '\nJarvis is installed.\n'
printf 'LAN URL: http://%s\n' "${SERVER_IP}"
printf 'Web login: %s (password entered into htpasswd above)\n' "${AUTH_USER}"
if [[ "${USE_OLLAMA}" == "1" ]]; then printf 'Ollama model: %s\n' "${OLLAMA_MODEL}"; fi
printf 'Code workspace: %s/Projects\n' "${DATA_DIR}"
printf 'Next: open the URL, click Settings, add your AI key and Home Assistant token there.\n'
if [[ -n "${PUBLIC_HOST}" ]]; then
  printf 'Tunnel: your existing Cloudflare tunnel needs no changes. %s is served without a password to same-machine tunnel traffic.\n' "${PUBLIC_HOST}"
  printf 'The tunnel service can stay on http://localhost:80 (or point it at http://127.0.0.1:5000); cloudflared is not installed or modified here.\n'
  printf 'SECURITY: that route has no password. Protect it with a Cloudflare Access policy, or anyone online can control Jarvis.\n'
fi
printf 'Logs: sudo journalctl -u jarvis -f\n'
printf 'Update later: copy the new files over and re-run this script; your settings and keys are kept.\n'
printf 'Do not port-forward this HTTP service to the public internet.\n'

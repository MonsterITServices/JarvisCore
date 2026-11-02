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
NGINX_SITE="/etc/nginx/sites-available/jarvis"
AUTH_FILE="/etc/nginx/.htpasswd"

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

OLLAMA_MODEL="${JARVIS_OLLAMA_MODEL:-}"
if [[ -z "${OLLAMA_MODEL}" ]]; then
  read -r -p "Ollama model [qwen3-coder:30b]: " OLLAMA_MODEL
  OLLAMA_MODEL="${OLLAMA_MODEL:-qwen3-coder:30b}"
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
apt-get install -y ca-certificates curl build-essential python3 python3-venv python3-dev nginx apache2-utils

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

log "Creating Nginx LAN-only reverse proxy"
if [[ -f "${AUTH_FILE}" ]]; then
  htpasswd -B "${AUTH_FILE}" "${AUTH_USER}"
else
  htpasswd -cB "${AUTH_FILE}" "${AUTH_USER}"
fi
chown root:www-data "${AUTH_FILE}"
chmod 0640 "${AUTH_FILE}"

cat > "${NGINX_SITE}" <<EOF
server {
    listen 80;
    server_name ${SERVER_IP};

    allow ${LAN_CIDR};
    deny all;

    client_max_body_size 26m;
    auth_basic "Jarvis LAN";
    auth_basic_user_file ${AUTH_FILE};

    add_header X-Content-Type-Options nosniff always;
    add_header X-Frame-Options DENY always;
    add_header Referrer-Policy no-referrer always;

    location / {
        proxy_pass http://127.0.0.1:5000;
        proxy_http_version 1.1;
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto \$scheme;
        proxy_read_timeout 240s;
    }
}
EOF
ln -sfn "${NGINX_SITE}" /etc/nginx/sites-enabled/jarvis
nginx -t
systemctl enable --now nginx
systemctl reload nginx

if command -v ufw >/dev/null 2>&1 && ufw status | grep -q "Status: active"; then
  ufw allow from "${LAN_CIDR}" to any port 80 proto tcp
fi

log "Checking the local Jarvis health endpoint"
curl --fail --silent --show-error http://127.0.0.1:5000/api/health >/dev/null

printf '\nJarvis is installed.\n'
printf 'LAN URL: http://%s\n' "${SERVER_IP}"
printf 'Web login: %s (password entered into htpasswd above)\n' "${AUTH_USER}"
printf 'Ollama model: %s\n' "${OLLAMA_MODEL}"
printf 'Code workspace: %s/Projects\n' "${DATA_DIR}"
printf 'Logs: sudo journalctl -u jarvis -f\n'
printf 'Optional Brave/Home Assistant credentials: sudo nano %s\n' "${ENV_FILE}"
printf 'Do not port-forward this HTTP service to the public internet.\n'

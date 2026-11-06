#!/usr/bin/env bash
set -Eeuo pipefail

WEB_PORT="8080"
APP_NAME="jarvis"
APP_USER="jarvis"
APP_GROUP="jarvis"
APP_DIR="/opt/jarvis/app"
VENV_DIR="/opt/jarvis/venv"
DATA_DIR="/var/lib/jarvis"
ENV_FILE="/etc/jarvis.env"
SERVICE_FILE="/etc/systemd/system/jarvis.service"
APACHE_SITE="/etc/apache2/sites-available/jarvis.conf"

log() { printf '\n[Jarvis] %s\n' "$1"; }
fail() { printf '\n[Jarvis] ERROR: %s\n' "$1" >&2; exit 1; }

if [[ "${EUID}" -ne 0 ]]; then fail "Run with sudo: sudo bash deploy/ubuntu/install.sh"; fi

SOURCE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
if [[ ! -f "${SOURCE_DIR}/app.py" ]]; then fail "Run from the copied Jarvis repository."; fi

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

USE_OLLAMA="${JARVIS_USE_OLLAMA:-0}"
OLLAMA_MODEL="${JARVIS_OLLAMA_MODEL:-qwen3-coder:30b}"

PUBLIC_HOST="${JARVIS_PUBLIC_HOST-}"
if [[ -z "${JARVIS_PUBLIC_HOST+x}" ]]; then
  read -r -p "Cloudflare tunnel hostname, blank for none [jarvis.monsterit.co.uk]: " PUBLIC_HOST
  PUBLIC_HOST="${PUBLIC_HOST:-jarvis.monsterit.co.uk}"
fi

log "Installing Ubuntu packages"
export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get install -y ca-certificates curl build-essential python3 python3-venv python3-dev apache2 apache2-utils rsync

if ! id "${APP_USER}" >/dev/null 2>&1; then
  useradd --system --create-home --home-dir "${DATA_DIR}" --shell /usr/sbin/nologin "${APP_USER}"
fi
install -d -o "${APP_USER}" -g "${APP_GROUP}" -m 0750 "${DATA_DIR}/Projects"
install -d -o "${APP_USER}" -g "${APP_GROUP}" -m 0700 "${DATA_DIR}/.local/share/jarviscore"
install -d -o root -g "${APP_GROUP}" -m 2770 "${APP_DIR}/config"
install -d -o root -g "${APP_GROUP}" -m 0750 "${APP_DIR}"
install -d -o "${APP_USER}" -g "${APP_GROUP}" -m 0750 "${VENV_DIR}"

log "Copying Jarvis application files"
rsync -a --exclude='.git/' --exclude='.venv/' --exclude='__pycache__/' --exclude='config/rules.json' "${SOURCE_DIR}/" "${APP_DIR}/"

if [[ ! -f "${APP_DIR}/config/rules.json" ]]; then
  install -o "${APP_USER}" -g "${APP_GROUP}" -m 0660 "${SOURCE_DIR}/config/rules.json" "${APP_DIR}/config/rules.json"
fi
chown -R root:"${APP_GROUP}" "${APP_DIR}"
chown root:"${APP_GROUP}" "${APP_DIR}/config"
chmod 2770 "${APP_DIR}/config"

log "Creating the local Python environment"
if [[ ! -x "${VENV_DIR}/bin/python" ]]; then
  python3 -m venv "${VENV_DIR}"
  chown -R "${APP_USER}":"${APP_GROUP}" "${VENV_DIR}"
fi
runuser -u "${APP_USER}" -- "${VENV_DIR}/bin/pip" install --upgrade pip
runuser -u "${APP_USER}" -- "${VENV_DIR}/bin/pip" install -r "${APP_DIR}/requirements.txt"

if [[ ! -f "${ENV_FILE}" ]]; then
  install -o root -g "${APP_GROUP}" -m 0640 /dev/null "${ENV_FILE}"
  cat > "${ENV_FILE}" <<EOF
HOME=${DATA_DIR}
JARVIS_OLLAMA_MODEL=${OLLAMA_MODEL}
JARVIS_KNOWLEDGE_DB=${DATA_DIR}/.local/share/jarviscore/knowledge.sqlite3
EOF
  chown root:"${APP_GROUP}" "${ENV_FILE}"
  chmod 0640 "${ENV_FILE}"
fi

log "Installing the systemd service"
cat > "${SERVICE_FILE}" <<EOF
[Unit]
Description=Jarvis local AI assistant
After=network-online.target
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

log "Creating Apache reverse proxy on port ${WEB_PORT} (No Password)"
cat > "${APACHE_SITE}" <<EOF
Listen ${WEB_PORT}

<VirtualHost *:${WEB_PORT}>
    ServerName ${SERVER_IP}
    ServerAlias ${PUBLIC_HOST}
    ServerSignature Off
    LimitRequestBody 27262976
    ProxyPreserveHost On
    ProxyTimeout 240
    ProxyPass / http://127.0.0.1:5000/ timeout=240
    ProxyPassReverse / http://127.0.0.1:5000/
    RequestHeader set X-Forwarded-Proto "http"
    
    <Location />
        Require all granted
    </Location>
</VirtualHost>
EOF

a2enmod proxy proxy_http headers >/dev/null
a2ensite jarvis >/dev/null
systemctl restart apache2

log "Checking the local Jarvis health endpoint"
sleep 2
if ! curl --fail --silent http://127.0.0.1:5000/api/health >/dev/null; then
  journalctl -u jarvis -n 20 --no-pager || true
  fail "Jarvis did not start. See log above."
fi

printf '\nJarvis is installed.\n'
printf 'URL: http://%s:%s\n' "${SERVER_IP}" "${WEB_PORT}"
#!/usr/bin/env bash
# Removes Computer. Keeps Apache, Ollama, system packages and your Cloudflare tunnel.
# Usage: sudo bash uninstall.sh [--purge-data] [--yes]
set -Eeuo pipefail

APP_USER="jarvis"
APP_ROOT="/opt/jarvis"
DATA_DIR="/var/lib/jarvis"
ENV_FILE="/etc/jarvis.env"
SERVICE_FILE="/etc/systemd/system/jarvis.service"
APACHE_SITE="/etc/apache2/sites-available/jarvis.conf"
AUTH_FILE="/etc/apache2/.htpasswd-jarvis"

PURGE_DATA=0
ASSUME_YES=0
for arg in "$@"; do
  case "${arg}" in
    --purge-data) PURGE_DATA=1 ;;
    --yes|-y) ASSUME_YES=1 ;;
    *) printf 'Unknown option: %s\n' "${arg}" >&2; exit 1 ;;
  esac
done

log() { printf '\n[Computer] %s\n' "$1"; }

if [[ "${EUID}" -ne 0 ]]; then
  printf '[Computer] ERROR: Run with sudo: sudo bash uninstall.sh\n' >&2
  exit 1
fi

if [[ "${ASSUME_YES}" -ne 1 ]]; then
  read -r -p "Remove Computer (service, Apache site, app, login)? [y/N]: " answer
  [[ "${answer}" =~ ^[yY] ]] || { echo "Cancelled."; exit 0; }
  if [[ -d "${DATA_DIR}" && "${PURGE_DATA}" -ne 1 ]]; then
    read -r -p "Also DELETE saved keys, knowledge and the Projects workspace in ${DATA_DIR}? [y/N]: " answer
    [[ "${answer}" =~ ^[yY] ]] && PURGE_DATA=1
  fi
fi

LAN_CIDR=""
if [[ -r "${APP_ROOT}/app/config/rules.json" ]]; then
  LAN_CIDR="$(python3 - "${APP_ROOT}/app/config/rules.json" <<'PY' 2>/dev/null || true
import json, sys
try:
    print(json.load(open(sys.argv[1], encoding="utf-8"))["security_policy"]["trusted_networks"][0])
except Exception:
    pass
PY
)"
fi

log "Stopping the Computer service"
systemctl disable --now jarvis 2>/dev/null || true
rm -f "${SERVICE_FILE}"
systemctl daemon-reload

log "Removing the Apache site"
if command -v a2dissite >/dev/null 2>&1; then
  a2dissite jarvis >/dev/null 2>&1 || true
  a2ensite 000-default >/dev/null 2>&1 || true
fi
rm -f "${APACHE_SITE}" "${AUTH_FILE}"
if systemctl is-active --quiet apache2 2>/dev/null; then
  apache2ctl configtest && systemctl reload apache2
fi

if [[ -n "${LAN_CIDR}" ]] && command -v ufw >/dev/null 2>&1 && ufw status | grep -q "Status: active"; then
  ufw delete allow from "${LAN_CIDR}" to any port 80 proto tcp >/dev/null 2>&1 || true
fi

log "Removing the application and Python environment"
rm -rf "${APP_ROOT}"
rm -f "${ENV_FILE}"

if [[ "${PURGE_DATA}" -eq 1 ]]; then
  log "Deleting saved data in ${DATA_DIR}"
  rm -rf "${DATA_DIR}"
  if id "${APP_USER}" >/dev/null 2>&1; then
    userdel "${APP_USER}" 2>/dev/null || true
  fi
  getent group "${APP_USER}" >/dev/null 2>&1 && groupdel "${APP_USER}" 2>/dev/null || true
else
  log "Keeping ${DATA_DIR} (keys, knowledge, Projects) and the ${APP_USER} user. Re-installing will reuse them."
fi

printf '\nJarvis has been removed.\n'
printf 'Left untouched: Apache, Ollama and its models, system packages, and your Cloudflare tunnel.\n'
printf 'The tunnel hostname will now return an error until you remove or repoint its route in Cloudflare.\n'

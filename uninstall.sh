#!/usr/bin/env bash
# One-step Ubuntu uninstaller: sudo bash uninstall.sh [--purge-data] [--yes]
set -Eeuo pipefail
exec bash "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/deploy/ubuntu/uninstall.sh" "$@"

#!/usr/bin/env bash
# One-step Ubuntu installer: sudo bash install.sh
set -Eeuo pipefail
exec bash "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/deploy/ubuntu/install.sh" "$@"

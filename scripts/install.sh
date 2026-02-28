#!/usr/bin/env bash
set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BIN_DIR="${HOME}/.local/bin"
SYSTEMD_USER_DIR="${HOME}/.config/systemd/user"

mkdir -p "${BIN_DIR}" "${SYSTEMD_USER_DIR}"

install -m 0755 "${PROJECT_ROOT}/src/cachyos_update_tray.py" "${BIN_DIR}/cachyos-update-tray"
install -m 0644 "${PROJECT_ROOT}/systemd/cachyos-update-tray.service" "${SYSTEMD_USER_DIR}/cachyos-update-tray.service"

systemctl --user daemon-reload
systemctl --user enable --now cachyos-update-tray.service

echo "Installed and started: cachyos-update-tray.service"
echo "Config file: ${HOME}/.config/cachyos-update-tray/config.json"

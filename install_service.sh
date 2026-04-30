#!/usr/bin/env bash
# Installs and enables the systemd --user service for status-monitor.
# Substitutes the placeholder __DAEMON_PATH__ in the unit with the
# absolute path of daemon.py so the service works regardless of where
# the repo is checked out.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")" && pwd)"
UNIT_SRC="${ROOT}/systemd/status-monitor.service"
UNIT_DST_DIR="${HOME}/.config/systemd/user"
UNIT_DST="${UNIT_DST_DIR}/status-monitor.service"
DAEMON_PATH="${ROOT}/daemon.py"

if [[ ! -x "${DAEMON_PATH}" && ! -f "${DAEMON_PATH}" ]]; then
    echo "error: daemon.py not found at ${DAEMON_PATH}" >&2
    exit 1
fi

mkdir -p "${UNIT_DST_DIR}"
# Use a sed delimiter that cannot appear in a Unix path (|).
sed "s|__DAEMON_PATH__|${DAEMON_PATH}|g" "${UNIT_SRC}" > "${UNIT_DST}"
chmod 0644 "${UNIT_DST}"

systemctl --user daemon-reload
systemctl --user enable --now status-monitor.service

echo
echo "Service installed and started."
echo "  status:  systemctl --user status status-monitor"
echo "  logs:    journalctl --user -u status-monitor -f"
echo "  stop:    systemctl --user stop status-monitor"
echo "  disable: systemctl --user disable --now status-monitor"

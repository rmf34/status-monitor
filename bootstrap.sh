#!/usr/bin/env bash
# Installs runtime dependencies for status-monitor.
# Targets Ubuntu/Debian. Idempotent; safe to re-run.
set -euo pipefail

log()  { printf "\n\033[1;34m==>\033[0m %s\n" "$*"; }
note() { printf "    %s\n" "$*"; }
warn() { printf "\033[1;33m[!]\033[0m %s\n" "$*" >&2; }

if [[ "${EUID}" -eq 0 ]]; then
    warn "Run as your normal user; this script will sudo when needed."
    exit 1
fi

if [[ -r /etc/os-release ]]; then
    . /etc/os-release
    if [[ "${ID:-}" != "ubuntu" && "${ID_LIKE:-}" != *ubuntu* && "${ID_LIKE:-}" != *debian* ]]; then
        warn "This installer targets Ubuntu/Debian. Detected ID=${ID:-unknown}; continuing."
    fi
fi

APT_UPDATED=0
ensure_apt_update() {
    if [[ "${APT_UPDATED}" -eq 0 ]]; then
        log "apt-get update"
        sudo apt-get update -qq
        APT_UPDATED=1
    fi
}

ensure_pkg() {
    local pkg="$1"
    if dpkg -s "$pkg" >/dev/null 2>&1; then
        note "ok: $pkg"
        return 0
    fi
    ensure_apt_update
    log "Installing $pkg"
    sudo apt-get install -y "$pkg"
}

# 1. Base packages
log "System packages"
for pkg in curl ca-certificates gnupg apt-transport-https \
           python3 python3-gi python3-yaml python3-requests python3-pytest \
           python3-pil libnotify-bin xdg-utils; do
    ensure_pkg "$pkg"
done

# 2. AppIndicator (Ayatana fork on 22.04+/24.04; legacy name as fallback)
log "Tray indicator library"
if dpkg -s gir1.2-ayatanaappindicator3-0.1 >/dev/null 2>&1; then
    note "ok: gir1.2-ayatanaappindicator3-0.1"
elif dpkg -s gir1.2-appindicator3-0.1 >/dev/null 2>&1; then
    note "ok: gir1.2-appindicator3-0.1 (legacy)"
else
    ensure_apt_update
    if apt-cache show gir1.2-ayatanaappindicator3-0.1 >/dev/null 2>&1; then
        sudo apt-get install -y gir1.2-ayatanaappindicator3-0.1
    else
        sudo apt-get install -y gir1.2-appindicator3-0.1
    fi
fi

# 3. gcloud (Google Cloud CLI)
if command -v gcloud >/dev/null 2>&1; then
    log "gcloud already installed: $(gcloud --version 2>/dev/null | head -1)"
else
    log "Installing Google Cloud CLI"
    sudo install -d -m 0755 /usr/share/keyrings
    if [[ ! -f /usr/share/keyrings/cloud.google.gpg ]]; then
        curl -fsSL https://packages.cloud.google.com/apt/doc/apt-key.gpg \
            | sudo gpg --dearmor -o /usr/share/keyrings/cloud.google.gpg
    fi
    echo "deb [signed-by=/usr/share/keyrings/cloud.google.gpg] https://packages.cloud.google.com/apt cloud-sdk main" \
        | sudo tee /etc/apt/sources.list.d/google-cloud-sdk.list >/dev/null
    sudo apt-get update -qq
    sudo apt-get install -y google-cloud-cli
fi

# 4. gh (GitHub CLI)
if command -v gh >/dev/null 2>&1; then
    log "gh already installed: $(gh --version 2>/dev/null | head -1)"
else
    log "Installing GitHub CLI"
    sudo install -d -m 0755 /usr/share/keyrings
    if [[ ! -f /usr/share/keyrings/githubcli-archive-keyring.gpg ]]; then
        curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg \
            | sudo dd of=/usr/share/keyrings/githubcli-archive-keyring.gpg status=none
        sudo chmod go+r /usr/share/keyrings/githubcli-archive-keyring.gpg
    fi
    echo "deb [arch=$(dpkg --print-architecture) signed-by=/usr/share/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" \
        | sudo tee /etc/apt/sources.list.d/github-cli.list >/dev/null
    sudo apt-get update -qq
    sudo apt-get install -y gh
fi

# 5. Auth status
log "Auth status"
ACCT="$(gcloud auth list --filter=status:ACTIVE --format='value(account)' 2>/dev/null | head -1 || true)"
if [[ -n "${ACCT}" ]]; then
    note "gcloud: ${ACCT}"
else
    warn "gcloud not authenticated. Run:  gcloud auth login"
fi
if gh auth status >/dev/null 2>&1; then
    note "gh: $(gh api user --jq .login 2>/dev/null || echo 'authed')"
else
    warn "gh not authenticated. Run:  gh auth login"
fi

# 6. GNOME tray extension reminder (Ubuntu's GNOME ships it on; vanilla GNOME does not)
if [[ "${XDG_CURRENT_DESKTOP:-}" == *GNOME* && "${XDG_CURRENT_DESKTOP:-}" != *ubuntu:GNOME* ]]; then
    warn "Vanilla GNOME detected. If the tray icon does not appear after starting the daemon, install:"
    echo "    sudo apt install gnome-shell-extension-appindicator"
    echo "    gnome-extensions enable ubuntu-appindicators@ubuntu.com"
    echo "    (then log out and back in)"
fi

log "Bootstrap complete"
echo
echo "Next steps:"
echo "  1. (if not done)  gcloud auth login"
echo "  2. (if not done)  gh auth login"
echo "  3.                python3 setup_cli.py setup"
echo "  4. (optional)     ./install_service.sh   # autostart on login"

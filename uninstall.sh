#!/bin/bash
#
# pi-weather-clock uninstaller
#
# Reverts the changes made by install.sh:
#   - Restores boot config from .bak.original
#   - Removes modprobe blacklist
#   - Removes autologin
#   - Removes app files (with confirmation)
#   - Optionally removes the kiosk user
#
# Usage:  sudo ./uninstall.sh
#         sudo ./uninstall.sh --force  # no confirmations

set -euo pipefail

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m'

msg()   { echo -e "${BLUE}[*]${NC} $*"; }
ok()    { echo -e "${GREEN}[OK]${NC} $*"; }
warn()  { echo -e "${YELLOW}[!]${NC} $*"; }

if [ "$EUID" -ne 0 ]; then
    echo "Run as root: sudo $0"
    exit 1
fi

FORCE=0
for a in "$@"; do
    [ "$a" = "--force" ] && FORCE=1
done

confirm() {
    [ "$FORCE" -eq 1 ] && return 0
    local prompt="$1"
    local r
    read -r -p "$prompt [y/N]: " r </dev/tty
    [[ "$r" =~ ^[Yy]$ ]]
}

# Find boot dir
if [ -d /boot/firmware ]; then
    BOOT_DIR="/boot/firmware"
else
    BOOT_DIR="/boot"
fi

# Stop services first
msg "Stopping services..."
systemctl stop getty@tty1 2>/dev/null || true
pkill -9 -f weather_clock 2>/dev/null || true
ok "Stopped"

# Restore boot config
for f in config.txt cmdline.txt; do
    src="${BOOT_DIR}/${f}.bak.original"
    dst="${BOOT_DIR}/${f}"
    if [ -f "$src" ]; then
        if confirm "Restore $dst from $src ?"; then
            cp "$src" "$dst"
            ok "Restored $dst"
        fi
    else
        warn "No original backup found for $dst (skipping)"
    fi
done

# Remove modprobe blacklist
if [ -f /etc/modprobe.d/disable-hdmi-audio.conf ]; then
    rm -f /etc/modprobe.d/disable-hdmi-audio.conf
    ok "Removed modprobe blacklist"
fi

# Remove autologin override
if [ -f /etc/systemd/system/getty@tty1.service.d/autologin.conf ]; then
    rm -f /etc/systemd/system/getty@tty1.service.d/autologin.conf
    rmdir /etc/systemd/system/getty@tty1.service.d 2>/dev/null || true
    systemctl daemon-reload
    ok "Removed autologin override"
fi

# Restore /etc/issue
if [ -f /etc/issue.bak ]; then
    mv /etc/issue.bak /etc/issue
    ok "Restored /etc/issue"
fi

# Remove journald + sysctl drop-ins
rm -f /etc/systemd/journald.conf.d/wc-limits.conf
rm -f /etc/sysctl.d/99-weatherclock-tuning.conf

# Remove management scripts
for s in wc-ctl wc-optimize; do
    rm -f "/usr/local/bin/$s"
done
ok "Removed management scripts"

# Remove app files (with prompt)
if [ -d /home/kiosk/weatherClock ]; then
    if confirm "Remove app directory /home/kiosk/weatherClock?"; then
        rm -rf /home/kiosk/weatherClock
        rm -f /home/kiosk/weatherClock.log /home/kiosk/weatherClock-stdout.log
        ok "Removed app directory"
    fi
fi

# Restore default .bash_profile (or empty it)
if [ -f /home/kiosk/.bash_profile ]; then
    if confirm "Reset /home/kiosk/.bash_profile to system default?"; then
        if [ -f /etc/skel/.bash_profile ]; then
            cp /etc/skel/.bash_profile /home/kiosk/.bash_profile
        else
            : > /home/kiosk/.bash_profile
        fi
        chown kiosk:kiosk /home/kiosk/.bash_profile
        ok "Reset bash_profile"
    fi
fi

# Remove kiosk user (optional)
if id kiosk >/dev/null 2>&1; then
    if confirm "Remove user 'kiosk' entirely (including home directory)?"; then
        userdel -r kiosk 2>/dev/null || true
        ok "Removed user kiosk"
    fi
fi

cat <<EOF

${GREEN}Uninstall complete.${NC}

A reboot is recommended to apply boot config changes:
  ${BLUE}sudo reboot${NC}

EOF

#!/bin/bash
#
# readonly-toggle.sh — enable/disable read-only root filesystem
#
# Wraps raspi-config's overlay filesystem feature. When enabled:
#   - / is mounted via overlayfs: writes go to RAM, discarded on reboot
#   - /boot/firmware is mounted read-only (no boot config drift)
#   - SD card sees ~zero writes during normal operation
#
# Tradeoffs:
#   - To change settings.json, the weatherclock log, or anything persistent,
#     you must temporarily disable, edit, re-enable
#   - System logs (journal) are also volatile — debugging requires care
#
# Usage:
#   sudo readonly-toggle.sh enable      Activate overlay (system goes read-only after reboot)
#   sudo readonly-toggle.sh disable     Restore writable rootfs
#   sudo readonly-toggle.sh status      Check current state
#
# A reboot is required after enable/disable.

set -euo pipefail

if [ "$EUID" -ne 0 ]; then
    echo "Run as root: sudo $0 $*"
    exit 1
fi

if ! command -v raspi-config >/dev/null 2>&1; then
    echo "raspi-config not found. This script requires Raspberry Pi OS."
    exit 1
fi

action="${1:-status}"

case "$action" in
    enable)
        echo "Enabling overlay filesystem..."
        # raspi-config's nonint API: do_overlayfs 0=enable, 1=disable
        # (yes, it's inverted — blame raspi-config conventions)
        raspi-config nonint do_overlayfs 0
        # Also lock /boot/firmware so no accidental writes
        raspi-config nonint do_bootro 0
        echo
        echo "Overlay enabled. The next reboot will mount / read-only."
        echo "After that, ALL changes will be lost on power-cycle except"
        echo "for explicit writes to tmpfs paths."
        echo
        echo "  ${YELLOW:-}Reboot now to activate: sudo reboot${NC:-}"
        ;;
    disable)
        echo "Disabling overlay filesystem..."
        raspi-config nonint do_overlayfs 1   # 1 = disable
        raspi-config nonint do_bootro 1      # 1 = boot writable
        echo
        echo "Overlay disabled. After reboot, the filesystem will be writable."
        echo "Make your changes, then re-run 'sudo $0 enable' before next deploy."
        echo
        echo "  Reboot now: sudo reboot"
        ;;
    status)
        echo "Current root filesystem state:"
        if mount | grep -q "overlay on / "; then
            echo "  ROOT: overlay (read-only, writes are temporary)"
        else
            echo "  ROOT: writable (normal mode)"
        fi
        if mount | grep -q " /boot/firmware .* ro"; then
            echo "  BOOT: read-only"
        else
            echo "  BOOT: writable"
        fi
        echo
        df -h / /boot/firmware 2>/dev/null || true
        ;;
    *)
        echo "Usage: $0 {enable|disable|status}"
        exit 1
        ;;
esac

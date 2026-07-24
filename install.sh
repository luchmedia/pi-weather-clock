#!/bin/bash
#
# pi-weather-clock installer
#
# Idempotent installer for Raspberry Pi OS Lite on a Pi Zero W (or Zero 2 W)
# with a HyperPixel 4.0 Square display.
#
# Usage:  sudo ./install.sh
#         sudo ./install.sh --unattended   # skip interactive prompts
#         sudo ./install.sh --uninstall    # rollback
#
# This script is safe to re-run: every step checks current state before
# making changes.

set -euo pipefail

# === Colors for output ===
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m'

msg()   { echo -e "${BLUE}[*]${NC} $*"; }
ok()    { echo -e "${GREEN}[OK]${NC} $*"; }
warn()  { echo -e "${YELLOW}[!]${NC} $*"; }
err()   { echo -e "${RED}[ERR]${NC} $*" >&2; }
step()  { echo; echo -e "${BLUE}===${NC} ${1} ${BLUE}===${NC}"; }

# === Helpers ===

require_root() {
    if [ "$EUID" -ne 0 ]; then
        err "This script must be run as root. Try: sudo $0"
        exit 1
    fi
}

require_pi() {
    if [ ! -f /sys/firmware/devicetree/base/model ]; then
        err "Not running on a Raspberry Pi (no devicetree model found)."
        exit 1
    fi
    local model
    model=$(tr -d '\0' < /sys/firmware/devicetree/base/model)
    msg "Detected: $model"
    case "$model" in
        *"Pi Zero W"*)
            PI_MODEL="zero_w"
            ;;
        *"Pi Zero 2"*)
            PI_MODEL="zero_2_w"
            ;;
        *"Pi 3"*|*"Pi 4"*|*"Pi 5"*)
            PI_MODEL="pi_3_4_5"
            warn "This is Pi 3/4/5 — overkill but should work. LED params differ."
            ;;
        *)
            warn "Unknown Pi model. Continuing anyway."
            PI_MODEL="unknown"
            ;;
    esac
}

# Patch a config file by adding a marked block. Idempotent: if the marker is
# already present, the block is replaced.
patch_config() {
    local target="$1"
    local marker="$2"  # comment line that marks the start of our block
    local content_file="$3"

    if [ ! -f "$target" ]; then
        err "Target file does not exist: $target"
        return 1
    fi
    local end_marker="# === end pi-weather-clock ==="
    if grep -qF "$marker" "$target"; then
        # Block exists — remove it then re-append
        msg "Existing block found in $target, replacing..."
        # Use sed to delete from marker to end_marker (inclusive)
        sed -i "/${marker//\//\\/}/,/${end_marker//\//\\/}/d" "$target"
    fi
    # Append the new block
    {
        echo ""
        echo "$marker"
        cat "$content_file"
        echo "$end_marker"
    } >> "$target"
}

# === Sanity checks ===

step "Pre-flight checks"
require_root
require_pi

REPO_DIR="$(cd "$(dirname "$0")" && pwd)"
msg "Repo directory: $REPO_DIR"

# Verify boot partition layout (modern: /boot/firmware/, legacy: /boot/)
if [ -d /boot/firmware ]; then
    BOOT_DIR="/boot/firmware"
elif [ -d /boot ]; then
    BOOT_DIR="/boot"
else
    err "Cannot find /boot/firmware/ or /boot/"
    exit 1
fi
msg "Boot directory: $BOOT_DIR"

# === Parse args ===

UNATTENDED=0
UNINSTALL=0
for arg in "$@"; do
    case "$arg" in
        --unattended) UNATTENDED=1 ;;
        --uninstall)  UNINSTALL=1 ;;
        --help|-h)
            cat <<EOF
Usage: sudo $0 [OPTIONS]

  --unattended   No interactive prompts (uses defaults / env vars)
  --uninstall    Rollback the installation
  --help, -h     Show this help

Environment variables (for unattended mode):
  WC_API_KEY     OpenWeatherMap API key
  WC_LATITUDE    Latitude (e.g. 45.4642)
  WC_LONGITUDE   Longitude (e.g. 9.1900)
  WC_LANGUAGE    Language code (e.g. en, it, de)

EOF
            exit 0
            ;;
    esac
done

if [ "$UNINSTALL" -eq 1 ]; then
    exec "$REPO_DIR/uninstall.sh"
fi

# === Step 1: APT packages ===

step "1/9  Installing system packages"

# Critical: include libgles2/libegl1/libgbm1 — these are NOT pulled in by
# python3-pygame on Bookworm and their absence causes "EGL not initialized"
# crashes that took us hours to debug. Always include them.

PACKAGES=(
    # Python runtime + libs the app uses
    python3-pygame
    python3-requests
    python3-pil
    # GPU acceleration libraries for SDL2 KMSDRM (do NOT remove)
    libgles2
    libegl1
    libgbm1
    libdrm2
    # Optional but useful tools
    curl
)

if ! dpkg -l "${PACKAGES[@]}" >/dev/null 2>&1; then
    msg "Updating package lists..."
    apt update -qq
    msg "Installing: ${PACKAGES[*]}"
    apt install -y "${PACKAGES[@]}"
fi
ok "Packages OK"

# === Step 2: Create kiosk user ===

step "2/9  Creating kiosk user"

if id kiosk >/dev/null 2>&1; then
    ok "User 'kiosk' already exists"
else
    useradd -m -s /bin/bash kiosk
    ok "Created user 'kiosk'"
fi

# Required groups: tty (write to /dev/tty1), video (DRM master),
# render (DRM render node), input (touchscreen events), audio (libsdl noise)
for group in tty audio video input render; do
    if ! id -nG kiosk | grep -qw "$group"; then
        usermod -aG "$group" kiosk
        msg "Added kiosk to group: $group"
    fi
done
ok "User and groups OK"

# === Step 3: Boot config ===

step "3/9  Patching boot configuration"

# /boot/firmware/config.txt
BOOT_CONFIG="$BOOT_DIR/config.txt"
if [ ! -f "${BOOT_CONFIG}.bak.original" ]; then
    cp "$BOOT_CONFIG" "${BOOT_CONFIG}.bak.original"
    msg "Backed up original config.txt"
fi

# Generate dynamic patch (LED params depend on Pi model)
TMP_PATCH=$(mktemp)
cat > "$TMP_PATCH" <<EOF
# GPU memory split: SDL2 KMSDRM needs at least 64MB for textures
gpu_mem=128

# HyperPixel 4.0 Square (720x720 DPI panel by Pimoroni)
dtoverlay=vc4-kms-v3d
dtoverlay=vc4-kms-dpi-hyperpixel4sq

# Disable Bluetooth (not used, frees ~3MB RAM and the UART)
dtoverlay=disable-bt

# Disable HDMI audio (the codec emits uevent change events every ~500ms
# on Pi+HyperPixel, causing systemd-udevd to consume CPU constantly and
# starve our Python app at boot. This took 2 days to diagnose, do not remove.)
# dtparam=audio=on  <- this is intentionally NOT enabled

# Suppress the rainbow firmware splash at boot
disable_splash=1

# HyperPixel needs display autodetect ON to bring up the DPI panel
# (counterintuitive given the name — it actually controls more than HDMI hotplug)
display_auto_detect=1
EOF

# LED params: differ by Pi model
case "$PI_MODEL" in
    zero_w|zero_2_w)
        cat >> "$TMP_PATCH" <<EOF

# LED management — Pi Zero W has only ACT (green) LED, logic INVERTED
# (activelow=on means the LED stays off when triggered, opposite of Pi 4/5)
dtparam=act_led_trigger=none
dtparam=act_led_activelow=on
EOF
        ;;
    pi_3_4_5)
        cat >> "$TMP_PATCH" <<EOF

# LED management — Pi 3/4/5 has both ACT (green) and PWR (red) LEDs
dtparam=act_led_trigger=none
dtparam=act_led_activelow=off
dtparam=pwr_led_trigger=none
dtparam=pwr_led_activelow=off
EOF
        ;;
    *)
        warn "Unknown Pi model — skipping LED config (you can add it manually)"
        ;;
esac

patch_config "$BOOT_CONFIG" "# === pi-weather-clock ===" "$TMP_PATCH"
rm -f "$TMP_PATCH"

# Also: comment out any existing 'dtparam=audio=on' (safe to do even if already)
sed -i 's/^dtparam=audio=on/#dtparam=audio=on/' "$BOOT_CONFIG"

ok "Boot config patched"

# /boot/firmware/cmdline.txt — silent boot
CMDLINE="$BOOT_DIR/cmdline.txt"
if [ ! -f "${CMDLINE}.bak.original" ]; then
    cp "$CMDLINE" "${CMDLINE}.bak.original"
fi

# Add silent boot flags (idempotent — only adds if not already present)
# IMPORTANT: console must stay tty1 — KMSDRM needs ownership of the system
# console TTY, and changing this to tty3 breaks the app entirely.
for flag in quiet loglevel=0 logo.nologo vt.global_cursor_default=0; do
    if ! grep -q "$flag" "$CMDLINE"; then
        sed -i "1s|\$| $flag|" "$CMDLINE"
        msg "Added cmdline flag: $flag"
    fi
done
ok "cmdline.txt patched (kept console=tty1)"

# === Step 4: Block HDMI audio modules ===

step "4/9  Blocking HDMI audio modules"

# blacklist alone doesn't work because vc4 pulls these in as dependencies.
# `install /bin/true` neutralizes them even when pulled by another module.
cat > /etc/modprobe.d/disable-hdmi-audio.conf <<'EOF'
# pi-weather-clock: prevent HDMI audio from loading.
# These modules generate continuous uevent storms on HyperPixel that cause
# CPU starvation during the Python app's init phase.
install snd_soc_hdmi_codec /bin/true
install snd_bcm2835 /bin/true
EOF
ok "modprobe.d/disable-hdmi-audio.conf written"

# === Step 5: Autologin on tty1 ===

step "5/9  Configuring autologin on tty1"

mkdir -p /etc/systemd/system/getty@tty1.service.d
cat > /etc/systemd/system/getty@tty1.service.d/autologin.conf <<'EOF'
[Service]
ExecStart=
# --noissue: don't print /etc/issue banner
# --noclear: don't clear the screen before login (we want it nero pulito)
# --autologin kiosk: log in as 'kiosk' automatically
ExecStart=-/sbin/agetty --noissue --noclear --autologin kiosk %I $TERM
EOF

# Empty /etc/issue so no banner appears
if [ -f /etc/issue ] && [ -s /etc/issue ]; then
    [ ! -f /etc/issue.bak ] && cp /etc/issue /etc/issue.bak
    : > /etc/issue
fi

systemctl daemon-reload
systemctl enable getty@tty1.service >/dev/null
ok "Autologin configured for kiosk on tty1"

# === Step 6: Install app files ===

step "6/9  Installing app files"

APP_DIR="/home/kiosk/weatherClock"
mkdir -p "$APP_DIR/fonts" "$APP_DIR/themes"

# Copy source files
for f in weather_clock.py icon_animations.py; do
    if [ -f "$REPO_DIR/src/$f" ]; then
        cp "$REPO_DIR/src/$f" "$APP_DIR/$f"
        msg "Installed src/$f"
    else
        err "Missing $REPO_DIR/src/$f"
        exit 1
    fi
done

# Copy settings template if no settings.json yet
if [ ! -f "$APP_DIR/settings.json" ]; then
    cp "$REPO_DIR/src/settings.example.json" "$APP_DIR/settings.json"
    msg "Created settings.json from template (you'll fill it in at step 8)"
fi

# Copy fonts if present in repo
if [ -d "$REPO_DIR/src/fonts" ]; then
    cp -r "$REPO_DIR/src/fonts/." "$APP_DIR/fonts/"
fi

chown -R kiosk:kiosk /home/kiosk
ok "App files installed in $APP_DIR"

# === Step 7: bash_profile watchdog ===

step "7/9  Installing bash_profile watchdog"

cp "$REPO_DIR/etc/kiosk/bash_profile" /home/kiosk/.bash_profile
chown kiosk:kiosk /home/kiosk/.bash_profile
chmod 644 /home/kiosk/.bash_profile
ok "bash_profile watchdog installed"

# Install management scripts
for s in wc-ctl wc-optimize; do
    if [ -f "$REPO_DIR/bin/$s" ]; then
        cp "$REPO_DIR/bin/$s" "/usr/local/bin/$s"
        chmod +x "/usr/local/bin/$s"
        msg "Installed /usr/local/bin/$s"
    fi
done

# systemd journald: keep logs small (we're on an SD card)
mkdir -p /etc/systemd/journald.conf.d
cat > /etc/systemd/journald.conf.d/wc-limits.conf <<'EOF'
[Journal]
SystemMaxUse=50M
SystemKeepFree=100M
RuntimeMaxUse=20M
Storage=auto
EOF

# Lower swappiness — SD is slow, prefer to OOM than thrash
echo "vm.swappiness=10" > /etc/sysctl.d/99-weatherclock-tuning.conf

ok "Management scripts installed"

# === Step 8: Interactive config of settings.json ===

step "8/9  Configuring settings.json"

SETTINGS="$APP_DIR/settings.json"

# Gather values: either from env vars (unattended) or prompts
get_value() {
    local var_name="$1"
    local prompt="$2"
    local default="$3"
    local env_value="${!var_name:-}"

    if [ -n "$env_value" ]; then
        echo "$env_value"
        return
    fi
    if [ "$UNATTENDED" -eq 1 ]; then
        echo "$default"
        return
    fi
    local input
    read -r -p "$prompt [$default]: " input </dev/tty
    echo "${input:-$default}"
}

API_KEY=$(get_value WC_API_KEY "OpenWeatherMap API key" "YOUR_API_KEY_HERE")
LAT=$(get_value WC_LATITUDE "Latitude (decimal, e.g. 45.4642)" "0.0")
LON=$(get_value WC_LONGITUDE "Longitude (decimal, e.g. 9.1900)" "0.0")
LANG=$(get_value WC_LANGUAGE "Language (en/it/de/fr/es)" "en")

# Patch settings.json in place
python3 - <<PYEOF
import json
p = "$SETTINGS"
d = json.load(open(p))
d["api_key"]   = "$API_KEY"
d["latitude"]  = float("$LAT")
d["longitude"] = float("$LON")
d["language"]  = "$LANG"
json.dump(d, open(p, "w"), indent=2, ensure_ascii=False)
print("settings.json updated")
PYEOF

chown kiosk:kiosk "$SETTINGS"
chmod 600 "$SETTINGS"  # API key is sensitive
ok "settings.json configured"

# === Step 9: Final summary ===

step "9/9  Installation complete"

cat <<EOF

${GREEN}╔════════════════════════════════════════════════════╗
║  pi-weather-clock installed successfully            ║
╚════════════════════════════════════════════════════╝${NC}

  App directory:    $APP_DIR
  Settings file:    $SETTINGS
  Service control:  sudo wc-ctl status|start|stop|restart|logs

  ${YELLOW}Next steps:${NC}
  1. (Optional) Edit settings to taste:
     ${BLUE}sudo -u kiosk nano $SETTINGS${NC}

  2. (Optional) Disable extra services to save RAM:
     ${BLUE}sudo wc-optimize${NC}

  3. (Optional) Enable read-only filesystem for SD longevity:
     ${BLUE}sudo $REPO_DIR/bin/readonly-toggle.sh enable${NC}

  4. Reboot:
     ${BLUE}sudo reboot${NC}

  After reboot, you should see the clock within ~45 seconds.

  If something goes wrong:
  - Check ${BLUE}sudo wc-ctl status${NC} and ${BLUE}sudo wc-ctl logs${NC}
  - See docs/TROUBLESHOOTING.md for common issues
  - The original boot config is at ${BOOT_CONFIG}.bak.original

EOF

if [ "$UNATTENDED" -eq 0 ]; then
    read -r -p "Reboot now? [y/N]: " reboot_now </dev/tty
    if [[ "$reboot_now" =~ ^[Yy]$ ]]; then
        msg "Rebooting..."
        sleep 2
        reboot
    fi
fi

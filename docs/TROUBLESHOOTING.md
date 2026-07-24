# Troubleshooting

Common problems and their fixes, ordered from most likely to most exotic.

## Diagnostic first

When something is wrong, start here:

```bash
# Status of everything
sudo wc-ctl status

# Last 50 events from the watchdog
sudo tail -50 /home/kiosk/weatherClock.log

# Verify display is detected by kernel
ls /dev/dri/
cat /sys/class/drm/card*/status

# Check who's holding the TTY
sudo fgconsole
sudo loginctl list-sessions
```

If you need Python stderr (e.g. for a tracebackable crash), enable stderr-to-file logging:

```bash
sudo wc-ctl stop
sudo sed -i 's|/usr/bin/python3 \./weather_clock.py$|/usr/bin/python3 -u ./weather_clock.py >> "$HOME/weatherClock-stdout.log" 2>\&1|' /home/kiosk/.bash_profile
sudo wc-ctl start
# Reproduce the crash
sudo tail -50 /home/kiosk/weatherClock-stdout.log
```

Don't forget to revert the log redirect later (in production it grows unbounded):
```bash
sudo sed -i 's|/usr/bin/python3 -u \./weather_clock.py >> .*$|/usr/bin/python3 ./weather_clock.py|' /home/kiosk/.bash_profile
```

---

## App crashes with `pygame.error: kmsdrm not available`

**Cause**: Python doesn't own the system console TTY.

**Common scenarios**:
1. You ran the app over SSH (you can't — KMSDRM needs `/dev/tty1`)
2. `console=tty3` is set in cmdline.txt (it must be `tty1`)
3. Another process is holding tty1

**Fix**:

```bash
# Verify cmdline
cat /proc/cmdline | grep -o "console=tty[0-9]*"
# Expected: console=tty1 (and only this — tty3 etc. don't work)

# If wrong, fix it
sudo sed -i 's|console=tty3 |console=tty1 |' /boot/firmware/cmdline.txt
sudo reboot
```

To test manually from SSH (without resetting tty1), use `openvt`:
```bash
sudo openvt -c 1 -f -s -- sudo -u kiosk bash -c '
  cd /home/kiosk/weatherClock
  export SDL_VIDEODRIVER=kmsdrm
  export SDL_RENDER_DRIVER=opengles2
  python3 weather_clock.py
'
```

---

## App crashes with `pygame._sdl2.sdl2.error: EGL not initialized`

**Cause**: Missing GPU libraries (libgles2, libegl1, libgbm1). This often happens when only `python3-pygame` is installed without explicit GPU deps.

**Fix**:
```bash
sudo apt install --reinstall libgles2 libegl1 libgbm1 libdrm2 -y
sudo wc-ctl restart
```

This is the #1 cause of crashes on a fresh install. The installer in this repo includes them by default.

---

## Black screen, no output at all

**Cause**: many possible — check in order:

```bash
# 1. Is DRM/KMS up?
ls /dev/dri/
# Expected: card0, renderD128. If empty, vc4 isn't binding properly.

# 2. Is the panel detected?
cat /sys/class/drm/card*-DPI-*/status
# Expected: connected

# 3. Is python running?
sudo wc-ctl status

# 4. Did vc4 crash during boot?
sudo dmesg | grep -iE "vc4|drm" | tail -20
```

If DRM `/dev/dri/` doesn't exist:
- Check `display_auto_detect=1` is in config.txt (counterintuitive but required for HyperPixel)
- Check `dtoverlay=vc4-kms-dpi-hyperpixel4sq` is set
- Check `dtoverlay=vc4-kms-v3d` is set
- Try `sudo reboot` (sometimes vc4 needs a clean restart after config changes)

---

## Constant crash loop with exit code 1

**Cause**: usually a Python-level error during init.

**Fix**: enable stderr logging (see Diagnostic First section above) and look at the traceback. Common things:

- `FileNotFoundError` for fonts or theme files
- `ConnectionError` if no network (app should handle this gracefully but check)
- `KeyError` on `settings.json` if a required field is missing (re-run installer with `--unattended` to repopulate)

---

## Crash loop with exit code 120

**Cause**: multiple python processes fighting for DRM master. Usually after restarting getty@tty1 many times or running `sudo openvt` while another instance is alive.

**Fix**:
```bash
sudo wc-ctl stop
sudo pkill -9 -f weather_clock
sudo systemctl reset-failed getty@tty1
sleep 3
sudo wc-ctl start
```

If it repeats, the `.bash_profile` should have an anti-duplicate check; verify it does:
```bash
sudo grep "Anti-duplicato" /home/kiosk/.bash_profile
```

If missing, re-run `install.sh` to refresh.

---

## App is running but display is black or stuck

**Cause**: usually `SDL_RENDER_DRIVER` not set, so SDL2 picked a backend that initializes OK but doesn't actually present frames.

**Fix**:
```bash
sudo grep "SDL_RENDER_DRIVER" /home/kiosk/.bash_profile
# Expected: export SDL_RENDER_DRIVER=opengles2

# If missing:
sudo sed -i '/^    export SDL_VIDEODRIVER=kmsdrm$/a\    export SDL_RENDER_DRIVER=opengles2' /home/kiosk/.bash_profile
sudo wc-ctl restart
```

---

## System feels slow, udev consuming CPU

**Cause**: HDMI audio polling storm.

**Verify**:
```bash
# uevent seqnum should be near-static after boot
cat /sys/kernel/uevent_seqnum; sleep 10; cat /sys/kernel/uevent_seqnum
# Difference > 20 → audio HDMI is still emitting events
```

**Fix**:
```bash
# Verify the modprobe override is in place
ls -la /etc/modprobe.d/disable-hdmi-audio.conf

# If missing or incomplete, recreate:
sudo tee /etc/modprobe.d/disable-hdmi-audio.conf > /dev/null <<'EOF'
install snd_soc_hdmi_codec /bin/true
install snd_bcm2835 /bin/true
EOF

# Also check audio is off in config.txt
grep -E "^(#)?dtparam=audio" /boot/firmware/config.txt
# Expected: #dtparam=audio=on  (commented out)

sudo reboot
```

After reboot, `lsmod | grep snd` should show no `snd_bcm2835` or `snd_soc_hdmi_codec`.

---

## App runs but framerate is terrible (1-5 FPS instead of ~30)

**Cause**: SDL2 fell back to the software renderer because GLES2 init failed.

**Check log** for which driver was actually chosen:
```bash
# Force INFO logging temporarily
sudo -u kiosk python3 -c "
import json
p='/home/kiosk/weatherClock/settings.json'
d=json.load(open(p))
d['log_level']='INFO'
json.dump(d, open(p,'w'), indent=2)
"
sudo wc-ctl restart
sleep 30
sudo journalctl _UID=$(id -u kiosk) -t weatherclock | grep -i "perf\|renderer\|driver"
```

Look for `SDL2 Renderer accelerated=1 target_texture=1 OK`. If `accelerated=0`, you're on software and need to fix EGL (see "EGL not initialized" above).

---

## LED still on after install

For Pi Zero W classic / Zero 2 W, the LED uses **inverted logic** (`activelow=on`):

```bash
grep "act_led" /boot/firmware/config.txt
# Expected:
#   dtparam=act_led_trigger=none
#   dtparam=act_led_activelow=on   ← MUST be 'on' for Pi Zero
```

For Pi 4/5, it's `activelow=off`. The installer picks the right one based on detected model.

Test runtime:
```bash
echo none | sudo tee /sys/class/leds/ACT/trigger
echo 1 | sudo tee /sys/class/leds/ACT/brightness
# LED should be off
```

---

## Touch input not working

**Verify the touchscreen is detected**:
```bash
ls /dev/input/event*
sudo libinput list-devices | grep -A 3 Touch
```

Expected: at least one event device labeled as a touchscreen.

**Check kiosk is in input group**:
```bash
id kiosk | tr ',' '\n' | grep -i input
# Should show: input(996) or similar
```

**Test event flow**:
```bash
sudo evtest /dev/input/event0  # adjust path
# Touch the screen - you should see ABS_X/ABS_Y events
```

If events arrive but the app doesn't react:
- Verify `SDL_MOUSE_TOUCH_EVENTS=1` and `SDL_TOUCH_MOUSE_EVENTS=1` are in `.bash_profile`
- Look for `FingerDown` events in INFO logs to confirm SDL is receiving them

---

## SD card wearing out fast

After a few months of use, SD cards can degrade. Symptoms: slow boot, occasional I/O errors in dmesg, file corruption, mysterious crashes.

**Prevention**:
- Use a class A2 or industrial-grade SD card
- Enable read-only filesystem mode (see [READONLY.md](READONLY.md))
- Run `wc-optimize` to disable services that write periodically
- Set `log_level: WARNING` in settings.json (not DEBUG/INFO)

**Diagnostics**:
```bash
sudo dmesg | grep -iE "mmc|i/o error|corruption" | tail -20
sudo dd if=/dev/mmcblk0 of=/dev/null bs=4M count=100 2>&1 | tail
# Should see ~20+ MB/s. If much slower, SD is degrading.
```

**Recovery**: clone to a new SD card. See [README.md → migration notes].

---

## Wrong moon phase shown

The app uses two sources for moon phase:

1. **OpenWeatherMap API** (`daily[0].moon_phase`) — astronomically accurate
2. **Local fallback** (`icon_animations.moon_phase()`) — approximate, ±2-3 hours

If the API hasn't responded yet (or you have no API key / no network), the local fallback runs. Its reference epoch is set in the code; it drifts by a few hours per decade.

**Verify which is being used**:
```bash
# Enable INFO logging, look for "MOON" or "moon_phase" lines
# (or temporarily add a print() to _get_moon_phase())
```

**Fix wrong moon**:
- Make sure the API key is correct and network works (look for `Weather fetch: HTTP 200` in logs)
- If using local fallback for long periods, the drift might warrant updating `_REF_NEW_MOON_TS` to a recent new moon timestamp

---

## "Aggiornato Xm fa" stuck at the same time

API isn't fetching, or `update_minutes` is too high.

```bash
# Check connectivity
ping -c 3 api.openweathermap.org

# Check API key validity (manual test)
curl "https://api.openweathermap.org/data/3.0/onecall?lat=45.46&lon=9.19&appid=YOUR_KEY"
# Should return JSON with weather data, not an "Invalid API key" error
```

If you're getting `429 Too Many Requests`, the daily quota is exhausted. Wait until tomorrow or get a paid plan.

---

## Reset to factory defaults

If you've heavily modified things and want to start fresh:
```bash
sudo ./uninstall.sh
sudo ./install.sh
```

This restores boot config from backups, recreates settings.json from template, and re-installs everything.

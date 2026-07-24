# Read-only filesystem (overlay mode)

Putting your Pi in read-only mode is the **single biggest thing you can do** to extend SD card lifespan. Without it, a 24/7 kiosk on a consumer SD card typically lasts 6-24 months. With it, the same setup can last 5+ years.

## How it works

Raspberry Pi OS has built-in support for an "overlay" filesystem mode (via raspi-config). It works like this:

```
                          BEFORE                 AFTER (overlay)
                          ──────                 ────────────────
/ (writable rootfs)       SD card                SD card (RO)
                              ↑                       ↑
                              │                       │  reads
                              │                       │
                              │                  overlay merged
                              │                  view ────────► RAM (tmpfs)
                              │                  ↑                ↑
                              │                  │                │ writes
write request ────────────────┘                  │                │
                                       app reads/writes  ────────┘
```

After enabling:
- Reading from `/`: served from the SD card (real data) or from the tmpfs overlay (recent writes)
- Writing to `/`: goes to RAM only (tmpfs overlay)
- On reboot: the tmpfs is discarded, system is back to the SD card state

## Trade-offs

### Pros
- **SD card lasts much longer** (no writes during normal operation)
- **System is bulletproof against bad shutdowns** — yank the power cord, no corruption
- **Filesystem stays exactly as you configured it** — no drift over time

### Cons
- **Settings changes are lost on reboot** unless you temporarily disable overlay first
- **Logs are lost on reboot** — debugging requires care
- **RAM usage grows** — every "write" stays in tmpfs until reboot. On a 24/7 kiosk that doesn't write much, this isn't a problem. But if your app accidentally fills `/tmp` or `/var/log` with gigabytes, you'll OOM.
- **Apt updates don't persist** — anything you `apt install` is lost on reboot

## When to enable

**After** you've finished setup, settings.json is configured, the clock is working, and you're happy with everything.

**Not** during the install phase (you need to be able to write to `/`).

## Enabling

```bash
sudo bin/readonly-toggle.sh enable
sudo reboot
```

After reboot, verify:
```bash
sudo bin/readonly-toggle.sh status
# Should show: ROOT: overlay (read-only, writes are temporary)

mount | grep " / "
# Should show overlay
```

## Making changes after enabling

The temporary disable → edit → re-enable workflow:

```bash
sudo bin/readonly-toggle.sh disable
sudo reboot
# After reboot, system is writable
sudo -u kiosk nano /home/kiosk/weatherClock/settings.json
sudo wc-ctl restart
# Verify your changes work
sudo bin/readonly-toggle.sh enable
sudo reboot
```

This requires 2 reboots. There's a way to do it without reboots using `mount -o remount`, but that's fragile and not officially supported. Better to just reboot.

## What still works during read-only

These don't write to SD (they use tmpfs or RAM):
- The app itself (settings.json is **read-only** but the app holds it in RAM)
- Network connections
- Watchdog log (goes to tmpfs)
- Weather API cache (goes to tmpfs)
- Python `__pycache__/` (goes to tmpfs)

These won't persist:
- New journal entries (gone on reboot)
- New crontab modifications
- Any file written to `/home/`, `/var/`, `/etc/`, `/root/`, etc.

## Recovery

If you ever boot into a broken state and can't reach SSH:
1. Insert SD into another computer
2. Edit `/boot/firmware/cmdline.txt` and remove `boot=overlay`
3. Boot Pi, fix the issue, re-enable overlay

Or just `sudo bin/readonly-toggle.sh disable` once you're back in.

## Notes

- Raspi-config's overlay also makes `/boot/firmware` read-only via `do_bootro` — protects boot configuration from accidental drift
- The overlay uses up RAM. Default tmpfs size is half of physical RAM. On a 512MB Pi, that's 256MB for writes — way more than the app will ever use
- If you do `sudo apt install foo` while overlay is enabled, it'll "work" but be gone on reboot. Disable overlay first if you want it persistent.

## When NOT to use it

- If you're actively developing/debugging on the device
- If your app writes substantial data that should persist (this app doesn't)
- If you frequently change settings (consider per-edit disable/enable workflow tedious)

For a "set and forget" kiosk, read-only mode is the right answer.

# Architecture

This document explains how the app works internally and why specific design choices were made. Useful if you want to modify behavior or debug exotic problems.

## Boot sequence

```
[Power on]
   ↓
Firmware (Raspberry Pi bootloader)         ~2-5s
   ↓  reads config.txt, loads kernel
[Linux kernel boot, quiet/silent]          ~5-10s
   ↓  runs systemd
[systemd brings up units]                  ~10-30s
   ↓  starts getty@tty1.service
[agetty autologin as kiosk]                ~1s
   ↓  loads /home/kiosk/.bash_profile
[bash_profile watchdog]                    ~5-30s
   ↓  shows splash, waits for udev/CPU
[python3 weather_clock.py]                 ~10s init + run
   ↓  initializes SDL2 KMSDRM
[Splash screens during texture build]      ~10-20s
   ↓
[Clock face visible]                       ← total: ~45-90 seconds
```

## SDL2 KMSDRM render pipeline

The app uses SDL2's direct framebuffer mode (KMSDRM) — no X server, no Wayland, no compositor.

```
Python (weather_clock.py)
    │
    │ uses pygame._sdl2.video.Window/Renderer
    │
    ▼
SDL2 (libsdl2-2.0)
    │
    │ SDL_VIDEODRIVER=kmsdrm
    │
    ▼
KMSDRM (kernel mode-setting + DRM)
    │
    │ via /dev/dri/card0
    │
    ▼
vc4 driver (kernel)
    │
    │ writes to VC4 hardware composer + DPI panel
    │
    ▼
HyperPixel 4.0 Square (720×720)
```

### Required environment variables (in `.bash_profile`)

```bash
export SDL_VIDEODRIVER=kmsdrm          # tells SDL2 to use KMSDRM, not X/Wayland
export SDL_RENDER_DRIVER=opengles2     # tells SDL2 Renderer to use GLES2 (vc4 supports)
export SDL_MOUSE_TOUCH_EVENTS=1        # bidirectional touch↔mouse event translation
export SDL_TOUCH_MOUSE_EVENTS=1
```

`SDL_RENDER_DRIVER=opengles2` is **critical** on Pi. Without it, SDL2 auto-selects a backend that may work but at sub-1-FPS speed on vc4 because the GL initialization path differs. We learned this the hard way.

### TTY ownership

KMSDRM requires the process to be the **owner of the system console TTY** (tty1). This means:
- The app must be launched from a getty on tty1 (so it inherits TTY ownership)
- Launching it via SSH fails with `pygame.error: kmsdrm not available`
- `console=tty3` in cmdline.txt breaks this — KMSDRM checks `/proc/consoles` for the active system console
- For testing, you can use `sudo openvt -c 1 -f -s -- ...` which forces a takeover of tty1

## Audio HDMI uevent storm

A peculiar Pi+HyperPixel issue: the kernel's vc4 driver polls the HDMI sound jack ~2 times per second to detect plug events. Even with HyperPixel (which is DPI, not HDMI), the polling finds the audio codec as "present" and emits `change` uevents for `/devices/.../sound/card0`.

This caused `systemd-udevd` to consume ~5% CPU continuously and create CPU starvation for the Python app during boot. We tried:
- `blacklist snd_bcm2835` → didn't work (module pulled in as dependency of vc4)
- `install snd_bcm2835 /bin/true` → works (intercepts module load)
- `dtparam=audio=off` → also helps

The installer applies both: comments out `dtparam=audio=on` in config.txt AND installs the modprobe override. Verify with:

```bash
cat /sys/kernel/uevent_seqnum; sleep 10; cat /sys/kernel/uevent_seqnum
# Expected: difference of 0-2 (was 20-30+ before the fix)
```

## Main app: weather_clock.py structure

```
weather_clock.py (~5700 lines)
├── Configuration dataclass        # parses settings.json
├── RenderBackend                  # wraps SDL2 Window/Renderer
├── WeatherFetcher                 # API client + caching + retry/backoff
├── icon_animations module         # procedural icon drawers + moon
├── WeatherClockSDL2 (main class)
│   ├── __init__                   # init SDL, pre-build textures, fetch weather
│   ├── _preload_icons             # pre-render sprite sheets (18 icons × N frames)
│   ├── Main loop:
│   │   ├── _handle_events         # touch, swipe gestures, mode changes
│   │   ├── _compute_render_signature  # decide whether to render this frame
│   │   ├── _draw_*                # mode-specific drawing
│   │   ├── renderer.present()     # flip to display
│   │   └── clock.tick(fps)        # framerate cap
│   └── ...
└── main()                         # arg parsing, app instantiation
```

### Render mode state machine

```
   ┌───────────────────────┐
   │  HANDS                │  ← analog clock face with hour icons
   │  - 12 weather icons   │
   │  - hour, minute, sec  │
   │  - moon, sun times    │
   └────────┬──────────────┘
            │ tap on icon          ▲ tap center
            ▼                      │
   ┌───────────────────────┐       │
   │  DETAIL               │───────┘
   │  - one hour expanded  │
   │  - paginate ±10h      │
   │  - swipe left/right   │
   └───────────────────────┘

   HANDS ──── swipe up ────► WEEKLY
   WEEKLY ── swipe down ───► HANDS
```

### Signature-based redraw skipping

To save CPU, the app computes a "signature" each frame — a tuple representing all data that could affect what's displayed. If the signature hasn't changed since the last render, we skip the draw step entirely (only clock ticks).

```python
sig = (mode, sec_bucket, minute, hour, weather_hash, moon_phase_bucket, ...)
if sig == last_sig:
    skipped += 1
else:
    render_frame()
    last_sig = sig
```

This is why idle CPU is ~5-10% even at 60 FPS: most frames the signature is the same and we skip.

When `smooth_seconds=true`, the signature includes a fractional bucket of the seconds hand, so every frame renders. Otherwise it changes only once per second and most frames are skipped.

## Texture pre-build at startup

To avoid stutter during animation, all icon frames are pre-rendered into pygame Surfaces during init, then uploaded as GPU Textures. For 18 weather icons × 20 frames at default settings, that's 360 textures totaling ~14 MB of VRAM.

Pre-build takes ~10-17 seconds on Pi Zero W. During this time, we show a splash screen ("Loading...", "Preparing icons...", "Uploading to GPU...", "Finalizing...") so the user knows the system isn't dead.

After init, `release_icon_sheets_after_boot` defaults to True, which frees the original (pre-upload) Surfaces from RAM once they're confirmed copied to GPU. Saves ~14 MB.

## Watchdog (.bash_profile)

The bash_profile acts as a supervisor with the following phases:

1. **Immediate clear** — hide TTY prompt artifacts before anything else paints
2. **System readiness gates**:
   - Wait for `systemctl is-system-running` to be `running` or `degraded`
   - `udevadm settle` (best effort)
   - Wait for udev workers to be idle (zero `(udev-worker)` processes for 5 sec)
   - Wait for `/sys/class/drm/card*-DPI-*/status` to be `connected`
   - Wait for CPU idle > 70% (avoid python init under CPU pressure)
   - Wait for free RAM > 100 MB
3. **Launch loop** with crash counter:
   - Run `python3 weather_clock.py`
   - If exit code != 0: increment failure counter, restart after 5s
   - After 5 consecutive failures, wait 5 minutes before retrying (avoid thrashing)
4. **Cleanup trap** on EXIT/INT/TERM/HUP — clears the screen so any error output doesn't linger

The watchdog is robust against:
- udev storms during boot
- DRM master not yet available
- Transient API errors (Python handles retries internally)
- Multiple-getty-restart scenarios (anti-duplicate check)

## Weather data caching

`WeatherFetcher` caches the last successful API response in `~/.cache/weatherClock/last.json`. On startup:
1. If cache file exists and is < 24h old, load it immediately (clock shows possibly-stale data right away)
2. Background fetch new data
3. Update display when fresh data arrives

This means the clock is usable even on cold boot before network is up (within reason — old data is still data).

Rate limiting: tracks daily API calls in the same cache file. The free OWM tier allows 1000 calls/day; the app caps at 900 to leave safety margin.

## Why no Tk/Qt/Kivy?

- **Tk**: requires X server, way too heavy
- **Qt**: similar; also massive deps
- **Kivy**: nice but big, slow startup on Pi Zero W
- **pygame + SDL2**: minimal deps, direct framebuffer access, perfect fit

We use the modern `pygame._sdl2.video` API (not the legacy `pygame.display.set_mode`) because it gives us GPU-accelerated `Texture` and `Renderer`, which are essential for smooth animation on Pi Zero W's weak CPU.

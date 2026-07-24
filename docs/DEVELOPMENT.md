# Development

How to modify the code, test changes, and contribute.

## Setup for development

The natural development loop is "edit on laptop, push to Pi, see result". A typical workflow:

### One-time: enable rsync sync

```bash
# On your laptop, in the cloned repo:
cat > sync-to-pi.sh << 'EOF'
#!/bin/bash
PI_HOST="${PI_HOST:-pi@raspberrypi.local}"
rsync -avz --exclude='.git' --exclude='__pycache__' \
    src/ "$PI_HOST:/tmp/wc-dev/"
ssh "$PI_HOST" "sudo cp /tmp/wc-dev/*.py /home/kiosk/weatherClock/ && \
                sudo chown kiosk:kiosk /home/kiosk/weatherClock/*.py && \
                sudo rm -rf /home/kiosk/weatherClock/__pycache__ && \
                sudo wc-ctl restart"
EOF
chmod +x sync-to-pi.sh
```

Then `./sync-to-pi.sh` pushes your local changes and restarts the app.

### Tail logs from laptop

```bash
ssh pi@raspberrypi.local "sudo tail -f /home/kiosk/weatherClock.log"
```

Or, for Python stderr (after enabling redirect — see TROUBLESHOOTING.md):
```bash
ssh pi@raspberrypi.local "sudo tail -f /home/kiosk/weatherClock-stdout.log"
```

## Running without a Pi (limited)

Most of `icon_animations.py` can run on a desktop with regular pygame:

```bash
# On a Linux laptop with pygame:
python3 << 'EOF'
import pygame
pygame.init()
import sys
sys.path.insert(0, 'src/')
import icon_animations
# Create a window and render an icon to inspect it
screen = pygame.display.set_mode((400, 400))
surf = icon_animations.create_animated_icon_sheet('01d', 200, 20, 2.0)
# ...
EOF
```

The full app needs SDL2 KMSDRM which requires a Pi (or a KMS-capable Linux with a real framebuffer).

## Code structure

```
src/
├── weather_clock.py        # ~5750 lines, main app
└── icon_animations.py      # ~930 lines, procedural icon drawers
```

### weather_clock.py — sections

Approximate line ranges (search for landmarks if line numbers drift):

| Section | Purpose |
|---|---|
| 1-200 | Imports, dataclass-like Configuration |
| 200-450 | `RenderBackend` — SDL2 wrapper |
| 450-1100 | Configuration dataclass with ~150 fields |
| 1100-1300 | `WeatherFetcher` — API client + cache |
| 1300-1700 | `WeatherClockSDL2.__init__` — startup, texture pre-build |
| 1700-2200 | Drawing helpers (`_draw_*`) |
| 2200-2800 | HANDS mode (analog clock) drawing |
| 2800-3500 | DETAIL mode drawing |
| 3500-4100 | WEEKLY mode drawing |
| 4100-4700 | Main loop, event handling, transitions |
| 4700-5400 | Edge cases, alerts, hot-reload |
| 5400-5750 | `main()`, arg parsing |

### icon_animations.py — sections

| Section | Purpose |
|---|---|
| 1-100 | Imports, constants |
| 100-340 | Icon drawing primitives (sun, cloud, rain particles, etc.) |
| 340-510 | `moon_phase()`, `_phase_name()`, lunar constants |
| 510-700 | `draw_moon_phase()`, `_apply_shadow`, maria/craters |
| 700-930 | Helper functions (`_add_glow`, `_is_position_illuminated`, etc.) |

## Style and conventions

The codebase is Italian-commented (the developer is Italian) with English for code identifiers. New contributions can be in either language for comments; code in English.

### Naming
- Public functions: `snake_case`
- Private/internal: `_leading_underscore`
- Classes: `CamelCase`
- Constants: `UPPER_CASE`

### Performance philosophy
This runs on Pi Zero W (single-core ARMv6, 512MB RAM). Always think about:
- Per-frame allocations → keep them minimal
- GPU vs CPU → prefer Textures over Surfaces when re-drawn often
- Cache anything that doesn't change every frame (icons, fonts, moon phase, etc.)
- Skip rendering when signature unchanged

### Adding a new setting
1. Add field to `Configuration` dataclass with type and default
2. Read it in the relevant `_draw_*` method
3. Document it in `docs/CONFIGURATION.md`
4. Mention hot-reload status (most settings hot-reload by default; if yours requires restart, note in `_apply_settings()` and docs)

## Testing changes

There's no formal test suite. Manual testing checklist:

1. **Visual**: clock face renders correctly at boot
2. **Animation**: weather icons animate smoothly
3. **Interaction**: tap an hour, swipe up/down — modes change
4. **Performance**: `Perf 10s: rendered=N skipped=M` log line shows reasonable values (N+M ≈ 600 at 60 FPS, with most frames skipped when idle)
5. **Network**: disconnect WiFi briefly, app continues showing cached data
6. **Long-run**: leave running for 24h, verify RAM doesn't grow (no leaks)
7. **Crash recovery**: `sudo wc-ctl stop && sudo wc-ctl start` works cleanly
8. **Reboot**: full power cycle, app comes back up within 1-2 minutes

For each: tail the log to spot warnings.

## Submitting changes

1. Fork the repo
2. Branch from `main`: `git checkout -b feature/your-feature`
3. Make changes, commit with clear message
4. Push, open a PR

Please:
- Include a screenshot/video of the visual change if applicable
- Mention what you tested (Pi model, OS version, display)
- Keep PRs focused (one logical change per PR)

## Common dev pitfalls

### Don't break the `_apply_settings()` hot reload
If you add a setting that requires a restart, don't try to handle it in `_apply_settings()` — instead, **explicitly mark it as needing restart** and document. Trying to hot-reload e.g. texture sizes leads to memory corruption.

### Don't allocate in the render path
Profile any new `_draw_*` method. If it allocates a `pygame.Surface` every frame, that's a bug — cache it.

### Be careful with float comparisons
For the signature mechanism, you want to bucket continuous values:
```python
# Wrong: every micro-change triggers a re-render
sig = (current_temp,)

# Right: bucket to one degree precision
sig = (int(current_temp),)
```

### Pi Zero W is ARMv6 — beware of pypi wheels
Pure-Python is fine. C extensions sometimes ship only x86_64 + aarch64 wheels, breaking on Pi Zero W. Stick with apt-installed packages where possible (pygame, requests, PIL all available via apt).

## Useful debug techniques

### Force log_level=INFO temporarily
```bash
sudo -u kiosk python3 -c "
import json
p='/home/kiosk/weatherClock/settings.json'
d=json.load(open(p))
d['log_level']='INFO'
json.dump(d, open(p,'w'), indent=2)
"
sudo wc-ctl restart
```

This reveals texture pre-build times, frame counts, mode transitions, etc.

### Check actual GPU memory
```bash
vcgencmd get_mem gpu       # configured
vcgencmd get_mem arm       # available to system
```

### Measure CPU per process
```bash
top -bn2 -p $(pgrep -f weather_clock | head -1) | tail -2
```

### Dump SDL2 environment of running process
```bash
sudo cat /proc/$(pgrep -f weather_clock | head -1)/environ | tr '\0' '\n' | grep SDL
```

Should show `SDL_VIDEODRIVER=kmsdrm` and `SDL_RENDER_DRIVER=opengles2`.

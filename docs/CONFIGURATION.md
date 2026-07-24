# Configuration reference

Every option in `settings.json` is documented here. Most settings support **hot reload** — edit the file and changes apply within ~1 second without restarting the app. The few exceptions are noted.

## API and location

| Key | Type | Default | Description |
|---|---|---|---|
| `api_key` | string | `"YOUR_API_KEY_HERE"` | OpenWeatherMap OneCall 3.0 API key. Get a free one at https://openweathermap.org/api/one-call-3 |
| `latitude` | float | `0.0` | Decimal latitude (e.g. `45.4642` for Milan) |
| `longitude` | float | `0.0` | Decimal longitude (e.g. `9.1900` for Milan) |
| `language` | string | `"en"` | OWM language code (`en`, `it`, `de`, `fr`, `es`, …) |
| `update_minutes` | int | `5` | How often to fetch fresh weather data (minutes) |

The free OWM tier allows 1000 calls/day. With `update_minutes=5`, you use 288/day, leaving plenty of headroom.

## Display and performance

| Key | Type | Default | Description |
|---|---|---|---|
| `fps` | int | `60` | Target framerate for the main loop |
| `smooth_seconds` | bool | `true` | If true, second hand moves smoothly at `fps`; if false, ticks once per second (lower CPU) |
| `screen_width` | int | `720` | Display width in pixels (HyperPixel 4.0 Square = 720) |
| `screen_height` | int | `720` | Display height in pixels |
| `fullscreen` | bool | `true` | Take the entire framebuffer |
| `render_scale_quality` | string | `"best"` | SDL2 scale filter: `"nearest"` (pixelated, fastest), `"linear"` (smooth), `"best"` (anisotropic where supported) |

## Antialiasing

The app supports per-element AA via supersampling (render at higher resolution, then downscale).

| Key | Values | Notes |
|---|---|---|
| `icon_antialias` | `"off"`, `"low"`, `"medium"`, `"high"`, `"ultra"`, `"max"` | `low`=×2, `high`=×4, `max`=×8. Affects weather icons. |
| `moon_antialias` | same as above | Affects the moon disk. `ultra` and `max` are sharper for big sizes. |
| `hands_antialias` | same as above | Clock hands. `max` recommended for the slim seconds hand. |

Higher AA = sharper output, more RAM and longer pre-build. On Pi Zero W, `icon_antialias="high"`, `moon_antialias="ultra"`, `hands_antialias="max"` is a good balance.

## Animations

| Key | Type | Default | Description |
|---|---|---|---|
| `animate_icons` | bool | `true` | Whether weather icons animate (rain falling, sun rays moving, etc.) |
| `animation_fps` | int | `8` | Animation playback rate (independent of main `fps`). 8 is smooth enough and saves CPU. |
| `animation_n_frames` | int | `20` | Number of pre-rendered frames per icon. More = smoother animation but more RAM. |
| `animation_loop_seconds` | float | `2.0` | Duration of one full animation cycle |

Total icon RAM ≈ `18 × n_frames × icon_size² × 4 × (icon_antialias_scale)²` bytes. With defaults: ~14 MB.

## Transitions

| Key | Type | Default | Description |
|---|---|---|---|
| `transition_duration_ms` | int | `250` | Generic fallback transition duration |
| `fade_duration_ms` | int | `400` | Fade transitions (mode changes) |
| `slide_duration_ms` | int | `450` | Slide transitions (within DETAIL mode, hour-to-hour) |
| `transition_easing` | string | `"out_cubic"` | Easing curve: `linear`, `in_quad`, `out_quad`, `in_out_quad`, `in_cubic`, `out_cubic`, `in_out_cubic`, `out_back`, `out_elastic` |

## Wind and temperature overlays

When enabled, small overlays appear on each hour position showing the forecast temperature or wind.

| Key | Type | Default | Description |
|---|---|---|---|
| `show_temperature` | bool | `true` | Show temperature overlays |
| `show_wind` | bool | `true` | Show wind speed overlays |
| `values_bg_alpha` | int | `220` | Background opacity (0-255) of the pill |
| `values_bg_adaptive` | bool | `false` | If true, pill background adapts to icon brightness |
| `values_temp_inset` | int | `6` | Distance from hour icon, temperature side |
| `values_wind_inset` | int | `6` | Distance from hour icon, wind side |
| `wind_gust_alert_threshold_ms` | float | `21.0` | Wind speed (m/s) above which gusts are highlighted |

## Moon

| Key | Type | Default | Description |
|---|---|---|---|
| `show_moon` | bool | `true` | Show moon phase indicator |
| `moon_lit_color` | string (hex) | `"#ebebe6"` | Color of the illuminated half |
| `moon_dark_color` | string (hex) | `"#282834"` | Color of the shadow half (deep blue) |
| `moon_show_label` | bool | `true` | Show phase name underneath (e.g. "Gibbosa crescente") |
| `current_moon_icon_size` | int | `48` | Size in pixels of the small moon in HANDS mode |

The moon phase value comes from the OWM API (`daily[0].moon_phase`), which is astronomically accurate. There's a local-calculation fallback if the API hasn't responded yet at boot.

## Sun times

| Key | Type | Default | Description |
|---|---|---|---|
| `show_sun_times` | bool | `true` | Show sunrise/sunset times on the dial |
| `sun_times_sunrise_color` | string (hex) | `"#ffa500"` | Color of the sunrise marker |
| `sun_times_sunset_color` | string (hex) | `"#5d6cb0"` | Color of the sunset marker |
| `sun_times_icon_size` | int | `28` | Size in pixels of the sun icons |

## Touch gestures

| Key | Type | Default | Description |
|---|---|---|---|
| `swipe_min_vertical_px` | int | `80` | Minimum vertical drag distance to register as a swipe |
| `swipe_max_horizontal_px` | int | `50` | Maximum horizontal drift allowed during a vertical swipe |

Vertical swipes drive a circular carousel. When there are **no** active alerts it toggles between HANDS (analog clock) and WEEKLY (7-day forecast), as before. When there **are** active alerts, the ALERTS report is inserted between them:

```
swipe down ↓ :  HANDS → ALERTS → WEEKLY → HANDS  (cyclic)
swipe up   ↑ :  HANDS → WEEKLY → ALERTS → HANDS  (cyclic)
```

Tap an hour position to enter DETAIL mode for that hour. Tap the alert banner (shown on HANDS/DIGITAL) to jump straight to the ALERTS report.

## Alerts report

A dedicated read-only page listing the active weather alerts (official OpenWeather alerts + the app's synthetic alerts). Reached by swiping vertically (carousel above) or by tapping the alert banner. The list shows every alert with a severity-coloured bar; tap a row to drill into the full text (official alerts carry OpenWeather's complete `description`, start/end times and sender). Swipe horizontally within a detail to move between alerts; tap or swipe vertically to go back. There is no manual dismiss — the list reflects the currently active alerts and clears itself when OpenWeather clears them. Auto-returns to HANDS after `alerts_timeout_seconds`.

| Key | Type | Default | Description |
|---|---|---|---|
| `alerts_timeout_seconds` | int | `60` | Seconds of inactivity before returning to HANDS (`0` = never) |
| `alerts_panel_width` | int | `440` | Panel width in px (kept inside the round case, like WEEKLY) |
| `alerts_panel_height` | int | `490` | Panel height in px |
| `alerts_row_height` | int | `62` | Height of each row in the list view |
| `alerts_header_font_size` | int | `24` | "Active alerts (N)" header size |
| `alerts_title_font_size` | int | `20` | Alert title size (list row + detail) |
| `alerts_meta_font_size` | int | `15` | Sender / time / horizon line size |
| `alerts_body_font_size` | int | `17` | Extended description size (detail view) |
| `alerts_line_spacing` | int | `3` | Extra px between wrapped body lines |
| `alerts_header_color` | hex string | `#cccccc` | Header colour |
| `alerts_title_color` | hex string | `#ffffff` | Title colour |
| `alerts_meta_color` | hex string | `#999999` | Meta line colour |
| `alerts_body_color` | hex string | `#dddddd` | Body text colour |
| `alerts_separator_color` | hex string | `#222232` | Row / divider line colour |
| `alerts_arrow_color` | hex string | `#666666` | Chevron (`›`) colour in the list |
| `alert_color_info` | hex string | `#3f7fbf` | Severity 1 (info) colour |
| `alert_color_warn` | hex string | `#e0872e` | Severity 2 (warning) colour |
| `alert_color_danger` | hex string | `#cc3333` | Severity 3 (danger) colour |

## Detail mode pagination dots

| Key | Type | Default | Description |
|---|---|---|---|
| `detail_page_dots_y_offset` | int | `200` | Vertical offset of the pagination dots from center |
| `detail_page_dots_spacing` | int | `20` | Horizontal spacing between dots |
| `detail_page_dot_radius` | int | `4` | Radius of each dot |

## Theme and fonts

| Key | Type | Default | Description |
|---|---|---|---|
| `theme` | string (optional) | unset | If set, name of a folder in app dir containing PNG icons named after OWM codes (`01d.png`, `01n.png`, ..., `50n.png`). If unset, procedural icons from `icon_animations.py` are used. |
| `font_name` | string | built-in | Path to a TTF font file. Relative paths are resolved from the app directory. Example: `"fonts/Inter-Regular.ttf"`. If unset or missing, falls back to pygame's built-in font. |
| `font_name_mono` | string | `"DejaVu Sans Mono"` | Monospaced font for the digital clock readout. Looked up by name in system font dirs. |

## Colors

| Key | Type | Description |
|---|---|---|
| `center_temp_color` | hex string | Color of the central temperature display in HANDS mode |
| `wind_hands_color` | hex string | Color of the wind direction "hands" overlay |
| `wind_hands_arrow_color` | hex string | Color of the wind arrow tip |

## Logging

| Key | Type | Default | Description |
|---|---|---|---|
| `log_level` | string | `"WARNING"` | One of `DEBUG`, `INFO`, `WARNING`, `ERROR`. INFO is useful for performance tuning (shows the "Perf 10s:..." line every 10 seconds). |
| `perf_log_interval_s` | int | `10` | How often to log performance summaries (frames rendered/skipped, current mode) |

## Sensitive: log_level on production

`log_level="DEBUG"` produces a lot of output. On an SD card, every log line is a write that wears the flash. For production, keep `WARNING` or `INFO`. Combine with the read-only filesystem mode for near-zero SD writes.

## Hot reload notes

These settings require a restart to apply:
- `screen_width`, `screen_height`, `fullscreen`
- `icon_antialias`, `moon_antialias`, `hands_antialias` (require re-building texture cache)
- `animation_n_frames` (cache rebuild)
- `font_name`, `font_name_mono`

Everything else applies within ~1 second of saving `settings.json`.

## Per-resolution presets

For non-720×720 displays (e.g. a 480×480 round display), you'll need to tune:
- `current_moon_icon_size`, `sun_times_icon_size`
- `values_temp_inset`, `values_wind_inset`
- Layout offsets are mostly relative to the screen center, so they scale automatically.

There's no automatic detection — set `screen_width`/`screen_height` to your panel and adjust visually.

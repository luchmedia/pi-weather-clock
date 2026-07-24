# Changelog

All notable changes to this project will be documented here.

The format is loosely based on [Keep a Changelog](https://keepachangelog.com/),
and this project adheres to [Semantic Versioning](https://semver.org/) starting from 1.0.0.

## [Unreleased]

### Added
- Weather alerts report page (`MODE_ALERTS`): a dedicated read-only view listing
  active OpenWeather alerts (plus the app's synthetic alerts), reachable two ways:
  tapping the on-screen alert banner, or a vertical swipe. Vertical swipes now form
  a circular carousel `HANDS → ALERTS → WEEKLY → HANDS` (ALERTS only present when
  alerts are active; otherwise the previous HANDS⇄WEEKLY toggle is preserved).
- Alerts report shows a severity-coloured list; tapping a row drills into the full
  text — official alerts now surface OpenWeather's complete `description`, sender
  and start/end times (previously fetched but discarded). Horizontal swipe pages
  between alert details.
- Configurable alerts-report styling (`alerts_*`, `alert_color_info/warn/danger`).

### Changed
- Tapping the alert banner now **opens** the alerts report instead of dismissing the
  alert (the report is read-only; alerts clear automatically when OpenWeather clears
  them).

### Fixed
- Clock now shows the time **of the configured location** (via the API's
  `timezone_offset`), not the host system's timezone. Applies to the analog hands,
  digital readout, the hour dial (icon placement + tap→hour mapping), sunrise/sunset,
  chart hour labels, per-hour DETAIL, and alert times. Freshness ("updated N min ago")
  intentionally stays in the system frame.
- Moon phase names are now localised: with `language: "en"` they render in English
  (New Moon, Waxing Crescent, …) instead of always showing the Italian names.

### Initial release
- Initial public release of the rewritten codebase
- Pure SDL2 KMSDRM rendering pipeline (no X server required)
- Astronomically-accurate moon phase via OpenWeatherMap API
- Animated weather icons (procedural drawers + optional Meteocons theme)
- Watchdog supervisor with graceful boot screens and crash recovery
- Three render modes: HANDS (analog clock), DETAIL (per-hour view), WEEKLY (7-day)
- Smooth fade and slide transitions between modes
- Touch gestures: tap to enter DETAIL, swipe to switch modes
- Anti-aliased clock hands with 8× supersampling
- Configurable settings.json with ~50 options and hot-reload
- Kiosk-grade hardening: udev silenced, audio HDMI disabled, services trimmed
- Installer script (`install.sh`) with hardware detection
- Read-only filesystem helper script
- Comprehensive documentation:
  - Architecture overview
  - Full configuration reference
  - Troubleshooting guide
  - Development guide
  - Read-only mode guide

### Credits
- Original concept and enclosure design: [KeepThisTicket/weatherClock](https://github.com/KeepThisTicket/weatherClock)

[Unreleased]: https://github.com/luchmedia/pi-weather-clock

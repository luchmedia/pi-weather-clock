#!/usr/bin/env python3
"""
weatherClock - Pygame edition (Phase 1: static icons, X11 driver)

Hardware target: Raspberry Pi Zero W + HyperPixel 4.0 Square (720x720)
Python:  3.11+
Display: Pygame 2.x via SDL2 on X11 (no Tk anymore)
API:     OpenWeather One Call API 3.0

Stack rationale (vs the Tk implementation in the backup):
  - Pygame + SDL2 gives us a frame-based render loop instead of widget tree
  - We get sub-second updates for free (lancetta secondi fluida) without
    paying the Tk widget overhead on every redraw
  - Touch events come straight via SDL_MOUSEBUTTON{DOWN,UP}; no more
    matchbox/binding gymnastics, no more bind_all dedup
  - Hardware: still software-rasterized on Pi Zero W (no GLES), but Pygame's
    sprite blit + dirty rect strategy is markedly cheaper than Tk Canvas
    item lookup-by-id

UI state machine (mode):
  HANDS    - default analog clock
             tap icon  -> DETAIL
             long-press 800ms center -> DIGITAL
  DETAIL   - hourly forecast detail
             tap same hour     -> reset 45s timer
             tap other hour    -> switch detail + reset timer
             SWIPE  left       -> next hour (+1)
             SWIPE  right      -> previous hour (-1)
             tap center        -> HANDS
             45s timeout       -> HANDS
  DIGITAL  - large digital clock at center
             tap icon          -> DETAIL
             tap center        -> HANDS
             60s timeout       -> HANDS
"""
from __future__ import annotations

import json
import logging
import math
import os
import signal
import sys
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from queue import Empty, Queue
from typing import Any, Optional

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# Hide pygame greeting message on import
os.environ.setdefault("PYGAME_HIDE_SUPPORT_PROMPT", "1")
# Disable SDL audio (we don't need sound and SDL would otherwise try to talk
# to pulseaudio/pipewire, causing wasted CPU + warnings on devices without audio)
os.environ.setdefault("SDL_AUDIODRIVER", "dummy")
import pygame
# SDL2 hardware-accelerated rendering API (Pygame 2.x).
# Marcata "experimental" nei docs ma stabile in pratica su 2.1+.
# Su Pi Zero W richiede Mesa GLES2 + driver fkms/kms.
from pygame._sdl2.video import Window, Renderer, Texture

# Local module: procedural icon animations
import icon_animations


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

BASE_DIR = Path(__file__).resolve().parent
CACHE_DIR = Path.home() / ".cache" / "weatherClock"
CACHE_FILE = CACHE_DIR / "last.json"
QUOTA_FILE = CACHE_DIR / "quota.json"
DEGREE = "\u00b0"

# Modes (kept compatible with Tk version for log readability)
MODE_HANDS = 0
MODE_DETAIL = 1
MODE_DIGITAL = 2
MODE_OFF = 3       # schermo nero: utile durante apt upgrade per liberare CPU
MODE_WEEKLY = 4    # previsione 7 giorni (giorno+1 a +7)
MODE_CHART = 5     # grafico temperatura 24h al centro (swipe oriz da HANDS/DIGITAL)
MODE_CURRENT = 6   # dati attuali completi al centro (swipe oriz da HANDS)
MODE_ALERTS = 7    # report allerte (tap sul banner / swipe verticale, carosello)


# ---------------------------------------------------------------------------
# Font paths: load direct from disk to avoid fc-list timeout on Pi Zero W.
# Pygame's SysFont() spawns 'fc-list' as subprocess which is slow + can timeout
# on slow devices. We hardcode the standard Debian/Raspberry Pi OS font paths.
# ---------------------------------------------------------------------------

FONT_PATHS: dict[tuple[str, bool], str] = {
    # Fontconfig "name" → path concreto. Aggiungi qui i font installati di
    # sistema che vuoi referenziare per nome. Per usare un font custom, basta
    # mettere un file .ttf in /home/kiosk/weatherClock/fonts/ (la dir
    # `fonts/` accanto allo script) e referenziarlo nel settings.json come:
    #   "font_name": "fonts/MioFont.ttf"   (path relativo a BASE_DIR)
    # oppure path assoluto:
    #   "font_name": "/path/assoluto/al/Font.ttf"
    ("DejaVu Sans", False):     "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    ("DejaVu Sans", True):      "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    ("DejaVu Sans Mono", False): "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf",
    ("DejaVu Sans Mono", True):  "/usr/share/fonts/truetype/dejavu/DejaVuSansMono-Bold.ttf",
    # Liberation (alternativa metrica-compatibile con Arial/Times/Courier)
    ("Liberation Sans", False):  "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
    ("Liberation Sans", True):   "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
    ("Liberation Mono", False):  "/usr/share/fonts/truetype/liberation/LiberationMono-Regular.ttf",
    ("Liberation Mono", True):   "/usr/share/fonts/truetype/liberation/LiberationMono-Bold.ttf",
    # Noto (utile per emoji, vietnamita, ecc.)
    ("Noto Sans", False):        "/usr/share/fonts/truetype/noto/NotoSans-Regular.ttf",
    ("Noto Sans", True):         "/usr/share/fonts/truetype/noto/NotoSans-Bold.ttf",
}


# ---------------------------------------------------------------------------
# Localization (same as Tk version)
# ---------------------------------------------------------------------------

DAY_NAMES: dict[str, list[str]] = {
    "en": ["Monday", "Tuesday", "Wednesday", "Thursday",
           "Friday", "Saturday", "Sunday"],
    "it": ["Lunedì", "Martedì", "Mercoledì", "Giovedì",
           "Venerdì", "Sabato", "Domenica"],
}

MONTH_NAMES: dict[str, list[str]] = {
    "en": ["January", "February", "March", "April", "May", "June",
           "July", "August", "September", "October", "November", "December"],
    "it": ["Gennaio", "Febbraio", "Marzo", "Aprile", "Maggio", "Giugno",
           "Luglio", "Agosto", "Settembre", "Ottobre", "Novembre", "Dicembre"],
}

DETAIL_LABELS: dict[str, dict[str, str]] = {
    "en": {"Day": "Day", "Hour": "Hour", "Temp": "Temp",
           "Feels": "Feels", "POP": "POP", "Rain": "Rain", "Wind": "Wind"},
    "it": {"Day": "Giorno", "Hour": "Ora", "Temp": "Temp",
           "Feels": "Percep.", "POP": "Prob.", "Rain": "Pioggia", "Wind": "Vento"},
}

FRESHNESS_LABELS: dict[str, str] = {
    "en": "updated {age} ago",
    "it": "aggiornato {age} fa",
}


def localize_day(language: str, dt: datetime) -> str:
    table = DAY_NAMES.get(language, DAY_NAMES["en"])
    return table[dt.weekday()]


def localize_month(language: str, dt: datetime) -> str:
    table = MONTH_NAMES.get(language, MONTH_NAMES["en"])
    return table[dt.month - 1]


def localize_label(language: str, key: str) -> str:
    table = DETAIL_LABELS.get(language, DETAIL_LABELS["en"])
    return table.get(key, key)


def format_age(seconds: int) -> str:
    if seconds < 60:
        return f"{seconds}s"
    minutes = seconds // 60
    if minutes < 60:
        return f"{minutes}m"
    hours = minutes // 60
    if hours < 24:
        return f"{hours}h"
    return f"{hours // 24}d"


# ---------------------------------------------------------------------------
# Config (Pygame-flavored: tuples for colors are RGB triples, not "#hex")
# ---------------------------------------------------------------------------

# Cache delle conversioni colore (evita parsing ripetuto a ogni frame).
# Le stringhe hex sono fisse nel config, quindi pochi entry totali.
_color_cache: dict[Any, tuple[int, int, int]] = {}


def parse_color(spec: Any, default: tuple[int, int, int] = (255, 255, 255)) -> tuple[int, int, int]:
    """Accept '#RRGGBB', '#RGB', [r,g,b], or (r,g,b)."""
    # Try cache first (only for hashable specs)
    try:
        cached = _color_cache.get(spec)
        if cached is not None:
            return cached
        hashable = True
    except TypeError:
        hashable = False
    if isinstance(spec, (list, tuple)) and len(spec) == 3:
        result = (int(spec[0]), int(spec[1]), int(spec[2]))
        if hashable:
            try:
                _color_cache[spec] = result
            except TypeError:
                pass
        return result
    if isinstance(spec, str):
        s = spec.strip()
        if s.startswith("#"):
            s = s[1:]
        if len(s) == 3 and all(c in "0123456789abcdefABCDEF" for c in s):
            s = "".join(c + c for c in s)
        if len(s) == 6 and all(c in "0123456789abcdefABCDEF" for c in s):
            result = (int(s[0:2], 16), int(s[2:4], 16), int(s[4:6], 16))
            _color_cache[spec] = result
            return result
    logging.warning("Color non valida %r, uso default %s", spec, default)
    return default


def safe_convert(surf: pygame.Surface, with_alpha: bool = False) -> pygame.Surface:
    """Helper: chiama Surface.convert() o convert_alpha() SOLO se il display
    è attivo (cioè è stato chiamato pygame.display.set_mode()).

    In SDL2 mode usiamo Window+Renderer senza set_mode, quindi convert*()
    fallisce con "Parameter 'surface' is invalid". In tal caso ritorniamo
    la Surface raw; sarà Texture.from_surface() a fare la conversione GPU.
    """
    if pygame.display.get_surface() is None:
        return surf
    try:
        if with_alpha:
            return surf.convert_alpha()
        return safe_convert(surf)
    except pygame.error:
        # Display ancora non pronto o conversione non supportata
        return surf


# ============================================================================
# RenderBackend: astrazione del rendering hardware-accelerated.
# ============================================================================
#
# Tre concetti chiave per chi viene dal Pygame software:
#
# 1. Renderer: oggetto SDL2 che disegna su una Window. Si "appiglia"
#    automaticamente come render target attivo. I disegni vanno in batch
#    fino a present(). I metodi base: clear(), present(), draw_line(),
#    fill_rect().
#
# 2. Texture: come una Surface ma vive in GPU memory. Si crea da una Surface
#    con Texture.from_surface(renderer, surf). Si blittasul renderer attivo
#    chiamando tex.draw(dstrect=..., angle=..., origin=...).
#    Non si possono modificare i pixel di una Texture (read-only).
#
# 3. Surface: l'API Pygame classica resta utile per:
#    - Comporre disegni complessi (font, draw.circle, ecc) come "scratchpad"
#    - Poi convertire in Texture per il blit finale GPU
#
# Pattern tipico:
#   surf = pygame.Surface((w, h), pygame.SRCALPHA)
#   pygame.draw.rect(surf, color, ..., border_radius=6)  # disegno complesso
#   tex = Texture.from_surface(renderer, surf)            # carico in GPU
#   tex.draw(dstrect=rect)                                # blit veloce GPU
#
# Per cache: tieni la Texture viva fino a quando il contenuto cambia.
# ============================================================================

class RenderBackend:
    """Wrapper sul Renderer SDL2 con utility per text/primitive caching.

    Su KMSDRM (Pi senza X11), usiamo SOLO il pattern SDL2 puro:
    Window() + Renderer(). NON chiamiamo pygame.display.set_mode() perche'
    questo crea un display surface "appiccicato" al framebuffer che entra
    in conflitto con il Renderer (errore "Surface already associated
    with window").

    Conseguenza: convert_alpha() e convert() non funzionano sui frame
    pygame perche' richiedono display.set_mode(). Soluzioni:
    - icon_animations gia' check display.get_surface() prima di convert
    - Nel codice principale: niente convert*() su Surface SDL2
    """

    def __init__(self, width: int, height: int, fullscreen: bool = True,
                  scale_quality: str = "linear"):
        self.width = width
        self.height = height
        # Imposta scale quality PRIMA di creare il renderer e le texture.
        # Valori: "nearest" (no AA su rotate/scale), "linear" (bilinear,
        # consigliato), "best" (anisotropic, fallback a linear).
        # Particolarmente importante per le lancette che ruotano ogni frame:
        # senza linear filtering, i bordi appaiono scalettati a angoli non
        # multipli di 90°. Costo: 1-2 cicli GPU/pixel su VC4 — trascurabile.
        if scale_quality not in ("nearest", "linear", "best"):
            scale_quality = "linear"
        # Pygame _sdl2 espone il SDL_SetHint via SDL_SetHint diretto;
        # la via portabile è l'env var SDL_RENDER_SCALE_QUALITY letta
        # da SDL all'inizializzazione.
        import os as _os
        _os.environ.setdefault("SDL_RENDER_SCALE_QUALITY", scale_quality)
        # In aggiunta: prova il hint via API SDL se disponibile
        try:
            import sdl2  # type: ignore
            sdl2.SDL_SetHint(b"SDL_RENDER_SCALE_QUALITY",
                             scale_quality.encode("ascii"))
        except ImportError:
            pass
        except Exception as e:
            logging.debug("SDL_SetHint not available: %s", e)
        logging.info("SDL scale quality: %s", scale_quality)
        # Window SDL2 pura: nessun set_mode chiamato.
        self.window = Window("WeatherClock", size=(width, height))
        if fullscreen:
            try:
                self.window.set_fullscreen(True)
            except Exception as e:
                logging.warning("set_fullscreen failed: %s", e)
        # accelerated=1 → richiede HW. target_texture=True per supportare le
        # transizioni fade (rendering off-screen su Texture buffer).
        try:
            self.renderer = Renderer(self.window, accelerated=1,
                                     target_texture=True)
            logging.info("SDL2 Renderer accelerated=1 target_texture=1 OK")
        except pygame.error as e:
            logging.warning("HW acceleration failed (%s), fallback SW", e)
            self.renderer = Renderer(self.window, accelerated=0,
                                     target_texture=True)
            logging.warning("SDL2 Renderer accelerated=0 (SW) — performance "
                            "WORSE than pure Pygame!")
        # Cache di Texture text: chiave = (text, color_tuple, font_id)
        # Le entry sono di norma pochissime (i testi cambiano raramente).
        self._text_cache: dict[tuple, Texture] = {}
        # Cache di Texture pill backgrounds: chiave = (w, h, color, alpha)
        self._pill_tex_cache: dict[tuple, Texture] = {}
        # Buffer Texture target per le transizioni fade (cached, riusati ad
        # ogni transizione). Sono full-screen 720×720. Lazy init in
        # get_transition_buffer() per evitare allocazione se mai usati.
        self._tx_buffer_a: Optional[Texture] = None
        self._tx_buffer_b: Optional[Texture] = None
        # Pulisci entrambi i back buffer (double buffering): clear + present 2x
        # così non vediamo "frame stale" dal buffer alternato al primo redraw.
        for _ in range(2):
            self.renderer.draw_color = (0, 0, 0, 255)
            self.renderer.clear()
            self.renderer.present()

    def get_transition_buffers(self) -> tuple[Texture, Texture]:
        """Crea (lazy) e restituisce 2 Texture target full-screen per fade.

        Riusate ad ogni transizione fade per evitare alloc/free del VRAM.
        """
        if self._tx_buffer_a is None:
            self._tx_buffer_a = Texture(self.renderer, (self.width, self.height),
                                        target=True)
            self._tx_buffer_a.blend_mode = 1
            self._tx_buffer_b = Texture(self.renderer, (self.width, self.height),
                                        target=True)
            self._tx_buffer_b.blend_mode = 1
            logging.info("Transition buffer textures create (2x %dx%d)",
                         self.width, self.height)
        return self._tx_buffer_a, self._tx_buffer_b

    def clear(self, color: tuple[int, int, int] = (0, 0, 0)) -> None:
        """Clear dello schermo (equivalente a screen.fill())."""
        self.renderer.draw_color = (*color, 255)
        self.renderer.clear()

    def present(self) -> None:
        """Flip finale (equivalente a pygame.display.flip())."""
        self.renderer.present()

    def blit_texture(self, texture: Texture,
                     dstrect: Optional[pygame.Rect] = None,
                     angle: float = 0.0,
                     origin: Optional[tuple[int, int]] = None) -> None:
        """Blit di una Texture (equivalente a screen.blit()).

        Pygame 2.6.1 API: texture.draw(dstrect=..., angle=..., origin=...).
        Nelle versioni precedenti era renderer.copy() — rimosso in 2.x final.
        """
        if angle == 0.0 and origin is None:
            texture.draw(dstrect=dstrect)
        else:
            # draw() con origin/angle: rotazione GPU
            texture.draw(dstrect=dstrect, angle=angle, origin=origin)

    def surface_to_texture(self, surf: pygame.Surface) -> Texture:
        """Crea una Texture a partire da una Surface (one-shot, costoso ~3-5ms
        per 720×720). Da usare nel boot o per cache infrequenti.

        Texture.from_surface() fa internamente la conversione del pixel format
        per la GPU, quindi NON chiamiamo convert*() prima (e non potremmo
        comunque perche' set_mode() non è stato chiamato).
        """
        return Texture.from_surface(self.renderer, surf)

    def get_text_texture(self, text: str, color: tuple[int, int, int],
                         font: pygame.font.Font) -> Texture:
        """Ottiene una Texture di testo, usando cache.

        La cache cresce un po' a runtime ma è limitata: i testi distinti
        sono nell'ordine delle decine (orari, valori meteo, label fissi).
        """
        key = (text, color, id(font))
        cached = self._text_cache.get(key)
        if cached is not None:
            return cached
        surf = font.render(text, True, color)
        tex = self.surface_to_texture(surf)
        self._text_cache[key] = tex
        return tex

    def get_pill_bg_texture(self, w: int, h: int,
                             color: tuple[int, int, int], alpha: int) -> Texture:
        """Ottiene una Texture di pill background (rect arrotondato).

        Per le pillole delle icone meteo: i parametri cambiano poco (3-5
        combinazioni totali in produzione).
        """
        key = (w, h, color, alpha)
        cached = self._pill_tex_cache.get(key)
        if cached is not None:
            return cached
        surf = pygame.Surface((w, h), pygame.SRCALPHA)
        pygame.draw.rect(surf, (*color, alpha), surf.get_rect(),
                         border_radius=6)
        tex = self.surface_to_texture(surf)
        self._pill_tex_cache[key] = tex
        return tex

    def clear_caches(self) -> None:
        """Invalida tutte le cache di Texture. Da chiamare su settings reload."""
        # Le Texture in pygame._sdl2 non hanno destroy() esplicito: il GC le
        # libera. Per essere certi, basta svuotare i dict.
        self._text_cache.clear()
        self._pill_tex_cache.clear()


@dataclass
class Config:
    # --- credentials / location ---
    api_key: str = ""
    latitude: float = 45.4642
    longitude: float = 9.1900
    units: str = "metric"

    # --- behavior ---
    title: str = "WeatherClock"
    # Intervallo polling weather API in minuti.
    # OpenWeatherMap One Call API 3.0 aggiorna i dati ogni 10 minuti server-side
    # (cf. https://openweathermap.org/api/one-call-3#start), quindi pollare più
    # frequente di 10 min può ritornare dati identici. Però conviene polling
    # ravvicinato per minimizzare la latenza dell'aggiornamento (es. valore
    # nuovo disponibile alle :00, polling alle :05 lo prende prima del default
    # :10). Default 5 min = 288 calls/day; floor 3 min = 480 calls/day.
    # Limite free tier OWM "One Call by Call": 1000 chiamate/giorno.
    update_minutes: int = 5
    daily_quota: int = 900
    log_level: str = "INFO"
    language: str = "it"
    fullscreen: bool = True

    # --- geometry (HyperPixel 4.0 Square: 720x720) ---
    screen_width: int = 720
    screen_height: int = 720
    fps: int = 15                  # main render loop FPS (15 is sane for clock)
    smooth_seconds: bool = False   # se True, lancetta secondi smooth a fps; False = salti 1s
    perf_log_interval_s: int = 60  # log periodico delle prestazioni (0 = disabilitato)
    center_x: int = 360
    center_y: int = 360
    radius: int = 280
    icon_size: int = 100
    hourly_touch_size: int = 110

    # --- background ---
    background_color: Any = "#000000"

    # --- icon animations (procedural) ---
    animate_icons: bool = True             # se False, icone statiche (frame fisso a t=0)
    animation_fps: int = 10                # frame al secondo dell'animazione icone
    animation_loop_seconds: float = 2.0    # durata di un loop completo
    animation_n_frames: int = 20           # frame pre-calcolati per icona (loop_seconds * animation_fps)
    # Anti-aliasing per le icone meteo procedurali e per la luna.
    # Preset semantici (consigliato): "off", "low", "medium", "high", "ultra"
    #   off    → scale 1 (no AA, boot più veloce)
    #   low    → scale 2 (AA buono, scalettature ridotte)
    #   medium → scale 3 (AA molto buono)
    #   high   → scale 4 (AA eccellente)
    #   ultra  → scale 6 (qualità massima, boot lento per le icone)
    # Puoi anche passare un intero direttamente (es. 8) per controllo fine.
    # L'AA influisce solo sul tempo di precompute al boot, non sul runtime:
    # le texture finali hanno la stessa dimensione e VRAM uguale.
    # ---
    # Icone meteo: 18 icone × N_FRAMES = molte texture. Boot più lento con
    # preset alti. Consigliato "low" o "medium" su Pi Zero W.
    icon_antialias: Any = "low"
    # Luna: 1 sola texture rebuildata ogni 7h. Costo trascurabile, puoi
    # alzare quanto vuoi.
    moon_antialias: Any = "high"
    # Lancette: 3 texture (ora/minuti/secondi) create UNA volta al boot.
    # Costo trascurabile, puoi alzare quanto vuoi. Con scale ≥3 i bordi
    # delle lancette appaiono molto più smooth quando ruotate.
    # Le lancette sono rettangoli sottili (2-7 px di larghezza) → l'AA
    # è più visibile rispetto alle icone perché ogni pixel del bordo
    # conta molto in proporzione.
    hands_antialias: Any = "high"

    # === Memory tuning ===
    # Rilascia le Surface CPU `icon_sheets` (~14 MB) DOPO che WEEKLY è
    # stato renderizzato almeno una volta. Le Texture GPU restano (icone
    # animate funzionano), e le cache WEEKLY/DETAIL/CHART hanno copie
    # GPU pre-cachate.
    # RISCHIO: se cambia `icon_size`/`weekly_icon_size` via hot-reload
    # dopo il release, le icone non potranno essere ri-scalate finché
    # un riavvio o un cambio di tema non ricarica gli spritesheet.
    # In pratica nessuno cambia queste dimensioni a runtime, quindi
    # default è True per Pi Zero W (RAM scarsa).
    release_icon_sheets_after_boot: bool = True

    # SDL2 render scale quality. Applicato globalmente a tutte le texture
    # ruotate o scalate dal GPU. Valori:
    #   "nearest" → nessun filtering (bordi scalettati su rotazioni)
    #   "linear"  → bilinear filtering (DEFAULT, smooth su rotate/scale)
    #   "best"    → anisotropic (fallback a linear se non supportato)
    # Particolarmente importante per le lancette che ruotano ogni frame.
    # Costo runtime trascurabile (1-2 cicli GPU/pixel sul VC4).
    render_scale_quality: str = "linear"

    # --- theme (icon dir name, relative to BASE_DIR) ---
    theme: str = "default"

    # --- hands ---
    hour_hand_length: int = 110
    hour_hand_color: Any = "#ffffff"
    hour_hand_width: int = 7
    minute_hand_length: int = 170
    minute_hand_color: Any = "#ffeb3b"
    minute_hand_width: int = 4
    second_hand_length: int = 200
    second_hand_color: Any = "#EB4034"
    second_hand_width: int = 2

    # --- tick marks (cerchi/pallini sui 12 punti dell'ora) ---
    show_tick_marks: bool = True
    tick_radius: int = 230            # distanza dal centro dei pallini (era 195-215 per stanghette)
    tick_dot_radius: int = 4          # raggio del pallino normale
    tick_dot_radius_major: int = 6    # raggio del pallino sulle ore 12/3/6/9
    tick_color: Any = "#888888"

    # --- overlay temp/wind on icons (pill) ---
    show_temperature: bool = True
    show_wind: bool = True
    values_color: Any = "#ffffff"
    values_bg_color: Any = "#1e1e1e"      # default: grigio molto scuro
    values_bg_alpha: int = 220             # 0=trasparente, 255=opaco
    values_bg_pad_x: int = 5
    values_bg_pad_y: int = 2
    values_temp_inset: int = 6             # margine dal bordo SUPERIORE dell'icona
    values_wind_inset: int = 6             # margine dal bordo INFERIORE dell'icona
    values_font_size: int = 12
    values_font_bold: bool = True
    # Colore pillola adattivo per condizione meteo (override values_bg_color
    # se True). Sole/sereno → arancio scuro, pioggia → blu scuro, ecc.
    values_bg_adaptive: bool = False

    # --- center temperature (HANDS mode) ---
    show_center_temp: bool = True
    center_temp_y_offset: int = -110      # ben più alto (distanziato dal centro)
    center_temp_x_offset: int = 0         # centrata (HANDS minimal: temp da sola)
    center_temp_font_size: int = 38       # leggermente più grande (era 32)
    center_temp_color: Any = "#aaaaaa"    # grigio (allineato a moon_label_color)

    # --- sunrise/sunset (HANDS mode, in cima sopra la temperatura) ---
    show_sun_times: bool = True
    sun_times_y_offset: int = -120        # in CIMA (era -55, sopra la temp)
    sun_times_font_size: int = 26         # era 32 (decisamente più piccolo)
    sun_times_color: Any = "#aaaaaa"
    sun_times_sunrise_color: Any = "#ffa500"
    sun_times_sunset_color: Any = "#5d6cb0"
    sun_times_icon_size: int = 28         # era 34
    sun_times_pair_gap: int = 30          # alba/tramonto molto vicini

    # --- moon phase (HANDS mode, simmetrico alla center_temp) ---
    show_moon: bool = True
    moon_y_offset: int = 60                # un po' più in alto (ora più piccola)
    moon_size: int = 90                    # ridotto (era 120, troppo grande nelle foto)
    moon_lit_color: Any = "#ebebe6"
    moon_dark_color: Any = "#282834"       # blu notte molto scuro
    moon_show_label: bool = True           # mostra il nome della fase sotto
    moon_label_font_size: int = 20         # ingrandito da 14
    moon_label_color: Any = "#aaaaaa"      # leggermente più chiaro del freshness

    # --- mini moon (HANDS minimal mode) ---
    # In HANDS si mostra solo: temperatura (al centro) + mini luna senza testo.
    # Per vedere i dati completi → swipe oriz → MODE_CURRENT.
    moon_mini_size: int = 70               # ingrandita (era 40, era troppo piccola)
    moon_mini_y_offset: int = 110          # ben più basso (simmetrico al temp -110)

    # --- CURRENT view (swipe oriz da HANDS) ---
    # Vista compatta con tutti i dati attuali: alba/tramonto, temp, percepita,
    # vento+cardinale, UV, luna+fase. Stile pulito su un pannello centrale.
    current_timeout_seconds: int = 45
    current_panel_width: int = 380          # come center_slide_width
    current_panel_height: int = 380
    current_header_font_size: int = 22
    current_header_color: Any = "#cccccc"
    current_label_font_size: int = 18
    current_label_color: Any = "#888888"
    current_value_font_size: int = 22
    current_value_color: Any = "#dddddd"
    current_moon_icon_size: int = 48

    # --- freshness ---
    show_freshness: bool = True
    freshness_y_offset: int = 200         # in basso, sotto la luna+nome più grande
    freshness_font_size: int = 16         # ingrandito da 11
    freshness_color: Any = "#888888"
    freshness_stale_color: Any = "#ff6666"
    freshness_stale_minutes: int = 30

    # --- alert banner ---
    # Pillola arrotondata centrata in alto, DENTRO il cerchio del case
    # (raggio ~330 dal centro). Width auto-fit al testo, altezza fissa.
    show_alerts: bool = True
    alert_bg_color: Any = "#cc3333"
    alert_text_color: Any = "#ffffff"
    alert_font_size: int = 14
    alert_height: int = 32                # altezza pillola
    alert_y_offset: int = -160            # dal centro: y abs = 360-160 = 200
                                           # (sotto icone 11/1 a y_bot=167,
                                           #  sopra temperatura a y=250)
    alert_padding_x: int = 16             # padding orizzontale interno
    alert_max_width: int = 420            # max larghezza per stare nel cerchio
    alert_border_radius: int = 16         # angoli arrotondati (pillola)
    alert_rotation_seconds: float = 5.0   # secondi tra rotazioni multi-allerta
    # Allerte meteo sintetiche basate sui weather condition codes OWM.
    # OWM non ha un campo dedicato per grandine, tornado severi, ecc:
    # questi vengono inferiti dai codes di weather[].id su current+hourly.
    # Riferimento: https://openweathermap.org/weather-conditions
    # IT, EN: testi mostrati per ogni categoria di alert sintetica.
    synthetic_alert_codes: dict = field(default_factory=lambda: {
        # code → (categoria, severità 1-3, testo_it, testo_en)
        # Tornado (gravissimo)
        781: ("tornado", 3, "Tornado",                "Tornado"),
        # Squall - raffiche violente improvvise
        771: ("squall",  3, "Burrasca / raffiche",    "Squalls"),
        # Pioggia estrema/violenta
        504: ("xrain",   3, "Pioggia estrema",        "Extreme rain"),
        503: ("xrain",   2, "Pioggia molto intensa",  "Very heavy rain"),
        # Pioggia gelata (pericoloso strade)
        511: ("frzrain", 3, "Pioggia gelata",         "Freezing rain"),
        # Neve abbondante
        602: ("hsnow",   2, "Neve abbondante",        "Heavy snow"),
        622: ("hsnow",   2, "Bufera di neve",         "Heavy snow shower"),
        # Sleet (neve mista pioggia, ghiaccio strade)
        611: ("sleet",   2, "Nevischio",              "Sleet"),
        612: ("sleet",   2, "Nevischio",              "Light sleet shower"),
        613: ("sleet",   2, "Nevischio",              "Sleet shower"),
        # Temporali intensi (probabile grandine)
        202: ("tstorm",  2, "Temporale con pioggia forte", "Thunderstorm with heavy rain"),
        212: ("tstorm",  2, "Temporale forte",        "Heavy thunderstorm"),
        221: ("tstorm",  2, "Temporale violento",     "Ragged thunderstorm"),
    })
    # Soglia per allerta vento forte (raffiche, m/s).
    # 75 km/h ≈ 21 m/s (scala Beaufort 9 "forte burrasca")
    wind_gust_alert_threshold_ms: float = 21.0
    # Soglia per allerta vento (sostenuto, m/s)
    wind_speed_alert_threshold_ms: float = 17.0  # ~60 km/h
    # Keyword grandine (cercati in weather.description: OWM non ha code
    # dedicato per grandine, ma a volte la menziona testualmente).
    hail_keywords: tuple = ("hail", "grandine")

    # --- alerts report (pagina dedicata: elenco + dettaglio) ---
    # Raggiungibile: (1) tap sul banner allerta in HANDS/DIGITAL,
    # (2) swipe verticale (carosello MAIN ⇄ ALLERTE ⇄ SETTIMANA).
    # Layout: elenco allerte; tap su una riga → dettaglio con testo completo.
    # Sola lettura: nessun dismiss manuale (il banner riflette le allerte
    # attive e sparisce da solo quando OpenWeather le rimuove).
    alerts_timeout_seconds: int = 60      # auto-ritorno a HANDS (0 = mai)
    alerts_panel_width: int = 440         # come WEEKLY: entra nel cerchio del case
    alerts_panel_height: int = 490
    alerts_row_height: int = 62           # altezza riga elenco
    alerts_header_font_size: int = 24
    alerts_title_font_size: int = 20      # titolo allerta (riga + dettaglio)
    alerts_meta_font_size: int = 15       # fonte / orario / orizzonte temporale
    alerts_body_font_size: int = 17       # descrizione estesa (solo dettaglio)
    alerts_line_spacing: int = 3          # px extra tra righe testo a capo
    alerts_header_color: Any = "#cccccc"
    alerts_title_color: Any = "#ffffff"
    alerts_meta_color: Any = "#999999"
    alerts_body_color: Any = "#dddddd"
    alerts_separator_color: Any = "#222232"
    alerts_arrow_color: Any = "#666666"   # chevron "›" nell'elenco
    # Colori per severità (1=info, 2=warning, 3=danger). La barra a sinistra
    # di ogni riga e la fascia del dettaglio usano questi colori.
    alert_color_info: Any = "#3f7fbf"     # severità 1
    alert_color_warn: Any = "#e0872e"     # severità 2
    alert_color_danger: Any = "#cc3333"   # severità 3

    # --- digital mode ---
    digital_timeout_seconds: int = 60
    long_press_ms: int = 800
    digital_color: Any = "#ffffff"
    digital_time_font_size: int = 72       # era 56
    digital_date_font_size: int = 22       # era 18
    digital_temp_font_size: int = 26       # era 22

    # --- chart mode (grafico temperatura 24h al centro) ---
    chart_timeout_seconds: int = 45
    chart_panel_width: int = 360
    chart_panel_height: int = 250
    chart_temp_color: Any = "#ffa500"     # arancio per la curva temperatura
    chart_temp_fill_alpha: int = 80       # alpha del fill sotto la curva
    chart_pop_color: Any = "#4fa3ff"      # blu per le barre POP
    chart_pop_alpha: int = 120            # alpha barre POP
    chart_axis_color: Any = "#666666"     # grigio scuro per assi
    chart_grid_color: Any = "#333333"     # grigio molto scuro per griglia
    chart_label_color: Any = "#aaaaaa"    # grigio chiaro per ticks/labels
    chart_label_font_size: int = 14
    chart_temp_label_font_size: int = 18  # min/max labels
    chart_now_marker_color: Any = "#ffffff"
    chart_sunrise_marker_color: Any = "#ffa500"
    chart_sunset_marker_color: Any = "#5d6cb0"
    chart_hours_to_show: int = 24
    chart_header_font_size: int = 24      # ridotto (era 32, troppo grande per panel piccolo)
    chart_header_color: Any = "#cccccc"

    # --- wind in HANDS mode (sotto sun_times, simmetrico) ---
    show_wind_hands: bool = True
    wind_hands_y_offset: int = -50        # stessa Y di center_temp (affiancato)
    wind_hands_x_offset: int = 50         # a destra del centro (con gap da temp)
    wind_hands_font_size: int = 38        # stesso font del temp (uniformati)
    wind_hands_color: Any = "#aaaaaa"     # grigio (allineato a moon_label_color)
    wind_hands_arrow_color: Any = "#aaaaaa"
    wind_hands_arrow_size: int = 26       # proporzionato al font 38

    # --- detail mode ---
    detail_label_color: Any = "#ffffff"
    detail_value_color: Any = "#ffffff"
    detail_label_font_size: int = 22       # era 18
    detail_value_font_size: int = 22       # era 18
    detail_line_spacing: int = 46          # era 50 (più compatto)
    detail_label_x_offset: int = -12
    detail_value_x_offset: int = 12
    detail_divider_color: Any = "#ffffff"
    detail_divider_width: int = 2
    detail_divider_half_height: int = 160  # era 180 (più compatto, evita uscita dal cerchio)
    detail_panel_y_offset: int = -25       # solleva il pannello (era 0 implicito)
    detail_timeout_seconds: int = 45
    detail_n_pages: int = 2

    # --- transition animations (tra modes) ---
    # SDL2 Fase 4: tipo di transizione deciso automaticamente dal codice
    # in base alla coppia (from_mode, to_mode). Vedi _pick_transition_style.
    # Settabile solo la durata; 0 disabilita tutte le transizioni.
    transition_duration_ms: int = 250
    # Override per tipo di transizione (entrambi opzionali, default = -1 che
    # significa "usa transition_duration_ms"). Permette di avere fade veloci
    # e slide più lunghe (gli slide si sentono "snappy" con durate maggiori
    # perché c'è movimento spaziale da percepire).
    # Esempio configurazione consigliata in settings.json:
    #   "fade_duration_ms":  300   # fade un po' più rilassati
    #   "slide_duration_ms": 450   # slide ben percepibili
    # Setta a 0 per disabilitare un tipo specifico (es. solo fade, no slide).
    # Setta a -1 (default) per ereditare da transition_duration_ms.
    fade_duration_ms: int = -1
    slide_duration_ms: int = -1
    # Curva di easing per le animazioni di transizione (fade/slide).
    # La curva trasforma il progresso lineare t∈[0,1] (frazione tempo
    # trascorso) in un valore t_eased∈[0,1] che controlla la posizione
    # finale. Curve disponibili (vedi _ease() per dettagli):
    #   "linear"       - costante (no easing). Sembra "robotico".
    #   "out_cubic"    - decelerazione liscia (DEFAULT, originale)
    #   "in_out_cubic" - accelera + decelera ("swoop" più drammatico)
    #   "out_quint"    - decelerazione rapida poi morbida (snappy)
    #   "out_expo"     - decelerazione esponenziale (super snappy)
    #   "out_back"     - overshoot leggero alla fine ("bouncy"/playful)
    transition_easing: str = "out_cubic"
    # Area di slide orizzontale per HANDS↔CHART↔DIGITAL (cerchio interno).
    # Limita il "centro" che slida orizzontalmente, in modo che le icone
    # del quadrante restino sempre visibili e l'animazione sia "contenuta"
    # entro un cerchio più piccolo di quello delle icone.
    # Su display 720×720, le icone sono su un cerchio di raggio ~290 (config
    # icon_radius). L'area di slide è 460×460 centrata = entra dentro.
    center_slide_width: int = 380
    center_slide_height: int = 380

    weekly_timeout_seconds: int = 60
    # Header
    weekly_header_font_size: int = 24
    weekly_header_color: Any = "#cccccc"
    # Day name
    weekly_day_font_size: int = 22               # ridotto (era 24, per non overlap con icona)
    weekly_day_color: Any = "#ffffff"
    # Temperature
    weekly_temp_font_size: int = 26              # era 24
    weekly_temp_max_color: Any = "#ffa500"
    weekly_temp_min_color: Any = "#5d6cb0"
    weekly_temp_sep_color: Any = "#666666"
    # POP
    weekly_pop_font_size: int = 16               # ingrandito (era 14)
    weekly_pop_color: Any = "#4fa3ff"
    # Stat secondarie (font 14, leggibile)
    weekly_stat_font_size: int = 14              # era 12
    weekly_stat_label_color: Any = "#888888"
    weekly_stat_value_color: Any = "#cccccc"
    weekly_uv_low_color: Any = "#7ec850"
    weekly_uv_mid_color: Any = "#f0b03b"
    weekly_uv_high_color: Any = "#e74c3c"
    weekly_wind_arrow_color: Any = "#cccccc"
    # Icone meteo
    weekly_icon_size: int = 48                   # era 44 (più grande, ben visibili)
    # Layout (panel 440×490 entra in cerchio raggio 330: corner=329)
    weekly_row_height: int = 58
    weekly_row_spacing: int = 3
    weekly_panel_width: int = 440
    weekly_panel_height: int = 490
    weekly_col_left_width: int = 140             # col sx compatta (nome abbr + icona)
    weekly_col_separator_color: Any = "#222232"
    weekly_row_separator_color: Any = "#1a1a25"

    # --- swipe gesture (DETAIL only) ---
    # Swipe ORIZZONTALE cambia ora (-1h/+1h)
    swipe_min_horizontal_px: int = 80     # min orizzontale per riconoscere swipe orizz
    swipe_max_vertical_px: int = 50       # max verticale per swipe orizz (oltre = è verticale)
    # Swipe VERTICALE cambia pagina DETAIL (su=succ, giù=prec)
    swipe_min_vertical_px: int = 80       # min verticale per riconoscere swipe vert
    swipe_max_horizontal_px: int = 50     # max orizz per swipe vert
    swipe_max_duration_ms: int = 500      # tempo max press→release (più lento = tap)

    # --- detail pages ---
    detail_page_dots_y_offset: int = 200   # offset Y dal centro per i pallini indicatore
    detail_page_dots_spacing: int = 20     # spaziatura orizzontale tra pallini
    detail_page_dot_radius: int = 4        # raggio pallino
    detail_page_dot_color_active: Any = "#ffffff"
    detail_page_dot_color_inactive: Any = "#555555"

    # --- offline cache ---
    cache_enabled: bool = True

    # --- hot reload ---
    settings_hot_reload: bool = True

    # --- font selection ---
    # Pygame cerca per nome (system font); se non c'è usa default.
    font_name: str = "DejaVu Sans"
    font_name_mono: str = "DejaVu Sans Mono"

    @staticmethod
    def _resolve_aa(value) -> int:
        """Converte un preset AA ("off"/"low"/"medium"/"high"/"ultra") o
        un intero in un valore numerico da passare a smoothscale.

        Accetta:
          - stringa preset: case-insensitive, mappata a un intero
          - int diretto: usato as-is (clampato a [1, 8] per sicurezza)
        Fallback su 2 ("low") se valore non riconoscibile.
        """
        AA_PRESETS = {
            "off":    1,
            "none":   1,
            "low":    2,
            "medium": 3,
            "mid":    3,
            "high":   4,
            "ultra":  6,
            "max":    8,
        }
        if isinstance(value, str):
            return AA_PRESETS.get(value.strip().lower(), 2)
        if isinstance(value, (int, float)):
            v = int(value)
            return max(1, min(8, v))
        return 2

    @property
    def icon_antialias_scale(self) -> int:
        """Scale numerico risolto da icon_antialias (preset o int)."""
        return self._resolve_aa(self.icon_antialias)

    def resolve_transition_duration(self, style: str) -> int:
        """Restituisce la durata corretta per il tipo di transizione.

        style: "fade", "slide_left", "slide_right", "slide_up", "slide_down"
        Fallback: -1 → eredita transition_duration_ms.
        Setta a 0 per disabilitare quel tipo specifico.
        """
        if style == "fade":
            v = self.fade_duration_ms
        elif style.startswith("slide"):
            v = self.slide_duration_ms
        else:
            v = -1
        # -1 = eredita; ogni altro valore (incluso 0) è override esplicito
        if v < 0:
            return self.transition_duration_ms
        return v

    @property
    def moon_antialias_scale(self) -> int:
        """Scale numerico risolto da moon_antialias (preset o int)."""
        return self._resolve_aa(self.moon_antialias)

    @property
    def hands_antialias_scale(self) -> int:
        """Scale numerico risolto da hands_antialias (preset o int)."""
        return self._resolve_aa(self.hands_antialias)

    def validate(self) -> None:
        errors: list[str] = []
        if not self.api_key:
            errors.append("api_key vuoto")
        elif "INSERISCI" in self.api_key.upper() or "YOUR_API_KEY" in self.api_key.upper():
            errors.append("api_key contiene ancora un placeholder")
        try:
            lat = float(self.latitude)
            if not -90 <= lat <= 90:
                errors.append(f"latitude={lat} fuori range [-90, 90]")
        except (TypeError, ValueError):
            errors.append(f"latitude non numerica: {self.latitude!r}")
        try:
            lon = float(self.longitude)
            if not -180 <= lon <= 180:
                errors.append(f"longitude={lon} fuori range [-180, 180]")
        except (TypeError, ValueError):
            errors.append(f"longitude non numerica: {self.longitude!r}")
        if self.units not in ("metric", "imperial", "standard"):
            errors.append(f"units invalido: {self.units!r}")
        if self.language not in DAY_NAMES:
            errors.append(f"language invalido: {self.language!r}")
        if self.update_minutes < 1:
            errors.append(f"update_minutes={self.update_minutes}")
        if not 100 <= self.daily_quota <= 1000:
            errors.append(f"daily_quota fuori [100,1000]: {self.daily_quota}")
        if not 2 <= self.fps <= 60:
            errors.append(f"fps fuori [2,60]: {self.fps}")
        # Validazione AA: stringa preset valida o int [1,8]
        valid_presets = {"off", "none", "low", "medium", "mid", "high",
                          "ultra", "max"}
        for attr in ("icon_antialias", "moon_antialias", "hands_antialias"):
            val = getattr(self, attr)
            if isinstance(val, str):
                if val.strip().lower() not in valid_presets:
                    errors.append(f"{attr}={val!r} non valido. "
                                   f"Usa: off/low/medium/high/ultra o un int 1-8")
            elif isinstance(val, (int, float)):
                if not 1 <= int(val) <= 8:
                    errors.append(f"{attr}={val} fuori [1,8]")
            else:
                errors.append(f"{attr}={val!r} deve essere stringa o int")
        if self.render_scale_quality not in ("nearest", "linear", "best"):
            errors.append(f"render_scale_quality={self.render_scale_quality!r} "
                           f"non valido. Usa: nearest/linear/best")
        if self.alerts_timeout_seconds < 0:
            errors.append(f"alerts_timeout_seconds={self.alerts_timeout_seconds} < 0")
        for attr in ("alerts_panel_width", "alerts_panel_height",
                     "alerts_row_height"):
            val = getattr(self, attr)
            if not isinstance(val, int) or val <= 0:
                errors.append(f"{attr}={val!r} deve essere int > 0")
        if errors:
            raise ValueError("settings.json non valido:\n  * " + "\n  * ".join(errors))

    @classmethod
    def load(cls, path: Path) -> "Config":
        with path.open() as f:
            raw = json.load(f)
        valid_keys = set(cls.__dataclass_fields__.keys())
        filtered = {k: v for k, v in raw.items() if k in valid_keys}
        unknown = set(raw.keys()) - valid_keys
        if unknown:
            logging.warning("Ignoro chiavi settings.json sconosciute: %s", sorted(unknown))
        return cls(**filtered)


# ---------------------------------------------------------------------------
# Weather fetcher (identical to Tk version: it's UI-agnostic)
# ---------------------------------------------------------------------------

class WeatherFetcher(threading.Thread):
    URL = "https://api.openweathermap.org/data/3.0/onecall"
    # Floor sotto il quale clamp-iamo update_minutes. 3 min = 480 calls/day,
    # safe sotto il limite OWM free di 1000/day anche con margine per chiamate
    # spot causate da SIGUSR (force refresh) o restart del processo.
    MIN_UPDATE_MINUTES = 3

    def __init__(self, cfg: Config, out_queue: "Queue[dict]"):
        super().__init__(daemon=True, name="WeatherFetcher")
        self.cfg = cfg
        self.queue = out_queue
        self._stop = threading.Event()
        self.session = self._build_session()
        if cfg.update_minutes < self.MIN_UPDATE_MINUTES:
            logging.warning("update_minutes=%d sotto il minimo %d, clamp",
                            cfg.update_minutes, self.MIN_UPDATE_MINUTES)
            self._interval_seconds = self.MIN_UPDATE_MINUTES * 60
        else:
            self._interval_seconds = cfg.update_minutes * 60
        self._calls_today, self._counter_day = self._load_quota()
        logging.info("WeatherFetcher: interval=%ds, daily_quota=%d, "
                     "counter all'avvio: %d (giorno UTC %s)",
                     self._interval_seconds, cfg.daily_quota,
                     self._calls_today, self._counter_day)

    @staticmethod
    def _today_utc() -> str:
        return datetime.now(timezone.utc).strftime("%Y-%m-%d")

    def _load_quota(self) -> tuple[int, str]:
        today = self._today_utc()
        try:
            p = json.loads(QUOTA_FILE.read_text())
            if p.get("day_utc") == today:
                return int(p.get("calls", 0)), today
            logging.info("Quota file e' del %s, oggi e' %s: reset", p.get("day_utc"), today)
        except (OSError, ValueError, KeyError) as e:
            logging.debug("Nessun quota file utilizzabile: %s", e)
        return 0, today

    def _save_quota(self) -> None:
        try:
            CACHE_DIR.mkdir(parents=True, exist_ok=True)
            payload = {"day_utc": self._counter_day, "calls": self._calls_today}
            tmp = QUOTA_FILE.with_suffix(".tmp")
            tmp.write_text(json.dumps(payload))
            tmp.replace(QUOTA_FILE)
        except OSError as e:
            logging.warning("Quota save failed: %s", e)

    def _maybe_reset_counter(self) -> None:
        today = self._today_utc()
        if today != self._counter_day:
            logging.info("UTC day rollover %s->%s; counter reset (was %d/%d)",
                         self._counter_day, today, self._calls_today, self.cfg.daily_quota)
            self._calls_today = 0
            self._counter_day = today
            self._save_quota()

    @staticmethod
    def _build_session() -> requests.Session:
        s = requests.Session()
        retry = Retry(total=2, backoff_factor=2.0,
                      status_forcelist=(500, 502, 503, 504),
                      allowed_methods=("GET",), raise_on_status=False)
        s.mount("https://", HTTPAdapter(max_retries=retry))
        s.headers.update({"User-Agent": "weatherClock-pygame/1.0"})
        return s

    def stop(self) -> None:
        self._stop.set()

    def _save_cache(self, data: dict) -> None:
        if not self.cfg.cache_enabled:
            return
        try:
            CACHE_DIR.mkdir(parents=True, exist_ok=True)
            payload = {"fetched_at": time.time(), "data": data}
            tmp = CACHE_FILE.with_suffix(".tmp")
            tmp.write_text(json.dumps(payload))
            tmp.replace(CACHE_FILE)
            if not getattr(self, "_cache_ever_saved", False):
                self._cache_ever_saved = True
                logging.info("Cache salvata per la prima volta in %s", CACHE_FILE)
        except OSError as e:
            logging.warning("Cache save failed: %s", e)

    def fetch_once(self) -> Optional[dict]:
        self._maybe_reset_counter()
        if self._calls_today >= self.cfg.daily_quota:
            logging.warning("Quota giornaliera raggiunta (%d/%d); skip",
                            self._calls_today, self.cfg.daily_quota)
            return None
        params = {
            "lat": self.cfg.latitude, "lon": self.cfg.longitude,
            "exclude": "minutely",
            "appid": self.cfg.api_key, "units": self.cfg.units,
            "lang": self.cfg.language,
        }
        try:
            r = self.session.get(self.URL, params=params, timeout=15)
            self._calls_today += 1
            self._save_quota()
            logging.info("Weather fetch: HTTP %d  (calls today: %d/%d)",
                         r.status_code, self._calls_today, self.cfg.daily_quota)
            if r.status_code == 401:
                logging.error("HTTP 401: api_key invalida o subscription mancante")
                return None
            if r.status_code == 429:
                logging.warning("HTTP 429: rate-limited; wait full interval")
                return None
            if r.status_code != 200:
                logging.warning("API HTTP %d: %s", r.status_code, r.text[:200])
                return None
            data = r.json()
            self._save_cache(data)
            return data
        except requests.RequestException as e:
            self._calls_today += 1
            self._save_quota()
            logging.warning("Weather fetch failed: %s (calls today: %d/%d)",
                            e, self._calls_today, self.cfg.daily_quota)
            return None
        except ValueError as e:
            logging.warning("Weather JSON decode failed: %s", e)
            return None

    def run(self) -> None:
        while not self._stop.is_set():
            data = self.fetch_once()
            if data is not None:
                self.queue.put(data)
            for _ in range(self._interval_seconds):
                if self._stop.is_set():
                    return
                time.sleep(1)


# ---------------------------------------------------------------------------
# Weather id -> icon base name
# ---------------------------------------------------------------------------

def weather_to_icon(weather_obj: dict) -> str:
    """Estrae il nome icona (es. '02n') dal blocco weather di OpenWeather.

    OpenWeather fornisce direttamente il campo 'icon' con sufisso 'd'/'n'
    in base all'alba/tramonto. Lo usiamo cosi' com'e' invece di derivarlo
    dal weather_id (che e' agnostico al giorno/notte).

    Se l'icona ricevuta non e' tra quelle supportate dai drawer, fallback
    a una mappatura dal weather_id.
    """
    icon = weather_obj.get("icon", "")
    # Icone supportate dai nostri drawer (vedi DRAWERS in icon_animations.py)
    if icon in ("01d", "01n", "02d", "02n", "03d", "03n", "04d", "04n",
                "09d", "09n", "10d", "10n", "11d", "11n", "13d", "13n",
                "50d", "50n"):
        return icon
    # Fallback su weather_id
    wid = weather_obj.get("id", 800)
    return weather_id_to_icon(wid)


def weather_id_to_icon(wid: int) -> str:
    """Mappa weather_id → icona base. Fallback quando il campo 'icon' manca.

    Restituisce sempre il suffisso 'd' perche' non conosciamo il momento
    della giornata. Per la versione notte usare weather_to_icon().
    """
    if 200 <= wid <= 232: return "11d"
    if 300 <= wid <= 321: return "09d"
    if 500 <= wid <= 504: return "10d"
    if wid == 511:        return "13d"
    if 520 <= wid <= 531: return "09d"
    if 600 <= wid <= 622: return "13d"
    if 701 <= wid <= 781: return "50d"
    if wid == 800:        return "01d"
    if wid == 801:        return "02d"
    if wid == 802:        return "03d"
    if wid in (803, 804): return "04d"
    logging.warning("Unknown weather id: %s", wid)
    return "01d"


def wind_display(speed: float, units: str) -> tuple[float, str]:
    if units == "imperial":
        return speed, "mph"
    return speed * 3.6, "km/h"


# ---------------------------------------------------------------------------
# Main Pygame app
# ---------------------------------------------------------------------------

class WeatherClockSDL2:
    ICON_EXTENSIONS: tuple[str, ...] = (".png", ".gif")

    def __init__(self, cfg: Config, settings_path: Path):
        self.cfg = cfg
        self.settings_path = settings_path
        self.theme_dir = BASE_DIR / cfg.theme

        # --- Weather state ---
        self.weather_data: Optional[dict] = None
        self.weather_queue: "Queue[dict]" = Queue()
        self.last_fetch_time: Optional[datetime] = None
        self._dismissed_alert_keys: set[str] = set()
        self._active_alert: Optional[dict] = None
        # Lista di TUTTE le allerte attive (API + sintetiche). Il banner
        # mostra una sola alert per volta; con più allerte ruota ogni
        # `alert_rotation_seconds` secondi e include un contatore "[i/N]".
        self._active_alerts_list: list[dict] = []
        self._active_alerts_idx: int = 0
        self._active_alerts_last_rotation: float = 0.0
        # Pagina report allerte (MODE_ALERTS): due sotto-viste.
        #   "list"   → elenco di tutte le allerte attive
        #   "detail" → testo completo di una singola allerta
        self._alerts_view: str = "list"
        self._alerts_detail_idx: int = 0

        # --- UI state ---
        self.mode = MODE_HANDS
        self.detail_hours_ahead = 0
        self.detail_page = 0   # 0=base, 1=atmosferica, 2=grafico
        self._mode_deadline: Optional[float] = None  # monotonic time when timer fires

        # Settings hot-reload
        try:
            self._settings_mtime = settings_path.stat().st_mtime
        except OSError:
            self._settings_mtime = 0.0

        # Touch / gesture state
        self._press_xy: Optional[tuple[int, int]] = None
        self._press_time_ms: int = 0
        self._long_press_fired = False
        self._running = True

        # --- Frame-skip optimization state ---
        # Signature for "what's on screen right now". If unchanged, skip flip.
        self._last_render_signature: Optional[tuple] = None
        self._force_redraw = True
        # Cached rendered text surfaces. Each is (key, surface): rebuild only
        # when key changes (where key encodes text+color+font_size).
        # Freshness: (key, texture, size)
        self._cache_freshness: Optional[tuple] = None
        self._cache_center_temp: Optional[tuple[tuple, "Texture"]] = None
        # Digital text caches: (key, texture, size)
        self._cache_digital_time: Optional[tuple] = None
        self._cache_digital_date: Optional[tuple] = None
        self._cache_digital_temp: Optional[tuple] = None
        self._cache_digital_temp_wind: Optional[tuple] = None
        # Alert: (key, texture, size)
        self._cache_alert: Optional[tuple] = None
        # DETAIL panel cached as one composite surface (rebuilt only on switch).
        # Tuple: (hash, surface, pos_tuple)
        # Detail/Weekly panel: (hash, texture, pos, size)
        self._cache_detail_panel: Optional[tuple] = None
        # Weekly panel cached: tuple separata da detail per evitare rebuild
        # in churn quando si passa da DETAIL a WEEKLY e viceversa.
        self._cache_weekly_panel: Optional[tuple] = None
        # Report allerte: (hash, texture, pos, size). Le row-rect (elenco) sono
        # in coordinate SCHERMO, ricomputate a ogni rebuild per l'hit-test tap.
        self._cache_alerts_panel: Optional[tuple] = None
        self._alerts_row_rects: list[tuple[pygame.Rect, int]] = []
        # Moon phase cached: (phase_rounded_2dec, surface). Rebuilt quando la
        # fase cambia significativamente (~ogni qualche ora).
        # Moon: (phase_key, moon_tex, label_tex, label_size)
        self._cache_moon: Optional[tuple] = None
        # Cache moon_key per signature: invalidata ogni minuto (la fase
        # cambia ogni ~7 ore, riusare lo stesso valore per 60 frame consecutivi
        # risparmia ~60 chiamate/sec a moon_phase()).
        self._cached_moon_key: float = 0.0
        self._cached_moon_minute: int = -1
        self._cache_moon_mini: Optional[tuple] = None
        self._cache_current_panel: Optional[tuple] = None
        # Sunrise/sunset cached: (sunrise_ts, sunset_ts, surface). I timestamp
        # cambiano una volta al giorno → rebuild raro.
        # Sun_times: (sunrise_ts, sunset_ts, texture, size)
        self._cache_sun_times: Optional[tuple] = None
        # Wind in HANDS: (key, texture, size)
        self._cache_wind_hands: Optional[tuple] = None
        self._cache_temp_wind_blob: Optional[tuple] = None
        # Chart 24h: (key, texture, size)
        self._cache_chart: Optional[tuple] = None

        # --- Transition state (Fase 4) ---
        # Animazione tra mode: tipo (slide vert / fade / slide oriz) scelto
        # automaticamente in base a (from, to). Vedi _pick_transition_style.
        self._transition_active = False
        self._transition_start_ms = 0
        self._transition_from_mode = MODE_HANDS
        self._transition_to_mode = MODE_HANDS
        self._transition_style = ""   # set da _begin_transition
        # Flag: i buffer Texture per fade sono stati renderizzati per questa
        # transizione (caching: rendero solo al primo frame della transizione).
        self._transition_fade_buffers_ready = False
        # Flag analogo per slide oriz centro (HANDS/DIGITAL/CHART)
        self._transition_slide_buffers_ready = False
        # Pill background surfaces cached by (width, height, color, alpha).
        # Senza cache si allocavano ~24 SRCALPHA surface/frame (mmap/munmap),
        # responsabili del ~14% del tempo kernel.
        self._cache_pill_bg: dict[tuple[int, int, tuple, int], pygame.Surface] = {}

        # --- Performance metrics ---
        self._frames_rendered = 0
        self._frames_skipped = 0
        self._last_perf_log = time.monotonic()

        # Throttle settings.json hot reload (filesystem stat once per N seconds)
        self._settings_last_check = 0.0

        # Static bg version (incremented on rebuild, used in signature)
        self._static_bg_version = 0

        # --- Pygame setup ---
        # Init only the subsystems we use. pygame.init() would also load
        # the audio mixer which on a Pi without audio device starts a
        # retry loop and keeps pulseaudio/pipewire CPU-hot.
        # Hint SDL2 per il touch: forza la generazione di mouse events anche
        # quando arrivano da un touchscreen. Senza questo, sotto KMSDRM
        # alcuni driver touch generano SOLO FINGERDOWN/UP e l'app deve
        # gestire entrambi i tipi. Con SDL_MOUSE_TOUCH_EVENTS=1, ogni
        # touch produce sia FINGER_* che MOUSEBUTTON_* sintetici → l'app
        # vede gli stessi event di un mouse e tutto funziona.
        # SDL_TOUCH_MOUSE_EVENTS=1 fa l'inverso (mouse → touch), utile
        # in test desktop ma irrilevante in produzione.
        import os as _os
        _os.environ.setdefault("SDL_MOUSE_TOUCH_EVENTS", "1")
        _os.environ.setdefault("SDL_TOUCH_MOUSE_EVENTS", "1")
        pygame.display.init()
        pygame.font.init()
        # Joystick / cdrom / mixer NON inizializzati (non ci servono).
        pygame.display.set_caption(cfg.title)
        # === SDL2 hardware-accelerated rendering (Fase 2 GPU pipeline) ===
        # `self.rb` (render backend) gestisce Renderer + Window + Texture cache.
        # Niente più scratchpad Surface: tutti i _draw_* disegnano direttamente
        # sul renderer GPU via texture cached.
        self.rb = RenderBackend(cfg.screen_width, cfg.screen_height,
                                fullscreen=cfg.fullscreen,
                                scale_quality=cfg.render_scale_quality)
        pygame.mouse.set_visible(False)
        # SPLASH SCREEN early: il display è pronto, ma manca il pre-build delle
        # texture (icone, lancette, sfondo) che richiede 3-8 secondi su Pi Zero W.
        # Senza splash, l'utente vede schermo nero durante tutto questo periodo,
        # indistinguibile da un crash. Mostriamo "Loading..." subito.
        try:
            self._render_loading_splash("Weather Clock", "Loading...")
            logging.info("Loading splash rendered (early)")
        except Exception:
            logging.exception("Loading splash render failed (non critico)")
        self.clock = pygame.time.Clock()
        # Pre-risolvi la funzione di easing una volta sola (evita lookup
        # string ad ogni frame della transizione)
        self._ease_fn = self._resolve_easing()

        # Pre-compute hour positions
        cx, cy, r = cfg.center_x, cfg.center_y, cfg.radius
        self.hour_pos: list[tuple[int, int]] = []
        for i in range(1, 13):
            ang = math.radians(90 - i * 30)
            self.hour_pos.append((int(cx + r * math.cos(ang)),
                                  int(cy - r * math.sin(ang))))
        # Pre-compute icon rects: in _draw_icons() get_rect(center=...) viene
        # chiamato 12 volte/frame. Le posizioni non cambiano mai → cache.
        # `topleft` calcolato sottraendo icon_size/2 dal centro.
        half = cfg.icon_size // 2
        self._icon_rects: list[pygame.Rect] = [
            pygame.Rect(x - half, y - half, cfg.icon_size, cfg.icon_size)
            for (x, y) in self.hour_pos
        ]

        # --- Fonts ---
        self.font_values = self._make_font(cfg.font_name, cfg.values_font_size,
                                           bold=cfg.values_font_bold)
        self.font_center_temp = self._make_font(cfg.font_name, cfg.center_temp_font_size, bold=True)
        self.font_freshness = self._make_font(cfg.font_name, cfg.freshness_font_size)
        self.font_moon_label = self._make_font(cfg.font_name, cfg.moon_label_font_size, bold=True)
        self.font_sun_times = self._make_font(cfg.font_name, cfg.sun_times_font_size, bold=True)
        self.font_alert = self._make_font(cfg.font_name, cfg.alert_font_size, bold=True)
        self.font_digital_time = self._make_font(cfg.font_name_mono,
                                                 cfg.digital_time_font_size, bold=True)
        self.font_digital_date = self._make_font(cfg.font_name, cfg.digital_date_font_size)
        self.font_digital_temp = self._make_font(cfg.font_name, cfg.digital_temp_font_size, bold=True)
        self.font_detail_label = self._make_font(cfg.font_name, cfg.detail_label_font_size, bold=True)
        self.font_detail_value = self._make_font(cfg.font_name, cfg.detail_value_font_size, bold=True)
        # Font WEEKLY dedicati (layout 2 colonne, più grandi)
        self.font_weekly_header = self._make_font(cfg.font_name, cfg.weekly_header_font_size, bold=True)
        self.font_weekly_day = self._make_font(cfg.font_name, cfg.weekly_day_font_size, bold=True)
        self.font_weekly_temp = self._make_font(cfg.font_name, cfg.weekly_temp_font_size, bold=True)
        self.font_weekly_pop = self._make_font(cfg.font_name, cfg.weekly_pop_font_size, bold=True)
        self.font_weekly_stat = self._make_font(cfg.font_name, cfg.weekly_stat_font_size, bold=False)
        # Font report allerte (MODE_ALERTS)
        self.font_alerts_header = self._make_font(cfg.font_name, cfg.alerts_header_font_size, bold=True)
        self.font_alerts_title = self._make_font(cfg.font_name, cfg.alerts_title_font_size, bold=True)
        self.font_alerts_meta = self._make_font(cfg.font_name, cfg.alerts_meta_font_size, bold=False)
        self.font_alerts_body = self._make_font(cfg.font_name, cfg.alerts_body_font_size, bold=False)

        # --- Icon cache (static icons fallback when animations disabled) ---
        self.icon_cache: dict[str, pygame.Surface] = {}
        self._preload_icons()

        # Aggiorna splash: stiamo per fare il lavoro più lungo del boot
        try:
            self._render_loading_splash("Weather Clock", "Preparing icons...")
        except Exception:
            pass

        # --- Animated icons (procedural) ---
        # Pre-rasterizza un loop completo per ogni condizione meteo: cosi'
        # a runtime ogni frame costa solo un blit, niente disegno primitivo.
        # Memory: 18 icone (day+night) × 20 frame × 100² × 4 byte = ~14 MB
        self.icon_sheets: dict[str, list[pygame.Surface]] = {}
        if cfg.animate_icons:
            t_start = time.monotonic()
            n_frames = cfg.animation_n_frames
            for name in icon_animations.DRAWERS.keys():
                self.icon_sheets[name] = icon_animations.precompute_spritesheet(
                    name, cfg.icon_size, n_frames,
                    antialias_scale=cfg.icon_antialias_scale,
                )
            elapsed = time.monotonic() - t_start
            logging.info("Spritesheet pre-renderizzati: %d icone × %d frame "
                         "(%.1f MB stimati, %.2fs)",
                         len(self.icon_sheets), n_frames,
                         len(self.icon_sheets) * n_frames * cfg.icon_size ** 2 * 4
                         / (1024 * 1024),
                         elapsed)
        # === SDL2: converti gli spritesheet Surface in Texture GPU ===
        # Questo è IL salto di performance: invece di blit software 100x100
        # SRCALPHA per 12 icone/frame (~6ms), avremo renderer.copy() GPU
        # per 12 textures (~0.5ms totali).
        # Costo memoria GPU: stesso degli sheets RAM (~14MB) ma i sheets RAM
        # restano: potremmo liberarli ma li teniamo per WEEKLY (vedi sotto).
        try:
            self._render_loading_splash("Weather Clock", "Uploading to GPU...")
        except Exception:
            pass
        self.icon_textures: dict[str, list[Texture]] = {}
        if self.icon_sheets:
            t_start = time.monotonic()
            for name, frames in self.icon_sheets.items():
                texs = []
                for frame in frames:
                    tex = self.rb.surface_to_texture(frame)
                    # Alpha blending esplicito per evitare artefatti di overlap
                    try:
                        tex.blend_mode = 1  # BLENDMODE_BLEND
                    except AttributeError:
                        pass
                    texs.append(tex)
                self.icon_textures[name] = texs
            elapsed = time.monotonic() - t_start
            logging.info("Icon textures GPU: %d × %d frame (%.2fs)",
                         len(self.icon_textures), n_frames, elapsed)
        # Frame counter globale (incrementato ogni `1/animation_fps` secondi)
        self._anim_frame_idx = 0
        self._anim_last_step_mono = time.monotonic()
        # Periodo cached per il main loop hot path (evita ricalcolo a ogni frame)
        self._anim_period = (1.0 / cfg.animation_fps
                              if cfg.animation_fps > 0 else 0.0)
        # Hourly icon names list (12 entries), aggiornato quando arrivano dati
        self._hour_icon_names: list[str] = ["01d"] * 12

        # --- Static background layer (built lazily on first frame) ---
        # _build_static_bg creates and converts self._static_bg
        self._static_bg: Optional[pygame.Surface] = None
        self._static_bg_dirty = True   # flag: rebuild on next frame
        # SDL2: la Texture del static_bg, ricreata quando il Surface cambia.
        self._static_bg_texture: Optional[Texture] = None

        # --- Load cache for immediate display ---
        if cfg.cache_enabled:
            cached = self._load_cache_initial()
            if cached is not None:
                self.weather_data, self.last_fetch_time = cached
                self._static_bg_dirty = True
                logging.info("Cache iniziale: dati di %s fa",
                             format_age(int((datetime.now() - self.last_fetch_time).total_seconds())))

        # --- Start fetcher ---
        self.fetcher = WeatherFetcher(cfg, self.weather_queue)
        self.fetcher.start()

        # Signal handler
        signal.signal(signal.SIGTERM, lambda *_: self._request_quit())
        signal.signal(signal.SIGINT, lambda *_: self._request_quit())
        # SIGUSR1 = toggle OFF↔HANDS (utile durante apt upgrade da SSH)
        # SIGUSR2 = forza ON (esce da OFF)
        # I signal NON sono async-safe in Python con Pygame: settiamo un flag
        # e lo processiamo nel main loop.
        self._signal_toggle_off = False
        self._signal_force_on = False
        signal.signal(signal.SIGUSR1, lambda *_: setattr(self, '_signal_toggle_off', True))
        signal.signal(signal.SIGUSR2, lambda *_: setattr(self, '_signal_force_on', True))

        # --- Post-init cleanup memoria ---
        # Durante __init__ abbiamo allocato MOLTO temporaneo:
        #   - precompute_spritesheet ha creato Surface intermedie a 4× la
        #     dimensione finale poi smoothscaled → arene PyMalloc cresciute
        #   - Texture.from_surface copia dati alla GPU ma lascia le Surface
        #     in RAM (~14 MB di icon_sheets) — sono ancora referenziate
        #     ma il GC potrebbe avere oggetti orfani della loro costruzione
        #   - I/O di vari font, asset → buffer transitori
        # Tutto questo lascia Python con arene "gonfiate" piene di buchi.
        # Forziamo gc + malloc_trim per restituire memoria al kernel.
        try:
            self._render_loading_splash("Weather Clock", "Finalizing...")
        except Exception:
            pass
        try:
            import gc
            gc.collect()
            # malloc_trim(0): restituisce al kernel pagine virtuali libere
            # dell'heap glibc. Senza, glibc tiene le arene "espanse" anche
            # quando vuote internamente.
            try:
                import ctypes
                libc = ctypes.CDLL("libc.so.6")
                libc.malloc_trim(0)
                logging.info("Post-init cleanup: gc.collect + malloc_trim done")
            except Exception:
                logging.info("Post-init cleanup: gc.collect done (malloc_trim non disponibile)")
        except Exception:
            logging.exception("Post-init memory cleanup failed (non critico)")

    # -----------------------------------------------------------------------
    # Utility
    # -----------------------------------------------------------------------

    def _request_quit(self) -> None:
        self._running = False

    def _render_loading_splash(self, title: str, subtitle: str = "") -> None:
        """Mostra una splash screen leggera centrata sul display.

        Chiamata durante __init__ (dopo che RenderBackend è pronto ma prima
        del lungo pre-build texture) e potenzialmente durante long-running
        operations. Costa pochissimo:
          - 2 font.render Surface (titolo + sottotitolo)
          - 2 Texture.from_surface
          - 1 fill nero + 2 texture.draw + present
        Tempo totale ~15-30ms su Pi Zero W.

        Le texture vengono distrutte automaticamente alla fine del metodo
        (out of scope → GC). Nessun residuo di memoria.
        """
        cfg = self.cfg
        # Usa un font di sistema veloce (non vogliamo dipendere da _make_font
        # che potrebbe non essere ancora pronto). DejaVu Sans è preinstallato
        # su Raspbian.
        try:
            title_font = pygame.font.SysFont("DejaVu Sans", 48, bold=True)
            sub_font = pygame.font.SysFont("DejaVu Sans", 22, bold=False)
        except Exception:
            title_font = pygame.font.Font(None, 48)
            sub_font = pygame.font.Font(None, 22)

        # Pulisci schermo (nero)
        self.rb.renderer.draw_color = (0, 0, 0, 255)
        self.rb.renderer.clear()

        # Titolo bianco
        try:
            title_surf = title_font.render(title, True, (240, 240, 240))
            title_tex = Texture.from_surface(self.rb.renderer, title_surf)
            tw, th = title_surf.get_size()
            title_rect = pygame.Rect(
                (cfg.screen_width - tw) // 2,
                (cfg.screen_height - th) // 2 - 20,
                tw, th)
            title_tex.draw(dstrect=title_rect)
        except Exception:
            pass

        # Sottotitolo grigio chiaro
        if subtitle:
            try:
                sub_surf = sub_font.render(subtitle, True, (160, 160, 160))
                sub_tex = Texture.from_surface(self.rb.renderer, sub_surf)
                sw, sh = sub_surf.get_size()
                sub_rect = pygame.Rect(
                    (cfg.screen_width - sw) // 2,
                    (cfg.screen_height - sh) // 2 + 40,
                    sw, sh)
                sub_tex.draw(dstrect=sub_rect)
            except Exception:
                pass

        # Present al display
        self.rb.renderer.present()

    @staticmethod
    def _find_bold_variant(regular_path: Path) -> Optional[Path]:
        """Dato un path al file font "regular", cerca un file bold associato
        nella stessa directory.

        Pattern provati nell'ordine (suffix nel filename, estensione preservata):
          1. Sostituisci "-Regular" / "-Medium" / "-Light" → "-Bold"
          2. Sostituisci "Regular" / "Medium" / "Light" → "Bold" (no trattino)
          3. Aggiungi "-Bold" prima dell'estensione (es. "Custom.ttf" → "Custom-Bold.ttf")
          4. Aggiungi "Bold" prima dell'estensione

        Matching dei suffix Regular/Medium/Light è case-insensitive (utile su
        filesystem dove i font potrebbero avere casing diverso).
        Restituisce il primo path che esiste, o None se nessuno trovato.
        """
        import re
        directory = regular_path.parent
        stem = regular_path.stem          # senza estensione
        ext = regular_path.suffix         # ".ttf" o ".otf"

        # Lista di candidate stem (senza estensione) da provare in ordine.
        # Uso regex case-insensitive per gestire varianti come "Regular",
        # "regular", "REGULAR" in modo uniforme.
        candidates: list[str] = []
        for needle in ("-Regular", "-Medium", "-Light", "-Thin"):
            pattern = re.compile(re.escape(needle), re.IGNORECASE)
            new_stem, n = pattern.subn("-Bold", stem, count=1)
            if n > 0:
                candidates.append(new_stem)
        for needle in ("Regular", "Medium", "Light", "Thin"):
            pattern = re.compile(re.escape(needle), re.IGNORECASE)
            new_stem, n = pattern.subn("Bold", stem, count=1)
            if n > 0:
                candidates.append(new_stem)
        # Aggiunta suffix se nessuna sostituzione applicabile
        candidates.append(f"{stem}-Bold")
        candidates.append(f"{stem}Bold")

        # 1° tentativo: exact match (case-sensitive, veloce)
        for cand_stem in candidates:
            p = directory / f"{cand_stem}{ext}"
            if p.exists():
                return p
        # 2° tentativo: case-insensitive scan della directory
        if directory.exists():
            try:
                existing = {f.name.lower(): f for f in directory.iterdir()
                              if f.suffix.lower() in (".ttf", ".otf")}
                for cand_stem in candidates:
                    key = f"{cand_stem}{ext}".lower()
                    if key in existing:
                        return existing[key]
            except OSError:
                pass
        return None

    def _make_font(self, name: str, size: int, bold: bool = False) -> pygame.font.Font:
        """Carica un font supportando 3 modalità:

        1. **Path diretto** (assoluto o relativo a BASE_DIR): se `name`
           termina con `.ttf` o `.otf` o contiene un separatore di path
           lo trattiamo come percorso al file. Esempio:
               "fonts/Roboto-Regular.ttf"
               "/home/kiosk/weatherClock/fonts/Custom.ttf"
           Per i font bold, se hai un file dedicato passa il path al file
           bold; altrimenti pygame applica grassetto sintetico (peggio).

        2. **Nome font nominato** dalla mappa FONT_PATHS (es. "DejaVu Sans"):
           cerca il file corrispondente con flag bold corretto.

        3. **Fallback**: se nulla funziona, usa il font built-in di pygame
           (no accenti italiani però).

        Pygame.font.SysFont spawna fc-list come subprocess per cercare font,
        che su Pi Zero W single-core sotto carico va in timeout. Caricando
        direttamente i .ttf siamo veloci e indipendenti da fontconfig.
        """
        path: Optional[str] = None
        synthetic_bold = False

        # Caso 1: path diretto (con estensione o separator)
        if (name.endswith(".ttf") or name.endswith(".otf")
                or "/" in name or "\\" in name):
            candidate = Path(name)
            if not candidate.is_absolute():
                candidate = BASE_DIR / candidate

            # Se serve bold, prova ad auto-discovery del file bold associato.
            # Pattern comuni nei font scaricati:
            #   Inter-Regular.ttf  → Inter-Bold.ttf
            #   Roboto-Regular.ttf → Roboto-Bold.ttf
            #   Custom-Light.ttf   → Custom-Bold.ttf (Light → Bold)
            #   Custom.ttf         → Custom-Bold.ttf (sufix add)
            #   Custom.otf         → Custom-Bold.otf (extension preserved)
            if bold:
                bold_candidate = self._find_bold_variant(candidate)
                if bold_candidate is not None and bold_candidate.exists():
                    path = str(bold_candidate)
                    # File bold dedicato trovato → no grassetto sintetico
                    synthetic_bold = False
                elif candidate.exists():
                    # Bold file non trovato, fallback su regular + bold sintetico
                    path = str(candidate)
                    synthetic_bold = True
                    logging.info("Variante bold di %s non trovata, "
                                  "uso grassetto sintetico", candidate.name)
                else:
                    logging.warning("Font path %s non trovato, fallback", candidate)
            else:
                # Regular: usa il file così com'è
                if candidate.exists():
                    path = str(candidate)
                else:
                    logging.warning("Font path %s non trovato, fallback", candidate)

        # Caso 2: nome nominato
        if path is None:
            path = FONT_PATHS.get((name, bold))
            if path is None and bold:
                # Bold richiesto ma manca: usa regular + bold sintetico
                path = FONT_PATHS.get((name, False))
                synthetic_bold = True

        if path and Path(path).exists():
            try:
                font = pygame.font.Font(path, size)
                if synthetic_bold:
                    font.set_bold(True)
                return font
            except (OSError, pygame.error) as e:
                logging.warning("Font %s fallito (%s), fallback default", path, e)

        # Ultimo fallback: font built-in (no accenti italiani)
        logging.warning("Font %s (bold=%s) non disponibile, uso built-in",
                         name, bold)
        font = pygame.font.Font(None, size)
        if bold:
            font.set_bold(True)
        return font

    def _load_cache_initial(self) -> Optional[tuple[dict, datetime]]:
        if not CACHE_FILE.exists():
            logging.info("Cache: %s assente (primo avvio)", CACHE_FILE)
            return None
        try:
            payload = json.loads(CACHE_FILE.read_text())
            fetched_at = datetime.fromtimestamp(payload["fetched_at"])
            return payload["data"], fetched_at
        except (OSError, ValueError, KeyError) as e:
            logging.warning("Cache non leggibile: %s", e)
            return None

    # -----------------------------------------------------------------------
    # Icons
    # -----------------------------------------------------------------------

    def _resolve_icon_path(self, base: str) -> Optional[Path]:
        for ext in self.ICON_EXTENSIONS:
            p = self.theme_dir / f"{base}{ext}"
            if p.exists():
                return p
        # Legacy fallback
        p = self.theme_dir / f"{base}@2x.gif"
        return p if p.exists() else None

    def _icon(self, base: str) -> pygame.Surface:
        if base in self.icon_cache:
            return self.icon_cache[base]
        path = self._resolve_icon_path(base)
        size = self.cfg.icon_size
        if path is None:
            logging.error("Icona non trovata: %s in %s", base, self.theme_dir)
            surf = pygame.Surface((size, size), pygame.SRCALPHA)
        else:
            try:
                surf = pygame.image.load(str(path))
                if pygame.display.get_surface() is not None:
                    surf = surf.convert_alpha()
                if surf.get_size() != (size, size):
                    surf = pygame.transform.smoothscale(surf, (size, size))
            except pygame.error as e:
                logging.error("Carico icona fallito %s: %s", path, e)
                surf = pygame.Surface((size, size), pygame.SRCALPHA)
        self.icon_cache[base] = surf
        return surf

    def _preload_icons(self) -> None:
        for name in icon_animations.DRAWERS.keys():
            self._icon(name)

    # -----------------------------------------------------------------------
    # Layered rendering
    # -----------------------------------------------------------------------

    def _build_static_bg(self) -> None:
        """Render layer 1: background + tick marks + icons + temperature overlays.

        Ridisegnato solo quando arrivano dati nuovi (di solito ogni 10 min).
        Dopo il build, la surface viene convertita al pixel format del display
        per accelerare i blit successivi (~3x più veloce su Pi Zero W).
        """
        cfg = self.cfg
        # Lavoriamo su una surface temporanea con alpha; convertiamo alla fine
        bg = pygame.Surface((cfg.screen_width, cfg.screen_height))
        bg.fill(parse_color(cfg.background_color, (0, 0, 0)))

        # Tick dots (pallini sui 12 punti delle ore)
        if cfg.show_tick_marks:
            cx, cy = cfg.center_x, cfg.center_y
            color = parse_color(cfg.tick_color)
            for i in range(1, 13):
                ang = math.radians(90 - i * 30)
                x = int(cx + cfg.tick_radius * math.cos(ang))
                y = int(cy - cfg.tick_radius * math.sin(ang))
                r = cfg.tick_dot_radius_major if i in (3, 6, 9, 12) else cfg.tick_dot_radius
                pygame.draw.circle(bg, color, (x, y), r)

        # Icons + overlay (only if we have data)
        # IMPORTANTE: quando le animazioni sono attive le ICONE non vanno
        # nel background statico (vengono ridisegnate ogni frame). Anche
        # le PILLOLE temp/wind non vanno qui perche' verrebbero coperte
        # dal blit dell'icona animata sopra: le disegniamo come overlay
        # in _draw_pills_overlay (chiamato DOPO _draw_icons).
        # Solo se animate_icons=False mettiamo icone e pillole in background.
        if not self.weather_data:
            self._static_bg = safe_convert(bg)
            self._static_bg_version += 1
            self._static_bg_texture = self.rb.surface_to_texture(self._static_bg)
            return
        hourly = self.weather_data.get("hourly") or []
        if len(hourly) < 12:
            self._static_bg = safe_convert(bg)
            self._static_bg_version += 1
            self._static_bg_texture = self.rb.surface_to_texture(self._static_bg)
            return
        now_h = self._location_now().hour
        new_icon_names: list[str] = ["01d"] * 12
        new_pill_specs: list[Optional[tuple[Optional[str], Optional[str], tuple[int, int], str]]] = [None] * 12
        for i in range(12):
            forecast_hour = now_h + i
            entry = hourly[i]
            icon_name = weather_to_icon(entry["weather"][0])
            dial_hour = forecast_hour % 12 or 12
            pos_idx = dial_hour - 1
            new_icon_names[pos_idx] = icon_name
            x, y = self.hour_pos[pos_idx]

            # Costruisci spec per pillole temp (in alto) e wind (in basso)
            temp_text: Optional[str] = None
            wind_text: Optional[str] = None
            if cfg.show_temperature:
                temp_text = f"{round(entry.get('temp', 0))}{DEGREE}"
            if cfg.show_wind:
                w_val, _ = wind_display(entry.get("wind_speed", 0), cfg.units)
                wind_text = f"{round(w_val)}"
            if temp_text or wind_text:
                new_pill_specs[pos_idx] = (temp_text, wind_text, (x, y), icon_name)

            # Senza animazioni: icona + pillola direttamente nel background
            if not cfg.animate_icons:
                icon_surf = self._get_icon_for_static(icon_name)
                bg.blit(icon_surf, icon_surf.get_rect(center=(x, y)))
                self._render_pills_onto(bg, temp_text, wind_text, x, y, icon_name=icon_name)

        self._hour_icon_names = new_icon_names
        # Salviamo le pillole per il disegno overlay (solo se animate)
        self._hour_pill_specs = new_pill_specs

        # Converti al pixel format del display → blit successivi ~3x più veloci
        self._static_bg = safe_convert(bg)
        self._static_bg_version += 1
        # === SDL2: ricrea la Texture del static_bg ===
        # Costoso (~3-5ms una tantum), ma avviene solo ai cambi di dati meteo
        # (ogni 10 minuti circa). Il blit successivo sarà ~0.3ms GPU.
        self._static_bg_texture = self.rb.surface_to_texture(self._static_bg)

    # Colore di sfondo adattivo per condizione meteo (quando values_bg_adaptive=True)
    # Scelti come tonalita' scura che si sposa col colore dominante dell'icona.
    PILL_COLORS_ADAPTIVE: dict[str, tuple[int, int, int]] = {
        "01d": (140, 70, 0),     # sole → arancione scuro
        "01n": (40, 40, 80),     # luna → blu notte
        "02d": (120, 80, 30),    # poco nuvoloso → senape
        "02n": (50, 50, 80),     # poco nuvoloso notte → blu scuro
        "03d": (60, 60, 70),     # nuvoloso → grigio scuro
        "03n": (50, 50, 60),
        "04d": (45, 45, 55),     # coperto → grigio piombo
        "04n": (40, 40, 50),
        "09d": (20, 50, 100),    # pioggia → blu scuro
        "10d": (40, 70, 110),    # pioggia con sole → blu medio
        "11d": (80, 60, 30),     # temporale → ocra scuro
        "13d": (90, 90, 110),    # neve → grigio-blu chiaro
        "50d": (60, 60, 70),     # nebbia → grigio
    }

    def _pill_bg_color(self, icon_name: Optional[str]) -> tuple[int, int, int]:
        """Colore di sfondo per la pillola, eventualmente adattivo."""
        cfg = self.cfg
        if cfg.values_bg_adaptive and icon_name:
            return self.PILL_COLORS_ADAPTIVE.get(icon_name,
                                                 parse_color(cfg.values_bg_color))
        return parse_color(cfg.values_bg_color)

    def _render_pills_onto(self, target: pygame.Surface,
                           temp_text: Optional[str],
                           wind_text: Optional[str],
                           x: int, y: int,
                           icon_name: Optional[str] = None) -> None:
        """Disegna pillole temp (in alto) e wind (in basso) DENTRO l'icona in (x, y).

        Posizionamento:
          - Temp: midtop ancorato a `y - icon_size/2 + values_temp_inset`
          - Wind: midbottom ancorato a `y + icon_size/2 - values_wind_inset`
        Cosi' le pillole sono SOVRAPPOSTE all'icona, non sotto.
        """
        cfg = self.cfg
        bg_color = self._pill_bg_color(icon_name)
        alpha = max(0, min(255, cfg.values_bg_alpha))
        pad_x, pad_y = cfg.values_bg_pad_x, cfg.values_bg_pad_y

        if temp_text:
            text_surf = self.font_values.render(
                temp_text, True, parse_color(cfg.values_color)
            )
            text_rect = text_surf.get_rect(
                midtop=(x, y - cfg.icon_size // 2 + cfg.values_temp_inset)
            )
            pill_rect = text_rect.inflate(pad_x * 2, pad_y * 2)
            self._blit_pill(target, pill_rect, bg_color, alpha)
            target.blit(text_surf, text_rect)

        if wind_text:
            text_surf = self.font_values.render(
                wind_text, True, parse_color(cfg.values_color)
            )
            text_rect = text_surf.get_rect(
                midbottom=(x, y + cfg.icon_size // 2 - cfg.values_wind_inset)
            )
            pill_rect = text_rect.inflate(pad_x * 2, pad_y * 2)
            self._blit_pill(target, pill_rect, bg_color, alpha)
            target.blit(text_surf, text_rect)

    def _blit_pill(self, target: pygame.Surface, pill_rect: pygame.Rect,
                   bg_color: tuple[int, int, int], alpha: int) -> None:
        """Helper: disegna un rettangolo pillola con alpha.

        Usa una cache di Surface per evitare ~24 allocazioni SRCALPHA per
        frame (un'allocazione = un mmap del kernel, costoso sul Pi Zero W).
        """
        if alpha >= 255:
            # No alpha: disegno diretto, gratis senza Surface intermedia
            pygame.draw.rect(target, bg_color, pill_rect, border_radius=6)
            return
        key = (pill_rect.width, pill_rect.height, bg_color, alpha)
        pill_surf = self._cache_pill_bg.get(key)
        if pill_surf is None:
            pill_surf = pygame.Surface(pill_rect.size, pygame.SRCALPHA)
            pygame.draw.rect(pill_surf, (*bg_color, alpha),
                             pill_surf.get_rect(), border_radius=6)
            # Convert preserves alpha and makes successive blits ~2x faster
            pill_surf = safe_convert(pill_surf, with_alpha=True)
            self._cache_pill_bg[key] = pill_surf
        target.blit(pill_surf, pill_rect.topleft)

    def _draw_pills_overlay(self) -> None:
        """Disegna le pillole temp/wind sopra le icone (GPU).

        Strategia: layer 720x720 con SRCALPHA, dove disegniamo TUTTE le
        pillole. Convertiamo a Texture cached e blittiamo ogni frame.
        Rebuild solo quando le _hour_pill_specs cambiano (ogni ~10 min).
        """
        if not self.cfg.animate_icons:
            return  # Sono già nel background
        specs = getattr(self, "_hour_pill_specs", [])
        if not specs:
            return
        # Signature per cache invalidation
        sig = tuple((s if s is None else
                     (s[0], s[1], s[2], s[3])) for s in specs)
        cached = getattr(self, '_cache_pills_overlay', None)
        if cached is None or cached[0] != sig:
            # Crea un layer SRCALPHA. SRCALPHA evita il bug del fill(magenta)
            # qui perche' usiamo .blit() di Surface SRCALPHA → SRCALPHA, che
            # funziona bene. fill((0,0,0,0)) iniziale lascia tutto trasparente
            # (NB: Pygame 2 bug solo se SUBITO seguito da pygame.draw, quindi
            # qui non è un problema).
            layer = pygame.Surface(
                (self.cfg.screen_width, self.cfg.screen_height),
                pygame.SRCALPHA
            )
            for spec in specs:
                if spec is None:
                    continue
                temp_text, wind_text, (x, y), icon_name = spec
                self._render_pills_onto(layer, temp_text, wind_text, x, y,
                                        icon_name=icon_name)
            # Converti a Texture
            tex = Texture.from_surface(self.rb.renderer, layer)
            try:
                tex.blend_mode = 1
            except AttributeError:
                pass
            self._cache_pills_overlay = (sig, tex)
        else:
            tex = cached[1]
        tex.draw()

    def _get_icon_for_static(self, base: str) -> pygame.Surface:
        """Icona statica: usa il primo frame dello spritesheet (se animazioni
        sono pre-renderizzate) altrimenti carica da disco via _icon().

        Se `icon_sheets` è stato rilasciato (release_icon_sheets_after_boot),
        ri-genera al volo il singolo spritesheet richiesto e lo cacha in
        modo permanente (non viene più rilasciato). In pratica questo
        succede rarissimamente perché DETAIL/CURRENT/CHART pre-cachano
        le loro texture già al primo render."""
        sheet = self.icon_sheets.get(base)
        if sheet:
            return sheet[0]
        # icon_sheets rilasciato? Ri-genera al volo (costoso una volta sola)
        if not self.icon_sheets and self.cfg.animate_icons \
                and base in icon_animations.DRAWERS:
            try:
                self.icon_sheets[base] = icon_animations.precompute_spritesheet(
                    base, self.cfg.icon_size, self.cfg.animation_n_frames,
                    antialias_scale=self.cfg.icon_antialias_scale,
                )
                logging.info("Spritesheet ri-generato post-release per %s", base)
                return self.icon_sheets[base][0]
            except Exception:
                logging.exception("Spritesheet regen fallito per %s", base)
        # Fallback: PNG da disco (theme legacy)
        return self._icon(base)

    def _draw_icons(self) -> None:
        """Disegna le 12 icone animate (SDL2 hardware-accelerated).

        Costo SDL2: 12 texture.draw() su Texture GPU = ~0.3-0.5ms totali.
        Equivalente Pygame software era ~6ms (42% del tempo CPU).

        Le Texture sono pre-create al boot da self.icon_textures (vedi
        __init__). Ogni frame fa solo lookup + texture.draw() GPU.

        API Pygame 2.6.x: tex.draw(dstrect=Rect) blitta sul renderer attivo.
        """
        if not self.cfg.animate_icons or not self.icon_textures:
            return
        n_frames = self.cfg.animation_n_frames
        frame = self._anim_frame_idx % n_frames
        rects = self._icon_rects
        textures = self.icon_textures
        names = self._hour_icon_names
        for pos_idx in range(12):
            sheet = textures.get(names[pos_idx])
            if sheet is not None:
                sheet[frame].draw(dstrect=rects[pos_idx])

    def _draw_hands(self) -> None:
        """Disegna le 3 lancette + il perno usando rotazione GPU.

        Pattern: ogni lancetta è una Texture rettangolare (length × width)
        pre-creata al boot. Ad ogni frame la ruotiamo attorno al perno
        (0, height/2 nella texture, che corrisponde a (cx, cy) sullo schermo).

        Costo GPU: ~0.5ms per lancetta + 0.5ms per perno = ~2ms totali.
        Era ~3-5ms in Pygame software, ora costantemente sotto 2ms.
        """
        cfg = self.cfg
        cx, cy = cfg.center_x, cfg.center_y
        now = self._location_now()
        # Smooth seconds: include microsecond fraction (richiede FPS alti per
        # essere visibile, costoso). Default: seconds a salti (orologio al quarzo).
        if cfg.smooth_seconds:
            sub_sec = now.second + now.microsecond / 1_000_000.0
        else:
            sub_sec = float(now.second)
        h = (now.hour % 12) + now.minute / 60.0 + sub_sec / 3600.0
        m = now.minute + sub_sec / 60.0
        s = sub_sec
        # Assicura che le texture lancette esistano (lazy build su prima call)
        if not hasattr(self, '_hand_textures') or self._hand_textures is None:
            self._build_hand_textures()
        # Padding texture: con AA attivo le texture hanno 1px di padding
        # per lato per evitare il "taglio" durante smoothscale. Devo
        # offsettare il dstrect e l'origin di conseguenza.
        pad = getattr(self, '_hand_texture_pad', 0)
        hand_specs = (
            ('hour',   cfg.hour_hand_length,   cfg.hour_hand_width,   h * 30),
            ('minute', cfg.minute_hand_length, cfg.minute_hand_width, m * 6),
            ('second', cfg.second_hand_length, cfg.second_hand_width, s * 6),
        )
        for name, length, width, angle_deg in hand_specs:
            tex = self._hand_textures.get(name)
            if tex is None:
                continue
            # Dimensioni effettive della texture (con eventuale padding AA)
            tex_w = length + 2 * pad
            tex_h = width + 2 * pad
            # Posizioniamo dstrect in modo che il "perno" della lancetta
            # cada esattamente su (cx, cy). Il perno è il pixel interno
            # all'inizio della lancetta (escludendo il padding):
            # nelle coordinate texture sta in (pad, pad + width/2).
            # Quindi dstrect topleft = (cx - pad, cy - pad - width/2).
            dst = pygame.Rect(cx - pad, cy - pad - width // 2, tex_w, tex_h)
            # `origin`: punto di rotazione in coord locali al dstrect.
            # Corrisponde al perno = (pad, pad + width/2).
            tex.draw(dstrect=dst, angle=angle_deg - 90,
                      origin=(pad, pad + width // 2))
        # Perno centrale: piccolo cerchio
        if self._pivot_texture is not None:
            pivot_size = 12
            pivot_rect = pygame.Rect(cx - pivot_size // 2, cy - pivot_size // 2,
                                     pivot_size, pivot_size)
            self._pivot_texture.draw(dstrect=pivot_rect)

    def _build_hand_textures(self) -> None:
        """Crea le texture per le 3 lancette + il perno centrale.

        AA via SUPERSAMPLING: rasterizziamo le lancette a `aa_scale`× la
        dimensione finale, poi le riduciamo con `pygame.transform.smoothscale`
        che applica bilinear filtering. Risultato: bordi smooth invece dei
        pixel staccato del rettangolo solido.

        Combinato con `SDL_RENDER_SCALE_QUALITY=linear` (filtering durante la
        rotazione GPU), le lancette appaiono morbide a qualsiasi angolo.

        Lancette dritte avrebbero comunque bordi netti perché un rettangolo
        bianco scalato resta bianco. Per ottenere bordi smooth genuini usiamo
        un trucco: invece di `surf.fill(color)`, disegnamo la lancetta come
        un rettangolo arrotondato (border_radius = mezzo width). In questo
        modo l'AA durante lo smoothscale produce gradienti reali ai bordi.

        Costo: 3 texture × ~1ms = ~3ms al boot, una sola volta.
        """
        cfg = self.cfg
        aa_scale = cfg.hands_antialias_scale
        self._hand_textures: dict[str, Texture] = {}
        for name, length, width, color in (
            ('hour',   cfg.hour_hand_length,   cfg.hour_hand_width,   cfg.hour_hand_color),
            ('minute', cfg.minute_hand_length, cfg.minute_hand_width, cfg.minute_hand_color),
            ('second', cfg.second_hand_length, cfg.second_hand_width, cfg.second_hand_color),
        ):
            color_rgb = parse_color(color)
            if aa_scale > 1:
                # SUPERSAMPLING: disegna a scale ×, riduci con smoothscale.
                # Aggiungiamo padding (1 pixel per lato a scala ×) per evitare
                # che i bordi vengano "tagliati" dallo smoothscale.
                pad = aa_scale  # 1 px finale = aa_scale px supersampled
                big_w = length * aa_scale + pad * 2
                big_h = width * aa_scale + pad * 2
                # SRCALPHA per supportare bordi smooth con alpha graduale
                big_surf = pygame.Surface((big_w, big_h), pygame.SRCALPHA)
                # Rettangolo arrotondato per ottenere AA reale ai bordi.
                # border_radius = width/2 in unità finali → semicerchio
                # alle estremità (lancetta a "stilo" classica).
                # In coord supersampled: rect (pad, pad, length*scale, width*scale)
                rect = pygame.Rect(pad, pad, length * aa_scale, width * aa_scale)
                radius = max(1, (width * aa_scale) // 2)
                pygame.draw.rect(big_surf, color_rgb, rect, border_radius=radius)
                # Riduci alla dimensione finale con smoothscale (bilinear)
                final_w = length + 2  # 1px padding per lato anche finale
                final_h = width + 2
                surf = pygame.transform.smoothscale(big_surf, (final_w, final_h))
            else:
                # AA off: rettangolo solido come prima
                surf = pygame.Surface((length, width), pygame.SRCALPHA)
                surf.fill(color_rgb)
            tex = Texture.from_surface(self.rb.renderer, surf)
            try:
                tex.blend_mode = 1  # BLENDMODE_BLEND
            except AttributeError:
                pass
            self._hand_textures[name] = tex
        # Memorizza padding usato (1 px se AA attivo, 0 altrimenti). Serve
        # a _draw_hands per offsettare il centro di rotazione correttamente.
        self._hand_texture_pad = 1 if aa_scale > 1 else 0
        # Perno: cerchio piccolo, beneficia anche lui di AA tramite
        # supersampling. Resa via pygame.draw.circle che già ha AA leggero,
        # ma supersampling rende il bordo davvero smooth.
        pivot_radius = 6
        pivot_size = pivot_radius * 2
        if aa_scale > 1:
            big_size = pivot_size * aa_scale
            big_surf = pygame.Surface((big_size, big_size), pygame.SRCALPHA)
            pygame.draw.circle(big_surf, parse_color(cfg.hour_hand_color),
                                (big_size // 2, big_size // 2),
                                pivot_radius * aa_scale)
            surf = pygame.transform.smoothscale(big_surf, (pivot_size, pivot_size))
        else:
            surf = pygame.Surface((pivot_size, pivot_size), pygame.SRCALPHA)
            pygame.draw.circle(surf, parse_color(cfg.hour_hand_color),
                                (pivot_size // 2, pivot_size // 2), pivot_radius)
        self._pivot_texture = Texture.from_surface(self.rb.renderer, surf)
        try:
            self._pivot_texture.blend_mode = 1
        except AttributeError:
            pass
        logging.info("Hand textures GPU create (AA scale=%d, pad=%d)",
                      aa_scale, self._hand_texture_pad)

    def _build_temp_wind_blob(self,
                                temp_text: str,
                                wind_kmh: Optional[int],
                                wind_deg: Optional[int],
                                cardinal: str,
                                font_temp,
                                font_wind,
                                arrow_size: int,
                                temp_color: tuple,
                                wind_color: tuple,
                                arrow_color: tuple,
                                gap_temp_arrow: int = 20,
                                gap_arrow_text: int = 6
                                ) -> pygame.Surface:
        """Costruisce una surface SRCALPHA con il blob:
            [temp][gap][freccia direzione][gap][velocità + cardinale]
        Se wind_kmh è None, restituisce solo la surface temp.
        """
        temp_surf = font_temp.render(temp_text, True, temp_color)
        if wind_kmh is None or wind_deg is None:
            return temp_surf
        wind_text = f"{wind_kmh} km/h {cardinal}"
        wind_text_surf = font_wind.render(wind_text, True, wind_color)
        arrow_surf = self._make_wind_arrow_surface(wind_deg, arrow_size, arrow_color)

        blob_w = (temp_surf.get_width() + gap_temp_arrow + arrow_surf.get_width()
                  + gap_arrow_text + wind_text_surf.get_width())
        blob_h = max(temp_surf.get_height(), arrow_surf.get_height(),
                     wind_text_surf.get_height())
        blob = pygame.Surface((blob_w, blob_h), pygame.SRCALPHA)
        x = 0
        blob.blit(temp_surf, (x, (blob_h - temp_surf.get_height()) // 2))
        x += temp_surf.get_width() + gap_temp_arrow
        blob.blit(arrow_surf, (x, (blob_h - arrow_surf.get_height()) // 2))
        x += arrow_surf.get_width() + gap_arrow_text
        blob.blit(wind_text_surf, (x, (blob_h - wind_text_surf.get_height()) // 2))
        return blob

    def _draw_temp_wind_hands(self) -> None:
        """HANDS mode: blob unico temp + freccia direzione + vento + cardinale,
        centrato orizzontalmente al centro_x, a y = cy + center_temp_y_offset.
        Sostituisce _draw_center_temp + _draw_wind_hands chiamati separati.
        Risolve la sovrapposizione tra "°" del temp e freccia del vento.
        """
        cfg = self.cfg
        if not (cfg.show_center_temp and self.weather_data):
            return
        current = self.weather_data.get("current", {})
        t = current.get("temp")
        if t is None:
            hourly = self.weather_data.get("hourly") or []
            if hourly:
                t = hourly[0].get("temp")
        if t is None:
            return

        wind_speed_ms = current.get("wind_speed")
        wind_deg_v = current.get("wind_deg")
        has_wind = (cfg.show_wind_hands and wind_speed_ms is not None
                    and wind_deg_v is not None)
        temp_text = f"{round(t)}{DEGREE}"

        if has_wind:
            wind_kmh = round(wind_speed_ms * 3.6)
            cardinal = self._wind_deg_to_cardinal(wind_deg_v)
            idx = int((wind_deg_v + 22.5) // 45) % 8
            cache_key = (temp_text, wind_kmh, idx)
        else:
            wind_kmh = None
            cardinal = ""
            cache_key = (temp_text, None, None)

        cached = getattr(self, '_cache_temp_wind_blob', None)
        if cached is None or cached[0] != cache_key:
            font_wind = self._make_font(
                cfg.font_name, cfg.wind_hands_font_size, bold=True
            )
            blob = self._build_temp_wind_blob(
                temp_text=temp_text,
                wind_kmh=wind_kmh,
                wind_deg=wind_deg_v,
                cardinal=cardinal,
                font_temp=self.font_center_temp,
                font_wind=font_wind,
                arrow_size=cfg.wind_hands_arrow_size,
                temp_color=parse_color(cfg.center_temp_color),
                wind_color=parse_color(cfg.wind_hands_color),
                arrow_color=parse_color(cfg.wind_hands_arrow_color),
            )
            tex = Texture.from_surface(self.rb.renderer, blob)
            try:
                tex.blend_mode = 1
            except AttributeError:
                pass
            self._cache_temp_wind_blob = (cache_key, tex, blob.get_size())
        _, tex, (w, h) = self._cache_temp_wind_blob
        rect = pygame.Rect(0, 0, w, h)
        rect.center = (cfg.center_x, cfg.center_y + cfg.center_temp_y_offset)
        tex.draw(dstrect=rect)

    def _draw_center_temp(self) -> None:
        """Disegna la temperatura corrente al centro (GPU diretto)."""
        cfg = self.cfg
        if not (cfg.show_center_temp and self.weather_data):
            return
        current = self.weather_data.get("current", {})
        t = current.get("temp")
        if t is None:
            hourly = self.weather_data.get("hourly") or []
            if hourly:
                t = hourly[0].get("temp")
        if t is None:
            return
        text = f"{round(t)}{DEGREE}"
        color = parse_color(cfg.center_temp_color)
        key = (text, color)
        if self._cache_center_temp is None or self._cache_center_temp[0] != key:
            # Render testo su Surface temporanea, poi converti a Texture cached
            surf = self.font_center_temp.render(text, True, color)
            tex = Texture.from_surface(self.rb.renderer, surf)
            try:
                tex.blend_mode = 1
            except AttributeError:
                pass
            # Salviamo anche le dimensioni per il get_rect
            self._cache_center_temp = (key, tex, surf.get_size())
        _, tex, (w, h) = self._cache_center_temp
        rect = pygame.Rect(0, 0, w, h)
        rect.center = (cfg.center_x + cfg.center_temp_x_offset,
                       cfg.center_y + cfg.center_temp_y_offset)
        tex.draw(dstrect=rect)

    def _location_now(self) -> datetime:
        """Ora dell'orologio della POSIZIONE meteo (non del sistema).

        Usa `timezone_offset` (secondi da UTC) della One Call API, così lancette
        e orario digitale mostrano l'ora del luogo configurato anche se il
        sistema è su un fuso diverso. Fallback all'ora di sistema finché non
        arrivano dati (né API né cache: la risposta cache contiene comunque
        timezone_offset).
        """
        off = (self.weather_data.get("timezone_offset")
               if self.weather_data else None)
        if off is None:
            return datetime.now()
        return (datetime.now(timezone.utc)
                + timedelta(seconds=off)).replace(tzinfo=None)

    def _location_dt(self, ts: Any) -> datetime:
        """Converte un timestamp UNIX nell'ora della posizione (stesso fuso di
        _location_now). Per alba/tramonto, ore DETAIL, etichette grafico, allerte.
        """
        off = (self.weather_data.get("timezone_offset")
               if self.weather_data else None)
        if off is None:
            return datetime.fromtimestamp(ts)
        return (datetime.fromtimestamp(ts, timezone.utc)
                + timedelta(seconds=off)).replace(tzinfo=None)

    def _get_moon_phase(self) -> tuple[float, str]:
        """Ritorna (phase, name) per la fase lunare attuale.

        Strategia: usa il valore daily[0].moon_phase dall'API OpenWeatherMap
        se disponibile (astronomicamente esatto, calcolato dai server OWM con
        algoritmo Jean Meeus o equivalente). Fallback al calcolo locale
        approssimato (`icon_animations.moon_phase`) se l'API non ha ancora
        risposto o se il campo manca.

        Il valore API ha la stessa semantica del nostro:
          0   = luna nuova
          0.25 = primo quarto
          0.5  = luna piena
          0.75 = ultimo quarto
          1   = luna nuova (chiusura ciclo)

        Quindi possiamo applicare direttamente `icon_animations._phase_name()`
        sul valore per ottenere il nome (localizzato via cfg.language) della fase.
        """
        daily = self.weather_data.get("daily") if self.weather_data else None
        if daily and len(daily) > 0:
            api_phase = daily[0].get("moon_phase")
            if api_phase is not None:
                # Range [0,1]; il nome lo deriviamo dalla stessa funzione locale
                # (mappa phase → testo IT/EN) - non importa che la phase
                # provenga dall'API, la mappatura testuale è la nostra.
                phase = float(api_phase)
                name = icon_animations._phase_name(phase, self.cfg.language)
                return phase, name
        # Fallback: calcolo locale (precisione ±2-3 ore). Ricalcoliamo il nome
        # nella lingua configurata (moon_phase ritorna il nome in italiano).
        phase, _ = icon_animations.moon_phase(time.time())
        return phase, icon_animations._phase_name(phase, self.cfg.language)

    def _draw_moon_mini(self) -> None:
        """HANDS minimal: piccola icona luna senza etichetta.

        Cache: rebuild solo su cambio fase (~ogni 7h).
        """
        cfg = self.cfg
        if not cfg.show_moon:
            return
        phase, name = self._get_moon_phase()
        phase_key = round(phase, 2)
        cached = getattr(self, '_cache_moon_mini', None)
        if cached is None or cached[0] != phase_key:
            surf = icon_animations.render_moon_surface(
                cfg.moon_mini_size, phase,
                lit_color=parse_color(cfg.moon_lit_color),
                dark_color=parse_color(cfg.moon_dark_color),
                antialias_scale=cfg.moon_antialias_scale,
            )
            # No colorkey: la Surface è SRCALPHA, alpha-blend nativo SDL2
            tex = Texture.from_surface(self.rb.renderer, surf)
            try:
                tex.blend_mode = 1
            except AttributeError:
                pass
            self._cache_moon_mini = (phase_key, tex)
        _, moon_tex = self._cache_moon_mini
        size = cfg.moon_mini_size
        rect = pygame.Rect(0, 0, size, size)
        rect.center = (cfg.center_x, cfg.center_y + cfg.moon_mini_y_offset)
        moon_tex.draw(dstrect=rect)

    def _draw_moon(self) -> None:
        """Disegna la fase lunare sotto la temperatura (GPU diretto).

        Cache: rebuild Surface+Texture solo se la fase cambia di >=0.01
        (~7 ore), cioè quasi mai a runtime.
        """
        cfg = self.cfg
        if not cfg.show_moon:
            return
        # Fase corrente: usa l'API OWM se disponibile (astronomicamente
        # esatta), altrimenti fallback al calcolo locale.
        phase, name = self._get_moon_phase()
        # Cache: ricalcola solo se la fase cambia di almeno 0.01 (~7 ore)
        phase_key = round(phase, 2)
        if self._cache_moon is None or self._cache_moon[0] != phase_key:
            # SRCALPHA: alpha-blend nativo SDL2, no colorkey magenta
            surf = icon_animations.render_moon_surface(
                cfg.moon_size, phase,
                lit_color=parse_color(cfg.moon_lit_color),
                dark_color=parse_color(cfg.moon_dark_color),
                antialias_scale=cfg.moon_antialias_scale,
            )
            tex = Texture.from_surface(self.rb.renderer, surf)
            try:
                tex.blend_mode = 1
            except AttributeError:
                pass
            # Etichetta luna (se attiva)
            label_tex = None
            label_size = None
            if cfg.moon_show_label:
                label_surf = self.font_moon_label.render(
                    name, True, parse_color(cfg.moon_label_color)
                )
                label_tex = Texture.from_surface(self.rb.renderer, label_surf)
                try:
                    label_tex.blend_mode = 1
                except AttributeError:
                    pass
                label_size = label_surf.get_size()
            self._cache_moon = (phase_key, tex, label_tex, label_size)
            logging.info("Moon phase aggiornata: %.3f → %s", phase, name)
        _, moon_tex, label_tex, label_size = self._cache_moon
        # Blit della luna
        moon_size = cfg.moon_size
        moon_rect = pygame.Rect(0, 0, moon_size, moon_size)
        moon_rect.center = (cfg.center_x, cfg.center_y + cfg.moon_y_offset)
        moon_tex.draw(dstrect=moon_rect)
        # Etichetta
        if label_tex is not None and label_size is not None:
            lw, lh = label_size
            label_rect = pygame.Rect(0, 0, lw, lh)
            label_rect.midtop = (cfg.center_x,
                                 cfg.center_y + cfg.moon_y_offset + moon_size // 2 + 6)
            label_tex.draw(dstrect=label_rect)

    def _draw_sun_times(self) -> None:
        """Disegna gli orari di alba e tramonto sotto il center temp (GPU).

        Layout: "[sole emerso] HH:MM    [sole immerso] HH:MM"
        I dati vengono da current.sunrise / current.sunset (UNIX timestamp).
        Cache: rebuilt solo quando i timestamp cambiano (1-2 volte/giorno).
        """
        cfg = self.cfg
        if not cfg.show_sun_times or not self.weather_data:
            return
        current = self.weather_data.get("current") or {}
        sunrise_ts = current.get("sunrise")
        sunset_ts = current.get("sunset")
        if not sunrise_ts or not sunset_ts:
            return

        # Cache: rebuild solo se i timestamp cambiano
        cached = self._cache_sun_times
        if cached is None or cached[0] != sunrise_ts or cached[1] != sunset_ts:
            surf = self._build_sun_times_surface(sunrise_ts, sunset_ts)
            # La Surface usa il colorkey magenta come bg per trasparenza
            tex = Texture.from_surface(self.rb.renderer, surf)
            try:
                tex.blend_mode = 1
            except AttributeError:
                pass
            self._cache_sun_times = (sunrise_ts, sunset_ts, tex, surf.get_size())
        _, _, tex, (w, h) = self._cache_sun_times
        rect = pygame.Rect(0, 0, w, h)
        rect.center = (cfg.center_x, cfg.center_y + cfg.sun_times_y_offset)
        tex.draw(dstrect=rect)

    def _build_sun_times_surface(self, sunrise_ts: int, sunset_ts: int) -> pygame.Surface:
        """Costruisce la surface "🌅 HH:MM   🌇 HH:MM" con simboli stilizzati.

        I simboli sono disegnati manualmente con pygame.draw per coerenza
        col resto della UI (no dipendenza da emoji nel font).

        Strategia colore in SDL2: Surface SRCALPHA, niente colorkey.
        font.render(text, True, color) crea Surface SRCALPHA con alpha
        channel; Texture.from_surface() la gestisce correttamente.
        """
        cfg = self.cfg
        # Format orari
        sunrise = self._location_dt(sunrise_ts).strftime("%H:%M")
        sunset = self._location_dt(sunset_ts).strftime("%H:%M")
        text_color = parse_color(cfg.sun_times_color)
        sunrise_color = parse_color(cfg.sun_times_sunrise_color)
        sunset_color = parse_color(cfg.sun_times_sunset_color)

        # Render testo SENZA background → SRCALPHA con alpha channel
        sunrise_text = self.font_sun_times.render(sunrise, True, text_color)
        sunset_text = self.font_sun_times.render(sunset, True, text_color)

        icon_size = cfg.sun_times_icon_size
        icon_text_gap = 6
        pair_gap = cfg.sun_times_pair_gap

        # Larghezza totale: [icon] gap [text]  pair_gap  [icon] gap [text]
        sunrise_w = icon_size + icon_text_gap + sunrise_text.get_width()
        sunset_w = icon_size + icon_text_gap + sunset_text.get_width()
        total_w = sunrise_w + pair_gap + sunset_w
        total_h = max(icon_size, sunrise_text.get_height(), sunset_text.get_height())

        # Surface SRCALPHA: ogni pixel ha (R,G,B,A). Aree non disegnate
        # rimangono (0,0,0,0) trasparenti (NB: per Surface SRCALPHA usate solo
        # per blit di sub-Surface, fill iniziale non è problematico — il bug
        # Pygame#1165 è solo per fill seguito da pygame.draw primitives).
        surf = pygame.Surface((total_w, total_h), pygame.SRCALPHA)

        # === Sunrise: sole emerso (mezzo cerchio + raggi sopra) + testo ===
        sr_cx = icon_size // 2
        sr_cy = total_h // 2
        self._draw_sun_emerging(surf, sr_cx, sr_cy, icon_size,
                                sunrise_color, rising=True)
        # Testo dopo l'icona (blit SRCALPHA → SRCALPHA: alpha blending corretto)
        surf.blit(sunrise_text, sunrise_text.get_rect(
            midleft=(icon_size + icon_text_gap, total_h // 2)
        ))

        # === Sunset: sole immerso (mezzo cerchio + raggi sopra) + testo ===
        ss_x_start = sunrise_w + pair_gap
        ss_cx = ss_x_start + icon_size // 2
        ss_cy = total_h // 2
        self._draw_sun_emerging(surf, ss_cx, ss_cy, icon_size,
                                sunset_color, rising=False)
        surf.blit(sunset_text, sunset_text.get_rect(
            midleft=(ss_x_start + icon_size + icon_text_gap, total_h // 2)
        ))

        return surf

    def _draw_sun_emerging(self, surf: pygame.Surface, cx: int, cy: int,
                            size: int, color: tuple[int, int, int],
                            rising: bool = True) -> None:
        """Disegna un sole che sorge (rising=True) o tramonta (rising=False).

        Composto da: mezzo cerchio (sopra orizzonte) + 3 raggi.
        Usiamo un polygon a forma di semicerchio invece di un cerchio completo
        + maschera, così funziona su Surface SRCALPHA senza pixel di colore
        di mascheramento (che creerebbero artefatti viola/colorkey issues).
        """
        r = size // 2 - 1
        sun_cy = cy + r // 3   # centro del sole spostato verso il basso
        # Semicerchio: approssimato con polygon
        # Punti lungo l'arco superiore + chiusura sulla linea orizzonte
        import math as _math
        n_points = 16   # smoothness del semicerchio
        points: list[tuple[int, int]] = []
        # Arco da -pi a 0 (semicerchio superiore)
        for i in range(n_points + 1):
            t = -_math.pi + (_math.pi * i / n_points)
            px = cx + r * _math.cos(t)
            py = sun_cy + r * _math.sin(t)
            points.append((int(px), int(py)))
        # Punti di chiusura: lato destro e sinistro della base
        # (già inclusi negli endpoint dell'arco)
        pygame.draw.polygon(surf, color, points)
        # Tre raggi sopra il sole
        ray_len = max(2, r // 2)
        ray_y_start = sun_cy - r - 1
        ray_y_end = ray_y_start - ray_len
        for dx in (-r, 0, r):
            x = cx + dx
            pygame.draw.line(surf, color, (x, ray_y_start), (x, ray_y_end),
                             max(1, size // 8))

    def _draw_wind_hands(self) -> None:
        """Visualizza il vento (velocità + direzione) in HANDS mode.

        Posizione: simmetrica rispetto a sun_times, ma sull'altro lato del
        pivot del quadrante. Layout: "💨 12 km/h NW" con freccia direzione.
        """
        cfg = self.cfg
        if not cfg.show_wind_hands:
            return
        if not self.weather_data:
            return
        current = self.weather_data.get("current") or {}
        # OpenWeather: wind_speed in m/s, wind_deg in gradi
        wind_speed_ms = current.get("wind_speed")
        wind_deg = current.get("wind_deg")
        if wind_speed_ms is None or wind_deg is None:
            return
        # m/s → km/h
        wind_kmh = round(wind_speed_ms * 3.6)
        # Direction in cardinal
        cardinal = self._wind_deg_to_cardinal(wind_deg)
        idx = int((wind_deg + 22.5) // 45) % 8
        cache_key = (wind_kmh, idx)
        cached = getattr(self, '_cache_wind_hands', None)
        if cached is None or cached[0] != cache_key:
            surf = self._build_wind_hands_surface(wind_kmh, cardinal, wind_deg)
            tex = Texture.from_surface(self.rb.renderer, surf)
            try:
                tex.blend_mode = 1
            except AttributeError:
                pass
            self._cache_wind_hands = (cache_key, tex, surf.get_size())
        _, tex, (w, h) = self._cache_wind_hands
        rect = pygame.Rect(0, 0, w, h)
        rect.center = (cfg.center_x + cfg.wind_hands_x_offset,
                       cfg.center_y + cfg.wind_hands_y_offset)
        tex.draw(dstrect=rect)

    def _make_wind_arrow_surface(self, wind_deg: int, size: int,
                                    color: tuple) -> pygame.Surface:
        """Crea una surface SRCALPHA con freccia direzione vento ruotata.

        OpenWeather wind_deg = direzione DA CUI proviene il vento.
        La freccia punta nella direzione VERSO cui va il vento.
        """
        arrow_surf = pygame.Surface((size + 4, size + 4), pygame.SRCALPHA)
        ax = arrow_surf.get_width() // 2
        ay = arrow_surf.get_height() // 2
        r = size // 2
        tip = (ax, ay - r)
        base_l = (ax - r // 2, ay + r // 2)
        base_r = (ax + r // 2, ay + r // 2)
        notch = (ax, ay + r // 4)
        pygame.draw.polygon(arrow_surf, color, [tip, base_r, notch, base_l])
        # Ruota: pygame ruota antiorario, vento DA 0° = freccia verso S = rotazione 180°
        rotated = pygame.transform.rotate(arrow_surf, -(wind_deg + 180))
        return rotated

    def _wind_deg_to_cardinal(self, wind_deg: int) -> str:
        """Converte gradi wind in punto cardinale (8 direzioni, IT)."""
        cardinals = ["N", "NE", "E", "SE", "S", "SO", "O", "NO"]
        idx = int((wind_deg + 22.5) // 45) % 8
        return cardinals[idx]

    def _build_wind_hands_surface(self, wind_kmh: int, cardinal: str,
                                    wind_deg: int) -> pygame.Surface:
        """Costruisce la surface "▲ 12 km/h NW" con freccia che punta verso
        la direzione di provenienza del vento (rotazione `wind_deg`).
        """
        cfg = self.cfg
        font = self._make_font(cfg.font_name, cfg.wind_hands_font_size, bold=True)
        text = f"{wind_kmh} km/h {cardinal}"
        text_surf = font.render(text, True, parse_color(cfg.wind_hands_color))
        text_w, text_h = text_surf.get_size()
        arrow_size = cfg.wind_hands_arrow_size
        gap = 12
        total_w = arrow_size + gap + text_w
        total_h = max(arrow_size, text_h)
        # SRCALPHA per coerenza con sun_times (no fringing magenta)
        surf = pygame.Surface((total_w + 4, total_h + 4), pygame.SRCALPHA)
        # Freccia direzione vento ruotata
        color = parse_color(cfg.wind_hands_arrow_color)
        rotated = self._make_wind_arrow_surface(wind_deg, arrow_size, color)
        rot_rect = rotated.get_rect(center=(arrow_size // 2 + 2, total_h // 2 + 2))
        surf.blit(rotated, rot_rect)
        # Testo a destra della freccia
        text_x = arrow_size + gap + 2
        text_y = (total_h - text_h) // 2 + 2
        surf.blit(text_surf, (text_x, text_y))
        return surf

    def _draw_chart(self) -> None:
        """Disegna il grafico temperatura 24h al centro (vista CHART).

        Layout (entro chart_panel_width x chart_panel_height):
          - Header "+24h" in alto
          - Curva temperatura con area fill
          - Barre POP in basso
          - Marker NOW (linea verticale)
          - Marker alba/tramonto sull'asse X
          - Labels min/max temp a sinistra
          - Tick labels orari (0/+6/+12/+18) in basso
        """
        if not self.weather_data:
            return
        cfg = self.cfg
        hourly = self.weather_data.get("hourly") or []
        n = min(cfg.chart_hours_to_show, len(hourly))
        if n < 2:
            return

        # Cache: rebuild solo se cambia dataset o version
        cache_key = (n, self._static_bg_version, id(hourly))
        cached = getattr(self, '_cache_chart', None)
        if cached is None or cached[0] != cache_key:
            surf = self._build_chart_surface(hourly, n)
            tex = Texture.from_surface(self.rb.renderer, surf)
            try:
                tex.blend_mode = 1
            except AttributeError:
                pass
            self._cache_chart = (cache_key, tex, surf.get_size())
        _, tex, (w, h) = self._cache_chart
        rect = pygame.Rect(0, 0, w, h)
        rect.center = (cfg.center_x, cfg.center_y)
        tex.draw(dstrect=rect)

    def _draw_current(self) -> None:
        """Vista CURRENT (swipe oriz da HANDS): dati attuali completi al
        centro. Mostra in modo pulito alba/tramonto, temperatura, percepita,
        vento+cardinale, UV, fase luna+nome.

        Architettura GPU-friendly (come WEEKLY):
          - Pannello base cached (testo, layout): rebuild solo a cambi dati
          - Icona luna blittata come Texture overlay separato (no rebuild)
        """
        if not self.weather_data:
            return
        cfg = self.cfg

        current = self.weather_data.get("current") or {}
        hourly = self.weather_data.get("hourly") or []
        # Cache key include pioggia attuale + POP prossima ora per rebuild
        # tempestivo quando cambiano (anche se temp/feels restano uguali).
        rain_now = 0.0
        if isinstance(current.get("rain"), dict):
            rain_now = current["rain"].get("1h", 0.0) or 0.0
        snow_now = 0.0
        if isinstance(current.get("snow"), dict):
            snow_now = current["snow"].get("1h", 0.0) or 0.0
        pop_next = (hourly[0].get("pop", 0) if hourly else 0) or 0
        cache_key = ("current", self._static_bg_version,
                     int(current.get("temp", 0) * 10),
                     int(current.get("feels_like", 0) * 10),
                     int(rain_now * 10), int(snow_now * 10),
                     int(pop_next * 100))
        cached = self._cache_current_panel
        rebuild = cached is None or cached[0] != hash(cache_key)
        if rebuild:
            panel, panel_pos, moon_pos = self._render_current_panel(current)
            tex = Texture.from_surface(self.rb.renderer, panel)
            try:
                tex.blend_mode = 1
            except AttributeError:
                pass
            size = panel.get_size()
            self._cache_current_panel = (hash(cache_key), tex, panel_pos, size, moon_pos)

        _, tex, panel_pos, size, moon_pos = self._cache_current_panel
        rect = pygame.Rect(panel_pos[0], panel_pos[1], size[0], size[1])
        tex.draw(dstrect=rect)

        # Overlay icona luna come Texture GPU (cache su phase_key)
        if cfg.show_moon and moon_pos is not None:
            phase, _name = self._get_moon_phase()
            phase_key = round(phase, 2)
            moon_size = cfg.current_moon_icon_size
            cached_m = getattr(self, '_cache_current_moon_tex', None)
            if cached_m is None or cached_m[0] != (phase_key, moon_size):
                surf = icon_animations.render_moon_surface(
                    moon_size, phase,
                    lit_color=parse_color(cfg.moon_lit_color),
                    dark_color=parse_color(cfg.moon_dark_color),
                    antialias_scale=cfg.moon_antialias_scale,
                )
                mtex = Texture.from_surface(self.rb.renderer, surf)
                try:
                    mtex.blend_mode = 1
                except AttributeError:
                    pass
                self._cache_current_moon_tex = ((phase_key, moon_size), mtex)
            _, mtex = self._cache_current_moon_tex
            mrect = pygame.Rect(panel_pos[0] + moon_pos[0],
                                 panel_pos[1] + moon_pos[1],
                                 moon_size, moon_size)
            mtex.draw(dstrect=mrect)

    def _render_current_panel(self, current: dict
                                ) -> tuple[pygame.Surface, tuple[int, int], tuple[int, int]]:
        """Costruisce il pannello CURRENT.

        Layout (380×380):
          ┌───────────────────────────────┐
          │           Adesso              │   header
          │ ─────────────────────────────│
          │  Temp        25°              │
          │  Percepita   27°              │
          │  Vento       12 km/h NE  ↓    │
          │  UV          5 (medio)        │
          │  Alba        06:23            │
          │  Tramonto    20:47            │
          │  Fase luna   Crescente  🌙    │
          └───────────────────────────────┘

        Ritorna (Surface, panel_pos, moon_icon_pos_in_panel).
        """
        cfg = self.cfg
        cx, cy = cfg.center_x, cfg.center_y
        lang = cfg.language

        panel_w = cfg.current_panel_width
        panel_h = cfg.current_panel_height
        panel = pygame.Surface((panel_w, panel_h))
        panel.fill(parse_color(cfg.background_color, (0, 0, 0)))

        # Font
        font_header = self._make_font(cfg.font_name, cfg.current_header_font_size, bold=True)
        font_lbl = self._make_font(cfg.font_name, cfg.current_label_font_size, bold=False)
        font_val = self._make_font(cfg.font_name, cfg.current_value_font_size, bold=True)
        col_lbl = parse_color(cfg.current_label_color)
        col_val = parse_color(cfg.current_value_color)
        col_hdr = parse_color(cfg.current_header_color)

        # Header (NO separator line per guadagnare spazio verticale)
        hdr_text = "Adesso" if lang == "it" else "Now"
        hdr_surf = font_header.render(hdr_text, True, col_hdr)
        panel.blit(hdr_surf, hdr_surf.get_rect(midtop=(panel_w // 2, 10)))
        # sep_y serve come baseline per il calcolo del rows_start_y
        sep_y = 10 + hdr_surf.get_height() + 4   # ridotto gap (era 8)

        # Costruisci le righe
        temp = current.get("temp")
        feels = current.get("feels_like")
        wind_speed_ms = current.get("wind_speed")
        wind_deg = current.get("wind_deg")
        sunrise_ts = current.get("sunrise")
        sunset_ts = current.get("sunset")
        _phase, moon_name = self._get_moon_phase()

        def fmt_temp(v):
            return f"{round(v)}{DEGREE}" if v is not None else "--"

        if wind_speed_ms is not None and wind_deg is not None:
            w_kmh = round(wind_speed_ms * 3.6)
            wcard = self._wind_deg_to_cardinal(wind_deg)
            wind_str = f"{w_kmh} km/h {wcard}"
        else:
            wind_str = "--"

        # Pioggia: combo "intensità attuale + probabilità prossima ora".
        # OWM current.rain.1h presente solo se sta piovendo (mm/h).
        # Aggiungiamo POP della prossima ora da hourly[0] per il contesto.
        # Più utile di UV in un cruscotto meteo da consultare quotidianamente.
        rain_now_mm = 0.0
        if isinstance(current.get("rain"), dict):
            rain_now_mm = current["rain"].get("1h", 0.0) or 0.0
        snow_now_mm = 0.0
        if isinstance(current.get("snow"), dict):
            snow_now_mm = current["snow"].get("1h", 0.0) or 0.0
        hourly = self.weather_data.get("hourly") or []
        pop_next = (hourly[0].get("pop", 0) if hourly else 0) or 0
        # Costruzione stringa:
        #   - se sta nevicando: "X.X mm (neve)"
        #   - se sta piovendo:  "X.X mm (Y%)"
        #   - se asciutto:      "0.0 mm (Y%)" (Y = probabilità prossima ora)
        if snow_now_mm > 0:
            rain_str = (f"{snow_now_mm:.1f} mm (neve)" if lang == "it"
                        else f"{snow_now_mm:.1f} mm (snow)")
        else:
            rain_str = f"{rain_now_mm:.1f} mm ({int(pop_next * 100)}%)"

        sr_str = self._location_dt(sunrise_ts).strftime("%H:%M") if sunrise_ts else "--"
        ss_str = self._location_dt(sunset_ts).strftime("%H:%M") if sunset_ts else "--"

        if lang == "it":
            rows = [
                ("Temperatura", fmt_temp(temp)),
                ("Percepita",   fmt_temp(feels)),
                ("Vento",       wind_str),
                ("Pioggia",     rain_str),
                ("Fase luna",   moon_name),
                ("Alba",        sr_str),
                ("Tramonto",    ss_str),
            ]
        else:
            rows = [
                ("Temperature", fmt_temp(temp)),
                ("Feels like",  fmt_temp(feels)),
                ("Wind",        wind_str),
                ("Rain",        rain_str),
                ("Moon phase",  moon_name),
                ("Sunrise",     sr_str),
                ("Sunset",      ss_str),
            ]

        # Layout: label a sinistra (col_lbl_x), value a destra (col_val_x)
        col_lbl_x = 30
        col_val_x = 180   # value allineato a sinistra dopo le label
        rows_start_y = sep_y + 14
        rows_total_h = panel_h - rows_start_y - 14
        line_h = rows_total_h // len(rows)

        moon_icon_pos = None
        for i, (lbl, val) in enumerate(rows):
            y = rows_start_y + i * line_h + line_h // 2
            lbl_surf = font_lbl.render(lbl, True, col_lbl)
            val_surf = font_val.render(val, True, col_val)
            panel.blit(lbl_surf,
                       lbl_surf.get_rect(midleft=(col_lbl_x, y)))
            panel.blit(val_surf,
                       val_surf.get_rect(midleft=(col_val_x, y)))

            # Per la riga "Vento", aggiungi anche la freccia direzione
            if lbl in ("Vento", "Wind") and wind_deg is not None:
                arrow_size = 22
                arrow_surf = self._make_wind_arrow_surface(
                    wind_deg, arrow_size, col_val
                )
                arrow_x = col_val_x + val_surf.get_width() + 10
                panel.blit(arrow_surf,
                           arrow_surf.get_rect(midleft=(arrow_x, y)))

            # Per la riga luna, calcola la posizione dell'icona overlay
            if lbl in ("Fase luna", "Moon phase"):
                moon_size = cfg.current_moon_icon_size
                moon_x = col_val_x + val_surf.get_width() + 16
                moon_y = y - moon_size // 2
                moon_icon_pos = (moon_x, moon_y)

        panel_pos = (cx - panel_w // 2, cy - panel_h // 2)
        return safe_convert(panel), panel_pos, moon_icon_pos

    def _build_chart_surface(self, hourly: list, n: int) -> pygame.Surface:
        """Costruisce la Surface con il grafico temperatura 24h."""
        cfg = self.cfg
        W = cfg.chart_panel_width
        H = cfg.chart_panel_height
        surf = pygame.Surface((W, H), pygame.SRCALPHA)

        # --- Layout interno (panel 360×250) ---
        margin_left = 34
        margin_right = 10
        margin_top = 38
        margin_bottom = 28
        plot_x = margin_left
        plot_y = margin_top
        plot_w = W - margin_left - margin_right
        plot_h = H - margin_top - margin_bottom

        # --- Header "+24h" ---
        header_font = self._make_font(cfg.font_name, cfg.chart_header_font_size,
                                       bold=True)
        header_surf = header_font.render(f"+{cfg.chart_hours_to_show}h", True,
                                          parse_color(cfg.chart_header_color))
        header_rect = header_surf.get_rect(center=(W // 2, margin_top // 2 + 4))
        surf.blit(header_surf, header_rect)

        # --- Dati temperatura ---
        temps = [hourly[i].get("temp") or 0 for i in range(n)]
        t_min = min(temps)
        t_max = max(temps)
        # Padding sull'asse Y per non far toccare i bordi
        t_range = max(2.0, t_max - t_min)
        t_min_pad = t_min - t_range * 0.15
        t_max_pad = t_max + t_range * 0.15

        # --- Y labels (min e max) ---
        label_font = self._make_font(cfg.font_name, cfg.chart_temp_label_font_size,
                                      bold=True)
        small_font = self._make_font(cfg.font_name, cfg.chart_label_font_size,
                                      bold=False)
        label_color = parse_color(cfg.chart_label_color)
        # max in alto sinistra
        max_lbl = label_font.render(f"{round(t_max)}°", True, label_color)
        surf.blit(max_lbl, (8, plot_y - 4))
        # min in basso sinistra
        min_lbl = label_font.render(f"{round(t_min)}°", True, label_color)
        surf.blit(min_lbl, (8, plot_y + plot_h - min_lbl.get_height() + 4))

        # --- Asse orizzontale (linea sottile sotto il grafico) ---
        axis_color = parse_color(cfg.chart_axis_color)
        grid_color = parse_color(cfg.chart_grid_color)
        pygame.draw.line(surf, axis_color,
                         (plot_x, plot_y + plot_h),
                         (plot_x + plot_w, plot_y + plot_h), 1)

        # --- Griglia orizzontale (3 livelli interni) ---
        for i in range(1, 4):
            gy = plot_y + plot_h - int(plot_h * i / 4)
            pygame.draw.line(surf, grid_color,
                             (plot_x, gy), (plot_x + plot_w, gy), 1)

        # --- Helper: x/y per indice ora i ---
        def x_for(i: int) -> int:
            return plot_x + int(i * plot_w / max(1, n - 1))

        def y_for_temp(t: float) -> int:
            frac = (t - t_min_pad) / max(0.1, t_max_pad - t_min_pad)
            return plot_y + plot_h - int(frac * plot_h)

        # --- Area sotto la curva (fill gradient-like) ---
        temp_color = parse_color(cfg.chart_temp_color)
        fill_alpha = cfg.chart_temp_fill_alpha
        points = [(x_for(i), y_for_temp(temps[i])) for i in range(n)]
        area_pts = points + [(plot_x + plot_w, plot_y + plot_h),
                              (plot_x, plot_y + plot_h)]
        area_surf = pygame.Surface((W, H), pygame.SRCALPHA)
        pygame.draw.polygon(area_surf, (*temp_color[:3], fill_alpha), area_pts)
        surf.blit(area_surf, (0, 0))

        # --- Curva temperatura (antialias, 2 passate per spessore) ---
        if len(points) >= 2:
            pygame.draw.aalines(surf, temp_color, False, points, 1)
            offset_points = [(x, y - 1) for (x, y) in points]
            pygame.draw.aalines(surf, temp_color, False, offset_points, 1)

        # --- Marker NOW (linea verticale tratteggiata bianca) ---
        now_color = parse_color(cfg.chart_now_marker_color)
        nx = plot_x
        y_cursor = plot_y
        while y_cursor < plot_y + plot_h:
            y_end = min(y_cursor + 4, plot_y + plot_h)
            pygame.draw.line(surf, now_color, (nx, y_cursor), (nx, y_end), 1)
            y_cursor += 7

        # --- Marker alba/tramonto come piccole frecce sotto l'asse ---
        # Più puliti dei cerchi precedenti.
        current = self.weather_data.get("current") or {}
        daily = self.weather_data.get("daily") or []
        now_ts = (hourly[0].get("dt") or 0) if hourly else 0
        end_ts = now_ts + n * 3600
        sunrise_color = parse_color(cfg.chart_sunrise_marker_color)
        sunset_color = parse_color(cfg.chart_sunset_marker_color)
        marker_y = plot_y + plot_h + 5

        # Raccogli tutti gli eventi sole nel range [now_ts, end_ts]:
        # current di oggi + daily[0..1] di oggi/domani (per coprire 24h)
        events: list[tuple[int, bool]] = []  # (ts, is_sunrise)
        if current.get("sunrise"):
            events.append((current["sunrise"], True))
        if current.get("sunset"):
            events.append((current["sunset"], False))
        for d in daily[:3]:
            if d.get("sunrise"):
                events.append((d["sunrise"], True))
            if d.get("sunset"):
                events.append((d["sunset"], False))
        # Dedup (current.sunrise di oggi == daily[0].sunrise)
        seen = set()
        unique_events = []
        for ts, is_sr in events:
            if ts not in seen:
                seen.add(ts)
                unique_events.append((ts, is_sr))

        for evt_ts, evt_is_sunrise in unique_events:
            if now_ts <= evt_ts <= end_ts:
                evt_color = sunrise_color if evt_is_sunrise else sunset_color
                hours_offset = (evt_ts - now_ts) / 3600.0
                ex = plot_x + int(hours_offset * plot_w / max(1, n - 1))
                tri_w = 6
                tri_h = 6
                if evt_is_sunrise:
                    pts = [(ex, marker_y), (ex - tri_w // 2, marker_y + tri_h),
                            (ex + tri_w // 2, marker_y + tri_h)]
                else:
                    pts = [(ex, marker_y + tri_h),
                            (ex - tri_w // 2, marker_y),
                            (ex + tri_w // 2, marker_y)]
                pygame.draw.polygon(surf, evt_color, pts)

        # --- Tick markers X ogni 2 ore (tick), label ogni 3 ore (8 in 24h) ---
        for h_off in range(0, n, 2):
            tick_x = x_for(h_off)
            tick_height = 4 if h_off % 3 == 0 else 2
            pygame.draw.line(surf, axis_color,
                             (tick_x, plot_y + plot_h),
                             (tick_x, plot_y + plot_h + tick_height), 1)
        # Label ogni 3 ore = 8 etichette in 24h (più informazione vs solo 4)
        for h_off in range(0, n, 3):
            tick_x = x_for(h_off)
            ts = (hourly[h_off].get("dt") or 0) if h_off < n else 0
            if ts:
                hh = self._location_dt(ts).hour
                lbl = small_font.render(f"{hh:02d}", True, label_color)
                lbl_x = tick_x - lbl.get_width() // 2
                lbl_y = plot_y + plot_h + 12
                surf.blit(lbl, (lbl_x, lbl_y))

        return surf

    def _draw_freshness(self) -> None:
        cfg = self.cfg
        if not cfg.show_freshness:
            return
        # In DETAIL la freshness si sovrappone al testo del pannello → la nascondiamo
        if self.mode == MODE_DETAIL:
            return
        if self.last_fetch_time is None:
            text = "—"
            color = parse_color(cfg.freshness_color)
        else:
            age_s = int((datetime.now() - self.last_fetch_time).total_seconds())
            template = FRESHNESS_LABELS.get(cfg.language, FRESHNESS_LABELS["en"])
            text = template.format(age=format_age(age_s))
            stale = age_s > cfg.freshness_stale_minutes * 60
            color = parse_color(cfg.freshness_stale_color if stale else cfg.freshness_color)
        key = (text, color)
        if self._cache_freshness is None or self._cache_freshness[0] != key:
            surf = self.font_freshness.render(text, True, color)
            tex = Texture.from_surface(self.rb.renderer, surf)
            try:
                tex.blend_mode = 1
            except AttributeError:
                pass
            self._cache_freshness = (key, tex, surf.get_size())
        _, tex, (w, h) = self._cache_freshness
        # Posizione: sotto la luna
        rect = pygame.Rect(0, 0, w, h)
        rect.center = (cfg.center_x, cfg.center_y + cfg.freshness_y_offset)
        tex.draw(dstrect=rect)

    def _update_alert_rotation(self) -> None:
        """Avanza l'indice di rotazione tra più allerte se necessario.

        Chiamato dal main loop PRIMA del calcolo della signature, così
        un cambio di alert attiva → signature cambia → forza il redraw.
        Se messo dentro `_draw_alert` la signature sarebbe stale.
        """
        if not self._active_alerts_list:
            return
        n = len(self._active_alerts_list)
        if n <= 1:
            return
        now = time.time()
        if now - self._active_alerts_last_rotation >= self.cfg.alert_rotation_seconds:
            self._active_alerts_idx = (self._active_alerts_idx + 1) % n
            self._active_alerts_last_rotation = now
            self._active_alert = self._active_alerts_list[self._active_alerts_idx]

    def _draw_alert(self) -> None:
        """Banner allerta come pillola arrotondata centrata in alto, DENTRO
        il cerchio del case. Width auto-fit al testo (clamp a alert_max_width).

        Mostra `self._active_alert`. Con N>1 allerte aggiunge prefisso "[i/N]".
        Rotazione gestita da `_update_alert_rotation` chiamato dal main loop.

        Pattern GPU: la pillola intera (background arrotondato + testo) è
        cached come Texture e ridisegnata via `texture.draw()`. Costo ~0.5ms.
        """
        if not self._active_alerts_list or self._active_alert is None:
            return
        cfg = self.cfg
        n = len(self._active_alerts_list)

        # Costruisci testo con prefisso [i/N] se serve
        text = self._active_alert["text"]
        if n > 1:
            text = f"[{self._active_alerts_idx + 1}/{n}] {text}"

        # Cache: la pillola si rebuilda solo a cambio testo o config
        bg_color = parse_color(cfg.alert_bg_color)
        text_color = parse_color(cfg.alert_text_color)
        key = (text, cfg.alert_height, cfg.alert_padding_x,
               cfg.alert_max_width, cfg.alert_border_radius,
               bg_color, text_color)
        if self._cache_alert is None or self._cache_alert[0] != key:
            # Render testo
            text_surf = self.font_alert.render(text, True, text_color)
            text_w = text_surf.get_width()
            text_h = text_surf.get_height()
            # Pillola: padding orizz, altezza fissa. Clamp a max_width
            pill_w = min(text_w + 2 * cfg.alert_padding_x, cfg.alert_max_width)
            pill_h = cfg.alert_height
            # Se testo non entra, ricalcolo con truncate (rare ma sicuro)
            if text_w + 2 * cfg.alert_padding_x > cfg.alert_max_width:
                # Tronca con ellissi
                while text and (self.font_alert.size(text + "…")[0]
                                 + 2 * cfg.alert_padding_x > cfg.alert_max_width):
                    text = text[:-1]
                text = (text + "…") if text else ""
                text_surf = self.font_alert.render(text, True, text_color)
                text_w = text_surf.get_width()
                text_h = text_surf.get_height()
                pill_w = text_w + 2 * cfg.alert_padding_x
            # Costruisci la pillola su una Surface SRCALPHA
            pill = pygame.Surface((pill_w, pill_h), pygame.SRCALPHA)
            pygame.draw.rect(
                pill, (*bg_color, 255),
                pygame.Rect(0, 0, pill_w, pill_h),
                border_radius=cfg.alert_border_radius,
            )
            # Centra il testo nella pillola
            text_rect = text_surf.get_rect(center=(pill_w // 2, pill_h // 2))
            pill.blit(text_surf, text_rect)
            tex = Texture.from_surface(self.rb.renderer, pill)
            try:
                tex.blend_mode = 1
            except AttributeError:
                pass
            self._cache_alert = (key, tex, (pill_w, pill_h))

        _, tex, (pill_w, pill_h) = self._cache_alert
        rect = pygame.Rect(0, 0, pill_w, pill_h)
        rect.center = (cfg.center_x, cfg.center_y + cfg.alert_y_offset)
        tex.draw(dstrect=rect)

    def _draw_digital(self) -> None:
        """Digital mode: ora HH:MM:SS, data, temp + vento (GPU)."""
        cfg = self.cfg
        cx, cy = cfg.center_x, cfg.center_y
        now = self._location_now()
        color = parse_color(cfg.digital_color)

        # Helper inline per disegnare testo cached come texture
        def draw_text_at(text: str, font: pygame.font.Font, center_y_offset: int,
                          cache_attr: str) -> None:
            key = (text, color)
            cached = getattr(self, cache_attr)
            if cached is None or cached[0] != key:
                surf = font.render(text, True, color)
                tex = Texture.from_surface(self.rb.renderer, surf)
                try:
                    tex.blend_mode = 1
                except AttributeError:
                    pass
                cached = (key, tex, surf.get_size())
                setattr(self, cache_attr, cached)
            _, tex, (w, h) = cached
            rect = pygame.Rect(0, 0, w, h)
            rect.center = (cx, cy + center_y_offset)
            tex.draw(dstrect=rect)

        # Time HH:MM:SS — cambia ogni secondo
        time_str = now.strftime("%H:%M:%S")
        draw_text_at(time_str, self.font_digital_time, -50, '_cache_digital_time')

        # Date — cambia solo a mezzanotte
        date_str = f"{localize_day(cfg.language, now)} {now.day} {localize_month(cfg.language, now)}"
        draw_text_at(date_str, self.font_digital_date, 35, '_cache_digital_date')

        # Sun times in alto (come HANDS)
        self._draw_sun_times()

        # Temp + freccia direzione + vento + cardinale — blob unico (come HANDS)
        if self.weather_data:
            current = self.weather_data.get("current", {})
            t = current.get("temp")
            if t is not None:
                wind_speed_ms = current.get("wind_speed")
                wind_deg_v = current.get("wind_deg")
                has_wind = (wind_speed_ms is not None and wind_deg_v is not None)
                temp_text = f"{round(t)}{DEGREE}"
                if has_wind:
                    wind_kmh = round(wind_speed_ms * 3.6)
                    cardinal = self._wind_deg_to_cardinal(wind_deg_v)
                    idx = int((wind_deg_v + 22.5) // 45) % 8
                    cache_key = (temp_text, wind_kmh, idx, color)
                else:
                    wind_kmh = None
                    cardinal = ""
                    cache_key = (temp_text, None, None, color)

                cached = getattr(self, '_cache_digital_temp_wind', None)
                if cached is None or cached[0] != cache_key:
                    blob = self._build_temp_wind_blob(
                        temp_text=temp_text,
                        wind_kmh=wind_kmh,
                        wind_deg=wind_deg_v,
                        cardinal=cardinal,
                        font_temp=self.font_digital_temp,
                        font_wind=self.font_digital_temp,
                        arrow_size=int(cfg.digital_temp_font_size * 0.7),
                        temp_color=color,
                        wind_color=color,
                        arrow_color=color,
                        gap_temp_arrow=18,
                        gap_arrow_text=6,
                    )
                    tex = Texture.from_surface(self.rb.renderer, blob)
                    try:
                        tex.blend_mode = 1
                    except AttributeError:
                        pass
                    self._cache_digital_temp_wind = (cache_key, tex, blob.get_size())
                _, tex, (bw, bh) = self._cache_digital_temp_wind
                rect = pygame.Rect(0, 0, bw, bh)
                rect.center = (cx, cy + 90)
                tex.draw(dstrect=rect)

    def _draw_detail(self) -> None:
        """Disegna il pannello DETAIL con la pagina corrente + indicatore pagine.

        Le pagine sono:
          0 = base (giorno, ora, temp, feels, pop, rain, wind)
          1 = atmosferica (pressione, umidità, UV, rugiada, visibilità, ecc)
        Il grafico temperatura 24h è stato estratto nella vista MODE_CHART
        (raggiungibile via swipe oriz da HANDS).
        """
        if not self.weather_data:
            return
        cfg = self.cfg
        hourly = self.weather_data.get("hourly") or []
        if self.detail_hours_ahead >= len(hourly):
            return
        offset = self.detail_hours_ahead
        page = self.detail_page

        # Use cached panel if same offset, page, and data version
        cache_key = (offset, page, self._static_bg_version)
        cached = self._cache_detail_panel
        if cached is not None and cached[0] == hash(cache_key):
            tex, panel_pos, size = cached[1], cached[2], cached[3]
            rect = pygame.Rect(panel_pos[0], panel_pos[1], size[0], size[1])
            tex.draw(dstrect=rect)
            self._draw_page_dots()
            return

        # Dispatch al renderer della pagina (solo 0=base e 1=atmospheric;
        # la pagina chart è stata estratta nella vista MODE_CHART)
        if page == 0:
            panel, panel_pos = self._render_detail_page_base(hourly, offset)
        elif page == 1:
            panel, panel_pos = self._render_detail_page_atmospheric(hourly, offset)
        else:
            return
        # Converti il panel Surface in Texture e cachalo
        tex = Texture.from_surface(self.rb.renderer, panel)
        try:
            tex.blend_mode = 1
        except AttributeError:
            pass
        size = panel.get_size()
        self._cache_detail_panel = (hash(cache_key), tex, panel_pos, size)
        rect = pygame.Rect(panel_pos[0], panel_pos[1], size[0], size[1])
        tex.draw(dstrect=rect)
        self._draw_page_dots()

    def _render_detail_page_base(self, hourly: list, offset: int
                                  ) -> tuple[pygame.Surface, tuple[int, int]]:
        """Pagina 0: Giorno, Ora, Temp, Percep, POP, Pioggia, Neve, Vento (con dir).

        NOTA: usiamo sempre `hourly[offset]` anche per offset=0 (ora corrente).
        DETAIL rappresenta la PREVISIONE del modello meteo per ogni ora del
        quadrante; può differire significativamente dal dato real-time
        (`current.*`) che è mostrato in CURRENT view (swipe oriz da HANDS).
        Le due fonti hanno semantica diversa:
          - hourly[0]: forecast del modello per l'ora in corso
          - current:   dato real-time (stazione meteo locale, può divergere)
        L'utente vede entrambe le viste con swipe → trasparenza dei dati.
        """
        cfg = self.cfg
        entry = hourly[offset]
        target_dt = self._location_dt(entry.get("dt", time.time()))
        # Pioggia: sempre numerico (anche 0.0 mm), mai trattini.
        # OpenWeatherMap fornisce entry["rain"]["1h"] in mm/h se piove,
        # altrimenti la chiave non c'è → 0.0.
        rain_mm = 0.0
        if isinstance(entry.get("rain"), dict):
            rain_mm = entry["rain"].get("1h", 0.0) or 0.0
        rain_str = f"{rain_mm:.1f} mm"
        # Neve: stesso pattern. entry["snow"]["1h"] se nevica.
        snow_mm = 0.0
        if isinstance(entry.get("snow"), dict):
            snow_mm = entry["snow"].get("1h", 0.0) or 0.0
        snow_str = f"{snow_mm:.1f} mm"
        w_val, w_unit = wind_display(entry.get("wind_speed", 0), cfg.units)
        # Direzione vento (cardinale). round a intero per non tagliare valore.
        wind_deg = entry.get("wind_deg")
        if wind_deg is not None:
            cardinal = self._wind_deg_to_cardinal(wind_deg)
            wind_str = f"{round(w_val)} {w_unit} {cardinal}"
        else:
            wind_str = f"{round(w_val)} {w_unit}"
        lang = cfg.language
        snow_label = "Neve" if lang == "it" else "Snow"
        rows = [
            (localize_label(lang, "Day"),   localize_day(lang, target_dt)),
            (localize_label(lang, "Hour"),  target_dt.strftime("%H:%M")),
            (localize_label(lang, "Temp"),  f"{round(entry.get('temp', 0))}{DEGREE}"),
            (localize_label(lang, "Feels"), f"{round(entry.get('feels_like', 0))}{DEGREE}"),
            (localize_label(lang, "POP"),   f"{int(entry.get('pop', 0) * 100)} %"),
            (localize_label(lang, "Rain"),  rain_str),
            (snow_label,                    snow_str),
            (localize_label(lang, "Wind"),  wind_str),
        ]
        return self._build_text_panel(rows)

    def _render_detail_page_atmospheric(self, hourly: list, offset: int
                                         ) -> tuple[pygame.Surface, tuple[int, int]]:
        """Pagina 1: Pressione (con trend), Umidità, UV (con qualificatore),
        Rugiada, Visibilità, Nuvolosità, Raffica.
        """
        cfg = self.cfg
        # Stessa logica di page_base: usa hourly[offset] (previsione modello)
        # anche per offset=0, NON merging con `current`. Vedi nota in page_base.
        entry = hourly[offset]

        # Pressione con trend: confronta con ora successiva (forecast)
        pressure = entry.get("pressure")
        trend = ""
        if pressure is not None and offset + 1 < len(hourly):
            next_p = hourly[offset + 1].get("pressure", pressure)
            diff = next_p - pressure
            if diff > 0.5:
                trend = " ↗"
            elif diff < -0.5:
                trend = " ↘"
            else:
                trend = " →"
        pressure_str = f"{int(pressure)} hPa{trend}" if pressure is not None else "--"

        # UV index con qualificatore italiano (mai trattini, anche 0.0)
        uvi = entry.get("uvi", 0) or 0
        if uvi < 2.5:    uv_label = "basso"
        elif uvi < 5.5:  uv_label = "medio"
        elif uvi < 7.5:  uv_label = "alto"
        elif uvi < 10.5: uv_label = "molto alto"
        else:            uv_label = "estremo"
        uv_str = f"{uvi:.1f} {uv_label}"

        # Visibilità in km
        vis_m = entry.get("visibility", 0)
        vis_str = f"{vis_m / 1000:.0f} km" if vis_m else "--"

        # Raffica
        gust = entry.get("wind_gust")
        if gust:
            g_val, g_unit = wind_display(gust, cfg.units)
            gust_str = f"{round(g_val, 1)} {g_unit}"
        else:
            gust_str = "--"

        # Etichette italiane fisse (non in DETAIL_LABELS perché specifiche
        # di questa pagina). Tradotte direttamente qui.
        lang = cfg.language
        if lang == "it":
            labels = ("Pressione", "Umidità", "UV", "Rugiada",
                      "Visibilità", "Nuvole", "Raffica")
        else:
            labels = ("Pressure", "Humidity", "UV", "Dew point",
                      "Visibility", "Clouds", "Gust")

        rows = [
            (labels[0], pressure_str),
            (labels[1], f"{entry.get('humidity', 0)} %"),
            (labels[2], uv_str),
            (labels[3], f"{round(entry.get('dew_point', 0), 1)}{DEGREE}"),
            (labels[4], vis_str),
            (labels[5], f"{entry.get('clouds', 0)} %"),
            (labels[6], gust_str),
        ]
        return self._build_text_panel(rows)

    def _build_text_panel(self, rows: list[tuple[str, str]]
                           ) -> tuple[pygame.Surface, tuple[int, int]]:
        """Costruisce un pannello a 2 colonne (label | value) per le pagine
        testuali. Restituisce (surface_opaca, posizione_blit).
        """
        cfg = self.cfg
        cx, cy = cfg.center_x, cfg.center_y
        lbl_color = parse_color(cfg.detail_label_color)
        val_color = parse_color(cfg.detail_value_color)
        rendered_rows = [
            (self.font_detail_label.render(label, True, lbl_color),
             self.font_detail_value.render(value, True, val_color))
            for label, value in rows
        ]
        max_lbl_w = max(s.get_width() for s, _ in rendered_rows)
        max_val_w = max(s.get_width() for _, s in rendered_rows)
        margin = 12
        # IMPORTANTE: il divider deve essere al CENTRO REALE tra label e value,
        # NON a panel_w//2. Altrimenti se max_lbl_w ≠ max_val_w il value sfora
        # oltre il bordo destro del pannello (bug fix: vento "12 km/h SO" tagliato).
        divider_x = margin + max_lbl_w + abs(cfg.detail_label_x_offset)
        panel_w = (divider_x + cfg.detail_divider_width
                   + abs(cfg.detail_value_x_offset) + max_val_w + margin)
        panel_h = cfg.detail_divider_half_height * 2 + margin * 2

        panel = pygame.Surface((panel_w, panel_h))
        panel.fill(parse_color(cfg.background_color, (0, 0, 0)))
        divider_center_x = divider_x + cfg.detail_divider_width // 2
        pcy = panel_h // 2

        # Divider verticale (al CENTRO REALE tra label e value, non panel center)
        pygame.draw.line(
            panel, parse_color(cfg.detail_divider_color),
            (divider_center_x, pcy - cfg.detail_divider_half_height),
            (divider_center_x, pcy + cfg.detail_divider_half_height),
            cfg.detail_divider_width,
        )

        spacing = cfg.detail_line_spacing
        total_h = (len(rendered_rows) - 1) * spacing
        y0 = pcy - total_h / 2
        for i, (lbl_surf, val_surf) in enumerate(rendered_rows):
            y = int(y0 + i * spacing)
            panel.blit(lbl_surf, lbl_surf.get_rect(
                midright=(divider_center_x + cfg.detail_label_x_offset, y)
            ))
            panel.blit(val_surf, val_surf.get_rect(
                midleft=(divider_center_x + cfg.detail_value_x_offset, y)
            ))

        panel_pos = (cx - panel_w // 2, cy - panel_h // 2 + cfg.detail_panel_y_offset)
        return safe_convert(panel), panel_pos

    def _render_detail_page_chart(self, hourly: list, offset: int
                                    ) -> tuple[pygame.Surface, tuple[int, int]]:
        """[DEPRECATED] Pagina 2 DETAIL: mini sparkline temperatura 24h.

        Funzionalità spostata nella vista MODE_CHART globale (swipe oriz
        da HANDS). Funzione lasciata come riferimento storico.
        """
        cfg = self.cfg
        cx, cy = cfg.center_x, cfg.center_y

        # Prendi le prossime 24 temperature (a partire dall'ora corrente, non da offset)
        n = min(24, len(hourly))
        temps = [hourly[i].get("temp", 0) for i in range(n)]
        if not temps:
            # Fallback: pannello vuoto
            panel = pygame.Surface((200, 100))
            panel.fill((0, 0, 0))
            return safe_convert(panel), (cx - 100, cy - 50)

        t_min = min(temps)
        t_max = max(temps)
        # Se min==max evita divisione per zero
        t_range = max(0.5, t_max - t_min)

        # Dimensioni pannello (più largo per il grafico)
        panel_w = 280
        panel_h = 340
        panel = pygame.Surface((panel_w, panel_h))
        panel.fill(parse_color(cfg.background_color, (0, 0, 0)))

        margin_x = 24
        margin_top = 50      # spazio per il titolo
        margin_bottom = 60   # spazio per asse X + footer

        chart_x0 = margin_x
        chart_x1 = panel_w - margin_x
        chart_y0 = margin_top
        chart_y1 = panel_h - margin_bottom
        chart_w = chart_x1 - chart_x0
        chart_h = chart_y1 - chart_y0

        # Titolo
        title_color = parse_color(cfg.detail_label_color)
        title_text = "Temperatura 24h" if cfg.language == "it" else "Temperature 24h"
        title_surf = self.font_detail_label.render(title_text, True, title_color)
        panel.blit(title_surf, title_surf.get_rect(
            midtop=(panel_w // 2, 12)
        ))

        # Calcola i punti del grafico in coordinate panel
        def y_for_temp(t):
            return chart_y1 - ((t - t_min) / t_range) * chart_h

        points = []
        for i, t in enumerate(temps):
            x = chart_x0 + (i / max(1, n - 1)) * chart_w
            y = y_for_temp(t)
            points.append((x, y))

        # Griglia leggera (3 linee orizzontali: min, mid, max)
        grid_color = (60, 60, 60)
        for frac in (0.0, 0.5, 1.0):
            y = chart_y0 + frac * chart_h
            pygame.draw.line(panel, grid_color,
                             (chart_x0, y), (chart_x1, y), 1)

        # Tick orizzontali ogni 6h: 0, 6, 12, 18, 23(=24)
        tick_color = (90, 90, 90)
        tick_font = self.font_freshness
        for h_offset in (0, 6, 12, 18, 23):
            if h_offset >= n:
                continue
            x = chart_x0 + (h_offset / max(1, n - 1)) * chart_w
            pygame.draw.line(panel, tick_color,
                             (x, chart_y1), (x, chart_y1 + 4), 1)
            label = "ora" if h_offset == 0 and cfg.language == "it" \
                else ("now" if h_offset == 0 else f"+{h_offset}h")
            lbl_s = tick_font.render(label, True, tick_color)
            panel.blit(lbl_s, lbl_s.get_rect(midtop=(int(x), int(chart_y1 + 6))))

        # Linea del grafico (bianca, 2px)
        line_color = parse_color("#ffffff")
        if len(points) >= 2:
            pygame.draw.lines(panel, line_color, False, points, 2)

        # Pallini sui punti min e max
        min_idx = temps.index(t_min)
        max_idx = temps.index(t_max)
        # Max in arancione, Min in azzurro
        max_color = (255, 165, 0)
        min_color = (90, 170, 230)
        pygame.draw.circle(panel, max_color,
                           (int(points[max_idx][0]), int(points[max_idx][1])), 4)
        pygame.draw.circle(panel, min_color,
                           (int(points[min_idx][0]), int(points[min_idx][1])), 4)

        # Etichetta temp del max sopra il pallino, min sotto
        lbl_max = self.font_freshness.render(
            f"{round(t_max)}{DEGREE}", True, max_color)
        lbl_min = self.font_freshness.render(
            f"{round(t_min)}{DEGREE}", True, min_color)
        # max sopra il punto
        panel.blit(lbl_max, lbl_max.get_rect(midbottom=(
            int(points[max_idx][0]), int(points[max_idx][1]) - 4)))
        # min sotto il punto
        panel.blit(lbl_min, lbl_min.get_rect(midtop=(
            int(points[min_idx][0]), int(points[min_idx][1]) + 4)))

        # Footer con riepilogo
        footer_color = parse_color(cfg.detail_label_color)
        footer_text = f"min {round(t_min)}{DEGREE}   max {round(t_max)}{DEGREE}"
        footer_surf = self.font_moon_label.render(footer_text, True, footer_color)
        panel.blit(footer_surf, footer_surf.get_rect(
            midbottom=(panel_w // 2, panel_h - 12)
        ))

        panel_pos = (cx - panel_w // 2, cy - panel_h // 2)
        return safe_convert(panel), panel_pos

    def _draw_page_dots(self) -> None:
        """Disegna i pallini indicatore di pagina in basso (●○○).

        I pallini sono piccoli quadrati colorati via renderer.fill_rect()
        (SDL2 non disegna cerchi nativi; sotto i 6px non si nota).
        """
        cfg = self.cfg
        n = cfg.detail_n_pages
        if n <= 1:
            return
        spacing = cfg.detail_page_dots_spacing
        total_w = (n - 1) * spacing
        y = cfg.center_y + cfg.detail_page_dots_y_offset
        x0 = cfg.center_x - total_w // 2
        active_color = parse_color(cfg.detail_page_dot_color_active)
        inactive_color = parse_color(cfg.detail_page_dot_color_inactive)
        r = cfg.detail_page_dot_radius
        for i in range(n):
            x = x0 + i * spacing
            color = active_color if i == self.detail_page else inactive_color
            self.rb.renderer.draw_color = (*color, 255)
            # fill_rect: pallino piccolo come quadratino (visivamente simile a un cerchio
            # per dimensioni <= 4-5 px)
            self.rb.renderer.fill_rect(pygame.Rect(x - r, y - r, 2 * r, 2 * r))

    # -----------------------------------------------------------------------
    # WEEKLY mode (previsione 7 giorni)
    # -----------------------------------------------------------------------

    # Cache per il pannello WEEKLY: ricostruito solo quando arrivano nuovi
    # dati (_static_bg_version cambia).

    def _draw_weekly(self) -> None:
        """Disegna il pannello WEEKLY con icone meteo animate.

        Architettura (come HANDS, per CPU minima su Pi Zero W):
          1. Pannello "base" cached: testo + layout + separatori, SENZA icone.
             Rebuild solo ai cambi dati (`_static_bg_version`).
          2. Icone meteo blittate ogni frame come Texture GPU separate.
             ~0.1ms/icona × 7 icone = ~1ms per frame anim. Niente Surface
             rebuild. Niente font.render. Niente Texture.from_surface.

        Prima del refactor: rebuild Surface 440×490 + 50× font.render +
        7× smoothscale + Texture.from_surface ad ogni anim step (10fps)
        = 50-100ms = 50-100% CPU su Pi Zero W. Ora ~1ms = 1% CPU.
        """
        if not self.weather_data:
            return
        cfg = self.cfg
        daily = self.weather_data.get("daily") or []
        if len(daily) < 2:
            self._draw_weekly_unavailable()
            return

        # Pannello base (NO icone): cached su data version. Costoso ma raro.
        # IMPORTANTE: usa _cache_weekly_panel (NON _cache_detail_panel che è
        # condiviso col DETAIL e creerebbe rebuild a ogni switch DETAIL↔WEEKLY,
        # con conseguente churn di Texture 440×490 in VRAM e crash su Pi
        # Zero W con poca VRAM).
        cache_key = ("weekly_base", self._static_bg_version)
        cached = getattr(self, '_cache_weekly_panel', None)
        rebuild = cached is None or cached[0] != hash(cache_key)
        if rebuild:
            panel, panel_pos, icon_positions = self._render_weekly_panel_base(daily)
            tex = Texture.from_surface(self.rb.renderer, panel)
            try:
                tex.blend_mode = 1
            except AttributeError:
                pass
            size = panel.get_size()
            # Cache estesa con icon_positions per i blit overlay
            self._cache_weekly_panel = (
                hash(cache_key), tex, panel_pos, size,
                icon_positions, [daily[i + 1] for i in range(min(7, len(daily) - 1))]
            )

        _, tex, panel_pos, size, icon_positions, entries = self._cache_weekly_panel
        rect = pygame.Rect(panel_pos[0], panel_pos[1], size[0], size[1])
        tex.draw(dstrect=rect)

        # Overlay icone animate in WEEKLY.
        # Le animazioni sono di nuovo abilitate perché lo stutter HANDS↔WEEKLY
        # era causato da TRANSITION_SLIDE_DOWN/UP che ri-renderizzava la
        # pipeline completa ogni frame della transizione (~15ms × 15 frame).
        # Quel fix ha eliminato la pressione sulla CPU che causava il crash.
        # La cache _cache_weekly_icon_textures cresce fino a ~5-7 nomi icona
        # × N_FRAMES texture, ~3 MB VRAM totali → trascurabile.
        anim_idx = self._anim_frame_idx if cfg.animate_icons else 0
        icon_size = cfg.weekly_icon_size
        for entry, (ix, iy) in zip(entries, icon_positions):
            icon_name = weather_to_icon(entry["weather"][0])
            self._draw_weekly_icon_tex_at(
                icon_name, anim_idx, icon_size,
                panel_pos[0] + ix, panel_pos[1] + iy
            )

        # Memory optimization: dopo che WEEKLY è stato disegnato per un
        # ciclo completo di animazione (N_FRAMES diversi), la cache
        # weekly_icon_textures è popolata per tutte le icone visibili.
        # Possiamo liberare gli icon_sheets Surface CPU (~14 MB) che non
        # servono più: le icone animate funzionano dalle Texture GPU
        # (icon_textures), e le icone WEEKLY funzionano dalla cache GPU
        # weekly_icon_textures. Solo le altre cache (CHART, DETAIL) potrebbero
        # ancora chiamare _icon_sheets[name][f_idx] al primo accesso.
        # Vedi _maybe_release_icon_sheets per dettagli.
        self._maybe_release_icon_sheets()

    def _maybe_release_icon_sheets(self) -> None:
        """Libera `self.icon_sheets` (Surface CPU, ~14 MB) dopo che le cache
        GPU sono popolate. Idempotente.

        Strategia conservativa: rilasciamo solo se:
          - opzione `release_icon_sheets_after_boot` è True
          - WEEKLY è stato visitato per un intero ciclo animazione
            (N_FRAMES diversi indici hanno generato cache miss)
          - sono passati almeno 30 secondi dal boot (margine sicurezza
            per evitare di rilasciare durante il primo render full)
        """
        if not self.cfg.release_icon_sheets_after_boot:
            return
        if not self.icon_sheets:
            return  # già rilasciato
        # Sicurezza: aspetta 30s dal boot prima di rilasciare
        if not hasattr(self, '_boot_mono_time'):
            self._boot_mono_time = time.monotonic()
            return
        if time.monotonic() - self._boot_mono_time < 30.0:
            return
        # Verifica che la cache weekly contenga abbastanza entries da
        # garantire copertura completa di animazione (anim_idx mod N_FRAMES
        # ha generato tutti i valori). Approssimazione: se cache ha
        # almeno N_FRAMES × 2 entries (numero icone visibili * frame), ok.
        cache = getattr(self, '_cache_weekly_icon_textures', {})
        n_frames = self.cfg.animation_n_frames
        if len(cache) < n_frames * 2:
            return
        # Rilascia
        mb_freed = (len(self.icon_sheets) * n_frames *
                      self.cfg.icon_size ** 2 * 4) / (1024 * 1024)
        self.icon_sheets = {}
        # Forza GC + malloc_trim per restituire memoria al kernel
        try:
            import gc
            gc.collect()
            try:
                import ctypes
                ctypes.CDLL("libc.so.6").malloc_trim(0)
            except Exception:
                pass
        except Exception:
            pass
        logging.info("icon_sheets Surface CPU rilasciate (~%.1f MB liberati). "
                      "Icone animate continuano da Texture GPU.", mb_freed)

    def _draw_weekly_icon_tex_at(self, icon_name: str, frame: int, size: int,
                                   x: int, y: int) -> None:
        """Blit GPU di una singola icona meteo animata WEEKLY.

        Cache di Texture keyed su (name, size, frame). Senza LRU: il dominio
        è naturalmente limitato dai dati reali (~3-5 nomi icone × N_FRAMES
        × 1 size = ~60-100 entries max). Non distruggere texture durante
        l'uso evita VRAM churn / frammentazione (SDL2 sotto KMSDRM ha
        deferred destruction che ritarda il rilascio fino al prossimo
        present, creando burst di allocazioni).
        Costo per chiamata:
          - cache hit (>99%): ~0.1ms (solo GPU draw)
          - cache miss (al primo frame di ogni combinazione): ~3-5ms
        """
        if not hasattr(self, "_cache_weekly_icon_textures"):
            self._cache_weekly_icon_textures: dict = {}
        cache = self._cache_weekly_icon_textures
        key = (icon_name, size, frame)
        tex = cache.get(key)
        if tex is None:
            sheet = self.icon_sheets.get(icon_name)
            if sheet is None:
                return
            f_idx = frame % len(sheet)
            surf = sheet[f_idx]
            if surf.get_size() != (size, size):
                surf = pygame.transform.smoothscale(surf, (size, size))
            tex = Texture.from_surface(self.rb.renderer, surf)
            try:
                tex.blend_mode = 1
            except AttributeError:
                pass
            cache[key] = tex
        rect = pygame.Rect(x, y, size, size)
        tex.draw(dstrect=rect)

    def _draw_weekly_unavailable(self) -> None:
        """Fallback se non abbiamo dati 'daily' (API ridotta o errore)."""
        cfg = self.cfg
        msg = "Previsioni settimanali non disponibili" if cfg.language == "it" \
            else "Weekly forecast not available"
        surf = self.font_detail_label.render(msg, True, parse_color(cfg.detail_label_color))
        w, h = surf.get_size()
        # Sfondo opaco via renderer.fill_rect
        bg_color = parse_color(cfg.background_color, (0, 0, 0))
        self.rb.renderer.draw_color = (*bg_color, 255)
        bg_rect = pygame.Rect(0, 0, w + 40, h + 20)
        bg_rect.center = (cfg.center_x, cfg.center_y)
        self.rb.renderer.fill_rect(bg_rect)
        # Testo come Texture (one-shot, non cachato perché raro)
        tex = Texture.from_surface(self.rb.renderer, surf)
        try:
            tex.blend_mode = 1
        except AttributeError:
            pass
        text_rect = pygame.Rect(0, 0, w, h)
        text_rect.center = (cfg.center_x, cfg.center_y)
        tex.draw(dstrect=text_rect)

    def _render_weekly_panel_base(self, daily: list
                                    ) -> tuple[pygame.Surface, tuple[int, int], list]:
        """Costruisce il pannello WEEKLY SENZA icone meteo (verranno
        blittate come Texture overlay separate ad ogni frame anim).
        Ritorna (Surface, panel_pos, icon_positions) dove icon_positions
        è lista di (x_in_panel, y_in_panel) per le 7 icone.
        """
        cfg = self.cfg
        cx, cy = cfg.center_x, cfg.center_y
        lang = cfg.language

        panel_w = cfg.weekly_panel_width
        panel_h = cfg.weekly_panel_height
        panel = pygame.Surface((panel_w, panel_h))
        panel.fill(parse_color(cfg.background_color, (0, 0, 0)))

        # === Header ===
        header_text = "Prossimi 7 giorni" if lang == "it" else "Next 7 days"
        header_surf = self.font_weekly_header.render(
            header_text, True, parse_color(cfg.weekly_header_color)
        )
        panel.blit(header_surf, header_surf.get_rect(midtop=(panel_w // 2, 8)))

        # === 7 righe ===
        row_h = cfg.weekly_row_height
        row_spacing = cfg.weekly_row_spacing
        margin_top = 12 + header_surf.get_height() + 12
        n_days = min(7, len(daily) - 1)
        sep_color = parse_color(cfg.weekly_row_separator_color)
        col_sep_color = parse_color(cfg.weekly_col_separator_color)
        col_left_w = cfg.weekly_col_left_width
        icon_size = cfg.weekly_icon_size

        icon_positions: list[tuple[int, int]] = []
        for i in range(n_days):
            entry = daily[i + 1]
            y = margin_top + i * (row_h + row_spacing)
            self._draw_weekly_row_base(panel, entry, y, panel_w, row_h, col_left_w)
            # Posizione dove l'icona meteo verrà blittata come overlay
            icon_x = col_left_w - icon_size - 8
            icon_y = y + (row_h - icon_size) // 2
            icon_positions.append((icon_x, icon_y))
            if i < n_days - 1:
                sep_y = y + row_h + row_spacing // 2
                pygame.draw.line(panel, sep_color,
                                 (16, sep_y), (panel_w - 16, sep_y), 1)
            pygame.draw.line(panel, col_sep_color,
                             (col_left_w, y + 6),
                             (col_left_w, y + row_h - 6), 1)

        panel_pos = (cx - panel_w // 2, cy - panel_h // 2)
        return safe_convert(panel), panel_pos, icon_positions

    def _draw_weekly_row_base(self, panel: pygame.Surface, entry: dict, y: int,
                                panel_w: int, row_h: int, col_left_w: int) -> None:
        """Disegna una riga WEEKLY SENZA l'icona meteo (verrà blittata come
        Texture overlay separato). Tutto il resto: testo, frecce vento,
        UV, ecc.
        """
        cfg = self.cfg
        lang = cfg.language

        # Nome giorno (abbreviato per stare in col sinistra compatta)
        target_dt = self._location_dt(entry.get("dt", time.time()))
        # 3 lettere maiuscole, più leggibile e nessun overlap con icona
        day_names_short_it = ["Lun", "Mar", "Mer", "Gio", "Ven", "Sab", "Dom"]
        day_names_short_en = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
        day_name = (day_names_short_it if lang == "it"
                    else day_names_short_en)[target_dt.weekday()]
        # Giorno del mese (es. "21 mag")
        months_it = ["gen", "feb", "mar", "apr", "mag", "giu",
                      "lug", "ago", "set", "ott", "nov", "dic"]
        months_en = ["jan", "feb", "mar", "apr", "may", "jun",
                      "jul", "aug", "sep", "oct", "nov", "dec"]
        month_abbr = (months_it if lang == "it" else months_en)[target_dt.month - 1]
        date_str = f"{target_dt.day} {month_abbr}"

        # === Colonna sinistra: nome giorno + data + icona ===
        # Layout vertical: giorno in alto, data sotto, icona a destra del testo
        day_surf = self.font_weekly_day.render(
            day_name, True, parse_color(cfg.weekly_day_color)
        )
        date_surf = self.font_weekly_stat.render(
            date_str, True, parse_color(cfg.weekly_stat_label_color)
        )
        # Posiziona giorno e data a sx
        text_x = 14
        text_block_h = day_surf.get_height() + date_surf.get_height() + 2
        text_y = y + (row_h - text_block_h) // 2
        panel.blit(day_surf, (text_x, text_y))
        panel.blit(date_surf, (text_x, text_y + day_surf.get_height() + 2))

        # NB: l'icona meteo NON viene disegnata qui. È blittata come Texture
        # overlay GPU separato in _draw_weekly() ad ogni frame animation step,
        # senza dover rebuild il pannello (CPU ~1% vs ~50-100% del rebuild).

        # === Colonna destra ===
        right_x0 = col_left_w + 14
        right_w = panel_w - right_x0 - 8

        # --- Riga 1: max/min temp + POP % (senza barra) ---
        temp = entry.get("temp", {})
        if isinstance(temp, dict):
            t_max = round(temp.get("max", 0))
            t_min = round(temp.get("min", 0))
        else:
            t_max = round(temp)
            t_min = round(temp)
        max_surf = self.font_weekly_temp.render(
            f"{t_max}{DEGREE}", True, parse_color(cfg.weekly_temp_max_color)
        )
        min_surf = self.font_weekly_temp.render(
            f"{t_min}{DEGREE}", True, parse_color(cfg.weekly_temp_min_color)
        )
        sep_surf = self.font_weekly_temp.render(
            "/", True, parse_color(cfg.weekly_temp_sep_color)
        )
        temp_y = y + 3
        x = right_x0
        panel.blit(max_surf, (x, temp_y))
        x += max_surf.get_width() + 4
        panel.blit(sep_surf, (x, temp_y))
        x += sep_surf.get_width() + 4
        panel.blit(min_surf, (x, temp_y))

        col_color = parse_color(cfg.weekly_stat_value_color)

        # --- Riga 1 dx: alba ▲ e tramonto ▼ ---
        sunrise_ts = entry.get("sunrise", 0)
        sunset_ts = entry.get("sunset", 0)
        if sunrise_ts and sunset_ts:
            sr_hh = self._location_dt(sunrise_ts).strftime("%H:%M")
            ss_hh = self._location_dt(sunset_ts).strftime("%H:%M")
            sun_text = self.font_weekly_stat.render(
                f"▲{sr_hh} ▼{ss_hh}", True, col_color
            )
            # Allineato a destra, baseline al centro delle temperature
            sun_y = temp_y + (max_surf.get_height() - sun_text.get_height()) // 2
            panel.blit(sun_text,
                       (right_x0 + right_w - sun_text.get_width() - 2, sun_y))

        # --- Riga 2: vento + UV + umidità + POP % ---
        stats_y = y + max_surf.get_height() + 6

        # Vento: freccia direzione + km/h
        wind_speed_ms = entry.get("wind_speed", 0) or 0
        wind_deg = entry.get("wind_deg", 0) or 0
        wind_kmh = round(wind_speed_ms * 3.6)
        arrow_color = parse_color(cfg.weekly_wind_arrow_color)
        arrow_size = 14
        wind_arrow = self._make_wind_arrow_surface(wind_deg, arrow_size, arrow_color)
        arrow_y = stats_y + (self.font_weekly_stat.get_height() - wind_arrow.get_height()) // 2
        panel.blit(wind_arrow, (right_x0, arrow_y))
        wind_text = self.font_weekly_stat.render(
            f"{wind_kmh}km/h", True, col_color
        )
        panel.blit(wind_text, (right_x0 + wind_arrow.get_width() + 2, stats_y))

        # UV (con colore per livello)
        uvi = entry.get("uvi", 0) or 0
        if uvi < 3:
            uv_color = parse_color(cfg.weekly_uv_low_color)
        elif uvi < 6:
            uv_color = parse_color(cfg.weekly_uv_mid_color)
        else:
            uv_color = parse_color(cfg.weekly_uv_high_color)
        uv_text = self.font_weekly_stat.render(
            f"UV {round(uvi)}", True, uv_color
        )
        uv_x = right_x0 + 90
        panel.blit(uv_text, (uv_x, stats_y))

        # Umidità
        humidity = entry.get("humidity", 0) or 0
        hum_text = self.font_weekly_stat.render(
            f"Um.{humidity}%", True, col_color
        )
        hum_x = right_x0 + 140
        panel.blit(hum_text, (hum_x, stats_y))

        # POP % (allineato a destra di riga 2, colore blu)
        pop = entry.get("pop", 0)
        pop_text = self.font_weekly_stat.render(
            f"P.{int(pop * 100)}%", True, parse_color(cfg.weekly_pop_color)
        )
        panel.blit(pop_text,
                   (right_x0 + right_w - pop_text.get_width() - 2, stats_y))

    def _get_weekly_icon(self, icon_name: str, size: int,
                          frame: int = 0) -> Optional[pygame.Surface]:
        """Ottiene icona meteo WEEKLY per il frame indicato (animata se
        frame > 0), scalata alla size richiesta. Cache locale keyed su
        (icon_name, size, frame).
        """
        if not hasattr(self, "_cache_weekly_icons"):
            self._cache_weekly_icons: dict[tuple[str, int, int], pygame.Surface] = {}
        key = (icon_name, size, frame)
        cached = self._cache_weekly_icons.get(key)
        if cached is not None:
            return cached
        sheet = self.icon_sheets.get(icon_name)
        if sheet is None:
            return None
        # Scegli il frame corretto (wrap se l'indice supera len(sheet))
        f_idx = frame % len(sheet) if sheet else 0
        base = sheet[f_idx]
        if base.get_size() != (size, size):
            scaled = pygame.transform.smoothscale(base, (size, size))
        else:
            scaled = base
        self._cache_weekly_icons[key] = scaled
        return scaled

    # -----------------------------------------------------------------------
    # Alerts report (MODE_ALERTS): elenco + dettaglio
    # -----------------------------------------------------------------------

    def _severity_color(self, severity: int) -> tuple[int, int, int]:
        """Colore associato alla severità (1=info, 2=warning, 3=danger)."""
        cfg = self.cfg
        if severity >= 3:
            return parse_color(cfg.alert_color_danger)
        if severity == 2:
            return parse_color(cfg.alert_color_warn)
        return parse_color(cfg.alert_color_info)

    @staticmethod
    def _ellipsize(font: pygame.font.Font, text: str, max_w: int) -> str:
        """Tronca `text` a una riga aggiungendo … se supera max_w px."""
        if not text or font.size(text)[0] <= max_w:
            return text
        ell = "…"
        t = text
        while t and font.size(t + ell)[0] > max_w:
            t = t[:-1]
        return (t + ell) if t else ell

    @staticmethod
    def _wrap_text(font: pygame.font.Font, text: str,
                   max_w: int) -> list[str]:
        """Word-wrap a larghezza pixel. Rispetta i newline espliciti e spezza
        le parole più lunghe di max_w. (Nessun helper simile esiste altrove.)
        """
        lines: list[str] = []
        for para in (text or "").split("\n"):
            para = para.rstrip()
            if not para:
                lines.append("")
                continue
            cur = ""
            for word in para.split(" "):
                if not word:
                    continue
                cand = word if not cur else cur + " " + word
                if font.size(cand)[0] <= max_w:
                    cur = cand
                    continue
                if cur:
                    lines.append(cur)
                    cur = ""
                if font.size(word)[0] > max_w:
                    piece = ""
                    for ch in word:
                        if font.size(piece + ch)[0] <= max_w:
                            piece += ch
                        else:
                            if piece:
                                lines.append(piece)
                            piece = ch
                    cur = piece
                else:
                    cur = word
            lines.append(cur)
        return lines

    def _fmt_alert_time(self, ts: Any, lang: str) -> str:
        """Formatta un timestamp OWM (unix) in ora locale; prefissa il giorno
        abbreviato se la data è diversa da oggi."""
        try:
            dt = self._location_dt(ts)
        except (OSError, OverflowError, ValueError, TypeError):
            return ""
        hm = dt.strftime("%H:%M")
        if dt.date() == self._location_now().date():
            return hm
        day_abbr = DAY_NAMES.get(lang, DAY_NAMES["en"])[dt.weekday()][:3]
        return f"{day_abbr} {hm}"

    def _alert_timerange_text(self, start_ts: Any, end_ts: Any,
                              lang: str) -> str:
        """Riga temporale per un'allerta API (start/end unix)."""
        it = (lang == "it")
        now_ts = time.time()
        future_start = bool(start_ts) and start_ts > now_ts + 600
        s = self._fmt_alert_time(start_ts, lang) if start_ts else ""
        e = self._fmt_alert_time(end_ts, lang) if end_ts else ""
        if future_start and e:
            return f"{s} – {e}"
        if e:
            return f"fino a {e}" if it else f"until {e}"
        if future_start:
            return f"dalle {s}" if it else f"from {s}"
        return ""

    def _synthetic_horizon(self, hour_off: int, lang: str) -> str:
        """Suffisso orizzonte temporale per allerte sintetiche."""
        it = (lang == "it")
        if hour_off <= 0:
            return "in corso" if it else "ongoing"
        if hour_off == 1:
            return "entro 1h" if it else "within 1h"
        return f"entro {hour_off}h" if it else f"within {hour_off}h"

    def _synthetic_body(self, lang: str) -> str:
        """Testo esteso per allerte sintetiche (derivate dalle condizioni meteo
        previste: OpenWeather non fornisce un bollettino ufficiale)."""
        if lang == "it":
            return ("Allerta generata automaticamente dalle condizioni meteo "
                    "previste. OpenWeather non fornisce un bollettino esteso "
                    "per questo tipo di evento.")
        return ("Alert generated automatically from the forecast conditions. "
                "OpenWeather provides no extended bulletin for this event type.")

    def _draw_alerts_report(self) -> None:
        """Disegna la pagina report allerte (elenco o dettaglio) come un unico
        pannello Texture, cachato e ricostruito solo quando cambia il contenuto
        (lista/vista/indice). Il pannello è pieno-schermo come WEEKLY.
        """
        alerts = self._active_alerts_list
        if not alerts:
            self._draw_alerts_empty()
            return
        if self._alerts_detail_idx >= len(alerts):
            self._alerts_detail_idx = 0
        view = self._alerts_view if self._alerts_view in ("list", "detail") else "list"

        key = ("alerts", view, self._alerts_detail_idx,
               tuple(a.get("key") for a in alerts))
        cached = self._cache_alerts_panel
        if cached is None or cached[0] != hash(key):
            if view == "detail":
                panel, panel_pos, row_rects = self._render_alerts_detail_panel(
                    alerts, self._alerts_detail_idx)
            else:
                panel, panel_pos, row_rects = self._render_alerts_list_panel(alerts)
            tex = Texture.from_surface(self.rb.renderer, panel)
            try:
                tex.blend_mode = 1
            except AttributeError:
                pass
            self._cache_alerts_panel = (hash(key), tex, panel_pos, panel.get_size())
            self._alerts_row_rects = row_rects

        _, tex, panel_pos, size = self._cache_alerts_panel
        rect = pygame.Rect(panel_pos[0], panel_pos[1], size[0], size[1])
        tex.draw(dstrect=rect)

    def _render_alerts_list_panel(
            self, alerts: list[dict]
    ) -> tuple[pygame.Surface, tuple[int, int], list]:
        """Costruisce il pannello ELENCO. Ritorna (surface, panel_pos, row_rects)
        dove row_rects è [(pygame.Rect in coord SCHERMO, alert_index)]."""
        cfg = self.cfg
        lang = cfg.language
        cx, cy = cfg.center_x, cfg.center_y
        pw, ph = cfg.alerts_panel_width, cfg.alerts_panel_height
        panel = pygame.Surface((pw, ph))
        panel.fill(parse_color(cfg.background_color, (0, 0, 0)))

        title_color = parse_color(cfg.alerts_title_color)
        meta_color = parse_color(cfg.alerts_meta_color)
        arrow_color = parse_color(cfg.alerts_arrow_color)
        sep_color = parse_color(cfg.alerts_separator_color)

        n = len(alerts)
        htxt = (f"Allerte attive ({n})" if lang == "it"
                else f"Active alerts ({n})")
        hs = self.font_alerts_header.render(
            htxt, True, parse_color(cfg.alerts_header_color))
        panel.blit(hs, hs.get_rect(midtop=(pw // 2, 12)))
        top = 12 + hs.get_height() + 14
        pygame.draw.line(panel, sep_color, (16, top - 8), (pw - 8 - 16, top - 8), 1)

        row_h = cfg.alerts_row_height
        avail = ph - top - 10
        max_rows = max(1, avail // row_h)
        if n <= max_rows:
            show, overflow = n, 0
        else:
            show = max(1, max_rows - 1)   # sempre almeno una riga tappabile
            overflow = n - show

        panel_pos = (cx - pw // 2, cy - ph // 2)
        row_rects: list[tuple[pygame.Rect, int]] = []
        text_left = 32
        text_right = pw - 34   # spazio per il chevron

        for i in range(show):
            a = alerts[i]
            ry = top + i * row_h
            barcol = self._severity_color(a.get("severity", 1))
            pygame.draw.rect(panel, barcol,
                             pygame.Rect(14, ry + 10, 6, row_h - 22),
                             border_radius=3)
            title = a.get("title") or a.get("text") or ""
            tsurf = self.font_alerts_title.render(
                self._ellipsize(self.font_alerts_title, title,
                                text_right - text_left), True, title_color)
            panel.blit(tsurf, (text_left, ry + 9))
            sub = a.get("subtitle") or ""
            if sub:
                ssurf = self.font_alerts_meta.render(
                    self._ellipsize(self.font_alerts_meta, sub,
                                    text_right - text_left), True, meta_color)
                panel.blit(ssurf, (text_left, ry + 11 + tsurf.get_height()))
            ch = self.font_alerts_title.render("›", True, arrow_color)
            panel.blit(ch, ch.get_rect(midright=(pw - 16, ry + row_h // 2)))
            if i < show - 1 or overflow > 0:
                sy = ry + row_h
                pygame.draw.line(panel, sep_color, (16, sy), (pw - 16, sy), 1)
            row_rects.append(
                (pygame.Rect(panel_pos[0], panel_pos[1] + ry, pw, row_h), i))

        if overflow > 0:
            oy = top + show * row_h
            otxt = (f"+{overflow} altre" if lang == "it"
                    else f"+{overflow} more")
            osurf = self.font_alerts_meta.render(otxt, True, meta_color)
            panel.blit(osurf, osurf.get_rect(midtop=(pw // 2, oy + 10)))

        return safe_convert(panel), panel_pos, row_rects

    def _render_alerts_detail_panel(
            self, alerts: list[dict], idx: int
    ) -> tuple[pygame.Surface, tuple[int, int], list]:
        """Costruisce il pannello DETTAGLIO di una singola allerta."""
        cfg = self.cfg
        lang = cfg.language
        it = (lang == "it")
        cx, cy = cfg.center_x, cfg.center_y
        pw, ph = cfg.alerts_panel_width, cfg.alerts_panel_height
        a = alerts[idx]
        panel = pygame.Surface((pw, ph))
        panel.fill(parse_color(cfg.background_color, (0, 0, 0)))

        title_color = parse_color(cfg.alerts_title_color)
        meta_color = parse_color(cfg.alerts_meta_color)
        body_color = parse_color(cfg.alerts_body_color)
        sep_color = parse_color(cfg.alerts_separator_color)

        inner_left, inner_right = 24, pw - 24
        max_w = inner_right - inner_left
        y = 14

        n = len(alerts)
        if n > 1:
            csurf = self.font_alerts_meta.render(
                f"{idx + 1}/{n}", True, meta_color)
            panel.blit(csurf, csurf.get_rect(topright=(inner_right, y)))

        title = a.get("title") or a.get("text") or ""
        title_lines = self._wrap_text(
            self.font_alerts_title, title, max_w - 16)[:2]
        line_h_t = self.font_alerts_title.get_linesize()
        bar_h = max(len(title_lines) * line_h_t, self.font_alerts_title.get_height())
        pygame.draw.rect(panel, self._severity_color(a.get("severity", 1)),
                         pygame.Rect(inner_left, y, 6, bar_h), border_radius=3)
        tx = inner_left + 16
        for ln in title_lines:
            panel.blit(self.font_alerts_title.render(ln, True, title_color),
                       (tx, y))
            y += line_h_t
        y += 4

        sub = a.get("subtitle") or ""
        if sub:
            for ln in self._wrap_text(self.font_alerts_meta, sub, max_w):
                panel.blit(self.font_alerts_meta.render(ln, True, meta_color),
                           (inner_left, y))
                y += self.font_alerts_meta.get_linesize()
        y += 8
        pygame.draw.line(panel, sep_color, (inner_left, y), (inner_right, y), 1)
        y += 12

        body = (a.get("body") or "").strip() or (a.get("text") or "")
        body_font = self.font_alerts_body
        # max(1, …): evita divisione per zero se alerts_line_spacing è negativo
        line_h = max(1, body_font.get_linesize() + cfg.alerts_line_spacing)
        bottom_limit = ph - 34   # spazio per l'hint in basso
        body_lines = self._wrap_text(body_font, body, max_w)
        max_lines = max(0, (bottom_limit - y) // line_h)
        truncated = len(body_lines) > max_lines
        if truncated:
            body_lines = body_lines[:max_lines]
        for i, ln in enumerate(body_lines):
            if truncated and i == len(body_lines) - 1:
                ln = self._ellipsize(body_font, ln + "…", max_w)
            panel.blit(body_font.render(ln, True, body_color), (inner_left, y))
            y += line_h

        if n > 1:
            hint = ("tocca: elenco  ‹ › cambia" if it
                    else "tap: list  ‹ › change")
        else:
            hint = "tocca: elenco" if it else "tap: list"
        hsurf = self.font_alerts_meta.render(
            self._ellipsize(self.font_alerts_meta, hint, max_w), True, meta_color)
        panel.blit(hsurf, hsurf.get_rect(midbottom=(pw // 2, ph - 10)))

        return safe_convert(panel), (cx - pw // 2, cy - ph // 2), []

    def _draw_alerts_empty(self) -> None:
        """Stato vuoto (raro: le allerte si sono azzerate mentre la pagina è
        aperta). Ripiega su un messaggio centrato, come WEEKLY."""
        cfg = self.cfg
        msg = "Nessuna allerta attiva" if cfg.language == "it" \
            else "No active alerts"
        surf = self.font_alerts_title.render(
            msg, True, parse_color(cfg.alerts_title_color))
        w, h = surf.get_size()
        bg_color = parse_color(cfg.background_color, (0, 0, 0))
        self.rb.renderer.draw_color = (*bg_color, 255)
        bg_rect = pygame.Rect(0, 0, w + 40, h + 20)
        bg_rect.center = (cfg.center_x, cfg.center_y)
        self.rb.renderer.fill_rect(bg_rect)
        tex = Texture.from_surface(self.rb.renderer, surf)
        try:
            tex.blend_mode = 1
        except AttributeError:
            pass
        text_rect = pygame.Rect(0, 0, w, h)
        text_rect.center = (cfg.center_x, cfg.center_y)
        tex.draw(dstrect=text_rect)

    # -----------------------------------------------------------------------
    # Main loop
    # -----------------------------------------------------------------------

    def _compute_render_signature(self) -> tuple:
        """Tuple identificante lo stato visibile.

        Se non cambia tra due iterazioni → niente redraw → niente flip().
        Risparmiamo CPU e (importante) richieste verso l'X server.
        """
        # Durante una transizione, ogni frame deve essere ridisegnato per
        # animare il movimento → signature unica per ms.
        if self._transition_active:
            return ("transition", pygame.time.get_ticks())

        now = datetime.now()
        # now_loc = ora della posizione: usata per i bucket minuto/ora/giorno di
        # HANDS/DIGITAL (che mostrano l'ora del luogo). `now` (sistema) resta per
        # l'età dati (freshness), che va confrontata con last_fetch_time (sistema).
        now_loc = self._location_now()
        # Freshness cambia ogni minuto. Riutilizza `now` (no double datetime.now).
        if self.last_fetch_time is not None:
            age_min = int((now - self.last_fetch_time).total_seconds()) // 60
        else:
            age_min = -1
        alert_key = self._active_alert["key"] if self._active_alert else None
        # Frame animazione: cambia ogni 1/animation_fps secondi
        anim_idx = self._anim_frame_idx if self.cfg.animate_icons else 0
        # Moon phase cached al minuto (cambia ogni ~7 ore, ricalcolarlo a
        # 60fps è spreco). Usa now.minute come key di invalidazione.
        moon_key = self._cached_moon_key
        if self._cached_moon_minute != now.minute and self.cfg.show_moon:
            self._cached_moon_key = round(
                self._get_moon_phase()[0], 2
            )
            self._cached_moon_minute = now.minute
            moon_key = self._cached_moon_key

        if self.mode == MODE_OFF:
            # Signature costante: niente redraw mai. CPU minima.
            return (MODE_OFF,)
        if self.mode == MODE_HANDS:
            # sec_bucket calcolato solo qui (modes che usano la lancetta).
            if self.cfg.smooth_seconds:
                sub_bucket = int(now.microsecond * self.cfg.fps / 1_000_000)
                sec_bucket = (now.second, sub_bucket)
            else:
                sec_bucket = (now.second,)
            return (MODE_HANDS, sec_bucket, now_loc.minute, now_loc.hour,
                    self._static_bg_version, age_min, alert_key, anim_idx, moon_key)
        if self.mode == MODE_DIGITAL:
            if self.cfg.smooth_seconds:
                sub_bucket = int(now.microsecond * self.cfg.fps / 1_000_000)
                sec_bucket = (now.second, sub_bucket)
            else:
                sec_bucket = (now.second,)
            return (MODE_DIGITAL, sec_bucket, now_loc.minute, now_loc.hour,
                    now_loc.day, age_min, alert_key, anim_idx)
        if self.mode == MODE_DETAIL:
            # Icone visibili anche in DETAIL → include anim_idx
            # detail_page nella signature → swipe verticale forza redraw
            return (MODE_DETAIL, self.detail_hours_ahead, self.detail_page,
                    self._static_bg_version, age_min, alert_key, anim_idx)
        if self.mode == MODE_WEEKLY:
            # WEEKLY: icone animate → include anim_idx per redraw frame
            return (MODE_WEEKLY, self._static_bg_version, age_min,
                    alert_key, anim_idx)
        if self.mode == MODE_CURRENT:
            # CURRENT: testi attuali + freshness; rebuild su nuovi dati o
            # quando freshness avanza di un minuto. Icona luna è statica
            # (cache su phase_key da moon, non da minuto).
            return (MODE_CURRENT, self._static_bg_version, age_min, alert_key,
                    anim_idx, moon_key)
        if self.mode == MODE_CHART:
            # CHART: include anim_idx per mantenere animate le icone meteo
            # del quadrante (CHART è nel carosello, le icone restano visibili).
            # Senza anim_idx la signature non cambierebbe → le icone
            # resterebbero ferme al frame in cui sei entrato in CHART.
            return (MODE_CHART, self._static_bg_version, age_min, alert_key,
                    anim_idx)
        if self.mode == MODE_ALERTS:
            # Report statico: redraw solo se cambia vista/indice/insieme allerte.
            return (MODE_ALERTS, self._alerts_view, self._alerts_detail_idx,
                    tuple(a.get("key") for a in self._active_alerts_list))
        return ()

    # -----------------------------------------------------------------------
    # Rendering pipeline (Fase 2 GPU + Fase 4 transitions)
    # -----------------------------------------------------------------------

    def _render_mode_pipeline(self, mode: int,
                                skip_animated: bool = False) -> None:
        """Renderizza tutti gli elementi del mode specificato sul renderer
        corrente. NON chiama clear() né present() — quelle sono responsabilità
        del chiamante. Permette di renderizzare la stessa pipeline con
        viewport diversi (per le transizioni slide).

        skip_animated: se True, NON disegna le icone meteo animate né le
        pillole. Usato dal fade per metterle live come overlay sopra il
        fade (evita il "congelamento" delle icone durante i 250ms del fade).
        """
        if mode == MODE_OFF:
            return  # OFF = nero, nient'altro

        if mode == MODE_WEEKLY:
            # WEEKLY: solo il pannello settimanale (no static_bg/icone)
            self._draw_weekly()
            return

        if mode == MODE_ALERTS:
            # ALERTS: pagina report a schermo pieno (no static_bg/icone/banner).
            self._draw_alerts_report()
            return

        # HANDS / DETAIL / DIGITAL / CHART: stessa base (quadrante con icone)
        if self._static_bg_texture is not None:
            self._static_bg_texture.draw()
        if not skip_animated:
            self._draw_icons()
            self._draw_pills_overlay()

        if mode == MODE_HANDS:
            # HANDS minimal: solo temperatura + mini icona luna (no testi).
            # Per dati completi (alba/tramonto, vento, UV, percepita)
            # swipe orizzontale → MODE_CURRENT.
            # NOTA: _draw_hands() viene chiamato per ULTIMO, dopo
            # freshness e alert, per garantire che le lancette siano
            # sempre sopra tutto.
            self._draw_center_temp()
            self._draw_moon_mini()
        elif mode == MODE_DETAIL:
            self._draw_detail()
        elif mode == MODE_DIGITAL:
            self._draw_digital()
        elif mode == MODE_CHART:
            self._draw_chart()
        elif mode == MODE_CURRENT:
            self._draw_current()

        # Freshness: visibile in tutte e 3 le viste del carosello
        # (HANDS, CURRENT, CHART) + DETAIL/DIGITAL. Solo WEEKLY/OFF
        # non la mostrano (WEEKLY ha un early return sopra).
        self._draw_freshness()
        # Allerta pillola: visibile SOLO in HANDS/DIGITAL dove c'è spazio
        # centrale libero. CURRENT/CHART/DETAIL hanno pannelli che occupano
        # quell'area: in quelle viste la nascondiamo per non sovrapporre.
        # L'allerta resta attiva nella lista; ricompare appena si torna in
        # HANDS/DIGITAL.
        if mode in (MODE_HANDS, MODE_DIGITAL):
            self._draw_alert()

        # Lancette HANDS: disegnate per ULTIME → sopra freshness/alert
        if mode == MODE_HANDS:
            self._draw_hands()

    def _compose_and_flip(self) -> None:
        """Render one frame — pipeline tutto GPU (Fase 2) + transizioni GPU
        (Fase 4 via viewport).

        Costo atteso: ~2-3ms per frame normale, ~4-5ms durante transizioni.
        """
        rb = self.rb
        rb.clear((0, 0, 0))

        if self._transition_active:
            self._render_transition_frame_v2()
        else:
            self._render_mode_pipeline(self.mode)

        rb.present()
        self._frames_rendered += 1

    # Style transizione automatico in base alla coppia (from, to).
    # Costanti interne, non più configurabili via settings.json.
    TRANSITION_SLIDE_DOWN = "slide_down"     # to entra dall'alto (per HANDS/DIG→WEEKLY)
    TRANSITION_SLIDE_UP = "slide_up"         # to entra dal basso (per WEEKLY→HANDS)
    TRANSITION_SLIDE_LEFT = "slide_left"     # to entra da destra (per DETAIL page+)
    TRANSITION_SLIDE_RIGHT = "slide_right"   # to entra da sinistra (per DETAIL page-)
    TRANSITION_FADE = "fade"                 # crossfade (per HANDS↔DIGITAL, →DETAIL, etc)

    # Nome leggibile dei modes, per logging. Costante di classe per evitare
    # di ricreare il dict ogni frame del main loop (60 volte/sec a fps=60).
    _MODE_NAMES = {
        0: "HANDS",     # MODE_HANDS
        1: "DETAIL",    # MODE_DETAIL
        2: "DIGITAL",   # MODE_DIGITAL
        3: "OFF",       # MODE_OFF
        4: "WEEKLY",    # MODE_WEEKLY
        5: "CHART",     # MODE_CHART
        6: "CURRENT",   # MODE_CURRENT
        7: "ALERTS",    # MODE_ALERTS
    }

    def _pick_transition_style(self, from_mode: int, to_mode: int,
                                 gesture_direction: Optional[str] = None) -> str:
        """Decide il tipo di transizione in base ai mode E alla direzione del
        gesto.

        Regola fondamentale: lo slide segue il dito (carosello).
          - swipe down (dito giù)  → slide_down (nuova vista entra dall'alto)
          - swipe up (dito su)     → slide_up   (nuova vista entra dal basso)
          - swipe right (dito dx)  → slide_right (nuova entra da sinistra)
          - swipe left (dito sx)   → slide_left  (nuova entra da destra)

        Senza direzione (long-press, tap, timeout): sempre fade.
        """
        if gesture_direction == "down":
            return self.TRANSITION_SLIDE_DOWN
        if gesture_direction == "up":
            return self.TRANSITION_SLIDE_UP
        if gesture_direction == "right":
            return self.TRANSITION_SLIDE_RIGHT
        if gesture_direction == "left":
            return self.TRANSITION_SLIDE_LEFT
        # Senza direzione esplicita: fade
        return self.TRANSITION_FADE

    def _render_to_buffer(self, buffer: "Texture", mode: int,
                           detail_offset_override: Optional[int] = None,
                           skip_animated: bool = False) -> None:
        """Renderizza la pipeline di `mode` su una Texture target buffer.

        Setta il target del renderer al buffer, fa clear + render_mode_pipeline,
        poi resetta il target al main framebuffer.

        detail_offset_override: se specificato e mode=DETAIL, sovrascrive
        detail_hours_ahead durante il render (per fade DETAIL→DETAIL ora diversa).

        skip_animated: se True, NON include icone meteo + pillole nel buffer.
        Usato dal fade per renderizzarle come overlay live sopra il blit del
        fade (evita "congelamento" delle icone animate per 250ms).
        """
        renderer = self.rb.renderer
        # Cambia target a buffer
        renderer.target = buffer
        renderer.draw_color = (0, 0, 0, 255)
        renderer.clear()
        # Salva stato corrente
        saved_mode = self.mode
        saved_offset = self.detail_hours_ahead
        self.mode = mode
        if detail_offset_override is not None:
            self.detail_hours_ahead = detail_offset_override
        try:
            self._render_mode_pipeline(mode, skip_animated=skip_animated)
        finally:
            self.mode = saved_mode
            self.detail_hours_ahead = saved_offset
        # Ripristina target al main framebuffer
        renderer.target = None

    # Lookup table costante per evitare branch+confronti string ad ogni frame.
    # Le 6 funzioni di easing sono inlined; viene scelta una sola volta
    # in __init__ via _resolve_easing(), poi usata via self._ease_fn(t).
    _EASE_FUNCTIONS = {
        "linear":       lambda t: t,
        "out_cubic":    lambda t: 1.0 - (1.0 - t) ** 3,
        "in_out_cubic": lambda t: 4.0 * t ** 3 if t < 0.5 else 1.0 - ((-2.0 * t + 2.0) ** 3) / 2.0,
        "out_quint":    lambda t: 1.0 - (1.0 - t) ** 5,
        "out_expo":     lambda t: 1.0 if t >= 1.0 else 1.0 - 2.0 ** (-10.0 * t),
        "out_back":     lambda t: 1.0 + 2.70158 * (t - 1.0) ** 3 + 1.70158 * (t - 1.0) ** 2,
    }

    def _resolve_easing(self) -> "callable":
        """Risolve cfg.transition_easing in una funzione concreta una volta sola.

        Chiamato in __init__ e in hot-reload se cambia transition_easing.
        Evita lookup string ad ogni frame della transizione.
        """
        curve = self.cfg.transition_easing
        return self._EASE_FUNCTIONS.get(curve, self._EASE_FUNCTIONS["out_cubic"])

    def _render_transition_frame_v2(self) -> None:
        """Renderizza un frame della transizione (Fase 4 GPU).

        Tecniche:
          - slide_*: viewport scrolling (no buffer Texture, costo ~6-9ms)
          - fade: render entrambi i mode su buffer Texture target,
                  poi blit con alpha modulation (costo ~10-15ms ma fluido)
        """
        now_ms = pygame.time.get_ticks()
        elapsed_ms = now_ms - self._transition_start_ms
        # Durata in base allo stile (fade/slide). Permette di avere fade
        # rapidi (250ms) e slide più lunghi (~450ms) o viceversa.
        duration_ms = self.cfg.resolve_transition_duration(self._transition_style)

        # Transizione finita → render normale + reset blend mode dei buffer
        if elapsed_ms >= duration_ms:
            self._transition_active = False
            # Resetta blend_mode dei buffer Texture (slide oriz usa ADD=2,
            # mentre fade usa BLEND=1; ripristino default BLEND).
            if self.rb._tx_buffer_a is not None:
                try:
                    self.rb._tx_buffer_a.blend_mode = 1
                    self.rb._tx_buffer_b.blend_mode = 1
                except Exception:
                    pass
            self._render_mode_pipeline(self.mode)
            return

        # Easing pre-risolto (lookup string evitato ogni frame)
        t = elapsed_ms / duration_ms
        if t < 0.0:
            t = 0.0
        elif t > 1.0:
            t = 1.0
        t_eased = self._ease_fn(t)

        w = self.cfg.screen_width
        h = self.cfg.screen_height
        style = self._transition_style
        from_mode = self._transition_from_mode
        to_mode = self._transition_to_mode

        if style == self.TRANSITION_SLIDE_DOWN:
            # to entra dall'alto, from esce verso il basso
            # progress 0: from a y=0, to a y=-h (fuori sopra)
            # progress 1: from a y=h (fuori sotto), to a y=0
            # OTTIMIZZATO: usa buffer Texture pre-renderizzati come il fade.
            # Vecchia versione faceva render completo di from + to ad ogni
            # frame (~13ms/frame su Pi Zero W) → stutter visibile soprattutto
            # con WEEKLY (cold cache per icone weekly al primo frame).
            # Nuova versione: render una sola volta i buffer in
            # _begin_transition, poi solo blit (~2-3ms/frame).
            offset = int(t_eased * h)
            buf_from, buf_to = self.rb.get_transition_buffers()
            if not self._transition_slide_buffers_ready:
                self._render_to_buffer(buf_from, from_mode)
                self._render_to_buffer(buf_to, to_mode)
                self._transition_slide_buffers_ready = True
            try:
                buf_from.blend_mode = 1
                buf_to.blend_mode = 1
                buf_from.alpha = 255
                buf_to.alpha = 255
            except Exception:
                pass
            # from: blit shiftato in basso di `offset` pixel
            buf_from.draw(dstrect=pygame.Rect(0, offset, w, h))
            # to: blit shiftato in alto, entra dall'alto
            buf_to.draw(dstrect=pygame.Rect(0, offset - h, w, h))

        elif style == self.TRANSITION_SLIDE_UP:
            # to entra dal basso, from esce verso l'alto (idem ottimizzazione)
            offset = int(t_eased * h)
            buf_from, buf_to = self.rb.get_transition_buffers()
            if not self._transition_slide_buffers_ready:
                self._render_to_buffer(buf_from, from_mode)
                self._render_to_buffer(buf_to, to_mode)
                self._transition_slide_buffers_ready = True
            try:
                buf_from.blend_mode = 1
                buf_to.blend_mode = 1
                buf_from.alpha = 255
                buf_to.alpha = 255
            except Exception:
                pass
            # from: blit shiftato in alto
            buf_from.draw(dstrect=pygame.Rect(0, -offset, w, h))
            # to: blit shiftato in basso, entra dal basso
            buf_to.draw(dstrect=pygame.Rect(0, h - offset, w, h))

        elif style == self.TRANSITION_SLIDE_LEFT:
            # to entra da destra, from esce verso sinistra
            offset = int(t_eased * w)
            from_off = getattr(self, '_transition_from_detail_offset', None)
            to_off = getattr(self, '_transition_to_detail_offset', None)
            from_page = getattr(self, '_transition_from_detail_page', None)
            to_page = getattr(self, '_transition_to_detail_page', None)
            # Caso 1: DETAIL → DETAIL cambio pagina (slide nella stessa
            # "finestra circolare interna" di HANDS↔CHART per uniformità)
            if (from_mode == MODE_DETAIL and to_mode == MODE_DETAIL
                    and from_page is not None and to_page is not None):
                self._render_detail_slide_horizontal(
                    from_page, to_page, t_eased, direction="left"
                )
            # Caso 2: HANDS/DIGITAL ↔ CHART (slide del SOLO centro, limitato
            # al cerchio interno center_slide_width × center_slide_height)
            elif (from_mode in (MODE_HANDS, MODE_DIGITAL, MODE_CHART, MODE_CURRENT)
                  and to_mode in (MODE_HANDS, MODE_DIGITAL, MODE_CHART, MODE_CURRENT)):
                self._render_quadrant_slide_horizontal(
                    from_mode, to_mode, t_eased, direction="left"
                )
            else:
                # Slide pieno (fallback)
                self.rb.renderer.set_viewport(pygame.Rect(-offset, 0, w, h))
                self._render_mode_pipeline_with_mode(from_mode, from_off)
                self.rb.renderer.set_viewport(pygame.Rect(w - offset, 0, w, h))
                self._render_mode_pipeline_with_mode(to_mode, to_off)
                self.rb.renderer.set_viewport(None)

        elif style == self.TRANSITION_SLIDE_RIGHT:
            offset = int(t_eased * w)
            from_off = getattr(self, '_transition_from_detail_offset', None)
            to_off = getattr(self, '_transition_to_detail_offset', None)
            from_page = getattr(self, '_transition_from_detail_page', None)
            to_page = getattr(self, '_transition_to_detail_page', None)
            if (from_mode == MODE_DETAIL and to_mode == MODE_DETAIL
                    and from_page is not None and to_page is not None):
                self._render_detail_slide_horizontal(
                    from_page, to_page, t_eased, direction="right"
                )
            elif (from_mode in (MODE_HANDS, MODE_DIGITAL, MODE_CHART, MODE_CURRENT)
                  and to_mode in (MODE_HANDS, MODE_DIGITAL, MODE_CHART, MODE_CURRENT)):
                self._render_quadrant_slide_horizontal(
                    from_mode, to_mode, t_eased, direction="right"
                )
            else:
                self.rb.renderer.set_viewport(pygame.Rect(offset, 0, w, h))
                self._render_mode_pipeline_with_mode(from_mode, from_off)
                self.rb.renderer.set_viewport(pygame.Rect(offset - w, 0, w, h))
                self._render_mode_pipeline_with_mode(to_mode, to_off)
                self.rb.renderer.set_viewport(None)

        elif style == self.TRANSITION_FADE:
            # Render entrambi i mode su buffer Texture target.
            # Poi blit: from a alpha=255*(1-t), to a alpha=255*t.
            buf_from, buf_to = self.rb.get_transition_buffers()
            # Forza BLEND mode (potrebbe essere stato ADD da una slide precedente)
            try:
                buf_from.blend_mode = 1
                buf_to.blend_mode = 1
            except Exception:
                pass

            # Modes che disegnano le icone meteo del quadrante.
            # WEEKLY è escluso (ha solo il pannello settimanale, no icone).
            # OFF idem.
            modes_with_icons = {MODE_HANDS, MODE_DETAIL, MODE_DIGITAL,
                                  MODE_CHART, MODE_CURRENT}
            # Se ENTRAMBI from e to disegnano le icone meteo, escludile dai
            # buffer fade e renderizzale live come overlay sopra il fade.
            # Questo evita il "congelamento" delle icone durante i 250ms
            # del fade (cf. swipe slide che le mantengono animate via
            # _render_fixed_quadrant_layer chiamato ogni frame).
            icons_overlay = (from_mode in modes_with_icons
                             and to_mode in modes_with_icons)

            # Render at PRIMO frame solo del `to` (il `from` è pre-renderizzato
            # in _begin_transition per non sovraccaricare il primo frame del fade)
            if not self._transition_fade_buffers_ready:
                from_off = getattr(self, '_transition_from_detail_offset', None)
                to_off = getattr(self, '_transition_to_detail_offset', None)
                logging.info("FADE frame1: from_mode=%s to_mode=%s "
                              "from_off=%s to_off=%s prerendered=%s icons_overlay=%s",
                              self._MODE_NAMES.get(from_mode, "?"),
                              self._MODE_NAMES.get(to_mode, "?"),
                              from_off, to_off,
                              getattr(self, '_transition_fade_from_prerendered', False),
                              icons_overlay)
                # Render `from` solo se NON è stato pre-renderizzato
                # (fallback per casi di errore o slide → fade non previsto)
                if not getattr(self, '_transition_fade_from_prerendered', False):
                    self._render_to_buffer(buf_from, from_mode, from_off,
                                             skip_animated=icons_overlay)
                # `to` sempre renderizzato qui (lo stato target è "nuovo")
                self._render_to_buffer(buf_to, to_mode, to_off,
                                         skip_animated=icons_overlay)
                self._transition_fade_buffers_ready = True

            # Blit sul main framebuffer
            # to sotto, full opaque sempre (l'utente finisce qui)
            buf_to.alpha = 255
            buf_to.draw()
            # from sopra, alpha che decresce (svanisce)
            # Clamp [0,255] perché out_back può fare t_eased > 1.0 (overshoot)
            alpha_from = int((1.0 - t_eased) * 255)
            buf_from.alpha = max(0, min(255, alpha_from))
            if buf_from.alpha > 0:
                buf_from.draw()

            # Overlay icone meteo + pillole LIVE sopra il fade.
            # Renderizzate ogni frame → le animazioni delle icone meteo
            # (pioggia, sole pulsante, ecc) continuano fluide durante il
            # fade invece di restare congelate per 250ms.
            if icons_overlay:
                self._draw_icons()
                self._draw_pills_overlay()

    def _render_detail_slide_horizontal(self, from_page: int, to_page: int,
                                          t_eased: float,
                                          direction: str) -> None:
        """Slide DETAIL→DETAIL (cambio pagina) usando lo stesso meccanismo
        di HANDS↔CHART: buffer Texture full screen con sfondo nero +
        BLEND_ADD, blit srcrect/dstrect ristretto a center_slide_width
        × center_slide_height. Così il pannello DETAIL slida nella stessa
        "finestra circolare interna" del chart, senza coprire le icone
        del quadrante.
        """
        cfg = self.cfg
        slide_w = cfg.center_slide_width
        slide_h = cfg.center_slide_height
        slide_x = cfg.center_x - slide_w // 2
        slide_y = cfg.center_y - slide_h // 2

        # Layer fisso full screen ogni frame (icone + page dots).
        # hide_alert=True: il pannello DETAIL coprirebbe la pillola allerta.
        self._render_fixed_quadrant_layer(include_page_dots=True, hide_alert=True)

        # Render dei due buffer (snapshot, una sola volta per la transizione)
        buf_from, buf_to = self.rb.get_transition_buffers()
        if not self._transition_slide_buffers_ready:
            for buf, page in [(buf_from, from_page), (buf_to, to_page)]:
                self.rb.renderer.target = buf
                self.rb.renderer.draw_color = (0, 0, 0, 255)
                self.rb.renderer.clear()
                self._render_detail_panel_only(page)
            self.rb.renderer.target = None
            self._transition_slide_buffers_ready = True

        try:
            buf_from.blend_mode = 2  # SDL_BLENDMODE_ADD
            buf_to.blend_mode = 2
        except Exception:
            pass

        offset = int(t_eased * slide_w)

        if direction == "left":
            if offset < slide_w:
                src_from = pygame.Rect(slide_x + offset, slide_y,
                                          slide_w - offset, slide_h)
                dst_from = pygame.Rect(slide_x, slide_y,
                                          slide_w - offset, slide_h)
                buf_from.draw(srcrect=src_from, dstrect=dst_from)
            if offset > 0:
                src_to = pygame.Rect(slide_x, slide_y, offset, slide_h)
                dst_to = pygame.Rect(slide_x + slide_w - offset, slide_y,
                                        offset, slide_h)
                buf_to.draw(srcrect=src_to, dstrect=dst_to)
        else:  # right
            if offset < slide_w:
                src_from = pygame.Rect(slide_x, slide_y,
                                          slide_w - offset, slide_h)
                dst_from = pygame.Rect(slide_x + offset, slide_y,
                                          slide_w - offset, slide_h)
                buf_from.draw(srcrect=src_from, dstrect=dst_from)
            if offset > 0:
                src_to = pygame.Rect(slide_x + slide_w - offset, slide_y,
                                        offset, slide_h)
                dst_to = pygame.Rect(slide_x, slide_y, offset, slide_h)
                buf_to.draw(srcrect=src_to, dstrect=dst_to)

    def _render_quadrant_slide_horizontal(self, from_mode: int, to_mode: int,
                                            t_eased: float,
                                            direction: str) -> None:
        """Slide oriz del SOLO centro tra HANDS/DIGITAL/CHART.

        Le 12 icone + pillole restano FERME (full screen).
        L'area centrale slida limitata a center_slide_width × center_slide_height.

        Tecnica:
          - render snapshot dei due "centri" su buffer Texture full screen,
            sfondo NERO opaco
          - blend_mode = SDL_BLENDMODE_ADD (=2): aree nere del buffer
            non contribuiscono (0+x = x), aree colorate si aggiungono
            sopra il main framebuffer
          - blit con srcrect/dstrect ristretti all'area centrale e offsettati

        Vantaggio ADD: il rettangolo del buffer non "copre" le icone meteo
        che sono sullo sfondo (sotto), perché il nero non aggiunge nulla.
        Lo sfondo del main framebuffer nel centro è già nero (background_color),
        quindi i pixel del centro appaiono col loro colore corretto.

        direction: "left" = to entra da destra
                   "right" = to entra da sinistra
        """
        cfg = self.cfg
        slide_w = cfg.center_slide_width
        slide_h = cfg.center_slide_height
        slide_x = cfg.center_x - slide_w // 2
        slide_y = cfg.center_y - slide_h // 2

        # Freshness visibile in tutte e 3 le viste del carosello.
        # Allerta: visibile solo se from E to sono HANDS/DIGITAL (le altre
        # viste hanno pannelli che la coprirebbero).
        alert_compatible_modes = {MODE_HANDS, MODE_DIGITAL}
        hide_alert = (from_mode not in alert_compatible_modes
                       or to_mode not in alert_compatible_modes)
        self._render_fixed_quadrant_layer(include_page_dots=False,
                                            hide_alert=hide_alert)

        # Render dei due buffer (snapshot, una sola volta per la transizione)
        buf_from, buf_to = self.rb.get_transition_buffers()
        if not self._transition_slide_buffers_ready:
            for buf, mode in [(buf_from, from_mode), (buf_to, to_mode)]:
                self.rb.renderer.target = buf
                self.rb.renderer.draw_color = (0, 0, 0, 255)
                self.rb.renderer.clear()
                saved = self.mode
                self.mode = mode
                try:
                    self._render_center_only(mode)
                finally:
                    self.mode = saved
            self.rb.renderer.target = None
            self._transition_slide_buffers_ready = True

        # SDL_BLENDMODE_ADD = 2. Aree nere del buffer non sovrascrivono icone.
        try:
            buf_from.blend_mode = 2
            buf_to.blend_mode = 2
        except Exception:
            pass  # se non supportato, sarà visibile il rettangolo (fallback)

        # Calcolo offset orizzontale
        offset = int(t_eased * slide_w)

        if direction == "left":
            # FROM esce a sinistra
            if offset < slide_w:
                src_from = pygame.Rect(slide_x + offset, slide_y,
                                          slide_w - offset, slide_h)
                dst_from = pygame.Rect(slide_x, slide_y,
                                          slide_w - offset, slide_h)
                buf_from.draw(srcrect=src_from, dstrect=dst_from)
            if offset > 0:
                src_to = pygame.Rect(slide_x, slide_y, offset, slide_h)
                dst_to = pygame.Rect(slide_x + slide_w - offset, slide_y,
                                        offset, slide_h)
                buf_to.draw(srcrect=src_to, dstrect=dst_to)
        else:  # direction == "right"
            if offset < slide_w:
                src_from = pygame.Rect(slide_x, slide_y,
                                          slide_w - offset, slide_h)
                dst_from = pygame.Rect(slide_x + offset, slide_y,
                                          slide_w - offset, slide_h)
                buf_from.draw(srcrect=src_from, dstrect=dst_from)
            if offset > 0:
                src_to = pygame.Rect(slide_x + slide_w - offset, slide_y,
                                        offset, slide_h)
                dst_to = pygame.Rect(slide_x, slide_y, offset, slide_h)
                buf_to.draw(srcrect=src_to, dstrect=dst_to)

    def _render_mode_pipeline_with_mode(self, mode: int,
                                         detail_offset_override: Optional[int] = None) -> None:
        """Helper: rendera con self.mode temporaneamente uguale a mode.

        Se mode=DETAIL e detail_offset_override è specificato, sovrascrive
        anche detail_hours_ahead durante il render (per transizioni DETAIL→DETAIL).
        """
        saved_mode = self.mode
        saved_offset = self.detail_hours_ahead
        self.mode = mode
        if detail_offset_override is not None:
            self.detail_hours_ahead = detail_offset_override
        try:
            self._render_mode_pipeline(mode)
        finally:
            self.mode = saved_mode
            self.detail_hours_ahead = saved_offset

    def _render_detail_fixed_layer(self) -> None:
        """Wrapper retrocompatibile: layer fisso DETAIL (con page dots)."""
        self._render_fixed_quadrant_layer(include_page_dots=True)

    def _render_fixed_quadrant_layer(self, include_page_dots: bool = False,
                                       hide_freshness: bool = False,
                                       hide_alert: bool = False) -> None:
        """Render del layer "fisso" del quadrante (static_bg + icons + pills +
        page_dots + freshness + alert), usato come backdrop ETUSO per le
        transizioni slide oriz.

        Usato durante le transizioni slide oriz tra mode del quadrante,
        in cui SOLO il centro deve slidare.

        include_page_dots: True per DETAIL (mostra i dots di pagina)
        hide_freshness: True quando from o to mode = CHART (no "aggiornato Xm fa")
        hide_alert: True quando from o to mode usa un pannello centrale che
                    sovrapporrebbe la pillola alert (CURRENT/CHART/WEEKLY/DETAIL).
        """
        if self._static_bg_texture is not None:
            self._static_bg_texture.draw()
        self._draw_icons()
        self._draw_pills_overlay()
        if include_page_dots:
            self._draw_page_dots()
        if not hide_freshness:
            self._draw_freshness()
        if not hide_alert:
            self._draw_alert()

    def _render_center_only(self, mode: int) -> None:
        """Renderizza SOLO gli elementi centrali del mode specificato,
        bypassando static_bg/icone/pillole/freshness.

        Usato per slide oriz tra HANDS↔CHART (e DIGITAL→CHART):
        far slidare solo l'elemento centrale (lancette / orario / grafico).
        """
        if mode == MODE_HANDS:
            # HANDS minimal: solo temp + mini-luna + lancette
            self._draw_center_temp()
            self._draw_moon_mini()
            self._draw_hands()
        elif mode == MODE_DIGITAL:
            self._draw_digital()
        elif mode == MODE_CHART:
            self._draw_chart()
        elif mode == MODE_CURRENT:
            self._draw_current()

    def _render_detail_panel_only(self, page: int) -> None:
        """Renderizza SOLO il pannello dettaglio per la pagina indicata,
        bypassando lo static_bg/icone/pillole/freshness/dots.

        Usato durante le transizioni DETAIL page→page per far slidare solo
        il pannello centrale. Niente cache: re-render diretto da Surface.
        """
        if not self.weather_data:
            return
        hourly = self.weather_data.get("hourly") or []
        if self.detail_hours_ahead >= len(hourly):
            return
        offset = self.detail_hours_ahead
        if page == 0:
            panel, panel_pos = self._render_detail_page_base(hourly, offset)
        elif page == 1:
            panel, panel_pos = self._render_detail_page_atmospheric(hourly, offset)
        else:
            return
        tex = Texture.from_surface(self.rb.renderer, panel)
        try:
            tex.blend_mode = 1
        except AttributeError:
            pass
        size = panel.get_size()
        rect = pygame.Rect(panel_pos[0], panel_pos[1], size[0], size[1])
        tex.draw(dstrect=rect)


    def run(self) -> int:
        try:
            # Conta crash consecutivi: se troppi di fila, termina e lascia
            # systemd/getty riavviare il processo da zero (cleanup completo).
            consecutive_errors = 0
            while self._running:
                try:
                    now_mono = time.monotonic()

                    # Drain weather queue
                    new_data = None
                    while True:
                        try:
                            new_data = self.weather_queue.get_nowait()
                        except Empty:
                            break
                    if new_data is not None:
                        self.weather_data = new_data
                        self.last_fetch_time = datetime.now()
                        logging.info("Weather updated at %s",
                                     self.last_fetch_time.isoformat(timespec="seconds"))
                        self._static_bg_dirty = True
                        # Invalida esplicitamente tutte le cache data-dependent.
                        self._cache_sun_times = None
                        self._cache_wind_hands = None
                        self._cache_temp_wind_blob = None
                        self._cache_chart = None
                        self._cache_detail_panel = None
                        self._cache_weekly_panel = None
                        self._cache_current_panel = None
                        if hasattr(self, '_cache_pills_overlay'):
                            self._cache_pills_overlay = None
                        # _check_alerts azzera _cache_alerts_panel internamente
                        self._check_alerts()
                        self._force_redraw = True

                    # Process signal flags (toggle OFF mode)
                    if self._signal_toggle_off:
                        self._signal_toggle_off = False
                        if self.mode == MODE_OFF:
                            logging.info("SIGUSR1: OFF → HANDS")
                            self._enter_hands()
                        else:
                            logging.info("SIGUSR1: mode=%d → OFF", self.mode)
                            self._enter_off()
                        self._force_redraw = True
                    if self._signal_force_on:
                        self._signal_force_on = False
                        if self.mode == MODE_OFF:
                            logging.info("SIGUSR2: OFF → HANDS")
                            self._enter_hands()
                            self._force_redraw = True

                    # Hot reload check (throttle: max una volta ogni 2s)
                    if (self.cfg.settings_hot_reload
                            and now_mono - self._settings_last_check >= 2.0):
                        self._settings_last_check = now_mono
                        self._check_settings_reload()

                    # Mode timeout check
                    if (self._mode_deadline is not None
                            and now_mono >= self._mode_deadline):
                        mode_name = self._MODE_NAMES.get(self.mode, "?")
                        logging.info("Mode timeout (%s) → HANDS", mode_name)
                        self._enter_hands()
                        self._force_redraw = True

                    # Long-press check (fires async from event loop)
                    if (self._press_xy is not None and not self._long_press_fired
                            and pygame.time.get_ticks() - self._press_time_ms
                                >= self.cfg.long_press_ms):
                        self._handle_long_press(self._press_xy)
                        self._force_redraw = True

                    # Events
                    for event in pygame.event.get():
                        self._handle_event(event)

                    # Animation frame counter: avanza ogni 1/animation_fps secondi
                    # period è cached (vedi __init__: _anim_period). Ricalcolarlo
                    # ogni frame non costava molto, ma siamo nel hot path.
                    if self.cfg.animate_icons and self._anim_period > 0:
                        if now_mono - self._anim_last_step_mono >= self._anim_period:
                            # Avanza di N step se siamo in ritardo (no drift)
                            steps = int((now_mono - self._anim_last_step_mono) / self._anim_period)
                            self._anim_frame_idx = (self._anim_frame_idx + steps) % self.cfg.animation_n_frames
                            self._anim_last_step_mono += steps * self._anim_period

                    # Rebuild static background if needed
                    if self._static_bg_dirty:
                        self._build_static_bg()
                        self._static_bg_dirty = False
                        # _build_static_bg increments _static_bg_version internally
                        self._force_redraw = True

                    # Aggiorna rotazione alert prima del signature: se è ora
                    # di mostrare un'altra allerta, _active_alert cambia → la
                    # signature cambia → forza redraw del banner col nuovo testo.
                    self._update_alert_rotation()

                    # Decide if we need to flip this frame
                    current_sig = self._compute_render_signature()
                    if self._force_redraw or current_sig != self._last_render_signature:
                        self._compose_and_flip()
                        self._last_render_signature = current_sig
                        self._force_redraw = False
                    else:
                        self._frames_skipped += 1

                    # Periodic performance log
                    if (self.cfg.perf_log_interval_s > 0
                            and now_mono - self._last_perf_log >= self.cfg.perf_log_interval_s):
                        total = self._frames_rendered + self._frames_skipped
                        pct = 100.0 * self._frames_rendered / max(1, total)
                        logging.info("Perf %ds: rendered=%d skipped=%d (%.1f%% render) "
                                     "mode=%d pill_cache=%d",
                                     self.cfg.perf_log_interval_s,
                                     self._frames_rendered, self._frames_skipped,
                                     pct, self.mode, len(self._cache_pill_bg))
                        self._frames_rendered = 0
                        self._frames_skipped = 0
                        self._last_perf_log = now_mono

                    self.clock.tick(self.cfg.fps)
                    consecutive_errors = 0   # reset su iterazione riuscita

                except Exception:  # noqa: BLE001
                    # Eccezione in singola iterazione: logga TUTTO il traceback
                    # con stack frames, poi continua. Evita di terminare il
                    # processo per un singolo errore transitorio (es. dati API
                    # parziali, edge case di indice fuori range).
                    consecutive_errors += 1
                    logging.exception(
                        "Iterazione main loop fallita (%d consecutiv%s): "
                        "mode=%d, weather_data=%s, detail_offset=%d",
                        consecutive_errors,
                        "a" if consecutive_errors == 1 else "e",
                        self.mode,
                        "loaded" if self.weather_data else "None",
                        self.detail_hours_ahead,
                    )
                    if consecutive_errors >= 20:
                        # Troppi errori di fila: meglio uscire e lasciare
                        # systemd/getty riavviare il processo da zero
                        logging.error("Troppi errori consecutivi (%d), termino.",
                                       consecutive_errors)
                        return 2
                    # Piccola pausa per evitare busy-loop di errori
                    time.sleep(0.1)

            return 0
        except Exception:  # noqa: BLE001
            # Eccezione FUORI dal main loop (es. inizializzazione tardiva,
            # signal handlers, eccezioni nel finally degli inner-try): fatale.
            logging.exception("Main loop crash (fatal)")
            return 1
        finally:
            self.fetcher.stop()
            pygame.quit()

    # -----------------------------------------------------------------------
    # Event handling
    # -----------------------------------------------------------------------

    def _handle_event(self, event: pygame.event.Event) -> None:
        # Diagnostico iniziale: logga i primi N eventi per capire che tipo
        # genera SDL2 sotto KMSDRM (a volte sono FINGER_*, altre volte
        # MOUSE_*, dipende dall'env e dal driver touchscreen).
        if not hasattr(self, '_event_diag_count'):
            self._event_diag_count = 0
        if self._event_diag_count < 20:
            type_name = pygame.event.event_name(event.type)
            if type_name not in ("Unknown", "ActiveEvent", "VideoExpose"):
                logging.info("EVENT[%d] type=%s (%d) dict=%s",
                              self._event_diag_count, type_name, event.type,
                              {k: v for k, v in event.__dict__.items()
                               if k not in ('window', 'instance_id')})
                self._event_diag_count += 1

        if event.type == pygame.QUIT:
            self._running = False
        elif event.type == pygame.KEYDOWN:
            if event.key == pygame.K_ESCAPE:
                self._running = False
        elif event.type == pygame.MOUSEBUTTONDOWN and event.button == 1:
            self._on_press(event.pos)
        elif event.type == pygame.MOUSEBUTTONUP and event.button == 1:
            self._on_release(event.pos)
        elif event.type == pygame.FINGERDOWN:
            # Touch event SDL2: x/y sono in coordinate normalizzate [0,1].
            # Le convertiamo a pixel screen-space.
            x = int(event.x * self.cfg.screen_width)
            y = int(event.y * self.cfg.screen_height)
            self._on_press((x, y))
        elif event.type == pygame.FINGERUP:
            x = int(event.x * self.cfg.screen_width)
            y = int(event.y * self.cfg.screen_height)
            self._on_release((x, y))
        # FINGERMOTION: ignorato (potrebbe essere usato per slide live ma
        # il design attuale usa start/end senza tracking continuo).

    def _on_press(self, pos: tuple[int, int]) -> None:
        self._press_xy = pos
        self._press_time_ms = pygame.time.get_ticks()
        self._long_press_fired = False
        logging.debug("PRESS at %s", pos)

    def _on_release(self, pos: tuple[int, int]) -> None:
        if self._press_xy is None:
            return
        dur = pygame.time.get_ticks() - self._press_time_ms
        start = self._press_xy
        self._press_xy = None
        logging.debug("RELEASE at %s durata=%dms long_press_fired=%s", pos, dur,
                      self._long_press_fired)
        if self._long_press_fired:
            self._long_press_fired = False
            return

        dx = pos[0] - start[0]
        dy = pos[1] - start[1]
        is_swipe_h = (abs(dx) >= self.cfg.swipe_min_horizontal_px
                      and abs(dy) <= self.cfg.swipe_max_vertical_px
                      and dur <= self.cfg.swipe_max_duration_ms)
        is_swipe_v = (abs(dy) >= self.cfg.swipe_min_vertical_px
                      and abs(dx) <= self.cfg.swipe_max_horizontal_px
                      and dur <= self.cfg.swipe_max_duration_ms)

        # --- Routing degli swipe per mode ---
        if self.mode == MODE_DETAIL:
            # In DETAIL: solo swipe orizzontale = cambia pagina (ciclico)
            # Verticale ignorato (l'utente vuole tappare per uscire)
            if is_swipe_h:
                direction = -1 if dx > 0 else 1
                self._on_swipe_detail_page(direction)
                return

        elif self.mode in (MODE_HANDS, MODE_DIGITAL, MODE_CURRENT, MODE_CHART):
            # Carosello orizzontale infinito a 3 ELEMENTI:
            #   HANDS → CURRENT → CHART → HANDS → ...
            # DIGITAL NON fa parte del carosello (si raggiunge solo via
            # long-press da HANDS). Quando si è in DIGITAL e si fa swipe,
            # DIGITAL si comporta come HANDS (idx 0) per scegliere la
            # destinazione: swipe forward → CURRENT, swipe backward → CHART.
            # Convenzione direzione:
            #   - swipe RIGHT (dx>0, dito sinistra→destra) = FORWARD nel ciclo
            #   - swipe LEFT  (dx<0, dito destra→sinistra) = BACKWARD nel ciclo
            # Swipe verticale → carosello verticale [MAIN, ALERTS?, WEEKLY].
            # Se ci sono allerte: giù = MAIN→ALERTS→WEEKLY, su = inverso.
            # Se non ci sono: MAIN⇄WEEKLY (come prima).
            if is_swipe_v:
                self._vertical_carousel(dy)
                return
            if is_swipe_h:
                carousel = [MODE_HANDS, MODE_CURRENT, MODE_CHART]
                # DIGITAL non è nel carosello: trattato come HANDS (idx 0)
                if self.mode == MODE_DIGITAL:
                    cur_idx = 0
                else:
                    cur_idx = carousel.index(self.mode)
                if dx > 0:   # swipe right → forward
                    next_mode = carousel[(cur_idx + 1) % len(carousel)]
                    gesture = "right"
                else:        # swipe left → backward
                    next_mode = carousel[(cur_idx - 1) % len(carousel)]
                    gesture = "left"
                enter_fn = {
                    MODE_HANDS:   self._enter_hands,
                    MODE_CURRENT: self._enter_current,
                    MODE_CHART:   self._enter_chart,
                }[next_mode]
                mode_names = {MODE_HANDS: "HANDS", MODE_DIGITAL: "DIGITAL",
                              MODE_CURRENT: "CURRENT", MODE_CHART: "CHART"}
                logging.info("Swipe %s: %s → %s (carousel)",
                             gesture, mode_names[self.mode], mode_names[next_mode])
                enter_fn(gesture_direction=gesture)
                self._force_redraw = True
                return

        elif self.mode == MODE_WEEKLY:
            # In WEEKLY: swipe verticale → carosello [MAIN, ALERTS?, WEEKLY]
            # (slide segue il dito). Swipe orizzontale e tap: ignorati.
            if is_swipe_v:
                self._vertical_carousel(dy)
                return
            if is_swipe_h:
                logging.debug("Swipe oriz in WEEKLY ignorato")
                return
            # Anche tap su WEEKLY ignorato: ritorno senza chiamare _on_tap
            return

        elif self.mode == MODE_ALERTS:
            # Report allerte. Swipe verticale:
            #   - in DETTAGLIO → torna all'ELENCO (un livello su)
            #   - in ELENCO    → carosello verticale (esce da ALERTS)
            # Swipe orizzontale in DETTAGLIO → allerta precedente/successiva.
            if is_swipe_v:
                if self._alerts_view == "detail":
                    self._alerts_view = "list"
                    self._arm_mode_timer(self.cfg.alerts_timeout_seconds)
                    self._force_redraw = True
                    return
                self._vertical_carousel(dy)
                return
            if is_swipe_h:
                if (self._alerts_view == "detail"
                        and len(self._active_alerts_list) > 1):
                    direction = 1 if dx < 0 else -1   # swipe sx → successiva
                    n = len(self._active_alerts_list)
                    self._alerts_detail_idx = (self._alerts_detail_idx + direction) % n
                    self._arm_mode_timer(self.cfg.alerts_timeout_seconds)
                    self._force_redraw = True
                return
            # Tap: hit-test righe (elenco) o ritorno da dettaglio → _on_tap
            self._on_tap(pos)
            return

        # Niente swipe riconosciuto: tap (per HANDS/DIGITAL/CHART/DETAIL)
        self._on_tap(pos)

    def _on_swipe_detail_page(self, direction: int) -> None:
        """Swipe orizzontale in DETAIL: cambia pagina (0→1→2→0), ciclico.

        Stessa ora, solo cambio pagina. Slide orizzontale del SOLO pannello
        dettaglio centrale (le icone orarie + pillole + freshness restano fisse).

        direction: -1 = swipe destra = pagina precedente (slide_right del pannello)
        direction: +1 = swipe sinistra = pagina successiva (slide_left del pannello)
        """
        n_pages = self.cfg.detail_n_pages
        if n_pages <= 1:
            return
        old_page = self.detail_page
        new_page = (old_page + direction) % n_pages
        if new_page == old_page:
            return
        logging.info("Swipe %+d: page %d → %d (ora +%dh)", direction,
                     old_page, new_page, self.detail_hours_ahead)
        self.detail_page = new_page
        self._begin_detail_page_transition(old_page, new_page, direction)
        self._arm_mode_timer(self.cfg.detail_timeout_seconds)
        self._force_redraw = True

    def _begin_detail_page_transition(self, from_page: int, to_page: int,
                                       direction: int) -> None:
        """Avvia una transizione slide oriz tra due pagine DETAIL.

        Solo il pannello centrale slida; le icone orarie restano ferme.
        direction: -1 = swipe destra = pagina precedente → slide_right
                   +1 = swipe sinistra = pagina successiva → slide_left

        PRE-RENDER DEI BUFFER: per evitare il "flash" del primo frame
        (in cui il pannello DETAIL sparisce mentre i buffer non sono
        ancora pronti, lasciando vedere icone+lancette sotto), li
        renderizziamo SUBITO qui, prima che parta la transizione.
        """
        if self.cfg.resolve_transition_duration("slide_left") <= 0:
            return
        self._transition_active = True
        self._transition_start_ms = pygame.time.get_ticks()
        self._transition_from_mode = MODE_DETAIL
        self._transition_to_mode = MODE_DETAIL
        self._transition_style = (self.TRANSITION_SLIDE_LEFT if direction > 0
                                  else self.TRANSITION_SLIDE_RIGHT)
        self._transition_fade_buffers_ready = False
        self._transition_slide_buffers_ready = False
        self._transition_from_detail_page = from_page
        self._transition_to_detail_page = to_page
        self._transition_from_detail_offset = None
        self._transition_to_detail_offset = None

        # PRE-RENDER dei due buffer del pannello DETAIL (from_page e to_page).
        # Lo facciamo qui (chiamato da event handler) invece che al primo
        # frame del slide render, così:
        #   - Risparmiamo ~10ms al primo frame (no over-budget)
        #   - Evitiamo il "flash" della base quadrante in cui il pannello
        #     centrale è momentaneamente vuoto al primo frame
        try:
            buf_from, buf_to = self.rb.get_transition_buffers()
            for buf, page in [(buf_from, from_page), (buf_to, to_page)]:
                self.rb.renderer.target = buf
                self.rb.renderer.draw_color = (0, 0, 0, 255)
                self.rb.renderer.clear()
                self._render_detail_panel_only(page)
            self.rb.renderer.target = None
            self._transition_slide_buffers_ready = True
        except Exception:
            # Fallback: i buffer saranno renderizzati al primo frame
            # (comportamento precedente, con possibile flash).
            logging.exception("Pre-render detail page buffers failed")
            self._transition_slide_buffers_ready = False

        logging.debug("Detail page slide: page %d → %d style=%s",
                      from_page, to_page, self._transition_style)

    def _handle_long_press(self, pos: tuple[int, int]) -> None:
        """Long-press: toggle HANDS ↔ DIGITAL (fade).

        Funziona ovunque sullo schermo, non solo al centro.
        Da WEEKLY o DETAIL, il long-press non fa nulla (escono via swipe/tap).
        """
        self._long_press_fired = True
        if self.mode == MODE_HANDS:
            logging.info("LONG-PRESS → DIGITAL")
            self._enter_digital()
            self._force_redraw = True
        elif self.mode == MODE_DIGITAL:
            logging.info("LONG-PRESS → HANDS")
            self._enter_hands()
            self._force_redraw = True

    def _on_tap(self, pos: tuple[int, int]) -> None:
        cfg = self.cfg
        x, y = pos
        logging.debug("TAP at %s mode=%d", pos, self.mode)

        # In OFF mode, qualsiasi tap risveglia il display
        if self.mode == MODE_OFF:
            logging.info("TAP in OFF → HANDS")
            self._enter_hands()
            self._force_redraw = True
            return

        # Report allerte: il tap è gestito qui. In ELENCO fa da hit-test sulle
        # righe (apre il dettaglio); in DETTAGLIO torna all'elenco.
        if self.mode == MODE_ALERTS:
            self._arm_mode_timer(cfg.alerts_timeout_seconds)
            if self._alerts_view == "detail":
                self._alerts_view = "list"
                self._force_redraw = True
                return
            for rect, aidx in self._alerts_row_rects:
                if rect.collidepoint(x, y):
                    self._alerts_detail_idx = aidx
                    self._alerts_view = "detail"
                    self._force_redraw = True
                    return
            return

        # Tap sul banner allerta (visibile solo in HANDS/DIGITAL) → apre il
        # report allerte. Sola lettura: nessun dismiss manuale (il banner
        # riflette le allerte attive e sparisce quando OWM le rimuove).
        if (self.mode in (MODE_HANDS, MODE_DIGITAL)
                and self._active_alert is not None
                and self._cache_alert is not None):
            _, _, (pill_w, pill_h) = self._cache_alert
            pill_cx = cfg.center_x
            pill_cy = cfg.center_y + cfg.alert_y_offset
            if (abs(x - pill_cx) <= pill_w // 2
                    and abs(y - pill_cy) <= pill_h // 2):
                logging.info("TAP su banner allerta → ALERTS")
                self._enter_alerts()
                self._force_redraw = True
                return

        # Hour icon hit (HANDS, DIGITAL, CHART, DETAIL — non WEEKLY)
        hour_idx = self._hit_test_hour(x, y)
        if hour_idx is not None and self.mode != MODE_WEEKLY:
            ahead = self._hour_idx_to_offset(hour_idx)
            if self.mode == MODE_DETAIL and ahead == self.detail_hours_ahead:
                # Stesso DETAIL già attivo: solo re-arm timer
                logging.info("Tap su ora attiva, re-arm timer")
                self._arm_mode_timer(cfg.detail_timeout_seconds)
            else:
                # Entra in DETAIL (fade, gestito da _enter_detail)
                self._enter_detail(ahead)
                self._force_redraw = True
            return

        # Tap su area "non icona":
        if self.mode == MODE_HANDS:
            # Tap su area non-icona in HANDS: nessuna azione (long-press serve per DIGITAL)
            pass
        elif self.mode == MODE_DIGITAL:
            # Qualsiasi tap (non long-press) in DIGITAL → torna a HANDS
            logging.info("TAP in DIGITAL → HANDS")
            self._enter_hands()
            self._force_redraw = True
        elif self.mode == MODE_CHART:
            # Tap in CHART (no icona): nessuna azione (esce con swipe o timeout)
            # Re-arm timer per essere gentili
            self._arm_mode_timer(cfg.chart_timeout_seconds)
        elif self.mode == MODE_CURRENT:
            # Tap in CURRENT (no icona): re-arm timer, niente di più
            self._arm_mode_timer(cfg.current_timeout_seconds)
        elif self.mode == MODE_DETAIL:
            # Tap fuori dalle icone in DETAIL → torna a HANDS (fade)
            logging.info("TAP in DETAIL (no icona) → HANDS")
            self._enter_hands()
            self._force_redraw = True
        elif self.mode == MODE_WEEKLY:
            # WEEKLY: tap ignorato (si esce solo via swipe verticale o timeout)
            pass

    def _hit_test_hour(self, x: int, y: int) -> Optional[int]:
        size = self.cfg.hourly_touch_size
        for idx, (hx, hy) in enumerate(self.hour_pos):
            if abs(x - hx) < size / 2 and abs(y - hy) < size / 2:
                return idx
        return None

    def _hour_idx_to_offset(self, idx: int) -> int:
        dial_hour = idx + 1
        now_h = self._location_now().hour % 12 or 12
        return (dial_hour - now_h) % 12

    # -----------------------------------------------------------------------
    # Mode transitions
    # -----------------------------------------------------------------------

    def _arm_mode_timer(self, seconds: int) -> None:
        if seconds > 0:
            self._mode_deadline = time.monotonic() + seconds
        else:
            self._mode_deadline = None

    def _enter_hands(self, gesture_direction: Optional[str] = None) -> None:
        """Entra in HANDS mode.

        gesture_direction: "up"/"down"/"left"/"right" per slide direzionale
        (segue il dito), None = fade (default per tap, timeout, ecc).
        """
        if self.mode != MODE_HANDS:
            self._begin_transition(self.mode, MODE_HANDS, gesture_direction)
        self.mode = MODE_HANDS
        self._mode_deadline = None

    def _enter_off(self) -> None:
        """Schermo nero: zero rendering, massima CPU disponibile."""
        # OFF è esplicitamente senza transizione (l'utente vuole spegnere)
        self.mode = MODE_OFF
        self._mode_deadline = None

    def _enter_digital(self, gesture_direction: Optional[str] = None) -> None:
        if self.mode != MODE_DIGITAL:
            self._begin_transition(self.mode, MODE_DIGITAL, gesture_direction)
        self.mode = MODE_DIGITAL
        self._arm_mode_timer(self.cfg.digital_timeout_seconds)

    def _enter_chart(self, gesture_direction: Optional[str] = None) -> None:
        """Entra nella vista CHART (grafico temperatura 24h centrale)."""
        if self.mode != MODE_CHART:
            self._begin_transition(self.mode, MODE_CHART, gesture_direction)
        self.mode = MODE_CHART
        self._arm_mode_timer(self.cfg.chart_timeout_seconds)
        logging.info("→ CHART")

    def _enter_current(self, gesture_direction: Optional[str] = None) -> None:
        """Entra nella vista CURRENT (dati attuali completi al centro)."""
        if self.mode != MODE_CURRENT:
            self._begin_transition(self.mode, MODE_CURRENT, gesture_direction)
        self.mode = MODE_CURRENT
        self._arm_mode_timer(self.cfg.current_timeout_seconds)
        logging.info("→ CURRENT")

    def _enter_weekly(self, gesture_direction: Optional[str] = None) -> None:
        """Entra in modalità previsione 7 giorni."""
        if self.mode != MODE_WEEKLY:
            self._begin_transition(self.mode, MODE_WEEKLY, gesture_direction)
        self.mode = MODE_WEEKLY
        # Reset del cache panel (era usato per DETAIL, ora per WEEKLY)
        self._cache_detail_panel = None
        self._cache_weekly_panel = None
        self._arm_mode_timer(self.cfg.weekly_timeout_seconds)
        logging.info("→ WEEKLY")

    def _enter_alerts(self, gesture_direction: Optional[str] = None) -> None:
        """Entra nella pagina report allerte, sempre partendo dall'elenco.

        Raggiungibile dal tap sul banner (fade) o dallo swipe verticale del
        carosello (slide che segue il dito). Se non ci sono allerte attive la
        pagina mostra lo stato vuoto e il timeout riporta a HANDS.
        """
        if self.mode != MODE_ALERTS:
            self._begin_transition(self.mode, MODE_ALERTS, gesture_direction)
        self.mode = MODE_ALERTS
        self._alerts_view = "list"
        if self._alerts_detail_idx >= len(self._active_alerts_list):
            self._alerts_detail_idx = 0
        self._cache_alerts_panel = None
        self._arm_mode_timer(self.cfg.alerts_timeout_seconds)
        logging.info("→ ALERTS (%d attive)", len(self._active_alerts_list))

    def _vertical_carousel(self, dy: int) -> None:
        """Naviga il carosello verticale circolare.

        Anello: [HANDS, ALERTS, WEEKLY] quando ci sono allerte attive, altrimenti
        [HANDS, WEEKLY] (comportamento storico). Le viste sorelle del carosello
        orizzontale (DIGITAL/CURRENT/CHART) contano come "main" (indice 0).

          swipe giù (dy>0) → avanti  (+1):  MAIN → ALERTS → WEEKLY → MAIN
          swipe su  (dy<0) → indietro(-1):  MAIN → WEEKLY → ALERTS → MAIN
        """
        # Caso limite: siamo nella pagina ALERTS ma le allerte si sono azzerate
        # (refresh in background). ALERTS non è più nell'anello: qualsiasi swipe
        # verticale torna a HANDS invece di finire sempre su WEEKLY.
        if self.mode == MODE_ALERTS and not self._active_alerts_list:
            self._enter_hands(gesture_direction=("down" if dy > 0 else "up"))
            self._force_redraw = True
            return
        has_alerts = bool(self._active_alerts_list)
        ring = ([MODE_HANDS, MODE_ALERTS, MODE_WEEKLY] if has_alerts
                else [MODE_HANDS, MODE_WEEKLY])
        if self.mode in ring:
            cur = ring.index(self.mode)
        else:
            cur = 0   # HANDS/DIGITAL/CURRENT/CHART = livello "main"
        step = 1 if dy > 0 else -1
        nxt = ring[(cur + step) % len(ring)]
        gesture = "down" if dy > 0 else "up"
        if nxt == MODE_HANDS:
            self._enter_hands(gesture_direction=gesture)
        elif nxt == MODE_ALERTS:
            self._enter_alerts(gesture_direction=gesture)
        elif nxt == MODE_WEEKLY:
            self._enter_weekly(gesture_direction=gesture)
        self._force_redraw = True

    def _enter_detail(self, hours_ahead: int,
                       gesture_direction: Optional[str] = None) -> None:
        """Entra in DETAIL@hours_ahead, sempre pagina 0.

        Casi:
          - mode != DETAIL: transizione standard (fade), via _begin_transition
          - mode == DETAIL e ora cambia: fade tra DETAIL@old_ora → DETAIL@new_ora
          - mode == DETAIL e ora uguale: niente, solo re-arm timer
        """
        if self.mode != MODE_DETAIL:
            # Entrata in DETAIL da altro mode (fade standard)
            self._begin_transition(self.mode, MODE_DETAIL, gesture_direction)
            self.mode = MODE_DETAIL
            self.detail_hours_ahead = hours_ahead
            self.detail_page = 0
        elif self.detail_hours_ahead != hours_ahead:
            # Già in DETAIL, ma cambio ora: fade DETAIL→DETAIL
            old_offset = self.detail_hours_ahead
            self.detail_hours_ahead = hours_ahead
            self.detail_page = 0
            self._begin_detail_hour_transition(old_offset, hours_ahead)
        else:
            # Stessa ora, niente da fare
            pass
        self._arm_mode_timer(self.cfg.detail_timeout_seconds)
        logging.info("DETAIL → +%dh page=0", hours_ahead)

    def _begin_detail_hour_transition(self, from_offset: int,
                                       to_offset: int) -> None:
        """Avvia un fade tra DETAIL@from_offset e DETAIL@to_offset.

        Sempre fade (no slide), perché il tap su un'ora diversa è cambio
        di "contesto" non scroll di una sequenza. Both render to page 0.

        PRE-RENDER DEI BUFFER: come per i fade normali (_begin_transition),
        renderizziamo subito qui il buf_from. Senza pre-render, al primo
        frame del fade i buffer non sono pronti → "flash" in cui si vede
        un layout simile a HANDS (icone + sfondo, ma senza il pannello
        DETAIL centrale).
        """
        if self.cfg.resolve_transition_duration("fade") <= 0:
            return
        self._transition_active = True
        self._transition_start_ms = pygame.time.get_ticks()
        self._transition_from_mode = MODE_DETAIL
        self._transition_to_mode = MODE_DETAIL
        self._transition_style = self.TRANSITION_FADE
        self._transition_fade_buffers_ready = False
        self._transition_slide_buffers_ready = False
        # Override del detail_hours_ahead durante render dei due buffer fade
        self._transition_from_detail_offset = from_offset
        self._transition_to_detail_offset = to_offset
        # No page transition (entrambi sono page 0)
        self._transition_from_detail_page = None
        self._transition_to_detail_page = None

        # PRE-RENDER buf_from (DETAIL@from_offset). Il from è il "vecchio"
        # stato che sta sfumando: dobbiamo snapshottarlo PRIMA che
        # `self.detail_hours_ahead` venga letto come "nuovo" durante il fade.
        # buf_to NON viene pre-renderizzato qui perché lo facciamo al primo
        # frame del fade dove `icons_overlay=True` skippa le icone (le
        # disegniamo come overlay live). Se pre-renderizzassimo `to` con
        # skip_animated dipenderebbe da uno stato condiviso col fade render
        # → meglio lasciarlo al primo frame.
        try:
            modes_with_icons = {MODE_HANDS, MODE_DETAIL, MODE_DIGITAL,
                                  MODE_CHART, MODE_CURRENT}
            # DETAIL→DETAIL: both in modes_with_icons → icons_overlay=True
            icons_overlay = True
            buf_from, _buf_to = self.rb.get_transition_buffers()
            try:
                buf_from.blend_mode = 1
            except Exception:
                pass
            logging.info("DETAIL fade pre-render: mode=%s detail_offset=%d → buf_from with from_offset=%d",
                         self._MODE_NAMES.get(self.mode, "?"),
                         self.detail_hours_ahead, from_offset)
            self._render_to_buffer(buf_from, MODE_DETAIL, from_offset,
                                     skip_animated=icons_overlay)
            # Flag analogo a quello di _begin_transition: il fade render
            # vedrà che il from è pronto e non lo ri-renderizzerà.
            self._transition_fade_from_prerendered = True
        except Exception:
            logging.exception("Pre-render detail hour fade buf_from failed")
            self._transition_fade_from_prerendered = False

        logging.debug("Detail hour fade: %dh → %dh", from_offset, to_offset)

    def _begin_transition(self, from_mode: int, to_mode: int,
                           gesture_direction: Optional[str] = None) -> None:
        """Avvia l'animazione di transizione tra due modes (Fase 4).

        Tipo di animazione (slide vert / fade / slide oriz) deciso automatic.
        in base alla coppia (from, to). Vedi _pick_transition_style.

        PRE-RENDER OTTIMIZZATO PER PI ZERO W: per i fade pre-renderizziamo
        il buf_from (snapshot dello stato `from_mode`) QUI in `_begin_transition`,
        invece che al primo frame del fade. Motivo: il primo frame del fade
        deve già fare render del to_mode + blit + overlay icone; se aggiungiamo
        anche il render del from_mode supera il budget 16ms a 60fps → frame
        drop visibile = stutter all'inizio della transizione.
        Spostando il render del from QUI (chiamato da event handler, non
        durante render loop), lo "spalmiamo" su un frame separato.
        """
        if from_mode == to_mode:
            return
        # Style picking PRIMA del check di durata, così possiamo controllare
        # la durata specifica del tipo di transizione (fade vs slide).
        style = self._pick_transition_style(from_mode, to_mode, gesture_direction)
        if self.cfg.resolve_transition_duration(style) <= 0:
            return
        self._transition_active = True
        self._transition_start_ms = pygame.time.get_ticks()
        self._transition_from_mode = from_mode
        self._transition_to_mode = to_mode
        self._transition_style = style
        # Flag per cachare il rendering dei buffer fade (set False ad ogni
        # nuova transizione fade; renderiamo i buffer solo al primo frame)
        self._transition_fade_buffers_ready = False
        self._transition_slide_buffers_ready = False
        # Reset degli override DETAIL: questi sono usati SOLO dai fade
        # DETAIL→DETAIL ora-diversa (gestiti da _begin_detail_hour_transition)
        # e dagli slide DETAIL page-diversa (_begin_detail_page_transition).
        # In un fade standard mode→mode questi devono essere None, altrimenti
        # forzerebbero il render del buffer to/from a un offset/pagina vecchio
        # rimasto da una transizione precedente.
        # BUG STORICO: senza questo reset, un fade HANDS→DETAIL fatto dopo un
        # precedente fade DETAIL@A→DETAIL@B mostrava per ~150-200ms il
        # pannello DETAIL@A o DETAIL@B vecchio prima che la cache_detail si
        # aggiornasse al nuovo offset.
        self._transition_from_detail_offset = None
        self._transition_to_detail_offset = None
        self._transition_from_detail_page = None
        self._transition_to_detail_page = None

        # PRE-RENDER del from_mode su buf_from se è un fade.
        # Solo per fade (slide oriz usa altro meccanismo, slide vert anche).
        # Risparmia ~5-10ms al primo frame della transizione.
        if self._transition_style == self.TRANSITION_FADE:
            try:
                modes_with_icons = {MODE_HANDS, MODE_DETAIL, MODE_DIGITAL,
                                      MODE_CHART, MODE_CURRENT}
                icons_overlay = (from_mode in modes_with_icons
                                 and to_mode in modes_with_icons)
                buf_from, buf_to = self.rb.get_transition_buffers()
                try:
                    buf_from.blend_mode = 1
                    buf_to.blend_mode = 1
                except Exception:
                    pass
                # Render del from sul buffer SUBITO (lo schermo mostra ancora
                # from_mode in questo momento)
                from_off = getattr(self, '_transition_from_detail_offset', None)
                self._render_to_buffer(buf_from, from_mode, from_off,
                                         skip_animated=icons_overlay)
                # Marker: il from è già pronto; il primo frame del fade
                # renderizzerà solo il to (vedi _transition_fade_buffers_ready)
                self._transition_fade_from_prerendered = True
            except Exception:
                logging.exception("Pre-render from buffer failed (continuerà al primo frame)")
                self._transition_fade_from_prerendered = False
        else:
            self._transition_fade_from_prerendered = False

        logging.debug("Transition begin: %s → %s style=%s",
                      from_mode, to_mode, self._transition_style)

    # -----------------------------------------------------------------------
    # Alerts
    # -----------------------------------------------------------------------

    def _check_alerts(self) -> None:
        """Raccoglie TUTTE le allerte attive (API + sintetiche derivate dai
        weather condition codes di OpenWeatherMap).

        Allerte sintetiche generate da:
          1. weather[].id in current/hourly[0..6] mappati via
             cfg.synthetic_alert_codes (tornado, squall, pioggia estrema/
             gelata, neve abbondante, sleet, temporali violenti)
          2. raffiche oltre cfg.wind_gust_alert_threshold_ms (current o hourly)
          3. vento sostenuto oltre cfg.wind_speed_alert_threshold_ms
          4. grandine sintetica via keyword in weather[].description
             (OWM non ha code dedicato per grandine; appare però testualmente
             dentro alcuni thunderstorm)

        Le allerte vengono deduplicate per categoria (es. se sia "current"
        che hourly[0] hanno tornado, una sola allerta tornado).

        Severità (1=info, 2=warning, 3=danger) potrà servire in futuro
        per colorare diversamente il banner. Per ora solo l'ordine
        nella lista (severità decrescente).
        """
        # La cache del report dipende dalla lista: invalidala a ogni ricalcolo
        # (anche quando le allerte si azzerano).
        self._cache_alerts_panel = None
        if not (self.cfg.show_alerts and self.weather_data):
            self._active_alerts_list = []
            self._active_alert = None
            self._alerts_detail_idx = 0
            return

        cfg = self.cfg
        lang = cfg.language
        collected: list[dict] = []
        # Track delle categorie già emesse per dedup (un alert per categoria)
        seen_categories: dict[str, dict] = {}

        # ==================================================================
        # 1. Allerte API ufficiali (sempre con severità massima, no dedup)
        # ==================================================================
        for a in self.weather_data.get("alerts") or []:
            event = a.get("event", "alert")
            sender = a.get("sender_name", "")
            start_ts = a.get("start", 0)
            end_ts = a.get("end")
            description = (a.get("description") or "").strip()
            key = f"api|{event}|{start_ts}"
            if key in self._dismissed_alert_keys:
                continue
            text = f"⚠ {event}"
            if sender:
                text += f" — {sender}"
            # Campi ricchi per il report (dettaglio): fonte + intervallo +
            # descrizione ufficiale completa (prima venivano scartati).
            sub_parts = []
            if sender:
                sub_parts.append(sender)
            timerange = self._alert_timerange_text(start_ts, end_ts, lang)
            if timerange:
                sub_parts.append(timerange)
            collected.append({
                "key": key, "text": text, "source": "api", "severity": 3,
                "title": event, "subtitle": " · ".join(sub_parts),
                "body": description,
            })

        # ==================================================================
        # 2. Allerte sintetiche da weather condition codes
        # ==================================================================
        # Costruisci lista (hour_offset, weather_data_block):
        current = self.weather_data.get("current") or {}
        hourly = self.weather_data.get("hourly") or []
        sources: list[tuple[int, dict]] = [(0, current)]
        for i, h in enumerate(hourly[:6], start=1):
            sources.append((i, h))

        for hour_off, src in sources:
            for w in src.get("weather") or []:
                code = w.get("id")
                if code in cfg.synthetic_alert_codes:
                    category, severity, label_it, label_en = cfg.synthetic_alert_codes[code]
                    label = label_it if lang == "it" else label_en
                    # Genera testo con orizzonte temporale
                    text = self._format_synthetic_alert_text(
                        label, hour_off, lang
                    )
                    # Dedup per categoria: prendiamo solo l'occorrenza più
                    # imminente (hour_off più basso) di ogni categoria
                    if category in seen_categories:
                        continue
                    key = f"synthetic|{category}|{hour_off}"
                    if key in self._dismissed_alert_keys:
                        seen_categories[category] = {}  # marca come "visto"
                        continue
                    alert = {
                        "key": key, "text": text,
                        "source": "synthetic", "severity": severity,
                        "category": category, "hour_offset": hour_off,
                        "title": label,
                        "subtitle": self._synthetic_horizon(hour_off, lang),
                        "body": self._synthetic_body(lang),
                    }
                    seen_categories[category] = alert
                    collected.append(alert)
                    break  # solo prima condizione meteo per blocco

        # ==================================================================
        # 3. Allerta grandine (keyword in description: OWM non ha code dedicato)
        # ==================================================================
        if "hail" not in seen_categories:
            hail_keywords = tuple(k.lower() for k in cfg.hail_keywords)
            for hour_off, src in sources:
                hit = False
                for w in src.get("weather") or []:
                    desc = (w.get("description") or "").lower()
                    main = (w.get("main") or "").lower()
                    if any(kw in f"{main} {desc}" for kw in hail_keywords):
                        hit = True
                        break
                if hit:
                    label = "Grandine" if lang == "it" else "Hail"
                    text = self._format_synthetic_alert_text(label, hour_off, lang)
                    key = f"synthetic|hail|{hour_off}"
                    if key not in self._dismissed_alert_keys:
                        collected.append({
                            "key": key, "text": text,
                            "source": "synthetic", "severity": 3,
                            "category": "hail", "hour_offset": hour_off,
                            "title": label,
                            "subtitle": self._synthetic_horizon(hour_off, lang),
                            "body": self._synthetic_body(lang),
                        })
                    break

        # ==================================================================
        # 4. Allerta raffiche violente
        # ==================================================================
        if "wind_gust" not in seen_categories:
            max_gust = 0.0
            max_gust_hour = 0
            for hour_off, src in sources:
                gust = src.get("wind_gust") or 0.0
                if gust > max_gust:
                    max_gust = gust
                    max_gust_hour = hour_off
            if max_gust >= cfg.wind_gust_alert_threshold_ms:
                gust_kmh = round(max_gust * 3.6)
                label_it = f"Raffiche {gust_kmh} km/h"
                label_en = f"Gusts {gust_kmh} km/h"
                label = label_it if lang == "it" else label_en
                text = self._format_synthetic_alert_text(label, max_gust_hour, lang)
                key = f"synthetic|wind_gust|{max_gust_hour}|{gust_kmh}"
                if key not in self._dismissed_alert_keys:
                    collected.append({
                        "key": key, "text": text,
                        "source": "synthetic", "severity": 2,
                        "category": "wind_gust", "hour_offset": max_gust_hour,
                        "title": label,
                        "subtitle": self._synthetic_horizon(max_gust_hour, lang),
                        "body": self._synthetic_body(lang),
                    })

        # ==================================================================
        # 5. Allerta vento sostenuto
        # ==================================================================
        if "wind_speed" not in seen_categories:
            max_wind = 0.0
            max_wind_hour = 0
            for hour_off, src in sources:
                w = src.get("wind_speed") or 0.0
                if w > max_wind:
                    max_wind = w
                    max_wind_hour = hour_off
            if max_wind >= cfg.wind_speed_alert_threshold_ms:
                wind_kmh = round(max_wind * 3.6)
                label_it = f"Vento forte {wind_kmh} km/h"
                label_en = f"Strong wind {wind_kmh} km/h"
                label = label_it if lang == "it" else label_en
                text = self._format_synthetic_alert_text(label, max_wind_hour, lang)
                key = f"synthetic|wind_speed|{max_wind_hour}|{wind_kmh}"
                if key not in self._dismissed_alert_keys:
                    collected.append({
                        "key": key, "text": text,
                        "source": "synthetic", "severity": 2,
                        "category": "wind_speed", "hour_offset": max_wind_hour,
                        "title": label,
                        "subtitle": self._synthetic_horizon(max_wind_hour, lang),
                        "body": self._synthetic_body(lang),
                    })

        # ==================================================================
        # Ordina per severità decrescente (più gravi prima)
        # ==================================================================
        collected.sort(key=lambda a: -a.get("severity", 1))

        self._active_alerts_list = collected
        if not collected:
            self._active_alert = None
            self._active_alerts_idx = 0
            self._alerts_detail_idx = 0
            return

        if self._active_alerts_idx >= len(collected):
            self._active_alerts_idx = 0
        if self._alerts_detail_idx >= len(collected):
            self._alerts_detail_idx = 0
        self._active_alert = collected[self._active_alerts_idx]
        self._active_alerts_last_rotation = time.time()
        logging.info("Allerte attive: %d → mostro [%d/%d]: %s",
                     len(collected), self._active_alerts_idx + 1,
                     len(collected), self._active_alert["text"])

    def _format_synthetic_alert_text(self, label: str, hour_off: int,
                                       lang: str) -> str:
        """Genera testo per allerta sintetica con orizzonte temporale.

        Esempi (it):
          hour_off=0 → "⚠ Grandine in corso"
          hour_off=1 → "⚠ Grandine entro 1h"
          hour_off=N → "⚠ Grandine entro Nh"
        """
        return f"⚠ {label} {self._synthetic_horizon(hour_off, lang)}"

    # -----------------------------------------------------------------------
    # Settings hot-reload
    # -----------------------------------------------------------------------

    def _check_settings_reload(self) -> None:
        try:
            mtime = self.settings_path.stat().st_mtime
        except OSError:
            return
        if mtime <= self._settings_mtime:
            return
        self._settings_mtime = mtime
        try:
            new_cfg = Config.load(self.settings_path)
            new_cfg.validate()
        except (ValueError, json.JSONDecodeError, OSError) as e:
            logging.error("settings.json reload skipped (invalid): %s", e)
            return
        logging.info("settings.json reloaded")
        # Invalida le cache che dipendono dai colori del config
        _color_cache.clear()
        self._cache_pill_bg.clear()
        # SDL2: invalida tutte le Texture cached (colori potrebbero essere cambiati)
        self.rb.clear_caches()
        self._cache_center_temp = None
        self._cache_freshness = None
        self._cache_alert = None
        self._cache_digital_time = None
        self._cache_digital_date = None
        self._cache_digital_temp = None
        self._cache_moon = None
        # Invalida tutte le cache luna (moon + moon_mini + current_moon).
        # Importante perché cambi a moon_antialias_scale, moon_lit_color,
        # moon_dark_color richiedono rebuild della Texture luna.
        if hasattr(self, '_cache_moon_mini'):
            self._cache_moon_mini = None
        if hasattr(self, '_cache_current_moon_tex'):
            self._cache_current_moon_tex = None
        self._cache_sun_times = None
        self._cache_detail_panel = None
        self._cache_weekly_panel = None
        self._cache_alerts_panel = None
        if hasattr(self, '_cache_pills_overlay'):
            self._cache_pills_overlay = None
        if hasattr(self, '_hand_textures'):
            self._hand_textures = None    # rebuild lazy on next _draw_hands
        # Field-by-field reload would be cleaner, but for Pygame we can just
        # force a full background rebuild and font reload if sizes changed.
        old = self.cfg
        self.cfg = new_cfg
        # Re-apply log_level se cambiato (basta setLevel su root logger,
        # gli handler esistenti rispetteranno il nuovo filtro).
        if old.log_level != new_cfg.log_level:
            new_level = getattr(logging, new_cfg.log_level.upper(), logging.INFO)
            logging.getLogger().setLevel(new_level)
            logging.warning("log_level cambiato: %s → %s",
                            old.log_level, new_cfg.log_level)
        # Re-risolvi la funzione di easing se cambiata
        if old.transition_easing != new_cfg.transition_easing:
            self._ease_fn = self._resolve_easing()
            logging.info("Easing cambiato: %s → %s",
                         old.transition_easing, new_cfg.transition_easing)
        self._static_bg_dirty = True
        # Recreate fonts if size changed
        if (old.values_font_size != new_cfg.values_font_size
                or old.font_name != new_cfg.font_name):
            self.font_values = self._make_font(new_cfg.font_name,
                                               new_cfg.values_font_size,
                                               bold=new_cfg.values_font_bold)
        if old.center_temp_font_size != new_cfg.center_temp_font_size:
            self.font_center_temp = self._make_font(new_cfg.font_name,
                                                    new_cfg.center_temp_font_size,
                                                    bold=True)
        # Report allerte: ricrea i font se cambiano dimensioni o font_name.
        _font_changed = old.font_name != new_cfg.font_name
        if _font_changed or old.alerts_header_font_size != new_cfg.alerts_header_font_size:
            self.font_alerts_header = self._make_font(
                new_cfg.font_name, new_cfg.alerts_header_font_size, bold=True)
        if _font_changed or old.alerts_title_font_size != new_cfg.alerts_title_font_size:
            self.font_alerts_title = self._make_font(
                new_cfg.font_name, new_cfg.alerts_title_font_size, bold=True)
        if _font_changed or old.alerts_meta_font_size != new_cfg.alerts_meta_font_size:
            self.font_alerts_meta = self._make_font(
                new_cfg.font_name, new_cfg.alerts_meta_font_size, bold=False)
        if _font_changed or old.alerts_body_font_size != new_cfg.alerts_body_font_size:
            self.font_alerts_body = self._make_font(
                new_cfg.font_name, new_cfg.alerts_body_font_size, bold=False)

        # Animation: se cambiano fps, numero frame, o AA scale, ricalcola sheet
        # Aggiorna cached anim period se cambia animation_fps
        if old.animation_fps != new_cfg.animation_fps:
            self._anim_period = (1.0 / new_cfg.animation_fps
                                  if new_cfg.animation_fps > 0 else 0.0)

        # E LE TEXTURE GPU CORRISPONDENTI.
        # IMPORTANTE: senza ricreare anche icon_textures, dopo cambio AA gli
        # sheets sono nuovi ma le texture GPU sono ancora vecchie → mismatch
        # e potenziale crash.
        if (old.animation_n_frames != new_cfg.animation_n_frames
                or old.animate_icons != new_cfg.animate_icons
                or old.icon_antialias_scale != new_cfg.icon_antialias_scale):
            if new_cfg.animate_icons:
                logging.info("Re-rasterizzo spritesheet (n_frames %d → %d, AA %d → %d)",
                             old.animation_n_frames, new_cfg.animation_n_frames,
                             old.icon_antialias_scale, new_cfg.icon_antialias_scale)
                t0 = time.monotonic()
                # 1. Drop dei riferimenti alle vecchie strutture. Python le
                #    libera quando il GC le visita. Non forziamo gc.collect()
                #    perché su ARMv6 single-core è molto costoso (50-200ms)
                #    e può creare stutter percepibile durante il rebuild.
                self.icon_textures = {}
                self.icon_sheets = {}
                # 2. Rebuild sheets a nuova risoluzione/AA
                for name in icon_animations.DRAWERS.keys():
                    self.icon_sheets[name] = icon_animations.precompute_spritesheet(
                        name, new_cfg.icon_size, new_cfg.animation_n_frames,
                        antialias_scale=new_cfg.icon_antialias_scale,
                    )
                # 3. Rebuild Texture GPU dai nuovi sheets
                for name, frames in self.icon_sheets.items():
                    texs = []
                    for frame in frames:
                        tex = self.rb.surface_to_texture(frame)
                        try:
                            tex.blend_mode = 1
                        except AttributeError:
                            pass
                        texs.append(tex)
                    self.icon_textures[name] = texs
                logging.info("Spritesheet + texture GPU pronti in %.2fs",
                             time.monotonic() - t0)
            else:
                # Disabilitato: drop di tutto
                self.icon_sheets = {}
                self.icon_textures = {}
            self._anim_frame_idx = 0

        # ... other fonts get recreated lazily on next render


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def setup_logging(level_name: str) -> None:
    """Imposta il livello di logging.

    Usa `force=True` per resettare eventuali handler installati implicitamente
    da chiamate `logging.warning(...)` precedenti (es. Config.load che logga
    chiavi sconosciute PRIMA che setup_logging venga chiamato). Senza force,
    `basicConfig` è no-op se ci sono già handler e il livello richiesto non
    viene mai applicato.
    """
    level = getattr(logging, level_name.upper(), logging.INFO)
    logging.basicConfig(
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        level=level,
        force=True,
    )
    # Esplicito set del root logger level per ridondanza (basicConfig dovrebbe
    # bastare, ma alcune versioni di Python hanno bug noti su Pi armv6).
    logging.getLogger().setLevel(level)


def main() -> int:
    settings_path = BASE_DIR / "settings.json"
    if not settings_path.exists():
        print(f"settings.json non trovato: {settings_path}", file=sys.stderr)
        return 2
    try:
        cfg = Config.load(settings_path)
    except (json.JSONDecodeError, OSError) as e:
        print(f"Errore lettura settings.json: {e}", file=sys.stderr)
        return 2

    setup_logging(cfg.log_level)

    try:
        cfg.validate()
    except ValueError as e:
        logging.error("%s", e)
        return 2

    app = WeatherClockSDL2(cfg, settings_path)
    return app.run()


if __name__ == "__main__":
    sys.exit(main())

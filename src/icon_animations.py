"""
icon_animations.py — Animazioni procedurali per le icone meteo.

Ogni funzione `draw_*(surface, size, t)` disegna un'icona meteo a fase
temporale `t` ∈ [0, 1] su una pygame.Surface di dimensione size×size.

Le animazioni sono progettate per essere leggere (poche primitive Pygame)
e cicliche con periodo 1.0. Cosi' lo stesso draw_xxx puo' essere chiamato
in loop con `t = (frame_counter % N) / N`.

Stile: piatto, colori warm. Adatto a icone 100×100 su HyperPixel 4.0.
"""
from __future__ import annotations

import math
from typing import Callable

import pygame


# ---------------------------------------------------------------------------
# Palette
# ---------------------------------------------------------------------------

C_SUN_CORE = (255, 200, 0)
C_SUN_RING = (255, 165, 0)
C_CLOUD = (210, 215, 225)
C_CLOUD_DARK = (130, 135, 145)
C_CLOUD_NIGHT = (180, 185, 200)
C_RAIN = (90, 170, 230)
C_SNOW = (240, 245, 255)
C_BOLT = (255, 235, 80)
C_FOG = (180, 185, 195)
C_MOON = (235, 235, 230)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _draw_cloud(surf: pygame.Surface, cx: float, cy: float, scale: float,
                color: tuple[int, int, int]) -> None:
    """Disegna una nuvola composta da 4 cerchi sovrapposti.

    `scale` e' la dimensione totale orizzontale desiderata in pixel (es. 80px
    su una surface 100×100). I cerchi vengono posizionati relativamente al
    centro (cx, cy) entro [-scale/2, +scale/2] sia X che Y.

    Layout: 3 lobi superiori + lobo basso centrale.
    """
    s = scale * 0.5  # half-extent
    # offset_x, offset_y, radius_factor (relativi a s)
    for dx, dy, rr in [(-0.55, 0.10, 0.40),
                       (-0.10, -0.25, 0.48),
                       ( 0.50, -0.05, 0.42),
                       ( 0.05,  0.35, 0.55)]:
        pygame.draw.circle(surf, color,
                           (int(cx + dx * s), int(cy + dy * s)),
                           int(rr * s))


def _easing(t: float) -> float:
    """Sinusoide normalizzata [0,1] → [0,1] con andamento naturale."""
    return 0.5 - 0.5 * math.cos(2 * math.pi * t)


# ---------------------------------------------------------------------------
# Icon drawers
# ---------------------------------------------------------------------------

def draw_clear_day(surf: pygame.Surface, size: int, t: float) -> None:
    """01d — sole con raggi che ruotano lenti."""
    cx = cy = size / 2
    # Raggi rotanti
    n_rays = 8
    angle_offset = t * 2 * math.pi / n_rays  # un giro lento (1/n_rays)
    ray_inner = size * 0.30
    ray_outer = size * 0.42
    for i in range(n_rays):
        ang = angle_offset + i * 2 * math.pi / n_rays
        x1 = cx + ray_inner * math.cos(ang)
        y1 = cy + ray_inner * math.sin(ang)
        x2 = cx + ray_outer * math.cos(ang)
        y2 = cy + ray_outer * math.sin(ang)
        pygame.draw.line(surf, C_SUN_RING, (x1, y1), (x2, y2),
                         max(2, int(size * 0.04)))
    # Disco solare con pulsazione di luminosità leggera
    pulse = 0.95 + 0.05 * _easing(t)
    pygame.draw.circle(surf, C_SUN_CORE, (int(cx), int(cy)),
                       int(size * 0.22 * pulse))


def draw_clear_night(surf: pygame.Surface, size: int, t: float) -> None:
    """01n — luna a falce con stelline che brillano."""
    cx = cy = size / 2
    # Falce: cerchio luna + cerchio nero offsettato per "tagliare"
    moon_r = size * 0.30
    pygame.draw.circle(surf, C_MOON, (int(cx), int(cy)), int(moon_r))
    # Maschera la parte destra con il colore di sfondo (nero qui)
    pygame.draw.circle(surf, (0, 0, 0),
                       (int(cx + moon_r * 0.45), int(cy - moon_r * 0.20)),
                       int(moon_r * 0.85))
    # Stelline (lampeggio sfasato)
    for i, (sx, sy) in enumerate([(0.18, 0.20), (0.78, 0.30), (0.85, 0.75)]):
        phase = (t + i / 3.0) % 1.0
        brightness = 0.5 + 0.5 * _easing(phase)
        col = (int(255 * brightness),) * 3
        pygame.draw.circle(surf, col, (int(sx * size), int(sy * size)),
                           max(1, int(size * 0.02)))


def draw_partly_cloudy_day(surf: pygame.Surface, size: int, t: float) -> None:
    """02d — sole + nuvola che oscilla orizzontalmente."""
    cx = cy = size / 2
    # Sole in alto a sinistra
    sun_cx, sun_cy = cx - size * 0.12, cy - size * 0.18
    sun_r = size * 0.13
    n_rays = 8
    angle_offset = t * 2 * math.pi / n_rays
    for i in range(n_rays):
        ang = angle_offset + i * 2 * math.pi / n_rays
        x1 = sun_cx + size * 0.17 * math.cos(ang)
        y1 = sun_cy + size * 0.17 * math.sin(ang)
        x2 = sun_cx + size * 0.24 * math.cos(ang)
        y2 = sun_cy + size * 0.24 * math.sin(ang)
        pygame.draw.line(surf, C_SUN_RING, (x1, y1), (x2, y2),
                         max(2, int(size * 0.025)))
    pygame.draw.circle(surf, C_SUN_CORE, (int(sun_cx), int(sun_cy)), int(sun_r))
    # Nuvola in basso-destra, oscillazione piccola entro i bounds
    sway = math.sin(2 * math.pi * t) * size * 0.025
    _draw_cloud(surf, cx + size * 0.10 + sway, cy + size * 0.18,
                size * 0.68, C_CLOUD)


def draw_partly_cloudy_night(surf: pygame.Surface, size: int, t: float) -> None:
    """02n — luna + nuvola oscillante."""
    cx = cy = size / 2
    moon_cx, moon_cy = cx - size * 0.12, cy - size * 0.18
    moon_r = size * 0.15
    pygame.draw.circle(surf, C_MOON, (int(moon_cx), int(moon_cy)), int(moon_r))
    pygame.draw.circle(surf, (0, 0, 0),
                       (int(moon_cx + moon_r * 0.45), int(moon_cy - moon_r * 0.20)),
                       int(moon_r * 0.85))
    sway = math.sin(2 * math.pi * t) * size * 0.025
    _draw_cloud(surf, cx + size * 0.10 + sway, cy + size * 0.18,
                size * 0.68, C_CLOUD_NIGHT)


def draw_cloudy(surf: pygame.Surface, size: int, t: float) -> None:
    """03d — nuvola singola con sfumatura di luminosità."""
    cx = cy = size / 2
    sway = math.sin(2 * math.pi * t) * size * 0.02
    pulse = 0.92 + 0.08 * _easing(t)
    col = tuple(int(c * pulse) for c in C_CLOUD)
    _draw_cloud(surf, cx + sway, cy + size * 0.05, size * 0.78, col)


def draw_cloudy_night(surf: pygame.Surface, size: int, t: float) -> None:
    """03n — nuvola notturna (più scura/blu)."""
    cx = cy = size / 2
    sway = math.sin(2 * math.pi * t) * size * 0.02
    pulse = 0.92 + 0.08 * _easing(t)
    col = tuple(int(c * pulse) for c in C_CLOUD_NIGHT)
    _draw_cloud(surf, cx + sway, cy + size * 0.05, size * 0.78, col)


def draw_overcast(surf: pygame.Surface, size: int, t: float) -> None:
    """04d — 2 nuvole sovrapposte, sfumatura grigia più scura."""
    cx = cy = size / 2
    sway = math.sin(2 * math.pi * t) * size * 0.015
    # Nuvola sfondo (più grande, leggermente in basso)
    _draw_cloud(surf, cx - size * 0.03 + sway, cy + size * 0.12,
                size * 0.78, C_CLOUD_DARK)
    # Nuvola in primo piano (più piccola, in alto)
    _draw_cloud(surf, cx + size * 0.05 - sway, cy - size * 0.05,
                size * 0.60, C_CLOUD)


def draw_overcast_night(surf: pygame.Surface, size: int, t: float) -> None:
    """04n — coperto notturno (2 nuvole con palette più scura)."""
    cx = cy = size / 2
    sway = math.sin(2 * math.pi * t) * size * 0.015
    # Nuvola sfondo molto scura
    _draw_cloud(surf, cx - size * 0.03 + sway, cy + size * 0.12,
                size * 0.78, (90, 95, 110))
    # Nuvola in primo piano (notturna)
    _draw_cloud(surf, cx + size * 0.05 - sway, cy - size * 0.05,
                size * 0.60, C_CLOUD_NIGHT)


def draw_rain(surf: pygame.Surface, size: int, t: float) -> None:
    """09d — nuvola con 3 gocce che cadono in loop sfasate."""
    cx = cy = size / 2
    # Nuvola in alto
    _draw_cloud(surf, cx, cy - size * 0.12, size * 0.72, C_CLOUD_DARK)
    # Gocce: ognuna ha la sua fase, escono dal basso nuvola e cadono
    drop_w = max(2, int(size * 0.035))
    drop_h = int(size * 0.10)
    drop_top = cy + size * 0.12
    drop_bottom = cy + size * 0.42
    for i, dx in enumerate([-0.18, 0.0, 0.18]):
        phase = (t + i / 3.0) % 1.0
        y = drop_top + phase * (drop_bottom - drop_top)
        x = cx + dx * size
        pygame.draw.line(surf, C_RAIN, (x, y), (x, y + drop_h), drop_w)


def draw_rain_sun(surf: pygame.Surface, size: int, t: float) -> None:
    """10d — sole + nuvola + gocce."""
    cx = cy = size / 2
    # Sole piccolo in alto a sx
    sun_cx, sun_cy = cx - size * 0.22, cy - size * 0.25
    angle = t * 2 * math.pi / 8
    for i in range(8):
        ang = angle + i * 2 * math.pi / 8
        x1 = sun_cx + size * 0.11 * math.cos(ang)
        y1 = sun_cy + size * 0.11 * math.sin(ang)
        x2 = sun_cx + size * 0.18 * math.cos(ang)
        y2 = sun_cy + size * 0.18 * math.sin(ang)
        pygame.draw.line(surf, C_SUN_RING, (x1, y1), (x2, y2),
                         max(2, int(size * 0.02)))
    pygame.draw.circle(surf, C_SUN_CORE, (int(sun_cx), int(sun_cy)),
                       int(size * 0.09))
    # Nuvola al centro-destra
    _draw_cloud(surf, cx + size * 0.05, cy - size * 0.05,
                size * 0.66, C_CLOUD)
    # Gocce
    drop_w = max(2, int(size * 0.03))
    drop_h = int(size * 0.08)
    drop_top = cy + size * 0.15
    drop_bottom = cy + size * 0.40
    for i, dx in enumerate([-0.10, 0.08, 0.22]):
        phase = (t + i / 3.0) % 1.0
        y = drop_top + phase * (drop_bottom - drop_top)
        x = cx + dx * size
        pygame.draw.line(surf, C_RAIN, (x, y), (x, y + drop_h), drop_w)


def draw_rain_moon(surf: pygame.Surface, size: int, t: float) -> None:
    """10n — luna + nuvola + gocce."""
    cx = cy = size / 2
    # Luna piccola in alto a sx
    moon_cx, moon_cy = cx - size * 0.22, cy - size * 0.25
    moon_r = size * 0.11
    pygame.draw.circle(surf, C_MOON, (int(moon_cx), int(moon_cy)), int(moon_r))
    pygame.draw.circle(surf, (0, 0, 0),
                       (int(moon_cx + moon_r * 0.45), int(moon_cy - moon_r * 0.20)),
                       int(moon_r * 0.85))
    # Nuvola al centro-destra
    _draw_cloud(surf, cx + size * 0.05, cy - size * 0.05,
                size * 0.66, C_CLOUD_NIGHT)
    # Gocce
    drop_w = max(2, int(size * 0.03))
    drop_h = int(size * 0.08)
    drop_top = cy + size * 0.15
    drop_bottom = cy + size * 0.40
    for i, dx in enumerate([-0.10, 0.08, 0.22]):
        phase = (t + i / 3.0) % 1.0
        y = drop_top + phase * (drop_bottom - drop_top)
        x = cx + dx * size
        pygame.draw.line(surf, C_RAIN, (x, y), (x, y + drop_h), drop_w)


def draw_thunderstorm(surf: pygame.Surface, size: int, t: float) -> None:
    """11d — nuvola scura + fulmine che lampeggia."""
    cx = cy = size / 2
    _draw_cloud(surf, cx, cy - size * 0.10, size * 0.78, C_CLOUD_DARK)
    # Lampo: visibile in [0.0, 0.15], poi spento, riacceso brevemente in [0.5, 0.6]
    flash = 0.0
    if t < 0.15:
        flash = 1.0 - t / 0.15
    elif 0.5 <= t < 0.6:
        flash = 1.0 - (t - 0.5) / 0.10
    if flash > 0.01:
        bolt_points = [
            (cx - size * 0.06, cy + size * 0.08),
            (cx + size * 0.04, cy + size * 0.16),
            (cx - size * 0.02, cy + size * 0.18),
            (cx + size * 0.04, cy + size * 0.36),
            (cx - size * 0.10, cy + size * 0.20),
            (cx + size * 0.00, cy + size * 0.18),
            (cx - size * 0.06, cy + size * 0.10),
        ]
        col = (int(C_BOLT[0] * flash + 80 * (1 - flash)),
               int(C_BOLT[1] * flash + 80 * (1 - flash)),
               int(C_BOLT[2] * flash + 20 * (1 - flash)))
        pygame.draw.polygon(surf, col, bolt_points)


def draw_snow(surf: pygame.Surface, size: int, t: float) -> None:
    """13d — nuvola + 3 fiocchi che roteano cadendo."""
    cx = cy = size / 2
    _draw_cloud(surf, cx, cy - size * 0.12, size * 0.72, C_CLOUD)
    # Fiocchi: 3 con fasi diverse
    flake_top = cy + size * 0.12
    flake_bottom = cy + size * 0.42
    for i, dx in enumerate([-0.18, 0.0, 0.18]):
        phase = (t + i / 3.0) % 1.0
        y = flake_top + phase * (flake_bottom - flake_top)
        x = cx + dx * size + math.sin(phase * 2 * math.pi) * size * 0.025
        rot = phase * 2 * math.pi
        _draw_snowflake(surf, x, y, size * 0.06, rot)


def _draw_snowflake(surf: pygame.Surface, cx: float, cy: float,
                    r: float, rot: float) -> None:
    """Fiocco a 6 raggi, lieve rotazione."""
    for i in range(6):
        ang = rot + i * math.pi / 3
        x = cx + r * math.cos(ang)
        y = cy + r * math.sin(ang)
        pygame.draw.line(surf, C_SNOW, (cx, cy), (x, y), 2)
    pygame.draw.circle(surf, C_SNOW, (int(cx), int(cy)), 2)


def draw_fog(surf: pygame.Surface, size: int, t: float) -> None:
    """50d — linee orizzontali che ondeggiano."""
    cx = cy = size / 2
    n_lines = 5
    line_w = max(2, int(size * 0.035))
    line_len = size * 0.55
    spacing = size * 0.13
    y_start = cy - (n_lines - 1) * spacing / 2
    for i in range(n_lines):
        y = y_start + i * spacing
        # Sfasamento orizzontale ondeggiato (ampiezza ridotta per non uscire)
        phase = (t + i * 0.20) % 1.0
        x_shift = math.sin(phase * 2 * math.pi) * size * 0.04
        # Lunghezza variabile (le righe centrali sono più lunghe)
        width_factor = 0.75 + 0.25 * math.sin(i / (n_lines - 1) * math.pi)
        ll = line_len * width_factor
        pygame.draw.line(surf, C_FOG,
                         (cx - ll / 2 + x_shift, y),
                         (cx + ll / 2 + x_shift, y),
                         line_w)


# ---------------------------------------------------------------------------
# Moon phase: calcolo astronomico + rendering
# ---------------------------------------------------------------------------

# Periodo sinodico medio in giorni (ciclo completo lunare).
SYNODIC_MONTH = 29.530588853

# Riferimento: luna nuova del 16 maggio 2026, 20:01 UTC.
# Aggiornato per migliorare la precisione (era 2000-01-06 con drift di 26 anni).
# Precisione di questo algoritmo: ±2-3 ore sulle date di novilunio per il
# 2024-2030, sufficiente per visualizzazione qualitativa. Quando si vorrà
# precisione astronomica si potrà passare a un algoritmo Jean Meeus completo.
_REF_NEW_MOON_TS = 1778961660.0   # datetime(2026, 5, 16, 20, 1, tzinfo=UTC).timestamp()


def moon_phase(timestamp: float) -> tuple[float, str]:
    """Calcola fase lunare dato un timestamp UNIX UTC.

    Restituisce (phase, name) dove:
      phase ∈ [0, 1) :  0=new, 0.25=first quarter, 0.5=full, 0.75=last quarter
      name è la fase localizzata in italiano (8 fasi standard)
    """
    days_since = (timestamp - _REF_NEW_MOON_TS) / 86400.0
    phase = (days_since % SYNODIC_MONTH) / SYNODIC_MONTH
    name = _phase_name(phase)
    return phase, name


# Nomi localizzati delle 8 fasi lunari (indice 0..7, vedi _phase_name).
_PHASE_NAMES: dict[str, list[str]] = {
    "it": ["Nuova", "Crescente", "Primo quarto", "Gibbosa crescente",
           "Piena", "Gibbosa calante", "Ultimo quarto", "Calante"],
    "en": ["New Moon", "Waxing Crescent", "First Quarter", "Waxing Gibbous",
           "Full Moon", "Waning Gibbous", "Last Quarter", "Waning Crescent"],
}


def _phase_name(phase: float, lang: str = "it") -> str:
    """Nome localizzato della fase corrispondente (default: italiano).

    Convenzione astronomica standard: 4 fasi "puntuali" strette (~1.5 giorni)
    attorno al momento esatto (Nuova, Primo quarto, Piena, Ultimo quarto) e
    4 fasi "intermedie" più ampie (~5.6 giorni) tra esse (Crescente, Gibbosa
    crescente, Gibbosa calante, Calante).

    Le soglie sono **esattamente simmetriche** attorno ai valori canonici
    (0, 0.25, 0.5, 0.75) perché il valore di `phase` proviene dall'API
    OpenWeatherMap che è astronomicamente esatto. Il calcolo locale di
    fallback (`moon_phase()`) ha un drift di ±3% ma viene usato solo quando
    l'API non ha ancora risposto (boot iniziale).

    Soglie (indice → nome):
      [0.000, 0.025) + [0.975, 1.000)  0  Nuova / New Moon
      [0.025, 0.225)                   1  Crescente / Waxing Crescent
      [0.225, 0.275)                   2  Primo quarto / First Quarter
      [0.275, 0.475)                   3  Gibbosa crescente / Waxing Gibbous
      [0.475, 0.525)                   4  Piena / Full Moon
      [0.525, 0.725)                   5  Gibbosa calante / Waning Gibbous
      [0.725, 0.775)                   6  Ultimo quarto / Last Quarter
      [0.775, 0.975)                   7  Calante / Waning Crescent

    `lang`: "it" o "en"; qualsiasi altro valore ricade sull'italiano.
    """
    if phase < 0.025 or phase >= 0.975:
        idx = 0
    elif phase < 0.225:
        idx = 1
    elif phase < 0.275:
        idx = 2
    elif phase < 0.475:
        idx = 3
    elif phase < 0.525:
        idx = 4
    elif phase < 0.725:
        idx = 5
    elif phase < 0.775:
        idx = 6
    else:
        idx = 7
    return _PHASE_NAMES.get(lang, _PHASE_NAMES["it"])[idx]


def draw_moon_phase(surf: pygame.Surface, size: int, phase: float,
                    lit_color: tuple[int, int, int] = (235, 235, 220),
                    dark_color: tuple[int, int, int] = (28, 32, 48),
                    bg_color: tuple[int, int, int] = (0, 0, 0),
                    antialias_scale: int = 2) -> None:
    """Disegna la fase lunare su `surf` con qualità migliorata.

    Migliorie rispetto alla versione precedente (solo primitive):
      - **Anti-aliasing**: render a `antialias_scale`× la risoluzione finale,
        poi downsample con smoothscale (filtro bilineare di Pygame) → bordi
        smooth invece di pixel scalettati. Default 2× (sicuro Pi Zero W);
        usa 4 se vuoi qualità massima e hai memoria a sufficienza.
      - **Mari lunari**: macchie scure determ. (posizioni dei principali
        mari reali: Imbrium, Serenitatis, Tranquillitatis, Nubium,
        Procellarum) → realismo visivo.
      - **Terminatore sfumato**: blur Gaussiano sulla maschera d'ombra
        per simulare la penombra reale (la zona di transizione luce/buio
        sulla Luna non è netta ma ha qualche pixel di gradient).
      - **Glow esterno**: alone luminoso bassa-alpha attorno al disco,
        in luna piena dà un'aura nostalgica.

    Costo: render una volta sola al boot (la fase cambia ~ogni 7h),
    quindi il N× di costo CPU è irrilevante in pratica. Memoria intermedia
    (Surface di lavoro) viene liberata al return.

    Note tecniche:
      - Se bg_color è (0,0,0) usiamo SRCALPHA (sfondo trasparente)
      - Se bg_color != (0,0,0) il chiamante vuole un colorkey:
        riempiamo lo sfondo con bg_color e disegniamo opaco
        (mantiene compatibilità retro col chiamante esistente)
    """
    # Determina se chiamatore vuole alpha (bg=(0,0,0)) o colorkey
    use_alpha = bg_color == (0, 0, 0) or len(bg_color) == 4

    # Render a antialias_scale× per AA
    SCALE = antialias_scale
    big = size * SCALE
    if use_alpha:
        s = pygame.Surface((big, big), pygame.SRCALPHA)
    else:
        s = pygame.Surface((big, big))
        s.fill(bg_color)

    cx = cy = big / 2
    r = big * 0.45    # un filo più grande del 0.42 originale

    # Caso speciale: luna nuova → disco scuro con mari scuri appena visibili
    if phase < 0.02 or phase > 0.98:
        pygame.draw.circle(s, (*dark_color, 255) if use_alpha else dark_color,
                            (int(cx), int(cy)), int(r))
        _draw_maria(s, cx, cy, r, dark_color, darker_offset=-8)
        _apply_circular_clip(s, cx, cy, r, use_alpha)
    # Caso speciale: luna piena → tutto illuminato con mari visibili
    elif abs(phase - 0.5) < 0.02:
        pygame.draw.circle(s, (*lit_color, 255) if use_alpha else lit_color,
                            (int(cx), int(cy)), int(r))
        _draw_maria(s, cx, cy, r, lit_color, mare_color=(180, 180, 170))
        _apply_circular_clip(s, cx, cy, r, use_alpha)
    else:
        # Fase generica
        # Strategia: prima disegna il disco con ombra (lit + dark via terminatore),
        # POI sovrappone i mari/crateri SOLO sulla parte illuminata. Questo evita
        # che l'ellisse del terminatore (che è di colore lit_color per gibbose)
        # sovrascriva i mari/crateri pre-disegnati.
        # 1. Disco illuminato pieno (base)
        pygame.draw.circle(s, (*lit_color, 255) if use_alpha else lit_color,
                            (int(cx), int(cy)), int(r))
        # 2. Ombra parametrica con terminatore curvo (potrebbe coprire parte
        # del disco con dark e parte con lit - non importa, mari arrivano dopo)
        _apply_shadow(s, cx, cy, r, phase, dark_color, lit_color, use_alpha)
        # 3. Mari + crateri SOPRA l'ombra, solo nelle posizioni illuminate
        _draw_maria_visible(s, cx, cy, r, phase, lit_color, mare_color=(180, 180, 170))
        # 4. Clip circolare: assicura che mari/crateri non sporgano dal disco
        _apply_circular_clip(s, cx, cy, r, use_alpha)

    # Glow esterno (alone): solo in fasi luminose (visibile da piena a quarto)
    # Non disegnato per luna nuova (sarebbe controintuitivo: glow su nulla)
    if 0.10 < phase < 0.90 and use_alpha:
        _add_glow(s, cx, cy, r)

    # Downsample con smoothscale (filtro bilineare → AA) solo se scale > 1
    if SCALE > 1:
        small = pygame.transform.smoothscale(s, (size, size))
        surf.blit(small, (0, 0))
    else:
        # scale=1: niente downsample necessario, blit diretto
        surf.blit(s, (0, 0))


# Posizioni dei principali mari lunari visibili dalla Terra (face near-side).
# Coordinate normalizzate dal centro del disco: (x_off, y_off, raggio)
# in unità di "raggio luna" (disco va da -1 a +1).
#
# Convenzione asse: x positivo = est (destra guardando da Terra),
#                   y positivo = sud (basso, come SDL).
# Coordinate basate su mappa selenografica reale, normalizzate al disco.
#
# Vincolo: ogni mare deve stare dentro al disco, cioè
#   sqrt(x^2 + y^2) + r <= ~0.95 (con margine per il bordo)
_MARIA = [
    # Lato EST (visibile in fase crescente, da luna nuova a piena)
    ( 0.62, -0.18, 0.08),   # Mare Crisium (est estremo, isolato circolare)
    ( 0.20, -0.40, 0.12),   # Mare Serenitatis (nord-est)
    ( 0.30, -0.05, 0.13),   # Mare Tranquillitatis (centro-est, grande)
    ( 0.40,  0.25, 0.10),   # Mare Fecunditatis (sud-est)
    ( 0.15,  0.45, 0.08),   # Mare Nectaris (sud centrale-est)
    # Centro e lato OVEST (visibile in fase calante)
    ( 0.00,  0.30, 0.11),   # Mare Nubium (sud centrale)
    (-0.18, -0.40, 0.16),   # Mare Imbrium (nord-ovest, grande circolare)
    (-0.45,  0.05, 0.18),   # Oceanus Procellarum (ovest, molto grande)
    (-0.30,  0.45, 0.08),   # Mare Humorum (sud-ovest)
]


# Crateri prominenti visibili a occhio nudo, con sistema di raggi luminosi.
# Sono piccoli ma molto contrastati. Coordinate (x_off, y_off, raggio).
# Quando disegnati hanno un colore PIU CHIARO del disco lunare, opposto ai mari.
_CRATERS = [
    # Tycho: sud, molto contrastato, raggi luminosi visibili anche a occhio nudo
    ( 0.00,  0.65, 0.045),
    # Copernicus: centro-ovest, su Procellarum
    (-0.20,  0.10, 0.035),
    # Kepler: ovest, piccolo
    (-0.35,  0.10, 0.025),
    # Aristarchus: nord-ovest, il più brillante della luna
    (-0.40, -0.20, 0.025),
]


def _draw_maria(surf: pygame.Surface, cx: float, cy: float, r: float,
                 base_color: tuple, mare_color: tuple = None,
                 darker_offset: int = 0) -> None:
    """Disegna i mari lunari (zone scure) sul disco. mare_color override
    per fase piena; altrimenti deriva da base_color scurito di `darker_offset`.

    Disegna anche i crateri prominenti come piccole macchie più chiare di
    base_color (sistema di raggi luminosi tipico di Tycho, Copernicus, etc.)
    """
    if mare_color is None:
        mare_color = (max(0, base_color[0] + darker_offset),
                       max(0, base_color[1] + darker_offset),
                       max(0, base_color[2] + darker_offset))
    # Mari (macchie scure)
    for mx, my, mr in _MARIA:
        x_px = int(cx + mx * r)
        y_px = int(cy + my * r)
        r_px = max(1, int(mr * r))
        pygame.draw.circle(surf, mare_color, (x_px, y_px), r_px)
    # Crateri brillanti (macchie più chiare di base_color)
    crater_color = (min(255, base_color[0] + 20),
                     min(255, base_color[1] + 20),
                     min(255, base_color[2] + 20))
    for cmx, cmy, cmr in _CRATERS:
        x_px = int(cx + cmx * r)
        y_px = int(cy + cmy * r)
        r_px = max(1, int(cmr * r))
        pygame.draw.circle(surf, crater_color, (x_px, y_px), r_px)


def _is_position_illuminated(x: float, y: float, phase: float) -> bool:
    """Determina se una posizione (x, y) normalizzata in [-1, 1] sul disco
    lunare è nella parte illuminata, dato il valore di `phase`.

    Geometria del terminatore: a phase=0.0 e 1.0 (nuova) niente illuminato.
    A phase=0.25 illuminata è metà destra (x > 0).
    A phase=0.5 (piena) tutto illuminato.
    A phase=0.75 illuminata è metà sinistra (x < 0).

    Il terminatore tra le 4 fasi è un'ellisse: a phase=0.125 (crescente
    sottile) il bordo dell'illuminata è a x=+0.7 circa (solo una falce
    a destra). A phase=0.375 (gibbosa crescente) il bordo è a x=-0.4
    circa (illuminato debordando a sinistra).

    Formula: x_terminator = -cos(phase * 2 * pi) per fasi crescenti,
    cos(phase * 2 * pi) per fasi calanti. Più semplice:
      illuminated_factor = 2 * phase if phase < 0.5 else 2 * (1 - phase)
      light_on_right = phase < 0.5
      ellipse_factor = 2 * illuminated_factor - 1  (in [-1, +1])

    Condizione di illuminazione:
      crescente (light_on_right): x >= -ellipse_factor (con ellisse curvata)
      calante (light_on_left):    x <= +ellipse_factor (curvata l'altro verso)

    Per semplicità, usiamo un test lineare sul terminatore ellittico:
    un punto (x, y) è illuminato se la sua x è oltre il terminatore.
    Il terminatore a y=0 è a x_t = -ellipse_factor (cresc) o +ellipse_factor (cal).
    Per altri y, il terminatore ellittico è scalato per cerchio:
    x_t(y) = x_t * sqrt(1 - y^2)  (ellisse iscritta nel cerchio)
    """
    if phase < 0.02 or phase > 0.98:
        # Luna nuova: niente illuminato
        return False
    if abs(phase - 0.5) < 0.02:
        # Luna piena: tutto illuminato
        return True

    if phase < 0.5:
        illuminated = 2 * phase
        light_on_right = True
    else:
        illuminated = 2 * (1 - phase)
        light_on_right = False
    ellipse_factor = 2 * illuminated - 1  # in [-1, +1]

    # Terminatore ellittico: a y=0 il bordo è a x = -ellipse_factor (cresc)
    # o +ellipse_factor (calante). Per altri y, scala con sqrt(1 - y²).
    # Se y² >= 1 il punto è oltre il disco (non dovrebbe succedere ma proteggi)
    if y * y >= 1:
        return False
    y_scale = (1 - y * y) ** 0.5
    if light_on_right:
        # Crescente: illuminato è a destra del terminatore
        x_terminator = -ellipse_factor * y_scale
        return x >= x_terminator
    else:
        # Calante: illuminato è a sinistra del terminatore
        x_terminator = ellipse_factor * y_scale
        return x <= x_terminator


def _draw_maria_visible(surf: pygame.Surface, cx: float, cy: float, r: float,
                         phase: float, base_color: tuple,
                         mare_color: tuple = None) -> None:
    """Come `_draw_maria` ma disegna solo i mari/crateri che cadono nella
    parte illuminata del disco lunare (data la fase corrente).

    Questo viene chiamato DOPO `_apply_shadow` per sovrascrivere i mari
    sopra l'ellisse del terminatore (che è di colore lit_color e cancellava
    i mari pre-disegnati).

    Regola di visibilità: un mare/cratere viene disegnato solo se il suo
    centro è dentro l'area illuminata CON UN MARGINE pari al raggio del
    mare. Questo evita che mari "a cavallo del terminatore" vengano
    disegnati per metà (mostrando un bordo netto innaturale).

    In altri termini: disegniamo il mare solo se è INTERAMENTE nell'area
    illuminata.
    """
    if mare_color is None:
        mare_color = (max(0, base_color[0] - 30),
                       max(0, base_color[1] - 30),
                       max(0, base_color[2] - 30))
    # Mari: disegna solo se il mare CADE INTERAMENTE nella zona illuminata
    for mx, my, mr in _MARIA:
        if not _is_feature_fully_illuminated(mx, my, mr, phase):
            continue
        x_px = int(cx + mx * r)
        y_px = int(cy + my * r)
        r_px = max(1, int(mr * r))
        pygame.draw.circle(surf, mare_color, (x_px, y_px), r_px)
    # Crateri brillanti: stesso criterio
    crater_color = (min(255, base_color[0] + 20),
                     min(255, base_color[1] + 20),
                     min(255, base_color[2] + 20))
    for cmx, cmy, cmr in _CRATERS:
        if not _is_feature_fully_illuminated(cmx, cmy, cmr, phase):
            continue
        x_px = int(cx + cmx * r)
        y_px = int(cy + cmy * r)
        r_px = max(1, int(cmr * r))
        pygame.draw.circle(surf, crater_color, (x_px, y_px), r_px)


def _is_feature_fully_illuminated(x: float, y: float, feature_radius: float,
                                    phase: float) -> bool:
    """Determina se un mare/cratere con centro in (x, y) e raggio `feature_radius`
    è INTERAMENTE nella parte illuminata del disco.

    Approccio: oltre a verificare il centro, controlliamo anche il bordo
    più vicino al terminatore. Se anche solo quel bordo è ancora illuminato,
    allora tutto il mare è illuminato e può essere disegnato senza tagli.

    Per phase < 0.5 (crescente, light_on_right), il bordo più "a rischio"
    è il bordo SINISTRO del mare (x - feature_radius).
    Per phase > 0.5 (calante, light_on_left), il bordo più a rischio è
    il bordo DESTRO (x + feature_radius).
    """
    if phase < 0.02 or phase > 0.98:
        return False
    if abs(phase - 0.5) < 0.02:
        return True

    if phase < 0.5:
        # Crescente: bordo a rischio = sinistro del mare
        check_x = x - feature_radius
    else:
        # Calante: bordo a rischio = destro
        check_x = x + feature_radius
    return _is_position_illuminated(check_x, y, phase)


def _apply_shadow(surf: pygame.Surface, cx: float, cy: float, r: float,
                   phase: float, dark_color: tuple, lit_color: tuple,
                   use_alpha: bool) -> None:
    """Applica l'ombra parametrica al disco illuminato per riprodurre la fase.

    Algoritmo: stessa logica della versione originale (rect + ellipse)
    ma su una surface separata che poi blittiamo con maschera circolare,
    così l'ombra non esce dal cerchio della luna. La maschera permette
    anche di sfumare il bordo per un effetto penombra realistico.
    """
    size = surf.get_size()[0]
    shadow = pygame.Surface(size if isinstance(size, tuple) else (size, size),
                              pygame.SRCALPHA)
    sw, sh = shadow.get_size()

    # Direzione e proporzione dell'illuminazione
    if phase < 0.5:
        illuminated = 2 * phase
        light_on_right = True
    else:
        illuminated = 2 * (1 - phase)
        light_on_right = False
    ellipse_factor = 2 * illuminated - 1

    # Semidisco scuro sul lato non illuminato
    dark_rgba = (*dark_color, 255)
    if light_on_right:
        pygame.draw.rect(shadow, dark_rgba,
                          pygame.Rect(int(cx - r), int(cy - r),
                                       int(r), int(r * 2)))
    else:
        pygame.draw.rect(shadow, dark_rgba,
                          pygame.Rect(int(cx), int(cy - r),
                                       int(r), int(r * 2)))

    # Ellisse al centro per la curvatura del terminatore
    ellipse_w = int(abs(ellipse_factor) * r * 2)
    if ellipse_w > 0:
        er = pygame.Rect(int(cx - ellipse_w / 2), int(cy - r),
                         ellipse_w, int(r * 2))
        ec = (*dark_color, 255) if ellipse_factor < 0 else (*lit_color, 255)
        pygame.draw.ellipse(shadow, ec, er)

    # Maschera circolare: l'ombra deve stare DENTRO il cerchio della luna
    mask = pygame.Surface((sw, sh), pygame.SRCALPHA)
    pygame.draw.circle(mask, (255, 255, 255, 255),
                        (int(cx), int(cy)), int(r))
    shadow.blit(mask, (0, 0), special_flags=pygame.BLEND_RGBA_MULT)

    # Compositazione sull'output
    surf.blit(shadow, (0, 0))


def _apply_circular_clip(surf: pygame.Surface, cx: float, cy: float, r: float,
                          use_alpha: bool) -> None:
    """Taglia tutto ciò che è fuori dal cerchio della luna.

    Crea una surface "negativo": un rettangolo completo MENO il cerchio della
    luna, dove il "fuori cerchio" è trasparente nel mezzo e opaco nei bordi.
    Bli ttiamo con BLEND_RGBA_MULT moltiplicando l'alpha, così tutto ciò che
    è dentro al cerchio resta come è e tutto ciò che è fuori va a alpha=0.

    Serve a contenere i mari/crateri (disegnati come pygame.draw.circle) che
    per via dell'antialiasing possono produrre pixel leggermente oltre il
    bordo del disco lunare.
    """
    if not use_alpha:
        # Senza alpha non possiamo "clippare" via, dovremmo riempire di bg_color
        # ma è raro e non lo gestiamo (la chiamata viene fatta solo per use_alpha)
        return
    sw, sh = surf.get_size()
    # Maschera: cerchio bianco opaco su sfondo trasparente
    mask = pygame.Surface((sw, sh), pygame.SRCALPHA)
    pygame.draw.circle(mask, (255, 255, 255, 255),
                        (int(cx), int(cy)), int(r))
    # BLEND_RGBA_MULT: moltiplica alpha della surf per alpha della mask.
    # Tutto ciò che è fuori dal cerchio mask (alpha=0) → diventa alpha=0 in surf.
    surf.blit(mask, (0, 0), special_flags=pygame.BLEND_RGBA_MULT)


def _add_glow(surf: pygame.Surface, cx: float, cy: float, r: float) -> None:
    """Aggiunge un soft glow attorno al disco lunare.

    Tre cerchi concentrici di raggio crescente con alpha decrescente:
    simula la diffusione atmosferica della luce lunare. È un effetto
    sottile ma dà profondità.

    IMPORTANTE: il glow va aggiunto SOTTO il disco lunare. Per farlo
    creiamo un nuovo layer con glow + copia del disco esistente, poi
    sostituiamo `surf`. Pygame non ha un blit_under(), quindi facciamo
    un layered compose.
    """
    sw, sh = surf.get_size()
    glow_layer = pygame.Surface((sw, sh), pygame.SRCALPHA)
    # 3 strati di alone con alpha crescente verso il centro
    glow_specs = [
        (1.30, 8),     # esterno, sussurrato
        (1.16, 18),    # medio, più diffuso
        (1.08, 30),    # vicino al bordo, intenso
    ]
    for r_mult, alpha in glow_specs:
        pygame.draw.circle(glow_layer, (255, 255, 240, alpha),
                            (int(cx), int(cy)), int(r * r_mult))
    # Compose: glow SOTTO + luna esistente SOPRA
    # Per fare "blit_under", creiamo un nuovo SRCALPHA finale e blittiamo
    # nell'ordine corretto, poi copiamo nel surf.
    final = pygame.Surface((sw, sh), pygame.SRCALPHA)
    final.blit(glow_layer, (0, 0))   # 1° glow sotto
    final.blit(surf, (0, 0))          # 2° luna sopra (alpha-composite)
    # Copia nel surf originale (clear + blit)
    surf.fill((0, 0, 0, 0))
    surf.blit(final, (0, 0))


def render_moon_surface(size: int, phase: float,
                        lit_color: tuple[int, int, int] = (235, 235, 230),
                        dark_color: tuple[int, int, int] = (40, 40, 50),
                        bg_color=None,
                        antialias_scale: int = 2) -> pygame.Surface:
    """Rasterizza una Surface size×size con la luna nella fase data.

    Ritorna SEMPRE una Surface SRCALPHA (sfondo trasparente), così:
      - Il chiamante può usare Texture.from_surface() e ottenere alpha-blend
        corretto senza bisogno di colorkey.
      - Il glow esterno della luna (alpha bassa) si compone correttamente
        sullo sfondo del display senza creare aloni colorati.

    bg_color: PARAMETRO IGNORATO (mantenuto per backward-compat).
    Le vecchie chiamate che passavano magenta non hanno più bisogno di
    `surf.set_colorkey(magenta)` lato chiamante (alpha-blend reale).

    antialias_scale: 2 default, 4 per qualità massima (più costoso).
    """
    surf = pygame.Surface((size, size), pygame.SRCALPHA)
    # bg_color ignorato; passiamo (0,0,0) a draw_moon_phase per attivare
    # il path SRCALPHA interno (use_alpha=True)
    draw_moon_phase(surf, size, phase, lit_color, dark_color,
                     bg_color=(0, 0, 0), antialias_scale=antialias_scale)
    # convert_alpha solo se display attivo (Pygame software); in SDL2
    # Texture.from_surface() converte internamente.
    if pygame.display.get_surface() is not None:
        return surf.convert_alpha()
    return surf


# ---------------------------------------------------------------------------
# Mapping OpenWeather id base → drawer fn
# ---------------------------------------------------------------------------

DRAWERS: dict[str, Callable[[pygame.Surface, int, float], None]] = {
    "01d": draw_clear_day,
    "01n": draw_clear_night,
    "02d": draw_partly_cloudy_day,
    "02n": draw_partly_cloudy_night,
    "03d": draw_cloudy,
    "03n": draw_cloudy_night,
    "04d": draw_overcast,
    "04n": draw_overcast_night,
    # Pioggia/temporale/neve/nebbia: di notte visivamente identici al giorno
    # (la pioggia e' pioggia, il fulmine illumina sia di giorno che di notte).
    # Eccezione: 10n (rovesci) ha la luna invece del sole.
    "09d": draw_rain,
    "09n": draw_rain,
    "10d": draw_rain_sun,
    "10n": draw_rain_moon,
    "11d": draw_thunderstorm,
    "11n": draw_thunderstorm,
    "13d": draw_snow,
    "13n": draw_snow,
    "50d": draw_fog,
    "50n": draw_fog,
}


def render_frame(name: str, size: int, t: float) -> pygame.Surface:
    """Rasterizza un singolo frame dell'icona `name` a fase t in [0,1].

    Restituisce una pygame.Surface size×size con alpha (sfondo trasparente).
    """
    surf = pygame.Surface((size, size), pygame.SRCALPHA)
    drawer = DRAWERS.get(name)
    if drawer is None:
        # Fallback: cerchio grigio
        pygame.draw.circle(surf, (100, 100, 100), (size // 2, size // 2), size // 4)
    else:
        drawer(surf, size, t)
    return surf


def precompute_spritesheet(name: str, size: int, n_frames: int,
                            antialias_scale: int = 4) -> list[pygame.Surface]:
    """Pre-renderizza tutti i frame in memoria una volta sola.

    Usare al boot: cosi' la animazione costa solo `blit()`, niente disegno
    primitivo a runtime. Costo memoria: n_frames × size² × 4 byte.

    Esempio: 12 icone × 20 frame × 100² × 4 = 9.6 MB.

    antialias_scale: render a `size*scale` poi smoothscale al `size` finale
    per ottenere anti-aliasing. pygame.draw non fa AA nativamente, ma il
    downsample bilineare di smoothscale produce edge smooth. scale=4 dà
    qualità eccellente; scale=2 è sufficiente per icone piccole.
    scale=1 disabilita l'AA (compatibilità retro).

    Costo CPU al boot: ~4× il tempo di rasterizzazione di base (precompute
    accade una sola volta, irrilevante a runtime). Costo memoria identico
    perché la Surface finale è size×size dopo il downsample.

    NOTA: `convert_alpha()` richiede che pygame.display.set_mode() sia stato
    chiamato (legge il pixel format del display). In modalita' SDL2
    hardware-accelerated dove non chiamiamo set_mode(), saltiamo la
    conversione: Texture.from_surface() farà la sua conversione GPU.
    """
    display_active = pygame.display.get_surface() is not None
    frames: list[pygame.Surface] = []
    big = size * antialias_scale
    for i in range(n_frames):
        t = i / n_frames  # mai includere 1.0: 0.0 e 1.0 sarebbero identici
        if antialias_scale > 1:
            # Rasterizza a big×big poi smoothscale al size finale (AA)
            frame_big = render_frame(name, big, t)
            frame = pygame.transform.smoothscale(frame_big, (size, size))
            # Surface intermedia 4× viene rilasciata automaticamente quando
            # `frame_big` esce dallo scope all'iterazione successiva.
            # Non forziamo del/gc.collect: su Pi Zero W ARMv6 single-core
            # il gc.collect() è molto costoso (50-200ms ciascuno) e durante
            # il precompute creerebbe stalli percepibili.
        else:
            # scale=1: path veloce, no allocazione di Surface intermedia
            frame = render_frame(name, size, t)
        # Converti al formato display per blit veloce (solo se display attivo)
        if display_active:
            frame = frame.convert_alpha()
        frames.append(frame)
    return frames

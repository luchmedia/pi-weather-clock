#!/bin/bash
# download-meteocons.sh
# Scarica le icone Meteocons (di Bas Milius) dalla repo v2 e le converte
# in PNG con i nomi attesi dal weather_clock (codici OWM 01d, 01n, ...).
#
# REPO: https://github.com/basmilius/meteocons (branch v2)
#
# Requisiti:
#   - bash, curl, ImageMagick (convert) o rsvg-convert
#
# Installazione su Raspbian/Debian:
#   sudo apt install imagemagick   # OR  sudo apt install librsvg2-bin
#
# Uso:
#   bash download-meteocons.sh                 # scarica in ./theme_meteocons/
#   bash download-meteocons.sh /path/output    # scarica in path custom
#   bash download-meteocons.sh /path/output 200  # specifica anche dimensione PNG

set -e

OUT_DIR="${1:-./theme_meteocons}"
ICON_SIZE="${2:-200}"   # PNG size; il codice ridimensiona poi a icon_size
STYLE="${3:-fill}"      # fill | line | monochrome

BASE_URL="https://raw.githubusercontent.com/basmilius/meteocons/v2/production/${STYLE}/svg"

mkdir -p "$OUT_DIR"

# Verifica converter disponibile
CONVERTER=""
if command -v rsvg-convert >/dev/null 2>&1; then
    CONVERTER="rsvg"
elif command -v magick >/dev/null 2>&1; then
    CONVERTER="magick"
elif command -v convert >/dev/null 2>&1; then
    CONVERTER="convert"
else
    echo "ERRORE: nessun converter SVG→PNG trovato."
    echo "Installa con: sudo apt install librsvg2-bin"
    echo "Oppure:       sudo apt install imagemagick"
    exit 1
fi
echo "Converter: $CONVERTER, dimensione: ${ICON_SIZE}px, stile: ${STYLE}"
echo "Output:    $OUT_DIR"
echo

# Mapping OWM code → Meteocons SVG filename (senza .svg)
# Vedi https://openweathermap.org/weather-conditions per significato OWM codes
declare -A MAPPING=(
    # Clear sky
    ["01d"]="clear-day"
    ["01n"]="clear-night"
    # Few clouds (11-25%)
    ["02d"]="partly-cloudy-day"
    ["02n"]="partly-cloudy-night"
    # Scattered clouds (25-50%)
    ["03d"]="cloudy"
    ["03n"]="cloudy"
    # Broken / overcast clouds (>50%)
    ["04d"]="overcast-day"
    ["04n"]="overcast-night"
    # Shower rain (drizzle/scattered)
    ["09d"]="partly-cloudy-day-drizzle"
    ["09n"]="partly-cloudy-night-drizzle"
    # Rain
    ["10d"]="partly-cloudy-day-rain"
    ["10n"]="partly-cloudy-night-rain"
    # Thunderstorm
    ["11d"]="thunderstorms-day"
    ["11n"]="thunderstorms-night"
    # Snow
    ["13d"]="partly-cloudy-day-snow"
    ["13n"]="partly-cloudy-night-snow"
    # Mist/Fog
    ["50d"]="mist"
    ["50n"]="mist"
)

# Convert helper
convert_svg() {
    local input="$1"
    local output="$2"
    local size="$3"
    case "$CONVERTER" in
        rsvg)
            rsvg-convert -w "$size" -h "$size" -o "$output" "$input"
            ;;
        magick)
            magick -background none -density 300 -resize "${size}x${size}" "$input" "$output"
            ;;
        convert)
            convert -background none -density 300 -resize "${size}x${size}" "$input" "$output"
            ;;
    esac
}

# Download + convert ogni icona
TMPDIR=$(mktemp -d)
trap "rm -rf $TMPDIR" EXIT

success=0
fail=0
for owm in "${!MAPPING[@]}"; do
    meteo="${MAPPING[$owm]}"
    svg_url="${BASE_URL}/${meteo}.svg"
    svg_file="$TMPDIR/${meteo}.svg"
    png_file="$OUT_DIR/${owm}.png"

    printf "  %-4s ← %-32s  " "$owm" "$meteo"

    # Download SVG (riusa se già scaricato in questo run)
    if [ ! -s "$svg_file" ]; then
        if ! curl -fsSL "$svg_url" -o "$svg_file"; then
            echo "[DOWNLOAD FAILED]"
            fail=$((fail + 1))
            continue
        fi
    fi

    # Convert to PNG
    if convert_svg "$svg_file" "$png_file" "$ICON_SIZE" 2>/dev/null; then
        echo "[OK]"
        success=$((success + 1))
    else
        echo "[CONVERT FAILED]"
        fail=$((fail + 1))
    fi
done

echo
echo "Completato: $success successi, $fail fallimenti"
echo
echo "File creati in $OUT_DIR:"
ls -la "$OUT_DIR"

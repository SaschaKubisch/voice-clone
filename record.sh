#!/usr/bin/env bash
# Record the microphone to a mono 48 kHz 16-bit WAV with ffmpeg (avfoundation).
# Usage: bash record.sh [OUTPUT_PATH] [SECONDS]
#   OUTPUT_PATH  default: voices/me.wav (relative to this script's folder)
#   SECONDS      default: 20
set -euo pipefail

FFMPEG=/opt/homebrew/bin/ffmpeg
FFPROBE=/opt/homebrew/bin/ffprobe
DEVICE="MacBook Air Microphone"

cd "$(dirname "$0")"

OUT="${1:-voices/me.wav}"
SECONDS_TO_RECORD="${2:-20}"

case "$SECONDS_TO_RECORD" in
  ''|*[!0-9]*) echo "error: seconds must be a positive integer, got '$SECONDS_TO_RECORD'" >&2; exit 2 ;;
esac
if [ "$SECONDS_TO_RECORD" -lt 1 ]; then
  echo "error: seconds must be at least 1" >&2
  exit 2
fi

mkdir -p voices "$(dirname "$OUT")"

printf 'Recording starts in 3'
sleep 1
printf '..2'
sleep 1
printf '..1\n'
sleep 1
echo "Speak now (${SECONDS_TO_RECORD} s)"

"$FFMPEG" -hide_banner -loglevel error -y \
  -f avfoundation -i ":${DEVICE}" \
  -t "$SECONDS_TO_RECORD" \
  -ac 1 -ar 48000 -c:a pcm_s16le \
  "$OUT"

DURATION="$("$FFPROBE" -v error -show_entries format=duration -of csv=p=0 "$OUT")"
echo "Done."
echo "File:     $OUT"
echo "Duration: ${DURATION} s"

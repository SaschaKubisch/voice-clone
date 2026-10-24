#!/usr/bin/env bash
# Record the microphone with ffmpeg (avfoundation), then cut the silence at the start and
# the end to about 0.5 s. Pauses inside the speech stay. Output: mono 48 kHz 16-bit WAV.
# Usage: bash record.sh [OUTPUT_PATH] [SECONDS]
#   OUTPUT_PATH  default: voices/me.wav (relative to this script's folder)
#   SECONDS      default: 100 (voice-script.md takes about 1.5 minutes to read)
# Ctrl-C ends the recording early; what was recorded so far is kept.
set -euo pipefail

FFMPEG=/opt/homebrew/bin/ffmpeg
FFPROBE=/opt/homebrew/bin/ffprobe
DEVICE="MacBook Air Microphone"

cd "$(dirname "$0")"

OUT="${1:-voices/me.wav}"
SECONDS_TO_RECORD="${2:-100}"

case "$SECONDS_TO_RECORD" in
  ''|*[!0-9]*) echo "error: seconds must be a positive integer, got '$SECONDS_TO_RECORD'" >&2; exit 2 ;;
esac
if [ "$SECONDS_TO_RECORD" -lt 1 ]; then
  echo "error: seconds must be at least 1" >&2
  exit 2
fi

# Same filter as app.py (TRIM_FILTER). The ends are padded with 0.5 s first, so a recording
# with less silence still gets some. The end is trimmed by reversing the audio, trimming its
# start, and reversing it back.
TRIM_START="silenceremove=start_periods=1:start_duration=0.1:start_threshold=-50dB:start_silence=0.5"
TRIM_FILTER="adelay=500:all=1,apad=pad_dur=0.5,${TRIM_START},areverse,${TRIM_START},areverse"

mkdir -p voices "$(dirname "$OUT")"

RAW="$(mktemp "${TMPDIR:-/tmp}/record-raw.XXXXXX")"
TRIMMED="$(mktemp "${TMPDIR:-/tmp}/record-trimmed.XXXXXX")"
trap 'rm -f "$RAW" "$TRIMMED"' EXIT

duration() {
  "$FFPROBE" -v error -show_entries format=duration -of csv=p=0 "$1" 2>/dev/null || echo 0
}

echo "Read voice-script.md in one take: all ten sections, a short pause between sections,"
echo "and stop about one second after the last word. Silence at both ends is cut afterwards."
echo "The transcript is the spoken text of voice-script.md (see README.md)."
echo
printf 'Recording starts in 3'
sleep 1
printf '..2'
sleep 1
printf '..1\n'
sleep 1
echo "Speak now (${SECONDS_TO_RECORD} s, Ctrl-C to stop earlier)"

# A handled SIGINT keeps this script alive after Ctrl-C; ffmpeg finishes the file itself.
trap ':' INT
"$FFMPEG" -hide_banner -loglevel error -y \
  -f avfoundation -i ":${DEVICE}" \
  -t "$SECONDS_TO_RECORD" \
  -ac 1 -ar 48000 -c:a pcm_s16le -f wav \
  "$RAW" || echo "Recording ended early (ffmpeg exit code $?)." >&2
trap - INT

RAW_SECONDS="$(duration "$RAW")"
if ! awk -v d="$RAW_SECONDS" 'BEGIN { exit !(d > 0) }'; then
  echo "error: nothing was recorded" >&2
  exit 1
fi

"$FFMPEG" -hide_banner -loglevel error -y -i "$RAW" \
  -af "$TRIM_FILTER" -ac 1 -ar 48000 -c:a pcm_s16le -f wav "$TRIMMED"

DURATION="$(duration "$TRIMMED")"
if ! awk -v d="$DURATION" 'BEGIN { exit !(d >= 1) }'; then
  echo "error: no speech found, less than 1 s is left after the silence is cut. Check the microphone." >&2
  exit 1
fi
cat "$TRIMMED" > "$OUT"

echo "Done."
echo "File:     $OUT"
echo "Recorded: ${RAW_SECONDS} s"
echo "Saved:    ${DURATION} s (silence at both ends cut to about 0.5 s)"

#!/bin/bash
# Helper of the Voice Clone launcher. The AppleScript applet calls it with one command per call.
# It is copied into "Voice Clone.app/Contents/Resources/" by make-app.sh, next to a file
# "repo-path" that holds the folder of the app (fixed at build time).
#
# Commands (exit 0 means yes / done):
#   begin          URL answers: print "running". Otherwise start the server and print "started <pid>".
#                  Exit 10: repo folder or app.py missing. 11: .venv/bin/python missing. 12: could not start.
#   check          exit 0 if the URL answers
#   alive <pid>    exit 0 if the process exists
#   stop <pid>     SIGTERM, SIGKILL after 10 s. Only stops "<repo>/.venv/bin/python app.py"; any other
#                  process is left alone (exit 3). A process that is already gone counts as stopped.
#   url | logpath | repo    print the URL, the log file or the repo folder
#
# Environment (for tests; the app itself sets none of them):
#   VOICECLONE_PORT (default 7860)  port polled by check/begin. The real app.py always serves 7860.
#   VOICECLONE_REPO                 repo folder instead of the one in "repo-path"
#   VOICECLONE_LOG                  log file instead of ~/Library/Logs/VoiceClone/app.log

set -u

HERE="$(cd "$(dirname "$0")" && pwd)"
PORT="${VOICECLONE_PORT:-7860}"
URL="http://127.0.0.1:${PORT}/"
LOG="${VOICECLONE_LOG:-$HOME/Library/Logs/VoiceClone/app.log}"
STOP_GRACE_STEPS=50   # 50 steps of 0.2 s = 10 s between SIGTERM and SIGKILL

if [ -n "${VOICECLONE_REPO:-}" ]; then
  REPO="$VOICECLONE_REPO"
elif [ -f "$HERE/repo-path" ]; then
  REPO="$(cat "$HERE/repo-path")"
else
  REPO=""
fi

check() {
  /usr/bin/curl -sf --max-time 2 -o /dev/null "$URL"
}

alive() {
  case "${1:-}" in ''|*[!0-9]*) return 1 ;; esac
  kill -0 "$1" 2>/dev/null
}

preflight() {
  [ -n "$REPO" ] && [ -d "$REPO" ] && [ -f "$REPO/app.py" ] || return 10
  [ -x "$REPO/.venv/bin/python" ] || return 11
  return 0
}

rotate_log() {
  local size
  size="$(stat -f %z "$LOG" 2>/dev/null || echo 0)"
  if [ "$size" -gt 5242880 ]; then
    mv -f "$LOG" "$LOG.1"
  fi
}

# Starts the server detached from the caller and prints its pid.
# BROWSER=/usr/bin/true makes the Python webbrowser module run "true <url>", so app.py's
# inbrowser=True opens nothing; the applet opens the URL itself, once.
spawn() {
  preflight || return $?
  mkdir -p "$(dirname "$LOG")" || return 12
  rotate_log
  echo "=== $(date '+%Y-%m-%d %H:%M:%S') start: $REPO/.venv/bin/python app.py (port $PORT)" >>"$LOG" || return 12
  cd "$REPO" || return 12
  PATH="/opt/homebrew/bin:/usr/local/bin:$PATH" PYTHONUNBUFFERED=1 BROWSER=/usr/bin/true \
    nohup "$REPO/.venv/bin/python" app.py >>"$LOG" 2>&1 </dev/null &
  local pid=$!
  disown "$pid" 2>/dev/null
  echo "$pid"
}

begin() {
  if check; then
    echo "running"
    return 0
  fi
  local pid
  pid="$(spawn)" || return $?
  echo "started $pid"
}

stop() {
  local pid="${1:-}" cmd i
  case "$pid" in ''|*[!0-9]*) return 2 ;; esac
  kill -0 "$pid" 2>/dev/null || return 0
  cmd="$(ps -o command= -p "$pid" 2>/dev/null)"
  cmd="${cmd#"${cmd%%[![:space:]]*}"}"
  [ "$cmd" = "$REPO/.venv/bin/python app.py" ] || return 3
  kill -TERM "$pid" 2>/dev/null
  i=0
  while [ "$i" -lt "$STOP_GRACE_STEPS" ]; do
    kill -0 "$pid" 2>/dev/null || return 0
    sleep 0.2
    i=$((i + 1))
  done
  kill -KILL "$pid" 2>/dev/null
  return 0
}

case "${1:-}" in
  begin)   begin ;;
  check)   check ;;
  alive)   alive "${2:-}" ;;
  stop)    stop "${2:-}" ;;
  url)     echo "$URL" ;;
  logpath) echo "$LOG" ;;
  repo)    echo "$REPO" ;;
  *)       echo "usage: voiceclone.sh begin|check|alive <pid>|stop <pid>|url|logpath|repo" >&2; exit 2 ;;
esac

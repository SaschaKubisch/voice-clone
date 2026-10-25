#!/bin/bash
# Server-logic test of the desktop app binary, with no window. It runs "VoiceClone --self-test <phase>"
# from macapp/build/Voice Clone.app (build it first: bash make-app.sh) against stand-in servers. It never
# touches port 7860: it needs VOICECLONE_PORT set to another port and refuses 7860.
#   VOICECLONE_PORT=7862 bash macapp/test-desktop.sh
# Phases: reuse a running server, start its own, stop only its own, time out, notice a server that exits,
# missing repo folder, missing .venv, and whether WebKit treats http://127.0.0.1 as a secure context (needed
# by the microphone). The window code (menus, microphone prompt, downloads, uploads) is not tested here.
set -u

PORT="${VOICECLONE_PORT:-}"
if [ -z "$PORT" ] || [ "$PORT" = 7860 ] || [ "$PORT" = 7861 ]; then
  echo "Set VOICECLONE_PORT to a free port other than 7860, for example VOICECLONE_PORT=7862 (7861 is used by tests/test_progress_ui.py)." >&2
  exit 2
fi
export VOICECLONE_PORT="$PORT"
OTHER=$((PORT + 1))

HERE="$(cd "$(dirname "$0")" && pwd -P)"
BIN="$HERE/build/Voice Clone.app/Contents/MacOS/VoiceClone"
REAL_PYTHON="$HERE/../.venv/bin/python"
[ -x "$BIN" ] || { echo "No desktop build at $BIN. Run: bash make-app.sh" >&2; exit 2; }
for p in "$PORT" "$OTHER"; do
  if /usr/bin/curl -s --max-time 2 -o /dev/null "http://127.0.0.1:$p/"; then
    echo "Port $p answers already. Stop that server or choose another VOICECLONE_PORT." >&2; exit 2
  fi
done

T="$(mktemp -d)"
FAILS=0
PIDS=""
cleanup() {
  for p in $PIDS; do kill -KILL "$p" 2>/dev/null; done
  rm -rf "$T"
}
trap cleanup EXIT

ok()   { echo "ok    $1"; }
fail() { echo "FAIL  $1"; FAILS=$((FAILS + 1)); }
expect() {  # description, then a command: passes when it exits 0
  local d="$1"; shift
  if "$@"; then ok "$d"; else fail "$d"; fi
}
answers() { /usr/bin/curl -sf --max-time 2 -o /dev/null "http://127.0.0.1:$1/"; }
stand_in() {  # port: a server that is not started by the app (python -m http.server)
  mkdir -p "$T/ext"
  ( cd "$T/ext" && exec "$REAL_PYTHON" -m http.server "$1" --bind 127.0.0.1 >/dev/null 2>&1 ) &
  local pid=$!
  disown "$pid"; PIDS="$PIDS $pid"
  local i=0
  while [ $i -lt 50 ]; do answers "$1" && break; sleep 0.2; i=$((i + 1)); done
  echo "$pid"
}
make_repo() {  # name, app.py content: a repo whose .venv/bin/python is the real interpreter
  mkdir -p "$T/$1/.venv/bin"
  ln -s "$(readlink "$REAL_PYTHON" || echo "$REAL_PYTHON")" "$T/$1/.venv/bin/python"
  printf '%s\n' "$2" > "$T/$1/app.py"
}
selftest() {  # phase, repo folder
  VOICECLONE_REPO="$2" VOICECLONE_LOG="$T/app.log" "$BIN" --self-test "$1"
}

make_repo serving 'import http.server, os
class H(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200); self.end_headers(); self.wfile.write(b"ok")
    def log_message(self, *a):
        pass
http.server.HTTPServer(("127.0.0.1", int(os.environ["VOICECLONE_PORT"])), H).serve_forever()'
make_repo silent 'import time
time.sleep(3600)'
make_repo exits 'raise SystemExit(1)'
mkdir -p "$T/novenv"; touch "$T/novenv/app.py"

echo "binary $BIN"
echo "ports $PORT and $OTHER"

# 1. Reuse: a server that is not the app's answers on the port. The app starts nothing and stops nothing.
EXT="$(stand_in "$PORT")"
expect "stand-in answers on $PORT" answers "$PORT"
selftest reuse "$T/serving"; RC=$?
expect "reuse: self-test exits 0 (got $RC)" test "$RC" = 0
expect "reuse: the stand-in process still runs" kill -0 "$EXT"
expect "reuse: the stand-in still answers" answers "$PORT"
kill -TERM "$EXT" 2>/dev/null
i=0; while answers "$PORT" && [ $i -lt 25 ]; do sleep 0.2; i=$((i + 1)); done
expect "reuse: port $PORT is free again" bash -c "! /usr/bin/curl -s --max-time 2 -o /dev/null http://127.0.0.1:$PORT/"

# 2. Own server: the app starts it, uses it, stops it. A second stand-in on $OTHER must survive.
EXT2="$(stand_in "$OTHER")"
selftest own "$T/serving"; RC=$?
expect "own: self-test exits 0 (got $RC)" test "$RC" = 0
expect "own: port $PORT is free after the app stopped its server" bash -c "! /usr/bin/curl -s --max-time 2 -o /dev/null http://127.0.0.1:$PORT/"
expect "own: the other server on $OTHER was not touched" kill -0 "$EXT2"
expect "own: the other server on $OTHER still answers" answers "$OTHER"
kill -TERM "$EXT2" 2>/dev/null

# 3. The microphone API needs a secure context. A WKWebView without a window loads the stand-in's page.
mkdir -p "$T/ext"
echo '<html><body>stand-in</body></html>' > "$T/ext/index.html"
EXT3="$(stand_in "$PORT")"
selftest secure-context "$T/serving"; RC=$?
expect "secure-context: self-test exits 0 (got $RC)" test "$RC" = 0
kill -TERM "$EXT3" 2>/dev/null
i=0; while answers "$PORT" && [ $i -lt 25 ]; do sleep 0.2; i=$((i + 1)); done

# 4. A server that never answers, a server that exits, missing folders.
selftest timeout "$T/silent"; RC=$?;  expect "timeout: self-test exits 0 (got $RC)" test "$RC" = 0
selftest died "$T/exits"; RC=$?;      expect "died: self-test exits 0 (got $RC)" test "$RC" = 0
selftest fail10 "$T/missing"; RC=$?;  expect "fail10: self-test exits 0 (got $RC)" test "$RC" = 0
selftest fail11 "$T/novenv"; RC=$?;   expect "fail11: self-test exits 0 (got $RC)" test "$RC" = 0

# 5. Nothing left behind.
expect "no server process left" bash -c "! pgrep -f '$T/[a-z]*/.venv/bin/python app.py' >/dev/null"
expect "port $PORT is free at the end" bash -c "! /usr/bin/curl -s --max-time 2 -o /dev/null http://127.0.0.1:$PORT/"

echo
if [ "$FAILS" = 0 ]; then echo "All checks passed."; else echo "$FAILS check(s) failed."; fi
exit "$FAILS"

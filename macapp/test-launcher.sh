#!/bin/bash
# Dry run of the launcher helper (macapp/voiceclone.sh) against a stand-in server. It never touches
# port 7860: it needs VOICECLONE_PORT set to another port and refuses 7860.
#   VOICECLONE_PORT=7862 bash macapp/test-launcher.sh
# Cases: start, already running, quit stops only its own server, SIGKILL after 10 s, missing venv and repo.
set -u

PORT="${VOICECLONE_PORT:-}"
if [ -z "$PORT" ] || [ "$PORT" = 7860 ]; then
  echo "Set VOICECLONE_PORT to a free port other than 7860, for example VOICECLONE_PORT=7862." >&2
  exit 2
fi
export VOICECLONE_PORT="$PORT"

HERE="$(cd "$(dirname "$0")" && pwd -P)"
HELPER="$HERE/voiceclone.sh"
REAL_PYTHON="$HERE/../.venv/bin/python"
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
gone() { ! kill -0 "$1" 2>/dev/null; }
wait_for_url() {  # up to 20 s
  local i=0
  while [ $i -lt 100 ]; do bash "$HELPER" check && return 0; sleep 0.2; i=$((i + 1)); done
  return 1
}

# Fake repo: .venv/bin/python is the real interpreter, app.py serves "ok" on VOICECLONE_PORT.
REPO="$T/repo"
mkdir -p "$REPO/.venv/bin" "$T/ext"
ln -s "$(readlink "$REAL_PYTHON" || echo "$REAL_PYTHON")" "$REPO/.venv/bin/python"
cat > "$REPO/app.py" <<'PY'
import http.server, os, signal, sys
print("fake app start, BROWSER=%s" % os.environ.get("BROWSER"), flush=True)
if os.environ.get("FAKE_IGNORE_TERM"):
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
class H(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200); self.end_headers(); self.wfile.write(b"ok")
    def log_message(self, *a):
        pass
http.server.HTTPServer(("127.0.0.1", int(os.environ["VOICECLONE_PORT"])), H).serve_forever()
PY
export VOICECLONE_REPO="$REPO"
export VOICECLONE_LOG="$T/logs/app.log"

echo "port $PORT, repo $REPO"
expect "port $PORT is free before the test" bash -c "! bash '$HELPER' check"

# 1. Start: nothing answers, so begin starts the server.
OUT="$(bash "$HELPER" begin)"
case "$OUT" in "started "[0-9]*) ok "begin prints 'started <pid>' ($OUT)" ;; *) fail "begin printed '$OUT'" ;; esac
OWN="${OUT#started }"; PIDS="$PIDS $OWN"
expect "server answers on $PORT within 20 s" wait_for_url
expect "alive reports the pid" bash "$HELPER" alive "$OWN"
expect "the process is '<repo>/.venv/bin/python app.py'" test "$(ps -o command= -p "$OWN" | sed 's/^ *//')" = "$REPO/.venv/bin/python app.py"
expect "log file written under the log path" grep -q "fake app start" "$VOICECLONE_LOG"
expect "BROWSER=/usr/bin/true reached the server" grep -q "BROWSER=/usr/bin/true" "$VOICECLONE_LOG"
expect "the log has the start line" grep -q "=== .* start: $REPO/.venv/bin/python app.py" "$VOICECLONE_LOG"

# 2. Already running: begin starts nothing.
BEFORE="$(pgrep -f "$REPO/.venv/bin/python app.py" | sort | tr '\n' ' ')"
OUT="$(bash "$HELPER" begin)"
AFTER="$(pgrep -f "$REPO/.venv/bin/python app.py" | sort | tr '\n' ' ')"
expect "begin prints 'running' when the URL answers" test "$OUT" = "running"
expect "no second server process" test "$BEFORE" = "$AFTER"

# 3. Quit stops only its own server. An external server (not started by begin) runs on another port.
EXT_PORT=$((PORT + 1))
( cd "$T/ext" && exec "$REPO/.venv/bin/python" -m http.server "$EXT_PORT" --bind 127.0.0.1 >/dev/null 2>&1 ) &
EXT=$!; disown "$EXT"; PIDS="$PIDS $EXT"
sleep 1
expect "external stand-in answers on $EXT_PORT" /usr/bin/curl -sf --max-time 2 -o /dev/null "http://127.0.0.1:$EXT_PORT/"
bash "$HELPER" stop "$EXT"; RC=$?
expect "stop refuses a process that is not '<repo>/.venv/bin/python app.py' (exit 3, got $RC)" test "$RC" = 3
expect "the external server is still running after the refused stop" kill -0 "$EXT"
bash "$HELPER" stop "abc" 2>/dev/null; RC=$?
expect "stop refuses a pid that is not a number (exit 2, got $RC)" test "$RC" = 2
bash "$HELPER" stop "$OWN"; RC=$?
expect "stop of the own server exits 0" test "$RC" = 0
expect "the own server is gone" gone "$OWN"
expect "port $PORT is free after stop" bash -c "! bash '$HELPER' check"
expect "the external server is still running after the own stop" kill -0 "$EXT"
bash "$HELPER" stop "$OWN"; RC=$?
expect "stop of an already-gone pid exits 0" test "$RC" = 0
kill -TERM "$EXT" 2>/dev/null

# 4. A server that ignores SIGTERM is killed after 10 s.
OUT="$(FAKE_IGNORE_TERM=1 bash "$HELPER" begin)"; STUCK="${OUT#started }"; PIDS="$PIDS $STUCK"
wait_for_url
START=$(date +%s)
bash "$HELPER" stop "$STUCK"; RC=$?
ELAPSED=$(( $(date +%s) - START ))
expect "stuck server: stop exits 0 (got $RC)" test "$RC" = 0
expect "stuck server is gone" gone "$STUCK"
expect "SIGKILL came after about 10 s, not earlier (took ${ELAPSED} s)" test "$ELAPSED" -ge 9 -a "$ELAPSED" -le 13

# 5. A server that exits at once (for example port busy): alive turns false.
OUT="$(VOICECLONE_PORT=notaport bash "$HELPER" begin)"; DEAD="${OUT#started }"; PIDS="$PIDS $DEAD"
sleep 2
expect "alive reports a server that exited" bash -c "! bash '$HELPER' alive '$DEAD'"

# 6. Missing venv and missing repo.
mkdir -p "$T/novenv"; touch "$T/novenv/app.py"
VOICECLONE_REPO="$T/novenv" bash "$HELPER" begin; RC=$?
expect "no .venv: begin exits 11 (got $RC)" test "$RC" = 11
VOICECLONE_REPO="$T/missing" bash "$HELPER" begin; RC=$?
expect "no repo folder: begin exits 10 (got $RC)" test "$RC" = 10

# 7. Port is free again, nothing left behind.
expect "no server process left" bash -c "! pgrep -f '$REPO/.venv/bin/python app.py' >/dev/null"
expect "port $PORT is free at the end" bash -c "! bash '$HELPER' check"

echo
if [ "$FAILS" = 0 ]; then echo "All checks passed."; else echo "$FAILS check(s) failed."; fi
exit "$FAILS"

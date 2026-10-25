#!/usr/bin/env python3
"""Checks how record.sh picks ffmpeg and the microphone, with stand-in ffmpeg and ffprobe. Nothing is recorded.

Run:  .venv/bin/python tests/test_record_sh.py [--script PATH]      # exit 0 when every check passes
      PATH is the record.sh to test; the default is the one in this repository.

The stand-ins are shell scripts in a temporary folder at the front of PATH. They log their arguments, and the
ffmpeg stand-in writes a few bytes where real ffmpeg would write audio, or fails when asked to. The real
ffmpeg is never started, so the microphone is never opened and macOS asks for no permission.

Each run of record.sh waits 3 s for its countdown, so the cases run at the same time.

Checks: ffmpeg and ffprobe are taken from PATH; the input is "default" unless VOICECLONE_MIC names another
(a name with spaces stays one argument); a failed recording prints the audio inputs (not the cameras) and
says how to set VOICECLONE_MIC; the seconds argument is still checked first.
"""
import argparse
import os
import shutil
import subprocess
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

PROJECT = Path(__file__).resolve().parent.parent

FAKE_FFMPEG = r"""#!/bin/bash
{ printf 'ffmpeg(%s)' "$0"; printf ' <%s>' "$@"; printf '\n'; } >> "$FAKE_LOG"
case "$*" in
  *-list_devices*)
    cat >&2 <<'LIST'
[AVFoundation indev @ 0x1] AVFoundation video devices:
[AVFoundation indev @ 0x1] [0] Fake Camera
[AVFoundation indev @ 0x1] AVFoundation audio devices:
[AVFoundation indev @ 0x1] [0] Fake Virtual Audio
[AVFoundation indev @ 0x1] [1] Fake Built-in Microphone
LIST
    echo "Error opening input file ." >&2
    exit 1 ;;
esac
if [[ "$*" == *"-f avfoundation"* && "${FAKE_RECORD_FAIL:-0}" = 1 ]]; then
  echo "Audio device not found" >&2
  exit 1
fi
printf 'RIFFfake' > "${@: -1}"
"""

FAKE_FFPROBE = r"""#!/bin/bash
{ printf 'ffprobe(%s)' "$0"; printf ' <%s>' "$@"; printf '\n'; } >> "$FAKE_LOG"
if [ -s "${@: -1}" ]; then echo 3.0; else echo 0; fi
"""

failures = []


def check(condition, message):
    print(("ok    " if condition else "FAIL  ") + message)
    if not condition:
        failures.append(message)


def make_tools(root):
    bin_dir = root / "bin"
    bin_dir.mkdir()
    for name, body in (("ffmpeg", FAKE_FFMPEG), ("ffprobe", FAKE_FFPROBE)):
        (bin_dir / name).write_text(body)
        (bin_dir / name).chmod(0o755)
    return bin_dir


def run_case(script, root, bin_dir, name, args, extra_env=None):
    """Run record.sh once. Returns (exit code, stdout, stderr, log text, output file)."""
    work = root / name
    work.mkdir()
    log, out = work / "calls.log", work / "out" / "voice.wav"
    env = {
        "PATH": f"{bin_dir}:/usr/bin:/bin",  # no Homebrew folder: only the stand-ins can be found first
        "HOME": str(work),
        "TMPDIR": str(work),
        "FAKE_LOG": str(log),
    }
    env.update(extra_env or {})
    done = subprocess.run(
        ["bash", str(script), str(out), *args], env=env, capture_output=True, text=True, timeout=60, cwd=work,
    )
    return done.returncode, done.stdout, done.stderr, log.read_text() if log.exists() else "", out


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--script", default=str(PROJECT / "record.sh"))
    args = parser.parse_args()
    root = Path(tempfile.mkdtemp(prefix="record-sh-test-")).resolve()
    # record.sh does `cd` to its own folder and makes voices/ there, so it runs from a copy.
    script_dir = root / "script"
    script_dir.mkdir()
    script = script_dir / "record.sh"
    shutil.copy(args.script, script)
    bin_dir = make_tools(root)
    try:
        syntax = subprocess.run(["bash", "-n", str(script)], capture_output=True, text=True)
        check(syntax.returncode == 0, f"bash -n finds no syntax error ({syntax.stderr.strip()})")

        cases = {
            "default": ("1", {}),
            "named": ("1", {"VOICECLONE_MIC": "Fake Built-in Microphone"}),
            "number": ("1", {"VOICECLONE_MIC": "1"}),
            "failing": ("1", {"FAKE_RECORD_FAIL": "1"}),
            "badseconds": ("abc", {}),
        }
        with ThreadPoolExecutor(max_workers=len(cases)) as pool:
            futures = {n: pool.submit(run_case, script, root, bin_dir, n, [secs], env) for n, (secs, env) in cases.items()}
            results = {n: f.result() for n, f in futures.items()}

        code, stdout, stderr, log, out = results["default"]
        record_lines = [ln for ln in log.splitlines() if "<avfoundation>" in ln]
        check(code == 0 and out.is_file() and out.stat().st_size > 0, f"default: the recording is saved (exit {code}, {stderr.strip()[:80]!r})")
        check(len(record_lines) == 1 and "<-i> <:default>" in record_lines[0], f"default: records from the Mac's default input ({record_lines})")
        check(f"ffmpeg({bin_dir}/ffmpeg)" in log and f"ffprobe({bin_dir}/ffprobe)" in log, "default: ffmpeg and ffprobe are the ones found on PATH")
        check("VOICECLONE_MIC" in stdout, "default: the output tells how to choose another input")

        code, stdout, stderr, log, out = results["named"]
        record_lines = [ln for ln in log.splitlines() if "<avfoundation>" in ln]
        check(code == 0 and len(record_lines) == 1 and "<-i> <:Fake Built-in Microphone>" in record_lines[0],
              f"VOICECLONE_MIC with spaces: one argument, the name ({record_lines})")

        code, stdout, stderr, log, out = results["number"]
        record_lines = [ln for ln in log.splitlines() if "<avfoundation>" in ln]
        check(code == 0 and len(record_lines) == 1 and "<-i> <:1>" in record_lines[0], f"VOICECLONE_MIC=1: the number is passed on ({record_lines})")

        code, stdout, stderr, log, out = results["failing"]
        check(code == 1 and not out.exists(), f"a failed recording exits 1 and saves nothing (exit {code})")
        check("Fake Built-in Microphone" in stderr and "[1]" in stderr and "Fake Virtual Audio" in stderr, "a failed recording lists the audio inputs")
        check("Fake Camera" not in stderr, "the list holds the audio inputs only, not the cameras")
        check("VOICECLONE_MIC" in stderr, "a failed recording says how to choose another input")
        check("default" in stderr, "a failed recording names the input that was tried")

        code, stdout, stderr, log, out = results["badseconds"]
        check(code == 2 and "seconds must be a positive integer" in stderr and "avfoundation" not in log, f"a bad seconds argument is refused first (exit {code})")
    finally:
        shutil.rmtree(root, ignore_errors=True)
    print("PASS" if not failures else f"FAILED: {len(failures)} check(s)")
    return 0 if not failures else 1


if __name__ == "__main__":
    sys.exit(main())

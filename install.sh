#!/bin/bash
# One-command installer for Voice Clone on a Mac with Apple Silicon. Safe to run again: it skips
# what is already done.
#
#   bash install.sh               tools, Python environment, models, desktop app
#   bash install.sh --browser     install the browser app instead of the desktop app
#   bash install.sh --no-app      skip the Mac app (start with start.command or .venv/bin/python app.py)
#   bash install.sh --no-models   skip the model downloads (the first Generate downloads them)
#   bash install.sh -h | --help   this text
#
# Steps: 1 checks, 2 uv and ffmpeg (Homebrew installs a missing one), 3 Python environment (.venv),
# 4 models (VoxCPM2 and the denoiser, downloaded but not loaded), 5 Mac app (make-app.sh --install).
set -euo pipefail
set -E

usage() { awk 'NR > 1 && /^#/ { sub(/^# ?/, ""); print; next } NR > 1 { exit }' "$0"; }

APP=1
APP_MODE=desktop
MODELS=1
while [ $# -gt 0 ]; do
  case "$1" in
    --browser) APP_MODE=browser ;;
    --no-app) APP=0 ;;
    --no-models) MODELS=0 ;;
    -h|--help) usage; exit 0 ;;
    *) usage >&2; exit 2 ;;
  esac
  shift
done

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
cd "$HERE"
VENV="$HERE/.venv"
PY="$VENV/bin/python"
RESULTS=""

STEP_NO=0
STEP_NAME="start"
step() { STEP_NO="$1"; STEP_NAME="$2"; printf '\n[%s/5] %s\n' "$1" "$3"; }  # number, short name for errors, what it does
die() { echo "install.sh: step $STEP_NO ($STEP_NAME) failed: $*" >&2; exit 1; }
have() { command -v "$1" >/dev/null 2>&1; }
cleanup() { if [ -n "$RESULTS" ]; then rm -f "$RESULTS"; fi; }
trap cleanup EXIT
trap 'echo "install.sh: step $STEP_NO ($STEP_NAME) failed (line $LINENO). The message above says why. Fix it, then run bash install.sh again." >&2' ERR

# 1. System ---------------------------------------------------------------------------------------
step 1 "checks" "Checking macOS, Apple Silicon and this folder"
[ "$(uname -s)" = Darwin ] || die "this installer is for macOS; this system is $(uname -s)."
[ "$(uname -m)" = arm64 ] || die "Apple Silicon (arm64) is needed; this shell reports $(uname -m). On an Apple Silicon Mac that means the terminal runs under Rosetta; open one that does not."
{ [ -f app.py ] && [ -f requirements.txt ] && [ -f voxcpm_patch.py ] && [ -f make-app.sh ]; } || die "app.py, requirements.txt, voxcpm_patch.py or make-app.sh is missing in $HERE. Run this from a clone of the repository."
if [ "$APP" = 1 ]; then
  # The same two conditions make-app.sh refuses on, checked now so a failure does not come after the downloads.
  if pgrep -f 'Voice Clone\.app/Contents/MacOS/(applet|VoiceClone)' >/dev/null; then
    die "Voice Clone is running. Quit it first (Quit stops the server it started), then run this again. Or use --no-app."
  fi
  if [ "$APP_MODE" = desktop ]; then
    SWIFTC="$(xcrun --find swiftc 2>/dev/null || true)"
    SDK="$(xcrun --show-sdk-path 2>/dev/null || true)"
    { [ -n "$SWIFTC" ] && [ -d "$SDK" ]; } || die "swiftc and the macOS SDK are needed for the desktop app. Run 'xcode-select --install' (Command Line Tools), or use --browser or --no-app."
  fi
fi
echo "macOS $(sw_vers -productVersion), $(uname -m), folder $HERE"

# 2. Tools ----------------------------------------------------------------------------------------
step 2 "tools" "Checking uv and ffmpeg (a missing one is installed with Homebrew)"
MISSING=""
have uv || MISSING="$MISSING uv"
have ffmpeg || MISSING="$MISSING ffmpeg"
if [ -n "$MISSING" ]; then
  if ! have brew && [ -x /opt/homebrew/bin/brew ]; then
    eval "$(/opt/homebrew/bin/brew shellenv)"
  fi
  if have brew; then
    echo "Missing:$MISSING. Running: brew install$MISSING"
    # shellcheck disable=SC2086  # one word per tool is intended
    brew install $MISSING
    hash -r
    for tool in $MISSING; do
      have "$tool" || die "$tool is still not found after brew install."
    done
  else
    {
      echo "install.sh: step $STEP_NO ($STEP_NAME) failed: missing:$MISSING, and Homebrew (brew) is not installed."
      echo "Install Homebrew from https://brew.sh, open a new terminal, then run:"
      echo "  brew install$MISSING"
      echo "  bash install.sh"
    } >&2
    exit 1
  fi
fi
echo "uv: $(uv --version)"
echo "ffmpeg: $(ffmpeg -version 2>/dev/null | sed -n 1p)"

# 3. Python environment ---------------------------------------------------------------------------
step 3 "Python environment" "Setting up the Python 3.11 environment in .venv from requirements.txt"
venv_ok() { [ -x "$PY" ] && "$PY" -c 'import sys; sys.exit(0 if sys.version_info[:2] == (3, 11) else 1)' >/dev/null 2>&1; }
if venv_ok; then
  echo ".venv exists and runs Python 3.11, keeping it."
else
  if [ -e "$VENV" ]; then
    echo ".venv exists but does not run Python 3.11, so it is recreated."
    rm -rf "$VENV"
  fi
  uv venv --python 3.11 .venv
fi
uv pip install --python .venv/bin/python -r requirements.txt

# 4. Models ---------------------------------------------------------------------------------------
step 4 "models" "Downloading the models into the standard caches (nothing is loaded into memory)"
if [ "$MODELS" = 0 ]; then
  echo "skipped (--no-models). The first Generate downloads VoxCPM2."
else
  RESULTS="$(mktemp "${TMPDIR:-/tmp}/voiceclone-install.XXXXXX")"
  # The model ids are read from the source files that use them (no import, so no torch is loaded):
  # MODEL_ID in voxcpm_patch.py (VoxCPM2, Hugging Face) and DENOISER_ID in app.py (ZipEnhancer,
  # ModelScope; the same id is the default of voxcpm's load_denoiser=True).
  PYTHONUNBUFFERED=1 "$PY" - "$HERE/voxcpm_patch.py" "$HERE/app.py" "$RESULTS" <<'PY' || die "the model step stopped (see the message above). If the cause is the network or the free disk space, fix that and run bash install.sh again; finished files are kept."
import ast
import os
import shutil
import sys
from pathlib import Path

patch_file, app_file, results_file = sys.argv[1:4]


def constant(path, name):
    """Value of the module-level assignment `name = <literal>` in a source file, without importing it."""
    for node in ast.parse(Path(path).read_text()).body:
        if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == name for t in node.targets):
            return ast.literal_eval(node.value)
    raise SystemExit(f"{name} not found in {path}")


def fmt(n):
    n = float(n)
    for unit in ("B", "KiB", "MiB", "GiB"):
        if n < 1024 or unit == "GiB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024


def folder_size(path):
    return sum(os.path.getsize(os.path.join(d, f)) for d, _, files in os.walk(path) for f in files)


def present(root, name, size):
    """True when root/name is a file (a link to a blob counts) of exactly this size."""
    file = Path(root) / name
    return file.is_file() and file.stat().st_size == size


def free_bytes(path):
    p = Path(path)
    while not p.exists():
        p = p.parent
    return shutil.disk_usage(p).free


def huggingface_model(repo_id):
    """Download a Hugging Face repo into the standard cache, only the files that are missing."""
    from huggingface_hub import HfApi, snapshot_download
    from huggingface_hub.constants import HF_HUB_CACHE

    print(f"VoxCPM2 ({repo_id}), cache {HF_HUB_CACHE}")
    try:
        cached = snapshot_download(repo_id, local_files_only=True)
    except Exception:
        cached = None
    if cached:
        print(f"  found in cache: {cached}")
    try:
        info = HfApi().model_info(repo_id, files_metadata=True)
    except Exception as error:
        if cached:
            print(f"  Hugging Face is not reachable ({type(error).__name__}). Using the cache as it is, not verified.")
            return cached
        raise
    files = [(s.rfilename, s.size or 0) for s in info.siblings]
    total = sum(n for _, n in files)
    same_revision = bool(cached) and Path(cached).name == info.sha
    missing = [(name, n) for name, n in files if not (same_revision and present(cached, name, n))]
    if not missing:
        print(f"  complete: {len(files)} files, {fmt(total)}. Nothing to download.")
        return cached
    need = sum(n for _, n in missing)
    print(f"  {len(files)} files, {fmt(total)} in all; downloading {len(missing)} files, {fmt(need)}.")
    free = free_bytes(HF_HUB_CACHE)
    if free < need + (1 << 30):
        raise SystemExit(f"not enough free disk space for {repo_id}: {fmt(need)} needed, {fmt(free)} free in {HF_HUB_CACHE}")
    path = snapshot_download(repo_id)
    still = [name for name, n in files if not present(path, name, n)]
    if still:
        raise SystemExit(f"{repo_id}: files missing or incomplete after the download: {', '.join(still)}")
    print(f"  downloaded: {path}")
    return path


def modelscope_model(model_id):
    """Download a ModelScope repo the way voxcpm's denoiser does (modelscope pipeline: revision master)."""
    from modelscope import snapshot_download
    from modelscope.utils.constant import DEFAULT_MODEL_REVISION

    print(f"Denoiser ZipEnhancer ({model_id}), ModelScope")
    try:
        cached = snapshot_download(model_id, revision=DEFAULT_MODEL_REVISION, local_files_only=True)
    except Exception:
        cached = None
    before = folder_size(cached) if cached else 0
    if cached:
        print(f"  found in cache: {cached} ({fmt(before)}). Checking for missing files.")
    try:
        path = snapshot_download(model_id, revision=DEFAULT_MODEL_REVISION)
    except Exception as error:
        if cached:
            print(f"  ModelScope is not reachable ({type(error).__name__}). Using the cache as it is, not verified.")
            return cached
        raise
    after = folder_size(path)
    if cached and after == before:
        print(f"  complete: {fmt(after)}. Nothing new downloaded.")
    else:
        print(f"  downloaded {fmt(after - before)}: {path} ({fmt(after)})")
    return path


try:
    hf_id = constant(patch_file, "MODEL_ID")
    ms_id = constant(app_file, "DENOISER_ID")
    hf_path = huggingface_model(hf_id)
    ms_path = modelscope_model(ms_id)
    Path(results_file).write_text(f"hf_id={hf_id}\nhf={hf_path}\nms_id={ms_id}\nms={ms_path}\n")
except SystemExit:
    raise
except Exception as error:
    print(f"error: {type(error).__name__}: {error}", file=sys.stderr)
    sys.exit(1)
PY
fi

# 5. Mac app --------------------------------------------------------------------------------------
step 5 "Mac app" "Building and installing the Mac app (make-app.sh --install)"
if [ "$APP" = 0 ]; then
  echo "skipped (--no-app)."
elif [ "$APP_MODE" = browser ]; then
  bash make-app.sh --install --browser || die "make-app.sh failed. The steps before this one are done. Fix the message above, then run: bash make-app.sh --install --browser"
else
  bash make-app.sh --install || die "make-app.sh failed. The steps before this one are done. Fix the message above, then run: bash make-app.sh --install"
fi

# Summary -----------------------------------------------------------------------------------------
result() { sed -n "s/^$1=//p" "$RESULTS"; }
echo
echo "Done in $SECONDS s. Installed:"
echo "  Tools                uv, ffmpeg"
echo "  Python environment   $VENV ($("$PY" --version 2>&1), packages from requirements.txt)"
if [ "$MODELS" = 1 ]; then
  echo "  VoxCPM2              $(result hf_id) in $(result hf)"
  echo "  Denoiser             $(result ms_id) in $(result ms)"
else
  echo "  Models               skipped (--no-models); the first Generate downloads VoxCPM2"
fi
if [ "$APP" = 1 ]; then
  echo "  Mac app              /Applications/Voice Clone.app ($APP_MODE)"
else
  echo "  Mac app              skipped (--no-app)"
fi
echo
echo "Start:"
if [ "$APP" = 1 ]; then
  echo "  Open Voice Clone from Launchpad. The first start can take a minute."
  echo "  Or double-click start.command, or run: .venv/bin/python app.py"
else
  echo "  Double-click start.command, or run: .venv/bin/python app.py"
fi
echo "  The web interface is at http://127.0.0.1:7860"

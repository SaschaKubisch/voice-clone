#!/bin/bash
# Builds "Voice Clone.app" into macapp/build/. With --install, copies it to /Applications and
# registers it with Launch Services, so Launchpad lists it. Safe to run again.
#
#   bash make-app.sh                  build only
#   bash make-app.sh --install        build, then install into /Applications
#   bash make-app.sh --repo <folder>  the app starts the server from <folder> (default: the folder of this script)
set -euo pipefail

usage() { sed -n '2,7p' "$0" | sed 's/^# \{0,1\}//'; }

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
REPO="$HERE"
INSTALL=0
while [ $# -gt 0 ]; do
  case "$1" in
    --install) INSTALL=1 ;;
    --repo) shift; [ $# -gt 0 ] || { usage >&2; exit 2; }; REPO="$1" ;;
    -h|--help) usage; exit 0 ;;
    *) usage >&2; exit 2 ;;
  esac
  shift
done

[ -d "$REPO" ] || { echo "Repo folder not found: $REPO" >&2; exit 1; }
REPO="$(cd "$REPO" && pwd -P)"
[ -f "$REPO/app.py" ] || { echo "No app.py in $REPO" >&2; exit 1; }
[ -x "$REPO/.venv/bin/python" ] || echo "Warning: $REPO/.venv/bin/python is missing. The app will ask for the setup (see README) when it starts." >&2

SRC="$HERE/macapp"
BUILD="$SRC/build"
APP="$BUILD/Voice Clone.app"
RES="$APP/Contents/Resources"
PLIST="$APP/Contents/Info.plist"
DEST="/Applications/Voice Clone.app"
LSREGISTER=/System/Library/Frameworks/CoreServices.framework/Versions/A/Frameworks/LaunchServices.framework/Versions/A/Support/lsregister
PYTHON="$HERE/.venv/bin/python"

"$PYTHON" -c "import PIL" 2>/dev/null || { echo "Pillow is needed for the icon: $PYTHON -c 'import PIL' failed. Set up .venv first (see README)." >&2; exit 1; }

rm -rf "$APP" "$BUILD/VoiceClone.iconset" "$BUILD/icon-1024.png" "$BUILD/VoiceClone.icns"
mkdir -p "$BUILD"

# 1. Icon: 1024 px PNG from Pillow, the sizes of an iconset with sips, then iconutil.
"$PYTHON" "$SRC/make_icon.py" "$BUILD/icon-1024.png"
ICONSET="$BUILD/VoiceClone.iconset"
mkdir "$ICONSET"
for size in 16 32 128 256 512; do
  sips -z "$size" "$size" "$BUILD/icon-1024.png" --out "$ICONSET/icon_${size}x${size}.png" >/dev/null
  sips -z "$((size * 2))" "$((size * 2))" "$BUILD/icon-1024.png" --out "$ICONSET/icon_${size}x${size}@2x.png" >/dev/null
done
iconutil -c icns "$ICONSET" -o "$BUILD/VoiceClone.icns"

# 2. Applet: stay-open (-s) so Dock, Launchpad and Quit events reach the script; execute-only (-x)
#    so the applet never writes its state back into the bundle.
osacompile -s -x -o "$APP" "$SRC/launcher.applescript"

# 3. Helper, repo folder and icon into the bundle. The repo folder is fixed here, as plain text.
cp "$SRC/voiceclone.sh" "$RES/voiceclone.sh"
chmod 755 "$RES/voiceclone.sh"
printf '%s' "$REPO" > "$RES/repo-path"
rm -f "$RES/applet.icns" "$RES/Assets.car"
cp "$BUILD/VoiceClone.icns" "$RES/VoiceClone.icns"

# 4. Info.plist.
plist_set() {  # key type value
  /usr/libexec/PlistBuddy -c "Set :$1 $3" "$PLIST" 2>/dev/null || /usr/libexec/PlistBuddy -c "Add :$1 $2 $3" "$PLIST"
}
/usr/libexec/PlistBuddy -c "Delete :CFBundleIconName" "$PLIST" 2>/dev/null || true
plist_set CFBundleIdentifier string local.voiceclone
plist_set CFBundleName string "Voice Clone"
plist_set CFBundleDisplayName string "Voice Clone"
plist_set CFBundleIconFile string VoiceClone
plist_set CFBundleShortVersionString string 1.0
plist_set CFBundleVersion string 1
plist_set LSApplicationCategoryType string public.app-category.utilities
plist_set LSMinimumSystemVersion string 11.0
plutil -lint "$PLIST"

# 5. Ad-hoc signature. Editing the bundle broke the seal osacompile made. An arm64 app needs a valid
#    signature to launch, and the seal must match the files. "-s -" signs with no certificate:
#    enough for an app built and run on this Mac (it has no quarantine flag, so Gatekeeper does not
#    assess it). It is not notarized and would be blocked if downloaded from elsewhere.
codesign --force --deep -s - "$APP"
codesign --verify --deep --strict "$APP"
echo "Built: $APP (starts the server from $REPO)"

# 6. Install.
if [ "$INSTALL" = 1 ]; then
  if pgrep -f 'Voice Clone.app/Contents/MacOS/applet' >/dev/null; then
    echo "Voice Clone is running. Quit it first (Quit stops the server it started), then run this again." >&2
    exit 1
  fi
  rm -rf "$DEST"
  ditto "$APP" "$DEST"
  codesign --verify --deep --strict "$DEST"
  "$LSREGISTER" -f "$DEST"
  echo "Installed: $DEST"
fi

#!/bin/bash
# Builds "Voice Clone.app" into macapp/build/. With --install, copies it to /Applications, registers
# that copy with Launch Services, so Launchpad lists it, and deletes the build copy. Safe to run again.
#
#   bash make-app.sh                       build the desktop app (a window of its own, Swift and WebKit)
#   bash make-app.sh --browser             build the browser launcher (opens the page in the default browser)
#   bash make-app.sh --install             build the desktop app, then install it into /Applications
#   bash make-app.sh --install --browser   build the browser launcher, then install it
#   bash make-app.sh --repo <folder>       the app starts the server from <folder> (default: the folder of this script)
# --desktop is the default and may be given for clarity. Both variants install as
# /Applications/Voice Clone.app, so installing one replaces the other.
set -euo pipefail

usage() { awk 'NR > 1 && /^#/ { sub(/^# ?/, ""); print; next } NR > 1 { exit }' "$0"; }

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
REPO="$HERE"
INSTALL=0
MODE=desktop
while [ $# -gt 0 ]; do
  case "$1" in
    --install) INSTALL=1 ;;
    --desktop) MODE=desktop ;;
    --browser) MODE=browser ;;
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
RUNNING_PATTERN='Voice Clone\.app/Contents/MacOS/(applet|VoiceClone)'   # the browser launcher or the desktop app

# Refuse early: Quit stops the server the app started, so a running app must be quit first.
if [ "$INSTALL" = 1 ] && pgrep -f "$RUNNING_PATTERN" >/dev/null; then
  echo "Voice Clone is running (browser launcher or desktop app). Quit it first (Quit stops the server it started), then run this again." >&2
  exit 1
fi

"$PYTHON" -c "import PIL" 2>/dev/null || { echo "Pillow is needed for the icon: $PYTHON -c 'import PIL' failed. Set up .venv first (see README)." >&2; exit 1; }
if [ "$MODE" = desktop ]; then
  SWIFTC="$(xcrun --find swiftc 2>/dev/null || true)"
  SDK="$(xcrun --show-sdk-path 2>/dev/null || true)"
  [ -n "$SWIFTC" ] && [ -d "$SDK" ] || { echo "swiftc and the macOS SDK are needed for the desktop app: run 'xcode-select --install' (Command Line Tools), or build the browser launcher with --browser." >&2; exit 1; }
fi

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

plist_set() {  # key type value
  /usr/libexec/PlistBuddy -c "Set :$1 $3" "$PLIST" 2>/dev/null || /usr/libexec/PlistBuddy -c "Add :$1 $2 $3" "$PLIST"
}

if [ "$MODE" = browser ]; then
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
  /usr/libexec/PlistBuddy -c "Delete :CFBundleIconName" "$PLIST" 2>/dev/null || true
  plist_set CFBundleIdentifier string local.voiceclone
  plist_set CFBundleName string "Voice Clone"
  plist_set CFBundleDisplayName string "Voice Clone"
  plist_set CFBundleIconFile string VoiceClone
  plist_set CFBundleShortVersionString string 1.0
  plist_set CFBundleVersion string 1
  plist_set LSApplicationCategoryType string public.app-category.utilities
  plist_set LSMinimumSystemVersion string 11.0
else
  # 2. Desktop app: the Swift files in macapp/desktop/ compiled to one arm64 executable (macOS 12 or
  #    newer: the microphone permission callback of WebKit needs it). No Xcode project.
  mkdir -p "$APP/Contents/MacOS" "$RES"
  BUILD_START=$SECONDS
  "$SWIFTC" -O -sdk "$SDK" -target arm64-apple-macos12.0 -module-name VoiceClone \
    -o "$APP/Contents/MacOS/VoiceClone" "$SRC"/desktop/*.swift
  echo "Compiled the desktop app in $((SECONDS - BUILD_START)) s."

  # 3. Helper (the same file the browser launcher uses), repo folder and icon into the bundle.
  cp "$SRC/voiceclone.sh" "$RES/voiceclone.sh"
  chmod 755 "$RES/voiceclone.sh"
  printf '%s' "$REPO" > "$RES/repo-path"
  cp "$BUILD/VoiceClone.icns" "$RES/VoiceClone.icns"
  printf 'APPL????' > "$APP/Contents/PkgInfo"

  # 4. Info.plist. NSMicrophoneUsageDescription is the text of the macOS microphone prompt.
  #    NSAllowsLocalNetworking lets the window load http://127.0.0.1 (no other plain http).
  cat > "$PLIST" <<'PLIST'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
	<key>CFBundleDevelopmentRegion</key><string>en</string>
	<key>CFBundleExecutable</key><string>VoiceClone</string>
	<key>CFBundleIdentifier</key><string>local.voiceclone</string>
	<key>CFBundleInfoDictionaryVersion</key><string>6.0</string>
	<key>CFBundleName</key><string>Voice Clone</string>
	<key>CFBundleDisplayName</key><string>Voice Clone</string>
	<key>CFBundlePackageType</key><string>APPL</string>
	<key>CFBundleSignature</key><string>????</string>
	<key>CFBundleIconFile</key><string>VoiceClone</string>
	<key>CFBundleShortVersionString</key><string>1.0</string>
	<key>CFBundleVersion</key><string>1</string>
	<key>LSApplicationCategoryType</key><string>public.app-category.utilities</string>
	<key>LSMinimumSystemVersion</key><string>12.0</string>
	<key>NSHighResolutionCapable</key><true/>
	<key>NSPrincipalClass</key><string>NSApplication</string>
	<key>NSMicrophoneUsageDescription</key><string>Voice Clone records your voice in the Record voice tab. The recording stays on this Mac.</string>
	<key>NSDownloadsFolderUsageDescription</key><string>Voice Clone saves audio files that you download into your Downloads folder.</string>
	<key>NSAppTransportSecurity</key>
	<dict>
		<key>NSAllowsLocalNetworking</key><true/>
	</dict>
</dict>
</plist>
PLIST
fi
plutil -lint "$PLIST"

# 5. Ad-hoc signature. Editing the bundle broke the seal osacompile made (browser), and an arm64
#    executable needs a valid signature to launch (desktop). "-s -" signs with no certificate:
#    enough for an app built and run on this Mac (it has no quarantine flag, so Gatekeeper does not
#    assess it). It is not notarized and would be blocked if downloaded from elsewhere. No hardened
#    runtime and no sandbox, so the app needs no entitlements.
codesign --force --deep -s - "$APP"
codesign --verify --deep --strict "$APP"
echo "Built ($MODE): $APP (starts the server from $REPO)"

# 6. Launch Services. It registers a new "*.app" folder by itself, 10 to 50 s after the folder appears
#    or changes (measured in macapp/build and in macapp/build.noindex: the ".noindex" name does not
#    prevent it; a folder name that does not end in ".app" was not registered). Two registered copies with
#    the same bundle id (local.voiceclone) show up as two "Voice Clone" in Launchpad, Spotlight and
#    "Open With". So with --install the build copy is deleted once the installed copy is registered, and
#    lsregister -u then drops any registration of the deleted path (a deleted bundle stays listed until then).
#    Without --install the build stays in macapp/build, and Launch Services may list it as a second
#    "Voice Clone" if one is installed. The next run with --install removes it.
if [ "$INSTALL" = 1 ]; then
  rm -rf "$DEST"
  ditto "$APP" "$DEST"
  codesign --verify --deep --strict "$DEST"
  "$LSREGISTER" -f "$DEST"
  rm -rf "$APP"
  "$LSREGISTER" -u "$APP" 2>/dev/null || true
  echo "Installed ($MODE): $DEST (the build copy in $BUILD is deleted)"
fi

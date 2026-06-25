#!/bin/bash
# Build a downloadable macOS launcher app + .dmg for Odysseus.
#
#   ./build-macos-app.sh
#
# Produces:
#   dist/Odysseus.app   — double-click: starts the local server (using this
#                         repo's venv) and opens the UI in an app-style window.
#   dist/Odysseus.dmg   — drag-to-Applications disk image (the downloadable).
#
# This is a *launcher* wrapper: it drives the venv we set up in this repo, it
# does not bundle Python. The install path is baked into the app at build time,
# so rebuild if you move the repo. Override the port with ODYSSEUS_PORT.
set -e

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP_NAME="${APP_NAME:-Odysseus}"
BUNDLE_ID="${BUNDLE_ID:-com.odysseus.launcher}"
INSTALL_DIR="$REPO_DIR"
PORT="${ODYSSEUS_PORT:-7860}"
APP_DATA_DIR="${ODYSSEUS_APP_DATA_DIR:-}"
DIST="$REPO_DIR/dist"
APP="$DIST/$APP_NAME.app"

echo "Building $APP_NAME.app"
echo "  install dir: $INSTALL_DIR"
echo "  port:        $PORT"
[ -n "$APP_DATA_DIR" ] && echo "  data dir:    $APP_DATA_DIR"

rm -rf "$APP"
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources"

# ── Icon (best effort) — center-crop docs/odysseus.jpg to a square .icns ──
if [ -f "$REPO_DIR/docs/odysseus.jpg" ] && command -v sips >/dev/null 2>&1; then
  TMPIMG="$(mktemp -d)"
  # Center-crop to a square, scale to 512 (sips' icns encoder caps at 512), and
  # let sips emit the .icns directly — more robust across macOS versions than
  # building an .iconset by hand.
  sips -c 720 720 "$REPO_DIR/docs/odysseus.jpg" --out "$TMPIMG/sq.png" >/dev/null 2>&1 || cp "$REPO_DIR/docs/odysseus.jpg" "$TMPIMG/sq.png"
  sips -z 512 512 "$TMPIMG/sq.png" --out "$TMPIMG/icon.png" >/dev/null 2>&1
  if sips -s format icns "$TMPIMG/icon.png" --out "$APP/Contents/Resources/odysseus.icns" >/dev/null 2>&1; then
    echo "  icon:        odysseus.icns"
  else
    echo "  icon:        (skipped — conversion failed)"
  fi
  rm -rf "$TMPIMG"
else
  echo "  icon:        (skipped — no docs/odysseus.jpg)"
fi

# ── Info.plist ──
cat > "$APP/Contents/Info.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>CFBundleName</key>            <string>$APP_NAME</string>
    <key>CFBundleDisplayName</key>     <string>$APP_NAME</string>
    <key>CFBundleIdentifier</key>      <string>$BUNDLE_ID</string>
    <key>CFBundleVersion</key>         <string>1.0</string>
    <key>CFBundleShortVersionString</key><string>1.0</string>
    <key>CFBundlePackageType</key>     <string>APPL</string>
    <key>CFBundleExecutable</key>      <string>$APP_NAME</string>
    <key>CFBundleIconFile</key>        <string>odysseus</string>
    <key>LSMinimumSystemVersion</key>  <string>11.0</string>
    <key>NSHighResolutionCapable</key> <true/>
    <key>LSUIElement</key>             <true/>
</dict>
</plist>
PLIST

# ── Launcher executable (placeholders filled below) ──
cat > "$APP/Contents/MacOS/$APP_NAME.tmpl" <<'LAUNCHER'
#!/bin/bash
# Odysseus.app — start the local server and open the UI in an app window.
INSTALL_DIR="__INSTALL_DIR__"
PORT="__PORT__"
APP_DATA_DIR="__APP_DATA_DIR__"
URL="http://127.0.0.1:${PORT}"
export PATH="$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:$PATH"
if [ -n "$APP_DATA_DIR" ]; then
  export ODYSSEUS_DATA_DIR="$APP_DATA_DIR"
fi

UVICORN="$INSTALL_DIR/venv/bin/uvicorn"
LOG="$INSTALL_DIR/logs/odysseus-app.log"
PID_FILE="$INSTALL_DIR/logs/odysseus-app.pid"

notify() { /usr/bin/osascript -e "display notification \"$1\" with title \"Odysseus\"" >/dev/null 2>&1; }
die_gui() {
  /usr/bin/osascript -e "display dialog \"$1\" with title \"Odysseus\" buttons {\"OK\"} default button 1 with icon stop" >/dev/null 2>&1
  exit 1
}

[ -x "$UVICORN" ] || die_gui "Odysseus isn't set up yet. Open Terminal and run:

cd $INSTALL_DIR
python3.11 -m venv venv
./venv/bin/pip install -r requirements.txt
./venv/bin/python setup.py"

# Open the UI in its own window. Chromium browsers get app mode; Safari needs
# AppleScript because `open URL` reuses an existing browser window as a tab.
open_ui() {
  local b base exe bin
  for b in "Google Chrome" "Microsoft Edge" "Brave Browser" "Chromium"; do
    for base in "/Applications" "$HOME/Applications"; do
      if [ -d "$base/$b.app" ]; then
        exe="$(/usr/bin/defaults read "$base/$b.app/Contents/Info" CFBundleExecutable 2>/dev/null)"
        bin="$base/$b.app/Contents/MacOS/$exe"
        if [ -x "$bin" ]; then
          "$bin" --app="$URL" --new-window >/dev/null 2>&1 &
          return 0
        fi
      fi
    done
  done
  if [ -d "/Applications/Safari.app" ]; then
    /usr/bin/osascript >/dev/null 2>&1 <<OSA
tell application "Safari"
  activate
  make new document with properties {URL:"$URL"}
end tell
OSA
    return 0
  fi
  /usr/bin/open -n "$URL"
}

mkdir -p "$INSTALL_DIR/logs"
[ -n "$APP_DATA_DIR" ] && mkdir -p "$APP_DATA_DIR"

# Already running? Just open the UI.
if /usr/bin/curl -s -o /dev/null --max-time 2 "$URL"; then
  open_ui
  exit 0
fi

notify "Starting…"
cd "$INSTALL_DIR" || die_gui "Install folder not found: $INSTALL_DIR"
if [ "$(uname -m)" = "arm64" ]; then
  nohup arch -arm64 "$UVICORN" app:app --host 127.0.0.1 --port "$PORT" >>"$LOG" 2>&1 &
else
  nohup "$UVICORN" app:app --host 127.0.0.1 --port "$PORT" >>"$LOG" 2>&1 &
fi
SERVER_PID=$!
echo "$SERVER_PID" > "$PID_FILE"

# Wait for readiness (first run downloads an embedding model — allow ~2 min).
READY=0
for i in $(seq 1 120); do
  /usr/bin/curl -s -o /dev/null --max-time 2 "$URL" && { READY=1; break; }
  if ! kill -0 "$SERVER_PID" 2>/dev/null; then
    LAST_LOG="$(tail -n 40 "$LOG" 2>/dev/null)"
    die_gui "Odysseus failed to start.

Log:
$LOG

$LAST_LOG"
  fi
  sleep 1
done

if [ "$READY" = "1" ]; then
  open_ui
else
  notify "Odysseus is taking a while — open $URL once it finishes starting."
fi
exit 0
LAUNCHER

sed -e "s|__INSTALL_DIR__|$INSTALL_DIR|g" -e "s|__PORT__|$PORT|g" -e "s|__APP_DATA_DIR__|$APP_DATA_DIR|g" \
    "$APP/Contents/MacOS/$APP_NAME.tmpl" > "$APP/Contents/MacOS/$APP_NAME"
rm -f "$APP/Contents/MacOS/$APP_NAME.tmpl"
chmod +x "$APP/Contents/MacOS/$APP_NAME"

# Refresh Finder's icon cache for the new bundle.
touch "$APP"

# ── .dmg (drag-to-Applications) ──
echo "Packaging dist/$APP_NAME.dmg"
STAGE="$(mktemp -d)/dmg"
mkdir -p "$STAGE"
cp -R "$APP" "$STAGE/"
ln -s /Applications "$STAGE/Applications"
rm -f "$DIST/$APP_NAME.dmg"
if hdiutil create -volname "$APP_NAME" -srcfolder "$STAGE" -ov -format UDZO "$DIST/$APP_NAME.dmg" >/dev/null; then
  DMG_CREATED=1
else
  DMG_CREATED=0
  echo "  dmg:         skipped (hdiutil failed)"
fi
rm -rf "$STAGE"

echo ""
echo "Done:"
echo "  $APP"
if [ "$DMG_CREATED" = "1" ]; then
  echo "  $DIST/$APP_NAME.dmg"
fi
echo ""
echo "Run it:        open '$APP'"
if [ "$DMG_CREATED" = "1" ]; then
  echo "Install:       open '$DIST/$APP_NAME.dmg'  (drag Odysseus to Applications)"
fi

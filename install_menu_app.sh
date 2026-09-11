#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")" && pwd)"
APP="$ROOT/LG TV Bridge.app"
CONTENTS="$APP/Contents"
MACOS="$CONTENTS/MacOS"
LAUNCH_AGENTS="$HOME/Library/LaunchAgents"
PLIST="$LAUNCH_AGENTS/local.lgtv.notificationbridge.menu.plist"

mkdir -p "$MACOS"
cp "$ROOT/LGTVBridgeMenu-Info.plist" "$CONTENTS/Info.plist"
swiftc "$ROOT/LGTVBridgeMenu.swift" -o "$MACOS/LGTVBridgeMenu" -framework AppKit
chmod +x "$MACOS/LGTVBridgeMenu"

mkdir -p "$LAUNCH_AGENTS"
if [ -e "$PLIST" ]; then
  /bin/rm "$PLIST"
fi
/usr/libexec/PlistBuddy -c "Add :Label string local.lgtv.notificationbridge.menu" "$PLIST"
/usr/libexec/PlistBuddy -c "Add :ProgramArguments array" "$PLIST"
/usr/libexec/PlistBuddy -c "Add :ProgramArguments:0 string /usr/bin/open" "$PLIST"
/usr/libexec/PlistBuddy -c "Add :ProgramArguments:1 string $APP" "$PLIST"
/usr/libexec/PlistBuddy -c "Add :RunAtLoad bool true" "$PLIST"

launchctl bootout "gui/$(id -u)" "$PLIST" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$PLIST" 2>/dev/null || true
open "$APP"
echo "$APP"

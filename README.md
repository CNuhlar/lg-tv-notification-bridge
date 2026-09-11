# LG TV Notification Bridge

A small local bridge that forwards macOS notifications to an LG webOS TV as toast notifications.

The bridge uses the LG webOS websocket API on port `3001`. It does not require rooting the TV.

## What It Does

- Watches macOS Notification Center's `usernoted` SQLite store.
- Uses `kqueue` vnode events on macOS instead of slow polling.
- Parses WhatsApp direct and group notifications.
- Sends notifications to the TV via `ssap://system.notifications/createToast`.
- Keeps a local state file to avoid replaying old notifications.
- Filters noisy apps like iTerm by default.
- Exposes a menu bar app with forwarding enable/disable.

## Files

- `mac_notifications_to_tv.py` - main bridge.
- `send_notification.py` - LG webOS toast client.
- `webos_client.py` - minimal websocket transport for webOS.
- `LGTVBridgeMenu.swift` - native macOS menu bar wrapper.
- `install_menu_app.sh` - builds the `.app` and installs the LaunchAgent.

## Setup

Pair the TV once by sending a test toast:

```bash
python3 send_notification.py "HELLO FROM MAC"
```

The TV should show a permission prompt. Accept it.

Build and install the menu bar app:

```bash
./install_menu_app.sh
```

Then give `LG TV Bridge.app` Full Disk Access:

`System Settings > Privacy & Security > Full Disk Access`

The app needs this to read Notification Center's local database.

## Configuration

Environment variables:

- `LG_TV_HOST` - default `192.168.1.130`
- `LG_TV_PORT` - default `3001`
- `LG_TV_TOAST_KEY_FILE` - default `.lg-tv-toast-key`
- `MAC_NOTIFICATION_DENY_BUNDLES` - comma-separated bundle IDs to ignore
- `MAC_NOTIFICATION_RECENT_RECORD_WINDOW` - recent row window for coalesced notifications
- `MAC_NOTIFICATION_MAX_CHARS` - max TV toast length
- `MAC_NOTIFICATION_DEBUG_SKIPS=1` - log denylist skips

Default denylist:

```text
com.googlecode.iterm2,com.apple.ScriptEditor2
```

## Notes

This is intentionally local and scrappy. It reads a private macOS database whose schema can change, and it uses LG's local webOS API. It works on my Mac and my LG TV. Treat it like a fun LAN utility, not infrastructure.

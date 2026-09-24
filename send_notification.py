#!/usr/bin/env python3
import os
import sys

from webos_client import WebSocket


TV_HOST = os.environ.get("LG_TV_HOST", "192.168.1.103")
TV_PORT = int(os.environ.get("LG_TV_PORT", "3001"))
KEY_FILE = os.environ.get("LG_TV_TOAST_KEY_FILE", ".lg-tv-toast-key")
LEGACY_KEY_FILE = os.environ.get("LG_TV_KEY_FILE", ".lg-tv-client-key")
MESSAGE = " ".join(sys.argv[1:]) or "CIHAZ UYANIYOR"


def load_key():
    for path in (KEY_FILE, LEGACY_KEY_FILE):
        try:
            value = open(path, "r", encoding="utf-8").read().strip()
            if value:
                return value
        except FileNotFoundError:
            pass
    return None


def save_toast_key(key):
    with open(KEY_FILE, "w", encoding="utf-8") as f:
        f.write(key + "\n")
    os.chmod(KEY_FILE, 0o600)


def register_for_toast(ws, force=False):
    payload = {
        "forcePairing": force,
        "pairingType": "PROMPT",
        "manifest": {
            "manifestVersion": 1,
            "appVersion": "1.0",
            "signed": {
                "created": "2026-09-11T00:00:00Z",
                "appId": "com.codex.toast",
                "vendorId": "com.codex",
                "localizedAppNames": {"": "Toast Sender"},
                "localizedVendorNames": {"": "Codex"},
                "permissions": ["WRITE_NOTIFICATION_TOAST"],
                "serial": "toast",
            },
            "permissions": ["WRITE_NOTIFICATION_TOAST"],
        },
    }
    client_key = load_key()
    if client_key and not force:
        payload["client-key"] = client_key
    ws.send_json({"id": "register_toast", "type": "register", "payload": payload})
    while True:
        msg = ws.recv_json(timeout=90)
        if msg.get("id") != "register_toast":
            continue
        if msg.get("type") == "registered":
            key = msg.get("payload", {}).get("client-key")
            if key:
                save_toast_key(key)
            return
        if msg.get("type") == "error":
            raise RuntimeError(msg)


def send_once(message, force_pairing=False, quiet=False):
    ws = WebSocket(TV_HOST, TV_PORT)
    try:
        register_for_toast(ws, force=force_pairing)
        ws.send_json({
            "id": "toast_1",
            "type": "request",
            "uri": "ssap://system.notifications/createToast",
            "payload": {"message": message},
        })
        while True:
            msg = ws.recv_json(timeout=10)
            if msg.get("id") == "toast_1":
                if not quiet:
                    print(msg)
                return msg
    finally:
        ws.close()


class ToastClient:
    def __init__(self):
        self.ws = None

    def connect(self):
        self.close()
        self.ws = WebSocket(TV_HOST, TV_PORT)
        register_for_toast(self.ws, force=False)

    def send(self, message):
        if self.ws is None:
            self.connect()
        try:
            return self._send(message)
        except Exception:
            self.connect()
            return self._send(message)

    def _send(self, message):
        self.ws.send_json({
            "id": "toast_1",
            "type": "request",
            "uri": "ssap://system.notifications/createToast",
            "payload": {"message": message},
        })
        while True:
            msg = self.ws.recv_json(timeout=10)
            if msg.get("id") == "toast_1":
                return msg

    def close(self):
        if self.ws is not None:
            try:
                self.ws.close()
            except Exception:
                pass
            self.ws = None


def main():
    msg = send_once(MESSAGE, force_pairing=False)
    if msg.get("type") == "response":
        return 0
    if msg.get("error") == "401 insufficient permissions":
        msg = send_once(MESSAGE, force_pairing=True)
        return 0 if msg.get("type") == "response" else 1
    return 1


if __name__ == "__main__":
    raise SystemExit(main())

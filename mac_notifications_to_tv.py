#!/usr/bin/env python3
import os
import json
import hashlib
import plistlib
import re
import select
import shutil
import sqlite3
import sys
import tempfile
import time
from pathlib import Path

from send_notification import ToastClient


POLL_SECONDS = float(os.environ.get("MAC_NOTIFICATION_POLL_SECONDS", "0.2"))
EVENT_TIMEOUT_SECONDS = float(os.environ.get("MAC_NOTIFICATION_EVENT_TIMEOUT_SECONDS", "5"))
RECENT_RECORD_WINDOW = int(os.environ.get("MAC_NOTIFICATION_RECENT_RECORD_WINDOW", "25"))
MAX_TOAST_CHARS = int(os.environ.get("MAC_NOTIFICATION_MAX_CHARS", "180"))
STATE_FILE = Path(os.environ.get("MAC_NOTIFICATION_STATE_FILE", ".mac-notification-bridge.state"))
LOG_FILE = Path(os.environ.get("MAC_NOTIFICATION_LOG_FILE", "mac-notification-bridge.log"))
DEBUG_SKIPS = os.environ.get("MAC_NOTIFICATION_DEBUG_SKIPS", "").lower() in {"1", "true", "yes"}
DENY_BUNDLES = {
    bundle.strip()
    for bundle in os.environ.get(
        "MAC_NOTIFICATION_DENY_BUNDLES",
        "com.googlecode.iterm2,com.apple.ScriptEditor2",
    ).split(",")
    if bundle.strip()
}

ROOTS = [
    Path.home() / "Library/Group Containers/group.com.apple.UserNotifications",
    Path.home() / "Library/Group Containers/group.com.apple.usernoted",
]

TEXT_KEYS = (
    "title",
    "subtitle",
    "body",
    "message",
    "informativetext",
    "notification",
    "content",
    "app",
    "bundle",
    "identifier",
)

TECHNICAL_RE = re.compile(
    r"(^[A-Za-z0-9_-]+(\.[A-Za-z0-9_-]+){2,}$)"
    r"|(^[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}$)"
    r"|(^/)"
    r"|(^https?://)"
    r"|(^file://)"
    r"|(^NS[A-Z])"
    r"|(^UN[A-Z])"
    r"|(^com\.apple\.)"
)

TECHNICAL_WORDS = {
    "$archiver",
    "$classes",
    "$classname",
    "$objects",
    "$top",
    "aps",
    "bundleid",
    "categoryidentifier",
    "defaultaction",
    "interruptionlevel",
    "relevance",
    "threadidentifier",
}

TIME_KEYS = (
    "date",
    "time",
    "timestamp",
    "delivered",
    "presented",
    "created",
    "modified",
)


def fail_permission(path, exc):
    print("Mac Notification Center verisine erişilemiyor.", file=sys.stderr)
    print(f"Yol: {path}", file=sys.stderr)
    print(f"Hata: {exc}", file=sys.stderr)
    print("", file=sys.stderr)
    print("System Settings > Privacy & Security > Full Disk Access içinde", file=sys.stderr)
    print("bu terminal/Codex uygulamasına izin verip script'i tekrar çalıştır.", file=sys.stderr)
    return 13


def log(message):
    stamp = time.strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{stamp}] {message}"
    print(line, file=sys.stderr)
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def is_sqlite(path):
    try:
        with open(path, "rb") as f:
            return f.read(16) == b"SQLite format 3\x00"
    except OSError:
        return False


def candidate_dbs():
    found = []
    for root in ROOTS:
        if not root.exists():
            continue
        try:
            root.iterdir().__next__()
            for path in root.rglob("*"):
                if path.is_file() and is_sqlite(path):
                    found.append(path)
        except PermissionError as exc:
            raise PermissionError(f"{root}: {exc}") from exc
        except StopIteration:
            pass
    return found


def copy_db(path):
    tmpdir = tempfile.mkdtemp(prefix="mac-notifications-")
    dst = Path(tmpdir) / path.name
    shutil.copy2(path, dst)
    for suffix in ("-wal", "-shm"):
        sidecar = Path(str(path) + suffix)
        if sidecar.exists():
            shutil.copy2(sidecar, Path(str(dst) + suffix))
    return tmpdir, dst


def table_names(conn):
    return [
        row[0]
        for row in conn.execute(
            "select name from sqlite_master where type='table' and name not like 'sqlite_%'"
        )
    ]


def columns(conn, table):
    rows = conn.execute(f'pragma table_info("{table}")').fetchall()
    return [{"name": row[1], "type": (row[2] or "").lower()} for row in rows]


def score_table(cols):
    names = [col["name"].lower() for col in cols]
    text_score = sum(any(key in name for key in TEXT_KEYS) for name in names)
    time_score = sum(any(key in name for key in TIME_KEYS) for name in names)
    blob_score = sum("blob" in col["type"] for col in cols)
    return text_score + time_score + min(blob_score, 2)


def likely_time_columns(cols):
    ranked = []
    for col in cols:
        name = col["name"].lower()
        ctype = col["type"]
        if any(key in name for key in TIME_KEYS) or "real" in ctype or "int" in ctype:
            ranked.append(col["name"])
    return ranked


def normalize_timestamp(value):
    if value is None:
        return 0.0
    try:
        ts = float(value)
    except (TypeError, ValueError):
        return 0.0
    if ts > 10_000_000_000:
        ts /= 1000
    if 500_000_000 < ts < 3_000_000_000:
        return ts
    if 100_000_000 < ts < 900_000_000:
        return ts + 978_307_200
    return 0.0


def strings_from_blob(value):
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    if not isinstance(value, (bytes, bytearray, memoryview)):
        return []
    data = bytes(value)
    strings = []
    try:
        plist = plistlib.loads(data)
        strings.extend(strings_from_object(plist))
    except Exception:
        pass
    for match in re.finditer(rb"[\x20-\x7e]{4,}", data):
        text = match.group(0).decode("utf-8", errors="ignore").strip()
        if text and not text.startswith(("bplist", "NS.", "com.apple.")):
            strings.append(text)
    return strings


def clean_notification_text(text):
    text = str(text).replace("\u200e", "").replace("\u200f", "")
    text = re.sub(r"\s+", " ", text).strip()
    text = text.strip("_[]")
    return text


def whatsapp_text(strings):
    if "net.whatsapp.WhatsApp" not in strings:
        return ""
    app_idx = strings.index("net.whatsapp.WhatsApp")
    tail = strings[app_idx + 1 :]
    body = ""
    title = ""
    sender = ""
    is_group = "UNNotificationContentTypeMessagingGroup" in strings

    for item in tail:
        text = clean_notification_text(item)
        if not text:
            continue
        if text in {"net.whatsapp.WhatsApp", "message", "UNNotificationContentTypeMessagingDirect"}:
            continue
        if text.endswith(".m4a") or "@" in text and text.endswith("@lid"):
            continue
        if len(text) > 20 and re.fullmatch(r"[A-Za-z0-9_-]+", text):
            continue
        body = text
        break

    for item in tail:
        text = clean_notification_text(item)
        if not text:
            continue
        if text in {body, "net.whatsapp.WhatsApp", "message", "UNNotificationContentTypeMessagingDirect"}:
            continue
        if text.endswith(".m4a") or text.endswith("@lid"):
            continue
        if len(text) > 20 and re.fullmatch(r"[-A-Za-z0-9_]+", text):
            continue
        if "displayName" in text or "$" in text or "bplist" in text or "NSKeyedArchiver" in text:
            continue
        if re.search(r"[A-Za-zÇĞİÖŞÜçğıöşü]", text) and len(text) <= 80:
            title = text
            break

    if is_group:
        for item in tail:
            text = clean_notification_text(item).lstrip("~").strip()
            if not text:
                continue
            if text in {body, title, "net.whatsapp.WhatsApp", "message", "UNNotificationContentTypeMessagingGroup"}:
                continue
            if text.endswith(".m4a") or text.endswith("@lid") or text.endswith("@g.us"):
                continue
            if len(text) > 20 and re.fullmatch(r"[-A-Za-z0-9_]+", text):
                continue
            if "displayName" in text or "$" in text or "bplist" in text or "NSKeyedArchiver" in text:
                continue
            if re.search(r"[A-Za-zÇĞİÖŞÜçğıöşü]", text) and len(text) <= 80:
                sender = text
                break

    if is_group and title and sender and body:
        return f"{title} - {sender}: {body}"
    if title and body:
        return f"{title}: {body}"
    return body or title


def strings_from_object(value):
    found = []
    if isinstance(value, str):
        found.append(value)
    elif isinstance(value, dict):
        for key, item in value.items():
            if isinstance(key, str) and key.lower() in TEXT_KEYS:
                found.append(str(item))
            found.extend(strings_from_object(item))
    elif isinstance(value, (list, tuple)):
        for item in value:
            found.extend(strings_from_object(item))
    return found


def is_human_text(text):
    text = re.sub(r"\s+", " ", str(text)).strip()
    lower = text.lower()
    if len(text) < 2:
        return False
    if lower in TECHNICAL_WORDS:
        return False
    if TECHNICAL_RE.search(text):
        return False
    if text.count(".") >= 2 and " " not in text:
        return False
    if text.count("_") >= 2 and " " not in text:
        return False
    if text.startswith(("{", "(", "<", "$")):
        return False
    return True


def text_score(text, key=""):
    score = 0
    lower_key = key.lower()
    if any(hint in lower_key for hint in ("title", "subtitle", "body", "message", "informativetext")):
        score += 6
    if " " in text:
        score += 3
    if any(ord(ch) > 127 for ch in text):
        score += 2
    if 6 <= len(text) <= 120:
        score += 2
    if len(text) > 220:
        score -= 3
    return score


def useful_text(row):
    candidates = []
    for key, value in row.items():
        lower = key.lower()
        if value is None:
            continue
        if isinstance(value, str):
            if any(hint in lower for hint in TEXT_KEYS) or len(value) > 3:
                candidates.append((text_score(value, key), value.strip()))
        elif isinstance(value, (bytes, bytearray, memoryview)):
            strings = strings_from_blob(value)
            special = whatsapp_text(strings)
            if special:
                return special
            for text in strings:
                candidates.append((text_score(text, key), text))
    cleaned = []
    seen = set()
    for _, part in sorted(candidates, key=lambda item: item[0], reverse=True):
        part = re.sub(r"\s+", " ", part).strip()
        if not is_human_text(part):
            continue
        if part in seen:
            continue
        seen.add(part)
        cleaned.append(part)
    return " - ".join(cleaned[:2])


def bundle_ids_from_row(row):
    found = []
    for value in row.values():
        if isinstance(value, str):
            if re.fullmatch(r"[A-Za-z0-9_-]+(\.[A-Za-z0-9_-]+)+", value):
                found.append(value)
        elif isinstance(value, (bytes, bytearray, memoryview)):
            for text in strings_from_blob(value):
                if re.fullmatch(r"[A-Za-z0-9_-]+(\.[A-Za-z0-9_-]+)+", text):
                    found.append(text)
    return found


def should_skip_row(row):
    bundles = bundle_ids_from_row(row)
    if "net.whatsapp.WhatsApp" in bundles:
        return False
    return any(bundle in DENY_BUNDLES for bundle in bundles)


def row_event_key(rowid, text):
    digest = hashlib.sha1(text.encode("utf-8", errors="ignore")).hexdigest()[:16]
    return f"record:{rowid}:{digest}"


def remember_event(state, key):
    sent = state.setdefault("sent_event_keys", [])
    if key in sent:
        return False
    sent.append(key)
    if len(sent) > 500:
        del sent[:-500]
    return True


def read_state():
    try:
        text = STATE_FILE.read_text(encoding="utf-8").strip()
        if text.startswith("{"):
            return json.loads(text)
        return {"version": 1, "since_ts": float(text), "cursors": {}}
    except Exception:
        return {"version": 2, "since_ts": time.time(), "cursors": {}, "initialized": False}


def write_state(state):
    STATE_FILE.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def db_signature(path):
    sig = []
    for candidate in (path, Path(str(path) + "-wal"), Path(str(path) + "-shm")):
        try:
            stat = candidate.stat()
            sig.append((str(candidate), stat.st_mtime_ns, stat.st_size))
        except FileNotFoundError:
            sig.append((str(candidate), 0, 0))
    return tuple(sig)


def scan_db(path, state, emit):
    if path.name == "db" and "group.com.apple.usernoted/db2" in str(path):
        return scan_usernoted_record(path, state, emit)

    events = []
    cursors = state.setdefault("cursors", {})
    tmpdir = None
    try:
        tmpdir, copied = copy_db(path)
        conn = sqlite3.connect(copied)
        conn.row_factory = sqlite3.Row
        try:
            for table in table_names(conn):
                cols = columns(conn, table)
                if score_table(cols) < 2:
                    continue
                col_names = [col["name"] for col in cols]
                time_cols = likely_time_columns(cols)
                cursor_key = f"{path}:{table}"
                last_rowid = int(cursors.get(cursor_key, 0))
                if emit and last_rowid:
                    query_sql = f'select rowid as __rowid__, * from "{table}" where rowid > ? order by rowid asc limit 80'
                    params = (last_rowid,)
                else:
                    query_sql = f'select rowid as __rowid__, * from "{table}" order by rowid desc limit 1'
                    params = ()
                try:
                    rows = conn.execute(query_sql, params).fetchall()
                except sqlite3.DatabaseError:
                    continue
                max_rowid = last_rowid
                for row in rows:
                    values = dict(row)
                    rowid = int(values.pop("__rowid__", 0))
                    max_rowid = max(max_rowid, rowid)
                    if not emit:
                        continue
                    if should_skip_row(values):
                        continue
                    timestamps = [normalize_timestamp(values.get(col)) for col in time_cols]
                    ts = max(timestamps) if timestamps else 0.0
                    text = useful_text(values)
                    if not text:
                        continue
                    events.append({"ts": ts or time.time(), "text": text, "source": f"{path.name}:{table}"})
                if max_rowid > last_rowid:
                    cursors[cursor_key] = max_rowid
        finally:
            conn.close()
    finally:
        if tmpdir:
            shutil.rmtree(tmpdir, ignore_errors=True)
    return events


def open_db_direct(path):
    uri = f"file:{path}?mode=ro"
    conn = sqlite3.connect(uri, uri=True, timeout=0.1)
    conn.row_factory = sqlite3.Row
    return conn


def scan_usernoted_record(path, state, emit):
    events = []
    cursors = state.setdefault("cursors", {})
    cursor_key = f"{path}:record"
    last_rowid = int(cursors.get(cursor_key, 0))
    max_rowid = last_rowid
    try:
        conn = open_db_direct(path)
        close_conn = True
    except sqlite3.DatabaseError:
        tmpdir, copied = copy_db(path)
        conn = sqlite3.connect(copied)
        conn.row_factory = sqlite3.Row
        close_conn = True
    else:
        tmpdir = None

    try:
        if emit and last_rowid:
            new_rows = conn.execute(
                'select rowid as __rowid__, * from "record" where rowid > ? order by rowid asc limit 120',
                (last_rowid,),
            ).fetchall()
            recent_rows = conn.execute(
                'select rowid as __rowid__, * from "record" order by rowid desc limit ?',
                (RECENT_RECORD_WINDOW,),
            ).fetchall()
            rows_by_id = {}
            for row in list(new_rows) + list(reversed(recent_rows)):
                rows_by_id[int(row["__rowid__"])] = row
            rows = [rows_by_id[key] for key in sorted(rows_by_id)]
        else:
            rows = conn.execute(
                'select rowid as __rowid__, * from "record" order by rowid desc limit ?',
                (RECENT_RECORD_WINDOW,),
            ).fetchall()
        for row in rows:
            values = dict(row)
            rowid = int(values.pop("__rowid__", 0))
            max_rowid = max(max_rowid, rowid)
            if not emit:
                continue
            if should_skip_row(values):
                if DEBUG_SKIPS:
                    log(f"record row {rowid} denylist nedeniyle atlandı")
                continue
            text = useful_text(values)
            if text:
                event_key = row_event_key(rowid, text)
                if not remember_event(state, event_key):
                    continue
                ts = normalize_timestamp(values.get("delivered_date")) or time.time()
                events.append({"ts": ts, "text": text, "source": f"{path.name}:record"})
            else:
                log(f"record row {rowid} metinsiz atlandı")
        if max_rowid > last_rowid:
            cursors[cursor_key] = max_rowid
    finally:
        if close_conn:
            conn.close()
        if tmpdir:
            shutil.rmtree(tmpdir, ignore_errors=True)
    return events


def compact_message(text):
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) <= MAX_TOAST_CHARS:
        return text
    return text[: MAX_TOAST_CHARS - 1].rstrip() + "..."


def watch_paths_for_db(path):
    return [path, Path(str(path) + "-wal"), Path(str(path) + "-shm")]


def build_kqueue(dbs):
    if not hasattr(select, "kqueue"):
        return None, {}
    kq = select.kqueue()
    watched = {}
    flags = (
        select.KQ_NOTE_WRITE
        | select.KQ_NOTE_EXTEND
        | select.KQ_NOTE_ATTRIB
        | select.KQ_NOTE_RENAME
        | select.KQ_NOTE_DELETE
        | select.KQ_NOTE_REVOKE
    )
    for db in dbs:
        for path in watch_paths_for_db(db):
            try:
                fd = os.open(path, os.O_EVTONLY)
            except FileNotFoundError:
                continue
            except OSError:
                continue
            event = select.kevent(
                fd,
                filter=select.KQ_FILTER_VNODE,
                flags=select.KQ_EV_ADD | select.KQ_EV_CLEAR,
                fflags=flags,
            )
            try:
                kq.control([event], 0, 0)
            except OSError:
                os.close(fd)
                continue
            watched[fd] = {"path": path, "db": db}
    if not watched:
        kq.close()
        return None, {}
    return kq, watched


def close_kqueue(kq, watched):
    for fd in watched:
        try:
            os.close(fd)
        except OSError:
            pass
    try:
        if kq is not None:
            kq.close()
    except OSError:
        pass


def process_once(dbs, state, first_run, db_signatures, toast, force=False):
    latest = float(state.get("since_ts", time.time()))
    emitted = []
    for db in dbs:
        signature = db_signature(db)
        if not force and not first_run and signature == db_signatures.get(str(db)):
            continue
        db_signatures[str(db)] = signature
        try:
            emitted.extend(scan_db(db, state, emit=not first_run))
        except PermissionError:
            raise
        except Exception as exc:
            log(f"{db.name} okunamadı: {exc}")
    emitted.sort(key=lambda event: event["ts"])
    for event in emitted:
        latest = max(latest, event["ts"])
        message = compact_message(event["text"])
        if message:
            response = toast.send(message)
            if response.get("type") == "response":
                log(f"TV toast OK [{event['source']}]: {message}")
            else:
                log(f"TV toast ERROR [{event['source']}]: {message} -> {response}")
    state["since_ts"] = max(latest, float(state.get("since_ts", 0)))
    state["initialized"] = True
    write_state(state)


def bridge_loop():
    try:
        dbs = candidate_dbs()
    except PermissionError as exc:
        return fail_permission(ROOTS[0], exc)
    if not dbs:
        print("Notification SQLite veritabanı bulunamadı.", file=sys.stderr)
        return 2

    state = read_state()
    first_run = not state.get("initialized")
    db_signatures = {str(db): None for db in dbs}
    toast = ToastClient()
    log("Mac -> LG TV notification bridge başladı.")
    if first_run:
        log("İlk çalıştırma: mevcut bildirimler baseline alınıyor, TV'ye basılmayacak.")
    kq, watched = build_kqueue(dbs)
    if kq is not None:
        log(f"kqueue event mode aktif ({len(watched)} file watch).")
    else:
        log(f"kqueue kurulamadı; {POLL_SECONDS}s fallback polling aktif.")
    log("Durdurmak için Ctrl-C.")

    try:
        try:
            process_once(dbs, state, first_run, db_signatures, toast, force=True)
        except PermissionError as exc:
            return fail_permission(ROOTS[0], exc)
        first_run = False

        if kq is None:
            while True:
                try:
                    process_once(dbs, state, False, db_signatures, toast)
                except PermissionError as exc:
                    return fail_permission(ROOTS[0], exc)
                time.sleep(POLL_SECONDS)

        while True:
            events = kq.control(None, 32, EVENT_TIMEOUT_SECONDS)
            if not events:
                continue
            time.sleep(0.03)
            try:
                process_once(dbs, state, False, db_signatures, toast, force=True)
            except PermissionError as exc:
                return fail_permission(ROOTS[0], exc)
            if any(event.fflags & (select.KQ_NOTE_DELETE | select.KQ_NOTE_RENAME | select.KQ_NOTE_REVOKE) for event in events):
                close_kqueue(kq, watched)
                kq, watched = build_kqueue(dbs)
                if kq is None:
                    log("kqueue watch düştü; polling fallback'e geçiliyor.")
                    while True:
                        process_once(dbs, state, False, db_signatures, toast)
                        time.sleep(POLL_SECONDS)
                log(f"kqueue watch yenilendi ({len(watched)} file watch).")
    finally:
        close_kqueue(kq, watched)


if __name__ == "__main__":
    raise SystemExit(bridge_loop())

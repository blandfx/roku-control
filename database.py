import sqlite3
import os
import datetime
import contextlib
import re

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "roku_monitor.db")

PLEX_EPISODE_TITLE_RE = re.compile(r"^(.+?) - S\d+E\d+ - .+$", re.IGNORECASE)


def _history_group(video_id, video_title):
    """Return a stable display group for episodes of the same Plex show."""
    if not video_id or not video_id.startswith("plex:"):
        return None
    match = PLEX_EPISODE_TITLE_RE.match(video_title or "")
    if not match:
        return None
    parts = video_id.split(":", 2)
    server_id = parts[1] if len(parts) == 3 else "unknown"
    show = " ".join(match.group(1).casefold().split())
    return f"plex-show:{server_id}:{show}"

@contextlib.contextmanager
def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

def db_init():
    with get_db() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS devices (
                ip TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                screen_id TEXT,
                last_seen TEXT
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                device_ip TEXT NOT NULL,
                device_name TEXT NOT NULL,
                video_id TEXT NOT NULL,
                video_title TEXT,
                thumbnail_url TEXT,
                timestamp TEXT NOT NULL,
                duration REAL,
                position REAL,
                history_group TEXT
            )
        """)
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(history)")}
        if "history_group" not in columns:
            conn.execute("ALTER TABLE history ADD COLUMN history_group TEXT")
        for row in conn.execute(
            "SELECT id, video_id, video_title FROM history WHERE history_group IS NULL AND video_id LIKE 'plex:%'"
        ).fetchall():
            group = _history_group(row["video_id"], row["video_title"])
            if group:
                conn.execute("UPDATE history SET history_group = ? WHERE id = ?", (group, row["id"]))
        conn.execute("""
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )
        """)

def db_get_setting(key, default=None):
    with get_db() as conn:
        row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else default

def db_set_setting(key, value):
    with get_db() as conn:
        conn.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", (key, value))

def db_get_devices():
    with get_db() as conn:
        rows = conn.execute("SELECT * FROM devices").fetchall()
        return [dict(r) for r in rows]

def db_add_device(ip, name, screen_id=None):
    with get_db() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO devices (ip, name, screen_id, last_seen) VALUES (?, ?, ?, ?)",
            (ip, name, screen_id, datetime.datetime.now().isoformat())
        )

def db_remove_device(ip):
    with get_db() as conn:
        conn.execute("DELETE FROM devices WHERE ip = ?", (ip,))

def db_update_device_screen_id(ip, screen_id):
    with get_db() as conn:
        conn.execute(
            "UPDATE devices SET screen_id = ?, last_seen = ? WHERE ip = ?",
            (screen_id, datetime.datetime.now().isoformat(), ip)
        )

def db_add_history_entry(device_ip, device_name, video_id, video_title, thumbnail_url, duration, position):
    with get_db() as conn:
        history_group = _history_group(video_id, video_title)
        # Check if the last entry for this device matches this video_id and was recent (within 2 hours)
        last_entry = conn.execute(
            "SELECT * FROM history WHERE device_ip = ? ORDER BY id DESC LIMIT 1",
            (device_ip,)
        ).fetchone()

        now_str = datetime.datetime.now().isoformat()

        if last_entry and last_entry["video_id"] == video_id:
            # Check time diff
            last_time = datetime.datetime.fromisoformat(last_entry["timestamp"])
            time_diff = datetime.datetime.now() - last_time
            if time_diff.total_seconds() < 7200: # 2 hours
                # Update position and duration
                conn.execute(
                    """UPDATE history SET position = ?, duration = ?, timestamp = ?,
                       video_title = ?, thumbnail_url = ?, history_group = ? WHERE id = ?""",
                    (position, duration, now_str, video_title, thumbnail_url, history_group, last_entry["id"])
                )
                return last_entry["id"]

        # Otherwise, insert a new entry
        cursor = conn.execute(
            """INSERT INTO history
               (device_ip, device_name, video_id, video_title, thumbnail_url, timestamp, duration, position, history_group)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (device_ip, device_name, video_id, video_title, thumbnail_url, now_str, duration, position, history_group)
        )
        return cursor.lastrowid

def db_get_history(limit=50):
    plex_connected = bool(db_get_setting("plex_token"))
    with get_db() as conn:
        rows = conn.execute(
            """
            SELECT h1.* FROM history h1
            INNER JOIN (
                SELECT COALESCE(history_group, video_id) AS display_group, MAX(id) as max_id
                FROM history
                GROUP BY COALESCE(history_group, video_id)
            ) h2 ON h1.id = h2.max_id
            ORDER BY h1.timestamp DESC
            LIMIT ?
            """,
            (limit,)
        ).fetchall()
        # Add a placeholder/fetched thumbnail URL in the dictionary helper for frontend
        result = []
        for r in rows:
            d = dict(r)
            if d['video_id'].startswith('plex:'):
                parts = d['video_id'].split(':', 2)
                if plex_connected:
                    d["thumbnail_url"] = f"api/plex/art/{parts[1]}/{parts[2]}"
                else:
                    d["thumbnail_url"] = "https://images.unsplash.com/photo-1594909122845-11baa439b7bf?w=400&q=80"
            elif d['video_id'].startswith('plex_'):
                d["thumbnail_url"] = d.get("thumbnail_url") or "https://images.unsplash.com/photo-1594909122845-11baa439b7bf?w=400&q=80"
            else:
                d["thumbnail_url"] = f"https://img.youtube.com/vi/{d['video_id']}/0.jpg"
            result.append(d)
        return result

def db_delete_history_entry(history_id):
    with get_db() as conn:
        row = conn.execute("SELECT video_id, history_group FROM history WHERE id = ?", (history_id,)).fetchone()
        if not row:
            return 0
        if row["history_group"]:
            cursor = conn.execute("DELETE FROM history WHERE history_group = ?", (row["history_group"],))
        else:
            cursor = conn.execute("DELETE FROM history WHERE video_id = ?", (row["video_id"],))
        return cursor.rowcount

def db_get_last_played_for_device(device_ip):
    with get_db() as conn:
        row = conn.execute(
            "SELECT * FROM history WHERE device_ip = ? ORDER BY timestamp DESC LIMIT 1",
            (device_ip,)
        ).fetchone()
        return dict(row) if row else None

import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from typing import Dict, Iterator, List, Optional, Tuple

# Railway'da bu Volume mount qilingan doimiy papkaga ko'rsatilishi kerak
# (masalan DB_PATH=/data/captions.db), aks holda har deploy'da ma'lumot o'chadi.
DB_PATH = os.getenv("DB_PATH", "captions.db")

LOG_KEEP_DAYS = 90


@contextmanager
def _conn() -> Iterator[sqlite3.Connection]:
    """Xatolik bo'lsa ham ulanish yopiladi; muvaffaqiyatda commit qilinadi."""
    conn = sqlite3.connect(DB_PATH)
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def _column_exists(cur: sqlite3.Cursor, table: str, column: str) -> bool:
    cur.execute(f"PRAGMA table_info({table})")
    return any(row[1] == column for row in cur.fetchall())


def init_db() -> None:
    db_dir = os.path.dirname(DB_PATH)
    if db_dir:
        os.makedirs(db_dir, exist_ok=True)
    if not db_dir:
        # Volume yo'q bo'lishi mumkin — foydalanuvchiga eslatma uchun
        import logging
        logging.getLogger(__name__).warning(
            "DB_PATH='%s' — doimiy Volume emas. Railway'da DB_PATH=/data/captions.db "
            "qo'ying, aks holda har deploy'da ma'lumot o'chadi.", DB_PATH
        )
    with _conn() as conn:
        cur = conn.cursor()
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS channels (
                channel_id INTEGER PRIMARY KEY,
                title TEXT,
                owner_id INTEGER,
                translit_mode TEXT DEFAULT 'off',
                active INTEGER DEFAULT 1,
                paused INTEGER DEFAULT 0,
                caption_pos TEXT DEFAULT 'after'
            )
            """
        )
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS captions (
                channel_id INTEGER PRIMARY KEY,
                caption TEXT
            )
            """
        )
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT
            )
            """
        )
        # Botga to'g'ridan-to'g'ri yozilgan matn uchun foydalanuvchi tanlagan rejim
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS user_modes (
                user_id INTEGER PRIMARY KEY,
                mode TEXT
            )
            """
        )
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS caption_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                channel_id INTEGER,
                ts TEXT
            )
            """
        )
        # Eski bazalar uchun migratsiyalar
        if not _column_exists(cur, "channels", "owner_id"):
            cur.execute("ALTER TABLE channels ADD COLUMN owner_id INTEGER")
        if not _column_exists(cur, "channels", "translit_mode"):
            # Eski kanallar avvalgi xulq (l2c) bilan qoladi
            cur.execute("ALTER TABLE channels ADD COLUMN translit_mode TEXT DEFAULT 'l2c'")
        if not _column_exists(cur, "channels", "active"):
            cur.execute("ALTER TABLE channels ADD COLUMN active INTEGER DEFAULT 1")
        if not _column_exists(cur, "channels", "paused"):
            cur.execute("ALTER TABLE channels ADD COLUMN paused INTEGER DEFAULT 0")
        if not _column_exists(cur, "channels", "caption_pos"):
            cur.execute("ALTER TABLE channels ADD COLUMN caption_pos TEXT DEFAULT 'after'")

        # Botdan foydalangan hamma shaxsiy chat foydalanuvchilari (statistika uchun)
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                username TEXT,
                first_seen TEXT,
                last_seen TEXT
            )
            """
        )
        cur.execute(
            "CREATE TABLE IF NOT EXISTS counters (name TEXT PRIMARY KEY, value INTEGER DEFAULT 0)"
        )
        cur.execute("CREATE INDEX IF NOT EXISTS idx_caption_log_ts ON caption_log(ts)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_channels_owner ON channels(owner_id)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_users_last_seen ON users(last_seen)")

        # Mavjud kanal egalarini ham foydalanuvchi sifatida hisobga olamiz
        now_iso = datetime.now(timezone.utc).isoformat()
        cur.execute(
            "INSERT OR IGNORE INTO users (user_id, first_seen, last_seen) "
            "SELECT DISTINCT owner_id, ?, ? FROM channels WHERE owner_id IS NOT NULL",
            (now_iso, now_iso),
        )

        # Eski jurnal qatorlari o'chirilmaydi, balki umumiy hisobga qo'shib
        # qo'yiladi — "jami" son hech qachon kamaymaydi
        cutoff = (datetime.now(timezone.utc) - timedelta(days=LOG_KEEP_DAYS)).isoformat()
        old_n = cur.execute("SELECT COUNT(*) FROM caption_log WHERE ts < ?", (cutoff,)).fetchone()[0]
        if old_n:
            cur.execute(
                "INSERT INTO counters (name, value) VALUES ('applied_archived', ?) "
                "ON CONFLICT(name) DO UPDATE SET value = value + excluded.value",
                (old_n,),
            )
            cur.execute("DELETE FROM caption_log WHERE ts < ?", (cutoff,))


def migrate_legacy_caption(channel_id: Optional[int]) -> None:
    if not channel_id:
        return
    with _conn() as conn:
        cur = conn.cursor()
        cur.execute("SELECT value FROM settings WHERE key = 'caption'")
        row = cur.fetchone()
        if row and row[0]:
            cur.execute(
                "INSERT INTO captions (channel_id, caption) VALUES (?, ?) "
                "ON CONFLICT(channel_id) DO NOTHING",
                (channel_id, row[0]),
            )


def assign_default_owner(default_owner_id: Optional[int]) -> None:
    if not default_owner_id:
        return
    with _conn() as conn:
        conn.execute(
            "UPDATE channels SET owner_id = ? WHERE owner_id IS NULL",
            (default_owner_id,),
        )


def register_channel(channel_id: int, title: str, owner_id: int) -> bool:
    """Kanalni ro'yxatga oladi. MAVJUD egani ALMASHTIRMAYDI (boshqa admin
    bot huquqini o'zgartirsa ham kanal unga o'tib ketmasin). Faqat ega
    noma'lum (NULL) bo'lsa biriktiriladi. Kanal qayta faollashtiriladi.
    True — yangi yozuv yaratildi."""
    with _conn() as conn:
        cur = conn.cursor()
        cur.execute("SELECT 1 FROM channels WHERE channel_id = ?", (channel_id,))
        existed = cur.fetchone() is not None
        cur.execute(
            "INSERT INTO channels (channel_id, title, owner_id, active, translit_mode) "
            "VALUES (?, ?, ?, 1, 'off') "
            "ON CONFLICT(channel_id) DO UPDATE SET "
            "title = excluded.title, "
            "owner_id = COALESCE(channels.owner_id, excluded.owner_id), "
            "active = 1",
            (channel_id, title, owner_id),
        )
        return not existed


def deactivate_channel(channel_id: int) -> None:
    """Bot adminlikdan tushganda sozlamalar (izoh, rejim) saqlanib qoladi —
    qayta qo'shilganda hammasini boshidan kiritish shart emas."""
    with _conn() as conn:
        conn.execute("UPDATE channels SET active = 0 WHERE channel_id = ?", (channel_id,))


# Eski nom bilan moslik
remove_channel = deactivate_channel


def is_active(channel_id: int) -> bool:
    with _conn() as conn:
        row = conn.execute(
            "SELECT COALESCE(active, 1) FROM channels WHERE channel_id = ?", (channel_id,)
        ).fetchone()
    return bool(row and row[0])


def list_channels_for_owner(owner_id: int) -> List[Tuple[int, str]]:
    with _conn() as conn:
        return conn.execute(
            "SELECT channel_id, COALESCE(title, channel_id) FROM channels "
            "WHERE owner_id = ? AND COALESCE(active, 1) = 1 ORDER BY title",
            (owner_id,),
        ).fetchall()


def get_channel_title(channel_id: int) -> Optional[str]:
    with _conn() as conn:
        row = conn.execute(
            "SELECT title FROM channels WHERE channel_id = ?", (channel_id,)
        ).fetchone()
    return row[0] if row else None


def get_translit_mode(channel_id: int) -> str:
    with _conn() as conn:
        row = conn.execute(
            "SELECT translit_mode FROM channels WHERE channel_id = ?", (channel_id,)
        ).fetchone()
    return row[0] if row and row[0] else "off"


def set_translit_mode(channel_id: int, mode: str) -> None:
    with _conn() as conn:
        conn.execute(
            "UPDATE channels SET translit_mode = ? WHERE channel_id = ?",
            (mode, channel_id),
        )


def get_paused(channel_id: int) -> bool:
    with _conn() as conn:
        row = conn.execute(
            "SELECT COALESCE(paused, 0) FROM channels WHERE channel_id = ?", (channel_id,)
        ).fetchone()
    return bool(row and row[0])


def set_paused(channel_id: int, paused: bool) -> None:
    with _conn() as conn:
        conn.execute(
            "UPDATE channels SET paused = ? WHERE channel_id = ?",
            (1 if paused else 0, channel_id),
        )


def get_caption_pos(channel_id: int) -> str:
    with _conn() as conn:
        row = conn.execute(
            "SELECT caption_pos FROM channels WHERE channel_id = ?", (channel_id,)
        ).fetchone()
    return row[0] if row and row[0] in ("before", "after") else "after"


def set_caption_pos(channel_id: int, pos: str) -> None:
    if pos not in ("before", "after"):
        return
    with _conn() as conn:
        conn.execute(
            "UPDATE channels SET caption_pos = ? WHERE channel_id = ?", (pos, channel_id)
        )


def get_user_mode(user_id: int) -> str:
    """Shaxsiy chatdagi o'girish rejimi: 'auto' (standart), 'l2c', 'c2l', 'new'."""
    with _conn() as conn:
        row = conn.execute(
            "SELECT mode FROM user_modes WHERE user_id = ?", (user_id,)
        ).fetchone()
    return row[0] if row and row[0] else "auto"


def set_user_mode(user_id: int, mode: str) -> None:
    with _conn() as conn:
        conn.execute(
            "INSERT INTO user_modes (user_id, mode) VALUES (?, ?) "
            "ON CONFLICT(user_id) DO UPDATE SET mode = excluded.mode",
            (user_id, mode),
        )


def get_channel_owner(channel_id: int) -> Optional[int]:
    with _conn() as conn:
        row = conn.execute(
            "SELECT owner_id FROM channels WHERE channel_id = ?", (channel_id,)
        ).fetchone()
    return row[0] if row else None


def count_channels() -> int:
    with _conn() as conn:
        row = conn.execute(
            "SELECT COUNT(*) FROM channels WHERE COALESCE(active, 1) = 1"
        ).fetchone()
    return row[0] if row else 0


def touch_user(user_id: int, username: Optional[str] = None) -> None:
    now = datetime.now(timezone.utc).isoformat()
    with _conn() as conn:
        conn.execute(
            "INSERT INTO users (user_id, username, first_seen, last_seen) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(user_id) DO UPDATE SET last_seen = excluded.last_seen, "
            "username = COALESCE(excluded.username, users.username)",
            (user_id, username, now, now),
        )


def count_users(active_hours: Optional[int] = None, new_hours: Optional[int] = None) -> int:
    """Jami / so'nggi N soatda faol / so'nggi N soatda qo'shilgan foydalanuvchilar."""
    query = "SELECT COUNT(*) FROM users WHERE 1=1"
    params: list = []
    if active_hours is not None:
        query += " AND last_seen >= ?"
        params.append((datetime.now(timezone.utc) - timedelta(hours=active_hours)).isoformat())
    if new_hours is not None:
        query += " AND first_seen >= ?"
        params.append((datetime.now(timezone.utc) - timedelta(hours=new_hours)).isoformat())
    with _conn() as conn:
        return conn.execute(query, params).fetchone()[0]


def bump_counter(name: str, n: int = 1) -> None:
    with _conn() as conn:
        conn.execute(
            "INSERT INTO counters (name, value) VALUES (?, ?) "
            "ON CONFLICT(name) DO UPDATE SET value = value + excluded.value",
            (name, n),
        )


def get_counter(name: str) -> int:
    with _conn() as conn:
        row = conn.execute("SELECT value FROM counters WHERE name = ?", (name,)).fetchone()
    return row[0] if row else 0


def count_paused_channels() -> int:
    with _conn() as conn:
        return conn.execute(
            "SELECT COUNT(*) FROM channels WHERE COALESCE(active, 1) = 1 AND COALESCE(paused, 0) = 1"
        ).fetchone()[0]


def count_owners() -> int:
    with _conn() as conn:
        row = conn.execute(
            "SELECT COUNT(DISTINCT owner_id) FROM channels "
            "WHERE owner_id IS NOT NULL AND COALESCE(active, 1) = 1"
        ).fetchone()
    return row[0] if row else 0


def count_channels_with_caption() -> int:
    with _conn() as conn:
        row = conn.execute(
            "SELECT COUNT(*) FROM captions c JOIN channels ch ON ch.channel_id = c.channel_id "
            "WHERE c.caption IS NOT NULL AND c.caption != '' AND COALESCE(ch.active, 1) = 1"
        ).fetchone()
    return row[0] if row else 0


def count_channels_by_mode() -> Dict[str, int]:
    with _conn() as conn:
        rows = conn.execute(
            "SELECT COALESCE(translit_mode, 'off'), COUNT(*) FROM channels "
            "WHERE COALESCE(active, 1) = 1 GROUP BY 1"
        ).fetchall()
    return {mode: count for mode, count in rows}


def log_caption_applied(channel_id: int) -> None:
    with _conn() as conn:
        conn.execute(
            "INSERT INTO caption_log (channel_id, ts) VALUES (?, ?)",
            (channel_id, datetime.now(timezone.utc).isoformat()),
        )


def count_captions_applied(
    since_hours: Optional[int] = None, channel_id: Optional[int] = None
) -> int:
    """Jami (yoki so'nggi N soatda, yoki bitta kanal bo'yicha) qo'shilgan
    avtomatik izohlar soni."""
    query = "SELECT COUNT(*) FROM caption_log WHERE 1=1"
    params: list = []
    if since_hours is not None:
        since = (datetime.now(timezone.utc) - timedelta(hours=since_hours)).isoformat()
        query += " AND ts >= ?"
        params.append(since)
    if channel_id is not None:
        query += " AND channel_id = ?"
        params.append(channel_id)
    with _conn() as conn:
        row = conn.execute(query, params).fetchone()
        total = row[0] if row else 0
        # Faqat "jami" so'ralganda (vaqt va kanal filtrisiz) arxivlanganlar qo'shiladi
        if since_hours is None and channel_id is None:
            arch = conn.execute(
                "SELECT value FROM counters WHERE name = 'applied_archived'"
            ).fetchone()
            total += arch[0] if arch else 0
    return total


def get_caption(channel_id: int) -> Optional[str]:
    with _conn() as conn:
        row = conn.execute(
            "SELECT caption FROM captions WHERE channel_id = ?", (channel_id,)
        ).fetchone()
    return row[0] if row else None


def set_caption(channel_id: int, text: str) -> None:
    """Izohning ASL (o'girilmagan) HTML matni saqlanadi — o'girish postga
    qo'shilayotganda kanalning joriy rejimi bo'yicha bajariladi."""
    with _conn() as conn:
        conn.execute(
            "INSERT INTO captions (channel_id, caption) VALUES (?, ?) "
            "ON CONFLICT(channel_id) DO UPDATE SET caption = excluded.caption",
            (channel_id, text),
        )


def clear_caption(channel_id: int) -> None:
    with _conn() as conn:
        conn.execute("DELETE FROM captions WHERE channel_id = ?", (channel_id,))

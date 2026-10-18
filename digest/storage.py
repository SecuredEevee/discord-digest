"""SQLite 스키마와 쿼리.

id 컬럼은 TEXT(snowflake)이므로 범위 비교는 항상 CAST(id AS INTEGER)로 한다.
자릿수가 다른 snowflake끼리 문자열 비교하면 순서가 틀어진다.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS messages (
    id           TEXT PRIMARY KEY,
    channel_id   TEXT NOT NULL,
    author_id    TEXT NOT NULL,
    author_name  TEXT NOT NULL,
    content      TEXT NOT NULL,
    created_at   TEXT NOT NULL,
    reply_to_id  TEXT,
    attachments  TEXT,
    embeds       TEXT,
    raw          TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_msg_ch_time ON messages(channel_id, created_at);

CREATE TABLE IF NOT EXISTS cursors (
    channel_id   TEXT PRIMARY KEY,
    last_id      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS summaries (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    channel_id    TEXT NOT NULL,
    kind          TEXT NOT NULL,
    from_id       TEXT NOT NULL,
    to_id         TEXT NOT NULL,
    message_count INTEGER NOT NULL,
    body          TEXT NOT NULL,
    created_at    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_sum_ch_kind ON summaries(channel_id, kind);
"""


def utc_iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def connect(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    return conn


def message_row(raw: dict, channel_id: str) -> tuple:
    author = raw.get("author") or {}
    ref = raw.get("message_reference") or {}
    attachments = [
        {"filename": a.get("filename"), "url": a.get("url")} for a in raw.get("attachments") or []
    ]
    embeds = [
        {k: e[k] for k in ("title", "url", "description") if e.get(k)}
        for e in raw.get("embeds") or []
    ]
    embeds = [e for e in embeds if e]
    return (
        str(raw["id"]),
        str(channel_id),
        str(author.get("id", "")),
        author.get("global_name") or author.get("username") or "unknown",
        raw.get("content") or "",
        utc_iso(datetime.fromisoformat(raw["timestamp"])),
        str(ref["message_id"]) if ref.get("message_id") else None,
        json.dumps(attachments, ensure_ascii=False) if attachments else None,
        json.dumps(embeds, ensure_ascii=False) if embeds else None,
        json.dumps(raw, ensure_ascii=False),
    )


def insert_messages(conn: sqlite3.Connection, rows: list[tuple]) -> int:
    before = conn.total_changes
    conn.executemany("INSERT OR IGNORE INTO messages VALUES (?,?,?,?,?,?,?,?,?,?)", rows)
    return conn.total_changes - before


def oldest_message_id(conn: sqlite3.Connection, channel_id: str) -> int | None:
    row = conn.execute(
        "SELECT MIN(CAST(id AS INTEGER)) AS m FROM messages WHERE channel_id = ?", (channel_id,)
    ).fetchone()
    return row["m"]


def get_cursor(conn: sqlite3.Connection, channel_id: str) -> str | None:
    row = conn.execute("SELECT last_id FROM cursors WHERE channel_id = ?", (channel_id,)).fetchone()
    return row["last_id"] if row else None


def set_cursor(conn: sqlite3.Connection, channel_id: str, last_id: str) -> None:
    conn.execute(
        "INSERT INTO cursors (channel_id, last_id) VALUES (?, ?) "
        "ON CONFLICT(channel_id) DO UPDATE SET last_id = excluded.last_id",
        (channel_id, str(last_id)),
    )


def messages_in_id_range(
    conn: sqlite3.Connection, channel_id: str, after_id: int, before_id: int | None = None
) -> list[sqlite3.Row]:
    """after_id < id (< before_id) 인 메시지, 오래된 순."""
    sql = "SELECT * FROM messages WHERE channel_id = ? AND CAST(id AS INTEGER) > ?"
    params: list = [channel_id, int(after_id)]
    if before_id is not None:
        sql += " AND CAST(id AS INTEGER) < ?"
        params.append(int(before_id))
    sql += " ORDER BY CAST(id AS INTEGER)"
    return conn.execute(sql, params).fetchall()


def get_messages(conn: sqlite3.Connection, ids: list[str]) -> dict[str, sqlite3.Row]:
    if not ids:
        return {}
    placeholders = ",".join("?" * len(ids))
    rows = conn.execute(f"SELECT * FROM messages WHERE id IN ({placeholders})", ids).fetchall()
    return {r["id"]: r for r in rows}


def last_summary(conn: sqlite3.Connection, channel_id: str, kind: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM summaries WHERE channel_id = ? AND kind = ? "
        "ORDER BY CAST(to_id AS INTEGER) DESC, id DESC LIMIT 1",
        (channel_id, kind),
    ).fetchone()


def summaries_in_id_range(
    conn: sqlite3.Connection, channel_id: str, kind: str, start_id: int, end_id: int
) -> list[sqlite3.Row]:
    """to_id 가 [start_id, end_id) 에 있는 요약, 시간순."""
    return conn.execute(
        "SELECT * FROM summaries WHERE channel_id = ? AND kind = ? "
        "AND CAST(to_id AS INTEGER) >= ? AND CAST(to_id AS INTEGER) < ? "
        "ORDER BY CAST(to_id AS INTEGER)",
        (channel_id, kind, int(start_id), int(end_id)),
    ).fetchall()


def daily_exists(conn: sqlite3.Connection, channel_id: str, day_start_id: int) -> bool:
    """일일 다이제스트는 from_id 에 그날 00:00(로컬)의 snowflake를 기록한다."""
    row = conn.execute(
        "SELECT 1 FROM summaries WHERE channel_id = ? AND kind = 'daily' AND from_id = ?",
        (channel_id, str(day_start_id)),
    ).fetchone()
    return row is not None


def insert_summary(
    conn: sqlite3.Connection,
    channel_id: str,
    kind: str,
    from_id: str,
    to_id: str,
    message_count: int,
    body: str,
) -> None:
    conn.execute(
        "INSERT INTO summaries (channel_id, kind, from_id, to_id, message_count, body, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            channel_id,
            kind,
            str(from_id),
            str(to_id),
            message_count,
            body,
            utc_iso(datetime.now(timezone.utc)),
        ),
    )

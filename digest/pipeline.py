"""수집 → 저장 → 요약 → 출력 흐름."""

from __future__ import annotations

import logging
import sqlite3
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path

from . import output, storage
from .config import Config
from .discord_api import DiscordClient, snowflake_from_datetime
from .summarizer import LLM, LOG_FORMAT_NOTE, format_messages, summarize_day, summarize_log

log = logging.getLogger(__name__)


def fetch_channel(
    conn: sqlite3.Connection,
    client: DiscordClient,
    channel_id: str,
    backfill_hours: float,
    now: datetime | None = None,
) -> int:
    """채널의 신규 메시지를 저장하고 저장된 개수를 반환한다.

    배치 저장과 커서 갱신을 한 트랜잭션으로 묶어, 중간에 실패하면 전부 롤백하고
    다음 실행에서 같은 구간부터 다시 가져온다.
    """
    after = storage.get_cursor(conn, channel_id)
    if after is None:
        start = (now or datetime.now(timezone.utc)) - timedelta(hours=backfill_hours)
        after = str(snowflake_from_datetime(start))
        log.info("[%s] 최초 실행: 최근 %g시간 백필", channel_id, backfill_hours)

    fetched = inserted = 0
    looks_empty = True
    last_id = after
    try:
        for batch in client.iter_messages_after(channel_id, after):
            inserted += storage.insert_messages(conn, [storage.message_row(m, channel_id) for m in batch])
            fetched += len(batch)
            looks_empty = looks_empty and all(
                not m.get("content") and not m.get("attachments") and not m.get("embeds")
                for m in batch
            )
            last_id = batch[-1]["id"]
        if fetched:
            storage.set_cursor(conn, channel_id, last_id)
        conn.commit()
    except BaseException:
        conn.rollback()
        raise

    if fetched and looks_empty:
        log.warning(
            "[%s] 가져온 메시지 %d개의 content가 모두 비어 있습니다. "
            "Developer Portal에서 Message Content Intent 가 켜져 있는지 확인하세요.",
            channel_id,
            fetched,
        )
    return inserted


def backfill_channel(
    conn: sqlite3.Connection,
    client: DiscordClient,
    channel_id: str,
    hours: float,
    now: datetime | None = None,
) -> int:
    """DB의 가장 오래된 메시지보다 앞, (지금 - hours) 이후 구간만 채운다. 커서와 기존 행은 건드리지 않는다."""
    oldest = storage.oldest_message_id(conn, channel_id)
    if oldest is None:
        log.warning("[%s] 저장된 메시지가 없어 backfill 할 수 없습니다. 먼저 fetch 를 실행하세요", channel_id)
        return 0
    start = (now or datetime.now(timezone.utc)) - timedelta(hours=hours)
    after = snowflake_from_datetime(start)
    if after >= oldest:
        log.info("[%s] 이미 %g시간 전까지 수집되어 있습니다", channel_id, hours)
        return 0

    inserted = 0
    for batch in client.iter_messages_after(channel_id, after, before_id=oldest):
        inserted += storage.insert_messages(conn, [storage.message_row(m, channel_id) for m in batch])
        conn.commit()
    return inserted


def _reply_targets(conn: sqlite3.Connection, rows: list[sqlite3.Row]) -> dict[str, sqlite3.Row]:
    ids = sorted({r["reply_to_id"] for r in rows if r["reply_to_id"]})
    return storage.get_messages(conn, ids)


def _period(rows: list[sqlite3.Row], cfg: Config) -> str:
    fmt = "%Y-%m-%d %H:%M"
    first = datetime.fromisoformat(rows[0]["created_at"]).astimezone(cfg.timezone)
    last = datetime.fromisoformat(rows[-1]["created_at"]).astimezone(cfg.timezone)
    return f"{first:{fmt}} ~ {last:{fmt}} ({cfg.timezone.key})"


def _summarize_rows(
    conn: sqlite3.Connection,
    llm: LLM,
    cfg: Config,
    channel_id: str,
    rows: list[sqlite3.Row],
    kind: str,
    title: str,
    now: datetime,
) -> str:
    lines = format_messages(rows, _reply_targets(conn, rows), cfg.timezone)
    body = summarize_log(llm, lines, cfg.chunk_chars, cfg.language) if lines else "요약할 본문이 없습니다."
    local_now = now.astimezone(cfg.timezone)
    path = output.periodic_path(
        cfg.output_dir, channel_id, local_now, prefix="manual-" if kind == "manual" else ""
    )
    output.write_markdown(
        path,
        title,
        {"채널 ID": channel_id, "기간": _period(rows, cfg), "메시지 수": str(len(rows))},
        body,
    )
    storage.insert_summary(conn, channel_id, kind, rows[0]["id"], rows[-1]["id"], len(rows), body)
    conn.commit()
    log.info("[%s] %s 요약 작성: %s (메시지 %d개)", channel_id, kind, path, len(rows))
    return str(path)


def maybe_periodic(
    conn: sqlite3.Connection, llm: LLM, cfg: Config, channel_id: str, now: datetime | None = None
) -> str | None:
    """마지막 주기 요약 이후 쌓인 메시지가 min_messages 이상이면 주기 요약을 만든다.

    min_messages 미만인 회차의 메시지는 버리지 않고 다음 회차 요약에 합쳐진다.
    """
    last = storage.last_summary(conn, channel_id, "periodic")
    rows = storage.messages_in_id_range(conn, channel_id, int(last["to_id"]) if last else 0)
    if len(rows) < cfg.min_messages:
        log.info("[%s] 요약 대기 메시지 %d개 < %d, 주기 요약 생략", channel_id, len(rows), cfg.min_messages)
        return None
    now = now or datetime.now(timezone.utc)
    return _summarize_rows(conn, llm, cfg, channel_id, rows, "periodic", "주기 요약", now)


def summarize_since(
    conn: sqlite3.Connection,
    llm: LLM,
    cfg: Config,
    channel_id: str,
    since: datetime,
    now: datetime | None = None,
) -> str | None:
    """수동 요약. 주기 요약 진행 상태에 영향을 주지 않도록 kind='manual' 로 기록한다."""
    rows = storage.messages_in_id_range(conn, channel_id, snowflake_from_datetime(since) - 1)
    if not rows:
        log.info("[%s] %s 이후 메시지가 없습니다", channel_id, since.isoformat())
        return None
    now = now or datetime.now(timezone.utc)
    return _summarize_rows(conn, llm, cfg, channel_id, rows, "manual", "수동 요약", now)


def day_bounds(day: date, cfg: Config) -> tuple[int, int]:
    start = datetime.combine(day, time(0), tzinfo=cfg.timezone)
    end = datetime.combine(day + timedelta(days=1), time(0), tzinfo=cfg.timezone)
    return snowflake_from_datetime(start), snowflake_from_datetime(end)


def stored_day_range(conn: sqlite3.Connection, cfg: Config, channel_id: str) -> tuple[date, date] | None:
    row = conn.execute(
        "SELECT MIN(created_at) AS lo, MAX(created_at) AS hi FROM messages WHERE channel_id = ?",
        (channel_id,),
    ).fetchone()
    if row["lo"] is None:
        return None
    to_day = lambda s: datetime.fromisoformat(s).astimezone(cfg.timezone).date()
    return to_day(row["lo"]), to_day(row["hi"])


def export_days(
    conn: sqlite3.Connection,
    cfg: Config,
    channel_id: str,
    export_dir: Path,
    start: date | None = None,
    end: date | None = None,
) -> list[Path]:
    """하루 단위로 원문 전체를 텍스트로 내보낸다. 요약은 넣지 않는다."""
    stored = stored_day_range(conn, cfg, channel_id)
    if stored is None:
        log.info("[%s] 저장된 메시지가 없습니다", channel_id)
        return []
    day = max(start or stored[0], stored[0])
    last = min(end or stored[1], stored[1])

    paths = []
    while day <= last:
        start_id, end_id = day_bounds(day, cfg)
        rows = storage.messages_in_id_range(conn, channel_id, start_id - 1, end_id)
        lines = [l for l in format_messages(rows, _reply_targets(conn, rows), cfg.timezone)
                 if not l.startswith("--- ")]
        if lines:
            path = export_dir / channel_id / f"{day.isoformat()}.md"
            path.parent.mkdir(parents=True, exist_ok=True)
            header = (
                f"# 디스코드 채널 {channel_id} 대화 원문 — {day.isoformat()}\n\n"
                f"- 기간: {_period(rows, cfg)}\n"
                f"- 메시지 수: {len(lines)}\n"
                f"- 형식: {LOG_FORMAT_NOTE.split(' `--- ')[0]}\n\n---\n\n"
            )
            path.write_text(header + "\n".join(lines) + "\n", encoding="utf-8")
            paths.append(path)
            log.info("[%s] %s 내보냄: %s (메시지 %d개)", channel_id, day, path, len(lines))
        day += timedelta(days=1)
    return paths


def maybe_daily(
    conn: sqlite3.Connection,
    llm: LLM,
    cfg: Config,
    channel_id: str,
    now: datetime | None = None,
    force: bool = False,
) -> str | None:
    """daily_digest_hour 이후 첫 실행에서, 그날 일일 다이제스트가 없으면 만든다."""
    local_now = (now or datetime.now(timezone.utc)).astimezone(cfg.timezone)
    if not force and local_now.hour < cfg.daily_digest_hour:
        return None
    day = local_now.date()
    start_id, end_id = day_bounds(day, cfg)
    if not force and storage.daily_exists(conn, channel_id, start_id):
        return None

    rows = storage.messages_in_id_range(conn, channel_id, start_id - 1, end_id)
    if not rows:
        log.info("[%s] %s 메시지가 없어 일일 다이제스트 생략", channel_id, day)
        return None

    periodic = [
        s["body"] for s in storage.summaries_in_id_range(conn, channel_id, "periodic", start_id, end_id)
    ]
    lines = format_messages(rows, _reply_targets(conn, rows), cfg.timezone)
    body = summarize_day(llm, day.isoformat(), periodic, lines, cfg.chunk_chars, cfg.language)

    path = output.daily_path(cfg.output_dir, channel_id, day)
    meta = {"채널 ID": channel_id, "기간": _period(rows, cfg), "메시지 수": str(len(rows))}
    output.write_markdown(path, f"일일 다이제스트 — {day}", meta, body)
    storage.insert_summary(conn, channel_id, "daily", str(start_id), str(end_id), len(rows), body)
    conn.commit()
    log.info("[%s] 일일 다이제스트 작성: %s (메시지 %d개)", channel_id, path, len(rows))

    if cfg.webhook_url:
        text = f"**일일 다이제스트 — {day}** (채널 {channel_id}, 메시지 {len(rows)}개)\n\n{body}"
        if output.send_webhook(cfg.webhook_url, text):
            log.info("[%s] 웹훅 전송 완료", channel_id)
    return str(path)

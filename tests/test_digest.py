"""§13 완료 기준을 오프라인으로 검증한다 (httpx.MockTransport + 가짜 LLM)."""

from __future__ import annotations

import logging
import multiprocessing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx
import pytest

from digest import output, pipeline, storage
from digest.__main__ import AlreadyRunning, single_instance
from digest.config import Config
from digest.discord_api import DiscordAccessError, DiscordClient, snowflake_from_datetime
from digest.summarizer import chunk_lines, summarize_log

TOKEN = "test-token-SECRET-123"
CHANNEL = "111111111111111111"
NOW = datetime(2026, 10, 18, 14, 0, tzinfo=timezone.utc)  # 23:00 KST


def make_msg(dt: datetime, n: int, **extra) -> dict:
    sid = snowflake_from_datetime(dt) + n
    return {
        "id": str(sid),
        "channel_id": CHANNEL,
        "author": {"id": "42", "username": "alice", "global_name": "Alice"},
        "content": f"message {n}",
        "timestamp": dt.isoformat(),
        "attachments": [],
        "embeds": [],
        **extra,
    }


class FakeDiscord:
    """after/limit 를 해석하는 가짜 채널. 디스코드처럼 최신순으로 응답한다."""

    def __init__(self, messages: list[dict]):
        self.messages = sorted(messages, key=lambda m: int(m["id"]))
        self.requests: list[httpx.Request] = []
        self.responses: list[httpx.Response] = []  # 앞에서부터 강제 응답

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.responses:
            return self.responses.pop(0)
        after = int(request.url.params["after"])
        limit = int(request.url.params["limit"])
        page = [m for m in self.messages if int(m["id"]) > after][:limit]
        return httpx.Response(200, json=list(reversed(page)))


@pytest.fixture
def conn(tmp_path):
    c = storage.connect(tmp_path / "digest.db")
    yield c
    c.close()


@pytest.fixture
def cfg(tmp_path) -> Config:
    return Config(
        base_dir=tmp_path,
        channels=[CHANNEL],
        backfill_hours=24,
        min_messages=5,
        daily_digest_hour=23,
        timezone=ZoneInfo("Asia/Seoul"),
        model="test",
        effort="low",
        fallback=False,
        language="ko",
        chunk_chars=60000,
        output_dir=tmp_path / "output",
        webhook_url="",
    )


class FakeLLM:
    def __init__(self):
        self.calls: list[str] = []

    def summarize(self, text: str) -> str:
        self.calls.append(text)
        return f"## 주요 주제\n요약 {len(self.calls)}\n\n## 결정/합의 사항\n없음"


def client_for(fake: FakeDiscord, sleeps: list | None = None) -> DiscordClient:
    sleeps = sleeps if sleeps is not None else []
    return DiscordClient(TOKEN, transport=httpx.MockTransport(fake), sleep=sleeps.append)


def count(conn) -> int:
    return conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0]


def test_backfill_and_pagination(conn):
    old = make_msg(NOW - timedelta(hours=30), 0)  # 백필 구간 밖
    recent = [make_msg(NOW - timedelta(hours=20) + timedelta(seconds=i), i) for i in range(250)]
    fake = FakeDiscord([old, *recent])
    with client_for(fake) as client:
        inserted = pipeline.fetch_channel(conn, client, CHANNEL, 24, now=NOW)
    assert inserted == 250
    assert count(conn) == 250
    assert storage.get_cursor(conn, CHANNEL) == recent[-1]["id"]
    assert len(fake.requests) == 3  # 100 + 100 + 50
    assert fake.requests[0].headers["Authorization"] == f"Bot {TOKEN}"


def test_second_run_fetches_only_new(conn):
    first = [make_msg(NOW - timedelta(hours=1, seconds=-i), i) for i in range(10)]
    fake = FakeDiscord(first)
    with client_for(fake) as client:
        pipeline.fetch_channel(conn, client, CHANNEL, 24, now=NOW)
        fake.messages += [make_msg(NOW + timedelta(seconds=i), 100 + i) for i in range(3)]
        fake.requests.clear()
        inserted = pipeline.fetch_channel(conn, client, CHANNEL, 24, now=NOW)
    assert inserted == 3
    assert count(conn) == 13
    assert fake.requests[0].url.params["after"] == first[-1]["id"]


def test_duplicate_insert_is_ignored(conn):
    m = make_msg(NOW, 1)
    storage.insert_messages(conn, [storage.message_row(m, CHANNEL)])
    assert storage.insert_messages(conn, [storage.message_row(m, CHANNEL)]) == 0


def test_429_waits_and_resumes(conn):
    fake = FakeDiscord([make_msg(NOW - timedelta(minutes=5), 1)])
    fake.responses.append(httpx.Response(429, json={"retry_after": 1.5, "global": False}))
    sleeps: list[float] = []
    with client_for(fake, sleeps) as client:
        assert pipeline.fetch_channel(conn, client, CHANNEL, 24, now=NOW) == 1
    assert sleeps == [1.5]


def test_bucket_exhaustion_waits(conn):
    fake = FakeDiscord([])
    fake.responses.append(
        httpx.Response(200, json=[], headers={"X-RateLimit-Remaining": "0", "X-RateLimit-Reset-After": "0.7"})
    )
    sleeps: list[float] = []
    with client_for(fake, sleeps) as client:
        pipeline.fetch_channel(conn, client, CHANNEL, 24, now=NOW)
    assert sleeps == [0.7]


def test_failure_mid_fetch_keeps_cursor(conn):
    msgs = [make_msg(NOW - timedelta(hours=2) + timedelta(seconds=i), i) for i in range(150)]
    fake = FakeDiscord(msgs)
    original = fake.__call__

    def flaky(request):
        if len(fake.requests) >= 1:  # 첫 페이지 이후 계속 500
            fake.requests.append(request)
            return httpx.Response(500)
        return original(request)

    sleeps: list[float] = []
    client = DiscordClient(TOKEN, transport=httpx.MockTransport(flaky), sleep=sleeps.append)
    with pytest.raises(Exception):
        pipeline.fetch_channel(conn, client, CHANNEL, 24, now=NOW)
    assert storage.get_cursor(conn, CHANNEL) is None
    assert count(conn) == 0
    assert sleeps == [2.0, 4.0, 8.0]  # 지수 백오프 3회


def test_forbidden_raises_access_error(conn):
    fake = FakeDiscord([])
    fake.responses.append(httpx.Response(403, json={"message": "Missing Access", "code": 50001}))
    with client_for(fake) as client, pytest.raises(DiscordAccessError):
        pipeline.fetch_channel(conn, client, CHANNEL, 24, now=NOW)


def test_empty_content_warns_about_intent(conn, caplog):
    msgs = [make_msg(NOW - timedelta(minutes=i), i, content="") for i in range(3)]
    with client_for(FakeDiscord(msgs)) as client, caplog.at_level(logging.WARNING):
        pipeline.fetch_channel(conn, client, CHANNEL, 24, now=NOW)
    assert "Message Content Intent" in caplog.text


def _seed(conn, n: int, start: datetime):
    rows = [storage.message_row(make_msg(start + timedelta(minutes=i), i), CHANNEL) for i in range(n)]
    storage.insert_messages(conn, rows)
    conn.commit()


def test_periodic_respects_min_messages(conn, cfg):
    llm = FakeLLM()
    _seed(conn, 4, NOW - timedelta(hours=1))
    assert pipeline.maybe_periodic(conn, llm, cfg, CHANNEL, now=NOW) is None
    assert llm.calls == []

    # 미달 회차의 메시지는 다음 회차에 합쳐진다
    storage.insert_messages(conn, [storage.message_row(make_msg(NOW, 99), CHANNEL)])
    path = pipeline.maybe_periodic(conn, llm, cfg, CHANNEL, now=NOW)
    assert path and Path(path).name == "2300.md"
    text = Path(path).read_text()
    assert "메시지 수**: 5" in text and CHANNEL in text
    assert pipeline.maybe_periodic(conn, llm, cfg, CHANNEL, now=NOW) is None


def test_daily_only_once(conn, cfg):
    llm = FakeLLM()
    _seed(conn, 10, NOW - timedelta(hours=3))
    before = NOW - timedelta(hours=2)  # 21:00 KST
    assert pipeline.maybe_daily(conn, llm, cfg, CHANNEL, now=before) is None
    path = pipeline.maybe_daily(conn, llm, cfg, CHANNEL, now=NOW)
    assert path and path.endswith("2026-10-18/daily.md")
    assert pipeline.maybe_daily(conn, llm, cfg, CHANNEL, now=NOW + timedelta(minutes=30)) is None
    assert len(llm.calls) == 1
    # --force 는 다시 만든다
    assert pipeline.maybe_daily(conn, llm, cfg, CHANNEL, now=before, force=True)


def test_reply_and_attachment_format(conn, cfg):
    llm = FakeLLM()
    orig = make_msg(NOW - timedelta(minutes=10), 1, content="원래 질문입니다 " * 10)
    reply = make_msg(
        NOW - timedelta(minutes=5),
        2,
        content="답장",
        message_reference={"message_id": orig["id"]},
        attachments=[{"filename": "a.png", "url": "https://cdn/a.png"}],
    )
    storage.insert_messages(conn, [storage.message_row(m, CHANNEL) for m in (orig, reply)])
    cfg = Config(**{**cfg.__dict__, "min_messages": 1})
    pipeline.maybe_periodic(conn, llm, cfg, CHANNEL, now=NOW)
    prompt = llm.calls[0]
    assert "[22:55] Alice: 답장  (↪ Alice: 원래 질문입니다 원래 질문입니다 원래 질문입니다 원래 질" in prompt
    assert "[첨부: a.png]" in prompt


def test_map_reduce_chunking():
    llm = FakeLLM()
    lines = [f"[10:{i:02d}] bob: " + "x" * 50 for i in range(40)]
    assert len(chunk_lines(lines, 500)) > 1
    summarize_log(llm, lines, 500, "ko")
    assert len(llm.calls) > 2
    assert "<summaries>" in llm.calls[-1]


def test_webhook_split_and_no_url_in_logs(caplog):
    text = "\n".join("line %d " % i + "y" * 100 for i in range(60))
    chunks = output.split_message(text)
    assert all(len(c) <= 2000 for c in chunks) and len(chunks) > 1

    url = "https://discord.com/api/webhooks/1/SECRET-WEBHOOK-TOKEN"
    sent = []

    def handler(request):
        sent.append(request)
        return httpx.Response(500) if len(sent) == 2 else httpx.Response(204)

    with caplog.at_level(logging.DEBUG):
        ok = output.send_webhook(url, text, transport=httpx.MockTransport(handler))
    assert not ok
    assert "SECRET-WEBHOOK-TOKEN" not in caplog.text


def _try_lock(path, q):
    try:
        with single_instance(Path(path)):
            q.put("acquired")
    except AlreadyRunning:
        q.put("blocked")


def test_lock_prevents_concurrent_runs(tmp_path):
    lock = tmp_path / "digest.lock"
    with single_instance(lock):
        q = multiprocessing.Queue()
        p = multiprocessing.Process(target=_try_lock, args=(str(lock), q))
        p.start()
        p.join(10)
        assert q.get(timeout=5) == "blocked"
    with single_instance(lock):  # 해제 후 재획득 가능 (stale lock 없음)
        pass


def test_token_not_logged(conn, caplog):
    fake = FakeDiscord([make_msg(NOW - timedelta(minutes=1), 1)])
    fake.responses.append(httpx.Response(401, json={"message": "401: Unauthorized", "code": 0}))
    with caplog.at_level(logging.DEBUG), client_for(fake) as client:
        with pytest.raises(DiscordAccessError) as exc:
            pipeline.fetch_channel(conn, client, CHANNEL, 24, now=NOW)
    assert TOKEN not in caplog.text and TOKEN not in str(exc.value)


def test_backfill_fills_only_before_oldest(conn):
    old = [make_msg(NOW - timedelta(hours=40) + timedelta(seconds=i), i) for i in range(150)]
    recent = [make_msg(NOW - timedelta(hours=10) + timedelta(seconds=i), i) for i in range(30)]
    too_old = make_msg(NOW - timedelta(hours=100), 0)
    fake = FakeDiscord([too_old, *old, *recent])
    with client_for(fake) as client:
        pipeline.fetch_channel(conn, client, CHANNEL, 24, now=NOW)
        cursor = storage.get_cursor(conn, CHANNEL)
        assert count(conn) == 30
        inserted = pipeline.backfill_channel(conn, client, CHANNEL, 48, now=NOW)
        assert inserted == 150
        assert count(conn) == 180
        assert storage.get_cursor(conn, CHANNEL) == cursor
        assert pipeline.backfill_channel(conn, client, CHANNEL, 48, now=NOW) == 0
        assert count(conn) == 180

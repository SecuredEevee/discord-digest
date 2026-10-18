"""디스코드 REST API: 채널 메시지 페치와 레이트 리밋 처리."""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Iterator
from datetime import datetime, timezone

import httpx

log = logging.getLogger(__name__)

API_BASE = "https://discord.com/api/v10"
USER_AGENT = "DiscordBot (discord-digest, 0.1.0)"
DISCORD_EPOCH_MS = 1420070400000
PAGE_SIZE = 100
MAX_RATE_LIMIT_WAITS = 10


def snowflake_from_datetime(dt: datetime) -> int:
    ms = int(dt.timestamp() * 1000)
    return max(0, (ms - DISCORD_EPOCH_MS) << 22)


def datetime_from_snowflake(snowflake: int | str) -> datetime:
    ms = (int(snowflake) >> 22) + DISCORD_EPOCH_MS
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc)


class DiscordError(Exception):
    pass


class DiscordAccessError(DiscordError):
    """401/403/404: 토큰이나 권한 문제. 해당 채널은 건너뛴다."""


def retry_after_seconds(resp: httpx.Response) -> float:
    try:
        return float(resp.json()["retry_after"])
    except (ValueError, KeyError, TypeError):
        pass
    try:
        return float(resp.headers.get("Retry-After", 1))
    except ValueError:
        return 1.0


def _error_message(resp: httpx.Response) -> str:
    try:
        body = resp.json()
        return f"{body.get('message', '')} (code {body.get('code', '?')})"
    except ValueError:
        return ""


class DiscordClient:
    def __init__(
        self,
        token: str,
        *,
        transport: httpx.BaseTransport | None = None,
        sleep: Callable[[float], None] = time.sleep,
        max_retries: int = 3,
    ):
        self._http = httpx.Client(
            base_url=API_BASE,
            headers={"Authorization": token, "User-Agent": USER_AGENT},
            timeout=30.0,
            transport=transport,
        )
        self._sleep = sleep
        self._max_retries = max_retries

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> DiscordClient:
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def _get(self, path: str, params: dict) -> list | dict:
        failures = 0
        rate_limit_waits = 0
        while True:
            try:
                resp = self._http.get(path, params=params)
            except httpx.TransportError as e:
                failures += 1
                if failures > self._max_retries:
                    raise DiscordError(f"네트워크 오류 ({type(e).__name__}) {path}") from e
                self._backoff(failures, type(e).__name__)
                continue

            status = resp.status_code
            if status == 429:
                rate_limit_waits += 1
                if rate_limit_waits > MAX_RATE_LIMIT_WAITS:
                    raise DiscordError(f"레이트 리밋이 계속됨 {path}")
                wait = retry_after_seconds(resp)
                log.warning("429 rate limited, %.2fs 대기 (%s)", wait, path)
                self._sleep(wait)
                continue
            if status >= 500:
                failures += 1
                if failures > self._max_retries:
                    raise DiscordError(f"HTTP {status} {path}")
                self._backoff(failures, f"HTTP {status}")
                continue
            if status in (401, 403, 404):
                raise DiscordAccessError(f"HTTP {status} {path}: {_error_message(resp)}")
            if resp.is_error:
                raise DiscordError(f"HTTP {status} {path}: {_error_message(resp)}")

            if resp.headers.get("X-RateLimit-Remaining") == "0":
                wait = float(resp.headers.get("X-RateLimit-Reset-After", 0) or 0)
                if wait > 0:
                    log.debug("rate limit bucket 소진, %.2fs 대기", wait)
                    self._sleep(wait)
            return resp.json()

    def _backoff(self, attempt: int, reason: str) -> None:
        wait = 2.0**attempt
        log.warning("%s, %d/%d번째 재시도 전 %.0fs 대기", reason, attempt, self._max_retries, wait)
        self._sleep(wait)

    def iter_messages_after(
        self, channel_id: str, after_id: int | str, before_id: int | str | None = None
    ) -> Iterator[list[dict]]:
        """after_id 이후 메시지를 오래된 순 배치(최대 100개)로 내보낸다. before_id 가 있으면 그 미만만."""
        after = str(after_id)
        limit_id = int(before_id) if before_id is not None else None
        while True:
            batch = self._get(
                f"/channels/{channel_id}/messages", {"after": after, "limit": PAGE_SIZE}
            )
            if not batch:
                return
            batch.sort(key=lambda m: int(m["id"]))
            full = len(batch) == PAGE_SIZE
            if limit_id is not None:
                batch = [m for m in batch if int(m["id"]) < limit_id]
                if not batch:
                    return
                full = full and len(batch) == PAGE_SIZE
            yield batch
            after = batch[-1]["id"]
            if not full:
                return

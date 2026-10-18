"""마크다운 파일 작성과 디스코드 웹훅 전송."""

from __future__ import annotations

import logging
import re
import time
from collections.abc import Callable
from datetime import date, datetime
from pathlib import Path

import httpx

from .discord_api import retry_after_seconds

log = logging.getLogger(__name__)

DISCORD_MESSAGE_LIMIT = 2000
_WEBHOOK_TOKEN = re.compile(r"(/webhooks/\d+/)[\w-]+")


class _RedactWebhookToken(logging.Filter):
    """httpx 는 INFO 레벨에서 요청 URL을 로그로 남긴다. 웹훅 토큰을 가린다."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = _WEBHOOK_TOKEN.sub(r"\1***", record.getMessage())
        record.args = None
        return True


logging.getLogger("httpx").addFilter(_RedactWebhookToken())


def periodic_path(output_dir: Path, channel_id: str, at: datetime, prefix: str = "") -> Path:
    return output_dir / channel_id / at.strftime("%Y-%m-%d") / f"{prefix}{at:%H%M}.md"


def daily_path(output_dir: Path, channel_id: str, day: date) -> Path:
    return output_dir / channel_id / day.isoformat() / "daily.md"


def write_markdown(path: Path, title: str, meta: dict[str, str], body: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    header = "\n".join(f"- **{k}**: {v}" for k, v in meta.items())
    path.write_text(f"# {title}\n\n{header}\n\n---\n\n{body.strip()}\n", encoding="utf-8")


def split_message(text: str, limit: int = DISCORD_MESSAGE_LIMIT) -> list[str]:
    """줄 경계 기준으로 limit 이하 조각으로 나눈다. 한 줄이 limit 보다 길면 강제로 자른다."""
    chunks: list[str] = []
    current = ""
    for line in text.splitlines():
        while len(line) > limit:
            if current:
                chunks.append(current)
                current = ""
            chunks.append(line[:limit])
            line = line[limit:]
        candidate = f"{current}\n{line}" if current else line
        if len(candidate) > limit:
            chunks.append(current)
            current = line
        else:
            current = candidate
    if current.strip():
        chunks.append(current)
    return [c for c in chunks if c.strip()]


def send_webhook(
    url: str,
    text: str,
    *,
    transport: httpx.BaseTransport | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> bool:
    """웹훅 URL에는 토큰이 포함되므로 로그나 예외 메시지에 URL을 남기지 않는다."""
    with httpx.Client(timeout=30.0, transport=transport) as client:
        for i, chunk in enumerate(split_message(text), 1):
            payload = {"content": chunk, "allowed_mentions": {"parse": []}}
            for _ in range(5):
                try:
                    resp = client.post(url, json=payload)
                except httpx.TransportError as e:
                    log.error("웹훅 전송 실패: 네트워크 오류 (%s)", type(e).__name__)
                    return False
                if resp.status_code == 429:
                    sleep(retry_after_seconds(resp))
                    continue
                break
            if resp.is_error:
                log.error("웹훅 전송 실패: HTTP %d (조각 %d)", resp.status_code, i)
                return False
    return True

"""config.toml + .env 로드. 비밀값은 환경변수(.env)에서만 읽는다."""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass
from pathlib import Path
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent


class ConfigError(Exception):
    pass


@dataclass(frozen=True)
class Config:
    base_dir: Path
    channels: list[str]
    backfill_hours: float
    min_messages: int
    daily_digest_hour: int
    timezone: ZoneInfo
    model: str
    effort: str
    fallback: bool
    language: str
    chunk_chars: int
    output_dir: Path
    webhook_url: str
    provider: str = "api"
    claude_path: str = "claude"

    @property
    def data_dir(self) -> Path:
        return self.base_dir / "data"

    @property
    def db_path(self) -> Path:
        return self.data_dir / "digest.db"

    @property
    def lock_path(self) -> Path:
        return self.data_dir / "digest.lock"

    @property
    def log_dir(self) -> Path:
        return self.base_dir / "logs"


def load_config(path: Path | None = None, base_dir: Path = BASE_DIR) -> Config:
    load_dotenv(base_dir / ".env")
    path = path or base_dir / "config.toml"
    try:
        with open(path, "rb") as f:
            raw = tomllib.load(f)
    except FileNotFoundError:
        raise ConfigError(f"설정 파일이 없습니다: {path}") from None

    discord = raw.get("discord", {})
    summary = raw.get("summary", {})
    output = raw.get("output", {})

    channels = discord.get("channels", [])
    if not isinstance(channels, list):
        channels = [channels]
    channels = [str(c) for c in channels]
    if not channels:
        raise ConfigError("[discord].channels 가 비어 있습니다")

    output_dir = Path(output.get("dir", "output"))
    if not output_dir.is_absolute():
        output_dir = base_dir / output_dir

    provider = summary.get("provider", "api")
    if provider not in ("api", "claude_code"):
        raise ConfigError(f"[summary].provider 는 'api' 또는 'claude_code' 여야 합니다: {provider!r}")

    return Config(
        base_dir=base_dir,
        channels=channels,
        backfill_hours=float(discord.get("backfill_hours", 24)),
        min_messages=int(summary.get("min_messages", 5)),
        daily_digest_hour=int(summary.get("daily_digest_hour", 23)),
        timezone=ZoneInfo(summary.get("timezone", "Asia/Seoul")),
        model=summary.get("model", "claude-sonnet-5-5"),
        effort=summary.get("effort", "medium"),
        fallback=bool(summary.get("fallback", True)),
        language=summary.get("language", "ko"),
        chunk_chars=int(summary.get("chunk_chars", 60000)),
        output_dir=output_dir,
        webhook_url=os.environ.get("DISCORD_WEBHOOK_URL") or output.get("webhook_url", ""),
        provider=provider,
        claude_path=summary.get("claude_path", "claude"),
    )


def discord_token() -> str:
    token = os.environ.get("DISCORD_BOT_TOKEN", "").strip()
    if not token:
        raise ConfigError("DISCORD_BOT_TOKEN 이 설정되지 않았습니다 (.env 확인)")
    return token

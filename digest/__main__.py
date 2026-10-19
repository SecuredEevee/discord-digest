"""CLI 진입점. 외부 스케줄러(cron/launchd)가 30분마다 `python -m digest run` 을 호출한다."""

from __future__ import annotations

import argparse
import fcntl
import logging
import sys
import time
from contextlib import contextmanager
from datetime import date, datetime, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path

from . import pipeline, storage
from .config import Config, ConfigError, discord_token, load_config
from .discord_api import DiscordAccessError, DiscordClient

log = logging.getLogger("digest")


class AlreadyRunning(Exception):
    pass


@contextmanager
def single_instance(lock_path: Path):
    """flock 기반 락. 프로세스가 죽으면 OS가 락을 풀어주므로 stale lock 이 남지 않는다."""
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    f = open(lock_path, "w")
    try:
        fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        f.close()
        raise AlreadyRunning from None
    try:
        yield
    finally:
        fcntl.flock(f, fcntl.LOCK_UN)
        f.close()


def setup_logging(log_dir: Path) -> None:
    log_dir.mkdir(parents=True, exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    file_handler = RotatingFileHandler(
        log_dir / "digest.log", maxBytes=5 * 1024 * 1024, backupCount=5, encoding="utf-8"
    )
    stream_handler = logging.StreamHandler(sys.stderr)
    for h in (file_handler, stream_handler):
        h.setFormatter(fmt)
    logging.basicConfig(level=logging.INFO, handlers=[file_handler, stream_handler])
    # 라이브러리 로거가 요청 URL(웹훅 토큰 포함)이나 헤더를 남기지 않도록 한다
    for name in ("httpx", "httpcore", "httpx2", "anthropic"):
        logging.getLogger(name).setLevel(logging.WARNING)


def make_llm(cfg: Config):
    from .summarizer import AnthropicLLM, ClaudeCodeLLM

    if cfg.provider == "claude_code":
        return ClaudeCodeLLM(cfg.model, effort=cfg.effort, claude_path=cfg.claude_path)
    return AnthropicLLM(cfg.model, effort=cfg.effort, fallback=cfg.fallback)


def _channels(cfg: Config, only: str | None) -> list[str]:
    return [only] if only else cfg.channels


def cmd_fetch(cfg: Config, conn, args, summarize: bool = False) -> int:
    errors = 0
    llm = make_llm(cfg) if summarize else None
    with DiscordClient(discord_token()) as client:
        for channel_id in _channels(cfg, args.channel):
            started = time.monotonic()
            try:
                new = pipeline.fetch_channel(conn, client, channel_id, cfg.backfill_hours)
                log.info("[%s] 신규 메시지 %d개", channel_id, new)
            except DiscordAccessError as e:
                log.error("[%s] 접근 불가, 건너뜀 (토큰/권한 확인): %s", channel_id, e)
                errors += 1
                continue
            except Exception:
                log.exception("[%s] 수집 실패, 커서 유지", channel_id)
                errors += 1

            if llm is not None:
                try:
                    pipeline.maybe_periodic(conn, llm, cfg, channel_id)
                    pipeline.maybe_daily(conn, llm, cfg, channel_id)
                except Exception:
                    log.exception("[%s] 요약 실패, 다음 실행에서 재시도", channel_id)
                    errors += 1
            log.info("[%s] 소요 %.1fs", channel_id, time.monotonic() - started)
    return 1 if errors else 0


def cmd_backfill(cfg: Config, conn, args) -> int:
    errors = 0
    with DiscordClient(discord_token()) as client:
        for channel_id in _channels(cfg, args.channel):
            try:
                n = pipeline.backfill_channel(conn, client, channel_id, args.hours)
                log.info("[%s] backfill: 과거 메시지 %d개 추가", channel_id, n)
            except DiscordAccessError as e:
                log.error("[%s] 접근 불가, 건너뜀 (토큰/권한 확인): %s", channel_id, e)
                errors += 1
            except Exception:
                log.exception("[%s] backfill 실패 (저장된 배치는 유지됨, 다시 실행하면 이어서 채움)", channel_id)
                errors += 1
    return 1 if errors else 0


def cmd_summarize(cfg: Config, conn, args) -> int:
    since = datetime.fromisoformat(args.since)
    if since.tzinfo is None:
        since = since.replace(tzinfo=cfg.timezone)
    path = pipeline.summarize_since(conn, make_llm(cfg), cfg, args.channel, since)
    if path:
        print(path)
    return 0


def cmd_daily(cfg: Config, conn, args) -> int:
    llm = make_llm(cfg)
    errors = 0
    for channel_id in _channels(cfg, args.channel):
        try:
            path = pipeline.maybe_daily(conn, llm, cfg, channel_id, force=args.force)
            if path:
                print(path)
        except Exception:
            log.exception("[%s] 일일 다이제스트 실패", channel_id)
            errors += 1
    return 1 if errors else 0


def cmd_export(cfg: Config, conn, args) -> int:
    start = date.fromisoformat(args.date or args.since) if (args.date or args.since) else None
    end = date.fromisoformat(args.date or args.until) if (args.date or args.until) else None
    export_dir = args.out or cfg.base_dir / "export"
    for channel_id in _channels(cfg, args.channel):
        for path in pipeline.export_days(conn, cfg, channel_id, export_dir, start, end):
            print(path)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m digest")
    parser.add_argument("--config", type=Path, help="config.toml 경로")
    sub = parser.add_subparsers(dest="command", required=True)

    for name, help_ in (("run", "수집 + 조건 충족 시 요약"), ("fetch", "수집만")):
        p = sub.add_parser(name, help=help_)
        p.add_argument("--channel", help="특정 채널만")

    p = sub.add_parser("backfill", help="DB에 있는 가장 오래된 메시지 앞쪽 과거 구간만 추가 수집")
    p.add_argument("--channel", help="특정 채널만")
    p.add_argument("--hours", type=float, required=True, help="지금으로부터 몇 시간 전까지 채울지")

    p = sub.add_parser("summarize", help="지정 시각 이후 메시지 수동 요약")
    p.add_argument("--channel", required=True)
    p.add_argument("--since", required=True, help="예: 2026-10-18T00:00 (시간대 없으면 config timezone)")

    p = sub.add_parser("daily", help="일일 다이제스트 생성")
    p.add_argument("--channel", help="특정 채널만")
    p.add_argument("--force", action="store_true", help="시각/중복 조건 무시하고 생성")

    p = sub.add_parser("export", help="저장된 원문을 하루 단위 텍스트 파일로 내보내기 (기본: 전체 기간)")
    p.add_argument("--channel", help="특정 채널만")
    p.add_argument("--date", help="하루만. 예: 2026-10-18")
    p.add_argument("--since", help="이 날짜부터 (포함)")
    p.add_argument("--until", help="이 날짜까지 (포함)")
    p.add_argument("--out", type=Path, help="출력 폴더 (기본: export/)")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        cfg = load_config(args.config)
    except ConfigError as e:
        print(f"설정 오류: {e}", file=sys.stderr)
        return 2
    setup_logging(cfg.log_dir)

    if args.command == "export":
        conn = storage.connect(cfg.db_path)
        try:
            return cmd_export(cfg, conn, args)
        finally:
            conn.close()

    try:
        with single_instance(cfg.lock_path):
            started = time.monotonic()
            conn = storage.connect(cfg.db_path)
            try:
                if args.command == "run":
                    code = cmd_fetch(cfg, conn, args, summarize=True)
                elif args.command == "fetch":
                    code = cmd_fetch(cfg, conn, args)
                elif args.command == "backfill":
                    code = cmd_backfill(cfg, conn, args)
                elif args.command == "summarize":
                    code = cmd_summarize(cfg, conn, args)
                else:
                    code = cmd_daily(cfg, conn, args)
            finally:
                conn.close()
            log.info("%s 완료 (%.1fs, exit=%d) @ %s", args.command, time.monotonic() - started,
                     code, datetime.now(timezone.utc).isoformat(timespec="seconds"))
            return code
    except AlreadyRunning:
        log.warning("다른 인스턴스가 실행 중이라 종료합니다 (%s)", cfg.lock_path)
        return 0
    except ConfigError as e:
        log.error("설정 오류: %s", e)
        return 2


if __name__ == "__main__":
    sys.exit(main())

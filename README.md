<div align="center">

# discord-digest

**Turn a busy Discord channel into a calm daily digest.**

Fetch messages over the REST API, store them in SQLite, and let an LLM write
periodic summaries and a daily digest as Markdown (optionally pushed to a webhook).

[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
![Python](https://img.shields.io/badge/python-3.11%2B-3776AB.svg)
![Storage](https://img.shields.io/badge/storage-SQLite-003B57.svg)

**English** · [한국어](README.ko.md)

</div>

---

## Features

- **Incremental fetching** — cursor-based pagination (100 per page) with an initial backfill window, transactional per channel so a failure rolls back and resumes from the same point.
- **Resilient API client** — honors `429 retry_after` and rate-limit buckets, exponential backoff on 5xx/network errors, and skips only the affected channel on 401/403/404.
- **Periodic + daily summaries** — periodic summaries once enough new messages accumulate; a daily digest after a configurable local hour. Leftover messages are folded into the next run, never dropped.
- **Long-log friendly** — map-reduce chunking when the log exceeds `chunk_chars`.
- **Pluggable LLM** — use the Anthropic API, or a logged-in `claude -p` (no API key needed). Swapping in another model means implementing one method.
- **History backfill** — extend the stored history backwards without touching the cursor or duplicating rows.
- **Safe to schedule** — `flock`-based single instance, rotating logs, secrets never logged.

## Quick start

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt      # add requirements-dev.txt for tests
cp .env.example .env                           # fill in DISCORD_BOT_TOKEN (+ ANTHROPIC_API_KEY for provider = "api")
cp config.example.toml config.toml             # config.toml is git-ignored
```

Put the target channel ID(s) in `config.toml` under `[discord].channels`, then:

```bash
.venv/bin/python -m digest run
```

### Bot setup

- Developer Portal → Bot → enable **Message Content Intent** (otherwise message bodies arrive empty and a warning is logged).
- Grant the bot `View Channel` and `Read Message History` on the target channel.
- Self-bots (user account tokens) violate Discord's Terms of Service and are not supported.

## Commands

| Command | What it does |
| --- | --- |
| `python -m digest run` | Fetch, then summarize when conditions are met. This is what your scheduler calls. |
| `python -m digest fetch` | Fetch only. |
| `python -m digest backfill --hours 96` | Fill history *before* the oldest stored message, back to *now − hours*. Existing rows and the cursor are untouched; safe to re-run. |
| `python -m digest summarize --channel <id> --since 2026-10-18T00:00` | Manual summary of everything since a time. |
| `python -m digest daily --force` | Force-generate the daily digest. |

- `--since` without a timezone is interpreted in `config.toml`'s `timezone`.
- Every command takes a `flock` on `data/digest.lock` and exits immediately if another instance is running. The OS releases the lock if the process dies, so there are no stale locks.

## Configuration

`config.toml`:

| Key | Default | Description |
| --- | --- | --- |
| `[discord].channels` | — | Channel ID or list of IDs. |
| `[discord].backfill_hours` | `24` | How far back to fetch on the very first run. |
| `[summary].min_messages` | `5` | Minimum new messages before a periodic summary is written. |
| `[summary].daily_digest_hour` | `23` | Local hour after which the first run produces the daily digest. |
| `[summary].timezone` | `Asia/Seoul` | Day boundaries, output paths, and `[HH:MM]` stamps. |
| `[summary].provider` | `api` | `api` or `claude_code`. |
| `[summary].claude_path` | `claude` | Path to the `claude` binary (`claude_code` only). Use an absolute path under cron/launchd. |
| `[summary].model` | `claude-sonnet-5-5` | Model name. |
| `[summary].effort` | `medium` | `low` · `medium` · `high` · `xhigh` · `max`. |
| `[summary].fallback` | `true` | Server-side fallback on refusal (Claude API only). Set `false` on Bedrock/Vertex. |
| `[summary].language` | `ko` | `ko`, `en`, or `ja`. |
| `[summary].chunk_chars` | `60000` | Split into map-reduce chunks above this size. |
| `[output].dir` | `output` | Where Markdown files go. |
| `[output].webhook_url` | `""` | Optional. `DISCORD_WEBHOOK_URL` in `.env` takes precedence. |

### LLM providers

| Provider | Needs | Notes |
| --- | --- | --- |
| `api` | `ANTHROPIC_API_KEY` | Default. Uses server-side fallback on refusal. |
| `claude_code` | A logged-in `claude` CLI | Runs `claude -p` with **all tools disabled** in an empty temp directory. Shares your subscription usage with regular Claude Code, so raise `min_messages` if the channel is busy. |

To plug in another model, implement the `LLM` protocol in `digest/summarizer.py`:

```python
class LLM(Protocol):
    def summarize(self, text: str) -> str: ...
```

## How it works

1. **Fetch** — messages after `cursors.last_id`; the first run starts from a snowflake derived from `backfill_hours`. Storing messages and advancing the cursor happen in one transaction per channel.
2. **Rate limits** — `429` waits `retry_after`; an exhausted bucket waits `X-RateLimit-Reset-After`; 5xx and network errors back off at 2/4/8 s for up to 3 retries.
3. **Periodic summary** — written once `min_messages` have accumulated since the last one.
4. **Daily digest** — on the first run after `daily_digest_hour`, built from that day's periodic summaries plus the raw log. With a webhook configured it is sent in 2000-character parts with mentions disabled. If no run happens between that hour and midnight (e.g. the Mac is asleep), that day's digest is skipped.
5. **Chunking** — long inputs are summarized chronologically in chunks, then merged.

## Output

```
output/{channel_id}/{YYYY-MM-DD}/{HHMM}.md          # periodic summary
output/{channel_id}/{YYYY-MM-DD}/daily.md           # daily digest
output/{channel_id}/{YYYY-MM-DD}/manual-{HHMM}.md   # `summarize` command
logs/digest.log                                     # rotating, 5 MB × 5
```

Each summary uses four sections: **Main topics**, **Decisions**, **Open questions**, **Shared links**.

## Scheduling

**cron**

```cron
*/30 * * * * cd /path/to/discord-digest && .venv/bin/python -m digest run >> logs/cron.log 2>&1
```

**launchd (macOS)** — `~/Library/LaunchAgents/com.user.discord-digest.plist`

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.user.discord-digest</string>
    <key>ProgramArguments</key>
    <array>
        <string>/Users/YOU/path/to/discord-digest/.venv/bin/python</string>
        <string>-m</string>
        <string>digest</string>
        <string>run</string>
    </array>
    <key>WorkingDirectory</key>
    <string>/Users/YOU/path/to/discord-digest</string>
    <key>StartInterval</key>
    <integer>1800</integer>
    <key>StandardOutPath</key>
    <string>/Users/YOU/path/to/discord-digest/logs/launchd.out.log</string>
    <key>StandardErrorPath</key>
    <string>/Users/YOU/path/to/discord-digest/logs/launchd.err.log</string>
</dict>
</plist>
```

```bash
mkdir -p logs
launchctl load ~/Library/LaunchAgents/com.user.discord-digest.plist
```

> With `provider = "claude_code"`, the `claude` login may live in the macOS Keychain, which cron cannot always reach. If summaries fail with auth errors under cron, use launchd instead.

## Testing

```bash
.venv/bin/python -m pytest -q
```

Runs fully offline with `httpx.MockTransport` and a fake LLM, covering backfill, incremental fetch, pagination, `429` handling, cursor retention on failure, `min_messages`, once-a-day digests, single-instance locking, and token non-disclosure.

## License

[MIT](LICENSE) © 2026 SecuredEevee

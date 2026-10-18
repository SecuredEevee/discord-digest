# discord-digest

디스코드 채널 메시지를 30분마다 REST API로 수집해 SQLite에 저장하고, LLM으로 요약해 마크다운(선택적으로 웹훅)으로 내보낸다.

## 설치

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt      # 테스트까지: requirements-dev.txt
cp .env.example .env                           # DISCORD_BOT_TOKEN, ANTHROPIC_API_KEY 입력
```

`config.toml`의 `[discord].channels`에 대상 채널 ID를 넣는다.

### 봇 준비

- Developer Portal → Bot → **Message Content Intent** 켜기 (꺼져 있으면 본문이 빈 문자열로 온다. 이 경우 로그에 경고가 남는다)
- 대상 채널에 `View Channel`, `Read Message History` 권한
- 개인 계정 토큰(셀프봇)은 디스코드 약관 위반이라 지원하지 않는다

## 사용법

```
python -m digest run            # 수집 + 조건 충족 시 요약 (스케줄러가 호출)
python -m digest fetch          # 수집만
python -m digest summarize --channel <id> --since 2026-10-18T00:00
python -m digest daily --force  # 일일 다이제스트 강제 생성
```

- `--since`에 시간대가 없으면 `config.toml`의 `timezone` 기준으로 해석한다.
- 모든 명령은 `data/digest.lock`에 `flock`을 건다. 다른 인스턴스가 실행 중이면 바로 종료한다. 프로세스가 죽으면 OS가 락을 풀기 때문에 stale lock이 남지 않는다.

## 동작 요약

- **수집**: `cursors.last_id` 이후 메시지를 100개씩 페이지네이션. 최초 실행은 `backfill_hours` 전 시각의 snowflake부터. 채널별로 저장과 커서 갱신을 한 트랜잭션으로 묶어, 중간에 실패하면 롤백하고 다음 실행에서 같은 구간부터 다시 가져온다.
- **레이트 리밋**: 429는 `retry_after`만큼 대기 후 재시도, `X-RateLimit-Remaining: 0`이면 `X-RateLimit-Reset-After`만큼 대기, 5xx/네트워크 오류는 지수 백오프(2·4·8초)로 최대 3회. 401/403/404는 해당 채널만 건너뛴다.
- **주기 요약**: 마지막 주기 요약 이후 쌓인 메시지가 `min_messages` 이상일 때 생성. 미달인 회차의 메시지는 버리지 않고 다음 회차 요약에 합쳐진다.
- **일일 다이제스트**: `daily_digest_hour` 이후 첫 실행에서, 그날(설정 시간대 기준) 다이제스트가 없으면 그날의 주기 요약 + 원문으로 생성. 웹훅이 설정돼 있으면 2000자 단위로 나눠 전송한다(멘션 비활성화). 그 시각부터 자정 사이에 한 번도 실행되지 않으면(예: 맥이 잠자기 상태) 그날 다이제스트는 만들어지지 않으므로, 다음 날 `daily --force`는 오늘 날짜로 생성된다는 점에 주의.
- **청킹**: 입력이 `chunk_chars`를 넘으면 시간순으로 나눠 요약한 뒤 합친다(map-reduce).
- **provider**: `[summary].provider` 로 `api`(기본, `ANTHROPIC_API_KEY` 필요) 또는 `claude_code`(로그인된 `claude -p` 사용, API 키 불필요)를 고른다. `claude_code`는 도구를 끄고 빈 임시 디렉터리에서 실행하며, 구독 사용량을 평소 Claude Code와 공유한다. cron/launchd에서는 `claude_path`를 절대경로로 지정한다.
- **LLM 교체**: `digest/summarizer.py`의 `LLM` 프로토콜(`summarize(text: str) -> str`)만 구현하면 된다. 기본 구현은 `AnthropicLLM`이며, 거절(refusal) 시 서버측 fallback(`fallbacks="default"`)을 쓴다. Bedrock/Vertex 등 Claude API가 아닌 곳에서는 `fallback = false`로 둔다.

## 출력

```
output/{channel_id}/{YYYY-MM-DD}/{HHMM}.md        # 주기 요약
output/{channel_id}/{YYYY-MM-DD}/daily.md         # 일일 다이제스트
output/{channel_id}/{YYYY-MM-DD}/manual-{HHMM}.md # summarize 명령
logs/digest.log                                   # 5MB × 5 회전
```

## 스케줄링

**cron (Linux)**

```
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
        <string>/Users/YOU/Programming/Python/discord-digest/.venv/bin/python</string>
        <string>-m</string>
        <string>digest</string>
        <string>run</string>
    </array>
    <key>WorkingDirectory</key>
    <string>/Users/YOU/Programming/Python/discord-digest</string>
    <key>StartInterval</key>
    <integer>1800</integer>
    <key>StandardOutPath</key>
    <string>/Users/YOU/Programming/Python/discord-digest/logs/launchd.out.log</string>
    <key>StandardErrorPath</key>
    <string>/Users/YOU/Programming/Python/discord-digest/logs/launchd.err.log</string>
</dict>
</plist>
```

```bash
mkdir -p logs
launchctl load ~/Library/LaunchAgents/com.user.discord-digest.plist
```

## 테스트

```bash
.venv/bin/python -m pytest -q
```

네트워크 없이 `httpx.MockTransport`와 가짜 LLM으로 완료 기준(백필, 증분 수집, 페이지네이션, 429, 실패 시 커서 유지, `min_messages`, 일일 1회, 동시 실행 방지, 토큰 비노출)을 검증한다.

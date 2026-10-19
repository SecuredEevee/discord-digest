<div align="center">

# discord-digest

**디스코드 채널 대화를 자동으로 모아서 요약해 주는 도구**

메시지를 주기적으로 받아 SQLite에 쌓아 두고, LLM으로 정리한 요약을 마크다운 파일로 남깁니다.
웹훅을 설정하면 일일 요약을 디스코드로 보내 줍니다.

[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
![Python](https://img.shields.io/badge/python-3.11%2B-3776AB.svg)
![Storage](https://img.shields.io/badge/storage-SQLite-003B57.svg)

[English](README.md) · **한국어**

</div>

---

## 주요 기능

- **이어받기 수집**: 마지막으로 받은 메시지 이후부터만 가져옵니다. 중간에 실패하면 저장한 걸 되돌리고 다음 실행 때 같은 지점부터 다시 받습니다.
- **레이트 리밋 처리**: 429 응답이 오면 안내된 시간만큼 기다리고, 서버 오류나 네트워크 오류는 간격을 늘려 가며 재시도합니다. 권한이 없는 채널은 건너뛰고 나머지는 계속 진행합니다.
- **주기 요약과 일일 요약**: 새 메시지가 일정 개수 이상 쌓이면 요약을 만들고, 정해 둔 시각이 지나면 하루치 요약을 따로 만듭니다. 개수가 모자란 회차의 메시지는 다음 요약에 합쳐집니다.
- **긴 대화도 처리**: 한 번에 넣기엔 너무 길면 시간 순으로 잘라서 요약한 뒤 다시 합칩니다.
- **LLM 선택**: Anthropic API 키로 돌리거나, 키 없이 로그인된 `claude -p`로 돌릴 수 있습니다. 다른 모델을 붙이고 싶으면 메서드 하나만 구현하면 됩니다.
- **과거 기록 추가 수집**: 이미 받은 기록보다 더 이전 구간을 나중에 채워 넣을 수 있습니다. 기존 데이터는 그대로 둡니다.
- **스케줄러에 걸어 두기 좋음**: 동시에 두 번 실행되지 않게 막아 두었고, 로그는 크기가 차면 자동으로 교체됩니다. 토큰은 로그에 남지 않습니다.

## 시작하기

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt      # 테스트도 돌리려면 requirements-dev.txt
cp .env.example .env                           # DISCORD_BOT_TOKEN 입력 (provider가 api면 ANTHROPIC_API_KEY도)
cp config.example.toml config.toml             # config.toml은 git에 올라가지 않습니다
```

`config.toml`의 `[discord].channels`에 채널 ID를 넣고 실행합니다.

```bash
.venv/bin/python -m digest run
```

### 봇 설정

- Developer Portal → Bot에서 **Message Content Intent**를 켜 주세요. 꺼져 있으면 메시지 본문이 비어서 들어옵니다. 이 경우 로그에 경고가 찍힙니다.
- 봇에게 대상 채널의 `View Channel`, `Read Message History` 권한이 있어야 합니다.
- 개인 계정 토큰(셀프봇)은 디스코드 약관 위반이라 지원하지 않습니다.

## 명령어

| 명령 | 설명 |
| --- | --- |
| `python -m digest run` | 수집하고, 조건이 맞으면 요약까지 합니다. 스케줄러에는 이걸 걸어 두면 됩니다. |
| `python -m digest fetch` | 수집만 합니다. |
| `python -m digest backfill --hours 96` | 지금부터 96시간 전까지, 아직 안 받은 과거 구간만 채웁니다. 여러 번 실행해도 중복되지 않습니다. |
| `python -m digest summarize --channel <id> --since 2026-10-18T00:00` | 지정한 시각 이후 대화를 바로 요약합니다. |
| `python -m digest daily --force` | 일일 요약을 지금 바로 만듭니다. |
| `python -m digest export [--date D \| --since D --until D]` | 저장된 원문 전체를 하루에 파일 하나씩 `export/{channel_id}/{YYYY-MM-DD}.md`로 내보냅니다. 요약은 들어가지 않습니다. 다른 LLM에 하루치 대화를 넘길 때 쓰기 좋습니다. 읽기만 하므로 다른 실행이 돌고 있어도 기다리지 않습니다. |

- `--since`에 시간대를 안 붙이면 `config.toml`의 `timezone` 기준으로 봅니다.
- 모든 명령은 실행 중에 `data/digest.lock`을 잡습니다. 이미 다른 실행이 돌고 있으면 바로 종료합니다. 프로세스가 비정상 종료돼도 락은 OS가 풀어 주니 따로 지울 필요는 없습니다.

## 설정

`config.toml`

| 키 | 기본값 | 설명 |
| --- | --- | --- |
| `[discord].channels` | - | 채널 ID. 여러 개면 리스트로 씁니다. |
| `[discord].backfill_hours` | `24` | 처음 실행할 때 몇 시간 전까지 가져올지 |
| `[summary].min_messages` | `5` | 주기 요약을 만들기 위한 최소 메시지 수 |
| `[summary].daily_digest_hour` | `23` | 이 시각이 지난 뒤 첫 실행에서 일일 요약을 만듭니다 |
| `[summary].timezone` | `Asia/Seoul` | 날짜 구분, 파일 경로, 메시지 시각 표기에 쓰는 시간대 |
| `[summary].provider` | `api` | `api` 또는 `claude_code` |
| `[summary].claude_path` | `claude` | `claude` 실행 파일 경로 (`claude_code`일 때만). cron이나 launchd에서는 절대경로로 적어 주세요 |
| `[summary].model` | `claude-sonnet-5-5` | 사용할 모델 |
| `[summary].effort` | `medium` | `low`, `medium`, `high`, `xhigh`, `max` |
| `[summary].fallback` | `true` | 요약을 거절당했을 때 다른 모델로 재시도 (Claude API에서만 동작). Bedrock이나 Vertex에서는 `false`로 |
| `[summary].language` | `ko` | `ko`, `en`, `ja` |
| `[summary].chunk_chars` | `60000` | 대화가 이 글자 수보다 길면 나눠서 요약합니다 |
| `[output].dir` | `output` | 요약 파일 저장 위치 |
| `[output].webhook_url` | `""` | 선택. `.env`의 `DISCORD_WEBHOOK_URL`이 있으면 그걸 씁니다 |

### provider 고르기

| provider | 필요한 것 | 참고 |
| --- | --- | --- |
| `api` | `ANTHROPIC_API_KEY` | 기본값입니다. |
| `claude_code` | `claude` CLI 로그인 | 키 없이 구독 계정으로 돌아갑니다. 파일 수정이나 명령 실행은 못 하게 도구를 전부 끄고, 빈 임시 폴더에서 실행합니다. 평소 쓰는 Claude Code와 사용량을 같이 쓰기 때문에, 채널이 활발하면 `min_messages`를 올려 두는 게 좋습니다. |

다른 모델을 쓰고 싶으면 `digest/summarizer.py`의 `LLM` 인터페이스만 맞춰 주면 됩니다.

```python
class LLM(Protocol):
    def summarize(self, text: str) -> str: ...
```

## 내부 동작

1. **수집**: `cursors.last_id` 이후 메시지를 100개씩 받습니다. 처음 실행할 때는 `backfill_hours` 전 시점부터 받습니다. 메시지 저장과 커서 갱신은 채널마다 한 트랜잭션으로 처리합니다.
2. **레이트 리밋**: 429면 `retry_after`만큼, 버킷이 바닥나면 `X-RateLimit-Reset-After`만큼 기다립니다. 5xx와 네트워크 오류는 2초, 4초, 8초 간격으로 최대 3번 재시도합니다.
3. **주기 요약**: 직전 주기 요약 이후 메시지가 `min_messages`개 이상 쌓이면 만듭니다.
4. **일일 요약**: `daily_digest_hour`가 지난 뒤 첫 실행에서 그날의 주기 요약과 원문을 합쳐 만듭니다. 웹훅이 있으면 2000자씩 나눠 보내고, 멘션은 걸리지 않게 막아 둡니다. 그 시각부터 자정까지 한 번도 실행되지 않으면(맥이 잠들어 있었다든지) 그날 요약은 건너뜁니다.
5. **나눠서 요약**: 대화가 길면 시간 순으로 잘라 각각 요약한 뒤 하나로 합칩니다.

## 결과물

```
output/{channel_id}/{YYYY-MM-DD}/{HHMM}.md          # 주기 요약
output/{channel_id}/{YYYY-MM-DD}/daily.md           # 일일 요약
output/{channel_id}/{YYYY-MM-DD}/manual-{HHMM}.md   # summarize로 만든 요약
logs/digest.log                                     # 5MB씩 최대 5개 보관
```

요약은 항상 **주요 주제 / 결정·합의 사항 / 열린 질문 / 공유된 링크·자료** 네 부분으로 나옵니다.

## 자동 실행

**cron**

```cron
*/30 * * * * cd /path/to/discord-digest && .venv/bin/python -m digest run >> logs/cron.log 2>&1
```

**launchd (macOS)**: `~/Library/LaunchAgents/com.user.discord-digest.plist`

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

> `claude_code`로 쓰는 경우, `claude` 로그인 정보가 macOS 키체인에 저장돼 있으면 cron에서는 못 읽을 수 있습니다. cron에서 요약만 인증 오류로 실패한다면 launchd로 바꿔 보세요.

## 테스트

```bash
.venv/bin/python -m pytest -q
```

실제 네트워크나 LLM은 쓰지 않습니다. 가짜 디스코드 서버와 가짜 LLM으로 백필, 이어받기, 페이지 넘김, 429 처리, 실패 시 커서 유지, 최소 메시지 수, 하루 한 번 생성, 중복 실행 방지, 토큰 노출 여부를 확인합니다.

## 라이선스

[MIT](LICENSE) © 2026 SecuredEevee

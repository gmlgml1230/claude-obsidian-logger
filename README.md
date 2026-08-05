# claude-obsidian-logger

Claude Code 세션을 Obsidian vault에 자동 기록하는 훅 2개.

세션이 끝나면 그날 한 일을 요약해 **주제별 마크다운**에 쌓고, 다음 세션 시작 때
진행 중인 주제 목록을 컨텍스트에 주입한다. 벡터DB도 상주 프로세스도 없다.

## 산출물

```
$OBSIDIAN_VAULT/
├── INDEX.md                    주제 진입점 (자동 생성, SessionStart에 주입)
├── topics/<slug>.md            주제 1개 = 파일 1개
│                                 🔜 다음 · 📌 결론 · ❌ 접은 안 — 사람이 유지
│                                 📈 진행 로그              — 훅이 append
├── conversations/<sid8>_<날짜>.md   그날 대화 원문
├── daily/<날짜>.md             그날 한 줄 (0토큰)
├── weekly/<ISO주차>.md         주간 집계 (0토큰)
└── 📌 작업현황.md               체크박스 태스크
```

## 설치

원하는 곳에 clone 한다. 훅은 경로만 맞으면 어디 있어도 된다.
다만 **`~/.claude/hooks/`는 피한다** — 다른 훅들이 함께 쓰는 공용 공간이라 레포를 두면 서로 간섭한다.

`~/.claude/settings.json`에 훅을 배선하고 **command 경로를 clone 위치로 맞춘다** (`settings.example.json` 참고):

```json
{
  "hooks": {
    "SessionEnd":   [{ "hooks": [{ "type": "command", "command": "python3 ~/claude-obsidian-logger/hooks/session_log.py" }] }],
    "SessionStart": [{ "hooks": [{ "type": "command", "command": "python3 ~/claude-obsidian-logger/hooks/session_start_inject.py" }] }]
  }
}
```

`claude` CLI가 PATH에 있어야 한다 (요약에 `claude -p` 사용).

## 설정 (환경변수)

| 변수 | 기본값 | 설명 |
|---|---|---|
| `OBSIDIAN_VAULT` | `~/Documents/Obsidian` | vault 경로 |
| `SESSIONLOG_MODEL` | `claude-sonnet-4-6` | 요약 모델 |
| `SESSIONLOG_TASKS_FILE` | `📌 작업현황.md` | 태스크 파일명 |
| `SESSIONLOG_STATE_DIR` | `~/.claude/hooks` | 상태 파일 위치 (아래) |

### 코드와 상태의 분리

증분 마커 DB(`sessionlog.db`)·락·디버그 로그는 **레포 밖**(`SESSIONLOG_STATE_DIR`)에 둔다.
clone 위치를 옮겨도 기록이 이어지고, 레포에는 로컬 상태가 섞이지 않는다.

> ⚠️ 이 경로를 바꾸면 증분 마커가 초기화되어 **전 세션이 재요약되고 진행 로그가 중복**된다.
> 옮겨야 한다면 `sessionlog.db`를 함께 옮길 것.

## 기록 제외

| 마커 | 범위 |
|---|---|
| `#nolog` · `#기록제외` · `#skiplog` | 그 세션 증분 **전체** |
| `#로그` (줄 단독) | 그 줄부터 **메시지 끝까지**. 붙여넣은 로그용 |

```
왜 이 에러가 나지?        ← 남음
#로그
app-1 | DEBUG | ...       ← 제외 (줄 수 무관)
```

펜스로 감싸면(` ```#로그 … ``` `) 블록만 제외하고 뒤 내용은 남는다.

## 동작

- **활동일 단위로 1콜.** 세션이 며칠에 걸쳐도 날짜별로 나눠 각각 요약한다.
- **증분.** 처리한 turn 수를 SQLite에 기록해 재요약하지 않는다.
- **기록 여부는 기계 판정.** 분량 게이트만 쓰고 LLM에게 묻지 않는다 (같은 대화가 날마다 다르게 처리되는 것을 막는다).
- **주제는 닫힌 선택지.** `topics/`에 이미 있는 슬러그 중에서만 고르고, 없으면 `daily/`에만 기록한다. 훅이 새 주제를 만들지 않는다.
- **도구 호출/결과와 `#로그` 블록은 저장·요약 양쪽에서 제외**하되 turn 구조는 보존한다(증분 마커가 깨지지 않도록).

## 실측

같은 vault 기준으로 측정한 값이다.

| | 적용 전 | 적용 후 |
|---|---:|---:|
| 대화 저장 용량 | 13.5 MB | 7.8 MB |
| 요약 프롬프트 15,000자 중 실제 대화 | 5,338자 | 14,735자 |
| 콜당 비용 (Sonnet 4.6) | \$0.03~0.09 | 동일 |

도구 노이즈가 요약 프롬프트의 52.7%를 차지하고 있었다. 제거해도 프롬프트 크기는
예산에 묶여 그대로이고, 그 안의 실제 대화만 2.76배가 된다.

## 한계

- **한국어 전용** — 프롬프트·섹션 헤더·기본 파일명이 한국어다.
- **macOS에서만 검증** — `fcntl` 사용. Linux는 동작할 것으로 보이나 미검증, Windows 미지원.
- 테스트 없음. vault 구조가 고정되어 있다.

## 안전장치

`--dry-run` / `--dry-run-llm`으로 실파일 변경 없이 임시 디렉토리에 산출물을 만들어 확인할 수 있다.

```bash
python3 session_log.py --dry-run-llm <transcript.jsonl> --out /tmp/check
```

트랜스크립트는 `~/.claude/projects/<프로젝트>/<세션id>.jsonl`에 있다.

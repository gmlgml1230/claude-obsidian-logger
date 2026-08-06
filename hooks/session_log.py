#!/usr/bin/env python3
"""
session_log.py — Claude Code SessionEnd hook (증분 진행 로그 재설계).

세션(=프로젝트)을 유지하며 긴 작업을 이어가도, '활동한 날짜'에 정확히 기록한다.
매 SessionEnd에서 '지난 처리 이후 새 turn만' 요약해 append(덮어쓰기 X).

산출물:
  - sessions/<시작일_시각_주제_sid8>.md : 프로젝트 허브 (🔜 다음 + 📈 진행 로그[날짜별])
  - conversations/<sid8>_<날짜>.md       : 그날 새 대화 (상단에 허브 역링크)
  - daily/<활동일>.md                    : 그날 진행 한 줄
  - weekly/<ISO주차>.md                  : 주간 다이제스트 (daily 기반 집계)
  - 📌 작업현황.md / 완료 아카이브.md      : 태스크 (단일 소스, 체크박스 완료, 2주 아카이브)

증분 상태(세션별 처리한 turn 수)는 SQLite(sessionlog.db)에 저장.
특정 세션 제외: user가 #nolog / #기록제외 / #skiplog 를 치면 기록 안 함.

사용:
  실제 hook :  cat <stdin-json> | session_log.py
  워커      :  session_log.py --worker <transcript.jsonl>
  dry-run   :  session_log.py --dry-run[-llm] <transcript.jsonl> [--out <dir>]
"""

import os
import sys
import json
import re
import glob
import fcntl
import difflib
import sqlite3
import contextlib
import subprocess
from datetime import datetime, timedelta

# ── 설정값 ──────────────────────────────────────────────────────────
VAULT = os.environ.get("OBSIDIAN_VAULT") or os.path.expanduser("~/Documents/Obsidian")
SUMMARY_MODEL = os.environ.get("SESSIONLOG_MODEL", "claude-sonnet-4-6")
TASKS_FILENAME = os.environ.get("SESSIONLOG_TASKS_FILE", "📌 작업현황.md")
TASKS_OPEN_HEADER = "## 🔧 진행 중"
TASKS_DONE_HEADER = "## ✅ 완료"
DONE_RETAIN_DAYS = 14
ARCHIVE_FILENAME = "완료 아카이브.md"
GUARD_ENV = "CLAUDE_SESSIONLOG_RUNNING"

MAX_TOOL_RESULT_CHARS = 280
CLAUDE_TIMEOUT_SEC = 300

MIN_USER_CHARS = 12
MIN_TOTAL_CHARS = 60
EXCLUDE_MARKERS = ("#nolog", "#기록제외", "#skiplog")   # 세션 증분 전체 제외
# 태스크만 만들지 않는다. 기록(진행 로그·대화·주제 매칭)은 그대로 간다.
# EXCLUDE_MARKERS 와 역할이 다르다 — 이쪽은 '한 일은 남기되 할 일은 안 만든다'.
TASK_SKIP_MARKERS = ("#완료", "#done")
SUMMARY_CHAR_BUDGET = 15000
SUMMARY_TURN_MAX = 2000
SUMMARY_MAX_TRIES = 2

# ── 붙여넣기 대응 ───────────────────────────────────────────────────
# '#로그' 단독 줄 → 그 줄부터 메시지 끝까지 제외(줄 수·크기 무관).
# EXCLUDE_MARKERS(세션 전체 제외)와 역할이 다르다: 이쪽은 메시지 단위.
LOG_MARKER_RE = re.compile(r"^[ \t]*#로그[ \t]*$", re.M)
LOG_FENCED_RE = re.compile(r"^[ \t]*#로그[ \t]*\n```[^\n]*\n.*?\n```[ \t]*$", re.M | re.S)
# turn당 상한. 실측 User 발화 99백분위 = 19,392자 → 정상 발화 무영향, 전체의 0.20%만 대상
PASTE_CAP = 20000
TOOL_LINE_PREFIXES = ("⌘ ", "→ 도구", "← 도구")

# ── 상태 파일 위치 ──────────────────────────────────────────────────
# 증분 마커 DB·락·디버그 로그가 있는 곳. **코드 위치와 분리한다.**
# 스크립트를 어디에 두든(레포 clone 위치가 바뀌어도) 같은 sessionlog.db 를 계속 쓰기 위함.
# ⚠️ 이 경로가 바뀌면 증분 마커가 초기화되어 전 세션이 재요약되고 진행 로그가 중복된다.
STATE_DIR = os.environ.get("SESSIONLOG_STATE_DIR") or os.path.expanduser("~/.claude/hooks")
os.makedirs(STATE_DIR, exist_ok=True)

LOCK_FILE = os.path.join(STATE_DIR, ".sessionlog.lock")
TASK_BACKUP_KEEP = 10
DEBUG_LOG_DIR = STATE_DIR
DEBUG_LOG_KEEP_DAYS = 7

DB_FILE = os.path.join(STATE_DIR, "sessionlog.db")
CONV_DIRNAME = "conversations"
PROGRESS_HEADER = "## 📈 진행 로그"
NEXT_HEADER = "## 🔜 다음"

# ── 주제축 (topics/) ────────────────────────────────────────────────
TOPICS_DIRNAME = "topics"
INDEX_FILENAME = "INDEX.md"
TASK_TITLE_MAX = 80
CONCLUSION_HEADER = "## 📌 결론"
DROPPED_HEADER = "## ❌ 접은 안"


# ── 디버그 로그 ─────────────────────────────────────────────────────
def _debug(msg):
    try:
        now = datetime.now()
        path = os.path.join(DEBUG_LOG_DIR, f"sessionlog_debug_{now:%Y-%m-%d}.log")
        with open(path, "a", encoding="utf-8") as f:
            f.write(f"{now.isoformat(timespec='seconds')} {msg}\n")
    except Exception:
        pass


def _rotate_debug_logs():
    try:
        cutoff = datetime.now().timestamp() - DEBUG_LOG_KEEP_DAYS * 86400
        for p in glob.glob(os.path.join(DEBUG_LOG_DIR, "sessionlog_debug_*.log")):
            if os.path.getmtime(p) < cutoff:
                os.remove(p)
    except Exception:
        pass


# ── SQLite 증분 상태 ────────────────────────────────────────────────
def _db(db_path):
    conn = sqlite3.connect(db_path, timeout=30)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("CREATE TABLE IF NOT EXISTS session_state "
                 "(session_id TEXT PRIMARY KEY, processed_turns INTEGER)")
    return conn


def db_get_processed(sid, db_path=DB_FILE):
    try:
        with _db(db_path) as c:
            r = c.execute("SELECT processed_turns FROM session_state WHERE session_id=?",
                          (sid,)).fetchone()
            return r[0] if r else 0
    except Exception:
        return 0


def db_set_processed(sid, n, db_path=DB_FILE):
    try:
        with _db(db_path) as c:
            c.execute("INSERT INTO session_state(session_id, processed_turns) VALUES(?,?) "
                      "ON CONFLICT(session_id) DO UPDATE SET processed_turns=?", (sid, n, n))
    except Exception as e:
        _debug("db_set ERROR: " + repr(e))


def _db_tasks(db_path):
    c = _db(db_path)
    c.execute("CREATE TABLE IF NOT EXISTS task_snapshot (task_key TEXT PRIMARY KEY, status TEXT)")
    c.execute("CREATE TABLE IF NOT EXISTS task_events (task_key TEXT, status TEXT, ts TEXT)")
    return c


def _task_states(md):
    """작업현황 마크다운 → {task_key: 'open'|'done'}."""
    states = {}
    for ln in (md or "").splitlines():
        low = ln.lstrip().lower()
        if low.startswith("- [ ]"):
            states[_task_key(ln)] = "open"
        elif low.startswith("- [x]"):
            states[_task_key(ln)] = "done"
    return states


def _sync_task_states(base, db_path=DB_FILE):
    """작업현황 현재 상태 vs snapshot diff → open→done 전이를 그 시각으로 task_events에 기록.
    FileChanged hook(외부 편집 즉시) + SessionEnd(폴백) 양쪽에서 호출."""
    tf = os.path.join(base, TASKS_FILENAME)
    if not os.path.exists(tf):
        return
    cur = _task_states(open(tf, encoding="utf-8").read())
    now = datetime.now().isoformat(timespec="seconds")
    try:
        with _db_tasks(db_path) as c:
            prev = dict(c.execute("SELECT task_key, status FROM task_snapshot").fetchall())
            for k, st in cur.items():
                if st == "done" and prev.get(k) != "done":
                    c.execute("INSERT INTO task_events(task_key, status, ts) VALUES(?,?,?)",
                              (k, "done", now))
            c.execute("DELETE FROM task_snapshot")
            c.executemany("INSERT INTO task_snapshot(task_key, status) VALUES(?,?)",
                          list(cur.items()))
    except Exception as e:
        _debug("sync_task ERROR: " + repr(e))


def _completion_date(task_key, fallback, db_path=DB_FILE):
    """task_events에서 실제 완료 시각(날짜) 조회. 없으면 fallback."""
    try:
        with _db_tasks(db_path) as c:
            r = c.execute("SELECT ts FROM task_events WHERE task_key=? AND status='done' "
                          "ORDER BY ts LIMIT 1", (task_key,)).fetchone()
            if r and r[0]:
                return r[0][:10]
    except Exception:
        pass
    return fallback


# ── 트랜스크립트 파싱 ───────────────────────────────────────────────
def _strip_command_noise(s):
    s = re.sub(r"<local-command-caveat>.*?</local-command-caveat>", "", s, flags=re.S)
    s = re.sub(r"<command-message>.*?</command-message>", "", s, flags=re.S)
    s = re.sub(r"<command-args>.*?</command-args>", "", s, flags=re.S)
    s = re.sub(r"<local-command-stdout>.*?</local-command-stdout>", "", s, flags=re.S)
    s = re.sub(r"<system-reminder>.*?</system-reminder>", "", s, flags=re.S)
    s = re.sub(r"<command-name>(.*?)</command-name>", r"⌘ \1", s, flags=re.S)
    return s.strip()


def _text_from_content(content):
    if isinstance(content, str):
        cleaned = _strip_command_noise(content)
        return [cleaned] if cleaned.strip() else []
    lines = []
    if isinstance(content, list):
        for block in content:
            if not isinstance(block, dict):
                continue
            btype = block.get("type")
            if btype == "text":
                t = block.get("text", "").strip()
                if t:
                    lines.append(t)
            elif btype == "thinking":
                continue
            elif btype == "tool_use":
                lines.append(f"→ 도구 호출: {block.get('name', '?')}")
            elif btype == "tool_result":
                raw = block.get("content", "")
                if isinstance(raw, list):
                    raw = " ".join(b.get("text", "") for b in raw if isinstance(b, dict))
                raw = str(raw).replace("\n", " ").strip()
                if len(raw) > MAX_TOOL_RESULT_CHARS:
                    raw = raw[:MAX_TOOL_RESULT_CHARS] + f"… (총 {len(raw)}자)"
                lines.append(f"← 도구 결과: {raw}" if raw else "← 도구 결과")
    return lines


# ── 렌더 공용 필터 ──────────────────────────────────────────────────
# 주의: 여기서 거르고 _text_from_content 는 건드리지 않는다.
# _text_from_content 를 고치면 도구만 있던 turn 이 `if not lines: continue`(아래)로
# 사라져 turn 수가 8,810→1,853 수준으로 줄고, 증분 마커(processed_turns)가 리셋되어
# 세션 전량 재요약 + 진행 로그 중복 append 가 발생한다.
def _strip_log_blocks(text):
    """'#로그' 마커 적용.

    - 펜스 형태(```#로그 … ```)는 그 블록만 제외하고 뒤 내용을 보존
    - 단독 마커 줄은 그 줄부터 메시지 끝까지 제외 (줄 수·크기 무관)
    """
    if "#로그" not in text:
        return text
    text = LOG_FENCED_RE.sub("…(#로그 블록 생략)", text)
    m = LOG_MARKER_RE.search(text)
    if m:
        dropped = len(text) - m.start()
        text = text[:m.start()] + f"…(#로그 이후 {dropped:,}자 생략)"
    return text.strip()


def _clean_lines(lines):
    """도구 라인 제거 + '#로그' 블록 제거. 저장(_render_turns)·요약·게이트 공용."""
    out = []
    for ln in lines:
        if ln.startswith(TOOL_LINE_PREFIXES):
            continue
        s = _strip_log_blocks(ln)
        if s.strip():
            out.append(s)
    return out


def _capped(body, cap=PASTE_CAP):
    """turn 하나가 상한을 넘으면 잘라내고 생략 표시를 남긴다."""
    if cap and len(body) > cap:
        return body[:cap] + f"\n\n…({len(body) - cap:,}자 생략 — 붙여넣기 {cap:,}자 상한)"
    return body


def parse_transcript(path):
    title = None
    turns = []  # (role, [lines], ts)
    first_ts = last_ts = None
    cwd = git_branch = None
    in_tok = out_tok = 0

    with open(path, encoding="utf-8") as fh:
        for ln in fh:
            ln = ln.strip()
            if not ln:
                continue
            try:
                o = json.loads(ln)
            except json.JSONDecodeError:
                continue
            t = o.get("type")
            if t == "ai-title":
                title = o.get("aiTitle") or title
                continue
            if t not in ("user", "assistant"):
                continue
            ts = o.get("timestamp")
            if ts:
                first_ts = first_ts or ts
                last_ts = ts
            cwd = o.get("cwd") or cwd
            git_branch = o.get("gitBranch") or git_branch
            msg = o.get("message") or {}
            role = msg.get("role", t)
            lines = _text_from_content(msg.get("content"))
            if role == "assistant":
                usage = msg.get("usage") or {}
                in_tok = max(in_tok, usage.get("input_tokens", 0) or 0)
                out_tok += usage.get("output_tokens", 0) or 0
            if not lines:
                continue
            turns.append((role, lines, ts))

    return {
        "title": title, "turns": turns,
        "first_ts": first_ts, "last_ts": last_ts,
        "cwd": cwd, "git_branch": git_branch,
        "in_tok": in_tok, "out_tok": out_tok,
        "session_id": os.path.splitext(os.path.basename(path))[0],
    }


# ── 게이트 ──────────────────────────────────────────────────────────
def _significance(meta):
    """도구 라인과 '#로그' 블록을 제외한 실질 분량으로 판정.
    로그만 붙여넣은 세션이 게이트를 통과하는 것을 막는다."""
    total = real_user = 0
    for role, lines, _ in meta["turns"]:
        for ln in _clean_lines(lines):
            t = ln.strip()
            total += len(t)
            if role == "user":
                real_user += len(t)
    return total, real_user


def is_significant(meta):
    total, real_user = _significance(meta)
    return real_user >= MIN_USER_CHARS and total >= MIN_TOTAL_CHARS


def _has_marker(meta, markers):
    """user 발화에서 마커를 찾는다. 도구 라인은 사용자가 친 것이 아니므로 제외."""
    for role, lines, _ in meta["turns"]:
        if role != "user":
            continue
        for ln in lines:
            if ln.startswith(("← 도구", "→ 도구", "⌘ ")):
                continue
            if any(m in ln.lower() for m in markers):
                return True
    return False


def _is_excluded(meta):
    return _has_marker(meta, EXCLUDE_MARKERS)


def _is_task_skipped(meta):
    """'#완료' — 태스크만 만들지 않는다.

    판정을 LLM 에게 묻지 않는 이유는 아래 주석과 같다(비결정적).
    #nolog 와 달리 기록은 그대로 남는다 — 한 일은 남기되 할 일만 안 만든다.
    """
    return _has_marker(meta, TASK_SKIP_MARKERS)


# 기록 여부는 기계 게이트(is_significant)만으로 결정한다.
# LLM 판정(worth_logging)은 비결정적이라 제거 — 같은 대화가 날마다 다르게 처리되는 것을 막음.


# ── 시간·문자열 유틸 ────────────────────────────────────────────────
def _fmt_ts(ts):
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone()
    except (ValueError, AttributeError):
        return None


def _turn_date(ts, fallback):
    d = _fmt_ts(ts)
    return d.strftime("%Y-%m-%d") if d else fallback


def _slug(title):
    s = (title or "untitled").strip().lower()
    s = re.sub(r"[^\w가-힣]+", "-", s).strip("-")
    return s[:50] or "untitled"


def _yaml_val(v):
    return '"' + str(v).replace("\n", " ").replace('"', "'") + '"'


def _project_of(meta):
    return (meta.get("cwd") or "?").rstrip("/").split("/")[-1] or "?"


# ── 요약 (claude -p) ────────────────────────────────────────────────
def _first_para(text, limit=400):
    """'[Assistant]' 헤더 + 첫 문단만 남긴다. 결과 요지는 담되 분량을 억제."""
    head, _, body = text.partition("\n")
    para = body.strip().split("\n\n")[0].strip()
    if not para:
        return ""
    if len(para) > limit:
        para = para[:limit] + " …"
    return f"{head}\n{para}"


def render_conversation_for_summary(meta):
    texts = []
    for role, lines, _ in meta["turns"]:
        ls = _clean_lines(lines)
        if not ls:
            continue  # 도구/로그만 있던 turn 은 요약 대상에서 제외
        who = "User" if role == "user" else "Assistant"
        t = f"[{who}]\n" + "\n".join(ls)
        if len(t) > SUMMARY_TURN_MAX:
            t = t[:SUMMARY_TURN_MAX] + " …(생략)"
        texts.append((role, t))
    n = len(texts)
    keep = [False] * n
    trimmed = {}  # index → 축약본(Assistant 첫 문단)
    used = 0
    tail_cap = SUMMARY_CHAR_BUDGET * 2 // 3

    # 1단계: 뒤에서부터(최신 우선) tail_cap 까지 — User·Assistant 모두
    last_kept = n
    for i in range(n - 1, -1, -1):
        t = texts[i][1]
        if used and used + len(t) > tail_cap:
            break
        keep[i] = True
        used += len(t)
        last_kept = i

    # 2단계: 그 앞쪽에서 User 발화 + 직후 Assistant 첫 문단까지 (결정 V)
    #   User 만 담으면 "무엇을 시켰나"는 남고 "결과가 뭐였나"가 사라진다.
    for i in range(last_kept - 1, -1, -1):
        if texts[i][0] != "user":
            continue
        a_idx, a_txt = None, ""
        if i + 1 < n and not keep[i + 1] and texts[i + 1][0] == "assistant":
            a_txt = _first_para(texts[i + 1][1])
            if a_txt:
                a_idx = i + 1
        add = len(texts[i][1]) + len(a_txt)
        if used and used + add > SUMMARY_CHAR_BUDGET:
            break
        keep[i] = True
        used += len(texts[i][1])
        if a_idx is not None:
            keep[a_idx] = True
            trimmed[a_idx] = a_txt
            used += len(a_txt)

    parts = []
    prev = -1
    for i in range(n):
        if not keep[i]:
            continue
        if prev != -1 and i > prev + 1:
            parts.append("[…중략…]")
        parts.append(trimmed.get(i, texts[i][1]))
        prev = i
    return "\n\n".join(parts)


def build_summary_prompt(meta, current_tasks, choices=()):
    conversation = render_conversation_for_summary(meta)
    title = meta.get("title") or "(제목 없음)"
    slug_list = "\n".join(f"- {sl} — {ti}" for sl, ti in choices) or "- (등록된 주제 없음)"
    instructions = """너는 내 작업 기록 비서다. 아래는 한 프로젝트 세션에서 '이번에 새로 진행한 대화'다. 읽고 뽑아라.

- topic: 이 대화가 속한 주제 슬러그를 '# 주제 목록'에서 **정확히 하나** 골라라. 해당 없으면 "none".
  · 목록에 없는 새 슬러그를 만들지 마라. 반드시 목록의 값 또는 "none" 이어야 한다.
- progress: 이번에 '실제로 한 일'을 1~3개의 짧은 불릿(- ...)으로. 계획이 아니라 한 일.
- resume: 재개 지점 + 남은 단계 요약. '지금 앉으면 무엇부터'로 시작하고, 남은 단계가 더 있으면
  '① … → ② …' 로 이어 붙여라. 이 주제의 로드맵이며 매번 덮어쓴다.
- tasks_markdown: 현재 미완료 작업 목록을 유지하되 - [ ] 체크박스 목록 전체를 반환.
  · **주제의 다음 단계는 resume 에만 쓴다.** 순서가 있고 서로 의존하는 단계를 태스크로 쪼개지 마라.
  · 태스크로 만들 것은 셋뿐이다 — ① 주제 밖 단발 작업, ② 주제 안이지만 순서 밖이라 잊기 쉬운 일,
    ③ 완료 날짜를 남겨야 하는 일. 셋 중 어느 것도 아니면 만들지 마라.
  · **topic 이 "none" 이면 남은 일을 반드시 태스크로 남겨라(= ①).** 주제가 없으면 resume 은
    세션 노트에만 적히고 목차에는 실리지 않아, 태스크로 남기지 않으면 어디에서도 보이지 않는다.
  · 기존 항목 보존, 관련 작업은 하나로 묶기(과도 분할 금지), 새 작업은 관련끼리 묶어 추가.
  · 완료 판정·삭제·체크(- [x]) 변경 금지(완료는 사용자가 직접). 기존 `[[...]]` 링크 보존, 새 링크 만들지 마라.
- conclusions: **다음에 같은 작업을 할 때 몰랐으면 헤맬 사실**만 배열로. 수치·조건·이유를 담아라.
  · 이번에 무엇을 했는지는 progress 의 몫이다. 여기 쓰지 마라.
  · 해당 없으면 [] 로 두어라. 억지로 만들지 마라.
- dropped: 이번 대화에서 **검토했다가 기각한 대안**을 "안 — 기각 이유" 형태 배열로.
  · 기각 이유가 없으면 넣지 마라. 없으면 [].

모든 답변 한국어. 확실치 않으면 짧게."""
    return (
        f"{instructions}\n\n"
        "반드시 아래 JSON 하나로만 답하라. 코드펜스/설명 금지. 값은 모두 문자열:\n"
        '{"topic": "slug 또는 none", "progress": "- ...", "resume": "...",\n'
        '  "conclusions": [], "dropped": [], "tasks_markdown": "..."}\n\n'
        f"# 주제 목록\n{slug_list}\n\n"
        f"# 프로젝트\n{title}\n\n"
        f"# 현재 미완료 작업\n{current_tasks or '(없음)'}\n\n"
        f"# 이번에 새로 진행한 대화\n{conversation}\n"
    )


def _extract_json(text):
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end <= start:
        return None
    try:
        return json.loads(text[start:end + 1])
    except json.JSONDecodeError:
        return None


def _valid_summary(parsed):
    if not isinstance(parsed, dict):
        return None
    for k in ("topic", "progress", "resume", "tasks_markdown"):
        v = parsed.get(k)
        if v is not None and not isinstance(v, str):
            return None
    for k in ("conclusions", "dropped"):
        v = parsed.get(k)
        if v is not None and not isinstance(v, list):
            return None
    return {
        "topic": parsed.get("topic"),
        "conclusions": [str(x) for x in (parsed.get("conclusions") or []) if str(x).strip()],
        "dropped": [str(x) for x in (parsed.get("dropped") or []) if str(x).strip()],
        "progress": parsed.get("progress"),
        "resume": parsed.get("resume"),
        "tasks_markdown": parsed.get("tasks_markdown"),
    }


def call_claude(prompt):
    env = dict(os.environ)
    env[GUARD_ENV] = "1"
    try:
        proc = subprocess.run(
            ["claude", "-p", "--model", SUMMARY_MODEL],
            input=prompt, text=True, capture_output=True, env=env, timeout=CLAUDE_TIMEOUT_SEC)
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return None
    return proc.stdout if proc.returncode == 0 else None


def summarize(meta, current_tasks, use_llm=True, choices=()):
    title = meta.get("title") or "세션"
    fallback = {
        "topic": None,
        "conclusions": [],
        "dropped": [],
        "progress": "- (요약 실패 — 대화 참조)",
        "resume": "(요약 실패 — 수동 확인 필요)",
        "tasks_markdown": current_tasks or "",
    }
    if not use_llm:
        return {**fallback, "progress": f"- [{title}] (dry-run)", "resume": "(dry-run)"}
    base_prompt = build_summary_prompt(meta, current_tasks, choices)
    nudge = "\n\n[중요] 직전 응답이 형식에 안 맞았다. JSON 객체 하나만 출력하라."
    valid = None
    for attempt in range(SUMMARY_MAX_TRIES):
        out = call_claude(base_prompt if attempt == 0 else base_prompt + nudge)
        valid = _valid_summary(_extract_json(out)) if out else None
        if valid is not None:
            break
        _debug(f"[worker] 요약 무효 — 재시도({attempt + 1})")
    if valid is None:
        return fallback

    # 닫힌 선택지 강제: 목록에 없는 슬러그는 none 으로 (결정 E — 훅은 새 주제를 만들지 않는다)
    topic = (valid["topic"] or "").strip()
    if topic.lower() in ("", "none", "null", "n/a"):
        topic = None
    elif choices and topic not in {sl for sl, _ in choices}:
        _debug(f"[worker] 미등록 슬러그 '{topic}' → none 처리")
        topic = None
    return {
        "topic": topic,
        "conclusions": valid["conclusions"],
        "dropped": valid["dropped"],
        "progress": valid["progress"] or fallback["progress"],
        "resume": valid["resume"] or fallback["resume"],
        "tasks_markdown": valid["tasks_markdown"] or (current_tasks or ""),
    }


# ── 태스크 (작업현황) ───────────────────────────────────────────────
def _split_tasks(md):
    open_t, done_t = [], []
    for ln in (md or "").splitlines():
        s = ln.rstrip()
        low = s.lstrip().lower()
        if low.startswith("- [ ]"):
            open_t.append(s)
        elif low.startswith("- [x]"):
            done_t.append(s)
    return open_t, done_t


def _stamp_done(line, date_str):
    return line if "✅" in line else f"{line} ✅ {date_str}"


def _compose_tasks(open_lines, done_lines):
    parts = [TASKS_OPEN_HEADER, ""]
    parts += open_lines if open_lines else ["_(진행 중 작업 없음)_"]
    parts += ["", TASKS_DONE_HEADER, ""]
    parts += list(reversed(done_lines)) if done_lines else ["_(완료 항목 없음)_"]
    return "\n".join(parts).rstrip() + "\n"


def _task_key(line):
    s = re.sub(r"-\s*\[[ xX]\]", "", line)
    s = re.sub(r"\[\[.*?\]\]", "", s)
    s = re.sub(r"✅\s*\d{4}-\d{2}-\d{2}", "", s)
    return s.strip().lower()


def _done_date(line):
    m = re.search(r"✅\s*(\d{4}-\d{2}-\d{2})", line)
    return m.group(1) if m else None


def _archive_done(base, lines):
    path = os.path.join(base, ARCHIVE_FILENAME)
    existing = ""
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            existing = f.read()
    seen = {_task_key(l) for l in existing.splitlines() if l.lstrip().lower().startswith("- [x]")}
    fresh = [l for l in lines if _task_key(l) not in seen]
    if not fresh:
        return
    with open(path, "a", encoding="utf-8") as f:
        if not existing:
            f.write("# ✅ 완료 아카이브\n\n")
        elif not existing.endswith("\n"):
            f.write("\n")
        for l in fresh:
            f.write(l.rstrip() + "\n")


def _link_tasks(md, hub_name, topic=None):
    """새 태스크에 세션 링크와 주제 태그를 붙인다.
    주제 귀속은 태스크가 **생길 때**만 확실히 아는 정보다 — 나중에 텍스트 유사도로
    추정하면 세션마다 답이 흔들린다. 이미 링크가 있는 줄은 다른 세션에서 온
    기존 태스크이므로 이번 주제를 덧씌우지 않는다."""
    out = []
    for line in md.splitlines():
        s = line.rstrip()
        if s.lstrip().lower().startswith(("- [ ]", "- [x]")):
            if topic and "[[" not in s:
                s = f"{s}  [[{TOPICS_DIRNAME}/{topic}|🔧]]"
            if "↗ 세션]]" not in s:
                s = f"{s}  [[{hub_name}|↗ 세션]]"
        out.append(s)
    return "\n".join(out)


def _tag_topic(lines, topic):
    """이번 콜에서 새로 생긴 줄(링크 없음)에 그 날짜의 주제 태그를 붙인다.
    날짜 그룹마다 topic 이 다를 수 있으므로 루프 안에서 그때그때 붙여야 한다 —
    마지막 날짜의 topic 을 전부에 붙이면 다른 날 생긴 태스크가 엉뚱한 주제로 간다."""
    if not topic:
        return lines
    return [l if "[[" in l else f"{l}  [[{TOPICS_DIRNAME}/{topic}|🔧]]" for l in lines]


def _task_topic(line):
    """태스크 줄에 심긴 주제 슬러그."""
    m = re.search(r"\[\[" + re.escape(TOPICS_DIRNAME) + r"/([^|\]]+)", line)
    return m.group(1).strip() if m else None


def _restore_task_topics(open_lines, base):
    """LLM 이 목록을 재작성하며 주제 태그를 떨어뜨려도 기존 파일에서 되살린다.
    태그가 날아가면 그룹핑이 통째로 무너지므로 링크 보존 지시에만 기대지 않는다."""
    tf = os.path.join(base, TASKS_FILENAME)
    if not os.path.exists(tf):
        return open_lines
    prev = {}
    with open(tf, encoding="utf-8") as f:
        for l in f.read().splitlines():
            t = _task_topic(l)
            if t:
                prev[_task_key(l)] = t
    out = []
    for l in open_lines:
        t = prev.get(_task_key(l)) if not _task_topic(l) else None
        out.append(f"{l}  [[{TOPICS_DIRNAME}/{t}|🔧]]" if t else l)
    return out


def _count_tasks(md):
    return sum(1 for ln in (md or "").splitlines()
               if ln.lstrip().lower().startswith(("- [ ]", "- [x]")))


def _backup_tasks(tasks_path, content):
    d = os.path.join(os.path.dirname(tasks_path), ".task-backups")
    os.makedirs(d, exist_ok=True)
    ts = datetime.now().strftime("%Y-%m-%d_%H%M%S")
    with open(os.path.join(d, f"tasks_{ts}.md"), "w", encoding="utf-8") as f:
        f.write(content)
    for old in sorted(glob.glob(os.path.join(d, "tasks_*.md")))[:-TASK_BACKUP_KEEP]:
        try:
            os.remove(old)
        except OSError:
            pass


def _safe_write_tasks(tasks_path, new_md):
    cur = ""
    if os.path.exists(tasks_path):
        with open(tasks_path, encoding="utf-8") as f:
            cur = f.read()
    cur_n, new_n = _count_tasks(cur), _count_tasks(new_md)
    if cur_n >= 1 and new_n == 0:
        _debug(f"[worker] 작업현황 덮어쓰기 거부(와이프 방지): {cur_n}→0")
        return False
    if new_n < cur_n:
        # 정당한 감소(아카이브 이동·항목 병합)도 있으므로 막지는 않는다.
        # 다만 LLM 이 조용히 항목을 떨어뜨리는 경우를 사후에 알 수 있어야 한다.
        _debug(f"[worker] 작업현황 항목 감소: {cur_n}→{new_n} (직전 사본 .task-backups/)")
    if cur.strip():
        _backup_tasks(tasks_path, cur)
    tmp = tasks_path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(new_md.rstrip() + "\n")
    os.replace(tmp, tasks_path)
    return True


def _update_tasks(base, tasks_markdown, done_cur, hub_name, db_path=DB_FILE, topic=None):
    """LLM 미완료 결과 + 기존 완료(보존·아카이브) → 작업현황 안전 갱신.
    완료일은 task_events의 실제 완료 시각(FileChanged로 포착) 우선."""
    _sync_task_states(base, db_path)  # 폴백: 아직 미기록된 완료 전이 포착
    # done 은 호출자가 넘긴 스냅샷이 아니라 **지금 파일**에서 다시 읽는다.
    # 락을 쥔 채 LLM 을 기다리는 37~91초 사이에 사람이 체크한 항목이 있으면,
    # 옛 스냅샷으로 덮어쓸 때 그 체크가 조용히 되돌려진다.
    tf_now = os.path.join(base, TASKS_FILENAME)
    if os.path.exists(tf_now):
        with open(tf_now, encoding="utf-8") as f:
            _, done_cur = _split_tasks(f.read())
    today = datetime.now().strftime("%Y-%m-%d")
    open_new, _ = _split_tasks(tasks_markdown or "")
    done_keys = {_task_key(d) for d in done_cur}
    open_new = [o for o in open_new if _task_key(o) not in done_keys]
    open_new = _restore_task_topics(open_new, base)
    done_stamped = [_stamp_done(d, _completion_date(_task_key(d), today, db_path)) for d in done_cur]
    cutoff = (datetime.now() - timedelta(days=DONE_RETAIN_DAYS)).strftime("%Y-%m-%d")
    recent, old = [], []
    for d in done_stamped:
        dd = _done_date(d)
        (old if (dd and dd < cutoff) else recent).append(d)
    if old:
        _archive_done(base, old)
        _debug(f"[worker] 완료 아카이브 이동: {len(old)}건")
    composed = _link_tasks(_compose_tasks(open_new, recent), hub_name, topic)
    _safe_write_tasks(os.path.join(base, TASKS_FILENAME), composed)
    _sync_task_states(base, db_path)  # 쓰기 후 snapshot 갱신


# ── 동시성 락 ───────────────────────────────────────────────────────
@contextlib.contextmanager
def _vault_lock():
    f = open(LOCK_FILE, "w")
    try:
        fcntl.flock(f.fileno(), fcntl.LOCK_EX)
        yield
    finally:
        try:
            fcntl.flock(f.fileno(), fcntl.LOCK_UN)
        finally:
            f.close()


# ── 증분 렌더링 (허브 / 대화 페이지 / daily / weekly) ────────────────
def _group_by_date(turns, fallback_date):
    groups, order, last = {}, [], fallback_date
    for t in turns:
        ds = _turn_date(t[2], last)
        last = ds
        if ds not in groups:
            groups[ds] = []
            order.append(ds)
        groups[ds].append(t)
    return [(d, groups[d]) for d in order]  # 시간순


def _render_turns(turns):
    """대화 페이지 본문. 도구 라인·'#로그' 제외 + turn당 PASTE_CAP 상한."""
    out = []
    for role, lines, _ in turns:
        ls = _clean_lines(lines)
        if not ls:
            continue  # 도구/로그만 있던 turn 은 저장하지 않음
        # 이모지만으로 역할이 구분되므로 'User'/'Assistant' 표기는 생략한다.
        # (요약 프롬프트 쪽은 [User]/[Assistant] 를 유지 — 요약기에는 역할 라벨이 필요하다)
        out.append("### 👤" if role == "user" else "### 🤖")
        out.append(_capped("\n".join(ls)))
        out.append("")
    return "\n".join(out).rstrip() + "\n"


def _write_conversation_page(base, sid8, date, hub_name, turns, topic=None):
    d = os.path.join(base, CONV_DIRNAME)
    os.makedirs(d, exist_ok=True)
    path = os.path.join(d, f"{sid8}_{date}.md")
    body = _render_turns(turns)
    if os.path.exists(path):
        with open(path, "a", encoding="utf-8") as f:
            f.write("\n" + body)
    else:
        # title frontmatter: Front Matter Title 플러그인이 파일명 대신 표시
        title = f"{topic or '대화'} · {date}"
        with open(path, "w", encoding="utf-8") as f:
            f.write(f"---\ntitle: {_yaml_val(title)}\ntopic: {_yaml_val(title)}\n---\n\n"
                    f"> ↑ 프로젝트: [[{hub_name}]]\n\n# 💬 {date} 대화\n\n" + body)


def _append_daily(base, date, label, progress, link_target):
    """label 은 topic 슬러그 우선. 매칭 실패 시에만 cwd 기반 project 로 폴백.
    한 주제가 여러 repo 에 걸치면 cwd 기준 그룹핑은 실제와 어긋난다."""
    d = os.path.join(base, "daily")
    os.makedirs(d, exist_ok=True)
    first = progress.strip().splitlines()[0] if progress.strip() else "진행"
    first = re.sub(r"^-\s*", "", first).strip()
    line = f"- [{label}] {first}  [[{link_target}|↗]]"
    with open(os.path.join(d, f"{date}.md"), "a", encoding="utf-8") as f:
        f.write(line + "\n")


def _hub_frontmatter(meta, next_step, started, updated):
    resume_cmd = f"claude --resume {meta.get('session_id') or '?'}"
    head = ["---",
            f"topic: {_yaml_val(meta['title'] or '(제목 없음)')}",
            f"resume: {resume_cmd}",
            f"next: {_yaml_val(next_step)}",
            f"started: {started}",
            f"updated: {updated}",
            f"cwd: {meta['cwd'] or '?'}"]
    branch = meta.get("git_branch")
    if branch and branch != "HEAD":
        head.append(f"git_branch: {branch}")
    head += [f"tokens: in≈{meta['in_tok']}, out≈{meta['out_tok']}", "---"]
    return "\n".join(head)


def _drop_progress_block(progress, header):
    """진행 로그에서 같은 헤더의 블록을 제거한다 (다음 '### ' 직전 또는 끝까지).
    같은 날 두 번 flush 되면 같은 (날짜, sid8) 블록이 쌓이므로 옛 것을 걷어낸다."""
    i = progress.find(header)
    if i == -1:
        return progress
    j = progress.find("\n### ", i + 1)
    return (progress[:i] + (progress[j + 1:] if j != -1 else "")).rstrip()


def _update_hub(base, meta, hub_name, sid8, new_blocks, next_step, started):
    """허브 노트: frontmatter·🔜 다음 갱신 + 📈 진행 로그에 새 블록 prepend(최신 위).
    기존 노트가 구형(진행 로그 없음)이면 기존 본문은 아래에 보존."""
    sessions_dir = os.path.join(base, "sessions")
    os.makedirs(sessions_dir, exist_ok=True)
    path = os.path.join(sessions_dir, hub_name + ".md")

    existing_progress, old_body = "", ""
    if os.path.exists(path):
        txt = open(path, encoding="utf-8").read()
        idx = txt.find(PROGRESS_HEADER)
        if idx != -1:
            existing_progress = txt[idx + len(PROGRESS_HEADER):].lstrip("\n")
        else:
            parts = txt.split("\n---\n", 1)  # frontmatter 이후 본문 보존
            old_body = (parts[1] if len(parts) == 2 else txt).strip()

    # 같은 (날짜, sid8) 블록이 이미 있으면 옛 것을 제거하고 새 것으로 갈음한다.
    for b in new_blocks:
        existing_progress = _drop_progress_block(existing_progress, b.split("\n", 1)[0])

    today = datetime.now().strftime("%Y-%m-%d")
    out = [_hub_frontmatter(meta, next_step, started, today), "",
           NEXT_HEADER, "", f"- {next_step}", "", PROGRESS_HEADER, ""]
    if new_blocks:
        out.append("\n\n".join(new_blocks))
    if existing_progress.strip():
        out.append("\n" + existing_progress.rstrip())
    if old_body:
        out.append("\n---\n\n" + old_body)
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(out).rstrip() + "\n")


# ── 주제축 (topics/ · INDEX.md) ─────────────────────────────────────
def _topic_slugs(base):
    d = os.path.join(base, TOPICS_DIRNAME)
    if not os.path.isdir(d):
        return []
    return sorted(os.path.splitext(os.path.basename(p))[0]
                  for p in glob.glob(os.path.join(d, "*.md")))


def _section_items(txt, header):
    """'## 헤더' 아래의 '- ' 항목들. 다음 '## ' 에서 멈춘다."""
    i = txt.find(header)
    if i == -1:
        return []
    out = []
    for ln in txt[i + len(header):].splitlines():
        s = ln.strip()
        if s.startswith("## "):
            break
        if s.startswith("- ") and s != "- _(없음)_":
            out.append(s[2:].strip())
    return out


def _dedup_against(items, existing, threshold=0.75):
    """기존 항목과 유사한 것을 걸러낸다. 요약기는 기존 목록을 못 보므로
    (topic 이 같은 콜에서 정해져 프롬프트에 미리 넣을 수 없다) 파이썬에서 막는다."""
    def norm(s):
        return re.sub(r"[\s`*_·,.\-—()\[\]]+", "", s).lower()

    out, seen = [], [norm(e) for e in existing]
    for it in items:
        n = norm(it)
        if not n:
            continue
        if any(n in e or e in n or difflib.SequenceMatcher(None, n, e).ratio() >= threshold
               for e in seen):
            continue
        out.append(it.strip())
        seen.append(n)   # 같은 응답 안에서의 중복도 막는다
    return out


def _append_section(txt, header, items):
    """섹션 끝에 항목을 append. 섹션이 없으면 만들지 않는다(스키마 보존).
    기존 줄은 절대 고치지 않는다 — 사람이 다듬은 문장을 훅이 덮지 않기 위함."""
    if not items:
        return txt
    i = txt.find(header)
    if i == -1:
        return txt
    j = txt.find("\n## ", i + 1)
    body = (txt[i:j] if j != -1 else txt[i:]).replace("- _(없음)_", "").rstrip()
    body += "\n" + "\n".join(f"- {x}" for x in items) + "\n"
    return txt[:i] + body + (f"\n{txt[j + 1:]}" if j != -1 else "")


def _topic_meta(path):
    """frontmatter(status/title/plan) + '🔜 다음' 첫 줄."""
    meta = {"status": "active", "title": "", "next": "", "plan": ""}
    try:
        txt = open(path, encoding="utf-8").read()
    except OSError:
        return meta
    m = re.match(r"^---\n(.*?)\n---\n", txt, re.S)
    if m:
        for ln in m.group(1).splitlines():
            k, _, v = ln.partition(":")
            k, v = k.strip(), v.strip().strip('"')
            if k in ("status", "title", "plan"):
                meta[k] = v
    i = txt.find(NEXT_HEADER)
    if i != -1:
        for ln in txt[i + len(NEXT_HEADER):].splitlines():
            s = ln.strip().lstrip("-").strip()
            if s.startswith("#"):
                break
            if s:
                meta["next"] = s
                break
    return meta


def _topic_choices(base):
    """프롬프트에 줄 (slug, title) 목록. 완료(status: done) 주제는 제외.

    슬러그만 주면 매칭이 안 된다 — 'session-memory-architecture' 라는 문자열만으로
    그게 무슨 작업인지 요약기가 알 수 없기 때문. title 을 함께 준다.
    """
    d = os.path.join(base, TOPICS_DIRNAME)
    out = []
    for slug in _topic_slugs(base):
        m = _topic_meta(os.path.join(d, f"{slug}.md"))
        if (m.get("status") or "active") == "done":
            continue
        out.append((slug, m.get("title") or slug))
    return out


def _fm_set(txt, key, value):
    m = re.match(r"^---\n(.*?)\n---\n", txt, re.S)
    if not m:
        return txt
    fm, line = m.group(1), f"{key}: {value}"
    if re.search(rf"^{re.escape(key)}:.*$", fm, re.M):
        fm = re.sub(rf"^{re.escape(key)}:.*$", line, fm, count=1, flags=re.M)
    else:
        fm = fm.rstrip() + "\n" + line
    return f"---\n{fm}\n---\n" + txt[m.end():]


def _append_topic(base, slug, date, sid8, progress, next_step,
                  conclusions=(), dropped=()):
    """주제 파일의 📈 진행 로그에 prepend + frontmatter 갱신.
    파일이 없으면 만들지 않는다 — 신규 주제 생성은 사용자 지시로만(결정 E)."""
    path = os.path.join(base, TOPICS_DIRNAME, f"{slug}.md")
    if not os.path.exists(path):
        _debug(f"[worker] topics/{slug}.md 없음 — daily 에만 기록")
        return False
    txt = open(path, encoding="utf-8").read()

    # ① 결론·접은 안 — append 만 한다. 기존 줄은 고치지 않는다.
    #    요약기는 기존 목록을 볼 수 없으므로(topic 이 같은 콜에서 정해진다) 여기서 중복을 막는다.
    #    진행 로그 중복 여부와 **무관하게** 먼저 처리한다 — 같은 날 두 번째 flush 에서
    #    새로 나온 결론이 유실되면 안 되기 때문이다.
    for header, items in ((CONCLUSION_HEADER, conclusions), (DROPPED_HEADER, dropped)):
        fresh = _dedup_against(list(items or ()), _section_items(txt, header))
        if fresh:
            txt = _append_section(txt, header, fresh)
            _debug(f"[worker] topics/{slug}: {header} +{len(fresh)}건")

    # ② 진행 로그 — 같은 (날짜, 세션) 블록이 이미 있으면 이 단계만 건너뛴다.
    marker = f"### {date}  [[{CONV_DIRNAME}/{sid8}_{date}"
    if marker in txt:
        _debug(f"[worker] topics/{slug}: {date}/{sid8} 진행 로그 블록 이미 존재 — 건너뜀")
    else:
        block = f"{marker}|💬 대화]]\n{progress.rstrip()}"
        i = txt.find(PROGRESS_HEADER)
        if i == -1:
            txt = txt.rstrip() + f"\n\n{PROGRESS_HEADER}\n\n{block}\n"
        else:
            head = txt[:i + len(PROGRESS_HEADER)]
            tail = txt[i + len(PROGRESS_HEADER):].lstrip("\n")
            txt = f"{head}\n\n{block}\n\n{tail}"

    txt = _fm_set(txt, "updated", date)
    if next_step:
        # '다음'의 정본은 '## 🔜 다음' 섹션 하나다 (_topic_meta 가 여기서 읽는다).
        # frontmatter 에 next 를 중복 기록하지 않는다.
        j = txt.find(NEXT_HEADER)
        if j != -1:
            # '🔜 다음' 이 마지막 섹션이면 find 가 -1 을 준다.
            # txt[-1:] 로 흘러가면 마지막 문자가 뒤에 붙으므로 명시적으로 분기한다.
            k = txt.find("\n##", j + 1)
            tail = txt[k:].lstrip("\n") if k != -1 else ""
            body = f"{NEXT_HEADER}\n\n- {next_step.strip()}\n"
            txt = txt[:j] + body + (f"\n{tail}" if tail else "")
    # sessions: [...] 에 sid8 추가
    ms = re.search(r"^sessions:\s*\[(.*?)\]\s*$", txt, re.M)
    if ms:
        cur = [s.strip() for s in ms.group(1).split(",") if s.strip()]
        if sid8 not in cur:
            cur.append(sid8)
            txt = txt[:ms.start()] + f"sessions: [{', '.join(cur)}]" + txt[ms.end():]
    with open(path, "w", encoding="utf-8") as f:
        f.write(txt.rstrip() + "\n")
    return True


def _task_title(line):
    """INDEX 표시용 짧은 제목. 상세는 작업현황.md 가 정본이므로 여기선 잘라도 된다
    (한 줄이 300자를 넘는 태스크가 있어, 그대로 실으면 목차가 목차 구실을 못 한다)."""
    s = re.sub(r"^\s*-\s*\[[ xX]\]\s*", "", line)
    s = re.sub(r"\[\[.*?\]\]", "", s).strip()
    head = s.split(": ", 1)[0].strip()
    if len(head) >= 5:            # 'A: B' 형태면 A 가 제목이다. 너무 짧으면 오탐
        s = head
    return s if len(s) <= TASK_TITLE_MAX else s[:TASK_TITLE_MAX].rstrip() + "…"


def _open_tasks_by_topic(base):
    """미완료 태스크를 주제별로 나눈다 → ({slug: [제목]}, [무소속 제목])."""
    by_topic, orphan = {}, []
    tf = os.path.join(base, TASKS_FILENAME)
    if not os.path.exists(tf):
        return by_topic, orphan
    with open(tf, encoding="utf-8") as f:
        for l in f.read().splitlines():
            if not l.lstrip().lower().startswith("- [ ]"):
                continue
            t, title = _task_topic(l), _task_title(l)
            if t:
                by_topic.setdefault(t, []).append(title)
            else:
                orphan.append(title)
    return by_topic, orphan


def _write_index(base):
    """topics/ 에서 INDEX.md 재생성 (0토큰). weekly 가 daily 에서 생성되는 것과 같은 방식.
    작업현황 미완료를 주제 아래로 접어 넣어 '어느 프로젝트의 일인가'를 한눈에 보이게 한다."""
    d = os.path.join(base, TOPICS_DIRNAME)
    os.makedirs(d, exist_ok=True)
    by_topic, orphan = _open_tasks_by_topic(base)
    entries = []
    for slug in _topic_slugs(base):
        m = _topic_meta(os.path.join(d, f"{slug}.md"))
        st = m.get("status") or "active"
        if st != "done":
            entries.append((slug, m, st))
    # 보류는 맨 아래에 실리므로 번호도 그 순서를 따른다 (sort 는 stable)
    entries.sort(key=lambda e: e[2] == "paused")

    # 번호는 이 렌더 한정의 순번이다 — 영구 ID 가 아니다.
    # 지시는 INDEX 를 열어보고 하므로 사용자가 보는 번호와 여기 번호가 같은 파일에서 나온다.
    # 그래서 다음 렌더에 밀려도 무해하다. 대신 **기록물(plan·topics)에는 번호를 쓰지 않는다** —
    # 거기 적힌 번호는 다음 렌더에 거짓이 된다.
    active, paused = [], []
    for n, (slug, m, st) in enumerate(entries, 1):
        line = (f"- **{n}.** [[{TOPICS_DIRNAME}/{slug}|{m.get('title') or slug}]] "
                f"`{st}` — {m.get('next') or '_(다음 미기재)_'}")
        # 설계 문서 포인터는 **지시문**으로 둔다. "그쪽에 있다"로 끝나는 서술문은
        # 읽을지 여부를 읽는 쪽 판단에 맡기므로 전달이 보장되지 않는다.
        if m.get("plan"):
            line += f"\n  📄 먼저 `{m['plan']}` 를 읽어라. 전역 결정·기각 이력은 그쪽에 있다."
        for j, title in enumerate(by_topic.pop(slug, ()), 1):
            line += f"\n\t- [ ] **{n}-{j}** {title}"
        (paused if st == "paused" else active).append(line)
    # 끝난(done) 주제나 사라진 슬러그에 달린 태스크는 흘려보내지 않고 기타로 모은다
    for left in by_topic.values():
        orphan += left

    out = ["# 🧭 INDEX", "",
           "> 주제 진입점. `topics/` 에서 자동 생성됩니다 — 직접 편집하지 마세요.", "",
           "## 🔧 진행 중", ""]
    out += active or ["_(없음)_"]
    if orphan:
        out += ["", "## ☑️ 기타 태스크", ""] + [f"- [ ] {t}" for t in orphan]
    if paused:  # 지금 손대지 않는 것이므로 맨 아래
        out += ["", "## ⏸ 보류", ""] + paused
    with open(os.path.join(base, INDEX_FILENAME), "w", encoding="utf-8") as f:
        f.write("\n".join(out).rstrip() + "\n")


def _write_weekly_digest(base, ref=None):
    """daily 노트 기반 집계 → weekly/<ISO주차>.md (활동일 기준, 0토큰).
    ref가 속한 주를 재생성 (기본: 이번 주)."""
    now = ref or datetime.now()
    iso = now.isocalendar()
    monday = now - timedelta(days=now.weekday())
    days = [(monday + timedelta(days=i)).strftime("%Y-%m-%d") for i in range(7)]
    wk_start, wk_end = days[0], days[6]
    weekly_dir = os.path.join(base, "weekly")
    os.makedirs(weekly_dir, exist_ok=True)

    by_proj = {}
    n_entries = 0
    for ds in days:
        fp = os.path.join(base, "daily", f"{ds}.md")
        if not os.path.exists(fp):
            continue
        for ln in open(fp, encoding="utf-8").read().splitlines():
            s = ln.strip()
            if not s.startswith("-"):
                continue
            n_entries += 1
            m = re.match(r"-\s*\[(.*?)\]\s*(.*)", s)
            proj = m.group(1) if m else "기타"
            text = (m.group(2) if m else s).strip()
            by_proj.setdefault(proj, []).append((ds[5:], text))

    done = []
    for fn in (TASKS_FILENAME, ARCHIVE_FILENAME):
        fp = os.path.join(base, fn)
        if os.path.exists(fp):
            for ln in open(fp, encoding="utf-8").read().splitlines():
                dd = _done_date(ln)
                if dd and wk_start <= dd <= wk_end:
                    done.append(ln.strip())

    out = [f"# 📅 주간 다이제스트 {iso[0]}-W{iso[1]:02d}", f"> {wk_start} ~ {wk_end}", "",
           f"**활동 {n_entries}건 · 프로젝트 {len(by_proj)}개 · 완료 {len(done)}건**", ""]
    for proj, items in sorted(by_proj.items(), key=lambda kv: -len(kv[1])):
        out.append(f"## {proj} ({len(items)})")
        out += [f"- {d} {t}" for d, t in items]
        out.append("")
    out.append("## ✅ 이번 주 완료")
    out += done if done else ["_(없음)_"]
    with open(os.path.join(weekly_dir, f"{iso[0]}-W{iso[1]:02d}.md"), "w", encoding="utf-8") as f:
        f.write("\n".join(out).rstrip() + "\n")


# ── 증분 처리 (핵심) ────────────────────────────────────────────────
def _process(transcript, base=None, db_path=DB_FILE, use_llm=True):
    base = base or VAULT
    try:
        _rotate_debug_logs()
        if not transcript or not os.path.exists(transcript):
            _debug("[worker] ABORT: transcript 없음")
            return
        meta = parse_transcript(transcript)
        sid = meta["session_id"]
        sid8 = sid[:8]
        all_turns = meta["turns"]

        with _vault_lock():
            processed = db_get_processed(sid, db_path)
            if len(all_turns) < processed:  # 트랜스크립트 축소 → 리셋
                processed = 0
            new_turns = all_turns[processed:]
            if not new_turns:
                _debug(f"[worker] SKIP: 새 turn 없음 (processed={processed})")
                return
            new_meta = {**meta, "turns": new_turns}
            if _is_excluded(new_meta):  # 제외 마커: '이번 증분'만 제외, 마커는 전진(다음 flush부터 재개)
                db_set_processed(sid, len(all_turns), db_path)
                _debug("[worker] SKIP: 제외 마커(이번 증분만 제외)")
                return
            if not is_significant(new_meta):
                db_set_processed(sid, len(all_turns), db_path)
                _debug("[worker] SKIP: 새 turn 사소 (마커만 전진)")
                return
            task_skip = _is_task_skipped(new_meta)
            if task_skip:
                _debug("[worker] '#완료' 마커 — 기록은 남기고 태스크만 만들지 않음")

            start = _fmt_ts(meta["first_ts"])
            started = start.strftime("%Y-%m-%d") if start else datetime.now().strftime("%Y-%m-%d")
            hhmm = start.strftime("%H%M") if start else "0000"
            hub_name = f"{started}_{hhmm}_{_slug(meta['title'])}_{sid8}"
            project = _project_of(meta)

            current_tasks = ""
            tf = os.path.join(base, TASKS_FILENAME)
            if os.path.exists(tf):
                current_tasks = open(tf, encoding="utf-8").read()
            open_cur, done_cur = _split_tasks(current_tasks)

            choices = _topic_choices(base)   # (slug, title) 닫힌 선택지
            groups = _group_by_date(new_turns, started)
            new_blocks = []       # (progress log 블록)
            last_summary = None
            for date, dturns in groups:
                dmeta = {**meta, "turns": dturns}
                summary = summarize(dmeta, "\n".join(open_cur), use_llm=use_llm, choices=choices)
                last_summary = summary
                _write_conversation_page(base, sid8, date, hub_name, dturns, meta.get("title"))
                prog = summary["progress"].rstrip()
                topic = summary.get("topic")
                on_topic = bool(topic) and _append_topic(
                    base, topic, date, sid8, prog, summary["resume"],
                    summary.get("conclusions"), summary.get("dropped"))
                # 주제에 붙였으면 hub 에는 포인터만 — 진행 로그 정본을 한 곳으로 유지한다.
                # 양쪽에 같은 내용을 쓰면 한쪽을 사람이 고쳤을 때 조용히 분기한다.
                head = f"### {date}  [[{CONV_DIRNAME}/{sid8}_{date}|💬 대화]]"
                new_blocks.append(
                    f"{head}\n→ 진행 로그: [[{TOPICS_DIRNAME}/{topic}]]" if on_topic
                    else f"{head}\n{prog}")
                _append_daily(base, date,
                              topic if on_topic else project,
                              summary["progress"],
                              f"{TOPICS_DIRNAME}/{topic}" if on_topic else hub_name)
                # 날짜별 결과를 이어받는다. 마지막 요약만 쓰면 중간 날짜에서 나온 태스크가
                # 통째로 사라진다 — 다중 활동일 flush 는 실측 42%(19건 중 8건)다.
                # 태그도 여기서 붙인다(날짜마다 topic 이 다를 수 있다).
                # '#완료' 면 open_cur 를 그대로 둔다 — 기존 목록이 다시 쓰이므로 신규가 안 생긴다.
                # (_update_tasks 는 계속 호출된다. 완료 전이·아카이브·완료일 스탬프는 그쪽 몫이다.)
                if not task_skip:
                    open_cur = _tag_topic(_split_tasks(summary["tasks_markdown"])[0],
                                          topic if on_topic else None)
                _debug(f"[worker] {date}: topic={topic or 'none'} 진행 로그·대화·데일리 기록")

            new_blocks.reverse()  # 최신이 위로
            _update_hub(base, meta, hub_name, sid8, new_blocks, last_summary["resume"], started)
            _update_tasks(base, "\n".join(open_cur), done_cur, hub_name, db_path,
                          last_summary.get("topic"))
            _write_index(base)    # topics/ + 작업현황 → INDEX.md 재생성 (0토큰)
            # 이번 flush가 건드린 모든 주차 + 이번 주를 재생성 (주 경계 넘김 대응)
            weeks = {}
            for date, _ in groups:
                try:
                    d = datetime.strptime(date, "%Y-%m-%d")
                except ValueError:
                    continue
                weeks[(d.isocalendar()[0], d.isocalendar()[1])] = d
            now = datetime.now()
            weeks[(now.isocalendar()[0], now.isocalendar()[1])] = now
            for d in weeks.values():
                _write_weekly_digest(base, d)
            db_set_processed(sid, len(all_turns), db_path)
            _debug(f"[worker] DONE: +{len(new_turns)}turn, {len(groups)}일, hub={hub_name}")
    except Exception as e:
        import traceback
        _debug("[worker] ERROR: " + repr(e) + "\n" + traceback.format_exc())


# ── 엔트리포인트 ────────────────────────────────────────────────────
def _run_dry(use_llm):
    flag = "--dry-run-llm" if use_llm else "--dry-run"
    transcript = sys.argv[sys.argv.index(flag) + 1]
    out = "/tmp/sessionlog-dryrun"
    if "--out" in sys.argv:
        out = sys.argv[sys.argv.index("--out") + 1]
    os.makedirs(out, exist_ok=True)
    _process(transcript, base=out, db_path=os.path.join(out, "test.db"), use_llm=use_llm)
    print(f"[{flag}] 처리 완료 → {out}")
    for p in sorted(glob.glob(os.path.join(out, "**", "*.md"), recursive=True)):
        print("  ", os.path.relpath(p, out))


def _dispatch_worker(transcript):
    try:
        subprocess.Popen(
            [sys.executable, os.path.abspath(__file__), "--worker", transcript],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            start_new_session=True, env=os.environ.copy())
        _debug(f"dispatched worker → {transcript}")
    except Exception as e:
        _debug("dispatch ERROR: " + repr(e))


def main():
    if "--dry-run-llm" in sys.argv:
        return _run_dry(use_llm=True)
    if "--dry-run" in sys.argv:
        return _run_dry(use_llm=False)
    if "--worker" in sys.argv:
        return _process(sys.argv[sys.argv.index("--worker") + 1])
    if "--filechanged" in sys.argv:  # 작업현황 외부 편집 감지 → 완료 전이 기록
        try:
            sys.stdin.read()
        except Exception:
            pass
        return _sync_task_states(VAULT)

    if os.environ.get(GUARD_ENV):
        return
    _debug("=== SessionEnd hook fired ===")
    try:
        payload = json.load(sys.stdin)
    except json.JSONDecodeError:
        _debug("ABORT: stdin JSON 파싱 실패")
        return
    transcript = payload.get("transcript_path")
    _debug(f"transcript={transcript} exists={bool(transcript) and os.path.exists(transcript)}")
    if not transcript or not os.path.exists(transcript):
        _debug("ABORT: transcript_path 없음")
        return
    _dispatch_worker(transcript)


if __name__ == "__main__":
    main()

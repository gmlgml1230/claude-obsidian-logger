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
INDEX_FILENAME = os.environ.get("SESSIONLOG_INDEX_FILE", "INDEX.md")
DONE_RETAIN_DAYS = 14
ARCHIVE_FILENAME = "완료 아카이브.md"
TASKS_DONE_HEADER = f"## ✅ 완료 ({DONE_RETAIN_DAYS // 7}주 보관)"
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
TASK_TITLE_MAX = 80
CONCLUSION_HEADER = "## 📌 결론"
DROPPED_HEADER = "## ❌ 접은 안"


# ── 디버그 로그 ─────────────────────────────────────────────────────
def _atomic_write(path, text):
    """tmp 에 쓰고 rename. 도중에 죽어도 원본이 잘린 채 남지 않는다.
    topics/ 는 자체 백업이 없어(태스크 백업은 INDEX 전용) 손상되면 git 뿐이다."""
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text)
    os.replace(tmp, path)


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


def _db_usage(db_path):
    c = _db(db_path)
    c.execute("CREATE TABLE IF NOT EXISTS llm_usage ("
              "ts TEXT, session_id TEXT, date TEXT, topic TEXT, model TEXT,"
              "prompt_chars INT, conv_chars INT, tasks_chars INT, topics_chars INT, instr_chars INT,"
              "input_tokens INT, output_tokens INT, cache_read INT, cache_write INT,"
              "cost_usd REAL, elapsed_ms INT)")
    return c


def _log_usage(db_path, sid, date, topic, parts, usage):
    """콜 1회의 비용과 프롬프트 구성을 남긴다. 실패해도 본 작업을 막지 않는다."""
    if not usage:
        return
    try:
        with _db_usage(db_path) as c:
            c.execute("INSERT INTO llm_usage VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (
                datetime.now().isoformat(timespec="seconds"), sid, date, topic or "none", SUMMARY_MODEL,
                parts.get("prompt_chars"), parts.get("conv_chars"), parts.get("tasks_chars"),
                parts.get("topics_chars"), parts.get("instr_chars"),
                usage.get("input_tokens"), usage.get("output_tokens"),
                usage.get("cache_read"), usage.get("cache_write"),
                usage.get("cost_usd"), usage.get("elapsed_ms")))
    except Exception as e:
        _debug("usage 기록 실패: " + repr(e))


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
    tf = os.path.join(base, INDEX_FILENAME)
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
- resume: '지금 앉으면 무엇부터' **한 줄**. 남은 단계를 전부 늘어놓지 마라 — 그건 태스크가 담는다.
- tasks_add: **이번 대화에서 새로 생긴 할 일만** 배열로. 기존 목록은 절대 반환하지 마라(건드릴 수 없다).
  · 원소는 {"text": "할 일", "after": "이 항목 바로 뒤에 넣어라"} 형태. 순서가 상관없으면 after 를 빼라.
  · after 에는 '# 현재 미완료 작업' 목록에 있는 문구를 그대로 적어라. 못 찾으면 맨 뒤에 붙는다.
  · **한 줄은 80자 안쪽.** 명령·경로·근거 같은 상세는 쓰지 마라 — 그건 주제의 진행 로그와 설계 문서가
    담는다. 태스크는 '무엇을 해야 하는가' 한 줄이다.
  · 주제의 다음 단계도 포함한다 — 순서대로 체크해 나가면 되고, 체크박스여야 완료 이력이 남는다.
    순서가 있으면 제목 앞에 ① ② ③ 를 붙여라.
  · 이미 목록에 있는 것과 같은 일이면 넣지 마라. 새로 생긴 게 없으면 [] 로 두어라.
- conclusions: **다음에 같은 작업을 할 때 몰랐으면 헤맬 사실**만 배열로. 수치·조건·이유를 담아라.
  · 이번에 무엇을 했는지는 progress 의 몫이다. 여기 쓰지 마라.
  · 해당 없으면 [] 로 두어라. 억지로 만들지 마라.
- dropped: 이번 대화에서 **검토했다가 기각한 대안**을 "안 — 기각 이유" 형태 배열로.
  · 기각 이유가 없으면 넣지 마라. 없으면 [].

모든 답변 한국어. 확실치 않으면 짧게."""
    prompt = (
        f"{instructions}\n\n"
        "반드시 아래 JSON 하나로만 답하라. 코드펜스/설명 금지. 값은 모두 문자열:\n"
        '{"topic": "slug 또는 none", "progress": "- ...", "resume": "...",\n'
        '  "conclusions": [], "dropped": [], "tasks_add": []}\n\n'
        f"# 주제 목록\n{slug_list}\n\n"
        f"# 프로젝트\n{title}\n\n"
        f"# 현재 미완료 작업 (참고용 — 중복 방지·after 지정에만 쓴다)\n{current_tasks or '(없음)'}\n\n"
        f"# 이번에 새로 진행한 대화\n{conversation}\n"
    )
    # 구성요소별 크기 — "무관한 태스크 목록이 비용을 얼마나 밀어올리는가" 를 나중에 따지기 위함
    parts = {"prompt_chars": len(prompt), "conv_chars": len(conversation),
             "tasks_chars": len(current_tasks or ""), "topics_chars": len(slug_list),
             "instr_chars": len(instructions)}
    return prompt, parts


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
    for k in ("topic", "progress", "resume"):
        v = parsed.get(k)
        if v is not None and not isinstance(v, str):
            return None
    for k in ("conclusions", "dropped", "tasks_add"):
        v = parsed.get(k)
        if v is not None and not isinstance(v, list):
            return None
    return {
        "topic": parsed.get("topic"),
        "conclusions": [str(x) for x in (parsed.get("conclusions") or []) if str(x).strip()],
        "dropped": [str(x) for x in (parsed.get("dropped") or []) if str(x).strip()],
        "progress": parsed.get("progress"),
        "resume": parsed.get("resume"),
        "tasks_add": _norm_adds(parsed.get("tasks_add")),
    }


def _norm_adds(raw):
    """tasks_add 원소를 {'text','after'} 로 정규화. 문자열만 온 경우도 받아준다."""
    out = []
    for it in (raw or []):
        if isinstance(it, str):
            t, af = it, None
        elif isinstance(it, dict):
            t, af = it.get("text") or it.get("task") or "", it.get("after")
        else:
            continue
        t = re.sub(r"^\s*-\s*\[[ xX]\]\s*", "", str(t)).strip()
        if t:
            out.append({"text": t, "after": (str(af).strip() if af else None)})
    return out


def call_claude(prompt):
    """(응답 텍스트, usage) 를 준다. 실패하면 (None, None).

    `--output-format json` 을 쓰면 total_cost_usd 와 토큰 내역이 함께 온다 —
    프롬프트의 어느 부분이 비용을 만드는지 나중에 따지려면 이 값이 있어야 한다."""
    env = dict(os.environ)
    env[GUARD_ENV] = "1"
    t0 = datetime.now()
    try:
        proc = subprocess.run(
            ["claude", "-p", "--model", SUMMARY_MODEL, "--output-format", "json"],
            input=prompt, text=True, capture_output=True, env=env, timeout=CLAUDE_TIMEOUT_SEC)
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return None, None
    if proc.returncode != 0:
        return None, None
    elapsed = int((datetime.now() - t0).total_seconds() * 1000)
    try:
        o = json.loads(proc.stdout)
    except json.JSONDecodeError:
        return proc.stdout, None          # 구버전 CLI 호환: 평문 응답
    u = o.get("usage") or {}
    return o.get("result") or "", {
        "cost_usd": o.get("total_cost_usd"),
        "input_tokens": u.get("input_tokens"),
        "output_tokens": u.get("output_tokens"),
        "cache_read": u.get("cache_read_input_tokens"),
        "cache_write": u.get("cache_creation_input_tokens"),
        "elapsed_ms": elapsed,
    }


def summarize(meta, current_tasks, use_llm=True, choices=()):
    """요약 결과. **LLM 호출 자체가 실패하면 None 을 준다** — 호출자가 마커를 전진시키지
    않고 다음 실행에 재시도하게 하기 위함이다. 오프라인에서 돌면 '(요약 실패)' 를 기록하고
    마커까지 전진해 그 구간이 영영 요약되지 않는 사고가 난다."""
    title = meta.get("title") or "세션"
    if not use_llm:
        return {"topic": None, "conclusions": [], "dropped": [],
                "progress": f"- [{title}] (dry-run)", "resume": "(dry-run)",
                "tasks_add": [], "_usage": None, "_parts": {}}
    base_prompt, parts = build_summary_prompt(meta, current_tasks, choices)
    nudge = "\n\n[중요] 직전 응답이 형식에 안 맞았다. JSON 객체 하나만 출력하라."
    valid, usage = None, None
    for attempt in range(SUMMARY_MAX_TRIES):
        out, usage = call_claude(base_prompt if attempt == 0 else base_prompt + nudge)
        valid = _valid_summary(_extract_json(out)) if out else None
        if valid is not None:
            break
        _debug(f"[worker] 요약 무효 — 재시도({attempt + 1})")
    if valid is None:
        _debug("[worker] 요약 실패(LLM 응답 없음/형식 불일치) — 기록하지 않는다")
        return None

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
        "progress": valid["progress"] or "- (내용 없음)",
        "resume": valid["resume"] or "(다음 미기재)",
        "tasks_add": valid["tasks_add"],
        "_usage": usage,
        "_parts": parts,
    }


# ── 태스크 (작업현황) ───────────────────────────────────────────────
def _split_tasks(md):
    """미완료/완료 태스크. 주제 줄(**3.**)은 체크박스를 갖지만 태스크가 아니므로 뺀다."""
    open_t, done_t = [], []
    for ln in (md or "").splitlines():
        s = ln.rstrip()
        if TOPIC_LINE_RE.match(s):
            continue
        low = s.lstrip().lower()
        if low.startswith("- [ ]"):
            open_t.append(s)
        elif low.startswith("- [x]"):
            done_t.append(s)
    return open_t, done_t


def _stamp_done(line, date_str):
    return line if "✅" in line else f"{line} ✅ {date_str}"


# 앞의 들여쓰기까지 흡수한다 — 렌더가 매번 탭을 새로 붙이므로 여기서 정규화하지 않으면
# 재렌더마다 탭이 누적되고 번호 패턴이 매칭되지 않는다.
# 주제 줄: - [ ] **3.** [[topics/slug|제목]] …   (태스크는 **3-1** 이라 겹치지 않는다)
TOPIC_LINE_RE = re.compile(
    r"^\s*-\s*\[([ xX])\]\s*\*\*(\d+)\.\*\*\s*\[\[" + re.escape(TOPICS_DIRNAME) + r"/([^\]|]+)")

NUM_RE = re.compile(r"^\s*(-\s*\[[ xX]\]\s*)(?:\*\*[0-9]+-[0-9]+\*\*\s*)?(.*)$")


def _numbered(line, num):
    """줄에 **n-j** 번호를 붙인다(이미 있으면 갈아끼운다). 번호는 렌더 순번이라 매번 다시 매긴다.
    num 이 None 이면 번호를 떼기만 한다(주제 없는 태스크)."""
    m = NUM_RE.match(line)
    if not m:
        return line
    return f"{m.group(1)}**{num}** {m.group(2)}" if num else f"{m.group(1)}{m.group(2)}"


def _task_key(line):
    """항목의 동일성 판정 키. **렌더 순번(3-2)과 링크는 반드시 빼야 한다** —
    주제가 하나 추가돼 번호가 밀리면 같은 태스크가 새 항목으로 인식되어
    완료 시각·주제 태그·아카이브 중복 판정이 전부 끊긴다."""
    s = re.sub(r"-\s*\[[ xX]\]", "", line)
    s = re.sub(r"\*\*\d+-\d+\*\*", "", s)
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


def _find_task(lines, needle):
    """`after` 로 지정된 문구와 같은 줄의 위치. 정규화 일치 → 부분 포함 → 유사도 순으로 찾는다."""
    if not needle:
        return None
    n = _norm_line(needle)
    keys = [_norm_line(re.sub(r"\[\[.*?\]\]", "", l)) for l in lines]
    for i, k in enumerate(keys):
        if k == n:
            return i
    for i, k in enumerate(keys):
        if n and (n in k or k in n):
            return i
    best, bi = 0.0, None
    for i, k in enumerate(keys):
        r = difflib.SequenceMatcher(None, n, k).ratio()
        if r > best:
            best, bi = r, i
    return bi if best >= 0.6 else None


def _apply_task_adds(open_cur, adds, topic, conv_link):
    """새 태스크를 목록에 넣는다. after 가 가리키는 줄 **뒤에**, 못 찾으면 맨 뒤에.

    기존 줄은 손대지 않는다 — LLM 은 추가분만 주므로 순서·태그·링크가 훼손될 수 없다."""
    out = list(open_cur)
    for a in adds or []:
        text = a["text"]
        if _dedup_against([text], [re.sub(r"\[\[.*?\]\]", "", l) for l in out]) == []:
            _debug(f"[worker] 태스크 중복 — 건너뜀: {text[:40]}")
            continue
        line = f"- [ ] {text}"
        if topic:
            line += f"  [[{TOPICS_DIRNAME}/{topic}|🔧]]"
        if conv_link:
            line += f"  [[{conv_link}|↗ 대화]]"
        pos = _find_task(out, a.get("after"))
        if pos is None:
            out.append(line)
        else:
            out.insert(pos + 1, line)
    return out


def _task_topic(line):
    """태스크 줄에 심긴 주제 슬러그."""
    m = re.search(r"\[\[" + re.escape(TOPICS_DIRNAME) + r"/([^|\]]+)", line)
    return m.group(1).strip() if m else None


def _find_task(lines, needle):
    """`after` 로 지정된 문구와 같은 줄의 위치. 정규화 일치 → 부분 포함 → 유사도 순으로 찾는다."""
    if not needle:
        return None
    n = _norm_line(needle)
    keys = [_norm_line(re.sub(r"\[\[.*?\]\]", "", l)) for l in lines]
    for i, k in enumerate(keys):
        if k == n:
            return i
    for i, k in enumerate(keys):
        if n and (n in k or k in n):
            return i
    best, bi = 0.0, None
    for i, k in enumerate(keys):
        r = difflib.SequenceMatcher(None, n, k).ratio()
        if r > best:
            best, bi = r, i
    return bi if best >= 0.6 else None


def _apply_task_adds(open_cur, adds, topic, conv_link):
    """새 태스크를 목록에 넣는다. after 가 가리키는 줄 **뒤에**, 못 찾으면 맨 뒤에.

    기존 줄은 손대지 않는다 — LLM 은 추가분만 주므로 순서·태그·링크가 훼손될 수 없다."""
    out = list(open_cur)
    for a in adds or []:
        text = a["text"]
        if _dedup_against([text], [re.sub(r"\[\[.*?\]\]", "", l) for l in out]) == []:
            _debug(f"[worker] 태스크 중복 — 건너뜀: {text[:40]}")
            continue
        line = f"- [ ] {text}"
        if topic:
            line += f"  [[{TOPICS_DIRNAME}/{topic}|🔧]]"
        if conv_link:
            line += f"  [[{conv_link}|↗ 대화]]"
        pos = _find_task(out, a.get("after"))
        if pos is None:
            out.append(line)
        else:
            out.insert(pos + 1, line)
    return out


def _task_topic(line):
    """태스크 줄에 심긴 주제 슬러그."""
    m = re.search(r"\[\[" + re.escape(TOPICS_DIRNAME) + r"/([^|\]]+)", line)
    return m.group(1).strip() if m else None


def _count_tasks(md):
    """와이프 방지용 개수. **주제 줄은 세지 않는다** — 주제가 남아 있으면
    태스크가 전부 사라져도 0 이 아니게 되어 방지가 무력해진다."""
    o, d = _split_tasks(md)
    return len(o) + len(d)


def _backup_tasks(path, content):
    d = os.path.join(os.path.dirname(path), ".task-backups")
    os.makedirs(d, exist_ok=True)
    ts = datetime.now().strftime("%Y-%m-%d_%H%M%S")
    with open(os.path.join(d, f"tasks_{ts}.md"), "w", encoding="utf-8") as f:
        f.write(content)
    for old in sorted(glob.glob(os.path.join(d, "tasks_*.md")))[:-TASK_BACKUP_KEEP]:
        try:
            os.remove(old)
        except OSError:
            pass


def _safe_write_index(path, new_md):
    cur = ""
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            cur = f.read()
    cur_n, new_n = _count_tasks(cur), _count_tasks(new_md)
    if cur_n >= 1 and new_n == 0:
        _debug(f"[worker] INDEX 덮어쓰기 거부(와이프 방지): {cur_n}→0")
        return False
    if new_n < cur_n:
        # 정당한 감소(아카이브 이동·항목 병합)도 있으므로 막지는 않는다.
        # 다만 LLM 이 조용히 항목을 떨어뜨리는 경우를 사후에 알 수 있어야 한다.
        _debug(f"[worker] 태스크 항목 감소: {cur_n}→{new_n} (직전 사본 .task-backups/)")
    if cur.strip():
        _backup_tasks(path, cur)
    _atomic_write(path, new_md.rstrip() + "\n")
    return True


def _update_tasks(base, open_new, done_cur, conv_link, db_path=DB_FILE, topic=None):
    """미완료/완료를 정리해 INDEX 를 다시 쓴다.

    태스크의 정본은 INDEX.md 하나다 — 별도 목록 파일을 두면 번호가 어긋나고
    '체크했는데 목차엔 남아있다' 가 생긴다."""
    _sync_task_states(base, db_path)  # 폴백: 아직 미기록된 완료 전이 포착
    # done 은 호출자가 넘긴 스냅샷이 아니라 **지금 파일**에서 다시 읽는다.
    # 락을 쥔 채 LLM 을 기다리는 37~91초 사이에 사람이 체크한 항목이 있으면,
    # 옛 스냅샷으로 덮어쓸 때 그 체크가 조용히 되돌려진다.
    tf_now = os.path.join(base, INDEX_FILENAME)
    prev_open = []
    if os.path.exists(tf_now):
        with open(tf_now, encoding="utf-8") as f:
            prev_open, done_cur = _split_tasks(f.read())
    today = datetime.now().strftime("%Y-%m-%d")
    done_keys = {_task_key(d) for d in done_cur}
    open_new = [o for o in (open_new or []) if _task_key(o) not in done_keys]
    # 번호는 렌더 순번이라 완료 시점에 뗀다 (아카이브까지 그대로 간다)
    done_stamped = [_stamp_done(_numbered(d, None), _completion_date(_task_key(d), today, db_path))
                    for d in done_cur]
    cutoff = (datetime.now() - timedelta(days=DONE_RETAIN_DAYS)).strftime("%Y-%m-%d")
    recent, old = [], []
    for d in done_stamped:
        dd = _done_date(d)
        (old if (dd and dd < cutoff) else recent).append(d)
    if old:
        _archive_done(base, old)
        _debug(f"[worker] 완료 아카이브 이동: {len(old)}건")
    recent.sort(key=lambda x: _done_date(x) or "", reverse=True)   # 최신 완료가 위
    _write_index(base, open_new, recent)
    _sync_task_states(base, db_path)  # 쓰기 후 snapshot 갱신


def _git_snapshot(base):
    """vault 가 git 저장소면 이번 flush 결과를 커밋한다.

    topics/ 는 자체 백업이 없어(.task-backups 는 INDEX 전용) git 이 유일한 복구 수단인데
    수동 커밋에만 의존하면 되돌릴 지점이 드문드문해진다. 실패는 무시한다 — 기록이 우선이다."""
    if not os.path.isdir(os.path.join(base, ".git")):
        return
    try:
        st = subprocess.run(["git", "-C", base, "status", "--porcelain"],
                            capture_output=True, text=True, timeout=30)
        if st.returncode != 0 or not st.stdout.strip():
            return
        subprocess.run(["git", "-C", base, "add", "-A"], capture_output=True, timeout=60)
        msg = f"auto: SessionEnd flush {datetime.now():%Y-%m-%d %H:%M}"
        r = subprocess.run(["git", "-C", base, "commit", "-m", msg],
                           capture_output=True, text=True, timeout=60)
        _debug(f"[worker] git 스냅샷 {'완료' if r.returncode == 0 else '실패'}: {msg}")
    except Exception as e:
        _debug("git 스냅샷 예외: " + repr(e))


# ── 동시성 락 ───────────────────────────────────────────────────────
@contextlib.contextmanager
def _vault_lock(blocking=True):
    """blocking=False 면 잡히지 않을 때 곧바로 False 를 준다.
    FileChanged 는 사람이 체크한 직후 도는 훅이라 워커의 LLM 대기(최대 90초)를
    기다리면 안 된다 — 못 잡으면 건너뛰고, 어차피 그 워커가 끝나며 다시 렌더한다."""
    f = open(LOCK_FILE, "w")
    got = False
    try:
        try:
            fcntl.flock(f.fileno(), fcntl.LOCK_EX if blocking else fcntl.LOCK_EX | fcntl.LOCK_NB)
            got = True
        except OSError:
            got = False
        yield got
    finally:
        if got:
            try:
                fcntl.flock(f.fileno(), fcntl.LOCK_UN)
            except OSError:
                pass
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


def _write_conversation_page(base, sid8, date, turns, title=None, progress=None, topic=None):
    """그날 대화 원문. 세션 허브를 두지 않으므로 진행 요약도 여기 얹는다
    (주제가 잡힌 세션은 topics/ 가 정본이고, 여기 요약은 대화를 여는 사람용 머리말)."""
    d = os.path.join(base, CONV_DIRNAME)
    os.makedirs(d, exist_ok=True)
    path = os.path.join(d, f"{sid8}_{date}.md")
    body = _render_turns(turns)
    if os.path.exists(path):
        with open(path, "a", encoding="utf-8") as f:
            f.write("\n" + body)
        return
    # title frontmatter: Front Matter Title 플러그인이 파일명 대신 표시
    disp = f"{title or topic or '대화'} · {date}"
    head = f"---\ntitle: {_yaml_val(disp)}\n---\n\n"
    with open(path, "w", encoding="utf-8") as f:
        f.write(head)
        if topic:
            f.write(f"> 주제: [[{TOPICS_DIRNAME}/{topic}]]\n\n")
        if progress:
            f.write(f"## 📈 이날 진행\n\n{progress.rstrip()}\n\n")
        f.write(f"# 💬 {date} 대화\n\n" + body)


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


SUBSTR_MIN = 6      # 부분 포함으로 중복 판정하려면 이 길이 이상이어야 한다


def _norm_line(s):
    return re.sub(r"[\s`*_·,.\-—()\[\]]+", "", s).lower()


def _dedup_against(items, existing, threshold=0.75):
    """기존 항목과 유사한 것을 걸러낸다. 요약기는 기존 목록을 못 보므로
    (topic 이 같은 콜에서 정해져 프롬프트에 미리 넣을 수 없다) 파이썬에서 막는다."""
    norm = _norm_line
    out, seen = [], [norm(e) for e in existing]
    for it in items:
        n = norm(it)
        if not n:
            continue
        # 부분 포함 검사에는 최소 길이를 둔다 — 짧은 항목이 긴 항목의 우연한 부분 문자열로
        # 걸려 사라진다("배포" 가 "VM 재배포…" 에 포함되는 식).
        if any((len(n) >= SUBSTR_MIN and (n in e or e in n))
               or difflib.SequenceMatcher(None, n, e).ratio() >= threshold
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
    # 같은 (날짜, 세션) 블록이 이미 있으면 **그 블록 끝에 이어붙인다.**
    # 건너뛰면 하루에 두 번 flush 될 때(자정 자동 flush → 그날 세션 종료) 뒤쪽 진행이 유실된다.
    marker = f"### {date}  [[{CONV_DIRNAME}/{sid8}_{date}"
    i0 = txt.find(marker)
    if i0 != -1:
        j0 = txt.find("\n### ", i0 + 1)
        blk_end = j0 if j0 != -1 else len(txt.rstrip())
        # 진행 로그는 **완전 일치**만 중복으로 본다. 유사도(0.75)를 쓰면
        # "오전 작업"/"오후 작업" 처럼 짧고 다른 줄이 오탐으로 사라진다 — 유실보다 중복이 낫다.
        have = {_norm_line(l.strip().lstrip("-").strip())
                for l in txt[i0:blk_end].splitlines() if l.strip().startswith("-")}
        fresh = [x for x in (l.strip().lstrip("-").strip()
                             for l in progress.splitlines() if l.strip().startswith("-"))
                 if x and _norm_line(x) not in have]
        if fresh:
            add = "\n" + "\n".join(f"- {x}" for x in fresh)
            txt = txt[:blk_end] + add + txt[blk_end:]
            _debug(f"[worker] topics/{slug}: {date}/{sid8} 블록에 +{len(fresh)}줄 이어붙임")
        else:
            _debug(f"[worker] topics/{slug}: {date}/{sid8} 새 진행 없음")
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
    _atomic_write(path, txt.rstrip() + "\n")
    return True


def _sync_topic_status(base):
    """INDEX 에서 체크된 주제를 `topics/<slug>.md` 의 status: done 으로 반영한다.
    주제를 닫는 것도 목차에서 할 수 있어야 한다 — 안 그러면 태스크가 0이 된 주제가
    '끝난 것'인지 '아직 안 쪼갠 것'인지 목차만 봐서는 알 수 없다."""
    p = os.path.join(base, INDEX_FILENAME)
    if not os.path.exists(p):
        return
    with open(p, encoding="utf-8") as f:
        lines = f.read().splitlines()
    for ln in lines:
        m = TOPIC_LINE_RE.match(ln)
        if not m or m.group(1).lower() != "x":
            continue
        tp = os.path.join(base, TOPICS_DIRNAME, f"{m.group(3)}.md")
        if not os.path.exists(tp):
            continue
        with open(tp, encoding="utf-8") as f:
            txt = f.read()
        if re.search(r"^status:\s*done\s*$", txt, re.M):
            continue
        txt = _fm_set(txt, "status", "done")
        txt = _fm_set(txt, "updated", datetime.now().strftime("%Y-%m-%d"))
        _atomic_write(tp, txt)
        _debug(f"[worker] topics/{m.group(3)}: status → done (INDEX 체크)")


def _topic_order(base):
    """진행 중 주제를 표시 순서대로. INDEX 와 작업현황이 **같은 번호**를 쓰게 하려면
    순서를 한 곳에서 정해야 한다. 두 파일이 다른 순서를 내면 5-2 를 보고 찾아갈 수 없다."""
    d = os.path.join(base, TOPICS_DIRNAME)
    entries = []
    for slug in _topic_slugs(base):
        m = _topic_meta(os.path.join(d, f"{slug}.md"))
        st = m.get("status") or "active"
        if st != "done":
            entries.append((slug, m, st))
    entries.sort(key=lambda e: e[2] == "paused")   # 보류는 뒤로 (stable)
    return entries


def _open_tasks_by_topic(base, open_lines=None):
    """미완료 태스크를 주제별로 나눈다 → ({slug: [원문 줄]}, [무소속 줄]).
    open_lines 가 없으면 INDEX 자신에서 읽는다(사람이 체크만 한 경우)."""
    if open_lines is None:
        p = os.path.join(base, INDEX_FILENAME)
        open_lines = _split_tasks(open(p, encoding="utf-8").read())[0] if os.path.exists(p) else []
    by_topic, orphan = {}, []
    for l in open_lines:
        t = _task_topic(l)
        (by_topic.setdefault(t, []) if t else orphan).append(l)
    return by_topic, orphan


def _write_index(base, open_lines=None, done_lines=None):
    """INDEX.md 재생성. **태스크의 정본이자 목차**다.

    사람은 체크박스만 건드리고 나머지는 자동 생성이다. open/done 을 주지 않으면
    현재 파일에서 읽어 그대로 다시 렌더한다(FileChanged 로 체크만 바뀐 경우)."""
    d = os.path.join(base, TOPICS_DIRNAME)
    os.makedirs(d, exist_ok=True)
    path = os.path.join(base, INDEX_FILENAME)
    _sync_topic_status(base)   # 체크된 주제는 목록에서 빠지도록 먼저 반영
    if open_lines is None or done_lines is None:
        cur = open(path, encoding="utf-8").read() if os.path.exists(path) else ""
        o, dn = _split_tasks(cur)
        open_lines = o if open_lines is None else open_lines
        done_lines = dn if done_lines is None else done_lines

    by_topic, orphan = _open_tasks_by_topic(base, open_lines)
    out = ["# 🧭 INDEX", "",
           "> 주제·할 일 목차. **체크박스만 직접 건드리세요** — 나머지는 세션 종료 시 다시 씁니다.",
           "> 주제를 체크하면 그 주제가 닫히고(`status: done`) 목록에서 빠집니다.", "",
           "## 🔧 진행 중인 주제", ""]
    active, paused = [], []
    for n, (slug, m, st) in enumerate(_topic_order(base), 1):
        mine = by_topic.pop(slug, [])
        line = f"- [ ] **{n}.** [[{TOPICS_DIRNAME}/{slug}|{m.get('title') or slug}]] `{st}`"
        # 태스크가 있으면 그것이 곧 '다음'이다. 🔜 다음 을 함께 실으면 같은 말이 두 번 나온다.
        line += f" · 남은 일 {len(mine)}" if mine else f" — {m.get('next') or '_(다음 미기재)_'}"
        # 설계 문서 포인터는 **지시문**으로 둔다. "그쪽에 있다"로 끝나는 서술문은
        # 읽을지 여부를 읽는 쪽 판단에 맡기므로 전달이 보장되지 않는다.
        if m.get("plan"):
            line += f"\n  📄 먼저 `{m['plan']}` 를 읽어라. 전역 결정·기각 이력은 그쪽에 있다."
        for j, l in enumerate(mine, 1):
            line += "\n\t" + _numbered(l, f"{n}-{j}")
        (paused if st == "paused" else active).append(line)
    # 끝난(done) 주제나 사라진 슬러그에 달린 태스크는 흘려보내지 않고 기타로 모은다
    for left in by_topic.values():
        orphan += left

    out += active or ["_(없음)_"]
    if orphan:
        out += ["", "## ☑️ 기타 태스크", ""] + [_numbered(l, None) for l in orphan]
    if paused:  # 지금 손대지 않는 것이므로 아래로
        out += ["", "## ⏸ 보류", ""] + paused
    out += ["", TASKS_DONE_HEADER, "",
            f"> {DONE_RETAIN_DAYS}일이 지나면 [[{os.path.splitext(ARCHIVE_FILENAME)[0]}]] 로 옮겨집니다.", ""]
    out += [_numbered(l, None) for l in done_lines] if done_lines else ["_(완료 항목 없음)_"]
    _safe_write_index(path, "\n".join(out).rstrip() + "\n")


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
    for fn in (INDEX_FILENAME, ARCHIVE_FILENAME):
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
            project = _project_of(meta)

            current_tasks = ""
            tf = os.path.join(base, INDEX_FILENAME)
            if os.path.exists(tf):
                current_tasks = open(tf, encoding="utf-8").read()
            open_cur, done_cur = _split_tasks(current_tasks)

            choices = _topic_choices(base)   # (slug, title) 닫힌 선택지
            groups = _group_by_date(new_turns, started)
            last_summary, last_conv = None, None
            processed_upto, done_groups = processed, 0
            for date, dturns in groups:
                dmeta = {**meta, "turns": dturns}
                summary = summarize(dmeta, "\n".join(open_cur), use_llm=use_llm, choices=choices)
                if summary is None:
                    # LLM 호출 실패(오프라인·타임아웃 등). 여기서 멈추고 **마커를 전진시키지 않는다** —
                    # 그래야 다음 실행(자정 flush 등)이 이 구간을 다시 요약한다.
                    _debug(f"[worker] {date}: 요약 실패 — 여기서 중단, 다음 실행에서 재시도")
                    break
                last_summary = summary
                prog = summary["progress"].rstrip()
                topic = summary.get("topic")
                on_topic = bool(topic) and _append_topic(
                    base, topic, date, sid8, prog, summary["resume"],
                    summary.get("conclusions"), summary.get("dropped"))
                # 주제에 붙었으면 진행 로그 정본은 topics/ 다. 대화 페이지에는 머리말로만 얹는다.
                _write_conversation_page(base, sid8, date, dturns, meta.get("title"),
                                         None if on_topic else prog, topic if on_topic else None)
                last_conv = f"{CONV_DIRNAME}/{sid8}_{date}"
                _append_daily(base, date, topic if on_topic else project,
                              summary["progress"],
                              f"{TOPICS_DIRNAME}/{topic}" if on_topic else last_conv)
                _log_usage(db_path, sid, date, topic, summary.get("_parts") or {}, summary.get("_usage"))
                # 날짜별 결과를 이어받는다. 마지막 요약만 쓰면 중간 날짜에서 나온 태스크가
                # 통째로 사라진다 — 다중 활동일 flush 는 실측 42%(19건 중 8건)다.
                # '#완료' 면 open_cur 를 그대로 둔다 — 기존 목록이 다시 쓰여 신규가 안 생긴다.
                if not task_skip:
                    open_cur = _apply_task_adds(open_cur, summary.get("tasks_add"),
                                                topic if on_topic else None, last_conv)
                processed_upto += len(dturns)
                done_groups += 1
                _debug(f"[worker] {date}: topic={topic or 'none'} 진행 로그·대화·데일리 기록")

            if done_groups == 0:
                _debug("[worker] ABORT: 한 그룹도 처리 못 함 — 마커 유지")
                return

            _update_tasks(base, open_cur, done_cur, last_conv, db_path,
                          last_summary.get("topic"))
            # 이번 flush가 건드린 모든 주차 + 이번 주를 재생성 (주 경계 넘김 대응)
            weeks = {}
            for date, _ in groups[:done_groups]:
                try:
                    dd = datetime.strptime(date, "%Y-%m-%d")
                except ValueError:
                    continue
                weeks[(dd.isocalendar()[0], dd.isocalendar()[1])] = dd
            now = datetime.now()
            weeks[(now.isocalendar()[0], now.isocalendar()[1])] = now
            for dd in weeks.values():
                _write_weekly_digest(base, dd)
            db_set_processed(sid, processed_upto, db_path)
            _git_snapshot(base)
            _debug(f"[worker] DONE: +{processed_upto - processed}turn, {done_groups}/{len(groups)}일")
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


CATCHUP_DAYS = int(os.environ.get("SESSIONLOG_CATCHUP_DAYS", "3"))


LAUNCHD_LOG_MAX = 1_000_000     # launchd StandardOut/ErrorPath 는 로테이션이 없다


def _trim_launchd_logs():
    """launchd 가 append 하는 로그에 상한을 둔다. 정상 동작 시에는 비어 있지만
    (진행 내역은 _debug 로 별도 파일에 가고 그쪽은 7일 로테이션이 있다),
    예외가 반복되면 무한히 자랄 수 있어 실행 끝에 잘라 둔다."""
    for f in glob.glob("/tmp/claude-obsidian-logger.catchup.*"):
        try:
            if os.path.getsize(f) > LAUNCHD_LOG_MAX:
                with open(f, "w"):
                    pass
                _debug(f"[catchup] {os.path.basename(f)} 이 상한 초과 — 비움")
        except OSError:
            pass


def _catchup():
    """열려 있는(=최근 수정된) 세션들을 SessionEnd 와 똑같이 기록한다. 세션 자체는 건드리지 않는다.

    증분 마커가 있어 이미 처리된 세션은 즉시 스킵되므로 '열림' 여부를 정확히 가릴 필요가 없다.
    LLM 호출이 실패하면 _process 가 마커를 전진시키지 않으므로 다음 실행이 그대로 재시도한다."""
    cutoff = datetime.now().timestamp() - CATCHUP_DAYS * 86400
    root = os.path.expanduser("~/.claude/projects")
    files = [f for f in glob.glob(os.path.join(root, "*", "*.jsonl"))
             if os.path.getmtime(f) >= cutoff]
    _debug(f"[catchup] 대상 {len(files)}개 (최근 {CATCHUP_DAYS}일)")
    for f in sorted(files, key=os.path.getmtime):
        _process(f)
    _trim_launchd_logs()
    _debug("[catchup] 완료")


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
    if "--catchup" in sys.argv:   # launchd 자정 실행: 열린 세션도 기록
        return _catchup()
    if "--worker" in sys.argv:
        return _process(sys.argv[sys.argv.index("--worker") + 1])
    if "--filechanged" in sys.argv:  # INDEX 외부 편집 감지 → 완료 전이 기록 + 즉시 재렌더
        try:
            sys.stdin.read()
        except Exception:
            pass
        _sync_task_states(VAULT)
        with _vault_lock(blocking=False) as got:
            if got:
                _write_index(VAULT)   # 체크한 항목이 바로 완료 섹션으로 내려간다
            else:
                _debug("[filechanged] 워커가 락 보유 — 재렌더는 그쪽에 맡김")
        return

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

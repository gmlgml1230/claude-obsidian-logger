#!/usr/bin/env python3
"""session_start_inject.py — Claude Code SessionStart hook.

Obsidian vault 의 INDEX.md(주제 진입점)를 새 세션 컨텍스트에 주입해
'무슨 주제가 진행 중이고 다음이 무엇인지'를 자동 복구한다.
파일 echo 라 추가 API 콜 0.

INDEX.md 는 session_log.py 가 topics/ 와 작업현황에서 0토큰으로 재생성한다.
아직 없으면 기존 동작(작업현황 미완료 주입)으로 폴백한다.
"""
import os
import sys
import json

VAULT = os.environ.get("OBSIDIAN_VAULT") or os.path.expanduser("~/Documents/Obsidian")
TASKS_FILENAME = os.environ.get("SESSIONLOG_TASKS_FILE", "📌 작업현황.md")
INDEX_FILENAME = "INDEX.md"
SKIP_PREFIXES = ("# 🧭", "> 주제 진입점")   # 헤더·안내문은 주입에서 제외


def _emit(context):
    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "SessionStart",
            "additionalContext": context,
        }
    }, ensure_ascii=False))


def _from_index():
    path = os.path.join(VAULT, INDEX_FILENAME)
    if not os.path.exists(path):
        return None
    with open(path, encoding="utf-8") as f:
        body = f.read()
    lines = [ln.rstrip() for ln in body.splitlines()
             if ln.strip() and not ln.startswith(SKIP_PREFIXES)]
    if not lines:
        return None
    return ("진행 중인 주제와 작업입니다 (Obsidian INDEX.md — 주제 본문은 "
            "`topics/<slug>.md` 에 있습니다):\n\n" + "\n".join(lines))


def _from_tasks():
    path = os.path.join(VAULT, TASKS_FILENAME)
    if not os.path.exists(path):
        return None
    with open(path, encoding="utf-8") as f:
        tasks = f.read()
    open_lines = [ln.rstrip() for ln in tasks.splitlines()
                  if ln.lstrip().lower().startswith("- [ ]")]
    if not open_lines:
        return None
    return "최근 세션들에서 진행 중인 작업(미완료) 목록입니다:\n\n" + "\n".join(open_lines)


def main():
    # 요약기(claude -p) 세션엔 주입 안 함 (컨텍스트 오염 방지)
    if os.environ.get("CLAUDE_SESSIONLOG_RUNNING"):
        return
    context = _from_index() or _from_tasks()
    if context:
        _emit(context)


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""버그 하드닝 회귀 테스트. 각 항목은 실제로 재현된 결함에 대응한다."""
import importlib.util, json, os, re, subprocess, tempfile, shutil

HOOK = os.path.join(os.path.dirname(__file__), "..", "hooks", "session_log.py")
spec = importlib.util.spec_from_file_location("sl", HOOK)
sl = importlib.util.module_from_spec(spec); spec.loader.exec_module(sl)

FAIL = []
def chk(name, got, want=True):
    if got != want:
        FAIL.append(name); print(f"✗ {name}\n   got : {got!r}\n   want: {want!r}")
    else:
        print(f"✓ {name}")

SKELETON = ('---\ntitle: "T"\nstatus: active\nupdated: 2026-08-20\n---\n\n'
            "## 📌 결론\n\n- _(없음)_\n\n## ❌ 접은 안\n\n- _(없음)_\n\n"
            "## 📈 진행 로그\n\n## 🔜 다음\n")


def main():
    tmp = tempfile.mkdtemp()
    sl.DEBUG_LOG_DIR = tmp
    try:
        base = os.path.join(tmp, "v"); os.makedirs(os.path.join(base, "topics"))
        db = os.path.join(tmp, "t.db")
        tp = os.path.join(base, "topics", "t.md")

        # ① 같은 날 두 번째 flush 가 첫 진행을 지우지 않고, 자기 자신도 살아남는다
        open(tp, "w", encoding="utf-8").write(SKELETON)
        sl._append_topic(base, "t", "2026-08-21", "abc12345", "- 첫 진행", "첫 다음")
        sl._append_topic(base, "t", "2026-08-21", "abc12345", "- 둘째 진행", "둘째 다음")
        txt = open(tp, encoding="utf-8").read()
        chk("같은 날 2차 flush: 첫 진행 보존", "첫 진행" in txt)
        chk("같은 날 2차 flush: 둘째 진행 보존", "둘째 진행" in txt)
        chk("같은 날 2차 flush: 다음 갱신", "둘째 다음" in txt and "첫 다음" not in txt)
        chk("진행이 '다음' 아래로 새지 않음",
            txt.index("둘째 진행") < txt.index(sl.NEXT_HEADER))

        # ② frontmatter 만 있고 섹션이 없는 주제에서도 결론이 남는다
        open(tp, "w", encoding="utf-8").write(
            '---\ntitle: "T"\nstatus: active\n---\n\n사람이 적어 둔 메모\n')
        ok = sl._append_topic(base, "t", "2026-08-21", "abc12345", "- 진행", "다음",
                              conclusions=["결론이 사라지면 안 된다"])
        txt = open(tp, encoding="utf-8").read()
        chk("섹션 없는 주제: append 성공", ok)
        chk("섹션 없는 주제: 결론 기록", "결론이 사라지면 안 된다" in txt)
        chk("섹션 없는 주제: 사람 메모 보존", "사람이 적어 둔 메모" in txt)

        # ③ frontmatter 삭제가 본문을 건드리지 않는다
        doc = '---\ntitle: "T"\nblocker: 승인 대기\n---\n\n본문\nblocker: 이건 사람이 쓴 줄\n'
        out = sl._fm_del(doc, "blocker")
        chk("_fm_del: frontmatter 키 제거", "blocker: 승인 대기" not in out)
        chk("_fm_del: 본문 보존", "blocker: 이건 사람이 쓴 줄" in out)

        # ④ 주제가 다르면 같은 문구라도 다른 항목이다
        a = "- [ ] VM 재배포  [[topics/alpha|🔧]]"
        b = "- [x] VM 재배포  [[topics/beta|🔧]]"
        chk("_task_key: 주제가 다르면 다른 키", sl._task_key(a) != sl._task_key(b))
        chk("_task_key: 번호·대화링크는 무시",
            sl._task_key("- [ ] **3-2** VM 재배포  [[topics/alpha|🔧]]  [[conversations/x_2026-01-01|↗]]")
            == sl._task_key(a))

        # ⑤ 형제 태스크가 중복으로 삼켜지지 않는다
        out = sl._apply_task_adds(["- [ ] skillflo sink PII 컬럼 제거 적용"],
                                  [{"text": "skillmatch sink PII 컬럼 제거 적용"}], None, None)
        chk("형제 태스크 보존(skillflo/skillmatch)", len(out) == 2)
        out = sl._apply_task_adds(["- [ ] 똑같은 일"], [{"text": "똑같은 일"}], None, None)
        chk("진짜 중복은 여전히 제거", len(out) == 1)

        # ⑥ LLM 문자열이 구조를 깨지 못한다
        adds = sl._norm_adds([{"text": "정상 작업\n- [x] 가짜 완료"}])
        chk("태스크 개행 제거", "\n" not in adds[0]["text"])
        chk("가짜 체크박스가 줄머리가 되지 않음",
            not any(l.strip().startswith("- [") for l in adds[0]["text"].splitlines()[1:]))
        chk("resume 헤더 무력화", not sl._one_line("다음\n## 새 섹션").startswith("#"))
        chk("progress 는 불릿만", sl._clean_progress("- 진행\n## 침입\n- 진행2")
            == "- 진행\n- 진행2")

        # ⑦ 같은 헤더가 두 번 나와도 전부 읽는다
        conv = ("## 📌 결론\n\n- 첫 결론\n\n# 💬 대화\n\n### 👤\n- 대화 본문\n\n"
                "## 📌 결론\n\n- 둘째 결론\n")
        items = sl._section_items(conv, sl.CONCLUSION_HEADER)
        chk("다중 헤더 전부 수집", items == ["첫 결론", "둘째 결론"])

        # ⑧ 락 대기 중 사람 편집: 해제·수기 추가는 살리고, 삭제는 존중한다
        idx = os.path.join(base, "INDEX.md")
        A, B, C = "- [ ] 작업 A", "- [ ] 작업 B", "- [ ] 사람이 손으로 넣은 C"
        open(idx, "w", encoding="utf-8").write(
            f"# 🧭 INDEX\n\n## ☑️ 기타 태스크\n\n{A}\n\n## ✅ 완료 (2주 보관)\n\n- [x] 작업 B\n")
        base_keys = {sl._task_key(A)}
        # 워커가 기다리는 사이 사람이 B 체크를 풀고 C 를 손으로 넣었다
        open(idx, "w", encoding="utf-8").write(
            f"# 🧭 INDEX\n\n## ☑️ 기타 태스크\n\n{A}\n{B}\n{C}\n")
        sl._update_tasks(base, [A], [], db, base_keys)
        cur = open(idx, encoding="utf-8").read()
        chk("체크 해제한 항목 복원", "작업 B" in cur)
        chk("손으로 넣은 항목 복원", "사람이 손으로 넣은 C" in cur)
        # 이번엔 사람이 A 를 지웠다
        open(idx, "w", encoding="utf-8").write(
            f"# 🧭 INDEX\n\n## ☑️ 기타 태스크\n\n{B}\n{C}\n")
        sl._update_tasks(base, [A, B], [], db, {sl._task_key(A), sl._task_key(B)})
        cur = open(idx, encoding="utf-8").read()
        chk("사람이 지운 항목은 되살리지 않음", "작업 A" not in cur)
        chk("나머지는 그대로", "작업 B" in cur and "사람이 손으로 넣은 C" in cur)
        # 아무도 안 건드린 정상 경로에서는 순서가 그대로여야 한다
        rows = [f"- [ ] 작업 {i}" for i in range(1, 6)]
        open(idx, "w", encoding="utf-8").write(
            "# 🧭 INDEX\n\n## ☑️ 기타 태스크\n\n" + "\n".join(rows) + "\n")
        sl._update_tasks(base, list(rows), [], db, {sl._task_key(r) for r in rows})
        got = [l.strip() for l in open(idx, encoding="utf-8")
               if l.lstrip().startswith("- [ ]") and not sl.HEADER_LINE_RE.match(l)]
        chk("정상 경로에서 순서 보존", got == rows)

        # ⑨ 실패한 세션이 pending 에 남고, 성공하면 빠진다
        tr = os.path.join(tmp, "aaaaaaaa-1111-2222-3333-444444444444.jsonl")
        with open(tr, "w", encoding="utf-8") as f:
            for r, t in (("user", "데모 주제 작업을 이어서 하자. 스키마를 정리하고 "
                                   "테스트를 전부 돌려서 결과를 알려줘."),
                         ("assistant", "스키마를 정리하고 테스트를 돌렸습니다. 18개 전부 통과했습니다.")):
                f.write(json.dumps({"type": r, "timestamp": "2026-08-21T10:00:00Z", "cwd": tmp,
                                    "message": {"role": r, "content": t}},
                                   ensure_ascii=False) + "\n")
        real = sl.summarize
        sl.summarize = lambda *a, **k: None                     # LLM 실패
        sl._process(tr, base=base, db_path=db)
        chk("요약 실패 → pending 등록", tr in sl._pending_list(db))
        chk("요약 실패 → 마커 전진 안 함",
            sl.db_get_processed("aaaaaaaa-1111-2222-3333-444444444444", db) == 0)
        sl.summarize = lambda *a, **k: {
            "topic": None, "topic_title": "", "progress": "- 했다", "resume": "다음",
            "verified": "", "blocker": "", "conclusions": [], "dropped": [],
            "tasks_add": [], "_usage": None, "_parts": {}}
        sl._process(tr, base=base, db_path=db)
        chk("성공하면 pending 해제", sl._pending_list(db) == [])
        # 상한: 파일이 실제로 있어야 '만료 정리'가 아니라 '상한'으로 빠지는지 검증된다
        stuck = os.path.join(tmp, "stuck.jsonl"); open(stuck, "w").write("{}\n")
        sl._pending_add(stuck, db)
        chk("상한 이내면 목록에 있음", stuck in sl._pending_list(db))
        for _ in range(sl.PENDING_MAX_TRIES):
            sl._pending_add(stuck, db)
        chk("재시도 상한 초과분은 목록에서 빠짐", stuck not in sl._pending_list(db))
        # 원본이 사라진 항목은 정리된다 (30일 뒤 트랜스크립트 삭제)
        sl._pending_add("/does/not/exist.jsonl", db)
        chk("만료된 pending 은 정리", "/does/not/exist.jsonl" not in sl._pending_list(db))
        sl.summarize = real

        # ⑩ git 스냅샷은 훅 경로만, 삭제도 반영
        g = os.path.join(tmp, "g"); os.makedirs(os.path.join(g, "topics"))
        subprocess.run(["git", "-C", g, "init", "-q"], check=True)
        for k, v in (("user.email", "t@t"), ("user.name", "t")):
            subprocess.run(["git", "-C", g, "config", k, v], check=True)
        open(os.path.join(g, "topics", "a.md"), "w").write("a")
        open(os.path.join(g, "내노트.md"), "w").write("사람이 편집 중")
        subprocess.run(["git", "-C", g, "add", "topics"], capture_output=True)
        subprocess.run(["git", "-C", g, "commit", "-qm", "init"], capture_output=True)
        os.remove(os.path.join(g, "topics", "a.md"))
        open(os.path.join(g, "topics", "b.md"), "w").write("b")
        sl._git_snapshot(g)
        show = subprocess.run(["git", "-C", g, "show", "--name-status", "HEAD"],
                              capture_output=True, text=True).stdout
        chk("스냅샷: 삭제 반영", "topics/a.md" in show and "D\t" in show)
        chk("스냅샷: 신규 반영", "topics/b.md" in show)
        chk("스냅샷: 사람 노트 제외", "내노트" not in show)
        os.remove(os.path.join(g, "topics", "b.md"))
        open(os.path.join(g, "INDEX.md"), "w").write("i")
        sl._git_snapshot(g)
        os.remove(os.path.join(g, "INDEX.md"))
        sl._git_snapshot(g)
        show = subprocess.run(["git", "-C", g, "show", "--name-status", "HEAD"],
                              capture_output=True, text=True).stdout
        chk("스냅샷: 최상위 파일 삭제도 커밋", "D\tINDEX.md" in show)

        # ⑪ 중복 판정은 같은 주제 안에서만, 부분포함은 태스크에서 끈다
        chk("교차 주제 같은 문구는 각자 생긴다",
            len(sl._apply_task_adds(["- [ ] VM 재배포  [[topics/alpha|🔧]]"],
                                    [{"text": "VM 재배포"}], "beta", None)) == 2)
        chk("앞 단계가 뒤 단계의 부분문자열이어도 남는다",
            len(sl._apply_task_adds(["- [ ] 운영 데이터 백필"],
                                    [{"text": "운영 데이터 백필 결과 검증"}], None, None)) == 2)
        # 접두어가 긴 형제 쌍(0.923). 임계값이 여기까지 내려오면 태스크가 소리 없이 사라진다.
        chk("접두어가 긴 형제 태스크도 둘 다 남는다",
            len(sl._apply_task_adds(["- [ ] dim_voucher 증분 적재 누락 백필"],
                                    [{"text": "dim_voucher_use 증분 적재 누락 백필"}],
                                    None, None)) == 2)

        # ⑫ 결론·접은 안도 한 줄, _one_line 은 선두 기호를 보존
        c1 = sl._valid_summary({"topic": "x", "conclusions": ["정상\n## 침입\n- [x] 가짜"]})
        chk("결론 한 줄 강제", "\n" not in c1["conclusions"][0])
        chk("_one_line: CLI 옵션 보존", sl._one_line("--force 없이 재실행") == "--force 없이 재실행")
        chk("_one_line: 이슈 번호 보존", sl._one_line("#123 확인") == "#123 확인")
        chk("_one_line: 불릿은 제거", sl._one_line("- 불릿") == "불릿")
        chk("_one_line: 잘리면 표시", sl._one_line("가" * 400).endswith("…"))

        # ⑬ frontmatter 문자열을 섹션으로 오인하지 않는다
        chk("헤더는 줄 전체로만 인식",
            sl._ensure_sections('---\ntitle: "## 📌 결론"\n---\n\n본문\n')
            .count("\n## 📌 결론") == 1)

        # ⑭ 같은 구간 재처리가 대화·daily 를 두 번 쌓지 않는다
        turns = [("user", ["안녕하세요 반갑습니다"], None)]
        for _ in range(2):
            sl._write_conversation_page(base, "ab12cd34", "2026-08-21", turns, "T", "- 진행")
            sl._append_daily(base, "2026-08-21", "t", "- 진행", "conversations/ab12cd34_2026-08-21")
        cp = open(os.path.join(base, "conversations", "ab12cd34_2026-08-21.md"), encoding="utf-8").read()
        chk("대화 본문 1회만", cp.count("안녕하세요 반갑습니다") == 1)
        chk("daily 1줄만",
            open(os.path.join(base, "daily", "2026-08-21.md"), encoding="utf-8").read().count("[t]") == 1)

        # ⑮ 완료일은 최신, 열린 줄에는 스탬프가 없다
        chk("열린 줄 스탬프 제거", "✅" not in sl._unstamp("- [ ] X ✅ 2026-01-01"))
        with sl._db_tasks(db) as c:
            for ts in ("2026-01-01T00:00:00", "2026-08-21T00:00:00"):
                c.execute("INSERT INTO task_events VALUES(?,?,?)", ("k", "done", ts))
        chk("재완료 시 최신 완료일", sl._completion_date("k", "fb", db) == "2026-08-21")

        # ⑯ 재시도 상한을 넘겨도 경고 집계에는 남는다
        stuck2 = os.path.join(tmp, "stuck2.jsonl"); open(stuck2, "w").write("{}")
        for _ in range(sl.PENDING_MAX_TRIES + 1):
            sl._pending_add(stuck2, db)
        chk("상한 초과: 자동 재시도 제외", stuck2 not in sl._pending_list(db))
        chk("상한 초과: 경고에는 남음",
            any(t == stuck2 and n >= sl.PENDING_MAX_TRIES for t, n in sl._pending_rows(db)))

        # ⑰ task_key 규칙 교체 시 가짜 완료 이벤트를 만들지 않는다
        vault2 = os.path.join(tmp, "v2"); os.makedirs(vault2)
        db2 = os.path.join(tmp, "m.db")
        open(os.path.join(vault2, "INDEX.md"), "w", encoding="utf-8").write(
            "# I\n\n## ☑️ 기타 태스크\n\n- [ ] 열린 일  [[topics/a|🔧]]\n\n"
            "## ✅ 완료 (2주 보관)\n\n- [x] 끝난 일  [[topics/a|🔧]] ✅ 2026-01-05\n")
        sl._sync_task_states(vault2, db2)
        with sl._db_tasks(db2) as c:
            n_ev = c.execute("SELECT count(*) FROM task_events").fetchone()[0]
        chk("키 교체 첫 실행: 이벤트 0건", n_ev == 0)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    print("\n" + ("=== 전부 통과 ===" if not FAIL else f"=== 실패 {len(FAIL)}건: {FAIL} ==="))
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())

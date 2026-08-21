#!/usr/bin/env python3
"""버그 하드닝 회귀 테스트. 각 항목은 실제로 재현된 결함에 대응한다."""
import importlib.util, os, re, tempfile, shutil

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
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    print("\n" + ("=== 전부 통과 ===" if not FAIL else f"=== 실패 {len(FAIL)}건: {FAIL} ==="))
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())

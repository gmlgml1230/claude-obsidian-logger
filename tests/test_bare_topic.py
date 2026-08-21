#!/usr/bin/env python3
"""빈 주제 파일(사람이 Obsidian 에서 만든 0바이트 노트) 승격 경로 회귀 테스트.

실제 증상: 묶음을 주제로 올리려고 topics/<slug>.md 를 만들었더니
① 목차에 슬러그가 그대로 뜨고 ② 주제를 체크해도 다음 렌더에서 체크가 풀렸다.
"""
import atexit
import importlib.util, os, re, tempfile, shutil

# 실제 ~/.claude/hooks 를 건드리지 않는다 — 그러면 진짜 워커 락과 경합하고
# 실제 pending DB 를 읽어 테스트가 비결정적이 된다. import 시점에 읽히므로 그 전에 건다.
_STATE = tempfile.mkdtemp(prefix="sessionlog-test-")
os.environ["SESSIONLOG_STATE_DIR"] = _STATE
atexit.register(shutil.rmtree, _STATE, True)   # 실행마다 남지 않게

HOOK = os.path.join(os.path.dirname(__file__), "..", "hooks", "session_log.py")
spec = importlib.util.spec_from_file_location("sl", HOOK)
sl = importlib.util.module_from_spec(spec); spec.loader.exec_module(sl)

FAIL = []
def chk(name, got, want=True):
    if got != want:
        FAIL.append(name); print(f"✗ {name}\n   got : {got!r}\n   want: {want!r}")
    else:
        print(f"✓ {name}")

CONV = "ab12cd34_2026-08-18"
TASK_DONE = ("- [x] 복구 실행 확정  [[topics/airflow-incident|Airflow 장애 조사·복구]]  "
             f"[[conversations/{CONV}|↗ 대화]] ✅ 2026-08-21")
TASK_OPEN = ("- [ ] 재발 방지 적용  [[topics/airflow-incident|Airflow 장애 조사·복구]]  "
             f"[[conversations/{CONV}|↗ 대화]]")
OTHER = "- [ ] 무관한 일  [[topics/other|기타 주제]]"


def build(vault):
    os.makedirs(os.path.join(vault, "topics"))
    os.makedirs(os.path.join(vault, "conversations"))
    # 사람이 만든 빈 노트 — 이것이 승격의 문서화된 방법이다
    open(os.path.join(vault, "topics", "airflow-incident.md"), "w").write("")
    open(os.path.join(vault, "conversations", f"{CONV}.md"), "w", encoding="utf-8").write(
        '---\ntitle: "장애 대응 · 2026-08-18"\n---\n\n'
        "## 📈 이날 진행\n\n- dbt deps 타임아웃을 원인으로 특정\n\n"
        "## 📌 결론\n\n- wait_for_downstream 연쇄 실패는 자력 회복 불가\n\n"
        "## ❌ 접은 안\n\n- 운영 VM SSH 직접 접속 — 사용자가 Slack 확인으로 변경\n\n"
        "# 💬 2026-08-18 대화\n\n### 👤\n- 이건 대화라 끌어오면 안 된다\n")
    open(os.path.join(vault, "INDEX.md"), "w", encoding="utf-8").write(
        "# 🧭 INDEX\n\n## 🔧 진행 중인 주제\n\n_(없음)_\n\n"
        f"## ☑️ 기타 태스크\n\n- **1.** Airflow\n\t{TASK_OPEN}\n{OTHER}\n\n"
        f"## ✅ 완료 (2주 보관)\n\n{TASK_DONE}\n")


def main():
    tmp = tempfile.mkdtemp()
    try:
        vault = os.path.join(tmp, "vault")
        sl.DEBUG_LOG_DIR = tmp      # 테스트 흔적이 실제 디버그 로그를 오염시키지 않게
        build(vault)
        tp = os.path.join(vault, "topics", "airflow-incident.md")

        sl._write_index(vault)
        txt = open(tp, encoding="utf-8").read()
        idx = open(os.path.join(vault, "INDEX.md"), encoding="utf-8").read()
        chk("빈 파일에 frontmatter 생성", txt.startswith("---\n"))
        chk("제목은 태스크 링크 alias 에서", "Airflow 장애 조사·복구" in txt.splitlines()[1])
        chk("목차에 슬러그 대신 제목", "|Airflow 장애 조사·복구]]" in idx)
        chk("결론 이관", "자력 회복 불가" in txt)
        chk("접은 안 이관", "SSH 직접 접속" in txt)
        chk("진행 로그 이관", "타임아웃을 원인으로 특정" in txt)
        chk("대화 본문은 끌어오지 않음", "끌어오면 안 된다" not in txt)
        chk("진행 로그 블록에 대화 링크", f"### 2026-08-18  [[conversations/{CONV}|" in txt)
        chk("created 는 마지막 활동일", "created: 2026-08-18" in txt)
        chk("4개 섹션 모두 존재",
            all(h in txt for h in ("## 📌 결론", "## ❌ 접은 안", "## 📈 진행 로그", "## 🔜 다음")))

        # 두 번째 렌더에서 내용이 불어나지 않아야 한다(멱등)
        sl._write_index(vault)
        chk("멱등", open(tp, encoding="utf-8").read() == txt)

        # 사람이 주제 줄을 체크 → status: done 이 저장되고 다시 풀리지 않아야 한다
        idx = open(os.path.join(vault, "INDEX.md"), encoding="utf-8").read()
        idx2 = re.sub(r"^- \[ \] (\*\*\d+\.\*\* \[\[topics/airflow-incident)", r"- [x] \1",
                      idx, count=1, flags=re.M)
        chk("테스트 전제: 주제 줄 체크됨", idx2 != idx)
        open(os.path.join(vault, "INDEX.md"), "w", encoding="utf-8").write(idx2)
        sl._write_index(vault)
        txt = open(tp, encoding="utf-8").read()
        idx = open(os.path.join(vault, "INDEX.md"), encoding="utf-8").read()
        chk("status: done 저장", bool(re.search(r"^status:\s*done\s*$", txt, re.M)))
        chk("닫힌 주제는 목록에서 빠짐", "**1.** [[topics/airflow-incident" not in idx)
        chk("체크가 풀리지 않음", "- [ ] **1.** [[topics/airflow-incident" not in idx)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    print("\n" + ("=== 전부 통과 ===" if not FAIL else f"=== 실패 {len(FAIL)}건: {FAIL} ==="))
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())

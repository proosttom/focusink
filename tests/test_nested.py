"""Nested task lists: only tasks without subtasks run; parents give context and tick themselves."""
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import focus  # noqa: E402

TODAY = date.today().isoformat()

NESTED = (
    "Target: Ship\n"
    "- [ ] Ship BLE display — 2h\n"
    "  - [ ] Firmware\n"
    "    - [ ] Count-up timer — 25m\n"
    "    - [ ] Tests\n"
    "  - [ ] Mac app — 45m\n"
    "    - [ ] Tests\n"
    "- [ ] Reply to Jan\n"
)


def sprint(tmp_path, text):
    p = tmp_path / "sprint.md"
    p.write_text(text, encoding="utf-8")
    return focus.Sprint(p), p


def finish(sp, minutes=0, skip_break=True):
    if sp.mode == "ready":
        sp.toggle_pause()
    sp.run_since -= minutes * 60
    sp.go()
    if skip_break and sp.mode == "offer":
        sp.go()


def line(p, start):
    return next(l for l in p.read_text(encoding="utf-8").splitlines() if l.lstrip().startswith(start))


def test_parse_tree(tmp_path):
    sp, _ = sprint(tmp_path, NESTED)
    keys = [t.key for t in sp.plan.tasks]
    assert keys[3] == "Ship BLE display › Firmware › Tests"
    assert keys[5] == "Ship BLE display › Mac app › Tests"
    assert [t.leaf for t in sp.plan.tasks] == [False, False, True, True, False, True, True]


def test_only_leaves_run_in_file_order(tmp_path):
    sp, _ = sprint(tmp_path, NESTED)
    seen = []
    while sp.mode != "done":
        seen.append(sp.current.key)
        finish(sp)
        assert len(seen) < 10
    assert seen == ["Ship BLE display › Firmware › Count-up timer", "Ship BLE display › Firmware › Tests",
                    "Ship BLE display › Mac app › Tests", "Reply to Jan"]


def test_duplicate_names_are_logged_on_the_right_line(tmp_path):
    sp, p = sprint(tmp_path, NESTED)
    finish(sp)                                      # Count-up timer
    finish(sp, minutes=7)                           # Firmware › Tests
    lines = p.read_text().splitlines()
    assert lines[4] == f"    - [x] Tests ⏱ 7m ✅ {TODAY}"
    assert lines[6] == "    - [ ] Tests"           # Mac app › Tests untouched


def test_parent_ticks_itself_with_summed_time(tmp_path):
    sp, p = sprint(tmp_path, NESTED)
    finish(sp, minutes=20)
    assert line(p, "- [ ] Firmware")                # one subtask left: still open
    assert sp.mode == "run"                         # skipping Break? started Firmware › Tests
    sp.interrupt(); sp.run_since -= 10 * 60; sp.go(); sp.go()
    assert line(p, "- [x] Firmware") == f"  - [x] Firmware ⏱ 30m ⚡1 ✅ {TODAY}"
    finish(sp, minutes=5)                           # Mac app › Tests -> Mac app and then Ship tick
    assert line(p, "- [x] Mac app") == f"  - [x] Mac app — 45m ⏱ 5m ✅ {TODAY}"
    assert line(p, "- [x] Ship BLE display") == f"- [x] Ship BLE display — 2h ⏱ 35m ⚡1 ✅ {TODAY}"


def test_summary_counts_leaves_only(tmp_path):
    sp, _ = sprint(tmp_path, NESTED)
    for m in (20, 10, 5, 3):
        finish(sp, minutes=m)
    s = focus.summarize(sp.plan.tasks)
    assert s["count"] == 4 and s["actual"] == 38


def test_header_shows_parent(tmp_path):
    sp, _ = sprint(tmp_path, NESTED)
    sp.toggle_pause()
    v = sp.view()
    assert v["header"] == "NOW >>" and v["body"] == "Count-up timer"   # 2 levels deep, no parent names
    assert v["footer"] == "Next: Tests"


def test_header_depth_markers(tmp_path):
    sp, _ = sprint(tmp_path, "- [ ] A\n  - [ ] B\n- [ ] Top\n")
    assert sp.view()["header"] == "READY" or sp.idle()
    sp.toggle_pause()
    assert sp.view()["header"] == "NOW >"
    sp.go(); sp.go()                                  # complete B, skip break -> Top runs
    assert sp.current.title == "Top" and sp.view()["header"] == "NOW"


def test_tabs_and_notes_keep_nesting(tmp_path):
    sp, _ = sprint(tmp_path, "- [ ] A\n\tSome note\n\n\t- [ ] A1\n\t\t- [ ] A1a\n- [ ] B\n")
    assert [t.key for t in sp.pending] == ["A › A1 › A1a", "B"]


def test_hand_ticked_parent_drops_its_subtasks(tmp_path):
    sp, p = sprint(tmp_path, NESTED)
    p.write_text(NESTED.replace("  - [ ] Firmware", "  - [x] Firmware"))
    sp.reload()
    assert sp.current.key == "Ship BLE display › Mac app › Tests"


def test_task_added_below_runs_next(tmp_path):
    sp, p = sprint(tmp_path, NESTED)
    sp.toggle_pause()
    p.write_text(NESTED.replace("    - [ ] Count-up timer — 25m\n",
                                "    - [ ] Count-up timer — 25m\n    - [ ] Hotfix — 5m\n"))
    sp.reload()
    assert sp.current.title == "Count-up timer" and sp.mode == "run"   # running task untouched
    assert sp.next_task().title == "Hotfix"
    finish(sp)
    assert sp.current.title == "Hotfix"


def test_task_added_above_comes_after_wrap(tmp_path):
    sp, p = sprint(tmp_path, NESTED)
    finish(sp)                                      # Count-up timer done; Firmware › Tests is ready
    p.write_text("- [ ] Early bird\n" + p.read_text())
    sp.reload()
    assert sp.current.key == "Ship BLE display › Firmware › Tests"     # ready task kept
    seen = []
    while sp.mode != "done":
        seen.append(sp.current.title)
        finish(sp)
    assert seen == ["Tests", "Tests", "Reply to Jan", "Early bird"]


def test_running_task_that_gains_subtasks_finishes_then_runs_them(tmp_path):
    sp, p = sprint(tmp_path, "- [ ] Write post — 30m\n- [ ] Other\n")
    sp.toggle_pause()
    p.write_text("- [ ] Write post — 30m\n  - [ ] Outline\n  - [ ] Draft\n- [ ] Other\n")
    sp.reload()
    assert sp.current.title == "Write post" and sp.mode == "run"
    finish(sp, minutes=12)
    assert line(p, "- [ ] Write post") == "- [ ] Write post — 30m ⏱ 12m"   # noted, still open
    assert sp.current.key == "Write post › Outline"
    finish(sp, minutes=5)
    finish(sp, minutes=8)
    assert line(p, "- [x] Write post") == f"- [x] Write post — 30m ⏱ 25m ✅ {TODAY}"
    assert sp.current.title == "Other"


def test_skip_moves_to_next_below(tmp_path):
    sp, _ = sprint(tmp_path, NESTED)
    sp.skip()
    assert sp.current.key == "Ship BLE display › Firmware › Tests"

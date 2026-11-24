"""Offline tests for the sprint logic in focus.py (no Bluetooth, no device)."""
import sys
from datetime import date
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import focus  # noqa: E402

TODAY = date.today().isoformat()


def sprint(tmp_path, text):
    p = tmp_path / "sprint.md"
    p.write_text(text, encoding="utf-8")
    return focus.Sprint(p), p


def complete(sp):
    """Start (if ready) and complete the current task, then skip the break prompt."""
    if sp.mode == "ready":
        sp.toggle_pause()
    sp.go()
    if sp.mode == "offer":
        sp.go()


def lines(p):
    return p.read_text(encoding="utf-8").splitlines()


# ---- the bug: an unticked task that still has an old log ----------------------
STALE = (
    "Day description\n"
    "-  [ ] Task1 — 2m  ⏱ 0m ☕ 0m ✅ 2026-11-24\n"
    "-  [ ] Task2 — 2m\n"
    "-  [ ] Untimed task3\n"
    "-  [ ] Last — 1h30m ⏱ 0m ✅ 2026-11-24\n"
)


def test_stale_log_does_not_block_the_next_task(tmp_path):
    sp, p = sprint(tmp_path, STALE)
    assert sp.current.title == "Task1" and sp.current.minutes == 2
    complete(sp)
    assert sp.mode == "run"
    assert sp.current.title == "Task2"


def test_stale_log_is_overwritten(tmp_path):
    sp, p = sprint(tmp_path, STALE)
    complete(sp)
    line = lines(p)[1]
    assert line.startswith("-  [x] Task1 — 2m")
    assert line.count("⏱") == 1 and line.count("✅") == 1
    assert "☕" not in line  # the old break log is gone too
    assert line.endswith(f"✅ {TODAY}")


def test_whole_stale_sprint_runs_to_done(tmp_path):
    sp, p = sprint(tmp_path, STALE)
    seen = []
    while sp.mode != "done":
        seen.append(sp.current.title)
        complete(sp)
        assert len(seen) < 10, "stuck in a loop"
    assert seen == ["Task1", "Task2", "Untimed task3", "Last"]
    assert all(l.startswith("-  [x]") for l in lines(p)[1:])
    assert all(l.count("⏱") == 1 for l in lines(p)[1:])


def test_break_log_after_stale_task(tmp_path):
    sp, p = sprint(tmp_path, STALE)
    sp.toggle_pause(); sp.go()          # complete Task1 -> Break?
    assert sp.mode == "offer"
    sp.toggle_pause()                   # start the break
    sp.run_since -= 6 * 60
    sp.go()                             # end it
    assert sp.current.title == "Task2" and sp.mode == "run"   # the next task starts by itself
    assert "☕ 6m ✅" in lines(p)[1]
    assert lines(p)[1].count("☕") == 1


def test_unfindable_task_never_loops(tmp_path, monkeypatch):
    sp, p = sprint(tmp_path, "- [ ] A\n- [ ] B\n")
    monkeypatch.setattr(focus, "log_done", lambda *a, **k: False)
    complete(sp)
    assert sp.current.title == "B"


# ---- breaks ---------------------------------------------------------------------
@pytest.mark.parametrize("button", ["toggle_pause", "go", "interrupt", "skip"])
def test_any_button_ends_the_break(tmp_path, button):
    sp, p = sprint(tmp_path, "- [ ] A — 10m\n- [ ] B\n")
    sp.toggle_pause(); sp.go(); sp.toggle_pause()
    assert sp.mode == "break"
    sp.run_since -= 125
    payload = sp.payload().split(focus.SEP.encode())
    assert payload[2] == b"Break!" and payload[3] == b"125" and payload[5] == b"up"
    getattr(sp, button)()
    assert sp.mode == "run" and sp.current.title == "B"   # ending a break starts the next task
    assert sp.spent() < 1                                 # with a fresh clock
    assert "☕ 2m" in lines(p)[0]


def test_break_prompt_has_no_footer(tmp_path):
    sp, _ = sprint(tmp_path, "- [ ] A\n- [ ] B\n")
    sp.toggle_pause(); sp.go()
    v = sp.view()
    assert v["body"] == "Break?" and v["footer"] == ""


def test_no_break_offer_after_last_task(tmp_path):
    sp, _ = sprint(tmp_path, "- [ ] A\n")
    sp.toggle_pause(); sp.go()
    assert sp.mode == "done"


# ---- done -> Focus? ---------------------------------------------------------------
def test_complete_on_done_goes_back_to_focus(tmp_path):
    sp, p = sprint(tmp_path, "- [ ] A\n")
    complete(sp)
    assert sp.mode == "done"
    sp.go()
    assert sp.mode == "start" and sp.view()["body"] == "Focus?"
    sp.toggle_pause()                   # nothing to start
    assert sp.mode == "start"
    p.write_text(p.read_text() + "- [ ] New\n")
    sp.reload()
    assert sp.view()["body"] == "Focus?"
    sp.toggle_pause()
    assert sp.mode == "run" and sp.current.title == "New"


# ---- parsing ----------------------------------------------------------------------
@pytest.mark.parametrize("text,title,minutes", [
    ("Write firmware — 25m", "Write firmware", 25),
    ("Mac script — 1h30m", "Mac script", 90),
    ("Thing 1h 30m", "Thing", 90),
    ("Thing (20m)", "Thing", 20),
    ("Thing — 20", "Thing", 20),
    ("Review PR 42", "Review PR 42", None),
])
def test_parse_duration(text, title, minutes):
    assert focus.parse_duration(text) == (title, minutes)


def test_old_log_is_ignored_when_parsing(tmp_path):
    sp, _ = sprint(tmp_path, STALE)
    t = sp.plan.tasks[0]
    assert (t.title, t.minutes, t.done) == ("Task1", 2, False)


def test_skipping_the_break_prompt_starts_the_next_task(tmp_path):
    sp, p = sprint(tmp_path, "- [ ] A\n- [ ] B — 20m\n")
    sp.toggle_pause(); sp.go()
    assert sp.mode == "offer"
    sp.go()
    assert sp.mode == "run" and sp.current.title == "B"
    v = sp.view()
    assert v["header"] == "NOW" and v["mode"] == "run" and sp.spent() < 1
    assert "☕" not in lines(p)[0]                       # no break, nothing logged for it


def test_task_started_by_a_break_is_timed_and_logged(tmp_path):
    sp, p = sprint(tmp_path, "- [ ] A — 10m\n- [ ] B — 20m\n- [ ] C\n")
    sp.toggle_pause(); sp.run_since -= 9 * 60; sp.go()   # A: 9m -> Break?
    sp.toggle_pause(); sp.run_since -= 4 * 60            # 4 min break
    sp.go()                                              # back: B starts on its own
    assert sp.mode == "run" and sp.current.title == "B"
    assert sp.payload().split(focus.SEP.encode())[5] == b"run"  # display counts down
    assert sp.remaining() > 19 * 60                      # break time not charged to B
    sp.run_since -= 15 * 60
    sp.go()                                              # complete B -> Break? for C
    assert sp.mode == "offer"
    l = lines(p)
    assert l[0] == f"- [x] A — 10m ⏱ 9m ☕ 4m ✅ {TODAY}"
    assert l[1] == f"- [x] B — 20m ⏱ 15m ✅ {TODAY}"


def test_break_has_no_length_and_no_alert(tmp_path):
    sp, p = sprint(tmp_path, "Break: 5m\n- [ ] A\n- [ ] B\n")
    assert [t.title for t in sp.plan.tasks] == ["A", "B"]   # an old "Break:" line is just ignored
    sp.toggle_pause(); sp.go(); sp.toggle_pause()
    sp.run_since -= 60 * 60                                 # an hour-long break
    assert sp.mode == "break" and sp.total() == 0
    assert not (sp.counting() and sp.total() and sp.remaining() <= 0)   # Link's alert condition
    sp.adjust(5)                                            # ±5 min does nothing during a break
    assert sp.total() == 0
    sp.go()
    assert "☕ 1h00m" in lines(p)[1]

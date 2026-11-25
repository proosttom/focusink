#!/usr/bin/env python3
"""Focus Sprint -> FocusInk e-paper display over Bluetooth LE.

Reads tasks from a markdown file and shows the current one on the ESP32-S3
e-paper display. Nothing starts by itself: a task is loaded as READY and
only starts when you start it.

Task file format (only unchecked items are run, top to bottom; indent to nest):

    Target: Ship the BLE display

    - [ ] Write firmware — 25m
    - [ ] Mac script — 1h30m
    - [ ] Reply to Jan            <- no duration: untimed, just the task name
    - [x] Done earlier — 10m ⏱ 12m ⚡2 ☕ 6m ✅ 2026-11-24
    - [ ] Firmware                <- a parent: context only, never run itself
        - [ ] Count-up timer — 25m
        - [ ] Sleep fix — 15m

Nesting (tabs or spaces, any depth): only tasks without subtasks are run. The
display header shows one ">" per nesting level ("NOW >>"). A parent ticks
itself when all its subtasks are done, logging their summed time. Ticking a
parent by hand drops its open subtasks. New tasks can be added any time: after
a task, the next one is the first open task below it in the file, wrapping to
the top.

Durations: 25m, 25 min, 1h, 1h30m, 1h 30m, 1h30, (20m), or a bare number
after a dash or in brackets ("— 20", "(20)") = minutes.

Completing a task ticks it and appends what happened: ⏱ time spent (pauses
don't count), ⚡ interruptions, ☕ the break taken after it, ✅ the date.

Controls:
    Start/Pause (lower ▶‖, PWR)   start / pause / resume; on the break prompt: start the break; during a break: end it
    Start/Pause double-press     count an interruption (while a task runs or is paused)
    Complete (upper ✓, BOOT)     complete the task / skip the break prompt / end the break (either way the next task starts)
    Complete held 1 s            skip (task: not ticked, not logged)
    During a break any button ends it and starts the next task: breaks can't be paused.
    terminal: Enter = complete, p = start/pause, i = interruption, s = skip, +/- = ±5 min, q = quit
Ticking the running task in the file (in any editor) also completes it.
"""

import argparse
import asyncio
import os
import re
import sys
import time
import unicodedata
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

from bleak import BleakClient, BleakScanner

SERVICE_UUID = "6e5a0001-8b3c-4a5e-9f1d-0f0c05f5a001"
STATE_UUID = "6e5a0002-8b3c-4a5e-9f1d-0f0c05f5a001"
EVENT_UUID = "6e5a0003-8b3c-4a5e-9f1d-0f0c05f5a001"
DEVICE_NAME = "FocusInk"
SEP = "\x1f"       # between payload fields
LINE_SEP = "\x1e"  # between lines of a list screen

IDLE_AFTER = 120   # seconds a task may wait as READY before the display shows the big "Focus?" screen

TASK_RE = re.compile(r"^(\s*)[-*+]\s+\[( ?|x|X)\]\s+(.*\S)\s*$")  # "[]" counts as unticked
TAB = 4  # a tab counts as 4 spaces of indentation
META_RE = re.compile(r"\s*(?:⏱|⚡|☕|✅).*$")  # logged time / interruptions / break / done date
UNIT_DUR_RE = re.compile(
    r"(?:^|\s)(?:[—–-]\s*)?\(?\s*"
    r"(?P<d>\d+\s*h(?:\s*\d+\s*(?:m(?:in(?:s|utes?)?)?)?)?|\d+\s*m(?:in(?:s|utes?)?)?)"
    r"\s*\)?\s*$",
    re.IGNORECASE,
)
BARE_DUR_RE = re.compile(r"(?:\s[—–-]\s*|\s*\(\s*)(?P<d>\d+)\s*\)?\s*$")
TARGET_RE = re.compile(r"^\s*(?:target|goal)\s*:\s*(.+)$", re.IGNORECASE)
LOG_SPENT_RE = re.compile(r"⏱\s*(\d+h\d+m|\d+m)")
LOG_INT_RE = re.compile(r"⚡\s*(\d+)")
LOG_BREAK_RE = re.compile(r"☕\s*(\d+h\d+m|\d+m)")
LOG_DATE_RE = re.compile(r"✅\s*(\d{4}-\d{2}-\d{2})")


CRUMB = " › "  # between levels of a task's path


@dataclass
class Task:
    line_no: int
    title: str
    minutes: int | None  # None = untimed
    done: bool
    raw: str = ""
    indent: int = 0
    path: tuple = ()                 # titles from the top-level ancestor down to this task
    parent: "Task | None" = field(default=None, repr=False, compare=False)
    children: list = field(default_factory=list, repr=False, compare=False)

    @property
    def key(self) -> str:
        """Identifies the task across reloads: its full path, so equal names under different parents differ."""
        return CRUMB.join(self.path or (self.title,))

    @property
    def leaf(self) -> bool:
        return not self.children

    def closed(self) -> bool:
        """Ticked itself, or under a ticked parent."""
        t = self
        while t:
            if t.done:
                return True
            t = t.parent
        return False


@dataclass
class Plan:
    tasks: list
    target: str = ""


def parse_duration(text: str):
    """Return (title_without_duration, minutes or None)."""
    m = UNIT_DUR_RE.search(text)
    if m and m.start() > 0:
        d = m.group("d").lower()
        h = re.match(r"(\d+)\s*h\s*(\d+)?", d)
        minutes = int(h.group(1)) * 60 + int(h.group(2) or 0) if h else int(re.match(r"\d+", d).group())
        return text[: m.start()].rstrip(" —–-("), minutes
    m = BARE_DUR_RE.search(text)
    if m and m.start() > 0:
        return text[: m.start()].rstrip(" —–-("), int(m.group("d"))
    return text, None


def parse_minutes(s: str) -> int:
    """'1h05m' / '27m' -> minutes."""
    h = re.match(r"(\d+)h(\d+)m", s)
    return int(h.group(1)) * 60 + int(h.group(2)) if h else int(s.rstrip("m"))


def parse_plan(path: Path) -> Plan:
    plan = Plan(tasks=[])
    stack: list[Task] = []  # open ancestors of the next task line
    for i, line in enumerate(path.read_text(encoding="utf-8").splitlines()):
        if not plan.target and (m := TARGET_RE.match(line)):
            plan.target = m.group(1).strip()
            continue
        m = TASK_RE.match(line)
        if not m:
            continue  # notes and blank lines don't break the nesting
        title, minutes = parse_duration(META_RE.sub("", m.group(3)))
        t = Task(i, title.strip(), minutes, m.group(2).strip() != "", line, indent=len(m.group(1).expandtabs(TAB)))
        while stack and stack[-1].indent >= t.indent:
            stack.pop()
        if stack:
            t.parent = stack[-1]
            t.parent.children.append(t)
        t.path = (t.parent.path if t.parent else ()) + (t.title,)
        stack.append(t)
        plan.tasks.append(t)
    return plan


def parse_tasks(path: Path):
    """Backwards-compatible helper: (tasks, target)."""
    p = parse_plan(path)
    return p.tasks, p.target


def fmt_spent(seconds: float) -> str:
    m = int(round(seconds / 60))
    return f"{m // 60}h{m % 60:02d}m" if m >= 60 else f"{m}m"


def fmt_min(m: int) -> str:
    return f"{m // 60}h{m % 60:02d}" if m >= 60 else f"{m}m"


def _find_task(path: Path, key: str, want: str) -> "Task | None":
    """The task with this key (full path; a plain title for top-level tasks), freshly parsed.
    want='open': the first unticked one, even if it still carries an old log (that log gets
    overwritten); else one ticked by hand without a log. want='logged': latest with ⏱, no ☕."""
    found = None
    for t in parse_plan(path).tasks:
        if t.key != key:
            continue
        if want == "open":
            if not t.done:
                return t
            if "⏱" not in t.raw and found is None:
                found = t
        elif want == "logged" and "⏱" in t.raw and "☕" not in t.raw:
            found = t
    return found


def _rewrite(path: Path, idx: int, fn):
    lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
    line = lines[idx]
    body, nl = line.rstrip("\r\n"), line[len(line.rstrip("\r\n")):]
    lines[idx] = fn(body) + (nl or "")
    path.write_text("".join(lines), encoding="utf-8")


def _log_tags(minutes: float, interruptions: int) -> str:
    ints = f" ⚡{interruptions}" if interruptions else ""
    return f" ⏱ {fmt_spent(minutes * 60)}{ints} ✅ {date.today().isoformat()}"


def _tick(body: str) -> str:
    return re.sub(r"\[ ?\]", "[x]", META_RE.sub("", body), count=1)


def log_done(path: Path, key: str, spent: float, interruptions: int = 0) -> bool:
    """Tick the task (if needed) and write time spent + interruptions + date, replacing any old log
    on that line; then tick parents whose subtasks are now all done. If the task has gained
    subtasks meanwhile, its time is noted (⏱ without ✅) and it stays open for them.
    Returns False if the task's line couldn't be found."""
    t = _find_task(path, key, "open")
    if t is None:
        return False
    if not t.leaf and not t.done:
        _rewrite(path, t.line_no, lambda b: f"{META_RE.sub('', b)} ⏱ {fmt_spent(spent)}")
    else:
        _rewrite(path, t.line_no, lambda b: _tick(b) + _log_tags(spent / 60, interruptions))
    _close_parents(path, t.path[:-1])
    return True


def _own_partial(t: Task) -> tuple[int, int]:
    """Time/interruptions a parent logged for itself before it had subtasks (⏱ without ✅)."""
    if "✅" in t.raw or not (m := LOG_SPENT_RE.search(t.raw)):
        return 0, 0
    n = LOG_INT_RE.search(t.raw)
    return parse_minutes(m.group(1)), int(n.group(1)) if n else 0


def _close_parents(path: Path, parent_path: tuple):
    """Walk up from `parent_path`: tick each parent whose subtasks are all done, with their summed ⏱/⚡."""
    while parent_path:
        key = CRUMB.join(parent_path)
        par = next((t for t in parse_plan(path).tasks if t.key == key and not t.done and t.children), None)
        if not par or not all(c.done for c in par.children):
            return
        mins, ints = _own_partial(par)
        for c in par.children:
            if m := LOG_SPENT_RE.search(c.raw):
                mins += parse_minutes(m.group(1))
            if n := LOG_INT_RE.search(c.raw):
                ints += int(n.group(1))
        _rewrite(path, par.line_no, lambda b: _tick(b) + _log_tags(mins, ints))
        print(f"✔ {par.title} — all subtasks done, {fmt_min(mins)}")
        parent_path = parent_path[:-1]


def log_break(path: Path, key: str, seconds: float):
    """Add '☕ 6m' (the break taken after this task) to its logged line, before the date."""
    t = _find_task(path, key, "logged")
    if t is None:
        return
    tag = f"☕ {fmt_spent(seconds)}"
    _rewrite(path, t.line_no, lambda b: b.replace(" ✅", f" {tag} ✅", 1) if " ✅" in b else f"{b} {tag}")


def summarize(tasks: list, day: str | None = None) -> dict:
    """Planned vs actual for the tasks completed on `day` (default today)."""
    day = day or date.today().isoformat()
    s = dict(count=0, planned=0, actual_timed=0, actual=0, breaks=0, interruptions=0)
    for t in tasks:
        if not t.leaf:
            continue  # a parent's log is the sum of its subtasks: don't count it twice
        d = LOG_DATE_RE.search(t.raw)
        sp = LOG_SPENT_RE.search(t.raw)
        if not (t.done and d and d.group(1) == day and sp):
            continue
        spent = parse_minutes(sp.group(1))
        s["count"] += 1
        s["actual"] += spent
        if t.minutes:
            s["planned"] += t.minutes
            s["actual_timed"] += spent
        if b := LOG_BREAK_RE.search(t.raw):
            s["breaks"] += parse_minutes(b.group(1))
        if n := LOG_INT_RE.search(t.raw):
            s["interruptions"] += int(n.group(1))
    return s


def summary_lines(s: dict) -> list[str]:
    if not s["count"]:
        return ["No tasks done today"]
    diff = s["actual_timed"] - s["planned"]
    sign = "+" if diff >= 0 else "-"
    lines = []
    if s["planned"]:
        lines += [f"Plan {fmt_min(s['planned'])}", f"Real {fmt_min(s['actual_timed'])} {sign}{fmt_min(abs(diff))}"]
    lines += [f"Focus {fmt_min(s['actual'])}", f"Breaks {fmt_min(s['breaks'])}"]
    if s["interruptions"]:
        lines.append(f"Interrupts {s['interruptions']}")
    return lines


def ascii_safe(s: str) -> str:
    """The display fonts are ASCII only: é -> e, — -> -, drop the rest."""
    s = s.replace("—", "-").replace("–", "-").replace("’", "'").replace("“", '"').replace("”", '"')
    s = s.replace("·", "-").replace("…", "...")
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode()
    return re.sub(r"[*_`\[\]]", "", s).strip()  # strip markdown emphasis / wikilink brackets


class Sprint:
    """Modes: start (Focus? with no tasks left), ready, run, pause (current task) · offer, break (break after `break_after`; any button ends it) · done."""

    def __init__(self, path: Path):
        self.path = path
        self.skipped: set[str] = set()   # task keys
        self.anchor = ""                 # key of the last finished/skipped task: "next" is the first open task below it
        self.current: Task | None = None
        self.mode = "ready"
        self.worked = 0.0            # seconds counted before the current run segment
        self.run_since: float | None = None
        self.extra = 0               # +/- adjustments, seconds
        self.interruptions = 0
        self.break_after = ""        # title of the task the break follows
        self.ready_since = time.time()
        self.started_any = False
        self.mtime = 0.0
        self.plan = Plan(tasks=[])
        self.pending: list[Task] = []
        self.dirty = True
        self.alerted = False
        self.reload()

    @property
    def target(self) -> str:
        return self.plan.target

    # ---- file -------------------------------------------------------------
    def reload(self):
        self.mtime = self.path.stat().st_mtime
        self.plan = parse_plan(self.path)
        tasks = self.plan.tasks
        # Only tasks without subtasks are run; parents are context.
        self.pending = [t for t in tasks if t.leaf and not t.closed() and t.key not in self.skipped]
        cur = self.current
        # The current task stays current even if it gains subtasks meanwhile (it finishes as is).
        match = cur and next((t for t in tasks if t.key == cur.key and not t.closed()), None)
        if match:
            self.current = match  # keep timer; pick up duration edits
        else:
            if cur and self.mode in ("run", "pause"):
                # ticked (or removed) in the file while running -> log it
                if any(t.key == cur.key and t.done for t in tasks):
                    log_done(self.path, cur.key, self.spent(), self.interruptions)
                    print(f"✔ {cur.title} — {fmt_spent(self.spent())} (ticked in file)")
                    self.mtime = self.path.stat().st_mtime
                    self.plan = parse_plan(self.path)
                self.anchor = cur.key
            self.load(self.first_after(self.anchor))
        self.dirty = True

    def first_after(self, key: str) -> "Task | None":
        """First open task below the task `key` in the file, wrapping to the top."""
        if not self.pending:
            return None
        line = next((t.line_no for t in self.plan.tasks if t.key == key), -1)
        return next((t for t in self.pending if t.line_no > line), self.pending[0])

    # ---- state ------------------------------------------------------------
    def _reset_clock(self):
        self.worked, self.run_since, self.extra = 0.0, None, 0
        self.alerted = False

    def load(self, task: Task | None):
        self.current = task
        back_to_start = not task and self.mode == "start"
        self.mode = "ready" if task else ("start" if back_to_start else "done")
        self._reset_clock()
        self.interruptions = 0
        self.ready_since = time.time()
        self.dirty = True
        if back_to_start:
            pass
        elif task:
            dur = f"{task.minutes} min" if task.minutes else "untimed"
            print(f"\n• Ready: {task.title} ({dur}) — Start/Pause to start")
        else:
            print("\n✔ All tasks done.")
            for line in summary_lines(summarize(self.plan.tasks)):
                print(f"  {line}")

    def counting(self) -> bool:
        return self.mode in ("run", "break")

    def spent(self) -> float:
        return self.worked + (time.time() - self.run_since if self.run_since else 0)

    def total(self) -> int:
        if self.mode in ("offer", "break"):
            return 0  # breaks have no set length: they last until you press a button
        if not self.current or not self.current.minutes:
            return 0
        return max(60, self.current.minutes * 60 + self.extra)

    def remaining(self) -> int:
        return int(round(self.total() - self.spent())) if self.total() else 0

    def idle(self) -> bool:
        return self.mode == "ready" and (not self.started_any or time.time() - self.ready_since >= IDLE_AFTER)

    def _start_clock(self, mode):
        self.mode, self.run_since = mode, time.time()
        self.started_any = True
        self.dirty = True

    def _end_break(self):
        log_break(self.path, self.break_after, self.spent())
        print(f"☕ break done — {fmt_spent(self.spent())}")
        self.break_after = ""
        self.load(self.current)
        self.mtime = self.path.stat().st_mtime
        if self.current:  # back from the break: the next task starts right away
            self._start_clock("run")
            print(f"▶ {self.current.title}")

    def go(self):
        """Complete: the running/paused task, the break prompt (skip it) or the break (end it)."""
        if self.mode == "ready" and self.current:
            self._start_clock("run")
            print(f"▶ {self.current.title}")
        elif self.mode in ("run", "pause") and self.current:
            spent, title, key = self.spent(), self.current.title, self.current.key
            if not log_done(self.path, key, spent, self.interruptions):
                self.skipped.add(key)  # never let a task we couldn't tick come back in a loop
                print(f"⚠ couldn't find '{title}' in the file to tick it; moving on")
            print(f"✔ {title} — {fmt_spent(spent)}" + (f" ⚡{self.interruptions}" if self.interruptions else ""))
            self.current, self.anchor = None, key
            self.reload()
            nxt = self.current
            if nxt:  # breaks come from this prompt, never from tasks in the file
                self.mode, self.break_after = "offer", key
                self._reset_clock()
                print("☕ Break? Start/Pause = break · Complete = skip")
        elif self.mode == "offer":
            print("↷ break skipped")
            self.break_after = ""
            self.load(self.current)
            if self.current:  # no break: the next task starts right away
                self._start_clock("run")
                print(f"▶ {self.current.title}")
        elif self.mode in ("break",):
            self._end_break()
        elif self.mode in ("done", "start"):
            # Back to the start screen: a fresh "Focus?", picking up any tasks added since.
            self.skipped.clear()
            self.started_any = False
            self.current, self.mode, self.anchor = None, "start", ""
            self.reload()
            print("◷ Focus? — back to the start screen")
        self.dirty = True

    def skip(self):
        if self.mode in ("offer", "break"):
            return self.go()
        if self.current:
            print(f"↷ skipped {self.current.title}")
            self.skipped.add(self.current.key)
            self.current, self.anchor = None, self.current.key
            self.reload()

    def toggle_pause(self):
        if self.mode == "start":  # no tasks left: nothing to start
            return
        if self.mode == "break":  # a break can't be paused: any button ends it
            return self.go()
        if self.mode == "ready":
            return self.go()  # Start/Pause on a waiting task starts it
        if self.mode == "offer":
            self._start_clock("break")
            print("☕ break started")
            return
        if self.mode == "run":
            self.worked, self.run_since, self.mode = self.spent(), None, "pause"
            print("⏸ paused")
        elif self.mode == "pause":
            self._start_clock("run")
            print("▶ resumed")
        self.dirty = True

    def interrupt(self):
        if self.mode == "break":  # any button ends a break
            return self.go()
        if self.mode in ("run", "pause"):
            self.interruptions += 1
            print(f"⚡ interruption #{self.interruptions}")
            self.dirty = True

    def adjust(self, minutes: int):
        if self.total() and self.mode != "offer":
            self.extra += minutes * 60
            self.alerted = self.alerted and self.remaining() <= 0
            print(f"⏱ {max(0, self.remaining()) // 60} min left")
            self.dirty = True

    def next_task(self) -> Task | None:
        if not self.current:
            return None
        if self.mode in ("offer", "break"):
            return self.current  # the task waiting after the break
        nxt = self.first_after(self.current.key)
        return nxt if nxt and nxt.key != self.current.key else None

    # ---- what the display shows ------------------------------------------
    def view(self) -> dict:
        """Everything the display draws. mode tells the device whether to count down."""
        v = dict(header="", right="", body="", remaining=0, total=0, mode="static", footer="", lines=[], ints=0)
        cur, nxt = self.current, self.next_task()
        nxt_s = ((f"{nxt.title} ({nxt.minutes}m)" if nxt.minutes else nxt.title) if nxt else "")
        pos = ""
        if cur:
            idx = next((i for i, t in enumerate(self.pending) if t.key == cur.key), 0)
            pos = f"{idx + 1}/{len(self.pending)}"

        if self.mode == "done":
            s = summarize(self.plan.tasks)
            v.update(header="DONE", right=f"{s['count']} tasks" if s["count"] else "",
                     lines=summary_lines(s), footer=self.plan.target)
        elif self.mode == "start" or self.idle():
            # Just a big "Focus?": no task text.
            v.update(header="READY", right=pos, body="Focus?", mode="start")
        elif self.mode == "offer":
            # Simple prompt: start the break, or skip straight to the next task.
            v.update(header="BREAK", right=pos, body="Break?", remaining=self.total(), total=self.total(),
                     mode="prompt")
        elif self.mode == "pause":
            # Paused task: just a big "Paused..", like the Focus? screen.
            v.update(header="PAUSED", right=pos, body="Paused..", mode="word", ints=self.interruptions)
        elif self.mode == "break":
            # Big "Break!" with a count-up of the break so far (the same time that's logged as ☕).
            v.update(header="BREAK", right=pos, body="Break!", remaining=int(self.spent()), mode="up")
        else:  # ready / run
            header = {"ready": "READY", "run": "NOW"}[self.mode]
            if depth := len(cur.path) - 1:
                header += " " + ">" * depth  # one ">" per nesting level; parent names would clutter the display
            footer = "Press Start/Pause" if self.mode == "ready" else (f"Next: {nxt_s}" if nxt_s else "Next: -")
            v.update(header=header, right=pos, body=cur.title, remaining=self.remaining(), total=self.total(),
                     mode="run" if self.mode == "run" else "static", footer=footer, ints=self.interruptions)
        return v

    def payload(self) -> bytes:
        v = self.view()
        fields = [v["header"], v["right"], v["body"], str(v["remaining"]), str(v["total"]), v["mode"],
                  v["footer"], LINE_SEP.join(ascii_safe(x) for x in v["lines"]), str(v["ints"])]
        return SEP.join(f if i == 7 else ascii_safe(f) for i, f in enumerate(fields)).encode()[:500]


def placeholder_payload(text: str) -> bytes:
    return SEP.join(["FOCUSINK", "", text, "0", "0", "static", "", "", "0"]).encode()


async def find_device(timeout=10.0):
    print(f"Scanning for {DEVICE_NAME}…")
    return await BleakScanner.find_device_by_filter(
        lambda d, adv: d.name == DEVICE_NAME or SERVICE_UUID in (adv.service_uuids or []),
        timeout=timeout,
    )


async def stdin_commands(queue: asyncio.Queue):
    loop = asyncio.get_running_loop()
    reader = asyncio.StreamReader()
    await loop.connect_read_pipe(lambda: asyncio.StreamReaderProtocol(reader), sys.stdin)
    while line := await reader.readline():
        await queue.put(line.decode().strip().lower() or "go")
    await queue.put("q")


# device event -> command. Case: upper ✓ = BOOT = Complete, lower ▶‖ = PWR = Start/Pause.
BUTTONS = {"pwr": "p", "pwr_double": "i", "boot": "go", "boot_long": "s"}
BUTTON_NAMES = {"pwr": "Start/Pause", "pwr_double": "Start/Pause ×2 (interruption)",
                "boot": "Complete", "boot_long": "Complete (held: skip)"}


class Link:
    """Runs a Sprint against the display: BLE (re)connect, commands, file watching, resyncs.

    Shared by the CLI and the menu bar app. Commands: "go" (complete), "p" (start/pause),
    "i" (interruption), "s" (skip), "+" / "-" (5 min), ("open", path) (switch task file), "q" (quit).
    """

    def __init__(self, sprint: "Sprint | None", resync: int = 60, log=print, on_alert=None):
        self.sprint = sprint
        self.resync = resync
        self.log = log
        self.on_alert = on_alert or (lambda msg: log(f"\a⏰ {msg}"))
        self.queue: asyncio.Queue = asyncio.Queue()
        self.client: BleakClient | None = None
        self.status = "starting"  # scanning | connected | not found | asleep
        self.asleep = False       # the display said it's going to deep sleep; a button press wakes it
        self._lost = asyncio.Event()
        self.last_sent = 0.0
        self.last_view = None

    def _on_event(self, _, data: bytearray):
        ev = data.decode(errors="ignore").strip()
        if ev == "sleep":
            self.asleep = True
            self.log("Display went to sleep (battery); press a button on it to wake it")
            return
        if cmd := BUTTONS.get(ev):
            self.log(f"  [{BUTTON_NAMES[ev]}]")
            self.queue.put_nowait(cmd)

    async def _connect_forever(self):
        while True:
            if self.client is None or not self.client.is_connected:
                self.status = "asleep" if self.asleep else "scanning"
                dev = await find_device()
                if not dev:
                    self.status = "asleep" if self.asleep else "not found"
                    if not self.asleep:
                        self.log("Display not found; retrying (timer keeps running).")
                    await asyncio.sleep(1)
                    continue
                client = BleakClient(dev, disconnected_callback=lambda _: self._lost.set())
                try:
                    await client.connect()
                    await client.start_notify(EVENT_UUID, self._on_event)
                    self.client, self.status = client, "connected"
                    try:  # the display reports why it last (re)started, e.g. "reset:watchdog"
                        why = (await client.read_gatt_char(EVENT_UUID)).decode(errors="ignore")
                    except Exception:  # noqa: BLE001
                        why = ""
                    why = re.match(r"[\w ()-]*", why.split(":", 1)[1]).group().strip() if why.startswith("reset:") else ""
                    self.asleep = False
                    self._lost.clear()
                    self.log(f"Connected to {DEVICE_NAME}" + (f" (last start: {why})" if why else ""))
                    self.last_view = None
                except Exception as e:  # noqa: BLE001
                    self.log(f"Connect failed: {e}")
                    try:
                        await client.disconnect()
                    except Exception:  # noqa: BLE001
                        pass
                    await asyncio.sleep(1)
                continue
            # connected: wait until the link drops (or check again after 5 s), then rescan right away
            try:
                await asyncio.wait_for(self._lost.wait(), timeout=5)
            except asyncio.TimeoutError:
                pass

    def _handle(self, cmd) -> bool:
        """Apply one command; returns False to quit."""
        sp = self.sprint
        if isinstance(cmd, tuple) and cmd[0] == "open":
            if sp and sp.mode in ("run", "pause"):
                self.log(f"(not logged: {sp.current.title}, {fmt_spent(sp.spent())} so far)")
            self.sprint = Sprint(Path(cmd[1]))
            self.log(f"Tasks from {cmd[1]}")
            return True
        if cmd == "q":
            if sp and sp.mode in ("run", "pause"):
                self.log(f"(not logged: {sp.current.title}, {fmt_spent(sp.spent())} so far)")
            return False
        if not sp:
            return True
        if cmd in ("go", "n", "d", "done", "start"):
            sp.go()
        elif cmd == "s":
            sp.skip()
        elif cmd == "p":
            sp.toggle_pause()
        elif cmd == "i":
            sp.interrupt()
        elif cmd in ("+", "-"):
            sp.adjust(5 if cmd == "+" else -5)
        return True

    def _payload(self) -> bytes:
        if self.sprint:
            return self.sprint.payload()
        return placeholder_payload("Choose a task file")

    async def run(self):
        connector = asyncio.create_task(self._connect_forever())
        try:
            while True:
                sp = self.sprint
                if sp:
                    try:
                        if sp.path.stat().st_mtime != sp.mtime:
                            sp.reload()
                    except FileNotFoundError:
                        pass

                while not self.queue.empty():
                    if not self._handle(self.queue.get_nowait()):
                        return
                sp = self.sprint

                if sp and sp.counting() and sp.total() and sp.remaining() <= 0 and not sp.alerted:
                    sp.alerted = True
                    sp.dirty = True
                    self.on_alert(f"Time's up: {sp.current.title} — press Complete when done")

                payload = self._payload()
                view_key = payload if not sp or not sp.counting() else None  # idle switch etc.
                changed = (sp.dirty if sp else False) or (view_key is not None and view_key != self.last_view)
                c = self.client
                if c and c.is_connected and (changed or time.time() - self.last_sent > self.resync):
                    try:
                        await c.write_gatt_char(STATE_UUID, payload, response=True)
                        self.last_sent, self.last_view = time.time(), view_key
                        if sp:
                            sp.dirty = False
                    except Exception as e:  # noqa: BLE001
                        self.log(f"Send failed: {e}")

                try:
                    cmd = await asyncio.wait_for(self.queue.get(), timeout=1.0)
                    self.queue.put_nowait(cmd)  # handle on next pass
                except asyncio.TimeoutError:
                    pass
        finally:
            connector.cancel()
            if self.client and self.client.is_connected:
                await self.client.disconnect()


async def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    default_file = os.environ.get("FOCUS_FILE") or str(Path(__file__).resolve().with_name("sprint.md"))
    ap.add_argument("file", nargs="?", default=default_file,
                    help="task file (default: $FOCUS_FILE, else sprint.md next to this script)")
    ap.add_argument("--resync", type=int, default=60, help="seconds between periodic resyncs")
    args = ap.parse_args()

    path = Path(args.file).expanduser().resolve()
    print(f"Tasks from {path}")
    sprint = Sprint(path)
    if sprint.target:
        print(f"Target: {sprint.target}")
    print("Start/Pause (p) · Complete (Enter) · interruption (i) · skip (s) · +/- = ±5 min · q = quit")

    link = Link(sprint, resync=args.resync)
    asyncio.create_task(stdin_commands(link.queue))
    await link.run()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass

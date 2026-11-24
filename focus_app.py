"""FocusInk menu bar app.

Runs the same sprint logic as focus.py (via focus.Link) without a terminal.
The menu bar shows the current task and time left; the menu has Start/Pause,
Complete, Skip, ±5 min, Choose Task File…, Open at Login. The task file is
picked once and remembered in ~/Library/Application Support/FocusInk/config.json.
"""

import asyncio
import json
import subprocess
import sys
import threading
from collections import deque
from datetime import datetime
from pathlib import Path

import rumps
from AppKit import NSApp, NSOpenPanel, NSSound

from focus import Link, Sprint, fmt_min, summarize

APP_NAME = "FocusInk"
SUPPORT = Path.home() / "Library" / "Application Support" / APP_NAME
CONFIG = SUPPORT / "config.json"
LOG = Path.home() / "Library" / "Logs" / f"{APP_NAME}.log"
TITLE_MAX = 28


def load_config() -> dict:
    try:
        return json.loads(CONFIG.read_text())
    except (OSError, ValueError):
        return {}


def save_config(cfg: dict):
    SUPPORT.mkdir(parents=True, exist_ok=True)
    CONFIG.write_text(json.dumps(cfg, indent=2))


def short(s: str, n: int) -> str:
    return s if len(s) <= n else s[: n - 1] + "…"


class TimestampedLog:
    """File-like: prefixes every line written (prints from the app and focus.py) with the time."""

    def __init__(self, path: Path):
        self.f = open(path, "a", buffering=1, encoding="utf-8")
        self.at_line_start = True

    def write(self, text: str):
        out = []
        for chunk in text.splitlines(keepends=True):
            if self.at_line_start and chunk.strip():
                out.append(datetime.now().strftime("%H:%M:%S "))
            out.append(chunk)
            self.at_line_start = chunk.endswith("\n")
        self.f.write("".join(out))
        return len(text)

    def flush(self):
        self.f.flush()


def login_service():
    try:
        from ServiceManagement import SMAppService
        return SMAppService.mainAppService()
    except Exception:  # noqa: BLE001  (not available / not running as a bundle)
        return None


class FocusApp(rumps.App):
    def __init__(self):
        super().__init__(APP_NAME, title="◷", quit_button=None)
        self.task_item = rumps.MenuItem("No task file")
        self.next_item = rumps.MenuItem("")
        self.today_item = rumps.MenuItem("")
        self.file_item = rumps.MenuItem("")
        self.status_item = rumps.MenuItem("Display: starting…")
        self.login_item = rumps.MenuItem("Open at Login", callback=self.toggle_login)
        self.presses: deque[str] = deque(maxlen=12)  # newest last
        self.presses_shown: list[str] = []
        self.presses_menu = rumps.MenuItem("Button Presses")
        self.presses_menu.add(rumps.MenuItem("None yet"))
        self.menu = [
            self.task_item,
            self.next_item,
            self.today_item,
            None,
            rumps.MenuItem("Start / Pause", callback=lambda _: self.send("p"), key="p"),
            rumps.MenuItem("Complete", callback=lambda _: self.send("go"), key="d"),
            rumps.MenuItem("Interruption", callback=lambda _: self.send("i"), key="i"),
            rumps.MenuItem("Skip", callback=lambda _: self.send("s")),
            rumps.MenuItem("+5 min", callback=lambda _: self.send("+")),
            rumps.MenuItem("−5 min", callback=lambda _: self.send("-")),
            None,
            self.file_item,
            rumps.MenuItem("Choose Task File…", callback=self.choose_file),
            rumps.MenuItem("Open Task File", callback=self.open_file),
            None,
            self.status_item,
            self.presses_menu,
            rumps.MenuItem("Show Live Log", callback=self.open_log),
            self.login_item,
            rumps.MenuItem("Quit FocusInk", callback=self.quit),
        ]
        self.alerts: list[str] = []
        self.loop: asyncio.AbstractEventLoop | None = None

        path = load_config().get("file")
        sprint = None
        if path and Path(path).exists():
            sprint = Sprint(Path(path))
        self.link = Link(sprint, log=self.log, on_alert=self.alerts.append)
        threading.Thread(target=self._run_link, daemon=True).start()

        rumps.Timer(self.refresh, 1).start()
        if not sprint:
            self._first_pick = rumps.Timer(self._pick_on_start, 1)
            self._first_pick.start()

    # ---- background asyncio thread ---------------------------------------
    def _run_link(self):
        self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)
        try:
            self.loop.run_until_complete(self.link.run())
        except Exception as e:  # noqa: BLE001
            self.log(f"link stopped: {e!r}")

    def send(self, cmd):
        if self.loop:
            self.loop.call_soon_threadsafe(self.link.queue.put_nowait, cmd)
            rumps.Timer(lambda t: (t.stop(), self.refresh(None)), 0.3).start()

    def log(self, msg: str):
        print(msg, flush=True)
        if msg.startswith("  ["):  # button press reported by the display
            self.presses.append(f"{datetime.now():%H:%M:%S}  {msg.strip()[1:-1]}")

    def open_log(self, _):
        """Live log in a Terminal window: `tail -f` keeps the newest line at the bottom."""
        SUPPORT.mkdir(parents=True, exist_ok=True)
        script = SUPPORT / "FocusInk Log.command"
        script.write_text(f'#!/bin/sh\nclear\nexec tail -n 100 -f "{LOG}"\n')
        script.chmod(0o755)
        subprocess.run(["open", str(script)], check=False)

    def _refresh_presses(self):
        current = list(self.presses)
        if current == self.presses_shown:
            return
        self.presses_shown = current
        self.presses_menu.clear()
        for line in reversed(current):  # newest first
            self.presses_menu.add(rumps.MenuItem(line))

    # ---- file selection ----------------------------------------------------
    def _pick_on_start(self, timer):
        timer.stop()
        self.choose_file(None)

    def choose_file(self, _):
        panel = NSOpenPanel.openPanel()
        panel.setTitle_("Choose your Focus Sprint task file")
        panel.setAllowedFileTypes_(["md", "markdown", "txt"])
        panel.setCanChooseDirectories_(False)
        panel.setAllowsMultipleSelection_(False)
        NSApp.activateIgnoringOtherApps_(True)
        if panel.runModal() == 1:  # NSModalResponseOK
            path = str(panel.URLs()[0].path())
            save_config({**load_config(), "file": path})
            self.send(("open", path))

    def open_file(self, _):
        sp = self.link.sprint
        if sp:
            subprocess.run(["open", str(sp.path)], check=False)
        else:
            self.choose_file(None)

    # ---- login item --------------------------------------------------------
    def toggle_login(self, item):
        svc = login_service()
        if not svc:
            rumps.alert("Open at Login works only from FocusInk.app.")
            return
        try:
            if svc.status() == 1:  # enabled
                svc.unregisterAndReturnError_(None)
            else:
                svc.registerAndReturnError_(None)
        except Exception as e:  # noqa: BLE001
            rumps.alert(f"Could not change login item: {e}")
        self.refresh(None)

    # ---- UI refresh (main thread) ----------------------------------------
    def refresh(self, _):
        while self.alerts:
            msg = self.alerts.pop(0)
            NSSound.soundNamed_("Glass").play()
            rumps.notification(APP_NAME, "", msg)

        self._refresh_presses()
        sp = self.link.sprint
        status = {"connected": "Display: connected", "scanning": "Display: searching…",
                  "not found": "Display: not found"}.get(self.link.status, "Display: starting…")
        self.status_item.title = status
        svc = login_service()
        self.login_item.state = 1 if svc and svc.status() == 1 else 0

        if not sp:
            self.title = "◷ Choose task file"
            self.task_item.title = "No task file"
            self.next_item.title = self.today_item.title = self.file_item.title = ""
            return

        self.file_item.title = f"File: {sp.path.name}"
        s = summarize(sp.plan.tasks)
        self.today_item.title = (
            f"Today: {s['count']} done · focus {fmt_min(s['actual'])} · breaks {fmt_min(s['breaks'])}"
            + (f" · plan {fmt_min(s['planned'])} vs real {fmt_min(s['actual_timed'])}" if s["planned"] else "")
            + (f" · ⚡{s['interruptions']}" if s["interruptions"] else "")
        ) if s["count"] else "Today: nothing completed yet"
        link_mark = "" if self.link.status == "connected" else " ⚠︎"

        if sp.mode == "start":
            self.title = "◷ Focus?" + link_mark
            self.task_item.title = "No open tasks — add some to the task file"
            self.next_item.title = ""
            return

        if sp.mode == "done":
            self.title = "✓ Done" + link_mark
            self.task_item.title = "All tasks done"
            self.next_item.title = ""
            return

        cur = sp.current
        rem = sp.remaining()
        t = (f"{(rem + 59) // 60}m" if rem > 0 else f"+{-rem // 60}m") if sp.total() else ""
        if sp.mode in ("offer", "break"):
            icon = {"offer": "☕?", "break": "☕"}[sp.mode]
            self.title = (f"{icon} Break! {fmt_min(int(sp.spent() // 60))}" if sp.mode == "break" else icon) + link_mark
            self.task_item.title = {"offer": "Break? (Start/Pause = break, Complete = skip)",
                                    "break": "On a break (any button ends it)"}[sp.mode]
            self.next_item.title = f"Next: {cur.key}" if cur else ""
            return

        if sp.mode in ("run", "pause") and not sp.total():
            t = fmt_min(int(sp.spent() // 60))
        if sp.mode == "ready":
            t = ""
        icon = {"ready": "●", "run": "▶", "pause": "⏸"}.get(sp.mode, "")
        ints = f" ⚡{sp.interruptions}" if sp.interruptions else ""
        self.title = f"{icon} {t + ' · ' if t else ''}{short(cur.title, TITLE_MAX)}{ints}" + link_mark

        mode = {"ready": "Ready", "run": "Now", "pause": "Paused"}.get(sp.mode, "")
        dur = f" ({cur.minutes} min)" if cur.minutes else ""
        self.task_item.title = f"{mode}: {cur.key}{dur}"  # full path, e.g. "Ship › Firmware › Tests"
        nxt = sp.next_task()
        self.next_item.title = f"Next: {nxt.key}" if nxt else "Next: —"

    def quit(self, _):
        if self.loop:
            self.loop.call_soon_threadsafe(self.link.queue.put_nowait, "q")
        rumps.Timer(lambda t: rumps.quit_application(), 0.8).start()


def main():
    LOG.parent.mkdir(parents=True, exist_ok=True)
    sys.stdout = sys.stderr = TimestampedLog(LOG)
    FocusApp().run()


if __name__ == "__main__":
    main()

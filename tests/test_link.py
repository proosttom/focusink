"""Offline checks of how the Mac side handles events from the display (no Bluetooth)."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import focus  # noqa: E402


def test_sleep_event_marks_display_asleep():
    link = focus.Link(None, log=lambda *_: None)
    link._on_event(None, bytearray(b"sleep"))
    assert link.asleep and link.queue.empty()


def test_button_events_become_commands():
    link = focus.Link(None, log=lambda *_: None)
    for ev, cmd in [("pwr", "p"), ("pwr_double", "i"), ("boot", "go"), ("boot_long", "s")]:
        link._on_event(None, bytearray(ev.encode()))
        assert link.queue.get_nowait() == cmd


def test_unknown_events_are_ignored():
    link = focus.Link(None, log=lambda *_: None)
    link._on_event(None, bytearray(b"reset:watchdog"))
    assert link.queue.empty() and not link.asleep

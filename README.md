# FocusInk

A desk display for focus sprints: pick a target, split it into timed subtasks, work through them one at a time with deliberate breaks in between. A Waveshare ESP32-S3-ePaper-1.54 shows the current task, the time left and the next task. A Mac reads the tasks from a plain Markdown file and sends them over Bluetooth LE; the device buttons start, pause and complete tasks, and the time spent is logged back into the file.

```
┌──────────────────────┐   ┌──────────────────────┐
│█NOW >████████████2/4█│   │█BREAK████████████2/4█│
│                      │   │                      │
│ Count-up timer       │   │       Break!         │
│                      │   │                      │
│        18 min        │   │        4 min         │
│ [██████░░░░░░░░░░░░] │   │                      │
│ Next: Tests          │   │                      │
└──────────────────────┘   └──────────────────────┘
  a task: one ">" per        a break counts up until
  nesting level              you press a button
```

## Task file

```markdown
Target: Ship the BLE display

- [ ] Firmware
  - [ ] Count-up timer — 25m
  - [ ] Tests
- [ ] Mac app — 1h30m
- [x] Flash board — 10m ⏱ 12m ⚡2 ☕ 6m ✅ 2026-11-24
```

- **Durations:** `25m`, `25 min`, `2h`, `1h30m`, `1h 30m`, `(20m)`, or a bare number after a dash or in brackets (`— 20`). No duration: just the task name, no timer.
- **Nesting:** indent with tabs or spaces, any depth.
  - Only tasks without subtasks run. The header shows one `>` per level; the menu bar shows the full path.
  - A parent ticks itself when all its subtasks are done, with their summed time. Ticking a parent by hand drops its open subtasks.
- **Adding tasks:** works any time. The next task is the first open one below the last finished task, wrapping to the top.
- **Log:** completing a task ticks it and writes `⏱` time spent (without pauses), `⚡` interruptions, `☕` the break after it, and `✅` the date. Any old log on that line is replaced. Skipped tasks aren't touched.
- **Test sprint:** every `./build_app.sh` resets `sprint.md` from `sprint.example.md`. Keep real tasks in another file.

## Flow

1. **Focus?** at startup, or after a task waited 2 min as READY. Start/Pause starts the task.
2. **NOW**: timer and progress bar, or just the name if untimed. After the time is up, a black `+N min` overtime bar. **Paused..** while paused.
3. **Break?** after you complete a task. Start/Pause starts the break; Complete skips it and starts the next task.
4. **Break!** with a timer counting up. It lasts as long as you want and can't be paused: any button ends it and starts the next task.
5. **DONE** after the last task (no break offered): today's Plan vs Real, Focus, Breaks and Interrupts. Complete goes back to **Focus?** and picks up newly added tasks.

| Button | Does |
|---|---|
| **Start/Pause** (lower, ▶‖) | start / pause / resume · start the break |
| double-press **Start/Pause** | count an interruption |
| **Complete** (upper, ✓) | complete the task · skip the break |
| hold **Complete** 1 s | skip the task (no tick, no log) |

During a break, any button ends it. Ticking the running task in any editor also completes it.

## Menu bar app

`~/Applications/FocusInk.app`: no Dock icon, no terminal.

- **First launch:** allow Bluetooth, then pick your task file. It's remembered in `~/Library/Application Support/FocusInk/config.json`.
- **Title:** e.g. `▶ 18m · Write firmware`. `●` ready, `⏸` paused, `☕` break, `⚠︎` display not connected.
- **Menu:** Start / Pause (⌘P), Complete (⌘D), Interruption (⌘I), Skip, ±5 min, Today's totals, Choose / Open Task File, Button Presses, Show Live Log, Open at Login.
- **Time's up** on a timed task: a sound and a notification.
- **Log:** `~/Library/Logs/FocusInk.log`.

Rebuild after editing `focus.py` or `focus_app.py` (py2app, no Xcode needed):

```bash
./build_app.sh
```

The icon is drawn from the case mesh by `icon/make_icon.py`. Only one program can be connected to the display, so quit the app before using the `focus` command.

## Terminal

```bash
./install.sh                # once: .venv + `focus` command in ~/.local/bin (PYTHON=... to pick a Python 3.10+)
cp sprint.example.md sprint.md   # a first task file to try
focus                       # $FOCUS_FILE, else sprint.md next to the script
focus ~/path/to/today.md
```

Keys: `p` start/pause, Enter complete, `i` interruption, `s` skip, `+`/`-` ±5 min, `q` quit (the running task isn't logged).

## Case

A 3D-printable case (FreeCAD script, STLs, slicer project) is in [`case/`](case/README.md).

## Tests

Offline, no device needed:

```bash
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m pytest tests
```

## Firmware

```bash
arduino-cli compile --upload -p /dev/cu.usbmodem* --fqbn esp32:esp32:esp32s3:CDCOnBoot=cdc,PartitionScheme=huge_app firmware/focus_display
```

- arduino-esp32 4.x core (`BLE.h` API), plus `GxEPD2` and `Adafruit GFX Library`.
- Pins from Waveshare's `user_config.h`: EPD DC 10, CS 11, SCK 12, MOSI 13, RST 9, BUSY 8, power 6; BOOT 0, PWR 18. Holding PWR is left alone: it's Waveshare's battery power-off.
- The device counts on its own and redraws once a minute; the Mac resyncs every 60 s. A `*` in the header means the Bluetooth link is down.

## Battery

- A battery icon shows when running without USB.
- On battery it deep-sleeps after 15 min without activity while no timer runs, or after 5 min without the Mac. Either button wakes it. On USB it never sleeps.

# OBS Wayland Workarounds — OBS Python Scripts

Fixes 2 Wayland issues (`libobs/obs-nix-wayland.c:261` `return false` + `libuiohook: Wayland is not supported`):
- **Hotkeys only when focused** — now all Settings → Hotkeys work unfocused
- **Input Overlay blank when unfocused** — now shows globally, even with Wayland apps focused

Both scripts run **inside OBS** (Tools → Scripts), no external terminal. They read `/dev/input/event*` via `evdev` (kernel, bypasses Wayland).

## Files

| Script | Solves | How |
|--------|--------|-----|
| `wayland-hotkeys.py` | **All** hotkeys in Settings → Hotkeys | `evdev` → `obs_hotkey_inject_event` (`libobs/obs-hotkey.c:1096`) for every binding |
| `wayland-input-overlay.py` | Overlay | `evdev` → **Text Source** + HTTP `http://127.0.0.1:16898/state` for Browser Source |
| `input-history-wayland.html` | Browser Source design (copy of your `input-history-windows.html`) | Polls `http://127.0.0.1:16898/state` — same `slideIn`/`mouse-wrapper` styling, separated from X11 version |

Original `input-history-windows.html` (X11, `ws://16899`) stays untouched in `obs-input-overlay-design/` — use it on X11 or when OBS focused. The Wayland copy is `obs-input-overlay-design/input-history-wayland.html` (and copied here).

## Install (once)

```bash
sudo apt install python3-evdev
sudo usermod -aG input $USER
# REBOOT required (groups only apply on new session)
groups | grep input
```

## OBS Setup

**Python:** Tools → Scripts → Python Settings → `/usr/bin/python3` (3.13)

**Hotkeys:**
1. `+` → `wayland-hotkeys.py` → check `Enabled`, optionally `Debug logging`
2. Set hotkeys normally in **Settings → Hotkeys** (e.g. `F9` → Start Recording) — the script **injects all** of them, no per-hotkey config needed.
3. `List hotkeys to log` button → Help → Logs → verifies injection (`inject OBS_KEY_F9 pressed=True`).

**Overlay — pick one:**

*a) Text Source (simplest):*
1. Create Text Source `Overlay Text` (Monospace 48, outline)
2. `+` → `wayland-input-overlay.py` → `Text Source: Overlay Text`, `History Lines: 6`
3. Optional: `Enable Browser Source HTTP` → also serves `http://127.0.0.1:16898/state`

*b) Browser Source (your design, separated):*
1. Add `wayland-input-overlay.py` as above (enable `Enable Browser Source HTTP`, port `16898`)
2. Add Browser Source → Local File → `/home/pain/NetBeansProjects/obs-input-overlay-design/input-history-wayland.html` (or the copy in `obs-wayland-workarounds/`) — Width 1920 Height 200, FPS 30
3. Keep original `input-history-windows.html` source disabled for Wayland testing — both implementations stay separated.

Logs: Help → Logs → `[wayland-hotkeys]` / `[wayland-overlay]`
- `No /dev/input readable` → forgot reboot
- `watching /dev/input/event3: Logitech ...` → working
- `HTTP state at http://127.0.0.1:16898/state` → browser source ready

Quick test without reboot: close OBS, `sudo obs` (root reads evdev) — not for daily use.

## Why not `run-obs-xwayland.sh`?

It did switch to `Using EGL/X11` (`frontend/OBSApp.cpp:1201`) but `xcb_query_keymap` on XWayland only sees X11 windows; focusing Terminal/Firefox (Wayland-native) → still fails. These scripts work for all windows.

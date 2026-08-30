import obspython as obs
import os
import glob
import threading
import time
import json
from http.server import BaseHTTPRequestHandler, HTTPServer

# Requires: python3-evdev, user in input group
# Fixes: input-overlay's libuiohook says "Wayland is not supported" — this reads evdev directly
#        and updates a Text Source + serves HTTP for Browser Source (input-history-wayland.html).

try:
    import evdev
    from evdev import ecodes
    HAS_EVDEV = True
except ImportError:
    HAS_EVDEV = False

# Config defaults
text_source_name = ""
enabled = True
history_max = 10
show_mouse = True
timeout_ms = 3000
enable_browser = True
browser_port = 16898

# Runtime
pressed = set()
pressed_lock = threading.Lock()
history = []  # list of (timestamp, combo_str) for Text Source
history_lock = threading.Lock()
# For Browser Source (input-history-wayland.html) — raw events like libuiohook
event_queue = []  # list of dicts {event_type, keycode, ...}
event_lock = threading.Lock()
# State-based dedup across devices (Logitech multi-interface)
logically_pressed = set()  # evdev keycodes currently pressed (any device)
logical_lock = threading.Lock()
threads = []  # list of (thread, stop_event) — per-thread events so reloads don't stack
http_server = None
http_thread = None

# Map ecodes to display names (AZERTY-aware like your input-history-windows.html)
KEY_DISPLAY = {}
if HAS_EVDEV:
    # Build from ecodes
    for n in dir(ecodes):
        if n.startswith("KEY_"):
            code = getattr(ecodes, n)
            # Use short name: KEY_A -> A, KEY_LEFTCTRL -> LEFTCTRL
            KEY_DISPLAY[code] = n[4:]
    # Mouse buttons
    KEY_DISPLAY[ecodes.BTN_LEFT] = "LMB"
    KEY_DISPLAY[ecodes.BTN_RIGHT] = "RMB"
    KEY_DISPLAY[ecodes.BTN_MIDDLE] = "MMB"
    KEY_DISPLAY[ecodes.BTN_SIDE] = "MB4"
    KEY_DISPLAY[ecodes.BTN_EXTRA] = "MB5"
    # Friendly aliases
    KEY_DISPLAY[ecodes.KEY_LEFTCTRL] = "Ctrl"
    KEY_DISPLAY[ecodes.KEY_RIGHTCTRL] = "Ctrl"
    KEY_DISPLAY[ecodes.KEY_LEFTSHIFT] = "Shift"
    KEY_DISPLAY[ecodes.KEY_RIGHTSHIFT] = "Shift"
    KEY_DISPLAY[ecodes.KEY_LEFTALT] = "Alt"
    KEY_DISPLAY[ecodes.KEY_RIGHTALT] = "Alt"
    KEY_DISPLAY[ecodes.KEY_LEFTMETA] = "Win"
    KEY_DISPLAY[ecodes.KEY_RIGHTMETA] = "Win"
    KEY_DISPLAY[ecodes.KEY_SPACE] = "Space"
    KEY_DISPLAY[ecodes.KEY_ENTER] = "Enter"
    KEY_DISPLAY[ecodes.KEY_BACKSPACE] = "Backspace"
    KEY_DISPLAY[ecodes.KEY_TAB] = "Tab"
    KEY_DISPLAY[ecodes.KEY_ESC] = "Esc"
    KEY_DISPLAY[ecodes.KEY_CAPSLOCK] = "Caps"

def format_combo(keys):
    if not keys:
        return ""
    # Sort for stable display: modifiers first
    order = {"Ctrl":0, "Shift":0, "Alt":0, "Win":0}
    def sort_key(k):
        name = KEY_DISPLAY.get(k, f"KEY_{k}")
        return (0 if name in order else 1, name)
    sorted_keys = sorted(keys, key=sort_key)
    names = [KEY_DISPLAY.get(k, f"KEY_{k}") for k in sorted_keys]
    return " + ".join(names)

def queue_event(evt):
    with event_lock:
        event_queue.append(evt)
        if len(event_queue) > 200:
            event_queue.pop(0)

def dedup_key_event(ev_code, is_press):
    """State-based dedup: True if this event should be processed (first device only)."""
    with logical_lock:
        if is_press:
            if ev_code in logically_pressed:
                return False  # another device already reported this press
            logically_pressed.add(ev_code)
            return True
        else:  # release
            if ev_code not in logically_pressed:
                return False
            logically_pressed.discard(ev_code)
            return True

# Wheel has no press/release state — time-window dedup (dual-interface dupes
# from receiver+bluetooth land within a few ms; genuine detents are slower)
last_wheel_time = {}
last_wheel_lock = threading.Lock()

def dedup_wheel(combo):
    now = time.time()
    with last_wheel_lock:
        last = last_wheel_time.get(combo, 0)
        if now - last < 0.02:  # 20ms
            return False
        last_wheel_time[combo] = now
        return True

def is_keyboard_device(dev):
    # Strict: must have A-Z and 0-9, not just any >10 keys (avoids Consumer Control / System Control dupes)
    try:
        caps = dev.capabilities().get(ecodes.EV_KEY, [])
        # caps is list of (code, ...) or ints
        codes = set(c[0] if isinstance(c, tuple) else c for c in caps)
        has_alpha = ecodes.KEY_A in codes and ecodes.KEY_Z in codes
        has_num = ecodes.KEY_1 in codes and ecodes.KEY_0 in codes
        has_enter = ecodes.KEY_ENTER in codes
        # Real keyboard has all three
        if has_alpha and has_num and has_enter:
            return True
        # Fallback: at least 80 keys is a full keyboard
        if len(codes) > 80:
            return True
        return False
    except:
        return False

def monitor_device(path, stop_ev):
    try:
        dev = evdev.InputDevice(path)
        obs.script_log(obs.LOG_INFO, f"[wayland-overlay] watching {path}: {dev.name}")
        # Per-device wheel strategy: modern Logitech emit BOTH REL_WHEEL (±1)
        # and REL_WHEEL_HI_RES (±120) per detent — prefer legacy, accumulate hi-res
        REL_WHEEL_HI_RES = getattr(ecodes, 'REL_WHEEL_HI_RES', 11)
        REL_HWHEEL_HI_RES = getattr(ecodes, 'REL_HWHEEL_HI_RES', 12)
        rel_codes = set(c[0] if isinstance(c, tuple) else c for c in dev.capabilities().get(ecodes.EV_REL, []))
        has_legacy_wheel = ecodes.REL_WHEEL in rel_codes or ecodes.REL_HWHEEL in rel_codes
        hi_res_accum = {}  # code -> accumulated hi-res value
        for ev in dev.read_loop():
            if stop_ev.is_set():
                break
            # --- Mouse wheel (EV_REL) -> for Browser Source event_queue ---
            if ev.type == ecodes.EV_REL:
                combo = None
                wheel_evt = None
                if ev.code in (REL_WHEEL_HI_RES, REL_HWHEEL_HI_RES):
                    if has_legacy_wheel:
                        continue  # legacy REL_WHEEL/HWHEEL already reports each detent
                    # Accumulate hi-res into whole detents (120 units)
                    acc = hi_res_accum.get(ev.code, 0) + ev.value
                    if abs(acc) < 120:
                        hi_res_accum[ev.code] = acc
                        continue
                    axis = ecodes.REL_WHEEL if ev.code == REL_WHEEL_HI_RES else ecodes.REL_HWHEEL
                    value = 1 if acc > 0 else -1
                    hi_res_accum[ev.code] = 0
                else:
                    axis, value = ev.code, ev.value
                if axis == ecodes.REL_WHEEL and value != 0:
                    if value > 0:
                        combo = "Scroll ↑"
                        wheel_evt = {"event_type": "mouse_wheel", "direction": 3, "rotation": 1, "amount": abs(value)}
                    else:
                        combo = "Scroll ↓"
                        wheel_evt = {"event_type": "mouse_wheel", "direction": 3, "rotation": -1, "amount": abs(value)}
                elif axis == ecodes.REL_HWHEEL and value != 0:
                    if value > 0:
                        combo = "Scroll →"
                        wheel_evt = {"event_type": "mouse_wheel", "direction": 4, "rotation": 1, "amount": abs(value)}
                    else:
                        combo = "Scroll ←"
                        wheel_evt = {"event_type": "mouse_wheel", "direction": 4, "rotation": -1, "amount": abs(value)}
                if combo:
                    # Cross-device dedup (receiver+bluetooth dual path)
                    if dedup_wheel(combo):
                        with history_lock:
                            history.append((time.time(), combo))
                            if len(history) > history_max:
                                history.pop(0)
                        if wheel_evt:
                            queue_event(wheel_evt)
                continue
            if ev.type != ecodes.EV_KEY:
                continue
            is_press = ev.value == 1
            is_release = ev.value == 0
            # ev.value 2 = repeat, ignore
            if ev.value == 2:
                continue
            # State-based dedup across devices (Logitech multi-interface)
            if not dedup_key_event(ev.code, is_press):
                continue
            # Queue raw event for Browser Source (so same counting as plugin)
            evt_type = "key_pressed" if is_press else "key_released" if is_release else None
            if evt_type:
                # ev.code is Linux input code, same as uiohook scancode for many keys (e.g. 30 -> 0x1e)
                queue_event({"event_type": evt_type, "keycode": ev.code, "rawcode": ev.code, "mask": 0})
                # Also try to infer mask for mouse buttons? For keyboard, mask not needed; onKeyEvent rebuilds via _mouseButtonsMask
                # For mouse buttons (BTN_*), send mouse event as well for Browser Source mouse highlight
                if ev.code in (ecodes.BTN_LEFT, ecodes.BTN_RIGHT, ecodes.BTN_MIDDLE, ecodes.BTN_SIDE, ecodes.BTN_EXTRA):
                    # Map to mouse_pressed / released with mask like libuiohook does
                    # Use same encoding as input-history-windows.html MOUSEENCODE
                    btn_flag = {ecodes.BTN_LEFT: 1<<8, ecodes.BTN_RIGHT: 1<<9, ecodes.BTN_MIDDLE: 1<<10, ecodes.BTN_SIDE: 1<<11, ecodes.BTN_EXTRA: 1<<12}.get(ev.code, 0)
                    # Maintain mask state for Browser Source
                    # We'll just send mouse_pressed/released; the HTML's onKeyEvent will handle mask diff
                    queue_event({"event_type": "mouse_pressed" if is_press else "mouse_released", "button": ev.code, "mask": btn_flag if is_press else 0, "clicks": 1})
            with pressed_lock:
                if is_press:
                    pressed.add(ev.code)
                    cur = set(pressed)
                elif is_release:
                    pressed.discard(ev.code)
                    cur = set(pressed)
                else:
                    continue
            if is_press and cur:
                combo = format_combo(cur)
                with history_lock:
                    history.append((time.time(), combo))
                    if len(history) > history_max:
                        history.pop(0)
    except Exception as e:
        obs.script_log(obs.LOG_WARNING, f"[wayland-overlay] {path}: {e}")

def get_state_snapshot():
    now = time.time()
    with history_lock:
        # Clean timeout in snapshot (don't mutate original if timeout=0)
        if timeout_ms > 0:
            filtered = [(t,c) for t,c in history if (now - t) * 1000 < timeout_ms]
            # Keep live if currently pressed
            with pressed_lock:
                has_pressed = len(pressed) > 0
                live_combo = format_combo(pressed) if has_pressed else ""
            if has_pressed and not filtered:
                filtered.append((now, live_combo))
            snap_history = [c for t,c in filtered[-history_max:]]
        else:
            snap_history = [c for t,c in history[-history_max:]]
            with pressed_lock:
                live_combo = format_combo(pressed) if pressed else ""
        with pressed_lock:
            live = format_combo(pressed) if pressed else ""
            pressed_list = [KEY_DISPLAY.get(k, f"KEY_{k}") for k in sorted(pressed)]
        # Build display lines like Text Source does
        if live and (not snap_history or snap_history[-1] != live):
            display = snap_history + [live] if snap_history else [live]
        else:
            display = snap_history
    return {
        "history": snap_history,
        "display": display,
        "live": live,
        "pressed": pressed_list,
        "ts": now
    }

class StateHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path in ("/state", "/state.json"):
            snap = get_state_snapshot()
            body = json.dumps(snap).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif self.path in ("/events", "/events.json"):
            with event_lock:
                evts = list(event_queue)
                event_queue.clear()
            body = json.dumps({"events": evts}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif self.path in ("/", "/health"):
            body = b"wayland-input-overlay running"
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_response(404)
            self.end_headers()
    def log_message(self, fmt, *args):
        if "/state" not in fmt % args and "/events" not in fmt % args:
            obs.script_log(obs.LOG_INFO, f"[wayland-overlay] http {fmt % args}")

def start_http_server():
    global http_server, http_thread
    if not enable_browser:
        return
    if http_server is not None:
        return
    try:
        http_server = HTTPServer(("127.0.0.1", browser_port), StateHandler)
        http_thread = threading.Thread(target=http_server.serve_forever, daemon=True)
        http_thread.start()
        obs.script_log(obs.LOG_INFO, f"[wayland-overlay] HTTP state at http://127.0.0.1:{browser_port}/state (for input-history-wayland.html)")
    except Exception as e:
        obs.script_log(obs.LOG_WARNING, f"[wayland-overlay] HTTP failed on :{browser_port}: {e}")
        http_server = None

def stop_http_server():
    global http_server
    if http_server:
        try:
            http_server.shutdown()
        except:
            pass
        http_server = None

def update_text_source():
    if not text_source_name:
        return
    source = obs.obs_get_source_by_name(text_source_name)
    if source is None:
        return
    # Check source is text
    sid = obs.obs_source_get_unversioned_id(source)
    if sid not in ("text_ft2_source_v2", "text_ft2_source", "text_gdiplus", "text_gdiplus_v2"):
        # Still try to set "text" property, may be other source
        pass

    snap = get_state_snapshot()
    text = "\n".join(snap["display"]) if snap["display"] else ""

    settings = obs.obs_data_create()
    obs.obs_data_set_string(settings, "text", text)
    obs.obs_source_update(source, settings)
    obs.obs_data_release(settings)
    obs.obs_source_release(source)

def start_threads():
    global threads
    stop_threads()
    if not enabled:
        obs.script_log(obs.LOG_INFO, "[wayland-overlay] disabled")
        return
    if not HAS_EVDEV:
        obs.script_log(obs.LOG_ERROR, "[wayland-overlay] python3-evdev not installed — sudo apt install python3-evdev")
        # Show error in text source
        if text_source_name:
            s = obs.obs_get_source_by_name(text_source_name)
            if s:
                d = obs.obs_data_create()
                obs.obs_data_set_string(d, "text", "Missing python3-evdev\nsudo apt install python3-evdev")
                obs.obs_source_update(s, d)
                obs.obs_data_release(d)
                obs.obs_source_release(s)
        return
    readable = sum(1 for p in glob.glob("/dev/input/event*") if os.access(p, os.R_OK))
    total = len(glob.glob("/dev/input/event*"))
    if readable == 0:
        obs.script_log(obs.LOG_ERROR, f"[wayland-overlay] No /dev/input readable ({readable}/{total}) — sudo usermod -aG input $USER && reboot")
        if text_source_name:
            s = obs.obs_get_source_by_name(text_source_name)
            if s:
                d = obs.obs_data_create()
                obs.obs_data_set_string(d, "text", "No /dev/input permission\nsudo usermod -aG input $USER\nthen reboot")
                obs.obs_source_update(s, d)
                obs.obs_data_release(d)
                obs.obs_source_release(s)
        return
    obs.script_log(obs.LOG_INFO, f"[wayland-overlay] {readable}/{total} devices readable")

    paths = glob.glob("/dev/input/event*")
    # Strict keyboard filter to avoid dupes (Logitech receiver exposes 4 interfaces)
    filtered = []
    for p in paths:
        try:
            d = evdev.InputDevice(p)
            if is_keyboard_device(d):
                filtered.append(p)
        except:
            pass
    # If no full keyboard found, fall back to any with >10 keys (avoid empty)
    if not filtered:
        for p in paths:
            try:
                d = evdev.InputDevice(p)
                caps = d.capabilities().get(ecodes.EV_KEY, [])
                if len(caps) > 10:
                    filtered.append(p)
            except:
                pass
    if filtered:
        paths = filtered
    # Always add mouse wheel devices separately for scroll (they are not keyboards)
    # Find devices with REL_WHEEL
    for p in glob.glob("/dev/input/event*"):
        if p in paths:
            continue
        try:
            d = evdev.InputDevice(p)
            caps = d.capabilities().get(ecodes.EV_REL, [])
            codes = set(c[0] if isinstance(c, tuple) else c for c in caps)
            if ecodes.REL_WHEEL in codes or ecodes.REL_HWHEEL in codes:
                paths.append(p)
        except:
            pass
    obs.script_log(obs.LOG_INFO, f"[wayland-overlay] monitoring {len(paths)} devices: {paths}")

    threads = []
    for p in paths:
        if not os.access(p, os.R_OK):
            continue
        stop_ev = threading.Event()
        t = threading.Thread(target=monitor_device, args=(p, stop_ev), daemon=True)
        t.start()
        threads.append((t, stop_ev))
    # Timer for UI updates (50ms ~20fps)
    obs.timer_add(update_text_source, 50)
    start_http_server()

def stop_threads():
    global threads
    # Signal each thread with its OWN event — old threads blocked in read()
    # will exit on next input instead of stacking across reloads
    for t, stop_ev in threads:
        stop_ev.set()
    threads = []
    try:
        obs.timer_remove(update_text_source)
    except:
        pass
    stop_http_server()
    with pressed_lock:
        pressed.clear()
    with logical_lock:
        logically_pressed.clear()
    with history_lock:
        history.clear()
    with event_lock:
        event_queue.clear()

# — OBS Script API —

def script_description():
    return (
        "<h2>Wayland Input Overlay (evdev)</h2>"
        "Replaces <code>input-overlay</code> which fails on Wayland:<br>"
        "<code>[input-overlay] Wayland is not supported by libuiohook</code><br><br>"
        "Reads <code>/dev/input/event*</code> directly.<br>"
        "Supports <b>Text Source</b> + <b>Browser Source</b> (your HTML) simultaneously.<br>"
        "Works even when OBS is not focused.<br><br>"
        "<b>Setup:</b><br>"
        "1. <code>sudo apt install python3-evdev</code><br>"
        "2. <code>sudo usermod -aG input $USER</code> → <b>reboot</b><br>"
        "3a. <b>Text Source:</b> create Text Source 'Overlay Text' → select below<br>"
        "3b. <b>Browser Source (your design):</b> use "
        "<code>input-history-wayland.html</code> (copy of your HTML) → URL "
        "<code>file:///home/pain/NetBeansProjects/obs-input-overlay-design/input-history-wayland.html</code><br>"
        "    The HTML polls <code>http://127.0.0.1:16898/state</code> from this script.<br>"
        "5. Type anywhere — history appears<br>"
    )

def script_properties():
    props = obs.obs_properties_create()
    obs.obs_properties_add_bool(props, "enabled", "Enabled")
    p = obs.obs_properties_add_list(props, "text_source", "Text Source (optional)", obs.OBS_COMBO_TYPE_EDITABLE, obs.OBS_COMBO_FORMAT_STRING)
    sources = obs.obs_enum_sources()
    if sources is not None:
        for s in sources:
            sid = obs.obs_source_get_unversioned_id(s)
            if sid in ("text_ft2_source_v2", "text_ft2_source", "text_gdiplus", "text_gdiplus_v2"):
                name = obs.obs_source_get_name(s)
                obs.obs_property_list_add_string(p, name, name)
        obs.source_list_release(sources)
    obs.obs_properties_add_int(props, "history_max", "History Lines", 1, 20, 1)
    obs.obs_properties_add_int(props, "timeout_ms", "Hide After (ms, 0=never)", 0, 10000, 100)
    obs.obs_properties_add_bool(props, "enable_browser", "Enable Browser Source HTTP (for input-history-wayland.html)")
    obs.obs_properties_add_int(props, "browser_port", "Browser HTTP Port", 1024, 65535, 1)
    return props

def script_defaults(settings):
    obs.obs_data_set_default_bool(settings, "enabled", True)
    obs.obs_data_set_default_string(settings, "text_source", "")
    obs.obs_data_set_default_int(settings, "history_max", 6)
    obs.obs_data_set_default_int(settings, "timeout_ms", 3000)
    obs.obs_data_set_default_bool(settings, "enable_browser", True)
    obs.obs_data_set_default_int(settings, "browser_port", 16898)

def script_update(settings):
    global text_source_name, enabled, history_max, timeout_ms, enable_browser, browser_port
    enabled = obs.obs_data_get_bool(settings, "enabled")
    text_source_name = obs.obs_data_get_string(settings, "text_source")
    history_max = obs.obs_data_get_int(settings, "history_max")
    timeout_ms = obs.obs_data_get_int(settings, "timeout_ms")
    enable_browser = obs.obs_data_get_bool(settings, "enable_browser")
    browser_port = obs.obs_data_get_int(settings, "browser_port")
    obs.script_log(obs.LOG_INFO, f"[wayland-overlay] config: source='{text_source_name}' history={history_max} timeout={timeout_ms}ms browser={enable_browser}:{browser_port}")
    start_threads()

def script_load(settings):
    obs.script_log(obs.LOG_INFO, "[wayland-overlay] loaded")
    start_threads()

def script_unload():
    obs.script_log(obs.LOG_INFO, "[wayland-overlay] unloading")
    stop_threads()
    # Clear text source on unload
    if text_source_name:
        s = obs.obs_get_source_by_name(text_source_name)
        if s:
            d = obs.obs_data_create()
            obs.obs_data_set_string(d, "text", "")
            obs.obs_source_update(s, d)
            obs.obs_data_release(d)
            obs.obs_source_release(s)

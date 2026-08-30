import obspython as obs
import os
import glob
import json
import threading
import time
import configparser
import base64
import hashlib
import ctypes
import ctypes.util

# Wayland Hotkeys — fires ALL Settings → Hotkeys unfocused.
# Fixes: libobs/obs-nix-wayland.c:261 always returns false on Wayland.
# Method: evdev reads /dev/input/event* → match against bindings parsed from
# OBS config (profile basic.ini [Hotkeys] + scene collection source hotkeys)
# → obs-websocket TriggerHotkeyByName (which does routed_callback true+false,
#   proper press AND release; Python/SWIG cannot enum hotkey ids itself).
# Requires: python3-evdev, input group, and obs-websocket server ENABLED
#           (Tools → WebSocket Server Settings → Enable).

try:
    import evdev
    from evdev import ecodes
    HAS_EVDEV = True
except ImportError:
    HAS_EVDEV = False

try:
    import websocket  # websocket-client
    HAS_WS = True
except ImportError:
    HAS_WS = False

enabled = True
log_debug = False

try:
    OBS_KEY_NONE = obs.OBS_KEY_NONE
except AttributeError:
    OBS_KEY_NONE = 0

threads = []  # (thread, stop_event)
current_modifiers = 0
modifier_counts = {}
pressed_lock = threading.Lock()
logically_pressed = set()
logical_lock = threading.Lock()
ws_lock = threading.Lock()

# (obs_key, modifiers) -> [(hotkeyName, contextName), ...]
combo_map = {}
combo_map_lock = threading.Lock()

EVDEV_ALIAS = {
    "ESC": "ESCAPE",
    "KPASTERISK": "NUMASTERISK", "KPSLASH": "NUMSLASH", "KPMINUS": "NUMMINUS",
    "KPPLUS": "NUMPLUS", "KPDOT": "NUMPERIOD", "KPCOMMA": "NUMCOMMA",
    "KPEQUAL": "NUMEQUAL",
    "KP0": "NUM0", "KP1": "NUM1", "KP2": "NUM2", "KP3": "NUM3", "KP4": "NUM4",
    "KP5": "NUM5", "KP6": "NUM6", "KP7": "NUM7", "KP8": "NUM8", "KP9": "NUM9",
    "KPENTER": "KP_ENTER",
    "LEFTBRACE": "BRACKETLEFT", "RIGHTBRACE": "BRACKETRIGHT",
    "APOSTROPHE": "QUOTE", "GRAVE": "QUOTELEFT", "DOT": "PERIOD",
}

MODIFIER_MAP = {}

def init_modifier_map():
    global MODIFIER_MAP
    if not HAS_EVDEV:
        return
    MODIFIER_MAP = {
        ecodes.KEY_LEFTSHIFT: obs.INTERACT_SHIFT_KEY,
        ecodes.KEY_RIGHTSHIFT: obs.INTERACT_SHIFT_KEY,
        ecodes.KEY_LEFTCTRL: obs.INTERACT_CONTROL_KEY,
        ecodes.KEY_RIGHTCTRL: obs.INTERACT_CONTROL_KEY,
        ecodes.KEY_LEFTALT: obs.INTERACT_ALT_KEY,
        ecodes.KEY_RIGHTALT: obs.INTERACT_ALT_KEY,
        ecodes.KEY_LEFTMETA: obs.INTERACT_COMMAND_KEY,
        ecodes.KEY_RIGHTMETA: obs.INTERACT_COMMAND_KEY,
    }

# — layout-aware scancode → OBS key via libxkbcommon —
# evdev reports PHYSICAL scancodes (AZERTY M = KEY_SEMICOLON); OBS bindings are
# layout-aware (OBS_KEY_M). libobs/obs-nix-wayland.c does the same xkb mapping.
XKB = None
XKB_KEYMAP = None
XKB_CODE_CACHE = {}

XKB_NAME_OVERRIDES = {
    "Escape": "ESCAPE", "Return": "ENTER", "space": "SPACE", "Tab": "TAB",
    "BackSpace": "BACKSPACE", "Prior": "PAGEUP", "Next": "PAGEDOWN",
    "End": "END", "Home": "HOME", "Left": "LEFT", "Right": "RIGHT",
    "Up": "UP", "Down": "DOWN", "Insert": "INSERT", "Delete": "DELETE",
    "Pause": "PAUSE", "Print": "PRINT", "Menu": "MENU", "Capital": "CAPSLOCK",
    "Num_Lock": "NUMLOCK", "Scroll_Lock": "SCROLLLOCK", "Multi_key": "MULTI_KEY",
    "KP_Add": "NUMPLUS", "KP_Subtract": "NUMMINUS", "KP_Multiply": "NUMASTERISK",
    "KP_Divide": "NUMSLASH", "KP_Decimal": "NUMPERIOD", "KP_Separator": "NUMCOMMA",
    "KP_Enter": "KP_ENTER", "ISO_Level3_Shift": "ALTGR", "less": "LESS",
    "scheduled": None,
}

def get_kb_layout():
    """Return (layout, variant) from env or GNOME settings."""
    v = os.environ.get("XKB_DEFAULT_LAYOUT")
    if not v:
        try:
            import subprocess, re
            out = subprocess.run(["gsettings", "get", "org.gnome.desktop.input-sources", "sources"],
                                 capture_output=True, text=True, timeout=2).stdout
            m = re.search(r"\('xkb',\s*'([^']+)'", out)
            if m:
                v = m.group(1)
        except Exception:
            pass
    if not v:
        return "us", ""
    layout, _, variant = v.partition("+")
    return layout.split(",")[0], variant.split(",")[0]

def init_xkb():
    global XKB, XKB_KEYMAP
    try:
        path = ctypes.util.find_library("xkbcommon")
        if not path:
            return
        x = ctypes.CDLL(path)
        class RuleNames(ctypes.Structure):
            _fields_ = [(n, ctypes.c_char_p) for n in ("rules", "model", "layout", "variant", "options")]
        x.xkb_context_new.restype = ctypes.c_void_p
        x.xkb_context_new.argtypes = [ctypes.c_int]
        x.xkb_keymap_new_from_names.restype = ctypes.c_void_p
        x.xkb_keymap_new_from_names.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_int]
        x.xkb_keymap_key_get_syms_by_level.restype = ctypes.c_int
        x.xkb_keymap_key_get_syms_by_level.argtypes = [ctypes.c_void_p, ctypes.c_uint32, ctypes.c_uint32, ctypes.c_uint32, ctypes.POINTER(ctypes.POINTER(ctypes.c_uint32))]
        x.xkb_keysym_to_utf8.restype = ctypes.c_int
        x.xkb_keysym_to_utf8.argtypes = [ctypes.c_uint32, ctypes.c_char_p, ctypes.c_size_t]
        x.xkb_keysym_get_name.restype = ctypes.c_int
        x.xkb_keysym_get_name.argtypes = [ctypes.c_uint32, ctypes.c_char_p, ctypes.c_size_t]
        layout, variant = get_kb_layout()
        rn = RuleNames(layout=layout.encode(), variant=variant.encode() if variant else None)
        ctx = x.xkb_context_new(0)
        km = x.xkb_keymap_new_from_names(ctx, ctypes.byref(rn), 0)
        if not km:
            obs.script_log(obs.LOG_WARNING, f"[wayland-hotkeys] xkb keymap failed for layout '{layout}' variant '{variant}'")
            return
        XKB = x
        XKB_KEYMAP = km
        obs.script_log(obs.LOG_INFO, f"[wayland-hotkeys] xkb layout '{layout}' variant '{variant}' loaded (layout-aware key mapping)")
    except Exception as e:
        obs.script_log(obs.LOG_WARNING, f"[wayland-hotkeys] xkb init failed: {e}")

def xkb_code_to_obs_key(code):
    if code in XKB_CODE_CACHE:
        return XKB_CODE_CACHE[code]
    result = OBS_KEY_NONE
    try:
        syms = ctypes.POINTER(ctypes.c_uint32)()
        n = XKB.xkb_keymap_key_get_syms_by_level(XKB_KEYMAP, code + 8, 0, 0, ctypes.byref(syms))
        if n > 0:
            ks = syms[0]
            name_buf = ctypes.create_string_buffer(64)
            XKB.xkb_keysym_get_name(ks, name_buf, 64)
            ksname = name_buf.value.decode()
            utf_buf = ctypes.create_string_buffer(8)
            XKB.xkb_keysym_to_utf8(ks, utf_buf, 8)
            utf = utf_buf.value.decode("utf-8", errors="ignore")
            override = XKB_NAME_OVERRIDES.get(ksname, None)
            candidates = [override, ksname.upper(), utf.upper(), ksname]
            for cand in candidates:
                if not cand:
                    continue
                key = obs.obs_key_from_name(f"OBS_KEY_{cand}")
                if key != OBS_KEY_NONE:
                    result = key
                    break
    except Exception:
        result = OBS_KEY_NONE
    XKB_CODE_CACHE[code] = result
    return result

def evdev_to_obs_key(code):
    if code == ecodes.BTN_LEFT:
        return obs.obs_key_from_name("OBS_KEY_MOUSE1")
    if code == ecodes.BTN_RIGHT:
        return obs.obs_key_from_name("OBS_KEY_MOUSE2")
    if code == ecodes.BTN_MIDDLE:
        return obs.obs_key_from_name("OBS_KEY_MOUSE3")
    if code == ecodes.BTN_SIDE:
        return obs.obs_key_from_name("OBS_KEY_MOUSE4")
    if code == ecodes.BTN_EXTRA:
        return obs.obs_key_from_name("OBS_KEY_MOUSE5")
    # Layout-aware via xkb (AZERTY M = KEY_SEMICOLON scancode etc.)
    if XKB is not None and XKB_KEYMAP is not None:
        return xkb_code_to_obs_key(code)
    # Fallback: scancode-name based (only correct for US layouts)
    try:
        name = ecodes.KEY[code]
    except:
        return OBS_KEY_NONE
    if not name.startswith("KEY_"):
        return OBS_KEY_NONE
    raw = name[4:]
    if code in MODIFIER_MAP:
        return OBS_KEY_NONE
    for cand in [raw, EVDEV_ALIAS.get(raw)]:
        if cand is None:
            continue
        key = obs.obs_key_from_name(f"OBS_KEY_{cand}")
        if key != OBS_KEY_NONE:
            return key
    if log_debug:
        obs.script_log(obs.LOG_WARNING, f"[wayland-hotkeys] no OBS key for evdev {name} ({code})")
    return OBS_KEY_NONE

# — config binding parsing (Python cannot obs_enum_hotkeys — SWIG limitation) —

OBS_CONFIG_DIR = os.path.expanduser("~/.config/obs-studio")

def read_global_ini():
    cp = configparser.ConfigParser(interpolation=None)
    cp.read(os.path.join(OBS_CONFIG_DIR, "global.ini"))
    profile_dir = "default"
    collection = ""
    try:
        profile_dir = cp.get("Basic", "ProfileDir", fallback="default")
        collection = cp.get("Basic", "SceneCollection", fallback="")
    except Exception:
        pass
    return profile_dir, collection

def add_binding(combo_map_local, binding_json, hotkey_name, context):
    try:
        data = json.loads(binding_json)
    except Exception:
        return
    # Two storage formats: basic.ini uses {"bindings":[...]}, scene collection
    # JSON uses a bare [...] array
    if isinstance(data, dict):
        bindings = data.get("bindings", [])
    elif isinstance(data, list):
        bindings = data
    else:
        return
    for b in bindings:
        mods = 0
        if b.get("shift"):
            mods |= obs.INTERACT_SHIFT_KEY
        if b.get("control"):
            mods |= obs.INTERACT_CONTROL_KEY
        if b.get("alt"):
            mods |= obs.INTERACT_ALT_KEY
        if b.get("command"):
            mods |= obs.INTERACT_COMMAND_KEY
        key_name = b.get("key", "OBS_KEY_NONE")
        key = obs.obs_key_from_name(key_name) if key_name else OBS_KEY_NONE
        entry = (hotkey_name, context or "")
        lst = combo_map_local.setdefault((int(key), int(mods)), [])
        if entry not in lst:  # dedupe across merged profiles
            lst.append(entry)

def load_bindings_from_config():
    """Parse profile basic.ini [Hotkeys] + scene collection source hotkeys."""
    new_map = {}
    profile_dir, collection = read_global_ini()

    # Profile basic.ini (frontend + script hotkeys)
    ini_paths = []
    preferred = os.path.join(OBS_CONFIG_DIR, "basic", "profiles", profile_dir, "basic.ini")
    if os.path.isfile(preferred):
        ini_paths.append(preferred)
    else:
        for p in glob.glob(os.path.join(OBS_CONFIG_DIR, "basic", "profiles", "*", "basic.ini")):
            if p not in ini_paths:
                ini_paths.append(p)

    for path in ini_paths:
        cp = configparser.ConfigParser(interpolation=None)
        try:
            cp.read(path)
        except Exception as e:
            obs.script_log(obs.LOG_WARNING, f"[wayland-hotkeys] parse {path}: {e}")
            continue
        if not cp.has_section("Hotkeys"):
            continue
        for name, val in cp.items("Hotkeys"):
            add_binding(new_map, val, name, "")

    # Scene collection(s) (source + scene-item hotkeys). global.ini can be
    # stale (e.g. "Sans nom" while the live collection is CODING.json), so
    # fall back to merging every collection like we do for profiles.
    scenes_dir = os.path.join(OBS_CONFIG_DIR, "basic", "scenes")
    sc_paths = []
    preferred_sc = os.path.join(scenes_dir, collection + ".json")
    if collection and os.path.isfile(preferred_sc):
        sc_paths.append(preferred_sc)
    else:
        for p in sorted(glob.glob(os.path.join(scenes_dir, "*.json"))):
            if p not in sc_paths:
                sc_paths.append(p)

    for sc_path in sc_paths:
        try:
            with open(sc_path, encoding="utf-8") as f:
                sc = json.load(f)
            for src in sc.get("sources", []):
                hk = src.get("hotkeys") or {}
                for name, val in hk.items():
                    if isinstance(val, (dict, list)):
                        # context = owning scene/source name: hotkey names like
                        # libobs.show_scene_item.N repeat across scenes
                        add_binding(new_map, json.dumps(val), name, src.get("name", ""))
        except Exception as e:
            obs.script_log(obs.LOG_WARNING, f"[wayland-hotkeys] parse {sc_path}: {e}")

    with combo_map_lock:
        combo_map.clear()
        combo_map.update(new_map)

    obs.script_log(obs.LOG_INFO, f"[wayland-hotkeys] config bindings: {len(combo_map)} combos (profile='{profile_dir}' collection='{collection}')")
    for (k, m), entries in sorted(combo_map.items()):
        try:
            kn = obs.obs_key_to_name(k)
        except:
            kn = str(k)
        obs.script_log(obs.LOG_INFO, f"  {kn} mods={m} -> {entries}")

# — obs-websocket trigger —

def read_ws_config():
    path = os.path.join(OBS_CONFIG_DIR, "plugin_config", "obs-websocket", "config.json")
    try:
        with open(path, encoding="utf-8") as f:
            c = json.load(f)
        return int(c.get("server_port", 4455)), c.get("server_password", ""), bool(c.get("auth_required", True)), bool(c.get("server_enabled", False))
    except Exception:
        return 4455, "", True, False

def ws_trigger_batch(entries):
    """Fire a batch of hotkey names over ONE connection, sequentially, in map
    order (parallel threads race show/hide into random final states)."""
    if not HAS_WS:
        return
    port, password, auth_required, server_enabled = read_ws_config()
    if not server_enabled:
        obs.script_log(obs.LOG_ERROR, "[wayland-hotkeys] obs-websocket server DISABLED — Tools → WebSocket Server Settings → Enable")
        return
    ws = None
    try:
        with ws_lock:
            ws = websocket.create_connection(f"ws://127.0.0.1:{port}", timeout=3)
            hello = json.loads(ws.recv())
            d = hello.get("d", {})
            if auth_required and d.get("authentication"):
                auth = d["authentication"]
                secret = base64.b64encode(hashlib.sha256((password + auth["salt"]).encode()).digest()).decode()
                proof = base64.b64encode(hashlib.sha256((secret + auth["challenge"]).encode()).digest()).decode()
                ws.send(json.dumps({"op": 1, "d": {"rpcVersion": 1, "authentication": proof}}))
                ws.recv()  # Identified
            for hotkey_name, context in entries:
                req_data = {"hotkeyName": hotkey_name}
                if context:
                    req_data["contextName"] = context
                ws.send(json.dumps({"op": 6, "d": {"requestType": "TriggerHotkeyByName", "requestId": "wh", "requestData": req_data}}))
                # Skip event frames (op 5) until the response (op 7) arrives
                status = {}
                deadline = time.time() + 3
                while time.time() < deadline:
                    msg = json.loads(ws.recv())
                    if msg.get("op") == 7 and msg.get("d", {}).get("requestId") == "wh":
                        status = msg.get("d", {}).get("requestStatus", {})
                        break
                if log_debug:
                    obs.script_log(obs.LOG_INFO, f"[wayland-hotkeys] ws TriggerHotkeyByName '{hotkey_name}' ctx='{context}' -> {status}")
                # Self-heal: hotkey no longer registered in this session
                if status.get("code") == 600:
                    with combo_map_lock:
                        for k, lst in combo_map.items():
                            if (hotkey_name, context) in lst:
                                lst.remove((hotkey_name, context))
                    obs.script_log(obs.LOG_INFO, f"[wayland-hotkeys] dropped stale hotkey '{hotkey_name}' (not registered in this session)")
    except Exception as e:
        obs.script_log(obs.LOG_WARNING, f"[wayland-hotkeys] ws failed: {e}")
    finally:
        if ws is not None:
            try:
                ws.close()
            except Exception:
                pass

def trigger_hotkeys(obs_key, mods):
    """Fire bindings matching key+modifiers (exact, then non-strict subset)."""
    with combo_map_lock:
        entries = combo_map.get((obs_key, mods))
        if not entries:
            for (k, bmods), hids in combo_map.items():
                if k == obs_key and (bmods & mods) == bmods:
                    entries = hids
                    break
    if not entries:
        if log_debug:
            obs.script_log(obs.LOG_INFO, f"[wayland-hotkeys] no binding for key={obs_key} mods={mods}")
        return False
    if log_debug:
        for name, context in entries:
            obs.script_log(obs.LOG_INFO, f"[wayland-hotkeys] press '{name}' ctx='{context}' mods={mods}")
    # Sequential batch: parallel calls race (show/hide order nondeterministic)
    threading.Thread(target=ws_trigger_batch, args=(entries,), daemon=True).start()
    return True

def is_keyboard_device(dev):
    try:
        caps = dev.capabilities().get(ecodes.EV_KEY, [])
        codes = set(c[0] if isinstance(c, tuple) else c for c in caps)
        has_alpha = ecodes.KEY_A in codes and ecodes.KEY_Z in codes
        has_num = ecodes.KEY_1 in codes and ecodes.KEY_0 in codes
        has_enter = ecodes.KEY_ENTER in codes
        if has_alpha and has_num and has_enter:
            return True
        if len(codes) > 80:
            return True
        return False
    except:
        return False

def dedup_key_event(ev_code, is_press):
    with logical_lock:
        if is_press:
            if ev_code in logically_pressed:
                return False
            logically_pressed.add(ev_code)
            return True
        else:
            if ev_code not in logically_pressed:
                return False
            logically_pressed.discard(ev_code)
            return True

def monitor_device(path, stop_ev):
    global current_modifiers
    try:
        dev = evdev.InputDevice(path)
        obs.script_log(obs.LOG_INFO, f"[wayland-hotkeys] watching {path}: {dev.name}")
        for ev in dev.read_loop():
            if stop_ev.is_set():
                break
            if ev.type != ecodes.EV_KEY:
                continue
            is_press = ev.value == 1
            is_release = ev.value == 0
            if ev.value == 2:
                continue
            if not dedup_key_event(ev.code, is_press):
                continue
            if log_debug:
                try:
                    evname = ecodes.KEY[ev.code]
                except:
                    evname = str(ev.code)
                obs.script_log(obs.LOG_INFO, f"[wayland-hotkeys] ev {path.split('/')[-1]} {evname} {'down' if is_press else 'up'}")
            if ev.code in MODIFIER_MAP:
                flag = MODIFIER_MAP[ev.code]
                with pressed_lock:
                    cnt = modifier_counts.get(flag, 0)
                    if is_press:
                        modifier_counts[flag] = cnt + 1
                        current_modifiers |= flag
                    elif is_release:
                        if cnt <= 1:
                            modifier_counts.pop(flag, None)
                            current_modifiers &= ~flag
                        else:
                            modifier_counts[flag] = cnt - 1
                with pressed_lock:
                    mods = current_modifiers
                if is_press:
                    trigger_hotkeys(OBS_KEY_NONE, mods)  # modifiers-only bindings
                continue
            if not is_press:
                continue  # TriggerHotkeyByName does press+release in one call
            with pressed_lock:
                mods = current_modifiers
            obs_key = evdev_to_obs_key(ev.code)
            if obs_key == OBS_KEY_NONE:
                continue
            trigger_hotkeys(obs_key, mods)
    except Exception as e:
        obs.script_log(obs.LOG_WARNING, f"[wayland-hotkeys] {path}: {e}")

def start_threads():
    global threads
    stop_threads()
    if not enabled:
        obs.script_log(obs.LOG_INFO, "[wayland-hotkeys] disabled")
        return
    if not HAS_EVDEV:
        obs.script_log(obs.LOG_ERROR, "[wayland-hotkeys] python3-evdev not installed — sudo apt install python3-evdev")
        return
    if not HAS_WS:
        obs.script_log(obs.LOG_ERROR, "[wayland-hotkeys] python3-websocket not installed — sudo apt install python3-websocket")
    init_modifier_map()
    init_xkb()
    load_bindings_from_config()

    port, password, auth_required, server_enabled = read_ws_config()
    if not server_enabled:
        obs.script_log(obs.LOG_ERROR, "[wayland-hotkeys] ENABLE obs-websocket: Tools → WebSocket Server Settings → Enable (port %d)" % port)

    readable = sum(1 for p in glob.glob("/dev/input/event*") if os.access(p, os.R_OK))
    total = len(glob.glob("/dev/input/event*"))
    if readable == 0:
        obs.script_log(obs.LOG_ERROR, f"[wayland-hotkeys] No /dev/input/event* readable ({readable}/{total}) — sudo usermod -aG input $USER && reboot")
        return
    obs.script_log(obs.LOG_INFO, f"[wayland-hotkeys] {readable}/{total} devices readable")

    paths = glob.glob("/dev/input/event*")
    filtered = []
    for p in paths:
        try:
            d = evdev.InputDevice(p)
            if is_keyboard_device(d):
                filtered.append(p)
        except:
            pass
    if filtered:
        paths = filtered
    obs.script_log(obs.LOG_INFO, f"[wayland-hotkeys] monitoring {len(paths)} devices: {paths}")

    threads = []
    for p in paths:
        if not os.access(p, os.R_OK):
            continue
        stop_ev = threading.Event()
        t = threading.Thread(target=monitor_device, args=(p, stop_ev), daemon=True)
        t.start()
        threads.append((t, stop_ev))

def stop_threads():
    global threads, current_modifiers
    for t, stop_ev in threads:
        stop_ev.set()
    threads = []
    current_modifiers = 0
    modifier_counts.clear()
    with logical_lock:
        logically_pressed.clear()

# — OBS Script API —

def script_description():
    return (
        "<h2>Wayland Hotkeys — All Hotkeys (via obs-websocket)</h2>"
        "Fixes <code>libobs/obs-nix-wayland.c:261</code> (Wayland never delivers keys unfocused).<br>"
        "evdev reads <code>/dev/input/event*</code>, matches your <b>Settings → Hotkeys</b> bindings "
        "(parsed from profile/scene config) and fires them via "
        "<b>obs-websocket TriggerHotkeyByName</b> (proper press+release, repeatable).<br><br>"
        "<b>REQUIRED:</b><br>"
        "1. Tools → <b>WebSocket Server Settings → Enable</b> (port 4455)<br>"
        "2. <code>sudo apt install python3-evdev</code><br>"
        "3. <code>sudo usermod -aG input $USER</code> → <b>reboot</b><br>"
        "4. After <i>changing</i> hotkeys: click <b>Reload bindings</b> below<br>"
    )

def script_properties():
    props = obs.obs_properties_create()
    obs.obs_properties_add_bool(props, "enabled", "Enabled (fire all hotkeys)")
    obs.obs_properties_add_bool(props, "log_debug", "Debug logging (Help → Logs)")
    obs.obs_properties_add_button(props, "reload_bindings", "Reload bindings from config", reload_bindings_pressed)
    return props

def reload_bindings_pressed(props, prop):
    load_bindings_from_config()
    return True

def script_defaults(settings):
    obs.obs_data_set_default_bool(settings, "enabled", True)
    obs.obs_data_set_default_bool(settings, "log_debug", False)

def script_update(settings):
    global enabled, log_debug
    enabled = obs.obs_data_get_bool(settings, "enabled")
    log_debug = obs.obs_data_get_bool(settings, "log_debug")
    obs.script_log(obs.LOG_INFO, f"[wayland-hotkeys] enabled={enabled} debug={log_debug}")
    start_threads()

def script_load(settings):
    obs.script_log(obs.LOG_INFO, "[wayland-hotkeys] loaded — hotkeys fire unfocused via obs-websocket")
    start_threads()

def script_unload():
    obs.script_log(obs.LOG_INFO, "[wayland-hotkeys] unloading")
    stop_threads()

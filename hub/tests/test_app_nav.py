"""Voice-navigation corpus for nlu/app_nav.match_navigation.

Each row is (screen, what the person said, what the app should do). None means
"not navigation": the words go to the house as a command.

Run: python3 tests/test_app_nav.py   (from anywhere)
"""
import inspect
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("HOMEVOICE_HOME", tempfile.mkdtemp())

from nlu.app_nav import match_navigation  # noqa: E402


class _Mod:
    match_navigation = staticmethod(match_navigation)


ACTIONS = ['add_device', 'update_device', 'remove_device', 'add_scene',
           'update_scene', 'remove_scene', 'add_automation',
           'update_automation', 'remove_automation', 'learn_ir']

CAMS = [{"id": "cam1", "label": "Front Door", "kind": "camera"},
        {"id": "cam2", "label": "Backyard Camera", "kind": "camera"}]
DEVS = [("dev1", "kitchen light"), ("dev2", "living room lamp"),
        ("dev3", "bedroom fan"), ("dev4", "garage door"),
        ("dev5", "front door lock"), ("dev6", "tv"), ("dev7", "porch light"),
        ("dev8", "heater"), ("dev9", "office"), ("dev10", "garden button")]
DEVICE_NAMES = [n for _, n in DEVS] + ["Front Door", "Backyard Camera"]
SCENES = ["movie time", "good night", "leaving home"]
AUTOS = [("a1", "Porch light at sunset"), ("a2", "Motion night light")]
NOTICES = [("n1", "3 devices need a description"), ("n2", "Kitchen board was rewired")]
HOUSE_PHRASES = SCENES + ["dim everything"]  # scene names + triggers


def ctx(screen, variant=None):
    t = list(CAMS) if variant != "onecam" else CAMS[:1]
    if screen == "devices":
        t += [{"id": i, "label": n, "kind": "device"} for i, n in DEVS]
    elif screen == "scenes":
        t += [{"id": n, "label": n, "kind": "scene"} for n in SCENES]
    elif screen == "automations":
        t += [{"id": i, "label": n, "kind": "automation"} for i, n in AUTOS]
    elif screen == "notifications":
        t += [{"id": i, "label": n, "kind": "notice"} for i, n in NOTICES]
    return {"screen": screen, "targets": t, "actions": list(ACTIONS)}


N = None
# (screen, utterance, expected)
# expected: None | ("open", screen) | ("back",) | ("scroll", dir) | ("select", id) | ("menu", aid)
CORPUS = [
    # ---- screens
    ("home", "go to settings", ("open", "settings")),
    ("home", "Go to settings.", ("open", "settings")),
    ("home", "go to setting", ("open", "settings")),
    ("home", "open settings", ("open", "settings")),
    ("home", "open the settings", ("open", "settings")),
    ("home", "open sittings", ("open", "settings")),
    ("home", "settings", ("open", "settings")),
    ("home", "settings please", ("open", "settings")),
    ("home", "please open settings", ("open", "settings")),
    ("home", "can you open the settings", ("open", "settings")),
    ("home", "take me to settings", ("open", "settings")),
    ("home", "open the devices", ("open", "devices")),
    ("home", "open devices", ("open", "devices")),
    ("home", "open device", ("open", "devices")),
    ("home", "show my devices", ("open", "devices")),
    ("home", "show me my devices", ("open", "devices")),
    ("home", "devices", ("open", "devices")),
    ("home", "go to devices", ("open", "devices")),
    ("home", "go to the device list", ("open", "devices")),
    ("home", "show my automations", ("open", "automations")),
    ("home", "show my automation", ("open", "automations")),
    ("home", "open automations", ("open", "automations")),
    ("home", "automations", ("open", "automations")),
    ("home", "open the rules", ("open", "automations")),
    ("home", "open scenes", ("open", "scenes")),
    ("home", "show scenes", ("open", "scenes")),
    ("home", "scenes", ("open", "scenes")),
    ("home", "stats", ("open", "stats")),
    ("home", "statistics", ("open", "stats")),
    ("home", "readings", ("open", "stats")),
    ("home", "show the readings", ("open", "stats")),
    ("home", "open statistics", ("open", "stats")),
    ("home", "show me the sensor readings", ("open", "stats")),
    ("home", "notifications", ("open", "notifications")),
    ("home", "open notifications", ("open", "notifications")),
    ("home", "show notifications", ("open", "notifications")),
    ("home", "check notifications", ("open", "notifications")),
    ("home", "profile", ("open", "profile")),
    ("home", "open my profile", ("open", "profile")),
    ("home", "show my account", ("open", "profile")),
    ("devices", "go to the settings screen", ("open", "settings")),
    ("settings", "go back to devices", ("open", "devices")),
    ("scenes", "back to devices", ("open", "devices")),
    ("devices", "okay open scenes", ("open", "scenes")),
    ("devices", "hey home open scenes", ("open", "scenes")),
    ("devices", "open scenes now", ("open", "scenes")),
    ("devices", "open scenes thanks", ("open", "scenes")),
    ("devices", "switch to scenes", ("open", "scenes")),
    ("devices", "navigate to automations", ("open", "automations")),
    ("devices", "bring up the notifications", ("open", "notifications")),
    # ---- home / back
    ("devices", "home", ("open", "home")),
    ("devices", "go home", ("open", "home")),
    ("devices", "Go home.", ("open", "home")),
    ("devices", "take me home", ("open", "home")),
    ("devices", "home screen", ("open", "home")),
    ("devices", "go to the home screen", ("open", "home")),
    ("devices", "back to home", ("open", "home")),
    ("devices", "main screen", ("open", "home")),
    ("devices", "back", ("back",)),
    ("devices", "go back", ("back",)),
    ("devices", "Go back.", ("back",)),
    ("devices", "go back please", ("back",)),
    ("devices", "please go back", ("back",)),
    ("devices", "can you go back", ("back",)),
    ("devices", "take me back", ("back",)),
    ("devices", "previous screen", ("back",)),
    ("devices", "okay go back", ("back",)),
    # ---- scroll
    ("devices", "scroll down", ("scroll", "down")),
    ("devices", "Scroll down.", ("scroll", "down")),
    ("devices", "scroll up", ("scroll", "up")),
    ("devices", "scroll to the top", ("scroll", "top")),
    ("devices", "scroll to the bottom", ("scroll", "bottom")),
    ("devices", "scroll down please", ("scroll", "down")),
    ("devices", "please scroll down", ("scroll", "down")),
    ("devices", "scroll down a bit", ("scroll", "down")),
    ("devices", "scroll down a little", ("scroll", "down")),
    ("devices", "scroll up a bit", ("scroll", "up")),
    ("devices", "scroll down more", ("scroll", "down")),
    ("devices", "page down", ("scroll", "down")),
    ("devices", "page up", ("scroll", "up")),
    ("devices", "go to the top", ("scroll", "top")),
    ("devices", "go to the bottom", ("scroll", "bottom")),
    ("devices", "scrolled down", ("scroll", "down")),
    ("devices", "scroll dawn", ("scroll", "down")),
    ("devices", "school down", ("scroll", "down")),
    ("devices", "next page", ("scroll", "down")),
    ("notifications", "scroll down", ("scroll", "down")),
    ("home", "scroll", N),
    # ---- select on devices
    ("devices", "select kitchen light", ("select", "dev1")),
    ("devices", "select the kitchen light", ("select", "dev1")),
    ("devices", "pick porch light", ("select", "dev7")),
    ("devices", "tap on the bedroom fan", ("select", "dev3")),
    ("devices", "details of the garage door", ("select", "dev4")),
    ("devices", "select kitchen", ("select", "dev1")),
    ("devices", "select living room lamp", ("select", "dev2")),
    ("devices", "select the office", ("select", "dev9")),
    ("devices", "select front door lock", ("select", "dev5")),
    ("devices", "open kitchen light", N),
    ("devices", "open the kitchen light", N),
    ("devices", "show me the kitchen light", N),
    ("devices", "open the garage door", N),
    ("devices", "open garage door", N),
    ("devices", "open garage", N),
    ("devices", "view the tv", N),
    ("devices", "press the garden button", N),
    ("devices", "turn on the kitchen light", N),
    ("devices", "kitchen light", N),
    ("devices", "select light", N),  # two lights: ambiguous
    # ---- cameras
    ("home", "show me the front door", ("select", "cam1")),
    ("home", "show the front door camera", ("select", "cam1")),
    ("home", "open camera front door", ("select", "cam1")),
    ("home", "open the front door camera", ("select", "cam1")),
    ("home", "view the backyard camera", ("select", "cam2")),
    ("home", "show me the backyard", ("select", "cam2")),
    ("devices", "select front door", ("select", "cam1")),
    ("home", "open the front door", N),
    ("home", "open front door", N),
    ("devices", "open the front door", N),
    ("home", "unlock the front door", N),
    ("home", "is the front door locked", N),
    ("home", "show me the front door lock", N),
    # ---- scenes / automations / notices
    ("scenes", "select movie time", ("select", "movie time")),
    ("scenes", "edit good night", ("select", "good night")),
    ("scenes", "open movie time", ("select", "movie time")),
    ("scenes", "movie time", N),
    ("scenes", "good night", N),
    ("scenes", "start movie time", N),
    ("automations", "select porch light at sunset", ("select", "a1")),
    ("automations", "open motion night light", ("select", "a2")),
    ("automations", "edit the motion night light", ("select", "a2")),
    ("automations", "turn on the porch light", N),
    ("notifications", "select kitchen board was rewired", ("select", "n2")),
    ("notifications", "open the kitchen board was rewired", ("select", "n2")),
    # ---- actions
    ("home", "add a new device", ("menu", "add_device")),
    ("home", "add device", ("menu", "add_device")),
    ("home", "add a device", ("menu", "add_device")),
    ("devices", "add a device", ("menu", "add_device")),
    ("home", "I want to add a device", ("menu", "add_device")),
    ("home", "I'd like to add a new device", ("menu", "add_device")),
    ("home", "I’d like to add a new device", ("menu", "add_device")),
    ("home", "can you add a device please", ("menu", "add_device")),
    ("home", "new rule", ("menu", "add_automation")),
    ("home", "new automation", ("menu", "add_automation")),
    ("home", "create a new automation", ("menu", "add_automation")),
    ("home", "add an automation", ("menu", "add_automation")),
    ("home", "create a scene", ("menu", "add_scene")),
    ("home", "new scene", ("menu", "add_scene")),
    ("home", "delete a scene", ("menu", "remove_scene")),
    ("home", "remove a device", ("menu", "remove_device")),
    ("home", "edit a device", ("menu", "update_device")),
    ("home", "learn a remote", ("menu", "learn_ir")),
    ("home", "learn ir codes", ("menu", "learn_ir")),
    ("home", "add a new board", ("menu", "add_device")),
    ("home", "add devices", ("menu", "add_device")),
    # ---- house commands that must never navigate
    ("home", "turn on the kitchen light", N),
    ("home", "turn off the tv", N),
    ("home", "open the garage", N),
    ("home", "close the garage door", N),
    ("home", "set the heater to 20", N),
    ("home", "dim the living room lamp to 50%", N),
    ("home", "what's the temperature", N),
    ("home", "show me the temperature", N),
    ("home", "is the porch light on", N),
    ("home", "lock the front door", N),
    ("home", "movie time", N),
    ("home", "good night", N),
    ("home", "dim everything", N),
    ("home", "turn it up", N),
    ("home", "add ten percent to the lamp", N),
    ("home", "open the front", N),
    ("home", "start", N),
    ("home", "switch to kitchen light", N),
    ("home", "switch on the office", N),
    ("home", "open the office", N),
    ("home", "hey home", N),
    ("home", "okay home", N),
    ("home", "home assistant", N),
    ("home", "turn on home", N),
    ("devices", "turn the heater off", N),
    ("devices", "set bedroom fan to high", N),
    ("devices", "is the garage door open", N),
    ("devices", "back door", N),
    ("home", "leaving home", N),
    ("home", "down", N),
    ("home", "turn it down", N),
    ("home", "go down", N),
    ("home", "a little more", N),
    ("home", "go to sleep", N),
    ("home", "go to bed", N),
    ("home", "go back to sleep", N),
    ("home", "return to normal", N),
    ("home", "back up", N),
    ("home", "check the front door", N),
    ("home", "check the garage", N),
    ("home", "see the heater", N),
    ("home", "display the temperature", N),
    ("home", "launch movie time", N),
    ("home", "press the button", N),
    ("home", "open the living room", N),
    ("home", "show me the office", N),
    ("home", "go to kitchen", N),
    ("home", "turn on the tv and open settings", N),
    ("devices", "view kitchen light", N),
    ("devices", "check the kitchen light", N),
    ("home", "check the notifications please", ("open", "notifications")),
    ("home", "open the settings screen please", ("open", "settings")),
    ("home", "Settings.", ("open", "settings")),
    ("home", "Devices?", ("open", "devices")),
    ("home", "Open, devices.", ("open", "devices")),
    ("home", "open the automation screen", ("open", "automations")),
    ("home", "go to my scenes", ("open", "scenes")),
    ("home", "open the dashboard", ("open", "home")),
    ("devices", "open scene", ("open", "scenes")),
    ("home", "make a new rule", ("menu", "add_automation")),
    ("home", "set up a new device", ("menu", "add_device")),
    ("home", "please add a device", ("menu", "add_device")),
    ("home", "add a device now", ("menu", "add_device")),
    # ---- a scene the owner triggers with "home" / "alerts" (4th field: extra house phrases)
    ("devices", "home", N, ["home"]),
    ("devices", "go home", ("open", "home"), ["home"]),
    ("home", "alerts", N, ["alerts"]),
    ("home", "open alerts", ("open", "notifications"), ["alerts"]),
    # ---- single camera
    ("settings", "show the camera", ("select", "cam1"), None, "onecam"),
    ("settings", "open the camera", ("select", "cam1"), None, "onecam"),
]

# Contexts that need the scene/house phrase set wired (after only)
HOUSE_SENSITIVE = {("devices", "home")}


def summarize(r):
    if r is None:
        return None
    a = r.get("action")
    if a == "open":
        return ("open", r.get("screen"))
    if a == "back":
        return ("back",)
    if a == "scroll":
        return ("scroll", r.get("direction"))
    if a == "select":
        return ("select", r.get("target"))
    if a == "menu":
        return ("menu", r.get("menu"))
    return (a,)


def run(mod):
    sig = inspect.signature(mod.match_navigation)
    out = []
    for item in CORPUS:
        screen, text, exp = item[:3]
        extra = item[3] if len(item) > 3 and item[3] else []
        variant = item[4] if len(item) > 4 else None
        kw = {}
        if "house_phrases" in sig.parameters:
            kw["house_phrases"] = HOUSE_PHRASES + extra
        names = DEVICE_NAMES if variant != "onecam" else DEVICE_NAMES[:-1]
        r = mod.match_navigation(text, ctx(screen, variant), names, **kw)
        out.append(summarize(r))
    return out



if __name__ == "__main__":
    results = run(_Mod)
    wrong = [(item[:3], got) for item, got in zip(CORPUS, results, strict=True) if got != item[2]]
    for (screen, text, exp), got in wrong:
        print(f"  [FAIL] {screen}: {text!r} -> {got}, expected {exp}")
    print(f"\n  {len(CORPUS) - len(wrong)} passed, {len(wrong)} failed")
    sys.exit(1 if wrong else 0)

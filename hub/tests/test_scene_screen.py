"""THE SCENE SCREEN (2026-11-08).

One screen for a new scene and for changing one: its name (what you say),
who it is for, what it is for (tap "Ask the AI" and the screen comes back
with its suggestion, as device registration does), each device with its own
controls, Try it now and Save.
Back is a cancel. Clients without the screen get the old questions.

Run: python3 tests/test_scene_screen.py
"""
import asyncio
import os
import sys
import tempfile

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("HOMEVOICE_HOME", tempfile.mkdtemp())
from app.shell import InteractiveShell, CancelledByUser    # noqa: E402
import app.shell as shell_mod                              # noqa: E402
try:
    from house.scene_manager import SceneManager           # noqa: E402
except ImportError:                                         # the defense copy
    from nlu.scene_manager import SceneManager             # noqa: E402

PASSED = FAILED = 0


def check(name, ok, detail=""):
    global PASSED, FAILED
    PASSED, FAILED = PASSED + bool(ok), FAILED + (not ok)
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}   {'' if ok else detail}")


LOOP = asyncio.new_event_loop()


def run(coro):
    return LOOP.run_until_complete(coro)


def embed(text):
    """A stand-in embedder: a vector from the words (enough to see a
    re-embed happen)."""
    v = np.zeros(8, dtype=np.float32)
    for i, ch in enumerate(text.lower()):
        v[i % 8] += ord(ch) % 13 + 1
    return v


embedded = []


def doc_embed(text):
    embedded.append(text)
    return embed(text)


class DM:
    def __init__(self):
        self.devices = {
            "living.lamp": {"name": "living room light", "room": "living room",
                            "ha_entity_id": "light.living", "state_key": "level",
                            "range_min": 0, "range_max": 100},
            "tv.remote": {"name": "tv", "room": "living room",
                          "ir_codes": {"power": "x", "mute": "y"}},
            "porch.temp": {"name": "porch temp", "ha_entity_id": "sensor.porch"},
            "front.lock": {"name": "front door", "secure": True,
                           "ha_entity_id": "lock.front", "state_key": "on"},
        }
        self.colour = None

    is_colourable = staticmethod(lambda d: False)

    @staticmethod
    def is_readable_only(d):
        return str(d.get("ha_entity_id", "")).startswith("sensor.")

    def set_colour(self, did, rgb):
        self.colour = (did, rgb)
        return True

    def press(self, did):
        return True


class IO:
    """The phone: answers each scene_builder frame from a script."""

    def __init__(self, replies):
        self.replies = list(replies)
        self.frames, self.data, self.lines = [], [], []

    async def scene_builder(self, payload):
        self.frames.append(payload)
        if not self.replies:
            return {"cancelled": True}
        nxt = self.replies.pop(0)
        return nxt(payload) if callable(nxt) else nxt

    async def send_data(self, kind, payload):
        self.data.append((kind, payload))

    async def write(self, text, level="info"):
        self.lines.append(text)


sh = InteractiveShell.__new__(InteractiveShell)
sh.dm, sh.esp, sh.automations = DM(), None, None
sh.failure_note = None
sh.current_speaker_id = "bruce"
sh.scene_learned = None
sh.sm = SceneManager(doc_embed, scenes_path=os.path.join(tempfile.mkdtemp(),
                                                         "scenes.json"),
                     emb_dim=8)
sh._household = lambda: ["bruce", "ada"]
sets, pressed = [], []


async def set_state(did, **kw):
    sets.append((did, kw))
    return True


async def press_ir(did, text, slots):
    pressed.append((did, text))
    return True
sh._set_state, sh._press_ir = set_state, press_ir

MOVIE = {"name": "Movie night", "description": "", "private": False,
         "actions": [
             {"device": "living.lamp", "state": {"on": True, "level": 30}},
             {"device": "tv.remote", "state": {"press": "power"}}]}

print("\nwhat the screen is given")
sh._io = io = IO([{"action": "save", "scene": MOVIE}])
check("a new scene opens the screen and saves", run(sh.menu_add_scene()) is True)
f = io.frames[0]
check("...a new one, empty", not f["editing"] and f["scene"]["name"] == ""
      and f["scene"]["actions"] == [], f["scene"])
check("...each device with its own controls; sensors left out",
      {t["value"]: t["use"]["kind"] for t in f["targets"]} ==
      {"living.lamp": "dimmer", "tv.remote": "ir", "front.lock": "switch"},
      [(t["value"], t["use"]) for t in f["targets"]])
check("...'who it is for' is offered (two live here)", f["whom"] is True)
sc = sh.sm.get("movie night")
check("saved: its name is what you say", sc is not None and
      sc.trigger == "Movie night")
check("...each device does its part",
      [a.to_dict() for a in sc.actions] ==
      [{"device_id": "living.lamp", "state": {"on": True, "level": 30.0}},
       {"device_id": "tv.remote", "state": {"press": "power"}}],
      [a.to_dict() for a in sc.actions])
check("...nothing waited on the AI", io.data == [], io.data)

print("\nwhat it is for, and the AI")
asked = []
shell_mod._LLM_AVAILABLE = True
shell_mod.describe_scene = lambda name, hint, acts: (
    asked.append((name, hint, acts)) or "Dims the light for a film.")
draft = dict(MOVIE, name="Film time")
sh._io = io = IO([{"action": "generate", "scene": draft},
                  {"action": "save", "scene": dict(
                      draft, description="Dims the light for a film.")}])
check("asking the AI does not end the screen", run(sh.menu_add_scene()) is True)
check("...it was asked with the name and each part in words",
      asked and asked[0][0] == "Film time" and
      "living room light (on, 30)" in asked[0][2][0]["device_id"], asked)
check("...the screen came back with the suggestion, as registration does",
      len(io.frames) == 2 and io.frames[1]["scene"]["name"] == "Film time"
      and io.frames[1]["ai_text"] == "Dims the light for a film."
      and io.frames[1]["ai_for"] == "Film time", io.frames[-1:])
check("...beside the person's words, not over them",
      io.frames[1]["scene"]["description"] == "", io.frames[1]["scene"])
sh._io = io = IO([{"action": "generate", "scene": dict(draft, name="")},
                  {"cancelled": True}])
try:
    run(sh.menu_add_scene())
except CancelledByUser:
    pass
check("no name yet: it says so, and asks nothing",
      "name first" in io.frames[1]["ai_error"] and len(asked) == 1,
      io.frames[1]["ai_error"])
sc = sh.sm.get("film time")
check("the description is kept with the scene",
      sc.description == "Dims the light for a film.", sc.description)
check("...and what is embedded is led by the name",
      embedded[-1] == "Film time. Dims the light for a film.", embedded[-1:])

print("\ntry it now")
sets.clear(), pressed.clear()
lock_too = dict(MOVIE, name="Bedtime", actions=MOVIE["actions"] + [
    {"device": "front.lock", "state": {"on": True}}])
sh._io = io = IO([{"action": "try", "scene": lock_too},
                  {"cancelled": True}])
try:
    run(sh.menu_add_scene())
    cancelled = False
except CancelledByUser:
    cancelled = True
said = io.frames[1]["tried"]
check("each part ran once", sets == [("living.lamp", {"on": True,
                                                       "level": 30.0})]
      and pressed == [("tv.remote", "power")], (sets, pressed))
check("...and said what happened, in words",
      "living room light: on, 30" in said and "tv: press power" in said, said)
check("...a secure device is not moved from here, and it says so",
      any("secure" in x for x in said) and
      not any(d == "front.lock" for d, _ in sets), said)
check("Back is a cancel, and nothing was saved",
      cancelled and sh.sm.get("bedtime") is None)

print("\nwhat cannot be saved keeps the screen open")
sh._io = io = IO([{"action": "save", "scene": dict(MOVIE, name="  ")},
                  {"action": "save", "scene": dict(MOVIE, actions=[])},
                  {"action": "save", "scene": dict(MOVIE, actions=[
                      {"device": "living.lamp", "state": {}}])},
                  {"action": "save", "scene": MOVIE},
                  {"cancelled": True}])
try:
    run(sh.menu_add_scene())
except CancelledByUser:
    pass
errors = [fr["error"] for fr in io.frames[1:]]
check("no name", "name" in errors[0], errors)
check("no device", "at least one device" in errors[1], errors)
check("a device with nothing chosen", "living room light" in errors[2], errors)
check("a name already used", "already" in errors[3].lower(), errors)

print("\nchanging a scene: the same screen, filled in")
before = sh.sm.get("movie night").trigger_embedding.copy()
sh._io = io = IO([lambda fr: {"action": "save", "scene": dict(
    fr["scene"], actions=[{"device": "living.lamp",
                           "state": {"on": True, "level": 60}}])}])
check("tap a scene: it opens filled in, and saves",
      run(sh.menu_edit_scene("movie night")) is True)
f = io.frames[0]
check("...filled in", f["editing"] and f["scene"]["name"] == "Movie night" and
      [a["words"] for a in f["scene"]["actions"]] == ["on, 30", "press power"],
      f["scene"])
sc = sh.sm.get("movie night")
check("...the change is kept", [a.to_dict()["state"] for a in sc.actions] ==
      [{"on": True, "level": 60.0}])
check("...and how it is matched is untouched (same name, same words)",
      np.array_equal(before, sc.trigger_embedding))
sh._io = io = IO([lambda fr: {"action": "save", "scene": dict(
    fr["scene"], name="Cinema")}])
run(sh.menu_edit_scene("movie night"))
check("renamed: it is said by its new name",
      sh.sm.get("cinema") is not None and sh.sm.get("movie night") is None
      and sh.sm.get("cinema").trigger == "Cinema")

print("\nwho it is for")
sh._io = io = IO([{"action": "save", "scene": dict(MOVIE, name="Mine",
                                                    private=True)}])
run(sh.menu_add_scene())
check("Just me", sh.sm.get("mine", user="bruce").private is True)
sh.current_speaker_id = "ada"
sh.sm.register("shared", "shared", [{"device_id": "living.lamp",
                                     "state": {"on": False}}], by="bruce")


async def ok_pin(*a, **k):
    return True
sh._may_change = ok_pin
sh._tell_owner = lambda *a: None
sh._io = io = IO([lambda fr: {"action": "save", "scene": dict(
    fr["scene"], private=True)}])
run(sh.menu_edit_scene("shared"))
check("on someone else's scene it is not offered, and not changed",
      io.frames[0]["whom"] is False and
      sh.sm.get("shared").private is False)
sh.current_speaker_id = "bruce"

print("\nrunning a scene: every kind of part")
sets.clear(), pressed.clear()
sh._io = IO([])
ok = run(sh._fire_scene({"name": "x", "actions": [
    {"device_id": "living.lamp", "state": {"on": True, "level": 50,
                                           "colour": [255, 0, 0]}},
    {"device_id": "tv.remote", "state": {"press": "mute"}}]}))
check("a level and a colour", ok and sets[-1] == ("living.lamp",
                                                  {"on": True, "level": 50})
      and sh.dm.colour == ("living.lamp", (255, 0, 0)), (sets, sh.dm.colour))
check("an IR button", pressed == [("tv.remote", "mute")], pressed)
check("the list says each part in words, never a raw state",
      [a["reading"] for r in sh._scene_records() if r["name"] == "film time"
       for a in r["actions"]] == ["on, 30", "press power"])

print("\na phone without the screen gets the questions")
sh._io = io = IO([])
io.scene_builder = lambda payload: asyncio.sleep(0, result=None)
check("None from the client: no screen", run(sh._scene_screen(None)) is None)

print(f"\n{PASSED} passed, {FAILED} failed")
sys.exit(1 if FAILED else 0)

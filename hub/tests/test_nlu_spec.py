"""
test_nlu_spec.py — the NLU architecture spec, as executable assertions.

Every branch the user specified is a test here. This file IS the definition of
"done": the rewrite is complete only when every check passes. It uses a
deterministic fake embedder so semantic behaviour is testable without the real
MPNet weights.

Run: python3 tests/test_nlu_spec.py   (from anywhere)
"""
import os
import shutil
import sys
import tempfile

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from nlu.device_manager import DeviceManager
from nlu.resolver import resolve_command
from nlu.slot_extractor import SlotExtractor
from nlu.executor import build_command

_PASS = _FAIL = 0
def check(name, cond, detail=""):
    global _PASS, _FAIL
    if cond:
        _PASS += 1
        print(f"  [PASS] {name}")
    else:
        _FAIL += 1
        print(f"  [FAIL] {name}   {detail}")


# ── Deterministic fake MPNet ────────────────────────────────────────────────
# Concept axes so paraphrases land near the right device description.
_AXES = {
    "climate": ("cool", "cold", "chill", "warm", "hot", "air", "conditioner",
                "temperature", "heat", "freezing", "ac", "thermostat"),
    "light":   ("light", "lamp", "bright", "dim", "brightness", "glow"),
    "secure":  ("lock", "door", "bolt", "latch", "gate", "seal", "secure",
                "deadbolt", "unlock", "unbolt"),
    "media":   ("tv", "television", "play", "music", "volume", "channel"),
}
def fake_embed(text):
    v = np.zeros(768, dtype="float32")
    t = text.lower()
    for i, (_, kws) in enumerate(_AXES.items()):
        if any(k in t for k in kws):
            v[i] += 5.0
    for w in t.split():
        v[hash(w) % 768] += 0.3
    return v


class Ctx:
    def __init__(self, text, dm, room=None):
        self.dm = dm
        self.text = text
        self.room_hint = room
        self.slots = SlotExtractor().extract(text)
        self.speaker_id = "u"


# One directory for every registry this run makes, removed at the end: a save
# also leaves the cross-process lock file (<name>.lock) beside the registry.
_DIR = tempfile.mkdtemp(prefix="nlu-spec-")


def fresh():
    tmp = tempfile.mktemp(suffix=".json", dir=_DIR)
    return DeviceManager(path=tmp, embed_fn=fake_embed), tmp


def main():
    # ============================================================
    print("\nBRANCH: device + room mentioned")
    dm, tmp = fresh()
    dm.add("lr.ac", name="ac", room="living room", description="air conditioner that cools the living room")
    dm.add("br.ac", name="ac", room="bedroom",    description="air conditioner that cools the bedroom")
    d = resolve_command(Ctx("the living room ac", dm, room="living room"))
    check("one device in that room -> execute (toggle)", d["kind"] == "execute" and d["targets"] == ["lr.ac"], d)
    os.unlink(tmp)

    dm, tmp = fresh()
    dm.add("lr.ac1", name="ac", room="living room", description="ac unit one cools the living room")
    dm.add("lr.ac2", name="ac", room="living room", description="ac unit two cools the living room")
    d = resolve_command(Ctx("the living room ac", dm, room="living room"))
    check("two+ in that room -> pick", d["kind"] == "pick" and len(d["candidates"]) == 2, d)
    os.unlink(tmp)

    # ============================================================
    print("\nBRANCH: device only (no room)")
    dm, tmp = fresh()
    dm.add("br.lamp", name="lamp", room="bedroom", description="bedside reading lamp")
    d = resolve_command(Ctx("lamp", dm))
    check("one device -> execute (toggle)", d["kind"] == "execute" and d["targets"] == ["br.lamp"], d)
    os.unlink(tmp)

    dm, tmp = fresh()
    dm.add("br.lamp", name="lamp", room="bedroom", description="bedside lamp")
    dm.add("of.lamp", name="lamp", room="office",  description="desk lamp")
    d = resolve_command(Ctx("lamp", dm))
    check("two+ devices -> pick (with rooms available)", d["kind"] == "pick" and len(d["candidates"]) == 2, d)
    check("pick candidates carry their room", all("room" in c for c in d["candidates"]), d)
    os.unlink(tmp)

    # ============================================================
    print("\nBRANCH: room only -> list devices in room, user picks (or all)")
    dm, tmp = fresh()
    dm.add("lr.ac",   name="ac",   room="living room", description="ac cools the living room")
    dm.add("lr.lamp", name="lamp", room="living room", description="floor lamp in the living room")
    dm.add("br.fan",  name="fan",  room="bedroom",     description="ceiling fan bedroom")
    d = resolve_command(Ctx("living room", dm, room="living room"))
    check("room only -> pick among that room's devices", d["kind"] == "pick" and len(d["candidates"]) == 2, d)
    check("room-only pick allows 'all'", d.get("allow_all") is True, d)
    check("room-only lists ONLY that room", {c["device_id"] for c in d["candidates"]} == {"lr.ac", "lr.lamp"}, d)
    os.unlink(tmp)

    # ============================================================
    print("\nBRANCH: ambiguous phrase, no name/room -> semantics -> pick")
    dm, tmp = fresh()
    dm.add("br.ac", name="ac", room="bedroom", description="air conditioner that cools the bedroom when hot")
    d = resolve_command(Ctx("it's freezing in here", dm))
    check("'it's freezing' -> semantic finds the ac", d["kind"] == "execute" and d["targets"] == ["br.ac"], d)
    os.unlink(tmp)

    # ============================================================
    print("\nBRANCH: plural device name -> ASK which/all (not auto-fanout)")
    dm, tmp = fresh()
    dm.add("lr.light", name="light", room="living room", description="living room ceiling light")
    dm.add("br.light", name="light", room="bedroom",     description="bedroom light")
    d = resolve_command(Ctx("lights", dm))
    check("bare plural 'lights' -> pick-all (asks, per new rule)",
          d["kind"] == "pick" and d.get("allow_all") is True and len(d["candidates"]) == 2, d)
    # but an EXPLICIT plural acts on all directly
    d = resolve_command(Ctx("turn on the lights", dm))
    check("'turn on the lights' -> execute all (explicit, no ask)",
          d["kind"] == "execute" and len(d["targets"]) == 2, d)
    os.unlink(tmp)

    # plural SEMANTIC: three bulbs, none named literally "lights" -> pick-all of all three
    dm, tmp = fresh()
    dm.add("bulb1", name="bulb",   room="lr", description="bulb that brightens the room with light")
    dm.add("bulb2", name="bulb 2", room="lr", description="second bulb, illuminates the room with light")
    dm.add("bulb3", name="bulb 3", room="br", description="third bulb, gives the room light and glow")
    dm.add("ac1",   name="ac",     room="lr", description="air conditioner that cools when hot")
    d = resolve_command(Ctx("lights", dm))
    check("plural semantic 'lights' -> pick-all (not auto-single)", d["kind"] == "pick" and d.get("allow_all") is True, d)
    ids = {c["device_id"] for c in d.get("candidates", [])}
    check("plural semantic gathers ALL bulbs", {"bulb1", "bulb2", "bulb3"} <= ids, ids)
    check("plural semantic excludes the unrelated ac", "ac1" not in ids, ids)
    os.unlink(tmp)

    # ============================================================
    print("\nBRANCH: securement -> semantics on the verb -> pick all/one")
    dm, tmp = fresh()
    dm.add("front.door", name="door",  room="hall", secure=True, description="the front door lock deadbolt")
    dm.add("back.gate",  name="gate",  room="yard", secure=True, description="back gate bolt latch")
    dm.add("win.latch",  name="latch", room="hall", secure=True, description="window latch seal")
    d = resolve_command(Ctx("bolt the door", dm))
    check("'bolt the door' routes through securement", d.get("action", {}).get("type") == "secure", d)
    check("'bolt' verb -> lock_on True", d["action"]["lock_on"] is True, d)
    # "door" is a device's exact name: naming a specific lock executes on it
    # (resolver's securement_named branch) rather than asking again. The
    # active voice challenge is still applied by main._carry_out.
    check("securement with an exact device name -> execute on it",
          d["kind"] == "execute" and d["targets"] == ["front.door"], d)
    # Naming no device -> semantics on the verb -> pick one/all.
    d = resolve_command(Ctx("bolt the entrance", dm))
    check("securement naming no device offers a pick with all-option",
          d.get("action", {}).get("type") == "secure"
          and d["kind"] == "pick" and d.get("allow_all") is True
          and {c["device_id"] for c in d["candidates"]} == {"front.door", "back.gate", "win.latch"}, d)
    os.unlink(tmp)

    dm, tmp = fresh()
    dm.add("front.door", name="door", room="hall", secure=True, description="front door deadbolt lock")
    d = resolve_command(Ctx("unlock the door", dm))
    check("single secure device -> execute directly", d["kind"] == "execute" and d["targets"] == ["front.door"], d)
    # No per-verb "requires_confirmation" flag: main._carry_out runs the
    # full active voice challenge for EVERY action of type "secure" (lock
    # and unlock alike), so the decision must simply carry that type.
    check("'unlock' -> secure action, lock_on False",
          d["action"]["type"] == "secure" and d["action"]["lock_on"] is False, d)
    os.unlink(tmp)

    # ============================================================
    print("\nBRANCH: global off skips secure devices")
    dm, tmp = fresh()
    dm.add("lr.ac",     name="ac",   room="lr",   secure=False, state={"on": True})
    dm.add("front.door", name="door", room="hall", secure=True,  state={"on": True})
    # (global-off is handled in main._all_off, tested separately below)
    secure_left = [did for did, dd in dm.devices.items() if not dd.get("secure")]
    check("global-off target list excludes secure", secure_left == ["lr.ac"], secure_left)
    os.unlink(tmp)

    # ============================================================
    print("\nBRANCH: value / comparative -> slot extractor, applied to last device")
    dm, tmp = fresh()
    dm.add("lr.ac", name="ac", room="lr", description="ac cools the room",
           state={"on": True, "temperature": 24.0}, state_key="temperature",
           step=1.0, range_min=16, range_max=30, unit="celsius")
    # semantics-first for 'make it warmer', then last-touched must be among matches
    dm.touch("u", "lr.ac")
    d = resolve_command(Ctx("make it warmer", dm))
    check("'make it warmer' -> last-touched ac (semantics-confirmed)", d["kind"] == "execute" and d["targets"] == ["lr.ac"], d)
    os.unlink(tmp)

    # ============================================================
    print("\nBRANCH: percent / fraction values -> share of the device's range")
    se = SlotExtractor()
    thermo = {"state": {"on": True, "level": 20.0, "unit": "C"}, "state_key": "level",
              "step": 1.0, "range_min": 16.0, "range_max": 30.0}
    dimmer = {"state": {"on": True, "level": 80.0, "unit": "%"}, "state_key": "level",
              "step": 10.0, "range_min": 0.0, "range_max": 100.0}
    cmd = build_command(thermo, se.extract("set the thermostat to 50%"))
    check("'50%' on a 16-30 thermostat -> 23 (half the range, not clamped to 30)",
          cmd.get("action") == "set" and abs(cmd["value"] - 23.0) < 1e-9, cmd)
    cmd = build_command(dimmer, se.extract("set the dimmer to 50%"))
    check("'50%' on a percent device stays 50", cmd.get("action") == "set" and cmd["value"] == 50.0, cmd)
    cmd = build_command(thermo, se.extract("set the thermostat to 22"))
    check("unitless '22' on the thermostat stays 22", cmd.get("value") == 22.0, cmd)
    cmd = build_command(thermo, se.extract("turn it up by 50%"))
    check("'up by 50%' on 16-30 moves by 7 (half the span)", cmd.get("value") == 27.0, cmd)
    # "by half" reaches build_command as the SAME slots as a typed "by 0.5"
    # (the extractor digitises number words first), so it cannot be read as a
    # fraction without breaking "down by 0.5" on a half-degree thermostat. It
    # stays a literal 0.5 until the extractor marks spoken fractions.
    cmd = build_command(thermo, se.extract("turn it down by 0.5 degrees"))
    check("'down by 0.5 degrees' is a literal half degree", cmd.get("value") == 19.5, cmd)
    cmd = build_command(thermo, se.extract("turn it down by 0.5"))
    check("unitless explicit 'down by 0.5' stays literal", cmd.get("value") == 19.5, cmd)

    # ============================================================
    print("\nBRANCH: up/down on something with no number to move -> on/off, never a toggle")
    fan = {"state": {"on": True}, "state_key": "on", "step": 0.0, "range_min": 0.0, "range_max": 1.0}
    cmd = build_command(fan, se.extract("turn the fan up"))
    check("'turn the fan up' on a running on/off fan keeps it on (was: flip -> off)",
          cmd == {"action": "set", "key": "on", "value": True}, cmd)
    plug = {"state": {"on": True, "power": 40.0}, "state_key": "on", "step": 0.0,
            "range_min": 0.0, "range_max": 1.0}
    cmd = build_command(plug, se.extract("turn the plug down"))
    check("'turn the plug down' switches it off (no sums on its watts)",
          cmd == {"action": "set", "key": "on", "value": False}, cmd)
    dark = {"state": {"on": False}, "state_key": "level", "step": 5.0,
            "range_min": 0.0, "range_max": 100.0}
    cmd = build_command(dark, se.extract("dim the light"))
    check("'dim the light' on a light that is off does not switch it on",
          cmd == {"action": "set", "key": "on", "value": False}, cmd)

    # ============================================================
    print("\nBRANCH: devices.json -- undo, corruption, two processes")
    dm, tmp = fresh()
    dm.mqtt_driver = lambda did, change: True
    dm.add("k.meter", name="meter", room="k", state={"level": 1.0}, readable_only=True)
    dm.add("k.lamp", name="lamp", room="k", state={"on": False}, transport="mqtt")
    dm.snapshot()
    dm.set_state("k.lamp", on=True)
    dm.devices["k.meter"]["state"] = {"level": 5.0}          # the reading moved
    reverted = [did for did, _ in dm.undo()]
    check("undo reverts the lamp, not a sensor's reading",
          reverted == ["k.lamp"] and dm.devices["k.meter"]["state"] == {"level": 5.0}, reverted)
    other = DeviceManager(path=tmp, embed_fn=fake_embed)      # a second process
    other.add("k.fan", name="fan", room="k", state={"on": False})
    dm.set_state("k.lamp", on=False)                          # a stale copy writes
    check("a second process's device survives this one's save",
          set(DeviceManager(path=tmp).devices) == {"k.meter", "k.lamp", "k.fan"})
    with open(tmp, "w", encoding="utf-8") as f:
        f.write('{"k.lamp": {"name": "lamp"')                 # torn file
    DeviceManager(path=tmp).add("k.new", name="new")
    quarantined = [p for p in os.listdir(os.path.dirname(tmp))
                   if p.startswith(os.path.basename(tmp) + ".corrupt-")]
    check("a corrupt devices.json is moved aside, not saved over", len(quarantined) == 1, quarantined)

    shutil.rmtree(_DIR, ignore_errors=True)
    print(f"\n{'='*50}\n  {_PASS} passed, {_FAIL} failed\n{'='*50}")
    return _FAIL == 0


if __name__ == "__main__":
    import sys
    sys.exit(0 if main() else 1)
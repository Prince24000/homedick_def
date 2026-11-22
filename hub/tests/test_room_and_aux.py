"""TWO FIXES (2026-10-21).

1. A verb after its own modal or negated auxiliary is one sentence:
   "you can't talk now" is never cut into "you can't" | "talk now".
2. A room named in a command: when the house has no such room, the person is
   told so ("There's no room called garage."); when the room exists but has
   nothing like that, they are told that ("There is no heater in the
   kitchen."). "in red", "in turbo", "in my room", "in the oven" are not rooms.

Run: python3 tests/test_room_and_aux.py
"""
import logging
import os
import re
import sys
import tempfile
import zlib

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
logging.disable(50)
import numpy as np                                           # noqa: E402
from nlu.command_splitter import split_commands               # noqa: E402

passed = failed = 0


def check(label, ok, got=None):
    global passed, failed
    if ok:
        passed += 1
    else:
        failed += 1
        print(f"FAIL {label}: {got!r}")


# ── 1. auxiliaries ──
for said, want in [
        ("you can't talk now", ["you can't talk now"]),
        ("i can't hear you", ["i can't hear you"]),
        ("you cannot talk now", ["you can not talk now"]),
        ("i won't stop", ["i won't stop"]),
        ("you can talk now", ["you can talk now"]),
        ("can you turn on the light", ["can you turn on the light"]),
        ("turn on the fan and lock the door", ["turn on the fan", "lock the door"])]:
    got = split_commands(said)
    check(said, got == want, got)


# ── 2. rooms ──
from app.shell import InteractiveShell                        # noqa: E402
from nlu.device_manager import DeviceManager                  # noqa: E402
from nlu.resolver import resolve_command                      # noqa: E402
from nlu.slot_extractor import SlotExtractor                  # noqa: E402


def embed(text, *a, **k):
    v = np.zeros(256, dtype="float32")
    for w in re.findall(r"[a-z0-9]+", str(text).lower()):
        v[zlib.crc32(w.encode()) % 256] += 1.0
    return v


dm = DeviceManager(path=os.path.join(tempfile.mkdtemp(), "d.json"), embed_fn=embed)
for did, name, room in [("kitchen.bulb", "bulb", "kitchen"),
                        ("kitchen.oven", "oven", "kitchen"),
                        ("bedroom.heater", "heater", "bedroom"),
                        ("living_room.light", "light", "living room"),
                        ("living_room.fan", "fan", "living room")]:
    dm.add(did, name=name, room=room, description=name, state_key="on",
           state={"on": False})
sh = InteractiveShell.__new__(InteractiveShell)
sh.dm = dm
sh.extractor = SlotExtractor()

for said, want in [("turn on the fan in the garage", "garage"),
                   ("turn on the fan in the kitchen", "kitchen"),
                   ("turn on the light in red", None),
                   ("turn the fan on in turbo", None),
                   ("turn on the lamp in my room", None),
                   ("turn on the light in the oven", None)]:
    got = sh._extract_room(said)
    check(f"room of {said!r}", got == want, got)

for said, want in [
        ("turn on the heater in the kitchen", "There is no heater in the kitchen."),
        ("turn on the fan in the garage", "There's no room called garage.")]:
    ctx = sh._make_ctx(said, sh._extract_room(said), sh.extractor.extract(said), None)
    dec = resolve_command(ctx)
    msg = str(dec.get("message") or "")
    check(f"{said!r} says {want!r}", want in msg, (dec.get("kind"), msg))

said = "turn on the oven in the kitchen"
dec = resolve_command(sh._make_ctx(said, sh._extract_room(said),
                                   sh.extractor.extract(said), None))
check("a device in its room still works",
      dec.get("kind") == "execute" and dec.get("targets") == ["kitchen.oven"], dec)

print(f"{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)

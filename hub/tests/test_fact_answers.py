"""Questions whose answer is a fact: time and date (the PHONE's clock), the
rooms themselves, and one room's devices -- answered at once, in the existing
query path, from word shape alone (no encoder, no resolver).

Run: python3 tests/test_fact_answers.py   (no hardware, no network)
"""
import asyncio
import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("HOMEVOICE_HOME", tempfile.mkdtemp())
os.environ.setdefault("HOMEVOICE_ENV_FILE", "")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import test_nlu_spec as T                                   # noqa: E402
from nlu.lexicon import clock_question, room_only_query, is_rooms_question  # noqa: E402
from app.shell import InteractiveShell                      # noqa: E402
from app.ws_server import _phone_clock                      # noqa: E402

PASSED = FAILED = 0


def check(name, ok, detail=""):
    global PASSED, FAILED
    PASSED, FAILED = PASSED + bool(ok), FAILED + (not ok)
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}   {'' if ok else detail}")


print("\ntime and date: word shape")
for t, want in [("what is the time", "time"), ("Hello Nova, what is the time?", "time"),
                ("what time is it", "time"), ("do you have the time", "time"),
                ("what's the date today", "date"), ("what day is it", "date"),
                ("what's today's date", "date"), ("tell me the date", "date"),
                ("what is the time and date", "both"),
                ("what time and date is it", "both"),
                ("And the time is what?", "time"), ("and the date is what", "date"),
                ("hey nova can you tell me the time please", "time"),
                ("what month is it", "date"), ("what year is it", "date"),
                ("hey nova what time is it right now", "time"),
                ("could you tell me what the date is today", "date"),
                ("time", "time"), ("Time", "time"),
                ("the date", "date"), ("today's date", "date"), ("day", "date"),
                ("time and date", "both")]:
    check(f"{t!r} -> {want}", clock_question(t) == want, clock_question(t))
for t in ["what time does the heater turn off", "what is the time zone",
          "set a timer for ten minutes", "set the time", "turn off the lights what time is it", "how was your day",
          "what time is sunset", "turn on movie time", "is the kitchen on",
          "what is the temperature", "what time the heater on",
          "turn the lights on every day", "what day is tomorrow",
          "wake me at 7 am", "have a good day", "what time was it yesterday",
          "set the time", "what time did the door open"]:
    check(f"not a clock question: {t!r}", clock_question(t) is None)

print("\nthe loose tier: only for what the house understood nothing of")
for t, want in [("Always the time.", "time"), ("so what is uh the time again my friend", "time"),
                ("the time is now what please thanks", "time"),
                ("tell me the date today my friend", "date")]:
    check(f"strict says no, loose says {want}: {t!r}",
          clock_question(t) is None and clock_question(t, loose=True) == want,
          (clock_question(t), clock_question(t, loose=True)))

# The loose tier needs no list: a stranger touching the clock word, or a
# question that opened as one, is not for it.
for t in ["good day", "happy new year", "every day", "time zone", "next year",
          "nice day", "time until sunset", "yesterdays date", "what time does the heater go off",
          "what day is tomorrow", "turn it on every day", "date night",
          "the time zone", "any day now", "sunset time", "alarm time", "every time"]:
    check(f"loose never takes: {t!r}", clock_question(t, loose=True) is None,
          clock_question(t, loose=True))

# A made-up house: whatever is said, the STRICT tier must never take it.
# Unknown words block by default, so none of these needed to be foreseen.
HOUSE = ["turn the heater on at the same time as the lights",
         "turn off the lights in ten minutes", "what time is the heater scheduled",
         "dim the lights at bedtime", "is the kitchen light on today",
         "what is the temperature right now", "what is the date on the milk",
         "i need the day off", "the day is bright turn on the lamp",
         "how long has the fan been on", "show me the time line of the heater",
         "set the time on the oven", "which day does the heater come on",
         "what is the time of the next alarm", "what day is the bin collection",
         "play something nice any time", "open the garage the moment i arrive",
         "year round heating off", "what time does it get dark",
         "the heater time please", "turn on the time machine",
         "lights on all day", "heater on for the day", "what is the day rate",
         "is it the time to turn the heater on", "when is the heater on",
         "the time the door opened", "month end report", "tell me the day the fan ran",
         "what time is movie night", "date night mode on", "good day to you",
         "any good time to heat the house", "turn the fan off for the day"]
for t in HOUSE:
    check(f"strict never takes: {t!r}", clock_question(t) is None, clock_question(t))

ROOMS = {"kitchen", "living room", "bedroom"}
print("\nthe splitter keeps one question in one piece")
from nlu.command_splitter import split_commands                  # noqa: E402
for t, want in [("and the time is what", ["the time is what"]),
                ("the heater is on", ["the heater is on"]),
                ("the front door is open", ["the front door is open"]),
                ("it is on", ["it is on"]),
                ("what time and date is it", ["what time and date is it"]),
                ("turn on the heater and the time is what",
                 ["turn on the heater", "the time is what"]),
                ("turn on the light and is the door open",
                 ["turn on the light", "is the door open"]),
                ("is the door locked and is the window open",
                 ["is the door locked", "is the window open"]),
                ("make sure the door is locked", ["make sure the door is locked"]),
                ("let me see what is in layout", ["let me see what is in layout"]),
                ("tell me what is on", ["tell me what is on"]),
                ("turn on the heater and let me see what is on",
                 ["turn on the heater", "let me see what is on"]),
                # real misses, 2026-10-03: a trailing auxiliary, and a
                # question word with an auxiliary the pair table did not list
                ("List the devices we have.", ["list the devices we have"]),
                ("tell me the devices I have", ["tell me the devices i have"]),
                ("list the devices we have and turn on the heater",
                 ["list the devices we have", "turn on the heater"]),
                ("what do I have", ["what do i have"]),
                ("what have I got", ["what have i got"]),
                ("what is the time and what is the date",
                 ["what is the time", "what is the date"])]:
    got = split_commands(t, known_devices={"heater", "front door"})
    check(f"{t!r} -> {want}", got == want, got)

check("a house name that holds a clock word is the house's",
      clock_question("what is movie time", ["movie time"]) is None)

print("\none room, named")
for t in ["how is the kitchen", "what's in the living room", "kitchen status",
          "status of the kitchen", "what is the state of the bedroom"]:
    check(f"room question: {t!r}", room_only_query(t, ROOMS) is not None)
for t in ["is the kitchen light on", "why is the kitchen cold",
          "what is the temperature in the kitchen", "turn on the kitchen",
          "how is the house"]:
    check(f"left to the device path: {t!r}", room_only_query(t, ROOMS) is None)

print("\nthe rooms themselves")
for t in ["which room", "what rooms are there", "list the rooms",
          "what rooms do I have", "show me the rooms"]:
    check(f"asks for the rooms: {t!r}", is_rooms_question(t))
check("a named device is not that", not is_rooms_question("which room is the heater in"))

print("\nwhat the house has, by its frame and not by the noun")
from nlu.lexicon import is_inventory_question                    # noqa: E402
for t in ["what devices do I have", "what appliances do I have", "what tools do I have",
          "what is installed in the house", "what do I have", "which devices are connected",
          "what devices are there", "what devices do we have now", "what have I got",
          "whats installed", "how many devices do I have", "what gadgets are registered",
          "List the devices we have.", "list my devices", "show me the devices we have",
          "tell me what is installed",
          "what machines do we have", "which equipment is installed", "what stuff do I have",
          "which things are connected", "what devices"]:
    check(f"inventory: {t!r}", is_inventory_question(t, ["ceiling light", "kitchen"]))
for t in ["do you have tesla", "what is the temperature", "what is the time",
          "what time do I have the ceiling light on", "what is the power rating of the kitchen",
          "turn on what I have", "which room", "what devices are on", "is anything on",
          "what is the kitchen light doing", "do I have any scenes",
          "what lights do I have", "what sensors do I have", "what lights are on",
          "what light devices do I have", "which fans are running",
          "list the lights we have", "check the ceiling light", "show me the temperature",
          "list the rooms", "tell me the time", "read the power meter"]:
    check(f"not inventory: {t!r}", not is_inventory_question(t, ["ceiling light", "kitchen"]))

from nlu.spoken_cancel import fixed_cancel, normalise           # noqa: E402
for t in ["go back home.", "go home", "go back"]:
    check(f"a way out of a question: {t!r}", fixed_cancel(normalise(t)))

print("\nthe phone's clock arrives as data")
check("an ISO time with an offset", _phone_clock("2026-10-01T17:42:10.123+01:00") is not None)
for bad in [None, 5, "", "yesterday", "2026-13-45T99:00:00", "1999-01-01T00:00:00+00:00", "x" * 80]:
    check(f"refused: {bad!r}", _phone_clock(bad) is None)

print("\nthrough _run_command")
dm, _ = T.fresh()
dm.add("k.light", name="ceiling light", room="kitchen", description="ceiling light")
dm.add("k.fan", name="fan", room="kitchen", description="extractor fan")
dm.add("b.lamp", name="bedside lamp", room="bedroom", description="lamp")
dm.devices["k.light"]["state"] = {"on": True}
dm.devices["k.fan"]["state"] = {"on": False}
dm.devices["b.lamp"]["state"] = {"on": True}


class _Ex:
    def extract(self, text):
        return {}


def shell(phone="", now=None):
    s = InteractiveShell.__new__(InteractiveShell)
    s.dm = dm
    s.automations = None
    s.said = []
    s.current_phone_id = phone
    s.phone_now = now
    s.extractor = _Ex()
    s.intent_classifier = None

    class IO:
        async def write(self, x, level=None):
            s.said.append(x)

        async def send_data(self, k, v):
            pass

        def raise_if_cancelled(self):
            pass
    s._io = IO()
    s._is_undo = lambda text: False
    return s


async def ask(text, **kw):
    s = shell(**kw)
    ok = await s._run_command(text, "Bruce Wayne")
    return ok, " | ".join(x.strip() for x in s.said if x.strip())


async def s_multi(text, **kw):
    """Through _run_multi: the splitter first, as a spoken command goes."""
    s = shell(**kw)
    s.sm = None

    async def passed(*_a, **_k):                  # the voice gate is not under test
        return True
    s._voice_gate = passed
    ok = await s._run_multi(text, "Bruce Wayne")
    return ok, " | ".join(x.strip() for x in s.said if x.strip())


evening = datetime(2026, 10, 1, 17, 42, tzinfo=timezone(timedelta(hours=1)))
ok, r = asyncio.run(ask("what is the time", phone="p1", now=evening))
check("time from the phone", ok and "5:42 PM" in r, r)
ok, r = asyncio.run(ask("what's the date", phone="p1", now=evening))
check("date from the phone", ok and "Thursday, 1 October 2026" in r, r)
ok, r = asyncio.run(ask("And the time is what?", phone="p1", now=evening))
check("the mangled form from the field log", ok and "5:42 PM" in r, r)
ok, r = asyncio.run(ask("what is the time and date", phone="p1", now=evening))
check("both", ok and "5:42 PM on Thursday, 1 October 2026" in r, r)
ok, r = asyncio.run(ask("Time", phone="p1", now=evening))
check("the bare word", ok and "5:42 PM" in r, r)


async def nothing_matched(text):
    """What the pipeline does when it resolved NOTHING (kind none)."""
    s = shell(phone="p1", now=evening)
    s._io.raise_if_cancelled = lambda: None
    ok = await s._carry_out({"kind": "none", "message": "No device matched."},
                            {}, "Bruce Wayne", text=text)
    return ok, " | ".join(x.strip() for x in s.said if x.strip())


ok, r = asyncio.run(nothing_matched("Always the time."))
check("a mis-heard question, last resort", ok and "5:42 PM" in r, r)
ok, r = asyncio.run(nothing_matched("what time the heater on"))
check("an event question is NOT the clock", not ok and "5:42" not in r, r)
ok, r = asyncio.run(nothing_matched("what day is tomorrow"))
check("another day is NOT the clock", not ok and "5:42" not in r, r)
ok, r = asyncio.run(ask("what is the time", phone="p1", now=None))
check("a phone with no clock gets an honest answer", not ok and "can't" in r, r)
ok, r = asyncio.run(ask("what is the time", phone="", now=None))
check("the hub's own keyboard uses the hub clock", ok and ("AM" in r or "PM" in r), r)

ok, r = asyncio.run(ask("how is the kitchen"))
check("a room reads out every device in it",
      ok and "ceiling light" in r and "fan" in r and "on" in r and "off" in r
      and "bedside lamp" not in r, r)
ok, r = asyncio.run(ask("what appliances do I have"))
check("the whole inventory, by room, whatever the kind is called",
      ok and "kitchen: ceiling light, fan" in r and "bedroom: bedside lamp" in r, r)
ok, r = asyncio.run(s_multi("List the devices we have."))
check("asked for as an instruction, through the splitter, in one piece",
      ok and "kitchen: ceiling light, fan" in r and "bedroom: bedside lamp" in r, r)
ok, r = asyncio.run(ask("which room"))
check("the rooms with their devices",
      ok and "kitchen: ceiling light, fan" in r and "bedroom: bedside lamp" in r, r)
ok, r = asyncio.run(ask("how is the house"))
check("the whole house is unchanged", ok and "bedside lamp" in r and "fan" in r, r)

print("\nnot available, and no device for that")
dm.add("p.sensor", name="porch sensor", room="porch", description="porch sensor")
dm.devices["p.sensor"]["state"] = {}
ok, r = asyncio.run(ask("what is the porch sensor reading"))
check("a registered device with no reading says so", ok and "porch sensor is not available" in r, r)
ok, r = asyncio.run(ask("what is the zebra"))
check("nothing in the house is not guessed at", "no device for that" in r and "light" not in r and "fan" not in r, r)

print("\na yes/no about having something says yes, then the device and its state")
from nlu.lexicon import asks_existence                          # noqa: E402
for t in ["do I have a power meter", "have we got a blaster", "is there an ir sensor",
          "do you have a porch sensor", "are there any fans", "does the house have a lock"]:
    check(f"asks if the house has it: {t!r}", asks_existence(t))
for t in ["what is the power rating", "is the fan on", "do the lights work", "turn on the fan",
          "what do I have"]:
    check(f"not that: {t!r}", not asks_existence(t))
ok, r = asyncio.run(ask("do I have the ceiling light"))
check("existence: Yes, name, state", ok and r.startswith("Yes. ceiling light:"), r)
ok, r = asyncio.run(ask("is the ceiling light on"))
check("an ordinary question has no Yes", ok and not r.startswith("Yes") and "ceiling light:" in r, r)

print("\nundo, by what was done")
dm.add("k.lock", name="back door lock", room="kitchen", description="door lock", secure=True)
dm.add("k.meter", name="power meter", room="kitchen", description="power meter",
       readable_only=True)


def reset():
    dm.devices["k.light"]["state"] = {"on": True, "level": 80}
    dm.devices["k.fan"]["state"] = {"on": False}
    dm.devices["b.lamp"]["state"] = {"on": True}
    dm.devices["k.lock"]["state"] = {"on": True}
    dm.devices["k.meter"]["state"] = {"level": 120.0, "unit": "W"}
    dm._undo = None


def undo():
    s = shell()

    async def took_it(did, **kw):          # a device that accepts what it is sent
        return True
    s._set_state = took_it
    ok = asyncio.run(s._do_undo())
    return ok, " | ".join(x.strip() for x in s.said if x.strip())


reset()
dm.snapshot()
dm.devices["k.light"]["state"] = {"on": False, "level": 80}
ok, r = undo()
check("a switch put back", ok and dm.devices["k.light"]["state"]["on"] is True, r)
ok, r = undo()
check("a second undo has nothing to undo", not ok and "Nothing to undo" in r, r)

reset()
dm.snapshot()
dm.devices["k.light"]["state"] = {"on": True, "level": 20}
ok, r = undo()
check("a level put back", ok and dm.devices["k.light"]["state"]["level"] == 80, r)

reset()
dm.snapshot()
for d, st in (("k.light", {"on": False}), ("k.fan", {"on": True}), ("b.lamp", {"on": False})):
    dm.devices[d]["state"] = st
ok, r = undo()
check("one snapshot puts back every device a command touched (all off, a scene, a split command)",
      ok and dm.devices["k.light"]["state"]["on"] is True
      and dm.devices["k.fan"]["state"]["on"] is False
      and dm.devices["b.lamp"]["state"]["on"] is True, r)

reset()
dm.snapshot()
dm.devices["k.lock"]["state"] = {"on": False}
dm.devices["k.meter"]["state"] = {"level": 150.0, "unit": "W"}
ok, r = undo()
check("a lock is never put back by a bare undo, and it says so",
      dm.devices["k.lock"]["state"]["on"] is False and "back door lock" in " ".join(dm.undo_held), r)
check("a meter's reading is not an undo", dm.devices["k.meter"]["state"]["level"] == 150.0, r)

reset()
dm.snapshot()
dm.devices["k.light"]["state"] = {"on": False, "level": 80}
asyncio.run(ask("is the fan on"))
asyncio.run(ask("what is the porch sensor reading"))
ok, r = undo()
check("a question in between does not use up the undo",
      ok and dm.devices["k.light"]["state"]["on"] is True, r)

reset()
dm.snapshot()
dm.devices["k.light"]["state"] = {"on": False, "level": 80}
kept = dm._undo


class _Declines:
    _CHOICE_NONE = " __none__"

    def __init__(self, sink):
        self.sink = sink

    async def write(self, x, level=None):
        self.sink.append(x)

    async def choose(self, prompt, options, **kw):
        return [self._CHOICE_NONE]

    def raise_if_cancelled(self):
        pass


sh = shell()
sh._io = _Declines(sh.said)
guess = {"kind": "pick", "reason": "semantic_confirm", "message": "Did you mean the fan?",
         "candidates": [{"device_id": "k.fan", "room": "kitchen"}],
         "alternatives": ["k.fan", "b.lamp"], "action": None}
took = []
ok = asyncio.run(sh._carry_out(guess, {}, "Bruce Wayne", snapshot=lambda: took.append(1), text="cool it down"))
check("a refused guess changes nothing and takes no snapshot", not ok and not took and dm._undo == kept,
      (ok, took))
check("and says it could not find it", any("couldn't find it" in x for x in sh.said), sh.said)
ok, r = undo()
check("so the earlier undo is still there", ok and dm.devices["k.light"]["state"]["on"] is True, r)

print("\nclosing the pin screen keeps what was laid out")
from app.shell import WebSocketIO                           # noqa: E402


def closed_with(reply):
    async def send(frame):
        pass

    async def recv(patient=False):
        return reply
    return asyncio.run(WebSocketIO(None, send, recv).pin_map({"items": []}))


laid = [{"type_id": "relay", "name": "lamp", "pins": {"pin": 4}}]
got = closed_with({"type": "pin_map_response", "cancelled": True, "draft": laid})
check("the draft comes back with the cancel", got.get("cancelled") and got.get("draft") == laid, got)
got = closed_with({"type": "pin_map_response", "cancelled": True})
check("no draft sent: an empty one, not a missing key", got.get("draft") == [], got)
got = closed_with({"type": "pin_map_response", "cancelled": True, "draft": "nonsense"})
check("a draft that is not a list is not a draft", got.get("draft") == [], got)

print("\nthe kind is a recommendation, not a fence")
opts = InteractiveShell._kind_options("relay")
ok_vals = {"name": "socket", "room": "kitchen", "kind": "level",
           "min": "0", "max": "100", "step": "10", "state": "0"}
check("a relay is not MEANT to be a level...",
      not next(o for o in opts if o["kind"] == "level")["enabled"])
check("...and choosing level for it is accepted all the same",
      InteractiveShell._validate_form_values(ok_vals, kind_options=opts) is None,
      InteractiveShell._validate_form_values(ok_vals, kind_options=opts))
check("a kind that does not exist is still refused",
      InteractiveShell._validate_form_values({**ok_vals, "kind": "banana"},
                                             kind_options=opts) is not None)

print("\nthe intent check does not take a sentence away from devices found")
from types import SimpleNamespace as _NS                    # noqa: E402


class _Announce:
    def classify(self, text, **_k):
        return {"intent": "announce", "score": 0.575, "margin": 0.082,
                "nearest_seed": "household announcement"}


def said_lights(scores, text="turn on the glowing things"):
    sh = shell()
    sh.intent_classifier = _Announce()
    sh._require_init = lambda: _NS(list_scenes=lambda: [])
    reached = []

    async def carry(decision, *_a, **_k):
        reached.append(decision)
        return True
    sh._carry_out = carry
    saved = dm.resolve_semantic_scored
    dm.resolve_semantic_scored = lambda _t: list(scores)
    try:
        ok = asyncio.run(sh._run_command(text, "Bruce Wayne"))
    finally:
        dm.resolve_semantic_scored = saved
    return ok, reached, sh


# Her hub, 2026-10-05: "Turn on the lights" found the bulb and the lamp by
# meaning, was called an announcement (0.575) and ended with nothing done.
ok, reached, sh = said_lights([("k.light", 0.455), ("b.lamp", 0.444), ("k.fan", 0.354)])
check("devices found by meaning, called an announcement: still theirs",
      reached and reached[0]["kind"] in ("pick", "execute")
      and not any("Announce" in x for x in sh.said), (reached, sh.said))
ok, reached, sh = said_lights([], text="tell everybody dinner is ready")
check("no device at all: the announcement answer stands, as before",
      not ok and not reached and any("Announce" in x for x in sh.said), (ok, sh.said))

print("\na board of ours that is offline is not handed to Home Assistant")
dm.add("k.bulb9", name="porch bulb", room="porch", description="a bulb")
dm.devices["k.bulb9"].update(transport="mqtt", ha_entity_id="switch.porch_bulb",
                             state={"on": False})
pushed = []
_saved = (dm.ha_client, dm._push_to_ha, dm.mqtt_driver, dm.drive_problem)
dm.ha_client = object()
dm._push_to_ha = lambda entity, changes: pushed.append(entity) or True
dm.mqtt_driver = lambda did, change: False
dm.drive_problem = lambda did: "offline"
ok = dm.set_state("k.bulb9", on=True)
check("offline: it fails, and Home Assistant is not asked",
      ok is False and not pushed and dm.devices["k.bulb9"]["state"]["on"] is False,
      (ok, pushed))
for verdict in ("error:TimeoutError", "no-broker", "refused"):
    dm.drive_problem = lambda did, v=verdict: v
    ok = dm.set_state("k.bulb9", on=True)
    check(f"{verdict}: a board of ours that was not reached fails the same way",
          ok is False and not pushed, (ok, pushed))
dm.drive_problem = lambda did: "no board record"
ok = dm.set_state("k.bulb9", on=True)
check("no board of ours for it: Home Assistant is still the road",
      ok and pushed == ["switch.porch_bulb"], (ok, pushed))
dm.ha_client, dm._push_to_ha, dm.mqtt_driver, dm.drive_problem = _saved
dm.remove("k.bulb9")

print("\na failed command says why")
_sh = shell()
_sh.failure_note = None
_sh._drive_problem = lambda did: "offline"
_sh.esp = _NS(_read_board=lambda slug: {"name": "kitchen"})
for _did in ("k.light", "b.lamp"):
    asyncio.run(_sh._render({"outcome": "error", "device_id": _did,
                             "reason": "write_rejected"}))
check("two devices on an offline board: one sentence naming both, in plain words",
      _sh.failure_note == "ceiling light, bedside lamp: the kitchen board is offline, "
      "so nothing was switched. It is off, off the Wi-Fi, or still starting up.",
      _sh.failure_note)

print("\nundo for a device that is named")


async def _gate(*_a, **_k):
    return True

from nlu.slot_extractor import SlotExtractor                # noqa: E402

U = InteractiveShell._undo_rest
check("what follows the undo words", U("Undo the bulbs.") == "the bulbs"
      and U("please undo the state of the fan") == "the state of the fan"
      and U("never mind the lamp") == "the lamp", U("Undo the bulbs."))
check("a plain undo has nothing after it", U("undo.") == "" and U("scratch that") == "")
check("not an undo at all", U("don't undo that") is None and U("turn on the fan") is None)


def named(text):
    sh = shell()
    del sh._is_undo                       # the real one
    sh.extractor = SlotExtractor()
    return sh._undo_named(text, "Bruce Wayne")


check("'undo the fan' names the fan", named("undo the fan") == ["k.fan"], named("undo the fan"))
check("...in the plural, and with words around it",
      named("undo the fans") == ["k.fan"]
      and named("undo the state of the fan") == ["k.fan"],
      (named("undo the fans"), named("undo the state of the fan")))
check("a plain undo names nothing", named("undo") is None and named("undo that") is None,
      named("undo that"))
check("a device that does not exist is not guessed at: plain undo",
      named("undo the zebra") is None, named("undo the zebra"))
check("'no wait, turn on the fan' is not an undo OF the fan",
      named("no wait turn on the fan") is None, named("no wait turn on the fan"))

dm.devices["k.fan"]["state"]["on"] = False
dm.devices["k.light"]["state"]["on"] = False
dm.snapshot()
dm.devices["k.fan"]["state"]["on"] = True
dm.devices["k.light"]["state"]["on"] = True
back = dm.undo(["k.fan"])
check("only the named device goes back",
      [d for d, _ in back] == ["k.fan"] and dm.devices["k.fan"]["state"]["on"] is False
      and dm.devices["k.light"]["state"]["on"] is True, back)
back = dm.undo(["k.fan"])
check("named again: nothing more to put back", back == [], back)
back = dm.undo()
check("the rest of that command can still be undone",
      [d for d, _ in back] == ["k.light"] and dm.devices["k.light"]["state"]["on"] is False, back)
check("...and then the snapshot is spent", dm.undo() == [])

sh = shell()
dm.snapshot()
ok = asyncio.run(sh._do_undo(["k.fan"]))
check("named, but the last command did not touch it: said so, by name",
      not ok and any("Nothing to undo for fan" in x for x in sh.said), sh.said)

sh = shell()
del sh._is_undo
sh.sm = None
sh._voice_gate = _gate
seen = []
dm.snapshot()
before = dm._undo
dm.devices["k.fan"]["state"]["on"] = True       # what the last command did


async def _watch(chunk, who, **kw):
    seen.append((chunk, dm._undo == before))
    return True
sh._run_command = _watch
asyncio.run(sh._run_multi("undo the fan and turn on the ceiling light", "Bruce Wayne"))
check("'undo ... and turn on ...': the undo still has the snapshot it puts back",
      seen and seen[0] == ("undo the fan", True), seen)
check("...and a new one is taken before the command after it",
      len(seen) == 2 and seen[1][1] is False, seen)
dm.devices["k.fan"]["state"]["on"] = False


print("\nwhere a command's time went")
from core.hv_common import timed, times_add, times_begin, times_line   # noqa: E402
import nlu.model_module as _mm                                      # noqa: E402
import numpy as _np                                                # noqa: E402


async def _timed_command():
    box = times_begin()
    with timed("voice"):
        await asyncio.sleep(0.06)

    def work():                       # a worker thread, as the resolver is
        times_add("model_wait", 2.0)
        times_add("model_run", 0.5, 3)
    with timed("resolve"):
        await asyncio.to_thread(work)
    return box

box = asyncio.run(_timed_command())
check("stages are collected, and a worker thread adds to the same command",
      box.get("voice", 0) >= 0.05 and box.get("model_run#") == 3
      and box.get("model_wait") == 2.0, box)
line = times_line({"voice": 0.1, "resolve": 23.8, "model_wait": 22.1,
                   "model_run": 1.6, "model_run#": 1, "asked": 4.0}, 30.0)
check("one line: the stages, what is left over, the model's queue and its work, the person's own time",
      "total 30.0s = voice 0.1 + resolve 23.8 + other 6.1" in line
      and "model: 1 sentence(s), waited 22.1s for its turn, ran 1.6s" in line
      and "4.0s of it was the person answering" in line, line)
times_add("voice", 1.0)               # outside any command: nothing, no error
_saved_fn = _mm._embed_fn
_mm._embed_fn = lambda text: _np.zeros(4, dtype="float32")


async def _one_embed():
    box = times_begin()
    await asyncio.to_thread(_mm.embed, "hello")
    return box

box = asyncio.run(_one_embed())
_mm._embed_fn = _saved_fn
check("the model's own code counts each sentence into the command that asked",
      box.get("model_run#") == 1 and "model_wait" in box, box)

print(f"\n{PASSED} passed, {FAILED} failed")
sys.exit(1 if FAILED else 0)

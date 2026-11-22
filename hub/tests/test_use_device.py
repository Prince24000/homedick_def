"""USING A DEVICE FROM ITS PAGE IN THE APP (2026-11-05).

Each device's page offers the controls that fit what it is; one menu
action carries the act; a secure device always asks for the PIN; a display
takes a line written in the app -- kept up and kept current, or for a
while and then the clock -- and gives the glass back on Clear.

Run: python3 tests/test_use_device.py
"""
import asyncio
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("HOMEVOICE_HOME", tempfile.mkdtemp())
from app.shell import InteractiveShell                          # noqa: E402
from nlu import automations as A                          # noqa: E402

PASSED = FAILED = 0


def check(name, ok, detail=""):
    global PASSED, FAILED
    PASSED, FAILED = PASSED + bool(ok), FAILED + (not ok)
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}   {'' if ok else detail}")


LOOP = asyncio.new_event_loop()


def run(coro):
    return LOOP.run_until_complete(coro)


class DM:
    def __init__(self):
        self.devices = {
            "living.lamp": {"name": "lamp", "transport": "mqtt"},
            "hall.light": {"name": "hall light",
                           "ha_entity_id": "light.hall", "state_key": "level",
                           "range_min": 0, "range_max": 100},
            "porch.temp": {"name": "porch temp",
                           "ha_entity_id": "sensor.porch"},
            "front.lock": {"name": "front lock", "secure": True,
                           "ha_entity_id": "lock.front", "state_key": "on"},
            "tv.remote": {"name": "tv", "ir_codes": {"pause": "x", "power": "y"}},
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

    @staticmethod
    def board_device_id(slug, name):
        return f"{slug}.{name.lower()}"


class Esp:
    def _plan_for(self, slug):
        return None

    def display_targets(self):
        return [{"slug": "living", "topic": "hv-living", "name": "Display",
                 "rows": 4, "cols": 20}]


sh = InteractiveShell.__new__(InteractiveShell)
sh.dm, sh.esp, sh.automations = DM(), Esp(), None
sh.failure_note = None
sh.current_speaker_id = "bruce"
sets, pins, pressed = [], [], []


async def set_state(did, **kw):
    sets.append((did, kw))
    return True


async def pin(who, act):
    pins.append((who, act))
    return pin.answer, "Wrong PIN."
pin.answer = True


async def press_ir(did, text, slots):
    pressed.append((did, text))
    return True
sh._set_state, sh.confirm_with_pin, sh._press_ir = set_state, pin, press_ir
sh._drive_problem = lambda did: ""

print("\nthe controls fit the device")
use = {did: sh._use_of(did, d, {}) for did, d in sh.dm.devices.items()}
check("a dimmable Home Assistant light gets a brightness slider",
      use["hall.light"]["kind"] == "dimmer", use["hall.light"])
check("a sensor only shows its reading", use["porch.temp"]["kind"] == "reading")
check("an IR appliance gets its own learned buttons",
      use["tv.remote"] == {"kind": "ir", "buttons": ["pause", "power"]})
check("a lock is a switch -- and secure", use["front.lock"]["kind"] == "switch")
check("a board with a display says so (for its Write card)",
      sh._board_display("living")["display"]["rows"] == 4)

print("\none menu action carries the act")
check("on", run(sh.menu_use_device("living.lamp|on|")) and
      sets[-1] == ("living.lamp", {"on": True}))
check("brightness 40: on at 40", run(sh.menu_use_device("hall.light|level|40"))
      and sets[-1] == ("hall.light", {"on": True, "level": 40.0}), sets[-1])
check("brightness 0: off", run(sh.menu_use_device("hall.light|level|0"))
      and sets[-1] == ("hall.light", {"on": False}), sets[-1])
check("a colour", run(sh.menu_use_device("hall.light|colour|255,0,10"))
      and sh.dm.colour == ("hall.light", (255, 0, 10)))
check("an IR button", run(sh.menu_use_device("tv.remote|press|pause"))
      and pressed[-1] == ("tv.remote", "pause"))

print("\na secure device always asks")
n = len(sets)
check("PIN right: unlocked", run(sh.menu_use_device("front.lock|off|"))
      and pins[-1][0] == "bruce" and len(sets) == n + 1)
pin.answer = False
ok = run(sh.menu_use_device("front.lock|on|"))
check("PIN wrong: nothing changed, and it says so",
      not ok and len(sets) == n + 1 and "not changed" in (sh.failure_note or ""),
      sh.failure_note)
pin.answer = True

print("\nwriting on a display")


class Info:
    rc = 0


class Pub:
    def __init__(self):
        self.sent = []

    def publish(self, topic, body, qos=0):
        self.sent.append((topic, body))
        return Info()


class EDM:
    devices = {"living.power_meter": {"name": "power meter"}}

    def get_state(self, did):
        return {}


now = [1_800_000_000.0]
eng = A.AutomationEngine(EDM(), None, None,
                         path=os.path.join(tempfile.mkdtemp(), "a.json"),
                         now=lambda: now[0])
eng._pub = pub = Pub()
eng.esp = Esp()
eng._lcd_targets = Esp().display_targets()
eng._lcd_awake["hv-living"] = 3
eng._last[("living.power_meter", "voltage")] = (230.1, now[0])
sh.automations = eng
line = json.dumps({"text": "Mains {living.power_meter:voltage} V", "for_s": 0})
ok = run(sh.menu_use_device("living.display|show|" + line))
texts = [b for t, b in pub.sent if t.endswith("/DisplayText")]
check("Keep it up: the line goes on the glass with the live reading in it",
      ok and any("230" in t for t in texts), (sh.failure_note, pub.sent))
check("...held with no clock turn", eng._lcd_hold_until["hv-living"] == float("inf"))
pub.sent.clear()
eng._last[("living.power_meter", "voltage")] = (231.4, now[0])
now[0] += 11
eng._lcd_tick()
now[0] += 11
eng._lcd_tick()
check("...and the reading on it stays current (repainted when it changes)",
      any("231" in b for t, b in pub.sent if t.endswith("/DisplayText")),
      pub.sent)
check("...and it is remembered for a restart",
      "living.display" in eng._manual)
pub.sent.clear()
ok = run(sh.menu_use_device("living.display|clear|"))
check("Clear gives the glass back to the clock",
      ok and ("cmnd/hv-living/DisplayMode", "1") in pub.sent
      and "living.display" not in eng._manual, pub.sent)
line = json.dumps({"text": "Dinner is ready", "for_s": 600})
ok = run(sh.menu_use_device("living.display|show|" + line))
check("For 10 minutes: shown, then the clock -- and not back again",
      ok and not (eng._lcd_standing.get("hv-living") or {})
      and eng._lcd_hold_until["hv-living"] == now[0] + 600)
bad = json.dumps({"text": "{living.power_meter.voltage}", "for_s": 0})
ok = run(sh.menu_use_device("living.display|show|" + bad))
check("a reading written with a dot where the colon goes is caught, in words",
      not ok and sh.failure_note, sh.failure_note)

print("\npages, lines and where they sit")
pub.sent.clear()
run(sh.menu_use_device("living.display|clear|"))
pages = [[{"text": "Kitchen", "align": "middle"},
          {"text": "V {living.power_meter:voltage}", "align": "right"}],
         [{"text": "Living room", "align": "left"}]]
line = json.dumps({"pages": pages, "for_s": 0, "page_s": 10,
                   "clock_between": True})
pub.sent.clear()
ok = run(sh.menu_use_device("living.display|show|" + line))
texts = [b for t, b in pub.sent if t.endswith("/DisplayText")]
check("page one goes up, every row written", ok and len(texts) == 4,
      (sh.failure_note, texts))
check("a middle line is centred", ok and texts[0] == "[x0y0]" +
      "Kitchen".center(20), texts[:1])
check("a right line ends at the edge, its reading filled in",
      ok and texts[1].endswith("V 231.4") and len(texts[1]) == 6 + 20,
      texts[1:2])
pub.sent.clear()
now[0] += 10
eng._lcd_tick()
check("after its time: the clock's turn between pages",
      ("cmnd/hv-living/DisplayMode", "1") in pub.sent and
      not any(t.endswith("/DisplayText") for t, b in pub.sent), pub.sent)
pub.sent.clear()
now[0] += max(A.DISPLAY_CLOCK_SECONDS, 1) + 0.1
eng._lcd_tick()
texts = [b for t, b in pub.sent if t.endswith("/DisplayText")]
check("then page two", texts[:1] == ["[x0y0]" + "Living room".ljust(20)],
      pub.sent)
pub.sent.clear()
now[0] += 10
eng._lcd_tick()
now[0] += max(A.DISPLAY_CLOCK_SECONDS, 1) + 0.1
eng._lcd_tick()
texts = [b for t, b in pub.sent if t.endswith("/DisplayText")]
check("and round to page one again", texts[:1] == ["[x0y0]" +
      "Kitchen".center(20)], pub.sent)
check("pages are remembered for a restart",
      eng._manual["living.display"]["action"].get("pages") == pages)
eng._save_state()
eng2 = A.AutomationEngine(EDM(), None, None, path=eng.path,
                          now=lambda: now[0])
check("...and come back after it",
      (eng2._manual.get("living.display") or {}).get("action", {})
      .get("clock_between") is True)
rule = {"name": "low", "trigger": {"kind": "time", "at": "07:00"},
        "action": {"kind": "display", "device": "living.display",
                   "pages": pages, "for_s": 600}}
check("a rule with pages is valid", A.validate_rule(rule) is None,
      A.validate_rule(rule))
check("...and reads back in words",
      "'Kitchen V {living.power_meter:voltage}' and 1 more page"
      in A.trigger_sentence(rule)
      or "and 1 more page" in A.trigger_sentence(rule),
      A.trigger_sentence(rule))
bad = dict(rule, action=dict(rule["action"],
                             pages=[[{"text": "x", "align": "top"}]]))
check("a line that sits somewhere odd is refused in words",
      "left, middle or right" in (A.validate_rule(bad) or ""))
empty = dict(rule, action=dict(rule["action"], pages=[[{"text": " "}]]))
check("pages with nothing on them are refused",
      "nothing to show" in (A.validate_rule(empty) or ""))
sh.dm.devices["living.power_meter"] = {"name": "power meter"}
check("the preview gets each reading's value now",
      any(r.get("value") == "231.4" for r in sh._display_readings()),
      sh._display_readings())

print("\nevery reading a meter has reported is offered, and remembered")
eng._index = {"kitchen": {("energy", 0): "living.power_meter"}}
eng.feed("tele/kitchen/SENSOR", '{"ENERGY":{"Voltage":230,"Power":120,'
         '"ApparentPower":130,"Factor":0.92,"Total":4.5}}')
check("apparent power is offered once the meter has sent it",
      "apparentpower" in eng.reading_keys("living.power_meter"),
      eng.reading_keys("living.power_meter"))
check("...named in words from the board's own spelling",
      eng.key_words("apparentpower") == "apparent power")
check("...and the reading picker says so",
      any(r["label"].endswith("apparent power") for r in sh._display_readings()),
      [r["label"] for r in sh._display_readings()])
eng._save_state()
eng3 = A.AutomationEngine(EDM(), None, None, path=eng.path, now=lambda: now[0])
check("after a restart, before the meter reports again, it is still offered",
      "apparentpower" in eng3.reading_keys("living.power_meter")
      and eng3.key_words("factor") == "factor",
      eng3.reading_keys("living.power_meter"))

print(f"\n{PASSED} passed, {FAILED} failed")
LOOP.close()
sys.exit(1 if FAILED else 0)

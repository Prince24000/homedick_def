"""INFRARED FIXES (2026-11-07): raw codes replay as raw, a press goes only
through a blaster in the appliance's room (and says so when none can), a
button is picked by its own learned name only, learning uses an online
receiver, and the hub does not take its own sends for presses.

Run: python3 tests/test_ir_fixes.py
"""
import asyncio
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("HOMEVOICE_HOME", tempfile.mkdtemp())
os.environ.setdefault("HOMEVOICE_BOARDS_DIR", tempfile.mkdtemp())
ENGINE_PKG = "nlu"
A = __import__(f"{ENGINE_PKG}.automations", fromlist=["x"])
from app.shell import InteractiveShell                          # noqa: E402
from esp_firmware import ir_flows as F                          # noqa: E402
from esp_firmware.esp_builder import EspBuilder                 # noqa: E402

PASSED = FAILED = 0


def check(name, ok, detail=""):
    global PASSED, FAILED
    PASSED, FAILED = PASSED + bool(ok), FAILED + (not ok)
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}   {'' if ok else detail}")


LOOP = asyncio.new_event_loop()


def run(coro):
    return LOOP.run_until_complete(coro)


NEC = {"Protocol": "NEC", "Bits": 32, "Data": "0x20DF10EF"}
RAW = [9000, 4500, 560, 560, 560, 1690, 560, 560]

print("\nwhat goes after IRsend")
check("a decoded code is Tasmota's JSON",
      F.ir_command_body(NEC) == json.dumps(NEC), F.ir_command_body(NEC))
check("a raw code is the raw form, 0 kHz (the default) first",
      F.ir_command_body({"RawData": RAW}) ==
      "0,9000,4500,560,560,560,1690,560,560")
old = json.loads('{"RawData": [9000, 4500, 560, 560, 560, 1690]}')
check("an old saved raw code reads the same",
      F.ir_command_body(old) == "0,9000,4500,560,560,560,1690")
check("a raw code's Hash is not sent",
      F.ir_command_body({"RawData": RAW, "Hash": "0x1"}).startswith("0,9000"))
check("a code that cannot be read is not sent",
      F.ir_command_body({"RawData": "nonsense"}) is None
      and F.ir_command_body("x") is None)
check("a comma string is read as timings",
      F.parse_raw_data("9000, 4500,560,560") == [9000, 4500, 560, 560])
check("Tasmota's compact string is read as timings",
      F.parse_raw_data("+8570-4240+550-1580C-510+565-1565F-505Fh")
      == [8570, 4240, 550, 1580, 550, 510, 565, 1565, 565, 505, 565, 505],
      F.parse_raw_data("+8570-4240+550-1580C-510+565-1565F-505Fh"))
check("a compact string that does not read cleanly is refused",
      F.parse_raw_data("+8570-4240Z") is None
      and F.parse_raw_data("+8570+4240-1-2") is None)

esp = EspBuilder.__new__(EspBuilder)
published = []
esp._read_board = lambda slug: {"topic": f"hv-{slug}", "name": slug}


async def _publish(record, commands, **_kw):
    published.append((record["topic"], commands))
    return True
esp._publish = _publish
run(esp.send_ir("hall", {"RawData": RAW}))
check("send_ir sends a raw code raw",
      published[-1] == ("hv-hall", ["IRsend 0,9000,4500,560,560,560,1690,560,560"]),
      published)
run(esp.send_ir("hall", NEC))
check("...and a decoded one as JSON",
      published[-1] == ("hv-hall", [f"IRsend {json.dumps(NEC)}"]), published)
check("...and refuses one it cannot read, without publishing",
      run(esp.send_ir("hall", {"RawData": "??"})) is False
      and len(published) == 2)

print("\nwhat learning keeps")
code, said = F.IRFlowRunner._ir_code_from(
    {"Protocol": "UNKNOWN", "Bits": 60, "Hash": "0xAB12", "RawData": RAW,
     "RawDataInfo": [8, 8, 0]})
check("a raw capture is kept as timings, with its Hash",
      code == {"RawData": RAW, "Hash": "0xAB12"}, code)
code, said = F.IRFlowRunner._ir_code_from(
    {"Protocol": "UNKNOWN", "RawData": "+8570-4240+550-1580C-510",
     "RawDataInfo": [6, 6, 0]})
check("a compact string capture is kept as a list",
      code == {"RawData": [8570, 4240, 550, 1580, 550, 510]}, code)
code, said = F.IRFlowRunner._ir_code_from(
    {"Protocol": "UNKNOWN", "RawData": "+85-?", "RawDataInfo": [2, 2, 0]})
check("an unreadable string capture is refused in a sentence",
      code is None and said == F.UNREADABLE_SAID, said)
code, said = F.IRFlowRunner._ir_code_from(
    {"Protocol": "UNKNOWN", "RawData": RAW, "RawDataInfo": [8, 8, 1]})
check("an overflowed capture is refused",
      code is None and said == ("That signal was too long to capture -- hold "
                                "the remote closer and press once."), said)
code, said = F.IRFlowRunner._ir_code_from(
    {"Protocol": "NEC", "Bits": 0, "Data": "0xFFFFFFFFFFFFFFFF", "Repeat": 1})
check("an NEC repeat frame is refused", code is None and said == F.REPEAT_SAID)
check("...by Bits 0 alone",
      F.IRFlowRunner._ir_code_from({"Protocol": "NEC", "Bits": 0,
                                    "Data": "0x0"})[0] is None)
check("...by all F's alone",
      F.IRFlowRunner._ir_code_from({"Protocol": "NEC", "Bits": 32,
                                    "Data": "0xFFFFFFFF"})[0] is None)
check("an ordinary NEC press is kept",
      F.IRFlowRunner._ir_code_from(dict(NEC, Repeat=0))[0] ==
      {"Protocol": "NEC", "Bits": 32, "Data": "0x20DF10EF"})

print("\nwhich button")
pick = InteractiveShell._ir_button_for
check("a button the person named 'off' matches 'turn off the tv'",
      pick({"power": NEC, "off": NEC}, "turn off the tv", {"switch": "off"},
           drop=("tv", "living room")) == "off")
check("power only: 'turn off the tv' picks nothing (no word lists)",
      pick({"power": NEC}, "turn off the tv", {"switch": "off"},
           drop=("tv", "living room")) is None)
check("'power off' matches a power_off button",
      pick({"power_off": NEC, "power": NEC}, "power off the tv", None,
           drop=("tv",)) == "power_off")
check("a button named like the appliance does not take over",
      pick({"tv": NEC, "power": NEC}, "mute the tv", None,
           drop=("tv", "living room")) is None)
check("...nor one named like its room",
      pick({"living": NEC, "power": NEC}, "mute the living room tv", None,
           drop=("tv", "living room")) is None)
check("...and the button that was said still wins",
      pick({"tv": NEC, "power": NEC}, "power on the tv", None,
           drop=("tv",)) == "power")


class DM:
    def __init__(self):
        self.devices = {
            "den.tv": {"name": "tv", "room": "living room", "transport": "ir",
                       "ir_codes": {"power": NEC, "tv": dict(NEC, Data="0x2"),
                                    "eject": {"RawData": "??"}}},
            "attic.radio": {"name": "radio", "room": "bedroom",
                            "transport": "ir", "ir_codes": {"power": NEC}},
            "den.fan": {"name": "fan", "room": "kitchen", "transport": "ir",
                        "ir_codes": {"power": NEC}},
            "lounge.lamp": {"name": "lamp", "room": "Living room"},
            "sofa.light": {"name": "light", "room": "living room"},
            "attic.bulb": {"name": "bulb", "room": "attic"},
            "porch.light": {"name": "porch", "room": "Kitchen"},
            "kitchen.tv": {"name": "tv", "room": "kitchen", "transport": "ir",
                           "ir_codes": {"power": NEC}},
        }

    @staticmethod
    def board_device_id(slug, name):
        return f"{slug}.{name}"


class Esp:
    def __init__(self):
        self.online = set()
        self.tried, self.published = [], []
        self.leds = ["attic", "lounge", "sofa"]
        self.receivers = []

    def boards_with(self, type_id):
        return list(self.leds if type_id == "ir_send" else
                    self.receivers if type_id == "ir_recv" else [])

    def _read_board(self, slug):
        return {"topic": f"hv-{slug}", "name": slug}

    async def send_ir(self, slug, code):
        self.tried.append(slug)
        return slug in self.online

    async def _publish(self, record, commands, **_kw):
        ok = record["name"] in self.online
        self.published.append((record["name"], commands, ok))
        return ok

    def listen_problem(self):
        return ""


class IO:
    def __init__(self, answers=()):
        self.lines, self.asked = [], []
        self.answers = list(answers)

    async def write(self, text, level=None):
        self.lines.append(str(text))

    async def get_input(self, prompt):
        self.asked.append(prompt)
        return self.answers.pop(0)

    async def ainput(self, prompt):
        self.asked.append(prompt)
        return ""

    async def send_data(self, *_a, **_k):
        return None

    async def choose(self, *_a, **_k):
        return ["finish"]


clock = [1_000_000.0]
eng = A.AutomationEngine(DM(), None, None,
                         path=os.path.join(tempfile.mkdtemp(), "a.json"),
                         now=lambda: clock[0])
sh = InteractiveShell.__new__(InteractiveShell)
sh.dm, sh.esp, sh._io, sh.automations = DM(), Esp(), IO(), eng
sh.failure_note = None


def press(did, text):
    sh.esp.tried.clear()
    sh._io.lines.clear()
    sh.failure_note = None
    return run(sh._press_ir(did, text, None))


print("\nwhich blaster")
sh.esp.online = {"sofa", "attic"}
check("an offline board in the room is skipped, the next one sends",
      press("den.tv", "power on the tv") and sh.esp.tried == ["lounge", "sofa"],
      sh.esp.tried)
check("...a board in another room is never tried", "attic" not in sh.esp.tried)
check("...and it says it was sent", any("tv: power" in x
                                        for x in sh._io.lines), sh._io.lines)
sh.esp.online = {"attic"}
ok = press("den.tv", "power")
check("no living-room board online: not sent, and failure",
      not ok and sh.esp.tried == ["lounge", "sofa"], sh.esp.tried)
check("...said in words",
      sh.failure_note == "No infrared board in the living room is online -- "
                         "tv was not sent 'power'.", sh.failure_note)
check("...and never 'sent'", not any("->" in x for x in sh._io.lines))
ok = press("den.fan", "power")
check("no blaster in the room at all: not sent, said so",
      not ok and sh.esp.tried == [] and sh.failure_note ==
      "No infrared board is in the kitchen -- fan was not sent 'power'.",
      (sh.esp.tried, sh.failure_note))
check("its own board sends first, whatever its room",
      press("attic.radio", "power") and sh.esp.tried == ["attic"],
      sh.esp.tried)
check("a code that cannot be sent is said so, and nothing is tried",
      not press("den.tv", "eject") and sh.esp.tried == []
      and "learn it again" in (sh.failure_note or ""), sh.failure_note)
check("power only: 'turn off the tv' lists what it knows",
      not press("kitchen.tv", "turn off the tv")
      and any("tv knows: power -- say one of those" in x
              for x in sh._io.lines), sh._io.lines)
sh.esp.online = {"lounge"}
check("the app's button named like the appliance still presses it",
      press("den.tv", "tv") and sh.esp.tried == ["lounge"], sh.esp.tried)
check("by voice, 'mute the tv' does not press the 'tv' button",
      not press("den.tv", "mute the tv") and sh.esp.tried == [])

print("\nthe engine's press")


class Pub:
    class Info:
        rc = 0

    def __init__(self):
        self.sent = []

    def publish(self, topic, body, qos=0):
        self.sent.append((topic, body))
        return self.Info()


eng.esp = Esp()
eng._pub = pub = Pub()
rule = {"name": "t", "trigger": {"kind": "time", "at": "07:00"},
        "action": {"kind": "press", "device": "den.tv", "button": "power"}}
eng._offline = {"hv-lounge"}
check("an offline board is skipped, a same-room one sends",
      eng._press(rule, rule["action"]) and pub.sent ==
      [("cmnd/hv-sofa/IRsend", json.dumps(NEC))], pub.sent)
eng._offline = {"hv-lounge", "hv-sofa"}
pub.sent.clear()
check("none in the room online: False, nothing sent",
      eng._press(rule, rule["action"]) is False and pub.sent == [], pub.sent)
check("...and the journal says why",
      any("No infrared board in the living room is online -- tv was not "
          "sent 'power'" in t for _ts, _n, t in eng.journal),
      list(eng.journal)[-1:])
eng.dm.devices["den.tv"]["ir_codes"]["power"] = {"RawData": RAW}
eng._offline = set()
pub.sent.clear()
eng._press(rule, rule["action"])
check("a raw code goes out raw",
      pub.sent == [("cmnd/hv-lounge/IRsend",
                    "0,9000,4500,560,560,560,1690,560,560")], pub.sent)

print("\nlearning")
sh.dm = DM()
sh.ir = F.IRFlowRunner(sh)
sh.esp = Esp()
sh.esp.receivers = ["a", "b", "c"]
sh.esp.online = {"b", "c"}
sh.dm.devices["x.tv"] = {"name": "tv", "room": "living room",
                         "transport": "ir", "ir_codes": {}}
sh.dm.devices["c.lamp"] = {"name": "lamp", "room": "Living Room"}
sh._io = IO(["tv"])
run(sh.ir.menu_learn_ir())
check("the online receiver in the appliance's room is used",
      [p[0] for p in sh.esp.published if p[2]] == ["c"]
      and any("receiver on c" in x for x in sh._io.lines),
      (sh.esp.published, sh._io.lines))
sh.esp.published.clear()
sh._io = IO(["radio"])
run(sh.ir.menu_learn_ir())
check("otherwise the first online one, offline ones passed over",
      [(p[0], p[2]) for p in sh.esp.published] == [("a", False), ("b", True)]
      and sh.esp.published[-1][1] == ["SetOption58 1"], sh.esp.published)
sh.esp.online = set()
sh._io = IO(["tv"])
ok = run(sh.ir.menu_learn_ir())
check("none online: it stops, in one sentence, before asking for buttons",
      ok is False and sh._io.lines[-1].strip() ==
      "No infrared receiver is online -- power one, then learn again."
      and len(sh._io.asked) == 1, (sh._io.lines, sh._io.asked))

print("\nthe hub hearing its own sends")


class EDM:
    def __init__(self):
        self.devices = {"remote": {"name": "remote", "transport": "mqtt",
                                   "state": {"on": False}},
                        "lamp": {"name": "lamp", "transport": "mqtt",
                                 "state": {"on": False}}}

    def get_state(self, did):
        return dict(self.devices[did]["state"])

    def set_state(self, did, _driver=None, **kw):
        self.devices[did]["state"].update(kw)
        return True


edm = EDM()
e = A.AutomationEngine(edm, None, None,
                       path=os.path.join(tempfile.mkdtemp(), "a.json"),
                       now=lambda: clock[0])
e.history_path = os.path.join(tempfile.mkdtemp(), "h.json")
e.add({"name": "ir", "trigger": {"kind": "event", "device": "remote",
                                 "value": "any"},
       "action": {"kind": "device", "device": "lamp", "set": {"on": True}}})
e._index = {"tb": {("ir", 0): "remote"}}
heard = json.dumps({"IrReceived": dict(NEC, Repeat=0)})
e.note_ir_sent(NEC)
clock[0] += 0.4
facts = e.feed("tele/tb/RESULT", heard)
check("its own send heard within a second: no press stored",
      not any(k == "ir" for _d, k, _v in facts), facts)
check("...and no 'any button' rule fires",
      edm.devices["lamp"]["state"]["on"] is False)
clock[0] += 1.5
facts = e.feed("tele/tb/RESULT", heard)
check("a real press of the same button later counts",
      any(k == "ir" for _d, k, _v in facts)
      and edm.devices["lamp"]["state"]["on"] is True, facts)
edm.devices["lamp"]["state"]["on"] = False
clock[0] += 100
e.note_ir_sent({"RawData": RAW, "Hash": "0xAB12"})
clock[0] += 0.2
facts = e.feed("tele/tb/RESULT", json.dumps({"IrReceived": {
    "Protocol": "UNKNOWN", "Bits": 4, "Hash": "0x9999",
    "RawData": [9040, 4470, 575, 545, 550, 1700, 570, 555]}}))
check("a raw send heard back (timings near enough) is ignored",
      not facts and edm.devices["lamp"]["state"]["on"] is False, facts)
facts = e.feed("tele/tb/RESULT", json.dumps({"IrReceived": {
    "Protocol": "UNKNOWN", "Bits": 4, "Hash": "0xAB12"}}))
check("...and by its Hash alone", not facts, facts)
facts = e.feed("tele/tb/RESULT", json.dumps({"IrReceived": {
    "Protocol": "SONY", "Bits": 12, "Data": "0xA90"}}))
check("a different button inside the second still counts",
      any(k == "ir" for _d, k, _v in facts), facts)
sh.automations = e
sh.dm = DM()
sh.esp = Esp()
sh.esp.online = {"lounge"}
clock[0] += 100
run(sh._press_ir("den.tv", "power", None))
clock[0] += 0.3
facts = e.feed("tele/tb/RESULT", heard)
check("a send from the shell is recorded in the engine too", not facts, facts)

print(f"\n{PASSED} passed, {FAILED} failed")
sys.exit(1 if FAILED else 0)

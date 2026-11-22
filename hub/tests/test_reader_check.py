"""CHECK THE CARD READER (2026-11-07).

A card reader's page has "Check the card reader": one menu action,
`checkreader:<device id>`, that asks the reader's board four things in
order -- is it online, does its firmware have the driver, are the pins set
as the layout says, does a tap arrive -- and stops at the first that fails,
in one plain sentence. The board's answers are faked here.

Run: python3 tests/test_reader_check.py
"""
import asyncio
import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("HOMEVOICE_HOME", tempfile.mkdtemp())
from app.shell import InteractiveShell                          # noqa: E402
from esp_firmware import esphome_catalog as cat                 # noqa: E402
from esp_firmware.esp_builder import EspBuilder                 # noqa: E402
from nlu.device_manager import DeviceManager                    # noqa: E402

PASSED = FAILED = 0


def check(name, ok, detail=""):
    global PASSED, FAILED
    PASSED, FAILED = PASSED + bool(ok), FAILED + (not ok)
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}   {'' if ok else detail}")


READER = "front.card_reader"
PINS = {"cs": 5, "rst": 22, "clk": 18, "mosi": 23, "miso": 19}
RECORD = {"name": "Front door", "slug": "front", "topic": "hv-front",
          "items": [{"type_id": "rc522", "name": "card reader",
                     "pins": dict(PINS)},
                    {"type_id": "onoff", "name": "porch light",
                     "pins": {"pin": 26}}]}


def gpio_reply(changes=None):
    """`Gpio 255` as the board would answer it for RECORD's template, split
    over two publishes; `changes` overrides pins ({pin: (code, name)})."""
    plan = Esp()._plan_for("front")
    sent = plan.template()["GPIO"]
    pins = {g: (sent[i], InteractiveShell._component_label(sent[i], "None"))
            for i, g in enumerate(plan.board.template_order)}
    pins.update(changes or {})
    parts = [f'"GPIO{g}":{{"{code}":"{label}"}}'
             for g, (code, label) in sorted(pins.items())]
    half = len(parts) // 2
    return ["{" + ",".join(parts[:half]) + "}",
            "{" + ",".join(parts[half:]) + "}"]


HOMEVOICE_GPIOS = ['{"GPIOs1":{"None":0,"Relay":224,"SPI MISO":672,'
                   '"SPI MOSI":704,"SPI CLK":736}}',
                   '{"GPIOs2":{"RC522 Rst":6112,"RC522 CS":6272}}']
STOCK_GPIOS = ['{"GPIOs1":{"None":0,"Relay":224,"SPI MISO":672,'
               '"SPI MOSI":704,"SPI CLK":736}}']


class Esp:
    """The board, as far as the check sees it: its record, its plan, and
    what it answers over MQTT."""
    _missing_drivers = EspBuilder._missing_drivers
    _pins_assigned = staticmethod(EspBuilder._pins_assigned)
    _plan_notes = []

    def __init__(self, answers=None):
        self.answers = answers or {}
        self.asked = []

    def list_nodes(self):
        return ["front"]

    def _read_board(self, slug):
        return dict(RECORD) if slug == "front" else None

    def _plan_for(self, slug):
        plan = cat.BoardPlan(RECORD["name"])
        for item in RECORD["items"]:
            plan.add(item["type_id"], item["name"], dict(item["pins"]),
                     anyway=True)
        return plan

    def listen_problem(self):
        return ""

    async def _mqtt_question(self, record, command, payload, suffix, *,
                             want=None, settle=0.0):
        self.asked.append(f"{command} {payload}".strip())
        return list(self.answers.get(command, []))


class Eng:
    def __init__(self, offline=(), tap_after=None):
        self._offline = set(offline)
        self.tap_after = tap_after          # calls before the card arrives
        self.calls = 0

    def hears(self, did, kind):
        return did == READER and kind == "tag"

    def last_tag(self, did):
        self.calls += 1
        if self.tap_after is not None and self.calls > self.tap_after:
            return ("04A1B2C3", time.time() + 1)
        return ("OLD999", 1.0)


class IO:
    renders_data = True

    def __init__(self):
        self.lines, self.frames = [], []

    async def write(self, text="", level=None, **_k):
        self.lines.append((str(text), level))

    async def send_data(self, kind, data):
        self.frames.append((kind, data))


class DM:
    board_device_id = staticmethod(DeviceManager.board_device_id)

    def __init__(self):
        self.devices = {READER: {"name": "card reader", "transport": "mqtt"},
                        "front.porch_light": {"name": "porch light",
                                              "transport": "mqtt"}}


def shell(answers=None, eng=None):
    sh = InteractiveShell.__new__(InteractiveShell)
    sh.dm, sh.esp, sh.automations = DM(), Esp(answers), eng
    sh._io = IO()
    sh.failure_note = None
    sh.current_speaker_id = None
    sh.READER_CHECK_SECONDS = 0.6
    return sh


def act(sh, did=READER):
    sh.failure_note = None
    return asyncio.run(sh.run_menu_action(f"checkreader:{did}", None))


def said(sh):
    return [t.strip() for t, _l in sh._io.lines if t.strip()]


GOOD = {"Status": ['{"StatusPRM":{"Baudrate":115200}}'],
        "GPIOs": HOMEVOICE_GPIOS, "Gpio": gpio_reply()}

print("\nthe parsing it leans on")
has = EspBuilder._pins_assigned(gpio_reply())
check("a split `Gpio 255` reply is read whole, pin by pin",
      has.get(5) == (6272, "RC522 CS") and has.get(19) == (672, "SPI MISO")
      and has.get(26) == (224, "Relay1"), has)
check("the short `GPIOs` list is not mistaken for pin assignments",
      EspBuilder._pins_assigned(HOMEVOICE_GPIOS) == {})
check("names come from the catalogue (Relay family numbered)",
      InteractiveShell._component_label(6272) == "RC522 CS"
      and InteractiveShell._component_label(225) == "Relay2"
      and InteractiveShell._component_label(99999, "Mystery") == "Mystery")

print("\n1. the board is offline")
sh = shell(GOOD, Eng(offline={"hv-front"}))
ok = act(sh)
check("its last will says offline: stops, says so",
      ok is False and sh.failure_note == "The Front door board is offline "
      "-- power it, or check its Wi-Fi.", sh.failure_note)
check("...without asking the board anything", sh.esp.asked == [],
      sh.esp.asked)
sh = shell({**GOOD, "Status": []}, Eng())
ok = act(sh)
check("no last will, but silent to a question: offline too",
      ok is False and "offline" in (sh.failure_note or "")
      and sh.esp.asked == ["Status 1"], (sh.failure_note, sh.esp.asked))

print("\n2. the firmware has no driver")
sh = shell({**GOOD, "GPIOs": STOCK_GPIOS}, Eng())
ok = act(sh)
check("stock Tasmota: no RC522 driver, reflash from the hub",
      ok is False and sh.failure_note == "This board's firmware has no "
      "RC522 driver (stock Tasmota). Reflash it from the hub so it gets "
      "HomeVoice's build.", sh.failure_note)
check("...said after 'online', and the pins are not asked",
      "The Front door board is online." in said(sh)
      and "Gpio 255" not in sh.esp.asked, (said(sh), sh.esp.asked))
sh = shell({**GOOD, "GPIOs": []}, Eng())
check("a board that does not list its drivers is not called stock",
      act(sh) is False and "did not say" in (sh.failure_note or ""),
      sh.failure_note)

print("\n3. a pin is not set as the layout says")
sh = shell({**GOOD, "Gpio": gpio_reply({5: (0, "None")})}, Eng())
ok = act(sh)
check("GPIO 5 empty on the board: named in words",
      ok is False and sh.failure_note == "GPIO 5 should be RC522 CS but the "
      "board has nothing there -- resend the layout from the hub.",
      sh.failure_note)
check("...after the firmware passed",
      "The firmware has the RC522 driver." in said(sh), said(sh))
sh = shell({**GOOD, "Gpio": gpio_reply({5: (0, "None"),
                                         18: (224, "Relay1")})}, Eng())
act(sh)
check("two wrong pins: both named, one thing to do",
      "GPIO 18 should be SPI CLK but the board has Relay1 there"
      in (sh.failure_note or "") and "GPIO 5 should be RC522 CS"
      in (sh.failure_note or "")
      and sh.failure_note.count("resend the layout") == 1, sh.failure_note)
sh = shell({**GOOD, "Gpio": []}, Eng())
check("no pin list at all: says so, not a mismatch",
      act(sh) is False and "did not say what its pins" in sh.failure_note,
      sh.failure_note)
sh = shell(GOOD, Eng(tap_after=1))
act(sh)
check("the porch light's pin is no business of the reader check",
      not any("GPIO 26" in t for t in said(sh)), said(sh))

print("\n4. a card is tapped")
sh = shell(GOOD, Eng(tap_after=1))
ok = act(sh)
check("every step passes and the UID is said",
      ok is True and said(sh)[-1] == "A card was read: 04A1B2C3", said(sh))
check("the person is told to tap first",
      "Tap a card on the reader now." in said(sh))
check("the questions asked, in order",
      sh.esp.asked == ["Status 1", "GPIOs", "Gpio 255"], sh.esp.asked)
busy = [d for k, d in sh._io.frames if k == "busy"]
check("the wait shows the countdown, then comes down",
      busy and busy[0].get("seconds") is not None
      and busy[-1] == {"done": True}, busy)
check("an earlier card is not taken for this tap",
      "OLD999" not in " ".join(said(sh)))
check("no failure note on success", sh.failure_note is None, sh.failure_note)

print("\n5. no tap in time")
sh = shell(GOOD, Eng(tap_after=None))
t0 = time.time()
ok = act(sh)
check("no tap: it's the wiring or power, said plainly",
      ok is False and sh.failure_note == "No tap arrived. The firmware and "
      "pins are right, so it's the wiring or power: the module needs 3.3 V "
      "(not 5 V), SDA goes to chip select, RST must be wired, IRQ stays "
      "unconnected, and SCK/MOSI/MISO must not be swapped.", sh.failure_note)
check("the listen time is the configured one", time.time() - t0 < 5)
sh = shell(GOOD, None)
check("no automation engine: nothing could hear a tap, said so",
      act(sh) is False and "automation engine" in sh.failure_note,
      sh.failure_note)

print("\nonly card readers")
sh = shell(GOOD, Eng())
check("a lamp is not checked as a reader",
      act(sh, "front.porch_light") is False
      and "not a card reader" in sh.failure_note, sh.failure_note)
check("...and asks nothing", sh.esp.asked == [])

print("\nthe device list marks readers for the app")
sh = shell(GOOD, None)
sh.esp.board_wiring = lambda rec: []


async def _states(dids):
    return {d: {} for d in dids}
sh._states_of = _states
sh._verified_recently = lambda: False
sh._layout_records = lambda: []
sh._board_parts = lambda rec: []
sh._board_display = lambda slug: {}
asyncio.run(sh.menu_list_devices())
frame = next((d for k, d in sh._io.frames if k == "device_list"), {})
recs = {r["id"]: r for r in frame.get("devices", [])}
check("the RC522 is a reader", recs.get(READER, {}).get("reader") is True,
      recs.get(READER))
check("the porch light is not", "reader" not in recs.get(
    "front.porch_light", {"reader": 1}), recs.get("front.porch_light"))

print("\nat every hub start: checked by itself, into the log")
import app.shell as _shell_mod                                   # noqa: E402
logged = []
real_event = _shell_mod.event
_shell_mod.event = lambda tag, text: logged.append((tag, text))
try:
    eng = Eng(tap_after=None)
    eng._pub = object()
    sh = shell(GOOD, eng)
    real_sleep = asyncio.sleep
    _shell_mod.asyncio.sleep = lambda *_a, **_k: real_sleep(0)
    try:
        asyncio.run(sh._reader_selfcheck())
    finally:
        _shell_mod.asyncio.sleep = real_sleep
    lines = [t for tag, t in logged if tag == "READER"]
    check("each step is written to the log, with no tap asked for",
          any("pins are set" in t for t in lines)
          and not any("Tap a card" in t for t in lines), lines)
    check("...ending with READY and where taps will appear",
          lines and "ready" in lines[-1] and "tapped" in lines[-1], lines)
    check("nothing is written for the app", sh._io.lines == [], sh._io.lines)
    logged.clear()
    sh = shell(dict(GOOD, GPIOs=['{"GPIOs1":{"0":"None"}}']), eng)
    _shell_mod.asyncio.sleep = lambda *_a, **_k: real_sleep(0)
    try:
        asyncio.run(sh._reader_selfcheck())
    finally:
        _shell_mod.asyncio.sleep = real_sleep
    lines = [t for tag, t in logged if tag == "READER"]
    check("no driver: the log says why, and NOT READY",
          any("no RC522 driver" in t for t in lines)
          and "NOT READY" in lines[-1], lines)
    sent = [d for k, d in sh._io.frames if k == "notice"]
    # 2026-11-13: no start-up notice any more -- the reader is checked when
    # a card is added, and an offline board has its own notice.
    check("...and no notification at start (checked when a card is added)",
          sent == [], sent)
    logged.clear()
    sh2 = shell(GOOD, eng)
    sh2._reader_problems = dict(sh._reader_problems)
    _shell_mod.asyncio.sleep = lambda *_a, **_k: real_sleep(0)
    try:
        asyncio.run(sh2._reader_selfcheck())
    finally:
        _shell_mod.asyncio.sleep = real_sleep
    check("fixed: the notification goes at the next check",
          READER not in sh2._reader_problems
          and not [d for k, d in sh2._io.frames if k == "notice"])
finally:
    _shell_mod.event = real_event

print(f"\n{PASSED} passed, {FAILED} failed")
sys.exit(1 if FAILED else 0)

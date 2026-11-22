"""WHAT EACH DEVICE DRAWS (2026-11-07): the hub learns roughly how much a
device draws from the jump in the MAIN METER's reading when that device
switches on or off. Nothing is hard-coded about meters: a board's meter is
the catalogue's energy part read through Tasmota's own ENERGY fields, and a
Home Assistant sensor says its own unit.

Run: python3 tests/test_device_draw.py
"""
import asyncio
import json
import os
import sys
import tempfile
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("HOMEVOICE_HOME", tempfile.mkdtemp())
ENGINE_PKG = "nlu"
A = __import__(f"{ENGINE_PKG}.automations", fromlist=["x"])
DMM = __import__(f"{ENGINE_PKG}.device_manager", fromlist=["x"])
from core import config as C                                     # noqa: E402
from app.shell import InteractiveShell                           # noqa: E402

PASSED = FAILED = 0


def check(name, ok, detail=""):
    global PASSED, FAILED
    PASSED, FAILED = PASSED + bool(ok), FAILED + (not ok)
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}   {'' if ok else detail}")


A.event = lambda *_a, **_k: None


class DM:
    def __init__(self):
        self.devices = {
            "kitchen.meter": {"name": "meter", "transport": "mqtt"},
            "lounge.kettle": {"name": "kettle", "transport": "mqtt"},
            "lounge.lamp": {"name": "lamp", "transport": "mqtt"},
        }
        self.units = {}

    def ha_unit(self, did):
        return self.units.get(did, "")

    def get_state(self, _did):
        return {}

    @staticmethod
    def board_device_id(slug, name):
        return f"{slug}.{name.lower()}"

    @staticmethod
    def is_readable_only(d):
        return str(d.get("ha_entity_id") or "").startswith("sensor.")

    def is_fresh(self, _did, _d=None):
        return True


class Pub:
    def __init__(self):
        self.sent = []

    def publish(self, topic, payload, *_a, **_k):
        self.sent.append((topic, payload))

        class Info:
            rc = 0
        return Info()


T0 = time.mktime((2026, 11, 7, 18, 0, 0, 0, 0, -1))
clock = [T0]


def engine(dm=None, path=None):
    e = A.AutomationEngine(dm or DM(), None, None,
                           path=path or os.path.join(tempfile.mkdtemp(), "a.json"),
                           now=lambda: clock[0])
    e._index = {"kitchen": {("energy", 0): "kitchen.meter"},
                "lounge": {("relay", 1): "lounge.kettle",
                           ("relay", 2): "lounge.lamp"}}
    return e


def meter(e, watts, amps=None, volts=230):
    e.feed("tele/kitchen/SENSOR", json.dumps({"ENERGY": {
        "Voltage": volts, "Power": watts,
        "Current": round(watts / 230.0, 3) if amps is None else amps}}))


def power(e, n, on):
    e.feed(f"stat/lounge/POWER{n}", "ON" if on else "OFF")


def wait(s):
    clock[0] += s


def settle(e):
    """Both relays off, a quiet house, a fresh meter reading."""
    power(e, 1, False)
    power(e, 2, False)
    wait(C.DRAW_WINDOW_SECONDS + 5)


def switch(e, n, on, before, after, *, gap=5, delay=None):
    """The meter reads `before`, relay n switches, the meter reads `after`."""
    meter(e, before)
    wait(gap)
    power(e, n, on)
    wait(C.DRAW_SETTLE_SECONDS + 1 if delay is None else delay)
    meter(e, after)
    wait(C.DRAW_WINDOW_SECONDS + 5)


# ── 1. the fixed list is gone ────────────────────────────────────────────
print("\nthe fixed meter-reading list is gone")
check("no _ENERGY_KEYS on the engine", not hasattr(A.AutomationEngine, "_ENERGY_KEYS"))
e = engine()
check("a meter not yet heard offers nothing yet",
      e.reading_keys("kitchen.meter") == [], e.reading_keys("kitchen.meter"))
check("...and no threshold on it (nothing it could be)",
      not [o for o in e.trigger_options("kitchen.meter") if o["kind"] == "threshold"],
      e.trigger_options("kitchen.meter"))
meter(e, 120)
check("once heard, it offers what it sent",
      {"power", "current", "voltage"} <= set(e.reading_keys("kitchen.meter"))
      and "energy_total" not in e.reading_keys("kitchen.meter"),
      e.reading_keys("kitchen.meter"))
check("...and a threshold on each",
      {o["key"] for o in e.trigger_options("kitchen.meter")
       if o["kind"] == "threshold"} >= {"power", "current", "voltage"})

# ── 2. recognising a meter from the meter itself ─────────────────────────
print("\nwhich readings are power or current: from the meter itself")
keys = {k["key"]: k["measures"] for k in e._meter_keys("kitchen.meter")}
check("board ENERGY: Power is power, Current is current, Voltage is neither",
      keys == {"power": "power", "current": "current"}, keys)
check("a relay is not a meter", e._meter_keys("lounge.kettle") == [])
dm = DM()
dm.devices.update({
    "ha.house": {"name": "house power", "ha_entity_id": "sensor.house_power",
                 "state": {"level": 450.0, "unit": "W"}},
    "ha.amps": {"name": "house current", "ha_entity_id": "sensor.house_current",
                "state": {"level": 2.0, "unit": "A"}},
    "ha.kw": {"name": "grid", "ha_entity_id": "sensor.grid",
              "state": {"level": 0.4, "unit": "kW"}},
    "ha.typed": {"name": "typed", "ha_entity_id": "sensor.typed",
                 "state": {"level": 9.0, "unit": "W"}},
    "ha.temp": {"name": "temp", "ha_entity_id": "sensor.temp",
                "state": {"level": 21.0, "unit": "°C"}},
})
dm.units = {"ha.house": "W", "ha.amps": "A", "ha.kw": "kW", "ha.temp": "°C"}
h = engine(dm)
check("Home Assistant W: power", h._meter_keys("ha.house")
      == [{"key": "level", "measures": "power", "scale": 1.0}], h._meter_keys("ha.house"))
check("Home Assistant A: current", h._meter_keys("ha.amps")
      == [{"key": "level", "measures": "current", "scale": 1.0}], h._meter_keys("ha.amps"))
check("Home Assistant kW: power, kept in W", h._meter_keys("ha.kw")
      == [{"key": "level", "measures": "power", "scale": 1000.0}], h._meter_keys("ha.kw"))
check("a unit typed on a form is not Home Assistant's: not a meter",
      h._meter_keys("ha.typed") == [])
check("a temperature is not a meter", h._meter_keys("ha.temp") == [])

# The unit kept is the one Home Assistant attached.
real = DMM.DeviceManager.__new__(DMM.DeviceManager)
real._lock = threading.RLock()
real._ha_seen, real._ha_units = {}, {}
real.devices = {"ha.house": {"name": "house power",
                             "ha_entity_id": "sensor.house_power",
                             "state": {"level": 0.0, "unit": "W"}}}
check("before Home Assistant answers, no unit is claimed", real.ha_unit("ha.house") == "")
real._take_ha_answer("ha.house", "sensor.house_power",
                     {"state": "512", "attributes": {"unit_of_measurement": "kW"}}, 1.0)
check("DeviceManager keeps Home Assistant's own unit_of_measurement",
      real.ha_unit("ha.house") == "kW", real._ha_units)

# ── 3. the main meter ────────────────────────────────────────────────────
print("\nthe main meter")
mm = e.main_meter()
check("one meter in the house: it is the main meter, on its power",
      mm and mm["device"] == "kitchen.meter" and mm["key"] == "power"
      and mm["measures"] == "power", mm)
check("several meters: none until the person picks one", h.main_meter() is None)
only_amps = DM()
only_amps.devices["ha.amps"] = dict(dm.devices["ha.amps"])
only_amps.units = {"ha.amps": "A"}
oa = engine(only_amps)
oa._index = {}
check("a meter with only current: its current",
      (oa.main_meter() or {}).get("measures") == "current", oa.main_meter())

# ── the hub menu actions (set, clear) through the shell ──────────────────


class IO:
    renders_data = True

    def __init__(self):
        self.lines, self.frames = [], []

    async def write(self, text, *_a, **_k):
        self.lines.append(str(text))

    async def send_data(self, kind, data):
        self.frames.append((kind, data))


class Esp:
    def list_nodes(self):
        return []

    def _read_board(self, _slug):
        return {}

    def board_wiring(self, _rec):
        return []

    def display_targets(self):
        return []


def shell(eng):
    sh = InteractiveShell.__new__(InteractiveShell)
    sh.dm, sh.esp, sh.automations = eng.dm, Esp(), eng
    sh._io = IO()
    sh.failure_note = ""
    sh.current_speaker_id = None

    async def states(dids):
        return {d: dict(eng.dm.devices[d].get("state") or {}) for d in dids}
    sh._states_of = states
    sh._verified_recently = lambda: False
    sh._layout_records = lambda: []
    return sh


def act(sh, action):
    sh.failure_note = ""
    return asyncio.run(sh.run_menu_action(action, None))


print("\nthe menu actions")
sh = shell(h)
check("mainmeter:<did>|<key>|power sets it",
      act(sh, "mainmeter:ha.house|level|power")
      and h.main_meter()["device"] == "ha.house", (h.main_meter(), sh.failure_note))
check("...said in a short sentence",
      any("main meter now" in t for t in sh._io.lines), sh._io.lines)
check("a power reading cannot be called current",
      not act(sh, "mainmeter:ha.house|level|current")
      and "power" in sh.failure_note, sh.failure_note)
check("a reading that is not power or current is refused",
      not act(sh, "mainmeter:ha.temp|level|power") and sh.failure_note,
      sh.failure_note)
check("...and so is a kind that is neither",
      not act(sh, "mainmeter:ha.house|level|volts")
      and sh.failure_note == "Say power or current.", sh.failure_note)
check("mainmeter:none clears it", act(sh, "mainmeter:none")
      and h.main_meter() is None and h._main_meter is None)
se = engine()
meter(se, 100)
ssh = shell(se)
act(ssh, "mainmeter:none")
check("...and the one meter in the house is not then chosen on its own",
      se.main_meter() is None, se.main_meter())
act(ssh, "mainmeter:kitchen.meter|current|current")
check("picking again works, on the reading asked for",
      se.main_meter() == {"device": "kitchen.meter", "key": "current",
                          "measures": "current"}, se.main_meter())

# ── 4. learning ──────────────────────────────────────────────────────────
print("\nlearning what a device draws")
clock[0] = T0
L = engine()
pub = Pub()
L._pub = pub
settle(L)
switch(L, 1, True, 300, 2300)
check("a clean jump is learned", L._draws.get("lounge.kettle") == [2000.0],
      L._draws)
check("...but one sample is not shown yet", L.draw_of("lounge.kettle") is None)
switch(L, 1, False, 2400, 300)
check("switching off is learned too (a drop)",
      L._draws.get("lounge.kettle") == [2000.0, 2100.0], L._draws)
check("two samples: still not shown", L.draw_of("lounge.kettle") is None)
switch(L, 1, True, 300, 2200)
d = L.draw_of("lounge.kettle")
check("three samples: shown, as the median",
      d == {"value": 2000, "unit": "W", "samples": 3}, d)
switch(L, 1, False, 2300, 300)
switch(L, 1, True, 300, 2400)
check("...the median, not the mean (one odd jump does not move it)",
      L.draw_of("lounge.kettle")["value"] == 2000
      and sorted(L._draws["lounge.kettle"]) == [1900.0, 2000.0, 2000.0, 2100.0, 2100.0],
      L._draws)
for i in range(8):
    switch(L, 1, i % 2 == 1, 2300 if i % 2 == 0 else 300,
           300 if i % 2 == 0 else 2300)
check("only the last 10 samples are kept",
      len(L._draws["lounge.kettle"]) == A.AutomationEngine.DRAW_SAMPLES_KEPT,
      L._draws)
check("the meter itself never gets a draw", "kitchen.meter" not in L._draws)

print("\nunclean measurements are refused")
before = list(L._draws["lounge.kettle"])
# Another device switched just before.
meter(L, 300)
power(L, 2, True)
wait(5)
power(L, 1, False)
wait(C.DRAW_SETTLE_SECONDS + 1)
meter(L, 200)
wait(C.DRAW_WINDOW_SECONDS + 5)
check("another device switched just before: refused",
      L._draws["lounge.kettle"] == before and "lounge.lamp" not in L._draws,
      L._draws)
# Another device switches before the "after" reading.
meter(L, 300)
wait(5)
power(L, 1, True)
wait(1)
power(L, 2, False)
wait(C.DRAW_SETTLE_SECONDS + 1)
meter(L, 2300)
wait(C.DRAW_WINDOW_SECONDS + 5)
check("another device switched before the 'after': refused",
      L._draws["lounge.kettle"] == before and "lounge.lamp" not in L._draws,
      L._draws)
# A stale "before".
meter(L, 2300)
wait(C.METER_FRESH_SECONDS + 10)
power(L, 1, False)
wait(C.DRAW_SETTLE_SECONDS + 1)
meter(L, 300)
wait(C.DRAW_WINDOW_SECONDS + 5)
check("a stale 'before' reading: refused", L._draws["lounge.kettle"] == before,
      L._draws)
# Wrong direction.
switch(L, 1, True, 300, 250)
check("switched on but the meter fell: refused",
      L._draws["lounge.kettle"] == before, L._draws)
switch(L, 1, False, 300, 300)
check("no jump at all: refused", L._draws["lounge.kettle"] == before, L._draws)
# An "after" too soon, or too late.
meter(L, 300)
wait(5)
power(L, 1, True)
wait(1)
meter(L, 2300)                        # before the meter caught up: skipped
wait(C.DRAW_WINDOW_SECONDS)
meter(L, 2300)                        # too late
wait(C.DRAW_WINDOW_SECONDS + 5)
check("an 'after' too soon is skipped, and one too late is refused",
      L._draws["lounge.kettle"] == before, L._draws)
# The silent meter.
power(L, 1, False)
wait(C.DRAW_WINDOW_SECONDS + 5)
L.feed("tele/kitchen/SENSOR", '{"ENERGY":{"Voltage":0,"Power":0,"Current":0,"Address":0}}')
wait(5)
power(L, 1, True)
wait(C.DRAW_SETTLE_SECONDS + 1)
meter(L, 2300)
wait(C.DRAW_WINDOW_SECONDS + 5)
check("a silent meter (every address 0, no volts): nothing learned",
      L._draws["lounge.kettle"] == before, L._draws)
meter(L, 2300)
power(L, 1, False)
wait(C.DRAW_SETTLE_SECONDS + 1)
L.feed("tele/kitchen/SENSOR", '{"ENERGY":{"Voltage":0,"Power":0,"Current":0,"Address":0}}')
wait(C.DRAW_WINDOW_SECONDS + 5)
check("...nor when it falls silent before the 'after'",
      L._draws["lounge.kettle"] == before, L._draws)
power(L, 1, False)
wait(C.DRAW_WINDOW_SECONDS + 5)
meter(L, 300)
wait(5)
L.feed("stat/lounge/POWER1", "ON", retained=True)
wait(C.DRAW_SETTLE_SECONDS + 1)
meter(L, 2300)
wait(C.DRAW_WINDOW_SECONDS + 5)
check("...nothing learned from a replayed state", L._draws["lounge.kettle"] == before,
      L._draws)

print("\na second device")
power(L, 2, False)
wait(C.DRAW_WINDOW_SECONDS + 5)
switch(L, 2, True, 300, 360)
check("each device is learned on its own",
      L._draws.get("lounge.lamp") == [60.0], L._draws)

print("\nasking a board meter for a fresh reading (Status 10)")
pub.sent.clear()
power(L, 2, False)
wait(C.DRAW_WINDOW_SECONDS + 5)
meter(L, 360)
wait(5)
power(L, 2, True)
L._draw_tick()
check("not asked before the meter can have caught up", not pub.sent, pub.sent)
wait(C.DRAW_SETTLE_SECONDS)
L._draw_tick()
check("then asked once: cmnd/<board>/Status 10",
      pub.sent == [("cmnd/kitchen/Status", "10")], pub.sent)
L._draw_tick()
check("...only once", len(pub.sent) == 1, pub.sent)
reply = ('{"StatusSNS":{"Time":"2026-11-07T18:00:00","ENERGY":{"Total":12.5,'
         '"Power":420,"Voltage":231,"Current":1.82,"Address":1}}}')
got = L.feed("stat/kitchen/STATUS10", reply)
check("its reply is read like tele SENSOR's ENERGY",
      ("kitchen.meter", "power", 420.0) in got
      and ("kitchen.meter", "current", 1.82) in got
      and ("kitchen.meter", "energy_total", 12.5) in got, got)
check("...and is the 'after' reading", L._draws["lounge.lamp"] == [60.0, 60.0],
      L._draws)
check("a STATUS10 on tele/ is not that reply", L.feed("tele/kitchen/STATUS10", reply) == [])
check("other Status answers are not read as readings",
      L.feed("stat/kitchen/STATUS2", '{"StatusFWR":{"Version":"14.4.1"}}') == [])
power(L, 2, False)
wait(C.DRAW_SETTLE_SECONDS + 1)
L._offline.add("kitchen")
pub.sent.clear()
L._draw_tick()
check("an offline meter board is not asked", not pub.sent, pub.sent)
L._offline.discard("kitchen")
wait(C.DRAW_WINDOW_SECONDS + 1)
L._draw_tick()
check("a jump with no 'after' in time is dropped", not L._draw_pending, L._draw_pending)

print("\npersistence across a restart")
L._save_state()
R = A.AutomationEngine(DM(), None, None, path=L.path, now=lambda: clock[0])
R._index = dict(L._index)
check("the samples come back", R._draws == L._draws, (R._draws, L._draws))
check("...and the draw is shown at once",
      R.draw_of("lounge.kettle") == L.draw_of("lounge.kettle"), R.draw_of("lounge.kettle"))
P = engine()
meter(P, 100)
act(shell(P), "mainmeter:kitchen.meter|current|current")
P._save_state()
P2 = A.AutomationEngine(DM(), None, None, path=P.path, now=lambda: clock[0])
check("a chosen main meter comes back",
      P2._main_meter == {"device": "kitchen.meter", "key": "current",
                         "measures": "current"}, P2._main_meter)
act(shell(P), "mainmeter:none")
P3 = A.AutomationEngine(DM(), None, None, path=P.path, now=lambda: clock[0])
check("...and so does 'no main meter'", P3.main_meter() is None and P3._main_meter_off)

print("\nchanging the main meter")
check("picking the meter already in use keeps what was learned",
      act(shell(R), "mainmeter:kitchen.meter|power|power") and R._draws, R._draws)
check("picking another forgets every sample",
      act(shell(R), "mainmeter:kitchen.meter|current|current") and R._draws == {},
      R._draws)
settle(R)
switch(R, 1, True, 300, 2300)
switch(R, 1, False, 2300, 300)
switch(R, 1, True, 300, 2300)
d = R.draw_of("lounge.kettle")
check("then it learns again, in the new meter's unit",
      d and d["unit"] == "A" and d["value"] == 8.7, d)

print("\nForget")
ok = act(shell(R), "forgetdraw:lounge.kettle")
check("forgetdraw:<did> forgets one device", ok and "lounge.kettle" not in R._draws
      and R.draw_of("lounge.kettle") is None, R._draws)
check("...an unknown device is said so", not act(shell(R), "forgetdraw:nowhere.x"))

print("\nthe device list frame")
F = engine()
F.dm.devices["lounge.kettle"]["secure"] = False
settle(F)
for i in range(3):
    switch(F, 1, i % 2 == 0, 300 if i % 2 == 0 else 2300, 2300 if i % 2 == 0 else 300)
fsh = shell(F)
asyncio.run(fsh.menu_list_devices())
frame = next(d for k, d in fsh._io.frames if k == "device_list")
recs = {r["id"]: r for r in frame["devices"]}
check("a learned device carries draw",
      recs["lounge.kettle"].get("draw") == {"value": 2000, "unit": "W", "samples": 3},
      recs["lounge.kettle"])
check("one not learned does not", "draw" not in recs["lounge.lamp"])
m = recs["kitchen.meter"].get("meter")
check("the meter carries meter: its keys and whether it is the main one",
      m and m["main"] is True and {k["key"]: k["measures"] for k in m["keys"]}
      == {"power": "power", "current": "current"}, m)
check("a device that is not a meter has no meter field", "meter" not in recs["lounge.lamp"])
check("the frame says which is the main meter",
      (frame.get("main_meter") or {}).get("device") == "kitchen.meter", frame.get("main_meter"))
F.dm.devices["lounge.kettle"]["secure"] = True
fsh._io.frames.clear()
asyncio.run(fsh.menu_list_devices())
frame = next(d for k, d in fsh._io.frames if k == "device_list")
check("a secure device's draw waits for a check, like its state",
      "draw" not in {r["id"]: r for r in frame["devices"]}["lounge.kettle"])

print(f"\n{PASSED} passed, {FAILED} failed")
sys.exit(1 if FAILED else 0)

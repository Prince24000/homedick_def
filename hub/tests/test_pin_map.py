"""The pin map round trip: what the phone's pin screen sends -> BoardPlan.

The phone (screens/pin_map_screen.dart) decides at placement time what it
will let a person put on a board, and the hub (EspBuilder._apply ->
BoardPlan.add) decides again at Save. When the two disagree, the phone
shows a finished board and the hub quietly drops a device from it. This
file holds the phone's rules, transcribed from the Dart (class App below),
and checks both directions on the same layouts:

  * everything the phone would send is accepted by the hub, whole;
  * everything the phone refuses at placement, forced into a payload,
    is refused by the hub ("was not added").

Run: python3 tests/test_pin_map.py   (from anywhere; no hardware)
"""
import os
import random
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("HOMEVOICE_HOME", tempfile.mkdtemp())

from esp_firmware import esphome_catalog as cat          # noqa: E402
from esp_firmware.esp_builder import EspBuilder           # noqa: E402

PASSED = FAILED = 0


def check(name, ok, detail=""):
    global PASSED, FAILED
    if ok:
        PASSED += 1
        print(f"  [PASS] {name}")
    else:
        FAILED += 1
        print(f"  [FAIL] {name}   {detail}")


CAT = cat.describe()
TYPES = {t["id"]: t for t in CAT["types"]}
PSRAM = sorted(cat.PSRAM_PINS)


# ── the phone, transcribed ──────────────────────────────────────────────

class App:
    """The pin screen's rules (pin_map_screen.dart), per frame.

    `owned` is the frame's pins_owned_by_module: AppState._handlePinMap
    gives those pins can=[] and severity 'impossible'.
    """

    def __init__(self, owned=(), catalogue=None):
        catalogue = catalogue or CAT
        self.types = {t["id"]: t for t in catalogue["types"]}
        self.pins = {}
        for p in catalogue["pins"]:
            p = dict(p)
            if p["n"] in owned:
                p.update(can=[], severity="impossible")
            self.pins[p["n"]] = p
        self.placed = []     # {"type_id","name","pins","confirmed"}

    # _fits / _taken / _blocked
    def fits(self, gpio, slot):
        return slot["role"] in self.pins[gpio]["can"]

    def taken(self):
        out = {}
        for p in self.placed:
            for g in p["pins"].values():
                out.setdefault(g, p)
        return out

    def blocked(self, gpio):
        pin = self.pins.get(gpio)
        return pin is None or pin["severity"] == "impossible"

    # _busKeyIn / _sharedPinFor
    def bus_key_in(self, p, slot):
        if not slot.get("shared"):
            return None
        for os_ in self.types.get(p["type_id"], {}).get("slots", []):
            if os_.get("shared") and os_.get("family") == slot.get("family"):
                return os_["key"]
        return None

    def shared_pin_for(self, slot):
        if not slot.get("shared"):
            return None
        for p in self.placed:
            key = self.bus_key_in(p, slot)
            if key is not None and p["pins"].get(key) is not None:
                return p["pins"][key]
        return None

    # _familyFull
    def family_full(self, t):
        counts = {}
        for p in self.placed:
            for sl in self.types.get(p["type_id"], {}).get("slots", []):
                if sl.get("shared") or sl.get("family") is None:
                    continue
                counts[sl["family"]] = counts.get(sl["family"], 0) + 1
        for sl in t["slots"]:
            if sl.get("shared") or sl.get("family") is None:
                continue
            counts[sl["family"]] = counts.get(sl["family"], 0) + 1
            if counts[sl["family"]] > sl["limit"]:
                return "family full"
        return None

    # _nameProblem (+ _askName: an empty name is no name)
    def name_problem(self, name):
        want = name.strip().lower()
        if not want:
            return "empty"
        if ";" in want:
            return "semicolon"
        if any(p["name"].strip().lower() == want for p in self.placed):
            return "duplicate"
        return None

    def place(self, item):
        """Would the screen produce `item`? (accepted, why). Walks the
        device-first path: _addDevice -> _adoptBuses -> _tapPin /
        _fillAwaitingSlot per open slot -> _askName. The entry-by-pin path
        (_offerDevices) applies the same rules to its first pin."""
        t = self.types.get(item["type_id"])
        if t is None:
            return False, "no such type"
        if self.family_full(t):
            return False, "family full"
        want = item["pins"]
        awaiting, confirmed = {}, False
        for sl in t["slots"]:                       # _adoptBuses
            bus = self.shared_pin_for(sl)
            if bus is None:
                continue
            if want.get(sl["key"]) != bus:
                return False, "bus is elsewhere (adopted, not choosable)"
            awaiting[sl["key"]] = bus
            if not self.fits(bus, sl):
                confirmed = True
        for sl in t["slots"]:
            key = sl["key"]
            if key in awaiting:
                continue
            if key not in want:
                return False, "incomplete"
            gpio = want[key]
            pin = self.pins.get(gpio)
            if pin is None:
                return False, "not on the board"      # nothing to tap
            taken = self.taken()
            if pin["severity"] == "impossible" and gpio not in taken:
                return False, "impossible"
            if gpio in taken:
                return False, "taken"
            if gpio in awaiting.values():
                return False, "own line twice"
            if not self.fits(gpio, sl):
                if not item.get("confirmed"):
                    return False, "declined won't-work"
                confirmed = True
            awaiting[key] = gpio
        if set(want) - set(awaiting):
            return False, "a line the device does not have"
        why = self.name_problem(item["name"])
        if why:
            return False, why
        self.placed.append({"type_id": t["id"], "name": item["name"].strip(),
                            "pins": dict(awaiting), "confirmed": confirmed})
        return True, ""

    def seed(self, item):
        """initState/_adopt: a saved record, as the frame's items carry it."""
        t = self.types.get(item["type_id"])
        known = {sl["key"] for sl in (t or {}).get("slots", [])}
        pins = {k: v for k, v in item["pins"].items()
                if isinstance(v, int) and (not known or k in known)}
        confirmed = bool(item.get("confirmed")) or any(
            g in self.pins and k in known and not self.fits(
                g, next(sl for sl in t["slots"] if sl["key"] == k))
            for k, g in pins.items())
        self.placed.append({"type_id": item["type_id"], "name": item["name"],
                            "pins": pins, "confirmed": confirmed})

    def problems(self, p):
        """_problems: what holds Save for this device."""
        t = self.types.get(p["type_id"])
        if t is None:
            return ["unknown type"]
        out = [sl["key"] for sl in t["slots"] if sl["key"] not in p["pins"]]
        out += [f"blocked {k}" for k, g in p["pins"].items() if self.blocked(g)]
        return out

    def move(self, p, key, gpio):
        """_tapPin -> _finishMove. (moved, why)."""
        t = self.types[p["type_id"]]
        slot = next(sl for sl in t["slots"] if sl["key"] == key)
        pin = self.pins.get(gpio)
        if pin is None:
            return False, "not on the board"
        if pin["severity"] == "impossible" and gpio not in self.taken():
            return False, "impossible"
        shared = bool(slot.get("shared"))
        for q in self.placed:
            bus_key = self.bus_key_in(q, slot) if shared else None
            for k, g in q["pins"].items():
                if g != gpio:
                    continue
                if k == bus_key or (q is p and k == key):
                    continue
                return False, "clash"
        if not self.fits(gpio, slot):
            p["confirmed"] = True                    # "use it anyway"
            if shared:
                for q in self.placed:
                    bk = self.bus_key_in(q, slot)
                    if bk is not None and bk in q["pins"]:
                        q["confirmed"] = True
        if shared:
            for q in self.placed:
                bk = self.bus_key_in(q, slot)
                if bk is not None and bk in q["pins"]:
                    q["pins"][bk] = gpio
        p["pins"][key] = gpio
        return True, ""

    def can_save(self):
        return bool(self.placed) and not any(self.problems(p)
                                             for p in self.placed)

    def payload(self):
        """_send: exactly the frame the screen sends."""
        return [dict({"type_id": p["type_id"], "name": p["name"],
                      "pins": dict(p["pins"])},
                     **({"confirmed": True} if p["confirmed"] else {}))
                for p in self.placed]


# ── the hub ─────────────────────────────────────────────────────────────

def hub(items, owns=None):
    """(plan, refusals, cautions) for a pin_map_response's items."""
    plan = cat.BoardPlan("Test", owns_psram_pins=owns)
    notes = EspBuilder._apply(plan, items, from_phone=True)
    refused = [n for n in notes if "was not added" in n]
    return plan, refused, [n for n in notes if n not in refused]


def owned_for(owns):
    """What esp_builder puts in pins_owned_by_module for this board."""
    return PSRAM if owns else []


def agree(label, steps, owns=None, expect=None):
    """Place `steps` one by one on the phone model; after each, the hub must
    agree: an accepted step leaves the whole payload accepted, a refused
    step (forced into the payload) is refused by the hub."""
    app = App(owned=owned_for(owns))
    outcomes = []
    for step in steps:
        ok, why = app.place(step)
        outcomes.append(ok)
        if ok:
            plan, refused, _ = hub(app.payload(), owns)
            check(f"{label}: {step['name']!r} placed -> hub keeps it",
                  not refused and len(plan.items) == len(app.placed),
                  f"hub refused {refused}")
        else:
            plan, refused, _ = hub(app.payload() + [step], owns)
            check(f"{label}: {step['name']!r} refused ({why}) -> hub refuses",
                  len(refused) == 1 and len(plan.items) == len(app.placed),
                  f"hub: refused={refused} items={len(plan.items)}")
    if expect is not None:
        check(f"{label}: phone outcomes {expect}", outcomes == expect,
              f"got {outcomes}")
    return app


def item(type_id, name, confirmed=False, **pins):
    out = {"type_id": type_id, "name": name, "pins": pins}
    if confirmed:
        out["confirmed"] = True
    return out


# ── the layouts ─────────────────────────────────────────────────────────

print("\nOne pin, one device")
agree("relay on D26", [item("relay", "kettle", pin=26)], expect=[True])
agree("relay on input-only 34, confirmed",
      [item("relay", "kettle", True, pin=34)], expect=[True])
agree("relay on 34, 'pick another pin'",
      [item("relay", "kettle", pin=34)], expect=[False])
agree("relay on 16, board not met yet (PSRAM unknown: asked at install)",
      [item("relay", "kettle", True, pin=16)], owns=None, expect=[True])
agree("relay on 17, board keeps its RAM there",
      [item("relay", "kettle", True, pin=17)], owns=True, expect=[False])
agree("relay on 16, board known free",
      [item("relay", "kettle", pin=16)], owns=False, expect=[True])
agree("relay on GPIO0 (off the 30-pin header)",
      [item("relay", "boot relay", pin=0)], expect=[True])
agree("relay on GPIO6 (flash; not drawn)",
      [item("relay", "flash relay", True, pin=6)], expect=[False])
agree("touch button on D4", [item("touch", "pad", pin=4)], expect=[True])
agree("touch button on D18 (no touch channel), confirmed",
      [item("touch", "pad", True, pin=18)], expect=[True])
agree("analog on VP", [item("analog", "pot", pin=36)], expect=[True])
agree("analog on D25 (ADC2), declined",
      [item("analog", "pot", pin=25)], expect=[False])

print("\nNames")
agree("same name, other case", [item("relay", "Lamp", pin=26),
                                 item("relay", "lamp ", pin=27)],
      expect=[True, False])
agree("semicolon in a name", [item("relay", "a;b", pin=26)], expect=[False])
agree("blank name", [item("relay", "   ", pin=26)], expect=[False])

print("\nOccupancy")
agree("two relays on D26", [item("relay", "one", pin=26),
                            item("relay", "two", pin=26)],
      expect=[True, False])
agree("H-bridge on one pin twice", [item("hbridge", "motor", fwd=25, rev=25)],
      expect=[False])
agree("H-bridge 25/26", [item("hbridge", "motor", fwd=25, rev=26)],
      expect=[True])
agree("HC-SR04 echo on input-only 34",
      [item("hcsr04", "tank", trigger=5, echo=34)], expect=[True])
agree("HC-SR04 trigger on 34, confirmed",
      [item("hcsr04", "tank", True, trigger=34, echo=35)], expect=[True])
agree("RGB light 25/26/27", [item("rgb", "strip", red=25, green=26, blue=27)],
      expect=[True])
agree("servo: relay enable + PWM", [item("servo", "arm", enable=13, pwm=14)],
      expect=[True])

print("\nShared buses")
agree("two I2C sensors share SDA/SCL", [
    item("bme280", "climate", sda=21, scl=22),
    item("sht3x", "bathroom", sda=21, scl=22)], expect=[True, True])
agree("second I2C sensor on other pins", [
    item("bme280", "climate", sda=21, scl=22),
    item("sht3x", "bathroom", sda=25, scl=26)], expect=[True, False])
agree("fresh I2C bus on a relay's pin", [
    item("relay", "kettle", pin=21),
    item("bme280", "climate", sda=21, scl=22)], expect=[True, False])
agree("I2C with SDA == SCL", [item("bme280", "climate", sda=21, scl=21)],
      expect=[False])
agree("card reader (SPI)", [
    item("rc522", "door", cs=5, rst=4, clk=18, mosi=23, miso=19)],
      expect=[True])
agree("bus confirmed onto 34 is joined with the override", [
    item("bme280", "climate", True, sda=21, scl=34),
    item("sht3x", "bathroom", sda=21, scl=34)], expect=[True, True])

print("\nFamily limits (describe() now sends family/limit per line)")
agree("second DS18B20 (a one-off family)", [
    item("ds18b20", "water", pin=4), item("ds18b20", "air", pin=13)],
      expect=[True, False])
agree("second card reader", [
    item("rc522", "door", cs=5, rst=4, clk=18, mosi=23, miso=19),
    item("rc522", "gate", cs=13, rst=14, clk=18, mosi=23, miso=19)],
      expect=[True, False])
agree("rotary: two fit, a third does not", [
    item("rotary", "knob 1", a=32, b=33), item("rotary", "knob 2", a=25, b=26),
    item("rotary", "knob 3", a=27, b=14)], expect=[True, True, False])
agree("DHT11: four fit, a fifth does not; a DHT22 is its own family", [
    item("dht11", f"dht {i}", pin=g) for i, g in
    enumerate([4, 13, 14, 25, 26], 1)] + [item("dht22", "attic", pin=27)],
      expect=[True, True, True, True, False, True])
pwm_pins = [0, 2, 4, 5, 12, 13, 14, 15, 18, 19, 21, 22, 23, 25, 26, 27, 32]
agree("PWM: sixteen channels fit, the seventeenth does not", [
    item("rgb", f"rgb {i}", red=pwm_pins[3 * i], green=pwm_pins[3 * i + 1],
         blue=pwm_pins[3 * i + 2]) for i in range(5)] + [
    item("dimmer", "dimmer 1", pin=pwm_pins[15]),
    item("dimmer", "dimmer 2", pin=pwm_pins[16])],
      expect=[True] * 6 + [False])
nine = agree("nine relays (FriendlyName has 8 slots; the ninth is named "
             "later, not dropped)",
             [item("relay", f"relay {i}", pin=g) for i, g in
              enumerate([4, 5, 13, 14, 18, 19, 21, 22, 23], 1)],
             expect=[True] * 9)
plan, refused, _ = hub(nine.payload())
check("nine relays: all nine in the plan, eight FriendlyName lines",
      len(plan.items) == 9 and len(plan.friendly_names()) == 8,
      f"items={len(plan.items)} names={plan.friendly_names()}")

print("\nEditing a saved board (the frame's items)")


def seeded(label, record, owns, save_ok):
    app = App(owned=owned_for(owns))
    for it in record:
        app.seed(it)
    plan, refused, _ = hub(app.payload(), owns)
    check(f"{label}: phone {'lets Save' if save_ok else 'holds Save'}",
          app.can_save() == save_ok,
          f"problems={[app.problems(p) for p in app.placed]}")
    if save_ok:
        check(f"{label}: hub keeps all", not refused, f"{refused}")
    else:
        check(f"{label}: hub would have dropped it", bool(refused),
              "the hub accepted what the phone holds")
    return app


seeded("power meter on TX2, board's PSRAM still unknown",
       [item("pzem", "meter", tx=17, rx=16)], owns=None, save_ok=True)
seeded("power meter on TX2, board said its RAM is there",
       [item("pzem", "meter", tx=17, rx=16)], owns=True, save_ok=False)
seeded("power meter on TX2/RX2, board known free",
       [item("pzem", "meter", tx=17, rx=16)], owns=False, save_ok=True)
seeded("relay saved on 34 without `confirmed` in the record",
       [item("relay", "kettle", pin=34)], owns=False, save_ok=True)
seeded("a type the catalogue no longer has",
       [item("relay", "kettle", pin=26), item("gone_part", "old", pin=27)],
       owns=False, save_ok=False)
seeded("card reader saved before it gained its reset line",
       [item("rc522", "door", cs=5, clk=18, mosi=23, miso=19)],
       owns=False, save_ok=False)
stale = seeded("a line the catalogue dropped is not carried",
               [item("relay", "kettle", pin=26, old_led=27)],
               owns=False, save_ok=True)
check("stale line: GPIO27 is free again on the phone",
      27 not in stale.taken())

print("\nMoving lines")
app = App(owned=PSRAM)
app.place(item("bme280", "climate", sda=21, scl=22))
app.place(item("sht3x", "bathroom", sda=21, scl=22))
app.place(item("relay", "kettle", pin=26))
moved, _ = app.move(app.placed[0], "sda", 25)
plan, refused, _ = hub(app.payload())
check("move a bus line: every rider follows, hub keeps all",
      moved and all(p["pins"].get("sda") == 25 for p in app.placed[:2])
      and not refused, f"{app.payload()} {refused}")
check("move a bus line onto the relay: refused on the phone",
      app.move(app.placed[1], "scl", 26) == (False, "clash"))
check("move a bus line onto its own other line: refused",
      app.move(app.placed[0], "sda", 22) == (False, "clash"))
moved, _ = app.move(app.placed[2], "pin", 34)
plan, refused, _ = hub(app.payload())
check("move the relay to 34 ('use it anyway'): hub keeps it",
      moved and app.placed[2].get("confirmed") and not refused, f"{refused}")
check("move onto the module's GPIO16: refused on the phone",
      app.move(app.placed[2], "pin", 16) == (False, "impossible"))

print("\nRandom layouts: the phone and the hub agree on every step")
rng = random.Random(20260923)
ids = sorted(TYPES)
names = ["lamp", "Lamp", "fan", "pump", "a;b", "", "heater", "door",
         "gate", "tank", "sensor", "light"]
mismatches = []
for _trial in range(400):
    owns = rng.choice([None, True, False])
    app = App(owned=owned_for(owns))
    for _ in range(rng.randint(1, 12)):
        t = TYPES[rng.choice(ids)]
        pins = {}
        for sl in t["slots"]:
            bus = app.shared_pin_for(sl)
            pins[sl["key"]] = (bus if bus is not None and rng.random() < 0.8
                               else rng.choice(list(range(40))))
        cand = item(t["id"], rng.choice(names) + str(rng.randint(0, 3)),
                    rng.random() < 0.5, **pins)
        before = app.payload()
        ok, why = app.place(cand)
        plan, refused, _ = hub(app.payload() if ok else before + [cand], owns)
        hub_ok = not refused
        if ok != hub_ok:
            mismatches.append((owns, before, cand, why, refused))
check("400 random boards: no step where phone and hub disagree",
      not mismatches, f"{len(mismatches)} mismatches, first: "
      f"{mismatches[0] if mismatches else ''}")

print(f"\n{PASSED} passed / {FAILED} failed")
sys.exit(1 if FAILED else 0)

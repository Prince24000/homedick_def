"""The automation engine, end to end, on a fake clock and a fake device
manager: the 2026-10-01 audit fixes and the per-rule override timer.

    python3 tests/test_automation_engine.py
"""
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_HOME = tempfile.mkdtemp()
os.environ["HOMEVOICE_HOME"] = _HOME
os.environ["HOMEVOICE_BOARDS_DIR"] = tempfile.mkdtemp()

from nlu import automations as A          # noqa: E402

PASSED = FAILED = 0


def check(label, ok, extra=""):
    global PASSED, FAILED
    if ok:
        PASSED += 1
        print(f"  [PASS] {label}")
    else:
        FAILED += 1
        print(f"  [FAIL] {label} {extra}")


class Clock:
    def __init__(self, t=1_000_000.0):
        self.t = t

    def __call__(self):
        return self.t


class DM:
    def __init__(self):
        self.devices = {}
        self.calls = []
        self.fail = False
        self.mqtt_driver = None

    def add(self, did, **kw):
        self.devices[did] = dict(name=did, state={"on": False},
                                 transport="mqtt", **kw)

    def get_state(self, did):
        return dict(self.devices[did]["state"])

    def set_state(self, did, _driver=None, **kw):
        self.calls.append((did, kw))
        if self.fail:
            return False
        self.devices[did]["state"].update(kw)
        return True


def mk(path=None, clock=None, dm=None, mono=None):
    clock = clock or Clock()
    dm = dm or DM()
    path = path or os.path.join(tempfile.mkdtemp(), "a.json")
    kw = {"mono": mono} if mono else {}
    e = A.AutomationEngine(dm, None, None, path=path, now=clock, **kw)
    e.history_path = os.path.join(tempfile.mkdtemp(), "h.json")
    return e, dm, clock


def motion_rule(**extra):
    r = {"name": "motion light",
         "trigger": {"kind": "event", "device": "pir", "value": "on"},
         "action": {"kind": "device", "device": "lamp", "set": {"on": True}},
         "revert": {"after_s": 120, "actions": [
             {"kind": "device", "device": "lamp", "set": {"on": False}}]}}
    r.update(extra)
    return r


def with_devices(dm):
    dm.add("lamp")
    dm.add("pir")
    dm.add("door")
    dm.add("temp")


print("\n== the store: two writers, bad ids, a failing disk")
e1, dm, clock = mk()
e2 = A.AutomationEngine(dm, None, None, path=e1.path, now=clock)
e2.history_path = e1.history_path
ok1, _ = e1.add({"name": "one", "trigger": {"kind": "time", "at": "07:00"},
                 "action": {"kind": "notify", "text": "hi"}})
ok2, _ = e2.add({"name": "two", "trigger": {"kind": "time", "at": "08:00"},
                 "action": {"kind": "notify", "text": "hi"}})
disk = json.load(open(e1.path))["rules"]
check("two engines on one file keep BOTH rules",
      ok1 and ok2 and {r["name"] for r in disk} == {"one", "two"})
e1.tick()
check("the first engine picks up the other's rule on its next tick",
      {r["name"] for r in e1.rules} == {"one", "two"})
ok, why = e1.add({"name": "ONE", "trigger": {"kind": "time", "at": "09:00"},
                  "action": {"kind": "notify", "text": "x"}})
check("a name already on disk is refused even from the other engine",
      not ok and "already" in why)

p = os.path.join(tempfile.mkdtemp(), "dup.json")
json.dump({"rules": [
    {"id": "x", "name": "a", "trigger": {"kind": "time", "at": "07:00"},
     "action": {"kind": "notify", "text": "1"}, "enabled": True},
    {"id": "x", "name": "b", "trigger": {"kind": "time", "at": "08:00"},
     "action": {"kind": "notify", "text": "2"}, "enabled": True}]},
    open(p, "w"))
e3, _, _ = mk(path=p)
ids = [r["id"] for r in e3.rules]
check("duplicate ids in an old file are given new ones", len(set(ids)) == 2)
check("and the repair is written back",
      len({r["id"] for r in json.load(open(p))["rules"]}) == 2)

bad = os.path.join(tempfile.mkdtemp(), "afile")
open(bad, "w").write("x")
e4, _, _ = mk(path=os.path.join(bad, "nested", "a.json"))
ok, why = e4.add({"name": "z", "trigger": {"kind": "time", "at": "07:00"},
                  "action": {"kind": "notify", "text": "1"}})
check("a failed disk write does not say saved", not ok and bool(why), why)

print("\n== numbers that are not numbers")
nan = float("nan")
check("persist for_s NaN is refused",
      A.validate_rule({"name": "n", "trigger": {
          "kind": "persist", "device": "d", "value": "on", "for_s": nan},
          "action": {"kind": "notify", "text": "x"}}) is not None)
check("threshold line inf is refused",
      A.validate_rule({"name": "n", "trigger": {
          "kind": "threshold", "device": "d", "key": "t",
          "above": float("inf")},
          "action": {"kind": "notify", "text": "x"}}) is not None)
check("a null name is refused",
      A.validate_rule({"name": None, "trigger": {"kind": "time", "at": "07:00"},
                       "action": {"kind": "notify", "text": "x"}}) is not None)

print("\n== the per-rule override timer")
spec = A.override_spec
check("no field = the house default", spec({}) == ("default", 0.0))
check("always", spec({"override": {"mode": "always"}}) == ("always", 0.0))
for unit, mult in (("seconds", 1), ("minutes", 60), ("hours", 3600),
                   ("days", 86400), ("min", 60), ("h", 3600), ("d", 86400)):
    got = spec({"override": {"mode": "window", "value": 2, "unit": unit}})
    check(f"2 {unit} = {2 * mult}s", got == ("window", 2.0 * mult), got)
check("a bad field reads as default and never raises",
      spec({"override": {"mode": "window", "value": "x"}}) == ("default", 0.0))
base = {"name": "o", "trigger": {"kind": "time", "at": "07:00"},
        "action": {"kind": "notify", "text": "x"}}
for ov, expect_ok in (
        ({"mode": "window", "value": 5, "unit": "minutes"}, True),
        ({"mode": "window", "value": 0, "unit": "minutes"}, False),
        ({"mode": "window", "value": 5, "unit": "weeks"}, False),
        ({"mode": "window", "value": nan, "unit": "hours"}, False),
        ({"mode": "window", "value": 31, "unit": "days"}, False),
        ({"mode": "sideways"}, False),
        ({"mode": "always"}, True)):
    got = A.validate_rule(dict(base, override=ov))
    check(f"validate {ov}", (got is None) == expect_ok, got)

e, dm, clock = mk()
with_devices(dm)
ok, _ = e.add(motion_rule(name="default one"))
ok, _ = e.add(motion_rule(name="wins", override={
    "mode": "window", "value": 1, "unit": "hours"}))
ok, _ = e.add(motion_rule(name="machine", override={"mode": "always"}))
d, w, m = (e.get(n) for n in ("default one", "wins", "machine"))
e.note_human("lamp")
check("default window: skipped inside 60 s",
      e._drive(d, "lamp", {"on": True}) is None)
check("always: ignores the person", e._drive(m, "lamp", {"on": True}) is True)
clock.t += 600
check("default window has closed after 10 minutes",
      e._drive(d, "lamp", {"on": True}) is True)
check("a 1 hour window still holds after 10 minutes",
      e._drive(w, "lamp", {"on": True}) is None)
clock.t += 2300
check("...and still at 60 minutes minus a little",
      e._drive(w, "lamp", {"on": True}) is None)
clock.t += 800
check("...and is over after the hour", e._drive(w, "lamp", {"on": True}) is True)
e.note_human("lamp")
clock.t += 3000
e.note_human("lamp")            # another command: counted from the LAST one
clock.t += 3000
check("the window counts from the person's LAST command",
      e._drive(w, "lamp", {"on": True}) is None)
tr = {"name": "t", "trigger": {"kind": "time", "at": "07:00"},
      "action": {"kind": "device", "device": "lamp", "set": {"on": True}}}
e.note_human("pir")
check("a time rule is exempt in the default mode",
      e._drive(dict(tr), "pir", {"on": True}) is True)
check("...but not when the person gave it a window",
      e._drive(dict(tr, override={"mode": "window", "value": 5,
                                  "unit": "minutes"}),
               "pir", {"on": True}) is None)
check("the sentence says so",
      "you win for 1 hour" in A.trigger_sentence(w))
check("and for always", "even if you switch it yourself"
      in A.trigger_sentence(m))
ok, _ = e.update("wins", override={"mode": "window", "value": 30,
                                   "unit": "seconds"})
check("an edit can change it", ok and A.override_spec(e.get("wins"))
      == ("window", 30.0))
ok, _ = e.update("wins", override={"mode": "default"})
check("and back to the house default drops the field",
      ok and "override" not in e.get("wins"))

ok, _ = e.add(dict(base, name="null override", override=None))
check("a rule built with override: null stores no override",
      ok and "override" not in e.get("null override"))
e.update("null override", override={"mode": "always"})
ok, _ = e.update("null override", override=None)
check("override: null on an edit clears it",
      ok and "override" not in e.get("null override"))

print("\n== lights that stay on")
e, dm, clock = mk()
with_devices(dm)
e.add(motion_rule())
e._index = {"tb": {("switch", 1): "pir"}}
e.feed("stat/tb/RESULT", json.dumps({"Switch1": {"Action": "ON"}}))
check("motion turns the lamp on", dm.devices["lamp"]["state"]["on"] is True)
check("a revert is pending", len(e._revert_due) == 1)
rid = e.get("motion light")["id"]
due = e._revert_due[rid]
e2 = A.AutomationEngine(dm, None, None, path=e.path, now=clock)
e2.history_path = e.history_path
check("a restarted engine remembers the pending revert",
      e2._revert_due.get(rid) == due)
clock.t += 130
e2.tick()
check("and switches the lamp off", dm.devices["lamp"]["state"]["on"] is False)

e, dm, clock = mk()
with_devices(dm)
e.add(motion_rule())
rid = e.get("motion light")["id"]
e._index = {"tb": {("switch", 1): "pir"}}
e.feed("stat/tb/RESULT", json.dumps({"Switch1": {"Action": "ON"}}))
e.update("motion light", enabled=False)
check("pausing keeps the pending revert", rid in e._revert_due)
clock.t += 130
e.tick()
check("a paused rule still puts its light out",
      dm.devices["lamp"]["state"]["on"] is False)

e, dm, clock = mk()
with_devices(dm)
e.add(motion_rule())
rid = e.get("motion light")["id"]
e._index = {"tb": {("switch", 1): "pir"}}
e.feed("stat/tb/RESULT", json.dumps({"Switch1": {"Action": "ON"}}))
dm.fail = True
clock.t += 130
e.tick()
check("a revert that failed is not forgotten", rid in e._revert_due)
dm.calls.clear()
clock.t += 31
e.tick()
check("and is tried again 30 s later", len(dm.calls) == 1)
dm.fail = False
clock.t += 31
e.tick()
check("it works once the board answers",
      dm.devices["lamp"]["state"]["on"] is False
      and rid not in e._revert_due)

e, dm, clock = mk()
with_devices(dm)
e.add(motion_rule())
rid = e.get("motion light")["id"]
e._index = {"tb": {("switch", 1): "pir"}}
e.feed("stat/tb/RESULT", json.dumps({"Switch1": {"Action": "ON"}}))
dm.fail = True
for _ in range(10):
    clock.t += 131
    e.tick()
check("it gives up after five tries and says so",
      rid not in e._revert_due
      and any("revert-failed" in a["id"] for a in e.alerts))

e, dm, clock = mk()
with_devices(dm)
e.add(motion_rule())
rid = e.get("motion light")["id"]
e._index = {"tb": {("switch", 1): "pir"}}
e.feed("stat/tb/RESULT", json.dumps({"Switch1": {"Action": "ON"}}))
first = e._revert_due[rid]
clock.t += 20                    # inside the cooldown
e.feed("stat/tb/RESULT", json.dumps({"Switch1": {"Action": "OFF"}}))
e.feed("stat/tb/RESULT", json.dumps({"Switch1": {"Action": "ON"}}))
check("movement the cooldown refuses still pushes the revert out",
      e._revert_due[rid] > first, (first, e._revert_due[rid]))

print("\n== approvals")
e, dm, clock = mk()
dm.add("door", secure=True)
dm.add("rfid")
e.add({"name": "ask", "trigger": {"kind": "event", "device": "rfid",
                                  "value": "any"},
       "action": {"kind": "ask", "device": "door", "set": {"on": False}}})
rule = e.get("ask")
e._raise_approval(rule, rule["action"])
e._raise_approval(rule, rule["action"])
check("a repeat question replaces the old one", len(e.pending_approvals()) == 1)
aid = e.pending_approvals()[0]["id"]
dm.fail = True
ok, msg = e.approve(aid)
check("approve says so when the board did not answer",
      not ok and "did not answer" in msg, msg)
check("and the question is still there to answer again",
      len(e.pending_approvals()) == 1)
dm.fail = False
ok, msg = e.approve(aid)
check("approve works the second time", ok and "done" in msg, msg)
check("and the question is used up now", len(e.pending_approvals()) == 0)
ok, msg = e.approve(aid)
check("a used question stays used", not ok)
ids = set()
for i in range(5):
    e._raise_approval(rule, dict(rule["action"], device="door" if i % 2 else "lamp"))
for a in e.pending_approvals():
    ids.add(a["id"])
check("approval ids are unique", len(ids) == len(e.pending_approvals()))

print("\n== what the hub hears")
src = open(os.path.join(os.path.dirname(A.__file__), "automations.py")).read()
check("the hub subscribes to tele/+/RESULT", '("tele/+/RESULT", 0)' in src)

e, dm, clock = mk()
with_devices(dm)
dm.add("remote")
e.add({"name": "ir", "trigger": {"kind": "event", "device": "remote",
                                 "value": "any"},
       "action": {"kind": "device", "device": "lamp", "set": {"on": True}}})
e._index = {"tb": {("ir", 0): "remote"}}
e.feed("tele/tb/RESULT", json.dumps({"IrReceived": {"Data": "0x20DF10EF"}}))
check("an IR code on tele/RESULT fires its rule",
      dm.devices["lamp"]["state"]["on"] is True)
dm.devices["lamp"]["state"]["on"] = False
clock.t += 100
e.feed("tele/tb/RESULT", json.dumps({"IrReceived": {"Data": "0x20DF10EF"}}),
       retained=True)
check("a retained replay of a tap or code does nothing",
      dm.devices["lamp"]["state"]["on"] is False)

e, dm, clock = mk()
with_devices(dm)
e.add({"name": "door opens",
       "trigger": {"kind": "event", "device": "door", "value": "on"},
       "action": {"kind": "notify", "text": "door!"}})
e._index = {"tb": {("switch", 1): "door"}}
e.feed("tele/tb/SENSOR", json.dumps({"Switch1": "ON"}))
check("hub restart: the first repeat of 'open' is not 'it opened'",
      len(e.alerts) == 0)
clock.t += 100
e.feed("tele/tb/SENSOR", json.dumps({"Switch1": "OFF"}))
clock.t += 100
e.feed("tele/tb/SENSOR", json.dumps({"Switch1": "ON"}))
check("a real opening afterwards still fires", len(e.alerts) == 1)
e, dm, clock = mk()
with_devices(dm)
e.add({"name": "door opens",
       "trigger": {"kind": "event", "device": "door", "value": "on"},
       "action": {"kind": "notify", "text": "door!"}})
e._index = {"tb": {("switch", 1): "door"}}
e.feed("stat/tb/RESULT", json.dumps({"Switch1": {"Action": "ON"}}))
check("a live press as the first thing heard still fires",
      len(e.alerts) == 1)
e, dm, clock = mk()
with_devices(dm)
e.add({"name": "door opens",
       "trigger": {"kind": "event", "device": "door", "value": "on"},
       "action": {"kind": "notify", "text": "door!"}})
e._index = {"tb": {("switch", 1): "door"}}
e.feed("stat/tb/RESULT", json.dumps({"Switch1": {"Action": "ON"}}),
       retained=True)
check("a retained 'open' does not fire", len(e.alerts) == 0)

print("\n== a temperature alert that was withheld is not lost")
e, dm, clock = mk()
with_devices(dm)
e.add({"name": "hot", "trigger": {"kind": "threshold", "device": "temp",
                                  "key": "temperature", "above": 30},
       "action": {"kind": "notify", "text": "hot!"}})
rid = e.get("hot")["id"]
e._fired_at[rid] = clock.t         # fired a moment ago: cooldown applies
e._index = {"tb": {("reading", "DHT"): "temp"}}
e.feed("tele/tb/SENSOR", json.dumps({"DHT": {"Temperature": 25}}))
clock.t += 5
e.feed("tele/tb/SENSOR", json.dumps({"DHT": {"Temperature": 35}}))
check("crossing inside the cooldown is withheld", len(e.alerts) == 0)
clock.t += 70
e.feed("tele/tb/SENSOR", json.dumps({"DHT": {"Temperature": 36}}))
check("the next report of the same excursion fires it", len(e.alerts) == 1)
clock.t += 70
e.feed("tele/tb/SENSOR", json.dumps({"DHT": {"Temperature": 37}}))
check("and it does not repeat while still hot", len(e.alerts) == 1)
e.feed("tele/tb/SENSOR", json.dumps({"DHT": {"Temperature": 20}}))
clock.t += 70
e.feed("tele/tb/SENSOR", json.dumps({"DHT": {"Temperature": 40}}))
check("a new excursion fires again", len(e.alerts) == 2)

print("\n== a board that went away")
e, dm, clock = mk()
with_devices(dm)
e.add({"name": "if closed", "trigger": {"kind": "time", "at": "07:00"},
       "condition": {"kind": "device", "device": "door", "key": "on",
                     "equals": False},
       "action": {"kind": "notify", "text": "closed"}})
e._index = {"tb": {("switch", 1): "door"}}
e.feed("tele/tb/SENSOR", json.dumps({"Switch1": "OFF"}))
rule = e.get("if closed")
check("the condition holds while the board is there",
      e._condition_holds(rule["condition"]))
e.feed("tele/tb/LWT", "Offline")
check("LWT Offline: the board's devices are offline", e.is_offline("door"))
check("the condition does not hold on an offline device",
      not e._condition_holds(rule["condition"]))
e._fire(rule)
check("and the journal says why",
      any("offline" in j[2] for j in e.journal))
e.feed("tele/tb/SENSOR", json.dumps({"Switch1": "OFF"}))
check("speaking again brings it back", not e.is_offline("door"))

e, dm, clock = mk()
with_devices(dm)
e.add({"name": "quiet", "trigger": {"kind": "absent", "device": "pir",
                                    "value": "on", "for_s": 300},
       "action": {"kind": "notify", "text": "nobody"}})
e._index = {"tb": {("switch", 1): "pir"}}
e.feed("stat/tb/RESULT", json.dumps({"Switch1": {"Action": "ON"}}))
e.feed("stat/tb/RESULT", json.dumps({"Switch1": {"Action": "OFF"}}))
e.feed("tele/tb/LWT", "Offline")
clock.t += 1000
e.tick()
check("an offline board is not 'quiet'", len(e.alerts) == 0)

print("\n== the day's numbers")
e, dm, clock = mk()
with_devices(dm)
e._index = {"tb": {("reading", "DHT"): "temp"}}
for i in range(120):                       # every 10 s for 20 minutes
    e.feed("tele/tb/SENSOR", json.dumps({"DHT": {"Temperature": 20 + i / 10}}))
    clock.t += 10
series = e._history[("temp", "temperature")]
check("a sensor reporting every 10 s still makes ~one point a minute",
      15 <= len(series) <= 21, len(series))
before = len(e.feed("tele/tb/SENSOR",
                    '{"DHT": {"Temperature": NaN}}'))
check("NaN is not a reading", before == 0)
check("and it is not in the history",
      all(v == v for _t, v in e._history[("temp", "temperature")]))

print("\n== the clock moves")
mono = Clock(5000.0)
e, dm, clock = mk(mono=mono)
with_devices(dm)
e.add(motion_rule())
rid = e.get("motion light")["id"]
e._index = {"tb": {("switch", 1): "pir"}}
e.tick()
e.feed("stat/tb/RESULT", json.dumps({"Switch1": {"Action": "ON"}}))
left = e._revert_due[rid] - clock.t
clock.t += 3 * 3600                        # NTP steps the date forward
mono.t += 5
e.tick()
check("a forward clock step keeps the time the revert had left",
      abs((e._revert_due[rid] - clock.t) - (left - 5)) < 1,
      (e._revert_due[rid] - clock.t, left))
check("so the lamp is not put out early",
      dm.devices["lamp"]["state"]["on"] is True)
clock.t -= 5 * 3600                        # and back
mono.t += 5
e.tick()
check("a backward step does not freeze it either",
      abs((e._revert_due[rid] - clock.t) - (left - 10)) < 1)

print("\n== other things the audit named")
e, dm, clock = mk()
with_devices(dm)
e.add({"name": "uses lamp",
       "trigger": {"kind": "event", "device": "pir", "value": "on"},
       "action": {"kind": "device", "device": "lamp", "set": {"on": True}}})
e.add({"name": "uses scene",
       "trigger": {"kind": "time", "at": "07:00"},
       "action": {"kind": "scene", "scene": "Evening"}})
check("rules_using_device finds trigger and action users",
      e.rules_using_device("lamp") == ["uses lamp"]
      and e.rules_using_device("pir") == ["uses lamp"]
      and e.rules_using_device("nothing") == [])
check("rules_using_scene finds it", e.rules_using_scene("evening")
      == ["uses scene"])
e.add({"name": "chatty", "trigger": {"kind": "change", "device": "temp",
                                     "key": "temperature"},
       "action": {"kind": "notify", "text": "changed"}})
e.add({"name": "panel", "trigger": {"kind": "change", "device": "temp",
                                    "key": "temperature"},
       "action": {"kind": "display", "device": "kit.lcd", "text": "{value}"}})
check("a 'tell me when it changes' rule keeps the full cooldown",
      e._cooldown_for(e.get("chatty")) == e.cooldown_s)
check("a panel-only rule still updates quickly",
      e._cooldown_for(e.get("panel")) == A.CHANGE_COOLDOWN_SECONDS)
e.add({"name": "x", "trigger": {"kind": "time", "at": "07:00"},
       "action": {"kind": "notify", "text": "x"}})
ok, _ = e.update("uses lamp", enabled=False)
check("a rule can be paused", ok)
ok, _ = e.update("uses lamp", enabled=True)
check("and resumed", ok and not e.get("uses lamp").get("_invalid"))

print("\n== a dimmer's level is read back, and a returning board is asked")
e, dm, clock = mk()
dm.add("dim")
dm.devices["dim"]["state"] = {"on": True, "level": 40.0}
e._index = {"tb": {("pwm", 2): "dim", ("relay", 1): "lamp"}}
dm.add("lamp")
e.feed("stat/tb/RESULT", json.dumps({"POWER1": "ON", "Channel": [0, 70]}))
got = e.latest("dim")
check("{'Channel': [0, 70]}: channel 2 is the dimmer, at 70 and on",
      got.get("level") == 70.0 and got.get("on") is True, got)
check("...and channel 1 belongs to nobody here, so nothing else moved",
      e.latest("lamp").get("level") is None)
e.feed("stat/tb/RESULT", json.dumps({"Channel2": 0}))
got = e.latest("dim")
check("{'Channel2': 0}: the same dimmer, now off", got.get("level") == 0.0
      and got.get("on") is False, got)
e.feed("stat/tb/RESULT", json.dumps({"Channel": ["x", None, True]}))
check("rubbish in a Channel list is ignored", e.latest("dim").get("level") == 0.0)

sent = []


class _Pub:
    def publish(self, topic, payload, qos=0, retain=False):
        sent.append((topic, payload))

        class _R:
            rc = 0
        return _R()


e._pub = _Pub()
e._reassert_settings = lambda topic: None
e.feed("tele/tb/LWT", "Offline")
e.feed("tele/tb/LWT", "Online")
check("a board seen going away and coming back is asked for its State",
      any(t.upper().endswith("/STATE") for t, _ in sent), sent)
sent.clear()
e2, _, _ = mk()
e2._pub = _Pub()
e2._reassert_settings = lambda topic: None
e2.feed("tele/tb/LWT", "Online", retained=True)
check("...but not on the broker's replay of an old Online",
      not any(t.upper().endswith("/STATE") for t, _ in sent), sent)

print("\n== a step is taken from where the device is NOW")
from nlu.executor import Executor                              # noqa: E402


class _LiveDM(DM):
    def get_state(self, did):
        return {"on": True, "level": 70.0}        # what the board says now

    def is_readable_only(self, d):
        return False

    def is_pressable(self, d):
        return False

    def touch(self, *a):
        pass


ldm = _LiveDM()
ldm.add("dim", state_key="level", step=10.0, range_min=0.0, range_max=100.0)
ldm.devices["dim"]["state"] = {"on": True, "level": 40.0}   # the stale record
res = Executor(ldm).execute("dim", {"delta": 0.1, "delta_direction": "increase",
                                    "delta_category": "comparative",
                                    "switch": None}, "x")
check("'brighten it' on a dimmer the record says is at 40 and the board says "
      "is at 70: 80, not 50",
      any(c[1].get("level") == 80.0 for c in ldm.calls), ldm.calls)

print(f"\n{PASSED} passed, {FAILED} failed")
sys.exit(1 if FAILED else 0)

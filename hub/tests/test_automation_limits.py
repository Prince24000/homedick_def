"""AUTOMATION ADDITIONS (2026-11-05): a press (an IR button or a Home
Assistant button), "only if" a reading is above/below a number, "only if"
it is one of these days, and several "only if"s together.

Run: python3 tests/test_automation_limits.py
"""
import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("HOMEVOICE_HOME", tempfile.mkdtemp())
ENGINE_PKG = "nlu"
A = __import__(f"{ENGINE_PKG}.automations", fromlist=["x"])

PASSED = FAILED = 0


def check(name, ok, detail=""):
    global PASSED, FAILED
    PASSED, FAILED = PASSED + bool(ok), FAILED + (not ok)
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}   {'' if ok else detail}")


class DM:
    def __init__(self):
        self.devices = {
            "living.meter": {"name": "meter"},
            "tv.remote": {"name": "tv", "ir_codes": {"power": {"Protocol": "NEC",
                                                               "Data": "0x1"}}},
            "ha.doorbell": {"name": "doorbell", "ha_entity_id": "button.ring"},
            "front.gate": {"name": "gate", "secure": True,
                           "ha_entity_id": "button.gate"},
        }
        self.pressed = []

    def press(self, did):
        self.pressed.append(did)
        return True

    def get_state(self, did):
        return {}


class Info:
    rc = 0


class Pub:
    def __init__(self):
        self.sent = []

    def publish(self, topic, body, qos=0):
        self.sent.append((topic, body))
        return Info()


class Esp:
    def boards_with(self, type_id):
        return ["hall"] if type_id == "ir_send" else []

    def _read_board(self, slug):
        return {"topic": "hv-hall"}

    def display_targets(self):
        return []


# A Wednesday at 12:00 local time.
base = time.mktime((2026, 11, 4, 12, 0, 0, 0, 0, -1))
now = [base]
dm = DM()
eng = A.AutomationEngine(dm, None, None,
                         path=os.path.join(tempfile.mkdtemp(), "a.json"),
                         now=lambda: now[0])
eng._pub = pub = Pub()
eng.esp = Esp()


def rule(**kw):
    r = {"name": "t", "trigger": {"kind": "time", "at": "07:00"},
         "action": {"kind": "notify", "text": "hi"}}
    r.update(kw)
    return r


print("\nchecking what is saved")
check("a press of an IR button is valid",
      A.validate_rule(rule(action={"kind": "press", "device": "tv.remote",
                                   "button": "power"})) is None)
check("a press with no device is refused in words",
      "names no device" in (A.validate_rule(rule(action={"kind": "press"}))
                            or ""))
reading = {"kind": "reading", "device": "living.meter", "key": "voltage",
           "below": 200}
check("only if a reading is below a number: valid",
      A.validate_rule(rule(condition=reading)) is None)
check("a reading limit with both above and below is refused",
      "above or below" in (A.validate_rule(rule(condition=dict(
          reading, above=250))) or ""))
days = {"kind": "days", "days": [0, 1, 2, 3, 4]}
check("only on weekdays: valid", A.validate_rule(rule(condition=days)) is None)
check("a day that does not exist is refused",
      "Monday" in (A.validate_rule(rule(condition={"kind": "days",
                                                   "days": [7]})) or ""))
both = rule(conditions=[days, {"kind": "time_range", "after": "6:00",
                               "before": "22:00"}])
check("several limits together: valid", A.validate_rule(both) is None)
A.normalise_rule(both)
check("...and their times are written in full",
      both["conditions"][1]["after"] == "06:00")
check("an ELSE with several limits is allowed",
      A.validate_rule(rule(conditions=[days], else_actions=[
          {"kind": "notify", "text": "no"}])) is None)
check("a press reads back in words",
      "press power on tv" in A.trigger_sentence(
          rule(action={"kind": "press", "device": "tv.remote",
                       "button": "power"}),
          lambda d: dm.devices.get(d, {}).get("name", d)))

print("\nthe limits, at the moment it fires")
check("Wednesday is a weekday", eng._conditions_hold(rule(condition=days)))
check("...and not a weekend day",
      not eng._conditions_hold(rule(condition={"kind": "days",
                                               "days": [5, 6]})))
check("...and the journal says which day it is",
      "Wednesday" in eng._cond_why, eng._cond_why)
eng._last[("living.meter", "voltage")] = (190.0, now[0])
check("voltage 190 is below 200", eng._conditions_hold(rule(condition=reading)))
check("...and not above 200", not eng._conditions_hold(
    rule(condition={"kind": "reading", "device": "living.meter",
                    "key": "voltage", "above": 200})))
check("a reading not yet reported does not hold, and says why",
      not eng._conditions_hold(rule(condition=dict(reading, key="current")))
      and "not reported" in eng._cond_why, eng._cond_why)
check("several limits: all hold",
      eng._conditions_hold(rule(conditions=[days, reading])))
check("several limits: one fails, so none",
      not eng._conditions_hold(rule(conditions=[days, dict(reading,
                                                           below=100)])))
eng.rules.append(rule(name="low power", conditions=[reading]))
check("a device used only in a limit is still found before it is deleted",
      "low power" in eng.rules_using_device("living.meter"),
      eng.rules_using_device("living.meter"))
eng.rules.clear()

print("\npressing")
r = rule(action={"kind": "press", "device": "tv.remote", "button": "power"})
eng._run_actions(r, A.actions_of(r))
check("an IR button goes out through the infrared board",
      ("cmnd/hv-hall/IRsend", '{"Protocol": "NEC", "Data": "0x1"}') in pub.sent,
      pub.sent)
r = rule(action={"kind": "press", "device": "ha.doorbell"})
eng._run_actions(r, A.actions_of(r))
check("a Home Assistant button is pressed", dm.pressed == ["ha.doorbell"])
check("a secure device is refused at save",
      "secure" in (eng.action_screening({"kind": "press",
                                         "device": "front.gate"}) or ""))
r = rule(action={"kind": "press", "device": "front.gate"})
eng._run_actions(r, A.actions_of(r))
check("...and never pressed by an automation", "front.gate" not in dm.pressed)

print("\ntry it now")
said = eng.try_now(rule(name="tv", action={"kind": "press",
                                           "device": "tv.remote",
                                           "button": "power"}))
check("it runs once and says what happened", any("pressed power" in x
                                                   for x in said), said)
check("...and nothing is saved", eng.rules == [])
said = eng.try_now(rule(action={"kind": "press", "device": "front.gate"}))
check("a secure device is refused, in words", "secure" in said[0], said)
said = eng.try_now(rule(trigger={"kind": "time"}))
check("an unfinished one says what is missing", "HH:MM" in said[0], said)

print("\na reading automation acts at once when it is saved")
fired = []
real_fire = eng._fire
eng._fire = lambda r: fired.append(r["name"]) or True
eng._index = {"hv-living": {("energy", 0): "living.meter"}}
pub.sent.clear()
eng._last[("living.meter", "voltage")] = (185.0, now[0] - 3600)   # an hour old
low = rule(name="low volts", trigger={"kind": "threshold", "device": "living.meter",
                                      "key": "voltage", "below": 200})
ok, rid = eng.add(low)
check("already below the line: it acts at once, on the old reading",
      ok and fired == ["low volts"], (ok, rid, fired))
check("...and a fresh reading is asked for at the same moment",
      ("cmnd/hv-living/Status", "10") in pub.sent, pub.sent)
eng.update(rid, name="low voltage")
check("renaming it does not act again", fired == ["low volts"], fired)
eng.update(rid, enabled=False)
eng.update(rid, enabled=True)
check("turning it back on acts again (still below)",
      fired == ["low volts", "low voltage"], fired)
eng._meter_silent["living.meter"] = now[0]
eng.update(rid, enabled=False)
eng.update(rid, enabled=True)
check("a meter not answering: it waits for a real reading, and says so",
      len(fired) == 2 and any("waiting for a real reading" in t
                              for _ts, _n, t in eng.journal), fired)
eng._meter_silent.clear()
eng._last[("living.meter", "voltage")] = (230.0, now[0])
ok, rid2 = eng.add(rule(name="high", trigger={"kind": "threshold",
                                              "device": "living.meter",
                                              "key": "voltage", "below": 200}))
check("not past the line: nothing happens until it crosses",
      "high" not in fired, fired)
eng._fire = real_fire
eng.rules.clear()

print(f"\n{PASSED} passed, {FAILED} failed")
sys.exit(1 if FAILED else 0)

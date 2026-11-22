"""ONE SENTENCE -> THE BUILDER'S BOXES (2026-11-05).

Run: python3 tests/test_rule_sentence.py
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("HOMEVOICE_HOME", tempfile.mkdtemp())
from app.rule_sentence import draft                      # noqa: E402

PASSED = FAILED = 0


def check(name, ok, detail=""):
    global PASSED, FAILED
    PASSED, FAILED = PASSED + bool(ok), FAILED + (not ok)
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}   {'' if ok else detail}")


NAMES = {"living.meter": "voltage meter", "hall.pir": "motion sensor",
         "living.lamp": "living room light", "kitchen.display": "kitchen display",
         "tv.remote": "tv", "hall.fan": "fan"}


def named(words):
    w = " " + " ".join(words.lower().replace(",", " ").split()) + " "
    return [d for d, n in NAMES.items() if f" {n} " in w]


def threshold(key):
    return [{"label": f"{key} rises above", "kind": "threshold", "needs": "number",
             "key": key, "edge": "above",
             "trigger": {"kind": "threshold", "device": "living.meter", "key": key}},
            {"label": f"{key} falls below", "kind": "threshold", "needs": "number",
             "key": key, "edge": "below",
             "trigger": {"kind": "threshold", "device": "living.meter", "key": key}},
            {"label": f"{key} changes", "kind": "change", "key": key,
             "trigger": {"kind": "change", "device": "living.meter", "key": key}}]


ASK = {
    "living.meter": {"name": "voltage meter", "options": threshold("voltage")},
    "hall.pir": {"name": "motion sensor", "options": [
        {"label": "turns on", "kind": "event",
         "trigger": {"kind": "event", "device": "hall.pir", "value": "on"}},
        {"label": "turns off", "kind": "event",
         "trigger": {"kind": "event", "device": "hall.pir", "value": "off"}},
        {"label": "is quiet for", "kind": "absent", "needs": "minutes",
         "trigger": {"kind": "absent", "device": "hall.pir", "value": "on"}}]},
}
TARGETS = [
    {"value": "living.lamp", "label": "living room light", "use": {"kind": "dimmer"}},
    {"value": "kitchen.display", "label": "kitchen display",
     "use": {"kind": "display", "display": {"rows": 2, "cols": 16}}},
    {"value": "tv.remote", "label": "tv",
     "use": {"kind": "ir", "buttons": ["pause", "power"]}},
    {"value": "hall.fan", "label": "fan", "use": {"kind": "switch"}},
]
SCENES = [{"value": "Movie night", "label": "Movie night"}]


def go(text):
    return draft(text, askable=ASK, targets=TARGETS, scenes=SCENES, named=named)


print("\nreading a whole sentence")
d = go('When the voltage meter drops below 200, show "Low power" on the '
       'kitchen display')
r = d["rule"]
check("WHEN: the meter, below, 200",
      r.get("trigger") == {"kind": "threshold", "device": "living.meter",
                           "key": "voltage", "below": 200.0}, r.get("trigger"))
check("DO: the quoted words on the display",
      r.get("actions") == [{"kind": "display", "device": "kitchen.display",
                            "for_s": 0, "pages": [[{"text": "Low power",
                                                     "align": "left"}]]}],
      r.get("actions"))
check("nothing left to check", d["unsure"] == [], d["unsure"])

d = go("when the motion sensor turns on turn on the living room light and "
       "turn off the fan")
r = d["rule"]
try:
    from nlu.command_splitter import _verbs_of        # noqa: F401
    GRAMMAR = True
except ImportError:                     # the defense copy: no grammar reader
    GRAMMAR = False
if GRAMMAR:
    check("no comma: split at the order verb",
          r.get("trigger", {}).get("value") == "on", r)
    check("two actions, each its own way round",
          [a.get("set", {}).get("on") for a in r.get("actions", [])]
          == [True, False], r.get("actions"))
else:
    check("no comma and no grammar reader: DO is flagged for tapping",
          "do" in d["unsure"], d)

d = go("At 7:30 turn on the fan, only on Monday and Friday")
r = d["rule"]
check("a time of day starts it", r.get("trigger") == {"kind": "time", "at": "07:30"},
      r.get("trigger"))
check("...only on those days (names from the calendar module)",
      r.get("condition") == {"kind": "days", "days": [0, 4]}, r.get("condition"))

d = go("If the motion sensor turns on, turn on the living room light to 40 "
       "between 18:00 and 06:00")
r = d["rule"]
check("between two times becomes the limit",
      r.get("condition") == {"kind": "time_range", "after": "18:00",
                             "before": "06:00"}, r.get("condition"))
check("a brightness is kept", r.get("actions", [{}])[0].get("set") ==
      {"on": True, "level": 40.0}, r.get("actions"))

d = go("when the motion sensor turns on, pause the tv")
check("an IR appliance's learned button",
      d["rule"].get("actions") == [{"kind": "press", "device": "tv.remote",
                                    "button": "pause"}], d["rule"])

d = go("when the motion sensor turns on, movie night")
check("a scene by its name",
      d["rule"].get("actions") == [{"kind": "scene", "scene": "Movie night"}],
      d["rule"])

print("\nwhat it cannot tell, it says")
d = go("when the garage thing beeps, do the usual")
check("an unknown device: WHEN and DO are flagged",
      d["unsure"] == ["when", "do"], d)
d = go("when the voltage meter drops below 200, show it on the kitchen display")
check("a display with no quoted words: DO is flagged",
      "do" in d["unsure"] and d["rule"].get("trigger", {}).get("below") == 200.0,
      d)
check("an empty sentence fills nothing", go("  ")["unsure"] == ["when", "do"])

print("\nwaits and the loop (Arduino's delay() and loop())")
d = go("when the motion sensor turns on, turn on the fan, wait 10 seconds, "
       "turn off the fan and repeat")
acts = d["rule"].get("actions") or []
check("a wait is a step, where it was said",
      [a.get("kind") for a in acts] == ["device", "wait", "device"]
      and acts[1] == {"kind": "wait", "s": 10.0}, acts)
check("...and 'and repeat' switches the loop on",
      d["rule"].get("loop") is True and "do" not in d["unsure"], d)
d = go("when the motion sensor turns on, turn on the fan, wait 5 minutes, "
       "then turn off the fan")
acts = d["rule"].get("actions") or []
check("minutes are read, and no loop unless said",
      len(acts) == 3 and acts[1] == {"kind": "wait", "s": 300.0}
      and "loop" not in d["rule"], d["rule"])
d = go("when the motion sensor turns on, turn on the fan, wait 2 hours, "
       "turn off the fan, keep repeating")
acts = d["rule"].get("actions") or []
check("hours, and 'keep repeating'",
      len(acts) == 3 and acts[1] == {"kind": "wait", "s": 7200.0}
      and d["rule"].get("loop") is True, d["rule"])
d = go("when the motion sensor turns on, turn on the fan, wait 1 minute, "
       "turn off the fan, wait 1 minute, loop")
acts = d["rule"].get("actions") or []
check("'loop' at the end; two waits in order",
      [a.get("kind") for a in acts] == ["device", "wait", "device", "wait"]
      and d["rule"].get("loop") is True, d["rule"])

print(f"\n{PASSED} passed, {FAILED} failed")
sys.exit(1 if FAILED else 0)

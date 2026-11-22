"""THE PANEL'S LINE SURVIVES A HUB RESTART (2026-11-05).

A reading put on an LCD by a rule stands there, with the clock's short
turn between. That standing line was kept only in memory, so after a hub
restart the panel showed the clock until the rule happened to fire again.
It is saved with the engine's state now, and dropped again when its rule
is gone.

Run: python3 tests/test_display_standing.py
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("HOMEVOICE_HOME", tempfile.mkdtemp())
from nlu import automations as A                                  # noqa: E402

PASSED = FAILED = 0


def check(name, ok, detail=""):
    global PASSED, FAILED
    PASSED, FAILED = PASSED + bool(ok), FAILED + (not ok)
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}   {'' if ok else detail}")


class DM:
    def __init__(self):
        self.devices = {}


now = [1_800_000_000.0]
path = os.path.join(tempfile.mkdtemp(), "a.json")
e = A.AutomationEngine(DM(), None, None, path=path, now=lambda: now[0])
e._lcd_standing["hv-dining"] = {None: {"rule_id": "r1", "device": "lcd"}}
e._lcd_standing["hv-hall"] = {2: {"rule_id": "r2", "device": "lcd2"}}
e._save_state()

e2 = A.AutomationEngine(DM(), None, None, path=path, now=lambda: now[0])
check("after a restart the panel's standing line is known again",
      e2._lcd_standing.get("hv-dining") == {None: {"rule_id": "r1",
                                                   "device": "lcd"}},
      e2._lcd_standing)
check("...a row's line keeps its row",
      e2._lcd_standing.get("hv-hall") == {2: {"rule_id": "r2",
                                              "device": "lcd2"}},
      e2._lcd_standing)
e2.rules = []                                # the rule has been deleted
lines = e2._standing_lines({"topic": "hv-dining"})
check("a line whose rule is gone is dropped, never brought back",
      lines == [] and "hv-dining" not in e2._lcd_standing, e2._lcd_standing)

print(f"\n{PASSED} passed, {FAILED} failed")
sys.exit(1 if FAILED else 0)

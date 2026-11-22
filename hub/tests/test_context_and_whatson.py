"""Back-references survive between voice commands; "is anything on?" is
answered by listing what is on.

Run: python3 tests/test_context_and_whatson.py   (no hardware, no network)
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("HOMEVOICE_HOME", tempfile.mkdtemp())
os.environ.setdefault("HOMEVOICE_ENV_FILE", "")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import test_nlu_spec as T                                   # noqa: E402
from nlu.resolver import resolve_command                    # noqa: E402
from nlu.lexicon import is_whats_on_query                   # noqa: E402
from app.ws_server import ConnectionHandler                 # noqa: E402

PASSED = FAILED = 0


def check(name, ok, detail=""):
    global PASSED, FAILED
    PASSED, FAILED = PASSED + bool(ok), FAILED + (not ok)
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}   {'' if ok else detail}")


class _Shell:
    """Just enough shell for _end_flow."""
    def __init__(self, dm):
        self.dm = dm
        self.current_speaker_id = "Bruce Wayne"
        self._io = None
        self.current_phone_id = None
        self.checkpoint_note = None
        self.failure_note = None


def handler(dm):
    h = ConnectionHandler.__new__(ConnectionHandler)
    h.shell = _Shell(dm)
    return h


def ctx(text, dm, speaker="Bruce Wayne"):
    c = T.Ctx(text, dm)
    c.speaker_id = speaker
    return c


print("\nback-reference across two voice commands")
dm, _ = T.fresh()
dm.add("l.bulb", name="bulb", room="living room", description="bulb")
dm.add("l.lights", name="ceiling lights", room="living room",
       description="ceiling lights")
dm.touch_group("Bruce Wayne", ["l.bulb"])          # "activate the bulb" worked
h = handler(dm)
h._end_flow("Bruce Wayne", keep_context=True)      # the command ended
d = resolve_command(ctx("Switch it off.", dm))
check("'switch it off' after a command reaches the bulb",
      d["kind"] == "execute" and d["targets"] == ["l.bulb"], d)
h._end_flow("Bruce Wayne")                         # a menu action / setup
d = resolve_command(ctx("Switch it off.", dm))
check("other flows still start with no history", d["kind"] == "none", d)
dm.touch_group("Bruce Wayne", ["l.bulb"])
d = resolve_command(ctx("Switch it off.", dm, speaker="Alfred"))
check("another person's 'it' does not reach it", d["kind"] == "none", d)
d = resolve_command(ctx("turn on the lights", dm))
check("a named device still wins over the history",
      d["targets"] == ["l.lights"], d)

print("\n'is anything on?'")
for t in ["Is there anything on?", "is anything on", "what's on", "What is on?",
          "what is still on", "anything on", "Is anything on in the kitchen?",
          "what's running", "are any devices on", "is anything left on",
          "is there anything turned on right now"]:
    check(f"asks what is on: {t!r}", is_whats_on_query(t))
for t in ["turn on the lights", "is the kitchen light on", "what's the temperature",
          "is it on", "what time is it", "turn it on", "is the door locked",
          "what's the status of the house", "anything else"]:
    check(f"is not that question: {t!r}", not is_whats_on_query(t))

print(f"\n{PASSED} passed, {FAILED} failed")
sys.exit(1 if FAILED else 0)

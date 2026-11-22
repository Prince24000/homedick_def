"""IS A SENTENCE ABOUT SEVERAL THINGS? By grammar (nlu.grammar.asks_for_group,
2026-10-21), not by a word ending in "s": "turn on the news" and "the status
of the heater" are one thing; "batteries", "boxes", "children's lamps",
"mice", "them", "two lamps", "every lamp" are several.

Run: python3 tests/test_plural_grammar.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from nlu import grammar                                       # noqa: E402
from nlu.resolver import _is_plural_intent                    # noqa: E402

GROUP = ["turn on the lights", "close the blinds", "turn off the boxes",
         "switch off the battery chargers", "turn off the children's lamps",
         "switch on the entries lights", "dim the mice lights", "turn them off",
         "switch these off", "turn those on", "turn on two lamps",
         "turn on several lamps", "turn on a few lights", "turn on a couple of lamps",
         "turn off every lamp", "turn off each lamp", "turn off everything",
         "turn off the whole house", "switch off the entire kitchen",
         "turn on the rest", "turn on all the lamps", "turn on both lamps",
         "turn off the lamps in the kitchen", "lights on", "are they on",
         "set the fans to low", "dim the bedroom lights to 50"]
ONE = ["turn on the news", "what is the status of the heater", "turn on the bus light",
       "turn on the glass lamp", "turn on the sales display",
       "turn off the children's lamp", "turn on the lamp for the kids",
       "it's freezing", "turn it off", "turn that off", "turn on one lamp",
       "is the kitchen light on", "turn on the fan", "turn on the heater",
       "turn on the ac", "turn on the bedroom 2 lamp"]

if not grammar.load():
    print(f"grammar model not available ({grammar.off_reason()}) -- skipped")
    sys.exit(0)
passed = failed = 0
for text, want in [(t, True) for t in GROUP] + [(t, False) for t in ONE]:
    got = _is_plural_intent(text)
    if got == want:
        passed += 1
    else:
        failed += 1
        print(f"  [FAIL] {text!r}: {'several' if got else 'one'}, should be "
              f"{'several' if want else 'one'}")
print(f"  {passed} passed, {failed} failed")
sys.exit(1 if failed else 0)

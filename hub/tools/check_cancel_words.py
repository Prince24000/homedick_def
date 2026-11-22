#!/usr/bin/env python3
"""Which spoken replies count as CANCEL on this hub? Loads the hub's own
encoder and intent index and prints the verdict for each phrase, with the
layer that decided it. Run on the hub (the model must be installed):

    python tools/check_cancel_words.py                 # the built-in list
    python tools/check_cancel_words.py "no I said the kitchen"   # your own
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import config                                    # noqa: E402
from app.shell import MODEL_DIR                            # noqa: E402
from nlu.model_module import (load_model, IntentClassifier,  # noqa: E402
                              intent_index_exists, build_intent_index)
from nlu.spoken_cancel import (means_cancel, normalise,    # noqa: E402
                               fixed_cancel, negated)

SAMPLES = [
    # should cancel
    "cancel", "stop", "go back", "please stop", "never mind",
    "I did not say that", "no no, I didn't say that", "that's not what I said",
    "that's not what I asked for", "you misheard me", "that's not it",
    "forget what I just said", "I changed my mind",
    # should NOT cancel
    "don't cancel", "do not stop", "the kitchen", "the second one",
    "turn on the fan", "stop light", "yes", "no", "all of them",
]


def main() -> None:
    load_model(MODEL_DIR)
    if not intent_index_exists(config.INTENT_INDEX_PATH):
        build_intent_index(config.INTENT_INDEX_PATH)
    clf = IntentClassifier(config.INTENT_INDEX_PATH)

    def classify(text):
        return clf.classify(text, threshold=config.INTENT_MIN_SCORE,
                            margin_floor=config.INTENT_MIN_MARGIN)

    for said in sys.argv[1:] or SAMPLES:
        norm = normalise(said)
        if negated(norm):
            layer = "negation"
        elif fixed_cancel(norm):
            layer = "word list"
        else:
            r = clf.classify(norm, threshold=config.INTENT_MIN_SCORE,
                             margin_floor=config.INTENT_MIN_MARGIN)
            layer = (f"meaning: {r.get('intent')} score={r.get('score')} "
                     f"margin={r.get('margin')} ~ {r.get('nearest_seed')!r}")
        verdict = "CANCEL" if means_cancel(said, classify) else "  -   "
        print(f"{verdict}  {said!r:40} {layer}")


if __name__ == "__main__":
    main()

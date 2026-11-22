"""nlu/spoken_cancel.py -- does what was just SAID mean "cancel"?

Asked by the phone while the hub is waiting on an answer or still working
(ws_server's match_request with cancel_check). Two layers, exactly like
undo (2026-09-30: "the cancel word can be fluid ... let's check how undo
is"):

  1. A fixed list (lexicon.CANCEL_PHRASES), matched on the WHOLE
     utterance after the fillers are dropped: fast, exact, and the only
     layer for one- and two-word replies ("stop", "go back"), which are
     too short to judge by meaning.
  2. Meaning: the intent index's "dismiss" examples (intent_index.npz,
     the same ones "I didn't mean that" is recognised by as undo). A
     longer sentence close enough to them is a cancel: "no no, I did not
     say that", "that's not what I asked for".

NEGATION WINS. "don't cancel", "do not stop", "not cancel yet" mean the
opposite and are never a cancel, whatever the meaning layer says.

The meaning layer is a BLOCKING model call: callers run means_cancel in a
thread (asyncio.to_thread), never on the event loop.
"""
from __future__ import annotations

import re
from typing import Callable, Dict, Optional

from nlu.lexicon import CANCEL_PHRASES, CANCEL_FILLERS

_APOSTROPHES = str.maketrans({"’": "'", "‘": "'", "`": "'"})
_CONTRACTIONS = (
    ("don't", "do not"), ("didn't", "did not"), ("doesn't", "does not"),
    ("isn't", "is not"), ("wasn't", "was not"), ("that's", "that is"),
    ("it's", "it is"), ("i'm", "i am"), ("won't", "will not"),
    ("can't", "cannot"),
)
#: A cancel word right after one of these is a refusal to cancel.
_NEGATORS = ("not", "never", "no need to", "dont")
_CANCEL_VERBS = ("cancel", "stop", "abort", "quit")

#: The meaning layer only judges sentences of at least this many words.
MIN_WORDS_FOR_MEANING = 3


def normalise(text: str) -> str:
    t = (text or "").lower().translate(_APOSTROPHES)
    for short, full in _CONTRACTIONS:
        t = t.replace(short, full)
    t = re.sub(r"[^\w\s]", " ", t)
    return " ".join(t.split())


def negated(norm: str) -> bool:
    """'do not cancel', 'never stop', 'not cancel yet' -- a cancel word
    with a negation straight before it."""
    words = norm.split()
    for i, w in enumerate(words):
        if w in _CANCEL_VERBS and i > 0:
            before = " ".join(words[max(0, i - 3):i])
            if any(before.endswith(n) for n in _NEGATORS):
                return True
    return False


def fixed_cancel(norm: str) -> bool:
    """The whole utterance, fillers dropped, is on the list."""
    if not norm:
        return False
    if norm in CANCEL_PHRASES:
        return True
    core = " ".join(w for w in norm.split() if w not in CANCEL_FILLERS)
    return bool(core) and core in CANCEL_PHRASES


def _sound_distance(a: str, b: str) -> int:
    """Edits between two sound codes (Levenshtein, short strings only)."""
    row = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        prev, row[0] = row[0], i
        for j, cb in enumerate(b, 1):
            prev, row[j] = row[j], min(row[j] + 1, row[j - 1] + 1,
                                       prev + (ca != cb))
    return row[-1]


def sounds_like_cancel(text: str) -> bool:
    """ONE OR TWO WORDS THAT SOUND LIKE "cancel". The recogniser wrote
    "council." for a spoken "cancel" twice in one afternoon (2026-10-07),
    then "consume" and "can't sue" at one question (2026-10-09) -- and the
    question took each for an answer, found nothing, and asked again.

    The rule: the words run together start with the same sound as "cancel"
    (K-N) and are at most two sounds away from it (KNSL): "council" KNSL,
    "consume" KNSM, "can't sue" KNTS. Generous ON PURPOSE, and safe because
    of where it is asked: only when the words matched NONE of the options
    (an option called "console" still wins), and a cancel only drops the
    question -- the same as its Cancel button. Inside a longer sentence
    it proves nothing, so never more than two words."""
    words = [w for w in normalise(text).split() if w not in CANCEL_FILLERS]
    if not words or len(words) > 2:
        return False
    if words == ["cancel"]:
        return True
    from nlu.resolver import _sound
    code = _sound("".join(words))
    return code.startswith("KN") and _sound_distance(code, "KNSL") <= 2


def means_cancel(text: str,
                 classify: Optional[Callable[[str], Dict]] = None) -> bool:
    """True when `text` means "stop / cancel / I did not say that".

    `classify` is IntentClassifier.classify (already thresholded: it
    returns intent None below the bar). Without one, only the list counts.
    """
    norm = normalise(text)
    if not norm or negated(norm):
        return False
    if fixed_cancel(norm):
        return True
    if classify is None or len(norm.split()) < MIN_WORDS_FOR_MEANING:
        return False
    try:
        result = classify(norm) or {}
    except Exception:                                  # noqa: BLE001
        return False                 # no model, no guess: not a cancel
    return result.get("intent") == "dismiss"

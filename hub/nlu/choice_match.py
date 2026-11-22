"""choice_match.py -- matching a SPOKEN answer to the options the hub sent.

WHY THIS IS ON THE HUB (2026-09-11, and it is the whole point of the file):

    "the orb should have already told u what works... replicate that for
     voice dock"

She is right, and the reason the dock was worse than the orb is structural.
A command spoken at the orb travels to the hub as WORDS and is resolved
here, against the lexicon, the embeddings and the resolver -- everything
this project is. A spoken answer to a LIST was resolved on the PHONE, by a
hundred lines of Dart bigram scoring written from scratch, in the one part
of the system that has no test runner. Two matchers, one of them a toy, and
the toy owned every list.

So the phone stopped matching. It sends what it heard; this decides. The
consequences are all in the right direction: one implementation instead of
two, on the side that can be tested, in a language where the rest of the
matching already lives -- and when it is not sure, the hub can ASK, because
the hub is what asks questions.

WHAT IT MUST NEVER DO is commit on a weak score. A single-select answer
reaches set_state and switches something in somebody's house, so the bar to
ACT is high and the bar to ASK is low: below the first, above the second, it
comes back with "I heard 'bob'. Did you mean bulb?" -- which is what a
person would do, and costs nothing but a second.

BIGRAMS COLLAPSE ON SHORT WORDS and that is not a small detail here: "bob"
shares no two-letter gram with "bulb" ({bo, ob} against {bu, ul, lb}), so
the phone scored it 0.000 -- exactly what it scored "banana". Half this
house is four letters long. Edit distance is what makes a short name
reachable at all, and metaphone (optional, the same jellyfish the voice
challenge uses) is what makes a mis-heard one reachable.
"""
from __future__ import annotations

import difflib
import re
from typing import Any, Dict, List, Optional, Sequence, Tuple

from nlu.lexicon import _TENS_WORDS, _UNITS_WORDS

try:                                   # optional, as everywhere else here
    import jellyfish as _jf            # type: ignore
    _metaphone = getattr(_jf, "metaphone", None)
except Exception:                      # noqa: BLE001
    _metaphone = None

#: Words that carry no identity -- dropped before comparison, so "the
#: kitchen lamp please" and "kitchen lamp" score the same. The second line
#: is hesitation and what a contraction leaves once punctuation is gone:
#: "it's Ada", "I'm new", "that's everything", "don't" -> "it s", "i m",
#: "that s", "don t" -- a stray "s" used to stop "it's ada" matching Ada.
FILLERS = {
    "the", "a", "an", "please", "select", "pick", "choose", "my", "that",
    "this", "it", "to", "and", "then", "also", "use", "turn", "of",
    "option", "number", "just", "i", "want", "wanna",
    "um", "uh", "er", "erm", "ah", "hmm", "s", "m", "t", "is", "am", "me",
    "its", "im", "ones", "thanks", "thank", "you",
}

ORDINALS = ("first", "second", "third", "fourth", "fifth",
            "sixth", "seventh", "eighth", "ninth", "tenth")

#: "the last one" -- the last ANSWER on the list, never the narrowed
#: round's "None of those" row that sits under it.
LAST_WORDS = ("last", "final", "bottom")

#: "2nd" is how a transcriber sometimes writes "second".
_ORDINAL_DIGITS = {f"{i + 1}{sfx}": ORDINALS[i] for i, sfx in enumerate(
    ("st", "nd", "rd", "th", "th", "th", "th", "th", "th", "th"))}

#: A number heard as its homophone: "bulb to", "lamp for", "bulb won".
#: Read as the digit ONLY where the list makes that reading the obvious one
#: (see _prepare) -- "switch to the kitchen" keeps its "to".
_HOMOPHONES = {"to": "2", "too": "2", "for": "4", "fore": "4", "won": "1",
               "ate": "8"}

#: The narrowed round's "show me the whole list again" row
#: (WebSocketIO._CHOICE_NONE). A real row for a tap, but never part of
#: "all of them", "both" or "the last one".
NONE_VALUE = " __none__"

#: Whole answers (fillers removed) that name nothing and mean "not any of
#: these" or "stop". They must never be scored: "none" lands on "No, leave
#: it" by prefix and "never mind" on "Good night routine" by sound. An
#: option whose name IS one of these (a "None of these" row, "Stop") is
#: still picked, because the exact name is checked first.
_NOTHING = {
    "cancel", "cancel cancel", "stop", "quit", "exit", "abort", "never mind",
    "nevermind", "forget", "forget about", "none", "neither", "nothing",
    "nobody", "no", "none them", "none those", "none these", "none above",
    "neither them", "neither those", "not those", "not any", "go back",
    "back", "both", "all", "everything", "every",
}
#: The part of _NOTHING that means "not any of THESE" rather than "stop".
#: On a narrowed round it is answered by that round's "None of those" row
#: -- the whole list again -- instead of by nothing.
_NOT_THESE = {"none", "neither", "nothing", "none them", "none those",
              "none these", "none above", "neither them", "neither those",
              "not those", "not any", "no", "wrong", "something else"}

#: Yes / no, as a whole answer or as its first word ("yeah try it").
_YES = {"yes", "yeah", "yep", "yup", "yea", "ya", "sure", "ok", "okay",
        "correct", "right", "affirmative", "absolutely", "definitely",
        "certainly", "do", "please do", "go ahead", "of course", "alright",
        "all right", "sounds good", "yes yes", "that 1", "thats right"}
_NO = {"no", "nope", "nah", "naw", "negative", "no no", "not really",
       "no way", "definitely not", "absolutely not"}
_YES_LEAD = {"yes", "yeah", "yep", "yup", "yea", "sure", "ok", "okay"}
_NO_LEAD = {"no", "nope", "nah"}

#: What follows "all the ..." without naming anything on the list.
_GENERIC = {"lights", "light", "lamps", "devices", "things", "items",
            "options", "rooms", "bulbs", "switches", "plugs", "sockets",
            "fans", "automations", "rules", "scenes", "people", "choices",
            "lot", "them", "those", "these", "everything"}

#: Enough to ACT on, for a single-select that commits to a real device.
COMMIT_FLOOR = 0.66
#: ...and daylight over the runner-up, or it is not a winner.
COMMIT_MARGIN = 0.12
#: A near-exact name needs less daylight -- but never NONE: "kitchen
#: lights" scored 1.0 against "kitchen light" too, and the tie went to
#: whichever came first on the list. (An EXACT name no longer gets here:
#: it is settled before any scoring.)
CERTAIN = 0.85
CERTAIN_MARGIN = 0.05
#: Weak enough to SUGGEST. Deliberately far below COMMIT_FLOOR: these
#: become a question, never an action.
NEAR_FLOOR = 0.45
#: How many to offer back. More than three is a list, not a question.
NEAR_MAX = 3
#: An answer to a list is a few words. Past this it is not an answer, and
#: scoring it -- part by part, word by word, against a couple of hundred
#: rows -- would hold the hub's event loop for seconds.
MAX_HEARD = 160
MAX_PARTS = 8


def _norm(text: str) -> str:
    """Lower case, punctuation to spaces, spoken numbers to digits.

    Underscores go the same way as everything else, which is what lets a
    device stored as `bulb_2` be said as "bulb two".
    """
    s = str(text or "").lower()
    s = re.sub(r"[^a-z0-9\s]", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return _spoken_numbers(s)


def _spoken_numbers(s: str) -> str:
    if not s:
        return s
    words = s.split(" ")
    out: List[str] = []
    i = 0
    while i < len(words):
        value = _NUM_WORD.get(words[i])
        if value is not None and value >= 20 and i + 1 < len(words):
            unit = _NUM_WORD.get(words[i + 1])
            if unit is not None and 1 <= unit <= 9:
                out.append(str(value + unit))
                i += 2
                continue
        out.append(str(value) if value is not None else words[i])
        i += 1
    return " ".join(out)


# The lexicon's number words. "oh" is left out: as a whole word in an answer
# it is an interjection, not a zero.
_NUM_WORD = {w: v for w, v in {**_UNITS_WORDS, **_TENS_WORDS}.items()
             if w != "oh"}


def _bigrams(labels: Sequence[str]) -> set:
    out = set()
    for label in labels:
        words = _norm(label).split()
        out.update(f"{a} {b}" for a, b in zip(words, words[1:], strict=False))
    return out


def _prepare(text: str, labels: Sequence[str]) -> str:
    """_norm, then read the words a number hides behind, against THIS list.

    - "2nd" -> "second".
    - A homophone ("bulb to", "lamp for") becomes its digit when the word
      before it and that digit are adjacent in some label, after "number" /
      "option", or when it is the whole answer.
    - "one" as a PRONOUN is dropped: "the kitchen one", "the first one",
      "that one". It stays a number where the list says it is one ("bulb
      one" over "Bulb 1") and after "number" / "option". Without this,
      "the kitchen one" was "kitchen 1" and matched nothing.
    """
    words = _norm(text).split()
    if not words:
        return ""
    bigrams = _bigrams(labels)
    words = [_ORDINAL_DIGITS.get(w, w) for w in words]
    for i, w in enumerate(words):
        digit = _HOMOPHONES.get(w)
        if digit is None:
            continue
        prev = words[i - 1] if i else ""
        if (len(words) == 1 or prev in ("number", "option")
                or (prev and f"{prev} {digit}" in bigrams)):
            words[i] = digit
    out: List[str] = []
    for i, w in enumerate(words):
        if w == "1" and i and words[i - 1] not in ("number", "option") \
                and f"{words[i - 1]} 1" not in bigrams:
            continue
        out.append(w)
    return " ".join(out)


def _tokens(text: str) -> List[str]:
    return [w for w in _norm(text).split(" ") if w and w not in FILLERS]


def _label_tokens(label: str) -> List[str]:
    """A label's identity words -- or all its words, when every one of them
    is a filler ("A device", "That is it")."""
    return _tokens(label) or [w for w in _norm(label).split(" ") if w]


def _edit_similar(a: str, b: str) -> float:
    """1.0 identical, 0.0 nothing in common -- and unlike a bigram score,
    meaningful at three characters, which is the whole reason it is here.
    Takes text already through _norm, as every caller has it."""
    if not a or not b:
        return 0.0
    longest = max(len(a), len(b))
    return 1.0 - _distance(a, b) / longest


def _distance(a: str, b: str) -> int:
    if a == b:
        return 0
    if not a:
        return len(b)
    if not b:
        return len(a)
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1,
                           prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1]


def _sounds_alike(a: str, b: str) -> bool:
    """Same metaphone key, when jellyfish is installed. Single words only:
    a phrase's key is dominated by its first word and says little."""
    if _metaphone is None:
        return False
    if not a or not b or " " in a or " " in b:
        return False
    try:
        ka, kb = _metaphone(a), _metaphone(b)
    except Exception:                              # noqa: BLE001
        return False
    return bool(ka) and ka == kb


#: What a sound-alike word is worth in the token score: a real signal,
#: never as good as the word itself -- "an" sounds like "Ann" AND "Anna".
_SOUND_WEIGHT = 0.8


def _landed(ta: List[str], tb: List[str], *, prefix: bool,
            weighted: bool = False) -> float:
    """How many said tokens pair off, one each, with a label token: equal,
    close by edit distance, or alike in sound -- and, with `prefix`, one a
    3+-letter prefix of the other.

    `weighted` counts a pairing by HOW close it is (1.0 only for the same
    word). Unweighted, "kitchen lights" landed fully on "kitchen light" and
    the two names tied at a perfect score."""
    pool = list(tb)
    landed = 0.0
    for t in ta:
        best_j, best_w = -1, 0.0
        for j, u in enumerate(pool):
            if t == u:
                w = 1.0
            else:
                w = 0.0
                sim = _edit_similar(t, u)
                if sim >= 0.75:
                    w = sim
                if _sounds_alike(t, u):
                    w = max(w, _SOUND_WEIGHT)
                if prefix and len(t) >= 3 and (u.startswith(t)
                                               or t.startswith(u)):
                    w = max(w, 0.9)
            if w > best_w:
                best_j, best_w = j, w
                if w == 1.0:
                    break
        if best_j >= 0:
            landed += best_w if weighted else 1.0
            pool.pop(best_j)
    return landed


def _coverage(said: str, label: str) -> float:
    """How much of what was SAID the label accounts for -- the 90% rule,
    kept from the phone because it is the rule that makes a clipped word
    ("new bod" for "New board") work."""
    ta, tb = _tokens(said), _label_tokens(label)
    if not ta or not tb:
        return 0.0
    return _landed(ta, tb, prefix=True) / len(ta)


def score(said: str, label: str) -> float:
    """How well an utterance names one option. 0.0 to 1.0."""
    na, nb = _norm(said), _norm(label)
    if not na or not nb:
        return 0.0
    if na == nb:
        return 1.0
    # The label spoken inside a longer sentence, with only filler around
    # it. NOT unconditional: "bulb and socket" must not score 1.0 on
    # `bulb` and quietly ignore that two things were named.
    ta, tb = _tokens(said), _label_tokens(label)
    if re.search(rf"(^|\s){re.escape(nb)}(\s|$)", na):
        if not (set(ta) - set(tb)):
            return 1.0
    token = 0.0
    if ta and tb:
        token = 2 * _landed(ta, tb, prefix=False, weighted=True) \
            / (len(ta) + len(tb))
    ratio = difflib.SequenceMatcher(None, na, nb).ratio()
    return max(token, ratio, _edit_similar(na, nb))


def _ordinal_index(said: str, count: int,
                   labels: Sequence[str] = (),
                   real: Sequence[int] = ()) -> Optional[int]:
    """"the third", "number two", "the last one", or a bare number that is
    the WHOLE answer -- so "turn on two lamps" never picks option two.

    An ordinal or a bare number that is itself a word of some option's
    label is NOT a position: over "first floor light" / "second floor
    light", saying "second floor light" meant that label, and positionally
    it picked option two -- the FIRST floor. Same for "two" over
    "bulb 2" / "bulb 3". Those fall through to scoring instead. An explicit
    "number two" / "option two" is always a position.

    Positions count the list as SHOWN -- a narrowed round's own rows, in
    its own order, which is the list the phone just read out.
    """
    n = _norm(said)
    toks = _tokens(said)
    label_words = {w for label in labels for w in _norm(label).split()}
    for i in range(count):
        if (i < len(ORDINALS) and ORDINALS[i] not in label_words
                and re.search(rf"(^|\s){ORDINALS[i]}(\s|$)", n)):
            return i
        num = str(i + 1)
        if re.search(rf"(^|\s)(number|option)\s+{num}(\s|$)", n):
            return i
        if toks == [num] and num not in label_words:
            return i
    if real and any(re.search(rf"(^|\s){w}(\s|$)", n)
                    for w in LAST_WORDS if w not in label_words):
        return real[-1]
    return None


def _label_of(option: Dict[str, Any]) -> str:
    return str(option.get("label") or option.get("value") or "")


def _values(options: Sequence[Dict[str, Any]]) -> List[str]:
    return [str(o.get("value", "")) for o in options]


def _segments(heard: str) -> List[str]:
    """The parts of an answer that name one thing each: "the kitchen and
    the hall", "kitchen, porch". Split BEFORE punctuation is thrown away,
    because a comma is one of the separators."""
    low = str(heard or "").lower()
    parts = re.split(r",|;|&|\b(?:and|plus|or|as well as|also)\b", low)
    return [p for p in (x.strip() for x in parts) if _tokens(p)][:MAX_PARTS]


# A resolution: ("pick", index, how) | ("ask", [indices]) | ("none",) --
# `how` is "exact", "position" or "polar" for a pick nothing can outrank,
# False for one that was scored.
_Res = Tuple[Any, ...]


def _polar_option(kind: str, options, labels, real) -> Optional[int]:
    """The ONE option that a bare yes / no answers: the one whose value is
    that word, else the one whose label starts with it ("Yes, remove it").
    Two of them ("Yes, and wipe" / "Yes, but keep") is not a yes/no
    question, and nothing is returned. A list with a single real answer is
    answered by yes."""
    by_value = [i for i in real
                if str(options[i].get("value", "")).strip().lower() == kind]
    if len(by_value) == 1:
        return by_value[0]
    if by_value:
        return None
    by_label = [i for i in real if _norm(labels[i]).split()[:1] == [kind]]
    if len(by_label) == 1:
        return by_label[0]
    if kind == "yes" and not by_label and len(real) == 1:
        return real[0]
    return None


def _negated(text: str) -> bool:
    words = _norm(text).split()
    joined = " ".join(words)
    if joined.startswith("never mind") or joined.startswith("nevermind"):
        return False
    return ("not" in words or "dont" in words or "never" in words
            or "don t" in joined or "do not" in joined)


def _yes_no(prepared, options, labels, real, confirming) -> Optional[_Res]:
    toks = _tokens(prepared)
    joined = " ".join(toks)
    kind, rest = None, []
    if joined in _YES:
        kind = "yes"
    elif joined in _NO:
        kind = "no"
    elif _negated(prepared):
        return None
    elif toks and toks[0] in _YES_LEAD:
        kind, rest = "yes", [t for t in toks[1:] if t not in _YES_LEAD]
    elif toks and toks[0] in _NO_LEAD:
        kind, rest = "no", [t for t in toks[1:] if t not in _NO_LEAD]
    if kind is None:
        return None
    target = _polar_option(kind, options, labels, real)
    if target is None:
        return None
    if rest:
        # "yeah try it" -- the rest must name the SAME option, or it is
        # two answers at once ("yes no") and nothing is committed.
        r = _resolve_one(" ".join(rest), options, labels, real,
                         confirming=confirming, polar=False)
        if r[0] != "pick":
            return None
        if r[1] != target:
            return ("none",)
    return ("pick", target, "polar")


def _resolve_one(prepared: str, options, labels, real, *,
                 confirming: bool, polar: bool = True) -> _Res:
    """One answer to a list: a position, an exact name, a yes/no, or the
    best score -- or a question back, or nothing."""
    # A BARE NUMBER THAT IS BOTH A NAME AND A POSITION. The phone reads a
    # list out numbered -- "1, Bulb 3. 2, Bulb 2. 3, Desk lamp" -- so
    # "three" is Bulb 3 by name and the desk lamp by position. Either
    # reading switches the wrong thing half the time; ask. ("Number three"
    # is always the position; a number that is both, pointing at the same
    # row, is simply that row.)
    bare = _tokens(prepared)
    if (len(bare) == 1 and bare[0].isdigit()
            and not re.search(r"(^|\s)(number|option)\s", prepared)):
        pos = int(bare[0]) - 1
        named = [i for i in range(len(options))
                 if bare[0] in _norm(labels[i]).split()]
        if named and 0 <= pos < len(options) and pos not in named:
            return ("ask", named + [pos])
    idx = _ordinal_index(prepared, len(options), labels, real)
    if idx is not None:
        return ("pick", idx, "position")
    toks = _tokens(prepared)
    if not toks:
        return ("none",)
    # THE EXACT NAME, before anything fuzzy. "kitchen lights" and "Anna"
    # used to tie at 1.0 with "kitchen light" and "Ann" and go to whichever
    # was listed first. Also matched with the spaces squeezed out:
    # "goodnight", "bed room", "login".
    exact = [i for i in range(len(options)) if _label_tokens(labels[i]) == toks]
    if not exact:
        squashed = "".join(toks)
        exact = [i for i in range(len(options))
                 if "".join(_label_tokens(labels[i])) == squashed]
    if len(exact) == 1:
        return ("pick", exact[0], "exact")
    if exact:
        return ("ask", exact)
    if polar:
        r = _yes_no(prepared, options, labels, real, confirming)
        if r is not None:
            return r
    joined = " ".join(toks)
    if joined in _NOT_THESE or joined in _NO:
        # The narrowed round's row, or a list's own "None of these" (the
        # serial-port picker has one).
        none_row = [i for i in range(len(options)) if i not in real] or [
            i for i in real if _norm(labels[i]).split()[:1] == ["none"]]
        if len(none_row) == 1:
            return ("pick", none_row[0], "exact")
    if joined in _NOTHING or joined in _NO:
        return ("none",)
    # THE 90% RULE: when everything said lands inside exactly one option,
    # that option is the answer even under the strict rules -- "new bod"
    # is "New board". Two options swallowing the whole utterance is
    # ambiguity, and falls through.
    covered = [i for i in range(len(options))
               if labels[i] and _coverage(prepared, labels[i]) >= 0.9]
    if len(covered) == 1:
        return ("pick", covered[0], False)
    scored = sorted(
        ((score(prepared, labels[i]), i) for i in range(len(options))),
        key=lambda pair: (-pair[0], pair[1]))
    best, best_i = scored[0]
    runner = scored[1][0] if len(scored) > 1 else 0.0
    floor = NEAR_FLOOR if confirming else COMMIT_FLOOR
    margin = CERTAIN_MARGIN if best >= CERTAIN else COMMIT_MARGIN
    if best >= floor and (len(options) == 1 or best - runner >= margin):
        return ("pick", best_i, False)
    # Not sure enough to act. Sure enough to ask?
    #
    # A bare score is not enough to earn a suggestion: difflib gives "the
    # weather" 0.45-odd against "card reader" on shared letters alone, and
    # offering that back is worse than admitting nothing was heard. A
    # mis-hearing almost never changes the LEADING SOUND of a word --
    # "bulb"/"bob", "socket"/"sock it", "buzzer"/"buzzah" all keep it --
    # so a weak candidate must either start the same way or score well
    # clear of the floor.
    first = _first_letter(prepared)
    near = [i for s, i in scored
            if s >= NEAR_FLOOR
            and (first == _first_letter(labels[i]) or s >= 0.6)][:NEAR_MAX]
    return ("ask", near) if near else ("none",)


def _by_words(prepared, options, labels, real, confirming) -> Optional[List[int]]:
    """A multi-select answer with no "and" in it -- "kitchen hall porch" --
    read one word at a time. Every word must name exactly one option, or be
    a word shared by options already named ("kitchen light hall light":
    "light" is in both). Anything else and the reading is refused whole."""
    toks = _tokens(prepared)
    if not 2 <= len(toks) <= MAX_PARTS:
        return None
    picks: List[int] = []
    shared: List[List[int]] = []
    for t in toks:
        r = _resolve_one(t, options, labels, real, confirming=confirming,
                         polar=False)
        if r[0] == "pick":
            if r[1] not in picks:
                picks.append(r[1])
        elif r[0] == "ask":
            shared.append(r[1])
        else:
            return None
    if len(picks) < 2 or any(not set(s) & set(picks) for s in shared):
        return None
    return picks


def _one_or_words(prepared, options, labels, real, confirming) -> _Res:
    """One part of a multi-select answer: a position or an exact name
    outright, else a word-by-word reading that names two or more, else
    whatever the single-select rules make of it."""
    r = _resolve_one(prepared, options, labels, real, confirming=confirming,
                     polar=False)
    if r[0] == "pick" and r[2] == "exact":
        return ("many", [r[1]])
    # Before a position: "first second" is two of them, not the first.
    split = _by_words(prepared, options, labels, real, confirming)
    if split:
        return ("many", split)
    if r[0] == "pick":
        return ("many", [r[1]])
    return r


def _resolve_many(heard: str, options, labels, real, *,
                  confirming: bool) -> _Res:
    """Every option named in one answer to a multi-select. All of them or
    none: a part that names nothing makes the whole answer a question,
    because a multi-select pick is COMMITTED as heard, not shown for
    correction first."""
    prepared = _prepare(heard, labels)
    segments = _segments(heard)
    if len(segments) > 1:
        # A name that itself holds an "and" ("Salt and pepper lamp").
        toks = _tokens(prepared)
        exact = [i for i in real if _label_tokens(labels[i]) == toks]
        if len(exact) == 1:
            return ("many", exact)
    else:
        segments = [heard]
    picks: List[int] = []
    near: List[int] = []
    failed = False
    for seg in segments:
        p = _prepare(seg, labels)
        got = _one_or_words(p, options, labels, real, confirming)
        if got[0] == "many":
            got = got[1]
        else:
            if len(segments) == 1:
                return got
            got = None
        if got:
            picks.extend(i for i in got if i not in picks)
            continue
        failed = True
        r = _resolve_one(p, options, labels, real, confirming=confirming,
                         polar=False)
        if r[0] == "ask":
            near.extend(i for i in r[1] if i not in near)
    if not failed and picks:
        return ("many", picks)
    offered = picks + [i for i in near if i not in picks]
    return ("ask", offered) if offered else ("none",)


#: "all [of them / the lights] except ...". Only words that name nothing
#: may sit between "all" and "except": "all the kitchen lights except the
#: night light" is NOT everything but the night light, and is refused.
_ALL_EXCEPT = re.compile(
    r"^(?:(?:select|pick|choose)\s+)?(?:all|everything|every)"
    r"(?:\s+(?:of|the|" + "|".join(sorted(_GENERIC)) + r"))*"
    r"\s+(?:except|but\s+not|but|apart\s+from|other\s+than|besides|"
    r"excluding|minus|without|save|not|leave\s+out|skip)"
    r"(?:\s+(?:for|4))?\s+(.+)$")


def _all_forms(heard: str, options, labels, real, *, allow_all: bool,
               confirming: bool) -> Optional[_Res]:
    """"all of them", "everything", "both", "all except the fan", "all the
    kitchen ones". None when the answer is none of these shapes.

    "all" used to be any answer that merely STARTED with the word, so "all
    except the fan" switched the fan on with everything else."""
    prepared = _prepare(heard, labels)
    toks = _tokens(prepared)
    if not toks:
        return None
    if toks[0] == "both" and all(t in ("both", "them", "those", "these")
                                 for t in toks):
        return ("many", list(real)) if len(real) == 2 else ("none",)
    if toks[0] not in ("all", "everything", "every", "everyone"):
        return None
    m = _ALL_EXCEPT.match(" ".join(w for w in prepared.split()
                                   if w not in ("please", "um", "uh", "er")))
    if m:
        if not allow_all:
            return ("none",)
        left_out = _resolve_many(m.group(1), options, labels, real,
                                 confirming=confirming)
        if left_out[0] != "many":
            return ("none",)
        keep = [i for i in real if i not in left_out[1]]
        return ("many", keep) if keep else ("none",)
    rest = [t for t in toks[1:] if t not in ("them", "those", "these", "lot")]
    if not rest:
        return ("many", list(real)) if allow_all else ("none",)
    # "all the kitchen ones": every option those words name.
    phrase = " ".join(rest)
    covered = [i for i in real if _coverage(phrase, labels[i]) >= 0.9]
    if covered:
        if len(covered) == len(real) and not allow_all:
            return ("none",)
        return ("many", covered)
    if allow_all and all(t in _GENERIC for t in rest):
        return ("many", list(real))
    return ("none",)


def match(heard: str,
          options: Sequence[Dict[str, Any]],
          *, multi: bool = False,
          allow_all: bool = False,
          confirming: bool = False
          ) -> Tuple[List[str], List[Dict[str, Any]]]:
    """(values to answer with, options worth suggesting instead).

    At most one of the two is ever non-empty. Values means "this is the
    answer"; suggestions mean "ask her which of these she meant"; both
    empty means nothing was named -- including "cancel", "none", "neither"
    and "never mind", which are never scored against the names.

    `confirming` is the SECOND round -- these two or three options are the
    ones just offered as "did you mean...", and this utterance is choosing
    between them. The commit floor exists to stop a weak guess reaching a
    device out of nine; once the field is this narrow and the person has
    been shown it, that danger is gone and the floor is the wrong rule.
    Without this, the same words mis-heard the same way hit the same wall
    twice -- "when i said it again it didnt flinch" -- which is the one
    thing saying it again must never do. A margin is still required when
    more than one was offered.

    A multi-select answer is COMMITTED as heard (the phone sends the words,
    the hub acts), so it is held to the single-select bar part by part:
    "the kitchen and the hall" is two answers, each of which must be clear
    on its own. It used to be one score per option against the whole
    sentence, over a lower floor -- which ticked all four of "kitchen
    light", "kitchen lights", "kitchen night light" and "hall light" for
    "kitchen light".
    """
    options = [o for o in options if isinstance(o, dict)]
    heard = str(heard or "").strip()[:MAX_HEARD]
    if not heard or not options:
        return [], []
    labels = [_label_of(o) for o in options]
    real = [i for i, o in enumerate(options)
            if str(o.get("value", "")) != NONE_VALUE]

    if multi:
        r = _all_forms(heard, options, labels, real, allow_all=allow_all,
                       confirming=confirming)
        if r is None:
            r = _resolve_many(heard, options, labels, real,
                              confirming=confirming)
        if r[0] == "many":
            picked = [i for i in r[1] if i in real] or [
                i for i in r[1]][:1]
            return _values([options[i] for i in sorted(picked)]), []
        if r[0] == "pick":
            return _values([options[r[1]]]), []
    else:
        prepared = _prepare(heard, labels)
        r = _resolve_one(prepared, options, labels, real,
                         confirming=confirming)
        if r[0] == "pick" and not r[2]:
            # TWO THINGS NAMED is not an answer to "which one?": "kitchen
            # light and hall light" scored its way onto "kitchen NIGHT
            # light", the one label that shares the most words with both.
            segments = _segments(heard)
            if len(segments) > 1:
                named = []
                for seg in segments:
                    s = _resolve_one(_prepare(seg, labels), options, labels,
                                     real, confirming=confirming)
                    if s[0] == "pick" and s[1] not in named:
                        named.append(s[1])
                if len(named) > 1:
                    r = ("ask", named)
        if r[0] == "pick":
            return _values([options[r[1]]]), []
    if r[0] == "ask":
        return [], [options[i] for i in r[1] if i in real][:NEAR_MAX]
    return [], []


def _first_letter(text: str) -> str:
    """The first letter of the first word that carries identity -- "the
    weather" is w, not t."""
    toks = _tokens(text)
    return toks[0][0] if toks and toks[0] else ""

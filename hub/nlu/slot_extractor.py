"""
slot_extractor.py — Extraction logic + command builder for Pi 4B.

Owns ZERO English vocabulary. Every word — direction, magnitude, unit,
synonym, and the closed-class particles that used to be spelled out here —
comes from lexicon.py, the single source of truth. The regexes below are
GENERATED from it, including the unit alternation, which was previously typed
out twice in this file in two versions that disagreed.

This module only does mechanical work:
  - regex to pull numbers out of text
  - assemble a SlotResult
  - decide whether a number is a VALUE or part of a device name
  - assemble the slots dict

Zero ML. Pure regex + lexicon lookups.

NOTE: the determiner / phrasal-verb / on-off-synonym sets that used to be
defined inline inside extract() now live in lexicon.py, where the docstring
above always claimed all vocabulary lived. They were also being rebuilt as
four fresh sets on every single call.
"""

import re
from typing import Any, Dict, FrozenSet, List, Literal, Optional, Tuple, TypedDict

from nlu.lexicon import (
    _MODIFIER_LOOKUP,        # word -> (category, magnitude)
    word_direction,          # word -> "increase"/"decrease"/None
    is_superlative,          # word -> True if -est/extreme form
    normalize_unit,          # unit token -> canonical name
    DECREASE_MAXIMIZERS,     # maximizer words that point DOWN
    COMMAND_STOPWORDS,       # function words that never name a device
    PRONOUNS,
    DETERMINERS,             # the/a/my/... — moved out of extract()
    ON_SYNONYMS,             # activate/enable/start/...
    OFF_SYNONYMS,            # deactivate/disable/stop/...
    phrasal_power_switch,    # "shut it down" is OFF, not a delta
    is_prepositional_on,     # "volume ON THE tv" is not a switch
    VALUE_PREPOSITIONS,      # to/at/by/... — a number after one of these is a value
    # Section 6 of lexicon.py: the closed-class particles this module used to
    # spell out inline. The docstring's "owns ZERO English vocabulary" claim is
    # now true again.
    DELTA_INTRODUCERS,
    TARGET_PREPOSITIONS,
    PERIPHRASTIC_UP, PERIPHRASTIC_DOWN, PERIPHRASTIC_EXTREME,
    SWITCH_WORDS,
    unit_pattern,            # one generated alternation for BOTH unit regexes
    NUMBER_WORDS,            # "twenty", "hundred", "half" ...
    words_to_number,         # "twenty three" -> 23.0
    find_number_span,
    find_colour,             # "make it blue" -> (0, 0, 255)
    _TENS_WORDS, _UNITS_WORDS,  # the time reader's "seven", "thirty"
)

def _digitize(text: str) -> str:
    """Rewrite spoken numbers as digits, BEFORE any pattern runs.

    "set it to twenty three"  ->  "set it to 23"
    "turn it up by five"      ->  "turn it up by 5"

    Done here, once, rather than by teaching every regex to also match words.
    Everything downstream -- the unit capture, the explicit-delta pattern, the
    name-suffix guard, evidence strength -- then works on digits exactly as it
    always has, and a spoken number behaves identically to a typed one. Adding
    word alternatives to each pattern instead would have meant the guard that
    stops "turn off bulb 4" becoming a VALUE had to be re-implemented for
    "bulb four", in a second place, where it would drift.

    Only runs where a number word is found, so ordinary text is untouched.

    Trailing punctuation and hyphens are looked THROUGH: a transcriber writes
    "set it to fifty." and "twenty-three", and neither token was in
    NUMBER_WORDS, so the number silently vanished and the command carried no
    value at all.
    """
    # (core word, trailing punctuation) per token, hyphenated number words
    # split into their parts ("twenty-three" -> "twenty", "three").
    toks: List[Tuple[str, str]] = []
    for raw in text.split():
        core = raw.rstrip(",.;:!?")
        tail = raw[len(core):]
        parts = core.split("-")
        if len(parts) > 1 and all(p.lower() in NUMBER_WORDS for p in parts):
            toks.extend((p, "") for p in parts[:-1])
            toks.append((parts[-1], tail))
        else:
            toks.append((core, tail))
    if not any(c.lower() in NUMBER_WORDS for c, _ in toks):
        return text
    words = [c for c, _ in toks]
    out: List[str] = []
    i = 0
    while i < len(words):
        if words[i].lower() in NUMBER_WORDS:
            end = find_number_span(words, i)
            value = words_to_number(words[i:end])
            if value is not None:
                # Integers as integers: "twenty" must become "20", not "20.0",
                # or the value regex captures a decimal the user never said.
                out.append((str(int(value)) if value == int(value) else str(value))
                           + toks[end - 1][1])
                i = end
                continue
        out.append(words[i] + toks[i][1])
        i += 1
    return " ".join(out)


# ═══════════════════════════════════════════════════════════════
# TIMES -- read FIRST, and their words removed before any number is read,
# so "at seven thirty" is a time, not the value 37.
# ═══════════════════════════════════════════════════════════════
# One reader for a time said in a command, read from the words AS SAID
# (before _digitize, which would make "two fifteen" 17). What counts --
# device names are the risk:
#   * a length needs a unit ("thirty seconds", "half an hour"; "the second
#     lamp" is not one);
#   * a clock needs a clock shape ("2:15", "7 pm", "quarter past two",
#     "noon"), or hour+minutes after a time word ("at seven thirty"), or a
#     bare hour after "at" with a day or part of day said ("tomorrow at
#     three"). "Set the heat to twenty two" stays a temperature;
#   * a day is "today", "last night", a weekday said as a day ("on Monday",
#     "next Monday"), or a date ("the 14th"). A lamp called "Monday" stays a
#     lamp.
# `expect_time=True` (a bare number can only be a time) loosens the bare-hour
# and weekday rules. read() returns what was said (a 12-hour clock stays
# ambiguous); nothing here turns it into a moment.

class When(TypedDict, total=False):
    #: A length of time on its own, seconds ("skip thirty seconds").
    duration: float
    #: "five minutes ago" -- seconds before now.
    ago: float
    #: "in ten minutes" / "after an hour" -- seconds from now.
    after: float
    #: "the last five minutes" -- a stretch ending now, seconds long.
    last: float
    #: Seconds after midnight. With clock_12h the hour was said without am,
    #: pm or a part of the day, so 3:00 may be 15:00.
    clock: float
    clock_12h: bool
    #: Days from today: 0 today, -1 yesterday, 1 tomorrow.
    day: int
    #: 0 Monday .. 6 Sunday, and which way: -1 last, 1 next, 0 nearest.
    weekday: int
    weekday_way: int
    #: (month or 0 for "this month", day of month).
    date: Tuple[int, int]
    #: morning, afternoon, evening or night.
    part: str
    #: The character spans the time words took, in the text as given.
    spans: List[Tuple[int, int]]


# ── vocabulary ──────────────────────────────────────────────────────────────

_UNIT_SECONDS: Dict[str, float] = {
    "second": 1, "seconds": 1, "sec": 1, "secs": 1,
    "minute": 60, "minutes": 60, "min": 60, "mins": 60,
    "hour": 3600, "hours": 3600, "hr": 3600, "hrs": 3600,
}
_WEEKDAYS: Dict[str, int] = {
    "monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3,
    "friday": 4, "saturday": 5, "sunday": 6,
}
_MONTHS: Dict[str, int] = {
    "january": 1, "february": 2, "march": 3, "april": 4, "may": 5,
    "june": 6, "july": 7, "august": 8, "september": 9, "october": 10,
    "november": 11, "december": 12,
}
_ORDINAL_WORDS: Dict[str, int] = {
    "first": 1, "second": 2, "third": 3, "fourth": 4, "fifth": 5,
    "sixth": 6, "seventh": 7, "eighth": 8, "ninth": 9, "tenth": 10,
    "eleventh": 11, "twelfth": 12, "thirteenth": 13, "fourteenth": 14,
    "fifteenth": 15, "sixteenth": 16, "seventeenth": 17, "eighteenth": 18,
    "nineteenth": 19, "twentieth": 20, "thirtieth": 30,
}
_PARTS = ("morning", "afternoon", "evening", "night")
#: Words after which an hour-and-minutes is a clock ("at seven thirty",
#: "go to two fifteen", "from three until four").
_CLOCK_CUES = {"at", "around", "about", "by", "until", "till", "til",
               "from", "since", "to", "before", "after"}
#: Fewer cues for a BARE hour when a day is said ("yesterday at 3"). Not
#: "to"/"by": "set the heat to 22 tomorrow" is a value.
_BARE_HOUR_CUES = {"at", "around", "about", "until", "till", "til", "since",
                   "from"}
_DAY_CUES = {"on", "last", "this", "next", "since", "from", "until", "till",
             "by", "before", "after"}
_AMPM = {"am", "pm"}
#: What may follow a date said without its month.
_AFTER_DATE = {"at", "in", "on", "from", "to", "until", "till", "and",
               "around", "about", "before", "after", "morning", "afternoon",
               "evening", "night", "recording", "recordings", "footage"}

#: "a.m."/"p.m." made single tokens without changing length, so spans still
#: index the caller's text.
_NORMALISE = [(re.compile(r"\ba\.m\.?", re.I), "am"),
              (re.compile(r"\bp\.m\.?", re.I), "pm")]
_TOKEN_RE = re.compile(r"\d{1,2}:\d{2}|\d+(?:\.\d+)?(?:st|nd|rd|th|am|pm)?"
                       r"|o'clock|[a-z]+(?:'s)?", re.I)


def _same_length(text: str) -> str:
    for pattern, word in _NORMALISE:
        text = pattern.sub(lambda m: word.ljust(len(m.group(0))), text)
    return text


class _Tok:
    __slots__ = ("w", "s", "e", "possessive")

    def __init__(self, w: str, s: int, e: int) -> None:
        self.possessive = w.endswith("'s")
        self.w = w[:-2] if self.possessive else w
        if self.w == "o'clock":
            self.w = "oclock"
        self.s, self.e = s, e


# ── the reader ──────────────────────────────────────────────────────────────

class _Reader:
    def __init__(self, text: str, expect_time: bool) -> None:
        norm = _same_length(text.lower())
        self.toks = [_Tok(m.group(0), m.start(), m.end())
                     for m in _TOKEN_RE.finditer(norm)]
        self.used = [False] * len(self.toks)
        self.expect = expect_time
        self.out: When = {}

    # helpers ---------------------------------------------------------------
    def w(self, i: int) -> str:
        return self.toks[i].w if 0 <= i < len(self.toks) else ""

    def take(self, i: int, j: int) -> None:
        for k in range(i, j):
            self.used[k] = True

    def free(self, i: int, j: int) -> bool:
        return all(0 <= k < len(self.toks) and not self.used[k]
                   for k in range(i, j))

    def small(self, i: int, hi: int = 59) -> Optional[Tuple[int, int]]:
        """A whole number 0..hi at i: digits, or "seven", "fifteen",
        "forty five". (value, next index)."""
        w = self.w(i)
        if w.isdigit():
            v = int(w)
            return (v, i + 1) if v <= hi else None
        if w in _TENS_WORDS:
            v = _TENS_WORDS[w]
            u = _UNITS_WORDS.get(self.w(i + 1))
            if u is not None and 0 < u < 10:
                return (v + u, i + 2) if v + u <= hi else None
            return (v, i + 1) if v <= hi else None
        if w in _UNITS_WORDS and w not in ("oh", "nought"):
            v = _UNITS_WORDS[w]
            return (v, i + 1) if v <= hi else None
        return None

    def number(self, i: int) -> Optional[Tuple[float, int]]:
        """Any amount at i ("90", "1.5", "one and a half", "twenty")."""
        w = self.w(i)
        try:
            return float(w), i + 1
        except ValueError:
            pass
        words = [t.w for t in self.toks]
        end = find_number_span(words, i)
        if end > i:
            v = words_to_number(words[i:end])
            if v is not None:
                return v, end
        return None

    # lengths of time -------------------------------------------------------
    def one_length(self, i: int) -> Optional[Tuple[float, int]]:
        w = self.w(i)
        # "half an hour", "half a minute"
        if w == "half" and self.w(i + 1) in ("an", "a") \
                and self.w(i + 2) in _UNIT_SECONDS:
            return 0.5 * _UNIT_SECONDS[self.w(i + 2)], i + 3
        # "a quarter of an hour", "quarter of an hour", "quarter hour"
        q = i + 1 if w == "a" and self.w(i + 1) == "quarter" else i
        if self.w(q) == "quarter":
            if self.w(q + 1) == "of" and self.w(q + 2) in ("an", "a") \
                    and self.w(q + 3) == "hour":
                return 900.0, q + 4
            if self.w(q + 1) == "hour":
                return 900.0, q + 2
        # "a minute", "an hour" ("a second" is a length too)
        if w in ("a", "an") and self.w(i + 1) in _UNIT_SECONDS:
            n, j = 1.0, i + 1
        # "a couple of minutes" (2), "a few hours" (3)
        elif (w == "a" and self.w(i + 1) == "couple" and self.w(i + 2) == "of"
              and self.w(i + 3) in _UNIT_SECONDS):
            n, j = 2.0, i + 3
        elif (w == "a" and self.w(i + 1) == "few"
              and self.w(i + 2) in _UNIT_SECONDS):
            n, j = 3.0, i + 2
        else:
            got = self.number(i)
            if got is None or self.w(got[1]) not in _UNIT_SECONDS:
                return None
            n, j = got
        secs = n * _UNIT_SECONDS[self.w(j)]
        j += 1
        # "a minute and a half"
        if self.w(j) == "and" and self.w(j + 1) == "a" and self.w(j + 2) == "half":
            secs += 0.5 * _UNIT_SECONDS[self.w(j - 1)]
            j += 3
        return secs, j

    def length(self, i: int) -> Optional[Tuple[float, int]]:
        """One length, or several added ("an hour and ten minutes",
        "1 minute 30 seconds")."""
        first = self.one_length(i)
        if first is None:
            return None
        total, j = first
        while True:
            k = j + 1 if self.w(j) == "and" else j
            more = self.one_length(k)
            if more is None:
                return total, j
            total, j = total + more[0], more[1]

    # clocks ----------------------------------------------------------------
    def ampm(self, j: int) -> Tuple[Optional[str], int]:
        w = self.w(j)
        if w in _AMPM:
            return w, j + 1
        if w == "oclock":
            return "oclock", j + 1
        return None, j

    def part_after(self, j: int) -> Tuple[Optional[str], int]:
        """ "in the morning", "in the afternoon", "at night" after a clock."""
        if self.w(j) == "in" and self.w(j + 1) == "the" and self.w(j + 2) in _PARTS[:3]:
            return self.w(j + 2), j + 3
        if self.w(j) == "at" and self.w(j + 1) == "night":
            return "night", j + 2
        if self.w(j) in ("tonight",):
            return "night", j + 1
        return None, j

    def clock_at(self, i: int, cue: bool, dayish: bool
                 ) -> Optional[Tuple[float, bool, int]]:
        """(seconds after midnight, 12-hour?, next index) for a clock that
        starts at i."""
        w = self.w(i)
        # 2:15, 14:30
        m = re.fullmatch(r"(\d{1,2}):(\d{2})", w)
        if m:
            h, mi = int(m.group(1)), int(m.group(2))
            if h > 23 or mi > 59:
                return None
            tag, j = self.ampm(i + 1)
            return self._hm(h, mi, tag, j)
        # 7pm, 7am
        m = re.fullmatch(r"(\d{1,2})(am|pm)", w)
        if m:
            return self._hm(int(m.group(1)), 0, m.group(2), i + 1)
        if w in ("noon", "midday"):
            return 12 * 3600.0, False, i + 1
        if w == "midnight":
            return 0.0, False, i + 1
        if w == "twelve" and self.w(i + 1) == "noon":
            return 12 * 3600.0, False, i + 2
        # quarter past two, half past three, ten to four, five after six
        lead = None
        if w == "quarter" or (w == "a" and self.w(i + 1) == "quarter"):
            lead, j = 15, (i + 2 if w == "a" else i + 1)
        elif w == "half":
            lead, j = 30, i + 1
        else:
            got = self.small(i, 59)
            if got is not None:
                lead, j = got
                if self.w(j) in ("minutes", "minute"):
                    j += 1
        if lead is not None and self.w(j) in ("past", "after", "to", "before"):
            rel = self.w(j)
            hour = self.small(j + 1, 12)
            if hour is not None and hour[0] >= 1:
                tag, k = self.ampm(hour[1])
                sure = (w in ("quarter", "half", "a") or rel == "past"
                        or tag is not None or self.expect or cue)
                if rel in ("to", "before") and lead == 30:
                    sure = False                       # "half to four" is not said
                if sure:
                    h = hour[0]
                    mins = lead if rel in ("past", "after") else 60 - lead
                    if rel in ("to", "before"):
                        h = 12 if h == 1 else h - 1
                    return self._hm(h, mins, tag, k)
        # seven thirty, two fifteen pm, twelve oh five, 7 30
        hour = self.small(i, 23)
        if hour is None:
            return None
        h, j = hour
        tag, k = self.ampm(j)
        if tag is not None:
            if tag != "oclock" and not 1 <= h <= 12:
                return None
            return self._hm(h, 0, tag, k)
        mins = None
        if self.w(j) == "oh":
            got = self.small(j + 1, 9)
            if got is not None:
                mins, j2 = got
        elif self.w(j) in _TENS_WORDS or (self.w(j) in _UNITS_WORDS
                                           and _UNITS_WORDS[self.w(j)] >= 10):
            got = self.small(j, 59)
            if got is not None:
                mins, j2 = got
        elif re.fullmatch(r"\d{2}", self.w(j)) and int(self.w(j)) <= 59:
            mins, j2 = int(self.w(j)), j + 1
        if mins is not None and 1 <= h <= 12:
            tag, k = self.ampm(j2)
            if tag is not None or cue or self.expect or dayish:
                return self._hm(h, mins, tag, k)
            return None
        # a bare hour: "at 3" with a day said, or where only a time can go
        bare_cue = self.w(i - 1) in _BARE_HOUR_CUES
        if ((bare_cue and dayish) or (self.expect and (cue or i == 0
                                                       or bare_cue))) and h <= 23:
            return self._hm(h, 0, None, j)
        return None

    @staticmethod
    def _hm(h: int, mi: int, tag: Optional[str], j: int
            ) -> Optional[Tuple[float, bool, int]]:
        if tag == "pm" and h < 12:
            h += 12
        elif tag == "am" and h == 12:
            h = 0
        if h > 23 or mi > 59:
            return None
        twelve = tag in (None, "oclock") and 1 <= h <= 12
        return h * 3600.0 + mi * 60.0, twelve, j

    # days ------------------------------------------------------------------
    def ordinal(self, i: int) -> Optional[Tuple[int, int]]:
        w = self.w(i)
        m = re.fullmatch(r"(\d{1,2})(st|nd|rd|th)", w)
        if m and 1 <= int(m.group(1)) <= 31:
            return int(m.group(1)), i + 1
        if w in ("twenty", "thirty") and self.w(i + 1) in _ORDINAL_WORDS \
                and _ORDINAL_WORDS[self.w(i + 1)] < 10:
            v = _TENS_WORDS[w] + _ORDINAL_WORDS[self.w(i + 1)]
            return (v, i + 2) if v <= 31 else None
        if w in _ORDINAL_WORDS:
            return _ORDINAL_WORDS[w], i + 1
        return None

    def day_at(self, i: int) -> Optional[int]:
        """Read a day starting at i into self.out; the next index, or None."""
        w = self.w(i)
        nxt = self.w(i + 1)
        if w in ("the",) and nxt == "day" and self.w(i + 2) in ("before", "after"):
            i += 1
            w, nxt = self.w(i), self.w(i + 1)
        if w == "day" and nxt == "before" and self.w(i + 2) == "yesterday":
            self.out["day"] = -2
            return i + 3
        if w == "day" and nxt == "after" and self.w(i + 2) == "tomorrow":
            self.out["day"] = 2
            return i + 3
        if w in ("today", "yesterday", "tomorrow", "tonight"):
            self.out["day"] = {"today": 0, "tonight": 0,
                               "yesterday": -1, "tomorrow": 1}[w]
            j = i + 1
            if w == "tonight":
                self.out["part"] = "night"
            elif self.w(j) in _PARTS:
                self.out["part"] = self.w(j)
                j += 1
            return j
        if w == "last" and nxt == "night":
            self.out["day"], self.out["part"] = -1, "night"
            return i + 2
        if w == "this" and nxt in _PARTS[:3]:
            self.out["day"], self.out["part"] = 0, nxt
            return i + 2
        if w in ("in", "at") and nxt in ("the", "night"):
            if w == "in" and self.w(i + 2) in _PARTS[:3]:
                self.out["part"] = self.w(i + 2)
                return i + 3
            if w == "at" and nxt == "night":
                self.out["part"] = "night"
                return i + 2
        # weekdays
        way = {"last": -1, "next": 1, "this": 0}.get(w)
        k = i + 1 if way is not None else i
        if self.w(k) in _WEEKDAYS:
            tok = self.toks[k]
            said_as_day = (way is not None or tok.possessive or self.expect
                           or self.w(i - 1) in _DAY_CUES
                           or self.w(k + 1) in _PARTS or self.w(k + 1) == "at")
            if not said_as_day:
                return None
            self.out["weekday"] = _WEEKDAYS[self.w(k)]
            self.out["weekday_way"] = way if way is not None else 0
            j = k + 1
            if self.w(j) in _PARTS:
                self.out["part"] = self.w(j)
                j += 1
            return j
        # dates: "October 14th", "14th of October", "the 14th"
        if w in _MONTHS:
            got = self.ordinal(i + 1) or self.small(i + 1, 31)
            if got and got[0] >= 1:
                self.out["date"] = (_MONTHS[w], got[0])
                return got[1]
        start = i + 1 if w == "the" else i
        got = self.ordinal(start)
        if got is not None and (w == "the" or self.w(i - 1) == "on"):
            j = got[1]
            month = 0
            if self.w(j) == "of" and self.w(j + 1) in _MONTHS:
                month, j = _MONTHS[self.w(j + 1)], j + 2
            # Without a month, a date only where nothing follows that it
            # could number ("the 14th at 3", never "the 2nd bulb").
            if month == 0 and self.w(j) and self.w(j) not in _AFTER_DATE:
                return None
            self.out["date"] = (month, got[0])
            return j
        return None

    # the whole sentence ----------------------------------------------------
    def run(self) -> When:
        n = len(self.toks)
        # Days first: a day said anywhere makes "at 3" a time.
        i = 0
        while i < n:
            if not self.used[i]:
                j = self.day_at(i)
                if j is not None and j > i and self.free(i, j):
                    self.take(i, j)
                    i = j
                    continue
            i += 1
        dayish = any(k in self.out for k in ("day", "weekday", "date", "part"))

        i = 0
        while i < n:
            if self.used[i]:
                i += 1
                continue
            w = self.w(i)
            # "in ten minutes", "after an hour", "in THE NEXT ten minutes",
            # "in the coming hour", "in the five hours TO COME"
            if w in ("in", "after", "within"):
                k = i + 1
                if self.w(k) == "the":
                    k += 1
                    if self.w(k) in ("next", "coming"):
                        k += 1
                elif self.w(k) in ("next", "coming"):
                    k += 1
                got = self.length(k)
                # "in the next HOUR": one of it
                if got is None and k > i + 1 and self.w(k) in ("minute", "hour", "second"):
                    got = (_UNIT_SECONDS[self.w(k)], k + 1)
                if got and (self.w(got[1]), self.w(got[1] + 1)) == ("to", "come"):
                    got = (got[0], got[1] + 2)
                elif got and self.w(got[1]) == "ahead":
                    got = (got[0], got[1] + 1)
                if got and self.free(i, got[1]):
                    self.out["after"] = got[0]
                    self.take(i, got[1])
                    i = got[1]
                    continue
            # "the last five minutes", "past hour"
            if w in ("last", "past", "previous") or (
                    w == "the" and self.w(i + 1) in ("last", "past", "previous")):
                k = i + 2 if w == "the" else i + 1
                got = self.length(k)
                if got is None and self.w(k) in ("minute", "hour", "second"):
                    got = (_UNIT_SECONDS[self.w(k)], k + 1)
                if got and self.free(i, got[1]):
                    self.out["last"] = got[0]
                    self.take(i, got[1])
                    i = got[1]
                    continue
            # clocks
            cue = self.w(i - 1) in _CLOCK_CUES
            got = self.clock_at(i, cue, dayish)
            if got and self.free(i, got[2]):
                secs, twelve, j = got
                part, j2 = self.part_after(j)
                if part:
                    self.out["part"] = part
                    j = j2
                self.out["clock"] = secs
                if twelve:
                    self.out["clock_12h"] = True
                # the cue word belongs to the time ("at 7 pm")
                start = i - 1 if cue and self.w(i - 1) in ("at", "around", "about") \
                    and not self.used[i - 1] else i
                self.take(start, j)
                i = j
                continue
            # "for THE NEXT hour", "for the next ten minutes": how long
            if w == "for" and self.w(i + 1) == "the" and self.w(i + 2) == "next":
                got = self.length(i + 3)
                if got is None and self.w(i + 3) in ("minute", "hour", "second"):
                    got = (_UNIT_SECONDS[self.w(i + 3)], i + 4)
                if got and self.free(i, got[1]):
                    self.out["duration"] = got[0]
                    self.take(i, got[1])
                    i = got[1]
                    continue
            # lengths: "for an hour", "thirty seconds", "5 minutes ago"
            got = self.length(i)
            if got and self.free(i, got[1]):
                secs, j = got
                if self.w(j) == "ago":
                    self.out["ago"] = secs
                    j += 1
                # "twenty minutes from now", "later", "wait five minutes",
                # "give it an hour": WHEN, not how long.
                elif self.w(j) == "from" and self.w(j + 1) == "now":
                    self.out["after"] = secs
                    j += 2
                elif self.w(j) == "later":
                    self.out["after"] = secs
                    j += 1
                elif self.w(i - 1) == "wait" or (
                        self.w(i - 2), self.w(i - 1)) == ("give", "it"):
                    self.out["after"] = secs
                else:
                    self.out["duration"] = secs
                start = i - 1 if self.w(i - 1) == "for" and not self.used[i - 1] else i
                self.take(start, j)
                i = j
                continue
            i += 1

        # "in a bit", "in a little while", "shortly": later but unspecified;
        # the hub asks when.
        if not any(k in self.out for k in ("after", "clock", "day", "weekday",
                                           "date", "duration")):
            for i in range(n):
                if self.used[i]:
                    continue
                w = self.w(i)
                ends = None
                if w == "in" and self.w(i + 1) == "a":
                    if self.w(i + 2) in ("bit", "while", "moment", "sec", "second", "minute"):
                        ends = i + 3
                    elif self.w(i + 2) == "little" and self.w(i + 3) in ("bit", "while"):
                        ends = i + 4
                elif w in ("shortly", "soon") or (w == "later" and self.w(i + 1) in ("on", "")):
                    ends = i + (2 if self.w(i + 1) == "on" else 1)
                if ends is not None and self.free(i, ends):
                    self.out["vague"] = True
                    self.take(i, ends)
                    break

        # A 12-hour clock with the part of the day said anywhere ("at 7 in
        # the evening", "this morning at 9"): settled now.
        if self.out.get("clock_12h") and self.out.get("part"):
            secs = self.out["clock"]
            h, part = int(secs // 3600), self.out["part"]
            if part in ("afternoon", "evening") and h < 12:
                secs += 12 * 3600
            elif part == "night" and 6 <= h < 12:
                secs += 12 * 3600
            elif part in ("morning", "night") and h == 12:
                secs -= 12 * 3600
            self.out["clock"] = secs
            del self.out["clock_12h"]

        spans: List[Tuple[int, int]] = []
        for k, t in enumerate(self.toks):
            if not self.used[k]:
                continue
            if spans and k > 0 and self.used[k - 1]:
                spans[-1] = (spans[-1][0], t.e)
            else:
                spans.append((t.s, t.e))
        if spans:
            self.out["spans"] = spans
        return self.out


def read(text: str, expect_time: bool = False) -> When:
    """Every time, length of time and day said in `text`. Empty when there
    is none. See the module docstring for what counts."""
    if not text:
        return {}
    return _Reader(str(text), expect_time).run()


def without(text: str, when: When, fill: str = " ") -> str:
    """`text` with the time's words blanked out (same length, so any other
    span into it still holds)."""
    out = list(text)
    for s, e in when.get("spans", []):
        for k in range(s, e):
            out[k] = fill
    return "".join(out)


class Slots(TypedDict):
    """The exact dict extract() returns — and the exact dict
    executor.build_command() consumes.

    Written down because the two modules previously agreed only by convention.
    Nothing checked that the keys matched, that `switch` was one of two
    strings, or that `delta_category` used the same spelling on both sides.
    One of those had already drifted: this module emits the category
    'maximizers' from its own branches, while the modifier-override branch
    copies whatever spelling lexicon.py uses — which the guard here shows can
    be the singular 'maximizer'. executor.py has to accept both.
    """
    absolute_value: Optional[float]
    absolute_unit: Optional[str]
    delta: Optional[float]
    delta_direction: Optional[Literal["increase", "decrease"]]
    delta_category: Optional[str]     # open: lexicon supplies its own names
    delta_raw: Optional[str]
    #: The directional WORD alone ("up" in "turn it up by 5"). delta_raw is
    #: the whole matched text ("by 5"), which the resolver could not test
    #: against GENERIC_DIRECTIONALS.
    delta_head: Optional[str]
    delta_unit: Optional[str]
    switch: Optional[Literal["on", "off"]]
    #: RGB, or None. A colour is NOT a value on the same axis as brightness:
    #: there is no ordering, no range to clamp to, and no meaningful delta
    #: ("a bit more blue" from what?). So it gets its own slot rather than
    #: being squeezed into absolute_value, where every piece of the numeric
    #: arithmetic would then have to special-case it.
    colour: Optional[Tuple[int, int, int]]
    #: The time said in the command (read() above), {} when none. Read
    #: first and its words removed, so none of its numbers becomes a value.
    when: When


# ═══════════════════════════════════════════════════════════════
# REGEXES  (structure is mechanical; the unit words inside come from
# the lexicon's _UNIT_MAP domain, kept in sync there)
# ═══════════════════════════════════════════════════════════════

# Unit alternation generated from lexicon._UNIT_MAP, so every token the
# normaliser knows is a token these patterns can capture -- and the two
# regexes below cannot disagree about which units exist.
_UNITS = unit_pattern()

_VALUE_RE = re.compile(
    r'(?<![\w.])(-?\d+(?:\.\d+)?)\s*'
    r'(' + _UNITS + r')?'
    r'(?=\s|$|[,.;:!?])', re.I
)

# The unit must END at a word boundary, as in _VALUE_RE. Without the
# lookahead the single-letter units matched the first letter of the next
# word: "up by 2 for me" captured 'f' (fahrenheit), "by 5 cooler" 'c'.
_EXPLICIT_DELTA_RE = re.compile(
    r'\b(?:' + '|'.join(DELTA_INTRODUCERS) + r')\s+(-?\d+(?:\.\d+)?)'
    r'(?:\s*(' + _UNITS + r')(?=\s|$|[,.;:!?]))?',
    re.I
)

_TARGET_PREP_RE = re.compile(
    r'\b(?:' + '|'.join(TARGET_PREPOSITIONS) + r')\s+-?\d', re.I)

# Modifier matcher is BUILT FROM the lexicon's modifier vocabulary — no words
# are hardcoded here. Longest-first so "a great deal" beats "deal".
# `(?!)` is a pattern that can never match. Without it, an empty or failed
# _MODIFIER_LOOKUP compiles to r'\b()\b', which matches the empty string at
# every position — every text would appear to contain a modifier, and the
# lookup of that empty match would then raise KeyError.
_MODIFIER_RE = re.compile(
    (r'\b(' + '|'.join(re.escape(w) for w in
                       sorted(_MODIFIER_LOOKUP, key=len, reverse=True)) + r')\b')
    if _MODIFIER_LOOKUP else r'(?!)',
    re.I
)

_WORD_RE = re.compile(r'\b\w+\b')
_TRAILING_TOKEN_RE = re.compile(r'[\w%°]+$')


# A bare number is a VALUE only if we can tell it isn't part of a device name.
# The token immediately before it decides: a function word, pronoun, value
# preposition, modifier, or direction word means what follows is a quantity
# ("set it to 20", "make it 50", "about 30"). An UNRECOGNISED content word
# means the number is almost certainly a name suffix — "bulb 4", "zone 2",
# "channel 3" — and must not become an absolute_value.
#
# This is the fix for: "turn off bulb 4" -> absolute_value=4 -> the extractor
# returned early with switch=None -> executor's absolute branch fired ->
# set_state(level=4, on=True). The command to turn a device OFF turned it ON.
# STRONG evidence: the preceding token positively marks a quantity.
#   "set it TO 20", "make IT 50", "about 20", "up 5"
_NUMBER_PREV_STRONG: FrozenSet[str] = frozenset(
    set(PRONOUNS) | set(VALUE_PREPOSITIONS)
)
# WEAK evidence: a general function word. Consistent with a value ("the 20")
# but equally consistent with a COUNT ("turn off the 3 lights", "all 2 fans").
# These used to sit in the same set as the strong ones, so a bare number after
# any determiner became an absolute_value -- and "turn off the 3 lights"
# extracted absolute_value=3 with switch=None, which the executor then applied
# as level=3, on=True. The command to switch three lights OFF switched them ON
# at level 3: the same defect the "bulb 4" note below describes, surviving on
# the determiner path.
_NUMBER_PREV_WEAK: FrozenSet[str] = frozenset(
    set(COMMAND_STOPWORDS) | set(DETERMINERS)
)

Evidence = Literal["strong", "weak", "none"]
Switch = Literal["on", "off"]
Direction = Literal["increase", "decrease"]


def _preceding_token(text: str, start: int) -> Optional[str]:
    """The word immediately before position `start`, or None at start-of-text."""
    lead = text[:start].rstrip()
    if not lead:
        return None
    m = _TRAILING_TOKEN_RE.search(lead)
    return m.group(0).lower() if m else None


def _value_evidence(text: str, match: "re.Match[str]") -> Evidence:
    """How strongly does this numeric match look like a quantity?

    'none' -> part of a device name ("bulb 4"), never a value.
    'weak' -> could be a value or a count; loses to an explicit switch word.
    'strong' -> unambiguously a quantity.
    """
    if match.group(2):
        return "strong"                  # has a unit: "20 degrees", "50%"
    # A FRACTION IS NEVER A NAME SUFFIX. "make the door half open" arrives
    # here as "make the door 0.5 open": the preceding token is "door", an
    # unknown content word, and the guard below read 0.5 as "door 0.5",
    # a device called door-point-five. Nobody numbers a device 0.5; a
    # number strictly between 0 and 1 was a spoken fraction, and on a
    # device with a range it is a fraction OF that range (build_command
    # does the arithmetic). 2026-09-10, the gate that stops half-way.
    if 0.0 < float(match.group(1)) < 1.0:
        return "strong"
    prev = _preceding_token(text, match.start())
    if prev is None:
        return "strong"                  # bare "20" as the whole command
    if prev in _NUMBER_PREV_STRONG:
        return "strong"
    if prev in _MODIFIER_LOOKUP:
        return "strong"                  # "about 20", "roughly 30"
    if word_direction(prev) is not None:
        return "strong"                  # "up 5", "raise 10"
    if prev in _NUMBER_PREV_WEAK:
        return "weak"
    return "none"                        # unknown content word -> name suffix


def _find_value(text: str, exclude_span=None, *, strong_only: bool = False):
    """First numeric match that passes the name-suffix guard. Returns the
    match object or None. `exclude_span` skips a region already consumed by
    the explicit-delta pattern. `strong_only` additionally rejects weak
    evidence -- used when the command also carries an explicit switch word,
    where a bare "3" is far more likely a count than a brightness."""
    need = ("strong",) if strong_only else ("strong", "weak")
    for m in _VALUE_RE.finditer(text):
        if exclude_span is not None:
            s, e = exclude_span
            if not (m.end() <= s or m.start() >= e):
                continue
        if _value_evidence(text, m) in need:
            return m
    return None


def _word_start(text: str, word: str) -> int:
    """Index of `word` as a WHOLE WORD, or -1. text.find() was matching
    substrings: find("low") inside "slowly" returns 1, which made the modifier
    look like it FOLLOWED the head word and silently killed the magnitude
    override ("slowly make it a bit low" lost the downtoner)."""
    if not word:
        return -1
    m = re.search(r'\b' + re.escape(word) + r'\b', text)
    return m.start() if m else -1


def _modifier_before(text: str, head_pos: int):
    """The modifier match closest to (and before) the head word, or None.
    Previously this was a bare .search(), which finds only the FIRST modifier
    in the string — if that one happened to sit after the head, a genuine
    modifier before the head was ignored."""
    best = None
    for m in _MODIFIER_RE.finditer(text):
        if m.start() >= head_pos:
            break
        best = m
    return best


def _detect_switch(word_list: List[str]) -> Optional[Switch]:
    """Bare binary switch, or None if the text carries no switch intent.

    None means "determined NOT to be a switch", not "unknown" — e.g. the
    prepositional 'on' in "volume on the tv" is deliberately rejected here.
    Downstream code must not re-scan the raw text for the word 'on' and
    override this."""
    for w in word_list:
        if w in ON_SYNONYMS:
            return "on"
        if w in OFF_SYNONYMS:
            return "off"

    # Distinguish the control particle ("turn it on") from the preposition
    # ("volume on the tv"): a prepositional 'on' is followed by a determiner
    # and not preceded by a phrasal verb.
    #
    # Scanned in WORD ORDER, once. The old shape looped over ("off", "on") in
    # the outer position, so it checked every 'off' in the sentence before any
    # 'on' -- and "switch it on, not off" answered 'off' because of where the
    # word sat in that fixed tuple rather than where it sat in the sentence.
    for i, w in enumerate(word_list):
        if w == "off":
            return "off"
        if w == "on" and not is_prepositional_on(word_list, i):
            return "on"
    return None


# ═══════════════════════════════════════════════════════════════
# EXTRACTOR
# ═══════════════════════════════════════════════════════════════

class SlotExtractor:
    def extract(self, text: Any) -> Slots:
        text = str(text or "").lower()
        # THE TIME FIRST, from the words as said, and its words blanked: "turn
        # on the fan at seven thirty" read "seven thirty" as the number 37 and
        # set the fan to it. Digitizing first would make "two fifteen" 17.
        when = read(text)
        if when:
            text = without(text, when)
        # Spoken numbers become digits first, so every pattern below sees one
        # consistent shape. See _digitize.
        text = _digitize(text)
        result: Slots = {
            'absolute_value': None,
            'absolute_unit': None,
            'delta': None,
            'delta_direction': None,
            'delta_category': None,
            'delta_raw': None,
            'delta_head': None,
            'delta_unit': None,
            'switch': None,
            'colour': None,
            'when': when,
        }

        word_list: List[str] = _WORD_RE.findall(text)

        # Colour is read from the ORIGINAL word list, before any numeric
        # machinery runs. It never interacts with values or deltas -- a colour
        # has no magnitude -- so keeping it out of that path entirely is
        # simpler than teaching each stage to ignore it.
        result['colour'] = find_colour(word_list)

        direction: Optional[Direction] = None

        # Computed early, used only as a TIE-BREAKER below; the switch slot
        # itself is still assigned last, exactly as before. A command that
        # plainly says "off" tells us a bare number in it is far more likely a
        # count than a value.
        switch_present = _detect_switch(word_list) is not None

        # ── Power phrasal ("shut it down", "power up") ──
        # Must precede direction detection; see lexicon.phrasal_power_switch.
        phrasal = phrasal_power_switch(word_list)
        if phrasal:
            result['switch'] = phrasal
            return result

        # ── Explicit delta ("by 5", "plus 3", "minus 2") ──
        explicit = _EXPLICIT_DELTA_RE.search(text)
        if explicit:
            val = float(explicit.group(1))
            lead = text[:explicit.start()]
            # "set the freezer TO minus eighteen" is a signed TARGET, not a
            # decrease of eighteen: _digitize makes it "to minus 18" and the
            # delta pattern claimed it, so a freezer at -18 was sent to -36.
            lead_words = lead.split()
            minus = 'minus' in explicit.group(0)
            if (minus and lead_words
                    and lead_words[-1] in TARGET_PREPOSITIONS):
                result['absolute_value'] = -abs(val)
                result['absolute_unit'] = normalize_unit(explicit.group(2))
                return result
            # Direction from the LAST directional word before the delta, not
            # from any of them. `any(... == 'decrease')` let a single stray
            # word anywhere in the lead-in flip the whole command: "make the
            # cool room warmer by 5" saw 'cool' and decreased, ignoring the
            # 'warmer' that actually governs the delta. The nearest directional
            # word is the operative one.
            for w in _WORD_RE.findall(lead):
                d = word_direction(w)
                if d is not None:
                    direction = d
                    result['delta_head'] = w
            if minus:
                direction = 'decrease'
            if direction is None:
                direction = 'increase'

            if val < 0:
                val = abs(val)
                direction = 'decrease' if direction == 'increase' else 'increase'

            result['delta'] = val
            result['delta_raw'] = explicit.group(0)
            result['delta_category'] = 'explicit'
            result['delta_direction'] = direction
            result['delta_unit'] = normalize_unit(explicit.group(2))

            other = _find_value(text, exclude_span=explicit.span(),
                                strong_only=switch_present)
            if other is not None:
                result['absolute_value'] = float(other.group(1))
                result['absolute_unit'] = normalize_unit(other.group(2))
            return result

        # ── Absolute values (guarded against device-name digits) ──
        val_match = _find_value(text, strong_only=switch_present)
        if val_match is not None:
            result['absolute_value'] = float(val_match.group(1))
            result['absolute_unit'] = normalize_unit(val_match.group(2))

        # ── Find the direction head ──
        head_word: Optional[str] = None
        category: Optional[str] = None
        magnitude: Optional[float] = None

        # Periphrastic comparative/superlative: "more/less/most/least <adj>".
        for i, w in enumerate(word_list):
            if w in (PERIPHRASTIC_UP + PERIPHRASTIC_DOWN) and i + 1 < len(word_list):
                d = word_direction(word_list[i + 1])
                if d:
                    if w in PERIPHRASTIC_DOWN:
                        d = 'decrease' if d == 'increase' else 'increase'
                    head_word = w + ' ' + word_list[i + 1]
                    direction = d
                    if w in PERIPHRASTIC_EXTREME:
                        category, magnitude = 'maximizers', 1.0
                    else:
                        category, magnitude = 'comparative', 0.10
                    break

        # Single directional / comparative / superlative head word.
        # Note: bare on/off are switch words, not gradable directions — they are
        # handled by the binary-switch path below, so they're skipped here.
        if not direction:
            for w in word_list:
                if w in SWITCH_WORDS:
                    continue
                d = word_direction(w)
                if d:
                    head_word = w
                    direction = d
                    if is_superlative(w):
                        # A superlative is the extreme of its scale.
                        category, magnitude = 'maximizers', 1.0
                    else:
                        category, magnitude = 'comparative', 0.10
                    break

        # No head yet — a bare maximizer/midpoint modifier can imply direction.
        if not direction:
            mod = _MODIFIER_RE.search(text)
            mod_word = mod.group(1).lower() if mod else ""
            cat, mag = _MODIFIER_LOOKUP.get(mod_word, (None, None))
            if cat in ('maximizers', 'midpoints'):
                # Every DECREASE_MAXIMIZERS word is a maximizer, so a
                # midpoint always points up.
                direction = ('decrease' if mod_word in DECREASE_MAXIMIZERS
                             else 'increase')
                head_word, category, magnitude = mod_word, cat, mag

        # A modifier overrides magnitude ONLY if it PRECEDES the head word.
        # A modifier-word after the head is part of the object ("dim the top
        # light" -> "top" is the object, not a maximizer).
        if direction:
            head_pos = _word_start(text, head_word.split()[0])
            mod = _modifier_before(text, head_pos)
            entry = _MODIFIER_LOOKUP.get(mod.group(1).lower()) if mod else None
            if entry is not None:
                cat, mag = entry
                # 'maximizers', plural. lexicon builds these category names
                # from LEXICON's own keys and every one of them is plural; the
                # singular spelling this used to also accept never occurs.
                if cat != 'maximizers' or category != 'maximizers':
                    category = cat
                    magnitude = mag

            # "up ... to 72" is an explicit target — keep it absolute.
            # "5 degrees warmer" — the number is the delta magnitude.
            has_target_prep = bool(_TARGET_PREP_RE.search(text))
            if result['absolute_value'] is not None and has_target_prep:
                return result
            if result['absolute_value'] is not None:
                result['delta'] = result['absolute_value']
                result['delta_category'] = 'explicit'
                result['delta_unit'] = result['absolute_unit']
                result['absolute_value'] = None
                result['absolute_unit'] = None
            else:
                result['delta'] = magnitude
                result['delta_category'] = category
            result['delta_direction'] = direction
            result['delta_raw'] = head_word
            result['delta_head'] = head_word.split()[-1]
            return result

        # ── Pure absolute target (no direction word) ──
        # Switch stays None here on purpose.
        #
        # The ORIGINAL reason no longer holds: build_command() used to check
        # switch before absolute_value, so emitting both made "turn on the lamp
        # to 50" jump to range_max. That precedence has since been fixed --
        # absolute, then delta, then switch -- and executor._apply_state now
        # routes on a value whenever one is present, so a switch alongside it
        # would simply be ignored.
        #
        # It stays None for a different and still-live reason: `switch` is part
        # of what resolver._has_explicit_action() reads, and main.py uses that
        # to decide whether a BARE reference to a secure device needs a full
        # voice challenge. Widening what this slot carries changes which
        # commands are treated as explicit, which is a security-relevant edge
        # to move deliberately rather than as a side effect of tidying.
        if result['absolute_value'] is not None:
            return result

        # ── Bare binary switch — only when nothing numeric/directional found ──
        result['switch'] = _detect_switch(word_list)
        return result

# build_command and extract_and_build moved to executor.py (extraction only here).
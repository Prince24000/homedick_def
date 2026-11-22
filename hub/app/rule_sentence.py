"""rule_sentence.py -- one typed or said sentence -> a DRAFT automation for
the builder's boxes (2026-11-05).

"When the voltage meter falls below 200, show "Low power" on the kitchen
display" fills WHEN, DO and (if said) ONLY IF. Nothing is saved: the boxes
come back filled, the parts it could not tell are listed in `unsure`, and
the person fixes those by tapping.

What it reads, and with what:
  * the two halves: closed-class joining words (when / if / once ...), or,
    with no comma, the first ORDER VERB of the rest (command_splitter,
    by grammar -- not a list);
  * devices and scenes: by their own names (the hub's device naming);
  * which WHEN: the device's own trigger choices, scored against the words
    (choice_match), with the number or the minutes read by value_extractor;
  * which DO: the device's own controls (its `use`) -- a display takes the
    quoted words, an IR appliance its learned button, a switch on/off;
  * ONLY IF: "between <time> and <time>", and weekday names (from Python's
    calendar module, not written here);
  * a pause in DO: "wait 10 seconds" / "wait 5 minutes" / "wait 2 hours"
    becomes a Wait step where it was said, and "and repeat" / "repeat" /
    "keep repeating" / "loop" at the end switches the loop on.
No hand-written vocabulary beyond closed-class English.
"""
from __future__ import annotations

import calendar
import re
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from nlu import choice_match as CM

try:                       # the personal copy's reader of times and spans
    from nlu.value_extractor import read as _read
except ImportError:        # (the defense copy has none: clock times only)
    _read = None

_CLOCK = re.compile(r"\b(?P<h>\d{1,2})(?:[:.](?P<m>\d{2}))?\s*(?P<ap>[ap]\.?m\.?)?"
                    r"(?![\w%])", re.I)


def read(text: str, expect_time: bool = False) -> Dict[str, Any]:
    """value_extractor.read where there is one; otherwise a clock time
    written as 18:00, 7.30 or 6pm (a bare number is not a time)."""
    if _read is not None:
        return _read(text, expect_time=expect_time)
    for m in _CLOCK.finditer(str(text)):
        if m.group("m") is None and m.group("ap") is None and not expect_time:
            continue
        h, mi = int(m.group("h")), int(m.group("m") or 0)
        ap = (m.group("ap") or "").lower()[:1]
        if ap == "p" and h < 12:
            h += 12
        elif ap == "a" and h == 12:
            h = 0
        if h > 23 or mi > 59:
            continue
        return {"clock": float(h * 3600 + mi * 60),
                "spans": [(m.start(), m.end())]}
    return {}

#: Closed-class words that open a condition.
_SUB = r"(?:when(?:ever)?|if|once|as\s+soon\s+as)"
_LEAD = re.compile(rf"^\s*{_SUB}\s+(?P<cond>.+?)\s*,\s*(?:then\s+)?"
                   rf"(?P<act>.+?)\s*[.!]?\s*$", re.I | re.S)
_TAIL = re.compile(rf"^\s*(?P<act>.+?)\s*,?\s+{_SUB}\s+(?P<cond>.+?)"
                   rf"\s*[.!]?\s*$", re.I | re.S)
_OPEN = re.compile(rf"^\s*{_SUB}\s+", re.I)
_BETWEEN = re.compile(r",?\s*\b(?:only\s+)?between\s+(?P<a>.+?)\s+and\s+"
                      r"(?P<b>.+?)(?=\s*,|\s*[.!]?\s*$|\s+(?:on|then)\b)",
                      re.I)
_QUOTED = re.compile(r"[\"“”]([^\"“”]+)[\"“”]")
_SPLIT = re.compile(r"\s*,\s*(?:and\s+)?|\s+and\s+(?:then\s+)?", re.I)
_NUMBER = re.compile(r"(?<![\w:])-?\d+(?:\.\d+)?(?![\w:])")
_DAY_NAMES = [calendar.day_name[i] for i in range(7)]
_DAYS = re.compile(
    r",?\s*\b(?:only\s+)?on\s+(?P<days>(?:%s)s?(?:\s*(?:,|and|or)\s*(?:%s)s?)*)"
    % ("|".join(_DAY_NAMES), "|".join(_DAY_NAMES)), re.I)

#: A Wait step's words, and the loop said at the end of DO.
_WAIT = re.compile(r"^\s*(?:then\s+)?wait\b(?:\s+for\b)?(?P<rest>.*)$", re.I)
_WAIT_FOR = re.compile(r"^\s*(?P<n>\d+(?:\.\d+)?|an?|one)\s*"
                       r"(?P<u>s|secs?|seconds?|m|mins?|minutes?|h|hrs?|hours?)"
                       r"\s*[.!]?\s*$", re.I)
_REPEAT = re.compile(r",?\s*\b(?:and\s+)?(?:then\s+)?(?:keep\s+repeating|repeat"
                     r"|loop)(?:\s+(?:it|that|forever))?\s*[.!]?\s*$", re.I)

#: The score a trigger choice or a button must reach to be taken without
#: being flagged for the person to check.
SURE = 0.5


def _hhmm(text: str) -> Optional[str]:
    """A said time ("6pm", "18:00", "7.30") as HH:MM, or None."""
    w = read(str(text), expect_time=True)
    clock = w.get("clock") if isinstance(w, dict) else None
    if clock is None:
        return None
    secs = int(clock) % 86400
    return f"{secs // 3600:02d}:{(secs % 3600) // 60:02d}"


def _split(text: str) -> Tuple[str, str]:
    """(condition words, action words); ("", text) when there is no
    condition."""
    m = _LEAD.match(text)
    if m:
        return m.group("cond").strip(), m.group("act").strip()
    if _OPEN.match(text):
        # No comma: the action starts at the first order verb after the
        # condition's own words.
        try:
            from nlu.command_splitter import _clean_word, _verbs_of
        except ImportError:     # no grammar reader here: a comma is needed
            return _OPEN.sub("", text, count=1).strip(), ""
        words = _OPEN.sub("", text, count=1).split()
        for k in range(2, len(words)):
            rest = " ".join(words[k:])
            if _clean_word(words[k].lower()) in _verbs_of(rest):
                return " ".join(words[:k]).strip(" ,"), rest.strip()
        return " ".join(words), ""
    m = _TAIL.match(text)
    if m:
        return m.group("cond").strip(), m.group("act").strip()
    # A time of day said first or last ("at 7:30 turn on the fan").
    w = read(text)
    spans = w.get("spans") if isinstance(w, dict) else None
    if isinstance(w, dict) and w.get("clock") is not None and spans:
        a, b = spans[0][0], spans[-1][1]
        if a <= 3:
            # Only the time's own words: a "wait 10 minutes" further on
            # is a step of DO, not part of WHEN.
            first = text.find(",")
            b = max([e for s, e in spans if first < 0 or s < first] or [b])
            return text[:b].strip(" ,"), text[b:].strip(" ,")
        if b >= len(text.rstrip(" .!")):
            last = text.rfind(",")
            a = min([s for s, e in spans if s > last] or [a])
            return text[a:b].strip(" ,"), text[:a].strip(" ,")
    return "", text.strip()


def _best(said: str, labels: Sequence[str]) -> Tuple[int, float]:
    """(index, score) of the label the words fit best; (-1, 0) for none."""
    best, top = -1, 0.0
    for i, label in enumerate(labels):
        sc = CM.score(said, label)
        if sc > top:
            best, top = i, sc
    return best, top


def _number(text: str) -> Optional[float]:
    m = _NUMBER.search(text)
    return float(m.group()) if m else None


def _wait_secs(part: str) -> Optional[float]:
    """Seconds of a "wait N seconds|minutes|hours" step; 0.0 for a wait
    whose time could not be read; None when the words are not a wait."""
    m = _WAIT.match(part)
    if not m:
        return None
    rest = m.group("rest")
    f = _WAIT_FOR.match(rest)
    if f:
        n = f.group("n").lower()
        num = 1.0 if n in ("a", "an", "one") else float(n)
        return num * {"s": 1, "m": 60, "h": 3600}[f.group("u")[0].lower()]
    w = read(rest)              # the personal copy also reads "five minutes"
    secs = (w.get("after") or w.get("duration")) if isinstance(w, dict) else None
    return float(secs) if secs else 0.0


def _polarity(words: str) -> Optional[bool]:
    """On or off, from the closed-class particle said ("turn it off")."""
    toks = re.findall(r"[a-z]+", words.lower())
    if "off" in toks:
        return False
    if "on" in toks:
        return True
    return None


def draft(text: str, *,
          askable: Dict[str, Dict[str, Any]],
          targets: Sequence[Dict[str, Any]],
          scenes: Sequence[Dict[str, Any]],
          named: Callable[[str], List[str]]) -> Dict[str, Any]:
    """{"rule": the boxes it could fill, "unsure": which boxes to check}."""
    unsure: List[str] = []
    said = " ".join(str(text or "").split())
    rule: Dict[str, Any] = {"name": said[:48].rstrip(" ,.")}
    if not said:
        return {"rule": rule, "unsure": ["when", "do"]}

    # Quoted words are a display's message: kept whole, out of the parsing.
    quotes = _QUOTED.findall(said)
    work = _QUOTED.sub(lambda m: f"Q{quotes.index(m.group(1))}Q", said)

    # ── ONLY IF ──
    limits: List[Dict[str, Any]] = []
    m = _BETWEEN.search(work)
    if m:
        a, b = _hhmm(m.group("a")), _hhmm(m.group("b"))
        if a and b and a != b:
            limits.append({"kind": "time_range", "after": a, "before": b})
        else:
            unsure.append("only if")
        work = (work[:m.start()] + work[m.end():]).strip()
    m = _DAYS.search(work)
    if m:
        found = re.findall("|".join(_DAY_NAMES), m.group("days"), re.I)
        days = sorted({[d.lower() for d in _DAY_NAMES].index(f.lower())
                       for f in found})
        if days:
            limits.append({"kind": "days", "days": days})
        work = (work[:m.start()] + work[m.end():]).strip()
    if len(limits) == 1:
        rule["condition"] = limits[0]
    elif limits:
        rule["conditions"] = limits

    cond, act = _split(work)

    # ── WHEN ──
    if cond:
        heard = [d for d in named(cond) if d in askable]
        when = read(cond)
        if not heard and isinstance(when, dict) and when.get("clock") is not None:
            secs = int(when["clock"]) % 86400
            rule["trigger"] = {"kind": "time",
                               "at": f"{secs // 3600:02d}:{(secs % 3600) // 60:02d}"}
        elif heard:
            did = heard[0]
            options = list((askable.get(did) or {}).get("options") or [])
            i, sc = _best(cond, [str(o.get("label", "")) for o in options])
            if i < 0:
                rule["trigger"] = {"kind": "event", "device": did}
                unsure.append("when")
            else:
                o = options[i]
                # Above or below is said by a closed-class preposition;
                # that decides between a reading's two edges.
                toks = set(re.findall(r"[a-z]+", cond.lower()))
                edge = ("below" if toks & {"below", "under"} else
                        "above" if toks & {"above", "over"} else None)
                if o.get("kind") in ("threshold", "change") and edge:
                    same = next((x for x in options
                                 if x.get("kind") == "threshold"
                                 and x.get("key") == o.get("key")
                                 and x.get("edge") == edge), None)
                    if same is not None:
                        o, sc = same, max(sc, SURE)
                trig = dict(o.get("trigger") or {})
                if o.get("needs") == "number" or o.get("kind") == "threshold":
                    n = _number(cond)
                    if n is None:
                        unsure.append("when")
                    else:
                        trig[str(o.get("edge") or "above")] = n
                elif o.get("needs") == "minutes":
                    dur = when.get("duration") if isinstance(when, dict) else None
                    if dur:
                        trig["for_s"] = float(dur)
                    else:
                        unsure.append("when")
                rule["trigger"] = trig
                if sc < SURE:
                    unsure.append("when")
        else:
            unsure.append("when")
    else:
        unsure.append("when")

    # ── DO ──
    m = _REPEAT.search(act)
    if m and m.start() > 0:
        rule["loop"] = True
        act = act[:m.start()].strip(" ,")
    by_id = {str(t.get("value")): t for t in targets if isinstance(t, dict)}
    actions: List[Dict[str, Any]] = []
    for part in [p for p in _SPLIT.split(act) if p and p.strip()]:
        quote = next((quotes[int(q)] for q in re.findall(r"Q(\d+)Q", part)),
                     None)
        plain = re.sub(r"Q\d+Q", " ", part)
        secs = _wait_secs(plain)
        if secs is not None:
            # A pause, in the order said; an unread time is flagged (5 s).
            if not secs:
                unsure.append("do")
            actions.append({"kind": "wait", "s": max(0.5, secs or 5.0)})
            continue
        ids = [d for d in named(plain) if d in by_id]
        if not ids:
            labels = [str(sc.get("label", "")) for sc in scenes]
            i, sc = _best(plain, labels)
            if i >= 0 and sc >= SURE:
                actions.append({"kind": "scene",
                                "scene": str(scenes[i].get("value"))})
            else:
                unsure.append("do")
            continue
        did = ids[0]
        use = (by_id[did].get("use") or {})
        kind = str(use.get("kind") or "")
        if kind == "display":
            if quote is None:
                unsure.append("do")
                continue
            actions.append({"kind": "display", "device": did, "for_s": 0,
                            "pages": [[{"text": quote, "align": "left"}]]})
        elif kind == "ir":
            buttons = [str(b) for b in use.get("buttons") or []]
            i, sc = _best(plain, [b.replace("_", " ") for b in buttons])
            if i < 0 or sc < SURE:
                actions.append({"kind": "press", "device": did})
                unsure.append("do")
            else:
                actions.append({"kind": "press", "device": did,
                                "button": buttons[i]})
        elif kind == "press":
            actions.append({"kind": "press", "device": did})
        else:
            on = _polarity(plain)
            level = _number(plain)
            if kind in ("cover", "servo") and level is not None:
                actions.append({"kind": "device", "device": did,
                                "set": {"level": level}})
                continue
            if kind == "cover" and on is None:
                i, sc = _best(plain, ["open", "close"])
                on = None if sc < SURE else i == 0
            if on is None:
                unsure.append("do")
                on = True
            change: Dict[str, Any] = {"on": on}
            if on and level is not None and kind in ("dimmer", "colour"):
                change["level"] = level
            actions.append({"kind": "device", "device": did, "set": change})
    if actions and all(a.get("kind") == "wait" for a in actions):
        unsure.append("do")                 # only a pause: nothing to do
    if actions:
        rule["actions"] = actions
    elif "do" not in unsure:
        unsure.append("do")
    return {"rule": rule, "unsure": list(dict.fromkeys(unsure))}

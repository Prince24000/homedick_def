"""automations.py — the hub's own reflexes.

An automation is a TRIGGER stapled onto a SCENE (or one device action, or
a notification): "when the kitchen PIR goes quiet for ten minutes, run
'kitchen off'". The decision recorded 2026-08-26, superseding the
2026-08-16 note that said Home Assistant's engine would carry triggers:

    HA only ever sees a SUBSET of this house, later. The card reader,
    the IR receiver, the display, the IMU never appear there BY DESIGN
    (they publish moments, not states), and even stateful devices arrive
    late or not at all when HA is down or discovery lags. The hub's MQTT
    is where every event is BORN — so the engine that reacts to events
    lives here, sees everything, and works with HA switched off.

Three laws, enforced here and nowhere softer:

  * HUMAN OUTRANKS MACHINE. A device a person explicitly commanded is
    immune to automations for AUTOMATION_OVERRIDE_SECONDS (the manual-
    override window decided 2026-08-16): rule says motion→lights on, you
    say "turn it off", you move ten seconds later — it stays off.
  * NO AUTOMATION MAKES THE HOUSE LESS SECURE ON ITS OWN. A motion
    sensor must never be able to unlock a door. Refined 2026-08-27 from
    the flatter "secure devices are never actions", which forbade two
    things it should not have: making a device SAFER (locking up at
    10pm) is allowed outright, and a rule may ASK -- raising a PIN-gated
    approval on the phone and driving nothing until a person answers.
    What stays forbidden is the unattended unsafe change. Screened when
    an automation is created AND again at every firing, because a device
    can be marked secure after the rule was written.

    Note what is deliberately NOT accepted: a PIN entered while WRITING
    the rule. That proves who authored it, not who tripped the sensor
    three weeks later -- and a cloned card or a curtain drifting past a
    PIR would then open the door with nobody there.
  * EVERY FIRING IS ON THE RECORD. A ring journal holds what fired, what
    it did, and what it refused to do — a house that acts by itself and
    cannot say why is not a thesis, it is a poltergeist.

The engine is deliberately testable without a broker or a thread:
`feed(topic, payload)` is the same entry point the MQTT callback uses,
`tick()` is the same the timer loop uses, and `now` is injected. The
paho thread in start() is just those two functions on a loop.
"""

from __future__ import annotations

import atexit
import datetime
import json
import os
import math
import re
import shutil
import sys
import threading
import time
import traceback
import unicodedata
from collections import deque
from typing import Any, Callable, Dict, List, Optional, Tuple

from core.hv_common import event

from nlu.executor import NO_READING, describe_reading
from core import config as _config
from core.locked_store import (atomic_write_json, flock,
                               path_fingerprint)
from nlu.lexicon import (SECUREMENT_VERB_FORMS as _SEC_FORMS,
                         UNSECURING_VERBS as _UNSEC_VERBS)


# ── the trigger vocabulary ──────────────────────────────────────────────
#
# kinds and their fields, all validated in validate_rule():
#   event     {device, value}            the moment it happens
#   persist   {device, value, for_s}     it has stayed that way for N
#   absent    {device, value, for_s}     none of that for N (the quiet room)
#   threshold {device, key, above|below} a reading crosses a line
#   change    {device, key, by?}         a reading MOVES -- see below
#   time      {at: "HH:MM"}              a time of day, daily
#
# condition (optional):
#   time_range {after: "HH:MM", before: "HH:MM"}   wraps midnight correctly
#   device     {device, key, equals}
#
# action:
#   scene   {scene}
#   device  {device, set: {...}}
#   notify  {text}

TRIGGER_KINDS = {"event", "persist", "absent", "threshold", "change", "time"}
#: "leave this field as it is" for update(); None there means "clear it".
_UNSET = object()
#: Who made a rule and who last changed it (2026-10-11): bookkeeping, never
#: judged -- pausing someone's rule stays a pause.
OWNER_FIELDS = ("created_by", "changed_by", "changed_at")
#: "display" (2026-08-31, her ruling): a panel is a DEVICE, and what it
#: shows is a rule like everything else. The rotation that was written into
#: tick() was the one piece of behaviour in this system that did not come
#: from an automation, which made the panel the odd one out. It stays -- as
#: the DEFAULT, what the glass says when no rule is holding it.
ACTION_KINDS = {"scene", "device", "notify", "ask", "display", "press",
                "wait"}
#: "wait" {s}: a pause between steps, as in Arduino's delay(). DO runs its
#: steps top to bottom; at a wait the run sleeps `s` seconds (on a timer,
#: never blocking the engine), then goes on with the next step. A rule with
#: `loop: true` starts DO again from the top after its last step, until the
#: rule is switched off, removed or edited (or the hub stops) -- Arduino's
#: loop(). A loop must hold at least one wait, so it cannot run flat out.
WAIT_MIN_S = 0.5
WAIT_MAX_S = 24 * 3600.0
#: "press": one button -- a learned IR button of an appliance (`button`),
#: or a Home Assistant button/scene/script (no `button`).
#: "only if" kinds: time_range, device (equals), reading (above/below a
#: number), days (0 = Monday .. 6 = Sunday). `conditions` holds several,
#: every one of which must hold.
DAY_NAMES = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday",
             "Saturday", "Sunday")

#: How long a rule's message holds the glass before the rotation resumes.
#: Long enough to walk over and read it; short enough that a panel is not
#: left showing yesterday's alarm. A message overwritten by the clock two
#: seconds later is not a message, it is a flicker -- which is the whole
#: reason this number exists.
#:
#: THIRTY SECONDS WAS TOO SHORT (2026-09-14: "i set power to display in lcd
#: it did show but then disappeared ... i want anything for display to show
#: for more time"). Nothing told her the borrow was half a minute, so a
#: reading she had deliberately put on the glass read as a fault when the
#: clock took the panel back. TEN MINUTES now -- her own number, the same
#: day -- and read from config so it is one line to change rather than an
#: edit here.
DISPLAY_HOLD_SECONDS = _config.DISPLAY_HOLD_SECONDS

#: THE CLOCK'S TURN, and the reason the line comes back afterwards.
#:
#: The hold above used to END THE STORY: mode 1, the clock, and the glass
#: stayed a clock until some rule happened to fire again. For a rule that
#: fires often that is invisible; for a threshold crossed once a day it
#: means the panel showed the reading once, ten minutes, and was a clock for
#: the rest of its life. Nobody putting a reading on a panel means that.
#:
#: So the panel CYCLES (her design, 2026-09-14: "the display of anything set
#: in automations fr like 10 mins ... then date and time for like 5s, then
#: the display"): the automation line is the resting state and the clock
#: gets a glance. 0 turns the cycle off and restores exactly the old
#: behaviour -- the clock keeps the glass until something else is shown.
#:
#: THIS IS NOT THE ROTATION THAT WAS REMOVED. That one walked READINGS
#: across the panel every five seconds, deciding for itself which number
#: came next, and it was the hub re-implementing a list the phone draws
#: better ("remove the rotation, don't need that", 2026-09-01). This shows
#: exactly what a RULE asked for and nothing else; it only stops the clock
#: keeping the glass for ever afterwards.
DISPLAY_CLOCK_SECONDS = _config.DISPLAY_CLOCK_SECONDS
#: How often a held line that quotes a reading is re-rendered (seconds).
LIVE_REFRESH_S = 10.0
#: How long each page of a several-page message stays, when the message
#: does not say (its `page_s`).
DISPLAY_PAGE_SECONDS = 10.0
#: Where a line sits on a character display.
DISPLAY_ALIGNS = ("left", "middle", "right")


def display_pages(action: Dict[str, Any]) -> List[List[Dict[str, str]]]:
    """A display action's pages: [[{text, align}, ...], ...], one entry per
    line from the top. The older shape (one `text`) is one page of one line."""
    pages = action.get("pages")
    if isinstance(pages, list) and pages:
        out: List[List[Dict[str, str]]] = []
        for page in pages:
            lines: List[Dict[str, str]] = []
            for line in (page if isinstance(page, list) else []):
                if isinstance(line, dict):
                    align = str(line.get("align") or "left")
                    lines.append({"text": str(line.get("text") or ""),
                                  "align": align if align in DISPLAY_ALIGNS
                                  else "left"})
                else:
                    lines.append({"text": str(line), "align": "left"})
            out.append(lines)
        return out
    return [[{"text": str(action.get("text", "")), "align": "left"}]]


def display_words(action: Dict[str, Any], first_page: bool = False) -> str:
    """Every line's words, joined (for the journal, the text checks, and a
    small display that takes one message)."""
    pages = display_pages(action)
    if first_page:
        pages = pages[:1]
    return " ".join(line["text"].strip() for page in pages for line in page
                    if line["text"].strip())


def _pages_problem(act: Dict[str, Any]) -> Optional[str]:
    """The pages of a display action, checked."""
    pages = act.get("pages")
    if not isinstance(pages, list) or not pages:
        return "the display pages must be a list with at least one page"
    if len(pages) > 20:
        return "a display message can have at most 20 pages"
    for page in pages:
        if not isinstance(page, list):
            return "each display page must be a list of lines"
        for line in page:
            if not isinstance(line, dict) or not isinstance(
                    line.get("text", ""), str):
                return "each display line must have text"
            if str(line.get("align") or "left") not in DISPLAY_ALIGNS:
                return "a display line sits left, middle or right"
    if not display_words(act):
        return "the display action has nothing to show"
    if act.get("page_s") is not None:
        if not _finite(act["page_s"]) or float(act["page_s"]) <= 0:
            return "each page must stay for some seconds"
    if act.get("clock_between") is not None and \
            not isinstance(act["clock_between"], bool):
        return "clock_between is yes or no"
    return None

#: How often a panel's board is asked what is really on its I2C bus. See
#: _note_i2c_scan; 0 turns the scan off. Config carries the reasoning.
DISPLAY_SCAN_EVERY_SECONDS = _config.DISPLAY_SCAN_EVERY_SECONDS

#: HOW OFTEN THE WHOLE PANEL IS RE-ASSERTED, from scratch, whatever the
#: hub believes is already on it (her ruling, 2026-09-14: "allow display
#: one to always be on loop ... if something was meant to show on LCD, let
#: it be on loop too. Just anything, let's just be on loop").
#:
#: EVERY CLEVER THING THIS FILE DOES ABOUT PANELS IS BOOKKEEPING: a wake
#: that runs once, a stage counter, a hold, a standing set, a soft variant
#: that skips a command. Every one of them is the hub REMEMBERING what it
#: thinks the glass is showing -- and a character LCD forgets without
#: telling anybody. It browns out, it misses its init, the board reboots,
#: the driver comes up after the ESP32 does. The memory and the glass drift
#: apart, and every drift so far has ended with her typing DisplayModel 1
#: into a console at midnight.
#:
#: So the panel is simply re-told, on a timer: the driver settings, then
#: whatever should be on it right now. No state is consulted and none is
#: needed -- if the glass already agrees, the commands are a no-op and cost
#: three MQTT publishes on a local broker; if it has drifted, it is fixed
#: within this many seconds without anybody noticing it drifted. That is a
#: worse design on paper and the only one that survives contact with the
#: hardware.
#:
#: 0 turns the loop off and leaves the one-shot wake alone.
DISPLAY_REPEAT_SECONDS = _config.DISPLAY_REPEAT_SECONDS

#: `for_s: 0` MEANS "UNTIL SOMETHING ELSE TAKES THE GLASS".
#:
#: It used to be refused -- "a display message must hold for more than 0
#: seconds" -- which meant a permanently displayed reading could not be
#: expressed at all, and the only way to approximate one was an absurd
#: number of seconds. Zero is the natural spelling for it and was sitting
#: there unused, so it now carries that meaning: the line stays until
#: another rule writes to the panel, or the board reboots, whichever comes
#: first. Negative is still refused -- it spells nothing.
HOLD_FOREVER = 0
#: A revert whose board did not answer: how soon to try again, and how many
#: tries in all before it is said out loud.
REVERT_RETRY_SECONDS = 30.0
REVERT_MAX_TRIES = 5
#: Open approval questions kept at once; the oldest fall off.
APPROVALS_MAX = 20

#: HOW OFTEN A `change`-TRIGGERED RULE MAY FIRE, when everything it does is
#: SHOW something (2026-09-14). The ordinary cooldown is a minute, which is
#: right for a rule that switches a relay and useless for one that keeps a
#: reading on a panel: a meter that updates once a minute is not a live
#: reading, it is a stale one that moves.
#:
#: WHY NOT SIMPLY REMOVE THE COOLDOWN FOR `change`. Because the cooldown is
#: the only thing between a noisy sensor and a rule firing at the sensor's
#: own rate, and what that rule DOES decides whether that matters. Writing a
#: line to a display fifty times a minute costs a few MQTT messages. Driving
#: a RELAY fifty times a minute destroys the relay. So the short cooldown is
#: earned by the actions, not granted by the trigger: a rule that only shows
#: and notifies gets it, and a rule that touches a device keeps the full
#: minute. See _cooldown_for.
CHANGE_COOLDOWN_SECONDS = _config.CHANGE_COOLDOWN_SECONDS

#: Action kinds that only PUT SOMETHING IN FRONT OF A PERSON. A rule whose
#: every action is one of these moves nothing in the house, so it may run
#: often. `ask` is deliberately absent: it interrupts.
_SHOWING_ACTIONS = frozenset({"display", "notify"})

#: WHICH WAY IS SAFER, per domain, for a device marked secure.
#:
#: This cannot be inferred from the value, and getting it backwards would
#: be the worst bug in the project, so it is written down: Home Assistant
#: maps a lock's on to the LOCK service (safer), and a cover's on to
#: OPEN_COVER (less safe -- a gate is safer shut). A secure device with no
#: entity, or in a domain not listed here, has NO known direction, and an
#: unknown direction is treated as the dangerous one.
#: `set: {"on": TOGGLE}` -- the one action that names no state, only a
#: change. Everything else in a rule says what a device should BE; this
#: says "the opposite of whatever it is", which is how a single sensor can
#: work a light the way a wall switch does.
#:
#: A STRING, not a bool, precisely because it must never be mistaken for
#: one. `bool("toggle")` is True, so a sentinel that looked like a value
#: would read as "turn it ON" to anything that did not know about it --
#: including secure_direction, which would then call a toggle on a lock
#: "safe". It is resolved to a real True/False in _drive BEFORE any other
#: question is asked about it.
TOGGLE = "toggle"

SECURING_ON = {"lock": True, "cover": False, "valve": False}

#: THE SAME QUESTION FOR A DEVICE THAT IS NOT IN HOME ASSISTANT.
#:
#: The table above is keyed on an HA domain, so it can only answer for a
#: device that HAS an `ha_entity_id`. A lock wired to one of this hub's own
#: boards has none -- it is MQTT, registered by hand, and `secure` is a yes/no
#: the owner typed. Every such device therefore came back "unknown", and
#: unknown is treated as the dangerous direction everywhere it matters. The
#: consequence was not theoretical: `action_screening` refuses any automation
#: that is not provably fail-safe, so "lock the front door at 10pm" -- the
#: exact rule her 2026-08-27 refinement was written to ALLOW -- was refused
#: for every local lock in the house. It only ever worked for HA locks.
#:
#: A local device does say which way its `on` points; it says it in its own
#: vocabulary. `state_words` holds the word the person chose for the ON state
#: ("locked", "open"), which is the whole of the answer.
#:
#: DERIVED from the lexicon's securement verbs, so the two cannot drift: a
#: form whose base verb is a securing one means `on` is the safe direction,
#: and an undoing one means it is not. The cover pair is added by hand
#: because "open"/"closed" are not securement verbs -- and False for "open"
#: is the same judgement SECURING_ON already makes for the cover domain.
SECURING_ON_WORD: Dict[str, bool] = {
    form: base not in _UNSEC_VERBS for form, base in _SEC_FORMS.items()}
SECURING_ON_WORD.update({"open": False, "opened": False,
                         "closed": True, "shut": True})


def securing_on_word(device: Dict[str, Any]) -> Optional[bool]:
    """Does `on` mean SAFER for this device, read off its own vocabulary?

    True, False, or None when the device never said.
    """
    words = device.get("state_words")
    on_word = str((words or {}).get("on") or "").strip().lower()
    return SECURING_ON_WORD.get(on_word) if on_word else None


def _finite(value: Any) -> bool:
    """A real, finite number. NaN and infinity pass `float()` and then
    compare False against everything, so they sailed through every "above
    zero" check and were stored (2026-10-01 audit)."""
    try:
        return math.isfinite(float(value))
    except (TypeError, ValueError, OverflowError):
        return False


def _text(value: Any) -> bool:
    """A non-empty STRING. `str(None)` is "None", which is non-empty: a null
    name, device or notification text used to validate."""
    return isinstance(value, str) and bool(value.strip())


def _hhmm_ok(s: Any) -> bool:
    return bool(isinstance(s, str) and re.fullmatch(r"\d{1,2}:\d{2}", s)
                and int(s.split(":")[0]) < 24 and int(s.split(":")[1]) < 60)


def _minutes(s: str) -> int:
    h, m = s.split(":")
    return int(h) * 60 + int(m)


def _hhmm(s: str) -> str:
    """Zero-padded. `_hhmm_ok` accepts "9:30" and tick() used to compare it
    against "09:30" as a string, so such a rule validated and never fired."""
    h, m = s.split(":")
    return f"{int(h):02d}:{int(m):02d}"


#: "If I command it myself" -- how long a person's own command outranks one
#: rule. Seconds, minutes, hours or days (2026-10-01: "not only seconds").
OVERRIDE_MODES = ("default", "always", "window")
OVERRIDE_UNITS = {
    "s": 1, "sec": 1, "secs": 1, "second": 1, "seconds": 1,
    "m": 60, "min": 60, "mins": 60, "minute": 60, "minutes": 60,
    "h": 3600, "hr": 3600, "hrs": 3600, "hour": 3600, "hours": 3600,
    "d": 86400, "day": 86400, "days": 86400,
}
_UNIT_NAMES = {1: "seconds", 60: "minutes", 3600: "hours", 86400: "days"}
OVERRIDE_MAX_SECONDS = 30 * 86400.0


def override_spec(rule: Any) -> Tuple[str, float]:
    """(mode, seconds) for a rule's `override` field.

    "default" -- the house window (AUTOMATION_OVERRIDE_SECONDS), counted
    from the first command, time rules exempt: exactly what every rule did
    before the field existed. "always" -- the automation always wins; a
    person's command never holds it back. "window" -- the person wins for
    `seconds` after their LAST command to that device. Anything unreadable
    reads as default: a bad field must never silence or arm a rule."""
    ov = rule.get("override") if isinstance(rule, dict) else None
    if not isinstance(ov, dict):
        return "default", 0.0
    mode = ov.get("mode")
    if mode == "always":
        return "always", 0.0
    if mode == "window":
        unit = OVERRIDE_UNITS.get(str(ov.get("unit", "seconds")).strip().lower())
        if unit is not None and _finite(ov.get("value")) \
                and float(ov["value"]) > 0:
            return "window", min(float(ov["value"]) * unit,
                                 OVERRIDE_MAX_SECONDS)
    return "default", 0.0


def _override_problem(ov: Any) -> Optional[str]:
    if ov is None:
        return None
    if not isinstance(ov, dict):
        return "override must be a mapping of mode, value and unit"
    mode = ov.get("mode", "default")
    if mode not in OVERRIDE_MODES:
        return "override mode must be default, always or window"
    if mode != "window":
        return None
    unit = OVERRIDE_UNITS.get(str(ov.get("unit", "seconds")).strip().lower())
    if unit is None:
        return "override unit must be seconds, minutes, hours or days"
    if not _finite(ov.get("value")) or float(ov["value"]) <= 0:
        return "override needs a time above zero"
    if float(ov["value"]) * unit > OVERRIDE_MAX_SECONDS:
        return "override can be at most 30 days"
    return None


def _override_words(seconds: float) -> str:
    for size, word in ((86400, "day"), (3600, "hour"), (60, "minute")):
        if seconds >= size and seconds % size == 0:
            n = int(seconds // size)
            return f"{n} {word}{'s' if n != 1 else ''}"
    n = int(seconds) if seconds == int(seconds) else round(seconds, 1)
    return f"{n} second{'s' if n != 1 else ''}"


def normalise_rule(rule: Dict[str, Any]) -> None:
    """In place: every HH:MM field zero-padded. Called after validate_rule
    passes, on the copy add()/update() are about to store."""
    trig = rule.get("trigger")
    if isinstance(trig, dict) and _hhmm_ok(trig.get("at")):
        trig["at"] = _hhmm(trig["at"])
    # "ON" meant "off" to the engine (anything but "on" read as wanted-off)
    # and a persist rule on "ON" never fired: the words are lower-cased.
    if isinstance(trig, dict) and trig.get("kind") in (
            "event", "persist", "absent") \
            and isinstance(trig.get("value"), str) \
            and trig["value"].strip().lower() in ("on", "off"):
        trig["value"] = trig["value"].strip().lower()
    for cond in [rule.get("condition")] + list(rule.get("conditions") or []):
        if not isinstance(cond, dict):
            continue
        for k in ("after", "before"):
            if _hhmm_ok(cond.get(k)):
                cond[k] = _hhmm(cond[k])
    if rule.get("loop") is not True:
        rule.pop("loop", None)              # stored only when on
    ov = rule.get("override")
    if ov is None:
        rule.pop("override", None)
    if isinstance(ov, dict):
        mode, secs = override_spec(rule)
        if mode == "default":
            rule.pop("override", None)          # the house rule: no field
        elif mode == "always":
            rule["override"] = {"mode": "always"}
        else:
            for size in (86400, 3600, 60, 1):
                if secs >= size and secs % size == 0:
                    break
            else:
                size = 1
            val = secs / size
            rule["override"] = {"mode": "window",
                                "value": int(val) if val == int(val) else val,
                                "unit": _UNIT_NAMES[size]}


def _condition_problem(cond: Any) -> Optional[str]:
    """One "only if", checked; None when it stands."""
    if not isinstance(cond, dict):
        return "the condition must be a mapping"
    ck = cond.get("kind")
    if ck == "time_range":
        if not (_hhmm_ok(cond.get("after"))
                and _hhmm_ok(cond.get("before"))):
            return "a time_range condition needs after/before as HH:MM"
        if _minutes(cond["after"]) == _minutes(cond["before"]):
            # It validated and meant ONE MINUTE, not "all day".
            return ("a time window cannot start and end at the same "
                    "minute -- leave the window out for all day")
    elif ck == "device":
        if not _text(cond.get("device")):
            return "the device condition names no device"
        # AND IT HAS TO SAY WHAT IT IS CHECKING FOR (2026-09-14). The
        # documented shape is {device, key, equals} and only `device`
        # was required, so a rule authored by hand or by API could
        # carry no `equals` at all -- and _condition_holds then
        # compared `state.get(key)` with `cond.get("equals")`, both
        # None on a device that has not reported yet, and answered
        # TRUE. A condition that checked nothing read as a condition
        # that held:
        #
        #   "if the garage sensor is closed do nothing, else notify"
        #   -- sensor has never published, equals missing
        #   -> None == None -> the THEN branch runs and the intrusion
        #      notification that should have fired is suppressed.
        if "equals" not in cond:
            return ("the device condition does not say what to compare "
                    "against -- it needs `equals`")
    elif ck == "reading":
        if not _text(cond.get("device")):
            return "the reading condition names no device"
        if not _text(cond.get("key")):
            return "the reading condition names no reading"
        if (cond.get("above") is None) == (cond.get("below") is None):
            return "a reading condition says above or below a number"
        if not _finite(cond.get("above", cond.get("below"))):
            return "the reading condition's number must be a number"
    elif ck == "days":
        days = cond.get("days")
        if not isinstance(days, list) or not days:
            return "a days condition names at least one day"
        if any(isinstance(d, bool) or not isinstance(d, int)
               or not 0 <= d <= 6 for d in days):
            return "days are counted 0 (Monday) to 6 (Sunday)"
    else:
        return "the condition kind is unknown"
    return None


def validate_rule(rule: Dict[str, Any]) -> Optional[str]:
    """One sentence naming what is wrong, or None when the rule stands."""
    if not isinstance(rule, dict):
        return "an automation must be a mapping"
    if not _text(rule.get("name")):
        return "an automation needs a name"
    trig = rule.get("trigger")
    if not isinstance(trig, dict) or trig.get("kind") not in TRIGGER_KINDS:
        return "the trigger is missing or of an unknown kind"
    kind = trig["kind"]
    if kind == "time":
        if not _hhmm_ok(trig.get("at")):
            return "a time trigger needs at: HH:MM"
    else:
        if not _text(trig.get("device")):
            return "the trigger names no device"
        if kind in ("persist", "absent"):
            if not _finite(trig.get("for_s", 0)):
                return "for_s must be a number of seconds"
            if float(trig.get("for_s", 0)) <= 0:
                return "persist/absent need for_s above zero"
        if kind == "threshold":
            has_above = trig.get("above") is not None
            has_below = trig.get("below") is not None
            if has_above == has_below:
                return "a threshold needs exactly one of above/below"
            if not _finite(trig["above"] if has_above else trig["below"]):
                return "a threshold line must be a number"
            if not _text(trig.get("key")):
                return "a threshold names no reading key"
        if kind == "change":
            # WHENEVER THIS READING MOVES (2026-09-14). A threshold fires on
            # CROSSING a line, which is the wrong shape for a panel that
            # should track a meter: the line is written once, at the
            # crossing, and then stands still while the real value moves.
            #
            # `by` is optional and is the smallest movement that counts. Left
            # out, the engine's own per-reading deadband decides -- the same
            # one that stops a repeated number waking the phones -- so the
            # ordinary rule needs no number at all.
            if not _text(trig.get("key")):
                return "a change trigger names no reading key"
            if trig.get("by") is not None:
                if not _finite(trig["by"]):
                    return "a change trigger's `by` must be a number"
                if float(trig["by"]) < 0:
                    return "a change trigger's `by` cannot be negative"
    cond = rule.get("condition")
    if cond is not None:
        problem = _condition_problem(cond)
        if problem:
            return problem
    conds = rule.get("conditions")
    if conds is not None:
        if not isinstance(conds, list):
            return "conditions must be a list"
        for one in conds:
            problem = _condition_problem(one)
            if problem:
                return problem
    problem = _override_problem(rule.get("override"))
    if problem:
        return problem
    # THEN: one action (the original shape) or a list of them (the block
    # builder's, where a rule may run a scene AND send a notification).
    then = actions_of(rule)
    if not then:
        return "the action is missing or of an unknown kind"
    problem = _first_problem(then)
    if problem:
        return problem
    if all(a.get("kind") == "wait" for a in then):
        return "DO needs something to do besides waiting"
    loop = rule.get("loop")
    if loop is not None and not isinstance(loop, bool):
        return "loop is on or off (true or false)"
    if loop and not any(a.get("kind") == "wait" for a in then):
        # Without a pause the loop would run flat out.
        return "a loop needs at least one Wait"

    # ELSE, at the trigger moment, when the condition did not hold.
    else_acts = rule.get("else_actions")
    if else_acts is not None:
        if not isinstance(else_acts, list):
            return "else_actions must be a list of actions"
        if else_acts and rule.get("condition") is None \
                and not rule.get("conditions"):
            return ("an ELSE needs an IF to be else OF -- add a condition, "
                    "or use the quiet-revert instead")
        problem = _first_problem(else_acts)
        if problem:
            return problem

    # ELSE AFTER quiet N: the revert, armed by a firing and cancelled by
    # the next one.
    rev = rule.get("revert")
    if rev is not None:
        if not isinstance(rev, dict):
            return "revert must be a mapping of after_s and actions"
        if not _finite(rev.get("after_s", 0)):
            return "revert after_s must be a number of seconds"
        if float(rev.get("after_s", 0)) <= 0:
            return "a revert needs after_s above zero"
        rev_acts = rev.get("actions")
        if not isinstance(rev_acts, list) or not rev_acts:
            return "a revert that does nothing is not a revert"
        if any(isinstance(a, dict) and a.get("kind") == "wait"
               for a in rev_acts):
            return "a Wait is not used in the quiet-revert"
        return _first_problem(rev_acts)
    return None


def _first_problem(acts: List[Any]) -> Optional[str]:
    """validate_action over a branch: the first problem, or None."""
    return next((p for p in map(validate_action, acts) if p), None)


def validate_action(act: Any) -> Optional[str]:
    """One action, checked. Split out because a rule now has up to three
    sets of them (THEN, ELSE, and the revert) and they are all held to
    exactly the same standard."""
    if not isinstance(act, dict) or act.get("kind") not in ACTION_KINDS:
        return "the action is missing or of an unknown kind"
    ak = act["kind"]
    if ak == "wait":
        s = act.get("s")
        if isinstance(s, bool) or not _finite(s):
            return "a Wait needs a number of seconds"
        if float(s) < WAIT_MIN_S:
            return "a Wait is at least half a second"
        if float(s) > WAIT_MAX_S:
            return "a Wait is at most 24 hours"
    if ak == "scene" and not _text(act.get("scene")):
        return "the scene action names no scene"
    if ak == "device":
        if not _text(act.get("device")):
            return "the device action names no device"
        if not isinstance(act.get("set"), dict) or not act["set"]:
            return "the device action changes nothing"
        for key, value in act["set"].items():
            if value == TOGGLE and key != "on":
                return (f"only `on` can be toggled -- {key!r} names a value, "
                        f"and there is no opposite of a number")
    if ak == "press":
        if not _text(act.get("device")):
            return "the press names no device"
        if act.get("button") is not None and not _text(act.get("button")):
            return "the press names an empty button"
    if ak == "notify" and not _text(act.get("text")):
        return "the notification has no words"
    if ak == "ask":
        if not _text(act.get("device")):
            return "an approval request names no device"
        if not isinstance(act.get("set"), dict) or not act["set"]:
            return "an approval request asks for no change"
    if ak == "display":
        if not _text(act.get("device")):
            return "the display action names no panel"
        if act.get("pages") is not None:
            problem = _pages_problem(act)
            if problem:
                return problem
        elif not _text(act.get("text")):
            return "the display action has nothing to show"
        # 0 is legal and means "hold until something else takes the glass"
        # -- see HOLD_FOREVER. Only a NEGATIVE hold is nonsense. Absent or
        # null means "share the panel with the clock" (trigger_sentence and
        # HOLD_KINDS already read null that way; this refused it).
        if act.get("for_s") is not None:
            if not _finite(act["for_s"]):
                return "for_s must be a number of seconds"
            if float(act["for_s"]) < 0:
                return ("a display message cannot hold for a negative time "
                        "-- use 0 to hold it until something else is shown")
        # ROW IS OPTIONAL AND 1-BASED. Absent means "own the whole panel",
        # which is what every stored rule already means; a number means
        # "this line and nothing else", so three rules can each hold a row.
        if act.get("row") is not None:
            try:
                if int(act["row"]) < 1:
                    return "a display row is counted from 1"
            except (TypeError, ValueError):
                return "the display row must be a whole number"
    return None


def actions_of(rule: Dict[str, Any], branch: str = "then"
               ) -> List[Dict[str, Any]]:
    """A branch's actions, always as a list.

    `action` (one) and `actions` (many) both mean THEN. The singular came
    first and every stored rule still uses it, so it stays readable
    forever rather than being migrated -- the plural is simply what the
    block builder writes when a rule does more than one thing.
    """
    if branch == "then":
        many = rule.get("actions")
        if not (isinstance(many, list) and many):
            one = rule.get("action")
            return [one] if isinstance(one, dict) and one else []
    elif branch == "else":
        many = rule.get("else_actions")
    elif branch == "revert":
        rev = rule.get("revert")
        many = rev.get("actions") if isinstance(rev, dict) else None
    else:
        return []
    return [a for a in many if isinstance(a, dict)] \
        if isinstance(many, list) else []


def _span(seconds: Any) -> str:
    """"10 minutes" / "1 minute" / "45 seconds". Tolerant of a bad number:
    a rule kept `_invalid` is still listed, and its sentence must not raise."""
    try:
        seconds = float(seconds or 0)
    except (TypeError, ValueError):
        seconds = 0.0
    if not math.isfinite(seconds):
        seconds = 0.0
    mins = int(seconds // 60)
    return (f"{mins} minute{'s' if mins != 1 else ''}" if mins
            else f"{int(seconds)} seconds")


def trigger_sentence(rule: Dict[str, Any],
                     device_name: Callable[[str], str] = lambda d: d) -> str:
    """The rule as a person would say it — the list row and the AI hint."""
    t = rule.get("trigger", {})
    kind = t.get("kind")
    dev = device_name(str(t.get("device", "")))
    span = _span(t.get("for_s"))
    if kind == "event":
        # A CARD UID OR AN INFRARED CODE IS THE FACT ITSELF, so the
        # sentence says it. Without this, "fires" was the whole story and a
        # rule bound to one specific card read exactly like a rule bound to
        # every card -- two very different doors, one sentence.
        said = str(t.get("value", "on"))
        if said == "off":
            what = "goes quiet"
        elif said in ("on", "any", ""):
            what = "fires"
        else:
            what = f"reads {said}"
        head = f"when {dev} {what}"
    elif kind == "persist":
        head = f"when {dev} has been {t.get('value', 'on')} for {span}"
    elif kind == "absent":
        head = f"when {dev} is quiet for {span}"
    elif kind == "threshold":
        if t.get("above") is not None:
            head = f"when {dev} {t.get('key', '?')} rises above {t['above']}"
        else:
            head = f"when {dev} {t.get('key', '?')} falls below {t['below']}"
    elif kind == "change":
        by = t.get("by")
        head = (f"whenever {dev} {t.get('key', '?')} changes"
                + (f" by {by} or more" if by else ""))
    elif kind == "time":
        head = f"at {t.get('at', '?')}"
    else:
        head = "when ?"
    def phrase(a: Dict[str, Any]) -> str:
        if a.get("kind") == "wait":
            try:
                return f"wait {_override_words(float(a.get('s')))}"
            except (TypeError, ValueError, OverflowError):
                return "wait a while"
        if a.get("kind") == "scene":
            return f"run the scene '{a.get('scene', '?')}'"
        if a.get("kind") == "device":
            change = a.get("set", {})
            if "on" in change:
                if change["on"] == TOGGLE:
                    return f"flip {device_name(a.get('device', '?'))}"
                return (f"turn {device_name(a.get('device', '?'))} "
                        f"{'on' if change['on'] else 'off'}")
            return f"set {device_name(a.get('device', '?'))} to {change}"
        if a.get("kind") == "press":
            dev = device_name(a.get("device", "?"))
            button = str(a.get("button") or "").replace("_", " ")
            return f"press {button} on {dev}" if button else f"press {dev}"
        if a.get("kind") == "ask":
            return (f"ask me to approve "
                    f"{device_name(a.get('device', '?'))}")
        if a.get("kind") == "display":
            # A DISPLAY ACTION READ AS "send a notification" (2026-09-14),
            # because every kind that was not scene/device/ask fell through
            # to the line below. So a rule that writes a meter's reading to
            # the kitchen panel described itself, in the app's own list and
            # in the description it stores, as sending a notification --
            # which is a different thing happening in a different place.
            panel = device_name(a.get("device", "?"))
            row = a.get("row")
            where = f" on line {row}" if row else ""
            # THREE HOLDS, THREE SENTENCES (2026-09-14). This said "for
            # 600s" for a rule that named no hold at all -- true of the
            # first ten minutes and silent about the thing that actually
            # happens, which is that the line and the clock take turns for
            # as long as the rule lives. And a rule that DID name a hold
            # read in seconds, so ten minutes arrived as "600s".
            #
            # The absence of `for_s` is the signal, so it is read before
            # the number: a rule built without an opinion gets the cycle,
            # and says so.
            n_pages = len(a.get("pages") or [])
            if n_pages:
                where = ""
                first = display_words(a, first_page=True)
                said = f"show '{first}'" if first else "show a message"
                if n_pages > 1:
                    said += (f" and {n_pages - 1} more page"
                             f"{'s' if n_pages > 2 else ''}")
                if a.get("clock_between"):
                    said += " with the clock between"
                try:
                    hold = float(a.get("for_s"))
                except (TypeError, ValueError):
                    hold = DISPLAY_HOLD_SECONDS
                if a.get("for_s") is None:
                    return f"{said} on {panel}, sharing it with the clock"
                if hold == HOLD_FOREVER:
                    return f"{said} on {panel} and leave it up"
                return f"{said} on {panel} for {_span(hold)}"
            if a.get("for_s") is None:
                return (f"show it on {panel}{where}, sharing the panel "
                        f"with the clock")
            try:
                hold = float(a.get("for_s"))
            except (TypeError, ValueError):
                hold = DISPLAY_HOLD_SECONDS
            if hold == HOLD_FOREVER:
                return f"show it on {panel}{where} and leave it up"
            return f"show it on {panel}{where} for {_span(hold)}"
        return "send a notification"

    def joined(acts: List[Dict[str, Any]]) -> str:
        said = [phrase(a) for a in acts]
        if len(said) <= 1:
            return said[0] if said else "do nothing"
        return ", ".join(said[:-1]) + f" and {said[-1]}"

    sentence = f"{head}, {joined(actions_of(rule))}"
    if rule.get("loop") is True:
        sentence += ", and repeat"

    # THE TWO ELSE SHAPES, said the way a person would (her design,
    # 2026-08-27). This is the sentence the block builder shows live under
    # the blocks and stores as the rule's description, so the clauses have
    # to read as one thought, not as a dump of fields.
    els = actions_of(rule, "else")
    if els:
        sentence += f" -- otherwise {joined(els)}"
    rev = actions_of(rule, "revert")
    if rev:
        span = _span((rule.get("revert") or {}).get("after_s"))
        sentence += f" -- and {joined(rev)} once it has been quiet for {span}"
    mode, secs = override_spec(rule)
    if mode == "always":
        sentence += " -- even if you switch it yourself"
    elif mode == "window":
        sentence += (f" -- but if you switch it yourself, you win for "
                     f"{_override_words(secs)}")
    return sentence


class AutomationEngine:
    """The reflexes: a store of rules, the state that feeds them, and the
    two entry points (feed / tick) the MQTT thread drives."""

    def __init__(self, dm: Any, scenes: Any = None, esp: Any = None, *,
                 path: Optional[str] = None,
                 now: Callable[[], float] = time.time,
                 mono: Optional[Callable[[], float]] = None):
        self.dm = dm
        self.scenes = scenes
        self.esp = esp
        self.now = now
        #: A clock that only ever runs forward, to notice the wall clock
        #: being stepped (NTP arriving after boot, a hand-set date).
        #: (An injected test clock is its own steady clock: only the real
        #: wall clock can be "stepped".)
        self.mono = mono if mono is not None else (
            time.monotonic if now is time.time else now)
        self.path = path or _config.AUTOMATIONS_JSON_PATH
        self.override_s = float(_config.AUTOMATION_OVERRIDE_SECONDS)
        self.cooldown_s = float(_config.AUTOMATION_COOLDOWN_SECONDS)
        self.rules: List[Dict[str, Any]] = []
        #: (ts, rule name, what happened) — the black box.
        self.journal: deque = deque(maxlen=200)
        #: notifications an automation raised; drained by the shell into
        #: the notices list.
        self.alerts: deque = deque(maxlen=200)   # grew without bound
        #: Questions an `ask` action raised, waiting for a person
        #: and their PIN. Nothing here has changed anything yet.
        self.approvals: List[Dict[str, Any]] = []
        # runtime state
        self._human: Dict[str, float] = {}          # did -> FIRST touch of the window
        #: did -> the LAST human command. A rule with its own override
        #: window counts from here: "I win for 10 minutes" means ten minutes
        #: after the last thing I did, not after the first.
        self._human_last: Dict[str, float] = {}
        self._last: Dict[Tuple[str, str], Tuple[Any, float]] = {}
        self._since: Dict[Tuple[str, str], float] = {}   # persist stretches
        #: rule id -> the stretch start (`_since`) it already fired for.
        #: PER RULE: two persist rules on the same device and value with
        #: different for_s used to share one "done" flag, so the shorter
        #: one firing silenced the longer one for the whole stretch.
        self._persist_done: Dict[str, float] = {}
        self._activity: Dict[Tuple[str, str], float] = {}  # absent tracking
        #: rule id -> the last activity (`_activity`) it already fired for.
        #: Per rule for the same reason as _persist_done.
        self._absent_done: Dict[str, float] = {}
        self._threshold_side: Dict[Tuple[str, str], bool] = {}
        #: rule id -> "YYYY-MM-DD@HH:MM" of the occurrence it last fired for.
        #: The time is part of the key so a rule moved to later the same
        #: day, after it already fired, still fires at its new time.
        self._time_fired: Dict[str, str] = {}
        #: When tick() last ran, so a time trigger whose minute fell inside
        #: a gap between ticks (broker backoff, a slow tick) still fires.
        self._last_tick_ts: Optional[float] = None
        self._fired_at: Dict[str, float] = {}       # rule id -> last firing
        #: rule id -> when its revert comes due. Set on every firing (so a
        #: re-fire pushes it out, which IS the cancel), cleared when it
        #: runs. The motion-light lives here.
        self._revert_due: Dict[str, float] = {}
        #: rule id -> {"tries": n, "todo": [action index, ...]} while a
        #: revert whose device did not answer is being tried again.
        self._revert_retry: Dict[str, Dict[str, Any]] = {}
        #: Pending reverts, and who touched what last, survive a restart:
        #: this file sits beside the rules. See _save_state.
        self._state_path = self.path + ".state"
        self._state_dirty = False
        self._state_saved = 0.0
        self._approval_seq = 0
        #: (wall, monotonic) at the last tick -- see _rebase_clock.
        self._clock_pair: Optional[Tuple[float, float]] = None
        #: rule ids whose revert has RUN since their last firing -- the only
        #: case in which a repeated report of the same state re-fires the
        #: rule. Not "no revert pending": a firing that ran ELSE arms no
        #: revert, so that test re-ran ELSE on every repeated report.
        self._reverted: set = set()
        #: rule id -> its DO still running: paused at a Wait, or looping.
        #: {"steps", "loop", "rule" (as started), "shape", "timer", "busy"}.
        #: One run per rule; never saved (a restart ends every run).
        self._runs: Dict[str, Dict[str, Any]] = {}
        self._index: Dict[str, Dict[Tuple[str, int], str]] = {}
        #: Board topics heard so far, for the one-time 'hearing' lines.
        self._heard_topics: set = set()
        self._thread: Optional[threading.Thread] = None
        #: start() is called from several threads (startup, the notices
        #: poll, saving a rule); without this two callers could both find
        #: no thread and start two listeners, firing every rule twice.
        self._start_lock = threading.Lock()
        #: The listener's own MQTT client, once it is up, so the LCD can be
        #: written on the connection that is already open.
        self._pub: Any = None
        #: Set by the shell to CardStore.owner. A hub with no card store
        #: leaves it None and every tap is written down by its UID, which
        #: is exactly what an unenrolled card is.
        self.card_owner: Optional[Any] = None
        #: THE LAST 24 HOURS OF EVERY NUMBER, kept here (owner's ruling:
        #: Home Assistant is for integration devices only, so the day's
        #: statistics are computed by the hub, not read from HA's
        #: recorder). (did, key) -> deque of (ts, value), at most one sample
        #: per minute, pruned to a day. Persisted to readings_history.json
        #: so a restart does not wipe a day of numbers.
        self._history: Dict[Tuple[str, str], deque] = {}
        self.history_path = _config.READINGS_HISTORY_PATH
        self._history_saved = 0.0
        #: A firing changed last_fired/fired_count and the store has not
        #: been written since. Flushed on the history's cadence and at
        #: stop(), not per firing: a `change` rule fires every two seconds,
        #: and a fsync'd rewrite each time is an SD card worn out.
        self._rules_dirty = False
        #: Parts on a board RECORD that its PLAN refused, most recently
        #: rebuilt. Read by the flows that wait on one -- see
        #: rebuild_index and plan_drops().
        #: Meters whose 'no answer' has already been said once. See
        #: the ENERGY branch in feed().
        #: Bumped by _observe on every value that arrives. Read by
        #: ws_server's home-tile push so it can react instead of poll.
        #: When each board last had its settings re-asserted, and
        #: which have been mentioned. See _reassert_settings.
        self._settings_sent: Dict[str, float] = {}
        self._settings_said: set = set()
        self._reading_seq: int = 0
        #: (device_id, lowercased key) -> the board's own spelling, as
        #: "BLOCK#Field" -- "BME280#Temperature". Filled from the payloads
        #: themselves, because a Tasmota rule has to name the field the way
        #: Tasmota does and the hub deliberately lowercases everywhere else.
        #: See the note where it is written, in feed().
        self._field_src: Dict[Tuple[str, str], str] = {}
        #: Boards already given their reading rules, topic -> the set of
        #: fields those rules cover. A board whose sensors have not changed
        #: is not written to again.
        self._rules_sent: Dict[str, frozenset] = {}
        #: When each board was last written to, so a board discovering its
        #: sensors one message at a time is not rewritten once per message.
        self._rules_at: Dict[str, float] = {}
        self._meter_silent_said: set = set()
        #: Energy meters not answering now (every Address 0 and Voltage 0):
        #: their zeros are shown as the board sends them, but no rule acts
        #: on them. did -> when the silence began (for the warning).
        self._meter_silent: Dict[str, float] = {}
        #: (did, key) -> what rules last saw before the silence, so the first
        #: real reading after it is compared with that, not with a fake 0.
        self._meter_held: Dict[Tuple[str, str], Any] = {}
        self._plan_drops: List[str] = []
        self._lcd_targets: List[Dict[str, Any]] = []
        #: How far each panel has been woken, by topic: 1 = its settings
        #: were re-asserted, 2 = the glass was cleared a tick later, 3 = the
        #: clock is back on it and there is nothing left to do. A board that rebooted since
        #: install comes back with its screen dark or garbled; the hub owns
        #: that glass, so it says so once before it writes. See _lcd_tick.
        self._lcd_awake: Dict[str, int] = {}
        #: board topic -> the last LWT seen ("online"/"offline"), so a
        #: board that went away and came back can be told from a retained
        #: Online redelivered on a subscribe. See _note_presence.
        self._lcd_seen: Dict[str, str] = {}
        #: board topic -> when (monotonic) the hub last set its clock.
        self._board_time_sent: Dict[str, float] = {}
        #: Board topics whose LWT last said Offline and that have not spoken
        #: since. Their devices' stored readings are the last thing they
        #: said, not the truth: conditions do not trust them, and "quiet" or
        #: "has been on for" are not judged from silence.
        self._offline: set = set()
        #: board topic -> when it went offline (for the hub's notice).
        self._offline_since: Dict[str, float] = {}
        #: (when, keys, raw timings) of infrared codes the hub just sent,
        #: so hearing them back is not a press (note_ir_sent / _ir_echo).
        self._ir_sent: List[Tuple[float, set, Any]] = []
        #: Fingerprint of the board records, to notice an edit made anywhere
        #: (see _check_boards).
        self._boards_sig: Optional[tuple] = None
        #: topic -> when its panel was last re-woken, and how many times in
        #: this hub run. Both are the brakes on a board that keeps coming
        #: and going.
        self._lcd_rewoke: Dict[str, float] = {}
        self._lcd_rewakes: Dict[str, int] = {}
        #: Topics whose display settings were re-sent in this hub run
        #: because the board answered DisplayModel 0 -- once per run, so a
        #: firmware with no display driver is said once, not every wake.
        self._lcd_resent: set = set()
        self._lcd_resent_said: set = set()
        self._lcd_nodriver_said: set = set()
        #: PANEL TOPIC -> the moment that panel is free again. While a
        #: topic's entry is in the future a RULE owns that panel and the
        #: wake sequence keeps its hands off it. See _show and _lcd_tick.
        #:
        #: Keyed by topic since 2026-09-14. It was one scalar for the whole
        #: house, so the shortest message on any panel handed EVERY panel
        #: back to the clock -- see _show for the worked example.
        self._lcd_hold_until: Dict[str, float] = {}
        #: PANEL TOPIC -> row -> the message that panel is STANDING ON.
        #:
        #: What a rule last wrote to each row, kept so it can be put back
        #: after the clock's turn (DISPLAY_CLOCK_SECONDS). Not the rendered
        #: text: the rule id, the RAW text and the hold -- so the line that
        #: returns is re-rendered against the live cache and shows the
        #: number that is true NOW, not the one that was true when the rule
        #: fired. A rule that has since been deleted or switched off has its
        #: standing line dropped rather than replayed.
        #:
        #: The row key is the action's own `row`, with None meaning "this
        #: message owns the whole panel" -- exactly the two shapes _show
        #: already writes.
        self._lcd_standing: Dict[str, Dict[Any, Dict[str, Any]]] = {}
        #: DEVICE ID -> the line written to that panel FROM THE APP (a
        #: rule of its own, never stored with the automations), so it is
        #: re-rendered and restored exactly like a rule's line.
        self._manual: Dict[str, Dict[str, Any]] = {}
        #: PANEL TOPIC -> (when, the lines last painted): a held line that
        #: quotes a reading is re-rendered every LIVE_REFRESH_S and
        #: repainted only when the text changed.
        self._lcd_live: Dict[str, Tuple[float, Any]] = {}
        #: PANEL TOPIC -> which page (or clock turn) of a several-page
        #: message is up, and since when: {slot, since, clock}.
        self._lcd_page: Dict[str, Dict[str, Any]] = {}
        #: DEVICE ID -> every reading it has ever reported, kept across
        #: restarts, so a display line and a rule can offer them all (not
        #: only what arrived since the hub started).
        self._seen_keys: Dict[str, set] = {}
        #: READING KEY -> words from the board's own field name
        #: ("apparentpower" -> "apparent power", from "ApparentPower").
        self._key_words: Dict[str, str] = {}
        #: PANEL TOPIC -> the moment the clock's turn is over and the
        #: standing lines go back up. Set when a hold expires and when a
        #: wake finishes, so the clock is always actually SEEN for its turn
        #: rather than flashed between two writes in the same tick.
        self._lcd_clock_until: Dict[str, float] = {}
        #: WHAT EACH DEVICE DRAWS (see _draw_switched). The main meter:
        #: {device, key, measures: "power"|"current"}, or None; persisted.
        self._main_meter: Optional[Dict[str, Any]] = None
        #: The person said "no main meter": none is chosen on its own.
        self._main_meter_off = False
        #: DEVICE ID -> its last DRAW_SAMPLES_KEPT jumps, in W or A, and
        #: the meter they were measured on ({device, key, measures}).
        self._draws: Dict[str, List[float]] = {}
        self._draw_meter: Optional[Dict[str, Any]] = None
        #: Every on/off change lately: (when, device id).
        self._onoff_log: deque = deque(maxlen=256)
        #: DEVICE ID -> a jump waiting for the meter's "after" reading.
        self._draw_pending: Dict[str, Dict[str, Any]] = {}
        #: PANEL TOPIC -> the I2C addresses its board last reported, or None
        #: for "never answered". A frozenset that does not contain the
        #: panel's own address is the only evidence of absence this hub
        #: accepts; everything else reads as present. See _note_i2c_scan.
        self._lcd_bus: Dict[str, Optional[frozenset]] = {}
        #: topic -> when I2Cscan was last sent, and the topics whose absence
        #: has already been said out loud (once per hub run, per topic).
        self._lcd_scanned: Dict[str, float] = {}
        self._lcd_absent_said: set = set()
        #: Topics whose I2C bus could not be scanned at all -- said once,
        #: because that is the template's fault and not the screen's.
        self._lcd_nobus_said: set = set()
        #: "<topic>#<n>" for every switch that has reported an action the
        #: hub cannot read as on or off. Said once each. See _switch_action.
        self._switch_odd_said: set = set()
        #: rule id -> has this rule's cooldown already been explained since
        #: it last really fired? One line per burst, not one per firing.
        self._cooldown_said: Dict[str, bool] = {}
        #: topic -> when the whole panel was last re-asserted from scratch.
        #: See DISPLAY_REPEAT_SECONDS.
        self._lcd_repeated: Dict[str, float] = {}
        #: Panels whose DisplayAddress has been corrected from the bus in
        #: this run -- once each, so a wrong setting is fixed rather than
        #: reported for ever, and a panel that still does not answer
        #: afterwards is a wire and not a number. See _note_i2c_scan.
        self._lcd_readdressed: set = set()
        #: topic -> when this hub sent it `DisplayModel <n>`. In Tasmota that
        #: command sets the restart flag, so the board goes down a second
        #: later; the wake pauses until it is back (or LCD_RESTART_WAIT
        #: passes, e.g. the command was refused and nothing restarted).
        self._lcd_restarting: Dict[str, float] = {}
        #: topic -> when the hub last restarted it (kept after it is back):
        #: the broker's notice that it went offline can come a minute late.
        self._lcd_restarted_at: Dict[str, float] = {}
        #: board topic -> when the hub last restarted it after a power-on so
        #: its SPI/I2C parts are found (see _note_boot_reason).
        self._boot_restarted_at: Dict[str, float] = {}
        self._stop = threading.Event()
        self._lock = threading.RLock()
        #: fingerprint of the rules file as this engine last read or wrote it.
        self._sig: Optional[tuple] = None
        self._load()
        self._load_history()
        self._load_state()

    # ── store ───────────────────────────────────────────────────────────

    def _load(self) -> None:
        try:
            with open(self.path, "r", encoding="utf-8") as fh:
                self._sig = path_fingerprint(self.path)
                data = json.load(fh)
            # Every entry is KEPT. A rule that fails validation (one from a
            # newer schema, a hand edit) is marked and skipped at evaluation
            # rather than dropped -- the next _save() used to delete it.
            repaired = self._adopt([r for r in data.get("rules", [])
                                    if isinstance(r, dict)])
            # An older file may carry a "hidden" list (the HA ear's); it is
            # ignored and dropped on the next save.
            if repaired:
                # Ids that were duplicated or missing were given new ones;
                # written back now, so the file and the memory agree.
                self._commit(lambda base: base.__setitem__(
                    slice(None), [{k: v for k, v in r.items()
                                   if k != "_invalid"} for r in self.rules])
                    or None)
        except FileNotFoundError:
            pass
        except Exception as exc:                    # noqa: BLE001
            # KEEP A COPY BEFORE STARTING EMPTY. The next _save() (any add,
            # any firing) writes over this path, so a hand-edit typo used to
            # cost every rule in the house with nothing left to recover.
            backup = f"{self.path}.unreadable-{int(time.time())}"
            try:
                shutil.copy2(self.path, backup)
            except OSError:
                backup = ""
            event("AUTO", f"store unreadable ({exc}) -- starting empty"
                          + (f"; the unreadable file was kept as {backup}"
                             if backup else ""))

    @staticmethod
    def _write_json(path: str, data: Any, what: str,
                    indent: Optional[int] = None) -> None:
        """Atomically: a fsync'd temp file, then os.replace. Never raises."""
        # A UNIQUE temp name: ws_server and a terminal hub each run an engine
        # on the same files, and a shared "<path>.tmp" let one rename the
        # other's half-written temp into place.
        try:
            atomic_write_json(path, data, indent=indent)
        except Exception as exc:                    # noqa: BLE001
            event("AUTO", f"{what} write failed: {exc}")

    #: Keys the engine itself writes on every firing. A rule that differs
    #: from the one in memory ONLY in these is the same rule.
    _BOOKKEEPING = ("_invalid", "fired_count", "last_fired")

    def _fresh_id(self, taken: set) -> str:
        base = f"auto_{int(self.now() * 1000):x}"
        rid, n = base, 1
        while rid in taken:
            rid = f"{base}_{n}"
            n += 1
        return rid

    def _adopt(self, raw: List[Dict[str, Any]]) -> bool:
        """Make `raw` (rules as the FILE has them) the engine's rules.

        A rule already in memory and unchanged keeps its object, so a
        firing in progress and the bookkeeping it writes are not orphaned.
        Rules that fail validation are KEPT, marked and not evaluated. Ids
        that are missing or duplicated (older files; ids did collide before
        2026-09-14, and a duplicate meant Pause on one rule paused another)
        are given fresh ones. True when any id had to be repaired."""
        mem = {r.get("id"): r for r in self.rules if r.get("id")}
        out: List[Dict[str, Any]] = []
        taken: set = set()
        repaired = False
        for r in raw:
            r = dict(r)
            rid = r.get("id")
            if not isinstance(rid, str) or not rid or rid in taken:
                rid = self._fresh_id(taken | {x.get("id") for x in raw
                                              if isinstance(x.get("id"), str)})
                r["id"] = rid
                repaired = True
            taken.add(rid)
            problem = validate_rule(r)
            if problem:
                r["_invalid"] = problem
                if not (mem.get(rid) or {}).get("_invalid"):
                    event("AUTO", f"rule {r.get('name')!r} kept but not "
                                  f"evaluated: {problem}")
            else:
                r.pop("_invalid", None)
            old = mem.get(rid)
            if old is not None and not repaired and all(
                    old.get(k) == r.get(k)
                    for k in set(old) | set(r)
                    if k not in self._BOOKKEEPING):
                # Same rule. Keep the object, but not a stale verdict.
                if r.get("_invalid"):
                    old["_invalid"] = r["_invalid"]
                else:
                    old.pop("_invalid", None)
                r = old
            out.append(r)
        self.rules = out
        return repaired

    @staticmethod
    def _read_disk(path: str) -> Optional[List[Dict[str, Any]]]:
        """The rules as the FILE has them right now; None when it is absent
        or unreadable (the caller then trusts its own memory)."""
        try:
            with open(path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
        except Exception:                           # noqa: BLE001
            return None
        rules = data.get("rules") if isinstance(data, dict) else None
        if not isinstance(rules, list):
            return None
        return [r for r in rules if isinstance(r, dict)]

    def _commit(self, mutate: Callable[[List[Dict[str, Any]]],
                                       Optional[str]]) -> Optional[str]:
        """THE ONE WAY THE STORE IS WRITTEN (2026-10-01).

        It used to rewrite the whole file from this engine's memory, and
        ws_server and a terminal hub each run an engine on the file: the
        phone adds "Porch light", the terminal hub then saves anything, and
        the file no longer has it. Now every change is: take the
        cross-process lock, READ THE FILE AS IT IS, apply this one change to
        that, write it whole, and adopt the result -- so what another process
        added or removed in the meantime is kept.

        `mutate` edits the list it is given and returns a sentence when the
        change cannot be made (nothing is written then). Returns None on
        success, or the sentence saying why not -- INCLUDING a failed write,
        which used to be logged while the caller was told "saved". Call it
        with self._lock held."""
        try:
            with flock(self.path + ".lock"):
                disk = self._read_disk(self.path)
                base = [dict(r) for r in (disk if disk is not None
                                          else self.rules)]
                for r in base:
                    r.pop("_invalid", None)
                problem = mutate(base)
                if problem:
                    return problem
                self._sig = atomic_write_json(
                    self.path, {"rules": base}, indent=2)
        except Exception as exc:                    # noqa: BLE001
            event("AUTO", f"store write failed: {exc}")
            return f"the automation could not be saved ({exc})"
        self._rules_dirty = False
        self._adopt(base)
        return None

    def _save(self) -> None:
        """Kept for callers that only want the file to match memory."""
        self._commit(lambda base: base.__setitem__(
            slice(None), [{k: v for k, v in r.items() if k != "_invalid"}
                          for r in self.rules]) or None)

    def _reload_if_changed(self) -> None:
        """Another process saved: take its rules. Cheap (one stat), and run
        on every tick, so a rule added from the phone reaches a terminal hub
        without a restart."""
        if path_fingerprint(self.path) == self._sig:
            return
        with self._lock:
            disk = self._read_disk(self.path)
            self._sig = path_fingerprint(self.path)
            if disk is not None:
                self._adopt(disk)

    def _flush_rules(self) -> None:
        """Write the firings' bookkeeping (count, last time) if it changed
        since the last save -- merged into what is on disk, not over it."""
        with self._lock:
            if not self._rules_dirty:
                return
            live = {r.get("id"): r for r in self.rules}

            def merge(base: List[Dict[str, Any]]) -> Optional[str]:
                for r in base:
                    m = live.get(r.get("id"))
                    if m is None:
                        continue
                    r["fired_count"] = max(int(r.get("fired_count") or 0),
                                           int(m.get("fired_count") or 0))
                    if m.get("last_fired") is not None:
                        r["last_fired"] = m["last_fired"]
                return None
            self._commit(merge)

    # ── runtime state that must outlive a restart ───────────────────────

    def _save_state(self) -> None:
        """Pending reverts and the last human command per device, beside the
        rules. WITHOUT IT a restart forgot every pending revert and the
        light that motion had switched on stayed on until someone noticed
        (2026-10-01 audit). Times are stored with the moment of saving so a
        clock that was wrong at boot can be corrected on the way back in."""
        self._state_dirty = False
        ts = self.now()
        self._state_saved = ts
        recent = {d: t for d, t in self._human_last.items()
                  if _finite(t) and 0 <= ts - t < OVERRIDE_MAX_SECONDS}
        data = {"saved_at": ts,
                "revert_due": dict(self._revert_due),
                "revert_retry": {k: dict(v) for k, v
                                 in self._revert_retry.items()},
                "human_last": recent,
                # WHAT EACH PANEL IS STANDING ON, so a restart does not leave
                # the clock on the glass until the rule happens to fire
                # again (2026-11-05). Row None is stored as "all".
                "seen_keys": {d: sorted(k) for d, k in self._seen_keys.items()
                              if k},
                "key_words": dict(self._key_words),
                "main_meter": (dict(self._main_meter)
                               if self._main_meter else None),
                "main_meter_off": self._main_meter_off,
                "draws": {d: list(v) for d, v in self._draws.items() if v},
                "draw_meter": (dict(self._draw_meter)
                               if self._draw_meter else None),
                "lcd_manual": {d: {k: v for k, v in r["action"].items()
                                   if k in ("text", "for_s", "pages",
                                            "page_s", "clock_between")}
                               for d, r in self._manual.items()},
                "lcd_standing": {
                    t: {("all" if r is None else str(r)): dict(e)
                        for r, e in rows.items()}
                    for t, rows in self._lcd_standing.items() if rows}}
        if not (data["revert_due"] or data["human_last"]
                or data["lcd_standing"] or data["lcd_manual"]
                or data["seen_keys"] or data["main_meter"]
                or data["main_meter_off"] or data["draws"]) \
                and not os.path.exists(self._state_path):
            return
        try:
            atomic_write_json(self._state_path, data)
        except OSError as exc:
            event("AUTO", f"could not save pending reverts ({exc})")

    def _load_state(self) -> None:
        try:
            with open(self._state_path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            saved_at = float(data.get("saved_at"))
        except (OSError, ValueError, TypeError):
            return
        ts = self.now()
        # The clock was BEHIND when this was saved (a Pi that booted with a
        # wrong date): keep every remaining span, not every absolute time.
        behind = max(0.0, saved_at - ts)

        def fix(t: Any) -> Optional[float]:
            return float(t) - behind if _finite(t) else None
        for rid, due in (data.get("revert_due") or {}).items():
            d = fix(due)
            if d is not None:
                self._revert_due[str(rid)] = d
        for rid, r in (data.get("revert_retry") or {}).items():
            if isinstance(r, dict) and str(rid) in self._revert_due:
                self._revert_retry[str(rid)] = {
                    "tries": int(r.get("tries") or 0),
                    "todo": r.get("todo")
                    if isinstance(r.get("todo"), list) else None}
        for did, keys in (data.get("seen_keys") or {}).items():
            if isinstance(keys, list):
                self._seen_keys[str(did)] = {str(k) for k in keys}
        words = data.get("key_words")
        if isinstance(words, dict):
            self._key_words.update({str(k): str(v) for k, v in words.items()})
        # The main meter, and what each device draws on it.
        for name in ("main_meter", "draw_meter"):
            meter = data.get(name)
            if isinstance(meter, dict) and meter.get("device") \
                    and meter.get("key") \
                    and meter.get("measures") in self._DRAW_UNIT:
                setattr(self, f"_{name}", {"device": str(meter["device"]),
                                           "key": str(meter["key"]),
                                           "measures": str(meter["measures"])})
        self._main_meter_off = data.get("main_meter_off") is True
        for did, samples in (data.get("draws") or {}).items():
            if isinstance(samples, list) and self._draw_meter is not None:
                kept = [float(v) for v in samples
                        if _finite(v) and float(v) > 0]
                if kept:
                    self._draws[str(did)] = kept[-self.DRAW_SAMPLES_KEPT:]
        for did, line in (data.get("lcd_manual") or {}).items():
            if isinstance(line, dict) and (
                    isinstance(line.get("text"), str)
                    or isinstance(line.get("pages"), list)):
                kept = {k: v for k, v in line.items()
                        if k in ("text", "pages", "page_s", "clock_between")}
                self._manual[str(did)] = {
                    "id": f"manual:{did}", "name": "From the app",
                    "enabled": True, "trigger": {},
                    "action": {"kind": "display", "device": str(did),
                               **kept,
                               "for_s": float(line.get("for_s") or 0)}}
        for topic, rows in (data.get("lcd_standing") or {}).items():
            if not isinstance(rows, dict):
                continue
            for key, entry in rows.items():
                if not isinstance(entry, dict) or not entry.get("rule_id"):
                    continue
                try:
                    row = None if key == "all" else int(key)
                except ValueError:
                    continue
                # Rules gone or switched off are dropped at the next tick
                # (_standing_lines), so nothing stale comes back.
                self._lcd_standing.setdefault(str(topic), {})[row] = {
                    "rule_id": str(entry["rule_id"]),
                    "device": entry.get("device")}
        for did, t in (data.get("human_last") or {}).items():
            v = fix(t)
            if v is not None and 0 <= ts - v < OVERRIDE_MAX_SECONDS:
                self._human_last[str(did)] = v
        if self._revert_due:
            event("AUTO", f"{len(self._revert_due)} pending revert(s) "
                          f"restored")

    #: A wall clock that moved this much more (or less) than the monotonic
    #: one was STEPPED, not merely slow.
    CLOCK_STEP_SECONDS = 30.0

    def _rebase_clock(self) -> None:
        """Keep every running timer honest across a clock step.

        A Pi has no battery clock: it boots on a stale date and NTP steps it
        forward by hours or years minutes later. Every timer here is a wall
        time, so a forward step made all reverts, cooldowns and persist
        stretches expire at once and a backward one froze them. Shifted by
        the size of the step, each keeps the time it still had to run."""
        wall, mono = self.now(), self.mono()
        pair, self._clock_pair = self._clock_pair, (wall, mono)
        if pair is None:
            return
        step = (wall - pair[0]) - (mono - pair[1])
        if abs(step) < self.CLOCK_STEP_SECONDS:
            return
        with self._lock:
            for table in (self._human, self._human_last, self._revert_due,
                          self._fired_at, self._since, self._activity,
                          self._lcd_hold_until, self._lcd_clock_until,
                          self._lcd_repeated, self._lcd_scanned,
                          self._lcd_restarting, self._lcd_restarted_at,
                          self._meter_silent, self._settings_sent,
                          self._offline_since):
                for k, v in list(table.items()):
                    if isinstance(v, (int, float)):
                        table[k] = v + step
            for k, (val, t) in list(self._last.items()):
                self._last[k] = (val, t + step)
            # A jump measured across a clock step cannot be timed: dropped.
            self._draw_pending.clear()
            self._onoff_log.clear()
            for a in self.approvals:
                a["asked_at"] += step
                a["expires_at"] += step
        self._last_tick_ts = wall
        event("AUTO", f"the clock moved by {int(step)}s -- timers "
                      f"adjusted to match")
        self._state_dirty = True

    def rules_using_device(self, did: str) -> List[str]:
        """Names of the rules that watch, test or drive this device -- asked
        before a device is deleted, so the person is told what will stop
        working instead of finding out when a rule fires into nothing."""
        did = str(did)
        out: List[str] = []
        for r in list(self.rules):
            t = r.get("trigger") or {}
            hit = t.get("device") == did or any(
                isinstance(c, dict) and c.get("kind") in ("device", "reading")
                and c.get("device") == did
                for c in [r.get("condition")] + list(r.get("conditions") or []))
            if not hit:
                for branch in ("then", "else", "revert"):
                    if any(a.get("device") == did
                           for a in actions_of(r, branch)):
                        hit = True
                        break
            if hit:
                out.append(str(r.get("name", "?")))
        return out

    def rules_using_scene(self, scene: str) -> List[str]:
        """Names of the rules that run this scene."""
        want = str(scene).strip().lower()
        out: List[str] = []
        for r in list(self.rules):
            for branch in ("then", "else", "revert"):
                if any(a.get("kind") == "scene"
                       and str(a.get("scene", "")).strip().lower() == want
                       for a in actions_of(r, branch)):
                    out.append(str(r.get("name", "?")))
                    break
        return out

    def get(self, name_or_id: str) -> Optional[Dict[str, Any]]:
        want = str(name_or_id).strip().lower()
        for r in self.rules:
            if r.get("id") == name_or_id or \
                    str(r.get("name", "")).strip().lower() == want:
                return r
        return None

    @staticmethod
    def _truthy(value: Any) -> bool:
        """`enabled: "false"` is a truthy STRING and the rule ran."""
        if isinstance(value, str):
            return value.strip().lower() not in ("", "false", "0", "no",
                                                 "off")
        return bool(value)

    def add(self, rule: Dict[str, Any]) -> Tuple[bool, str]:
        problem = validate_rule(rule)
        if problem:
            return False, problem
        problem = self.screen_all(rule)
        if problem:
            return False, problem
        rule = dict(rule)
        normalise_rule(rule)
        rule.pop("_invalid", None)
        placed: Dict[str, Any] = {}

        def put(base: List[Dict[str, Any]]) -> Optional[str]:
            # Checked against the FILE, under the cross-process lock, so two
            # saves racing with one name -- even from two processes -- cannot
            # both pass the check.
            want = str(rule.get("name", "")).strip().lower()
            if any(str(r.get("name", "")).strip().lower() == want
                   for r in base):
                return "an automation with that name already exists"
            taken = {r.get("id") for r in base}
            new = dict(rule)
            # AN ID THAT IS ACTUALLY UNIQUE (2026-09-14): `auto_<ms>`, bumped
            # until free. get() returns the first match, so two rules with
            # one id meant every edit aimed at the second landed on the
            # first.
            rid = str(new.get("id") or "")
            if not rid or rid in taken:
                rid = self._fresh_id(taken)
            new["id"] = rid
            new["enabled"] = self._truthy(new.get("enabled", True))
            new.setdefault("created_at", self.now())
            try:
                new["fired_count"] = int(new.get("fired_count") or 0)
            except (TypeError, ValueError):
                new["fired_count"] = 0
            base.append(new)
            placed.update(new)
            return None
        with self._lock:
            problem = self._commit(put)
        if problem:
            return False, problem
        event("AUTO", f"added {placed['name']!r}: "
                      f"{trigger_sentence(placed)}")
        self._check_reading_now(placed["id"])
        return True, placed["id"]

    def update(self, name_or_id: str, /, **changes) -> Tuple[bool, str]:
        """Passing None for a field CLEARS it (condition, else_actions,
        revert). It used to be filtered out as "not passed", so a rule could
        never lose its time window once it had one. Omit a field, or pass
        _UNSET, to leave it alone."""
        rule = self.get(name_or_id)
        if rule is None:
            return False, "no automation by that name"
        candidate = dict(rule)
        candidate.pop("_invalid", None)   # a verdict on the OLD text
        for k, v in changes.items():
            if v is _UNSET:
                continue
            if v is None:
                candidate.pop(k, None)
            else:
                candidate[k] = v
        # THEN IS ONE SLOT WITH TWO SPELLINGS, AND WRITING ONE MUST CLEAR
        # THE OTHER (2026-09-14): `action` (one) and `actions` (many) both
        # mean THEN, `actions_of` prefers a non-empty `actions`, so an edit
        # that wrote only `action` was ignored while update() said "updated".
        # Only a REAL value clears its twin: None means "clear this field".
        if changes.get("action", _UNSET) not in (_UNSET, None):
            candidate.pop("actions", None)
        if changes.get("actions", _UNSET) not in (_UNSET, None):
            candidate.pop("action", None)
        if "enabled" in candidate:
            candidate["enabled"] = self._truthy(candidate["enabled"])
        # PAUSING IS NEVER JUDGED (2026-10-01). It re-ran full validation and
        # screening, so a rule whose scene had been deleted could not be
        # paused -- only deleted -- and a rule that fires "scene no longer
        # exists" every time could not be stopped. Turning something OFF
        # cannot make the house less safe; resuming is checked as before.
        pausing = (all(v is _UNSET or k == "enabled" or k in OWNER_FIELDS
                       for k, v in changes.items())
                   and candidate.get("enabled") is False)
        if not pausing:
            problem = validate_rule(candidate)
            if problem:
                return False, problem
            normalise_rule(candidate)
            problem = self.screen_all(candidate)
            if problem:
                return False, problem
        rid = candidate.get("id")

        def swap(base: List[Dict[str, Any]]) -> Optional[str]:
            # A RENAME ONTO ANOTHER RULE'S NAME is refused, as add() refuses
            # it: get() returns the first match, so the second rule of that
            # name could no longer be edited or removed by name.
            new_name = str(candidate.get("name", "")).strip().lower()
            for r in base:
                if r.get("id") != rid and \
                        str(r.get("name", "")).strip().lower() == new_name:
                    return "an automation with that name already exists"
            for i, existing in enumerate(base):
                if existing.get("id") == rid:
                    base[i] = dict(candidate)
                    return None
            # Removed (here or by another process) while this edit was
            # being validated: nothing to replace.
            return "that automation is no longer here"
        # SWAP, DO NOT EDIT IN PLACE (2026-09-14): tick() and _observe() walk
        # the rules with no lock; _adopt() replaces the list with whole
        # dicts, so a reader holding the old one acts on a complete, old
        # rule, never a half-written one.
        with self._lock:
            problem = self._commit(swap)
        if problem:
            return False, problem
        self._check_run(str(rid or ""))     # switched off or edited: stop it
        if candidate.get("trigger") != rule.get("trigger") \
                or bool(candidate.get("enabled", True)) \
                != bool(rule.get("enabled", True)):
            # The side a threshold was last on belongs to the OLD line -- or
            # to before a pause, when nothing watched it. Kept, it could
            # swallow the first crossing after the edit or the resume.
            self._threshold_side.pop((str(rid or ""), "side"), None)
            # A new line, or just turned on: act now if already past it.
            self._check_reading_now(str(rid))
        return True, str(rid)

    # ── owners ──────────────────────────────────────────────────────────

    def owned_by(self, user: str) -> List[Dict[str, Any]]:
        return [r for r in self.rules if user and r.get("created_by") == user]

    def _restamp(self, pick: Callable[[Dict[str, Any]], Optional[Dict[str, Any]]]
                 ) -> int:
        """Rewrite owner fields only -- no validation (nothing the rule DOES
        changes), swapped whole like every other write."""
        moved = [0]

        def swap(base: List[Dict[str, Any]]) -> Optional[str]:
            for i, r in enumerate(base):
                new = pick(r)
                if new is not None:
                    base[i] = new
                    moved[0] += 1
            return None
        with self._lock:
            problem = self._commit(swap)
        return 0 if problem else moved[0]

    def rename_owner(self, old: str, new: str) -> int:
        if not old or not new or old == new:
            return 0

        def pick(r):
            if old not in (r.get("created_by"), r.get("changed_by")):
                return None
            out = dict(r)
            for k in ("created_by", "changed_by"):
                if out.get(k) == old:
                    out[k] = new
            return out
        return self._restamp(pick)

    def hand_over(self, rule_id: str, to: str) -> bool:
        return self._restamp(lambda r: dict(r, created_by=to)
                             if r.get("id") == rule_id else None) > 0

    def remove(self, name_or_id: str) -> bool:
        rule = self.get(name_or_id)
        if rule is None:
            return False
        rid = rule.get("id")
        with self._lock:
            problem = self._commit(lambda base: base.__setitem__(
                slice(None), [r for r in base if r.get("id") != rid]) or None)
        if problem:
            return False
        self._check_run(str(rid or ""))     # removed: stop its run
        event("AUTO", f"removed {rule.get('name')!r}")
        return True

    # ── the two hard laws ───────────────────────────────────────────────

    def screen_all(self, rule: Dict[str, Any]) -> Optional[str]:
        """Every branch of a rule, held to the secure-device law.

        THEN, ELSE and the revert are screened alike, because a rule that
        refused to unlock the door on the way in and unlocked it on the way
        out would be worse than one that never pretended. (The law is
        enforced AGAIN at every firing -- a device can be marked secure
        after the rule was written -- so this is the early, explaining
        refusal, not the guarantee.)
        """
        for branch in ("then", "else", "revert"):
            for action in actions_of(rule, branch):
                problem = self.action_screening(action)
                if problem:
                    return problem
                if str(action.get("kind")) == "display":
                    problem = self.display_text_problem(display_words(action))
                    if problem:
                        return problem
        return None

    #: The two placeholders that need no device: they are the rule's own
    #: trigger, which every rule has by definition.
    _OWN_PLACEHOLDERS = ("value", "name")

    def display_text_problem(self, text: Any) -> Optional[str]:
        """A placeholder that can only ever come out blank, caught at SAVE.

        `_fill` renders anything it cannot resolve as an empty string, on
        purpose: a blank space on a panel says "no reading" more honestly
        than `{hall.dht11}` does. That is right for a reading that has not
        arrived, and quietly wrong for a TYPO, which can never arrive. The
        two look identical on the glass -- you write a line, the rule
        fires, and the panel shows `kitchen  A` with no way to tell
        whether the meter is quiet or you mistyped.

        The commonest slip by far is a DOT where the colon goes
        (2026-09-12: "what if i write kitchen {kitchen.power_meter.current}
        A"). It is an easy mistake because a device id is already full of
        dots -- and `{kitchen.power_meter.current}` is not an error to the
        parser, it is a request for a device with that name, which simply
        does not exist. So it blanks. That one is named and corrected here
        rather than merely refused.

        DELIBERATELY LENIENT ABOUT THE KEY. A reading name is only refused
        when the device is known to have OTHER readings, which makes the
        one written a typo. A device nothing has been heard from yet has no
        list to check against, and refusing `{hall.dht11:temperature}`
        because the sensor has not spoken would block a rule written while
        a board is still on the bench. Unknown DEVICE is different: that is
        never a matter of waiting.
        """
        devices = (self.dm.devices or {}) if self.dm else {}
        for body in re.findall(r"\{([^{}]*)\}", str(text or "")):
            body = body.strip()
            if not body or body in self._OWN_PLACEHOLDERS:
                continue
            did, _, want = body.partition(":")
            did, want = did.strip(), want.strip()
            if did not in devices:
                head, _, tail = did.rpartition(".")
                if head in devices and tail:
                    return (f"{{{body}}} has a dot where it needs a colon. "
                            f"Write {{{head}:{tail}}} -- a device id already "
                            f"has dots in it, so the colon is what separates "
                            f"the device from the reading.")
                return (f"there is no device called {did!r}, so {{{body}}} "
                        f"would only ever show as a blank space")
            if want:
                keys = self.reading_keys(did)
                if keys and want not in keys:
                    name = (devices.get(did) or {}).get("name") or did
                    return (f"{name} has no reading called {want!r}, so "
                            f"{{{body}}} would only ever show as a blank "
                            f"space. It reports {', '.join(keys)}.")
        return None

    def secure_direction(self, did: str, change: Dict[str, Any]) -> str:
        """Is this change making the device MORE secure, or less?

        "secure" (lock it, shut the gate), "unsecure" (open it), or
        "unknown" -- and unknown counts as unsecure everywhere it matters.
        See SECURING_ON for why this is a table and not a rule of thumb.

        TWO TABLES, because there are two kinds of device here. An HA device
        is answered by its domain; a local MQTT device has no domain and is
        answered by the word it uses for its own ON state. Asking only the
        first meant every hand-registered lock in the house came back
        "unknown" -- see SECURING_ON_WORD for what that cost.
        """
        d = (self.dm.devices or {}).get(str(did)) or {}
        entity = str(d.get("ha_entity_id") or "")
        domain = entity.split(".", 1)[0] if "." in entity else ""
        want = SECURING_ON.get(domain)
        if want is None:
            want = securing_on_word(d)
        if want is None or "on" not in change:
            return "unknown"
        # A TOGGLE NAMES NO DIRECTION, so it has none -- and "unknown" is
        # treated as the dangerous one everywhere it matters, which is
        # exactly right here. Without this the sentinel would be read as a
        # value: `bool("toggle")` is True, so a rule that toggles a LOCK
        # would be screened as "makes it safer, allowed outright", when
        # half the time it unlocks the door. The one place a string
        # masquerading as a bool could have opened something.
        if change.get("on") == TOGGLE:
            return "unknown"
        return "secure" if bool(change.get("on")) is want else "unsecure"

    def action_screening(self, action: Dict[str, Any]) -> Optional[str]:
        """The secure-device law, as the owner refined it (2026-08-27).

        It used to be flat: a secure device could never be an automation
        action at all. That was too blunt in two directions she spotted --
        "lock the front door at 10pm" is fail-safe and was being refused,
        and there are real moments (a guest taps a card) where the house
        SHOULD be able to raise the question. So the law is now:

          * MAKING A DEVICE SAFER is allowed outright -- locking, closing,
            arming. Nothing bad follows from a door locking itself.
          * MAKING IT LESS SAFE is never done unattended. Not with a PIN
            entered when the rule was written either: that proves who
            AUTHORED it, not who tripped the sensor three weeks later, and
            a cloned card or a curtain moving past a PIR would open the
            door with nobody there.
          * Instead the rule may ASK -- an `ask` action raises a
            PIN-gated approval on the phone and drives nothing until a
            person answers it. The automation carries the question; the
            human carries the authority.

        Returns the refusal sentence, or None when the action is clean.
        """
        kind = action.get("kind")
        pairs: List[Tuple[str, Dict[str, Any]]] = []
        if kind == "device":
            pairs = [(str(action.get("device", "")),
                      dict(action.get("set", {})))]
        elif kind == "press":
            d = (self.dm.devices or {}).get(str(action.get("device", ""))) or {}
            if d.get("secure"):
                return (f"{d.get('name') or action.get('device')} is a secure "
                        f"device -- no automation presses it on its own")
            return None
        elif kind in ("ask", "display"):
            # The whole point of an ask is to reach a secure device
            # safely, so it is not screened against them -- it never
            # drives anything by itself. Nor does a display: writing a
            # sentence on glass opens no door.
            return None
        elif kind == "scene" and self.scenes is not None:
            scene = self.scenes.get(str(action.get("scene", "")))
            if scene is None:
                return "that scene does not exist"
            pairs = [(a.device_id, dict(a.state)) for a in scene.actions]
        for did, change in pairs:
            d = (self.dm.devices or {}).get(did)
            if d is None or not d.get("secure"):
                continue
            name = d.get("name") or did
            if self.secure_direction(did, change) == "secure":
                continue                            # fail-safe: allowed
            return (f"{name} is a secure device, and this would leave it "
                    f"LESS secure -- no automation does that on its own. "
                    f"A motion sensor must not hold a key to the house. "
                    f"Use 'ask' instead: the rule raises the question on "
                    f"your phone and your PIN answers it.")
        return None

    def note_human(self, did: str) -> None:
        """A person just commanded this device: it outranks the machine for
        the next override_s seconds, counted from the FIRST command. A repeat
        command inside an open window does not extend it; one after the
        window has closed starts a new one."""
        if not self._overridden(did):
            self._human[str(did)] = self.now()
        self._human_last[str(did)] = self.now()
        self._state_dirty = True

    def _overridden(self, did: str) -> bool:
        """The HOUSE window: counted from the first command."""
        ts = self._human.get(str(did))
        return ts is not None and (self.now() - ts) < self.override_s

    def _overridden_for(self, rule: Dict[str, Any], did: str) -> bool:
        """Does a person's own command still outrank THIS rule on this
        device? Per rule (2026-10-01): "always" = never; a window = counted
        from the person's last command; otherwise the house window."""
        mode, secs = override_spec(rule)
        if mode == "always":
            return False
        if mode == "window":
            ts = self._human_last.get(str(did))
            return ts is not None and (self.now() - ts) < secs
        return self._overridden(did)

    # ── the topic index: what each board's numbers mean ────────────────

    _SWITCHY = {"Switch", "Switch_n"}
    #: A Button is a MOMENT, not a state: with SetOption73 1 (the
    #: catalogue's button setup) Tasmota publishes each press as
    #: {"Button<n>":{"Action":"SINGLE"}}, never on a SWITCH topic.
    _BUTTONY = {"Button", "Button_n"}
    _RELAYY = {"Relay", "Relay_i"}
    #: Devices whose message is a MOMENT, not a state. They carry no Tasmota
    #: instance number (a board has one of each), so they index at 0 -- the
    #: shape the energy meter already used.
    #: BOTH READERS, not just the SPI one (2026-09-12). main.py's
    #: `_CARD_TYPES = ("rc522", "pn532")` decides what counts as a card
    #: reader for enrolment; this set decides what the engine indexes as a
    #: TAP. They disagreed, and the PN532 lost.
    #:
    #: A PN532 fell to the `else` below and was indexed as a ("reading",
    #: "PN532"), so its tap arrived, feed() tried `float("BA839D07")` on the
    #: UID, and the ValueError dropped it in silence. `last_tag` reads
    #: `_last[(did, "tag")]`, which was therefore never written -- so
    #: menu_enroll_card waited its full thirty seconds and said "No card was
    #: tapped", however many times the card was tapped.
    #:
    #: Worst of all, menu_enroll_card's own no-reader advice RECOMMENDS the
    #: PN532 -- "a board still on stock Tasmota takes the pins and never
    #: reads them, so use the PN532 there instead" -- so the hub sent people
    #: to the one reader it could not hear.
    #:
    #: The payloads are the same shape, which is why one line fixes it:
    #: {"RC522": {"UID": ...}} and {"PN532": {"UID": ..., "DATA": ...}}, and
    #: the tag branch reads UID out of either. test_card_reader.py holds
    #: this set and main's together so they cannot drift apart again.
    _TAG_TYPES = {"rc522", "pn532"}
    _IR_RECV_TYPES = {"ir_recv"}
    #: Slot components that are BUS WIRING, never a thing that speaks. Their
    #: names would otherwise be indexed as JSON keys that can never arrive.
    _BUS_WORDS = {"SPI", "I2C", "I²C", "UART", "SERIAL"}

    #: The one bus whose readings arrive under the BUS's name rather than
    #: the chip's. Tasmota's analog driver publishes everything on the ADC
    #: pins as {"ANALOG": {"Illuminance": 8}} / {"A0": 512} / {"Range": 45},
    #: whatever the thing on the other end of the wire is called -- so an
    #: LDR, a soil probe and an MQ-135 all speak as ANALOG. Every other bus
    #: lets the chip name itself, which is why this table has one row and
    #: is not expected to grow.
    _BUS_BLOCK = {"ADC": "ANALOG"}

    @staticmethod
    def _json_key(component_name: str) -> str:
        """The name Tasmota publishes a device's block under, derived from
        the template component's name: "RC522 CS" -> RC522, "AM2301" ->
        AM2301. Tasmota names the sensor block after the driver, and the
        catalogue's component names ARE the template's names, so the first
        word is the link -- no second table to keep in step."""
        return component_name.split()[0].upper() if component_name else ""

    def rebuild_index(self) -> None:
        """topic -> {(kind, n): device_id}. Rebuilt from the board records,
        so a rewire or a new board is picked up on the next build.

        WIDENED 2026-08-27 (her ruling: the trigger list is every registered
        device). It used to record switches, relays and the energy meter and
        DROP everything else -- so the card reader, the IR receiver and every
        temperature sensor published faithfully and were discarded on
        arrival. The card reader was the whole argument for putting this
        engine in the hub rather than HA, and it could not be a trigger in
        its own engine. Now every board item that SPEAKS is recorded, keyed
        by the name it speaks under.

        `n` is an instance number for the numbered kinds (switch, relay) and
        a JSON key string for the named ones (tag, reading) -- one table,
        because feed() looks each up by exactly the thing it has in hand.
        """
        if self.esp is None:
            return
        from esp_firmware import esphome_catalog as cat
        index: Dict[str, Dict[Tuple[str, Any], str]] = {}
        dropped: List[str] = []
        for slug in self.esp.list_nodes():
            rec = self.esp._read_board(slug) or {}
            topic = rec.get("topic")
            plan = self.esp._plan_for(slug)
            # A PART ON THE RECORD THAT IS NOT ON THE PLAN (2026-09-12).
            #
            # _plan_for builds the plan by replaying each recorded item
            # through BoardPlan.add, and add REFUSES for a dozen good
            # reasons -- a missing pin for one of its slots, a pin that
            # belongs to the module's own RAM, one pin pointed at twice,
            # a family that is already full. PlacementError subclasses
            # ValueError, so _plan_for catches it, drops that item, and
            # writes a line into `_plan_notes`.
            #
            # Two flows read those notes. THIS ONE DID NOT -- and it is the
            # one that decides what the hub can HEAR. So a card reader that
            # is on the board record but cannot be placed is found by
            # menu_enroll_card (which reads the record) and missing from
            # this index (which reads the plan): enrolment says "Hold the
            # card to the reader now", waits its full thirty seconds, and
            # reports "No card was tapped" -- while the tap arrives, misses
            # the index, and is dropped as an unknown key.
            #
            # _plan_for's own comment says it: "a device that silently
            # vanishes from the template while the record still lists it is
            # a lie in both directions". It was still silent here.
            # _plan_for resets _plan_notes first, so a missing record
            # (plan None) leaves none of the previous board's refusals.
            for note in self.esp._plan_notes:
                dropped.append(f"{slug}: {note}")
            if not topic or plan is None:
                continue
            table: Dict[Tuple[str, Any], str] = {}
            for item in plan.items:
                did = self.dm.board_device_id(slug, item.name)
                dev = cat.BY_ID.get(item.type_id)
                if dev is None:
                    continue
                if item.type_id in self.esp._ENERGY_TYPES:
                    table[("energy", 0)] = did
                if item.type_id in self._IR_RECV_TYPES:
                    table[("ir", 0)] = did
                # A device SPEAKS if it reports readings (domain "sensor")
                # or moments (the empty domain EVENT_ONLY wears). Things
                # that only ever take orders publish nothing, so indexing
                # them would only add keys no payload can match.
                speaks = dev.produces.domain in ("sensor", "")
                # ...unless its message already has a home above. The IR
                # receiver speaks, but in IrReceived, not in a block named
                # after its component -- and the meter speaks in ENERGY.
                # Without this they were ALSO indexed as generic readings,
                # which put "the remote eye" in the list of things with
                # numbers to compare against.
                if item.type_id in (self._IR_RECV_TYPES | self.esp._ENERGY_TYPES):
                    speaks = False
                # THE NAME A DEVICE PUBLISHES UNDER, when its catalogue
                # entry says one (DeviceType.publishes).
                declared = (dev.publishes or "").strip().upper()
                for slot in dev.slots:
                    comp = slot.component.name
                    n = item.instances.get(slot.key)
                    if n and comp in self._SWITCHY:
                        table[("switch", n)] = did
                    elif n and comp in self._BUTTONY:
                        table[("button", n)] = did
                    elif n and comp in self._RELAYY:
                        table[("relay", n)] = did
                    elif n and comp == "PWM" and len(dev.slots) == 1:
                        # A one-channel dimmer: driven as Channel<n> (see
                        # esp_builder._drive_target), so its level is read
                        # back under the same number. RGB lights (three
                        # channels to one device) are not read back here.
                        table[("pwm", n)] = did
                    elif speaks:
                        # A declared name wins over the derived one, and is
                        # NOT bus-filtered: the author named their own
                        # block, and "SPI" would be a strange thing to call
                        # it but it is not ours to refuse.
                        key = declared or self._json_key(comp)
                        if not key or (not declared and key in self._BUS_WORDS):
                            continue
                        # Tags and readings are told apart HERE, where the
                        # type is known, so feed() never has to guess what
                        # a payload means.
                        kind = ("tag" if item.type_id in self._TAG_TYPES
                                else "reading")
                        table[(kind, key)] = did
                # A DEVICE WITH NO PINS still names its block; the slot
                # loop above never ran for it.
                if speaks and declared and not dev.slots:
                    table[("reading", declared)] = did
                # A SENSOR ON A SHARED BUS NAMES ITSELF, NOT THE BUS
                # (2026-09-13). The loop above derives the block name from
                # the SLOT, which is right for a part with a pin of its own
                # -- "AM2301" -> AM2301, "SR04 Tri/TX" -> SR04. Every I2C
                # part's slots are called "I2C SDA1" and "I2C SCL1", so the
                # derivation yields "I2C", which is a bus word, and the
                # `continue` above dropped it. That is EVERY I2C SENSOR IN
                # THE CATALOGUE -- around twenty-five of them, bme280,
                # sht3x, bh1750, scd40 and the rest -- indexed under
                # nothing, so their readings arrived and were discarded as
                # an unknown key. Nothing said so, because an unknown key
                # is exactly what a message from another house looks like.
                #
                # Tasmota names such a block after the CHIP, and so does
                # this catalogue: `bme280` -> BME280, `scd40` -> SCD40.
                # Two more the slot cannot give: `ds18b20`'s slot is spelt
                # "DS18x20" (-> DS18X20) while Tasmota publishes DS18B20,
                # and the analog family publishes under ANALOG whatever the
                # part is called.
                #
                # ADDED, NOT SUBSTITUTED. A name nothing ever sends costs a
                # dictionary entry and matches nothing; a name that is
                # missing costs every reading that part will ever take. So
                # a part is indexed under all of them -- which also covers
                # a DHT22 that Tasmota calls AM2301 on one build and DHT22
                # on another.
                if speaks:
                    kind = ("tag" if item.type_id in self._TAG_TYPES
                            else "reading")
                    also = {str(item.type_id).upper()}
                    for slot in dev.slots:
                        bus = self._json_key(slot.component.name)
                        if bus in self._BUS_BLOCK:
                            also.add(self._BUS_BLOCK[bus])
                    if declared:
                        also.add(declared)
                    for name in also:
                        if name and (kind, name) not in table:
                            table[(kind, name)] = did
            index[topic] = table
        with self._lock:
            changed = set(index) != set(self._index)
            self._index = index
            # Kept, not just printed: menu_enroll_card and menu_learn_ir ask
            # for it by name when the thing they were waiting for never
            # arrived, so the person gets the reason instead of a shrug.
            said = self._plan_drops != dropped
            self._plan_drops = list(dropped)
        if dropped and said:
            for line in dropped:
                event("AUTO", f"NOT INDEXED -- {line}. It is on the board "
                              f"record but could not be placed, so the hub "
                              f"cannot hear it however well it is wired")
        # SAY WHAT IS BEING LISTENED FOR, once per change. "The readings
        # do not show" has three different causes -- the engine is not
        # connected, the board is not publishing, the board's topic is not
        # in any record -- and this line plus the 'hearing' lines below
        # tell them apart from the console (field report 2026-09-03).
        if changed:
            event("AUTO", "boards indexed: " + (", ".join(
                f"{t} ({len(tbl)} item(s))" for t, tbl in sorted(index.items()))
                or "none -- no board record has a topic"))

    def reading_seq(self) -> int:
        """How many values this engine has taken in. Strictly increasing.

        The home tiles used to be rebuilt on a timer and compared with the
        last push -- a full walk of every device every tick, whether or not
        anything had happened, which is why the interval was 15 seconds and
        why a relay somebody flipped could sit unreported for most of that.
        A caller that watches this number can do the expensive walk ONLY
        when there is something new, which is both faster to react and
        cheaper at rest.
        """
        return self._reading_seq

    def plan_drops(self) -> List[str]:
        """Parts that are on a board's RECORD but were refused by its PLAN,
        as of the last rebuild -- each already a sentence naming the part
        and the reason.

        This is the answer to "it is registered, it is wired, and the hub
        hears nothing": a refused part is in no index, so nothing it
        publishes is ever recognised, and the same refusal kept it out of
        the flashed template too. Empty is the normal case.
        """
        with self._lock:
            return list(self._plan_drops)

    def hears(self, did: str, kind: str) -> bool:
        """Is `did` in the index under `kind` -- would the hub recognise
        that kind of message from it at all?

        Asked BEFORE a flow settles in to wait. menu_enroll_card opened a
        thirty-second window for a reader the engine had no entry for,
        which can only ever end in "No card was tapped".
        """
        return kind in self._index_kinds(did)

    #: Which index kinds are moments (something HAPPENED) and which are
    #: numbers (something READS). The trigger vocabulary divides the same
    #: way, so this is the one place the mapping is written down.
    _EVENT_KINDS = ("switch", "relay", "tag", "ir", "button")
    _READING_KINDS = ("energy", "reading")

    def capabilities(self) -> Dict[str, List[str]]:
        """What this house can FEEL, in two buckets: 'event' ids (a pulse or
        a state the engine hears -- now including card taps and IR presses)
        and 'reading' ids (anything that reports numbers).

        Kept in its original shape because the spoken/CLI trigger picker
        reads it. The block builder wants something richer and asks
        askable() instead."""
        self.rebuild_index()
        ev, rd = set(), set()
        for table in self._index.values():
            for (kind, _n), did in table.items():
                if kind in self._EVENT_KINDS:
                    ev.add(did)
                elif kind in self._READING_KINDS:
                    rd.add(did)
        return {"event": sorted(ev), "reading": sorted(rd)}

    def _index_kinds(self, did: str) -> List[str]:
        """Every index kind this device is heard under, most specific first."""
        found = []
        for table in self._index.values():
            for (kind, _n), owner in table.items():
                if owner == did and kind not in found:
                    found.append(kind)
        return found

    def last_tag(self, did: str) -> Optional[Tuple[Any, float]]:
        """The last card this reader saw, and WHEN -- or None.

        The timestamp is the point. Card enrolment waits for a tap, and a
        UID left sitting here by a tap five minutes ago would otherwise be
        mistaken for the one it just asked for. Baseline the time, watch
        for a newer one.
        """
        return self._last.get((str(did), "tag"))

    #: HOW FAR A READING MUST MOVE TO BE WORTH REDRAWING, by reading name.
    #:
    #: A board asked to publish on every read speaks once a second, and a
    #: real sensor is never still: a DHT22 jitters 21.5 / 21.6 / 21.5 with
    #: nothing happening in the room, and a plain "has it changed" test
    #: calls every one of those a change. So the test is "has it MOVED",
    #: which is what `PowerDelta` does for the meter and what Tasmota's own
    #: documented delta rule does with two Vars -- the same idea, kept on
    #: this side where it costs no Var, no rule text, and can be retuned
    #: without going near a board.
    #:
    #: The numbers are the smallest change a person would act on, not the
    #: smallest the sensor can resolve. Half a degree is a draught; 0.1 is
    #: the sensor breathing.
    #: NOTHING THE BOARD ALREADY FILTERS IS IN HERE. The meter is the whole
    #: of that exception: `PowerDelta` means the board publishes an ENERGY
    #: block only when power has really moved, so every value that arrives
    #: is one the board decided was worth sending. A second, coarser test
    #: here would throw away a decision already made with better
    #: information -- the first draft of this table put 5 W on `power` and
    #: would have hidden a light bulb switching on. Energy fields are
    #: absent deliberately; do not add them.
    #:
    #: What is left is small on purpose. This is not "is the change
    #: interesting", it is "is this a change at all, or the last decimal
    #: breathing" -- roughly the sensor's own resolution, no more.
    _DEADBAND: Dict[str, float] = {
        "temperature": 0.2, "dewpoint": 0.2, "heatindex": 0.2,
        "humidity": 0.5, "pressure": 0.3,
        "illuminance": 2.0, "lux": 2.0, "uvindex": 0.1, "uvlevel": 0.1,
        "co2": 10.0, "carbondioxide": 10.0, "ecos": 10.0, "tvoc": 5.0,
        "distance": 0.5, "moisture": 1.0,
    }
    #: Anything not named above, energy included: only float noise in the
    #: last decimal is held back. A reading this table has never heard of
    #: is shown when it moves, which is the safe way round -- a band that
    #: is too wide hides real news, a band that is too narrow costs a
    #: redraw nobody notices.
    _DEADBAND_DEFAULT = 0.0009

    def _worth_showing(self, key: str, prev: Any, value: Any) -> bool:
        """Has this reading moved enough to be worth waking the phones?

        The FIRST value of anything always is. A non-number (a tag UID, an
        IR code) is a moment, and every moment counts. Only a number that
        has barely twitched is held back -- and held back only from the
        PUSH: it is still stored, still fed to every rule, still in the
        day's history. This decides what is worth LOOKING at.
        """
        if prev is None:
            return True
        if isinstance(value, bool) or isinstance(prev, bool):
            return prev != value
        if not isinstance(value, (int, float)) \
                or not isinstance(prev, (int, float)):
            return prev != value
        band = self._DEADBAND.get(str(key).lower(), self._DEADBAND_DEFAULT)
        return abs(float(value) - float(prev)) >= band

    def observed_keys(self, did: str) -> List[str]:
        """Reading names this device has actually published -- the honest
        source for a threshold picker, since a sensor names its own fields
        ('temperature', 'humidity') and only the device knows which."""
        skip = {"on", "tag", "ir", "button"}
        now = {k for (d, k) in list(self._last)
               if d == did and k not in skip}
        kept = self._seen_keys.setdefault(did, set())
        if not now <= kept:
            kept |= now
            self._state_dirty = True        # remembered for a restart
        return sorted(kept - skip)

    def key_words(self, key: str) -> str:
        """A reading's name in words, from the board's own spelling."""
        return self._key_words.get(str(key)) or str(key).replace("_", " ")

    def reading_keys(self, did: str) -> List[str]:
        """The readings this device OFFERS -- what the BUILDER may put in
        front of a person, for a trigger or for a display line.

        ONE ANSWER, ASKED TWICE. The WHEN dropdown and the display line's
        reading chips are both "which numbers does this device have", and
        until now they were computed in two places from two different
        rules: the dropdown took observed keys UNION the meter's four (see
        trigger_options), the chips took observed keys alone. On one
        screen that read as a contradiction, and she caught it -- a WHEN
        block offering "current rises above" on the kitchen meter, with
        "Nothing in the house is reporting a reading yet" under the
        display line directly below it (2026-09-11: "but then that
        reporting a reading and this already selected... any
        contradiction?"). Both statements were true of their own source
        and the pair of them was nonsense.

        Every reading the device has ever reported (kept across restarts).
        Nothing is assumed about what a meter sends: one not yet heard
        offers nothing yet.

        Not `level`: it lives on the state plane and would come out of
        `_fill` blank forever. A bare `{device}` shows a dimmer's level
        properly, through get_state.
        """
        return sorted(set(self.observed_keys(did)))

    def latest(self, did: str) -> Dict[str, Any]:
        """Every value this device has published, newest wins.

        The live state plane. These arrive over stat/# and tele/+/SENSOR and
        are the board's own word, so nothing here owes anything to Home
        Assistant. Read by DeviceManager.get_state through the state_reader
        seam.

        No age cutoff: a value is replaced only by a newer one. Whether the
        board is still there is the LWT's question, not this one.
        """
        did = str(did)
        # A COPY first: the MQTT thread writes _last. This one is read through
        # DeviceManager.get_state from every thread that asks for a state.
        return {k: v for (d, k), (v, _ts) in dict(self._last).items()
                if d == did}

    def trigger_options(self, did: str) -> List[Dict[str, Any]]:
        """What THIS device can be asked -- the WHEN block's second dropdown.

        Her ruling (2026-08-27): the first dropdown is every registered
        device, so capabilities stops being a filter and becomes this. Each
        option carries the trigger fields it would build, so the builder
        composes a rule without knowing the vocabulary.

        The numbers come from the device's own envelope -- the same
        "Behaves as: 0-100, moving 5 per step" line registration shows.
        Description and arithmetic, one source.
        """
        did = str(did)
        record = (self.dm.devices.get(did) or {}) if self.dm else {}
        kinds = self._index_kinds(did)
        out: List[Dict[str, Any]] = []

        # A PANEL IS NEVER A TRIGGER. It is a thing the house WRITES TO --
        # it reports nothing, ever, so no rule can wait on it. It was
        # offered anyway (its envelope is on/off and MQTT never hears it,
        # so the on/off branch below claimed it), which built a rule that
        # saved, validated, and could not fire for the rest of its life.
        # The app said so in amber under the dropdown and left Save lit,
        # and the same impossible rule was built three times over
        # (2026-09-01). A warning you can save past is not a guard: THE
        # PICKER IS THE ONLY GUARD, so the option stops existing.
        if any(p.get("slug") and self.dm is not None
               and self.dm.board_device_id(p["slug"], p.get("name", "")) == did
               for p in self._lcd_targets):
            return out

        if "tag" in kinds:
            out.append({"label": "a card is tapped", "kind": "event",
                        "trigger": {"kind": "event", "device": did,
                                    "value": "any"}})
            # THE LAST CARD THIS READER SAW, carried so the builder has
            # something to offer. A UID is sixteen hex characters nobody
            # knows by heart, and the only place it was ever printed was
            # the hub's log -- so "a specific card" meant "go and read a
            # log file, then type it back". Tap the card, open the
            # builder, it is already there.
            seen = self._last.get((did, "tag"))
            out.append({"label": "a specific card is tapped",
                        "kind": "event", "needs": "tag",
                        "last": "" if seen is None else str(seen[0]),
                        "trigger": {"kind": "event", "device": did}})
        if "ir" in kinds:
            out.append({"label": "any button is pressed", "kind": "event",
                        "trigger": {"kind": "event", "device": did,
                                    "value": "any"}})
        if "button" in kinds:
            for value, word in (("any", "is pressed"),
                                ("DOUBLE", "is pressed twice"),
                                ("HOLD", "is held down")):
                out.append({"label": word, "kind": "event",
                            "trigger": {"kind": "event", "device": did,
                                        "value": value}})

        # ON/OFF SHAPES. A device the engine hears as a pulse or a state --
        # and any registered device whose envelope is on/off. (askable()
        # only asks about devices the MQTT index hears; there is no other
        # ear any more.)
        # `or "on"`, NOT a get() default (2026-09-14). The loader writes an
        # explicit `state_key: None` into every record whose key is not a
        # string -- so the key is always PRESENT, the get() default could
        # never fire, and a record with no state_key came out as "not
        # binary". The consequence is not cosmetic: a device the MQTT index
        # does not hear (a legacy record, anything over Home Assistant) then
        # satisfies neither `heard_binary` nor `binary`, so the WHEN list
        # offered it NO on/off trigger at all -- and fell through to the
        # `level` fallback below, offering thresholds on a thing that has no
        # level. Every registration path in use today writes a real
        # state_key, so this was latent; it is still a line that said one
        # thing and did another.
        binary = (record.get("state_key") or "on") == "on"
        heard_binary = any(k in kinds for k in ("switch", "relay"))
        if heard_binary or (binary and not kinds):
            for value, word in (("on", "turns on"), ("off", "turns off")):
                out.append({"label": word, "kind": "event",
                            "trigger": {"kind": "event", "device": did,
                                        "value": value}})
            for value, word in (("on", "stays on for"), ("off", "stays off for")):
                out.append({"label": word, "kind": "persist", "needs": "minutes",
                            "trigger": {"kind": "persist", "device": did,
                                        "value": value}})
            out.append({"label": "is quiet for", "kind": "absent",
                        "needs": "minutes",
                        "trigger": {"kind": "absent", "device": did,
                                    "value": "on"}})

        # THRESHOLD SHAPES, one pair per reading the device offers.
        #
        # A METER ALWAYS OFFERS ALL FOUR (2026-09-01). The fallback was
        # `if not keys` -- the full set only when NOTHING had been heard
        # yet. But a PZEM sends power, voltage, current and total in ONE
        # ENERGY block, and a meter watching an idle house reports total
        # first and the rest as zero, which `observed_keys` reads as "only
        # energy_total exists". The WHEN list then offered exactly one
        # trigger, on the one reading that never moves, and the three that
        # do were unreachable. They are not speculative: the block carries
        # them every time.
        #
        # That union LIVES IN reading_keys now, because the display line's
        # chips are the same question and were answering it differently.
        # See reading_keys.
        # Nothing heard yet: the reading the catalogue says this part
        # publishes (a DHT's "temperature"). Never `level` -- no MQTT
        # payload is read as `level`, so a threshold on it could not fire;
        # nor a card reader's "tag", which is a UID and not a number.
        keys = self.reading_keys(did)
        if not keys and record.get("reading_key") not in (None, "", "tag"):
            keys = [str(record["reading_key"])]
        bounds = {k: record.get(k) for k in
                  ("range_min", "range_max", "step")
                  if record.get(k) is not None}
        for key in keys:
            for edge, word in (("above", "rises above"), ("below", "falls below")):
                # `edge` says WHICH side the number goes on. The two options
                # otherwise carried identical trigger dicts and differed only
                # in their label, so a builder filling in the number had no
                # field telling it whether to write `above` or `below`.
                out.append({"label": f"{key} {word}", "kind": "threshold",
                            "needs": "number", "key": key, "bounds": bounds,
                            "edge": edge,
                            "trigger": {"kind": "threshold", "device": did,
                                        "key": key}})
            # AND "WHENEVER IT CHANGES" (2026-09-14), offered for every
            # reading a threshold is offered for, because it answers the
            # question a threshold cannot: a panel that should TRACK a
            # meter rather than react to it crossing a line. `needs` is
            # nothing -- the engine's own deadband decides what counts as
            # movement unless the rule names a `by`.
            out.append({"label": f"{key} changes", "kind": "change",
                        "key": key, "bounds": bounds,
                        "trigger": {"kind": "change", "device": did,
                                    "key": key}})
        return out

    def askable(self) -> Dict[str, Dict[str, Any]]:
        """Every registered device, what it can be asked, and HOW it is
        heard -- the WHEN half of the builder's payload.

        `heard` is "mqtt" for every device listed. A device the MQTT index
        does not hear is NOT listed: nothing can tell the hub it changed
        (the HA event ear is gone -- Home Assistant is for integration
        devices only), so a trigger on it could never fire, and the picker
        is the only guard.
        """
        self.rebuild_index()
        out: Dict[str, Dict[str, Any]] = {}
        devices = dict(self.dm.devices) if self.dm else {}
        for did, record in devices.items():
            kinds = self._index_kinds(did)
            if not kinds:
                continue
            options = self.trigger_options(did)
            # NOTHING TO LISTEN FOR, SO NOT IN THE LIST. A device with no
            # trigger options cannot start a rule, and offering it means
            # offering a dead end -- the second dropdown reads "nothing to
            # listen for" and the person has already spent a choice
            # getting there. Same law as the panel guard in
            # trigger_options: the picker is the guard.
            if not options:
                continue
            out[did] = {
                "name": record.get("name") or did,
                "room": record.get("room", ""),
                "description": record.get("description", ""),
                "secure": bool(record.get("secure")),
                "heard": "mqtt",
                "options": options,
            }

        return out

    # ── what each device draws, learned from the main meter ─────────────
    #
    # When a device switches on or off, the main meter's reading jumps by
    # about what that device draws. Each clean jump is one sample; the
    # median of the last few is the answer. Nothing here knows a meter by
    # its name: a board's meter is the catalogue's energy part, and its
    # fields are Tasmota's own ENERGY protocol; a Home Assistant sensor says
    # its own unit.

    #: Tasmota's ENERGY fields that are power (W) and current (A): the
    #: protocol's own names (feed() keeps them lowercased).
    _ENERGY_DRAW_FIELDS = {"Power": "power", "Current": "current"}
    #: Home Assistant's unit symbols for power and current, and what one of
    #: each is in the unit a draw is kept in.
    _HA_DRAW_UNITS = {"W": ("power", 1.0), "kW": ("power", 1000.0),
                      "A": ("current", 1.0), "mA": ("current", 0.001)}
    #: What a draw is kept and shown in.
    _DRAW_UNIT = {"power": "W", "current": "A"}
    #: Jumps kept per device, and how many before a draw is shown.
    DRAW_SAMPLES_KEPT = 10
    DRAW_MIN_SAMPLES = 3

    def _ha_unit(self, did: str) -> str:
        """The unit Home Assistant itself attached to this device's state
        (never one typed on a form); "" when it gave none."""
        unit_of = getattr(self.dm, "ha_unit", None)
        if not callable(unit_of):
            return ""
        try:
            return str(unit_of(did) or "").strip()
        except Exception:                           # noqa: BLE001
            return ""

    def _meter_keys(self, did: str) -> List[Dict[str, Any]]:
        """This device's readings that are power or current, as the meter
        itself says: [{"key", "measures", "scale"}] (scale: one of it in W or
        A). Empty: not a meter."""
        did = str(did)
        record = ((self.dm.devices or {}).get(did) or {}) \
            if self.dm is not None else {}
        if "energy" in self._index_kinds(did):
            fields = {f.lower(): m for f, m in self._ENERGY_DRAW_FIELDS.items()}
            return [{"key": k, "measures": fields[k], "scale": 1.0}
                    for k in self.observed_keys(did) if k in fields]
        if record.get("ha_entity_id") and record.get("transport") != "mqtt":
            got = self._HA_DRAW_UNITS.get(self._ha_unit(did))
            return ([{"key": "level", "measures": got[0], "scale": got[1]}]
                    if got else [])
        return []

    def meter_candidates(self) -> Dict[str, List[Dict[str, Any]]]:
        """Every device with a power or current reading: id -> _meter_keys."""
        devices = list((self.dm.devices or {}) if self.dm is not None else {})
        out: Dict[str, List[Dict[str, Any]]] = {}
        for did in devices:
            keys = self._meter_keys(did)
            if keys:
                out[did] = keys
        return out

    def main_meter(self) -> Optional[Dict[str, Any]]:
        """The meter whose jumps are learned: {device, key, measures}, or
        None. The person's choice; else, unless they said none, the one
        meter in the house when there is exactly one (its power, else its
        current). With several, none until the person picks one."""
        devices = (self.dm.devices or {}) if self.dm is not None else {}
        chosen = self._main_meter
        if chosen is not None and chosen["device"] in devices:
            return dict(chosen)
        if self._main_meter_off:
            return None
        known: Dict[str, List[Dict[str, Any]]] = {}
        for did, keys in self.meter_candidates().items():
            told = [k for k in keys if k["measures"]]
            if told:
                known[did] = told
        if len(known) != 1:
            return None
        did, keys = next(iter(known.items()))
        pick = next((k for k in keys if k["measures"] == "power"), keys[0])
        return {"device": did, "key": pick["key"],
                "measures": pick["measures"], "auto": True}

    def meter_info(self, did: str, main: Optional[Dict[str, Any]] = None
                   ) -> Optional[Dict[str, Any]]:
        """For the device list: {"keys": [{key, measures, words}], "main"},
        or None when this device is not a meter. `main`: main_meter() when
        the caller already has it ({} for none)."""
        did = str(did)
        keys = self._meter_keys(did)
        if not keys:
            return None
        if main is None:
            main = self.main_meter()
        is_main = bool(main) and main.get("device") == did
        return {"keys": [{"key": k["key"], "measures": k["measures"],
                          "words": self.key_words(k["key"])} for k in keys],
                "main": is_main}

    def _meter_scale(self, meter: Dict[str, Any]) -> float:
        for k in self._meter_keys(meter["device"]):
            if k["key"] == meter["key"]:
                return float(k["scale"])
        return 1.0

    def set_main_meter(self, did: str, key: str,
                       measures: str) -> Optional[str]:
        """The person picks the main meter. Returns why not, or None.
        Another meter forgets every draw learned: its numbers are of
        another circuit, or in another unit."""
        did, key, measures = str(did), str(key), str(measures).strip().lower()
        record = ((self.dm.devices or {}).get(did)) if self.dm is not None \
            else None
        if record is None:
            return "That device is no longer here."
        name = str(record.get("name") or did)
        if measures not in self._DRAW_UNIT:
            return "Say power or current."
        keys = {k["key"]: k for k in self._meter_keys(did)}
        if key not in keys:
            return f"{name[:1].upper()}{name[1:]} has no such power or current reading."
        known = keys[key]["measures"]
        if known is not None and known != measures:
            return f"That reading is {known}, not {measures}."
        choice = {"device": did, "key": key, "measures": measures}
        with self._lock:
            if self._draw_meter != choice:
                self._draws.clear()
                self._draw_meter = None
            self._draw_pending.clear()
            self._main_meter = choice
            self._main_meter_off = False
        self._save_state()
        event("AUTO", f"main meter: {did} ({key}, {measures})")
        return None

    def clear_main_meter(self) -> None:
        """No main meter, and none chosen on its own: nothing is learned,
        and what was learned is forgotten."""
        with self._lock:
            self._main_meter = None
            self._main_meter_off = True
            self._draws.clear()
            self._draw_meter = None
            self._draw_pending.clear()
        self._save_state()
        event("AUTO", "main meter: none")

    def forget_draw(self, did: str) -> bool:
        """Forget what one device draws. False when nothing was known."""
        with self._lock:
            had = self._draws.pop(str(did), None)
            self._draw_pending.pop(str(did), None)
        if had:
            self._save_state()
        return bool(had)

    @staticmethod
    def _round_draw(value: float) -> Any:
        """Two significant figures: about 2000 W, 63 W, 0.27 A."""
        if not _finite(value) or value <= 0:
            return 0
        out = round(value, 1 - int(math.floor(math.log10(value))))
        return int(out) if float(out).is_integer() else out

    def draw_of(self, did: str) -> Optional[Dict[str, Any]]:
        """{"value", "unit", "samples"} once DRAW_MIN_SAMPLES clean jumps
        are known: the median, so one odd jump does not move it."""
        with self._lock:
            samples = sorted(self._draws.get(str(did)) or ())
            meter = self._draw_meter
        if len(samples) < self.DRAW_MIN_SAMPLES or not meter:
            return None
        n = len(samples)
        mid = samples[n // 2] if n % 2 else (samples[n // 2 - 1]
                                             + samples[n // 2]) / 2.0
        return {"value": self._round_draw(mid),
                "unit": self._DRAW_UNIT[meter["measures"]], "samples": n}

    def _check_reading_now(self, rid: str) -> None:
        """A reading automation, just saved or turned on, acts AT ONCE on the
        latest reading the hub has (however old) when it is already past
        the line -- not only on the next crossing (2026-11-07). A fresh
        reading is asked for at the same moment, so a changed value corrects
        it within seconds. Not when the device is offline or its meter is
        not answering: that last value may be the driver's zeros."""
        rule = next((r for r in list(self.rules) if r.get("id") == rid), None)
        if rule is None or not rule.get("enabled", True) or \
                rule.get("_invalid"):
            return
        t = rule.get("trigger") or {}
        if t.get("kind") != "threshold":
            return
        did, key = str(t.get("device", "")), str(t.get("key", ""))
        topic = next((tp for tp, table in self._index.items()
                      if did in table.values()), None)
        if self._pub is not None and topic and topic not in self._offline:
            try:
                self._pub.publish(f"cmnd/{topic}/Status", "10")
            except Exception:                       # noqa: BLE001
                pass
        if self.is_offline(did) or did in self._meter_silent:
            self._note(rule, "set -- waiting for a real reading (the device "
                             "is not answering right now)")
            return
        value = self.latest(did).get(key)
        if value is None:
            got = self._last.get((did, key))
            value = None if got is None else got[0]
        try:
            reading = float(value)
        except (TypeError, ValueError):
            self._note(rule, "set -- waiting for its first reading")
            return
        above = t.get("above")
        try:
            line = float(above if above is not None else t["below"])
        except (KeyError, TypeError, ValueError):
            return
        hot = reading > line if above is not None else reading < line
        self._threshold_side[(rid, "side")] = hot
        if hot:
            self._note(rule, f"the reading is already "
                             f"{'above' if above is not None else 'below'} "
                             f"{line:g} ({reading:g}) -- acting now")
            self._fire(rule)

    def _meter_topic(self, did: str) -> Optional[str]:
        """The board topic of a meter the catalogue knows (its energy part)."""
        for topic, table in self._index.items():
            if table.get(("energy", 0)) == did:
                return topic
        return None

    def _draw_watch(self, did: str, key: str, value: Any,
                    prev: Optional[Tuple[Any, float]], ts: float,
                    source: str) -> None:
        """From _observe, for every fact: a device's on/off CHANGE (switched
        by the hub or seen switching), or a reading that may be the meter's
        "after"."""
        if key == "on":
            if isinstance(value, bool) and source != "retained" \
                    and prev is not None and prev[0] != value:
                self._draw_switched(did, value, ts)
            return
        if self._draw_pending and source != "retained":
            self._draw_reading(did, key, value, ts)

    def _draw_switched(self, did: str, on: bool, ts: float) -> None:
        """A device switched: start measuring its jump, when the meter's
        "before" is fresh and nothing else switched lately."""
        window = _config.DRAW_WINDOW_SECONDS
        with self._lock:
            # Every jump still waiting is spoilt: the meter sees both.
            self._draw_pending.clear()
            earlier = [t for (t, _d) in self._onoff_log]
            self._onoff_log.append((ts, did))
            while self._onoff_log and self._onoff_log[0][0] < \
                    ts - window - _config.METER_FRESH_SECONDS:
                self._onoff_log.popleft()
        meter = self.main_meter()
        if meter is None or meter["device"] == did:
            return                          # the meter itself is not learned
        if meter["device"] in self._meter_silent:
            return
        got = self._last.get((meter["device"], meter["key"]))
        if got is None or isinstance(got[0], bool) or not _finite(got[0]) \
                or not 0 <= ts - got[1] <= _config.METER_FRESH_SECONDS:
            return                          # no fresh "before"
        # Nothing else switched since the "before", nor in the window.
        quiet_from = min(got[1], ts - window)
        if any(t >= quiet_from for t in earlier):
            return
        with self._lock:
            self._draw_pending[did] = {
                "on": on, "at": ts, "before": float(got[0]),
                "meter": {k: meter[k] for k in ("device", "key", "measures")},
                "scale": self._meter_scale(meter), "asked": False}

    def _draw_reading(self, did: str, key: str, value: Any,
                      ts: float) -> None:
        """A reading arrived: the "after" of a jump waiting on it, once the
        meter has had DRAW_SETTLE_SECONDS to catch up."""
        took: List[Tuple[str, Dict[str, Any]]] = []
        with self._lock:
            for dev, p in list(self._draw_pending.items()):
                if (p["meter"]["device"], p["meter"]["key"]) != (did, key):
                    continue
                age = ts - p["at"]
                if age < _config.DRAW_SETTLE_SECONDS:
                    continue
                del self._draw_pending[dev]
                if age <= _config.DRAW_WINDOW_SECONDS:
                    took.append((dev, p))
        for dev, p in took:
            if did in self._meter_silent or isinstance(value, bool) \
                    or not _finite(value):
                continue
            jump = float(value) - p["before"]
            # Up when switched on, down when off; never nothing.
            if jump == 0 or (jump > 0) is not p["on"]:
                continue
            self._draw_learn(dev, abs(jump) * p["scale"], p["meter"])

    def _draw_learn(self, did: str, amount: float,
                    meter: Dict[str, Any]) -> None:
        with self._lock:
            if self._draw_meter != meter:
                # Samples of another meter are another circuit or unit.
                self._draws.clear()
                self._draw_meter = dict(meter)
            samples = self._draws.setdefault(did, [])
            samples.append(round(amount, 4))
            del samples[:-self.DRAW_SAMPLES_KEPT]
            self._state_dirty = True
        event("AUTO", f"{did}: switching it moved the main meter by "
                      f"{amount:g} {self._DRAW_UNIT[meter['measures']]}")

    def _draw_tick(self) -> None:
        """Every tick: a jump with no "after" in time is dropped; a board
        meter is asked for a fresh reading (Status 10) once it has had
        DRAW_SETTLE_SECONDS to catch up."""
        now = self.now()
        ask: List[str] = []
        with self._lock:
            for dev, p in list(self._draw_pending.items()):
                age = now - p["at"]
                if age > _config.DRAW_WINDOW_SECONDS:
                    del self._draw_pending[dev]
                elif not p["asked"] and age >= _config.DRAW_SETTLE_SECONDS:
                    p["asked"] = True
                    topic = self._meter_topic(p["meter"]["device"])
                    if topic and topic not in ask:
                        ask.append(topic)
        client = self._pub
        for topic in ask:
            if client is None or topic in self._offline:
                continue
            try:
                client.publish(f"cmnd/{topic}/Status", "10")
            except Exception:                       # noqa: BLE001
                pass

    # ── observations: MQTT in, normalised facts out ─────────────────────

    _TOPIC_RX = re.compile(r"^(stat|tele)/([^/]+)/([A-Za-z0-9_]+)$")

    @staticmethod
    def _add_level(add: Callable[..., None], n: int, value: Any) -> None:
        """One PWM channel's percentage -> the dimmer's level and on."""
        if isinstance(value, bool):
            return
        try:
            level = float(value)
        except (TypeError, ValueError):
            return
        if not math.isfinite(level):
            return
        add("pwm", n, "level", level)
        add("pwm", n, "on", level > 0)

    @staticmethod
    def _numbered(word: str, stem: str) -> Optional[int]:
        """"POWER2" -> 2, "POWER" -> 1 (Tasmota's single-relay spelling),
        anything else ("POWERONSTATE") -> None."""
        rest = word[len(stem):]
        if not word.startswith(stem) or not (rest.isdigit() or not rest):
            return None
        return int(rest or "1")

    def feed(self, topic: str, payload: str,
             *, retained: bool = False) -> List[Tuple[str, str, Any]]:
        """One MQTT message -> zero or more (device_id, key, value) facts,
        each already pushed through the rule evaluator. Returns them so a
        test can SEE what the engine made of a message.

        `retained` is the BROKER'S OWN mark, taken straight off the received
        message, and it means one specific thing: this arrived because the
        hub SUBSCRIBED just now, not because it HAPPENED just now. MQTT
        3.1.1 is strict about it -- a broker sets the flag only on messages
        it replays into a new subscription, and clears it on everything
        forwarded live to a subscription that already existed, whatever the
        publisher asked for. So it is the one reliable way to tell "here is
        the last thing that was true" from "this just happened", and
        _note_presence needs exactly that distinction.

        Defaults False, which treats an unmarked caller as live. That is the
        safe way round: the worst it can cost is one extra panel wake, and
        the cost of the other way round is a panel left showing rubbish.
        """
        m = self._TOPIC_RX.match(str(topic))
        if not m:
            return []
        prefix, board_topic, tail = m.groups()
        tail_up = tail.upper()
        text = payload.decode("utf-8", "replace") \
            if isinstance(payload, (bytes, bytearray)) else str(payload)
        # PRESENCE FIRST, BEFORE THE INDEX. A board that carries only a
        # display and a card reader has an EMPTY device table -- nothing on
        # it reports a reading -- and the stranger check below would drop
        # its LWT and call it unknown. Presence needs the topic and nothing
        # else, so it is read here where every board has one.
        if tail_up == "LWT":
            self._note_presence(board_topic, text, retained=retained)
            return []
        if tail_up in ("STATE", "RESULT") and '"Uptime"' in text:
            # A board's state carries its clock: wrong (1970, no internet)
            # -> set from the hub's.
            self._note_board_time(board_topic, text)
            if prefix == "tele" and tail_up == "STATE":
                return []           # subscribed for the clock only
        if prefix == "tele" and tail_up == "INFO1":
            # A board announcing itself (tele/<board>/INFO1, sent once at boot). The ONE thing looked for is the
            # word "safeboot" in its version: the recovery partition, which
            # joins the Wi-Fi, connects to the broker, reports Online and
            # answers "Unknown" to everything else -- a bricked board that
            # looks alive (Kitchen, 2026-09-09 and again 2026-09-10). It is
            # answered with the two commands that heal it, from the hub's
            # own firmware server (hub/ota_server.py), rate-limited there.
            self._note_info1(board_topic, text)
            return []
        if prefix == "tele" and tail_up == "INFO3":
            # Why the board just booted (sent once, at its first connection).
            self._note_boot_reason(board_topic, text)
            return []
        if prefix == "stat" and tail_up == "RESULT":
            # Two things read off a RESULT before the index, because a
            # board that carries only a display and a card reader has an
            # EMPTY device table and would be dropped below.
            #
            # 1. The OTHER way safeboot shows itself: INFO1 goes out once,
            #    on the board's first broker connection, so a board already
            #    sitting in safeboot when this hub restarts never announces
            #    again. But every command it cannot run is answered
            #    {"Command":"Unknown","Input":"<the command>"}, and Backlog /
            #    Status / Power / SetOption are core commands every full
            #    Tasmota has -- only the safeboot (minimal) build lacks
            #    them. This is the fingerprint the cable path uses too.
            # 2. A panel whose settings are gone: {"DisplayModel":0} from a
            #    board the hub owns a screen on. See _note_display_model.
            try:
                result = json.loads(text)
            except ValueError:
                result = None
            if isinstance(result, dict):
                self._note_unknown(board_topic, result)
                self._note_display_model(board_topic, result)
                # 3. What is actually on the board's I2C bus, in answer to
                #    the `I2Cscan` the panel tick sends. See _note_i2c_scan.
                self._note_i2c_scan(board_topic, result)
        table = self._index.get(board_topic)
        if board_topic not in self._heard_topics:
            self._heard_topics.add(board_topic)
            # Once per stranger: a board that publishes under a topic no
            # record carries is the classic "renamed in the Tasmota
            # console" fault, and it deserves a line rather than silence.
            # (A known board whose table is merely EMPTY -- a panel and
            # nothing that speaks -- is not a stranger, and is not called
            # one.)
            event("AUTO", f"hearing {board_topic!r} but no board record "
                          f"uses that topic -- ignored (its record says "
                          f"a different topic, or it is not ours)"
                  if table is None else
                  f"hearing board {board_topic!r} ({tail})")
        if not table:
            return []
        obs: List[Tuple[str, str, Any]] = []

        def add(kind: str, n: Optional[int], key: str, value: Any) -> None:
            did = table.get((kind, n))
            if did:
                obs.append((did, key, value))

        # Only POWER / POWER<n> and SWITCH<n>: any other stat topic starting
        # with those letters would make int() raise and drop the message.
        if prefix == "stat" and self._numbered(tail_up, "POWER"):
            add("relay", self._numbered(tail_up, "POWER"), "on",
                text.strip().upper() == "ON")
        elif prefix == "stat" and self._numbered(tail_up, "SWITCH"):
            n = self._numbered(tail_up, "SWITCH")
            try:
                raw = json.loads(text)
                if isinstance(raw, dict):   # {"Switch1":{"Action":"ON"}}
                    raw = next((v for k, v in raw.items()
                                if str(k).upper() == tail_up), raw)
                action = raw.get("Action") if isinstance(raw, dict) else raw
            except Exception:                       # noqa: BLE001
                action = text
            state = self._switch_action(board_topic, n, action)
            if state is not None:
                add("switch", n, "on", state)
        elif tail_up in ("RESULT", "SENSOR", "STATE") or (
                prefix == "stat" and tail_up == "STATUS10") or (
                prefix == "stat" and self._numbered(tail_up, "BUTTON")):
            # (stat/<board>/BUTTON<n> is a button press on a board with
            # SetOption4 1; the payload is the same JSON as on RESULT.)
            try:
                data = json.loads(text)
            except Exception:                       # noqa: BLE001
                return []
            if tail_up == "STATUS10" and isinstance(data, dict):
                # The answer to `Status 10` (asked after a switch, _draw_tick):
                # {"StatusSNS": {...}}, read exactly as tele/SENSOR is.
                data = data.get("StatusSNS")
            if not isinstance(data, dict):
                return []
            for key, value in data.items():
                ku = str(key).upper()
                if ku.startswith("SWITCH") and ku[6:].isdigit():
                    act = value.get("Action") if isinstance(value, dict) \
                        else value
                    n = int(ku[6:])
                    state = self._switch_action(board_topic, n, act)
                    if state is not None:
                        add("switch", n, "on", state)
                elif ku.startswith("BUTTON") and ku[6:].isdigit():
                    # A PRESS, as its action word: SINGLE, DOUBLE ... PENTA,
                    # HOLD. An event like a tap, never an on/off state.
                    act = value.get("Action") if isinstance(value, dict) \
                        else value
                    act = str(act or "").strip().upper()
                    if act:
                        add("button", int(ku[6:]), "button", act)
                elif ku.startswith("POWER"):
                    add("relay", self._numbered(ku, "POWER"), "on",
                        str(value).strip().upper() == "ON")
                elif ku == "CHANNEL" and isinstance(value, list):
                    # A DIMMER'S LEVEL, as the board itself reports it:
                    # {"Channel":[40,0,...]} in STATE and in the answer to a
                    # Channel command, one percentage per PWM channel. Read
                    # so "brighten it" steps from where the light really is.
                    # (Both shapes are read, this list and "Channel<n>"
                    # below, because which one a given Tasmota sends has not
                    # been seen on her boards yet.)
                    for i, v in enumerate(value, start=1):
                        self._add_level(add, i, v)
                elif ku.startswith("CHANNEL") and ku[7:].isdigit():
                    self._add_level(add, int(ku[7:]), value)
                elif ku == "ENERGY" and isinstance(value, dict):
                    # EVERY FIELD THE METER SENDS (her ruling, 2026-09-01:
                    # "let the hub read every value any sensor outputs").
                    # The old four-name list silently dropped
                    # ApparentPower, ReactivePower, Factor, Frequency,
                    # Today, Yesterday and Period -- numbers the meter had
                    # already measured, published, and paid for in wiring.
                    # A whitelist here meant the hub could only ever know
                    # what someone had thought to type into it.
                    #
                    # SAME RULE AS EVERY OTHER SENSOR BLOCK, below: numeric
                    # fields only, named by what the board itself calls
                    # them, lowercased. `Total` alone keeps its old name --
                    # saved rules, stored state and the statistics screen
                    # already say `energy_total`, and renaming it would
                    # break rules that work today.
                    # IS A METER ANSWERING AT ALL? (2026-09-12)
                    #
                    # Tasmota builds the ENERGY block from the DRIVER, not
                    # from the meter: a PZEM that is configured and wired
                    # to nothing still publishes a complete block, every
                    # field a confident 0.000. So "0 W" on the app and "no
                    # meter connected" looked identical, and the app showed
                    # a tidy zero for three days.
                    #
                    # `Address` is the tell. The driver polls each PZEM by
                    # its Modbus address and writes the address it got an
                    # answer from; an address of 0 means nothing replied.
                    # Paired with a voltage of 0 it is conclusive -- a live
                    # meter reports mains voltage even under no load.
                    #
                    # Her board, 12 Sep: "Address":[0,0,0,0,0,0,0] with
                    # "Voltage":[0,0,0,0,0,0,0], while its boot log said
                    # `NRG: Init driver 5`. The driver was up. The meter
                    # was not there.
                    def _first(raw):
                        """One number out of a field that may be a LIST.
                        Tasmota's energy driver reports PER SLOT -- seven
                        of them -- so every value arrives as an array on a
                        multi-slot build. float() on a list raises, and the
                        `except` below dropped EVERY reading in silence: a
                        working meter on this firmware would have shown
                        nothing in the app either. Slot 0 is the meter."""
                        if isinstance(raw, (list, tuple)):
                            return raw[0] if raw else None
                        return raw

                    addresses = value.get("Address")
                    volts = _first(value.get("Voltage"))
                    silent = False
                    if addresses is not None:
                        addr = addresses if isinstance(addresses, (list, tuple)) \
                            else [addresses]
                        try:
                            silent = (all(float(a) == 0 for a in addr)
                                      and float(volts or 0) == 0)
                        except (TypeError, ValueError):
                            silent = False
                    did = table.get(("energy", 0))
                    if did and silent:
                        # Shown as sent; rules ignore it (_observe). Warned
                        # only if it lasts: right after a boot the driver
                        # sends zeros until it has found the meter.
                        began = self._meter_silent.setdefault(did, self.now())
                        if did not in self._meter_silent_said and \
                                self.now() - began >= self.METER_SILENT_WARN_S:
                            self._meter_silent_said.add(did)
                            event("AUTO", f"{did}: the energy driver is "
                                          f"running but NO METER IS "
                                          f"ANSWERING -- every address is 0 "
                                          f"and voltage is 0. Its readings "
                                          f"are the driver's zeros, not "
                                          f"measurements. FIRST CHECK ITS "
                                          f"MAINS: a PZEM-004T draws the "
                                          f"power for its own processor "
                                          f"from the AC line it measures, "
                                          f"and the 5V on its serial header "
                                          f"only feeds the isolated side -- "
                                          f"so one with no live AC does not "
                                          f"answer at all, exactly like a "
                                          f"broken wire. After that, its "
                                          f"Tx/Rx and its Modbus address. "
                                          f"Rules ignore it until it answers")
                    elif did and did in self._meter_silent:
                        self._meter_silent.pop(did, None)
                        if did in self._meter_silent_said:
                            self._meter_silent_said.discard(did)
                            event("AUTO", f"{did}: the meter is answering "
                                          f"again ({volts} V) -- rules use "
                                          f"its readings again")
                    for field, raw in value.items():
                        if isinstance(raw, bool):
                            continue
                        raw = _first(raw)
                        out = ("energy_total" if str(field) == "Total"
                               else str(field).lower())
                        if out not in self._key_words:
                            self._key_words[out] = re.sub(
                                r"(?<=[a-z])(?=[A-Z])", " ",
                                str(field)).lower() \
                                if out != "energy_total" else "energy total"
                        try:
                            add("energy", 0, out, float(raw))
                        except (TypeError, ValueError):
                            # A timestamp (TotalStartTime) or a chip name
                            # is not a reading. Not an error -- there is
                            # simply no number in it.
                            pass
                elif ku == "IRRECEIVED" and isinstance(value, dict):
                    # A button on a remote, heard. The code IS the fact --
                    # "there is no current infrared", as the catalogue puts
                    # it, so this is a moment and never a state. Data when
                    # the protocol is known, Hash when it is not.
                    code = value.get("Data") or value.get("Hash") or ""
                    if self._ir_echo(value):
                        # The hub's own send, heard back: not a press.
                        event("AUTO", f"{board_topic}: heard the hub's own "
                                      f"infrared send -- ignored")
                    elif str(code).strip():
                        add("ir", 0, "ir", str(code).strip())
                else:
                    # EVERYTHING ELSE THAT SPEAKS, found by the name it
                    # speaks under: {"RC522": {"UID": ...}} for a tap,
                    # {"AM2301": {"Temperature": 24.5, ...}} for a reading.
                    tag_did = table.get(("tag", ku))
                    read_did = table.get(("reading", ku))
                    if read_did is None and tag_did is None and "-" in ku:
                        # TWO OF THE SAME CHIP ON ONE WIRE. Tasmota numbers
                        # them in the block name -- with three probes on a
                        # 1-Wire pin it publishes DS18B20-1, DS18B20-2,
                        # DS18B20-3, "where the number is practically
                        # random" (its own docs). One probe is plain
                        # DS18B20, so a board worked until the day a second
                        # probe was added and then reported nothing at all.
                        #
                        # They share one board item, so they resolve to one
                        # device: the suffix says WHICH probe, and this hub
                        # has no separate device to give it to. The reading
                        # that lands is whichever spoke last. Better than
                        # the alternative, which was silence.
                        stem = ku.split("-", 1)[0]
                        read_did = table.get(("reading", stem))
                        tag_did = table.get(("tag", stem))
                    if tag_did is not None and isinstance(value, dict):
                        # The UID is a NAME, not a proof (the RFID design
                        # law, 2026-08-16): it says who is here, and law 2
                        # -- secure devices are never automation actions --
                        # is what keeps a tap from being a key.
                        uid = str(value.get("UID", "")).strip()
                        if uid:
                            obs.append((tag_did, "tag", uid))
                    elif read_did is not None:
                        # A sensor block is a dict of numbers; a bare
                        # scalar is the one-field case. Non-numeric fields
                        # (a unit, a chip type) are simply not readings.
                        fields = (value.items() if isinstance(value, dict)
                                  else [(ku, value)])
                        for field, raw in fields:
                            if isinstance(raw, bool):
                                continue
                            try:
                                obs.append((read_did, str(field).lower(),
                                            float(raw)))
                            except (TypeError, ValueError):
                                continue
                            # THE BOARD'S OWN SPELLING, kept (2026-09-13).
                            #
                            # Everything above lowercases, because inside
                            # the hub a reading is "temperature" whoever
                            # sent it. But a Tasmota RULE has to name the
                            # field exactly as Tasmota spells it --
                            # `BME280#Temperature`, `SCD40#CarbonDioxide`
                            # -- and capitalising the lowercase name back
                            # would be a guess that is right for
                            # temperature and wrong for CO2.
                            #
                            # So it is not guessed and not tabulated: the
                            # board says it, and this remembers it. That
                            # is what lets the rule writer work for a
                            # sensor nobody has typed into the catalogue,
                            # including somebody's own .ino -- the same
                            # reason `publishes` exists for the block name.
                            self._field_src[(read_did, str(field).lower())] = \
                                f"{ku}#{field}"
        # WHERE IT CAME FROM decides whether it can be an EVENT (2026-10-01):
        #   retained -- the broker replaying what was last true on subscribe;
        #   state    -- tele/SENSOR and STATE REPEAT a switch's standing state
        #               every TelePeriod, whether or not it just changed
        #               (and STATUS10 answers with the same);
        #   live     -- stat/ and tele/RESULT: something happened just now.
        # A restart used to read the first repeat of "door open" as the
        # door opening.
        source = ("retained" if retained else
                  "state" if tail_up in ("SENSOR", "STATE", "STATUS10")
                  else "live")
        # A board that is TALKING is not offline, whatever its LWT said.
        if obs and not retained:
            self._offline.discard(board_topic)
            self._offline_since.pop(board_topic, None)
        good = []
        for did, key, value in obs:
            # NaN / inf: json.loads accepts the words, float() accepts them,
            # and every comparison after that is False -- they poisoned the
            # tiles, the history and the day's statistics.
            if isinstance(value, float) and not math.isfinite(value):
                continue
            good.append((did, key, value))
            self._observe(did, key, value, source=source)
        obs = good
        if obs:
            self._maybe_send_reading_rules(board_topic, set(table.values()))
        return obs

    def _maybe_send_reading_rules(self, board_topic: str,
                                  dids: set) -> None:
        """Give a board the rules that make its sensors report on every
        read, once its sensors have said what they are called.

        THE ORDER MATTERS AND IT IS NOT A TIMER. The rules name fields by
        the board's own spelling, so they cannot be written before the
        board has spoken once -- which it does on its first TelePeriod,
        within five minutes of coming up. That first message is what fires
        this, and the board reports every second from then on. So the
        sequence is: flash, one slow telemetry, fast for ever.

        A board whose sensors have not changed is never written to again:
        `_rules_sent` holds the field set each board was last given, and
        our own published telemetry comes straight back through feed() --
        which is exactly the loop this guard closes.

        Never raises. A board that cannot be given rules still reports on
        TelePeriod like it always did, which is the old behaviour and not
        a failure.
        """
        client = self._pub
        if client is None or self.esp is None:
            return
        # THE SPELLINGS, AS A SET. Keyed by reading NAME they would
        # merge: a board carrying a BME280 and a DS18B20 has two
        # different "temperature"s, and a dict would keep one.
        # The board's devices come from its index table, which feed()
        # already holds: this runs on EVERY message that carried a
        # reading, and finding them from the board records meant a
        # directory walk and a JSON read per board, per message.
        spellings = {src for (d, _k), src in list(self._field_src.items())
                     if d in dids}
        if not spellings:
            return
        seen = frozenset(spellings)
        if self._rules_sent.get(board_topic) == seen:
            return
        # AND NOT TOO OFTEN. A board discovers its sensors over
        # several telemetry messages, and each new field changes the
        # set -- so without this the whole rule set is rewritten once
        # per message while a board is waking up. The same guard, and
        # the same reason, as _reassert_settings.
        now = self.now()
        if now - self._rules_at.get(board_topic, 0.0) < self.SETTINGS_GAP:
            return
        try:
            commands = self.esp.reading_rules(spellings)
            # WHAT WOULD NOT FIT, ASKED FOR IMMEDIATELY (2026-09-14).
            # reading_rules packs three Tasmota rule sets and names what it
            # had no room for; read here, next to the call, because it is
            # per-call state and anything else would be reporting the wrong
            # board's leftovers.
            dropped = tuple(self.esp.rules_dropped() or ())
        except Exception:                           # noqa: BLE001
            return
        self._rules_sent[board_topic] = seen
        if not commands:
            return                                  # nothing to do, and known
        self._rules_at[board_topic] = now
        sent = sum(1 for command in commands
                   if self._lcd_send(client, board_topic, command))
        if sent:
            # `commands` carries one `RuleN 1` enable line PER RULE SET now,
            # not one overall, so the reading count is the spellings that
            # actually got a trigger -- not len(commands) - 1, which was
            # right only while everything fitted in rule set 1.
            wrote = len(spellings) - len(dropped)
            event("AUTO", f"{board_topic}: {wrote} sensor "
                          f"reading(s) set to report on every read "
                          f"({', '.join(sorted(set(spellings) - set(dropped)))}) "
                          f"-- Tasmota publishes a sensor only on "
                          f"TelePeriod, whose minimum is 10s and whose "
                          f"default is 300s")
        if dropped:
            # NAMED, NOT SWALLOWED. Three rule sets hold about 1500
            # characters between them; a board with more sensors than that
            # leaves some on TelePeriod, which is SLOW rather than broken.
            # Saying which ones is the whole difference between a known
            # limit and "that sensor is wired and registered and somehow
            # lags" -- the failure this mechanism exists to remove.
            event("AUTO", f"{board_topic}: no rule room left for "
                          f"{', '.join(sorted(dropped))} -- Tasmota has "
                          f"three rule sets and this board has filled them. "
                          f"Those readings still arrive, on TelePeriod "
                          f"(10-300s) rather than on every read.")

    # ── the day's numbers, kept locally ─────────────────────────────────

    HISTORY_SPAN = 24 * 3600.0      # how far back day_stats looks
    HISTORY_STEP = 60.0             # at most one sample per key per minute
    HISTORY_SAVE_EVERY = 300.0      # tick() writes the file this often

    #: The switch actions that MEAN something about the pin's state.
    #: Tasmota sends these for `SwitchMode 1`/`2`, which is what every
    #: contact, PIR and sensor-module entry in the catalogue configures.
    _SWITCH_ON = frozenset({"ON", "1", "TRUE"})
    _SWITCH_OFF = frozenset({"OFF", "0", "FALSE"})

    def _switch_action(self, board_topic: str, n: int,
                       action: Any) -> Optional[bool]:
        """One switch action as a state -- or None when it does not carry
        one, in which case NOTHING is observed.

        WHY THIS IS NOT `action == "ON"` (2026-09-14). It was, and the else
        of that comparison is `False` -- so every action Tasmota can send
        that is not the word ON was recorded as the pin being OFF:

            {"Switch1":{"Action":"TOGGLE"}}   -> "there is no motion"
            {"Switch1":{"Action":"HOLD"}}     -> "there is no motion"
            {"Switch1":{}}                    -> "there is no motion"

        None of those said that. The hub was inventing an absence out of a
        message that reported an event, writing it into the device record
        the app draws from, and -- worse -- feeding it to `absent` rules,
        which would then start counting an emptiness that had not happened.

        AND `TOGGLE` IS NOT HYPOTHETICAL: it is precisely what a switch
        sends when its `SwitchMode` has been lost, since mode 0 is
        Tasmota's default and mode 0 is TOGGLE. That is the same settings
        loss `switch_commands` and `_reassert_settings` exist for -- so the
        one message that says "this board's input settings are gone" was
        being read as a confident, ordinary "nothing is happening".

        An unreadable action is now observed as nothing at all, and said
        once per switch per hub run, because a PIR that reports TOGGLE is a
        PIR whose rules will never fire and that is worth a line.
        """
        word = str("" if action is None else action).strip().upper()
        if word in self._SWITCH_ON:
            return True
        if word in self._SWITCH_OFF:
            return False
        key = f"{board_topic}#{n}"
        if key not in self._switch_odd_said:
            self._switch_odd_said.add(key)
            event("AUTO", f"{board_topic} switch {n} reports "
                          f"{word or '(nothing)'!r}, which says nothing about "
                          f"whether it is on or off -- ignored rather than "
                          f"recorded as off. TOGGLE means its SwitchMode is "
                          f"back at Tasmota's default: the hub re-sends "
                          f"SwitchMode{n} every time the board comes back, so "
                          f"power-cycle it, or send `SwitchMode{n} 1` on its "
                          f"console. Until then no rule on this input can "
                          f"fire")
        return None

    def _remember(self, did: str, key: str, value: Any, ts: float) -> None:
        """One numeric sample into the history. Bools are not numbers here."""
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return
        hk = (str(did), str(key))
        with self._lock:
            series = self._history.get(hk)
            if series is None:
                series = self._history[hk] = deque()
            # A 10-second telemetry must not bloat the deque: a sample
            # younger than a minute is REPLACED, not appended.
            if not math.isfinite(float(value)):
                return
            if series and ts - series[-1][0] < self.HISTORY_STEP:
                # The bucket keeps the time it OPENED at. Stamping the new
                # time on every replacement meant a sensor reporting more
                # than once a minute never closed its bucket: the whole day
                # collapsed into one point and the 24-hour view was blank.
                series[-1] = (series[-1][0], float(value))
            else:
                series.append((ts, float(value)))
            cutoff = ts - self.HISTORY_SPAN
            while series and series[0][0] < cutoff:
                series.popleft()

    def record(self, did: str, key: str, value: Any) -> None:
        """A numeric reading into the history WITHOUT rule evaluation --
        main calls this with values it reads from Home Assistant for an
        integration sensor, so they accumulate like a board's do."""
        self._remember(did, key, value, self.now())

    def day_stats(self, did: str, key: str) -> Optional[Dict[str, float]]:
        """min / max / mean / count / since over the last 24 h of one
        reading, or None with fewer than two samples."""
        ts = self.now()
        with self._lock:
            series = self._history.get((str(did), str(key)))
            points = [v for (t, v) in (series or ()) if t >= ts - self.HISTORY_SPAN]
            since = next((t for (t, _v) in (series or ())
                          if t >= ts - self.HISTORY_SPAN), None)
        if len(points) < 2:
            return None
        return {"min": min(points), "max": max(points),
                "mean": sum(points) / len(points),
                "count": float(len(points)), "since": float(since)}

    def _load_history(self) -> None:
        try:
            with open(self.history_path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
        except FileNotFoundError:
            return
        except Exception as exc:                    # noqa: BLE001
            event("AUTO", f"readings history unreadable ({exc}) -- "
                          f"starting empty")
            return
        if not isinstance(data, dict):
            return
        cutoff = self.now() - self.HISTORY_SPAN
        for name, rows in data.items():
            did, _, key = str(name).partition("|")
            if not did or not key or not isinstance(rows, list):
                continue
            series: deque = deque()
            for row in rows:
                try:
                    t, v = float(row[0]), float(row[1])
                except (TypeError, ValueError, IndexError):
                    continue
                if t >= cutoff:
                    series.append((t, v))
            if series:
                self._history[(did, key)] = series

    def _save_history(self) -> None:
        """{"<did>|<key>": [[ts, value], ...]}, pruned, written atomically."""
        cutoff = self.now() - self.HISTORY_SPAN
        with self._lock:
            out = {}
            for (did, key), series in self._history.items():
                while series and series[0][0] < cutoff:
                    series.popleft()
                if series:
                    out[f"{did}|{key}"] = [[t, v] for (t, v) in series]
        self._write_json(self.history_path, out, "readings history")

    def _observe(self, did: str, key: str, value: Any, *,
                 source: str = "live") -> None:
        ts = self.now()
        k = (did, key)
        prev = self._last.get(k)
        self._last[k] = (value, ts)
        # What each device draws: its on/off changes, and the main meter's
        # readings around them (a silent meter's zeros are refused there).
        try:
            self._draw_watch(did, key, value, prev, ts, source)
        except Exception:                           # noqa: BLE001
            traceback.print_exc()
        # SOMETHING NEW ARRIVED. Counted so the home-tile push can be
        # EVENT-DRIVEN rather than a poll: see ws_server._readings_watcher.
        # A number, not a flag, because two readings landing between two
        # ticks must not look like one.
        #
        # ONLY WHEN IT ACTUALLY MOVED (2026-09-12: "theres a way to keep a
        # number constant and once it changes its displayed immediately...
        # instead of constant polling, its just a watcher type of polling").
        # The previous value was already held one line above, for the
        # threshold crossing below; it was simply not consulted here, so a
        # sensor repeating 21.5 every second woke the tile walk every
        # second to rebuild rows nobody could tell apart. The watcher's
        # fingerprint threw the result away, which made it harmless but
        # not free -- and it is what stopped a board being allowed to
        # speak once a second in the first place.
        #
        # THE COUNTER ONLY. Everything below still runs on EVERY arrival:
        # `absent` needs to know the device is alive even when it is
        # saying the same thing, and a rule that has not fired yet must
        # still get its chance. This gates the PUSH, not the thinking.
        if self._worth_showing(key, prev[0] if prev else None, value):
            self._reading_seq += 1
        self._remember(did, key, value, ts)
        # A meter that is not answering: its zeros are kept for showing (as
        # the board sends them), but no rule acts on them; the first real
        # reading after is compared with what rules saw before the silence.
        if did in self._meter_silent:
            if k not in self._meter_held:
                self._meter_held[k] = prev
            return
        if k in self._meter_held:
            prev = self._meter_held.pop(k)

        # activity for `absent`, persistence stretches for `persist`
        if isinstance(value, bool):
            for want in (True, False):
                wk = (did, "on" if want else "off")
                if value is want:
                    self._activity[wk] = ts
                    if wk not in self._since:
                        self._since[wk] = ts
                else:
                    # It WAS so until this moment, so the quiet starts now,
                    # not at the last report of it (a PIR held on for twenty
                    # minutes is not twenty minutes quiet when it drops).
                    if self._since.pop(wk, None) is not None:
                        self._activity[wk] = ts

        answered = 0
        for rule in list(self.rules):
            if not rule.get("enabled", True) or rule.get("_invalid"):
                continue
            t = rule.get("trigger", {})
            if t.get("device") != did:
                continue
            try:
                kind = t.get("kind")
                if kind == "event" and key == "on":
                    want = t.get("value", "on") == "on"
                    # THE MOMENT IT BECOMES SO, not every report that it IS
                    # so. Tasmota answers one relay change on BOTH
                    # stat/RESULT and stat/POWER, and repeats a detached
                    # switch's state in every tele/SENSOR -- so this fired
                    # again (or logged "held back") for reports that were
                    # not events, e.g. "the door opened" every TelePeriod
                    # while it stood open.
                    if source == "retained":
                        pass        # what WAS true, not something happening
                    elif value is want:
                        if prev is None and source == "state":
                            # The first report since this hub started is
                            # the board repeating where it stands, not the
                            # door opening: it sets the baseline.
                            pass
                        elif prev is None or prev[0] != value:
                            if not self._fire(rule) and rule.get("revert"):
                                # Withheld (condition, cooldown): a repeat
                                # report may still assert it, as before.
                                self._reverted.add(rule.get("id", ""))
                        elif rule.get("id", "") in self._reverted:
                            # Still so, but the revert has already run
                            # (the light went off while the sensor stayed
                            # on): the repeat report re-asserts the rule,
                            # subject to its cooldown in _fire.
                            self._fire(rule)
                        else:
                            # Still so: keep a pending revert away, as a
                            # re-fire would have (the motion light stays
                            # on while the sensor still says motion).
                            self._extend_revert(rule)
                elif kind == "event" and key in ("tag", "ir", "button"):
                    # A TAP AND A BUTTON PRESS ARE EVENTS TOO -- and until now
                    # the only event that could fire anything was `on`. The
                    # reader was heard (the observation is recorded, the
                    # capability is offered, "a card is tapped" appears in the
                    # WHEN list), the rule saved, and then nothing happened,
                    # ever. Proven 2026-09-02: the tap arrives as
                    # ("hv.reader", "tag", "BA839D07") and `key == "on"` is
                    # false, so every branch was skipped in silence.
                    #
                    # THE VALUE IS THE IDENTITY. "any" fires on every card or
                    # every code; anything else fires only on that exact UID or
                    # that exact infrared code. A missing value reads as "any",
                    # because a rule that named no card meant all of them.
                    #
                    # A UID IS A NAME, NOT A PROOF (the RFID design law,
                    # 2026-08-16): this starts a rule, and law 2 -- secure
                    # devices are never automation actions -- is what keeps a
                    # tap from being a key.
                    # "on" too: trigger_sentence already reads it as
                    # "fires", and a button rule authored as a switch's
                    # "turns on" means any press.
                    want = str(t.get("value", "any"))
                    # A REPLAYED TAP IS NOT A TAP. A retained message is
                    # what the broker last held, and acting on it would
                    # answer a card nobody is holding to the reader now.
                    if source == "retained":
                        continue
                    if want in ("any", "", "on") or want == str(value):
                        answered += 1
                        self._fire(rule)
                elif kind == "threshold" and key == str(t.get("key", "")):
                    try:
                        reading = float(value)
                    except (TypeError, ValueError):
                        continue
                    rk = (rule["id"], "side")
                    above = t.get("above")
                    line = float(above if above is not None else t["below"])
                    hot = reading > line if above is not None else reading < line
                    was_hot = self._threshold_side.get(rk, False)
                    # fire on the CROSSING into hot, not on every report --
                    # a kettle at 1800 W must not fire once per telemetry.
                    #
                    # THE SIDE IS REMEMBERED ONLY ONCE THE CROSSING HAS BEEN
                    # ANSWERED (2026-10-01). It was written before _fire, so
                    # a crossing that the cooldown or a condition withheld
                    # was recorded as "already hot" and every later report
                    # of the same excursion was ignored: a temperature alert
                    # lost for as long as the temperature stayed high. Kept
                    # as "not hot yet", the next report tries again.
                    if not hot:
                        self._threshold_side[rk] = False
                    elif source == "retained":
                        self._threshold_side[rk] = True     # old news
                    elif not was_hot:
                        # (a first-ever reading already over the line counts:
                        # "power above 100" on a hub that just started must
                        # not wait for the kettle to cool and reboil.)
                        if self._fire(rule):
                            self._threshold_side[rk] = True
                elif kind == "change" and key == str(t.get("key", "")):
                    # WHENEVER IT MOVES. The prev/new pair is already in
                    # hand three lines up -- the same pair the push counter
                    # uses -- so "did it move" is asked once, here, in the
                    # one place a reading actually lands.
                    #
                    # WHAT COUNTS AS MOVEMENT, in order:
                    #   `by` on the trigger, if the rule named one;
                    #   otherwise `_worth_showing`, which is the engine's
                    #   own per-reading deadband and already knows that
                    #   temperature moves in tenths and illuminance in
                    #   whole numbers.
                    #
                    # THE FIRST READING IS A CHANGE. A panel that has been
                    # dark since the hub started should show the number as
                    # soon as one exists, not wait for the second one.
                    by = t.get("by")
                    if prev is None:
                        moved = True
                    elif by is not None:
                        try:
                            moved = abs(float(value) - float(prev[0])) >= float(by)
                        except (TypeError, ValueError):
                            # A non-numeric reading (a tag, an IR code) has
                            # no distance; any difference is a change.
                            moved = prev[0] != value
                    else:
                        moved = self._worth_showing(key, prev[0], value)
                    if moved:
                        self._fire(rule)
            except Exception:                       # noqa: BLE001
                # One bad rule (a non-numeric line, a missing id) used to
                # abort every later rule AND every later fact in the batch.
                traceback.print_exc()
                self._note(rule, "crashed while evaluating -- skipped")

        # EVERY TAP IS WRITTEN DOWN, answered or not (her ruling,
        # 2026-09-02).
        #
        # A card that matched no rule used to leave NO TRACE ANYWHERE: the
        # journal records rules that fired, `observed_keys` skips "tag" on
        # purpose, and nothing else looks. So the one event a security
        # system most needs to have noticed -- an unrecognised card held to
        # the reader -- was the one event it forgot instantly.
        #
        # The journal already goes to the app in the automation list
        # payload, so this needs no new screen and no new store. A UID is
        # a name and not a proof, which is exactly why a LOG of names is
        # worth keeping: it cannot authenticate anybody, and it can tell
        # you afterwards what was held to the reader and when.
        if key == "tag" and source != "retained":
            who = ((self.dm.devices or {}).get(did) or {}).get("name") or did
            # WHOSE CARD, when the hub has been told. `card_owner` is set by
            # the shell and stays None on a hub with no card store -- so a
            # UID is what a line says until somebody enrols it, and a name
            # afterwards. That legibility is the whole reason enrolment
            # exists; it grants no authority (a UID is a name, not a proof).
            named = None
            if self.card_owner is not None:
                try:
                    named = self.card_owner(str(value))
                except Exception:                   # noqa: BLE001
                    named = None
            whose = f"{named}'s card" if named else f"card {value}"
            tail = "" if answered else " -- no rule answers to it"
            self.journal.appendleft((ts, who, f"{whose} tapped{tail}"))
            event("AUTO", f"{who}: {whose} tapped"
                          f"{'' if answered else ' (unrecognised)'}")

    # ── The LCD ─────────────────────────────────────────────────────────
    #
    # One line at a time, advanced by the same 5-second tick everything else
    # here runs on -- no second clock, no second connection. The engine's
    # MQTT client is already open and signed in as the hub (see
    # EspBuilder.hub_login), so it can publish as well as subscribe.
    #
    # THE CLOCK COMES FROM THE HUB, not the board. Tasmota can show its own
    # time, but only with NTP, and a board on a hotspot with no internet
    # never gets it -- it would show 1970 for ever.

    def _show(self, rule: Dict[str, Any], action: Dict[str, Any]) -> None:
        """A rule writes one line to a panel, and holds it.

        THE VALUE COMES FROM THE CACHE, not from a value threaded through
        the firing. `{value}` is resolved at the moment the line is
        written, out of the same _last the tiles and the rotation read --
        so no signature changes, and the number on the glass is the number
        that is true when it appears rather than the one that tripped the
        rule a beat earlier.
        """
        did = str(action.get("device", ""))
        slug = did.split(".", 1)[0]
        panel = next((p for p in self._lcd_targets if p.get("slug") == slug),
                     None)
        if panel is None:
            self._note(rule, f"{did} is not a panel this hub knows -- "
                             f"nothing shown")
            return
        if self._pub is None:
            self._note(rule, "no broker connection -- nothing shown")
            return
        # AND IS THE PANEL REALLY THERE? (2026-09-14.)
        #
        # Everything above this line proves the panel is on the PLAN. That
        # is not the same as being on the BUS, and the difference is the
        # whole of her dead kitchen screen: the plan said an LCD, so this
        # method wrote to it and wrote "showed '146W' for 600s" in the
        # journal, every firing, for weeks, to an I2C bus with nothing on
        # it. The hub claiming it did something it did not do is the one
        # fault this project treats as unforgivable, and it had it on the
        # most visible surface in the house.
        #
        # `_panel_absent` answers only when a scan really came back and
        # really did not list this panel's address. Silence, an unparsable
        # answer, a firmware with no I2C: all read as present, and this
        # writes exactly as it always did.
        # IT WARNS, IT DOES NOT REFUSE (2026-09-14, same day it was written).
        #
        # The first version of this RETURNED here, so a scan that did not
        # list the panel's address stopped the write. The reasoning was
        # sound -- do not claim to have shown something on a screen that is
        # not there -- and the consequence was not: a scan is a guess about
        # hardware, and a wrong guess left her with a panel that works from
        # the console and shows nothing from a rule. She proved the panel
        # alive with one command while this was silently refusing every
        # write to it.
        #
        # So the honesty stays and the refusal goes. The line is written
        # exactly as it always was; the journal carries the doubt instead
        # of the hub acting on it. If the panel really is missing, the
        # message costs three MQTT publishes into the void and the journal
        # says so plainly -- which is the right way round, because a lost
        # line is worse than a wasted publish.
        doubt = self._panel_absent(panel)

        text = self._fill(display_words(action), rule)
        # WHICH ROW, AND WHAT THAT MEANS FOR THE REST OF THE GLASS.
        #
        # No row: the message OWNS the panel -- one line addressed at the
        # top and every other row blanked, which is what a warning wants
        # and what every rule written before today already means.
        #
        # A row: this line and NOTHING ELSE, so three rules can each hold
        # a row of a 20x4 (power on 1, temperature on 2, the door on 3)
        # without wiping each other out on every firing. Her ask,
        # 2026-09-01.
        row = action.get("row")
        try:
            row = None if row is None else max(1, int(row))
        except (TypeError, ValueError):
            row = None
        topic = panel["topic"]
        # TAKE THE GLASS, WRITE, AND THE TICK HANDS IT BACK.
        #
        # The panel RESTS in Tasmota's own mode 1 -- its clock, its
        # renderer, its layout, costing this hub nothing and proven to
        # render correctly on the owner's board when nothing else
        # would. DisplayText only works in mode 0, so a message takes
        # the screen, holds it, and _lcd_tick returns it to the clock
        # when the hold expires. Her design, 2026-09-01.
        # ONE COMMAND PER ROW, single escape each. `[x0y0]ABCD` is the
        # exact shape proved on the board; four escapes packed into one
        # DisplayText was not, and that is the only difference between
        # the write that worked by hand and the ones that came out as
        # gibberish. Not worth being clever about on a four-line
        # screen.
        hold = self._hold_of(action)
        if action.get("pages"):
            row = None                       # pages own the whole panel
            self._lcd_page[topic] = {"slot": 0, "since": self.now()}
        # THE BOARD IS NOT THERE TO WRITE TO RIGHT NOW (2026-10-20: "showed
        # 'Good morning' for 600s but the text isnt showing but its lighting
        # up"). At start-up the panel's wake sends DisplayModel, the board
        # RESTARTS, and a rule firing in those seconds wrote into the void,
        # set a ten-minute hold -- and the hold froze the rest of the wake.
        # Backlight on, no text, for ten minutes, and the journal said
        # "showed". Now the line is kept as the panel's standing line and
        # NOT written: the wake finishes, the clock has its turn, and the
        # cycle puts the line up (re-rendered) once the board can take it.
        away = self._panel_away(topic)
        if away:
            standing = self._lcd_standing.setdefault(topic, {})
            if row is None:
                standing.clear()
            else:
                standing.pop(None, None)
            standing[row] = {"rule_id": str(rule.get("id", "")), "device": did}
            self._state_dirty = True        # survives a restart
            self._note(rule, f"waiting to show {text.strip()!r} -- {away}; "
                             f"it goes up as soon as the board is ready")
            return
        if action.get("pages"):
            painted = self._page_lines(panel, action, rule, hold)
        else:
            painted = [(row, text, hold)]
        if not self._paint(panel, painted):
            self._note(rule, "the panel could not be written to -- the "
                             "broker did not take the message")
            return
        # PER PANEL, AND THE LONGEST HOLD WINS (2026-09-14).
        #
        # This was ONE SCALAR on the engine, overwritten by every call and
        # applied by _lcd_tick to every panel at once -- which contradicts
        # this method's own promise three paragraphs up, that a `row` lets
        # "three rules each hold a row of a 20x4 ... without wiping each
        # other out". The row ADDRESSING was per-row; the TIMER was global:
        #
        #     Rule A writes row 1, for_s 300 ("door left open").
        #     Ten seconds later Rule B writes row 2, for_s 10.
        #     The deadline is now now+10, so twenty seconds in, the tick
        #     hands the glass back to the clock and the five-minute warning
        #     is gone.
        #
        # With two panels it was worse still: a long hold on one froze the
        # OTHER panel's reboot-recovery wake sequence, because the gate in
        # _lcd_tick read the same single scalar.
        #
        # max(), not assignment, for the same reason: a short message on
        # row 2 must not cut short a long one already standing on row 1.
        # The rows are independent but the panel has one mode, so its
        # deadline is the last moment any row still needs mode 0.
        # ZERO MEANS FOREVER -- see HOLD_FOREVER. `math.inf` rather than a
        # very large number so that the max() below keeps working and so
        # that "is this panel held?" stays one comparison everywhere: a
        # sentinel like -1 would need a special case at every reader, and
        # the readers are what forget.
        self._hold_panel(topic, hold)
        # WHAT THIS PANEL IS NOW STANDING ON, so the clock's turn is a turn
        # and not the end (2026-09-14). The RULE is kept, never the
        # rendered text: when this line comes back after the clock it is
        # filled again, so `{kitchen.meter:power}W` shows the power now.
        # Replaying the rendered string would put a frozen number back on
        # the glass, which is the exact complaint that started all of this.
        #
        # THE TWO SHAPES _show WRITES, kept apart. A message with no row
        # OWNS the panel -- it blanked every other row on its way in -- so
        # it replaces the whole standing set. A message WITH a row owns one
        # row, so it joins the set; and it drops any whole-panel message
        # standing there, because bringing that back would blank the rows
        # its neighbours are holding.
        standing = self._lcd_standing.setdefault(topic, {})
        if row is None:
            standing.clear()
        else:
            standing.pop(None, None)
        standing[row] = {"rule_id": str(rule.get("id", "")), "device": did}
        self._state_dirty = True        # survives a restart
        self._note(rule, f"{'sent' if doubt else 'showed'} {text.strip()!r}"
                         + (" until something else is shown" if hold == 0
                            else f" for {int(hold)}s")
                   # THE DOUBT, CARRIED WITH THE CLAIM. "showed" is a claim
                   # about a screen; when the bus has not confirmed that
                   # screen exists the verb changes to "sent" and the
                   # reason is attached, so the journal never says a line
                   # appeared somewhere the hub cannot vouch for.
                   + (f" -- but {doubt}" if doubt else ""))

    @staticmethod
    def _shown(value: Any) -> str:
        """One reading, as it should read on glass twenty characters wide."""
        if isinstance(value, bool):
            return "on" if value else "off"
        if isinstance(value, float):
            return f"{value:g}"
        return str(value)

    def _fill(self, text: str, rule: Dict[str, Any]) -> str:
        """The placeholders a display line may carry.

        `{value}` and `{name}` are the RULE'S OWN TRIGGER -- its reading
        and its device's name. They came first and they stay: a rule that
        fires on a meter and then shows a sentence without the number in
        it is a worse notification than the tile already on the phone.

        ...AND ANY OTHER DEVICE BY NAME (2026-09-01: "in automations can I
        write what I want displayed, or any device's data I want
        displayed?"). The trigger was the only thing a line could quote,
        so a rule that fires on a door could not show the temperature --
        the panel could only ever talk about the thing that woke it. Now:

            {hall.dht11}            its whole reading, as a sentence
            {hall.dht11:temperature}  one named reading of it

        A colon separates the device from the key, because a device id has
        dots in it already and a second meaning for the dot is how a
        parser starts guessing.

        VALUES ARE READ WHEN THE LINE IS WRITTEN, out of the same cache
        the tiles and the rotation read -- so the number on the glass is
        the one that is true when it appears, not the one that tripped the
        rule a beat earlier. An unknown device or a reading nothing has
        sent yet leaves the placeholder EMPTY rather than printing its own
        name: a blank space on a display says "no reading" more honestly
        than `{hall.dht11}` does.
        """
        if "{" not in text:
            return text
        trigger = rule.get("trigger") or {}
        did = str(trigger.get("device", ""))
        # An event names no key: its value is whichever moment this device
        # last reported -- a switch's on/off, a card's UID, a press, a code.
        keys = [trigger["key"]] if trigger.get("key") else ["on", "tag", "button", "ir"]
        pair = max((p for p in (self._last.get((did, str(k))) for k in keys) if p),
                   key=lambda p: p[1], default=None)
        name = ((self.dm.devices or {}).get(did) or {}).get("name") or did
        text = (text.replace("{value}", "" if pair is None
                             else self._shown(pair[0]))
                    .replace("{name}", str(name)))
        if "{" not in text:
            return text

        def quote(match: "re.Match") -> str:
            body = match.group(1).strip()
            other, _, want = body.partition(":")
            other, want = other.strip(), want.strip()
            if not other:
                return ""
            if want:
                got = self._last.get((other, want))
                return "" if got is None else self._shown(got[0])
            # No key named: the device's whole reading, through the same
            # describe_reading the voice answers with -- the panel and the
            # spoken answer cannot disagree.
            try:
                state = self.dm.get_state(other) if self.dm else None
            except Exception:                       # noqa: BLE001
                state = None
            if not isinstance(state, dict) or not state:
                return ""
            said = describe_reading(state)
            return "" if said == NO_READING else said

        return re.sub(r"\{([^{}]{1,64})\}", quote, text)

    def _lcd_tick(self) -> None:
        """Keep every panel woken, and hand the glass back when a message's
        time is up. Never raises: a display is decoration, and decoration
        must not take the reflexes down.

        THE ROTATION IS GONE (her ruling, 2026-09-01: "remove the rotation,
        don't need that"). It walked the readings across the panel every
        five seconds, and it was the hub re-implementing a clock and a
        reading list that the phone already draws better. What the panel
        is FOR is the two things nothing else can do: Tasmota's own time
        when nothing is happening, and an automation's line when something
        is. Both are here; the middle was noise.

        With it went `_lcd_lines`, `_lcd_greeting`, `_lcd_step` and
        `_lcd_greeted` -- about sixty lines whose whole job was deciding
        which reading to show next.

        WHAT CAME BACK IN ITS PLACE IS NOT THAT (2026-09-14). The panel now
        CYCLES between the clock and the line a rule asked for -- ten
        minutes of the line, five seconds of the clock, the line again --
        and the difference from the old rotation is the whole point: this
        chooses NOTHING. It shows what a rule asked for and the clock, and
        the only thing it decides is that the clock does not get to keep the
        glass for ever once a message's hold runs out. Set
        DISPLAY_CLOCK_SECONDS to 0 and the clock keeps it, exactly as
        before.

        So one panel, one tick, in order:

            1.  a hold that has run out  -> the clock, and its turn starts
            2.  a clock turn that is over -> the standing lines, re-rendered
            3.  a panel that has not been woken -> the next wake stage
            4.  a panel that is due a bus scan -> `I2Cscan`

        Three is skipped while a rule owns the panel -- a wake stage under a
        message would either hand the glass back mid-sentence or
        re-initialise the driver beneath it. FOUR IS NOT: `I2Cscan` asks a
        question and touches no display setting, and a panel held by a
        `for_s: 0` line would otherwise never be re-checked at all -- which
        is exactly the panel whose disappearance matters most.
        """
        client = self._pub
        if client is None or self.esp is None or not self._lcd_targets:
            return

        # HAND THE GLASS BACK FIRST. A message held the panel in mode 0;
        # when its time is up the clock returns. ONCE -- `_lcd_hold_until`
        # is zeroed so this cannot re-send mode 1 every five seconds, which
        # is what would turn a resting display into a flicker.
        #
        # BEFORE the wake, so a panel that was still mid-wake when a rule
        # took the screen is marked finished by the handback and does not
        # then send its own DisplayMode 1 in the same breath.
        # PANEL BY PANEL. The deadline is per topic since 2026-09-14, so a
        # ten-second message on the kitchen panel no longer hands the
        # hallway panel back mid-sentence. Each entry is dropped as it is
        # honoured, which is what keeps this to ONE mode-1 send per hold.
        now_ts = self.now()
        for topic in [t for t, until in list(self._lcd_hold_until.items())
                      if now_ts >= until]:
            # Only once the broker has taken it: a hand-back that was
            # dropped (paho rc != 0) keeps its expired hold and is tried
            # again next tick, rather than the panel being marked done.
            if not self._lcd_send(client, topic, "DisplayMode 1"):
                continue
            self._lcd_hold_until.pop(topic, None)
            # FULLY AWAKE: the clock is on it, so the wake has nothing
            # left to do. Left at a lower stage, a panel that was
            # mid-wake when a rule took the screen would re-send its
            # own DisplayMode 1 on the next tick -- a blink for no
            # reason. The number is the last stage, so it moves when
            # the sequence does.
            self._lcd_awake[topic] = 3
            # AND THE CLOCK'S TURN STARTS NOW. Written even when the cycle
            # is switched off, because the number it is compared against is
            # read at the comparison and a hub whose setting changes mid-run
            # must not find a deadline from the old one.
            self._lcd_clock_until[topic] = now_ts + DISPLAY_CLOCK_SECONDS

        # PUT THE LINE BACK WHEN THE CLOCK'S TURN IS OVER.
        #
        # The second half of the cycle, and the half that did not exist: a
        # hold running out used to be the end of the message for good. A
        # rule that fires once a day lit the panel for its hold and the
        # glass was a clock until the next day -- so "show the power on the
        # LCD" meant "show the power for ten minutes, once".
        #
        # WHAT COMES BACK IS RE-RENDERED, not replayed. `_fill` runs again
        # against the live cache, so the returning line carries the number
        # that is true at this tick. That is the difference between a panel
        # that tracks the meter and a panel showing a photograph of it.
        #
        # THE RULE IS ASKED FIRST. A standing line belongs to a rule, and a
        # rule that has since been deleted, switched off or marked invalid
        # has nothing standing on any glass -- its line is dropped here
        # rather than brought back from the dead every five seconds.
        if DISPLAY_CLOCK_SECONDS > 0:
            for panel in self._lcd_targets:
                topic = str(panel.get("topic") or "")
                if not topic or not self._lcd_standing.get(topic):
                    continue
                if now_ts < self._lcd_hold_until.get(topic, 0.0):
                    continue                # a rule owns it right now
                if now_ts < self._lcd_clock_until.get(topic, 0.0):
                    continue                # the clock is having its turn
                if self._lcd_awake.get(topic, 0) < 3:
                    continue                # mid-wake: let the wake finish
                # NO BUS GATE HERE EITHER (2026-09-14). This used to skip a
                # panel the scan had not confirmed, which meant a screen the
                # hub was wrong about never got its line back after the
                # clock's turn -- the cycle silently stopping halfway round.
                # Same rule as _show: a guess about hardware does not get to
                # withhold a write.
                self._restore(panel)

        # A HELD LINE THAT QUOTES A READING STAYS CURRENT (2026-11-05). It
        # was painted once and then sat for its whole hold -- a voltage on
        # the glass ten minutes old. Re-rendered every LIVE_REFRESH_S and
        # repainted only when the text actually changed.
        for panel in self._lcd_targets:
            topic = str(panel.get("topic") or "")
            if not topic or now_ts >= self._lcd_hold_until.get(topic, 0.0):
                continue                    # not showing a line right now
            if self._on_clock_turn(topic):
                continue                    # the clock's turn between pages
            standing = self._lcd_standing.get(topic) or {}
            if not standing:
                continue
            when, painted = self._lcd_live.get(topic, (0.0, None))
            if now_ts - when < LIVE_REFRESH_S:
                continue
            lines = self._standing_lines(panel)
            if not any("{" in self._raw_line(panel, k)
                       for k in (self._lcd_standing.get(topic) or {})):
                self._lcd_live[topic] = (now_ts, lines)
                continue
            if lines and lines != painted:
                self._paint(panel, lines)
            self._lcd_live[topic] = (now_ts, lines)

        # PAGES TURN (2026-11-05): a message of several pages shows each for
        # its page_s, with the clock between them when asked.
        for panel in self._lcd_targets:
            topic = str(panel.get("topic") or "")
            if not topic or now_ts >= self._lcd_hold_until.get(topic, 0.0):
                continue
            if self._panel_away(topic):
                continue
            found = self._rotation(panel)
            if found is None:
                continue
            live, _rule = found
            n_pages = len(display_pages(live))
            clock = bool(live.get("clock_between"))
            try:
                page_s = max(2.0, float(live.get("page_s")
                                        or DISPLAY_PAGE_SECONDS))
            except (TypeError, ValueError):
                page_s = DISPLAY_PAGE_SECONDS
            was = self._lcd_page.setdefault(topic, {"slot": 0,
                                                    "since": now_ts})
            stay = ((DISPLAY_CLOCK_SECONDS or page_s) if was.get("clock")
                    else page_s)
            if now_ts - float(was.get("since", now_ts)) < stay:
                continue
            slots = 2 * n_pages if clock else n_pages
            slot = (int(was.get("slot", 0)) + 1) % max(1, slots)
            if clock and slot % 2 == 1:
                if self._lcd_send(client, topic, "DisplayMode 1"):
                    self._lcd_page[topic] = {"slot": slot, "since": now_ts,
                                             "clock": True}
                continue
            self._lcd_page[topic] = {"slot": slot, "since": now_ts}
            lines = self._standing_lines(panel)
            if lines and self._paint(panel, lines):
                self._lcd_live[topic] = (now_ts, lines)
            else:
                self._lcd_page[topic] = was    # not written: try next tick

        # WAKE A PANEL -- IN THREE STAGES, ONE TICK APART, and then never
        # again.
        #
        # A board that has rebooted since it was installed comes back with
        # its display dark, or lit and full of rubbish: a character LCD
        # that finishes booting after the ESP32 does misses its own init
        # and draws garbage. Her board, every power cycle -- "no light at
        # all until I send DisplayModel 1", and later "the gibberish came
        # again until I did DisplayMode 0 which cleared everything and
        # DisplayMode 1 that brought back the time and date".
        #
        # THAT IS THE SEQUENCE, AND THE GAPS IN IT ARE NOT INCIDENTAL.
        # Three commands, three ticks, five seconds apart -- her hand
        # sequence at her own pace:
        #
        #     1.  DisplayModel / DisplayDimmer   re-init, backlight
        #     2.  DisplayMode 0                  clear
        #     3.  DisplayMode 1                  the clock
        #
        # It was TWO stages until 2026-09-02, with the clear riding along
        # with the re-init. That does not work: the clear reaches a driver
        # still coming up and the rubbish survives it -- "the initial
        # activation just showed symbols and gibberish". The re-init is
        # what MAKES the gibberish, so it has to be given a tick to finish
        # before anything tries to wipe it.
        #
        # Stage three is also the answer to "on activation let the LCD
        # just show the time and date": DisplayMode 1 IS that text --
        # Tasmota's own clock, kept by the board, needing nothing from the
        # hub and surviving the hub going away. It goes LAST, so it is
        # what remains on the glass.
        #
        # A MESSAGE OUTRANKS THE WAKE. While a rule holds the glass, no
        # stage advances: a later stage would hand the panel back to the
        # clock mid-sentence, and an earlier one would re-initialise
        # underneath it. A panel found during a hold wakes when it ends.
        #
        # ASKED OF THE PANEL BEING WOKEN, not of the house (2026-09-14).
        # This read the single shared deadline, so ANY rule holding ANY
        # panel froze EVERY panel's wake -- including a second panel that
        # had just rebooted and was sitting there showing garbage until an
        # unrelated message on the first one expired.
        for panel in self._lcd_targets:
            topic = str(panel.get("topic") or "")
            if not topic:
                continue
            if now_ts < self._lcd_hold_until.get(topic, 0.0):
                continue                    # a rule owns THIS panel
            stage = self._lcd_awake.get(topic, 0)
            if stage >= 3:
                continue
            restart_at = self._lcd_restarting.get(topic)
            if restart_at is not None:
                if now_ts - restart_at < self.LCD_RESTART_WAIT:
                    continue                # rebooting from our DisplayModel
                self._lcd_restarting.pop(topic, None)
            if stage == 0:
                # ONE: the panel's own settings, and nothing else.
                # `DisplayModel` RE-INITIALISES the driver, and a character
                # LCD mid-init is the gibberish -- so this tick starts it
                # and then leaves it alone.
                #
                # SOFT WAKE: a board that has just come back on its own
                # gets the backlight and nothing that re-initialises the
                # driver. See _note_presence for why that distinction is
                # the difference between a panel relighting and a board
                # restarting in a loop.
                #
                # ...UNLESS THE BUS SAYS THE PANEL IS REALLY THERE
                # (2026-09-14: "lcd is not displaying until i personally
                # tune it in console").
                #
                # The soft wake withholds `DisplayModel`, and DisplayModel
                # is precisely the command she has to send by hand to get
                # any light at all -- her own words on 2026-09-01, "when I
                # power cycle, no light at all until I send DisplayModel
                # 1". So every time the board came back, the hub sent the
                # backlight, cleared, showed the clock, and left the glass
                # dark, because the one command that re-initialises the
                # driver was the one it had decided to skip. The console
                # was the only way back, every single reboot.
                #
                # The soft wake was right when the hub could not tell a
                # present panel from an absent one: re-initialising a
                # driver for a screen that is not there is what the
                # fifteen-second reboot loop was made of (2026-09-04). It
                # can tell now. A scan that FOUND the panel's address is
                # positive evidence that the chip is on the bus, which is
                # exactly the case where re-initialising helps and the
                # case where the loop risk is worth taking.
                # `DisplayModel <n>` restarts the board, so it goes only
                # when it is needed (2026-10-26): to a board just POWERED ON
                # (_note_boot_reason -- her "no light at all until I send
                # DisplayModel 1" is a power-on: the panel starts slower than
                # the ESP32 and is not found), or to one that answers 0
                # (settings lost: _note_display_model). Here it is only
                # ASKED (a bare `DisplayModel`), so a board back from a
                # reconnect or a restart is not restarted all over again.
                commands = [c for c in (panel.get("wake") or [])
                            if str(c).partition(" ")[0] not in
                            ("DisplayMode", "DisplayModel")] + ["DisplayModel"]
            elif stage == 1:
                # TWO: clear, a whole tick later. This used to ride in the
                # same breath as the re-init and did not take -- the clear
                # reached a driver that was still coming up, and the
                # rubbish stayed ("the initial activation just showed
                # symbols and gibberish", 2026-09-02).
                commands = ["DisplayMode 0"]
            else:
                # THREE: the clock. Her whole ask for the wake -- "on
                # activation let the LCD just show the time and date" --
                # and the last thing sent, so it is what remains on the
                # glass.
                commands = ["DisplayMode 1"]
            # THE STAGE MOVES ONLY IF THE COMMANDS ACTUALLY WENT OUT.
            # It used to move first and publish afterwards, swallowing
            # whatever happened -- and a qos-0 publish on a client that is
            # not connected yet is DROPPED, silently, with no exception to
            # catch (paho returns rc=4, it does not raise). The engine's
            # first tick fires immediately after connect() and can easily
            # beat the CONNACK, so stage one -- DisplayModel and
            # DisplayDimmer, the re-init and the backlight -- went nowhere
            # while the counter said it had been done, and the panel then
            # sat dark for ever ("the LCD commands are not working ... it
            # is meant dimmer 100 and display 0 once the LCD is detected
            # but it did not", 2026-09-04). Now a stage that could not be
            # sent is simply tried again on the next tick.
            if all(self._lcd_send(client, topic, c) for c in commands):
                self._lcd_awake[topic] = stage + 1
                if self._restarts_board(commands):
                    self._lcd_restarting[topic] = self._lcd_restarted_at[topic] \
                        = self.now()
                if stage + 1 >= 3:
                    # AND ASK. A bare `DisplayModel` is a question; the
                    # board answers {"DisplayModel":N} on its RESULT, and 0
                    # means the settings this wake relied on are not on the
                    # chip at all (Kitchen, 2026-09-10, after healing from
                    # safeboot). _note_display_model then re-sends them.
                    self._lcd_send(client, topic, "DisplayModel")
                    # AND THE CLOCK GETS ITS TURN BEFORE ANY STANDING LINE
                    # COMES BACK. Stage three IS the clock -- "on activation
                    # let the LCD just show the time and date" -- and
                    # without this the cycle's restore could take the glass
                    # back in the same tick, so the thing the wake exists to
                    # put on the screen would never be seen.
                    self._lcd_clock_until[topic] = \
                        self.now() + DISPLAY_CLOCK_SECONDS
                    # The wake has just said all of this. The loop's first
                    # turn is DISPLAY_REPEAT_SECONDS from now, not in the
                    # same breath.
                    self._lcd_repeated[topic] = self.now()

        # AND THEN TELL EVERY PANEL THE WHOLE TRUTH AGAIN, ON A TIMER.
        #
        # Her ruling, 2026-09-14: "just let it be on loop". Nothing above
        # is consulted and nothing below depends on it -- this re-sends the
        # driver settings and then whatever should be on the glass right
        # now, whether or not the hub believes it is already there. See
        # DISPLAY_REPEAT_SECONDS for why a design this blunt is the right
        # one for a character LCD.
        if DISPLAY_REPEAT_SECONDS > 0:
            for panel in self._lcd_targets:
                topic = str(panel.get("topic") or "")
                if not topic:
                    continue
                held = now_ts < self._lcd_hold_until.get(topic, 0.0)
                # Not while the wake is still walking its stages -- it is
                # saying the same things one tick apart, and two voices
                # sending DisplayModel at once is the one way to make this
                # worse rather than better. A HELD panel's wake is frozen
                # (see above) and says nothing, so it is not skipped: a
                # `for_s: 0` line that arrived mid-wake would otherwise
                # keep this loop off its panel for ever.
                if self._lcd_awake.get(topic, 0) < 3 and not held:
                    continue
                if now_ts - self._lcd_repeated.get(topic, 0.0) \
                        < DISPLAY_REPEAT_SECONDS:
                    continue
                self._lcd_repeated[topic] = now_ts
                # THE DRIVER FIRST -- `DisplayModel`, `DisplayDimmer`. This
                # is the command she has been typing by hand, sent now
                # without waiting for a reboot, a scan or a stage counter
                # to justify it.
                for command in (panel.get("wake") or []):
                    # Only settings that change nothing when already held.
                    # DisplayMode is decided below; DisplayModel RESTARTS
                    # the board, so on a timer it is a reboot loop.
                    if str(command).partition(" ")[0] in ("DisplayMode",
                                                          "DisplayModel"):
                        continue
                    self._lcd_send(client, topic, command)
                # THEN WHATEVER BELONGS ON IT. A standing line if a rule
                # owns the glass; Tasmota's clock if nothing does. Either
                # way the panel is told, rather than assumed. (An unheld
                # standing line is the restore's to bring back, after the
                # clock's turn -- painting it here, unheld, would pin it on
                # the glass with no hold, and with DISPLAY_CLOCK_SECONDS 0
                # would undo "the clock keeps the glass".)
                lines = (self._standing_lines(panel)
                         if held and not self._on_clock_turn(topic) else [])
                if lines:
                    self._paint(panel, lines)
                else:
                    self._lcd_send(client, topic, "DisplayMode 1")

        # ASK EACH PANEL'S BOARD WHAT IS REALLY ON ITS BUS.
        #
        # Last, and never while a rule owns the panel: `I2Cscan` is a
        # question, its answer arrives on stat/<board>/RESULT, and
        # _note_i2c_scan does the reading. Nothing here waits for it --
        # this is one publish per panel every DISPLAY_SCAN_EVERY_SECONDS.
        if DISPLAY_SCAN_EVERY_SECONDS > 0:
            for panel in self._lcd_targets:
                topic = str(panel.get("topic") or "")
                if not topic or panel.get("addr") is None:
                    continue
                if now_ts - self._lcd_scanned.get(topic, 0.0) \
                        < DISPLAY_SCAN_EVERY_SECONDS:
                    continue
                # Only a panel the hub believes it can talk to at all: a
                # board mid-wake has not necessarily brought its I2C bus up
                # yet, and scanning it then would produce exactly the false
                # "nothing there" this is meant to prevent. (A held panel's
                # wake is frozen, not walking: scanned, as the docstring
                # promises for a `for_s: 0` line.)
                if self._lcd_awake.get(topic, 0) < 3 and \
                        now_ts >= self._lcd_hold_until.get(topic, 0.0):
                    continue
                if self._lcd_send(client, topic, "I2Cscan"):
                    self._lcd_scanned[topic] = now_ts

    def _standing_lines(self, panel: Dict[str, Any]
                        ) -> List[Tuple[Any, str, float]]:
        """This panel's standing messages, re-rendered against the live
        cache: [(row, text, hold)]. Drops any whose rule has gone."""
        topic = str(panel.get("topic") or "")
        standing = self._lcd_standing.get(topic) or {}
        by_id = {str(r.get("id", "")): r for r in self.rules}
        by_id.update({str(r["id"]): r for r in self._manual.values()})
        lines: List[Tuple[Any, str, float]] = []
        for key in sorted(standing, key=lambda k: (k is not None, k or 0)):
            entry = standing[key]
            rule = by_id.get(str(entry.get("rule_id", "")))
            # A RULE THAT IS GONE HAS NOTHING ON ANY GLASS. Deleted,
            # switched off, or marked invalid by a failed validation: all
            # three mean the line should not come back, and dropping it here
            # is what stops a deleted rule's message returning every five
            # seconds for the rest of the hub's life.
            if rule is None or not rule.get("enabled", True) \
                    or rule.get("_invalid"):
                standing.pop(key, None)
                continue
            # THE RULE AS IT IS NOW, NOT AS IT WAS WHEN IT LAST FIRED
            # (2026-09-14: "if I want to display, will the text act
            # accordingly?").
            #
            # The standing entry is a SNAPSHOT taken by _show. Re-painting
            # it forever means an edit to the rule's line -- new wording, a
            # different reading, a changed hold -- does not reach the glass
            # until the rule happens to fire again. For a `for_s: 0` line
            # that never hands the panel back, "again" can be days. She
            # would change the text, watch the old text sit there, and have
            # no way to tell whether the edit saved.
            #
            # So the rule is asked afresh on every repaint, matched by the
            # row the entry owns. A rule that no longer writes to that row
            # -- the action deleted, or moved to another line -- has its
            # standing line dropped rather than kept alive by a snapshot of
            # a thing that is not in the rule any more.
            live = next(
                (a for a in actions_of(rule)
                 if a.get("kind") == "display"
                 and str(a.get("device", "")) == str(entry.get("device")
                                                     or a.get("device", ""))
                 and (None if a.get("row") is None
                      else int(a["row"])) == key),
                None)
            if live is None:
                standing.pop(key, None)
                continue
            if live.get("pages"):
                lines.extend(self._page_lines(panel, live, rule,
                                              self._hold_of(live)))
            else:
                lines.append((key, self._fill(str(live.get("text", "")),
                                              rule), self._hold_of(live)))
        if not lines:
            self._lcd_standing.pop(topic, None)
        return lines

    def _raw_line(self, panel: Dict[str, Any], key: Any) -> str:
        """The unrendered text standing on one row of a panel ("" if none)."""
        topic = str(panel.get("topic") or "")
        entry = (self._lcd_standing.get(topic) or {}).get(key) or {}
        rid = str(entry.get("rule_id", ""))
        rule = next((r for r in list(self.rules) + list(self._manual.values())
                     if str(r.get("id", "")) == rid), None)
        if rule is None:
            return ""
        return " ".join(display_words(a) for a in actions_of(rule)
                        if a.get("kind") == "display")

    # ── pages: lines placed left, middle or right (2026-11-05) ─────────

    @staticmethod
    def _align(text: str, cols: int, align: Any) -> str:
        """One line, made safe, cut to the width and placed."""
        line = AutomationEngine._lcd_safe(text).strip()[:cols]
        if align == "middle":
            return line.center(cols)
        if align == "right":
            return line.rjust(cols)
        return line.ljust(cols)

    def _page_lines(self, panel: Dict[str, Any], action: Dict[str, Any],
                    rule: Dict[str, Any], hold: float
                    ) -> List[Tuple[Any, str, float]]:
        """The page now due, every row of the panel: [(row, text, hold)]."""
        topic = str(panel.get("topic") or "")
        pages = display_pages(action)
        slot = int((self._lcd_page.get(topic) or {}).get("slot", 0))
        index = (slot // 2 if action.get("clock_between") else slot) \
            % max(1, len(pages))
        page = pages[index] if pages else []
        cols = int(panel.get("cols") or 20)
        rows = max(1, int(panel.get("rows") or 2))
        out: List[Tuple[Any, str, float]] = []
        for i in range(rows):
            line = page[i] if i < len(page) else {"text": "", "align": "left"}
            text = self._fill(line["text"], rule) if line["text"] else ""
            out.append((i + 1, self._align(text, cols, line.get("align")),
                        hold))
        return out

    def _rotation(self, panel: Dict[str, Any]
                  ) -> Optional[Tuple[Dict[str, Any], Dict[str, Any]]]:
        """(action, rule) when this panel stands on a message that turns
        pages (or shows the clock between); else None."""
        topic = str(panel.get("topic") or "")
        entry = (self._lcd_standing.get(topic) or {}).get(None)
        if not entry:
            return None
        rid = str(entry.get("rule_id", ""))
        rule = next((r for r in list(self.rules) + list(self._manual.values())
                     if str(r.get("id", "")) == rid), None)
        if rule is None:
            return None
        live = next((a for a in actions_of(rule)
                     if a.get("kind") == "display" and a.get("pages")
                     and str(a.get("device", "")) == str(entry.get("device")
                                                         or a.get("device", ""))),
                    None)
        if live is None:
            return None
        if len(display_pages(live)) <= 1 and not live.get("clock_between"):
            return None
        return live, rule

    def _on_clock_turn(self, topic: str) -> bool:
        """True while a several-page message is giving the clock its turn."""
        return bool((self._lcd_page.get(topic) or {}).get("clock"))

    # ── a line written from the app (2026-11-05) ──────────────────────

    def show_now(self, did: str, text: str, hold: float,
                 pages: Optional[List[Any]] = None,
                 page_s: Optional[float] = None,
                 clock_between: bool = False) -> Optional[str]:
        """Write `text` on the panel `did` is part of, from the app. None
        when it went; otherwise why not, in words.

        hold 0 = KEEP IT UP: it stays (no clock turn), its readings kept
        current, and it survives a restart. hold > 0 = for that long, then
        the clock, and it does not come back."""
        action: Dict[str, Any] = {"kind": "display", "device": did,
                                  "for_s": float(hold)}
        if pages:
            action["pages"] = pages
            if page_s is not None:
                action["page_s"] = float(page_s)
            action["clock_between"] = bool(clock_between)
            problem = _pages_problem(action)
        else:
            action["text"] = str(text)
            problem = None if str(text).strip() else \
                "the display action has nothing to show"
        problem = problem or self.display_text_problem(display_words(action))
        if problem:
            return problem
        slug = str(did).split(".", 1)[0]
        panel = next((p for p in self._lcd_targets if p.get("slug") == slug),
                     None)
        if panel is None:
            return "that board has no display this hub knows about"
        if self._pub is None:
            return "the hub is not connected to the boards right now"
        away = self._panel_away(str(panel.get("topic") or ""))
        if away:
            return f"the display is not ready yet ({away}) -- try again in a moment"
        rule = {"id": f"manual:{did}", "name": "From the app",
                "enabled": True, "trigger": {},
                "action": action}
        self._manual[str(did)] = rule
        topic = str(panel.get("topic") or "")
        self._lcd_hold_until.pop(topic, None)    # this replaces what was up
        self._show(rule, rule["action"])
        if float(hold) > 0:
            # FOR A WHILE, THEN THE CLOCK -- and not back again.
            standing = self._lcd_standing.get(topic) or {}
            for k in [k for k, e in standing.items()
                      if e.get("rule_id") == rule["id"]]:
                standing.pop(k, None)
            self._manual.pop(str(did), None)
            self._lcd_live.pop(topic, None)
        else:
            self._lcd_live[topic] = (self.now(), self._standing_lines(panel))
        self._state_dirty = True
        return None

    def clear_now(self, did: str) -> bool:
        """Take the app's line off a panel and give it back to the clock."""
        slug = str(did).split(".", 1)[0]
        panel = next((p for p in self._lcd_targets if p.get("slug") == slug),
                     None)
        rid = f"manual:{did}"
        self._manual.pop(str(did), None)
        if panel is None:
            return False
        topic = str(panel.get("topic") or "")
        standing = self._lcd_standing.get(topic) or {}
        for k in [k for k, e in standing.items() if e.get("rule_id") == rid]:
            standing.pop(k, None)
        self._lcd_live.pop(topic, None)
        self._lcd_page.pop(topic, None)
        self._state_dirty = True
        if self._pub is None:
            return False
        if self._lcd_send(self._pub, topic, "DisplayMode 1"):
            self._lcd_hold_until.pop(topic, None)
            self._lcd_clock_until[topic] = self.now() + DISPLAY_CLOCK_SECONDS
            return True
        return False

    def _paint(self, panel: Dict[str, Any],
               lines: List[Tuple[Any, str, float]]) -> bool:
        """Write these lines to the glass. True when they all went out."""
        client = self._pub
        topic = str(panel.get("topic") or "")
        if client is None or not lines:
            return False
        # DisplayText only renders in mode 0 -- the same first move _show
        # makes, for the same reason.
        payloads = [("DisplayMode", "0")] + [
            ("DisplayText", suffix) for row, text, _hold in lines
            for suffix in self._lcd_rows(text, panel, row=row)]
        return all(self._lcd_publish(client, topic, head, body)
                   for head, body in payloads)

    @staticmethod
    def _hold_of(action: Dict[str, Any]) -> float:
        """A display action's hold in seconds (0 = HOLD_FOREVER)."""
        try:
            return float(action.get("for_s", DISPLAY_HOLD_SECONDS))
        except (TypeError, ValueError):
            return DISPLAY_HOLD_SECONDS

    def _hold_panel(self, topic: str, hold: float) -> None:
        """A rule owns this panel for `hold` more seconds -- or for ever at
        0. The longest hold wins; see _show."""
        until = math.inf if hold == 0 else self.now() + max(1.0, hold)
        self._lcd_hold_until[topic] = max(
            self._lcd_hold_until.get(topic, 0.0), until)

    def _restore(self, panel: Dict[str, Any]) -> None:
        """Put a panel's standing lines back on the glass, re-rendered.

        The returning half of the cycle. Called only from _lcd_tick, only
        for a panel no rule is holding, whose clock turn is over and whose
        wake has finished.
        """
        topic = str(panel.get("topic") or "")
        if topic in self._lcd_page:
            self._lcd_page[topic] = {"slot": 0, "since": self.now()}
        lines = self._standing_lines(panel)
        if not lines or not self._paint(panel, lines):
            # A panel that could not be written to keeps its standing lines
            # and is simply tried again next tick. Nothing is claimed.
            return
        # THE LONGEST HOLD AMONG THE LINES PUT BACK, by the same rule _show
        # uses: the rows are independent, the panel has one mode, so its
        # deadline is the last moment any row still needs mode 0.
        for _row, _text, hold in lines:
            self._hold_panel(topic, hold)

    def _panel_on(self, board_topic: str) -> Optional[Dict[str, Any]]:
        """The panel this board carries, or None."""
        return next((p for p in self._lcd_targets
                     if str(p.get("topic")) == board_topic), None)

    def _panel_away(self, topic: str) -> str:
        """"" when this panel's board can take a line now; else why not: it
        is offline (its last will said so), or it is restarting from the
        wake's own DisplayModel. A line written then is lost."""
        if topic in self._offline:
            return f"{topic} is offline"
        started = self._lcd_restarting.get(topic)
        if started is not None and self.now() - started < self.LCD_RESTART_WAIT:
            return f"{topic} is restarting"
        return ""

    def _panel_absent(self, panel: Dict[str, Any]) -> str:
        """"" if this panel may be written to; a sentence saying why not if
        its board has told us there is nothing at its address.

        THE ONLY EVIDENCE OF ABSENCE ACCEPTED HERE is a scan that really
        answered and really did not list the panel's own address. Every
        other case -- never scanned, no answer, an answer that could not be
        read, a catalogue entry with no address, the scan switched off --
        reads as PRESENT, and the hub writes exactly as it did before this
        existed. A false "your screen is missing" would be a worse bug than
        the one this fixes.
        """
        if DISPLAY_SCAN_EVERY_SECONDS <= 0:
            return ""
        addr = panel.get("addr")
        if addr is None:
            return ""
        seen = self._lcd_bus.get(str(panel.get("topic") or ""))
        if seen is None or int(addr) in seen:
            return ""
        return (f"nothing answers at 0x{int(addr):02X} on "
                f"{panel.get('slug') or panel.get('topic')}'s I2C bus -- "
                f"the bus says nothing is there -- if the screen is dark, "
                f"start with its power and its SDA/SCL wires")

    @staticmethod
    def _lcd_publish(client: Any, topic: str, head: str, body: str) -> bool:
        """Publish one display command with its payload exactly as given
        (DisplayText's trailing padding is what blanks the rest of a row,
        so it must not be stripped). True only when the broker took it."""
        try:
            info = client.publish(f"cmnd/{topic}/{head}", body, qos=0)
        except Exception:                           # noqa: BLE001
            return False
        return getattr(info, "rc", 0) == 0

    @staticmethod
    def _lcd_send(client: Any, topic: str, command: str) -> bool:
        """Publish one display command. True ONLY when the broker actually
        took it.

        paho's publish() does not raise when the client is not connected;
        it returns an info object whose rc says so, and a qos-0 message in
        that state is thrown away. Anything that must know whether a panel
        heard it has to read that rc.
        """
        head, _, body = str(command).partition(" ")
        return AutomationEngine._lcd_publish(client, topic, head, body.strip())

    @staticmethod
    def _lcd_safe(text: str) -> str:
        """Text a character LCD can show one cell per character.

        The panel's ROM is ASCII plus a few symbols and Tasmota sends the
        UTF-8 bytes as they are, so "21.5°C" arrived as two junk cells and
        ran one past the row -- on a 20x4 that spills into another row,
        another rule's line. Accents are folded (é -> e), the degree sign
        dropped, anything else non-ASCII removed. `[` and `]` become
        parentheses: in DisplayText they start a display COMMAND, so a
        reading or a typed line containing one could move the cursor or
        clear the screen."""
        text = str(text).replace("\u00b0", "").replace("[", "(").replace("]", ")")
        text = unicodedata.normalize("NFKD", text)
        return "".join(ch for ch in text if 32 <= ord(ch) < 127)

    @staticmethod
    def _lcd_rows(text: str, panel: Dict[str, Any],
                  row: Optional[int] = None) -> List[str]:
        """The DisplayText payloads for one message -- ONE PER ROW.

        Each carries a single `[x0y<n>]` escape and a full-width line,
        which is the exact shape proved on the board. Four escapes packed
        into one payload was not, and it is the only difference between
        the write that worked by hand and the ones that arrived as
        gibberish.

        ZERO-BASED on the wire, 1-based for a person: "line 2" is what
        somebody means, `y1` is what the panel wants, and the conversion
        happens here and nowhere else.

        No row: the message OWNS the panel -- its line at the top and every
        other row blanked, so nothing of what it interrupted survives
        underneath. A row: that line and nothing else, so several rules can
        each hold one.
        """
        cols = int(panel.get("cols") or 20)
        rows = max(1, int(panel.get("rows") or 2))
        text = AutomationEngine._lcd_safe(text)[:cols].ljust(cols)
        if row is not None:
            here = min(max(1, row), rows)
            return [f"[x0y{here - 1}]{text}"]
        out = [f"[x0y0]{text}"]
        out += [f"[x0y{i}]{'' .ljust(cols)}" for i in range(1, rows)]
        return out

    #: A board that has just come back is re-woken at most this often, and
    #: at most this many times in one hub run. A panel that keeps going out
    #: is not fixed by re-initialising it harder -- and a re-init that is
    #: itself what put the board down would otherwise run for ever.
    LCD_REWAKE_GAP = 120.0
    LCD_REWAKE_LIMIT = 3
    #: How long a wake waits for a board to come back from the restart its
    #: own `DisplayModel` caused, before carrying on regardless.
    LCD_RESTART_WAIT = 45.0
    #: A meter silent this long is warned about (shorter is a board that has
    #: just booted and not found its meter yet).
    METER_SILENT_WARN_S = 180.0
    #: How long after the hub restarted a board an "offline" from the
    #: broker is still that restart, not a drop (its notice can come late:
    #: the broker waits out the board's keepalive).
    LCD_RESTART_NOTICE_S = 150.0

    @staticmethod
    def _restarts_board(commands: List[str]) -> bool:
        """True when these commands include `DisplayModel <n>` -- which in
        Tasmota saves the model and restarts the board. A bare
        `DisplayModel` (no value) is only a question."""
        for c in commands:
            head, _, body = str(c).partition(" ")
            if head == "DisplayModel" and body.strip():
                return True
        return False

    #: Words in Tasmota's restart reason that mean the power came on.
    _POWER_BOOT = ("power on", "poweron", "power-on", "brownout")

    def _note_boot_reason(self, board_topic: str, payload: str) -> None:
        """tele/<board>/INFO3: why it booted. After a POWER-ON boot a
        character LCD can stay dark -- it starts slower than the ESP32 and is
        not found -- and the cure (hers since 2026-09: "no light at all until
        I send DisplayModel 1") is `DisplayModel <n>`, which restarts the
        board once with the panel already powered. Any other boot (the hub's
        own restart, a crash, a reconnect without a boot) leaves the panel
        powered, so nothing restarts it; nor more than once per
        LCD_REWAKE_GAP."""
        try:
            info = json.loads(payload)
            reason = str((info.get("Info3") or info).get("RestartReason") or "")
        except (ValueError, AttributeError):
            return
        if not any(w in reason.lower() for w in self._POWER_BOOT):
            return
        panel = self._panel_on(board_topic)
        client = self._pub
        if client is None:
            return
        if panel is None:
            self._restart_for_late_parts(board_topic, reason)
            return
        model = [c for c in (panel.get("wake") or [])
                 if self._restarts_board([c])]
        now = self.now()
        if not model:
            self._restart_for_late_parts(board_topic, reason)
            return
        if now - self._lcd_restarted_at.get(board_topic, -1e18) \
                < self.LCD_REWAKE_GAP:
            return
        if self._lcd_send(client, board_topic, model[0]):
            # The clear and the clock follow once it is back.
            self._lcd_awake[board_topic] = 1
            self._lcd_restarting[board_topic] = \
                self._lcd_restarted_at[board_topic] = now
            event("AUTO", f"{board_topic} was powered on ({reason}) -- sending "
                          f"{model[0]} so its screen starts: it restarts once, "
                          f"with the screen already powered")

    def _late_parts(self, board_topic: str) -> List[str]:
        """The parts on this board that Tasmota looks for only once, at boot:
        anything on SPI or I2C (card readers, displays, I2C sensors)."""
        if self.esp is None:
            return []
        try:
            from esp_firmware import esphome_catalog as cat
            bus = {cat.C_SPI_CLK, cat.C_SPI_MOSI, cat.C_SPI_MISO,
                   cat.C_I2C_SDA, cat.C_I2C_SCL}
            for slug in self.esp.list_nodes():
                record = self.esp._read_board(slug) or {}
                if record.get("topic") != board_topic:
                    continue
                out = []
                for item in record.get("items") or []:
                    if not isinstance(item, dict):
                        continue
                    dev = cat.BY_ID.get(str(item.get("type_id")))
                    if dev is not None and any(
                            slot.component in bus for slot in dev.slots):
                        out.append(str(item.get("name") or dev.id))
                return out
        except Exception:                              # noqa: BLE001
            return []
        return []

    def _restart_for_late_parts(self, board_topic: str, reason: str) -> None:
        """After a power-on: one restart, so parts that missed their only
        look at boot are found with the power already settled."""
        late = self._late_parts(board_topic)
        now = self.now()
        if not late or self._pub is None or \
                now - self._boot_restarted_at.get(board_topic, -1e18) \
                < self.LCD_REWAKE_GAP:
            return
        if self._lcd_send(self._pub, board_topic, "Restart 1"):
            self._boot_restarted_at[board_topic] = now
            event("AUTO", f"{board_topic} was powered on ({reason}) -- "
                          f"restarting it once so {', '.join(late)} "
                          f"{'is' if len(late) == 1 else 'are'} found with "
                          f"the power settled")

    def _note_info1(self, board_topic: str, payload: str) -> None:
        """tele/<board>/INFO1 -- heal a board that reports a safeboot version."""
        healer = getattr(self, "healer", None)
        if healer is None:
            return
        try:
            info = json.loads(payload)
            version = str((info.get("Info1") or {}).get("Version") or info.get("Version") or "")
        except (ValueError, AttributeError):
            return
        if healer.is_safeboot(version):
            try:
                healer.heal(board_topic, version)
            except Exception as exc:                # noqa: BLE001
                event("OTA", f"could not heal {board_topic}: {exc}")

    #: Commands every full Tasmota answers and the SAFEBOOT build does not
    #: (its whole table is Upgrade, Upload, OtaUrl, SerialLog, Restart --
    #: support_command.ino, kTasmotaCommands under FIRMWARE_MINIMAL).
    _CORE_COMMANDS = ("BACKLOG", "STATUS", "POWER", "SETOPTION", "SO",
                      "GPIO", "GPIOS", "TEMPLATE", "MODULE", "DELAY", "STATE")

    @classmethod
    def looks_like_safeboot(cls, result: Any) -> str:
        """The command a stat/<board>/RESULT refused, when that refusal can
        only come from the safeboot build; "" otherwise."""
        if not isinstance(result, dict):
            return ""
        if str(result.get("Command", "")).strip().lower() != "unknown":
            return ""
        line = str(result.get("Input", "") or "").strip()
        word = re.split(r"[\s;]", line, maxsplit=1)[0].upper() if line else ""
        word = word.rstrip("0123456789") or word
        return line if word in cls._CORE_COMMANDS else ""

    def _note_unknown(self, board_topic: str, result: Dict[str, Any]) -> None:
        """stat/<board>/RESULT -- a core command refused means safeboot."""
        healer = getattr(self, "healer", None)
        if healer is None:
            return
        refused = self.looks_like_safeboot(result)
        if refused:
            try:
                healer.heal(board_topic, f"refused {refused[:40]!r} -- the safeboot build")
            except Exception as exc:                # noqa: BLE001
                event("OTA", f"could not heal {board_topic}: {exc}")

    #: Every hex address in an I2Cscan answer. Tasmota's reply is prose --
    #: `{"I2CScan":"Device(s) found at 0x27 0x3C"}` on a populated bus and
    #: `{"I2CScan":"Error 5, no devices found"}` on an empty one -- so the
    #: addresses are read out of it rather than the sentence being matched.
    #: `0x` is required: the "5" in "Error 5" is not an address.
    _I2C_ADDR_RX = re.compile(r"0x([0-9a-fA-F]{2})")

    #: The two addresses a character-LCD backpack answers on, and the only
    #: two this hub will re-address a panel to. A PCF8574 is 0x27, a
    #: PCF8574A is 0x3F; which one is on the board behind your screen is
    #: decided by a chip marking nobody reads, and it is the single most
    #: common reason a perfectly wired LCD shows nothing. Deliberately NOT
    #: the whole 0x20-0x27 / 0x38-0x3F jumper ranges: those overlap with
    #: other I2C parts, and moving a display onto a sensor's address would
    #: be a worse fault than the one being fixed.
    _LCD_BACKPACKS = frozenset({0x27, 0x3F})

    def _note_i2c_scan(self, board_topic: str, result: Dict[str, Any]) -> None:
        """stat/<board>/RESULT carrying an `I2Cscan` answer.

        HER ASK, 2026-09-14: "the system does I2Cscan to search for display
        if found, then display". Until now the hub took a panel's existence
        entirely on trust -- the board's PLAN said an LCD, so every rule
        that wrote to it reported success, to an empty bus, for ever. Her
        own kitchen screen spent weeks in exactly that state while the
        journal said the lines were being shown.

        Tasmota answers in prose and the addresses are read out of it.

        AN EMPTY BUS IS NOT THE SAME AS A BUS THAT IS NOT UP, and this is
        the distinction the whole method turns on:

          "Device(s) found at 0x27 0x3C"   a real scan: these are there,
                                           anything else is not
          "Error 5, no devices found"      a real scan of a working bus
                                           with nothing on it -- what an
                                           empty bus with no pull-ups says,
                                           and what her kitchen board says
                                           today. Evidence of absence.
          "Error 1, I2C not enabled"       NOT evidence of anything about
          "...bus busy", anything else     the screen. The bus itself is
                                           not up, or the scan could not
                                           run: the panel may be perfectly
                                           well wired to pins the firmware
                                           is not driving. Recorded as
                                           unknown, and said once, because
                                           the fix is the TEMPLATE and not
                                           the screen.

        So only the first two ever reach `_lcd_bus`; everything else leaves
        it as None and writing carries on exactly as before.

        WHAT IS NOT READ HERE: an answer with no "I2CScan" key at all.
        Every other RESULT this board sends flows through here, and reading
        "no addresses" out of a Power or a Dimmer reply would condemn a
        perfectly good panel on the strength of an unrelated message.
        """
        if "I2CScan" not in result:
            return
        said = str(result.get("I2CScan") or "")
        found = frozenset(int(h, 16)
                          for h in self._I2C_ADDR_RX.findall(said))
        if not found and "no devices" not in said.lower():
            # The scan did not run. Say why once -- a bus that is not
            # enabled is the lost-template fault, and it has a known fix --
            # then leave the panel exactly as writable as it was.
            if board_topic not in self._lcd_nobus_said:
                self._lcd_nobus_said.add(board_topic)
                event("AUTO", f"{board_topic} could not scan its I2C bus "
                              f"({said.strip()[:60]!r}) -- that is the BUS, "
                              f"not the screen: on its console `GPIO` should "
                              f"show I2C SDA and I2C SCL on two pins, and if "
                              f"they are None its template is gone (Settings "
                              f"> Edit a board > this board, keep everything, "
                              f"send). Its panel is still being written to "
                              f"meanwhile, since nothing here says the screen "
                              f"is missing")
            return
        self._lcd_nobus_said.discard(board_topic)
        was = self._lcd_bus.get(board_topic)
        self._lcd_bus[board_topic] = found
        panel = self._panel_on(board_topic)
        if panel is None or panel.get("addr") is None:
            return
        addr = int(panel["addr"])
        if addr in found:
            # BACK FROM THE DEAD. A screen that was plugged in, powered or
            # rewired since the last scan says so here, and it is worth a
            # line because its absence was worth one.
            if board_topic in self._lcd_absent_said:
                self._lcd_absent_said.discard(board_topic)
                event("AUTO", f"{board_topic}: 0x{addr:02X} is answering "
                              f"again -- its panel is back and rules that "
                              f"write to it will be shown")
            return
        # THE PANEL IS THERE, AT A DIFFERENT NUMBER (2026-09-14).
        #
        # A character LCD's backpack is a PCF8574 at 0x27 or a PCF8574A at
        # 0x3F, and which one you have is decided by a chip marking nobody
        # reads. The catalogue configures 0x27, so a board carrying the 'A'
        # part scanned clean, answered at 0x3F, matched nothing, and was
        # declared missing -- and the fix she was performing by hand in the
        # console is one command: `DisplayAddress 63`.
        #
        # The hub knows both numbers and can see which one answered, so it
        # sends the correction itself rather than reporting a fault. Once
        # per run: if the panel still does not answer after being told its
        # own address, the next thing to suspect is a wire, and repeating a
        # setting would only hide that.
        # Only a panel that is itself a backpack LCD: anything else found at
        # 0x27/0x3F (another part on the same bus) is not its "other number".
        wrong_number = (sorted(found & self._LCD_BACKPACKS)
                        if addr in self._LCD_BACKPACKS else [])
        client = self._pub
        if wrong_number and client is not None \
                and board_topic not in self._lcd_readdressed:
            self._lcd_readdressed.add(board_topic)
            right = wrong_number[0]
            event("AUTO", f"{board_topic}: its panel is configured for "
                          f"0x{addr:02X} and the bus answers at "
                          f"0x{right:02X} -- that is the other backpack "
                          f"(PCF8574 is 0x27, PCF8574A is 0x3F). Sending "
                          f"`DisplayAddress {right}` and re-waking it")
            if self._lcd_send(client, board_topic, f"DisplayAddress {right}"):
                panel["addr"] = right
                # A NEW ADDRESS IS A NEW DRIVER. Re-run the wake from the
                # start -- the whole point is to re-initialise against the
                # chip that is actually there.
                self._lcd_awake.pop(board_topic, None)
                self._lcd_absent_said.discard(board_topic)
            return
        if board_topic in self._lcd_absent_said:
            return                                  # said once, per run
        self._lcd_absent_said.add(board_topic)
        others = (", ".join(f"0x{a:02X}" for a in sorted(found))
                  if found else "nothing at all")
        event("AUTO", f"{board_topic} scanned its I2C bus and found {others} "
                      f"-- its panel is configured for 0x{addr:02X}, which "
                      f"is not there. Rules will STILL write to it -- a scan "
                      f"is a guess about hardware and a wrong guess must not "
                      f"cost you a working screen -- but the journal will say "
                      f"'sent' rather than 'showed' until the bus agrees. "
                      f"Check the screen's own power and its SDA/SCL "
                      f"wires first; if the backpack is a PCF8574A it "
                      f"answers on 0x3F, and `DisplayAddress 63` is the "
                      f"setting for that. Re-checked every "
                      f"{int(DISPLAY_SCAN_EVERY_SECONDS)}s, so it comes back "
                      f"by itself once it answers")
        if was is not None and was != found:
            event("AUTO", f"{board_topic} I2C bus changed: {sorted(was)} -> "
                          f"{sorted(found)}")

    def _mid_install(self, board_topic: str) -> bool:
        """A board not installed yet belongs to the CABLE: its install sends
        its settings in order, and anything sent over the air meanwhile
        (DisplayModel before its I2C pins exist, PowerDelta before the
        meter's) was refused -- and used up the panel's one-shot heal
        (Dining, 2026-11-02). The install itself sends what these would."""
        esp = getattr(self, "esp", None)
        if esp is None:
            return False
        try:
            for slug in esp.list_nodes():
                record = esp._read_board(slug) or {}
                if record.get("topic") == board_topic:
                    return not record.get("flashed")
        except Exception:                           # noqa: BLE001
            return False
        return False

    def _note_display_model(self, board_topic: str, result: Dict[str, Any]) -> None:
        """stat/<board>/RESULT from a board the hub owns a panel on.

        `{"DisplayModel":0}` means the chip holds NO display settings: the
        driver is not configured, so every wake -- backlight, clear, clock
        -- lands on nothing and the glass stays dark. That is not a reboot
        the soft wake can fix; it is the install's settings gone. Seen on
        Kitchen (2026-09-10) the minute it healed from safeboot: Wi-Fi,
        broker account and topic all intact, panel settings not. The
        answer is the catalogue's own setup lines again (address, geometry,
        model, dimmer -- everything but the mode, which the wake's clear
        and clock stages then supply), sent ONCE per hub run: a board that
        still says 0 afterwards runs a firmware with no display driver,
        and that is said once rather than re-sent for ever.

        The question is asked by the wake itself: its last stage sends a
        bare `DisplayModel`, and the board's answer comes back through
        here. `{"Command":"Unknown","Input":"DISPLAYMODEL"}` from a panel
        board is the same fault one level down -- a build without
        USE_DISPLAY -- and is said once too.
        """
        if self._mid_install(board_topic):
            return
        panel = self._panel_on(board_topic)
        if panel is None:
            return
        client = self._pub
        if "DisplayModel" in result:
            try:
                model = int(result.get("DisplayModel"))
            except (TypeError, ValueError):
                return
            if model != 0:
                return
            # Answered 0: DisplayModel was refused, so nothing is restarting.
            self._lcd_restarting.pop(board_topic, None)
            setup = [str(c) for c in (panel.get("setup") or [])
                     if str(c).partition(" ")[0] != "DisplayMode"]
            if board_topic in self._lcd_resent:
                if board_topic not in self._lcd_resent_said:
                    self._lcd_resent_said.add(board_topic)
                    # Tasmota decides the model at boot by DETECTION: the
                    # LCD driver scans the I2C bus for 0x27/0x3F and sets
                    # model 1 itself when it finds the backpack, whatever
                    # the saved setting says; and `DisplayModel 1` by hand
                    # is refused (answered 0) when the I2C bus is not up.
                    # So a 0 that survives the re-send is a bus or a wire,
                    # not a setting (Kitchen, 2026-09-10 15:37).
                    event("AUTO", f"{board_topic} refused DisplayModel 1 -- its I2C bus is not up or no "
                                  f"panel answers on it. On its console: `GPIO` must show I2C SCL and "
                                  f"I2C SDA on two pins (if they are None its wiring is gone: Settings > "
                                  f"Edit a board > this board, keep everything, send -- it restarts and "
                                  f"finds the panel by itself); then `I2Cscan` must list 0x27 or 0x3F "
                                  f"(if not, the screen's power or wires). Only if both are right is it "
                                  f"the firmware (needs USE_DISPLAY_LCD). Nothing more sent this run")
                return
            if not setup or client is None:
                return                           # nothing to send with
            self._lcd_resent.add(board_topic)
            event("AUTO", f"{board_topic} answers DisplayModel 0 -- its display settings are "
                          f"gone from the chip; re-sending {', '.join(setup)}")
            if all(self._lcd_send(client, board_topic, c) for c in setup):
                # The re-init went out: the clear and the clock follow as
                # stages two and three once the board is back from the
                # restart DisplayModel causes.
                self._lcd_awake[board_topic] = 1
                if self._restarts_board(setup):
                    self._lcd_restarting[board_topic] = \
                        self._lcd_restarted_at[board_topic] = self.now()
            return
        if str(result.get("Command", "")).strip().lower() == "unknown" and \
                str(result.get("Input", "")).upper().startswith("DISPLAY"):
            if board_topic in self._lcd_nodriver_said:
                return
            self._lcd_nodriver_said.add(board_topic)
            event("AUTO", f"{board_topic} does not know {str(result.get('Input'))[:24]!r} -- "
                          f"either it is in safeboot (then it is being healed, see the OTA lines) "
                          f"or its firmware has no display driver (needs the Omniscience build with "
                          f"USE_DISPLAY and USE_DISPLAY_LCD); its panel waits until it runs that image")

    def probe_boards(self, publish: Callable[[str, str], Any]) -> int:
        """Ask every known board `Status 2` once. A full firmware answers
        STATUS2 (ignored here); the safeboot build answers Unknown, which
        _note_unknown turns into the heal. Run at each broker connection,
        so a board stranded BEFORE this hub started is found within a
        second of the hub coming up, not on its next reboot. Returns the
        number of boards asked."""
        asked = 0
        try:
            slugs = list(self.esp.list_nodes()) if self.esp is not None else []
        except Exception:                           # noqa: BLE001
            slugs = []
        for slug in slugs:
            try:
                rec = self.esp._read_board(slug) or {}
                topic = rec.get("topic")
                if topic:
                    publish(f"cmnd/{topic}/Status", "2")
                    asked += 1
            except Exception:                       # noqa: BLE001
                continue
        return asked

    #: How often one board may have its settings re-asserted. The same
    #: shape as LCD_REWAKE_GAP and for the same reason: a board rebooting
    #: in a loop must not be chased around it.
    SETTINGS_GAP = 120.0

    def _reassert_settings(self, board_topic: str) -> None:
        """Send a returning board the behaviour settings it must hold.

        Never raises: this is maintenance, and maintenance that can take
        the message loop down with it is worse than none.

        WHAT MAKES IT SAFE TO REPEAT. Every command comes from
        EspBuilder.maintenance_commands, which admits only settings that
        are idempotent, carry no credential, and re-initialise nothing --
        so a board already holding them changes nothing and writes no
        flash. DisplayModel is deliberately excluded there; it is the one
        that makes gibberish and reboot loops.

        SAID, NOT SILENT. A hub that quietly reconfigures hardware is a hub
        nobody can debug, so the first time a board is corrected in a run
        it says which commands went and why.
        """
        client = self._pub
        if client is None or self.esp is None:
            return
        now = self.now()
        if now - self._settings_sent.get(board_topic, 0.0) < self.SETTINGS_GAP:
            return
        try:
            slug = next((s for s in self.esp.list_nodes()
                         if (self.esp._read_board(s) or {}).get("topic")
                         == board_topic), None)
            if slug is None:
                return
            record = self.esp._read_board(slug) or {}
            commands = list(self.esp.maintenance_commands(record))
        except Exception:                           # noqa: BLE001
            return                                  # an older builder
        # AND HOW TO READ ITS INPUT PINS (2026-09-13). SetOption114 and
        # SwitchMode<n> are sent once at install and were never re-sent --
        # while the board that lost its DISPLAY settings healing out of
        # safeboot proves settings go missing one group at a time. A PIR
        # that loses SetOption114 stops publishing entirely, so every rule
        # written for it goes quiet, and nothing anywhere says why.
        #
        # Separately, and not fatally: an older builder without
        # switch_commands still gets everything above.
        try:
            commands += self.esp.switch_commands(self.esp._plan_for(slug))
        except Exception:                           # noqa: BLE001
            pass
        if not commands:
            return
        self._settings_sent[board_topic] = now
        # THE SAME PUBLISHER THE PANEL USES. A first draft here built the
        # topic as `cmnd/<board>/Sleep/0` with an empty payload -- which is
        # not Tasmota's shape at all (the command is the TOPIC, the value
        # is the PAYLOAD), so it would have published nothing anybody
        # listens for, silently, for ever. _lcd_send splits it correctly
        # and reads paho's rc, which is the only way to know a qos-0
        # message was not simply thrown away.
        sent = sum(1 for command in commands
                   if self._lcd_send(client, board_topic, command))
        if sent and board_topic not in self._settings_said:
            self._settings_said.add(board_topic)
            event("AUTO", f"{board_topic} is back -- re-asserting "
                          f"{', '.join(commands)} (these are lost when a "
                          f"board heals out of safeboot, and nothing else "
                          f"puts them back)")

    #: A board clock this far from the hub's is set again, at most once
    #: per BOARD_TIME_GAP_S per board.
    BOARD_TIME_SLACK_S = 120.0
    BOARD_TIME_GAP_S = 600.0

    def _hub_clock_trusted(self) -> bool:
        """The hub's clock is right: past 2026-01-01 and not marked wrong
        by hub_clock (a power cut with no internet)."""
        if self.now() < 1_767_225_600:
            return False
        guard = getattr(sys.modules.get("app.ws_server"), "_CLOCK", None)
        return not getattr(guard, "suspect", False)

    def _note_board_time(self, board_topic: str, text: str) -> None:
        """A board's state said its time. A Tasmota board gets the time only
        from the internet (NTP); without it the clock starts at 1970. When it
        is more than BOARD_TIME_SLACK_S off, the hub sends its own time and
        UTC offset (`Timezone`, `Time <epoch>`)."""
        m = re.search(r'"Time"\s*:\s*"(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d)',
                      text)
        if not m or self._pub is None or not self._hub_clock_trusted():
            return
        try:
            board = datetime.datetime.strptime(
                m.group(1), "%Y-%m-%dT%H:%M:%S").timestamp()
        except (ValueError, OverflowError, OSError):
            board = 0.0
        now = self.now()
        if abs(board - now) <= self.BOARD_TIME_SLACK_S:
            return
        last = self._board_time_sent.get(board_topic)
        if last is not None and self.mono() - last < self.BOARD_TIME_GAP_S:
            return
        self._board_time_sent[board_topic] = self.mono()
        off = (time.localtime(now).tm_gmtoff or 0) // 60
        zone = f"{'+' if off >= 0 else '-'}{abs(off) // 60:02d}:{abs(off) % 60:02d}"
        self._lcd_send(self._pub, board_topic, f"Timezone {zone}")
        self._lcd_send(self._pub, board_topic, f"Time {int(now)}")
        event("AUTO", f"{board_topic}'s clock said {m.group(1)}: set from "
                      f"the hub's")

    def _note_presence(self, board_topic: str, payload: str,
                       *, retained: bool = False) -> None:
        """A board's LWT. The only thing done with it: WAKE ITS PANEL AGAIN
        after the board has been away.

        A character LCD that finishes booting after the ESP32 does misses
        its own init and comes up dark, or lit and full of rubbish -- "when
        I power cycle, no light at all until I send DisplayModel 1". The
        wake runs once per panel per hub run, so before this the only cure
        for a board reboot was a hub restart.

        NOT ON A REPLAYED Online. Tasmota's LWT is retained, so the broker
        hands the hub a copy the moment it subscribes; re-waking on that
        would re-initialise every panel on every hub reconnect, which is
        the flicker `refresh_lcd_targets` was fixed for in the first place.

        THAT RULE USED TO BE WRITTEN AS "only offline -> online", and the
        proxy was wrong in the one case that mattered (2026-09-12):

            "at first it shows gibberish then after a power cycle it shows
             the right display of time and date"

        Power the house up and the hub subscribes before the board is on
        the network. What arrives is the RETAINED Online from the board's
        previous life -- stale, but it records `online`. The wake's three
        stages then run on their timer into an empty room: `_lcd_send`
        returns True because the BROKER took each publish, and the broker
        takes a publish whether or not anybody is subscribed to it, so all
        three stages are spent and the panel is marked done for the rest of
        the hub run. The board finally boots, its character LCD misses its
        own init and comes up full of rubbish, and it publishes a live
        Online -- which this method dropped, because `was` was already
        "online". Nothing ever woke it again.

        Power-cycle the BOARD and it works, which is the other half of her
        sentence: the hub sees a real LWT Offline, then Online, the
        transition test passes, and the re-wake lands on a board that has
        just proved it is listening.

        So the test is the broker's own retain flag, which is what the
        proxy was always standing in for: a LIVE Online means the board is
        listening RIGHT NOW and is the best moment there will ever be to
        redraw its screen. A replayed one still only records where we are.

        AND THE RE-WAKE IS THE SOFT ONE -- no `DisplayModel`. In Tasmota
        that command re-initialises the driver and raises the restart flag,
        so answering a board's return by sending it is a loop with a
        fifteen-second period: model, restart, LWT Offline, LWT Online,
        model again. That is exactly "it keeps going on and off"
        (2026-09-04), and it is why the geometry and the model are NOT
        re-sent here: they live in the board's own flash and survived the
        reboot. What a panel actually needs on the way back is its
        backlight and a clean screen, which is what the soft wake sends.
        Two more guards sit behind that, for whatever else can put a board
        down: no more than one re-wake per LCD_REWAKE_GAP, and no more than
        LCD_REWAKE_LIMIT of them in a hub run -- after that the board is
        left alone and said once, because a board rebooting on a loop is a
        fault to go and look at, not a screen to keep redrawing.
        """
        state = payload.strip().lower()
        if state not in ("online", "offline"):
            return
        was = self._lcd_seen.get(board_topic)
        self._lcd_seen[board_topic] = state
        if state == "offline":
            self._offline.add(board_topic)
            self._offline_since.setdefault(board_topic, self.now())
            # What a device "has been" for N minutes is unknown while its
            # board is away: the stretch is dropped, and starts again from
            # the first report after it returns.
            for did in (self._index.get(board_topic) or {}).values():
                for wk in [w for w in list(self._since) if w[0] == did]:
                    self._since.pop(wk, None)
            started = self._lcd_restarted_at.get(board_topic)
            if started is not None and \
                    self.now() - started < self.LCD_RESTART_NOTICE_S:
                # The hub's own DisplayModel restarted it: expected, brief.
                # (The broker may say so up to a minute or more after.)
                event("AUTO", f"{board_topic} is restarting -- the hub sent it "
                              f"its display settings "
                              f"{self.now() - started:.0f}s ago, which restarts "
                              f"the board; back in a few seconds")
            else:
                event("AUTO", f"{board_topic} went offline -- its readings are "
                              f"treated as unknown until it speaks again")
        else:
            self._offline.discard(board_topic)
            self._offline_since.pop(board_topic, None)
        if state != "online":
            return
        # A REPLAY SAYS NOTHING ABOUT NOW. Record it and stop -- unless it
        # is telling us the board came back from an Offline we actually
        # watched happen, which is a real transition however it reached us.
        if retained and was != "offline":
            return
        # OUR OWN RESTART COMING BACK. The wake sent DisplayModel, the board
        # restarted, and this Online is it returning: the wake carries on
        # from where it paused rather than treating it as a new return
        # (which would re-send DisplayModel and restart it again).
        started = self._lcd_restarting.pop(board_topic, None)
        if started is not None and self.now() - started < self.LCD_RESTART_WAIT:
            return

        # A BOARD THAT IS BACK GETS ITS SETTINGS RE-ASSERTED (2026-09-12).
        #
        #     "i need fix boards to be automatic to constantly be fixing
        #      the boards"
        #
        # Right instinct, wrong word -- and this file carries the scar.
        # Re-asserting panel settings on a TIMER is exactly what "the LCD
        # comes off and on on its own" was (2026-08-27), and re-sending
        # DisplayModel on every return is a fifteen-second reboot loop
        # (2026-09-04). A clock re-does work nobody asked for, for ever.
        #
        # The moment that actually matters is a board COMING BACK, because
        # that is when settings go missing: a reboot, a power cut, a heal
        # out of safeboot (Kitchen, 2026-09-10 -- Wi-Fi and broker account
        # intact, display settings gone). So the re-assert rides the same
        # live Online the panel's wake does, and a house whose boards stay
        # up sends nothing at all.
        #
        # BEFORE the panel check below, deliberately: a board with no
        # screen still needs its Sleep 0, and that early return would skip
        # every one of them.
        if self._mid_install(board_topic):
            return                  # the install sends these itself
        self._reassert_settings(board_topic)
        # AND WHAT IT IS DOING NOW. While it was away its devices' stored
        # values are the last thing it said; a relay may have been switched
        # at the wall, a dimmer restored to a different level. "State" makes
        # Tasmota publish its whole state at once (POWER<n>, Channel), which
        # feed() reads like any report -- so the first command after it
        # returns starts from the truth, not from before it left.
        if self._pub is not None:
            self._lcd_send(self._pub, board_topic, "State")

        if self._panel_on(board_topic) is None:
            return                       # no panel on it; presence is noise
        now = self.now()
        if now - self._lcd_rewoke.get(board_topic, 0.0) < self.LCD_REWAKE_GAP:
            return                       # too soon: it is flapping
        count = self._lcd_rewakes.get(board_topic, 0) + 1
        self._lcd_rewakes[board_topic] = count
        if count > self.LCD_REWAKE_LIMIT:
            if count == self.LCD_REWAKE_LIMIT + 1:
                event("AUTO", f"{board_topic} has come back {count} times -- "
                              f"leaving its panel alone now; a board that "
                              f"keeps restarting is the thing to look at")
            return
        self._lcd_rewoke[board_topic] = now
        self._lcd_awake.pop(board_topic, None)
        # AND DROP ANY HOLD (2026-09-14). The wake loop skips a panel a
        # rule is holding, which was harmless while a hold was thirty
        # seconds and is a trap now that it is five minutes or forever: the
        # board REBOOTED, so whatever was on the glass is gone with it, and
        # a hold kept for a message that no longer exists would leave the
        # panel showing its power-on garbage until the hold expired -- for
        # a `for_s: 0` line, permanently.
        #
        # Dropping it here rather than teaching the wake loop to ignore
        # holds: a hold means "a rule owns this glass", and after a reboot
        # no rule does.
        self._lcd_hold_until.pop(board_topic, None)
        # THE STANDING LINES ARE KEPT, THOUGH (2026-09-14). The hold is
        # about who owns the glass right now; the standing set is about what
        # this panel is FOR, and a reboot does not change that. So the wake
        # runs, the clock takes stage three, and the cycle puts the reading
        # back a few seconds later -- which is what a person expects from a
        # panel whose board blinked. The clock deadline is cleared so that
        # turn is measured from the wake finishing, not from whenever the
        # last hold happened to run out.
        self._lcd_clock_until.pop(board_topic, None)
        # AND THE BUS IS RE-ASKED as soon as the wake finishes: a board that
        # has just restarted may have come up with a panel that was not
        # there before, or lost one that was. The last scan's answer is
        # about the board as it was before it rebooted.
        self._lcd_scanned.pop(board_topic, None)
        event("AUTO", f"{board_topic} is back -- relighting its panel")

    def boards_changed(self) -> None:
        """A board was just registered, renamed or removed. Re-read both
        directories NOW instead of waiting for the five-minute sweep.

        Both, because they go stale together: the index is how a new
        board's readings are understood at all, and the panel list is how
        its display gets woken. Five minutes of neither is what "the LCD
        commands are not working" looked like from the outside -- the wake
        was correct and simply had nothing to wake yet, because the panel
        was registered after the engine last looked (2026-09-04).
        """
        try:
            self.rebuild_index()
        except Exception:                           # noqa: BLE001
            pass
        self.refresh_lcd_targets()

    def _boards_signature(self) -> Optional[tuple]:
        """(name, mtime, size) of every record in the boards directory --
        one directory listing, no JSON read."""
        if self.esp is None:
            return None
        try:
            base = self.esp._dir()
            out = []
            with os.scandir(base) as it:
                for entry in it:
                    if entry.name.endswith(".json") and entry.is_file():
                        st = entry.stat()
                        out.append((entry.name, st.st_mtime_ns, st.st_size))
            return tuple(sorted(out))
        except (OSError, AttributeError, TypeError):
            return None

    def _check_boards(self) -> None:
        """A board record changed on disk -- an edit, a removal, a retired
        board, a new part -- so the topic index is rebuilt NOW. Only the
        paths that registered a NEW device used to tell the engine; every
        other change left the index up to five minutes stale, and a sensor
        removed from a board went on being read as the old one."""
        sig = self._boards_signature()
        if sig is None:
            return
        if self._boards_sig is None:
            self._boards_sig = sig
            return
        if sig != self._boards_sig:
            self._boards_sig = sig
            self.boards_changed()

    def refresh_lcd_targets(self) -> None:
        """Which boards carry a panel. Read with the index, not per tick --
        it is a directory walk."""
        try:
            self._lcd_targets = (self.esp.display_targets()
                                 if self.esp is not None else [])
        except Exception:                           # noqa: BLE001
            self._lcd_targets = []
        # A PANEL ALREADY AWAKE STAYS AWAKE. This used to clear the whole
        # map, and this method runs every five minutes -- so every panel
        # was re-initialised, blanked and restarted on a timer forever.
        # That is the "the LCD comes off and on on its own" she reported
        # (2026-08-27), and it was the hub doing it. Only topics that have
        # GONE are forgotten; a panel registered a minute ago has no entry
        # yet and is woken by the next tick, which is the whole point of
        # calling this the moment a board finishes registering.
        topics = {str(p.get("topic")) for p in self._lcd_targets
                  if p.get("topic")}
        # EVERYTHING ELSE KEYED BY PANEL TOPIC GOES THE SAME WAY, for the
        # same reason: a board that has been removed or renamed must not
        # leave a hold, a standing line, a clock deadline or a bus reading
        # behind it. Each of these was added on 2026-09-14 and every one of
        # them would otherwise grow for the life of the hub.
        # (_lcd_awake is pruned IN PLACE like the rest: rebinding it to a
        # new dict from this thread lost any stage the engine thread wrote
        # meanwhile, and a lost stage 1 re-sends DisplayModel -- a restart.)
        for table in (self._lcd_awake, self._lcd_hold_until, self._lcd_standing,
                      self._lcd_clock_until, self._lcd_bus,
                      self._lcd_scanned, self._lcd_repeated,
                      self._lcd_restarting, self._lcd_restarted_at):
            # list(): this runs on the app's thread (boards_changed) while
            # the engine thread may be adding to the same tables.
            for gone in [t for t in list(table) if t not in topics]:
                table.pop(gone, None)

    def tick(self) -> None:
        """The clock's half: persist stretches maturing, absences growing
        long, times of day arriving -- and reverts coming due. Call every
        few seconds."""
        try:
            self._reload_if_changed()       # another process saved a rule
        except Exception:                           # noqa: BLE001
            traceback.print_exc()
        try:
            self._rebase_clock()
        except Exception:                           # noqa: BLE001
            traceback.print_exc()
        try:
            self._check_boards()
        except Exception:                           # noqa: BLE001
            traceback.print_exc()
        self._run_reverts()
        try:
            for rid in list(self._runs):
                self._check_run(rid)    # another process may have changed it
        except Exception:                           # noqa: BLE001
            traceback.print_exc()
        if self._state_dirty and self.now() - self._state_saved >= 10.0:
            self._save_state()      # extensions are written in batches
        self.pending_approvals()   # drops the ones nobody answered
        try:
            self._lcd_tick()
        except Exception:                           # noqa: BLE001
            # A panel is decoration: a fault in it must not stop the time,
            # persist and absent triggers below from being evaluated.
            traceback.print_exc()
        try:
            self._draw_tick()
        except Exception:                           # noqa: BLE001
            traceback.print_exc()
        ts = self.now()
        prev_tick, self._last_tick_ts = self._last_tick_ts, ts
        if ts - self._history_saved >= self.HISTORY_SAVE_EVERY:
            # Stamped whether or not the write works: a failing disk used
            # to be retried, and logged, on every five-second tick.
            self._history_saved = ts
            self._save_history()
            self._flush_rules()
        for rule in list(self.rules):
            if not rule.get("enabled", True) or rule.get("_invalid"):
                continue
            t = rule.get("trigger", {})
            kind = t.get("kind")
            rid = str(rule.get("id", ""))
            try:
                if kind in ("persist", "absent") \
                        and self.is_offline(str(t.get("device"))):
                    continue        # silence from an absent board is not quiet
                if kind == "persist":
                    wk = (str(t.get("device")), t.get("value", "on"))
                    since = self._since.get(wk)
                    if since is not None \
                            and self._persist_done.get(rid) != since \
                            and ts - since >= float(t.get("for_s", 0)):
                        if self._fire(rule):
                            self._persist_done[rid] = since
                elif kind == "absent":
                    wk = (str(t.get("device")), t.get("value", "on"))
                    last = self._activity.get(wk)
                    # NOT WHILE IT IS STILL SO: a sensor held on reports once
                    # and then says nothing, and that silence is not quiet.
                    if last is not None and wk not in self._since \
                            and self._absent_done.get(rid) != last \
                            and ts - last >= float(t.get("for_s", 0)):
                        if self._fire(rule):
                            # quiet stays quiet: re-arm only on the next event
                            self._absent_done[rid] = last
                elif kind == "time":
                    due = self._time_due(str(t.get("at", "")), ts, prev_tick)
                    if due is not None and self._time_fired.get(rid) != due:
                        if self._fire(rule):
                            self._time_fired[rid] = due
            except Exception:                       # noqa: BLE001
                # One bad rule must not stop every rule after it, on every
                # tick -- the same guard _observe has.
                traceback.print_exc()
                self._note(rule, "crashed while evaluating -- skipped")

    #: How far back a missed tick may still fire a time trigger. Covers the
    #: listener's broker backoff (up to a minute) with room to spare; a hub
    #: that was stopped for longer does not replay the day's appointments.
    TIME_CATCHUP_SECONDS = 600.0

    def _time_due(self, at: str, ts: float,
                  prev_tick: Optional[float]) -> Optional[str]:
        """The occurrence of a daily HH:MM that is due at `ts`, as
        "YYYY-MM-DD@HH:MM", or None.

        Due means: within its own minute, OR passed since the previous
        tick. It used to be "the current minute equals HH:MM", so a tick
        that skipped the minute -- the listener waits up to 60s between
        broker reconnects -- lost that day's firing entirely. Local time
        via mktime, so DST is honoured (a time that does not exist on a
        spring-forward day fires at the equivalent moment after the jump).
        """
        if not _hhmm_ok(at):
            return None
        hh, mm = (int(x) for x in at.split(":"))
        lt = time.localtime(ts)
        # Today's occurrence, and yesterday's for a gap across midnight.
        for back in (0, 1):
            target = time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday - back,
                                  hh, mm, 0, 0, 0, -1))
            if target > ts:
                continue
            in_minute = ts - target < 60.0
            missed = (prev_tick is not None and target > prev_tick
                      and ts - target <= self.TIME_CATCHUP_SECONDS)
            if in_minute or missed:
                day = time.strftime("%Y-%m-%d", time.localtime(target))
                return f"{day}@{_hhmm(at)}"
        return None

    # ── conditions and firing ───────────────────────────────────────────

    def _cooldown_for(self, rule: Dict[str, Any]) -> float:
        """How long this rule must wait between firings.

        The ordinary answer is `cooldown_s` -- a minute -- and it exists so
        that a sensor reporting once a second cannot drive a relay once a
        second.

        A `change`-TRIGGERED RULE THAT ONLY SHOWS THINGS IS THE EXCEPTION
        (2026-09-14). Its whole purpose is to keep a reading in front of a
        person, and a minute between updates is not a live reading. What
        earns the shorter wait is the ACTIONS, never the trigger: writing a
        line to a panel fifty times a minute costs a few MQTT messages,
        while driving a relay fifty times a minute destroys the relay. So a
        rule that only displays and notifies gets CHANGE_COOLDOWN_SECONDS,
        and a rule that touches a device keeps the full minute even when it
        is triggered the same way.

        EVERY branch is counted -- then, else and revert. A rule whose THEN
        only shows but whose ELSE switches something is a rule that
        switches something.
        """
        if (rule.get("trigger") or {}).get("kind") != "change":
            return self.cooldown_s
        for branch in ("then", "else", "revert"):
            for act in actions_of(rule, branch):
                # ONLY A PANEL IS CHEAP TO UPDATE (2026-10-01). A `notify`
                # reaches a PHONE: "tell me when it changes" at one message
                # every two seconds buried the phone, so a rule that
                # notifies keeps the full wait like one that drives a relay.
                if act.get("kind") not in ("display", "wait"):
                    return self.cooldown_s
        return CHANGE_COOLDOWN_SECONDS

    def is_offline(self, did: str) -> bool:
        """True while the board carrying this device has said Offline and
        not spoken since."""
        if not self._offline:
            return False
        did = str(did)
        return any(did in (self._index.get(t) or {}).values()
                   for t in list(self._offline))

    def _conditions_hold(self, rule: Dict[str, Any]) -> bool:
        """Every "only if" of a rule holds (`condition` and `conditions`)."""
        for cond in [rule.get("condition")] + list(rule.get("conditions") or []):
            if cond and not self._condition_holds(cond):
                return False
        self._cond_why = ""
        return True

    def _condition_holds(self, cond: Optional[Dict[str, Any]]) -> bool:
        self._cond_why = ""         # why NOT, in words, for the journal
        if not cond:
            return True
        if cond.get("kind") == "time_range":
            lt = time.localtime(self.now())
            now_m = lt.tm_hour * 60 + lt.tm_min
            a, b = _minutes(cond["after"]), _minutes(cond["before"])
            return (a <= now_m <= b) if a <= b else (now_m >= a or now_m <= b)
        if cond.get("kind") == "device":
            did = str(cond.get("device", ""))
            # OFFLINE IS NOT "STILL AS IT WAS" (2026-10-01). The last thing a
            # board said before it dropped is all the hub holds, and "if the
            # garage door is closed" was being answered from it, for hours.
            if self.is_offline(did):
                nm = ((self.dm.devices or {}).get(did) or {}).get("name") or did
                self._cond_why = (f"the condition is not met -- {nm} is "
                                  f"offline, so its state is unknown")
                return False
            # Nor is a meter that is not answering: its zeros are not readings.
            if did in self._meter_silent:
                nm = ((self.dm.devices or {}).get(did) or {}).get("name") or did
                self._cond_why = (f"the condition is not met -- {nm} is not "
                                  f"answering, so its reading is unknown")
                return False
            stored = ((self.dm.devices or {}).get(did) or {}).get("state")
            state = dict(stored) if isinstance(stored, dict) else {}
            # THE BOARD'S OWN LATEST WORD ON TOP. For a board device the
            # registry's `state` is only refreshed when something calls
            # get_state, so on its own it can be hours stale -- "if the
            # garage door is closed" would be judged on whatever it was the
            # last time somebody opened the app. This engine holds the live
            # value; get_state merges it the same way.
            state.update(self.latest(did))
            key = cond.get("key", "on")
            # UNKNOWN IS NOT A MATCH. A device that has never published the
            # key being asked about has not satisfied anything, and saying
            # so here is the second half of the `equals` requirement added
            # in validate_rule -- validation stops new rules of that shape,
            # this stops the ones already saved, which validation cannot
            # reach. `None == None` used to make both cases read as "the
            # condition holds".
            if key not in state:
                return False
            return state.get(key) == cond.get("equals")
        if cond.get("kind") == "days":
            day = time.localtime(self.now()).tm_wday
            if day in [int(d) for d in cond.get("days") or []]:
                return True
            self._cond_why = (f"the condition is not met -- it is "
                              f"{DAY_NAMES[day]}")
            return False
        if cond.get("kind") == "reading":
            did = str(cond.get("device", ""))
            key = str(cond.get("key", ""))
            nm = ((self.dm.devices or {}).get(did) or {}).get("name") or did
            if self.is_offline(did) or did in self._meter_silent:
                self._cond_why = (f"the condition is not met -- {nm} is not "
                                  f"answering, so its reading is unknown")
                return False
            value = self.latest(did).get(key)
            if value is None:
                got = self._last.get((did, key))
                value = None if got is None else got[0]
            try:
                value = float(value)
            except (TypeError, ValueError):
                self._cond_why = (f"the condition is not met -- {nm} has not "
                                  f"reported its {key.replace('_', ' ')} yet")
                return False
            if cond.get("above") is not None:
                return value > float(cond["above"])
            return value < float(cond["below"])
        return True

    def try_now(self, rule: Dict[str, Any]) -> List[str]:
        """The builder's TRY IT NOW: the rule's THEN, once, now, not saved.
        The secure law and the human override apply as in a real firing; a
        display line stays half a minute at most. Returns what happened, in
        words."""
        trial = json.loads(json.dumps(rule))
        trial["name"] = str(rule.get("name") or "This automation")
        problem = validate_rule(trial)
        if problem:
            return [problem]
        trial["id"] = "try:" + str(rule.get("id") or "new")
        trial["name"] += " (tried)"
        # DO once, straight through: waits are skipped and a loop does not
        # repeat here (Try it now answers at once, and starts no run).
        every = actions_of(trial)
        acts = [a for a in every if a.get("kind") != "wait"]
        skipped = len(acts) < len(every)
        for act in acts:
            refusal = self.action_screening(act)
            if refusal:
                return [refusal]
            if act.get("kind") == "display":
                try:
                    hold = float(act.get("for_s"))
                except (TypeError, ValueError):
                    hold = 0.0
                act["for_s"] = 30.0 if hold <= 0 else min(hold, 30.0)
        lock = getattr(self, "_ear_lock", None) or self._lock
        with lock:
            failed = self._run_actions(trial, acts)
        said = [text for _ts, name, text in list(self.journal)
                if name == trial["name"]][:6][::-1]
        if failed and not said:
            said = ["a device did not answer"]
        said = said or ["done"]
        if skipped:
            said.append("(waits are skipped in Try it now)")
        if trial.get("loop"):
            said.append("(it ran once -- the loop is not tried)")
        return said

    def _note(self, rule: Dict[str, Any], text: str) -> None:
        self.journal.appendleft((self.now(), rule.get("name", "?"), text))
        event("AUTO", f"{rule.get('name')!r}: {text}")

    def _run_actions(self, rule: Dict[str, Any],
                     actions: List[Dict[str, Any]],
                     skip: Any = ()) -> List[int]:
        """Carry out one branch. Every action goes through _drive, so the
        secure refusal and the human override apply to THEN, ELSE and the
        revert identically.

        Returns the positions of the actions whose DEVICE did not answer --
        the ones worth trying again. A refusal (secure law, a person's
        override) is an answer, not a failure. `skip` is the positions that
        already worked on an earlier try."""
        rid = rule.get("id", "")
        failed: List[int] = []
        for i, action in enumerate(actions):
            if i in skip:
                continue
            try:
                if action.get("kind") == "notify":
                    text = str(action.get("text", ""))
                    # The action's place in the branch is part of the id: the
                    # phone REPLACES a notice with the same id, so two notify
                    # actions in one firing used to show only the last.
                    self.alerts.append({
                        "id": f"auto:{rid}:{int(self.now())}"
                              + (f":{i}" if i else ""),
                        "title": rule.get("name", "automation"),
                        "body": text, "ts": self.now(),
                    })
                    self._note(rule, f"notified: {text}")
                elif action.get("kind") == "device":
                    if self._drive(rule, str(action.get("device", "")),
                                   dict(action.get("set", {}))) is False:
                        failed.append(i)
                elif action.get("kind") == "press":
                    if self._press(rule, action) is False:
                        failed.append(i)
                elif action.get("kind") == "ask":
                    self._raise_approval(rule, action)
                elif action.get("kind") == "display":
                    self._show(rule, action)
                elif action.get("kind") == "scene":
                    scene = self.scenes.get(str(action.get("scene", ""))) \
                        if self.scenes else None
                    if scene is None:
                        self._note(rule, "its scene no longer exists -- "
                                         "nothing done")
                    else:
                        bad = False
                        for a in scene.actions:
                            if self._drive(rule, a.device_id,
                                           dict(a.state)) is False:
                                bad = True
                        if bad:
                            failed.append(i)
            except Exception:                       # noqa: BLE001
                traceback.print_exc()
                self._note(rule, "crashed while acting -- see the log")
                failed.append(i)
        return failed

    def note_ir_sent(self, code: Any) -> None:
        """Remember a code the hub is sending (from a rule or from the shell),
        so a receiver hearing it back within IR_ECHO_S is not taken as a
        press."""
        from esp_firmware.ir_flows import IR_ECHO_S, ir_signature
        keys, raw = ir_signature(code)
        if not keys and not raw:
            return
        now = self.now()
        recent = [s for s in getattr(self, "_ir_sent", [])
                  if now - s[0] <= IR_ECHO_S]
        recent.append((now, keys, raw))
        self._ir_sent = recent

    def _ir_echo(self, heard: Dict[str, Any]) -> bool:
        """Whether an IrReceived is a code the hub sent in the last
        IR_ECHO_S seconds (same Data or Hash, or near-same raw timings)."""
        sent = list(getattr(self, "_ir_sent", []))
        if not sent:
            return False
        from esp_firmware.ir_flows import IR_ECHO_S, ir_raw_close, ir_signature
        keys, raw = ir_signature(heard)
        now = self.now()
        for when, sent_keys, sent_raw in sent:
            if now - when > IR_ECHO_S:
                continue
            if keys & sent_keys or (raw and sent_raw
                                    and ir_raw_close(raw, sent_raw)):
                return True
        return False

    def _press(self, rule: Dict[str, Any], action: Dict[str, Any]) -> bool:
        """One button: a learned IR button sent by a board with an infrared
        LED (its own board first), or a Home Assistant press. False when
        nothing took it."""
        did = str(action.get("device", ""))
        d = (self.dm.devices or {}).get(did) or {}
        name = d.get("name") or did
        if d.get("secure"):
            self._note(rule, f"{name} is a secure device -- not pressed")
            return True                         # a refusal is an answer
        button = str(action.get("button") or "")
        codes = d.get("ir_codes") if isinstance(d.get("ir_codes"), dict) \
            else {}
        if button:
            code = codes.get(button)
            if code is None:
                self._note(rule, f"{name} has no button called "
                                 f"{button.replace('_', ' ')!r} -- not pressed")
                return True
            from esp_firmware.ir_flows import (ir_command_body, ir_not_sent,
                                               ir_senders)
            esp, client = self.esp, self._pub
            emitters = esp.boards_with("ir_send") if esp is not None else []
            if not emitters or client is None:
                self._note(rule, f"no infrared board can send "
                                 f"{button.replace('_', ' ')!r} to {name}")
                return False
            body = ir_command_body(code)
            if body is None:
                self._note(rule, f"{name}'s {button.replace('_', ' ')!r} is "
                                 f"stored in a form that cannot be sent -- "
                                 f"learn it again")
                return False
            # Its own board, then only blasters in its room, and only online.
            order, room = ir_senders(self.dm, esp, did, emitters)
            for slug in order:
                topic = ((esp._read_board(slug) or {}).get("topic") or "")
                if not topic or topic in self._offline:
                    continue
                self.note_ir_sent(code)
                if self._lcd_send(client, topic, "IRsend " + body):
                    self._note(rule, f"pressed {button.replace('_', ' ')} "
                                     f"on {name}")
                    return True
            self._note(rule, ir_not_sent(name, button, room, bool(order)))
            return False
        if self.dm is not None and self.dm.press(did):
            self._note(rule, f"pressed {name}")
            return True
        self._note(rule, f"{name} could not be pressed")
        return False

    def _fire(self, rule: Dict[str, Any]) -> bool:
        """Run one rule. True when the firing counted (even if a device
        inside it was skipped); False when it was withheld.

        THE IF/ELSE FORK (her design, 2026-08-27). A condition that does
        not hold used to mean "nothing happens". It still does when the
        rule has no ELSE -- but a rule that HAS one acts either way, and
        which branch ran is on the record.
        """
        rid = rule.get("id", "")
        run = self._runs.get(str(rid))
        if run is not None:
            # One run at a time: a WHEN while DO is still waiting or looping
            # does not start a second copy (said once per run).
            if not run.get("busy"):
                run["busy"] = True
                self._note(rule, "still running from its last start -- "
                                 "not started again")
            self._extend_revert(rule)
            return False
        last = self._fired_at.get(rid, 0.0)
        gap = self._cooldown_for(rule)
        if self.now() - last < gap:
            # HELD BACK, AND IT USED TO SAY NOTHING (2026-09-14: "pir fired
            # but cant fire anything").
            #
            # A PIR retriggers every few seconds. The first pass fires; for
            # the next minute every one is dropped here, silently, so
            # walking past four times produced one action and three
            # nothings with no record that anything had even been
            # considered. Add a manual command to that -- which used to
            # blank the rule for fifteen minutes -- and the automation
            # looked completely dead.
            #
            # ONCE PER BURST, not once per firing: noted only the first
            # time after a real firing, so a sensor reporting every second
            # adds one line rather than sixty. The next successful fire
            # re-arms it.
            if not self._cooldown_said.get(rid):
                self._cooldown_said[rid] = True
                self._note(rule, f"held back -- it fired "
                                 f"{int(self.now() - last)}s ago and this "
                                 f"rule waits {int(gap)}s between firings")
            # STILL MOVING IS STILL NOT QUIET (2026-10-01). A report the
            # cooldown refuses used to leave the pending revert where it
            # was, so a light could go out in the middle of a busy minute.
            self._extend_revert(rule)
            return False
        self._cond_why = ""
        held = self._conditions_hold(rule)
        if not held and getattr(self, "_cond_why", ""):
            self._note(rule, self._cond_why)
        branch = actions_of(rule, "then" if held else "else")
        if not held and not branch:
            return False                            # the original silence
        self._fired_at[rid] = self.now()
        self._cooldown_said[rid] = False     # re-arm the held-back line
        self._reverted.discard(rid)
        # Under the lock: _save() on another thread iterates this dict, and
        # adding `last_fired` to it mid-dump raises there.
        with self._lock:
            rule["last_fired"] = self.now()
            rule["fired_count"] = int(rule.get("fired_count", 0)) + 1
            self._rules_dirty = True        # written by _flush_rules
        if not held:
            self._note(rule, "the condition did not hold -- running ELSE")
        self._start_run(rule, branch, loop=held and rule.get("loop") is True)

        # ARM THE REVERT -- but only behind the THEN branch. A revert
        # undoes what the rule DID; the else branch is a different thing
        # happening, not a thing to be undone. Setting the deadline on
        # every firing is also the cancel: motion keeps pushing it out,
        # and only real quiet lets it arrive.
        if held and actions_of(rule, "revert"):
            after = float((rule.get("revert") or {}).get("after_s", 0))
            if after > 0:
                fresh = rid not in self._revert_due
                self._revert_due[rid] = self.now() + after
                self._revert_retry.pop(rid, None)
                if fresh:
                    self._save_state()      # once per burst, not per report
                else:
                    self._state_dirty = True
        return True

    # ── waits and the loop: DO run as steps, in the background ──────────

    @staticmethod
    def _run_shape(rule: Optional[Dict[str, Any]]) -> Any:
        """What a rule DOES, without its bookkeeping: a run stops when this
        changes (the rule was edited)."""
        skip = set(OWNER_FIELDS) | {"last_fired", "fired_count", "_invalid",
                                    "created_at"}
        return json.dumps({k: v for k, v in (rule or {}).items()
                           if k not in skip}, sort_keys=True, default=str)

    def _start_run(self, rule: Dict[str, Any], steps: List[Dict[str, Any]],
                   loop: bool = False) -> None:
        """Carry out a branch. Without a Wait and without the loop: exactly
        _run_actions, at once, as always. Otherwise the steps up to the
        first Wait run now and the rest on timers (_resume), so a pause
        never holds the engine up.

        Each pass of a loop runs the steps exactly as written: the ONLY IF
        is not checked again per pass (kept simple, like Arduino's loop())."""
        has_wait = any(a.get("kind") == "wait" for a in steps)
        loop = loop and has_wait            # never a loop with no pause
        if not has_wait:
            self._run_actions(rule, steps)
            return
        rid = str(rule.get("id", ""))
        run = {"steps": list(steps), "loop": loop, "rule": rule,
               "shape": self._run_shape(rule), "timer": None, "busy": False}
        with self._lock:
            self._runs[rid] = run
        if loop:
            self._note(rule, "loop started -- DO runs again and again until "
                             "this automation is switched off")
        self._run_from(rule, run, 0)

    def _run_from(self, rule: Dict[str, Any], run: Dict[str, Any],
                  start: int) -> None:
        """Run steps from `start` up to the next Wait (which sets a timer)
        or the end (where a loop starts again from the top)."""
        rid = str(rule.get("id", ""))
        steps = run["steps"]
        i = start
        while self._runs.get(rid) is run:
            if i >= len(steps):
                if not run["loop"]:
                    with self._lock:
                        if self._runs.get(rid) is run:
                            del self._runs[rid]
                    return
                i = 0                       # loop(): again from the top
            if steps[i].get("kind") == "wait":
                try:
                    secs = max(WAIT_MIN_S, float(steps[i].get("s")))
                except (TypeError, ValueError):
                    secs = WAIT_MIN_S
                if not math.isfinite(secs):
                    secs = WAIT_MAX_S
                timer = threading.Timer(min(secs, WAIT_MAX_S), self._resume,
                                        args=(rid, run, i + 1))
                timer.daemon = True
                run["timer"] = timer
                timer.start()
                return
            j = i
            while j < len(steps) and steps[j].get("kind") != "wait":
                j += 1
            # The whole list with the rest skipped: a notice keeps its
            # position in its id.
            self._run_actions(rule, steps,
                              skip=set(range(len(steps))) - set(range(i, j)))
            i = j

    def _resume(self, rid: str, run: Dict[str, Any], start: int) -> None:
        """A Wait is over (on the timer's thread): go on, unless the run was
        stopped or its rule switched off, removed or edited meanwhile."""
        try:
            with self._lock:
                if self._runs.get(rid) is not run:
                    return                  # stopped meanwhile
                if self._check_run(rid):
                    return
                rule = next((r for r in self.rules
                             if str(r.get("id", "")) == rid), run["rule"])
                self._run_from(rule, run, start)
        except Exception:                           # noqa: BLE001
            traceback.print_exc()
            with self._lock:
                if self._runs.get(rid) is run:
                    del self._runs[rid]
            self._note(run["rule"], "crashed while acting -- see the log")

    def _check_run(self, rid: str) -> bool:
        """Stop this rule's run if the rule was switched off, removed or
        edited. True when it was stopped."""
        run = self._runs.get(rid)
        if run is None:
            return False
        rule = next((r for r in self.rules
                     if str(r.get("id", "")) == rid), None)
        if rule is None:
            why = "removed"
        elif not self._truthy(rule.get("enabled", True)):
            why = "switched off"
        elif rule.get("_invalid") or self._run_shape(rule) != run["shape"]:
            why = "edited"
        else:
            return False
        return self._stop_run(rid, why)

    def _stop_run(self, rid: str, why: str) -> bool:
        """End a rule's run now: its pending Wait is cancelled. Journalled
        with the reason (a loop's stop, or a run cut short)."""
        with self._lock:
            run = self._runs.pop(rid, None)
        if run is None:
            return False
        timer = run.get("timer")
        if timer is not None:
            timer.cancel()
        what = "loop stopped" if run["loop"] else "stopped before its last step"
        self._note(run["rule"], f"{what} -- {why}")
        return True

    def _stop_runs(self) -> None:
        """The hub is stopping: every run ends with it (nothing is noted)."""
        with self._lock:
            runs, self._runs = list(self._runs.values()), {}
        for run in runs:
            if run.get("timer") is not None:
                run["timer"].cancel()

    def _extend_revert(self, rule: Dict[str, Any]) -> None:
        """Push a pending revert out, without running anything: the trigger
        is still reporting the state that fired the rule."""
        rid = rule.get("id", "")
        if rid not in self._revert_due:
            return
        after = float((rule.get("revert") or {}).get("after_s", 0))
        if after > 0:
            self._revert_due[rid] = max(self._revert_due[rid],
                                        self.now() + after)
            self._revert_retry.pop(rid, None)
            self._state_dirty = True

    # ── approvals: the rule asks, a person answers ──────────────────────

    def _raise_approval(self, rule: Dict[str, Any],
                        action: Dict[str, Any]) -> None:
        """An `ask` action: put the question where a person can answer it.

        NOTHING IS DRIVEN HERE. The rule has noticed something and is
        asking; until someone answers with their PIN, the secure device is
        exactly as it was. This is the shape her RFID design left open on
        2026-08-16 -- "someone without a paired phone taps, and an owner's
        phone gets the approve-with-PIN prompt" -- now buildable because
        the engine exists to notice the tap.
        """
        did = str(action.get("device", ""))
        d = (self.dm.devices or {}).get(did) or {}
        name = d.get("name") or did
        change = dict(action.get("set", {}))
        words = str(action.get("text", "")).strip()
        if not words:
            doing = ("unlock" if change.get("on") is False else "open") \
                if self.secure_direction(did, change) == "unsecure" \
                else "change"
            words = f"{rule.get('name', 'An automation')} wants to {doing} {name}."
        # A NUMBER IN THE ID: two questions in the same millisecond used to
        # share one, and answering one answered "both".
        self._approval_seq += 1
        approval = {
            "id": f"ask:{rule.get('id', '')}:{int(self.now() * 1000):x}"
                  f"-{self._approval_seq}",
            "rule": rule.get("name", "automation"),
            "rule_id": rule.get("id", ""),
            "device": did, "device_name": name, "set": change,
            "text": words, "asked_at": self.now(),
            "expires_at": self.now() + _config.AUTOMATION_APPROVAL_SECONDS,
        }
        with self._lock:
            # ONE OPEN QUESTION PER RULE AND DEVICE. A sensor that keeps
            # asking used to stack ten identical "approve?" cards; the new
            # one replaces the old, and the whole pile is capped.
            self.approvals = [a for a in self.approvals
                              if not (a.get("rule_id") == approval["rule_id"]
                                      and a.get("device") == did)]
            self.approvals.append(approval)
            del self.approvals[:-APPROVALS_MAX]
        # It also rides the ordinary alert road, so a phone that is only
        # watching notices still SEES the question even before the
        # approval screen exists.
        self.alerts.append({
            "id": approval["id"], "title": approval["rule"],
            "body": words + " Approve with your PIN.", "ts": self.now(),
        })
        self._note(rule, f"asked to {change} on {name} -- waiting for a "
                         f"person to approve it")

    def pending_approvals(self) -> List[Dict[str, Any]]:
        """Questions still worth answering. Expiry matters: an approval
        that outlived the moment it belongs to is a key left in a door."""
        ts = self.now()
        with self._lock:
            self.approvals = [a for a in self.approvals
                              if a["expires_at"] > ts]
            return list(self.approvals)

    def _take_approval(self, approval_id: str) -> Optional[Dict[str, Any]]:
        ts = self.now()
        with self._lock:
            for a in list(self.approvals):
                if a["id"] != approval_id:
                    continue
                self.approvals.remove(a)
                return a if a["expires_at"] > ts else None
        return None

    def approve(self, approval_id: str) -> Tuple[bool, str]:
        """A person said yes AND proved it. The PIN itself is checked by
        the shell (it owns the auth stack); reaching here means it passed.

        This is the ONLY road on which an automation touches a secure
        device in the unsafe direction, and it is not really the
        automation doing it -- a person is, with the rule as the messenger.
        """
        approval = self._take_approval(approval_id)
        if approval is None:
            return False, ("that request has expired or was already "
                           "answered -- nothing was changed")
        rule = next((r for r in self.rules
                     if r.get("id") == approval["rule_id"]), None)
        # The rule must still exist and be enabled: an approval used to be
        # honoured after its rule was deleted or paused.
        if rule is None or not rule.get("enabled", True):
            return False, ("the automation behind that request is gone or "
                           "paused -- nothing was changed")
        # SAY WHAT HAPPENED, NOT WHAT WAS HOPED (2026-10-01). This used to
        # answer "approved and done" whatever the board did, and the request
        # was already used up -- a person left thinking the door was open.
        result = self._drive(rule, approval["device"], dict(approval["set"]),
                             approved=True)
        if result is True:
            return True, f"{approval['device_name']} -- approved and done"
        if result is False:
            # It did not answer. The question goes back (with the time it
            # had left) so the person can try again.
            with self._lock:
                self.approvals.append(approval)
            return False, (f"{approval['device_name']} did not answer -- "
                           f"nothing was changed. Try again.")
        return False, (f"{approval['device_name']} is no longer there -- "
                       f"nothing was changed")

    def decline(self, approval_id: str) -> bool:
        approval = self._take_approval(approval_id)
        if approval is None:
            return False
        self.journal.appendleft((self.now(), approval["rule"],
                                 f"declined: {approval['device_name']} was "
                                 f"left exactly as it was"))
        return True

    def _run_reverts(self) -> None:
        """Reverts whose quiet has lasted long enough. Driven by tick().

        THE COOLDOWN DOES NOT APPLY HERE, deliberately. Cooldown exists so
        a chattering sensor cannot machine-gun a scene -- it governs how
        often a rule may FIRE. A revert is the tail of a firing that has
        already happened, and gating it would leave the light on after the
        room emptied, which is the exact failure the shape exists to fix.

        NEITHER DOES A PAUSE (2026-10-01). Pausing a rule stops it STARTING
        things; the light it already turned on still has to go out. And a
        revert whose board did not answer is tried again, every
        REVERT_RETRY_SECONDS, REVERT_MAX_TRIES times, then said out loud --
        it used to be forgotten after one failed attempt.
        """
        ts = self.now()
        changed = False
        for rid, due in list(self._revert_due.items()):
            if ts < due:
                continue
            rule = next((r for r in self.rules if r.get("id") == rid), None)
            actions = actions_of(rule, "revert") if rule else []
            if rule is None or not actions:
                # Its rule is gone: nothing left to say what to undo.
                del self._revert_due[rid]
                self._revert_retry.pop(rid, None)
                changed = True
                continue
            retry = self._revert_retry.get(rid) or {"tries": 0, "todo": None}
            todo = retry.get("todo")
            skip = set() if todo is None else \
                set(range(len(actions))) - set(todo)
            self._note(rule, "quiet for long enough -- reverting"
                       if not retry["tries"] else
                       f"trying the revert again (try {retry['tries'] + 1})")
            failed = self._run_actions(rule, actions, skip=skip)
            retry["tries"] = int(retry["tries"]) + 1
            changed = True
            if failed and retry["tries"] < REVERT_MAX_TRIES:
                retry["todo"] = failed
                self._revert_retry[rid] = retry
                self._revert_due[rid] = ts + REVERT_RETRY_SECONDS
                continue
            del self._revert_due[rid]
            self._revert_retry.pop(rid, None)
            self._reverted.add(rid)
            if failed:
                self._note(rule, f"GAVE UP reverting after "
                                 f"{retry['tries']} tries -- check it")
                self.alerts.append({
                    "id": f"auto:{rid}:revert-failed",
                    "title": rule.get("name", "automation"),
                    "body": "It could not switch something back after "
                            f"{retry['tries']} tries. Please check it.",
                    "ts": ts,
                })
        if changed:
            self._save_state()

    def _drive(self, rule: Dict[str, Any], did: str,
               change: Dict[str, Any], *, approved: bool = False
               ) -> Optional[bool]:
        """True = the device did it. False = it was tried and the device did
        not answer. None = deliberately not done (not in the registry, the
        secure law, a person's command outranking the rule)."""
        d = (self.dm.devices or {}).get(did)
        name = (d or {}).get("name") or did
        if d is None:
            self._note(rule, f"skipped {name}: not in the registry")
            return None
        # A TOGGLE BECOMES A STATE BEFORE ANYTHING ELSE LOOKS AT IT
        # (2026-09-13). Every question below -- the secure law, the note
        # the person reads, the value that reaches the board -- needs to
        # know WHICH WAY this is going, and a rule that says "the
        # opposite" does not carry that until the current state is read.
        #
        # Read from the device rather than the record: for a board that is
        # the engine's own MQTT cache, which is the freshest thing there
        # is, and it is what the executor's own flip uses.
        if change.get("on") == TOGGLE:
            try:
                now_on = bool((self.dm.get_state(did) or {}).get("on", False))
            except Exception:                       # noqa: BLE001
                now_on = bool((d.get("state") or {}).get("on", False))
            change = dict(change, on=(not now_on))
            self._note(rule, f"{name} is {'on' if now_on else 'off'}, so "
                             f"this turns it {'off' if now_on else 'on'}")
        if d.get("secure") and not approved:
            # THE LAW AGAIN, AT THE MOMENT IT MATTERS. Screening at
            # authoring is the explanation; this is the guarantee -- a
            # device can be marked secure after its rule was written, and
            # the direction table can only be trusted where it applies.
            if self.secure_direction(did, change) != "secure":
                self._note(rule, f"REFUSED {name}: this would leave a "
                                 f"secure device less secure, and no "
                                 f"automation does that unattended")
                return None
            self._note(rule, f"{name} is secure -- allowed because this "
                             f"makes it safer, not less safe")
        if approved and d.get("secure"):
            self._note(rule, f"{name}: driven because a person approved it "
                             f"with their PIN")
        # AN APPOINTMENT IS NOT A REACTION (her ruling, 2026-09-02: "let it
        # fire regardless but then i switch it off and then that's it").
        #
        # The override window exists for ONE shape of rule: motion turns
        # the lights on, you say "off", you move ten seconds later, and it
        # fights you. There, human intent has to keep outranking the
        # machine for a while or the rule wins every argument.
        #
        # A TIME TRIGGER IS THE OPPOSITE. You asked for the lamp at 16:03;
        # touching it at 15:55 is not an argument with that, and refusing
        # the appointment because you happened to use the switch beforehand
        # is the machine second-guessing an instruction you gave it on
        # purpose. And there is nothing to fight: a time rule fires ONCE a
        # day (see _time_fired), so switching it off afterwards is final --
        # it cannot come back until tomorrow.
        #
        # Every reactive kind -- event, persist, absent, threshold -- keeps
        # the window, because those are the ones that can loop.
        #
        # PER RULE (2026-10-01): a rule may carry its own `override` -- the
        # automation always wins, or the person wins for N seconds, minutes,
        # hours or days. The time-rule exemption is the HOUSE default's; a
        # window the person chose for this rule is the person's word.
        mode, _secs = override_spec(rule)
        by_the_clock = (rule.get("trigger") or {}).get("kind") == "time" \
            and mode == "default"
        if self._overridden_for(rule, did) and not approved \
                and not by_the_clock:
            self._note(rule, f"skipped {name}: you commanded it yourself "
                             f"recently, and you outrank the machine")
            return None
        ok = False
        try:
            # Through the listener's own open connection where it can be:
            # the default driver opens a fresh broker session per action
            # (connect, LWT wait, publish, disconnect -- seconds each) ON
            # THIS THREAD, which starved feed()/tick() and let the
            # keepalive lapse.
            ok = bool(self.dm.set_state(did, _driver=self._board_driver,
                                        **change))
        except Exception:                           # noqa: BLE001
            traceback.print_exc()
        self._note(rule, f"{'set' if ok else 'FAILED to set'} "
                         f"{name} -> {change}")
        return ok

    def _board_driver(self, did: str, change: Dict[str, Any]) -> bool:
        """DeviceManager's mqtt_driver contract, served over `_pub`.

        Same commands drive_item_sync builds (EspBuilder.drive_commands),
        published on the connection that is already open. Falls
        back to the ordinary driver when the listener is not connected or
        the item is not one this can address. The devices.json save (with
        its fsync) still happens in set_state -- left as is."""
        client, esp = self._pub, self.esp
        fallback = self.dm.mqtt_driver
        if client is None or esp is None or "." not in did:
            return bool(fallback(did, change)) if fallback else False
        slug, suffix = did.split(".", 1)
        try:
            record = esp._read_board(slug)
            plan = esp._plan_for(slug)
            topic = (record or {}).get("topic")
            if not topic or plan is None:
                return bool(fallback(did, change)) if fallback else False
            commands, _why = esp.drive_commands(
                plan, suffix, on=change.get("on"), level=change.get("level"),
                rgb=change.get("colour") or None)
            if not commands:
                return False
            if not all(self._lcd_send(client, topic, c) for c in commands):
                return bool(fallback(did, change)) if fallback else False
            event("MQTT", f"engine drive {did}: {commands}")
            return True
        except Exception as exc:                    # noqa: BLE001
            event("MQTT", f"engine drive {did} failed ({exc}) -- "
                          f"using the direct driver")
            return bool(fallback(did, change)) if fallback else False

    # ── the broker thread ───────────────────────────────────────────────

    def start(self) -> bool:
        """Bring the reflexes online. False (with a log line) when there is
        nothing to connect with — no paho, no boards, no broker address.
        Safe to call again later; it retries from scratch."""
        with self._start_lock:
            return self._start()

    def _start(self) -> bool:
        if self._thread is not None and self._thread.is_alive():
            return True
        if self.esp is None:
            return False
        # Signs in as the hub (EspBuilder.hub_login: the operator's
        # HOMEVOICE_HUB_MQTT_USER/PASS, else the hub's own account) --
        # never a board's, whose topic ACL would make the engine deaf.
        try:
            user = self.esp.hub_login()[0]
            client = self.esp._mqtt_client()
        except ImportError:
            event("AUTO", "paho-mqtt is not installed -- automations idle")
            return False
        except (OSError, ValueError) as exc:
            event("AUTO", f"no MQTT account to listen with yet ({exc}) -- "
                          f"run `python setup_manager.py --broker`")
            return False
        except Exception as exc:                    # noqa: BLE001
            event("AUTO", f"listener could not be built: {exc}")
            return False
        settings = self.esp._mqtt_settings()
        self.rebuild_index()
        self._stop.clear()

        def on_connect(_c, _u, _flags, rc, *_rest):
            if rc == 0:
                # LWT TOO, and only for the panels: a board that power
                # cycles comes back with its display dark or full of
                # rubbish, and until now nothing here could even see that
                # it had gone -- `stat/#` and the sensor topic say nothing
                # about a board leaving. The retained Online is delivered
                # on subscribe, which is why the handler waits for an
                # Offline before it re-wakes anything.
                # tele/+/RESULT TOO (2026-10-01): an infrared code arrives as
                # {"IrReceived":...} on tele/<board>/RESULT, and this hub
                # never listened there -- an IR remote rule could be saved,
                # listed, and never fire.
                _c.subscribe([("stat/#", 0), ("tele/+/SENSOR", 0),
                              ("tele/+/LWT", 0), ("tele/+/INFO1", 0),
                              ("tele/+/RESULT", 0), ("tele/+/STATE", 0),
                              ("tele/+/INFO3", 0)])
                event("AUTO",
                      "listening -- stat/#, tele/+/SENSOR, tele/+/LWT, "
                      "tele/+/INFO1 and tele/+/RESULT")
                # Every board is asked Status 2 once: the safeboot build
                # answers Unknown and is healed (see _note_unknown).
                try:
                    n = self.probe_boards(lambda t, v: _c.publish(t, v))
                    if n:
                        event("OTA", f"asked {n} board(s) for Status 2 -- one in safeboot will answer Unknown and be healed")
                except Exception as exc:            # noqa: BLE001
                    event("OTA", f"board probe failed: {exc}")
            else:
                # A refused CONNACK used to be silent: the loop just saw
                # the socket close and said "connection lost" forever. 4
                # and 5 are bad credentials / not authorised -- the hub
                # account is missing from the password file, or the
                # broker never reloaded it.
                event("AUTO", f"broker REFUSED the hub login (rc={rc}) -- "
                              f"run: python setup_manager.py --broker, then "
                              f"restart the broker")

        def on_message(_c, _u, msg):
            try:
                # THE RETAIN FLAG IS CARRIED, not dropped. This is the only
                # place in the hub that ever sees it, and _note_presence
                # cannot tell a replayed Online from a live one without it
                # -- which is the whole of the gibberish-until-power-cycle
                # bug (2026-09-12). getattr, because the fakes in the tests
                # are plain objects and a missing flag must mean "live".
                self.feed(msg.topic, msg.payload,
                          retained=bool(getattr(msg, "retain", False)))
            except Exception:                       # noqa: BLE001
                traceback.print_exc()

        client.on_connect = on_connect
        client.on_message = on_message
        self._pub = client
        # The safeboot healer publishes through this same client; the URL
        # it hands a board is esp_builder's, "" when there is no image.
        from hub.ota_server import SafebootHealer
        self.healer = SafebootHealer(
            publish=lambda topic, value: client.publish(topic, value),
            url_of=self.esp._ota_heal_url,
            say=lambda m: event("OTA", m))
        self.refresh_lcd_targets()

        def run() -> None:
            last_tick = 0.0
            last_index = self.now()
            connected = False
            backoff = 2.0
            while not self._stop.is_set():
                try:
                    if not connected:
                        # connect() every time (not reconnect()): it resets
                        # paho's socket state cleanly, and on_connect
                        # resubscribes on every successful connection.
                        client.connect(settings["host"],
                                       int(settings["port"]), 30)
                        connected = True
                        backoff = 2.0
                    # paho's loop() does NOT raise when the broker goes
                    # away: it returns an error code, and every later call
                    # returns at once. Read as an exception it never was,
                    # the old loop never reconnected and spun flat out.
                    rc = client.loop(timeout=1.0)
                    if rc:
                        raise ConnectionError(f"mqtt loop rc={rc}")
                except Exception as exc:            # noqa: BLE001
                    if connected:
                        event("AUTO", f"broker connection lost ({exc}) -- "
                                      f"reconnecting")
                    connected = False
                    # broker away: breathe, then court it again
                    self._stop.wait(backoff)
                    backoff = min(backoff * 2, 60.0)
                ts = self.now()
                if ts - last_tick >= 5.0:
                    last_tick = ts
                    try:
                        self.tick()
                    except Exception:               # noqa: BLE001
                        traceback.print_exc()
                if ts - last_index >= 300.0:
                    last_index = ts
                    try:
                        self.rebuild_index()
                        # A panel added since the last sweep. Read here and
                        # not per tick: it is a directory walk.
                        self.refresh_lcd_targets()
                    except Exception:               # noqa: BLE001
                        traceback.print_exc()
            self._pub = None
            try:
                client.disconnect()
            except Exception:                       # noqa: BLE001
                pass

        self._thread = threading.Thread(target=run, name="automations",
                                        daemon=True)
        self._thread.start()
        # The thread is a daemon: this writes the day's history and the
        # firing counts on exit even when the app's shutdown path is skipped.
        if not getattr(self, "_atexit_set", False):     # once, however often start() runs
            atexit.register(self.stop)
            self._atexit_set = True
        event("AUTO", f"engine up as {user!r} "
                      f"({len(self.rules)} rule(s))")
        return True

    def stop(self) -> None:
        self._stop.set()
        self._stop_runs()
        self._save_history()
        self._flush_rules()
        self._save_state()
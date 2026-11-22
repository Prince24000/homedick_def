"""
executor.py — Carry out a resolved action, end to end, and say what a device is.

The single mutation point. It owns:
  - the ENVELOPE vocabulary: what a device's numbers mean (DEFAULT_ENVELOPE,
    BINARY_ENVELOPE, is_binary_envelope, describe_envelope)
  - describe_reading(): a device's state as a sentence
  - build_command(): arithmetic turning extracted slots + a device into a
    concrete {action, key, value}. (Moved here from slot_extractor, which is
    now purely extraction.)
  - applying that change to device state (via DeviceManager.set_state)

WHY THE DESCRIBE FUNCTIONS LIVE HERE and not in resolver.py (moved out of
main.py, 2026-08-29). resolver.py is a pure DECISION — its own docstring says
it never prints — so a family of rendering functions there would contradict
the one rule that file is built on. This module already speaks the whole
vocabulary they need: `state_key`, `range_min`, `range_max`, `step`, and the
distinction between a bool and a number. `is_binary_envelope` asks
`state_key == "on"`, which is exactly the question build_command's binary
branch answers, and describe_reading reads the same state dict _write compares.

Keeping them together is not tidiness, it is agreement. They were already
drifting: build_command's fallbacks were written as bare literals 10.0 / 0.0 /
100.0 — the same three numbers as DEFAULT_ENVELOPE, in a different file, with
nothing to hold them equal. main.py's own comment on that constant says it
exists because two inline copies HAD drifted; this was the third copy, and it
was missed. build_command reads the constant now.

build_command takes slots as a DICT (the shape SlotExtractor.extract returns)
and contains NO English vocabulary — all of that lives in lexicon.py.

RESULT CONTRACT
---------------
execute() returns a dict the caller renders AND believes:

    outcome     'acted' | 'noop' | 'error'
    satisfied   bool — is the device now in the state the person asked for?
    device_id   str
    reason      str, on 'noop' and 'error'
    change      'on' | 'off' | 'set', on 'acted'
    key/value   on change == 'set'
    before/after the device state either side of the write

`satisfied` is separate from `outcome` ON PURPOSE. "Turn on the light" when
the light is already on changes nothing — outcome 'noop' — but the person got
what they asked for, so satisfied is True. Collapsing the two reports a
correctly-executed command to the phone as a failure.

This contract used to be a promise the code did not keep: execute() returned
'acted' on every path except an unknown device id. It never read
DeviceManager.set_state()'s return value, never compared the state either side
of a write, and had no 'error' outcome at all — so a caller asking "did that
work?" was always told yes.
"""

import re
from typing import Any, Dict, Optional

from core.config import INTENSITY_STEPS_PER_UNIT
from nlu.slot_extractor import Slots   # the exact dict this module consumes
from nlu.lexicon import normalize_unit


# Resolved: lexicon.py derives its category names from LEXICON's own keys, all
# of which are plural, and slot_extractor's own branches emit 'maximizers' too.
# The singular was a hedge written before lexicon.py was available and it never
# occurs -- accepting it was dead code that read as live.
MAXIMIZER_CATEGORY = "maximizers"


# ═══════════════════════════════════════════════════════════════
# ENVELOPES — what a device's numbers mean
# ═══════════════════════════════════════════════════════════════

# What a device's numbers mean when nothing better is known, and what they mean
# when it has no gradable value at all. Both shapes were written out inline in
# three separate places in menu_add_device, which is how the "restore the
# defaults" path and the "treat it as binary" path drifted apart.
# NO "unit". A device's declared unit was stored, printed, asked about and
# copied around, and exactly ONE thing ever read it: `unit == "binary"`. That
# is the same fact as `state_key == "on"`, which every envelope already sets
# alongside it and which resolver._is_adjustable was already using instead.
# Two encodings of one fact, one of them purely decorative -- and a decoration
# that could disagree with the thing it decorated.
#
# What the unit SAID also varied by source: llm_describer emitted
# "celsius"/"fahrenheit"/"percent", device_manager emitted "degrees"/"percent",
# so the same thermostat read differently depending on how it was registered.
# The number is the answer to "set it to 20"; the label was never consulted.
DEFAULT_ENVELOPE: Dict[str, Any] = {
    "step": 10.0, "range_min": 0.0, "range_max": 100.0,
    "state_key": "level",
}
BINARY_ENVELOPE: Dict[str, Any] = {
    "step": 0.0, "range_min": 0.0, "range_max": 1.0,
    "state_key": "on",
}


def is_binary_envelope(meta: Dict[str, Any]) -> bool:
    """On/off only. The one question the unit field was ever asked."""
    return meta.get("state_key") == "on"


#: Fields that are HOW a device is described, not WHAT it is reading. They
#: belong on the record so the picker and the tiles can consult them; read
#: aloud they are Python leaking into a sentence -- "on, 50.2,
#: colour_modes=['rgb', 'color_temp'], colour=[255, 0, 0]".
#:
#: main._board_reading_rows already excludes exactly `unit`, `colour` and
#: `colour_modes`. The same knowledge, in the same words, was simply never
#: carried across to the spoken path (2026-09-14).
_NOT_A_READING = frozenset({
    "level", "unit", "on", "text", "colour", "colour_modes",
})


#: What a device with nothing to report is said to be. One word for it, used
#: by the spoken answer and by the displays that must stay blank instead.
NO_READING = "not available"


def describe_reading(state: Dict[str, Any],
                     reading_key: Optional[str] = None) -> str:
    """A device's state as a sentence, for answering a question.

    `reading_key` is the field a BOARD sensor actually publishes under --
    the record's own `reading_key`. Given it, that field is the headline
    number and the stored `level` is dropped.

    WHY THAT MATTERS AND IS NOT A FLOURISH (2026-09-14): register_board
    seeds a board sensor's state as `{"level": range_min}` -- a placeholder
    nobody measured -- and the real value arrives over MQTT under the
    board's own name. So a DHT22 registered -10..60 and reporting 21.5 C
    answered "what's the temperature" with:

        -10, temperature=21.5, humidity=44.0

    the placeholder first, in the position a person reads as the answer,
    and the real reading trailing behind it as `key=value`.
    `_board_reading_rows` folds `level` into `reading_key` for the phone's
    tiles and has since 2026-09-03; the spoken path never learned to, so
    the tile was right and the sentence was wrong about the same device.

    `dm.get_state()` returns a dict, and printing it raw is fine for a lock --
    {'on': True} reads clearly enough. It is not fine for a meter, which comes
    back as {'level': 146.0, 'unit': 'W'} and should be read aloud as "146 W",
    not as Python.

    THREE PATHS READ A STATE and only one of them called this, so the same
    question was answered in English or in Python depending on which branch
    matched: "status of the house" and the ambiguous-match branch both
    interpolated the dict, so asking after a lamp replied "{'on': False}"
    when the word is "off" (her report, 2026-08-29). test_query_answers
    carries a lint that fails if any _io.write interpolates get_state again.
    """
    if not isinstance(state, dict) or not state:
        return NO_READING
    if "text" in state:                      # a non-numeric sensor
        return str(state["text"])
    parts = []
    # ON/OFF FIRST, because it is the answer to the question actually
    # asked. "is the heater on?" wants "on, 146 W", not "146 W, on" --
    # and for the common case, a thing that is simply off, it is the
    # whole answer.
    if "on" in state:
        parts.append("on" if state["on"] else "off")

    def _tidy(value):
        """Whole numbers without a trailing .0 -- "21 C", not "21.0 C"."""
        if isinstance(value, float) and value == int(value):
            return int(value)
        return value

    # THE HEADLINE NUMBER. `reading_key` when the record names one AND the
    # board has actually published under it; otherwise the stored `level`,
    # which is what an HA device and every pre-reading_key record carry.
    unit = state.get("unit")
    primary = str(reading_key or "") or None
    head = primary if primary and primary in state else (
        "level" if "level" in state else None)
    if head:
        parts.append(f"{_tidy(state[head])}{' ' + unit if unit else ''}")
    for key, value in state.items():
        if key in _NOT_A_READING or key == primary:
            continue
        parts.append(f"{key}={_tidy(value)}")
    return ", ".join(parts) if parts else NO_READING


def describe_envelope(meta: Dict[str, Any]) -> str:
    """The numbers, in words, for someone deciding whether to accept them."""
    if is_binary_envelope(meta):
        return "on/off only, no adjustable level"
    return (f"{meta.get('range_min')}-{meta.get('range_max')}, "
            f"moving {meta.get('step')} per step")


def _as_float(value, default: Optional[float] = None) -> Optional[float]:
    """float(value), or `default` if it isn't a number. Bools are NOT numbers
    here: True would otherwise read as 1.0 and make a switch look like a
    dimmer."""
    if isinstance(value, bool):
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


# ═══════════════════════════════════════════════════════════════
# COMMAND BUILDER  (moved from slot_extractor.py — pure arithmetic)
# ═══════════════════════════════════════════════════════════════

#: The verbs a device's own state words stand for. A record whose words are
#: open/closed is commanded with "open" and "close" (and "shut"); one whose
#: words are locked/unlocked with "lock" and "unlock". The generic switch
#: particles (on/off) stay the extractor's; these are looked up per device,
#: AFTER resolution, because "open" means nothing until we know what is
#: being opened. Nothing here is gradable: "open the gate a bit more" has a
#: delta and never reaches this table.
STATE_WORD_VERBS: Dict[str, Dict[str, tuple]] = {
    "open":     {"on": ("open",), "off": ("close", "shut")},
    "locked":   {"on": ("lock",), "off": ("unlock",)},
}


def switch_from_state_words(text: Any, device: Dict[str, Any]) -> Optional[str]:
    """'on' / 'off' when the command's verb is one of THIS device's own
    state words, else None. "open the gate" on a gate whose words are
    open/closed is ON; "close it" is OFF; "open the lamp" on a lamp whose
    words are on/off is nothing (None) and stays a flip. Both verbs in one
    command is nobody's answer: None. (2026-09-10 -- "open the door" used
    to toggle, so an open door closed.)"""
    words = device.get("state_words") if isinstance(device.get("state_words"), dict) else {}
    on_word = str(words.get("on", "")).lower()
    verbs = STATE_WORD_VERBS.get(on_word)
    if not verbs:
        return None
    said = set(re.findall(r"[a-z]+", str(text or "").lower()))
    # the bare stems and their -s / -ed / -ing forms as people say them
    def forms(v):
        return {v, v + "s", v + "ed", v + "d", v + "ing", v[:-1] + "ing" if v.endswith("e") else v + "ing"}
    hit_on = any(said & forms(v) for v in verbs["on"])
    hit_off = any(said & forms(v) for v in verbs["off"])
    if hit_on == hit_off:
        return None
    return "on" if hit_on else "off"


#: Comparatives about the ROOM's temperature: +1 warmer, -1 cooler.
_TEMPERATURE_HEADS = {"warmer": 1, "hotter": 1, "warm": 1, "heat": 1,
                      "cooler": -1, "colder": -1, "cool": -1, "chill": -1}
_HEATING_WORDS = ("heater", "heating", "radiator", "boiler", "warmer", "heat")
_COOLING_WORDS = ("air con", "aircon", "air conditioner", "cooler", "fan",
                  "chiller", "a/c", "ac")


def _thermal_effect(device: Dict[str, Any]) -> Optional[int]:
    """+1 if switching this device ON warms the room, -1 if it cools it,
    None when its name and type don't say."""
    words = " ".join(str(device.get(k) or "") for k in
                     ("name", "type_id", "ha_entity_id", "type")).lower()
    tokens = set(re.split(r"[^a-z/]+", words))
    def says(options):
        return any((o in tokens) if " " not in o else (o in words) for o in options)
    heats, cools = says(_HEATING_WORDS), says(_COOLING_WORDS)
    if heats == cools:
        return None
    return 1 if heats else -1


def build_command(device, slots):
    """Turn extracted slots + a device dict into a concrete action.

    device: a dict with state/step/range_min/range_max/state_key.
    slots:  the dict returned by SlotExtractor.extract().
    Returns {'action': 'set'|'flip', ...}. No English here — magnitude and
    direction were already resolved by the lexicon-driven extractor. 'flip'
    means "no explicit value given"; the caller resolves it to a concrete
    opposite on/off state (HA can't act on an abstract toggle).

    PRECEDENCE: colour, then absolute value, then delta, then bare switch. A
    concrete target is always more specific than an on/off particle, so "turn
    on the lamp to 50" must land on 50 — not on range_max.

    Colour sits at the top because it is unambiguous: nothing else in an
    utterance competes with a named colour, and "turn the lamp red" carries a
    switch word that would otherwise win and merely switch it on.

    Every slot is read defensively: a partial or ill-typed dict from a test or
    a future caller degrades to 'flip', it does not raise. (The original
    guarded KeyError but not type: a non-numeric absolute_value still raised
    TypeError out of the clamp, and a string step raised out of float().)
    """
    if not isinstance(device, dict) or not device or not isinstance(slots, dict):
        return {'action': 'flip'}

    # ── Colour ──
    # First, and returned whole rather than as a value+key pair: an RGB triple
    # is not a number on the device's range, has no step, and cannot be
    # clamped. Everything below this line assumes a scalar.
    colour = slots.get('colour')
    if colour is not None:
        return {'action': 'set_colour', 'rgb': tuple(colour)}

    state = device.get('state')
    state = state if isinstance(state, dict) else {}

    # FROM THE CONSTANT, not from three bare literals. These were 10.0, 0.0
    # and 100.0 written out here -- the same three numbers as
    # DEFAULT_ENVELOPE, which lived in main.py with nothing holding them
    # equal. That constant's own comment records that two inline copies had
    # already drifted once; this was the third, and it was missed because it
    # was in a different file.
    # (`or`: a step of 0, None or unparseable all take the default.)
    step = _as_float(device.get('step')) or float(DEFAULT_ENVELOPE['step'])
    range_min = _as_float(device.get('range_min'), float(DEFAULT_ENVELOPE['range_min']))
    range_max = _as_float(device.get('range_max'), float(DEFAULT_ENVELOPE['range_max']))
    if range_max < range_min:         # an inverted range clamps everything to
        range_min, range_max = range_max, range_min   # one value otherwise

    state_key = device.get('state_key')

    def clamp(v):
        return min(range_max, max(range_min, v))

    current_val = _as_float(state.get(state_key)) if state_key else None

    # A PERCENT THE PERSON SAID IS A SHARE OF THE RANGE -- unless the
    # device's own numbers are percent already (a dimmer, a blind), where
    # "50%" is simply 50. "set the thermostat to 50%" on a 16-30 device was
    # clamped straight to 30. On a 0-100 range the mapping is the identity.
    # (The unit is the STATE's: records carry no top-level unit since the
    # migration in DeviceManager._load.)
    device_unit = normalize_unit(str(state.get('unit') or ''))
    span = range_max - range_min

    if current_val is None:
        # Last-resort baseline, in SORTED key order rather than dict insertion
        # order. The old loop took whichever numeric happened to come first,
        # so the same device serialised in a different order gave a different
        # baseline for the same spoken command.
        for key in sorted(state):
            current_val = _as_float(state[key])
            if current_val is not None:
                break

    # ── Absolute value (clamped to the device range) ──
    if slots.get('absolute_value') is not None:
        absolute = _as_float(slots['absolute_value'])
        if absolute is None:
            return {'action': 'flip'}          # present, but not a number
        # No 'unit' key. It carried slots['absolute_unit'] -- the unit the
        # PERSON said -- into the command dict, and _set_from_cmd never read
        # it. The extractor still parses unit tokens, because a unit is strong
        # evidence that a number is a value rather than part of a device name
        # (see _value_evidence); the parsed label just has no consumer.
        #
        # A SPOKEN FRACTION IS A FRACTION OF THE RANGE. "half open", "a
        # quarter", "three quarters" reach here as 0.5 / 0.25 / 0.75 with
        # no unit; on a 0-100 gate that used to mean position 0.5 -- shut,
        # to the eye. A unitless value strictly between 0 and 1 on a device
        # whose range is wider than 1 is read as that share of the range;
        # "50%" carries its unit and is untouched; a thermostat's 16-30 is
        # wider than 1 and "half" of it is the middle, which is what "half"
        # means there too. (2026-09-10, the gate that stops half-way.)
        if slots.get('absolute_unit') is None and 0.0 < absolute < 1.0 and span > 1.0:
            absolute = range_min + absolute * span
        elif slots.get('absolute_unit') == 'percent' and device_unit != 'percent':
            absolute = range_min + absolute / 100.0 * span
        return {'action': 'set', 'key': state_key, 'value': clamp(absolute)}

    # ── Delta ──
    delta = _as_float(slots.get('delta'))
    direction = slots.get('delta_direction')
    if delta is not None and direction:
        category = slots.get('delta_category')

        if category == MAXIMIZER_CATEGORY:
            bound = range_max if direction == 'increase' else range_min
            return {'action': 'set', 'key': state_key, 'value': bound}

        if category == 'midpoints':
            return {'action': 'set', 'key': state_key,
                    'value': (range_min + range_max) / 2}

        if state_key == 'on' or current_val is None:
            # NOTHING TO MOVE, SO UP IS ON AND DOWN IS OFF -- never a toggle.
            # This returned 'flip' (and a binary plug with a `power` reading
            # did sums on the watts), so "turn the fan up" switched a running
            # fan OFF, "turn the plug down" left it on, and "dim the light"
            # on a light that was off -- HA sends no brightness then --
            # switched it ON (2026-09-23).
            if state_key == 'on' or isinstance(state.get('on'), bool):
                # EXCEPT WARMER/COOLER: those name the ROOM, not the device.
                # "make it cooler" means an air conditioner ON and a heater
                # OFF; with no way to tell which it is, toggle as before.
                head = str(slots.get('delta_head') or '').lower()
                if head in _TEMPERATURE_HEADS:
                    effect = _thermal_effect(device)
                    if effect is None:
                        return {'action': 'flip'}
                    return {'action': 'set', 'key': 'on',
                            'value': (effect > 0) == (_TEMPERATURE_HEADS[head] > 0)}
                return {'action': 'set', 'key': 'on',
                        'value': direction == 'increase'}
            return {'action': 'flip'}

        if category != 'explicit':
            magnitude = step * delta * INTENSITY_STEPS_PER_UNIT
        elif slots.get('delta_unit') == 'percent' and device_unit != 'percent':
            # "up by 10%" on a 16-30 thermostat: 10% of the span, not ten
            # degrees -- the same reading as an absolute percent above.
            magnitude = delta / 100.0 * span
        else:
            magnitude = delta
        moved = current_val + (magnitude if direction == 'increase' else -magnitude)
        return {'action': 'set', 'key': state_key, 'value': round(clamp(moved), 1)}

    # ── Binary switch ──
    # Expressed as an explicit on/off write, NOT as value=range_min/range_max.
    #
    # Those are not the same instruction, and this file used to give both
    # answers depending on which entry point you came through: build_command
    # turned "off" on a dimmer into level=0, while Executor._apply_state
    # turned the same word into on=False. The Executor's reading is the right
    # one — "turn the lamp off" should not also erase the brightness it will
    # come back on at — so build_command now agrees with it instead of
    # standing as a second, contradictory answer in the same file.
    switch = slots.get('switch')
    if switch in ('on', 'off'):
        want = (switch == 'on')
        if state_key and isinstance(state.get(state_key), bool):
            return {'action': 'set', 'key': state_key, 'value': want}
        for key in sorted(state):
            if isinstance(state[key], bool):
                return {'action': 'set', 'key': key, 'value': want}
        return {'action': 'set', 'key': 'on', 'value': want}

    return {'action': 'flip'}


# ═══════════════════════════════════════════════════════════════
# EXECUTOR
# ═══════════════════════════════════════════════════════════════

class Executor:
    """Applies a resolved action to a single device. The one mutation point.

    Constructed with a DeviceManager. The caller snapshots for undo before
    invoking (main.py snapshots once per command so a multi-device command
    reverts as a single batch).
    """

    def __init__(self, dm):
        self.dm = dm

    # ── the single write ──────────────────────────────────────────────

    def _write(self, did: str, changes: Dict[str, Any],
               before: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """Apply one state change and report what actually happened.

        EVERY mutation in this class goes through here. Previously each call
        site invoked dm.set_state() directly and discarded its return value,
        so a rejected write was indistinguishable from an applied one, and
        "did anything actually change?" was never asked at all.

        set_state() returning None is read as "no signal, assume applied" —
        only an explicit False is a refusal.

        `changed` is the REQUESTED DIFF against the pre-write state, and
        `after` is that state with the request applied. Neither is re-read
        from the device: a board answers on stat/POWER a beat later and HA's
        REST state can lag its service call, so a live read straight after
        the write returned the OLD value -- every real switch reported as
        "already in that state", every toggle as "had no effect".

        `before`: the state the caller has JUST read, so a flip does not pay
        a second read (for an HA device, a second REST round trip).
        """
        if before is None:
            before = self.dm.get_state(did)
        accepted = self.dm.set_state(did, **changes)
        after = dict(before)
        if accepted is not False:
            after.update(changes)
        return {'ok': accepted is not False,
                'changed': any(before.get(k) != v for k, v in changes.items()),
                'before': before,
                'after': after}

    @staticmethod
    def _result(outcome: str, did: str, write: Optional[Dict[str, Any]] = None,
                *, satisfied: bool, reason: str = "", **extra) -> Dict[str, Any]:
        res: Dict[str, Any] = {'outcome': outcome, 'device_id': did,
                               'satisfied': satisfied}
        if reason:
            res['reason'] = reason
        if write is not None:
            res['before'] = write['before']
            res['after'] = write['after']
        res.update(extra)
        return res

    def _settle(self, did: str, write: Dict[str, Any], **detail) -> Dict[str, Any]:
        """Turn a write into the caller's verdict.

        Three genuinely different cases that used to be reported as one:
          rejected            -> error,  not satisfied
          accepted, no change -> noop,   SATISFIED (it was already like that)
          accepted, changed   -> acted,  satisfied
        """
        if not write['ok']:
            return self._result('error', did, write, satisfied=False,
                                reason='write_rejected', **detail)
        if not write['changed']:
            return self._result('noop', did, write, satisfied=True,
                                reason='already_in_that_state', **detail)
        return self._result('acted', did, write, satisfied=True, **detail)

    # ── entry point ───────────────────────────────────────────────────

    def execute(self, did: str, slots: Slots,
                speaker_id: str) -> Dict[str, Any]:
        """Carry out the action for one device. See the module docstring for
        the shape of the returned dict."""
        device = self.dm.devices.get(did)
        if device is None:
            return self._result('noop', did, satisfied=False,
                                reason='unknown_device')

        # A COLOUR is checked before the read-only and press guards because
        # neither applies to it: a light is not read-only, and a colourable
        # light is not a button. Checking here also keeps the "is this device
        # even colourable?" question next to the only place that can answer it
        # honestly -- the device record.
        if slots.get('colour') is not None:
            return self._set_colour(did, device, slots['colour'])

        # A PRESS is its own kind of action, and has to be checked before the
        # read-only test and before _apply_state. A button's state translates
        # to {} -- nothing to read, nothing to switch -- so _apply_state's
        # final fallback would try to FLIP it, writing an `on` key onto a
        # device that has none and reporting a change that never happened.
        if self.dm.is_pressable(device):
            return self._press(did, slots)

        # A READ-ONLY device is not a failure to act on -- it is a device with
        # nothing to act on. A power meter measures the line; you can no more
        # set its reading than set a thermometer's. Home Assistant agrees:
        # there is no sensor.turn_on, because nothing exists at the far end to
        # receive a value.
        #
        # Refusing HERE, before _apply_state, matters. That method's final
        # fallback is "flip the current state", so a sensor reaching it would
        # have had `on` written into a state dict that has no `on` -- inventing
        # a switch on a device that has none.
        if self.dm.is_readable_only(device):
            return self._result('noop', did, satisfied=False,
                                reason='read_only_device')

        # The extractor's switch slot is AUTHORITATIVE. slots['switch'] is
        # always present in the dict it returns, so None means "determined NOT
        # to be a switch", not "unknown" -- e.g. the prepositional 'on' in
        # "volume on the tv" is deliberately rejected there, along with 'on'
        # after a non-phrasal verb. The old fallback re-scanned the raw text
        # for the bare word "on"/"off" whenever switch was None, which undid
        # exactly that disambiguation and switched devices on for commands
        # that never asked. There is no fallback now, by design.
        result = self._apply_state(did, device, slots)

        # Record the device as what this speaker was last talking about only
        # if something reached it. Touching after a rejected write left a
        # back-reference ("turn it off") pointing at a device the command
        # never actually moved.
        if result['outcome'] != 'error':
            self.dm.touch(speaker_id, did)

        # NOTE: outbound transport belongs HERE, on the far side of the write,
        # and nowhere else. No outbound publisher exists yet -- the mqtt module
        # is an inbound state subscriber -- so a state change currently lands
        # in DeviceManager only. There is deliberately no placeholder method:
        # an empty one taking three arguments it never reads is dead weight,
        # and a commented-out one is worse, since nothing can check it against
        # the signature it claims to implement.
        return result

    def _set_colour(self, did: str, device: Dict[str, Any],
                    rgb: Any) -> Dict[str, Any]:
        """Set a light's colour.

        REFUSES rather than approximating when the light cannot do colour. A
        dimmable-only bulb asked to turn blue has no sensible fallback --
        setting it to full brightness would be a different instruction, and
        doing nothing while reporting success would be a lie. Say so.
        """
        if not self.dm.is_colourable(device):
            return self._result('noop', did, satisfied=False,
                                reason='not_colourable')
        if not self.dm.set_colour(did, tuple(rgb)):
            return self._result('error', did, satisfied=False,
                                reason='colour_failed')
        return self._result('acted', did, satisfied=True,
                            change='colour', value=list(rgb))

    def _press(self, did: str, slots: Slots) -> Dict[str, Any]:
        """Fire a momentary action.

        NO before/after comparison, because there is nothing to compare. A
        press leaves no local state, and an IR blaster is one-way -- the beam
        leaves and nothing comes back. `satisfied` therefore means "the request
        reached Home Assistant", which is the strongest claim this device
        type can honestly support.

        A VALUE OR AN OFF IS REFUSED rather than pressed anyway. "Turn off the
        play button" is not a thing the hardware can express, and pressing PLAY
        in response would be the opposite of what was asked. Saying so is
        better than doing something adjacent.
        """
        if slots.get('absolute_value') is not None or slots.get('delta') is not None:
            return self._result('noop', did, satisfied=False,
                                reason='press_takes_no_value')
        if slots.get('switch') == 'off':
            return self._result('noop', did, satisfied=False,
                                reason='press_cannot_turn_off')

        if not self.dm.press(did):
            return self._result('error', did, satisfied=False,
                                reason='press_failed')
        return self._result('acted', did, satisfied=True, change='pressed')

    # ── the decision ──────────────────────────────────────────────────

    def _apply_state(self, did, device, slots) -> Dict[str, Any]:
        """Decide and apply the concrete state change. Numeric targets/deltas
        take priority over a bare on/off so a stray prepositional 'on'
        ('volume on the tv') can't clobber a real value."""
        sk = device.get("state_key") or "level"
        wants_value = (slots.get('absolute_value') is not None
                       or (slots.get('delta') is not None
                           and slots.get('delta_direction')))

        if wants_value:
            # FROM WHAT THE DEVICE IS NOW, not from what the record last
            # said. A step up ("brighten it") is current + step, and the
            # record keeps the last value it was told -- through a board
            # being away, a wall switch, its own web page. get_state merges
            # the board's latest report (or asks Home Assistant) onto it.
            try:
                live = self.dm.get_state(did)
            except Exception:                       # noqa: BLE001
                live = None
            if isinstance(live, dict) and live:
                device = {**device, "state": live}
            cmd = build_command(device, slots)
            if cmd['action'] == 'set':
                return self._set_from_cmd(did, device, cmd, sk)
            # 'flip' here means the arithmetic had nothing to work from: no
            # numeric baseline, or a value slot that wasn't a number. Going
            # straight to the toggle is deliberate. The old code fell into the
            # switch branch instead, so a command carrying BOTH a value and an
            # on/off particle quietly became a plain on/off — the exact
            # inversion of the precedence build_command documents.
            return self._flip(did, reason='no_numeric_baseline')

        switch = slots.get("switch")
        if switch in ('on', 'off'):
            want = (switch == 'on')
            changes: Dict[str, Any] = {"on": want}
            # A POSITIONAL on/off (a gate whose words are open/closed and
            # whose state is a level) is a move to the end of its travel:
            # "open" is the top of the range, "close" the bottom. Written
            # with the on/off so the stored position is true and the board
            # driver can send the position it needs (2026-09-10).
            if device.get("state_key") == "level" and device.get("state_words"):
                lo = _as_float(device.get("range_min"))
                hi = _as_float(device.get("range_max"))
                if lo is not None and hi is not None:
                    changes["level"] = hi if want else lo
            write = self._write(did, changes)
            return self._settle(did, write, change=switch)

        # Nothing specified -> flip to the OPPOSITE concrete state. HA can't
        # act on "toggle" (it doesn't carry a target state), so read the
        # current state here and resolve to a definite on/off. The reported
        # change is the concrete result ('on' or 'off'), never 'toggle', so
        # whatever publishes downstream sends a real command.
        return self._flip(did)

    def _flip(self, did, reason: str = "") -> Dict[str, Any]:
        current = self.dm.get_state(did) or {}
        want = not current.get("on", False)
        write = self._write(did, {"on": want}, before=current)
        result = self._settle(did, write, change='on' if want else 'off')
        if result['outcome'] == 'noop':
            # A flip that changed nothing is NOT "already as you asked" --
            # nobody asked for a particular state. Reporting it as satisfied
            # would tell the phone a toggle worked when the device ignored it.
            result['satisfied'] = False
            result['reason'] = reason or 'flip_had_no_effect'
        elif reason:
            result['reason'] = reason
        return result

    def _set_from_cmd(self, did, device, cmd, sk) -> Dict[str, Any]:
        """Apply a concrete {'action':'set', 'key', 'value'} from build_command.

        One implementation for both the absolute and the delta path. They had
        diverged: the absolute path derived on/off from the value while the
        delta path hardcoded on=True.

        The other half of that bug is fixed here too. For a BINARY device the
        value decided — but for a NUMERIC one this method still wrote
        `on=True` unconditionally, so "turn it all the way down" and "set it to
        minimum" produced value=range_min AND switched the device on. A dimmer
        sitting at 0% while reporting itself on is the same defect the comment
        above claimed to have closed, surviving on the other branch.
        """
        key = cmd.get('key') or sk
        value = cmd['value']

        if key == "on" or isinstance(value, bool):
            on_val = bool(value)
            write = self._write(did, {"on": on_val})
            return self._settle(did, write, change='on' if on_val else 'off')

        # A numeric target at or below the floor IS off; above it, on -- but
        # ONLY on a scale whose floor is zero (a dimmer, a fan). A thermostat's
        # floor is its lowest setting, not off: "set the AC to 16" on a 16-30
        # range used to send set_temperature(16) and then turn_off.
        floor = _as_float(device.get('range_min'), float(DEFAULT_ENVELOPE['range_min']))
        numeric = _as_float(value)
        if numeric is None or floor != 0.0:
            on_val = True
        else:
            on_val = numeric > floor

        write = self._write(did, {key: value, "on": on_val})
        return self._settle(did, write, change='set', key=key, value=value)
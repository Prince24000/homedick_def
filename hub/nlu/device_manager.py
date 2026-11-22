import contextvars
import json, os, re, math, time
from collections import defaultdict
from dataclasses import dataclass, field
from enum import Enum
from typing import (Optional, Dict, List, Sequence, Set, Tuple, Any,
                    Callable, Iterable)

from core.config import (DEVICES_JSON_PATH, CONTEXT_TTL_SECONDS,
                    SEMANTIC_THRESHOLD_TFIDF, HA_STALE_SECONDS)
from core.hv_common import event, times_add
from core.locked_store import JsonRegistry

# The pin catalogue, so a board that has just come alive can be turned into
# devices here rather than in a module of its own. A plain import: it is pure
# data with no dependencies, and an ImportError with a real traceback beats a
# fallback branch that silently disables board registration.
from esp_firmware import esphome_catalog as _cat

# A VERSION CHECK, because the alternative is worse than it sounds.
#
# match_board reads `DeviceType.produces` -- the field saying what each thing
# becomes in Home Assistant. Against an older catalogue that predates it, the
# failure is `AttributeError: 'DeviceType' object has no attribute
# 'produces'` raised from the middle of a matching loop, which reads as a bug
# in the matcher and sends you reading code that is fine.
#
# It is not a bug, it is two files from different days. Said plainly, once,
# at import, naming the file to replace.
if not hasattr(_cat.DeviceType, "produces"):
    raise ImportError(
        f"{_cat.__file__} is older than this device_manager.py expects: its "
        f"DeviceType has no 'produces' field, so a board's devices cannot be "
        f"matched to Home Assistant entities. Replace that file with the "
        f"current esphome_catalog.py -- the two move together.")

#: Domains where sending the VALUE also powers the device on, so a separate
#: turn_on afterwards would be noise. `light.turn_on(brightness_pct=…)` is
#: literally the turn-on call; a fan given a percentage spins; a cover or
#: valve moved to a position is by definition open at it.
#:
#: climate and water_heater are deliberately ABSENT even though they are in
#: HA_VALUE_SERVICES: their value service is `set_temperature`, which stores
#: a target and starts nothing. See _push_to_ha, where treating them as
#: members set a radiator's target and never lit it (2026-09-14).
_VALUE_SERVICE_POWERS_ON = frozenset({"light", "fan", "cover", "valve"})

#: Home Assistant's two ways of saying "I have no idea". Neither is a state
#: the device is IN; both mean the integration has lost touch with it, and
#: the difference between them is only how it was lost. They must never be
#: read as "off" -- see translate_ha_state.
HA_NO_ANSWER_STATES = frozenset({"unavailable", "unknown", "none", ""})

#: For the switchable domains, the states that genuinely mean OFF. Anything
#: else (that is not a no-answer state above) is ON.
#:
#: WHY A DENYLIST AND NOT AN ALLOWLIST. The old code allowlisted the ON
#: states and the list was hand-typed, so it was wrong for three domains at
#: once: a water_heater's state IS its operation mode (`electric`,
#: `performance`, `high_demand`, `heat_pump`, `gas` -- only `eco` was
#: listed), a climate can be `dry` or `fan_only`, and an alarm panel is
#: `armed_home`/`armed_away`/`armed_night`/`triggered`/`pending` -- none of
#: which were there. Every one of those read as OFF.
#:
#: That is not merely a wrong label. The executor computes `changed` against
#: this baseline, so a wrong False SUPPRESSES THE COMMAND: a tank heating in
#: `electric` mode answered "turn off the water heater" with "it's already
#: off" and never called the service (2026-09-14).
#:
#: The off-words are few, closed, and defined by HA itself, so the denylist
#: cannot rot the same way -- a mode nobody here has heard of is ON, which
#: for "is this thing doing something" is the right default.
#:
#: AND THE TWO MISTAKES ARE NOT EQUAL, which is what decides every doubtful
#: word below. A wrong ON costs a redundant service call that HA discards. A
#: wrong OFF makes the executor answer "it's already off" and send NOTHING.
#: So anything genuinely ambiguous is left OUT of this set and reads as on:
#:
#:   `idle`   a media player that is idle is powered up, and a vacuum that
#:            is idle is out on the floor. Off for either would suppress
#:            the very command that fixes it.
#:   `paused` paused is playing, stopped.
#:   `locked` belongs to one domain only, and there it means the opposite
#:            of off -- see HA_ON_STATES_BY_DOMAIN rather than special-
#:            casing a word two domains could share.
HA_OFF_STATES = frozenset({
    "off",        # every switchable domain
    "closed",     # cover, valve
    "standby",    # media_player -- a TV in standby is off in every sense
    "docked",     # vacuum, home and charging
    "disarmed",   # alarm_control_panel
})

#: …except where a word means the opposite of what it means elsewhere. A
#: lock reporting `locked` is SECURED, which this hub models as ON; a cover
#: reporting `closed` is shut, which is off. Rather than let one shared word
#: list arbitrate between them, the domain that inverts states its own ON
#: words and is asked first.
HA_ON_STATES_BY_DOMAIN = {
    # `jammed` is deliberately NOT here: a lock that jammed is not locked,
    # and for a securement device the unsafe reading is the one to avoid.
    "lock": frozenset({"locked", "locking"}),
}


def _as_number(value: Any) -> Optional[float]:
    """float(value), or None if it isn't one. Bools excluded: True would read
    as 1.0 and make a switch look like a gradable device."""
    if isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None

# MPNet cosine similarities sit in a different range than the TF-IDF
# fallback's word-overlap scores, so each path has its own threshold. Both now
# come from config alongside every other tunable.
SEMANTIC_DEBUG = os.environ.get("SEMANTIC_DEBUG", "") not in ("", "0", "false")

def _norm(text: Any) -> str:
    """Down to letters and digits. 'Ceiling Light' == 'ceiling_light'."""
    return re.sub(r"[^a-z0-9]", "", str(text or "").lower())


def _domain(entity_id: Any) -> str:
    """'light' for 'light.kitchen'; '' for anything that is not an entity id."""
    if not isinstance(entity_id, str) or "." not in entity_id:
        return ""
    return entity_id.split(".", 1)[0]


#: WHO THE DEVICE WRITES OF THIS TASK ARE FOR (2026-10-11). Set by
#: snapshot(owner) at the start of a command; every commanded write after it
#: in the same task -- and in the worker threads it starts -- is recorded as
#: theirs. So two people's commands can overlap and each "undo" puts back
#: only what that person changed. Writes from no command (an automation, a
#: board reporting) carry None.
ACTOR: "contextvars.ContextVar[Any]" = contextvars.ContextVar(
    "homevoice_actor", default=None)


@dataclass
class BoardMatch:
    """One thing placed on a board, and what became of it in Home Assistant."""
    type_id: str
    name: str
    entity_id: Optional[str] = None
    #: Entities from the SAME physical part that were not registered -- a
    #: BME280's humidity and pressure. Offered, never assumed.
    extras: List[str] = field(default_factory=list)
    reason: str = ""            # why it was not matched, if it wasn't
    #: Registered WITHOUT a Home Assistant entity, because there was never
    #: going to be one. An IR blaster is commanded and reports nothing; an
    #: IR receiver reports EVENTS, which Home Assistant does not turn into
    #: entities either. Both were being skipped entirely -- so a person who
    #: placed one saw it on the pin screen, saw it in `Gpio 255`, and then
    #: could not find it anywhere in the device list. The hub knew, and
    #: never said.
    local_only: bool = False

    @property
    def matched(self) -> bool:
        return bool(self.entity_id)

    @property
    def registerable(self) -> bool:
        """Should this end up in devices.json at all?

        An entity is one way to deserve a record. Being a real thing on a
        real pin is another -- and the registry has always allowed
        `ha_entity_id` to be None, so nothing new is being invented here.
        """
        return self.matched or self.local_only


class Action(str, Enum):
    EXECUTE = "execute"
    PICK = "pick"
    ELSEWHERE = "elsewhere"
    NONE = "none"


class _Embedder:
    """Fallback semantic scorer: from-scratch TF-IDF cosine over description
    text. Used only when no real embed_fn (MPNet) is injected — e.g. CLI tools
    that manage devices without loading the 100MB model. It matches literal
    words, not meaning, so 'cool the room' won't match a description that says
    'lowers the temperature'. The MPNet path is preferred whenever available."""
    def score_devices(self, query: str, devices: Dict[str, Any],
                      threshold: float = SEMANTIC_THRESHOLD_TFIDF) -> List[str]:
        query = str(query or "")
        if not query:
            return []
        corpus = [(d.get("description") or "") for d in devices.values()]
        df: Dict[str, int] = defaultdict(int)
        for c in corpus:
            for w in set(c.lower().split()):
                df[w] += 1
        N = len(corpus) or 1

        def vec(toks: List[str]) -> Dict[str, float]:
            c: Dict[str, int] = defaultdict(int)
            for w in toks:
                c[w] += 1
            n = len(toks) or 1
            return {w: (c[w] / n) * math.log((N + 1) / (df[w] + 1)) for w in c}

        q = query.lower().split()
        vq = vec(q)
        nq = sum(x * x for x in vq.values()) ** 0.5
        scored = []
        for did, d in devices.items():
            desc = d.get("description") or ""
            if not desc:
                continue
            vd = vec(desc.lower().split())
            dot = sum(vq[w] * vd[w] for w in vq if w in vd)
            nd = sum(x * x for x in vd.values()) ** 0.5
            s = dot / (nq * nd) if nq and nd else 0.0
            if s >= threshold:
                scored.append((s, did))
        scored.sort(reverse=True)
        return [did for _, did in scored]


class DeviceManager(JsonRegistry):
    # devices.json is SHARED BETWEEN PROCESSES: the service (ws_server) and a
    # terminal main.py each hold a DeviceManager over the same file, and every
    # save writes the whole registry. Without a lock and a re-read, whichever
    # process saved last won: a device added in the terminal vanished from
    # disk the next time the service recorded a light switching (2026-09-23).
    # JsonRegistry: lock, reload if changed, modify, atomic write, quarantine.
    _TAG = "DEVICE"
    _WHAT = "devices"
    _MISSING_NOTE = ("No file at {path} -- starting with zero devices. If you "
                     "expected existing devices to load, check this is the "
                     "SAME path they were saved to.")

    # Domains with clean, unambiguous on/off semantics -- covers the large
    # majority of typical voice commands. Domains NOT listed here (vacuum's
    # start/pause/dock, alarm_control_panel's arm/disarm modes, etc.) don't
    # map cleanly onto a single "on"/"off" concept, so set_state fails loud
    # for those rather than silently guessing the wrong service call.
    HA_ON_OFF_SERVICES = {
        "light": ("turn_on", "turn_off"),
        "switch": ("turn_on", "turn_off"),
        "fan": ("turn_on", "turn_off"),
        "humidifier": ("turn_on", "turn_off"),
        "valve": ("open_valve", "close_valve"),
        "water_heater": ("turn_on", "turn_off"),
        "climate": ("turn_on", "turn_off"),
        "lock": ("lock", "unlock"),
        "cover": ("open_cover", "close_cover"),
        # ADDED 2026-09-14, because config.CONTROLLABLE_DOMAINS has offered
        # both in the registration picker all along while this table mapped
        # neither -- so registering a TV and then saying "turn off the TV"
        # made no HA call at all, reported success, and flipped the tile
        # while the TV kept playing. See _push_to_ha's `services is None`
        # branch, which used to say True to exactly this.
        "media_player": ("turn_on", "turn_off"),
        # start/return_to_base, NOT turn_on/turn_off: HA deprecated the
        # latter for vacuums and most integrations no longer implement
        # them, so they would have failed silently at the far end -- the
        # same lie, one layer further out. "Off" for a robot means go home.
        "vacuum": ("start", "return_to_base"),
    }

    #: Domains this hub can register and READ but deliberately refuses to
    #: drive -- distinct from HA_READ_ONLY_DOMAINS, where nothing could
    #: accept a new state, and from "no mapping yet", which is work not
    #: done. Here there is a service and it is a BAD IDEA to guess it.
    #:
    #: alarm_control_panel is the whole membership. Its "on" is not one
    #: thing (alarm_arm_home / _away / _night / _vacation are four
    #: different promises about who is in the house), and its "off" --
    #: alarm_disarm -- takes a code this hub does not hold and must not
    #: start storing. A wrong guess here arms the wrong mode or reports an
    #: alarm disarmed that is still live, which is worse than any silence.
    HA_REFUSE_TO_DRIVE = {"alarm_control_panel"}

    #: Domains that can be READ and never written. Home Assistant receives
    #: their state from somewhere; nothing exists at the other end to accept
    #: a new one. Kept separate from "no mapping yet" because the two need
    #: opposite answers: one is work not done, the other is a fact.
    HA_READ_ONLY_DOMAINS = {"sensor", "binary_sensor", "camera"}

    #: Domains whose only action is a MOMENTARY press. No on/off, no value --
    #: pressing is the entire vocabulary.
    #:
    #: This is how IR reaches the system. A learned remote code becomes a
    #: button.* entity in Home Assistant -- identically whether it was captured
    #: by hand into ESPHome YAML or by the OpenIRBlaster integration -- and
    #: pressing it blasts the code. Nothing downstream can tell the two routes
    #: apart, which is why button support did not have to wait on either.
    HA_PRESS_SERVICES = {
        "button": "press",
        "scene": "turn_on",     # HA models a scene as fire-once, same shape
        "script": "turn_on",
    }

    # (service, data field) for setting a NUMERIC value, per domain.
    #
    # There was no equivalent of this table, so set_state only ever called HA
    # for the "on" key. A numeric write -- "set the brightness to 50", every
    # delta, every maximizer -- updated our local copy, returned True, and
    # never left the process. The light did not change and the caller was told
    # it had. Domains absent here have no single obvious numeric service and
    # are reported rather than guessed at, same principle as HA_ON_OFF_SERVICES.
    #
    # brightness_pct, not brightness: our "level" is normalised to 0-100 on the
    # way in (see translate_ha_state), so it must go back out on the same
    # scale rather than being posted as a raw 0-255 value.
    HA_VALUE_SERVICES = {
        "light":        ("turn_on", "brightness_pct"),
        "fan":          ("set_percentage", "percentage"),
        "climate":      ("set_temperature", "temperature"),
        "water_heater": ("set_temperature", "temperature"),
        # A cover or valve has a position as well as open/close (the
        # on/off table above): "open the blind halfway" is a number, and
        # without these rows it logged success that never left the
        # process (2026-09-10).
        "cover":        ("set_cover_position", "position"),
        "valve":        ("set_valve_position", "position"),
    }

    # Where a domain's numeric ENVELOPE comes from: which HA attributes carry
    # the range and step, what to fall back on when the entity doesn't send
    # them, and what our own `unit` field should say.
    #
    # This table exists because registration had no idea a thermostat is not a
    # dimmer. A climate entity registered through the HA picker kept the
    # generic 0-100 "percent" defaults while holding 21 degrees, so "all the
    # way up" resolved to range_max = 100 and asked for a hundred degrees, and
    # a plain "warmer" moved by step = 10 -- ten degrees. HA already publishes
    # min_temp/max_temp/target_temp_step; nothing was reading them.
    #
    # `level` is the state key throughout, matching translate_ha_state.
    HA_VALUE_PROFILES: Dict[str, Dict[str, Any]] = {
        # `unit` is the FALLBACK word, used only when the entity itself
        # publishes none (2026-09-14). It is what makes this table's answer
        # match its own docstring, which has always promised "range, step,
        # unit" while returning three keys and no unit.
        "climate": {
            "min_attr": "min_temp", "max_attr": "max_temp",
            "step_attr": "target_temp_step",
            "fallback": (7.0, 35.0, 0.5), "unit": "C",
        },
        "water_heater": {
            "min_attr": "min_temp", "max_attr": "max_temp",
            "step_attr": "target_temp_step",
            "fallback": (30.0, 60.0, 1.0), "unit": "C",
        },
        "fan": {
            # percentage is 0-100 by definition; only the step is published,
            # derived by HA from the fan's speed count.
            "min_attr": None, "max_attr": None, "step_attr": "percentage_step",
            "fallback": (0.0, 100.0, 1.0), "unit": "%",
        },
        "light": {
            # brightness arrives 0-255 and translate_ha_state normalises it to
            # 0-100, so the envelope is expressed on the SCALE WE STORE, not
            # the one HA sends. HA publishes no brightness step; 5% is a
            # perceptible dim increment.
            "min_attr": None, "max_attr": None, "step_attr": None,
            "fallback": (0.0, 100.0, 5.0), "unit": "%",
        },
        # A cover's or valve's position: 0 closed, 100 open, HA's own
        # convention; a quarter is a sensible nudge for a gate or a blind.
        # Registered as open/closed WITH a position (2026-09-10).
        "cover": {
            "min_attr": None, "max_attr": None, "step_attr": None,
            "fallback": (0.0, 100.0, 25.0), "unit": "%",
        },
        "valve": {
            "min_attr": None, "max_attr": None, "step_attr": None,
            "fallback": (0.0, 100.0, 25.0), "unit": "%",
        },
    }

    def __init__(self, path: str = DEVICES_JSON_PATH,
                 embed_fn: Optional[Callable[[str], Any]] = None,
                 ha_client: Optional[Any] = None):
        """embed_fn: the real MPNet embedder (main.py passes model_module.embed).
        When present, device descriptions are matched semantically by MPNet.
        When absent, the TF-IDF `embedder` fallback is used so device-management
        tooling works without loading the model.

        ha_client: optional HAClient instance. When a device's record has an
        `ha_entity_id` field AND this is set, get_state/set_state talk to
        Home Assistant live instead of (as well as) our own local state dict --
        HA remains the source of truth for name/state; our own devices.json
        stays the semantic sidecar (description, embedding, secure, room),
        exactly as designed. Devices with no ha_entity_id are entirely
        unaffected -- this is fully backward compatible with pre-HA devices.
        """
        self.devices: Dict[str, Any] = {}
        self.rooms: set = set()
        self.context: Dict[Any, str] = {}
        self._group: Dict[Any, List[str]] = {}
        self._touched_at: Dict[Any, float] = {}
        #: owner -> snapshot (JSON of every device's state), one per person:
        #: see snapshot(). The terminal and tests are owner None.
        self._undo: Dict[Any, str] = {}
        #: device id -> who last CHANGED it by command (ACTOR), so one
        #: person's undo never puts back another person's later change.
        self._changed_by: Dict[str, Any] = {}
        #: owner -> the secure devices their last undo() declined to move.
        self._held: Dict[Any, List[str]] = {}
        #: WHEN each HA-backed device was last actually heard from, by
        #: device id -> monotonic seconds (2026-09-12). Home Assistant
        #: pushes nothing to this hub -- there is no event ear and no
        #: subscription -- so an HA sensor's stored state is only ever as
        #: fresh as the last time something asked. Without a timestamp the
        #: home screen could not tell "21 degrees" from "21 degrees three
        #: hours ago", and drew both as live.
        #:
        #: DELIBERATELY NOT IN THE RECORD. It would then be persisted, and
        #: a hub that has just restarted would claim its saved numbers were
        #: heard seconds ago when nothing has been heard at all. Empty
        #: after a restart is the truth.
        self._ha_seen: Dict[str, float] = {}
        #: The unit Home Assistant itself attached to each device's state
        #: (unit_of_measurement), apart from any unit typed on a form: what
        #: tells a power or current reading (the main meter). Not persisted.
        self._ha_units: Dict[str, str] = {}
        self.embed_fn = embed_fn
        self.embedder = _Embedder()
        self.ha_client = ha_client
        #: The DIRECT road to a board device (local-first, 2026-08-26):
        #: a callable (device_id, change_dict) -> bool that drives the
        #: board over its own MQTT. Wired by main.py to the ESP builder's
        #: drive_item_sync; when unset, devices without an HA entity
        #: cannot be actuated and set_state says so honestly.
        self.mqtt_driver: Optional[Callable[[str, Dict[str, Any]], bool]] = None
        #: Why the driver's last attempt on a device failed ("offline", ...),
        #: or "". Set by the shell beside mqtt_driver; see _board_is_offline.
        self.drive_problem: Optional[Callable[[str], str]] = None
        #: The board's OWN word about its state: a callable
        #: (device_id) -> {key: value} over what it last published on
        #: stat/# and tele/+/SENSOR. Wired by main.py to the automation
        #: engine's cache, which already holds that subscription open.
        #: Unset leaves get_state exactly as it was.
        self.state_reader: Optional[Callable[[str], Dict[str, Any]]] = None
        self._init_registry(path)

    # ── JsonRegistry hooks ────────────────────────────────────────────────
    def _valid(self, raw: Any) -> bool:
        return isinstance(raw, dict) and all(isinstance(d, dict) for d in raw.values())

    def _clear(self) -> None:
        self.devices = {}
        self._sync_rooms()

    def _document(self) -> Dict[str, Any]:
        return self.devices

    def _install(self, raw: Dict[str, Any]) -> None:
        self.devices = raw
        for d in self.devices.values():
            d["name"] = str(d.get("name") or "").lower()
            d["room"] = str(d.get("room") or "").lower()
            for k in ("state", "ir_codes", "state_words"):
                if not isinstance(d.get(k), dict):
                    d[k] = {}
            if not isinstance(d.get("state_key"), str):
                d["state_key"] = None
            # MIGRATION: the "unit" field is gone. It was written by
            # every registration path but read for exactly one thing --
            # `unit == "binary"` -- and state_key already says that.
            # Records saved before this change still carry it, so a
            # device whose ONLY binary marker was the unit (a lock, in
            # practice) would come back as gradable and accept
            # "set the front door to 50". Promote it once, then drop.
            if d.pop("unit", None) == "binary" and d.get("state_key") != "on":
                event("DEVICE", f"Migrating {d.get('name') or 'device'!r}: "
                      f"unit=binary -> state_key=on")
                d["state_key"] = "on"
            # MIGRATION: state_key had three values and only ever
            # needed two. "temperature" was written by the manual
            # registration path while translate_ha_state writes every
            # domain's value into "level" -- so such a device looked up
            # a field nothing populates, build_command found no
            # baseline, and "make it warmer" returned flip and toggled
            # the thermostat OFF. The range says what kind of number it
            # is; the key never needed to.
            if d.get("state_key") == "temperature":
                event("DEVICE", f"Migrating {d.get('name') or 'device'!r}: "
                      f"state_key=temperature -> level")
                d["state_key"] = "level"
                state = d.get("state")
                if isinstance(state, dict) and "temperature" in state:
                    # Carry the stored reading across, so the device
                    # keeps its baseline instead of starting blank.
                    state.setdefault("level", state.pop("temperature"))
            # Embeddings: keep a valid list, drop anything malformed so
            # backfill can recompute it cleanly.
            if not isinstance(d.get("embedding"), list):
                d["embedding"] = None
        self._sync_rooms()

    def _sync_rooms(self) -> None:
        self.rooms = {d.get("room", "") for d in self.devices.values()}

    #: Secure devices that the last undo() declined to move, by name. Read by
    #: main._do_undo so the person hears WHY the door did not go back.
    undo_held: List[str] = []

    def _snapshots(self) -> Dict[Any, str]:
        if not isinstance(self._undo, dict):      # cleared to None by a test
            self._undo = {}
        return self._undo

    def snapshot(self, owner: Any = None) -> None:
        """Remember every device's STATE (and only its state) -- for `owner`.

        It used to serialise the whole registry, and undo() put the whole
        registry back -- so "never mind" after a voice command also erased any
        device registered or edited since that command.

        ONE PER PERSON (2026-10-11). Two phones' commands now run side by
        side, so one shared snapshot would let Chidi's "undo" put back what
        Augusta just did. Each owner keeps their own, and the writes that
        follow in this task are recorded as theirs (ACTOR)."""
        ACTOR.set(owner)
        with self._lock:
            self._reload_if_changed()
            self._undo = {**self._snapshots(), owner: json.dumps(
                {did: d.get("state") or {} for did, d in self.devices.items()})}

    def held_for(self, owner: Any = None) -> List[str]:
        """The secure devices `owner`'s last undo() declined to move."""
        return list(self._held.get(owner) or [])

    def undo(self, only: Optional[Sequence[str]] = None, owner: Any = None
             ) -> List[Tuple[str, Dict[str, Any]]]:
        """Restore the snapshotted state of every device that still exists.

        `only`: just these devices ("undo the bulbs"). The snapshot is then
        KEPT, so what else the last command changed can still be undone.

        RETURNS [(device_id, before_state), ...] for each device whose state
        differed from the snapshot -- the caller must push each before_state
        to the device (main._set_state); this method only rewrites the local
        record, it drives nothing. [] when there is no snapshot or nothing
        differed. It used to return a bool, and main reported "Undone" while
        the device it had just switched stayed exactly as it was.

        A SECURE DEVICE IS NEVER REVERTED (2026-09-12). "lock the front door"
        passes the active voice challenge and then snapshots, so a bare
        "undo" -- no challenge, no device named, the easiest word in the
        vocabulary to say -- put the lock back exactly as it was. That is an
        unlock with none of the checks an unlock requires, and it was
        reachable. The skip is HERE rather than in main because this method
        has already rewritten the record by the time main sees the list: a
        caller that declined to push would leave the registry claiming a door
        is open while it is shut, which is the one lie this file must not
        tell. Their names are left in `undo_held` so the person can be told
        rather than left wondering.
        """
        with self._writing():
            self.undo_held = []
            self._held[owner] = self.undo_held
            snaps = self._snapshots()
            if snaps.get(owner) is None:
                return []
            saved = json.loads(snaps[owner])
            if only is None:
                self._undo = {k: v for k, v in snaps.items() if k != owner}
            else:
                wanted = set(only)
                saved = {did: st for did, st in saved.items() if did in wanted}
            # ONLY WHAT THIS OWNER CHANGED: a device somebody else switched
            # since is theirs to undo, not this person's. (No owner -- a
            # caller that names nobody -- is the whole house, as before.)
            if owner is not None:
                saved = {did: st for did, st in saved.items()
                         if self._changed_by.get(did) == owner}
            reverted: List[Tuple[str, Dict[str, Any]]] = []
            for did, before in saved.items():
                d = self.devices.get(did)
                # A READING THAT MOVED IS NOT AN UNDO. A sensor's record was
                # rewritten to the number it showed at the snapshot and then
                # skipped by the caller (nothing sets a sensor), so the
                # home tile drew that old number, stamped fresh, until the
                # next poll.
                if d is None or (d.get("state") or {}) == before \
                        or self.is_readable_only(d):
                    continue
                if d.get("secure"):
                    self.undo_held.append(d.get("name") or did)
                    continue
                d["state"] = dict(before)
                reverted.append((did, dict(before)))
            if reverted:
                self._save()
            return reverted

    def _embed_description(self, description: str) -> Optional[List[float]]:
        """MPNet-embed a description to a plain list (JSON-serialisable). Returns
        None if no embed_fn is available or the text is empty, so callers stay
        robust when the model isn't loaded."""
        if not self.embed_fn or not description:
            return None
        try:
            import numpy as np
            v = np.asarray(self.embed_fn(description), dtype="float32")
            n = float(np.linalg.norm(v))
            if n < 1e-9:
                return None
            normalised: List[float] = (v / n).tolist()
            return normalised
        except Exception as e:
            event("DEVICE", f"Device embedding failed: {e}")
            return None

    def add(self, did: str, **kwargs) -> bool:
        did = str(did)
        if not did:
            return False
        _st = kwargs.get("state")
        _ir = kwargs.get("ir_codes")
        _sw = kwargs.get("state_words")
        description = str(kwargs.get("description") or "")
        device = {
            "name": str(kwargs.get("name") or "").lower(),
            "room": str(kwargs.get("room") or "").lower(),
            "description": description,
            "embedding": self._embed_description(description),
            "state": dict(_st) if isinstance(_st, dict) else {},
            "secure": kwargs.get("secure"),
            "transport": kwargs.get("transport"),
            "ir_codes": dict(_ir) if isinstance(_ir, dict) else {},
            "state_words": dict(_sw) if isinstance(_sw, dict) else {},
            "step": kwargs.get("step"),
            "range_min": kwargs.get("range_min"),
            "range_max": kwargs.get("range_max"),
            "state_key": kwargs.get("state_key"),
            "ha_entity_id": kwargs.get("ha_entity_id"),
            # WHETHER ITS NUMBERS ARE PUT ON DISPLAY -- the home tiles and
            # the hallway panel, one answer for both.
            #
            # The ONLY stored flag on this record that is a PREFERENCE
            # rather than a fact. Everything else here is derived or
            # measured, and this file has already deleted three stored
            # fields for contradicting what could be derived; "does this
            # thing measure something" is one of those and is never asked.
            # "Do you want to look at it" is not derivable from anything,
            # which is what earns it a place.
            #
            # None means never asked -- an older record, or a device from a
            # path that does not offer the question. Readers treat None as
            # yes, so nothing that used to appear disappears on upgrade.
            "show_on_home": kwargs.get("show_on_home"),
            # True/False when the registering path KNOWS (the catalogue's
            # domain for a board item); None means derive it -- see
            # is_readable_only.
            "readable_only": kwargs.get("readable_only"),
            # WHAT THE BOARD CALLS ITS MAIN NUMBER ("power" for a meter,
            # "temperature" for a DHT). The stored state key is always
            # "level" (see Produces.as_meta), but the board publishes under
            # its own name, so the tile that shows "the reading" needs to
            # know which of the published fields IS the reading. None for
            # actuators and for records older than the field (backfilled).
            "reading_key": kwargs.get("reading_key"),
            # WHOSE VALUE EACH FIELD HOLDS -- "part", "ha", "ai" or "you",
            # per form field (description, kind, min, max, step, unit). Kept
            # so the form can say it back when the device is edited again:
            # the AI's sentence stays marked as the AI's until a person
            # changes it (2026-09-10). None for records older than this.
            "provenance": (dict(kwargs["provenance"])
                           if isinstance(kwargs.get("provenance"), dict) else None),
        }
        with self._writing():
            if did in self.devices:
                return False
            self.devices[did] = device
            self._sync_rooms()
            self._save()
        return True

    def remove(self, did: str) -> bool:
        did = str(did)
        with self._writing():
            if not did or did not in self.devices:
                return False
            del self.devices[did]
            self.context = {s: d for s, d in self.context.items() if d != did}
            # ...and out of the groups too. last_group filtered deleted ids at
            # read time, so this was invisible rather than harmless: the id sat
            # in _group for the life of the process.
            for speaker, group in list(self._group.items()):
                remaining = [g for g in group if g != did]
                if remaining:
                    self._group[speaker] = remaining
                else:
                    self._group.pop(speaker, None)
                    self._touched_at.pop(speaker, None)
            self._sync_rooms()
            self._save()
        return True

    # ── ESP boards ──────────────────────────────────────────────────────
    #
    # THE SECOND TRIP THIS DELETES. You name things once, on the pin screen:
    # GPIO13 is "kettle". The board is flashed, Home Assistant discovers it,
    # and until this existed you went back through Add device and typed
    # "kettle" again -- for every single thing on the board.
    #
    # Living HERE rather than in a module of its own because the end of the
    # job is self.add(), and a separate class whose whole body was a loop
    # around another object's method is a file to keep in sync for nothing.
    #
    # NO IO IN ANY OF IT. These return facts; the shell does the talking.
    # That is the existing convention in this class and it is what lets the
    # matching be tested against a list of entity ids and nothing else.

    @staticmethod
    def _board_entities(entities: Sequence[str], slug: str,
                        board_name: str) -> List[str]:
        """The entities belonging to one board.

        Tasmota builds entity ids from the device name, so the board's own
        name is the thread. Both the slug and the display name are tried:
        one is what we set, the other is what somebody may have renamed it
        to in Home Assistant.
        """
        keys = {_norm(slug), _norm(board_name)}
        keys.discard("")
        return [e for e in entities
                if any(k in _norm(e.split(".", 1)[-1]) for k in keys)]

    @staticmethod
    def _find_entity(pool: Sequence[str], used: Set[str], domain: str,
                     name: str, reading: str, *, sole: bool) -> Optional[str]:
        """This device's entity: by name first, then by reading.

        ORDER MATTERS. The name is the strong signal -- it is the thing we
        deliberately wrote onto the board during provisioning -- so it is
        tried alone before the reading is considered. Reading-only matching
        is the fallback for sensors, which never carried a name at all.
        """
        want_name, want_read = _norm(name), _norm(reading)
        candidates = [e for e in pool
                      if e.startswith(f"{domain}.") and e not in used]
        tail = {e: _norm(e.split(".", 1)[-1]) for e in candidates}

        for entity in candidates:                       # name AND reading
            if want_name and want_name in tail[entity] and \
                    want_read and want_read in tail[entity]:
                return entity
        for entity in candidates:                       # name alone
            if want_name and want_name in tail[entity]:
                return entity
        for entity in candidates:                       # reading alone
            if want_read and want_read in tail[entity]:
                return entity
        # Last resort, deliberately narrow: ONE unclaimed candidate in the
        # domain, and only ONE device asking for that domain. It rescues a
        # board whose entity was renamed by hand. With two askers it refuses,
        # because a wrong guess here is a device that answers to the wrong
        # words forever.
        if sole and len(candidates) == 1:
            return candidates[0]
        return None

    def match_board(self, items: Sequence[Dict[str, Any]], slug: str,
                    board_name: str,
                    entities: Sequence[str],
                    local_first: bool = False,
                    has_ha: bool = True) -> List[BoardMatch]:
        """Line up what was placed on a board against what turned up in HA.

        Best-effort by construction, and that is fine -- what it must never
        do is guess WRONG in silence. Anything it cannot place comes back
        with a reason, to be registered the ordinary way. A device you are
        told about is an annoyance; a kettle bound to the doorway sensor's
        entity boils when somebody walks past.

        `has_ha` is the difference between "Home Assistant has not shown
        this yet" and "there is no Home Assistant". An EMPTY POOL CANNOT
        TELL THEM APART -- a silent HA and an absent one both hand over
        nothing -- so the caller, which holds the client, says which it is.
        Without it every reason line promised to keep courting a Home
        Assistant that was never configured (her report, 2026-09-01).
        """
        pool = list(self._board_entities(entities, slug, board_name)) or \
            list(entities)
        used: Set[str] = set()
        out: List[BoardMatch] = []

        # How many devices compete for each domain.
        #
        # This exists because of one test. With two relays placed and only
        # the toaster's entity present, the kettle went first, matched
        # nothing by name, fell through to "there is a single unclaimed
        # switch, take it", and bound itself to the toaster. The toaster then
        # matched nothing. Two devices silently wrong, no error anywhere.
        demand: Dict[str, int] = {}
        for item in items:
            device = _cat.BY_ID.get(item.get("type_id", ""))
            if device and device.produces.domain:
                key = device.produces.domain
                demand[key] = demand.get(key, 0) + 1

        for item in items:
            device = _cat.BY_ID.get(item.get("type_id", ""))
            name = str(item.get("name") or "")
            if device is None:
                out.append(BoardMatch(item.get("type_id", "?"), name,
                                      reason="unknown device type"))
                continue

            produces = device.produces
            if not produces.domain:
                # NO ENTITY, BUT STILL A DEVICE. An IR blaster is commanded
                # and reports nothing; an IR receiver reports events, which
                # Home Assistant does not make entities from either. Neither
                # will ever appear in a match pool, and that is not a
                # failure -- there was never going to be an entity.
                #
                # This used to end the story: they were reported as
                # "nothing to register" and skipped, so a person who placed
                # one saw it green on the pin screen and then could not find
                # it in the device list, ever. The board record knew, the
                # chip knew, the registry did not. Now they go in with no
                # ha_entity_id -- which the registry has always permitted --
                # so the system can SAY it has them.
                out.append(BoardMatch(
                    device.id, name, local_only=True,
                    reason=("registered here -- it has no Home Assistant "
                            "entity, because it reports events rather than "
                            "a state") if has_ha else
                           ("registered here -- it reports events rather "
                            "than a state")))
                continue

            found = self._find_entity(
                pool, used, produces.domain, name, produces.envelope[0],
                sole=demand.get(produces.domain, 0) == 1)
            if found is None:
                if local_first:
                    # LOCAL-FIRST (2026-08-26): Home Assistant not showing
                    # a device -- slowly, or ever -- is not a reason to
                    # refuse it a record. It registers here NOW, works over
                    # its own MQTT, and the attach courier keeps trying to
                    # marry it to an entity in the background.
                    #
                    # ...AND WITH NO HOME ASSISTANT AT ALL there is no
                    # courier and nothing to wait for, so the line says
                    # what is true instead of promising a retry against
                    # something that was never configured.
                    out.append(BoardMatch(
                        device.id, name, local_only=True,
                        reason=(f"registered here -- its {produces.domain} "
                                f"entity has not shown up in Home Assistant "
                                f"yet; the hub will keep trying and attach "
                                f"it quietly") if has_ha else
                               "registered here, and driven straight from "
                               "this hub"))
                else:
                    out.append(BoardMatch(
                        device.id, name,
                        reason=f"no {produces.domain} entity turned up "
                               f"for it"))
                continue
            used.add(found)

            extras: List[str] = []
            for reading in produces.extras:
                more = self._find_entity(pool, used, produces.domain, name,
                                         reading, sole=False)
                if more:
                    used.add(more)
                    extras.append(more)
            out.append(BoardMatch(device.id, name, entity_id=found,
                                  extras=extras))
        return out

    @staticmethod
    def board_device_id(slug: str, name: str) -> str:
        base = re.sub(r"[^a-z0-9]+", "_", str(name).strip().lower()).strip("_")
        return f"{slug}.{base or 'device'}"

    @classmethod
    def _part_record(cls, produces: Any
                     ) -> Tuple[Dict[str, Any], bool, Optional[str], Dict[str, Any]]:
        """What the catalogue says a board part IS, for its record: the
        envelope (as_meta), whether it only reports, the name of the reading
        it publishes, and the state a fresh record starts in. One answer for
        register_board, refit_board_device and backfill_readable_only."""
        meta = produces.as_meta()
        sensor = produces.domain in cls.HA_READ_ONLY_DOMAINS
        reading_key = produces.envelope[0] if sensor and produces.envelope else None
        state: Dict[str, Any] = {
            meta["state_key"]: False if meta["state_key"] == "on" else meta["range_min"]}
        if not sensor:
            state["on"] = False                 # an actuator has a switch
        if meta.get("unit"):
            state["unit"] = str(meta["unit"])
        return meta, sensor, reading_key, state

    @staticmethod
    def plain_description(type_id: str, name: str, board_name: str) -> str:
        """A description with no AI involved.

        Always written, even when the AI is going to be asked, so a device is
        never registered with an empty one: if generation is declined or
        fails, this is what remains.
        """
        device = _cat.BY_ID.get(type_id)
        label = device.label.split(" (")[0].lower() if device else type_id
        return f"{name}, a {label} on the {board_name} board."

    def register_board(self, matches: Sequence[BoardMatch], slug: str,
                       board_name: str,
                       descriptions: Optional[Dict[str, Any]] = None
                       ) -> Tuple[List[str], List[str]]:
        """Add the matched devices. Returns (created ids, already-present).

        The envelope comes from the catalogue, which is why nobody is asked
        the four range questions for anything on a board: a relay is binary,
        a dimmer runs 0-100, a BME280 reads -40 to 85.

        `descriptions` maps a device name to EITHER a plain description
        string (what every caller sent until 2026-08-31) OR a dict of the
        answers one registration form collected: description, room, secure,
        show_on_home. Both shapes are read here rather than at three call
        sites, and an absent key keeps exactly the value this method used
        to hard-code -- the board's own name for the room, not secure.
        """
        created: List[str] = []
        skipped: List[str] = []
        for m in matches:
            if not m.registerable:
                continue
            # WHAT KIND OF THING IT IS, from the catalogue, stored once. A
            # dimmer's record was {"level": 0.0} with no "on", and
            # is_readable_only read that absence as "a sensor" -- so every
            # board dimmer and cover answered "nothing to switch" to voice.
            meta, sensor, reading_key, state = self._part_record(
                _cat.BY_ID[m.type_id].produces)
            did = self.board_device_id(slug, m.name)
            answers = (descriptions or {}).get(m.name)
            if isinstance(answers, str):
                answers = {"description": answers}
            elif not isinstance(answers, dict):
                answers = {}
            description = answers.get("description") or \
                self.plain_description(m.type_id, m.name, board_name)
            # THE FORM'S ANSWERS OVER THE CATALOGUE'S DEFAULTS, where it
            # gave any (2026-09-03): the spoken name, the starting state
            # of an actuator, and a level's range/step/unit. The catalogue
            # still decides the KIND -- a relay cannot be talked into
            # being a dimmer here.
            if isinstance(answers.get("state"), dict) and not sensor:
                given = answers["state"]
                if "on" in given:
                    state["on"] = bool(given["on"])
                if meta["state_key"] == "level" and "level" in given:
                    try:
                        state["level"] = float(given["level"])
                    except (TypeError, ValueError):
                        pass
            # THE CATALOGUE'S UNIT WHEN THE FORM GAVE NONE (2026-09-14).
            # `Produces.as_meta()` returns `"unit": UNIT_OF.get(key, "")` for
            # every non-binary part, so the catalogue DOES know a DHT reads
            # degrees -- and this line kept only what somebody typed, while
            # `refit_board_device` (the rewire path, below) writes
            # `meta["unit"]` unconditionally.
            #
            # So one device carried a different record depending on which
            # path last touched it; and since describe_reading reads
            # `state["unit"]`, the spoken answer gained its degrees only
            # after an unrelated rewire.
            if answers.get("unit"):
                state["unit"] = str(answers["unit"])

            nums = {k: meta[k] for k in ("step", "range_min", "range_max")}
            if meta["state_key"] == "level":
                for k in nums:
                    try:
                        nums[k] = float(answers[k]) if k in answers else nums[k]
                    except (TypeError, ValueError):
                        pass
            ok = self.add(
                did,
                name=str(answers.get("name") or m.name),
                room=str(answers.get("room") or board_name),
                description=description,
                state=state,
                readable_only=sensor,
                state_key=meta["state_key"],
                secure=bool(answers.get("secure", False)),
                show_on_home=answers.get("show_on_home"),
                transport="mqtt",
                ha_entity_id=m.entity_id,
                reading_key=reading_key,
                **nums,
            )
            (created if ok else skipped).append(did)
        return created, skipped

    def refit_board_device(self, did: str, type_id: str) -> bool:
        """Re-fit a registered board device to the part its board NOW names.

        A rewire can keep a name and change the part behind it: "lamp" was
        a relay and is now a dimmer, "gate" was a relay and is now a
        shutter, "meter" was a DHT and is now a PZEM. register_board wrote
        the catalogue's envelope for the OLD part into the record --
        state_key, readable_only, reading_key, range and step -- and the
        re-link after a rewire kept every field of the record, so the
        record went on saying "on/off, nothing to dim" about a dimmer and
        "a temperature from -40 to 85" about a power meter. The edit form
        then popped state_key and readable_only ("the part decides what
        the device is"), so nothing a person did could put it right short
        of deleting the device (2026-09-10).

        This is the one place that follows the part. WHAT KIND OF THING IT
        IS comes from the catalogue; WHAT IT IS CALLED stays the person's:
        name, room, description, secure, show_on_home, ha_entity_id and
        the state words are untouched. Only when the kind of thing really
        differs -- a different state key, a reading versus an actuator, or
        a different reading -- is anything rewritten; a part swapped for
        another of the same kind (relay for relay, on new pins) keeps the
        range and step a person may have edited. Returns True when the
        record changed.
        """
        did = str(did)
        dev = _cat.BY_ID.get(str(type_id or ""))
        if dev is None:
            return False
        meta, sensor, reading_key, state = self._part_record(dev.produces)
        with self._writing():
            d = self.devices.get(did)
            if d is None:
                return False
            same = (d.get("state_key") == meta["state_key"]
                    and bool(d.get("readable_only")) == sensor
                    and (d.get("reading_key") or None) == reading_key)
            if same:
                return False
            if not sensor:
                # An actuator keeps its switch, and what it was last
                # switched to: a relay that became a dimmer is still on.
                state["on"] = bool(d["state"].get("on", False))
            d["state"] = state
            d["state_key"] = meta["state_key"]
            d["readable_only"] = sensor
            d["reading_key"] = reading_key
            d["step"] = meta["step"]
            d["range_min"] = meta["range_min"]
            d["range_max"] = meta["range_max"]
            # The numbers are the new part's now, whoever chose the old
            # ones; the description's tag is left as it was.
            prov = d.get("provenance") if isinstance(d.get("provenance"), dict) else {}
            prov = {k: v for k, v in prov.items()
                    if k not in ("kind", "min", "max", "step", "unit")}
            prov["kind"] = "part"
            if meta["state_key"] == "level":
                prov.update(min="part", max="part", step="part")
                if meta.get("unit"):
                    prov["unit"] = "part"
            d["provenance"] = prov
            self._save()
        return True

    def backfill_readable_only(
            self, boards: Iterable[Tuple[str, List[Dict[str, Any]]]]) -> int:
        """One-time repair of board records written before `readable_only`
        existed. `boards` is (slug, items) pairs, items shaped exactly as a
        board record's list ({"type_id", "name", ...}).

        Such a record has no flag and, with no HA entity, falls to the legacy
        "no 'on' key" test -- so an existing dimmer, cover or RGB light kept
        being refused by voice after the upgrade. For every device on a board
        whose record has no boolean flag: set it from the catalogue (as
        register_board does), and seed "on": False on an actuator whose state
        lacks it (its level is kept). Saves once; returns how many records
        changed. Idempotent -- a second call changes nothing and returns 0.
        Devices not on any board, and devices with an HA entity (whose
        domain already answers), are untouched.
        """
        changed = 0
        with self._writing():
            for slug, items in boards:
                for item in items or ():
                    if not isinstance(item, dict):
                        continue
                    dev = _cat.BY_ID.get(str(item.get("type_id", "")))
                    if dev is None:
                        continue
                    did = self.board_device_id(str(slug), str(item.get("name") or ""))
                    record = self.devices.get(did)
                    if record is None:
                        continue
                    _, sensor, reading_key, _ = self._part_record(dev.produces)
                    touched = False
                    if not isinstance(record.get("readable_only"), bool) \
                            and not record.get("ha_entity_id"):
                        record["readable_only"] = sensor
                        if not sensor:
                            record["state"].setdefault("on", False)
                        touched = True
                    # THE READING'S NAME, for records written before the
                    # field existed: without it a meter's tile was a
                    # permanent dash while "power" sat one row down.
                    if "reading_key" not in record and reading_key:
                        record["reading_key"] = reading_key
                        touched = True
                    if touched:
                        changed += 1
            if changed:
                self._save()
        return changed

    def devices_on_board(self, slug: str) -> List[str]:
        """Every device registered from one board.

        The id carries the board: `kitchen.kettle`. That is deliberate and
        it is what makes retiring a board a single, honest operation instead
        of a hunt through entity ids that may since have been renumbered.
        """
        prefix = f"{slug}."
        with self._lock:
            self._reload_if_changed()
            return sorted(d for d in self.devices if d.startswith(prefix))

    # `device_for_entity` USED TO SIT HERE and was removed 2026-09-14.
    # Referenced nowhere in the tree, and its docstring served "the HA event
    # ear" -- which `refresh_ha_states` forty lines down states plainly does
    # not exist: "HOME ASSISTANT PUSHES NOTHING TO THIS HUB. There is no
    # event ear, no websocket subscription, no poller." Two methods in one
    # file describing opposite architectures. The polling one is the real
    # one, so the scaffolding for the imaginary one is gone; if an event ear
    # is ever built, an entity->device lookup is four lines.

    def update(self, did: str, **kwargs) -> bool:
        did = str(did)
        with self._writing():
            if not did or did not in self.devices:
                return False
            d = self.devices[did]
            applied = 0
            for k, v in kwargs.items():
                if k == "name":
                    d["name"] = str(v or "").lower()
                elif k == "room":
                    d["room"] = str(v or "").lower()
                elif k == "description":
                    d["description"] = str(v or "")
                    d["embedding"] = self._embed_description(d["description"])
                elif k in ("state", "ir_codes", "state_words"):
                    d[k] = dict(v) if isinstance(v, dict) else {}
                elif k == "provenance":
                    d[k] = dict(v) if isinstance(v, dict) else None
                elif k in ("secure", "transport", "step", "range_min", "range_max",
                          "state_key", "ha_entity_id", "show_on_home",
                          "readable_only"):
                    d[k] = v
                else:
                    # Silently dropping an unrecognised field and then
                    # returning True told the caller a change had been saved
                    # that was never even considered.
                    event("DEVICE", f"update({did}): ignoring unknown field {k!r}")
                    continue
                applied += 1
            if not applied:
                return False
            self._sync_rooms()
            self._save()
        return True

    @staticmethod
    def _root(name: str) -> str:
        return (name or "").rstrip("0123456789_")

    @staticmethod
    def _is_numbered_sibling(spoken: str, dname: str) -> bool:
        """True if dname is `spoken` optionally followed by a separator and
        digits — a numbered sibling (ac -> ac2, light -> light_1) — but NOT a
        letter-continuation (ac -> accent). Lets a bare base name reach all its
        numbered instances without matching unrelated words that merely start
        with the same letters."""
        if not spoken or not dname:
            return False
        # A spoken name never carries an underscore: "bulb 2" IS "bulb_2".
        spoken, dname = spoken.replace("_", " "), dname.replace("_", " ")
        if dname == spoken:
            return True
        return bool(re.match(rf"^{re.escape(spoken)}[\s\-]?\d+$", dname))

    def resolve_literal(self, name: str, room: Optional[str] = None) -> Tuple[List[str], List[Tuple[str, str]]]:
        name = str(name or "").lower()
        if not name:
            return [], []
        room = str(room).lower() if room is not None else None
        with self._lock:
            self._reload_if_changed()
            in_scope, elsewhere = [], []
            plural = self._root(name[:-1]) if name.endswith('s') else None
            for did, d in self.devices.items():
                dname, droom = d.get("name", ""), d.get("room", "")
                droot = self._root(dname)
                # A multi-word name ("smart door") is invisible to exact/root
                # comparison against the whole string, since "smart door" never
                # equals "door". Check whole WORD TOKENS too — split on space and
                # underscore — so "door"/"doors" finds "smart door" the same way
                # "bulb"/"bulbs" already finds "bulb_2". This deliberately checks
                # whole tokens, not substrings: "padlock" is one fused word, so
                # "lock" does NOT match it, same principle as "ac" not matching
                # "accent" — only a real separate word counts.
                # ...and only the HEAD word of a multi-word name -- the last
                # one -- names the device on its own. English compound names
                # are right-headed: a "power meter" is a meter, a "card
                # reader" is a reader, a "smart door" is a door. The words in
                # front describe it and are not it: "power" is what the meter
                # measures and the verb in "power the outlet", which the old
                # any-token rule sent to the meter (paraphrase run, 2026-09-10,
                # "power the outlet" and "cut the power to the appliances"
                # both -> power meter). A modifier alone now falls through to
                # the semantic rung, which weighs the whole sentence.
                # THE HEAD IS THE LAST NON-NUMERIC TOKEN (2026-09-14).
                # The right-headed reasoning above is sound English and
                # wrong about this project's OWN naming convention: split
                # on underscore, the head token of "bulb_2" is "2". So the
                # bare numeral "2" matched the device, and since this rung
                # runs before the context rung and never hands back, a pure
                # back-reference went to the wrong device entirely:
                #
                #     registry: kitchen.bulb_2, kitchen.ac (just touched)
                #     "set it to 2"   -> execute on kitchen.bulb_2 (!)
                #                        level 2, and switched ON
                #     "set it to 22"  -> the AC, correctly -- only because
                #                        nothing is named "*_22"
                #
                # A trailing number is a SIBLING INDEX, not a name. It
                # already has its own road in `_is_numbered_sibling`
                # ("ac" -> "ac2"), which is the right one and is unchanged;
                # what a bare digit must not do is name a device by itself.
                dname_tokens = dname.replace('_', ' ').split()
                head = next((t for t in reversed(dname_tokens)
                             if not t.isdigit()), "")
                word_match = (name == head) or (bool(plural) and plural == head)
                # Match if: exact name, a numbered sibling of the spoken name
                # (ac -> ac2), a whole-word token within a multi-word name, or —
                # for a plural request — any device sharing the plural root.
                match = (self._is_numbered_sibling(name, dname)
                         or (plural and (dname == plural or droot == plural))
                         or word_match)
                if not match:
                    continue
                if room is None or droom == room:
                    in_scope.append(did)
                else:
                    elsewhere.append((did, droom))
            return in_scope, elsewhere

    def resolve_count(self, name: str, room: Optional[str] = None) -> Tuple[Action, Optional[Any]]:
        in_scope, elsewhere = self.resolve_literal(name, room)
        is_plural = str(name or "").lower().endswith("s")
        if len(in_scope) == 1:
            return Action.EXECUTE, in_scope
        if len(in_scope) >= 2:
            return (Action.EXECUTE, in_scope) if is_plural else (Action.PICK, in_scope)
        if elsewhere:
            return Action.ELSEWHERE, elsewhere
        return Action.NONE, None

    def refresh_ha_states(self) -> int:
        """Re-read every Home-Assistant-backed device in ONE round trip.

        HOME ASSISTANT PUSHES NOTHING TO THIS HUB. There is no event ear,
        no websocket subscription, no poller -- grep for subscribe_events,
        state_changed, event_stream, listen_states and the answer is
        nothing, anywhere. So an HA sensor's stored state was only ever as
        fresh as the last time a PERSON caused something to read it:
        opening Devices, asking by voice, a status query. The home screen's
        tiles were rebuilt every fifteen seconds from a dict that nothing
        on a timer ever changed -- the same number, redrawn, for hours.

        ONE `get_states`, NOT ONE `get_state` PER DEVICE. That distinction
        is the whole reason this method can exist at all: `live_reading_rows`
        is documented as never making an HA REST call, because it runs on a
        timer on the event loop and a blocking round trip per HA device per
        tick was a real fault (2026-09-03 -- "List devices" timing out, five
        seconds at a time, with HA down). This is one request for the entire
        house, run from a worker thread on its own slow cadence, and it
        writes into the cache those tiles already read. The rule it must not
        break is unchanged: nothing on the event loop ever waits on HA.

        Returns how many records were updated. 0 when HA is unreachable --
        and on that path nothing is stamped, so every tile correctly goes
        stale rather than freezing at its last value and claiming to be live.

        IN MEMORY ONLY, like get_state's own refresh: no _save() on a read
        path.
        """
        if not self.ha_client:
            return 0
        with self._lock:
            wanted = {did: str(d.get("ha_entity_id") or "")
                      for did, d in self.devices.items()
                      if d.get("ha_entity_id")
                      and d.get("transport") != "mqtt"}
        if not wanted:
            return 0
        try:
            states = self.ha_client.get_states()
        except Exception as e:                              # noqa: BLE001
            event("DEVICE", f"HA refresh failed ({e})")
            return 0
        if not states:
            return 0
        by_id = {str(s.get("entity_id") or ""): s
                 for s in states if isinstance(s, dict)}
        now = time.monotonic()
        changed = 0
        for did, entity in wanted.items():
            # An entity gone from HA (renamed, or its device removed) is NOT
            # stamped: a stale registration must read as stale, not as a
            # device that answered. Nor is one that is present but says
            # "unavailable" -- see _take_ha_answer.
            got = self._take_ha_answer(did, entity, by_id.get(entity), now)
            if got is not None and got[1]:
                changed += 1
        return changed

    def _take_ha_answer(self, did: str, entity_id: str, raw: Any, now: float
                        ) -> Optional[Tuple[Dict[str, Any], bool]]:
        """Store what Home Assistant said about one device: translated, as a
        COPY, and stamped as heard. Returns (translated, whether the stored
        state moved), or None -- nothing stored, nothing stamped -- when HA
        gave no answer. Shared by get_state and refresh_ha_states, whose
        rules here must agree."""
        # PRESENT IN /api/states IS NOT THE SAME AS ANSWERING
        # (2026-09-14). An entity whose integration has lost touch with
        # it is still LISTED, carrying state "unavailable" -- so `raw is
        # None` above never caught it, it was translated, written, and
        # stamped as having answered. is_fresh then said yes, and a dead
        # door contact drew on the home screen as "off, reporting".
        #
        # Asked here as well as in translate_ha_state because the two
        # answers have to agree: the value says "nothing known" and the
        # freshness has to say the same, or the tile finds a confident
        # False sitting under a fresh stamp and draws it.
        if not isinstance(raw, dict) or \
                str(raw.get("state", "")).strip().lower() in HA_NO_ANSWER_STATES:
            return None
        translated = self.translate_ha_state(entity_id, raw)
        with self._lock:
            d = self.devices.get(did)
            if d is None:
                return translated, False
            if translated.get("unit"):
                self._ha_units[did] = str(translated["unit"])
            else:
                self._ha_units.pop(did, None)
            # The unit registration wrote ("C" for a thermostat) survives:
            # HA's state carries one only for a sensor, and replacing the
            # record's state wholesale dropped it from the record for good.
            old_unit = (d.get("state") or {}).get("unit")
            if old_unit and "unit" not in translated:
                translated["unit"] = old_unit
            # STAMPED EVERY TIME, COUNTED ONLY WHEN IT MOVED. The
            # stamp is about whether the device ANSWERED -- that is
            # what freshness means, and a sensor sitting at 21.5 all
            # evening is answering perfectly well. The count is what
            # the caller uses to decide whether to wake the phones,
            # and a house where nothing changed has nothing to say.
            # Same rule the MQTT side follows in _observe.
            #
            # A COPY: handing the record's own dict back to the caller let
            # set_state() mutate the "before" the executor was holding, so
            # every HA device reported "already in that state" and every
            # toggle "had no effect" (demo against a live HA, 2026-09-06).
            moved = d.get("state") != translated
            d["state"] = dict(translated)
            self._ha_seen[did] = now
        return translated, moved

    def ha_unit(self, did: str) -> str:
        """The unit Home Assistant last attached to this device's reading
        ("W", "kW", "A"...), or "" -- never one typed on a form."""
        return self._ha_units.get(str(did), "")

    def ha_last_seen(self, did: str) -> Optional[float]:
        """Seconds since this HA device last actually answered, or None if
        it never has in this run of the hub."""
        seen = self._ha_seen.get(str(did))
        return None if seen is None else max(0.0, time.monotonic() - seen)

    def is_fresh(self, did: str, device: Optional[Dict[str, Any]] = None) -> bool:
        """Is this device's stored state something it actually said recently?

        Only Home-Assistant-backed devices can answer no here. A board is
        judged by the engine's MQTT cache, which the caller checks; a
        typed-in device has no source to be stale against and its stored
        state is the only truth there is.
        """
        d = device if device is not None else (self.devices.get(str(did)) or {})
        if not d.get("ha_entity_id") or d.get("transport") == "mqtt":
            return True
        age = self.ha_last_seen(did)
        return age is not None and age <= HA_STALE_SECONDS

    def room_devices(self, room: Optional[str]) -> Tuple[Optional[List[str]], Optional[List[str]]]:
        """(device ids in that room, None), or (None, the known rooms)."""
        with self._lock:
            self._reload_if_changed()
            room = str(room).lower() if room is not None else None
            if room not in self.rooms:
                return None, sorted(self.rooms)
            return [did for did, d in self.devices.items() if d.get("room", "") == room], None

    def _backfill_embeddings(self) -> None:
        """Embed any device that has a description but no stored vector (e.g.
        devices registered before embeddings existed, or added while the model
        wasn't loaded). Callers hold self._lock."""
        if not self.embed_fn or not any(d.get("description") and not d.get("embedding")
                                        for d in self.devices.values()):
            return
        with self._writing():
            changed = False
            for d in self.devices.values():
                if d.get("description") and not d.get("embedding"):
                    emb = self._embed_description(d["description"])
                    if emb is not None:
                        d["embedding"] = emb
                        changed = True
            if changed:
                self._save()

    def _mpnet_scores(self, query: str, allowed: Optional[Set[str]] = None
                      ) -> Optional[List[Tuple[str, float]]]:
        """(device_id, cosine) for every stored description vector (within
        `allowed`), best first. None when no model is loaded or it failed --
        the caller then falls back to TF-IDF. Callers hold self._lock.

        The ONE MPNet scorer: resolve_semantic and resolve_semantic_scored
        each carried a copy of this loop."""
        if not self.embed_fn:
            return None
        self._backfill_embeddings()
        try:
            import numpy as np
            q = np.asarray(self.embed_fn(query), dtype="float32")
            qn = float(np.linalg.norm(q))
            if qn < 1e-9:
                return []
            q = q / qn
            # both L2-normalised -> cosine
            scored = [(did, float(np.dot(q, np.asarray(d["embedding"], dtype="float32"))))
                      for did, d in self.devices.items()
                      if d.get("embedding") and (allowed is None or did in allowed)]
        except Exception as e:
            event("DEVICE", f"MPNet semantic match failed ({e}); "
                  f"falling back to TF-IDF")
            return None
        scored.sort(key=lambda p: p[1], reverse=True)
        if SEMANTIC_DEBUG:
            top = ", ".join(f"{did}={s:.2f}" for did, s in scored[:5])
            print(f"  [semantic] {query!r} top: {top}")
        return scored

    def resolve_semantic_scored(self, query: str) -> List[Tuple[str, float]]:
        """Like resolve_semantic but returns (device_id, cosine_score) pairs,
        best first, with a LOW floor so callers can band the results themselves
        (e.g. keep everything within 0.12 of the top). MPNet path only; if no
        model is loaded, returns the TF-IDF order with placeholder scores."""
        query = str(query or "")
        if not query:
            return []
        _asked = time.perf_counter()
        with self._lock:
            times_add("registry_wait", time.perf_counter() - _asked)
            self._reload_if_changed()
            scored = self._mpnet_scores(query)
            if scored is not None:
                # Very low floor so the resolver's banding can see close
                # runners-up that sit just under the match threshold. The
                # resolver bands around the top score, so genuine noise far
                # below is excluded there, not here.
                return [(did, s) for did, s in scored if s >= 0.05]
            # No model: reuse the ranked TF-IDF order, scores unknown -> descending
            ranked = self.embedder.score_devices(query, self.devices, SEMANTIC_THRESHOLD_TFIDF)
            n = len(ranked)
            return [(did, 1.0 - i / max(n, 1)) for i, did in enumerate(ranked)]

    def resolve_semantic(self, query: str, restrict: List[str]) -> List[str]:
        """Rank devices by semantic similarity to `query`, best first.
        `restrict`: only consider these device ids (e.g. the secure devices).
        There is no threshold, so a subset is always ranked rather than
        filtered to empty — the caller (securement) wants an order, not a
        cutoff."""
        query = str(query or "")
        if not query:
            return []
        allowed = set(restrict)
        with self._lock:
            self._reload_if_changed()
            # Preferred path: real MPNet semantic match against stored vectors.
            scored = self._mpnet_scores(query, allowed)
            if scored is not None:
                ranked = [did for did, _s in scored]
            else:
                # Fallback: TF-IDF word overlap (no model loaded).
                ranked = self.embedder.score_devices(
                    query, {did: d for did, d in self.devices.items()
                            if did in allowed}, -1.0)
        # Include any restricted devices that had no embedding, so a
        # subset is never silently dropped.
        return ranked + [d for d in restrict if d not in ranked]

    def get_state(self, did: str) -> Dict[str, Any]:
        did = str(did)
        with self._lock:
            device = self.devices.get(did, {})
            cached = device.get("state")
            cached = dict(cached) if isinstance(cached, dict) else {}
            entity_id = device.get("ha_entity_id")
            board_backed = device.get("transport") == "mqtt"

        # THE BOARD'S OWN WORD FIRST. A board publishes its relays and its
        # sensor blocks over MQTT; the automation listener already holds that
        # subscription open and caches every value. That is a live read with
        # no Home Assistant in it, and for a board this hub flashed it is the
        # source of truth -- HA would only be repeating it back, one REST
        # round trip later.
        #
        # MERGED ONTO the cache, not substituted for it: a board reports
        # `on` and its readings, never the descriptive fields registration
        # wrote (colour_modes, unit, envelope). Replacing would drop them.
        if board_backed and self.state_reader is not None:
            try:
                live = self.state_reader(did)
            except Exception:                            # noqa: BLE001
                live = None
            if live:
                cached.update(live)
                with self._lock:
                    if did in self.devices:
                        self.devices[did]["state"] = dict(cached)
                return cached

        # A BOARD IS NEVER READ THROUGH HOME ASSISTANT. HA is for
        # integration devices (her rule, 2026-09-02); a board this hub
        # flashed speaks on the hub's own MQTT, and when the engine has not
        # heard it yet the honest answer is the stored state marked "not
        # reporting" -- not a 5-second REST timeout per device with HA
        # down, which is what "List devices" had turned into (log,
        # 2026-09-03: read timeouts for switch.kitchen_bulb_2 and the
        # meter's sensor, one after another).
        if board_backed:
            return cached

        if entity_id and self.ha_client:
            # AN ENTITY THAT ANSWERED "unavailable" DID NOT ANSWER. Same
            # rule as refresh_ha_states: HA returns a perfectly well-formed
            # record carrying that word, so `is not None` is satisfied and
            # the stamp would certify a device nobody has heard from
            # as live. Falling through to `cached` keeps the last real
            # reading and leaves the freshness where it was, which is what
            # "not reporting" on the tile is built from.
            #
            # Otherwise: opportunistically refresh our cache on every
            # successful live read -- this is what the read-only fallback
            # snapshot actually is: a side effect of normal operation, not a
            # separate sync process.
            #
            # IN MEMORY ONLY, deliberately: _save() on a read path would
            # put a disk write behind every status query. The consequence
            # is that after a restart the "last-known state" is whatever
            # set_state last persisted, not what was last read from HA.
            # That is the right trade, but it is not what the surrounding
            # comment implied.
            got = self._take_ha_answer(did, entity_id,
                                       self.ha_client.get_state(entity_id),
                                       time.monotonic())
            if got is not None:
                return got[0]
            # TWO different failures land here and only one is a network
            # problem. get_state returns None both when HA is unreachable
            # AND when HA answered "404 -- no such entity", and this line
            # used to blame the network for both. The second case is a
            # STALE REGISTRATION: the entity was renamed or its device
            # removed in HA, and no amount of waiting fixes it -- the
            # device needs re-registering. `--doctor` now names these.
            print(f"[DeviceManager] no live state for {entity_id} -- HA is "
                  f"unreachable, or that entity no longer exists in HA "
                  f"(renamed/removed? re-register the device). Using "
                  f"last-known state, which may be stale.")
        return cached

    def translate_ha_state(self, entity_id: str, ha_state: Dict[str, Any]) -> Dict[str, Any]:
        """HA's raw state shape ({'state': 'on'/'locked'/..., 'attributes': {...}})
        translated into our own shape ({'on': bool, <numeric fields>: ...}).
        Deliberately simple: handles the common on/off-ish states correctly;
        domain-specific nuance (fan speed strings, cover position, vacuum
        mode) is a real, separate refinement, not silently guessed at here."""
        domain = _domain(entity_id)
        raw_state = ha_state.get("state", "")
        attrs = ha_state.get("attributes")
        if not isinstance(attrs, dict):     # absent, null, or not an object:
            attrs = {}                      # `.get` on it raised out of get_state

        # "I HAVE LOST TOUCH WITH IT" IS NOT A STATE THE DEVICE IS IN
        # (2026-09-14). `unavailable` and `unknown` appeared NOWHERE in this
        # file, the executor, ha_client or ha_flows -- so they fell through
        # the membership test below and came out `{"on": False}`. A door
        # contact whose battery died was then written as CLOSED, and
        # refresh_ha_states stamped it as having answered, so the home tile
        # drew "off, reporting" for an open front door. main.py's own tile
        # code says this must not happen: "an HA contact that stopped
        # answering must not draw as a door that is definitely shut."
        #
        # An EMPTY DICT is the answer, and it is the same shape a button
        # already returns: no `on`, no `level`, nothing to read and nothing
        # to claim. `reporting` downstream is driven by freshness, which
        # refresh_ha_states no longer stamps for these states either -- the
        # two halves have to agree or the tile lies again.
        if str(raw_state).strip().lower() in HA_NO_ANSWER_STATES:
            return {}

        # A SENSOR IS NOT A SWITCH, and must not be run through the on/off
        # test below. Its state IS the measurement -- "241.3", "14.7" -- never
        # the word "on", so that test returns False for every reading and a
        # power meter registers as permanently off. It also has no service to
        # call: there is no sensor.turn_on in Home Assistant, because nothing
        # exists at the other end to receive a value. HA RECEIVES a sensor's
        # state; it cannot set it.
        #
        # So the shape is deliberately different: a reading and NO "on" key at
        # all. is_readable_only() keys off exactly that absence, which is why
        # it is derived rather than a stored flag that could disagree.
        # A BUTTON has no state to translate. HA reports its `state` as the
        # timestamp of the last press -- useful for automations, meaningless
        # as a device state, and actively misleading here: it is neither an
        # on/off nor a level. An empty dict says exactly that, and is what
        # is_readable_only correctly reports False for (there is nothing to
        # read AND nothing to switch -- only something to press).
        if domain in self.HA_PRESS_SERVICES:
            return {}

        # A CAMERA'S STATE IS A WORD (idle / streaming / recording), never
        # a switch; run through the on/off test below it came out as
        # {"on": False} and looked like something voice could turn on.
        if domain == "camera":
            return {"text": str(raw_state or "unknown")}

        if domain == "sensor":
            reading: Dict[str, Any] = {}
            value = _as_number(raw_state)
            if value is not None:
                reading["level"] = value
            else:
                # Non-numeric sensors are real (a text sensor, an enum). Keep
                # the raw string so a query can still report it, rather than
                # discarding a reading for not being a float.
                reading["text"] = raw_state
            unit = attrs.get("unit_of_measurement")
            if isinstance(unit, str) and unit.strip():
                reading["unit"] = unit.strip()
            return reading

        # IS IT DOING SOMETHING? Asked as a denylist of the words that mean
        # OFF, not an allowlist of the words that mean on -- see
        # HA_OFF_STATES for the three domains the hand-typed allowlist got
        # wrong and why a wrong False here SUPPRESSED THE COMMAND rather
        # than merely mislabelling it.
        word = str(raw_state).strip().lower()
        inverts = HA_ON_STATES_BY_DOMAIN.get(domain)
        if inverts is not None:
            on = word in inverts
        else:
            on = word not in HA_OFF_STATES
        result: Dict[str, Any] = {"on": on}
        # Pull through the one or two numeric attributes that actually matter
        # for voice value-setting, per domain -- not every attribute HA sends.
        #
        # UNITS MATTER HERE. Every domain used to be copied into "level" raw,
        # but HA does not speak one scale: light.brightness is 0-255 while
        # fan.percentage is 0-100. A device's declared range_min/range_max
        # default to 0-100, so a light at full brightness reported level=255 --
        # outside its own range. Every delta then computed from a baseline the
        # arithmetic had no way to interpret, and "dim it a bit" moved by a
        # fraction of the wrong scale. Brightness is normalised on the way in.
        if domain == "climate" and "temperature" in attrs:
            result["level"] = attrs["temperature"]      # degrees; range is the device's
        # Carried through so is_colourable can answer from the device record
        # rather than making a fresh HA call every time someone says a colour.
        if domain == "light":
            modes = attrs.get("supported_color_modes")
            if isinstance(modes, (list, tuple)):
                result["colour_modes"] = list(modes)
            rgb = attrs.get("rgb_color")
            if isinstance(rgb, (list, tuple)) and len(rgb) == 3:
                result["colour"] = list(rgb)

        if domain == "light" and attrs.get("brightness") is not None:
            raw_brightness = _as_number(attrs["brightness"])
            if raw_brightness is not None:
                result["level"] = round(raw_brightness * 100.0 / 255.0, 1)
        elif domain == "fan" and "percentage" in attrs:
            result["level"] = attrs["percentage"]       # already 0-100
        elif domain in ("cover", "valve") and "current_position" in attrs:
            # A position, 0 closed to 100 open -- so "half open" has a
            # number to move from and the home card can show it (2026-09-10).
            result["level"] = attrs["current_position"]
        elif domain == "water_heater" and "temperature" in attrs:
            # Same shape as climate, and it was already in HA_ON_OFF_SERVICES
            # -- so it could be switched but never read a value, and a command
            # like "set it to 50" had nothing to move from.
            result["level"] = attrs["temperature"]
        return result

    @classmethod
    def is_pressable(cls, device: Dict[str, Any]) -> bool:
        """True for a device whose only action is a momentary press.

        DERIVED from the HA domain, like is_readable_only is derived from the
        state shape -- no stored capability flag that could disagree with the
        entity it describes.

        A press is genuinely a third kind of action. It is not on/off: there is
        no "off" for a TV's PLAY button, and asking one to turn off is not a
        request the hardware can even express. It is not a value either. And an
        IR blaster is one-way, so unlike every other actionable device there is
        nothing to read back -- you can press play, you cannot ask whether the
        TV is playing.
        """
        return _domain(device.get("ha_entity_id")) in cls.HA_PRESS_SERVICES

    @classmethod
    def is_readable_only(cls, device: Dict[str, Any]) -> bool:
        """True for a device that can be READ but never acted on.

        From the KIND of thing it is: the HA entity's domain, or the
        catalogue domain register_board recorded as `readable_only`. The
        old rule -- "a state with no 'on' key is a sensor" -- was also true
        of every board dimmer and cover ({"level": 0.0}), which voice then
        refused to touch. That shape test survives only as the fallback for
        records written before the flag existed.
        """
        domain = _domain(device.get("ha_entity_id"))
        if domain:
            return domain in cls.HA_READ_ONLY_DOMAINS
        flag = device.get("readable_only")
        if isinstance(flag, bool):
            return flag
        state = device.get("state")
        if not isinstance(state, dict) or not state:
            return False
        return "on" not in state

    def ha_value_profile(self, entity_id: str,
                         ha_state: Optional[Dict[str, Any]] = None
                         ) -> Optional[Dict[str, Any]]:
        """The numeric envelope for an HA-backed device: range, step, unit.

        Read from the entity's OWN attributes wherever it publishes them, so a
        thermostat registers as 7-35 degrees rather than as the generic 0-100
        percent every device used to get. Returns None for domains with no
        gradable value (a lock, a plain switch, a sensor), which is not a
        failure -- those devices genuinely have no envelope.

        A SENSOR falls through here correctly and for the right reason: it has
        no settable range because nothing sets it. Reporting the generic
        0-100 default would tell registration it was a dimmer.

        `ha_state` may be passed in by a caller that already fetched it; the
        registration flow has it in hand and should not pay for a second round
        trip.
        """
        profile = self.HA_VALUE_PROFILES.get(_domain(entity_id))
        if profile is None:
            return None

        if ha_state is None and self.ha_client is not None:
            ha_state = self.ha_client.get_state(entity_id)
        attrs = (ha_state or {}).get("attributes") or {}
        if not isinstance(attrs, dict):
            attrs = {}

        low, high, step = profile["fallback"]

        def published(name: Optional[str], default: float) -> float:
            if not name:
                return default
            value = _as_number(attrs.get(name))
            return default if value is None else value

        low = published(profile["min_attr"], low)
        high = published(profile["max_attr"], high)
        step = published(profile["step_attr"], step)

        if high < low:                     # an entity publishing nonsense
            low, high = high, low
        if step <= 0 or step > (high - low or step):
            step = profile["fallback"][2]

        # THE UNIT THE DOCSTRING PROMISED (2026-09-14). It says "range,
        # step, unit" and three keys came back, none of them a unit -- so
        # main.py's `profile.get("unit")` was always empty and fell through
        # to the translated state, which for a climate entity has no unit
        # either (translate_ha_state sets one only on the sensor branch).
        # A thermostat registered through the HA picker therefore opened
        # its form with an empty unit box while HA_VALUE_PROFILES knew
        # perfectly well it was degrees.
        #
        # Taken from the entity FIRST, because HA publishes the real one
        # and a house is not always in Celsius; the profile's word is the
        # fallback.
        unit = attrs.get("unit_of_measurement") \
            or attrs.get("temperature_unit") or profile.get("unit") or ""
        result = {"range_min": float(low), "range_max": float(high),
                  "step": float(step), "state_key": "level"}
        if str(unit).strip():
            result["unit"] = str(unit).strip()
        return result

    def set_state(self, did: str, *,
                  _driver: Optional[Callable[[str, Dict[str, Any]], bool]] = None,
                  **kwargs) -> bool:
        """Apply a state change, pushing it to Home Assistant when the device
        is HA-backed. Returns False if the change did NOT take effect.

        `_driver`: a board driver to use INSTEAD of self.mqtt_driver for this
        one call (the automation engine publishes on its own open connection).

        The return value is load-bearing: executor.py reports command success
        from it, so claiming True for a write that never reached the device is
        the difference between "the door is locked" and "we asked nicely"."""
        did = str(did)
        if not did:
            return False
        with self._lock:
            device = self.devices.get(did)
            if device is None:
                return False
            entity_id = device.get("ha_entity_id")

        # READ-ONLY IS READ-ONLY WITH OR WITHOUT A CLIENT.
        #
        # This check sits ABOVE the ha_client test on purpose. Without it, a
        # hub with no Home Assistant attached would skip the push entirely,
        # update its own copy of a thermostat reading it invented, and
        # return True -- so "set the doorway sensor to 30" would be reported
        # as done. The device being unreachable is a reason to fail, not a
        # reason to succeed locally.
        if _domain(entity_id) in self.HA_READ_ONLY_DOMAINS:
            event("DEVICE", f"{entity_id}: can only be READ. Nothing sets a "
                  f"sensor's state.")
            return False

        # OUR OWN WIRE FIRST. ALWAYS. (2026-09-01)
        #
        # This was Home Assistant first whenever an entity was attached,
        # with the direct drive as the fallback "when HA refuses". The flaw
        # is that HA does not have to refuse to fail: it can ACCEPT the
        # call, return success, and drive nothing -- a stale entity, an
        # integration that has lost the device, an entity that was never
        # the right one. The hub then reported done, wrote the new state,
        # and the bulb stayed on.
        #
        # Observed exactly that way: "Turn off the bulb" -> ok=True, and the
        # bulb did not move. The meter had been auto-attached to
        # `sensor.kitchen_energy_apparentpower`, which is the same class of
        # mistake one layer along.
        #
        # A board this hub flashed is reachable over a wire this hub owns:
        # local, instant, and answerable. Home Assistant is a third party
        # that may or may not still be pointing at the right thing. So the
        # direct drive goes first and HA is the fallback -- which is also
        # what makes an HA-only device (a TV, someone else's plug) still
        # work: mqtt_driver refuses a device it has no board for, and the
        # push happens then.
        #
        # A device with no road at all still fails honestly: "we asked
        # nicely" must never report as done.
        board_backed = device.get("transport") == "mqtt"
        drove = False
        driver = _driver or self.mqtt_driver
        if board_backed and driver is not None:
            drove = bool(driver(did, dict(kwargs)))
        # A BOARD OF OURS THAT IS OFFLINE IS NOT HANDED TO HOME ASSISTANT.
        # The fallback is for a device this hub has no board for. Her
        # kitchen board was off the network (2026-10-05): the lamp, with no
        # entity, failed honestly; the bulb, with one, was pushed to Home
        # Assistant, which ACCEPTED the call for a board it cannot reach
        # either -- and the hub said "already on" from a stale record.
        offline = (not drove and board_backed and driver is not None
                   and self._board_is_offline(did))
        if not drove:
            if entity_id and self.ha_client and not offline:
                if not self._push_to_ha(entity_id, kwargs):
                    return False
                event("DEVICE", f"{did}: not ours to drive directly -- pushed "
                      f"to Home Assistant.")
            elif board_backed and driver is not None:
                # THE ROAD EXISTS AND WAS TAKEN (2026-09-11). This used to
                # fall into the line below and report "no road to this
                # device -- no board of ours", about a board the hub had
                # just connected to the broker for, asked after, and found
                # offline. Two different failures wearing one sentence: a
                # device nothing can reach, and a board that is simply not
                # answering right now. The second is the common one and the
                # only one a person can fix, and it read as the first.
                #
                # The driver logged the actual verdict (offline / refused /
                # no-broker...) and the shell keeps it for the sentence the
                # person gets; this line says which of the two happened.
                event("DEVICE", f"{did}: its board did not take the command -- "
                      f"see the MQTT line above for why"
                      + ("" if not entity_id
                         else " (not reached: not handed to Home Assistant, "
                              "which would only reach it through the board)"
                         if offline
                         else " (and no Home Assistant client is attached "
                              "to try instead)") + ".")
                return False
            else:
                event("DEVICE", f"{did}: no road to this device -- no board of "
                      f"ours, and no Home Assistant entity.")
                return False
        return self._record_change(did, kwargs)

    def _board_is_offline(self, did: str) -> bool:
        """Did the driver just fail to REACH this device's board -- a board
        this hub has on file? Off the broker, the broker itself not
        answering or refusing, or the send timing out (her hub, 2026-10-05:
        `error:TimeoutError`, and the bulb was handed to Home Assistant and
        reported switched). NOT the verdicts that mean there is no board of
        ours to speak to at all (no record, no wiring, no topic, not an
        output): those are what the Home Assistant road is for."""
        why = self.drive_problem
        try:
            said = str(why(did)) if why else ""
        except Exception:                                   # noqa: BLE001
            return False
        return (said in ("offline", "no-broker", "refused")
                or said.startswith("error:"))

    def _record_change(self, did: str, changes: Dict[str, Any]) -> bool:
        """Write a change that reached the device into its record, and save.
        False if the device was removed meanwhile (the change is dropped)."""
        with self._writing():
            # Re-fetch under the lock rather than reusing the dict captured
            # above. HA calls happen with the lock RELEASED, so a remove() in
            # between left the old code updating an orphaned dict and then
            # saving a devices map that never contained it -- the write
            # vanished and True was returned anyway.
            device = self.devices.get(did)
            if device is None:
                event("DEVICE", f"{did}: device removed mid-write, change dropped")
                return False
            device["state"].update(changes)
            self._changed_by[did] = ACTOR.get()
            self._save()
        return True

    @staticmethod
    def is_colourable(device: Dict[str, Any]) -> bool:
        """True if this light can take an RGB colour.

        DERIVED from what the entity itself publishes, like every other
        capability here. HA lists `supported_color_modes` on a light, and a
        bulb that only dims reports ["brightness"] or ["onoff"] -- so sending
        it rgb_color would be rejected, or worse, silently ignored.
        """
        state = device.get("state")
        if not isinstance(state, dict):
            return False
        modes = state.get("colour_modes")
        if not isinstance(modes, (list, tuple)):
            return False
        # The modes that accept an RGB triple. "color_temp" is deliberately
        # absent: it takes kelvin, a different field on a different axis.
        return bool({"rgb", "rgbw", "rgbww", "hs", "xy"} & set(modes))

    def set_colour(self, did: str, rgb: Tuple[int, int, int]) -> bool:
        """Set a light's colour. True if it reached Home Assistant.

        Its OWN method, not a set_state key, for the same reason press() is:
        rgb_color is a different service FIELD from brightness_pct, and
        HA_VALUE_SERVICES maps one field per domain. Squeezing colour through
        there would mean that table's one-field assumption stops holding.
        """
        did = str(did)
        with self._lock:
            device = self.devices.get(did)
            if device is None:
                return False
            entity_id = device.get("ha_entity_id")
        change = {"colour": list(rgb), "on": True}

        # A BOARD LIGHT'S COLOUR IS THREE PWM CHANNELS, and the hub can set
        # them itself. Tried first for a board device; everything below is
        # unchanged and still serves every entity this hub did not flash.
        if device.get("transport") == "mqtt" and self.mqtt_driver is not None \
                and self.mqtt_driver(did, {"colour": list(rgb)}):
            self._record_change(did, change)
            return True

        if device.get("transport") == "mqtt" and self._board_is_offline(did):
            event("DEVICE", f"set_colour({did}): its board was not reached -- "
                  f"not handed to Home Assistant.")
            return False
        if not entity_id or self.ha_client is None:
            event("DEVICE", f"set_colour({did}): no HA entity attached, and no "
                  f"board colour path took it.")
            return False
        # turn_on with rgb_color: HA switches the light on and sets the colour
        # in one call, which is what a person means by "make it blue" when the
        # light is off. _push_to_ha makes exactly that call (and refuses
        # anything that is not a light).
        if not self._push_to_ha(entity_id, change):
            return False
        self._record_change(did, change)
        return True

    def press(self, did: str) -> bool:
        """Fire a momentary action. True if it reached Home Assistant.

        A SEPARATE entry point from set_state, deliberately. set_state's whole
        contract is "read the state, apply a change, report whether anything
        moved" -- and a press has no state either side of it, so every part of
        that contract would have to be faked. `changed` would be permanently
        False, which _settle reads as "already in that state", which would
        report every successful press as a no-op.

        There is also nothing local to update. A button holds no state; the
        entire effect happens at the far end of an IR beam.
        """
        did = str(did)
        with self._lock:
            device = self.devices.get(did)
            if device is None:
                return False
            entity_id = device.get("ha_entity_id")

        if not entity_id or self.ha_client is None:
            event("DEVICE", f"press({did}): no HA entity attached -- a press has "
                  f"no local effect, so there is nothing to fall back to.")
            return False

        domain = _domain(entity_id)
        service = self.HA_PRESS_SERVICES.get(domain)
        if service is None:
            event("DEVICE", f"{entity_id}: '{domain}' has no press mapping.")
            return False
        if not self.ha_client.call_service(domain, service, entity_id):
            event("DEVICE", f"{entity_id}: HA rejected {domain}.{service}")
            return False
        return True

    def _push_to_ha(self, entity_id: str, changes: Dict[str, Any]) -> bool:
        """Send `changes` to Home Assistant. True if everything asked for got
        there (or there was nothing HA-shaped to send)."""
        client = self.ha_client
        if client is None:
            # set_state checks this before calling, so reaching here means a
            # direct call with no client attached. Reported rather than
            # AttributeError-ing three lines down.
            event("DEVICE", f"{entity_id}: _push_to_ha called with no HA client attached")
            return False
        domain = _domain(entity_id)

        # A COLOUR IS A LIST, AND A LIST MATCHED NOTHING HERE (2026-09-14).
        # The `numeric` comprehension below keeps int and float only, so a
        # colour was filtered out of it, matched no other branch, and fell
        # all the way to this method's final `return True`. set_state then
        # wrote it into the local record and reported success.
        #
        # `set_colour` is the only road that ever really sent one, and
        # NEITHER undo NOR scenes use it -- both go through set_state with
        # the whole state dict:
        #
        #     light.lamp is red. "Make the lamp blue" -> blue, recorded.
        #     "Undo" -> undo hands back {'on':True,'level':50.2,
        #               'colour':[255,0,0]}; exactly one HA call goes out,
        #               turn_on(brightness_pct=50.2); True is returned; the
        #               record is rewritten to red. THE BULB IS STILL BLUE.
        #
        # Sent properly now, by the same `turn_on(rgb_color=...)` call
        # set_colour makes -- one call that both lights it and colours it,
        # which is what "make it blue" means when the light is off.
        value_pushed = False
        # SWITCHING A LIGHT OR FAN OFF SENDS NO VALUE. Their value services
        # (turn_on with brightness_pct / rgb_color, set_percentage) switch
        # the device ON, so a change carrying both a value and on=False --
        # undo restoring {'on': False, 'level': 50}, "set the dimmer to 0"
        # -- lit the bulb or spun the fan up before the turn_off below. The
        # value is still written to the local record by set_state.
        switching_off = ("on" in changes and not bool(changes["on"])
                         and domain in ("light", "fan"))
        colour = changes.get("colour")
        if isinstance(colour, (list, tuple)) and len(colour) == 3:
            if domain != "light":
                event("DEVICE", f"{entity_id}: only a light takes a colour, not "
                      f"'{domain}'. Refusing rather than reporting a success "
                      f"that never left the process.")
                return False
            if switching_off:
                pass
            elif not client.call_service("light", "turn_on", entity_id,
                                         rgb_color=[int(c) for c in colour]):
                event("DEVICE", f"{entity_id}: HA rejected rgb_color="
                      f"{list(colour)} -- local state NOT updated.")
                return False
            else:
                value_pushed = True
            # `turn_on` with a colour IS the turn-on, so the `on` branch
            # below must not send a second one. A brightness in the same
            # change still gets its own call: two turn_on calls with
            # different fields is what HA expects, and combining them into
            # one payload would put a second field through a table that
            # maps exactly one per domain.
        elif colour is not None:
            # Present but not a triple: a record written by hand, or a
            # half-migrated one. Refusing beats writing nonsense to a bulb.
            event("DEVICE", f"{entity_id}: {colour!r} is not an RGB triple.")
            return False

        numeric = {k: v for k, v in changes.items()
                   if k != "on" and isinstance(v, (int, float)) and not isinstance(v, bool)}
        if switching_off:
            numeric = {}

        if numeric:
            spec = self.HA_VALUE_SERVICES.get(domain)
            if spec is None:
                event("DEVICE", f"{entity_id}: '{domain}' has no numeric service "
                      f"mapping -- the value {numeric} cannot be delivered. "
                      f"Refusing rather than reporting a success that never "
                      f"left the process.")
                return False
            service, field = spec
            # One value per call: our state carries at most one gradable field
            # (state_key), and sending several to one service is a guess.
            # `level` first when present: a whole-state restore (undo) can
            # carry other numbers too -- a board's `power` reading -- and
            # whichever came first in the dict used to be sent as the value.
            value = numeric["level"] if "level" in numeric \
                else next(iter(numeric.values()))
            # Keyword expansion, per ha_client.call_service's documented
            # convention. A positional dict raises TypeError.
            if not client.call_service(domain, service, entity_id, **{field: value}):
                event("DEVICE", f"{entity_id}: HA rejected {field}={value} -- "
                      f"local state NOT updated.")
                return False
            value_pushed = True

        if "on" in changes:
            want_on = bool(changes["on"])
            # A value service on a light is turn_on-with-brightness: it has
            # already switched the device on, so re-sending turn_on is noise.
            # Switching OFF still needs its own call.
            #
            # ONLY WHERE THE VALUE SERVICE REALLY DOES POWER THE DEVICE ON
            # (2026-09-14). The comment above reasons about `light.turn_on`
            # and the shortcut was applied to EVERY domain in
            # HA_VALUE_SERVICES -- including climate and water_heater, whose
            # value service is `set_temperature`, which stores a target and
            # starts nothing:
            #
            #     climate.living_room is off. "Set the heating to 22."
            #       -> set_temperature(22) succeeds, we return True here,
            #          climate.turn_on is NEVER sent, the hub reports acted,
            #          the radiator stays cold, and 30s later the poller
            #          flips the tile back to off with no explanation.
            #
            # The four below are the ones where the value service IS the
            # turn-on: light brightness, fan percentage, and a cover or
            # valve position (moving to a position opens it by definition).
            if value_pushed and want_on and domain in _VALUE_SERVICE_POWERS_ON:
                return True
            if domain in self.HA_REFUSE_TO_DRIVE:
                # A service EXISTS and guessing which one is the danger --
                # see HA_REFUSE_TO_DRIVE. Refusing names the reason; the
                # device stays registered and readable.
                event("DEVICE", f"{entity_id}: this hub does not arm or disarm "
                      f"a '{domain}'. Its modes are not one on/off, and "
                      f"disarming takes a code the hub does not hold.")
                return False
            # (A read-only domain never gets here: set_state refuses it
            # before anything is pushed.)
            services = self.HA_ON_OFF_SERVICES.get(domain)
            if services is None:
                # FAILS LOUD, which is what this table's own header has
                # promised since it was written: "domains NOT listed here
                # ... don't map cleanly onto a single on/off concept, so
                # set_state FAILS LOUD for those rather than silently
                # guessing the wrong service call."
                #
                # It did not. It returned True (2026-09-14) -- "local state
                # only" -- the exact phrase and the exact fault the
                # read-only refusal in set_state was written to stop,
                # and the opposite of what the numeric branch at the top of
                # this method does for the identical condition. Three
                # domains reached it in practice, all three offered by the
                # registration picker.
                event("DEVICE", f"{entity_id}: '{domain}' has no clear on/off "
                      f"mapping in this hub, so nothing was sent to Home "
                      f"Assistant. Refusing rather than reporting a success "
                      f"that never left the process.")
                return False
            service = services[0] if want_on else services[1]
            if not client.call_service(domain, service, entity_id):
                event("DEVICE", f"{entity_id}: HA call {service} failed -- local "
                      f"state NOT updated (avoids claiming success that "
                      f"didn't happen).")
                return False
        return True

    def touch(self, speaker: Any, did: str) -> bool:
        """Record a SINGLE device as what this speaker last acted on.

        This overwrites any group set by touch_group -- it does not defer to
        it, whatever the old comment here claimed. Callers that act on several
        devices must therefore call touch_group AFTER the per-device work, not
        before, or the group collapses to whichever device happened to go last.
        """
        did = str(did)
        with self._lock:
            if did not in self.devices:
                return False
            self.context[speaker] = did
            self._group[speaker] = [did]
            self._touched_at[speaker] = time.monotonic()
        return True

    def touch_group(self, speaker: Any, dids: List[str]) -> None:
        """Record that these devices were acted on together (e.g. a multi-pick
        'set the ACs to 20'). A later pronoun command ('make it colder') can then
        refer back to this whole set, not just the single most-recent device.

        An empty list is NOT a way to clear the context -- it returns without
        touching anything, because "these zero devices were acted on together"
        is not a fact worth recording. Use clear_context() to forget."""
        with self._lock:
            valid = [str(d) for d in dids if str(d) in self.devices]
            if not valid:
                return
            self.context[speaker] = valid[-1]   # single last-touched (back-compat)
            self._group[speaker] = valid         # the whole group
            self._touched_at[speaker] = time.monotonic()

    def clear_context(self, speaker: Any) -> None:
        """Forget everything this speaker was last talking about.

        THIS METHOD DID NOT EXIST. ws_server._clear_speaker_context probed for
        it (and for a `forget_group`), found neither, and fell back to
        `touch_group(speaker, [])` -- which returns early on an empty list and
        clears nothing at all. So the per-command context reset that exists to
        stop one phone's "turn it off" resolving against another phone's
        devices was, end to end, a no-op. Verified by test.

        Deliberately one method, not two names for the same idea."""
        with self._lock:
            self.context.pop(speaker, None)
            self._group.pop(speaker, None)
            self._touched_at.pop(speaker, None)

    def _context_expired(self, speaker: Any) -> bool:
        at = self._touched_at.get(speaker)
        return at is None or (time.monotonic() - at) > CONTEXT_TTL_SECONDS

    # `last_touched` USED TO SIT HERE and was removed 2026-09-14: referenced
    # nowhere. Every caller uses `last_group()` below, which carries the same
    # expiry check AND the single-device fallback this one duplicated.

    def last_group(self, speaker: Any) -> List[str]:
        """The set of devices most recently acted on together, or [] once the
        reference has gone stale. Falls back to the single last-touched device.

        Reads under the lock, like every writer: these were the only accessors
        that touched the shared dicts unsynchronised."""
        with self._lock:
            if self._context_expired(speaker):
                return []
            group = self._group.get(speaker)
            if group:
                return [d for d in group if d in self.devices]
            one = self.context.get(speaker)
            return [one] if one and one in self.devices else []
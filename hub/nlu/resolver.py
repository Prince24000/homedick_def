"""
resolver.py — Pure resolution. One function, one decision, no side effects.

resolve_command(ctx) walks a fixed priority order and returns ONE decision
object describing WHAT should happen — it never prints, prompts, or mutates
state. main.py takes the decision and handles I/O (pick prompts, room
questions); executor.py applies it. The resolver is a pure function of its
inputs, testable in isolation.

Priority (first match wins):
    1. securement, 2. literal name, 3. context, 3a. a word of a name,
    3b. a name heard as another word, 4. semantic, 5. room only, 6. none

Decision object:
    {"kind": "execute"|"pick"|"elsewhere"|"none",
     "targets": [...], "action": {...}|None, "candidates": [...],
     "allow_all": bool, "reason": str, "message": str}

"ask_room" used to be listed here and no rung has ever produced it --
_resolve_room_only answers the room case with a "pick" instead. main.py
carried a whole handler for it, including a recursion guard against a loop
that could not occur. Both are gone; the "rooms" key went with them.

"allow_all" was the reverse: produced by four rungs, consumed by main.py to
decide whether the picker offers "all of them", and documented nowhere.
"""

import difflib
from typing import Any, Dict, List, Optional, Protocol, Sequence, Set, Tuple, TypedDict

from nlu.lexicon import (SECUREMENT_VERB_FORMS, PRONOUNS,
                     SECUREMENT_PLURAL_NOUNS, COMMAND_STOPWORDS, GENERIC_DIRECTIONALS,
                     LOCK_VERBS, BARE_COMMAND_WORDS, DETERMINERS, find_colour,
                     is_query, asks_purpose, PURPOSE_WORDS,
                     QUERY_STARTERS, QUERY_VERBS, QUERY_NOUNS,
                     _INVENTORY_FILLER, _INVENTORY_VERBS, _COLLECTIVE_NOUNS)

from core.config import (SEMANTIC_THRESHOLD_MPNET, PLURAL_MATCH_BAND,
                    UNION_SYNONYM_BAND, SEMANTIC_WINNER_MARGIN_MPNET,
                    PLURAL_GROUP_TIGHT_MPNET, PLURAL_GROUP_GAP_MPNET)
from core.hv_common import event

# A plain import: every caller of this module builds its ctx.dm from
# device_manager, so there is no configuration in which the resolver runs
# without it. The ImportError fallback (and the string-matching `_is` it
# needed) could only ever hide a broken device_manager.
from nlu.device_manager import Action


class Ctx(Protocol):
    """What resolve_command needs from its caller. main.py builds this in
    _make_ctx; writing it down means the two can be checked against each
    other instead of agreeing by habit."""
    dm: Any                      # DeviceManager
    text: str
    room_hint: Optional[str]
    slots: Any                   # slot_extractor.Slots
    speaker_id: Any


class Decision(TypedDict, total=False):
    """The one object every rung returns. total=False because only `kind` is
    always meaningful — _decision() fills the rest with empty defaults."""
    kind: str
    targets: List[str]
    action: Optional[Dict[str, Any]]
    candidates: List[Dict[str, str]]
    allow_all: bool
    reason: str
    message: str
    #: heard-as rung: {"said": the word heard, "as": the name word taken}.
    heard: Dict[str, str]
    #: A "Did you mean ...?" decision: the devices to ask about, best first
    #: (shell._confirm_guess).
    alternatives: List[str]
    #: Internal to resolve_command: a sound only CLOSE to a name, so meaning
    #: is tried first. Never reaches the caller.
    heard_close: bool


_PUNCT = ".,;:!?"


def _tokens(text: str) -> List[str]:
    """Lowercase word tokens, punctuation stripped, apostrophes removed.

    ONE tokenizer. There were five slightly different ones: two stripped
    punctuation and apostrophes, two stripped punctuation only, and
    _resolve_context stripped neither -- so "shut it." tokenised to
    ["shut", "it."], "it." was not in PRONOUNS, and the back-reference rung
    declined a command that plainly contained a pronoun. The securement rung
    has a five-line comment about fixing exactly this for itself; the fix was
    never carried across."""
    return [w for w in (t.strip(_PUNCT)
                        for t in text.lower().replace("'", " ").split()) if w]


# Vocabulary lives in lexicon.py (single word-authority). Local aliases keep
# the rest of this module readable.
#
# _LOCK_VERBS was a hand-written list of ten words here. lexicon.LOCK_VERBS
# DERIVES exactly the same ten from SECUREMENT_VERBS (every form without an
# un-/dis- prefix), so the copy was correct only until somebody added an
# eleventh securement verb and didn't mirror it.
_LOCK_VERBS = LOCK_VERBS
_SEC_PLURAL_NOUNS = SECUREMENT_PLURAL_NOUNS
_LITERAL_STOPWORDS = COMMAND_STOPWORDS
_PRONOUNS = PRONOUNS

# Words that carry the ACTION rather than name a target. A command built only
# from these (plus stopwords, pronouns and bare numbers) has named nothing, and
# is therefore a genuine back-reference. Used by _names_a_target below.
# NOTE: any of these already present in COMMAND_STOPWORDS is harmlessly
# redundant — check lexicon.py before adding more, so there's one place to edit.
# Named BARE_COMMAND_WORDS in the lexicon. The old name collided with a
# DIFFERENT module-level _ACTION_WORDS in lexicon.py -- same identifier, two
# memberships, two jobs, two files.
_ACTION_WORDS = BARE_COMMAND_WORDS

# For plural literal-match union (Option C): a semantic candidate only joins if
# it scores within this band of literal's OWN confirmed match. Tight, by
# design — this is meant to catch a near-synonym of the specific device found
# (padlock ~ lock), not everything on the same general topic (lamp ~ bulb is
# too loose to qualify). Smaller than _PLURAL_BAND on purpose.
_UNION_SYNONYM_BAND = UNION_SYNONYM_BAND

# The following are resolution-tuning NUMBERS, not word judgments — they live
# in core/config.py, not in lexicon.py, because they are cosine-similarity
# cutoffs, not vocabulary.
#
# A SINGULAR semantic match is the winner and whatever follows it closely
# (SEMANTIC_WINNER_MARGIN_MPNET), not the cluster above the largest gap. The
# largest-gap cut kept everything above the widest break, however far below
# the best it sat: "turn on the light" offered the motion sensor and the
# display along with the lights. See _resolve_semantic.


# For plural semantic queries, keep every device whose similarity is within this
# band of the top match — catches sibling bulbs that scored just under the
# singular threshold, without dragging in unrelated devices. Tune empirically.
_PLURAL_BAND = PLURAL_MATCH_BAND


def _is_plural_intent(text: str) -> bool:
    """Does the utterance ask for a GROUP? By grammar (grammar.asks_for_group):
    a plural thing, a plural pronoun, or a closed-class group word -- not a
    word that merely ends in "s" ("news", "status")."""
    from nlu import grammar
    return grammar.asks_for_group(text)


def _names_a_target(text: str, ignore: Sequence[str] = ()) -> bool:
    """True if the utterance contains a content word that could name or
    describe a device ("lights", "ac", "cold"), as opposed to being pure
    action + grammar ("turn it off", "set to 20").

    Context is BACK-reference. If the speaker actually named something, then
    guessing from history is wrong even when history exists — that is the
    exact bug this guards: "turn on the lights" carries an explicit switch, so
    it used to satisfy _resolve_context's entry test and get answered with
    whatever device was last touched. The literal rung can't be relied on to
    have consumed it first, because category words ("lights", "fans") match
    semantically but are nobody's literal name.

    `ignore`: words that are not a target even though they are content words
    -- the room the caller already extracted, a colour word."""
    skip = set(ignore)
    for w in _tokens(text):
        if w in _LITERAL_STOPWORDS or w in _PRONOUNS or w in skip:
            continue
        if w in _ACTION_WORDS or w in GENERIC_DIRECTIONALS:
            continue
        if w.replace(".", "").replace("-", "").isdigit():
            continue
        return True
    return False


_DEMONSTRATIVES = frozenset({"this", "that", "these", "those"})


def _has_back_reference(text: str) -> bool:
    """A pronoun standing for a device. "that" FOLLOWED BY A CONTENT WORD is
    a determiner ("turn off that lamp" names a lamp), not a back-reference;
    it used to count as one, so such a command was answered with whatever
    was last touched -- the front-door lock, in the case that found it."""
    words = _tokens(text)
    for i, w in enumerate(words):
        if w not in _PRONOUNS:
            continue
        if w in _DEMONSTRATIVES and i + 1 < len(words):
            nxt = words[i + 1]
            if (nxt not in _LITERAL_STOPWORDS and nxt not in _PRONOUNS
                    and nxt not in _ACTION_WORDS
                    and nxt not in GENERIC_DIRECTIONALS):
                continue                          # determiner, not pronoun
        return True
    return False


def _decision(kind: str, **kw: Any) -> Decision:
    d: Decision = {"kind": kind, "targets": [], "action": None,
                   "candidates": [], "reason": kind, "message": ""}
    d.update(kw)          # type: ignore[typeddict-item]  # kw is open by design
    return d


def _cands(dm: Any, ids: Sequence[str]) -> List[Dict[str, str]]:
    """Pick candidates: each device id with the room it is in."""
    return [{"device_id": d, "room": dm.devices.get(d, {}).get("room", "")}
            for d in ids]


def _phrases(text: str):
    """Every run of up to four words, longest first, that contains no
    stopword -- the n-gram walk both name-matching rungs make."""
    words = _tokens(text)
    n = len(words)
    for length in range(min(4, n), 0, -1):
        for i in range(n - length + 1):
            parts = words[i:i + length]
            if not any(w in _LITERAL_STOPWORDS for w in parts):
                yield parts


#: Said when nothing in the house is what the sentence is about. Never a
#: guess at the nearest device.
NO_DEVICE = "I have no device for that."


def resolve_command(ctx: Ctx) -> Decision:
    """Resolve one command to a single decision. Pure — no side effects.

    ctx provides: dm (lean DeviceManager), text, room_hint, slots (dict),
    speaker_id, embed(s)->normalized vector.
    """
    for rung in (_resolve_securement, _resolve_literal,
                 _resolve_context, _resolve_name_word, _resolve_heard_as,
                 _resolve_semantic, _resolve_room_only):
        dec = rung(ctx)
        if dec is None:
            continue
        # A word only sounding CLOSE to a name is a guess: let meaning try
        # first ("air conditioner" vs "ir receiver"). A word sounding the
        # SAME ("the fun") is not a guess and stays ahead.
        if dec.pop("heard_close", False):      # type: ignore[misc]
            meant = _resolve_semantic(ctx)
            if meant is not None:
                event("CMD", "...but the sentence has a meaning as it stands "
                             "-- that is used, not the sound")
                return meant
        return dec
    if ctx.room_hint and ctx.room_hint not in ctx.dm.rooms:
        return _decision("none", reason="no_such_room",
                         message=f"{not_there(ctx.dm, ctx.room_hint)} {NO_DEVICE}")
    return _decision("none", reason="no_match", message=NO_DEVICE)


def _literal_match_within(ctx: Ctx, allowed: Sequence[str]) -> Optional[List[str]]:
    """Exact-name n-gram walk, restricted to a subset of devices.

    Same phrase walk _resolve_literal does (longest n-gram first, stopwords
    skipped), but scoped to `allowed` — so the securement rung can honour an
    exact device name without handing control to the general literal rung,
    which would lose the securement action.

    Returns a list of matching device ids from `allowed`, or None.
    """
    allowed_ids = set(allowed)
    for parts in _phrases(ctx.text):
        # A phrase made only of securement verbs is the VERB, not a name.
        # Without this, "lock the ac" would match a device literally named
        # "lock" off the back of the verb itself. A device genuinely named
        # "lock" is still reachable via "lock the lock", where the second
        # occurrence is a separate n-gram position.
        if all(w in SECUREMENT_VERB_FORMS for w in parts):
            continue
        action, payload = ctx.dm.resolve_count(" ".join(parts), ctx.room_hint)
        if not payload:
            continue
        if action == Action.ELSEWHERE:
            payload = [d for d, _room in payload]
        hits = [d for d in payload if d in allowed_ids]
        if hits:
            return hits
    return None


# ── 1. Securement ──
def _resolve_securement(ctx: Ctx) -> Optional[Decision]:
    dm = ctx.dm
    # Strip punctuation before matching. _resolve_literal already does this;
    # this rung did not, so "please unlock." tokenised to "unlock." — no
    # securement verb found, no securement action, and the command silently
    # dropped through to the ordinary rungs. (It was still caught downstream
    # by _carry_out's bare-reference guard, so it was never an open door —
    # but a trailing full stop should not change which rung handles a lock.)
    tokens = _tokens(ctx.text)
    words = set(tokens)
    # Match any surface form (bare, "-ed", "-ing" — "unlocked", "locking") and
    # normalize to its base verb, so tense doesn't matter for recognition or
    # for the lock_on/_LOCK_VERBS lookup below. By text order, so "unlock the
    # lock" resolves to unlock, not the alphabetically-first "lock".
    verb = next((SECUREMENT_VERB_FORMS[w] for w in tokens if w in SECUREMENT_VERB_FORMS), None)
    if not verb:
        return None

    lock_on = verb in _LOCK_VERBS
    # NOTE: no "requires_confirmation" key. It used to be set to `not lock_on`
    # — i.e. only security-REDUCING verbs need confirming — and nothing ever
    # read it. Policy since then is stricter and lives in _carry_out: EVERY
    # secure-device action gets the full active voice challenge, lock and
    # unlock alike. Leaving the field in place would invite someone to wire up
    # the weaker rule later and quietly reintroduce "unlocking is checked,
    # locking isn't".
    action = {"type": "secure", "lock_on": lock_on, "verb": verb}

    all_secure = [(did, d) for did, d in dm.devices.items() if d.get("secure")]
    sec = [(did, d) for did, d in all_secure
           if not ctx.room_hint or d.get("room") == ctx.room_hint]
    if not sec:
        # Distinguish "nothing is secure" from "nothing secure IS IN THAT
        # ROOM". Both used to say "No securement devices to act on.", which is
        # false and unhelpful when the lock is one room over -- the literal
        # rung reports that case as `elsewhere`, and this rung never returns
        # control to it.
        if all_secure and ctx.room_hint:
            return _decision("elsewhere", action=action, allow_all=True,
                             candidates=_cands(dm, [did for did, _ in all_secure]),
                             reason="securement_elsewhere",
                             message=f"{not_there(dm, ctx.room_hint, 'securement device')} "
                                     f"Found elsewhere — pick one or skip:")
        return _decision("none", reason="securement_no_devices",
                         message="No securement devices to act on.")

    sec_ids = [did for did, _ in sec]

    # Plural ("lock all the doors") -> act on every secure device in scope,
    # offered as a pick-all so the user still confirms the batch.
    is_plural = "all" in words or bool(words & _SEC_PLURAL_NOUNS)
    if is_plural and len(sec) > 1:
        return _decision("pick", allow_all=True, candidates=_cands(dm, sec_ids),
                         action=action, reason="securement_plural",
                         message="Which securement device(s)?")

    # Exact device name, scoped to secure devices. This rung runs BEFORE
    # _resolve_literal and never returns control to it, so without this check
    # a named lock was unreachable: "lock the front door" fell straight to the
    # semantic branch below and asked which device, every single time, even
    # with an exact name match sitting in the registry. Naming a specific lock
    # is precisely the case where the user should not be asked again.
    #
    # Runs BEFORE the single-device shortcut. It used to run only with two or
    # more secure devices, so with one lock in the house "unlock the bike
    # lock" (or "unlock the garage") unlocked the front door: the verb alone
    # chose the target and the object was never read.
    named = _literal_match_within(ctx, sec_ids)
    if not named:
        # The utterance names a device that is NOT secure -> refuse, rather
        # than aim the verb at whichever lock happens to exist.
        #
        # "NOT SECURE" IS A QUESTION ABOUT THE DEVICE, NOT ABOUT THE ROOM
        # (2026-09-14). This asked it against `by_id`, which is `sec` --
        # the secure devices IN `ctx.room_hint`. So a lock one room away
        # came back as "not a securement device", the verb was dropped, and
        # the person was told a flat untruth about their own lock:
        #
        #     registry: hall.front_door (secure), kitchen.window_latch (secure)
        #     room_hint "kitchen", "lock the front door"
        #       -> "front door is not a securement device."   the door never locks
        #     room_hint None, same words
        #       -> execute on hall.front_door
        #
        # The `elsewhere` branch written for exactly this case (above) could
        # not save it either: that one only runs when `sec` is EMPTY, so a
        # single secure device in the current room disabled it.
        #
        # Asked against `all_secure` now. A named device that IS secure but
        # out of scope falls through to the pick below, which is what
        # `elsewhere` means; only a genuinely non-secure device is refused.
        secure_ids = {did for did, _ in all_secure}
        other = _literal_match_within(ctx, list(dm.devices))
        if other and not any(did in secure_ids for did in other):
            name = dm.devices.get(other[0], {}).get("name") or other[0]
            return _decision("none", reason="securement_not_secure",
                             message=f"{name} is not a securement device.")
        if other:
            # Named, secure, and in another room. Offer it rather than
            # silently acting on the one that happens to be in here: a lock
            # is the last device in the house to aim by proximity.
            elsewhere = [did for did in other if did in secure_ids]
            return _decision("pick", allow_all=False, action=action,
                             candidates=_cands(dm, elsewhere),
                             reason="securement_named_elsewhere",
                             message=_named_elsewhere(dm, ctx.room_hint, elsewhere[0]))
        if len(sec) == 1:
            return _decision("execute", targets=sec_ids, action=action,
                             reason="securement_single")
    elif len(named) == 1:
        return _decision("execute", targets=named, action=action,
                         reason="securement_named")
    else:
        return _decision("pick", allow_all=True, candidates=_cands(dm, named),
                         action=action, reason="securement_named_pick",
                         message="Which securement device(s)?")

    # Semantic disambiguation: embed the whole command (verb + object words like
    # "door"/"gate") and rank secure devices by description similarity. This
    # catches bolt/latch/seal that share no literal word with a device's name.
    # A restricted ranking always returns the whole subset (device_manager
    # appends any it could not score), so it needs no fallback or filter.
    return _decision("pick", allow_all=True,
                     candidates=_cands(dm, dm.resolve_semantic(ctx.text, restrict=sec_ids)),
                     action=action, reason="securement_semantic",
                     message="Which securement device(s)?")


def not_there(dm, room: Optional[str], thing: str = "") -> str:
    """What to say when a room was named: the house has no such room, or the
    room has no such thing."""
    if not room:
        return f"No {thing or 'such device'} there."
    if room not in dm.rooms:
        return f"There's no room called {room}."
    if not thing:
        return f"There's nothing like that in the {room}."
    from nlu.grammar import asks_for_group
    are = "are" if asks_for_group(f"the {thing}") else "is"
    return f"There {are} no {thing} in the {room}."


def _named_elsewhere(dm, room: Optional[str], did: str) -> str:
    name = dm.devices.get(did, {}).get("name") or did
    if room and room not in dm.rooms:
        return f"{not_there(dm, room)} {name} is elsewhere — confirm:"
    return f"{name} is not in {room} — confirm:"


# ── 2. Literal name ──
def _resolve_literal(ctx: Ctx) -> Optional[Decision]:
    dm = ctx.dm
    for parts in _phrases(ctx.text):
        phrase = " ".join(parts)
        action, payload = dm.resolve_count(phrase, ctx.room_hint)
        if action == Action.EXECUTE and payload:
            targets = list(payload)
            semantic_added = False
            if phrase.endswith("s"):
                # Plural category word ("locks", "doors") — also check
                # semantics for devices of the same KIND that literal's
                # exact-name matching missed (e.g. "smart padlock" for
                # "locks" — "padlock" shares no word with "lock"). TIGHT: a
                # candidate is only added if it scores nearly as high as
                # literal's OWN confirmed match — a near-synonym (padlock ~
                # lock), not merely topically related (a "reading lamp" is
                # NOT a bulb, even though both concern light — it should NOT
                # ride along on "bulbs"). This only runs for plural phrases
                # and only once literal already found something, so a normal
                # singular command ("turn on the ac") never pays this cost.
                scored = dm.resolve_semantic_scored(ctx.text)
                score_of = dict(scored)
                anchor = max((score_of.get(t, -1.0) for t in targets), default=-1.0)
                if anchor > -1.0:
                    extra = [d for d, s in scored
                            if d not in targets and s >= anchor - _UNION_SYNONYM_BAND]
                    if ctx.room_hint:
                        extra = [d for d in extra
                                if dm.devices.get(d, {}).get("room") == ctx.room_hint]
                    if extra:
                        targets += extra
                        semantic_added = True

            # A plural matching multiple devices ("acs" -> ac, ac2), OR a
            # semantic addition (less certain than a literal name match —
            # always confirm it, even if the command had an explicit verb,
            # since we're now acting on a device literal never found and it
            # may be a secure one). A bare plural asks; an explicit action
            # with NO semantic addition still applies directly, unchanged.
            if len(targets) > 1 and (semantic_added or not _has_explicit_action(ctx.slots)):
                reason = "literal_semantic_union" if semantic_added else "literal_plural_pick"
                return _decision("pick", allow_all=True,
                                 candidates=_cands(dm, targets),
                                 reason=reason, message="Which one(s)?")
            reason = "literal_single" if len(targets) == 1 else "literal_fanout"
            return _decision("execute", targets=targets, reason=reason)
        if action == Action.PICK and payload:
            # allow_all, like every other pick. Without it main.py takes
            # the _pick_one branch and the client renders a single-select
            # list -- so "turn on the ac" with two ACs let you have one of
            # them and no way to say both. This is the rung a plain device
            # name lands on, which made it the one people meet most.
            #
            # Multi-select is strictly more capable: picking one is still
            # one tap plus Send. Being unable to pick two is a dead end.
            return _decision("pick", allow_all=True,
                             candidates=_cands(dm, payload),
                             reason="literal_pick", message="Multiple matches")
        if action == Action.ELSEWHERE and payload:
            return _decision("elsewhere", allow_all=True,
                             candidates=[{"device_id": d, "room": rm}
                                         for d, rm in payload],
                             reason="literal_elsewhere",
                             message=f"{not_there(dm, ctx.room_hint, phrase)} "
                                     f"Found elsewhere — pick one or skip:")
    return None


def _has_explicit_action(slots: Optional[Dict[str, Any]]) -> bool:
    """True if the command specifies WHAT to do — an on/off switch, a target
    value, or a directional delta. False for a bare device name (which would
    otherwise defaults to a flip). Used to decide whether a plural should act
    immediately or ask which/all."""
    slots = slots or {}
    # NB: no `slots.get("direction")`. That key does not exist -- SlotExtractor
    # emits `delta_direction`, and the Slots TypedDict now says so -- so the
    # test was dead weight reading as a fourth condition.
    return bool(slots.get("switch")
                or slots.get("absolute_value") is not None
                or slots.get("delta") is not None
                or slots.get("delta_direction") is not None)


def _is_adjustable(device: Dict[str, Any]) -> bool:
    """True if the device has an adjustable numeric value (a non-binary state
    key with a defined range) — i.e. something a generic 'higher'/'lower' can
    move. Binary on/off devices return False."""
    if not device:
        return False
    sk = device.get("state_key")
    if not sk or sk == "on":
        return False
    return device.get("range_min") is not None and device.get("range_max") is not None


# ── 3. Context (semantics-confirmed, group-aware) ──
def _resolve_context(ctx: Ctx) -> Optional[Decision]:
    """Refers back to what you just controlled. Fires when the command either
    (a) contains a pronoun ("make IT colder", "turn IT off"), or (b) is a bare
    directional/value change that NAMES NO DEVICE ("a little higher", "warmer",
    "set to 20", "off") — humans routinely drop the pronoun, and such a command
    is still implicitly about the last-touched device. For directional commands
    the command must be semantically relevant to the device, so "make it warmer"
    after touching a washing machine won't drive the washer.

    The "names no device" half of that is enforced by _names_a_target, and it
    is load-bearing. It used to be asserted only in this docstring: the entry
    test was `has_pronoun or directional or explicit_switch`, so ANY command
    with an on/off switch entered the rung — including ones that plainly named
    their target. "turn on the lights" would then be answered with whatever was
    last touched (the shed AC), because "lights" is nobody's literal name and
    so the literal rung above hadn't consumed it. The bug was invisible on a
    cold start (empty last_group -> falls through) and near-permanent in the
    long-lived ws_server shell, where the group is essentially never empty."""
    dm = ctx.dm
    # _tokens, not a bare split: this rung stripped no punctuation at all, so
    # a trailing full stop hid the pronoun it exists to detect.
    has_pronoun = _has_back_reference(ctx.text)

    slots = ctx.slots or {}
    directional = (slots.get("delta") is not None
                   or slots.get("delta_direction") is not None)
    explicit_switch = (bool(slots.get("switch"))
                       or slots.get("absolute_value") is not None)
    ignore = _non_target_words(ctx)

    # Enter only on a pronoun, or a bare change-command (directional/value) that
    # named no device. If neither, this rung doesn't apply.
    #
    # The pronoun branch is held to _names_a_target too. It was exempt, so
    # "set this lamp to 50" -- "this" counted as a pronoun and "lamp" was
    # never read -- set whatever was last touched.
    # "What does IT do?" (slots["purpose"], set by the shell for a purpose
    # question) is about the device just touched, whatever its other words.
    if has_pronoun and (slots.get("purpose")
                        or not _names_a_target(ctx.text, ignore)):
        pass                                    # "turn IT off" — explicit back-reference
    elif directional and not explicit_switch:
        pass                                    # bare "warmer", "a little higher"
    elif explicit_switch and not _names_a_target(ctx.text):
        pass                                    # bare "off", "set to 20"
    else:
        return None

    group = dm.last_group(ctx.speaker_id)
    if not group:
        return None
    if ctx.room_hint:
        group = [d for d in group if dm.devices.get(d, {}).get("room") == ctx.room_hint]
        if not group:
            return None

    # Pure back-reference with an explicit uniform action ("set it to 20",
    # "turn it off") -> apply to the WHOLE last group, no filtering needed.
    if explicit_switch and not directional:
        return _context_execute(group)

    # Directional/comparative ("colder", "warmer", "brighter", "louder") -> the
    # command must be semantically RELEVANT to the device. This is general: it
    # excludes a washing machine from "make it warmer" (a washer's description
    # doesn't match warming) while including a heat pump. It is not a hardcoded
    # list of temperature words — the embedding decides relevance for any
    # command. Devices whose descriptions are too thin to score will be excluded
    # here; the fix for those is a real description, since they're invisible to
    # ALL semantic matching, not just this path.
    if directional:
        # delta_head, not delta_raw: for "turn it up by 5" the raw text is
        # "by 5", which never matched a generic directional, so an explicit
        # delta on a pronoun skipped this shortcut every time.
        dir_word = (slots.get("delta_head") or slots.get("delta_raw") or "").lower()
        is_generic = dir_word in GENERIC_DIRECTIONALS

        if is_generic:
            # Generic directionals ("higher", "up", "raise") mean "change the
            # value" with no domain implied — they apply to any group member
            # that HAS an adjustable numeric value, regardless of description.
            # This is why "a little higher" works on an AC even with a thin
            # description: the test is "does it have something to raise", not
            # "does the word 'higher' match the description". Adjustability
            # strictly decides — a group of only binary devices has nothing to
            # raise, so we fall through rather than no-op on an unadjustable one.
            hit = [d for d in group if _is_adjustable(dm.devices.get(d, {}))]
            return _context_execute(hit) if hit else None

        # Domain-laden directionals ("warmer", "cooler", "brighter", "louder")
        # imply a KIND of device, so the command must be semantically relevant.
        # This excludes a washing machine from "make it warmer" while including a
        # heat pump. Thin-description devices score low here; the fix for those
        # is a real description, since they're invisible to all semantic matching.
        scored = dm.resolve_semantic_scored(ctx.text)
        relevant = {d for d, s in scored if s >= SEMANTIC_THRESHOLD_MPNET}
        hit = [d for d in group if d in relevant]
        # No group member fits (e.g. "make it warmer" after touching only a
        # washing machine) -> fall through so the semantic rung can find the
        # right device instead.
        return _context_execute(hit) if hit else None

    # Bare pronoun, no explicit action -> toggle the whole last group.
    return _context_execute(group)


# ── Spoken names: what is said over a running command ──
#
# Rules only, never meaning: a word of a device's name, or a word that surely
# sounds like one ("lump" for "lamp"), for what is said while a command runs.
try:  # optional, as in app_nav and voice_security/challenge
    import jellyfish as _jf  # type: ignore
    _metaphone = getattr(_jf, "metaphone", None)
except Exception:  # noqa: BLE001
    _metaphone = None


def _sound(word: str) -> str:
    if _metaphone is None:
        return ""
    try:
        return str(_metaphone(word) or "")
    except Exception:  # noqa: BLE001
        return ""


def _one_sound_apart(a: str, b: str) -> bool:
    """One sound code is the other with a single consonant dropped
    (BB / BLB: "bob" for "bulb")."""
    if abs(len(a) - len(b)) != 1 or not a or not b or a[0] != b[0]:
        return False
    short, long_ = (a, b) if len(a) < len(b) else (b, a)
    return any(long_[:i] + long_[i + 1:] == short for i in range(len(long_)))


def _heard_as(said: str, name: str) -> float:
    """How well `said` passes for the name word `name`: 1.0 the same sound
    or nearly the same spelling (sure), 0.5 close enough to ASK about, 0.0
    not at all."""
    if said == name or len(said) < 2 or len(name) < 2:
        return 0.0
    ratio = difflib.SequenceMatcher(None, said, name).ratio()
    sa, sn = _sound(said), _sound(name)
    if ratio >= 0.84 or (sa and sa == sn and min(len(said), len(name)) >= 3):
        return 1.0
    if ratio >= 0.7:
        return 0.5
    if sa and sn and abs(len(said) - len(name)) <= 1 and _one_sound_apart(sa, sn):
        return 0.5
    return 0.0


def _spoken_words(text: str) -> List[str]:
    """The sentence's words, with letters the recogniser spelled out put
    back together: "d-a-c" and "a c" are one word each."""
    out: List[str] = []
    run: List[str] = []
    for w in _tokens(text):
        if "-" in w and all(len(p) == 1 for p in w.split("-")):
            w = w.replace("-", "")
        if len(w) == 1 and w.isalpha():
            run.append(w)
            continue
        if len(run) > 1:
            out.append("".join(run))
        else:
            out += run
        run = []
        out.append(w)
    out += ["".join(run)] if len(run) > 1 else run
    return out


# ── 3a. A word of a device's name, said for the whole name ──
def _resolve_name_word(ctx: Ctx) -> Optional[Decision]:
    """A word of a device's name said for the whole name ("the lamp" for
    "living room lamp", "the ac" for "ac socket"). Done only when MEANING
    AGREES (its best device carries the word), which tells "the ac" from
    "the motion light" (not the motion sensor). Else left to the rungs
    below. Never a security device."""
    dm = ctx.dm
    owners: Dict[str, List[str]] = {}
    for did, d in dm.devices.items():
        if ctx.room_hint and d.get("room") != ctx.room_hint:
            continue
        for w in _tokens(str(d.get("name") or "")):
            if w in _LITERAL_STOPWORDS or w.isdigit():
                continue
            owners.setdefault(w, []).append(did)
    if not owners:
        return None
    skip = set(_non_target_words(ctx))
    named: Optional[List[str]] = None
    plural = False
    for said in _tokens(ctx.text):
        if (said in _LITERAL_STOPWORDS or said in _PRONOUNS or said in skip
                or said in _ACTION_WORDS or said in GENERIC_DIRECTIONALS):
            continue
        dids = owners.get(said)
        if dids is None and said.endswith("s") and said[:-1] in owners:
            dids, plural = owners[said[:-1]], True
        if not dids:
            continue
        # Two name words: devices carrying both, else the narrower word.
        both = [d for d in dids if named and d in named]
        named = both or (dids if named is None or len(dids) < len(named)
                         else named)
    if not named:
        return None
    # A security device is never switched on part of its name.
    if any(dm.devices.get(d, {}).get("secure") for d in named):
        return None
    scored = dm.resolve_semantic_scored(ctx.text)
    if ctx.room_hint:
        scored = [(d, sc) for d, sc in scored
                  if dm.devices.get(d, {}).get("room") == ctx.room_hint]
    if not scored or scored[0][0] not in named:
        return None
    label = ", ".join(str(dm.devices.get(d, {}).get("name") or d) for d in named)
    event("CMD", f"a word of the name, and meaning agrees "
                 f"({scored[0][1]:.3f}): {label}")
    if len(named) == 1:
        return _decision("execute", targets=list(named), reason="name_word")
    if plural and _has_explicit_action(ctx.slots):
        return _decision("execute", targets=list(named),
                         reason="name_word_fanout")
    return _decision("pick", allow_all=True, candidates=_cands(dm, named),
                     reason="name_word_pick", message="Which one(s)?")


def _verbs_here(text: str) -> Set[str]:
    """The words grammar reads as verbs in `text` (empty when grammar is
    off)."""
    try:
        from nlu import grammar
        if not grammar.ENABLED or not grammar.load():
            return set()
        doc = grammar._parse(text)
        return {t.lower_ for t in (doc or ()) if t.pos_ == "VERB"}
    except Exception:                                   # noqa: BLE001
        return set()


# ── 3b. A name the recogniser wrote as another word ──
def _resolve_heard_as(ctx: Ctx) -> Optional[Decision]:
    """"Turn on the bob" for "bulb": tried after the name and back-reference
    rungs, before meaning, and only toward words in a device's own name (the
    registry is the vocabulary).

    Only a sentence saying what to do ("It's dark in here" is meaning's,
    "here" is not "heater"), or a QUESTION (reads only; question words are
    skipped), or -- ungated -- one whose verb was misheard too ("Done on the
    lump"): then only a same-sound word, with meaning agreeing, and it is
    ASKED, never done. A security device is always asked about."""
    asking = is_query(ctx.text)
    ungated = not (_has_explicit_action(ctx.slots) or asking)
    dm = ctx.dm
    # As in the semantic rung: a sound-alike is a guess, and an order is
    # never offered a device that can only be read.
    orders = not _asks(ctx.text)
    name_words: Dict[str, List[str]] = {}
    for did, d in dm.devices.items():
        if ctx.room_hint and d.get("room") != ctx.room_hint:
            continue
        if orders and _read_only(dm, did):
            continue
        for w in _tokens(str(d.get("name") or "")):
            if w in _LITERAL_STOPWORDS or w.isdigit():
                continue
            name_words.setdefault(w, []).append(did)
    if not name_words:
        return None
    skip = set(_non_target_words(ctx))
    if asking:
        skip |= (QUERY_STARTERS | QUERY_VERBS | QUERY_NOUNS | _INVENTORY_FILLER
                 | _INVENTORY_VERBS | _COLLECTIVE_NOUNS)
    # Words grammar reads as verbs are what is done, not the device ("can I
    # CREATE ..." is not "card").
    skip |= _verbs_here(ctx.text)
    best: Dict[str, Tuple[float, str, str]] = {}      # device -> (how, said, as)
    for said in _spoken_words(ctx.text):
        if (said in _LITERAL_STOPWORDS or said in _PRONOUNS or said in skip
                or said in _ACTION_WORDS or said in GENERIC_DIRECTIONALS
                or said in name_words or not said.isalpha()):
            continue
        # Plural ("bobs"): keep the better of the two.
        one = said[:-1] if said.endswith("s") and len(said) > 3 else said
        for name, dids in name_words.items():
            how = max(_heard_as(said, name), _heard_as(one, name))
            for did in dids if how else ():
                if how > best.get(did, (0.0, "", ""))[0]:
                    best[did] = (how, said, name)
    if ungated and best:
        # Ungated: also spelt close, not only same sound ("phone" vs "fan").
        near = [d for d, _ in dm.resolve_semantic_scored(ctx.text)[:3]]
        best = {d: v for d, v in best.items()
                if v[0] >= 1.0 and d in near
                and difflib.SequenceMatcher(None, v[1], v[2]).ratio() >= 0.7}
    if not best:
        return None
    ranked = sorted(best, key=lambda d: -best[d][0])
    sure = [d for d in ranked if best[d][0] >= 1.0]
    how, said, name = best[ranked[0]]
    event("CMD", f"heard {said!r}, which no device is called -- it sounds like "
                 f"{name!r} ({'the same' if how >= 1.0 else 'close'}): "
                 + ", ".join(str(dm.devices.get(d, {}).get('name') or d)
                             for d in ranked[:3]))
    heard = {"said": said, "as": name}
    secure = any(dm.devices.get(d, {}).get("secure") for d in ranked)
    if sure and len(sure) == len(ranked) and not secure and not ungated:
        if len(sure) == 1:
            return _decision("execute", targets=sure, reason="heard_as",
                             heard=heard)
        # Several devices carry that word ("bulb", "bulb 2"): which.
        return _decision("pick", allow_all=True, candidates=_cands(dm, sure),
                         reason="heard_pick", message="Which one(s)?",
                         heard=heard)
    d = dm.devices.get(ranked[0], {})
    what = " in the ".join(p for p in (d.get("name") or ranked[0],
                                       d.get("room") or "") if p)
    return _decision("pick", allow_all=False, candidates=_cands(dm, ranked[:3]),
                     reason="heard_confirm", alternatives=ranked[:3],
                     message=f"Did you mean the {what}?", heard=heard,
                     # Only close, not the same sound: see resolve_command.
                     heard_close=how < 1.0)


def _name_words(names: Sequence[str]) -> Set[str]:
    out: Set[str] = set()
    for name in names:
        for w in _tokens(str(name or "")):
            if w not in _LITERAL_STOPWORDS:
                out |= {w, w + "s"}
    return out


def names_a_device(text: str, names: Sequence[str]) -> bool:
    """Does the sentence say one of these names -- a word of it, or a word
    that surely sounds like one ("lump" for "lamp")? Rules only: what is
    said over a running command is never searched by meaning for a device,
    or the television would add them."""
    named = _name_words(names)
    for w in _spoken_words(text):
        if w in _LITERAL_STOPWORDS or w in DETERMINERS:
            continue
        if w in named or any(_heard_as(w, n) >= 1.0 for n in named):
            return True
    return False


def only_named(text: str, names: Sequence[str], rooms: Sequence[str] = ()) -> bool:
    """Is the sentence the device and nothing else ("the kitchen lamp",
    "lamp please")? That is a flip; anything more has to say which way."""
    known = _name_words(names) | set(_LITERAL_STOPWORDS) | set(DETERMINERS)
    for room in rooms:
        known |= set(_tokens(str(room or "")))
    return all(w in known for w in _spoken_words(text))

def _context_execute(targets: List[str]) -> Decision:
    return _decision("execute", targets=targets,
                     reason="context" if len(targets) == 1 else "context_group")


def _non_target_words(ctx: Ctx) -> List[str]:
    """Words that are content words but not a device: the room already
    extracted, the delta head ("warmer"), and a colour ("make it blue")."""
    slots = ctx.slots or {}
    ignore = list(_tokens(ctx.room_hint or "")) + list(_tokens(slots.get("delta_raw") or ""))
    if slots.get("colour") is not None:
        ignore += [w for w in _tokens(ctx.text) if find_colour([w])]
    # "What does it do?": the question's own words name nothing.
    if asks_purpose(ctx.text):
        ignore += list(PURPOSE_WORDS)
    return ignore


def _asks(text: str) -> bool:
    """A question: by its first word (lexicon.is_query), or by grammar
    wherever it starts ("hey is the fan still on", grammar.asks). The same
    test the shell makes before it reads instead of switching."""
    if is_query(text):
        return True
    from nlu import grammar
    return grammar.asks(text)


def _read_only(dm: Any, did: str) -> bool:
    """Can this device only be READ (a sensor, a meter)? DeviceManager's own
    rule (is_readable_only); a stand-in registry without it is read by the
    record's `readable_only` flag."""
    d = dm.devices.get(did) or {}
    rule = getattr(dm, "is_readable_only", None)
    try:
        return bool(rule(d)) if rule is not None else bool(d.get("readable_only"))
    except Exception:                                   # noqa: BLE001
        return bool(d.get("readable_only"))


# ── 4. Semantic ──
def _resolve_semantic(ctx: Ctx) -> Optional[Decision]:
    dm = ctx.dm
    # This rung weighs a DESCRIPTION -- "it's dark in here", "sound the
    # alarm" -- against the devices' descriptions. A chunk that names or
    # describes nothing (pure action and grammar: "down", "off", "set to
    # 20") has nothing to weigh, and its cosine against any description is
    # noise; with the floor at 0.15 that noise occasionally clears it. The
    # spoken-command run of 2026-09-10 showed it: the recogniser wrote
    # "Down on the Bob." for "turn on the bulb", the splitter cut off the
    # bare "down", and this rung sent it to the display at 0.161. Such a
    # chunk is a back-reference (the context rung's, above) or nothing.
    # Only the room is set aside here: a domain-laden word -- "brighter",
    # "warmer", a colour -- does describe a kind of device and may still
    # be weighed when nothing was touched before; a generic one -- "up",
    # "down", "higher" -- describes nothing and is skipped by the test.
    if not _names_a_target(ctx.text, _tokens(ctx.room_hint or "")):
        return None
    # ONE scoring pass, a LOW floor, banded or clustered below. This used to
    # call resolve_semantic() first purely as a gate and then
    # resolve_semantic_scored() for the answer -- two MPNet embeddings of the
    # same sentence, the second one on every command that reached this rung.
    scored = dm.resolve_semantic_scored(ctx.text)
    if ctx.room_hint:
        scored = [(d, s) for d, s in scored
                  if dm.devices.get(d, {}).get("room") == ctx.room_hint]
    # AN ORDER NEVER REACHES A DEVICE THAT CAN ONLY BE READ. "Turn on the
    # light" offered the motion sensor beside the lights: a sensor scores
    # near anything it watches, and picking it could only end in "that only
    # reports a reading" (executor). A QUESTION still reaches it ("is the
    # light on", "hey is the fan still on" -- grammar), since reading is
    # all a question does.
    if not _asks(ctx.text):
        scored = [(d, s) for d, s in scored if not _read_only(dm, d)]
    floor, margin = SEMANTIC_THRESHOLD_MPNET, SEMANTIC_WINNER_MARGIN_MPNET
    # What was weighed, for tuning the numbers in core/config.py.
    event("CMD", "by meaning: " + (", ".join(
        f"{dm.devices.get(d, {}).get('name') or d} ({s:.3f})"
        for d, s in scored[:3]) or "nothing scored")
        + f"  [match from {floor}, winner's lead {margin}]")
    # Guard: the BEST match must clear the real threshold, else this isn't a
    # genuine hit — fall through to no-match rather than surfacing noise.
    if not scored or scored[0][1] < floor:
        return None
    top = scored[0][1]

    # Plural intent ("lights", "all the bulbs") offers the WHOLE matching set
    # for the user to choose all/any. To catch sibling devices that scored
    # just under the singular threshold (e.g. a 2nd/3rd bulb), widen to a
    # band: keep only devices whose score is close to the top match — so all
    # the lights come, but the AC doesn't.
    if _is_plural_intent(ctx.text):
        # ...UNLESS IT NEEDS NO QUESTION: a tight group at the top (within
        # PLURAL_GROUP_TIGHT of the best) with a clear drop after it (at
        # least PLURAL_GROUP_GAP) is done, when the sentence says what to do
        # ("turn on the lights") and none of the group is a security device.
        # A bare "lights", a loose group or a lock among them still asks.
        # (Defense used to ask every time, so a plural by meaning never
        # simply happened.)
        tight = [(d, s) for d, s in scored
                 if s >= floor and top - s <= PLURAL_GROUP_TIGHT_MPNET + 1e-9]
        rest = scored[len(tight):]
        if (len(tight) > 1
                and _has_explicit_action(ctx.slots)
                and (not rest or tight[-1][1] - rest[0][1]
                     >= PLURAL_GROUP_GAP_MPNET - 1e-9)
                and not any(dm.devices.get(d, {}).get("secure")
                            for d, _ in tight)):
            return _decision("execute", targets=[d for d, _ in tight],
                             reason="semantic_plural_group")
        chosen = [d for d, s in scored if s >= top - _PLURAL_BAND]
        reason = "semantic_plural"
    else:
        # Singular intent: THE WINNER AND WHATEVER FOLLOWS IT CLOSELY
        # (SEMANTIC_WINNER_MARGIN). This used to keep everything above the
        # largest gap in the scores, however far below the best that gap
        # sat -- which is how a motion sensor and a display came to be
        # offered for "turn on the light".
        chosen = [d for d, s in scored if s >= floor and top - s < margin]
        reason = "semantic_pick"
        # A lone winner must be clear of noise too: a runner-up close behind
        # it but under the floor means the top is barely above noise itself.
        if (len(chosen) == 1 and len(scored) > 1
                and top - scored[1][1] < margin):
            return None
    # Only one device actually matches -> nothing to pick among, just act on
    # it (e.g. "wash my clothes" -> the one washer).
    if len(chosen) == 1:
        return _decision("execute", targets=chosen, reason="semantic_single")
    return _decision("pick", allow_all=True, candidates=_cands(dm, chosen),
                     reason=reason, message="Which one(s)?")


# ── 5. Room only ──
def _resolve_room_only(ctx: Ctx) -> Optional[Decision]:
    """A room was named but nothing above resolved to a specific device
    ("living room", "kitchen"). List that room's devices for the user to pick
    one or all."""
    dm = ctx.dm
    if not ctx.room_hint:
        return None
    matches, _ = dm.room_devices(ctx.room_hint)
    if not matches:
        return None
    # Something besides the room was named and nothing there is it: said so
    # before its devices are offered.
    if _names_a_target(ctx.text, _non_target_words(ctx)):
        return _decision("pick", allow_all=True, candidates=_cands(dm, matches),
                         reason="room_only_not_there",
                         message=f"There's nothing like that in the "
                                 f"{ctx.room_hint}. Its devices — pick one or skip:")
    return _decision("pick", allow_all=True, candidates=_cands(dm, matches),
                     reason="room_only",
                     message=f"Devices in {ctx.room_hint} — pick one or all:")

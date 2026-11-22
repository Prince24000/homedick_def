"""Things both the shell and its collaborators need.

Extracted so ha_flows.py and esp_builder.py can log and can raise the
cancellation exception without importing main.py, which imports them --
a cycle that Python would only report as a confusing half-initialised
module at start-up.
"""

import contextlib
import contextvars
import re
import time
from typing import Optional
import unicodedata


def _cancel_tokens():
    """The project's cancel words, read at CALL time.

    Not bound at import: the test suites stub `lexicon.CANCEL_TOKENS`, and a
    module-level import would have copied the old value before they got the
    chance. Python caches the module, so asking each time is a dict lookup.
    """
    from nlu.lexicon import CANCEL_TOKENS
    return CANCEL_TOKENS


#: Padding around a cancel that carries no intent of its own.
_CANCEL_FILLERS = frozenset({
    "please", "just", "ok", "okay", "um", "uh", "er", "ah", "yeah", "yep",
    "well", "so", "now", "it", "this", "that", "all", "right", "then",
    "lets", "let", "i", "wanna", "want", "to", "the",
})


def means_cancel(text) -> bool:
    """Does this answer mean "stop, I'm out"?

    WHY THIS EXISTS. Every cancel check in the project was
    `value.lower() in CANCEL_TOKENS` -- exact set membership. That is right
    for something TYPED at a terminal and wrong for something SPOKEN. The
    phone transcribes with Moonshine and sends the text verbatim, so the hub
    receives "Cancel." with a capital and a full stop, or "cancel please", or
    "just cancel". None of those are in the set, so each was accepted as the
    ANSWER instead: a room named "Cancel.", a description of "just cancel"
    (field test 2026-08-29).

    It is the same defect main.py's `_is_undo` already carries a comment
    about -- "a user who says 'undo.' and watches nothing happen has no way
    to tell that a full stop was the problem" -- fixed there for undo alone
    and never generalised. Now it is one function and forty call sites.

    NEGATION IS CHECKED BEFORE THE PADDING IS STRIPPED, and that ordering is
    the point: "don't cancel" and "not that one" both contain a cancel word
    and mean its opposite. Stripping padding without this guard would turn a
    refusal to cancel INTO a cancel -- worse than the bug being fixed, since
    it discards work the person just said they wanted to keep. Same reasoning
    as _is_undo's "'don't undo that' must NOT undo".

    AN ANSWER THAT MERELY CONTAINS A CANCEL WORD IS NOT A CANCEL. "stop the
    music in the kitchen" is a command and "the cancel switch" is a device
    name; only an utterance whose whole content, once padding is removed, IS
    a cancel word counts.
    """
    raw = "" if text is None else str(text)
    low = " ".join(re.sub(r"[^\w\s]", " ", raw.lower()).split())
    if not low:
        return False
    tokens = _cancel_tokens()
    if low in tokens:
        return True

    words = low.split()
    # "do not" always leaves a bare "not" after normalisation, so one set
    # covers every spelling of the negation.
    if any(w in ("dont", "not", "never") for w in words):
        if not low.startswith("never mind") and not low.startswith("nevermind"):
            return False

    core = " ".join(w for w in words if w not in _CANCEL_FILLERS)
    return bool(core) and core in tokens


# ── WHERE ONE COMMAND'S TIME WENT ────────────────────────────────────────
# One dict per command, carried by a context variable -- which asyncio copies
# into every task and to_thread() call that command makes, so the model's own
# code can add to it from a worker thread without being handed anything.
# Outside a command (start-up, the automations thread) there is no dict and
# every call below is a no-op.
_TIMES: "contextvars.ContextVar[Optional[dict]]" = contextvars.ContextVar(
    "hv_times", default=None)


def times_begin() -> dict:
    """Start collecting for the command running in this task."""
    box: dict = {}
    _TIMES.set(box)
    return box


def times_add(stage: str, seconds: float, count: int = 0) -> None:
    box = _TIMES.get()
    if box is None:
        return
    box[stage] = box.get(stage, 0.0) + seconds
    if count:
        box[stage + "#"] = box.get(stage + "#", 0) + count


@contextlib.contextmanager
def timed(stage: str):
    """Time a block into the current command's collector."""
    began = time.perf_counter()
    try:
        yield
    finally:
        times_add(stage, time.perf_counter() - began)


def times_line(box: dict, total: float) -> str:
    """The collector as one line: the stages a person waits through, then
    what the models and the device registry took inside them."""
    def s(key: str) -> str:
        return f"{box.get(key, 0.0):.1f}"
    stages = [(k, box[k]) for k in ("before", "lock", "voice", "scene",
                                    "resolve", "intent", "act")
              if box.get(k, 0.0) >= 0.05]
    known = sum(v for _k, v in stages)
    parts = [f"{k} {v:.1f}" for k, v in stages]
    if total - known >= 0.3:
        parts.append(f"other {total - known:.1f}")
    line = f"total {total:.1f}s = " + (" + ".join(parts) or "under 0.1 everywhere")
    n = box.get("model_run#", 0)
    if n:
        line += (f" | model: {n} sentence(s), waited {s('model_wait')}s for "
                 f"its turn, ran {s('model_run')}s")
    if box.get("registry_wait", 0.0) >= 0.05:
        line += f" | waited {s('registry_wait')}s for the device list"
    if box.get("asked", 0.0) >= 0.05:
        line += f" | {s('asked')}s of it was the person answering"
    return line


def event(tag: str, msg: str) -> None:
    """One timestamped, tagged line on the SERVER console describing what the
    system is doing right now.

    This is the operational record, for whoever is watching the Pi. It is not
    the same channel as IOBackend.write(), which carries text the PERSON needs
    in order to answer something -- and the two were previously tangled, with
    voice scores and device changes going out as ad-hoc prints in whatever
    format each call site happened to pick, or not at all.

    flush=True because this is usually read through a pipe, journald or `less`,
    and a buffered trace that lags the thing it is describing is worse than no
    trace: you watch enrollment appear to hang, then see the log catch up.

    Tags in use:
      CONN AUTH FRAME AUDIO CMD ENROLL VOICE DEVICE SCENE RESULT TIMING
    """
    print(f"[{time.strftime('%H:%M:%S')}] {tag:<7} {msg}", flush=True)


class CancelledByUser(Exception):
    pass


def new_paho_client():
    """A bare paho client with the VERSION1 callback API, so on_connect is
    (client, userdata, flags, rc) with an int rc. paho 2.x refuses a
    Client() without the version; paho 1.x (Debian's python3-paho-mqtt)
    has no CallbackAPIVersion and takes no argument."""
    import paho.mqtt.client as mqtt
    try:
        return mqtt.Client(mqtt.CallbackAPIVersion.VERSION1)
    except AttributeError:                         # paho 1.x
        return mqtt.Client()

# ── what a person may be called ───────────────────────────────────────────
#
# ONE RULE, IN ONE PLACE (2026-09-14: "allow any name writeup because
# spacebar was rejected during name change").
#
# There were two rules, written out as two separate literals -- the
# enrolment door's `r"[A-Za-z0-9._-]{1,64}"` and the rename door's
# `USER_ID_RE`, the same characters typed twice. Neither allowed a SPACE, so
# nobody on this hub could be called "Augusta Chinenye". A person's name is
# the one field in the whole system that is theirs, and it was the most
# restricted thing in it.
#
# WHY IT WAS EVER THAT NARROW, and why widening it is safe: the comment at
# the enrolment door says "if the store is file-backed, an unvalidated one is
# a path". That was worth being careful about, and it is not what happens. A
# user id is a KEY IN A JSON OBJECT in every store that holds one --
# `biometric_store.voiceprints[user]`, `pin_auth.pin_db[user]`,
# `phones.json`'s `user_id` field, the people journal -- and an in-memory
# session key (`f"enroll_{user_id}"`). Grepped: no store builds a filename
# from it. A space is not a path separator, and JSON keys take any text.
#
# So the rule below refuses what is actually dangerous or ambiguous rather
# than everything that is not a letter:
#
#   - path separators and the parent directory, so that if any store ever
#     DOES go file-per-user the answer is already no
#   - control characters, and the invisible formatting characters that make
#     two different names render identically (a zero-width space is the
#     oldest impersonation trick there is, and these names gate a PIN)
#   - "__multi_", already reserved by the enrolment session keys
#   - anything that is only whitespace
#
# and NORMALISES the rest: outer whitespace stripped, internal runs collapsed
# to one space, so "Augusta  Chinenye " and "Augusta Chinenye" cannot become
# two different people with two different PINs.

#: Reserved by enrollment's session keys (main.menu_enroll_person).
RESERVED_NAME_TOKEN = "__multi_"

#: Path separators and the parent directory. Refused so that a store which
#: later becomes file-per-user inherits a name that was already safe.
_NAME_PATH_CHARS = frozenset('/\\:\x00')

NAME_MAX = 64


def normalise_person_name(raw) -> str:
    """The one spelling of a name this hub stores.

    Outer whitespace stripped, every internal run of whitespace collapsed to
    a single space. Returns "" for anything that is only whitespace. Applied
    BEFORE validation and before any store is keyed, so that the name a
    person types and the name the hub holds are the same string.
    """
    return " ".join(str(raw or "").split())


def person_name_problem(raw) -> str:
    """Why this name cannot be used, or "" if it can.

    A sentence fit to show the person, not an error code -- every caller
    here puts it straight on their screen.
    """
    name = normalise_person_name(raw)
    if not name:
        return "A name cannot be empty."
    if len(name) > NAME_MAX:
        return f"A name can be at most {NAME_MAX} characters."
    if RESERVED_NAME_TOKEN in name:
        return f"A name cannot contain {RESERVED_NAME_TOKEN!r} -- it is reserved."
    if any(ch in _NAME_PATH_CHARS for ch in name):
        return "A name cannot contain / \\ or :"
    if ".." in name:
        return "A name cannot contain '..'"
    # The Unicode categories: Cc is every control character (C0, DEL and
    # the C1 range 0x80-0x9f) and Cf every invisible formatter -- the
    # zero-width and bidirectional marks that render as nothing, so two
    # visibly identical names would be two different keys, U+061C ARABIC
    # LETTER MARK, U+00AD SOFT HYPHEN, ...
    if any(unicodedata.category(ch) in ("Cc", "Cf") for ch in name):
        return ("A name cannot contain hidden or control characters -- two "
                "names that look the same must not be two different people.")
    return ""
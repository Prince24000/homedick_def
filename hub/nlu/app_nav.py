"""app_nav.py -- spoken navigation of the PHONE APP, decided on the hub.

WHAT THIS IS. "Open automations", "go back", "scroll down", "select kitchen
night": words about the SCREEN, not about the house. The hub knows the
lexicon and the fuzzy matching; the app knows what is on screen. So the
app sends a small `ui` context with every command frame (which screen it is
on and the labels of the things voice may pick), this module decides
whether the utterance is a navigation phrase, and the app carries out the
resulting `ui` frame against its own allowlist. Nothing here touches a
device, and nothing here can: the outcome is a frame, not an action.

WHAT IT MUST NEVER DO is steal a house command. That shapes every rule:

  * A phrase counts only when it is ENTIRELY navigation: a verb from the
    small sets below, then a screen name, a direction, or a label that is
    on screen -- and nothing else. "Open the garage" names no screen and
    no label, so it falls through to the device path untouched.

  * A label that is ALSO a device name ("kitchen light" on the Devices
    screen) is picked only by a SELECT verb ("select", "pick", "tap",
    "details of"). "Open the kitchen light" and "show me the kitchen light"
    keep their house meanings -- one drives a cover, the other is a status
    query -- so those verbs are refused for device-named labels.

  * No `ui` context, no navigation. An older app sends none and nothing
    changes for it.

  * The context is DATA FROM THE PHONE and is validated as such: a bounded
    number of targets, bounded label lengths, strings only. A label is
    matched, never executed.

Screens, labels, back and scroll are matched literally-with-tolerance, not
semantically: a screen has a name, and the tolerance exists for the
transcriber ("scroll" heard as "scroll", "automation" heard as
"automations"), not for paraphrase. difflib is the stdlib; jellyfish's
metaphone joins in when installed, the same optional dependency the voice
challenge uses.

ACTIONS -- "add a device", "new automation", "learn a remote" -- are the
app's management doors (the hub's MENU_ACTIONS), and they get the same
two-stage treatment a house command gets: a deterministic pass over their
spoken aliases first, then, only if that finds nothing, a semantic pass
with the SAME sentence encoder device matching uses, against a few
prototype phrasings per action. The reason is the one Section 3.5 of the
report gives for devices: "add a device" is said a hundred ways ("register
a new board", "I want to put a new lamp on the app"), and a list of literal
aliases is exactly the fixed-phrase failure the project exists to avoid.
The phone says which actions voice may start (`ui.actions`, its own
allowlist -- an older app sends none and nothing changes), the hub only
ever answers with one of those, and the app then does precisely what a tap
on that row does -- including the PIN/voice gate the hub puts on it. The
semantic pass is fenced so it cannot take a house command: the utterance
must name no device, carry no number and no on/off, be short, clear the
score floor, and beat every other action by a margin. Until this existed,
"add device" fell through to the house path, where the lexicon reads "add"
as "increase by ten per cent" (it is a comparative there), and the answer
was "No device matched" -- or, with a device recently touched, a nudge to
its level.
"""
from __future__ import annotations

import difflib
import string
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple

from nlu.lexicon import SWITCH_PARTICLES, fold_apostrophes

try:  # the same optional dependency voice_security/challenge.py uses
    import jellyfish as _jf  # type: ignore
    _metaphone = getattr(_jf, "metaphone", None)
except Exception:  # noqa: BLE001
    _metaphone = None

# ── Screens ──────────────────────────────────────────────────────────────
#
# id -> the words people say for it. The id is what the app receives; the
# app maps it to a route (or to the menu action that opens the screen) on
# its own allowlist, so an id the app does not know is simply ignored.
# No "front", "start", "front page" or "start screen" for home: "page" and
# "screen" are filler, so those aliases reduced to the bare word, and "open
# the front" (a clipped "open the front door") and "start" navigated home.
SCREENS: Dict[str, Tuple[str, ...]] = {
    "home": ("home", "home screen", "main screen", "main page", "dashboard"),
    "devices": ("devices", "device", "device list", "my devices",
                "the devices", "boards", "device screen"),
    "scenes": ("scenes", "scene", "scene list", "my scenes"),
    "automations": ("automations", "automation", "rules", "automation list",
                    "my automations", "my rules", "routines"),
    "notifications": ("notifications", "notification", "notices", "alerts",
                      "messages", "inbox"),
    "stats": ("statistics", "stats", "readings", "sensors", "sensor readings",
              "measurements", "graphs", "charts"),
    "profile": ("profile", "my profile", "my account", "account"),
    "settings": ("settings", "setting", "options", "preferences",
                 "configuration"),
}

#: What is said for a screen whose id is not its spoken name.
_SCREEN_SAY: dict = {}

# ── Verbs ────────────────────────────────────────────────────────────────
#
# Multi-word verbs are listed longest first so "go to" is tried before "go".
# Verbs outside _LOOSE_SELECT ("check", "back to", "go") open a SCREEN and
# nothing else, so "check the kitchen light" stays a status query.
OPEN_VERBS: Tuple[str, ...] = (
    "take me to", "bring me to", "navigate to", "switch to", "go to",
    "jump to", "bring up", "show me the", "show me", "show the", "show",
    "open the", "open up", "open", "goto", "view",
    "go back to", "back to", "return to", "head to", "get me to", "go into",
    "move to", "check", "look at", "see", "display", "launch", "go",
    "press the", "press",
)
SELECT_VERBS: Tuple[str, ...] = (
    "details of", "details for", "select the", "select", "pick the", "pick",
    "choose the", "choose", "tap the", "tap on", "tap",
    "click the", "click on", "click", "edit the", "edit",
)
# Select verbs that are ALSO house verbs are not in the list above on purpose
# ("open", "show", "press" -- a Home Assistant button is pressed); they may
# select only labels that are not device names.
_LOOSE_SELECT: Tuple[str, ...] = ("open the", "open", "show me the", "show me",
                                  "show the", "show", "view", "go to",
                                  "press the", "press")
# The loose verbs that ask to SEE something: the only ones that may pick a
# camera by its bare name. "Open the front door" is the door, never the
# camera that happens to share its name; "show me the front door" is the feed.
_SHOW_VERBS: Tuple[str, ...] = ("show me the", "show me", "show the", "show",
                                "view")
# Words that say "the camera" rather than name one: "open the front door
# camera", "show me the backyard feed".
CAMERA_WORDS: Set[str] = {"camera", "cameras", "cam", "feed", "video",
                          "stream", "live"}

BACK_PHRASES: Tuple[str, ...] = (
    "go back", "back", "previous screen", "previous page", "return",
    "take me back", "go back one", "one back",
)
HOME_PHRASES: Tuple[str, ...] = (
    "go home", "home", "home screen", "main screen", "take me home",
    "back to home", "back to the home screen", "back home", "start screen",
)
SCROLL_WORDS: Tuple[str, ...] = ("scroll", "page", "swipe")
# How the transcriber hears "scroll" when it does not: none of these means
# anything to the house, so taking them costs no command.
SCROLL_MISHEARD: Set[str] = {"school", "scrawl", "stroll", "scroller"}
SCROLL_DIRECTIONS: Dict[str, str] = {
    "down": "down", "downwards": "down", "further": "down",
    "up": "up", "upwards": "up",
    "top": "top", "to the top": "top", "to top": "top", "beginning": "top",
    "bottom": "bottom", "to the bottom": "bottom", "to bottom": "bottom",
    "end": "bottom", "to the end": "bottom",
}
# "Scroll down a bit": trailing amounts that change nothing. Without them the
# phrase fell through to the house path, where "down a little" is a dimmer.
_SCROLL_TAIL: Tuple[str, ...] = ("a little bit", "a little", "a bit", "a lot",
                                 "some more", "more", "some", "again")
# Whole phrases that scroll without a scroll word.
SCROLL_PHRASES: Dict[str, str] = {
    "go to the top": "top", "go to top": "top", "back to the top": "top",
    "back to top": "top", "jump to the top": "top", "top of the page": "top",
    "top of the list": "top",
    "go to the bottom": "bottom", "go to bottom": "bottom",
    "jump to the bottom": "bottom", "bottom of the page": "bottom",
    "bottom of the list": "bottom",
    "next page": "down",
}

# ── Actions ──────────────────────────────────────────────────────────────
#
# id -> the words people say for it. The ids are the hub's MENU_ACTIONS
# keys; the app decides which of them voice may start (it sends the list),
# and does for a match exactly what a tap on that row does. Listing screens
# are not here -- "devices", "scenes", "statistics" are SCREENS above and
# open the same way a drawer row does.
ACTIONS: Dict[str, Tuple[str, ...]] = {
    "add_device": ("add device", "add a device", "add new device",
                   "add a new device", "new device", "register a device",
                   "register device", "register a new device", "add a board",
                   "add board", "add a new board", "set up a device",
                   "set up a board",
                   "set up a new board", "add an appliance", "add appliance",
                   "install a device", "pair a device", "connect a device"),
    "update_device": ("update device", "update a device", "edit device",
                      "edit a device", "change a device", "change device",
                      "rename a device", "rename device", "modify a device"),
    "remove_device": ("remove device", "remove a device", "delete device",
                      "delete a device", "forget a device", "unregister a device",
                      "retire a board", "remove a board"),
    "add_scene": ("add scene", "add a scene", "add new scene", "new scene",
                  "create a scene", "create scene", "make a scene",
                  "make a new scene", "set up a scene"),
    "update_scene": ("update scene", "update a scene", "edit scene",
                     "edit a scene", "change a scene", "modify a scene",
                     "rename a scene"),
    "remove_scene": ("remove scene", "remove a scene", "delete scene",
                     "delete a scene", "forget a scene"),
    "add_automation": ("add automation", "add an automation", "new automation",
                       "create an automation", "create automation",
                       "make an automation", "add a rule", "new rule",
                       "create a rule", "make a rule", "add a routine",
                       "new routine", "create a routine", "set up an automation"),
    "update_automation": ("update automation", "update an automation",
                          "edit automation", "edit an automation",
                          "change an automation", "edit a rule", "change a rule",
                          "update a rule", "edit a routine"),
    "remove_automation": ("remove automation", "remove an automation",
                          "delete automation", "delete an automation",
                          "remove a rule", "delete a rule", "remove a routine",
                          "delete a routine"),
    "learn_ir": ("learn ir codes", "learn ir", "learn ir code", "learn a remote",
                 "learn remote codes", "learn the remote", "teach a remote",
                 "learn infrared codes", "learn an infrared code",
                 "record a remote", "copy a remote"),
}
# Extra phrasings for the semantic pass only -- fuller sentences a person
# says, which the literal aliases above would never cover. Deliberately
# about the ACT of registering or editing, never about a kind of device
# ("light", "lamp"), so a house command about a light has nothing here to
# sit close to.
ACTION_PROTOTYPES: Dict[str, Tuple[str, ...]] = {
    "add_device": ("i want to add a new device to the app",
                   "register a new device with the hub",
                   "set up something new i just wired",
                   "put a new device on the system",
                   "i have a new board to add",
                   "add something to my devices"),
    "update_device": ("i want to change one of my devices",
                      "edit the details of a device",
                      "rename one of the devices",
                      "update the settings of a device"),
    "remove_device": ("take a device off the system",
                      "i want to get rid of a device",
                      "delete one of my devices",
                      "remove a board i no longer use"),
    "add_scene": ("i want to make a new scene",
                  "set up a scene for the evening",
                  "create a new scene for the house"),
    "update_scene": ("change one of my scenes",
                     "edit what a scene does"),
    "remove_scene": ("get rid of a scene",
                     "delete one of my scenes"),
    "add_automation": ("i want to make a new rule for the house",
                       "set up an automation that runs on its own",
                       "create a rule that triggers something",
                       "make the house do something automatically"),
    "update_automation": ("change one of my automations",
                          "edit a rule i made"),
    "remove_automation": ("get rid of an automation",
                          "delete one of my rules"),
    "learn_ir": ("teach the hub the buttons on my remote",
                 "record the codes from an infrared remote",
                 "learn the codes of my tv remote"),
}
ACTION_LABELS: Dict[str, str] = {
    "add_device": "Add device", "update_device": "Update device",
    "remove_device": "Remove device", "add_scene": "Add scene",
    "update_scene": "Update scene", "remove_scene": "Remove scene",
    "add_automation": "Add automation", "update_automation": "Update automation",
    "remove_automation": "Remove automation", "learn_ir": "Learn IR codes",
}
# Politeness and intent lead-ins, stripped from the front before an action
# alias is matched: "can you add a device", "i'd like to add a device".
# _tokens turns an apostrophe into a space, so "i'd" arrives as "i d".
# Longest first, so "i want to" is stripped whole rather than as "i want".
_LEAD_INS: Tuple[str, ...] = tuple(sorted((
    "i would like to", "i d like to", "i want to", "i need to", "could you",
    "can you", "would you", "will you", "let me", "let us", "help me",
    "lets", "please", "i wanna", "i want", "okay", "ok", "um", "uh", "so",
    "and", "now", "just",
), key=lambda l: -len(l.split())))
# Politeness at the END: "go back please", "open scenes thanks".
_TAILS: Tuple[str, ...] = ("thank you", "thanks", "please", "now", "for me")
# The wake phrase ("hey home") heard again at the front of the command. It
# is stripped only as a whole and only with words after it: "hey home" on
# its own must not read as "home".
_WAKE_ECHO_FIRST: Set[str] = {"hey", "hi", "ok", "okay", "a"}
MAX_ACTIONS = 40

# Words that carry nothing and are dropped before matching a label or a
# screen name. Deliberately short: a long filler list is how "the kitchen
# light" and "the kitchen" start looking the same.
_FILLER: Set[str] = {"the", "a", "an", "my", "to", "screen", "page", "tab",
                     "list", "please", "now", "of", "for", "on", "into",
                     "menu", "section", "view", "one"}

# Limits on the context the phone sends. Data from a device is validated
# as data, however friendly the device.
MAX_TARGETS = 200
MAX_LABEL_LEN = 80
MAX_TEXT_LEN = 200

_PUNCT = string.punctuation + "“”‘’"


def _tokens(text: str) -> List[str]:
    # Every apostrophe, not only the straight one: "i’d like to add a
    # device" from a transcriber kept "i’d" whole, no lead-in matched, and
    # the action was missed.
    return [w for w in (t.strip(_PUNCT)
                        for t in fold_apostrophes(text.lower(), " ").split()) if w]


def _same_word(a: str, b: str) -> bool:
    """One transcribed word against one expected word, with tolerance for
    the transcriber, not for paraphrase."""
    if a == b:
        return True
    if len(a) >= 4 and len(b) >= 4:
        # automations/automation, notices/notice, scenes/scene
        if a.startswith(b) or b.startswith(a):
            return abs(len(a) - len(b)) <= 2
        if difflib.SequenceMatcher(None, a, b).ratio() >= 0.84:
            return True
        if _metaphone is not None:
            try:
                return bool(_metaphone(a)) and _metaphone(a) == _metaphone(b)
            except Exception:  # noqa: BLE001
                return False
    return False


def _starts_with(words: Sequence[str], expected: Sequence[str]) -> bool:
    """`words` begins with every word of `expected`, in order."""
    return len(words) >= len(expected) and all(
        _same_word(w, e) for w, e in zip(words[:len(expected)], expected, strict=True))


def _phrase_matches(words: Sequence[str], phrase: str) -> bool:
    """Every word of `phrase` matches, in order, exactly covering `words`."""
    expected = phrase.split()
    return len(expected) == len(words) and _starts_with(words, expected)


def _says_alias(core: Sequence[str], alias: str) -> bool:
    """`core` (filler removed) is `alias` with its filler removed."""
    return _phrase_matches(core, " ".join(_strip_filler(alias.split())) or alias)


def _strip_filler(words: Sequence[str]) -> List[str]:
    return [w for w in words if w not in _FILLER]


def _leading_verb(words: List[str], verbs: Sequence[str]) -> Optional[Tuple[str, List[str]]]:
    """The longest verb at the front of `words`, and what follows it."""
    best: Optional[Tuple[str, List[str]]] = None
    for verb in verbs:
        vw = verb.split()
        if _starts_with(words, vw):
            if best is None or len(vw) > len(best[0].split()):
                best = (verb, words[len(vw):])
    return best


def _screen_for(words: Sequence[str]) -> Optional[str]:
    """A screen id when `words` (filler already removed) name one."""
    core = _strip_filler(words)
    if not core:
        return None
    for sid, aliases in SCREENS.items():
        if any(_says_alias(core, alias) for alias in aliases):
            return sid
    return None


def _label_score(words: Sequence[str], label: str, kind: str = "") -> float:
    """How completely `words` say `label`. 1.0 is every label word matched
    in order with nothing left over; anything under 1.0 needs the label to
    be the clear best and still have nothing left over.

    For a camera, "camera"/"feed" on either side is not part of the name:
    "the front door camera" says the camera labelled "Front Door", and
    "front door" says the one labelled "Front Door Camera"."""
    lw = _strip_filler(_tokens(label))
    sw = _strip_filler(words)
    if kind == "camera":
        sw = [w for w in sw if w not in CAMERA_WORDS]
        lw = [w for w in lw if w not in CAMERA_WORDS] or lw
    if not lw or not sw:
        return 0.0
    if len(sw) > len(lw):
        return 0.0                      # words left over: not this label
    matched = 0
    li = 0
    for w in sw:
        while li < len(lw) and not _same_word(w, lw[li]):
            li += 1
        if li >= len(lw):
            return 0.0                  # a said word matches nothing, in order
        matched += 1
        li += 1
    return matched / len(lw)


def _best_target(words: Sequence[str], targets: Sequence[Dict[str, str]]
                 ) -> Optional[Dict[str, str]]:
    scored: List[Tuple[float, Dict[str, str]]] = []
    for t in targets:
        s = _label_score(words, t["label"], t.get("kind", ""))
        if s > 0:
            scored.append((s, t))
    if not scored:
        return None
    scored.sort(key=lambda x: -x[0])
    top, tgt = scored[0]
    # A partial say ("kitchen" for "kitchen night") is accepted only when it
    # is unambiguous and covers most of the label.
    if top < 1.0:
        if top < 0.5:
            return None
        if len(scored) > 1 and scored[1][0] >= top:
            return None
    elif len(scored) > 1 and scored[1][0] >= 1.0:
        return None                     # two labels said exactly: ask by tapping
    return tgt


def _strip_lead_ins(words: List[str]) -> List[str]:
    """Drop every leading lead-in phrase ("please", "can you", ...)."""
    out = list(words)
    changed = True
    while changed and out:
        changed = False
        for lead in _LEAD_INS:
            lw = lead.split()
            if len(out) > len(lw) and _starts_with(out, lw):
                out = out[len(lw):]
                changed = True
                break
    return out


def _action_for(words: Sequence[str], allowed: Sequence[str]) -> Optional[str]:
    """An action id when `words`, minus lead-ins and filler, say one of its
    aliases exactly (with the transcriber tolerance every screen name gets)."""
    core = _strip_filler(_strip_lead_ins(list(words)))
    if not core:
        return None
    for aid in allowed:
        if any(_says_alias(core, alias) for alias in ACTIONS.get(aid, ())):
            return aid
    # "Create a NEW automation", "make a new rule": the alias list spells
    # "new" in some places and not others. Compared again with it dropped on
    # both sides -- but only where a verb is left beside the noun, so a bare
    # "device" never becomes "new device".
    bare = [w for w in core if w != "new"]
    if len(bare) >= 2 and len(bare) != len(core):
        for aid in allowed:
            for alias in ACTIONS.get(aid, ()):
                aw = [w for w in _strip_filler(alias.split()) if w != "new"]
                if len(aw) >= 2 and _phrase_matches(bare, " ".join(aw)):
                    return aid
    return None


def _normalize(words: List[str]) -> Optional[List[str]]:
    """The utterance without what carries nothing for navigation: the wake
    phrase heard again ("hey home, go back"), lead-ins ("can you", "okay",
    "please") and politeness at the end ("thanks"). None when nothing but
    the wake phrase was said."""
    out = list(words)
    if len(out) >= 2 and out[0] in _WAKE_ECHO_FIRST and out[1] == "home":
        out = out[2:]
        if not out:
            return None
    out = _strip_lead_ins(out)
    changed = True
    while changed and len(out) > 1:
        changed = False
        for tail in _TAILS:
            tw = tail.split()
            if len(out) > len(tw) and out[-len(tw):] == tw:
                out = out[:-len(tw)]
                changed = True
                break
    return out


_ARTICLES: Set[str] = {"the", "a", "an", "my"}


def _is_house_phrase(words: Sequence[str], phrases: Sequence[str]) -> bool:
    """The whole utterance IS something the house answers to by name -- a
    scene's name or trigger, a device's name. Those belong to the house
    even when they sound like the app ("home", "alerts")."""
    # Articles only: the navigation filler also drops "view", "screen" and
    # "list", which made "view the backyard camera" equal the camera's name.
    core = [w for w in words if w not in _ARTICLES]
    if not core:
        return False
    for p in phrases:
        pw = [w for w in _tokens(p) if w not in _ARTICLES]
        if pw and pw == core:
            return True
    return False


def _camera_allowed(verb: str, rest: Sequence[str], target: Dict[str, str],
                    names: Sequence[str], ctx_targets: Sequence[Dict[str, str]]) -> bool:
    """May a LOOSE verb pick this camera? Yes when the words say "camera"
    ("open the front door camera"). Without that word only a verb that asks
    to SEE may, and only when no other device answers to the same name:
    "open the front door" is the lock or the cover, never the camera that
    shares its name, and "show me the front door" is the lock's state when
    a lock is called that too."""
    if any(w in CAMERA_WORDS for w in rest):
        return True
    if verb not in _SHOW_VERBS:
        return False
    label = " ".join(_strip_filler(_tokens(target["label"])))
    said = " ".join(_strip_filler(list(rest)))
    same_label_cams = sum(
        1 for t in ctx_targets if t.get("kind") == "camera"
        and " ".join(_strip_filler(_tokens(t["label"]))) == label)
    for_label = sum(1 for n in names
                    if " ".join(_strip_filler(_tokens(n))) == label)
    if for_label > same_label_cams:
        return False                   # another device shares the camera's name
    cam_labels = {" ".join(_strip_filler(_tokens(t["label"])))
                  for t in ctx_targets if t.get("kind") == "camera"}
    return not any(" ".join(_strip_filler(_tokens(n))) == said
                   and said not in cam_labels for n in names)


def _mentions_device(words: Sequence[str], device_names: Set[str]) -> bool:
    """True when a device's name (filler removed) appears, in order and
    contiguously, in `words`. The fence that keeps the semantic action pass
    off anything that names something in the house."""
    sw = list(words)
    for name in device_names:
        nw = _strip_filler(_tokens(name))
        if not nw or len(nw) > len(sw):
            continue
        if any(_starts_with(sw[i:], nw) for i in range(len(sw) - len(nw) + 1)):
            return True
    return False


def _looks_like_house_command(words: Sequence[str]) -> bool:
    """A number, a percentage or an on/off particle is a value being set,
    which no app action carries."""
    for w in words:
        if w in SWITCH_PARTICLES:
            return True
        if any(ch.isdigit() for ch in w) or w.endswith("%"):
            return True
    return False


# The semantic pass's prototype vectors, embedded once: model_module loads one
# model per process and refuses to switch.
_SEM_CACHE: Dict[str, Any] = {"ids": [], "vecs": None}


def warm_semantic() -> bool:
    """Embed the navigation prototypes NOW, off the request path.

    The cache above used to fill on the first utterance that reached the
    semantic pass -- and on a laptop that first embedding of every
    prototype took about 85 seconds (field log 2026-09-30: "The righteous"
    sat for 85 s before the hub answered, and Cancel had nothing to
    interrupt). Called once from a background thread at start-up, so no
    person ever pays for it. True when the cache is ready."""
    try:
        _action_semantic("open settings", ())
    except Exception:                                      # noqa: BLE001
        return False
    return _SEM_CACHE["vecs"] is not None


def _action_semantic(text: str, allowed: Sequence[str]
                     ) -> Optional[Tuple[str, float, float]]:
    """(action id, score, margin) for the best action by cosine similarity
    against the prototypes, or None -- including None whenever the encoder
    is not loaded, so a hub without the model simply has no semantic pass.

    The winner must be one of `allowed`, clear APP_ACTION_MIN_SCORE, and beat
    the best OTHER action (allowed or not) by APP_ACTION_MIN_MARGIN: an
    action the phone cannot start that still scores higher means the words
    meant something this pass should not decide."""
    try:
        from nlu import model_module as mm
        import numpy as np
        from core.config import APP_ACTION_MIN_SCORE, APP_ACTION_MIN_MARGIN
        mm.get_backend()                    # raises when no model is loaded
    except Exception:                        # noqa: BLE001
        return None
    try:
        if _SEM_CACHE["vecs"] is None:
            ids: List[str] = []
            texts: List[str] = []
            for aid in ACTIONS:
                for phrase in tuple(ACTIONS[aid]) + tuple(ACTION_PROTOTYPES.get(aid, ())):
                    ids.append(aid)
                    texts.append(phrase)
            vecs = np.asarray(mm.embed_batch(texts), dtype="float32")
            norms = np.linalg.norm(vecs, axis=1, keepdims=True)
            norms[norms < 1e-9] = 1.0
            _SEM_CACHE.update(ids=ids, vecs=vecs / norms)
        q = np.asarray(mm.embed(text.lower().strip()), dtype="float32")
        qn = float(np.linalg.norm(q))
        if qn < 1e-9:
            return None
        sims = _SEM_CACHE["vecs"] @ (q / qn)
        best: Dict[str, float] = {}
        for aid, sim in zip(_SEM_CACHE["ids"], sims, strict=True):
            if float(sim) > best.get(aid, float("-inf")):
                best[aid] = float(sim)
        ranked = sorted(best.items(), key=lambda kv: -kv[1])
        top_id, top = ranked[0]
        second = ranked[1][1] if len(ranked) > 1 else float("-inf")
        margin = top - second if second != float("-inf") else top
        if top_id not in allowed or top < APP_ACTION_MIN_SCORE \
                or margin < APP_ACTION_MIN_MARGIN:
            return None
        return top_id, top, margin
    except Exception:                        # noqa: BLE001
        return None


def validate_ui(ui: Any) -> Optional[Dict[str, Any]]:
    """The phone's `ui` context, checked as data. None when absent or
    malformed -- and then there is no navigation at all."""
    if not isinstance(ui, dict):
        return None
    screen = ui.get("screen")
    if not isinstance(screen, str) or not (1 <= len(screen) <= 40):
        return None
    # A screen with nothing to pick may send "targets": null -- that is no
    # targets, not a malformed context that turns back/home/scroll off too.
    raw = ui.get("targets") or []
    if not isinstance(raw, list):
        return None
    targets: List[Dict[str, str]] = []
    for item in raw[:MAX_TARGETS]:
        if not isinstance(item, dict):
            continue
        tid = item.get("id")
        label = item.get("label")
        if not isinstance(tid, str) or not isinstance(label, str):
            continue
        tid = tid.strip()
        label = label.strip()
        if not tid or not label or len(tid) > MAX_LABEL_LEN \
                or len(label) > MAX_LABEL_LEN:
            continue
        kind = item.get("kind")
        targets.append({"id": tid, "label": label,
                        "kind": kind if isinstance(kind, str) else ""})
    # The actions voice may start, by id, as the phone's own allowlist. Only
    # ids this module knows survive: anything else is a name the hub would
    # never send back anyway.
    raw_actions = ui.get("actions") or []
    actions: List[str] = []
    if isinstance(raw_actions, list):
        for item in raw_actions[:MAX_ACTIONS]:
            if isinstance(item, str) and item.strip() in ACTIONS \
                    and item.strip() not in actions:
                actions.append(item.strip())
    return {"screen": screen, "targets": targets, "actions": actions}


def match_navigation(text: str, ui: Any,
                     device_names: Optional[Sequence[str]] = None,
                     house_phrases: Optional[Sequence[str]] = None,
                     ) -> Optional[Dict[str, Any]]:
    """The `ui` frame for `text`, or None when it is not navigation.

    `ui` is the phone's context (see validate_ui). `device_names` are the
    house's device names: a target label equal to one is picked only by an
    explicit select verb, so house verbs keep their house meaning.
    `house_phrases` are the other things the house answers to by name --
    scene names and triggers: an utterance that IS one of them (or IS a
    device name) is never navigation, however much it sounds like the app
    (a scene triggered by "home")."""
    ctx = validate_ui(ui)
    if ctx is None:
        return None
    if not isinstance(text, str) or not text.strip() \
            or len(text) > MAX_TEXT_LEN:
        return None
    raw_words = _tokens(text)
    if not raw_words or len(raw_words) > 12:
        return None
    names = [n for n in (device_names or ()) if isinstance(n, str)]
    dev = {n.lower().strip() for n in names}
    # A said name belongs to the house, checked on the words as said AND as
    # normalised ("the movie time please").
    owned = list(names) + [p for p in (house_phrases or ()) if isinstance(p, str)]
    if _is_house_phrase(raw_words, owned):
        return None
    words = _normalize(raw_words)
    if not words or len(words) > 10:
        return None
    if _is_house_phrase(words, owned):
        return None

    # 1. Back / home: whole-utterance phrases.
    for phrase in BACK_PHRASES:
        if _phrase_matches(words, phrase):
            return {"type": "ui", "action": "back", "say": "back"}
    for phrase in HOME_PHRASES:
        if _phrase_matches(words, phrase):
            return {"type": "ui", "action": "open", "screen": "home",
                    "say": "home"}

    # 2. Scroll: "<scroll word> <direction> [a bit]", "scroll to the
    #    bottom", "go to the top", "next page".
    for phrase, direction in SCROLL_PHRASES.items():
        if _phrase_matches(words, phrase):
            return {"type": "ui", "action": "scroll",
                    "direction": direction, "say": f"scroll {direction}"}
    if len(words) >= 2 and (words[0] in SCROLL_MISHEARD or any(
            _same_word(words[0], s) for s in SCROLL_WORDS)):
        tail = list(words[1:])
        for amount in _SCROLL_TAIL:
            aw = amount.split()
            if len(tail) > len(aw) and tail[-len(aw):] == aw:
                tail = tail[:-len(aw)]
                break
        for key, direction in sorted(SCROLL_DIRECTIONS.items(),
                                     key=lambda kv: -len(kv[0])):
            if _phrase_matches(tail, key):
                return {"type": "ui", "action": "scroll",
                        "direction": direction, "say": f"scroll {direction}"}

    def _select(target: Dict[str, str]) -> Dict[str, Any]:
        return {"type": "ui", "action": "select", "target": target["id"],
                "label": target["label"], "kind": target["kind"],
                "say": target["label"]}

    def _only_camera(rest: Sequence[str]) -> Optional[Dict[str, str]]:
        """ "the camera" with exactly one camera on offer."""
        core = _strip_filler(rest)
        if not core or any(w not in CAMERA_WORDS for w in core):
            return None
        cams = [t for t in ctx["targets"] if t.get("kind") == "camera"]
        return cams[0] if len(cams) == 1 else None

    # 3. Select a visible label: explicit select verbs first.
    hit = _leading_verb(words, SELECT_VERBS)
    if hit:
        rest = hit[1]
        target = (_best_target(rest, ctx["targets"]) or _only_camera(rest)) \
            if rest else None
        if target:
            return _select(target)
        # An explicit select verb with no matching label is still not a
        # house command; but returning None lets the NLU say "no match"
        # in its own words rather than inventing one here.

    # 4. Open a screen -- or, with a loose verb, a label that is not a
    #    device name.
    hit = _leading_verb(words, OPEN_VERBS)
    if hit:
        verb, rest = hit
        screen = _screen_for(rest) if rest else None
        if screen:
            return {"type": "ui", "action": "open", "screen": screen,
                    "say": _SCREEN_SAY.get(screen, screen)}
        if rest and verb in _LOOSE_SELECT and ctx["targets"]:
            target = _best_target(rest, ctx["targets"])
            if target is None:
                target = _only_camera(rest)
            # A CAMERA IS THE EXCEPTION to the device rule: "show me the
            # front camera" means its feed. But only when the words say
            # "camera", or ask to SEE a name no other device answers to --
            # "open the front door" is the door (see _camera_allowed).
            if target and target.get("kind") == "camera":
                if _camera_allowed(verb, rest, target, names, ctx["targets"]):
                    return _select(target)
                return None
            if target and target["label"].lower().strip() not in dev \
                    and target.get("kind") != "device":
                return _select(target)
        # No screen and no label: not navigation by THIS verb. Fall through
        # to the LITERAL action aliases only ("show add device" is still
        # the action); the semantic pass never sees an "open the ..." --
        # that is a house sentence far more often than an app one.
        led_by_open_verb = True
    else:
        led_by_open_verb = False
        # 5. A bare screen name ("automations", "settings") with nothing else.
        screen = _screen_for(words)
        if screen and len(_strip_filler(words)) <= 3:
            return {"type": "ui", "action": "open", "screen": screen,
                    "say": _SCREEN_SAY.get(screen, screen)}

    # 6. An app action, by its spoken aliases: "add a device", "please
    #    create a new rule". Only actions the phone said it can start.
    allowed = ctx["actions"]
    if allowed and not _looks_like_house_command(words):
        aid = _action_for(words, allowed)
        if aid:
            return {"type": "ui", "action": "menu", "menu": aid,
                    "say": ACTION_LABELS.get(aid, aid.replace("_", " "))}

        # 7. The same, semantically -- the one paraphrase-tolerant stage in
        #    this module, and fenced: nothing that names a device, sets a
        #    value or switches anything gets this far, and the encoder has
        #    to be both sure (score) and unambiguous (margin).
        if len(words) <= 8 and not led_by_open_verb \
                and not _mentions_device(words, dev):
            hit = _action_semantic(" ".join(words), allowed)
            if hit:
                aid, score, margin = hit
                return {"type": "ui", "action": "menu", "menu": aid,
                        "say": ACTION_LABELS.get(aid, aid.replace("_", " ")),
                        "via": "semantic", "score": round(score, 3),
                        "margin": round(margin, 3)}
    return None

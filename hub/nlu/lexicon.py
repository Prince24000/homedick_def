"""
lexicon.py — the single English word-authority for the whole pipeline.

Data module. No extraction logic, no resolution logic. Every open-class word
the system recognises lives here, and every other module imports rather than
respells: slot_extractor, resolver, executor and main all take their
vocabulary from this file.

Sections 6 and 7 are new arrivals -- constants that had been sitting in
slot_extractor, resolver and main, several of them duplicating something
already defined here. They are grouped by the module that consumes them, not
merged into the older sections, so what moved stays traceable.
"""

import re
from typing import Any, Dict, FrozenSet, Iterable, List, Literal, Optional, Sequence, Set, Tuple


# ═══════════════════════════════════════════════════════════════
# 1. MASTER LEXICON
# ═══════════════════════════════════════════════════════════════

# Two shapes in one dict: the degree categories carry {"magnitude", "words"},
# while "directionals" carries {"increase", "decrease"}. Annotated as Any
# rather than restructured -- the shape difference is real and every consumer
# below already branches on it.
LEXICON: Dict[str, Dict[str, Any]] = {
    "downtoners": {
        "magnitude": 0.05,
        "words": [
            "a bit", "a little", "slightly", "somewhat", "marginally",
            "a touch", "a tad", "kind of", "sort of", "rather",
            "fairly", "relatively", "moderately",
            "a smidge", "a notch", "a hair", "a shade", "a trace",
            "a tiny bit", "a wee bit", "minimally",
            "just a bit", "only a little"
        ]
    },
    "compromisers": {
        "magnitude": 0.08,
        "words": [
            "more or less", "almost", "nearly", "practically", "virtually",
            "basically", "essentially", "roughly", "approximately",
            "about", "around", "close to", "in the ballpark of"
        ]
    },
    "minimizers": {
        "magnitude": 0.02,
        "words": [
            "barely", "hardly", "only just", "by a hair",
            "by a whisker", "at a minimum", "the least", "not at all",
            "next to nothing", "just barely", "hardly at all"
        ]
    },
    "amplifiers": {
        "magnitude": 0.20,
        "words": [
            "much", "way", "a lot", "lots", "plenty", "significantly",
            "considerably", "substantially", "markedly", "noticeably",
            "appreciably", "decidedly", "distinctly", "a good deal",
            "a great deal", "a fair bit", "heaps", "loads", "tons",
            "a ton", "a bunch", "a whole lot", "massively", "hugely",
            "vastly", "immensely", "enormously", "radically", "sharply",
            "drastically", "dramatically", "strikingly", "powerfully"
        ]
    },
    "boosters": {
        "magnitude": 0.15,
        "words": [
            "very", "really", "extremely", "highly", "terribly", "awfully",
            "remarkably", "especially", "particularly", "exceptionally",
            "extraordinarily", "intensely", "strongly", "deeply", "thoroughly",
            "seriously", "majorly", "wildly", "fiercely", "mightily", "quite",
            # "too" (2026-09-12). It was in this file exactly once, in
            # ROOM_STOP_WORDS, and that is the OTHER sense of the word -- "in
            # the kitchen TOO" meaning AS WELL, there only so the room does
            # not come out as "kitchen too". As a degree word it was absent
            # everywhere, so "it's too cold" was parsed as plain "cold" and
            # scored the comparative default of 0.10: the same step as a bare
            # "colder", with the word carrying the complaint contributing
            # nothing. A booster, alongside "very": "too cold" is about as far
            # past comfortable as "very cold", not a maximizer's all-the-way.
            #
            # SAFE IN BOTH SENSES. A booster can only SCALE a direction that
            # is already there -- only maximizers and midpoints may invent one
            # (slot_extractor, "a bare maximizer/midpoint modifier can imply
            # direction") -- so "turn on the lamp too" still carries no delta
            # at all, and room_named still reads "kitchen" out of "in the
            # kitchen too".
            #
            # IT DOES NOT FIX THE DIRECTION, and was not meant to: "it's too
            # cold" still points DOWN, because the direction comes from
            # BASE_ADJECTIVES["cold"] and a complaint names the state it
            # wants ended, not the one it wants more of. This makes that step
            # bigger, not righter. That is a separate decision, deliberately
            # left alone.
            "too",
        ]
    },
    "maximizers": {
        "magnitude": 1.0,
        "words": [
            "max", "maximum", "full", "fully", "completely", "totally",
            "entirely", "absolutely", "utterly", "altogether", "wholly",
            "all the way", "to the max", "top", "highest",
            "lowest", "min", "minimum", "zero", "none",
            "ultimate", "peak",
            "extreme", "total", "absolute", "sheer", "perfect", "maxed out",
            "all out", "100 percent", "one hundred percent", "all in"
        ]
    },
    "midpoints": {
        "magnitude": 0.50,
        "words": [
            "halfway", "mid", "middle", "center", "median"
        ]
    },
    # NOTE on "on"/"off" below: they are listed here because "turn it on" is
    # loosely an increase, but they are ALSO the two bare switch particles, and
    # word_direction() refuses to read a direction from them. See
    # SWITCH_PARTICLES for why.
    "directionals": {
        "increase": [
            "up", "on", "increase", "raise", "boost", "ramp up", "step up",
            "crank up", "pump up", "turn up", "add", "more", "higher",
            "greater", "plus", "advance", "escalate", "intensify", "heighten",
            "uplift", "increment", "augment", "amplify", "magnify", "strengthen",
            "upward", "upwards", "gain", "grow", "expand", "extend", "widen"
        ],
        "decrease": [
            "down", "off", "decrease", "lower", "reduce", "drop", "cut",
            "ramp down", "step down", "crank down", "turn down", "less",
            "subtract", "minus", "diminish", "dampen", "depress", "weaken",
            "lessen", "abate", "decline", "fall", "sink", "dive", "plummet",
            "downward", "downwards", "lose", "shrink", "contract", "narrow"
        ]
    }
}


# ═══════════════════════════════════════════════════════════════
# 2. COMPARATIVE + SUPERLATIVE GENERATOR
# ═══════════════════════════════════════════════════════════════

BASE_ADJECTIVES: Dict[str, Literal["increase", "decrease"]] = {
    "warm": "increase", "hot": "increase",
    "bright": "increase", "light": "increase",
    "loud": "increase", "high": "increase", "fast": "increase",
    "quick": "increase", "strong": "increase", "big": "increase",
    "large": "increase", "wide": "increase", "broad": "increase",
    "close": "increase",
    "tight": "increase", "hard": "increase",
    "sharp": "increase", "clear": "increase", "thick": "increase",
    "deep": "increase", "heavy": "increase", "rough": "increase",
    "firm": "increase", "near": "increase",
    "cool": "decrease", "cold": "decrease", "dim": "decrease",
    "dark": "decrease", "quiet": "decrease", "soft": "decrease",
    "silent": "decrease", "low": "decrease", "slow": "decrease",
    "weak": "decrease", "small": "decrease", "tiny": "decrease",
    "narrow": "decrease", "thin": "decrease", "far": "decrease",
    "loose": "decrease", "gentle": "decrease", "easy": "decrease",
    "smooth": "decrease", "dull": "decrease", "fuzzy": "decrease",
    "shallow": "decrease",
}
# REMOVED from BASE_ADJECTIVES: "open" and "shut".
# Not because their bare forms are ambiguous (AMBIGUOUS_BASES below handles
# that) but because the GENERATOR turns them into device nouns: shut ->
# "shutter", open -> "openner". Those land in _DECREASE_COMPARATIVES /
# _INCREASE_COMPARATIVES, so a device actually called a shutter reads as a
# degree word. Neither has a real comparative in English — you don't say "make
# it shutter" — so nothing is lost by dropping them from generation entirely.
# They are verbs with a target, not gradable degrees; route open/close/shut
# through the securement and semantic rungs instead.

# Bases that ARE gradable adjectives but are ALSO common device nouns or
# control verbs. Their comparative and superlative forms stay fully active
# ("lighter", "closest", "clearer" are unambiguous degree words) — only the
# BARE form is withheld from word_direction()'s fallback.
#
# This is the fix for the worst bug in the pipeline: in "turn off the light",
# the extractor's head-word loop skips 'on'/'off' as switch particles, reaches
# "light", and word_direction() returned "increase" from this table. Direction
# beats the switch block, so the command resolved to a one-step INCREASE and
# executor's delta branch ran set_state(level=current+step, on=True) — an OFF
# command that turned the light on and brightened it. Same shape for "close
# the blinds" (increase) and "clear" (which is also a CANCEL_SIGNAL).
AMBIGUOUS_BASES = frozenset({"light", "close", "clear"})

COMPARATIVE_RULES = [
    (r"^(.+[^aeiou])y$", r"\1ier"),
    (r"^(.+)e$", r"\1er"),
    (r"^(.*[^aeiou][aeiou])([^aeiouwxy])$", r"\1\2\2er"),
    (r"^(.+)$", r"\1er"),
]

SUPERLATIVE_RULES = [
    (r"^(.+[^aeiou])y$", r"\1iest"),
    (r"^(.+)e$", r"\1est"),
    (r"^(.*[^aeiou][aeiou])([^aeiouwxy])$", r"\1\2\2est"),
    (r"^(.+)$", r"\1est"),
]

IRREGULAR_COMPARATIVES = {
    "good": "better", "bad": "worse", "far": "farther",
    "little": "less", "much": "more", "many": "more",
    "well": "better", "ill": "worse"
}

IRREGULAR_SUPERLATIVES = {
    "good": "best", "bad": "worst", "far": "farthest",
    "little": "least", "much": "most", "many": "most",
    "well": "best", "ill": "worst"
}


def _generate_form(base: str, rules, irregulars: Dict[str, str]) -> str:
    base = base.lower().strip()
    if base in irregulars:
        return irregulars[base]
    for pattern, repl in rules:
        if re.search(pattern, base):
            return re.sub(pattern, repl, base)
    return base  # unreachable (last rule matches any non-empty input); safe


def generate_comparative(base: str) -> str:
    return _generate_form(base, COMPARATIVE_RULES, IRREGULAR_COMPARATIVES)


def generate_superlative(base: str) -> str:
    return _generate_form(base, SUPERLATIVE_RULES, IRREGULAR_SUPERLATIVES)


COMPARATIVE_LEXICON: Dict[str, float] = {}
_INCREASE_COMPARATIVES: Set[str] = set()
_DECREASE_COMPARATIVES: Set[str] = set()

_INCREASE_SUPERLATIVES: Set[str] = set()
_DECREASE_SUPERLATIVES: Set[str] = set()

for adj, direction in BASE_ADJECTIVES.items():
    comp = generate_comparative(adj)
    sup = generate_superlative(adj)
    COMPARATIVE_LEXICON[comp] = 0.10
    if direction == "increase":
        _INCREASE_COMPARATIVES.add(comp)
        _INCREASE_SUPERLATIVES.add(sup)
    else:
        _DECREASE_COMPARATIVES.add(comp)
        _DECREASE_SUPERLATIVES.add(sup)

# Manual overrides for edge cases the rules miss
COMPARATIVE_LEXICON.update({
    "tinier": 0.10,
})
_DECREASE_COMPARATIVES.add("tinier")


# ═══════════════════════════════════════════════════════════════
# 3. FLAT LOOKUP TABLES
# ═══════════════════════════════════════════════════════════════

_MODIFIER_LOOKUP: Dict[str, Tuple[str, float]] = {}
for category, data in LEXICON.items():
    if category == "directionals":
        continue
    for word in data["words"]:
        _MODIFIER_LOOKUP[word.lower()] = (category, data["magnitude"])

for word, mag in COMPARATIVE_LEXICON.items():
    _MODIFIER_LOOKUP[word] = ("comparative", mag)

# Periphrastic superlatives: "most warm" = maximizer increase, "least warm" = maximizer decrease
_MODIFIER_LOOKUP["most"] = ("maximizers", 1.0)
_MODIFIER_LOOKUP["least"] = ("maximizers", 1.0)

_DIRECTIONAL_LOOKUP: Dict[str, Literal["increase", "decrease"]] = {}
for word in LEXICON["directionals"]["increase"]:
    _DIRECTIONAL_LOOKUP[word.lower()] = "increase"
for word in LEXICON["directionals"]["decrease"]:
    _DIRECTIONAL_LOOKUP[word.lower()] = "decrease"


# ═══════════════════════════════════════════════════════════════
# 4. CLOSED-CLASS GRAMMATICAL MARKERS
# ═══════════════════════════════════════════════════════════════

# Words that make an utterance a QUESTION when they come first.
#
# Grouped by the auxiliary they belong to, because the gap that prompted this
# was asymmetric: is/are/does/do/has/have were all present and their PAST
# tenses -- was/were/did/had -- were all missing. So "is the door locked" read
# as a question and "was the door locked" did not; it fell through to command
# resolution, found no verb, and hit the executor's flip fallback. Asking
# whether a lock HAD been locked would unlock it.
#
# Keeping the tenses adjacent is the point. A missing form is visible here in
# a way it was not in one flat alphabetical set.
QUERY_STARTERS = frozenset({
    # wh-words
    "what", "whats", "how", "hows", "where", "wheres", "when", "whens",
    "who", "whos", "whose", "whom", "which", "why", "whys",
    # be: present / past
    "is", "are", "am", "was", "were",
    # do: present / past
    "does", "do", "did",
    # have: present / past
    "has", "have", "had",
    # modals -- "can you turn on the lights" is handled by the _MODAL_STARTERS
    # guard in is_query(), which sends a modal + "you" back to the command path
    # unless a query verb follows.
    "can", "could", "will", "would", "should", "shall", "may", "might",
})
QUERY_VERBS = frozenset({
    "tell", "give", "show", "read", "get", "report", "display",
    "fetch", "find", "check", "list",
})
# Nouns that make a bare phrase a question -- "front door status", "power
# usage" -- with no question word in front of it. A wh-word already carries
# "what's the power usage"; these cover the shorter form people actually use.
#
# The electrical terms are here because a power meter is a device you only
# ever ASK about. Without them "power usage" fell through to command
# resolution, and a phrase with no verb reaches the executor's flip fallback.
QUERY_NOUNS = frozenset({
    "reading", "status", "level", "setting", "state", "value",
    "humidity", "position", "measurement", "report", "temp", "temperature",
    "brightness", "volume",
    # electrical -- what a PZEM-style meter reports
    "power", "usage", "watts", "wattage", "voltage", "volts", "amps",
    "amperage", "current", "energy", "consumption", "draw",
})

# Any word meaning "the whole place" — generic across dwelling types, so
# "status of the house/home/apartment/caravan/..." are all recognised the
# same way, not just literally "house".
PREMISES_WORDS = frozenset({
    "house", "home", "building", "apartment", "apt", "flat", "place",
    "property", "premises", "caravan", "cabin", "cottage", "condo", "unit",
    "residence", "dwelling", "everywhere",
})

# Words that mean this is an ACTION, not a question, even if a QUERY_NOUN is
# also present ("turn up the volume" has the noun "volume" but is a command).
# Used to gate the QUERY_NOUNS check in is_query() below.
#
# The directional block is load-bearing: without it, "volume up", "brightness
# down" and "temperature higher" all matched a QUERY_NOUN with no action word
# present, so is_query() returned True and run_pre_filters reported
# intent="query" — main.py then READ the state instead of changing it. Verb-
# less commands are normal speech ("volume up"), so directionals count as
# actions here.
_ACTION_WORDS = frozenset({
    "turn", "set", "make", "switch", "increase", "decrease", "raise",
    "lower", "adjust", "dim", "brighten", "on", "off",
    "up", "down", "higher", "louder", "quieter", "brighter", "dimmer",
    "warmer", "cooler", "more", "less", "boost", "reduce", "cut", "crank",
})

# Up to five ordinary words may sit between the negation and the verb
# ("don't you dare unlock", "do not under any circumstances unlock"); the
# old pattern needed the verb immediately after, so those were not negated.
#
# The apostrophe class accepts the typographic ’ (U+2019) as well as the
# straight one: speech recognisers emit the former, and "don’t unlock the
# front door" used to miss this pattern entirely -- command_splitter then cut
# it into ["don't", "unlock the front door"] and the second chunk ran.
#
# The in-between word is `\w+(?:['’]\w+)?`, NOT `\w+['’]?\w*`: the latter can
# split one plain word between `\w+` and `\w*` in len(word) ways, and with
# five repetitions a non-matching sentence backtracked through the product of
# those -- ~30 s of CPU for "no" followed by six long words.
# "don't forget to lock the door" is the opposite of a negation -- it ASKS
# for the lock -- so a negator followed by "forget" is not one.
NEGATION_RE = re.compile(
    r"\b(?:don['’]?t|do not|never|no need to|no)\b[\s,]*(?!forget\b)"
    r"(?:\w+(?:['’]\w+)?[\s,]+){0,5}"
    r"\b(turn|switch|power|set|make|open|close|lock|unlock|play|start|"
    r"raise|lower|increase|decrease|dim|brighten|adjust|change|put|"
    r"cancel|stop|dismiss|abort|halt)\b"
)

OFF_WORDS = frozenset({"off", "shutdown"})
#: The mirror of OFF_WORDS, for the global ON added 2026-09-12. Deliberately
#: SMALLER: "off" and "shutdown" are unambiguous switch words, while "up" and
#: "start" are not ("turn everything up" is a brightness, "start the timer"
#: is not a switch at all), so only the particle that _says_on already
#: recognises counts here.
ON_WORDS = frozenset({"on"})

CANCEL_SIGNALS = frozenset({
    "cancel", "stop", "scrap", "clear", "remove", "delete", "nix", "void",
    "dismiss",
})

# Verbs that act on security-critical devices (locks, doors, gates). A command
# containing any of these routes through securement resolution before normal
# name/semantic matching, so "unlock the door" can never accidentally act on
# the wrong device.
SECUREMENT_VERBS = frozenset({
    "lock", "unlock", "bolt", "unbolt", "latch", "unlatch", "deadbolt",
    "arm", "disarm", "secure", "engage", "disengage", "chain", "unchain",
    "fasten", "unfasten", "seal", "unseal",
})


def _inflect(verb: str):
    """-ed and -ing forms with correct spelling (drop a trailing 'e' before
    adding the suffix: secure -> secured/securing, not secureed/secureing)."""
    if verb.endswith("e"):
        return verb + "d", verb[:-1] + "ing"
    return verb + "ed", verb + "ing"


# Every surface form (bare, "-ed", "-ing") mapped back to its base verb, so
# "is the door UNLOCKED" or "is it LOCKING" are recognised as securement
# vocabulary just like the bare imperative "unlock" — not just the exact verb
# string. Built once from SECUREMENT_VERBS so there's one list to maintain.
SECUREMENT_VERB_FORMS = {}
for _v in SECUREMENT_VERBS:
    _ed, _ing = _inflect(_v)
    SECUREMENT_VERB_FORMS[_v] = _v
    SECUREMENT_VERB_FORMS[_ed] = _v
    SECUREMENT_VERB_FORMS[_ing] = _v
del _v, _ed, _ing


# WHICH WAY A SECUREMENT VERB POINTS (2026-09-12: "what about lock all doors
# or lock everything").
#
# SECUREMENT_VERBS is one flat bag, so "lock" and "unlock" were the same kind
# of word to every caller. A blanket command has to know the difference: "lock
# everything" is fail-safe and may act outright, while "unlock everything"
# must never be a blanket command at all -- it names no device, so there is
# nothing for the challenge to name back.
#
# DERIVED, not typed. English does the work: a securement verb that is
# another securement verb with `un-` or `dis-` in front of it is the undoing
# of that verb. That pairs all eight (unlock/unbolt/unlatch/disarm/
# disengage/unchain/unfasten/unseal) and leaves the rest securing -- and it
# stays right when somebody adds a verb to the set above, which a second
# hand-typed list would not. Twice this week a hand-kept list of types drifted
# from the one beside it and a real device went unhandled in silence.
def _undoes(verb: str) -> bool:
    return any(verb.startswith(p) and verb[len(p):] in SECUREMENT_VERBS
               for p in ("un", "dis"))


UNSECURING_VERBS = frozenset(v for v in SECUREMENT_VERBS if _undoes(v))
SECURING_VERBS = frozenset(SECUREMENT_VERBS - UNSECURING_VERBS)

# Third-person pronouns that back-reference a previously controlled device
# ("make IT warmer", "turn THEM off").
PRONOUNS = frozenset({"it", "that", "this", "them", "those", "these"})

# Plural securement nouns — a command with any of these acts on the whole set
# ("lock all the doors").
SECUREMENT_PLURAL_NOUNS = frozenset({
    "doors", "locks", "gates", "padlocks", "bolts", "latches", "deadbolts",
    "entries",
})

# Function words to ignore when deciding whether a phrase names a device or is
# just a bare command wrapper.
COMMAND_STOPWORDS = frozenset({
    "the", "a", "an", "on", "off", "in", "turn", "set", "make", "it", "to",
    "that", "this", "them", "and", "or", "is", "are", "of", "my",
    "please", "up", "down", "little", "bit", "some", "more", "less",
})

# GENERIC directional words: they mean "change the value" without implying any
# domain, so they apply to ANY device with an adjustable numeric value
# ("higher" works on temperature, volume, or brightness alike). Contrast with
# domain-laden words like "warmer"/"brighter"/"louder", which imply a specific
# kind of device and so still need a semantic relevance check.
GENERIC_DIRECTIONALS = frozenset({
    "higher", "lower", "up", "down", "raise", "increase", "decrease",
    "more", "less", "reduce", "boost",
})


# ═══════════════════════════════════════════════════════════════
# 5. SWITCH GRAMMAR
# ═══════════════════════════════════════════════════════════════
# Vocabulary for deciding on/off, previously defined inline inside
# SlotExtractor.extract() — contradicting that module's "owns ZERO English
# vocabulary" docstring, and rebuilt as four fresh sets on every call.

# Determiners. A bare "on" followed by one of these is prepositional
# ("volume ON THE tv"), not a control particle ("turn it on").
DETERMINERS = frozenset({
    "the", "a", "an", "my", "your", "his", "her", "our", "their",
    "this", "that", "these", "those",
})

# Phrasal verbs that make a following "on" a control particle rather than a
# preposition.
PHRASAL_SWITCH_VERBS = frozenset({
    "turn", "switch", "power", "flip", "flick", "shut", "boot",
})

# Verbs that unambiguously mean on/off, so "activate the ac" forces ON rather
# than falling through to a bare toggle.
ON_SYNONYMS = frozenset({
    "activate", "enable", "start", "engage", "arm", "run",
})
OFF_SYNONYMS = frozenset({
    "deactivate", "disable", "stop", "disengage", "kill", "halt",
})

# Verbs where a following up/down means POWER, not degree: "shut it down" and
# "power down" are OFF, while "turn it down" stays a volume decrease. Kept
# deliberately small — every member removes a word from gradable use.
POWER_PHRASAL_VERBS = frozenset({"shut", "power", "boot"})

# A number following one of these is a quantity, not a device-name suffix
# ("set it to 20" vs "bulb 4"). COMMAND_STOPWORDS already covers "to"; the
# rest are listed here so the intent is explicit in one place.
VALUE_PREPOSITIONS = frozenset({
    "to", "at", "by", "plus", "minus", "equals", "around", "about", "near",
})


_APOSTROPHE_RE = re.compile("['’ʼ]")


def fold_apostrophes(text: str, repl: str = "") -> str:
    """Every apostrophe a transcriber emits (straight, typographic U+2019,
    modifier U+02BC) replaced by `repl`. The one place that knows the set:
    each module that spelt its own missed a different one."""
    return _APOSTROPHE_RE.sub(repl, text)


def _words(text: str) -> List[str]:
    # Both apostrophes: a transcriber's "what’s" (U+2019) otherwise stays
    # "what’s", which is not the query starter "whats", and the question
    # went down the command path.
    return fold_apostrophes(text.lower()).split()


_MODAL_STARTERS = frozenset({"can", "could", "will", "would"})


def is_query(text: str) -> bool:
    words = _words(text)
    if not words:
        return False
    # A modal + "you" is usually a polite COMMAND ("can you turn on the
    # lights"), not a question — unless it asks about state ("can you tell me
    # the temperature"). Defer to the query-verb check for the latter.
    if (words[0] in _MODAL_STARTERS and len(words) > 1 and words[1] == "you"):
        return len(words) > 2 and words[2] in QUERY_VERBS
    if words[0] in QUERY_STARTERS:
        return True
    if words[0] in QUERY_VERBS and len(words) > 1 and words[1] not in {"me", "us"}:
        return True
    # Noun-first phrasing with no starter word ("status of the locks",
    # "temperature reading in the kitchen"). A QUERY_NOUN present with no
    # action word or securement verb anywhere is a read request, not a
    # command — distinguishes this from "turn up the volume" (has "turn")
    # and from "volume up" (has the directional "up").
    wset = set(words)
    return (not wset.isdisjoint(QUERY_NOUNS) and wset.isdisjoint(_ACTION_WORDS)
            and wset.isdisjoint(SECUREMENT_VERB_FORMS))


#: "Is there anything on?", "what's on", "what is still running" -- a question
#: about WHICH devices are on, naming no device. It is a state query, but not
#: of any one device: the resolver has nothing to match, so it used to answer
#: "(nothing to read.)" to a house with the lights burning.
_WHATS_ON_OBJECTS = frozenset({"anything", "something", "everything", "devices",
                               "things", "stuff", "appliances"})
_ON_STATES = ("on", "running", "turned on", "switched on", "playing", "active",
              "left on")


def is_whats_on_query(text: str) -> bool:
    words = [w.strip(".,;:!?") for w in _words(text)]
    words = [w for w in words if w]
    if len(words) < 2:
        return False
    joined = " ".join(words)
    # drop filler that carries no meaning here
    for filler in (" still ", " currently ", " right now", " at the moment",
                   " there "):
        joined = joined.replace(filler, " ")
    joined = joined.strip()
    # "what's on" / "what is on" / "whats on"
    for head in ("whats", "what is", "what are", "which are", "which is"):
        if joined.startswith(head + " "):
            rest = joined[len(head) + 1:].strip()
            if rest in _ON_STATES:
                return True
    # "is anything on" / "are any devices on" / "anything on"
    parts = joined.split()
    if parts and parts[0] in ("is", "are", "any"):
        parts = parts[1:]
    if parts and parts[0] == "any":
        parts = parts[1:]
    if parts and parts[0] in _WHATS_ON_OBJECTS | {"devices"}:
        rest = " ".join(parts[1:]).strip()
        for suffix in (" in the house", " at home", " in here", " right now"):
            if rest.endswith(suffix):
                rest = rest[: -len(suffix)].strip()
        if rest in _ON_STATES:
            return True
        # "anything on in the kitchen": handled by the caller via room hint
        for st in _ON_STATES:
            if rest.startswith(st + " in ") or rest.startswith(st + " upstairs") \
                    or rest.startswith(st + " downstairs"):
                return True
    return False


def is_premises_query(text: str) -> bool:
    """A status question about the whole place ("status of the house", "how
    is the house doing", "how's the caravan"), not any specific device. This
    should be answered by listing every device, not by resolving to one or a
    few via semantic similarity — the query isn't naming a device at all."""
    if not is_query(text):
        return False
    words = set(_words(text))
    return bool(words & PREMISES_WORDS)


# ── Questions whose answer is a FACT, not a guess ───────────────────────
#
# The same rule as everywhere else in this file: what the sentence NAMES is
# answered at once, and only what it does not name is left to the encoder.
# TWO TIERS, so that a sentence nobody foresaw can never take a real command.
#
#   STRICT (asked before anything else): the sentence is a clock word and
#   harmless filler and NOTHING ELSE -- "what is the time", "time", "and the
#   time is what", "hey nova what day is it". Any other word, listed or not,
#   and it is not this tier's: unknown words block by default.
#
#   LOOSE (asked only AFTER the house understood nothing -- reason no_match):
#   a clock word with other words around it, "always the time" (a mis-heard
#   "what is the time"). The worst this can do is turn a "didn't understand"
#   into the time. The fences below keep that to sentences that are not
#   about something ELSE, and it needs no list of such things: the clock
#   word must have filler (or nothing) on both sides, so another event
#   ("what time does the heater turn off"), another day ("tomorrow's date"),
#   a pleasantry ("good day") all have a stranger touching the word. An
#   instruction (an action word) or a clock WITH A NUMBER ("at 7 pm") is out.
# A house name (device, room, scene: "movie time") is the house's in both.
_CLOCK_WORDS = {"time": "time", "date": "date", "day": "date",
                "month": "date", "year": "date"}
_CLOCK_FILLER = frozenset({
    "what", "whats", "which", "is", "its", "it", "the", "a", "an", "and", "now",
    "right", "today", "todays", "this", "current", "exact", "please", "tell",
    "me", "give", "can", "could", "you", "do", "have", "got", "know", "say",
    "hey", "hi", "hello", "ok", "okay", "i", "need", "want", "to", "like",
    "of", "just", "again", "then", "so", "um", "uh", "nova"})


def clock_question(text: str, names: Iterable[str] = (), loose: bool = False,
                   extra_filler: Iterable[str] = ()) -> Optional[str]:
    """"time", "date" or "both" for a sentence that asks for the time, the
    date, the day, the month or the year -- else None. `names` are the house's
    own names (devices, rooms, scenes). `loose` is the last-resort tier (see
    above); `extra_filler` is what else may sit in a strict sentence, e.g. the
    assistant's own name."""
    words = [w for w in (x.strip(".,;:!?") for x in _words(text)) if w]
    kinds = {_CLOCK_WORDS[w] for w in words if w in _CLOCK_WORDS}
    if not kinds or len(words) > 10 or any(c.isdigit() for w in words for c in w):
        return None
    if loose:
        # NO WORD OF OURS TO FENCE IT: the clock word must sit among filler
        # on BOTH sides ("always the time", "the date today my friend"). A
        # word of any other kind touching it means the sentence is about
        # something else -- "good day", "time zone", "every day", "next
        # year", "time until sunset", "what time does the heater ..." --
        # whatever that word is, listed or not. An instruction is out too.
        # A sentence that OPENS as a question had its one chance in the strict
        # tier; this tier is for fragments and mis-hearings, not for a real
        # question with a stranger in it ("what day is tomorrow").
        if words[0] in QUERY_STARTERS:
            return None
        ok = _CLOCK_FILLER | set(_CLOCK_WORDS) | {str(e).lower() for e in extra_filler}
        for i, w in enumerate(words):
            if w in _CLOCK_WORDS and (
                    (i > 0 and words[i - 1] not in ok)
                    or (i + 1 < len(words) and words[i + 1] not in ok)):
                return None
        if not _ACTION_WORDS.isdisjoint(words):
            return None
    else:
        ok = _CLOCK_FILLER | set(_CLOCK_WORDS) | {str(e).lower() for e in extra_filler}
        if any(w not in ok for w in words):
            return None
    joined = " " + " ".join(words) + " "
    if any(f" {n} " in joined for n in (str(x).lower().strip() for x in names) if n):
        return None
    return "both" if len(kinds) == 2 else kinds.pop()


_ROOM_FILLER = frozenset({
    "what", "whats", "how", "hows", "is", "are", "the", "a", "an", "in", "of",
    "about", "tell", "me", "give", "show", "status", "state", "states",
    "doing", "going", "happening", "everything", "all", "things", "devices",
    "there", "like", "report", "summary", "check", "can", "you", "please",
    "it", "its", "everything", "stuff", "up", "with",
})


def room_only_query(text: str, rooms: Iterable[str]) -> Optional[str]:
    """The (lower-case) room when the sentence is a question that names one
    room and nothing else -- "how is the kitchen", "what's in the living
    room", "kitchen status" -- else None. A question that names anything
    more ("is the kitchen light on", "why is the kitchen cold") is a device
    question and goes the device way."""
    if not is_query(text):
        return None
    joined = " " + " ".join(w for w in (x.strip(".,;:!?") for x in _words(text)) if w) + " "
    for r in sorted((str(x).lower().strip() for x in rooms), key=len, reverse=True):
        if r and f" {r} " in joined:
            rest = joined.replace(f" {r} ", " ", 1).split()
            return r if all(w in _ROOM_FILLER for w in rest) else None
    return None


_ROOMS_ASK_FILLER = frozenset({
    "which", "what", "are", "is", "there", "do", "i", "we", "have", "my", "the",
    "all", "me", "list", "show", "tell", "give", "name", "available", "rooms",
    "room", "can", "you", "please", "of", "got", "any", "exist", "exists",
})


def is_rooms_question(text: str) -> bool:
    """"which room", "what rooms are there", "list the rooms" -- asking for
    the rooms themselves, naming none."""
    words = [w for w in (x.strip(".,;:!?") for x in _words(text)) if w]
    if not words or not {"room", "rooms"} & set(words):
        return False
    return all(w in _ROOMS_ASK_FILLER for w in words)


#: The words people use for "the things in the house" as a WHOLE, singular and
#: plural. A kind of device ("lights", "fans", "sensors") is deliberately NOT
#: here: a device of that kind is an ordinary device, and a question about it
#: goes the way every device question goes (by name, then by meaning).
_COLLECTIVE_NOUNS = frozenset({
    "device", "devices", "appliance", "appliances", "tool", "tools",
    "machine", "machines", "gadget", "gadgets", "equipment", "hardware",
    "gear", "kit", "thing", "things", "stuff", "item", "items", "unit",
    "units", "object", "objects", "electronics", "electricals", "fixture",
    "fixtures", "instrument", "instruments", "apparatus", "utility",
    "utilities", "accessory", "accessories", "peripheral", "peripherals",
    "component", "components", "module", "modules", "node", "nodes",
    "entity", "entities", "gizmo", "gizmos", "widget", "widgets",
    "contraption", "contraptions", "system", "systems", "control",
    "controls", "installation", "installations", "setup", "devise", "devises",
    "electrical", "mechanism", "mechanisms", "product", "products",
    "resource", "resources", "asset", "assets", "equipments", "tech",
    "technology", "technologies", "smart"})

#: Possession and existence, and the hub's own words for a device being part
#: of the house.
_INVENTORY_VERBS = frozenset({"have", "got", "there", "installed", "connected",
                              "registered", "own", "owned", "available"})

#: Grammar around such a question: nothing here names anything.
_INVENTORY_FILLER = frozenset({
    "what", "whats", "which", "how", "many", "much", "is", "are", "was",
    "were", "do", "does", "did", "have", "has", "had", "got", "there",
    "theres", "i", "we", "you", "me", "my", "our", "us", "the", "a", "an",
    "all", "any", "of", "in", "at", "here", "home", "house", "now",
    "currently", "right", "today", "installed", "connected", "registered",
    "own", "owned", "available", "around", "altogether", "total", "else",
    "other", "more", "ones", "one", "that", "this", "these", "those", "to",
    "tell", "list", "give", "show", "please", "again", "nova", "hey",
    "read", "get", "report", "display", "fetch", "find", "check", "out",
    "can", "could", "would"})


def is_inventory_question(text: str, names: Iterable[str] = ()) -> bool:
    """"what devices do I have", "what appliances / tools / machines do we
    have", "what is installed in the house", "what do I have" -- asking for
    WHAT THE HOUSE HAS, as a whole. Known by its frame (a wh-word with
    possession or existence) and by a collective noun ("devices", "tools",
    "stuff") or no noun at all. A KIND of device ("what lights do I have",
    "what lights are on") has a word that is neither, so it is not this and
    goes the ordinary way. It names no device, room or scene, asks for no
    reading or time, and gives no instruction."""
    words = [w for w in (x.strip(".,;:!?") for x in _words(text)) if w]
    if not words or len(words) > 10:
        return False
    # Asked ("what ... do I have") or asked FOR ("list the devices we have",
    # "tell me what is installed"): the verbs that ask to be told are the
    # ones the hub already knows a read request by (QUERY_VERBS).
    if words[0] not in {"what", "whats", "which"} and words[:2] != ["how", "many"] \
            and words[0] not in QUERY_VERBS:
        return False
    if any(c.isdigit() for w in words for c in w):
        return False
    wset = set(words)
    if not (wset.isdisjoint(_CLOCK_WORDS) and wset.isdisjoint(QUERY_NOUNS)
            and wset.isdisjoint(_ACTION_WORDS)):
        return False
    content = {w for w in words if w not in _INVENTORY_FILLER}
    if not content <= _COLLECTIVE_NOUNS:
        return False
    if not content and wset.isdisjoint(_INVENTORY_VERBS):
        return False
    joined = " " + " ".join(words) + " "
    return not any(f" {n} " in joined
                   for n in (str(x).lower().strip() for x in names) if n)


def asks_existence(text: str) -> bool:
    """"do I have a power meter", "have we got a blaster", "is there an IR
    sensor" -- a yes/no question about whether the house has something. Its
    answer starts "Yes", then says the device the way every device answer
    does."""
    words = [w for w in (x.strip(".,;:!?") for x in _words(text)) if w]
    if not words:
        return False
    if words[0] in {"do", "does", "did", "have", "has"}:
        return not {"have", "got"}.isdisjoint(words[1:])
    return words[:2] in (["is", "there"], ["are", "there"]) or words[0] == "theres"


def detect_negation(text: str):
    m = NEGATION_RE.search(text.lower())
    return m.group(1) if m else None


def _says_off(words: List[str]) -> bool:
    """Does this utterance carry an OFF signal?

    Two forms: a bare off-word, or a power phrasal verb with its particle
    ("shut everything DOWN", "power it DOWN"). Only the first was recognised,
    so "shut everything down" -- an entirely ordinary way to say it -- was not
    an all-off at all and fell through to device resolution.
    """
    if set(words) & OFF_WORDS:
        return True
    for i, w in enumerate(words):
        if w in POWER_PHRASAL_VERBS:
            if any(nxt in POWER_OFF_PARTICLES for nxt in words[i + 1:i + 4]):
                return True
    return False


def phrasal_power_switch(words: List[str]) -> Optional[str]:
    """'shut it down', 'power down', 'boot up' -> a hard on/off, not a delta.

    These have to be decided BEFORE direction detection, because 'down' and
    'up' are directional words: without this, "shut it down" resolved to
    'decrease by one step' and never reached the switch block. Deliberately
    narrow — only the power-phrasal verbs qualify, so "turn it down" (a real
    volume decrease) is untouched."""
    for i, w in enumerate(words):
        if w not in POWER_PHRASAL_VERBS:
            continue
        for nxt in words[i + 1:i + 4]:      # allow "shut IT down"
            if nxt in POWER_OFF_PARTICLES:
                return "off"
            if nxt in POWER_ON_PARTICLES:
                return "on"
    return None


def is_prepositional_on(words: Sequence[str], i: int) -> bool:
    """Is the "on" at words[i] a preposition ("volume ON THE tv", "off ON
    the second floor") rather than a switch particle ("turn it on")? A
    preposition is followed by a determiner and not preceded by a phrasal
    verb."""
    prev = words[i - 1] if i > 0 else ""
    nxt = words[i + 1] if i + 1 < len(words) else ""
    return prev not in PHRASAL_SWITCH_VERBS and nxt in DETERMINERS


def _says_on(words: List[str]) -> bool:
    """Is there a switch-particle "on"? A PREPOSITIONAL "on" does not count.

    The old guard was a bare `"on" in words`, which vetoed
    "turn everything off ON the second floor" -- an off command containing a
    perfectly ordinary preposition. Same distinction _detect_switch draws in
    slot_extractor: a control particle follows a phrasal verb or ends the
    clause; a preposition is followed by a determiner.
    """
    return any(w == "on" and not is_prepositional_on(words, i)
               for i, w in enumerate(words))


def _names_everything(words: List[str], particles: FrozenSet[str]) -> bool:
    """Does the utterance address the whole scope? "everything",
    "everywhere", "all off", or a closing "all" that belongs to the switch
    ("turn off all", "switch them all").

    A closing "all" after anything else is not a scope: "turn off the lamp,
    that is all" switched off the whole house, and "turn on the lamp and
    that's all" switched the whole house on. ("shut down all" keeps working:
    a power particle counts as the switch.)"""
    for i, w in enumerate(words):
        if w in ("everything", "everywhere"):
            return True
        if w == "all":
            nxt = words[i + 1] if i + 1 < len(words) else ""
            prev = words[i - 1] if i > 0 else ""
            if nxt in particles or (nxt == "" and (
                    prev in particles or prev in PRONOUNS
                    or prev in POWER_OFF_PARTICLES + POWER_ON_PARTICLES)):
                return True
    return False


def detect_scope_off(text: str) -> bool:
    words = _words(text)
    if _says_on(words) or not _says_off(words):
        return False
    return _names_everything(words, OFF_WORDS)


def detect_scope_on(text: str) -> bool:
    """The mirror of detect_scope_off (2026-09-12: "just do what all off
    does, just copy it but for on").

    Same shape, same words, the two signals swapped: the utterance must
    carry an ON and must NOT carry an OFF. `_says_on` already knows the
    difference between a switch particle and a preposition, which is what
    keeps "turn everything on the second floor off" out of here -- that
    sentence says OFF, and the second test rejects it.
    """
    words = _words(text)
    if _says_off(words) or not _says_on(words):
        return False
    return _names_everything(words, ON_WORDS)


def detect_cancel(text: str):
    if detect_negation(text):
        return None
    words = set(_words(text))
    if not (words & CANCEL_SIGNALS):
        return None
    if words & {"timer", "countdown"}:
        return "cancel_timer"
    if words & {"alarm", "wake"}:
        return "cancel_alarm"
    return None


# ── Maximizer words that force a DECREASE ("set to minimum") ──
# "min"/"minimum"/"zero"/"none"/"lowest"/"least"/"off" are maximizer-magnitude
# (1.0) but point DOWN, unlike "max"/"full"/"completely" which point up.
DECREASE_MAXIMIZERS: FrozenSet[str] = frozenset({
    "min", "minimum", "zero", "none", "lowest", "least"})
# "off" was a member and could never match. This set is only consulted for a
# word the extractor found via _MODIFIER_RE, which is built from
# _MODIFIER_LOOKUP -- and "off" is a directional, which _MODIFIER_LOOKUP skips.
# It read as a seventh case and was an eighth line of nothing.


# ── Unit normalization ──
# Every unit alias a user might say -> canonical unit name. This is English
# vocabulary, so it lives in the lexicon, not the extractor.
_UNIT_MAP = {
    'f': 'fahrenheit', 'fahrenheit': 'fahrenheit',
    'c': 'celsius', 'celsius': 'celsius',
    'degrees': 'degrees', 'degree': 'degrees',
    '°': 'degrees', '°f': 'fahrenheit', '°c': 'celsius',
    '%': 'percent', 'percent': 'percent', 'pct': 'percent',
    'minutes': 'minutes', 'minute': 'minutes', 'mins': 'minutes',
    'min': 'minutes', 'hours': 'hours', 'hour': 'hours', 'hrs': 'hours',
    'hr': 'hours', 'seconds': 'seconds', 'secs': 'seconds', 'sec': 'seconds',
    'volts': 'volts', 'volt': 'volts', 'v': 'volts', 'watts': 'watts',
    'watt': 'watts', 'w': 'watts', 'lumens': 'lumens', 'lumen': 'lumens',
    'lux': 'lux', 'kelvin': 'kelvin', 'k': 'kelvin',
    # 'second' was missing while 'seconds'/'secs'/'sec' were present, so the
    # one singular form fell through normalize_unit unchanged.
    'second': 'seconds',
    # Step-relative units. slot_extractor's delta regex already accepted these
    # and there was nothing here to canonicalise them against, so they came
    # back as whatever the user happened to say.
    'point': 'steps', 'points': 'steps', 'step': 'steps', 'steps': 'steps',
    'level': 'steps', 'levels': 'steps', 'notch': 'steps', 'notches': 'steps',
}


def unit_pattern() -> str:
    """Regex alternation of every unit token, longest first.

    slot_extractor built TWO hand-written unit lists into two regexes, and they
    disagreed with each other and with this map: the value regex knew
    fahrenheit/minutes/watts, the delta regex knew points/steps/notches, and
    neither knew what the other did. "raise it by 2 fahrenheit" parsed the
    number and dropped the unit. Both are generated from here now, so the three
    cannot drift again."""
    return "|".join(re.escape(u) for u in sorted(_UNIT_MAP, key=len, reverse=True))


def normalize_unit(raw: Optional[str]) -> Optional[str]:
    """Canonicalize a unit token ('degrees'/'°f'/'pct' -> a standard name).
    Returns None for empty input."""
    if not raw:
        return None
    return _UNIT_MAP.get(raw.lower().strip().rstrip('.'), raw.lower())


def is_superlative(word: str) -> bool:
    """True if the word is a superlative form (warmest, coolest, best...).
    A superlative denotes the extreme of its scale, so callers treat it as a
    maximizer (go to the range bound) rather than a one-step comparative."""
    w = word.lower()
    return w in _INCREASE_SUPERLATIVES or w in _DECREASE_SUPERLATIVES


# The two bare switch particles. They appear in LEXICON["directionals"] --
# "turn it up" and "turn it on" really are cousins -- but treating them as
# DEGREE words is wrong, and was actively harmful:
#
#   word_direction("off") == "decrease"
#     -> slot_extractor._value_evidence saw a direction word before a number
#     -> "turn off 2 lamps" read 2 as a VALUE, not a count
#     -> build_command produced set level=2, and executor's `on = value > floor`
#        switched the lamps ON at 2%.
#   An OFF command turned two lamps on and dimmed them.
#
# This is the same defect AMBIGUOUS_BASES exists to prevent, one table over:
# that gate only covers the BASE_ADJECTIVES fallback, while _DIRECTIONAL_LOOKUP
# is consulted first and was ungated. The switch path in slot_extractor handles
# on/off correctly on its own; nothing needs a direction from them.
SWITCH_PARTICLES: FrozenSet[str] = frozenset({"on", "off"})


def word_direction(word: str) -> Optional[Literal["increase", "decrease"]]:
    """Direction implied by a SINGLE word, via directionals, comparatives,
    superlatives, or base-adjective derivation. Returns 'increase',
    'decrease', or None. The extractor uses this to resolve a head word
    without duplicating any vocabulary.

    The BASE_ADJECTIVES fallback is gated by AMBIGUOUS_BASES: a bare "light",
    "close" or "clear" is far more often a noun or a control verb than a
    degree word, and letting it imply a direction reverses commands like
    "turn off the light". Their comparatives and superlatives are NOT gated —
    "lighter" and "closest" are unambiguous and still resolve here."""
    w = word.lower()
    if w in SWITCH_PARTICLES:
        return None
    if w in _DIRECTIONAL_LOOKUP:
        return _DIRECTIONAL_LOOKUP[w]
    if w in _INCREASE_COMPARATIVES or w in _INCREASE_SUPERLATIVES:
        return "increase"
    if w in _DECREASE_COMPARATIVES or w in _DECREASE_SUPERLATIVES:
        return "decrease"
    if w in BASE_ADJECTIVES and w not in AMBIGUOUS_BASES:
        return BASE_ADJECTIVES[w]
    return None


def room_named(text: str, known_rooms: Optional[Sequence[str]] = None) -> Optional[str]:
    """The room an utterance names, or None.

    A known room name appearing as whole words wins (longest first, so
    "living room" beats "room"); otherwise "in the <words>" cut at the first
    ROOM_STOP_WORDS entry, the same rule main uses to extract a room hint."""
    low = text.lower()
    for r in sorted((str(r).lower() for r in (known_rooms or []) if r),
                    key=len, reverse=True):
        if re.search(r"\b" + re.escape(r) + r"\b", low):
            return r
    m = re.search(r"\bin (?:the |my )?([a-z]+(?: [a-z]+){0,2})", " " + low)
    if not m:
        return None
    words = m.group(1).split()
    cut = next((i for i, w in enumerate(words) if w in ROOM_STOP_WORDS),
               len(words))
    return " ".join(words[:cut]) or None


#: Verbs that ASK for a switch (never the "turned off" of a state question).
_SWITCH_REQUEST_VERBS = frozenset({"turn", "turning", "switch", "switching",
                                   "shutting", "kill", "killing"})


def run_pre_filters(text: str, known_rooms: Optional[Sequence[str]] = None):
    negated_verb = detect_negation(text)
    if negated_verb:
        return {"negated": True, "verb": negated_verb, "source": "lexicon"}

    cancel = detect_cancel(text)
    if cancel:
        return {"intent": cancel, "source": "lexicon"}

    # A QUESTION IS NEVER A BLANKET SWITCH. "is everything off?" carries an
    # off-word and "everything", and switched the whole house off.
    query = is_query(text)
    # A question that asks for the switching itself ("is it possible to turn
    # everything off", "how about turning it all on") is a polite request;
    # only a question about the STATE ("is it all off") must not switch.
    blanket_ok = not query or not set(_words(text)).isdisjoint(_SWITCH_REQUEST_VERBS)
    if blanket_ok and detect_scope_off(text):
        # "room": the room the utterance names, or None for the whole house.
        # "turn everything off in the bedroom" used to switch off the house.
        room = room_named(text, known_rooms)
        return {"intent": "all_off", "scope": "room" if room else "global",
                "room": room, "source": "lexicon"}

    # OFF IS TESTED FIRST, deliberately. The two detectors already exclude
    # each other -- each refuses when the opposite signal is present -- so
    # the order cannot change an answer. It is this way round because an
    # utterance that somehow carried both should end up OFF: too much off
    # has never hurt anybody, and this is the branch that turns a house
    # dark rather than the one that turns a heater on.
    if blanket_ok and detect_scope_on(text):
        room = room_named(text, known_rooms)
        return {"intent": "all_on", "scope": "room" if room else "global",
                "room": room, "source": "lexicon"}

    # THERE IS NO SECUREMENT BRANCH HERE, and that is deliberate (2026-09-12).
    # One was added and removed the same hour. "lock everything", "lock up",
    # "unlock all the doors" are NOT unhandled: every one of them reaches
    # resolver._resolve_securement, which finds the verb, reads its direction,
    # and comes back with a pick-all over every secure device in scope --
    # which main then puts through the full active voice challenge. Claiming
    # them here intercepted a rung that already worked: it dropped the
    # confirmation on the way in, and turned "unlock everything" from a
    # challenged unlock into a flat refusal. A pre-filter is for commands that
    # name no device; a securement command names the set, and the rung that
    # knows about secure devices is the one that should answer it.
    if query:
        return {"intent": "query", "source": "lexicon"}

    return None

# ═══════════════════════════════════════════════════════════════
# COLOUR WORDS
# ═══════════════════════════════════════════════════════════════
# Colour does NOT compose -- there is no rule that builds "teal" out of
# smaller parts, only a name someone assigned to a triple. So it is a lookup,
# and the question is only WHOSE lookup.
#
# Two tiers, because neither alone is right:
#
#   1. This table -- the words people actually say to a light, including ones
#      CSS has never heard of ("amber", "mint"). Small, safe, spoken.
#   2. webcolors, consulted only when the table misses -- 147 CSS3 names, so
#      "chartreuse" and "skyblue" work without anyone maintaining a list.
#
# The second tier CANNOT be used unguarded, and that is the whole reason this
# table still exists. CSS3 contains "snow", "tan", "linen", "plum", "orchid",
# "wheat", "ivory" -- ordinary household nouns. Input here is a TOKENISED
# SENTENCE, not a colour picker, and colour is checked FIRST in the executor's
# precedence, so a false hit outranks a real value or switch word. A device
# named "orchid", a room called "the snug": every command mentioning them
# becomes a colour change. Same failure shape as word_direction("off")
# returning "decrease" -- vocabulary too eager, silently beating the correct
# reading. _AMBIGUOUS_COLOUR_WORDS below is the exclusion list.
#
# Values are RGB triples because that is what Home Assistant's light.turn_on
# takes as `rgb_color`. Warm/cool white are the exception -- they are really
# colour TEMPERATURE, a different service field on a different axis, and are
# left out rather than approximated with an RGB value that would look wrong on
# a tunable-white bulb. See COLOUR_TEMPERATURE_WORDS below for why they are
# named but unhandled.

COLOUR_WORDS: Dict[str, Tuple[int, int, int]] = {
    "red": (255, 0, 0),
    "green": (0, 255, 0),
    "blue": (0, 0, 255),
    "yellow": (255, 255, 0),
    "orange": (255, 165, 0),
    "purple": (128, 0, 128),
    "violet": (138, 43, 226),
    "pink": (255, 105, 180),
    "magenta": (255, 0, 255),
    "cyan": (0, 255, 255),
    "turquoise": (64, 224, 208),
    "teal": (0, 128, 128),
    "lime": (50, 205, 50),
    "indigo": (75, 0, 130),
    "white": (255, 255, 255),
    "amber": (255, 191, 0),
    "gold": (255, 215, 0),
    "crimson": (220, 20, 60),
    "maroon": (128, 0, 0),
    "navy": (0, 0, 128),
    "brown": (139, 69, 19),
    "coral": (255, 127, 80),
    "salmon": (250, 128, 114),
    "lavender": (230, 230, 250),
    "mint": (152, 255, 152),
    "olive": (128, 128, 0),
}

#: Named so they are RECOGNISED rather than silently missed, but not mapped to
#: RGB. "Warm white" on a tunable bulb is a low colour temperature, not a
#: yellowish RGB value -- sending the latter would look wrong and would also
#: switch a colour-temp bulb into RGB mode. Handling them needs the
#: color_temp_kelvin service field, which is a separate change.
COLOUR_TEMPERATURE_WORDS: FrozenSet[str] = frozenset({
    "warm", "cool", "daylight", "soft", "neutral",
})


#: CSS3 colour names that are ordinary English nouns first and colours second.
#: Excluded from the webcolors fallback -- never from the curated table above,
#: where a word appearing is a deliberate decision.
_AMBIGUOUS_COLOUR_WORDS: FrozenSet[str] = frozenset({
    "snow", "linen", "wheat", "ivory", "tan", "plum", "orchid", "thistle",
    "seashell", "honeydew", "lavenderblush", "mistyrose", "azure", "bisque",
    "cornsilk", "peru", "sienna", "chocolate", "tomato", "moccasin", "khaki",
    "gainsboro", "oldlace", "papayawhip", "navajowhite", "peachpuff",
    "mintcream", "aliceblue", "ghostwhite", "floralwhite", "antiquewhite",
    "beige", "burlywood", "rosybrown",
})

try:
    import webcolors as _webcolors
    _HAS_WEBCOLORS = True
except ImportError:
    _HAS_WEBCOLORS = False
    print("[LEXICON] webcolors not installed -- colour recognition is limited to "
          "the built-in list. `pip install webcolors` for the full CSS3 set.")


def _webcolours_lookup(word: str) -> Optional[Tuple[int, int, int]]:
    """CSS3 name -> RGB, or None. Excludes the ambiguous nouns."""
    if not _HAS_WEBCOLORS or word in _AMBIGUOUS_COLOUR_WORDS:
        return None
    try:
        r, g, b = _webcolors.name_to_rgb(word)
        return (int(r), int(g), int(b))
    except ValueError:
        return None


def word_colour(word: str) -> Optional[Tuple[int, int, int]]:
    """RGB for a colour word, or None. Case-insensitive.

    Curated table first, then CSS3. The order matters: "mint" and "amber" are
    in the table and not in CSS3, and where both know a name the table's value
    is the one chosen deliberately for a light rather than for a web page.
    """
    w = word.lower().strip()
    return COLOUR_WORDS.get(w) or _webcolours_lookup(w)


#: A single letter is never a colour here. matplotlib and some other palettes
#: map "b"/"r"/"g"/"k"/"w" to blue/red/green/black/white -- fine in a plotting
#: call, ruinous against tokenised speech, where any stray initial would
#: outrank the rest of the command.
_MIN_COLOUR_WORD_LEN = 3


def find_colour(words: Sequence[str]) -> Optional[Tuple[int, int, int]]:
    """The first colour named in a run of words, or None.

    Tries TWO-WORD forms first, because that is how people speak and how no
    colour library stores them: CSS3 has "darkblue", a person says "dark
    blue". Joining the pair recovers the whole light/dark/hot/sky family
    without a single extra entry anywhere.

    Skips colour-temperature words rather than guessing, so "warm white" is an
    honest miss instead of a confidently wrong plain white.
    """
    lowered = [w.lower().strip() for w in words]

    for i, w in enumerate(lowered):
        if len(w) < _MIN_COLOUR_WORD_LEN:
            continue

        if w in COLOUR_TEMPERATURE_WORDS:
            # "warm white", "cool white" -- the following word is qualified by
            # this one, so neither is a plain colour.
            if i + 1 < len(lowered) and word_colour(lowered[i + 1]):
                return None
            continue

        # "dark blue" -> "darkblue". Checked BEFORE the single word, or
        # "dark blue" would resolve on "blue" alone and lose the qualifier.
        if i + 1 < len(lowered):
            nxt = lowered[i + 1]
            if len(nxt) >= _MIN_COLOUR_WORD_LEN and nxt not in COLOUR_TEMPERATURE_WORDS:
                joined = word_colour(w + nxt)
                if joined is not None:
                    return joined

        rgb = word_colour(w)
        if rgb is not None:
            return rgb
    return None


# ═══════════════════════════════════════════════════════════════
# NUMBER WORDS
# ═══════════════════════════════════════════════════════════════
# "set it to twenty" was invisible to the extractor: _VALUE_RE matches digits
# only, so the number simply was not there. Worse than nothing on the delta
# path -- "turn it up by five" found no explicit delta, fell back to the
# COMPARATIVE magnitude, and moved ONE step instead of five. A wrong answer
# with no error.
#
# A LOOKUP TABLE ALONE IS NOT ENOUGH. English numbers compose: "twenty three"
# is twenty + three, "two hundred and fifty" is 2 x 100 + 50. Listing every
# number anyone might say is unbounded; listing the ATOMS and composing them
# is finite and complete. So this is a small table plus words_to_number().

_UNITS_WORDS: Dict[str, int] = {
    "zero": 0, "oh": 0, "nought": 0,
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
    "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10,
    "eleven": 11, "twelve": 12, "thirteen": 13, "fourteen": 14,
    "fifteen": 15, "sixteen": 16, "seventeen": 17, "eighteen": 18,
    "nineteen": 19,
}
_TENS_WORDS: Dict[str, int] = {
    "twenty": 20, "thirty": 30, "forty": 40, "fourty": 40,   # fourty: common ASR spelling
    "fifty": 50, "sixty": 60, "seventy": 70, "eighty": 80, "ninety": 90,
}
#: Multipliers. Ordered because they must be applied largest-first.
_SCALES_WORDS: Dict[str, int] = {"hundred": 100, "thousand": 1000}

#: Half and quarter are the fractions people say to a door or a blind:
#: "half open", "a quarter", "three quarters" (2026-09-10 -- a gate that can
#: stop part-way is a device with a position). The plurals are the same
#: words; "three quarters" is three of them. Anything finer than this is not
#: something anyone says to a thermostat.
_FRACTION_WORDS: Dict[str, float] = {"half": 0.5, "halves": 0.5,
                                     "quarter": 0.25, "quarters": 0.25}

#: Words that may sit INSIDE a spoken number without contributing a value.
#: "a hundred" and "two hundred AND fifty" -- the article and the conjunction
#: are structural, not numeric.
_NUMBER_GLUE: FrozenSet[str] = frozenset({"a", "an", "and"})

#: Every word that can begin or continue a spoken number. Used to find the
#: SPAN of a number in a sentence before parsing it.
NUMBER_WORDS: FrozenSet[str] = frozenset(
    set(_UNITS_WORDS) | set(_TENS_WORDS) | set(_SCALES_WORDS)
    | set(_FRACTION_WORDS)
)


def words_to_number(words: Sequence[str]) -> Optional[float]:
    """Parse a run of number words into a value, or None if it is not one.

    Handles the shapes people actually say to a thermostat or a dimmer:

        "twenty"                  -> 20
        "twenty three"            -> 23      (tens + units)
        "twenty-three"            -> 23      (the caller splits on the hyphen)
        "a hundred"               -> 100     (article as glue)
        "two hundred and fifty"   -> 250     (scale, then conjunction, then tens)
        "one and a half"          -> 1.5
        "half"                    -> 0.5
        "three quarters"          -> 0.75    (a count of fractions)

    Returns None for anything that is not a complete number, INCLUDING a run
    that is only glue ("a", "and"). Returning 0 for those would turn the "a"
    in "turn on a light" into a value.

    Deliberately not a general English-number library. It covers the closed
    set of forms that appear in device commands; "three quarters of the way"
    and ordinals ("the third light") are out of scope and return None rather
    than a wrong number.
    """
    if not words:
        return None

    total = 0.0        # completed scale groups ("two hundred" -> 200)
    current = 0.0      # the group being accumulated
    fraction = 0.0
    seen_value = False        # did any word actually contribute a number?
    seen_scale_or_tens = False
    after_glue = False        # was the previous word "and"/"a"/"an"?

    for raw in words:
        w = raw.lower().strip()
        if not w:
            continue
        if w in _NUMBER_GLUE:
            after_glue = True
            continue
        glued, after_glue = after_glue, False

        if w in _UNITS_WORDS:
            # "twenty three" -- a unit after a tens word ADDS. But "three
            # three" is not a number, it is two numbers; reject rather than
            # silently making 6.
            if current % 10 and _UNITS_WORDS[w] < 10 and not seen_scale_or_tens:
                return None
            current += _UNITS_WORDS[w]
            seen_value = True
        elif w in _TENS_WORDS:
            current += _TENS_WORDS[w]
            seen_value = True
            seen_scale_or_tens = True
        elif w in _SCALES_WORDS:
            # A scale multiplies what came before it. "hundred" alone means
            # one hundred, so an empty group counts as 1.
            #
            # "hundred" stays INSIDE the current group and only "thousand"
            # closes it: committing after "hundred" made "a hundred
            # thousand" 100 + 1000 = 1100 instead of 100000.
            if _SCALES_WORDS[w] >= 1000:
                total += (current or 1) * _SCALES_WORDS[w]
                current = 0.0
            else:
                current = (current or 1) * _SCALES_WORDS[w]
            seen_value = True
            seen_scale_or_tens = True
        elif w in _FRACTION_WORDS:
            # "three quarters" is 3 x 0.25: units DIRECTLY before a fraction
            # word COUNT it. With glue between ("one and a half", "two and
            # a half") the fraction ADDS instead. The count used to be
            # decided by value alone (>= 2 multiplies), so "two and a half"
            # parsed as 2 x 0.5 = 1.0.
            if (current >= 2 and not glued and not seen_scale_or_tens
                    and not total):
                fraction += current * _FRACTION_WORDS[w]
                current = 0.0
            else:
                fraction += _FRACTION_WORDS[w]
            seen_value = True
        else:
            return None       # not a number word at all

    if not seen_value:
        return None
    return total + current + fraction


def find_number_span(words: Sequence[str], start: int) -> int:
    """Index just past the number that begins at `words[start]`.

    Returns `start` when there is no number there. Trailing glue is NOT
    included: "twenty and the lamp" gives the span for "twenty" alone, so the
    stray "and" cannot drag an unrelated word in.
    """
    # "oh" is zero only INSIDE a number ("twenty oh five"). As the first
    # word it is an interjection: "oh, turn on the light" became "0 turn on
    # the light", an absolute value of 0 -- and the light went OFF.
    if start < len(words) and words[start].lower() == "oh":
        return start
    i = start
    end = start
    while i < len(words):
        w = words[i].lower()
        if w in NUMBER_WORDS:
            i += 1
            end = i          # only a real number word extends the span
        elif w in _NUMBER_GLUE and i > start:
            i += 1           # glue may sit INSIDE, but never ends a number
        else:
            break
    return end


# ═══════════════════════════════════════════════════════════════
# 6. VOCABULARY MIGRATED FROM slot_extractor.py
# ═══════════════════════════════════════════════════════════════
# Closed-class particles that slot_extractor spelled out inline, contradicting
# its own "owns ZERO English vocabulary" docstring. Grouped by the grammar rule
# that consumes them.

# Introduce an explicit delta: "by 5", "plus 3", "minus 2".
DELTA_INTRODUCERS: Tuple[str, ...] = ("by", "plus", "minus")

# Introduce an absolute target: "to 20", "at 72". A DESTINATION, not a
# movement -- which is the distinction VALUE_PREPOSITIONS above does not make.
# It lumps "to"/"at" together with "by"/"plus"/"minus", so it cannot be used
# where only a target counts. Both remain: VALUE_PREPOSITIONS answers "is the
# number that follows a quantity?", these answer "is it a destination?".
TARGET_PREPOSITIONS: Tuple[str, ...] = ("to", "at")

# Periphrastic comparative/superlative heads: "more warm", "least bright".
# The analytic counterpart of the -er/-est forms the generator produces.
PERIPHRASTIC_UP: Tuple[str, ...] = ("more", "most")
PERIPHRASTIC_DOWN: Tuple[str, ...] = ("less", "least")
PERIPHRASTIC_EXTREME: Tuple[str, ...] = ("most", "least")

# Particles that turn a POWER_PHRASAL_VERB into a hard on/off.
POWER_OFF_PARTICLES: Tuple[str, ...] = ("down", "off")
POWER_ON_PARTICLES: Tuple[str, ...] = ("up", "on")

# The bare switch words, as an ordered pair for iteration. Same membership as
# SWITCH_PARTICLES, which is the set used for exclusion tests.
SWITCH_WORDS: Tuple[str, ...] = ("on", "off")


# ═══════════════════════════════════════════════════════════════
# 7. VOCABULARY MIGRATED FROM resolver.py AND main.py
# ═══════════════════════════════════════════════════════════════

# Securement verbs that INCREASE security, so `lock_on` is true for them.
# resolver.py held a hand-written copy of the same ten words, which was
# correct only for as long as nobody added an eleventh verb above and forgot
# to mirror it here.
#
# 2026-09-12: it is now THE SAME OBJECT as SECURING_VERBS, not a second
# derivation of it. This line used to read `not v.startswith(("un", "dis"))`
# -- the same answer today, by a looser rule: it asks how a word is SPELT,
# where SECURING_VERBS asks whether the rest of the word is itself a
# securement verb. The two agree on all seventeen verbs in the set, and would
# part company the first time somebody added one -- "unseat", "disable" --
# that merely begins with those letters. Two rules that happen to agree are
# the shape every drift in this codebase has started from, so there is one.
LOCK_VERBS: FrozenSet[str] = SECURING_VERBS

# Words that carry the ACTION rather than name a target. A command built only
# from these (plus stopwords, pronouns and bare numbers) has named nothing and
# is therefore a genuine back-reference.
#
# Distinct from the private _ACTION_WORDS used by is_query() above, which is a
# different set for a different job -- resolver had its own module-level
# `_ACTION_WORDS` too, so the same name meant two things in two files.
BARE_COMMAND_WORDS: FrozenSet[str] = frozenset({
    "turn", "switch", "set", "make", "put", "on", "off", "to", "at", "please",
})

# "Put it back." Matched as a whole utterance or as a prefix, so "undo that"
# counts and "don't undo that" does not.
UNDO_PHRASES: Tuple[str, ...] = (
    "undo", "cancel that", "wrong one", "no wait", "no not that one",
    "revert that", "never mind", "nevermind", "forget it",
    "scratch that", "abort",
)

# Politeness that may precede an undo phrase. NOT a general stopword list:
# "no" must stay out of it, because "no wait" and "no not that one" are undo
# phrases in their own right.
UNDO_LEADERS: Tuple[str, ...] = ("please", "ok", "okay", "hey")

# Escape words for an interactive prompt. "abort" is deliberately in both this
# and UNDO_PHRASES: at a prompt it escapes the prompt, as a standalone command
# there is no prompt to escape and it means undo. Same word, two contexts.
CANCEL_TOKENS: FrozenSet[str] = frozenset({"cancel", "back", "abort"})

# SPOKEN CANCEL, whole utterance (nlu/spoken_cancel.py). The fixed layer:
# short replies are judged here, longer sentences also by meaning (the
# intent index's "dismiss" examples). Kept in step with the phone's own
# list (voice_flow.dart _cancelPhrase), which answers offline.
CANCEL_PHRASES: FrozenSet[str] = frozenset({
    "cancel", "cancel that", "cancel it", "cancel this", "stop", "stop it",
    "stop that", "stop this", "no stop", "quit", "exit", "abort",
    "never mind", "nevermind", "forget it", "forget that", "go back",
    "go back home", "go home", "back to home", "back", "none", "none of them", "none of these", "scratch that",
    "wrong", "that is wrong", "no that is wrong", "not that", "not that one",
    "i did not say that", "i never said that", "that is not what i said",
    "that is not what i meant", "i did not mean that", "you heard me wrong",
    "you got that wrong", "no no", "no no no",
})
#: Dropped before the whole-utterance check: "please stop", "ok cancel
#: that now", "hey, go back".
CANCEL_FILLERS: FrozenSet[str] = frozenset({
    "please", "ok", "okay", "hey", "now", "just", "oh", "um", "uh", "erm",
    "actually", "wait", "sorry",
})

# Words that can follow "in the ..." but are never part of a room name. Used to
# truncate a room candidate: without them "turn it on in the kitchen please"
# yielded the room "kitchen please".
ROOM_STOP_WORDS: FrozenSet[str] = frozenset({
    "here", "there", "morning", "evening", "night", "afternoon",
    "and", "then", "please", "also", "too", "now", "a", "an", "the",
    "it", "them", "that", "this", "to", "for", "at", "on", "off",
})

# ═══════════════════════════════════════════════════════════════
# 8. "WHAT IS IT FOR?" -- a question about a device's PURPOSE
# ═══════════════════════════════════════════════════════════════
# A purpose question has its own shape: a wh-question ending on "do" or
# "for", asking how it works, naming its purpose, or asking for a
# description. Each rule is one way English asks it. "what is the fan
# doing", "what does the meter say" are state questions. Neither answer
# ever switches anything.

#: Words of a purpose question; none names a device ("what does it do" is a
#: back-reference).
PURPOSE_WORDS = frozenset({
    "what", "whats", "which", "how", "hows", "does", "do", "did", "can",
    "will", "would", "for", "used", "use", "uses", "usage", "work", "works",
    "describe", "explain", "tell", "me", "about", "purpose", "function",
    "job", "role", "point", "is", "are", "of", "i", "we", "you"})

#: Nouns that ask for a purpose ("what's the point of the fan").
_PURPOSE_NOUNS = frozenset({"purpose", "function", "job", "role", "point",
                            "use", "uses", "usage"})
_PURPOSE_WH = frozenset({"what", "whats", "which", "how", "hows", "tell"})
_PURPOSE_LEAD_TRIM = frozenset({"please", "so", "ok", "okay", "hey", "and",
                                "um", "uh", "can", "could", "would", "you",
                                "will"})


def _purpose_words(text: str) -> List[str]:
    out = []
    for w in fold_apostrophes(str(text or "").lower()).split():
        w = re.sub(r"[^\w]", "", w)
        if w:
            out.append(w)
    return out


def asks_purpose(text: str) -> bool:
    """True when the sentence asks what something does or is for ("what does
    the fan do", "what is it used for", "how does the fan work", "describe
    the lamp", "tell me about the fan"), not its state ("what is the fan
    doing", "is the fan on")."""
    words = _purpose_words(text)
    while words and words[0] in _PURPOSE_LEAD_TRIM:
        words = words[1:]
    if len(words) < 2:
        return False
    first, last = words[0], words[-1]
    # Asked to describe it.
    if first in ("describe", "explain"):
        return True
    if words[:3] == ["tell", "me", "about"]:
        return True
    if first == "tell" and "what" in words and last in ("do", "does", "for"):
        return True
    if first not in _PURPOSE_WH:
        return False
    # "how does the fan work", "how do I use the dimmer", "how is it used"
    if first in ("how", "hows"):
        return last in ("work", "works", "used") or \
            (len(words) > 3 and words[1] in ("do", "does", "can")
             and words[2] in ("i", "we", "you") and words[3] == "use")
    # "what does the fan do", "what can it do", "what will the plug do"
    if last == "do" and any(w in ("does", "do", "can", "will", "would")
                            for w in words[1:-1]):
        return True
    # "what is the fan for", "what is the sensor used for"
    if last == "for":
        return True
    # "what's the point of the fan", "what is the purpose of the dimmer"
    return any(w in _PURPOSE_NOUNS for w in words[1:])


# ── ...and by MEANING, the backup to asks_purpose ──────────────────────────
# Asked only about a QUESTION the shape rules above did not take: a sentence
# model puts "what does the fan do" next to "what is the fan doing", so the
# exact rules decide first.
#
#   * device names (and rooms) are blanked to "it" first, so the name does
#     not dominate;
#   * the sentence is weighed against purpose AND state examples;
#   * purpose wins only at config.PURPOSE_MIN_SCORE and PURPOSE_MIN_MARGIN
#     over the nearest state example; else it is a state question.
#
# One MPNet pass per question it is asked about (the examples are embedded
# once, warmed at start by shell). Both scores are logged for tuning.

#: Asking what a thing is for. "it" stands where the device's name was.
PURPOSE_EXAMPLES: Tuple[str, ...] = (
    "what does it do",
    "what is it for",
    "what is it used for",
    "what is it meant to do",
    "what is it supposed to do",
    "what is the purpose of it",
    "what is its job",
    "what is it good for",
    "why do I have it",
    "why is it here",
    "what can I do with it",
    "how does it work",
    "what kind of device is it",
    "what is it",
)

#: Asking what state it is in (the default answer).
STATE_EXAMPLES: Tuple[str, ...] = (
    "what is it doing",
    "is it on",
    "is it off",
    "is it running",
    "is it working",
    "what is it set to",
    "what level is it at",
    "what does it say",
    "what does it read",
    "what is it showing",
    "how is it",
    "what is its reading",
    "is it open",
    "how hot is it",
    "is anyone there",
)

_purpose_cache: Dict[Tuple[int, str], Any] = {}


def _unit_vec(v: Any) -> Any:
    import numpy as np
    v = np.asarray(v, dtype=np.float32).reshape(-1)
    n = float(np.linalg.norm(v))
    return v / n if n > 1e-9 else v


def _model_embed(text: str) -> Any:
    from nlu.model_module import embed
    return embed(text)


def _purpose_examples(embed_fn: Any, kind: str, texts: Sequence[str]) -> Any:
    import numpy as np
    key = (id(embed_fn), kind)
    got = _purpose_cache.get(key)
    if got is None:
        got = np.stack([_unit_vec(embed_fn(t)) for t in texts])
        _purpose_cache[key] = got
    return got


def blank_names(text: str, names: Iterable[str]) -> str:
    """Device names and rooms made "it" (with any article): "what is the
    kitchen fan meant to do" -> "what is it meant to do". Longest first."""
    out = " " + str(text or "").lower() + " "
    for n in sorted({str(n).lower().strip() for n in names if str(n).strip()},
                    key=len, reverse=True):
        out = re.sub(r"(?:\b(?:the|my|our|this|that)\s+)?\b" + re.escape(n)
                     + r"(?:'s)?\b", "it", out)
    out = re.sub(r"\b(it\s+)+it\b", "it", out)        # "kitchen" + "fan"
    return " ".join(out.split())


def purpose_by_meaning(text: str, names: Iterable[str] = (),
                       embed_fn: Any = None,
                       min_score: Optional[float] = None,
                       min_margin: Optional[float] = None) -> Dict[str, Any]:
    """{"purpose", "score", "state", "nearest", "said"}. Never raises: any
    failure is "purpose": False (the state answer)."""
    from core.config import PURPOSE_MIN_MARGIN, PURPOSE_MIN_SCORE
    from core.hv_common import event
    lo = PURPOSE_MIN_SCORE if min_score is None else min_score
    gap = PURPOSE_MIN_MARGIN if min_margin is None else min_margin
    said = blank_names(text, names)
    try:
        fn = embed_fn or _model_embed
        q = _unit_vec(fn(said))
        p = _purpose_examples(fn, "purpose", PURPOSE_EXAMPLES) @ q
        s = _purpose_examples(fn, "state", STATE_EXAMPLES) @ q
    except Exception as exc:                            # noqa: BLE001
        event("CMD", f"purpose by meaning not weighed ({exc})")
        return {"purpose": False, "score": 0.0, "state": 0.0,
                "nearest": "", "said": said}
    best, worst = float(p.max()), float(s.max())
    yes = best >= lo and best - worst >= gap
    nearest = PURPOSE_EXAMPLES[int(p.argmax())] if yes else \
        STATE_EXAMPLES[int(s.argmax())]
    event("CMD", f"what-is-it-for by meaning: {said!r} -> purpose {best:.2f}, "
                 f"state {worst:.2f} (bar {lo:.2f}, margin {gap:.2f}) -> "
                 f"{'what it is for' if yes else 'its state'} "
                 f"(nearest {nearest!r})")
    return {"purpose": yes, "score": round(best, 3), "state": round(worst, 3),
            "nearest": nearest, "said": said}


def warm_purpose(embed_fn: Any = None) -> bool:
    """Embed the purpose/state examples now, off the request path. True when
    ready."""
    try:
        fn = embed_fn or _model_embed
        _purpose_examples(fn, "purpose", PURPOSE_EXAMPLES)
        _purpose_examples(fn, "state", STATE_EXAMPLES)
    except Exception:                                   # noqa: BLE001
        return False
    return True

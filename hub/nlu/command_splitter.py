"""
command_splitter.py — Split a multi-command utterance into individual
command chunks.

Handles things like:
    "turn on the oven and set the timer to 30 minutes"
    -> ["turn on the oven", "set the timer to 30 minutes"]

Zero ML. Pure lexical rules based on English closed-class markers
(starters, question pairs, phrasal verbs, negation forms).

Wired into main.py at the very top of the pipeline: every command goes
through split_commands() first. Single-chunk inputs pass through
unchanged (returned as a single-element list).

VERB ELLIPSIS
-------------
"turn on the ac and the tv" splits on the conjunction because a known device
name follows it -- but the second chunk, "the tv", carries no verb. The
extractor then reports switch=None, build_command returns 'flip', and the TV
gets TOGGLED: if it was on, an instruction to turn it on switched it off.

People drop the repeated verb constantly ("turn on the fan and ac", "lock the
front door and the back gate"), so the chunk has to inherit it. A verbless
chunk now takes the leading verb phrase of the chunk before it.
"""

import re
from typing import Iterable, List, Optional, Set

from nlu.lexicon import (detect_negation, is_query, fold_apostrophes, QUERY_STARTERS,
                         DETERMINERS, NUMBER_WORDS, PRONOUNS, SWITCH_PARTICLES,
                         _MODIFIER_LOOKUP)

# ── STARTERS ──────────────────────────────────────────────────
STARTERS = {
    # Core actions
    "turn", "switch", "set", "dim", "brighten",
    "lock", "unlock", "open", "close", "shut",
    "arm", "disarm", "play", "pause", "stop", "perform",
    "start", "run", "activate", "deactivate", "pull", "push",
    "show", "query", "check", "put", "fetch", "clear",
    "cancel", "abort", "reset", "kill",
    "make", "get", "bring", "send", "restart",
    "create", "remove", "add", "delete", "secure", "unsecure",
    "call", "ring", "buzz", "alert", "roll", "unroll",
    "cook", "heat", "cool", "warm", "chill", "tape", "log",
    "wake", "sleep", "nap", "rest", "configure", "withdraw",
    "record", "feed", "water", "sprinkle", "pop", "unpop",
    "illuminate", "engage", "disengage", "maximize", "minimize",
    "terminate", "calibrate", "synchronize", "recalibrate",
    "retract", "depressurize", "vent", "purge", "zip", "unzip",
    "execute", "reauthenticate", "bypass", "isolate",
    "throttle", "deploy", "grant", "reconfigure", "unbolt", "bolt",
    "stream", "disable", "enable", "broadcast", "fix", "unfix",
    "charge", "discharge", "reboot", "override", "step",
    "initialize", "cleanse", "clean", "transition",
    "upload", "sound", "permit", "crank",
    "throw", "trigger", "seal", "unseal", "latch",
    "unlatch", "release", "boost", "confirm", "reconfirm",
    "extend", "limit", "reduce", "restrict", "empty",
    "drain", "steam", "pressure", "line", "go", "flush",
    "circumvent", "carry", "halt", "allow",
    "bleed", "wipe", "decontaminate",
    "fill", "provide", "redirect", "direct",
    "route", "reroute", "divert", "soft", "load",

    # Phrasal-verb first words (also valid standalone/command verbs)
    "power", "boot", "flip", "flick",

    # Modifiers
    "raise", "lower", "lift", "drop",
    "increase", "decrease",
    "adjust", "change", "modify",

    # Media / Navigation
    "resume", "unpause", "mute",
    "next", "previous", "begin",
    "skip",

    # Visibility
    "display", "reveal", "hide",

    # Information
    "verify", "scan",

    # Question words
    "what", "when", "where", "why", "how", "which",
    "is", "are", "did", "does", "can", "could", "will", "would",
    "have", "has", "had", "am", "may", "might", "must",
    "do",

    # Standalone particles
    "on", "off",

    # Misc
    "remind",
}

# The auxiliaries that take a negation. "do not" is merged to "donot" by
# preprocess() and restored after splitting; "dont" is what apostrophe
# stripping leaves of "don't".
_NEGATED_AUXILIARIES = ("do", "will", "can", "should", "could", "would",
                        "must", "did", "does", "have", "has", "had", "is",
                        "are", "was", "were", "am")

# Contraction forms
CONTRACTIONS = {
    "dont", "wont", "cant", "shouldnt", "couldnt", "wouldnt", "mustnt",
    "didnt", "doesnt", "havent", "hasnt", "hadnt", "isnt", "arent",
    "wasnt", "werent", "amnt"
}

# Merged negation forms
MERGED_NEGATIONS = {a + "not" for a in _NEGATED_AUXILIARIES}

NEGATION_TOKENS = CONTRACTIONS | MERGED_NEGATIONS

# ── QUESTION PAIRS ────────────────────────────────────────────
#: The question words. With an auxiliary straight after one, it is a
#: question (see is_question_pair).
_WH_WORDS = {"what", "when", "where", "why", "how", "which", "who"}

QUESTION_PAIRS = {
    ("why", "is"), ("why", "are"), ("why", "does"), ("why", "did"),
    ("where", "are"), ("where", "is"), ("where", "did"),
    ("when", "is"), ("when", "does"), ("when", "did"),
    ("how", "is"), ("how", "does"), ("how", "did"), ("how", "are"),
    ("what", "is"), ("what", "are"), ("what", "does"), ("what", "did"),
    ("is", "the"), ("is", "it"), ("is", "this"), ("is", "that"),
    ("are", "the"), ("are", "they"), ("are", "those"), ("are", "these"),
    ("did", "the"), ("did", "it"), ("did", "you"), ("did", "i"),
    ("do", "the"), ("do", "you"), ("do", "i"),
    ("does", "the"), ("does", "it"), ("does", "this"),
    ("can", "you"), ("can", "the"), ("can", "i"),
    ("could", "you"), ("could", "the"),
    ("will", "the"), ("will", "it"), ("will", "you"),
    ("would", "you"), ("would", "the"), ("are", "you"),
    ("whats", "on"), ("whats", "in"), ("whats", "for"),
    ("what", "on"), ("what", "in"), ("what", "for"),
}

# ── SUBJECT & AUXILIARY SETS ──────────────────────────────────
SUBJECT_WORDS = {
    "i", "you", "he", "she", "it", "we", "they",
    "me", "him", "her", "us", "them",
    "someone", "somebody", "anyone", "anybody",
    "everyone", "everybody", "noone", "nobody", "all", "both",
    "something", "anything", "everything", "nothing",
    "the", "a", "an", "my", "your", "his",
    "our", "their", "this", "that", "these", "those",
    "one", "two", "three", "1", "2", "3",
    "weather", "temperature", "thermostat", "calendar",
    "music", "movie", "tv", "television", "lights", "door",
    "garage", "kitchen", "plants", "cat", "mom", "dishwasher",
    "room", "house", "home", "office", "bedroom", "bathroom",
    "mode", "panic", "alarm", "state", "status",
    "sensor", "zone", "area",
    "window", "path", "screen", "cache", "log",
}

#: The modal auxiliaries (closed-class). A verb straight after one of these,
#: or after a negated auxiliary ("can't", "cannot", "won't"), belongs to it:
#: "you can't TALK now" is one sentence, not "you can't" | "talk now".
MODALS = {"can", "could", "will", "would", "shall", "should", "may", "might",
          "must"}
#: Subject pronouns (closed-class): "i WILL", "we CAN".
_SUBJECT_PRONOUNS = {"i", "you", "he", "she", "it", "we", "they"}

QUESTION_AUXILIARIES = {
    "did", "does", "do", "is", "are", "was", "were",
    "have", "has", "had", "can", "could", "will", "would",
    "shall", "should", "may", "might", "must",
    "what", "when", "where", "why", "how", "which",
}

# ── AMBIGUOUS / NOUN-LIKE STARTERS ───────────────────────────
NOUN_LIKE_STARTERS = {
    "display", "stream", "feed", "water", "heat", "cool", "chill", "start", "restart",
    "sleep", "rest", "record", "switch", "lock", "zip", "unzip", "tape", "log",
    "clear", "scan", "power", "sync", "sound", "roll", "unroll", "play", "boost",
    "clean", "wake", "ring", "seal", "latch", "trigger", "crank", "step",
    "withdraw", "configure", "secure", "unsecure", "push", "pull", "limit",
    "drain", "empty", "bypass", "steam", "pressure", "line", "carry", "halt",
    "bleed", "permit", "wipe", "fill", "direct", "route", "soft", "load",
    "next", "previous",
}

FUNCTION_WORDS = {
    "the", "a", "an", "and", "or", "but", "then", "to", "of", "in", "on", "at",
    "by", "with", "from", "for", "as", "if", "than", "when", "where", "how",
    "so", "yet", "nor", "not", "no", "yes",
}

NOUN_COMPOUND_PAIRS = {
    ("smart", "display"), ("smart", "lock"), ("smart", "switch"),
    ("audio", "stream"), ("video", "stream"), ("video", "display"),
    ("water", "refill"), ("water", "tank"), ("water", "level"),
    ("heat", "pump"), ("news", "feed"), ("pet", "feed"),
    ("plant", "water"), ("music", "stream"), ("light", "switch"),
    ("power", "switch"), ("security", "camera"), ("security", "system"),
    ("robotic", "arm"), ("media", "play"), ("check", "log"), ("soft", "start"),
    ("soft", "stop"),
}

#: Words a sentence opens with before the QUESTION it asks: "let me SEE what is
#: in layout", "tell me what is on", "i wonder what the time is".
_ASK_LEADS = {"let", "me", "us", "see", "tell", "show", "know", "wonder", "find",
              "out", "i", "want", "to", "would", "like", "can", "could", "you",
              "please", "need", "check", "say", "ask"}

#: "is" and its kin when they link a subject to what is said about it.
COPULAS = {"is", "are", "was", "were"}

PHRASAL_VERBS = {"turn", "switch", "shut", "power", "boot", "flip",
                 "flick", "step", "charge", "discharge", "go", "carry",
                 }
PHRASAL_PARTICLES = {"on", "off", "down", "up"}

SPLIT_TRIGGERS = {"and", "then", "also", "so", "or", "plus",
                  "additionally", "moreover", "furthermore", "meanwhile",
                  "afterwards", "next", "subsequently"}

# Words that naturally follow a state verb/adjective at the end of a question
STATE_FOLLOWERS = {
    "", "and", "then", "also", "so", "or",
    "to", "for", "at", "in", "on", "by", "with", "from",
}

# ── IMPLICIT CLAUSE STARTERS (after apostrophe stripping) ─────
CLAUSE_STARTERS = {"its", "thats", "theres", "heres"}
CLAUSE_CONTINUATIONS = DETERMINERS | {"been", "not", "just", "only", "already", "still", "probably"}

# ── RESTORATION MAP ───────────────────────────────────────────
# Every stripped form back to its written one, applied in one pass: the
# merged negations to their two words ("cannot" -> "can not"), the
# contractions to their apostrophe ("dont" -> "don't").
RESTORE = {
    **{a + "not": a + " not" for a in _NEGATED_AUXILIARIES},
    **{c: c[:-1] + "'t" for c in CONTRACTIONS},
    "whats": "what's", "lets": "let's",
    "its": "it's", "thats": "that's", "theres": "there's", "heres": "here's",
}
_RESTORE_RE = re.compile(r'\b(' + '|'.join(RESTORE) + r')\b')
_NEGATION_MERGE_RE = re.compile(
    r'\b(' + '|'.join(_NEGATED_AUXILIARIES) + r')\s+not\b')

# Filler clauses stripped from every chunk. (Sentence-leading polite frames
# like "i would like to" are stripped earlier in preprocess(), before the
# splitter can break them across chunks.) "don'?t": the chunks are already
# RESTOREd when this runs, so a bare "dont" never reaches it.
FILLER_RE = re.compile(
    r"\b(?:by the way|after that|finally|also|please|then|so|don'?t forget to"
    r"|do not forget to|can you tell me|could you tell me)\b[\s,]*",
    re.IGNORECASE)
# The same words, plus a dangling conjunction, at the END of a chunk. As one
# repeated group: stripped one pattern at a time, "turn on the light, and
# please" lost "please" only after the "and" pattern had already run, and
# kept the "and".
_TRAILING_FILLER_RE = re.compile(
    r'(?:\s+(?:and|then|also|so|please|or|after that|by the way|finally))+\s*$',
    re.IGNORECASE)
_SENTENCE_BOUNDARY_RE = re.compile(r'([.!?]+)\s+')
# A conjunction left at the FRONT of a chunk once filler around it is gone
# ("so, and then close the gate") joins nothing.
_LEADING_CONJUNCTION_RE = re.compile(r'^(?:(?:and|or|plus)\b[\s,]*)+')


def preprocess(text: str) -> str:
    text = fold_apostrophes(text.lower().strip())
    # Strip sentence-leading polite frames before splitting, so the splitter
    # cannot break "i would like to ..." across chunks.
    text = re.sub(
        r'^\s*(?:i would like to|id like to|i want to)\b[\s,]*', '', text)
    return _NEGATION_MERGE_RE.sub(lambda m: m.group(1) + "not", text)


def _clean_word(word: str) -> str:
    return re.sub(r"[^\w]", "", word)


VERB_ENDINGS = ("ing", "ed")


def _has_main_verb_in_question(chunk_words):
    if len(chunk_words) < 3:
        return False
    for w in chunk_words[1:]:
        cw = _clean_word(w)
        if cw in STARTERS and cw not in QUESTION_AUXILIARIES:
            return True
        if cw.endswith(VERB_ENDINGS) and len(cw) > 4 and cw not in FUNCTION_WORDS:
            return True
    return False


def _leading_verb_phrase(chunk: str, starters=STARTERS) -> List[str]:
    """The command verb a chunk opens with, plus its particle if phrasal.

    "turn on the ac" -> ["turn", "on"];  "lock the door" -> ["lock"];
    "the tv" -> [] (no verb to lend).  Question auxiliaries do not count --
    "is the door open" lends nothing to a following chunk. `starters`: the
    sentence's verbs (split_commands), the hand list by default.
    """
    words = chunk.split()
    if not words:
        return []
    first = _clean_word(words[0])
    if first not in starters or first in QUESTION_AUXILIARIES:
        return []
    lead = [words[0]]
    if (len(words) > 1 and first in PHRASAL_VERBS
            and _clean_word(words[1]) in PHRASAL_PARTICLES):
        lead.append(words[1])
    return lead


#: Bare particles are STARTERS but cannot govern a clause on their own, so a
#: chunk containing only these still counts as verbless.
_BARE_PARTICLES = SWITCH_PARTICLES

#: States that take an extent: "half open", "fully closed".
_EXTENT_STATES = ("open", "close")


def _can_stand_alone(chunk: str, starters=STARTERS) -> bool:
    """Does this chunk already carry a word that could govern it?

    ANY starter counts, not just command verbs -- a question auxiliary means
    the chunk is a question ("is the door locked"), and prefixing a verb to a
    question produces nonsense ("turn on is the door locked"). Bare on/off are
    excluded: they are particles, not governors.
    """
    words = chunk.split()
    # "what's ..." is a question too, though its cleaned "whats" is no STARTER.
    return bool(words) and _clean_word(words[0]) in QUERY_STARTERS or any(
        _clean_word(w) in starters and _clean_word(w) not in _BARE_PARTICLES
        for w in words)


def _carry_leading_verb(chunks: List[str], name_heads: Set[str],
                        starters=STARTERS) -> List[str]:
    """Give a verbless chunk the verb of the chunk before it.

    Only forward, only from the immediately preceding chunk, and only when the
    receiving chunk has no command verb of its own -- so "set the ac to 20 and
    turn off the fan" is untouched, while "turn on the ac and the tv" becomes
    ["turn on the ac", "turn on the tv"].
    """
    out: List[str] = []
    for chunk in chunks:
        if out and not _can_stand_alone(chunk, starters):
            lead = _leading_verb_phrase(out[-1], starters)
            if lead:
                chunk = " ".join(lead) + " " + chunk
            elif is_query(out[-1]) and _clean_word(chunk.split()[0]) in (
                    DETERMINERS | name_heads):
                # A QUESTION LENDS NO VERB, and a verbless chunk run on its
                # own is a TOGGLE: "is the front door locked and the back
                # gate" flipped the back gate. A second thing named after a
                # question stays part of the question.
                out[-1] += " and " + chunk
                continue
        out.append(chunk)
    return out


# ── VERBS READ BY GRAMMAR ─────────────────────────────────────
# STARTERS above is a hand list, so an order whose verb is not on it was
# never cut from the one before it: "turn on the lamp IGNITE the heater",
# "dim the lamp TOGGLE the fan" ran as one command and the second device was
# never touched. _verbs_of reads which words are verbs in THIS sentence from
# the parse (nlu/grammar.py) and adds them to STARTERS for it -- the list
# stays, grammar only adds. Grammar off: the list alone, as before.

#: What follows an order's verb: its object, or a particle then the object.
_OBJECT_POS = frozenset({"DET", "PRON", "NUM"})
#: Where a clause begins: the start, after a comma or SPLIT_TRIGGERS, or (by
#: grammar) after a conjunction, infinitive "to" or interjection.
_OPENING_POS = frozenset({"CCONJ", "PART", "INTJ"})

_verbs_cache: dict = {}


def _verbs_of(text: str, names: Iterable[str] = ()) -> frozenset:
    """The words that are ORDER VERBS in `text`, read by grammar (not a list).

    A base-form verb (VB) here ("IGNITE the heater"), or -- since the small
    model misreads bare orders -- a word opening a clause and followed by its
    object (determiner/pronoun/number, or up/down/on/off then the object), or
    a word straight before a determiner that is not itself after one ("...
    DIM the lamp"). A word of a house name said as that name ("the water
    HEATER") is not, unless its object follows. Grammar off: empty.
    """
    text = fold_apostrophes(str(text or "").lower())
    key = (text, frozenset(names))
    hit = _verbs_cache.get(key)
    if hit is not None:
        return hit
    from nlu import grammar
    if not (grammar.ENABLED and grammar.load()):
        return frozenset()
    words = text.split()
    cleaned = [_clean_word(w) for w in words]
    out: Set[str] = set()
    # Indices of every house name said in full.
    in_name: Set[int] = set()
    for n in names:
        nw = [t for t in (_clean_word(x) for x in fold_apostrophes(n).split()) if t]
        if not nw:
            continue
        for j in range(len(cleaned) - len(nw) + 1):
            if cleaned[j:j + len(nw)] == nw:
                in_name.update(range(j, j + len(nw)))

    tags: dict = {}
    complement: Set[int] = set()
    starts, at = {}, 0
    for k, w in enumerate(words):
        starts[at] = k
        at += len(w) + 1
    try:
        doc = grammar._parse(" ".join(words))
    except Exception:                                   # noqa: BLE001
        doc = None
    for tok in (doc or ()):
        k = starts.get(tok.idx)
        if k is None:
            continue
        tags[k] = (tok.tag_, tok.pos_)
        # "let me SEE", "make it STOP": a verb after a verb and its pronoun
        # is what that verb asks for -- one order.
        head = tok.head
        if (tok.dep_ in ("ccomp", "xcomp") and head.i < tok.i
                and head.pos_ == "VERB" and tok.i - head.i <= 3
                and all(t.pos_ == "PRON" for t in doc[head.i + 1:tok.i])
                and tok.i - head.i > 1):
            complement.add(k)

    def pos(k: int) -> str:
        return tags.get(k, ("", ""))[1]

    def opens(k: int) -> bool:
        return (k == 0 or cleaned[k - 1] in SPLIT_TRIGGERS
                or pos(k - 1) in _OPENING_POS or words[k - 1].endswith(","))

    def object_after(k: int) -> bool:
        """Is word k followed by its object?"""
        nxt = cleaned[k + 1] if k + 1 < len(cleaned) else ""
        if pos(k + 1) in _OBJECT_POS or nxt in PRONOUNS or nxt in DETERMINERS:
            return True
        after = cleaned[k + 2] if k + 2 < len(cleaned) else ""
        return nxt in PHRASAL_PARTICLES and (
            pos(k + 2) in _OBJECT_POS or after in DETERMINERS or after in PRONOUNS)

    closed = {"DET", "PRON", "ADP", "AUX", "CCONJ", "SCONJ", "PART", "NUM",
              "INTJ", "PUNCT"}
    for k, c in enumerate(cleaned):
        if not c or c in DETERMINERS or c in PRONOUNS:
            continue
        tag, kind = tags.get(k, ("", ""))
        named = k in in_name
        if k in complement:
            continue
        if tag == "VB" and (not named or object_after(k)):
            out.add(c)
        elif named or kind in closed or kind == "ADV":
            continue
        elif opens(k) and object_after(k):
            out.add(c)
        elif (k > 0 and kind != "ADJ" and cleaned[k - 1] not in DETERMINERS
              and k + 1 < len(cleaned) and cleaned[k + 1] in DETERMINERS
              and cleaned[k + 1] not in PRONOUNS):
            # "... DIM the lamp": a word straight before a determiner (not
            # after one) takes an object; a thing never does. Not an
            # adjective ("make SURE the door ...").
            out.add(c)
    res = frozenset(out)
    if len(_verbs_cache) > 512:
        _verbs_cache.clear()
    _verbs_cache[key] = res
    return res


def split_commands(text: str, known_rooms: Optional[Iterable[str]] = None,
                   known_devices: Optional[Iterable[str]] = None) -> List[str]:
    """Split a multi-command utterance into a list of individual command
    strings. Returns [text] unchanged if only one command is detected.

    `known_rooms` / `known_devices` are optional sets of names from the device
    registry. When provided, 'and'/'or'/'plus'/'then' followed within a few
    words by a room OR a device name also triggers a split — this catches
    parallel commands like 'bedroom lights and living room lights' (room-named)
    and bare-name pairs like 'ac and tv' where neither side has a starter verb."""
    known_rooms = {r.lower() for r in (known_rooms or [])}
    known_devices = {d.lower() for d in (known_devices or [])}
    # A NEGATED UTTERANCE IS NEVER SPLIT. The chunk loop below used to cut
    # "don't you dare unlock the front door" into ["don't you dare",
    # "unlock the front door"], and the second chunk unlocked the door. The
    # whole text goes through as one chunk so lexicon.detect_negation (the
    # pre-filter main runs first) sees the negation next to its verb.
    if text and text.strip() and detect_negation(text):
        return [text.strip()]
    text = preprocess(text)
    words = text.split()
    if not words:
        return []

    cleaned_words = [_clean_word(w) for w in words]
    # A name is matched by its FIRST word, cleaned the way the utterance's
    # words are -- raw, "bob's lamp" had the head "bob's", which the cleaned
    # word "bobs" never equalled.
    name_heads = {_clean_word(fold_apostrophes(n.split()[0]))
                  for n in known_rooms | known_devices if n.split()}
    device_heads = {_clean_word(fold_apostrophes(d.split()[0]))
                    for d in known_devices if d.split()}

    # WORDS INSIDE A KNOWN MULTI-WORD NAME NEVER START A NEW CHUNK. "turn on
    # the salt and pepper lamp" was cut at "and" (a device head followed it)
    # and "the rock and roll lamp" at "roll" (a STARTER), so neither device
    # could be named in a sentence at all. Every word of a name after its
    # first is marked here and appended to the chunk in progress verbatim.
    protected = set()
    for name in known_rooms | known_devices:
        nw = [_clean_word(t) for t in fold_apostrophes(name).split()]
        nw = [t for t in nw if t]
        if len(nw) < 2:
            continue
        for j in range(len(words) - len(nw) + 1):
            if cleaned_words[j:j + len(nw)] == nw:
                protected.update(range(j + 1, j + len(nw)))

    # The words that open an order in THIS sentence: the hand list, plus the
    # verbs grammar reads in it ("... IGNITE the heater"). See _verbs_of.
    starters = STARTERS | _verbs_of(text, known_rooms | known_devices)

    chunks = []
    current = []
    # `pending_negation` USED TO LIVE HERE and was removed 2026-09-14: it
    # was initialised to None and re-assigned only to None, so the three
    # `if pending_negation:` guards that consumed it -- one joining a
    # negator to the following word, two flushing it into `current` --
    # could never run. Nine lines that read like the negation handling and
    # were not it. The live handling is the whole-utterance check above and
    # the `is_negation` branch below, both of which work.

    for i, word in enumerate(words):
        clean = cleaned_words[i]
        prev_clean = cleaned_words[i - 1] if i > 0 else ""
        next_clean = cleaned_words[i + 1] if i + 1 < len(words) else ""

        if not clean or (i in protected and current):
            current.append(word)
            continue

        is_negation = clean in NEGATION_TOKENS
        is_starter = clean in starters
        # A question word and the auxiliary straight after it are one
        # question, whichever two they are ("what do", "what have", "which
        # will") -- the table above lists some and "what do I have" was cut
        # into 'what' | 'do i have' for not being on it.
        is_question_pair = (prev_clean, clean) in QUESTION_PAIRS or (
            prev_clean in _WH_WORDS and clean in QUESTION_AUXILIARIES
            and clean not in _WH_WORDS)
        is_purpose_infinitive = prev_clean == "to" and is_starter and len(current) >= 2
        prev_is_starter = prev_clean in starters

        is_phrasal_particle = (
            clean in PHRASAL_PARTICLES
            and prev_clean in PHRASAL_VERBS
            and current
        )

        # A starter, or a known room/device name, in the next few words.
        # "what time AND DATE IS IT": the only starter after the conjunction
        # is the copula of the same question, so there is no second command.
        is_question_continuation = bool(
            current and _clean_word(current[0]) in QUESTION_AUXILIARIES
            and next_clean not in starters and next_clean not in name_heads
            and not any((c in starters and c not in COPULAS) or c in name_heads
                        for c in cleaned_words[i + 1:i + 5]))
        is_conjunction_before_starter = (
            clean in SPLIT_TRIGGERS
            and any(c in starters or c in name_heads
                    for c in cleaned_words[i + 1:i + 5])
            and current
            and not is_question_continuation
        )

        if is_negation:
            # Kept in the chunk it appears in, never flushed into a chunk of
            # its own: a stray "don't" chunk carried no verb and the command
            # it governed ran in the next one. (A negation that governs a
            # command verb never reaches here -- see the whole-utterance
            # check at the top.)
            current.append(word)
            continue

        if clean in CLAUSE_STARTERS and next_clean in CLAUSE_CONTINUATIONS and current:
            if len(current) >= 3 and any(_clean_word(w) in starters for w in current):
                chunks.append(" ".join(current))
                current = [word]
                continue

        if is_starter:
            if is_phrasal_particle:
                current.append(word)
                continue

            # A verb after its own modal or negated auxiliary ("can't TALK",
            # "will SLEEP"), or a modal after its subject ("i WILL"), is the
            # same clause.
            if current and not is_question_pair and (
                    prev_clean in MODALS or prev_clean in NEGATION_TOKENS
                    or (clean in MODALS and prev_clean in _SUBJECT_PRONOUNS)):
                current.append(word)
                continue

            # "make the door HALF OPEN", "50 percent open", "fully open": an
            # extent before open/close is how far, not the end of a command.
            # Split there, "open" ran on its own and opened the door fully.
            # Not across a comma, and not when open/close takes an object of
            # its own ("set it to 5 open the door").
            if (clean in _EXTENT_STATES and current and words[i - 1][-1].isalnum()
                    and next_clean not in DETERMINERS and next_clean not in PRONOUNS
                    and (prev_clean in NUMBER_WORDS or prev_clean.isdigit()
                         or prev_clean == "percent"
                         or _MODIFIER_LOOKUP.get(prev_clean, ("comparative",))[0]
                         != "comparative")):
                current.append(word)
                continue

            current_first = _clean_word(current[0]) if current else ""
            in_question = current_first in QUESTION_AUXILIARIES and len(current) >= 2

            # A bare on/off inside a question ("what is ON the tv") is part
            # of it: split there, "on the tv" ran as a verbless toggle.
            if clean in _BARE_PARTICLES and current:
                prev_is_prep = prev_clean in {"from", "to", "at", "in", "into", "onto"}
                current_has_verb = any(
                    _clean_word(w) in starters
                    and _clean_word(w) not in QUESTION_AUXILIARIES
                    and _clean_word(w) not in _BARE_PARTICLES
                    for w in current
                )
                # "FAN ON", "the ceiling light OFF": the particle that ends
                # the clause is what is asked of the thing just named. Cut
                # there, 'fan' ran alone (a bare name, which flips it) and
                # then 'on' ran against "the last thing touched".
                ends_clause = not next_clean or next_clean in SPLIT_TRIGGERS
                if (prev_is_prep or current_has_verb or in_question
                        or ends_clause) and not is_question_pair:
                    current.append(word)
                    continue

            prev_is_subject = prev_clean in SUBJECT_WORDS
            has_main_verb = in_question and _has_main_verb_in_question(current)

            if in_question and prev_is_subject and not is_question_pair and not has_main_verb:
                current.append(word)
                continue

            if in_question and clean in {"is", "are", "was", "were", "does", "did", "do", "have", "has", "had"}:
                current.append(word)
                continue

            if in_question and next_clean in STATE_FOLLOWERS and not is_question_pair and not has_main_verb:
                current.append(word)
                continue

            prev_is_determiner = prev_clean in DETERMINERS
            if prev_is_determiner and (clean in NOUN_LIKE_STARTERS
                                       or clean not in STARTERS) and not is_question_pair:
                current.append(word)
                continue

            if (prev_clean, clean) in NOUN_COMPOUND_PAIRS:
                current.append(word)
                continue

            prev_is_content = (
                (prev_clean not in starters or prev_clean in NOUN_LIKE_STARTERS)
                and prev_clean not in FUNCTION_WORDS
                and len(prev_clean) > 2
            )
            next_is_determiner = next_clean in DETERMINERS
            is_phrasal_verb = clean in PHRASAL_VERBS and next_clean in PHRASAL_PARTICLES
            is_likely_noun = (
                clean in NOUN_LIKE_STARTERS
                and prev_is_content
                and not next_is_determiner
                and not is_phrasal_verb
                and not is_question_pair
            )
            if is_likely_noun:
                current.append(word)
                continue

            if is_question_pair and current:
                current.append(word)
                continue

            if is_purpose_infinitive:
                current.append(word)
                continue

            # A COPULA STAYS WITH ITS SUBJECT, AND WHAT IS SAID OF IT STAYS
            # WITH THE COPULA ("the time is what", "the heater is on", "the
            # door is locked"). Nothing in the chunk so far is a command verb,
            # so nothing is being commanded: "is" is not the start of a new
            # question, and the word after it is a state, not a new verb.
            # Cut at each, "and the time is what?" became 'the time' | 'is' |
            # 'what', and every piece was run on its own.
            if current and not any(
                    _clean_word(w) in starters
                    and _clean_word(w) not in QUESTION_AUXILIARIES
                    and _clean_word(w) not in _BARE_PARTICLES
                    for w in current) and (
                    clean in COPULAS or prev_clean in COPULAS):
                current.append(word)
                continue

            # "let me see WHAT is in layout": the question belongs to the lead
            # that asks it, not a second command.
            if (current and clean in QUESTION_AUXILIARIES
                    and all(_clean_word(w) in _ASK_LEADS for w in current)):
                current.append(word)
                continue

            # AN AUXILIARY WITH NOTHING AFTER IT STARTS NOTHING ("list the
            # devices we HAVE", "tell me the devices i HAVE and turn on the
            # fan"): it closes the clause it is in. Cut there, "have" ran
            # as a command of its own and failed the whole sentence.
            if (current and clean in QUESTION_AUXILIARIES
                    and (not next_clean or next_clean in SPLIT_TRIGGERS)):
                current.append(word)
                continue

            if prev_is_starter and current:
                chunks.append(" ".join(current))
                current = [word]
                continue

            last_word = _clean_word(current[-1]) if current else ""
            current_has_verb = any(
                _clean_word(w) in starters
                and _clean_word(w) not in QUESTION_AUXILIARIES
                for w in current)
            is_command_verb = clean not in QUESTION_AUXILIARIES
            ends_with_noun = ((last_word in SUBJECT_WORDS or last_word in NOUN_LIKE_STARTERS)
                              and last_word not in DETERMINERS)
            if current and not current_has_verb and ends_with_noun and is_command_verb and not is_question_pair:
                current.append(word)
                continue

            # The mirror of the rule above. A QUESTION AUXILIARY ("is", "are",
            # "does", "did"...) cannot begin a new command when the chunk so
            # far already contains a real command verb -- it is part of the
            # sentence in progress, not the start of another instruction.
            #
            # Without this, "turn on the light. i think that is all" split at
            # "is", leaving the fragment "is all" as its own chunk. That
            # fragment then went to the resolver as a command, matched
            # nothing, and _run_multi reported the WHOLE utterance as failed
            # -- because one piece of trailing chatter had become a command
            # that could not succeed.
            if (current and current_has_verb and not is_question_pair
                    and clean in QUESTION_AUXILIARIES):
                current.append(word)
                continue

            # 7c — BUG FIX: original was `current == [0] == 'lets'` which is
            # always False (comparing a list to int 0). The intent was to
            # check whether the single-word chunk so far is "lets", i.e. the
            # start of "let's do X". Now correctly reads current[0].
            if current and len(current) == 1 and current_first == "lets":
                current.append(word)
                continue

            if (current and len(current) == 1
                    and current_first in {"it", "there", "here", "this", "that"}
                    and clean in {"is", "are", "was", "were"}):
                current.append(word)
                continue

            if current:
                chunks.append(" ".join(current))
            current = [word]
            continue

        if is_conjunction_before_starter:
            if current:
                chunks.append(" ".join(current))
                current = []
            continue

        current.append(word)

    if current:
        chunks.append(" ".join(current))

    # ── POST-PROCESS: CLEAN UP CHUNKS ─────────────────────────
    # (Leading filler is not stripped here: the third pass removes the same
    # words wherever they sit.)
    cleaned = []
    for chunk in chunks:
        chunk = _RESTORE_RE.sub(lambda m: RESTORE[m.group(1)], chunk)
        chunk = re.sub(r'\s*,\s*$', '', chunk.strip())
        chunk = _TRAILING_FILLER_RE.sub('', chunk).strip(' ,.;:!?')
        # Keep any chunk with at least one word character. The old
        # len>2 filter dropped valid 2-char device names like "ac" / "tv".
        if re.search(r"\w", chunk):
            cleaned.append(chunk)

    # ── SECOND PASS: SPLIT ON SENTENCE BOUNDARIES ────────────
    final_chunks = []
    for chunk in cleaned:
        # One capture group, so parts is [text, punct, text, punct, ...,
        # text]: every punct has a following text.
        parts = _SENTENCE_BOUNDARY_RE.split(chunk)
        current_part = parts[0]
        for j in range(1, len(parts), 2):
            current_part += parts[j]
            following_words = parts[j + 1].strip().split()
            if not following_words:
                continue
            final_chunks.append(current_part.strip(' ,.;:!?'))
            first_word = following_words[0].lower().strip('.,!?;')
            # COMMAND verbs only. STARTERS also holds question
            # auxiliaries ("is", "does", "did"), so trailing
            # chatter like "i think that is all" looked like the
            # start of a new instruction and was kept.
            has_starter_soon = any(
                _clean_word(w) in starters
                and _clean_word(w) not in QUESTION_AUXILIARIES
                for w in following_words[:4])
            is_chatter = first_word in {"i", "you", "we", "he", "she", "they", "by", "after", "finally", "also"} and not has_starter_soon
            current_part = "" if is_chatter else parts[j + 1]
        if current_part.strip():
            final_chunks.append(current_part.strip(' ,.;:!?'))

    # ── THIRD PASS: STRIP FILLER FROM START OF EACH CHUNK ────
    result = []
    for chunk in final_chunks:
        chunk = _LEADING_CONJUNCTION_RE.sub('', FILLER_RE.sub('', chunk).strip(' ,.;:!?'))
        if re.search(r"\w", chunk):
            result.append(chunk)

    # ── CARRY THE VERB INTO VERBLESS CHUNKS ──────────────────
    # See VERB ELLIPSIS in the module docstring. Splitting on a conjunction
    # that is followed by a bare device or room name is deliberate -- it is
    # what makes "turn on the fan and ac" two commands -- but it leaves the
    # second chunk without the verb that governs it, and a verbless chunk is
    # executed as a TOGGLE. "Turn on X and Y" would turn Y off whenever Y was
    # already on.
    result = _carry_leading_verb(result, name_heads, starters)

    # ── MERGE SHORT TAIL ─────────────────────────────────────
    # A short trailing chunk is only a fragment worth merging if it does
    # NOT start with a command verb and is NOT a known device name. "open it",
    # "mute it", "turn off" are valid short commands; "ac"/"tv" are valid bare
    # device names ("turn on the fan and ac"); merging any of these back into
    # the previous chunk would destroy a real command. Only merge a genuine
    # stray like "please", "now", "thanks".
    if len(result) >= 2:
        last_words = result[-1].split()
        first = _clean_word(last_words[0]) if last_words else ""
        starts_with_verb = first in starters
        is_device_tail = any(_clean_word(w) in device_heads for w in last_words)
        if len(last_words) < 3 and not starts_with_verb and not is_device_tail:
            result[-2] = result[-2] + " " + result[-1]
            result.pop()

    return result
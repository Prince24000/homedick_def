"""Grammar (spaCy) for reading sentences: whether a sentence asks about one
thing or several (asks_for_group), and whether it is a question wherever it
starts (asks). The command splitter also reads which words are verbs from
its parse. spaCy and its small English model are requirements of the hub
(setup_manager)."""
from __future__ import annotations

import os
import re
import threading
from typing import Any, Dict

#: Off with HOMEVOICE_GRAMMAR=0 (or grammar.ENABLED = False), to compare.
ENABLED = os.environ.get("HOMEVOICE_GRAMMAR", "1") != "0"
#: Grammar model: sm = en_core_web_sm (12 MB, fast, default); trf =
#: en_core_web_trf (better, hundreds of MB, slower). HOMEVOICE_GRAMMAR_MODEL
#: picks; if unavailable the small one is used and logged.
_CHOICES = {"sm": "en_core_web_sm", "small": "en_core_web_sm",
            "trf": "en_core_web_trf", "big": "en_core_web_trf"}
_SMALL = "en_core_web_sm"
MODEL = _SMALL                      # which one is loaded (set by load())


def chosen_model() -> str:
    """The model the hub's settings ask for (HOMEVOICE_GRAMMAR_MODEL, read
    from ~/.homevoice/env by core.config)."""
    try:
        import core.config  # noqa: F401  (reads the env file)
    except Exception:                                   # noqa: BLE001
        pass
    want = os.environ.get("HOMEVOICE_GRAMMAR_MODEL", "sm").strip().lower()
    return _CHOICES.get(want, want or _SMALL)

_nlp: Any = None
_tried = False
_why_off = ""
_lock = threading.Lock()

def load() -> bool:
    """Load the model once. False (and why, in off_reason()) if it can't."""
    global _nlp, _tried, _why_off
    if _nlp is not None:
        return True
    with _lock:
        if _tried:
            return _nlp is not None
        _tried = True
        global MODEL
        try:
            import spacy
        except Exception as exc:                        # noqa: BLE001
            _why_off = (f"{type(exc).__name__}: {exc}".splitlines()[0][:160]
                        + f" -- pip install spacy && python -m spacy download {_SMALL}")
            return False
        want = chosen_model()
        for name in dict.fromkeys((want, _SMALL)):
            try:
                _nlp = spacy.load(name, disable=["ner", "lemmatizer"])
                if name != want:
                    print(f"[GRAMMAR] {want} could not be loaded ({_why_off}) "
                          f"-- using {name}")
                MODEL = name
                break
            except Exception as exc:                    # noqa: BLE001
                _why_off = (f"{type(exc).__name__}: {exc}".splitlines()[0][:160]
                            + f" -- python -m spacy download {name}")
                _nlp = None
    return _nlp is not None


def off_reason() -> str:
    return "" if _nlp is not None else (_why_off or "not loaded yet")


_cache: "Dict[str, Any]" = {}
_CACHE_MAX = 256


def _parse(text: str):
    """Parse of `text`, cached: one command is parsed several times and the
    bigger model is slow."""
    t = re.sub(r"[’‘`]", "'", str(text or "").lower()).strip()
    if not t:
        return None
    with _lock:                       # one parse at a time: the model is shared
        doc = _cache.get(t)
        if doc is None:
            doc = _nlp(t)
            if len(_cache) >= _CACHE_MAX:
                _cache.pop(next(iter(_cache)))
            _cache[t] = doc
        return doc


def _ready() -> bool:
    return ENABLED and load()


# ── is a sentence a QUESTION, wherever it starts? ─────────────────────────
# A second opinion beside lexicon.is_query, which reads the first word: "hey
# is the fan still on" was taken as an order. Grammar sees "is" in front of
# what it is about, wherever the sentence starts.
#
# ONE WAY ONLY: this can make a sentence a question (read, never switched);
# it never makes a question an order, and never chooses a device or action.
# Grammar off or unsure: not a question (the first-word rules stand alone).

_WH = frozenset({"what", "which", "who", "whom", "whose", "where", "when",
                 "why", "how"})
#: Be, do, have: in front of what they are about, they ask.
_ASKING_AUX = frozenset({"is", "are", "was", "were", "am", "'s", "s",
                         "do", "does", "did", "has", "have", "had"})
_BE = frozenset({"is", "are", "was", "were", "am"})
#: "can you turn on the fan" is an order said politely: modals do not ask.
_SUBJECTS = frozenset({"nsubj", "nsubjpass", "expl"})
_WHAT_IS_SAID = frozenset({"nsubj", "nsubjpass", "expl", "attr"})
#: Words that only open a sentence ("wait, what's ...", "sorry, is ...").
_OPENERS = frozenset({"wait", "sorry", "excuse", "me", "hold", "on", "listen",
                      "look", "so", "well", "um", "uh", "hmm", "ok", "okay",
                      "right", "now", "hey", "hi", "please", ","})
#: "any" and the words English makes of it.
_ANY = frozenset({"any", "anyone", "anybody", "anything", "anywhere"})
#: "tell me IF", "i wonder WHETHER": asks how things are.
_ASK_VERBS = frozenset({"tell", "know", "wonder", "check", "see", "ask",
                        "show", "find"})


def reading(text: str) -> Dict[str, Any]:
    """{"question": bool, "why": which rule, "parse": word/kind/role ...}.
    question is False when grammar is off or unsure."""
    out: Dict[str, Any] = {"question": False, "why": "", "parse": ""}
    if not _ready():
        out["why"] = "off"
        return out
    doc = _parse(text)
    if doc is None or not len(doc):
        return out
    out["parse"] = " ".join(f"{t.text}/{t.pos_}/{t.dep_}" for t in doc)
    sent = next(iter(doc.sents))
    root = sent.root

    # 1. "IS the fan on", "hey IS the fan still on": a form of "be" opens
    #    its clause (any clause) with what it is about after it. Not "the
    #    fan IS on", "it is too hot", or an "if ... is on" clause.
    for t in sent:
        if t.lower_ in _BE and (t == root or t.left_edge.i == t.i) and any(
                c.dep_ in _WHAT_IS_SAID and c.i > t.i
                and (c.head == t or c.head.head == t) for c in t.subtree):
            out.update(question=True, why="'is' before what it is about")
            return out
    # 2. "DID i leave the heater on", "what'S the fan doing": be/do/have
    #    before the subject, of the main verb or opening its own clause.
    for k in sent:
        if k.dep_ not in ("aux", "auxpass") or k.lower_ not in _ASKING_AUX:
            continue
        h = k.head
        subj = [c.i for c in h.children if c.dep_ in _SUBJECTS]
        # "HAVE the fan turn on" is an order: "have" asks only before a past
        # participle.
        if k.lower_ in ("have", "has", "had") and h.tag_ != "VBN":
            continue
        if subj and k.i < min(subj) and (h == root or h.left_edge.i == k.i):
            out.update(question=True, why=f"'{k.text}' before the subject")
            return out
    # 3. "WHAT is the lamp at": a question word belonging to the main verb,
    #    ahead of it (not "when the door opens ...", a rule).
    for t in sent:
        if t.i >= root.i:
            break
        # Not an exclamation ("what a lovely day") or a clause-opening "when".
        if t.lower_ in _WH and t.dep_ != "mark" \
                and not (t.lower_ == "what" and t.dep_ == "det") \
                and t.pos_ != "SCONJ" and (
                t.head == root or (t.head.head == root
                                   and t.head.dep_ in ("acomp", "attr", "dobj",
                                                       "nsubj", "advmod"))):
            out.update(question=True, why=f"'{t.text}' asks")
            return out
    #    ...or after nothing but an opener ("wait, WHAT's the fan doing").
    for t in sent:
        if t.lower_ in _WH:
            if t.dep_ != "mark" and t.pos_ != "SCONJ" \
                    and not (t.lower_ == "what" and t.dep_ == "det") \
                    and all(o.pos_ in ("INTJ", "PROPN") or o.lower_ in _OPENERS
                            for o in sent if o.i < t.i):
                out.update(question=True, why=f"'{t.text}' asks")
                return out
            break
    # 4. "tell me IF the tv is on", "i wonder WHETHER ...". Not "let me know
    #    if the door opens" (a rule).
    for t in sent:
        v = t.head.head
        if t.lower_ in ("if", "whether") and t.dep_ == "mark" \
                and v.lower_ in _ASK_VERBS and v.head.lower_ != "let":
            out.update(question=True, why=f"'{v.text} ... {t.text}'")
            return out
    # 5. "ANY movement in the house": no verb, opens with "any" ("is there"
    #    unsaid). An order has a verb.
    words = [t for t in sent if not t.is_punct and t.lower_ not in _OPENERS]
    if words and words[0].lower_ in _ANY \
            and not any(t.pos_ in ("VERB", "AUX") for t in sent):
        out.update(question=True, why=f"'{words[0].text}' with no verb")
        return out
    return out


def asks(text: str) -> bool:
    """True when grammar says `text` is a question (never an order)."""
    try:
        return bool(reading(text)["question"])
    except Exception:                                   # noqa: BLE001
        return False                  # a broken parse never changes anything


# ── does a sentence ask about a GROUP? ─────────────────────────────────

#: The closed-class words that ask for a group with a singular word or none:
#: "every lamp", "each lamp", "everything", "the whole house". Grammar marks
#: them only as determiners/adjectives, the same as "any" or "big", so these
#: few are named (closed-class English, the one list the rules allow).
_GROUP_WORDS = frozenset({"all", "both", "every", "each", "everything",
                          "whole", "entire"})
#: Roles of the thing a command is about.
_THING = frozenset({"dobj", "nsubj", "nsubjpass", "attr", "ROOT", "npadvmod",
                    "conj", "appos", "oprd", "dative"})


def asks_for_group(text: str) -> bool:
    """Does the sentence ask about several things? By grammar: the thing it
    is about is a plural noun ("lights", "batteries", "children's lamps",
    "mice", "two lamps") or a plural pronoun ("them", "those"), or a
    closed-class group word is said ("every lamp", "the rest"). A number on
    its own is not counted: "bedroom 2 lamp" is a name, one lamp. Only the THING counts: "the lamp for the
    kids" is one lamp. Grammar is a requirement of the hub; off (as for a
    comparison run), the answer is no -- one thing, which is asked about
    rather than done to a group."""
    if not _ready():
        return False
    try:
        doc = _parse(text)
    except Exception:                                   # noqa: BLE001
        return False
    if doc is None:
        return False
    for t in doc:
        if t.lower_ in _GROUP_WORDS:
            return True
        if (t.pos_ in ("PRON", "DET") and t.dep_ in _THING
                and "Plur" in t.morph.get("Number")):
            return True
    chunks = [c for c in doc.noun_chunks if c.root.tag_ not in ("WP", "WDT", "PRP")
              or "Plur" in c.root.morph.get("Number")]
    things = [c for c in chunks if c.root.dep_ in _THING]
    if not things:
        # No object: the first thing after a preposition ("switch on
        # several lamps"), never a later one ("... for the kids").
        things = [c for c in chunks if c.root.dep_ == "pobj"][:1]
    if not things:
        nouns = [t for t in doc if t.pos_ in ("NOUN", "PROPN")]
        return bool(nouns) and (nouns[0].tag_ in ("NNS", "NNPS")
                                or "Plur" in nouns[0].morph.get("Number"))
    for c in things:
        # "a couple OF lamps", "a set OF speakers": the thing is what comes
        # after "of".
        heads = [c.root] + [w for p in c.root.children if p.lower_ == "of"
                            for w in p.children if w.dep_ == "pobj"]
        if any(h.tag_ in ("NNS", "NNPS") or "Plur" in h.morph.get("Number")
               for h in heads):
            return True
        root = c.root
        # "the rest": what is left of a group.
        if root.lower_ == "rest" and any(w.lower_ == "the" for w in c):
            return True
    return False

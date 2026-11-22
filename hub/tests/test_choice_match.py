"""Spoken-answer corpus for nlu/choice_match.match and the match_request path.

Each row is (list, what the person said, what should happen). The lists are
the shapes the hub really asks: devices with numbers, near-identical names,
rooms, people, automations, yes/no confirms and multi-selects -- and the
utterances are what a speech-to-text engine really hands over, fillers,
homophones and all.

Expected:
    ["v", ...]   commit exactly these values (order-insensitive)
    NOPICK       commit NOTHING (a question back is allowed)
    ASK(...)     commit nothing, and offer at least these back

The dangerous failures are a WRONG commit: another device, "all" when one
was meant, or anything at all when the person said cancel / none. A missed
pick only costs a second attempt.

Run: python3 tests/test_choice_match.py   (from anywhere)
"""
import asyncio
import contextlib
import io
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("HOMEVOICE_HOME", tempfile.mkdtemp())

from nlu.choice_match import match  # noqa: E402


def opts(*pairs):
    return [{"value": v, "label": l} for v, l in pairs]


NONE_ROW = (" __none__", "None of those")

L = {
    "bulbs": opts(("light.bulb_3", "Bulb 3"), ("light.bulb_2", "Bulb 2"),
                  ("switch.desk", "Desk lamp")),
    "bulbs123": opts(("b1", "Bulb 1"), ("b2", "Bulb 2"), ("b3", "Bulb 3")),
    "underscored": opts(("bulb_2", "bulb_2"), ("bulb_3", "bulb_3")),
    "lamps": opts(("l4", "Lamp 4"), ("l2", "Lamp 2"), ("l1", "Lamp 1")),
    "kitchen": opts(("kl", "kitchen light"), ("kls", "kitchen lights"),
                    ("knl", "kitchen night light"), ("hl", "hall light")),
    "rooms": opts(("kitchen", "Kitchen"), ("living", "Living room"),
                  ("bedroom", "Bedroom"), ("hall", "Hall"),
                  ("office", "Office"), ("garage", "Garage")),
    "people": opts(("chinenye", "Chinenye"), ("emeka", "Emeka"),
                   ("ada", "Ada"), ("adaeze", "Adaeze"), ("ann", "Ann"),
                   ("anna", "Anna")),
    "autos": opts(("a1", "Porch light at sunset"), ("a2", "Motion night light"),
                  ("a3", "Morning coffee"), ("a4", "Good night routine")),
    "confirm": opts(("yes", "Yes, remove it"), ("no", "No, leave it")),
    "trykey": opts(("no", "Leave it as it is"), ("yes", "Try the key")),
    "approve": opts(("yes", "Approve, with my PIN"), ("no", "Decline it")),
    "wipe": opts(("wipe", "Yes, and wipe the board"),
                 ("all", "Yes, but leave the chip alone"),
                 ("keep", "Yes, but keep its wiring here")),
    "more": opts(("yes", "Add another"), ("no", "That is everything")),
    "retry": opts(("retry", "Try again"), ("stop", "Not now")),
    "login": opts(("login", "Log in"), ("signup", "I'm new")),
    "scenes": opts(("movie time", "movie time"), ("good night", "good night"),
                   ("leaving home", "leaving home")),
    "short": opts(("socket", "Socket"), ("bulb", "Bulb"), ("buzzer", "Buzzer"),
                  ("fan", "Fan")),
    "floors": opts(("f1", "First floor light"), ("f2", "Second floor light"),
                   ("bl", "Basement light")),
    "lights": opts(("kl", "Kitchen light"), ("hl", "Hall light"),
                   ("pl", "Porch light"), ("bl", "Bedroom lamp"),
                   ("fan", "Ceiling fan")),
    "two": opts(("kl", "Kitchen light"), ("hl", "Hall light")),
    "narrow": opts(("b2", "Bulb 2"), ("b3", "Bulb 3"), NONE_ROW),
    "narrow1": opts(("bulb", "Bulb"), NONE_ROW),
    "update": opts(("device", "A device"), ("board", "A board")),
    "ok": opts(("ok", "OK")),
    "ports": opts(("/dev/ttyUSB0", "CP2102 USB to UART"),
                  ("/dev/ttyACM0", "USB JTAG serial"),
                  ("\x00none", "None of these")),
}

SINGLE = {}
MULTI = {"multi": True}
ALL = {"multi": True, "allow_all": True}
CONF = {"confirming": True}
CONF_MULTI = {"confirming": True, "multi": True, "allow_all": True}


class _Ask:
    def __init__(self, *values):
        self.values = values

    def __repr__(self):
        return f"ASK{self.values}"


def ASK(*values):
    return _Ask(*values)


NOPICK = "NOPICK"

# (list, heard, kwargs, expected)
CORPUS = [
    # ---- numbered devices: a digit in the name is not a position
    ("bulbs", "bulb two", SINGLE, ["light.bulb_2"]),
    ("bulbs", "bulb 2", SINGLE, ["light.bulb_2"]),
    ("bulbs", "Bulb 3.", SINGLE, ["light.bulb_3"]),
    ("bulbs", "two", SINGLE, ["light.bulb_2"]),
    ("bulbs", "three", SINGLE, ASK("light.bulb_3", "switch.desk")),
    ("bulbs", "number two", SINGLE, ["light.bulb_2"]),
    ("bulbs", "number three", SINGLE, ["switch.desk"]),
    ("bulbs", "number one", SINGLE, ["light.bulb_3"]),
    ("bulbs", "the first one", SINGLE, ["light.bulb_3"]),
    ("bulbs", "the second one", SINGLE, ["light.bulb_2"]),
    ("bulbs", "the third one", SINGLE, ["switch.desk"]),
    ("bulbs", "the last one", SINGLE, ["switch.desk"]),
    ("bulbs", "the 2nd one", SINGLE, ["light.bulb_2"]),
    ("bulbs", "option 3", SINGLE, ["switch.desk"]),
    ("bulbs", "the desk lamp", SINGLE, ["switch.desk"]),
    ("bulbs", "desk", SINGLE, ["switch.desk"]),
    ("bulbs", "bulb", SINGLE, ASK("light.bulb_3", "light.bulb_2")),
    ("bulbs", "the bulb", SINGLE, ASK("light.bulb_3", "light.bulb_2")),
    ("bulbs", "bulb to", SINGLE, ["light.bulb_2"]),
    ("bulbs", "bulb too", SINGLE, ["light.bulb_2"]),
    ("bulbs", "bob two", SINGLE, ["light.bulb_2"]),
    ("bulbs", "bulb four", SINGLE, NOPICK),
    ("bulbs", "the fourth one", SINGLE, NOPICK),
    ("bulbs", "five", SINGLE, NOPICK),
    ("bulbs123", "one", SINGLE, ["b1"]),
    ("bulbs123", "bulb one", SINGLE, ["b1"]),
    ("bulbs123", "bulb won", SINGLE, ["b1"]),
    ("bulbs123", "the second", SINGLE, ["b2"]),
    ("bulbs123", "the last one", SINGLE, ["b3"]),
    ("bulbs123", "bulb 1 please", SINGLE, ["b1"]),
    ("underscored", "bulb two", SINGLE, ["bulb_2"]),
    ("underscored", "bulb three", SINGLE, ["bulb_3"]),
    ("lamps", "lamp for", SINGLE, ["l4"]),
    ("lamps", "lamp four", SINGLE, ["l4"]),
    ("lamps", "lamp to", SINGLE, ["l2"]),
    ("lamps", "lamp one", SINGLE, ["l1"]),
    ("lamps", "the lamp", SINGLE, NOPICK),
    ("lamps", "for", SINGLE, ["l4"]),
    ("lamps", "number for", SINGLE, ["l4"]),
    ("lamps", "number to", SINGLE, ["l2"]),

    # ---- near-identical names
    ("kitchen", "kitchen light", SINGLE, ["kl"]),
    ("kitchen", "the kitchen light", SINGLE, ["kl"]),
    ("kitchen", "um the kitchen light", SINGLE, ["kl"]),
    ("kitchen", "uh kitchen light please", SINGLE, ["kl"]),
    ("kitchen", "kitchen lights", SINGLE, ["kls"]),
    ("kitchen", "kitchen night light", SINGLE, ["knl"]),
    ("kitchen", "the night light", SINGLE, ["knl"]),
    ("kitchen", "night light", SINGLE, ["knl"]),
    ("kitchen", "the kitchen one", SINGLE, NOPICK),
    ("kitchen", "kitchen", SINGLE, NOPICK),
    ("kitchen", "the hall one", SINGLE, ["hl"]),
    ("kitchen", "hall", SINGLE, ["hl"]),
    ("kitchen", "hallway light", SINGLE, ["hl"]),
    ("kitchen", "the second one", SINGLE, ["kls"]),
    ("kitchen", "the last one", SINGLE, ["hl"]),
    ("kitchen", "all of them", SINGLE, NOPICK),
    ("kitchen", "kitchen light and hall light", SINGLE, NOPICK),

    # ---- rooms
    ("rooms", "kitchen", SINGLE, ["kitchen"]),
    ("rooms", "the living room", SINGLE, ["living"]),
    ("rooms", "living", SINGLE, ["living"]),
    ("rooms", "lounge", SINGLE, NOPICK),
    ("rooms", "bed room", SINGLE, ["bedroom"]),
    ("rooms", "the hall", SINGLE, ["hall"]),
    ("rooms", "hole", SINGLE, ["hall"]),
    ("rooms", "my office", SINGLE, ["office"]),
    ("rooms", "garage please", SINGLE, ["garage"]),
    ("rooms", "the fifth one", SINGLE, ["office"]),
    ("rooms", "number six", SINGLE, ["garage"]),
    ("rooms", "six", SINGLE, ["garage"]),
    ("rooms", "the last one", SINGLE, ["garage"]),
    ("rooms", "the kitchen one", SINGLE, ["kitchen"]),
    ("rooms", "cancel", SINGLE, NOPICK),
    ("rooms", "never mind", SINGLE, NOPICK),

    # ---- people ("Who are you?")
    ("people", "ada", SINGLE, ["ada"]),
    ("people", "it's ada", SINGLE, ["ada"]),
    ("people", "i'm ada", SINGLE, ["ada"]),
    ("people", "this is emeka", SINGLE, ["emeka"]),
    ("people", "emeka", SINGLE, ["emeka"]),
    ("people", "adaeze", SINGLE, ["adaeze"]),
    ("people", "ann", SINGLE, ["ann"]),
    ("people", "anna", SINGLE, ["anna"]),
    ("people", "an", SINGLE, NOPICK),
    ("people", "chinenye", SINGLE, ["chinenye"]),
    ("people", "chi nen ye", SINGLE, ["chinenye"]),
    ("people", "me", SINGLE, NOPICK),
    ("people", "none of those", SINGLE, NOPICK),
    ("people", "somebody else", SINGLE, NOPICK),

    # ---- automations ("Update which automation?")
    ("autos", "porch light at sunset", SINGLE, ["a1"]),
    ("autos", "the sunset one", SINGLE, ["a1"]),
    ("autos", "the porch one", SINGLE, ["a1"]),
    ("autos", "morning coffee", SINGLE, ["a3"]),
    ("autos", "coffee", SINGLE, ["a3"]),
    ("autos", "the motion one", SINGLE, ["a2"]),
    ("autos", "motion light", SINGLE, ["a2"]),
    ("autos", "good night", SINGLE, ["a4"]),
    ("autos", "night", SINGLE, NOPICK),
    ("autos", "the night light", SINGLE, ["a2"]),
    ("autos", "never mind", SINGLE, NOPICK),
    ("autos", "forget it", SINGLE, NOPICK),
    ("autos", "none", SINGLE, NOPICK),
    ("autos", "neither", SINGLE, NOPICK),
    ("autos", "stop", SINGLE, NOPICK),

    # ---- yes / no confirms
    ("confirm", "yes", SINGLE, ["yes"]),
    ("confirm", "yes please", SINGLE, ["yes"]),
    ("confirm", "yeah", SINGLE, ["yes"]),
    ("confirm", "yep", SINGLE, ["yes"]),
    ("confirm", "sure", SINGLE, ["yes"]),
    ("confirm", "yes remove it", SINGLE, ["yes"]),
    ("confirm", "remove it", SINGLE, ["yes"]),
    ("confirm", "no", SINGLE, ["no"]),
    ("confirm", "nope", SINGLE, ["no"]),
    ("confirm", "no thanks", SINGLE, ["no"]),
    ("confirm", "nah", SINGLE, ["no"]),
    ("confirm", "leave it", SINGLE, ["no"]),
    ("confirm", "cancel", SINGLE, NOPICK),
    ("confirm", "yes no", SINGLE, NOPICK),
    ("confirm", "not yet", SINGLE, NOPICK),
    ("confirm", "the first one", SINGLE, ["yes"]),
    ("trykey", "yes", SINGLE, ["yes"]),
    ("trykey", "yeah try it", SINGLE, ["yes"]),
    ("trykey", "no", SINGLE, ["no"]),
    ("trykey", "leave it", SINGLE, ["no"]),
    ("approve", "yes", SINGLE, ["yes"]),
    ("approve", "approve", SINGLE, ["yes"]),
    ("approve", "no", SINGLE, ["no"]),
    ("approve", "decline", SINGLE, ["no"]),
    ("approve", "don't approve", SINGLE, NOPICK),
    ("wipe", "yes", SINGLE, NOPICK),
    ("wipe", "yes wipe it", SINGLE, ["wipe"]),
    ("wipe", "leave the chip alone", SINGLE, ["all"]),
    ("wipe", "all", SINGLE, NOPICK),
    ("more", "yes", SINGLE, ["yes"]),
    ("more", "add another", SINGLE, ["yes"]),
    ("more", "no", SINGLE, ["no"]),
    ("more", "that's everything", SINGLE, ["no"]),
    ("more", "that's it", SINGLE, NOPICK),
    ("retry", "try again", SINGLE, ["retry"]),
    ("retry", "not now", SINGLE, ["stop"]),
    ("retry", "again", SINGLE, ["retry"]),
    ("login", "log in", SINGLE, ["login"]),
    ("login", "login", SINGLE, ["login"]),
    ("login", "i'm new", SINGLE, ["signup"]),
    ("login", "new", SINGLE, ["signup"]),
    ("ok", "ok", SINGLE, ["ok"]),
    ("ok", "okay", SINGLE, ["ok"]),
    ("update", "a board", SINGLE, ["board"]),
    ("update", "board", SINGLE, ["board"]),
    ("update", "device", SINGLE, ["device"]),

    # ---- scenes and short names
    ("scenes", "movie time", SINGLE, ["movie time"]),
    ("scenes", "movie", SINGLE, ["movie time"]),
    ("scenes", "good night", SINGLE, ["good night"]),
    ("scenes", "goodnight", SINGLE, ["good night"]),
    ("scenes", "leaving", SINGLE, ["leaving home"]),
    ("short", "bob", SINGLE, ASK("bulb")),
    ("short", "bulb", SINGLE, ["bulb"]),
    ("short", "the fan", SINGLE, ["fan"]),
    ("short", "sock it", SINGLE, ["socket"]),
    ("short", "buzzer", SINGLE, ["buzzer"]),
    ("short", "the weather", SINGLE, NOPICK),
    ("short", "banana", SINGLE, NOPICK),
    ("short", "bulb and socket", SINGLE, NOPICK),
    ("short", "both", SINGLE, NOPICK),
    ("floors", "second floor light", SINGLE, ["f2"]),
    ("floors", "first floor", SINGLE, ["f1"]),
    ("floors", "the basement", SINGLE, ["bl"]),
    ("floors", "the third one", SINGLE, ["bl"]),

    # ---- multi-select
    ("lights", "all of them", ALL, ["kl", "hl", "pl", "bl", "fan"]),
    ("lights", "all", ALL, ["kl", "hl", "pl", "bl", "fan"]),
    ("lights", "everything", ALL, ["kl", "hl", "pl", "bl", "fan"]),
    ("lights", "select all", ALL, ["kl", "hl", "pl", "bl", "fan"]),
    ("lights", "all of them please", ALL, ["kl", "hl", "pl", "bl", "fan"]),
    ("lights", "all except the fan", ALL, ["kl", "hl", "pl", "bl"]),
    ("lights", "all but the fan", ALL, ["kl", "hl", "pl", "bl"]),
    ("lights", "everything except the porch light", ALL,
     ["kl", "hl", "bl", "fan"]),
    ("lights", "all of them except the kitchen and the hall", ALL,
     ["pl", "bl", "fan"]),
    ("lights", "all except the garage", ALL, NOPICK),
    ("lights", "all the lights", ALL, ["kl", "hl", "pl"]),
    ("lights", "all of them", MULTI, NOPICK),
    ("lights", "the kitchen and the hall", ALL, ["kl", "hl"]),
    ("lights", "kitchen light and hall light", ALL, ["kl", "hl"]),
    ("lights", "kitchen hall and porch", ALL, ["kl", "hl", "pl"]),
    ("lights", "the kitchen, the porch", ALL, ["kl", "pl"]),
    ("lights", "hall light", ALL, ["hl"]),
    ("lights", "the fan", ALL, ["fan"]),
    ("lights", "the first and the third", ALL, ["kl", "pl"]),
    ("lights", "one and two", ALL, ["kl", "hl"]),
    ("lights", "the bedroom lamp and the fan", ALL, ["bl", "fan"]),
    ("lights", "kitchen and banana", ALL, NOPICK),
    ("lights", "none", ALL, NOPICK),
    ("lights", "none of them", ALL, NOPICK),
    ("lights", "neither", ALL, NOPICK),
    ("lights", "cancel", ALL, NOPICK),
    ("lights", "never mind", ALL, NOPICK),
    ("lights", "hallway", ALL, ["hl"]),
    ("lights", "all the kitchen", ALL, ["kl"]),
    ("two", "both", MULTI, ["kl", "hl"]),
    ("two", "both of them", MULTI, ["kl", "hl"]),
    ("two", "both please", ALL, ["kl", "hl"]),
    ("two", "neither", MULTI, NOPICK),
    ("two", "the kitchen one", MULTI, ["kl"]),
    ("kitchen", "kitchen light", MULTI, ["kl"]),
    ("kitchen", "kitchen light and hall light", MULTI, ["kl", "hl"]),
    ("kitchen", "the kitchen", MULTI, NOPICK),

    # ---- a narrowed round ("I heard 'bulb'. Did you mean Bulb 2, Bulb 3?")
    ("narrow", "bulb two", CONF, ["b2"]),
    ("narrow", "two", CONF, NOPICK),
    ("narrow", "the second one", CONF, ["b3"]),
    ("narrow", "the first one", CONF, ["b2"]),
    ("narrow", "the last one", CONF, ["b3"]),
    ("narrow", "none of those", CONF, [" __none__"]),
    ("narrow", "none of those please", CONF, [" __none__"]),
    ("narrow", "bob three", CONF, ["b3"]),
    ("narrow", "bulb", CONF, NOPICK),
    ("narrow1", "bulb", CONF, ["bulb"]),
    ("narrow1", "bob", CONF, ["bulb"]),
    ("narrow1", "banana", CONF, NOPICK),
    ("narrow", "all of them", CONF_MULTI, ["b2", "b3"]),
    ("narrow", "both", CONF_MULTI, ["b2", "b3"]),
    ("narrow", "bulb", CONF_MULTI, ASK("b2", "b3")),
    ("narrow", "the third one", CONF, [" __none__"]),
    ("narrow", "none of the above", CONF, [" __none__"]),
    ("narrow", "cancel", CONF, NOPICK),
    ("narrow", "neither", CONF, [" __none__"]),
    ("narrow1", "yes", CONF, ["bulb"]),
    ("narrow1", "yes please", CONF, ["bulb"]),
    ("narrow1", "no thanks", CONF, [" __none__"]),

    # ---- more noise, found probing
    ("lights", "all of them but not the fan", ALL, ["kl", "hl", "pl", "bl"]),
    ("lights", "all lights except fan", ALL, ["kl", "hl", "pl", "bl"]),
    ("lights", "everything but the kitchen light and the fan", ALL,
     ["hl", "pl", "bl"]),
    ("lights", "not the fan", ALL, NOPICK),
    ("lights", "both", ALL, NOPICK),
    ("lights", "first second and third", ALL, ["kl", "hl", "pl"]),
    ("lights", "one two three", ALL, ["kl", "hl", "pl"]),
    ("lights", "kitchen or hall", ALL, ["kl", "hl"]),
    ("lights", "porch and bedroom", ALL, ["pl", "bl"]),
    ("lights", "the kitchen light, the hall light and the porch light", ALL,
     ["kl", "hl", "pl"]),
    ("lights", "fan and cancel", ALL, NOPICK),
    ("lights", "every one of them", ALL, ["kl", "hl", "pl", "bl", "fan"]),
    ("bulbs123", "two and three", MULTI, ["b2", "b3"]),
    ("bulbs123", "bulb 2 and 3", MULTI, ["b2", "b3"]),
    ("bulbs123", "bulb", SINGLE, ASK("b1", "b2", "b3")),
    ("bulbs123", "bulb to", SINGLE, ["b2"]),
    ("bulbs123", "bulb for", SINGLE, NOPICK),
    ("bulbs", "bulb 3 not bulb 2", SINGLE, NOPICK),
    ("bulbs", "desk lamp and bulb 2", SINGLE, NOPICK),
    ("bulbs", "bulb number two", SINGLE, ["light.bulb_2"]),
    ("people", "ann and anna", SINGLE, NOPICK),
    ("people", "my name is emeka", SINGLE, ["emeka"]),
    ("people", "i am chinenye", SINGLE, ["chinenye"]),
    ("rooms", "living room and kitchen", SINGLE, NOPICK),
    ("confirm", "yeah no", SINGLE, NOPICK),
    ("confirm", "don't remove it", SINGLE, NOPICK),
    ("confirm", "okay", SINGLE, ["yes"]),
    ("approve", "yes decline", SINGLE, NOPICK),
    ("approve", "no decline", SINGLE, ["no"]),
    ("wipe", "no", SINGLE, NOPICK),
    ("retry", "yes", SINGLE, NOPICK),
    ("ok", "yes", SINGLE, ["ok"]),
    ("ok", "cancel", SINGLE, NOPICK),
    ("two", "both", SINGLE, NOPICK),
    ("two", "all", MULTI, NOPICK),
    ("two", "kitchen and hall", SINGLE, NOPICK),
    ("scenes", "movie night", SINGLE, NOPICK),
    ("ports", "none of these", SINGLE, ["\x00none"]),
    ("ports", "none", SINGLE, ["\x00none"]),
    ("ports", "the jtag one", SINGLE, ["/dev/ttyACM0"]),
    ("ports", "the first one", SINGLE, ["/dev/ttyUSB0"]),
    ("ports", "cancel", SINGLE, NOPICK),
]


def check(expected, got):
    values, near = got
    if isinstance(expected, list):
        return sorted(values) == sorted(expected)
    if expected == NOPICK:
        return not values
    if isinstance(expected, _Ask):
        offered = {o.get("value") for o in near}
        return not values and all(v in offered for v in expected.values)
    raise ValueError(expected)


def run(fn=match):
    out = []
    for name, heard, kw, _exp in CORPUS:
        out.append(fn(heard, L[name], **kw))
    return out


# ── the match_request path: the same matcher behind ws_server ──────────


def run_match_request():
    """Drive ws_server's _dispatch_match_request with a fake connection and
    check it answers every corpus row exactly as the matcher does -- the
    frame path must add nothing and lose nothing (the labels, the flags)."""
    try:
        from app.ws_server import ConnectionHandler  # noqa: WPS433
    except Exception as exc:                          # noqa: BLE001
        return [f"ws_server import failed: {exc}"]
    sent = []

    class _Fake:
        MAX_MATCH_OPTIONS = ConnectionHandler.MAX_MATCH_OPTIONS
        MAX_MATCH_LABEL = ConnectionHandler.MAX_MATCH_LABEL
        MAX_MATCH_HEARD = ConnectionHandler.MAX_MATCH_HEARD

        async def _send_json(self, frame):
            sent.append(frame)

    fake = _Fake()
    problems = []
    for n, (name, heard, kw, _exp) in enumerate(CORPUS):
        sent.clear()
        msg = {"type": "match_request", "id": n, "heard": heard,
               "options": L[name], "multi": kw.get("multi", False),
               "allow_all": kw.get("allow_all", False),
               "confirming": kw.get("confirming", False)}
        asyncio.run(ConnectionHandler._dispatch_match_request(fake, msg))
        if len(sent) != 1 or sent[0].get("id") != n:
            problems.append(f"{heard!r}: bad reply {sent!r}")
            continue
        want = match(heard, L[name], **kw)
        got = (sent[0]["values"], sent[0]["near"])
        if (sorted(got[0]) != sorted(want[0])
                or [o["value"] for o in got[1]]
                != [o["value"] for o in want[1]]):
            problems.append(f"{name}: {heard!r} frame {got!r} != match {want!r}")
    # A malformed request is answered, empty, never raised.
    for bad in ({"id": "x"}, {"id": "y", "heard": "bulb", "options": "nope"},
                {"id": "z", "heard": "bulb", "options": [1, None, {"label": "x"}]}):
        sent.clear()
        asyncio.run(ConnectionHandler._dispatch_match_request(fake, bad))
        if (len(sent) != 1 or sent[0]["values"] or sent[0]["near"]
                or sent[0]["id"] != bad["id"]):
            problems.append(f"malformed {bad!r} -> {sent!r}")
    return problems


# ── the WebSocketIO.choose loop: narrowing rounds, yes/no, the none row ─


def run_choose_flows():
    """Script the phone's replies to WebSocketIO.choose and check what the
    flow finally gets. Each script is a list of reply frames; the fake
    socket records every question the hub asked."""
    from app.shell import WebSocketIO

    def flow(options, replies, **kw):
        asked = []
        queue = list(replies)

        async def send(frame):
            asked.append(frame)

        async def recv(patient=False):
            if not queue:
                return {"type": "choice_response", "cancelled": True}
            return queue.pop(0)

        io = WebSocketIO(None, send, recv)
        got = asyncio.run(io.choose("Which one?", options, **kw))
        return got, asked

    def heard(text, **extra):
        return {"type": "choice_response", "values": [], "via": "voice",
                "heard": text, **extra}

    def tap(*values):
        return {"type": "choice_response", "values": list(values),
                "via": "screen"}

    bulbs = L["bulbs"]
    problems = []

    def expect(label, got, want, asked=None, rounds=None):
        if sorted(got) != sorted(want):
            problems.append(f"{label}: got {got!r}, want {want!r}")
        if rounds is not None and asked is not None and len(asked) != rounds:
            problems.append(f"{label}: asked {len(asked)} time(s), "
                            f"want {rounds}")

    got, asked = flow(bulbs, [heard("bulb two")])
    expect("clear spoken pick", got, ["light.bulb_2"], asked, 1)

    got, asked = flow(bulbs, [heard("bulb"), heard("the second one")])
    expect("narrowed, then an ordinal of the NARROWED list", got,
           [asked[1]["options"][1]["value"]], asked, 2)
    if len(asked) > 1 and asked[1]["options"][-1]["value"] != " __none__":
        problems.append("narrowed round lacks the None-of-those row")

    got, asked = flow(L["short"], [heard("bob"), heard("yes please")])
    expect("one suggestion, answered 'yes please'", got, ["bulb"], asked, 2)

    got, asked = flow(L["short"], [heard("bob"), heard("yeah")])
    expect("one suggestion, answered 'yeah'", got, ["bulb"], asked, 2)

    got, asked = flow(L["short"], [heard("bob"), heard("no"), heard("fan")])
    expect("'no' reopens the whole list", got, ["fan"], asked, 3)
    if len(asked) > 2 and len(asked[2]["options"]) != len(L["short"]):
        problems.append("'no' did not reopen the WHOLE list")

    got, asked = flow(L["short"], [heard("bob"), heard("none of those please"),
                                   heard("buzzer")])
    expect("'none of those please' reopens the list", got, ["buzzer"],
           asked, 3)

    got, asked = flow(bulbs, [heard("bulb"), tap(" __none__"),
                              tap("switch.desk")])
    expect("the None-of-those row tapped", got, ["switch.desk"], asked, 3)

    got, asked = flow(bulbs, [heard("bulb"),
                              tap(" __none__", "light.bulb_2")],
                      multi=True)
    expect("none row beside a real pick is dropped", got, ["light.bulb_2"])

    got, asked = flow(bulbs, [heard("bulb"), {"type": "choice_response",
                                              "cancelled": True}])
    expect("cancel in a narrowed round", got, [])

    got, asked = flow(bulbs, [tap("light.bulb_2")])
    expect("a tap", got, ["light.bulb_2"], asked, 1)

    got, asked = flow(bulbs, [tap("light.gone"), tap("light.bulb_3")])
    expect("a stale tap from a replaced list is asked again", got,
           ["light.bulb_3"], asked, 2)

    got, asked = flow(bulbs, [tap("light.bulb_2", "light.bulb_3")])
    expect("single-select trims to one", got, ["light.bulb_2"])

    got, asked = flow(L["lights"], [heard("all except the fan")],
                      multi=True, allow_all=True)
    expect("all except, through choose", got, ["kl", "hl", "pl", "bl"])

    got, asked = flow(L["lights"],
                      [heard("and the porch", selected=["kl"])],
                      multi=True, allow_all=True)
    expect("spoken pick joins the taps already made", got, ["kl", "pl"])

    got, asked = flow(L["lights"],
                      [heard("and the porch", selected=["kl", "bogus"])],
                      multi=True, allow_all=True)
    expect("taps not on the list are dropped", got, ["kl", "pl"])

    got, asked = flow(bulbs, [heard("banana"), heard("banana"),
                              heard("banana")])
    expect("three misses give up", got, [], asked, 3)

    got, asked = flow(bulbs, [{"type": "choice_response", "value": "2"}])
    expect("typed position (older client)", got, ["light.bulb_2"])

    got, asked = flow(L["short"], [heard("bob"), heard("That's right.")])
    expect("'That's right.' to one suggestion", got, ["bulb"], asked, 2)

    got, asked = flow(L["short"], [heard("bob"), heard("um no thanks"),
                                   heard("socket")])
    expect("'um no thanks' reopens the list", got, ["socket"], asked, 3)

    got, asked = flow(bulbs, [heard("bulb"), tap(" __none__"), heard("bulb"),
                              tap(" __none__"), heard("bulb 2")])
    expect("'none of those' does not use up the rounds", got,
           ["light.bulb_2"], asked, 5)

    got, asked = flow(bulbs, [heard("cancel")])
    expect("'cancel' as words picks nothing", got, [])

    got, asked = flow(L["lights"], [heard("all of them")], multi=True)
    expect("'all' where all was not offered", got, [])

    # A YES/NO question about one thing ("Did you mean the lamp?").
    NONE = {"value": " __none__", "label": "No", "sub": ""}
    one = [L["short"][0], NONE]
    got, asked = flow(one, [heard("yes")], polar=True)
    expect("polar: yes", got, [L["short"][0]["value"]], asked, 1)
    got, asked = flow(one, [heard("no")], polar=True)
    expect("polar: no is a refusal, not 'I did not catch that'", got,
           [" __none__"], asked, 1)
    got, asked = flow(one, [heard("nope thanks")], polar=True)
    expect("polar: 'nope thanks'", got, [" __none__"], asked, 1)
    got, asked = flow(one, [tap(" __none__")], polar=True)
    expect("polar: the No row tapped", got, [" __none__"], asked, 1)
    got, asked = flow(one, [{"type": "choice_response", "cancelled": True}],
                      polar=True)
    expect("polar: cancel is not a no", got, [])

    # The walk through the resolver's ranked guesses.
    from app.shell import InteractiveShell

    def walk(replies, guesses):
        queue = list(replies)
        said = []

        async def send(frame):
            pass

        async def recv(patient=False):
            return (queue.pop(0) if queue else
                    {"type": "choice_response", "cancelled": True})

        sh = InteractiveShell.__new__(InteractiveShell)
        sh._io = WebSocketIO(None, send, recv)
        sh.dm = type("DM", (), {"devices": {
            g: {"name": g, "room": "kitchen"} for g in guesses}})()

        async def write(x, level=None):
            said.append(x)
        sh._io.write = write
        dec = {"kind": "pick", "reason": "semantic_confirm",
               "message": f"Did you mean the {guesses[0]} in the kitchen?",
               "candidates": [{"device_id": guesses[0], "room": "kitchen"}],
               "alternatives": list(guesses)}
        return asyncio.run(sh._confirm_guess(dec)), said

    got, said = walk([heard("yes")], ["lamp", "fan", "heater"])
    if got != "lamp":
        problems.append(f"guess: yes took {got!r}")
    got, said = walk([heard("no"), heard("yes")], ["lamp", "fan", "heater"])
    if got != "fan":
        problems.append(f"guess: no, yes took {got!r}, want the next-best 'fan'")
    got, said = walk([heard("no"), heard("no"), heard("yes")],
                     ["lamp", "fan", "heater"])
    if got != "heater":
        problems.append(f"guess: no, no, yes took {got!r}")
    got, said = walk([heard("no"), heard("no"), heard("no")],
                     ["lamp", "fan", "heater"])
    if got is not None or not any("couldn't find it" in x for x in said):
        problems.append(f"guess: three no's: {got!r}, said {said!r}")
    got, said = walk([heard("no")], ["lamp"])
    if got is not None or not any("Say the device's name" in x for x in said):
        problems.append(f"guess: one guess refused: {got!r}, said {said!r}")
    got, said = walk([{"type": "choice_response", "cancelled": True}],
                     ["lamp", "fan"])
    if got is not None or said:
        problems.append(f"guess: cancel says nothing and picks nothing: {got!r} {said!r}")

    return problems


def _fmt(expected):
    return expected if not isinstance(expected, list) else sorted(expected)


if __name__ == "__main__":
    results = run()
    wrong = [(row, got) for row, got in zip(CORPUS, results, strict=True)
             if not check(row[3], got)]
    for (name, heard, kw, exp), (values, near) in wrong:
        flags = ",".join(k for k, v in kw.items() if v) or "single"
        print(f"  [FAIL] {name} [{flags}]: {heard!r} -> values={values} "
              f"near={[o['value'] for o in near]}, expected {_fmt(exp)}")
    # The hub's event log is noise here; only failures are printed.
    with contextlib.redirect_stdout(io.StringIO()):
        frame_problems = run_match_request()
        flow_problems = run_choose_flows()
    for p in frame_problems + flow_problems:
        print(f"  [FAIL] {p}")
    checks = len(CORPUS) + 2
    failed = (len(wrong) + (1 if frame_problems else 0)
              + (1 if flow_problems else 0))
    print(f"\n  {checks - failed} passed, {failed} failed"
          f"  (corpus {len(CORPUS)}, match_request path, choose flows)")
    sys.exit(1 if failed else 0)

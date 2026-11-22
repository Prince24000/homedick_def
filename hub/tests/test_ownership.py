"""Who made what (2026-10-11): makers on scenes and automations, "just mine"
scenes, the editor's own PIN for somebody else's, a letter to the maker, and
removing a person.

Run: python3 tests/test_ownership.py   (no models, no network)
"""
import asyncio
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("HOMEVOICE_HOME", tempfile.mkdtemp())
os.environ.setdefault("HOMEVOICE_ENV_FILE", "")
# Never the real ~/.homevoice: phones, boards and recordings live in a temp
# folder (leaving and clearing delete them).
_HV = tempfile.mkdtemp()
os.environ.setdefault("HOMEVOICE_PHONES_FILE", os.path.join(_HV, "phones.json"))
os.environ.setdefault("HOMEVOICE_BOARDS_DIR", os.path.join(_HV, "boards"))
os.environ.setdefault("HOMEVOICE_RECORDINGS_DIR", os.path.join(_HV, "recordings"))
os.environ.setdefault("HOMEVOICE_RECORDING_SETTINGS",
                      os.path.join(_HV, "recording.json"))
os.environ.setdefault("HOMEVOICE_PHONE_VIDEOS", os.path.join(_HV, "videos.json"))

import numpy as np                                          # noqa: E402

from app.shell import InteractiveShell, CancelledByUser     # noqa: E402
from identity.people import PeopleJournal                   # noqa: E402
from nlu import automations as A                            # noqa: E402
from nlu.scene_manager import SceneManager                  # noqa: E402

PASSED = FAILED = 0


def check(name, ok, detail=""):
    global PASSED, FAILED
    PASSED, FAILED = PASSED + bool(ok), FAILED + (not ok)
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}   {'' if ok else detail}")


DIM = 8


def vec(text):
    """Words -> a fixed direction, so one sentence matches its own scene."""
    v = np.zeros(DIM, dtype=np.float32)
    for w in text.lower().split():
        v[sum(map(ord, w)) % DIM] += 1.0
    return v if v.any() else np.ones(DIM, dtype=np.float32)


def scenes():
    path = Path(tempfile.mkdtemp()) / "scenes.json"
    return SceneManager(embed_fn=vec, scenes_path=path, emb_dim=DIM)


def unit(text):
    v = vec(text)
    return v / np.linalg.norm(v)


A_LAMP = [{"device_id": "lamp", "state": {"on": True}}]


def store_checks():
    print("\nscenes remember who made them")
    sm = scenes()
    sm.register("night", "night", A_LAMP, by="augusta")
    sm.register("movie time", "movie time", A_LAMP, by="augusta", private=True)
    sm.register("party", "party", A_LAMP)
    again = SceneManager(embed_fn=vec, scenes_path=sm.scenes_path, emb_dim=DIM)
    night, movie = again.get("night"), again.get("movie time")
    check("maker and 'just mine' survive a reload",
          night.created_by == "augusta" and not night.private
          and movie.private and movie.created_by == "augusta")
    check("a scene from before has no maker", again.get("party").created_by == "")
    check("Chidi does not see Augusta's 'just mine' scene",
          [s.name for s in again.list_scenes(user="chidi")] == ["night", "party"]
          and again.get("movie time", user="chidi") is None)
    check("Augusta does", again.get("movie time", user="augusta") is not None)
    check("Chidi's voice never fires it",
          (again.resolve(unit("movie time"), speaker="chidi") or {})
          .get("scene", {}).get("name") != "movie time")
    check("Augusta's voice does",
          (again.resolve(unit("movie time"), speaker="augusta") or {})
          .get("scene", {}).get("name") == "movie time")
    try:
        again.register("movie time", "movie time", A_LAMP, by="chidi")
        said = ""
    except ValueError as e:
        said = str(e)
    check("the name is hers, and said to be", "augusta already has" in said, said)

    print("\nyour own scene wins a near tie with a shared one")
    sm = scenes()
    sm.register("relax", "relax", A_LAMP, by="chidi")
    sm.register("relax mine", "relax", A_LAMP, by="augusta", private=True)
    got = sm.resolve(unit("relax"), speaker="augusta") or {}
    check("Augusta's 'relax' fires hers", got.get("status") == "fired"
          and got["scene"]["name"] == "relax mine", got.get("status"))
    got = sm.resolve(unit("relax"), speaker="chidi") or {}
    check("Chidi's fires the shared one", got.get("status") == "fired"
          and got["scene"]["name"] == "relax", got.get("status"))

    sm.update("relax", by="augusta", actions=A_LAMP)
    r = sm.get("relax")
    check("an edit records who changed it, and keeps the maker",
          r.created_by == "chidi" and r.changed_by == "augusta" and r.changed_at)
    check("a rename follows the person",
          sm.rename_owner("chidi", "chidi o") == 1
          and sm.get("relax").created_by == "chidi o")
    check("handed over: it is the new owner's, and shared",
          sm.hand_over("relax", "augusta") and sm.get("relax").created_by == "augusta")

    print("\nautomations remember who made them")
    dm = type("DM", (), {"devices": {"lamp": {"name": "lamp", "state": {}}},
                         "mqtt_driver": None})()
    path = os.path.join(tempfile.mkdtemp(), "a.json")
    eng = A.AutomationEngine(dm, None, None, path=path)
    ok, rid = eng.add({"name": "porch", "created_by": "chidi",
                       "trigger": {"kind": "time", "at": "19:00"},
                       "action": {"kind": "device", "device": "lamp",
                                  "set": {"on": True}}})
    check("added with its maker", ok and eng.get(rid)["created_by"] == "chidi", rid)
    ok, _ = eng.update(rid, enabled=False, changed_by="augusta", changed_at=1.0)
    check("a pause with who did it is still only a pause",
          ok and eng.get(rid)["enabled"] is False
          and eng.get(rid)["changed_by"] == "augusta")
    check("owned_by", [r["id"] for r in eng.owned_by("chidi")] == [rid])
    check("rename and hand over",
          eng.rename_owner("chidi", "chidi o") == 1
          and eng.get(rid)["created_by"] == "chidi o"
          and eng.hand_over(rid, "augusta") and eng.get(rid)["created_by"] == "augusta")

    print("\nletters, and a person removed")
    j = PeopleJournal(os.path.join(tempfile.mkdtemp(), "people.json"))
    j.record("augusta")
    j.record("chidi", "augusta")
    j.send_letter("augusta", "scene:night", "Chidi changed your scene 'night'", "x")
    j.send_letter("augusta", "scene:night", "Chidi removed your scene 'night'", "y")
    box = j.letters_for("augusta")
    check("one thing, one letter: the latest", len(box) == 1
          and "removed" in box[0]["title"], box)
    check("read, and gone", j.drop_letter("augusta", "scene:night")
          and j.letters_for("augusta") == [])
    j.send_letter("chidi", "x", "t", "b")
    j.mark_removed("chidi", "augusta")
    e = j.get("chidi")
    check("removed: kept as history, letters gone",
          e.get("removed_by") == "augusta" and e.get("added_by") == "augusta"
          and j.letters_for("chidi") == [])
    check("someone let in again under that name is a new arrival",
          j.record("chidi", "augusta") and not j.get("chidi").get("removed_at"))


# ── the flows, on a shell with no models ─────────────────────────────

class IO:
    renders_data = True
    trusted_local = False

    def __init__(self, answers):
        self.answers = list(answers)
        self.asked, self.said, self.data = [], [], []

    async def write(self, text, level=None):
        self.said.append(text)

    async def send_data(self, kind, payload):
        self.data.append((kind, payload))

    def _next(self, q):
        self.asked.append(q)
        if not self.answers:
            raise CancelledByUser()
        return self.answers.pop(0)

    async def ainput(self, prompt, **kw):
        return self._next(prompt)

    async def get_input(self, prompt, **kw):
        return self._next(prompt)

    async def choose(self, prompt, options, **kw):
        got = self._next(prompt)
        return got if isinstance(got, list) else [got]

    def raise_if_cancelled(self):
        pass


class _Pins:
    def __init__(self):
        self.pin_db = {"augusta": b"x", "chidi": b"y"}
        self.deleted = []

    def delete_pin(self, user):
        self.deleted.append(user)
        self.pin_db.pop(user, None)

    def get_lockout_remaining(self, user):
        return 0


class _Fallback:
    PINS = {"augusta": "1111", "chidi": "2222"}

    def __init__(self):
        self.pin = _Pins()

    def authenticate(self, user, pin):
        ok = self.PINS.get(user) == pin
        return ok, {"status": "success" if ok else "wrong_pin"}


class _Bio:
    def __init__(self):
        self.users = ["augusta", "chidi"]

    def list_users(self):
        return list(self.users)

    def delete_user(self, user):
        self.users.remove(user)
        return True


class _Cards:
    def __init__(self):
        self.gone = []

    def remove_all(self, user):
        self.gone.append(user)
        return 1


def shell(me, answers):
    sh = InteractiveShell.__new__(InteractiveShell)
    sh.sm = scenes()
    dm = type("DM", (), {"devices": {"lamp": {"name": "lamp", "state": {}}},
                         "mqtt_driver": None, "rooms": set(),
                         "clear_context": lambda self, who: None})()
    sh.dm = dm
    sh.automations = A.AutomationEngine(
        dm, None, None, path=os.path.join(tempfile.mkdtemp(), "a.json"))
    sh.people = PeopleJournal(os.path.join(tempfile.mkdtemp(), "people.json"))
    sh.fallback = _Fallback()
    sh.biometric_store = _Bio()
    sh.cards = _Cards()
    sh.sv = type("SV", (), {"remove_user": lambda self, u: True})()
    sh._gate_grace = {}
    sh.current_speaker_id = me
    sh.current_phone_id = "phone-1"
    sh._io = IO(answers)
    sh.failure_note = None
    sh.gate_calls = []

    async def gate(label, speaker):
        sh.gate_calls.append(label)
        return True
    sh._gate_for = gate
    return sh


def run(coro):
    try:
        return asyncio.run(coro)
    except CancelledByUser:
        return "cancelled"


def flow_checks():
    print("\nchanging somebody else's needs THEIR PIN, and they are told")
    sh = shell("chidi", ["1111"])
    sh.sm.register("night", "night", A_LAMP, by="augusta")
    ok = run(sh.run_menu_action("dropscene:night", "chidi"))
    check("Chidi removes Augusta's scene with HER PIN",
          ok is True and sh.sm.get("night") is None, sh._io.said)
    check("...asked for augusta's PIN, not passed by the usual check",
          any(q.startswith("augusta's PIN") for q in sh._io.asked)
          and sh.gate_calls == [],
          (sh._io.asked, sh.gate_calls))
    box = sh.people.letters_for("augusta")
    check("Augusta has a letter saying so",
          len(box) == 1 and box[0]["title"] == "chidi removed your scene 'night'",
          box)

    sh = shell("chidi", ["2222", "2222", "2222"])
    sh.sm.register("night", "night", A_LAMP, by="augusta")
    ok = run(sh.run_menu_action("dropscene:night", "chidi"))
    check("HIS OWN PIN does not open her scene: nothing changes, nobody "
          "is written to",
          ok is False and sh.sm.get("night") is not None
          and sh.people.letters_for("augusta") == [] and sh.failure_note,
          (ok, sh.failure_note))

    sh = shell("augusta", [])
    sh.sm.register("night", "night", A_LAMP, by="augusta")
    ok = run(sh.run_menu_action("dropscene:night", "augusta"))
    check("your own: the usual check, no PIN, no letter",
          ok is True and sh.gate_calls and not sh._io.asked
          and sh.people.letters_for("augusta") == [], (sh.gate_calls, sh._io.asked))

    sh = shell("chidi", [])
    sh.sm.register("party", "party", A_LAMP)
    ok = run(sh.run_menu_action("dropscene:party", "chidi"))
    check("the house's (no maker): anyone's own usual check changes it",
          ok is True and sh.gate_calls and not sh._io.asked
          and sh.sm.get("party") is None, (sh.gate_calls, sh._io.asked))

    sh = shell("chidi", ["1111"])
    ok_add, rid = sh.automations.add({
        "name": "porch", "created_by": "augusta",
        "trigger": {"kind": "time", "at": "19:00"},
        "action": {"kind": "device", "device": "lamp", "set": {"on": True}}})
    ok = run(sh.run_menu_action(f"pause:{rid}", "chidi"))
    rule = sh.automations.get(rid)
    check("even pausing hers takes her PIN; stamped, and she is told",
          ok is True and rule["enabled"] is False and rule["changed_by"] == "chidi"
          and sh.people.letters_for("augusta")[0]["title"]
          == "chidi paused your automation 'porch'", (ok, rule))

    print("\nthe letter is a notice until it is read")
    sh.current_speaker_id = "augusta"
    notes = sh._letter_notices()
    check("Augusta's check finds it, with a Got it",
          len(notes) == 1 and notes[0]["action_value"] == "seen:automation:porch"
          and notes[0]["action_label"] == "Got it", notes)
    run(sh.run_menu_action("seen:automation:porch", "augusta"))
    check("Got it: read, and gone from her list",
          sh._letter_notices() == []
          and ("notice_gone", {"id": "letter:automation:porch"}) in sh._io.data)

    print("\nnew things carry their maker; 'just me' is asked once two live here")
    sh = shell("augusta", ["me"])
    private = run(sh._ask_for_whom())
    check("'Just me'", private is True)
    sh.biometric_store.users = ["augusta"]
    sh.fallback.pin.pin_db = {"augusta": b"x"}
    sh._io = IO([])
    check("alone, nobody is asked", run(sh._ask_for_whom()) is False
          and sh._io.asked == [])
    check("alone, lists name no maker", sh._maker_shown("augusta") == ""
          and sh._maker_chip("augusta") == "" and sh._maker_chip("") == "")
    sh.biometric_store.users = ["augusta", "chidi"]
    sh.fallback.pin.pin_db = {"augusta": b"x", "chidi": b"y"}
    check("two here: the card tags",
          [sh._maker_chip(o, p) for o, p in (("augusta", True), ("augusta", False),
                                            ("chidi", False), ("", False))]
          == ["Just mine", "by you", "by chidi", "Everyone"])
    check("the details name you as 'you'",
          sh._maker_shown("augusta") == "you" and sh._maker_shown("chidi") == "chidi")


def removal_checks():
    print("\nremoving a person")
    sh = shell("augusta", ["yes", "1111", "pick", ["scene:night"]])
    sm = sh.sm
    sm.register("night", "night", A_LAMP, by="chidi")
    sm.register("morning", "morning", A_LAMP, by="chidi")
    sm.register("gym", "gym", A_LAMP, by="chidi", private=True)
    sm.register("mine", "mine", A_LAMP, by="augusta")
    ok, rid = sh.automations.add({
        "name": "porch", "created_by": "chidi",
        "trigger": {"kind": "time", "at": "19:00"},
        "action": {"kind": "device", "device": "lamp", "set": {"on": True}}})
    sh.people.send_letter("chidi", "x", "t", "b")
    sh._gate_grace[("phone-2", "chidi")] = 1e12
    _reg_after = _phones(**{"phone-2": "chidi"})
    done = run(sh.run_menu_action("removeperson:chidi", "augusta"))
    _reg_after._reload_if_changed()
    check("removed", done is True, (done, sh.failure_note, sh._io.said))
    check("asked: sure, PIN, then what of his shared things",
          sh._io.asked[0] == "Remove chidi from this house?"
          and "Your PIN" in sh._io.asked[1]
          and "made 2 scenes, 1 automation that everyone uses" in sh._io.asked[2],
          sh._io.asked)
    check("his 'just mine' scene is gone", sm.get("gym") is None)
    check("the one ticked is deleted; the rest are now Augusta's",
          sm.get("night") is None and sm.get("morning").created_by == "augusta"
          and sh.automations.get(rid)["created_by"] == "augusta")
    check("her own things are untouched", sm.get("mine").created_by == "augusta")
    check("his phone is unpaired, not just unbound (else \"I'm new\" lets "
          "him back in)", _reg_after.get("phone-2") is None)
    check("voice, PIN and cards gone; his check window closed",
          "chidi" not in sh.biometric_store.users
          and "chidi" in sh.fallback.pin.deleted and sh.cards.gone == ["chidi"]
          and ("phone-2", "chidi") not in sh._gate_grace)
    check("the journal keeps that he was here, and who removed him",
          sh.people.get("chidi").get("removed_by") == "augusta"
          and sh.people.letters_for("chidi") == [])

    sh = shell("augusta", ["yes", "1111", "keep"])
    sh.sm.register("night", "night", A_LAMP, by="chidi")
    run(sh.run_menu_action("removeperson:chidi", "augusta"))
    check("Keep them all: everything becomes hers",
          sh.sm.get("night").created_by == "augusta")

    sh = shell("augusta", ["yes", "0000", "0000", "0000"])
    sh.sm.register("night", "night", A_LAMP, by="chidi")
    done = run(sh.run_menu_action("removeperson:chidi", "augusta"))
    check("a wrong PIN removes nobody and touches nothing",
          done is False and "chidi" in sh.biometric_store.users
          and sh.sm.get("night").created_by == "chidi")

    sh = shell("augusta", ["no"])
    done = run(sh.run_menu_action("removeperson:chidi", "augusta"))
    check("'No, keep chidi' is a cancel, before any PIN",
          done == "cancelled" and len(sh._io.asked) == 1
          and "chidi" in sh.biometric_store.users)

    sh = shell("augusta", [])
    done = run(sh.run_menu_action("removeperson:augusta", "augusta"))
    check("nobody removes themselves", done is False
          and "yourself" in (sh.failure_note or ""), sh.failure_note)


def _phones(**owners):
    """A phone register: phone id -> its person."""
    import json
    from core import config
    with open(config.PHONES_FILE, "w", encoding="utf-8") as f:
        json.dump({pid: {"id": pid, "name": pid, "user_id": who,
                         "public_key": "", "paired_at": 1, "last_seen": 0}
                   for pid, who in owners.items()}, f)
    from identity import phones as P
    return P.PhoneRegistry()


def leave_checks():
    print("\nleaving the house yourself")
    sh = shell("chidi", ["2222"])
    sm = sh.sm
    sm.register("night", "night", A_LAMP, by="chidi")
    sm.register("gym", "gym", A_LAMP, by="chidi", private=True)
    sm.register("mine", "mine", A_LAMP, by="augusta")
    ok, rid = sh.automations.add({
        "name": "porch", "created_by": "chidi",
        "trigger": {"kind": "time", "at": "19:00"},
        "action": {"kind": "device", "device": "lamp", "set": {"on": True}}})
    reg = _phones(**{"phone-c": "chidi", "phone-a": "augusta"})
    done = run(sh.run_menu_action("leavehouse:keep", "chidi"))
    check("Chidi leaves with his OWN PIN", done is True
          and sh._io.asked == ["Your PIN, to leave: "], (done, sh._io.asked,
                                                         sh.failure_note))
    check("his 'just mine' scene is gone; what he made for everyone stays, "
          "for everyone", sm.get("gym") is None
          and sm.get("night").created_by == ""
          and sh.automations.get(rid)["created_by"] == "")
    check("her things untouched", sm.get("mine").created_by == "augusta")
    check("voice, PIN and cards gone; the journal says he left",
          "chidi" not in sh.biometric_store.users
          and "chidi" in sh.fallback.pin.deleted and sh.cards.gone == ["chidi"]
          and sh.people.get("chidi").get("removed_by") == "chidi")
    reg._reload_if_changed()
    check("his phone is signed out, hers is not",
          reg.get("phone-c") is None and reg.get("phone-a") is not None)
    check("the app is told to forget the hub",
          ("left_house", {"cleared": False}) in sh._io.data, sh._io.data)
    check("others still here: pairing rules unchanged",
          not os.path.exists(reg._button_flag()))

    sh = shell("chidi", ["2222"])
    done = run(sh.run_menu_action("leavehouse:clear", "chidi"))
    check("the hub is not cleared while others live here, and no PIN is asked",
          done is False and not sh._io.asked
          and "Other people" in (sh.failure_note or ""), sh.failure_note)

    sh = shell("chidi", ["1111", "1111", "1111"])
    done = run(sh.run_menu_action("leavehouse:keep", "chidi"))
    check("a wrong PIN: nothing changes", done is False
          and "chidi" in sh.biometric_store.users)

    sh = shell("augusta", ["1111"])
    sh.biometric_store.users = ["augusta"]
    sh.fallback.pin.pin_db.pop("chidi")
    reg = _phones(**{"phone-a": "augusta", "phone-old": ""})
    done = run(sh.run_menu_action("leavehouse:keep", "augusta"))
    reg._reload_if_changed()
    check("the last person leaves; the house stays; EVERY phone is unpaired "
          "(an unbound one would set the hub up as its own)", done is True
          and sh.biometric_store.users == [] and reg.count() == 0)
    check("...and pairing needs the hub's button (no phones is no longer "
          "'nothing to protect')", not reg.may_enroll())
    os.remove(reg._button_flag())
    check("(without that mark, an empty register is open, as before)",
          reg.may_enroll())

    print("\nclearing the whole hub")
    from core import config

    class _Esp:
        def __init__(self):
            self.wiped, self.revoked = [], []

        def list_nodes(self):
            return ["kitchen", "gate"]

        async def decommission(self, slug):
            self.wiped.append(slug)
            return True

        async def revoke_access(self, slug):
            self.revoked.append(slug)
            return True

    sh = shell("augusta", ["1111"])
    sh.biometric_store.users = ["augusta"]
    sh.fallback.pin.pin_db.pop("chidi")
    sh.esp = _Esp()
    done = run(sh.run_menu_action("leavehouse:clear", "augusta"))
    marker = os.path.join(os.path.dirname(config.PHONES_FILE), "clear-hub")
    check("the last person clears it with her PIN", done is True
          and sh._io.asked == ["Your PIN, to clear the whole hub: "],
          (done, sh._io.asked, sh.failure_note))
    check("every board told to wipe itself, its login revoked",
          sh.esp.wiped == ["kitchen", "gate"]
          and sh.esp.revoked == ["kitchen", "gate"])
    check("the app is told; the files wait for the restart",
          ("left_house", {"cleared": True}) in sh._io.data
          and os.path.exists(marker))

    data = str(config.DATA_DIR)
    os.makedirs(data, exist_ok=True)
    os.makedirs(os.path.join(config.BOARDS_DIR), exist_ok=True)
    for name in ("devices.json", "voiceprints.json", "pin_db.json",
                 "challenge_words.txt", "nav_vectors.npz"):
        open(os.path.join(data, name), "w").close()
    open(os.path.join(config.BOARDS_DIR, "kitchen.json"), "w").close()
    _phones(**{"phone-a": "augusta"})
    from app import ws_server
    ws_server._clear_hub_if_asked()
    left = sorted(os.listdir(data))
    check("at the start: the house's data gone, the wordlist and caches kept",
          left == ["challenge_words.txt", "nav_vectors.npz"], left)
    check("boards, phones and the note gone",
          not os.path.exists(config.BOARDS_DIR)
          and not os.path.exists(config.PHONES_FILE)
          and not os.path.exists(marker))
    from identity import phones as P
    check("...and pairing needs the hub's button (the hub code is the same)",
          not P.PhoneRegistry().may_enroll())
    os.remove(P.PhoneRegistry()._button_flag())
    ws_server._clear_hub_if_asked()
    check("no note, nothing done", sorted(os.listdir(data)) == left)


store_checks()
flow_checks()
removal_checks()
leave_checks()
print(f"\n  {PASSED} passed, {FAILED} failed")
sys.exit(1 if FAILED else 0)

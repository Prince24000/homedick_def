"""Saved layouts: a wiring plan kept under a name for one chip family, owned
and deleted only by the person. Covers the store, the carry-over of old
per-board drafts, a failed push saving a layout, and "use on a board".

Run: python3 tests/test_layouts.py   (no board, no esptool)
"""
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
os.environ.setdefault("HOMEVOICE_HOME", tempfile.mkdtemp())
os.environ["HOMEVOICE_BOARDS_DIR"] = tempfile.mkdtemp()

from esp_firmware import chips                               # noqa: E402
from app.shell import InteractiveShell                       # noqa: E402
import test_boards as tb                                     # noqa: E402

PASSED = FAILED = 0
CLASSIC, S3 = chips.DEFAULT, chips.ESP32_S3
relay, run = tb.relay, tb.run


def check(name, ok, detail=""):
    global PASSED, FAILED
    if ok:
        PASSED += 1
        print(f"  [PASS] {name}")
    else:
        FAILED += 1
        print(f"  [FAIL] {name}   {detail}")


def fresh(io=None):
    tb.fresh_dir()
    return tb.builder(io or tb.FakeIO())


def plans(b):
    """The layouts that are on no board -- plans, drafts."""
    return [r for r in b.list_layouts() if not r.get("on_board")]


def of_board(b, slug):
    return [r for r in b.list_layouts() if r.get("on_board") == slug]


def store():
    print("\nthe store")
    b = fresh()
    lay = b.save_layout("Relay board", CLASSIC, [relay("lamp", 26)],
                        source="designed by you")
    check("a layout is saved with an id, a chip family and its devices",
          lay["id"].startswith("lay-") and lay["family"] == "esp32"
          and lay["items"] == [relay("lamp", 26)], lay)
    check("it is listed", [r["id"] for r in b.list_layouts()] == [lay["id"]])
    check("layouts never show up as boards", b.list_nodes() == [])
    check("a second layout of the same name gets (2)",
          b.save_layout("Relay board", CLASSIC, [relay("a", 4)])["name"]
          == "Relay board (2)")
    s3 = b.save_layout("S3 set", S3, [relay("fan", 4)])
    check("listing by chip family only returns that family",
          [r["id"] for r in b.list_layouts("esp32s3")] == [s3["id"]])
    again = b.save_layout("Relay board", CLASSIC,
                          [relay("lamp", 26), relay("fan", 27)],
                          layout_id=lay["id"])
    check("saving over an id replaces its devices and keeps its name",
          again["id"] == lay["id"] and again["name"] == "Relay board"
          and len(again["items"]) == 2 and again["created"] == lay["created"])
    check("junk items are dropped, not stored",
          b.save_layout("Junk", CLASSIC,
                        [{"nope": 1}, "x", relay("ok", 4)])["items"]
          == [relay("ok", 4)])
    for bad in ("../wifi", "lay-zzzzzzzz", "", None, "lay-1234567"):
        check(f"an id like {bad!r} is refused", b._read_layout(bad) is None
              and b.delete_layout(bad) is False)
    check("delete removes it, and only it",
          b.delete_layout(lay["id"])
          and b._read_layout(lay["id"]) is None
          and b._read_layout(s3["id"]) is not None)


def migrate():
    print("\nold per-board drafts are carried over")
    b = fresh()
    b._write_board(tb.record("bench", items=[relay("lamp", 26)], flashed="x",
                             items_pending=[relay("lamp", 26),
                                            relay("fan", 27)],
                             items_pending_at="2026-09-09 10:00"))
    b._write_board(tb.record("clean", items=[relay("a", 4)]))
    got = b.list_layouts()
    got = plans(b)
    check("the draft became a layout", len(got) == 1
          and got[0]["name"] == "Bench (not sent)"
          and got[0]["state"] == "not_sent"
          and len(got[0]["items"]) == 2, got)
    check("it keeps when it was drawn", got[0]["created"] == "2026-09-09 10:00")
    check("and each board has its own layout too",
          len(of_board(b, "bench")) == 1 and len(of_board(b, "clean")) == 1)
    rec = b._read_board("bench")
    check("the board no longer carries a draft and keeps its own wiring",
          "items_pending" not in rec and "items_pending_at" not in rec
          and rec["items"] == [relay("lamp", 26)], rec)
    n = len(b.list_layouts())
    check("running it again moves nothing", b.migrate_drafts() == 0
          and len(b.list_layouts()) == n)


def edit_and_failed_push():
    print("\nEdit: use a layout; a failed push saves one")
    layout = [relay("lamp", 26), relay("fan", 27)]
    io = tb.FakeIO(pin_map={"cancelled": True})
    b = fresh(io)
    b._write_board(tb.record("bench", items=[relay("lamp", 26)]))
    run(b.edit_board("bench", start_items=layout))
    check("the pin screen opens on the layout, not the board's wiring",
          io.frames[-1]["items"] == layout, io.frames[-1]["items"])
    check("backing out changes nothing and saves no layout",
          b._read_board("bench")["items"] == [relay("lamp", 26)]
          and plans(b) == [])

    io = tb.FakeIO(pin_map={"items": layout})
    b = fresh(io)
    b._write_board(tb.record("bench", items=[relay("lamp", 26)]))
    res = run(b.edit_board("bench", start_items=layout))
    check("saving puts the layout on the board and reports the change",
          b._read_board("bench")["items"] == layout
          and res["added"] == ["fan"] and res["gone"] == [], res)
    check("applying a layout this way saves no extra plan",
          plans(b) == [])
    check("the board's own layout now shows the new wiring",
          of_board(b, "bench")[0]["items"] == layout)

    # a push that cannot reach the board
    io = tb.FakeIO(answers=["later"], pin_map={"items": layout})
    b = fresh(io)
    b._write_board(tb.record("bench", items=[relay("lamp", 26)],
                             flashed="2026-09-01 10:00"))

    async def no_facts(_r):
        return None

    async def unreachable(_r, _p):
        return False
    b._learn_board_facts = no_facts
    b._send_plan = unreachable
    res = run(b.edit_board("bench"))
    saved = plans(b)
    check("a push that cannot reach the board returns None", res is None)
    check("the board keeps what the chip holds",
          b._read_board("bench")["items"] == [relay("lamp", 26)])
    check("the layout was SAVED in the library, for the same chip family",
          len(saved) == 1 and saved[0]["family"] == "esp32"
          and len(saved[0]["items"]) == 2
          and saved[0]["state"] == "not_sent", saved)
    check("the board's own layout still shows what the chip holds",
          of_board(b, "bench")[0]["items"] == [relay("lamp", 26)])
    check("and the person is told where it went (Notifications)",
          "Notifications" in io.text(), io.text())
    check("no per-board draft is left behind",
          "items_pending" not in b._read_board("bench"))


class _Stub:
    """Just enough of the shell for the layout actions."""
    def __init__(self, esp):
        self.esp = esp
        self._io = tb.FakeIO()
        self.failure_note = ""
        self.edited = None
        self.installed = []

    _board_parts = staticmethod(InteractiveShell._board_parts)
    _layout_records = InteractiveShell._layout_records
    menu_use_layout = InteractiveShell.menu_use_layout
    menu_drop_layout = InteractiveShell.menu_drop_layout

    async def _choose_or_cancel(self, title, options):
        self.asked = (title, options)
        return getattr(self, "pick", None) or options[0]["value"]

    async def menu_edit_board(self, slug=None, start_items=None):
        self.edited = (slug, start_items)
        return True

    async def _flash_and_register(self, slug):
        self.installed.append(slug)
        return True


def shell_side():
    print("\nthe shell's side")
    b = fresh()
    b._write_board(tb.record("classic1", items=[relay("a", 4)],
                             flashed="2026-10-01 10:00"))
    b._write_board(tb.record("s3one", S3, [relay("b", 4)]))
    b._write_board(tb.record("waiting1", items=[relay("w", 4)]))
    lay = b.save_layout("Pair", CLASSIC, [relay("x", 4), relay("y", 5)])
    stub = _Stub(b)

    recs = {r["id"]: r for r in stub._layout_records()}
    mine = recs[lay["id"]]
    check("the app's records name the chip, count and parts",
          mine["chip"] == CLASSIC.family_label
          and mine["count"] == 2 and len(mine["parts"]) == 2, mine)
    check("a plan says it is on no board",
          mine["status"] == "saved" and mine["on_board"] == ""
          and mine["board_name"] == "", mine)
    on = [r for r in recs.values() if r["on_board"] == "classic1"][0]
    check("a board's layout carries its status and the board's name",
          on["status"] == "flashed" and on["board_name"] == "Classic1"
          and on["name"] == "Classic1", on)
    check("a board not installed yet is not in Layouts (it waits in "
          "Notifications)", not any(r["on_board"] in ("waiting1", "s3one")
                                    for r in recs.values()), list(recs.values()))

    stub.pick = "classic1"
    ok = run(stub.menu_use_layout(lay["id"]))
    slugs = [o["value"] for o in stub.asked[1]]
    check("'use on a board' offers a new board, then only boards of the "
          "SAME chip family", slugs == ["\x00new", "classic1", "waiting1"],
          slugs)
    check("and opens Edit on that board with the layout's devices",
          ok and stub.edited == ("classic1", lay["items"]), stub.edited)

    stub = _Stub(b)
    check("deleting removes the layout",
          run(stub.menu_drop_layout(lay["id"])) is True
          and b._read_layout(lay["id"]) is None)
    check("deleting twice reports it is already gone",
          run(stub.menu_drop_layout(lay["id"])) is False
          and "already gone" in stub.failure_note)

    only_s3 = fresh()
    only_s3._write_board(tb.record("s3one", S3, [relay("b", 4)]))
    lay2 = only_s3.save_layout("Classic only", CLASSIC, [relay("x", 4)])
    stub = _Stub(only_s3)
    started = []

    async def add_node(layout_id=None):
        started.append(layout_id)
        only_s3._last_node = "newone"
        return True
    only_s3.add_node = add_node
    ok = run(stub.menu_use_layout(lay2["id"]))
    check("with no board of that chip: a new board from the layout, "
          "installed at once, nothing asked",
          ok and started == [lay2["id"]] and stub.installed == ["newone"]
          and stub.edited is None and not hasattr(stub, "asked"),
          (started, stub.installed))


def layout_then_flash():
    print("\na layout put on a board never installed: installed at once")

    class Esp:
        def __init__(self, flashed, ok=True):
            self.rec = {"name": "Bedroom", "flashed": flashed}
            self.ok, self.flashed_now = ok, []

        def _read_board(self, slug):
            return self.rec

        async def edit_board(self, slug, start_items=None):
            return {"name": "Bedroom", "gone": [], "added": ["lamp"],
                    "changed": []}

        async def flash(self, slug):
            self.flashed_now.append(slug)
            return self.ok

    class Reg:
        def __init__(self):
            self.registered, self.settled = [], []

        async def register_board(self, slug):
            self.registered.append(slug)

        async def settle_after_edit(self, slug, result):
            self.settled.append(slug)
            return True

    class Shell:
        menu_edit_board = InteractiveShell.menu_edit_board

        def __init__(self, esp):
            self.esp, self.boards_reg, self._io = esp, Reg(), tb.FakeIO()
            self.checkpoint_note = self.failure_note = ""

        def _scenes_using(self, did):
            return []

        async def _which_board(self, slug):
            return slug, self.esp.rec

    sh = Shell(Esp(flashed=""))
    ok = run(sh.menu_edit_board("bedroom", start_items=[relay("lamp", 26)]))
    check("layout saved -> flashed straight away -> its devices registered",
          ok and sh.esp.flashed_now == ["bedroom"]
          and sh.boards_reg.registered == ["bedroom"], (sh.esp.flashed_now, sh._io.said))
    sh = Shell(Esp(flashed="", ok=False))
    ok = run(sh.menu_edit_board("bedroom", start_items=[relay("lamp", 26)]))
    check("no board on the cable: the design is kept, parked in Notifications",
          ok and sh.boards_reg.registered == [] and "Notifications" in sh.checkpoint_note,
          sh.checkpoint_note)
    sh = Shell(Esp(flashed="2026-10-01"))
    run(sh.menu_edit_board("bedroom", start_items=[relay("lamp", 26)]))
    check("a board already installed: its wiring goes over the air, no reflash",
          sh.esp.flashed_now == [] and sh.boards_reg.settled == ["bedroom"])


def new_board_from_a_layout():
    print("\nAdding a board: a saved layout, and drafts kept")
    items = [relay("lamp", 26), relay("fan", 27)]

    def make(io, cable=None):
        b = fresh(io)

        async def wifi(**_k):
            return {"ssid": "x", "password": "y"}

        async def grant(_r):
            return True

        async def peek():
            return cable
        b._ensure_wifi = wifi
        b.grant_access = grant
        b._peek_cable_family = peek
        return b

    # starting from a layout: no "which kind of board" question, frame prefilled
    io = tb.FakeIO(pin_map={"items": items})
    io.typed = ["Porch"]
    b = make(io)
    lay = b.save_layout("Pair", CLASSIC, items)
    ok = run(b.menu_add_esp_node(lay["id"]))
    check("a board starts from the layout without asking the chip",
          ok and not any("kind of board" in q for q, _ in io.asked), io.asked)
    check("the pin screen opens on the layout's devices",
          io.frames[-1]["items"] == items
          and io.frames[-1]["catalogue"]["board"] == CLASSIC.id)
    check("the board is saved with that chip and those devices",
          b._read_board("porch")["items"] == items
          and b._read_board("porch")["board_family"] == "esp32")
    check("an unchanged design makes no copy of the plan",
          [r["name"] for r in plans(b)] == ["Pair"])
    check("the new board has its own layout, waiting for install",
          [r["name"] for r in of_board(b, "porch")] == ["Porch"])

    # a new design finished: kept as a layout too, once
    io = tb.FakeIO(answers=["esp32"], pin_map={"items": items})
    io.typed = ["Kitchen"]
    b = make(io)
    run(b.menu_add_esp_node())
    got = b.list_layouts()
    check("a finished new design is a layout at once, once",
          len(got) == 1 and got[0]["name"] == "Kitchen"
          and got[0]["on_board"] == "kitchen"
          and got[0]["items"] == items, got)

    # abandoned halfway: whatever was placed is kept
    io = tb.FakeIO(answers=["esp32"],
                   pin_map={"cancelled": True, "draft": items})
    io.typed = ["Garage"]
    b = make(io)
    ok = run(b.menu_add_esp_node())
    got = plans(b)
    check("cancelling saves no board", ok is False
          and b._read_board("garage") is None)
    check("but what was placed is kept as an unfinished layout",
          len(got) == 1 and got[0]["name"] == "Garage (unfinished)"
          and got[0]["state"] == "unfinished"
          and got[0]["items"] == items, got)
    check("and the person is told", "Notifications" in io.text(), io.text())

    io = tb.FakeIO(answers=["esp32"], pin_map={"cancelled": True})
    io.typed = ["Shed"]
    b = make(io)
    run(b.menu_add_esp_node())
    check("cancelling with nothing placed keeps nothing",
          b.list_layouts() == [])

    # abandoned edit
    io = tb.FakeIO(pin_map={"cancelled": True,
                            "draft": items + [relay("pump", 12)]})
    b = make(io)
    b._write_board(tb.record("bench", items=items))
    run(b.edit_board("bench"))
    got = plans(b)
    check("an edit abandoned with changes is kept as an unfinished layout",
          len(got) == 1 and got[0]["name"] == "Bench (unfinished edit)"
          and len(got[0]["items"]) == 3, got)
    io = tb.FakeIO(pin_map={"cancelled": True, "draft": items})
    b = make(io)
    b._write_board(tb.record("bench", items=items))
    run(b.edit_board("bench"))
    check("an abandoned edit with no changes keeps nothing",
          plans(b) == [])


def picking_a_layout_knows_the_chip():
    print("\nPicking a layout when the chip is known")

    def make(io, cable):
        b = fresh(io)

        async def peek():
            return cable
        b._peek_cable_family = peek
        return b

    io = tb.FakeIO(answers=[lambda rows: rows[0]["value"]])
    b = make(io, S3)
    b.save_layout("Classic one", CLASSIC, [relay("a", 4)])
    s3 = b.save_layout("S3 one", S3, [relay("b", 4)])
    got = run(b.pick_layout())
    rows = io.asked[-1][1]
    check("layouts for the chip on the cable lead, and say so",
          rows[0]["label"] == "S3 one" and "fits the board" in rows[0]["sub"]
          and "different chip" in rows[1]["sub"], rows)
    check("picking the matching one returns it", got == s3["id"], got)

    io = tb.FakeIO(answers=[lambda rows: rows[1]["value"]])
    b = make(io, S3)
    b.save_layout("Classic one", CLASSIC, [relay("a", 4)])
    b.save_layout("S3 one", S3, [relay("b", 4)])
    check("picking one for another chip is refused, with the reason",
          run(b.pick_layout()) is None and "different" not in "x"
          and "on the cable" in io.text(), io.text())

    io = tb.FakeIO(answers=[lambda rows: rows[0]["value"]])
    b = make(io, None)
    one = b.save_layout("Any", CLASSIC, [relay("a", 4)])
    check("with no cable every layout is open to choose",
          run(b.pick_layout()) == one["id"])

    b = make(tb.FakeIO(), None)
    check("with no layouts it says so and returns nothing",
          run(b.pick_layout()) is None)



def flash_never_touches_the_library():
    print("\nnothing but its owner deletes a layout")
    b = fresh()
    b._write_board(tb.record("bench", items=[relay("lamp", 26)]))
    lay = b.save_layout("Keep me", CLASSIC, [relay("lamp", 26)])
    b._write_board(dict(b._read_board("bench"), flashed="2026-09-01"))
    b.list_layouts()
    b.migrate_drafts()
    check("migrations and lists leave a layout alone",
          b._read_layout(lay["id"]) is not None)


def a_boards_wiring_is_its_layout():
    print("\nOne point of truth: every board's wiring is a layout")
    b = fresh()
    items = [relay("lamp", 26)]
    b._write_board(tb.record("kitchen", items=items))
    one = of_board(b, "kitchen")
    check("a board with wiring has its layout the moment it is written",
          len(one) == 1 and one[0]["items"] == items, one)
    check("it is waiting (not installed)",
          b.layout_view(one[0])[0] == "waiting")
    b._write_board(dict(b._read_board("kitchen"), flashed="2026-10-01 10:00"))
    check("flashing makes it 'flashed', and names the board",
          b.layout_view(of_board(b, "kitchen")[0])[0] == "flashed")
    new = [relay("lamp", 26), relay("fan", 27)]
    b._write_board(dict(b._read_board("kitchen"), items=new))
    got = of_board(b, "kitchen")
    check("an edit updates the same layout, no copies",
          len(got) == 1 and got[0]["items"] == new
          and got[0]["id"] == one[0]["id"], got)
    b._write_board(dict(b._read_board("kitchen"), name="Galley"))
    check("renaming the board renames its layout",
          of_board(b, "kitchen")[0]["name"] == "Galley")
    b._write_board(dict(b._read_board("kitchen"), board=S3.id,
                        board_family=S3.family))
    check("a change of board type moves the layout's chip",
          of_board(b, "kitchen")[0]["family"] == "esp32s3")
    b.delete_node("kitchen")
    gone = [r for r in b.list_layouts() if r["items"] == new]
    check("retiring the board keeps its wiring as a plain plan",
          len(gone) == 1 and gone[0]["on_board"] is None
          and "was on Galley" in gone[0]["source"]
          and b.layout_view(gone[0]) == ("saved", ""), gone)
    b._write_board(tb.record("empty", items=[]))
    check("a board with nothing wired has no layout",
          of_board(b, "empty") == [])
    n = len(b.list_layouts())
    b.list_layouts()
    b.migrate_drafts()
    check("lists and migrations make no copies", len(b.list_layouts()) == n)

    # the shell: a board's layout edits the board, and cannot be deleted
    b = fresh()
    b._write_board(tb.record("kitchen", items=items))
    lid = of_board(b, "kitchen")[0]["id"]
    stub = _Stub(b)
    stub.menu_edit_layout = InteractiveShell.menu_edit_layout.__get__(stub)
    run(stub.menu_edit_layout(lid))
    check("Edit on a board's layout edits the BOARD",
          stub.edited == ("kitchen", None), stub.edited)
    check("a board's layout cannot be deleted while the board is there",
          run(stub.menu_drop_layout(lid)) is False
          and b._read_layout(lid) is not None
          and "wiring of a board" in stub.failure_note, stub.failure_note)
    io = tb.FakeIO()
    b = fresh(io)
    b._write_board(tb.record("kitchen", items=items))
    lid = of_board(b, "kitchen")[0]["id"]
    check("and it cannot be renamed apart from the board",
          run(b.rename_layout(lid)) is None and "Rename the board" in io.text())


def main():
    store()
    migrate()
    edit_and_failed_push()
    shell_side()
    layout_then_flash()
    new_board_from_a_layout()
    picking_a_layout_knows_the_chip()
    flash_never_touches_the_library()
    a_boards_wiring_is_its_layout()
    print(f"\n{PASSED} passed, {FAILED} failed")
    sys.exit(1 if FAILED else 0)


if __name__ == "__main__":
    main()

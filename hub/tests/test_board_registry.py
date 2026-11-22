"""ESP boards and Home Assistant are kept apart: registering a board's
devices, and settling the registry after an Edit, never touch Home
Assistant, never wait for it, and never ask a question.

Run: python3 tests/test_board_registry.py   (no board, no esptool, no HA)
"""
import os
import re
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
os.environ.setdefault("HOMEVOICE_HOME", tempfile.mkdtemp())
os.environ["HOMEVOICE_BOARDS_DIR"] = tempfile.mkdtemp()

from esp_firmware.board_registry import BoardRegistrar       # noqa: E402
from app.shell import InteractiveShell                       # noqa: E402
from nlu.device_manager import DeviceManager                 # noqa: E402
import test_boards as tb                                     # noqa: E402
import test_nlu_spec as ns                                   # noqa: E402

PASSED = FAILED = 0
relay, run = tb.relay, tb.run


def check(name, ok, detail=""):
    global PASSED, FAILED
    if ok:
        PASSED += 1
        print(f"  [PASS] {name}")
    else:
        FAILED += 1
        print(f"  [FAIL] {name}   {detail}")


class _NoHA:
    """A shell whose Home Assistant client is a tripwire: any use fails."""

    def __getattr__(self, name):
        # The form helpers read the real shell's class constants (_KINDS,
        # _KIND_WORDS ...) and small pure methods: borrowed, not copied.
        if name.startswith("_") and hasattr(InteractiveShell, name):
            import inspect
            raw = inspect.getattr_static(InteractiveShell, name)
            attr = getattr(InteractiveShell, name)
            if isinstance(raw, (staticmethod, classmethod)) or not callable(attr):
                return attr
            return attr.__get__(self)
        raise AttributeError(name)
    def __init__(self, io=None, pin_map=None):
        self._io = io or tb.FakeIO(pin_map=pin_map)
        self.esp = tb.builder(self._io)
        self.dm = DeviceManager(path=tempfile.mktemp(suffix=".json"),
                                embed_fn=ns.fake_embed)
        self.automations = None
        self.boards_reg = BoardRegistrar(self)
        self.asked = 0
        self.forms = []          # every registration form that was shown
        self.back_out = False    # True: the person backs out of the form

    async def _registration_loop(self, spec):
        """The registration form, answered: saved as it was offered, with
        the person's own words for what the device does."""
        from core.hv_common import CancelledByUser
        self.forms.append(spec)
        if self.back_out:
            raise CancelledByUser()
        return {**spec["values"], "does": "keeps the room warm"}

    _record_fields_from_values = InteractiveShell._record_fields_from_values
    _validate_form_values = InteractiveShell._validate_form_values

    @property
    def ha_client(self):
        raise AssertionError("an ESP board path reached Home Assistant")

    def _scenes_using(self, did):
        return []

    async def _which_board(self, slug):
        return slug, self.esp._read_board(slug)

    menu_edit_board = InteractiveShell.menu_edit_board


def setup(items_before, items_after, flashed=True):
    shell = _NoHA(pin_map={"items": items_after})
    tb.fresh_dir()
    shell.esp = tb.builder(shell._io)
    rec = tb.record("bench", items=items_before,
                    flashed="2026-09-01" if flashed else "")
    shell.esp._write_board(rec)

    async def no_facts(_r):
        return None

    async def sent(_r, _p):
        return True
    shell.esp._learn_board_facts = no_facts
    shell.esp._send_plan = sent
    if items_before and flashed:
        matches = shell.dm.match_board(items_before, "bench", "Bench", [],
                                       local_first=True, has_ha=False)
        shell.dm.register_board(matches, "bench", "Bench")
    return shell


def source_is_clean():
    print("\nthe module itself")
    src = (ROOT / "esp_firmware" / "board_registry.py").read_text()
    code = "\n".join(l for l in src.splitlines()
                     if not l.lstrip().startswith("#"))
    check("board_registry.py never names the Home Assistant client",
          "ha_client" not in code and "get_states" not in code
          and "config_flow" not in code)
    check("the HA flow runner no longer carries any board registration",
          not re.search(r"def (_?register_board|_describe_board_devices|"
                        r"_summarise_board|_wait_for_board)",
                        (ROOT / "home_assistant" / "ha_flows.py").read_text()))


def settle():
    print("\nEdit settles the registry by itself")
    before = [relay("lamp", 26), relay("fan", 27)]
    after = [relay("lamp", 26), relay("fan", 14), relay("heater", 13)]
    shell = setup(before, after)
    shell.dm.update("bench.lamp", description="my own words about the lamp",
                    room="Lounge")
    ok = run(shell.menu_edit_board("bench"))
    devs = shell.dm.devices
    check("the edit finished, and no HA touched", ok is True)
    check("the NEW device went through the registration form -- only it",
          [f["values"]["name"] for f in shell.forms] == ["heater"], shell.forms)
    check("and is registered with what the form gave",
          "bench.heater" in devs
          and devs["bench.heater"].get("description") == "keeps the room warm",
          devs.get("bench.heater"))
    check("devices that were already there keep their names and rooms",
          devs["bench.lamp"].get("description") == "my own words about the lamp"
          and devs["bench.lamp"].get("room") == "lounge")
    check("a moved pin changes nothing about the device's record",
          "bench.fan" in devs)
    check("and the person is told what was added",
          "heater" in shell._io.text())

    form = shell.forms[0]
    check("the form marks what the part is meant to be, and locks nothing",
          not form.get("kind_locked")
          and [o["kind"] for o in form["kind_options"] if o["enabled"]][:1] == ["onoff"],
          form.get("kind_options"))

    shell = setup(before, after)

    async def as_level(spec):
        shell.forms.append(spec)
        return {**spec["values"], "does": "sets how warm it is", "kind": "level",
                "min": "0", "max": "100", "step": "10", "state": "0"}
    shell._registration_loop = as_level
    run(shell.menu_edit_board("bench"))
    heater = shell.dm.devices.get("bench.heater") or {}
    check("a kind other than the part's own is the one the record keeps",
          heater.get("state_key") == "level" and heater.get("range_max") == 100.0,
          heater)

    shell = setup(before, after)
    shell.back_out = True
    ok = run(shell.menu_edit_board("bench"))
    check("backed out of the form: the edit stands, the device is NOT registered",
          ok is True and "bench.heater" not in shell.dm.devices
          and "bench.lamp" in shell.dm.devices, list(shell.dm.devices))
    check("...and the person is told it is still to register",
          "Still to register" in shell._io.text(), shell._io.text()[-300:])

    shell = setup([relay("lamp", 26), relay("kettle", 25)],
                  [relay("lamp", 26)])
    run(shell.menu_edit_board("bench"))
    check("a device the layout dropped is unregistered",
          "bench.kettle" not in shell.dm.devices
          and "bench.lamp" in shell.dm.devices)

    shell = setup([relay("lamp", 26)], [relay("lamp", 26)], flashed=False)
    run(shell.menu_edit_board("bench"))
    check("a board never installed registers nothing yet",
          shell.dm.devices_on_board("bench") == [])


def unregistered_stays_unregistered():
    print("\nan edit does not undo a deliberate 'Remove one'")
    shell = setup([relay("lamp", 26), relay("fan", 27)],
                  [relay("lamp", 26), relay("fan", 27)])
    shell.dm.remove("bench.fan")          # the person removed it, on purpose
    run(shell.menu_edit_board("bench"))
    check("a wired-but-unregistered device is NOT registered by an edit",
          "bench.fan" not in shell.dm.devices)


def legacy_rewire_is_settled():
    print("\na board parked on the old rewire question")
    shell = setup([relay("lamp", 26)], [relay("lamp", 26)])
    rec = shell.esp._read_board("bench")
    rec["items"] = [relay("lamp", 26), relay("pump", 12)]
    rec["rewire_pending"] = {"before": [], "added": ["pump"], "changed": [],
                             "at": "x"}
    shell.esp._write_board(rec)
    shell.send = None

    async def send_data(kind, payload):
        shell.sent = (kind, payload)
    shell._io.send_data = send_data
    shell._settle_legacy_rewires = \
        InteractiveShell._settle_legacy_rewires.__get__(shell)
    n = run(shell._settle_legacy_rewires())
    check("it is settled quietly and the mark is cleared",
          n == 1 and "bench.pump" in shell.dm.devices
          and not shell.esp.rewire_pending("bench"))


def main():
    source_is_clean()
    settle()
    unregistered_stays_unregistered()
    legacy_rewire_is_settled()
    print(f"\n{PASSED} passed, {FAILED} failed")
    sys.exit(1 if FAILED else 0)


if __name__ == "__main__":
    main()

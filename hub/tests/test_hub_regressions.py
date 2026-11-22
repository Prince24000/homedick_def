"""Regression checks for hub behaviour fixed in review (no hardware needed).

Run: python3 tests/test_hub_regressions.py   (from anywhere)
"""
import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("HOMEVOICE_HOME", tempfile.mkdtemp())

from esp_firmware import esphome_catalog as cat          # noqa: E402
from esp_firmware.esp_builder import EspBuilder           # noqa: E402
from nlu import automations as A                          # noqa: E402

PASSED = FAILED = 0


def check(name, ok, detail=""):
    global PASSED, FAILED
    if ok:
        PASSED += 1
        print(f"  [PASS] {name}")
    else:
        FAILED += 1
        print(f"  [FAIL] {name}   {detail}")


class _Info:
    rc = 0


class _Client:
    def __init__(self, clock):
        self.clock, self.log = clock, []

    def publish(self, topic, payload, qos=0):
        self.log.append((self.clock[0], topic.split("/")[-1], payload))
        return _Info()


class _DM:
    devices = {}
    mqtt_driver = None

    def get_state(self, _did):
        return {}

    def set_state(self, *_a, **_k):
        return True


def drive_commands():
    print("\ndrive commands (one builder for both drivers)")
    b = EspBuilder.headless()
    targets = {"dim": ("Channel2", "pwm"), "rel": ("Power1", "relay"),
               "gate": ("Shutter1", "shutter")}
    b._drive_target = lambda plan, s: targets.get(s)
    b._colour_target = lambda plan, s: (["Channel1", "Channel2", "Channel3"]
                                        if s == "rgb" else None)
    f = lambda s, **k: b.drive_commands(None, s, **k)[0]     # noqa: E731
    check("off wins over a level on a dimmer", f("dim", on=False, level=50) == ["Channel2 0"])
    check("off wins over a level on a relay", f("rel", on=False, level=50) == ["Power1 OFF"])
    check("shutter level is a position", f("gate", level=40) == ["ShutterPosition1 40"])
    check("shutter off closes", f("gate", on=False) == ["ShutterClose1"])
    check("colour needs three values", f("rgb", rgb=(255, 0)) == [])
    check("off wins over a colour", f("rgb", on=False, rgb=(255, 0, 0))
          == ["Channel1 0", "Channel2 0", "Channel3 0"])


def display_model_ordering():
    print("\nDisplayModel restarts the board, so it goes last")
    lcd = cat.BY_ID["lcd2004"]
    plan = cat.BoardPlan("kit")
    plan.add("lcd2004", "Screen",
             {sl.key: pin for sl, pin in zip(lcd.slots, (21, 22), strict=False)}, anyway=True)
    relay = next(d for d in cat.CATALOGUE if len(d.slots) == 1
                 and d.slots[0].component.name == "Relay")
    plan.add(relay.id, "Lamp", {relay.slots[0].key: 26}, anyway=True)
    cmds = plan.configuration_commands()
    check("install backlog ends with DisplayModel", cmds[-1] == "DisplayModel 1", cmds)


def lcd_panel():
    print("\nLCD panel: restarted only when it lost its settings, never on the timer")
    clock = [time.mktime((2026, 9, 23, 12, 0, 0, 0, 0, -1))]
    b = EspBuilder.headless()
    b._boards = lambda: [("kit", {"topic": "kitchen",
                                  "items": [{"type_id": "lcd2004"}]})]
    b.list_nodes = lambda: ["kit"]
    b._read_board = lambda s: {"topic": "kitchen", "items": [{"type_id": "lcd2004"}]}
    panels = b.display_targets()
    check("a character LCD is a text panel", len(panels) == 1, panels)
    e = A.AutomationEngine(_DM(), None, None,
                           path=os.path.join(tempfile.mkdtemp(), "a.json"),
                           now=lambda: clock[0])
    c = _Client(clock)
    e._pub = c

    class _Esp:
        def display_targets(self):
            return [dict(p) for p in panels]
    e.esp = _Esp()
    e.refresh_lcd_targets()
    e._lcd_seen["kitchen"] = "online"

    def step(n=1):
        for _ in range(n):
            clock[0] += 5
            e._lcd_tick()
    restarts = lambda: [x for x in c.log if x[1:] == ("DisplayModel", "1")]  # noqa: E731
    asked = lambda: [x for x in c.log if x[1:] == ("DisplayModel", "")]  # noqa: E731
    step()
    check("the wake ASKS whether the board still has its display settings",
          asked() and not restarts(), c.log)
    e._note_presence("kitchen", "Online", retained=False)
    step(240)
    check("a board that still has them is never restarted (20 minutes of ticks)",
          not restarts(), len(restarts()))
    e.feed("tele/kitchen/INFO3", '{"Info3":{"RestartReason":"Software reset CPU","BootCount":9}}')
    check("a board back from a software restart: still no restart", not restarts())
    e.feed("tele/kitchen/INFO3", '{"Info3":{"RestartReason":"Vbat power on reset","BootCount":10}}')
    check("one just POWERED ON gets DisplayModel once, so its screen starts",
          len(restarts()) == 1, len(restarts()))
    e.feed("tele/kitchen/INFO3", '{"Info3":{"RestartReason":"Vbat power on reset","BootCount":11}}')
    check("...not again within a couple of minutes (a power fault is not looped)",
          len(restarts()) == 1, len(restarts()))
    clock[0] += 200
    e._note_presence("kitchen", "Online", retained=False)
    c.log.clear()
    e._note_display_model("kitchen", {"DisplayModel": 0})
    check("one that answers 0 (lost) gets its setup again: one restart",
          len(restarts()) == 1, len(restarts()))
    t0 = clock[0]
    step(3)
    check("the wake waits while that restart happens",
          not [x for x in c.log if x[0] > t0])
    said, real_event = [], A.event
    A.event = lambda tag, text: said.append(text)
    e._note_presence("kitchen", "Online", retained=False)
    clock[0] += 70
    e._note_presence("kitchen", "Offline", retained=False)
    check("an offline notice arriving a minute late is still the hub's own restart",
          said and "is restarting" in said[-1], said)
    clock[0] += e.LCD_RESTART_NOTICE_S
    e._note_presence("kitchen", "Online", retained=False)
    e._note_presence("kitchen", "Offline", retained=False)
    check("...but one long after is a real drop", "went offline" in said[-1], said)
    A.event = real_event
    rows = A.AutomationEngine._lcd_rows("Temp 21.5°C café [z]", panels[0], row=2)
    body = rows[0][len("[x0y1]"):]
    check("text is one ASCII cell per character", len(body) == 20 and body.isascii()
          and "[" not in body, repr(body))


def lcd_line_while_restarting():
    """2026-10-20: 'showed Good morning for 600s but the text isnt showing
    but its lighting up' -- a rule fired while the board was restarting
    from the wake's own DisplayModel."""
    print("\nLCD: a line said while the board restarts waits, then goes up")
    clock = [time.mktime((2026, 10, 20, 7, 0, 0, 0, 0, -1))]
    b = EspBuilder.headless()
    rec = {"topic": "kitchen", "items": [{"type_id": "lcd2004"}]}
    b._boards = lambda: [("kit", rec)]
    b.list_nodes = lambda: ["kit"]
    b._read_board = lambda s: rec
    panels = b.display_targets()
    e = A.AutomationEngine(_DM(), None, None,
                           path=os.path.join(tempfile.mkdtemp(), "a.json"),
                           now=lambda: clock[0])
    c = _Client(clock)
    e._pub = c

    class _Esp:
        def display_targets(self):
            return [dict(p) for p in panels]
    e.esp = _Esp()
    e.refresh_lcd_targets()
    e._lcd_seen["kitchen"] = "online"
    rule = {"id": "gm", "name": "Good morning", "enabled": True,
            "action": {"kind": "display", "device": "kit.screen",
                       "text": "Good morning", "for_s": 600}}
    e.rules.append(rule)

    def step(n=1):
        for _ in range(n):
            clock[0] += 5
            e._lcd_tick()
    texts = lambda: [x for x in c.log if x[1] == "DisplayText"]  # noqa: E731
    step()                                  # wake stage one
    # A board just powered on: the hub restarts it once so its screen starts.
    e.feed("tele/kitchen/INFO3",
           '{"Info3":{"RestartReason":"Vbat power on reset","BootCount":3}}')
    e._show(rule, rule["action"])
    check("said while restarting: nothing written, nothing claimed",
          not texts() and "waiting to show" in e.journal[0][2]
          and "showed" not in e.journal[0][2], e.journal[0])
    e._note_presence("kitchen", "Online", retained=False)
    step(12)
    check("...the wake finishes, and the line goes up on its own",
          any("Good morning" in x[2] for x in texts()), texts())
    e._offline.add("kitchen")
    n = len(texts())
    e._show(rule, rule["action"])
    check("board offline: not written, the journal says it is waiting",
          len(texts()) == n and "kitchen is offline" in e.journal[0][2],
          e.journal[0])


def meter_not_answering():
    print("\nA meter not answering: shown as the board sends it, ignored by rules")
    clock = [time.mktime((2026, 10, 27, 7, 0, 0, 0, 0, -1))]
    e = A.AutomationEngine(_DM(), None, None,
                           path=os.path.join(tempfile.mkdtemp(), "a.json"),
                           now=lambda: clock[0])
    e._index = {"kitchen": {("energy", 0): "kitchen.meter"}}
    fired, cond = [], []
    e._fire = lambda rule: fired.append(rule["id"]) or True
    e.rules = [{"id": "low", "enabled": True,
                "trigger": {"kind": "threshold", "device": "kitchen.meter",
                            "key": "power", "below": 10}},
               {"id": "moved", "enabled": True,
                "trigger": {"kind": "change", "device": "kitchen.meter",
                            "key": "voltage"}}]
    said, real_event = [], A.event
    A.event = lambda tag, text: said.append(text)

    def report(volts, watts, address):
        e.feed("tele/kitchen/SENSOR", '{"ENERGY":{"Voltage":%s,"Power":%s,'
               '"Current":0,"Address":%s}}' % (volts, watts, address))
    report(231, 120, 1)
    fired.clear()
    report(0, 0, 0)                       # the board just booted: no meter yet
    check("the zeros are kept, exactly as the board sent them",
          e.latest("kitchen.meter").get("power") == 0.0, e.latest("kitchen.meter"))
    check("...but no rule fires on them (no fake 'under 10 W')", not fired, fired)
    check("a device condition on it is not met: its reading is unknown",
          not e._condition_holds({"kind": "device", "device": "kitchen.meter",
                                  "key": "power", "equals": 0.0})
          and "not answering" in e._cond_why, e._cond_why)
    check("no warning on the first silent report (it may just have booted)",
          not any("NO METER" in t for t in said), said)
    report(231, 120, 1)
    check("it answers again within the minute: nothing fires, nothing said",
          not fired and not [t for t in said if "meter" in t.lower()], (fired, said))
    report(0, 0, 0)
    clock[0] += A.AutomationEngine.METER_SILENT_WARN_S + 1
    report(0, 0, 0)
    check("silent for minutes: warned once", sum("NO METER" in t for t in said) == 1,
          said)
    report(229, 115, 1)
    check("...and told when it answers again",
          any("answering again (229" in t for t in said), said)
    check("the first real reading is compared with the last REAL one, not a "
          "fake 0 (no 'voltage moved' from 0 to 229, no 'under 10 W')",
          "low" not in fired and fired.count("moved") <= 1, fired)
    A.event = real_event


def buttons():
    print("\nbuttons are events the hub hears")
    e = A.AutomationEngine(_DM(), None, None,
                           path=os.path.join(tempfile.mkdtemp(), "a.json"))
    e._index = {"b": {("button", 1): "b.bell"}}
    got = e.feed("stat/b/RESULT", '{"Button1":{"Action":"SINGLE"}}')
    check("a press arrives as a button event",
          ("b.bell", "button", "SINGLE") in got, got)
    setup = cat.BY_ID["doorbell"].setup
    check("buttons are detached from relay 1", "SetOption73 1" in setup, setup)


def warmer_cooler():
    print("\nwarmer/cooler on an on/off device depends on what it does")
    from nlu.executor import build_command
    from nlu.slot_extractor import SlotExtractor
    ex = SlotExtractor()

    def on_after(name, said, on):
        dev = {"name": name, "state_key": "on", "state": {"on": on}}
        return build_command(dev, ex.extract(said))
    check("cooler turns an AC on", on_after("Bedroom AC", "make it cooler", False)
          == {"action": "set", "key": "on", "value": True})
    check("cooler keeps a running AC on", on_after("Bedroom AC", "make it cooler", True)
          == {"action": "set", "key": "on", "value": True})
    check("cooler turns a heater off", on_after("Heater", "make it cooler", True)
          == {"action": "set", "key": "on", "value": False})
    check("warmer turns a heater on", on_after("Heater", "make it warmer", False)
          == {"action": "set", "key": "on", "value": True})
    check("unknown device still toggles", on_after("Plug", "make it warmer", False)
          == {"action": "flip"})
    check("up still means on", on_after("Fan", "turn the fan up", True)
          == {"action": "set", "key": "on", "value": True})


def override_window():
    print("\nyour command outranks automations for 60 s from the FIRST command")
    clock = [1000.0]
    e = A.AutomationEngine(_DM(), None, None,
                           path=os.path.join(tempfile.mkdtemp(), "a.json"),
                           now=lambda: clock[0])
    e.override_s = 60
    e.note_human("hall.light")
    clock[0] += 50
    e.note_human("hall.light")                  # repeat inside the window
    clock[0] += 5
    check("protected 55 s after the first command", e._overridden("hall.light"))
    clock[0] += 10
    check("a repeat command does not extend the window",
          not e._overridden("hall.light"))
    e.note_human("hall.light")                  # after it closed: new window
    check("a command after the window starts a new one", e._overridden("hall.light"))
    check("other devices are unaffected", not e._overridden("kitchen.light"))


def repeat_reports():
    print("\nrepeated sensor reports: ELSE once, a withheld rule still fires later")
    clock = [100000.0]
    e = A.AutomationEngine(_DM(), None, None,
                           path=os.path.join(tempfile.mkdtemp(), "a.json"),
                           now=lambda: clock[0])
    ran, cond = [], [False]
    e._run_actions = lambda rule, acts: ran.append(acts[0]["tag"])
    e._condition_holds = lambda c: cond[0]

    def rule(**kw):
        r = {"id": "r1", "enabled": True,
             "trigger": {"kind": "event", "device": "pir", "value": "on"},
             "action": {"kind": "x", "tag": "then"}, "condition": {"x": 1},
             "revert": {"after_s": 60, "actions": [{"kind": "x", "tag": "rev"}]}}
        r.update(kw)
        return r
    e.rules = [rule(else_actions=[{"kind": "x", "tag": "else"}])]
    for _ in range(4):
        e._observe("pir", "on", True)
        clock[0] += 40
    check("ELSE runs once while the door stays open", ran.count("else") == 1, ran)
    e = A.AutomationEngine(_DM(), None, None,
                           path=os.path.join(tempfile.mkdtemp(), "a.json"),
                           now=lambda: clock[0])
    ran.clear()
    e._run_actions = lambda rule, acts: ran.append(acts[0]["tag"])
    e._condition_holds = lambda c: cond[0]
    e.rules = [rule()]
    e._observe("pir", "on", True)
    clock[0] += 10
    cond[0] = True
    e._observe("pir", "on", True)
    check("a rule held by its condition fires on a later report", ran == ["then"], ran)


def hub_name_for_phones():
    print("\nthe name a phone lists this hub under")
    import os
    import socket
    from core import config
    saved = (os.environ.pop("HOMEVOICE_HUB_NAME", None), config.WS_HOSTNAME)
    try:
        os.environ["HOMEVOICE_HUB_NAME"] = "  Office  "
        check("a chosen name wins", config.hub_display_name() == "Office")
        del os.environ["HOMEVOICE_HUB_NAME"]
        config.WS_HOSTNAME = "pi-hub.tail0d1.ts.net"
        check("else the tailnet name's first label",
              config.hub_display_name() == "pi-hub")
        config.WS_HOSTNAME = ""
        check("else the machine's hostname",
              config.hub_display_name() == socket.gethostname().split(".")[0][:40])
    finally:
        if saved[0] is not None:
            os.environ["HOMEVOICE_HUB_NAME"] = saved[0]
        config.WS_HOSTNAME = saved[1]


def speaker_model_stays_warm():
    """After a command the speaker model is touched every few seconds for a
    while, by ONE loop however many commands arrive, and then left alone."""
    import asyncio
    from app.shell import InteractiveShell
    print("\n== the speaker model is kept warm after a command")
    s = InteractiveShell.__new__(InteractiveShell)
    calls = []

    class SV:
        def warm(self):
            calls.append(time.monotonic())
    s.sv = SV()
    s._SV_WARM_EVERY_S, s._SV_WARM_FOR_S = 0.05, 0.3

    async def go():
        s._keep_speaker_model_warm()
        first = s._sv_warm_task
        s._keep_speaker_model_warm()
        await asyncio.sleep(0.7)
        return first, s._sv_warm_task
    first, after = asyncio.run(go())
    check("touched more than once", len(calls) >= 3, len(calls))
    check("one loop, however many commands", first is not None)
    check("and it ends", after is None)
    n = len(calls)
    time.sleep(0.2)
    check("nothing runs once it has ended", len(calls) == n)

    # The sentence model too, at once, and never while a command is running.
    import nlu.model_module as mm
    sentences = []
    saved = mm._embed_fn
    mm._embed_fn = lambda text: sentences.append(text)
    try:
        async def once(busy):
            s.models_busy = lambda: busy
            s._keep_speaker_model_warm()
            await asyncio.sleep(0.02)          # less than one interval
            s._sv_warm_until = 0
            await asyncio.sleep(0.1)
        before = len(calls)
        asyncio.run(once(False))
        check("the first pass is at once, and the sentence model is touched too",
              len(sentences) >= 1 and len(calls) > before, (sentences, len(calls)))
        n = len(sentences)
        asyncio.run(once(True))
        check("no pass while a command is running", len(sentences) == n, len(sentences))
        check("a model in use is left alone, not queued for",
              mm._infer_lock.acquire() and mm.warm() == -1.0)
        mm._infer_lock.release()
        # A phone that is only connected: its ping counts for half an hour
        # after the last thing anybody did, and not past it.
        async def pinged(idle_for):
            s._sv_last_active = time.monotonic() - idle_for
            s._sv_warm_until = 0
            s._keep_speaker_model_warm(active=False)
            opened = s._sv_warm_until > time.monotonic()
            s._sv_warm_until = 0
            await asyncio.sleep(0.1)
            return opened
        s._SV_WARM_CAP_S = 1800.0
        check("a ping ten minutes after the last command keeps them warm",
              asyncio.run(pinged(600)))
        check("a ping forty minutes after it does not",
              not asyncio.run(pinged(2400)))
    finally:
        mm._embed_fn = saved
        s.models_busy = None


def pairing_hardened():
    """2026-10-15: no window at every start, pairing from away is a choice,
    and every other phone hears of a new one, with the road it came by."""
    import asyncio
    import base64
    from app.shell import InteractiveShell
    from core import config
    from core.hv_common import CancelledByUser
    from identity import phones
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from cryptography.hazmat.primitives import serialization
    print("\n== pairing, hardened")
    check("the window no longer opens at every start", config.PAIR_ON_BOOT is False)
    d = tempfile.mkdtemp()
    win = phones.PairingWindow(os.path.join(d, "w.json"))
    win.open(opened_by=phones.PairingWindow.OPENED_BY_HUB)
    check("a window is for a phone at home unless opened for one away",
          win.is_open() and not win.allows_away())
    win.open(opened_by=phones.PairingWindow.OPENED_BY_MENU, from_away=True)
    check("...Pair a new phone > Away from home lets that window take one",
          win.allows_away())
    win.close()
    check("...and a shut window takes nobody from away", not win.allows_away())
    with open(win.path, "w") as f:
        f.write("{broken")
    check("an unreadable window falls shut", not win.allows_away())
    check("no standing switch is left behind", not hasattr(phones, "PairingPolicy")
          and not hasattr(config, "PAIR_FROM_AWAY"))

    from app import ws_server
    check("a Tailscale address is 'away', a house one is not",
          ws_server.is_tailnet_ip("100.101.7.9") and not ws_server.is_tailnet_ip("192.168.1.20")
          and not ws_server.is_tailnet_ip("?"))

    def key():
        raw = Ed25519PrivateKey.generate().public_key().public_bytes(
            serialization.Encoding.Raw, serialization.PublicFormat.Raw)
        return base64.b64encode(raw).decode()
    reg = phones.PhoneRegistry(os.path.join(d, "phones.json"))
    reg.window.path = os.path.join(d, "pairing-open.json")
    mine = reg.add("Tecno", key(), device="TECNO KJ5", via="home")
    reg.window.open(opened_by=phones.PairingWindow.OPENED_BY_MENU)
    new = reg.add("Pixel", key(), device="Pixel 7", via="away")
    check("the record says which road a phone came by",
          new["paired_via"] == "away" and mine["paired_via"] == "home")

    real = phones.PhoneRegistry
    phones.PhoneRegistry = lambda path=None: reg
    try:
        s = InteractiveShell.__new__(InteractiveShell)
        s.current_phone_id, s.current_speaker_id, s.failure_note = mine["id"], "augusta", ""
        notes = s._new_phone_notices()
        check("every other phone is told of the new one, with the road",
              len(notes) == 1 and notes[0]["id"] == f"new_phone:{new['id']}"
              and "Tailscale" in notes[0]["body"] and notes[0]["action_value"] == f"newphone:{new['id']}",
              notes)
        s.current_phone_id = new["id"]
        check("...never the new phone about itself",
              all(n["id"] != f"new_phone:{new['id']}" for n in s._new_phone_notices()))
        s.current_phone_id = mine["id"]

        said, gated = [], []
        queue = []

        class IO:
            async def write(self, x, level=None):
                said.append(x)

            async def choose(self, prompt, options, **kw):
                said.append((prompt, [o["value"] for o in options]))
                return queue.pop(0) if queue else []

            async def send_data(self, kind, data):
                said.append((kind, data))
        s._io = IO()

        async def gate(label, who):
            gated.append(label)
            return True
        s._gate_for = gate
        queue[:] = [["keep"]]
        asyncio.run(s.menu_new_phone(new["id"]))
        check("'it is ours' asks the asker's own check, and the alert leaves",
              gated == ["Keep a new phone"] and s._new_phone_notices() == [], gated)

        reg.window.open(opened_by=phones.PairingWindow.OPENED_BY_MENU)
        third = reg.add("Stranger", key(), via="away")
        gated.clear()
        queue[:] = [["remove"], ["yes"]]
        ok = asyncio.run(s.menu_new_phone(third["id"]))
        check("'remove it' asks, checks the asker, and the phone is gone",
              ok is True and gated == ["Unpair a phone"] and reg.get(third["id"]) is None, gated)
        queue[:] = [["remove"], ["no"]]
        reg.window.open(opened_by=phones.PairingWindow.OPENED_BY_MENU)
        fourth = reg.add("Maybe", key(), via="home")
        try:
            asyncio.run(s.menu_new_phone(fourth["id"]))
            out = "done"
        except CancelledByUser:
            out = "cancelled"
        check("'no' removes nothing", out == "cancelled" and reg.get(fourth["id"]) is not None)
        s.current_phone_id = fourth["id"]
        queue[:] = [[]]
        try:
            asyncio.run(s.menu_new_phone(fourth["id"]))
        except CancelledByUser:
            pass
        offered = [x for x in said if isinstance(x, tuple) and isinstance(x[1], list)][-1][1]
        check("a new phone cannot vouch for itself", offered == ["remove"], offered)
    finally:
        phones.PhoneRegistry = real
    # Pair a new phone asks where the phone is; "away" opens THIS window
    # for a phone over Tailscale, "home" does not.
    for answer, expect in ((["away"], True), (["home"], False)):
        sent = []

        class PIO:
            async def write(self, x, level=None):
                pass

            async def choose(self, prompt, options, **kw):
                sent.append((prompt, [o["value"] for o in options]))
                return answer

            async def send_data(self, kind, data):
                sent.append((kind, data))
        ps = InteractiveShell.__new__(InteractiveShell)
        ps._io = PIO()
        phones.PhoneRegistry = lambda path=None: reg
        # On a network (hotspot on / Tailscale up): this is about WHERE the
        # phone is; waiting for an address is tests/test_pair_waits.py.
        from app import setup_manager as _sm
        real_road = _sm._reachable_road
        _sm._reachable_road = lambda from_away=False: ("ap", "192.168.4.1", "")
        try:
            asyncio.run(ps.menu_pair_phone())
        finally:
            phones.PhoneRegistry = real
            _sm._reachable_road = real_road
        check(f"Pair a new phone asks where it is; '{answer[0]}' "
              f"{'lets' if expect else 'does not let'} this window take one from away",
              sent[0] == ("Where is the new phone?", ["home", "away"])
              and reg.window.allows_away() is expect, sent)
        reg.window.close()
    old = dict(mine)
    old.pop("paired_via")
    check("phones paired before this change are not announced again",
          not old.get("paired_via"))


if __name__ == "__main__":
    for section in (drive_commands, display_model_ordering, lcd_panel, meter_not_answering,
                    lcd_line_while_restarting, buttons,
                    warmer_cooler, override_window, repeat_reports,
                    hub_name_for_phones, speaker_model_stays_warm, pairing_hardened):
        section()
    print(f"\n  {PASSED} passed, {FAILED} failed")
    sys.exit(1 if FAILED else 0)

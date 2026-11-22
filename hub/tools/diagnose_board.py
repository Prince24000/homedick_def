"""What the hub believes about one board, and whether that board is
actually saying anything. READ-ONLY.

    "the pzem reading is not showing on the app nothing is displaying on
     the lcd...see this cascade of problems / whats the issue"

Three parts on one board went quiet at once -- the meter, the panel and the
card reader. That is not three bugs. Every one of them needs the same two
things to be true: the board has to be TALKING, and the hub has to
RECOGNISE what it says. This prints both, so the next question is not a
guess.

It writes nothing, publishes nothing, and touches no board. The only
network it uses is a subscribe to that board's own topics.

    python3 tools/diagnose_board.py            # every board
    python3 tools/diagnose_board.py kitchen    # one
    python3 tools/diagnose_board.py kitchen 30 # listen longer than the default 15s

READ IT LIKE THIS:

  "the board said nothing"      -> the board is off, crashed, off the
                                   network, or in safeboot. Nothing the hub
                                   can do about the meter, the panel or the
                                   reader until it is back. Check power and
                                   its LED first, then Board report.

  "the board is talking" but a part is missing from the index -> the hub's
                                   record and the hub's plan disagree; the
                                   line above it says which part and why.

  both fine, still no readings  -> the part itself: its wiring, its power,
                                   or its own driver. The board is heard
                                   and the hub understands it.
"""
import re
import json
import os
import sys
import types

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)
os.environ.setdefault("HOMEVOICE_NO_AUTOSTACK", "1")

from core import config                                                   # noqa: E402
from esp_firmware.esp_builder import EspBuilder                 # noqa: E402
from nlu.device_manager import DeviceManager                    # noqa: E402
from tools.board_mqtt import converse                           # noqa: E402


def main() -> int:
    want = sys.argv[1] if len(sys.argv) > 1 else ""
    try:
        seconds = float(sys.argv[2]) if len(sys.argv) > 2 else 15.0
    except ValueError:
        print(f"seconds must be a number, not {sys.argv[2]!r}")
        return 2

    # No __init__: that would build an IO backend and a broker connection.
    # Every method used below reads files or settings only.
    esp = EspBuilder.headless()

    try:
        slugs = [s for s in esp.list_nodes() if not want or s == want]
    except Exception as exc:                       # noqa: BLE001
        print(f"could not read {config.BOARDS_DIR}: {exc}")
        return 2
    if not slugs:
        print(f"no board called {want!r} in {config.BOARDS_DIR}"
              if want else f"no boards in {config.BOARDS_DIR}")
        return 2

    for slug in slugs:
        record = esp._read_board(slug) or {}
        topic = record.get("topic", "")
        print(f"\n=== {slug}  (topic {topic!r}) ===")

        # ── 1. WHAT IS ON THE RECORD ──────────────────────────────────
        # WHAT THE BOARD SAID ABOUT ITSELF, and what the hub concluded.
        # GPIO16/17 are ordinary pins on a WROOM and the RAM chip's own
        # lines on a WROVER; driving them on a WROVER is a board that
        # crash-loops with the bad template saved in its settings. The
        # plan only allows them when the board ANSWERED that it has no
        # PSRAM -- silence stays None and is refused -- so a part sitting
        # on them is worth seeing next to the answer it rests on.
        # Which board it is decides WHICH pins the RAM may own: 16/17 on
        # the classic, 33-37 on an S3 with octal PSRAM, none elsewhere.
        board = esp._board_of(record)
        ram_pins = sorted(board.psram)
        psram = record.get("psram")
        print(f"  board {board.label} ({board.family_label}), "
              f"chip {str(record.get('chip') or '?')!r}, "
              f"PSRAM: {'yes' if psram else ('no' if psram is False else 'never answered')}")
        items = [i for i in (record.get("items") or []) if isinstance(i, dict)]
        on_ram = [str(i.get("name")) for i in items
                  if any(int(v) in ram_pins
                         for v in (i.get("pins") or {}).values()
                         if str(v).lstrip("-").isdigit())]
        if on_ram:
            note = ("-- and this board answered that it has none, so they "
                    "are ordinary pins here"
                    if psram is False else
                    "-- WHICH THIS MODULE KEEPS FOR ITS OWN RAM. A part "
                    "driven there crash-loops the board on every boot")
            print(f"  on GPIO{'/'.join(str(n) for n in ram_pins)}: "
                  f"{', '.join(on_ram)} {note}")
        print(f"  on the record ({len(items)}):")
        for i in items:
            print(f"    {str(i.get('name','?')):<20} {str(i.get('type_id','?')):<10} "
                  f"pins {i.get('pins')}")

        # ── 2. WHAT SURVIVES THE PLAN ─────────────────────────────────
        #
        # _plan_for replays every recorded item through BoardPlan.add, and
        # add REFUSES for a dozen good reasons. A refused part stays on the
        # record, vanishes from the plan, and is therefore in no index and
        # in no firmware template.
        plan = esp._plan_for(slug)
        placed = [i.name for i in plan.items] if plan is not None else []
        print(f"  survives the plan ({len(placed)}): {', '.join(placed) or '(none)'}")
        for note in esp._plan_notes:
            print(f"    DROPPED: {note}")
        missing = [str(i.get("name")) for i in items
                   if str(i.get("name")) not in placed]
        if missing:
            print(f"    ^ NOT on the plan: {', '.join(missing)} -- the hub "
                  f"cannot hear these and the chip was never told about them")

        # ── 3. WHAT THE HUB WOULD RECOGNISE ───────────────────────────
        #
        # The same table the automation engine builds, from the same
        # source, so this is what the hub can actually understand from a
        # message on that topic.
        # Reset per board: read again at the end, and a failed build below
        # must not leave the PREVIOUS board's table in place (or none at
        # all on the first board, which was a NameError).
        table = {}
        try:
            from nlu import automations as auto
            eng = auto.AutomationEngine(
                types.SimpleNamespace(devices={}), None,
                types.SimpleNamespace(), path=os.path.join(
                    str(config.DATA_DIR), "automations.json"))
            eng.dm = DeviceManager.__new__(DeviceManager)
            eng.dm.devices = {}
            eng.esp = esp
            eng.rebuild_index()
            table = eng._index.get(topic, {})
            print(f"  the hub would recognise ({len(table)}):")
            for (kind, key), did in sorted(table.items(), key=lambda x: str(x[0])):
                print(f"    {kind:<8} {str(key):<10} -> {did}")
            if not table:
                print("    (nothing -- no message from this board can mean "
                      "anything to the hub)")
            # A PANEL IS NOT AN INDEX ENTRY. Displays are written TO, never
            # heard from, so they live in their own list -- and a panel
            # missing from it is the reason a screen stays dark, which no
            # amount of staring at the index above would ever show.
            try:
                panels = [p for p in (esp.display_targets() or [])
                          if str(p.get("topic")) == topic]
            except Exception:                      # noqa: BLE001
                panels = []
            for p in panels:
                # THE SAME RECORD-VS-PLAN SPLIT, ONE LEVEL OVER.
                # display_targets() reads the board RECORD, so it offers a
                # panel whose part the PLAN refused -- and that refusal is
                # also why the part never reached the firmware template.
                # The hub then wakes a screen the chip was never told it
                # has: DisplayModel, DisplayMode 0, DisplayMode 1, all
                # published, all accepted by the broker, none of them
                # reaching a configured display. A dark panel, for ever,
                # with every command apparently succeeding.
                if str(p.get("name")) not in placed:
                    print(f"  panel: {p.get('name')} IS A TARGET BUT ITS "
                          f"PART WAS REFUSED BY THE PLAN -- the hub will "
                          f"keep sending display commands that cannot "
                          f"land, because the chip was never configured "
                          f"for this screen. This is the dark panel.")
                else:
                    print(f"  panel: {p.get('name')} "
                          f"{p.get('cols')}x{p.get('rows')} -- on the plan, "
                          f"and the hub will wake it and write to it")
            if not panels and any(str(i.get("type_id", "")).startswith("lcd")
                                  for i in items):
                print("  panel: ON THE RECORD BUT NOT A TARGET -- the hub "
                      "will never wake or write to this screen")
        except Exception as exc:                   # noqa: BLE001
            print(f"  could not build the index here: "
                  f"{type(exc).__name__}: {exc}")

        # ── 3b. AND IS IT A DEVICE THE APP KNOWS ABOUT? ───────────────
        #
        # THE THIRD SOURCE OF TRUTH, and the one nothing had checked.
        #
        #   the board RECORD    ~/.homevoice/boards/<slug>.json -- wired
        #   the board PLAN      _plan_for()                     -- template
        #   the DEVICE REGISTRY data/devices.json               -- the app
        #
        # The engine's index above is built from the PLAN. The app's home
        # tiles and its Statistics screen are built from the REGISTRY, by
        # live_reading_rows walking dm.devices. A part can be wired, on the
        # plan, indexed, publishing perfectly -- and absent from the
        # registry, in which case the hub understands every reading it
        # sends and the app has nowhere to put them.
        #
        # That is "the pzem reading is not showing on the app" with the
        # meter demonstrably publishing ENERGY, which is where this began.
        try:
            with open(config.DEVICES_JSON_PATH, encoding="utf-8") as fh:
                registry = json.load(fh)
            if not isinstance(registry, dict):
                registry = {}
        except Exception as exc:                   # noqa: BLE001
            registry = None
            print(f"  could not read {config.DEVICES_JSON_PATH}: {exc}")
        if registry is not None:
            unregistered, hidden = [], []
            for i in items:
                d_id = DeviceManager.board_device_id(slug, i.get("name"))
                rec = registry.get(d_id)
                if rec is None:
                    unregistered.append(f"{i.get('name')} ({d_id})")
                elif isinstance(rec, dict) and rec.get("show_on_home") is False:
                    hidden.append(str(i.get("name")))
            print(f"  in the app's device registry: "
                  f"{len(items) - len(unregistered)}/{len(items)}")
            if unregistered:
                print(f"    NOT REGISTERED: {', '.join(unregistered)}")
                print("    ^ wired and understood by the hub, but with no "
                      "record in devices.json -- so nothing reads or "
                      "drives them and NO TILE CAN EVER APPEAR for them. "
                      "Notifications offers 'Register them'; Board report "
                      "lists them too.")
            if hidden:
                print(f"    registered but SHOW ON HOME = NO: "
                      f"{', '.join(hidden)}")
                print("    ^ these are working and deliberately kept off "
                      "the home screen. They still appear under Settings > "
                      "Readings & statistics.")

        # ── 4. IS THE BOARD ACTUALLY TALKING? ─────────────────────────
        if not topic:
            print("  cannot listen: this record carries no MQTT topic")
            continue
        # LISTENING ALONE PROVES NOTHING, and the first version of this tool
        # pretended otherwise. Tasmota publishes tele/<topic>/SENSOR every
        # TelePeriod, which this hub never sets -- so it is Tasmota's
        # default of 300 SECONDS. A fifteen-second listen could not catch a
        # reading from a perfectly healthy board, and "the board said
        # nothing" would have been a false verdict.
        #
        # So ASK. Three questions, answered in milliseconds:
        #
        #   Status 2   the firmware it runs. The SAFEBOOT build answers
        #              {"Command":"Unknown","Input":"STATUS"} -- it lacks
        #              the core commands -- which is a board that joins
        #              Wi-Fi, reports Online and does nothing else. That
        #              single fact would explain a dead meter, a dark
        #              panel and a silent card reader at once.
        #   Status 10  what its sensors say RIGHT NOW, with no waiting.
        #              This is the meter's answer.
        #   TelePeriod how long the silence above was ever going to be.
        print(f"  asking Status 2, Status 10 and TelePeriod, then listening "
              f"for {seconds:.0f}s ...")
        # topic bound per board: this fires from client.loop(), and must
        # never see a later board's topic.
        def ask(_c, topic=topic):
            _c.subscribe(f"tele/{topic}/+")
            _c.subscribe(f"stat/{topic}/+")
            # SUBSCRIBE BEFORE ASKING -- a reply that beats the
            # subscription is a reply that never happened.
            _c.publish(f"cmnd/{topic}/Status", "2")
            _c.publish(f"cmnd/{topic}/Status", "10")
            _c.publish(f"cmnd/{topic}/Status", "7")
            _c.publish(f"cmnd/{topic}/TelePeriod", "")
            # WHICH DRIVERS THIS BOARD ACTUALLY CARRIES.
            #
            # Not the GPIO enum -- that is compiled unconditionally and
            # proves nothing (check_firmware.py: bedroom's firmware
            # contained "RC522 CS" while containing no driver at all).
            # A COMMAND is the proof: a build without USE_DISPLAY
            # answers {"Command":"Unknown","Input":"DISPLAYMODEL"},
            # and a build with it answers {"DisplayModel":N}.
            _c.publish(f"cmnd/{topic}/DisplayModel", "")
            _c.publish(f"cmnd/{topic}/Sensor80", "")
            # A LIVE DRIVER IS NOT A WORKING SCREEN. DisplayModel 1
            # only says the LCD driver is up and found a backpack on
            # the I2C bus -- Tasmota sets the model BY DETECTION. What
            # decides whether anything is on the glass is the MODE (0
            # is "cleared and showing nothing", 1 is the clock) and the
            # DIMMER (0 is the backlight off). A blank panel is one of
            # those two far more often than it is a broken driver.
            _c.publish(f"cmnd/{topic}/DisplayMode", "")
            _c.publish(f"cmnd/{topic}/DisplayDimmer", "")
            _c.publish(f"cmnd/{topic}/I2Cscan", "")
            # THE BOARD'S ADDRESS, so its console is one click away.
            # The RC522 driver announces itself at BOOT with "MFR:
            # RC522 Rfid Reader" and only when it actually reads the
            # chip's version register over SPI -- a driver that is
            # compiled in but finds no reader says nothing and reports
            # no taps for ever. That line goes to the serial and web
            # log and never to MQTT, so it cannot be asked for here;
            # it can only be READ, on the board's own console page.
            _c.publish(f"cmnd/{topic}/Status", "5")

        try:
            heard, rc = converse(esp, ask, seconds)
        except Exception as exc:                   # noqa: BLE001
            print(f"    could not listen: {type(exc).__name__}: {exc}")
            continue
        # THE HUB'S END FAILING IS NOT THE BOARD BEING SILENT. A refused
        # login used to land in `heard` as a message, and the verdict below
        # then called the board "on the broker but not answering".
        if rc != 0:
            print("    could not listen: " + (
                "the broker never answered the hub's connection"
                if rc is None else f"the broker refused the hub login "
                                   f"(rc={rc})"))
            continue
        # WHOLE texts: cut to 160 here, every longer JSON reply (Status 10,
        # a busy SENSOR) failed to parse below. Cut only when printed.
        heard = [(t.rsplit("/", 1)[-1], text) for t, text in heard]

        # ── THE VERDICT ───────────────────────────────────────────────
        #
        # Read off the ANSWERS, not off the silence. The questions were
        # asked a moment ago, so a board that is running and healthy has
        # already replied by now whatever its TelePeriod is.
        blob = " ".join(t for _tail, t in heard)
        answered = any(tail.upper().startswith("STATUS") for tail, _t in heard)
        # PARSED, not string-matched. The first version of this uppercased
        # the haystack and left the needle mixed-case, so it matched
        # NOTHING and reported a stock image as healthy -- a diagnostic
        # that lies quietly is worse than none.
        # "Unknown" AND "Error" ARE OPPOSITE ANSWERS, and reading them as
        # the same thing would have been the worst mistake in this file.
        #
        #   {"Command":"Unknown","Input":"SENSOR80"}  no such command ->
        #                                             the driver is NOT in
        #   {"Command":"Error","Input":"SENSOR80"}    the command exists
        #                                             and did not like its
        #                                             arguments -> THE
        #                                             DRIVER IS IN
        #
        # Only Unknown is proof of absence. Error is proof of PRESENCE.
        refused, errored, values = set(), set(), {}
        for _tail, text in heard:
            try:
                data = json.loads(text)
            except ValueError:
                continue
            if not isinstance(data, dict):
                continue
            verdict_word = str(data.get("Command", "")).strip().lower()
            if verdict_word == "unknown":
                refused.add(str(data.get("Input", "")).strip().upper())
            elif verdict_word == "error":
                errored.add(str(data.get("Input", "")).strip().upper())
            for key in ("DisplayMode", "DisplayDimmer", "DisplayModel",
                        "I2CScan"):
                if key in data:
                    values[key] = data[key]
        # SAFEBOOT is an Unknown answer to a CORE command (Status). An
        # Unknown to DisplayModel or Sensor80 -- asked above precisely to
        # find a missing driver -- is a full image without that driver,
        # and must not be read as safeboot.
        unknown_cmd = bool(refused & {"STATUS", "TELEPERIOD"})
        live = "ENERGY" in blob.upper()

        if not heard:
            print("    THE BOARD SAID NOTHING AT ALL -- not even a refusal.")
            print("    It is off, crashed, or off the network. Every symptom "
                  "on this board follows from that, and none of them can be "
                  "fixed in the hub.")
        elif unknown_cmd:
            print("    IT IS IN SAFEBOOT. The board answered 'Unknown' to a "
                  "core command, which only Tasmota's minimal recovery build "
                  "does. It joins Wi-Fi, connects to the broker and reports "
                  "Online -- and runs no sensor, no display and no card "
                  "reader. That is every symptom on this board, at once.")
            print("    The hub heals this by itself (hub/ota_server.py): "
                  "restart the hub and watch for [OTA] lines, or run Board "
                  "report on it.")
        elif not answered:
            print("    IT IS ON THE BROKER BUT DID NOT ANSWER A QUESTION.")
            print("    Its LWT says Online, so the broker has a live session "
                  "for it -- but Status went unanswered, which a running "
                  "Tasmota always replies to within a moment. It is wedged, "
                  "mid-reboot-loop, or the retained Online is stale and the "
                  "board is actually gone. Power-cycle it and run this "
                  "again.")
        elif not live and any(str(i.get("type_id")) in esp._ENERGY_TYPES
                              for i in items):
            print("    IT IS RUNNING, BUT ITS SENSORS REPORT NOTHING.")
            print("    Status 10 is what a board says about its sensors "
                  "RIGHT NOW, with no waiting -- and no ENERGY block came "
                  "back. The firmware is fine and the hub is fine, so the "
                  "meter itself is the thing to look at: its wiring to the "
                  "board, its own power, and that its Tx/Rx are not "
                  "swapped.")
        # A BLANK PANEL, SPECIFICALLY. The wake's last stage is
        # `DisplayMode 1`, and on a character LCD that mode shows THE TIME
        # AND DATE -- from the board's own clock, over NTP, which needs the
        # internet. This hub never sends a board an NtpServer, a Timezone
        # or a Time, so a board on a house network with no internet has no
        # time to show: the screen is cleared by stage 2 and then asked to
        # display a clock that does not exist. Cleared, lit, and empty --
        # which is exactly "the lcd outputs blank when initialized", and is
        # a different fault from the gibberish a missed init gives.
        if any(str(i.get("type_id", "")).startswith("lcd") for i in items):
            stamp = re.search(r'"(?:UTC|Local)"\s*:\s*"([^"]+)"', blob)
            if stamp:
                when = stamp.group(1)
                bad = when.startswith(("1970", "2000", "0000"))
                print(f"    the board's own clock says {when}"
                      + (" -- IT HAS NEVER HAD THE TIME. DisplayMode 1 is "
                         "the time and date, so the panel is cleared and "
                         "then asked to show a clock that does not exist: "
                         "lit, and blank. The board gets its time from NTP "
                         "over the internet and this hub never sends it "
                         "one." if bad else " -- so a blank panel is not "
                         "the clock's fault"))
            elif answered:
                print("    the board did not report a time (Status 7), so "
                      "whether DisplayMode 1 has a clock to show is not "
                      "known -- ask it `Status 7` directly")

        # WHAT THIS FIRMWARE CAN AND CANNOT DO, from its own answers.
        #
        # THE WHOLE POINT: a board can be healthy, on the broker, keeping
        # good time and publishing its meter -- and still have no display
        # and no card reader, because those two drivers are NOT in stock
        # Tasmota and this project adds them. Then the meter works, the
        # panel is blank and the reader never reports a tap, which reads as
        # three faults and is one: the wrong image. (refused / errored are
        # parsed above, with the verdict.)
        for label, probe, need in (
                ("DISPLAY DRIVER (USE_DISPLAY / USE_DISPLAY_LCD)",
                 "DISPLAYMODEL",
                 "a blank panel is exactly what that looks like"),
                ("RC522 CARD READER (xsns_80_mfrc522)", "SENSOR80",
                 "a tap can never be reported, however well it is wired")):
            if probe in refused:
                print(f"    THIS FIRMWARE HAS NO {label} -- {need}.")
                print("      The board is healthy; it is running an image "
                      "that lacks the driver. Neither is in stock Tasmota "
                      "-- this project's own build adds them. Check which "
                      "file this hub flashes: "
                      "python3 tools/check_firmware.py")
            elif probe in errored:
                print(f"    the {label} IS in this firmware -- the command "
                      f"exists and only objected to its arguments, which "
                      f"'Unknown' would not have done.")

        # THE BLANK PANEL, NARROWED. Everything above can be perfect and
        # the glass still empty, so say which of the two things that
        # actually put characters on it is wrong.
        if "DisplayMode" in values or "DisplayDimmer" in values:
            mode = values.get("DisplayMode")
            dim = values.get("DisplayDimmer")
            print(f"    display: Model={values.get('DisplayModel', '?')} "
                  f"Mode={mode if mode is not None else '?'} "
                  f"Dimmer={dim if dim is not None else '?'}")
            if mode == 0:
                print("      MODE 0 IS 'CLEARED AND SHOWING NOTHING'. That "
                      "is the blank panel, and it is not a fault in the "
                      "screen: the hub's wake sends DisplayMode 0 to clear "
                      "and then DisplayMode 1 to put the clock back, so a "
                      "panel left on 0 is a wake that stopped after its "
                      "second step. Send it `DisplayMode 1` to prove the "
                      "glass works.")
            if dim == 0:
                print("      DIMMER 0 IS THE BACKLIGHT OFF -- nothing will "
                      "be readable whatever the mode says.")
        ip = re.search(r'"IPAddress"\s*:\s*"([0-9.]+)"', blob)
        if ip and any(str(i.get("type_id")) in ("rc522", "pn532")
                      for i in items):
            print(f"    the board's console: http://{ip.group(1)}/cs")
            print("      ^ TAP A CARD and watch. The RC522 driver logs "
                  "'MFR: RC522 Rfid Reader' ONCE AT BOOT, and only when "
                  "it really read the chip over SPI -- a driver compiled "
                  "in but finding no reader is silent for ever. That line "
                  "never goes to MQTT, so it can only be read there. "
                  "Restart the board with the console open to see it.")
        if "I2CScan" in values:
            print(f"    I2C bus: {values['I2CScan']}")
            print("      ^ a character LCD's backpack answers at 0x27 or "
                  "0x3F. No address there means the screen's wiring or its "
                  "power, not the firmware.")

        if heard:
            tails = {}
            for tail, _text in heard:
                tails[tail] = tails.get(tail, 0) + 1
            print(f"    the board is TALKING -- {len(heard)} message(s): "
                  + ", ".join(f"{t} x{n}" for t, n in sorted(tails.items())))
            # EVERY MESSAGE, not the first eight. The count line said
            # "11 message(s)" while this printed 8 -- so three arrived and
            # were never shown, and the whole reason to run this while
            # tapping a card is to see whether an RC522 block is among
            # them. A diagnostic that hides its own evidence is worse than
            # no diagnostic.
            for tail, text in heard:
                print(f"      {tail}: {text[:160]}")
            # Which JSON keys arrived, against what the hub knows.
            keys = set()
            for _tail, text in heard:
                try:
                    data = json.loads(text)
                except ValueError:
                    continue
                if isinstance(data, dict):
                    keys |= {str(k).upper() for k in data}
            known = {str(k).upper() for (_kind, k) in table}
            unknown = sorted(keys - known - {"TIME"})
            if unknown:
                print(f"    blocks the hub has no entry for: "
                      f"{', '.join(unknown)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
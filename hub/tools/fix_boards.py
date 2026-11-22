"""Send `Sleep 0` to boards that need the CPU awake, over MQTT, now.

Tasmota's default is `SleepMode Dynamic` / `Sleep 50`: the CPU idles up to
50 ms between loop passes. That is right for a battery sensor and wrong for
every board this hub provisions, because it starves the drivers that need
the processor:

    PZEM        bit-banged serial -- a frame that lands while the CPU is
                asleep is corrupt, so the meter publishes a complete ENERGY
                block of ZEROS. Not missing. Zero.
    IR receive  pulse widths in microseconds, destroyed by a sleep measured
                in milliseconds.
    RC522       the driver POLLS for a card once per loop; sleeping through
                most of them means most taps are never seen.
    displays    I2C writes are issued from the loop too.

What keeps working is the tell: relays are direct GPIO writes and a PIR is
an interrupt. Kitchen's relays and motion sensor were fine while its meter,
panel, card reader and IR receiver were all "broken" -- four symptoms, one
cause, sitting in the board's own STATE the whole time:

    {"SleepMode":"Dynamic","Sleep":50,"LoadAvg":46, ...}

This is the one-shot repair for boards already installed. New boards get
`Sleep 0` at provisioning from now on (esp_builder._provisioning_commands).

    python3 tools/fix_boards.py                   # show what would be sent
    python3 tools/fix_boards.py kitchen --apply    # send Sleep 0
    python3 tools/fix_boards.py kitchen --lcd --apply   # clear a garbled
                                                       # panel and put the
                                                       # clock back
    python3 tools/fix_boards.py kitchen --meter --apply  # make the energy
                                                       # meter report on
                                                       # CHANGE instead of
                                                       # every 300s
    python3 tools/fix_boards.py kitchen --boot --apply  # restart it and read
                                                       # its BOOT LOG over
                                                       # MQTT -- which
                                                       # driver found its
                                                       # chip, and which
                                                       # did not

The setting is saved in the board's own flash and survives a reboot. It is
reversible: `Sleep 50` puts the default back.
"""
import os
import sys
import time


HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)
os.environ.setdefault("HOMEVOICE_NO_AUTOSTACK", "1")

from core import config                                                   # noqa: E402
from esp_firmware.esp_builder import EspBuilder                 # noqa: E402
from tools.board_mqtt import converse                           # noqa: E402

#: SELF-CONTAINED ON PURPOSE. The first version of this script called
#: EspBuilder._needs_no_sleep, which only exists in the UPDATED builder --
#: so the one tool whose whole job is repairing an installed hub could not
#: run until the hub had already been updated. A repair tool must not
#: depend on the repair.
#:
#: Parts whose driver is starved by Tasmota's dynamic sleep: bit-banged
#: serial, microsecond pulse timing, and per-loop polling.
#: EVERY BOARD GETS Sleep 0 (2026-09-12, her question: "only for these
#: devices??"). It used to be conditional on a hand-typed list, and the
#: list was wrong: it named the four parts that had bitten and omitted
#: every other bit-banged driver in the catalogue. Tasmota's dynamic sleep
#: is a BATTERY setting and this hub provisions no battery devices, so the
#: condition is gone entirely.
#:
#: What survives is the EXPLANATION -- which parts on a board make it
#: load-bearing rather than merely tidy -- and even that is DERIVED from
#: the catalogue rather than typed. The builder owns the rule; this asks
#: it.
def sleep_critical_parts(record) -> list:
    """Parts on this board whose driver needs the CPU awake, from the
    catalogue itself. Empty list if this hub predates the rule -- the
    setting still goes out either way, so the worst case is a less
    specific sentence."""
    try:
        return EspBuilder._sleep_critical_parts(record)
    except Exception:                                  # noqa: BLE001
        return []


#: HER OWN HAND-CURE FOR A GARBLED PANEL, 2026-09-02: "the gibberish came
#: again until I did DisplayMode 0 which cleared everything and DisplayMode
#: 1 that brought back the time and date."
#:
#: Deliberately WITHOUT DisplayModel. That command RE-INITIALISES the driver
#: and is what MAKES the gibberish in the first place -- the hub's own wake
#: sends it first and then spends two more ticks undoing it. Sending only
#: the clear and the clock is the shorter road to the same place, and it is
#: the one she found by hand.
#:
#: Needed as a separate act because the hub wakes a panel ONCE per run: a
#: board whose screen garbles after that is never woken again, and there is
#: no way to ask for it.
LCD_CURE = (("DisplayMode", "0"), ("DisplayMode", "1"))

#: A METER THAT REPORTS WHEN IT CHANGES, not when a timer says so.
#:
#: The PZEM already measures about once a second and Tasmota reads it
#: continuously -- but it only PUBLISHES on TelePeriod, and this hub never
#: set one, so it was Tasmota's default of 300 SECONDS. The meter was never
#: the slow part; the silence between publishes was.
#:
#: `PowerDelta` sends the whole ENERGY block the moment power moves by more
#: than N percent: a kettle appears the second it is switched on, and a
#: quiet house still sends nothing. TelePeriod comes down to a minute as
#: the floor for what PowerDelta does not watch -- voltage, power factor,
#: the running total.
METER_LIVE = (("PowerDelta", str(config.POWER_DELTA_PERCENT)),
              ("TelePeriod", str(config.METER_TELE_PERIOD)))

#: THE BOOT LOG, WITHOUT OPENING THE BOARD'S WEB PAGE.
#:
#: A driver that is COMPILED IN is not a driver that FOUND ITS CHIP. The
#: RC522 driver reads the reader's version register over SPI at boot; on
#: success it logs "MFR: RC522 Rfid Reader", and on failure it logs nothing
#: and reports no tap for ever. `Sensor80` answering "Error" rather than
#: "Unknown" proves only the first half of that.
#:
#: That line goes to the serial and web log and never to an ordinary MQTT
#: topic -- which is why the first answer here was "open the console". But
#: Tasmota can mirror its whole log to MQTT: `MqttLog 3` publishes every
#: line to tele/<topic>/LOGGING. So the boot log can be read FROM THE HUB,
#: and nobody has to open a board's web page -- which is this project's
#: design, not a preference ("the design is that nobody ever opens a
#: board's web page; the hub asks, the person reads").
#:
#: Set it, restart, listen, put it back.
BOOT_LOG_LEVEL = "3"

#: Lines worth pulling out of the noise: a driver announcing it found its
#: hardware, and the bus lines that say whether it could have.
BOOT_MARKERS = ("MFR:", "RC522", "PN532", "DSP:", "LCD", "PZEM",
                "IRR", "I2C", "SPI", "Project", "FAIL", "not found")


def main() -> int:
    args = sys.argv[1:]
    apply = "--apply" in args
    # ONE mode, decided once. The dry run and the send used to rank the
    # flags differently, so `--boot --meter` promised PowerDelta and then
    # restarted the board.
    mode = next((m for m in ("boot", "meter", "lcd") if f"--{m}" in args),
                "sleep")
    boot, meter, lcd = mode == "boot", mode == "meter", mode == "lcd"
    want = next((a for a in args if not a.startswith("-")), "")

    esp = EspBuilder.headless()
    slugs = [s for s in esp.list_nodes() if not want or s == want]
    if not slugs:
        print(f"no board called {want!r}" if want else "no boards")
        return 2

    records = {slug: esp._read_board(slug) or {} for slug in slugs}
    for slug, record in records.items():
        topic = record.get("topic", "")
        why = sleep_critical_parts(record)
        print(f"{slug:<12} topic {topic!r:<16} Sleep 0"
              + (f" -- critical for {', '.join(why)}" if why
                 else " -- no timing-critical part, but it is mains "
                      "powered and the setting costs nothing"))
    # EVERY board named, not only the timing-critical ones -- for --boot
    # too: reading a boot log is a question, not a repair.
    targets = [(sl, rec["topic"]) for sl, rec in records.items()
               if rec.get("topic")]
    carries = {"meter": (EspBuilder._ENERGY_TYPES, "an energy meter"),
               "lcd": (EspBuilder._display_types(), "a character LCD")}.get(mode)
    if carries:
        # Only boards that actually carry the part.
        targets = [(sl, t) for sl, t in targets
                   if any(isinstance(i, dict) and i.get("type_id")
                          in carries[0]
                          for i in (records[sl].get("items") or []))]
        if not targets:
            print(f"\nNo board here carries {carries[1]}.")
            return 0
    if not targets:
        return 0
    if not apply:
        what = {"meter": " + ".join(f"{c} {v}" for c, v in METER_LIVE) + " to ",
                "lcd": "the LCD clear-and-clock to ",
                "boot": "MqttLog 3 + Restart to (and read the boot log of) ",
                "sleep": "`Sleep 0` to "}[mode]
        print("\nNothing sent. Re-run with --apply to send "
              + what + ", ".join(s for s, _t in targets) + ".")
        return 0

    def ask(_c):
        for _slug, topic in targets:
            _c.subscribe(f"stat/{topic}/RESULT")
        if boot:
            for _slug, topic in targets:
                _c.subscribe(f"tele/{topic}/LOGGING")
                _c.publish(f"cmnd/{topic}/MqttLog", BOOT_LOG_LEVEL)
            time.sleep(1.0)
            for _slug, topic in targets:
                _c.publish(f"cmnd/{topic}/Restart", "1")
            return
        if meter:
            for _slug, topic in targets:
                for cmd, val in METER_LIVE:
                    _c.publish(f"cmnd/{topic}/{cmd}", val)
            return
        if lcd:
            # CLEAR, THEN THE CLOCK -- with a breath between them. Sent
            # back to back the clear can still be in flight when the
            # clock arrives, and the panel keeps the rubbish.
            for i, (cmd, val) in enumerate(LCD_CURE):
                if i:
                    time.sleep(1.5)
                for _slug, topic in targets:
                    _c.publish(f"cmnd/{topic}/{cmd}", val)
            return
        for _slug, topic in targets:
            _c.publish(f"cmnd/{topic}/Sleep", "0")

    def is_ack(text):
        if meter:
            return "PowerDelta" in text or "TelePeriod" in text
        return boot or (("DisplayMode" in text) if lcd else ("Sleep" in text))

    def put_log_back(c):
        for _slug, topic in targets:
            c.publish(f"cmnd/{topic}/MqttLog", "0")
        for _ in range(10):
            c.loop(timeout=0.2)

    want_acks = len(targets) * (len(METER_LIVE) if meter else 1)
    try:
        # A BOOT TAKES ITS TIME. Restart, Wi-Fi, broker, then the drivers
        # announcing themselves -- the RC522 line lands well after the
        # board is back on MQTT, so this waits out the whole of it rather
        # than stopping at the first message.
        heard, rc = converse(
            esp, ask, 45.0 if boot else 10.0,
            None if boot else
            (lambda msgs: sum(is_ack(t) for _, t in msgs) >= want_acks),
            put_log_back if boot else None)
    except Exception as exc:                           # noqa: BLE001
        print(f"could not reach the broker: {type(exc).__name__}: {exc}")
        return 2
    if rc != 0:
        print(f"the broker refused the hub login (rc={rc})" if rc
              else "the broker never answered the hub's connection")
        return 2
    acks = [(top, text) for top, text in heard if is_ack(text)]

    if not acks:
        print("\nSent, but no board acknowledged. Check they are on the "
              "broker.")
        return 1
    if boot:
        print(f"\n{len(acks)} log line(s). The ones that name hardware:\n")
        shown = 0
        for _topic, text in acks:
            if any(mark.lower() in text.lower() for mark in BOOT_MARKERS):
                print(f"  {text.strip()[:160]}")
                shown += 1
        if not shown:
            print("  (none -- the whole log follows)")
            for _topic, text in acks[:60]:
                print(f"  {text.strip()[:160]}")
        # DID THE METHOD ITSELF WORK? Answering "no RC522 line" when NO
        # LOG LINES ARRIVED AT ALL is not a diagnosis, it is a confident
        # sentence built on nothing -- and this printed exactly that once
        # (2026-09-12). Absence is only evidence when presence was
        # possible, so that has to be established first.
        logs = [t for top, t in acks if top.endswith("/LOGGING")]
        if not logs:
            print("\nNO LOG LINES ARRIVED, so this proves NOTHING about "
                  "the reader.")
            print("  MqttLog can only carry lines produced AFTER the broker "
                  "connection exists, and a driver announces itself during "
                  "boot -- before Wi-Fi, before MQTT. The one line worth "
                  "reading is written when there is nothing to publish it "
                  "to. Everything above is ordinary command replies, not "
                  "log output.")
            print("  The boot log lives in the board's own web console, "
                  "which buffers it in RAM and shows it once the page "
                  "loads: http://<board-ip>/cs -- restart with that page "
                  "open. This is the one question the hub genuinely cannot "
                  "ask for you.")
            print("MqttLog has been put back to 0.")
            return 1
        found = any("rc522" in t.lower() for t in logs)
        print("\n" + ("The RC522 ANNOUNCED ITSELF -- the reader is "
                      "detected on SPI, so the wiring is good and the "
                      "fault is above it."
                      if found else
                      f"{len(logs)} log line(s) arrived and none names the "
                      f"RC522. Suggestive, not conclusive: a driver "
                      f"announces itself before MQTT exists, so its line "
                      f"may simply have been written too early to travel. "
                      f"The board's web console is the certain answer."))
        print("MqttLog has been put back to 0.")
        return 0
    print()
    for topic, text in acks:
        print(f"  {topic}  {text[:120]}")
    if meter:
        print(f"\nThe meter will now publish the moment power moves more "
              f"than {config.POWER_DELTA_PERCENT}%, instead of waiting up "
              f"to five minutes. Switch something on and watch the app.")
        print("To undo: PowerDelta 0 and TelePeriod 300.")
        return 0
    if lcd:
        print("\nThe panel was cleared and put back on the clock. If it is "
              "still garbled, the fault is below the hub: the screen's own "
              "I2C wiring, its address, or its contrast trimpot.")
    else:
        print("\n`Sleep 0` is saved in each board's own flash and survives "
              "a reboot. The meter should report real voltage within one "
              "TelePeriod (300 s by default), and the card reader and IR "
              "receiver should answer immediately.")
        print("To undo: send `Sleep 50`.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
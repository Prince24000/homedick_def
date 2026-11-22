"""WHICH firmware file does this hub actually flash, and what is in it?

Run this on the machine that does the flashing:

    cd voice_ai
    python3 tools/check_firmware.py

Why it exists: `EspBuilder._firmware()` picks its file by SEARCHING -- an
absolute path in config (esp_firmware/firmware/) first, then esp_firmware/, then esp_firmware/
firmware/, then EVERY PARENT DIRECTORY above it. That search has never
said out loud which file it landed on, so replacing "the firmware" in one
folder while an older copy sits in another produces exactly the symptom
that costs an evening: a board flashed with a build nobody meant to keep,
and a person certain the new one is in place. (Augusta, 2026-08-24 --
"it has been downloaded a long time ago, nothing was changed".)

This prints the winner, every runner-up, and what each file says about
itself, so the question stops being a matter of belief.
"""

import re
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
HERE = ROOT / "esp_firmware"
sys.path.insert(0, str(ROOT))

_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun",
           "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")
#: Tasmota builds its BuildDateTime at RUN time out of the compiler's
#: __DATE__ ("Aug 18 2026") and __TIME__ ("09:39:00"), so the binary carries
#: the two halves separately -- never the joined form the console prints.
_DATE = re.compile((r"(?:%s)\s+\d{1,2}\s+20\d{2}" % "|".join(_MONTHS)).encode())
_TIME = re.compile(rb"(?:[01]\d|2[0-3]):[0-5]\d:[0-5]\d")
_VERSION = re.compile(rb"\d+\.\d+\.\d+\(tasmota[0-9a-z_]*\)")

#: THE ENUM-VS-DRIVER LESSON, AT THE BINARY LEVEL -- and it is not a
#: theory any more. Bedroom's own firmware, 2026-08-24, contained
#: "RC522 CS" and "RC522 Rst" while containing no driver at all: those two
#: are GPIO NAME-TABLE entries and they are compiled unconditionally, so a
#: perfectly stock build carries them. The same file had no "Rfid Reader"
#: and no "MFRC522" -- the driver's own log line and its library -- because
#: the driver was never built in.
#:
#: So each entry here is a PAIR: the name that proves nothing, and the
#: string that only the driver itself can leave behind.
_DRIVERS = {
    "RC522 reader": {"name": (b"RC522 CS", b"RC522 Rst"),
                     "driver": (b"Rfid Reader", b"MFRC522")},
    "PN532 reader": {"name": (b"PN532 Tx",), "driver": (b"NFC Reader",)},
    "PZEM meter": {"name": (b"PZEM0XX Tx", b"PZEM004T"), "driver": ()},
    # THE LESSON THIS PROJECT ALREADY LEARNED, AND THIS FILE DID NOT TAKE
    # (2026-09-12). This row used to be {"name": b"DisplayModel", "driver":
    # b"LiquidCrystal"} -- and it reported "LCD display  not present at
    # all" for an image that HAS the display core and the LCD driver, while
    # hub/ota_server.py, probing the SAME FILE, correctly reported
    # "drivers in: ... Display LCD". Two tables, one truth, and the wrong
    # one was the one a person runs when they want to know what is in the
    # firmware -- so it sent the hunt off after a missing driver that was
    # never missing.
    #
    # ota_server.py's own comment says why, and records that it was found
    # over there first:
    #
    #   "Nor is 'DisplayMode' as one word: Tasmota keeps its command names
    #    in a '|'-joined table with the prefix separate (kDisplayCommands =
    #    "Display||Model|Type|Width|Height|Mode|..."), so the joined word
    #    never exists in a binary -- the first draft of this table looked
    #    for it and called a working display 'NOT in'."
    #
    # `LiquidCrystal` is the same mistake one level down: a C++ class name
    # out of the library header, which leaves no string in a binary. The
    # driver's own log line does -- "DSP: LCD".
    #
    # The markers are imported from ota_server below so there is ONE table.
    # These are the fallback for running this file where the hub package
    # cannot be imported.
    "Display core": {"name": (), "driver": (
        b"Display||Model|Type|Width|Height|Mode|",)},
    "LCD display": {"name": (), "driver": (b"DSP: LCD",)},
}

#: ONE TABLE, when the hub package is importable. Only the driver half is
#: taken: the `name` half is this file's own job -- it is what warns that a
#: GPIO entry exists with no driver behind it, the trap ota_server
#: deliberately does not track.
try:
    from hub.ota_server import PROJECT_DRIVERS as _OTA_DRIVERS
    _OTA_BY_NAME = dict(_OTA_DRIVERS)
    for _label, _key in (("Display core", "Display"),
                         ("LCD display", "LCD"),
                         ("RC522 reader", "RC522")):
        _marker = _OTA_BY_NAME.get(_key)
        if _marker and _label in _DRIVERS:
            _DRIVERS[_label] = {
                "name": _DRIVERS[_label].get("name", ()),
                "driver": tuple(set(_DRIVERS[_label].get("driver", ()))
                                | {_marker}),
            }
except Exception:                                  # noqa: BLE001
    pass                                           # the fallbacks above stand

#: THE 2026-08-25 SWEEP. I²C drivers own no GPIO component, so there is
#: no misleading name-table string to warn about -- `name` stays empty
#: and the driver's own tele-SENSOR block name is the whole proof. The
#: same strings are what homevoice-build.yml greps in CI: this probe is
#: the bench-side half of one method.
_DRIVERS.update({
    "SHT3x":       {"name": (), "driver": (b"SHT3X",)},
    "AHT2x":       {"name": (), "driver": (b"AHT2",)},
    "HTU21":       {"name": (), "driver": (b"HTU21",)},
    "AM2320":      {"name": (), "driver": (b"AM2320",)},
    "BMP/BME280":  {"name": (), "driver": (b"BMP280",)},
    "BME680":      {"name": (), "driver": (b"BME680",)},
    "MLX90614":    {"name": (), "driver": (b"MLX90614",)},
    "Seesaw soil": {"name": (), "driver": (b"eesaw", b"SeeSoil")},
    "Chirp soil":  {"name": (), "driver": (b"hirp",)},
    "VL53L0X":     {"name": (), "driver": (b"VL53L0X",)},
    "VL53L1X":     {"name": (), "driver": (b"VL53L1X",)},
    "QMC5883L":    {"name": (), "driver": (b"QMC5883L",)},
    "HMC5883L":    {"name": (), "driver": (b"HMC5883L",)},
    "ADS1115":     {"name": (), "driver": (b"ADS1115",)},
    "INA226":      {"name": (), "driver": (b"INA226",)},
    "AS3935":      {"name": (), "driver": (b"AS3935",)},
    "SGP30":       {"name": (), "driver": (b"SGP30",)},
    "CCS811":      {"name": (), "driver": (b"CCS811",)},
    "SCD30":       {"name": (), "driver": (b"SCD30",)},
    "SCD40":       {"name": (), "driver": (b"SCD4",)},
    "SEN5x":       {"name": (), "driver": (b"SEN5",)},
    "VEML7700":    {"name": (), "driver": (b"VEML7700",)},
    "VEML6070":    {"name": (), "driver": (b"VEML6070",)},
    "SI1145":      {"name": (), "driver": (b"SI1145",)},
    "MAX44009":    {"name": (), "driver": (b"MAX44009",)},
    "BH1750":      {"name": (), "driver": (b"BH1750",)},
    # No SSD1306 probe: USE_DISPLAY_SSD1306 proved to be a name with no
    # driver behind it in this Tasmota (CI, 2026-08-25) -- OLEDs went to
    # the universal display driver, parked until a board proves a path.
    # Harvest-pending GPIO drivers: these DO own name-table strings, so
    # the enum-vs-driver pair applies to them in full.
    "HX711 scale": {"name": (b"HX711 SCK",), "driver": (b"HX711",)},
    "PMS5003":     {"name": (b"PMS5003",), "driver": ()},
    "MH-Z19":      {"name": (b"MHZ Tx",), "driver": (b"MHZ19",)},
    "SDS011":      {"name": (b"SDS0X1",), "driver": ()},
})


def candidates() -> "list[Path]":
    """Every file _firmware() would consider, in ITS order of preference --
    asked of the builder itself, so this list and the search it reports
    on can never drift apart."""
    from esp_firmware.esp_builder import EspBuilder
    return EspBuilder._firmware_candidates()


def ota_image() -> Path:
    """The app image hub/ota_server.py serves to RUNNING boards:
    tasmota32.bin beside the configured factory image (OtaImage's own
    rule), which is also where build_firmware.py installs it. A board
    that updated over the air is running THIS, not the factory file."""
    from core import config
    return Path(config.TASMOTA_FIRMWARE_PATH).parent / "tasmota32.bin"


def describe(path: Path) -> None:
    blob = path.read_bytes()
    size = len(blob)
    when = datetime.fromtimestamp(path.stat().st_mtime)
    print(f"    size            {size:,} bytes")
    print(f"    saved to disk   {when:%Y-%m-%d %H:%M}")
    first = blob[:1]
    kind = {b"\xe9": "OTA image (starts E9) -- for the board's web page",
            b"\xff": "FACTORY image (starts FF) -- what this hub flashes",
            b"P": "a ZIP, not firmware -- unpack the artifact first"}
    print(f"    kind            {kind.get(first, 'unrecognised first byte')}")
    versions = sorted({v.decode('ascii', 'replace')
                       for v in _VERSION.findall(blob)})
    print(f"    version         {', '.join(versions) or 'not found'}")
    dates = sorted({d.decode('ascii', 'replace')
                    for d in _DATE.findall(blob)})
    times = sorted({t.decode('ascii', 'replace')
                    for t in _TIME.findall(blob)})
    # EVERY date, not the first. A factory image is bootloader + partition
    # table + app, and the bootloader is prebuilt by ESP-IDF months
    # earlier -- reading the first match reports that one and says nothing
    # about which Tasmota is inside.
    print(f"    dates in it     {', '.join(dates) or '?'}")
    print(f"    times in it     {', '.join(times[:8]) or '?'}")
    print( "                    ^ one of these pairs IS the app's build; the")
    print( "                      board's own boot line says which.")
    for label, strings in _DRIVERS.items():
        named = any(n in blob for n in strings["name"])
        proof = strings["driver"]
        built = any(n in blob for n in proof) if proof else None
        if built is None:
            verdict = "name present" if named else "not present"
        elif built:
            verdict = "DRIVER BUILT IN"
        elif named:
            verdict = ("name only -- NO DRIVER (this is the trap: the "
                       "template would take the pins and nothing would read "
                       "them)")
        else:
            verdict = "not present at all"
        print(f"    {label:<15} {verdict}")


def families() -> None:
    """Every OTHER ESP32 family: the factory image a board chosen as that
    family is flashed with (config.firmware_path_for), and the app image
    the hub serves it over the air (ota/<its ota_name>, cut from the
    factory image when absent -- hub/ota_server.py)."""
    from core import config
    from esp_firmware import chips
    print("\nOther ESP32 families")
    print("=" * 62)
    for board in chips.FAMILIES.values():
        if board.family == "esp32":
            continue
        factory = Path(config.firmware_path_for(board))
        ota = factory.parent / board.ota_name
        if not factory.is_file():
            print(f"\n  {board.family_label:<10} no image ({factory.name}) -- "
                  f"a board of this family cannot be installed yet")
            continue
        print(f"\n  {board.family_label:<10} USES -> {factory}")
        try:
            describe(factory)
        except OSError as exc:
            print(f"    could not be read: {exc}")
        if ota.is_file():
            print(f"    OTA -> {ota}")


def main() -> int:
    from core import config
    config.settle_firmware_folder(say=print)
    print("\nThe firmware this hub would flash")
    print("=" * 62)
    found = candidates()
    ota = ota_image()
    if ota.is_file():
        print(f"\n  OTA   -> {ota}")
        print("    (served by the hub to RUNNING boards -- over-the-air "
              "updates and safeboot recovery install this one; a NEWER "
              "factory image below replaces it on the hub's next read)")
        try:
            describe(ota)
        except OSError as exc:
            print(f"    could not be read: {exc}")
    if not found:
        print("\n  NOTHING FOUND. No tasmota32.factory.bin anywhere this hub "
              "looks -- an install over the cable would refuse before "
              "touching a board.")
        families()
        return 1
    for i, path in enumerate(found):
        head = "  USES  ->" if i == 0 else "  ignored "
        print(f"\n{head} {path}")
        try:
            describe(path)
        except OSError as exc:
            print(f"    could not be read: {exc}")
    if len(found) > 1:
        print("\n" + "!" * 62)
        print("  MORE THAN ONE COPY EXISTS. Only the first is ever used.")
        print("  If you replaced one of the others, the swap did nothing --")
        print("  put the new build at the path marked USES, or delete the")
        print("  copies above it so there is only one answer.")
    families()
    print("\n  Decisive test, once a board is running: its console, command")
    print("  `GPIOs` (the SHORT list). A driver name that appears there IS")
    print("  in the binary. `Status 2` gives BuildDateTime to compare with")
    print("  the 'built' line above -- same build, same numbers.\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
"""Every offered board is drawn the way the real one is (Espressif's own
header tables), and the drawing agrees with the chip's pin rules.

Run: python3 tests/test_board_drawings.py
"""
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.setdefault("HOMEVOICE_HOME", tempfile.mkdtemp())

from esp_firmware import chips                               # noqa: E402
from esp_firmware import esphome_catalog as cat              # noqa: E402

PASSED = FAILED = 0
#: Pads the DevKitC V4 brings out that are the module's flash: drawn, never
#: assignable. The 30-pin V1 does not draw GPIO0/9/10 (they stay reachable).
FLASH_PADS = {"esp32-devkitc-38": {6, 7, 8, 11}}
NOT_DRAWN = {"esp32-devkit-30": {0, 9, 10}}


def check(name, ok, detail=""):
    global PASSED, FAILED
    if ok:
        PASSED += 1
        print(f"  [PASS] {name}")
    else:
        FAILED += 1
        print(f"  [FAIL] {name}   {detail}")


def drawn(board):
    return [(lab, n) for side in board.drawing().values() for lab, n in side]


def main():
    boards = chips.pickable_boards()
    print("\nwhat is offered")
    check("the 'Any ... board' lists are not offered",
          not any(chips.is_generic(b) for b in boards))
    check("every chip family has at least one named board",
          {b.family for b in boards} == set(chips.FAMILIES))
    check("the generics still resolve (old records, the cable's chip)",
          chips.get("esp32").id == "esp32"
          and chips.family_of_chip("ESP32-S3 (revision v0.2)").id == "esp32s3"
          and chips.known("esp32c3"))

    print("\neach drawing against the chip's rules")
    for b in boards:
        d = drawn(b)
        gpios = [n for _, n in d if n is not None]
        flash = FLASH_PADS.get(b.id, set())
        skipped = NOT_DRAWN.get(b.id, set())
        check(f"{b.id}: no GPIO is drawn twice",
              len(gpios) == len(set(gpios)),
              sorted(n for n in gpios if gpios.count(n) > 1))
        check(f"{b.id}: everything drawn is usable (or a flash pad)",
              all(n in b.usable or n in flash for n in gpios),
              [n for n in gpios if n not in b.usable and n not in flash])
        check(f"{b.id}: flash pads are never assignable",
              all(n not in b.usable for n in flash))
        check(f"{b.id}: every usable pin is on the header",
              set(b.usable) - set(gpios) == skipped,
              sorted(set(b.usable) - set(gpios)))
        info = cat.describe(b)
        tiles = {p["n"]: p["label"] for p in info["pins"]}
        check(f"{b.id}: the tile and the drawing call each pin the same",
              all(tiles.get(n) == lab for lab, n in d if n in tiles),
              [(lab, tiles.get(n)) for lab, n in d
               if n in tiles and tiles[n] != lab])
        check(f"{b.id}: it is sent to the phone with both columns",
              len(info["layout"]["left"]) > 0 and len(info["layout"]["right"]) > 0)

    print("\nspot checks against Espressif's tables")
    s3 = chips.get("esp32s3-devkitc-1")
    right = [lab for lab, _ in s3.drawing()["right"]]
    check("S3: J3 starts GND, TX, RX, then pins 1 and 2",
          right[:5] == ["GND", "TX", "RX", "IO1", "IO2"], right[:5])
    check("S3: GPIO33/34 are not brought out, 35-37 are",
          33 not in s3.usable and 34 not in s3.usable
          and {35, 36, 37} <= set(s3.usable))
    c3 = chips.get("esp32c3-devkitm-1")
    check("C3: TX is GPIO21 and RX GPIO20, USB is 18/19",
          c3.pin_label(21) == "TX" and c3.pin_label(20) == "RX"
          and c3.usb == frozenset({18, 19}))
    c6 = chips.get("esp32c6-devkitc-1")
    labs = [lab for lab, _ in c6.drawing()["left"]]
    check("C6: GPIO14 does not exist on this kit; NC pads are drawn",
          14 not in c6.usable and 10 in c6.usable and labs[-1] == "NC")
    c2 = chips.get("esp32c2-devkitm-1")
    check("C2: the three LED pins say so, and that v1.0 differs",
          all(n in c2.onboard for n in (0, 1, 8))
          and "v1.0" in c2.onboard[0] and "v1.0" in c2.onboard[8])
    check("C2: no guess is made about the BOOT button's pin",
          "BOOT" not in " ".join(c2.onboard.values()))
    s2 = chips.get("esp32s2-devkitc-1")
    check("S2: GPIO33 and 34 are brought out, 46 is input-only",
          {33, 34} <= set(s2.usable) and 46 in s2.input_only)
    v4 = chips.get("esp32-devkitc-38")
    check("DevKitC V4: VP/VN, and D2/D3 are GPIO9/10",
          v4.pin_label(36) == "VP" and v4.pin_label(9) == "D2"
          and v4.pin_label(10) == "D3")
    check("DevKitC V4: the flash pads are drawn as inert pads",
          [lab for lab, n in v4.drawing()["left"] if lab == "CMD"] == ["CMD"]
          and 11 not in v4.usable)

    print("\nwhat the board has on a pin is said on that pin")
    pin8 = [p for p in cat.describe(c3)["pins"] if p["n"] == 8][0]
    check("C3 GPIO8 says it is also the on-board RGB LED",
          "RGB LED" in pin8["caution"] and pin8["severity"] != "ok", pin8)
    pin9 = [p for p in cat.describe(c3)["pins"] if p["n"] == 9][0]
    check("C3 GPIO9 keeps its strapping warning and adds the BOOT button",
          "starts" in pin9["caution"] and "BOOT button" in pin9["caution"],
          pin9)
    plain = [p for p in cat.describe(c3)["pins"] if p["n"] == 5][0]
    check("a pin with nothing on it has no board sentence",
          "also wired" not in plain["caution"])

    print(f"\n{PASSED} passed, {FAILED} failed")
    sys.exit(1 if FAILED else 0)


if __name__ == "__main__":
    main()

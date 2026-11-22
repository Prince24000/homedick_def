"""Board profiles: what each kind of ESP32 board is, as DATA.

The hub used to know exactly one board -- the classic ESP32 DevKit -- as a
set of module-level constants. Every other chip was refused at the cable,
because a template is position-for-pin and the classic board's rules
(which pins are flash, which to ground when idle) land on the wrong pins of
any other chip. This module turns those rules into a profile per board, so
a new board is a new profile (built in here, or a JSON file in boards/),
not new code.

TWO LAYERS
  Family  -- the chip's own facts, physics: which GPIOs exist, which are
             the flash bus, which are USB, ADC1, touch, strapping, the
             console, Tasmota's template order. Written once per family,
             from Tasmota's own tasmota_template.h (MAX_USER_PINS, the
             kModules default templates and their per-pin comments) and
             Espressif's datasheets.
  Board   -- one family plus what the board around it adds: which pins
             reach the header, what the silkscreen calls them, the drawing
             for the phone, whether the module's PSRAM is known present.
             Every family has a GENERIC board, so any board of that family
             works on day one; a named board only adds a better drawing
             and firmer facts.

This file imports nothing from the project, on purpose: build_firmware.py
runs as a stand-alone script and needs the same facts.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Dict, List, Optional, Tuple

#: Pin roles (the same strings esphome_catalog uses).
OUT, IN, ADC, TOUCH, BIDIR = "out", "in", "adc", "touch", "bidir"

Layout = Dict[str, List[Tuple[str, Optional[int]]]]


@dataclass(frozen=True)
class Board:
    """One kind of board. Frozen: profiles are shared by every plan."""
    id: str
    label: str
    family: str                     # "esp32", "esp32s3", ...
    family_label: str               # "ESP32 (classic)", "ESP32-S3", ...
    #: Tasmota's names for this family's build and images.
    pio_env: str                    # tasmota32c3
    firmware: str                   # tasmota32c3.factory.bin (cable, at 0x0)
    ota_name: str                   # tasmota32c3.bin (over the air)
    #: The GPIO behind each position of Tasmota's Template "GPIO" array
    #: (length = the family's MAX_USER_PINS). Position means pin; this is
    #: the one table where a mistake configures a different pin silently.
    template_order: Tuple[int, ...]
    flash: frozenset                # the flash bus: never assignable
    absent: frozenset               # not on this chip/module: never assignable
    input_only: frozenset = frozenset()
    strapping: frozenset = frozenset()
    console: frozenset = frozenset()     # UART0: boot log + cable flashing
    usb: frozenset = frozenset()         # native USB D-/D+
    qio_flash: frozenset = frozenset()   # flash on Quad-I/O modules only
    maybe_absent: frozenset = frozenset()  # package-dependent pins
    #: Pins the module's own PSRAM (or a PICO's flash) may use. Whether it
    #: DOES is per board: BoardPlan.owns_psram_pins, learned at the first
    #: flash; `psram_known` is what the profile itself already knows.
    psram: frozenset = frozenset()
    psram_known: Optional[bool] = None
    adc: frozenset = frozenset()          # ADC1 only: ADC2 dies with Wi-Fi
    touch: frozenset = frozenset()
    #: Idle pins the template drives low (Output Lo) -- the classic board's
    #: cure for LEDs glowing on floating pins. EMPTY on generic profiles:
    #: many S3/C3 boards wire an LED or button to a pin and grounding it
    #: would light it for good. A named board may list its own.
    ground_idle: frozenset = frozenset()
    silkscreen: Dict[int, str] = field(default_factory=dict, hash=False)
    label_prefix: str = "GPIO"      # what an unlisted pin is printed as
    layout: Optional[Layout] = field(default=None, hash=False)
    #: What the BOARD itself has wired to a pin: {8: "the on-board RGB LED"}.
    #: Said on that pin's card, because whatever you attach there shares it.
    onboard: Dict[int, str] = field(default_factory=dict, hash=False)
    #: esptool chip-name test: the names this family answers to.
    chip_pattern: str = ""
    note: str = ""

    # -- derived -------------------------------------------------------
    @property
    def usable(self) -> Tuple[int, ...]:
        """Every pin an assignment can reach, ascending."""
        return tuple(sorted(set(self.template_order) - self.flash - self.absent))

    def role_pins(self, role: str) -> frozenset:
        usable = frozenset(self.usable)
        return {
            OUT: usable - self.input_only,
            IN: usable,
            ADC: usable & self.adc,
            TOUCH: usable & self.touch,
            BIDIR: usable - self.input_only,
        }[role]

    def pin_label(self, n: int) -> str:
        return self.silkscreen.get(n, f"{self.label_prefix}{n}")

    def matches_chip(self, chip_name: str) -> bool:
        return bool(self.chip_pattern) and bool(
            re.match(self.chip_pattern, chip_name.strip().upper()))

    def drawing(self) -> Layout:
        """The board as the phone draws it: the profile's own layout, or two
        columns of every usable pin for a generic board."""
        if self.layout:
            return self.layout
        pins = list(self.usable)
        half = (len(pins) + 1) // 2
        return {"left": [(self.pin_label(n), n) for n in pins[:half]],
                "right": [(self.pin_label(n), n) for n in pins[half:]]}

    def as_meta(self) -> Dict[str, object]:
        return {"id": self.id, "label": self.label, "family": self.family,
                "family_label": self.family_label, "note": self.note}


def _r(a: int, b: int) -> frozenset:
    return frozenset(range(a, b + 1))


# ---------------------------------------------------------------------------
# The families (generic boards). Sources: tasmota_template.h -- MAX_USER_PINS
# and the kModules default template per CONFIG_IDF_TARGET (which positions
# exist, which are zeroed as flash), PINS_WEMOS; Espressif datasheets for
# ADC1 / touch / strapping / USB.
# ---------------------------------------------------------------------------

#: The classic ESP32 DevKit V1 (30 pins) -- the board this hub was built on.
#: These values ARE the hub's historical constants (esphome_catalog re-exports
#: them), unchanged.
ESP32_DEVKIT = Board(
    id="esp32-devkit-30", label="ESP32 DevKit V1 (30 pins)",
    family="esp32", family_label="ESP32 (classic)",
    pio_env="tasmota32", firmware="tasmota32.factory.bin",
    ota_name="tasmota32.bin",
    template_order=(0, 1, 2, 3, 4, 5, 9, 10, 12, 13, 14, 15, 16, 17, 18, 19,
                    20, 21, 22, 23, 24, 25, 26, 27, 6, 7, 8, 11, 32, 33, 34,
                    35, 36, 37, 38, 39),
    flash=frozenset({6, 7, 8, 11}),
    absent=frozenset({20, 24, 37, 38}),
    input_only=frozenset({34, 35, 36, 37, 38, 39}),
    strapping=frozenset({0, 2, 5, 12, 15}),
    console=frozenset({1, 3}),
    qio_flash=frozenset({9, 10}),
    psram=frozenset({16, 17}),
    adc=frozenset({32, 33, 34, 35, 36, 39}),
    touch=frozenset({0, 2, 4, 12, 13, 14, 15, 27, 32, 33}),
    # usable - input-only - console - maybe-flash; 16/17 are spared at
    # template time while the module's PSRAM might own them.
    ground_idle=frozenset({0, 2, 4, 5, 12, 13, 14, 15, 16, 17, 18, 19, 21,
                           22, 23, 25, 26, 27, 32, 33}),
    silkscreen={36: "VP", 39: "VN", 1: "TX0", 3: "RX0", 17: "TX2", 16: "RX2"},
    label_prefix="D",
    layout={
        "left": [("EN", None), ("VP", 36), ("VN", 39), ("D34", 34), ("D35", 35),
                 ("D32", 32), ("D33", 33), ("D25", 25), ("D26", 26), ("D27", 27),
                 ("D14", 14), ("D12", 12), ("D13", 13), ("GND", None), ("VIN", None)],
        "right": [("D23", 23), ("D22", 22), ("TX0", 1), ("RX0", 3), ("D21", 21),
                  ("D19", 19), ("D18", 18), ("D5", 5), ("TX2", 17), ("RX2", 16),
                  ("D4", 4), ("D2", 2), ("D15", 15), ("GND", None), ("3V3", None)],
    },
    # Any classic die: ESP32-D0WD(-V3), D0WDQ6, D2WD, S0WD, U4WDH, PICO-*
    # -- and esptool 2.x's hyphenless spelling (ESP32D0WDQ6).
    chip_pattern=r"ESP32(-?(D0|D2|S0|U4)|-PICO|$|\s)",
)

ESP32_GENERIC = replace(
    ESP32_DEVKIT, id="esp32", label="Any classic ESP32 board",
    layout=None, label_prefix="GPIO", silkscreen={1: "TX0", 3: "RX0"},
    note="Every pin the chip has, as a list. Pick a named board for a "
         "drawing of yours.")

ESP32_S3 = Board(
    id="esp32s3", label="Any ESP32-S3 board", family="esp32s3",
    family_label="ESP32-S3", pio_env="tasmota32s3",
    firmware="tasmota32s3.factory.bin", ota_name="tasmota32s3.bin",
    # MAX_USER_PINS 38: GPIO 0-21, then 33-48 (22-25 do not exist, 26-32
    # are the flash bus and are not in the template at all).
    template_order=tuple(range(0, 22)) + tuple(range(33, 49)),
    flash=_r(26, 32), absent=_r(22, 25),
    strapping=frozenset({0, 3, 45, 46}),
    console=frozenset({43, 44}), usb=frozenset({19, 20}),
    # Octal PSRAM/flash (N*R8, R16, N32...) sits on 33-37: Tasmota's own
    # default template leaves them unset. Unknown is treated as taken.
    psram=_r(33, 37),
    adc=_r(1, 10), touch=_r(1, 14),
    chip_pattern=r"ESP32-S3",
    note="GPIO 33-37 are refused until the hub knows the module has no "
         "octal PSRAM (an N8 or N4 module).")

ESP32_S2 = Board(
    id="esp32s2", label="Any ESP32-S2 board", family="esp32s2",
    family_label="ESP32-S2", pio_env="tasmota32s2",
    firmware="tasmota32s2.factory.bin", ota_name="tasmota32s2.bin",
    # MAX_USER_PINS 36: GPIO 0-21, then 33-46.
    template_order=tuple(range(0, 22)) + tuple(range(33, 47)),
    flash=_r(26, 32), absent=_r(22, 25),
    input_only=frozenset({46}),
    strapping=frozenset({0, 45, 46}),
    console=frozenset({43, 44}), usb=frozenset({19, 20}),
    adc=_r(1, 10), touch=_r(1, 14),
    chip_pattern=r"ESP32-S2")

ESP32_C3 = Board(
    id="esp32c3", label="Any ESP32-C3 board", family="esp32c3",
    family_label="ESP32-C3", pio_env="tasmota32c3",
    firmware="tasmota32c3.factory.bin", ota_name="tasmota32c3.bin",
    # MAX_USER_PINS 22: GPIO 0-21; 11-17 are the flash supply and bus.
    template_order=tuple(range(0, 22)),
    flash=_r(11, 17), absent=frozenset(),
    strapping=frozenset({2, 8, 9}),
    console=frozenset({20, 21}), usb=frozenset({18, 19}),
    adc=_r(0, 4),
    chip_pattern=r"ESP32-C3",
    note="Many C3 boards wire an LED to GPIO 8 and the BOOT button to "
         "GPIO 9.")

ESP32_C6 = Board(
    id="esp32c6", label="Any ESP32-C6 board", family="esp32c6",
    family_label="ESP32-C6", pio_env="tasmota32c6",
    firmware="tasmota32c6.factory.bin", ota_name="tasmota32c6.bin",
    # MAX_USER_PINS 31: GPIO 0-30; 24-30 are the flash bus.
    template_order=tuple(range(0, 31)),
    flash=_r(24, 30), absent=frozenset(),
    strapping=frozenset({4, 5, 8, 9, 15}),
    console=frozenset({16, 17}), usb=frozenset({12, 13}),
    maybe_absent=frozenset({10, 11, 14}),
    adc=_r(0, 6),
    chip_pattern=r"ESP32-C6",
    note="GPIO 10 and 11 exist only on the larger (QFN40) chip, GPIO 14 "
         "only on the smaller (QFN32) one.")

ESP32_C2 = Board(
    id="esp32c2", label="Any ESP32-C2 board", family="esp32c2",
    family_label="ESP32-C2", pio_env="tasmota32c2",
    firmware="tasmota32c2.factory.bin", ota_name="tasmota32c2.bin",
    # MAX_USER_PINS 21: GPIO 0-20; 11-17 are the flash supply and bus.
    template_order=tuple(range(0, 21)),
    flash=_r(11, 17), absent=frozenset(),
    strapping=frozenset({8, 9}),
    console=frozenset({19, 20}),
    adc=_r(0, 4),
    chip_pattern=r"ESP32-C2",
    note="The C2 has little memory: the full house driver set may not fit "
         "its firmware.")

#: The family bases, by family id -- a board file names one of these.
FAMILIES: Dict[str, Board] = {
    b.family: b for b in (ESP32_GENERIC, ESP32_S3, ESP32_S2, ESP32_C3,
                          ESP32_C6, ESP32_C2)}

#: What a board record with no "board" field is: every board made before
#: profiles existed was this one.
DEFAULT = ESP32_DEVKIT



# ---------------------------------------------------------------------------
# THE NAMED BOARDS (her ruling, 2026-10-01): every board is drawn the way the
# real one is, pin for pin in the order the headers carry them, so what the
# phone shows is what is in the hand. The "Any ... board" profiles above are
# no longer OFFERED -- a plain list cannot tell a person which pin is which on
# THEIR board -- but they stay: they are the family's facts (the base every
# named board is made from), what a cable's chip name resolves to, and what
# a record saved before this existed still says.
#
# Source for every drawing below: Espressif's own user guides in the
# esp-dev-kits documentation (docs.espressif.com/projects/esp-dev-kits),
# header tables J1/J2/J3, read 2026-10-01. Top to bottom as the table lists.
# An int is a GPIO (labelled by the board's silkscreen); a string is a pad
# that is not for wiring (power, reset, NC); a (label, n) pair is a pad that
# has a GPIO number the CHIP spends on flash -- drawn, never assignable.
# ---------------------------------------------------------------------------

def _named(base: "Board", *, id: str, label: str, left, right,
           silkscreen: Optional[Dict[int, str]] = None,
           label_prefix: str = "IO", absent: frozenset = frozenset(),
           maybe_absent: Optional[frozenset] = None,
           onboard: Optional[Dict[int, str]] = None,
           note: str = "") -> "Board":
    silk = dict(silkscreen or {})

    def side(items) -> List[Tuple[str, Optional[int]]]:
        out: List[Tuple[str, Optional[int]]] = []
        for item in items:
            if isinstance(item, tuple):
                out.append((str(item[0]), int(item[1])))
            elif isinstance(item, int):
                out.append((silk.get(item, f"{label_prefix}{item}"), item))
            else:
                out.append((str(item), None))
        return out
    return replace(
        base, id=id, label=label, silkscreen=silk, label_prefix=label_prefix,
        absent=base.absent | absent,
        maybe_absent=(base.maybe_absent if maybe_absent is None
                      else maybe_absent),
        layout={"left": side(left), "right": side(right)},
        onboard=dict(onboard or {}), note=note)


#: ESP32-DevKitC V4 (38 pins, ESP32-WROOM-32E) -- Espressif's own classic
#: board. J2 left, J3 right. GPIO6/7/8/11 are brought out as CLK/D0/D1/CMD
#: but are the module's flash: drawn, not for wiring.
ESP32_DEVKITC = _named(
    ESP32_DEVKIT, id="esp32-devkitc-38", label="ESP32-DevKitC V4 (38 pins)",
    silkscreen={36: "VP", 39: "VN", 1: "TX", 3: "RX", 9: "D2", 10: "D3"},
    left=["3V3", "EN", 36, 39, 34, 35, 32, 33, 25, 26, 27, 14, 12, "GND",
          13, 9, 10, ("CMD", 11), "5V"],
    right=["GND", 23, 22, 1, 3, 21, "GND", 19, 18, 5, 17, 16, 4, 0, 2, 15,
           ("D1", 8), ("D0", 7), ("CLK", 6)],
    onboard={0: "the BOOT button"})

#: ESP32-S3-DevKitC-1 (v1.1; v1.0 drew the LED on GPIO48). J1 left, J3 right.
#: GPIO33/34 are not brought out.
ESP32_S3_DEVKITC = _named(
    ESP32_S3, id="esp32s3-devkitc-1", label="ESP32-S3-DevKitC-1",
    silkscreen={43: "TX", 44: "RX"}, absent=frozenset({33, 34}),
    left=["3V3", "3V3", "RST", 4, 5, 6, 7, 15, 16, 17, 18, 8, 3, 46, 9, 10,
          11, 12, 13, 14, "5V", "GND"],
    right=["GND", 43, 44, 1, 2, 42, 41, 40, 39, 38, 37, 36, 35, 0, 45, 48,
           47, 21, 20, 19, "GND", "GND"],
    onboard={38: "the on-board RGB LED (v1.1 boards; v1.0 uses GPIO48)",
             48: "the on-board RGB LED on v1.0 boards",
             0: "the BOOT button"},
    note="GPIO35-37 are the module's own on octal-PSRAM versions (N8R8, "
         "N16R8...); the hub refuses them until it knows yours has none.")

#: ESP32-S2-DevKitC-1. Same header shape as the S3 kit; GPIO33/34 are brought
#: out, GPIO48 does not exist.
ESP32_S2_DEVKITC = _named(
    ESP32_S2, id="esp32s2-devkitc-1", label="ESP32-S2-DevKitC-1",
    silkscreen={43: "TX", 44: "RX"},
    left=["3V3", "3V3", "RST", 4, 5, 6, 7, 15, 16, 17, 18, 8, 3, 46, 9, 10,
          11, 12, 13, 14, "5V", "GND"],
    right=["GND", 43, 44, 1, 2, 42, 41, 40, 39, 38, 37, 36, 35, 0, 45, 34,
           33, 21, 20, 19, "GND", "GND"],
    onboard={18: "the on-board RGB LED", 0: "the BOOT button"})

#: ESP32-C3-DevKitM-1. J1 left, J3 right; GND pads between the signals.
ESP32_C3_DEVKITM = _named(
    ESP32_C3, id="esp32c3-devkitm-1", label="ESP32-C3-DevKitM-1",
    silkscreen={21: "TX", 20: "RX"},
    left=["GND", "3V3", "3V3", 2, 3, "GND", "RST", "GND", 0, 1, 10, "GND",
          "5V", "5V", "GND"],
    right=["GND", 21, 20, "GND", 9, 8, "GND", 7, 6, 5, 4, "GND", 18, 19,
           "GND"],
    onboard={8: "the on-board RGB LED", 9: "the BOOT button"})

#: ESP32-C6-DevKitC-1 (the QFN40 module: GPIO10/11 present, GPIO14 not).
ESP32_C6_DEVKITC = _named(
    ESP32_C6, id="esp32c6-devkitc-1", label="ESP32-C6-DevKitC-1",
    silkscreen={16: "TX", 17: "RX"}, absent=frozenset({14}),
    maybe_absent=frozenset(),
    left=["3V3", "RST", 4, 5, 6, 7, 0, 1, 8, 10, 11, 2, 3, "5V", "GND",
          "NC"],
    right=["GND", 16, 17, 15, 23, 22, 21, 20, 19, 18, 9, "GND", 13, 12,
           "GND", "NC"],
    onboard={8: "the on-board RGB LED", 9: "the BOOT button"})

#: ESP8684-DevKitM-1 (ESP32-C2): J1 left, J3 right.
ESP32_C2_DEVKITM = _named(
    ESP32_C2, id="esp32c2-devkitm-1", label="ESP8684-DevKitM-1 (ESP32-C2)",
    silkscreen={20: "TX", 19: "RX"},
    left=["GND", "3V3", "3V3", 2, 3, "GND", "RST", "GND", 0, 1, 10, "GND",
          "5V", "5V", "GND"],
    right=["GND", 20, 19, "GND", 9, 8, "GND", 7, 6, 5, 4, "GND", 18, "GND",
           "GND"],
    # v1.1 has ONE RGB LED with a pin per colour (GPIO0 red, GPIO1 green,
    # GPIO8 blue); v1.0 had an addressable LED on GPIO8 alone, so there
    # GPIO0 and GPIO1 are free. Espressif's user guide, read 2026-10-01.
    onboard={0: "the on-board RGB LED, red (v1.1 boards; free on v1.0)",
             1: "the on-board RGB LED, green (v1.1 boards; free on v1.0)",
             8: "the on-board RGB LED (blue on v1.1; the addressable one "
                "on v1.0)"},
    note="The C2 has little memory: the full house driver set may not fit "
         "its firmware. Pins 8 and 9 are strapping pins.")

_BUILT_IN: Tuple[Board, ...] = (
    ESP32_DEVKIT, ESP32_DEVKITC, ESP32_GENERIC,
    ESP32_S3, ESP32_S3_DEVKITC, ESP32_S2, ESP32_S2_DEVKITC,
    ESP32_C3, ESP32_C3_DEVKITM, ESP32_C6, ESP32_C6_DEVKITC,
    ESP32_C2, ESP32_C2_DEVKITM)
_BOARDS: Dict[str, Board] = {b.id: b for b in _BUILT_IN}


# ---------------------------------------------------------------------------
# Board files: boards/<id>.json -- a family plus what the board adds.
# ---------------------------------------------------------------------------

def board_from_json(raw: Dict[str, object]) -> Board:
    """One board file into a Board. Raises ValueError naming the problem.

    {"id": "s3-devkitc1-n16r8", "label": "ESP32-S3 DevKitC-1 (N16R8)",
     "family": "esp32s3", "psram": true,
     "absent": [0], "silkscreen": {"43": "TX", "44": "RX"},
     "layout": {"left": [["3V3", null], ["4", 4], ...], "right": [...]},
     "ground_idle": [], "note": "..."}
    Only the family's facts can be narrowed (absent, psram), never widened:
    a board file cannot make a flash pin assignable."""
    base = FAMILIES.get(str(raw.get("family", "")))
    if base is None:
        raise ValueError(f"family must be one of {sorted(FAMILIES)}")
    board_id = str(raw.get("id", "")).strip()
    if not re.match(r"^[a-z0-9][a-z0-9._-]{1,48}$", board_id):
        raise ValueError("id must be lower-case letters, digits, . _ -")

    def pins(key: str) -> frozenset:
        value = raw.get(key) or []
        if not isinstance(value, list) or not all(
                isinstance(n, int) and not isinstance(n, bool) for n in value):
            raise ValueError(f"{key} must be a list of GPIO numbers")
        return frozenset(value)
    absent = pins("absent")
    ground = pins("ground_idle")
    if ground - frozenset(base.usable):
        raise ValueError("ground_idle names pins this chip cannot use")
    psram = raw.get("psram")
    if psram is not None and not isinstance(psram, bool):
        raise ValueError("psram must be true, false or absent")
    silk = {}
    for key, value in (raw.get("silkscreen") or {}).items():
        silk[int(key)] = str(value)
    layout = raw.get("layout")
    drawn: Optional[Layout] = None
    if layout is not None:
        if not isinstance(layout, dict):
            raise ValueError("layout must map a side to a list of pins")
        drawn = {}
        for side, column in layout.items():
            drawn[str(side)] = [
                (str(label), None if n is None else int(n))
                for label, n in column]
    return replace(
        base, id=board_id, label=str(raw.get("label") or board_id),
        absent=base.absent | absent,
        psram_known=psram if psram is not None else base.psram_known,
        silkscreen={**base.silkscreen, **silk},
        label_prefix=str(raw.get("label_prefix", base.label_prefix)),
        layout=drawn, ground_idle=ground,
        onboard={int(k): str(v)
                 for k, v in (raw.get("onboard") or {}).items()},
        note=str(raw.get("note", base.note)))


def load_board_files(folder: Optional[Path]) -> List[str]:
    """Merge boards/*.json into the profile list. One sentence per file,
    problems and confirmations alike. Built-in ids cannot be replaced."""
    notes: List[str] = []
    if folder is None or not Path(folder).is_dir():
        return notes
    for path in sorted(Path(folder).glob("*.json")):
        try:
            board = board_from_json(json.loads(path.read_text("utf-8")))
        except (OSError, ValueError, TypeError, KeyError) as exc:
            notes.append(f"{path.name}: not loaded ({exc})")
            continue
        if board.id in {b.id for b in _BUILT_IN}:
            notes.append(f"{path.name}: '{board.id}' is a built-in board; "
                         f"give it another id")
            continue
        _BOARDS[board.id] = board
        notes.append(f"{path.name}: {board.label} ({board.family_label})")
    return notes


def get(board_id: Optional[str]) -> Board:
    """The profile for a board record's "board" field; the classic DevKit
    when the record predates profiles (or names one no longer present)."""
    return _BOARDS.get(board_id or "", DEFAULT)


def known(board_id: str) -> bool:
    return board_id in _BOARDS


def all_boards() -> List[Board]:
    """Named boards first within each family, generic last."""
    order = list(FAMILIES)
    return sorted(_BOARDS.values(),
                  key=lambda b: (order.index(b.family) if b.family in order
                                 else 99, b.id == b.family, b.label))


def is_generic(board: Board) -> bool:
    """The family's own base profile ("Any ... board"): a plain list of the
    chip's pins, which says nothing about WHICH pin is which on a real
    board. Kept for what it knows, never offered."""
    return board.id == board.family


def pickable_boards() -> List[Board]:
    """Every board a person can CHOOSE: the named ones, drawn as built."""
    return [b for b in all_boards() if not is_generic(b)]


def family_of_chip(chip_name: str) -> Optional[Board]:
    """The generic board of the family an esptool chip name belongs to."""
    for board in FAMILIES.values():
        if board.matches_chip(chip_name):
            return board
    return None

"""What each pin can be, and how to say it to Tasmota.

THE FILENAME IS HISTORICAL. This module has not described ESPHome since the
boards moved to prebuilt Tasmota firmware; the name was kept so that every
import across the project stayed put during the switch. If that ever reads
as a lie rather than as a scar, rename it -- but rename it on purpose, in
one commit, not by accident in the middle of something else.

This replaces esphome_catalog. The knowledge is the same knowledge -- which
ESP32 pins exist, which can drive a load, which can read an analog value --
but its JOB has changed completely.

Under ESPHome the pin rules decided what YAML to write, and the compiler was
a second opinion: get a pin wrong and the build failed loudly before the
board was ever touched. Tasmota has no such opinion. Its own device table
declares GPIO34 with exactly the same flags as GPIO13, and there is no
validation anywhere that stops you putting a relay on it. It will accept the
configuration, report success, and drive nothing -- because GPIO34 has no
output transistor on the die. The firmware cannot know that; only this file
does.

So the rule for this module: THE PICKER IS THE ONLY GUARD. If a combination
is wrong, it must be impossible to select here, because nothing downstream
will catch it.

The public shape is small on purpose:

    pins()                    -> every pin, with what it can and cannot do
    where_can_it_go(type_id)  -> the pins that type will accept
    BoardPlan.activation_commands()    -> Template, then Module 0 -- alone
    BoardPlan.configuration_commands() -> everything the board is told
                                          AFTER it has come back
    BoardPlan.backlogs()      -> both halves, chunked and ready to send

WHY TWO HALVES (audit, 2026-08-25). `Module 0` activates the stored
template by RESTARTING the board, and Tasmota abandons whatever is queued
behind it in the same Backlog. Bedroom's display settings were queued
exactly there: every command reported sent, DisplayModel read back 0. So
activation and configuration are separate lists with a wait between them,
and there is no single-stream method left to reach for by accident.
"""

from __future__ import annotations

import json
import re
from collections import Counter
from dataclasses import dataclass, field, replace
# `Any` was missing here, and nothing noticed for a reason worth knowing:
# `from __future__ import annotations` above turns every annotation into a
# string that is never evaluated, so Dict[str, Any] in a signature is inert
# at runtime. The tests all passed. Only the type checker looked.
#
# Iterable and Sequence went the other way -- imported and never used. Same
# blind spot, opposite symptom.
from typing import Any, Dict, List, Optional, Tuple

try:                                   # the package, or esp_firmware/ on the path
    from . import chips
except ImportError:                    # pragma: no cover
    import chips                       # type: ignore[no-redef]
Board = chips.Board

#: Bumped on every delivered change, alongside esp_builder.REVISION -- the
#: two files ship as a PAIR, and the builder refuses to start against a
#: catalogue that does not carry what it needs (a day of hand-installed
#: deliveries one file apart is where that rule comes from).
REVISION = "2026-08-25c two-phase-wiring"

# ---------------------------------------------------------------------------
# The chip
# ---------------------------------------------------------------------------
#
# Classic ESP32 on a WROOM-32 module -- a DevKitC or any of the clones. Every
# set below is that board specifically. When S3 and C3 support arrives it
# arrives as a sibling table, not as edits to these numbers: the last round of
# bugs came from one table quietly claiming to describe every chip.

#: The order Tasmota's Template JSON expects, and it is not sorted.
#:
#: 36 slots. Physical GPIO6/7/8/11 are wired to the flash chip and are parked
#: at the END of the array (slots 24-27) where they must always be 0. Slots
#: 16 and 20 are GPIO20 and GPIO24, which do not exist on this package and are
#: likewise always 0. Getting this order wrong does not raise an error -- it
#: silently configures a different pin than the one you meant, which is the
#: single most dangerous mistake this module can make. Hence the test.
TEMPLATE_ORDER: Tuple[int, ...] = (
    0, 1, 2, 3, 4, 5, 9, 10, 12, 13, 14, 15, 16, 17, 18, 19, 20, 21,
    22, 23, 24, 25, 26, 27, 6, 7, 8, 11, 32, 33, 34, 35, 36, 37, 38, 39,
)

#: Wired to the internal SPI flash. Tasmota hard-blocks 6, 7, 8 and 11.
FLASH_PINS = frozenset({6, 7, 8, 11})

#: Used for flash on a module wired for Quad I/O, free on one wired for Dual.
#: Nothing on the board says which you have. Offered, with that said plainly.
QIO_FLASH_PINS = frozenset({9, 10})

#: UART0 -- the USB serial console. Perfectly usable if you are willing to
#: give up the boot log and re-flashing over the cable, which some people
#: legitimately are. Offered, with the cost stated.
CONSOLE_PINS = frozenset({1, 3})

#: On the die but not brought out to a header on WROOM/WROVER modules, and
#: forced to zero by Tasmota's own esp32dev table -- so an assignment here
#: would not merely be unwise, it would be discarded on the way in.
UNBONDED_PINS = frozenset({37, 38})

#: No output driver. This is the set Tasmota will let you misuse.
INPUT_ONLY_PINS = frozenset({34, 35, 36, 37, 38, 39})

#: Read at boot to choose boot mode. Legal, and worth a warning: whatever you
#: attach must not hold the pin at the wrong level while the board resets.
STRAPPING_PINS = frozenset({0, 2, 5, 12, 15})

#: Ordinary pins on a WROOM. On a WROVER they are the chip-select and clock
#: of the RAM chip inside the module's can, and DRIVING EITHER ONE KILLS THE
#: RAM MID-SENTENCE -- the firmware crashes, the template that did it is
#: saved, and the board crashes again on every boot after. Tasmota does not
#: protect anyone here: its own source paints these two red in the template
#: console ("may be dangerous to change") and then applies whatever you
#: chose anyway.
#:
#: Nothing on the chip says which module it is sitting in, so this is not
#: a fact this table can know -- the builder learns it per board (esptool
#: names the chip; `Status 4` reports PsrMax) and derives ONE conclusion:
#: does the module keep these pins for itself? BoardPlan.owns_psram_pins
#: is where that conclusion lands: False means grounding and assignment
#: are open, True means the pins are the module's and are refused
#: outright, and unknown is treated as True -- guessing wrong about a
#: glow costs a dim LED, guessing wrong about the module's bus costs the
#: board.
PSRAM_PINS = frozenset({16, 17})

#: ADC1 survives Wi-Fi. ADC2 does not, and Wi-Fi is always on here, so ADC2 is
#: simply not an analog option -- not "discouraged", not available.
ADC1_PINS = frozenset({32, 33, 34, 35, 36, 39})

#: WHAT THE BOARD SAYS ON ITS OWN HEADER. Nobody at a board reads "GPIO17";
#: the silkscreen says TX2, and the group's schematic says TX2 -> RX_PZEM.
#: The pin screen used to show GPIO numbers only, so the person holding the
#: board looked for TX2 and RX2 and found "no pins for such" (2026-09-09).
#: Every pin not listed here is printed as D<n>.
SILKSCREEN: Dict[int, str] = {
    36: "VP", 39: "VN",          # ADC preamp inputs, input-only
    1: "TX0", 3: "RX0",          # UART0, the USB console
    17: "TX2", 16: "RX2",        # UART2 -- the PZEM's serial port here
}


def silkscreen(n: int, board: Optional[Board] = None) -> str:
    if board is not None:
        return board.pin_label(n)
    return SILKSCREEN.get(n, f"D{n}")


#: THE 30-PIN ESP32 DEVKIT V1, as it sits on the bench: two columns, top to
#: bottom, USB connector at the top. GPIO entries carry their number; power
#: and control pins carry None and are drawn inert, so the picture on the
#: phone has the same shape as the board in the hand and GND/VIN/3V3 are
#: where the wires actually go. This is the board in the group's
#: schematic (ESP1, pins 1-30). Pins this board does not bring out --
#: GPIO0 (the BOOT button), 9 and 10 (flash) -- are simply not drawn.
LAYOUT_30: Dict[str, List[Tuple[str, Optional[int]]]] = {
    "left": [("EN", None), ("VP", 36), ("VN", 39), ("D34", 34), ("D35", 35),
             ("D32", 32), ("D33", 33), ("D25", 25), ("D26", 26), ("D27", 27),
             ("D14", 14), ("D12", 12), ("D13", 13), ("GND", None), ("VIN", None)],
    "right": [("D23", 23), ("D22", 22), ("TX0", 1), ("RX0", 3), ("D21", 21),
              ("D19", 19), ("D18", 18), ("D5", 5), ("TX2", 17), ("RX2", 16),
              ("D4", 4), ("D2", 2), ("D15", 15), ("GND", None), ("3V3", None)],
}

#: Native capacitive touch channels T0-T9.
TOUCH_PINS = frozenset({0, 2, 4, 12, 13, 14, 15, 27, 32, 33})

#: Every pin the picker shows. EVERYTHING that physically exists and can
#: actually carry an assignment -- not a curated subset.
#:
#: The earlier version of this list withheld the console pins and the
#: maybe-flash pins on the grounds that they were unwise. That was the wrong
#: call. Withholding a pin does not teach anybody anything; it just makes the
#: board look smaller than it is and leaves someone with a real reason stuck.
#: They are all here now, each carrying what it is for and what it costs.
#:
#: The only pins missing are ones where an assignment CANNOT TAKE EFFECT:
#: 6/7/8/11 are held by the flash chip and hard-blocked by Tasmota, 20 and 24
#: do not exist on this package, and 37/38 are forced to zero by Tasmota's
#: own device table. Offering those would not be flexibility, it would be a
#: control that does nothing.
USABLE_PINS: Tuple[int, ...] = tuple(sorted(
    set(TEMPLATE_ORDER) - FLASH_PINS - UNBONDED_PINS - {20, 24}
))

#: Pins the template DRIVES TO GROUND when nothing is assigned to them.
#:
#: An unassigned pin is not "off". It is an input nobody reads, floating
#: wherever leakage current and the Wi-Fi radio's coupling push it -- and
#: that is enough to dimly light an LED left wired from an earlier
#: configuration. The first bedroom board glowed on 16 and 17 for exactly
#: this reason. Fixing it on the board's own web page was not a fix: the
#: hub re-sends its template on every edit, and a template with those slots
#: at 0 puts the pins straight back to floating. So the template itself is
#: what grounds them -- every send carries the cure, not the relapse.
#:
#: What is deliberately NOT in this set:
#:
#:   6/7/8/11, 20/24, 37/38 -- flash, non-existent, unbonded. Already
#:     outside USABLE_PINS; Tasmota discards assignments there anyway.
#:   34/35/36/39 -- input-only: no output driver on the die, so there is
#:     nothing to drive them with. They can still float; they cannot be
#:     helped. Nothing should be left wired to them without a job.
#:   1/3 (TX0/RX0) -- the serial console, which is how a board is
#:     provisioned. Grounding TX0 kills the boot log and the next install.
#:     (An LED wired to TX0 will glow regardless: a UART idles HIGH by
#:     design. That is not floating -- that is the console holding its
#:     line, and it stops only when the wire comes off.)
#:   9/10 -- flash pins on a module wired for Quad I/O, free on one wired
#:     for Dual, and nothing on the board says which this is. Driving them
#:     on a QIO module crashes the firmware mid-boot. A floating pin is
#:     annoying; a board that cannot boot is worse.
#:
#: The strapping pins (0/2/5/12/15) ARE grounded. Strapping is sampled
#: while the chip is held in reset, when every GPIO is tri-stated and no
#: firmware configuration exists yet -- an Output Lo applied after boot
#: cannot reach back in time to that moment. And for GPIO12 specifically,
#: low is the safe direction: high at reset selects 1.8 V flash and the
#: board never comes up.
#:
#: 16 and 17 are members of this set but conditionally SPARED at template
#: time: they are only grounded on a board whose record says PSRAM is
#: absent. See PSRAM_PINS -- that is a per-board fact, not a per-chip one,
#: so it cannot live in a frozenset.
GROUNDED_WHEN_UNUSED = (
    frozenset(USABLE_PINS) - INPUT_ONLY_PINS - CONSOLE_PINS - QIO_FLASH_PINS
)


# ---------------------------------------------------------------------------
# What a pin is asked to do
# ---------------------------------------------------------------------------

OUT = "out"          # must be able to drive a load
IN = "in"            # digital read; any pin
ADC = "adc"          # analog read; ADC1 only
TOUCH = "touch"      # native touch channel
BIDIR = "bidir"      # one-wire style: driven and read on the same pin

#: Which pins satisfy each role. One place, so a new role cannot be added
#: without deciding this.
_ROLE_PINS: Dict[str, frozenset] = {
    OUT: frozenset(USABLE_PINS) - INPUT_ONLY_PINS,
    IN: frozenset(USABLE_PINS),
    ADC: frozenset(USABLE_PINS) & ADC1_PINS,
    TOUCH: frozenset(USABLE_PINS) & TOUCH_PINS,
    BIDIR: frozenset(USABLE_PINS) - INPUT_ONLY_PINS,
}


# ---------------------------------------------------------------------------
# Tasmota component codes
# ---------------------------------------------------------------------------
#
# From the Tasmota32 column of Components.md -- NOT the ESP8266 column, which
# has different numbers for the same names. A wrong number here does not
# fail; it configures the wrong peripheral. Treat these as checksums.
#
# Families that support several instances are spaced 32 apart, and instance n
# is base + (n - 1). Relay1..Relay32 are 224..255, then Relay_i1 begins at
# 256 -- so `slots` is 32 for those, and exceeding it would silently spill
# into the inverted family.

@dataclass(frozen=True)
class Component:
    name: str            # exactly as Tasmota prints it
    code: int            # base code, instance 1
    slots: int = 1       # how many instances share this family

    def at(self, instance: int) -> int:
        if not 1 <= instance <= self.slots:
            raise ValueError(
                f"{self.name} supports {self.slots} instance(s) on one board; "
                f"asked for number {instance}.")
        return self.code + instance - 1


C_RELAY = Component("Relay", 224, 32)
C_BUTTON = Component("Button", 32, 32)
C_SWITCH = Component("Switch", 160, 32)
C_PWM = Component("PWM", 416, 16)          # MAX_PWMS: 16 LEDC channels
C_WS2812 = Component("WS2812", 1376)
C_BUZZER = Component("Buzzer", 480)
C_IR_SEND = Component("IRsend", 1056)
C_IR_RECV = Component("IRrecv", 1088)
C_RF_SEND = Component("RFSend", 1120)
C_RF_RECV = Component("RFrecv", 1152)
C_ROTARY_A = Component("Rotary A", 3264, 2)  # MAX_ROTARIES is 2
C_ROTARY_B = Component("Rotary B", 3296, 2)
C_TOUCH_BTN = Component("Button_tc", 5408, 4)    # four touch buttons, no more
C_ADC_RAW = Component("ADC Input", 4704, 8)
C_ADC_LIGHT = Component("ADC Light", 4768, 8)
C_ADC_MQ = Component("ADC MQ", 8544, 8)
C_ADC_RANGE = Component("ADC Range", 4864, 8)
C_DHT11 = Component("DHT11", 1184, 4)           # DHT_MAX_SENSORS is 4
C_DHT22 = Component("AM2301", 1216, 4)           # covers DHT22/AM2301/AM2302
C_DS18X20 = Component("DS18x20", 1312)
C_SR04_TRIG = Component("SR04 Tri/Tx", 1856)
C_SR04_ECHO = Component("SR04 Ech/Rx", 1888)
#: Codes read off the 2026-08-24 build's own short `GPIOs` answer -- the
#: house method: the board is the referee for what its firmware carries,
#: and these four were in bedroom's list, so the drivers are in EVERY
#: image this hub flashes (one image, identical bytes).
C_ADC_JOY = Component("ADC Joystick", 3328, 8)
C_595_SRCLK = Component("74x595 SRCLK", 8288)
C_595_RCLK = Component("74x595 RCLK", 8320)
C_595_OE = Component("74x595 OE", 8352)
C_595_SER = Component("74x595 SER", 8384)
C_I2C_SDA = Component("I2C SDA1", 640)
C_I2C_SCL = Component("I2C SCL1", 608)
#: HARDWARE SPI, shared by every SPI device on the board, marked shared
#: exactly like I2C. The firmware initialises the bus the moment CLK, MOSI
#: and MISO are assigned ("SPI: Bus1 using GPIO14(CLK)..."); the RC522
#: below is its first rider.
C_SPI_MISO = Component("SPI MISO", 672)
C_SPI_MOSI = Component("SPI MOSI", 704)
C_SPI_CLK = Component("SPI CLK", 736)
#: RC522 -- the two pins that are the reader's alone. The codes are the ones
#: HomeVoice's OWN firmware printed in its short `GPIOs` list on 2026-08-18
#: (`"RC522 CS":6272,"RC522 Rst":6112`), which is the only list that proves
#: a driver is compiled in. Stock Tasmota prints neither: see the entry.
C_RC522_CS = Component("RC522 CS", 6272)
C_RC522_RST = Component("RC522 Rst", 6112)
C_PZEM_TX = Component("PZEM0XX Tx", 1472)
#: "PZEM016 Rx", NOT "PZEM004 Rx". The two names are two PROTOCOLS, not two
#: pin roles: PZEM004 Rx (1504) speaks the legacy v1/v2 serial protocol,
#: PZEM016 Rx (1536) speaks Modbus, which every PZEM-004T from V3.0 on uses
#: (V4.0 is the same Modbus meter on a revised board). A V3/V4 module wired
#: to a PZEM004 Rx pin is silent -- no error, no reading, forever -- because
#: the firmware is polling it in a language it does not speak. The board is
#: the referee here as everywhere: `Gpio 255` prints the NAME of what took,
#: and `Status 10` grows an ENERGY block the moment the meter answers.
C_PZEM_RX = Component("PZEM016 Rx", 1536)
#: The legacy protocol, kept as its OWN component and its own catalogue
#: entry below -- not a setting on one entry. The two generations fail
#: invisibly when crossed, so the person must say which meter is in their
#: hand at the moment they place it; a default would just decide wrongly
#: for half of them.
C_PZEM004_RX = Component("PZEM004 Rx", 1504)
#: The DC branch of the family (PZEM-017 and the shunt-based -003). Modbus
#: like V3/V4, but a DIFFERENT Tasmota driver: it asks for volts/amps out
#: of a battery or solar line instead of mains AC. Same silence rule --
#: a DC meter on an AC component reads nothing, forever.
C_PZEM017_RX = Component("PZEM017 Rx", 1568)
C_PN532_TX = Component("PN532 Tx", 2848)
C_PN532_RX = Component("PN532 Rx", 2880)

#: "Fixed output state" -- a pin held at ground, doing nothing on purpose.
#:
#: No device in the catalogue offers this; template() applies it to every
#: unassigned pin in GROUNDED_WHEN_UNUSED. The value 3872 is the same in
#: BOTH columns of Components.md and was checked against the Tasmota32
#: table alongside nine codes this file already trusts (224, 416, 608,
#: 640, 1376, 3264, 4704, 5408, 8544 -- all matched). GpioInit() in
#: Tasmota's support_tasmota.ino handles the code PER PIN -- a bare
#: `pinMode(OUTPUT); digitalWrite(0)` inside the loop over the template --
#: so the same 3872 on twenty pins is twenty grounded pins, not one
#: instance and nineteen surprises. That is why `slots` stays 1 and the
#: code is used directly, never through at().
C_OUTPUT_LO = Component("Output Lo", 3872)


# ---------------------------------------------------------------------------
# The catalogue
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class PinSlot:
    """One pin a device type needs, and what it needs of it."""
    key: str
    label: str
    role: str
    component: Component
    #: Shared buses (I2C) are claimed once per board and then reused. A slot
    #: marked shared does not ask again if the bus is already assigned.
    shared: bool = False
    #: A GPIO the pin screen puts this pin on first, when one is given.
    #: Moving it is still allowed.
    default_pin: Optional[int] = None


#: What a placed device turns into once Home Assistant sees it.
#:
#: The hub needs three things to register a device without asking: which
#: Home Assistant domain to expect it in, how it behaves (on/off, or a range
#: with a step), and whether it produces MORE readings than the one you were
#: thinking of when you placed it.
#:
#: That last field is why this exists. A BME280 is one thing you screwed to a
#: wall and three entities in Home Assistant. Registering all three turns a
#: four-device board into a ten-device list; registering none loses the
#: humidity you actually wanted. So one is primary and the rest are offered.
@dataclass(frozen=True)
class Produces:
    domain: str                    # switch / light / sensor / binary_sensor...
    binary: bool = True            # on/off, versus a range
    #: (state_key, range_min, range_max, step) when not binary.
    envelope: Tuple[str, float, float, float] = ("level", 0.0, 100.0, 10.0)
    #: Additional readings this one device also reports, in words, offered
    #: after the primary is registered.
    extras: Tuple[str, ...] = ()

    def as_meta(self) -> Dict[str, Any]:
        """The device record's numeric envelope.

        THE STATE KEY IS "on" OR "level". NOTHING ELSE, EVER.

        This looks like it throws away information -- a BME280 clearly reads
        a temperature, not a "level" -- and the first version did use the
        reading's name. That was a bug, and one this project has already had
        once: device_manager still carries a migration for records whose
        state_key was "temperature", because translate_ha_state writes EVERY
        domain's value into "level", so such a device looked up a field
        nothing populates. build_command then found no baseline and "make it
        warmer" came back as a flip, which turned the thermostat off.

        The reading's name is still real and still used -- envelope[0] is
        what matches `sensor.kitchen_bme280_temperature` to the thing you
        called "climate". It is a search term, not a storage key. The range
        says what kind of number it is; the key never needed to.
        """
        if self.binary:
            return {"step": 0.0, "range_min": 0.0, "range_max": 1.0,
                    "state_key": "on"}
        key, lo, hi, step = self.envelope
        return {"step": step, "range_min": lo, "range_max": hi,
                "state_key": "level", "unit": UNIT_OF.get(key, "")}


#: The unit a reading is measured in, by the name it declares.
#:
#: as_meta returned no unit at all, and _envline reads env['unit'] -- so a
#: DHT's card said "-10.0 to 60.0" with nothing to say what OF. The unit was
#: never in `envelope`, which is (name, min, max, step).
#:
#: DERIVED FROM THE NAME, not stored per device, because that is the fact
#: the catalogue already has: 39 sensor types declare "temperature" or
#: "power" or "distance", and none of them measures those in anything else.
#: A name not in this table simply has no unit shown, which is what every
#: reading had until now.
#:
#: FOR DISPLAY ONLY. The board's own telemetry is the truth -- Tasmota even
#: publishes TempUnit beside the value -- and nothing here is used to
#: convert anything.
UNIT_OF: Dict[str, str] = {
    "temperature": "°C",
    "ambient": "°C",
    "humidity": "%",
    "moisture": "%",
    "pressure": "hPa",
    "illuminance": "lx",
    "distance": "cm",
    "power": "W",
    "voltage": "V",
    "current": "A",
    "energy": "kWh",
    "co2": "ppm",
    "eco2": "ppm",
    "tvoc": "ppb",
    "ppm": "ppm",
    "pm2_5": "µg/m³",
    "uv_index": "",
    "magnetic_field": "µT",
    "brightness": "%",
    "position": "%",
}


SWITCHY = Produces("switch")
CONTACTY = Produces("binary_sensor")
LIGHTY = Produces("light", binary=False,
                  envelope=("brightness", 0.0, 100.0, 10.0))
COVERY = Produces("cover", binary=False,
                  envelope=("position", 0.0, 100.0, 25.0))


def _sensor(key: str, lo: float, hi: float, step: float,
            *extras: str) -> Produces:
    return Produces("sensor", binary=False, envelope=(key, lo, hi, step),
                    extras=extras)


#: Things the hub COMMANDS rather than things Home Assistant shows. An IR
#: blaster has no state to report and no entity to register; putting one in
#: the device list would give you something you can name and never query.
COMMAND_ONLY = Produces("", binary=True)

#: Things that SPEAK but produce no entity either. An IR receiver reports
#: every code it hears as a one-off telemetry message
#: (`tele/<topic>/RESULT` carrying `IrReceived`), never as a state -- there
#: is no "current infrared", the way there is a current temperature. Home
#: Assistant's Tasmota integration therefore creates nothing for it, and the
#: hub cannot register it as a device.
#:
#: Same shape as COMMAND_ONLY and deliberately a SEPARATE NAME: one is
#: "nothing to read because we only write", the other is "nothing to read
#: because what it says is an event". They arrive at the same empty domain
#: from opposite directions, and a future reader deserves to know which they
#: are looking at.
#:
#: The consequence is architectural: an event can only reach the hub if the
#: hub is LISTENING on MQTT. See homevoice-rfid-automation-design.md -- the
#: card reader has exactly this shape, and one listener serves both.
EVENT_ONLY = Produces("", binary=True)


# ---------------------------------------------------------------------------
# Direction -- the one question the picker asks
# ---------------------------------------------------------------------------
#
# WHY THIS REPLACED THE CATEGORY LIST. Assigning a pin is answering "what did
# I physically connect here", and there are only two kinds of answer: the hub
# drives it, or it tells the hub something. Everything else -- relay versus
# lamp versus pump -- is a NAME, not a wiring difference, and the old picker
# made you choose between fifteen of them before it would continue.
#
# The evidence it was wrong: a single yellow LED was described as an RGB light
# across three PWM channels, because "RGB light (three channels)" was the
# closest-looking entry under Lighting and there was no entry for a plain LED.
# Every layer then reported success and Home Assistant drew a colour picker.
#
# OTHER is not a third kind of pin. It is the escape hatch -- the full
# catalogue and the raw Tasmota functions -- and it exists so that simplifying
# the other two costs nobody anything.
DOES = "does"        # the hub drives this pin
READS = "reads"      # this pin tells the hub something
OTHER = "other"      # everything else, one tap away

DIRECTIONS: Dict[str, str] = {
    DOES: "It does something",
    READS: "It reads something",
    OTHER: "Something else",
}

#: Which roles a branch needs at least one of. Used to answer the direction
#: question FOR the pin where the pin already knows: GPIO34-39 have no output
#: transistor, so "it does something" is not a choice there, it is a mistake
#: waiting to be reported as success.
_DIRECTION_ROLES: Dict[str, Tuple[str, ...]] = {
    DOES: (OUT,),
    READS: (IN, ADC, TOUCH, BIDIR),
    OTHER: (OUT, IN, ADC, TOUCH, BIDIR),
}

#: One line under each branch, and one line when a branch is not available on
#: the pin in hand. Wording lives here so the phone and the terminal say the
#: same thing, and so neither has to work out WHY a branch is missing.
_DIRECTION_HINT: Dict[str, str] = {
    DOES: "the hub switches it on, off, or varies it",
    READS: "it tells the hub something -- a button, a sensor",
    OTHER: "the full list, or a Tasmota function by name",
}

_DIRECTION_UNAVAILABLE: Dict[str, str] = {
    DOES: "This pin has no output transistor, so nothing wired to it can be "
          "switched on. Tasmota would accept it and report success.",
    READS: "This pin cannot be read.",
    OTHER: "Nothing can go on this pin.",
}


@dataclass(frozen=True)
class DeviceType:
    id: str
    label: str
    group: str
    slots: Tuple[PinSlot, ...]
    #: Commands sent after the template is applied. `{n}` is replaced with the
    #: instance number allocated to the first slot -- SwitchMode2, PWM3 and so
    #: on -- and `{pin}` with its GPIO number.
    setup: Tuple[str, ...] = ()
    note: str = ""
    #: What Home Assistant will show for it. Defaults to a plain switch,
    #: which is what most of the Switching group is.
    produces: Produces = SWITCHY

    #: DOES / READS / OTHER. Stamped per section at the bottom of this file
    #: rather than typed on every entry, so a new device cannot be added
    #: without landing in a branch -- see _stamp().
    direction: str = OTHER

    #: Offered in the SHORT list, the one the two-question flow shows. Most
    #: types are not: they are correct, they are reachable through "Something
    #: else", and putting all thirty in front of somebody wiring an LED is
    #: what this rework exists to undo.
    primary: bool = False

    #: The short list's wording. `label` names the part ("Temperature +
    #: humidity (DHT22 / AM2301)"); this names the JOB ("Temperature and
    #: humidity"). Empty means the two are the same thing.
    plain: str = ""

    #: Names that must appear in the board's SHORT `GPIOs` list -- the one
    #: printed with no argument -- before this device is sent to it.
    #:
    #: THE ENUM/DRIVER DISTINCTION, made enforceable. `GPIOs 255` lists every
    #: component Tasmota has ever named; the template accepts any of them and
    #: `Gpio 255` echoes the assignment back, so a device whose driver was
    #: never compiled into the binary looks perfectly placed and is silent
    #: for ever. Only the short list is filtered to what THIS binary can
    #: use. Empty for the many devices every Tasmota build carries; set on
    #: the ones that need HomeVoice's own build, and the builder refuses to
    #: send them to a board whose list does not carry the name -- with a
    #: sentence saying which firmware it needs, instead of a silent reader.
    driver_names: Tuple[str, ...] = ()

    #: The name this device's telemetry block arrives under, when it is not
    #: derivable from the component.
    #:
    #: A BUILT-IN NEEDS NOTHING HERE. The hub chose its driver and wrote the
    #: template, so the component name IS the block name -- "AM2301" is put
    #: on the chip and comes back on tele/<topic>/SENSOR, and _json_key
    #: reads the first word of it.
    #:
    #: A device whose block name is not its component name says it here
    #: (empty: derived from the component, as above).
    publishes: str = ""

    @property
    def short_label(self) -> str:
        return self.plain or self.label

    @property
    def friendly_family(self) -> str:
        """Which Tasmota FriendlyName slot carries this device's name.

        Only relays and lights have FriendlyName slots -- Tasmota names
        sensors from their driver, not from a setting. So a relay's name
        travels to Home Assistant by itself, and a sensor's has to be
        applied by the hub after the entity appears. Knowing which is which
        up front is the difference between auto-registration working and
        auto-registration producing four devices called 'Kitchen'.
        """
        first = self.slots[0].component.name if self.slots else ""
        return "friendly" if first in ("Relay", "PWM") else "driver"


def _one(key: str, label: str, role: str, comp: Component) -> Tuple[PinSlot, ...]:
    return (PinSlot(key, label, role, comp),)


_I2C = (
    # BIDIR and OUT, not IN. Both lines are DRIVEN by the ESP32 -- SDA in
    # both directions, SCL always by the master -- so an input-only pin
    # (GPIO34-39, no output transistor) cannot carry either. They were
    # marked IN, which let the pin screen offer 34-39 for an I²C bus that
    # would then never clock a single bit: a dead bus reported as a
    # successful placement, which is this catalogue's cardinal sin.
    PinSlot("sda", "I²C data (SDA)", BIDIR, C_I2C_SDA, shared=True),
    PinSlot("scl", "I²C clock (SCL)", OUT, C_I2C_SCL, shared=True),
)

#: The three shared SPI lines. Claimed once and reused, exactly like I²C --
#: so a second SPI device later costs ONE pin (its chip select), not four.
_SPI = (
    PinSlot("clk", "SPI clock (SCK)", OUT, C_SPI_CLK, shared=True),
    PinSlot("mosi", "SPI data to the device (MOSI)", OUT, C_SPI_MOSI,
            shared=True),
    PinSlot("miso", "SPI data from the device (MISO)", IN, C_SPI_MISO,
            shared=True),
)

def _stamp(direction: str,
           types: Tuple[DeviceType, ...]) -> Tuple[DeviceType, ...]:
    """Put a whole section on one side of the question.

    Stated once per section instead of once per entry, so a device cannot be
    added without landing in a branch -- and so the sections in this file
    ARE the branches, rather than a second list somewhere that has to be
    kept in step with this one.
    """
    return tuple(replace(t, direction=direction) for t in types)


#: Setup for every two-relay (open / close) shutter entry -- see "servo".
_TWO_RELAY_SHUTTER = ("SetOption80 1", "Interlock {n},{n2}", "Interlock ON",
                      "ShutterRelay{sh} {n}")

#: Pins the hub DRIVES.
_DOES: Tuple[DeviceType, ...] = _stamp(DOES, (
    # ---- the short list --------------------------------------------------
    #
    # THE ENTRY THAT WAS MISSING. A bare LED is the most ordinary thing
    # anybody connects to a dev board, and there was nowhere to say it: the
    # Lighting group offered a single-channel dimmer, a three-channel RGB
    # light and an addressable strip. So a yellow LED became an RGB light
    # across GPIO26/27/32 and Home Assistant drew a colour picker for it.
    #
    # Electrically this is the same as a relay, a socket and a pump, because
    # to the chip they ARE the same: drive the pin high or low. The
    # difference between them is the name you type next, which is why they
    # are one entry now and not four.
    DeviceType("onoff", "Something that turns on and off", "Switching",
               _one("pin", "Output pin", OUT, C_RELAY),
               primary=True,
               plain="On or off — an LED, relay, socket, pump, lamp",
               note="Appears in Home Assistant as a switch, because that is "
                    "what Tasmota announces. Voice control matches on the "
                    "name, so it makes no difference to saying 'turn on the "
                    "bedroom light'."),

    # ---- things that switch on and off ---------------------------------
    DeviceType("relay", "Relay / socket / pump", "Switching",
               _one("pin", "Relay control pin", OUT, C_RELAY)),
    DeviceType("lamp", "Lamp on a relay", "Switching",
               _one("pin", "Relay control pin", OUT, C_RELAY)),
    DeviceType("fan", "Fan on a relay", "Switching",
               _one("pin", "Relay control pin", OUT, C_RELAY)),
    # ---- named things that are electrically a relay -------------------
    # The relay/lamp/fan pattern, widened on purpose (2026-08-25 sweep):
    # to the chip these are ALL "drive the pin", but each name earns its
    # row by carrying the one piece of wiring wisdom that device needs.
    # A picker that only says "relay" makes the person translate; these
    # translate for them, and the note is the payload.
    DeviceType("heater", "Heater / immersion element", "Switching",
               _one("pin", "Relay control pin", OUT, C_RELAY),
               plain="A heater switched on and off",
               note="The relay MODULE switches the heater's own circuit -- "
                    "a bare 10A board relay is not rated for a 2kW element. "
                    "Big heaters go through a contactor that the relay "
                    "drives instead."),
    DeviceType("valve", "Solenoid valve (water / gas line)", "Switching",
               _one("pin", "Relay control pin", OUT, C_RELAY),
               plain="A valve that opens while powered",
               note="A solenoid is a coil: put the flyback diode across it "
                    "(most relay modules carry one already) and feed it "
                    "from its own supply, never the ESP32's 3V3. Most are "
                    "open-while-powered -- power loss closes them, which "
                    "is usually what you want on water."),
    DeviceType("lock", "Door strike / magnetic lock", "Switching",
               _one("pin", "Relay control pin", OUT, C_RELAY),
               plain="An electric lock",
               note="Know which way yours fails: a strike is usually "
                    "fail-secure (power to OPEN), a maglock is fail-safe "
                    "(power to STAY LOCKED, so a power cut opens it). "
                    "Wire through the relay's NO or NC contact "
                    "accordingly, and use the lock's own 12V supply."),
    DeviceType("siren", "Siren / alarm bell", "Switching",
               _one("pin", "Relay control pin", OUT, C_RELAY),
               plain="A loud thing for when it matters",
               note="Feed the siren from its own 12V; the relay only "
                    "switches it. Test it once at arm's length."),
    DeviceType("exhaust", "Exhaust / extractor fan", "Switching",
               _one("pin", "Relay control pin", OUT, C_RELAY),
               plain="A bathroom or kitchen extractor",
               note="Mains fan through the relay module's switched side. "
                    "Pairs well with a humidity sensor on the same board "
                    "-- the hub can then run it from the reading."),
    DeviceType("humidifier", "Humidifier / diffuser", "Switching",
               _one("pin", "Relay control pin", OUT, C_RELAY),
               plain="A humidifier switched on and off"),
    DeviceType("aquarium", "Aquarium pump / light", "Switching",
               _one("pin", "Relay control pin", OUT, C_RELAY),
               plain="Aquarium equipment on a schedule",
               note="Anything near water goes through an RCD/GFCI outlet. "
                    "The relay is the switch, not the safety."),
    DeviceType("irrigation", "Irrigation zone valve", "Switching",
               _one("pin", "Relay control pin", OUT, C_RELAY),
               plain="One watering zone",
               note="One relay per zone valve; common wire shared. Garden "
                    "solenoids are usually 24V AC -- match the supply to "
                    "the valve, not to the board."),
    # Infrastructure, not a device: four pins buy eight more outputs. The
    # chip's outputs become EXTRA relays numbered after the physical ones,
    # so this entry registers nothing itself -- the new relays announce
    # themselves in Home Assistant and arrive through the same picker every
    # other entity does, where they get their names.
    DeviceType("hc595", "Output expander (74HC595)", "Switching",
               (PinSlot("ser", "Serial data (SER / DS)", OUT, C_595_SER),
                PinSlot("srclk", "Shift clock (SRCLK / SHCP)", OUT,
                        C_595_SRCLK),
                PinSlot("rclk", "Latch clock (RCLK / STCP)", OUT,
                        C_595_RCLK),
                PinSlot("oe", "Output enable (OE -- active low)", OUT,
                        C_595_OE)),
               plain="Eight more on/off outputs from four pins",
               note="Wire the chip's own OE low-active pin here rather than "
                    "strapping it to ground, so the outputs stay quiet "
                    "through boot. Its eight outputs appear as extra relays "
                    "after the board's physical ones -- register and name "
                    "them as they arrive.",
               produces=EVENT_ONLY,
               driver_names=("74x595 SER",)),

    # ---- things that vary -----------------------------------------------
    DeviceType("dimmer", "Dimmable light (single channel)", "Lighting",
               _one("pin", "PWM output pin", OUT, C_PWM),
               primary=True,
               plain="Brightness or speed you can vary",
               produces=LIGHTY),
    DeviceType("rgb", "RGB light (three channels)", "Lighting",
               (PinSlot("red", "Red channel", OUT, C_PWM),
                PinSlot("green", "Green channel", OUT, C_PWM),
                PinSlot("blue", "Blue channel", OUT, C_PWM)),
               produces=LIGHTY),
    DeviceType("ws2812", "Addressable LED strip (WS2812 / SK6812)", "Lighting",
               _one("pin", "Strip data pin", OUT, C_WS2812),
               primary=True,
               plain="Addressable LED strip",
               note="One data pin for the whole strip. A strip of any real "
                    "length needs its own 5V supply -- a pin can give a few "
                    "milliamps and a strip wants amps -- with its ground "
                    "joined to the board's.",
               produces=LIGHTY),
    DeviceType("strip12v", "12V LED strip (single colour, via MOSFET)",
               "Lighting",
               _one("pin", "MOSFET gate / module signal pin", OUT, C_PWM),
               plain="A plain 12V LED strip, dimmable",
               note="The pin drives a logic-level MOSFET module (IRLZ44N "
                    "board or similar); the strip's 12V comes from its own "
                    "supply with grounds joined. Dimmable like any PWM "
                    "light.",
               produces=LIGHTY),
    DeviceType("grow_light", "Grow light (dimmable)", "Lighting",
               _one("pin", "PWM output pin", OUT, C_PWM),
               plain="A plant light on a schedule and a dimmer",
               produces=LIGHTY),
    DeviceType("night_light", "Night light (dimmable)", "Lighting",
               _one("pin", "PWM output pin", OUT, C_PWM),
               plain="A soft light the hub can fade",
               produces=LIGHTY),

    # {sh} is the SHUTTER number, counted across every shutter-declaring
    # item on the board, and {n} is this item's own first relay instance.
    # The old hardcoded "ShutterRelay1 1" was wrong twice at once: a second
    # shutter re-declared shutter 1 (silently stealing the first one's
    # relays), and the literal 1 assumed the shutter's relay was Relay1,
    # which is only true on a board with nothing else on it.
    #
    # BENCH-TEST OWED (audit, 2026-08-25): whether ShutterMode 5 wants the
    # ShutterRelay declaration in exactly this shape has NOT been proven on
    # a board yet -- the servo is still in the kit box. The board is the
    # referee; when it arrives, this entry gets its verdict.
    #
    # SetOption80 1 FIRST: Tasmota's shutter driver (xdrv_27) answers
    # nothing -- ShutterRelay, ShutterPosition, ShutterOpen are all
    # "Unknown" -- until it is on, and its default is off. A two-relay
    # shutter also needs its pair INTERLOCKED before ShutterRelay runs
    # ShutterInit: auto-configured mode 1 without an interlock disables the
    # shutter ("Relays are not in INTERLOCK"), and an undefined mode falls
    # back to mode 2, whose "close" energises both relays at once.
    DeviceType("servo", "Servo (lock, latch, valve)", "Movement",
               (PinSlot("enable", "Enable relay", OUT, C_RELAY),
                PinSlot("pwm", "Servo signal pin", OUT, C_PWM)),
               setup=("SetOption80 1", "ShutterRelay{sh} {n}",
                      "ShutterMode 5", "PWMfrequency 200"),
               note="Tasmota drives a servo through its shutter driver, so "
                    "this appears as a shutter with a position rather than "
                    "as a servo. It behaves correctly; the wording is odd.",
               produces=COVERY),
    DeviceType("shutter", "Blind / curtain / gate motor", "Movement",
               (PinSlot("up", "Open relay", OUT, C_RELAY),
                PinSlot("down", "Close relay", OUT, C_RELAY)),
               setup=_TWO_RELAY_SHUTTER,
               produces=COVERY),
    # Shutter aliases (2026-08-25 sweep): same two-relay driver, each
    # name carrying its own wiring truth. All ride {sh}/{n}, so several
    # on one board number themselves correctly.
    DeviceType("garage", "Garage door (two relays)", "Movement",
               (PinSlot("up", "Open relay", OUT, C_RELAY),
                PinSlot("down", "Close relay", OUT, C_RELAY)),
               setup=_TWO_RELAY_SHUTTER,
               plain="A garage door that opens and closes",
               note="For an opener with separate open/close inputs. An "
                    "opener with ONE push-button input is a plain on/off "
                    "thing with PulseTime instead -- ask before wiring.",
               produces=COVERY),
    DeviceType("gate", "Driveway gate motor", "Movement",
               (PinSlot("up", "Open relay", OUT, C_RELAY),
                PinSlot("down", "Close relay", OUT, C_RELAY)),
               setup=_TWO_RELAY_SHUTTER,
               plain="A gate that opens and closes",
               produces=COVERY),
    DeviceType("curtain", "Curtain motor", "Movement",
               (PinSlot("up", "Open relay", OUT, C_RELAY),
                PinSlot("down", "Close relay", OUT, C_RELAY)),
               setup=_TWO_RELAY_SHUTTER,
               plain="A curtain track the hub can open and close",
               produces=COVERY),
    # A DC motor through an H-bridge is on/off/DIRECTION, not speed: speed
    # needs PWM into the enable pin, and Tasmota has no first-class DC
    # motor driver -- promising one here would be the catalogue's cardinal
    # sin. What Tasmota DOES have is relays and Interlock, and that pair is
    # exactly an H-bridge's contract: IN1 and IN2 must never be high
    # together (on the cheap L298N/L9110 boards that is shoot-through).
    # The Interlock travels WITH the wiring -- {n} and {n2} are this item's
    # own two relay numbers -- so the safety is not a fact somebody has to
    # remember on the console.
    DeviceType("hbridge", "DC motor (H-bridge: L298N / L9110)", "Movement",
               (PinSlot("fwd", "Forward input (IN1 / A-1A)", OUT, C_RELAY),
                PinSlot("rev", "Reverse input (IN2 / A-1B)", OUT, C_RELAY)),
               setup=("Interlock {n},{n2}", "Interlock ON"),
               plain="A motor that runs forward or backward",
               note="Forward, reverse, stop -- not speed (that needs PWM on "
                    "the ENable pin; leave its jumper on for full speed). "
                    "The two inputs appear in Home Assistant as two "
                    "switches, interlocked so both can never be on at once. "
                    "Motor power comes from the driver board's own supply, "
                    "never the ESP32's 3V3.",
               produces=EVENT_ONLY),

    DeviceType("buzzer", "Buzzer / chime (passive)", "Signalling",
               _one("pin", "Buzzer pin", OUT, C_BUZZER),
               setup=("SetOption111 1",),
               note="SetOption111 switches the buzzer from a plain on/off "
                    "beep to a real tone, which is what a passive piezo "
                    "needs."),
    DeviceType("ir_send", "Infrared blaster", "Signalling",
               _one("pin", "IR LED pin", OUT, C_IR_SEND),
               note="The standard firmware covers the common remote "
                    "protocols. Air-conditioner remotes need the separate "
                    "IR build.",
               produces=COMMAND_ONLY),
    DeviceType("ir_recv", "Infrared receiver (TSOP38xx)", "Signalling",
               _one("pin", "Signal pin", IN, C_IR_RECV),
               note="A TSOP1838 or similar 38 kHz receiver: three legs, "
                    "OUT / GND / VCC facing the bulge. Runs happily on "
                    "3.3 V. It LEARNS codes -- every remote press it hears "
                    "is published as an event, so nothing appears in Home "
                    "Assistant and there is nothing to register; the hub "
                    "hears it over MQTT instead.",
               plain="Infrared receiver, for learning remote-control codes",
               produces=EVENT_ONLY),
    DeviceType("rf_send", "433 MHz RF transmitter", "Signalling",
               _one("pin", "RF data pin", OUT, C_RF_SEND),
               produces=COMMAND_ONLY),
    # THE OTHER HALF, ADDED 2026-09-14. `C_RF_RECV` had been defined since
    # the transmitter went in and used by nothing -- a reserved component
    # code with no way to reach it, so a 433 MHz door sensor or remote
    # could not be registered at all while the catalogue implied support.
    #
    # The driver is PROVEN PRESENT, by this project's own rule that the
    # board is the referee: her kitchen board's short `GPIOs` list -- the
    # one filtered to what the binary can actually use -- printed
    # `"RFrecv":1152` alongside IRsend, IRrecv and RFSend. Same shape as
    # the IR receiver above and for the same reasons: a press is a MOMENT,
    # published as an event, so nothing registers in Home Assistant and
    # the hub hears it over MQTT.
    DeviceType("rf_recv", "433 MHz RF receiver", "Signalling",
               _one("pin", "RF data pin", IN, C_RF_RECV),
               note="A 433 MHz superheterodyne receiver (RXB6, SRX882 or "
                    "similar); the cheap regenerative ones drift and will "
                    "give you phantom presses. Three or four legs -- "
                    "DATA / GND / VCC, and a second DATA that is the same "
                    "signal. Most want 5 V to reach their rated range, "
                    "and their DATA line is then 5 V too, which an ESP32 "
                    "pin is not rated for: run it at 3.3 V (shorter "
                    "range) or put a divider on DATA. Like the infrared "
                    "receiver, an unconnected pin FLOATS and the driver "
                    "will decode noise off it all day.",
               plain="433 MHz receiver, for remotes and wireless sensors",
               produces=EVENT_ONLY),

    # ---- Displays ------------------------------------------------------
    # A display takes no pins of its own beyond the shared I²C bus, so the
    # driver guard cannot vouch for it through `GPIOs` -- I²C devices leave
    # no GPIO component name behind. The runtime proof is the DisplayModel
    # command: on a build without USE_DISPLAY it answers "Unknown", on the
    # HomeVoice display build it answers with the model number this entry
    # set. The TM1637 seven-segment and MAX7219 dot-matrix entries are
    # deliberately NOT here yet: they DO have GPIO components, whose codes
    # must be read off a board running the display build first -- the same
    # way the RC522 and 74x595 codes were earned, never guessed.
    # TWO ENTRIES FOR ONE DRIVER, because the geometry is settings and a
    # wrong geometry writes half a line into nowhere: same build, same
    # DisplayModel 1, different Cols/Rows. Both need the PCF8574 I²C
    # backpack -- Tasmota drives these screens over I²C ONLY. A bare
    # module (16 parallel pins, no little board soldered on the back) has
    # no Tasmota driver at all; the backpack is the cheap fix, not an
    # option.
    # DISPLAYDIMMER AND DISPLAYMODE ARE SETTINGS, NOT DECORATION
    # (field test 2026-09-01). This tuple sent address, geometry and model
    # and stopped, leaving two things at Tasmota's defaults -- which cost a
    # whole evening on a board where every other thing was already right:
    #
    #   * THE BACKLIGHT. `DSP: LCD` in the boot log, `I2C: LCD found at
    #     0x27`, DisplayText accepted and echoed -- and a dark screen,
    #     because Tasmota takes the backlight pin at init and leaves the
    #     dimmer wherever its default puts it. Her board, exactly: "the
    #     light comes on then off". `DisplayDimmer 100` on the live board
    #     brought it back, which is what earns it a place here.
    #   * WHO OWNS THE GLASS, AND WHEN. Mode 1 is Tasmota drawing its own
    #     clock; mode 0 hands the screen to DisplayText. The RESTING state
    #     is 1 (her ruling, 2026-09-01) -- it costs the hub nothing, and on
    #     her board it rendered correctly when nothing else would. An
    #     automation's `display` action flips to 0, writes its line, holds
    #     it, and _lcd_tick returns the panel to the clock when the hold
    #     expires. Set here so a panel comes up showing the time the
    #     moment it is installed, with no rule and no hub.
    #
    # CONTRAST STAYS WHERE IT HAS ALWAYS BEEN: a trimmer on the backpack,
    # reachable by no command at all. At maximum every cell renders as a
    # filled block and the characters drown in it, which looks exactly like
    # a display that is not working. Said in the note rather than left to
    # be rediscovered by the next person holding a screwdriver at 4am.
    DeviceType("lcd2004", "Character LCD 20x4 (2004, I²C)", "Displays",
               _I2C,
               setup=("DisplayAddress 39", "DisplayCols 20",
                      "DisplayRows 4", "DisplayModel 1",
                      "DisplayMode 1", "DisplayDimmer 100"),
               plain="A four-line text screen the hub can write to",
               note="Needs the Omniscience display build (USE_DISPLAY + "
                    "USE_DISPLAY_LCD); on other firmware DisplayModel "
                    "answers Unknown and this entry does nothing. Address "
                    "39 is the common 0x27 backpack; the other batch is "
                    "0x3F -- `I2Cscan` on the console names yours, "
                    "`DisplayAddress 63` switches. If it lights up showing "
                    "solid blocks and no text, that is the CONTRAST "
                    "trimmer on the backpack turned too far up -- no "
                    "command can reach it; turn it down until the blocks "
                    "fade and the characters stay. Test with: "
                    "DisplayText Hello",
               produces=EVENT_ONLY),
    DeviceType("lcd1602", "Character LCD 16x2 (1602 / 1602A, I²C)",
               "Displays",
               _I2C,
               setup=("DisplayAddress 39", "DisplayCols 16",
                      "DisplayRows 2", "DisplayModel 1",
                      "DisplayMode 1", "DisplayDimmer 100"),
               plain="A two-line text screen the hub can write to",
               note="The 1602A needs the PCF8574 I²C backpack soldered on "
                    "its 16-pin header -- Tasmota cannot drive the bare "
                    "parallel module. Same build and checks as the 20x4: "
                    "display build required, `I2Cscan` for the address "
                    "(39 = 0x27, 63 = 0x3F), blocks-with-no-text is the "
                    "contrast trimmer, test with DisplayText Hello.",
               produces=EVENT_ONLY),
    # NO SSD1306 OLED ENTRY, deliberately (2026-08-25). The sweep tried:
    # USE_DISPLAY_SSD1306 compiled green and the CI string-proof found
    # NO driver in the binary -- in this Tasmota the define is a name
    # with nothing behind it, and OLEDs are driven by the universal
    # display driver from a descriptor file loaded onto the board. That
    # is an integration design job, not a define, so the entry waits
    # until a board has proven one -- offering it now would be the
    # RC522 trap with a screen attached.
))


#: Pins that TELL the hub something.
#:
#: This branch keeps its second question -- "what is connected?" -- and that
#: is not the catalogue being fussy. A DHT22, a DS18B20 and a plain analog
#: probe speak completely different protocols down the same wire, and Tasmota
#: has to be told which driver to load. It is also the only place the units
#: and the sensible range come from, so Home Assistant shows 21.4 °C instead
#: of a bare number.
_READS: Tuple[DeviceType, ...] = _stamp(READS, (
    # ---- the short list --------------------------------------------------
    # SetOption73 1 ON BOTH. Without it a Tasmota Button TOGGLES RELAY 1
    # and publishes nothing of its own, so the hub never heard a press --
    # it saw, at most, a relay it did not ask for changing. With it the
    # button is detached and every press is published as an event,
    # `{"Button1":{"Action":"SINGLE"}}` (DOUBLE ... PENTA, HOLD), which
    # the automation engine indexes as ("button", n). Global and restart-
    # free, so it is re-asserted with the switch settings.
    DeviceType("button", "Push button", "Inputs",
               _one("pin", "Button pin", IN, C_BUTTON),
               setup=("SetOption73 1",),
               primary=True,
               plain="A button you press",
               produces=CONTACTY),
    DeviceType("doorbell", "Doorbell button", "Inputs",
               _one("pin", "Button pin", IN, C_BUTTON),
               setup=("SetOption73 1",),
               plain="The doorbell, heard by the hub",
               note="The BUTTON side only -- the pin reads the press, and "
                    "the hub decides what rings (a chime relay, a spoken "
                    "announcement, both). An existing lit doorbell may "
                    "carry AC on its wires; give the board its own dry "
                    "contact instead of sharing the transformer.",
               produces=CONTACTY),

    # WHICH WAY DOES THE PIN MOVE WHEN THE THING HAPPENS? That is the only
    # question separating these entries, and until 2026-08-24 the catalogue
    # did not ask it -- every one of them sent `SwitchMode 1` and the word
    # beside them was the only difference. For half of them that was
    # backwards, silently: an entity that reads MOTION in an empty room and
    # nothing when you walk in front of it. Nothing errors. It just lies.
    #
    # TWO PHYSICAL FAMILIES:
    #
    #   WHICH MODE IS WHICH (corrected 2026-09-13, on her board). Tasmota's
    #   own table is the authority and it is the plain reading:
    #
    #       SwitchMode 1  follow           pin 0 = OFF, pin 1 = ON
    #       SwitchMode 2  inverted follow  pin 0 = ON,  pin 1 = OFF
    #
    #   The note that used to sit here said the opposite -- that mode 1
    #   "reads triggered = LOW" -- and every entry below was set from that
    #   sentence. Her kitchen PIR settled it: on SwitchMode 2 a hand held
    #   over the lens (detecting, pin HIGH) read OFF and an empty room read
    #   ON, with detection pulses of exactly 1.25s. Backwards, on the
    #   hardware, measured.
    #
    #   So the rule is only: WHICH LEVEL DOES THIS PART DRIVE WHEN THE
    #   THING HAPPENS?
    #
    #   FIRES HIGH -> `SwitchMode 1`. A PIR (HC-SR501), a radar, a
    #   vibration or sound board: idle LOW, HIGH when it fires.
    #
    #   FIRES LOW -> `SwitchMode 2`. Gas (MQ-series), rain, leak and most
    #   comparator boards sit HIGH and pull LOW once the threshold is
    #   crossed. A DRY CONTACT is in this family too when what you mean by
    #   ON is "closed": Tasmota holds the pin HIGH through its internal
    #   pull-up and the contact drags it to ground.
    #
    #   THE DRY CONTACTS BELOW ARE DELIBERATELY LEFT ON 1, which means ON =
    #   the contact is OPEN. For a door or a window that is the reading
    #   people want -- ON when it is open -- so the number is right even
    #   though the sentence that produced it was wrong. For a float switch
    #   or a pressure mat it is a convention, not a fact, and the note on
    #   each says which way to flip it.
    #
    # AND SetOption114 1, ON ALL OF THEM. Without it a Switch drives relay
    # 1 -- so motion would physically flip whatever lamp shares the board --
    # and, just as bad here, Home Assistant never sees it: switches are
    # announced as binary sensors only when they are detached. It is a
    # global setting, so sending it from every entry costs nothing and
    # means no board can be assembled in an order that forgets it.
    #
    # The mode is a fact about the module in your hand, not about the word
    # on the label -- many boards carry an inverting jumper. If a sensor
    # reads backwards, `SwitchMode<n> 1` or `2` on its console flips it,
    # and the same word belongs in the record afterwards.
    DeviceType("contact", "Switch or contact", "Inputs",
               _one("pin", "Contact pin", IN, C_SWITCH),
               setup=("SetOption114 1", "SwitchMode{n} 1"),
               primary=True,
               plain="A switch or contact that stays where you put it",
               note="Covers a reed switch on a door or window, a float "
                    "switch, a wall switch -- anything that CLOSES A "
                    "CIRCUIT rather than driving a signal. A three-wire "
                    "module with its own power is the other kind; use the "
                    "sensor-module entry for those.",
               produces=CONTACTY),
    DeviceType("door", "Door contact (reed switch)", "Inputs",
               _one("pin", "Contact pin", IN, C_SWITCH),
               setup=("SetOption114 1", "SwitchMode{n} 1"),
               produces=CONTACTY),
    DeviceType("window", "Window contact (reed switch)", "Inputs",
               _one("pin", "Contact pin", IN, C_SWITCH),
               setup=("SetOption114 1", "SwitchMode{n} 1"),
               produces=CONTACTY),
    DeviceType("pir", "Motion sensor (PIR)", "Inputs",
               _one("pin", "Signal pin", IN, C_SWITCH),
               # 1, WHICH IS FOLLOW: Tasmota documents SwitchMode 1 as
               # "0 = OFF, 1 = ON", so the pin going HIGH is ON, and an
               # HC-SR501 drives its pin HIGH on motion. Motion reads ON.
               #
               # THE COMMENT HERE USED TO SAY "2, not 1" and argue the
               # opposite, while the code sent 1 and the note below said 1
               # was right -- a line that contradicted both its own file
               # and Tasmota's manual, and an invitation for the next
               # reader to "fix" a correct setting into an inverted one
               # (found 2026-09-14). Mode 2 is the INVERTED follow and
               # belongs only to a module that idles high.
               #
               # Not 14 either -- "push to on" only ever turns ON, which
               # Home Assistant discovers as an automation trigger rather
               # than a binary sensor, so the room would never go quiet
               # again.
               setup=("SetOption114 1", "SwitchMode{n} 1"),
               primary=True,
               plain="Movement in the room",
               note="Wire OUT to the signal pin, and VCC/GND to the board. "
                    "Most PIRs (HC-SR501) idle low and go high on movement, "
                    "which is what this sends. If it reads backwards -- ON "
                    "in an empty room -- the module inverts, and "
                    "`SwitchMode<n> 2` on its console is the flip.",
               produces=CONTACTY),
    DeviceType("sensor_high", "Sensor module, HIGH when it fires", "Inputs",
               _one("pin", "Signal pin (OUT / DO)", IN, C_SWITCH),
               setup=("SetOption114 1", "SwitchMode{n} 1"),
               plain="A three-wire sensor board that goes HIGH when it "
                     "detects something",
               note="The PIR family: microwave radar (RCWL-0516), sound or "
                    "clap boards, vibration (SW-420), IR obstacle and "
                    "beam-break, tilt, flame. Idle low, high when it fires.",
               produces=CONTACTY),
    DeviceType("sensor_low", "Sensor module, LOW when it fires", "Inputs",
               _one("pin", "Signal pin (OUT / DO)", IN, C_SWITCH),
               setup=("SetOption114 1", "SwitchMode{n} 2"),
               plain="A three-wire sensor board that goes LOW when it "
                     "detects something",
               note="The other half of the same family, and the commoner "
                    "wiring on gas (MQ-series), rain, soil-moisture and "
                    "comparator boards: the digital output sits high and "
                    "pulls low once the threshold is crossed.",
               produces=CONTACTY),
    DeviceType("leak", "Water leak sensor", "Inputs",
               _one("pin", "Signal pin", IN, C_SWITCH),
               # Left on 1 deliberately: leak boards are comparator boards
               # and most pull LOW when wet. Confirmed by watching it, not
               # by the label -- a splash of water is a cheap experiment.
               setup=("SetOption114 1", "SwitchMode{n} 2"),
               note="Most leak boards pull their output LOW when wet, which "
                    "is what this assumes. Wet it and watch; if it reads "
                    "backwards, `SwitchMode<n> 1` flips it.",
               produces=CONTACTY),
    # More dry contacts (2026-08-25 sweep) -- electrically the reed-switch
    # story, each with its own installation truth.
    DeviceType("float_switch", "Float switch (tank level)", "Inputs",
               _one("pin", "Contact pin", IN, C_SWITCH),
               setup=("SetOption114 1", "SwitchMode{n} 1"),
               plain="Water reached this level, or left it",
               note="A dry contact that closes when the float tips. Which "
                    "way is 'full' depends on how it is mounted -- watch "
                    "it once and flip with `SwitchMode<n> 2` if backwards.",
               produces=CONTACTY),
    DeviceType("tilt", "Tilt switch", "Inputs",
               _one("pin", "Contact pin", IN, C_SWITCH),
               setup=("SetOption114 1", "SwitchMode{n} 1"),
               plain="Something moved or tipped over",
               note="The bare metal-ball kind is a dry contact and belongs "
                    "here. A SW-520/SW-420 BOARD with three pins drives "
                    "its output -- use the sensor-module entries for "
                    "those.",
               produces=CONTACTY),
    DeviceType("mailbox", "Mailbox contact", "Inputs",
               _one("pin", "Contact pin", IN, C_SWITCH),
               setup=("SetOption114 1", "SwitchMode{n} 1"),
               plain="The mailbox lid opened",
               produces=CONTACTY),
    DeviceType("pressure_mat", "Pressure mat", "Inputs",
               _one("pin", "Contact pin", IN, C_SWITCH),
               setup=("SetOption114 1", "SwitchMode{n} 1"),
               plain="Somebody stood or sat here",
               note="A mat is a big flat dry contact: two wires, closes "
                    "under weight.",
               produces=CONTACTY),
    DeviceType("alarm", "Smoke / gas alarm (digital out)", "Inputs",
               _one("pin", "Alarm output pin", IN, C_SWITCH),
               # Also 1, and for the same reason: an MQ-series board's DO
               # goes LOW when its threshold is crossed.
               setup=("SetOption114 1", "SwitchMode{n} 2"),
               note="MQ-series and most alarm outputs pull LOW when they "
                    "trip. If yours is the other kind, `SwitchMode<n> 1`.",
               produces=CONTACTY),
    DeviceType("rain", "Rain sensor board", "Inputs",
               _one("pin", "Signal pin (DO)", IN, C_SWITCH),
               setup=("SetOption114 1", "SwitchMode{n} 2"),
               plain="It is raining on the sensor plate",
               note="Comparator board, pulls LOW when wet -- the leak "
                    "sensor's outdoor cousin. The bare plate corrodes "
                    "if held powered constantly; mounting it at an angle "
                    "helps it dry.",
               produces=CONTACTY),
    DeviceType("touch", "Capacitive touch pad", "Inputs",
               _one("pin", "Touch pad pin", TOUCH, C_TOUCH_BTN),
               note="Four touch pads per board is the firmware's limit.",
               produces=CONTACTY),
    DeviceType("rotary", "Rotary encoder (dial)", "Inputs",
               (PinSlot("a", "Encoder pin A", IN, C_ROTARY_A),
                PinSlot("b", "Encoder pin B", IN, C_ROTARY_B)),
               setup=("SetOption98 1",),
               note="Without SetOption98 the dial only drives brightness. "
                    "With it, the hub sees every turn and can do what it "
                    "likes with them.",
               produces=COMMAND_ONLY),

    # ---- things that measure ---------------------------------------------
    DeviceType("ldr", "Light level (LDR on analog)", "Analog",
               _one("pin", "Analog input pin", ADC, C_ADC_LIGHT),
               primary=True,
               plain="How bright the room is",
               produces=_sensor("illuminance", 0.0, 10000.0, 10.0)),
    DeviceType("gas_mq", "Gas sensor (MQ-2 / MQ-135 analog)", "Analog",
               _one("pin", "Analog input pin", ADC, C_ADC_MQ),
               primary=True,
               plain="Gas or air quality",
               produces=_sensor("ppm", 0.0, 1000.0, 10.0)),
    DeviceType("soil", "Soil moisture (analog probe)", "Analog",
               _one("pin", "Analog input pin", ADC, C_ADC_RANGE),
               produces=_sensor("moisture", 0.0, 100.0, 5.0)),

    DeviceType("dht11", "Temperature + humidity (DHT11)", "Sensors",
               _one("pin", "Data pin", BIDIR, C_DHT11),
               primary=True,
               plain="Temperature and humidity (DHT11)",
               produces=_sensor("temperature", -10.0, 60.0, 0.5, "humidity")),
    DeviceType("dht22", "Temperature + humidity (DHT22 / AM2301)", "Sensors",
               _one("pin", "Data pin", BIDIR, C_DHT22),
               primary=True,
               plain="Temperature and humidity (DHT22 / AM2301)",
               produces=_sensor("temperature", -40.0, 80.0, 0.5, "humidity")),
    DeviceType("ds18b20", "Temperature probe (DS18B20, 1-Wire)", "Sensors",
               _one("pin", "Data pin", BIDIR, C_DS18X20),
               primary=True,
               plain="Temperature only (DS18B20 probe)",
               note="Several probes can share one pin; they are told apart "
                    "by their built-in serial numbers.",
               produces=_sensor("temperature", -55.0, 125.0, 0.5)),
    DeviceType("hcsr04", "Distance (HC-SR04 ultrasonic)", "Sensors",
               (PinSlot("trigger", "Trigger pin", OUT, C_SR04_TRIG),
                PinSlot("echo", "Echo pin", IN, C_SR04_ECHO)),
               primary=True,
               plain="Distance to something (ultrasonic)",
               produces=_sensor("distance", 0.0, 400.0, 1.0)),

    DeviceType("joystick", "Joystick (analog X/Y)", "Analog",
               (PinSlot("x", "X axis (VRx)", ADC, C_ADC_JOY),
                PinSlot("y", "Y axis (VRy)", ADC, C_ADC_JOY)),
               plain="A two-axis analog stick, read as positions",
               note="ADC1 pins only (32-39) -- ADC2 dies with Wi-Fi on. "
                    "Readings arrive with telemetry, POLLED: it reads a "
                    "position, it is not a live game controller. The "
                    "stick's press-switch (SW) is its own Push button "
                    "entry on a third pin. Watch tele SENSOR on the "
                    "console once wired to see what yours reports.",
               produces=EVENT_ONLY),
    DeviceType("analog", "Any other analog sensor", "Analog",
               _one("pin", "Analog input pin", ADC, C_ADC_RAW),
               primary=True,
               plain="A voltage I will make sense of myself",
               produces=_sensor("value", 0.0, 4095.0, 50.0)),

    DeviceType("bme280", "Temperature + humidity + pressure (BME280)",
               "Sensors", _I2C,
               note="Found automatically once the I²C bus exists -- nothing "
                    "to configure per sensor.",
               produces=_sensor("temperature", -40.0, 85.0, 0.5,
                                 "humidity", "pressure")),
    DeviceType("bh1750", "Light level (BH1750 / GY-302, I²C)", "Light & UV",
               _I2C,
               note="Found automatically on the I²C bus (0x23 or 0x5C).",
               produces=_sensor("illuminance", 0.0, 65535.0, 10.0)),
    DeviceType("ina219", "DC current + power (INA219, I²C)", "Sensors", _I2C,
               note="Found automatically on the I²C bus.",
               produces=_sensor("current", 0.0, 3.2, 0.05,
                                 "voltage", "power")),
    DeviceType("mpu6050", "Motion IMU (MPU6050 gyro + accel, I²C)",
               "Sensors", _I2C,
               note="Needs the Omniscience build with USE_MPU6050 -- I²C "
                    "devices leave no name in `GPIOs`, so the proof is "
                    "`I2Cscan` finding 0x68 AND an MPU6050 block appearing "
                    "in tele SENSOR (acceleration and gyro per axis). "
                    "Registers entity-less here; whatever entities Home "
                    "Assistant creates for it arrive through the picker "
                    "and can be registered from there.",
               produces=EVENT_ONLY),

    # ---- THE I²C SWEEP (2026-08-25) ----------------------------------
    #
    # Every entry below rides the shared bus and costs ZERO extra pins,
    # which is what makes this sweep cheap and honest at once. None of
    # them can be vouched for through `GPIOs` -- I²C devices leave no
    # component name there -- so no driver_names claims are made; each
    # note instead carries the RUNTIME proof: the I2Cscan address, and
    # the block name that appears in tele SENSOR once the chip answers.
    # All need the HomeVoice sweep build (user_config_override.h,
    # 2026-08-25 defines) -- the CI workflow proves each driver by its
    # own string in the binary before the artifact ships, so a green run
    # MEANS these are in the image.
    #
    # Addresses are crowded. Where two supported chips share one (CCS811
    # and MLX90614 at 0x5A; VEML6070 and AHT20 at 0x38), Tasmota tells
    # them apart by register signature -- its own tasmota32-sensors build
    # ships them together -- and the note says so where it matters.
    # Deliberately NOT swept, each for a named reason in the override
    # file: PCF8574 (would fight the LCD backpack for 0x27), DS3231
    # (0x68 is the MPU6050's), APDS9960 (its define disables SHT and
    # VEML6070), TSL2561/TSL2591 (0x29/0x39 crowd VL53L0X and others;
    # the four light sensors here cover the range).

    # -- temperature and humidity, the kit-box parade -------------------
    DeviceType("sht3x", "Temperature + humidity (SHT30/31/35, I²C)",
               "Sensors", _I2C,
               plain="Temperature and humidity (SHT3x)",
               note="Needs the Omniscience sweep build. Proof: I2Cscan finds "
                    "0x44 (or 0x45), and an SHT3X block appears in tele "
                    "SENSOR.",
               produces=_sensor("temperature", -40.0, 125.0, 0.5,
                                "humidity")),
    DeviceType("aht20", "Temperature + humidity (AHT20/AHT21, I²C)",
               "Sensors", _I2C,
               plain="Temperature and humidity (AHT20)",
               note="Needs the Omniscience sweep build. Proof: I2Cscan finds "
                    "0x38, and an AHT2X block appears in tele SENSOR. "
                    "0x38 is also the VEML6070's second address; Tasmota "
                    "tells them apart by their registers.",
               produces=_sensor("temperature", -40.0, 85.0, 0.5,
                                "humidity")),
    DeviceType("htu21", "Temperature + humidity (HTU21 / SI7021, I²C)",
               "Sensors", _I2C,
               plain="Temperature and humidity (HTU21 or SI7021 breakout)",
               note="Needs the Omniscience sweep build. Proof: I2Cscan finds "
                    "0x40, and an HTU21/SI7021 block appears in tele "
                    "SENSOR. The GY-21 breakout is this chip.",
               produces=_sensor("temperature", -40.0, 125.0, 0.5,
                                "humidity")),
    DeviceType("am2320", "Temperature + humidity (AM2320, I²C)",
               "Sensors", _I2C,
               plain="Temperature and humidity (AM2320)",
               note="The I²C sibling of the wired AM2301/DHT22. Needs the "
                    "sweep build; proof: I2Cscan finds 0x5C, AM2320 block "
                    "in tele SENSOR.",
               produces=_sensor("temperature", -40.0, 80.0, 0.5,
                                "humidity")),
    DeviceType("bmp180", "Pressure + temperature (BMP085/180/280, I²C)",
               "Sensors", _I2C,
               plain="Barometric pressure (BMP-series)",
               note="Same driver as the BME280, so it is in every build "
                    "this hub has shipped -- the chip announces which it "
                    "is. Proof: I2Cscan finds 0x76 or 0x77, and a "
                    "BMP180/BMP280 block appears in tele SENSOR.",
               produces=_sensor("pressure", 300.0, 1100.0, 1.0,
                                "temperature")),
    DeviceType("bme680", "Climate + gas (BME680/688, I²C)",
               "Sensors", _I2C,
               plain="Temperature, humidity, pressure and air resistance",
               note="Needs the Omniscience sweep build. Proof: I2Cscan finds "
                    "0x76/0x77, BME680 block in tele SENSOR. Its gas "
                    "reading is a raw resistance, not a calibrated air "
                    "quality -- trend, not truth.",
               produces=_sensor("temperature", -40.0, 85.0, 0.5,
                                "humidity", "pressure", "gas")),
    DeviceType("mlx90614", "Non-contact temperature (MLX90614, I²C)",
               "Sensors", _I2C,
               plain="The temperature of whatever it is pointed at",
               note="An IR thermometer: object and ambient, no touching. "
                    "Needs the sweep build; proof: I2Cscan finds 0x5A, "
                    "MLX90614 block in tele SENSOR. 0x5A is also the "
                    "CCS811's -- registers tell them apart, but avoid "
                    "putting both on one board.",
               produces=_sensor("temperature", -70.0, 380.0, 0.5,
                                "ambient")),

    # -- soil ------------------------------------------------------------
    DeviceType("seesaw_soil", "Soil moisture (Adafruit capacitive, I²C)",
               "Sensors", _I2C,
               plain="Capacitive soil moisture, no corroding probe",
               note="Needs the sweep build. Proof: I2Cscan finds 0x36, "
                    "SeeSoil block in tele SENSOR.",
               produces=_sensor("moisture", 0.0, 100.0, 5.0,
                                "temperature")),
    DeviceType("chirp", "Soil moisture (Chirp!, I²C)",
               "Sensors", _I2C,
               plain="The Chirp soil sensor",
               note="Needs the sweep build. Proof: I2Cscan finds 0x20, "
                    "Chirp block in tele SENSOR.",
               produces=_sensor("moisture", 0.0, 100.0, 5.0,
                                "temperature", "illuminance")),

    # -- distance and motion --------------------------------------------
    DeviceType("vl53l0x", "Laser distance (VL53L0X, I²C)",
               "Sensors", _I2C,
               plain="Distance by laser, to about 2 m",
               note="Time-of-flight: narrow beam, indoor light only, "
                    "millimetre answers where the ultrasonic gives "
                    "centimetres. Needs the sweep build; proof: I2Cscan "
                    "finds 0x29, VL53L0X block in tele SENSOR.",
               produces=_sensor("distance", 0.0, 200.0, 1.0)),
    DeviceType("vl53l1x", "Laser distance, long (VL53L1X, I²C)",
               "Sensors", _I2C,
               plain="Distance by laser, to about 4 m",
               note="The VL53L0X's longer-armed sibling -- I2Cscan finds "
                    "the same 0x29, so fit ONE of the two per board. "
                    "Needs the sweep build; VL53L1X block in tele "
                    "SENSOR.",
               produces=_sensor("distance", 0.0, 400.0, 1.0)),
    DeviceType("qmc5883l", "Compass (QMC5883L / GY-271, I²C)",
               "Sensors", _I2C,
               plain="A magnetometer -- which way, and how strong",
               note="Most GY-271 boards carry the QMC clone, not the HMC "
                    "the silkscreen claims -- I2Cscan settles it: 0x0D is "
                    "QMC, 0x1E is HMC (its own entry). Needs the sweep "
                    "build; QMC5883L block in tele SENSOR. Also a decent "
                    "door sensor for a steel door, reading the field "
                    "change.",
               produces=_sensor("magnetic_field", -32768.0, 32767.0,
                                100.0)),
    DeviceType("hmc5883l", "Compass (HMC5883L, I²C)",
               "Sensors", _I2C,
               plain="The genuine Honeywell magnetometer",
               note="I2Cscan finds 0x1E (0x0D means yours is the QMC "
                    "clone -- its own entry). Needs the sweep build; "
                    "HMC5883L block in tele SENSOR.",
               produces=_sensor("magnetic_field", -32768.0, 32767.0,
                                100.0)),

    # -- electrical -----------------------------------------------------
    DeviceType("ads1115", "4-channel analog input (ADS1115, I²C)",
               "Sensors", _I2C,
               plain="Four more analog inputs, better than the chip's own",
               note="16-bit and immune to the ESP32's noisy ADC. Needs "
                    "the sweep build; proof: I2Cscan finds 0x48 (0x49-4B "
                    "by address pin), ADS1115 block with A0..A3 in tele "
                    "SENSOR.",
               produces=_sensor("value", -32768.0, 32767.0, 100.0)),
    DeviceType("ina226", "DC volts + amps (INA226, I²C)",
               "Sensors", _I2C,
               plain="A battery or solar line, measured precisely",
               note="Needs the sweep build AND one console setup: "
                    "Sensor54 tells it the shunt value -- see its Tasmota "
                    "page. I2Cscan finds 0x40 by default, which collides "
                    "with the HTU21; solder the address jumper to 0x41 if "
                    "both share a board. INA226 block in tele SENSOR.",
               produces=_sensor("current", -20.0, 20.0, 0.05,
                                "voltage", "power")),

    # -- weather ---------------------------------------------------------
    DeviceType("as3935", "Lightning detector (AS3935, I²C)",
               "Sensors", _I2C,
               plain="Storms, before they arrive",
               note="Reports each strike with an estimated distance as an "
                    "EVENT in tele SENSOR (AS3935 block) -- nothing to "
                    "poll, nothing in Home Assistant to register. Needs "
                    "the sweep build; I2Cscan finds 0x03. Mount away "
                    "from the board's own switching noise.",
               produces=EVENT_ONLY),

    # -- air quality -----------------------------------------------------
    DeviceType("sgp30", "Air quality (SGP30 TVOC/eCO2, I²C)",
               "Air quality", _I2C,
               plain="How stale the air is (TVOC and eCO2)",
               note="Needs the sweep build. Proof: I2Cscan finds 0x58, "
                    "SGP30 block in tele SENSOR. Its CO2 is ESTIMATED "
                    "from VOCs -- for real CO2, the SCD entries.",
               produces=_sensor("tvoc", 0.0, 60000.0, 10.0, "eco2")),
    DeviceType("ccs811", "Air quality (CCS811 eCO2/TVOC, I²C)",
               "Air quality", _I2C,
               plain="How stale the air is (CCS811 boards)",
               note="Needs the sweep build. Proof: I2Cscan finds 0x5A, "
                    "CCS811 block in tele SENSOR. Wire the board's WAKE "
                    "pin to GND. Wants a 20-minute warm-up before its "
                    "numbers mean much; eCO2 is estimated, not measured.",
               produces=_sensor("eco2", 400.0, 8192.0, 10.0, "tvoc")),
    DeviceType("scd30", "True CO2 (SCD30, I²C)",
               "Air quality", _I2C,
               plain="Real CO2, measured not estimated",
               note="Needs the sweep build. Proof: I2Cscan finds 0x61, "
                    "SCD30 block in tele SENSOR (CO2, temperature, "
                    "humidity). The one to trust for ventilation "
                    "decisions.",
               produces=_sensor("co2", 400.0, 5000.0, 10.0,
                                "temperature", "humidity")),
    DeviceType("scd40", "True CO2 (SCD40/SCD41, I²C)",
               "Air quality", _I2C,
               plain="Real CO2, the newer smaller sensor",
               note="Needs the sweep build. Proof: I2Cscan finds 0x62, "
                    "SCD40 block in tele SENSOR.",
               produces=_sensor("co2", 400.0, 5000.0, 10.0,
                                "temperature", "humidity")),
    DeviceType("sen5x", "Particulates + VOC (SEN54/55, I²C)",
               "Air quality", _I2C,
               plain="Dust, pollen and VOCs in one module",
               note="Needs the sweep build. Proof: I2Cscan finds 0x69, "
                    "SEN5X block in tele SENSOR (PM1/2.5/4/10, VOC, "
                    "humidity, temperature). 0x69 is also the MPU6050's "
                    "alternate address -- keep the MPU's AD0 pin low.",
               produces=_sensor("pm2_5", 0.0, 1000.0, 1.0,
                                "pm10", "voc", "humidity", "temperature")),

    # -- light and UV ----------------------------------------------------
    DeviceType("veml7700", "Light level, wide range (VEML7700, I²C)",
               "Light & UV", _I2C,
               plain="From moonlight to full sun, in lux",
               note="Needs the sweep build. Proof: I2Cscan finds 0x10, "
                    "VEML7700 block in tele SENSOR.",
               produces=_sensor("illuminance", 0.0, 120000.0, 10.0)),
    DeviceType("veml6070", "UV index (VEML6070, I²C)",
               "Light & UV", _I2C,
               plain="How strong the UV is",
               note="Needs the sweep build. Proof: I2Cscan finds 0x38 "
                    "AND 0x39 together (the chip answers on both), "
                    "VEML6070 block in tele SENSOR.",
               produces=_sensor("uv_index", 0.0, 16.0, 0.5)),
    DeviceType("si1145", "Sunlight sensor (SI1145, I²C)",
               "Light & UV", _I2C,
               plain="Visible, infrared and UV in one",
               note="Needs the sweep build. Proof: I2Cscan finds 0x60, "
                    "SI1145 block in tele SENSOR.",
               produces=_sensor("illuminance", 0.0, 65535.0, 10.0,
                                "uv_index", "infrared")),
    DeviceType("max44009", "Light level, tiny (MAX44009 / GY-49, I²C)",
               "Light & UV", _I2C,
               plain="Lux from a very small board",
               note="Needs the sweep build. Proof: I2Cscan finds 0x4A, "
                    "MAX44009 block in tele SENSOR. 0x4A sits inside the "
                    "ADS1115's address range -- jumper the ADS elsewhere "
                    "if they share a board.",
               produces=_sensor("illuminance", 0.0, 188000.0, 10.0)),

    # TWO ENTRIES FOR ONE PRODUCT NAME, because "PZEM-004T" is two meters.
    # V3.0 and later (V4.0 included) speak Modbus and need PZEM016 Rx; the
    # originals (v1/v2, no version printed) speak their own serial protocol
    # and need PZEM004 Rx. Wired to the wrong one, a meter is SILENT -- no
    # error, no reading, nothing to debug -- so the picker asks the one
    # question that decides it: is "V3.0" or "V4.0" printed on the board?
    DeviceType("pzem", "Mains power meter (PZEM-004T V3/V4, UART)", "Sensors",
               # Crossed, like every UART: the meter's TX lands on the pin
               # named here as "receive", and its RX on "transmit". The
               # meter's logic side wants 5 V on VCC; its TX idles at that
               # 5 V, so that ONE wire gets a divider (1k series, 2k to
               # ground) before an ESP32 input.
               (PinSlot("tx", "Transmit pin to meter (-> meter RX)", OUT, C_PZEM_TX),
                PinSlot("rx", "Receive pin from meter (<- meter TX)", IN, C_PZEM_RX)),
               plain="Mains power meter (PZEM-004T with V3.0 or V4.0 "
                     "printed on it)",
               produces=_sensor("power", 0.0, 5000.0, 10.0,
                                 "voltage", "current", "energy")),
    DeviceType("pzem_v1", "Mains power meter (PZEM-004T v1/v2, UART)",
               "Sensors",
               (PinSlot("tx", "Transmit pin to meter (-> meter RX)", OUT, C_PZEM_TX),
                PinSlot("rx", "Receive pin from meter (<- meter TX)", IN, C_PZEM004_RX)),
               plain="Mains power meter (older PZEM-004T, no version "
                     "printed on the board)",
               produces=_sensor("power", 0.0, 5000.0, 10.0,
                                 "voltage", "current", "energy")),
    DeviceType("pzem_dc", "DC power meter (PZEM-017 / -003, UART)",
               "Sensors",
               (PinSlot("tx", "Transmit pin to meter (-> meter RX)", OUT, C_PZEM_TX),
                PinSlot("rx", "Receive pin from meter (<- meter TX)", IN, C_PZEM017_RX)),
               note="For a battery or solar line, not mains. The -017 "
                    "measures through its external shunt; volts and amps "
                    "are DC.",
               plain="DC power meter (PZEM-017 or PZEM-003, for a battery "
                     "or solar line)",
               produces=_sensor("power", 0.0, 5000.0, 10.0,
                                 "voltage", "current", "energy")),
    # THE RC522, BACK -- and the reason it left is now a field on it.
    #
    # It was removed once, after an afternoon lost to this: RC522 is not in
    # any precompiled Tasmota binary (the docs say so plainly), but a name
    # in `GPIOs 255` proves only that the component is in Tasmota's ENUM,
    # not that the DRIVER is in this binary. The template took the pins,
    # `Gpio 255` echoed `RC522 CS` back, and nothing on the chip ever read
    # them. Silent, for ever -- the one failure this catalogue exists to
    # make impossible.
    #
    # It is back because the fleet no longer runs stock: HomeVoice builds
    # its own tasmota32 with USE_SPI + USE_RC522 (esp_firmware/
    # user_config_override.h, built by .github/workflows/homevoice-build.yml
    # in the Tasmota fork), and the board's SHORT `GPIOs` list -- the one
    # that is filtered to what the binary can use -- printed `RC522 CS` and
    # `RC522 Rst` on 2026-08-18. `driver_names` carries that requirement
    # into the builder, which asks the same question of every board before
    # sending this entry, so a board still on stock gets a sentence about
    # firmware rather than a reader that never speaks.
    #
    # Rst IS A REAL SLOT, not a nicety. The driver initialises the reader
    # only with both CS and Rst assigned, and the library uses the line to
    # hard-reset the chip on boot. A reader with RST tied to 3.3 V and no
    # Rst pin in the template can sit undetected. One GPIO buys certainty.
    #
    # EVENT_ONLY, like the infrared receiver: a tap is a one-off telemetry
    # line (tele/<topic>/SENSOR with an "RC522" block carrying the UID),
    # not a Home Assistant entity, so nothing registers here. MIND THE GAP:
    # the hub does not yet RUN a listener for those lines. Sensor readings
    # reach this system through Home Assistant, and a tap never becomes an
    # HA entity -- so today a tap is published and heard by NOBODY. The
    # listener is designed (homevoice-rfid-automation-design.md) and not
    # built; until it is, this entry ends at a reader that detects and
    # publishes, which is the chip's half of the promise only. Found
    # 2026-08-24, while hunting a different removal bug: an earlier
    # version of this comment claimed the hub's listener as if it existed.
    DeviceType("rc522", "RFID card reader (RC522, SPI)", "Sensors",
               (PinSlot("cs", "Chip select (SDA / SS on the module)", OUT,
                        C_RC522_CS),
                PinSlot("rst", "Reset (RST on the module)", OUT, C_RC522_RST))
               + _SPI,
               note="Needs Omniscience's own firmware build; stock Tasmota "
                    "takes these pins and never reads them. Module pins: "
                    "SDA -> chip select, RST -> reset, SCK / MOSI / MISO -> "
                    "the shared SPI lines, IRQ left unconnected, 3.3 V and "
                    "GND. Every card tap is published as an event, so "
                    "nothing appears in Home Assistant -- the hub hears it "
                    "over MQTT instead. The tap listener IS built now "
                    "(2026-08-27): a tap can start an automation, and law "
                    "2 keeps it a claim rather than a key. Binding a UID "
                    "to a PERSON (cards.json) is still to come, so today "
                    "a rule reacts to 'a card was tapped', not to whose.",
               plain="Card reader (RC522), for taps that ask a secure "
                     "device to act",
               produces=EVENT_ONLY,
               driver_names=("RC522 CS", "RC522 Rst")),
    # The PN532 IS in the precompiled binary (it appears in the same short
    # list on stock), which is why it never left. For 13.56 MHz cards on a
    # board that has NOT been given the HomeVoice build, this is the reader.
    DeviceType("pn532", "RFID / NFC reader (PN532)", "Sensors",
               (PinSlot("tx", "Transmit pin to reader", OUT, C_PN532_TX),
                PinSlot("rx", "Receive pin from reader", IN, C_PN532_RX)),
               note="Wire the PN532's DIP switches for UART (HSU). Tasmota "
                    "does not implement this reader over I²C, whatever the "
                    "module's silkscreen offers.",
               produces=_sensor("tag", 0.0, 0.0, 0.0)),
))


#: ORDER MATTERS ONLY FOR DISPLAY, and this order is the one the picker
#: walks: what the hub drives, then what reports back. The old file was
#: ordered by category, which is why "relay" sat below nine kinds of input
#: in an alphabetical group list and had to be hunted for.
CATALOGUE: Tuple[DeviceType, ...] = _DOES + _READS

BY_ID: Dict[str, DeviceType] = {d.id: d for d in CATALOGUE}


# ---------------------------------------------------------------------------
# Questions the picker asks
# ---------------------------------------------------------------------------

#: How strongly to argue. Only IMPOSSIBLE stops anything.
#:
#: NOTE and SEVERITY used to sit here too. NOTE was a fifth level that
#: nothing ever produced -- neither _pin_info nor check() can return it --
#: and SEVERITY was a ranking table built so code could ask "is this at
#: least as serious as WARN", which nothing went on to ask: blocks() is a
#: bare comparison against IMPOSSIBLE. Both removed rather than left as a
#: vocabulary the app might reasonably expect to receive.
OK = "ok"                # nothing to say
WARN = "warn"            # works, but can bite in a specific situation
WONT_WORK = "wont_work"  # the silicon cannot do this; confirm to proceed
IMPOSSIBLE = "impossible"  # the assignment could not take effect at all


class PlacementError(ValueError):
    """A refusal from BoardPlan.add that says HOW HARD it refuses.

    The builder used to decide whether a refusal was confirmable by
    grepping the prose for words like "transistor" -- which meant the
    sentence could never be reworded without silently changing which
    refusals offered an override (audit, 2026-08-25). The severity rides
    on the exception now: WONT_WORK means `anyway=True` reopens the
    question, IMPOSSIBLE means nothing does. Still a ValueError, so every
    caller that only wanted "it failed" keeps working unchanged.
    """

    def __init__(self, message: str, severity: str = IMPOSSIBLE):
        super().__init__(message)
        self.severity = severity


@dataclass(frozen=True)
class PinInfo:
    """Everything known about one pin, so the picker can just show it.

    The point of this record is that NOTHING IS WITHHELD. Every pin appears,
    every pin says what it is for, and the awkward ones say why they are
    awkward. Somebody who wants GPIO1 for a relay can have it; they just find
    out first that it is the serial console.
    """
    number: int
    can: Tuple[str, ...]         # roles this pin genuinely satisfies
    purpose: str                 # the one-line "what is this pin"
    caution: str = ""            # the cost or risk, if any
    severity: str = OK           # how loudly to say the caution


def _ranges(pins: frozenset) -> str:
    """{34, 35, 36, 37} -> "34-37"; for sentences about a board's pins."""
    out, run = [], []
    for n in sorted(pins):
        if run and n == run[-1] + 1:
            run.append(n)
            continue
        if run:
            out.append(f"{run[0]}-{run[-1]}" if len(run) > 1 else str(run[0]))
        run = [n]
    if run:
        out.append(f"{run[0]}-{run[-1]}" if len(run) > 1 else str(run[0]))
    return ", ".join(out)


def _pin_info(n: int, board: Optional[Board] = None) -> PinInfo:
    board = board or chips.DEFAULT
    can = tuple(role for role in (OUT, IN, ADC, TOUCH, BIDIR)
                if n in board.role_pins(role))
    classic = board.family == "esp32"

    bits = ["reads only, cannot drive" if n in board.input_only
            else "input or output"]
    if n in board.adc:
        bits.append("analog in")
    if n in board.touch:
        bits.append("touch")
    if n in board.console:
        bits.append("serial console")
    if n in board.usb:
        bits.append("USB")
    if classic and n in (16, 17):
        bits.append("second serial port (TX2/RX2)")
    purpose = ", ".join(bits)

    # Worst thing about this pin wins, and only one line is shown -- a pin
    # with three footnotes reads as noise and gets skipped.
    caution, severity = "", OK
    if n in board.maybe_absent:
        caution = (f"GPIO{n} exists only on some packages of this chip -- "
                   f"if your board has no pin for it, there is nothing to "
                   f"wire.")
        severity = WARN
    if n in board.psram:
        # The static, before-anyone-has-asked version of the truth. Once a
        # board has answered Status 4, the plan enforces the answer -- this
        # line is for the pin screen of a board the hub has not met yet.
        caution = ((f"Ordinary on a WROOM module. On a WROVER, GPIO{n} "
                    f"belongs to the RAM chip inside the module and cannot "
                    f"be used at all -- the hub confirms which this board "
                    f"is at its first flash, and will refuse this pin if "
                    f"the RAM owns it.") if classic else
                   (f"On modules with octal PSRAM or flash (N8R8, N16R8, "
                    f"N32...), GPIO{n} belongs to that memory and cannot be "
                    f"used -- the hub confirms which this board is at its "
                    f"first flash, and refuses it until then."))
        severity = WARN
    if n in board.strapping:
        caution = (f"Read while the board starts. Works normally, but if "
                   f"whatever you attach holds GPIO{n} high or low during a "
                   f"reset, the board may not boot.")
        severity = WARN
    if n in board.qio_flash:
        caution = (f"Free on most modules, but used for flash memory on ones "
                   f"wired for Quad I/O -- and nothing on the board tells you "
                   f"which you have. If GPIO{n} stops the board booting, that "
                   f"is why.")
        severity = WARN
    if n in board.usb:
        caution = (f"This is the chip's own USB connection. Using GPIO{n} "
                   f"costs you flashing and the console over that USB port "
                   f"-- fine if you do everything over the air.")
        severity = WARN
    if n in board.console:
        caution = (f"This is the USB serial console. Using GPIO{n} costs you "
                   f"the boot log and re-flashing over the cable -- fine if "
                   f"you do everything over the air, awkward when something "
                   f"goes wrong.")
        severity = WARN
    if n in board.onboard:
        # What the board itself has on this pin: said last, and joined to
        # whatever else is true of the pin, because it is the one that
        # surprises (an LED glowing, a button pulling the line low).
        shared = (f"On this board GPIO{n} is also wired to "
                  f"{board.onboard[n]}, so whatever you attach shares it.")
        caution = f"{caution} {shared}".strip()
        severity = WARN
    return PinInfo(n, can, purpose, caution, severity)


def pins(board: Optional[Board] = None) -> List[PinInfo]:
    """Every pin, in ascending order. The picker shows all of them."""
    board = board or chips.DEFAULT
    return [_pin_info(n, board) for n in board.usable]


def where_can_it_go(type_id: str, slot_key: Optional[str] = None,
                    board: Optional[Board] = None) -> List[int]:
    """The pins a device type will accept -- the other direction, for when
    somebody starts from 'I have a motion sensor' instead of from a pin."""
    board = board or chips.DEFAULT
    device = BY_ID.get(type_id)
    if device is None:
        return []
    slots = ([s for s in device.slots if s.key == slot_key]
             if slot_key else list(device.slots))
    return sorted(set().union(*(board.role_pins(slot.role) for slot in slots)))


def check(type_id: str, slot_key: str, pin: int,
          board: Optional[Board] = None) -> Tuple[str, str]:
    """(severity, message). ADVICE, not a gate.

    Only IMPOSSIBLE blocks, and it blocks because the assignment would not
    reach the chip at all -- a control that pretends to do something and
    does not is worse than no control. Everything else is said and then
    allowed, because a rule I invented should not outrank somebody who can
    see their own board.

    WONT_WORK is the interesting one. It is not a matter of taste: GPIO34
    has no output transistor, so a relay on it will never switch, no matter
    how creative the person is. Tasmota accepts it silently and the fault
    looks like a code bug for a week. So it is stated in those words and
    then allowed, behind a confirmation.
    """
    board = board or chips.DEFAULT
    device = BY_ID.get(type_id)
    if device is None:
        return IMPOSSIBLE, f"{type_id!r} is not a device type."
    slot = next((s for s in device.slots if s.key == slot_key), None)
    if slot is None:
        return IMPOSSIBLE, f"{device.label} has no {slot_key!r} pin."

    if pin not in board.usable:
        if pin in board.flash:
            return IMPOSSIBLE, (
                f"GPIO{pin} is wired to the flash chip the board runs its "
                f"own program from. Tasmota refuses to assign it at all, so "
                f"this setting would be discarded.")
        if pin in board.absent and pin in board.template_order:
            return IMPOSSIBLE, (
                f"GPIO{pin} exists on the chip but no pin on this module "
                f"connects to it, and Tasmota's board table zeroes it. There "
                f"is nothing to wire to.")
        return IMPOSSIBLE, (
            f"There is no GPIO{pin} on this chip ({board.family_label}).")

    if pin not in board.role_pins(slot.role):
        if slot.role in (OUT, BIDIR):
            return WONT_WORK, (
                f"GPIO{pin} has no output driver -- the transistor that "
                f"would pull the pin high or low is simply absent on pins "
                f"{_ranges(board.input_only)}. It can read all day but it "
                f"cannot drive {device.label.lower()}. Tasmota will accept "
                f"this and the device will never respond.")
        if slot.role == ADC:
            return WONT_WORK, (
                (f"GPIO{pin} measures on ADC2, and the radio takes ADC2 over "
                 f"whenever Wi-Fi is on -- which is always, here. Readings "
                 f"will be nonsense or nothing. The pins that work are "
                 f"{sorted(board.role_pins(ADC))}.") if board.adc else
                "This chip has no analogue input the hub can use.")
        if slot.role == TOUCH:
            return WONT_WORK, (
                (f"GPIO{pin} has no touch channel wired to it. Touch works "
                 f"on {sorted(board.role_pins(TOUCH))}.") if board.touch else
                f"This chip ({board.family_label}) has no touch channels.")
        return WONT_WORK, (
            f"GPIO{pin} cannot do {slot.label.lower()}.")

    # Tasmota reads buttons, switches and encoders with the pin's INTERNAL
    # pull-up, and input-only pins have none: a bare contact there floats
    # and reports phantom presses and flips.
    if pin in board.input_only and slot.component.code in (
            C_BUTTON.code, C_SWITCH.code, C_ROTARY_A.code, C_ROTARY_B.code):
        return WARN, (
            f"GPIO{pin} has no internal pull-up. A module that drives its "
            f"own output (a PIR, a sensor board) is fine; a bare switch, "
            f"button or encoder needs a 10k resistor from GPIO{pin} to 3V3, "
            f"or it floats and reports phantom changes.")

    info = _pin_info(pin, board)
    return info.severity, info.caution


def blocks(severity: str) -> bool:
    """Whether a severity should stop the assignment outright."""
    return severity == IMPOSSIBLE


# ---------------------------------------------------------------------------
# Turning a board full of choices into something Tasmota understands
# ---------------------------------------------------------------------------

@dataclass
class Assignment:
    """One device the person has placed on this board."""
    type_id: str
    name: str
    pins: Dict[str, int]                 # slot key -> GPIO number
    instances: Dict[str, int] = field(default_factory=dict)   # filled in


class BoardPlan:
    """Everything assigned to one physical board.

    Holds the allocation state -- which relay number, which switch number --
    because Tasmota identifies devices by their position within a family, and
    those numbers have to be stable and unique across the whole board.
    """

    def __init__(self, name: str,
                 owns_psram_pins: Optional[bool] = None,
                 board: Optional[Board] = None):
        self.name = name
        self.items: List[Assignment] = []
        #: Which board this is (chips.Board): its pins, its template order.
        #: A record from before profiles is the classic DevKit.
        self.board: Board = board or chips.DEFAULT
        if owns_psram_pins is None and self.board.psram_known is not None:
            owns_psram_pins = self.board.psram_known
        #: Does the module keep PSRAM_PINS (GPIO16/17) for itself?
        #:
        #: A CONCLUSION, not a measurement -- the builder derives it from
        #: two facts it stores raw: the chip's own name (a PICO keeps its
        #: internal flash there) and the board's Status 4 answer (a WROVER
        #: keeps its RAM there). Neither fact is ever bent to fit the
        #: flag; the flag is recomputed from them.
        #: False -- known free: ordinary pins, ground them when idle,
        #:          assign them at will.
        #: True  -- known taken: refused outright, `anyway` included.
        #: None  -- nobody knows yet. Treated exactly like True where it
        #:          matters, because the two mistakes are not symmetric:
        #:          wrongly sparing two pins leaves a dim LED glowing,
        #:          wrongly driving the module's own bus crash-loops the
        #:          board with the bad template SAVED in its settings.
        self.owns_psram_pins = owns_psram_pins

    # -- placing -------------------------------------------------------
    def taken(self) -> Dict[int, str]:
        held: Dict[int, str] = {}
        for item in self.items:
            for pin in item.pins.values():
                held.setdefault(pin, item.name)
        return held

    def free_pins_for(self, type_id: str, slot_key: str) -> List[int]:
        """Pins this slot could take that nothing else has claimed --
        except a shared bus, which is exactly the thing that IS reused."""
        device = BY_ID[type_id]
        slot = next(s for s in device.slots if s.key == slot_key)
        shared_pin = self._shared_pin(slot) if slot.shared else None
        if shared_pin is not None:
            return [shared_pin]
        held = set(self.taken())
        # A known-PSRAM board simply does not offer 16/17: a pin that would
        # crash the board is not a choice, it is a trap with a label. An
        # UNKNOWN board (None) is treated the same -- the safe direction,
        # as the class docstring promises; add() enforces it too.
        blocked = (self.board.psram if self.owns_psram_pins
                   else frozenset())
        return [p for p in where_can_it_go(type_id, slot_key, self.board)
                if p not in held and p not in blocked]

    def _shared_pin(self, slot: PinSlot) -> Optional[int]:
        # By CODE, not identity: two Component objects for "I2C SDA1" / 640
        # are one bus, as the phone and the firmware see it.
        for item in self.items:
            device = BY_ID[item.type_id]
            for other in device.slots:
                if other.shared and other.component.code == slot.component.code:
                    pin = item.pins.get(other.key)
                    if pin is not None:
                        return pin
        return None

    def _family_room_left(self, device: DeviceType) -> Optional[str]:
        """A sentence when placing this device would overflow a component
        family's instance limit; None when there is room.

        Component.at() already refuses instance N+1 -- but it refuses at
        TEMPLATE time, three questions after the person thought they were
        done, and for the Relay family (32 slots, then the inverted family
        directly behind it) a silent overflow would not even raise: it
        would configure Relay_i and call it success. Enforced here, at
        placement, where the answer can still change (audit, 2026-08-25).
        Shared bus slots are excluded: every rider is instance 1 of the
        one bus, so a second I²C sensor is not a second I2C SDA1.
        """
        counts = Counter(slot.component.code for item in self.items
                         for slot in BY_ID[item.type_id].slots
                         if not slot.shared)
        for slot in device.slots:
            if slot.shared:
                continue
            code = slot.component.code
            counts[code] += 1
            if counts[code] > slot.component.slots:
                limit = slot.component.slots
                return (f"This board already carries {limit} "
                        f"{slot.component.name} device(s), which is the "
                        f"firmware's limit for that family -- a "
                        f"{device.label.lower()} would be number "
                        f"{counts[code]}, and Tasmota has no slot for it.")
        return None

    def add(self, type_id: str, name: str, pins_by_slot: Dict[str, int],
            *, anyway: bool = False) -> List[str]:
        """Place a device. Returns the cautions the person should be shown.

        `anyway=True` is the app saying "they were told it will not work and
        chose it regardless". Without it, a WONT_WORK assignment raises, so
        the caller cannot skip the conversation by accident -- but with it,
        the answer is yes. Nothing here is the last word except assignments
        the chip would discard.

        Every refusal is a PlacementError carrying its severity, so the
        caller can tell "confirmable" from "never" without reading prose.
        """
        device = BY_ID.get(type_id)
        if device is None:
            raise PlacementError(f"{type_id!r} is not a device type.")
        if any(i.name.strip().lower() == name.strip().lower()
               for i in self.items):
            raise PlacementError(f"This board already has something called "
                                 f"{name!r}.")
        full = self._family_room_left(device)
        if full is not None:
            # Not a WONT_WORK: `anyway` does not open this door. The limit
            # is the firmware's, not this table's opinion.
            raise PlacementError(full)
        held = self.taken()
        used_here: Dict[int, str] = {}
        cautions: List[str] = []
        for slot in device.slots:
            if slot.key not in pins_by_slot:
                raise PlacementError(f"{device.label} still needs its "
                                     f"{slot.label.lower()}.")
            pin = pins_by_slot[slot.key]
            # Known taken: refused even with `anyway`. NOT KNOWN YET (a
            # board never installed) is allowed here: the install asks the
            # board itself (Status 4) before any wiring goes on, and a
            # wiring push stops while it is still unknown.
            if self.owns_psram_pins and pin in self.board.psram:
                raise PlacementError(
                    f"GPIO{pin} belongs to this module itself -- its RAM "
                    f"or internal flash lives on it. Anything driven there "
                    f"crashes the board on every boot. Pick another pin.")
            # ONE DEVICE, ONE PIN PER JOB. Two slots of the same device on
            # one pin collided with nothing in the taken-pin map (the item
            # is not placed yet) and surfaced only at template time as
            # "two different jobs" -- or, for two instances of one family,
            # not at all until the board behaved strangely. Refused here,
            # by the slot that already holds the pin (audit, 2026-08-25).
            if pin in used_here:
                raise PlacementError(
                    f"GPIO{pin} was pointed at twice in one device -- its "
                    f"{used_here[pin]} already has it. Each connection "
                    f"needs its own pin.")
            used_here[pin] = slot.label.lower()
            severity, message = check(type_id, slot.key, pin, self.board)
            if blocks(severity):
                raise PlacementError(message)
            if severity == WONT_WORK and not anyway:
                raise PlacementError(message, WONT_WORK)
            if message:
                cautions.append(message)
            if slot.shared:
                # The check that matters is against the EXISTING bus, not
                # against the taken-pin map: naming two fresh pins for a
                # second bus collides with nothing, and would quietly give
                # the board an I²C bus its sensors are not wired to.
                existing = self._shared_pin(slot)
                if existing is not None and existing != pin:
                    # Named by its component: this path serves the SPI
                    # lines too, where "the I²C bus" sent an RC522's
                    # installer looking at the wrong wires.
                    raise PlacementError(
                        f"{slot.component.name} is already on "
                        f"GPIO{existing}. Everything on that bus shares "
                        f"the same pins.")
                if existing is not None:
                    continue
                # A FRESH bus on a taken pin falls through to the taken
                # check below. The old code skipped the taken check for
                # every shared slot on the grounds that joining a bus
                # reuses a pin -- true, but a fresh bus is not joining
                # anything, and quietly stacking it on a relay's pin gave
                # the relay and the bus one wire between them (audit,
                # 2026-08-25).
            if pin in held:
                raise PlacementError(f"GPIO{pin} is already used by "
                                     f"{held[pin]!r}.")
        self.items.append(Assignment(type_id, name, dict(pins_by_slot)))
        self._allocate()
        return cautions

    def remove(self, name: str) -> bool:
        before = len(self.items)
        self.items = [i for i in self.items
                      if i.name.strip().lower() != name.strip().lower()]
        if len(self.items) != before:
            self._allocate()
            return True
        return False

    # -- numbering -----------------------------------------------------
    def _allocate(self) -> None:
        """Give every slot its instance number within its component family.

        Redone from scratch on every change rather than incremented, so that
        removing the first relay renumbers the rest instead of leaving a hole
        that the next addition would fill with a surprise.
        """
        counters: Counter = Counter()
        for item in self.items:
            item.instances = {}
            for slot in BY_ID[item.type_id].slots:
                # Every rider of a shared bus is instance 1 of that bus.
                if not slot.shared:
                    counters[slot.component.code] += 1
                item.instances[slot.key] = (
                    1 if slot.shared else counters[slot.component.code])

    # -- output --------------------------------------------------------
    def template(self) -> Dict[str, object]:
        """The Template object Tasmota wants.

        BASE 1 is "ESP32-DevKit", the generic module of the ESP32 build --
        no inherited behaviour from some vendor's board, which is what we
        want for a board somebody wired themselves. (18 is Generic on the
        ESP8266 only; on an ESP32 it is not a module at all, and Tasmota's
        JsonTemplate stores it as base 18 anyway, an index into nothing.)
        FLAG is a pre-9.1 leftover and is always 0.

        UNASSIGNED IS NOT NEUTRAL. A slot left at 0 is a floating input,
        and a floating input with an old LED still wired to it glows --
        which is how the bedroom board haunted GPIO16 and 17 through every
        re-flash. So every pin that is safe to drive and has no job gets
        one anyway: Output Lo, actively held at ground. This lives HERE and
        not in some one-off console command precisely because the template
        is re-sent on every edit; whatever it carries is the state the
        board returns to, so it must carry the cure. Assigning the pin to
        a device on a later edit takes it back automatically -- devices are
        placed first, and grounding only fills what is still empty.

        The one exception is per-board, not per-chip: 16 and 17 are only
        grounded when THIS board is known not to keep them for itself --
        a conclusion the builder draws from the chip's own name (PICO:
        internal flash lives there) and its `Status 4` answer (WROVER:
        its RAM lives there). Until a board is known free, they are left
        alone. A floating pin is an annoyance; a template that bricks on
        boot is a house call.
        """
        order = self.board.template_order
        slots = [0] * len(order)
        position = {gpio: i for i, gpio in enumerate(order)}
        for item in self.items:
            for slot in BY_ID[item.type_id].slots:
                pin = item.pins[slot.key]
                code = slot.component.at(item.instances[slot.key])
                index = position[pin]
                if slots[index] and slots[index] != code:
                    raise ValueError(
                        f"GPIO{pin} was given two different jobs.")
                slots[index] = code
        grounded = self.board.ground_idle
        if self.owns_psram_pins is not False:   # True OR unasked: hands off
            grounded = grounded - self.board.psram
        for pin in grounded:
            index = position[pin]
            if not slots[index]:
                slots[index] = C_OUTPUT_LO.code
        # The flash pins keep the last word: whatever the sets above ever
        # come to contain, these slots leave this method as 0.
        for dead in (self.board.flash | self.board.absent) & set(order):
            slots[position[dead]] = 0
        return {"NAME": self.name[:60], "GPIO": slots, "FLAG": 0, "BASE": 1}

    def template_command(self) -> str:
        """`Template {...}` -- separators tightened because Tasmota's command
        buffer is finite and 36 numbers plus a name is most of it."""
        body = json.dumps(self.template(), separators=(",", ":"))
        return f"Template {body}"

    def activation_commands(self) -> List[str]:
        """The template, then the ONE command that applies it.

        Module 0 restarts the board -- that is what "apply" physically is
        -- and Tasmota ABANDONS anything queued behind it in the same
        Backlog. So nothing else is ever allowed in this list: the
        settings that used to ride here reported "sent" while the restart
        ate them, and bedroom's DisplayModel read back 0 (2026-08-24).
        """
        return [self.template_command(), "Module 0"]

    def configuration_commands(self) -> List[str]:
        """Everything the board is told AFTER it has come back from the
        Module 0 restart: per-device settings, then the friendly names.

        The caller owns the wait. This module can say what must come
        after the restart; only the wire knows when the restart is over.
        """
        out: List[str] = []
        shutters = 0
        for item in self.items:
            device = BY_ID[item.type_id]
            # A device may declare no slots at all (event-only).
            first = device.slots[0].key if device.slots else ""
            number = item.instances.get(first, 1)
            pin = item.pins.get(first, 0)
            # {n2}: the SECOND slot's instance, for setup that names a pair
            # -- an H-bridge's `Interlock {n},{n2}` is two relay numbers
            # belonging to one item, and {n}+1 would be a guess that broke
            # the day a board mixed this item with another relay device.
            second = (item.instances.get(device.slots[1].key, 1)
                      if len(device.slots) > 1 else number)
            # {sh}: this item's SHUTTER number, counted across every
            # shutter-declaring item on the board -- the second blind is
            # shutter 2, whatever relay numbers it landed on.
            declares_shutter = any("{sh}" in c for c in device.setup)
            if declares_shutter:
                shutters += 1
            for command in device.setup:
                out.append(command.replace("{n2}", str(second))
                                  .replace("{n}", str(number))
                                  .replace("{sh}", str(shutters))
                                  .replace("{pin}", str(pin)))
        out = self._merge_interlocks(out)
        out.extend(self.friendly_names())
        # GLOBAL SETTINGS ARRIVE ONCE. A per-device command carries {n} and
        # is unique by construction; a global one (SetOption114, which every
        # contact entry sends so that no assembly order can forget it) is
        # word-for-word identical each time. Eight contacts on a board would
        # otherwise spend eight of Backlog's thirty slots saying the same
        # thing, and Tasmota's queue is finite. dict.fromkeys keeps the first
        # of each, in order (audit, 2026-08-25).
        deduped = list(dict.fromkeys(out))
        # `DisplayModel <n>` makes Tasmota save and RESTART about a second
        # later, and a restart abandons whatever is still queued in the
        # Backlog. Placed where the LCD sits in the item list, it cut off
        # every device configured after it (switch modes, interlocks,
        # friendly names). It goes last, so nothing is behind it.
        model = [c for c in deduped if c.partition(" ")[0] == "DisplayModel"]
        return [c for c in deduped if c not in model] + model

    @staticmethod
    def _merge_interlocks(commands: List[str]) -> List[str]:
        """Fold every `Interlock a,b` group command into ONE.

        Tasmota's Interlock command REPLACES the whole group list each
        time it is sent -- it does not append. Two H-bridges each sending
        their own `Interlock x,y` therefore left only the second one
        protected, silently: the first motor's shoot-through guard was
        overwritten a millisecond after it was set (audit, 2026-08-25).
        The command accepts several space-separated groups at once, so
        the merge is the fix: one command carrying every pair. It lands
        where the FIRST group command stood, keeping its place before
        `Interlock ON`.
        """
        groups: List[str] = []
        merged: List[str] = []
        slot_index: Optional[int] = None
        for command in commands:
            found = re.match(r"Interlock ([\d,]+)$", command)
            if found:
                groups.append(found.group(1))
                if slot_index is None:
                    slot_index = len(merged)
                    merged.append("")          # placeholder, filled below
                continue
            merged.append(command)
        if slot_index is None:
            return commands
        merged[slot_index] = "Interlock " + " ".join(groups)
        return merged

    def friendly_names(self) -> List[str]:
        """Give each relay and light the name it was given on the pin screen.

        THIS IS WHAT MAKES AUTOMATIC REGISTRATION POSSIBLE, and it took a
        while to see. The board used to be told `FriendlyName1 <board name>`,
        so a board with four relays produced four Home Assistant entities all
        called some variant of "Kitchen" -- and the hub, seeing them appear,
        had no way to tell which one was the kettle. Somebody had to say so,
        by hand, which is the second trip through Add device that this
        removes.

        The name travels with the device instead. Relay 2 is told it is
        "kettle", so Home Assistant calls it kettle, and matching what
        appeared against what was placed is just a name comparison.

        ONLY RELAYS AND LIGHTS. Tasmota names sensors from their driver --
        there is no FriendlyName slot for a BME280 -- so those are renamed
        from the hub after their entities exist. Attempting it here would
        silently number a relay that is not there.
        """
        if self.naming_is_deferred():
            return []
        out: List[str] = []
        for item in self.items:
            device = BY_ID[item.type_id]
            if device.friendly_family != "friendly":
                continue
            # Tasmota has MAX_FRIENDLYNAMES = 8 slots; `FriendlyName9` is
            # not a command, so a ninth relay is named after its entity
            # appears, like a sensor.
            number = item.instances[device.slots[0].key]
            if number > 8:
                continue
            # A semicolon in a name would cut the Backlog line it rides in
            # -- Tasmota reads "; " as "next command" and keeps the
            # fragment. The builder refuses semicolons at the naming
            # question; this comma is the seatbelt for records written
            # before it did (audit, 2026-08-25).
            out.append(f"FriendlyName{number} {item.name.replace(';', ',')}")
        return out

    def naming_is_deferred(self) -> bool:
        """True when FriendlyName numbers cannot be predicted from here.

        A PWM light and a relay both want a FriendlyName slot, and Tasmota
        does NOT number them in the order this file allocates instances --
        the light takes the first slot and the relays shift up behind it.
        I could not confirm the exact rule from documentation, and the
        failure mode is the worst kind available: every command lands, the
        board reports success, and your kettle is labelled "ceiling light".

        So on a board that mixes the two, no names are sent at all and all
        of them are applied AFTER the entities appear, matched by what each
        one turns out to be. That path has to exist anyway -- sensors have
        no FriendlyName slot and have always needed it -- so this costs a
        second of latency and buys certainty.

        Boards with relays only, or lights only, are unambiguous and take
        the fast path.
        """
        families = set()
        for item in self.items:
            device = BY_ID[item.type_id]
            if device.friendly_family != "friendly":
                continue
            families.add("light" if device.slots[0].component is C_PWM
                         else "relay")
        return len(families) > 1

    def backlogs(self) -> Tuple[List[str], List[str]]:
        """(activation, configuration), each ready to send as-is.

        The activation half is never wrapped in a Backlog: the Template
        line is long and travels alone, and Module 0 travels alone because
        NOTHING may queue behind it -- its restart abandons the rest of
        the batch (bedroom's display settings, 2026-08-24). Configuration
        is chunked into Backlogs of thirty, Tasmota's per-Backlog limit,
        for sending only after the board has come back.

        This replaces the old single-stream backlog(): a method that
        produced the race as its normal output was not an API worth
        keeping around to reach for by accident (audit, 2026-08-25).
        """
        # AND BY LENGTH: a serial console line past Tasmota's input buffer
        # (800 bytes now, 520 on older builds) is dropped WHOLE with only a
        # "Serial buffer overrun" log line -- no Unknown/Error to catch --
        # and thirty long FriendlyNames get there.
        chunks: List[List[str]] = []
        for command in self.configuration_commands():
            if not chunks or len(chunks[-1]) == 30 or len(
                    "Backlog " + "; ".join(chunks[-1] + [command])) > 500:
                chunks.append([])
            chunks[-1].append(command)
        return (self.activation_commands(),
                ["Backlog " + "; ".join(chunk) for chunk in chunks])


def describe(board: Optional[Board] = None) -> Dict[str, Any]:
    """Everything the phone needs to draw a board and validate a tap.

    THE RULES ARE NOT COPIED, THE DATA IS. The app never learns that GPIO34
    is input-only or that ADC2 dies with Wi-Fi on; it learns that pin 34
    satisfies the roles ('in', 'adc') and that a relay's pin wants the role
    'out', and the intersection falls out. So there is one pin table in this
    project, in this file, and a Dart screen cannot drift from it -- the
    worst it can do is fail to redraw.

    Sent once when the pin screen opens. About 6KB.
    """
    board = board or chips.DEFAULT
    return {
        "board": board.id,
        # Which board is drawn, in words (an older app ignores it).
        "board_info": board.as_meta(),
        "roles": {
            "out": "can drive a load",
            "in": "can be read",
            "adc": "can measure a voltage",
            "touch": "has a touch channel",
            "bidir": "driven and read on one wire",
        },
        "pins": [
            {"n": info.number, "label": board.pin_label(info.number),
             "can": list(info.can), "purpose": info.purpose,
             "caution": info.caution, "severity": info.severity}
            for info in pins(board)
        ],
        # The board as drawn on the phone: silkscreen names, physical
        # order, power pins in their places. An older app ignores this and
        # falls back to two columns of numbers.
        "layout": {side: [{"label": label, "n": n} for label, n in column]
                   for side, column in board.drawing().items()},
        "groups": groups(),
        # The branches, in the order they are asked. The app renders these
        # rather than hard-coding "does / reads", so adding a third one day
        # is a change in this file only.
        #
        # `roles` is here for the same reason the pin table sends roles
        # instead of pin numbers: the app must not learn that GPIO34 cannot
        # drive a load. It intersects `roles` with the pin's `can` and the
        # answer falls out. `unavailable` is the sentence to show when that
        # intersection is empty -- wording, not a rule.
        "directions": [
            {"id": key,
             "label": DIRECTIONS[key],
             "roles": list(_DIRECTION_ROLES[key]),
             "hint": _DIRECTION_HINT[key],
             "unavailable": _DIRECTION_UNAVAILABLE[key]}
            for key in (DOES, READS, OTHER)
        ],
        "types": [
            {"id": d.id, "label": d.label, "group": d.group, "note": d.note,
             "direction": d.direction, "primary": d.primary,
             "short_label": d.short_label,
             # `family` / `limit`: the firmware family this line counts
             # against and how many of it one board may carry (PWM 16,
             # rotary 2, DHT 4, most one-offs 1). BoardPlan.add refuses
             # the one past the limit, so the phone counts too and says
             # so at placement instead of the device vanishing at Save.
             # A shared line counts once per board, never per rider; the
             # same code on two shared lines is one bus.
             "slots": [{"key": s.key, "label": s.label, "role": s.role,
                        "shared": s.shared, "family": s.component.code,
                        "family_name": s.component.name,
                        "limit": s.component.slots,
                        **({"default_pin": s.default_pin}
                           if s.default_pin is not None else {})}
                       for s in d.slots]}
            for d in CATALOGUE
        ],
    }


def groups() -> List[str]:
    return list(dict.fromkeys(device.group for device in CATALOGUE))
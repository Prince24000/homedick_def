"""hub/hub_gpio.py -- two LEDs and one button on the Pi's header, for a hub
that has no screen.

WHAT THEY MEAN. The FIRST LED is the hub's word on whether it is ready:

    off            ws_server is not running, or not yet listening
    solid          listening -- the stack is loaded and phones can connect
    two flashes    the button was pressed but not held (a nudge, no action)

It does NOT blink for the pairing window any more (her call, 2026-09-09:
"let only the phone LED be blinking"). The window is the phone LED's
story, told once, an inch away; two lights blinking at different rates
for one fact read as a fault.

The SECOND LED is the PHONES -- the door, and who came through it. It is
the one that answers "did that actually work?" while you are standing at
the hub with a phone in your hand, which one LED cannot say (her ask,
2026-09-04: once --pair runs and a connection opens, another LED lights):

    off            no phone connected, no window open
    slow blink     the window is OPEN and nobody has come through yet --
                   `setup_manager.py --pair`, the button, or Settings >
                   Pair a new phone. The window is a file, so it blinks
                   for whoever opened it, this process or another
    three flashes  a phone just PAIRED -- a key was cut, said once
    solid          at least one phone is CONNECTED to the hub right now

The sequence a person watches: run --pair, the second LED starts blinking;
the phone scans the code and connects, it flashes three times and stays
on; close the app or leave the network and it goes out. A phone that
simply reconnects later (already paired) lights it solid without the
flashes -- the flashes mean a new key, and only that.

The button, HELD for HOLD_SECONDS, opens the pairing window -- the same
door `setup_manager.py --pair` opens from the keyboard and Settings > Pair
a new phone opens from a paired phone -- and writes the pairing codes to
certs/pair.png (one per road the hub is on: pair-lan.png, pair-tailnet.png)
for whoever can fetch them, since a headless hub has no screen to show
one. A SHORT PRESS while the window is open CLOSES it early. Physically standing at the hub is
the trust `--pair` already assumes ("the console is the hub's own
keyboard"), so a button on its header is the same factor in a different
shape, and it is what makes recovery possible when the only paired phone
is gone and there is no keyboard. It does NOT run --broker or anything
else that rewrites configuration: a button can be leaned on.

WHY IT LIVES INSIDE ws_server rather than as its own script: the LED must
mean "ws_server is alive and listening", and the only process that knows
that is ws_server. Driven from here, a crash takes the LED down with it
(gpiozero releases the pin on exit) -- which is exactly the reading you
want. A separate blink loop would keep glowing over a dead hub.

OPTIONAL, ALWAYS. Off unless gpiozero imports and this is a Raspberry Pi
(or HOMEVOICE_GPIO=1 forces it); off entirely with HOMEVOICE_GPIO=0. On a
PC it prints one line and does nothing. Pins come from config.GPIO_LED_PIN
/ config.GPIO_BUTTON_PIN / config.GPIO_PAIR_LED_PIN (BCM numbering),
overridable by HOMEVOICE_LED_GPIO / HOMEVOICE_BUTTON_GPIO /
HOMEVOICE_PAIR_LED_GPIO. The second LED is OPTIONAL ON TOP OF THAT: a pin
of 0, a pin already taken, or one that will not open leaves a hub with
exactly the one LED it had before and nothing else changes.

On Bookworm / Pi 5 gpiozero needs lgpio:  sudo apt install python3-gpiozero
python3-lgpio   (or pip install gpiozero lgpio inside the venv).
"""
from __future__ import annotations

import asyncio
import os
from typing import Callable, Optional

HOLD_SECONDS = 2.0


def _is_pi() -> bool:
    try:
        with open("/proc/device-tree/model", "rb") as f:
            return b"Raspberry Pi" in f.read()
    except OSError:
        return False


def wanted() -> bool:
    flag = os.environ.get("HOMEVOICE_GPIO", "").strip()
    if flag == "0":
        return False
    if flag == "1":
        return True
    return _is_pi()


class HubPanel:
    """The LED and the button. Every method is safe to call when the
    hardware is absent: they simply do nothing."""

    def __init__(self, led_pin: int, button_pin: int,
                 on_hold: Callable[[], None],
                 loop: Optional[asyncio.AbstractEventLoop] = None,
                 pair_led_pin: int = 0,
                 on_cancel: Optional[Callable[[], None]] = None) -> None:
        self.led_pin = int(led_pin)
        self.button_pin = int(button_pin)
        # 0 means "no second LED" -- and so does a pin that collides with
        # one already spoken for, which would otherwise fight the first
        # LED or the button on the same wire.
        pair_led_pin = int(pair_led_pin or 0)
        if pair_led_pin in (self.led_pin, self.button_pin):
            pair_led_pin = 0
        self.pair_led_pin = pair_led_pin
        self._on_hold = on_hold
        self._on_cancel = on_cancel
        self._loop = loop
        self._led = None
        self._pair_led = None
        self._button = None
        self._ready = False
        self._pairing = False
        #: How many phones hold a live session right now. Not a bool: two
        #: phones connected and one leaving must not put the light out.
        self._phones = 0
        #: Whether the press currently in progress reached HOLD_SECONDS.
        #: Kept here because gpiozero offers nothing that answers it at
        #: release time: its `is_held` is cleared BEFORE when_released
        #: fires, and `was_held` is a name from its recipes that the
        #: application is expected to set itself -- it is not an attribute
        #: of Button, so reading it always gave False and a successful hold
        #: was followed by the two "no action" flashes on release.
        self._was_held = False

    # ── lifecycle ────────────────────────────────────────────────────────

    def start(self, say=print) -> bool:
        if not wanted():
            return False
        try:
            from gpiozero import LED, Button          # type: ignore
        except Exception as exc:                       # noqa: BLE001
            say(f"[GPIO] not driven: gpiozero unavailable ({exc}). "
                f"sudo apt install python3-gpiozero python3-lgpio")
            return False
        try:
            self._led = LED(self.led_pin)
            self._led.off()
            # Wired button -> GND, internal pull-up. bounce_time swallows
            # contact chatter; hold_time is the deliberate press.
            self._button = Button(self.button_pin, pull_up=True,
                                  bounce_time=0.05, hold_time=HOLD_SECONDS)
            self._button.when_pressed = self._pressed
            self._button.when_held = self._held
            self._button.when_released = self._released
        except Exception as exc:                       # noqa: BLE001
            say(f"[GPIO] not driven: {exc}")
            self.close()
            return False
        # THE SECOND LED FAILS ON ITS OWN. A pin that is taken, missing or
        # refused must cost the hub its phone light and nothing else -- the
        # readiness LED and the pairing button are the part you cannot run
        # a headless hub without.
        pair_note = ""
        if self.pair_led_pin:
            try:
                self._pair_led = LED(self.pair_led_pin)
                self._pair_led.off()
                pair_note = f", phone LED on GPIO{self.pair_led_pin}"
            except Exception as exc:                   # noqa: BLE001
                self._pair_led = None
                say(f"[GPIO] phone LED on GPIO{self.pair_led_pin} not driven: "
                    f"{exc} -- the hub LED and the button are unaffected")
        say(f"[GPIO] LED on GPIO{self.led_pin}, button on GPIO{self.button_pin}"
            f"{pair_note} (hold {HOLD_SECONDS:g}s to open pairing)")
        return True

    def close(self) -> None:
        for dev in (self._button, self._led, self._pair_led):
            try:
                if dev is not None:
                    if dev is not self._button:
                        dev.off()
                    dev.close()
            except Exception:                          # noqa: BLE001
                pass
        self._led = None
        self._pair_led = None
        self._button = None

    # ── what the LEDs say ────────────────────────────────────────────────

    def ready(self) -> None:
        """Listening: solid on (unless a pairing window is showing)."""
        self._ready = True
        self._show()

    def pairing(self, is_open: bool) -> None:
        """Called by the window watcher on every poll; only a change
        touches the LEDs, so this is free to call every two seconds."""
        if is_open == self._pairing:
            return
        self._pairing = is_open
        self._show_phones()          # the hub LED no longer shows the window

    def phones(self, count: int) -> None:
        """How many phones hold a live session, from ws_server's own set.

        Called on every connect and every disconnect. The COUNT is passed,
        not a change, so the light cannot drift out of step with the truth
        -- a dropped connection that never ran its cleanup would otherwise
        leave the LED on over an empty house.
        """
        count = max(0, int(count))
        if count == self._phones:
            return
        self._phones = count
        self._show_phones()

    def paired(self) -> None:
        """A NEW PHONE WAS PAIRED -- a key was cut. Three flashes, then
        back to whatever the phone LED was saying (solid, because the
        phone that just paired reconnects as a session a moment later).

        Said once, at the moment it happens. A phone that merely
        reconnects later gets no flashes: this is the light for "a key
        exists now that did not exist before", which is the one event in
        this system that should never pass unremarked.
        """
        led = self._pair_led
        if led is None:
            return
        try:
            led.blink(on_time=0.12, off_time=0.12, n=3, background=True)
            self._later(0.8, self._show_phones)
        except Exception:                              # noqa: BLE001
            pass

    def _show(self) -> None:
        """The hub LED: solid when listening, off when not. The pairing
        window no longer touches it -- that is the phone LED's job."""
        led = self._led
        if led is None:
            return
        try:
            if self._ready:
                led.on()
            else:
                led.off()
        except Exception:                              # noqa: BLE001
            pass

    def _show_phones(self) -> None:
        """The second LED. A CONNECTED PHONE OUTRANKS AN OPEN WINDOW: once
        somebody is through the door, "connected" is the more useful fact,
        and the first LED is still blinking to say the window is open."""
        led = self._pair_led
        if led is None:
            return
        try:
            if self._phones > 0:
                led.on()
            elif self._pairing:
                # Slow, so it reads differently from the hub LED's fast
                # blink an inch away on the same header.
                led.blink(on_time=0.5, off_time=0.5, background=True)
            else:
                led.off()
        except Exception:                              # noqa: BLE001
            pass

    @staticmethod
    def _later(seconds: float, show: Callable[[], None]) -> None:
        """blink(n=...) leaves the LED off when it finishes; put the light
        back to what it should be saying, without blocking the caller (a
        gpiozero callback thread, or the event loop)."""
        import threading
        threading.Timer(seconds, show).start()

    def _dispatch(self, action: Callable[[], None]) -> None:
        """gpiozero calls the button handlers on ITS thread; hand the action
        to the event loop, where the registry and the broadcast live."""
        loop = self._loop
        if loop is not None and loop.is_running():
            loop.call_soon_threadsafe(action)
        else:
            action()

    # ── the button ───────────────────────────────────────────────────────

    def _pressed(self) -> None:
        # Every press starts out as "not held"; _held changes that if the
        # finger stays down for HOLD_SECONDS.
        self._was_held = False

    def _held(self) -> None:
        # gpiozero calls this on ITS thread; hand the action to the event
        # loop, where the registry and the broadcast live.
        self._was_held = True
        self._dispatch(self._on_hold)

    def _released(self) -> None:
        # A press that was not held. WHILE THE WINDOW IS OPEN it closes it
        # (her ask, 2026-09-09: a way to cancel without waiting five
        # minutes) -- the same finger that opened the door shuts it. With
        # no window open it is a nudge: two quick flashes, no action. A
        # release that ENDS a hold gets no flashes: the pairing window it
        # opened is the acknowledgement, and two flashes here would say
        # the opposite.
        btn = self._button
        if btn is None or self._was_held:
            return
        if self._pairing and self._on_cancel is not None:
            self._dispatch(self._on_cancel)
            # No flashes: the phone LED going out IS the acknowledgement.
            return
        led = self._led
        if led is None:
            return
        try:
            led.blink(on_time=0.08, off_time=0.08, n=2, background=True)
            self._later(0.5, self._show)
        except Exception:                              # noqa: BLE001
            pass
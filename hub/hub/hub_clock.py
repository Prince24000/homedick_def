"""hub/hub_clock.py -- how the hub gets its clock back after a power cut, with
no internet and no battery-backed clock.

THE PROBLEM. A Raspberry Pi has no real-time clock. When the power goes, the
clock stops; when it comes back, Raspberry Pi OS restores the time it last
saved (fake-hwclock, once an hour), so the hub wakes up BEHIND by roughly
the length of the outage. Nothing corrects that without an internet time
server -- and a hub on a router with no data has none. The pairing
handshake refuses any phone whose timestamp is more than AUTH_MAX_SKEW
(300 s) from the hub's own clock, so after a two-hour outage every phone is
refused with "stale_timestamp" until somebody sets the time by hand.

THE FIX. Borrow the time from the first PAIRED phone. A phone has a battery
and network time. The hub can tell its own clock is wrong by two signs:

  * the current time is EARLIER than a time it has already seen (a file
    this module writes every minute while running). Time does not run
    backwards, so a clock that has is a clock that lost power;
  * the operating system itself says the clock is UNSYNCHRONISED -- no
    time server has set it since boot -- and this machine has no
    battery-backed clock (/dev/rtc0 is absent, as on a Pi 4). Such a
    clock is only ever as right as the moment it was last switched off.

The second sign was added on 2026-09-07, after the first one missed the
commonest case in the field. A CLEAN shutdown saves the time as it stops,
so the next boot resumes from that very moment: the clock has not gone
backwards, it has PAUSED, for exactly as long as the hub was off, and the
first test cannot see a pause. Nor can it see anything on a brand-new hub,
which has no saved time to compare against, so a fresh Pi on a hotspot
refused its very first phone with "stale_timestamp". The OS's own verdict
covers both: a hub that has never been told the time is suspect from its
first second. While the clock is suspect:

  * the skew check TOLERATES a phone timestamp it would otherwise refuse --
    but only one that is plausible: no earlier than the last time the hub
    saw (or than FLOOR, the date this software was written, on a hub that
    has never saved a time), and not more than MAX_AHEAD beyond the later
    of that and the hub's own clock;
  * a phone that then proves itself with its Ed25519 signature -- the same
    proof that lets it operate the house -- has its timestamp ADOPTED as the
    hub's clock, and the check goes back to normal.

WHY THIS IS SAFE. The handshake's defence against replay is the nonce: the
hub issues a fresh one per connection and the phone signs it, so a captured
auth frame is useless on any other connection whatever its timestamp says.
The skew window is a second fence on top of that, and this module lowers
that second fence only while the hub KNOWS its own clock is wrong, only for
a plausible time, and raises it again the moment a key-holding phone has
supplied a signed time. A phone that can set the clock is a phone that can
already open the front door; this grants it nothing new. An unpaired phone
(the HMAC pairing path) is let through the lowered fence too -- otherwise a
house whose only paired phone was lost could never pair another after an
outage -- but it never sets the clock: adopting a time is a paired phone's
privilege.

SETTING THE SYSTEM CLOCK needs a privilege the hub does not otherwise
have. Two ways, tried in that order by _set_system_clock:

  1. CAP_SYS_TIME. When the hub runs as the systemd service that
     `setup_manager.py --service` installs, the unit grants the process
     that ONE capability (AmbientCapabilities=CAP_SYS_TIME, bounded by
     CapabilityBoundingSet) and the hub calls clock_settime itself. No
     sudo, no wrapper, nothing on disk that gives the user's shell any
     power at all. This is the intended way on a Pi.
  2. The wrapper, for a hub started by hand from a terminal. It calls
     `sudo -n /usr/local/bin/homevoice-setclock <seconds>` -- a five-line
     script that accepts ONE integer, checks it is a plausible year, and
     runs `date -u -s @<seconds>`. The sudoers line names only that wrapper:

    homevoice ALL=(root) NOPASSWD: /usr/local/bin/homevoice-setclock

(replace `homevoice` with the user the hub runs as). NOT `date` itself:
a rule that let this user run /usr/bin/date as root with any arguments
would also let `date -f /etc/shadow` read root-only files back through
its error messages, and let any process running as this user set the
clock to anything. The wrapper can do one thing. Install both with
`python3 setup_manager.py --clock-sudo`, or by hand:

    sudo install -m 755 hub/homevoice-setclock /usr/local/bin/homevoice-setclock
    echo "$USER ALL=(root) NOPASSWD: /usr/local/bin/homevoice-setclock" \
        | sudo tee /etc/sudoers.d/homevoice-clock
    sudo chmod 440 /etc/sudoers.d/homevoice-clock

Without it the module says so once, keeps tolerating the skew so phones
can still connect, and leaves the clock as it is -- schedules and readings
stay on the wrong time until the wrapper exists or NTP reaches the hub.

The saved-time file is DATA_DIR/clock_last_seen: one number, epoch seconds,
rewritten every SAVE_EVERY seconds while the hub runs and never while the
clock is suspect (writing a wrong time would teach the next boot the wrong
lesson).
"""
from __future__ import annotations

import asyncio
import os
import subprocess
import time
from typing import Callable, Optional

SAVE_EVERY = 60.0
#: A phone time this far beyond the last time the hub saw is refused: a hub
#: does not sit unpowered for a year, and a phone with a broken clock must
#: not be able to throw the hub's clock into the far future.
MAX_AHEAD = 365 * 24 * 3600.0
#: Slack on the "went backwards" test, so a save racing a shutdown does not
#: mark a healthy clock suspect.
BACKWARDS_SLACK = 5.0
#: No phone time earlier than this is ever believed: the date this module
#: was written. It is the lower bound on a hub that has never saved a time
#: (nothing to compare against) and a floor under every other one -- a phone
#: reporting 1990 is a phone with a dead battery, not a time machine.
FLOOR = 1_767_225_600.0            # 2026-01-01 00:00:00 UTC


def _fmt(ts: float) -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts))


def _os_trusts_clock() -> Optional[bool]:
    """Does the operating system vouch for its own clock?

    True  -- a time server has synchronised it since boot, or this machine
             has a battery-backed clock (/dev/rtc0) that kept time while
             off, so a stale clock is not the expected state.
    False -- unsynchronised AND no hardware clock: the Pi 4 case. The time
             is whatever was saved at the last shutdown, i.e. paused.
    None  -- cannot tell (no systemd's timedatectl: Windows, a container).
             Treated as "no opinion"; only the went-backwards test applies.
    """
    if os.path.exists("/dev/rtc0") or os.path.exists("/sys/class/rtc/rtc0"):
        return True
    # `timedatectl show` is systemd 239+ (2018); every Raspberry Pi OS this
    # hub supports has it, so the old fallback that scraped the human
    # `timedatectl status` text for a line saying "synchronized" is gone.
    try:
        done = subprocess.run(
            ["timedatectl", "show", "-p", "NTPSynchronized", "--value"],
            capture_output=True, text=True, timeout=5)
    except Exception:                                  # noqa: BLE001
        return None
    out = (done.stdout or "").strip().lower()
    return out == "yes" if done.returncode == 0 and out in ("yes", "no") else None


def _forced() -> bool:
    """HOMEVOICE_CLOCK_SUSPECT=1: treat the clock as suspect, for a test."""
    return os.environ.get("HOMEVOICE_CLOCK_SUSPECT", "") == "1"


#: The one command this module may run as root. See the module docstring
#: for why it is a wrapper and not /usr/bin/date.
SETCLOCK = "/usr/local/bin/homevoice-setclock"


def _set_system_clock(ts: int) -> bool:
    """Set the OS clock. True on success. Never raises.

    TWO WAYS, tried in order. First directly: a process holding the
    CAP_SYS_TIME capability may call clock_settime itself -- which is how
    the hub runs under systemd (`setup_manager.py --service` writes
    AmbientCapabilities=CAP_SYS_TIME into the unit), with no sudo, no
    wrapper and nothing on disk granting anything to the user's shell.
    Second, for a hub started by hand from a terminal, which has no such
    capability: the one-purpose wrapper under its sudoers rule.
    """
    try:
        time.clock_settime(time.CLOCK_REALTIME, float(int(ts)))
        return True
    except Exception:                                  # noqa: BLE001
        pass                                           # EPERM: no CAP_SYS_TIME
    try:
        done = subprocess.run(
            ["sudo", "-n", SETCLOCK, str(int(ts))],
            capture_output=True, text=True, timeout=10)
        return done.returncode == 0
    except Exception:                                  # noqa: BLE001
        return False


class ClockGuard:
    def __init__(self, path: str, say: Callable[[str], None] = print,
                 set_clock: Callable[[int], bool] = _set_system_clock,
                 now: Callable[[], float] = time.time,
                 os_trusts_clock: Callable[[], Optional[bool]] = _os_trusts_clock) -> None:
        self.path = path
        self._say = say
        self._set_clock = set_clock
        self._now = now
        self._os_trusts_clock = os_trusts_clock
        self.last_seen: float = self._read()
        self.suspect: bool = False
        self.reason: str = ""
        self._warned_no_root = False
        self.check()

    # ── the saved time ───────────────────────────────────────────────────

    def _read(self) -> float:
        try:
            with open(self.path, encoding="utf-8") as f:
                return float(f.read().strip() or 0)
        except (OSError, ValueError):
            return 0.0

    def note_now(self) -> None:
        """Remember the current time -- only while it is believed."""
        if self.suspect:
            return
        now = self._now()
        if now < self.last_seen:
            return
        self.last_seen = now
        try:
            os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                f.write(f"{int(now)}\n")
            os.replace(tmp, self.path)
        except OSError:
            pass

    # ── the verdict ──────────────────────────────────────────────────────

    def check(self) -> bool:
        """Is the clock suspect? Time cannot run backwards; a clock that has
        lost power. HOMEVOICE_CLOCK_SUSPECT=1 forces it, for a test."""
        now = self._now()
        if _forced():
            self.suspect, self.reason = True, "forced by HOMEVOICE_CLOCK_SUSPECT=1"
        elif self.last_seen and now < self.last_seen - BACKWARDS_SLACK:
            self.suspect = True
            self.reason = (f"the clock went backwards: it is {_fmt(now)} now but "
                           f"the hub last saw {_fmt(self.last_seen)}")
        elif now < FLOOR:
            # BEFORE THIS MODULE WAS WRITTEN, so the clock is wrong no
            # matter who says otherwise (2026-09-14). This branch sits
            # ABOVE the OS question on purpose: a Pi with no RTC can come
            # up at the epoch with systemd-timesyncd reporting
            # "synchronised" from a stale stamp file, and the OS's word
            # was the only thing consulted -- so a hub sitting in 1970 was
            # NOT suspect, the strict skew check applied, and no phone
            # could reach it to set the time. FLOOR is the one date this
            # module can assert from its own existence.
            self.suspect = True
            self.reason = (f"the clock reads {_fmt(now)}, which is before this "
                           f"hub's software was written -- it has never been "
                           f"set")
        elif self._os_trusts_clock() is False:
            # PAUSED, not backwards: a clean shutdown saved the time as it
            # stopped and the boot resumed from there -- or a hub that has
            # never been told the time at all. The OS knows it has not
            # been synchronised and there is no hardware clock to trust.
            self.suspect = True
            self.reason = (f"the system clock is unsynchronised and this machine "
                           f"has no hardware clock, so {_fmt(now)} is only the "
                           f"moment it was last switched off")
        if self.suspect:
            self._say(f"[CLOCK] {self.reason} -- no time server has set it. "
                      f"Phone timestamps outside the skew window are tolerated "
                      f"until a paired phone supplies the time.")
        return self.suspect

    def plausible(self, ts: float) -> bool:
        """A time the hub could believe: not before what it already saw (nor
        before FLOOR), and not absurdly far ahead of the later of that and
        its own clock -- a paused clock is behind, so the bound is taken
        from whichever of the two is later.

        FLOOR IS IN THE UPPER BOUND TOO, and leaving it out deadlocked the
        hub (2026-09-14: "i hope the hmac wont be problem because theres no
        clock for pi as we previously saw this issue").

        The ceiling is built from what the hub knows about the time -- and
        when the hub knows NOTHING, the old ceiling was built out of that
        nothing. A machine with no saved time and a clock at the epoch gave
        `upper = max(0, ~0) + MAX_AHEAD`, a date in 1971, so a phone's
        perfectly genuine 2026 timestamp was "absurdly far ahead" and was
        refused. And the hub can only learn the time FROM a phone -- so a
        fresh install, a wiped SD card, or a Pi whose fake-hwclock file has
        not been written yet could never pair its FIRST phone. A deadlock,
        and one that always-open pairing makes easier to walk into rather
        than harder. Measured, not reasoned:

            no saved time, clock at the epoch  ->  plausible(2026) False
            hub unpowered for two years        ->  plausible(2026) False
            hub unpowered for two months       ->  True  (the normal case)

        FLOOR is the date this module was written, so `FLOOR + MAX_AHEAD`
        is still a hard ceiling: a phone claiming 2050 is still refused,
        which is the whole point of MAX_AHEAD. FLOOR only takes over when
        both other candidates are nonsense.
        """
        lower = max(FLOOR, self.last_seen - BACKWARDS_SLACK)
        upper = max(self.last_seen, self._now(), FLOOR) + MAX_AHEAD
        return lower <= ts <= upper

    def recheck(self) -> None:
        """Called by the minute hand. A clock the OS has since synchronised
        (NTP reached the hub after the guard was built) stops being suspect
        without waiting for a phone."""
        if not self.suspect:
            return
        # THE OS'S WORD DOES NOT OVERRULE FLOOR. check() ranks the FLOOR
        # test ABOVE it on purpose -- timesyncd can report "synchronised"
        # from a stale stamp while the clock reads 1970 -- so that verdict
        # must not be cleared by the very answer it overrules (it was, on
        # the first tick of run()). Nor is a forced test.
        # Nor does it overrule "went backwards", which check() also ranks
        # above it: on a machine with /dev/rtc0 (a Pi 5, battery or not)
        # the OS always "trusts" its clock, so that verdict was cleared on
        # the first tick while the clock still read earlier than a time the
        # hub had already seen -- and phones went back to the strict check
        # against a clock known to be wrong.
        now = self._now()
        if _forced() or now < FLOOR or now < self.last_seen - BACKWARDS_SLACK:
            return
        if self._os_trusts_clock() is True:
            self.suspect, self.reason = False, ""
            self._say("[CLOCK] the system clock is synchronised now; back to the normal skew check")

    def tolerates(self, ts: float) -> bool:
        """Should a timestamp outside the skew window be let through? Only
        while suspect, and only a plausible one."""
        return self.suspect and self.plausible(ts)

    # ── adopting a phone's time ──────────────────────────────────────────

    def adopt(self, ts: int, source: str) -> bool:
        """A PAIRED phone proved itself with this timestamp in its signed
        string; make it the hub's clock. True when the OS clock was set."""
        if not self.suspect:
            return False
        if not self.plausible(ts):
            self._say(f"[CLOCK] {source} offered {_fmt(ts)}, which is not a time "
                      f"this hub can believe (last seen {_fmt(self.last_seen)}) -- ignored")
            return False
        before = self._now()
        if self._set_clock(int(ts)):
            self.suspect, self.reason = False, ""
            self.last_seen = float(ts)
            self.note_now()
            self._say(f"[CLOCK] set from {source}: {_fmt(ts)} (was {_fmt(before)}, "
                      f"{(ts - before) / 3600:.1f} h behind)")
            return True
        if not self._warned_no_root:
            self._warned_no_root = True
            self._say(f"[CLOCK] {source} says it is {_fmt(ts)} but this process may not "
                      f"set the system clock. Install the wrapper and its sudoers "
                      f"line once:  python3 setup_manager.py --clock-sudo\n"
                      f"    Phones can still connect; schedules and readings keep "
                      f"the wrong time until then.")
        return False

    # ── the minute hand ──────────────────────────────────────────────────

    async def run(self) -> None:
        """Keep the saved time current for as long as the hub runs."""
        while True:
            # recheck() may run timedatectl (up to two 5 s subprocesses):
            # off the event loop, which is serving every phone.
            await asyncio.to_thread(self.recheck)
            self.note_now()
            await asyncio.sleep(SAVE_EVERY)
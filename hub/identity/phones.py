"""The phones allowed to drive this hub, and the window in which new ones
may enroll.

WHY THIS EXISTS
---------------
Authentication used to be one shared secret. Every phone signed the server's
nonce with the same `HOMEVOICE_API_KEY`, which worked, but meant there was
no such thing as "this phone" -- only "someone who knows the key". Removing
one phone meant changing the key and then re-entering it on every other
phone in the house.

Now each phone generates an Ed25519 keypair, keeps the private half, and
registers only the public half here. Three things follow from that:

  * NOTHING IN THIS FILE IS SECRET. Public keys are public. The file is
    still written 0600 because the list of devices in someone's house is
    private even when the keys are not, but losing it does not let anyone
    in. Contrast the old design, where the hub had to hold a secret that
    could impersonate any phone.

  * REVOKING IS DELETING ONE LINE. No other phone notices.

  * THE HUB CANNOT FORGE A PHONE. It has no phone's private key. That is
    worth saying plainly because it is the property the shared secret could
    never have.

THE ENROLLMENT WINDOW
---------------------
The API key still exists and is still needed -- it is how a phone with no
keypair yet proves it is allowed to register one. But if knowing the API key
were *sufficient*, revocation would be theatre: a removed phone still knows
the key and would simply enroll itself again.

So enrollment is refused unless the hub is in pairing mode, which is opened
deliberately and closes on its own. The one exception is a hub with no
phones registered at all, which cannot be protecting anything yet and so
accepts the first phone to ask -- that is what makes recovery possible when
every phone has been lost and there is no laptop to hand.

Pairing mode lives in a FILE beside the registry (PairingWindow), because
setup_manager and ws_server are different processes. NOTE that ws_server
opens it for PAIRING_WINDOW_SECONDS at boot only when
HOMEVOICE_PAIR_ON_BOOT=1 (off since 2026-10-15: a crash-restart must not
open a door nobody chose to open).
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import time
from typing import Any, Dict, List, Optional

from core import config
from core.locked_store import (StoreLock, atomic_write_json,
                                         fingerprint, quarantine)


# How long a deliberately-opened pairing window stays open. Long enough to
# walk to another room and type a key; short enough that leaving it open by
# accident is not a standing invitation.
#
# FIVE MINUTES, up from two (her call, 2026-08-31). Two was measured
# against typing a key you had already found. It is short against walking
# to the hub, reading a long random string off paper, and getting it into a
# phone without a mistake. The loosening is smaller than it looks: an open
# window is only an opportunity to TRY -- enrolling inside it still needs
# the HMAC of the API key, so five minutes of open door is not five minutes
# of access.
#
# NOTHING ELSE SPELLS THIS OUT. Five sentences used to type "two minutes"
# in prose -- three in ws_server, one in main, one in the app's settings
# row -- and a constant that five sentences can contradict is five bugs
# waiting for the day it changes. Which is today.
PAIRING_WINDOW_SECONDS = 300

#: How long a pairing ASKED FOR while the hub had no address waits for one
#: (PairingWindow.ask). Past this the ask lapses, and pairing is asked again.
PAIRING_WAIT_SECONDS = 15 * 60


def _pairing_always_open() -> bool:
    """Is this hub configured to keep the pairing door open permanently?

    READ AT CALL TIME, never bound at import. This module is imported by
    ws_server at start-up and by setup_manager in a different process; a
    value captured at import would have frozen whichever config that
    process happened to see, and turning the setting back OFF would then
    need a restart to take effect. A security control that needs a reboot
    to tighten is not a control.

    `identity.phones` must stay importable on its own (setup_manager
    reaches it before the environment is fully built): core.config is
    standard library only, so importing it here is safe.
    """
    return bool(config.PAIRING_ALWAYS_OPEN)


def window_words() -> str:
    """The window as a phrase: "five minutes", "90 seconds".

    So a sentence can name the duration without keeping its own copy.
    """
    seconds = PAIRING_WINDOW_SECONDS
    if seconds % 60:
        return f"{seconds} seconds"
    minutes = seconds // 60
    names = {1: "one", 2: "two", 3: "three", 4: "four", 5: "five",
             6: "six", 10: "ten", 15: "fifteen", 30: "thirty"}
    return f"{names.get(minutes, minutes)} minute{'' if minutes == 1 else 's'}"

#: Names the app supplies when the person typed none. Not usable as a
#: handset identity -- see purge_stale_siblings.
GENERIC_PHONE_NAMES = frozenset({"my phone", "unnamed phone"})


def _phones_file() -> str:
    return str(config.PHONES_FILE)


def _boards_dir() -> str:
    return str(config.BOARDS_DIR)


def adopt_legacy_file(target: str, legacy: str) -> bool:
    """Move a registry from where it should never have been to `target`.

    The first version of this module parked phones.json INSIDE the boards
    directory, because the folder already existed with the right
    permissions. But that folder is a REGISTRY, not storage: every *.json
    in it is listed as a board, so the phones file appeared in every board
    picker as a board called "phones" -- and "wiping" it crashed on the
    MQTT credentials it never had. The file lives one level up now; this
    brings an existing installation's registrations along, so the fix does
    not cost anyone a second pairing ceremony.

    Refuses unless the legacy file exists and the target does not -- a
    registry that already moved is never overwritten by a stale leftover.
    A failed move leaves the legacy file untouched; the next start simply
    tries again.
    """
    if os.path.abspath(legacy) == os.path.abspath(target):
        return False
    if not os.path.exists(legacy) or os.path.exists(target):
        return False
    try:
        os.makedirs(os.path.dirname(target) or ".", exist_ok=True)
        os.replace(legacy, target)
        return True
    except OSError:
        return False


def _adopt_legacy_default(target: str) -> None:
    """Run the migration -- but ONLY for the default installation layout.

    Deliberately inert when HOMEVOICE_PHONES_FILE points somewhere custom:
    the test suite points it at a temp directory, and an unguarded version
    of this would have MOVED a real installation's registry into that temp
    directory -- where the test teardown deletes it. Running the tests must
    never be able to eat the registrations of the machine they run on.
    """
    boards = _boards_dir()
    expected = os.path.join(
        os.path.dirname(boards.rstrip("/\\")) or ".", "phones.json")
    if os.path.abspath(target) != os.path.abspath(expected):
        return
    adopt_legacy_file(target, os.path.join(boards, "phones.json"))
    # A pairing window some earlier run left open at the old address is
    # ephemeral state nothing will ever read again: sweep, best effort.
    try:
        os.unlink(os.path.join(boards, "pairing-open.json"))
    except OSError:
        pass


_spki_cache: Dict[str, str] = {}


def spki_fingerprint(cert_path: str) -> str:
    """SHA-256 of the public key inside a certificate, hex.

    THE POINT OF THIS VALUE IS THAT IT IS NEVER TRANSMITTED. Both ends put
    it into the string they sign -- the phone using the key out of the
    certificate it was actually shown, the hub using the key it actually
    holds. Anyone relaying in the middle is presenting a different
    certificate, so the two strings differ and the proof fails, and he
    cannot correct for it without a key whose hash he does not control.

    It hashes the SubjectPublicKeyInfo rather than the certificate bytes so
    that renewing the certificate with the SAME key does not invalidate
    anything. That is what lets a certificate expire and be reissued without
    every phone in the house needing to pair again.

    Lives here rather than in ws_server so it can be tested without starting
    a server, and so the app-facing and hub-facing halves of "what is this
    hub's identity" sit in one file.
    """
    cached = _spki_cache.get(cert_path)
    if cached:
        return cached
    from cryptography import x509
    from cryptography.hazmat.primitives import serialization
    with open(cert_path, "rb") as f:
        cert = x509.load_pem_x509_certificate(f.read())
    der = cert.public_key().public_bytes(
        encoding=serialization.Encoding.DER,
        format=serialization.PublicFormat.SubjectPublicKeyInfo)
    digest = hashlib.sha256(der).hexdigest()
    _spki_cache[cert_path] = digest
    return digest


def signed_message(nonce: str, timestamp: int, spki: str) -> str:
    """THE ONE STRING both ends sign, written down in ONE place.

    Server and phone previously each built this by hand, which is fine
    until someone changes the separator on one side and the failure is a
    silent "wrong secret" that sends you looking at the key.
    """
    return f"{nonce}:{timestamp}:{spki}"


def _pairing_file() -> str:
    """Beside the phone records. Deliberately a separate file: it is
    ephemeral state that gets deleted routinely, and mixing it into the
    file that holds the actual registrations would mean rewriting that
    file every time a window opens or closes."""
    return os.path.join(os.path.dirname(_phones_file()) or ".",
                        "pairing-open.json")


def phone_id_for(public_key_b64: str) -> str:
    """A phone's id is derived FROM its key, not assigned to it.

    Re-registering the same phone therefore lands on the same record and
    updates it, instead of quietly accumulating a second entry for a device
    the user thinks they only paired once. It also means an id cannot be
    claimed by a phone that does not hold the matching private key.
    """
    raw = base64.b64decode(public_key_b64, validate=True)
    return hashlib.sha256(raw).hexdigest()[:16]


def short_fingerprint(public_key_b64: str) -> str:
    """Eight hex characters, grouped, for reading aloud or comparing on a
    screen. Not a security boundary on its own -- it is there so a person
    approving a pairing can see they are approving the device in their hand
    and not one in the next flat."""
    ident = phone_id_for(public_key_b64)
    return f"{ident[:4]}-{ident[4:8]}"


class PairingWindow:
    """Is the hub currently willing to enroll a new phone?

    ON DISK, NOT IN MEMORY, and that is not an implementation detail. The
    person opening the window is at a terminal running setup_manager; the
    process that has to honour it is ws_server, which is a DIFFERENT
    PROCESS, usually started hours earlier. An in-memory flag would have
    been set in one and read in the other, so `--pair` would have appeared
    to work and then silently refused every phone.

    What is stored is an ABSOLUTE expiry, not a duration, so the window
    still closes on time across a restart, a clock read, or a crash. A
    missing file, an unreadable file and a corrupt file all mean closed --
    every failure falls shut.
    """

    def __init__(self, path: Optional[str] = None) -> None:
        self.path = path or _pairing_file()

    #: Who opened the window. Stored beside the expiry because the phones
    #: treat the openers differently: a window somebody opened ON PURPOSE
    #: (a phone's menu action, --pair at the hub's keyboard) is shown as a
    #: countdown on every paired phone -- it is confirmation that the door
    #: they asked for is open. The BOOT window is not: it opens on every
    #: restart of the hub, its whole purpose is the house whose only phone
    #: is dead, and to a phone that is alive and paired it is a two-minute
    #: timer for nothing. That phone learned nothing from it except to ask
    #: "why is there a timer for adding devices?", which is the question
    #: this field exists to stop.
    OPENED_BY_BOOT = "boot"
    OPENED_BY_MENU = "menu"
    OPENED_BY_HUB = "hub"
    #: Not a window at all: `config.PAIRING_ALWAYS_OPEN` is set, so the door
    #: simply does not close. Its own value because a phone must be able to
    #: tell it from a window that is about to expire -- there is no
    #: countdown to draw, and drawing one from `seconds_left()` (which is 0
    #: here, correctly) would read as "closed" or as a stuck timer.
    OPENED_BY_ALWAYS = "always"

    #: How far the wall clock may appear to run backwards past `opened_at`
    #: before the window is treated as shut (see _open_until).
    _BACKWARDS_SLACK = 5.0

    def open(self, seconds: int = PAIRING_WINDOW_SECONDS,
             opened_by: str = OPENED_BY_HUB, from_away: bool = False) -> float:
        """`from_away`: THIS window may take a phone over Tailscale (her
        design, 2026-10-15: the choice belongs to Pair a new phone, not to
        a standing switch). Every other window -- the button, --pair, the
        boot window -- is for a phone at home."""
        now = time.time()
        until = now + seconds
        # Written whole and renamed into place: the reader is another
        # process, and a half-written file must never be what it sees.
        atomic_write_json(self.path, {"open_until": until, "opened_at": now,
                                      "opened_by": opened_by,
                                      "from_away": bool(from_away)})
        return until

    def ask(self, seconds: int = PAIRING_WINDOW_SECONDS,
            opened_by: str = OPENED_BY_HUB, from_away: bool = False) -> float:
        """ASKED FOR, BUT THE HUB HAS NO ADDRESS YET (her rule, 2026-10-19:
        "pairing should start once address is available"). Nothing is open:
        the countdown would run down while no phone could reach the hub.
        The ask waits on disk -- ws_server's watcher opens the window, full
        length, the moment an address appears (or --pair's console does).
        It lapses after PAIRING_WAIT_SECONDS, so a hotspot switched on
        hours later does not open a door nobody is standing at. Returns
        when the ask lapses."""
        now = time.time()
        atomic_write_json(self.path, {"pending_until": now + PAIRING_WAIT_SECONDS,
                                      "asked_at": now, "seconds": int(seconds),
                                      "opened_by": opened_by,
                                      "from_away": bool(from_away)})
        return now + PAIRING_WAIT_SECONDS

    def pending(self) -> Optional[Dict[str, Any]]:
        """The waiting ask (see ask), or None: none, lapsed, or the clock
        ran backwards past it (fails shut, like the window)."""
        data = self._read()
        try:
            until = float(data.get("pending_until") or 0.0)
            asked = float(data.get("asked_at") or 0.0)
        except (TypeError, ValueError):
            return None
        now = time.time()
        if not until or now >= until or now < asked - self._BACKWARDS_SLACK:
            return None
        return data

    def open_asked(self) -> Optional[float]:
        """Open the waiting ask now, full length, as it was asked. None
        when nothing is waiting."""
        ask = self.pending()
        if ask is None:
            return None
        return self.open(int(ask.get("seconds") or PAIRING_WINDOW_SECONDS),
                         opened_by=str(ask.get("opened_by") or self.OPENED_BY_HUB),
                         from_away=ask.get("from_away") is True)

    def allows_away(self) -> bool:
        """Is a window open that may take a new phone from away? Shut,
        unreadable or not asked for: no."""
        return self.is_open() and self._read().get("from_away") is True

    def close(self) -> None:
        try:
            os.unlink(self.path)
        except OSError:
            pass

    def _read(self) -> Dict[str, Any]:
        try:
            with open(self.path, encoding="utf-8") as f:
                data = json.load(f)
            return data if isinstance(data, dict) else {}
        except Exception:                              # noqa: BLE001
            return {}                                  # fail shut, always

    def _open_until(self) -> float:
        data = self._read()
        try:
            until = float(data.get("open_until") or 0.0)
            opened_at = float(data.get("opened_at") or 0.0)
        except (TypeError, ValueError):
            return 0.0
        # The expiry is wall-clock time, and a Pi's wall clock JUMPS (no
        # RTC; NTP or hub_clock correcting it). A window opened while the
        # clock read ahead would stay open for as long as the clock had
        # been wrong once it is set back. The clock now reading earlier
        # than the moment the window was opened means exactly that: shut.
        if time.time() < opened_at - self._BACKWARDS_SLACK:
            return 0.0
        return until

    def is_open(self) -> bool:
        # ALWAYS-OPEN IS NOT A WINDOW WITH A LONG EXPIRY, and writing one
        # would have been the easy version of this: a far-future timestamp
        # in the file survives turning the setting back off, which is the
        # one thing a security control must never do. The setting is asked
        # every time instead, so switching it off shuts the door on the
        # next call with nothing to clean up.
        return _pairing_always_open() or time.time() < self._open_until()

    def seconds_left(self) -> int:
        """How long this window has to run -- 0 when it never ends.

        Zero is the honest answer for an always-open hub: there is no time
        remaining because nothing is running out. Callers that draw a
        countdown must gate on `is_open()`, not on this being positive.
        """
        if _pairing_always_open():
            return 0
        return max(0, int(self._open_until() - time.time()))

    def opened_by(self) -> str:
        """Who opened the current window; OPENED_BY_HUB when the file
        predates the field or is shut. Never raises."""
        if _pairing_always_open():
            return self.OPENED_BY_ALWAYS
        if not self.is_open():
            return self.OPENED_BY_HUB
        value = self._read().get("opened_by")
        return value if isinstance(value, str) and value else self.OPENED_BY_HUB


class PairingClosed(ValueError):
    """add() refused a NEW key because no pairing window is open."""


class PhoneRegistry:
    """The phones that may connect, keyed by a hash of their public key."""

    def _locked(self):
        """Every read-modify-write of the file: take the lock, then re-read
        UNCONDITIONALLY. `_save` writes the whole dict, so a mutator that
        starts from a copy another process has since changed writes that
        copy back -- a revoked phone resurrected, a rename undone. Checking
        the lock first and reading second is what makes that impossible,
        rather than merely unlikely. Thread lock + flock on <registry>.lock
        (locked_store.StoreLock), re-entrant within one thread."""
        return self._lock.hold(on_acquire=self._load)

    def __init__(self, path: Optional[str] = None) -> None:
        self.path = path or _phones_file()
        if path is None:
            # Only the default construction migrates: an explicit path is
            # a caller (or a test) that knows what it wants, and moving
            # other files toward it would be a surprise, not a service.
            _adopt_legacy_default(self.path)
        self.phones: Dict[str, Dict[str, Any]] = {}
        #: (mtime_ns, inode, size) of the file last loaded; None = no file.
        #: More than mtime alone: two saves inside one kernel clock tick
        #: share an mtime, and the second would never be picked up.
        self._sig: Optional[tuple] = None
        #: The file exists but could not be READ (permissions, I/O). The
        #: last-known phones are kept, and an empty register is NOT taken
        #: to mean "nothing to protect" -- see may_enroll.
        self._unreadable = False
        self._lock = StoreLock(self.path + ".lock")
        self.window = PairingWindow(
            os.path.join(os.path.dirname(self.path) or ".",
                         "pairing-open.json"))
        self._load()

    # ── storage ──────────────────────────────────────────────────────────

    def _reload_if_changed(self) -> None:
        """Pick up edits made by ANOTHER PROCESS, before every decision.

        Revocation is run from setup_manager while ws_server is already
        running. Without this, removing a phone would change a file the
        server had read once at import and would keep on trusting until
        someone restarted it -- a revocation that silently does nothing
        until reboot is worse than no revocation at all, because you think
        you are safe.
        """
        # Under the thread lock: a reader on the event loop must not swap
        # `self.phones` while a worker thread is between changing and saving
        # it (the RLock lets a mutation that already holds it re-enter).
        with self._lock.thread_lock:
            try:
                st = os.stat(self.path)
            except FileNotFoundError:
                # The file went away. Treat it as "nobody is registered"
                # rather than keeping a list nothing backs any more.
                self.phones = {}
                self._sig = None
                self._unreadable = False
                return
            except OSError:
                return                  # transient; keep what we know
            if fingerprint(st) != self._sig:
                self._load()

    def _load(self) -> None:
        try:
            with open(self.path, encoding="utf-8") as f:
                st = os.fstat(f.fileno())
                data = json.load(f)
            if not isinstance(data, dict):
                raise ValueError(f"expected a JSON object, got "
                                 f"{type(data).__name__}")
        except FileNotFoundError:
            self.phones = {}
            self._sig = None
            self._unreadable = False
            return
        except OSError as exc:
            # PERMISSIONS OR I/O, not bad contents: the file may be
            # perfectly good and simply not readable by THIS process (a
            # different user running setup_manager) or not right now. Do
            # not move it aside -- that would wipe every registration over
            # a transient fault -- and keep whatever was last known.
            print(f"[phones] {self.path} could not be read ({exc}); "
                  f"keeping the last known register.")
            self._unreadable = True
            return
        except ValueError as exc:
            # AN UNREADABLE REGISTRY QUARANTINES ITSELF (2026-08-27). The
            # old behaviour was to refuse to start and tell the person to
            # move the file aside by hand -- fail-shut, on the theory that
            # corrupt security data must not be silently discarded. The
            # principle stands; the chore does not. Nothing is discarded:
            # the unreadable file is ARCHIVED under a timestamped name (for
            # forensics, or hand-repair if it ever mattered), the hub
            # starts with an empty register, and it says so loudly --
            # every phone pairs again, under the same window rules as ever.
            #
            # Why this is not a weakening worth losing sleep over: the file
            # holds no secrets by design (public keys and names), and an
            # attacker who can CORRUPT it on disk could just as easily have
            # WRITTEN a valid registry containing their own phone -- the
            # crash never stood between such an attacker and anything.
            # What it did stand between was the owner and her own hub after
            # a torn write or a hand-emptied file.
            moved = quarantine(self.path)
            print(f"[phones] {self.path} could not be read ({exc}). "
                  + (f"Moved aside to {moved}. " if moved else
                     "It could not even be moved aside. ")
                  + "Starting with an EMPTY phone register -- every phone "
                    "must pair again (same pairing rules as always).")
            self.phones = {}
            # Left in place (could not be moved): remember its signature so
            # it is not re-read -- and re-reported -- on every single call,
            # and do not let the empty register count as "nothing to
            # protect" while the real one is still sitting there.
            self._sig = None if moved else fingerprint(st)
            self._unreadable = not moved
            return
        self.phones = {k: v for k, v in data.items() if isinstance(v, dict)}
        # Taken from the descriptor that was read, so a replace racing the
        # read leaves a signature that no longer matches -> reloaded next time.
        self._sig = fingerprint(st)
        self._unreadable = False

    def _save(self) -> None:
        if self._unreadable:
            # The reload under the lock FAILED, so what is in memory is the
            # last-known register, not what is on disk -- writing it would
            # resurrect a phone another process revoked.
            raise OSError(f"{self.path} could not be read -- refusing to "
                          f"overwrite it with a stale copy")
        # Under _locked(): nobody else can have replaced it since.
        self._sig = atomic_write_json(self.path, self.phones,
                                      ensure_ascii=False, indent=2)

    # ── the register ─────────────────────────────────────────────────────

    def count(self) -> int:
        self._reload_if_changed()
        return len(self.phones)

    def list(self) -> List[Dict[str, Any]]:
        self._reload_if_changed()
        return sorted(self.phones.values(),
                      key=lambda p: p.get("paired_at", 0))

    def get(self, phone_id: str) -> Optional[Dict[str, Any]]:
        self._reload_if_changed()
        return self.phones.get(phone_id)

    def may_enroll(self) -> bool:
        """Deliberately opened, or nothing yet to protect.

        THREE WAYS THE DOOR IS OPEN, and they are not equally serious.

        1. `config.PAIRING_ALWAYS_OPEN` -- her call, 2026-09-14: "for now
           always allow the pairing window be open no need for buttons".
           The hub stops requiring a deliberate window at all.

           WHAT IT DOES NOT WAIVE, and this is the whole reason it is a
           reasonable thing to ask for: enrolment still requires the HMAC
           of the API key, computed over a message bound to THIS hub's
           certificate. A stranger on the network without that key gets
           exactly nowhere, window or no window. The window was never the
           thing keeping people out; it was a second, human-timed fence
           behind the cryptographic one.

           WHAT IT COSTS: anyone who HAS the API key can pair a phone at
           any moment, rather than only during a window somebody opened on
           purpose. If the key leaks -- a screenshot, a shared photo of
           the pairing screen, an old backup -- the window was the thing
           that would have limited the damage to five minutes. That is the
           trade, and it is stated in config.py beside the setting so it
           is switched back with one line.

        2. HOMEVOICE_BENCH_PAIRING=1 -- the development bench, where every
           app reinstall mints a fresh keypair and the hub greets its own
           developer as a stranger for the fifth time in an afternoon.
           Same waiver, same cost, read from the environment on every call
           so unsetting it takes effect immediately. Kept working
           unchanged; the setting above is the one meant for a house.

        3. An actual window somebody opened, or a hub with no phones yet
           registered -- nothing to protect until there is a first phone.
        """
        if _pairing_always_open():
            return True
        if os.environ.get("HOMEVOICE_BENCH_PAIRING") == "1":
            return True
        self._reload_if_changed()
        # An empty register only means "nothing to protect" when it was
        # actually READ as empty -- not when the real file is unreadable.
        return self.window.is_open() or (not self.phones
                                         and not self._unreadable
                                         and not os.path.exists(
                                             self._button_flag()))

    def _button_flag(self) -> str:
        return os.path.join(os.path.dirname(self.path) or ".",
                            "pairing-button-only")

    def need_button(self) -> None:
        """The last person left and the house stays: an empty phone list is
        no longer "nothing to protect" -- pairing needs the hub's button
        until a phone pairs."""
        with open(self._button_flag(), "w", encoding="utf-8") as f:
            f.write("1\n")

    def add(self, name: str, public_key_b64: str,
            device: str = "", via: str = "") -> Dict[str, Any]:
        """Register a phone. Raises ValueError if the key is not a usable
        Ed25519 public key -- checked HERE rather than at first use, so a
        malformed key is refused at the moment it is offered.

        TWO IDENTITIES PER RECORD, deliberately separate (the owner's
        design, 2026-08-26): `name` is what the PERSON calls this handset
        ("Augusta", "mum's phone") -- typed, human, theirs to choose --
        and `device` is what the handset says it IS ("TECNO KJ5",
        "samsung SM-A225F") -- read from the OS, stable across
        reinstalls, never typed. They used to share one field, which
        meant renaming your phone quietly destroyed the reinstall
        discriminator; now recognition asks BOTH (see
        purge_stale_siblings), so a pet name costs nothing."""
        key = _load_public_key(public_key_b64)          # validates
        del key
        phone_id = phone_id_for(public_key_b64)
        # ONE PHONE PER WINDOW, decided HERE, under one lock with the
        # registry write: two pairings racing past may_enroll() used to
        # both get in before either closed the window. The already-
        # registered test is under the same lock, so a second enrolment of
        # the same key racing the first finds the record and cannot
        # overwrite it (and its owner binding) with a fresh one.
        with self._locked():
            existing = self.phones.get(phone_id)
            if existing:
                # ALREADY REGISTERED: knowing the API key and a phone's
                # PUBLIC key must not let anyone rename or re-device it, or
                # touch its binding. Only last_seen moves (and a name, if it
                # never had one).
                existing["last_seen"] = int(time.time())
                if not (existing.get("name") or "").strip():
                    existing["name"] = ((name or "").strip()[:60]
                                        or "unnamed phone")
                self._save()
                return existing
            if not self.may_enroll():
                raise PairingClosed("this hub is not accepting NEW phones "
                                    "right now")
            record = {
                "id": phone_id,
                "name": (name or "").strip()[:60] or "unnamed phone",
                "device": (device or "").strip()[:80],
                "public_key": public_key_b64,
                "paired_at": int(time.time()),
                # HOW IT CAME IN: "home" (the house Wi-Fi or the hub's own)
                # or "away" (Tailscale). Told to every other phone.
                "paired_via": via if via in ("home", "away") else "",
                "last_seen": 0,
                # WHICH PERSON THIS PHONE BELONGS TO -- empty until they
                # enrol.
                "user_id": "",
            }
            self.phones[phone_id] = record
            self._save()
            self.window.close()
            try:
                os.remove(self._button_flag())
            except OSError:
                pass
        return record

    def remove(self, phone_id: str) -> bool:
        """Revoke one phone.

        RELOADS FIRST, for the reason spelled out in `touch`: `_save` writes
        the whole dict, so removing one phone out of a stale copy silently
        REINSTATES every other phone as it was when this instance last read
        the file -- including one another process revoked a minute ago, and
        including a rename. Revocation writing a stale snapshot is the exact
        failure `_reload_if_changed` exists to prevent.

        Also reports honestly when the phone is already gone: the membership
        test now runs against what is actually on disk, so a phone another
        process revoked returns False rather than "removed" a second time.
        """
        with self._locked():
            if phone_id not in self.phones:
                return False
            del self.phones[phone_id]
            self._save()
        return True

    def acknowledge(self, phone_id: str, by: str) -> bool:
        """Someone in the house said a newly paired phone is theirs: the
        new-phone notice leaves every phone. Under the same reload-first
        lock as remove()."""
        with self._locked():
            record = self.phones.get(phone_id)
            if not record:
                return False
            record["acknowledged_by"] = str(by or "someone")
            self._save()
        return True

    def bind_user(self, phone_id: str, user_id: str) -> bool:
        """Record which enrolled person owns this phone.

        THE TWO HALVES OF IDENTITY MEET HERE, and they are not the same
        question. The Ed25519 keypair proves WHICH PHONE is talking -- it
        cannot prove who is holding it. The voiceprint and PIN prove WHICH
        PERSON -- they cannot prove what they are speaking into. A stolen
        paired phone still fails the voice check at the door; a perfect
        impersonation from an unpaired handset never reaches the door at
        all. Neither is sufficient, which is the entire point of keeping
        them separate and writing the link down here.
        """
        with self._locked():
            record = self.phones.get(phone_id)
            if not record:
                return False
            record["user_id"] = str(user_id or "").strip()
            self._save()
        return True

    def rename_user(self, old_id: str, new_id: str) -> List[str]:
        """Re-point every phone bound to `old_id` at `new_id`.

        The fourth store a rename has to move, and the quietest one to
        forget: nothing here fails loudly if it is missed. The hub simply
        starts resolving a phone to a person who no longer exists, and
        ws_server's speaker-seeding hands the shell an id that is in no
        other store -- so the phone is "recognised" as nobody and every
        gated action refuses, with no error that names the cause.

        Returns the phone ids that moved, so the caller can log what it
        actually did rather than what it meant to do. An empty list is a
        perfectly normal answer: a person may have enrolled and never
        paired a handset.
        """
        old_id = (old_id or "").strip()
        new_id = (new_id or "").strip()
        if not old_id or not new_id or old_id == new_id:
            return []
        moved: List[str] = []
        with self._locked():
            for phone_id, record in self.phones.items():
                if (record.get("user_id") or "").strip() == old_id:
                    record["user_id"] = new_id
                    moved.append(phone_id)
            if moved:
                self._save()
        return moved

    def purge_stale_siblings(self, phone_id: str) -> List[str]:
        """Remove records that are THIS handset's previous lives.

        Every app reinstall mints a fresh keypair, so the same physical
        phone re-pairs as a NEW record and its old one stays behind --
        dead keys, still bound to a person, accumulating one per
        reinstall. Called at the moment the phone PROVES its person (the
        PIN login, or a completed enrolment), because that is when the
        hub knows the new key and the old ones answer to the same hand.

        A sibling is: same owner AND same name AND same device, different
        key (the owner's three-way match, 2026-08-26: "the system uses
        that phone name to match, if the same phone, then the username,
        if the same then the acceptance"). The name alone used to carry
        the whole discrimination, which held only as long as the name WAS
        the model string the pair screen prefilled -- the moment someone
        renamed their phone "Augusta" and their tablet "Augusta" too, a
        reinstall purge could revoke the living tablet. The device string
        (read from the OS, never typed) now sits beside the name, and
        BOTH must agree before a record is treated as this handset's
        previous life.

        LEGACY RULE: a record (or a caller) with no device string on file
        cannot be held to a match it never had the chance to make --
        when either side's device is empty, the name+owner rule of old
        decides alone. So existing registries keep purging exactly as
        before until their phones re-pair and pick up device strings.

        The cost that remains, accepted: two identical, identically-NAMED
        handsets of the same model still collide. That is a rare
        inconvenience; the alternative is a registry where revoking "the
        old phone" means guessing which of five identical entries is the
        live one.

        GENERIC NAMES NEVER PURGE. "my phone" is the pair screen's
        fallback for an empty name field, so two different handsets can
        wear it at once -- on a default name, same-name-same-owner stops
        meaning "reinstall sibling" and starts meaning "two devices whose
        owner skipped the field". A purge on that would revoke a living
        phone. Records under a generic name accumulate instead, which is
        the lesser harm and an argument for naming your phones.

        Never purges an unbound record (no user_id proved nothing), and
        never the caller's own.
        """
        with self._locked():
            me = self.phones.get(phone_id)
            if not me:
                return []
            user = str(me.get("user_id") or "").strip()
            if not user:
                return []
            name = str(me.get("name") or "").strip().lower()
            if name in GENERIC_PHONE_NAMES:
                return []
            device = str(me.get("device") or "").strip().lower()
            if not device:
                # The CALLER's device string is client-supplied; an empty one
                # must never act as a wildcard, or a phone paired with
                # device="" could purge any same-named handset. No device, no
                # purge.
                return []

            def _same_handset(p: Dict[str, Any]) -> bool:
                other = str(p.get("device") or "").strip().lower()
                # Empty on the OLD record only -> a legacy record that never
                # had the chance to match; the name+owner rule decides. Both
                # present -> they must AGREE, which is what keeps "Augusta"
                # the phone from revoking "Augusta" the tablet.
                return (not other) or other == device

            gone = sorted(pid for pid, p in self.phones.items()
                          if pid != phone_id
                          and str(p.get("user_id") or "").strip() == user
                          and str(p.get("name") or "").strip().lower() == name
                          and _same_handset(p))
            for pid in gone:
                del self.phones[pid]
            if gone:
                self._save()
            return gone

    def user_for(self, phone_id: str) -> str:
        """The person this phone belongs to, or "" if it has not enrolled."""
        self._reload_if_changed()
        return str((self.phones.get(phone_id) or {}).get("user_id") or "")

    def phones_for_user(self, user_id: str) -> List[str]:
        """Every phone belonging to one person. Removing a person should not
        leave their handsets authenticating against a user that is gone."""
        self._reload_if_changed()
        return sorted(p["id"] for p in self.phones.values()
                      if p.get("user_id") == user_id)

    def touch(self, phone_id: str) -> None:
        """Record that a phone connected -- but not on every connection.

        The hub's records live on an SD card, and a write per connection is
        a write per wake-word on a card with a finite number of them. A
        minute's granularity is plenty for "when did this phone last talk to
        me" and costs almost nothing.

        RELOAD FIRST, AND THIS IS THE WHOLE POINT OF THE METHOD NOW
        (2026-09-14: "phones identity didnt change when name was changed").

        `_save` writes the ENTIRE in-memory dict. This method read a record
        out of a dict that might be minutes old, changed one field in it,
        and wrote the whole thing back -- so any edit another registry had
        made to that file in between was overwritten by a stale snapshot.

        That is not a theoretical race. `ws_server.PHONES` is a MODULE-LEVEL
        SINGLETON, loaded once at import; `menu_rename_user` builds a FRESH
        `PhoneRegistry()` (main.py) and renames through that one. So:

            1. the phone is paired, user_id "augusta"
            2. Change your name -> the fresh registry writes
               "augusta chinenye" to disk. Correct, on disk.
            3. the phone reconnects -> the SINGLETON touches it, from the
               copy it loaded before the rename, and writes "augusta" back

        The rename was undone by the next connection, every time, which is
        exactly "the phone's identity didn't change". Reproduced, then
        fixed here. Every other mutator already reloaded; this one and
        `remove` did not, and `remove` is the revocation path -- where a
        stale write does not merely lose a rename, it RESURRECTS A REVOKED
        PHONE. See `_reload_if_changed`, whose own docstring says a
        revocation that silently does nothing is worse than none.
        """
        self._reload_if_changed()
        record = self.phones.get(phone_id)
        now = int(time.time())
        if not record or now - int(record.get("last_seen") or 0) < 60:
            return                      # the common case: no write, no lock
        with self._locked():
            record = self.phones.get(phone_id)
            if not record or self._unreadable:
                return                  # revoked meanwhile: do not re-add
            record["last_seen"] = now
            self._save()

    # ── the part that actually authenticates ─────────────────────────────

    def verify(self, phone_id: str, signature_b64: str, message: str) -> bool:
        """Does this phone's registered public key sign this message?

        Returns False for every kind of failure -- unknown phone, malformed
        signature, bad signature -- deliberately. The caller must not be able
        to tell an unknown id from a wrong signature, because that
        distinction tells an attacker which half to keep guessing at.
        """
        self._reload_if_changed()
        record = self.phones.get(phone_id)
        if not record:
            return False
        try:
            key = _load_public_key(record["public_key"])
            signature = base64.b64decode(signature_b64, validate=True)
            key.verify(signature, message.encode("utf-8"))
            return True
        except Exception:                              # noqa: BLE001
            return False


def _load_public_key(public_key_b64: str):
    """Decode a base64 Ed25519 public key, or raise.

    Ed25519 public keys are exactly 32 bytes. Checking the length before
    handing it to the library turns a confusing backend error into a
    sentence that says what is wrong.
    """
    from cryptography.hazmat.primitives.asymmetric import ed25519
    raw = base64.b64decode(public_key_b64, validate=True)
    if len(raw) != 32:
        raise ValueError(
            f"an Ed25519 public key is 32 bytes; this one is {len(raw)}")
    return ed25519.Ed25519PublicKey.from_public_bytes(raw)
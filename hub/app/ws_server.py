"""
ws_server.py — WebSocket server with HTTPS static file serving.

AUTHENTICATION  (Tailscale + HMAC challenge-response)
-----------------------------------------------------
Two layers, deliberately:

  1. Tailscale ACLs are the network gate -- to reach this socket at all a
     device must be on the tailnet (authenticated to Tailscale with its own
     identity, authorized by you).

  2. HMAC challenge-response is the app gate on top. On connect the server
     sends {type:"auth_challenge", nonce:<random>}. The client replies with
     HMAC_SHA256(shared_secret, "<nonce>:<timestamp>") as lowercase hex, plus
     the same timestamp in cleartext. The server recomputes and compares
     constant-time.

     The timestamp is INSIDE the MAC on purpose. A field that is checked but
     not authenticated is decoration -- an attacker sitting between client and
     server could rewrite it freely. Binding it means a captured (hmac,
     timestamp) pair can't be re-dated.

Why HMAC and not a bare API key:
  * The shared secret NEVER crosses the wire -- only the MAC does. So it can't
    be captured by sniffing the handshake, and (critically for the browser test
    client) it isn't shipped inside the page. The server no longer injects the
    key into the served HTML at all; the client is given the secret
    out-of-band (pasted into a field / stored in the phone keychain) and uses
    it only to compute HMACs locally.
  * Replay-proof: the nonce is fresh and random per connection, so an HMAC
    captured from one session is useless against the next.

Defense in depth for a system that controls physical locks: one failure (a
loosened Tailscale ACL, one compromised tailnet device) is not total
compromise, because the attacker still needs the shared secret.

CONCURRENCY MODEL
-----------------
One reader task per connection owns websocket.recv(). Frames route by type:
"command" -> its own _handle_command() task; anything else -> the running
command's bounded reply queue. See _reader_loop.

SHARED SHELL STATE
------------------
There is ONE InteractiveShell for the whole process, shared by every
connection. That is deliberate (the NLU/voice stack is expensive to load).
What a phone is IN THE MIDDLE OF -- where its questions go, who is speaking,
why its last command failed -- is not shared any more: each connection has
its own app.shell.Conversation, chosen by a context variable set once at the
top of run(), and the shell's attributes of those names read and write it
(2026-10-11). See _reset_shell_state() for what is cleared per command.

WHO WAITS FOR WHOM
------------------
Commands do NOT take the shell lock any more, and neither do the read-only
doors (LOCK_FREE_ACTIONS). Two phones' commands run side by side -- each
asking its own questions and checking its own voice -- and the stores they
touch lock themselves (devices, scenes, PINs, the models'
inference). The shell lock is kept for what changes the house's SETUP:
adding and removing devices, boards, people, firmware, remote access. Two
of those at once could undo each other, so they still take turns.

ERROR FRAMES
------------
Every {"type":"error"} and every failed {"type":"result"} carries BOTH a
machine-readable "reason" and a human-readable "text". Clients were reading
one or the other and logging `undefined`; sending both ends the guessing.
"""

import asyncio
import base64
import hashlib
import hmac
import http.server
import json
import os
import secrets
import signal
import socket
import socketserver
import sys as _sys

#: SO_REUSEADDR means two different things.
#:
#: On Unix it lets you bind a port stuck in TIME_WAIT, and binding a port
#: another socket is ACTIVELY listening on still fails -- which is exactly the
#: behaviour _address_available needs.
#:
#: On Windows it lets you bind a port another socket is actively listening on;
#: Microsoft's documentation says it "allows a socket to forcibly bind to a
#: port in use by another socket". So on Windows the option turns the probe
#: into a lie: a busy address reports as free, and two instances each believe
#: they own the port. Python's own socketserver docs carry the same warning
#: about allow_reuse_address.
_REUSE_IS_SAFE = _sys.platform != "win32"
import ssl
import threading
import time
import traceback
import functools
from datetime import datetime
import urllib.parse

import websockets.exceptions        # binds `websockets` too; the package imports it lazily
from typing import Any, Dict, List, Optional, Set, Tuple

from core import config
from core.hv_common import times_add, times_begin, times_line
from identity import phones as _phones
from hub import hub_net
from app.shell import (InteractiveShell, WebSocketIO, NullIO, CancelledByUser,
                  Conversation, CONVERSATION,
                  event, describe_audio, _bring_the_stack_up,
                  _take_the_stack_down, run_entry)
from voice_security import audio_io


#: The phones allowed to connect. ONE registry for the process -- every
#: connection reads the same list, so revoking a phone takes effect on its
#: next connection rather than after a restart.
PHONES = _phones.PhoneRegistry()

def own_spki_fingerprint() -> str:
    """This hub's own public key fingerprint. See phones.spki_fingerprint.

    ALWAYS local.crt, on every binding, for a PAIRED phone: the app signs
    the SPKI it stored at pairing (local.crt's), whichever address it came
    in on, so the tailnet listener -- which serves Tailscale's Let's
    Encrypt certificate -- verifies the same string. That is what lets a
    paired phone connect from anywhere.

    PAIRING is different, and was broken on the tailnet until 2026-09-08:
    a phone with no key yet computes its HMAC over the certificate it was
    ACTUALLY SHOWN (phones.spki_fingerprint's whole point), which on the
    tailnet is the Let's Encrypt one, so the HMAC never matched local.crt
    and pairing could only happen on the access point or the house Wi-Fi.
    The enrolment check now also accepts the HMAC over the certificate
    this connection's listener really served (served_spki_fingerprint) --
    the binding to the shown certificate is exactly the relay defence,
    made correct per listener rather than dropped -- so a phone can pair
    over Tailscale as well. Nothing changes for the local roads, where the
    served certificate IS local.crt.
    """
    return _phones.spki_fingerprint(config.LOCAL_TLS_CERT)


def _pair_reachable(from_away: bool) -> bool:
    """Can a NEW phone reach this hub right now? An address this machine
    really holds (hotspot on, Wi-Fi joined); the tailnet only for a window
    opened for a phone away. What a pairing window waits on (2026-10-19).
    Unsure (the check itself failed): yes -- the old behaviour."""
    try:
        from app import setup_manager
        return setup_manager._reachable_road(from_away) is not None
    except Exception:                                  # noqa: BLE001
        return True


def _write_pair_codes(until: float) -> None:
    """The pairing codes as PNGs, one per road, when the BUTTON opens the
    window. `--pair` draws them on the console; a button on a headless hub
    has no console, so they go to certs/ (pair.png, pair-<road>.png) for
    whoever can fetch them -- and the log says where, and for which
    address, without ever printing the key."""
    try:
        from app import setup_manager
        roads = setup_manager._pair_roads()
        written = []
        for index, (name, host, _label) in enumerate(roads):
            payload = setup_manager._pair_code_payload(until, host=host)
            if payload is None:
                print("[GPIO] no pairing code: HOMEVOICE_API_KEY is not set")
                return
            out = setup_manager._write_pair_png(payload, road=name if index else "")
            if out:
                written.append(f"{os.path.basename(out)} ({name} {host})")
        if written:
            print("[GPIO] pairing codes written to certs/: " + ", ".join(written))
        else:
            print("[GPIO] no road to draw a pairing code for (no address held)")
    except Exception as exc:                           # noqa: BLE001
        print(f"[GPIO] could not write the pairing codes: {exc}")


def _adopt_tailnet_binding(name: str) -> None:
    """A tailnet joined WHILE THE HUB RUNS becomes a listener without a
    restart. config.NETWORK_BINDINGS is built at import from the name
    Tailscale had then -- on a hub that joined from the app a moment ago,
    that was no name at all, so the tailnet entry did not exist and the
    supervisor had nothing to bind until somebody restarted the service
    (2026-09-08). Now the entry is added (or its name replaced, after a
    switch) and the next twenty-second check binds it; config.WS_HOSTNAME
    follows, so the very next auth_ok tells phones the name. Without the
    certificate there is nothing to serve TLS with, so the entry waits for
    `tailscale cert` to have succeeded.

    UPDATE 2026-09-09: the tailnet serves local.crt -- the PINNED
    certificate -- like every other road (config.NETWORK_BINDINGS says
    why: the app dials the tailnet by IP and pins it, and the Let's Encrypt
    certificate's key is not the pinned one). local.crt always exists, so
    the listener no longer waits on `tailscale cert`."""
    if not name:
        return
    entry = {"name": "tailnet", "host": name, "origin": name,
             "cert": config.LOCAL_TLS_CERT, "key": config.LOCAL_TLS_KEY}
    bindings = config.NETWORK_BINDINGS
    for i, existing in enumerate(bindings):
        if existing.get("name") == "tailnet":
            if existing.get("host") == name:
                return
            bindings[i] = entry
            break
    else:
        bindings.append(entry)
    config.WS_HOSTNAME = name
    event("TAILNET", f"listener for {name} will be bound on the next check")


def _tailnet_memory() -> str:
    return os.path.join(str(config.DATA_DIR), "tailnet_last.json")


def _remembered_tailnet() -> Tuple[str, str]:
    """(name, ip) the hub last had on Tailscale, or ("", "")."""
    try:
        with open(_tailnet_memory(), encoding="utf-8") as f:
            d = json.load(f)
        return str(d.get("name") or ""), str(d.get("ip") or "")
    except (OSError, ValueError, AttributeError):
        return "", ""


def _remember_tailnet(name: str, ip: str) -> None:
    """Keep the hub's Tailscale name and address while they work."""
    if not name and not ip:
        return
    old = _remembered_tailnet()
    new = (name or old[0], ip if is_tailnet_ip(ip) else old[1])
    if new == old:
        return
    try:
        with open(_tailnet_memory(), "w", encoding="utf-8") as f:
            json.dump({"name": new[0], "ip": new[1]}, f)
    except OSError:
        pass


def _forget_tailnet() -> None:
    try:
        os.remove(_tailnet_memory())
    except OSError:
        pass


def _drop_tailnet_binding() -> None:
    """The hub signed out of Tailscale: forget the tailnet binding, so the
    next supervisor check closes its listener; config.WS_HOSTNAME follows."""
    config.NETWORK_BINDINGS[:] = [b for b in config.NETWORK_BINDINGS
                                  if b.get("name") != "tailnet"]
    config.WS_HOSTNAME = ""
    _forget_tailnet()
    event("TAILNET", "signed out: the tailnet listener closes on the next "
                     "check")


#: Which certificate each listening address serves: filled by _bind, emptied
#: by _reconcile when a listener is dropped, read by the enrolment check and
#: by bound_tailnet_ip(). Every binding serves LOCAL_TLS_CERT since
#: 2026-09-09 (the tailnet included -- see _adopt_tailnet_binding); the
#: per-address record stays because config.NETWORK_BINDINGS is meant to be
#: edited and an entry may name another certificate again.
_CERT_BY_ADDRESS: Dict[str, str] = {}


def is_tailnet_ip(ip: str) -> bool:
    """Is this peer reaching the hub through Tailscale (100.64.0.0/10) --
    that is, from away? Anything unparseable is not."""
    import ipaddress
    try:
        return ipaddress.ip_address(ip) in ipaddress.ip_network("100.64.0.0/10")
    except ValueError:
        return False


def bound_tailnet_ip() -> str:
    """The tailnet ADDRESS this hub is actually listening on (100.64/10),
    read from the live bindings -- "" when no tailnet listener is up.

    This is what a phone should dial away from home, and it is told on
    every auth_ok beside the tailnet NAME. The name was all the app ever
    learned, and the name is the fragile road: it needs the phone's own
    resolver to answer for MagicDNS, the phone's system root store to
    trust the Let's Encrypt chain, and that certificate to be the one on
    the listener -- and on the first real phone (2026-09-09) every one of
    those failed while the IP worked. An IP is PINNED by the app against
    local.crt, which the tailnet listener now serves, so it needs nothing
    from the phone at all. Read from _CERT_BY_ADDRESS rather than asked of
    tailscale, so it is the address that is genuinely answering.
    """
    import ipaddress
    net = ipaddress.ip_network("100.64.0.0/10")
    for address in list(_CERT_BY_ADDRESS):
        try:
            if ipaddress.ip_address(address) in net:
                return address
        except ValueError:
            continue
    return ""


def served_spki_fingerprint(websocket) -> Optional[str]:
    """The SPKI fingerprint of the certificate the listener this connection
    arrived on actually presented, or None when it is local.crt (already
    covered by own_spki_fingerprint) or cannot be worked out."""
    try:
        local = websocket.local_address
        address = local[0] if local else ""
    except Exception:                                  # noqa: BLE001
        return None
    cert = _CERT_BY_ADDRESS.get(address)
    if not cert:
        return None
    try:
        if os.path.realpath(cert) == os.path.realpath(config.LOCAL_TLS_CERT):
            return None
        return _phones.spki_fingerprint(cert)
    except Exception:                                  # noqa: BLE001
        return None


def decode_audio_bytes(raw: bytes, fmt: str = "wav"):
    """Decode raw network audio to 16kHz mono float32 (or None) via the shared
    audio_io chokepoint -- the same decoder enrollment uses, so enroll and
    verify decode identically."""
    return audio_io.decode_bytes(raw, fmt_hint=fmt)


def _b64_kb(b64: Any) -> str:
    if not isinstance(b64, str):
        return f"<{type(b64).__name__}>"      # a malformed frame, traced as such
    return f"{len(b64) * 3 // 4 / 1024:.0f} KiB"


#: Longest name/device string accepted from a pairing frame.
MAX_LABEL_LEN = 64


def _clean_label(value: Any) -> str:
    """A pairing-frame label as it may be stored and spoken: a str, control
    characters removed, whitespace collapsed, cut at MAX_LABEL_LEN. Anything
    else is ""."""
    if not isinstance(value, str):
        return ""
    cleaned = "".join(ch for ch in value if ch.isprintable())
    return " ".join(cleaned.split())[:MAX_LABEL_LEN]


def _clear_speaker_context(shell, speaker_id) -> None:
    """Forget what the previous command on this shell acted on.

    The resolver's context rung answers pronoun and bare commands from
    dm.last_group(speaker_id), which is written by touch_group() at the end of
    every successful command. In the CLI that history is scoped to one process
    run by one person. Here the shell outlives every connection, so without
    this the group from one phone's command is still sitting there when a
    different phone connects -- and a command like "turn it off" resolves
    against a device the current user never touched.

    NOTE: this is the per-connection half of the fix. The other half is a TTL
    inside DeviceManager, so that even within one long session a back-reference
    made twenty minutes later doesn't silently bind to something stale. That
    belongs in device_manager.py next to last_group/touch_group, not here.

    Callers must pass the LAST KNOWN speaker, not just the one this command
    resolved. A command that fails before _resolve_current_speaker() runs has
    speaker_id None, and this used to return immediately -- leaving the
    PREVIOUS user's last_group sitting in a shell that outlives every
    connection. That is precisely the leak this function exists to stop, and
    it was skipped on exactly the path where it mattered most.

    Calls DeviceManager.clear_context() directly. This used to probe by
    hasattr for that method OR a `forget_group`, and fall back to
    `touch_group(speaker_id, [])` when it found neither -- which it never did,
    because neither existed. And touch_group returns early on an empty list,
    so the fallback cleared nothing. The entire mechanism was a no-op until
    clear_context was actually written.
    """
    if speaker_id is None:
        return
    try:
        shell.dm.clear_context(speaker_id)
    except Exception:
        traceback.print_exc()


def _phone_clock(value: Any) -> Optional[datetime]:
    """The phone's own clock from a command frame's `now` (ISO-8601 with its
    UTC offset), or None for anything missing or implausible. DATA FROM THE
    PHONE: a string of sane length that parses, nothing else."""
    if not isinstance(value, str) or not (10 <= len(value) <= 40):
        return None
    try:
        t = datetime.fromisoformat(value)
    except ValueError:
        return None
    return t if 2020 <= t.year <= 2100 else None


#: What starts a correction said over a running command, besides the cancel
#: phrases themselves: "no, the fan", "I meant the fan". A closed grammar.
CORRECTION_LEADS = frozenset({"no", "nope", "i meant", "i mean", "no i meant",
                              "no i mean"})


#: Commands running right now, on every phone. They take no lock (see WHO
#: WAITS FOR WHOM); this is what the model warm-up asks before a beat, so a
#: beat is never what a command waits behind.
_COMMANDS_RUNNING = 0


def _enter_command() -> None:
    global _COMMANDS_RUNNING
    _COMMANDS_RUNNING += 1


def _leave_command() -> None:
    global _COMMANDS_RUNNING
    _COMMANDS_RUNNING = max(0, _COMMANDS_RUNNING - 1)


async def _in_own_conversation(coro: Any) -> Any:
    """Run `coro` in a fresh Conversation (its task's context only)."""
    CONVERSATION.set(Conversation(io=NullIO()))
    return await coro


#: Menu actions that only READ (and a camera feed, which is relayed beside
#: the flow): no shell lock, so a list or a profile never waits behind
#: another phone's Add device. A door that writes anything -- even rarely,
#: like check_notices settling an old rewire -- is not here. Arg actions
#: are matched by their head ("viewcam:<id>" -> "viewcam").
LOCK_FREE_ACTIONS = frozenset({
    "list_devices", "readings", "list_scenes", "list_layouts",
    "list_automations", "profile", "board_report", "boardreport", "viewcam",
})


class ConnectionHandler:
    # Every one of these is a config setting. They stay class attributes so a
    # test or a subclass can override one in isolation, but the values a
    # person would actually tune -- how long a human gets to answer, how much
    # audio is too much, how many sockets are too many -- now live in one file
    # with the rest, instead of eight numbers buried in a server module.
    MAX_AUDIO_B64_LEN = config.MAX_AUDIO_B64_LEN
    PROMPT_TIMEOUT = config.PROMPT_TIMEOUT
    SETUP_PROMPT_TIMEOUT = config.SETUP_PROMPT_TIMEOUT
    LOCK_ACQUIRE_TIMEOUT = config.LOCK_ACQUIRE_TIMEOUT
    REPLY_QUEUE_MAX = config.REPLY_QUEUE_MAX
    AUTH_MAX_SKEW = config.AUTH_MAX_SKEW
    AUTH_TIMEOUT = config.AUTH_TIMEOUT

    #: Reply window for a question the person must LEAVE THE PHONE to answer
    #: -- today only the recovery phrase, which lives on a piece of paper in
    #: a drawer. The ordinary PROMPT_TIMEOUT assumes the answer is already in
    #: the person's head; applied to "type the five words you wrote down" it
    #: cancels the reset while they are still finding them, and the attempt
    #: costs one of the recovery budget for nothing.
    #:
    #: 0 (or None) means WAIT INDEFINITELY. That is safe against a dead
    #: peer -- a dropped connection cancels the command task and releases
    #: the shell lock -- but a client that stays connected and never answers
    #: holds the lock for as long as it likes, so the default is a generous
    #: bound rather than none. Set this to 0 to
    #: remove it outright. (Raised to 1800 when the ordinary window became
    #: 900: a leave-the-phone question keeps a longer leash than an
    #: in-your-head one, or the patient flag stops meaning anything.)
    PATIENT_PROMPT_TIMEOUT = 1800

    #: How long "Still there?" waits for a tap (or the actual answer)
    #: before the flow is put away. See _await_reply.
    STILL_THERE_GRACE = config.STILL_THERE_GRACE

    def __init__(self, websocket, shell, lock):
        self.websocket = websocket
        self.shell = shell
        self.lock = lock
        #: What THIS phone is in the middle of (app.shell.Conversation). Made
        #: current for every task of this connection at the top of run().
        #: NullIO until a flow installs this connection's own WebSocketIO.
        self._conv = Conversation(io=NullIO())
        self._active_command_queue = None
        # The task that installed _active_command_queue. The done-callback
        # used to clear the queue unconditionally, so a SECOND, unrelated
        # task finishing (an enrollment that lost the lock race, say) tore
        # the queue out from under a first one that was still running -- and
        # _reader_loop silently drops replies when the queue is None, so the
        # live flow simply stopped receiving answers and timed out.
        self._queue_owner = None
        self._command_task = None
        # True while _command_task is one of QUIET_ACTIONS -- see _busy.
        self._quiet_flow = False
        #: The phone pressed Cancel (a `cancel` frame). The running flow
        #: stops at its next question or checkpoint; cleared when the next
        #: flow starts, so it can never reach past the flow it was aimed at.
        self._cancel_requested = False
        # True while a command has not started on the house yet: reading the
        # screen-navigation verdict, decoding audio. (The name is from when
        # it also waited for the shell lock; commands take none now.)
        # Nothing has been touched there, so Cancel may end it on the spot.
        self._pre_lock = False
        # TALKING OVER A COMMAND (2026-10-09). Set = nothing held. The phone
        # clears it (`hold`) the moment it hears speech while a command is
        # running, and the shell waits on it before touching any device.
        self._hold = asyncio.Event()
        self._hold.set()
        #: What was said over the running command and is to run after it:
        #: {"text", "audio", "free", "replace"}. The FIRST rides free (her
        #: rule: one piggyback per running command); the rest are counted.
        self._asides: List[Dict[str, Any]] = []
        self._aside_free = True
        self._auth_nonce = None
        # How this connection proved itself: "phone" (its own keypair, full
        # session) or "enroll" (the shared key, may only register a keypair).
        # None until _authenticate has run, and never trusted before then.
        self._auth_mode: Optional[str] = None
        self._phone_id: Optional[str] = None
        #: What the app said it can do in its auth frame ("files": the
        #: file_request frame). An older app says nothing.
        self._features: frozenset = frozenset()
        self._peer_cache: Optional[str] = None
        # True while first-run enrollment is being driven for this
        # connection. The reader must refuse 'command' frames during that
        # window -- see _dispatch_command_frame.
        self._bootstrapping = False
        # Which connection-cap bucket this socket is counted in -- see
        # _handler / _count_authenticated.
        self._counted_active = False
        # Set when this connection's slot has been released (see _handler),
        # so a newer session from the same phone can wait for it.
        self._released = asyncio.Event()
        # Tasks this connection started that outlive one command (today:
        # the tail of an interactive tailnet sign-in). Cancelled in run().
        self._background: Set[asyncio.Task] = set()
        # The link gauge (see _dispatch_link_ping): how Tailscale reaches
        # this phone, when that was last asked, and the grade last logged.
        self._link_path = ""
        self._link_path_at = 0.0
        self._link_grade_said = ""
        # Both the reader task and the command task send on this socket.
        # Concurrent sends from different tasks have no ordering guarantee,
        # which is how a refused command's `result` could overtake the
        # `prompt` it was racing and close a question the server was
        # blocked on. One writer at a time.
        self._send_lock = asyncio.Lock()

    def _peer(self) -> str:
        """Who this connection is, for the log.

        CACHED on first success. websockets clears remote_address once the
        socket is closed, so the disconnect line -- the one you actually read
        when working out who dropped and when -- came out as "CONN ?
        disconnected", naming nobody. Observed on the first real run."""
        if self._peer_cache is not None:
            return self._peer_cache
        try:
            addr = self.websocket.remote_address
            if addr:
                self._peer_cache = f"{addr[0]}:{addr[1]}"
                return self._peer_cache
        except Exception:
            pass
        return "?"

    def _peer_ip(self) -> str:
        """The address alone, for the throttle: one key per machine, not
        per socket, or a peer could dodge it by reconnecting."""
        try:
            addr = self.websocket.remote_address
            return str(addr[0]) if addr else "?"
        except Exception:                              # noqa: BLE001
            return "?"

    async def _send_json(self, obj: dict) -> None:
        async with self._send_lock:
            await self.websocket.send(json.dumps(obj))

    # ── reply-queue ownership ─────────────────────────────────────────────

    def _install_queue(self, owner: Any) -> "asyncio.Queue[Dict[str, Any]]":
        q: "asyncio.Queue[Dict[str, Any]]" = asyncio.Queue(maxsize=self.REPLY_QUEUE_MAX)
        self._active_command_queue = q
        self._queue_owner = owner
        return q

    def _release_queue(self, owner: Any) -> None:
        """Only the task that installed the queue may retire it."""
        if self._queue_owner is owner:
            self._active_command_queue = None
            self._queue_owner = None

    async def _acquire_lock(self, timeout: float) -> bool:
        """Bounded lock acquisition that cannot leak the lock.

        asyncio.wait_for(lock.acquire(), t) has a cancellation hazard: if the
        lock is handed over at the same moment the timeout fires, the acquire
        is cancelled AFTER succeeding and the lock is held by nobody, forever.
        Acquiring inside a task we own lets us hand it straight back if the
        timeout wins the race.

        FIX: the same hazard existed on a path the timeout handler never saw.
        When the WAITER ITSELF is cancelled -- a connection drops and run()'s
        finally cancels the command task while it is queued for the lock --
        the shielded acquire survived the cancellation, kept waiting, and
        could acquire the lock LATER with nobody left to release it. One dead
        phone mid-queue deadlocked the shell for the whole process. The
        CancelledError branch below hands back exactly like the timeout one.
        """
        acquire = asyncio.ensure_future(self.lock.acquire())

        def _hand_back(t):
            if not t.cancelled() and t.exception() is None:
                self.lock.release()

        try:
            await asyncio.wait_for(asyncio.shield(acquire), timeout)
            return True
        except asyncio.TimeoutError:
            acquire.add_done_callback(_hand_back)
            acquire.cancel()
            return False
        except asyncio.CancelledError:
            acquire.add_done_callback(_hand_back)
            acquire.cancel()
            raise

    async def _fail(self, reason: str, text: str, kind: str = "result",
                    refused: Optional[str] = None) -> None:
        """A refusal or failure frame of type `kind` ("result", "error",
        "auth_failed", "tailscale_failed", "pair_failed"). Always carries
        both keys -- see module docstring; a `result` also says ok=False.
        `refused` names the request a refused `result` answers ("command"
        or "menu:<action>"), so the phone never lowers the flags of a
        DIFFERENT request that is still running."""
        frame: Dict[str, Any] = {"type": kind, "reason": reason, "text": text}
        if kind == "result":
            frame["ok"] = False
        if refused:
            frame["action"] = refused
        await self._send_json(frame)

    async def _hang_up(self, reason: str = "", text: str = "") -> None:
        """Say why in an auth_failed frame (when there is a reason), then
        close. A peer that has already gone is an ordinary ending."""
        try:
            if reason:
                await self._fail(reason, text, "auth_failed")
            await self.websocket.close()
        except websockets.exceptions.ConnectionClosed:
            pass

    async def _recv_json(self):
        """Read one text frame, parse JSON. Returns None on garbage."""
        raw = await self.websocket.recv()
        try:
            parsed = json.loads(raw)
            return parsed if isinstance(parsed, dict) else None
        except (json.JSONDecodeError, TypeError):
            return None

    def _drain_replies(self) -> None:
        """Clear any replies queued before an interactive prompt is issued.
        The queue is fresh per command, but a user (or phone) can send a
        reply DURING _first_run_setup/_resolve_current_speaker -- before the
        first real prompt -- and _await_reply would then pop that stale frame
        as the prompt's answer. Draining at each interactive entry point
        prevents a pre-sent frame from satisfying (e.g.) a challenge prompt."""
        q = self._active_command_queue
        if q is not None:
            while not q.empty():
                try:
                    q.get_nowait()
                except asyncio.QueueEmpty:
                    break

    async def _await_reply(self, patient: bool = False):
        """Wait for the reader to hand this command its next non-command frame.

        SILENCE IS ANSWERED WITH A QUESTION, NOT A VERDICT (the owner's
        design, 2026-08-26). When the window expires, the client is sent a
        `still_there` frame -- the app shows "Still there?" -- and gets
        STILL_THERE_GRACE more to react. ANY frame proves presence: a
        `still_here` tap restarts the full window and the original question
        goes on waiting; a real answer inside the grace is simply the
        answer. Only silence through window AND grace ends the flow --
        CancelledByUser -> the ribbon's cancelled path, home screen,
        progress checkpointed where a record exists. A person confirming
        can keep a question open all evening; an abandoned phone cannot
        pin the shell lock forever (ping_timeout is off, so this backstop
        is the only one).

        `patient` is set by the ASKING CODE for a question whose answer is
        not even in the room (the recovery phrase in a drawer, a board
        being walked to) and stretches the leash before the ask. See
        PATIENT_PROMPT_TIMEOUT."""
        # The queue THIS flow owns, read once: the attribute is shared by the
        # connection and must never be re-read mid-wait.
        q = self._active_command_queue
        if q is None or self._take_cancel():
            raise CancelledByUser()
        if patient:
            timeout = self.PATIENT_PROMPT_TIMEOUT or None  # None = no bound
        elif self._bootstrapping:
            timeout = self.SETUP_PROMPT_TIMEOUT
        else:
            timeout = self.PROMPT_TIMEOUT
        while True:
            try:
                reply = await asyncio.wait_for(q.get(), timeout=timeout)
            except asyncio.TimeoutError:
                # Ask, don't eject. An older app that ignores the frame
                # changes nothing: it stays silent and the grace expires,
                # which is the old behaviour on a longer leash.
                try:
                    await self._send_json({
                        "type": "still_there",
                        "grace": self.STILL_THERE_GRACE,
                    })
                except websockets.exceptions.ConnectionClosed:
                    raise CancelledByUser() from None
                try:
                    reply = await asyncio.wait_for(q.get(),
                                                   timeout=self.STILL_THERE_GRACE)
                except asyncio.TimeoutError:
                    raise CancelledByUser() from None
            if isinstance(reply, dict) and reply.get("type") == "still_here":
                # Presence proven; the question earns a fresh full window.
                # Consumed HERE so _recv_answer never counts it against its
                # stale-frame budget.
                continue
            if isinstance(reply, dict) and reply.get("type") == "cancel":
                self._take_cancel()
                raise CancelledByUser()
            return reply

    # ── Auth ──────────────────────────────────────────────────────────────

    async def _authenticate(self) -> bool:
        """Prove who is on the other end. TWO ways in, and they are not equal.

        A PAIRED PHONE signs "<nonce>:<timestamp>:<spki>" with its own
        Ed25519 private key and names itself with 'phone_id'. The hub checks
        the signature against the public key it stored at pairing. This
        grants a full session.

        A PHONE WITH NO KEYPAIR YET sends an HMAC of the same string, keyed
        with the shared API key. This grants an ENROLLMENT session and
        nothing else -- see _run_enrollment_session. It exists so a new
        phone can register a keypair, and it deliberately cannot be used to
        drive the house, because otherwise revoking a phone would mean
        nothing: a removed phone still knows the API key.

        The <spki> in the signed string is this hub's own public key
        fingerprint, and it is NEVER SENT in either direction. Each side
        supplies it from what it holds -- see own_spki_fingerprint().
        """
        try:
            msg = await self._recv_json()
        except websockets.exceptions.ConnectionClosed:
            self._log_auth("closed_before_auth")
            self._no_attempt = True     # hung up without trying: not a strike
            return False
        if msg is None:
            self._log_auth("malformed_json")
            await self._fail("malformed_json", "auth frame was not valid JSON", "auth_failed")
            return False
        if msg.get("type") != "auth":
            self._log_auth("expected_auth_first")
            await self._fail(
                "expected_auth_first", "first frame after the challenge must be type 'auth'",
                "auth_failed")
            return False

        features = msg.get("features")
        if isinstance(features, list):
            self._features = frozenset(str(f) for f in features)
        # bool is a subclass of int; exclude it explicitly so True isn't a timestamp.
        ts = msg.get("timestamp")
        if isinstance(ts, bool) or not isinstance(ts, int):
            await self._fail(
                "bad_timestamp", "'timestamp' must be an integer of whole seconds since the epoch",
                "auth_failed")
            return False
        if abs(time.time() - ts) > self.AUTH_MAX_SKEW:
            # A HUB THAT KNOWS ITS OWN CLOCK IS WRONG (a power cut, no time
            # server -- see hub_clock) lets a plausible timestamp through:
            # the nonce below still makes a replayed frame useless, and a
            # paired phone's signed time is about to fix the clock.
            if _CLOCK is not None and _CLOCK.tolerates(ts):
                self._log_auth("skew_tolerated_clock_suspect")
            else:
                self._log_auth("stale_timestamp")
                await self._fail(
                    "stale_timestamp",
                    f"timestamp is outside the {self.AUTH_MAX_SKEW}s skew window -- check the client clock",
                    "auth_failed")
                return False

        # The one string both paths cover. Channel-bound: a relay attack
        # fails here rather than anywhere subtler.
        try:
            signed_message = _phones.signed_message(
                self._auth_nonce, ts, own_spki_fingerprint())
        except Exception as exc:                        # noqa: BLE001
            # No certificate, unreadable certificate -- refuse rather than
            # silently drop the binding and accept the connection anyway.
            self._log_auth("no_own_certificate")
            await self._fail(
                "server_misconfigured",
                f"the hub cannot read its own certificate ({exc})", "auth_failed")
            return False

        # THE SAME STRING OVER THE CERTIFICATE THIS LISTENER SERVED, for a
        # phone that is pairing: it hashed the certificate it was shown,
        # which on the tailnet is not local.crt. None on the local roads.
        served = served_spki_fingerprint(self.websocket)
        served_message = (_phones.signed_message(self._auth_nonce, ts, served)
                          if served else None)

        signature = msg.get("signature")
        phone_id = msg.get("phone_id")
        if isinstance(signature, str) and isinstance(phone_id, str):
            return await self._auth_as_phone(phone_id, signature, signed_message, ts)

        client_hmac = msg.get("hmac")
        if isinstance(client_hmac, str) and client_hmac:
            return await self._auth_for_enrollment(client_hmac, signed_message, served_message)

        await self._fail(
            "missing_proof",
            "auth frame needs either 'phone_id' + 'signature' (a paired "
            "phone) or 'hmac' (a phone asking to pair)", "auth_failed")
        return False

    async def _auth_as_phone(self, phone_id: str, signature: str,
                             signed_message: str, ts: int) -> bool:
        """A registered phone, signing with its own key."""
        if not PHONES.verify(phone_id, signature, signed_message):
            # ONE outcome for every failure -- unknown phone, malformed
            # signature, wrong signature. Telling them apart would tell an
            # attacker which half to keep working on. The log below is for
            # the owner, who can see the phone id; the client is told
            # nothing it did not already know.
            self._log_auth(f"bad_signature (phone {phone_id[:8]})")
            await self._fail(
                "bad_signature",
                "this phone is not registered, or the signature did not "
                "match. If it was removed from the hub, pair it again.", "auth_failed")
            return False
        self._auth_mode = "phone"
        self._phone_id = phone_id
        # THE FIRST PAIRED PHONE AFTER A POWER CUT SETS THE CLOCK. Its
        # timestamp was inside the string its key just signed, so it is as
        # trustworthy as the phone itself -- and a phone that can open the
        # door gains nothing by being allowed to set the time.
        if _CLOCK is not None and _CLOCK.suspect:
            await asyncio.to_thread(_CLOCK.adopt, ts,
                                    source=f"phone {phone_id[:8]}")
        # shell.current_phone_id is NOT set here: this runs outside the shell
        # lock, so it used to overwrite the id of whichever connection was
        # mid-action. It is set when a flow starts, in _handle_command/
        # _handle_menu/_handle_tailscale_join/_bootstrap_enrollment, and
        # cleared by _reset_shell_state -- in this phone's own Conversation.
        # Off the loop (a file lock and an fsync), and never fatal: last_seen
        # is bookkeeping, and an unwritable registry must not refuse a phone
        # whose signature just checked out.
        try:
            await asyncio.to_thread(PHONES.touch, phone_id)
        except Exception as exc:                        # noqa: BLE001
            self._log_auth(f"last_seen not recorded ({exc})")
        record = PHONES.get(phone_id) or {}
        # The PHONE's name (typed at pairing) and the PERSON it belongs to
        # -- two different things, and printing only the first made a
        # renamed person look like a mix-up ("ok as 'Tony Stark'" followed
        # by "speaker='Bruce Wayne'").
        _person = str(record.get("user_id") or "").strip()
        self._log_auth(f"ok as phone '{record.get('name', phone_id[:8])}'"
                       + (f", person '{_person}'" if _person else ""))
        # WHICH PERSON this handset belongs to, sent at the door.
        #
        # Not the same as record["name"], which is what the person calls the
        # HANDSET ("mum's phone"); this is the enrolled person the binding
        # names, written only after a PIN or a completed enrolment proved
        # who holds it. The drawer's nameplate says it out loud, which is a
        # question the app could not answer at all before -- and it has to
        # be answerable before anything is tapped, so it rides on auth_ok
        # rather than waiting for a profile fetch that only happens once
        # somebody opens the screen.
        await self._send_json({
            "type": "auth_ok",
            "phone_id": phone_id,
            "user_id": PHONES.user_for(phone_id) or "",
            # THE NAME THIS HUB ANSWERS TO AWAY FROM HOME, told on every
            # connect rather than only at the moment of joining.
            #
            # The app learned the tailnet name from the `tailscale_joined`
            # result and NOWHERE ELSE, so a phone that was not the one
            # holding the key -- or that reinstalled, or joined the house
            # afterwards -- kept two AP addresses and had no way in from
            # outside, for ever. Her report, twice: "in the app no address
            # of the tailscale", and "i cant connect to the tailnet".
            #
            # Empty when this hub has no tailnet, and additive, so an older
            # app simply ignores it. The app APPENDS it and never reorders:
            # the addresses that already work stay ahead of it in the dial
            # order, so a name that cannot resolve costs nothing until
            # everything else has already been tried.
            # Live, else the ones the hub last had (it may have no
            # internet right now): see _remember_tailnet.
            "tailnet": config.WS_HOSTNAME or (await self._known_tailnet())[0],
            # AND THE ADDRESS, which is the road that actually works. The
            # name above needs the phone's resolver, the phone's root store
            # and the Let's Encrypt chain on the listener all to agree; on
            # the first real phone (2026-09-09) none did, the IP did, and
            # it had to be typed into Server addresses by hand -- and typed
            # AGAIN on the Windows bench, whose address is different. Read
            # from the live bindings, so it is an address genuinely
            # answering right now; "" when no tailnet listener is up. The
            # app files it ahead of the name and behind the local roads.
            "tailnet_ip": bound_tailnet_ip() or (await self._known_tailnet())[1],
            # Which Tailscale account the hub is signed in with ("" when
            # none): Settings > Remote access shows it.
            "tailnet_account": (await self._remote_access()).get("account", ""),
            # on | paused | off | expired | not_set_up | ...: what the app's
            # Remote access sheet says.
            "tailnet_state": (await self._remote_access()).get("state", ""),
            # THIS HUB ANSWERS link_ping (the app's connection gauge). Told
            # here so a newer app never sends one to an older hub, where an
            # unknown frame is taken for the answer to an open question.
            "link": True,
        })
        return True

    async def _auth_for_enrollment(self, client_hmac: str,
                                   signed_message: str,
                                   served_message: Optional[str] = None) -> bool:
        """The shared API key. Buys the right to register a keypair, nothing
        more. `served_message` is the same string over the certificate this
        listener actually presented (the tailnet's), when that is not
        local.crt; either binding is accepted, because either is a
        certificate this hub really served on this very connection."""
        secret = os.environ.get("HOMEVOICE_API_KEY") or ""
        if not secret:
            self._log_auth("no_api_key_set")
            await self._fail(
                "server_misconfigured",
                "no HOMEVOICE_API_KEY is set on the hub, so no new phone can "
                "be paired", "auth_failed")
            return False

        # Compared as BYTES: compare_digest raises TypeError on a str holding
        # any non-ASCII character, and a client-supplied "hmac" can hold
        # anything -- that TypeError escaped run() before _auth_strike, so a
        # peer could fail forever without ever being throttled.
        offered = client_hmac.lower().encode("utf-8")
        matched = False
        for message in (signed_message, served_message):
            if message is not None:
                expected = hmac.new(secret.encode("utf-8"), message.encode("utf-8"),
                                    hashlib.sha256).hexdigest()
                # constant-time, and both compared whichever matches
                matched = hmac.compare_digest(offered, expected.encode("ascii")) or matched
        if not matched:
            self._log_auth("bad_hmac")
            await self._fail(
                "bad_hmac",
                "HMAC did not match -- wrong key, or the hub's certificate "
                "is not the one this phone was shown", "auth_failed")
            return False

        # The window is NOT checked here any more -- it is checked at the
        # pair frame, where the public key is known. A phone the hub
        # already trusts may re-pair at any time (same key, same id, same
        # owner -- it is only re-fetching the certificate), and demanding
        # --pair for that made every app reinstall a trip to the hub's
        # keyboard. An UNKNOWN key still needs the window, checked below.

        self._auth_mode = "enroll"
        self._log_auth("ok for enrollment")
        await self._send_json({
            "type": "auth_ok",
            "mode": "enroll",
            "phones_registered": PHONES.count(),
            # THE WINDOW, TOLD TO THE ONE PHONE THAT CANNOT HEAR IT ANY
            # OTHER WAY. pair_window frames go to authenticated phones, and
            # the phone being paired is by definition not one yet -- so the
            # countdown every paired phone gets was invisible to exactly the
            # phone standing at the door. Carried in auth_ok because this
            # exchange is the only conversation the two ever have: additive,
            # so an older app simply ignores the field.
            "pair_window_seconds": PHONES.window.seconds_left(),
            # Open at all (an always-open window has no seconds): the pair
            # screen says "Hub pairing is open" from this.
            "pair_window_open": PHONES.window.is_open(),
        })
        return True

    def _log_auth(self, outcome: str) -> None:
        """Every authentication attempt, with its source, on the console.

        This system opens locks. Failed attempts used to leave no trace at
        all -- no address, no timestamp, no count -- so there was no way to
        answer 'has anyone been trying?' after the fact."""
        event("AUTH", f"{self._peer()} -> {outcome}")

    # ── Command lifecycle ─────────────────────────────────────────────────

    def _reset_shell_state(self) -> None:
        """Return the shared shell to a clean per-command baseline.

        Called before and after every command. Everything here is state that
        the CLI can safely carry for the life of a process but that must NOT
        cross between commands, users, or connections on the server.

        `running` is NOT reset: _first_run_setup() still clears it, but only
        the terminal's loop (InteractiveShell.run) reads it, and that loop
        never runs in this process since the "setup_incomplete" reply went.

        `_io` is reset to NullIO rather than None or TerminalIO.
        InteractiveShell awaits self._io.write() from a dozen places, so None
        turns any stray write into an AttributeError. TerminalIO was worse
        than it looked: its ainput() reads the process's stdin, which on a
        daemon blocks a worker thread forever, and it is trusted_local=True,
        so a stray prompt-and-gate between connections would have run under
        terminal trust. NullIO writes to the console and refuses questions.
        """
        self.shell.current_speaker_id = None
        self.shell._io = NullIO()
        # Per-connection identity and per-action notes. None of these were
        # reset before, so the Profile screen showed the last-authenticated
        # phone to whoever asked next, and a checkpoint_note left behind by a
        # dropped connection rode the next connection's unrelated result.
        self.shell.current_phone_id = None
        self.shell.checkpoint_note = None
        self.shell.failure_note = None

    def _claim_shell(self) -> None:
        """Baseline + this connection's identity, in its own Conversation."""
        self._reset_shell_state()
        self.shell.current_phone_id = self._phone_id
        self.shell._io = WebSocketIO(self.websocket, self._send_json,
                                     self._await_reply,
                                     drain=self._drain_replies)
        self.shell._io.can_send_files = "files" in self._features
        # The shell's checkpoints (raise_if_cancelled) ask THIS connection.
        self.shell._io.is_cancelled = self._take_cancel
        # ...and wait, before a device is touched, while something said
        # over this command is still being read (_wait_hold).
        self.shell._io.wait_hold = self._wait_hold

    def _is_unbound_phone(self) -> bool:
        """A handset that proved a keypair but no person. It may enrol or
        log in; it may not drive the house or its settings."""
        return not PHONES.user_for(self._phone_id)

    def _flow_running(self) -> bool:
        return self._command_task is not None and not self._command_task.done()

    #: Menu actions the app sends BY ITSELF on connect (its
    #: _backgroundActions): read-only, and they ask nothing. A command
    #: spoken the moment the app connects used to be refused "busy" behind
    #: one; it now runs beside it (a command takes no shell lock).
    QUIET_ACTIONS = frozenset({"check_notices"})

    def _busy(self) -> bool:
        """A flow is running that a new request must not start beside."""
        return self._flow_running() and not self._quiet_flow

    def _spawn_flow(self, coro: Any, own_queue: bool = True,
                    quiet: bool = False) -> asyncio.Task:
        """Start a flow AS A TASK, recorded in _command_task so every busy
        check sees it. With `own_queue` the reply queue is installed BEFORE
        dispatching, so a fast reply arriving before the task's first line
        isn't dropped. There is no await between creating the task and
        recording it as the owner."""
        if self._flow_running():
            # Only a quiet read gets here (see _busy). It finishes on its
            # own, in a Conversation of its own; run() still cancels it if
            # the phone leaves first.
            self._background.add(self._command_task)
            self._command_task.add_done_callback(self._background.discard)
        self._cancel_requested = False     # a new flow starts clean
        if quiet:
            # A QUIET READ GETS A CONVERSATION OF ITS OWN. A command no
            # longer waits for it on the shell lock, so the two can run at
            # once on this connection -- and sharing this phone's
            # Conversation, each one's baseline reset would blank the
            # other's questions and notes mid-flow.
            coro = _in_own_conversation(coro)
        task = asyncio.ensure_future(coro)
        if own_queue:
            self._install_queue(task)
        self._command_task = task
        self._quiet_flow = quiet
        task.add_done_callback(self._on_command_done)
        return task

    async def _take_lock(self, kind: str = "result",
                         refused: Optional[str] = None) -> bool:
        """The shell lock -- for setup and management flows only (commands
        and the read-only doors take none: see WHO WAITS FOR WHOM). Fail
        fast if another such flow holds it -- a 2nd user gets 'server_busy'
        instead of a silent stall. A refused `result` names
        the request (see _fail): with a quiet read queued ahead of a
        request, an unnamed refusal lowered the other one's flags too."""
        if await self._acquire_lock(self.LOCK_ACQUIRE_TIMEOUT):
            return True
        await self._fail("server_busy",
                         "another command is still running -- try again shortly",
                         kind, refused)
        return False

    async def _seat_speaker(self) -> Optional[str]:
        """Who this flow acts for.

        THE HANDSET ALREADY KNOWS WHOSE IT IS.

        _resolve_current_speaker auto-picks when exactly one person is
        enrolled and otherwise ASKS "Who is speaking?" -- which on a phone is
        both a question before every command once a second person exists,
        and a SELF-DECLARATION where a stronger answer was already on file.
        The pairing record is that answer: it was written after a PIN or a
        completed enrolment proved who holds this handset (see
        phones.bind_user). On the menu path the question also took a typed
        name as the answer -- a self-declaration that then unlocked the gate
        grace window of whoever was named.

        Seeding it here does not weaken anything. Voice verification still
        runs against this person's own template and still refuses a
        different voice; what changes is that the hub stops asking a
        question it can answer, and stops accepting a typed claim as the
        answer to it. The CLI keeps the picker -- a terminal has no handset
        to ask."""
        bound = PHONES.user_for(self._phone_id)
        if bound:
            self.shell.current_speaker_id = bound
        await self.shell._resolve_current_speaker()
        return self.shell.current_speaker_id

    def _end_flow(self, speaker_id: Optional[str] = None,
                  keep_context: bool = False) -> None:
        """Drop the back-reference history along with the identity it
        belongs to, so the next connection's "turn it off" can't resolve
        against this one's devices -- then the clean baseline.

        `or _ws_last_speaker`: a flow that died before
        _resolve_current_speaker() leaves speaker_id None, and clearing
        nothing there left the PREVIOUS user's group in a shell that
        outlives every connection. The last known id is this phone's
        (Conversation): with commands side by side, a shared one let one
        phone's failed flow clear ANOTHER phone's back-reference."""
        stale = speaker_id or getattr(self.shell, "_ws_last_speaker", None)
        if speaker_id:
            self.shell._ws_last_speaker = speaker_id
        # A VOICE COMMAND KEEPS ITS BACK-REFERENCE (2026-10-01). Clearing it
        # here, after EVERY command, meant "switch it off" right after "turn
        # on the bulb" had nothing to refer to: it resolved to no_match and
        # the phone said "the command did not complete". The context is
        # per speaker and expires on its own (CONTEXT_TTL_SECONDS), so
        # another person's "it" cannot reach these devices; only the other
        # flows (menu actions, setup) still start from a blank slate.
        if not keep_context:
            _clear_speaker_context(self.shell, stale)
        self._reset_shell_state()

    async def _navigation_for(self, text: Any, ui: Any) -> Optional[Dict[str, Any]]:
        """nlu.app_nav's verdict, with the house's device names so a label
        that is also a device is only ever picked by an explicit select
        verb. Never raises: a navigation module that throws must not take
        a house command down with it.

        The match runs IN A THREAD: its semantic pass embeds the utterance
        with the NLU model, and inference on the event loop froze every
        connection (and every watcher) for as long as it took. The device
        names are read here, on the loop, so the thread never walks a dict
        a running command may be changing."""
        if ui is None:
            return None
        try:
            from nlu.app_nav import match_navigation
            names = [d.get("name", "") for d in self.shell.dm.devices.values()
                     if isinstance(d, dict)]
            # Scene names and triggers: a scene the owner fires by saying
            # "home" must not be swallowed as "go to the home screen".
            phrases: List[str] = []
            sm = self.shell.sm
            try:
                for sc in (sm.list_scenes() if sm is not None else ()):
                    phrases.extend(p for p in (getattr(sc, "name", ""),
                                               getattr(sc, "trigger", ""))
                                   if isinstance(p, str) and p.strip())
            except Exception as exc:               # noqa: BLE001
                event("UI", f"scene names unavailable for navigation: {exc}")
            return await asyncio.to_thread(match_navigation, text, ui, names,
                                           phrases)
        except Exception as exc:                   # noqa: BLE001
            event("UI", f"navigation check failed: {exc}")
            return None

    async def _handle_command(self, msg: dict) -> None:
        self._pre_lock = True
        self._asides, self._aside_free = [], True
        self._hold_released()
        # WHERE THIS COMMAND'S TIME GOES, one TIMING line at its end.
        _t0 = time.perf_counter()
        _times = times_begin()
        try:
            text = msg.get("text", "")
            audio_b64 = msg.get("audio")
            if not isinstance(text, str) or (audio_b64 and not isinstance(audio_b64, str)):
                await self._fail("bad_command_frame", "'text' and 'audio' must be strings")
                return
            audio_format = msg.get("audio_format", "wav")
            if not isinstance(audio_format, str):
                audio_format = "wav"
            if audio_b64 and len(audio_b64) > self.MAX_AUDIO_B64_LEN:
                await self._fail("audio_too_large",
                                 "recording exceeded the size limit -- record a shorter clip")
                return

            # WORDS ABOUT THE SCREEN, NOT THE HOUSE. "Open automations", "go
            # back", "scroll down", "select kitchen night": when the phone
            # sent a `ui` context and the whole utterance is navigation, the
            # answer is a `ui` frame the app carries out on its own
            # allowlist. Decided BEFORE the shell lock and the voice gate:
            # nothing here touches a device, so nothing here needs a voice,
            # and a screen change should not wait behind a lock nobody needs.
            # The ordering of the two frames matters: the result first, so
            # the flow is idle by the time the app acts on the `ui` frame
            # (opening Devices sends a menu action of its own).
            # SAY WHAT THE PHONE SENT, every time (2026-09-11). Until now
            # a command with a screen context left no trace unless it
            # matched a navigation phrase, so "the dock does nothing on any
            # screen" could not be told apart from "the phone sent no
            # context": one line names the screen, how many labels and
            # actions rode along, and -- when the words then fall through
            # to the house path -- that they did.
            ui_ctx = msg.get("ui")
            if isinstance(ui_ctx, dict):
                # len() only of real lists: this is a trace of an untrusted
                # frame, and `"targets": 7` used to fail the whole command.
                targets, actions = ui_ctx.get("targets"), ui_ctx.get("actions")
                event("UI", f"context: screen={ui_ctx.get('screen')!r} "
                            f"targets={len(targets) if isinstance(targets, list) else 0} "
                            f"actions={len(actions) if isinstance(actions, list) else 0} "
                            f"for {text!r}")
            else:
                event("UI", f"no screen context with {text!r} (home orb, "
                            f"navigation off, or a screen that sends none)")
            nav = await self._navigation_for(text, ui_ctx)
            if nav is None and isinstance(ui_ctx, dict):
                event("UI", f"{text!r} is not navigation -> house command path")
            if nav is not None:
                event("UI", f"{text!r} -> {nav.get('action')} "
                            f"{nav.get('screen') or nav.get('label') or nav.get('direction') or nav.get('menu') or ''}"
                            + (f" via semantic score={nav.get('score')} margin={nav.get('margin')}"
                               if nav.get('via') == 'semantic' else ''))
                await self._send_json({
                    "type": "result", "ok": True,
                    "reason": f"ui:{nav.get('action')}",
                    "text": str(nav.get("say", "")),
                })
                await self._send_json(nav)
                return

            # The clip is decoded only for the house path: navigation never
            # reads it, and decoding first put that cost in front of every
            # "go back".
            audio = None
            if audio_b64:
                try:
                    raw = base64.b64decode(audio_b64, validate=True)
                except Exception:
                    await self._fail("invalid_audio_encoding", "audio was not valid base64")
                    return
                _t_decode = time.perf_counter()
                audio = await asyncio.to_thread(decode_audio_bytes, raw, audio_format)
                # FIX: one branch, not two -- the old shape tested
                # `audio is None` twice in a row (log in the first, respond
                # in the second).
                if audio is None:
                    event("AUDIO", f"decode FAILED for {len(raw)} bytes "
                                   f"({audio_format})")
                    await self._fail("audio_decoding_failed",
                                     "server could not decode the audio -- expected 16-bit PCM WAV")
                    return
                event("AUDIO", f"command audio decoded: {describe_audio(audio)} "
                               f"in {time.perf_counter() - _t_decode:.2f}s")

            times_add("before", time.perf_counter() - _t0)
            # NO SHELL LOCK FOR A COMMAND (2026-10-11). It used to wait here
            # -- up to LOCK_ACQUIRE_TIMEOUT, then "busy" -- for ANY other
            # phone's command to finish, questions and voice checks
            # included. This phone's state is its own now (Conversation), so
            # the only thing that waits is a model's forward pass, for as
            # long as that pass takes. See the module's WHO WAITS FOR WHOM.
            _enter_command()
            self._pre_lock = False

            speaker_id = None
            try:
                # Clean baseline BEFORE the command, not just after: the
                # previous command may have died in a way that skipped its own
                # cleanup, and a stale `running=False` from any earlier
                # connection would otherwise fail this one for no reason.
                self._claim_shell()
                try:
                    # FIRST-RUN ENROLMENT IS NOT DONE HERE ANY MORE. It moved
                    # to _bootstrap_enrollment, and _dispatch_command_frame
                    # refuses every command frame while no user exists, so by
                    # the time this runs there is always at least one -- which
                    # made _first_run_setup() a no-op and the
                    # "setup_incomplete" reply unreachable. Removed rather
                    # than left sitting under the new path.
                    speaker_id = await self._seat_speaker()
                    # Cancel pressed while this command waited its turn.
                    self.shell._io.raise_if_cancelled()
                    _t_run = time.perf_counter()
                    self.shell.last_multi_outcome = {}
                    self.shell.phone_now = _phone_clock(msg.get("now"))
                    self.shell.command_action = {}
                    try:
                        accomplished = await self.shell._run_multi(
                            text, speaker_id, audio=audio
                        )
                    except CancelledByUser:
                        # A CORRECTION stopped it ("no, the fan"): what was
                        # said runs in its place, below. A plain cancel
                        # ends the command, as always.
                        if not any(a["replace"] for a in self._asides):
                            raise
                        event("CMD", f"{text!r} stopped for a correction")
                        accomplished = False
                        self.shell.last_multi_outcome = {}
                    accomplished = await self._run_asides(
                        speaker_id, accomplished)
                    event("RESULT", f"{text!r} -> ok={accomplished} "
                                    f"({time.perf_counter() - _t_run:.2f}s)")
                    # PER-CHUNK detail when the utterance was split. A single
                    # ok=false for "turn on the ac and lock the door" tells the
                    # person their sentence failed while half of it worked, and
                    # leaves them guessing which half.
                    outcome = self.shell.last_multi_outcome or {}
                    frame: Dict[str, Any] = {"type": "result", "ok": accomplished}
                    if outcome.get("done") or outcome.get("not_done"):
                        frame["parts"] = outcome
                    if not accomplished:
                        frame["reason"] = "not_completed"
                        # THE REASON, NOT THE FILLER (her ask, 2026-10-08:
                        # "instead of saying the command did not complete,
                        # let it state the reason"). failure_note is set by
                        # whatever knew why -- a board that is offline, a
                        # PIN refused -- and is what the phone says.
                        note = (self.shell.failure_note or "").strip()
                        frame["text"] = (
                            note if note
                            else "did not do: " + "; ".join(outcome["not_done"])
                            if outcome.get("not_done")
                            else "the command did not complete")
                    await self._send_json(frame)
                except CancelledByUser:
                    await self._fail("cancelled",
                                     "cancelled (or timed out waiting for your reply)")
                except Exception:
                    traceback.print_exc()
                    await self._fail("internal_error",
                                     "internal server error -- see server logs")
                finally:
                    self.shell.phone_now = None
                    self._end_flow(speaker_id, keep_context=True)
            finally:
                _took = time.perf_counter() - _t0
                if _took >= 0.3:        # not for words ignored in an instant
                    event("TIMING", f"{text!r}: " + times_line(_times, _took)
                          + f" [{_COMMANDS_RUNNING} running]")
                _leave_command()

        except websockets.exceptions.ConnectionClosed:
            pass
        except Exception:
            traceback.print_exc()
            try:
                await self._fail("fatal_error", "fatal server error -- see server logs")
            except Exception:
                pass
        finally:
            self._pre_lock = False
            self._hold_released()

    async def _run_asides(self, speaker_id: Optional[str],
                          accomplished: bool) -> bool:
        """What was said over the command, run after it under the same lock
        and answered in the same result: an addition takes the command's
        own action (shell.carry_slots), a correction replaces it."""
        merged = dict(self.shell.last_multi_outcome or {})
        while self._asides:
            aside = self._asides.pop(0)
            self.shell.carry_slots = dict(self.shell.command_action or {})
            self.shell.free_ride = aside["free"]
            self.shell.last_multi_outcome = {}
            try:
                ok = await self.shell._run_multi(aside["text"], speaker_id,
                                                 audio=aside["audio"])
            finally:
                self.shell.carry_slots = {}
                self.shell.free_ride = False
            accomplished = ok if aside["replace"] else (accomplished or ok)
            for key in ("done", "not_done"):
                got = (self.shell.last_multi_outcome or {}).get(key) or []
                if got:
                    merged[key] = list(merged.get(key) or []) + list(got)
        self.shell.last_multi_outcome = merged
        return accomplished

    def _on_command_done(self, task: asyncio.Task) -> None:
        """Retire this task's reply queue and surface any exception so asyncio
        doesn't warn 'Task exception was never retrieved'.

        Ownership-checked. Clearing unconditionally let a finishing task
        destroy a DIFFERENT task's queue -- and the reader drops replies when
        the queue is None, so the surviving flow went deaf and timed out
        with a message blaming the user for not answering."""
        self._release_queue(task)
        if self._command_task is task:
            self._command_task = None
            self._pre_lock = False
        try:
            task.result()
        except asyncio.CancelledError:
            pass
        except Exception:
            traceback.print_exc()

    async def _reader_loop(self) -> None:
        """Single owner of websocket.recv() for this connection."""
        while True:
            msg = await self._recv_json()
            if msg is None:
                try:
                    await self._fail("malformed_json", "frame was not a valid JSON object", "error")
                except websockets.exceptions.ConnectionClosed:
                    pass
                continue

            # REVOCATION IS CHECKED PER FRAME, not only at the door. The
            # signature was verified once, at connect -- and with
            # ping_timeout=None a session lives as long as its socket, so a
            # phone removed from the registry (from another phone, or from
            # setup_manager in another process) could go on driving the
            # house for days. PHONES.get() re-reads the file only when its
            # mtime moved, so this is one stat() per frame.
            if PHONES.get(self._phone_id) is None:
                self._log_auth(f"revoked mid-session (phone {self._phone_id[:8]})")
                await self._hang_up("revoked", "this phone was removed from the "
                                               "hub -- pair it again to reconnect")
                return

            try:
                await self._route_frame(msg)
            except (asyncio.CancelledError,
                    websockets.exceptions.ConnectionClosed):
                raise
            except Exception:                          # noqa: BLE001
                # ONE BAD FRAME MUST NOT END THE SESSION. A field of the
                # wrong type (an "audio" that is a number, say) used to raise
                # straight out of the reader, which closed the connection --
                # and cancelled whatever flow was running on it.
                traceback.print_exc()
                try:
                    await self._fail("bad_frame", "that frame could not be handled",
                                     "error")
                except websockets.exceptions.ConnectionClosed:
                    pass

    async def _route_frame(self, msg: dict) -> None:
        """Hand one inbound frame to whatever it belongs to."""
        # The gauge's ping, every few seconds: answered at once, off the
        # shell lock, and not traced -- it is not something anyone did.
        # A PHONE IS HERE: both models are kept warm (see the shell). From
        # the first frame a phone sends, and while one stays connected --
        # its ping every few seconds counts, so a phone left open and silent
        # for ten minutes still gets a warm first command ("it is still slow
        # after five minutes", 2026-10-05) -- up to half an hour after the
        # last thing anybody did; a ping alone does not count past that.
        try:
            self.shell._keep_speaker_model_warm(
                active=msg.get("type") != "link_ping")
        except Exception as exc:                       # noqa: BLE001
            event("VOICE", f"model warm-up not started: {exc}")
        if msg.get("type") == "link_ping":
            await self._dispatch_link_ping(msg)
            return
        # Not traced: the app's notices check, every 45 s while it is open.
        if not (msg.get("type") == "menu"
                and msg.get("action") == "check_notices"):
            self._trace_frame(msg)

        dispatch = {"command": self._dispatch_command_frame,
                    "menu": self._dispatch_menu_frame,
                    "tailscale_join": self._dispatch_tailscale_join,
                    "tailscale_cancel": self._dispatch_tailscale_cancel,
                    "tailscale_logout": self._dispatch_tailscale_logout,
                    "match_request": self._dispatch_match_request,
                    "cancel": self._dispatch_cancel,
                    "hold": self._dispatch_hold,
                    "release": self._dispatch_release,
                    "aside": self._dispatch_aside,
                    }.get(msg.get("type"))
        if dispatch is not None:
            await dispatch(msg)
        else:
            q = self._active_command_queue
            if q is not None:
                try:
                    q.put_nowait(msg)
                except asyncio.QueueFull:
                    # Visible, not silent: a dropped reply is the shape of
                    # a client bug, and dropping it quietly is how the
                    # question it was meant to answer ends up "timing out".
                    event("FRAME", f"reply queue full -- DROPPED "
                                   f"{msg.get('type')!r} frame")
            else:
                event("FRAME", f"{msg.get('type')!r} arrived with no question "
                               f"outstanding -- ignored")

    #: A round trip, graded. The same lines the app draws its bars by
    #: (socket_client.dart, LinkQuality): under 150 ms, under 400, under a
    #: second, and anything slower.
    LINK_GRADES = ((150, "excellent"), (400, "good"), (1000, "slow"))
    #: How often Tailscale is asked which road reaches this phone.
    LINK_PATH_REFRESH_S = 30.0

    @classmethod
    def _link_grade(cls, ms: Optional[int]) -> str:
        if ms is None:
            return ""
        return next((name for limit, name in cls.LINK_GRADES if ms < limit), "poor")

    async def _dispatch_link_ping(self, msg: dict) -> None:
        """THE CONNECTION, GAUGED FROM BOTH ENDS (2026-10-03: Tailscale was
        up and the road was a relay in Paris, 0.9-1.7 s a round trip, and
        nothing on either side said so -- it only looked like "not
        working").

        The app sends link_ping every few seconds and times the answer:
        that is the phone's measure. The reply carries the HUB's measure --
        the round trip of its own keepalive ping to this phone, which the
        socket library sends every 20 s and times -- and which road
        Tailscale is using to reach the phone ("direct", "relay par"). The
        app draws its bars from these; the hub logs a line when the grade
        changes, so a connection that went bad is in the log with the time.
        Nothing here reads or changes the house."""
        ident = msg.get("id")
        latency = float(getattr(self.websocket, "latency", 0) or 0)
        hub_ms = int(latency * 1000) if latency > 0 else None
        now = time.monotonic()
        if now - self._link_path_at >= self.LINK_PATH_REFRESH_S:
            self._link_path_at = now
            task = asyncio.ensure_future(self._refresh_link_path())
            self._background.add(task)
            task.add_done_callback(self._background.discard)
        await self._send_json({
            "type": "link_pong",
            "id": ident if isinstance(ident, int) else 0,
            "hub_rtt_ms": hub_ms,
            "path": self._link_path,
        })
        self._log_link(hub_ms)

    def _log_link(self, hub_ms: Optional[int]) -> None:
        """One line when the grade CHANGES, never one per ping."""
        grade = self._link_grade(hub_ms)
        if not grade or grade == self._link_grade_said:
            return
        self._link_grade_said = grade
        road = f", {self._link_path}" if self._link_path else ""
        event("LINK", f"{self._peer()}: {grade} ({hub_ms} ms{road})")

    async def _refresh_link_path(self) -> None:
        """Ask Tailscale which road reaches this phone. Off the loop (it
        runs `tailscale status`); a local address answers "" at once."""
        from core import tailnet as _tailnet
        try:
            path = await asyncio.to_thread(_tailnet.path_to, self._peer_ip())
        except Exception as exc:                       # noqa: BLE001
            event("LINK", f"could not ask Tailscale for the road: {exc}")
            return
        if path != self._link_path:
            self._link_path = path
            # A road that changed is worth a line whatever the grade.
            self._link_grade_said = ""

    #: A phone may not hand over an unbounded list to be matched. Generous
    #: next to any real screen (the pin map's is the longest at a few dozen)
    #: and small enough that the cost of matching is never worth thinking
    #: about.
    MAX_MATCH_OPTIONS = 200
    MAX_MATCH_LABEL = 120
    MAX_MATCH_HEARD = 500

    async def _dispatch_match_request(self, msg: dict) -> None:
        """Match spoken words against a list the SCREEN is drawing.

        THE LAST MATCHER ON THE PHONE (2026-09-11: "route every screen
        choices to the hub because i dont know maybe the code u made for
        dart is just useless"). A server `choice` already comes here as
        words; this is the same service for the lists no server question
        stands behind -- the pin map's pins and parts, a form's fields, the
        rule builder's blocks. The phone now owns no matching at all.

        ANSWERED OFF THE SHELL LOCK, like the `ui` navigation frame and for
        the same reason: this reads no device, writes nothing, and touches
        no state. It is a pure function of the words and the list the phone
        just sent, so making it queue behind a running flow would stall the
        very screens that are INSIDE one -- the pin map runs in the middle
        of Add device, holding the lock.

        Never raises: a matcher that can take a connection down with it is
        worse than one that says "no match".
        """
        request_id = msg.get("id")
        # Bounded like the labels: an 8 MB "heard" string scored against
        # 200 options would hold the event loop for every connection.
        heard = str(msg.get("heard") or "").strip()[:self.MAX_MATCH_HEARD]
        raw = msg.get("options")
        options: List[Dict[str, Any]] = []
        if isinstance(raw, list):
            for o in raw[:self.MAX_MATCH_OPTIONS]:
                if not isinstance(o, dict):
                    continue
                value = str(o.get("value") or "")
                label = str(o.get("label") or value)[:self.MAX_MATCH_LABEL]
                if value:
                    options.append({"value": value, "label": label})
        values: List[str] = []
        near: List[Dict[str, Any]] = []
        if heard and options:
            try:
                from nlu.choice_match import match as _match_spoken
                values, near = _match_spoken(
                    heard, options,
                    multi=bool(msg.get("multi")),
                    allow_all=bool(msg.get("allow_all")),
                    confirming=bool(msg.get("confirming")))
            except Exception as exc:                   # noqa: BLE001
                event("MATCH", f"failed for {heard!r}: {exc}")
        event("MATCH", f"screen list ({len(options)}) heard {heard[:40]!r} -> "
                       f"{len(values)} value(s)"
                       + (f", offering {len(near)}" if near else ""))
        frame = {
            "type": "match_result",
            "id": request_id,
            "values": values,
            "near": near,
        }
        # SPOKEN CANCEL, FLUID (2026-09-30). Asked only when nothing on the
        # list was named -- an answer always wins over a cancel, so a
        # device called "stop light" is still a device. The meaning layer
        # embeds (a blocking model call), so it runs in a thread, and as
        # its own task: the reader keeps reading while it thinks.
        if heard and not values and msg.get("cancel_check"):
            task = asyncio.ensure_future(self._answer_cancel_check(frame, heard))
            self._background.add(task)
            task.add_done_callback(self._background.discard)
            return
        await self._send_json(frame)

    async def _answer_cancel_check(self, frame: Dict[str, Any], heard: str) -> None:
        """Finish a match_result with `cancel`: did the words mean stop?"""
        from nlu.spoken_cancel import means_cancel
        clf = getattr(self.shell, "intent_classifier", None)

        def classify(text: str) -> Dict[str, Any]:
            return clf.classify(text, threshold=config.INTENT_MIN_SCORE,
                                margin_floor=config.INTENT_MIN_MARGIN)
        try:
            cancel = await asyncio.to_thread(
                means_cancel, heard, classify if clf else None)
        except Exception as exc:                        # noqa: BLE001
            event("MATCH", f"cancel check failed: {exc}")
            cancel = False
        event("MATCH", f"cancel check {heard[:40]!r} -> {cancel}")
        frame["cancel"] = bool(cancel)
        try:
            await self._send_json(frame)
        except websockets.exceptions.ConnectionClosed:
            pass

    def _trace_frame(self, msg: dict) -> None:
        """One line per inbound frame. Audio is reported by SIZE, never
        logged -- a base64 payload in a console trace is both useless and a
        recording of someone's voice sitting in a log file."""
        t = msg.get("type", "?")
        if t == "command":
            detail = f"text={msg.get('text', '')!r}"
            if msg.get("audio"):
                detail += f" audio={_b64_kb(msg['audio'])}"
            else:
                detail += " audio=none"
        elif t == "recording":
            if msg.get("cancelled"):
                detail = "cancelled"
            elif msg.get("audio"):
                detail = f"audio={_b64_kb(msg['audio'])} " \
                         f"fmt={msg.get('audio_format', 'wav')}"
            else:
                detail = "no audio"
        elif t == "menu":
            detail = f"action={msg.get('action')!r}"
        elif t == "choice_response":
            detail = ("cancelled" if msg.get("cancelled")
                      else f"values={msg.get('values')}")
        elif t == "prompt_reply":
            detail = f"{len(str(msg.get('value', '')))} chars"
        else:
            detail = ""
        event("FRAME", f"<- {t} {detail}".rstrip())

    def _take_cancel(self) -> bool:
        """Consume a pending Cancel: True ONCE, then False. One-shot on
        purpose -- a Cancel ends exactly what a cancelled question would
        end. Flows that answer a cancel by starting a step over (the
        registration ceremony) keep doing so; a sticky flag would make
        their re-ask cancel itself for ever. The wake-up frame left in the
        reply queue goes with it, so it cannot cancel the question after."""
        if not self._cancel_requested:
            return False
        self._cancel_requested = False
        q = self._active_command_queue
        if q is not None:
            kept = []
            while not q.empty():
                try:
                    frame = q.get_nowait()
                except asyncio.QueueEmpty:
                    break
                if not (isinstance(frame, dict) and frame.get("type") == "cancel"):
                    kept.append(frame)
            for frame in kept:
                q.put_nowait(frame)
        return True

    async def _dispatch_cancel(self, msg: dict) -> None:
        """CANCEL MEANS STOP, NOW (2026-09-30: "it's meant to send the
        cancel and then stop it immediately in the hub, so you can check
        another new one").

        Answered here, off the shell lock, the moment it arrives. A flow
        waiting on a question is woken and ends at once (a `cancel` in its
        reply queue); one that is busy thinking ends at its next question
        or checkpoint -- before it changes anything (shell._carry_out
        checks before every device write). The flow's own cancelled result
        then reaches the phone as usual. With nothing running, the phone is
        told so straight away and simply goes back to ready, instead of
        sitting on "cancelling..." waiting for a question that never comes.
        """
        running = self._flow_running() and not self._quiet_flow
        if running and self._pre_lock and self._command_task is not None:
            # Still reading the screen-navigation verdict, decoding, or
            # queued for the lock: nothing has been changed anywhere, so
            # the command is simply ended now (2026-09-30: a 85 s
            # navigation check could not be cancelled). A worker thread
            # already computing finishes on its own and is ignored.
            self._command_task.cancel()
            self._pre_lock = False    # a task that never ran clears nothing
            event("FLOW", "cancel from the phone -- command ended before it "
                          "started")
            await self._fail("cancelled", "cancelled")
            await self._send_json({"type": "cancel_ack", "running": False})
            return
        if running:
            self._cancel_requested = True
            q = self._active_command_queue
            if q is not None:
                try:
                    q.put_nowait({"type": "cancel"})
                except asyncio.QueueFull:
                    pass                  # the flag alone still stops it
            event("FLOW", "cancel from the phone -- stopping the running flow")
        await self._send_json({"type": "cancel_ack", "running": running})

    # ══ TALKING OVER A COMMAND (2026-10-09) ═════════════════════════════
    #
    # A spoken "cancel" used to lose every race: the phone had to hear the
    # pause after it and turn it into words, and by then the lamp was on.
    # Now the phone says `hold` the moment it hears speech while a command
    # is running, and the hub does not touch a device until the phone says
    # what it was (`aside`) or that it was nothing (`release`), or until
    # config.INTERRUPT_HOLD_MAX_S runs out. What it was, decided by rules,
    # never by meaning alone (the television would add devices):
    #
    #   * a cancel ("stop", "never mind", a sentence meaning it) -> stop;
    #   * "no, the fan" / "I meant the fan" -> a correction: the running
    #     command stops and the fan takes its place, with its action;
    #   * a device named ("and the fan", "the heater too") -> an addition,
    #     run straight after with the running command's action;
    #   * anything else -> nothing, and the command goes on.
    #
    # SECURITY (her rulings). Every one of them is checked against this
    # phone's person's voice first, cancel included -- or a television
    # saying "stop" could keep anybody from doing anything. ONE addition or
    # correction per running command rides free; any more are ordinary
    # commands with the ordinary counter (every fifth one, the full check).
    # A secure device is checked as always.

    def _hold_released(self) -> None:
        if not self._hold.is_set():
            self._hold.set()

    async def _dispatch_hold(self, msg: dict) -> None:
        if not (self._flow_running() and not self._quiet_flow):
            return                        # nothing to hold
        if self._hold.is_set():
            event("CMD", "hold: something is being said over the command "
                         "-- no device is touched until it is read")
        self._hold.clear()
        self._hold_since = time.monotonic()

    async def _dispatch_release(self, msg: dict) -> None:
        if not self._hold.is_set():
            event("CMD", "hold released: what was said was not for the hub")
        self._hold_released()

    async def _wait_hold(self) -> None:
        """The shell's last stop before a device: wait while held, never
        longer than INTERRUPT_HOLD_MAX_S from when the hold began."""
        if self._hold.is_set():
            return
        left = config.INTERRUPT_HOLD_MAX_S - (
            time.monotonic() - getattr(self, "_hold_since", time.monotonic()))
        _t = time.perf_counter()
        try:
            await asyncio.wait_for(self._hold.wait(), max(0.0, left))
        except asyncio.TimeoutError:
            event("CMD", "hold ran out -- going on with the command")
            self._hold.set()
        event("CMD", f"held {time.perf_counter() - _t:.1f}s for what was "
                     f"said over the command")

    def _aside_kind(self, text: str) -> Tuple[str, str]:
        """("cancel" | "correct" | "add" | "", the words to run). Rules
        first: a device is found only by its name or a word that surely
        sounds like it (resolver.names_a_device), never by meaning. Run in
        a thread: the cancel meaning check is a model call."""
        from nlu.resolver import names_a_device
        from nlu.spoken_cancel import fixed_cancel, means_cancel, normalise
        names = [str(d.get("name") or did)
                 for did, d in self.shell.dm.devices.items()]
        norm = normalise(text)
        if not norm:
            return "", ""
        words = norm.split()
        # Where the first device word is: what comes before it decides
        # between "no, the fan" and "and the fan".
        first = next((i for i in range(len(words))
                      if names_a_device(words[i], names)), None)
        if first is not None:
            lead = " ".join(w for w in words[:first]
                            if w not in ("the", "a", "my", "and", "also"))
            if lead and (lead in CORRECTION_LEADS or fixed_cancel(lead)):
                return "correct", " ".join(words[first:])
            return "add", norm
        clf = getattr(self.shell, "intent_classifier", None)

        def classify(t: str) -> Dict[str, Any]:
            return clf.classify(t, threshold=config.INTENT_MIN_SCORE,
                                margin_floor=config.INTENT_MIN_MARGIN)
        if means_cancel(norm, classify if clf else None):
            return "cancel", ""
        return "", ""

    async def _dispatch_aside(self, msg: dict) -> None:
        text = msg.get("text") if isinstance(msg.get("text"), str) else ""
        running = self._flow_running() and not self._quiet_flow
        if not running:
            # The command it was said over is already over: it is an
            # ordinary command now, counted like one.
            self._hold_released()
            if text.strip():
                # As OVERHEARD: acted on only if plainly meant for the house
                # and in this phone's person's voice (the dock's rule).
                await self._dispatch_command_frame(
                    {**msg, "type": "command", "ambient": True})
            return
        try:
            kind, words = await asyncio.to_thread(self._aside_kind, text)
        except Exception as exc:                        # noqa: BLE001
            event("CMD", f"aside not read ({exc})")
            kind, words = "", ""
        if not kind:
            event("CMD", f"said over the command: {text!r} -- not for the "
                         f"hub, the command goes on")
            self._hold_released()
            await self._send_json({"type": "aside_ack", "kind": "ignored"})
            return
        # WHOSE VOICE. This phone's person, from this very clip.
        speaker = PHONES.user_for(self._phone_id) or ""
        audio = None
        b64 = msg.get("audio")
        if isinstance(b64, str) and b64 and len(b64) <= self.MAX_AUDIO_B64_LEN:
            try:
                audio = await asyncio.to_thread(
                    decode_audio_bytes, base64.b64decode(b64, validate=True),
                    msg.get("audio_format") or "wav")
            except Exception:                           # noqa: BLE001
                audio = None
        mine = False
        if audio is not None and speaker:
            try:
                mine = await self.shell._passive_voice_check(speaker, audio)
            except Exception as exc:                    # noqa: BLE001
                event("VOICE", f"aside: voice check failed -- {exc}")
        if not mine:
            event("CMD", f"said over the command: {text!r} ({kind}) -- not "
                         f"{speaker or 'this phone'}'s voice, ignored")
            self._hold_released()
            await self._send_json({"type": "aside_ack", "kind": "ignored",
                                   "why": "voice"})
            return
        if kind == "cancel":
            event("CMD", f"said over the command: {text!r} -> cancel")
            self._hold_released()
            await self._dispatch_cancel({"type": "cancel"})
            return
        free, self._aside_free = self._aside_free, False
        self._asides.append({"text": words, "audio": audio, "free": free,
                             "replace": kind == "correct"})
        event("CMD", f"said over the command: {text!r} -> {kind} "
                     f"{words!r} ({'rides free' if free else 'counted'})")
        if kind == "correct":
            # Stop what is running at its next checkpoint -- the hold has
            # kept it from touching anything -- and run the correction in
            # its place (_handle_command).
            self._cancel_requested = True
            q = self._active_command_queue
            if q is not None:
                try:
                    q.put_nowait({"type": "cancel"})
                except asyncio.QueueFull:
                    pass
        self._hold_released()
        await self._send_json({"type": "aside_ack", "kind": kind})

    async def _known_tailnet(self) -> Tuple[str, str]:
        """The Tailscale name and address the hub last had -- only while it
        is still signed in (paused, off or run out still are)."""
        state = (await self._remote_access()).get("state", "")
        if state in ("", "not_set_up", "absent"):
            return "", ""
        return _remembered_tailnet()

    async def _remote_access(self) -> dict:
        """tailnet.access_state(), kept a few seconds (auth_ok asks twice)."""
        import time as _t
        cached = getattr(self, "_ra_cache", None)
        if cached and _t.monotonic() - cached[0] < 5:
            return cached[1]
        try:
            from core import tailnet as _tn
            access = await asyncio.to_thread(_tn.access_state)
        except Exception:                              # noqa: BLE001
            access = {}
        self._ra_cache = (_t.monotonic(), access)
        return access
        try:
            from core import tailnet as _tn
            return await asyncio.to_thread(
                lambda: _tn.account_of(_tn.status(_tn.find_tailscale() or
                                                  "tailscale")))
        except Exception:                              # noqa: BLE001
            return ""

    async def _dispatch_tailscale_cancel(self, msg: dict) -> None:
        """Cancel on the phone while it signs in: the sign-in is dropped and
        the hub stays as it was."""
        proc = (getattr(self, "_ts_signin", None)
                or _SIGNINS.get(self._phone_id or ""))
        if proc is None:
            return
        from core import tailnet as _tn
        event("TAILNET", "sign-in cancelled on the phone")
        await asyncio.to_thread(_tn.cancel_interactive, proc)

    async def _dispatch_tailscale_logout(self, msg: dict) -> None:
        """Sign the hub out of Tailscale, behind the PIN, like signing in."""
        if self._auth_mode != "phone":
            await self._fail("not_allowed", "only a fully paired phone may "
                             "change remote access", "tailscale_failed")
            return
        if self._busy():
            await self._fail("busy", "something is already running on this "
                             "connection -- try again when it finishes",
                             "tailscale_failed")
            return
        enrolled = await asyncio.to_thread(
            self.shell.biometric_store.list_users)
        if not enrolled:
            await self._fail("not_enrolled", "somebody has to be enrolled "
                             "on this hub first.", "tailscale_failed")
            return
        self._spawn_flow(self._handle_tailscale_logout())

    async def _handle_tailscale_logout(self) -> None:
        if not await self._take_lock("tailscale_failed"):
            return
        try:
            self._claim_shell()
            try:
                person = PHONES.user_for(self._phone_id)
                if not person:
                    await self._fail("not_bound", "this phone is not bound "
                                     "to anyone on the hub yet, so it cannot "
                                     "change remote access.",
                                     "tailscale_failed")
                    return
                self.shell.current_speaker_id = person
                self.shell._ws_last_speaker = person
                proven, why = await self.shell.confirm_with_pin(
                    person, "sign the hub out of Tailscale")
                if not proven:
                    await self._fail("pin_refused", f"{why} The hub is still "
                                     f"signed in.", "tailscale_failed")
                    return
                from core import tailnet as _tn
                result = await asyncio.to_thread(_tn.logout)
                if not result.get("ok"):
                    await self._fail(result.get("reason", "failed"),
                                     result.get("text", ""),
                                     "tailscale_failed")
                    return
                event("TAILNET", f"signed out by {person!r} "
                                 f"({result.get('account') or '?'})")
                _drop_tailnet_binding()
                await self._send_json({
                    "type": "tailscale_left",
                    "previous_name": result.get("previous_name", ""),
                    "previous_ip": result.get("previous_ip", ""),
                    "text": result.get("text", "")})
            except CancelledByUser:
                await self._fail("cancelled", "cancelled -- the hub is still "
                                 "signed in.", "tailscale_failed")
            except websockets.exceptions.ConnectionClosed:
                raise
            except Exception:
                traceback.print_exc()
                await self._fail("internal_error", "internal server error -- "
                                 "see server logs", "tailscale_failed")
            finally:
                self._end_flow()
        finally:
            self.lock.release()

    async def _dispatch_tailscale_join(self, msg: dict) -> None:
        """Attach this hub to a tailnet, with a key a paired phone carried.

        ONLY ON A FULL SESSION. This frame reaches the reader loop, which an
        enrollment session never does -- but the mode is still checked here
        explicitly, because "unreachable today" is one refactor away from
        reachable, and a phone that has proven nothing but the shared API
        key must not be able to attach the hub to a network.

        SPAWNED, NOT AWAITED, and that is a correctness fix rather than
        tidying. This used to run inline in the reader loop, which was only
        safe while it never asked the client anything: the reader is the
        SOLE drainer of the socket, so a question awaited from inside it
        waits for an answer that can never be read. The PIN gate below is
        exactly such a question. So the join becomes a task holding the
        shell lock and owning the reply queue, like every menu action.
        """
        if self._auth_mode != "phone":
            await self._fail("not_allowed", "only a fully paired phone may join "
                             "this hub to a tailnet", "tailscale_failed")
            return

        if self._bootstrapping:
            await self._fail("setup_in_progress", "finish enrolling a voice first",
                             "tailscale_failed")
            return

        if self._busy():
            await self._fail("busy", "something is already running on this "
                             "connection -- try again when it finishes",
                             "tailscale_failed")
            return

        # NOBODY ENROLLED MEANS NO PIN TO ASK FOR, and this action is
        # PIN-gated by design -- so it is refused rather than quietly run
        # ungated, which is the shape of every "temporary" security hole.
        enrolled = await asyncio.to_thread(
            self.shell.biometric_store.list_users)
        if not enrolled:
            await self._fail("not_enrolled", "somebody has to be enrolled on this hub "
                             "first.",
                             "tailscale_failed")
            return

        self._spawn_flow(self._handle_tailscale_join(msg))

    async def _handle_tailscale_join(self, msg: dict) -> None:
        """The join itself, under the shell lock, behind the PIN.

        WHY A PIN AT ALL (her ruling, 2026-08-27: "guard that with pin also
        in the app"). Everything else this frame could do was additive --
        a hub with no tailnet gains one -- and it was the only settings
        action with no authentication whatever. It is not additive any
        more: the same frame can now MOVE the hub to another tailnet,
        dropping the name every phone has saved and the certificate it
        serves with. That is a change to how the house is reached, and it
        should cost what unlocking a door costs.

        The PIN is asked of the PERSON THIS HANDSET BELONGS TO, not of
        whoever the shell would otherwise resolve to: the pairing record
        already says whose hand this phone is in, and _resolve_current_speaker
        auto-picks the only enrolled user, which is a convenience for
        commands and would be a hole here.

        The key is never stored, logged, or echoed -- the event lines record
        THAT a join was attempted and by which phone, never what the key was.
        """
        if not await self._take_lock("tailscale_failed"):
            return
        try:
            self._claim_shell()
            try:
                await self._tailscale_join_gated(msg)
            except CancelledByUser:
                await self._fail("cancelled", "cancelled -- nothing was changed.",
                                 "tailscale_failed")
            except websockets.exceptions.ConnectionClosed:
                raise
            except Exception:
                traceback.print_exc()
                await self._fail("internal_error", "internal server error -- see "
                                 "server logs", "tailscale_failed")
            finally:
                self._end_flow()
        finally:
            self.lock.release()

    async def _tailscale_join_gated(self, msg: dict) -> None:
        person = PHONES.user_for(self._phone_id)
        if not person:
            # An unbound phone has proven a keypair, never a person. It may
            # talk to the hub; it may not move the hub's network.
            await self._fail("not_bound", "this phone is not bound to anyone on "
                             "the hub yet, so it cannot change remote access. "
                             "Enroll or log in on it first.", "tailscale_failed")
            return
        self.shell.current_speaker_id = person
        self.shell._ws_last_speaker = person

        key = msg.get("auth_key")
        hostname = msg.get("hostname")
        hostname = hostname if isinstance(hostname, str) else ""
        has_key = isinstance(key, str) and bool(key.strip())
        event("TAILNET", f"{self._peer()}: join requested by phone "
                         f"{self._phone_id[:8]} for {person!r}"
                         + ("" if has_key else " (interactive sign-in)"))

        # THE CHECKS THAT NEED NO PIN, FIRST (2026-11-05): Tailscale missing,
        # not running, no internet or a sign-in already running -- said
        # before anyone types a PIN for something that could never work.
        from core import tailnet as _tn
        refused = await asyncio.to_thread(_tn.ready)
        if refused:
            await self._fail(refused.get("reason", "failed"),
                             refused.get("text", ""), "tailscale_failed")
            return

        proven, why = await self.shell.confirm_with_pin(
            person, "change remote access")
        if not proven:
            event("TAILNET", "join refused: PIN not proven")
            await self._fail("pin_refused", f"{why} Remote access was not changed.",
                             "tailscale_failed")
            return

        from core import tailnet
        if has_key:
            await self.shell._io.send_data("busy", {
                "text": "Joining Tailscale",
                "detail": "The hub is signing in and getting its address.",
                "seconds": 90})
            try:
                result = await asyncio.to_thread(tailnet.join, key, hostname)
            finally:
                await self.shell._io.send_data("busy", {"done": True})
            # ALREADY ON A TAILNET: the key cannot be read, so the hub asks
            # instead of guessing. Answering yes spends the key and lands
            # wherever it belongs -- same tailnet means nothing moves.
            if result.get("reason") == "confirm_switch":
                if await self._confirm_tailnet_switch(result):
                    event("TAILNET", "switch approved -- spending the key")
                    result = await asyncio.to_thread(
                        tailnet.join, key, hostname, True)
                else:
                    event("TAILNET", "switch declined -- key not spent")
                    await self._fail(
                        "unchanged", f"nothing was changed -- the hub is still "
                                     f"{result.get('tailnet_name', 'where it was')}"
                                     f" and the key was NOT spent, so it can still "
                                     f"be used where you meant it to go.",
                        "tailscale_failed")
                    return
        else:
            # NO KEY MEANS SIGN IN ON THE PHONE. `tailscale up` prints a
            # login URL and blocks until someone authenticates at it; the
            # URL goes to the phone through the SAME web screen Home
            # Assistant's integration sign-ins already use, and the join
            # completes the moment the person finishes there. Nothing is
            # typed and no key ever exists.
            await self.shell._io.send_data("busy", {
                "text": "Opening Tailscale's sign-in", "seconds": 30})
            try:
                begun = await asyncio.to_thread(
                    tailnet.begin_interactive_join, hostname)
            finally:
                await self.shell._io.send_data("busy", {"done": True})
            if not begun.get("ok"):
                await self._fail(begun.get("reason", "failed"), begun.get("text", ""),
                                 "tailscale_failed")
                return
            if "proc" not in begun:
                result = begun          # nothing to sign; report as joined
            else:
                # THE WAIT LEAVES THE LOCK. Signing in on a web page can
                # take minutes, and holding the shell lock for it made
                # every other phone "server_busy" the whole time (audit
                # F53). Nothing after this point touches the shell -- it
                # only waits on a process and sends frames -- so it runs
                # as its own task and this handler returns, releasing the
                # lock in its finally. run() cancels the task on
                # disconnect; the process has its own timeout and is
                # reaped by tailnet either way.
                #
                # Started BEFORE the `open` frame is sent: if that send
                # fails (the phone left), `tailscale up` must still have a
                # waiter that times it out and reaps it, not run for ever.
                # Remembered for Cancel on the phone.
                self._ts_signin = begun["proc"]
                # NOT TIED TO THIS CONNECTION (2026-11-21). To sign in, the
                # phone usually has to leave the hub's network for one with
                # internet, so this connection drops halfway by design. The
                # sign-in carries on (Cancel, or its own timeout, ends it);
                # the answer goes to the phone wherever it is connected
                # then, or the moment it comes back.
                _SIGNINS[self._phone_id or ""] = begun["proc"]
                task = asyncio.ensure_future(
                    self._finish_join_unlocked(begun["proc"], self._phone_id or ""))
                _SIGNIN_TASKS.add(task)
                task.add_done_callback(_SIGNIN_TASKS.discard)
                # In the log too: it can be opened from any device with
                # internet, if the phone's page will not load.
                event("TAILNET", f"sign-in link: {begun['url']}")
                await self._send_json({
                    "type": "open", "screen": "web",
                    "url": begun["url"],
                    # The phone's browser, not a page in the app (Google and
                    # others refuse to sign in inside an app). The app waits
                    # for the hub's answer with Cancel beside it.
                    "external": True,
                    "title": "Sign in to Tailscale",
                    # WHOSE INTERNET THIS NEEDS. The hub reached the
                    # control plane over its own uplink -- that is how it
                    # got this URL -- but the page has to open on the
                    # PHONE, and a phone sitting on the hub's access point
                    # has no route out: the AP is deliberately built with
                    # no NAT and no forwarding (pi-network-setup.sh), so a
                    # compromised board cannot phone home. Nothing here is
                    # broken; the sign-in simply has to happen somewhere
                    # with internet. Said HERE because the alternative is
                    # a page that fails to load with a generic network
                    # error and no hint that the isolation is the reason.
                    "note": "Sign in with the SAME account you use in the "
                            "Tailscale app on this phone (no account yet? "
                            "you can make one here, free). If this page "
                            "will not load, tap Copy link and open it on "
                            "any phone or computer with internet -- the "
                            "hub joins as soon as you finish."})
                return
        await self._report_join_result(result)

    async def _finish_join_unlocked(self, proc: Any, phone_id: str = "") -> None:
        """The tail of a sign-in, outside the shell lock and outside this
        connection's life. The phone usually leaves the hub's network to
        sign in, so it is often not here when Tailscale finishes: the hub
        takes the new address either way, and the answer goes to the
        phone's live connection, or is kept and told when it reconnects
        (_SIGNIN_RESULTS). Cancel, or the sign-in's own timeout, ends it."""
        from core import tailnet
        try:
            try:
                result = await asyncio.to_thread(
                    tailnet.finish_interactive_join, proc)
            except asyncio.CancelledError:
                # The hub is shutting down: drop the sign-in; the waiting
                # thread puts the old account back.
                tailnet.cancel_interactive(proc)
                raise
            except Exception:                          # noqa: BLE001
                traceback.print_exc()
                # Told, not left waiting: a failed attempt is a failure.
                result = {"ok": False, "reason": "internal_error",
                          "text": "The sign-in did not finish, so remote "
                                  "access was not changed. Try again."}
        finally:
            if getattr(self, "_ts_signin", None) is proc:
                self._ts_signin = None
            if _SIGNINS.get(phone_id) is proc:
                _SIGNINS.pop(phone_id, None)
        _apply_join_result(result)
        # This connection if it is still open, else the phone's new one.
        target = self if self in _LIVE_PHONES else next(
            (c for c in list(_LIVE_PHONES)
             if phone_id and c._phone_id == phone_id), None)
        if target is not None:
            try:
                await target._report_join_result(result, applied=True)
                return
            except Exception:                          # noqa: BLE001
                pass                       # it went too: keep the answer
        if phone_id:
            _SIGNIN_RESULTS[phone_id] = result
            event("TAILNET", "the sign-in ended while the phone was away -- "
                             "it is told when it reconnects")

    async def _report_join_result(self, result: dict,
                                  applied: bool = False) -> None:
        """Tell this phone how a join ended. `applied`: the hub already
        took the result (_apply_join_result) and this only tells."""
        if not applied:
            _apply_join_result(result)
        if result.get("ok"):
            await self._send_json({
                "type": "tailscale_joined",
                "tailnet_name": result.get("tailnet_name", ""),
                # The address beside the name, so Remote access finishing
                # IS the list being updated -- nothing left to type. From
                # tailscale's own status first: the listener for a name
                # adopted a moment ago binds on the next twenty-second
                # check, so the live bindings may not carry it yet.
                "tailnet_ip": (result.get("tailnet_ip") or ""
                               or bound_tailnet_ip()),
                # After a MOVE the old address is as dead as the old name.
                "previous_ip": result.get("previous_ip", ""),
                "cert_ok": bool(result.get("cert_ok")),
                # WHICH KIND of success: "joined" (was on nothing),
                # "same_tailnet" (the key belonged here, nothing moved),
                # "switched" (a different tailnet, the hub moved). Additive
                # -- an older app ignores it -- and the app uses it to
                # decide whether the OLD saved address is now dead.
                "reason": result.get("reason", "joined"),
                "previous_name": result.get("previous_name", ""),
                # Which account it is signed in with now.
                "account": result.get("account", ""),
                "text": result.get("text", ""),
            })
        else:
            await self._fail(result.get("reason", "failed"), result.get("text", ""),
                             "tailscale_failed")

    async def _confirm_tailnet_switch(self, asked: dict) -> bool:
        """"This may move the hub. Go ahead?" -- asked ONCE, after the PIN.

        Two gates rather than one, and they answer different questions. The
        PIN proves a PERSON is here. This proves they understood WHAT: a key
        that turns out to belong to another tailnet takes the hub's name,
        its certificate and every phone's saved address with it, and none of
        that is visible in the act of pasting a key.

        A choice, not a typed word: the app already draws these as two
        buttons, and the destructive one must not be the one your thumb
        lands on by habit -- so "stay" is first.
        """
        was = asked.get("tailnet_name", "this tailnet")
        picked = await self.shell._io.choose(
            f"This hub is on {was}. Spend the key and see where it lands?",
            [{"value": "no", "label": "Leave it as it is",
              "sub": "the key is not spent -- use it elsewhere"},
             {"value": "yes", "label": "Try the key",
              "sub": f"same tailnet: nothing changes. Another one: the hub "
                     f"moves there and {was} stops working"}])
        return bool(picked) and picked[0] == "yes"

    async def _dispatch_command_frame(self, msg: dict) -> None:
        """Decide what a 'command' frame means right now, and act on it.

        THE ORDER OF THESE CHECKS IS THE FIX. _bootstrapping used to sit in
        an elif AFTER the empty-store branch -- and it is only ever True
        while the store IS empty, so it was unreachable in the exact
        situation it exists for. A command frame arriving mid-enrollment
        therefore fell into the empty-store branch, found _command_task None
        (enrollment was awaited inline from run(), never assigned to it),
        concluded nothing was running, and spawned a SECOND enrollment that
        clobbered the first one's reply queue. The client's own
        "Start enrollment" button sends exactly such a frame.

        Busy-ness is now asked first and answered from one place.
        """
        if await self._may_start_flow(
                "CMD", "a command is already running on this connection",
                "sending commands", "sending commands"):
            # Set HERE, not only in the handler's first line: a cancel
            # that arrives in the same breath as the command is read before
            # the task has run at all, found this False from the command
            # before, and the command went on regardless (2026-10-07).
            self._pre_lock = True
            self._spawn_flow(self._handle_command(msg))

    async def _may_start_flow(self, tag: str, busy_text: str,
                              enroll_purpose: str, bound_purpose: str,
                              refused: str = "command") -> bool:
        """The gate a command frame and a menu frame both pass, in the order
        _dispatch_command_frame explains. False means refused, and already
        answered."""
        if self._bootstrapping:
            # First-run enrollment owns the shell AND the reply queue.
            event(tag, "refused: enrollment in progress")
            await self._fail("setup_in_progress", "finish enrolling a voice first",
                             refused=refused)
            return False

        if self._busy():
            event(tag, "refused: something is already running")
            await self._fail("busy", busy_text, refused=refused)
            return False

        # Off the event loop: this may touch disk, and the reader is the
        # ONLY task draining the socket -- stalling it stalls every reply.
        enrolled = await asyncio.to_thread(self.shell.biometric_store.list_users)
        if not enrolled:
            # HARD GATE, and the retry path in one. With no voiceprint on
            # file there is nobody to verify against, so a command cannot
            # run -- but simply refusing it would strand the user on a
            # screen with no way back. Trying to use an unenrolled system
            # IS the request to enroll, so the attempt starts enrollment.
            event(tag, "refused: nobody enrolled -- starting enrollment instead")
            await self._fail("not_enrolled", f"enroll a voice before {enroll_purpose}",
                             refused=refused)
            self._spawn_bootstrap()
            return False

        # A PAIRED PHONE WITH NOBODY BEHIND IT may not drive the house. The
        # keypair proved which handset this is, not whose hand it is in; the
        # only thing it may do is finish logging in or signing up, so the
        # attempt re-opens that ceremony instead of running the command.
        if self._is_unbound_phone():
            event(tag, "refused: phone not bound to a person -- "
                       "re-opening login/sign-up")
            await self._fail("not_bound",
                             f"log in or sign up on this phone before {bound_purpose}",
                             refused=refused)
            self._spawn_bootstrap()
            return False
        return True

    async def _dispatch_menu_frame(self, msg: dict) -> None:
        """A named management action from the sidebar.

        Held to EXACTLY the same discipline as a command frame -- the same
        busy checks, the same enrollment gate, the same single reply queue,
        the same shell lock. A management action mutates the device registry;
        letting it run beside a command, or before anyone is enrolled, would
        be a wider door than the one commands come through, not a narrower
        one.
        """
        action = msg.get("action")
        if not isinstance(action, str):
            await self._fail("bad_menu_frame", "menu frame needs an 'action' string", "error")
            return
        if await self._may_start_flow(
                f"MENU {action!r}", "something is already running on this connection",
                "using device management", "using settings",
                refused=f"menu:{action}"):
            self._spawn_flow(self._handle_menu(action),
                             quiet=action in self.QUIET_ACTIONS)

    async def _handle_menu(self, action: str) -> None:
        """Run one management action -- under the shell lock unless it only
        reads (LOCK_FREE_ACTIONS)."""
        locked = action.split(":", 1)[0] not in LOCK_FREE_ACTIONS
        if locked and not await self._take_lock(refused=f"menu:{action}"):
            return

        speaker_id: Optional[str] = None
        try:
            self._claim_shell()
            try:
                speaker_id = await self._seat_speaker()
                ok = await self.shell.run_menu_action(action, speaker_id)
                # FIX: the module contract says every FAILED result carries
                # both reason and text; a bare reason "menu:list_devices"
                # was being shown to browser users as the human message.
                frame: Dict[str, Any] = {"type": "result", "ok": bool(ok),
                                         "reason": f"menu:{action}"}
                # A CHECKPOINT is neither success nor failure (the ribbon
                # law, 2026-08-25): the flow parked its work and a notice
                # owns the way forward. The app shows a quiet banner for
                # it -- never the Done window, never the amber alarm.
                # (Both notes are cleared by _end_flow's reset below.)
                note = self.shell.checkpoint_note
                if note:
                    frame["ok"] = False
                    frame["checkpointed"] = True
                    frame["text"] = note
                elif not ok:
                    # A refusal that knows its reason SAYS the reason --
                    # failure_note is the first-class channel for it. The
                    # generic filler survives only for flows that returned
                    # False without explaining themselves.
                    frame["text"] = (self.shell.failure_note
                                     or f"{action.replace('_', ' ')} did not complete")
                await self._send_json(frame)
            except CancelledByUser:
                # `action` names WHICH flow was cancelled -- additive, so
                # an older app simply ignores it. The new app uses it to
                # acknowledge a cancelled device flow with a neutral toast
                # instead of silence (field test 2026-08-25).
                await self._fail("cancelled", "cancelled",
                                 refused=f"menu:{action}")
            except Exception:
                # FIX: exception details go to the SERVER log only, exactly
                # like _handle_command. f"{type(e).__name__}: {e}" was
                # shipping internal error text to every connected client.
                traceback.print_exc()
                # Tagged with the action like every other menu result, so
                # the app shows its failure window, not a voice-command one.
                await self._fail(f"menu:{action}",
                                 "internal server error -- see server logs")
            finally:
                # A door that only reads leaves "it" alone: it may be
                # running beside this phone's own command now.
                self._end_flow(speaker_id, keep_context=not locked
                               or action in self.QUIET_ACTIONS)
        finally:
            if locked:
                self.lock.release()

    def _spawn_bootstrap(self) -> asyncio.Task:
        """Start enrollment AS A TASK, recorded in _command_task.

        It used to be awaited inline from run(), which left _command_task
        None for the whole of first-run setup -- so every busy check that
        consults it reported 'nothing running' while enrollment was very
        much running. It installs its own reply queue, under the lock."""
        return self._spawn_flow(self._bootstrap_enrollment(), own_queue=False)

    async def _login_or_signup(self, users: list) -> None:
        """A new handset in a house that already has people: whose is it?

        The PIN is the right proof here and the voice check is not, for a
        reason worth writing down: voice verification needs a stored
        template to compare against, which exists -- but a voice check
        over a brand-new, unbound phone would make the microphone the root
        of trust at the exact moment the device is least trusted. The PIN
        is knowledge, rate-limited and lockable, and pin_auth already
        enforces both. Voice keeps guarding COMMANDS; the PIN guards
        BELONGING.
        """
        picked = await self.shell._io.choose(
            "Who is holding this phone?",
            [{"value": "login", "label": "Log in",
              "sub": "I already have a voice and PIN on this hub"},
             {"value": "signup", "label": "I'm new",
              "sub": "record my voice and set a PIN"}])
        if not picked:
            return

        if picked[0] == "signup":
            # ONE TRANSACTION (2026-08-27): voice + PIN + acknowledged
            # recovery words, or nothing persists at all. The old shape
            # here could leave a voice with no PIN; register_person rolls
            # an unfinished ceremony back completely, and clears
            # current_speaker_id so the bind fallback in the caller can
            # never attach this phone to an erased person.
            #
            # *_persistently: the owner's cancel law -- a mid-ceremony
            # cancel rolls back and re-asks the name question in the same
            # room. Only a cancel at the untouched name question reaches
            # the except, and for a SIGNUP (people already exist, this is
            # a choice, not a necessity) it means "let me out": return
            # quietly, and the caller's tail sends view:command -> home.
            try:
                await self.shell.register_person_persistently()
            except CancelledByUser:
                pass
            return

        # ── Log in ────────────────────────────────────────────────────
        if len(users) == 1:
            user = users[0]
        else:
            chosen = await self.shell._io.choose(
                "Who are you?",
                [{"value": u, "label": u, "sub": ""} for u in users])
            if not chosen:
                return
            user = chosen[0]

        # THE SAME CALLS the voice-fallback path makes -- is_enrolled and
        # authenticate -- not a parallel API. The first version of this
        # invented has_pin()/verify() on a class that has neither, which a
        # real phone found in one connection; there is no substitute for
        # using the methods the rest of the codebase already exercises.
        enrolled = await asyncio.to_thread(self.shell.fallback.is_enrolled,
                                           user)
        if not (enrolled or {}).get("pin"):
            # THE ADVICE USED TO BE IMPOSSIBLE (corrected 2026-09-12). It
            # said "set a PIN from an already-trusted phone", and no phone
            # can do that: menu_setup_pin forces user_id to the caller over
            # a socket and refuses to name anybody else. Someone in this
            # state was stuck until the hub's own keyboard was found.
            #
            # The state is unconstructible now -- "Add another person" runs
            # the whole ceremony, see menu_add_person -- but anyone already
            # in it still needs a way out that exists.
            await self.shell._io.write(
                f"  {user!r} has no PIN on file, so logging in with one is "
                f"impossible -- a voice was recorded without the rest of "
                f"the ceremony. Ask whoever set the house up to add you "
                f"again from Profile > Add another person, which records a "
                f"voice AND sets a PIN. Choosing \"I'm new\" under the same "
                f"name will be refused as taken.", level="warn")
            return

        # THE SHELL'S PIN LOOP, the one every PIN moment uses: a lockout
        # that is already running is announced BEFORE the field appears,
        # cancel is CANCEL and never a guess, every refusal speaks through
        # pin_verdict, and a try that trips the lockout says both facts at
        # once. THREE TRIES, but the REAL limits live in pin_auth: attempts
        # are rate-limited and lockouts persist across connections, so
        # reconnecting does not reset anything.
        ok, _why = await self.shell._pin_tries(user, "  PIN: ")
        if ok:
            # No sentence here: the person is greeted by name by the
            # `welcome` frame the caller sends once the phone is bound.
            self.shell.current_speaker_id = user
        elif ok is False:
            await self.shell._io.write(
                "  This phone stays unbound -- connect again to retry, or "
                "pair it afresh.", level="warn")

    async def _bootstrap_enrollment(self) -> None:
        """Run first-run enrollment immediately after auth, if nobody is
        enrolled.

        Every path keeps the connection up, deliberately: a setup problem
        must never be reported to the user as a mystery disconnect. (An
        earlier revision returned a bool for run() to branch on; no caller
        ever read it, so the return value was dead logic and is gone.)

        _first_run_setup() used to be reachable only from inside
        _handle_command, so on an empty store the phone authenticated,
        landed on the command screen looking perfectly ready, and only
        discovered it had to enroll AFTER sending a command that then
        could not run. Enrollment is a precondition for the session, not
        a step inside one command, so it belongs here.

        This runs with the reader loop ALREADY started -- the prompts it
        issues need replies, and replies only arrive through the reader.
        """
        me = asyncio.current_task()

        # A PAIRED PHONE IS NOT YET A PERSON.
        #
        # The keypair proved which HANDSET is talking. It cannot prove who
        # is holding it, and this system opens locks. So a phone that has
        # nobody behind it is walked through voice enrolment and a PIN
        # before it can drive anything, and the person it produces is
        # written into its record -- see phones.bind_user.
        #
        # The old guard was "does ANYONE exist", which was right when the
        # hub could not tell one phone from another. It is wrong now: a
        # second handset joining a house that already has an enrolled user
        # would have sailed past enrolment and inherited the household's
        # trust without ever proving whose hand it was in.
        #
        # BOUND IS ONLY ENOUGH WHILE SOMEBODY IS ON FILE. With the voice store
        # empty (moved aside as unreadable) a bound phone's every command was
        # refused not_enrolled, and this returned at once -- so the ceremony
        # that refusal promises never came. Nobody on file is first run.
        # (Every session has a phone id: the test page pairs too.)
        users_before = set(
            await asyncio.to_thread(self.shell.biometric_store.list_users))
        if users_before and PHONES.user_for(self._phone_id):
            return

        if not await self._acquire_lock(self.LOCK_ACQUIRE_TIMEOUT):
            # Another connection holds the shell. Say so and stay up --
            # every exit from this method keeps the socket alive, so a
            # setup problem is never reported as a mystery disconnect.
            await self._fail("server_busy",
                             "another connection is setting up -- try again shortly", "error")
            return

        # This phone may have been bound while we waited for the lock.
        if users_before and PHONES.user_for(self._phone_id):
            self.lock.release()
            return

        # Claim the flag and the queue TOGETHER, under the lock, and record
        # this task as the queue's owner so no other task can retire it.
        event("ENROLL", f"{self._peer()}: first-run enrollment starting "
                        f"(shell lock held)")
        self._bootstrapping = True
        self._install_queue(me)
        try:
            self._claim_shell()
            if users_before:
                # Somebody is already enrolled, so this is a NEW HANDSET
                # rather than a new house -- and there are two kinds of
                # person holding it. LOG IN: an enrolled person proving
                # themselves with their PIN, after which this phone is
                # theirs -- their voiceprints already exist and belong to
                # THEM, not to any handset, so nothing is re-recorded.
                # SIGN UP: a new person, who records their voice and sets
                # a PIN like the first user did.
                await self._login_or_signup(sorted(users_before))
            else:
                await self.shell._first_run_setup()

            # BIND, while the lock is still held. Whoever appeared (or, for
            # a re-enrolment, whoever the shell last worked on) becomes this
            # phone's person. Done here rather than in the flow itself so
            # the enrolment code stays ignorant of phones.
            users_after = set(await asyncio.to_thread(
                self.shell.biometric_store.list_users))
            fresh = sorted(users_after - users_before)
            # No new user means an EXISTING name was re-enrolled -- the
            # "my other phone" case. menu_enroll_user records who that
            # was in current_speaker_id on success (and only there; a
            # failed attempt leaves it untouched, so a phone is never
            # bound to a person whose enrolment did not complete).
            owner = fresh[0] if fresh else self.shell.current_speaker_id or ""
            try:
                bound = bool(owner) and await asyncio.to_thread(
                    PHONES.bind_user, self._phone_id, owner)
            except OSError as exc:              # registry unreadable
                event("PAIR", f"{self._peer()}: phone not bound -- {exc}")
                bound = False
            if bound:
                event("PAIR", f"{self._peer()}: phone "
                              f"{self._phone_id[:8]} now belongs to "
                              f"{owner!r}")
                # THE OLD KEYS DIE THE MOMENT THE NEW ONE PROVES ITS
                # PERSON. A reinstall pairs as a fresh keypair, so
                # without this every reinstall left one more dead
                # registration answering to the same hand. Purged only
                # now -- after the PIN (or a completed enrolment)
                # proved WHO -- never at pairing, where knowing the
                # API key must not be enough to evict anything.
                try:
                    stale = await asyncio.to_thread(
                        PHONES.purge_stale_siblings, self._phone_id)
                except OSError as exc:
                    event("PAIR", f"stale registrations kept -- {exc}")
                    stale = []
                if stale:
                    event("PAIR", f"purged {len(stale)} stale "
                                  f"registration(s) of this handset: "
                                  f"{', '.join(p[:8] for p in stale)}")
                # NOT SAID ALOUD any more (it was a sentence about keys the
                # person never needed). They are greeted by name instead; the
                # phone adds the time of day from its own clock.
                await self._send_json({"type": "welcome", "name": owner})
        except CancelledByUser:
            event("ENROLL", f"{self._peer()}: attempt abandoned or timed out")
            await self._fail(
                "enrollment_timeout",
                "that attempt timed out -- tap send to start enrollment again", "error")
        except websockets.exceptions.ConnectionClosed:
            raise
        except Exception:
            traceback.print_exc()
            try:
                await self._fail("internal_error",
                                 "enrollment failed -- see server logs", "error")
            except websockets.exceptions.ConnectionClosed:
                pass
        finally:
            self._bootstrapping = False
            self._release_queue(me)
            self._reset_shell_state()
            self.lock.release()

        # ONE attempt, deliberately. An earlier revision looped here until
        # someone enrolled, which spins forever against a client that stays
        # connected and never answers -- re-prompting into the void. The
        # retry belongs to the reader gate below instead: trying to use an
        # unenrolled system IS the request to enroll, so there is exactly
        # one mechanism enforcing this rather than two.
        #
        # FIX: this tail runs AFTER the lock is released, on a socket the
        # phone may have abandoned mid-enrollment -- ConnectionClosed here
        # is an ordinary ending, not a traceback. And list_users is asked
        # ONCE now; the success log used to call it a second time inside
        # the f-string, a wasted thread hop per enrollment.
        users = await asyncio.to_thread(self.shell.biometric_store.list_users)
        if not users:
            event("ENROLL", f"{self._peer()}: still nobody enrolled -- "
                            f"sending the client home (the reader gate "
                            f"reopens the ceremony on the next attempt)")
        else:
            event("ENROLL", f"{self._peer()}: enrollment finished, "
                            f"users on file: {users}")
        # GO HOME, both ways. Every path above ends in PROSE -- "Welcome
        # back, Prince", "Nothing was saved..." -- and prose holds no
        # instruction. Success used to send view:command while a stopped
        # attempt HELD the client on the enroll view -- which was right
        # when that view had a working dock, and became a trap when it
        # became the ceremony room: a full-screen ritual with nothing
        # pending is a spinner nobody can answer (field test 2026-08-27).
        # Home always works, and with nobody enrolled the very next
        # command or menu tap re-spawns the ceremony -- trying to use an
        # unenrolled system IS the request to enroll.
        try:
            await self._send_json({"type": "view", "name": "command"})
        except websockets.exceptions.ConnectionClosed:
            pass

    async def _run_pairing_exchange(self) -> None:
        """One frame in, one frame out, then the connection ends.

        The phone sends the PUBLIC half of a keypair it generated itself and
        a name to show in the phones list. It gets back the hub's
        certificate, which is what frees the app from having the certificate
        baked into its assets -- the thing this whole design exists for.

        The certificate is not a secret. It is the public half of the hub's
        identity and is already sent in the clear during every TLS
        handshake. What makes handing it over safe is that this exchange
        only happens after the API key has been proven over a channel bound
        to that exact certificate.
        """
        try:
            msg = await asyncio.wait_for(self._recv_json(),
                                         timeout=self.AUTH_TIMEOUT)
        except asyncio.TimeoutError:
            event("PAIR", f"{self._peer()}: no pair frame before the timeout")
            return
        if not msg or msg.get("type") != "pair":
            await self._fail("expected_pair", "this session may only send a 'pair' frame",
                             "pair_failed")
            return

        public_key = msg.get("public_key")
        # Bounded and printable: both strings are stored, broadcast to every
        # phone and SPOKEN ALOUD by each one, and a party that has proven
        # only the API key must not be able to push an arbitrary message
        # (or a kilobyte of it) into the household's ears.
        name = _clean_label(msg.get("name"))
        # What the handset says it IS ("TECNO KJ5"), beside what the person
        # CALLS it. Optional: an older app sends no field and the record
        # simply carries no device string (phones.py's legacy rule).
        device = _clean_label(msg.get("device"))
        if not isinstance(public_key, str) or not public_key:
            await self._fail("missing_public_key",
                             "'public_key' must be a base64 Ed25519 public key", "pair_failed")
            return

        # KNOWING THE API KEY IS NOT ENOUGH -- for a phone the hub has
        # never seen. New keys need the pairing window (or an empty
        # registry, the recovery rule). A key already in the registry is a
        # phone the hub trusts re-fetching its certificate: same id, same
        # owner, nothing new admitted -- no window needed.
        try:
            known = PHONES.get(_phones.phone_id_for(public_key)) is not None
        except Exception:                              # noqa: BLE001
            known = False                              # let add() name the fault
        # NEW PHONES PAIR AT HOME unless the window was opened for one
        # away (Pair a new phone > Away from home, 2026-10-15). A phone
        # already on the list re-fetching its certificate is not a new
        # key, and may.
        away = is_tailnet_ip(self._peer_ip())
        if not known and away and not PHONES.window.allows_away():
            event("PAIR", f"{self._peer()}: refused -- pairing from away "
                          f"is off")
            await self._fail(
                "pairing_away_off",
                "the key was right, but this hub pairs new phones from "
                "away only when asked to: on a phone that is already "
                "paired, Settings > Pair a new phone > Away from home. "
                "Or pair on the house Wi-Fi.",
                "pair_failed")
            return
        if not known and not PHONES.may_enroll():
            event("PAIR", f"{self._peer()}: refused -- pairing closed")
            restart = (f"restart the hub -- it accepts pairing for its "
                       f"first {_phones.window_words()} -- or "
                       if config.PAIR_ON_BOOT else "")
            await self._fail(
                "pairing_closed",
                "the key was right, but this hub is not accepting "
                "NEW phones right now. Ways in: ask an "
                "already-paired phone (Settings > Pair a new "
                f"phone), hold the hub's button, or {restart}run "
                "--pair at its console.",
                "pair_failed")
            return

        try:
            # add() checks the window and closes it under the registry's
            # file lock when the key is NEW -- one phone per window, with
            # no gap between the check above and the write for a second
            # connection to slip through.
            record = await asyncio.to_thread(PHONES.add, name, public_key,
                                             device=device,
                                             via="away" if away else "home")
        except _phones.PairingClosed:
            event("PAIR", f"{self._peer()}: refused -- pairing closed "
                          f"(lost the race for the window)")
            await self._fail(
                "pairing_closed",
                "the key was right, but another phone took this "
                "pairing window first -- open a new one and retry.",
                "pair_failed")
            return
        except ValueError as exc:
            await self._fail("bad_public_key", str(exc), "pair_failed")
            return
        except Exception as exc:                        # noqa: BLE001
            event("PAIR", f"{self._peer()}: could not be saved -- {exc}")
            await self._fail("not_saved", f"the phone could not be recorded: {exc}",
                             "pair_failed")
            return

        # SAY WHO CAME THROUGH THE DOOR, to everyone already inside. Two
        # frames because they are two different acts: pair_window kills the
        # countdown chip on every phone, and the important output is spoken
        # aloud by each phone's own voice -- the household hears a new key
        # being cut, which is the one event in this system that should
        # never happen silently.
        arrived = record.get("name") or "A new phone"
        # The announcement names the hardware too when it is known --
        # "Augusta (TECNO KJ5) has just been paired" tells the household
        # WHICH physical object was given a key, which is the fact a
        # listener would want to verify.
        device_str = record.get("device") or ""
        arrived_full = f"{arrived} ({device_str})" if device_str else arrived
        await _broadcast({"type": "pair_window", "open": False, "seconds": 0,
                          "reason": "paired", "phone": arrived_full})
        # ...and WHICH ROAD it came by: a phone paired from away is the one
        # a household would most want to hear about. Every other phone
        # also gets a notice that waits for it (shell: new_phone), with
        # Remove it -- so a phone that was off right now still learns.
        road = ("over Tailscale, from away" if away
                else "on the home network")
        await _broadcast({"type": "output", "level": "important",
                          "text": f"{arrived_full} has just been paired with "
                                  f"the house, {road}. If that was not "
                                  f"someone in your house, remove it in "
                                  f"Notifications."})

        try:
            with open(config.LOCAL_TLS_CERT, encoding="utf-8") as f:
                certificate = f.read()
        except OSError as exc:
            await self._fail("no_certificate",
                             f"the hub could not read its own certificate: {exc}", "pair_failed")
            return

        event("PAIR", f"{self._peer()}: paired '{record['name']}' "
                      f"({_phones.short_fingerprint(public_key)}); "
                      f"{PHONES.count()} phone(s) now registered")
        # A KEY WAS CUT: three flashes on the header's phone LED, said once.
        # The phone reconnects as a session a second later and the same LED
        # settles solid -- which is the whole story of --pair, in one light.
        if _PANEL is not None:
            try:
                _PANEL.paired()
            except Exception:                          # noqa: BLE001
                pass
        await self._send_json({
            "type": "pair_ok",
            "phone_id": record["id"],
            "fingerprint": _phones.short_fingerprint(public_key),
            "certificate": certificate,
            "spki": own_spki_fingerprint(),
            # A label for the phone's list of hubs (Settings -> Hubs).
            "hub_name": config.hub_display_name(),
        })

    async def run(self) -> None:
        # THIS PHONE'S CONVERSATION, for everything this connection does from
        # here on: every task started below inherits it, and so does every
        # worker thread those start (asyncio.to_thread copies the context).
        CONVERSATION.set(self._conv)
        event("CONN", f"{self._peer()} connected "
                      f"({_active_connections}/{MAX_CONNECTIONS} authenticated, "
                      f"{_pending_auth} pending)")
        # A PEER THAT KEEPS FAILING is not given another challenge. Cheap
        # to hold, and the one fence a shared network (the `lan` binding)
        # adds over an access point nobody else is on. See config.AUTH_FAIL_*.
        #
        # The address is read ONCE, now, while the socket is open: websockets
        # clears remote_address when the connection closes, so a peer that
        # sent a bad proof and hung up at once used to be struck as "?" --
        # which is never counted -- and could guess for ever unthrottled.
        ip = self._peer_ip()
        left = _auth_cooldown_left(ip)
        if left > 0:
            self._log_auth(f"throttled ({left:.0f}s of cooldown left)")
            await self._hang_up("too_many_attempts",
                                f"too many failed attempts from this address -- "
                                f"try again in {left:.0f} s")
            return
        self._auth_nonce = secrets.token_urlsafe(32)  # fresh per connection -> replay protection
        try:
            await self._send_json({"type": "auth_challenge", "nonce": self._auth_nonce})
        except websockets.exceptions.ConnectionClosed:
            return

        # Bounded. A peer that opens a socket and never answers the
        # challenge used to sit here forever, and with ping_timeout=None
        # nothing else reaps it either.
        try:
            ok = await asyncio.wait_for(self._authenticate(), timeout=self.AUTH_TIMEOUT)
        except asyncio.TimeoutError:
            self._log_auth("timeout")
            ok = False
        except websockets.exceptions.ConnectionClosed:
            self._log_auth("closed_during_auth")
            self._no_attempt = True
            ok = False
        except Exception:                              # noqa: BLE001
            # A bug on the auth path must still count as a FAILED attempt
            # (strike, close) rather than escape with the throttle skipped.
            traceback.print_exc()
            self._log_auth("error_during_auth")
            ok = False
        if not ok:
            # A STRIKE IS A WRONG ANSWER, not a hang-up. The app dials every
            # address at once and closes the slower ones right after the
            # challenge; those never sent a signature, so they are not
            # guesses -- striking them locked the phone's second road out.
            if not getattr(self, "_no_attempt", False):
                _auth_strike(ip)
            await self._hang_up()
            return

        # ONE SESSION PER PHONE. ping_timeout is off, so a phone that
        # reconnects after a Wi-Fi blip leaves its old socket half-open for
        # as long as TCP takes to notice (minutes), and those ghosts filled
        # MAX_CONNECTIONS until real phones were refused. The new session
        # has just proved the same key, so it replaces them.
        if self._auth_mode == "phone":
            await _replace_old_sessions(self)

        # Proven. Now it counts against the AUTHENTICATED cap.
        if not _count_authenticated(self):
            self._log_auth("refused: connection limit reached")
            await self._hang_up("too_many_connections",
                                "server is at its connection limit -- try again shortly")
            return

        # AN ENROLLMENT SESSION IS NOT A SESSION. It handles one frame and
        # hangs up. It never reaches the reader loop, so it can never issue
        # a command, unlock anything, or start enrollment -- which is the
        # whole reason the two modes are separate.
        if self._auth_mode == "enroll":
            try:
                await self._run_pairing_exchange()
            except websockets.exceptions.ConnectionClosed:
                pass
            await self._hang_up()
            return

        # A real phone session from here on: it may receive hub-wide state.
        _LIVE_PHONES.add(self)
        # A Tailscale sign-in that ended while this phone was away (it went
        # to a Wi-Fi with internet to sign in): its answer, now.
        waiting = _SIGNIN_RESULTS.pop(self._phone_id or "", None)
        if waiting is not None:
            try:
                await self._report_join_result(waiting, applied=True)
            except websockets.exceptions.ConnectionClosed:
                _SIGNIN_RESULTS[self._phone_id or ""] = waiting
        # THE HEADER'S PHONE LED GOES SOLID HERE. This is the moment the
        # person standing at the hub is waiting for after --pair: not the
        # window opening, but a phone actually through it.
        _panel_phones()
        # THE HOME TILES, STRAIGHT AWAY. _readings_watcher broadcasts them
        # only when they CHANGE, and forgets its last broadcast only when
        # it sees an empty room -- so a phone that connected between two
        # changes (a reinstall, a reconnect inside one 15 s tick while
        # another phone stayed on) had an empty home until some value
        # moved. A steady meter never moved, a bulb somebody flipped did
        # (field report 2026-09-03: "the power readings were showing, now
        # they aren't, but the bulb did show"). Same rows, same cheap
        # dictionary walk, sent once to this phone alone.
        try:
            rows = self.shell.live_reading_rows()
            if rows:
                await self._send_json({"type": "readings_live",
                                       "sensors": rows})
        except Exception as exc:                      # noqa: BLE001
            event("READINGS", f"initial push skipped: {exc}")
        # TELL IT THE DOOR IS OPEN, if it is. This is what makes the boot
        # window (and the window an app reinstall rides in on) visible: the
        # hub was restarted, the window opened itself, and every phone that
        # connects during it gets the countdown -- recomputed here exactly
        # like notices are, never remembered from last time.
        try:
            if PHONES.window.is_open():
                await self._send_json(_pair_window_frame())
        except Exception:                              # noqa: BLE001
            pass                      # the chip is a courtesy, never a gate

        # The reader has to be running BEFORE enrollment starts: every
        # prompt it issues waits on _await_reply, and nothing reaches that
        # queue unless someone is draining the socket. Starting enrollment
        # first would deadlock on its own first question.
        reader = asyncio.create_task(self._reader_loop())
        try:
            # Enrollment runs AS A TASK, concurrently with the reader, and is
            # recorded in _command_task -- the busy checks in
            # _dispatch_command_frame consult it, and awaiting it inline here
            # left it None for the whole of setup. The reader is what keeps
            # the socket drained, so it must not be blocked on this.
            #
            # NOT for a phone that already has its person: the task would
            # only return, and until it had run, the app's own on-connect
            # frames -- a buffered check_notices, a first command -- were
            # refused "busy" by a flow that did nothing.
            if not PHONES.user_for(self._phone_id):
                self._spawn_bootstrap()
            await reader
        except websockets.exceptions.ConnectionClosed:
            pass
        finally:
            # Cancel only if still running -- but await it EITHER WAY.
            # The old shape only awaited inside `if not reader.done()`, so a
            # reader that had already died on its own (a dropped connection,
            # a keepalive timeout) was never awaited and asyncio reported
            # "Task exception was never retrieved" with a traceback that
            # looked like a crash but was really just an unread exception.
            if not reader.done():
                reader.cancel()
            try:
                await reader
            except (asyncio.CancelledError,
                    websockets.exceptions.ConnectionClosed):
                pass   # both are ordinary ways for a connection to end
            except Exception:
                traceback.print_exc()   # never silently eat a real bug
            if self._flow_running():
                self._command_task.cancel()
                try:
                    await self._command_task
                except asyncio.CancelledError:
                    pass
                except Exception:
                    traceback.print_exc()  # never silently eat a real bug
            self._command_task = None
            self._active_command_queue = None
            self._queue_owner = None
            for task in list(self._background):
                task.cancel()
            _LIVE_PHONES.discard(self)
            _panel_phones()               # last one out puts the light off
            event("CONN", f"{self._peer()} disconnected")


#: Nothing capped concurrent connections. Combined with an unbounded auth
#: wait and ping_timeout=None, a peer could open sockets until the process
#: ran out of file descriptors without ever proving it knew the secret.
MAX_CONNECTIONS = config.MAX_CONNECTIONS
_active_connections = 0

#: Every connection currently authenticated AS A PHONE (never enrollment
#: sessions -- those handle one frame and hang up). This exists for exactly
#: one kind of message: hub-wide STATE the person should see wherever they
#: are, like "the pairing window is open". Everything else in this server
#: stays strictly per-connection, and should.
_LIVE_PHONES: set = set()

#: Tailscale sign-ins in progress, by phone id: the `tailscale up` process,
#: so Cancel works from whichever connection that phone has now.
_SIGNINS: Dict[str, Any] = {}
#: Their finishing tasks, held here, not on a connection: a phone leaves the
#: hub's network to sign in, and its connection closing must not end them.
_SIGNIN_TASKS: Set[asyncio.Task] = set()
#: How a sign-in ended, for a phone that was away at that moment: told on
#: its next connection.
_SIGNIN_RESULTS: Dict[str, dict] = {}


def _apply_join_result(result: dict) -> None:
    """What the hub itself does with a join's outcome (once, whether or not
    any phone is there to hear it): log it, and on success serve the new
    tailnet name."""
    if result.get("ok"):
        event("TAILNET", f"{result.get('reason', 'joined')} as "
                         f"{result.get('tailnet_name', '?')} "
                         f"(cert_ok={result.get('cert_ok')})")
        _adopt_tailnet_binding(result.get("tailnet_name", ""))
    else:
        event("TAILNET", f"join refused: {result.get('reason', '?')}")

#: Failed authentications per peer address, as timestamps -- the throttle
#: of config.AUTH_FAIL_*. Bounded: an address's list is trimmed to the
#: window on every touch, and an address with no recent failures is dropped.
_AUTH_STRIKES: Dict[str, List[float]] = {}
_AUTH_COOLDOWN_UNTIL: Dict[str, float] = {}


def _auth_strike(ip: str) -> None:
    limit = int(config.AUTH_FAIL_LIMIT)
    if limit <= 0 or ip == "?":
        return
    # MONOTONIC, like every other security window here: the hub's clock is
    # stepped (a phone sets it after a power cut, NTP corrects it), and a
    # step backwards held an address in its cooldown for as long as the step.
    now = time.monotonic()
    window = float(config.AUTH_FAIL_WINDOW)
    recent = [t for t in _AUTH_STRIKES.get(ip, []) if now - t <= window]
    recent.append(now)
    _AUTH_STRIKES[ip] = recent
    if len(recent) >= limit:
        cooldown = float(config.AUTH_FAIL_COOLDOWN)
        _AUTH_COOLDOWN_UNTIL[ip] = now + cooldown
        _AUTH_STRIKES.pop(ip, None)
        event("AUTH", f"{ip} -> {limit} failures in {window:.0f}s: refused for "
                      f"{cooldown:.0f}s")
    # Keep the book small: forget addresses whose failures have all aged out.
    for other in [k for k, v in _AUTH_STRIKES.items() if not v or now - v[-1] > window]:
        _AUTH_STRIKES.pop(other, None)
    # And cooldowns that have run out: _auth_cooldown_left only forgets an
    # address that comes BACK, so every address that ever tripped the
    # throttle and went away stayed in this dict for the life of the process.
    for other in [k for k, until in _AUTH_COOLDOWN_UNTIL.items() if until <= now]:
        _AUTH_COOLDOWN_UNTIL.pop(other, None)


def _auth_cooldown_left(ip: str) -> float:
    until = _AUTH_COOLDOWN_UNTIL.get(ip, 0.0)
    left = until - time.monotonic()
    if left <= 0:
        _AUTH_COOLDOWN_UNTIL.pop(ip, None)
        return 0.0
    return left

#: The Pi's LEDs + button, when there is one (hub_gpio). None on a PC.
_PANEL = None
#: The clock guard (hub_clock): knows when the clock went backwards after a
#: power cut and lets the first paired phone put it right. None until main().
_CLOCK = None


def _panel_phones() -> None:
    """Tell the header's phone LED how many phones are connected.

    One helper rather than two call sites poking the panel: the COUNT is
    what is sent, so connect and disconnect are the same line, and a panel
    that is absent (every PC) costs one `is None`.
    """
    if _PANEL is not None:
        try:
            _PANEL.phones(len(_LIVE_PHONES))
        except Exception:                              # noqa: BLE001
            pass                     # a light is never worth a dropped call


async def _broadcast(frame: dict) -> None:
    """Send one frame to every live phone; a dead socket loses its own copy
    and nobody else's. Never raises -- announcing state must not be able to
    take down the state."""
    for handler in list(_LIVE_PHONES):
        # A phone revoked while its socket sits SILENT is told nothing more:
        # the per-frame check in _reader_loop only runs when it sends, and
        # these frames carry the house's readings and its PIN questions.
        if PHONES.get(handler._phone_id) is None:
            _LIVE_PHONES.discard(handler)
            continue
        try:
            await handler._send_json(frame)
        except Exception:                              # noqa: BLE001
            pass                    # its reader loop will reap it properly


def _pair_window_frame() -> dict:
    """The window as it stands, as a frame. One producer, so the connect-time
    announcement, the menu action and the watcher cannot drift apart."""
    seconds = PHONES.window.seconds_left()
    # `opened_by` rides along so a phone can tell a door somebody asked for
    # (menu, --pair) from the hub's own boot window, which it should not
    # put a countdown on -- see PairingWindow.OPENED_BY_*.
    #
    # `open` ASKS THE WINDOW; it no longer infers the answer from the
    # countdown (2026-09-14). With config.PAIRING_ALWAYS_OPEN the door is
    # open and `seconds_left()` is 0 -- correctly, because nothing is
    # running out -- so `seconds > 0` would have reported a permanently
    # open hub as SHUT. Two separate questions, asked separately now.
    # `opened_by` comes back OPENED_BY_ALWAYS, which is how a phone knows
    # to say "pairing is on" rather than draw a timer at zero.
    return {"type": "pair_window", "open": PHONES.window.is_open(),
            "seconds": seconds, "opened_by": PHONES.window.opened_by(),
            # Opened for a phone away from home (over Tailscale)?
            "from_away": PHONES.window.allows_away()}


async def _pair_window_watcher() -> None:
    """Announce pairing-window TRANSITIONS to every connected phone.

    The window is a FILE, opened by whoever holds it -- this process (boot,
    a phone's menu action) or a different one entirely (setup_manager
    --pair at the hub's own keyboard). Watching the file is the only vantage
    point that sees every opener, which is the same reasoning that put the
    window on disk in the first place.

    Only TRANSITIONS are announced. Opening (or re-opening with a fresh
    expiry) broadcasts the new deadline; closing EARLY -- one phone joined,
    and the window shuts behind it -- broadcasts the closure. Natural expiry
    broadcasts nothing, because every phone was handed the absolute deadline
    and lets its own countdown die; a frame saying "the thing you watched
    reach zero is now zero" would be noise.
    """
    last_until = 0.0
    while True:
        try:
            # A PAIRING ASKED FOR WITH NO ADDRESS (PairingWindow.ask) opens
            # here, full length, the moment an address appears -- whoever
            # asked: the button, --pair, a phone, the boot window.
            ask = PHONES.window.pending()
            if ask is not None and await asyncio.to_thread(
                    _pair_reachable, ask.get("from_away") is True):
                opened = PHONES.window.open_asked()
                if opened:
                    print(f"[WS] On a network now -- pairing open until "
                          f"{time.strftime('%H:%M:%S', time.localtime(opened))}")
                    threading.Thread(target=_write_pair_codes, args=(opened,),
                                     name="pair-codes", daemon=True).start()
            until = PHONES.window._open_until()
            now = time.time()
            # The header LED blinks while the door is open -- whoever
            # opened it, since the file is the one truth about that.
            if _PANEL is not None:
                _PANEL.pairing(until > now)
            if until > now and until != last_until:
                await _broadcast(_pair_window_frame())
                last_until = until
            elif until <= now and last_until > now:
                # Closed before its own deadline -- someone paired, or an
                # operator shut it. The "who" travels separately (the pair
                # path broadcasts the name); this frame kills the countdown.
                await _broadcast({"type": "pair_window", "open": False,
                                  "seconds": 0})
                last_until = 0.0
            elif until <= now:
                last_until = 0.0
        except Exception:                              # noqa: BLE001
            pass                        # a bad poll skips a beat, not a leg
        await asyncio.sleep(2)


#: How often the home tiles are refreshed. Slower than the engine's own
#: five-second tick on purpose: a temperature that moved a tenth of a
#: degree is not news, and every push wakes a phone's radio.
READINGS_PUSH_SECONDS = 15.0

#: How often the watcher WAKES. It does almost nothing on a tick where the
#: engine's counter has not moved -- one attribute read -- so this is the
#: reaction time to a relay or a reading, not the cost of standing still.
#: READINGS_PUSH_SECONDS above is now the FLOOR: the longest a change that
#: the engine does not count (a tile hidden, a device renamed) can go
#: unnoticed.
READINGS_TICK_SECONDS = 1.0


async def _readings_watcher(shell) -> None:
    """Keep the home screen's tiles current, for free.

    The numbers are already arriving: the automation engine holds stat/#
    and tele/+/SENSOR open and caches every value. Nothing was sending
    them to the phone between visits to Statistics, so a hub full of
    boards reporting every ten seconds showed a home screen that knew
    nothing.

    ITS OWN FRAME, not `readings`. That one carries the 24-hour trio the
    Statistics screen draws, and each of those is a REST call to Home
    Assistant; broadcasting the light rows under the same name would let
    a timer quietly overwrite the expensive screen with a cheaper answer.
    Two frames, two lists, neither able to damage the other.

    ONLY WHEN IT CHANGED, and only while somebody is connected: a house
    at rest is silent, and an empty room costs one dictionary walk.

    IT REACTS NOW, RATHER THAN POLLING (2026-09-12: "let the watcher be
    every second instead why 15s").

    Every second would have been the wrong answer to a fair complaint. The
    tiles were rebuilt on a TIMER -- a full walk of every device plus a
    JSON fingerprint of the result, whether or not anything had happened --
    so a one-second interval would have meant fifteen times that work at
    rest and bought NOTHING for sensors: a board publishes
    tele/<topic>/SENSOR every TelePeriod, which is 300 seconds by default,
    and polling three hundred times faster than the data arrives cannot
    make it fresher.

    What the fifteen seconds really cost was STATE. A relay flipped by a
    rule, by a physical switch, or by another phone arrives on
    stat/<topic>/POWERn in milliseconds -- and then sat unreported for up
    to fifteen seconds.

    So the engine COUNTS what it takes in (`reading_seq`), and this wakes
    every second only to read that number. Unchanged, it goes back to sleep
    having done nothing at all. Changed, it walks and pushes at once. That
    is quicker than a one-second poll AND cheaper than the fifteen-second
    one: a house at rest is now genuinely idle rather than merely quiet.

    The old interval survives as a FLOOR, not a heartbeat. Things that are
    not readings also change a tile -- "show on the home screen" turned
    off, a device renamed or removed -- and none of those move a number the
    engine counts. A full walk every READINGS_PUSH_SECONDS regardless
    catches them, exactly as before.
    """
    last: Optional[str] = None
    seen_seq = -1
    forced = 0.0
    ha_at = 0.0
    while True:
        await asyncio.sleep(READINGS_TICK_SECONDS)
        try:
            if not _LIVE_PHONES:
                last = None      # nobody heard the last one; re-send on join
                continue
            # HOME ASSISTANT HAS TO BE ASKED (2026-09-12). Everything else
            # in this loop is event-driven off MQTT, because a board TELLS
            # the hub when something changed. HA tells it nothing -- no
            # event ear, no subscription anywhere -- so its devices were
            # redrawn every fifteen seconds from a dict that nothing on a
            # timer ever refreshed: the same number, for hours.
            #
            # ONE get_states for the whole house, IN A THREAD, on its own
            # slow cadence. NOT one call per device on the event loop --
            # that is precisely the fault live_reading_rows was written to
            # avoid (2026-09-03, "List devices" timing out five seconds at
            # a time with HA down), and the rule that nothing on the loop
            # ever waits on Home Assistant is unchanged.
            #
            # A refresh that finds HA down stamps nothing, so those tiles
            # go stale by themselves and say so. That is the honest
            # outcome, and it is why this does not need its own error path.
            if (time.monotonic() - ha_at) >= config.HA_POLL_SECONDS:
                ha_at = time.monotonic()
                try:
                    if await asyncio.to_thread(shell.dm.refresh_ha_states):
                        forced = 0.0          # something moved: push it now
                except Exception:                          # noqa: BLE001
                    pass
            engine = shell.automations
            seq = engine.reading_seq() if engine is not None else None
            now = time.monotonic()
            due = (now - forced) >= READINGS_PUSH_SECONDS
            if seq is not None and seq == seen_seq and not due:
                continue         # nothing arrived, and the floor is not up
            if seq is not None:
                seen_seq = seq
            if due:
                forced = now
            rows = shell.live_reading_rows()
            fingerprint = json.dumps(rows, sort_keys=True, default=str)
            if fingerprint == last:
                continue
            last = fingerprint
            await _broadcast({"type": "readings_live", "sensors": rows})
        except Exception:                              # noqa: BLE001
            pass                        # a bad poll skips a beat, not a leg


#: How often a rule's open questions are offered to the phones. Fast,
#: because an approval only lives AUTOMATION_APPROVAL_SECONDS (120) and a
#: question asked with four seconds left is not a question. Readings can
#: wait fifteen seconds; a door cannot.
APPROVALS_PUSH_SECONDS = 5.0


async def _approvals_watcher(shell) -> None:
    """Deliver what an `ask` action raised.

    THE ROAD WAS MISSING, and only the last few feet of it. `_ask` builds
    the approval properly, drops it in `automations.approvals`, and
    returns -- but the ONLY thing that ever turned that list into
    something a phone could see was menu_check_notices: "Pending
    actions", a menu item somebody has to go and choose. So a rule fired,
    asked its question into a list nobody was reading, and the question
    deleted itself 120 seconds later without having been put to anyone.
    Her report, 2026-09-01: "controlled it and it turned on but no
    approval". The rule was right; it was shouting into a drawer.

    A QUESTION IS NOT A REPORT. It goes out the moment it exists, with
    now=True so it reaches the glass rather than a list to be read later
    -- the same shape menu_check_notices builds, sent on a timer instead
    of on request, so the app needs no new frame and no change at all.

    Ordinary alerts ride along behind it. `_ask` files the same id in
    both lists, so answering the approval also settles its alert and
    nobody is told twice.

    IDS ARE STABLE AND RE-SENDING REPLACES (see Shell._notice), but a
    phone must not be re-asked every five seconds, so what has been said
    is remembered -- and forgotten again as approvals lapse, and on the
    way out of an empty house, so a phone that joins late still gets a
    question that is still open.
    """
    eng = shell.automations
    told: set = set()
    while True:
        await asyncio.sleep(APPROVALS_PUSH_SECONDS)
        try:
            # A HOME ASSISTANT THAT WAS DOWN AT BOOT IS ASKED FOR AGAIN
            # (2026-09-11). _try_connect_ha runs once, in the shell's
            # __init__, so a hub that started while HA was stopped said
            # "running without live HA control until it's back" and then
            # never looked -- the integration door stayed hidden, HA-backed
            # devices stayed undriveable, and only a restart fixed it. This
            # loop is already running and the check has its own cooldown
            # (HA_RETRY_SECONDS), so it costs one cheap call a minute at
            # most, and nothing at all on a hub with no HA configured.
            # Before the _LIVE_PHONES test on purpose: the house should
            # recover whether or not anybody is holding a phone.
            await asyncio.to_thread(shell.reconnect_ha_if_needed)
            if eng is None:
                # No engine, no questions -- but the HA retry above still
                # runs. This used to `return` before the loop, so a hub whose
                # automation engine failed to start also never reconnected
                # to a Home Assistant that was down at boot.
                continue
            if not _LIVE_PHONES:
                # Nobody heard them -- but only OPEN QUESTIONS are worth
                # asking again when somebody joins. An alert already
                # spoken stays spoken: forgetting those too meant walking
                # back into the house and being handed, as news, the
                # question you answered before you left.
                told -= {a["id"] for a in eng.pending_approvals()}
                continue
            pending = eng.pending_approvals()
            for ask in pending:
                if ask["id"] in told:
                    continue
                told.add(ask["id"])
                await _broadcast({"type": "notice", **shell._notice(
                    ask["id"], f"🔐 {ask['device_name']}", ask["text"],
                    kind="menu", value=f"approve:{ask['id']}",
                    label="Approve with PIN", now=True)})
            alerts = list(eng.alerts)[-10:]
            for alert in alerts:
                if alert["id"] in told:
                    continue
                told.add(alert["id"])
                await _broadcast({"type": "notice", **shell._notice(
                    alert["id"], f"⚡ {alert['title']}", alert["body"])})
            # Lapsed questions leave the set with them: without this it is
            # a leak that also silences a rule which asks the same thing
            # again an hour later.
            told &= ({a["id"] for a in pending}
                     | {a["id"] for a in alerts})
        except Exception:                              # noqa: BLE001
            pass                    # a bad tick skips a beat, not a leg


#: Sockets that have not yet proven anything. Counted SEPARATELY from
#: authenticated ones: when the two shared one cap of 8, a peer with no key
#: at all could hold every slot by reconnecting each AUTH_TIMEOUT and lock
#: every real phone out. Pre-auth sockets are cheap and short-lived (they
#: die at AUTH_TIMEOUT), so their cap is wider.
MAX_PENDING_AUTH = 32
_pending_auth = 0


async def _handler(websocket, shell, lock):
    global _active_connections, _pending_auth
    if _pending_auth >= MAX_PENDING_AUTH:
        try:
            print(f"[WS] refused {websocket.remote_address} -- too many "
                  f"unauthenticated connections ({MAX_PENDING_AUTH})")
            await websocket.close()
        except Exception:
            pass
        return
    _pending_auth += 1
    handler = ConnectionHandler(websocket, shell, lock)
    try:
        await handler.run()
    finally:
        # run() moves the connection from pending to active once it has
        # authenticated (see _count_authenticated); whichever bucket it is
        # in at the end is the one released.
        if handler._counted_active:
            _active_connections -= 1
        else:
            _pending_auth -= 1
        handler._released.set()


async def _replace_old_sessions(new: "ConnectionHandler") -> None:
    """Close every other live session of `new`'s phone, and wait (briefly)
    for their connection slots to be released."""
    olds = [h for h in list(_LIVE_PHONES)
            if h is not new and h._phone_id == new._phone_id]
    for old in olds:
        _LIVE_PHONES.discard(old)
        event("CONN", f"{old._peer()} replaced by a new session from the "
                      f"same phone")
        try:
            await asyncio.wait_for(old.websocket.close(code=1000,
                                                       reason="replaced"), 2)
        except Exception:                           # noqa: BLE001
            transport = getattr(old.websocket, "transport", None)
            if transport is not None:
                transport.abort()           # a dead peer never answers close
        try:
            await asyncio.wait_for(old._released.wait(), 3)
        except asyncio.TimeoutError:
            pass


def _count_authenticated(handler: "ConnectionHandler") -> bool:
    """Move a freshly authenticated connection into the authenticated bucket.
    False (and the caller should hang up) when that bucket is full."""
    global _active_connections, _pending_auth
    if _active_connections >= MAX_CONNECTIONS:
        return False
    _pending_auth -= 1
    _active_connections += 1
    handler._counted_active = True
    return True


def _address_available(host: str, port: int) -> Optional[str]:
    """The address `host` resolves to, IF this machine can bind it. Else None.

    "Can I bind it?" is the question that matters and the only portable way to
    ask it -- an address belongs to us exactly when bind() succeeds. Resolution
    alone is not enough: a MagicDNS name can resolve from a cached netmap while
    tailscaled holds no address, and a hardcoded AP address resolves trivially
    whether or not hostapd is running.

    Binds and immediately closes. There is a race between that and the real
    bind a moment later, but the alternative is enumerating interfaces, which
    needs a dependency and still cannot tell you whether the port is free.
    """
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except socket.gaierror:
        return None
    for family, kind, proto, _canon, addr in infos:
        probe = socket.socket(family, kind, proto)
        try:
            if _REUSE_IS_SAFE:
                # Unix only: tolerate TIME_WAIT without tolerating a live
                # listener. On Windows the same flag would tolerate both, and
                # this probe would report every busy address as available.
                probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            probe.bind(addr)
            return str(addr[0])
        except OSError:
            continue
        finally:
            probe.close()
    return None


def _start_static_https_server(directory: str, ssl_context: ssl.SSLContext,
                               port: int, host: str, spki: str = ""):
    """Serve ONLY the test client page. Returns httpd for clean shutdown. The
    HMAC secret is NOT injected into the page -- the client prompts for it.

    ONE page, two views. A brief experiment split this into real /enroll and /
    routes; it was reverted. A route change destroys the JS context, which
    means the websocket closes and re-authenticates between finishing
    enrollment and issuing the first command, and the shared secret has to
    survive the navigation in sessionStorage -- weakening the "never stored"
    property this module's docstring describes, to buy a URL. The isolation
    that actually matters (you cannot issue commands with nobody enrolled) is
    enforced in _reader_loop, server-side, where a typed URL cannot reach it.

    A native app has no such constraint: screens are views over one persistent
    session, and the secret belongs in the platform keystore. The
    {"type":"view"} frame maps onto a navigation controller unchanged.

    The only substitution is __WS_URL__, which carries config.WS_PORT into the
    page. The client must not hardcode a port: the HTTPS port (8443) and the
    WebSocket port are independent, and hardcoding made the page dial the
    wrong one whenever they differed."""

    class Handler(http.server.SimpleHTTPRequestHandler):
        protocol_version = "HTTP/1.1"  # keep-alive: reuse one TLS handshake, not one per request
        # A socket timeout for the whole exchange: without one, a client that
        # opens a keep-alive connection and goes quiet holds a thread forever.
        timeout = 30

        def _serve_test_client(self, send_body: bool):
            path = urllib.parse.urlparse(self.path).path
            if path == "/favicon.ico":
                self.send_response(204)  # stop the browser's favicon hunt (was 404-ing)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            if path not in ("/", "/ws_test_client.html"):
                self.send_error(404)
                return
            html_path = os.path.join(directory, "ws_test_client.html")
            try:
                with open(html_path, "r", encoding="utf-8") as f:
                    content = f.read()
            except FileNotFoundError:
                self.send_error(404, "ws_test_client.html not found")
                return
            # ONLY THE PORT is substituted. The ADDRESS comes from the
            # browser, via location.hostname in the page itself.
            #
            # Neither value on this side is right. config.WS_HOSTNAME is the
            # TAILNET name, so the page served on the access point told the
            # client to dial a network it has no route to. And `host` here is
            # the bound ADDRESS -- 100.120.186.34 for the tailnet -- while
            # `tailscale cert` issues its certificate for the NAME, so using
            # it would swap one broken page for another with a certificate
            # mismatch.
            #
            # The browser is the only party that knows which of the two names
            # for this machine the person actually typed, and location.hostname
            # is exactly that. It cannot be wrong: the page loaded from it.
            content = content.replace("__WS_PORT__", str(config.WS_PORT))
            # AND THE SPKI of the certificate this very listener serves --
            # the page's HMAC has to cover it (phones.signed_message) and
            # JavaScript cannot read the certificate it was served with. It
            # is public (it is in every TLS handshake). It gives the browser
            # page no channel binding of its own -- a party able to rewrite
            # this page could equally rewrite the secret field -- which is
            # the browser's limit, not a new one.
            content = content.replace("__HUB_SPKI__", spki)
            # NO __API_KEY__ injection -- the secret must not be shipped in the page.
            body = content.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            # This page takes a shared secret into a password field. It had
            # no security headers at all. connect-src has to allow wss: for
            # the socket; everything else is same-origin and inline (the
            # page is one self-contained file).
            self.send_header(
                "Content-Security-Policy",
                "default-src 'self'; script-src 'self' 'unsafe-inline'; "
                "style-src 'self' 'unsafe-inline'; connect-src 'self' wss:; "
                "img-src 'self' data:; frame-ancestors 'none'; base-uri 'none'",
            )
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Strict-Transport-Security", "max-age=63072000")
            self.end_headers()
            if send_body:
                self.wfile.write(body)

        def _guarded(self, send_body: bool):
            """Serve, treating a vanished client as ordinary rather than fatal.

            A browser that reloads, navigates away, or lets a keep-alive
            socket lapse closes the connection mid-response. http.server does
            not catch that, so socketserver prints a full traceback for what
            is a completely normal disconnect -- and a traceback in the log of
            a system that opens locks is not something to leave lying around
            looking like a crash.

            ConnectionError covers the plain-socket cases; ssl.SSLEOFError and
            ssl.SSLError cover the TLS ones, which is what this always is,
            because everything here is HTTPS.
            """
            try:
                self._serve_test_client(send_body=send_body)
            except (ConnectionError, ssl.SSLError):
                # Nothing to report and nobody to report it to -- the client
                # is already gone.
                pass

        def do_GET(self):
            self._guarded(send_body=True)

        def do_HEAD(self):
            self._guarded(send_body=False)

        def log_message(self, fmt, *args):
            print(f"[HTTPS] {self.address_string()} - {fmt % args}")

    class _Server(socketserver.ThreadingTCPServer):
        # Without SO_REUSEADDR the listening socket sits in TIME_WAIT after a
        # restart and binding fails with "Address already in use" for a minute
        # or so -- which, during development, reads as "the server is broken"
        # rather than "wait 60 seconds".
        # Same asymmetry as above, and this one is a real server rather than a
        # probe: on Windows True would let a SECOND instance bind the page port
        # while the first is still serving it, with the OS free to deliver a
        # given connection to either. On Unix it only avoids a TIME_WAIT wait
        # on restart, which is worth having.
        allow_reuse_address = _REUSE_IS_SAFE
        daemon_threads = True

        def finish_request(self, request, client_address):
            # THE TLS HANDSHAKE RUNS HERE, in the per-connection thread,
            # bounded by a timeout. With the default handshake-on-accept it
            # ran inside serve_forever's own accept() -- one thread for the
            # whole page server, no timeout -- so a single client that
            # opened TCP and never sent a ClientHello froze the page for
            # everybody until it went away.
            request.settimeout(Handler.timeout)
            try:
                request.do_handshake()
            except (OSError, ValueError):
                return                     # shutdown_request() closes it
            super().finish_request(request, client_address)

    # Bind where the WebSocket binds. 0.0.0.0 published this page on every
    # interface -- LAN and, behind a careless router, beyond -- while the
    # entire security model of this module assumes the tailnet is the only
    # way in.
    httpd = _Server((host, port), Handler)
    httpd.socket = ssl_context.wrap_socket(httpd.socket, server_side=True,
                                           do_handshake_on_connect=False)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    print(f"[HTTPS] Serving on https://{host}:{port}")
    # Same mistake, same fix: printing WS_HOSTNAME here put the tailnet name
    # under the access-point listener, so following the log's own link tested
    # the wrong network.
    print(f"[HTTPS] Open: https://{host}:{port}/ws_test_client.html")
    return httpd


def _build_ssl_context(certfile: str, keyfile: str) -> ssl.SSLContext:
    """A TLS context for ONE binding.

    Per-binding, not global. The tailnet certificate comes from `tailscale
    cert` and is a Let's Encrypt certificate for the .ts.net name -- it cannot
    be extended to cover 192.168.4.1, because no public CA will certify a
    private address. So the access points carry their own self-signed one, and
    which certificate a listener presents depends on which address it answers.
    """
    if not os.path.isfile(certfile) or not os.path.isfile(keyfile):
        raise RuntimeError(f"TLS cert/key not found:\n  {certfile}\n  {keyfile}")
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.load_cert_chain(certfile=certfile, keyfile=keyfile)
    return context


def _binding_cert(entry: Dict[str, str]) -> Tuple[str, str]:
    """(cert, key) for a binding, falling back to the tailnet pair."""
    return (entry.get("cert") or config.TLS_CERT,
            entry.get("key") or config.TLS_KEY)


class Listener:
    """The pair of servers -- WebSocket and static HTTPS -- bound to ONE address.

    They move together on purpose. The client page computes its socket URL from
    the address it was served on (`wss://${location.hostname}:PORT`), so a page
    served from one address and a socket listening on another cannot meet.
    """

    def __init__(self, name: str, host: str, address: str,
                 ws: Any, httpd: Any) -> None:
        self.name = name
        self.host = host
        self.address = address
        self.ws = ws
        self.httpd = httpd

    async def close(self) -> None:
        self.ws.close()
        try:
            await self.ws.wait_closed()
        except Exception:
            pass
        try:
            # shutdown() blocks until serve_forever notices (up to its poll
            # interval) -- not on the loop every phone's session runs on.
            await asyncio.to_thread(self.httpd.shutdown)
        finally:
            # shutdown() stops serve_forever but does NOT release the listening
            # socket; without server_close() the port stays bound.
            self.httpd.server_close()


async def _bind(entry: Dict[str, str], address: str, static_dir: str,
                https_port: int, handler: Any, origins: List[Optional[str]]) -> Listener:
    certfile, keyfile = _binding_cert(entry)
    ssl_context = _build_ssl_context(certfile, keyfile)
    # A resolved binding (the router) is announced and re-checked by the
    # address it actually got, and the browser page served from it needs
    # that address as an allowed origin -- the static list built at start
    # cannot know it.
    host = entry["host"]
    names = {entry.get("origin"), entry.get("host")}
    if host == hub_net.LAN_SENTINEL:
        host = address
        names.add(address)
    # This entry's own names, too: the list main() built covers only the
    # bindings that existed at start, and a tailnet adopted at run time
    # (_adopt_tailnet_binding) would otherwise refuse its own page's Origin.
    origins = list(origins) + [o for o in _origins_for(names, https_port)
                               if o not in origins]
    try:
        page_spki = _phones.spki_fingerprint(certfile)
    except Exception:                                  # noqa: BLE001
        page_spki = ""                 # the page then says it cannot pair
    httpd = _start_static_https_server(static_dir, ssl_context,
                                       port=https_port, host=address,
                                       spki=page_spki)
    try:
        ws = await websockets.serve(
            handler, address, config.WS_PORT,
            ssl=ssl_context, origins=origins,
            # max_queue bounds how many max_size frames the library buffers
            # per socket before applying TCP back-pressure -- and it applies
            # BEFORE auth. At 32 an unauthenticated peer could pin ~256 MB
            # per socket (x MAX_PENDING_AUTH). The reader drains
            # continuously, so a short queue costs a real phone nothing.
            max_size=8_000_000, max_queue=4,
            # ping_timeout=None: NEVER close a connection for inactivity.
            #
            # This was ping_timeout=90, so the server dropped any peer that
            # missed pongs for 90 seconds. A phone that locks its screen or
            # backgrounds the tab gets its timers throttled and does exactly
            # that -- and from the server side it looks like a dead peer, not
            # an error, so the socket closed silently while the user was doing
            # nothing at all. Reading a phrase and finding the record button
            # is easily 90 seconds.
            #
            # Pings still go out every 20s -- they keep NAT and relay mappings
            # alive, which matters on the Tailscale DERP path. They just no
            # longer have a deadline attached.
            #
            # Cost: a genuinely dead connection is not reaped until the OS
            # notices the TCP socket is gone. Acceptable for a handful of
            # trusted clients on a tailnet; revisit if this ever faces many.
            ping_interval=20, ping_timeout=None, close_timeout=5,
        )
    except Exception:
        # FIX: the page server above is ALREADY serving. Leaving it up when
        # the WebSocket bind fails leaks the HTTPS port -- held by US -- so
        # every later reconcile finds "WS_PORT free but HTTPS_PORT busy",
        # skips this binding, and it stays wedged until a process restart.
        # Both clients (browser page AND app socket) depend on the pair
        # coming up together; half a binding serves neither. (shutdown()
        # waits for serve_forever's poll: off the loop, as in Listener.close.)
        await asyncio.to_thread(httpd.shutdown)
        httpd.server_close()
        raise
    _CERT_BY_ADDRESS[address] = certfile
    return Listener(entry["name"], host, address, ws, httpd)


def _origins_for(names: Any, https_port: int) -> List[str]:
    """The browser Origins a page served under each of `names` sends: with
    and without the page port. The "lan" sentinel is no name -- its address
    is added per listener in _bind."""
    return [origin for name in names if name and name != hub_net.LAN_SENTINEL
            for origin in (f"https://{name}:{https_port}", f"https://{name}")]


def _validate_bindings() -> List[str]:
    """Problems with config.NETWORK_BINDINGS, in plain words. Empty if fine.

    That list is meant to be EDITED -- add a machine, drop a route, reorder the
    preference. So a mistake in it should read as a mistake in it, not as a
    KeyError raised from inside a loop three functions away, or as a duplicate
    name that silently makes the failover log ambiguous.
    """
    problems: List[str] = []
    entries = config.NETWORK_BINDINGS
    if not isinstance(entries, list) or not entries:
        return ["NETWORK_BINDINGS is empty or is not a list -- there is nowhere "
                "to listen. Add at least one entry."]

    seen = set()
    for i, entry in enumerate(entries):
        where = f"NETWORK_BINDINGS[{i}]"
        if not isinstance(entry, dict):
            problems.append(f"{where} is {type(entry).__name__}, expected a dict "
                            f"with 'name', 'host' and 'origin'.")
            continue
        for key in ("name", "host", "origin"):
            value = entry.get(key)
            if not isinstance(value, str) or not value.strip():
                problems.append(f"{where} is missing a non-empty {key!r} "
                                f"(got {value!r}).")
        # A missing certificate is a startup problem, not a failover-time
        # surprise. Without this you learn that the access-point certificate
        # was never generated at the moment the AP comes up and the switch
        # fails -- which is the worst possible moment.
        certfile, keyfile = _binding_cert(entry)
        for label, path in (("cert", certfile), ("key", keyfile)):
            if not os.path.isfile(path):
                problems.append(f"{where} ({entry.get('name')!r}): {label} not "
                                f"found at {path}")
        name = entry.get("name")
        if isinstance(name, str):
            if name in seen:
                problems.append(f"{where}: duplicate name {name!r} -- the failover "
                                f"log could not tell the two apart.")
            seen.add(name)
    return problems


def _expanded_bindings() -> List[Dict[str, str]]:
    """config.NETWORK_BINDINGS with the "lan" entry opened out into ONE
    entry per private address this machine holds right now (hub_net.
    local_addresses): the router's, a hotspot it runs, any of them. Each is
    named "lan <address>", so an address that appears gets a listener and
    one that vanishes loses it. Runs subprocesses: call it off the loop."""
    out: List[Dict[str, str]] = []
    for entry in config.NETWORK_BINDINGS:
        if entry.get("host") != hub_net.LAN_SENTINEL:
            out.append(entry)
            continue
        for address in hub_net.local_addresses():
            out.append({**entry, "name": f"{entry['name']} {address}",
                        "host": address, "origin": address})
    return out


def _serves(entry_name: str, live_names) -> bool:
    """Is config entry `entry_name` being served -- itself, or (for "lan")
    any of the per-address listeners it opened out into?"""
    return any(n == entry_name or n.startswith(entry_name + " ")
               for n in live_names)


def _all_available(https_port: int, skip: Set[str],
                   entries: Optional[List[Dict[str, str]]] = None
                   ) -> List[Tuple[Dict[str, str], str]]:
    """EVERY configured binding this machine can serve, with its address.

    All of them, not the best one. The server cannot know where the phone is --
    it can only know which addresses it holds -- so it holds them all open and
    lets the phone reach whichever it can see. Near, the phone finds the access
    point; away, it finds the tailnet. Nothing switches, nothing guesses, and
    the two never contend because neither network can route to the other's
    address.

    This replaced a preference-ordered failover that bound exactly ONE, which
    was a mistake with a specific shape: "which address do I have?" is a
    question the SERVER can answer, and it was allowed to stand in for "where
    is the phone?", which only the phone can answer. With the access point
    running permanently -- which is how it would run -- the tailnet listener
    never existed and reaching the house from outside was impossible.
    """
    found: List[Tuple[Dict[str, str], str]] = []
    for entry in (entries if entries is not None else _expanded_bindings()):
        # Never probe a binding we are already serving. Its port is held BY US,
        # so the probe would report it unavailable -- which is the bug that made
        # the server close its own listener every re-check.
        if entry["name"] in skip:
            continue
        # "lan" is resolved HERE, every time: the router's address is a
        # fact about now, not about the config file (see hub_net).
        host = hub_net.resolve_host(entry["host"])
        if not host:
            continue
        address = _address_available(host, config.WS_PORT)
        if address is None:
            continue
        # The page must be servable on the SAME address, or the client is
        # served from one place and told to open a socket somewhere else.
        # Probed on `address` -- the one both servers will bind -- not on
        # `host`: a name with both an IPv4 and an IPv6 address could pass
        # this check on one family and be bound on the other.
        if _address_available(address, https_port) is None:
            event("CONN", f"binding {entry['name']!r}: {config.WS_PORT} is free but "
                          f"{https_port} is not -- skipping")
            continue
        found.append((entry, address))
    return found


async def _reconcile(state: Dict[str, Any], static_dir: str, https_port: int,
                     handler: Any, origins: List[Optional[str]]) -> None:
    """Make the live listeners match the addresses this machine actually has.

    ONE rule: every configured binding whose address exists gets a listener,
    and nothing else does. Bring up what appeared, drop what vanished.

    Deliberately no "only switch when idle" caution, because nothing switches.
    A listener is dropped only when its address has GONE -- and a connection on
    an address that no longer exists is already dead, so there is nothing left
    to protect.
    """
    live: Dict[str, Listener] = state["listeners"]
    # "lan" opened out into one entry per address held NOW (off the loop:
    # it asks the OS for its adapters).
    expanded = await asyncio.to_thread(_expanded_bindings)
    configured = {e["name"] for e in expanded}

    # KEEP: live listeners that should stay up. Two conditions, and both are
    # needed -- an earlier version tested only the second, so deleting an entry
    # from NETWORK_BINDINGS did nothing until a restart.
    #
    # The probe here is port 0, NOT the real port. For a listener we already
    # hold, "is host:8765 free?" is always no -- WE hold it -- and answering
    # that question about our own listener is what made the server close and
    # rebind every 20 seconds, taking the phone down with it. Port 0 asks the
    # question that actually matters: does this address still belong to this
    # machine? The OS hands out a spare port, so our own listener is no
    # obstacle.
    #
    # The probe is on listener.ADDRESS, the one actually bound, not on its
    # host NAME: re-resolving a tailnet name answered for whatever address
    # the name points at NOW, so after a tailnet switch the listener on the
    # old, dead address was kept for ever and the new one never bound. And
    # a listener whose configured host was CHANGED (_adopt_tailnet_binding
    # renaming the tailnet entry) is dropped, so it is rebound with the new
    # name's origins.
    hosts = {e["name"]: e["host"] for e in expanded}

    def _still_ours(name: str, listener: Listener) -> bool:
        if name not in configured:
            return False
        host = hosts.get(name)
        if host != hub_net.LAN_SENTINEL and host != listener.host:
            return False
        return _address_available(listener.address, 0) is not None

    # BOTH PROBES RUN IN A THREAD. They resolve names (getaddrinfo on a
    # MagicDNS name can stall for seconds) and hub_net asks the routing
    # table via a subprocess -- every BIND_RECHECK_SECONDS, on the loop that
    # carries every phone's session.
    live_now = dict(live)
    keep = await asyncio.to_thread(
        lambda: {name for name, listener in live_now.items()
                 if _still_ours(name, listener)})

    # START: configured bindings we are NOT already serving, whose address and
    # both ports are free. set(live) is passed as `skip` so live listeners are
    # never probed on the real port -- see above. (The safer superset: it also
    # covers listeners about to be dropped this cycle, whose address is gone
    # and could not restart this round anyway.)
    to_start: Dict[str, Tuple[Dict[str, str], str]] = {
        entry["name"]: (entry, address)
        for entry, address in await asyncio.to_thread(
            _all_available, https_port, set(live_now), expanded)
    }

    # Two separate collections on purpose. A previous version used one dict for
    # both, which meant inventing a fake binding entry for every live listener
    # purely to mark it as "keep" -- an entry with no 'origin', no 'cert' and no
    # 'key' that was never read, sitting one relaxed guard away from reaching
    # _bind malformed.
    for name in list(live):
        if name in keep:
            continue
        event("CONN", f"binding {name!r} lost its address -- closing that listener")
        listener = live.pop(name)
        # The cert map must follow the listener out, or bound_tailnet_ip()
        # keeps telling phones an address nothing answers on (a tailnet
        # switch hands the hub a new 100.x address and drops the old one).
        _CERT_BY_ADDRESS.pop(listener.address, None)
        try:
            await listener.close()
        except Exception:
            traceback.print_exc()

    for name, (entry, address) in to_start.items():
        try:
            live[name] = await _bind(entry, address, static_dir,
                                     https_port, handler, origins)
        except Exception as e:
            # One binding failing must not take the others down: a missing
            # access-point certificate should not cost you remote access.
            event("CONN", f"binding {name!r} at {address} could not start: "
                          f"{type(e).__name__}: {e}")
            continue
        _announce(live[name], https_port)


#: A freeze longer than this prints every thread's stack to the console.
FREEZE_DUMP_AFTER = 5.0


async def _freeze_watch() -> None:
    """THE HUB'S OWN WATCHDOG (2026-09-28). A phone's keep-alive ping goes
    unanswered when this process's event loop is stuck in something that
    does not await -- and then the phone drops, new connections time out, and
    nothing here says why.

    Every second the loop proves it is alive. If it cannot for
    FREEZE_DUMP_AFTER seconds, faulthandler (its own thread, so it fires
    even while the loop is stuck) writes the stack of every thread to
    data/freeze_stacks.txt -- the top of the main thread's stack is the exact
    line holding the hub. When the loop comes back it says for how long.

    TO A FILE, NOT THE CONSOLE (2026-09-29). Written to the console, the
    dump itself could hang: a Windows console with text selected pauses
    every write to it, and re-arming the timer waits for a dump in
    progress -- so a 235 s freeze showed the loop "stuck" in this very
    function. A file write never waits on a window."""
    import faulthandler
    path = os.path.join(str(config.DATA_DIR), "freeze_stacks.txt")
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        out = open(path, "a", encoding="utf-8")
    except OSError:
        return                               # no watchdog rather than a risky one
    try:
        while True:
            faulthandler.dump_traceback_later(FREEZE_DUMP_AFTER, repeat=False,
                                              file=out)
            started = time.monotonic()
            await asyncio.sleep(1.0)
            lag = time.monotonic() - started - 1.0
            if lag > 2.0:
                out.write(f"--- {time.strftime('%Y-%m-%d %H:%M:%S')} the loop "
                          f"was stuck for {lag:.1f} s (stacks above) ---\n")
                out.flush()
                event("FREEZE", f"the hub was stuck for {lag:.1f} s and answered "
                                f"nothing (phones drop after 20 s). Stacks: {path}")
    finally:
        faulthandler.cancel_dump_traceback_later()
        out.close()


async def _supervise_bindings(state: Dict[str, Any], static_dir: str,
                              https_port: int, handler: Any,
                              origins: List[Optional[str]],
                              stop: asyncio.Event) -> None:
    """Re-reconcile on a timer, so an address appearing later is picked up.

    This is what makes a reboot survivable. tailscaled routinely takes several
    seconds after the network is up to acquire its address, and hostapd may
    start after this process does; a server that resolved once at t=0 would
    simply be missing whichever route was slower to arrive.
    """
    while not stop.is_set():
        try:
            await asyncio.wait_for(stop.wait(), timeout=config.BIND_RECHECK_SECONDS)
            return
        except asyncio.TimeoutError:
            pass
        try:
            # Tailscale that came up after the hub did (tailnet.name_if_late).
            from core import tailnet as _tailnet
            access = await asyncio.to_thread(_tailnet.access_state)
            if access.get("state") == "on":
                _remember_tailnet(access.get("name", ""), access.get("ip", ""))
            if access.get("state") == "on" and \
                    "tailnet" not in state["listeners"]:
                _adopt_tailnet_binding(_tailnet.name_if_late(False))
            await _reconcile(state, static_dir, https_port, handler, origins)
            # REMOTE ACCESS CHANGED (Tailscale up after the hub, paused,
            # back): told to every phone now, not at its next connect.
            was = state.get("remote")
            state["remote"] = access.get("state")
            if was is not None and was != state["remote"]:
                await _tell_remote_access(access)
        except Exception:
            traceback.print_exc()


async def _tell_remote_access(access: dict) -> None:
    """Every live phone hears the new remote-access state: the address when
    it works (and the notice goes), else the one notice that says why."""
    from core import tailnet as _tn
    event("TAILNET", f"remote access is now {access.get('state') or '?'}")
    await _broadcast({"type": "tailnet_state",
                      "state": access.get("state", ""),
                      "tailnet_name": access.get("name", ""),
                      "tailnet_ip": access.get("ip", "") or bound_tailnet_ip(),
                      "account": access.get("account", "")})
    notice = _tn.remote_access_notice(access)
    if notice is None:
        await _broadcast({"type": "notice_gone", "id": "tailnet:not_joined"})
    else:
        await _broadcast({"type": "notice", **notice})


def _announce(listener: Listener, https_port: int) -> None:
    print(f"[WS] Listening on {listener.name!r}: {listener.host} ({listener.address})")
    print(f"[WS]   client page  https://{listener.host}:{https_port}/")
    print(f"[WS]   websocket    wss://{listener.host}:{config.WS_PORT}")


def _check_api_key_at_startup() -> None:
    """The API key is now an ENROLLMENT credential, not the way in.

    Paired phones authenticate with their own keypairs and never touch it,
    so an unset key is only fatal when there is nobody paired -- in which
    case there is no way for anyone to ever connect, and saying so at
    startup beats discovering it from a phone that will not pair.

    With phones already registered, an unset key is a legitimate and rather
    strong configuration: the hub keeps working for the phones it knows and
    NO NEW PHONE CAN ENROLL AT ALL, whatever anyone knows. Worth saying out
    loud rather than passing over in silence, because it is also what a
    forgotten `export` looks like.
    """
    if os.environ.get("HOMEVOICE_API_KEY"):
        return
    if PHONES.count() == 0:
        config.require_api_key()          # raises with the full explanation
        return
    print(f"[WS] HOMEVOICE_API_KEY is not set. The {PHONES.count()} paired "
          f"phone(s) will still connect -- they sign with their own keys -- "
          f"but no new phone can be paired until it is set.")


def _clear_hub_if_asked() -> None:
    """The last person chose "Clear the whole hub" (shell._clear_whole_hub
    left a note and restarted the hub): everything about the house is
    deleted here, at the start, before anything is loaded. Kept: the
    software, the models, this hub's own keys and certificate, the broker's
    own setup, and a few caches with nothing personal in them."""
    import shutil
    home = os.path.dirname(config.PHONES_FILE)
    marker = os.path.join(home, "clear-hub")
    if not os.path.exists(marker):
        return
    print("[WS] Clearing the whole hub, as its last person asked...")

    def gone(path: str) -> None:
        try:
            if os.path.isdir(path) and not os.path.islink(path):
                shutil.rmtree(path, ignore_errors=True)
            elif os.path.lexists(path):
                os.remove(path)
        except OSError as exc:
            print(f"[WS]   {path} not deleted ({exc})")

    # Camera recordings, in every place they were kept (if this hub records).
    settings_path = getattr(config, "RECORDING_SETTINGS", None)
    try:
        from hub import recorder
        store = recorder.store_from(recorder.read_settings())
        for slug in store.cameras():
            recorder.delete_camera(store, slug)
    except Exception as exc:                        # noqa: BLE001
        if settings_path:
            print(f"[WS]   recordings not all deleted ({exc})")
    keep = {"challenge_words.txt", "clock_last_seen", "nav_vectors.npz",
            "decision_cache.pkl"}
    data = str(config.DATA_DIR)
    for name in (os.listdir(data) if os.path.isdir(data) else []):
        if name not in keep:
            gone(os.path.join(data, name))
    gone(str(config.BOARDS_DIR))
    for path in (config.PHONES_FILE, settings_path,
                 getattr(config, "PHONE_VIDEOS", None),
                 os.path.join(home, "pairing-open.json"),
                 os.path.join(home, "pairing-button-only")):
        if path:
            gone(str(path))
    # The hub code is unchanged: whoever ever saw it must not walk in.
    # Pairing needs the hub's button until a phone pairs.
    try:
        os.makedirs(home, exist_ok=True)
        with open(os.path.join(home, "pairing-button-only"), "w",
                  encoding="utf-8") as f:
            f.write("1\n")
    except OSError as exc:
        print(f"[WS]   pairing-needs-button not written ({exc})")
    gone(marker)
    print("[WS] The hub is cleared: it starts as new (pairing needs the "
          "hub's button).")


async def main():
    _clear_hub_if_asked()
    _check_api_key_at_startup()

    static_dir = str(config.PROJECT_ROOT / "web")
    # The static page's port -- a real config setting, never hardcoded.
    https_port = int(config.HTTPS_PORT)

    # THE SAME STARTUP main.py DOES, and for a stronger reason.
    #
    # This is a SEPARATE PROCESS with its own InteractiveShell -- main.py
    # never launches it and cannot start anything on its behalf. On the Pi
    # this is the process that actually runs: main.py is the console you use
    # when you are sitting at the machine, and this is what the phone talks
    # to. So the headless case, the one where nobody is present to notice a
    # broker did not come back after a power cut, was the ONE path that had
    # no automatic startup at all.
    #
    # Before InteractiveShell(), not after: the shell connects to Home
    # Assistant while it is being constructed.
    await _bring_the_stack_up()

    print("[WS] Loading shared voice/NLU stack...")
    shell = InteractiveShell()
    await shell.init()
    # The shell's own conversation is NOT the terminal here: nothing with no
    # phone behind it (a watcher, the automation engine) may ask a question
    # or pass a gate under terminal trust. Every phone has its own.
    shell._io = NullIO()
    lock = asyncio.Lock()
    shell.models_busy = lambda: lock.locked() or _COMMANDS_RUNNING > 0
    handler = functools.partial(_handler, shell=shell, lock=lock)

    # FIX: validate BEFORE touching entry["origin"]/entry["host"] below. The
    # origin loop used to run first, so a malformed NETWORK_BINDINGS entry
    # died right here with a bare KeyError -- the exact failure mode
    # _validate_bindings exists to turn into plain words.
    problems = _validate_bindings()
    if problems:
        print("[WS] FATAL: config.NETWORK_BINDINGS is not usable:")
        for problem in problems:
            print(f"[WS]   {problem}")
        return

    # EVERY configured origin, whichever address is live. The Origin header is
    # a CSRF defence, not the security boundary -- auth is -- and a page can
    # only reach a socket it can route to anyway. Listing them all means
    # failing over does not also invalidate a page the phone already has open.
    #
    # None IS IN THE LIST, meaning "no Origin header at all is acceptable".
    # That reads like a hole and is not one:
    #
    #   * A BROWSER always sends Origin on a WebSocket handshake. It is not
    #     optional and a page cannot suppress it. So permitting None takes
    #     nothing away from the CSRF defence -- a malicious page still cannot
    #     connect, because it still cannot forge an allowed Origin.
    #   * A NON-BROWSER client -- the Flutter app, a script, curl -- sets the
    #     header to whatever it likes (the app sends none at all). Rejecting a
    #     missing one therefore stops only clients honest enough not to fake
    #     it, which is not a threat model.
    #
    # The comment here used to say "add None when you move off the browser",
    # as though it were a future concession. It is not: without it every
    # non-browser client is refused at the handshake, before the HMAC that
    # actually decides anything, with an error naming a header rather than a
    # credential.
    allowed_origins: List[Optional[str]] = [None]
    for entry in config.NETWORK_BINDINGS:
        allowed_origins += _origins_for({entry["origin"], entry["host"]}, https_port)

    state: Dict[str, Any] = {"listeners": {}}
    await _reconcile(state, static_dir, https_port, handler, allowed_origins)

    if not state["listeners"]:
        print("[WS] FATAL: none of the configured addresses is available on this "
              "machine:")
        for entry in config.NETWORK_BINDINGS:
            print(f"[WS]   {entry['name']:<11} {entry['host']}")
        print("[WS] Start the access point, or bring Tailscale up, then retry. "
              "Refusing to fall back to 0.0.0.0 -- that would publish the "
              "secret-entry page on every interface.")
        return

    missing = [e["name"] for e in config.NETWORK_BINDINGS
               if not _serves(e["name"], state["listeners"])]
    if missing:
        print(f"[WS] NOT listening on: {', '.join(missing)} -- those addresses are "
              f"not on this machine. They are picked up automatically if they "
              f"appear.")

    # POWER-CYCLE RECOVERY: the pairing window opens ITSELF for its first
    # PAIRING_WINDOW_SECONDS after the hub starts. This exists for the day the only
    # paired phone dies (reinstall, lost, cleared) in a house with a
    # HEADLESS hub -- without it, recovery meant SSH, which is exactly the
    # skill wall this project exists to remove. The factor it grants is
    # the same one the window has always demanded, in physical form:
    # pulling the hub's power plug and putting it back requires standing
    # in the house, which is the WPS-button model every router ships
    # with. What it does NOT waive: enrollment inside this window still
    # requires the API key's HMAC, so a stranger with power access and no
    # key gets nowhere.
    #
    # OFF BY DEFAULT (her call, 2026-10-15), and the 2026-09-02 audit's
    # point is why: a restart is not only a power plug -- a crash under
    # systemd is a restart too, and a phone that pairs in this window
    # becomes a full household member. A paired phone, the button and
    # --pair remain. HOMEVOICE_PAIR_ON_BOOT=1 (config.PAIR_ON_BOOT) puts
    # it back, for a house whose only phone may die.
    #
    # OPENED HERE, AFTER THE SOCKETS ARE LISTENING -- not at the top of
    # main(). It used to open first thing, and then the broker check, Home
    # Assistant, and the whole voice/NLU stack loaded while the window
    # ran down; on the Pi that is most of the window gone before a phone
    # could have connected at all, and the countdown a phone did see was
    # already short. The window exists so a phone can pair; it should start
    # when a phone can first reach us, and that is this line.
    if config.PAIR_ON_BOOT:
        if not _pair_reachable(False):
            # Listening only on the tailnet (or nothing local yet): a new
            # phone at home cannot reach us, so the window waits for an
            # address instead of running down (2026-10-19).
            _phones.PairingWindow().ask(
                opened_by=_phones.PairingWindow.OPENED_BY_BOOT)
            print("[WS] Pairing (power-cycle recovery) waits for a network: "
                  "it opens when the hub has an address.")
        else:
            until = _phones.PairingWindow().open(
                opened_by=_phones.PairingWindow.OPENED_BY_BOOT)
            print(f"[WS] Pairing window open for the first "
                  f"{_phones.window_words()} now that the hub "
                  f"is listening (power-cycle recovery). A new phone with the "
                  f"hub key can pair until "
                  f"{time.strftime('%H:%M:%S', time.localtime(until))}.")

    # THE HEADER LED AND BUTTON, on a Pi. Solid now -- this is the moment
    # a phone can first reach us, so it is the moment "ready" is true.
    # Held button = open the pairing window (see hub_gpio). Started after
    # the listeners for the same reason the boot window is: an LED that
    # lit while the stack was still loading would have said "ready" for
    # a minute in which nothing was.
    # THE CLOCK, before the header: a hub that came back from a power cut
    # with no time server must not refuse every phone for being "stale".
    global _CLOCK
    try:
        from hub import hub_clock
        _CLOCK = hub_clock.ClockGuard(
            os.path.join(str(config.DATA_DIR), "clock_last_seen"))
    except Exception as exc:                           # noqa: BLE001
        print(f"[CLOCK] guard not running: {exc}")
        _CLOCK = None

    global _PANEL
    try:
        from hub import hub_gpio

        def _button_opens_pairing() -> None:
            try:
                if not _pair_reachable(False):
                    # No address: no countdown (2026-10-19). The watcher
                    # opens it, full length, when one appears.
                    PHONES.window.ask(
                        opened_by=_phones.PairingWindow.OPENED_BY_HUB)
                    print("[GPIO] button held -- no network yet; pairing "
                          "opens when the hub has an address")
                    return
                until = PHONES.window.open(
                    opened_by=_phones.PairingWindow.OPENED_BY_HUB)
                print(f"[GPIO] button held -- pairing open until "
                      f"{time.strftime('%H:%M:%S', time.localtime(until))}")
                # Show it NOW. The LEDs normally follow the window file
                # through _pair_window_watcher's two-second poll, which is
                # right for a window some other process opened -- but the
                # person whose finger just came off this button is looking
                # at the header for an answer, and up to two seconds of
                # nothing reads as "the hold did not take". pairing() only
                # acts on a change, so the watcher's next poll is a no-op.
                if _PANEL is not None:
                    _PANEL.pairing(True)
                # Off the loop: hub_gpio delivers this callback ON the event
                # loop, and drawing and writing a PNG per road stalled every
                # connected phone while it happened.
                threading.Thread(target=_write_pair_codes, args=(until,),
                                 name="pair-codes", daemon=True).start()
            except Exception as exc:                   # noqa: BLE001
                print(f"[GPIO] could not open pairing: {exc}")

        def _button_closes_pairing() -> None:
            try:
                PHONES.window.close()
                print("[GPIO] button pressed -- pairing closed")
                if _PANEL is not None:
                    _PANEL.pairing(False)
            except Exception as exc:                   # noqa: BLE001
                print(f"[GPIO] could not close pairing: {exc}")

        # The pins are config's, which already reads HOMEVOICE_*_GPIO.
        panel = hub_gpio.HubPanel(
            led_pin=config.GPIO_LED_PIN,
            button_pin=config.GPIO_BUTTON_PIN,
            on_hold=_button_opens_pairing,
            loop=asyncio.get_running_loop(),
            # The second LED: the door and who came through it. 0 (or a
            # pin that will not open) means one LED, exactly as before.
            pair_led_pin=config.GPIO_PAIR_LED_PIN,
            on_cancel=_button_closes_pairing,
        )
        if panel.start(say=print):
            _PANEL = panel
            _PANEL.ready()
            # Whatever is already connected at start (a restart with phones
            # waiting) is the truth the phone LED must open on.
            _panel_phones()
    except Exception as exc:                           # noqa: BLE001
        print(f"[GPIO] not driven: {exc}")

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except (NotImplementedError, RuntimeError):
            pass  # Windows: unsupported -> KeyboardInterrupt path in __main__

    background = [asyncio.ensure_future(c) for c in (
        _supervise_bindings(state, static_dir, https_port,
                            handler, allowed_origins, stop),
        # The pairing window's town crier -- see _pair_window_watcher. Started
        # here, next to the supervisor, because it has the same lifetime: as
        # long as anything is listening, window transitions must be announced.
        _pair_window_watcher(),
        # The home screen's tiles, kept current from the cache the automation
        # engine already fills. See _readings_watcher.
        _readings_watcher(shell),
        # A rule's open questions. Same lifetime and same reason as the two
        # above: an `ask` that nobody is told about is a rule that stopped for
        # nothing. See _approvals_watcher.
        _approvals_watcher(shell),
        # The watchdog on the hub itself: a stuck event loop is named, with
        # the line that stuck it, instead of every phone silently dropping.
        _freeze_watch(),
    )]
    # The clock's minute hand: the saved time a future boot compares itself
    # against. Same lifetime as the watchers above.
    if _CLOCK is not None:
        background.append(asyncio.ensure_future(_CLOCK.run()))
    # THE BOARDS' FIRMWARE, served by the hub itself (hub/ota_server.py): the
    # address every board's OtaUrl names. A board stranded in Tasmota's
    # safeboot partition fetches its app from here and heals -- before this
    # the URL pointed at a hub that served nothing, and every failed update
    # was an afternoon with a cable (Kitchen, 2026-09-09 and 2026-09-10).
    ota_server = None
    try:
        # An install from before the one firmware folder: its images move
        # into esp_firmware/firmware/ now, before anything looks for them.
        config.settle_firmware_folder(say=print)
    except Exception as exc:                           # noqa: BLE001
        print(f"[OTA] firmware folder: {exc}")
    try:
        from hub import ota_server as _ota
        ota_port = config.OTA_HTTP_PORT
        if ota_port > 0:
            # One app image per ESP32 family, each at its own path; the
            # classic one stays at /firmware/tasmota32.bin.
            ota_server = _ota.OtaServer(
                _ota.family_images(config.firmware_path_for, say=print),
                ota_port, say=print)
            if not await ota_server.start():
                ota_server = None
    except Exception as exc:                           # noqa: BLE001
        print(f"[OTA] not serving firmware: {exc}")
        ota_server = None
    try:
        await stop.wait()
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass
    finally:
        print("[WS] Shutting down...")
        if _PANEL is not None:
            _PANEL.close()                 # LED off: not ready, truthfully
            _PANEL = None
        for task in background:
            task.cancel()
        if _CLOCK is not None:
            _CLOCK.note_now()              # the last time this hub believed
        if ota_server is not None:
            try:
                await ota_server.stop()
            except Exception:              # noqa: BLE001
                pass
        for task in background:
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
        for name, listener in list(state.get("listeners", {}).items()):
            # Listener.close() releases BOTH sockets. shutdown() alone stops
            # serve_forever without freeing the port, which matters when this
            # is restarted in-process -- which the supervisor does.
            try:
                await listener.close()
            except Exception:
                traceback.print_exc()
            print(f"[WS] Closed {name!r}")
        await _take_the_stack_down(shell)


def _console_quick_edit_off() -> None:
    """WINDOWS: a click in the hub's console window must not freeze the hub.

    With the console's "QuickEdit Mode" on (Windows' default), clicking in
    the window or selecting text to copy it PAUSES every program writing to
    that window until the selection ends -- and the hub prints a line for
    nearly everything it does, so the whole hub stops: no replies, no
    keep-alive, phones drop after 20 s. The watchdog caught it: two
    freezes, 4.6 s and 2.1 s, exactly while log lines were being copied
    out of the window (2026-09-29).

    Turned off for THIS window only, while the hub runs (Windows restores
    the setting for new windows). Copying still works: right-click the
    title bar > Edit > Mark -- which pauses output the same way, so press
    Enter as soon as the text is copied. Anywhere else: nothing to do."""
    if os.name != "nt":
        return
    try:
        import ctypes
        from ctypes import wintypes
        k32 = ctypes.windll.kernel32
        handle = k32.GetStdHandle(-10)                 # STD_INPUT_HANDLE
        mode = wintypes.DWORD()
        if not k32.GetConsoleMode(handle, ctypes.byref(mode)):
            return                                     # not a console (service, pipe)
        ENABLE_QUICK_EDIT_MODE, ENABLE_EXTENDED_FLAGS = 0x0040, 0x0080
        if mode.value & ENABLE_QUICK_EDIT_MODE:
            k32.SetConsoleMode(handle, (mode.value & ~ENABLE_QUICK_EDIT_MODE)
                               | ENABLE_EXTENDED_FLAGS)
            print("[WS] Console QuickEdit turned off for this window: a click "
                  "in it can no longer pause the hub. To copy text: right-click "
                  "the title bar > Edit > Mark, then press Enter.")
    except Exception:                                  # noqa: BLE001
        pass                    # never a reason not to start


class _NoHandshakeNoise:
    """A client that opens a TCP connection and closes it without speaking
    -- a phone's slower parallel dial, a probe that timed out while the hub
    was busy -- makes the websockets library log "opening handshake failed"
    with a full traceback. Nothing failed that matters; eight of them in a
    row buried the one line that did. Other server errors still print."""

    def filter(self, record) -> bool:            # logging.Filter protocol
        return record.getMessage() != "opening handshake failed"


def run() -> None:
    """Service entry point (python3 ws_server.py; systemd)."""
    import logging
    logging.getLogger("websockets.server").addFilter(_NoHandshakeNoise())
    _console_quick_edit_off()
    # No os.chdir -- it mutates global process CWD and would break any
    # module relying on relative paths. static_dir is resolved explicitly
    # in main() from __file__, so the working directory is irrelevant.
    # Stops only what THIS process started. After --autostart that is
    # nothing, because the broker was already up and belongs to the machine.
    run_entry(main, "\n[WS] Interrupted. Goodbye.")


if __name__ == "__main__":
    run()

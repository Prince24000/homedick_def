"""Joining this hub to a Tailscale network, driven from a paired phone.

WHY THE PHONE DOES THIS. Tailscale signs in through an identity provider in
a browser -- Google, Apple, GitHub, Microsoft, Okta, OneLogin, or custom
OIDC. There is no Tailscale password, by design, and there is no browser on
a headless Pi. But by the time remote access matters, the phone already
holds a proven, pinned, mutually-authenticated channel to the hub -- so the
phone carries a one-off auth key across, and the hub spends it.

WHAT THIS DELIBERATELY IS NOT: authentication. Being on the tailnet lets a
phone REACH the hub; it never decides whether the hub obeys it. That stays
with the phone's own keypair and the voice check, entirely local -- no
third party is consulted on any path when a door opens. config.py's
NETWORK_BINDINGS comment states the reasoning and this module honours it:
Tailscale is the road, not the key.

THE KEY IS HANDLED LIKE THE SECRET IT IS. Never written to disk, never
logged, never echoed back in any message, held in a local variable for the
length of one call. One honest limit is written down rather than papered
over: the tailscale CLI takes the key as an argument, so it is visible in
the process table to root and to the same user for the seconds the command
runs. Tailscale's own guidance addresses shell history, not argv, and there
is no file-based alternative -- the mitigation is a ONE-OFF key, spent the
moment it is used.

Every subprocess call goes through the module-level `_run`, so tests fake
the machine instead of needing one -- nothing in the test suite ever dials
Tailscale.
"""

from __future__ import annotations

import getpass
import ipaddress
import json
import os
import re
import shutil
import subprocess
from typing import Any, Dict, List, Optional

#: Tailscale hands every node one IPv4 address out of the CGNAT block.
TAILNET_V4 = ipaddress.ip_network("100.64.0.0/10")

#: Bounded, generously. `tailscale up` with a valid key returns in seconds;
#: the failure mode without a bound is a hung command holding a phone's
#: spinner forever.
UP_TIMEOUT = 60
#: Certificate issuance talks to Let's Encrypt and can be slower.
CERT_TIMEOUT = 120


def _run(argv: List[str], timeout: int) -> subprocess.CompletedProcess:
    """The one chokepoint for every external call. Tests replace this."""
    return subprocess.run(argv, capture_output=True, text=True,
                          timeout=timeout)


#: Any Tailscale auth key, whole or partial. Tailscale echoes the key back
#: INSIDE its own error text ("Use 'sudo tailscale up --auth-key=tskey-...
#: --reset'"), and this module surfaces that stderr so the one detail that
#: says which failure it was is not lost.
_TSKEY_RE = re.compile(r"tskey-[A-Za-z0-9_-]+")


def _scrub(text: str, key: str = "") -> str:
    """Redact any auth key before the text leaves this module.

    THE KEY MUST NEVER BE ECHOED (the module docstring), and a FAILED join is
    exactly when that matters most: its one-off key was never spent, so it is
    still LIVE, and Tailscale's own "Access denied" message hands it back in a
    copy-pasteable `sudo tailscale up --auth-key=tskey-...` line. Printed on a
    phone or dropped in a log, that is a working credential to anyone who sees
    it (leak found 2026-09-09: 'checkprefs access denied' put the live key on
    the screen). Redact the exact key we hold and any tskey- token."""
    if key:
        text = text.replace(key, "tskey-<redacted>")
    return _TSKEY_RE.sub("tskey-<redacted>", text)


def _permission_hint(detail: str) -> str:
    """The one setup step behind 'Access denied: checkprefs access denied':
    the hub runs the tailscale CLI as its own non-root user, and tailscaled
    only obeys root or the user named as its OPERATOR. Named with the hub's
    actual user, run once, this is the whole fix."""
    low = detail.lower()
    if "access denied" in low or "checkprefs" in low or "operator" in low:
        try:
            who = getpass.getuser()
        except Exception:                          # noqa: BLE001
            who = os.environ.get("USER") or "the-hub-user"
        # THE PHONE'S WAY OUT FIRST (2026-11-05): the hub's service gives
        # itself this permission every time it starts, so turning the hub
        # off and on is the whole fix -- no command needed.
        return (f" The hub is not allowed to run Tailscale yet. Turn the "
                f"hub off and on again (it gives itself permission when it "
                f"starts), then try again. (Or once on the Pi: sudo "
                f"tailscale set --operator={who})")
    return ""


def _operator_args() -> List[str]:
    """`--operator=<this user>` on every `up`: --reset would otherwise
    clear the operator, and every later non-root command (cert, a move)
    would be "access denied" until the hub restarted. Not on Windows."""
    if os.name == "nt":
        return []
    try:
        who = getpass.getuser()
    except Exception:                              # noqa: BLE001
        return []
    return [f"--operator={who}"] if who else []


#: When a join or sign-in started (0 = none running). ONE AT A TIME: a
#: second `tailscale up --reset` raced the first and made it fail. A time,
#: not a lock, so a sign-in whose phone vanished cannot hold it for ever.
_joining_since = 0.0


def _busy_now() -> bool:
    import time as _t
    return _joining_since > 0 and _t.monotonic() - _joining_since < \
        LOGIN_TIMEOUT + 60


def _mark_joining(on: bool) -> None:
    global _joining_since
    import time as _t
    _joining_since = _t.monotonic() if on else 0.0


_BUSY = {"ok": False, "reason": "busy",
         "text": "a Tailscale sign-in is already running on the hub -- "
                 "finish that one first, or wait a few minutes."}


#: No internet: said plainly. Not "share your phone's data": switching the
#: phone's network loses the app's link to the hub mid-setup.
_NO_INTERNET = ("the hub has no internet. Remote access needs your router "
                "to have internet -- connect it, then try again. Everything "
                "at home works without it.")


def ready() -> Optional[Dict[str, Any]]:
    """The checks that need NO PIN, asked before it: installed, running,
    not already busy, and internet for a hub not yet on a tailnet. None
    when the join can go ahead; else the refusal, in words."""
    _binary, state, refused = _preflight()
    if refused:
        return refused
    if _busy_now():
        return dict(_BUSY)
    if not _joined(state) and not has_internet():
        return {"ok": False, "reason": "no_internet",
                "text": _NO_INTERNET}
    return None


def connected_name() -> str:
    """This hub's tailnet name when it is signed in AND running, else "" --
    what tells a hub that lost Tailscale from one that has it."""
    binary = find_tailscale()
    state = status(binary) if binary else None
    return _joined(state) if state else ""


def _connect(host: str, port: int, timeout: float) -> bool:
    """Chokepoint for tests, like _run: one bounded TCP dial."""
    import socket
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def has_internet() -> bool:
    """Can this hub reach Tailscale's control plane right now?

    A REAL DIAL, not a ping and not a DNS lookup alone -- the question the
    join is about to ask is "can I open a TCP connection to the control
    plane", so that is the question asked here, once, cheaply, BEFORE any
    key is spent or any sign-in page is shown. Without this check the
    failure arrives minutes later wearing a confusing costume: a login page
    that never loads, or a join that times out looking like a Tailscale
    fault, when the truth is a hub with no route out.
    """
    return _connect("controlplane.tailscale.com", 443, 5.0)


#: THE INSTRUCTIONS DIFFER BY MACHINE, and this hub runs on two: the
#: Windows bench today, the Pi after the migration. find_tailscale()
#: already knows about both -- it looks in Program Files -- so the CODE
#: was portable while the SENTENCES were not, telling a Windows operator
#: to run `sudo systemctl start tailscaled`. On a headless box the error
#: message is the whole user interface (the reasoning in join() below), so
#: an instruction for the wrong operating system is a real defect, not a
#: cosmetic one.
def _install_hint() -> str:
    if os.name == "nt":
        return ("On Windows: install it from "
                "https://tailscale.com/download/windows")
    return ("On the Pi: https://tailscale.com/download/linux")


def _daemon_hint() -> str:
    if os.name == "nt":
        return ("On Windows: start Tailscale from the Start menu (it runs "
                "as a service -- if it is installed, opening it once is "
                "usually enough)")
    return "On the Pi: sudo systemctl start tailscaled"


def find_tailscale() -> Optional[str]:
    """The tailscale binary, on PATH or where its installer puts it."""
    binary = shutil.which("tailscale")
    if binary:
        return binary
    if os.name == "nt":
        for base in (os.environ.get("ProgramFiles", r"C:\Program Files"),
                     os.environ.get("ProgramData", r"C:\ProgramData")):
            candidate = os.path.join(base, "Tailscale", "tailscale.exe")
            if os.path.isfile(candidate):
                return candidate
    return None


def _preflight() -> "tuple[str, Dict[str, Any], Optional[Dict[str, Any]]]":
    """(binary, status, None) -- or ("", {}, the refusal) when tailscale is
    not installed or its daemon is not answering. The two refusals both
    join paths make first, in words, because with a headless box the error
    message is the whole user interface."""
    binary = find_tailscale()
    if not binary:
        return "", {}, {"ok": False, "reason": "not_installed",
                        "text": ("tailscale is not installed on the hub. "
                                 + _install_hint() + " -- then try again.")}
    state = status(binary)
    if state is None:
        return "", {}, {"ok": False, "reason": "daemon_down",
                        "text": ("the tailscale service is not running on "
                                 "the hub. " + _daemon_hint())}
    return binary, state, None


def _joined(state: Dict[str, Any]) -> str:
    """This machine's tailnet name when it is signed in and running, or ""."""
    name = current_name(state)
    return name if name and state.get("BackendState") == "Running" else ""


def _refusal(up: subprocess.CompletedProcess, key: str = "") -> str:
    """Tailscale's own words for a refused `up`, minus any auth key.

    Rewriting it friendlier would lose the one detail that says WHICH of
    expired / already-used / wrong-tailnet / access-denied it was, but its
    "use sudo tailscale up --auth-key=tskey-..." suggestion carries the
    LIVE key, so it is scrubbed first."""
    detail = _scrub((up.stderr or up.stdout or "").strip(), key)[-400:]
    return (detail or "tailscale refused the key.") + _permission_hint(detail)


def status(binary: str) -> Optional[Dict[str, Any]]:
    """`tailscale status --json`, parsed, or None when the daemon is not
    answering. JSON rather than the human output, which changes shape
    between versions and was never meant to be parsed."""
    try:
        out = _run([binary, "status", "--json"], timeout=10)
    except (OSError, subprocess.SubprocessError):
        return None
    if out.returncode != 0:
        return None
    try:
        data = json.loads(out.stdout)
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


def current_name(state: Optional[Dict[str, Any]]) -> str:
    """This machine's MagicDNS name from a status dict, or ""."""
    if not state:
        return ""
    name = ((state.get("Self") or {}).get("DNSName") or "").strip()
    return name.rstrip(".")           # DNS names properly end with a dot


def name_now() -> str:
    """This machine's MagicDNS name as Tailscale reports it RIGHT NOW, or ""
    (no Tailscale, not connected yet, daemon not answering).
    HOMEVOICE_WS_HOSTNAME overrides it."""
    override = os.environ.get("HOMEVOICE_WS_HOSTNAME", "").strip()
    if override:
        return override
    binary = find_tailscale()
    return current_name(status(binary)) if binary else ""


def name_if_late(listening: bool) -> str:
    """TAILSCALE THAT CAME UP AFTER THE HUB DID. The hub's bindings are built
    once, at start, from the name Tailscale had then; a hub started before
    Tailscale had connected (a laptop that got its internet a minute later,
    a reboot where the hub won the race) had no tailnet entry at all, so
    nothing listened there and the phone's tailnet addresses timed out
    until somebody restarted the hub (2026-10-03: only 'lan' was
    listening). The server's binding check calls this every round:
    `listening` is whether a tailnet listener is already up. While it is
    not, Tailscale is asked again, and the name it gives (if any) is what
    the server adopts and binds. Once it is listening, nothing is asked.
    Runs `tailscale status`, so it is called off the event loop."""
    return "" if listening else name_now()


def peer_path(state: Optional[Dict[str, Any]], ip: str) -> str:
    """HOW this machine reaches the tailnet peer at `ip`, from a status dict:
    "direct" (the two talk to each other), "relay par" (through Tailscale's
    DERP relay, here Paris -- it works, slowly), or "" (no such peer, or
    Tailscale is not saying). It is what tells "Tailscale is up but the road
    is poor" from "Tailscale is down" (2026-10-03: a phone that could only
    be reached by relay, 0.9-1.7 s a round trip)."""
    for peer in ((state or {}).get("Peer") or {}).values():
        if not isinstance(peer, dict) or ip not in (peer.get("TailscaleIPs") or []):
            continue
        if str(peer.get("CurAddr") or "").strip():
            return "direct"
        relay = str(peer.get("Relay") or "").strip()
        return f"relay {relay}" if relay else "relay"
    return ""


def path_to(ip: str) -> str:
    """peer_path for a live address, asked of Tailscale now; "" for an
    address that is not on the tailnet at all (a local one). Runs
    `tailscale status`, so it is called off the event loop."""
    try:
        if ipaddress.ip_address(str(ip).strip()) not in TAILNET_V4:
            return ""
    except ValueError:
        return ""
    binary = find_tailscale()
    return peer_path(status(binary), str(ip).strip()) if binary else ""


def current_ip(state: Optional[Dict[str, Any]]) -> str:
    """This machine's Tailscale IPv4 address (100.64.0.0/10) from a status
    dict, or "".

    THE ADDRESS TRAVELS WITH THE NAME from here on. Every success this
    module reports carried only the .ts.net name, so the name was all a
    phone ever learned -- and the name is the road with the most ways to
    fail on a phone (its resolver for MagicDNS, its root store for the
    Let's Encrypt chain, and that chain being the one on the listener).
    The IP is what the app PINS against local.crt, which the tailnet
    listener serves, so it needs nothing from the phone. On the first real
    phone (2026-09-09) the name failed every one of those and the IP
    worked, and the person typed the IP in by hand. Nobody should have to.
    """
    if not state:
        return ""
    for value in (state.get("Self") or {}).get("TailscaleIPs") or []:
        try:
            address = ipaddress.ip_address(str(value).strip())
        except ValueError:
            continue
        if address.version == 4 and address in TAILNET_V4:
            return str(address)
    return ""


def account_of(state: Optional[Dict[str, Any]]) -> str:
    """The Tailscale account this machine is signed in with
    ("you@gmail.com"), or "" when signed out or unknown."""
    if not state:
        return ""
    uid = (state.get("Self") or {}).get("UserID")
    users = state.get("User") or {}
    user = users.get(str(uid)) or users.get(uid) or {}
    if not isinstance(user, dict):
        return ""
    return str(user.get("LoginName") or user.get("DisplayName") or "").strip()


def _current_profile(binary: str) -> str:
    """The ID of the Tailscale profile (account) in use, from `tailscale
    switch --list` (the current one is starred), or "" when unknown."""
    try:
        out = _run([binary, "switch", "--list"], timeout=10)
    except (OSError, subprocess.SubprocessError):
        return ""
    if out.returncode != 0:
        return ""
    for line in (out.stdout or "").splitlines():
        words = line.split()
        if words and "*" in line and words[0].lower() != "id":
            return words[0].strip("*")
    return ""


def saved_login(binary: str) -> Optional[str]:
    """The account of the login saved on this machine (the starred line of
    `tailscale switch --list`), with no internet needed: "" when a login is
    saved but no account is shown, None when none is saved (or it cannot be
    told)."""
    try:
        out = _run([binary, "switch", "--list"], timeout=10)
    except (OSError, subprocess.SubprocessError):
        return None
    if out.returncode != 0:
        return None
    for line in (out.stdout or "").splitlines():
        words = line.split()
        if not words or words[0].lower() == "id" or "*" not in line:
            continue
        return next((w.strip("*") for w in reversed(words) if "@" in w), "")
    return None


def access_state() -> Dict[str, str]:
    """Remote access as it really is now: {"state", "account", "name", "ip"}.

    state: "on" (works) | "paused" (signed in, but cannot connect -- the
    hub has no internet) | "off" (stopped on the hub) | "expired" (its
    sign-in ran out) | "needs_approval" (waiting in the admin page) |
    "not_set_up" (never signed in) | "absent" (Tailscale not installed) |
    "daemon_down" (its service is not running)."""
    binary = find_tailscale()
    if not binary:
        return {"state": "absent"}
    st = status(binary)
    if st is None:
        return {"state": "daemon_down"}
    backend = str(st.get("BackendState") or "")
    account = account_of(st)
    out = {"account": account, "name": current_name(st), "ip": current_ip(st)}
    if backend == "Running" and out["name"]:
        return {"state": "on", **out}
    if backend == "Stopped":
        return {"state": "off", **out}
    if backend == "NeedsMachineAuth":
        return {"state": "needs_approval", **out}
    if not account and backend in ("NeedsLogin", "NoState"):
        # No internet since the hub started: Tailscale has no account to
        # show and sits in NoState. Signed in or never? The login saved on
        # this machine says, with no internet.
        saved = saved_login(binary)
        if saved is None:
            return {"state": "not_set_up", **out}
        out["account"] = saved
        return {"state": "expired" if backend == "NeedsLogin" else "paused",
                **out}
    if backend == "NeedsLogin":
        return {"state": "expired", **out}
    return {"state": "paused", **out}


#: What each state says, as the hub's one Remote access notice. "on" says
#: nothing. Its action opens Remote access where the person is (the app's
#: sheet), not the Settings screen.
_ACCESS_WORDS = {
    "absent": ("Remote access is not set up",
               "Tailscale is not installed on the hub. Install it on the Pi "
               "(curl -fsSL https://tailscale.com/install.sh | sh), then "
               "Details > Sign in."),
    "daemon_down": ("Remote access is not running",
                    "The Tailscale service on the hub is not running. "
                    "Turning the hub off and on usually fixes it (or on the "
                    "Pi: sudo systemctl start tailscaled)."),
    "not_set_up": ("Remote access is not set up",
                   "Away from home, this phone cannot reach the hub yet. "
                   "1. Install the Tailscale app on this phone and sign in. "
                   "2. Details > Sign in, with the same account. The hub "
                   "needs internet for this one step."),
    "paused": ("Remote access is paused",
               "The hub has no internet, so Tailscale cannot connect. At "
               "home everything still works; away-from-home access comes "
               "back by itself once the hub has internet."),
    "off": ("Remote access is switched off",
            "Tailscale is stopped on the hub. To turn it back on, run on "
            "the Pi: sudo tailscale up --operator={user}"),
    "expired": ("Remote access stopped working",
                "The hub's Tailscale sign-in has run out. Details > Sign in "
                "fixes it. Tip: in the Tailscale admin page, turn off key "
                "expiry for the hub so this does not happen again."),
    "needs_approval": ("Remote access is waiting for approval",
                       "The hub signed in, but your Tailscale network wants "
                       "each device approved: approve the hub in the "
                       "Tailscale admin page."),
}


def remote_access_notice(access: Dict[str, str]) -> Optional[Dict[str, Any]]:
    """The hub's one Remote access notice for `access` (access_state), or
    None while it works. Shaped like every other notice."""
    words = _ACCESS_WORDS.get(str(access.get("state") or ""))
    if words is None:
        return None
    try:
        user = getpass.getuser()
    except Exception:                              # noqa: BLE001
        user = "the-hub-user"
    return {"id": "tailnet:not_joined", "title": words[0],
            "body": words[1].replace("{user}", user),
            "action_kind": "route", "action_value": "/remote_access",
            "action_label": "Details", "now": False}


def tailnet_id(state: Optional[Dict[str, Any]]) -> str:
    """WHICH tailnet this is, as one comparable string.

    This is the whole basis of "same network or another one?": the hub
    cannot read a tailnet out of an auth key, but it CAN read the tailnet
    it is standing on, before and after. If the string is the same, it did
    not move.

    MagicDNSSuffix ("tail1234.ts.net") is the tailnet's own identity and
    sits at the top level of `status --json`; newer versions also carry it
    under CurrentTailnet. The last fallback derives it from the machine's
    own name -- "hub.tail1234.ts.net" without its first label is that same
    string -- so this keeps answering on a status shape that has neither
    field, which matters because the comparison is a SAFETY check and a
    silent "" == "" would read as "nothing moved".
    """
    if not state:
        return ""
    current = state.get("CurrentTailnet") or {}
    for value in (state.get("MagicDNSSuffix"),
                  current.get("MagicDNSSuffix"),
                  current.get("Name")):
        if isinstance(value, str) and value.strip():
            return value.strip().strip(".").lower()
    name = current_name(state)
    return name.split(".", 1)[1].lower() if "." in name else ""


def join(auth_key: str, hostname: str = "",
         allow_switch: bool = False) -> Dict[str, Any]:
    """One join at a time (see _joining_since); the work is _join."""
    if _busy_now():
        return dict(_BUSY)
    _mark_joining(True)
    try:
        return _join(auth_key, hostname, allow_switch)
    finally:
        _mark_joining(False)


def _join(auth_key: str, hostname: str = "",
          allow_switch: bool = False) -> Dict[str, Any]:
    """The whole exchange, as one call returning what the phone is told.

    Returns a dict shaped for the wire:
      ok:            joined, stayed, or moved
      reason/text:   when not ok, which check refused and what to do
      tailnet_name:  the machine's .ts.net name when ok
      cert_ok:       whether `tailscale cert` produced a certificate --
                     a SEPARATE outcome, because HTTPS has to be enabled
                     for the tailnet in the admin console and "joined but
                     no certificate yet" is a real state, not a failure.

    `allow_switch` IS THE CONSENT, carried in as an argument rather than
    decided here, because this module cannot ask anyone anything. Without
    it, a hub that is already on a tailnet does NOTHING and returns
    reason "confirm_switch" -- the question, for the caller to put behind
    a PIN. With it, the key is spent and the answer comes back as
    "same_tailnet" (it stayed) or "switched" (it moved).

    REFUSALS COME FIRST, each with its own sentence, because with a
    headless box the error message is the whole user interface:
    """
    # 1. No binary: nothing else is diagnosable until it is installed.
    # 3. Daemon down: the CLI exists but nobody is home.
    # (Both asked by _preflight, the same two refusals the sign-in path
    # makes -- so a daemon that is down is said before a mistyped key.)
    binary, state, refused = _preflight()
    if refused:
        return refused

    # 2. Key shape: a mistyped paste should be one clear sentence, not
    #    whatever error tailscale produces for garbage input.
    key = (auth_key or "").strip()
    if not key.startswith("tskey-"):
        return {"ok": False, "reason": "bad_key",
                "text": "that does not look like a Tailscale auth key -- "
                        "they start with 'tskey-'. Generate a ONE-OFF key "
                        "in the admin console under Settings > Keys."}

    # 4. Already joined: ASK FIRST, then try the key and see where it lands.
    #
    #    THE KEY CANNOT BE INSPECTED. An auth key is an opaque string;
    #    nothing in it names its tailnet in any form the CLI will reveal
    #    before the key is spent. So a key for a DIFFERENT tailnet is
    #    indistinguishable from a key for this one, and the only way to
    #    find out is to spend it.
    #
    #    This used to be a flat refusal, which was honest but useless: the
    #    person with a second tailnet's key had to go and run `tailscale
    #    logout` on a headless Pi by hand. Her ruling (2026-08-27): "it
    #    joins the key and checks if the network is the same, if so then it
    #    stays, if not it joins another network -- guard that with pin."
    #
    #    So the shape is CONSENT, then act. With allow_switch False the hub
    #    does nothing at all and hands back the question, because the answer
    #    is consequential: moving tailnets drops this name and its
    #    certificate, and every phone's saved address stops resolving. The
    #    caller (ws_server) asks that question behind a PIN. With the answer
    #    in hand, _switch spends the key and reads the tailnet back -- same
    #    suffix means it stayed, a different one means it moved.
    existing = _joined(state)
    if existing:
        if not allow_switch:
            return {"ok": False, "reason": "confirm_switch",
                    "tailnet_name": existing,
                    "tailnet": tailnet_id(state),
                    "cert_ok": _have_cert(existing),
                    "text": f"this hub is already on a tailnet as "
                            f"{existing}, and nothing has been changed yet. "
                            f"A key does not say which network it belongs "
                            f"to until it is spent, so the only way to know "
                            f"is to use it: if it belongs to THIS tailnet "
                            f"the hub stays exactly where it is, and if it "
                            f"belongs to another one the hub moves there -- "
                            f"losing the name {existing}, its certificate, "
                            f"and every phone's saved address until they "
                            f"are told the new one."}

    # 5. No internet: say so NOW, in words that name the fix, before the
    #    key is spent on a join that cannot reach the control plane. The
    #    hub's hotspot shares the hub machine's own connection -- so if
    #    this fires, the machine itself is offline, and no amount of
    #    retrying from the phone changes that.
    #
    #    BEFORE the switch too, and that ordering is deliberate: a
    #    re-authentication that cannot reach the control plane is the one
    #    way to end up neither here nor there.
    if not has_internet():
        return {"ok": False, "reason": "no_internet",
                "text": _NO_INTERNET}

    if existing:
        return _switch(binary, key, hostname, state)

    # The join itself. --reset clears any half-configured prior state so
    # the outcome depends on this key, not on history.
    argv = [binary, "up", f"--auth-key={key}", "--reset", *_operator_args()]
    if hostname.strip():
        argv.append(f"--hostname={hostname.strip()}")
    try:
        up = _run(argv, timeout=UP_TIMEOUT)
    except subprocess.TimeoutExpired:
        return {"ok": False, "reason": "timeout",
                "text": f"the join was still running after {UP_TIMEOUT}s -- "
                        f"it may yet succeed. Look at Settings > Remote "
                        f"access again in a minute."}
    except (OSError, subprocess.SubprocessError) as exc:
        return {"ok": False, "reason": "failed",
                "text": f"could not run tailscale: {exc}"}
    if up.returncode != 0:
        # Tailscale's own message, minus any auth key -- see _refusal.
        return {"ok": False, "reason": "refused", "text": _refusal(up, key)}

    return _after_up(binary)


def _switch(binary: str, key: str, hostname: str,
            before: Dict[str, Any]) -> Dict[str, Any]:
    """Spend the key on a hub that is ALREADY on a tailnet, and report
    whether it stayed or moved. Only reached with the person's consent.

    --force-reauth IS THE WHOLE MECHANISM, and the reason is the one this
    module keeps repeating: the key is opaque. A plain `tailscale up
    --auth-key` on a node that is already signed in is a no-op -- it does
    not re-register, so it can neither confirm nor deny that the key
    belongs here, and reporting "same tailnet" off the back of it would be
    a guess wearing a fact's clothes. --force-reauth actually presents the
    key. Same tailnet: the node re-authenticates and keeps its identity,
    its name and its certificate. Different tailnet: it lands there, which
    is what was agreed.

    NOT --reset. The fresh-join path uses it to clear half-configured
    state; here the hub has working state (its hostname, its routes) and
    the only thing being changed is which tailnet it answers to.

    The before/after comparison is on the TAILNET, not the machine name: a
    node keeps its hostname across a move, so "hub" would look identical on
    both networks while "tail1234.ts.net" would not.
    """
    before_id = tailnet_id(before)
    was = current_name(before)
    argv = [binary, "up", f"--auth-key={key}", "--force-reauth",
            *_operator_args()]
    # TAILSCALE REFUSES AN `up` THAT DOES NOT REPEAT EVERY NON-DEFAULT FLAG:
    # "changing settings via 'tailscale up' requires mentioning all
    # non-default flags. To proceed, either re-run your command with --reset
    # or ... explicitly mention the current value". The hub sets --hostname
    # on the fresh join, so on a re-auth it MUST be repeated or the whole
    # re-auth is refused -- which is exactly what happened (2026-09-09): the
    # app passed no hostname, the node held one, and every re-auth bounced
    # with that error while "nothing moved". --reset would satisfy tailscale
    # but is refused above for a reason; repeating the name the node ALREADY
    # has (Self.HostName in status) satisfies it AND preserves the name,
    # which is the very thing that "NOT --reset" reasoning protects.
    want = (hostname.strip()
            or ((before.get("Self") or {}).get("HostName") or "").strip())
    if want:
        argv.append(f"--hostname={want}")
    try:
        up = _run(argv, timeout=UP_TIMEOUT)
    except subprocess.TimeoutExpired:
        return {"ok": False, "reason": "timeout",
                "text": f"the hub was still re-authenticating after "
                        f"{UP_TIMEOUT}s, so which tailnet it is on now is "
                        f"genuinely unknown. Look at Settings > Remote access "
                        f"again in a minute: it shows where the hub is."}
    except (OSError, subprocess.SubprocessError) as exc:
        return {"ok": False, "reason": "failed",
                "text": f"could not run tailscale: {exc}"}

    after = status(binary)
    now = _joined(after or {})
    running = bool(now)

    if up.returncode != 0:
        # Tailscale's own message, verbatim -- it is the one thing that
        # says WHICH of expired / already-used / revoked this was.
        #
        # AND THEN THE STATE, because a refused re-authentication is not
        # always harmless: presenting a bad key can leave the node signed
        # out of the tailnet it was perfectly happy on. Saying only "the
        # key was refused" would send someone looking for a key problem
        # while the hub sat unreachable.
        text = _refusal(up, key)
        if running:
            text += (f" The hub is still on its own tailnet as {now} -- "
                     f"nothing moved.")
        else:
            text += (f" The hub is now signed OUT (it was {was}): the "
                     f"refused key knocked it off. Join it again with a "
                     f"good key, or sign in.")
        return {"ok": False, "reason": "refused", "text": text}

    if not running:
        return {"ok": False, "reason": "no_name",
                "text": f"the key was accepted but the hub has no tailnet "
                        f"name yet (it was {was}). Check `tailscale status` "
                        f"on the hub."}

    if tailnet_id(after) == before_id:
        # STAYED. A certificate on disk -- IF THERE IS ONE -- is still for
        # this name, so it is not reissued. But "nothing was dropped" was
        # read as "so a certificate exists", and that is false for a hub
        # that joined out-of-band (the CLI, 2026-09-09) and never had one
        # issued: this path reported cert_ok=False, ws_server never opened
        # the tailnet listener, and the phone was told "the same
        # certificate" about a certificate that did not exist -- while
        # dialling a name nobody was answering on. A re-auth on the same
        # tailnet is exactly the app's chance to get the missing one.
        cert_ok = _have_cert(now) or _issue_cert(binary, now)
        return {"ok": True, "reason": "same_tailnet", "tailnet_name": now,
                "tailnet_ip": current_ip(after),
                "cert_ok": cert_ok,
                "text": (f"that key belongs to the tailnet this hub is "
                         f"already on, so nothing moved -- it is still {now}"
                         + ", with the same address."
                         + " The key is spent.")}

    # MOVED. New tailnet, new name, and the old certificate is for a name
    # that no longer resolves -- so _after_up issues a fresh one.
    result = _after_up(binary)
    if result.get("ok"):
        result["reason"] = "switched"
        result["previous_name"] = was
        # A move hands the node a NEW address as well as a new name, so the
        # phone needs to drop the old one too -- it is told what it was.
        result["previous_ip"] = current_ip(before)
        tail = ""
        result["text"] = (
            f"that key was for a DIFFERENT tailnet, so the hub moved: it is "
            f"{result.get('tailnet_name', '?')} now, and it was {was}. Every "
            f"phone has to be told the new address -- this one has been." + tail)
    return result


def _cert_paths() -> "tuple[str, str]":
    from core import config
    return config.TLS_CERT, config.TLS_KEY


def _have_cert(name: str) -> bool:
    """A certificate on disk that is FOR THIS NAME and still valid -- not
    merely a file.

    The old check was os.path.isfile. A hub whose folder was copied over
    from another machine (the Windows bench -> the Pi, 2026-09-09) arrives
    with THAT machine's certificate: a perfectly valid Let's Encrypt chain
    for desktop-2jkh971.tail0d1738.ts.net, served on
    homeserver.tail0d1738.ts.net. Every phone rejected it as a name
    mismatch, the app showed "offline" over Tailscale, and isfile said all
    was well so nothing was ever reissued. The NAME is what the phone
    checks, so it is what this checks -- and expiry with it, because a
    Let's Encrypt certificate is 90 days and a stale one fails the same
    way. Anything unreadable is treated as absent, so a fresh one is
    issued rather than served.
    """
    paths = _cert_paths()
    if not os.path.isfile(paths[0]):
        return False
    want = name.strip().rstrip(".").lower()
    if not want:
        return False
    try:
        import datetime
        from cryptography import x509
        with open(paths[0], "rb") as f:
            pem = f.read()
        cert = x509.load_pem_x509_certificate(pem)   # the LEAF: first in the chain
        names = set()
        try:
            san = cert.extensions.get_extension_for_class(
                x509.SubjectAlternativeName)
            names.update(n.lower().rstrip(".")
                         for n in san.value.get_values_for_type(x509.DNSName))
        except x509.ExtensionNotFound:
            pass
        for attr in cert.subject.get_attributes_for_oid(
                x509.NameOID.COMMON_NAME):
            names.add(str(attr.value).lower().rstrip("."))
        if want not in names:
            return False
        expires = getattr(cert, "not_valid_after_utc", None)
        if expires is None:                              # cryptography < 42
            expires = cert.not_valid_after.replace(tzinfo=datetime.timezone.utc)
        return expires > datetime.datetime.now(datetime.timezone.utc)
    except Exception:                                    # noqa: BLE001
        return False


def _issue_cert(binary: str, name: str) -> bool:
    """`tailscale cert`, writing where NETWORK_BINDINGS already points.

    The tailnet binding in config serves TLS_CERT/TLS_KEY; issuing straight
    to those paths means joining the tailnet and serving on it need no
    further wiring at all.
    """
    cert_file, key_file = _cert_paths()
    try:
        os.makedirs(os.path.dirname(cert_file) or ".", exist_ok=True)
        out = _run([binary, "cert",
                    f"--cert-file={cert_file}",
                    f"--key-file={key_file}",
                    name], timeout=CERT_TIMEOUT)
    except (OSError, subprocess.SubprocessError):
        return False
    return out.returncode == 0


def _after_up(binary: str) -> Dict[str, Any]:
    """What both join paths share once `up` has returned: read the name,
    then try for the certificate -- which is allowed to fail ON ITS OWN,
    because it needs HTTPS enabled for the tailnet in the admin console,
    and "joined, no cert yet" is a real state rather than a failure."""
    state = status(binary)
    name = current_name(state)
    if not name:
        return {"ok": False, "reason": "no_name",
                "text": "joined, but the tailnet has not assigned a name "
                        "yet. Look at Settings > Remote access again in a minute."}
    cert_ok = _issue_cert(binary, name)
    account = account_of(state)
    return {"ok": True, "tailnet_name": name, "tailnet_ip": current_ip(state),
            "cert_ok": cert_ok, "account": account,
            "tailnet": tailnet_id(state),
            "text": (f"joined as {name}"
                     + (f", signed in as {account}." if account else "."))}


# ── The interactive path: sign in on the phone, no key at all ────────────
#
# `tailscale up` with no key prints a login URL and BLOCKS until someone
# authenticates at it. The hub captures that URL, the phone shows it in the
# same web screen Home Assistant's sign-ins already use, and the moment the
# person finishes, the process exits and the join completes. Two halves,
# because the caller has to send the URL to the phone BETWEEN them.

#: How long tailscale gets to print its login URL. It appears in the first
#: moments or not at all.
URL_TIMEOUT = 30
#: How long a human gets to finish signing in on the phone.
LOGIN_TIMEOUT = 900          # a first Tailscale account takes a while

_LOGIN_URL = re.compile(r"https://login\.tailscale\.com/\S+")


def _reap(proc) -> None:
    """wait() after kill(), bounded, so a killed tailscale is not left a
    zombie for the life of the hub."""
    try:
        proc.wait(timeout=5)
    except Exception:                                  # noqa: BLE001
        pass


def _popen(argv: List[str]) -> subprocess.Popen:
    """Chokepoint for tests, like _run."""
    return subprocess.Popen(argv, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, text=True)


def begin_interactive_join(hostname: str = "") -> Dict[str, Any]:
    """Start a keyless join and capture the login URL.

    Returns {"ok": True, "url": ..., "proc": <handle>} -- the caller shows
    the URL, then calls finish_interactive_join(proc). Or an error dict in
    exactly the shape join() produces, so the wire handling is one path.
    """
    if _busy_now():
        return dict(_BUSY)
    _mark_joining(True)
    begun = _begin_interactive(hostname)
    if "proc" not in begun:
        _mark_joining(False)        # nothing is waiting: no finish will come
    return begun


def _begin_interactive(hostname: str = "") -> Dict[str, Any]:
    binary, state, refused = _preflight()
    if refused:
        return refused
    # ALREADY SIGNED IN: Sign in is another account. It is a new Tailscale
    # profile (`tailscale login`), so the account in use stays intact until
    # the new one succeeds; halfway, the hub switches back to it.
    existing = _joined(state)
    previous: Optional[Dict[str, Any]] = None
    if existing:
        previous = {"name": existing, "ip": current_ip(state),
                    "account": account_of(state),
                    "tailnet": tailnet_id(state),
                    "profile": _current_profile(binary)}
        if not previous["profile"]:
            return {"ok": False, "reason": "failed",
                    "text": "this hub's Tailscale is too old to keep its "
                            "account while another one signs in. Update "
                            "it on the hub (curl -fsSL "
                            "https://tailscale.com/install.sh | sh), then "
                            "try again. Nothing was changed."}

    if not has_internet():
        return {"ok": False, "reason": "no_internet",
                "text": _NO_INTERNET}

    argv = ([binary, "login", *_operator_args()] if previous is not None
            else [binary, "up", "--reset", *_operator_args()])
    if hostname.strip():
        argv.append(f"--hostname={hostname.strip()}")
    try:
        proc = _popen(argv)
    except (OSError, subprocess.SubprocessError) as exc:
        return {"ok": False, "reason": "failed",
                "text": f"could not run tailscale: {exc}"}

    # Read the URL off the process through a helper thread, so a tailscale
    # that prints nothing cannot hang the hub -- readline() has no timeout
    # of its own, on any platform.
    import queue as _queue
    import threading as _threading
    import time as _time
    lines: "_queue.Queue[Optional[str]]" = _queue.Queue()

    def _pump():
        for line in proc.stdout or []:
            lines.put(line)
        lines.put(None)

    _threading.Thread(target=_pump, daemon=True).start()
    seen: List[str] = []
    deadline = _time.monotonic() + URL_TIMEOUT
    while _time.monotonic() < deadline:
        try:
            line = lines.get(timeout=0.5)
        except _queue.Empty:
            continue
        if line is None:                      # process ended before any URL
            # Its output closed; it is exiting -- but never wait unbounded
            # on a process this thread cannot otherwise stop.
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                _reap(proc)
            detail = "".join(seen).strip()[-400:]
            # UP WITH NO SIGN-IN NEEDED: a hub still logged in but stopped
            # comes straight back up, and that is a success, not a refusal.
            if proc.returncode == 0:
                after = status(binary)
                if _joined(after or {}):
                    return _after_up(binary)
            _restore(binary, previous)
            return {"ok": False, "reason": "refused",
                    "text": detail or "tailscale exited before offering a "
                                      "sign-in address."}
        seen.append(line)
        found = _LOGIN_URL.search(line)
        if found:
            # The account to come back to, if this sign-in is dropped.
            proc.hv_previous = previous
            return {"ok": True, "url": found.group(0), "proc": proc,
                    "previous": previous}
    proc.kill()
    _reap(proc)
    _restore(binary, previous)
    return {"ok": False, "reason": "timeout",
            "text": f"tailscale did not offer a sign-in address within "
                    f"{URL_TIMEOUT}s."}


def finish_interactive_join(proc) -> Dict[str, Any]:
    """Wait for the person to finish signing in, then the shared tail."""
    try:
        return _finish_interactive(proc)
    finally:
        _mark_joining(False)


def cancel_interactive(proc) -> None:
    """Drop a sign-in halfway (Cancel, or the phone went away): the waiting
    finish_interactive_join then puts the old account back."""
    proc.hv_cancelled = True
    try:
        proc.kill()
    except Exception:                                  # noqa: BLE001
        pass


def _restore(binary: str, previous: Optional[Dict[str, Any]]) -> None:
    """Back to the account in use before a dropped sign-in, if there was
    one (a first sign-in leaves nothing to restore)."""
    profile = (previous or {}).get("profile")
    if profile:
        try:
            _run([binary, "switch", str(profile)], timeout=30)
        except (OSError, subprocess.SubprocessError):
            pass


def _finish_interactive(proc) -> Dict[str, Any]:
    binary = find_tailscale() or "tailscale"
    previous = getattr(proc, "hv_previous", None)
    kept = (f" Nothing changed: the hub is still signed in as "
            f"{previous.get('account') or previous.get('name')}."
            if previous else " Nothing changed.")
    try:
        code = proc.wait(timeout=LOGIN_TIMEOUT)
    except subprocess.TimeoutExpired:
        proc.kill()
        _reap(proc)
        _restore(binary, previous)
        return {"ok": False, "reason": "timeout",
                "text": f"nobody finished signing in within "
                        f"{LOGIN_TIMEOUT // 60} minutes -- start it again "
                        f"when you are ready." + kept}
    if code != 0:
        _restore(binary, previous)
        if getattr(proc, "hv_cancelled", False):
            return {"ok": False, "reason": "cancelled",
                    "text": "the sign-in was stopped." + kept}
        return {"ok": False, "reason": "refused",
                "text": "the sign-in did not finish. Start again from "
                        "Settings > Remote access." + kept}
    result = _after_up(binary)
    if previous and result.get("ok"):
        moved = (result.get("tailnet") or "") != (previous.get("tailnet") or "")
        result.update(
            reason="switched" if moved else "same_tailnet",
            previous_name=previous.get("name", ""),
            previous_ip=previous.get("ip", ""),
            previous_account=previous.get("account", ""))
        if not moved:
            result["text"] = (f"signed in as {result.get('account') or 'the new account'}"
                              f" -- the same tailnet, so every address "
                              f"stays as it was.")
    elif previous and not result.get("ok"):
        _restore(binary, previous)
    return result


def logout() -> Dict[str, Any]:
    """Sign the hub out of Tailscale; its account is removed from the hub.
    Phones away from home can no longer reach it until someone signs in."""
    binary, state, refused = _preflight()
    if refused:
        return refused
    if _busy_now():
        return dict(_BUSY)
    name = _joined(state)
    if not name:
        return {"ok": False, "reason": "not_joined",
                "text": "the hub is not signed in to Tailscale."}
    account = account_of(state)
    try:
        out = _run([binary, "logout"], timeout=UP_TIMEOUT)
    except (OSError, subprocess.SubprocessError) as exc:
        return {"ok": False, "reason": "failed",
                "text": f"could not run tailscale: {exc}"}
    if out.returncode != 0:
        detail = (out.stderr or out.stdout or "").strip()[-300:]
        return {"ok": False, "reason": "failed",
                "text": (detail or "tailscale refused to log out.")
                + _permission_hint(detail)}
    return {"ok": True, "reason": "logged_out", "previous_name": name,
            "previous_ip": current_ip(state), "account": account,
            "text": "the hub is signed out of Tailscale"
                    + (f" ({account} removed)" if account else "")
                    + ". Away from home, phones can no longer reach it "
                      "until someone signs in again."}
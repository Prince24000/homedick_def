"""hub/hub_net.py -- the hub on somebody else's network.

The hub was designed to RUN the network: a Pi in hotspot mode, phones and
boards joining it, its address fixed by our own script (192.168.4.1). This
module is what changes when the hub is instead a CLIENT of a house router
-- an Airtel box, a landlord's Wi-Fi, a phone's hotspot on demo day -- and
holds whatever address that router hands out.

TWO JOBS, both about that address:

  lan_address()   Which of this machine's addresses is the one a phone on
                  the router's Wi-Fi can reach? Asked at bind time by
                  ws_server (the `lan` entry in config.NETWORK_BINDINGS)
                  and by `--pair` when it draws the code, and asked again
                  every twenty seconds by the bind supervisor -- so a new
                  router, or a new address after a reboot, needs no edit
                  and no restart. PRIVATE RANGES ONLY. The hub never binds
                  a public address, never the tailnet's 100.64/10 (that
                  binding has its own entry and its own certificate), and
                  never an access point it runs itself (those have theirs).
                  HOMEVOICE_LAN_HOST overrides the guess on a machine with
                  several networks, exactly as HOMEVOICE_MQTT_HOST does for
                  the boards.

  join_wifi()     Move THIS MACHINE onto a different Wi-Fi, through
                  NetworkManager's nmcli -- the tool Raspberry Pi OS uses
                  since Bookworm. Called by Settings > Change ESP Wi-Fi
                  after the boards have been moved, when the person says
                  the hub should follow. Keeps the old connection profile
                  and falls back to it if the new one will not come up, so
                  a mistyped password costs a minute, not a keyboard; once
                  the new one IS up, makes it the only profile that
                  autoconnects (retrying forever), so neither the old road
                  nor the hub's own hotspot profile can take the radio back
                  uninvited. Needs
                  no root: Raspberry Pi OS puts its user in `netdev`, which
                  NetworkManager trusts to change connections.

WHAT THIS DOES NOT CHANGE. Every connection on the `lan` binding is still
TLS to the pinned certificate and still has to sign the per-connection
nonce with a paired key (or the household key, and then only for pairing,
and only while the pairing window is open). Reachable on a shared network
is not open on it; ws_server additionally throttles a peer that keeps
failing authentication (config.AUTH_FAIL_*), which is the one new risk a
router's Wi-Fi adds over an access point the hub runs itself: other people
are on it.
"""
from __future__ import annotations

import ipaddress
import os
import re
import shutil
import socket
import subprocess
import time
from typing import Callable, List, Optional, Tuple

#: The word in config.NETWORK_BINDINGS that means "work it out".
LAN_SENTINEL = "lan"

_PRIVATE = (ipaddress.ip_network("10.0.0.0/8"),
            ipaddress.ip_network("172.16.0.0/12"),
            ipaddress.ip_network("192.168.0.0/16"))
_NEVER = (ipaddress.ip_network("127.0.0.0/8"),      # this machine talking to itself
          ipaddress.ip_network("100.64.0.0/10"),    # the tailnet: its own binding, its own cert
          ipaddress.ip_network("169.254.0.0/16"))   # link-local: no router gave this out
#: The usual addresses of a hotspot THIS machine runs (NetworkManager, the
#: classic Pi script, Windows Mobile Hotspot). NOT used to decide where the
#: hub listens -- it listens on every private address it holds, whatever the
#: number (local_addresses). Only the board helpers read this, to word a hint
#: ("turn your hotspot back on") and to suggest an address for the boards.
AP_ADDRESSES = ("10.42.0.1", "192.168.4.1", "192.168.137.1")


def is_lan_address(address: str) -> bool:
    """A private address somebody else's router could have handed out."""
    try:
        ip = ipaddress.ip_address(address)
    except ValueError:
        return False
    if ip.version != 4:
        return False
    if any(ip in net for net in _NEVER):
        return False
    return any(ip in net for net in _PRIVATE)


def route_address() -> str:
    """The address the OS would send from to reach the wider network. A
    connected UDP socket sends nothing; it only makes the kernel choose --
    so it works on a hotspot with no internet too. "" when there is no
    route at all."""
    try:
        probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            probe.connect(("8.8.8.8", 80))
            return probe.getsockname()[0]
        finally:
            probe.close()
    except OSError:
        return ""


def holds_address(address: str) -> bool:
    """Does this machine hold `address` right now? Asked by BINDING to it:
    an OS only lets you bind what it holds, so this is an exact yes/no with
    no interface enumeration and no dependency."""
    try:
        probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            probe.bind((address, 0))
            return True
        finally:
            probe.close()
    except OSError:
        return False


def _all_addresses() -> List[str]:
    """Every IPv4 address this machine holds. Read from the adapter list
    (ipconfig / ip addr) -- a hostname lookup on a Pi answers only
    127.0.1.1, and would miss a hotspot's address entirely -- with the
    hostname lookup as the fallback when neither tool can be run."""
    out = [addr for _name, addr in interface_addresses()]
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            addr = info[4][0]
            if addr not in out:
                out.append(addr)
    except socket.gaierror:
        pass
    return out


def parse_ipconfig_all(text: str) -> List[Tuple[str, str]]:
    """(adapter, IPv4) for every adapter in `ipconfig` output."""
    out: List[Tuple[str, str]] = []
    adapter = ""
    for line in text.splitlines():
        if line and not line[0].isspace() and line.rstrip().endswith(":"):
            adapter = line.strip().rstrip(":")
            continue
        if adapter and "IPv4" in line:
            m = re.search(r"(\d{1,3}(?:\.\d{1,3}){3})", line.split(":", 1)[-1])
            if m:
                out.append((adapter, m.group(1)))
    return out


def parse_ip_addr_all(text: str) -> List[Tuple[str, str]]:
    """(interface, IPv4) from Linux `ip -o -4 addr show`."""
    out: List[Tuple[str, str]] = []
    for line in text.splitlines():
        m = re.match(r"\s*\d+:\s*(\S+)\s+inet\s+(\d{1,3}(?:\.\d{1,3}){3})", line)
        if m:
            out.append((m.group(1), m.group(2)))
    return out


_IFACE_CACHE: Tuple[float, List[Tuple[str, str]]] = (0.0, [])


def interface_addresses(max_age: float = 5.0) -> List[Tuple[str, str]]:
    """(adapter, IPv4) pairs this machine holds right now (cached a few
    seconds: the bind check asks every 20 s, the beacon more often)."""
    global _IFACE_CACHE
    now = time.monotonic()
    if _IFACE_CACHE[0] and now - _IFACE_CACHE[0] < max_age:
        return _IFACE_CACHE[1]
    found: List[Tuple[str, str]] = []
    try:
        if os.name == "nt":
            text = subprocess.run(["ipconfig"], capture_output=True, text=True,
                                  timeout=5, errors="replace").stdout
            found = parse_ipconfig_all(text)
        elif shutil.which("ip"):
            text = subprocess.run(["ip", "-o", "-4", "addr", "show"], capture_output=True,
                                  text=True, timeout=5).stdout
            found = parse_ip_addr_all(text)
    except (OSError, subprocess.SubprocessError):
        found = []
    _IFACE_CACHE = (now, found)
    return found


#: Adapter names that are a VIRTUAL network on this machine, never a place a
#: phone can be: WSL / Hyper-V ("vEthernet (WSL)", "Default Switch" --
#: 172.x addresses on every Windows laptop with WSL or Docker), VirtualBox,
#: VMware, Docker, libvirt. Taking one of those for "the house router" made
#: the hub bind, announce and hand out 172.27.160.1, which no phone reaches.
_VIRTUAL_NAMES = ("vethernet", "wsl", "hyper-v", "default switch", "virtualbox",
                  "vmware", "docker", "vbox", "virbr", "veth", "br-", "vmnet",
                  "tap", "tun", "zerotier", "loopback")


def is_virtual_adapter(name: str) -> bool:
    n = name.strip().lower()
    return any(k in n for k in _VIRTUAL_NAMES)


def parse_ipconfig_virtual(text: str) -> List[str]:
    """IPv4 addresses under virtual adapters in `ipconfig` output. Adapter
    headers are the unindented lines ending in ':'; the address is the line
    naming IPv4 (English and French both say "IPv4")."""
    out: List[str] = []
    virtual = False
    for line in text.splitlines():
        if line and not line[0].isspace() and line.rstrip().endswith(":"):
            virtual = is_virtual_adapter(line)
            continue
        if virtual and "IPv4" in line:
            m = re.search(r"(\d{1,3}(?:\.\d{1,3}){3})", line.split(":", 1)[-1])
            if m and m.group(1) not in out:
                out.append(m.group(1))
    return out


def parse_ip_addr_virtual(text: str) -> List[str]:
    """Same, from Linux `ip -o -4 addr show` ("3: docker0    inet 172.17.0.1/16 ...")."""
    out: List[str] = []
    for line in text.splitlines():
        m = re.match(r"\s*\d+:\s*(\S+)\s+inet\s+(\d{1,3}(?:\.\d{1,3}){3})", line)
        if m and is_virtual_adapter(m.group(1)) and m.group(2) not in out:
            out.append(m.group(2))
    return out


_VIRTUAL_CACHE: Tuple[float, List[str]] = (0.0, [])


def virtual_addresses(max_age: float = 60.0) -> List[str]:
    """This machine's addresses on virtual adapters (cached a minute; ''
    tools missing -> nothing is excluded, as before)."""
    global _VIRTUAL_CACHE
    now = time.monotonic()
    if _VIRTUAL_CACHE[0] and now - _VIRTUAL_CACHE[0] < max_age:
        return _VIRTUAL_CACHE[1]
    found: List[str] = []
    try:
        if os.name == "nt":
            text = subprocess.run(["ipconfig"], capture_output=True, text=True,
                                  timeout=5, errors="replace").stdout
            found = parse_ipconfig_virtual(text)
        elif shutil.which("ip"):
            text = subprocess.run(["ip", "-o", "-4", "addr", "show"], capture_output=True,
                                  text=True, timeout=5).stdout
            found = parse_ip_addr_virtual(text)
    except (OSError, subprocess.SubprocessError):
        found = []
    _VIRTUAL_CACHE = (now, found)
    return found


def lan_address(*, route: Callable[[], str] = route_address,
                all_addresses: Callable[[], List[str]] = _all_addresses,
                virtual: Callable[[], List[str]] = virtual_addresses) -> Optional[str]:
    """This machine's address on a router's network, or None when it is
    not on one. The route's answer first (right on a hub that is a client
    of one network), then any private address the machine holds."""
    override = os.environ.get("HOMEVOICE_LAN_HOST", "").strip()
    if override:
        try:
            ipaddress.ip_address(override)
        except ValueError:
            return None
        return override                     # an operator's explicit word
    virtual = set(virtual())
    candidate = route()
    if candidate and is_lan_address(candidate) and candidate not in virtual:
        return candidate
    for addr in all_addresses():
        if is_lan_address(addr) and addr not in virtual:
            return addr
    return None


def local_addresses(*, route: Callable[[], str] = route_address,
                    all_addresses: Callable[[], List[str]] = _all_addresses,
                    virtual: Callable[[], List[str]] = virtual_addresses) -> List[str]:
    """EVERY private address this machine holds right now -- the router's,
    a hotspot it runs, any network at all -- which is where the hub listens.
    No number is written down anywhere: whatever the network handed out (or
    the hotspot took) is served, and the phone finds it from the pairing
    code or the beacon and proves it by the certificate.

    Not: loopback, link-local, the tailnet (its own binding), public
    addresses, or virtual adapters (WSL / Hyper-V / Docker -- no phone
    there). HOMEVOICE_LAN_HOST, when set, is the only answer.

    Order: networks this machine RUNS (a hotspot: an address that is not the
    way out) first, the router's last -- the order a pairing code prefers."""
    override = os.environ.get("HOMEVOICE_LAN_HOST", "").strip()
    if override:
        try:
            ipaddress.ip_address(override)
        except ValueError:
            return []
        return [override]
    virtual_set = set(virtual())
    held: List[str] = []
    for addr in all_addresses():
        if is_lan_address(addr) and addr not in virtual_set and addr not in held:
            held.append(addr)
    way_out = route()
    if (way_out and is_lan_address(way_out) and way_out not in virtual_set
            and way_out not in held):
        held.append(way_out)
    return [a for a in held if a != way_out] + [a for a in held if a == way_out]


def resolve_host(host: str) -> Optional[str]:
    """A binding's `host` as an address: itself, or -- for the sentinel --
    whatever lan_address() says right now (None when the hub is on no
    router, which the caller treats as 'this binding does not exist')."""
    if host == LAN_SENTINEL:
        return lan_address()
    return host


# ── moving this machine to another Wi-Fi ────────────────────────────────

def _profile_name(ssid: str) -> str:
    return f"homevoice-{ssid}"


def _nm_unescape(value: str) -> str:
    """Undo nmcli's terse-mode escaping of a field: in `-t` output a ':'
    or '\\' inside a value is written '\\:' / '\\\\'. A profile name
    read back escaped ('Marc4\\:Airtel') names no profile when handed to
    nmcli again."""
    out, i = [], 0
    while i < len(value):
        if value[i] == "\\" and i + 1 < len(value):
            i += 1
        out.append(value[i])
        i += 1
    return "".join(out)


def _nm(*args: str, timeout: float = 15) -> Optional[subprocess.CompletedProcess]:
    """One nmcli call, or None if it could not be run at all. The one
    wrapper every call below goes through -- each used to carry its own
    try/except around its own subprocess.run."""
    try:
        return subprocess.run(["nmcli", *args], capture_output=True,
                              text=True, timeout=timeout)
    except Exception:                                  # noqa: BLE001
        return None


def _nm_rows(*args: str) -> List[Tuple[str, str]]:
    """nmcli's terse `-t` output as (everything before the last ':', the
    last field) pairs -- unescaped, see _nm_unescape."""
    done = _nm(*args)
    rows = []
    for line in ((done.stdout or "") if done else "").splitlines():
        head, _, last = line.rpartition(":")
        rows.append((_nm_unescape(head), last))
    return rows


def _nm_setting(profile: str, field: str) -> str:
    """One setting of a saved profile (`802-11-wireless.ssid`), or ''.

    UNESCAPED like every other terse field: an SSID with a ':' in it came
    back as 'Home\\:Net' and never equalled the network it names."""
    done = _nm("-t", "-f", field, "con", "show", profile)
    for line in ((done.stdout or "") if done else "").splitlines():
        key, _, value = line.partition(":")
        if key.strip() == field:
            return _nm_unescape(value.strip())
    return ""


def saved_password(profile: str) -> str:
    """The Wi-Fi password NetworkManager keeps for `profile`, or '' when it
    will not say (a system profile's secrets can need root) or has none."""
    if not profile:
        return ""
    done = _nm("-s", "-t", "-f", "802-11-wireless-security.psk", "con",
               "show", profile)
    for line in ((done.stdout or "") if done else "").splitlines():
        key, _, value = line.partition(":")
        if key.strip() == "802-11-wireless-security.psk":
            return _nm_unescape(value.strip())
    return ""


def profile_ssid(profile: str) -> str:
    """The SSID a NetworkManager profile joins, or '' -- a profile is a
    NAME ('homevoice-router') and the network is a fact about it, and the
    two must never be compared to each other (2026-09-09: a hub already on
    'Marc4@Airtel_C6D3' under a profile called 'homevoice-router' was
    offered a move to 'Marc4@Airtel_C6D3', accepted it, and failed)."""
    return _nm_setting(profile, "802-11-wireless.ssid") if profile else ""


def access_point_profiles() -> List[str]:
    """Every saved NetworkManager profile that makes this machine an ACCESS
    POINT (802-11-wireless.mode ap) -- the hub's own hotspot, from before it
    became a client of a router. Such a profile never fails to come up (it
    needs no other network), so if it is left able to autoconnect it wins
    every time the router is slow or absent at boot, and then holds the
    radio for good: NetworkManager does not go back to a client profile
    while an access point it runs is up. Found so it can be demoted."""
    return [name for name, kind in _nm_rows("-t", "-f", "NAME,TYPE", "con", "show")
            if name and "wireless" in kind
            and _nm_setting(name, "802-11-wireless.mode") == "ap"]


def _settle_autoconnect(new: str, old: str, say: Callable[[str], None]) -> None:
    """After a successful move: the new road is the one that comes back on
    its own, and nothing else takes the radio uninvited.

    The router's profile is made to retry FOREVER (a power cut brings the
    Pi up before the router, and NetworkManager's default of four tries
    would give up on it), at priority 200 -- the hub's own setup gives its
    router profile 200 and its hotspot 100, so a lower number here would
    have ranked the new road UNDER the hotspot (seen on the Pi, 2026-09-10). The road
    it replaced, and every access-point profile this hub still holds, are
    left saved -- a person can pick one by hand -- but no longer
    autoconnect. Before this, the hub's own hotspot profile was kept able
    to autoconnect as a safety net, and it was that safety net that put a
    hub back on its hotspot, unreachable from the router's phones, after
    the router was slow to come up (2026-09-10)."""
    _nm("con", "modify", new, "connection.autoconnect", "yes",
        "connection.autoconnect-priority", "200", "connection.autoconnect-retries", "0")
    demote = [old] if old and old != new else []
    demote += [ap for ap in access_point_profiles() if ap not in demote and ap != new]
    for name in demote:
        done = _nm("con", "modify", name, "connection.autoconnect", "no")
        if done is not None and done.returncode == 0:
            say(f"[NET] {name!r} stays saved but no longer comes up on its own")


def active_ssid(device: str = "wlan0") -> str:
    """The SSID this machine is joined to on `device`, or ''."""
    return profile_ssid(active_wifi(device))


def active_wifi(device: str = "wlan0") -> str:
    """The NetworkManager profile currently up on `device`, or ''."""
    return next((name for name, dev in
                 _nm_rows("-t", "-f", "NAME,DEVICE", "con", "show", "--active")
                 if dev == device and name), "")


def join_wifi(ssid: str, password: str, *, device: str = "wlan0",
              say: Callable[[str], None] = print, timeout: float = 90.0
              ) -> Tuple[bool, str]:
    """Move this machine onto `ssid`. (ok, what happened) -- never raises.

    The old profile is kept, and re-activated if the new network will not
    come up, so the hub is not stranded by a typo. nmcli blocks until the
    connection is up or has failed, which is exactly the answer wanted."""
    if not shutil.which("nmcli"):
        return False, ("NetworkManager's nmcli is not on this machine, so the hub "
                       "cannot move itself. Join the network from its keyboard, "
                       "or with a cable: sudo nmtui.")
    old = active_wifi(device)
    name = _profile_name(ssid)
    # A leftover profile with this name (an earlier failed attempt) would
    # make `connect` refuse; drop it first. Failure here is fine.
    _nm("con", "delete", name)
    _nm("dev", "wifi", "rescan", "ifname", device, timeout=30)
    say(f"[NET] joining {ssid!r} on {device} (was {old or 'nothing'})...")
    # AN OPEN NETWORK HAS NO PASSWORD, and `password ""` is not "none":
    # nmcli builds a WPA profile around the empty key and refuses it.
    secret = ["password", password] if password else []
    try:
        done = subprocess.run(["nmcli", "dev", "wifi", "connect", ssid, *secret,
                               "ifname", device, "name", name],
                              capture_output=True, text=True, timeout=timeout)
        ok = done.returncode == 0
        detail = ((done.stderr or done.stdout or "").strip().splitlines() or [""])[-1]
    except Exception as exc:                           # noqa: BLE001
        ok, detail = False, f"{type(exc).__name__}: {exc}"
    if ok:
        say(f"[NET] on {ssid!r}: {detail or 'connected'}")
        _settle_autoconnect(name, old, say)
        return True, detail or "connected"
    say(f"[NET] could not join {ssid!r}: {detail}")
    if "not authorized" in detail.lower():
        # POLKIT, not the password: a hub started by systemd has no login
        # session and NetworkManager refuses it. setup_manager --service
        # writes the rule that allows the hub's user; say so, once.
        detail += (" (the hub is not allowed to change networking from a "
                   "service: run  python3 setup_manager.py --service  again, "
                   "which installs the permission)")
    if old:
        # A refused ATTEMPT is not a dropped connection. If the old profile
        # is still up, nothing was lost and saying "could not get back"
        # would be a lie that sends someone to the keyboard for nothing.
        if active_wifi(device) == old:
            say(f"[NET] still on {old!r}; nothing changed")
            return False, f"{detail} -- still on {old!r}, nothing changed"
        back = _nm("con", "up", old, timeout=timeout)
        if back is not None and back.returncode == 0:
            say(f"[NET] back on {old!r}")
            return False, f"{detail} -- fell back to {old!r}"
        return False, f"{detail} -- and could not get back onto {old!r} either"
    return False, detail
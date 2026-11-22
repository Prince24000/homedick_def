"""The hub must never take a VIRTUAL adapter (WSL / Hyper-V / Docker) for the
house network: it bound, announced and handed out 172.27.160.1, which no
phone can reach. Run: python3 tests/test_hub_net_virtual.py"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from hub import hub_net as H                  # noqa: E402

IPCONFIG = """Windows IP Configuration

Ethernet adapter vEthernet (WSL (Hyper-V firewall)):

   IPv4 Address. . . . . . . . . . . : 172.27.160.1
   Subnet Mask . . . . . . . . . . . : 255.255.240.0

Wireless LAN adapter Local Area Connection* 10:

   Adresse IPv4. . . . . . . . . . . . . .: 192.168.137.1

Wireless LAN adapter Wi-Fi:

   IPv4 Address. . . . . . . . . . . : 172.16.153.32

Ethernet adapter vEthernet (Default Switch):

   IPv4 Address. . . . . . . . . . . : 172.20.0.1
"""
PASSED = FAILED = 0


def check(name, ok, detail=""):
    global PASSED, FAILED
    PASSED, FAILED = PASSED + bool(ok), FAILED + (not ok)
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}   {'' if ok else detail}")


def run():
    got = H.parse_ipconfig_virtual(IPCONFIG)
    check("ipconfig: WSL and Default Switch are virtual, Wi-Fi and hotspot are not",
          got == ["172.27.160.1", "172.20.0.1"], got)
    got = H.parse_ip_addr_virtual("1: lo    inet 127.0.0.1/8\n2: wlan0    inet 192.168.4.1/24\n"
                                  "3: docker0    inet 172.17.0.1/16\n4: virbr0    inet 192.168.122.1/24")
    check("ip addr: docker0 and virbr0 are virtual", got == ["172.17.0.1", "192.168.122.1"], got)
    virt = lambda: ["172.27.160.1"]                                     # noqa: E731
    check("lan: the router address, not WSL",
          H.lan_address(route=lambda: "", all_addresses=lambda: ["172.27.160.1", "172.16.153.32"],
                        virtual=virt) == "172.16.153.32")
    check("lan: the route itself pointing at WSL is refused",
          H.lan_address(route=lambda: "172.27.160.1", all_addresses=lambda: ["172.27.160.1"],
                        virtual=virt) is None)
    check("lan: a hotspot this machine runs is a network like any other",
          H.lan_address(route=lambda: "", all_addresses=lambda: ["172.27.160.1", "192.168.137.1"],
                        virtual=virt) == "192.168.137.1")
    # NO FIXED ADDRESSES: every private address held is served, whatever
    # its number; hotspot-like ones (not the way out) first, router last.
    got = H.local_addresses(route=lambda: "172.16.153.32",
                            all_addresses=lambda: ["127.0.0.1", "10.42.0.1", "172.27.160.1",
                                                   "172.16.153.32", "100.101.2.3",
                                                   "169.254.9.9", "8.8.8.8"],
                            virtual=virt)
    check("local: every private address, WSL/tailnet/loopback/public/link-local left out",
          got == ["10.42.0.1", "172.16.153.32"], got)
    got = H.local_addresses(route=lambda: "192.168.1.7", all_addresses=lambda: ["127.0.1.1"],
                            virtual=lambda: [])
    check("local: the route's address counts even when the adapter list missed it",
          got == ["192.168.1.7"], got)
    got = H.local_addresses(route=lambda: "", all_addresses=lambda: ["10.0.0.4"],
                            virtual=lambda: [])
    check("local: an unusual hotspot number is served too", got == ["10.0.0.4"], got)
    os.environ["HOMEVOICE_LAN_HOST"] = "192.168.9.9"
    got = H.local_addresses(route=lambda: "", all_addresses=lambda: ["10.0.0.4"],
                            virtual=lambda: [])
    del os.environ["HOMEVOICE_LAN_HOST"]
    check("local: HOMEVOICE_LAN_HOST is the only answer when set", got == ["192.168.9.9"], got)
    got = H.parse_ipconfig_all(IPCONFIG)
    check("ipconfig: every adapter's address, French 'Adresse IPv4' included",
          [a for _, a in got] == ["172.27.160.1", "192.168.137.1", "172.16.153.32", "172.20.0.1"],
          got)
    got = H.parse_ip_addr_all("1: lo    inet 127.0.0.1/8 scope host lo\n"
                              "3: wlan0    inet 10.42.0.1/24 brd 10.42.0.255")
    check("ip addr: every interface's address", got == [("lo", "127.0.0.1"), ("wlan0", "10.42.0.1")], got)

    # THE SERVER opens "lan" out into one listener per address.
    from core import config
    from app import ws_server
    saved = H.local_addresses
    H.local_addresses = lambda **_k: ["10.42.0.1", "192.168.1.7"]
    try:
        names = [(e["name"], e["host"], e["origin"]) for e in ws_server._expanded_bindings()]
    finally:
        H.local_addresses = saved
    check("server: one listener per address held, named by address",
          ("lan 10.42.0.1", "10.42.0.1", "10.42.0.1") in names
          and ("lan 192.168.1.7", "192.168.1.7", "192.168.1.7") in names, names)
    check("server: no fixed IP in NETWORK_BINDINGS",
          all(e["host"] in ("lan", config.WS_HOSTNAME) for e in config.NETWORK_BINDINGS),
          config.NETWORK_BINDINGS)
    check("server: 'lan' counts as served when any of its addresses is",
          ws_server._serves("lan", {"lan 10.42.0.1": 1})
          and not ws_server._serves("lan", {"tailnet": 1}))


if __name__ == "__main__":
    run()
    print(f"\n{PASSED}/{PASSED + FAILED} checks passed")
    sys.exit(0 if FAILED == 0 else 1)

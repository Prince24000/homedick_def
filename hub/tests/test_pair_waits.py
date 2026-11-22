"""PAIRING STARTS ONCE THERE IS AN ADDRESS (her rule, 2026-10-19).

Asked for with the hotspot off and no Wi-Fi -- --pair, the button, a phone's
Pair a new phone -- the window does not count down for nothing: the ask
waits, and opens, full length, the moment the hub has an address. It lapses
after PAIRING_WAIT_SECONDS. Restarting opens nothing (PAIR_ON_BOOT off).

Run: python3 tests/test_pair_waits.py
"""
import asyncio
import json
import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("HOMEVOICE_HOME", tempfile.mkdtemp())

from core import config                                       # noqa: E402
from identity import phones                                   # noqa: E402
from app import setup_manager as sm                           # noqa: E402

PASSED = FAILED = 0


def check(label, ok, extra=""):
    global PASSED, FAILED
    if ok:
        PASSED += 1
        print(f"  [PASS] {label}")
    else:
        FAILED += 1
        print(f"  [FAIL] {label} {extra}")


d = tempfile.mkdtemp()
W = phones.PairingWindow(os.path.join(d, "pairing-open.json"))

print("== the window waits for an address")
check("restarting the hub opens nothing", config.PAIR_ON_BOOT is False)
W.ask(opened_by=phones.PairingWindow.OPENED_BY_HUB)
check("asked with no address: NOT open, no countdown",
      not W.is_open() and W.seconds_left() == 0)
check("...the ask waits", W.pending() is not None)
time.sleep(1.1)
until = W.open_asked()
check("address appears: opened for the FULL length",
      W.is_open() and abs((until - time.time()) - phones.PAIRING_WINDOW_SECONDS) < 2,
      until - time.time())
check("...and nothing is waiting any more", W.pending() is None)
W.ask(opened_by=phones.PairingWindow.OPENED_BY_MENU, from_away=True)
W.open_asked()
check("who asked, and 'away', are kept",
      W.opened_by() == "menu" and W.allows_away())
W.ask()
W.close()
check("closing (the button pressed) cancels a waiting ask", W.pending() is None)
W.ask()
data = json.load(open(W.path))
data["pending_until"] = time.time() - 1
json.dump(data, open(W.path, "w"))
check("an ask lapses after PAIRING_WAIT_SECONDS",
      W.pending() is None and W.open_asked() is None and not W.is_open())

print("\n== is there an address?")
real = (sm._pair_roads, sm._local_addresses, sm.hub_net.holds_address)
sm._local_addresses = lambda: set()
sm.hub_net.holds_address = lambda a: False
sm._pair_roads = lambda: [("ap", "192.168.137.1",
                           "first address in NETWORK_BINDINGS (this hub is on "
                           "no network yet)")]
check("hotspot off, no Wi-Fi: no road (the stand-in address is not one)",
      sm._reachable_road() is None)
sm._pair_roads = lambda: [("tailnet", "hub.tail1234.ts.net", "over Tailscale")]
check("only Tailscale: none for a phone at home, one for a phone away",
      sm._reachable_road(False) is None and sm._reachable_road(True) is not None)
sm._local_addresses = lambda: {"192.168.137.1"}
sm._pair_roads = lambda: [("ap", "192.168.137.1", "at 192.168.137.1")]
check("hotspot on: a road", sm._reachable_road() is not None)

print("\n== --pair waits, then opens and draws")
roads = [None, None, ("ap", "192.168.137.1", "at 192.168.137.1")]
sm._reachable_road = lambda from_away=False: roads.pop(0) if len(roads) > 1 else roads[0]
reg = phones.PhoneRegistry(os.path.join(d, "phones.json"))
reg.window.path = os.path.join(d, "pairing2.json")
sm._phone_registry = lambda: reg
drawn = []
sm._print_pair_code = lambda until, only=None: drawn.append(until)
real_sleep = time.sleep
sm.time.sleep = lambda s: None
ok = sm._open_pairing(["--pair"])
sm.time.sleep = real_sleep
check("no address at first: it waited, then opened when one came",
      ok and reg.window.is_open() and drawn
      and abs(drawn[0] - time.time() - phones.PAIRING_WINDOW_SECONDS) < 2, drawn)

print("\n== the hub opens a waiting ask by itself")
from app import ws_server                                     # noqa: E402
reg.window.close()
reg.window.ask()
ws_server.PHONES = reg
codes = []
ws_server._write_pair_codes = lambda until: codes.append(until)
state = {"up": False}
ws_server._pair_reachable = lambda away: state["up"]


async def run():
    task = asyncio.create_task(ws_server._pair_window_watcher())
    await asyncio.sleep(0.2)
    shut = not reg.window.is_open()
    state["up"] = True
    await asyncio.sleep(2.5)
    task.cancel()
    return shut
shut = asyncio.run(run())
real_sleep(0.2)
check("still no address: shut", shut)
check("address appears: opened full length, and the code written",
      reg.window.is_open() and codes
      and reg.window.seconds_left() > phones.PAIRING_WINDOW_SECONDS - 10, codes)

sm._pair_roads, sm._local_addresses, sm.hub_net.holds_address = real
print(f"\n{PASSED} passed, {FAILED} failed")
sys.exit(1 if FAILED else 0)

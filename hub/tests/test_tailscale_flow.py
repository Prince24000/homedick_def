"""Tailscale: the hub starts after tailscaled, and a sign-in that fails is
reported to the phone instead of leaving it waiting.

Run: python3 tests/test_tailscale_flow.py
"""
import asyncio
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.setdefault("HOMEVOICE_HOME", tempfile.mkdtemp())

from app import setup_manager, ws_server                     # noqa: E402
from core import tailnet                                     # noqa: E402

PASSED = FAILED = 0


def check(name, ok, detail=""):
    global PASSED, FAILED
    if ok:
        PASSED += 1
        print(f"  [PASS] {name}")
    else:
        FAILED += 1
        print(f"  [FAIL] {name}   {detail}")


class _Stub:
    """A phone's connection: `live` puts it among the connected phones."""
    def __init__(self, phone_id="p1", live=True):
        self.frames = []
        self.joined = []
        self._phone_id = phone_id
        if live:
            ws_server._LIVE_PHONES.add(self)

    async def _fail(self, reason, text, kind="result", refused=None):
        self.frames.append((kind, reason, text))

    async def _report_join_result(self, result, applied=False):
        if result.get("ok"):
            self.joined.append(result)
        else:
            self.frames.append(("tailscale_failed", result.get("reason"),
                                result.get("text", "")))


def run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def main():
    print("\nthe service starts after tailscaled")
    unit = setup_manager._service_unit("pi", "/home/pi", "/home/pi/Voice AI",
                                       "/usr/bin/python3")
    after = [l for l in unit.splitlines() if l.startswith("After=")]
    check("After= names tailscaled and still the network",
          after and "tailscaled.service" in after[0]
          and "network-online.target" in after[0], after)

    print("\na sign-in that blows up is told to the phone")
    call = ws_server.ConnectionHandler._finish_join_unlocked
    real = tailnet.finish_interactive_join
    real_apply = ws_server._apply_join_result
    ws_server._apply_join_result = lambda _r: None   # no real tailnet here
    try:
        def boom(_proc):
            raise RuntimeError("tailscaled went away")
        tailnet.finish_interactive_join = boom
        stub = _Stub()
        run(call(stub, object(), "p1"))
        check("the phone gets a tailscale_failed frame saying to try again",
              len(stub.frames) == 1 and stub.frames[0][0] == "tailscale_failed"
              and "Try again" in stub.frames[0][2], stub.frames)
        check("nothing was reported as joined", stub.joined == [])

        tailnet.finish_interactive_join = lambda _p: {"ok": True}
        stub = _Stub()
        run(call(stub, object(), "p1"))
        check("a sign-in that works is reported as before, no failure frame",
              stub.joined == [{"ok": True}] and stub.frames == [])

        print("\nthe phone leaves the hub's network to sign in (2026-11-21)")
        ws_server._LIVE_PHONES.clear()
        ws_server._SIGNIN_RESULTS.clear()
        away = _Stub("p2", live=False)          # its connection dropped
        run(call(away, object(), "p2"))
        check("the sign-in still finishes; the answer is kept for the phone",
              ws_server._SIGNIN_RESULTS.get("p2") == {"ok": True}
              and away.joined == [])
        back = _Stub("p2")                      # the same phone, reconnected
        run(back._report_join_result(ws_server._SIGNIN_RESULTS.pop("p2"),
                                     applied=True))
        check("told on the phone's next connection", back.joined == [{"ok": True}])
        ws_server._LIVE_PHONES.clear()
        old, new = _Stub("p3", live=False), _Stub("p3")
        run(call(old, object(), "p3"))
        check("already back on a new connection: told there at once",
              new.joined == [{"ok": True}] and old.joined == []
              and "p3" not in ws_server._SIGNIN_RESULTS)
        proc = object()
        ws_server._SIGNINS["p4"] = proc
        cancelled = []
        real_cancel = tailnet.cancel_interactive
        tailnet.cancel_interactive = cancelled.append
        try:
            run(ws_server.ConnectionHandler._dispatch_tailscale_cancel(
                _Stub("p4"), {}))
        finally:
            tailnet.cancel_interactive = real_cancel
        check("Cancel from the phone's new connection still stops it",
              cancelled == [proc])
    finally:
        tailnet.finish_interactive_join = real
        ws_server._apply_join_result = real_apply
        ws_server._LIVE_PHONES.clear()
        ws_server._SIGNINS.clear()
        ws_server._SIGNIN_RESULTS.clear()

    print("\nTailscale that comes up after the hub did")
    saved = (tailnet.find_tailscale, tailnet.status,
             os.environ.pop("HOMEVOICE_WS_HOSTNAME", None))
    try:
        asked = []

        def status_now(_binary):
            asked.append(1)
            return status_now.value
        status_now.value = None
        tailnet.find_tailscale = lambda: "tailscale"
        tailnet.status = status_now
        check("no Tailscale yet: asked, and no name",
              tailnet.name_if_late(False) == "" and len(asked) == 1)
        status_now.value = {"Self": {"DNSName": "hub.tail0000.ts.net."}}
        check("Tailscale answers later: its name, to be adopted and bound",
              tailnet.name_if_late(False) == "hub.tail0000.ts.net")
        n = len(asked)
        check("once it is listening, Tailscale is not asked again",
              tailnet.name_if_late(True) == "" and len(asked) == n)
        tailnet.find_tailscale = lambda: None
        check("no Tailscale installed: no name", tailnet.name_if_late(False) == "")
    finally:
        tailnet.find_tailscale, tailnet.status = saved[0], saved[1]
        if saved[2] is not None:
            os.environ["HOMEVOICE_WS_HOSTNAME"] = saved[2]
    import inspect
    check("the server's binding check uses it",
          "name_if_late" in inspect.getsource(ws_server._supervise_bindings))

    print("\nthe connection gauge: which road, and how slow")
    state = {"Peer": {
        "k1": {"TailscaleIPs": ["100.80.138.3"], "CurAddr": "", "Relay": "par"},
        "k2": {"TailscaleIPs": ["100.80.1.1"], "CurAddr": "10.0.0.7:41641", "Relay": "lhr"},
        "k3": {"TailscaleIPs": ["100.80.2.2"], "CurAddr": "", "Relay": ""}}}
    check("a peer reached through a relay says which", tailnet.peer_path(state, "100.80.138.3") == "relay par")
    check("a peer reached directly", tailnet.peer_path(state, "100.80.1.1") == "direct")
    check("a relay with no name", tailnet.peer_path(state, "100.80.2.2") == "relay")
    check("nobody by that address", tailnet.peer_path(state, "100.80.9.9") == "")
    check("no status at all", tailnet.peer_path(None, "100.80.138.3") == "")
    check("a local address is never asked of Tailscale", tailnet.path_to("192.168.1.20") == "")
    check("nor is nonsense", tailnet.path_to("not an address") == "")

    H = ws_server.ConnectionHandler
    for ms, want in [(40, "excellent"), (149, "excellent"), (150, "good"), (399, "good"),
                     (400, "slow"), (999, "slow"), (1000, "poor"), (1670, "poor"), (None, "")]:
        check(f"{ms} ms is {want or 'not graded'}", H._link_grade(ms) == want)

    class _Sock:
        latency = 0.87
        remote_address = ("100.80.138.3", 5555)

    h = H.__new__(H)
    h.websocket, h._peer_cache, h._background = _Sock(), None, set()
    h._link_path, h._link_path_at, h._link_grade_said = "", 0.0, ""
    sent = []

    async def send(obj):
        sent.append(obj)
    h._send_json = send
    real_path = tailnet.path_to
    tailnet.path_to = lambda ip: "relay par"
    try:
        async def two_pings():
            await h._dispatch_link_ping({"type": "link_ping", "id": 7})
            await asyncio.sleep(0.05)              # the road is asked off the loop
            await h._dispatch_link_ping({"type": "link_ping", "id": 8})
        run(two_pings())
    finally:
        tailnet.path_to = real_path
    check("answered with the same id and the hub's own measure",
          sent[0]["type"] == "link_pong" and sent[0]["id"] == 7 and sent[0]["hub_rtt_ms"] == 870, sent)
    check("the road follows on the next answer", sent[1]["path"] == "relay par", sent)
    check("the grade is remembered so it is logged once, not per ping", h._link_grade_said == "slow")
    import inspect
    check("the ping is answered before any tracing or lock",
          inspect.getsource(H._route_frame).index("link_ping")
          < inspect.getsource(H._route_frame).index("_trace_frame"))

    print(f"\n{PASSED} passed, {FAILED} failed")
    sys.exit(1 if FAILED else 0)


if __name__ == "__main__":
    main()

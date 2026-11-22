"""TAILSCALE, MADE SIMPLER (2026-11-05).

Checks before the PIN; one sign-in at a time; the hub keeps its own
permission across --reset; a hub that comes back up without a sign-in is a
success; no stale "HTTPS certificate" advice or `tailscale status` homework;
a hub that lost Tailscale is noticed. The tailscale CLI is faked.

Run: python3 tests/test_tailscale_fixes.py
"""
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.setdefault("HOMEVOICE_HOME", tempfile.mkdtemp())

from core import tailnet as T                                  # noqa: E402

PASSED = FAILED = 0


def check(name, ok, detail=""):
    global PASSED, FAILED
    PASSED, FAILED = PASSED + bool(ok), FAILED + (not ok)
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}   {'' if ok else detail}")


state = {"BackendState": "NeedsLogin", "Self": {"DNSName": ""}}
calls = []


def fake_run(argv, timeout):
    calls.append(argv)
    if argv[1] == "status":
        return subprocess.CompletedProcess(argv, 0, json.dumps(state), "")
    if argv[1] == "up":
        state.update({"BackendState": "Running",
                      "Self": {"DNSName": "hub.tail1234.ts.net.",
                               "TailscaleIPs": ["100.64.0.5"]}})
        return subprocess.CompletedProcess(argv, 0, "", "")
    if argv[1] == "cert":
        return subprocess.CompletedProcess(argv, 1, "", "no https")
    return subprocess.CompletedProcess(argv, 0, "", "")


T._run = fake_run
T.find_tailscale = lambda: "/usr/bin/tailscale"
online = {"up": True}
T._connect = lambda h, p, t: online["up"]

print("\nchecked BEFORE the PIN")
check("ready: nothing in the way", T.ready() is None, T.ready())
online["up"] = False
r = T.ready()
check("no internet: said before any PIN, in words",
      r and r["reason"] == "no_internet" and "router" in r["text"], r)
online["up"] = True
T.find_tailscale = lambda: None
r = T.ready()
check("Tailscale not on the hub: said before any PIN",
      r and r["reason"] == "not_installed", r)
T.find_tailscale = lambda: "/usr/bin/tailscale"

print("\na key join")
res = T.join("tskey-auth-abc123")
up = next(a for a in calls if a[1] == "up")
check("joined, and the reply carries no certificate homework",
      res["ok"] and "certificate" not in res["text"].lower()
      and "tailscale cert" not in res["text"], res)
check("the hub keeps its own permission across --reset (--operator)",
      any(a.startswith("--operator=") for a in up), up)
check("...and is free for the next join afterwards", not T._busy_now())

print("\none sign-in at a time")
T._mark_joining(True)
r = T.join("tskey-auth-zzz")
check("a second join while one runs: refused in words, key not spent",
      r["reason"] == "busy" and not any("tskey-auth-zzz" in " ".join(a)
                                       for a in calls), r)
check("...and the PIN is not even asked (ready says busy)",
      (T.ready() or {}).get("reason") == "busy")
T._mark_joining(False)

print("\na hub that comes back up with no sign-in needed")
state.update({"BackendState": "Stopped", "Self": {"DNSName": "hub.tail1234.ts.net."}})


class Proc:
    returncode = None
    stdout = iter([])

    def wait(self, timeout=None):
        self.returncode = 0
        state["BackendState"] = "Running"
        return 0

    def kill(self):
        pass


T._popen = lambda argv: Proc()
begun = T.begin_interactive_join()
check("logged in but stopped: 'up' is a success, not 'exited before a "
      "sign-in address'", begun.get("ok") and "proc" not in begun
      and begun.get("tailnet_name") == "hub.tail1234.ts.net", begun)
check("...and nothing is left marked busy", not T._busy_now())

print("\nthe permission message")
hint = T._permission_hint("Access denied: checkprefs access denied")
check("access denied: the phone's way out first (turn the hub off and on)",
      "off and on again" in hint, hint)

print("\nlosing Tailscale is noticed")
state["BackendState"] = "NeedsLogin"
check("logged out: no connected name", T.connected_name() == "")
state["BackendState"] = "Running"
check("running: its name", T.connected_name() == "hub.tail1234.ts.net")

print(f"\n{PASSED} passed, {FAILED} failed")
sys.exit(1 if FAILED else 0)

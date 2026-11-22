"""Talking over a running command (2026-10-09): the hold, cancel, one free
addition or correction, and nothing for the television.

Run: python3 tests/test_talk_over.py   (from anywhere; no models needed)
"""
import asyncio
import base64
import io
import os
import sys
import tempfile
import wave

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("HOMEVOICE_HOME", tempfile.mkdtemp())

import numpy as np                                         # noqa: E402

from core import config                                    # noqa: E402
from app import ws_server as W                            # noqa: E402


class _Phones:
    def user_for(self, phone_id):
        return "augusta" if phone_id == "phone-1" else None


W.PHONES = _Phones()

PASSED = FAILED = 0


def check(name, ok, detail=""):
    global PASSED, FAILED
    if ok:
        PASSED += 1
        print(f"  [PASS] {name}")
    else:
        FAILED += 1
        print(f"  [FAIL] {name}   {detail}")


class _DM:
    devices = {"lamp": {"name": "lamp"}, "fan": {"name": "ceiling fan"},
               "heater": {"name": "heater"}}


class _Shell:
    def __init__(self, mine=True):
        self.dm = _DM()
        self.intent_classifier = None
        self.mine = mine
        self.checked = 0
        self.runs = []
        self.command_action = {"switch": "on"}
        self.last_multi_outcome = {"done": ["lamp on"]}

    async def _passive_voice_check(self, speaker, audio):
        self.checked += 1
        return self.mine

    async def _run_multi(self, text, speaker_id, audio=None):
        self.runs.append((text, dict(getattr(self, "carry_slots", {})),
                          getattr(self, "free_ride", False)))
        self.last_multi_outcome = {"done": [f"{text} done"]}
        return True


def wav_b64():
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16000)
        w.writeframes((np.zeros(16000) * 0).astype(np.int16).tobytes())
    return base64.b64encode(buf.getvalue()).decode()


def handler(shell, running=True):
    h = W.ConnectionHandler.__new__(W.ConnectionHandler)
    h.shell = shell
    h._hold = asyncio.Event()
    h._hold.set()
    h._asides, h._aside_free = [], True
    h._cancel_requested = False
    h._active_command_queue = None
    h._quiet_flow = False
    h._phone_id = "phone-1"
    h.sent = []
    h.cancelled = 0
    h.forwarded = []

    async def send(frame):
        h.sent.append(frame)

    async def cancel(msg):
        h.cancelled += 1

    async def forward(msg):
        h.forwarded.append(msg)
    h._send_json = send
    h._dispatch_cancel = cancel
    h._dispatch_command_frame = forward
    h._flow_running = lambda: running
    return h


def kinds():
    print("\n== what was said over the command (rules, not meaning)")
    h = handler(_Shell())
    cases = [
        ("and the fan", ("add", "and the fan")),
        ("the heater too", ("add", "the heater too")),
        ("no, the fan", ("correct", "fan")),
        ("I meant the heater", ("correct", "heater")),
        ("no the lump", ("correct", "lump")),           # sounds like lamp
        ("stop", ("cancel", "")),
        ("never mind", ("cancel", "")),
        ("cancel", ("cancel", "")),
        ("the weather is nice today", ("", "")),
        ("pass me the remote", ("", "")),
    ]
    for said, want in cases:
        got = h._aside_kind(said)
        check(f"{said!r} -> {want[0] or 'nothing'}", got == want, got)


async def flows():
    print("\n== the hold")
    h = handler(_Shell())
    await h._dispatch_hold({})
    check("speech over a running command holds it", not h._hold.is_set())
    config.INTERRUPT_HOLD_MAX_S = 0.2
    t = asyncio.get_running_loop().time()
    await h._wait_hold()
    took = asyncio.get_running_loop().time() - t
    check("never longer than the limit", 0.1 <= took < 0.5 and h._hold.is_set(),
          took)
    await h._dispatch_hold({})
    await h._dispatch_release({})
    check("released when it was nothing", h._hold.is_set())
    idle = handler(_Shell(), running=False)
    await idle._dispatch_hold({})
    check("nothing running: nothing to hold", idle._hold.is_set())

    print("\n== the voice check comes first, cancel included")
    stranger = handler(_Shell(mine=False))
    await stranger._dispatch_hold({})
    await stranger._dispatch_aside({"text": "stop", "audio": wav_b64()})
    check("a stranger's 'stop' (the TV) is ignored, and the hold let go",
          stranger.cancelled == 0 and stranger._hold.is_set()
          and stranger.sent[-1].get("why") == "voice", stranger.sent)
    noclip = handler(_Shell())
    await noclip._dispatch_aside({"text": "stop"})
    check("no recording, no proof: ignored", noclip.cancelled == 0)
    owner = handler(_Shell())
    await owner._dispatch_aside({"text": "stop", "audio": wav_b64()})
    check("the owner's 'stop' cancels", owner.cancelled == 1)
    check("and does not use the free ride", owner._aside_free)

    print("\n== one free ride per running command")
    h = handler(_Shell())
    await h._dispatch_aside({"text": "and the fan", "audio": wav_b64()})
    await h._dispatch_aside({"text": "and the heater", "audio": wav_b64()})
    check("the first addition rides free, the second is counted",
          [a["free"] for a in h._asides] == [True, False], h._asides)
    ok = await h._run_asides("augusta", True)
    runs = h.shell.runs
    check("both run after the command, with its action",
          [r[0] for r in runs] == ["and the fan", "and the heater"]
          and all(r[1] == {"switch": "on"} for r in runs), runs)
    check("free only for the first", [r[2] for r in runs] == [True, False], runs)
    check("one result for all of it", ok and h.shell.last_multi_outcome["done"]
          == ["lamp on", "and the fan done", "and the heater done"],
          h.shell.last_multi_outcome)
    check("nothing left to ride", not getattr(h.shell, "free_ride", False)
          and h.shell.carry_slots == {})

    print("\n== a correction")
    h = handler(_Shell())
    await h._dispatch_aside({"text": "no, the fan", "audio": wav_b64()})
    check("stops the running command", h._cancel_requested
          and h._asides[0]["replace"] and h._asides[0]["text"] == "fan")
    ok = await h._run_asides("augusta", False)
    check("and the fan takes its place", ok and h.shell.runs[0][0] == "fan")

    print("\n== the command is already over")
    late = handler(_Shell(), running=False)
    await late._dispatch_aside({"text": "and the fan", "audio": wav_b64()})
    check("an ordinary command now, taken as overheard (owner's voice, "
          "meant for the house, counted)",
          late.forwarded and late.forwarded[0]["type"] == "command"
          and late.forwarded[0]["ambient"] is True, late.forwarded)


async def free_ride_gate():
    print("\n== the hub's gate: the voice of the command, then the PIN")
    from app.shell import InteractiveShell

    class _Store:
        bumped = 0

        def has_voice(self, u):
            return True

        def get_command_count(self, u):
            return 0

        def increment_command_count(self, u):
            _Store.bumped += 1

    class _IO:
        trusted_local = False

        async def write(self, *a, **k):
            pass

    sh = InteractiveShell.__new__(InteractiveShell)
    sh._io, sh.biometric_store = _IO(), _Store()
    sh.current_phone_id = "phone-1"
    checks, pins = [], []
    voice_ok = [True]

    async def passive(u, a):
        checks.append(u)
        return voice_ok[0]

    async def pin(u):
        pins.append(u)
        return True
    sh._passive_voice_check, sh._offer_pin_fallback = passive, pin
    clip = np.zeros(16000, dtype=np.float32)
    sh.free_ride = True
    ok = await sh._voice_gate("augusta", clip)
    check("its own clip is checked", ok and checks == ["augusta"])
    ok = await sh._voice_gate("augusta", clip, force_active=True)
    check("a secure device: the voice of the command, no challenge words",
          ok and checks == ["augusta", "augusta"] and not pins, pins)
    check("no challenge exists any more",
          not hasattr(sh, "_active_voice_challenge"))
    voice_ok[0] = False
    ok = await sh._voice_gate("augusta", clip, force_active=True)
    check("a voice that does not match is asked for the PIN",
          ok and pins == ["augusta"], pins)


def cancel_by_sound():
    print("\n== 'cancel' as the recogniser writes it, at a question")
    from nlu.spoken_cancel import sounds_like_cancel
    for heard in ("Council.", "consume", "can't sue.", "cancel please"):
        check(f"{heard!r} -> cancel", sounds_like_cancel(heard))
    for heard in ("number one", "the kitchen lamp", "a new board", "yes",
                  "camera", "turn on the lamp now please"):
        check(f"{heard!r} -> not", not sounds_like_cancel(heard))


if __name__ == "__main__":
    kinds()
    cancel_by_sound()
    asyncio.run(flows())
    asyncio.run(free_ride_gate())
    print(f"\n  {PASSED} passed, {FAILED} failed")
    sys.exit(1 if FAILED else 0)

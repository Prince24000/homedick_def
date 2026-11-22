"""Cancel stops the hub at once; changing a PIN starts with the old one;
"Recovery words" makes new words behind the PIN.

Run: python3 tests/test_cancel_and_pin.py   (no hardware, no network)
"""
import asyncio
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("HOMEVOICE_HOME", tempfile.mkdtemp())
os.environ.setdefault("HOMEVOICE_ENV_FILE", "")

from app.ws_server import ConnectionHandler            # noqa: E402
from app.shell import InteractiveShell, IOBackend      # noqa: E402
from core.hv_common import CancelledByUser             # noqa: E402

PASSED = FAILED = 0


def check(name, ok, detail=""):
    global PASSED, FAILED
    PASSED, FAILED = PASSED + bool(ok), FAILED + (not ok)
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}   {'' if ok else detail}")


def handler():
    h = ConnectionHandler.__new__(ConnectionHandler)
    h._command_task = None
    h._quiet_flow = False
    h._cancel_requested = False
    h._pre_lock = False
    h._active_command_queue = asyncio.Queue()
    h._bootstrapping = False
    h.sent = []

    async def send(frame):
        h.sent.append(frame)
    h._send_json = send
    return h


async def cancel_checks():
    print("\ncancel")
    h = handler()
    await h._dispatch_cancel({"type": "cancel"})
    check("idle: answered at once, nothing left armed",
          h.sent == [{"type": "cancel_ack", "running": False}]
          and not h._cancel_requested, h.sent)

    # A flow WAITING on a question ends the moment Cancel arrives.
    h = handler()
    waiting = asyncio.ensure_future(h._await_reply())
    h._command_task = waiting
    await asyncio.sleep(0)
    await h._dispatch_cancel({"type": "cancel"})
    try:
        await asyncio.wait_for(waiting, 1.0)
        ended = "answered"
    except CancelledByUser:
        ended = "cancelled"
    except asyncio.TimeoutError:
        ended = "still waiting"
    check("waiting flow: stops immediately", ended == "cancelled", ended)
    check("waiting flow: the cancel is spent, queue clean",
          not h._cancel_requested and h._active_command_queue.empty())
    check("waiting flow: phone told the flow is stopping",
          {"type": "cancel_ack", "running": True} in h.sent, h.sent)

    # A BUSY flow (thinking, no question out) stops at its next question,
    # and only that one: the cancel is one-shot.
    h = handler()
    h._command_task = asyncio.ensure_future(asyncio.sleep(5))
    await h._dispatch_cancel({"type": "cancel"})
    try:
        await asyncio.wait_for(h._await_reply(), 1.0)
        first = "answered"
    except CancelledByUser:
        first = "cancelled"
    h._active_command_queue.put_nowait({"type": "prompt_reply", "value": "x"})
    second = await asyncio.wait_for(h._await_reply(), 1.0)
    h._command_task.cancel()
    check("busy flow: its next question is cancelled", first == "cancelled", first)
    check("busy flow: one-shot -- the question after is answered normally",
          second.get("value") == "x", second)

    # Checkpoint before a device write (shell._carry_out).
    h = handler()
    h._command_task = asyncio.ensure_future(asyncio.sleep(5))
    await h._dispatch_cancel({"type": "cancel"})
    io = IOBackend()
    io.is_cancelled = h._take_cancel
    try:
        io.raise_if_cancelled()
        stopped = False
    except CancelledByUser:
        stopped = True
    h._command_task.cancel()
    check("checkpoint: a cancelled command stops before touching a device", stopped)
    try:
        io.raise_if_cancelled()
        again = False
    except CancelledByUser:
        again = True
    check("checkpoint: and only once", not again)
    check("terminal backend (no cancel channel) never stops",
          IOBackend().raise_if_cancelled() is None)

    # A command still BEFORE the shell lock (the 85 s navigation check in
    # the field log) is ended on the spot, and the phone hears it.
    h = handler()
    h._pre_lock = True
    h._command_task = asyncio.ensure_future(asyncio.sleep(5))
    await h._dispatch_cancel({"type": "cancel"})
    await asyncio.sleep(0)
    check("pre-lock command: ended at once",
          h._command_task.cancelled() or h._command_task.done(), h._command_task)
    check("pre-lock command: phone told it is over, nothing left armed",
          not h._cancel_requested
          and any(f.get("reason") == "cancelled" for f in h.sent)
          and {"type": "cancel_ack", "running": False} in h.sent, h.sent)

    # A cancel in the SAME BREATH as the command (2026-10-07: the two
    # frames arrived together over a slow link, the cancel was read before
    # the command's task had run a line, and the command went on).
    h = handler()
    h._background = set()
    ran = []

    async def may(*_a, **_k):
        return True

    async def command(_msg):
        ran.append(1)
    h._may_start_flow = may
    h._handle_command = command
    h._install_queue = lambda task: None
    h._release_queue = lambda task: None
    await h._dispatch_command_frame({"type": "command", "text": "x"})
    await h._dispatch_cancel({"type": "cancel"})
    await asyncio.sleep(0.05)
    check("cancel right behind the command: the command never runs",
          not ran and any(f.get("reason") == "cancelled" for f in h.sent)
          and not h._pre_lock and h._command_task is None, (ran, h.sent))

    # A real answer queued beside the wake-up frame survives the take.
    h = handler()
    h._cancel_requested = True
    h._active_command_queue.put_nowait({"type": "cancel"})
    h._active_command_queue.put_nowait({"type": "choice_response", "values": ["a"]})
    h._take_cancel()
    left = h._active_command_queue.get_nowait()
    check("take: removes only the wake-up frame", left.get("type") == "choice_response", left)


class _IO(IOBackend):
    def __init__(self, choices, inputs):
        self.choices, self.inputs, self.out = list(choices), list(inputs), []

    async def write(self, text, level="info", **_k):
        self.out.append(text)

    async def choose(self, title, options, **_k):
        return [self.choices.pop(0)] if self.choices else []

    async def get_input(self, prompt, **_k):
        return self.inputs.pop(0)

    async def ainput(self, prompt, **_k):
        return "OK"


class _Pin:
    def __init__(self, pin):
        self.pin, self.renewed = pin, 0

    def verify(self, user, pin):
        return (True, "success") if pin == self.pin else (False, "wrong_pin")

    def get_lockout_remaining(self, user):
        return 0


class _Fallback:
    def __init__(self, pin="123456"):
        self.pin = _Pin(pin)
        self.set_to = None

    def is_enrolled(self, user):
        return {"pin": True}

    def set_pin(self, user, pin):
        self.set_to = pin
        return True, ["alpha", "bravo", "charlie", "delta", "echo"]

    def renew_recovery(self, user):
        self.pin.renewed += 1
        return ["kilo", "lima", "mike", "november", "oscar"]


def shell(io, fallback, recovered=True):
    sh = InteractiveShell.__new__(InteractiveShell)
    sh._io = io
    sh.fallback = fallback
    sh.current_speaker_id = "augusta"
    sh.failure_note = None
    sh.recover_called = 0

    class _Store:
        def list_users(self):
            return ["augusta"]
    sh.biometric_store = _Store()

    async def recover():
        sh.recover_called += 1
        return recovered
    sh.menu_recover_pin = recover
    return sh


async def pin_checks():
    print("\nchange PIN")
    fb = _Fallback()
    sh = shell(_IO(["pin"], ["123456", "567890"]), fb)
    ok = await sh.menu_setup_pin()
    check("old PIN first, then the new one", ok and fb.set_to == "567890", (ok, fb.set_to))

    fb = _Fallback()
    io = _IO(["pin", "stop"], ["000000", "111111", "222222"])
    sh = shell(io, fb)
    ok = await sh.menu_setup_pin()
    check("wrong old PIN three times: nothing changed", not ok and fb.set_to is None)

    fb = _Fallback()
    sh = shell(_IO(["forgot"], []), fb)
    ok = await sh.menu_setup_pin()
    check("forgot: goes to the recovery words (which set the new PIN)",
          ok and sh.recover_called == 1 and fb.set_to is None)

    fb = _Fallback()
    sh = shell(_IO(["pin", "forgot"], ["000000", "111111", "222222"]), fb)
    ok = await sh.menu_setup_pin()
    check("wrong three times, then the words: recovery offered",
          ok and sh.recover_called == 1)

    sh = shell(_IO([], []), _Fallback())
    try:
        await sh.menu_setup_pin()
        cancelled = False
    except CancelledByUser:
        cancelled = True
    check("backing out of the first question is a cancel", cancelled)

    print("\nrecovery words")
    fb = _Fallback()
    io = _IO(["pin"], ["123456"])
    sh = shell(io, fb)
    ok = await sh.menu_new_recovery()
    shown = [t for t in io.out if str(t).startswith("RECOVERY PHRASE")]
    check("PIN, then five new words shown", ok and fb.pin.renewed == 1 and shown
          and "kilo" in shown[0], io.out)
    fb = _Fallback()
    io = _IO(["pin", "stop"], ["9", "9", "9"])
    sh = shell(io, fb)
    ok = await sh.menu_new_recovery()
    check("wrong PIN: no new words", not ok and fb.pin.renewed == 0)


def spoken_cancel_checks():
    print("\nspoken cancel (fixed words + meaning)")
    from nlu.spoken_cancel import means_cancel
    for said in ("cancel", "Stop.", "please stop", "go back", "never mind",
                 "ok cancel that now", "I didn't say that", "that's not what I said",
                 "No, no.", "Wait, sorry, cancel"):
        check(f"word list: {said!r} cancels", means_cancel(said), said)
    for said in ("don't cancel", "do not stop", "not cancel yet", "the kitchen",
                 "stop light", "yes", "", "turn off the fan"):
        check(f"not a cancel: {said!r}", not means_cancel(said), said)

    seen = []

    def fake(text):
        seen.append(text)
        return {"intent": "dismiss" if "misheard" in text else None}
    check("meaning layer: a sentence close to the examples cancels",
          means_cancel("you misheard me there", fake))
    check("meaning layer: other sentences do not",
          not means_cancel("the one in the kitchen", fake))
    seen.clear()
    means_cancel("go", fake)
    check("meaning layer: never asked about one or two words", seen == [], seen)
    check("negation beats meaning", not means_cancel("do not cancel it misheard", fake))

    def broken(_text):
        raise RuntimeError("no model")
    check("no model: only the word list counts", not means_cancel("you misheard me", broken)
          and means_cancel("stop", broken))


async def match_cancel_checks():
    print("\nmatch_request with cancel_check")
    h = handler()
    h._background = set()

    class _Clf:
        def classify(self, text, **_k):
            return {"intent": "dismiss" if "misheard" in text else None}

    class _Shell:
        intent_classifier = _Clf()
    h.shell = _Shell()
    opts = [{"value": "stop_light", "label": "Stop light"},
            {"value": "fan", "label": "Fan"}]
    await ConnectionHandler._dispatch_match_request(
        h, {"type": "match_request", "id": 1, "heard": "you misheard me",
            "options": opts, "cancel_check": True})
    await asyncio.sleep(0.2)
    got = [f for f in h.sent if f.get("id") == 1]
    check("a fluid cancel is reported, answered off the loop",
          got and got[0].get("cancel") is True and got[0]["values"] == [], got)
    await ConnectionHandler._dispatch_match_request(
        h, {"type": "match_request", "id": 2, "heard": "stop light",
            "options": opts, "cancel_check": True})
    await asyncio.sleep(0.2)
    got = [f for f in h.sent if f.get("id") == 2]
    check("an option named wins over the cancel word in it",
          got and got[0]["values"] == ["stop_light"] and not got[0].get("cancel"), got)
    await ConnectionHandler._dispatch_match_request(
        h, {"type": "match_request", "id": 3, "heard": "cancel",
            "options": [], "cancel_check": True})
    await asyncio.sleep(0.2)
    got = [f for f in h.sent if f.get("id") == 3]
    check("no list at all (the working screen): still answered",
          got and got[0].get("cancel") is True, got)


async def main():
    await cancel_checks()
    await pin_checks()
    spoken_cancel_checks()
    await match_cancel_checks()


if __name__ == "__main__":
    asyncio.run(main())
    print(f"\n{PASSED}/{PASSED + FAILED} checks passed")
    sys.exit(0 if FAILED == 0 else 1)

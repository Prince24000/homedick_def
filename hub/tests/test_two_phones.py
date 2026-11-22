"""Two phones at once (2026-10-11): one phone's question never makes the
other wait, neither sees the other's state, the shell lock is only for
setup, and each person's undo puts back only what they did.

Run: python3 tests/test_two_phones.py   (no models, no network)
"""
import asyncio
import json
import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("HOMEVOICE_HOME", tempfile.mkdtemp())
os.environ.setdefault("HOMEVOICE_ENV_FILE", "")

from app import ws_server as W                                # noqa: E402
from app.shell import InteractiveShell, CONVERSATION, NullIO  # noqa: E402
from nlu.device_manager import DeviceManager                  # noqa: E402

PASSED = FAILED = 0


def check(name, ok, detail=""):
    global PASSED, FAILED
    PASSED, FAILED = PASSED + bool(ok), FAILED + (not ok)
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}   {'' if ok else detail}")


class _Phones:
    OWNERS = {"phone-a": "augusta", "phone-b": "chidi"}

    def user_for(self, phone_id):
        return self.OWNERS.get(phone_id)


W.PHONES = _Phones()


class _Socket:
    remote_address = ("100.64.0.1", 5000)

    def __init__(self):
        self.frames = []

    async def send(self, raw):
        self.frames.append(json.loads(raw))


def make_shell():
    sh = InteractiveShell.__new__(InteractiveShell)
    tmp = tempfile.mktemp(suffix=".json")
    sh.dm = DeviceManager(path=tmp, embed_fn=lambda t: [0.0] * 8)
    sh.dm.add("lamp", name="lamp", room="hall", description="lamp")
    sh.dm.add("fan", name="fan", room="hall", description="fan")
    sh.sm = None
    sh.intent_classifier = None
    sh._io = NullIO()

    async def resolve():
        return None
    sh._resolve_current_speaker = resolve

    async def run_multi(text, speaker_id, audio=None):
        if text == "which one":
            # A QUESTION, through this phone's own socket, then an answer
            # that names whose it was.
            sh.failure_note = f"{speaker_id} is asking"
            ans = await sh._io.ainput("Which one?")
            sh.failure_note = f"{speaker_id} answered {ans}"
            return False
        sh.failure_note = f"{speaker_id} did {text}"
        return False
    sh._run_multi = run_multi
    return sh


def phone(sh, lock, pid):
    h = W.ConnectionHandler(_Socket(), sh, lock)
    h._phone_id = pid
    return h


async def as_phone(h, coro_fn):
    """Run as this phone's connection does: its Conversation current."""
    async def body():
        CONVERSATION.set(h._conv)
        return await coro_fn()
    return await asyncio.ensure_future(body())


async def send_command(h, text):
    async def go():
        task = h._spawn_flow(h._handle_command({"type": "command", "text": text}))
        return task
    return await as_phone(h, go)


def results(h):
    return [f for f in h.websocket.frames if f.get("type") == "result"]


async def side_by_side():
    print("\none phone's question does not hold up the other")
    sh = make_shell()
    lock = asyncio.Lock()
    a, b = phone(sh, lock, "phone-a"), phone(sh, lock, "phone-b")

    ta = await send_command(a, "which one")
    for _ in range(50):
        await asyncio.sleep(0.01)
        if any(f.get("type") == "prompt" for f in a.websocket.frames):
            break
    check("phone A is waiting on its question",
          any(f.get("type") == "prompt" for f in a.websocket.frames),
          a.websocket.frames)

    t0 = time.perf_counter()
    tb = await send_command(b, "lamp on")
    await asyncio.wait_for(tb, 2.0)
    took = time.perf_counter() - t0
    check("phone B's command finished while A was still asked",
          not ta.done() and results(b), (ta.done(), b.websocket.frames))
    check(f"...with no wait for its turn ({took * 1000:.0f} ms)", took < 0.5, took)
    check("phone B got its OWN reason, not A's",
          results(b) and results(b)[-1].get("text") == "chidi did lamp on",
          results(b))
    check("phone B never saw A's question",
          not any(f.get("type") == "prompt" for f in b.websocket.frames),
          b.websocket.frames)
    check("no shell lock was taken by either command", not lock.locked())

    a._active_command_queue.put_nowait({"type": "prompt_reply", "value": "the fan"})
    await asyncio.wait_for(ta, 2.0)
    check("phone A's answer finished A's command, in A's words",
          results(a) and results(a)[-1].get("text") == "augusta answered the fan",
          results(a))
    check("the shell's own conversation was never touched",
          sh._home_conversation().failure_note is None
          and isinstance(sh._home_conversation()._io, NullIO))

    print("\nwhat runs in a worker thread is still this phone's")

    async def in_thread():
        sh.failure_note = "set on A"
        return await asyncio.to_thread(lambda: sh.failure_note)
    got = await as_phone(a, in_thread)
    check("asyncio.to_thread sees the phone's conversation", got == "set on A", got)

    async def read():
        return sh.failure_note
    check("...and phone B does not", await as_phone(b, read) != "set on A")


async def setup_takes_turns():
    print("\nthe shell lock is for setup, not for using the house")
    sh = make_shell()
    lock = asyncio.Lock()
    ran = []

    async def menu(action, speaker_id):
        ran.append((action, speaker_id))
        return True
    sh.run_menu_action = menu
    b = phone(sh, lock, "phone-b")
    W.ConnectionHandler.LOCK_ACQUIRE_TIMEOUT = 0.2

    await lock.acquire()           # another phone is in the middle of Add device
    try:
        t = await send_command(b, "fan on")
        await asyncio.wait_for(t, 2.0)
        check("a command runs while a setup flow holds the lock",
              results(b) and results(b)[-1].get("text") == "chidi did fan on",
              results(b))

        async def go(action):
            return b._spawn_flow(b._handle_menu(action))
        t = await as_phone(b, lambda: go("profile"))
        await asyncio.wait_for(t, 2.0)
        check("a read-only door (Profile) does not wait either",
              ("profile", "chidi") in ran, ran)
        t = await as_phone(b, lambda: go("viewcam:cam1"))
        await asyncio.wait_for(t, 2.0)
        check("nor does opening a camera", ("viewcam:cam1", "chidi") in ran, ran)

        t = await as_phone(b, lambda: go("add_device"))
        await asyncio.wait_for(t, 2.0)
        check("a second setup flow still takes its turn (busy, not beside it)",
              ("add_device", "chidi") not in ran
              and any(f.get("reason") == "server_busy" for f in b.websocket.frames),
              (ran, b.websocket.frames[-1:]))
    finally:
        lock.release()
    check("read-only doors are a short, fixed list",
          "add_device" not in W.LOCK_FREE_ACTIONS
          and "check_notices" not in W.LOCK_FREE_ACTIONS)


def _ok(did, changes):
    """A board that takes what it is sent."""
    return True


def undo_is_per_person():
    print("\neach person's undo puts back only what they did")
    tmp = tempfile.mktemp(suffix=".json")
    dm = DeviceManager(path=tmp, embed_fn=lambda t: [0.0] * 8)
    for did in ("lamp", "fan", "heater"):
        dm.add(did, name=did, room="hall", description=did, transport="mqtt")
        dm.set_state(did, _driver=_ok, on=False)

    async def as_person(who, *dids):
        dm.snapshot(who)
        for did in dids:
            # Through a worker thread, as the shell does.
            await asyncio.to_thread(lambda: dm.set_state(did, _driver=_ok, on=True))

    async def scene():
        await asyncio.ensure_future(as_person("augusta", "lamp"))
        await asyncio.ensure_future(as_person("chidi", "fan"))
    asyncio.run(scene())
    on = lambda did: dm.devices[did]["state"].get("on")       # noqa: E731
    check("both commands happened", on("lamp") and on("fan"))

    back = dm.undo(owner="augusta")
    check("Augusta's undo puts back her lamp only",
          [d for d, _ in back] == ["lamp"] and on("fan"), back)
    back = dm.undo(owner="chidi")
    check("Chidi's undo puts back his fan", [d for d, _ in back] == ["fan"], back)

    async def overlap():
        dm.snapshot("augusta")
        await asyncio.to_thread(lambda: dm.set_state("heater", _driver=_ok, on=True))
        dm.snapshot("chidi")
        await asyncio.to_thread(lambda: dm.set_state("heater", _driver=_ok, on=False))
    dm.set_state("heater", _driver=_ok, on=False)

    async def run(coro_fn):
        await asyncio.ensure_future(coro_fn())
    asyncio.run(run(overlap))
    back = dm.undo(owner="augusta")
    check("a device somebody changed AFTER you is theirs to undo, not yours",
          back == [] and on("heater") is False, back)
    back = dm.undo(owner="chidi")
    check("...and their undo puts it back to how you left it",
          [d for d, _ in back] == ["heater"] and on("heater") is True, back)

    dm.snapshot()
    dm.devices["lamp"]["state"]["on"] = True
    check("the terminal (nobody) still undoes as before",
          [d for d, _ in dm.undo()] == ["lamp"] and on("lamp") is False)


async def quiet_beside_command():
    print("\nthe on-connect check runs beside this phone's own command")
    sh = make_shell()
    lock = asyncio.Lock()
    a = phone(sh, lock, "phone-a")
    release = asyncio.Event()
    seen = {}

    async def menu(action, speaker_id):
        sh.failure_note = "notices note"
        await release.wait()          # still running when the command ends
        seen["note"] = sh.failure_note
        return True
    sh.run_menu_action = menu

    async def go():
        return a._spawn_flow(a._handle_menu("check_notices"), quiet=True)
    quiet = await as_phone(a, go)
    await asyncio.sleep(0.05)
    t = await send_command(a, "lamp on")
    await asyncio.wait_for(t, 2.0)
    check("the command did not wait for it",
          results(a) and results(a)[-1].get("text") == "augusta did lamp on",
          results(a))
    release.set()
    await asyncio.wait_for(quiet, 2.0)
    check("...and neither blanked the other's state",
          seen.get("note") == "notices note", seen)


async def main():
    await side_by_side()
    await setup_takes_turns()
    await quiet_beside_command()


asyncio.run(main())
undo_is_per_person()
print(f"\n  {PASSED} passed, {FAILED} failed")
sys.exit(1 if FAILED else 0)

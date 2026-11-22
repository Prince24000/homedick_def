"""Learning and replaying infrared, as a conversation.

WHY THIS IS NOT IN main.py: it was, and it was 300 lines about remote
controls in a file that otherwise runs a voice assistant. Same reasoning
that moved the config flows out to ha_flows.py.

WHY IT LIVES HERE AND NOT IN home_assistant/: it was there first, beside
ha_flows.py, on the grounds that both are FLOWS. But ha_flows earns that
folder -- board registration genuinely cannot happen without Home
Assistant, because it waits for entities to appear there and matches them.
This file's PRIMARY route never mentions Home Assistant at all: a TSOP on
one of this hub's own boards, Tasmota decoding it, MQTT carrying it. Its
transport is `esp_builder.wait_for_ir` and `esp_builder.send_ir`, both in
this folder, so the flow now sits with the machinery it drives -- the same
logic that keeps ha_flows next to ha_client.

The OpenIRBlaster route below IS Home Assistant work and is therefore a
guest here. That is a smaller wart than one feature split across two
folders with the dispatch in a third.

TWO ROUTES TO THE SAME PLACE, chosen by what is actually wired:

    a board with an IR receiver     -> Tasmota decodes, MQTT carries it,
                                       codes land in this hub's registry
    an OpenIRBlaster in HA          -> the integration learns, each code
                                       becomes a button.* entity

The first is preferred and the reason is not taste. It is hardware this hub
provisioned, its codes live in devices.json where everything else this hub
knows lives -- backed up with it, migrated with it, retired with it -- and
no second integration has to be installed or kept working. The
OpenIRBlaster route is kept intact for a house that already has one.

WHAT AN IR DEVICE IS, in this system: ONE APPLIANCE, one record, its buttons
in `ir_codes`. Not one device per button. "Pause the telly" resolves the
DEVICE semantically the ordinary way and then looks up the action -- where
fifteen devices called "tv something" would need semantic matching to
separate "tv play" from "tv pause", which is exactly the collision that
makes embeddings the wrong tool for transport controls.

It registers with NO Home Assistant entity, because an infrared press is an
EVENT and the appliance it commands reports nothing back. That was
impossible until BoardMatch.local_only existed; before it, a thing with no
entity was skipped entirely and became invisible everywhere but the pin
screen.
"""

import asyncio
import json
import re
from typing import Any, Dict, Iterable, List, Optional, Tuple

from core.hv_common import event, CancelledByUser, means_cancel
from core.config import IR_LEARN_TIMEOUT, OPENIRBLASTER_ENTRY_ID

# describe_ir_appliance loads its model lazily and falls back to
# ir_fallback_description (which names every button) when there is none.
from nlu.llm_describer import describe_ir_appliance, ir_fallback_description


# ── a stored code, sent and recognised (shared by the shell and the engine) ──

#: A receiver hearing a code this soon after the hub sent it is hearing the
#: hub, not a person (seconds).
IR_ECHO_S = 1.0

OVERFLOW_SAID = ("That signal was too long to capture -- hold the remote "
                 "closer and press once.")
REPEAT_SAID = ("That was the remote's repeat signal, not a button -- press "
               "the button once, briefly.")
UNREADABLE_SAID = ("That raw signal could not be read safely -- press the "
                   "button once more.")


def _compact_raw(text: str, shared: bool) -> Optional[List[int]]:
    """Tasmota's compact raw string ("+8570-4240+550-1580C-510...") read one
    way: `shared` -- one table of values in the order they first appear;
    otherwise marks and spaces each keep their own. Upper case is a mark,
    lower case a space. None when it does not read cleanly that way."""
    tokens = re.findall(r"[+-]\d+|[A-Za-z]", text)
    if "".join(tokens) != text:
        return None
    table: List[Tuple[int, bool]] = []
    out: List[int] = []
    for tok in tokens:
        mark = len(out) % 2 == 0                # marks and spaces alternate
        if tok[0] in "+-":
            if (tok[0] == "+") != mark:
                return None
            value = int(tok[1:])
            table.append((value, mark))
        else:
            if tok.isupper() != mark:
                return None
            pool = table if shared else [t for t in table if t[1] == mark]
            i = ord(tok.upper()) - ord("A")
            if i >= len(pool) or pool[i][1] != mark:
                return None
            value = pool[i][0]
        out.append(value)
    return out


def parse_raw_data(raw: Any) -> Optional[List[int]]:
    """Raw timings as a list of microseconds, from a list or from a string
    (comma-separated, or Tasmota's compact form). None when they cannot be
    read without guessing."""
    if isinstance(raw, (list, tuple)):
        try:
            out = [int(v) for v in raw if not isinstance(v, bool)]
        except (TypeError, ValueError):
            return None
        if len(out) != len(raw):
            return None
    elif isinstance(raw, str):
        text = re.sub(r"\s+", "", raw)
        if re.fullmatch(r"\d+(,\d+)*", text):
            out = [int(v) for v in text.split(",")]
        elif text.startswith("+"):
            # Read both ways; used only when exactly one reading holds.
            readings = {tuple(r) for r in (_compact_raw(text, True),
                                           _compact_raw(text, False)) if r}
            if len(readings) != 1:
                return None
            out = list(readings.pop())
        else:
            return None
    else:
        return None
    if len(out) < 4 or any(v <= 0 or v > 65535 for v in out):
        return None
    return out


def _decoded(code: Dict[str, Any]) -> bool:
    protocol = str(code.get("Protocol") or "").strip().upper()
    return bool(code.get("Data")) and protocol not in ("", "UNKNOWN")


def ir_command_body(code: Any) -> Optional[str]:
    """What follows "IRsend " for a stored code. Decoded: Tasmota's JSON
    (Protocol/Bits/Data). Raw: "0,<t1>,<t2>,..." -- the frequency first, 0
    meaning the default 38 kHz. Old raw codes ({"RawData": [...]}) read the
    same. None when the code cannot be sent."""
    if not isinstance(code, dict):
        return None
    if _decoded(code):
        return json.dumps({k: v for k, v in code.items()
                           if k not in ("RawData", "RawDataInfo", "Hash",
                                        "Repeat")})
    timings = parse_raw_data(code.get("RawData"))
    if timings is None:
        return None
    return "0," + ",".join(str(t) for t in timings)


def is_repeat_frame(heard: Dict[str, Any]) -> bool:
    """A remote's "still held" frame (NEC repeat): a Repeat flag, Bits 0 on
    a named protocol, or Data all F's. Not a button."""
    if heard.get("Repeat") not in (None, 0, "0", False, ""):
        return True
    if _decoded(heard) and str(heard.get("Bits")).strip() == "0":
        return True
    data = str(heard.get("Data") or "").strip().upper()
    digits = data[2:] if data.startswith("0X") else data
    return bool(digits) and set(digits) == {"F"}


def ir_overflowed(heard: Dict[str, Any]) -> bool:
    """Whether RawDataInfo says the capture ran out of room ([n, n, overflow])."""
    info = heard.get("RawDataInfo")
    if isinstance(info, (list, tuple)) and len(info) >= 3:
        flag = info[2]
    elif isinstance(info, dict):
        flag = info.get("Overflow")
    else:
        return False
    return flag not in (None, 0, "0", False, "")


def ir_signature(code: Any) -> Tuple[set, Optional[List[int]]]:
    """What identifies a code, sent or heard: its Data / Hash, and its raw
    timings when it has them."""
    if not isinstance(code, dict):
        return set(), None
    keys = set()
    if code.get("Data") and not is_repeat_frame(code):
        keys.add("data:" + str(code["Data"]).strip().upper())
    if code.get("Hash"):
        keys.add("hash:" + str(code["Hash"]).strip().upper())
    return keys, parse_raw_data(code.get("RawData"))


def ir_raw_close(a: List[int], b: List[int]) -> bool:
    """Two raw captures of one signal: about as long, and nearly every timing
    within a quarter (or 150 us) of the other."""
    if abs(len(a) - len(b)) > 2:
        return False
    pairs = list(zip(a, b))
    near = sum(1 for x, y in pairs if abs(x - y) <= max(150, 0.25 * max(x, y)))
    return near >= 0.9 * len(pairs)


def ir_board_room(dm: Any, esp: Any, slug: str) -> str:
    """A board's room, casefolded: its record's room, else the room most of
    its own (non-infrared) devices are in. "" when unknown."""
    record = esp._read_board(slug) or {}
    room = str(record.get("room") or "").strip()
    if room:
        return room.casefold()
    count: Dict[str, int] = {}
    for did, d in list((getattr(dm, "devices", None) or {}).items()):
        if str(did).startswith(f"{slug}.") and isinstance(d, dict) \
                and d.get("transport") != "ir":
            here = str(d.get("room") or "").strip().casefold()
            if here:
                count[here] = count.get(here, 0) + 1
    return max(count, key=count.get) if count else ""


def ir_senders(dm: Any, esp: Any, did: str,
               emitters: Iterable[str]) -> Tuple[List[str], str]:
    """Which blasters may send for one appliance, in order, and its room.
    Its own board first, then only boards in the same room. With no room on
    file there is no room to be wrong about, so any blaster may send."""
    emitters = list(emitters)
    d = (getattr(dm, "devices", None) or {}).get(did) or {}
    room = str(d.get("room") or "").strip()
    home = did.split(".", 1)[0] if "." in did else ""
    order = [home] if home in emitters else []
    for slug in emitters:
        if slug != home and (not room or ir_board_room(dm, esp, slug)
                             == room.casefold()):
            order.append(slug)
    return order, room


def ir_not_sent(name: str, button: str, room: str, any_board: bool) -> str:
    """Why a button was not sent, in one sentence."""
    if room and not any_board:
        why = f"No infrared board is in the {room}"
    elif room:
        why = f"No infrared board in the {room} is online"
    else:
        why = "No infrared board is online"
    return f"{why} -- {name} was not sent '{button.replace('_', ' ')}'."


class IRFlowRunner:
    """Runs the infrared flows on behalf of the shell.

    Holds the shell rather than copies of its parts, for the same reason
    ConfigFlowRunner does: `_io` is swapped for a WebSocketIO on every
    connection and `ha_client` is rebuilt on reconnect, so a snapshot taken
    at construction would be pointing at a dead object by the second
    command.
    """

    def __init__(self, shell):
        self._shell = shell

    @property
    def _io(self):
        return self._shell._io

    @property
    def ha_client(self):
        return self._shell.ha_client

    async def menu_learn_ir(self) -> bool:
        """Capture IR codes from a remote, one button at a time.

        TWO ROUTES, CHOSEN BY WHAT IS ACTUALLY WIRED. A board carrying an
        IR receiver (catalogue `ir_recv`) is preferred: it is hardware this
        hub provisioned, its codes land in this hub's own registry, and no
        second integration is involved. OpenIRBlaster remains the fallback
        for a house that has one, unchanged.

        The board is the referee here as everywhere -- the choice is made by
        reading the board records, not by a setting somebody has to keep
        true.
        """
        local = self._shell.esp.boards_with("ir_recv")
        if local:
            return await self._learn_ir_local(local)
        if not OPENIRBLASTER_ENTRY_ID:
            # SAY WHY, with the way back in. "Can't access Learn IR" on
            # the bench (2026-08-25) was this exact hole: the one board
            # carrying the receiver had been retired, and the flow fell
            # through to a blaster that does not exist, refusing with a
            # sentence about hardware the person never had.
            await self._io.write("\n--- LEARN IR CODES ---")
            await self._io.write(
                "  No board carries an infrared receiver right now, so "
                "there is nothing to listen with. Codes learned earlier "
                "are still saved -- but hearing a remote needs the "
                "hardware back: Add device -> pick a board -> 'Infrared "
                "receiver (TSOP38xx)' on one free pin. If the receiver "
                "left when a board was retired, that is where it went.",
                level="warn")
            # The popup states the CAUSE, not the outcome (owner's spec,
            # 2026-08-26): failure_note is carried as the failed result's
            # text, so the app never says "did not complete" for a refusal
            # that knows exactly why.
            self._shell.failure_note = (
                "The IR receiver isn't registered, so there's nothing to "
                "listen with. Your saved codes are safe -- Add device -> "
                "'Infrared receiver (TSOP38xx)' brings it back.")
            return False
        return await self._learn_ir_openirblaster()

    async def _pick_receiver(self, receivers: List[str],
                             appliance: str) -> Optional[str]:
        """The receiver board to learn with: one that is online and took
        SetOption58 1 (raw timings reported, not only what Tasmota decoded).
        The appliance's own board, or one in its room, is tried first when the
        appliance is already known; otherwise the first online one."""
        dm, esp = self._shell.dm, self._shell.esp
        homes, rooms = set(), set()
        for did, d in list((dm.devices or {}).items()):
            if isinstance(d, dict) and d.get("transport") == "ir" and \
                    str(d.get("name") or "").strip().lower() == appliance:
                homes.add(str(did).split(".", 1)[0])
                rooms.add(str(d.get("room") or "").strip().casefold())
        rooms.discard("")
        first = [s for s in receivers
                 if s in homes or (rooms and ir_board_room(dm, esp, s) in rooms)]
        for slug in first + [s for s in receivers if s not in first]:
            record = esp._read_board(slug)
            # _publish reads the board's last will first: False when offline.
            if record and await esp._publish(record, ["SetOption58 1"]):
                return slug
        return None

    async def _learn_ir_local(self, receivers: List[str]) -> bool:
        """Learn from a TSOP on one of this hub's own boards.

        WHAT THIS PRODUCES IS A DEVICE, not a pile of codes. One appliance,
        one record, its buttons in `ir_codes` -- a field the registry has
        accepted since long before anything wrote to it. "Pause the telly"
        then resolves the DEVICE the ordinary way and looks up the action,
        rather than needing a separate device per button.

        No Home Assistant entity is involved and none is wanted: an infrared
        press is an event, and the appliance it controls has no state the
        hub can read back. It registers entity-less, exactly like the
        blaster -- see BoardMatch.local_only.

        NOTHING IS REFUSED FOR BEING UNRECOGNISED. An earlier version stored
        only codes Tasmota could name a protocol for, on the grounds that a
        raw capture is fragile. That was wrong in a way worth writing down:
        the hub never interprets these bytes, it only replays them, so a
        protocol nobody has heard of is no harder to send than NEC. And for
        an air-conditioner it is the ONLY option -- those remotes transmit
        their whole state in one long packet rather than one code per
        button, so "cool, 22 degrees, medium fan" is a thing you capture,
        not a thing you compose. Refusing raw made that impossible in
        service of tidiness.
        """
        await self._io.write("\n--- LEARN IR CODES ---")
        # Checked before anything is asked: a receiver board must be online.
        downs = [self._shell._board_down(s) for s in receivers]
        if downs and all(downs):
            said = downs[0] if len(downs) == 1 else (
                "No infrared receiver is online -- power one, then learn "
                "again.")
            await self._io.write(f"  {said}", level="warn")
            self._shell.failure_note = said
            return False

        try:
            appliance = (await self._io.get_input(
                "  What is being controlled? (e.g. 'tv'): ")).strip().lower()
        except CancelledByUser:
            await self._io.write("  (Cancelled.)")
            return False
        if not re.fullmatch(r"[a-z0-9 _-]{1,32}", appliance):
            await self._io.write(
                "  Letters, digits, spaces, hyphens and underscores only.",
                level="warn")
            return False

        slug = await self._pick_receiver(list(receivers), appliance)
        if slug is None:
            said = "No infrared receiver is online -- power one, then learn again."
            await self._io.write(f"  {said}", level="warn")
            self._shell.failure_note = said
            return False
        record = self._shell.esp._read_board(slug) or {}
        board_name = record.get("name", slug)
        await self._io.write(
            f"  Using the infrared receiver on {board_name}.")

        did = self._shell.dm.board_device_id(slug, appliance)
        record_now = self._shell.dm.devices.get(did) or {}
        if record_now and record_now.get("transport") != "ir":
            # The id is the board's slug plus the name, so a relay called
            # "tv" on this board IS this id -- and would have been handed
            # ir_codes and, with no description of its own, a remote's.
            await self._io.write(
                f"  {board_name} already has a device called "
                f"{record_now.get('name', appliance)!r}. Pick another name "
                f"for the remote.", level="warn")
            return False
        existing = dict(record_now.get("ir_codes") or {})
        if existing:
            await self._io.write(
                f"  {appliance} already knows: "
                f"{', '.join(sorted(existing))}. Learning a name again "
                f"REPLACES that one and leaves the rest alone.")

        await self._io.write(
            f"  Name a button, press it on the remote, then keep or redo "
            f"what was heard.\n  {IR_LEARN_TIMEOUT}s per press. Answer with "
            f"nothing (or 'done') to finish.")

        codes = dict(existing)
        learned = 0
        done = False
        # The app's Cancel now RAISES out of ainput. Buttons already told
        # "Saved" must not vanish with it: they are written below, without
        # asking anything more, and the cancel is re-raised after.
        cancelled = False
        while True:
            try:
                action = (await self._io.ainput(
                    "\n  Button name (power / play / pause / cool_22), "
                    "or nothing to finish: ")).strip().lower()
            except (EOFError, KeyboardInterrupt):
                break
            except CancelledByUser:
                if not learned:
                    raise
                cancelled = True
                break
            if not action or action == "done" or means_cancel(action):
                break
            if not re.fullmatch(r"[a-z0-9_]{1,32}", action):
                await self._io.write(
                    "  Lowercase letters, digits and underscores only.",
                    level="warn")
                continue

            # PRESS, LOOK, DECIDE -- and stay on this button until the
            # person is happy. Auto-saving whatever arrived first meant a
            # mis-press became a stored button you would only discover was
            # wrong when the house did the wrong thing.
            while True:
                await self._io.write(
                    f"  Point the remote at {board_name} and press "
                    f"{action.upper()} now...")
                event("IR", f"listening for {appliance!r}.{action!r} "
                            f"on {slug!r} ({IR_LEARN_TIMEOUT}s)")
                # THE OVERLAY, because this is the one step where the hub
                # goes quiet while a PERSON is expected to act. Without it
                # the app shows "press POWER now" and then nothing moves for
                # the whole timeout, which reads as a hung flow rather than
                # as listening. Bounded bar: the wait has a known length.
                await self._io.send_data("busy", {
                    "text": f"Listening for {action.upper()}",
                    "detail": f"Point the remote at {board_name} and press "
                              f"it now.",
                    "seconds": IR_LEARN_TIMEOUT,
                })
                try:
                    heard = await self._shell.esp.wait_for_ir(
                        slug, float(IR_LEARN_TIMEOUT))
                finally:
                    # ALWAYS cleared, including when the wait raises. A busy
                    # overlay left up over a question nobody can reach is
                    # the worst of both.
                    await self._io.send_data("busy", {"done": True})
                code, described = self._ir_code_from(heard)
                if code is None:
                    # WAS IT THE ROOM, OR WAS IT US? (2026-09-12) "Nothing
                    # heard. Check the receiver is pointed at" was said
                    # whatever went wrong -- including a broker that refused
                    # the hub's login and a TLS setting with no certificate
                    # behind it, both of which _mqtt_listen knew and threw
                    # away. A person can act on "the broker refused the hub
                    # login"; nobody can act on being told to aim the remote
                    # better at a receiver nothing was listening to.
                    why = self._shell.esp.listen_problem()
                    if why:
                        described = (f"Nothing could be heard, and the "
                                     f"remote is not the reason: {why}.")
                    await self._io.write(f"  {described}", level="warn")
                else:
                    await self._io.write(f"  Heard: {described}")

                # THREE ANSWERS, AND NO "SKIP". Skipping one button only ever
                # returned to the name prompt, which is reachable by saving
                # or by finishing anyway -- so it was a third door into the
                # same corridor. Finish ends the session outright, which is
                # what somebody actually wants after their last button.
                options = []
                if code is not None:
                    options.append({"value": "keep", "label": "Save it",
                                    "sub": described})
                options.append({"value": "again", "label": "Try again",
                                "sub": f"press {action.upper()} once more"})
                options.append(
                    {"value": "finish", "label": "Finish",
                     # SAVE MEANS SAVE, FINISH MEANS STOP. Finishing does
                     # not quietly keep the press being looked at -- that
                     # would make the Save button decorative and leave
                     # somebody unsure which of their two taps did the work.
                     "sub": "stop learning"
                            + (" without saving this one"
                               if code is not None else "")})
                picked = await self._io.choose(
                    f"{appliance} / {action}", options)
                choice = picked[0] if picked else "finish"

                if choice == "again":
                    continue
                if choice == "keep" and code is not None:
                    codes[action] = code
                    learned += 1
                    await self._io.write(f"  Saved {action}.")
                    break
                # finish, or a cancelled picker
                done = True
                break
            if done:
                break

        # Nothing NEW is nothing to save: rewriting the record, asking for
        # a hint and re-describing it would all be for buttons already on
        # file, and the flow would report success for a session that
        # stored nothing.
        if not learned:
            await self._io.write(
                f"  Nothing new learned -- {appliance} keeps its "
                f"{len(existing)} button(s)." if existing
                else "  Nothing learned.")
            return False

        # THE DESCRIPTION IS THE SEMANTIC SURFACE -- the only input voice
        # matching ever sees -- so for an IR appliance it must speak in the
        # BUTTON vocabulary: "pause the tv" can only find a device whose
        # description talks about pausing. The hint is the person's chance
        # to add what the button names cannot carry ("the living room telly,
        # the big one"), and the model, when present, turns names and hint
        # into the phrases people actually say. Without a model the fallback
        # still lists every button by name -- literal words are exactly what
        # the TF-IDF matcher can use.
        hint = ""
        try:
            if not cancelled:
                hint = (await self._io.ainput(
                    "  Anything worth knowing about it? (helps voice "
                    "matching, Enter to skip): ")).strip()
        except (EOFError, KeyboardInterrupt):
            pass
        except CancelledByUser:
            cancelled = True
        if means_cancel(hint):
            hint = ""

        description = ir_fallback_description(appliance, sorted(codes), hint)
        if not cancelled:
            await self._io.send_data("busy", {
                "text": "Writing its description",
                "detail": "The AI is phrasing the buttons the way people "
                          "say them.",
                "seconds": 30,
            })
            try:
                described = await asyncio.to_thread(
                    describe_ir_appliance, appliance, sorted(codes), hint)
                if described:
                    description = described
            except Exception as e:                     # noqa: BLE001
                await self._io.write(f"  AI description failed ({e}) -- "
                                     f"using the plain one.", level="warn")
            finally:
                await self._io.send_data("busy", {"done": True})
            await self._io.write(f"  Description: {description}")

        # ONE RECORD, WRITTEN ONCE at the end. Writing per button would leave
        # a half-learned remote on disk if the person walked away mid-loop --
        # and a device that answers to "pause" and not "play" is worse than
        # one that does not exist, because it looks finished.
        if did in self._shell.dm.devices:
            # THE DESCRIPTION FOLLOWS THE BUTTONS -- on a machine-written
            # description only. One this flow wrote always carries its
            # "Buttons:" tail or came from the model over the same names, and
            # must grow when the vocabulary grows, or "mute the tv" stays
            # unmatchable after mute was learned. But a description a PERSON
            # typed in Update device is theirs; overwriting it because they
            # added a button would be the hub deciding it words things
            # better. Their words, their call -- they can rerun Update
            # device any time.
            current = str((self._shell.dm.devices.get(did) or {})
                          .get("description") or "")
            machine_made = ("Buttons:" in current or "Also:" in current
                            or not current)
            if machine_made:
                self._shell.dm.update(did, ir_codes=codes,
                                      description=description)
            else:
                self._shell.dm.update(did, ir_codes=codes)
                await self._io.write(
                    "  (Your own description was kept. Update device "
                    "rewrites it if you want the new buttons in it.)")
            await self._io.write(
                f"\n  {appliance} updated -- {len(codes)} button(s) on file.")
        else:
            self._shell.dm.add(
                did,
                name=appliance,
                room=board_name,
                description=description,
                state={"on": False},
                state_key="on",
                secure=False,
                transport="ir",
                ir_codes=codes,
                ha_entity_id=None,
            )
            await self._io.write(
                f"\n  {appliance} registered with {len(codes)} button(s).")
        if cancelled:
            raise CancelledByUser()

        emitters = self._shell.esp.boards_with("ir_send")
        if not emitters:
            await self._io.write(
                "  NOTE: no board has an infrared LED yet, so these codes "
                "can be stored but not sent. Add an 'Infrared blaster' to a "
                "board and they start working -- nothing here needs "
                "relearning.", level="warn")
        return True

    @staticmethod
    def _ir_code_from(heard: Optional[Dict[str, Any]]) -> tuple:
        """Turn what the receiver reported into something sendable.

        Returns (payload or None, one line describing it). TWO SHAPES are
        accepted and they are not ranked by quality, only by size:

        DECODED -- Tasmota named the protocol, so the whole press is a few
        bytes. Sends from any board, survives being written down.

        RAW -- Tasmota heard it and could not name it, so what survives is
        the timing of every pulse. Bulkier and captured from one distance
        and angle, but a perfectly valid thing to replay, and the only way
        an air-conditioner is ever going to work here.

        The hub never interprets either one. It stores what the board said
        and hands it back verbatim, which is why an unknown protocol costs
        nothing to support.
        """
        if not isinstance(heard, dict):
            return None, ("Nothing heard. Check the receiver is pointed at, "
                          "and try that button again.")
        if ir_overflowed(heard):
            return None, OVERFLOW_SAID
        if is_repeat_frame(heard):
            return None, REPEAT_SAID
        data = heard.get("Data")
        protocol = str(heard.get("Protocol") or "")
        if data and protocol and protocol.upper() != "UNKNOWN":
            return ({"Protocol": protocol, "Bits": heard.get("Bits"),
                     "Data": data},
                    f"{protocol} {data}")

        raw = heard.get("RawData")
        if raw:
            # A list, or a string if the build sends one; stored as a list.
            timings = parse_raw_data(raw)
            if timings is None:
                return None, UNREADABLE_SAID
            code: Dict[str, Any] = {"RawData": timings}
            if heard.get("Hash"):
                # Lets the hub recognise its own send heard back (no timings).
                code["Hash"] = str(heard["Hash"])
            return (code,
                    f"an unrecognised remote, captured raw "
                    f"({len(timings)} pulses)")

        return None, ("Heard something, but there was nothing usable in it "
                      "-- no protocol and no raw timings. Try again, closer.")

    async def _learn_ir_openirblaster(self) -> bool:
        """The original route: a separate OpenIRBlaster device driven through
        Home Assistant. Unchanged, and reached only when no board of this
        hub's own carries a receiver.

        This is the flow the scrapped ir_control.py had and Home Assistant's
        own UI does not: the person is prompted for each action IN THIS APP,
        presses the corresponding key on their remote, and is told immediately
        whether it was captured.

        It works because openirblaster.learn_start BLOCKS. Given a response
        variable it arms the receiver, waits, and reports back
        {"status": "received"|"timeout"|"cancelled", ...} -- so this can be a
        straight prompt-and-confirm loop rather than firing a request and then
        polling the entity registry hoping a button turned up.

        Each captured code becomes a button.* entity, which is then an ordinary
        device: register it from the picker like a relay. Nothing downstream
        knows or cares that it came from an IR remote.
        """
        await self._io.write("\n--- LEARN IR CODES ---")
        # OPENIRBLASTER_ENTRY_ID is set: menu_learn_ir is the only caller
        # and answers the unset case itself.
        if self.ha_client is None:
            await self._io.write("Home Assistant is not connected.", level="warn")
            return False

        try:
            prefix = (await self._io.get_input(
                "  Name for this remote (e.g. 'tv'): ")).strip().lower()
        except CancelledByUser:
            await self._io.write("(Cancelled.)")
            return False
        # get_input already refuses an empty answer and handles 'cancel', so
        # the old `if not prefix` branch below it was unreachable. What it
        # could not check is the CHARACTER SET: this string becomes part of
        # an entity id, and a space or slash there produces an id that needs
        # quoting everywhere it is used afterwards.
        if not re.fullmatch(r"[a-z0-9_]{1,24}", prefix):
            await self._io.write(
                "  Use lowercase letters, digits and underscores only "
                "(it becomes part of the entity id).", level="warn")
            return False

        await self._io.write(
            f"  For each action: press the matching key on your remote when asked.\n"
            f"  You have {IR_LEARN_TIMEOUT}s per code. "
            f"Answer with nothing (or 'done') to finish.")

        try:
            learned = await self._learn_ir_loop(prefix)
        except CancelledByUser:
            # Codes already SAVED stay saved -- they are in Home Assistant,
            # not in a buffer this method owns. Say so rather than implying
            # the whole session was lost.
            await self._io.write(
                "  (Stopped. Any codes already saved are kept.)")
            return False
        if not learned:
            await self._io.write("  Nothing learned.")
            return False
        await self._io.write(
            f"\n  Learned {learned} code(s). Each is now a button in Home "
            f"Assistant -- add them with 'Add device' and pick them from the "
            f"entity list.")
        return True

    async def _learn_ir_loop(self, prefix: str) -> int:
        """One capture per iteration; returns how many were saved.

        Split out of menu_learn_ir so the cancel path has ONE handler and
        the count survives it -- the loop used to be inline with its
        exit condition unreachable (see the ainput note below).
        """
        learned = 0
        while True:
            # ainput, NOT get_input. get_input REJECTS an empty answer and
            # re-asks forever, so "enter a blank name to finish" described a
            # gesture this loop could not accept: the only way out was
            # cancel, which reads as abandoning the whole session. Same bug
            # class as the keepsafe "press Enter" prompt.
            try:
                action = (await self._io.ainput(
                    "\n  Action to learn (play / pause / volume_up), "
                    "or nothing to finish: ")).strip().lower()
            except (EOFError, KeyboardInterrupt):
                break
            if not action or action == "done":
                break
            if means_cancel(action):
                raise CancelledByUser()
            # The name the button entity will carry. Kept to the same character
            # set as a device id so it cannot produce an entity id that needs
            # quoting later.
            if not re.fullmatch(r"[a-z0-9_]{1,32}", action):
                await self._io.write(
                    "  Use lowercase letters, digits and underscores only.", level="warn")
                continue

            code_name = f"{prefix}_{action}"
            await self._io.write(f"  Point the remote at the receiver and press "
                                 f"{action.upper()} now...")
            event("IR", f"arming learn for {code_name!r} ({IR_LEARN_TIMEOUT}s)")

            # Off the event loop: this call blocks for the whole timeout, and
            # holding the loop for 45 seconds would stall every other
            # connection, the reader task and the keepalives.
            # `timeout` is BOTH the service's own field and this method's HTTP
            # read timeout, so it cannot be passed once and mean both. The
            # named argument extends the HTTP wait; the dict carries the
            # service field. Passing it twice through **kwargs raised
            # TypeError: got multiple values for 'timeout'.
            # Two timeouts, deliberately distinct names: http_wait is how long
            # to hold the connection, `timeout` is the field learn_start reads
            # to decide how long to listen. One name could not carry both --
            # see call_service_with_response.
            capture = await asyncio.to_thread(
                lambda: self.ha_client.call_service_with_response(
                    "openirblaster", "learn_start",
                    http_wait=IR_LEARN_TIMEOUT,
                    config_entry_id=OPENIRBLASTER_ENTRY_ID,
                    timeout=IR_LEARN_TIMEOUT),
            )
            status = (capture or {}).get("status")
            event("IR", f"learn {code_name!r} -> {status!r}")

            if status != "received":
                await self._io.write({
                    "timeout": "  Nothing arrived in time. Try again, closer to "
                               "the receiver.",
                    "cancelled": "  That capture was cancelled.",
                    None: "  The blaster did not answer -- check it is online.",
                }.get(status, f"  Capture failed: {status}"), level="warn")
                continue

            await self._io.write(
                f"  Captured: {capture.get('pulse_count')} pulses at "
                f"{capture.get('carrier_hz')} Hz")

            saved = await asyncio.to_thread(
                self.ha_client.call_service_with_response,
                "openirblaster", "save_pending",
                config_entry_id=OPENIRBLASTER_ENTRY_ID,
                name=code_name,
            )
            if not saved or not saved.get("code_id"):
                # The capture is still PENDING at this point, not lost -- the
                # integration holds it. Say so rather than implying it vanished.
                await self._io.write(
                    "  Captured but not saved (the name may already exist). The "
                    "capture is still pending on the blaster.", level="warn")
                continue

            learned += 1
            await self._io.write(f"  [OK] Saved as {saved.get('name', code_name)!r}")

        return learned
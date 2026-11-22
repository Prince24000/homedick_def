"""Registering an ESP board's devices on THIS hub -- and nothing else.

A board this hub flashed is OURS: its topic, its template and every device
on it are known from its own record, so registration reads the plan. It
never asks Home Assistant anything, never waits for it, never matches a
device against its entities and never binds one to them. Home Assistant is
for integrations (Hue, a smart TV, whatever it can discover); a board is
not one, and the two are kept apart on purpose (her ruling, 2026-10-01:
"they are not meant to be together").

This lived in home_assistant/ha_flows.py until then, where its HA-shaped
neighbours (the wait for entities, the re-link after a rewire) kept
pulling it back toward a thing it should never have touched.
"""

import asyncio
from typing import Any, Dict, List, Optional

from core.hv_common import CancelledByUser, event, means_cancel


class BoardRegistrar:
    """Registers boards' devices on behalf of the shell.

    Holds the shell rather than copies of its parts: `_io` is swapped for a
    WebSocketIO on every connection, so a snapshot taken at construction
    would be pointing at a dead object by the second command.
    """

    def __init__(self, shell):
        self._shell = shell

    @property
    def _io(self):
        return self._shell._io

    def _tell_engine(self, method: str) -> None:
        """Call one of the automation engine's refresh hooks, if it runs.

        boards_changed: a board arrived, so its readings are understood and
        its panel is woken now rather than up to five minutes from now.
        refresh_lcd_targets: see _panels_changed below.

        NEVER RAISES, and never fatal: registration has already succeeded
        by this point, and a display is decoration."""
        engine = getattr(self._shell, "automations", None)
        if engine is None:
            return
        try:
            getattr(engine, method)()
        except Exception:                              # noqa: BLE001
            pass
    async def _describe_board_devices(
            self, matches: List[Any], board_name: str
    ) -> Dict[str, Dict[str, Any]]:
        """Descriptions for a whole board -- ONE DEVICE AT A TIME.

        The owner's design, per device (2026-08-27, "do the per devices
        registration"): each device is its own small ritual, the same
        shape as the manual add. Say what it does in her words (Enter
        keeps the catalogue's plain sentence), then the AI offer FOR
        THAT DEVICE, then the result reviewed before the next device --
        the fuller description shown together with the value range and
        per-step change it will move by. The earlier one-AI-question-
        for-the-whole-board shape saved taps but treated the devices as
        a batch; they are registered as individuals and they are
        described as individuals.

        The description is not decoration. It is the only thing semantic
        matching has: dm.add embeds it, resolve_semantic compares those
        vectors, and that is how "turn on the thing that boils water"
        reaches the kettle. A board has no embedding and is never
        resolved against -- only devices are.

        The value/step envelope is SHOWN, not asked: a board device's
        envelope is hardware fact (a relay is binary, a dimmer runs
        0-100 -- register_board takes it from the catalogue), the same
        law that lets HA's real numbers overwrite an AI guess on the
        manual add path.
        """
        # registerable, not matched: a device registered LOCALLY (an event
        # device, or one HA has not shown yet) needs its description just
        # as much -- the description IS its voice reachability, and it has
        # one regardless of what Home Assistant thinks.
        wanted = [m for m in matches if m.registerable]
        if not wanted:
            return {}

        from esp_firmware import esphome_catalog as _cat

        # name -> {description?, room, secure, show_on_home?, name?, state?
        # ...}. It was name -> description; register_board reads both shapes.
        #
        # ONE SCREEN PER DEVICE (her ruling, 2026-09-03): the same
        # registration form Add device and Edit device use -- room, name,
        # what it does, how it behaves, security, its state right now,
        # home screen -- prefilled with what the board and the catalogue
        # already know. The kind is DECLARED by the part (a relay is
        # on/off, a DHT reads), so it is shown locked; the numbers are the
        # catalogue's and can be changed on the form. The AI is a button
        # inside the form, never a separate question. A terminal walks the
        # same form as prompts (IOBackend.registration).
        out: Dict[str, Dict[str, Any]] = {}
        for i, m in enumerate(wanted, 1):
            plain = self._shell.dm.plain_description(m.type_id, m.name,
                                                     board_name)
            await self._io.write(f"\n  [{i}/{len(wanted)}] {m.name}")
            produces = _cat.BY_ID[m.type_id].produces
            env = produces.as_meta()
            domain = produces.domain
            binary = env.get("state_key") == "on"
            source = f"From board {board_name!r} -- {m.name}"
            # Does this part report numbers? The catalogue's own answer. A
            # tag reader is domain 'sensor' too and reports a card id, not
            # a measurement -- the automation index puts it under 'tag' for
            # the same reason. Offering "show its data" for one would be a
            # tile with nothing to put on it.
            if domain == "sensor" and produces.envelope[0] not in ("tag", "ir"):
                kind = "reading"
                # The read-only half of the form: what this part measures.
                # Declared, not asked -- and shown, because a person about
                # to answer "show its data?" should be able to see what the
                # data IS.
                names = [produces.envelope[0], *produces.extras]
                unit = f" {env['unit']}" if env.get("unit") else ""
                source += (f": Measures: {' · '.join(names)}. "
                           f"{env['range_min']}-{env['range_max']}{unit}, "
                           f"moving {env['step']} per step -- from the part "
                           f"itself, nothing to fill in.")
            elif domain == "cover":
                kind = "open"
            elif binary:
                kind = "onoff"
            else:
                kind = "level"
            family = self._shell._family_of_part(m.type_id)
            spec = {
                "title": "Add device",
                "source": source,
                "editing": False,
                # WHAT THE PART IS MEANT TO BE is marked on the form, and
                # any kind may still be chosen (her ruling, 2026-10-05). It
                # was locked to the part's own kind until then.
                "kind_options": self._shell._kind_options(family),
                "position_kinds": self._shell._position_kinds(family),
                # A board reports its own state the moment it speaks; the
                # relay's starting position is still asked, so the first
                # command is right before the first telemetry.
                "state_known": kind == "reading",
                "values": {
                    "name": m.name, "room": board_name, "does": "",
                    "kind": kind,
                    "min": "" if binary else f"{float(env.get('range_min', 0.0)):g}",
                    "max": "" if binary else f"{float(env.get('range_max', 100.0)):g}",
                    "step": "" if binary else f"{float(env.get('step', 1.0)):g}",
                    "unit": str(env.get("unit") or ""),
                    # The door strike is a relay ("switch"): no catalogue
                    # part produces a `lock` domain, so it is told by type.
                    "secure": m.type_id == "lock",
                    "state": "", "show_on_home": True,
                },
            }
            values = await self._shell._registration_loop(spec)
            fields = self._shell._record_fields_from_values(values)
            record: Dict[str, Any] = {
                "name": fields["name"],
                "room": fields["room"] or board_name,
                "secure": fields["secure"],
                "show_on_home": fields["show_on_home"],
                "description": (fields["description"]
                                if str(values.get("does") or "").strip()
                                or values.get("_description") else plain),
            }
            if kind == "level":
                record.update(step=fields["step"], range_min=fields["range_min"],
                              range_max=fields["range_max"])
            if kind != "reading":
                record["state"] = fields["state"]
            unit = str(values.get("unit") or "").strip()
            if unit:
                record["unit"] = unit
            # A KIND OTHER THAN THE PART'S OWN was chosen: register_board
            # writes what the catalogue says the part is, so the person's
            # choice is laid over the record afterwards (describe_and_
            # register), exactly as an Edit of the device would.
            chosen = str(values.get("kind") or kind).strip().lower()
            if chosen != kind:
                keep = ("name", "room", "description", "secure",
                        "show_on_home", "provenance")
                self._kind_choice[m.name] = {
                    k: v for k, v in fields.items() if k not in keep}
            out[m.name] = record
        return out

    async def describe_and_register(self, matches: List[Any], slug: str,
                                    board_name: str):
        """The registration form for each of `matches`, then their records.
        Returns what dm.register_board does: (created ids, skipped).
        Raises CancelledByUser when a form is backed out of -- nothing is
        registered then."""
        self._kind_choice = {}
        descriptions = await self._describe_board_devices(matches, board_name)
        dm = self._shell.dm
        fresh: set = set()          # the parts actually added just now

        def write():
            created, skipped = dm.register_board(matches, slug, board_name,
                                                 descriptions)
            for m in matches:
                final = (descriptions.get(m.name) or {}).get("name") or m.name
                if {dm.board_device_id(slug, m.name),
                        dm.board_device_id(slug, final)} & set(created):
                    fresh.add(getattr(m, "type_id", ""))
            for name, fields in self._kind_choice.items():
                final = (descriptions.get(name) or {}).get("name") or name
                for did in {dm.board_device_id(slug, name),
                            dm.board_device_id(slug, final)}:
                    if did in created:
                        dm.update(did, **fields)
            return created, skipped
        # Off the loop: every record is embedded and the registry rewritten.
        try:
            result = await asyncio.to_thread(write)
        finally:
            self._kind_choice = {}
        await self._first_setup(fresh)
        return result

    async def _first_setup(self, fresh: set) -> None:
        """Right after registering, a part that needs a first reading starts
        it -- or is skipped, and done later. Never undoes the registration:
        a stop or a miss is only said."""
        if "ir_recv" in fresh:
            await self._offer_setup(
                "The infrared receiver is ready. Learn a remote now?",
                "Learn a remote now",
                "point the remote at the receiver and press each button",
                "later: Devices > the infrared receiver > Learn a remote",
                self._shell.menu_learn_ir)
        if fresh & {"rc522", "pn532"}:
            await self._offer_setup(
                "The card reader is ready. Add your card now?",
                "Tap my card now", "hold your card on the reader",
                "later: Enrol a card",
                self._shell.menu_enroll_card)

    async def _offer_setup(self, question: str, label: str, sub: str,
                           later: str, start) -> None:
        io = self._shell._io
        try:
            picked = await io.choose(question, [
                {"value": "now", "label": label, "sub": sub},
                {"value": "skip", "label": "Skip", "sub": later}])
        except CancelledByUser:
            picked = None
        if not picked or picked[0] != "now":
            await io.write(f"  Skipped -- {later}.")
            return
        # The engine must know the new board before it can hear it.
        self._tell_engine("boards_changed")
        before = self._shell.failure_note
        try:
            await start()
        except CancelledByUser:
            await io.write(f"  Stopped -- {later}.")
        except Exception as exc:                       # noqa: BLE001
            event("SETUP", f"first setup did not finish ({exc})")
            await io.write(f"  That did not finish -- {later}.", level="warn")
        finally:
            # The registration itself worked: its verdict stays clean.
            self._shell.failure_note = before

    #: name -> the record fields of a kind the person chose over the part's
    #: own, between the forms and the registering (describe_and_register).
    _kind_choice: Dict[str, Dict[str, Any]] = {}
    async def _register_board(self, slug: str) -> bool:
        """Everything between a flashed board and devices you can talk to.

        Takes no `before` (HA's entity ids from before the flash): this path
        stopped needing it on 2026-09-01 when HA was taken out of the
        matching -- see `has_ha=False` at the match_board call below.
        """
        record = self._shell.esp._read_board(slug) or {}
        board_name = record.get("name", slug)
        items = record.get("items", [])

        # A BOARD CAN BE ALL EVENTS (the card-reader question,
        # 2026-08-25). An RC522, an IR receiver, a display, the IMU --
        # none of them ever creates a Home Assistant entity, BY DESIGN:
        # they publish moments, not states, and they register here
        # entity-less (BoardMatch.local_only). Waiting for HA on such a
        # board would park it at a register checkpoint that no Continue
        # could ever finish. If nothing placed on it produces an entity,
        # there is nothing to wait for -- register now, and the ribbon
        # ends in a genuine Done.
        from esp_firmware import esphome_catalog as _cat
        event_only = bool(items) and not any(
            (_cat.BY_ID.get(i.get("type_id", "")) is not None
             and _cat.BY_ID[i["type_id"]].produces.domain)
            for i in items if isinstance(i, dict))

        # HOME ASSISTANT IS NOT PART OF REGISTERING OUR OWN BOARDS
        # (her ruling, 2026-09-01: "remove this HA, it is conflicting with
        # the notifications of registering devices").
        #
        # This waited on HA, then matched every placed device against HA's
        # entity list. Both were wrong for a board THIS HUB flashed:
        #
        #  * The wait put "Waiting for the board to appear in Home
        #    Assistant" in front of a registration that does not need it.
        #  * The matching attached a PZEM to
        #    `sensor.kitchen_energy_apparentpower` -- HA's guess at which of
        #    its entities was this device -- and that wrong entity then
        #    became the road every command took.
        #
        # A board we flashed is ours: we know its topic, its template and
        # every device on it. Registration reads the plan, not a third
        # party's opinion of the plan. HA keeps the one job she left it --
        # adding devices other people made. (So the all-events board above
        # takes this same road now: with no wait on HA there is nothing it
        # needs a road of its own for -- only its closing sentence.)

        # LOCAL-FIRST (the owner's law, 2026-08-26): Home Assistant not
        # showing a device -- slowly, or ever -- must not hold the ribbon
        # hostage. Whatever HA offered inside the bounded wait attaches
        # NOW; everything else registers locally, gets its description,
        # works over its own MQTT (the direct-drive seam), and the attach
        # courier keeps courting HA in the background. The old ending --
        # park at a register checkpoint and make the person Continue it by
        # hand -- is gone: registration is not HA's to gate. Checkpoints
        # remain only where the BOARD is the problem (flash, network).
        matches = self._shell.dm.match_board(items, slug, board_name, [],
                                             local_first=True,
                                             has_ha=False)
        created, skipped = await self.describe_and_register(
            matches, slug, board_name)
        self._tell_engine("boards_changed")

        await self._summarise_board(slug, board_name, matches, created,
                                    skipped)

        # NOTHING IS OWED, because nothing was asked. This block used to
        # list every device HA had not shown, set `attach_pending`, and
        # promise to keep courting it -- which made sense while
        # registration matched against HA's entities. It no longer does
        # (2026-09-01), so there is no debt to record and no courier to
        # arm. A board we flashed is registered from its own plan and
        # driven over its own wire; Home Assistant is not in this story.
        self._clear_pending(slug, "register_pending", "attach_pending")
        if event_only:
            await self._io.write(
                f"  {board_name} carries only event devices -- taps, "
                f"presses, readings that arrive as moments. It is "
                f"registered here and finished.")
            return True
        return bool(created)
    def _clear_pending(self, slug: str, *keys: str) -> None:
        """Drop these marks from the board's record, written only if one was
        there. RE-READ, not the copy taken when registration began: that
        copy is minutes old by now -- a form per device sits between the
        two -- and writing it back would undo anything saved to the record
        meanwhile. (It was also written unconditionally, and a record that
        had vanished came back as {} and raised KeyError on its slug.)"""
        esp = self._shell.esp
        record = esp._read_board(slug)
        if record is not None and [k for k in keys
                                   if record.pop(k, None) is not None]:
            esp._write_board(record)

    async def _summarise_board(self, slug: str, board_name: str,
                               matches: List[Any], created: List[str],
                               skipped: List[str]) -> None:
        """What was registered, and a chance to change it.

        Everything is stated including what did NOT work, because a device
        quietly missing from this list is one you discover months later when
        it does not answer.
        """
        await self._io.write(f"\n--- {board_name.upper()} ---")
        if created:
            await self._io.write(f"  Registered {len(created)}:")
            for did in created:
                device = self._shell.dm.devices.get(did, {})
                await self._io.write(
                    f"    {device.get('name', did)} -- "
                    f"{device.get('description', '')}")
        if skipped:
            await self._io.write(
                f"  Already registered, left alone: {', '.join(skipped)}")

        # THREE OUTCOMES, NOT TWO. A device with no Home Assistant entity
        # is now REGISTERED (see BoardMatch.local_only) rather than skipped,
        # so listing it under "Not registered" would be a lie -- and the old
        # code did exactly that, which is how an IR blaster came to be
        # invisible everywhere except the pin screen. It is registered, it
        # simply has nothing in Home Assistant behind it, and that is worth
        # saying out loud rather than leaving to be discovered.
        # NO "with no Home Assistant entity" SECTION. Registering a board
        # does not consult Home Assistant at all any more, so EVERY device
        # is local -- and this listed the whole board a second time under a
        # heading naming a thing that is no longer part of the story. The
        # "Registered N" list above already named them all, in the same
        # order.

        unmatched = [m for m in matches
                     if not m.matched and not m.local_only]
        if unmatched:
            await self._io.write("\n  Not registered:")
            for m in unmatched:
                await self._io.write(f"    {m.name} -- {m.reason}")

        extras = [(m, e) for m in matches for e in m.extras]
        if extras:
            await self._io.write("")
            for m, entity in extras:
                reading = entity.rsplit("_", 1)[-1]
                await self._io.write(
                    f"  {m.name} also reports {reading}.")

        if not created:
            return
        while True:
            choices = [{"value": "done", "label": "That is right",
                        "sub": "finish"},
                       {"value": "rename", "label": "Rename one",
                        "sub": "if a name reads wrong"},
                       {"value": "drop", "label": "Remove one",
                        "sub": "unregister it here"}]
            picked = await self._io.choose("Anything to change?", choices)
            if not picked or picked[0] == "done":
                return
            target = await self._shell._pick_one(
                [self._shell.dm.devices[d].get("name", d) for d in created
                 if d in self._shell.dm.devices],
                "Which one?")
            if target is None:
                continue
            did = next((d for d in created
                        if self._shell.dm.devices.get(d, {}).get("name") == target),
                       None)
            if did is None:
                continue
            if picked[0] == "rename":
                new = (await self._io.ainput(
                    f"  New name for {target}: ", patient=True)).strip()
                # A CANCEL IS NOT A NAME (audit, 2026-09-12). ainput hands
                # back the raw answer, and a phone answers a cancelled box
                # with the word "cancel" -- so backing out of this question
                # renamed the device to "cancel". 28 of the 31 ainput sites
                # check for it; this was the one where the word got STORED.
                # Exactly the fault means_cancel exists for: "a room named
                # 'Cancel.'" (field test 2026-08-29).
                if means_cancel(new):
                    await self._io.write(f"  {target} keeps its name.")
                elif new and self._shell.dm.update(did, name=new):
                    await self._io.write(f"  Now called {new}.")
            else:
                if self._shell.dm.remove(did):
                    created.remove(did)
                    await self._io.write(
                        f"  {target} unregistered. It is still wired to the "
                        f"board -- remove it from the board's pins if you "
                        f"want it gone for good.")
                if not created:
                    return

    async def settle_after_edit(self, slug: str,
                                result: Dict[str, Any]) -> bool:
        """After an Edit changed what a board carries: bring this hub's
        registry in line.

        * devices that went are unregistered by the caller (before this);
        * a device NEW on the board goes through THE REGISTRATION FORM, one
          each (her ruling, 2026-10-05: "any new device, whether it was an
          existing integration or existing board, must go through the
          registration form ... the only way the system can understand
          that device"). This replaced four days of registering them
          plainly, with the catalogue's sentence and nothing asked. Backed
          out of, they stay unregistered and are offered again from
          Pending actions -- never registered behind the person's back;
        * a device that kept its name but changed its part is re-fitted to
          what it is now (its name, room and description kept).

        Everything else is left exactly as it was. Purely local: this hub
        decides what a board it flashed carries, and there is no one else to
        ask.
        """
        record = self._shell.esp._read_board(slug) or {}
        board_name = record.get("name", slug)
        items = record.get("items", [])
        matches = self._shell.dm.match_board(items, slug, board_name, [],
                                             local_first=True, has_ha=False)
        added = set(result.get("added") or [])
        changed = set(result.get("changed") or [])
        fresh: List[Any] = []
        refitted: List[str] = []
        for m in matches:
            did = self._shell.dm.board_device_id(slug, m.name)
            known = did in self._shell.dm.devices
            if known and m.name in changed:
                if self._shell.dm.refit_board_device(did, m.type_id):
                    refitted.append(m.name)
            elif not known and m.registerable and m.name in added:
                # ONLY what this edit added. A device the person once chose
                # to "Remove one" from the summary is wired but deliberately
                # unregistered; an edit must not register it behind their
                # back.
                fresh.append(m)
        created: List[str] = []
        if fresh:
            try:
                created, _ = await self.describe_and_register(
                    fresh, slug, board_name)
            except CancelledByUser:
                # The wiring is already on the board; only the forms were
                # left. Say so, and leave them for Pending actions.
                names = ", ".join(m.name for m in fresh)
                await self._io.write(
                    f"  Still to register on {board_name}: {names}. They "
                    f"are wired, but this hub knows nothing about them "
                    f"until their forms are filled in -- Pending actions "
                    f"has them.", level="warn")
        # ALWAYS, NOT ONLY WHEN A DEVICE WAS ADDED (2026-10-01): a part that
        # was removed, re-wired or re-fitted changes what the engine should
        # make of this board's messages just as much, and told nothing it
        # kept reading the old wiring for up to five minutes.
        self._tell_engine("boards_changed")
        if refitted:
            await self._io.write(
                f"  Now a different part: {', '.join(refitted)} -- "
                f"re-fitted to what it is now (its name, room and "
                f"description kept).")
        if created:
            names = ", ".join(
                self._shell.dm.devices.get(d, {}).get("name", d)
                for d in created)
            await self._io.write(
                f"  New on the board, registered: {names}.")
        self._panels_changed()
        return True

    async def register_board(self, slug: str) -> bool:
        ok = await self._register_board(slug)
        self._panels_changed()
        return ok

    def _panels_changed(self) -> None:
        """Re-read which boards carry a panel, immediately.

        The engine re-reads this on its own every five minutes, which is
        fine for a house that is not changing and far too slow for the
        minute after you install a screen. Her ask, 2026-09-01: "I want
        immediately a display is found the automatic text of time and date
        is sent." This is the "immediately" half -- the engine's next tick,
        a few seconds later, wakes any panel it has not woken before and
        hands the glass to Tasmota's own clock.

        NEVER RAISES. A display is decoration, and decoration must not be
        able to fail a registration that has already happened.
        """
        self._tell_engine("refresh_lcd_targets")

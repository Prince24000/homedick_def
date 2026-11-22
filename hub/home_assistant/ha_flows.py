"""Driving a Home Assistant config flow, as a conversation.

WHY THIS IS NOT IN ha_client.py: that file speaks HTTP. Everything here is
the other half -- asking, waiting, explaining, and handling somebody backing
out. Putting it in the client would make the client import a cancellation
exception, know what a form is, and know that a person might change their
mind. It would stop being a client and become a second monolith with a
different name.

WHY IT IS NOT IN main.py EITHER: it was, and it was 660 lines of a 4,650
line file that otherwise has nothing to do with Home Assistant.

Home Assistant expresses every integration set-up as a flow: a sequence of
steps it hands back one at a time. There are five kinds and this drives four
of them --

    form            fill these fields in          -> one screen, all fields
    menu            pick a route                  -> the choice screen
    external        sign in on someone's website  -> open it, then watch
    progress        do something physical, I wait -> say what, then watch
    create_entry    finished
    abort           stopped

The last two are outcomes rather than questions. The interesting pair is
external and progress: they look like different features and are
the same mechanic -- a step with nothing to answer, which Home Assistant
advances by itself. Nothing arrives on our socket, so neither can be
awaited. Both are polled, by one helper.
"""

import asyncio
import json
import time
from typing import Optional, List, Dict, Any

from core.hv_common import event, CancelledByUser, means_cancel


class ConfigFlowRunner:
    """Runs config flows on behalf of the shell.

    Holds the shell rather than copies of its parts: `_io` is swapped for a
    WebSocketIO on every connection and `ha_client` is rebuilt on reconnect,
    so a snapshot taken at construction would be pointing at a dead object
    by the second command.
    """

    def __init__(self, shell):
        self._shell = shell

    @property
    def _io(self):
        return self._shell._io

    @property
    def ha_client(self):
        return self._shell.ha_client

    #: How long to wait for an integration's entities to appear after its
    #: config entry is created. HA sets the entry up, then the integration
    #: connects and registers entities -- asynchronously, so they are not
    #: there the instant the flow returns.
    #: Eight seconds was right when a config flow had just finished and
    #: the integration was already loaded. A freshly provisioned board
    #: has more to do: join Wi-Fi, reach the broker, announce itself,
    #: and wait for Home Assistant to notice. Twenty-five is patient
    #: enough for that without leaving somebody staring at a screen.
    ENTITY_SETTLE_SECONDS = 25.0

    #: iot_class values meaning "talks to the device directly". Everything
    #: else either needs somebody's cloud or has no class declared.
    _LOCAL_IOT_CLASSES = ("local_polling", "local_push")
    async def _start_manual_flow(self) -> Optional[Dict[str, Any]]:
        """Begin a set-up for a device Home Assistant did not find by itself.

        The integration list comes FROM Home Assistant, not from a copy kept
        here. HA supports well over a thousand and ships more every release;
        any list maintained on this side would be a snapshot that silently
        went stale. If HA cannot answer, this says so rather than offering a
        shorter list that pretends to be complete.

        Searched rather than listed, for the same reason -- a thousand entries
        is not a picker. Type part of a name, choose from what matches.

        Identical to the discovered path from here on: HA returns the same kind
        of form, asking for a host instead of confirming one it already knows.
        """
        # NO HOME ASSISTANT IS NOT A FAULT. This door is the only thing it
        # still does -- boards this hub flashed are driven and read over their
        # own MQTT -- so a hub without it is a working hub that simply cannot
        # add other people's devices. Reaching list_flow_handlers on a None
        # client raised outright, and a warning would put a fault marker on a
        # hub with nothing wrong with it. The screen opens, states the fact,
        # and offers nothing: an empty list is an honest list.
        if self.ha_client is None:
            await self._io.write(
                "Home Assistant is not set up on this hub, so there is no "
                "catalogue to search. This is the only thing it does -- "
                "adding devices other people made. Your own boards do not "
                "need it.")
            await self._io.choose("Add an integration", [])
            return None

        handlers = await asyncio.to_thread(self.ha_client.list_flow_handlers)
        if not handlers:
            await self._io.write(
                "  Home Assistant did not return its list of integrations, so "
                "there is nothing to search. Add this one from Home Assistant's "
                "own page instead.", level="warn")
            return None

        # Best effort: real names and the local/cloud split. Without it the
        # domain is shown, which is readable enough ('esphome', 'shelly').
        manifests = await asyncio.to_thread(self.ha_client.get_integration_manifests, handlers)

        def iot_of(domain: str) -> str:
            return str((manifests.get(domain) or {}).get("iot_class") or "")

        def remote(domain: str) -> bool:
            return iot_of(domain) not in self._LOCAL_IOT_CLASSES

        def describe(domain: str) -> Dict[str, str]:
            m = manifests.get(domain) or {}
            iot = iot_of(domain)
            if iot in self._LOCAL_IOT_CLASSES:
                sub = "local — talks to the device directly"
            elif iot.startswith("cloud"):
                sub = "cloud — needs an account, may need Home Assistant's page"
            else:
                sub = domain
            return {"value": domain, "label": m.get("name") or domain, "sub": sub}

        await self._io.write(f"  {len(handlers)} integrations available.")
        # THE LIST AND THE SEARCH, TOGETHER (field test 2026-08-25: "both
        # search bar and the scrollable list of the integrations should be
        # shown"). Search-only made the person guess a name before seeing
        # anything; the full list now shows immediately -- local-first,
        # scrollable on the phone -- with a search row pinned on top for
        # when scrolling a thousand rows is the wall instead. Backing out
        # of a search returns to this list, not to the menu.
        ordered = sorted(handlers, key=lambda d: (
            remote(d), str((manifests.get(d) or {}).get("name") or d).lower()))
        full_rows = [{"value": "\x00search",
                      "label": "Search by name…",
                      "sub": "type part of it -- 'esp', 'hue', 'zigbee'"}]
        full_rows += [describe(d) for d in ordered]
        handler = None
        while handler is None:
            picked = await self._io.choose(
                f"Which integration? ({len(handlers)} available, "
                f"local ones first)", full_rows)
            if not picked:
                return None
            if picked[0] != "\x00search":
                handler = picked[0]
                break
            term = (await self._io.ainput(
                "  Search (part of the name, e.g. 'esp'): ")).strip().lower()
            if means_cancel(term) or not term:
                continue                      # back to the full list
            matches = [d for d in handlers if term in d.lower()]
            # Names too, since "kasa" finds tplink and "hue" finds philips_hue.
            if manifests:
                matches += [d for d in handlers
                            if d not in matches
                            and term in str((manifests.get(d) or {}).get("name", "")).lower()]
            if not matches:
                await self._io.write(f"  Nothing matches {term!r}.",
                                     level="warn")
                continue
            # Local first: those are the ones that can finish here.
            matches.sort(key=lambda d: (remote(d), d))
            picked = await self._io.choose("Which one?",
                                           [describe(d) for d in matches])
            if picked:
                handler = picked[0]
            # No pick = back around to the full list, nothing lost.

        iot = iot_of(handler)
        if iot.startswith("cloud"):
            # Said BEFORE the flow starts, not after it dead-ends on an OAuth
            # step. The flow driver handles that case correctly either way,
            # but being told upfront is better than finding out three prompts
            # in.
            await self._io.write(
                "  This one connects through its maker's cloud. If it asks you "
                "to sign in with a browser, finish it in Home Assistant's page "
                "— the sign-in cannot be shown here.")

        event("HA", f"manual set-up of {handler!r} requested (iot_class={iot!r})")
        step = await asyncio.to_thread(self.ha_client.start_config_flow, handler)
        if step is None:
            await self._io.write(
                f"  Home Assistant could not start a {handler} set-up.",
                level="warn")
        return step
    #: Steps one config flow may take before we stop. Flows are short -- host,
    #: password, confirm -- and a flow that never finishes is a bug in the
    #: integration, not something to sit in forever.
    MAX_FLOW_STEPS = 12

    #: Plain-language help for the config-flow fields people actually meet.
    #: Home Assistant's schema gives a machine name and nothing else, so
    #: "host" arrives on screen as the bare word "host" -- which is exactly
    #: as useful as it sounds when you are holding a phone in front of a
    #: chip you just flashed.
    FLOW_FIELD_HELP: Dict[str, str] = {
        "host": "The device's address on your network — e.g. 192.168.1.42. "
                "The ESPHome builder shows it once the node is online; your "
                "router's device list also has it. The node's name followed "
                "by '.local' often works too.",
        "port": "Leave 6053 unless you changed it. That is the port ESPHome "
                "devices listen on.",
        "password": "The API encryption key generated when the node was "
                    "created — the long code shown on the copy card.",
        "noise_psk": "The API encryption key generated when the node was "
                     "created — the long code shown on the copy card.",
        "encryption_key": "The API encryption key generated when the node "
                          "was created — the long code shown on the copy "
                          "card.",
        "name": "A label for this device inside Home Assistant.",
        "username": "The account name for this device or service.",
        "api_key": "The access token this service issued to you.",
        "ssid": "The Wi-Fi network name.",
    }
    def _flow_fields(self, schema: List[Any]) -> List[Dict[str, Any]]:
        """An HA schema turned into form fields a person can answer.

        Two jobs: give every field a readable label instead of its machine
        name, and attach help where we know what the field means.
        """
        fields: List[Dict[str, Any]] = []
        for entry in schema:
            if not isinstance(entry, dict):
                continue
            name = entry.get("name") or "value"
            ftype, options = self._field_shape(entry)
            if ftype not in ("string", "integer", "float", "boolean",
                             "select"):
                # Unsupported types are NOT guessed at -- a wrong value
                # creates a broken config entry someone has to hunt down.
                raise ValueError(
                    f"field {name!r} has type {ftype!r}, which this app "
                    f"cannot render")
            choices = ([str(o) for o in options] if ftype != "boolean"
                       else ["yes", "no"])
            field = {
                "name": name,
                "label": name.replace("_", " ").capitalize(),
                "type": ftype,
                "default": entry.get("default"),
                "required": bool(entry.get("required")),
                "help": self.FLOW_FIELD_HELP.get(name, ""),
                "choices": choices,
            }
            # A secret is typed hidden, like the Wi-Fi password is.
            low = str(name).lower()
            if any(w in low for w in ("password", "psk", "api_key",
                                      "token", "secret")):
                field["secret"] = True
            fields.append(field)
        return fields

    @staticmethod
    def _field_shape(entry: Dict[str, Any]) -> "tuple[str, List[Any]]":
        """(type, raw option values) of one serialised schema field.

        TWO SHAPES ARRIVE. A plain voluptuous field carries `type` and, for
        a select, `options` as values or (value, label) pairs. A SELECTOR
        field -- what most integrations now declare -- carries no `type` at
        all, only {"selector": {"<kind>": {config}}}. Those used to fall
        through to "string": a number or yes/no question became free text,
        and HA's BooleanSelector coerces with bool(), so an answer of "no"
        arrived as True. A multiple-choice select wants a list, which this
        app does not render, so it is named as such and refused.
        """
        sel = entry.get("selector")
        if isinstance(sel, dict) and sel:
            kind, cfg = next(iter(sel.items()))
            cfg = cfg if isinstance(cfg, dict) else {}
            if kind == "select" and cfg.get("multiple"):
                return "multi_select", []
            ftype = {"number": "float", "boolean": "boolean",
                     "select": "select"}.get(kind, "string")
            return ftype, [o.get("value") if isinstance(o, dict) else o
                           for o in cfg.get("options") or []]
        return entry.get("type", "string"), [
            o[0] if isinstance(o, (list, tuple)) and len(o) == 2 else o
            for o in entry.get("options") or []]

    @staticmethod
    def _coerce_flow_answers(schema: List[Any],
                             answers: Dict[str, Any]) -> Dict[str, Any]:
        """Answers typed the way HA's schema declared them.

        A form answers in text -- the terminal walk always does, since it is
        ainput -- and HA validates with voluptuous types: ESPHome's port is a
        bare `int`, so "6053" came back as "expected int" and the form was
        re-asked until MAX_FLOW_STEPS ran out. A boolean answered "no" and a
        select whose option values are not strings fail the same way. Text
        that does not convert is left as typed, so HA's own error for it is
        what comes back on the form.
        """
        by_name = {e.get("name"): e for e in schema
                   if isinstance(e, dict) and e.get("name")}
        out: Dict[str, Any] = {}
        for key, value in answers.items():
            ftype, options = ConfigFlowRunner._field_shape(by_name.get(key) or {})
            if isinstance(value, str):
                text = value.strip()
                if ftype in ("integer", "float"):
                    try:
                        value = (int if ftype == "integer" else float)(text)
                    except ValueError:
                        pass
                elif ftype == "boolean":
                    low = text.lower()
                    if low in ("yes", "true", "on", "1"):
                        value = True
                    elif low in ("no", "false", "off", "0"):
                        value = False
                elif ftype == "select":
                    value = next((raw for raw in options if str(raw) == value),
                                 value)
            out[key] = value
        return out
    async def _flow_menu(self, step: Dict[str, Any]) -> Optional[str]:
        """A menu step turned into a pick. Returns the chosen step id.

        Home Assistant sends `menu_options` either as a list of step ids or as
        a mapping of id to label, depending on the integration and whether
        translations came through. Both shapes are handled -- a bare id like
        `pick_implementation` is at least readable once the underscores are
        gone, which is better than refusing the whole integration because the
        labels did not arrive.
        """
        raw = step.get("menu_options")
        options: List[Dict[str, str]] = []
        if isinstance(raw, dict):
            for value, label in raw.items():
                options.append({"value": str(value),
                                "label": str(label),
                                "sub": ""})
        elif isinstance(raw, list):
            for value in raw:
                options.append({"value": str(value),
                                "label": str(value).replace("_", " ").capitalize(),
                                "sub": ""})
        if not options:
            await self._io.write(
                "  Home Assistant offered a menu with nothing in it.",
                level="warn")
            return None
        title = step.get("title") or "How do you want to set this up?"
        picked = await self._io.choose(title, options)
        return picked[0] if picked else None
    async def _await_flow_advance(self, step: Dict[str, Any],
                                  flow_id: Optional[str]) -> Optional[Dict[str, Any]]:
        """Watch a step that takes no input until Home Assistant moves on.

        Returns the next step, or None if it timed out or HA stopped
        answering. Polled rather than awaited: nothing is going to arrive on
        our socket, because the thing we are waiting for is happening
        somewhere else entirely -- in a browser, or in the hand of whoever is
        holding a button on a bulb.
        """
        kind = step.get("type")

        # The *_done pair are not waits at all. HA is telling us the external
        # part finished and wants the flow carried on WITHOUT input -- which
        # is a GET (HA configures the flow with no answer), exactly as its
        # own frontend does. An empty SUBMIT handed the next step `{}` where
        # it expects None, and a step that tests `user_input is not None`
        # read that as a filled-in form.
        if kind in ("external_done", "progress_done"):
            return await self._submit(flow_id, None)

        get_flow = self.ha_client.get_config_flow

        if kind == "external":
            url = str(step.get("url") or "").strip()
            if not url:
                await self._io.write(
                    "  This sign-in step arrived without an address to open.",
                    level="warn")
                await self._abort(flow_id)
                return None
            # WHAT THIS PAGE NEEDS, said before it fails to load.
            #
            # An account sign-in is NOT a Tailscale auth key. There is no
            # code to copy: the provider hands the authorisation back by
            # REDIRECT, to a callback address that must reach Home
            # Assistant. So the browser doing this needs BOTH -- the
            # internet, and a route to this hub -- and a phone sitting on
            # the hub's own access point has neither route out nor any way
            # to be handed a key instead (the AP is deliberately built
            # with no NAT; see pi-network-setup.sh).
            #
            # The house Wi-Fi satisfies both at once, and so does the
            # tailnet once this hub has joined one -- which is a good
            # reason to do Tailscale FIRST and every account sign-in
            # afterwards.
            await self._io.send_data("open", {
                "screen": "web",
                "url": url,
                "title": step.get("title") or "Sign in",
                "note": "This phone needs the internet for this page. "
                        "Switch to your house Wi-Fi (or the tailnet, once "
                        "this hub has joined one) -- the hub is reachable "
                        "there too, so YOU DO NOT LOSE THE CONNECTION by "
                        "switching. The hub's own Wi-Fi has no route out "
                        "by design. And unlike a Tailscale key there is "
                        "no code to copy back: the provider hands the "
                        "answer straight to Home Assistant, so whichever "
                        "browser signs in must also be able to reach this "
                        "hub.",
            })
            await self._io.write(
                "\n  Opening the sign-in page. Finish signing in there and "
                "this will carry on by itself -- there is nothing to answer "
                "here.")
            waiting = "Waiting for the sign-in to finish"
            timeout = self.EXTERNAL_STEP_TIMEOUT
        else:  # progress
            action = str(step.get("progress_action") or "").replace("_", " ")
            await self._io.write(
                f"\n  {(action or 'Working').capitalize()}...")
            # Pairing steps almost always need a HUMAN to do something -- hold
            # a button, pull a tab, power-cycle a bulb. Saying so beats a
            # spinner that looks like the hub is busy on its own.
            await self._io.write(
                "  If this is a pairing step, put the device into pairing mode "
                "now -- usually a button held for a few seconds.")
            waiting = "Waiting for the device"
            timeout = self.SHOW_PROGRESS_TIMEOUT

        deadline = time.monotonic() + timeout
        next_note = time.monotonic() + self.FLOW_NOTE_SECONDS
        while time.monotonic() < deadline:
            await asyncio.sleep(self.FLOW_POLL_SECONDS)
            nxt = await asyncio.to_thread(get_flow, flow_id)
            if nxt is None:
                await self._io.write(
                    "  Home Assistant is no longer tracking this set-up.",
                    level="warn")
                return None
            if nxt.get("type") != kind:
                return nxt
            if time.monotonic() >= next_note:
                # Proof of life. A silent minute reads as a hang, and the one
                # thing a person must not do here is give up and start again.
                left = int(deadline - time.monotonic())
                await self._io.write(f"  {waiting}... ({left}s left)")
                next_note = time.monotonic() + self.FLOW_NOTE_SECONDS

        await self._io.write(
            "  Nothing happened in time, so this set-up has been stopped. "
            "Nothing was saved -- you can start it again.", level="warn")
        await self._abort(flow_id)
        return None

    async def _abort(self, flow_id: Optional[str]) -> None:
        """Leave nothing half-open: an abandoned flow shows up in HA as a
        set-up in progress that nobody is driving."""
        if flow_id:
            await asyncio.to_thread(self.ha_client.abort_config_flow, flow_id)

    async def _submit(self, flow_id: Optional[str],
                      answers: Optional[Dict[str, Any]]
                      ) -> Optional[Dict[str, Any]]:
        """The next step after answering this one -- or, with `answers`
        None, after carrying it on with no answer (a GET). None, said, when
        Home Assistant did not answer."""
        if answers is None:
            nxt = await asyncio.to_thread(self.ha_client.get_config_flow,
                                          flow_id)
        else:
            nxt = await asyncio.to_thread(self.ha_client.submit_config_flow,
                                          flow_id, answers)
        if nxt is None:
            await self._io.write("  Home Assistant did not answer.",
                                 level="warn")
        return nxt
    #: How long to watch a step nobody can answer. Generous, because both are
    #: waiting on a human: reading a consent screen, or finding the button on
    #: the underside of a bulb.
    EXTERNAL_STEP_TIMEOUT = 300.0
    SHOW_PROGRESS_TIMEOUT = 180.0
    #: Gently. HA is on the same box; this costs nothing and a tighter poll
    #: buys no responsiveness a person could perceive.
    FLOW_POLL_SECONDS = 2.0
    #: How often to say "still waiting" so a long step cannot look like a hang.
    FLOW_NOTE_SECONDS = 30.0
    async def _run_config_flow(self, step: Dict[str, Any]) -> bool:
        """Drive an HA config flow to completion, one step at a time.

        `step` is whatever start_config_flow or a pending-flow entry gave us.
        Each iteration renders the current form, submits it, and receives the
        next step -- exactly what HA's own frontend does, through prompts.

        PREFILL USED TO LIVE HERE and is deliberately gone. It let a caller
        answer steps it already knew, and the only caller that ever did was
        _adopt_flashed_node -- deleted in the Tasmota migration, because the
        hub now provisions its own boards and they never reach a config flow
        at all. Both branches were unreachable, and so was the helper that
        fed them.
        """
        flow_id = step.get("flow_id")
        # A DISCOVERED flow is not a step. config_entries/flow/progress lists
        # {flow_id, handler, step_id, context} -- no `type`, no schema -- so
        # handing one straight to the loop below ended on "Unexpected step
        # type None" for every device HA had found. Ask HA for the step the
        # flow is actually on (GET re-renders it without answering).
        if not step.get("type") and flow_id:
            current = await asyncio.to_thread(
                self.ha_client.get_config_flow, flow_id)
            if current is None:
                await self._io.write(
                    "  That set-up is no longer waiting in Home Assistant.",
                    level="warn")
                return False
            step = current
            flow_id = step.get("flow_id", flow_id)
        for _ in range(self.MAX_FLOW_STEPS):
            kind = step.get("type")

            if kind == "create_entry":
                title = step.get("title") or "the device"
                await self._io.write(f"  [OK] {title} is set up.")
                return True

            if kind == "abort":
                # An abort is often NOT an error: "already_configured" means
                # somebody set it up while this was open.
                reason = str(step.get("reason") or "unknown")
                await self._io.write(
                    f"  Set-up stopped: {reason.replace('_', ' ')}",
                    level="info" if reason == "already_configured" else "warn")
                return reason == "already_configured"

            if kind == "menu":
                # HA offering a choice of routes -- "connect over the cloud or
                # locally", "reconfigure or re-authenticate". Plenty of
                # integrations open with one, so before this they died on
                # their FIRST step, having shown nothing at all.
                picked = await self._flow_menu(step)
                if picked is None:
                    await self._abort(flow_id)
                    raise CancelledByUser()
                nxt = await self._submit(flow_id, {"next_step_id": picked})

            elif kind in ("external", "external_done",
                          "progress", "progress_done"):
                # Two names for one mechanic: a step with nothing to answer,
                # which Home Assistant advances by itself. A browser sign-in
                # finishes when the site redirects back; a Zigbee pairing
                # finishes when the bulb joins. Neither sends us anything, so
                # neither can be awaited -- they have to be watched.
                #
                # THOSE ARE HOME ASSISTANT'S OWN NAMES (FlowResultType:
                # "external", "progress", and their *_done). This tested
                # "external_step" / "show_progress" -- the Python method
                # names, which never appear on the wire -- so every browser
                # sign-in and every pairing step ended on "Unexpected step
                # type".
                nxt = await self._await_flow_advance(step, flow_id)

            elif kind != "form":
                await self._io.write(f"  Unexpected step type {kind!r}.", level="warn")
                return False

            else:
                # Errors from the PREVIOUS submission, shown before asking again.
                for where, msg in (step.get("errors") or {}).items():
                    await self._io.write(f"  ! {where}: {msg}", level="warn")

                schema = step.get("data_schema") or []
                answers: Dict[str, Any] = {}
                try:
                    # ONE FORM, not one question per field. A six-field schema
                    # asked as six screens gives no way to see that host and
                    # port belong together, or to look back at what you typed.
                    fields = self._flow_fields(schema)
                    if fields:
                        title = (step.get("title")
                                 or f"Set up {step.get('handler', 'this device')}")
                        result = await self._io.form(
                            title, fields,
                            note="Home Assistant needs these to reach the device.")
                        if result is None:
                            raise CancelledByUser()
                        answers = self._coerce_flow_answers(
                            schema, {k: v for k, v in result.items()
                                     if v not in (None, "")})
                except ValueError as e:
                    await self._io.write(
                        f"  {e}. Finish this one in Home Assistant's interface.",
                        level="warn")
                    await self._abort(flow_id)
                    return False
                except CancelledByUser:
                    await self._abort(flow_id)
                    raise
                nxt = await self._submit(flow_id, answers)

            if nxt is None:
                return False
            step = nxt
            flow_id = step.get("flow_id", flow_id)

        await self._io.write(f"  Set-up did not finish within "
                             f"{self.MAX_FLOW_STEPS} steps.", level="warn")
        return False
    # _adopt_flashed_node USED TO LIVE HERE, and is deliberately gone.
    #
    # It started an ESPHome config flow against a freshly flashed board,
    # filling in the IP read from the boot log and the encryption key this
    # hub had generated. All of that existed to answer a form nobody should
    # have had to see in the first place.
    #
    # There is no form now. Home Assistant's Tasmota integration finds a
    # board through the message broker by itself, so the hub's only job
    # after flashing is to WAIT -- which settle_new_entities below already
    # did for every other path. Deleted rather than left unreferenced: a
    # dead adoption path is exactly the sort of thing someone restores in a
    # hurry two years from now.

    # A BOARD BECOMING DEVICES IS NOT HERE. Registering an ESP board's
    # devices -- and settling the registry after an edit -- lives in
    # esp_firmware/board_registry.py and never touches Home Assistant. This
    # file is for Home Assistant's own integrations only.

    # ── The surface the shell actually calls ────────────────────────────
    run = _run_config_flow
    start_manual = _start_manual_flow

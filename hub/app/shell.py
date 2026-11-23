"""
main.py — Production CLI for the semantic home command system.
"""

import string
import sys
import os
import asyncio
import contextvars
from datetime import datetime
import re
import textwrap
import threading
import traceback
from typing import Optional, List, Dict, Any, Set, Tuple, Sequence, Callable
import math
import time
import base64 

import numpy as np

from nlu.model_module import (load_model, embed, embedding_dim, get_backend,
                              build_intent_index, IntentClassifier,
                              intent_index_exists, resolve_index_path)
from nlu.device_manager import DeviceManager, ACTOR as _ACTOR
from nlu.scene_manager import SceneManager
from nlu.slot_extractor import SlotExtractor
from nlu.lexicon import (asks_existence, clock_question, is_inventory_question,
                         is_rooms_question,
                         room_only_query,
                         run_pre_filters, is_premises_query, is_whats_on_query,
                         asks_purpose, purpose_by_meaning,
                         UNDO_PHRASES, UNDO_LEADERS, ROOM_STOP_WORDS)
from nlu.command_splitter import split_commands
from nlu.resolver import NO_DEVICE, resolve_command, _has_explicit_action
from nlu.executor import (Executor, BINARY_ENVELOPE,
                          is_binary_envelope, describe_reading, NO_READING,
                          describe_envelope, switch_from_state_words)
from core.config import (MPNET_MODEL_PATH, INTENT_INDEX_PATH, init_directories,
                    INTENT_MIN_SCORE, INTENT_MIN_MARGIN,
                    MAX_ENROLL_RETRIES as config_MAX_ENROLL_RETRIES,
                    MAX_FIRST_RUN_ATTEMPTS as config_MAX_FIRST_RUN_ATTEMPTS,
                    MAX_STALE_REPLY_FRAMES as config_MAX_STALE_REPLY_FRAMES,
                    MAX_AUDIO_B64_LEN as config_MAX_AUDIO_B64_LEN,
                    SV_SCORE_THRESHOLD,
                    PIN_MIN_LENGTH, PIN_MAX_LENGTH,
                    MENU_GATE_GRACE_SECONDS)
from app.setup_manager import run_setup
# event() and CancelledByUser live in hv_common rather than here: ha_flows and
# esp_builder both need them, and this module imports THEM -- so importing
# back would be a cycle Python reports as a confusing half-built module.
from core.hv_common import (event, CancelledByUser, means_cancel,
                       normalise_person_name, person_name_problem,
                       timed, times_add)
from identity.people import PeopleJournal
from home_assistant.ha_flows import ConfigFlowRunner
from esp_firmware.ir_flows import IRFlowRunner
from esp_firmware.esp_builder import EspBuilder
from esp_firmware.board_registry import BoardRegistrar
from esp_firmware import chips

from voice_security.biometric_store import BiometricStore
from voice_security.sv import SherpaSV
from voice_security.fallback_auth import FallbackAuthManager
from voice_security.enrollment_handler import EnrollmentHandler
# The card store sits beside the PIN and recovery stores, because a rename
# has to move all three and that file is where two of them already do it.
# Imported the way test_profile_flows.py imports it -- bare, from the
# project root -- rather than guessed into a package it does not live in.
from voice_security.pin_auth import CardStore
from voice_security import audio_io

try:
    from nlu.llm_describer import describe_device, describe_scene
    _LLM_AVAILABLE = True
except Exception as _e:  # not just ImportError -- a missing model file or a
    # bad config raised at import time took the whole CLI down, when the
    # correct behaviour is the same graceful degradation as a missing module.
    print(f"[llm] Description helper unavailable ({_e}) -- "
          f"AI description generation is disabled.")
    _LLM_AVAILABLE = False

MODEL_DIR = MPNET_MODEL_PATH  # single source of truth: config.py

# INTENT_MIN_SCORE / INTENT_MIN_MARGIN moved to config with the other
# tunables; imported at the top of the file with the rest.

# UNDO_PHRASES, UNDO_LEADERS and CANCEL_TOKENS were defined here -- three
# English word lists in a module that should own none. They live in lexicon.py
# section 7 now (the first two are imported above; nothing here reads
# CANCEL_TOKENS), with the "abort" overlap documented there rather than in
# two places.

# THE ENVELOPE VOCABULARY AND THE DESCRIBE FUNCTIONS LIVE IN nlu.executor
# (moved 2026-08-29, to take weight off this file).
#
# DEFAULT_ENVELOPE, BINARY_ENVELOPE, is_binary_envelope, describe_reading, NO_READING,
# and describe_envelope went there rather than to resolver.py:
# resolver is a pure DECISION and its own docstring says it never prints, so
# a family of rendering functions would contradict the rule that file is
# built on. executor already owns this whole vocabulary -- state_key,
# range_min, range_max, step, and bool-versus-number -- because build_command
# does the arithmetic on it.
#
# It was not only a move. build_command's fallbacks were the bare literals
# 10.0 / 0.0 / 100.0: the same three numbers as DEFAULT_ENVELOPE, in another
# file, with nothing holding them equal. That constant exists BECAUSE two
# inline copies had drifted once already; this was the third copy and it was
# missed. It reads the constant now.

# ROOM_STOP_WORDS moved to lexicon.py section 7 with the rest.




def describe_audio(audio) -> str:
    """Samples and seconds, for a trace line. Never raises -- a logging call
    must not be the thing that kills a command."""
    try:
        n = len(audio)
        return f"{n} samples ({n / 16000.0:.2f}s @16k)"
    except Exception:
        return "unmeasurable audio"




#: Put the words themselves in trace lines (see loggable). Off: a debugging
#: switch, not a setting.
LOG_TRANSCRIPTS = False


def loggable(text) -> str:
    """A transcript, challenge phrase or heard text as it may appear in a
    trace line: the words only when LOG_TRANSCRIPTS is on, else
    just their length. event() goes to stdout and therefore to journald or
    a log file, and a privacy-preserving hub does not keep what was said."""
    if LOG_TRANSCRIPTS:
        return repr(text)
    try:
        return f"<{len(text)} chars>"
    except TypeError:
        return "<?>"


async def ainput(prompt: str) -> str:
    return await asyncio.to_thread(input, prompt)


class IOBackend:
    """Where a shell's prompts actually go. The CLI and the network server
    need the exact same command-flow logic (voice gate, challenges, PIN
    fallback, picking) -- the only thing that differs is where a question
    gets asked and where the answer comes from. Swapping this one object is
    the whole adaptation; nothing in the command flow itself needs to know
    or care which backend it's talking to."""

    #: The value of the row that says "no" to a yes/no question and "none of
    #: those" to a list. Never a device id (real values do not start with a
    #: space).
    _CHOICE_NONE = " __none__"


    #: Does an answer to this backend come from someone PHYSICALLY PRESENT
    #: at the machine?  A terminal: yes -- reaching the keyboard already
    #: required getting into the house.  A socket: no, however well
    #: authenticated the transport is.
    #:
    #: This exists because _voice_gate used to treat "no audio attached" as
    #: "CLI typing, nothing to verify, allow" -- which is true at a keyboard
    #: and catastrophically false over a network, where omitting one JSON
    #: field skipped every voice check including the one guarding locks.
    #: The gate cannot tell the two apart from the audio alone, so the
    #: backend has to say.
    trusted_local = False

    async def wait_hold(self) -> None:
        """Wait while something said over the running command is still being
        read (the phone's `hold`). Only a phone can hold; this is the
        backend with nothing to wait for."""
        return

    def raise_if_cancelled(self) -> None:
        """A CHECKPOINT: stop here if the person pressed Cancel. Called
        before anything is changed (shell._carry_out), so a cancelled
        command never reaches a device. A backend with no cancel channel
        (the terminal) never stops here."""
        asked = getattr(self, "is_cancelled", None)
        if asked is not None and asked():
            raise CancelledByUser()

    #: Can this backend DRAW a data frame, or does it only have words?
    #:
    #: A terminal needs every fact printed; a phone renders send_data and
    #: would show the same facts twice. The app used to solve that at ITS
    #: end, with a "swallow the next lines" flag -- which is a latch, and a
    #: latch that fails to clear leaves the feed silently eating output with
    #: nothing on screen saying why. The backend knows this for certain and
    #: cannot get stuck, so the backend decides.
    renders_data = False

    #: Can this backend hand over files (file_request)? Only a
    #: phone whose app says so in its auth frame.
    can_send_files = False

    async def pick_files(self, text: str, *, purpose: str,
                         accept: Sequence[str], multiple: bool = True,
                         max_bytes: int = 5_000_000,
                         family: str = "") -> List[Dict[str, Any]]:
        """Ask the client for files: [{name, data (base64)}]. A cancel raises
        CancelledByUser. Only called when can_send_files is True."""
        raise NotImplementedError

    async def ainput(self, prompt: str, *,
                     meta: Optional[Dict[str, Any]] = None,
                     patient: bool = False, secret: bool = False) -> str:
        """`secret` marks an answer that must not be shown as it is typed --
        a Wi-Fi password, an encryption key. The SERVER decides this, because
        the alternative is the client guessing from the prompt text, and that
        guess was wrong in both directions: it hid GPIO pin numbers because
        the word "pin" appears in "Data pin", while showing the API key in
        full. A client that obscures a field must also offer to reveal it;
        people mistype passwords.

        `patient` marks a question whose answer is NOT in the person's
        head -- today only the recovery phrase, which is on paper in a
        drawer. A network backend widens (or removes) its reply window for
        it; a terminal ignores it, having none.

        `meta` is OPTIONAL STRUCTURED CONTEXT about the question -- e.g.
        {"wizard": {"index": 2, "total": 6, "last": False}} on a multi-field
        update flow, so a graphical client can render Next/Back buttons and
        know when Next becomes Finish. A terminal ignores it entirely; the
        text prompt remains the complete question on its own."""
        raise NotImplementedError

    async def get_input(self, prompt: str, *,
                        meta: Optional[Dict[str, Any]] = None,
                        patient: bool = False, secret: bool = False) -> str:
        """ainput, but it will not take an empty answer for an answer.

        `meta` MEANS THE SAME THING IT MEANS ABOVE, and it is here because
        it was not (2026-09-12). ainput has carried `meta` since the wizard
        steps needed it; get_input -- the LOOPING half of the same contract,
        and the one every required field goes through -- never grew the
        parameter, on any of the four backends.

        `menu_rename_user` passed it anyway:

            new = (await self._io.get_input(
                "  New name: ",
                meta={"hint": "letters, digits, dot, underscore or hyphen"}))

        which is a TypeError on every single call, caught by
        run_menu_action's blanket handler and shown as "Change your name
        stopped unexpectedly -- the server console has the details". So
        Change your name has never once worked, from a phone or from the
        hub's own keyboard, and the only report it could produce was that
        sentence (her screenshot, 12 Sep).

        Adding the parameter rather than deleting the argument, because the
        hint is right and the asymmetry was the actual fault: the two halves
        of one contract must take the same question."""
        raise NotImplementedError

    async def write(self, text: str, *, level: str = "info") -> None:
        """Send a line of RESULT text -- not a question, nothing is awaited
        back. This is the half of the IO contract that was missing until
        now: ainput/get_input carry questions to the client, but every
        user-facing result ("Turned on the AC", a device listing, a scene
        firing) went through bare print() -- fine for the CLI, invisible to
        the phone, since WebSocketIO had no way to emit anything that wasn't
        a question. See the call sites in InteractiveShell that were
        converted from print() to await self._io.write() alongside this.

        RULE OF THUMB for which to use: anything the person must READ IN
        ORDER TO ANSWER a question goes through write() -- option lists,
        room lists, challenge phrases, the reason a check failed. Internal
        traces (resolver rung, similarity scores, split previews) stay on
        print(), since they are for whoever is watching the server console.

        `level` marks text the person MUST NOT MISS -- currently only the
        one-time PIN recovery phrase. A terminal boxes it; a graphical client
        is expected to surface it rather than scroll it past in a log pane,
        which is where it went when everything shared one severity."""
        raise NotImplementedError

    async def choose(self, prompt: str, options, *,
                     multi: bool = False, allow_all: bool = False,
                     cancel_label: str = "", polar: bool = False):
        """Ask the person to select from a fixed list. Returns a list of the
        chosen options' `value` strings; an empty list means cancelled.

        `cancel_label` renames the screen's own Cancel control for this one
        question. It exists because "Cancel" alone is a poor answer to a
        destructive question: walking away from "overwrite this chip?" has a
        CONSEQUENCE, and the button should name it ("Cancel -- leave the chip
        as 'kitchen'"). Empty means the client's default wording.

        THE ALTERNATIVE WAS WORSE. Both overwrite questions used to carry
        their own "Cancel" ROW in the options list, so the screen had two
        cancels -- a row and a button -- meaning the same thing, and the two
        variants did not even agree on whether the row came first or last
        (her call, 2026-08-29: one shape, Cancel is the button).

        `polar` marks a YES/NO question about the first option ("Did you
        mean the lamp?"): "yes" returns that option, "no" returns
        [CHOICE_NONE], and an empty list is still cancelled.

        `options` is a list of {"value", "label", "sub"} dicts -- data, not
        rendered text. That distinction is the whole point. Until now the
        options were formatted into numbered lines and pushed through write(),
        so the only way for a client to build real controls was to regex the
        output stream and hope the format never changed. The caller states
        WHAT is being chosen; the backend decides how to present it. A
        terminal draws a numbered list, a phone draws buttons, and neither
        the caller nor the other backend has to know.
        """
        raise NotImplementedError
    async def capture_audio(self, prompt: str, *, phrase: str = "",
                            index: Optional[int] = None,
                            total: Optional[int] = None,
                            context: str = "recording"):
        """Obtain a recording of the person saying `phrase`.
 
        Returns 16k mono float32, or None if nothing usable arrived (the
        caller may retry). Raises CancelledByUser if they explicitly abort.
 
        The third question type, after "type something" (ainput) and "pick
        something" (choose): "say something". Same principle -- the caller
        states what it needs, the backend decides how to obtain it. A terminal
        asks for a file path; a phone records from its microphone.
 
        `context` is a hint for the client ("enrollment", "challenge"), not a
        behavioural switch -- the audio comes back the same way regardless.
        """
        raise NotImplementedError
 
    async def set_view(self, name: str) -> None:
        """Tell the client which screen it should be showing.
 
        A no-op on a terminal, which has exactly one screen. It exists so a
        multi-step flow like enrollment can put a graphical client into a
        dedicated mode and take it back out again, without the flow itself
        knowing whether anything is listening.
        """
        return None

    async def form(self, title: str, fields: List[Dict[str, Any]],
                   note: str = "") -> Optional[Dict[str, Any]]:
        """Ask for a WHOLE set of fields at once. None means cancelled.

        The fifth message type, and the one Home Assistant's config flows
        actually need. Asking a six-field schema as six separate questions
        means six screens, no way to look back at what you already typed,
        and no way to see that "host" and "port" belong together -- which is
        exactly how "what is the host field?" happens.

        A terminal has no forms, so IOBackend's default walks the fields as
        individual prompts: identical behaviour to before, no CLI change.

        Each field: {name, label, type, default, required, help, choices}.
        `help` is the important one -- it is where a field like `host` gets
        to say "the ESP32's address on your network, e.g. 192.168.1.42".
        """
        answers: Dict[str, Any] = {}
        if title:
            await self.write(f"\n{title}")
        if note:
            await self.write(f"  {note}")
        for field in fields:
            label = field.get("label") or field.get("name") or "value"
            if field.get("help"):
                await self.write(f"  ({field['help']})")
            default = field.get("default")
            hint = f" [{default}]" if default not in (None, "") else ""
            raw = (await self.ainput(f"  {label}{hint}: ")).strip()
            if means_cancel(raw):
                return None
            if not raw:
                if default not in (None, ""):
                    answers[field["name"]] = default
                elif field.get("required"):
                    await self.write("  That one is required.", level="warn")
                    return None
                continue
            answers[field["name"]] = raw
        return answers

    async def registration(self, spec: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """The one registration screen, walked as prompts on a terminal.

        The app draws the whole thing at once (`WebSocketIO.registration`);
        here the same fields are asked in order, prefilled from
        `spec["values"]`, and Enter keeps a prefill. Returns
        {"action": "generate"|"save", "values": {...}} -- exactly what the
        app sends, so `_registration_loop` cannot tell the two apart; a
        cancel raises CancelledByUser, which is what the loop makes of the
        app's None. An error the loop attached to the spec is printed first.
        """
        values = dict(spec.get("values") or {})
        if spec.get("title"):
            await self.write(f"\n{spec['title']}")
        if spec.get("source"):
            await self.write(f"  {spec['source']}")
        if spec.get("error"):
            await self.write(f"  {spec['error']}", level="warn")
        if spec.get("ai_text"):
            await self.write(f"  The AI wrote: \"{spec['ai_text']}\"")

        async def ask(key: str, label: str, *, allow_empty: bool = True) -> None:
            cur = str(values.get(key) or "")
            hint = f" [{cur}]" if cur else ""
            raw = (await self.ainput(f"  {label}{hint}: ")).strip()
            if means_cancel(raw):
                raise CancelledByUser()
            if raw:
                values[key] = raw
            elif not cur and not allow_empty:
                await self.write("  That one is required.", level="warn")
                raise CancelledByUser()

        async def yes(question: str, default: bool = False) -> bool:
            raw = (await self.ainput(
                f"  {question} [{'Y/n' if default else 'y/N'}]: ")).strip().lower()
            if means_cancel(raw):
                raise CancelledByUser()
            return raw not in ("n", "no") if default else raw in ("y", "yes")

        rooms = [r for r in (spec.get("rooms") or []) if r]
        if rooms:
            await self.write(f"  Rooms so far: {', '.join(rooms)}")
        for key, label, req in (("room", "Room", True), ("name", "Device name", True),
                                ("does", "What does it do", False)):
            await ask(key, label, allow_empty=not req)
        if (spec.get("ai_available") and not spec.get("ai_text")
                and await yes("Let the AI fill in how it behaves?")):
            return {"action": "generate", "values": values}
        options = [o for o in (spec.get("kind_options") or []) if isinstance(o, dict)]
        allowed = [o["kind"] for o in options if o.get("enabled", True)] or list(InteractiveShell._KINDS)
        if spec.get("kind_locked") or len(allowed) == 1:
            # The one kind the part allows -- whatever an older record
            # said before the part behind it changed.
            if values.get("kind") not in allowed:
                values["kind"] = allowed[0]
            await self.write(f"  Kind: {values.get('kind') or '?'} (declared by the part)")
        else:
            greyed = [f"{o['kind']} ({o.get('why')})" for o in options if not o.get("enabled", True)]
            if greyed:
                await self.write("  Not for this part: " + "; ".join(greyed))
            await ask("kind", f"Kind ({'/'.join(allowed)})", allow_empty=False)
        kind = str(values.get("kind") or "").lower()
        position_kinds = tuple(spec.get("position_kinds") or ("level",))
        if kind == "level" or (kind == "open" and kind in position_kinds) or kind == "reading":
            optional = kind == "reading"
            if optional:
                await self.write("  Its range and step (leave empty if you don't know them):")
            for key, label in (("min", "Min"), ("max", "Max"), ("step", "Step"), ("unit", "Unit")):
                await ask(key, label, allow_empty=optional or key == "unit")
        values["secure"] = await yes("Security device (voice/PIN before it moves)?")
        if not spec.get("state_known") and kind != "reading":
            words = {"lock": "locked/unlocked", "open": "open/closed",
                     "level": "a number"}.get(kind, "on/off")
            await ask("state", f"Right now it is ({words})", allow_empty=False)
        values["show_on_home"] = await yes("Show on the home screen?", default=True)
        return {"action": "save", "values": values}

    async def pin_map(self, payload: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """Hand the whole board to the client and get it back laid out.

        The SIXTH message type, and the only one that is a screen rather
        than a question. Assigning pins by question is nine screens with no
        way to see the board; the client that can draw one should draw one.

        None means "this client has no pin screen" -- NOT "cancelled". The
        default is None because a terminal cannot draw a board, and the
        caller falls back to asking. Cancelling is an empty items list.
        """
        return None

    async def send_data(self, kind: str, payload: Dict[str, Any]) -> None:
        """Structured data for clients that can render it.

        The fourth message type, after ask/pick/say: "here is DATA". A no-op
        everywhere except the network backend -- the terminal already renders
        the text version of the same facts, and an older client that does not
        know the frame type simply ignores it, so this is additive.
        """
        return None

    async def rule_builder(self, payload: Dict[str, Any]
                           ) -> Optional[Dict[str, Any]]:
        """Hand the whole automation to the client and get it back built.

        The SEVENTH message type, and the pin screen's twin (her design,
        2026-08-27: "do it like the way Tinkercad does it... you select
        the device and then do if else statements"). One message out
        carrying every device, what each can be asked, the scenes and the
        rule being edited; one message back carrying the whole rule.

        WHY A WHOLE RULE AND NOT TWENTY QUESTIONS: because nothing is
        committed while a person builds. They can set the THEN and then
        change their mind about the WHEN, wander around their own rule,
        and only Save sends anything. That is the difference between a
        builder and an interrogation.

        None means "this client has no builder" -- NOT "cancelled" -- so
        the caller falls back to asking, exactly as pin_map does. A
        terminal cannot draw blocks, and the spoken ribbon stays the voice
        face of the same door. Cancelling is an empty mapping.
        """
        return None

    async def scene_builder(self, payload: Dict[str, Any]
                            ) -> Optional[Dict[str, Any]]:
        """The scene screen: the whole scene out, the person's next step
        back ({"action": "save" | "try" | "generate", "scene": {...}}, or
        {"cancelled": True}). None: this client has no scene screen."""
        return None


class TerminalIO(IOBackend):
    """Default backend -- delegates to the exact same module-level functions
    that have always driven the CLI. Behavior is byte-for-byte unchanged."""

    trusted_local = True   # you are at the machine; see IOBackend.trusted_local

    async def ainput(self, prompt: str, *,
                     meta: Optional[Dict[str, Any]] = None,
                     patient: bool = False, secret: bool = False) -> str:
        # meta is for screens and patient is for reply windows; a terminal
        # has neither. `secret` it DOES honour: a PIN typed through input()
        # sat in the console and its scrollback, so a masked field goes
        # through getpass (off-loop, like input()).
        if secret:
            import getpass
            return await asyncio.to_thread(getpass.getpass, prompt)
        return await ainput(prompt)

    async def get_input(self, prompt: str, *,
                        meta: Optional[Dict[str, Any]] = None,
                        patient: bool = False, secret: bool = False) -> str:
        # `meta` is accepted and ignored, exactly as ainput ignores it here:
        # a terminal has no Next/Back buttons to label and no hint line to
        # draw, and the text prompt is the whole question on its own. It is
        # in the signature so a caller cannot crash one backend and not the
        # other -- which is the bug this parameter was added for.
        #
        # `secret` IS honoured, exactly as ainput honours it: Set PIN and
        # Forgot PIN ask for the new PIN through get_input(secret=True), and
        # a plain input() echoed the PIN onto the console and its scrollback.
        while True:
            value = (await self.ainput(prompt, secret=secret)).strip()
            if means_cancel(value):
                raise CancelledByUser()
            if value:
                return value
            print("Please enter a value (or 'cancel' to abort).")

    async def write(self, text: str, *, level: str = "info") -> None:
        if level == "important":
            print("\n" + "!" * 60)
            print(text)
            print("!" * 60)
            return
        print(text)

    #: Data frames the TERMINAL has to draw itself, because nothing else
    #: prints them. The base class's no-op is documented as safe on the
    #: grounds that "the terminal already renders the text version of the
    #: same facts" -- which is true of device_list and false of these two.
    #: Board credentials produced a menu action that logged "credentials
    #: shown for 'bedroom'" and displayed nothing at all; pending notices
    #: were invisible from the CLI in exactly the same way.
    #:
    #: device_list is deliberately absent: it has a text version four lines
    #: after its send_data call, and drawing it here would print the device
    #: table twice.
    _DRAWN_IN_TERMINAL = ("secret", "notice")

    async def send_data(self, kind: str, payload: Dict[str, Any]) -> None:
        if kind not in self._DRAWN_IN_TERMINAL:
            return None
        title = payload.get("title") or payload.get("body") or kind
        print()
        print("  " + "-" * 58)
        print(f"  {title}")
        print("  " + "-" * 58)
        for key in ("note", "body"):
            text = payload.get(key)
            if text and text != title:
                print(textwrap.fill(str(text), width=60,
                                    initial_indent="  ",
                                    subsequent_indent="  "))
        if payload.get("value"):
            print()
            print(f"    {payload['value']}")
        items = payload.get("items") or []
        if items:
            print()
            if payload.get("items_label"):
                print(f"  {payload['items_label']}:")
            for item in items:
                print(f"    {item}")
        if payload.get("action_label"):
            print()
            print(f"  -> {payload['action_label']}")
        print()
        return None

    async def choose(self, prompt: str, options, *,
                     multi: bool = False, allow_all: bool = False,
                     cancel_label: str = "", polar: bool = False):
        # Renders exactly what _pick_one/_pick_multi used to render, so the
        # CLI is unchanged: numbered list, 'a' for all, blank cancels,
        # re-prompt on a typo.
        if not options:
            return []
        for i, opt in enumerate(options, 1):
            label = opt.get("label") or opt["value"]
            sub = f" — {opt['sub']}" if opt.get("sub") else ""
            ident = f"   ({opt['value']})" if opt["value"] != label else ""
            print(f"  {i:>2}. {label}{sub}{ident}")
        if multi and allow_all:
            print("   a. all of them")

        if multi:
            question = (f"{prompt} — number(s) like 1 or 1,3"
                        + (", or 'a' for all" if allow_all else "")
                        + " (Enter to cancel): ")
        else:
            question = (f"{prompt} — enter a number 1-{len(options)} "
                        f"(or press Enter to cancel): ")

        while True:
            try:
                ans = (await ainput(question)).strip().lower()
            except (EOFError, KeyboardInterrupt):
                print()
                return []
            if not ans:
                return []
            if multi and allow_all and ans in ("a", "all"):
                return [o["value"] for o in options]
            picked, bad = [], False
            for tok in (ans.replace(" ", "").split(",") if multi else [ans]):
                if tok.isdigit() and 1 <= int(tok) <= len(options):
                    v = options[int(tok) - 1]["value"]
                    if v not in picked:
                        picked.append(v)
                else:
                    bad = True
                    break
            if picked and not bad:
                return picked if multi else picked[:1]
            print(f"  '{ans}' isn't valid. Use numbers 1-{len(options)}"
                  + (", 'a' for all" if (multi and allow_all) else "")
                  + ", or Enter to cancel.")
    async def capture_audio(self, prompt: str, *, phrase: str = "",
                            index: Optional[int] = None,
                            total: Optional[int] = None,
                            context: str = "recording"):
        if phrase:
            counter = f" {index}/{total}" if index and total else ""
            print(f'\n  Phrase{counter}: "{phrase}"')
        try:
            path = (await ainput(f"    {prompt} (or 'cancel'): ")).strip()
        except (EOFError, KeyboardInterrupt):
            print()
            raise CancelledByUser() from None
        if means_cancel(path):
            raise CancelledByUser()
        if not path:
            return None                      # empty -> caller re-prompts
        audio = audio_io.decode_file(path)
        if audio is None:
            print(f"    [audio] could not read/decode: {path}")
        return audio


class NullIO(IOBackend):
    """Nobody is listening. Every question fails fast instead of blocking.

    The correct neutral backend for a shell with NO connection attached.
    TerminalIO used to fill that role on the server, which was wrong twice
    over: its ainput() calls input() on the process's stdin -- in a daemon
    that blocks a worker thread forever on a read nobody will ever answer --
    and it declares trusted_local=True, so any stray prompt-then-gate
    sequence between connections would have run with terminal trust on a
    machine where no terminal is involved.

    write() still goes to the console, because a detached shell producing
    output is a diagnostic worth seeing."""

    trusted_local = False

    async def ainput(self, prompt: str, *,
                     meta: Optional[Dict[str, Any]] = None,
                     patient: bool = False, secret: bool = False) -> str:
        raise CancelledByUser()

    async def get_input(self, prompt: str, *,
                        meta: Optional[Dict[str, Any]] = None,
                        patient: bool = False, secret: bool = False) -> str:
        raise CancelledByUser()

    async def write(self, text: str, *, level: str = "info") -> None:
        print(f"[detached] {text}")

    async def choose(self, prompt: str, options, *,
                     multi: bool = False, allow_all: bool = False,
                     cancel_label: str = "", polar: bool = False):
        raise CancelledByUser()

    async def capture_audio(self, prompt: str, *, phrase: str = "",
                            index=None, total=None, context: str = "recording"):
        raise CancelledByUser()


class WebSocketIO(IOBackend):
    """Network backend -- a 'prompt' becomes a JSON message sent to the
    client, and the 'answer' is whatever the client sends back on the same
    connection. One backend per connection, so concurrent connections never
    share or clobber each other's pending question -- there is no shared
    global state here, unlike monkey-patching the module-level ainput/
    get_input functions would have been."""

    trusted_local = False  # a signed socket is not a person in the room
    renders_data = True    # it draws send_data frames; do not print them twice

    # Which reply frame answers which question. Replies used to be matched to
    # questions BY ARRIVAL ORDER -- every ask did a bare recv and read the
    # field it wanted -- so any stray frame satisfied whatever happened to be
    # outstanding. The concrete failure: a client that re-sent a recording
    # (because its UI gave no "submitted" feedback) had that duplicate consumed
    # by the NEXT capture_audio, so enrollment phrase 2 was stored holding
    # phrase 1's audio, silently, and the voiceprint was built from it.
    _ANSWERS = {
        "prompt":          ("prompt_reply",),
        "choice":          ("choice_response",),
        "record_required": ("recording",),
        "form":            ("form_response",),
        "registration":    ("registration_response",),
        "pin_map":         ("pin_map_response",),
        "automation_builder": ("automation_builder_response",),
        "scene_builder":   ("scene_builder_response",),
        "file_request":    ("file_response",),
    }

    # How many mismatched frames to skip before giving up. Bounded so a client
    # spraying frames can't pin a question open forever.
    MAX_STALE_FRAMES = config_MAX_STALE_REPLY_FRAMES

    def __init__(self, websocket, send_json, recv_json, drain=None):
        self.websocket = websocket
        self._send_json = send_json
        self._recv_json = recv_json
        # Discards replies queued BEFORE a question is asked. A stale frame
        # (yesterday's cancel, a double-send) must never auto-answer the
        # NEXT question -- that off-by-one made every reply land one
        # question behind, reading as "Reply cancels". ws_server passes its
        # _drain_replies here; a caller that doesn't gets the old behaviour.
        self._drain = drain

    async def _recv_answer(self, question: str, *, patient: bool = False):
        """Read replies until one actually answers `question`.

        A frame of the wrong type is DISCARDED, not consumed as the answer.
        An untyped frame is accepted, so an older client still works."""
        want = self._ANSWERS[question]
        for _ in range(self.MAX_STALE_FRAMES):
            reply = await self._recv_json(patient=patient)
            if not isinstance(reply, dict):
                continue
            rtype = reply.get("type")
            if rtype is None or rtype in want:
                return reply
            print(f"[ws-io] discarded stale {rtype!r} frame while awaiting "
                  f"{'/'.join(want)}")
        # Nothing usable arrived. Treat as an abandoned question rather than
        # looping: the caller's cancellation path is already correct.
        raise CancelledByUser()

    async def _exchange(self, frame: Dict[str, Any], question: str, *,
                        patient: bool = False) -> Dict[str, Any]:
        """Drain, send one question frame, and return the reply that answers
        it -- always a dict (see _recv_answer). A question starts from an
        EMPTY reply queue: anything queued before the person could even see
        it is stale by definition."""
        if self._drain is not None:
            self._drain()
        await self._send_json(frame)
        with timed("asked"):            # the person's own time, not the hub's
            return await self._recv_answer(question, patient=patient)

    async def pick_files(self, text: str, *, purpose: str,
                         accept: Sequence[str], multiple: bool = True,
                         max_bytes: int = 5_000_000,
                         family: str = "") -> List[Dict[str, Any]]:
        reply = await self._exchange(
            {"type": "file_request", "text": text, "purpose": purpose,
             "accept": list(accept), "multiple": multiple,
             "max_bytes": max_bytes,
             # One chip family's firmware at a time: two images of one
             # family fit a frame; six families' do not.
             **({"family": family} if family else {})},
            "file_request", patient=True)        # a picker takes its time
        if reply.get("cancelled") is True:
            raise CancelledByUser()
        files = reply.get("files")
        return [f for f in files if isinstance(f, dict)] \
            if isinstance(files, list) else []

    async def ainput(self, prompt: str, *,
                     meta: Optional[Dict[str, Any]] = None,
                     patient: bool = False, secret: bool = False) -> str:
        frame: Dict[str, Any] = {"type": "prompt", "text": prompt}
        if meta:
            frame["meta"] = meta  # wizard position etc. -- see IOBackend.ainput
        if secret:
            frame["secret"] = True
        reply = await self._exchange(frame, "prompt", patient=patient)
        # A PROMPT CAN BE CANCELLED LIKE EVERYTHING ELSE (2026-09-12).
        #
        # `prompt_reply` was the only answer frame in the protocol with no
        # cancelled form -- `recording`, `choice_response`, `form_response`,
        # `pin_map_response`, `automation_builder_response` and
        # `challenge_response` have all carried one from the start. So the
        # phone had to express a cancel as a VALUE: the word "cancel" for an
        # ordinary box, and an EMPTY STRING for a masked one (because the
        # word typed into a PIN field used to be checked as the PIN and cost
        # an attempt).
        #
        # That empty string is indistinguishable from "you pressed send on
        # an empty box", which get_input answers by asking again -- so
        # cancelling a PIN prompt looped for ever. Her report, and the two
        # places it bit: Forgot PIN (patient, so no timeout could even end
        # it) and Set PIN.
        #
        # AINPUT RETURNS EMPTY FOR IT rather than raising. Every one of this
        # method's callers already reads empty as "they backed out" -- the
        # PIN gate's `if not pin or means_cancel(pin)` is the pattern -- so
        # the flag changes no behaviour anywhere that works today. It is
        # get_input, the one that LOOPS on empty, that raises. An older app
        # that never sends the flag is unaffected: it sends the word, and
        # the word still works.
        #
        # It also ends a smaller bug: an answer of "cancel" typed into a
        # box whose caller forgot to check means_cancel used to be STORED
        # (a device renamed to "cancel"). A cancelled reply now carries no
        # value at all, whatever the client put in it.
        if reply.get("cancelled") is True:
            # Cancel means CANCEL (2026-09-23). Returning "" let every caller
            # that reads empty as "skip / keep / finish" carry on -- an open
            # Wi-Fi network, a skipped hint, an acknowledged recovery phrase
            # -- while the phone had already gone home.
            raise CancelledByUser()
        # str(), always: every caller .strip()s the answer, and a client
        # sending a number or a list here crashed the whole flow instead
        # of being told the answer did not parse.
        value = reply.get("value", "")
        return "" if value is None else str(value)

    async def get_input(self, prompt: str, *,
                        meta: Optional[Dict[str, Any]] = None,
                        patient: bool = False, secret: bool = False) -> str:
        while True:
            # `meta` rides EVERY re-ask, not just the first. A hint or a
            # wizard position that vanished the moment somebody pressed send
            # on an empty box would strip the screen of the very context
            # that explains why it is asking again.
            value = (await self.ainput(prompt, meta=meta, patient=patient,
                                       secret=secret)).strip()
            if means_cancel(value):
                raise CancelledByUser()
            if value:
                return value
            # AN EMPTY ANSWER TO A MASKED BOX IS A CANCEL, not a slip.
            #
            # The nudge below is right for a text box -- you pressed send
            # with nothing typed, here is your box back. It is wrong for a
            # PIN field, where an empty answer is the only cancel the app
            # can send that will not be checked as the PIN, and where the
            # nudge's own advice ("or 'cancel' to abort") cannot be taken:
            # the box is masked and digits-only.
            #
            # This is the compatibility half of the `cancelled` flag above.
            # A current app sends the flag and never reaches here; an older
            # one sends empty, and empty now ends the question instead of
            # re-asking it until the app is killed.
            if secret:
                raise CancelledByUser()
            # NOT an error frame: nothing failed, the person just pressed
            # send on an empty box. Error frames carry a machine-readable
            # reason and get logged red; this is a nudge.
            await self._send_json({
                "type": "output", "level": "warn",
                "text": "Please enter a value (or 'cancel' to abort).",
            })

    async def write(self, text: str, *, level: str = "info") -> None:
        # A WARNING THE PHONE SAW IS IN THE HUB'S LOG TOO, so why a flow
        # stopped can be read with journalctl, not only off a screen.
        if level == "warn" and text.strip():
            event("SAID", " ".join(text.split())[:300])
        await self._send_json({"type": "output", "text": text, "level": level})

    #: How many times a question may come back narrowed before it gives up
    #: and returns nothing. Two rounds of "did you mean" is help; a third
    #: is an argument.
    MAX_CHOICE_ROUNDS = 3

    async def choose(self, prompt: str, options, *,
                     multi: bool = False, allow_all: bool = False,
                     cancel_label: str = "", polar: bool = False):
        """Ask, and if the spoken answer was not clear enough to act on,
        ask again with the two or three it nearly heard.

        THE MATCHING MOVED HERE (2026-09-11): "the orb should have already
        told u what works... replicate that for voice dock". A command
        spoken at the orb arrives as WORDS and is resolved by this hub; a
        spoken answer to a LIST was resolved on the phone by a separate,
        weaker matcher in the one part of the system with no test runner.
        Now the phone sends `heard` and nlu/choice_match decides -- one
        implementation, on the side that can be tested, and when it is not
        sure it comes back with a question instead of a dead end.
        """
        from nlu.choice_match import match as _match_spoken
        full = current = list(options)
        asked = prompt
        confirming = False
        offered = 0          # how many candidates the narrowed round showed
        # Answers that led nowhere. "None of those" is not one of them: it
        # is the person asking for the list, and it used to spend one of
        # the three rounds, so "bulb" / none of those / "bulb" ended the
        # question with nothing chosen. Resets are bounded by the loop.
        misses = 0
        for _round in range(self.MAX_CHOICE_ROUNDS * 3):
            if misses >= self.MAX_CHOICE_ROUNDS:
                break
            reply = await self._ask_choice(asked, current, multi=multi,
                                           allow_all=allow_all,
                                           cancel_label=cancel_label)
            if reply is None:
                return []
            values, heard, selected = reply
            if values:
                return values
            if not heard:
                return []
            said = _plain_answer(heard)
            if polar:
                if _YES.match(said):
                    event("CHOICE", f"{heard!r} -> yes")
                    return [str(current[0].get("value", ""))]
                if heard == "__none_of_those__" or _NO.match(said):
                    event("CHOICE", f"{heard!r} -> no")
                    return [self._CHOICE_NONE]
            # A NARROWED QUESTION IS A YES/NO QUESTION when it offered
            # exactly one candidate, and people answer it that way --
            # "yes" is the shortest true answer to "did you mean
            # bulb?". The matcher scores it against `bulb` and quite
            # rightly finds nothing, so it is read here instead.
            if confirming and offered == 1 and _YES.match(said):
                picked = [str(current[0].get("value", ""))]
                event("CHOICE", f"{heard!r} -> yes, {picked[0]}")
                return picked
            # The whole list again: the row was tapped, or a narrowed
            # round was answered no.
            reset = heard == "__none_of_those__" or bool(confirming and _NO.match(said))
            if not reset:
                # WORDS, NOT A SELECTION: the phone heard something and is
                # asking this hub what it means.
                picked, near = _match_spoken(heard, current, multi=multi,
                                             allow_all=allow_all,
                                             confirming=confirming)
                # THE "NONE OF THOSE" ROW IS NOT AN ANSWER. In a narrowed round
                # it is one of `current`, so words like "none of those please"
                # (which _NO does not match whole) can be matched TO it -- and
                # the sentinel then went back to the caller as if it were a
                # device id. Alone, it means: the whole list again. Beside
                # real values ("all of them" picks every row), it is dropped.
                if self._CHOICE_NONE in picked:
                    picked = [v for v in picked if v != self._CHOICE_NONE]
                    reset = not picked
            if reset:
                event("CHOICE", f"{heard!r} -> none of those")
                asked, current, confirming = prompt, full, False
                continue
            near = [o for o in near if o.get("value") != self._CHOICE_NONE]
            if picked:
                # SPOKEN ON TOP OF TAPS. On a multi-select the phone sends
                # the rows already ticked beside the words ("and the
                # porch"), and the answer is both. Only with THESE words:
                # a narrowed round shows other rows, and ticks the person
                # can no longer see are not carried into it.
                if multi and selected:
                    chosen = set(picked) | set(selected)
                    picked = [str(o["value"]) for o in current
                              if str(o["value"]) in chosen]
                event("CHOICE", f"matched {heard!r} -> {len(picked)} value(s)")
                return picked
            misses += 1
            if near and not confirming:
                names = ", ".join(str(o.get("label") or o.get("value"))
                                  for o in near)
                event("CHOICE", f"{heard!r} matched nothing outright; "
                                f"offering {names}")
                asked = f'I heard "{heard}". Did you mean {names}?'
                # The way back to the whole list, said in words a person
                # would use, and tappable like any other row.
                current = list(near) + [
                    {"value": self._CHOICE_NONE, "label": "None of those",
                     "sub": "show the whole list again"}]
                offered = len(near)
                confirming = True
                continue
            # Nothing close, or the narrowed round failed too: back to the
            # full list, said plainly.
            event("CHOICE", f"{heard!r} matched nothing")
            asked = (f'I did not catch that. {prompt}'
                     if not confirming else prompt)
            current = full
            confirming = False
        return []


    async def _ask_choice(self, prompt, options, *, multi, allow_all,
                          cancel_label):
        """One round: send the list, wait for the answer, and hand back
        (values chosen, words heard, rows ticked beside the words). None
        means cancelled.

        AN ANSWER TO AN OLDER LIST IS ASKED AGAIN, not taken as "nothing".
        A tap that left the phone before this question reached it names a
        row of the list it replaced; every value is then dropped below, and
        that empty answer used to end the whole flow as if nothing had been
        chosen. The same question goes out again instead, a bounded number
        of times.
        """
        for _attempt in range(self.MAX_CHOICE_ROUNDS):
            answer = await self._ask_choice_once(
                prompt, options, multi=multi, allow_all=allow_all,
                cancel_label=cancel_label)
            if answer != "stale":
                return answer
        return [], "", []

    async def _ask_choice_once(self, prompt, options, *, multi, allow_all,
                               cancel_label):
        # WHAT WAS ASKED, AND HOW IT WAS ANSWERED (2026-09-11). A tap on a
        # card and a spoken name send the identical frame, so a log of one
        # line of values could not tell "I said all of them" from "I tapped
        # both" -- and that is exactly the question when somebody reports
        # that speaking to a list does not work. The app now marks its
        # answer (`via`, `heard`); both keys are diagnostics, the values are
        # read exactly as before, and an older app that sends neither is
        # logged as "via unstated".
        event("CHOICE", f"asked {prompt[:60]!r} with {len(options)} option(s)"
                        + (" [multi]" if multi else "")
                        + (" [all-allowed]" if allow_all else ""))
        reply = await self._exchange({
            "type": "choice",
            "text": prompt,
            "multi": multi,
            "allow_all": allow_all,
            "cancel": True,
            # Omitted when empty rather than sent blank: an older app reads a
            # missing key as "use your own wording", which is exactly right,
            # while a present-but-empty one would blank the button.
            **({"cancel_label": cancel_label} if cancel_label else {}),
            "options": options,
        }, "choice")

        def _said(outcome: str) -> None:
            via = str(reply.get("via") or "").strip().lower() or "unstated"
            heard = str(reply.get("heard") or "").strip()
            event("CHOICE", f"answered via {via}"
                            + (f", heard {heard[:60]!r}" if heard else "")
                            + f" -> {outcome}")

        if reply.get("cancelled"):
            _said("cancelled")
            return None

        heard = str(reply.get("heard") or "").strip()
        values = reply.get("values")
        valid = {str(o["value"]) for o in options}
        # The rows ticked when the words were spoken (multi-select only),
        # checked against this list like any other value.
        ticked = reply.get("selected")
        selected = ([str(v) for v in ticked
                     if str(v) in valid and str(v) != self._CHOICE_NONE]
                    if multi and isinstance(ticked, list) else [])
        if isinstance(values, list):
            # Only echo back values we actually offered -- a client must not
            # be able to name a device that wasn't on the list it was shown.
            # This return value goes on to reach set_state, and on the
            # securement path the voice checks have already run by then.
            kept = [str(v) for v in values if str(v) in valid]
            dropped = len(values) - len(kept)
            # "SHOW ME THE WHOLE LIST AGAIN" is not an answer, it is a
            # request to be asked again -- handed back as words so the
            # loop reopens with everything. Beside real picks it is only
            # a stray tick, and the picks stand (the same rule the spoken
            # path follows in choose).
            if self._CHOICE_NONE in kept:
                kept = [v for v in kept if v != self._CHOICE_NONE]
                if not kept:
                    _said("none of those -- the whole list again")
                    return [], "__none_of_those__", []
            if kept:
                # ONE ANSWER TO A ONE-ANSWER QUESTION. The typed fallback
                # below already trims to one; this path handed a
                # single-pick caller every value the client sent, so
                # _pick_one and friends silently took whichever came first.
                if not multi:
                    kept = kept[:1]
                _said(f"{len(kept)} value(s)"
                      + (f", {dropped} not on the list and dropped"
                         if dropped else ""))
                return kept, heard, []
            if dropped and not heard:
                _said(f"{dropped} value(s) from an older list -- asking again")
                return "stale"
            # NOTHING SURVIVED, BUT SOMETHING WAS SAID. The phone stopped
            # matching on 2026-09-11 and now sends only what it heard; the
            # hub matches it (see choose).
            _said("no values"
                  + (f", {dropped} not on the list and dropped" if dropped else "")
                  + (" -- matching what was heard" if heard else ""))
            return [], heard, selected

        # Fallback for a client that answers with typed text rather than a
        # selection ("1,3", "a"). Interpreted as 1-based positions into the
        # list we just sent, which is what the old numeric prompt meant -- so
        # an older client keeps working against a newer server.
        raw = str(reply.get("value", "")).strip().lower()
        if not raw:
            _said("nothing")
            return [], heard, selected
        if allow_all and raw in ("a", "all"):
            _said(f"all {len(options)}")
            return [o["value"] for o in options
                    if o["value"] != self._CHOICE_NONE], heard, []
        picked = []
        for tok in raw.replace(" ", "").split(","):
            if tok.isdigit() and 1 <= int(tok) <= len(options):
                v = options[int(tok) - 1]["value"]
                if v not in picked:
                    picked.append(v)
        out = picked if multi else picked[:1]
        _said(f"typed {raw[:30]!r} -> {len(out)} value(s)")
        # Typed text that named nothing is still words worth matching.
        return (out, heard, []) if out else ([], heard or raw, selected)
    async def capture_audio(self, prompt: str, *, phrase: str = "",
                            index: Optional[int] = None,
                            total: Optional[int] = None,
                            context: str = "recording"):
        # _exchange drains first: a stale recording must not satisfy this capture
        reply = await self._exchange({
            "type": "record_required",
            "text": prompt,
            "phrase": phrase,
            "index": index,
            "total": total,
            "context": context,
        }, "record_required")
        if reply.get("cancelled"):
            raise CancelledByUser()
        b64 = reply.get("audio")
        if not b64 or not isinstance(b64, str):
            return None
        # THE SAME CAP the command and challenge paths enforce. Enrollment
        # recordings arrive here and had none, so a client could hand the
        # decoder an arbitrarily large payload.
        if len(b64) > config_MAX_AUDIO_B64_LEN:
            await self._send_json({
                "type": "output", "level": "warn",
                "text": "That recording is too large -- record a shorter clip.",
            })
            return None
        try:
            raw = base64.b64decode(b64, validate=True)
        except Exception:
            return None
        # Decode off the event loop -- ffmpeg may be involved, and blocking
        # here would stall every other connection. Same reason ws_server
        # wraps its own decode in to_thread.
        # A str, as the command and challenge paths insist: decode_bytes
        # calls .lower() on it, so `"audio_format": 5` crashed the flow.
        fmt = reply.get("audio_format")
        return await asyncio.to_thread(
            audio_io.decode_bytes, raw, fmt if isinstance(fmt, str) else "wav"
        )
 
    async def set_view(self, name: str) -> None:
        await self._send_json({"type": "view", "name": name})

    async def form(self, title: str, fields: List[Dict[str, Any]],
                   note: str = "") -> Optional[Dict[str, Any]]:
        reply = await self._exchange({
            "type": "form",
            "text": title,
            "note": note,
            "fields": fields,
        }, "form")
        if reply.get("cancelled"):
            return None
        values = reply.get("values")
        return values if isinstance(values, dict) else None

    async def registration(self, spec: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        # PATIENT: this is a screen somebody fills in, not a question.
        reply = await self._exchange({"type": "registration", **spec},
                                     "registration", patient=True)
        action = str(reply.get("action") or "save")
        if reply.get("cancelled") or action == "cancel":
            return None
        values = reply.get("values")
        return {"action": "generate" if action == "generate" else "save",
                "values": values if isinstance(values, dict) else {}}

    async def pin_map(self, payload: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        # PATIENT. Laying out a board is not answering a question -- it is
        # sitting with a diagram, thinking about which pin the relay is
        # actually soldered to, possibly walking over to look. The ordinary
        # prompt timeout would take the screen away mid-thought, which is
        # precisely the bug that made entering a Wi-Fi password impossible.
        reply = await self._exchange({"type": "pin_map", **payload},
                                     "pin_map", patient=True)
        if reply.get("cancelled"):
            # THE DRAFT COMES BACK WITH THE CANCEL. The phone sends what was
            # laid out when the screen is closed, so an unfinished design
            # is kept in Saved layouts; this line used to drop it, and
            # closing the pin screen lost the work (her report, 2026-10-05:
            # "i cant save a draft of the layout"). Bounded like any list
            # from a phone; its items are cleaned where they are kept.
            draft = reply.get("draft")
            return {"items": [], "cancelled": True,
                    "draft": draft[:200] if isinstance(draft, list) else []}
        items = reply.get("items")
        return {"items": items if isinstance(items, list) else [],
                "cancelled": False}

    async def send_data(self, kind: str, payload: Dict[str, Any]) -> None:
        await self._send_json({"type": kind, **payload})

    async def rule_builder(self, payload: Dict[str, Any]
                           ) -> Optional[Dict[str, Any]]:
        # PATIENT, for the pin screen's reason: building a rule is sitting
        # and thinking about how the house should behave, not answering a
        # question. The ordinary timeout would take the blocks away
        # mid-thought.
        reply = await self._exchange({"type": "automation_builder", **payload},
                                     "automation_builder", patient=True)
        if reply.get("cancelled"):
            return {"rule": None, "cancelled": True}
        trial = reply.get("try")
        if isinstance(trial, dict):
            return {"rule": None, "try": trial, "cancelled": False}
        heard = reply.get("sentence")
        if isinstance(heard, str):
            return {"rule": None, "sentence": heard, "cancelled": False}
        rule = reply.get("rule")
        return {"rule": rule if isinstance(rule, dict) else None,
                "cancelled": False}

    async def scene_builder(self, payload: Dict[str, Any]
                            ) -> Optional[Dict[str, Any]]:
        # Patient, as the automation builder: the person takes their time.
        reply = await self._exchange({"type": "scene_builder", **payload},
                                     "scene_builder", patient=True)
        if reply.get("cancelled"):
            return {"cancelled": True}
        return reply


#: Answers to a narrowed "did you mean X?" that the option matcher cannot
#: see, because they name nothing on the list. Whole utterance only, the
#: same rule every spoken control in this system follows: bare "no" ends
#: the suggestion, "the no entry sign" is still a device.
_YES = re.compile(r"^(yes|yeah|yep|yup|correct|right|that one|thats right|"
                  r"that is right|ok|okay|sure|do|do it|please do)$")
_NO = re.compile(r"^(no|nope|none|neither|none of (them|these|those)|"
                 r"not those|not that|wrong|something else|"
                 r"read them again)$")

#: Words that carry no answer around a yes or a no: "yes please", "um no",
#: "no thanks" -- which, matched whole against _YES / _NO, fell through to
#: the option matcher and came back "I did not catch that".
_ANSWER_PADDING = {"please", "thanks", "thank", "you", "um", "uh", "er",
                   "erm", "ah", "oh", "well", "so", "just"}


def _plain_answer(heard: str) -> str:
    """A spoken answer as _YES / _NO read it: lower case, the apostrophe
    closed up ("that's right" -> "thats right"), other punctuation gone, no
    padding. "Yes, please." is "yes". The phone's _plainAnswer is its twin.
    """
    text = re.sub(r"['\u2019]", "", str(heard or "").lower())
    words = re.sub(r"[^a-z0-9\s]", " ", text).split()
    return " ".join(w for w in words if w not in _ANSWER_PADDING)


#: PIN verdicts another try cannot change.
_FINAL_PIN_STATUSES = ("locked_out", "no_pin_set", "bcrypt_unavailable",
                       "system_error")


#: The slots that say WHAT to do (resolver._has_explicit_action's), carried
#: from a command to an addition said over it.
_ACTION_SLOTS = ("switch", "absolute_value", "delta", "delta_direction", "unit")


# ── ONE CONVERSATION PER PHONE (2026-10-11) ─────────────────────────────
#
# The shell is ONE object for the whole hub: models, devices, scenes,
# boards, people. That is right for what it KNOWS. It was wrong for what it
# was DOING: who is speaking, where the questions go, what the last command
# did, why it failed -- all of that sat on the one shell too, so the server
# had to lock the whole shell for each command, questions and voice checks
# included, and a second phone waited (up to 20 s, then "busy") for the
# first one's conversation to end.
#
# Those fields now live in a Conversation: one per phone connection, chosen
# by a context variable the connection sets once. Every task it starts, and
# every worker thread those start through asyncio.to_thread, sees its own.
# The shell keeps the same attribute names (properties below), so the
# thousands of lines that say self._io or self.failure_note are unchanged
# and simply mean "this phone's". Code with no connection (startup, the
# terminal, the automation engine's thread) uses the shell's own home
# conversation, exactly as before.

#: name -> default (a callable is a factory, for fields that are mutable).
CONVERSATION_FIELDS: Dict[str, Any] = {
    "_io": None,                    # set to NullIO()/TerminalIO by owners
    "current_speaker_id": None,
    "current_phone_id": "",
    "phone_now": None,
    "_last_no_match": False,
    "last_multi_outcome": dict,
    "checkpoint_note": None,
    "failure_note": None,
    "answers_said": (),
    "nav_offers_fn": None,
    "nav_pick": None,
    "command_action": dict,
    "carry_slots": dict,
    "free_ride": False,
    "_split_now": False,
    "_esp_handoff": False,
    "_manual_add": False,
    "_ceremony_begun": False,
    "_ws_last_speaker": None,
}


class Conversation:
    """What ONE phone is in the middle of. See CONVERSATION_FIELDS."""

    def __init__(self, io: Any = None):
        for name, default in CONVERSATION_FIELDS.items():
            setattr(self, name, default() if callable(default) else default)
        if io is not None:
            self._io = io


#: The conversation of the task that is running (None: the shell's own).
CONVERSATION: "contextvars.ContextVar[Optional[Conversation]]" = \
    contextvars.ContextVar("homevoice_conversation", default=None)


def _conversation_property(name: str) -> property:
    def get(shell):
        conv = CONVERSATION.get()
        if conv is None:
            conv = shell._home_conversation()
        return getattr(conv, name)

    def put(shell, value):
        conv = CONVERSATION.get()
        if conv is None:
            conv = shell._home_conversation()
        setattr(conv, name, value)
    return property(get, put, doc=f"This phone's {name} (see Conversation).")


class InteractiveShell:
    def _try_connect_ha(self):
        """Optional, not required. If Home Assistant isn't configured (no
        HOMEVOICE_HA_TOKEN set) or isn't reachable right now, this returns
        None and the whole system keeps working exactly as it did before HA
        existed -- devices without an ha_entity_id are entirely unaffected
        either way (see device_manager.py)."""
        try:
            from home_assistant.ha_client import HAClient
            client = HAClient()
        except RuntimeError as e:
            print(f"[HA] Not configured -- running without Home Assistant. ({e})")
            return None
        except Exception as e:
            print(f"[HA] Could not initialize client -- running without Home Assistant. ({e})")
            return None

        if not client.is_reachable():
            # TWO DIFFERENT FAULTS, AND ONLY ONE OF THEM HEALS ITSELF
            # (2026-09-14). Both used to get the sentence below, so a
            # REVOKED TOKEN was told "nothing needs restarting when HA
            # comes back" -- about a fault that will never come back on its
            # own -- and was then re-probed every sixty seconds for the
            # rest of the run. is_reachable now says which one it was.
            if client.token_rejected:
                print("[HA] Home Assistant is UP and REFUSED the token. "
                      "This one will not fix itself: put a valid long-lived "
                      "token in HOMEVOICE_HA_TOKEN and restart the hub.")
            else:
                print("[HA] Configured, but not reachable right now -- "
                      "running without live HA control. The hub re-checks "
                      "on its own; nothing needs restarting when HA comes "
                      "back.")
            # RELEASE THE POOL. __init__ opens a requests.Session, and
            # dropping the reference here left it to the garbage collector
            # -- once a minute, for as long as HA stayed down. close() has
            # named this exact call site in its docstring since it was
            # written; the call itself was never there.
            try:
                client.close()
            except Exception:                      # noqa: BLE001
                pass
            # CONFIGURED, so it is worth asking again later. See
            # reconnect_ha_if_needed: without this flag a hub that has no HA
            # AT ALL would be probed forever for something it does not have.
            self._ha_configured = True
            return None

        print("[HA] Connected.")
        self._ha_configured = True
        return client

    #: How long to leave a configured-but-dead Home Assistant alone between
    #: attempts. Long enough that a stopped HA is not probed on every menu,
    #: short enough that starting HA and walking to the phone is sufficient.
    HA_RETRY_SECONDS = 60.0

    def reconnect_ha_if_needed(self) -> bool:
        """Ask again for a Home Assistant that was configured but down.
        True when this call is what brought it back.

        THE MESSAGE USED TO PROMISE THIS AND NOTHING DID IT (2026-09-11).
        `_try_connect_ha` runs ONCE, in __init__, and returns None when HA
        is not answering -- so `self.ha_client` stayed None for the life of
        the process while the log said "until it's back". Starting Home
        Assistant afterwards changed nothing: the integration door stayed
        hidden, the entity picker never ran, cameras stayed unreachable,
        and every HA-backed device stayed dark until somebody restarted the
        hub. Found from her log, where HA had been down at boot:

            [HA] Configured, but not reachable right now ...
            (and then, hours later, no "Add an integration" door)

        DeviceManager holds its OWN reference (it is handed one at
        construction), so both are set here or the two disagree about
        whether the house has Home Assistant.
        """
        if self.ha_client is not None:
            return False
        if not self._ha_configured:
            return False                      # no HA here at all; leave it
        now = time.monotonic()
        if now - self._ha_last_try < self.HA_RETRY_SECONDS:
            return False
        self._ha_last_try = now
        client = self._try_connect_ha()
        if client is None:
            return False
        self.ha_client = client
        self.dm.ha_client = client
        event("HA", "Home Assistant is answering again -- live control is back")
        return True

    def _home_conversation(self) -> Conversation:
        """The shell's own conversation: the terminal's, startup's, and
        anything running with no phone behind it."""
        conv = self.__dict__.get("_home_conv")
        if conv is None:
            conv = self.__dict__["_home_conv"] = Conversation()
        return conv

    def __init__(self):
        # Set by _try_connect_ha: HA is configured here, so a client that is
        # None right now is worth asking for again (reconnect_ha_if_needed).
        self._ha_configured = False
        self._ha_last_try = time.monotonic()
        self.ha_client = self._try_connect_ha()
        self.dm = DeviceManager(embed_fn=embed, ha_client=self.ha_client)
        # The width the loaded model actually emits, not a constant. A
        # SceneManager sized wrongly discards every stored scene at load.
        # Built in init(), NOT here: it needs the embedding width, and the
        # model is not loaded until init() runs. Constructing it here called
        # embedding_dim() before load_model() and killed startup outright --
        # the previous version passed no width at all and defaulted to a
        # hardcoded 768, which is the bug this was meant to fix.
        self.sm: Optional[SceneManager] = None
        self.extractor = SlotExtractor()
        self.executor = Executor(self.dm)
        self.intent_classifier: Optional[IntentClassifier] = None
        self.running = True
        # Where prompts actually go -- terminal by default, swappable to a
        # WebSocketIO per-connection when the network server drives this
        # same shell instead of a local terminal. Never set to None: callers
        # await self._io.write() from many places, and a None here turns every
        # one of them into an AttributeError at the worst possible moment.
        self._io = TerminalIO()
        # Who commands are attributed to -- resolved after enrollment/login,
        # NOT a hardcoded placeholder. Nobody is ever enrolled under a literal
        # "interactive_user" string, so every voice/PIN check would fail
        # forever if this stayed unset.
        self.current_speaker_id: Optional[str] = None
        #: WHICH HANDSET is talking, set by ws_server at authentication and
        #: empty on the terminal. The twin of current_speaker_id and NOT the
        #: same fact: that one names a PERSON (voice, PIN), this one names a
        #: DEVICE (an Ed25519 key). Only the Profile screen reads it, to say
        #: "this phone" about the phone actually asking.
        self.current_phone_id: str = ""
        #: The asking phone's own clock (aware datetime) for THIS command, set
        #: by ws_server from the command frame; None when it sent none.
        self.phone_now: Optional[datetime] = None
        #: Set by _carry_out when a chunk resolved to no device at all. Read
        #: and cleared by _run_multi, which must not let one fragment of
        #: mis-transcription veto the chunks that worked.
        self._last_no_match: bool = False
        #: Per-chunk outcome of the last split command, for the caller's
        #: result frame. A single bool cannot say WHICH half of "turn on the
        #: ac and lock the door" failed, and that is the one thing the person
        #: needs to know.
        self.last_multi_outcome: Dict[str, List[str]] = {}
        #: A flow that PARKED its work rather than finishing or failing --
        #: the ribbon's checkpoint (2026-08-25). Read and cleared by the
        #: transport after each action; the app shows it as a quiet
        #: banner, never as success and never as the amber failure.
        self.checkpoint_note: Optional[str] = None
        #: A flow that REFUSED and knows exactly why -- the first-class
        #: refusal channel (2026-08-26, "learn ir codes should have a
        #: warning saying that the ir receiver isnt registered... not
        #: 'didnt complete'"). One short sentence naming the cause; the
        #: transport carries it as the failed result's text, so the amber
        #: popup states the reason instead of generic filler. Read and
        #: cleared by the transport after each action.
        self.failure_note: Optional[str] = None
        #: The sudo model (2026-08-26): when a full gate check (voice or
        #: PIN) passes for a menu action, the SAME person's next gated
        #: actions inside MENU_GATE_GRACE_SECONDS pass without re-asking.
        #: Keyed by (phone id or "cli", speaker_id) -> monotonic expiry -- see
        #: _grace_key: one phone's window must not vouch for another
        #: connection claiming the same name. This is why adding a device
        #: and then renaming it no longer asks for the PIN twice in one
        #: minute ("theres pin for everything now", field test 2026-08-26).
        self._gate_grace: Dict[Tuple[str, str], float] = {}
        #: The automation engine (automations.py), born in init() once
        #: scenes exist. None when the module is absent -- every caller
        #: checks, and the automation menu doors refuse in words.
        self.automations: Optional[Any] = None
        #: Set by _offer_pending_setup when it hands the person over to the
        #: ESPHome installer. That path ENDS the add-device flow on purpose --
        #: flashing takes minutes and must not hold the shell lock -- but it
        #: is a successful hand-off, not a cancellation, so menu_add_device
        #: needs to tell the two apart before it prints "nothing saved".
        self._esp_handoff: bool = False
        #: Config flows and ESP firmware, lifted out of this file. Both
        #: hold `self` rather than copies of _io/ha_client, which are
        #: swapped per connection and on reconnect.
        self.flows = ConfigFlowRunner(self)
        self.ir = IRFlowRunner(self)
        self.esp = EspBuilder(self)
        #: An ESP board's devices on THIS hub. Never touches Home
        #: Assistant: the two are kept apart on purpose.
        self.boards_reg = BoardRegistrar(self)
        # THE DIRECT ROAD (local-first, 2026-08-26): a board device with no
        # HA entity attached -- or one HA cannot reach right now -- is
        # driven straight over its own MQTT. Wired here because this is
        # where dm and esp finally both exist.
        self.dm.mqtt_driver = self._drive_board_device
        self.dm.drive_problem = self._drive_problem
        # WHY THE LAST DRIVE FAILED, per device -- see _drive_board_device.
        # Written on a worker thread, read a moment later by _render.
        self._drive_problems: Dict[str, Tuple[float, str]] = {}
        # AND THE ROAD BACK. What the board says about itself, read from the
        # subscription the automation engine already holds open, so a status
        # query answers from the board rather than from Home Assistant.
        self.dm.state_reader = self._read_board_state

        # Voice security. Anti-spoof deliberately absent (see voice_auth.py's
        # module docstring for why). Constructed here, always -- if a
        # dependency (bcrypt, the Fernet key, the SV model) is genuinely
        # missing, each component fails loudly on its own rather than this
        # class silently skipping security.
        self.biometric_store = BiometricStore()
        self.sv = SherpaSV()
        self.fallback = FallbackAuthManager()
        self.enrollment = EnrollmentHandler(self.sv, self.biometric_store)
        # WHO LET WHOM IN. Names and times only -- see people.py. Nothing
        # gates on it; it exists so the Profile screen can answer a question
        # the hub could not answer at all before, and so an enrolment nobody
        # expected leaves a trace.
        self.people = PeopleJournal()
        # WHOSE CARD IS WHOSE. Gates nothing -- a UID is a name, not a
        # proof -- so this exists to make a tap LEGIBLE: "Augusta's card
        # tapped" rather than "card BA839D07 tapped", and a rule that
        # answers to a person rather than to hex. See pin_auth.CardStore,
        # which is where it lives because a rename must move it too.
        self.cards = CardStore()
        # Load any persisted voiceprints into SherpaSV's live cosine cache --
        # BiometricStore is the durable copy, SherpaSV's cache is rebuilt from
        # it every startup (mirrors how devices.json/scenes.json are the
        # durable copy and other in-memory structures get rebuilt from them).
        for user_id in self.biometric_store.list_users():
            emb = self.biometric_store.get_voice(user_id)
            if emb is not None:
                self.sv.sync_cache(user_id, emb)

    # ── Lifecycle ───────────────────────────────────────────

    async def init(self):
        print("=" * 60)
        print("SEMANTIC HOME COMMAND SYSTEM")
        print("=" * 60)

        init_directories()  # ensure models/ and data/ exist before anything loads
        run_setup()  # check/fetch required assets; hard-stops if one's missing and declined

        print(f"Loading model from: {MODEL_DIR}")
        try:
            load_model(MODEL_DIR)
            print(f"Backend: {get_backend()}")
        except (FileNotFoundError, ImportError) as e:
            print(f"ERROR: {e}")
            sys.exit(1)

        # NOW the width is knowable. Scenes are validated against it at load,
        # so a SceneManager built before the model would either guess or fail.
        # is_secure: so resolve() can say whether a matched scene touches a
        # lock, and _run_command can demand the active challenge first.
        self.sm = SceneManager(
            embed_fn=embed, emb_dim=embedding_dim(),
            is_secure=lambda did: bool(self.dm.devices.get(did, {}).get("secure")))
        await self._io.write(f"Scenes: {len(self.sm.list_scenes())} loaded "
                             f"(embedding width {embedding_dim()})")

        # THE HUB'S OWN REFLEXES (2026-08-26). Scenes exist now, so the
        # automation engine can be born: trigger + condition + scene as
        # action, evaluated on the hub's MQTT -- not Home Assistant's
        # engine, because HA only ever sees a subset of this house.
        # Guarded: a missing automations.py degrades to "the feature is
        # absent", never to a hub that will not start.
        try:
            # nlu\ is its home, beside scene_manager and device_manager --
            # the owner's placement.
            #
            # NO BARE `from automations import ...` FALLBACK, here or at
            # any of the five other sites that had one (2026-09-01). It
            # was there for "a flat checkout", which is a layout main.py
            # never runs in -- so the branch was unreachable, Pylance
            # flagged an unresolvable import on every open, and worse: an
            # ImportError raised INSIDE nlu/automations.py is still an
            # ImportError, so the retry ran, failed on the bare name, and
            # the console reported "No module named 'automations'" --
            # naming a path that was never the problem and throwing away
            # the one that was. One import, one truth, one error message.
            from nlu.automations import AutomationEngine
            self.automations = AutomationEngine(self.dm, self.sm, self.esp)
            # WHOSE CARD, for the journal. Handed over as a function rather
            # than the store itself: the engine may read a name and must
            # never be able to write one.
            self.automations.card_owner = self.cards.owner
            if self.automations.start():
                await self._io.write(
                    f"Automations: {len(self.automations.rules)} rule(s), "
                    f"engine listening")
            elif self.automations.rules:
                await self._io.write(
                    f"Automations: {len(self.automations.rules)} rule(s) "
                    f"saved -- the engine starts when a board and broker "
                    f"exist", level="warn")
        except Exception as _auto_exc:             # noqa: BLE001
            print(f"[auto] Automations unavailable ({_auto_exc})")
            self.automations = None

        # Records registered before the upgrade carry no readable_only flag,
        # so voice refused dimmers and covers; this stamps it from the board
        # plans. Records only -- the boards themselves are untouched.
        try:
            fixed = self.dm.backfill_readable_only(
                (slug, (self.esp._read_board(slug) or {}).get("items") or [])
                for slug in self.esp.list_nodes())
            if fixed:
                await self._io.write(f"Devices: {fixed} board device record(s) "
                                     f"upgraded (actuator/sensor flag)")
        except Exception as exc:   # noqa: BLE001
            print(f"[devices] backfill skipped: {exc}")

        # Each card reader checks itself once the broker is up (the log).
        try:
            asyncio.get_running_loop().create_task(self._reader_selfcheck())
        except RuntimeError:
            pass

        # THERE IS NO HOME ASSISTANT EVENT EAR (owner's decision, 2026-09-02):
        # Home Assistant is only for adding and controlling integration
        # devices. The engine hears boards on the hub's own MQTT and
        # nothing else; statistics for HA sensors are recorded locally
        # by the readings screen (eng.record / eng.day_stats).

        # Path normalization matters here: np.savez appends ".npz", so a raw
        # Path(INTENT_INDEX_PATH).exists() check looked at a filename that is
        # never written. It reported "missing" on every startup, rebuilt the
        # index (re-embedding every prototype), and then IntentClassifier
        # raised FileNotFoundError on the file that had just been created.
        # resolve_index_path()/intent_index_exists() are the single authority.
        index_path = resolve_index_path(INTENT_INDEX_PATH)
        if not intent_index_exists(INTENT_INDEX_PATH):
            print("\n[intent] Index not found. Building...")
            try:
                index_path = build_intent_index(INTENT_INDEX_PATH)
                print(f"[intent] Index built: {index_path}")
            except Exception as e:
                print(f"[intent] Build FAILED: {e}")
        else:
            print(f"\n[intent] Index loaded: {index_path}")

        try:
            self.intent_classifier = IntentClassifier(INTENT_INDEX_PATH)
            print("[intent] Classifier ready")
        except Exception as e:
            # Not fatal, but not a footnote either. With no classifier the
            # dismiss and announce intents are silently unreachable for the
            # entire run -- and a one-line "WARNING" is exactly how that went
            # unnoticed. Say what stopped working, not just that something did.
            print(f"\n[intent] CLASSIFIER UNAVAILABLE: {e}")
            print("[intent] -> 'dismiss' and 'announce' intents are DISABLED "
                  "for this session. Device control is unaffected.")
            self.intent_classifier = None

        # Screen-navigation phrases are embedded once, in the background, so
        # the first spoken sentence does not wait a minute for them.
        def _warm_nav() -> None:
            try:
                from nlu.app_nav import warm_semantic
                t0 = time.perf_counter()
                ok = warm_semantic()
                print(f"[nav] semantic phrases {'ready' if ok else 'not available'}"
                      f" ({time.perf_counter() - t0:.1f}s)")
            except Exception as exc:                       # noqa: BLE001
                print(f"[nav] warm-up skipped: {exc}")
            # "What is it for" examples (lexicon.purpose_by_meaning), and the
            # grammar model (spaCy) that every command now reads: loaded here
            # so the first spoken sentence does not wait for them.
            try:
                from nlu.lexicon import warm_purpose
                warm_purpose()
            except Exception:                              # noqa: BLE001
                pass
            try:
                from nlu import grammar
                if grammar.ENABLED and not grammar.load():
                    print(f"[grammar] not available: {grammar.off_reason()}")
            except Exception:                              # noqa: BLE001
                pass
        threading.Thread(target=_warm_nav, daemon=True, name="nav-warm").start()

        print(f"\nDevices: {len(self.dm.devices)} | Scenes: {len(self.sm.list_scenes())}")
        print(f"  (devices file: {os.path.abspath(self.dm.path)})")
        print("=" * 60)
        print("READY")
        print("=" * 60 + "\n")

    # ── Input helpers ───────────────────────────────────────

    # ── Voice security ──────────────────────────────────────

    # FASTER-WHISPER IS GONE, and with it `_get_test_asr` and
    # `_transcribe_for_test`.
    #
    # They existed for one purpose: in the CLI, to simulate the step the
    # PHONE performs, so that a test .wav produced a transcript and its
    # verification audio together the way production always has them coupled.
    # It was never wired into resolution and never ran on the Pi -- the hub
    # does not transcribe commands at all. Transcription happens on the
    # handset; this machine turns no speech into text at all (there are
    # no spoken challenge words in Defense).
    #
    # So the whole thing was a second, entirely different ASR engine, plus a
    # model folder, carried for the convenience of not typing a sentence
    # during a local test. On a Raspberry Pi that is real disk and a real
    # install for a path the Pi never takes. The CLI now asks for the
    # transcript, which is honest about where the words come from: in
    # production a phone supplies them, and in the CLI there is no phone.

    def _load_wav(self, path: str) -> Optional[np.ndarray]:
        """Load an audio file to mono 16kHz float32 for CLI/enrollment testing.
        Routes through the shared audio_io chokepoint (ffmpeg-first, wave/
        soundfile fallback) -- the SAME decoder the network path uses -- so
        enrollment audio and phone-verification audio are decoded and
        resampled identically. That symmetry is the whole point; do not
        re-add a separate decode path here.

        (Two such paths, _load_wav_stdlib and _load_audio_soundfile, were
        still sitting below this method after the rewrite, unreferenced by
        anything -- exactly the "separate decode path" this docstring warns
        against, waiting to be wired back up. They have been deleted, along
        with the `from dependency_flags import sf, _SOUNDFILE` line that
        existed solely to serve them.)"""
        audio = audio_io.decode_file(path)
        if audio is None:
            print(f"  [audio] could not read/decode: {path}")
        return audio

    async def _prompt_command_audio_and_text(self) -> tuple:
        """Audio-FIRST entry point, matching production: the phone captures
        ONE utterance and derives both the transcript and the verification
        audio from it together -- they are never two independently-supplied
        things. Returns (text, audio). Blank audio path falls back to plain
        typed text with no audio (the honest CLI-typing gap in _voice_gate);
        a real file is loaded for speaker verification and you type what it
        says.

        THE ORDER IS WHAT MATTERS, and it is unchanged: audio first, then the
        words that go with it. Typing the transcript is not a downgrade from
        the faster-whisper simulation that used to sit here -- it is the same
        coupling, done by the only party present. What is NOT allowed is
        supplying text and audio as two unrelated things, which is the shape
        this method exists to make impossible."""
        try:
            path = await self._io.ainput("  Path to command audio (.wav), or Enter to type a command with no audio: ")
        except (EOFError, KeyboardInterrupt):
            print()
            return "", None

        if not path.strip():
            text = await self._io.get_input("  Enter command: ")
            return text, None

        audio = self._load_wav(path)
        if audio is None:
            text = await self._io.get_input("  (audio failed to load) Enter command: ")
            return text, None

        text = await self._io.get_input(
            "  Type what was said in that recording: ")
        return text, audio

    #: How long after a command the speaker model is kept warm, and how often.
    _SV_WARM_FOR_S = 300.0
    _SV_WARM_EVERY_S = 20.0
    #: A phone that is only CONNECTED (its ping, nothing said or tapped)
    #: keeps the models warm this long after it last did something, and no
    #: longer: a phone left on a stand must not keep the machine working all
    #: day. The first command after that pays the cold model, once (her
    #: ruling, 2026-10-05).
    _SV_WARM_CAP_S = 1800.0

    #: Set by the server to "is a command running right now?". A beat is
    #: skipped while one is: it must never be what a command waits behind.
    models_busy: Optional[Callable[[], bool]] = None

    def _keep_speaker_model_warm(self, active: bool = True) -> None:
        """For five minutes after a phone last did anything (a frame from
        it, a command), a throwaway pass through BOTH models every twenty
        seconds -- the speaker model and the sentence model -- so the next
        command does not pay a cold one (speaker: 2.45 s cold, 0.17 s warm;
        sentences: 5.6 s cold, 0.6 s warm on her hub). The first pass is at
        once, so a phone that has only just connected is warm for its first
        command too. Nothing runs while nobody is using the house.

        `active=False`: a phone's ping -- it is there, and has done nothing.
        That keeps the window open only for _SV_WARM_CAP_S after the last
        thing anybody actually did."""
        now = time.monotonic()
        if active:
            self._sv_last_active = now
        elif now - getattr(self, "_sv_last_active", -1e9) > self._SV_WARM_CAP_S:
            return
        self._sv_warm_until = now + self._SV_WARM_FOR_S
        if getattr(self, "_sv_warm_task", None) is not None:
            return

        async def beat():
            busy = self.models_busy
            if busy is not None and busy():
                return
            warm = getattr(getattr(self, "sv", None), "warm", None)
            if warm is not None:
                await asyncio.to_thread(warm)
            from nlu import model_module
            took = await asyncio.to_thread(model_module.warm)
            if took > 2.0:
                # Worth a line: this is the wait a person would have had.
                event("EMBED", f"the sentence model had gone cold -- warming "
                               f"it took {took:.1f}s")

        async def loop():
            try:
                while time.monotonic() < self._sv_warm_until:
                    await beat()
                    await asyncio.sleep(self._SV_WARM_EVERY_S)
            except Exception as exc:                   # noqa: BLE001
                event("VOICE", f"model warm-up stopped: {exc}")
            finally:
                self._sv_warm_task = None
        self._sv_warm_task = asyncio.ensure_future(loop())

    async def _passive_voice_check(self, speaker_id: str, audio: np.ndarray) -> bool:
        """Baseline check on every command -- speaker match against the SAME
        audio the command arrived in, no extra utterance. Genuine speaker
        verification only (no challenge phrase involved).

        sv.verify() is a synchronous model call taking hundreds of ms; run
        off the event loop or it stalls the reader task, the keepalives and
        every other connection for its duration."""
        event("VOICE", f"passive check: speaker={speaker_id!r} on {describe_audio(audio)}")
        t = time.perf_counter()
        d = await asyncio.to_thread(self.sv.verify_detail, speaker_id, audio)
        elapsed = time.perf_counter() - t
        self._keep_speaker_model_warm()
        score = d["score"]
        passed = score >= SV_SCORE_THRESHOLD
        event("VOICE", f"passive check: score={score:.4f} threshold={SV_SCORE_THRESHOLD} "
                       f"-> {'PASS' if passed else 'FAIL'}  ({elapsed:.2f}s)")
        return passed

    def _pin_lockout_seconds(self, user_id: str,
                             result: Optional[Dict[str, Any]] = None) -> int:
        """Best effort: how long until this user may try a PIN again.

        Asks the result dict first (the authenticator may already say), then
        the PIN engine FallbackAuthManager wraps; any failure there degrades
        this to "locked, no countdown" instead of an exception inside an
        error path. Returns 0 when not locked or when nobody can say."""
        try:
            wait = int((result or {}).get("lockout_remaining") or 0)
        except (TypeError, ValueError):
            wait = 0
        if wait > 0:
            return wait
        try:
            return max(0, int(self.fallback.pin.get_lockout_remaining(user_id)))
        except Exception:                          # noqa: BLE001
            return 0

    def pin_verdict(self, user_id: str,
                    result: Optional[Dict[str, Any]]) -> str:
        """One sentence for every wrong-PIN moment, everywhere.

        The raw status strings ("wrong_pin", "locked_out") used to be
        printed verbatim at sign-in and in the voice fallback -- machine
        vocabulary at the exact moment a person is being told no, which
        reads as "the app is broken", not "you mistyped". Every PIN check
        in the system now speaks through here, so "Incorrect PIN" means
        the same thing on the login screen and mid-command, and a lockout
        always says how long it lasts when that can be known."""
        status = (result or {}).get("status", "")
        if status == "locked_out":
            wait = self._pin_lockout_seconds(user_id, result)
            if wait >= 120:
                minutes = (wait + 59) // 60
                return (f"PIN entry is locked -- too many wrong tries. "
                        f"Try again in about {minutes} minutes.")
            if wait > 0:
                return (f"PIN entry is locked -- too many wrong tries. "
                        f"Try again in {wait} seconds.")
            return ("PIN entry is locked -- too many wrong tries. "
                    "Wait a while and try again.")
        if status == "invalid_format":
            if PIN_MIN_LENGTH == PIN_MAX_LENGTH:
                return (f"A PIN is exactly {PIN_MAX_LENGTH} digits -- "
                        f"that wasn't one.")
            return (f"A PIN is {PIN_MIN_LENGTH} to {PIN_MAX_LENGTH} digits -- "
                    f"that wasn't one.")
        if status == "no_pin_set":
            return "No PIN is set for this user."
        if status == "bcrypt_unavailable":
            return ("The hub cannot check PINs right now (its password "
                    "hashing library is missing) -- this needs fixing on "
                    "the hub, not another try.")
        if status == "system_error":
            return ("The hub could not record this attempt (a storage "
                    "error) -- try again later.")
        return "Incorrect PIN."

    async def _offer_pin_fallback(self, speaker_id: str) -> bool:
        """The PIN gate, and -- since 2026-09-12 -- a gate that SAYS WHY.

        THE BUG THIS CLOSES. "that profile settings didnt work...now its
        just warnings when i wanted to rename user or chsnge pin or add
        another peron". Every refusal in here wrote its real reason as an
        `output` line at INFO level and nothing else. `failure_note` -- the
        first-class channel ws_server reads to build the failure card -- was
        never set by this method or by _gate_for, in a file with 79 other
        assignments to it. So the phone's card fell through to the generic
        filler, "rename user did not complete", while the sentence that
        would have explained it ("no PIN is set for this user", "locked out
        for 14 more minutes") went out as a quiet info line the Profile
        screen does not draw.

        Three different faults -- no PIN on file, a cancelled PIN box, a
        lockout left over from an earlier bad run -- all presented as the
        same unexplained amber warning on all three gated rows, which is
        exactly why they read as one broken screen.

        EVERY `return False` below now leaves a note. The note names the
        person, because on a shared hub the answer is often that the gate
        is asking somebody ELSE for a PIN.
        """
        event("VOICE", f"falling back to PIN for {speaker_id!r}")
        who = speaker_id or "this connection"
        if not self.fallback.is_enrolled(speaker_id).get("pin"):
            event("VOICE", f"no PIN enrolled for {speaker_id!r} -> DENY")
            self.failure_note = (
                f"{who} has no PIN set, and this needs one. The voice check "
                f"could not settle it, and a PIN is the only other proof "
                f"this hub accepts. Set one from Profile > Set PIN.")
            await self._io.write("  [voice] Voice check failed and no PIN is set "
                                 "for this user -- denying.", level="warn")
            return False
        ok, why = await self._pin_tries(
            speaker_id, "  Voice check failed. Enter PIN: ", again="  PIN: ",
            tag="[voice] ", retry=" Try again, or say cancel.")
        if ok is None:
            event("VOICE", f"PIN fallback cancelled by {speaker_id!r}")
            self.failure_note = (
                "The PIN box was closed without a PIN, so this was not "
                "authorised. Nothing was changed.")
            await self._io.write("  [voice] PIN entry cancelled -- denying.",
                                 level="warn")
            return False
        if not ok:
            self.failure_note = f"{why} Nothing was changed."
        # Deliberately NOT touching the voiceprint on a pass -- see
        # voice_auth.py's module docstring: a PIN is weaker proof of
        # identity than a voice match, so PIN success must never feed
        # the adaptive template update.
        return ok

    async def confirm_with_pin(self, user_id: str, act: str,
                               ) -> Tuple[bool, str]:
        """"Prove it's you before this happens." Returns (ok, why-not).

        THE ONE PIN GATE FOR DELIBERATE ACTS, as opposed to _offer_pin_fallback,
        which is the gate for a voice check that did not settle. Two callers so
        far and they are the same shape: approving an automation's `ask`, and
        moving this hub to another tailnet. Both are moments where the system is
        about to do something consequential and wants to know a PERSON is here.

        `act` finishes the sentence "Your PIN to ___" and names the thing in the
        refusals, so the person is never asked for a PIN by a prompt that does
        not say what it buys.

        THREE TRIES, and the real limit lives in pin_auth: attempts are
        rate-limited and lockouts persist, so this loop cannot be used to guess
        harder -- it only spares a typo. Cancel is CANCEL, never fed to
        authenticate as a guess (that bug cost three lockout attempts for three
        cancels, field test 2026-08-26).

        It does NOT decide what happens on refusal -- the caller does. An
        approval declines the request, a tailnet switch simply does not happen;
        this returns the sentence and lets each caller add its own ending.
        CancelledByUser propagates for the same reason: the ribbon law says a
        cancel belongs to the flow that owns the room, not to the gate.
        """
        ok, why = await self._pin_tries(user_id, f"  Your PIN to {act} (or cancel): ")
        if ok is None:
            await self._io.write("  Cancelled -- nothing was changed.")
            return False, "Cancelled -- nothing was changed."
        return ok, why

    async def _pin_tries(self, user_id: str, prompt: str, *,
                         again: Optional[str] = None, tag: str = "",
                         retry: str = " Try again.") -> Tuple[Optional[bool], str]:
        """THE PIN LOOP both gates above share: (True, "") on a pass,
        (None, "") when the box was closed or answered cancel, and
        (False, why) on a refusal it has already written, prefixed `tag`.
        `again` is the prompt for a second or third ask."""
        # Already locked? Say so up front, with the countdown, instead of
        # collecting a PIN that cannot be checked and reporting a cryptic
        # failure after the fact.
        if self._pin_lockout_seconds(user_id):
            verdict = self.pin_verdict(user_id, {"status": "locked_out"})
            event("VOICE", f"PIN for {user_id!r}: pre-locked -> DENY")
            await self._io.write(f"  {tag}{verdict}", level="warn")
            return False, verdict
        # THREE ASKS HERE, but the REAL limit lives in pin_auth: attempts
        # are rate-limited and lockouts persist, so this loop cannot be used
        # to guess harder -- it only spares a fat-fingered person from
        # restarting the whole action over one typo. (Field test 2026-08-26:
        # a wrong PIN printed "PIN fallback failed: wrong_pin" -- one shot,
        # raw status, no way back in.)
        verdict = ""
        for attempt in range(3):
            try:
                pin = (await self._io.ainput(prompt, secret=True)).strip()
            except CancelledByUser:
                return None, ""
            except (EOFError, KeyboardInterrupt):
                print()
                return None, ""
            # Cancel is CANCEL, not a PIN attempt. Before this check, the word
            # "cancel" was fed to fallback.authenticate as if it were the PIN --
            # it failed (correct) but also burned one of the PIN_MAX_ATTEMPTS
            # lockout attempts (very wrong): three cancels locked the account
            # for PIN_LOCKOUT_SECONDS even though nobody guessed anything.
            # An empty reply is the same gesture made silently.
            if not pin or means_cancel(pin):
                return None, ""
            ok, result = await asyncio.to_thread(
                self.fallback.authenticate, user_id, pin)
            status = (result or {}).get("status", "")
            event("VOICE", f"PIN check for {user_id!r}: status={status or '?'} "
                           f"-> {'PASS' if ok else 'FAIL'}")
            if ok:
                return True, ""
            verdict = self.pin_verdict(user_id, result)
            if status == "wrong_pin" and self._pin_lockout_seconds(user_id):
                # THIS try tripped the engine's lockout: say both facts in
                # one breath rather than inviting a doomed retry.
                verdict += " " + self.pin_verdict(user_id, {"status": "locked_out"})
                break
            if status in _FINAL_PIN_STATUSES:
                break      # another try cannot change these; asking again would be theatre
            if attempt < 2:
                await self._io.write(f"  {tag}{verdict}{retry}", level="warn")
                prompt = again or prompt
        await self._io.write(f"  {tag}{verdict}", level="warn")
        return False, verdict

    async def _voice_gate(self, speaker_id: str, audio: Optional[np.ndarray],
                          force_active: bool = False) -> bool:
        """The single entry point for every voice-security decision in this
        app. Returns True to let the command proceed, False to block it."""
        if audio is None:
            # WHO is asking decides what "no audio" means.
            #
            # At a terminal it means someone standing at the machine typed a
            # command; there is nothing to verify and getting to the keyboard
            # was itself the check. Over a socket it means a JSON field was
            # left out -- and this branch used to return True unconditionally,
            # so ANY authenticated client could operate a lock by simply
            # omitting `audio`, skipping the voice check in one move. The
            # transport auth was doing all the work the voice stack was
            # supposed to do.
            if self._io.trusted_local:
                await self._io.write("  [voice] (no audio provided -- local terminal, "
                                     "speaker verification not applicable)")
                return True
            await self._io.write("  [voice] no audio attached -- this command cannot be "
                                 "verified by voice.")
            # PIN is the designed fallback whenever voice can't settle it (the
            # same fallback a voice that does not match gets), and it denies
            # outright when no PIN is set. It is NOT a free pass: force_active
            # callers reach here only after that explicit fallback succeeds.
            return await self._offer_pin_fallback(speaker_id)

        if not self.biometric_store.has_voice(speaker_id):
            await self._io.write(f"  [voice] '{speaker_id}' has no enrolled voiceprint -- "
                                 f"cannot verify audio that was provided.")
            return await self._offer_pin_fallback(speaker_id)

        # NO CHALLENGE WORDS (Defense, 2026-11-16). The voice the command
        # was spoken in is checked against the enrolled voiceprint -- every
        # command, a secure device's too. A voice that does not match is
        # asked for the PIN; nothing is ever read out to be repeated.
        if await self._passive_voice_check(speaker_id, audio):
            self._note_voice_ok(speaker_id)
            return True
        await self._io.write("  [voice] voice not recognised -- asking for "
                             "the PIN")
        passed = await self._offer_pin_fallback(speaker_id)
        if passed:
            self._note_voice_ok(speaker_id)
        return passed

    #: When each person last passed a voice check, per phone.
    _voice_ok_at: Dict[Tuple[str, str], float] = {}

    def _note_voice_ok(self, speaker_id: str) -> None:
        self._voice_ok_at[self._grace_key(speaker_id)] = time.monotonic()

    #: How long a remembered drive failure still describes what just
    #: happened. A reason from a minute ago belongs to a different
    #: attempt, and attaching it to this one would be a new kind of lie.
    _DRIVE_PROBLEM_TTL = 30.0

    def _drive_board_device(self, did: str, change: Dict[str, Any]) -> bool:
        """dm.mqtt_driver: actuate a board device over its own MQTT.

        Synchronous on purpose -- set_state is always called from a worker
        thread (_set_state / _carry_out wrap it in to_thread), and the
        drive is a short blocking publish with its own timeouts.
        Never raises: an actuation path that can take the command flow down
        with it is worse than one that says no.

        THE REASON IS KEPT, not just logged (2026-09-11). This seam returns
        a bare bool, so everything above it knew only "it did not work":
        DeviceManager.set_state then logged "no road to this device -- no
        board of ours" about a board it had just spoken to, and the person
        was told "FAILED (write_rejected)". Meanwhile this method had the
        real answer in its hand -- `offline`, which means Kitchen is not on
        the broker right now. It goes in a small box that _render reads a
        moment later, so the sentence a person gets names the board and
        what is actually wrong with it.
        """
        if "." not in did:
            return False
        slug, suffix = did.split(".", 1)
        colour = change.get("colour")
        try:
            ok, why = self.esp.drive_item_sync(
                slug, suffix,
                on=change.get("on"), level=change.get("level"),
                rgb=tuple(colour) if colour else None)
        except Exception as exc:                    # noqa: BLE001
            event("MQTT", f"direct drive {did} crashed: "
                          f"{type(exc).__name__}: {exc}")
            self._drive_problems[did] = (time.monotonic(),
                                         f"error:{type(exc).__name__}")
            return False
        if not ok:
            event("MQTT", f"direct drive {did}: {why}")
            self._drive_problems[did] = (time.monotonic(), why)
        else:
            self._drive_problems.pop(did, None)
        return ok

    def _drive_problem(self, did: str) -> str:
        """Why the last direct drive of `did` failed, if it was just now."""
        entry = self._drive_problems.get(did)
        if entry is None:
            return ""
        when, why = entry
        if time.monotonic() - when > self._DRIVE_PROBLEM_TTL:
            self._drive_problems.pop(did, None)
            return ""
        return why

    def _drive_problem_sentence(self, did: str, why: str) -> str:
        """A drive's machine verdict as something a person can act on.

        Every string drive_item_sync / _drive_publish_sync can return has a
        sentence here, because the whole point is that the person stops
        getting a word from inside the hub and starts getting the fact.
        """
        slug = did.split(".", 1)[0]
        board = slug
        try:
            board = (self.esp._read_board(slug) or {}).get("name") or slug
        except Exception:                              # noqa: BLE001
            pass
        known = {
            "offline": (f"the {board} board is offline, so nothing was "
                        f"switched. It is off, off the Wi-Fi, or still "
                        f"starting up."),
            "no-broker": ("the hub could not reach its own message broker, so "
                          "nothing was sent. Check that mosquitto is running."),
            "refused": (f"the message broker refused the login, so nothing was "
                        f"sent. {board}'s broker account may need rebuilding."),
            "no-creds": (f"{board} has no broker topic on file -- this hub did "
                         f"not provision it, so there is no way to speak to it."),
            "no-paho": ("the hub's MQTT library (paho-mqtt) is missing, so no "
                        "board can be driven at all."),
            "no board record": (f"{board}'s record is gone from this hub, so there is "
                                f"nothing to send to."),
            "no plan": f"{board} has no wiring on file, so there is no pin to drive.",
            "not a drivable output": (f"that is not something {board} can switch -- its pin "
                                      f"reads rather than drives."),
            "not a colour light": f"that light on {board} has no colour channels.",
        }
        if why in known:
            return known[why]
        if why.startswith("error:"):
            if "timeout" in why.lower():
                return (f"the {board} board did not answer in time, so it may "
                        f"not have switched.")
            return (f"sending to the {board} board failed "
                    f"({why.split(':', 1)[1]}).")
        return f"{board} did not take it ({why})."

    def _read_board_state(self, did: str) -> Dict[str, Any]:
        """dm.state_reader: what the board itself last published.

        Empty dict when the engine is not running, which leaves get_state
        exactly as it was rather than reporting a device as blank."""
        if self.automations is None:
            return {}
        try:
            return self.automations.latest(did)
        except Exception:                              # noqa: BLE001
            return {}

    async def _set_state(self, did: str, **kw) -> bool:
        """Apply a state change AND record it. Every direct dm.set_state()
        call in this class goes through here.

        A home-automation system whose console never says which device changed,
        from what, to what, is unauditable after the fact: you have the command
        that was spoken and the fact that something happened, and nothing
        connecting them. Reading the state either side is cheap next to the
        command that produced it.

        ASYNC, and the three dm calls run in ONE worker thread: get_state is
        an HA REST round trip when an entity is attached, and set_state is a
        REST call or a blocking MQTT connect+publish. On the event loop they
        stalled the reader, the keepalives and every other connection."""
        def _trio():
            b = self.dm.get_state(did)
            o = self.dm.set_state(did, **kw)
            return b, o, self.dm.get_state(did)
        before, ok, after = await asyncio.to_thread(_trio)
        # HUMAN OUTRANKS MACHINE: this path only ever runs for a person's
        # own command, so the device becomes immune to automations for the
        # override window. (The engine drives dm.set_state directly and so
        # never marks itself.)
        if ok and self.automations is not None:
            try:
                self.automations.note_human(did)
            except Exception:                      # noqa: BLE001
                pass
        if before == after:
            event("DEVICE", f"{did}: no change (still {after}) [requested {kw}]")
        else:
            event("DEVICE", f"{did}: {before} -> {after} [requested {kw}]")
        return ok

    def _device_options(self, ids: List[str]) -> List[Dict[str, str]]:
        """Turn device ids into option records. The only place that knows how
        a device should be labelled, shared by every chooser.

        This replaces _write_options, which formatted the same information
        into numbered lines and pushed it through write(). The numbering was
        baked into shared logic, so every backend inherited it -- including
        the phone, which then had to reverse-engineer the format to build
        controls. Options are data now; each backend renders them its own way.
        """
        opts: List[Dict[str, str]] = []
        for did in ids:
            d = self.dm.devices.get(did, {})
            opts.append({
                "value": did,
                "label": d.get("name") or did,
                "sub": d.get("room", ""),
            })
        return opts

    def _what_it_does(self, did: str) -> str:
        """What a device is for, said aloud: name, room, description, and how it
        is controlled. "The fan, in the kitchen: a fan that cools the kitchen.
        It switches on and off." The answer to "what does the fan do?"."""
        d = self.dm.devices.get(did) or {}
        name = d.get("name") or did
        where = f"The {name}" + (f", in the {d['room']}" if d.get("room") else "")
        desc = str(d.get("description") or "").strip().rstrip(".")
        head = f"{where}: {desc}." if desc else \
            f"{where}. It has no description yet."
        state = d.get("state") if isinstance(d.get("state"), dict) else {}
        if d.get("secure"):
            how = ("It locks and unlocks, and changing it always checks "
                   "your voice.")
        elif self.dm.is_readable_only(d):
            what = str(d.get("reading_key") or "").replace("_", " ")
            how = f"It can only be read{f' (its {what})' if what else ''}."
        elif d.get("ir_codes"):
            keys = [str(k).replace("_", " ") for k in d["ir_codes"]][:6]
            how = "It's worked by remote: " + ", ".join(keys) + "."
        elif self.dm.is_pressable(d):
            how = "It's pressed, like a button."
        else:
            from nlu.resolver import _is_adjustable
            if _is_adjustable(d):
                what = str(d.get("state_key") or "level").replace("_", " ")
                lo, hi = d.get("range_min"), d.get("range_max")
                lo = int(lo) if float(lo) == int(lo) else lo
                hi = int(hi) if float(hi) == int(hi) else hi
                how = (f"Its {what} goes from {lo} to {hi}"
                       + (", and it switches on and off." if "on" in state
                          else "."))
            else:
                how = "It switches on and off."
        return f"{head} {how}"

    async def _confirm_guess(self, decision) -> Optional[str]:
        """Ask "Did you mean the X?" and take the answer as it is given.

        Yes: that device. No: the next-best device the resolver ranked, up
        to three in all, asked the same way. When every one has been
        refused, say so and stop -- never act on the nearest thing. A
        cancel (or silence) ends it with nothing said."""
        guesses = list(decision.get("alternatives")
                       or [c["device_id"] for c in decision["candidates"]][:1])
        none_row = {"value": self._io._CHOICE_NONE, "label": "No",
                    "sub": "not that one"}
        for i, did in enumerate(guesses):
            if i == 0 and decision.get("message"):
                asked = decision["message"]
            else:
                d = self.dm.devices.get(did, {})
                what = " in the ".join(p for p in (d.get("name") or did,
                                                   d.get("room") or "") if p)
                asked = f"Did you mean the {what}?"
            got = await self._io.choose(
                asked, self._device_options([did]) + [none_row], polar=True)
            if not got:
                return None
            if got[0] != self._io._CHOICE_NONE:
                return got[0]
            event("CHOICE", f"{did}: no -- {len(guesses) - i - 1} guess(es) left")
        await self._io.write("I couldn't find it. Say the device's name.")
        return None

    async def _pick_one(self, options: List[str], prompt: str = "Pick") -> Optional[str]:
        if not options:
            return None
        picked = await self._io.choose(prompt, self._device_options(options))
        return picked[0] if picked else None

    async def _pick_multi(self, options: List[str], prompt: str = "Pick") -> List[str]:
        """Like _pick_one, but the user may choose several or all of them.
        Returns a list of device ids (possibly empty)."""
        if not options:
            return []
        return await self._io.choose(prompt, self._device_options(options),
                                     multi=True, allow_all=True)

    # ── Menu ────────────────────────────────────────────────

    #: The terminal menu: number -> (method name, label).
    #
    # ONE TABLE, used by print_menu, by the accept/reject test and by the
    # dispatch. It used to be three separate things -- a hand-drawn
    # two-column printout, a set comprehension over range(1, 15), and an
    # if/elif chain -- which is why the ESP actions were missing here for as
    # long as they existed: they were added to MENU_ACTIONS for the app and
    # there was no single place that would have shown them up as absent.
    #
    # The numbers 1-14 are unchanged and deliberately so. They are in
    # muscle memory and in the help text.
    CLI_MENU = {
        "1":  ("menu_add_device",    "Add device"),
        "2":  ("menu_remove_device", "Remove device"),
        "3":  ("menu_update_device", "Update device"),
        "4":  ("menu_list_devices",  "List devices"),
        "5":  ("menu_issue_command", "Issue command"),
        "6":  ("menu_add_scene",     "Add scene"),
        "7":  ("menu_list_scenes",   "List scenes"),
        "8":  ("menu_remove_scene",  "Remove scene"),
        # 9 KEEPS ITS NUMBER and gains a meaning. It used to run
        # menu_statistics, which counted devices and rooms under a heading
        # that promised measurements; it now runs the flow that actually
        # reads the sensors, which is what anyone pressing 9 was after.
        "9":  ("menu_readings",      "Readings & statistics"),
        "12": ("menu_enroll_user",   "Enroll voice"),
        "13": ("menu_setup_pin",     "Set PIN"),
        "14": ("menu_recover_pin",   "Forgot PIN"),
        # "Set up an ESP board" USED TO BE 15 AND IS DELIBERATELY GONE.
        #
        # It was a second front door to the same place: Add device already
        # opens with "Set up a new ESP board", and having both meant deciding
        # which one you wanted before you could start -- a question about
        # this menu rather than about your board. Adding a board IS adding a
        # device; the fact that it involves a cable is a detail of how, not a
        # different intention.
        #
        # The entries below are not duplicates of that. They are things you
        # can only do to a board that already exists, which is why they read
        # as maintenance rather than as a way in.
        # FOUR BOARD ROWS USED TO BE HERE: Retire a board, Edit a board,
        # Rename a board and Board credentials. They are all things you do to
        # a board that already exists, so they now live where the equivalent
        # device actions live -- retiring under Remove device, the other
        # three under Update device. The methods are unchanged and both
        # menus route to them; this table just stopped asking you to pick
        # between four words for "change the thing I already have".
        #
        # Change ESP Wi-Fi stays: it changes the network EVERY board joins,
        # which is a property of the system rather than of one board.
        "17": ("menu_change_wifi",   "Change ESP Wi-Fi"),
        "19": ("menu_learn_ir",      "Learn IR codes"),
        "20": ("menu_check_notices", "Pending actions"),
        # Reachable from the app since scenes were built, and never from
        # here. Found by diffing this table against MENU_ACTIONS, which is
        # the check that did not exist before.
        "21": ("menu_update_scene",  "Update scene"),
        "22": ("menu_pair_phone",    "Pair a new phone"),
    }

    def print_menu(self):
        print("\n" + "-" * 46)
        print("MAIN MENU")
        print("-" * 46)
        rows = [(n, label) for n, (_, label) in self.CLI_MENU.items()]
        rows += [("10", "Help"), ("11", "Quit")]
        # Two columns, filled down the left then the right, so the numbers
        # still read in order rather than zig-zagging.
        half = (len(rows) + 1) // 2
        left, right = rows[:half], rows[half:]
        for i in range(half):
            cell = f" {left[i][0]:>2}. {left[i][1]:<20}"
            if i < len(right):
                cell += f" {right[i][0]:>2}. {right[i][1]}"
            print(cell)
        print("-" * 46)
        print("  Or type a command directly")
        print("-" * 46)

    def print_help(self):
        print("""
  1. ADD DEVICE     — Register a new device.
  2. REMOVE DEVICE  — Delete a device.
  3. UPDATE DEVICE  — Change description.
  4. LIST DEVICES   — Show registry.
  5. ISSUE COMMAND  — Natural language control.
  6. ADD SCENE      — Create a scene.
  7. LIST SCENES    — Show scenes.
  8. REMOVE SCENE   — Delete a scene.
  9. READINGS       — Live sensor readings and 24 h statistics.
 10. HELP           — This text.
 11. QUIT           — Exit.
 12. ENROLL VOICE   — Record a voiceprint for a user.
 13. SET PIN        — Set a PIN fallback and show the recovery phrase.
 14. FORGOT PIN     — Reset a PIN using the recovery phrase.
 17. ESP WI-FI      — Change the network the boards join, and offer to
                      tell the ones that are switched on right now.
 19. LEARN IR       — Record a remote's codes so they can be replayed.
 20. PENDING        — Anything the hub is waiting on you for.
 21. UPDATE SCENE   — Change what a scene does.
 22. PAIR A PHONE   — Open the pairing window for one new phone.

 Setting up a board is under 1 (Add device); retiring one is under 2
 (Remove device); editing, renaming, its report and its credentials are
 under 3 (Update device -> A board).
""")

    async def menu_add_device(self, entry: str = ""):
        await self._io.write("\n--- ADD DEVICE ---")
        try:
            ha_entity_id = None
            ha_picked_name = None
            ha_picked_domain = None

            # STEP ZERO, and the reason this is one menu item rather than two.
            # Getting a device INTO Home Assistant and registering it HERE were
            # separate actions; from the outside it is one job: "add my bulb".
            # Anything HA has discovered is offered first, then the doors --
            # Set up an ESP board and (with HA) Add an integration. This ran
            # ONLY `if self.ha_client`, which took the ESP door away from any
            # hub without Home Assistant (Augusta, 2026-09-09: "set up an esp
            # board is gone"). An ESP board is the hub's own hardware,
            # so the doors are offered either way; _offer_pending_setup skips
            # the HA scan and the integration door when HA is absent.
            self._esp_handoff = False
            self._manual_add = False
            new_entities = await self._offer_pending_setup(entry)
            if new_entities is None:
                if self._esp_handoff:
                    # Handed over to the ESPHome installer. The flow is OVER
                    # and finished the way it should -- the cancel handler
                    # here would print "nothing saved" over the top of
                    # instructions the person is about to follow.
                    self._esp_handoff = False
                    return None
                if not self._manual_add:
                    raise CancelledByUser()
                # _manual_add: the person chose "enter a device by id" -- fall
                # through to the typed-id form below, no HA entity chosen.
            elif self.ha_client:
                # new_entities is a set (possibly empty) -> only reachable with
                # HA, so its controllable-entity picker is safe here.
                already_used = {d.get("ha_entity_id") for d in self.dm.devices.values()
                                if d.get("ha_entity_id")}
                controllable = await asyncio.to_thread(
                    self.ha_client.list_controllable_entities)
                entities = [e for e in controllable
                            if e["entity_id"] not in already_used
                            and (not new_entities or e["entity_id"] in new_entities)]
                if new_entities and not entities:
                    await self._io.write(
                        "  Its entities are not controllable ones -- nothing to "
                        "register yet. They may still be arriving.", level="warn")
                if entities:
                    # A CHOICE, not a numbered printout. This predated the
                    # app's tappable choice screen: seventy entities went out
                    # as text lines and the person typed an index back on a
                    # PHONE KEYBOARD, scrolling a feed that was never a list.
                    # choose() renders as buttons on the phone (scrollable,
                    # tappable, voice-selectable) and as the same numbered
                    # list on a terminal -- nothing lost, one thing gained.
                    options = [{"value": e["entity_id"],
                                "label": e["name"],
                                "sub": f"{e['state']} · {e['entity_id']}"}
                               for e in entities]
                    options.append({
                        "value": "__manual__",
                        "label": "Add one manually instead",
                        "sub": "an address, or an integration's own setup"})
                    picked = await self._io.choose(
                        "Pick a device to register", options)
                    if not picked:
                        raise CancelledByUser()
                    if picked[0] != "__manual__":
                        chosen = next(e for e in entities
                                      if e["entity_id"] == picked[0])
                        ha_entity_id = chosen["entity_id"]
                        ha_picked_name = chosen["name"]
                        ha_picked_domain = chosen["domain"]
                        # ha_client offers every domain a person might plausibly
                        # want registered; device_manager can only ACT on the
                        # ones with an unambiguous on/off service. Those two
                        # lists differ (media_player, vacuum,
                        # alarm_control_panel), so a device could be registered
                        # here and then refuse every command it was ever given,
                        # with the explanation buried in a log line at the far
                        # end. Say it once, at the point of choosing.
                        if ha_picked_domain not in self.dm.HA_ON_OFF_SERVICES:
                            await self._io.write(f"  ! '{ha_picked_domain}' has no on/off mapping yet, so "
                                  f"voice commands cannot switch this device.")
                            await self._io.write("    You can still register it -- its state will be "
                                  "readable -- but control needs a mapping adding to "
                                  "DeviceManager.HA_ON_OFF_SERVICES.")

            if ha_entity_id:
                # HA's own entity_id becomes ours too -- one id, not two
                # parallel identifiers for the same physical device.
                device_id = ha_entity_id
                spoken = ha_picked_name.lower()
                await self._io.write(f"  Using HA entity: {device_id} (voice name: '{spoken}')")
            else:
                device_id = await self._io.get_input("Device ID (e.g., living_room.lamp_01): ")
                spoken = device_id.split(".")[-1].replace("_", " ").replace("-", " ").strip().lower()

            # ── THE ONE SCREEN (her ruling, 2026-09-03) ──────────────────
            # Everything below used to be eight questions in a row. It is
            # one form now: prefilled with what Home Assistant already
            # knows (name, domain -> kind, live state, real range), the AI
            # an optional button inside it, every value editable.
            kind = "onoff"
            prefill: Dict[str, Any] = {}
            state_known: Optional[Dict[str, Any]] = None
            source = ""
            if ha_entity_id:
                dom = ha_picked_domain or ""
                if dom in self.dm.HA_READ_ONLY_DOMAINS:
                    kind = "reading"
                elif dom == "lock":
                    kind = "lock"
                elif dom in ("cover", "valve"):
                    kind = "open"
                elif dom in self.dm.HA_VALUE_SERVICES:
                    kind = "level"
                source = f"From Home Assistant -- {ha_entity_id}"
                raw = await asyncio.to_thread(self.ha_client.get_state, ha_entity_id)
                if raw is not None:
                    state_known = self.dm.translate_ha_state(ha_entity_id, raw)
                    profile = self.dm.ha_value_profile(ha_entity_id, raw)
                    if profile:
                        kind = "level" if kind == "onoff" else kind
                        prefill.update(
                            min=f"{float(profile.get('range_min', 0.0)):g}",
                            max=f"{float(profile.get('range_max', 100.0)):g}",
                            step=f"{float(profile.get('step', 1.0)):g}",
                            unit=str(profile.get("unit") or
                                     (state_known or {}).get("unit") or ""))
                else:
                    await self._io.write("  Could not read its live state from "
                                         "Home Assistant -- say what it is now.")
            else:
                source = f"Typed in -- {device_id}"
            family = (self._family_of_ha(ha_picked_domain or "", bool(prefill))
                      if ha_entity_id else "typed")
            kind_options = self._kind_options(family)
            position_kinds = self._position_kinds(family)
            # Whose numbers the form opens with: Home Assistant's, when it
            # reported a range; nobody's for a typed-in device.
            provenance = ({k: "ha" for k in ("min", "max", "step", "unit") if prefill.get(k)}
                          if ha_entity_id else {})
            if ha_entity_id:
                provenance["kind"] = "ha"
            spec = {
                "title": "Add device",
                "source": source,
                "editing": False,
                "kind_locked": sum(1 for o in kind_options if o["enabled"]) <= 1,
                "kind_options": kind_options,
                "position_kinds": position_kinds,
                "family": family,
                "state_known": state_known is not None,
                "values": {
                    "name": spoken, "room": "", "does": "", "kind": kind,
                    "min": prefill.get("min", ""), "max": prefill.get("max", ""),
                    "step": prefill.get("step", ""), "unit": prefill.get("unit", ""),
                    "secure": ha_picked_domain in ("lock", "alarm_control_panel",
                                                   "camera"),
                    "state": "", "show_on_home": True,
                    "provenance": provenance,
                },
            }
            values = await self._registration_loop(spec)
            fields = self._record_fields_from_values(values, state_known=state_known,
                                                     position_kinds=position_kinds)
            # Off the loop: add() embeds the description and rewrites the
            # registry (see the note at dm.update in the edit flow).
            ok = await asyncio.to_thread(lambda: self.dm.add(
                device_id,
                # "mqtt" marks a device THIS HUB'S BOARDS drive over their
                # own broker; a device picked from Home Assistant is HA's.
                transport="ha" if ha_entity_id else "mqtt",
                ha_entity_id=ha_entity_id,
                **fields,
            ))
            await self._io.write(f"\n{'Added' if ok else 'Error: already exists'}: {device_id}")
            # THE VERDICT, not None. A bare fall-through read as success in
            # run_menu_action, so "Error: already exists" arrived under the
            # green Done window.
            if not ok:
                self.failure_note = (f"A device with the id {device_id!r} "
                                     f"already exists -- nothing was saved.")
                return False
            return True

        except CancelledByUser:
            # Said, then PASSED ON -- the same law every other flow here
            # follows. Swallowed, it returned None, which run_menu_action
            # reads as a completed flow: the Done window over nothing saved.
            await self._io.write("\n(Cancelled — nothing saved.)")
            raise

    # ── the one registration screen ───────────────────────────────────
    #
    # Add device (a board item, a Home Assistant entity, a typed id) and
    # Edit device all end on the same form: room, name, what it does, how
    # it behaves (kind / min / max / step / unit -- typed by hand, or filled
    # by the AI from the name and the words), security, its state right
    # now, home screen. `_registration_loop` runs the screen; the callers
    # turn its answers into a record with `_record_fields_from_values`.

    _KINDS = ("onoff", "level", "open", "lock", "reading")
    _KIND_WORDS = {"lock": ("locked", "unlocked"), "open": ("open", "closed")}
    _KIND_LABELS = {"onoff": "On / off", "level": "Level", "open": "Open / closed",
                    "lock": "Locked / unlocked", "reading": "Reading"}

    #: THE KIND IS A FAMILY, NOT A LOCK (2026-09-10). What a part can be is
    #: decided by the part: a relay switches, so it may be called on/off,
    #: open/closed or locked/unlocked -- the same command, the person's
    #: words -- but it has no position and reads nothing; a PWM channel or
    #: a cover carries a position, so Open / closed on it MEANS a position
    #: (open = the top of its travel); a sensor only reads. Each family:
    #: (kinds that can be chosen, kinds that carry a range, the reason
    #: the others are greyed). "typed" is a device with no part behind it
    #: and every kind open.
    _KIND_FAMILIES: Dict[str, Tuple[Tuple[str, ...], Tuple[str, ...], str]] = {
        "relay":   (("onoff", "open", "lock"), (),
                    "a relay only switches -- it has no position and reads nothing"),
        "dimmer":  (("level", "onoff", "open"), ("level", "open"),
                    "a channel sets a level -- it is not a latch and reads nothing"),
        "cover":   (("open", "level", "onoff"), ("level", "open"),
                    "this part moves to a position -- lock words are for latches, and it reads nothing"),
        "sensor":  (("reading",), (),
                    "this part reads a value and cannot be switched"),
        "contact": (("reading",), (),
                    "this part reports open or closed and cannot be driven"),
        "event":   (("reading",), (),
                    "this part reports moments and cannot be driven"),
        "command": (("onoff", "open", "lock"), (),
                    "this part sends commands -- it has no position and reads nothing"),
        "latch":   (("lock",), (),
                    "a lock locks and unlocks"),
        "typed":   (("onoff", "level", "open", "lock", "reading"), ("level", "open"), ""),
    }

    @classmethod
    def _kind_options(cls, family: str) -> List[Dict[str, Any]]:
        """The kind chips for a family: every kind, enabled or not, with
        the reason when not -- the form shows them all, greyed where the
        part cannot honour them."""
        enabled, _pos, why = cls._KIND_FAMILIES.get(family, cls._KIND_FAMILIES["typed"])
        return [{"kind": k, "label": cls._KIND_LABELS[k], "enabled": k in enabled,
                 "why": "" if k in enabled else why}
                for k in cls._KINDS]

    @classmethod
    def _position_kinds(cls, family: str) -> List[str]:
        """The kinds that carry a range on this family. Level always does;
        Open / closed does when the part has a position; Reading may carry
        one optionally (the form shows the row for it regardless)."""
        _enabled, pos, _why = cls._KIND_FAMILIES.get(family, cls._KIND_FAMILIES["typed"])
        return ["level"] + [k for k in pos if k != "level"]

    @staticmethod
    def _family_of_part(type_id: str) -> str:
        """A board part's family from what the catalogue says it produces."""
        from esp_firmware import esphome_catalog as _cat
        dev = _cat.BY_ID.get(type_id)
        if dev is None:
            return "typed"
        prod = dev.produces
        if prod is _cat.EVENT_ONLY:
            return "event"
        if prod is _cat.COMMAND_ONLY:
            return "command"
        dom = getattr(prod, "domain", "")
        if dom == "cover":
            return "cover"
        if dom == "light":
            return "dimmer" if not getattr(prod, "binary", True) else "relay"
        if dom == "switch":
            return "relay"
        if dom == "binary_sensor":
            return "contact"
        if dom == "sensor":
            return "sensor"
        return "relay" if getattr(prod, "binary", True) else "dimmer"

    def _family_of_board_device(self, did: str) -> str:
        """The family of a device that lives on one of this hub's boards,
        by the part its board record names; "typed" when nothing is found."""
        if "." not in did:
            return "typed"
        slug, suffix = did.split(".", 1)
        try:
            record = self.esp._read_board(slug) or {}
        except Exception:                              # noqa: BLE001
            return "typed"
        for item in record.get("items") or []:
            if not isinstance(item, dict):
                continue
            if self.dm.board_device_id(slug, str(item.get("name") or "")) == did:
                return self._family_of_part(str(item.get("type_id") or ""))
        return "typed"

    @classmethod
    def _family_of_ha(cls, domain: str, has_profile: bool = False) -> str:
        # A CONTACT IS NOT A METER (2026-09-12). Both are read-only, so both
        # used to come back "sensor" -- whose reason line reads "this part
        # reads a value and cannot be switched", which is the wrong sentence
        # for a door. The "contact" family already exists for exactly this,
        # used by board reed switches and PIRs, and says "this part reports
        # open or closed and cannot be driven". Tested BEFORE the read-only
        # set so it is not swallowed by it.
        if domain == "binary_sensor":
            return "contact"
        if domain in DeviceManager.HA_READ_ONLY_DOMAINS:
            return "sensor"
        if domain in ("cover", "valve"):
            return "cover"
        if domain == "lock":
            return "latch"
        if domain in ("light", "fan", "climate", "water_heater"):
            return "dimmer" if (has_profile or domain != "light") else "relay"
        return "relay"

    @staticmethod
    def _kind_of_record(d: Dict[str, Any]) -> str:
        """The form's `kind` for a stored device."""
        if DeviceManager.is_readable_only(d):
            return "reading"
        words = d.get("state_words") if isinstance(d.get("state_words"), dict) else {}
        on_word = str(words.get("on", "")).lower()
        if d.get("state_key") == "level":
            # A level whose words are open/closed is a cover WITH a
            # position: still "Open / closed" to the person, with a range.
            return "open" if on_word == "open" else "level"
        if on_word == "locked":
            return "lock"
        if on_word == "open":
            return "open"
        entity = str(d.get("ha_entity_id") or "")
        if entity.startswith("lock."):
            return "lock"
        if entity.startswith(("cover.", "valve.")):
            return "open"
        return "onoff"

    def _form_values_from_record(self, did: str, d: Dict[str, Any]) -> Dict[str, Any]:
        state = d.get("state") if isinstance(d.get("state"), dict) else {}
        kind = self._kind_of_record(d)

        def num(x: Any) -> str:
            """A stored number as the form shows it; "" for anything that
            is not one (an HA "unknown", an old record's None)."""
            try:
                return f"{float(x):g}"
            except (TypeError, ValueError):
                return ""

        positional = d.get("state_key") == "level" and kind == "open"
        if kind == "level":
            now = num(state.get("level"))
        elif kind == "reading":
            now = ""
        else:
            on = bool(state.get("on"))
            words = self._KIND_WORDS.get(kind, ("on", "off"))
            now = words[0] if on else words[1]
        # A reading's range is kept as stored -- a record that was once
        # zeroed (the old save wrote 0/0/0 for every reading) shows empty
        # rather than a range of nothing.
        lo, hi, st = num(d.get("range_min")), num(d.get("range_max")), num(d.get("step"))
        if kind == "reading" and lo == "0" and hi == "0":
            lo = hi = st = ""
        prov = d.get("provenance") if isinstance(d.get("provenance"), dict) else {}
        return {
            "name": d.get("name") or did,
            "room": d.get("room") or "",
            "does": d.get("description") or "",
            "kind": kind,
            "positional": positional,
            "min": lo,
            "max": hi,
            "step": st,
            "unit": str(state.get("unit") or d.get("unit") or ""),
            "secure": bool(d.get("secure")),
            "state": now,
            "show_on_home": d.get("show_on_home") is not False,
            "provenance": dict(prov),
            "description_source": str(prov.get("description") or ""),
        }

    @staticmethod
    def _validate_form_values(v: Dict[str, Any], *,
                              kind_options: Optional[List[Dict[str, Any]]] = None,
                              position_kinds: Sequence[str] = ()) -> Optional[str]:
        """The one place the form's answers are checked. A sentence for
        the person when something is wrong, None when the values will do.

        A LEVEL needs its range; so does OPEN / CLOSED on a part with a
        position (position_kinds says which); a READING may carry one --
        empty is allowed, half-filled or nonsense is not."""
        name = str(v.get("name") or "").strip()
        if not name:
            return "Give it a name."
        # Home Assistant names arrive with dots, brackets and accents
        # ("Sonoff S31 (kitchen)", "Küche"); only control characters and
        # silly lengths are refused -- the name is spoken, not parsed.
        if len(name) > 64 or re.search(r"[\x00-\x1f]", name):
            return "Name: up to 64 characters, on one line."
        if not str(v.get("room") or "").strip():
            return "Pick a room."
        kind = str(v.get("kind") or "").strip().lower()
        if kind not in InteractiveShell._KINDS:
            return "Pick how it behaves."
        # ANY KIND MAY BE CHOSEN (her ruling, 2026-10-05). The options say
        # which kinds the part is MEANT to be (`enabled`), and the form
        # marks those; they are a recommendation, not a fence -- "somebody
        # connects a tunable socket and calls it a switch". It refused the
        # others until then.
        needs_range = kind == "level" or (kind == "open" and kind in position_kinds)
        given = [str(v.get(k) or "").strip() for k in ("min", "max", "step")]
        if needs_range or (kind == "reading" and any(given)):
            what = ("A level" if kind == "level" else
                    "A reading with a range" if kind == "reading" else
                    "A part that opens to a position")
            try:
                lo = float(v.get("min")); hi = float(v.get("max")); st = float(v.get("step"))
            except (TypeError, ValueError):
                return f"{what} needs its min, max and step as numbers."
            if not lo < hi:
                return "Min must be below max."
            if not st > 0 or st > (hi - lo):
                return "Step must be above zero and within the range."
            raw = str(v.get("state") or "").strip()
            if kind == "level" and raw:
                try:
                    val = float(raw)
                except ValueError:
                    return "Right now it is: a number, or leave it empty."
                if not lo <= val <= hi:
                    return "Right now it is: between min and max."
        return None

    def _record_fields_from_values(self, v: Dict[str, Any], *,
                                   state_known: Optional[Dict[str, Any]] = None,
                                   position_kinds: Sequence[str] = ()
                                   ) -> Dict[str, Any]:
        """Form answers -> the keyword arguments dm.add / dm.update take."""
        kind = str(v.get("kind") or "onoff").strip().lower()
        name = str(v.get("name") or "").strip().lower()
        fields: Dict[str, Any] = {
            "name": name,
            "room": str(v.get("room") or "").strip().lower(),
            # The AI's sentence when it was written for these very words
            # (the loop sets _description then), else the person's own.
            "description": (str(v.get("_description") or "").strip()
                            or str(v.get("does") or "").strip() or name),
            "secure": bool(v.get("secure")),
            "show_on_home": bool(v.get("show_on_home", True)),
        }

        def num(k: str) -> Optional[float]:
            try:
                return float(v.get(k))
            except (TypeError, ValueError):
                return None

        positional = kind == "open" and kind in position_kinds and num("min") is not None
        if kind == "level" or positional:
            fields.update(step=num("step"), range_min=num("min"),
                          range_max=num("max"), state_key="level")
        elif kind == "reading":
            # A reading KEEPS whatever range it was given -- the old save
            # wrote 0/0/0 here and wiped the power meter's 0-5000 W on
            # every edit (2026-09-10). Empty stays empty (None), which the
            # automation builder reads as "no bounds".
            fields.update(step=num("step"), range_min=num("min"),
                          range_max=num("max"), state_key="level",
                          readable_only=True)
        else:
            fields.update(**BINARY_ENVELOPE)
            fields["state_key"] = "on"
        words = self._KIND_WORDS.get(kind)
        fields["state_words"] = ({"on": words[0], "off": words[1]} if words else {})
        # THE STATE RIGHT NOW. Home Assistant's live word wins when it had
        # one; otherwise the person's answer; otherwise off / the bottom.
        if state_known is not None:
            state = dict(state_known)
        else:
            raw = str(v.get("state") or "").strip().lower()
            if kind == "level":
                state = {"level": float(raw) if raw else float(v.get("min"))}
            elif kind == "reading":
                state = {"level": 0.0}
            else:
                on = raw in {"on", "locked", "open", "yes", "true", "1"}
                state = {"on": on}
                if positional:
                    state["level"] = fields["range_max"] if on else fields["range_min"]
        unit = str(v.get("unit") or "").strip()
        if unit and (kind in ("level", "reading") or positional):
            state["unit"] = unit
        fields["state"] = state
        # WHOSE VALUES THESE ARE, per field, as the form reported them.
        prov = v.get("provenance") if isinstance(v.get("provenance"), dict) else {}
        prov = {k: str(t) for k, t in prov.items() if t in ("part", "ha", "ai", "you")}
        src = str(v.get("description_source") or "").strip().lower()
        if src in ("ai", "you"):
            prov["description"] = src
        if prov:
            fields["provenance"] = prov
        return fields

    async def _registration_loop(self, spec: Dict[str, Any]) -> Dict[str, Any]:
        """Show the form until it is saved with values that pass, running
        the AI for a `generate` in between. Raises CancelledByUser when the
        person backs out. Returns the accepted values."""
        spec = dict(spec)
        spec.setdefault("ai_available", bool(_LLM_AVAILABLE))
        # For the form: every kind may be picked; `enabled` in kind_options
        # is what the part is meant to be, to be marked, not enforced.
        spec.setdefault("kind_free", True)
        spec.setdefault("rooms", sorted(r for r in (self.dm.rooms or []) if r))
        spec.setdefault("values", {})
        while True:
            reply = await self._io.registration(spec)
            if reply is None:
                raise CancelledByUser()
            values = {**spec["values"], **(reply.get("values") or {})}
            spec["values"] = values
            spec["error"] = None
            if reply.get("action") == "generate":
                name = str(values.get("name") or "").strip()
                does = str(values.get("does") or "").strip()
                if not name:
                    spec["error"] = "Give it a name first -- the AI works from it."
                    continue
                await self._io.send_data("busy", {
                    "text": "Working out how it behaves",
                    "detail": "The AI is reading the name and what it does.",
                    "seconds": 45,
                })
                try:
                    meta = await asyncio.to_thread(describe_device, name, does)
                    # Parsed INSIDE the try: a malformed answer (a range
                    # that is not a number) is the AI failing, not the
                    # form -- it becomes the error line, not a dead flow.
                    if meta:
                        binary = is_binary_envelope(meta)
                        mins = f"{float(meta.get('range_min', 0.0)):g}"
                        maxs = f"{float(meta.get('range_max', 100.0)):g}"
                        steps = f"{float(meta.get('step', 10.0)):g}"
                        text = str(meta.get("description") or "")
                except Exception as exc:                  # noqa: BLE001
                    meta = None
                    spec["error"] = f"The AI could not answer ({exc}). Fill it in by hand."
                finally:
                    await self._io.send_data("busy", {"done": True})
                if meta:
                    # THE PROPOSAL, BESIDE THE PERSON'S OWN (2026-09-10):
                    # both are sent, the AI's is applied as the default,
                    # and every field it touched is marked "ai" so the
                    # form can show whose value is whose. The old app
                    # ignores `proposal` and `provenance` and sees exactly
                    # what it always saw.
                    allowed = {o["kind"] for o in (spec.get("kind_options") or [])
                               if isinstance(o, dict) and o.get("enabled", True)}
                    mine = {k: values.get(k, "") for k in
                            ("does", "kind", "min", "max", "step", "unit")}
                    mine["description"] = str(values.get("does") or "")
                    ai_kind = "onoff" if binary else "level"
                    if allowed and ai_kind not in allowed:
                        ai_kind = str(values.get("kind") or "")   # the part decides
                    ai: Dict[str, Any] = {"description": text, "kind": ai_kind}
                    if not binary:
                        ai.update(min=mins, max=maxs, step=steps,
                                  unit=str(meta.get("unit") or values.get("unit") or ""))
                    else:
                        ai.update(min=values.get("min", ""), max=values.get("max", ""),
                                  step=values.get("step", ""), unit=values.get("unit", ""))
                    spec["proposal"] = {"mine": mine, "ai": ai,
                                        "for": {"name": name, "does": does}}
                    prov = dict(values.get("provenance") or {})
                    if ai_kind != str(values.get("kind") or ""):
                        # An "on" left over from the old kind is not
                        # a level; a "40" is not an on/off word.
                        values["state"] = ""
                    if ai_kind:
                        values["kind"] = ai_kind
                        prov["kind"] = "ai"
                    if not binary:
                        values["min"], values["max"], values["step"] = mins, maxs, steps
                        prov.update(min="ai", max="ai", step="ai")
                        if ai.get("unit"):
                            values["unit"] = ai["unit"]
                            prov["unit"] = "ai"
                    values["provenance"] = prov
                    values["description_source"] = "ai"
                    spec["ai_text"] = text
                    spec["ai_for_does"] = does
                    # The AI's sentence is the description voice matching
                    # reads; the person's words stay in `does` untouched.
                continue
            problem = self._validate_form_values(
                values, kind_options=spec.get("kind_options"),
                position_kinds=tuple(spec.get("position_kinds") or ()))
            if problem:
                spec["error"] = problem
                continue
            # The stored description: the AI's sentence when it wrote one
            # for THESE words and the person kept the AI's side; else the
            # person's own words.
            if (spec.get("ai_text")
                    and str(values.get("description_source") or "ai") == "ai"
                    and str(values.get("does") or "").strip() ==
                    str(spec.get("ai_for_does") or "").strip()):
                values["_description"] = spec["ai_text"]
            return values

    def _rules_using(self, device_ids: List[str]) -> List[str]:
        """Automations that watch, test or drive any of these devices. A
        rule whose device is gone keeps firing into nothing and says so only
        in a journal nobody reads, so it is said BEFORE the removal."""
        eng = getattr(self, "automations", None)
        finder = getattr(eng, "rules_using_device", None)
        if finder is None:
            return []
        out: List[str] = []
        for did in device_ids:
            for name in finder(did):
                if name not in out:
                    out.append(name)
        return out

    def _scenes_using(self, device_id: str) -> List[str]:
        """Scenes that would silently lose an action if this device went.

        A scene keeps working with a missing device -- _fire_scene skips it
        with a log line nobody reads -- so the scene looks fine and quietly
        does less than it used to. Said out loud BEFORE the removal, while it
        can still be reconsidered.
        """
        if self.sm is None:
            return []
        return [s.name for s in self.sm.list_scenes()
                if any(a.device_id == device_id for a in s.actions)]

    async def menu_remove_device(self):
        """Remove a device from here, from Home Assistant, and OFF THE CHIP.

        There are FOUR layers to a registered ESP device: the registry entry
        here, the Home Assistant config entry, the board record on disk, and
        the template on the chip. This menu used to stop at the first (and
        offer the second), with a docstring explaining why the chip was out
        of scope. That explanation was the bug: a removed card reader kept
        its pins, kept its driver running, kept publishing -- removal that
        does not reach the layer where the thing RUNS is not removal. Now,
        when the picked device lives on a board, esp.remove_item strips it
        from the board's plan and pushes the corrected wiring -- over the
        air, or over the cable when the board cannot hear the broker.

        The Home Assistant entry is offered only when NOTHING ELSE NEEDS IT.
        One board with three relays is three devices here but ONE entry
        there, so deleting it on the first removal would take the other two
        with it. The chip is stripped BEFORE the HA delete, so a deleted
        entity is not immediately re-announced and rediscovered.
        """
        await self._io.write("\n--- REMOVE DEVICE ---")
        # RETIRING A BOARD IS REMOVING EVERY DEVICE ON IT, so it belongs
        # here rather than as its own menu row. It is listed second and
        # described plainly, because it is much the bigger of the two: it
        # takes the record, the broker account, the Home Assistant entry and
        # everything wired to the board.
        if self.esp.list_nodes():
            picked = await self._io.choose(
                "Remove what?",
                [{"value": "device", "label": "One device",
                  "sub": "the rest of its board stays as it is"},
                 {"value": "board", "label": "A whole board",
                  "sub": "every device on it, its account, and its record"}])
            # BACKING OUT IS A CANCEL, NOT A COMPLETION. Every bare
            # `return` in these flows fed run_menu_action a None, which
            # `result is not False` read as ok -- so the app threw the
            # centre DONE window for a flow that removed nothing (field
            # test 2026-08-26: "the done popup showing in places not
            # needed"). A person who picks nothing gets the cancelled
            # ending; only the job's true end may say Done.
            if not picked:
                raise CancelledByUser()
            if picked[0] == "board":
                # The sub-flow's own verdict, not an unconditional ok.
                return await self.menu_retire_board()

        if not self.dm.devices:
            await self._io.write("No devices.")
            self.failure_note = ("No devices are registered yet, so there "
                                 "is nothing to remove.")
            return False
        pick = await self._pick_one(list(self.dm.devices.keys()), "Remove")
        if not pick:
            raise CancelledByUser()
        return await self.menu_drop_device(pick)

    async def menu_drop_device(self, did: str) -> bool:
        """Remove ONE device by id -- all four layers, no picker.

        THE SAME REMOVAL, reached from the Devices screen's row menu
        instead of the wizard's list. It deliberately runs the WHOLE tail
        rather than a quicker delete: the scenes warning, the Home
        Assistant sibling check that stops one relay's removal taking its
        two siblings with it, the strip from the chip, and the offered HA
        delete. A row tap that skipped any of those would be a second,
        weaker removal path -- which is exactly how the deleted card reader
        used to keep its pins and go on publishing.

        The id arrives from the network, so it is LOOKED UP first and an id
        that is not in the registry removes nothing.
        """
        if did not in self.dm.devices:
            self.failure_note = ("That device is no longer here -- the list "
                                 "may be out of date.")
            return False
        device = dict(self.dm.devices[did])
        name = device.get("name") or did

        affected = self._scenes_using(did)
        if affected:
            await self._io.write(
                f"  ! {name} is used by {len(affected)} scene(s): "
                f"{', '.join(affected)}. Those scenes will still run, with "
                f"this device skipped.", level="warn")
        used_by = self._rules_using([did])
        if used_by:
            await self._io.write(
                f"  ! {name} is used by {len(used_by)} automation(s): "
                f"{', '.join(used_by)}. They will keep running but can no "
                f"longer do anything with it -- change or remove them.",
                level="warn")

        # ── Does anything else depend on its Home Assistant set-up? ──
        entry_id: Optional[str] = None
        siblings: List[str] = []
        entity_id = device.get("ha_entity_id")
        if entity_id and self.ha_client:
            # ONE registry read, indexed locally. Asking per device would be
            # a WebSocket connection each, for a question about a handful of
            # rows in a table we can just fetch whole.
            owners = await asyncio.to_thread(self.ha_client.entity_owners)
            entry_id = owners.get(entity_id)
            if entry_id:
                # `other`, NOT `did`: the comprehension's own loop variable
                # used to be called the same thing as this method's
                # argument, which made the guard read `did != did` -- always
                # false, so siblings came back empty and the flow would have
                # offered to delete a Home Assistant entry that two other
                # relays were still using. Exactly the disaster the sibling
                # check exists to prevent, caught by test_device_flows.
                siblings = [d.get("name") or other
                            for other, d in self.dm.devices.items()
                            if other != did
                            and owners.get(d.get("ha_entity_id") or "") == entry_id]

        also_ha = False
        if entry_id:
            if siblings:
                shown = ", ".join(siblings[:3])
                if len(siblings) > 3:
                    shown += f" and {len(siblings) - 3} more"
                await self._io.write(
                    f"  Its Home Assistant set-up stays -- {len(siblings)} "
                    f"other registered device(s) share it ({shown}).")
            else:
                picked = await self._io.choose(
                    f"{name} is the last device using its Home Assistant "
                    f"set-up.",
                    [{"value": "here", "label": "Unregister here only",
                      "sub": "Home Assistant keeps controlling it"},
                     {"value": "both", "label": "Remove from Home Assistant too",
                      "sub": "deletes the set-up and all of its entities"}])
                if not picked:
                    raise CancelledByUser()  # backed out -> cancelled, not Done
                also_ha = picked[0] == "both"

        if not self.dm.remove(did):
            await self._io.write(f"Could not remove {name}.", level="warn")
            self.failure_note = (f"{name} could not be removed from the "
                                 f"registry -- see the hub's log.")
            return False
        event("DEVICE", f"{did} unregistered (ha_entry={entry_id!r} "
                        f"also_ha={also_ha})")
        await self._io.write(f"Removed: {name}")

        # ── The chip itself ──  kitchen.card_reader names both a board and
        # an item on it; a manual device that merely shares the room prefix
        # matches no item and remove_item answers None without a word. Done
        # BEFORE the HA delete so a deleted entity is not being re-announced
        # by a board that still carries it.
        stripped = None
        if "." in did:
            slug, suffix = did.split(".", 1)
            if slug in self.esp.list_nodes():
                stripped = await self.esp.remove_item(slug, suffix)

        if also_ha:
            ok = await asyncio.to_thread(
                self.ha_client.delete_config_entry, entry_id)
            if ok:
                await self._io.write("  Removed from Home Assistant too.")
                # Stripped over the air or the cable, the chip no longer
                # announces it; nothing to warn.
                if stripped not in ("air", "cable"):
                    await self._io.write(
                        "  The device itself is still powered and still "
                        "announcing itself, so it will turn up in the next "
                        "scan. Unplug it, or retire the whole board, to stop "
                        "that.")
            else:
                await self._io.write(
                    "  Removed here, but Home Assistant would not delete its "
                    "set-up -- remove it from HA's own Devices & Services "
                    "page.", level="warn")
        return True

    async def _prompt_update_field(self, label: str, current: str,
                                   index: Optional[int] = None,
                                   total: Optional[int] = None,
                                   last: bool = False) -> Optional[str]:
        """One field in the update flow. Returns:
          None            -> keep the current value, advance
          "__BACK__"      -> caller should move to the previous field
          "__DONE__"      -> caller should stop here, keep everything else as-is
          any other str   -> the new value
        Raises CancelledByUser on 'cancel' (aborts the whole update).

        index/total/last ride along as wizard metadata so a graphical
        client can draw Next/Back/Cancel (Finish on the last field) instead
        of the typed ritual. The text stays the CLI's complete question."""
        raw = await self._io.ainput(
            f"  {label} [{current}] (Enter=keep, 'prev' for previous field, 'done' to save now): ",
            meta={"wizard": {"index": index, "total": total, "last": last}})
        v = raw.strip()
        if means_cancel(v):
            raise CancelledByUser()
        if v.lower() == "prev":
            return "__BACK__"
        if v.lower() == "done":
            return "__DONE__"
        if not v:
            return None  # keep current
        return v

    #: Board actions reached from Update device, rather than as rows of their
    #: own in the main menu.
    #:
    #: WHY THEY MOVED. "Edit a board", "Rename a board" and "Board
    #: credentials" were three top-level entries for things you do to a board
    #: that already exists -- so the menu asked you to decide which of four
    #: words covered your intention before you could begin. Changing what a
    #: board is called and changing what is wired to it are both "change
    #: something that exists", which is what Update device already means. The
    #: methods behind them are untouched; only the way in has changed.
    _BOARD_UPDATES = (
        ("wiring", "Change what is wired to it",
         "the pin screen, then it restarts into the new template",
         "menu_edit_board"),
        ("report", "Ask it how it is",
         "firmware build, drivers, I2C scan and live readings -- over "
         "the air, no web page",
         "menu_board_report"),
        ("name", "Rename it",
         "what this hub and Home Assistant call it",
         "menu_rename_board"),
        ("password", "Show its password",
         "for its own web page, or signing in to the broker by hand",
         "menu_show_esp_key"),
    )

    async def menu_board_report(self, slug: Optional[str] = None) -> bool:
        """Everything the bench used to fetch from Tasmota's own console,
        asked by the HUB over the air ("i thought we dont need that" --
        field test 2026-08-25, and she was right: the design is that
        nobody ever opens a board's web page; the hub asks, the person
        reads). Four questions, one screen: which firmware it runs, which
        drivers that firmware offers, what answers on the I2C bus, and
        what its sensors say RIGHT NOW.
        """
        slug, record = await self._which_board(slug)
        if not slug:
            return False
        name = record.get("name", slug)
        await self._io.write(f"\n--- {name.upper()} -- BOARD REPORT ---")

        async def ask(command, payload, suffix, want=None, settle=0.0):
            return await self.esp._mqtt_question(
                record, command, payload, suffix, want=want, settle=settle)

        heard = await ask("Status", "2", "STATUS2")
        if heard:
            version = re.search(r'"Version"\s*:\s*"([^"]+)"', heard[0])
            built = re.search(r'"BuildDateTime"\s*:\s*"([^"]+)"', heard[0])
            await self._io.write(
                f"  Firmware: {version.group(1) if version else '?'}"
                f"  built {built.group(1) if built else '?'}")
            await self._io.write(
                "    ^ must match the file check_firmware.py names, or the "
                "board is running an older image.")
        else:
            self.failure_note = (f"{name} did not answer. It is off, or not on "
                                 f"the network -- nothing below could be asked.")
            await self._io.write(f"  {self.failure_note}", level="warn")
            await self._io.choose("That is all it would say.",
                                  [{"value": "ok", "label": "OK",
                                    "sub": "back to the menu"}])
            return False

        heard = await ask("GPIOs", "", "RESULT", want='"', settle=1.5)
        if heard:
            names = sorted({m.group(1)
                            for line in heard
                            for m in re.finditer(
                                r'"([A-Za-z][^"]*)"\s*:\s*\d+', line)})
            await self._io.write(
                f"  Drivers its firmware offers ({len(names)}):")
            await self._io.write("    " + ", ".join(names))
            await self._io.write(
                "    ^ a device type must appear here (or ride I2C) before "
                "its wiring can work -- the enum-vs-driver law, asked of "
                "the board itself.")
        else:
            await self._io.write("  It did not list its drivers.",
                                 level="warn")

        # WHAT THE PINS ARE ACTUALLY SET TO (2026-09-12).
        #
        # `GPIOs` above is the ENUM -- every function this firmware KNOWS,
        # whether or not a single pin is assigned to one. ota_server.py has
        # a comment about that exact trap ("GPIO names such as 'TM1637 CLK'
        # are NOT used: those are in every build's enum whether or not the
        # driver is"), and this report was falling into it: it showed what
        # the board COULD do and never what it WAS doing.
        #
        # `GPIO` -- singular -- is the assignment. It is the question that
        # answers "the card reader didn't work" and "IR can't learn" on a
        # board whose template was lost, which is a thing that HAPPENS: a
        # heal out of safeboot brought Kitchen back with Wi-Fi, broker
        # account and topic intact and its DISPLAY settings gone
        # (2026-09-10, see _note_display_model). Settings live in the same
        # flash area, so a part whose pins are no longer assigned is silent
        # in a way nothing else here reports -- the driver is in the build,
        # the board is on the broker, and the chip is simply not wired to
        # anything as far as the firmware is concerned.
        #
        # The fix, when this finds one, is the same one the panel gets:
        # Edit a board -> keep everything -> send, which re-lays the
        # template and restarts.
        heard = await ask("GPIO", "", "RESULT", want='"', settle=1.5)
        if heard:
            # {"GPIO5":{"160":"RC522 CS"}, "GPIO0":{"0":"None"}, ...}
            assigned = {m.group(1).strip()
                        for line in heard
                        for m in re.finditer(r':\s*"([^"]+)"', line)}
            assigned.discard("None")
            await self._io.write(
                f"  Pins actually assigned ({len(assigned)}):")
            await self._io.write("    " + (", ".join(sorted(assigned))
                                           or "(none -- the template is empty)"))
            # Every part on this board's plan, against what the chip says.
            missing: List[str] = []
            for part in self._board_parts(record):
                want_comps = [c for c in (part.get("components") or [])
                              # I2C and SPI parts share one bus entry; a
                              # missing bus shows up as its own line rather
                              # than once per part that rides it.
                              if c]
                gone = [c for c in want_comps if c not in assigned]
                if gone:
                    missing.append(f"{part.get('name') or part.get('type_id')}"
                                   f" (no {', '.join(gone)})")
            if missing:
                await self._io.write(
                    "  WIRED ON PAPER, NOT ON THE CHIP: " + "; ".join(missing),
                    level="warn")
                await self._io.write(
                    "    ^ these parts are on this board's plan but their "
                    "pins are not assigned in the firmware, so they are "
                    "silent however well they are wired. This is what a "
                    "lost template looks like -- it can happen after a "
                    "board heals out of safeboot. Settings > Edit a board "
                    "> this board, keep everything, send: that re-lays the "
                    "template and restarts it.")
            else:
                await self._io.write(
                    "    ^ every part on this board's plan has its pins "
                    "assigned on the chip.")
        else:
            await self._io.write(
                "  It did not say what its pins are set to.", level="warn")

        heard = await ask("I2Cscan", "", "RESULT", want="I2CScan")
        if heard:
            found = re.search(r'"I2CScan"\s*:\s*"([^"]+)"', heard[0])
            await self._io.write(
                f"  I2C bus: {found.group(1) if found else heard[0][:120]}")
            await self._io.write(
                "    ^ every I2C device wired and powered should show its "
                "address here. Error 5 with nothing attached is normal -- "
                "an empty bus has no pull-ups.")
        else:
            await self._io.write(
                "  No I2C answer -- the board may have no bus assigned "
                "yet.", level="warn")

        heard = await ask("Status", "10", "STATUS10")
        if heard:
            blocks = sorted({m.group(1)
                             for m in re.finditer(
                                 r'"([A-Za-z][A-Za-z0-9_]+)"\s*:\s*\{',
                                 heard[0])}
                            - {"StatusSNS", "ANALOG"})
            await self._io.write(
                f"  Live sensor blocks: "
                f"{', '.join(blocks) if blocks else '(none reporting)'}")
            await self._io.write(f"    raw: {heard[0][:400]}")

        # WHAT IS ON THE PLAN AND NOT IN THE REGISTRY. This report is where
        # somebody looks when a board seems perfectly healthy and a device
        # is missing from every list -- so it is where the answer belongs.
        # Read off the board's own record, so it is true even when the
        # board did not answer a single question above.
        adrift = [item for s, _name, items in self._unregistered_board_devices()
                  if s == slug for item in items]
        if adrift:
            await self._io.write(
                f"  NOT REGISTERED: "
                f"{', '.join(str(i.get('name') or '?') for i in adrift)}")
            await self._io.write(
                "    ^ wired, and on this board's plan, but with no record "
                "in devices.json -- so nothing reads or drives them. "
                "Notifications offers 'Register them'.")
        # HOLD THE SCREEN. Every line above is an `output` frame, and a
        # menu action that ends the moment its last line is sent takes the
        # screen with it -- on the phone the whole report was "just a
        # flash" (field test 2026-09-10). A question is what keeps a flow
        # on screen, so the report ends with one that has a single answer.
        await self._io.choose("That is the report.",
                              [{"value": "ok", "label": "OK",
                                "sub": "back to the menu"}])
        return True

    async def _update_a_board(self) -> bool:
        picked = await self._io.choose(
            "What about it?",
            [{"value": key, "label": label, "sub": sub}
             for key, label, sub, _ in self._BOARD_UPDATES])
        if not picked:
            raise CancelledByUser()  # backing out is a cancel, not a failure
        method = next(m for k, _, _, m in self._BOARD_UPDATES
                      if k == picked[0])
        return bool(await getattr(self, method)())

    async def menu_update_device(self):
        await self._io.write("\n--- UPDATE DEVICE ---")
        # ASKED ONLY WHEN THERE IS A BOARD TO ASK ABOUT. On a hub with none,
        # this would be a question with one real answer, which is a worse
        # menu than no question at all.
        if self.esp.list_nodes():
            picked = await self._io.choose(
                "Change what?",
                [{"value": "device", "label": "A device",
                  "sub": "its name, room, description or range"},
                 {"value": "board", "label": "A board",
                  "sub": "its wiring, its name, or its password"},
                 # Here as well as Settings (field test 2026-08-25:
                 # "change esp wifi can be inside update devices right?").
                 # It is the one board-ish act that touches EVERY board,
                 # which is why it never fit under "A board".
                 {"value": "wifi", "label": "The Wi-Fi every board joins",
                  "sub": "moves all boards to a different network"}])
            # Same law as menu_remove_device: backing out is a CANCEL
            # (bare `return` used to read as ok and threw the Done window
            # over nothing), and a sub-flow reports its OWN verdict.
            if not picked:
                raise CancelledByUser()
            if picked[0] == "board":
                return await self._update_a_board()
            if picked[0] == "wifi":
                return await self.menu_change_wifi()

        if not self.dm.devices:
            await self._io.write("No devices.")
            self.failure_note = ("No devices are registered yet, so there "
                                 "is nothing to update.")
            return False
        pick = await self._pick_one(list(self.dm.devices.keys()), "Update")
        if not pick:
            raise CancelledByUser()
        return await self.menu_edit_device(pick)

    #: Every catalogue type that reads a card. Both are EVENT_ONLY -- a tap
    #: is a moment, never a Home Assistant entity -- so the hub hears them
    #: over its own MQTT and nowhere else.
    _CARD_TYPES = ("rc522", "pn532")

    #: How long "hold the card to the reader now" waits. Long enough to
    #: walk to the reader, short enough that a forgotten flow does not sit
    #: holding the shell open.
    CARD_TAP_SECONDS = 30.0

    async def _wait_for_tap(self, eng, readers: List[Tuple[str, str]],
                            seconds: float, text: str,
                            detail: str) -> Tuple[str, str]:
        """Wait for a NEW tap on any of `readers`: (uid, reader id), or
        ("", "") when none came in time. Every reader is baselined first, so
        an earlier UID is not mistaken for this tap. The wait is shown as a
        countdown bar."""
        before = {did: (eng.last_tag(did) or (None, 0.0))[1]
                  for did, _n in readers}
        await self._io.send_data("busy", {
            "text": text, "detail": detail, "seconds": int(seconds),
        })
        try:
            deadline = time.time() + seconds
            while time.time() < deadline:
                await asyncio.sleep(min(0.4, max(seconds, 0.01)))
                for did, _name in readers:
                    seen = eng.last_tag(did)
                    if seen is None or seen[1] <= before.get(did, 0.0):
                        continue
                    uid = str(seen[0] or "").strip()
                    if uid:
                        return uid, did
        finally:
            await self._io.send_data("busy", {"done": True})
        return "", ""

    def _card_readers(self) -> List[Tuple[str, str]]:
        """(device id, name) for every card reader on every board.

        THE BOARD IS THE REFEREE, here as in menu_learn_ir: what can read a
        card is read off the board records, never off a setting somebody
        has to keep true.
        """
        out: List[Tuple[str, str]] = []
        for slug in self.esp.list_nodes():
            record = self.esp._read_board(slug) or {}
            for item in (record.get("items") or []):
                if not isinstance(item, dict):
                    continue
                if item.get("type_id") not in self._CARD_TYPES:
                    continue
                name = str(item.get("name") or "").strip()
                if name:
                    out.append((self.dm.board_device_id(slug, name), name))
        return out

    async def menu_enroll_card(self) -> bool:
        """Bind a card to a person, by tapping it.

        THE SAME DOOR FOR BOTH CASES, exactly as enroll_voice is: the hub
        asks WHO, and what differs is only where that person is standing
        when they answer. Her correction, 2026-09-02 -- "the pin already
        gates both for owner and guest right" -- and she was right: there
        was never a second flow to build.

        WHAT THIS DOES NOT DO is give the card any authority. A UID is a
        name, not a proof (2026-08-16), so an enrolled card opens nothing
        an unenrolled one could not. What it buys is legibility: the
        journal says "Augusta's card tapped", and a rule can answer to a
        person instead of to sixteen hex characters.
        """
        await self._io.write("\n--- ENROL A CARD ---")

        # NO READER, THE LEARN-IR SHAPE (her ruling, 2026-09-01: "if no
        # card reader found then just the same warning as learn ir
        # codes"). Say the cause and the way back, and put it in
        # failure_note so the popup states WHY rather than "did not
        # complete".
        readers = self._card_readers()
        if not readers:
            await self._io.write(
                "  No board carries a card reader right now, so there is "
                "nothing to tap. Add device -> pick a board -> 'RFID card "
                "reader (RC522, SPI)' on free pins. The RC522 needs "
                "Omniscience's own firmware build; a board still on stock "
                "Tasmota takes the pins and never reads them, so use the "
                "PN532 there instead.", level="warn")
            self.failure_note = (
                "No card reader is registered, so there is nothing to tap. "
                "Add device -> 'RFID card reader (RC522, SPI)' puts one on "
                "a board.")
            return False

        # NOTHING IS LISTENING WITHOUT THE ENGINE. The reader publishes to
        # MQTT and the automation engine is the only thing subscribed, so
        # without it a tap is not late -- it is unheard.
        eng = self.automations
        if eng is None:
            self.failure_note = (
                "The automation engine is not running, and it is what "
                "listens to the reader -- so a tap would not be heard.")
            return False

        # READY? Checked before anyone is asked anything, whichever door
        # this came through (registration, the device page, Profile): each
        # reader on one of this hub's boards gets its own check (board
        # online, driver, pins), and the first reason it cannot work is said.
        mine = [(d, n) for d, n in readers if self._reader_part(d) is not None]
        if mine:
            await self._io.write("  Checking the reader...")
            ready, why = [], ""
            for did, name in mine:
                if await self.menu_check_reader(did, say=lambda _t: None,
                                                listen=False):
                    ready.append((did, name))
                else:
                    why = self.failure_note or why
            self.failure_note = None
            if not ready and len(mine) == len(readers):
                self.failure_note = why or "The card reader is not ready."
                await self._io.write(f"  {self.failure_note}", level="warn")
                return False
            readers = [r for r in readers if r not in mine] + ready

        # WHO IS DOING THE ADDING, read before anything can move it -- the
        # same reason menu_enroll_user reads it first.
        sponsor = (self.current_speaker_id or "").strip()
        try:
            people = sorted(self.biometric_store.list_users() or [])
        except Exception:                              # noqa: BLE001
            people = []
        if not people:
            self.failure_note = ("Nobody is enrolled on this hub yet, so "
                                 "there is no one to give a card to.")
            return False
        if len(people) == 1:
            user_id = people[0]
        else:
            # The sponsor first: the common case is your own card.
            ordered = ([sponsor] if sponsor in people else []) + \
                      [p for p in people if p != sponsor]
            picked = await self._io.choose("Whose card?", [
                {"value": p, "label": p,
                 "sub": "your own" if p == sponsor else "someone else here"}
                for p in ordered])
            if not picked:
                raise CancelledByUser()
            user_id = picked[0]

        # ALREADY HAS ONE? Then this row is a change, not an addition, and
        # the question is which. Same shape as the PIN row, which reads
        # "set - change it" rather than offering a second PIN.
        held = self.cards.cards_of(user_id)
        if held:
            picked = await self._io.choose(
                f"{user_id} already has a card ({', '.join(held)}).",
                [{"value": "add", "label": "Add another card",
                  "sub": "both will work"},
                 {"value": "replace", "label": "Replace it",
                  "sub": "the old card stops being recognised"},
                 {"value": "remove", "label": "Remove it",
                  "sub": "no card for this person"}])
            if not picked:
                raise CancelledByUser()
            if picked[0] == "remove":
                gone = self.cards.remove_all(user_id)
                await self._io.write(
                    f"  {gone} card(s) removed. {user_id} has no card now.")
                return True
            if picked[0] == "replace":
                self.cards.remove_all(user_id)

        # IS ANYONE LISTENING? The reader lives on a board; if the hub has
        # not heard that board on MQTT since it started, a tap cannot
        # arrive -- the board is off, on stock Tasmota (which never reads
        # an RC522), or publishing under a topic no record carries. Say
        # which before waiting, not after thirty silent seconds (field
        # report 2026-09-03: "it didn't appear in the app").
        silent = []
        for did, name in readers:
            slug = did.split(".", 1)[0]
            topic = (self.esp._read_board(slug) or {}).get("topic", "")
            if topic and topic not in eng._heard_topics:
                silent.append(f"{name} (board {slug!r}, topic {topic!r})")
        if silent and len(silent) == len(readers):
            self.failure_note = (
                "The reader's board has not been heard on MQTT since the hub "
                "started: " + "; ".join(silent) + ". Check the board is "
                "powered and connected to the broker, and that it runs "
                "Omniscience's own build -- stock Tasmota never reads an "
                "RC522.")
            await self._io.write(f"  {self.failure_note}", level="warn")
            return False

        # AND CAN THE HUB RECOGNISE A TAP FROM IT? (2026-09-12)
        #
        # The board being on the broker is not enough. This flow finds
        # readers by reading the board RECORD; the engine recognises a tap
        # by looking the reader up in an index built from the board's
        # PLAN -- and _plan_for REFUSES a recorded part for a dozen good
        # reasons (a slot with no pin, a pin that belongs to the module's
        # own RAM, one pin pointed at twice, a full family). A refused part
        # stays on the record and vanishes from the plan, so it is offered
        # here and unhearable there -- and the SAME refusal kept it out of
        # the flashed template, so the chip is not wired to it either.
        #
        # That is a thirty-second wait that can only ever end in "No card
        # was tapped", which is what it did. Asked before waiting now, and
        # the engine's own note says which part and why.
        deaf = []
        for did, name in readers:
            try:
                if not eng.hears(did, "tag"):
                    deaf.append(f"{name} ({did})")
            except Exception:                          # noqa: BLE001
                pass                                   # an older engine
        if deaf and len(deaf) == len(readers):
            try:
                why = "; ".join(eng.plan_drops())
            except Exception:                          # noqa: BLE001
                why = ""
            self.failure_note = (
                "This hub cannot recognise a tap from " + ", ".join(deaf)
                + ". It is on the board's record but not on the board's "
                  "plan"
                + (f" -- {why}" if why else "")
                + ". The same refusal kept it out of the firmware "
                  "template, so the chip is not reading those pins either. "
                  "Settings > Edit a board > that board will say what it "
                  "objects to; fixing the pins and sending puts it back.")
            await self._io.write(f"  {self.failure_note}", level="warn")
            return False

        # THE TAP: every reader baselined, a countdown bar while waiting.
        where = ", ".join(name for _d, name in readers)
        await self._io.write(f"  Hold the card to {where} now.")
        uid, _seen_on = await self._wait_for_tap(
            eng, readers, self.CARD_TAP_SECONDS,
            f"Hold the card to {where}", "Flat on the reader, for a second.")
        if not uid:
            await self._io.write("  No card was tapped.", level="warn")
            self.failure_note = (
                "No card was tapped, so nothing was saved. Hold the card "
                "flat on the reader for a second and try again.")
            return False

        ok, why = self.cards.add(uid, user_id, added_by=sponsor)
        if not ok:
            # `why` is the OTHER person's name when the card is taken --
            # refused rather than reassigned, so nobody quietly gains
            # somebody else's card.
            self.failure_note = (
                f"That card already belongs to {why!r}. Remove it from "
                f"them first, or use a different card."
                if why not in ("", "empty") else
                "That card could not be read clearly. Try tapping again.")
            await self._io.write(f"  {self.failure_note}", level="warn")
            return False
        await self._io.write(
            f"  {uid} is now {user_id}'s card. A tap says who is here -- "
            f"it opens nothing on its own.")
        return True

    # ── Check the card reader (its device page) ────────────────────────

    #: How long the reader check listens for a tap.
    READER_CHECK_SECONDS = 20.0

    def _reader_part(self, did: str
                     ) -> Optional[Tuple[str, Dict[str, Any], Dict[str, Any]]]:
        """(board slug, board record, item) when `did` is a card reader on one
        of this hub's boards -- the same readers as _card_readers -- else
        None."""
        slug = str(did).split(".", 1)[0]
        if not slug or slug not in self.esp.list_nodes():
            return None
        record = self.esp._read_board(slug) or {}
        for item in record.get("items") or []:
            if isinstance(item, dict) \
                    and item.get("type_id") in self._CARD_TYPES \
                    and self.dm.board_device_id(
                        slug, str(item.get("name") or "")) == did:
                return slug, record, item
        return None

    @staticmethod
    def _component_label(code: int, fallback: str = "") -> str:
        """The catalogue's name for a Tasmota component code ("RC522 CS",
        "Relay2"); the board's own name for it when the catalogue has none."""
        from esp_firmware import esphome_catalog as cat
        for dev in cat.BY_ID.values():
            for slot in dev.slots:
                comp = slot.component
                count = max(comp.slots, 1)
                if comp.code <= code < comp.code + count:
                    return comp.name + (str(code - comp.code + 1)
                                        if count > 1 else "")
        return fallback or f"function {code}"

    #: How long a board is offline before its notice.
    BOARD_OFFLINE_NOTICE_S = 120.0

    def _offline_board_notices(self) -> List[Dict[str, Any]]:
        eng = self.automations
        since = dict(getattr(eng, "_offline_since", None) or {}) \
            if eng is not None else {}
        if not since:
            return []
        out: List[Dict[str, Any]] = []
        for slug in self.esp.list_nodes():
            record = self.esp._read_board(slug) or {}
            went = since.get(record.get("topic") or "")
            if went is None or eng.now() - went < self.BOARD_OFFLINE_NOTICE_S:
                continue
            name = record.get("name") or slug
            out.append(self._notice(
                f"offline:{slug}", f"{name} is offline",
                "Its devices don't work until it's back. Check its power "
                "and Wi-Fi. This goes away by itself when it's back."))
        return out

    def _board_down(self, slug: str) -> str:
        """Why the board `slug` cannot be used right now (its last will says
        Offline), or "" when nothing says so."""
        eng = self.automations
        record = self.esp._read_board(slug) or {}
        topic = record.get("topic") or ""
        if eng is None or not topic or \
                topic not in (getattr(eng, "_offline", None) or ()):
            return ""
        return (f"The {record.get('name') or slug} board is offline -- power "
                f"it, or check its Wi-Fi.")

    def _reader_notice(self, did: str) -> Dict[str, Any]:
        name, why = (getattr(self, "_reader_problems", {}) or {}).get(
            did, (did, ""))
        return self._notice(f"reader:{did}", f"{name} is not ready",
                            why or "Its check did not pass -- see the hub's "
                                   "log (READER).")

    async def _reader_selfcheck(self) -> None:
        """At every start: each card reader is checked (board online, its
        driver, its pins) and the result written to the hub's log, so a
        reader that cannot work says why without anyone asking. Taps are
        logged as they come ("card ... tapped")."""
        for _ in range(60):                       # wait for the broker link
            eng = self.automations
            if eng is not None and getattr(eng, "_pub", None) is not None:
                break
            await asyncio.sleep(2)
        else:
            return
        await asyncio.sleep(10)                   # boards announce themselves
        for did, d in list((self.dm.devices or {}).items()):
            if self._reader_part(did) is None:
                continue
            name = d.get("name") or did
            lines: List[str] = []
            try:
                ok = await self.menu_check_reader(did, say=lines.append,
                                                  listen=False)
            except Exception as exc:              # noqa: BLE001
                event("READER", f"{name}: the check could not run "
                                f"({type(exc).__name__}: {exc})")
                continue
            for line in lines:
                if line:
                    event("READER", f"{name}: {line}")
            # The phone hears about a reader that cannot work (Notifications);
            # nothing is said while it is fine.
            problems = self.__dict__.setdefault("_reader_problems", {})
            if ok:
                problems.pop(did, None)
            else:
                why = next((ln for ln in reversed(lines) if ln), "")
                problems[did] = (name, why)
            event("READER", f"{name}: " + (
                "ready -- tap a card; each tap appears in this log as "
                "'card ... tapped'" if ok else
                "NOT READY -- fix the line above, then restart the hub"))
            self.failure_note = None

    async def menu_check_reader(self, did: str, *, say=None,
                                listen: bool = True) -> bool:
        """Check one card reader, step by step: its board is online, the
        firmware has its driver, its pins are set as the layout says, and a
        tap arrives. Each step says one sentence; the first failure that
        makes the rest pointless stops it, with failure_note saying why."""
        from esp_firmware import esphome_catalog as cat
        found = self._reader_part(did)
        if found is None:
            self.failure_note = ("That device is not a card reader on one of "
                                 "this hub's boards.")
            return False
        slug, record, item = found
        name = str(item.get("name") or did)
        board = record.get("name") or slug
        dev = cat.BY_ID.get(str(item.get("type_id")))
        eng = self.automations

        async def out(text: str, level: str = "info") -> None:
            """To the person (a flow), or to the hub's log (say)."""
            if say is not None:
                say(text.strip().strip("-").strip())
            else:
                await self._io.write(text, level=level)
        await out(f"\n--- CHECK THE CARD READER: {name} ---")

        async def fail(sentence: str) -> bool:
            self.failure_note = sentence
            await out(f"  {sentence}", level="warn")
            return False

        async def ask(command, payload, suffix, want=None, settle=0.0):
            return await self.esp._mqtt_question(
                record, command, payload, suffix, want=want, settle=settle)

        # 1. The board is online: its last will, else a quick question.
        offline = (f"The {board} board is offline -- power it, or check its "
                   f"Wi-Fi.")
        topic = record.get("topic") or ""
        if eng is not None and topic \
                and topic in (getattr(eng, "_offline", None) or ()):
            return await fail(offline)
        if not await ask("Status", "1", "STATUS1"):
            why = self.esp.listen_problem()
            return await fail(f"The hub could not ask the {board} board: "
                              f"{why}." if why else offline)
        await out(f"  The {board} board is online.")

        # 2. The firmware has the driver: the board's short `GPIOs` list,
        # checked by the provisioning helper against THIS reader alone.
        drivers = tuple(dev.driver_names) if dev is not None else ()
        if drivers:
            short = drivers[0].split()[0]           # "RC522 CS" -> RC522
            reply = await ask("GPIOs", "", "RESULT", want='"GPIO', settle=1.0)
            # The helper reads only plan.items[].type_id: a plan holding
            # just this reader asks about its drivers and no others.
            from types import SimpleNamespace
            only_reader = SimpleNamespace(
                items=[SimpleNamespace(type_id=dev.id)])
            missing = self.esp._missing_drivers(only_reader, reply)
            if missing is None:
                return await fail(f"The {board} board did not say which "
                                  f"drivers it has -- try the check again.")
            if missing:
                return await fail(
                    f"This board's firmware has no {short} driver (stock "
                    f"Tasmota). Reflash it from the hub so it gets "
                    f"HomeVoice's build.")
            await out(f"  The firmware has the {short} driver.")
        else:
            await out("  This reader needs no extra firmware "
                                 "driver.")

        # 3. The pins are set as the layout says: what the layout sends
        # (its template) against what the board reports now (`Gpio 255`).
        plan = self.esp._plan_for(slug)
        notes = list(self.esp._plan_notes)
        part = None if plan is None else next(
            (a for a in plan.items
             if self.dm.board_device_id(slug, a.name) == did), None)
        if part is None or dev is None:
            why = "; ".join(notes)
            return await fail(
                f"The layout for the {board} board leaves {name} out"
                + (f" ({why})" if why else "")
                + " -- fix its pins and resend the layout from the hub.")
        try:
            sent = list(plan.template()["GPIO"])
        except (ValueError, KeyError) as exc:
            return await fail(f"The layout for the {board} board cannot be "
                              f"sent ({exc}) -- fix it from the hub.")
        position = {gpio: i for i, gpio
                    in enumerate(plan.board.template_order)}
        has = self.esp._pins_assigned(await ask(
            "Gpio", "255", "RESULT", want='"GPIO', settle=1.0))
        if not has:
            return await fail(f"The {board} board did not say what its pins "
                              f"are set to -- try the check again.")
        wrong: List[str] = []
        for slot in dev.slots:
            pin = part.pins.get(slot.key)
            if pin is None or pin not in position:
                continue
            want = sent[position[pin]]
            label = self._component_label(want, slot.component.name)
            got = has.get(int(pin))
            if got is None:
                wrong.append(f"GPIO {pin} should be {label} but the board "
                             f"did not report that pin")
            elif got[0] != want:
                there = ("nothing" if got[0] == 0
                         else self._component_label(got[0], got[1]))
                wrong.append(f"GPIO {pin} should be {label} but the board "
                             f"has {there} there")
        if wrong:
            return await fail(", and ".join(wrong)
                              + " -- resend the layout from the hub.")
        await out("  The reader's pins are set as the layout "
                             "says.")

        if not listen:
            return True

        # 4. A real tap, heard the way enrolment hears one.
        if eng is None:
            return await fail("The automation engine is not running, so the "
                              "hub cannot hear a tap.")
        try:
            listening = eng.hears(did, "tag")
        except Exception:                              # noqa: BLE001
            listening = True                           # an older engine
        if not listening:
            return await fail(f"The hub is not listening to {name} yet -- "
                              f"resend the layout from the hub.")
        await out("  Tap a card on the reader now.")
        uid, _seen_on = await self._wait_for_tap(
            eng, [(did, name)], self.READER_CHECK_SECONDS,
            "Tap a card on the reader now", f"Listening to {name}.")
        if not uid:
            spi = {cat.C_SPI_CLK, cat.C_SPI_MOSI, cat.C_SPI_MISO}
            if any(slot.component in spi for slot in dev.slots):
                return await fail(
                    "No tap arrived. The firmware and pins are right, so "
                    "it's the wiring or power: the module needs 3.3 V (not "
                    "5 V), SDA goes to chip select, RST must be wired, IRQ "
                    "stays unconnected, and SCK/MOSI/MISO must not be "
                    "swapped.")
            return await fail("No tap arrived. The firmware and pins are "
                              "right, so it's the wiring or power. "
                              + dev.note)
        await out(f"  A card was read: {uid}")
        return True

    # menu_hide_ha IS GONE with the Home Assistant event ear it existed for
    # (owner's decision, 2026-09-02): HA entities no longer appear in the
    # automation builder, so there is nothing to hide from it.

    async def menu_register_missing(self) -> bool:
        """Give a record to everything on a board that has none.

        The gap `_unregistered_board_devices` reports, closed in one act:
        each of them goes through THE REGISTRATION FORM, one at a time
        (her ruling, 2026-10-05: every new device does -- it is the only
        way this hub comes to understand one). It used to register them
        plainly, with the catalogue's own sentence and nothing asked.
        Backing out of a form ends the act; what was not reached stays on
        the notice.

        HOME ASSISTANT IS NOT CONSULTED, on either count. The law from
        2026-08-26: Home Assistant not showing a device -- slowly, or ever
        -- must never decide whether the device EXISTS. `local_first=True`
        is what lets a real thing on a real pin have a record with no
        entity behind it, and the attach courier marries the two later, in
        the background, needing nothing from here.
        """
        await self._io.write("\n--- REGISTER WHAT IS MISSING ---")
        adrift = self._unregistered_board_devices()
        if not adrift:
            await self._io.write(
                "  Every device on every board already has a record.")
            return True
        created_all: List[str] = []
        for slug, board_name, items in adrift:
            matches = self.dm.match_board(items, slug, board_name, [],
                                          local_first=True, has_ha=False)
            created, _ = await self.boards_reg.describe_and_register(
                matches, slug, board_name)
            if self.automations is not None:
                self.automations.boards_changed()
            created_all += created
            for did in created:
                device = self.dm.devices.get(did, {})
                await self._io.write(
                    f"  {device.get('name', did)} ({board_name}) -- "
                    f"{device.get('description', '')}")
        if not created_all:
            self.failure_note = ("Nothing could be registered -- the board "
                                 "records name devices this hub's catalogue "
                                 "does not recognise. See the hub's log.")
            return False
        await self._io.write(f"\n  Registered {len(created_all)}.")
        # The notice has served its purpose; take it down rather than
        # leaving a solved problem on the list.
        await self._io.send_data("notice_gone", {"id": "boards:unregistered"})
        return True

    # ── using a device from its page in the app (2026-11-05) ─────────

    def _use_of(self, did: str, d: Dict[str, Any],
                state: Any) -> Dict[str, Any]:
        """Which controls a device's page shows: {"kind": ...} and what that
        kind needs. Read off what the device IS -- its board part, or its
        Home Assistant domain -- never guessed from its name."""
        state = state if isinstance(state, dict) else {}
        codes = d.get("ir_codes")
        if isinstance(codes, dict) and codes:
            return {"kind": "ir", "buttons": sorted(codes)}
        if "." in did and d.get("transport") == "mqtt" and self.esp:
            slug, suffix = did.split(".", 1)
            plan = self.esp._plan_for(slug)
            if plan is not None:
                want = self.esp._snake_name(suffix)
                item = next((i for i in plan.items
                             if self.esp._snake_name(i.name) == want), None)
                if item is not None and \
                        item.type_id in self.esp._display_types():
                    return {"kind": "display", **self._board_display(slug)}
                if self.esp._colour_target(plan, suffix) is not None:
                    return {"kind": "colour"}
                target = self.esp._drive_target(plan, suffix)
                if target is not None:
                    if target[1] == "shutter":
                        return {"kind": "servo" if item is not None and
                                item.type_id == "servo" else "cover"}
                    if target[1] == "relay":
                        return {"kind": "switch"}
                    return {"kind": "dimmer", "min": 0, "max": 100,
                            "step": 5}
        domain = str(d.get("ha_entity_id") or "").split(".", 1)[0]
        if domain == "light":
            if self.dm.is_colourable(d):
                return {"kind": "colour"}
            if d.get("state_key") != "on":
                return {"kind": "dimmer", "min": 0, "max": 100, "step": 5}
            return {"kind": "switch"}
        if domain == "cover":
            return {"kind": "cover"}
        if domain in ("button", "input_button", "scene", "script"):
            return {"kind": "press"}
        if self.dm.is_readable_only(d):
            return {"kind": "reading"}
        if d.get("state_key") == "on" or "on" in state:
            return {"kind": "switch"}
        if d.get("range_max") is not None:
            return {"kind": "dimmer", "min": d.get("range_min") or 0,
                    "max": d.get("range_max"), "step": d.get("step") or 1}
        return {"kind": "none"}

    def _board_display(self, slug: str) -> Dict[str, Any]:
        """{"display": {...}} for a board with a text panel, else {}."""
        try:
            panel = next((p for p in self.esp.display_targets()
                          if p.get("slug") == slug), None)
        except Exception:                          # noqa: BLE001
            panel = None
        if panel is None:
            return {}
        did = self.dm.board_device_id(slug, panel.get("name") or "display")
        return {"display": {"device": did, "rows": panel.get("rows") or 2,
                            "cols": panel.get("cols") or 16,
                            "name": panel.get("name") or "Display"}}

    def _display_readings(self) -> List[Dict[str, str]]:
        """Every reading a display line can quote: a label for the picker
        and the token the hub fills in live ({device:key})."""
        eng = self.automations
        if eng is None:
            return []
        out: List[Dict[str, str]] = []
        for did, d in self.dm.devices.items():
            try:
                keys = eng.reading_keys(did)
            except Exception:                      # noqa: BLE001
                keys = []
            name = d.get("name") or did
            for key in keys:
                # The value now, so the app's preview shows a real number.
                got = (getattr(eng, "_last", {}) or {}).get((did, str(key)))
                words = (eng.key_words(key) if hasattr(eng, "key_words")
                         else str(key).replace("_", " "))
                out.append({"label": f"{name} · {words}",
                            "token": f"{{{did}:{key}}}",
                            "value": "" if got is None
                            else eng._shown(got[0])})
        return out

    async def menu_use_device(self, argument: str) -> bool:
        """One act on one device, from its page: "<id>|<what>|<value>".

        what: on | off | level <0-100> | colour <r,g,b> | open | close |
        position <0-100> | press <button> | clear |
        show {"text" or "pages", "for_s", "page_s", "clock_between"}.
        A SECURE device always asks for the PIN first (her rule: security
        devices are always asked), however recently it was asked."""
        import json as _json
        did, _, rest = argument.partition("|")
        what, _, value = rest.partition("|")
        d = (self.dm.devices or {}).get(did)
        if d is None and what not in ("show", "clear"):
            self.failure_note = ("That device is no longer here -- the list "
                                 "may be out of date.")
            return False
        name = (d or {}).get("name") or did
        if d is not None and d.get("secure"):
            who = (getattr(self, "current_speaker_id", None)
                   or getattr(self, "_ws_last_speaker", None))
            if not who:
                self.failure_note = ("This hub does not know who is holding "
                                     "this phone, so it cannot open a secure "
                                     "device. Enrol a voice first.")
                return False
            ok, why = await self.confirm_with_pin(who, f"use {name}")
            if not ok:
                self.failure_note = f"{why} {name} was not changed."
                return False

        def number(default: float = 0.0) -> float:
            try:
                return max(0.0, min(100.0, float(value)))
            except ValueError:
                return default

        ok = False
        if what in ("on", "off"):
            ok = await self._set_state(did, on=(what == "on"))
        elif what == "level":
            v = number()
            ok = await (self._set_state(did, on=False) if v <= 0 else
                        self._set_state(did, on=True, level=v))
        elif what in ("open", "close"):
            ok = await self._set_state(did, on=(what == "open"))
        elif what == "position":
            ok = await self._set_state(did, level=number())
        elif what == "colour":
            try:
                rgb = tuple(max(0, min(255, int(x)))
                            for x in value.split(","))[:3]
            except ValueError:
                rgb = ()
            if len(rgb) != 3:
                self.failure_note = "That colour could not be read."
                return False
            ok = await asyncio.to_thread(self.dm.set_colour, did, rgb)
            if ok and self.automations is not None:
                self.automations.note_human(did)
        elif what == "press":
            if isinstance((d or {}).get("ir_codes"), dict):
                ok = await self._press_ir(did, value, None)
            else:
                ok = await asyncio.to_thread(self.dm.press, did)
        elif what in ("show", "clear"):
            eng = self.automations
            if eng is None:
                self.failure_note = "Displays need the automation engine, " \
                                    "which is not running."
                return False
            if what == "clear":
                ok = eng.clear_now(did)
            else:
                try:
                    line = _json.loads(value)
                    text = str(line.get("text", ""))
                    hold = float(line.get("for_s", 0))
                    pages = line.get("pages")
                    page_s = line.get("page_s")
                    page_s = None if page_s is None else float(page_s)
                    clock_between = bool(line.get("clock_between"))
                except (ValueError, TypeError, AttributeError):
                    self.failure_note = "That message could not be read."
                    return False
                problem = eng.show_now(did, text, hold, pages=pages,
                                       page_s=page_s,
                                       clock_between=clock_between)
                if problem:
                    self.failure_note = problem[:1].upper() + problem[1:]
                    return False
                ok = True
        else:
            self.failure_note = f"Nothing called {what!r} can be done here."
            return False
        if not ok and not self.failure_note:
            why = self._drive_problem(did)
            self.failure_note = (self._drive_problem_sentence(did, why)
                                 if why else f"{name} did not answer.")
        return bool(ok)

    async def menu_main_meter(self, argument: str) -> bool:
        """"<id>|<key>|<power|current>": the meter whose jumps teach the hub
        what each device draws; "none": no main meter. Another meter forgets
        what was learned. Ungated: it switches nothing and shows no state."""
        eng = self.automations
        if eng is None:
            self.failure_note = ("That needs the automation engine, which is "
                                 "not running.")
            return False
        if argument.strip().lower() == "none":
            await asyncio.to_thread(eng.clear_main_meter)
            await self._io.write("  No main meter now.")
            return True
        did, _, rest = argument.partition("|")
        key, _, measures = rest.partition("|")
        problem = await asyncio.to_thread(eng.set_main_meter, did, key, measures)
        if problem:
            self.failure_note = problem
            return False
        name = ((self.dm.devices or {}).get(did) or {}).get("name") or did
        await self._io.write(f"  {name[:1].upper()}{name[1:]} is the main meter now.")
        return True

    async def menu_forget_draw(self, did: str) -> bool:
        """"<id>": forget what one device draws (it is learned again)."""
        eng = self.automations
        if eng is None:
            self.failure_note = ("That needs the automation engine, which is "
                                 "not running.")
            return False
        d = (self.dm.devices or {}).get(did)
        if d is None:
            self.failure_note = ("That device is no longer here -- the list "
                                 "may be out of date.")
            return False
        name = d.get("name") or did
        await asyncio.to_thread(eng.forget_draw, did)
        await self._io.write(f"  {name[:1].upper()}{name[1:]}: what it draws is forgotten.")
        return True

    async def menu_show_on_home(self, did: str) -> bool:
        """Put one device's readings on the home screen, or take them off.

        UNGATED, DELIBERATELY, and it is the only device action that is.
        Everything in menu_edit_device renames a thing, moves it between
        rooms or marks it security-critical, so that flow is gated as
        `update_device` and should be. This is a preference about YOUR OWN
        SCREEN -- it opens nothing, reveals nothing, and changes nothing
        physical. Asking for a PIN to hide a temperature reading teaches
        people to type it without reading the prompt, which is how a PIN
        stops meaning anything (her ruling, 2026-09-01: "no pin to add the
        device to the home screen, same with taking it off").

        A TOGGLE, not a setting to spell out. The tap already said which
        device and the flag has two states, so there is nothing left to
        ask. None means never asked and reads as YES -- an older record,
        or a path that never offered the question -- so the first toggle
        on one of those turns it off, which is what somebody tapping
        "hide" expects.
        """
        device = (self.dm.devices or {}).get(did)
        if device is None:
            self.failure_note = ("That device is no longer here -- the list "
                                 "may be out of date.")
            return False
        # Flip what the Devices card SHOWS (menu_list_devices' on_home): a
        # switchable is on home only when asked (None reads as off), a
        # reading unless hidden (None reads as on).
        state = self._cached_state(did, device)
        if isinstance(state, dict) and "on" in state:
            showing = device.get("show_on_home") is True
        else:
            showing = device.get("show_on_home") is not False
        wanted = not showing
        if not self.dm.update(did, show_on_home=wanted):
            self.failure_note = "That could not be saved."
            return False
        name = device.get("name") or did
        await self._io.write(
            f"  {name} {'now shows' if wanted else 'no longer shows'} on the "
            f"home screen.")
        return True

    async def menu_edit_device(self, did: str) -> bool:
        """Edit ONE device by id, no picker.

        THE LIST IS THE PLACE (her merge law, applied to devices after
        automations and scenes): the Devices screen IS the feature, so
        editing is a tap on the thing rather than a trip through a question
        flow that exists for the voice. menu_update_device keeps the picker
        -- and the board / Wi-Fi fork above it -- because a spoken "update a
        device" has nothing to point at.

        The id arrives from the network, so it is LOOKED UP and never
        trusted: an id that is not in the registry edits nothing.

        CancelledByUser is NOT caught here. It used to be, and the handler
        wrote "(Cancelled -- nothing saved.)" and returned None, which
        run_menu_action reads as success -- so backing out of an edit ended
        in the green Done window. Same defect the scene wizards had; the
        transport layer owns the cancelled ending.
        """
        if did not in self.dm.devices:
            self.failure_note = ("That device is no longer here -- the list "
                                 "may be out of date.")
            return False
        d = self.dm.devices[did]

        # Devices registered before the envelope was read from HA still carry
        # the generic 0-100 "percent" guess. Home Assistant's own numbers go
        # onto the form as the prefill, where they can be seen and kept or
        # changed -- no separate question in front of the screen.
        entity_id = d.get("ha_entity_id")
        if entity_id and self.ha_client and d.get("transport") != "mqtt":
            try:
                profile = await asyncio.to_thread(self.dm.ha_value_profile, entity_id)
            except Exception:                              # noqa: BLE001
                profile = None
            if profile and any(d.get(k) != v for k, v in profile.items()):
                d = {**d, **profile}
                await self._io.write(
                    f"  Home Assistant reports {profile.get('range_min')}-"
                    f"{profile.get('range_max')} (step {profile.get('step')}); "
                    f"that is what the form shows.")

        # THE SAME SCREEN AS ADD, prefilled (her merge law: one form for
        # add and edit). device_id itself is deliberately NOT editable: it
        # is the key scenes, context and undo reference. Show on home IS on
        # the form -- it is a preference, so saving it here costs nothing
        # more than the gate this whole flow already sits behind.
        entity = str(d.get("ha_entity_id") or "")
        # A BOARD DEVICE is one whose id names a board of ours -- not
        # merely transport "mqtt", which older typed-in records carry too.
        try:
            boards = set(self.esp.list_nodes())
        except Exception:                              # noqa: BLE001
            boards = set()
        is_board = (d.get("transport") == "mqtt" and "." in did
                    and did.split(".", 1)[0] in boards)
        values = self._form_values_from_record(did, d)
        # THE FAMILY, from the part or the entity; the chips the family
        # allows are open, the rest greyed with the reason (2026-09-10).
        if is_board:
            family = self._family_of_board_device(did)
        elif entity and "." in entity:
            family = self._family_of_ha(entity.split(".", 1)[0], bool(values.get("min")))
        else:
            family = "typed"
        kind_options = self._kind_options(family)
        position_kinds = self._position_kinds(family)
        prov = dict(values.get("provenance") or {})
        origin = ("part" if is_board and values["kind"] in ("reading", "level", "open")
                  else "ha" if entity else "")
        if origin:
            for k in ("min", "max", "step", "unit"):
                if values.get(k) and k not in prov:
                    prov[k] = origin
            prov.setdefault("kind", origin)
        values["provenance"] = prov
        spec = {
            "title": "Edit device",
            "source": (f"From Home Assistant -- {entity}" if entity and not is_board
                       else (f"On board {did.split('.', 1)[0]}" if is_board
                             else f"Typed in -- {did}")),
            "editing": True,
            "kind_locked": sum(1 for o in kind_options if o["enabled"]) <= 1,
            "kind_options": kind_options,
            "position_kinds": position_kinds,
            "family": family,
            # The state is asked again only where nothing else reports it.
            "state_known": bool(entity) or is_board,
            "values": values,
        }
        new_values = await self._registration_loop(spec)
        fields = self._record_fields_from_values(
            new_values,
            state_known=(d.get("state") if spec["state_known"] else None),
            position_kinds=position_kinds)
        if is_board:
            # THE PART DECIDES WHAT THE DEVICE IS (its state key and whether
            # it can be driven); the person decides its words, its range
            # and its step. A reading's or a cover's range edited here IS
            # kept -- it used to be thrown away with the rest of the
            # envelope, so a step of 25 on a gate could never become 10.
            for k in ("state_key", "readable_only"):
                fields.pop(k, None)
            for k in ("step", "range_min", "range_max"):
                if fields.get(k) is None:
                    fields.pop(k, None)
            typed = fields.pop("state", None) or {}
            kept = dict(d.get("state") or {}) if isinstance(d.get("state"), dict) else {}
            if typed.get("unit") and typed["unit"] != kept.get("unit"):
                kept["unit"] = typed["unit"]
                fields["state"] = kept
        # OFF THE LOOP. update() re-embeds the description with the sentence
        # encoder and rewrites the registry with an fsync: on the event
        # loop that froze the hub for as long as it took -- 19.7 s once,
        # long enough for the phone to drop the connection (field log,
        # 2026-10-05, "FREEZE ... stuck for 19.7 s" after every edit).
        if await asyncio.to_thread(lambda: self.dm.update(did, **fields)):
            await self._io.write(f"\nUpdated: {did}")
            return True
        await self._io.write("\nUpdate failed.", level="warn")
        self.failure_note = (f"{d.get('name') or did} could not be updated "
                             f"-- see the hub's log.")
        return False

    def _cached_state(self, did: str, d: Dict[str, Any]) -> Dict[str, Any]:
        """A device's state WITHOUT touching the network: the board's own
        MQTT cache merged onto the stored copy, and never a Home Assistant
        REST call. live_reading_rows runs on a timer from ws_server, off
        the shell lock and on the event loop; dm.get_state there was a
        blocking HTTP round trip per HA-backed device every tick."""
        cached = d.get("state")
        cached = dict(cached) if isinstance(cached, dict) else {}
        if d.get("transport") == "mqtt":
            live = self._read_board_state(did)
            if live:
                cached.update(live)
        return cached

    async def _states_of(self, dids: List[str]) -> Dict[str, Dict[str, Any]]:
        """dm.get_state for each id, gathered in ONE worker thread. The
        per-device loops in list/status/query used to call it on the
        event loop, one blocking HA round trip at a time."""
        def _read():
            out = {did: self.dm.get_state(did) for did in dids}
            # An HA sensor read in bulk feeds the local 24 h history too,
            # exactly as menu_readings does -- same key, same engine.
            eng = self.automations
            if eng is not None:
                for did, st in out.items():
                    ent = str((self.dm.devices.get(did) or {}).get("ha_entity_id") or "")
                    lvl = st.get("level") if isinstance(st, dict) else None
                    if ent.startswith("sensor.") and isinstance(lvl, (int, float)) \
                            and not isinstance(lvl, bool):
                        try:
                            eng.record(did, "level", float(lvl))
                        except Exception:          # noqa: BLE001
                            pass
            return out
        return await asyncio.to_thread(_read)

    #: Units for the fields a board publishes under its own names. The
    #: catalogue's UNIT_OF covers the primary readings; Tasmota's ENERGY
    #: block adds these. A field with no entry shows no unit rather than
    #: a wrong one -- "230 W" for a voltage was the bug this replaces.
    _FIELD_UNITS = {
        "energy_total": "kWh", "today": "kWh", "yesterday": "kWh",
        "period": "Wh", "apparentpower": "VA", "reactivepower": "VAr",
        "factor": "", "frequency": "Hz",
        # The catalogue's own table, repeated here only so a tile still
        # carries its unit if that import ever fails; UNIT_OF wins when
        # the two disagree.
        "power": "W", "voltage": "V", "current": "A", "energy": "kWh",
        "temperature": "\u00b0C", "humidity": "%", "pressure": "hPa",
        "illuminance": "lx", "distance": "cm", "co2": "ppm",
    }

    #: Which of a many-field sensor's numbers earn a HOME tile, after the
    #: primary reading, in this order. The home screen draws a handful of
    #: tiles; a meter that speaks eleven fields (Tasmota's ENERGY block)
    #: used to fill them with yesterday/today/period and push voltage and
    #: current -- the two she looks at -- off the end. Statistics shows
    #: every field; home shows what a glance is for.
    _HOME_FIELDS = ("voltage", "current", "energy_total", "humidity",
                    "temperature", "pressure", "co2")

    def _unit_for(self, d: Dict[str, Any], key: str) -> str:
        try:
            from esp_firmware.esphome_catalog import UNIT_OF
        except Exception:                              # noqa: BLE001
            UNIT_OF = {}
        if key in UNIT_OF:
            return UNIT_OF[key]
        if key in self._FIELD_UNITS:
            return self._FIELD_UNITS[key]
        st = d.get("state") if isinstance(d.get("state"), dict) else {}
        return str(st.get("unit") or "")

    def _board_reading_rows(self, did: str, d: Dict[str, Any],
                            home: bool = False) -> List[Dict[str, Any]]:
        """The rows a board device contributes to Statistics (all of its
        numbers) or to the home tiles (`home=True`: the primary reading
        and _HOME_FIELDS only).

        THE PRIMARY READING IS ONE ROW, NAMED FOR THE DEVICE. The record
        stores it under "level" (Produces.as_meta) while the board publishes
        it under its own name ("power"), so the two used to be two rows: a
        "Power meter" tile stuck on a dash forever because nothing ever
        publishes "level", and a "Power meter — power" tile with the number
        (field report 2026-09-03). `reading_key` on the record joins them.
        """
        # HER ANSWER AT REGISTRATION, the same flag the panel reads. None
        # means never asked and is read as yes.
        if d.get("show_on_home") is False:
            return []
        state = self._cached_state(did, d)
        name = d.get("name") or did
        is_board = d.get("transport") == "mqtt"
        # AN EMPTY STATE IS STILL A CARD FOR ANYTHING THAT MEASURES -- a
        # board sensor (see the primary-row note below) AND an HA one
        # (2026-09-14). It used to be board-only, and the difference showed
        # the moment a Home Assistant sensor went quiet: a battery dies, HA
        # reports `unavailable`, the state translates to nothing, and the
        # hallway-temperature tile simply VANISHED from the home screen --
        # no dash, no "not reporting", no card at all.
        #
        # That is the same symptom, on the other kind of device, that the
        # primary-row comment below already describes and was fixed for on
        # 2026-09-03: "produced no row and therefore NO CARD -- not even a
        # dash -- which is what 'it wasn't showing any cards' was". A card
        # going missing is worse than a card saying nothing, because a
        # missing card reads as "I never had that sensor".
        ha_sensor = bool(d.get("ha_entity_id")) and self.dm.is_readable_only(d)
        if not state and not (is_board and d.get("reading_key")) \
                and not ha_sensor:
            return []
        # DeviceManager.is_readable_only's definition, applied to the state
        # just read rather than to the stored copy: a thing you can act on
        # has an "on" key, and a dimmer's brightness is a setting, not a
        # measurement. Statistics is for what the house OBSERVES.
        if "on" in state:
            return []
        # WHICH OF THESE THE BOARD ACTUALLY SAID, as opposed to what is
        # merely sitting in the record. Registration writes the envelope's
        # opening value before anything has been heard, so a meter nobody
        # had heard from showed a confident "power 0". A reading nobody has
        # sent is an ABSENCE, and the row says "not reporting" until the
        # board speaks. Her ruling, 2026-09-02, over a plain 0. Only for our
        # own boards: an HA device's numbers came from HA.
        heard: Set[str] = set(self._read_board_state(did)) if is_board else set()
        primary = str(d.get("reading_key") or "") if is_board else ""

        # IS THIS NUMBER STILL TRUE? (2026-09-12)
        #
        # This read `(not is_board) or key in heard` -- a board is trusted
        # only for the keys it actually published, and everything else was
        # trusted unconditionally. For a typed-in device that is right:
        # its stored state is the only truth there is, and there is no
        # source for it to have fallen behind. For a HOME ASSISTANT device
        # it was a claim the hub could not support. HA pushes nothing here,
        # so the stored number is only as fresh as the last time something
        # asked -- and a sensor that stopped reporting three hours ago drew
        # exactly like one answering now, while a board gone quiet for five
        # minutes correctly drew "--".
        #
        # dm.is_fresh answers it from when the entity last actually came
        # back in a get_states, and says True for everything that is not
        # HA-backed, so nothing else moves.
        ha_fresh = self.dm.is_fresh(did, d)

        def row(key: str, value: Any, label: str) -> Dict[str, Any]:
            reporting = (key in heard) if is_board else ha_fresh
            unit = self._unit_for(d, key)
            return {
                "id": f"{did}:{key}",
                "name": label,
                "room": d.get("room", ""),
                "entity": "",
                "reporting": reporting,
                "value": (f"{value:g}" if isinstance(value, float)
                          else str(value)) if reporting else "\u2014",
                "unit": unit if reporting else "",
                # WHICH KIND OF TILE. Additive, so the statistics screen --
                # which shares this builder -- simply ignores it.
                "kind": "reading",
                # The last 24 h from the engine's own local history -- a
                # dict lookup, so it is cheap enough for the home timer.
                **self._day_stats(did, key),
            }

        numeric = {k: v for k, v in state.items()
                   if k not in ("unit", "colour", "colour_modes")
                   and isinstance(v, (int, float)) and not isinstance(v, bool)}
        rows: List[Dict[str, Any]] = []
        # 1. The device's own number, named for the device. On a board that
        #    is the published field named by reading_key; the stored "level"
        #    is its placeholder and is folded into it rather than drawn
        #    beside it. Without a reading_key (an HA device, or an old
        #    record with a single "level"), "level" is the primary.
        if primary:
            # THE CARD EXISTS WHETHER OR NOT A NUMBER DOES. A board meter
            # whose stored state had been overwritten by an HA read of
            # "unavailable" ({"text": ...}, no number at all) produced no
            # row and therefore NO CARD -- not even a dash -- which is what
            # "it wasn't showing any cards" was (field report 2026-09-03).
            # A board sensor is a thing you can look at; when nothing has
            # been heard the card says so with a dash, and it says it on
            # the home screen, where the absence is the information.
            if primary in numeric or "level" in numeric:
                value = numeric.get(primary, numeric.get("level"))
            else:
                value = 0.0            # placeholder; reporting=False draws "—"
            rows.append(row(primary, value, name))
            numeric.pop(primary, None)
            numeric.pop("level", None)
        elif "level" in numeric:
            rows.append(row("level", numeric.pop("level"), name))
        elif ha_sensor:
            # AND THE SAME FOR A HOME ASSISTANT SENSOR (2026-09-14). The
            # branch above is reachable only when `primary` is set, and
            # `primary` is reading_key -- which is BOARD-ONLY (see its
            # assignment). So an HA sensor could only ever get a row
            # through the `level` branch, and an entity that had gone
            # `unavailable` has no level, no number, and therefore no card
            # at all. Same dash, same reasoning, the other kind of device.
            rows.append(row("level", 0.0, name))
        # 2. Everything else it publishes: on home only the fields a glance
        #    wants, in a fixed order; in Statistics all of them.
        if home:
            extras = [k for k in self._HOME_FIELDS if k in numeric]
        else:
            preferred = [k for k in self._HOME_FIELDS if k in numeric]
            extras = preferred + sorted(k for k in numeric if k not in preferred)
        for key in extras:
            rows.append(row(key, numeric[key], f"{name} \u2014 {key}"))
        return rows

    def _day_stats(self, did: str, key: str) -> Dict[str, Any]:
        """min/max/avg over the last 24 h from the engine's LOCAL history
        (eng.day_stats, readings_history.json) -- all None when there is no
        engine or no history yet, which the screen draws as "no statistics
        yet". Home Assistant's recorder is no longer asked."""
        out: Dict[str, Any] = {"min": None, "max": None, "avg": None}
        eng = self.automations
        if eng is None:
            return out
        try:
            stats = eng.day_stats(did, key)
        except Exception:                          # noqa: BLE001
            stats = None
        if stats:
            out["min"], out["max"], out["avg"] = (
                stats.get("min"), stats.get("max"), stats.get("mean"))
        return out

    def _board_device_rows(self, did: str,
                           d: Dict[str, Any]) -> List[Dict[str, Any]]:
        """The row a SWITCHABLE device contributes to the HOME SCREEN.

        Her ruling, 2026-09-02: "offer to any devices and let the readings
        send and let it be a card like display." The toggle was drawn on
        every device and could only ever do something for a device with
        readings -- a switch you can flip that changes nothing, which is
        exactly what "the picker is the only guard" forbids. Rather than
        take the toggle away from switches, the home screen learns to draw
        them.

        HOME ONLY, never Statistics: that screen is for what the house
        OBSERVES, and "the bulb is on" is a setting, not a measurement.
        This is why it is a second method rather than a branch in the
        first -- menu_readings calls that one too.

        OPT-IN, and this is the one place the two kinds differ. A reading
        exists in order to be read, so absent means yes. A switch's home is
        the Devices screen; a house with ten lamps would otherwise fill the
        home screen with them the moment this shipped. So a switch appears
        only when somebody actually asked for it.
        """
        if d.get("show_on_home") is not True:
            return []
        state = self._cached_state(did, d)
        if "on" not in state:
            return []                      # not a thing you can switch
        return [{
            "id": f"{did}:on",
            "name": d.get("name") or did,
            "room": d.get("room", ""),
            "entity": "",
            # The same question as the reading tiles ask, and for the same
            # reason: an HA contact that stopped answering must not draw as
            # a door that is definitely shut. True for a board or a
            # typed-in device, which have no source to have fallen behind.
            "reporting": self.dm.is_fresh(did, d),
            "value": "on" if state.get("on") else "off",
            "unit": "",
            "kind": "device",
            "min": None, "max": None, "avg": None,
        }]

    def live_reading_rows(self) -> List[Dict[str, Any]]:
        """Every reading worth a tile on home, cheaply.

        THE CHEAP HALF OF menu_readings. That one asks Home Assistant for
        each sensor's live state -- a REST round trip per device -- which
        is right for a screen you opened on purpose and quite wrong for
        something refreshing behind the home screen. This reads the stored
        state, the board's own MQTT cache (_cached_state) and the engine's
        local 24 h history (_day_stats) and nothing else -- never HA REST
        -- so it costs a dictionary walk and can run on a timer.
        """
        rows: List[Dict[str, Any]] = []
        for did in sorted(self.dm.devices):
            record = self.dm.devices[did]
            # NEVER A SECURE DEVICE. These rows are broadcast on a timer to
            # every connected phone with no gate, while list_devices and
            # readings blank a lock's state unless the caller verified
            # recently -- so a lock flagged "show on home" leaked its
            # locked/unlocked state to every handset (app audit, 2026-09-02).
            if record.get("secure"):
                continue
            rows.extend(self._board_reading_rows(did, record, home=True))
            # AND THE SWITCHES SOMEBODY ASKED FOR. Only here -- the
            # statistics screen calls _board_reading_rows directly and
            # never sees these.
            rows.extend(self._board_device_rows(did, record))
        return rows

    async def menu_readings(self) -> bool:
        """Every reading the house can take, in one place.

        The home screen used to guess at a few power metrics phone-side;
        this asks the actual sensors. Anything registered whose entity is
        a sensor -- temperature, humidity, power from a PZEM, distance
        from an ultrasonic, whatever an integration reports -- shows its
        LIVE value with its unit, grouped by room. Values are Home
        Assistant's current state, which is Tasmota's latest telemetry;
        the 24 h min/max/avg is the hub's OWN history (eng.day_stats),
        fed here for HA sensors and by MQTT for boards.
        """
        await self._io.write("\n--- READINGS ---")
        # TWO ROADS, NOT ONE. This screen was Home Assistant's alone -- it
        # returned early with a warning when HA was absent -- so a hub full
        # of boards reporting temperature every ten seconds showed an empty
        # Statistics screen and said nothing was measuring. The boards were
        # measuring; nothing asked them. History is LOCAL for both roads
        # now (readings_history.json, via the engine): a board's values
        # arrive over MQTT, an HA sensor's each time this screen reads it.
        # No history yet draws as "no statistics yet".
        by_id: Dict[str, Any] = {}
        if self.ha_client is not None:
            try:
                states = await asyncio.to_thread(
                    self.ha_client.get_states) or []
                by_id = {e.get("entity_id"): e
                         for e in states if isinstance(e, dict)}
            except Exception:                          # noqa: BLE001
                await self._io.write(
                    "  Home Assistant did not answer -- showing what the "
                    "boards report.", level="warn")

        # ONE PASS, TWO RENDERINGS. The 24-hour summary used to be fetched
        # inside the printing loop, which made the text table the only thing
        # that could ever have the statistics -- a screen would have had to
        # ask Home Assistant all over again. Gather the facts first, then let
        # each client draw them: the phone gets `readings` cards, the
        # terminal gets the same numbers as lines.
        rows: List[Dict[str, Any]] = []
        # Same law as menu_list_devices: a secure device's reading is not
        # a listing, so it stays out unless this connection passed the
        # gate recently.
        show_secure = self._verified_recently()
        for did in sorted(self.dm.devices):
            d = self.dm.devices[did]
            if d.get("secure") and not show_secure:
                continue
            entity = str(d.get("ha_entity_id") or "")
            if not entity.startswith(("sensor.", "binary_sensor.")):
                # THE BOARD'S OWN ROAD. No HA entity, so ask the device what
                # it last published. One row PER FIELD, because one board
                # sensor really is several statistics -- a DHT reports
                # temperature AND humidity, and HA only looked like it had
                # one number per device because it split them into two
                # entities first. `on` is skipped: a relay is not a
                # statistic.
                rows.extend(self._board_reading_rows(did, d))
                continue
            live = by_id.get(entity)
            row: Dict[str, Any] = {
                "id": did,
                "name": d.get("name") or did,
                "room": d.get("room", ""),
                "entity": entity,
                # A sensor Home Assistant has never heard from is REPORTED AS
                # SUCH, not as a zero. Inventing a number for a dead sensor is
                # how a screen tells a comfortable lie about the house.
                "reporting": live is not None,
                "value": None, "unit": "",
                "min": None, "max": None, "avg": None,
            }
            if live is not None:
                attrs = live.get("attributes")
                row["unit"] = (str(attrs.get("unit_of_measurement") or "")
                               if isinstance(attrs, dict) else "")
                row["value"] = str(live.get("state", "?"))
                # THE HUB KEEPS ITS OWN HISTORY. Home Assistant's recorder
                # is not asked any more; each time this screen reads a
                # numeric HA sensor the value goes into the engine's local
                # 24 h history under "level" (its single number), and the
                # summary below comes from the same place.
                if self.automations is not None:
                    try:
                        self.automations.record(
                            did, "level", float(live.get("state")))
                    except Exception:  # noqa: BLE001 -- "unavailable", "unknown"
                        pass
            row.update(self._day_stats(did, "level"))
            rows.append(row)

        # Sent even when EMPTY, and this is deliberate: the Statistics screen
        # opens on the answer either way, and an empty list is the answer
        # "nothing here measures anything yet". Suppressing it would leave
        # the screen showing the PREVIOUS visit's sensors.
        await self._io.send_data("readings", {"sensors": rows})

        if not rows:
            await self._io.write(
                "  Nothing here measures anything yet. Add a sensor -- "
                "temperature, power, distance -- and its readings appear "
                "here on their own.")
            return True
        current_room = None
        for row in sorted(rows, key=lambda r: (r["room"], r["name"])):
            if row["room"] != current_room:
                current_room = row["room"]
                await self._io.write(f"  {current_room or 'no room'}:")
            suffix = f" {row['unit']}" if row["unit"] else ""
            value = row["value"] if row["reporting"] else "not reporting"
            line = f"    {row['name']}: {value}{suffix}"
            if row["avg"] is not None:
                line += (f"   (24h: {row['min']:g}–{row['max']:g}"
                         f"{suffix}, avg {row['avg']:.1f})")
            await self._io.write(line)
        return True

    async def menu_list_devices(self):
        await self._io.write("\n--- DEVICES ---")
        # NO EARLY RETURN for an empty registry: the frame below also carries
        # the BOARDS, and a hub whose boards have no device records yet sent
        # none -- so the Devices screen lost its only door to them.
        # Structured copy for clients with a real screen. The text table
        # below remains the terminal's rendering of the same facts. get_state
        # is read once per device and reused for both the raw dict and the
        # spoken-style reading.
        # THE WIRING, read once per board and looked up per device.
        #
        # A device id on an ESP board is "<slug>.<something>", so the slug is
        # the front of it -- that is the whole test for "is this on a board of
        # mine, or reached through Home Assistant", and it is the ESP tag.
        recs = {slug: self.esp._read_board(slug) or {}
                for slug in self.esp.list_nodes()}
        wiring: Dict[str, List[Dict[str, Any]]] = {
            slug: self.esp.board_wiring(rec) for slug, rec in recs.items() if rec}

        # EVERY live read in one worker thread: get_state is an HA REST
        # call per attached entity, and a loop of them on the event loop
        # stalled every other connection for the length of the list.
        states = await self._states_of(list(self.dm.devices))
        # A LOCK'S STATE IS NOT A LISTING. Whether the front door is locked
        # goes only to someone who has passed the gate on this connection
        # recently; by voice the same question costs a voice check.
        show_secure = self._verified_recently()
        # The main meter, and what each device draws on it.
        eng = self.automations
        try:
            main_meter = eng.main_meter() if eng is not None else None
        except Exception:                          # noqa: BLE001
            main_meter = None
        # Card readers on the boards (the page's "Check the card reader").
        try:
            readers = {rid for rid, _n in self._card_readers()
                       if rid.split(".", 1)[0] in recs}
        except Exception:                          # noqa: BLE001
            readers = set()
        # Infrared receivers on the boards (the page's "Learn a remote").
        ir_receivers = {self.dm.board_device_id(slug, str(i.get("name") or ""))
                        for slug, rec in recs.items() if rec
                        for i in (rec.get("items") or [])
                        if isinstance(i, dict) and i.get("type_id") == "ir_recv"}
        records = []
        for did, d in self.dm.devices.items():
            state = states.get(did, {})
            if d.get("secure") and not show_secure:
                state = {}
            slug = did.split(".", 1)[0]
            rows = wiring.get(slug) or []
            # Matched by NAME, because that is the only thing the board record
            # and the device registry share -- the board knows "light bulb_3",
            # the registry knows "living.light_bulb_3". A device whose name was
            # changed after the board was saved simply shows no pin, which is
            # honest; it does not show somebody else's.
            mine = [r for r in rows
                    if r["name"] == (d.get("name") or "")]
            record = {
                "id": did,
                "name": d.get("name") or did,
                "room": d.get("room", ""),
                "description": d.get("description", ""),
                "secure": bool(d.get("secure")),
                # WHETHER ITS READINGS ARE ON THE HOME SCREEN, so the card
                # can show the toggle's real state rather than guessing.
                # None means never asked and reads as yes -- an older
                # record, or a path that never offered the question.
                # A READING is on home unless it was hidden (absent means
                # yes); a SWITCHABLE is on home only when somebody asked
                # (see _board_device_rows) -- the card's toggle must show
                # the same truth the home screen draws.
                "on_home": (d.get("show_on_home") is True
                            if isinstance(state, dict) and "on" in state
                            else d.get("show_on_home") is not False),
                "voice": "full" if d.get("embedding") else "name",
                "state": state,
                "reading": describe_reading(state, d.get("reading_key")),
                "envelope": describe_envelope(d),
                "ha_entity_id": d.get("ha_entity_id"),
            }
            # WHAT ITS PAGE CAN DO WITH IT (the app's "Use it" card).
            try:
                record["use"] = self._use_of(did, d, state)
            except Exception:                      # noqa: BLE001
                record["use"] = {"kind": "none"}
            if did in readers:
                record["reader"] = True
            if did in ir_receivers:
                record["ir_receiver"] = True
            if eng is not None:
                try:
                    # {} for "none": not asked again per device.
                    meter = eng.meter_info(did, main_meter or {})
                    if meter:
                        record["meter"] = meter
                    # Like its state: a secure one's only after a check.
                    draw = eng.draw_of(did)
                    if draw and (show_secure or not d.get("secure")):
                        record["draw"] = draw
                except Exception:                  # noqa: BLE001
                    traceback.print_exc()
            if slug in wiring:
                record["esp"] = True
                record["board"] = slug
                record["board_name"] = recs[slug].get("name") or slug
                if mine:
                    # "Power2 · GPIO27" -- and both halves, separately, for a
                    # screen that wants to lay them out itself.
                    record["control"] = mine[0]["control"]
                    record["pin"] = mine[0]["pin"]
                    if len(mine) > 1:
                        record["pins"] = [r["pin"] for r in mine]
            records.append(record)
        # THE BOARDS RIDE ALONG, because the merged Devices screen has to
        # show them: "Update device -> A board" is how you reach a board's
        # wiring, name and password, and folding four drawer rows into one
        # screen would otherwise quietly lose that door. Read-only here --
        # the screen lists what you have and routes editing into the
        # existing board flows, which are untouched.
        boards = []
        for slug, rec in recs.items():
            owned = sorted(did for did in self.dm.devices
                           if did.split(".", 1)[0] == slug)
            boards.append({
                "slug": slug,
                "name": rec.get("name") or slug,
                "topic": rec.get("topic", ""),
                "board": rec.get("board", ""),
                "devices": owned,
                # The board carries the table too (her ask): every pin, what
                # is on it, and which Power number drives it.
                "wiring": wiring.get(slug) or [],
                # THE PARTS AND THEIR DRIVERS (2026-09-11), for the board's
                # own sheet: each part by its catalogue name, the Tasmota
                # component on each of its pins, and the driver names the
                # firmware must carry for it. Read off the record and the
                # catalogue, so it is true whether or not the board is
                # online; "Ask it how it is" is the live counterpart.
                "parts": self._board_parts(rec),
                "flashed": bool(rec.get("flashed")),
                # What the board told us about itself, when it has.
                "psram": rec.get("psram"),
                # A board with a text display: where its "Write on the
                # display" card sends, and how big the glass is.
                **self._board_display(slug),
            })
        # LAYOUTS RIDE ALONG: every board's wiring, every draft and every
        # saved plan is one list, so Devices -> Layouts is always as fresh
        # as the devices beside it.
        try:
            layouts = self._layout_records()
        except Exception as exc:                   # noqa: BLE001
            event("SHELL", f"layouts not listed: {exc}")
            layouts = []
        await self._io.send_data("device_list",
                                 {"devices": records, "boards": boards,
                                  "display_readings": self._display_readings(),
                                  "layouts": layouts,
                                  "main_meter": main_meter})
        if not self.dm.devices:
            await self._io.write("No devices.")
            return

        # THE TABLE IS FOR THE TERMINAL, and only the terminal -- the same
        # rule menu_profile has always followed, applied here at last
        # (2026-09-11: "i will be in adding devices, it's still dictating
        # devices list").
        #
        # A phone already has every one of these facts in the frame above,
        # and draws them as cards. Sending the fixed-width table as well
        # meant twenty-odd `output` frames of console formatting to a
        # client that renders none of it -- and the app READ THEM ALOUD,
        # one awaited utterance per row, because a menu flow leaves its
        # pipeline idle and idle is when output gets spoken. So opening
        # Devices started a recital you could not get out of, and it was
        # still going when you had moved on to adding a device. The app
        # stops speaking menu narration in the same round; this stops
        # sending it something it never needed.
        #
        # `no_embed` is counted either way: the warning that follows is
        # about the HOUSE, not about the table, and a phone deserves it as
        # much as the terminal does.
        no_embed = sum(1 for d in self.dm.devices.values()
                       if not d.get("embedding"))
        if not self._io.renders_data:
            await self._io.write(f"{'#':<3} {'NAME':<14} {'ROOM':<14} {'VOICE':<6} DESCRIPTION")
            await self._io.write("-" * 78)
            for i, (did, d) in enumerate(self.dm.devices.items(), 1):
                name = (d.get("name") or did)[:13]
                room = (d.get("room", ""))[:13]
                # "VOICE" = can this be reached by paraphrase (semantic), or
                # only by its literal name? An embedding present means yes.
                voice = "full" if d.get("embedding") else "name"
                desc = (d.get("description", "") or "(none)")[:34]
                await self._io.write(f"{i:<3} {name:<14} {room:<14} {voice:<6} {desc}")
        if no_embed:
            await self._io.write(f"\n  {no_embed} device(s) show 'name' — reachable only by their exact")
            await self._io.write("  name, not by paraphrase. Re-run 'Update description' on them")
            await self._io.write("  (with the model loaded) to enable full semantic matching.")

    # ── WHO MADE WHAT (2026-10-11) ──────────────────────────────────
    #
    # Every scene and automation records who made it and who last changed
    # it. Your own you change as before. SOMEBODY ELSE'S is theirs: editing,
    # pausing or removing it needs THEIR PIN -- every time, never a voice,
    # never the recent-check window, never the PIN of whoever is holding
    # the phone (her rule, 2026-10-11: "only with the person's pin") -- and
    # then they get a letter saying what was done. Things made before this
    # was recorded have no maker: they are the house's, and anyone's own
    # check changes them, as before.

    def _household(self) -> List[str]:
        """Everyone who lives here: a voice or a PIN on file."""
        names: Set[str] = set()
        try:
            names |= set(self.biometric_store.list_users() or [])
        except Exception:                          # noqa: BLE001
            pass
        try:
            names |= set(self.fallback.pin.pin_db)
        except Exception:                          # noqa: BLE001
            pass
        return sorted(n for n in names if n)

    def _maker_shown(self, owner: str) -> str:
        """`owner`, for a list -- only once the house has two people: on
        your own, "by you" on every row says nothing. The person asking
        reads "you" (the details panel says "Made by you")."""
        if not owner or len(self._household()) < 2:
            return ""
        return "you" if owner == self.current_speaker_id else owner

    def _maker_chip(self, owner: str, private: bool = False) -> str:
        """The tag on a scene or automation card (her design, 2026-10-11):
        "Just mine", "by you", "by chidi", or "Everyone" for one with no
        maker. Nothing while one person lives here."""
        if len(self._household()) < 2:
            return ""
        if private:
            return "Just mine"
        if not owner:
            return "Everyone"
        return "by you" if owner == self.current_speaker_id else f"by {owner}"

    def _subject_owner(self, head: str, argument: str) -> str:
        """Who made the thing a by-name action is about ("" for nobody)."""
        try:
            if head in ("pause", "drop"):
                eng = getattr(self, "automations", None)
                rule = eng.get(argument) if eng is not None else None
                return str((rule or {}).get("created_by") or "")
            if head in ("editscene", "dropscene") and self.sm is not None:
                scene = self.sm.get(argument)
                return scene.created_by if scene is not None else ""
        except Exception:                          # noqa: BLE001
            pass
        return ""

    async def _may_change(self, kind: str, name: str, owner: str) -> bool:
        """True when the person speaking may change this. Somebody else's
        thing needs ITS MAKER'S PIN, now -- the maker is the one who says
        yes, whoever holds the phone."""
        me = self.current_speaker_id or ""
        if not owner or owner == me:
            return True
        if owner not in self._household():
            return True             # its maker has left: it is the house's
        if not me:
            self.failure_note = (f"This {kind} is {owner}'s, and this hub "
                                 f"does not know who is asking.")
            return False
        # No line before it (her design, 2026-10-11): the PIN question
        # names whose PIN and what for, and says enough.
        ok, why = await self._pin_tries(
            owner, f"{owner}'s PIN, to change their {kind} {name!r}: ")
        if ok is None:
            raise CancelledByUser()
        if not ok:
            if not self.failure_note:
                self.failure_note = (why or f"{owner}'s PIN was not accepted "
                                     f"-- their {kind} was not changed.")
            return False
        return True

    def _tell_owner(self, owner: str, kind: str, name: str,
                    what: str) -> None:
        """A letter to the maker: '<me> <what> your <kind> <name>'."""
        me = self.current_speaker_id or ""
        if not owner or owner == me or owner not in self._household():
            return
        when = datetime.now().strftime("%d %b, %H:%M").lstrip("0")
        try:
            self.people.send_letter(
                owner, f"{kind}:{name.lower()}",
                f"{me or 'Someone'} {what} your {kind} {name!r}",
                f"{when}, on {me or 'another'}'s phone, with your PIN.")
            event("PEOPLE", f"letter to {owner!r}: {me} {what} {kind} {name!r}")
        except Exception as exc:                   # noqa: BLE001
            event("PEOPLE", f"letter to {owner!r} not kept ({exc})")

    async def menu_seen_letter(self, letter_id: str) -> bool:
        """"Got it" on a letter: it is read, and leaves the list."""
        me = self.current_speaker_id or ""
        if me:
            await asyncio.to_thread(self.people.drop_letter, me, letter_id)
        await self._io.send_data("notice_gone", {"id": f"letter:{letter_id}"})
        return True

    def _letter_notices(self) -> List[Dict[str, Any]]:
        me = self.current_speaker_id or ""
        if not me:
            return []
        return [self._notice(f"letter:{x['id']}", x.get("title", ""),
                             x.get("body", ""), "menu", f"seen:{x['id']}",
                             "Got it")
                for x in self.people.letters_for(me) if x.get("id")]

    async def _ask_for_whom(self, current: Optional[bool] = None) -> bool:
        """'Just me' or 'everyone' -- asked only once two people live here.
        `current`: an existing scene's answer, offered first as Keep."""
        if len(self._household()) < 2:
            return bool(current)
        options = [
            {"value": "all", "label": "Everyone",
             "sub": "anyone in the house can say it"},
            {"value": "me", "label": "Just me",
             "sub": "only your voice runs it, and only you see it"}]
        if current is not None:
            options.sort(key=lambda o: (o["value"] == "me") != current)
            options[0] = dict(options[0], label=f"Keep: {options[0]['label']}")
        return await self._choose_or_cancel("Who is this scene for?",
                                            options) == "me"

    # ── THE SCENE SCREEN (2026-11-08) ───────────────────────────────────
    #
    # What one device does in a scene (SceneAction.state): on, level,
    # colour [r,g,b], press (an IR button, or "" for a button's press), or
    # show (a display's pages, as the display writer makes them).

    def _scene_targets(self) -> List[Dict[str, Any]]:
        """Every device a scene can move, with the controls that fit it --
        the same as its page in Devices. Sensors are left out."""
        out: List[Dict[str, Any]] = []
        for did, d in (self.dm.devices or {}).items():
            use = self._use_safe(did, d)
            if use.get("kind") in ("reading", "none"):
                continue
            out.append({"value": did, "label": d.get("name") or did,
                        "room": d.get("room", ""),
                        "secure": bool(d.get("secure")), "use": use})
        return sorted(out, key=lambda t: (str(t["label"]).lower(), t["value"]))

    def _scene_state(self, did: str, raw: Any) -> Dict[str, Any]:
        """One device's part as the phone sent it, kept to what a scene can
        do ({} when nothing usable was chosen)."""
        if not isinstance(raw, dict):
            return {}

        def num(v: Any) -> Optional[float]:
            if isinstance(v, bool) or not isinstance(v, (int, float)):
                return None
            return float(v) if math.isfinite(v) else None

        st: Dict[str, Any] = {}
        if isinstance(raw.get("press"), str):
            return {"press": raw["press"].strip()[:64]}
        show = raw.get("show")
        if isinstance(show, dict):
            keep: Dict[str, Any] = {}
            pages = show.get("pages")
            if isinstance(pages, list):
                keep["pages"] = [
                    [{"text": str(l.get("text") or "")[:120],
                      "align": str(l.get("align") or "left")[:8]}
                     for l in page[:8] if isinstance(l, dict)]
                    for page in pages[:8] if isinstance(page, list)]
            if isinstance(show.get("text"), str):
                keep["text"] = show["text"][:200]
            for k in ("for_s", "page_s"):
                v = num(show.get(k))
                if v is not None and v >= 0:
                    keep[k] = v
            if show.get("clock_between") is True:
                keep["clock_between"] = True
            words = any(l.get("text", "").strip() for page in
                        keep.get("pages", []) for l in page) or \
                str(keep.get("text") or "").strip()
            return {"show": keep} if words else {}
        if isinstance(raw.get("on"), bool):
            st["on"] = raw["on"]
        level = num(raw.get("level"))
        if level is not None:
            st["level"] = level
        colour = raw.get("colour")
        if isinstance(colour, list) and len(colour) == 3 and \
                all(num(c) is not None for c in colour):
            st["colour"] = [max(0, min(255, int(c))) for c in colour]
        # An older scene's own value key (asked by the old questions).
        key = str(((self.dm.devices or {}).get(did) or {}).get("state_key")
                  or "")
        if key and key not in ("on", "level") and num(raw.get(key)) is not None:
            st[key] = num(raw.get(key))
        return st

    def _scene_words(self, did: str, state: Any) -> str:
        """One part of a scene in words: "on, 30", "press power",
        'show "Dinner"'. Never a raw state."""
        st = state if isinstance(state, dict) else {}
        if "press" in st:
            button = str(st.get("press") or "").replace("_", " ").strip()
            return f"press {button}" if button else "press it"
        show = st.get("show")
        if isinstance(show, dict):
            lines: List[str] = []
            for page in (show.get("pages") or [])[:1]:
                if isinstance(page, list):
                    lines += [str(l.get("text") or "").strip()
                              for l in page if isinstance(l, dict)]
            text = " / ".join(x for x in lines if x) or \
                str(show.get("text") or "").strip()
            return f'show "{text[:40]}"' if text else "show a message"
        d = (self.dm.devices or {}).get(did) or {}
        rest = {k: v for k, v in st.items() if k != "colour"}
        words = ""
        if rest:
            try:
                words = describe_reading(rest, None, d.get("state_words"))
            except TypeError:                      # the two-argument copy
                words = describe_reading(rest)
        if st.get("colour") and st.get("on") is not False:
            words = f"{words}, a colour" if words else "a colour"
        return words or "nothing chosen"

    async def _press_device(self, did: str, button: str) -> bool:
        """One press: an IR appliance's learned button, a board's own press
        command, or a Home Assistant button."""
        d = (self.dm.devices or {}).get(did) or {}
        if isinstance(d.get("ir_codes"), dict) and d.get("ir_codes"):
            return await self._press_ir(did, button, None)
        if d.get("transport") == "mqtt" and not d.get("ha_entity_id") \
                and hasattr(self.dm, "device_actions"):
            acts = await asyncio.to_thread(self.dm.device_actions, did)
            return bool(acts) and bool(await asyncio.to_thread(
                self.dm.do_device_action, did, acts[0]))
        return bool(await asyncio.to_thread(self.dm.press, did))

    async def _scene_act(self, did: str, state: Any) -> Tuple[bool, str]:
        """Carry out one part of a scene: (done, what happened in words).
        Used when the scene runs and by Try it now."""
        d = (self.dm.devices or {}).get(did)
        if d is None:
            return False, f"{did}: no longer registered"
        name = d.get("name") or did
        st = dict(state) if isinstance(state, dict) else {}
        ok = False
        if "press" in st:
            ok = await self._press_device(did, str(st.get("press") or ""))
        elif isinstance(st.get("show"), dict):
            eng = getattr(self, "automations", None)
            if eng is None:
                return False, f"{name}: displays need the automation engine"
            show = st["show"]
            panel = ((self._use_safe(did, d).get("display") or {})
                     .get("device") or did)
            problem = eng.show_now(
                panel, str(show.get("text") or ""),
                float(show.get("for_s") or 0), pages=show.get("pages"),
                page_s=(None if show.get("page_s") is None
                        else float(show["page_s"])),
                clock_between=bool(show.get("clock_between")))
            if problem:
                return False, f"{name}: {problem}"
            ok = True
        else:
            colour = st.pop("colour", None)
            ok = True
            if st:
                ok = bool(await self._set_state(did, **st))
            if ok and colour and st.get("on") is not False:
                ok = bool(await asyncio.to_thread(
                    self.dm.set_colour, did, tuple(colour)))
        if not ok:
            return False, f"{name}: could not be set"
        return True, f"{name}: {self._scene_words(did, state)}"

    def _scene_draft_out(self, draft: Dict[str, Any]) -> Dict[str, Any]:
        """The scene as the screen draws it: each part with its device's
        name and room, and whether that device is still here."""
        parts = []
        for a in draft.get("actions") or []:
            did = a["device"]
            d = (self.dm.devices or {}).get(did) or {}
            parts.append({"device": did, "state": a["state"],
                          "device_name": d.get("name") or did,
                          "room": d.get("room", ""),
                          "known": did in (self.dm.devices or {}),
                          "words": self._scene_words(did, a["state"])})
        return dict(draft, actions=parts)

    def _scene_draft_in(self, raw: Any, was: Dict[str, Any]
                        ) -> Dict[str, Any]:
        """The scene as the phone sent it back, checked. A device that is no
        longer registered drops out; one listed twice counts once."""
        if not isinstance(raw, dict):
            return was
        parts: List[Dict[str, Any]] = []
        seen = set()
        for a in raw.get("actions") or []:
            if not isinstance(a, dict):
                continue
            did = str(a.get("device") or "")
            if did in seen or did not in (self.dm.devices or {}):
                continue
            seen.add(did)
            parts.append({"device": did,
                          "state": self._scene_state(did, a.get("state"))})
        return {"name": " ".join(str(raw.get("name") or "").split())[:60],
                "description": str(raw.get("description") or "").strip()[:600],
                "private": raw.get("private") is True,
                "actions": parts}

    async def _scene_ai(self, draft: Dict[str, Any]) -> Tuple[str, str]:
        """What the AI suggests the scene is for: (its words, why not).
        Asked as registration asks: the screen waits, then shows it beside
        the person's words, to tap and use."""
        if not _LLM_AVAILABLE:
            return "", "The AI is not available on this hub."
        name = draft.get("name") or ""
        if not name:
            return "", "Give the scene a name first -- the AI works from it."
        acts = [{"device_id": f"{self._dev_name(a['device'])} "
                              f"({self._scene_words(a['device'], a['state'])})",
                 "state": {}} for a in draft.get("actions") or []]
        try:
            text = await asyncio.to_thread(
                describe_scene, name, draft.get("description") or "", acts)
        except Exception as exc:                   # noqa: BLE001
            return "", f"The AI could not answer ({type(exc).__name__})."
        return str(text or "").strip(), ""

    async def _scene_try(self, draft: Dict[str, Any]) -> List[str]:
        """TRY IT NOW: each part once, nothing saved. A secure device is
        not moved from here (saying the scene asks for what it needs)."""
        before = self.failure_note
        said: List[str] = []
        for a in draft.get("actions") or []:
            did = a["device"]
            d = (self.dm.devices or {}).get(did) or {}
            name = d.get("name") or did
            if d.get("secure"):
                said.append(f"{name}: a secure device -- not tried from here; "
                            f"say the scene to run it")
                continue
            if not a["state"]:
                said.append(f"{name}: nothing chosen yet")
                continue
            try:
                _ok, line = await self._scene_act(did, a["state"])
            except Exception as exc:               # noqa: BLE001
                traceback.print_exc()
                line = f"{name}: could not be tried ({type(exc).__name__})"
            said.append(line)
        self.failure_note = before     # a try is not the flow's ending
        return said or ["Add a device first."]

    async def _scene_save(self, sm, draft: Dict[str, Any], scene,
                          whom: bool) -> Optional[str]:
        """Save it; the reason in words when it cannot be saved."""
        name = draft["name"]
        if not name:
            return "Give the scene a name -- it is also what you say."
        if not draft["actions"]:
            return "Add at least one device."
        empty = [self._dev_name(a["device"]) for a in draft["actions"]
                 if not a["state"]]
        if empty:
            return (f"Choose what {empty[0]} does, or take it out "
                    f"with the cross.")
        actions = [{"device_id": a["device"], "state": a["state"]}
                   for a in draft["actions"]]
        by = self.current_speaker_id or ""
        desc = draft["description"]
        try:
            if scene is None:
                await asyncio.to_thread(
                    sm.register, name, name, actions,
                    description=desc or None, by=by,
                    private=bool(whom and draft["private"]))
                event("SCENE", f"added {name.lower()!r} from the scene screen "
                               f"({len(actions)} part(s))")
                return None
            renamed = name.lower() != scene.name
            words: Dict[str, Any] = {}
            if renamed or desc != getattr(scene, "description", ""):
                # Re-taught: what it is for, led by its name.
                words = {"description": desc, "trigger": name}
            await asyncio.to_thread(
                sm.update, scene.name,
                new_name=name if renamed else None, actions=actions, by=by,
                private=(bool(draft["private"]) if whom else None), **words)
        except ValueError as exc:
            return f"Not saved: {exc}"
        if renamed and getattr(self, "scene_learned", None) is not None:
            # Renamed: what it learned goes with it.
            await asyncio.to_thread(self.scene_learned.rename_device,
                                    scene.name, name.lower())
        event("SCENE", f"changed {scene.name!r} from the scene screen")
        return None

    async def _scene_screen(self, scene=None) -> Optional[bool]:
        """THE SCENE SCREEN: True saved, False not, None when this client
        has no scene screen (the caller asks its questions instead).
        CancelledByUser when the person backs out."""
        sm = self._require_init()
        me = self.current_speaker_id or ""
        # "Just me" needs a known person, and two living here; on a scene
        # being changed only its maker may change it.
        whom = len(self._household()) >= 2 and bool(me) and (
            scene is None or (bool(scene.created_by)
                              and scene.created_by == me))
        draft: Dict[str, Any] = {
            # As it was typed ("Movie night"), not its lower-case key.
            "name": ("" if scene is None else scene.trigger.strip()
                     if scene.trigger.strip().lower() == scene.name
                     else scene.name),
            "description": getattr(scene, "description", "") or "",
            "private": bool(scene is not None and scene.private),
            "actions": ([{"device": a.device_id, "state": dict(a.state)}
                         for a in scene.actions] if scene is not None else []),
        }
        tried: List[str] = []
        error = ""
        # The AI's last suggestion, and the name it was asked for (the
        # screen says when the name has changed since).
        ai_text, ai_error, ai_for = "", "", ""
        while True:
            try:
                targets = await asyncio.to_thread(self._scene_targets)
                payload = {
                    "editing": scene is not None,
                    "scene": self._scene_draft_out(draft),
                    "targets": targets,
                    "whom": whom,
                    "ai_available": bool(_LLM_AVAILABLE),
                    "ai_text": ai_text,
                    "ai_error": ai_error,
                    "ai_for": ai_for,
                    "tried": tried,
                    "error": error,
                }
            except Exception as exc:               # noqa: BLE001
                traceback.print_exc()
                event("SCENE", f"the scene screen could not be prepared "
                               f"({type(exc).__name__}: {exc}) -- the "
                               f"questions instead. THIS IS A HUB BUG.")
                return None
            reply = await self._io.scene_builder(payload)
            if reply is None:
                return None                        # no scene screen here
            if reply.get("cancelled"):
                raise CancelledByUser()
            draft = self._scene_draft_in(reply.get("scene"), draft)
            tried, error = [], ""
            step = reply.get("action")
            if step == "generate":
                ai_text, ai_error = await self._scene_ai(draft)
                ai_for = draft["name"]
                continue
            if step == "try":
                tried = await self._scene_try(draft)
                continue
            error = await self._scene_save(sm, draft, scene, whom) or ""
            if not error:
                await self._io.write(
                    f"Scene {'updated' if scene is not None else 'created'}: "
                    f"{draft['name'].lower()!r}")
                return True

    async def menu_add_scene(self):
        await self._io.write("\n--- ADD SCENE ---")
        if not self.dm.devices:
            await self._io.write("No devices registered.")
            self.failure_note = ("No devices are registered yet, so a scene "
                                 "has nothing to act on.")
            return False
        # The scene screen first; clients without it get the questions.
        saved = await self._scene_screen(None)
        if saved is not None:
            return saved
        try:
            name = await self._io.get_input("Scene name: ")
            hint = (await self._io.ainput("What is this scene for? (optional): ")).strip()
            if means_cancel(hint):
                raise CancelledByUser()

            actions: List[Dict[str, Any]] = []
            while True:
                ids = list(self.dm.devices.keys())
                pick = await self._pick_one(ids, "Add device (Enter to finish)")
                if not pick:
                    break
                actions.append(await self._ask_scene_action(pick, pick))

            if not actions:
                await self._io.write("No devices added — scene not created.")
                self.failure_note = ("No devices were added, so the scene "
                                     "was not created.")
                return False

            private = await self._ask_for_whom()

            description = None
            if _LLM_AVAILABLE:
                try:
                    description = await asyncio.to_thread(
                        describe_scene, name, hint, actions)
                    await self._io.write(f"AI description: {description}")
                except Exception as e:
                    await self._io.write(f"AI description failed: {e}")

            try:
                self.sm.register(name, name, actions, description=description,
                                 by=self.current_speaker_id or "",
                                 private=private)
            except ValueError as e:
                # Chiefly "already exists". This used to escape to run()'s
                # catch-all and print a stack trace at someone who had simply
                # reused a name.
                await self._io.write(f"Scene not created: {e}")
                self.failure_note = f"The scene was not created: {e}"
                return False
            await self._io.write(f"Scene created: {name!r}")
            return True
        except CancelledByUser:
            # Passed on, like every other flow: swallowed, the None it
            # returned read as success and threw the Done window.
            await self._io.write("\n(Cancelled.)")
            raise

    async def _ask_scene_action(self, did: str, label: str) -> Dict[str, Any]:
        """One scene action for `did`, asked: on or off, then a value when
        it has one. Shared by Add scene and the scene editor."""
        dev = self.dm.devices[did]
        on_off = (await self._io.ainput(f"  {label}: on or off? ")).strip().lower()
        if means_cancel(on_off):
            raise CancelledByUser()   # not "off", and not an action
        state: Dict[str, Any] = {"on": on_off in ("on", "yes", "true", "1")}
        if dev.get("state_key") != "on" and state["on"]:
            val_str = (await self._io.ainput(
                f"  Value ({dev.get('range_min', 0)}-"
                f"{dev.get('range_max', 100)}, Enter to skip): ")).strip()
            if means_cancel(val_str):
                raise CancelledByUser()
            if val_str:
                try:
                    state[dev.get("state_key") or "level"] = float(val_str)
                except ValueError:
                    await self._io.write("    couldn't parse -- skipped")
        return {"device_id": did, "state": state}

    def _scene_records(self) -> List[Dict[str, Any]]:
        """Every scene as data, for a client with a real screen.

        WHY THIS EXISTS AT ALL. The Scenes screen used to reconstruct
        scenes by regex-parsing the printed table below -- pulling the name
        back out of "  1. movie time". That made a CONSOLE FORMAT into an
        API: reformat one print statement and the phone silently stops
        recognising its own scenes. Tolerable while the screen only
        displayed them; not tolerable now that tapping a row edits or
        deletes one, because the name scraped off a line is the argument
        that names WHAT gets deleted.

        Devices and automations already send structured records. Scenes
        were the last list that did not.

        `known` is carried per action because a scene can outlive a device:
        the editor drops such actions when it walks them, but until someone
        opens it the scene still references a device that is gone, and the
        screen should say so rather than print an id nobody recognises.
        """
        records: List[Dict[str, Any]] = []
        for s in self.sm.list_scenes(user=self.current_speaker_id):
            actions: List[Dict[str, Any]] = []
            secure = False
            for a in s.actions:
                dev = self.dm.devices.get(a.device_id) or {}
                if dev.get("secure"):
                    secure = True
                actions.append({
                    "device": a.device_id,
                    "device_name": dev.get("name") or a.device_id,
                    "room": dev.get("room", ""),
                    "known": a.device_id in self.dm.devices,
                    "state": dict(a.state),
                    "reading": self._scene_words(a.device_id, a.state),
                })
            records.append({
                "name": s.name,
                "trigger": s.trigger,
                "actions": actions,
                # Whether ANY member is security-critical. The screen marks
                # it, for the same reason the device cards do: a scene that
                # can open a door should not look like one that dims a lamp.
                "secure": secure,
                # WHO MADE IT -- empty while one person lives here, and for
                # a scene older than the record.
                "by": self._maker_shown(s.created_by),
                "chip": self._maker_chip(s.created_by, s.private),
                "at": s.created_at,
                "changed_by": self._maker_shown(s.changed_by),
                "changed_at": s.changed_at,
                "private": s.private,
                "description": getattr(s, "description", ""),
            })
        return records

    async def menu_list_scenes(self):
        scenes = self.sm.list_scenes(user=self.current_speaker_id)
        # SENT EVEN WHEN EMPTY, and before the early return: a client whose
        # last scene was just removed has to be told the list is now empty,
        # and silence would leave the removed scene on its screen.
        await self._io.send_data("scene_list",
                                 {"scenes": self._scene_records()})
        if self._io.renders_data:
            return                     # the phone draws scene_list itself
        await self._io.write("\n--- SCENES ---")
        if not scenes:
            await self._io.write("No scenes.")
            return
        for i, s in enumerate(scenes, 1):
            await self._io.write(f"\n  {i}. {s.name}")
            await self._io.write(f"     Trigger: {s.trigger!r}")
            for a in s.actions:
                await self._io.write(f"       - {a.device_id}: {a.state}")

    async def menu_update_scene(self):
        """Pick a scene, then edit it -- the VOICE and terminal door.

        The picker lives here and not in _edit_scene because a spoken
        "update a scene" has nothing to point at. A tap on the Scenes
        screen does, and goes straight to menu_edit_scene.
        """
        await self._io.write("\n--- UPDATE SCENE ---")
        sm = self._require_init()
        scenes = sm.list_scenes(user=self.current_speaker_id)
        if not scenes:
            await self._io.write("No scenes.")
            self.failure_note = ("There are no scenes yet, so there is "
                                 "nothing to update.")
            return False
        picked = await self._io.choose(
            "Update which scene?",
            [{"value": s.name, "label": s.name,
              "sub": f"{len(s.actions)} action(s)"} for s in scenes])
        # BACKING OUT IS A CANCEL, not a quiet success. The bare `return`
        # this replaces produced None, which run_menu_action reads as "ran
        # to completion" -- so abandoning the picker threw the green Done
        # window over a scene nobody touched. menu_update_device already
        # carries this law in a comment; scenes were never brought up to it.
        if not picked:
            raise CancelledByUser()
        return await self.menu_edit_scene(picked[0])

    async def menu_edit_scene(self, name: str) -> bool:
        """Edit ONE named scene, no picker.

        THE LIST IS THE PLACE (her merge law, 2026-08-27, applied to scenes
        the way it was applied to automations): the Scenes screen IS the
        feature, so editing is a tap on the thing rather than a trip
        through a question flow that exists for the voice.

        The name arrives from the network, so it is LOOKED UP and never
        trusted: a name that is not in the registry does nothing at all.
        """
        sm = self._require_init()
        scene = sm.get(name, user=self.current_speaker_id)
        if scene is None:
            self.failure_note = ("That scene is no longer here -- the list "
                                 "may be out of date.")
            return False
        if not await self._may_change("scene", scene.name, scene.created_by):
            return False
        done = await self._scene_screen(scene)
        if done is None:                 # no scene screen: the questions
            done = await self._edit_scene(sm, scene)
        if done:
            self._tell_owner(scene.created_by, "scene", scene.name, "changed")
        return done

    async def _edit_scene(self, sm, scene) -> bool:
        """Rename a scene, re-teach its voice matching, and walk its actions
        (keep / change / remove each, then add more).

        Mirrors menu_update_device's wizard shape; SceneManager.update()
        does the storage work and only re-embeds when a new description was
        actually given.

        CancelledByUser is NOT caught here. It used to be, and the handler
        wrote "(Cancelled — nothing saved.)" and returned None -- which
        run_menu_action reads as success, so a deliberate cancel ended in
        the Done window. The transport layer owns the cancelled ending
        (run_menu_action says so in its own comment); this only has to let
        it through.
        """
        # Fields 1-2: name and matching sentence, with back/finish.
        answers: List[Optional[str]] = [None, None]     # new name, hint
        done_early = False
        i = 0
        while i < 2:
            result = await self._prompt_update_field(
                *(("Scene name", answers[0] or scene.name) if i == 0 else
                  ("What is this scene for? (re-teaches voice matching)", "keep as-is")),
                index=i + 1, total=3)
            if result == "__BACK__":
                i = max(0, i - 1)    # the first field has nothing before it
                continue
            if result == "__DONE__":
                done_early = True
                break
            if result is not None:
                answers[i] = result
            i += 1
        new_name, hint = answers

        description: Optional[str] = None
        if hint:
            description = hint
            if _LLM_AVAILABLE:
                ans = (await self._io.ainput(
                    "Expand that with AI? [y/N]: ")).strip().lower()
                if means_cancel(ans):
                    raise CancelledByUser()
                if ans in ("y", "yes"):
                    await self._io.write("\nGenerating with local AI...")
                    try:
                        gen = await asyncio.to_thread(
                            describe_scene, new_name or scene.name, hint,
                            [a.to_dict() for a in scene.actions])
                        await self._io.write(f"Description: {gen}")
                        confirm = (await self._io.ainput(
                            "Use it? [Y/n]: ")).strip().lower()
                        if means_cancel(confirm):
                            raise CancelledByUser()
                        if confirm not in ("n", "no"):
                            description = gen
                    except CancelledByUser:
                        raise
                    except Exception as e:
                        await self._io.write(
                            f"AI description failed: {e} -- using your words.")

        # Field 3: the actions, one decision per existing action.
        actions = [a.to_dict() for a in scene.actions]
        if not done_early:
            edited: List[Dict[str, Any]] = []
            for act in actions:
                did = act["device_id"]
                if did not in self.dm.devices:
                    await self._io.write(
                        f"  {did} is no longer registered -- dropping it.")
                    continue
                label = self.dm.devices[did].get("name") or did
                verdict = await self._io.choose(
                    f"{label}: currently {act['state']}",
                    [{"value": "keep", "label": "Keep as is", "sub": ""},
                     {"value": "change", "label": "Change", "sub": ""},
                     {"value": "remove", "label": "Remove from scene",
                      "sub": ""}])
                if not verdict:
                    raise CancelledByUser()
                if verdict[0] == "remove":
                    continue
                if verdict[0] == "keep":
                    edited.append(act)
                    continue
                edited.append(await self._ask_scene_action(did, label))

            while True:
                used = {a["device_id"] for a in edited}
                remaining = [d for d in self.dm.devices if d not in used]
                if not remaining:
                    break
                pick = await self._pick_one(
                    remaining, "Add another device (Enter to finish)")
                if not pick:
                    break
                edited.append(await self._ask_scene_action(
                    pick, self.dm.devices[pick].get("name") or pick))

            if not edited:
                await self._io.write(
                    "A scene needs at least one action -- nothing saved.")
                # Said as a REFUSAL, not returned bare. A bare return reads
                # as "ran to completion", so emptying a scene of every
                # action used to end in the Done window while nothing had
                # been saved -- silence looking like success, the one thing
                # a result frame must never do.
                self.failure_note = ("A scene has to do something -- every "
                                     "action was taken out, so nothing was "
                                     "saved and the scene is unchanged.")
                return False
            actions = edited

        # Its maker may change who it is for; nobody else may.
        private = None
        if scene.created_by and scene.created_by == self.current_speaker_id \
                and not done_early:
            private = await self._ask_for_whom(scene.private)

        try:
            sm.update(scene.name,
                      new_name=new_name,
                      description=description,
                      actions=actions,
                      by=self.current_speaker_id or "",
                      private=private)
        except ValueError as e:
            await self._io.write(f"Scene not updated: {e}", level="warn")
            self.failure_note = f"That scene was not updated: {e}"
            return False
        await self._io.write(f"Scene updated: {(new_name or scene.name)!r}")
        return True

    async def menu_remove_scene(self):
        """Pick a scene and remove it -- the voice and terminal door."""
        await self._io.write("\n--- REMOVE SCENE ---")
        scenes = self.sm.list_scenes(user=self.current_speaker_id)
        if not scenes:
            await self._io.write("No scenes.")
            self.failure_note = ("There are no scenes yet, so there is "
                                 "nothing to remove.")
            return False
        names = [s.name for s in scenes]
        pick = await self._pick_one(names, "Remove scene")
        if not pick:
            raise CancelledByUser()
        return await self.menu_drop_scene(pick)

    async def menu_drop_scene(self, name: str) -> bool:
        """Remove ONE named scene, no picker -- the row's own menu.

        Like menu_edit_scene, the name is LOOKED UP rather than trusted,
        and a name that is not in the registry removes nothing. Devices are
        untouched: a scene is a habit, not the things it acts on, and the
        message says so because "removed" over a list of lamps reads
        alarmingly otherwise.
        """
        scene = self.sm.get(name, user=self.current_speaker_id)
        if scene is None:
            self.failure_note = ("That scene is no longer here -- the list "
                                 "may be out of date.")
            return False
        if not await self._may_change("scene", scene.name, scene.created_by):
            return False
        eng = getattr(self, "automations", None)
        finder = getattr(eng, "rules_using_scene", None)
        used_by = finder(name) if finder is not None else []
        if used_by:
            await self._io.write(
                f"  ! {name} is run by {len(used_by)} automation(s): "
                f"{', '.join(used_by)}. They will keep running but will do "
                f"nothing there -- change or remove them.", level="warn")
        if not self.sm.remove(name):
            self.failure_note = f"{name} could not be removed."
            return False
        await self._io.write(f"  [OK] Removed scene: {name}. Its devices are "
                             f"untouched -- only the scene is gone.")
        self._tell_owner(scene.created_by, "scene", scene.name, "removed")
        return True

    # `menu_statistics` WAS HERE and is gone. It printed two lines -- how
    # many devices exist and which rooms they are in -- under a heading that
    # promised statistics, and it never read a single measurement. Both facts
    # are already on the Devices screen, which shows the rooms next to the
    # devices that are in them. The name is now what it always claimed to be:
    # `readings` draws the actual numbers, and the app's Statistics door
    # opens that.

    # ── Pipeline ────────────────────────────────────────────

    def _is_undo(self, text: str) -> bool:
        """Does this utterance mean "put it back"?

        Normalised before matching. The raw form compared `text.strip().lower()`
        against the phrase list directly, so anything carrying punctuation or a
        politeness word missed entirely:

            "undo."             -> no match   (speech recognition adds stops)
            "undo!"             -> no match
            "please undo"       -> no match
            "no, not that one"  -> no match   (the list holds the comma-less form)

        A user who says "undo." and watches nothing happen has no way to tell
        that a full stop was the problem. Same defect class as the resolver's
        five tokenizers, fixed there and not here."""
        return self._undo_rest(text) is not None

    @staticmethod
    def _undo_rest(text: str) -> Optional[str]:
        """What follows the undo words ("the bulbs" in "undo the bulbs"; ""
        when nothing does), or None when this is not an undo at all."""
        low = " ".join(re.sub(r"[^\w\s]", " ", text.lower()).split())
        words = low.split()
        while words and words[0] in UNDO_LEADERS:
            words.pop(0)
        low = " ".join(words)
        # Prefix, not substring: "don't undo that" must NOT undo.
        # Longest first, so the rest is what follows the WHOLE phrase.
        for p in sorted(UNDO_PHRASES, key=len, reverse=True):
            if low == p:
                return ""
            if low.startswith(p + " "):
                return low[len(p) + 1:]
        return None

    def _undo_named(self, text: str, speaker_id: str) -> Optional[List[str]]:
        """The devices an undo NAMES ("undo the bulbs"), or None for a plain
        undo of everything the last command changed.

        BY NAME ONLY -- the same exact-name walk the resolver's name rung
        uses, never by meaning: an undo that guessed its device would put
        back something nobody asked about. And only when the rest of the
        sentence asks for nothing ITSELF: "no wait, turn on the lamp" names a
        lamp too, and is not an undo of the lamp."""
        from nlu.resolver import _has_explicit_action, _literal_match_within
        rest = self._undo_rest(text) or ""
        if not rest:
            return None
        slots = self.extractor.extract(rest)
        if _has_explicit_action(slots):
            return None
        ctx = self._make_ctx(rest, self._extract_room(rest), slots, speaker_id)
        return _literal_match_within(ctx, list(self.dm.devices)) or None

    async def _do_undo(self, only: Optional[List[str]] = None) -> bool:
        """True only if something was actually rolled back.

        dm.undo() rewrites the LOCAL record and hands back what changed;
        pushing each old state to the device is done here, through
        _set_state, so "Undone" means the bulb went back, not just the
        JSON. Read-only devices (a sensor whose reading moved between
        snapshot and undo) are skipped -- nothing sets a sensor.

        AND SO ARE SECURE DEVICES (2026-09-12), by dm.undo() itself -- which
        is where the skip has to live, because that method has already
        rewritten the record by the time this one sees the list. "lock the
        front door" passes the active voice challenge and then snapshots, so
        a bare "undo" -- no challenge, no device named, the easiest word in
        the vocabulary to say -- put the lock back exactly as it was. That is
        an unlock with none of the checks an unlock requires, and it was
        reachable. What is left to do here is SAY SO: a door that quietly
        did not move is the same silence this project has spent the week
        digging out.
        """
        await self._io.write("[UNDO]")
        # Off the loop: undo() rewrites devices.json (write + fsync).
        # THIS PERSON'S: their own snapshot, and only what they changed
        # since (dm.snapshot's owner) -- another phone's command is theirs.
        owner = self.current_speaker_id
        reverted = await asyncio.to_thread(self.dm.undo, only, owner)
        held = self.dm.held_for(owner)
        if not reverted and not held:
            if only:
                names = ", ".join((self.dm.devices.get(d) or {}).get("name") or d
                                  for d in only)
                await self._io.write(
                    f"-> Nothing to undo for {names}: the last command did "
                    f"not change it.")
                return False
            await self._io.write("-> Nothing to undo.")
            return False
        # THE VERDICT IS WHAT THE DEVICES DID, NOT WHAT THE RECORD SAYS
        # (2026-09-14). This loop already warned per device that would not
        # move -- and then said "-> Undone." and returned True anyway,
        # because both were computed from `reverted`, which dm.undo() fills
        # the moment it rewrites the LOCAL RECORD.
        #
        # So: "kitchen lamp: could not be put back" immediately followed by
        # "-> Undone.", with True going back to ws_server, which tells the
        # phone ok=true. The lamp is still on and the registry now says it
        # is off -- the record and the house disagreeing, which is the one
        # outcome this method's own docstring forbids: "'Undone' means the
        # bulb went back, not just the JSON."
        moved: List[str] = []
        refused: List[str] = []
        for did, before in reverted:
            device = self.dm.devices.get(did) or {}
            name = device.get("name") or did
            if self.dm.is_readable_only(device):
                # Nothing to push and nothing claimed: a sensor's reading
                # moved between the snapshot and now, which is not an undo.
                continue
            if await self._set_state(did, **before):
                moved.append(name)
                continue
            refused.append(name)
            # PUT THE RECORD BACK TOO. dm.undo() has already rewritten it to
            # the old state; leaving that rewrite standing beside a device
            # that refused it is precisely the split brain above.
            #
            # `get_state` IS the repair and no new method is needed: reading
            # a device writes what it finds back into the record (the board
            # branch and the HA branch both do, deliberately, as the
            # "opportunistic refresh" their comments describe). So asking
            # the device where it actually is re-syncs the record to it. If
            # that read fails too, the warning below is all there is.
            try:
                # Off the loop: for an HA-backed device this is a REST call.
                await asyncio.to_thread(self.dm.get_state, did)
            except Exception:                          # noqa: BLE001
                pass
            await self._io.write(
                f"  -> {name}: could not be put back", level="warn")
        if moved:
            await self._io.write(
                "-> Undone." if not refused
                else f"-> Undone for {', '.join(moved)}.")
        elif refused:
            await self._io.write(
                "-> Nothing was put back -- nothing would take it.",
                level="warn")
        for name in held:
            await self._io.write(
                f"-> {name} was left exactly as it is -- undo does not move "
                f"a secure device. Name it and say what you want: "
                f"\"unlock the {name}\".", level="warn")
        return bool(moved)

    async def _premises_status(self, room: Optional[str] = None):
        """Full status of every device, grouped by room — for a whole-place
        query ('status of the house') rather than a specific device. With
        `room`, only that room: "how is the kitchen" reads out every device
        in it."""
        devices = {did: d for did, d in self.dm.devices.items()
                   if not room or str(d.get("room") or "").lower() == room}
        if not devices:
            await self._io.write(f"No devices in the {room}." if room
                                 else "No devices registered.")
            return
        by_room: Dict[str, list] = {}
        for did, d in devices.items():
            by_room.setdefault(d.get("room") or "(no room)", []).append((did, d))
        states = await self._states_of(list(devices))  # one thread, not N REST calls on the loop
        await self._io.write(f"\n--- STATUS ({len(devices)} device(s)) ---")
        for rm in sorted(by_room):
            await self._io.write(f"\n{rm}:")
            for did, d in sorted(by_room[rm], key=lambda p: p[1].get("name", p[0])):
                name = d.get("name", did)
                # THROUGH describe_reading, like every other answer.
                #
                # This used to interpolate the dict, so asking after the
                # house replied "{'on': False}" per device -- Python
                # punctuation read out as an answer, when the word is
                # "off". A meter was worse: "{'level': 146.0, 'unit': 'W'}"
                # instead of "146 W" (her report, 2026-08-29).
                reading = describe_reading(states.get(did, {}), d.get("reading_key"))
                await self._io.write(f"  {name:<20} {reading}")

    async def _rooms_overview(self):
        """"Which room?" / "what rooms are there": each room with the names
        of its devices. States are for when one room is named."""
        by_room: Dict[str, list] = {}
        for d in self.dm.devices.values():
            by_room.setdefault(d.get("room") or "(no room)", []).append(
                d.get("name") or "?")
        if not by_room:
            await self._io.write("No devices registered.")
            return
        for rm in sorted(by_room):
            await self._io.write(f"  {rm}: {', '.join(sorted(by_room[rm]))}")

    def _house_names(self) -> List[str]:
        """Every name the house answers to: devices, rooms, scenes."""
        sm = getattr(self, "sm", None)
        return ([d.get("name", "") for d in self.dm.devices.values()]
                + list(self.dm.rooms)
                + [getattr(sc, "name", "") for sc in
                   (sm.list_scenes() if sm is not None else ())])

    async def _say_clock(self, kind: str) -> bool:
        """The time and/or date, from the PHONE's clock (a Pi has no RTC and
        can be hours out). The hub's own keyboard has no phone, so it uses the
        hub's clock; a phone that sent none gets an honest "can't tell"."""
        now = self.phone_now
        if now is None and not self.current_phone_id:
            now = datetime.now().astimezone()
        if now is None:
            await self._io.write("  I can't read this phone's clock, so I "
                                 "can't tell you the time.", level="warn")
            return False
        t = now.strftime("%I:%M %p").lstrip("0")
        d = f"{now:%A}, {now.day} {now:%B %Y}"
        await self._io.write("  " + {"time": f"It's {t}.", "date": f"Today is {d}.",
                                     "both": f"It's {t} on {d}."}[kind])
        return True

    async def _whats_on(self, room: Optional[str] = None) -> None:
        """Answer "is anything on?": the devices that are ON now, in one
        room when the question named one. Sensors and learned remotes have no
        on/off to report, and a lock's "on" means locked, not running, so
        none of those are listed."""
        cands = []
        for did, d in self.dm.devices.items():
            if d.get("secure") or d.get("ir_codes") or self.dm.is_readable_only(d):
                continue
            if room and d.get("room") != room:
                continue
            cands.append(did)
        where = f" in the {room}" if room else ""
        if not cands:
            await self._io.write(f"  (no devices to check{where}.)")
            return
        states = await self._states_of(cands)

        def _is_on(st: Any) -> bool:
            if not isinstance(st, dict):
                return False
            if st.get("on") is True:
                return True
            lvl = st.get("level")
            return (isinstance(lvl, (int, float)) and not isinstance(lvl, bool)
                    and lvl > 0 and "on" not in st)

        on = [did for did in cands if _is_on(states.get(did))]
        if not on:
            await self._io.write(f"  Nothing is on{where}.")
            return
        names = [self.dm.devices[did].get("name") or did for did in on]
        for did in on:
            d = self.dm.devices[did]
            await self._io.write(
                f"  {d.get('name') or did}: "
                f"{describe_reading(states.get(did, {}), d.get('reading_key'))}")
        await self._io.send_data("toast", {"text": "On: " + ", ".join(names[:6])})

    async def _all_switch(self, on: bool, room: Optional[str] = None) -> bool:
        """Everything non-secure ON or OFF -- in one room when the utterance
        named one (the pre-filter reports it), else the whole house. `on` is
        one parameter since _all_off and _all_on were one body copied twice;
        the ON half dates from 2026-09-12.

        SECURE DEVICES ARE LEFT OUT OF BOTH, and for _all_off the reason is
        plain: `off` on a lock means UNLOCKED, so a blanket off that
        included them would open every door in the house -- and a blanket
        command names no device, so it cannot be challenged the way
        "unlock the front door" is.

        It is kept out of the ON half by her instruction ("just do what all
        off does, just copy it but for on") and it is also the cautious
        reading. `on` is the securing direction for a lock, so including
        them would mostly mean locking up -- but only mostly: a gate or a
        curtain opens on `on`, and secure_direction exists precisely
        because which way a device's `on` points is a per-domain fact and
        not a rule of thumb. A blanket command is the wrong place to be
        mostly right about locks, and the symmetry is worth more than the
        convenience: what "everything" means does not change with the verb.

        A person who wants the doors locked says so, and that command is
        challenged -- which is the whole point of the challenge.
        """
        word = "on" if on else "off"
        if room and room not in self.dm.rooms:
            from nlu.resolver import not_there
            await self._io.write(f"-> {not_there(self.dm, room)} Nothing switched.")
            return False
        where = f"{room} {word.upper()}" if room else f"Global {word.upper()}"
        await self._io.write(f"[PRE-FILTER] {where}")
        # Sensors and learned remotes have no state to set: counting them
        # reported "12 device(s) off" for a house with four lights.
        targets = [did for did, d in self.dm.devices.items()
                   if not d.get("secure") and not d.get("ir_codes")
                   and not self.dm.is_readable_only(d)
                   and (room is None or d.get("room") == room)]
        event("DEVICE", f"{where.lower()}: {len(targets)} non-secure device(s)")
        # COUNTED, NOT ASSUMED: the old line announced every target as
        # switched whatever the devices said.
        moved = 0
        for did in targets:
            moved += bool(await self._set_state(did, on=on))
        await self._io.write(f"-> {moved} device(s) {word} (secure devices left untouched).")
        if moved < len(targets):
            await self._io.write(f"   {len(targets) - moved} did not respond.",
                                 level="warn")
        return moved > 0 or not targets

    def _secure_on_value(self, did: str, d: Dict[str, Any]) -> Optional[bool]:
        """Which way is `on` the SAFE way for this device -- True, False, or
        None when nothing here knows.

        Asked through the engine, which owns the two tables, so there is one
        answer to this question in the project. The engine can fail to start
        and the hub keeps running, so the tables are reachable without it.
        """
        eng = self.automations
        if eng is not None:
            if eng.secure_direction(did, {"on": True}) == "secure":
                return True
            if eng.secure_direction(did, {"on": False}) == "secure":
                return False
            return None
        from nlu.automations import SECURING_ON, securing_on_word
        entity = str(d.get("ha_entity_id") or "")
        domain = entity.split(".", 1)[0] if "." in entity else ""
        want = SECURING_ON.get(domain)
        return want if want is not None else securing_on_word(d)

    def _extract_room(self, text: str) -> Optional[str]:
        low = text.lower()
        for r in sorted(self.dm.rooms, key=len, reverse=True):
            if r and re.search(r"\b" + re.escape(r) + r"\b", low):
                return r
        # A place the house does not know is a room only when it is said like
        # one: "in THE garage", "in MY study" (not "in red", "in turbo"). At
        # most three words, trimmed of trailing filler.
        m = re.search(r'\bin (?:the|my|our) ([a-z]+(?: [a-z]+){0,2})', " " + low)
        if m:
            # Cut at the FIRST stop word, not just trailing ones: in
            # "the light in the kitchen and lock the door" the room ends at
            # "and", and trimming only from the right left "kitchen and lock".
            words = m.group(1).strip().split()
            cut = next((i for i, w in enumerate(words) if w in ROOM_STOP_WORDS),
                       len(words))
            cand = " ".join(words[:cut])
            # "my room" is the word itself, not a room's name; "in the oven"
            # is a device; "in the high mode", "in the red" are a setting.
            if cand == "room" or (cand and self._names_device_or_setting(cand)):
                cand = ""
            if cand:
                # Returned even though the house has no such room, so the
                # person is told so rather than another room being used.
                return cand
        return None

    def _names_device_or_setting(self, phrase: str) -> bool:
        """Is `phrase` a device's name, or a value/colour/level the extractor
        reads (so not a place)?"""
        first = phrase.split()[0]
        for d in (getattr(self.dm, "devices", None) or {}).values():
            name = str(d.get("name") or "").lower()
            if name and (name == phrase or first in name.split()):
                return True
        try:
            got = (getattr(self, "extractor", None) or SlotExtractor()).extract(phrase) or {}
        except Exception:                                   # noqa: BLE001
            return False
        return bool(got.get("colour") or got.get("delta_raw")
                    or got.get("absolute_value") is not None or got.get("switch"))

    async def _fire_scene(self, scene: Dict[str, Any]) -> bool:
        """True when at least one device took its part (or there was none
        to take). Each line used to print the device id and the raw state
        dict -- and "-> kitchen: {'on': True}" for a device that had refused
        it, with the command reported as done."""
        event("SCENE", f"firing {scene['name']!r} "
                       f"({len(scene.get('actions', []))} action(s))")
        await self._io.write(f"[SCENE] {scene['name']!r}")
        tried = moved = 0
        for act in scene.get("actions", []):
            did = act["device_id"]
            if did not in self.dm.devices:
                await self._io.write(f"  skipping {did} (not registered)")
                continue
            tried += 1
            ok, said = await self._scene_act(did, act["state"])
            if ok:
                moved += 1
                await self._io.write(f"  -> {said}")
            else:
                await self._io.write(f"  -> {self._dev_name(did)}: could not "
                                     f"be set", level="warn")
        await self._io.write("[SCENE] Complete")
        return moved > 0 or not tried

    @staticmethod
    def _names_a_device(decision: Dict[str, Any]) -> bool:
        """Did the resolver reach this decision through a device's NAME --
        the literal rung, the securement rung (a lock named outright) or the
        context rung (a pronoun for the device just touched)? Those are the
        rungs whose answer is a fact about the registry, not a similarity
        score, and a command that names a device is a device command however
        much it resembles "make an announcement". A word of a device's name
        that meaning agrees with (name_word*) and a word that sounds the
        same as one (heard_as) count too, as they do in the other copy's
        _BY_NAME. Semantic and room-only decisions, a sound only close to a
        name (heard_confirm), and no match at all, are guesses -- the intent
        classifier may still overrule them."""
        reason = str(decision.get("reason") or "")
        return reason.startswith(("literal", "securement", "context",
                                  "name_word")) or reason == "heard_as"

    async def _classify_intent(self, text: str) -> Optional[Dict[str, Any]]:
        if not self.intent_classifier:
            return None
        # classify() already nulls the intent below threshold/margin — feed it
        # the production values so the decision lives in exactly one place.
        # Off-loop: it embeds, which is a blocking model call.
        return await asyncio.to_thread(
            self.intent_classifier.classify,
            text, threshold=INTENT_MIN_SCORE, margin_floor=INTENT_MIN_MARGIN)

    # ── Resolution context adapter ──────────────────────────
    def _make_ctx(self, text, room_hint, slots, speaker_id):
        shell = self

        class _Ctx:
            def __init__(self):
                self.dm = shell.dm
                self.text = text
                self.room_hint = room_hint
                self.slots = slots
                self.speaker_id = speaker_id

        return _Ctx()

    # ── Decision carry-out: orchestrate I/O, then execute ────
    # ── infrared appliances: a button, not a state ──────────────────────
    #
    # A learned remote (Learn IR codes) is a device whose whole vocabulary
    # is its buttons. It has no state the hub can read and no road that
    # set_state knows -- transport "ir", no board relay, no HA entity -- so
    # until this existed a resolved "pause the tv" reached dm.set_state and
    # died as "no road to this device" (audit, 2026-09-03). The codes were
    # stored and never sent. This is the road: the words name a button,
    # its own board, or a blaster in its room, sends it.

    @staticmethod
    def _ir_words(text: Any, drop: Any = ()) -> List[str]:
        """Lower-case words of `text`, punctuation and the words of `drop`
        removed."""
        def split(s: Any) -> List[str]:
            return [w.strip(string.punctuation)
                    for w in str(s or "").lower().replace("'", " ")
                    .replace("_", " ").split()]
        gone = {w for s in drop for w in split(s)}
        return [w for w in split(text) if w and w not in gone]

    @classmethod
    def _ir_button_for(cls, codes: Dict[str, Any], text: str,
                       slots: Optional[Dict[str, Any]] = None,
                       drop: Any = ()) -> Optional[str]:
        """Which learned button the utterance asks for: button names matched as
        words (underscores as spaces, longest first so `volume_up` beats `up`).
        `drop` is the appliance's own name and room: their words are removed
        from both sides first, so a button named like the appliance cannot
        take over. Only the buttons' own names are matched (`slots` is not
        read). None when nothing fits; never a guess."""
        words = " " + " ".join(cls._ir_words(text, drop)) + " "
        for name in sorted(codes, key=len, reverse=True):
            said = " ".join(cls._ir_words(name, drop))
            if said and f" {said} " in words:
                return name
        return None

    async def _press_ir(self, did: str, text: str,
                        slots: Optional[Dict[str, Any]]) -> bool:
        """Send one learned button of one IR appliance. True only when a
        board in reach took it. Says, in words, why not otherwise."""
        from esp_firmware.ir_flows import (ir_command_body, ir_not_sent,
                                           ir_senders)
        d = self.dm.devices.get(did) or {}
        codes = d.get("ir_codes") if isinstance(d.get("ir_codes"), dict) else {}
        name = d.get("name") or did
        # The app's button sends the button's own name.
        button = text if text in codes else self._ir_button_for(
            codes, text, slots, drop=(d.get("name"), d.get("room")))
        if button is None:
            known = ", ".join(sorted(k.replace("_", " ") for k in codes))
            await self._io.write(
                f"  {name} knows: {known or 'no buttons yet'} -- say one of "
                f"those.", level="warn")
            return False
        said = button.replace("_", " ")
        if ir_command_body(codes[button]) is None:
            self.failure_note = (f"{name}'s '{said}' is stored in a form "
                                 f"that cannot be sent -- learn it again.")
            await self._io.write(f"  {self.failure_note}", level="warn")
            return False
        emitters = self.esp.boards_with("ir_send")
        if not emitters:
            await self._io.write(
                f"  No board has an infrared LED yet, so {name} cannot be "
                f"sent '{said}'. Add an 'Infrared "
                f"blaster' to a board; nothing needs relearning.",
                level="warn")
            return False
        # Its own board, then only blasters in its room; send_ir reads each
        # board's last will first, so an offline one is passed over.
        order, room = ir_senders(self.dm, self.esp, did, emitters)
        for slug in order:
            if self.automations is not None:
                # Before the send: the echo can arrive before it returns.
                self.automations.note_ir_sent(codes[button])
            if await self.esp.send_ir(slug, codes[button]):
                event("IR", f"sent {did}.{button} via {slug}")
                await self._io.write(f"  -> {name}: {said}")
                return True
        self.failure_note = ir_not_sent(name, button, room, bool(order))
        await self._io.write(f"  {self.failure_note}", level="warn")
        return False

    async def _carry_out(self, decision, slots, speaker_id, audio=None,
                         snapshot=None, text: str = "") -> bool:
        """Returns True if the command actually accomplished something
        (a device changed, a scene fired), False if nothing happened --
        blocked by the voice gate, no device matched, or nothing resolved.
        This return value is what lets ws_server.py tell the phone the
        truth about whether a command worked, instead of always saying
        ok=true regardless of what actually occurred.

        `snapshot` is _run_command's undo-snapshot callable, taken HERE,
        immediately before the first mutation. Taken before this method
        ran, a no-match chunk or a refused secure command replaced the
        undo buffer with the current state and "undo" then undid nothing."""
        kind = decision["kind"]
        act = decision.get("action")
        # AN ADDITION SAID OVER THE LAST COMMAND ("and the fan") takes that
        # command's action when its own words give none (ws_server
        # _run_asides). Words that do say ("and the fan off") win.
        carry = getattr(self, "carry_slots", None) or {}
        if carry and act is None and not _has_explicit_action(slots):
            slots = {**(slots or {}), **carry}
        # What THIS command was told to do, for whatever is said over it.
        self.command_action = {k: (slots or {})[k] for k in _ACTION_SLOTS
                               if (slots or {}).get(k) is not None}
        # Something is being said over this command: wait for it to be read
        # (or for the hold to run out) before a device is touched.
        hold = getattr(self._io, "wait_hold", None)
        if hold is not None:
            await hold()
        # Cancel pressed while the hub was still working this out: stop
        # before anything is touched, and again just before each write.
        self._io.raise_if_cancelled()
        _take_snapshot = snapshot or (lambda: None)

        def snapshot():
            self._io.raise_if_cancelled()
            _take_snapshot()

        if kind == "none":
            # NOT A FAILURE, and _run_multi needs to know the difference. A
            # chunk that resolved to nothing was not a command -- speech
            # recognition noise, most often -- while a chunk that found a
            # device and could not act on it is a real failure with a cause.
            # Both return False here; this flag is what separates them.
            #
            # LAST RESORT FOR THE CLOCK: nothing understood this, and it
            # names the time or the date ("always the time" -- a mis-heard
            # "what is the time"). See lexicon.clock_question, loose tier.
            clock = clock_question(text, self._house_names(), loose=True)
            if clock:
                return await self._say_clock(clock)
            self._last_no_match = True
            await self._io.write(decision.get("message", "No device matched."))
            return False

        # Resolve the target list (prompting where the decision is ambiguous).
        if kind == "execute":
            targets = list(decision["targets"])
        elif kind in ("pick", "elsewhere"):
            # THE QUESTION IS THE PICKER'S PROMPT, NOT A LINE ABOVE IT.
            #
            # This used to write() the resolver's message ("Which one(s)?",
            # "Found in other rooms:") as ordinary output and then ask with
            # a generic "Pick". On a terminal that read as a heading over
            # the list. On a phone it was a bug with a voice:
            #
            #   write("Which one(s)?")   -> output frame, level=info
            #   choose("Pick", ...)      -> choice frame
            #   ... she picks, the bulb comes on ...
            #   write("  -> bulb: on")   -> output frame
            #   result
            #
            # The phone collects output lines while a command is in flight
            # and speaks them as the outcome, so what it said after turning
            # the light on was "Which one(s)? Bulb: on" -- reciting the
            # question it had just been given the answer to (her report,
            # 2026-09-11: "it's meant to say done or the device is on...
            # instead it says which ones").
            #
            # A question is not narration. Asked as the prompt it reaches
            # the phone inside the `choice` frame, which is the frame that
            # gets answered and thrown away, and the terminal asks it at
            # the input line where a question belongs.
            asked = decision.get("message") or (
                "Found in other rooms:" if kind == "elsewhere"
                else "Which one(s)?")
            options = [c["device_id"] for c in decision["candidates"]]
            # "Did you mean the X?" -- one device at a time, yes or no: a
            # guess by meaning, or a word that only sounds like a name
            # (heard_confirm, the resolver's heard-as rung).
            if decision.get("reason") in ("semantic_confirm", "heard_confirm"):
                did = await self._confirm_guess(decision)
                targets = [did] if did else []
            elif decision.get("allow_all"):
                targets = await self._pick_multi(options, asked)
            else:
                pick = await self._pick_one(options, asked)
                targets = [pick] if pick else []
        else:
            return False

        if not targets:
            return False

        # Safety net: a BARE/ambiguous reference (no explicit on/off/value/
        # delta) must never silently flip a secure device — whether it got
        # here by a direct match or after the user picked from a list. This
        # closes the gap that let something like bare "the front door" (a
        # secure device reached by plain name, not a securement verb) flip a
        # lock with zero confirmation. Decisions that already carry their own
        # action (the securement rung's explicit lock/unlock handling below)
        # are untouched — this only guards a plain execute with no action yet.
        # A secure device ALWAYS goes through the voice gate: the voice of
        # the command must match, or the PIN is asked.
        if act is None and any(self.dm.devices.get(t, {}).get("secure") for t in targets):
            # ANY secure target, full stop. This used to carry
            # `and not _has_explicit_action(slots)`, which meant the guard only
            # fired for a BARE reference -- so "the front door" was challenged
            # but "turn off the front door" was not. The securement rung only
            # claims commands built on a securement VERB, so a lock reached by
            # its literal name with an ordinary switch attached arrived here
            # with action=None and an explicit slot, and went through on the
            # passive check alone. Verified reachable.
            #
            # The stated policy is "a secure device ALWAYS goes through the
            # voice gate" -- the exemption contradicted it.
            names = ", ".join(self.dm.devices.get(d, {}).get("name", d) for d in targets)
            await self._io.write(f"  Confirming access to secure device(s): {names}")
            if not await self._voice_gate(speaker_id, audio, force_active=True):
                await self._io.write("  (cancelled — voice/PIN verification failed)")
                return False

        # Secure action: the voice gate for EVERY secure-device action, lock
        # or unlock alike — not just security-reducing verbs.
        if act and act.get("type") == "secure":
            await self._io.write(f"  Confirming {act['verb']} on {len(targets)} secure device(s)")
            if not await self._voice_gate(speaker_id, audio, force_active=True):
                await self._io.write("  (cancelled — voice/PIN verification failed)")
                return False
            snapshot()
            # THE VERB SAYS WHICH DIRECTION, THE DEVICE SAYS WHICH VALUE
            # (2026-09-12). This drove `on=act["lock_on"]` -- True for
            # lock/bolt/arm, False for unlock/disarm -- across every target,
            # which is right for a lock and backwards for anything that opens
            # on `on`. "lock the gate" set a cover to on, and a cover's on is
            # OPEN_COVER: the command to shut the gate opened it. That is the
            # bug secure_direction was written to prevent, and this was the
            # one place in the project that did not ask it. A device whose
            # direction is unknown keeps the verb's plain reading, which is
            # what it has always had -- it is not made worse here, and it is
            # said out loud rather than assumed.
            moved: List[str] = []
            for did in targets:
                dev = (self.dm.devices or {}).get(did) or {}
                safe = self._secure_on_value(did, dev)
                on = act["lock_on"] if safe is None else (
                    safe if act["lock_on"] else not safe)
                name = dev.get("name") or did
                # THE LOCK'S ANSWER, NOT THE VERB'S. This reported "-> front
                # door: lock" and returned True for a lock that refused the
                # write -- ok=true on the phone for a door still open.
                if not await self._set_state(did, on=on):
                    await self._io.write(
                        f"  -> {name}: could not {act['verb']} -- it did not "
                        f"take the command.", level="warn")
                    continue
                moved.append(did)
                if safe is None:
                    await self._io.write(
                        f"  -> {name}: {act['verb']} -- but it never said "
                        f"which way is safe, so this is the plain reading of "
                        f"the word. Set it to Locked/Unlocked or Open/Closed "
                        f"when you edit it and I will be sure.", level="warn")
                else:
                    await self._io.write(f"  -> {name}: {act['verb']}")
            self.dm.touch_group(speaker_id, moved)
            return bool(moved)

        # The executor's verdict is the ONLY evidence that anything happened.
        # It used to be rendered and thrown away, and True returned regardless
        # -- so a command where every device no-op'd or errored still reported
        # ok=true to the phone, which is exactly what this return value exists
        # to prevent.
        # `satisfied` rather than `outcome == 'acted'`: see executor.py's
        # result contract. A device already in the requested state satisfies
        # the command without changing anything.
        acted = False
        snapshot()
        # INFRARED APPLIANCES TAKE THEIR OWN ROAD: a button is sent, no
        # state is written, and the executor never sees them (it would
        # only report "no road"). Everything else goes on as before.
        remote = [t for t in targets
                  if (self.dm.devices.get(t) or {}).get("ir_codes")]
        targets = [t for t in targets if t not in remote]
        for did in remote:
            if await self._press_ir(did, text, slots):
                acted = True
        for did in targets:
            # THE DEVICE'S OWN WORDS ARE ITS ON AND OFF. "open the gate" on
            # a gate registered with open/closed used to reach the executor
            # with no switch at all and became a FLIP -- so an open gate
            # closed. The verb is read against the resolved device's state
            # words here, where both are known; a command that already
            # carries a switch, a value or a delta is left alone.
            per_slots = slots
            if (slots.get("switch") is None and slots.get("absolute_value") is None
                    and slots.get("delta") is None):
                sw = switch_from_state_words(text, self.dm.devices.get(did) or {})
                if sw:
                    per_slots = {**slots, "switch": sw}
            # Off the loop: execute() ends in dm.set_state, which is an HA
            # REST call or a blocking MQTT connect+publish.
            res = await asyncio.to_thread(
                self.executor.execute, did, per_slots, speaker_id)
            await self._render(res)
            if res.get("satisfied"):
                acted = True
                # HUMAN OUTRANKS MACHINE: a device this person just
                # commanded is immune to automations for the override
                # window -- including "already like that", which is still
                # an expressed intention about this exact device.
                if self.automations is not None:
                    try:
                        self.automations.note_human(did)
                    except Exception:              # noqa: BLE001
                        pass
        if acted:
            # Remember the set acted on together, so a later pronoun command
            # ("make it colder") refers back to this group. Only on success:
            # a group nothing happened to is not a back-reference worth having.
            self.dm.touch_group(speaker_id, targets + remote)
            # A NAME MATCHED BY SOUND ONLY ("bob" for "bulb", the resolver's
            # heard-as rung): said, so a misreading is heard at once.
            heard = decision.get("heard") or {}
            if decision.get("reason") == "heard_as" and heard.get("said"):
                await self._io.write(
                    f"  I heard \"{heard['said']}\" and took it as "
                    f"{heard.get('as')}.")
        return acted

    async def _render(self, res):
        outcome = res.get("outcome")
        did = res.get("device_id", "?")
        # CHANGED: the person hears/reads the NAME and the resulting STATE,
        # not the internal id and a bare change word. The event line below
        # keeps the full machine detail for the console.
        name = self.dm.devices.get(did, {}).get("name") or did
        after = res.get("after")
        event("DEVICE", f"executor {did}: outcome={outcome} "
                        f"satisfied={res.get('satisfied')} "
                        f"change={res.get('change')} key={res.get('key')} "
                        f"value={res.get('value')} reason={res.get('reason')} "
                        f"{res.get('before')} -> {after}")
        if outcome == "acted":
            change = res.get("change")
            if change == "set":
                # CHANGED: was f"{did}: {key}={value}" -- now reads as
                # "ceiling lights: on, 50.2" via describe_reading.
                await self._io.write(f"  -> {name}: {describe_reading(after)}")
            elif change == "pressed":
                await self._io.write(f"  -> {name}: pressed")
            elif change == "colour":
                await self._io.write(f"  -> {name}: colour {res.get('value')}")
            else:
                await self._io.write(f"  -> {name}: {change}")
        elif outcome == "noop":
            if res.get("satisfied"):
                # CHANGED: was "already like that" -- now carries the value:
                # "ceiling lights: already on, 50.2".
                await self._io.write(f"  -> {name}: already {describe_reading(after)}")
            else:
                # The executor's reasons are machine strings. The ones a
                # PERSON can act on get a sentence; everything else still
                # shows the raw reason rather than being swallowed.
                reason = res.get("reason", "")
                await self._io.write(f"  -> {name}: " + {
                    "read_only_device":
                        "that only reports a reading -- there is nothing to switch",
                    "press_takes_no_value":
                        "that is a button; it can be pressed, but not set to a value",
                    "press_cannot_turn_off":
                        "that is a button; it can be pressed, but has no off",
                    "not_colourable":
                        "that light cannot change colour -- it only dims",
                    "unknown_device": "no such device",
                }.get(reason, f"no action ({reason})"))
        elif outcome == "error":
            # THE REAL REASON, WHEN THE HUB HAS IT (2026-09-11). A rejected
            # write on a board device used to read "FAILED (write_rejected)"
            # -- the executor's own word for "dm.set_state said no", which
            # tells a person nothing they can do anything about. The drive
            # seam remembers why it said no, so a board that is simply not
            # on the broker says exactly that, with its name.
            problem = (self._drive_problem(did)
                       if res.get("reason") == "write_rejected" else "")
            if problem:
                sentence = self._drive_problem_sentence(did, problem)
                await self._io.write(f"  -> {name}: {sentence}", level="warn")
                # And it is the note the app's failure popup shows, so the
                # phone says it too rather than "that did not complete".
                # Several devices with the same reason are ONE sentence:
                # "bulb, lamp: the kitchen board is offline ...".
                prev, tail = self.failure_note or "", f": {sentence}"
                self.failure_note = (prev[:-len(tail)] + f", {name}{tail}"
                                     if prev.endswith(tail)
                                     else f"{name}{tail}")
            else:
                await self._io.write(
                    f"  -> {name}: FAILED ({res.get('reason', 'unknown error')})")
        else:
            # Anything the executor grows later still reaches the person
            # instead of vanishing -- silence was indistinguishable from success.
            await self._io.write(f"  -> {name}: unrecognised outcome {outcome!r}")

    # ── The pipeline ────────────────────────────────────────
    async def _run_command(self, text: str, speaker_id: str,
                           _skip_snapshot: bool = False,
                           audio: Optional[np.ndarray] = None) -> bool:
        """One command. NOT AN ENTRY POINT -- go through _run_multi.

        THE VOICE GATE IS NOT HERE, and that is deliberate. It runs once in
        _run_multi, before the utterance is split: one recording is one proof
        of identity, and gating per chunk verified the same audio N times and
        advanced the periodic-challenge counter N times for a single thing
        the person said.

        This method used to hold a second copy of the gate behind a
        _skip_voice_gate flag that both callers always set to True, so it
        could never run -- an unreachable security check, which is worse than
        none because it reads as protection. Removed rather than left there.
        Anything that calls this directly is unverified; call _run_multi.

        Per-target escalation for secure devices still happens in _carry_out,
        which is where it belongs: that depends on the target, not on the
        utterance.
        """
        event("CMD", f"running {loggable(text)} as speaker={speaker_id!r} "
                     f"audio={'yes' if audio is not None else 'no'}")
        # Every device this command changes is recorded as this person's,
        # so their undo -- and only theirs -- puts it back (dm.snapshot).
        _ACTOR.set(speaker_id)
        # Cancel pressed while the voice was being checked: stop here.
        self._io.raise_if_cancelled()

        if self._is_undo(text):
            return await self._do_undo(self._undo_named(text, speaker_id))

        def _snapshot():
            # Snapshot only right before an actual mutation, so a query, an
            # ML-dismiss, a no-match or a refused secure command never
            # clobbers the undo buffer. _carry_out takes it itself.
            if not _skip_snapshot:
                self.dm.snapshot(speaker_id)

        # Pre-filters. known_rooms lets "everything off in the kitchen"
        # report the room instead of switching off the whole house.
        pf = run_pre_filters(text, known_rooms=self.dm.rooms)
        # GRAMMAR'S SECOND OPINION (nlu/grammar.asks): spots a question
        # wherever it starts. lexicon.is_query reads the first word, so "hey
        # is the fan still on" was an order, and "excuse me is everything
        # off" switched the house off. One way only: it can make a sentence a
        # question, never an order.
        from nlu import grammar
        by_grammar = await asyncio.to_thread(grammar.asks, text)
        if not getattr(self, "_grammar_said", False):
            self._grammar_said = True
            event("CMD", "grammar check " + ("on" if not grammar.off_reason()
                                             else f"off: {grammar.off_reason()}"))
        if pf and by_grammar and pf.get("intent") in ("all_off", "all_on",
                                                       "cancel_timer"):
            event("CMD", f"{loggable(text)} is a question (grammar) -- "
                         f"read, not {pf.get('intent')}")
            pf = {"intent": "query", "source": "grammar"}
        if pf:
            if pf.get("negated"):
                await self._io.write("-> Command negated — ignoring.")
                return True  # correctly recognized and deliberately a no-op, not a failure
            intent = pf.get("intent")
            if intent in ("all_off", "all_on"):
                _snapshot()
                return await self._all_switch(intent == "all_on",
                                              room=pf.get("room"))
            if intent in ("cancel_timer", "cancel_alarm"):
                await self._io.write(f"-> {intent} (not implemented)")
                return False  # genuinely didn't happen -- say so honestly

        room_hint = self._extract_room(text)
        slots = self.extractor.extract(text)
        is_query = bool(pf and pf.get("intent") == "query")
        if by_grammar and not is_query:
            is_query = True
            event("CMD", f"{loggable(text)} is a question (grammar) -- "
                         f"read, not switched")
        # "What does the fan do?" asks what it is FOR: answered from its
        # description and how it is controlled; nothing is switched.
        purpose = asks_purpose(text)
        if purpose:
            is_query = True
        # ...and by meaning, for questions the shape rules missed ("why do I
        # have a fan"). Names are blanked first; only a clear win is "what is
        # it for" (lexicon.purpose_by_meaning, config.PURPOSE_MIN_*).
        if is_query and not purpose:
            names = [str(d.get("name") or did)
                     for did, d in self.dm.devices.items()] + list(self.dm.rooms)
            got = await asyncio.to_thread(purpose_by_meaning, text, names)
            purpose = bool(got.get("purpose"))
        if purpose:
            # Read by the resolver: "what does IT do" is about the device
            # last touched.
            slots = {**(slots or {}), "purpose": True}

        # A TIME SAID FOR LATER IS NOT NOW. The extractor reads a time first
        # ("at seven thirty", "in ten minutes") so it is no longer taken for
        # a value, but this hub has no timed commands: doing it now would
        # answer "Done" to something asked for later. Said, and nothing done.
        when = slots.get("when") or {}
        if (not is_query and _has_explicit_action(slots)
                and (any(k in when for k in ("after", "clock", "weekday",
                                             "date", "vague"))
                     or when.get("day", 0) != 0)):
            event("CMD", f"{loggable(text)}: a time for later ({when}) -- "
                         f"not done now")
            await self._io.write("-> I can't do things at a later time yet. "
                                 "Say it again when you want it done.")
            return False

        # A status question about the WHOLE place ("status of the house", "how
        # is the house doing") names no specific device — answer it by listing
        # every device, not by resolving to a semantic subset of a few.
        if is_query and is_premises_query(text):
            await self._premises_status()
            return True

        # Questions whose answer is a fact: time and date (the phone's clock),
        # the rooms themselves, one room's devices. Named, so answered -- the
        # resolver and the encoder are for what is not.
        clock = clock_question(text, self._house_names())
        if clock:
            return await self._say_clock(clock)
        if is_rooms_question(text) or is_inventory_question(
                text, self._house_names()):
            await self._rooms_overview()
            return True
        room_only = room_only_query(text, self.dm.rooms)
        if room_only:
            await self._premises_status(room_only)
            return True

        # "Is there anything on?" / "what's on" -- which devices are ON, not
        # the state of one named device. The resolver has nothing to match
        # in it, so it answered "(nothing to read.)" with the lights burning.
        if is_query and is_whats_on_query(text):
            await self._whats_on(room_hint)
            return True

        # Scene (needs an embedding). Skipped for state queries so "is movie
        # time on?" reads state instead of firing the scene.
        if not is_query and self._require_init().list_scenes():
            try:
                t = time.perf_counter()
                cmd_emb = await asyncio.to_thread(embed, text)
                times_add("scene", time.perf_counter() - t)
                event("CMD", f"scene embed in {time.perf_counter() - t:.2f}s")
                cmd_emb = cmd_emb / (np.linalg.norm(cmd_emb) + 1e-9)
                scene_res = self.sm.resolve(cmd_emb, speaker=speaker_id)
            except Exception as e:
                print(f"[scene] skipped: {e}")  # internal diagnostic, console-only
                scene_res = None
            if scene_res:
                if scene_res["status"] == "fired":
                    # A SCENE THAT TOUCHES A LOCK IS A LOCK COMMAND. It used
                    # to fire on the passive check alone -- the one road
                    # past the voice gate every secure device gets.
                    if scene_res.get("secure"):
                        await self._io.write(
                            f"  Scene {scene_res['scene']['name']!r} touches a "
                            f"secure device -- confirming.")
                        if not await self._voice_gate(speaker_id, audio,
                                                      force_active=True):
                            await self._io.write(
                                "  (scene refused — voice/PIN verification failed)")
                            return False
                    _snapshot()
                    return await self._fire_scene(scene_res["scene"])
                if scene_res["status"] == "ambiguous":
                    await self._io.write("Ambiguous scene. Please be more specific.")
                    return False

        # Resolve (pure)
        ctx = self._make_ctx(text, room_hint, slots, speaker_id)
        t = time.perf_counter()
        # Off the loop: the semantic rungs embed the command (one MPNet
        # pass, the same blocking call _classify_intent already threads).
        with timed("resolve"):
            decision = await asyncio.to_thread(resolve_command, ctx)
        # `kind` is the RESOLVER's word for how well it identified a target --
        # "execute" means "exactly one device", as opposed to "pick" or "none".
        # It is not a statement that anything will be acted on. Printing it bare
        # made a read-only question log as `resolved -> execute`, which reads as
        # "it fired", and cost a real debugging session working out whether a
        # question had switched a light. Say which branch is about to run.
        event("CMD", f"resolved -> {decision['kind']}"
                     f"{' (read-only query)' if is_query else ''} "
                     f"(reason={decision.get('reason')}) in {time.perf_counter() - t:.2f}s")
        # ...or while the devices were being worked out: nothing is
        # answered, asked or guessed at for a command that was cancelled.
        self._io.raise_if_cancelled()

        # Intent (dismiss/announce) -- AFTER resolution, and only for an
        # utterance that did not NAME a device.
        #
        # This ran BEFORE the resolver, on every command, and the Chapter
        # Four benchmark on the Pi (2026-09-10) caught it stealing device
        # commands: "turn on the buzzer" -- a device registered by that
        # name -- never reached the resolver, because MPNet put it within
        # INTENT_MIN_SCORE of the "announce" seeds ("shout this message
        # through the intercom", "make an audio announcement") and the
        # announce branch, which is not even implemented, answered instead.
        # Five of six buzzer commands and "turn on the display in the
        # kitchen" went the same way. The classifier exists for two
        # phrasings that collide with commands ("never mind", "announce"),
        # and its own file says so: everything with a device in it is
        # "handled deterministically upstream". So a decision that came
        # from a device's NAME (the literal, securement and context rungs)
        # is final and the classifier is not consulted; a semantic guess or
        # no match is where "never mind" must still win, and there it does.
        # The classifier is also one MPNet pass, ~130 ms of a ~145 ms
        # command on a Pi 4B: literal commands no longer pay it at all.
        # NOR A MATCH THE RESOLVER WAS SURE ENOUGH OF TO ACT ON. "Turn on the
        # lights" found the bulb and the lamp by meaning, and this check then
        # called it an announcement (0.575, nearest 'household announcement')
        # and ended it with nothing done -- twice (her hub, 2026-10-05).
        #
        # SO IT HAS THE LAST TURN, NOT A VETO (her ruling, same day): exact
        # words, then names, then devices by meaning -- and only when all of
        # those found NOTHING is "did they mean never mind?" asked. One guess
        # by meaning does not overrule another.
        if decision["kind"] == "none":
            with timed("intent"):
                intent_res = await self._classify_intent(text)
            if intent_res:
                # SAID IN THE LOG: this check can end a command before any
                # device is asked about, and nothing used to show that it had.
                event("CMD", f"intent check: {intent_res.get('intent')} "
                             f"(score {intent_res.get('score')}, margin "
                             f"{intent_res.get('margin')}, nearest "
                             f"{intent_res.get('nearest_seed')!r})")
                if intent_res.get("intent") == "dismiss":
                    # "NEVER MIND" IS NOT "UNDO" (2026-10-07). This used to
                    # undo the last command, and the check is a guess by
                    # meaning: "It is too dark in here", heard as "He stood
                    # back in a.", scored 0.686 against 'stand down' and
                    # put back whatever was done before it. Undo is asked
                    # for in its own words (_is_undo), never guessed.
                    await self._io.write("-> Okay, never mind. Nothing was changed.")
                    return True
                if intent_res.get("intent") == "announce":
                    await self._io.write("-> Announce intent.")
                    return False  # not implemented -- say so honestly, not a silent success

        if is_query:
            dids: List[str] = []
            matched = decision["kind"] == "execute" and bool(decision["targets"])
            if matched:
                dids = list(decision["targets"])
            elif decision.get("reason") in ("semantic_confirm", "heard_confirm"):
                # A WEAK GUESS IS ASKED, never answered: a reading of the
                # wrong device is worse than none. (heard_confirm: a word
                # that only sounds like a device's name.)
                did = await self._confirm_guess(decision)
                if not did:
                    return True
                dids = [did]
                matched = True
            elif decision["kind"] in ("pick", "elsewhere") and decision["candidates"]:
                # THE SAME SENTENCE THE MATCHED PATH GIVES, and the same
                # NAME. This branch is the ambiguous one ("which lamp did
                # you mean?"), and it answered in two machine dialects at
                # once: the raw state dict, and the device ID -- a storage
                # key. Nobody named their lamp `light.living`, and nobody
                # calls off "{'on': False}".
                dids = [c["device_id"] for c in decision["candidates"]]
            if not dids:
                await self._io.write(
                    f"  {NO_DEVICE} Ask me what devices you have to hear them.")
                return True
            if purpose:
                # "What does the fan do?": what it is FOR, not its state. No
                # reading is taken, nothing is switched.
                for did in dids:
                    await self._io.write(f"  {self._what_it_does(did)}")
                return True
            states = await self._states_of(dids)
            shown = []
            # "Do I have a power meter?" -- yes, and here it is, said like
            # every device answer: its name, then its state.
            yes = "Yes. " if matched and asks_existence(text) else ""
            for did in dids:
                d = self.dm.devices.get(did, {})
                reading = describe_reading(states.get(did, {}), d.get("reading_key"))
                shown.append(f"{yes}{d.get('name') or did} is {reading}"
                             if reading == NO_READING
                             else f"{yes}{d.get('name') or did}: {reading}")
                yes = ""
                await self._io.write(f"  {shown[-1]}")
            if matched:
                # THE ANSWER AS A CARD: spoken as always, and also shown
                # in the same small self-vanishing popup "Done" uses -- a
                # glance at the phone catches the number without hunting
                # for it in a log.
                await self._io.send_data(
                    "toast", {"text": "\n".join(shown[:4])})
            return True  # a query that found nothing is still a complete, correct answer

        # No _snapshot() here: _carry_out takes it just before it mutates.
        with timed("act"):
            return await self._carry_out(decision, slots, speaker_id, audio=audio,
                                         snapshot=_snapshot, text=text)

    async def _run_multi(self, text: str, speaker_id: str,
                         audio: Optional[np.ndarray] = None) -> bool:
        """Run every chunk of a (possibly split) command.

        A chunk that MATCHED NOTHING is not a failure -- it is not a command.
        Speech recognition mangles the front of an utterance constantly:
        "turn on the lights" came back as "Done on the lights", which split
        into 'done' (matching nothing) and 'on the lights' (which switched the
        light on correctly). Requiring every chunk to succeed reported that
        whole utterance as failed while the lights were, in fact, on.
        Splitting exists so each real command gets acted on independently;
        letting a fragment of noise veto the ones that worked defeats it.

        So: True if at least one chunk did something and nothing actively
        FAILED. A chunk that was blocked by the voice gate, errored, or found
        a device and could not act on it still fails the whole utterance --
        those are real failures with a cause worth surfacing. Only "I could
        not find anything that resembles a device here" is ignored."""
        if not speaker_id:
            # Every downstream check keys off this id. Passing None through
            # reached has_voice(None)/_offer_pin_fallback(None) and failed
            # somewhere deep with a confusing message instead of here.
            await self._io.write("  No speaker identified -- enroll a voice first.")
            return False
        device_names = {d.get("name", "") for d in self.dm.devices.values() if d.get("name")}
        chunks = split_commands(text, known_rooms=self.dm.rooms,
                                known_devices=device_names)
        if not chunks:
            return False  # nothing recognized as a command at all

        # ONE utterance, ONE voice decision -- taken here, before any chunk
        # runs, so a split command cannot be half-authorised and the counter
        # advances once. Secure targets are still escalated individually
        # inside _carry_out.
        with timed("voice"):
            passed = await self._voice_gate(speaker_id, audio)
        if not passed:
            await self._io.write("  (command blocked — voice/PIN verification failed)")
            return False

        if len(chunks) == 1:
            return await self._run_command(chunks[0], speaker_id, audio=audio)
        event("CMD", f"split into {len(chunks)}: " + " | ".join(repr(c) for c in chunks))
        # ONE SNAPSHOT FOR THE WHOLE SENTENCE -- taken before its first
        # command that is not itself an undo. It was taken here, up front,
        # so "undo that and turn on the fan" replaced the very snapshot its
        # first half was about to put back, and undid nothing.
        snapped = False

        done: List[str] = []
        not_done: List[str] = []
        ignored: List[str] = []
        for chunk in chunks:
            if not snapped and not self._is_undo(chunk):
                self.dm.snapshot(speaker_id)
                snapped = True
            self._last_no_match = False
            ok = await self._run_command(chunk, speaker_id,
                                         _skip_snapshot=True, audio=audio)
            if ok:
                done.append(chunk)
            elif self._last_no_match:
                ignored.append(chunk)
            else:
                not_done.append(chunk)

        # SAY WHICH, rather than collapsing it to one word. "Two commands, one
        # failed -> False" tells the person their sentence did not work while
        # half of it did, and leaves them to guess which half. They asked for
        # two things; they are owed two answers.
        for chunk in ignored:
            event("CMD", f"{chunk!r} matched no device -- ignoring "
                         f"(likely mis-transcription)")
        if done or not_done:
            if done:
                await self._io.write("  Done: " + "; ".join(done))
            if not_done:
                await self._io.write("  NOT done: " + "; ".join(not_done),
                                     level="warn")
            if ignored:
                await self._io.write("  Ignored (not a command): "
                                     + "; ".join(ignored))

        # The bool remains the ANSWER TO "did the utterance accomplish
        # something", which is what the caller reports and what the periodic
        # counter keys off. The detail above is for the person; this is for
        # the machine.
        # Kept for the caller's result frame, so a client can show which part
        # of a split command worked without re-deriving it from the log.
        self.last_multi_outcome = {"done": done, "not_done": not_done,
                                   "ignored": ignored}
        return bool(done) and not not_done

    async def menu_issue_command(self):
        await self._io.write("\n--- ISSUE COMMAND ---")
        text, audio = await self._prompt_command_audio_and_text()
        if not text.strip():
            await self._io.write("  (empty command, cancelled)")
            return
        await self._run_multi(text, self.current_speaker_id, audio=audio)

    async def process_natural_command(self, text: str):
        # `text` here is whatever was typed at the main "or type a command"
        # prompt -- still supported with no audio at all, for quick testing
        # of resolution logic without touching the voice-security path.
        await self._run_multi(text, self.current_speaker_id, audio=None)

    # ── Enrollment & PIN (voice security) ───────────────────

    #: Consecutive unusable recordings tolerated per enrollment before giving
    #: up. The retry loop had no counter, so a client answering every
    #: record_required with an empty frame spun forever -- re-prompting into
    #: the void while holding the shell lock. Value from config.
    MAX_ENROLL_RETRIES = config_MAX_ENROLL_RETRIES

    async def menu_enroll_user(self, *, manage_view: bool = True,
                               sponsor: Optional[str] = None) -> bool:
        """Enroll a user's voice, one recording per phrase. Returns True on
        success.

        `manage_view=False` leaves the client on the enrollment screen when
        this returns, for callers that have more setup to do (first-run does
        PIN + recovery phrase next, and those prompts used to arrive on the
        command screen with the mic and send button live -- the exact problem
        set_view("enroll") was added to fix, one call later).

        Backend-agnostic: TerminalIO asks for a WAV path, WebSocketIO records
        from the phone's microphone. That second half is the point -- enrolling
        from desktop WAVs while every probe arrives from a phone guarantees a
        mic/codec mismatch no threshold can compensate for.
        """
        event("ENROLL", "enrollment flow started")
        await self._io.write("\n--- ENROLL VOICE ---")
        if self.sv.extractor is None:
            event("ENROLL", "ABORT: speaker-verification model not loaded")
            await self._io.write(
                "Speaker-verification model not loaded -- enrollment cannot run. "
                "Place the TitaNet model where config.SHERPA_SV_MODEL expects it, "
                "then restart the server.", level="warn")
            return False

        # Switch the view BEFORE asking anything. The user id was previously
        # requested while the client was still on the command screen, so the
        # first question of enrollment arrived with the microphone and send
        # button live -- and pressing send there got the command refused,
        # which closed the very prompt enrollment was blocked on.
        await self._io.set_view("enroll")
        # The session holds every phrase EMBEDDING while the ceremony runs;
        # nothing reaches the biometric STORE until the final
        # advance_phrase reports success. These two names let the finally
        # below guarantee that an abandoned session -- however it was
        # abandoned -- is dropped: the cancel/failure paths already cleaned
        # up, but a hard disconnect (ConnectionClosed mid-recording) raised
        # PAST those handlers and left the half-built embeddings sitting in
        # enrollment.sessions until the process restarted.
        session_key = None
        voice_stored = False
        # WHO IS DOING THE ADDING, read BEFORE the ceremony can move it.
        # A successful enrolment sets current_speaker_id to the NEW person,
        # so asking afterwards would record everyone as their own sponsor --
        # the one answer the journal must never give.
        #
        # A CALLER MAY NAME THE SPONSOR INSTEAD (2026-09-12). menu_add_person
        # blanks current_speaker_id before the ceremony -- deliberately, so
        # the "already taken" check refuses every existing name including the
        # sponsor's own -- and that would otherwise cost the journal the one
        # fact it exists to hold: who let this person in.
        sponsor = (sponsor if sponsor is not None
                   else (self.current_speaker_id or "")).strip()
        try:
            # THE SAME RULE AS THE RENAME DOOR, from the one place that
            # states it (2026-09-14). These were two separate literals of
            # the same characters, and both refused a space -- so a person
            # could not be enrolled under their own name here either. The
            # rule now lives in hv_common.person_name_problem, which says
            # why a space is safe and what is still refused.
            user_id = normalise_person_name(
                await self._io.get_input("  Name to enroll: "))
            if not user_id:
                await self._io.write("Empty name -- enrollment cancelled.",
                                     level="warn")
                return False
            problem = person_name_problem(user_id)
            if problem:
                await self._io.write(f"  {problem}")
                self.failure_note = problem
                return False
            # A NAME THAT EXISTS BELONGS TO SOMEBODY. save_voice overwrites,
            # so enrolling under another person's name replaced their
            # voiceprint with yours (and, in the signup ceremony, their PIN
            # and recovery words too). Re-recording YOUR OWN voice stays
            # allowed -- that is what the Profile door is for.
            if (user_id in (self.biometric_store.list_users() or [])
                    and user_id != (self.current_speaker_id or "")):
                await self._io.write(
                    "  That name is already taken -- pick another, or log in "
                    "as them from a phone.", level="warn")
                self.failure_note = ("That name is already taken -- pick "
                                     "another, or log in as them from a phone.")
                return False
            # AND A NEW NAME IS NOT THIS DOOR'S BUSINESS OVER A SOCKET. With
            # somebody identified, `enroll_voice` is "re-record MY voice"
            # (MENU_ACTIONS says so); typing a fresh name here minted a
            # voice-only person -- no PIN, no recovery words, no way to log
            # in, and the name blocked for everyone -- which is exactly the
            # half-person menu_add_person exists to stop. The ceremony
            # (register_person) blanks current_speaker_id first, so it and
            # first-run still pass; the hub's own keyboard is trusted as ever.
            me = (self.current_speaker_id or "").strip()
            if (me and user_id != me
                    and not self._io.trusted_local):
                self.failure_note = (
                    f"Enroll voice re-records your own voice ({me!r}). To add "
                    f"somebody else, use Add a person -- it sets their PIN "
                    f"and recovery words too.")
                await self._io.write(f"  {self.failure_note}", level="warn")
                return False

            # THE CEREMONY HAS BEGUN. Everything before this line is the
            # door (the name question, untouched); everything after it is
            # the ritual. register_person_persistently reads this to tell a
            # mid-ceremony cancel ("start me over") from a cancel at the
            # door ("let me out") -- the owner's cancel law, 2026-08-27.
            self._ceremony_begun = True
            session_key = f"enroll_{user_id}"
            self.enrollment.start_enrollment(session_key, user_id)
            phrases = self.enrollment.enrollment_phrases
            event("ENROLL", f"session {session_key!r} opened for user={user_id!r}, "
                            f"{len(phrases)} phrase(s) to record")
            await self._io.write(f"  You'll record {len(phrases)} short phrases.")

            retries = 0
            while True:
                cur = self.enrollment.sessions.get(session_key)
                if cur is None:
                    break
                idx = cur.current_phrase
                if not (0 <= idx < len(phrases)):
                    # The session index and the phrase list disagreeing is a
                    # bug in the handler, not something the user can fix --
                    # fail loudly here rather than on an IndexError below.
                    await self._io.write("  Enrollment state is inconsistent -- "
                                         "cancelled. Please start again.")
                    return False
                if retries > self.MAX_ENROLL_RETRIES:
                    await self._io.write("  Too many unusable recordings -- stopping. "
                                         "Check the microphone and start again.")
                    return False

                event("ENROLL", f"awaiting phrase {idx + 1}/{len(phrases)}: "
                                f"{phrases[idx]!r}")
                try:
                    audio = await self._io.capture_audio(
                        "Record yourself saying it",
                        phrase=phrases[idx],
                        index=idx + 1,
                        total=len(phrases),
                        context="enrollment",
                    )
                except CancelledByUser:
                    # A cancel IS a cancel -- clean up the session, then let
                    # it propagate so the caller gives it the cancel ending:
                    # run_menu_action re-raises into a proper 'cancelled'
                    # frame (Settings -> Enroll voice ends quietly instead
                    # of with a failure popup), and the registration
                    # ceremony's wrapper rolls back and RESTARTS at the
                    # name question (the owner's cancel law, 2026-08-27).
                    # The old shape returned False here, which dressed a
                    # deliberate cancel up as a failure.
                    event("ENROLL", f"CANCELLED by user at phrase {idx + 1}")
                    raise

                if audio is None:
                    retries += 1
                    event("ENROLL", f"phrase {idx + 1}: no usable audio "
                                    f"(retry {retries}/{self.MAX_ENROLL_RETRIES})")
                    await self._io.write("    No usable audio -- try that phrase again.")
                    continue
                event("AUDIO", f"enrollment phrase {idx + 1}/{len(phrases)} received: "
                               f"{describe_audio(audio)}")

                # receive_audio_array DROPS audio it judges too quiet and says
                # so in its return value. Ignoring that turned a precise
                # complaint ("you were too quiet") into the generic "problem
                # with that phrase" from advance_phrase below -- the one piece
                # of feedback that would actually change what the user does.
                # Off the event loop: this extracts an embedding.
                recv = await asyncio.to_thread(
                    self.enrollment.receive_audio_array, session_key, audio) or {}
                if recv.get("warning") == "audio_too_quiet":
                    event("ENROLL", f"phrase {idx + 1}: REJECTED (too quiet)")
                    await self._io.write("    Too quiet to use -- move closer to the mic "
                                         "and say it again, a little louder.")
                    retries += 1
                    continue
                if recv.get("type") == "error":
                    event("ENROLL", f"phrase {idx + 1}: REJECTED "
                                    f"({recv.get('reason', 'no reason given')})")
                    await self._io.write(f"    {recv.get('reason', 'could not accept that recording')}")
                    retries += 1
                    continue

                result = await asyncio.to_thread(self.enrollment.advance_phrase, session_key)

                if result.get("type") == "enroll_error":
                    event("ENROLL", f"phrase {idx + 1}: NOT ACCEPTED "
                                    f"({result.get('reason', result.get('instruction'))})")
                    await self._io.write(
                        f"    {result.get('instruction', 'Problem with that phrase, retry.')}")
                    retries += 1
                    continue

                if result.get("type") == "enroll_result":
                    if result.get("success"):
                        event("ENROLL", f"COMPLETE: user={user_id!r} enrolled from "
                                        f"{result.get('phrases_used')} phrase(s)")
                        await self._io.write(
                            f"\n  [OK] Enrolled '{user_id}' "
                            f"({result.get('phrases_used')} phrases).")
                        # FIRST WRITE WINS, so re-recording your own voice
                        # leaves your history alone. Written before
                        # current_speaker_id moves, using the sponsor read
                        # at the top of this method. Never fatal: a ledger
                        # that cannot be written must not undo an
                        # enrolment that succeeded.
                        try:
                            if self.people.record(user_id, sponsor):
                                event("ENROLL",
                                      f"journal: {user_id!r} added by "
                                      f"{sponsor or '(nobody -- first person)'}")
                        except Exception as exc:   # noqa: BLE001
                            event("ENROLL", f"WARNING: could not journal "
                                            f"{user_id!r}: {exc}")
                        self.current_speaker_id = user_id
                        # The ONE moment embeddings became a stored
                        # voiceprint -- the finally below must not touch
                        # the session that produced it.
                        voice_stored = True
                        return True
                    event("ENROLL", f"FAILED for user={user_id!r}: {result.get('reason')}")
                    await self._io.write(
                        f"Enrollment failed: {result.get('reason')}. "
                        f"{result.get('hint', '')}".strip(), level="warn")
                    # The dead session is dropped by the finally below. The
                    # shell outlives every connection, so abandoned sessions
                    # accumulated in it.
                    return False
        finally:
            # NO EXIT LEAVES EMBEDDINGS BEHIND -- the ONE place a session
            # is dropped, for every ending (cancel, failure, retry
            # exhaustion). Each of those used to drop it itself, and an
            # exception that is NOT CancelledByUser -- the socket dying
            # mid-recording is the live one -- unwound past every handler
            # with the session still holding its per-phrase embeddings.
            # They were never on disk (only a successful advance_phrase
            # writes the store), but "in memory until the
            # hub restarts" is not "nothing was saved" either. Guarded on
            # voice_stored so a COMPLETED enrollment's session is never
            # cancelled out from under the voiceprint it just produced.
            if (session_key and not voice_stored
                    and session_key in self.enrollment.sessions):
                try:
                    self.enrollment.cancel_enrollment(session_key)
                    event("ENROLL", f"session {session_key!r} dropped -- "
                                    f"embeddings discarded")
                except Exception:                  # noqa: BLE001
                    pass
            # Leave the enrollment screen ONLY if a voiceprint now exists.
            #
            # This used to fire unconditionally, so a failed or cancelled
            # attempt flipped the client to the command view -- a screen it
            # cannot use, since the reader refuses every command while nobody
            # is enrolled. _first_run_setup's retry question then arrived
            # against the wrong view, and declining it left the client bouncing
            # back to enroll with nothing pending. Staying put on failure is
            # both simpler and honest about the state the system is in.
            if manage_view and self.biometric_store.list_users():
                await self._io.set_view("command")
        return False

    #: How long a new-phone notice waits for an answer before it retires.
    NEW_PHONE_NOTICE_S = 7 * 86400

    def _new_phone_notices(self) -> List[Dict[str, Any]]:
        """One notice per phone paired in the last week that nobody has
        answered for -- never on that phone itself. Records from before
        2026-10-15 carry no `paired_via` and are not announced again."""
        try:
            from identity import phones
            records = phones.PhoneRegistry().list()
        except Exception:                              # noqa: BLE001
            return []
        mine = str(getattr(self, "current_phone_id", "") or "")
        now = time.time()
        out = []
        for r in records:
            pid = str(r.get("id") or "")
            via = r.get("paired_via") or ""
            if (not pid or pid == mine or not via or r.get("acknowledged_by")
                    or now - float(r.get("paired_at") or 0)
                    > self.NEW_PHONE_NOTICE_S):
                continue
            name = str(r.get("name") or "A phone")
            device = str(r.get("device") or "")
            label = f"{name} ({device})" if device else name
            when = time.strftime("%a %d %b, %H:%M",
                                 time.localtime(int(r.get("paired_at") or 0)))
            road = ("over Tailscale, from away" if via == "away"
                    else "on the home network")
            out.append(self._notice(
                f"new_phone:{pid}", f"New phone paired: {name}",
                f"{label} was paired with the house {road}, {when}. If it "
                f"is not someone in your house, remove it.",
                kind="menu", value=f"newphone:{pid}", label="Check it",
                now=True))
        return out

    async def menu_new_phone(self, pid: str) -> bool:
        """The new-phone notice's one door: is it ours? Keep it (the notice
        leaves every phone) or remove it (confirmed). Either way the
        asker's own check -- the same lock as Settings > Unpair a phone.
        The new phone itself may not vouch for itself."""
        from identity import phones
        registry = phones.PhoneRegistry()
        record = registry.get(pid)
        if record is None:
            await self._io.write("  That phone is no longer paired.")
            return True
        mine = str(getattr(self, "current_phone_id", "") or "")
        name = str(record.get("name") or pid[:8])
        device = str(record.get("device") or "")
        label = f"{name} ({device})" if device else name
        road = ("over Tailscale, from away"
                if record.get("paired_via") == "away" else "at home")
        options = [{"value": "remove", "label": "Remove it",
                    "sub": "it is refused from now on"}]
        if pid != mine:
            options.insert(0, {"value": "keep", "label": "It is ours, keep it",
                               "sub": "the alert leaves every phone"})
        picked = await self._io.choose(
            f"{label} was paired {road}. Is it someone in your house?",
            options)
        if not picked:
            raise CancelledByUser()
        if picked[0] == "keep":
            # Vouching for a phone is a security decision too: the asker's
            # own check, as removing is.
            if not await self._gate_for("Keep a new phone",
                                        self.current_speaker_id):
                return False
            who = self.current_speaker_id or "someone"
            await asyncio.to_thread(registry.acknowledge, pid, who)
            event("PAIR", f"new phone {pid[:8]} ({name}) kept by {who!r}")
            await self._io.write(f"  {name} stays paired.")
            return True
        sure = await self._io.choose(
            f"Remove {label}?",
            [{"value": "yes", "label": "Yes, remove it",
              "sub": "it must be paired again to come back"},
             {"value": "no", "label": "No, leave it", "sub": ""}])
        if not sure or sure[0] != "yes":
            raise CancelledByUser()
        if not await self._gate_for("Unpair a phone", self.current_speaker_id):
            return False
        if not await asyncio.to_thread(registry.remove, pid):
            await self._io.write("  It was already removed.")
            return True
        event("PAIR", f"new phone {pid[:8]} ({name}) removed from its notice")
        await self._io.write(f"  Removed {name}. It is refused from now on.")
        return True

    async def menu_pair_phone(self) -> bool:
        """Open the pairing window for one new phone -- the phone-reachable
        twin of `setup_manager.py --pair`.

        The window lives in a FILE (see phones.PairingWindow), which is the
        whole reason this works: this shell and ws_server are different
        processes, and both read the same expiry off disk. Closing is
        automatic -- PAIRING_WINDOW_SECONDS, or the moment one phone
        registers.
        """
        try:
            from identity import phones
            registry = phones.PhoneRegistry()
        except Exception as exc:                       # noqa: BLE001
            await self._io.write(f"  Could not open pairing: {exc}",
                                 level="warn")
            return False
        # WHERE IS THE NEW PHONE? (her design, 2026-10-15). At home is the
        # ordinary case; away -- over Tailscale -- is allowed for THIS
        # window only, because somebody chose it just now, behind the same
        # check that opens the window. No standing switch to forget on.
        where = await self._io.choose(
            "Where is the new phone?",
            [{"value": "home", "label": "Here at home",
              "sub": "on the house Wi-Fi"},
             {"value": "away", "label": "Away from home",
              "sub": "over Tailscale -- it must be on your Tailscale already"}])
        if not where:
            raise CancelledByUser()
        away = where[0] == "away"
        # NO ADDRESS THE NEW PHONE COULD USE: no countdown (2026-10-19).
        # The hub opens it, full length, when one appears.
        try:
            from app import setup_manager as _sm
            reachable = await asyncio.to_thread(_sm._reachable_road, away)
        except Exception:                              # noqa: BLE001
            reachable = True
        if not reachable:
            await asyncio.to_thread(
                registry.window.ask,
                opened_by=phones.PairingWindow.OPENED_BY_MENU, from_away=away)
            await self._io.write(
                "  The hub isn't on a network the new phone can use yet "
                "(hotspot off, no Wi-Fi). Pairing opens by itself as soon "
                "as it is.")
            return True
        # Off the loop: the window is a file written under a flock.
        until = await asyncio.to_thread(
            registry.window.open,
            opened_by=phones.PairingWindow.OPENED_BY_MENU, from_away=away)
        seconds = max(0, int(until - time.time()))
        # THE DOOR MUST BE VISIBLE WHILE IT IS OPEN. The initiating phone
        # gets the state frame immediately -- the countdown chip appears in
        # the same breath as the confirmation. Every OTHER connected phone
        # hears about it from ws_server's window watcher within a couple of
        # seconds, off the same file this just wrote; the terminal's
        # send_data is a no-op, which is correct -- the text lines above the
        # frame ARE its rendering of the same fact.
        await self._io.send_data("pair_window",
                                 {"open": True, "seconds": seconds,
                                  "opened_by":
                                      phones.PairingWindow.OPENED_BY_MENU,
                                  "from_away": away})
        await self._io.write(
            f"\n  Pairing is open for {seconds} seconds.")
        await self._io.write(
            "  On the new phone: open the app over Tailscale and enter the "
            "hub key when it asks. The window closes as soon as one phone "
            "joins." if away else
            "  On the new phone: join this hub's Wi-Fi, open the app, and "
            "enter the hub key when it asks. The window closes as soon as "
            "one phone joins.")
        event("PAIR", f"window opened from a phone (gated menu action"
                      f"{', for a phone away' if away else ''})")
        return True

    async def menu_setup_pin(self, user_id: Optional[str] = None, *,
                             strict_ack: bool = False) -> bool:
        """Set a PIN and show the one-time recovery phrase.

        `strict_ack=True` is the registration ceremony's setting: the
        recovery words must actually be acknowledged for this to count as
        done -- see the transaction law in register_person.

        THE RULES ARE IN THE PROMPT. A PIN outside PIN_MIN/MAX_LENGTH used
        to fail with a vague one-liner AFTER the fact -- the person walked
        away believing their PIN was set (and then got denied everywhere,
        with no recovery words on file). Now the length is stated up front,
        a bad entry says exactly what was wrong and re-asks, and only a
        stored PIN reports success."""
        await self._io.write("\n--- SET PIN ---")
        me = (self.current_speaker_id or "").strip()
        asked_from_menu = user_id is None
        if user_id is None:
            # YOUR OWN PIN, over the network. The "User ID:" question let
            # any phone set anybody's PIN (and void their recovery words);
            # only the hub's own keyboard may still name someone else.
            if self._io.trusted_local:
                user_id = (await self._io.get_input(
                    f"  User ID{f' [{me}]' if me else ''}: ")).strip() or me
            else:
                user_id = me
        if not user_id:
            await self._io.write("  Empty user ID -- cancelled.")
            self.failure_note = ("Nobody is identified on this connection, "
                                 "so there is no PIN to set.")
            return False
        if user_id != me and user_id in (self.biometric_store.list_users() or []):
            await self._io.write(
                f"  {user_id!r} is somebody else -- only they can change "
                f"their PIN (Forgot PIN uses their recovery words).",
                level="warn")
            self.failure_note = "Only a person can change their own PIN."
            return False
        # CHANGING A PIN STARTS WITH THE OLD ONE (her flow, 2026-09-30:
        # "write old pin, if forgotten use recovery words, then new pin").
        # Only for YOUR OWN PIN from a phone (asked == None and not the
        # hub's keyboard); registration passes user_id and sets a first PIN.
        if asked_from_menu and not self._io.trusted_local:
            has_pin = await asyncio.to_thread(
                lambda: self.fallback.is_enrolled(user_id).get("pin"))
            if has_pin:
                proof = await self._prove_current_pin(user_id, "Change PIN")
                if proof == "recovered":
                    return True          # the recovery flow set the new PIN
                if proof != "pin":
                    return False
            elif not await self._gate_for("Set PIN", user_id):
                # No PIN yet: nothing old to prove, so the voice check
                # stands in (what this action was always gated by).
                return False
        length_rule = (f"exactly {PIN_MAX_LENGTH}"
                       if PIN_MIN_LENGTH == PIN_MAX_LENGTH
                       else f"{PIN_MIN_LENGTH}-{PIN_MAX_LENGTH}")
        for _ in range(3):
            pin = (await self._io.get_input(
                f"  New PIN for '{user_id}' "
                f"({length_rule} digits): ",
                secret=True)).strip()
            if not pin.isdigit():
                await self._io.write("  Digits only -- try again.", level="warn")
                continue
            if not (PIN_MIN_LENGTH <= len(pin) <= PIN_MAX_LENGTH):
                await self._io.write(
                    f"  That's {len(pin)} digit(s) -- a PIN must be "
                    f"{length_rule}. Try again.",
                    level="warn")
                continue
            # bcrypt at 12 rounds is a quarter of a second; off the loop.
            ok, recovery = await asyncio.to_thread(
                self.fallback.set_pin, user_id, pin)
            if not ok:
                self.failure_note = ("PIN not set -- the hub could not hash "
                                     "or save it (bcrypt missing, or the PIN "
                                     "store is unwritable).")
                await self._io.write(f"  {self.failure_note}", level="warn")
                return False
            await self._io.write(f"  [OK] PIN set for '{user_id}'.")
            if not recovery:
                # (True, None): the PIN is set but pin_auth could not mint
                # words -- the BIP-39 list is missing. Said, not skipped.
                await self._io.write(
                    "  No recovery phrase could be issued (the BIP-39 "
                    "wordlist is missing on the hub). Install it and set "
                    "the PIN again to get one.", level="warn")
            else:
                # Shown exactly once; cannot be regenerated. It must reach
                # the person, not the server console.
                await self._io.write(
                    "RECOVERY PHRASE -- WRITE THIS DOWN NOW.\n"
                    "It is shown ONCE and cannot be recovered if lost.\n"
                    "Enter these words IN ORDER to reset a forgotten PIN:\n\n"
                    "    " + "  ".join(recovery),
                    level="important")
                # ainput, NOT get_input: get_input REJECTS an empty answer,
                # so "press Enter" was a promise the loop refused to keep --
                # any reply (or none) acknowledges. CancelledByUser included:
                # someone reading the words past the prompt timeout must not
                # have their ALREADY-SET PIN reported as cancelled.
                try:
                    await self._io.ainput(
                        "\n  Type OK once you've written it down: ",
                        patient=True)
                except (EOFError, KeyboardInterrupt, CancelledByUser):
                    if strict_ack:
                        # REGISTRATION IS ONE TRANSACTION (2026-08-27):
                        # inside the ceremony, walking away from the words
                        # is walking away from the registration -- the
                        # caller rolls everything back and the next attempt
                        # mints a FRESH phrase, so the words a person was
                        # shown are always the words that work. Outside the
                        # ceremony (Settings -> Set PIN for an existing
                        # user) the old mercy stands: the PIN is already
                        # set, and a timeout must not report it cancelled.
                        await self._io.write(
                            "  The words were not acknowledged -- the "
                            "registration is not complete.", level="warn")
                        return False
            return True
        await self._io.write("  PIN not set -- three invalid attempts.",
                             level="warn")
        return False

    # ── Automations: the four doors ─────────────────────────────────
    #
    # An automation is a TRIGGER stapled onto a SCENE (or one device
    # action, or a notification). The engine lives in automations.py and
    # runs on the hub's MQTT; these flows only author the rules -- same
    # ribbon shape as every other door: back-outs cancel, refusals carry
    # failure_note, and only a true completion earns the Done window.

    def _require_automations(self):
        """The engine, or a refusal that names the fix."""
        if self.automations is None:
            self.failure_note = (
                "Automations are unavailable on this hub -- automations.py "
                "is not in place (its home is the nlu folder, beside "
                "scene_manager.py).")
            return None
        return self.automations

    def _dev_name(self, did: str) -> str:
        return self.dm.devices.get(did, {}).get("name") or did

    async def _choose_or_cancel(self, title: str,
                                options: List[Dict[str, Any]]) -> str:
        """The one value a single-choice question came back with. Backing
        out is a cancel -- the shape every question flow below repeated as
        choose / `if not picked: raise CancelledByUser()` / `picked[0]`."""
        got = await self._io.choose(title, options)
        if not got:
            raise CancelledByUser()
        return got[0]

    async def _pick_rule_device(self, dids: List[str], title: str) -> str:
        return await self._choose_or_cancel(title, [
            {"value": d, "label": self._dev_name(d), "sub": d}
            for d in dids])

    async def _ask_hhmm(self, prompt: str) -> str:
        while True:
            raw = (await self._io.get_input(prompt)).strip()
            m = re.fullmatch(r"(\d{1,2}):(\d{2})", raw)
            if m and int(m.group(1)) < 24 and int(m.group(2)) < 60:
                return f"{int(m.group(1)):02d}:{m.group(2)}"
            await self._io.write("  As HH:MM -- 22:30, 07:00.", level="warn")

    async def _ask_minutes(self, prompt: str) -> int:
        while True:
            raw = (await self._io.get_input(prompt)).strip()
            if raw.isdigit() and int(raw) > 0:
                return int(raw) * 60
            await self._io.write("  A whole number of minutes.", level="warn")

    async def _ask_nonempty(self, prompt: str, what: str) -> str:
        """A free-text answer that must say something. The terminal wizard
        used to take an empty last answer and find out at the very end that
        the rule was refused -- throwing away every question before it.
        Asked again here, where the person still has the context."""
        while True:
            raw = (await self._io.get_input(prompt)).strip()
            if means_cancel(raw):
                raise CancelledByUser()
            if raw:
                return raw
            await self._io.write(f"  {what} can't be empty.", level="warn")

    async def _ask_override(self) -> Optional[Dict[str, Any]]:
        """"If I command it myself" -- who wins, for ONE rule.

        None = the house setting (what every rule did before this existed).
        The time can be given in seconds, minutes, hours or days."""
        mode = await self._choose_or_cancel(
            "If you switch the device yourself, who wins?", [
                {"value": "default", "label": "The house setting",
                 "sub": "you win for a short while (a minute, unless "
                        "changed); time-of-day rules ignore it"},
                {"value": "window", "label": "I win for a time I choose",
                 "sub": "the rule leaves that device alone for that long "
                        "after your last command"},
                {"value": "always", "label": "The automation always wins",
                 "sub": "your own command never holds it back"}])
        if mode == "default":
            return None
        if mode == "always":
            return {"mode": "always"}
        while True:
            raw = (await self._io.get_input(
                "  For how long? (a number) ")).strip()
            if means_cancel(raw):
                raise CancelledByUser()
            try:
                value = float(raw)
            except ValueError:
                value = float("nan")
            if math.isfinite(value) and value > 0:
                break
            await self._io.write("  A number above zero.", level="warn")
        unit = await self._choose_or_cancel("In what unit?", [
            {"value": "seconds", "label": "Seconds", "sub": ""},
            {"value": "minutes", "label": "Minutes", "sub": ""},
            {"value": "hours", "label": "Hours", "sub": ""},
            {"value": "days", "label": "Days", "sub": "up to 30"}])
        return {"mode": "window",
                "value": int(value) if value == int(value) else value,
                "unit": unit}

    async def _build_trigger(self, eng) -> Dict[str, Any]:
        caps = await asyncio.to_thread(eng.capabilities)
        options = []
        if caps["event"]:
            options.append({"value": "event", "label": "Something happens",
                            "sub": "motion, a press, a door, a switch "
                                   "changing"})
        if caps["reading"]:
            options.append({"value": "reading",
                            "label": "A reading crosses a line",
                            "sub": "power from an energy meter"})
        options.append({"value": "time", "label": "A time of day",
                        "sub": "every day at the same minute"})
        kind = await self._choose_or_cancel("What starts it?", options)
        if kind == "time":
            return {"kind": "time", "at": await self._ask_hhmm(
                "  At what time? (HH:MM): ")}
        if kind == "reading":
            did = await self._pick_rule_device(caps["reading"],
                                               "Which meter?")
            side = await self._choose_or_cancel(
                "Which way?",
                [{"value": "above", "label": "Rises above a value",
                  "sub": "something switched on, or left running"},
                 {"value": "below", "label": "Falls below a value",
                  "sub": "something switched off, or unplugged"}])
            while True:
                raw = (await self._io.get_input("  How many watts? ")).strip()
                try:
                    watts = float(raw)
                except ValueError:
                    watts = float("nan")
                if math.isfinite(watts):
                    break
                await self._io.write("  A number of watts.", level="warn")
            return {"kind": "threshold", "device": did, "key": "power",
                    side: watts}
        did = await self._pick_rule_device(caps["event"], "Which device?")
        shape = await self._choose_or_cancel(
            "When, exactly?",
            [{"value": "moment", "label": "The moment it happens",
              "sub": "motion seen, button pressed, switch on"},
             {"value": "stays", "label": "After it has stayed on a while",
              "sub": "a heater still running, a door still open"},
             {"value": "quiet", "label": "After it has been quiet a while",
              "sub": "no motion for N minutes -- the empty room"}])
        if shape == "moment":
            return {"kind": "event", "device": did, "value": "on"}
        for_s = await self._ask_minutes("  For how many minutes? ")
        return {"kind": "persist" if shape == "stays" else "absent",
                "device": did, "value": "on", "for_s": for_s}

    async def _build_condition(self) -> Optional[Dict[str, Any]]:
        picked = await self._choose_or_cancel(
            "When may it act?",
            [{"value": "any", "label": "Any time", "sub": ""},
             {"value": "between", "label": "Only between two times",
              "sub": "for example only after dark"}])
        if picked == "any":
            return None
        after = await self._ask_hhmm("  From (HH:MM): ")
        before = await self._ask_hhmm("  Until (HH:MM): ")
        return {"kind": "time_range", "after": after, "before": before}

    #: THE THREE WAYS A LINE CAN HOLD THE GLASS, worded once and offered
    #: everywhere -- by the same law as `set_kinds` and `else_kinds`: the
    #: wording lives beside the engine that honours it, not in the client.
    #:
    #: `for_s` is the field, and 0 is not "no time" but "until something
    #: else is shown". The middle option is the cycle: the line holds for
    #: config.DISPLAY_HOLD_SECONDS, the clock gets its
    #: DISPLAY_CLOCK_SECONDS, and the line comes back re-rendered, round
    #: and round. The first is the one that opts OUT of that.
    HOLD_KINDS = [
        {"value": "keep", "label": "Leave it up",
         "for_s": 0,
         "sub": "until another rule writes to the panel -- the clock "
                "never takes it back"},
        {"value": "cycle", "label": "Share it with the clock",
         "for_s": None,
         "sub": "the line, then a few seconds of the time and date, then "
                "the line again"},
        {"value": "once", "label": "For a set time",
         "for_s": "minutes",
         "sub": "show it for a while, then hand the panel back"},
    ]

    def _rule_panels(self) -> List[Dict[str, Any]]:
        """Every panel a rule could write to: [{did, name, rows}].

        Read off the same `display_targets` the engine writes through, so
        the list offered and the list honoured are one fact. Empty -- and
        therefore no display option anywhere -- on a house with no screen.
        """
        out: List[Dict[str, Any]] = []
        try:
            for panel in (self.esp.display_targets() or []):
                slug, name = panel.get("slug", ""), panel.get("name", "")
                if not (slug and name):
                    continue
                did = self.dm.board_device_id(slug, name)
                # The REGISTERED name, else the panel's own -- never the
                # id, which is what _dev_name falls back to (so the `or`
                # this replaced could never reach the panel's name).
                out.append({"did": did,
                            "name": (self.dm.devices.get(did) or {}).get("name") or name,
                            "rows": int(panel.get("rows") or 2)})
        except Exception:                          # noqa: BLE001
            return []
        return out

    async def _build_display_action(self, eng, panels) -> Dict[str, Any]:
        """The question flow's `display` action: which panel, which row,
        what it says, and how long it holds the glass."""
        if len(panels) == 1:
            panel = panels[0]
        else:
            got = await self._choose_or_cancel("Which panel?", [
                {"value": p["did"], "label": p["name"],
                 "sub": f"{p['rows']} rows"} for p in panels])
            panel = next(p for p in panels if p["did"] == got)

        # WHICH ROW, AND THE ONE THAT IS NOT A ROW. Omitting the row makes
        # the message own the panel -- every other row blanked -- which is
        # right for a warning and wrong for a dashboard, so it is offered
        # as its own choice rather than hidden in a blank answer.
        rows = [{"value": str(n), "label": f"Line {n}",
                 "sub": "this line only; other rules keep theirs"}
                for n in range(1, panel["rows"] + 1)]
        rows.append({"value": "whole", "label": "The whole panel",
                     "sub": "clears every other line -- for a warning"})
        got = await self._choose_or_cancel("Where on it?", rows)
        row = None if got == "whole" else int(got)

        # WHAT IT SAYS, and the readings it can quote. `_fill` substitutes
        # EVERY placeholder it finds, from any device -- so the hint says
        # so, because the single text box reads as one value per line and
        # is not (2026-09-14).
        quotable = []
        for did in sorted(self.dm.devices or {}):
            for key in eng.reading_keys(did):
                quotable.append(f"{{{did}:{key}}}")
        if quotable:
            await self._io.write(
                "  Readings you can put in the line: "
                + ", ".join(quotable[:8])
                + (" ..." if len(quotable) > 8 else ""))
            await self._io.write(
                "  As many as you like on one line -- "
                "'{p:power}W {p:voltage}V' shows both.")
        text = await self._ask_nonempty("  What should it show? ",
                                        "The line")

        got = await self._choose_or_cancel("How long should it stay?",
                                    self.HOLD_KINDS)
        action = {"kind": "display", "device": panel["did"], "text": text}
        if row is not None:
            action["row"] = row
        if got == "keep":
            action["for_s"] = 0
        elif got == "once":
            action["for_s"] = await self._ask_minutes(
                "  For how many minutes? ")
        # "cycle" leaves for_s off entirely, which is what makes the engine
        # use config.DISPLAY_HOLD_SECONDS -- one number, changed in one
        # place, rather than baked into every rule ever built.
        return action

    async def _build_action(self, eng) -> Dict[str, Any]:
        options = []
        scenes = (self.sm.list_scenes(user=self.current_speaker_id)
                  if self.sm else [])
        if scenes:
            options.append({"value": "scene", "label": "Run a scene",
                            "sub": "everything the scene sets, at once"})
        drivable = [d for d, rec in self.dm.devices.items()
                    if not rec.get("secure")
                    and rec.get("state_key", "on") in ("on", "level")]
        if drivable:
            options.append({"value": "device", "label": "Set one device",
                            "sub": "turn something on or off"})
        options.append({"value": "notify", "label": "Send a notification",
                        "sub": "a sticky note in Notifications, no action"})
        # THE FOURTH CHOICE, AND WHY IT WAS MISSING (2026-09-14: "pick per
        # rule?").
        #
        # The engine has had a `display` action since 2026-09-01. It
        # validates, it addresses a row, it holds the glass, and `for_s: 0`
        # means "leave it up". NOTHING IN THIS FLOW OFFERED IT -- the list
        # above is scene, device, notify, and that is all it has ever been
        # -- so every one of those capabilities was reachable only from the
        # phone's block builder, and the HOLD was reachable from nowhere at
        # all: not here, not in _builder_payload, not in any question.
        #
        # This is the flip bug again, thirty lines below, in the same
        # method, written down at the time: "A capability with no door is
        # not a capability, it is a test that passes." Two of them in one
        # method is a pattern, not an accident -- the engine grows a
        # feature, the tests prove it, and the screen that would let a
        # person use it is the step nobody does.
        panels = self._rule_panels()
        if panels:
            options.append({"value": "display", "label": "Show it on a panel",
                            "sub": "a line on an LCD, with a reading in it"})
        kind = await self._choose_or_cancel("What should it do?", options)
        if kind == "notify":
            text = await self._ask_nonempty("  What should it say? ",
                                            "The message")
            return {"kind": "notify", "text": text}
        if kind == "display":
            return await self._build_display_action(eng, panels)
        if kind == "scene":
            got = await self._choose_or_cancel("Which scene?", [
                {"value": s.name, "label": s.name,
                 "sub": f"{len(s.actions)} action(s)"} for s in scenes])
            return {"kind": "scene", "scene": got}
        did = await self._pick_rule_device(sorted(drivable), "Which device?")
        # THE THIRD CHOICE, AND WHY IT WAS MISSING (2026-09-13: "pir
        # automations is not done yet...its not ding anyting").
        #
        # The engine grew a flip on 12 Sep -- `{"on": TOGGLE}`, tested,
        # refused on locks, resolved to a real True/False before anything
        # else reads it. And then NOTHING OFFERED IT. A capability with no
        # door is not a capability, it is a test that passes. She asked for
        # "if on and senses change..if off vice versa", was told it was
        # built, and found two buttons in the app that both name a
        # direction.
        from nlu.automations import TOGGLE
        turn = await self._choose_or_cancel(
            f"What happens to {self._dev_name(did)}?",
            [{"value": "on", "label": "Turn it on", "sub": ""},
             {"value": "off", "label": "Turn it off", "sub": ""},
             {"value": "flip", "label": "Flip it",
              "sub": "the opposite of however it is when this fires"}])
        if turn == "flip":
            # THE STRING SENTINEL, CARRIED WHOLE. `bool("toggle")` is True,
            # so anything that coerced it on the way through would silently
            # mean "turn it on" -- which on a cover is "open the gate". The
            # engine is the only thing allowed to resolve it, and it does
            # that against the device's state at the moment of firing.
            return {"kind": "device", "device": did, "set": {"on": TOGGLE}}
        return {"kind": "device", "device": did,
                "set": {"on": turn == "on"}}

    #: Actions whose name carries their subject: "<verb>:<id>". Each maps
    #: to one of OUR methods and to the fixed action whose gating applies
    #: (None means the action carries its own proof -- an approval is
    #: answered with a PIN, which is a stronger check than the voice gate,
    #: not a weaker one).
    ARG_ACTIONS = {
        # A device used from its page: "<id>|<what>|<value>". Ungated for an
        # ordinary device (a lamp is not a PIN matter); a SECURE one always
        # asks, inside menu_use_device.
        "usedevice": ("menu_use_device", None),
        # The main meter, "<id>|<key>|<power|current>" or "none", and
        # forgetting what one device draws: ungated, they drive nothing.
        "mainmeter": ("menu_main_meter", None),
        "forgetdraw": ("menu_forget_draw", None),
        # Checking a card reader: ungated, it only asks the board and
        # listens for a tap.
        "checkreader": ("menu_check_reader", None),
        "approve": ("menu_approve_request", None),
        "pause":   ("menu_pause_automation", "update_automation"),
        "drop":    ("menu_drop_automation", "remove_automation"),
        # Scenes, by the same law and with the same gating authority: a
        # tap on a row edits or removes exactly the scene under the
        # finger, checked as `update_scene`/`remove_scene` would be. The
        # picker wizards stay -- they are how VOICE reaches the same
        # capability, and a spoken request has nothing to point at.
        "editscene": ("menu_edit_scene", "update_scene"),
        "dropscene": ("menu_drop_scene", "remove_scene"),
        # Devices, last of the three merges. `gate_as` is what keeps this
        # honest: editing one device by id is checked EXACTLY as
        # `update_device` is, and dropping one exactly as `remove_device`.
        # The tap is a shortcut through the PICKER, never around the GATE.
        "editdev": ("menu_edit_device", "update_device"),
        "dropdev": ("menu_drop_device", "remove_device"),
        # AND BOARDS, last of all, because the Devices screen shows them
        # too and the merge had stopped one row short. Retiring the board
        # under your finger is checked EXACTLY as `remove_device` is --
        # the tap is a shortcut through the PICKER, never around the GATE,
        # and menu_retire_board still lists everything it is about to
        # delete before deleting any of it. This is not the standalone
        # "retire_board" action removed on 2026-08-25 ("the retire board,
        # remove it its not needed"): that one was a second door with no
        # subject, and this one has a slug in its hand.
        "dropboard": ("menu_retire_board", "remove_device"),
        # THE BOARD'S OWN SHEET (2026-09-11: "when boards are clicked in
        # devices, info about them is shown, its credentials gated by pin,
        # its drivers used and the devices connected"). Every board act the
        # hub already had behind "Update device -> A board" and Settings,
        # reachable with the slug under the finger instead of a picker:
        # its passwords, checked EXACTLY as show_esp_key is; its wiring,
        # as update_device is; its name, ungated as rename_board is; and
        # the over-the-air report, ungated as board_report is.
        # Saved layouts. Editing, renaming and deleting a PLAN change
        # nothing on any chip, so they carry no gate; putting one on a board
        # is an edit of that board and is checked as `update_device` is.
        "editlayout":   ("menu_edit_layout", None),
        "renamelayout": ("menu_rename_layout", None),
        "droplayout":   ("menu_drop_layout", None),
        "uselayout":    ("menu_use_layout", "update_device"),
        # A pending design or edit, from its notice: Continue or delete.
        "resumelayout": ("menu_resume_layout", "add_device"),
        "boardkey":    ("menu_show_esp_key", "show_esp_key"),
        "editboard":   ("menu_edit_board", "update_device"),
        "renameboard": ("menu_rename_board", "rename_board"),
        # Change board type lives on the board's sheet; it is checked as
        # editing a board is.
        "boardtype":      ("menu_board_type", "update_device"),
        "boardreport": ("menu_board_report", "board_report"),
        # UNGATED -- gate_as None. The only device action without a PIN,
        # because it is the only one that changes nothing but a screen.
        # See menu_show_on_home.
        "homeflag": ("menu_show_on_home", None),
        # A CAMERA'S FEED. gate_as None here because the gate is CONDITIONAL:
        # a camera registered as security-critical (the default for the
        # domain) is checked as a secure device is; one deliberately marked
        # not secure (a fish tank) opens on a tap. menu_view_camera does that
        # check itself with _gate_for, the one gating authority.
        "viewcam": ("menu_view_camera", None),
        # "Got it" on a letter (WHO MADE WHAT). Changes nothing in the
        # house: the letter is read.
        "seen": ("menu_seen_letter", None),
        # A person's row on the Profile screen. gate_as None because the
        # flow asks for the remover's OWN PIN itself -- the one check, and
        # never the recent-check window, for the strongest act there is.
        "removeperson": ("menu_remove_person", None),
        # Leaving yourself (keep / clear): your OWN PIN, asked inside.
        "leavehouse": ("menu_leave_house", None),
        # PAIRING (2026-10-15). The new-phone notice's door, gated inside
        # (keeping or removing: the asker's own check).
        "newphone": ("menu_new_phone", None),
    }

    async def menu_view_camera(self, did: str) -> bool:
        """Hand the app a live feed of one camera.

        THE FEED NEVER TOUCHES THE HUB. Home Assistant decodes whatever the
        camera speaks (RTSP, ONVIF, an ESP32-CAM's MJPEG) and serves it as
        MJPEG behind /api/camera_proxy_stream/<entity>, authorised by the
        camera's own rotating access token rather than the hub's bearer
        token -- so what the app receives can open ONE camera for a while
        and nothing else. The hub reads that token from the entity's
        attributes and sends an `open camera` frame; the app's WebView
        plays it (http only inside the house, https anywhere -- the same
        policy as every `open web`). When the token rotates the stream
        stops, and the app asks here again.
        """
        device = (self.dm.devices or {}).get(did)
        if device is None:
            self.failure_note = ("That camera is no longer here -- the list "
                                 "may be out of date.")
            return False
        entity = str(device.get("ha_entity_id") or "")
        name = device.get("name") or did
        if not entity.startswith("camera."):
            self.failure_note = f"{name} is not a camera."
            return False
        if self.ha_client is None:
            self.failure_note = ("Home Assistant is not connected, and the "
                                 "camera lives there.")
            return False
        # SECURE BY DEFAULT: a camera looks into the house. Checked exactly
        # as a gated menu action is -- once per sudo window, voice or PIN.
        if device.get("secure"):
            if not await self._gate_for(f"View {name}",
                                        self.current_speaker_id):
                return False
        state = await asyncio.to_thread(self.ha_client.get_state, entity)
        if not isinstance(state, dict):
            self.failure_note = (f"Home Assistant did not answer for {name}. "
                                 f"Is it running, and is the camera still "
                                 f"set up there?")
            return False
        attrs = state.get("attributes") or {}
        token = str(attrs.get("access_token") or "")
        if not token:
            self.failure_note = (f"Home Assistant gave no stream token for "
                                 f"{name} -- its camera integration may not "
                                 f"support streaming.")
            return False
        from urllib.parse import quote
        # AN ADDRESS THE PHONE CAN REACH, not the one this process uses:
        # HA is usually http://localhost:8123 from here, and a phone sent
        # "localhost" would talk to itself. HA's internal_url first, else
        # the hub's own binding hosts stand in for loopback.
        from core import config as _cfg
        hosts = [str(e.get("host") or "") for e in
                 _cfg.NETWORK_BINDINGS if isinstance(e, dict)]
        base = await asyncio.to_thread(self.ha_client.reachable_base_url, hosts)
        base = base.rstrip("/")
        ent = quote(entity, safe="")
        stream = f"{base}/api/camera_proxy_stream/{ent}?token={quote(token, safe='')}"
        snapshot = f"{base}/api/camera_proxy/{ent}?token={quote(token, safe='')}"
        await self._io.send_data("open", {
            "screen": "camera",
            "device_id": did,
            "title": name,
            "url": stream,
            "snapshot": snapshot,
            "state": str(state.get("state") or ""),
        })
        event("CAMERA", f"{name!r} ({entity}) opened for "
                        f"{self.current_speaker_id!r}")
        await self._io.write(f"  Showing {name}.")
        return True

    def _rule_by_id(self, rule_id: str) -> Tuple[Any, Optional[Dict[str, Any]]]:
        """(engine, rule) for a rule named by id, or (_, None) with the
        refusal already in failure_note -- the lookup the by-id doors share."""
        eng = self._require_automations()
        rule = eng.get(rule_id) if eng is not None else None
        if eng is not None and rule is None:
            self.failure_note = ("That automation is no longer here -- the "
                                 "list may be out of date.")
        return eng, rule

    def _stamp(self) -> Dict[str, Any]:
        """Who is changing a rule, and when -- see WHO MADE WHAT."""
        return {"changed_by": self.current_speaker_id or "",
                "changed_at": time.time()}

    async def menu_pause_automation(self, rule_id: str,
                                    checked: bool = False) -> bool:
        """Pause or resume one rule, from the list, without a wizard.

        THE LIST IS THE PLACE (her merge, 2026-08-27): automations used to
        be four drawer rows and are now one screen, so the small changes a
        person makes most often -- stop that for now, start it again --
        have to be a tap ON the thing, not a trip through a question flow
        that exists for the voice.
        """
        eng, rule = self._rule_by_id(rule_id)
        if rule is None:
            return False
        owner = str(rule.get("created_by") or "")
        if not checked and not await self._may_change(
                "automation", rule.get("name", rule_id), owner):
            return False
        running = rule.get("enabled", True)
        ok, why = await asyncio.to_thread(
            lambda: eng.update(rule["id"], enabled=not running, **self._stamp()))
        if not ok:
            self.failure_note = why
            return False
        await self._io.write(
            f"  [OK] {rule['name']} is "
            f"{'paused' if running else 'running'}.")
        self._tell_owner(owner, "automation", rule.get("name", rule_id),
                         "paused" if running else "resumed")
        return True

    async def menu_drop_automation(self, rule_id: str) -> bool:
        """Remove one rule, named by id rather than picked from a list.

        What is gone is the "which one?" question, which a tap already
        answered; the confirmation stays where the person is already
        looking at the rule they mean.
        """
        eng, rule = self._rule_by_id(rule_id)
        if rule is None:
            return False
        name = rule.get("name", rule_id)
        owner = str(rule.get("created_by") or "")
        if not await self._may_change("automation", name, owner):
            return False
        if not await asyncio.to_thread(eng.remove, rule["id"]):
            self.failure_note = f"{name} could not be removed."
            return False
        await self._io.write(f"  [OK] {name} is gone. Its devices are "
                             f"untouched -- only the habit was removed.")
        self._tell_owner(owner, "automation", name, "removed")
        return True

    async def menu_approve_request(self, approval_id: str) -> bool:
        """Answer one `ask` action -- the only road on which an automation
        touches a secure device in the unsafe direction.

        THE PIN IS ASKED HERE, AT THE MOMENT OF THE ACT. Not when the rule
        was written: that would prove who authored it, never who tripped
        the sensor three weeks later, and a cloned card or a curtain
        drifting past a PIR would open the door with nobody home. The
        automation carries the question; the person carries the authority.
        """
        eng = self._require_automations()
        if eng is None:
            return False
        pending = {a["id"]: a for a in eng.pending_approvals()}
        ask = pending.get(approval_id)
        if ask is None:
            await self._io.write(
                "  That request has already been answered, or it lapsed.",
                level="warn")
            self.failure_note = ("That approval is no longer open -- nothing "
                                 "was changed. Requests lapse quickly on "
                                 "purpose: a key must not be left in a door.")
            return False

        user = self.current_speaker_id
        if not user:
            self.failure_note = ("Nobody is identified on this connection, "
                                 "so this cannot be answered. The request "
                                 "is still open for someone who is.")
            return False

        await self._io.write(f"\n--- APPROVE: {ask['device_name']} ---")
        await self._io.write(f"  {ask['text']}")

        # ONLY AN EXPLICIT NO DECLINES. Every failure used to decline the
        # request, which let anyone who could not answer it -- wrong PIN,
        # cancel, an unidentified connection -- veto it for the person who
        # could. Now a cancel, a wrong PIN or a lockout leaves the question
        # open; the engine's own expiry (AUTOMATION_APPROVAL_SECONDS) is
        # what closes an unanswered one.
        answer = await self._io.choose(
            "Answer this request?",
            [{"value": "yes", "label": "Approve, with my PIN", "sub": ""},
             {"value": "no", "label": "Decline it", "sub": "the door stays as it is"}])
        if not answer:
            self.failure_note = "Not answered -- the request is still open."
            raise CancelledByUser()
        if answer[0] == "no":
            await asyncio.to_thread(eng.decline, approval_id)
            await self._io.write("  Declined -- nothing was changed.")
            return True

        # A closed PIN box comes back as (False, "Cancelled -- ..."):
        # _pin_tries turns the cancel into an answer, never an exception.
        proven, why = await self.confirm_with_pin(user, "approve")
        if not proven:
            self.failure_note = f"{why} The request is still open."
            return False

        # Off the loop: approving DRIVES the device (HA REST, or an MQTT
        # connect+publish), exactly the call _set_state threads.
        done, said = await asyncio.to_thread(eng.approve, approval_id)
        if not done:
            self.failure_note = said
            return False
        await self._io.write(f"  [OK] {said}.")
        return True

    def _builder_payload(self, eng,
                         rule: Optional[Dict[str, Any]] = None
                         ) -> Dict[str, Any]:
        """Everything the block builder needs, in one message.

        Her ruling (2026-08-27): the WHEN list is EVERY registered device,
        so `askable` carries them all -- with `heard` saying plainly which
        arrive over MQTT and which nothing can currently tell us about.
        Only boards can START a rule: Home Assistant is for adding and
        controlling integration devices, and has no event ear here.
        """
        from nlu.automations import TOGGLE as _TOGGLE
        from nlu import automations as _auto
        scenes = []
        if self.sm is not None:
            try:
                scenes = [{"value": sc.name, "label": sc.name}
                          for sc in sorted(self.sm.list_scenes(
                              user=self.current_speaker_id),
                              key=lambda sc: sc.name)]
            except Exception:                      # noqa: BLE001
                scenes = []
        # HOW TALL EACH PANEL IS, so the display action can offer a ROW
        # picker instead of a number to guess at. Read from the same
        # display_targets the engine writes through, keyed by device id.
        panel_rows = {p["did"]: p["rows"] for p in self._rule_panels()}

        targets = []
        for did, d in (self.dm.devices or {}).items():
            targets.append({
                "value": did,
                "label": d.get("name") or did,
                "room": d.get("room", ""),
                "secure": bool(d.get("secure")),
                "state_key": d.get("state_key", "on"),
                "range_min": d.get("range_min"),
                "range_max": d.get("range_max"),
                "step": d.get("step"),
                # THE READINGS THIS DEVICE OFFERS, so a display line can
                # quote ANY device rather than only the one that triggered
                # the rule -- and can be built by tapping rather than by
                # typing `{hall.dht11:temperature}` correctly from memory.
                # Empty for things that only take orders.
                #
                # reading_keys, NOT observed_keys: the WHEN dropdown right
                # above these chips asks the same question, and asking it
                # a second way put a contradiction on one screen -- "power
                # meter, current rises above" in the WHEN block over
                # "Nothing in the house is reporting a reading yet" under
                # the display line (2026-09-11). One source now.
                "readings": eng.reading_keys(did),
                # The controls that fit it, as on its page in Devices.
                "use": self._use_safe(did, d),
                # Present only on panels, and only then. The row picker
                # appears when this does.
                **({"rows": panel_rows[did]} if did in panel_rows else {}),
            })
        return {
            "devices": eng.askable(),
            "targets": sorted(targets, key=lambda t: t["label"]),
            "scenes": scenes,
            "rule": rule,
            # The two ELSE shapes, named for the screen so the wording of
            # the blocks lives in ONE place -- here, beside the engine that
            # honours them.
            "else_kinds": [
                {"value": "else", "label": "ELSE",
                 "sub": "when the IF does not hold"},
                {"value": "revert", "label": "ELSE AFTER quiet",
                 "sub": "undo it once things go quiet"},
            ],
            # WHAT A THEN CAN DO TO A DEVICE, by the same law as
            # `else_kinds` above: the wording lives beside the engine that
            # honours it, not in the client. Two of these are as old as the
            # builder; `flip` is the one the engine has been able to do
            # since 12 Sep with nothing anywhere offering it.
            #
            # `on` IS THE VALUE TO SEND, and for flip it is the string
            # sentinel rather than a bool -- a client that copies this
            # value straight into `set` is correct, and one that coerces it
            # to a bool turns a flip into "turn it on". That is why the
            # sentinel is a string and why it is spelled out here.
            #
            # A CLIENT THAT DOES NOT READ THIS FIELD simply draws the two
            # chips it always drew; nothing breaks, and the flip stays
            # reachable through the question flow in _build_action until
            # the app is next built.
            "set_kinds": [
                {"value": "on", "label": "Turn it on", "on": True,
                 "sub": ""},
                {"value": "off", "label": "Turn it off", "on": False,
                 "sub": ""},
                {"value": "flip", "label": "Flip it", "on": _TOGGLE,
                 "sub": "the opposite of however it is when this fires"},
            ],
            # HOW LONG A DISPLAY LINE HOLDS THE GLASS, by the same law as
            # the two lists above. The payload has carried `rows` for a
            # panel since 2026-09-01, so the app could always pick WHERE a
            # line goes -- and never HOW LONG it stays, because nothing
            # here said the choice existed. A rule built without it takes
            # the hub's default, which is the cycle.
            #
            # `for_s` says what to put in the action: 0 for "leave it up",
            # the field OMITTED for the cycle (so one config number governs
            # every rule that did not ask for something else), and a number
            # of seconds for a set time. A client that does not read this
            # field builds rules exactly as it does today.
            "hold_kinds": self.HOLD_KINDS,
            # The two numbers those choices mean, so the app can say "10
            # minutes, then 5 seconds of clock" rather than guess. Read
            # from the ENGINE, which is where config lands and where the
            # deadline is actually computed -- a second import of config
            # here would be a second place for these to disagree.
            "hold_default_s": float(_auto.DISPLAY_HOLD_SECONDS),
            "clock_share_s": float(_auto.DISPLAY_CLOCK_SECONDS),
        }

    def _use_safe(self, did: str, d: Dict[str, Any]) -> Dict[str, Any]:
        """_use_of, never raising (one odd device must not stop the builder)."""
        try:
            return self._use_of(did, d, d.get("state") or {})
        except Exception:                          # noqa: BLE001
            return {"kind": "none"}

    async def _rule_from_builder(self, eng, rule=None
                                 ) -> Optional[Dict[str, Any]]:
        """The blocks, if this client can draw them. None means it cannot,
        and the caller asks its questions instead -- the pin screen's rule
        exactly, so voice and the console lose nothing.

        TWO FAILURES THAT ARE NOT THE SAME, and this used to treat them
        alike. "This client has no block builder" is ordinary and silent:
        the terminal and a voice-only session walk the questions, as
        designed. "The payload could not be built" is a HUB BUG, and
        answering it by quietly walking the questions means the blocks
        vanish and the person is shown a completely different screen with
        no explanation anywhere they can see.

        That is what a mismatched pair of files did to her (2026-09-12).
        `main.py` was updated to ask `eng.reading_keys` and `automations.py`
        was not, so _builder_payload raised AttributeError, the blanket
        except below swallowed it, and Add automation went from the blocks
        to "A name for it..." followed by "What starts it?" -- the old
        question flow, which is a real and working path, which is exactly
        why nothing looked broken. The traceback went to the hub's console
        and nowhere near the phone.

        So the payload is built OUTSIDE the try now, and a failure there
        SAYS so -- in the log and in failure_note, which the phone shows.
        The questions still run, because half a flow beats none, but
        nobody has to guess why the screen changed.
        """
        try:
            payload = await asyncio.to_thread(self._builder_payload, eng, rule)
        except Exception as exc:                   # noqa: BLE001
            traceback.print_exc()
            event("AUTO", f"the block builder could not be prepared "
                          f"({type(exc).__name__}: {exc}) -- falling back to "
                          f"the question flow. THIS IS A HUB BUG, not a "
                          f"client without a builder.")
            self.failure_note = (
                "The block builder could not be prepared, so this is the "
                "question version instead. The hub's log has the reason -- "
                "the usual cause is main.py and nlu/automations.py being "
                "from different updates.")
            return None
        while True:
            try:
                built = await self._io.rule_builder(payload)
            except CancelledByUser:
                raise
            except Exception:                      # noqa: BLE001
                traceback.print_exc()
                return None
            if built is None:
                return None                        # no builder on this client
            if built.get("cancelled"):
                raise CancelledByUser()
            heard = built.get("sentence")
            if isinstance(heard, str) and heard.strip():
                # THE SENTENCE BOX: the boxes come back filled with what
                # could be read; what could not is listed for tapping.
                from app.rule_sentence import draft
                try:
                    got = await asyncio.to_thread(
                        draft, heard, askable=payload.get("devices") or {},
                        targets=payload.get("targets") or [],
                        scenes=payload.get("scenes") or [],
                        named=self._devices_named)
                except Exception as exc:           # noqa: BLE001
                    traceback.print_exc()
                    got = {"rule": {"name": heard[:48]},
                           "unsure": ["when", "do"],
                           "error": type(exc).__name__}
                filled = dict(got.get("rule") or {})
                if rule is not None and rule.get("id"):
                    filled["id"] = rule["id"]
                payload = dict(payload, rule=filled, heard=heard,
                               unsure=got.get("unsure") or [], tried=[])
                continue
            trial = built.get("try")
            if not isinstance(trial, dict):
                return built.get("rule") or None
            # TRY IT NOW: the THEN runs once, nothing is saved, and the
            # builder comes back as it was, with what happened.
            try:
                said = await asyncio.to_thread(eng.try_now, trial)
            except Exception as exc:               # noqa: BLE001
                traceback.print_exc()
                said = [f"it could not be tried ({type(exc).__name__})"]
            payload = dict(payload, rule=trial, tried=said,
                           editing=rule is not None)

    async def menu_add_automation(self) -> bool:
        await self._io.write("\n--- ADD AUTOMATION ---")
        eng = self._require_automations()
        if eng is None:
            return False

        # THE BLOCKS FIRST, the ribbon second. A client with a screen
        # builds the whole rule at once and sends it here already whole;
        # everything else (the terminal, a voice-only session) walks the
        # question flow below, which is unchanged.
        saved = await self._build_and_save(eng)
        if saved is not None:
            return saved

        name = await self._ask_nonempty(
            "  A name for it (what you'd call this habit): ", "The name")
        if eng.get(name):
            await self._io.write(f"  {name!r} already exists.", level="warn")
            self.failure_note = (f"An automation called {name!r} already "
                                 f"exists -- update it, or pick another "
                                 f"name.")
            return False
        trigger = await self._build_trigger(eng)
        condition = await self._build_condition()
        action = await self._build_action(eng)
        override = await self._ask_override()
        from nlu.automations import trigger_sentence
        rule = {"name": name, "trigger": trigger, "condition": condition,
                "action": action}
        if override:
            rule["override"] = override
        default = trigger_sentence(rule, self._dev_name)
        typed = (await self._io.ainput(
            f"  Describe it (Enter keeps: \"{default}\"): ")).strip()
        if means_cancel(typed):
            raise CancelledByUser()
        rule["description"] = typed or default
        rule["created_by"] = self.current_speaker_id or ""
        ok, why = await asyncio.to_thread(eng.add, rule)
        if not ok:
            await self._io.write(f"  {why}", level="warn")
            self.failure_note = why
            return False
        # the first rule may be what wakes the engine; off the loop, since
        # start() reads the broker account and builds its client.
        await asyncio.to_thread(eng.start)
        await self._io.write(f"  [OK] {name}: {default}.")
        return True

    async def _build_and_save(self, eng, rule: Optional[Dict[str, Any]] = None,
                              existing_id: str = "") -> Optional[bool]:
        """The blocks, then the save. None means this client draws no
        blocks, and the caller asks its questions instead.

        A RULE THE HUB REFUSES GOES BACK TO THE BLOCKS, as the person built
        it, with the reason said. Closing the builder on a refusal threw
        away minutes of work over one wrong field. The builder's own Cancel
        is the way out.
        """
        while True:
            built = await self._rule_from_builder(eng, rule)
            if built is None:
                return None
            if await self._save_built_rule(eng, built, existing_id):
                return True
            # `important`: the app shows it as a banner over the re-opened
            # blocks and says it aloud; a warn line only reaches the status.
            await self._io.write(f"  Not saved -- {self.failure_note.rstrip('.')}. "
                                 f"Change it and save again, or cancel.",
                                 level="important")
            # The id says "editing" to the app's builder title.
            rule = dict(built, id=existing_id) if existing_id else built

    async def _save_built_rule(self, eng, rule: Dict[str, Any],
                               existing_id: str = "") -> bool:
        """Validate, screen and store a rule that arrived whole.

        THE HUB IS STILL THE AUTHORITY. The builder composes; every law is
        applied here -- the vocabulary, the secure-device direction, the
        scene's existence. A refusal goes back as failure_note in the
        hub's own words, so the person is told WHY rather than "did not
        complete".
        """
        from nlu.automations import trigger_sentence, validate_rule
        # Checked BEFORE the sentence is worded: trigger_sentence reads the
        # trigger as a mapping, and a malformed one raised out of the flow.
        problem = validate_rule(rule)
        if problem:
            self.failure_note = problem
            return False
        rule = dict(rule)
        sentence = trigger_sentence(rule, self._dev_name)
        rule.setdefault("description", sentence)
        if existing_id:
            # The builder sends the WHOLE rule: an optional part it left
            # out was removed in the app, so clear it rather than keep the
            # stored one (update() treats None as "clear").
            # The builder never carries who made it: that stays as stored.
            from nlu.automations import OWNER_FIELDS
            changes = {k: v for k, v in rule.items()
                       if k != "id" and k not in OWNER_FIELDS}
            for optional in ("condition", "else_actions", "revert", "loop"):
                changes.setdefault(optional, None)
            changes.update(self._stamp())
            ok, why = await asyncio.to_thread(
                lambda: eng.update(existing_id, **changes))
        else:
            from nlu.automations import OWNER_FIELDS
            rule = {k: v for k, v in rule.items() if k not in OWNER_FIELDS}
            rule["created_by"] = self.current_speaker_id or ""
            ok, why = await asyncio.to_thread(eng.add, rule)
        if not ok:
            self.failure_note = why
            return False
        await asyncio.to_thread(eng.start)   # the first rule may wake it
        await self._io.write(f"  [OK] {rule['name']}: {sentence}.")
        return True

    async def _pick_automation(self, eng, verb: str) -> Dict[str, Any]:
        from nlu.automations import trigger_sentence
        rules = eng.rules
        if not rules:
            raise LookupError
        picked = await self._choose_or_cancel(f"{verb} which automation?", [
            {"value": r["id"],
             "label": r.get("name", r["id"]),
             "sub": trigger_sentence(r, self._dev_name)
                    + ("" if r.get("enabled", True) else "  (paused)")}
            for r in rules])
        return eng.get(picked)

    async def menu_list_automations(self) -> bool:
        eng = self._require_automations()
        if eng is None:
            return False
        from nlu.automations import trigger_sentence
        # Structured first, for the client with a real screen -- the same
        # two-paths rule list_devices follows. The text below stays the
        # terminal's rendering of exactly these facts.
        await self._io.send_data("automation_list", {"automations": [
            {"id": r.get("id", ""),
             "name": r.get("name", ""),
             "sentence": trigger_sentence(r, self._dev_name),
             "description": r.get("description", ""),
             "enabled": bool(r.get("enabled", True)),
             "fired_count": int(r.get("fired_count", 0)),
             "last_fired": r.get("last_fired"),
             "has_else": bool(r.get("else_actions")),
             "has_revert": bool((r.get("revert") or {}).get("actions")),
             # WHO MADE IT -- see WHO MADE WHAT.
             "by": self._maker_shown(str(r.get("created_by") or "")),
             "chip": self._maker_chip(str(r.get("created_by") or "")),
             "at": r.get("created_at"),
             "changed_by": self._maker_shown(str(r.get("changed_by") or "")),
             "changed_at": r.get("changed_at"),
             "rule": r}
            for r in eng.rules],
            "journal": [{"ts": ts, "name": name, "text": text}
                        for ts, name, text in list(eng.journal)[:20]]})

        await self._io.write("\n--- AUTOMATIONS ---")
        if not eng.rules:
            await self._io.write(
                "  None yet. Add automation builds one: a trigger, and a "
                "scene (or one device, or a notification) it runs.")
            return True
        for r in eng.rules:
            state = "on" if r.get("enabled", True) else "paused"
            fired = int(r.get("fired_count", 0))
            tail = f" -- fired {fired}x" if fired else ""
            await self._io.write(
                f"  {r.get('name')}: "
                f"{trigger_sentence(r, self._dev_name)} [{state}]{tail}")
        recent = list(eng.journal)[:5]
        if recent:
            await self._io.write("  Recently:")
            for _ts, name, text in recent:
                await self._io.write(f"    {name}: {text}")
        return True

    async def menu_update_automation(self) -> bool:
        await self._io.write("\n--- UPDATE AUTOMATION ---")
        eng = self._require_automations()
        if eng is None:
            return False
        try:
            rule = await self._pick_automation(eng, "Update")
        except LookupError:
            await self._io.write("  No automations yet.")
            self.failure_note = ("No automations exist yet -- Add "
                                 "automation is the way in.")
            return False

        owner = str(rule.get("created_by") or "")
        if not await self._may_change("automation", rule.get("name", ""),
                                      owner):
            return False

        # ONE BUILDER FOR ADD AND EDIT (her ruling): the chosen rule opens
        # as pre-filled blocks, so changing a trigger is moving a dropdown
        # rather than answering the whole ribbon again. The four-way
        # question below stays for the clients that cannot draw blocks.
        saved = await self._build_and_save(eng, rule, existing_id=rule["id"])
        if saved is not None:
            if saved:
                self._tell_owner(owner, "automation", rule.get("name", ""),
                                 "changed")
            return saved

        running = rule.get("enabled", True)
        what = await self._choose_or_cancel(
            f"{rule['name']} -- change what?",
            [{"value": "toggle",
              "label": "Pause it" if running else "Resume it", "sub": ""},
             {"value": "rename", "label": "Rename it", "sub": ""},
             {"value": "trigger", "label": "Change what starts it",
              "sub": ""},
             {"value": "action", "label": "Change what it does",
              "sub": ""},
             {"value": "override",
              "label": "Change who wins if I switch it myself",
              "sub": ""}])
        if what == "toggle":
            return await self.menu_pause_automation(rule["id"], checked=True)
        if what == "rename":
            new = await self._ask_nonempty("  New name: ", "The name")
            if eng.get(new):
                self.failure_note = f"{new!r} is already taken."
                await self._io.write(f"  {self.failure_note}", level="warn")
                return False
            changes: Dict[str, Any] = {"name": new}
        elif what == "trigger":
            changes = {"trigger": await self._build_trigger(eng)}
        elif what == "override":
            changes = {"override": await self._ask_override()}  # None = house
        else:
            changes = {"action": await self._build_action(eng)}
        changes.update(self._stamp())
        ok, why = await asyncio.to_thread(
            lambda: eng.update(rule["id"], **changes))
        if not ok:
            await self._io.write(f"  {why}", level="warn")
            self.failure_note = why
            return False
        await self._io.write(f"  [OK] {rule['name']} updated.")
        self._tell_owner(owner, "automation", rule.get("name", ""), "changed")
        return True

    async def menu_remove_automation(self) -> bool:
        await self._io.write("\n--- REMOVE AUTOMATION ---")
        eng = self._require_automations()
        if eng is None:
            return False
        try:
            rule = await self._pick_automation(eng, "Remove")
        except LookupError:
            await self._io.write("  No automations yet.")
            self.failure_note = "No automations exist -- nothing to remove."
            return False
        sure = await self._io.choose(
            f"Remove {rule['name']!r}?",
            [{"value": "yes", "label": "Yes, remove it",
              "sub": "the house stops doing this on its own"},
             {"value": "no", "label": "No, leave it", "sub": ""}])
        if not sure or sure[0] == "no":
            raise CancelledByUser()
        # The by-id door's removal, which checks what remove() answered:
        # this one printed "Removed" whatever the store said.
        return await self.menu_drop_automation(rule["id"])

    #: What a remote client may ask for by name, and whether doing it needs
    #: proof of identity first.
    #:
    #: `gated` runs the SAME check a secure device gets. On a terminal that is
    #: free -- _voice_gate treats "no audio" from a trusted_local backend as
    #: nothing to verify, because reaching the keyboard was the check. Over a
    #: socket it is not free: the gate falls through to the PIN, because a
    #: socket is not a person in the room.
    #:
    #: Why these are gated and the rest are not: add/remove/update a device can
    #: delete a LOCK from the registry, and set_pin / enroll_voice mint an
    #: identity that works from then on. Listing and statistics only read.
    #: forgot_pin is self-gated -- it demands the recovery phrase, and asking
    #: for a PIN to reset a forgotten PIN is a closed loop.
    MENU_ACTIONS: Dict[str, Any] = {
        "list_devices":  ("menu_list_devices",  False, "List devices"),
        # The app's "Statistics" door sends THIS. There is no separate
        # `statistics` action any more -- the one that existed counted
        # devices and read nothing.
        "readings":      ("menu_readings",      False, "Sensor readings"),
        "add_device":    ("menu_add_device",    True,  "Add device"),
        "update_device": ("menu_update_device", True,  "Update device"),
        "remove_device": ("menu_remove_device", True,  "Remove device"),
        "list_scenes":   ("menu_list_scenes",   False, "List scenes"),
        # SAVED LAYOUTS (Devices -> Layouts). Reading and designing a
        # plan change nothing physical, so they are ungated; putting one on
        # a board is gated as update_device (see ARG_ACTIONS).
        "list_layouts":  ("menu_list_layouts",  False, "Saved layouts"),
        "new_layout":    ("menu_new_layout",    False, "New layout"),
        "add_scene":     ("menu_add_scene",     True,  "Add scene"),
        "update_scene":  ("menu_update_scene",  True,  "Update scene"),
        "remove_scene":  ("menu_remove_scene",  True,  "Remove scene"),
        # Automations: gated like scenes -- they change what the house
        # DOES on its own, which is the strongest kind of change there is.
        # Listing only reads.
        "list_automations":  ("menu_list_automations",  False,
                              "List automations"),
        "add_automation":    ("menu_add_automation",    True,
                              "Add automation"),
        "update_automation": ("menu_update_automation", True,
                              "Update automation"),
        "remove_automation": ("menu_remove_automation", True,
                              "Remove automation"),
        # RE-RECORDING YOUR OWN VOICE. Voice and nothing else, which is
        # right: you already have a PIN and recovery words.
        "enroll_voice":  ("menu_enroll_user",   True,  "Enroll a voice"),
        # ADDING SOMEBODY ELSE, which is NOT the same act and used to send
        # the line above -- building a person with a voice, no PIN, and no
        # way in. See menu_add_person.
        "add_person":    ("menu_add_person",    True,  "Add a person"),
        # Gated like the voice it sits beside on the Profile screen. One
        # door for "my card" and "someone else's" -- the hub asks who.
        "enroll_card":   ("menu_enroll_card",   True,  "Enrol a card"),
        # Ungated HERE because the flow itself asks for the CURRENT PIN
        # (or the recovery words) before a new one -- a voice check in
        # front of that would be two locks on one door. A person with no
        # PIN yet still gets the voice check, inside menu_setup_pin.
        "set_pin":       ("menu_setup_pin",     False, "Set PIN"),
        # Five new recovery words behind the current PIN (the old words
        # cannot be shown: only their hash is kept).
        "new_recovery_words": ("menu_new_recovery", False,
                               "Recovery words"),
        # Profile. Read-only and UNGATED: it says a PIN exists, never what
        # it is, and the recovery words it reports on are held as HMACs --
        # nothing here could reveal them if it tried. Asking for a PIN to
        # be shown your own name would be theatre.
        "profile":       ("menu_profile",       False, "Profile"),
        # Gated, and of everything in this table it is the one that can
        # lock a person out of their own house -- see menu_rename_user for
        # the five stores a name is a key in.
        "rename_user":   ("menu_rename_user",   True,  "Change your name"),
        # Taking somebody OUT of the house. Not gated here: the flow
        # confirms, then asks for the remover's own PIN (see
        # menu_remove_person), and a voice check in front of that would be
        # two locks on one door.
        "remove_person": ("menu_remove_person", False, "Remove a person"),
        # Gated: opening the door for a new device is an act of household
        # authority, and the voice/PIN check the gate runs is what makes a
        # verified person at a phone as good as someone at the hub's
        # keyboard. This is how a headless Pi admits a second phone with
        # no SSH and no screen.
        "pair_phone":    ("menu_pair_phone",    True,  "Pair a new phone"),
        "forgot_pin":    ("menu_recover_pin",   False, "Forgot PIN"),
        # Gated: a learned code becomes a device that can be fired by voice.
        "learn_ir":      ("menu_learn_ir",      True,  "Learn IR codes"),
        # Gated: this describes a device that will end up controlling
        # something physical.
        "add_esp_node":  ("menu_add_esp_node",  True,  "Add ESP node"),
        # "retire_board" IS DELIBERATELY GONE from this table (field test
        # 2026-08-25: "the retire board, remove it its not needed"). It
        # was the last surviving duplicate door: Remove device already
        # offers the full retirement when a board is picked, so a
        # standalone row only made the person decide between two words
        # for one act. menu_retire_board itself STAYS -- the Remove
        # device path routes to it by slug.
        # Settings. Gated: one changes the network every board joins, the
        # other reveals a credential.
        "edit_board":    ("menu_edit_board",    True,  "Edit a board"),
        "resume_rewire": ("menu_resume_rewire", True,  "Finish a rewire"),
        # Not gated: it changes a label, not anything physical.
        "rename_board":  ("menu_rename_board",  False, "Rename a board"),
        "change_wifi":   ("menu_change_wifi",   True,  "Change ESP Wi-Fi"),
        "show_esp_key":  ("menu_show_esp_key",  True,  "Board credentials"),
        # Read-only and UNGATED: it runs on every connect, and asking
        # for a PIN to be told what is outstanding helps nobody.
        "check_notices": ("menu_check_notices", False, "Check pending actions"),
        # Read-only and ungated: the hub asking a board how it is. Exists
        # so no diagnostic ever needs Tasmota's own web page again.
        "board_report":  ("menu_board_report",  False, "Board report"),
        # "hide_ha" is gone with the HA event ear (2026-09-02); an app that
        # still sends it gets the ordinary "Unknown action" refusal.
        # Gated as add_device is, and for the same reason: it puts records
        # in devices.json. The boards:unregistered notice routes here.
        "register_missing": ("menu_register_missing", True,
                             "Register missing devices"),
        # Firmware images handed over from the phone -- for a hub that cannot
        # build (the phone carries them from the one that did). Gated as the
        # build is: it replaces what every board is flashed with.
        "firmware_from_phone": ("menu_firmware_from_phone", True,
                                "Install firmware on hub"),
        # Settings -> GitHub builds. Gated: it takes and keeps a credential
        # (the GitHub token), and it replaces what every board is flashed
        # with when it fetches a build.
        "github_builds": ("menu_github_builds", True, "GitHub builds"),
        # The checkpoint's door: continue an interrupted board setup from
        # its exact stage, or delete it. The setup:<slug> notices route
        # here; Delete inside it is the only destroyer anywhere.
        "resume_setup":  ("menu_resume_setup",  True,  "Continue board setup"),
    }

    def _require_init(self) -> "SceneManager":
        """The SceneManager, or a sentence explaining why there isn't one.

        It is built in init() rather than __init__ because it needs the
        model's embedding width. Every entry point (run(), and ws_server's
        startup) awaits init() immediately after constructing the shell, so
        this should never fire -- but "NoneType has no attribute list_scenes"
        thrown from the middle of a command is a poor way to learn that a new
        entry point forgot to.
        """
        if self.sm is None:
            raise RuntimeError(
                "InteractiveShell.init() has not been awaited, so scenes are "
                "not loaded. Construct the shell, then await init().")
        return self.sm

    def _grace_key(self, speaker_id: Optional[str]) -> Tuple[str, str]:
        """(who is asking, whom they claim to be). The sudo window was keyed
        by name alone, so a phone that merely CLAIMED a name inherited the
        window another phone had just earned for it. The phone id is set
        per connection by ws_server; the terminal is "cli"."""
        return (self.current_phone_id or "cli",
                speaker_id or "")

    def _verified_recently(self, speaker_id: Optional[str] = None) -> bool:
        """Has THIS connection passed a gate check for this person inside
        MENU_GATE_GRACE_SECONDS? Read by the listings that hide a secure
        device's state from anyone who has not."""
        if speaker_id is None:
            speaker_id = self.current_speaker_id
        # MONOTONIC, not the wall clock. A Pi has no RTC: it boots on the
        # fake-hwclock time and NTP (or the hub's own clock setting) moves
        # time.time() afterwards -- a step BACKWARDS stretched an earned
        # window by however far the clock fell, vouching for gated acts
        # long after MENU_GATE_GRACE_SECONDS. The window is in-memory only,
        # so nothing outside this process needs to read the number.
        return time.monotonic() < self._gate_grace.get(self._grace_key(speaker_id), 0.0)

    async def _gate_for(self, label: str,
                        speaker_id: Optional[str]) -> bool:
        """The voice check and the sudo window, in ONE place.

        Extracted so an action that carries its subject in its name
        (`pause:<id>`) is gated by exactly the same code as the fixed
        action it belongs to -- a second copy of this is how two doors
        into one capability end up with two different locks.

        A REFUSAL HERE SAYS WHY (2026-09-12). Both exits used to write a
        warn line and nothing more, so the failure card ws_server builds
        fell through to "<action> did not complete" -- the same blank amber
        verdict whether nobody was identified, the PIN was cancelled, or
        the account was locked out. See _offer_pin_fallback, which sets the
        note for every refusal it owns; this method only fills in the two
        it owns itself, and never overwrites a more specific one.
        """
        if not speaker_id:
            self.failure_note = (
                "This hub does not know who is holding this phone, so it "
                "cannot authorise anything. Enrol a voice first.")
            await self._io.write(
                "Nobody is identified, so this cannot be authorised. "
                "Enroll a voice first.", level="warn")
            return False
        # THE SUDO WINDOW: one passed check vouches for the next few
        # minutes of the same person's housework. Without it, every
        # gated action re-ran the full check back to back.
        if self._verified_recently(speaker_id):
            event("MENU", f"{label!r} allowed for {speaker_id!r} "
                          f"within the verified window")
            # SAY SO. A check that is skipped in silence reads as a check
            # that is missing (field report 2026-09-03: "no pin gate was
            # seen"). The app shows this as a passing line.
            try:
                ago = max(0, int(MENU_GATE_GRACE_SECONDS - (
                    self._gate_grace.get(self._grace_key(speaker_id), 0.0)
                    - time.monotonic())))
                mins = max(1, round(ago / 60))
                await self._io.send_data("output", {
                    "level": "info", "kind": "gate_skipped",
                    "text": f"Verified {mins} min ago -- no check needed for "
                            f"{label.lower()}.",
                })
            except Exception:                              # noqa: BLE001
                pass
            return True
        if not await self._voice_gate(speaker_id, None, force_active=True):
            event("MENU", f"{label!r} DENIED for {speaker_id!r}")
            # NEVER OVERWRITE a more specific note. _voice_gate's own
            # refusals know exactly what went wrong -- no PIN on file, the
            # box closed, a lockout with a countdown -- and that sentence
            # is the whole point. This fills in only when nothing below
            # explained itself, so the card can never be blank again.
            if not self.failure_note:
                self.failure_note = (
                    f"{label} was not authorised -- the voice check did not "
                    f"pass and the PIN was not accepted. Nothing was changed.")
            await self._io.write(f"{label}: not authorised.", level="warn")
            return False
        if MENU_GATE_GRACE_SECONDS > 0:
            self._gate_grace[self._grace_key(speaker_id)] = (
                time.monotonic() + MENU_GATE_GRACE_SECONDS)
        return True

    async def run_menu_action(self, action: str,
                              speaker_id: Optional[str]) -> bool:
        """Run one named management action for a remote client.

        An ALLOWLIST, deliberately: `action` arrives from the network, and
        resolving it with getattr(self, action) would turn any method name on
        this class into something a client could call.
        """
        # AN APPROVAL CARRIES ITS OWN SUBJECT, so it cannot be a fixed
        # entry in the allowlist -- the id names one question raised at one
        # moment. It is still not a free-form call: the prefix is matched
        # exactly, the remainder is only ever looked UP in the engine's own
        # pending list, and an id that is not waiting there does nothing.
        # ACTIONS THAT CARRY THEIR SUBJECT. One automation is paused, one
        # request approved -- the id names a thing, so these cannot be
        # fixed entries in the allowlist. They are still not free-form
        # calls: the PREFIX is matched exactly against this table, the
        # method is one of ours by name, and the remainder is only ever
        # looked UP in a store. `gate_as` keeps ONE gating authority --
        # pausing a rule is checked exactly as updating one is.
        #
        # ONE RUNNER FOR BOTH KINDS. The argument path used to return the
        # method's raw result straight from here -- past the cancel note,
        # past the blanket handler below (an exception reached ws_server
        # as "internal server error"), and past the None-is-success rule.
        head, colon, argument = action.partition(":")
        arg_entry = self.ARG_ACTIONS.get(head) if colon else None
        if arg_entry is not None:
            method_name, gate_as = arg_entry
            _m, gated, label = (self.MENU_ACTIONS.get(gate_as or "")
                                or (None, False, head))
            args: Tuple[str, ...] = (argument,)
        else:
            entry = self.MENU_ACTIONS.get(action)
            if entry is None:
                event("MENU", f"refused unknown action {action!r}")
                await self._io.write(f"Unknown action: {action!r}", level="warn")
                return False
            method_name, gated, label = entry
            args = ()
        event("MENU", f"{label!r} requested by speaker={speaker_id!r} (gated={gated})")

        # SOMEBODY ELSE'S scene or automation: the subject method asks for
        # ITS MAKER'S PIN (WHO MADE WHAT) -- the only key to it. The usual
        # gate checks the person holding the phone, who is not the one
        # whose say-so counts here.
        if gated and arg_entry is not None:
            owner = self._subject_owner(head, argument)
            if owner and owner != speaker_id and owner in self._household():
                gated = False

        if gated:
            if not await self._gate_for(label, speaker_id):
                return False

        try:
            result = await getattr(self, method_name)(*args)
        except CancelledByUser:
            # RE-RAISED, not swallowed. Turning a cancel into False here
            # made ws_server report "did not complete" -- the amber failure
            # window -- for a person who deliberately backed out, and the
            # proper cancelled frame (Cancel popup, progress checkpointed)
            # was unreachable from inside a flow. The transport layer owns
            # the cancelled ending; this layer only passes the word along.
            await self._io.write("(cancelled)")
            raise
        except Exception:
            # A MENU ACTION MUST NEVER TAKE THE CONNECTION WITH IT.
            #
            # Without this, an exception here propagated to ws_server's
            # connection handler, which responds by closing the socket -- so
            # any unhandled error in any flow presented as "the app
            # disconnected", with no message and nothing on screen to explain
            # it. One line of Windows asyncio arcana (Selector loops cannot
            # spawn child processes) reached the user as a dead connection
            # three seconds after pressing Run.
            #
            # The console keeps the traceback; the person gets a sentence and
            # a working app.
            traceback.print_exc()
            event("MENU", f"{label!r} FAILED with an unhandled exception")
            await self._io.write(
                f"{label} stopped unexpectedly -- the server console has the "
                f"details. Nothing else was affected.", level="warn")
            return False
        event("MENU", f"{label!r} finished")
        # Most menu methods return None; that is not a failure, it is a flow
        # that ran to completion. Only an explicit False means it did not.
        return result is not False


    async def _offer_pending_setup(self, entry: str = "") -> Optional[Set[str]]:
        """Offer anything Home Assistant has discovered but not yet set up.

        Returns the entity ids a completed set-up produced, an EMPTY set if
        nothing was set up, or None if the person cancelled.

        The empty-versus-None distinction is load-bearing: "I did not set
        anything up" must carry on into the normal picker, while "I cancelled"
        must stop. Collapsing them would either strand someone trying to back
        out, or silently skip the rest of the flow.
        """
        # ── SCAN FIRST ────────────────────────────────────────────────
        # Looking is free and usually enough. Offering "add by address" as a
        # PEER of the found devices was the confusing part: it presented a
        # fallback as an equal choice, so someone with a perfectly
        # discoverable device was invited to go and type an IP for no reason.
        # Scan, show what turned up, and only reach for the harder paths when
        # the easy one came back empty or the person says "not that one".
        # THE SCAN NEEDS HOME ASSISTANT -- it asks HA what it has discovered.
        # A hub with no HA has nothing to scan, but it can still set up its
        # OWN ESP boards, so skip straight to the doors below instead of
        # calling a client that is not there.
        #
        # ASK AGAIN FIRST, if HA was configured and was down when the hub
        # started (2026-09-11). This is the door where its absence is most
        # visible -- no scan, and no "Add an integration" -- so it is the
        # door that should not be answering from a judgement made hours ago.
        # `entry` names a door to go straight through ("esp"): no scan, no
        # "how do you want to add it?".
        await asyncio.to_thread(self.reconnect_ha_if_needed)
        if self.ha_client and not entry:
            await self._io.write("\n  Looking for new devices...")
            pending = await asyncio.to_thread(self.ha_client.list_pending_flows)
        else:
            pending = []

        picked_flow_id = ""
        if pending:
            options = []
            for flow in pending:
                ctx = flow.get("context") or {}
                title = (flow.get("title")
                         or ctx.get("title_placeholders", {}).get("name")
                         or flow.get("handler") or "unknown")
                options.append({"value": flow.get("flow_id", ""),
                                "label": str(title),
                                "sub": f"{flow.get('handler', '?')} · "
                                       f"found via {ctx.get('source', 'unknown')}"})
            options.append({"value": self._OTHER_SENTINEL,
                            "label": "None of these",
                            "sub": "other ways to add a device"})
            await self._io.write(
                f"  Found {len(pending)} device(s) waiting to be set up.")
            picked = await self._io.choose("Which one?", options)
            if not picked:
                return None
            picked_flow_id = picked[0]
        elif self.ha_client and not entry:
            # Only after a scan actually ran -- without HA nothing was looked
            # for, so this would claim a search that never happened.
            await self._io.write(
                "  Nothing new found on the network.")

        # ── FALLBACKS, only once scanning has not answered it ─────────
        #
        # TWO DOORS, DOWN FROM FOUR -- the owner's call (2026-08-20), and
        # the two that went were both disguises. "Install a board I already
        # set up" was the SAME flash path as "Set up a new ESP board" with
        # the design step skipped; splitting them made a person decide
        # which door they were standing at before they could start, and on
        # the migration night the wrong-door cost was real. "It is already
        # set up" described a world before registration was automatic --
        # boards register themselves after the flash, integration entities
        # are offered the moment their flow finishes, so a door whose whole
        # job was "just register it" had quietly become a door to nowhere
        # anyone needed. What remains is the honest pair: an ESP board of
        # ours, or somebody else's integration.
        if entry or not picked_flow_id or picked_flow_id == self._OTHER_SENTINEL:
            choices = [
                {"value": "esp", "label": "Set up an ESP board",
                 "sub": "new or already designed -- plug it in once and "
                        "this hub does the rest"},
            ]
            # AN INTEGRATION IS A HOME ASSISTANT THING. Without HA there is
            # nothing for this door to open, so it is offered only when HA is
            # here -- unlike the ESP door, which is the hub's own and stands
            # on its own.
            if self.ha_client:
                choices.append(
                    {"value": "integration", "label": "Add an integration",
                     "sub": "anything that is not an ESP board -- by name or "
                            "address"})
            # The typed-id path. With HA it also lives inside the entity
            # picker ("Add one manually instead"); without HA that picker
            # never runs, so it needs its own door here or a hub with no HA
            # could not register a typed device at all.
            if not self.ha_client:
                choices.append(
                    {"value": "manual", "label": "Enter a device by id",
                     "sub": "register a device by typing its id -- no board "
                            "to flash"})
            picked = [entry] if entry else await self._io.choose(
                "How do you want to add it?", choices)
            if not picked:
                return None
            choice = picked[0]

            if choice == "manual":
                # Hand back to menu_add_device, which runs the typed-id form.
                self._manual_add = True
                return None

            if choice == "esp":
                # ONE picker covers what the two doors used to split: every
                # existing board (designed or installed -- picking one goes
                # straight to the cable) and "A new board" (the design flow
                # first). One question, with real content.
                slug: Optional[str] = None
                layout_id: Optional[str] = None
                existing = self.esp.list_nodes()
                layouts = self.esp.list_layouts()
                # THE PICKER: a new board, a saved layout to start from, or
                # a board still mid-setup. A board that is finished is not
                # this door's business (Augusta, 2026-08-24) -- and its
                # reinstall / board-type acts now live on its own sheet in
                # Devices, where the board already is. "Already set up" is
                # gone from here: starting from existing wiring is what
                # SAVED LAYOUTS are for (her design, 2026-10-01).
                records = {k: (self.esp._read_board(k) or {})
                           for k in existing}
                unfinished = [k for k in existing
                              if not records[k].get("flashed")
                              or records[k].get("network_pending")]
                if unfinished or layouts:

                    def _board_row(known: str, tail: str) -> Dict[str, str]:
                        rec = records[known]
                        # esp.board_state, not a second opinion: this row
                        # and the CLI picker disagreed about a board that
                        # has firmware but no network, because each
                        # phrased it itself.
                        return {"value": known,
                                "label": rec.get("name", known),
                                "sub": f"{len(rec.get('items', []))} "
                                       f"device(s), "
                                       f"{self.esp.board_state(rec)}"
                                       f" -- {tail}"}

                    rows = [{"value": "\x00new", "label": "A new board",
                             "sub": "assign its pins, then plug it in "
                                    "once"}]
                    rows += [_board_row(k, "finish setting it up")
                             for k in unfinished]
                    if layouts:
                        rows.append({
                            "value": "\x00layout",
                            "label": f"A saved layout ({len(layouts)})",
                            "sub": "start from wiring you saved -- drafts "
                                   "and finished designs, one chip type "
                                   "each"})
                    got = await self._io.choose("Which board?", rows)
                    if not got:
                        return None
                    if got[0] == "\x00layout":
                        layout_id = await self.esp.pick_layout()
                        if layout_id is None:
                            return None
                        got = ["\x00new"]
                    if got[0] != "\x00new":
                        slug = got[0]
                        # THREE VERBS, not one. Picking a board used to go
                        # straight to the cable, which made install the only
                        # thing you could DO with a designed board -- editing
                        # its wiring or removing it had no door at all
                        # (Augusta, 2026-08-24). One small question buys all
                        # three, and only when acting on an existing board.
                        rec = self.esp._read_board(slug) or {}
                        board_name = rec.get("name", slug)
                        act = await self._io.choose(
                            f"{board_name} -- do what?",
                            [{"value": "install",
                              "label": ("Reinstall it" if rec.get("flashed")
                                        else "Install it now"),
                              "sub": "plug it in by USB -- firmware, "
                                     "network, wiring"},
                             {"value": "edit", "label": "Edit its wiring",
                              "sub": "add or remove devices on it"},
                             # For a board chosen wrong: the cable refuses
                             # an install on a chip of another family and
                             # sends the person here.
                             {"value": "boardtype",
                              "label": "Change its board type",
                              "sub": "it is "
                                     + self.esp._board_of(rec).label},
                             {"value": "remove", "label": "Remove it",
                              "sub": ("the full retirement -- devices, "
                                      "account, record"
                                      if rec.get("flashed") else
                                      "it was never installed -- its design "
                                      "and its broker account go")}])
                        if not act:
                            return None
                        if act[0] == "edit":
                            # The SAME flow Settings offers -- pick skipped.
                            await self.menu_edit_board(slug)
                            self._esp_handoff = True
                            return None
                        if act[0] == "boardtype":
                            await self.esp.change_board_type(slug)
                            self._esp_handoff = True
                            return None
                        if act[0] == "remove":
                            if rec.get("flashed"):
                                await self.menu_retire_board(slug)
                            else:
                                # Never flashed: no chip to wipe, no HA
                                # entry, no registered devices -- only the
                                # design and the broker account minted with
                                # it. Still confirmed: a design is work.
                                sure = await self._io.choose(
                                    f"Remove {board_name}? It was never "
                                    f"installed.",
                                    [{"value": "yes",
                                      "label": "Yes, remove its design",
                                      "sub": "its wiring here and its "
                                             "broker account"},
                                     {"value": "no", "label": "No, keep it",
                                      "sub": ""}])
                                if sure and sure[0] == "yes":
                                    if await self.esp.revoke_access(slug):
                                        await self._io.write(
                                            "  Revoked its account on the "
                                            "broker.")
                                    if self.esp.delete_node(slug):
                                        await self._io.write(
                                            f"  Removed {board_name}: its "
                                            f"wiring and credentials.")
                                    event("ESP", f"designed board {slug!r} "
                                                 f"removed before install")
                                else:
                                    await self._io.write("  Kept.")
                            self._esp_handoff = True
                            return None
                        # act == "install" falls through to the cable.
                # A board has to be SET UP before Home Assistant can see it,
                # and that now happens entirely over the USB cable: the same
                # prebuilt firmware every time, then the board is told its
                # network, its broker credentials and its wiring.
                #
                # THE ADOPTION DANCE IS GONE. There used to be a config flow
                # to run afterwards, with an encryption key to hand over and
                # an address to discover -- which is why the flash had to
                # read the IP off the boot log. Home Assistant's Tasmota
                # integration finds the board over the broker by itself, so
                # there is nothing to adopt and nothing to type.
                if slug is None:
                    if not await self.esp.add_node(layout_id):
                        return None
                    slug = self.esp.last_node

                # THE "SNAPSHOT BEFORE THE CABLE GOES IN" IS GONE
                # (2026-09-14). Its reasoning was sound -- a board flashed
                # once already can register within seconds of rebooting, so
                # a snapshot taken afterwards would call its own entities
                # pre-existing -- and it had been feeding an argument
                # `register_board` stopped reading on 2026-09-01. Every
                # entity in the house, fetched over REST, discarded.

                await self._flash_and_register(slug)
                # THE FLOW IS OVER, whichever way it went: the success is
                # registered, the failure is parked as a checkpoint. Without
                # this the None falls into the cancel handler above and
                # prints "(Cancelled -- nothing saved.)" underneath a board
                # that was just flashed, provisioned and registered.
                self._esp_handoff = True
                return None

            # choice == "integration" -- HA's own catalogue, searched
            # live; cancelling here still falls through to the register
            # picker below, so an entity that is ALREADY in Home Assistant
            # can be registered by opening this door and backing out of the
            # search.
            flow = await self.flows.start_manual()
            if flow is None:
                return set()
        else:
            flow = next((f for f in pending
                         if f.get("flow_id") == picked_flow_id), None)
            if flow is None:
                await self._io.write("  That set-up is no longer waiting.",
                                     level="warn")
                return set()

        event("HA", f"config flow {flow.get('handler')!r} started")
        # What exists BEFORE, so we can say what the set-up actually added.
        before = {e["entity_id"] for e in
                  (await asyncio.to_thread(self.ha_client.get_states) or [])}

        # NO PREFILL ANY MORE. This used to fill an ESPHome encryption key
        # into whichever config flow asked for one, on every path, because
        # the alternative was somebody hunting for a key the hub had
        # generated and never shown them.
        #
        # Our own boards no longer reach this code at all -- Home Assistant's
        # Tasmota integration discovers them over the broker, with no flow to
        # run and no credential to hand over. What still arrives here are
        # OTHER people's integrations: a Hue bridge, a Zigbee stick, a
        # thermostat. The hub has no business inventing values for those, so
        # the fields are asked for honestly.
        ok = await self.flows.run(flow)
        event("HA", f"config flow {flow.get('handler')!r} -> {ok}")
        if not ok:
            # A failed set-up is not a failed ADD -- the devices already in HA
            # are still registrable, so fall through rather than abandoning.
            return set()

        # Entities register asynchronously after the entry is created, so a
        # states read the instant the flow returns finds nothing new.
        await self._io.write("  Waiting for its entities...")
        appeared: set = set()
        deadline = time.monotonic() + self.flows.ENTITY_SETTLE_SECONDS
        while time.monotonic() < deadline:
            await asyncio.sleep(1.0)
            now = {e["entity_id"] for e in
                   (await asyncio.to_thread(self.ha_client.get_states) or [])}
            appeared = now - before
            if appeared:
                break

        if not appeared:
            await self._io.write(
                "  Set up, but no entities have arrived yet. Try adding it again "
                "in a moment.", level="warn")
            return set()

        await self._io.write(f"  {len(appeared)} new entit"
                             f"{'y' if len(appeared) == 1 else 'ies'}. "
                             f"Now register one:")
        return appeared

    #: "None of these" in the discovered list -- opens the fallback menu
    #: rather than being a fallback itself. A literal that cannot collide
    #: with a real flow_id or with the empty string that means "skip".
    #:
    #: (There was a _BY_ADDRESS_SENTINEL beside this one, from when "type an
    #: address" sat in the discovered list as a peer of the found devices.
    #: Scanning first moved it into the fallback menu, where it is an
    #: ordinary "address" value, so the sentinel had no readers left.)
    _OTHER_SENTINEL = "\x00other"


    # THE INFRARED FLOWS MOVED to home_assistant/ir_flows.py, beside the
    # config flows and for the same reason: 300 lines about remote controls
    # in a file that otherwise runs a voice assistant. MENU_ACTIONS names
    # this method, so it stays as the door -- one line, delegating.
    async def menu_learn_ir(self) -> bool:
        return await self.ir.menu_learn_ir()

    async def menu_firmware_from_phone(self) -> bool:
        return await self.esp.menu_firmware_from_phone()

    async def menu_github_builds(self) -> bool:
        return await self.esp.menu_github_builds()

    # ── Pending actions ─────────────────────────────────────────────
    #
    # A notice is something the hub knows is UNFINISHED and can route the
    # person to. They are computed here rather than inferred by the app from
    # result codes: the server is the only party that knows a board was just
    # created, or that a device has no embedding and is therefore deaf to
    # every phrasing except its exact name.
    #
    # Each one carries its own ACTION, so the app never has to map a message
    # back to a screen.

    #: A description this short cannot carry meaning for semantic matching.
    WEAK_DESCRIPTION_WORDS = 3

    def _notice(self, notice_id: str, title: str, body: str,
                kind: str = "none", value: str = "",
                label: str = "", now: bool = False) -> Dict[str, Any]:
        return {
            "id": notice_id,          # stable: re-sending REPLACES, never duplicates
            "title": title,
            "body": body,
            "action_kind": kind,      # "menu" | "route" | "none"
            "action_value": value,
            "action_label": label,
            "now": now,               # ask about it immediately, not just in the list
        }

    async def _send_notices(self, notices: List[Dict[str, Any]]) -> None:
        for notice in notices:
            await self._io.send_data("notice", notice)

    def _devices_needing_description(self) -> List[str]:
        """Devices reachable ONLY by their exact name.

        The description is the single input to semantic matching, so a device
        without a usable one answers "turn on the lamp" and nothing else --
        not "light in here", not "make it brighter". That is invisible until
        someone tries a phrasing and it silently fails.
        """
        weak = []
        for did, d in self.dm.devices.items():
            description = (d.get("description") or "").strip()
            name = (d.get("name") or did).strip().lower()
            if (not d.get("embedding")
                    or not description
                    or description.lower() == name
                    or len(description.split()) < self.WEAK_DESCRIPTION_WORDS):
                weak.append(d.get("name") or did)
        return weak

    @staticmethod
    def _board_parts(record: Dict[str, Any]) -> List[Dict[str, Any]]:
        """The parts on a board's plan, each with the driver it rides on.

        For the app's board sheet (2026-09-11). One row per item of the
        record: the name it was given, the catalogue's own name for the
        part, every pin with the Tasmota component the template puts on
        it ("Relay1 on GPIO33", "I2C SDA on GPIO21"), and -- for the parts
        that need HomeVoice's own build -- the driver names the firmware
        must carry. A part the catalogue no longer knows is still listed,
        by its type id, rather than dropped: what is wired is what is
        wired. Pure, so it is true offline; nothing here asks the board.
        """
        from esp_firmware import esphome_catalog as _cat
        out: List[Dict[str, Any]] = []
        for item in (record.get("items") or []):
            if not isinstance(item, dict):
                continue
            type_id = str(item.get("type_id") or "")
            dev = _cat.BY_ID.get(type_id)
            pins = item.get("pins") if isinstance(item.get("pins"), dict) else {}
            rows: List[Dict[str, Any]] = []
            comps: List[str] = []
            if dev is not None:
                for slot in dev.slots:
                    gpio = pins.get(slot.key)
                    if gpio is None:
                        continue
                    comp = slot.component.name
                    rows.append({"slot": slot.label, "gpio": gpio,
                                 "component": comp})
                    if comp not in comps:
                        comps.append(comp)
            else:
                for key, gpio in pins.items():
                    rows.append({"slot": str(key), "gpio": gpio,
                                 "component": ""})
            out.append({
                "name": str(item.get("name") or ""),
                "type_id": type_id,
                "part": (dev.label if dev is not None else type_id) or "?",
                "family": (dev.friendly_family if dev is not None else ""),
                "pins": rows,
                # The Tasmota components its pins are set to -- what the
                # template says, and what `GPIO` on the board echoes back.
                "components": comps,
                # Driver names HomeVoice's own firmware must carry (empty
                # for the many parts every Tasmota build has).
                "needs": list(dev.driver_names) if dev is not None else [],
            })
        return out

    def _unregistered_board_devices(
            self) -> List[Tuple[str, str, List[Dict[str, Any]]]]:
        """Things on a board's own plan that have no record here.

        THE GAP NOTHING ELSE LOOKS AT. Registration happens once, inside a
        ceremony. If that ceremony is interrupted, refused, or was run by
        an older build that dropped the device, the item stays on the
        board's plan and out of devices.json for ever, and nothing
        anywhere looks again. The attach courier walks this exact ground
        and steps over it -- it sets `never_registered` and moves on,
        because its job is entity ids, not records.

        Her PZEM was precisely this: wired, on the plan, answering over
        MQTT, and in no list anywhere ("i didnt see pzem reader in
        devices.json", 2026-09-01). Nothing was broken enough to report,
        which IS the bug -- so this reports it.

        REPORTED, NEVER REGISTERED BEHIND HER BACK. "Remove one" during a
        board summary deliberately leaves a device wired and unregistered
        ("It is still wired to the board -- remove it from the board's
        pins if you want it gone for good"), so a sweep that registered
        whatever it found would undo that choice on every restart. Her
        ruling, 2026-09-01: say it, and let one tap fix it.

        Returns (slug, board name, the items with no record).
        """
        from esp_firmware import esphome_catalog as _cat
        out: List[Tuple[str, str, List[Dict[str, Any]]]] = []
        for slug in self.esp.list_nodes():
            record = self.esp._read_board(slug) or {}
            # A BOARD MID-CEREMONY OWES ITS OWN NOTICE. An unfinished
            # setup already says so in its own words; this one would only
            # repeat it, and would accuse a flow that has not finished yet
            # of having failed.
            if self._setup_stage(record):
                continue
            board_name = record.get("name", slug)
            missing: List[Dict[str, Any]] = []
            for item in (record.get("items") or []):
                if not isinstance(item, dict):
                    continue
                if item.get("type_id") not in _cat.BY_ID:
                    continue        # unknown type: the plan is the problem
                name = str(item.get("name") or "").strip()
                if not name:
                    continue
                if self.dm.board_device_id(slug, name) in self.dm.devices:
                    continue
                missing.append(item)
            if missing:
                out.append((slug, board_name, missing))
        return out

    async def _unregistered_ha_entities(self) -> List[str]:
        """Things Home Assistant can control that HomeVoice has never been
        told about. Off the event loop -- this is an HTTP call."""
        if self.ha_client is None:
            return []
        try:
            entities = await asyncio.to_thread(
                self.ha_client.list_controllable_entities)
        except Exception:
            return []          # HA being unreachable is not a notice
        known = {d.get("ha_entity_id") for d in self.dm.devices.values()
                 if d.get("ha_entity_id")}
        return [e.get("name") or e.get("entity_id")
                for e in entities if e.get("entity_id") not in known]

    def _github_token_notice(self, now: Any = None) -> Optional[Dict[str, Any]]:
        """THE GITHUB TOKEN'S DATE, as GitHub gave it (cloud_build.EXPIRY_KEY):
        a week's warning, then a notice that asks at once when it has run
        out -- builds stop working that day, and nothing else would say so."""
        try:
            from esp_firmware import cloud_build as _cb
            token, _repo = _cb.settings()
            when = _cb.token_expiry() if token else ""
            left = _cb.days_left(when, now) if when else None
        except Exception:                          # noqa: BLE001
            return None
        if left is None or left > 7:
            return None
        how = ("Make a new one on github.com (Settings > Developer settings "
               "> Fine-grained tokens, the same steps as before), then "
               "Settings > GitHub builds > Change the token.")
        words = _cb.expiry_words(when, now)
        if left < 0:
            return self._notice(
                "github:token", "Your GitHub token has expired",
                f"It {words.replace('EXPIRED', 'expired')}. Firmware cannot "
                f"be built on GitHub until it is renewed. {how}",
                kind="menu", value="github_builds", label="Renew the token",
                now=True)
        return self._notice(
            "github:token", f"Your GitHub token {words}",
            f"After that, firmware cannot be built on GitHub. {how}",
            kind="menu", value="github_builds", label="Renew the token")

    async def menu_check_notices(self) -> bool:
        """Recompute what is outstanding and send it to the client.

        UNGATED and read-only on purpose: this runs on every connect, and
        asking for a PIN to be told 'three devices need a description' would
        be worse than not telling anyone.
        """
        # A board an earlier version parked on the (now removed) rewire
        # question is settled here, silently, before notices are computed.
        try:
            await self._settle_legacy_rewires()
        except Exception:                          # noqa: BLE001
            traceback.print_exc()
        notices: List[Dict[str, Any]] = []

        # A CEREMONY THAT DIED HALF-WAY, first in the list because it is the
        # only notice about the person reading it.
        #
        # Registration is one transaction, so nothing of the attempt
        # survives -- and that is exactly why this note has to. The voice
        # was recorded and then erased; without a line saying so, a
        # registration killed by the network is indistinguishable from one
        # that never started.
        interrupted = self._interrupted_registration()
        if interrupted:
            who = str(interrupted.get("name") or "").strip()
            notices.append(self._notice(
                "registration_interrupted",
                "Registration did not finish",
                (f"The recording for {who!r} was discarded, because a person "
                 f"is only added once the voice, the PIN and the recovery "
                 f"words are all done. Nothing was kept. Start again when "
                 f"you are ready."
                 if who else
                 "A registration was interrupted before it finished, so "
                 "nothing was kept. Start again when you are ready."),
                kind="menu", value="enroll_voice", label="Start again"))

        # The automation engine: retry its start (the first board or the
        # broker may have arrived since boot -- start() is idempotent and
        # cheap when it cannot), and surface what its rules NOTIFIED as
        # sticky notices ("something was left running in the kitchen").
        if self.automations is not None:
            try:
                await asyncio.to_thread(self.automations.start)
            except Exception:                      # noqa: BLE001
                pass
            for alert in list(self.automations.alerts)[-10:]:
                notices.append(self._notice(
                    alert["id"], f"⚡ {alert['title']}", alert["body"]))

            # AN OPEN QUESTION OUTRANKS A REPORT. An `ask` action is a
            # rule that noticed something and stopped -- the door is
            # untouched until a person answers -- so it is raised with
            # now=True, straight onto the screen rather than into a list
            # somebody might read later. It expires on its own (see
            # AUTOMATION_APPROVAL_SECONDS); a stale key in a door is
            # exactly what this must never become.
            try:
                pending = self.automations.pending_approvals()
            except Exception:                      # noqa: BLE001
                pending = []
            for ask in pending:
                notices.append(self._notice(
                    ask["id"], f"🔐 {ask['device_name']}",
                    ask["text"], kind="menu",
                    value=f"approve:{ask['id']}",
                    label="Approve with PIN", now=True))

        def _first_four(names: List[str]) -> str:
            more = f" and {len(names) - 4} more" if len(names) > 4 else ""
            return ", ".join(names[:4]) + more

        weak = self._devices_needing_description()
        if weak:
            shown = _first_four(weak)
            notices.append(self._notice(
                "devices:weak_description",
                f"{len(weak)} device(s) only answer to their exact name",
                f"{shown} have no usable description, so voice matching "
                f"cannot recognise other phrasings. Updating the description "
                f"fixes it.",
                kind="menu", value="update_device", label="Update a device"))

        stray = await self._unregistered_ha_entities()
        if stray:
            shown = _first_four(stray)
            notices.append(self._notice(
                "ha:unregistered",
                f"{len(stray)} Home Assistant device(s) not registered here",
                f"{shown} can be controlled by Home Assistant but Omniscience "
                f"has never been told about them.",
                kind="menu", value="add_device", label="Add a device"))

        # THE SAME QUESTION ABOUT OUR OWN BOARDS, and the more serious one:
        # an entity Home Assistant knows and we do not is somebody else's
        # device, but a thing on OUR board's plan with no record here is a
        # device this hub designed, flashed, wired and then lost. See
        # _unregistered_board_devices.
        adrift = self._unregistered_board_devices()
        if adrift:
            named = [f"{item.get('name')} ({board})"
                     for _s, board, items in adrift for item in items]
            shown = _first_four(named)
            notices.append(self._notice(
                "boards:unregistered",
                f"{len(named)} device(s) on a board have no record here",
                f"{shown} are wired and on the board's plan, but Omniscience "
                f"has no record of them -- so nothing reads them, nothing "
                f"drives them, and they appear in no list. The wiring is "
                f"already done; this only adds the record.",
                kind="menu", value="register_missing",
                label="Register them"))

        # Remote access: ONE notice, from the hub's real Tailscale state
        # (tailnet.access_state) -- paused, switched off, run out, not set
        # up -- and none while it works. Its Details opens Remote access
        # where the person is.
        try:
            from core import tailnet as _tn
            access = await asyncio.to_thread(_tn.access_state)
            remote = _tn.remote_access_notice(access)
        except Exception:                          # noqa: BLE001
            remote = None
        if remote is not None:
            notices.append(remote)

        github = self._github_token_notice()
        if github is not None:
            notices.append(github)

        # A NEW PHONE WAS PAIRED (2026-10-15): every OTHER phone is told,
        # with the road it came by, until someone says it is theirs or
        # removes it -- or a week passes. A phone that was off when the
        # announcement was spoken still learns of it here.
        notices.extend(self._new_phone_notices())

        # LETTERS: what somebody else did to your scenes and automations,
        # kept until you say "Got it" (see WHO MADE WHAT).
        notices.extend(self._letter_notices())


        # EVERY BOARD-SHAPED PIECE OF UNFINISHED BUSINESS, recomputed from
        # the board records on every connect -- which is what lets the app
        # prune by this list without ever losing a live question. A notice
        # here survives restarts, reconnects and any amount of viewing,
        # and disappears only when its condition actually clears.
        #
        # ONE notice per board, whatever its stage (the ribbon law,
        # 2026-08-25): every interrupted setup is a checkpoint, and its
        # notice carries the only two verbs that exist -- Continue (from
        # exactly where it stopped) and Delete (the only thing anywhere
        # that destroys the work).
        for slug in self.esp.list_nodes():
            record = self.esp._read_board(slug) or {}
            if self._setup_stage(record) is not None:
                notices.append(self._setup_notice(slug, record))
        for rec in self.esp.pending_layouts():
            notices.append(self._layout_notice(rec))

        # A board that dropped out (its last will says Offline) for two
        # minutes: one notice each, gone by itself when it is back.
        notices.extend(self._offline_board_notices())

        await self._send_notices(notices)
        # THE FULL CURRENT LIST, by id. The app prunes anything it still
        # holds that is no longer on this list -- so a notice disappears
        # when its CONDITION clears, never merely because it was looked at.
        await self._io.send_data("notice_ids",
                                 {"ids": [n["id"] for n in notices]})
        event("NOTICE", f"{len(notices)} pending action(s) reported")
        return True

    # ── ESP nodes ───────────────────────────────────────────────────


    async def menu_add_esp_node(self) -> bool:
        """Describe a new ESP board. The work is in esp_builder.py; this is
        the name MENU_ACTIONS resolves, and the voice command reaches."""
        return await self.esp.add_node()

    @staticmethod
    def _norm_name(text: str) -> str:
        """A name reduced to letters, digits and hyphens, for matching.

        Home Assistant titles an ESPHome entry after the node, but not
        byte-identically -- "Kitchen Node" against `kitchen-node`. Both
        normalise to the same thing.
        """
        return re.sub(r"[^a-z0-9]+", "-", str(text).lower()).strip("-")

    async def _which_board(self, slug: Optional[str]
                           ) -> Tuple[str, Dict[str, Any]]:
        """(slug, record) of the board a door acts on: the slug it was
        handed (from the network -- `editboard:<slug>` and friends), or the
        picker's answer. ("", {}) is a refusal, its reason already in
        failure_note. Backing out of the picker is a CANCEL: two of the four
        doors that shared this returned False for it, which put the amber
        "did not complete" over a person who simply changed their mind."""
        if slug is None:
            slug = await self.esp.pick_existing()
            if slug is None:
                if self.esp.list_nodes():
                    raise CancelledByUser()
                self.failure_note = "No boards have been set up yet."
                return "", {}
        record = self.esp._read_board(slug)
        if record is None:
            await self._io.write(f"  No board called {slug!r}.", level="warn")
            self.failure_note = (f"No board called {slug!r} is here -- the "
                                 f"list may be out of date. Nothing changed.")
            return "", {}
        return slug, record

    async def menu_retire_board(self, slug: Optional[str] = None,
                                quiet: bool = False) -> bool:
        """Take a whole ESP board out of service.

        The counterpart to Add device, at the level a BOARD lives at rather
        than a device. Removing devices one at a time never removes the
        board's record or its broker account, because neither belongs to a
        device -- a board with three relays has three devices and one
        record. Home Assistant is not involved: a board is this hub's alone.

        Everything is listed before anything is deleted. This removes
        several registry entries and the board's credentials at once, and
        the difference between that and what someone meant is worth one
        screen.
        """
        await self._io.write("\n--- RETIRE A BOARD ---")
        # A SLUG THE HUB DOES NOT KNOW RETIRES NOTHING. The slug arrives
        # from the network (dropboard:<slug>); an unknown one used to run
        # the whole removal against an empty record -- and an EMPTY slug
        # made the topic sweep below match every board device there is.
        slug, record_now = await self._which_board(slug)
        if not slug:
            return False

        # ── THE FULL REMOVAL SET, computed BEFORE the question ────────
        # Every device registered on this board, shown once; the deletion
        # below removes exactly this list. Home Assistant is not part of a
        # board's life and is not consulted or touched here (her ruling,
        # 2026-10-01): a board's devices are this hub's alone.
        attached: List[str] = list(self.dm.devices_on_board(slug))

        # ── Say exactly what goes, before offering to do it ──────────
        await self._io.write(f"\n  Retiring {slug} would remove:")
        await self._io.write(
            f"    - this hub's record of {slug}: its wiring and its broker "
            f"credentials")
        await self._io.write(
            "    - (the chip itself keeps its firmware and will keep asking "
            "for the broker until it is re-flashed or erased)")
        if attached:
            names = ", ".join(self.dm.devices[d].get("name") or d
                              for d in attached)
            await self._io.write(
                f"    - {len(attached)} device(s) registered here: {names}")
            scenes = sorted({s for d in attached for s in self._scenes_using(d)})
            if scenes:
                await self._io.write(
                    f"  ! Used by {len(scenes)} scene(s): {', '.join(scenes)}. "
                    f"Those scenes will still run, with these devices skipped.",
                    level="warn")
            used_by = self._rules_using(list(attached))
            if used_by:
                await self._io.write(
                    f"  ! Used by {len(used_by)} automation(s): "
                    f"{', '.join(used_by)}. They will keep running but can "
                    f"no longer do anything with these devices.",
                    level="warn")

        if quiet:
            # The checkpoint's Delete (2026-08-25): the one decision was
            # already made in the notification flow, so no second
            # ceremony here -- wipe the chip if it answers, keep nothing.
            keep_record, wipe = False, True
        else:
            picked = await self._io.choose(
                f"Remove {slug}?",
                [{"value": "wipe", "label": "Remove and wipe it",
                  "sub": "The board is erased, ready to use for something "
                         "else."},
                 {"value": "keep", "label": "Remove, keep its setup",
                  "sub": "The board is left as it is, and the hub remembers "
                         "its wiring, so you can put it back later without "
                         "setting it up again."}])
            if not picked or picked[0] == "no":
                # A deliberate "leave it alone" is the cancelled ending --
                # `return False` here read as FAILURE upstream and put an
                # amber "did not complete" over a choice that completed
                # exactly as asked: by removing nothing.
                await self._io.write("  Nothing removed.")
                raise CancelledByUser()
            keep_record = picked[0] == "keep"
            wipe = picked[0] == "wipe"

        # EVERY device on the board. A board is not a container you tidy --
        # once it is gone its devices are not devices, they are entries
        # pointing at nothing.
        # -- and it is exactly the set listed above: nothing is swept up
        # here that the person did not see before answering.
        # Off the loop: every remove() rewrites devices.json.
        removed: List[str] = await asyncio.to_thread(
            lambda: [did for did in attached if self.dm.remove(did)])
        if removed:
            await self._io.write(f"  Unregistered {len(removed)}: "
                                 f"{', '.join(removed)}")

        # WIPE FIRST, delete the record second. The record holds the
        # credentials the wipe command has to sign in with, so deleting it
        # first would leave the chip permanently unreachable -- still
        # knocking on a broker that no longer knows it, and no longer
        # wipeable except over a cable.
        wiped = False
        if wipe:
            wiped = await self.esp.decommission(slug)
            if wiped:
                await self._io.write(
                    "  Told the board to wipe itself. It will restart with no "
                    "settings, as if it were new.")
            else:
                await self._io.write(
                    "  Could not reach the board to wipe it -- it is probably "
                    "switched off. Everything else was still removed; wipe it "
                    "later over a cable with `esptool erase_flash`.",
                    level="warn")

        # REVOKE BEFORE FORGETTING. The account name lives in the record, so
        # deleting the record first would leave a working login on the
        # broker that nothing knows about and nothing can remove -- a
        # credential outliving the device it belonged to is the exact thing
        # per-board passwords exist to prevent.
        if await self.esp.revoke_access(slug):
            await self._io.write("  Revoked its account on the broker.")

        if not keep_record:
            if self.esp.delete_node(slug):
                await self._io.write("  Removed its wiring and credentials.")
            else:
                await self._io.write("  Could not remove its record.",
                                     level="warn")
        else:
            await self._io.write(
                "  Kept its wiring here, so it can be installed again "
                "without being described from scratch.")

        event("ESP", f"board {slug!r} retired (devices={len(removed)}, "
                     f"kept={keep_record}, "
                     f"wiped={wiped})")

        if not wiped:
            # The one thing none of this touches, and the one thing that
            # makes a removal look like it failed.
            await self._io.write(
                "\n  The chip itself is untouched. It is still running the "
                "firmware and will keep trying to reach this hub -- so it may "
                "reappear in a scan until you unplug it, wipe it, or flash "
                "something else onto it.")
        return True


    async def menu_rename_board(self, slug: Optional[str] = None) -> bool:
        """Change what a board is called.

        THIS EXISTS BECAUSE ITS ABSENCE COST AN EVENING. There was no way to
        rename a board, so the only route to a different name was to create a
        SECOND board and flash it onto the same chip -- which left two
        records and two broker accounts for one piece of hardware, and a Home
        Assistant device still wearing the old name because HA identifies
        boards by MAC address rather than by what they call themselves.

        A rename is not a new board. Now it is not spelled like one.
        """
        await self._io.write("\n--- RENAME A BOARD ---")
        slug, _record = await self._which_board(slug)
        return bool(slug) and await self.esp.rename_board(slug) is not None

    async def _flash_and_register(self, slug: Optional[str]) -> bool:
        """Put the firmware on a board over the cable and register its
        devices; or, when that cannot happen, PARK it as a checkpoint.

        THE CHECKPOINT, not a failure (the ribbon law, 2026-08-25: "if the
        board cant be reached, it saves at that point of the process").
        Everything done so far is already on disk; what could not happen is
        recorded as the STAGE, and one sticky notification owns the way
        forward: Continue picks up exactly here, Delete erases everything.
        Nothing is ever lost except by Delete. True when it all landed."""
        if slug and await self.esp.flash(slug):
            # Registration happens by itself: the devices were named on the
            # pin screen and are described in the registration forms.
            await self.boards_reg.register_board(slug)
            return True
        record_now = self.esp._read_board(slug) or {} if slug else {}
        stage = ("told its network"
                 if self.esp.last_flash_needs_network
                 else "installed")
        self.checkpoint_note = (
            f"Saved -- {record_now.get('name', slug)} still needs "
            f"to be {stage}. Continue or delete it from "
            f"Notifications.")
        await self._io.write(f"\n  {self.checkpoint_note}")
        if slug:
            await self._send_notices(
                [self._setup_notice(slug, record_now)])
        return False

    async def menu_board_type(self, slug: str) -> bool:
        """Say a board is a different kind of board -- from its own sheet."""
        slug, _record = await self._which_board(slug)
        if not slug:
            return False
        await self.esp.change_board_type(slug)
        return True

    async def menu_edit_board(self, slug: Optional[str] = None,
                              start_items: Optional[List[Dict[str, Any]]]
                              = None) -> bool:
        """Change what is wired to a board, without a cable.

        `start_items` opens the pin screen on a SAVED LAYOUT ("Use on a
        board"); nothing is applied until it is saved, and everything after
        the save is this method's own bookkeeping, unchanged.

        Settings rather than Add device: this changes something that already
        exists. The pin screen opens PRE-FILLED, the new template goes out
        over the broker, and the board restarts into it.

        The reconciliation afterwards is the part worth reading. Tasmota
        identifies devices by POSITION, so removing the first relay shifts
        every relay behind it down one and `switch.kettle_2` becomes
        `switch.kettle_1`. Every entity id stored against this board is
        therefore untrustworthy the moment the template changes. Matching is
        redone from scratch, by name; trusting the stored ids would leave
        each device quietly pointing at its neighbour.
        """
        await self._io.write("\n--- EDIT A BOARD ---")
        slug, _record = await self._which_board(slug)
        if not slug:
            return False

        result = await self.esp.edit_board(slug, start_items=start_items)
        if result is None:
            return False

        # Devices whose pin is gone go FIRST, so re-matching never has to
        # consider a name the board no longer has.
        for name in result["gone"]:
            did = self.dm.board_device_id(slug, name)
            scenes = self._scenes_using(did)
            if scenes:
                await self._io.write(
                    f"  ! {name} was used by {len(scenes)} scene(s): "
                    f"{', '.join(scenes)}. Those scenes will skip it.",
                    level="warn")
            if await asyncio.to_thread(self.dm.remove, did):
                await self._io.write(f"  Unregistered {name}.")

        if result["added"]:
            await self._io.write(
                f"  New on the board: {', '.join(result['added'])}")

        # A board that has never been INSTALLED has no devices registered
        # yet: the edit changed a design on paper and the install registers
        # what it carries.
        #
        # BELOW the unregistration, not above it, so a device dropped from
        # a designed board never stays registered.
        if not (self.esp._read_board(slug) or {}).get("flashed"):
            if start_items is None:
                await self._io.write(
                    "  The design is updated. Install the board and its "
                    "devices register then.")
                return True
            # A LAYOUT PUT ON A BOARD NEVER INSTALLED is installed now:
            # layout, then flash -- nothing to come back for.
            if not await self.esp.flash(slug):
                name = (self.esp._read_board(slug) or {}).get("name", slug)
                self.checkpoint_note = (
                    f"Saved -- {name} could not be installed now. It stays "
                    f"in Notifications.")
                await self._io.write(f"  {self.checkpoint_note}")
                return True
            await self.boards_reg.register_board(slug)
            return True

        # THE WIRING HAS LANDED, and the registry follows on its own: new
        # devices are registered plainly, a changed part is re-fitted, and
        # nobody is asked anything. This hub flashed the board, so this hub
        # decides what it carries -- Home Assistant is not consulted (her
        # ruling, 2026-10-01). The "rewire question" this used to leave in
        # Notifications -- keep, redo, set them up again -- is gone.
        return await self.boards_reg.settle_after_edit(slug, result)

    # ── saved layouts ───────────────────────────────────────────────────
    #
    # The Devices screen's Layouts section shows these. A layout is a wiring
    # plan kept under a name for one chip family; it controls nothing, and
    # only its owner deletes it (see EspBuilder's "saved layouts").

    def _layout_records(self) -> List[Dict[str, Any]]:
        out: List[Dict[str, Any]] = []
        for rec in self.esp.list_layouts():
            # UNFINISHED WORK IS NOT A LAYOUT (2026-11-04): a board waiting
            # for its install, a design stopped halfway, an edit that never
            # reached its board -- each waits in Notifications instead.
            if self.esp.is_pending(rec):
                continue
            board = chips.get(rec.get("board"))
            items = rec.get("items") or []
            status, on_name = self.esp.layout_view(rec)
            out.append({
                # flashed | waiting | unfinished | not_sent | saved -- and
                # the ESP board it is the wiring of, when it is one's.
                "status": status,
                "on_board": self.esp.layout_is_boards(rec) or "",
                "board_name": on_name,
                "id": rec.get("id"),
                "name": rec.get("name") or "Layout",
                "board": board.id,
                "board_label": board.label,
                "chip": board.family_label,
                "family": board.family,
                "count": len(items),
                # The same rows the board's own sheet draws: each part, its
                # pins, the Tasmota component on each.
                "parts": self._board_parts({"items": items}),
                "created": rec.get("created", ""),
                "updated": rec.get("updated", ""),
                "source": rec.get("source", ""),
            })
        return out

    async def menu_list_layouts(self) -> bool:
        if self._io.renders_data:
            # The phone shows layouts inside Devices, from the same frame.
            await self.menu_list_devices()
            return True
        records = self._layout_records()
        await self._io.write("\n--- SAVED LAYOUTS ---")
        if not records:
            await self._io.write("No saved layouts.")
        for i, rec in enumerate(records, 1):
            await self._io.write(
                f"\n  {i}. {rec['name']}  ({rec['chip']}, "
                f"{rec['count']} device(s))")
            for part in rec["parts"]:
                pins = ", ".join(f"{r['slot']} on GPIO{r['gpio']}"
                                 for r in part.get("pins", [])) \
                    if isinstance(part.get("pins"), list) else ""
                await self._io.write(f"       - {part['name']}: "
                                     f"{part['part']} {pins}".rstrip())
        return True

    async def menu_new_layout(self) -> bool:
        if await self.esp.new_layout() is None:
            raise CancelledByUser()
        return True

    async def menu_edit_layout(self, layout_id: str) -> bool:
        rec = self.esp._read_layout(layout_id)
        if rec is None:
            self.failure_note = ("That layout is not here any more -- the "
                                 "list may be out of date.")
            return False
        # A board's own layout is that board's wiring: editing it IS editing
        # the board (pushed to it, registry kept straight), so the two can
        # never disagree.
        slug = self.esp.layout_is_boards(rec)
        if slug:
            return await self.menu_edit_board(slug)
        if await self.esp.edit_layout(layout_id) is None:
            raise CancelledByUser()
        return True

    async def menu_rename_layout(self, layout_id: str) -> bool:
        if self.esp._read_layout(layout_id) is None:
            self.failure_note = ("That layout is not here any more -- the "
                                 "list may be out of date.")
            return False
        if await self.esp.rename_layout(layout_id) is None:
            raise CancelledByUser()
        return True

    async def menu_drop_layout(self, layout_id: str) -> bool:
        """Delete one saved layout. The ONLY thing that ever does: no flash,
        edit or board removal touches the library. The app asks "are you
        sure" before sending this."""
        rec = self.esp._read_layout(layout_id)
        if rec is None:
            self.failure_note = ("That layout is already gone -- the list "
                                 "may be out of date.")
            return False
        if self.esp.layout_is_boards(rec):
            self.failure_note = (
                f"{rec.get('name')} is the wiring of a board that is set "
                f"up, so it stays while the board does. Retire the board "
                f"and its wiring stays here as a plan you can delete.")
            await self._io.write(f"  {self.failure_note}", level="warn")
            return False
        if not self.esp.delete_layout(layout_id):
            self.failure_note = "The layout could not be deleted."
            return False
        await self._io.write(f"  Deleted the layout {rec.get('name')!r}.")
        return True

    def _layout_notice(self, rec: Dict[str, Any]) -> Dict[str, Any]:
        """The notice a pending layout owns: Continue picks it up, Delete
        forgets it. Nothing on any board changes until Continue."""
        slug = rec.get("for_board") or ""
        board = (self.esp._read_board(slug) or {}) if slug else {}
        board_name = board.get("name") or slug
        if rec.get("state") == "not_sent" and board_name:
            title = f"Send {board_name} its new wiring?"
            body = (f"{board_name}'s new wiring never reached it, so it still "
                    f"has its old one. Continue sends it; delete forgets it.")
        elif board_name:
            title = f"Finish editing {board_name}?"
            body = (f"An edit of {board_name} was stopped halfway; the board "
                    f"still has its old wiring. Continue picks it up.")
        else:
            title = f"Finish {rec.get('name') or 'the new board'}?"
            body = ("A new board's design was stopped halfway. Continue picks "
                    "it up and installs it; delete forgets it.")
        return self._notice(f"layout:{rec.get('id')}", title, body,
                            kind="menu", value=f"resumelayout:{rec.get('id')}",
                            label="Continue / delete")

    async def menu_resume_layout(self, layout_id: str) -> bool:
        """A pending design or edit, from its notice: Continue or delete."""
        rec = self.esp._read_layout(layout_id)
        if rec is None or rec not in self.esp.pending_layouts():
            await self._io.send_data("notice_gone", {"id": f"layout:{layout_id}"})
            await self._io.write("  That is no longer waiting.")
            return True
        slug = rec.get("for_board") or ""
        board = self.esp._read_board(slug) if slug else None
        picked = await self._io.choose(
            f"{rec.get('name') or 'This wiring'} -- continue or delete?",
            [{"value": "continue", "label": "Continue",
              "sub": ("sends the new wiring to the board"
                      if board is not None else
                      "picks the design up, then installs the board")},
             {"value": "delete", "label": "Delete it",
              "sub": "forgets this wiring; no board changes"}])
        if not picked:
            raise CancelledByUser()
        if picked[0] == "delete":
            self.esp.delete_layout(layout_id)
            await self._io.send_data("notice_gone", {"id": f"layout:{layout_id}"})
            await self._io.write("  Deleted.")
            return True
        items = rec.get("items") or []
        await self._io.send_data("notice_gone", {"id": f"layout:{layout_id}"})
        if board is not None:
            # Taken off the list first: a run that fails again keeps ONE
            # pending edit for this board (esp._pending_for), not two.
            self.esp.delete_layout(layout_id)
            return await self.menu_edit_board(slug, start_items=items)
        if not await self.esp.add_node(layout_id):
            return False
        self.esp.delete_layout(layout_id)
        await self._flash_and_register(self.esp.last_node)
        return True

    async def menu_use_layout(self, layout_id: str) -> bool:
        """Put a saved layout on one of your boards of the SAME chip family.
        It opens the pin screen on the layout; saving it is an ordinary edit
        (pushed to the board, registry kept straight), and backing out
        changes nothing."""
        rec = self.esp._read_layout(layout_id)
        if rec is None:
            self.failure_note = ("That layout is not here any more -- the "
                                 "list may be out of date.")
            return False
        board = chips.get(rec.get("board"))
        mine = [(slug, r) for slug, r in self.esp._boards()
                if self.esp._board_of(r).family == board.family]
        # A NEW BOARD IS ALWAYS AN ANSWER: plug it in and it is installed
        # with this layout. With no board of this chip yet it is the only
        # answer, so nothing is asked (it used to refuse, 2026-11-02).
        new = "\x00new"
        rows = [{"value": new, "label": "A new board",
                 "sub": "plug it into the hub -- it is installed with "
                        "this layout"}]
        rows += [{"value": s, "label": r.get("name", s),
                  "sub": f"{len(r.get('items', []))} device(s) now, "
                         f"{self.esp.board_state(r)}"} for s, r in mine]
        slug = new if not mine else await self._choose_or_cancel(
            f"Which {board.family_label} board gets {rec.get('name')!r}? "
            f"An existing one has its wiring replaced when you save.", rows)
        if slug == new:
            if not await self.esp.add_node(layout_id):
                return False
            await self._flash_and_register(self.esp.last_node)
            return True
        return await self.menu_edit_board(slug,
                                          start_items=rec.get("items") or [])

    @staticmethod
    def _setup_stage(record: Dict[str, Any]) -> Optional[str]:
        """Where an interrupted board setup stopped, or None when it is
        done (or was never started). Read off the record's own facts --
        never remembered separately, so it cannot lie."""
        if not record:
            return None
        if not record.get("flashed"):
            return "flash"
        if record.get("network_pending"):
            return "network"
        if record.get("register_pending"):
            return "register"
        return None

    def _setup_notice(self, slug: str,
                      record: Dict[str, Any]) -> Dict[str, Any]:
        """The ONE notification an interrupted setup owns. Continue picks
        up from the exact stage; Delete -- offered inside the flow it
        opens -- is the only thing that destroys the work."""
        name = record.get("name", slug)
        stage = self._setup_stage(record) or "flash"
        what = {
            "flash": "designed -- it still needs to be plugged in once "
                     "and installed",
            "network": "installed, but never told its network",
            "register": "installed -- its devices have not appeared in "
                        "Home Assistant yet",
        }[stage]
        return self._notice(
            f"setup:{slug}",
            f"Continue setting up {name}?",
            f"{name} is {what}. Continue from exactly there, or delete "
            f"it -- nothing is lost until you choose delete.",
            kind="menu", value="resume_setup", label="Continue / delete")

    async def menu_resume_setup(self) -> bool:
        """The checkpoint's own door: Continue from the exact stage, or
        Delete everything. The ribbon law (2026-08-25): a setup that gets
        interrupted -- unreachable board, a cancel, a closed app -- parks
        HERE, and only the Delete in this flow ever erases the work."""
        pending = [(s, r) for s in self.esp.list_nodes()
                   if self._setup_stage(r := self.esp._read_board(s) or {})
                   is not None]
        if not pending:
            # Informational refusal, not a completion -- returning True
            # here put the Done window over "nothing was here to resume".
            await self._io.write("  No board is mid-setup.")
            self.failure_note = "No board is mid-setup -- nothing to resume."
            return False
        if len(pending) == 1:
            slug, record = pending[0]
        else:
            slug = await self._choose_or_cancel(
                "Which board?",
                [{"value": s, "label": r.get("name", s),
                  "sub": self.esp.board_state(r)} for s, r in pending])
            record = dict(pending)[slug]
        name = record.get("name", slug)
        stage = self._setup_stage(record)

        picked = await self._io.choose(
            f"{name} -- continue or delete?",
            [{"value": "continue", "label": "Continue",
              "sub": {"flash": "plug it in -- installation starts now",
                      "network": "sends its network settings over the "
                                 "cable, nothing else",
                      "register": "checks Home Assistant for its devices "
                                  "and registers them"}[stage]},
             {"value": "delete", "label": "Delete it",
              "sub": "erases EVERYTHING -- design, progress, accounts. "
                     "The only path that destroys work."},
             {"value": "leave", "label": "Leave it for now", "sub": ""}])
        # Three different endings for three different gestures. Backing out
        # is a cancel. "Leave it for now" is the CHECKPOINT kept on purpose
        # -- neither done nor failed, so it reports as checkpointed (quiet
        # bookmark banner), never as the Done window it used to throw.
        if not picked:
            raise CancelledByUser()
        if picked[0] == "leave":
            await self._io.write("  Left as it was.")
            self.checkpoint_note = (
                f"Left as it was -- {name} stays in Notifications.")
            return True

        if picked[0] == "delete":
            return await self.menu_retire_board(slug, quiet=True)

        # Continue -- from EXACTLY the stage the record names.
        if stage in ("flash", "network"):
            # No /api/states snapshot: register_board does not read
            # `before`. See its docstring (2026-09-14).
            if not await self.esp.flash(slug):
                self.checkpoint_note = (
                    f"Saved again -- {name} could not be reached. It "
                    f"stays in Notifications.")
                await self._io.write(f"  {self.checkpoint_note}")
                return True

        # stage == "register" NEEDS NO HOME ASSISTANT. This refused with
        # "Home Assistant is not connected" -- but register_board builds
        # our own boards from their plan, with HA out of the story since
        # 2026-09-01, so a hub without HA could never finish a board parked
        # at this stage.
        #
        # THE SNAPSHOT THAT USED TO BE HERE IS GONE (2026-09-14), and it
        # was the worst-placed of the three: register_board does not read
        # `before`, so this fetched every entity in the house over REST,
        # filtered them, and threw the result away -- and when HA answered
        # slowly, `for e in None` raised TypeError out of that
        # comprehension, which run_menu_action reported as "Continue setup
        # stopped unexpectedly" and which left a perfectly good board
        # parked in Notifications.
        await self.boards_reg.register_board(slug)
        return True

    async def menu_resume_rewire(self) -> bool:
        """LEGACY DOOR, kept so an old "Decide now" notification still on a
        phone does something sensible. The rewire question no longer exists:
        any board still carrying one is settled here, quietly, exactly as
        an Edit settles it now, and the mark is cleared."""
        await self._settle_legacy_rewires()
        return True

    async def _settle_legacy_rewires(self) -> int:
        """Boards parked on the old rewire question by an earlier version:
        register what is new, re-fit what changed, clear the mark."""
        settled = 0
        for slug in self.esp.boards_with_pending_rewire():
            pending = self.esp.rewire_pending(slug) or {}
            try:
                await self.boards_reg.settle_after_edit(slug, {
                    "added": pending.get("added") or [],
                    "changed": pending.get("changed") or []})
            except Exception:                      # noqa: BLE001
                traceback.print_exc()
                continue
            self.esp.clear_rewire_pending(slug)
            await self._io.send_data("notice_gone", {"id": f"rewire:{slug}"})
            settled += 1
        return settled

    async def menu_change_wifi(self) -> bool:
        """Change the Wi-Fi the ESP nodes join. Reached from Settings.

        The Wi-Fi belongs to the HUB, not to any one board -- one network,
        one secrets.yaml, every node. It used to be reachable only as the
        first question of "build a new ESP board", so changing a password
        meant starting to describe hardware you did not want.
        """
        return await self.esp.change_wifi()

    async def menu_show_esp_key(self, slug: Optional[str] = None) -> bool:
        """Show ONE BOARD's credentials, on request.

        GATED, because it is a credential.

        The name is unchanged so that nothing else has to be touched, but
        what it shows is not: there is no shared key any more. The old
        design gave every board the same ESPHome key, which meant a board
        unscrewed from a wall was a key to the whole house. Each board now
        has its own, so the board is asked for first -- or named by the
        caller: the app's board sheet passes the slug under the finger
        (`boardkey:<slug>`), the terminal still gets the picker.

        TWO SIGN-INS, AND EACH HAS ITS OWN USERNAME (2026-09-11, "the
        username was hv-kitchen but it's admin that worked"). The popup
        said "Username: hv-kitchen" once, above both passwords, and that is
        the BROKER's username; the board's web page is Tasmota's, and
        Tasmota's web login is always `admin`. A person reading the popup
        took the one username shown to the one login they were trying, and
        was refused. Each password now carries its own username beside it.

        Almost nobody should need this -- the hub provisions boards itself.
        It is here for a board being pointed at a second Home Assistant, or
        for signing in by hand when something has gone wrong.
        """
        await self._io.write("\n--- BOARD CREDENTIALS ---")
        slug, record = await self._which_board(slug)
        if not slug:
            return False
        name = record.get("name") or slug
        credentials = self.esp.credentials(slug)
        if not credentials.get("password"):
            await self._io.write(
                "That board has no credentials yet -- they are generated "
                "when it is first set up.", level="warn")
            return False
        web = credentials.get("web_password") or ""
        await self._io.send_data("secret", {
            "title": f"{name} -- passwords",
            "note": f"Two sign-ins, two usernames. The board's OWN WEB PAGE "
                    f"(its IP address in a browser) takes user 'admin' with "
                    f"the web page password. The MESSAGE BROKER is what the "
                    f"board itself signs in to, as {credentials['user']!r}, "
                    f"answering on the topic {credentials['topic']!r}. Both "
                    f"belong to THIS BOARD ALONE -- the others have their "
                    f"own, so this one can be revoked without touching them.",
            "value": web or credentials["password"],
            # PASSWORDS ONLY IN MASKED FIELDS. They used to ride in `items`
            # as plain lines beside the masked `value`, so the eye button
            # hid the MQTT password on one line and printed it on another
            # (app audit, 2026-09-02). `secrets` is additive -- an older
            # app shows `value` alone.
            "secrets": [
                *([{"label": "Web page password (user: admin)",
                    "value": web}] if web else []),
                {"label": f"MQTT password (user: {credentials['user']})",
                 "value": credentials["password"]},
            ],
            "items": [
                "Web page: user admin"
                + ("" if web else " -- no web password set"),
                f"Broker: user {credentials['user']}",
                f"Topic: {credentials['topic']}",
            ],
            "items_label": "Signs in as",
        })
        event("ESP", f"credentials shown for {slug!r}")
        return True

    async def _prove_current_pin(self, user_id: str, label: str) -> str:
        """The current PIN, or the recovery words instead.

        Returns "pin" when the current PIN was entered, "recovered" when
        the person took the forgotten-PIN road and a new PIN is now set
        (menu_recover_pin did it), "" when neither worked. Backing out
        raises CancelledByUser like every other question."""
        way = await self._choose_or_cancel(f"{label}: your current PIN", [
            {"value": "pin", "label": "Enter my current PIN"},
            {"value": "forgot", "label": "I forgot my PIN",
             "sub": "use my five recovery words, then choose a new PIN"}])
        if way == "forgot":
            return "recovered" if await self.menu_recover_pin() else ""
        for attempt in range(3):
            pin = (await self._io.get_input("  Current PIN: ",
                                            secret=True)).strip()
            ok, reason = await asyncio.to_thread(
                self.fallback.pin.verify, user_id, pin)
            if ok:
                return "pin"
            if reason == "locked_out":
                wait = self.fallback.pin.get_lockout_remaining(user_id)
                self.failure_note = (f"Too many wrong PINs -- try again in "
                                     f"{max(1, wait // 60)} min, or use your "
                                     f"recovery words.")
                await self._io.write(f"  {self.failure_note}", level="warn")
                return ""
            if attempt < 2:
                await self._io.write("  That is not your current PIN -- try "
                                     "again.", level="warn")
        way = await self._choose_or_cancel("That PIN was not accepted", [
            {"value": "forgot", "label": "Use my recovery words",
             "sub": "then choose a new PIN"},
            {"value": "stop", "label": "Stop here"}])
        if way == "forgot":
            return "recovered" if await self.menu_recover_pin() else ""
        self.failure_note = "The current PIN was not accepted. Nothing changed."
        return ""

    async def menu_new_recovery(self) -> bool:
        """"Recovery words" on the Profile screen: YOUR PIN, THEN FIVE NEW
        WORDS.

        The words shown when the PIN was set cannot be shown again -- the
        hub keeps only a one-way hash of them, never the words (pin_auth).
        So this is the honest version of "show me my words": prove the
        current PIN, and five fresh words are made and shown once; the old
        ones stop working. Forgot the PIN too? The recovery road is offered
        right there."""
        await self._io.write("\n--- RECOVERY WORDS ---")
        me = (self.current_speaker_id or "").strip()
        if not me:
            self.failure_note = ("Nobody is identified on this phone, so "
                                 "there are no recovery words to show.")
            return False
        has_pin = await asyncio.to_thread(
            lambda: self.fallback.is_enrolled(me).get("pin"))
        if not has_pin:
            self.failure_note = ("You have no PIN yet -- set one and five "
                                 "recovery words are made with it.")
            return False
        await self._io.write(
            "  Your old words cannot be shown -- the hub never keeps them, "
            "only a fingerprint. Enter your PIN and you get five NEW words "
            "to write down; the old ones stop working.")
        proof = await self._prove_current_pin(me, "Recovery words")
        if proof == "recovered":
            return True
        if proof != "pin":
            return False
        words = await asyncio.to_thread(self.fallback.renew_recovery, me)
        if not words:
            self.failure_note = ("New words could not be made (the BIP-39 "
                                 "wordlist is missing, or the PIN store is "
                                 "not writable). Your old words still work.")
            await self._io.write(f"  {self.failure_note}", level="warn")
            return False
        await self._io.write(
            "RECOVERY PHRASE -- WRITE THIS DOWN NOW.\n"
            "It is shown ONCE and cannot be recovered if lost.\n"
            "Enter these words IN ORDER to reset a forgotten PIN:\n\n"
            "    " + "  ".join(words),
            level="important")
        try:
            await self._io.ainput("\n  Type OK once you've written it down: ",
                                  patient=True)
        except (EOFError, KeyboardInterrupt, CancelledByUser):
            pass            # the words are already saved; closing is fine
        return True

    async def menu_recover_pin(self) -> bool:
        """Reset a forgotten PIN with the recovery phrase.

        This is the other half of menu_setup_pin. That method printed five
        words under "WRITE THIS DOWN NOW -- shown ONCE and cannot be recovered
        if lost", and until now there was nowhere to type them: PinAuth
        implemented the entire verification and reset, and nothing reached it.
        """
        await self._io.write("\n--- FORGOT PIN ---")
        # WHOSE PIN. Over the network it is the person this handset is
        # bound to (ws_server seeds current_speaker_id from the binding);
        # the "User ID:" question stays only at the hub's own keyboard.
        # Asking it of every phone made this an ungated way to reset
        # ANYBODY's PIN given their words -- and, with the has_recovery
        # answer below, a free list of who exists.
        # Keyed on the BACKEND's trust as well as the phone id: any
        # connection that is not the hub's own keyboard answers for its
        # own person only, whether or not a phone id was stamped on it.
        if self.current_phone_id or not self._io.trusted_local:
            user_id = (self.current_speaker_id or "").strip()
            if not user_id:
                self.failure_note = ("This phone is not bound to anyone yet, "
                                     "so there is no PIN to recover from it.")
                return False
        else:
            user_id = (await self._io.get_input(
                "  User ID: ", patient=True)).strip()
        if not user_id:
            await self._io.write("  Empty user ID -- cancelled.")
            return False
        # NOT CHECKED UP FRONT any more: "no recovery phrase on file for X"
        # told the asker whether X exists before any attempt was spent.
        # pin_auth answers every failure as invalid_phrase; so does this.
        total = 5
        await self._io.write(
            f"  Enter the {total} words IN THE ORDER they were shown. "
            f"Take your time -- this question does not time out.")
        # patient=True: the answer is on a piece of paper somewhere, not in
        # the person's head. The ordinary reply window cancelled the reset
        # while they were still looking for it -- and a cancelled attempt
        # still burned one of the limited recovery attempts.
        phrase = (await self._io.get_input(
            "  Recovery phrase: ", patient=True, secret=True)).strip().lower()
        words = phrase.split()
        # Length is NOT validated here on purpose. PinAuth answers
        # "invalid_phrase" for every kind of failure so an attacker learns
        # nothing; telling them "wrong number of words" for free would undo
        # that, and it also consumes an attempt from the global budget, which
        # is what makes guessing expensive.
        new_pin = (await self._io.get_input(
            "  New PIN (digits only): ", patient=True, secret=True)).strip()

        ok, reason = await asyncio.to_thread(
            self.fallback.reset_pin_with_recovery, user_id, words, new_pin)
        event("VOICE", f"PIN recovery for {user_id!r}: {reason} -> {'PASS' if ok else 'FAIL'}")
        if ok:
            await self._io.write(f"  [OK] PIN reset for {user_id!r}. "
                                 f"Your recovery phrase is unchanged -- keep it.")
            return True
        # ONE ANSWER for "wrong words", "no such person", "no PIN" and "no
        # phrase": telling them apart is telling the asker who exists.
        # Only faults in the asker's own input, or in the hub, are named.
        await self._io.write(
            {"invalid_new_pin_format": "The new PIN must be digits, of the allowed length.",
             "bcrypt_unavailable": "Password hashing is unavailable on this machine."
             }.get(reason, "That phrase was not accepted. Attempts are limited."),
            level="warn")
        self.failure_note = "The PIN was not reset."
        return False

    # ── Profile: who you are, and renaming you ──────────────────────

    #: A person's name is a STORE KEY, so it obeys the same grammar
    #: everywhere it can be set. That instinct was right and the execution
    #: was not: "copied from menu_enroll_user" meant the same characters
    #: TYPED TWICE, in two files' worth of distance from each other, and a
    #: pair of literals is not one grammar -- it is two that happen to
    #: agree until somebody edits one.
    #:
    #: Both are gone. hv_common.person_name_problem is the single rule now,
    #: and it allows the space that neither of these did (2026-09-14).

    def _person_record(self, user_id: str) -> Dict[str, Any]:
        """One person, as facts rather than a sentence.

        Every field here is something a store can actually answer. Anything
        the hub cannot know is simply absent -- the screen then shows
        nothing instead of a confident guess, which is the whole reason
        this is a dict and not a printed line.
        """
        def fact(ask) -> bool:
            # Each store answers for itself; one that cannot says False.
            try:
                return bool(ask())
            except Exception:                      # noqa: BLE001
                return False

        rec: Dict[str, Any] = {
            "user_id": user_id,
            "has_voice": fact(lambda: self.biometric_store.has_voice(user_id)),
            "has_pin": fact(lambda: self.fallback.is_enrolled(user_id).get("pin")),
            "has_recovery": False,
            "has_card": fact(lambda: self.cards.has_card(user_id)),
            # Recovery words held the OLD way (per-word HMACs) rather than
            # as one whole-phrase bcrypt -- re-setting the PIN upgrades them.
            "recovery_legacy": fact(
                lambda: self.fallback.pin.needs_recovery_upgrade(user_id)),
        }
        try:
            status = self.fallback.recovery_status(user_id) or {}
            rec["has_recovery"] = bool(status.get("has_recovery"))
            created = status.get("created_at")
            if created:
                # STRING, like every other number crossing this wire. The
                # app casts with `as String?` and an int arrives as null --
                # the bug that ate three rows off the AI review card.
                rec["recovery_created_at"] = str(int(float(created)))
        except Exception:                          # noqa: BLE001
            pass
        try:
            entry = self.people.get(user_id)
            if entry:
                rec["added_by"] = entry.get("added_by") or ""
                rec["added_at"] = str(int(entry.get("added_at") or 0))
        except Exception:                          # noqa: BLE001
            pass
        return rec

    async def menu_profile(self) -> bool:
        """Everything the Profile screen shows, as DATA.

        Read-only and ungated: it reveals no secret. It says a PIN EXISTS,
        never what it is; it says recovery words exist and when they were
        minted, never the words -- they were shown once and are held as
        HMACs, so nothing here could reveal them even if it wanted to.

        The whole record goes over in one frame because the screen is one
        screen. Two round trips would let it render half a person.
        """
        me = (self.current_speaker_id or "").strip()
        # YOUR OWN RECORD, and the household as NAMES. The full
        # has_pin/has_recovery/has_card record of every person used to go
        # to any phone that asked, ungated -- a roster of who exists and
        # which of them has no PIN yet. The names are what the screen needs
        # to say who is here; the facts about a person belong to them.
        names = set()
        try:
            names |= set(self.biometric_store.list_users() or [])
        except Exception:                          # noqa: BLE001
            pass
        names |= set(self.fallback.pin.pin_db)
        you = self._person_record(me) if me else None
        people = [you if uid == me and you else {"user_id": uid}
                  for uid in sorted(names)]

        phone: Optional[Dict[str, Any]] = None
        phones_of_mine: List[Dict[str, str]] = []
        if self.current_phone_id:
            try:
                from identity import phones as _phones
                registry = _phones.PhoneRegistry()
                record = registry.get(self.current_phone_id)
                if record:
                    phone = {
                        "id": record.get("id", ""),
                        "name": record.get("name", ""),
                        "device": record.get("device", ""),
                        "paired_at": str(int(record.get("paired_at") or 0)),
                        "user_id": record.get("user_id", ""),
                    }
                # The handsets bound to YOU, by name only.
                if me:
                    for pid in registry.phones_for_user(me):
                        rec = registry.get(pid) or {}
                        # `device` RIDES ALONG (audit, 2026-09-12). The
                        # screen builds each handset's label from name AND
                        # device, and only name was ever sent -- so two
                        # unnamed phones both read "unnamed phone" and the
                        # list could not do the one job it exists for,
                        # which is showing that a reinstall or a second
                        # handset is on your account.
                        phones_of_mine.append({
                            "id": pid,
                            "name": rec.get("name", ""),
                            "device": rec.get("device", ""),
                        })
            except Exception:                      # noqa: BLE001
                phone = None

        await self._io.send_data("profile", {
            "me": me,
            "you": you,
            "phone": phone,
            "phones": phones_of_mine,
            "people": people,
        })

        # THE TERMINAL gets the same facts in words, and only the terminal.
        # It has no screen to render into, and a management action that
        # prints nothing there reads as a hang. A phone already drew the
        # frame above, so printing here would say everything twice.
        if not self._io.renders_data:
            await self._io.write("\n--- PROFILE ---")
            if not me:
                await self._io.write(
                    "  Nobody is identified on this connection.")
            for rec in people:
                marks = []
                if rec.get("has_voice"):
                    marks.append("voice")
                if rec.get("has_pin"):
                    marks.append("PIN")
                if rec.get("has_recovery"):
                    marks.append("recovery")
                who = rec["user_id"] + (" (you)" if rec["user_id"] == me else "")
                detail = (' · '.join(marks) or 'nothing set') if rec is you else ""
                if rec is you and rec.get("recovery_legacy"):
                    detail += " (re-set your PIN to upgrade the recovery words)"
                await self._io.write(f"  {who:<24} {detail}")
        return True

    async def menu_rename_user(self) -> bool:
        """Change the name of the person now speaking, in every store.

        THIS IS THE ONE THAT CAN LOCK SOMEONE OUT OF THEIR OWN HOUSE.

        A name is not a label here -- it is the KEY, in SIX places:

            1. the voiceprint store   (voice_security, probed by capability)
            2. the PIN database       (pin_auth.pin_db)
            3. the recovery set       (pin_auth.recovery_data)
            4. every phone binding    (phones.PhoneRegistry.user_id)
            5. the enrolment journal  (people.json, INCLUDING added_by)
            6. their cards            (pin_auth.CardStore, cards.json)

        The first four lock somebody out if missed. Five and six do not --
        they record and they label -- so they are moved last and their
        failure is logged rather than rolled back.

        Move four and miss one and the person is gone in a way nothing
        reports: the voice resolves to a name the PIN db never heard of, so
        the front door fails -- and the PIN fallback fails too, because it
        is looked up under the same missing key. Two doors, both shut, by a
        spelling correction.

        So the order is chosen for ROLLBACK, not for tidiness: the
        voiceprint is COPIED first and the original is deleted LAST. Until
        that final delete, every failure leaves the old person completely
        intact and the only cleanup needed is dropping a copy.
        """
        old = (self.current_speaker_id or "").strip()
        if not old:
            self.failure_note = ("Nobody is identified on this connection, "
                                 "so there is no name to change.")
            return False

        raw = await self._io.get_input(
            "  New name: ",
            meta={"hint": "spaces are fine -- anything but / \\ : or '..'"})
        if means_cancel(raw):
            raise CancelledByUser()
        # NORMALISED BEFORE ANYTHING IS COMPARED OR KEYED (2026-09-14:
        # "allow any name writeup because spacebar was rejected during name
        # change"). The old rule was `[A-Za-z0-9._-]{1,64}` -- no space, so
        # nobody here could be called "Augusta Chinenye". See
        # hv_common.person_name_problem for why widening it is safe: a user
        # id is a JSON key in every store that holds one, never a filename.
        #
        # Collapsing internal whitespace matters as much as allowing it:
        # "Augusta  Chinenye" and "Augusta Chinenye" must not be able to
        # become two people with two PINs.
        new = normalise_person_name(raw)
        if not new:
            self.failure_note = "No new name was given, so nothing changed."
            return False
        if new == normalise_person_name(old):
            self.failure_note = f"{new!r} is already your name."
            return False
        problem = person_name_problem(new)
        if problem:
            self.failure_note = problem
            return False

        # EVERY STORE ASKED BEFORE ANY STORE IS TOUCHED.
        try:
            taken = set(self.biometric_store.list_users() or [])
        except Exception:                          # noqa: BLE001
            taken = set()
        pin_store = self.fallback.pin
        taken |= set(pin_store.pin_db) | set(pin_store.recovery_data)
        if new in taken:
            self.failure_note = (f"Somebody on this hub is already called "
                                 f"{new!r}. Pick another name.")
            return False

        # ── 1. the voiceprint: COPY, verify, and only delete at the end ──
        try:
            embedding = self.biometric_store.get_voice(old)
        except Exception:                          # noqa: BLE001
            embedding = None
        if embedding is None:
            self.failure_note = (f"No voiceprint is stored for {old!r}, so "
                                 f"there is nothing to move. Nothing was "
                                 f"changed.")
            return False

        try:
            self.biometric_store.save_voice(new, embedding)
        except Exception as exc:                   # noqa: BLE001
            event("PROFILE", f"rename {old!r}->{new!r} ABORTED: voiceprint "
                             f"copy failed ({exc})")
            self.failure_note = ("The voiceprint could not be copied to the "
                                 "new name. Nothing was changed.")
            return False

        # PROVE THE COPY before anything irreversible happens. A writer that
        # silently no-ops would otherwise take the old voiceprint with it.
        try:
            written = self.biometric_store.get_voice(new)
            moved_voice = (written is not None
                           and np.allclose(np.asarray(written, dtype=float),
                                           np.asarray(embedding, dtype=float)))
        except Exception:                          # noqa: BLE001
            moved_voice = False
        if not moved_voice:
            event("PROFILE", f"rename {old!r}->{new!r} ABORTED: the copied "
                             f"voiceprint did not read back")
            self._erase_person(new)
            self.failure_note = ("The copied voiceprint did not read back "
                                 "correctly, so the rename was abandoned. "
                                 "Nothing was changed.")
            return False

        # ── 2 & 3. PIN, recovery, lockouts -- one persist, or nothing ────
        ok, why = pin_store.rename_user(old, new)
        if not ok:
            event("PROFILE", f"rename {old!r}->{new!r} ABORTED at the PIN "
                             f"store ({why})")
            self._erase_person(new)
            self.failure_note = ("Your PIN could not be moved to the new "
                                 "name, so the rename was undone. Nothing "
                                 "was changed.")
            return False

        # ── 4. every phone bound to this person ─────────────────────────
        try:
            from identity import phones as _phones
            moved_phones = _phones.PhoneRegistry().rename_user(old, new)
        except Exception as exc:                   # noqa: BLE001
            # ROLL THE PIN BACK. This is the one step whose failure would
            # otherwise leave the split brain: person renamed, handset
            # still pointing at a name nothing answers to.
            event("PROFILE", f"rename {old!r}->{new!r} ABORTED at the phone "
                             f"binding ({exc}) -- rolling the PIN store back")
            pin_store.rename_user(new, old)
            self._erase_person(new)
            self.failure_note = ("Your phone's binding could not be updated, "
                                 "so the rename was undone. Nothing was "
                                 "changed.")
            return False

        # ── 5. the ledger, including everyone this person sponsored ─────
        try:
            self.people.rename(old, new)
        except Exception as exc:                   # noqa: BLE001
            # NOT fatal and NOT rolled back: the journal records history,
            # gates nothing, and unwinding four good stores over a lost
            # line of it would be the worse trade by far.
            event("PROFILE", f"rename {old!r}->{new!r}: the journal could "
                             f"not be updated ({exc}) -- history may still "
                             f"say {old!r}")

        # ── 6. their cards ──────────────────────────────────────────────
        # NOT fatal and NOT rolled back, for the journal's reason exactly:
        # a card gates nothing (it cannot open a secure device, because no
        # automation can), so a card left behind costs one re-tap, while
        # unwinding five good stores over it would be far worse.
        try:
            self.cards.rename_user(old, new)
        except Exception as exc:                   # noqa: BLE001
            event("PROFILE", f"rename {old!r}->{new!r}: cards could not be "
                             f"moved ({exc}) -- enrol the card again")

        # ── 7. what they made ───────────────────────────────────────────
        # Their scenes and automations, and the "changed by" on others'.
        # Not fatal: a maker left behind only mislabels a row.
        try:
            if self.sm is not None:
                self.sm.rename_owner(old, new)
            if getattr(self, "automations", None) is not None:
                self.automations.rename_owner(old, new)
        except Exception as exc:                   # noqa: BLE001
            event("PROFILE", f"rename {old!r}->{new!r}: scenes/automations "
                             f"still name {old!r} ({exc})")

        # ── the old voiceprint goes LAST, once everything else has moved ─
        try:
            self.biometric_store.delete_user(old)
        except Exception as exc:                   # noqa: BLE001
            event("PROFILE", f"WARNING: {old!r} renamed to {new!r} "
                             f"but the old voiceprint could not be "
                             f"erased ({exc}) -- remove it by hand")

        # ── the live caches, which no file rebuild will fix mid-session ──
        try:
            self.sv.sync_cache(new, embedding)
        except Exception:                          # noqa: BLE001
            pass
        # remove_user drops BOTH the cosine cache and the sherpa manager's
        # entry. (The capability probe this replaces looked for method
        # names SherpaSV never had, so the old name was never evicted.)
        try:
            self.sv.remove_user(old)
        except Exception:                          # noqa: BLE001
            pass
        # THE SUDO WINDOW MOVES WITH THE PERSON. Left behind, the window
        # they just proved themselves in would keep vouching for a name
        # that no longer exists, and they would be re-challenged for the
        # very next action.
        grace = self._gate_grace.pop(self._grace_key(old), None)
        if grace is not None:
            self._gate_grace[self._grace_key(new)] = grace
        self.current_speaker_id = new

        event("PROFILE", f"renamed {old!r} -> {new!r} "
                         f"(voice, PIN, recovery, {len(moved_phones)} phone(s), "
                         f"journal)")
        await self._io.write(f"\n  [OK] You are now {new!r}.")
        await self._io.write("  Your PIN and your recovery words still work, "
                             "unchanged.")
        return True

    # ── Taking someone out of the house (2026-10-11) ───────────────

    async def _take_out_person(self, user: str, by: str) -> None:
        """Everything that lets `user` in, gone: voice, PIN, recovery words,
        cards, hearing fixes, check windows. The journal keeps that they were
        here, and who took them out (themselves, when they left)."""
        await asyncio.to_thread(self._erase_person, user, True)
        fixes = getattr(self, "hearing_fixes", None)
        if fixes is not None:
            try:
                fixes.forget_user(user)
            except Exception as exc:               # noqa: BLE001
                event("PEOPLE", f"{user!r}'s hearing fixes not removed ({exc})")
        try:
            self.cards.remove_all(user)
        except Exception as exc:                   # noqa: BLE001
            event("PEOPLE", f"{user!r}'s cards not removed ({exc})")
        try:
            self.people.mark_removed(user, by)
        except Exception as exc:                   # noqa: BLE001
            event("PEOPLE", f"journal: {user!r} removal not recorded ({exc})")
        for key in [k for k in self._gate_grace if k[1] == user]:
            self._gate_grace.pop(key, None)
        for key in [k for k in self._voice_ok_at if k[1] == user]:
            self._voice_ok_at.pop(key, None)
        try:
            self.dm.clear_context(user)
        except Exception:                          # noqa: BLE001
            pass

    async def menu_leave_house(self, how: str = "") -> bool:
        """Leave this house yourself (Profile, Leave this house).

        "keep": your voice, PIN, recovery words, cards and phones' sign-in
        go, and your "just mine" scenes. What you made for everyone stays,
        for everyone; devices and settings stay. When you were the last
        person, pairing needs the hub's button from then on.
        "clear" (the LAST person only): the whole hub, as new -- see
        _clear_whole_hub. Your own PIN first; nothing changes before it."""
        me = self.current_speaker_id or ""
        if not me:
            self.failure_note = ("This hub does not know who is holding this "
                                 "phone, so it cannot sign you out.")
            return False
        clear = (how or "").strip().lower() == "clear"
        others = [p for p in self._household() if p != me]
        if clear and others:
            self.failure_note = ("Other people still live here, so the hub "
                                 "cannot be cleared. You can still leave.")
            return False
        ok, why = await self._pin_tries(
            me, "Your PIN, to clear the whole hub: " if clear
            else "Your PIN, to leave: ")
        if ok is None:
            raise CancelledByUser()
        if not ok:
            if not self.failure_note:
                self.failure_note = (why or "Your PIN was not accepted -- "
                                     "nothing changed.")
            return False
        if clear:
            return await self._clear_whole_hub(me)

        from identity import phones as _phones
        registry = _phones.PhoneRegistry()
        mine = registry.phones_for_user(me)      # before they are unbound
        sm, eng = self.sm, getattr(self, "automations", None)
        dropped = 0
        for sc in (sm.owned_by(me) if sm is not None else []):
            if sc.private:
                dropped += bool(await asyncio.to_thread(sm.remove, sc.name))
            else:
                await asyncio.to_thread(sm.hand_over, sc.name, "")
        for r in (eng.owned_by(me) if eng is not None else []):
            await asyncio.to_thread(eng.hand_over, r["id"], "")
        await self._take_out_person(me, me)
        if not others:
            # Nobody lives here now: every phone still paired goes too (one
            # left unbound would run first-time setup and own the house),
            # and pairing needs the hub's button.
            for rec in registry.list():
                pid = str(rec.get("id") or "")
                if pid and pid not in mine:
                    try:
                        registry.remove(pid)
                    except OSError as exc:
                        event("PEOPLE", f"phone {pid[:8]} not unpaired ({exc})")
            try:
                registry.need_button()
            except OSError as exc:
                event("PEOPLE", f"pairing-needs-button not written ({exc})")
            from core import config as _cfg
            if _cfg.PAIR_ON_BOOT:
                # A pair-on-boot hub: open pairing now, as a restart would,
                # so a phone can pair and register without one. The PIN
                # above is the person choosing it.
                try:
                    _phones.PairingWindow().open(
                        opened_by=_phones.PairingWindow.OPENED_BY_HUB)
                    event("PEOPLE", "last person left: pairing open for "
                                    f"{_phones.window_words()}")
                except OSError as exc:
                    event("PEOPLE", f"pairing not opened ({exc})")
        event("PEOPLE", f"{me!r} left the house ({dropped} \"just mine\" "
                        f"scene(s) deleted; {len(mine)} phone(s) signed out"
                        f"{'; nobody lives here now' if not others else ''})")
        await self._io.send_data("left_house", {"cleared": False})
        for pid in mine:
            try:
                registry.remove(pid)
            except OSError as exc:
                event("PEOPLE", f"phone {pid[:8]} not signed out ({exc})")
        self.current_speaker_id = ""
        return True

    async def _clear_whole_hub(self, me: str) -> bool:
        """The last person's "Clear the whole hub": every person, phone,
        device, board, scene, automation, camera recording and learned
        phrase, and Tailscale. Kept: the software, the models, this hub's own
        keys and certificate. Boards are told to wipe themselves now (while
        their logins still exist) and their broker accounts revoked;
        Tailscale is logged out; the files go at the next start
        (ws_server._clear_hub_if_asked), before anything is loaded, so
        nothing saved on the way out can bring them back. Then a restart."""
        await self._io.write("  Clearing the hub...")
        try:
            slugs = list(self.esp.list_nodes())
        except Exception as exc:                   # noqa: BLE001
            event("PEOPLE", f"clear: boards not listed ({exc})")
            slugs = []
        for slug in slugs:
            try:
                await self.esp.decommission(slug)
            except Exception as exc:               # noqa: BLE001
                event("PEOPLE", f"clear: {slug} not wiped ({exc})")
            try:
                await self.esp.revoke_access(slug)
            except Exception as exc:               # noqa: BLE001
                event("PEOPLE", f"clear: {slug}'s login not revoked ({exc})")
        try:
            from core import tailnet
            await asyncio.to_thread(tailnet.logout)
        except Exception as exc:                   # noqa: BLE001
            event("PEOPLE", f"clear: Tailscale not logged out ({exc})")
        from core import config as _cfg
        marker = os.path.join(os.path.dirname(_cfg.PHONES_FILE), "clear-hub")
        try:
            os.makedirs(os.path.dirname(marker), exist_ok=True)
            import json as _json
            with open(marker, "w", encoding="utf-8") as f:
                _json.dump({"by": me, "at": int(time.time())}, f)
        except OSError as exc:
            self.failure_note = f"The hub could not be cleared ({exc})."
            return False
        event("PEOPLE", f"{me!r} cleared the whole hub "
                        f"({len(slugs)} board(s) told to wipe); restarting")
        await self._io.send_data("left_house", {"cleared": True})
        import signal as _signal
        asyncio.get_running_loop().call_later(
            2.0, os.kill, os.getpid(), _signal.SIGTERM)
        return True

    async def menu_remove_person(self, user: str = "") -> bool:
        """Remove ANOTHER person: everything that lets them in, and what
        was only theirs. Confirmed first, then the remover's own PIN.

        GOES WITH THEM: their voice, their PIN and
        recovery words, their cards, their phones' sign-in, their letters,
        and their "just mine" scenes. ASKED ABOUT: the scenes and
        automations they made for everyone -- kept by default, and then
        they are the remover's. Nothing is changed before the PIN."""
        me = self.current_speaker_id or ""
        if not me:
            self.failure_note = ("This hub does not know who is holding this "
                                 "phone, so it cannot remove anyone.")
            return False
        others = [p for p in self._household() if p != me]
        if user == me:
            self.failure_note = ("You cannot remove yourself -- someone else "
                                 "in the house has to.")
            return False
        if not others:
            self.failure_note = ("You are the only person here -- there is "
                                 "nobody else to remove.")
            return False
        if user and user not in others:
            self.failure_note = (f"{user} is not one of the people here -- "
                                 f"the list may be out of date.")
            return False
        if not user:
            user = await self._choose_or_cancel("Remove who?", [
                {"value": p, "label": p, "sub": ""} for p in others])

        sm, eng = self.sm, getattr(self, "automations", None)
        theirs = sm.owned_by(user) if sm is not None else []
        own_only = [sc for sc in theirs if sc.private]
        shared = [sc for sc in theirs if not sc.private]
        rules = eng.owned_by(user) if eng is not None else []

        goes = ("their voice, PIN, recovery words and cards stop working, "
                "and their phones are signed out")
        if own_only:
            goes += (f"; their {len(own_only)} \"just mine\" scene"
                     f"{'s are' if len(own_only) != 1 else ' is'} deleted")
        sure = await self._choose_or_cancel(f"Remove {user} from this house?", [
            {"value": "yes", "label": f"Yes, remove {user}", "sub": goes},
            {"value": "no", "label": f"No, keep {user}", "sub": ""}])
        if sure != "yes":
            raise CancelledByUser()
        ok, why = await self._pin_tries(me, f"Your PIN, to remove {user}: ")
        if ok is None:
            raise CancelledByUser()
        if not ok:
            if not self.failure_note:
                self.failure_note = (why or f"Your PIN was not accepted -- "
                                     f"{user} was not removed.")
            return False

        # WHAT THEY MADE FOR EVERYONE: kept unless the remover says so.
        doomed_scenes: Set[str] = set()
        doomed_rules: Set[str] = set()
        if shared or rules:
            count = ", ".join(x for x in (
                f"{len(shared)} scene{'s' if len(shared) != 1 else ''}"
                if shared else "",
                f"{len(rules)} automation{'s' if len(rules) != 1 else ''}"
                if rules else "") if x)
            what = await self._choose_or_cancel(
                f"{user} made {count} that everyone uses", [
                    {"value": "keep", "label": "Keep them all",
                     "sub": "they become yours"},
                    {"value": "pick", "label": "Choose which to delete",
                     "sub": "the rest become yours"},
                    {"value": "delete", "label": "Delete them all", "sub": ""}])
            if what == "delete":
                doomed_scenes = {sc.name for sc in shared}
                doomed_rules = {r["id"] for r in rules}
            elif what == "pick":
                ticked = await self._io.choose(
                    "Tick the ones to delete",
                    [{"value": f"scene:{sc.name}", "label": sc.name,
                      "sub": "scene"} for sc in shared]
                    + [{"value": f"auto:{r['id']}",
                        "label": r.get("name", r["id"]),
                        "sub": "automation"} for r in rules],
                    multi=True)
                if not ticked:
                    raise CancelledByUser()
                doomed_scenes = {v[6:] for v in ticked if v.startswith("scene:")}
                doomed_rules = {v[5:] for v in ticked if v.startswith("auto:")}

        # ── and now it is done ───────────────────────────────────────
        removed_scenes = 0
        for sc in own_only + [sc for sc in shared if sc.name in doomed_scenes]:
            if await asyncio.to_thread(sm.remove, sc.name):
                removed_scenes += 1
        for sc in shared:
            if sc.name not in doomed_scenes:
                await asyncio.to_thread(sm.hand_over, sc.name, me)
        removed_rules = 0
        for r in rules:
            if r["id"] in doomed_rules:
                removed_rules += bool(await asyncio.to_thread(eng.remove, r["id"]))
            else:
                await asyncio.to_thread(eng.hand_over, r["id"], me)
        await self._take_out_person(user, me)

        kept = (len([sc for sc in shared if sc.name not in doomed_scenes])
                + len([r for r in rules if r["id"] not in doomed_rules]))
        said = f"{user} has been removed."
        if removed_scenes or removed_rules:
            said += (f" Deleted {removed_scenes} scene(s) and "
                     f"{removed_rules} automation(s).")
        if kept:
            said += f" {kept} thing(s) they made are now yours."
        event("PEOPLE", f"{user!r} removed by {me!r}: {removed_scenes} "
                        f"scene(s), {removed_rules} automation(s) deleted, "
                        f"{kept} handed over")
        await self._io.write(f"  [OK] {said}")
        if eng is not None and removed_scenes:
            finder = getattr(eng, "rules_using_scene", None)
            gone = [sc.name for sc in own_only
                    + [sc for sc in shared if sc.name in doomed_scenes]]
            for name in gone:
                used = finder(name) if finder is not None else []
                if used:
                    await self._io.write(
                        f"  ! {', '.join(used)} ran the deleted scene "
                        f"{name!r} -- change or remove it.", level="warn")
        return True

    # ── Registration: one transaction ───────────────────────────────

    def _erase_person(self, user: str, keep_history: bool = False) -> None:
        """Best-effort teardown of a half-made person: voiceprint, PIN,
        recovery data, phone bindings. The stores are called by their own names now: the
        capability probe that was here guarded against voice_security
        classes other than the one this shell constructs, and there are
        none."""
        try:
            self.biometric_store.delete_user(user)
        except Exception as exc:                   # noqa: BLE001
            # SAY SO, LOUDLY. This teardown is best-effort by design (it
            # runs inside a finally and must not crash the unwind), but
            # best-effort that fails SILENTLY would let the rollback log
            # claim a voiceprint was erased when the store refused -- the
            # exact half-person the transaction law exists to prevent,
            # surviving invisibly.
            event("ENROLL", f"WARNING: could not erase the voiceprint for "
                            f"{user!r} ({exc}). Remove it by hand (the "
                            f"registration transaction is not honoured "
                            f"until it is gone).")
        # The live speaker caches go with the file: left behind, the sherpa
        # manager refuses to re-add this name for the life of the process.
        try:
            self.sv.remove_user(user)
        except Exception:                          # noqa: BLE001
            pass
        try:
            self.fallback.pin.delete_pin(user)
        except Exception:                          # noqa: BLE001
            pass
        # The ledger entry goes with them. A rolled-back registration is a
        # person who never existed, and leaving "added by you" behind for
        # one would put a name on the Profile screen that no store can
        # resolve. (A person REMOVED did exist: keep_history, and the
        # journal marks them removed instead.)
        if not keep_history:
            try:
                self.people.forget(user)
            except Exception:                      # noqa: BLE001
                pass
        # Their handsets let go of them. A phone left bound to a person who
        # is gone resolves to nobody: ws_server takes it as already bound,
        # so it is refused as not enrolled and never offered enrolment.
        try:
            from identity import phones as _phones
            registry = _phones.PhoneRegistry()
            for pid in registry.phones_for_user(user):
                if keep_history:
                    # Removed, or left: unpaired for good. Only unbound, the
                    # phone would be offered "I'm new" and walk back in.
                    registry.remove(pid)
                else:
                    registry.bind_user(pid, "")
        except Exception as exc:                   # noqa: BLE001
            event("ENROLL", f"WARNING: could not unbind {user!r}'s phones "
                            f"({exc})")

    def _rollback_registration(self, before: Set[str]) -> None:
        """Erase everything an unfinished registration wrote. SYNCHRONOUS
        on purpose: this runs in a finally that may be unwinding a
        CANCELLED task (the phone disconnected mid-ceremony), and an await
        there can itself be cancelled -- leaving exactly the half-person
        this exists to prevent."""
        try:
            after = set(self.biometric_store.list_users() or [])
        except Exception:                          # noqa: BLE001
            return
        rolled = sorted(after - before)
        for user in rolled:
            self._erase_person(user)
            if self.current_speaker_id == user:
                # A phone must never be bound to an erased person -- the
                # bind fallback in ws_server reads current_speaker_id.
                self.current_speaker_id = None
            event("ENROLL", f"registration rolled back -- {user!r} erased "
                            f"(voice, PIN, recovery)")
        # LEAVE A MARK. This used to write an event() line and nothing else,
        # so a registration killed by the network vanished without trace: the
        # voice was erased (correctly -- a half-person is the thing this
        # transaction exists to prevent) and the person came back to a hub
        # that behaved as though nothing had ever happened.
        #
        # The ribbon law does not ask for the WORK to be kept here. It cannot
        # be; keeping it would rebuild the half-person. It asks that the
        # ENDING be visible, and silence is never how a ceremony reports that
        # it failed.
        #
        # Durable on disk, because the socket that would have carried a
        # result is exactly the thing that died. Read on the next notices
        # check, cleared the moment a registration completes.
        if rolled or self._ceremony_begun:
            self._mark_registration_interrupted(rolled)

    @staticmethod
    def _interrupted_path() -> str:
        """Where an interrupted ceremony leaves its note -- beside
        pin_db.json, because it is hub state and not a document.

        A METHOD, not a class attribute. `Path(DATA_DIR) / ...` in the class
        body is evaluated at IMPORT time, which would have made json, Path
        and DATA_DIR three new hard module-level dependencies of main.py --
        the exact shape of the breakage that spent this afternoon. Computed
        on use, it depends on nothing until something actually needs it.
        """
        import os as _os
        from core.config import DATA_DIR as _data_dir
        return _os.path.join(_data_dir, "registration_interrupted.json")

    def _mark_registration_interrupted(self, rolled: List[str]) -> None:
        """Record that a ceremony died half-way. Never raises: this runs in
        a finally that may already be unwinding a cancelled task, and a
        failure to write a NOTE must not become the thing that crashes the
        rollback it is describing."""
        import json as _json
        import os as _os
        try:
            # SERIALISE FIRST, WRITE SECOND, and write to a temp file that
            # is then moved into place -- the same shape pin_auth._persist
            # uses, and for the same reason.
            #
            # The first version of this did `open(path, "w")` and then built
            # the JSON inside the with-block. open("w") TRUNCATES, so any
            # failure while building the dict left a zero-byte file behind:
            # the note "existed" and read back as nothing, which meant the
            # notice never appeared and the rollback looked silent all over
            # again -- the exact defect this method was added to fix,
            # reintroduced inside the fix. Caught by test_registration.
            blob = _json.dumps({
                # The name only -- no audio, no PIN, nothing that was
                # erased a moment ago. Enough to say WHOSE attempt it
                # was, and nothing more.
                "name": (rolled[0] if rolled else ""),
                "at": int(time.time()),
            })
            path = self._interrupted_path()
            parent = _os.path.dirname(path)
            if parent:
                _os.makedirs(parent, exist_ok=True)
            tmp = path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                f.write(blob)
                f.flush()
                _os.fsync(f.fileno())
            _os.replace(tmp, path)
        except Exception as exc:                   # noqa: BLE001
            event("ENROLL", f"could not note the interrupted registration: "
                            f"{exc}")

    def _clear_registration_interrupted(self) -> None:
        import os as _os
        try:
            _os.unlink(self._interrupted_path())
        except Exception:                          # noqa: BLE001
            pass                  # FileNotFoundError included: nothing to clear

    def _interrupted_registration(self) -> Optional[Dict[str, Any]]:
        import json as _json
        try:
            with open(self._interrupted_path(), "r", encoding="utf-8") as f:
                data = _json.load(f)
            return data if isinstance(data, dict) else None
        except Exception:                          # noqa: BLE001
            return None           # missing, unreadable or not JSON alike

    async def register_person(self, *, sponsor: Optional[str] = None,
                              first_run: bool = True) -> bool:
        """REGISTRATION IS ONE TRANSACTION (the owner's law, 2026-08-27).

        A person exists only when the WHOLE ceremony finished: voice
        recorded, PIN set, recovery words acknowledged. Any stop anywhere
        -- a cancel, a disconnect, this task dying -- rolls back everything
        the ceremony wrote. So the half-states cannot exist (a voice with
        no PIN, a PIN with no voice, words never seen), the old "'X' has no
        PIN on file, so logging in is impossible" dead-end is
        unconstructible, and "no voiceprints" alone is enough to know
        registration must run.
        """
        before = set(self.biometric_store.list_users() or [])
        completed = False
        # Fresh ceremony, fresh door: menu_enroll_user raises this flag the
        # moment the name question is answered. See
        # register_person_persistently for who reads it and why.
        self._ceremony_begun = False
        # A SIGNUP IS A NEW PERSON, so nobody is identified while it runs:
        # with this None, menu_enroll_user's "already taken" check refuses
        # EVERY existing name (its self-re-enrol exemption needs a self).
        self.current_speaker_id = None
        try:
            enrolled = await self.menu_enroll_user(manage_view=False,
                                                   sponsor=sponsor)
            if not enrolled:
                return False
            user = self.current_speaker_id
            if not user:
                return False
            if not await self.menu_setup_pin(user, strict_ack=True):
                await self._io.write(
                    "  Registration is one piece -- without the PIN and "
                    "its recovery words, the voice recording is discarded "
                    "too. Start again whenever you're ready.", level="warn")
                return False
            completed = True
            # A finished person clears the note from any earlier attempt --
            # otherwise the hub keeps offering to resume a ceremony that has
            # already succeeded, which is its own kind of lie.
            #
            # FIRST RUN ONLY. The note means "the FIRST registration was
            # interrupted"; adding a second person later says nothing about
            # whether that one finished, so menu_add_person passes False
            # rather than quietly marking someone else's ceremony done.
            # UNLESS IT IS THAT ONE: an interrupted Add a person leaves the
            # same note (the rollback cannot tell the doors apart), and
            # finishing it under the same name must take the note down --
            # nothing else ever did, so it stayed in Notifications for good.
            if first_run or (self._interrupted_registration() or {}).get(
                    "name") == user:
                self._clear_registration_interrupted()
            return True
        finally:
            if not completed:
                self._rollback_registration(before)
                # NOT COMPLETED => NOBODY. menu_enroll_user sets this on
                # voice success, before the PIN; _rollback_registration
                # only cleared it for a NEWLY created name. ws_server's
                # bind fallback reads it, so a ceremony that stopped after
                # the voice step used to bind the phone anyway.
                self.current_speaker_id = None

    async def menu_add_person(self) -> bool:
        """Add somebody else to the house -- the WHOLE ceremony (2026-09-12).

        THE BUG THIS EXISTS TO KILL. The Profile screen's "Add another
        person" said "They record their own voice and set their own PIN"
        and sent `enroll_voice`, which is menu_enroll_user: voice, and
        nothing else. So it built exactly the half-person register_person's
        docstring calls unconstructible -- and every way out was shut:

            Log in          ws_server: "no PIN on file, so logging in
                            with one is impossible"
            "I'm new"       menu_enroll_user: "That name is already taken"
            Their PIN,      menu_setup_pin: over a socket user_id is forced
            set by you      to the caller, and naming someone else is
                            refused outright

        The hub's own advice -- "set a PIN from an already-trusted phone
        first" -- could not be taken from any phone at all. Only the Pi's
        own keyboard could rescue them, on a product whose whole point is
        that it needs neither a screen nor SSH.

        So this runs register_person: voice, PIN and recovery words as ONE
        transaction, rolled back entire if anything stops. The same law the
        first person is held to.

        TWO THINGS ARE HELD ACROSS IT.

        `current_speaker_id` is blanked for the ceremony, because that is
        what makes menu_enroll_user refuse every name already in use --
        including the sponsor's own, whose self-re-enrol exemption would
        otherwise let "Add another person" silently re-record the person
        already holding the phone. It is put back in the finally, on every
        path: register_person leaves the NEW person there on success and
        None on failure, and this handset belongs to neither.

        And the sponsor is passed down explicitly, since the journal reads
        it from the very field being blanked.
        """
        sponsor = (self.current_speaker_id or "").strip()
        if not sponsor:
            self.failure_note = ("Nobody is identified on this connection, "
                                 "so there is no one to add a person on "
                                 "behalf of.")
            await self._io.write(f"  {self.failure_note}", level="warn")
            return False
        await self._io.write("\n--- ADD A PERSON ---")
        await self._io.write(
            "  They record their voice, then set their own PIN and write "
            "down their recovery words. It is one piece: stopping anywhere "
            "leaves nothing behind.")
        try:
            return await self.register_person(sponsor=sponsor,
                                              first_run=False)
        finally:
            # ALWAYS, and after register_person's own finally has run.
            self.current_speaker_id = sponsor

    async def register_person_persistently(self) -> bool:
        """register_person, under the owner's CANCEL LAW (field test
        2026-08-27): "it should just take me back to the beginning of
        enrollment, where I write what the hub will call me."

        A cancel MID-ceremony -- during the phrases, at the PIN, anywhere
        past the name question -- means "start me over", not "let me out".
        register_person has already rolled everything back by the time the
        cancel reaches here, so restarting is safe by construction, and the
        loop simply asks the name question again.

        Only a cancel at the UNTOUCHED first question (nothing typed,
        nothing recorded, nothing to roll back) still means "let me out",
        and that one propagates to the caller -- so the door out exists,
        one cancel away, at the exact place the restart lands you.
        """
        while True:
            try:
                return await self.register_person()
            except CancelledByUser:
                if not self._ceremony_begun:
                    raise  # cancelled at the door -- actually leaving
                await self._io.write(
                    "  Cancelled -- nothing was saved. Let's start over: "
                    "first, what should the house call you?")

    async def _first_run_setup(self):
        """Option 3: if NO users are enrolled, force enroll + PIN before the
        main menu is reachable. Keys off current state (zero users), not
        first-launch-ever -- so it also triggers if every user was later
        deleted, closing the 'usable menu with nobody to verify against' gap.
        Since 2026-08-27 the loop condition is also EXACT: registration is
        atomic (register_person), so a user in the store means a COMPLETE
        person -- voice, PIN, and acknowledged recovery words.

        BUGFIX: this used to be just the early-return guard below, with the
        actual enrollment loop sitting at the tail of _resolve_current_speaker()
        instead -- reachable only AFTER that method's multi-user branch ran,
        which meant it was unreachable on a genuinely fresh install (zero
        users), the one case it exists to handle. _resolve_current_speaker()
        returns early precisely when there are no users, so the loop below
        never ran. Net effect: nothing ever forced enrollment; every
        voice-gated command was silently denied with no PIN set, no
        indication why. Moved here, where the docstring already said it
        lived."""
        if self.biometric_store.list_users():
            return  # at least one user exists -- nothing forced

        await self._io.write("\n" + "=" * 60)
        await self._io.write("  FIRST-TIME SETUP")
        await self._io.write("  No users are enrolled yet. Enroll a voice and set a PIN")
        await self._io.write("  before the system can be used.")
        await self._io.write("  (type 'cancel' at any prompt to quit)")
        await self._io.write("=" * 60)
        attempts = 0
        while not self.biometric_store.list_users():
            attempts += 1
            if attempts > config_MAX_FIRST_RUN_ATTEMPTS:
                # Bounded. A client that answers "retry" forever (or a
                # scripted one) otherwise pins the shell lock indefinitely.
                await self._io.write("Enrollment has failed several times -- stopping for "
                                     "now. Start it again whenever you're ready.",
                                     level="warn")
                self.running = False
                return
            try:
                # THE WHOLE CEREMONY, atomically -- voice, PIN, recovery
                # words -- or nothing at all. The old shape here enrolled
                # the voice and then treated a cancelled PIN as "set it
                # later", which is precisely the half-person the owner's
                # transaction law forbids. The *_persistently wrapper adds
                # the cancel law on top: a mid-ceremony cancel rolls back
                # and re-asks the name question, so the only cancel that
                # reaches the except below is one at the untouched door.
                enrolled = await self.register_person_persistently()
            except CancelledByUser:
                if not self._io.trusted_local:
                    # ONLY a cancel at the untouched door reaches here
                    # (mid-ceremony cancels restart inside
                    # register_person_persistently) -- and at the door,
                    # cancel means "let me out". On a phone, OUT now goes
                    # somewhere real: home. The old shape held the client
                    # in the ceremony room with nothing pending -- a
                    # spinner nobody could answer (field test 2026-08-27,
                    # "when cancel was pressed it didn't go to the enroll
                    # screen"). Leaving costs nothing: the reader gate
                    # turns the next command into a fresh ceremony.
                    await self._io.write(
                        "  Nothing was saved. Start registration again "
                        "whenever you're ready -- any command brings it "
                        "back.", level="warn")
                    return
                # The CLI operator is at the machine and Ctrl-C-class exits
                # are theirs to take.
                await self._io.write("\nSetup cancelled. Goodbye.")
                self.running = False
                return
            if not enrolled:
                # A two-option decision, asked through choose(): buttons on the
                # phone, a numbered list on the CLI. Typing "n" on a phone
                # keyboard is the worst available control for yes/no.
                try:
                    picked = await self._io.choose(
                        "Enrollment didn't complete. Try again?",
                        [{"value": "retry", "label": "Try again", "sub": ""},
                         {"value": "stop", "label": "Not now", "sub": ""}],
                    )
                except CancelledByUser:
                    await self._io.write("\nSetup cancelled.")
                    self.running = False
                    return
                # Cancelling the picker and choosing "Not now" mean the same
                # thing: not doing this right now.
                if not picked or picked[0] == "stop":
                    # This used to say "Cannot continue... Goodbye." -- true on
                    # the CLI, where run() checks self.running and the process
                    # exits. On the socket nothing says goodbye: the connection
                    # stays up and the client sits on the enroll view, so a
                    # farewell message with no visible way forward read as a
                    # freeze. Name the control that resumes it instead.
                    await self._io.write(
                        "Nothing is enrolled, so commands stay locked. "
                        "Start enrollment again whenever you're ready.", level="warn")
                    self.running = False
                    return
                continue
            # register_person finished WHOLE -- voice, PIN, and the words
            # acknowledged -- or the loop above would still be running. The
            # "PIN setup skipped, set it later" mercy that used to live
            # here is deliberately gone: it manufactured the half-person
            # the transaction law forbids.
            await self._io.set_view("command")
        await self._io.write("\n  Setup complete. Welcome.\n")

    async def _resolve_current_speaker(self):
        """Called after _first_run_setup(). If enrollment just happened this
        session, current_speaker_id is already set -- nothing to do. If
        users existed from a PRIOR run instead, nobody has told the shell who
        is at the keyboard now: auto-pick if there's exactly one enrolled
        user (the common single-operator case), otherwise ask."""
        if self.current_speaker_id is not None:
            return
        users = self.biometric_store.list_users()
        if not users:
            return  # shouldn't happen -- _first_run_setup would have forced enrollment
        if len(users) == 1:
            self.current_speaker_id = users[0]
            event("VOICE", f"speaker resolved to {users[0]!r} (only enrolled user)")
            return
        await self._io.write(f"\n  Multiple enrolled users: {', '.join(users)}")
        # A cancel is a cancel and a name nobody has is nobody: both used
        # to become users[0], the alphabetically-first person, silently.
        chosen = (await self._io.get_input("  Who is speaking? ")).strip()
        if chosen not in users:
            await self._io.write(f"  No enrolled user called {chosen!r}.",
                                 level="warn")
            raise CancelledByUser()
        self.current_speaker_id = chosen
        await self._io.write(f"  (speaking as: {self.current_speaker_id})")

    # ── Main loop ───────────────────────────────────────────

    async def run(self):
        await self.init()
        await self._first_run_setup()
        if not self.running:
            return
        # A name nobody has is a CancelledByUser from here, and this sat
        # outside the loop's handler: one typo at "Who is speaking?" ended
        # the console with a traceback. Asked again instead; a typed
        # cancel three times running is taken as leaving.
        for _ in range(3):
            try:
                await self._resolve_current_speaker()
                break
            except CancelledByUser:
                continue
        else:
            print("\nGoodbye.")
            return
        while self.running:
            try:
                self.print_menu()
                highest = max(int(n) for n in self.CLI_MENU)
                choice = await self._io.get_input(
                    f"Select option (1-{highest}) or type a command: ")

                if choice == "10" or choice in ("help", "h"):
                    self.print_help()
                    continue
                if choice in ("11", "quit", "exit", "q"):
                    print("\nGoodbye.")
                    self.running = False
                    continue

                entry = self.CLI_MENU.get(choice)
                if entry is None:
                    # NOT an error. Anything that is not a menu number is
                    # taken as spoken language -- "turn the kitchen light
                    # off" is a perfectly good thing to type here.
                    await self.process_natural_command(choice)
                    continue

                # No PIN gate on this path, unchanged and on purpose: the
                # terminal is the machine's own console. The gate exists for
                # actions arriving over the network, and run_menu_action is
                # where it lives.
                await getattr(self, entry[0])()

            # EOFError too: Ctrl-D, or a console started with no stdin
            # (nohup, `< /dev/null`), reached the blanket handler below and
            # re-prompted into the same EOF for ever -- a traceback per spin.
            except (KeyboardInterrupt, EOFError):
                print("\nGoodbye.")
                self.running = False
            except CancelledByUser:
                continue
            except Exception:
                print("\nUnexpected error:")
                traceback.print_exc()

        print("(All changes saved.)")


# Each per-phone field is a property of the shell (see Conversation): the
# class-level annotations above document a few of them; these replace any
# plain class attribute of the same name.
for _name in CONVERSATION_FIELDS:
    setattr(InteractiveShell, _name, _conversation_property(_name))
del _name


#: How long startup will wait for the broker and Home Assistant before
#: carrying on regardless.
#:
#: IT COSTS NOTHING IN THE NORMAL CASE. When everything is already up this
#: returns in about a fifth of a second, so the number only matters on the
#: run where something genuinely has to be started -- and there, waiting is
#: the whole point. Docker Desktop takes about a minute from cold and this
#: has to outlast it, because the shell connects to Home Assistant the
#: moment this returns: give up too early and the hub spends the entire
#: session in its no-HA fallback while Home Assistant comes up behind it.
STACK_TIMEOUT_SECONDS = 120


#: Set once the stack has been dealt with, so a second caller is free.
#:
#: This exists because the startup belongs to the ENTRY POINT and there are
#: two of them -- the console and the websocket server -- which is how the
#: headless one went without it. Making the call idempotent means a third
#: entry point can call it without anyone having to work out whether
#: something earlier already did.
_STACK_CHECKED = False


async def _bring_the_stack_up() -> None:
    """Start the broker and Home Assistant if they are down. Never fatal.

    WHY THIS IS HERE AND NOT IN __init__: the shell connects to Home
    Assistant while it is being constructed, so anything that starts Home
    Assistant has to run before that. Started afterwards, the hub would spend
    the whole session in its no-HA fallback while Home Assistant sat there
    perfectly healthy.

    EVERYTHING IS ANNOUNCED. Servers that appear behind your back are how you
    end up with two brokers and an empty log, so every action taken is
    printed and every action skipped says why. setup_manager refuses to start
    a second broker; this only asks it to try.
    """
    global _STACK_CHECKED
    if _STACK_CHECKED:
        return
    _STACK_CHECKED = True

    # AN OFF SWITCH, FIRST. A hub that cannot be started without waiting for
    # docker is a hub that cannot be started on a machine where docker is
    # broken -- and "it hangs" is what that looks like from the outside.
    if os.environ.get("HOMEVOICE_NO_AUTOSTACK") == "1":
        event("SETUP", "skipped (HOMEVOICE_NO_AUTOSTACK=1). Start the broker "
                       "yourself: python setup_manager.py --start")
        return

    try:
        from app import setup_manager
    except Exception as exc:                      # noqa: BLE001
        event("SETUP", f"could not load setup_manager: {exc}")
        return

    lines: List[str] = []
    done = threading.Event()

    def work() -> None:
        try:
            setup_manager.ensure_stack(lines.append)
        except Exception as exc:                  # noqa: BLE001
            # A hub that will not start because a helper threw is worse than
            # a hub with no broker: one is fixable from the menu, the other
            # is not.
            lines.append(f"could not be started automatically: {exc}")
        finally:
            done.set()

    # A DAEMON THREAD, NOT asyncio.to_thread. to_thread runs on the default
    # executor, whose threads are joined at interpreter exit -- so a docker
    # command that hangs for five minutes would not merely delay startup, it
    # would hang QUITTING the hub, long after anyone stopped caring whether
    # Home Assistant came up. This one can be abandoned where it stands.
    threading.Thread(target=work, name="homevoice-stack", daemon=True).start()

    started = time.monotonic()
    deadline = started + STACK_TIMEOUT_SECONDS
    said = 0
    ticked = 0.0
    while not done.is_set() and time.monotonic() < deadline:
        # Printed as they happen rather than in a lump at the end: "starting
        # the Home Assistant container" is worth seeing DURING the twenty
        # seconds it takes, not after.
        while said < len(lines):
            text = lines[said].strip()
            said += 1
            if text:
                event("SETUP", text)
        # A HEARTBEAT, because silence for two minutes IS a hang as far as
        # anyone watching is concerned, and the reflex to that is Ctrl-C.
        # Waiting is fine; waiting without saying so is not.
        waited = time.monotonic() - started
        if waited - ticked >= 10.0:
            ticked = waited
            event("SETUP", f"still waiting ({int(waited)}s of "
                           f"{int(STACK_TIMEOUT_SECONDS)}s). Ctrl-C is safe "
                           f"here, or set HOMEVOICE_NO_AUTOSTACK=1 to skip "
                           f"this step next time.")
        await asyncio.sleep(0.2)

    if not done.is_set():
        lines.append(f"gave up waiting after {STACK_TIMEOUT_SECONDS}s -- "
                     f"carrying on without it")
    for line in lines[said:]:
        text = line.strip()
        if text:
            event("SETUP", text)


async def _take_the_stack_down(shell: Optional["InteractiveShell"] = None) -> None:
    """Stop what startup started, and nothing else.

    ASYMMETRIC ON PURPOSE. Startup will happily use a broker that was
    already running; shutdown will not stop it. If --autostart registered
    the broker at boot, or another window is using it, or Home Assistant is
    running the automations for the whole house, then closing a console is
    not a reason to take any of it down -- and after --autostart this does
    nothing at all, because nothing here started anything.

    `shell`'s automation engine is stopped first, explicitly: its atexit
    hook would save the same history, but only after the broker it is
    connected to may have been stopped under it.
    """
    try:
        if shell is not None and shell.automations is not None:
            await asyncio.to_thread(shell.automations.stop)
        from app import setup_manager
        # Only what this process started: shutdown_stack never stops a
        # broker and a Home Assistant this run merely found -- the ones
        # ws_server and every board were using.
        await asyncio.to_thread(setup_manager.shutdown_stack,
                                lambda line: event("SETUP", line.strip()))
    except Exception as exc:                      # noqa: BLE001
        # Quitting must not fail. Whatever is still running can be stopped
        # by hand, and an exception here would bury the goodbye.
        event("SETUP", f"could not stop cleanly: {exc}")


async def main():
    await _bring_the_stack_up()
    shell = None
    try:
        shell = InteractiveShell()
        await shell.run()
    finally:
        await _take_the_stack_down(shell)


def run_entry(main_coro_fn, goodbye: str) -> None:
    """The body of both entry points (the console and ws_server): run
    `main_coro_fn()`, take Ctrl-C as leaving with `goodbye`, and stop what
    this process started however it ended."""
    try:
        asyncio.run(main_coro_fn())
    except KeyboardInterrupt:
        print(goodbye)
    finally:
        # BELT AND BRACES. Ctrl-C can tear the loop down before main()'s
        # `finally` manages to await anything -- an await inside a cancelled
        # task raises immediately -- so the last word is a plain synchronous
        # call. Running twice is free: shutdown_stack empties its own record
        # of what it started, so whichever gets there first does the work
        # and the other returns at once. (A main() that returned early, like
        # ws_server's on a FATAL, is covered here too.)
        try:
            from app import setup_manager
            setup_manager.shutdown_stack()  # ours only; see _take_the_stack_down
        except Exception:
            pass


def run() -> None:
    """Console entry point (python3 main.py)."""
    run_entry(main, "\n\nInterrupted. Goodbye.")


if __name__ == "__main__":
    run()

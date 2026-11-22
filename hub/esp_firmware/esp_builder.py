"""Describing an ESP board, flashing it, and telling it what it is.
ONE prebuilt Tasmota firmware, identical on every board. It knows nothing
about your wiring when it is flashed; the hub tells it afterwards --
"GPIO13 is relay 1, GPIO21 and 22 are the I2C bus" -- and it reconfigures
itself. Adding a device is a message. Changing a pin is a message. Nothing
is ever compiled, here or on the Pi.
The one-time cost is provisioning over the USB cable, where the board is
handed its Wi-Fi, its broker and its own credentials. After that the cable
is never needed again.
Each board gets its OWN MQTT account and its OWN web password, both
revocable: delete one, the others do not notice.
"""
import asyncio
import base64
import contextlib
import json
import os
import re
import secrets as _secrets
import shutil
import sys
import ssl
import subprocess
import threading
import time
import traceback
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple
from core.hv_common import CancelledByUser, event, means_cancel, new_paho_client
from core import config as _config
from core.locked_store import atomic_write_json
from hub import hub_net

#: Bumped on every delivered change, printed at the first add-device
#: question -- so any pasted transcript says which build produced it.
REVISION = "2026-08-25c two-phase-wiring"
#: Largest total a phone may hand over in one go (before base64). Sized to
#: stay inside the WebSocket frame limit (8 MB) once encoded.
_PHONE_MAX_BYTES = 5_000_000
#: How a GitHub build waits between looks. A name the tests replace, so a
#: whole cloud run takes no real time there.
_cloud_sleep = asyncio.sleep
# The catalogue is a SIBLING of this module -- a plain import, no fallback.
# It kept its old filename through the switch; its CONTENTS are the Tasmota
# pin table.
from . import esphome_catalog as cat
from . import chips

#: Board profiles written as files (boards/<id>.json, chips.board_from_json)
#: -- beside the hub, so a new board is a file, not code.
_BOARD_FILES_DIR = Path(__file__).resolve().parent.parent / "boards"
#: The notes of the one load of those files per process (None = not yet).
_BOARD_FILE_NOTES: Optional[List[str]] = None


def _load_board_files() -> List[str]:
    """Merge boards/*.json into chips' profile list, once per process --
    at builder construction, and on first use by a headless builder, so a
    record naming a board file never reads as the classic DevKit merely
    because nothing loaded the file yet."""
    global _BOARD_FILE_NOTES
    if _BOARD_FILE_NOTES is None:
        try:
            _BOARD_FILE_NOTES = chips.load_board_files(_BOARD_FILES_DIR)
        except Exception as exc:                   # noqa: BLE001
            _BOARD_FILE_NOTES = [f"board files could not be loaded: {exc}"]
    return _BOARD_FILE_NOTES


def _require_catalogue_interface() -> None:
    """Refuse to START against a catalogue that is not this builder's pair.

    The two files ship together and are installed by hand, so they CAN
    arrive one delivery apart -- and when they did, the failure was an
    AttributeError an hour into an install, on whichever method the newer
    half reached for first (a builder expecting a field from an
    older catalogue, 2026-08-25 -- "this inconsistency is what I get
    tired of telling you"). Import time is the cheapest moment to notice:
    the refusal names every missing piece and BOTH revisions, so the fix
    is obvious from the message alone.
    """
    missing: List[str] = []
    for name in ("REVISION", "PlacementError", "BoardPlan", "DeviceType",
                 "describe"):
        if not hasattr(cat, name):
            missing.append(name)
    if hasattr(cat, "BoardPlan"):
        for method in ("activation_commands", "configuration_commands",
                       "backlogs", "remove", "add", "template_command"):
            if not hasattr(cat.BoardPlan, method):
                missing.append(f"BoardPlan.{method}")
    if hasattr(cat, "DeviceType"):
        fields = getattr(cat.DeviceType, "__dataclass_fields__", {})
        for field_name in ("driver_names",):
            if field_name not in fields:
                missing.append(f"DeviceType.{field_name}")
    if missing:
        raise RuntimeError(
            f"esp_builder {REVISION} cannot run against esphome_catalog "
            f"{getattr(cat, 'REVISION', '(unstamped -- older than 25c)')}: "
            f"it is missing {', '.join(sorted(missing))}. The two files "
            f"ship as a pair; install the esphome_catalog.py delivered "
            f"with this esp_builder.py and start again.")


_require_catalogue_interface()

#: "argument not given", for the one place None is itself a value.
_UNSET: Any = object()


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S")

def _slugify(name: str) -> str:
    """A name safe for a filename, an MQTT topic and a hostname at once."""
    slug = re.sub(r"[^a-z0-9]+", "-", name.strip().lower()).strip("-")
    return slug[:32] or "board"

#: OtaImage per firmware path, for _ota_heal_url (see there).
_HEAL_IMAGES: Dict[str, Any] = {}



async def _port_sleep(seconds: float) -> None:
    """The wait between looks for a plugged-in board (tests replace it)."""
    await asyncio.sleep(seconds)


class EspBuilder:
    """Everything about a physical board, from wiring to credentials."""

    #: Writing two megabytes over serial is not fast, and a slow cable is
    #: not a fault.
    FLASH_TIMEOUT = 300.0

    #: Seconds between progress lines while a command streams.
    PROGRESS_EVERY = 2.0

    #: A board that has just been flashed takes its time coming up.
    CONSOLE_WAIT = 45.0

    #: For ASKING a board something when it may not run Tasmota at all.
    CONSOLE_PEEK = 6.0

    #: The retained LWT arrives the instant you subscribe, or not at all.
    LWT_WAIT = 3.0
    _LOUD = re.compile(r"error|fail|fatal|traceback|denied|refus", re.I)

    #: esptool says this when it cannot find a board, which is nearly always
    #: a cable that carries power but not data.
    _NO_BOARD = re.compile(
        r"failed to connect|no serial data received|could not open port", re.I)

    #: Tasmota's TWO rejection shapes, and only those. Matching a bare
    #: "Error" also matched boot spew and unrelated telemetry, which failed
    #: provisioning runs that had in fact worked.
    _CONSOLE_BAD = re.compile(r'"Command"\s*:\s*"(Unknown|Error)"', re.I)

    #: Settings whose VALUE must never reach a log. Tasmota echoes every
    #: serial command line as `CMD: ...` at INFO (SetMinimumSeriallog forces
    #: that level for serial input, so SerialLog 0 does not silence it), and
    #: its index-1 replies print the stored secret in clear.
    _SECRET_KEYS = r"(?:Password\d?|MqttPassword\d?|WebPassword|SSId\d?)"
    _SECRET_JSON = re.compile(r'("%s")\s*:\s*"(?:[^"\\]|\\.)*"' % _SECRET_KEYS,
                              re.I)
    _SECRET_CMD = re.compile(r'\b(%s)\s+[^;\r\n]*' % _SECRET_KEYS, re.I)

    @classmethod
    def _redact(cls, line: str) -> str:
        """A board's line with every secret value replaced by ****."""
        line = cls._SECRET_JSON.sub(r'\1:"****"', line)
        return cls._SECRET_CMD.sub(r"\1 ****", line)

    def __init__(self, shell):
        self._shell = shell
        self._last_node: Optional[str] = None
        self._said_threaded = False
        self._said_no_board = False
        self._said_sidecar_notes = False
        # True when the last install wrote firmware and wiring but no
        # network -- the caller's "it was not set up" message would be a lie.
        self.last_flash_needs_network = False
        # True while a busy bar is on screen: raw esptool chatter behind a
        # progress card is noise. Warnings still speak.
        self._quiet_stream = False
        # Board files the same way: loaded now, said at the first question.
        self._board_load_notes: List[str] = list(_load_board_files())

    @classmethod
    def headless(cls) -> "EspBuilder":
        """A builder WITHOUT __init__: no shell, no IO backend, no broker
        connection. For callers that only read board files and settings."""
        return cls.__new__(cls)

    # ── plumbing ────────────────────────────────────────────────────────
    #
    # _io is a property, not a stored reference: the shell swaps its IO
    # backend per connection, and a builder holding the old one writes into
    # a socket that closed some time ago.

    @property
    def _io(self):
        return self._shell._io

    # ── where things live ───────────────────────────────────────────────

    def _dir(self) -> Path:
        """Board records and the shared Wi-Fi file.
        Outside the source tree, deliberately: it holds the Wi-Fi password
        and every board's broker password.
        """
        base = Path(_config.BOARDS_DIR)
        base.mkdir(parents=True, exist_ok=True)
        return base

    def _wifi_path(self) -> Path:
        return self._dir() / "wifi.json"

    def _board_path(self, slug: str) -> Path:
        return self._dir() / f"{slug}.json"

    def _read_board(self, slug: str) -> Optional[Dict[str, Any]]:
        try:
            record = json.loads(self._board_path(slug).read_text("utf-8"))
        except Exception:
            return None
        # A file that merely LIVES in this directory -- wifi.json, a registry
        # an old version parked here -- must not come back dressed as a board.
        if not isinstance(record, dict) or record.get("slug") != slug:
            return None
        return record

    @staticmethod
    def _write_json(path: Path, payload: Any) -> None:
        """Write-then-rename, so the file is whole or it is the old one.

        A plain write_text interrupted mid-way -- power, a crash, the app
        killed -- leaves half a JSON file, which _read_board then reads as
        "not a board" and the add flow happily re-creates: two broker
        accounts for one chip, and the original's credentials orphaned
        (audit, 2026-08-25). os.replace is atomic on POSIX and on NTFS;
        the temp file sits BESIDE the target so the rename never crosses
        a filesystem. CREATED 0600 (os.open with the mode, not write_text
        then chmod), so the secrets in it are never world-readable, even for
        a moment.
        """
        # core.locked_store: a UNIQUE temp name (mkstemp, 0600), fsynced --
        # a fixed "<name>.tmp" was shared by every writer.
        atomic_write_json(str(path), payload, indent=2)

    def _write_board(self, record: Dict[str, Any]) -> None:
        self._write_json(self._board_path(record["slug"]), record)
        # ONE POINT OF TRUTH: whatever a board is wired to is also its
        # layout in Saved layouts, kept in step on EVERY write -- a new
        # design, an edit, a flash, a rename, a change of board type. Never
        # allowed to break the board write itself.
        try:
            self._sync_board_layout(record)
        except Exception as exc:                   # noqa: BLE001
            event("ESP", f"{record.get('slug')}: layout not kept in step: "
                         f"{exc}")

    def list_nodes(self) -> List[str]:
        """Every board this hub knows about, by slug."""
        return [slug for slug, _ in self._boards()]

    def _boards(self) -> List[Tuple[str, Dict[str, Any]]]:
        """(slug, record) for every board, sorted by slug -- one read each.
        BY SHAPE, NOT BY NAME. This directory has never held only boards.
        A list of excluded names goes stale the day the next file arrives;
        the writer's invariant cannot: a board is a json file whose record
        names itself.
        """
        out: List[Tuple[str, Dict[str, Any]]] = []
        try:
            for path in self._dir().glob("*.json"):
                record = self._read_board(path.stem)
                if record is not None:
                    out.append((path.stem, record))
        except OSError:
            return []
        return sorted(out, key=lambda pair: pair[0])

    def _retired_path(self) -> Path:
        return self._dir() / "retired_topics.txt"

    def _retired_topics(self) -> set:
        """Topics of boards this hub itself retired: a chip still carrying
        one is not a stranger, so it is reused without asking."""
        try:
            text = self._retired_path().read_text("utf-8")
        except OSError:
            return set()
        return {line.strip() for line in text.splitlines() if line.strip()}

    def _remember_retired(self, topic: str) -> None:
        if not topic or topic in self._retired_topics():
            return
        try:
            with open(self._retired_path(), "a", encoding="utf-8") as fh:
                fh.write(topic + "\n")
        except OSError:
            pass

    def delete_node(self, slug: str) -> bool:
        """Forget a board. Does not touch the chip -- see menu_retire_board
        in the shell, which also unregisters the board's devices."""
        gone = self._read_board(slug)
        if gone is None:
            return False                     # wifi.json and friends
        board_name = gone.get("name", slug)
        try:
            self._board_path(slug).unlink()
        except OSError:
            return False
        self._remember_retired(gone.get("topic") or "")
        if self._last_node == slug:
            self._last_node = None
        self._forget_board_layout(slug, board_name, bool(gone.get("flashed")))
        return True

    # ── the old "rewire question" (LEGACY READERS ONLY) ─────────────────
    #
    # An Edit used to leave one decision behind -- keep the devices' names,
    # or set them all up again -- stored in the board's record. That
    # question is gone: the hub now settles its own registry after an edit
    # (see board_registry.settle_after_edit). These readers remain only so
    # a board parked by an earlier version is settled and cleared.

    def rewire_pending(self, slug: str) -> Optional[Dict[str, Any]]:
        return (self._read_board(slug) or {}).get("rewire_pending")

    def clear_rewire_pending(self, slug: str) -> None:
        record = self._read_board(slug)
        if record is not None and record.pop("rewire_pending", None) is not None:
            self._write_board(record)

    def boards_with_pending_rewire(self) -> List[str]:
        return [slug for slug, record in self._boards()
                if record.get("rewire_pending")]

    # ── saved layouts ───────────────────────────────────────────────────
    #
    # A LAYOUT IS A WIRING PLAN THAT BELONGS TO NO BOARD (her design,
    # 2026-10-01). Which devices sit on which pins, kept under a name, for
    # one CHIP FAMILY -- a classic layout is for classic boards, an S3's for
    # S3s. It controls nothing: it is dormant until somebody puts it on a
    # board, and it is never deleted by anything but its owner. Not by a
    # flash, not by an edit -- the same layout may be wanted on a second
    # board tomorrow.
    #
    # This replaced the per-board draft (`items_pending`), which the hub
    # deleted as a side effect of the very flash that used it. Old drafts
    # are carried over by migrate_drafts().
    #
    # ONE POINT OF TRUTH (her ruling, 2026-10-01): every connection lives in
    # Saved layouts -- unfinished, finished but never sent, and the wiring a
    # board really holds. A board's own wiring is ITS layout, kept in step by
    # _write_board on every write (`on_board` = the board's slug), so the
    # layout screen can never show wiring the board does not have. The
    # board keeps its own copy for the install; the layout is the one
    # people look at. `state` is what a plan is when it is on no board:
    #   "unfinished"  a design abandoned halfway
    #   "not_sent"    a finished edit that never reached its board
    #   "saved"       a plan kept for reuse
    # and a board's layout reads "flashed" or "waiting" off the board.

    _LAYOUT_ID = re.compile(r"^lay-[0-9a-f]{8}$")

    def _layouts_dir(self) -> Path:
        # A sub-folder, so `_boards()` (which globs *.json beside it) never
        # meets a layout dressed as a board.
        base = self._dir() / "layouts"
        base.mkdir(parents=True, exist_ok=True)
        return base

    def _layout_path(self, layout_id: str) -> Optional[Path]:
        """None for anything that is not one of OUR ids: the id arrives
        from the network and becomes part of a file name."""
        if not isinstance(layout_id, str) or not self._LAYOUT_ID.match(layout_id):
            return None
        return self._layouts_dir() / f"{layout_id}.json"

    def _read_layout(self, layout_id: str) -> Optional[Dict[str, Any]]:
        path = self._layout_path(layout_id)
        if path is None:
            return None
        try:
            record = json.loads(path.read_text("utf-8"))
        except Exception:
            return None
        if not isinstance(record, dict) or record.get("id") != layout_id:
            return None
        return record

    @staticmethod
    def _clean_layout_items(items: Sequence[Any]) -> List[Dict[str, Any]]:
        out: List[Dict[str, Any]] = []
        for item in items or []:
            if not isinstance(item, dict):
                continue
            try:
                pins = {str(k): int(v)
                        for k, v in (item.get("pins") or {}).items()}
                out.append({"type_id": str(item["type_id"]),
                            "name": str(item["name"]), "pins": pins})
            except (KeyError, TypeError, ValueError, AttributeError):
                continue
        return out

    def list_layouts(self, family: Optional[str] = None
                     ) -> List[Dict[str, Any]]:
        """Every saved layout, newest first -- or only those for one chip
        family. Carries any old per-board draft over first, so a list never
        misses one."""
        self.migrate_drafts()
        return self._all_layouts(family)

    def _all_layouts(self, family: Optional[str] = None
                     ) -> List[Dict[str, Any]]:
        """The library as it stands -- no carry-over, so the writers that
        the carry-over itself uses cannot loop back into it."""
        out: List[Dict[str, Any]] = []
        try:
            paths = list(self._layouts_dir().glob("lay-*.json"))
        except OSError:
            return []
        for path in paths:
            record = self._read_layout(path.stem)
            if record is None:
                continue
            if family is not None and record.get("family") != family:
                continue
            out.append(record)
        return sorted(out, key=lambda r: str(r.get("updated") or ""),
                      reverse=True)

    def _unique_layout_name(self, name: str, own_id: Optional[str]) -> str:
        taken = {str(r.get("name") or "").lower()
                 for r in self._all_layouts() if r.get("id") != own_id}
        if name.lower() not in taken:
            return name
        n = 2
        while f"{name} ({n})".lower() in taken:
            n += 1
        return f"{name} ({n})"

    def save_layout(self, name: str, board: "chips.Board",
                    items: Sequence[Any], *, source: str = "",
                    layout_id: Optional[str] = None,
                    created: Optional[str] = None,
                    state: Optional[str] = None,
                    on_board: Any = _UNSET,
                    for_board: Optional[str] = None) -> Dict[str, Any]:
        """Write one layout. With `layout_id` it REPLACES that layout's
        devices (and name, when given); without, it is a new one."""
        existing = self._read_layout(layout_id) if layout_id else None
        record = dict(existing or {})
        record["id"] = existing["id"] if existing else \
            f"lay-{_secrets.token_hex(4)}"
        record["name"] = self._unique_layout_name(
            (name or record.get("name") or "Layout").strip() or "Layout",
            record["id"])
        record["board"] = board.id
        record["family"] = board.family
        record["items"] = self._clean_layout_items(items)
        record["source"] = source or record.get("source", "")
        if state:
            record["state"] = state
        record.setdefault("state", "saved")
        if on_board is not _UNSET:
            record["on_board"] = on_board
        record.setdefault("on_board", None)
        if for_board:
            record["for_board"] = for_board
        record["created"] = (record.get("created") or created or _now())
        record["updated"] = _now()
        self._write_json(self._layout_path(record["id"]), record)
        return record

    def delete_layout(self, layout_id: str) -> bool:
        path = self._layout_path(layout_id)
        if path is None or not path.exists():
            return False
        try:
            path.unlink()
        except OSError:
            return False
        return True

    def migrate_drafts(self) -> int:
        """Carry every per-board draft (`items_pending`, the old way) into
        the library, then clear it from the board. Idempotent and cheap:
        the common case is one pass over the board files and no writes."""
        moved = 0
        for slug, record in self._boards():
            draft = record.get("items_pending")
            if not draft:
                if "items_pending" in record or "items_pending_at" in record:
                    record.pop("items_pending", None)
                    record.pop("items_pending_at", None)
                    self._write_board(record)
                continue
            name = record.get("name", slug)
            self.save_layout(f"{name} (not sent)", self._board_of(record),
                             draft,
                             source=f"an edit of {name} that never reached it",
                             created=record.get("items_pending_at"),
                             state="not_sent", **self._pending_for(slug))
            record.pop("items_pending", None)
            record.pop("items_pending_at", None)
            self._write_board(record)
            moved += 1
            event("ESP", f"{slug}: saved draft moved to Saved layouts")
        self._sync_all_boards()
        return moved

    # ── a board's wiring IS a layout ────────────────────────────────────

    def _board_layout_for(self, slug: str) -> Optional[Dict[str, Any]]:
        for rec in self._all_layouts():
            if rec.get("on_board") == slug:
                return rec
        return None

    def _sync_board_layout(self, record: Dict[str, Any],
                           existing: Any = _UNSET) -> None:
        """Make the board's layout say what the board record says. Writes
        only when something differs, so a plain list never churns files."""
        slug = record.get("slug")
        items = self._clean_layout_items(record.get("items") or [])
        if not slug or not items:
            return
        board = self._board_of(record)
        name = record.get("name") or slug
        if existing is _UNSET:
            existing = self._board_layout_for(slug)
        if existing is not None and \
                existing.get("items") == items and \
                existing.get("board") == board.id and \
                existing.get("family") == board.family and \
                existing.get("name") == self._unique_layout_name(
                    name, existing.get("id")):
            return
        self.save_layout(name, board, items,
                         source=f"the wiring of {name}",
                         layout_id=existing.get("id") if existing else None,
                         created=record.get("created"),
                         state="saved", on_board=slug)

    def _forget_board_layout(self, slug: str, board_name: str,
                             installed: bool) -> None:
        """A board is gone. NEVER INSTALLED: its wiring goes with it -- it
        was only ever a pending install (Notifications), and keeping it bred
        a second copy of the layout it started from (2026-11-04). INSTALLED:
        its wiring stays as a plain plan, unless that plan is already
        saved."""
        if installed:
            self._detach_board_layout(slug, board_name)
            return
        for rec in self._all_layouts():
            if rec.get("on_board") == slug or rec.get("for_board") == slug:
                self.delete_layout(rec["id"])

    #: What a layout is while the job around it is unfinished: shown in
    #: Notifications (Continue / Delete), never in Saved layouts.
    PENDING = ("waiting", "unfinished", "not_sent")

    def is_pending(self, rec: Dict[str, Any]) -> bool:
        return self.layout_view(rec)[0] in self.PENDING

    def pending_layouts(self) -> List[Dict[str, Any]]:
        """Unfinished designs and edits that never reached their board --
        the ones Notifications offers to continue. (A board waiting for its
        install has its own notice.)"""
        return [r for r in self._all_layouts()
                if not r.get("on_board")
                and r.get("state") in ("unfinished", "not_sent")]

    def _pending_for(self, slug: str) -> Dict[str, Any]:
        """save_layout arguments that keep ONE pending edit per board: a
        second failed attempt replaces the first instead of adding a copy."""
        for rec in self._all_layouts():
            if (rec.get("for_board") == slug and not rec.get("on_board")
                    and rec.get("state") in ("unfinished", "not_sent")):
                return {"layout_id": rec["id"], "for_board": slug}
        return {"for_board": slug}

    def _detach_board_layout(self, slug: str, board_name: str = "") -> None:
        """The board is gone; its wiring stays, as a plain saved plan."""
        for rec in self._all_layouts():
            if rec.get("on_board") != slug:
                continue
            if any(o.get("id") != rec.get("id") and not o.get("on_board")
                   and o.get("family") == rec.get("family")
                   and o.get("items") == rec.get("items")
                   for o in self._all_layouts()):
                self.delete_layout(rec["id"])       # already saved: no copy
                continue
            board = chips.get(rec.get("board"))
            self.save_layout(rec.get("name") or "Layout", board,
                             rec.get("items") or [],
                             source=f"was on {board_name or slug}",
                             layout_id=rec["id"], state="saved",
                             on_board=None)

    def _sync_all_boards(self) -> None:
        """Every board with wiring has its layout (boards from before this
        existed get theirs here), and a layout whose board is gone is a
        plain plan again. Idempotent; the common case writes nothing."""
        boards = dict(self._boards())
        layouts = self._all_layouts()
        by_board = {r.get("on_board"): r for r in layouts
                    if r.get("on_board")}
        for slug, record in boards.items():
            self._sync_board_layout(record, existing=by_board.get(slug))
        for slug, rec in by_board.items():
            if slug not in boards:
                self._detach_board_layout(slug)

    def layout_view(self, rec: Dict[str, Any]) -> Tuple[str, str]:
        """(status, board name) for a layout, read off the board it is on.
        status: flashed | waiting | unfinished | not_sent | saved."""
        slug = rec.get("on_board")
        if slug:
            board = self._read_board(slug)
            if board is not None:
                return ("flashed" if board.get("flashed") else "waiting",
                        board.get("name", slug))
        state = rec.get("state")
        return (state if state in ("unfinished", "not_sent") else "saved",
                "")

    def layout_is_boards(self, rec: Dict[str, Any]) -> Optional[str]:
        """The slug of the board this layout is the wiring of -- None when
        it is a plain plan (or its board is gone)."""
        slug = rec.get("on_board")
        return slug if slug and self._read_board(slug) is not None else None

    def _remember_layout(self, name: str, board: "chips.Board",
                         items: Sequence[Any], source: str,
                         state: str = "saved",
                         for_board: Optional[str] = None
                         ) -> Optional[Dict[str, Any]]:
        """Keep a design in Saved layouts, unless an identical one for the
        same chip is already there (starting from a layout and not changing
        it must not breed copies). Every draft, finished or not, ends up
        here -- and stays until its owner deletes it."""
        cleaned = self._clean_layout_items(items)
        if not cleaned:
            return None
        if for_board:
            return self.save_layout(name, board, cleaned, source=source,
                                    state=state, **self._pending_for(for_board))
        # A board's own layout does not count: the same plan on a second
        # board is a plan of its own, and an abandoned edit that equals what
        # a board holds has been handled by the caller.
        for rec in self._all_layouts(board.family):
            if not rec.get("on_board") and rec.get("items") == cleaned:
                return None
        return self.save_layout(name, board, cleaned, source=source,
                                state=state)

    async def pick_layout(self) -> Optional[str]:
        """Which saved layout to start a NEW board from. The chip is often
        already known (a board on the cable), so layouts for that chip lead
        the list and say so; the rest follow, marked, and are refused if
        they cannot fit the chip on the cable. None = backed out."""
        layouts = [r for r in self.list_layouts() if not self.is_pending(r)]
        if not layouts:
            await self._io.write(
                "  No saved layouts yet. Design a new board, or make one "
                "from Devices -> Layouts.",
                level="warn")
            return None
        cable = await self._peek_cable_family()
        if cable is not None:
            layouts.sort(key=lambda r: r.get("family") != cable.family)
        rows = []
        for rec in layouts:
            board = chips.get(rec.get("board"))
            n = len(rec.get("items") or [])
            sub = f"{board.family_label} · {n} device(s)"
            status, on_name = self.layout_view(rec)
            sub += " · " + self._status_words(status, on_name)
            if cable is not None:
                sub += (" -- fits the board on the cable"
                        if board.family == cable.family else
                        f" -- a different chip from the {cable.family_label}"
                        f" on the cable")
            rows.append({"value": rec["id"], "label": rec["name"],
                         "sub": sub})
        picked = await self._io.choose("Which saved layout?", rows)
        if not picked:
            return None
        chosen = self._read_layout(picked[0])
        if chosen is None:
            return None
        if cable is not None and chosen.get("family") != cable.family:
            await self._io.write(
                f"  {chosen['name']!r} is a layout for "
                f"{chips.get(chosen.get('board')).family_label}, but the "
                f"board on the cable is a {cable.family_label}. Pick one "
                f"for that chip, or unplug the cable to choose freely.",
                level="warn")
            return None
        return picked[0]

    @staticmethod
    def _status_words(status: str, board_name: str = "") -> str:
        return {"flashed": f"on {board_name}",
                "waiting": f"on {board_name}, not installed yet",
                "unfinished": "unfinished",
                "not_sent": "never reached its board"}.get(status, "saved")

    async def _lay_out(self, name: str, board: "chips.Board",
                       items: List[Dict[str, Any]]
                       ) -> Optional[List[Dict[str, Any]]]:
        """The pin screen (or the questions, on a console) for a LAYOUT.
        Returns the finished items, or None for backed out / nothing placed.

        A layout is designed with the module's facts left OPEN -- no PSRAM
        rule is applied here -- because it is not on any chip yet. The board
        that receives it applies its own rules (BoardPlan.add) when the
        layout is used, and says what could not go."""
        plan = cat.BoardPlan(name, owns_psram_pins=False, board=board)
        laid_out = await self._io.pin_map({
            "board_name": name,
            "catalogue": cat.describe(board),
            "items": items,
            "pins_owned_by_module": [],
            "pins_reported": [],
        })
        if laid_out is not None:
            if laid_out.get("cancelled"):
                return None
            for note in self._apply(plan, laid_out.get("items") or [],
                                    from_phone=True):
                await self._io.write(f"  ! {note}", level="warn")
        else:
            for note in self._apply(plan, items):
                await self._io.write(f"  ! {note}", level="warn")
            plan = await self._edit_by_questions(name, plan)
            if plan is None:
                return None
        if not plan.items:
            await self._io.write("  Nothing was placed -- nothing saved.")
            return None
        return self._record_items(plan)

    async def new_layout(self) -> Optional[Dict[str, Any]]:
        """Start a saved layout: from a blank design, or by copying what one
        of the boards is wired to now (to put the same wiring on another
        board of the same kind)."""
        picked = await self._io.choose(
            "Start the layout from...",
            [{"value": "new", "label": "A new design",
              "sub": "pick the chip, then place the devices"},
             {"value": "copy", "label": "A board's current wiring",
              "sub": "copy what one of your boards has, to reuse it"}])
        if not picked:
            return None
        if picked[0] == "copy":
            slug = await self._pick_existing_node()
            record = self._read_board(slug) if slug else None
            if record is None:
                return None
            items = self._clean_layout_items(record.get("items") or [])
            if not items:
                await self._io.write(
                    f"  {record.get('name', slug)} has nothing wired to "
                    f"copy.", level="warn")
                return None
            board = self._board_of(record)
            source = f"copied from {record.get('name', slug)}"
            default = f"{record.get('name', slug)} wiring"
        else:
            board = await self._ask_which_board()
            if board is None:
                return None
            items, source, default = [], "designed by you", ""
        name = await self._ask_name(
            "\n  Name this layout"
            + (f" (e.g. {default}): " if default else " (e.g. Relay board): "))
        if name is None:
            return None
        if not items:
            items = await self._lay_out(name, board, [])
            if items is None:
                await self._io.write("  Nothing saved.")
                return None
        saved = self.save_layout(name, board, items, source=source)
        await self._io.write(
            f"  Saved {saved['name']!r} for {board.family_label} boards -- "
            f"{len(saved['items'])} device(s). It controls nothing until you "
            f"use it on a board.")
        return saved

    async def edit_layout(self, layout_id: str) -> Optional[Dict[str, Any]]:
        record = self._read_layout(layout_id)
        if record is None:
            await self._io.write("  That layout is not here any more -- the "
                                 "list may be out of date.", level="warn")
            return None
        board = chips.get(record.get("board"))
        items = await self._lay_out(record["name"], board,
                                    record.get("items") or [])
        if items is None:
            await self._io.write("  Left as it was.")
            return None
        saved = self.save_layout(record["name"], board, items,
                                 layout_id=layout_id)
        await self._io.write(f"  Saved {saved['name']!r} -- "
                             f"{len(saved['items'])} device(s).")
        return saved

    async def rename_layout(self, layout_id: str) -> Optional[str]:
        record = self._read_layout(layout_id)
        if record is None:
            await self._io.write("  That layout is not here any more.",
                                 level="warn")
            return None
        if self.layout_is_boards(record):
            await self._io.write(
                f"  {record['name']!r} is the wiring of a board and takes "
                f"that board's name. Rename the board instead.",
                level="warn")
            return None
        new = await self._ask_name(
            f"\n  Call {record['name']!r} what instead? ")
        if new is None or new == record["name"]:
            await self._io.write("  Left as it was.")
            return None
        saved = self.save_layout(new, chips.get(record.get("board")),
                                 record.get("items") or [],
                                 layout_id=layout_id)
        await self._io.write(f"  Now called {saved['name']!r}.")
        return saved["name"]

    # ── the Wi-Fi the boards join ───────────────────────────────────────

    def _read_wifi(self) -> Dict[str, str]:
        try:
            wifi = json.loads(self._wifi_path().read_text("utf-8"))
        except Exception:
            return {}
        return wifi if isinstance(wifi, dict) else {}

    def wifi_ssid(self) -> str:
        return self._read_wifi().get("ssid", "")

    async def _ensure_wifi(self, *, force_change: bool = False
                           ) -> Optional[Dict[str, str]]:
        """The network every board joins. Asked once, then remembered."""
        current = self._read_wifi()
        if current.get("ssid") and not force_change:
            return current
        if current.get("ssid"):
            await self._io.write(
                f"\n  The boards currently join {current['ssid']!r}.")
        else:
            await self._io.write(
                "\n  Your boards need a Wi-Fi network to reach this hub. "
                "This is asked once and remembered.")
        ssid = await self._io.ainput("  Wi-Fi network name (SSID): ",
                                     patient=True)
        if means_cancel(ssid):
            return None
        ssid = ssid.strip()
        if not ssid:
            await self._io.write("  A network name is needed.", level="warn")
            return None
        # THE SEMICOLON RULE (audit, 2026-08-25). These values travel to
        # the board inside a Backlog line joined with "; ", and Tasmota
        # reads a semicolon as "next command" -- so an SSID or password
        # carrying one would be silently CUT, the fragment kept, and the
        # board stranded on a network name that does not exist. Refused at
        # the question, where it can still be fixed, with the reason said.
        if ";" in ssid:
            await self._io.write(
                "  A semicolon cannot travel in a network name here -- it "
                "is sent to boards inside a command line where ';' means "
                "'next command', so the name would be cut at that point. "
                "If the network is really called that, rename the network.",
                level="warn")
            return None
        # NO CANCEL-TOKEN CHECK ON THE PASSWORD. A secret is an opaque
        # string; parsing it means a Wi-Fi password of "no" cannot be typed.
        password = await self._io.ainput("  Wi-Fi password: ",
                                         patient=True, secret=True)
        if ";" in password:
            await self._io.write(
                "  A semicolon cannot travel in the Wi-Fi password -- it is "
                "sent to boards inside a command line where ';' means 'next "
                "command', so everything after it would be lost and every "
                "board would join with a truncated password, silently. "
                "Change the network's password to one without ';'.",
                level="warn")
            return None
        # AND WHERE THE HUB IS ON THAT NETWORK -- a fact about this network,
        # not about this machine, which is why it is stored beside the SSID.
        guess = self._detect_address()
        moving = bool(current.get("ssid")) and ssid != current.get("ssid")
        if moving and guess and not self._runs_access_point():
            # A MOVE TO A DIFFERENT ROUTER. The address this machine holds
            # now belongs to the OLD network; offering it as the guess
            # sent boards to look for the hub where it will not be.
            await self._io.write(
                f"\n  {ssid!r} is a different network from {current['ssid']!r}, "
                f"so this hub's address there is not known yet. Type the "
                f"address you reserved for it in that router (its admin "
                f"page, 'static IP' or 'address reservation', by this "
                f"machine's Wi-Fi MAC). {guess} is its address on the OLD "
                f"network and will not work there.")
            guess = ""
        elif guess:
            await self._io.write(
                f"\n  Boards will look for this hub at an address on that "
                f"network. This machine appears to be {guess}.")
        else:
            await self._io.write(
                "\n  Boards need this hub's address on that network. It "
                "could not be worked out automatically. If this hub RUNS the "
                "network -- a Pi in hotspot mode -- it is the access point's "
                "own address, not whichever one reaches the internet.")
        typed = await self._io.ainput(
            f"  Hub address on {ssid}"
            f"{f' [{guess}]' if guess else ''}: ", patient=True)
        if means_cancel(typed):
            return None
        address = typed.strip() or guess
        if moving and not address:
            # Left blank, hub_address() would fall back to what this machine
            # holds NOW -- the old network's address -- and every board
            # would be sent there.
            await self._io.write(
                f"  This hub's address on {ssid!r} is needed -- nothing was "
                f"changed.", level="warn")
            return None
        if address and not self.reachable_by_a_board(address):
            await self._io.write(
                f"  {address} is not an address a board can reach -- it is "
                f"either this machine talking to itself, a tailnet address, "
                f"or a public one.", level="warn")
            return None
        record = {"ssid": ssid, "password": password, "hub_address": address}
        self._write_json(self._wifi_path(), record)
        return record

    async def change_wifi(self) -> bool:
        """Move every board to a different network. One flow, no ceremony.
        Reachable over the air -> told in one backlog. Unreachable but on the
        USB cable -> told over the cable, and read back. Neither -> named,
        with the one instruction that fixes it.
        The cost of the simple version is stated plainly: a mistyped password
        strands the board until a cable reaches it, and the cable path is
        part of this same flow.
        """
        # THE ADDRESS THE BROKER ANSWERS ON TODAY, read before the record
        # changes. The over-the-air push has to reach the broker where the
        # boards currently find it; dialling the NEW address (which this
        # machine may not hold yet) reported every board unreachable.
        old_hub = (self._read_wifi().get("hub_address") or "").strip()
        via = old_hub if old_hub and hub_net.holds_address(old_hub) \
            else "127.0.0.1"
        wifi = await self._ensure_wifi(force_change=True)
        if wifi is None:
            await self._io.write("  Left as it was.")
            return False
        boards = self.list_nodes()
        here = self.hub_address()
        await self._io.write(
            f"  Saved. Boards join {wifi['ssid']!r} and look for this hub "
            f"at {here}.")
        if here == "127.0.0.1":
            await self._io.write(
                "  ! This machine's address on that network could not be "
                "worked out, so boards would be told to look for the hub at "
                "themselves. Set MQTT_HOST in config.py.", level="warn")
        if not await self._say_if_plaintext():
            return False
        if not boards:
            # No boards to move -- but the broker's certificate and the hub
            # itself may still need to follow the network.
            await self._broker_cert_must_name(here)
            await self._offer_hub_move(wifi, here)
            return True
        moved: List[str] = []
        for slug in boards:
            board = self._read_board(slug)
            if not board:
                continue
            name = board.get("name", slug)
            # Obsolete two-phase state from any earlier attempt: this flow
            # leaves a board holding ONE network.
            if board.pop("wifi_pending", None) is not None:
                self._write_board(board)
            # Identity is not changing, so only the network half is sent --
            # through the broker at its CURRENT address (`via`), carrying
            # the new one.
            if await self._publish(
                    board, self._provisioning_commands(board, identity=False),
                    host=via):
                moved.append(name)
                await self._io.write(
                    f"  {name}: told over the air -- restarting onto "
                    f"{wifi['ssid']!r}.")
                continue
            # Not reachable. If a board is on the cable RIGHT NOW, use it.
            # With SEVERAL ports the question is asked rather than the
            # cable path silently skipped -- a bench with a second dongle
            # plugged in used to lose this fallback without a word
            # (audit, 2026-08-25). "None of these" is a real answer.
            ports = self._serial_ports()
            port = None
            if ports is None:
                await self._io.write(
                    "  pyserial is not installed on this hub (pip install "
                    f"pyserial), so there is no cable fallback for {name} "
                    f"this run, plugged in or not.", level="warn")
            elif len(ports) == 1:
                port = ports[0]["value"]
            elif len(ports) > 1:
                picked_port = await self._io.choose(
                    f"{name} is not reachable over the air. Is it on one "
                    f"of these cables?",
                    ports + [{"value": "\x00none",
                              "label": "None of these",
                              "sub": "skip the cable for this board"}])
                if picked_port and picked_port[0] != "\x00none":
                    port = picked_port[0]
            if port:
                await self._io.write(
                    f"  {name}: not reachable over the air -- trying the "
                    f"board on {port}...")
                if await self._send_over_serial(
                        port, self._provisioning_line(board), board):
                    took = await self._settings_took(port, wifi["ssid"],
                                                     self.hub_address())
                    if took is False:
                        await self._io.write(
                            f"  {name} took the commands but did not KEEP "
                            f"them. Run this again; if it repeats, its "
                            f"console will say why.", level="warn")
                        continue
                    suffix = ("" if took else
                              " (could not be read back -- watch its log)")
                    await self._io.write(
                        f"  {name}: switched over the cable{suffix}.")
                    moved.append(name)
                    continue
            await self._io.write(
                f"  {name}: not reachable over the air and not on a cable. "
                f"Plug it into this machine over USB and run Change ESP "
                f"Wi-Fi again -- it will be picked up automatically.",
                level="warn")
        await self._io.write(
            f"\n  {len(moved)} of {len(boards)} board(s) moved. Each one now "
            f"holds ONLY the new network.")
        await self._broker_cert_must_name(here)
        await self._offer_hub_move(wifi, here)
        return True

    # ── the hub following its boards onto a router ───────────────────────

    async def _broker_cert_must_name(self, address: str) -> None:
        """Tasmota checks the NAME in the broker's certificate as well as
        its fingerprint (error 56, see setup_manager._broker_cert_names),
        so a board sent to look for the hub at an address the certificate
        does not carry is refused at the handshake. Regenerate when needed.

        Safe for the boards: _self_sign reuses the broker's private key, so
        the fingerprint they pin does not change -- only the names do. The
        broker is reloaded (SIGHUP re-reads the TLS certificate too) to serve
        the new one; boards reconnect on their own within seconds."""
        if not address or address == "127.0.0.1":
            return
        if address in self._broker_cert_names():
            return
        from app import setup_manager as _sm
        await self._io.write(
            f"  The broker's certificate must name {address} for the boards "
            f"to accept it there -- regenerating it (same key, so nothing "
            f"the boards pin changes) and reloading the broker.")
        ok = await asyncio.to_thread(_sm.generate_broker_cert, True)
        if not ok:
            await self._io.write("  ! Could not regenerate the broker's "
                                 "certificate. See the hub's console.", level="warn")
            return
        lines: List[str] = []
        # reload_accounts, not stop+start: it SIGHUPs a broker this hub
        # started, runs MQTT_RELOAD_COMMAND for a system one (which the old
        # stop_broker never touched while reporting "restarted"), and says
        # so when it can do neither.
        reloaded = await asyncio.to_thread(_sm.reload_accounts, lines.append)
        for line in lines:
            if line.strip():
                print(f"[NET] {line.strip()}")
        if reloaded:
            await self._io.write("  The broker now serves the new certificate.")
        else:
            await self._io.write(
                "  ! The running broker still serves the OLD certificate -- "
                "restart it, or boards will refuse it on the new network.",
                level="warn")

    def _broker_cert_names(self) -> List[str]:
        """The names the broker's certificate carries. [] when unreadable."""
        path = _config.BROKER_TLS_CERT
        try:
            from cryptography import x509
            with open(path, "rb") as f:
                cert = x509.load_pem_x509_certificate(f.read())
            san = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
            return [str(n) for n in san.get_values_for_type(x509.DNSName)]
        except Exception:                              # noqa: BLE001
            return []

    def _runs_access_point(self) -> bool:
        return any(hub_net.holds_address(a) for a in hub_net.AP_ADDRESSES)

    async def _offer_hub_move(self, wifi: Dict[str, str], address: str) -> None:
        """The boards have been told where to go. Should this hub follow?

        Offered only when it makes sense: this machine is a CLIENT of a
        network (not running the access point the boards used to join),
        NetworkManager is here to do it, and the new network is not the
        one it is on. The switch happens a few seconds after the answer,
        off the event loop, so the phone gets the last words before its
        connection drops -- it will have to join the new Wi-Fi itself."""
        if self._runs_access_point() or not shutil.which("nmcli"):
            return
        target = wifi.get("ssid", "")
        if not target:
            return
        # THE NETWORK, not the profile's name. The hub may already be on
        # the boards' new Wi-Fi under a profile called anything at all
        # ('homevoice-router'); offering it a move to where it is sent it
        # into nmcli, which refused, and then into a fallback that reported
        # a loss that never happened (2026-09-09).
        current_ssid = await asyncio.to_thread(hub_net.active_ssid)
        if current_ssid == target:
            await self._io.write(
                f"  This hub is already on {target!r}; nothing to move.")
            return
        picked = await self._io.choose(
            f"Move this hub onto {target!r} as well?",
            [{"value": "yes", "label": f"Yes -- switch this hub to {target!r}",
              "sub": f"in a few seconds; join {target!r} on your phone and "
                     f"find the hub at {address}"},
             {"value": "no", "label": "No -- the boards only",
              "sub": "this hub stays on its current network"}])
        if not picked or picked[0] != "yes":
            return
        await self._io.write(
            f"\n  Switching this hub to {target!r} in 5 seconds. This "
            f"connection will drop. Join {target!r} on your phone; the hub "
            f"will be at {address}, and the pairing code (--pair) carries "
            f"that address if the app needs to be told. If the new network "
            f"will not come up, the hub falls back to its current one.")

        def _switch() -> None:
            time.sleep(5.0)
            ok, detail = hub_net.join_wifi(target, wifi.get("password", ""))
            if ok:
                holds = hub_net.holds_address(address)
                print(f"[NET] hub is on {target!r}; "
                      f"{'at ' + address + ' as the boards expect' if holds else 'BUT NOT at ' + address + ' -- the boards were told to look there. Reserve that address in the router for this machine, or run Change ESP Wi-Fi again with the address it now holds.'}")
            else:
                print(f"[NET] the hub did NOT move: {detail}")

        threading.Thread(target=_switch, name="hub-wifi-move", daemon=True).start()

    # ── designing a board ───────────────────────────────────────────────

    async def _ask_name(self, prompt: str) -> Optional[str]:
        text = await self._io.ainput(prompt, patient=True)
        if means_cancel(text):
            return None
        text = text.strip()
        if ";" in text:
            # Names travel in command lines too -- DeviceName in
            # provisioning, FriendlyName with the wiring -- where ';'
            # means 'next command' and the tail becomes one (audit,
            # 2026-08-25). Refused with the reason, not silently mangled.
            await self._io.write(
                "  A name cannot carry a semicolon -- it is sent to the "
                "board inside a command line where ';' means 'next "
                "command'. A comma works fine.", level="warn")
            return None
        return text or None

    async def _pick_pin(self, plan: "cat.BoardPlan", device: "cat.DeviceType",
                        slot: "cat.PinSlot") -> Optional[int]:
        """Which pin for one slot of a device.
        Pins that will not work for this particular job are still SHOWN,
        marked and sorted last. Only pins the catalogue outright blocks, and
        pins already taken by something else, are left out.
        """
        # An existing shared bus never reaches here: _add_one joins it first.
        held = plan.taken()
        options: List[Dict[str, str]] = []
        awkward: List[Dict[str, str]] = []
        broken: List[Dict[str, str]] = []
        for info in cat.pins(plan.board):
            severity, message = cat.check(device.id, slot.key, info.number,
                                          plan.board)
            if cat.blocks(severity):
                continue
            # Taken pins are out for a FRESH shared slot too. A shared
            # slot reaching this loop means no bus exists yet (an existing
            # bus returned early above), so it claims a pin like anything
            # else -- offering a relay's pin here let the question be
            # answered and then refused three questions later (audit,
            # 2026-08-25).
            if held.get(info.number):
                continue
            row = {"value": str(info.number),
                   "label": f"GPIO{info.number}",
                   "sub": message or info.purpose}
            if info.number == slot.default_pin:
                # The pin the driver's own code names: offered first.
                row["label"] += "  (set in the driver)"
                options.insert(0, row)
                continue
            if severity == cat.WONT_WORK:
                row["label"] += "  (will not work)"
                broken.append(row)
            elif severity == cat.WARN:
                row["label"] += "  (read this)"
                awkward.append(row)
            else:
                options.append(row)
        picked = await self._io.choose(
            f"{device.label} -- {slot.label}", options + awkward + broken)
        return int(picked[0]) if picked else None

    async def _say_load_notes(self) -> None:
        """SAY WHICH VERSION IS RUNNING, once. A day of hand-installed
        deliveries produced flows that behaved one version apart, and
        the person could not tell WHICH esp_builder was answering --
        "this inconsistency is what I get tired of telling you"
        (2026-08-25, 5am, and she was right: the file was one delivery
        behind, and nothing on screen could have said so). One line at
        the first question makes every paste self-dating. The
        board-file notes ride with it."""
        if self._said_sidecar_notes:
            return
        self._said_sidecar_notes = True
        # BOTH stamps, because they can genuinely differ: the pair
        # check at import refuses a catalogue missing what this
        # builder needs, but two 25c-or-later deliveries could still
        # sit one apart. One line makes every paste self-dating for
        # both files at once (audit, 2026-08-25).
        await self._io.write(
            f"  (esp_builder {REVISION}; catalogue "
            f"{getattr(cat, 'REVISION', '?')})")
        for note in getattr(self, "_board_load_notes", []):
            if re.search(r"not loaded|built-in|could not", note):
                await self._io.write(f"  ! board file: {note}", level="warn")
            else:
                await self._io.write(f"  board file: {note}")

    async def _pick_device(self) -> Optional[str]:
        """What is being added -- the DEVICE, before any pin.

        This flow ran PIN FIRST for a long time, on the argument that you
        are looking at a board with a wire in GPIO13 and should not have to
        know which catalogue category a relay lives in. That argument had a
        cost nobody priced: the catalogue-on-one-pin listing had to print
        every SLOT of every device as its own row, so an RC522 appeared
        FIVE times -- cs, rst, clk, mosi, miso -- each row wearing the same
        sentence, with the PN532 twice behind it. Fifty rows of that is not
        a question, it is a wall. And the premise was wrong anyway: the
        person is holding a READER, not a wire. The device is the thing
        they know; the pins are what this hub is supposed to walk them
        through, one slot at a time, with the valid pins for THAT slot in
        front of them. Reversed 2026-08-24 on Augusta's call: device first,
        one row per device, pins after -- and later the same day, ONE list
        instead of a branch question in front of it.

        Returns a type_id, or None for backed out.
        """
        await self._say_load_notes()
        # THE WHOLE CATALOGUE AT ONCE, one row per device, in the
        # catalogue's own group order -- roughly commonest first (sorting
        # by name once put nine kinds of input above "relay"). There used
        # to be a branch question first (does / reads / other -> a short
        # list -> the full list), which earned its keep exactly once by
        # stopping a yellow LED from being described as an RGB light --
        # but it charged every single add one extra answer for it.
        # Augusta, 2026-08-24: "should just show everything at once."
        # Each row carries its group and its note, so the scan stays
        # honest without the toll booth.
        order = {name: i for i, name in enumerate(cat.groups())}
        rows = []
        for device in sorted(cat.CATALOGUE,
                             key=lambda d: order.get(d.group, 99)):
            note = device.note.split(".")[0]
            rows.append({"value": device.id,
                         "label": device.label,
                         "sub": f"{device.group} -- {note}" if note
                                else device.group})
        chosen = await self._io.choose("What are you adding?", rows)
        return chosen[0] if chosen else None

    @staticmethod
    def _bus_pin(plan: "cat.BoardPlan",
                 slot: "cat.PinSlot") -> Optional[int]:
        """The pin a shared bus already lives on this plan, or None.

        Why a second SPI device costs ONE pin, not four: its clk, mosi and
        miso are not ASKED, they join the bus that is already there. Asking
        would be worse than redundant -- a different answer would mean a
        template with two SPI clocks, which plan.add would refuse three
        questions after the person thought they were done.
        """
        return plan._shared_pin(slot) if slot.shared else None

    async def _add_one(self, plan: "cat.BoardPlan") -> bool:
        """One thing wired to the board, DEVICE FIRST -- _pick_device says
        why that order. False means backed out."""
        held = plan.taken()
        no_free_pins = not [i for i in cat.pins(plan.board)
                            if i.number not in held]
        # A board with a shared bus is never "full": an I²C sensor joins
        # the bus at zero pin cost, so the every-pin-used early exit was
        # turning away exactly the additions that still fit (audit,
        # 2026-08-25).
        has_bus = any(slot.shared
                      for item in plan.items
                      for slot in cat.BY_ID[item.type_id].slots)
        if no_free_pins and not has_bus:
            await self._io.write("  Every pin on this board is used.")
            return False
        type_id = await self._pick_device()
        if type_id is None:
            return False
        device = cat.BY_ID[type_id]
        if device.note:
            await self._io.write(f"  {device.note}")
        pins: Dict[str, int] = {}
        for slot in device.slots:
            bus = self._bus_pin(plan, slot)
            if bus is not None:
                pins[slot.key] = bus
                await self._io.write(
                    f"  {slot.label}: joins the bus already on GPIO{bus}.")
                continue
            more = await self._pick_pin(plan, device, slot)
            if more is None:
                return True              # back to the board, nothing lost
            pins[slot.key] = more
        name = await self._ask_name("  What do you call it? ")
        if name is None:
            return True
        try:
            cautions = plan.add(device.id, name, pins)
        except cat.PlacementError as exc:
            # THE SEVERITY RIDES ON THE EXCEPTION now. This used to grep
            # the prose for "transistor" / "ADC2" / "touch channel" to
            # decide whether the refusal was confirmable, which meant a
            # reworded sentence silently changed which refusals offered
            # the override (audit, 2026-08-25). A "will not work" is an
            # unanswered question, not an error; everything else is final.
            if exc.severity == cat.WONT_WORK:
                await self._io.write(f"  {exc}", level="warn")
                confirm = await self._io.choose(
                    "Use it anyway?",
                    [{"value": "no", "label": "Pick a different pin",
                      "sub": "recommended"},
                     {"value": "yes", "label": "Use it anyway",
                      "sub": "you know something this hub does not"}])
                if not confirm or confirm[0] != "yes":
                    return True
                try:
                    cautions = plan.add(device.id, name, pins, anyway=True)
                except cat.PlacementError as second:
                    # `anyway` reopens ONE door; a later slot can still
                    # refuse outright, and that refusal is final too.
                    await self._io.write(f"  {second}", level="warn")
                    return True
            else:
                await self._io.write(f"  {exc}", level="warn")
                return True
        for caution in cautions:
            await self._io.write(f"  ! {caution}", level="warn")
        where = ", ".join(f"{slot.key} GPIO{pins[slot.key]}"
                          if len(device.slots) > 1 else f"GPIO{pins[slot.key]}"
                          for slot in device.slots)
        await self._io.write(f"  Added {name}" + (f" on {where}." if where
                                                   else "."))
        return True

    @staticmethod
    def _apply(plan: "cat.BoardPlan",
               items: List[Dict[str, Any]], *,
               from_phone: bool = False) -> List[str]:
        """Put the client's layout into a real BoardPlan, and report.
        THE CLIENT IS NOT TRUSTED. It is a second implementation of the same
        rules, shipped separately, so it WILL disagree eventually. An
        override is honoured only for an item carrying `confirmed`, which the
        client sets after showing the warning and getting an answer -- with a
        blanket override a client bug becomes a board that does nothing.
        Returns rejections AND the catalogue's cautions, so the pin-screen
        path surfaces what the question path prints.
        """
        notes: List[str] = []
        for item in items:
            if not isinstance(item, dict):
                continue
            try:
                name = str(item["name"])
                if from_phone:
                    name = name.strip()
                # A NAME FROM THE PIN SCREEN obeys the naming question's
                # rules (_ask_name): not blank, no ';' -- the name rides in
                # FriendlyName inside a Backlog line where ';' starts the
                # next command. Records already on disk are not re-judged.
                if from_phone and not name:
                    raise ValueError("it has no name.")
                if from_phone and ";" in name:
                    raise ValueError("a name cannot carry a semicolon.")
                notes.extend(plan.add(
                    str(item["type_id"]), name,
                    {str(k): int(v)
                     for k, v in (item.get("pins") or {}).items()},
                    anyway=bool(item.get("confirmed"))))
            except (ValueError, KeyError, TypeError, AttributeError) as exc:
                # AttributeError: `pins` sent as a list, not an object.
                notes.append(
                    f"{item.get('name') or 'one device'} was not added: {exc}")
        return notes

    #: (board, note) pairs board_wiring has already put in the log.
    _wiring_notes_said: set = set()

    def board_wiring(self, record: Dict[str, Any]) -> List[Dict[str, Any]]:
        """What is on each pin of a saved board, in the words Tasmota uses.

        HER ASK, 2026-08-29: "for any devices added, the pin information is
        also shown ... then maybe u put a tag on devices with esp connected".

        Nothing new is stored to answer it. The board record already holds
        items: [{type_id, name, pins}], and the INSTANCE number -- the 1 in
        Power1 -- falls out of rebuilding the plan, because _allocate() is
        deterministic: same items in the same order, same numbers, every
        time. So this reads what is already on disk and says it out loud.

        Why it matters more than it sounds: the Power numbers are NOT in pin
        order. They follow the order things were added, so a board can
        perfectly well have Power2 on GPIO27 and Power3 on GPIO26 -- and it
        does. She sent Power1/2/3 believing she was driving pins 14, 26 and
        27, and the board drove 4, 27 and 26. Nothing on any screen said
        otherwise; the only way to find out was to read a raw template array.

        Returns one row per SLOT (a two-pin sensor gets two), each with the
        device name, the pin, and the control that owns it.
        """
        rows: List[Dict[str, Any]] = []
        try:
            # WITH WHAT THIS BOARD SAID ABOUT ITSELF, like every other plan
            # built from a record. Without it GPIO16/17 were treated as
            # "may be the module's own" on a board that had already
            # answered: Kitchen's power meter on GPIO17 was "not added" on
            # every List devices, and missing from the wiring table (field
            # log, 2026-10-05).
            plan = cat.BoardPlan(record.get("name") or record.get("slug", ""),
                                 owns_psram_pins=self._owns_psram_pins(record),
                                 board=self._board_of(record))
            for note in self._apply(plan, record.get("items") or []):
                # A record that no longer fits the catalogue is worth
                # reporting ONCE, not on every list, and not worth losing
                # the whole table over.
                said = (record.get("slug", ""), note)
                if said not in self._wiring_notes_said:
                    self._wiring_notes_said.add(said)
                    event("BOARD", f"wiring: {note}")
            for item in plan.items:
                device = cat.BY_ID[item.type_id]
                for slot in device.slots:
                    pin = item.pins.get(slot.key)
                    if pin is None:
                        continue
                    instance = item.instances.get(slot.key, 1)
                    family = slot.component.name
                    rows.append({
                        "name": item.name,
                        "pin": str(pin),
                        # "Power1" for a relay, "PWM2" for a dimmer channel --
                        # the name the person will actually type at a console
                        # or hear in a Tasmota answer.
                        "control": (f"Power{instance}" if family == "Relay"
                                    else f"{family}{instance}"
                                    if device.slots and slot.component.slots > 1
                                    else family),
                        "slot": slot.label,
                        "type_id": item.type_id,
                    })
        except Exception as exc:                   # noqa: BLE001
            event("BOARD", f"could not read the wiring of "
                           f"{record.get('slug', '?')}: {exc}")
            return []
        return rows

    # ── which board is it ───────────────────────────────────────────────
    #
    # THE PERSON SAYS WHICH BOARD IT IS; the cable double-checks at install,
    # before anything is erased (_chip_mismatch). A board no longer has to
    # be on the cable to be designed, and any ESP32 family can be added --
    # a profile in chips.py or a file in boards/, not new code here.

    #: USB bridges ESP boards ship with: Espressif's own USB, WCH (CH340,
    #: CH9102), Silicon Labs (CP210x), FTDI. Anything else on the only port
    #: is not asked -- esptool toggles reset lines, and a Zigbee stick
    #: behind the same bridge chip must not be bounced into its bootloader.
    _ESP_BRIDGES = (0x303A, 0x1A86, 0x10C4, 0x0403)
    _NOT_AN_ESP = re.compile(
        r"zigbee|zbdongle|sonoff|conbee|skyconnect|slzb|z-?wave|zwa|ezsp|"
        r"thread|matter", re.I)

    async def _peek_cable_family(self) -> Optional["chips.Board"]:
        """The family of a board ALREADY on the cable, or None -- a hint for
        the board question, never a requirement. Only asked when exactly one
        port is present and it looks like an ESP board's bridge. Never
        raises; says one line when it asks."""
        try:
            from serial.tools import list_ports
            ports = list(list_ports.comports())
        except Exception:                          # noqa: BLE001
            return None
        if len(ports) != 1:
            return None
        port = ports[0]
        described = f"{port.description or ''} {port.product or ''} " \
                    f"{port.manufacturer or ''}"
        if getattr(port, "vid", None) not in self._ESP_BRIDGES \
                or self._NOT_AN_ESP.search(described):
            return None
        try:
            await self._io.write(
                f"  Asking the board on {port.device} what it is (a hint "
                f"only; no cable is needed)...")
            chip = await self._identify_chip(port.device, quiet=True,
                                             timeout=20.0)
        except Exception:                          # noqa: BLE001
            return None
        return chips.family_of_chip(chip) if chip else None

    async def _ask_which_board(self) -> Optional["chips.Board"]:
        """WHICH BOARD? -- every profile the hub knows (built in, and
        boards/*.json), its family and its note beside it. A board already
        on the cable puts its family first, marked; none is needed.
        None = backed out."""
        await self._say_load_notes()
        cable = await self._peek_cable_family()
        boards = chips.pickable_boards()
        if cable is not None:
            # Stable: the cable's family moves up, its own order kept.
            boards.sort(key=lambda b: b.family != cable.family)
        rows = []
        for board in boards:
            here = cable is not None and board.family == cable.family
            rows.append({
                "value": board.id,
                "label": board.label + ("  (on the cable now)" if here
                                        else ""),
                "sub": board.family_label + (f" -- {board.note}"
                                             if board.note else "")})
        picked = await self._io.choose("Which kind of board is it?", rows)
        if not picked or not chips.known(picked[0]):
            return None
        return chips.get(picked[0])

    def _refit_problems(self, record: Dict[str, Any],
                        board: "chips.Board") -> List[str]:
        """What of this record's wiring would NOT fit
        `board` -- one sentence per device, naming its pins. Empty = every
        placement is valid there (cat's own rules: BoardPlan.add, the
        module's PSRAM included)."""
        same_family = board.family == self._board_of(record).family
        problems: List[str] = []
        for label, items in (("", record.get("items") or []),):
            # The module's facts belong to the chip it was met as: another
            # family starts again at "not asked" (refused, the safe way).
            plan = cat.BoardPlan(
                record.get("name") or record.get("slug", ""),
                owns_psram_pins=(self._owns_psram_pins(record)
                                 if same_family else None),
                board=board)
            for item in items:
                if not isinstance(item, dict):
                    continue
                pins: Dict[str, int] = {}
                try:
                    pins = {str(k): int(v)
                            for k, v in (item.get("pins") or {}).items()}
                    plan.add(str(item["type_id"]), str(item["name"]), pins,
                             anyway=True)
                except (ValueError, KeyError, TypeError,
                        AttributeError) as exc:
                    where = ", ".join(f"GPIO{n}" for n in
                                      sorted(set(pins.values()))) or "?"
                    problems.append(f"{label}{item.get('name', '?')} "
                                    f"({where}): {exc}")
        return problems

    async def change_board_type(self, slug: str,
                                board: Optional["chips.Board"] = None
                                ) -> bool:
        """Say a board is a different kind of board -- for when it was
        chosen wrong. Only when that board has pins for every current
        placement (re-validated, refused naming the pins that do not fit).
        A different FAMILY is a different chip: what the old one said about
        itself is forgotten, and an installed board must be installed again
        (a full erase and that family's image). True when changed."""
        record = self._read_board(slug)
        if record is None:
            await self._io.write(f"  No board called {slug!r}.", level="warn")
            return False
        name = record.get("name", slug)
        current = self._board_of(record)
        if board is None:
            await self._say_load_notes()
            rows = [{"value": b.id,
                     "label": b.label + ("  (now)" if b.id == current.id
                                         else ""),
                     "sub": b.family_label + (f" -- {b.note}"
                                              if b.note else "")}
                    for b in chips.pickable_boards()]
            picked = await self._io.choose(
                f"{name} is set as {current.label}. Which board is it "
                f"really?", rows)
            if not picked or not chips.known(picked[0]):
                await self._io.write("  Left as it was.")
                return False
            board = chips.get(picked[0])
        if board.id == current.id:
            await self._io.write(f"  {name} is already {board.label}.")
            return False
        problems = self._refit_problems(record, board)
        if problems:
            await self._io.write(
                f"  {name} cannot become {board.label}: its wiring does not "
                f"fit that board. Move these first (Edit its wiring), then "
                f"change the type:", level="warn")
            for problem in problems:
                await self._io.write(f"    {problem}", level="warn")
            return False
        record["board"] = board.id
        record["board_family"] = board.family
        if board.family != current.family:
            for fact in ("chip", "chip_features", "psram", "pins_reported"):
                record.pop(fact, None)
            if record.get("flashed"):
                record["flashed"] = ""
                record.pop("network_pending", None)
                self._write_board(record)
                await self._io.write(
                    f"  {name} is now {board.label}. The chip on it holds "
                    f"{current.family_label} firmware, so it must be "
                    f"installed again over the cable -- a full erase and "
                    f"{board.firmware}. Add device -> Set up an ESP board -> "
                    f"{name} (listed as still to finish) -> Install.",
                    level="warn")
                event("ESP", f"{slug}: board type {current.id} -> {board.id}")
                return True
        self._write_board(record)
        event("ESP", f"{slug}: board type {current.id} -> {board.id}")
        await self._io.write(
            f"  {name} is now {board.label}."
            + (" The new pin table reaches the chip with its next wiring "
               "send (an edit, or an install)." if record.get("flashed")
               else ""))
        return True

    async def _offer_cable_family(self, slug: str, chip: str) -> None:
        """After a chip mismatch (nothing erased): offer to make the board
        the family the cable found -- only when the wiring fits it. The
        install still stops here; the next one flashes the right image."""
        cable = chips.family_of_chip(chip)
        record = self._read_board(slug)
        if cable is None or record is None:
            return
        problems = self._refit_problems(record, cable)
        if problems:
            await self._io.write(
                f"  Its wiring would not fit an {cable.family_label} as it "
                f"is: {'; '.join(problems[:3])}", level="warn")
            return
        picked = await self._io.choose(
            f"Make {record.get('name', slug)} {cable.label}?",
            [{"value": "change", "label": f"Yes, it is {cable.label}",
              "sub": "the wiring fits; then install it again"}],
            cancel_label="No, leave it as it is")
        if picked and picked[0] == "change" \
                and await self.change_board_type(slug, cable):
            await self._io.write("  Install it again to flash it.")

    async def menu_add_esp_node(self, layout_id: Optional[str] = None
                                ) -> bool:
        """Describe a board. Nothing is flashed here -- this is only the plan,
        saved before anything physical happens, so a cable that falls out
        costs a flash and not an evening.

        `layout_id` starts the design from a SAVED LAYOUT: its chip decides
        the board kind (no question), and the pin screen opens on its
        devices. Every design made here -- finished or abandoned halfway --
        is also kept in Saved layouts."""
        layout = self._read_layout(layout_id) if layout_id else None
        if layout_id and layout is None:
            await self._io.write("  That layout is not here any more.",
                                 level="warn")
            return False
        if await self._ensure_wifi() is None:
            await self._io.write("  Cancelled -- nothing saved.")
            return False
        name = await self._ask_name("\n  Name this board (e.g. Kitchen): ")
        if name is None:
            await self._io.write("  Cancelled -- nothing saved.")
            return False
        slug = _slugify(name)
        if self._board_path(slug).exists():
            await self._io.write(
                f"  {name!r} would share a record with a board that already "
                f"exists. Pick another name.", level="warn")
            return False
        # WHICH BOARD, chosen by the person -- the cable only double-checks
        # it at install, before anything is erased.
        if layout is not None:
            board = chips.get(layout.get("board"))
            start_items = list(layout.get("items") or [])
            await self._io.write(
                f"  Starting from the saved layout {layout['name']!r} "
                f"({board.family_label}) -- change anything before saving.")
        else:
            board = await self._ask_which_board()
            start_items = []
        if board is None:
            await self._io.write("  Cancelled -- nothing saved.")
            return False
        plan = cat.BoardPlan(name, board=board)
        # THE SCREEN FIRST, the questions only if there is no screen. Both
        # paths end at the same BoardPlan and the same validation; the
        # question loop is how this works from the machine's own console.
        laid_out = await self._io.pin_map({
            "board_name": name,
            "catalogue": cat.describe(board),
            "items": start_items,
            # A new board has not been met yet: both facts arrive at first
            # flash and ride every edit after. Until then the module's
            # PSRAM pins (GPIO16/17 on the classic) count as its own,
            # because BoardPlan.add refuses them while unknown.
            "pins_owned_by_module": self._module_pins({}, board),
            "pins_reported": [],
        })
        if laid_out is not None:
            if laid_out.get("cancelled"):
                # AN UNFINISHED DESIGN IS NOT LOST: whatever was placed so
                # far goes to Saved layouts, to pick up later or on another
                # board of the same chip.
                kept = self._remember_layout(
                    f"{name} (unfinished)", board,
                    laid_out.get("draft") or [],
                    f"a design for {name} that was not finished",
                    state="unfinished")
                if kept:
                    await self._io.write(
                        "  Cancelled -- no board was saved. What you had "
                        "placed is kept in Notifications -- Continue "
                        "picks it up.")
                else:
                    await self._io.write("  Cancelled -- nothing saved.")
                return False
            for note in self._apply(plan, laid_out.get("items") or [],
                                    from_phone=True):
                await self._io.write(f"  ! {note}", level="warn")
            if not plan.items:
                await self._io.write("  Nothing was assigned -- nothing saved.")
                return False
        else:
            while await self._add_one(plan):
                if not plan.items:
                    continue
                more = await self._io.choose(
                    f"{len(plan.items)} thing(s) on {name}. Anything else?",
                    [{"value": "yes", "label": "Add another",
                      "sub": f"{len(plan.board.usable) - len(plan.taken())} "
                             f"pins still free"},
                     {"value": "no", "label": "That is everything",
                      "sub": "save the board"}])
                if not more or more[0] == "no":
                    break
            if not plan.items:
                await self._io.write("  Cancelled -- nothing saved.")
                return False
        record = self._save(plan, name, slug)
        await self.grant_access(record)
        # The design is already in Saved layouts: _save wrote the board, and
        # a board's wiring is its layout (see _sync_board_layout).
        await self._io.write(
            f"\n  Saved {name} with {len(plan.items)} device(s). Nothing has "
            f"been written to a chip yet -- plug the board in when ready.")
        return True

    def _save(self, plan: "cat.BoardPlan", name: str,
              slug: str) -> Dict[str, Any]:
        record = {
            "slug": slug,
            "name": name,
            "items": self._record_items(plan),
            "mqtt_user": f"hv-{slug}",
            # Per board, so a board that leaves the house takes only its own
            # access with it.
            "mqtt_pass": _secrets.token_urlsafe(32),
            # SEPARATE from the broker password on purpose: Tasmota's web
            # login is plain HTTP on the LAN, so sharing one secret would
            # broadcast the broker credential on every visit to its page.
            "web_pass": _secrets.token_urlsafe(18),
            "topic": f"hv-{slug}",
            "created": _now(),
            "flashed": "",
            # Which board it is (a chips id), and its family -- the family
            # outlives a board FILE that is later deleted, so the record
            # never falls back to the classic pin table by accident.
            "board": plan.board.id,
            "board_family": plan.board.family,
        }
        self._write_board(record)
        self._last_node = slug
        return record

    @staticmethod
    def _record_items(plan: "cat.BoardPlan") -> List[Dict[str, Any]]:
        """A plan's items as a board record stores them."""
        return [{"type_id": i.type_id, "name": i.name, "pins": i.pins}
                for i in plan.items]

    @staticmethod
    def _web_password(record: Dict[str, Any]) -> str:
        """Records written before web_pass existed fall back to the old
        behaviour rather than locking their owner out."""
        return record.get("web_pass") or record["mqtt_pass"]

    def _plan_for(self, slug: str,
                  items: Optional[List[Dict[str, Any]]] = None
                  ) -> Optional["cat.BoardPlan"]:
        """Build the board's plan from its recorded wiring -- or, when
        `items` is given, from that list instead. The override is how a
        flash can lay down a different plan than the record's."""
        notes: List[str] = []
        self._plan_local.notes = notes
        record = self._read_board(slug)
        if record is None:
            return None
        # The record's raw facts become one conclusion here, so the template
        # knows whether 16/17 are ordinary pins or the module's own bus.
        plan = cat.BoardPlan(record.get("name", slug),
                             owns_psram_pins=self._owns_psram_pins(record),
                             board=self._board_of(record))
        for item in (record.get("items", []) if items is None else items):
            try:
                plan.add(item["type_id"], item["name"],
                         {k: int(v) for k, v in item["pins"].items()},
                         anyway=True)
            except (ValueError, KeyError, TypeError, AttributeError) as exc:
                # A record written by an older catalogue can name a device
                # type that no longer exists -- or a pin the board has since
                # said is its own (PSRAM). Logged AND kept for the caller:
                # a device that silently vanishes from the template while
                # the record still lists it is a lie in both directions.
                label = item.get("name", "?") if isinstance(item, dict) \
                    else "?"
                note = f"{label} was dropped from the template: {exc}"
                event("ESP", f"{slug}: {note}")
                notes.append(note)
        return plan

    #: Per THREAD: drive_item_sync builds plans on the executor while a flow
    #: builds one on the loop, and a single shared list let either wipe the
    #: other's notes between _plan_for and the read that reports them.
    _plan_local = threading.local()

    @property
    def _plan_notes(self) -> List[str]:
        """What the last _plan_for on THIS thread could NOT place -- read by
        the callers that send a template, so the person hears about a
        dropped device."""
        return getattr(self._plan_local, "notes", [])

    async def _say_plan_notes(self) -> None:
        for note in self._plan_notes:
            await self._io.write(f"  ! {note}", level="warn")

    # ── broker accounts ─────────────────────────────────────────────────
    #
    # A password on a board is worth nothing until the BROKER knows it: the
    # board connects, is refused, and retries forever, which looks exactly
    # like a wrong Wi-Fi password.

    async def _reload_broker(self) -> None:
        """Make Mosquitto re-read the password file.
        It reloads on SIGHUP and does not poll, so a new account does not
        exist as far as a running broker is concerned until it is told.
        setup_manager.reload_accounts covers Windows, which has no SIGHUP.
        """
        from app import setup_manager
        lines: List[str] = []
        await asyncio.to_thread(setup_manager.reload_accounts, lines.append)
        for line in lines:
            if line.strip():
                await self._io.write(f"  {line.strip()}")

    @staticmethod
    def _passwd_entry_state(text: str, user: str) -> str:
        """'ok', 'missing' or 'malformed' for one user's line in a mosquitto
        password file's TEXT. Pure, and pure Python -- no grep, no shell --
        so Windows and the Pi read the same answer from the same bytes.
        Mosquitto 2.x writes `user:$7$iterations$salt$hash` (PBKDF2) or the
        older `user:$6$salt$hash` (SHA512), salt and hash base64. A line
        that fails this shape is one Mosquitto DROPS AT LOAD -- "Unable to
        decode password salt ... removing entry" -- taking the account with
        it silently. Found the hard way: the Windows->Pi copy mangled
        exactly one entry, and every refusal after looked like a TLS fault,
        a Wi-Fi fault, and a board fault before the broker's own foreground
        log named it.
        """
        import base64
        for line in text.splitlines():
            line = line.strip()
            if not line or ":" not in line:
                continue
            name, _, value = line.partition(":")
            if name != user:
                continue
            parts = value.split("$")
            try:
                if len(parts) == 5 and parts[1] == "7":
                    int(parts[2])
                    base64.b64decode(parts[3], validate=True)
                    base64.b64decode(parts[4], validate=True)
                elif len(parts) == 4 and parts[1] == "6":
                    base64.b64decode(parts[2], validate=True)
                    base64.b64decode(parts[3], validate=True)
                else:
                    return "malformed"
            except Exception:                      # noqa: BLE001
                return "malformed"
            return "ok"
        return "missing"

    def _account_state(self, user: str) -> str:
        """'ok' / 'missing' / 'malformed' / 'no-file', for THIS hub's
        password file. The read side of grant_access: writing an account
        proves mosquitto_passwd ran; only reading it back proves the broker
        will still be holding it after its next reload."""
        passwd = _config.MQTT_PASSWD_FILE
        if not passwd or not Path(passwd).is_file():
            return "no-file"
        try:
            text = Path(passwd).read_text("utf-8", errors="replace")
        except OSError:
            return "no-file"
        return self._passwd_entry_state(text, user)

    async def grant_access(self, record: Dict[str, Any]) -> bool:
        """Give one board an account on the broker. Idempotent -- and READ
        BACK: a write proves it happened, not that the entry it left is one
        the broker can load.

        WRITTEN IN-PROCESS by setup_manager.write_passwd_entry, not by
        `mosquitto_passwd -b`, which put every board's password on a
        command line visible in `ps`. No Mosquitto tool is needed for it."""
        from app import setup_manager
        passwd = _config.MQTT_PASSWD_FILE
        try:
            await asyncio.to_thread(
                setup_manager.write_passwd_entry,
                passwd, record["mqtt_user"], record["mqtt_pass"])
        except (OSError, ValueError) as exc:
            await self._io.write(f"  Could not create the account: {exc}",
                                 level="warn")
            return False
        state = self._account_state(record["mqtt_user"])
        if state != "ok":
            await self._io.write(
                f"  The account was written but reads back {state} -- the "
                f"broker would drop it at load. The password file at "
                f"{passwd} needs a look.", level="warn")
            return False
        event("MQTT", f"granted {record['mqtt_user']!r}")
        await self._reload_broker()
        return True

    async def revoke_access(self, slug: str) -> bool:
        """Take a board's account away. Its password stops working."""
        record = self._read_board(slug)
        passwd = _config.MQTT_PASSWD_FILE
        if record is None or not Path(passwd or "").is_file():
            return False
        # Same in-process writer as grant_access, for one file format and
        # one set of permissions rules.
        try:
            from app import setup_manager
            await asyncio.to_thread(
                setup_manager.write_passwd_entry, passwd,
                record["mqtt_user"], remove=True)
        except Exception:                          # noqa: BLE001
            return False
        event("MQTT", f"revoked {record['mqtt_user']!r}")
        await self._reload_broker()
        return True

    def credentials(self, slug: str) -> Dict[str, str]:
        """What a board signs in with. The CALLER masks these before showing
        them -- they are returned in full."""
        record = self._read_board(slug) or {}
        return {"user": record.get("mqtt_user", ""),
                "password": record.get("mqtt_pass", ""),
                # What the board's web page actually takes -- the separate
                # web_pass, or the broker password only on a record written
                # before web_pass existed (the same fallback provisioning uses).
                "web_password": (self._web_password(record)
                                 if record.get("mqtt_pass") else ""),
                "topic": record.get("topic", "")}

    async def rename_board(self, slug: str) -> Optional[str]:
        """Change what a board is called. Returns the new name, or None.
        THE NAME ONLY. Moving the topic means minting an account, granting
        it, switching a live board mid-connection and revoking the old one --
        four things that can each half-happen, to change a string nobody
        reads. The name you see is DeviceName.
        """
        record = self._read_board(slug)
        if record is None:
            await self._io.write(f"  No board called {slug!r}.", level="warn")
            return None
        old = record.get("name", slug)
        new = await self._ask_name(f"\n  Call {old!r} what instead? ")
        if new is None or new == old:
            await self._io.write("  Left as it was.")
            return None
        if any(str(other.get("name") or "").lower() == new.lower()
               for s, other in self._boards() if s != slug):
            await self._io.write(
                f"  There is already a board called {new!r}.", level="warn")
            return None
        record["name"] = new
        self._write_board(record)
        await self._io.write(f"  This hub now calls it {new!r}.")
        if await self._publish(record, [f"DeviceName {new}"]):
            await self._io.write("  The board has been told as well.")
        else:
            await self._io.write(
                f"  The board is not reachable right now, so it still calls "
                f"itself {old!r}. It will be told the next time it is "
                f"installed over the cable.", level="warn")
        return new

    # ── the broker, and how a board is told to trust it ─────────────────

    @staticmethod
    def broker_fingerprint() -> str:
        """The broker's public key, hashed EXACTLY as the board hashes it.
        Tasmota (v8.4.0+) hashes the RSA public key in a format that is
        ALMOST an OpenSSH key blob, and "almost" is the whole story:
            uint32(7) "ssh-rsa" uint32(len e) e uint32(len n) n
        with e and n as minimal big-endian bytes -- NO leading-zero mpint
        rule. Canonical SSH inserts one 0x00 before a modulus whose top bit
        is set (every RSA modulus), producing a 257-byte field where Tasmota
        hashes 256, so the SHA-1s differ.
        Three formats were shipped wrongly before this one: SHA-1 of the
        certificate DER, of the subjectPublicKeyInfo, and the canonical SSH
        blob. All three look right, none match, and the board reports every
        mismatch as `TLS connection error: 1` at the end of a complete
        handshake. Verified against Tasmota's own learn mode on a real board.
        Do not "simplify" this to a library encoder; the library will be
        correct, and wrong.
        Returns "" when there is nothing pinnable, which switches TLS off
        rather than pinning nothing and calling it encrypted.
        """
        path = _config.BROKER_TLS_CERT
        if not path or not Path(path).is_file():
            return ""
        try:
            import hashlib
            import struct
            from cryptography import x509
            key = x509.load_pem_x509_certificate(
                Path(path).read_bytes()).public_key()
            pub = key.public_numbers()
        except Exception as exc:                   # noqa: BLE001
            event("MQTT", f"broker certificate unreadable ({exc}) -- TLS off")
            return ""
        if not hasattr(pub, "e"):
            # SAID, not swallowed. Tasmota pins RSA only, so an EC broker
            # certificate silently took the whole fleet to plaintext.
            event("MQTT", "broker certificate is not RSA -- Tasmota cannot "
                          "pin it, so TLS is off")
            return ""
        def field(i: int) -> bytes:
            raw = i.to_bytes((i.bit_length() + 7) // 8, "big")
            return struct.pack(">I", len(raw)) + raw
        blob = struct.pack(">I", 7) + b"ssh-rsa" + field(pub.e) + field(pub.n)
        digest = hashlib.sha1(blob).hexdigest().upper()
        # Space-separated byte pairs, the form Tasmota prints and accepts.
        return " ".join(digest[i:i + 2] for i in range(0, len(digest), 2))

    @staticmethod
    def _broker_tls_context() -> Optional[ssl.SSLContext]:
        """An SSL context that trusts THIS hub's certificate and nothing else.
        The certificate is the identity here. Self-signed means it is its own
        authority, so loading it as the CA proves exactly one thing: this is
        our broker. That is stricter than the public trust store, not looser.
        HOSTNAME CHECKING IS OFF, and only that: the SANs come from
        NETWORK_BINDINGS and the address the hub answers on today is not
        necessarily among them. local.crt is never regenerated because the
        app pins its bytes, so the NAME is the one part allowed to go stale.
        """
        path = _config.BROKER_TLS_CERT
        if not path or not Path(path).is_file():
            return None
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        # check_hostname must go False BEFORE verify_mode is touched.
        context.check_hostname = False
        context.verify_mode = ssl.CERT_REQUIRED
        context.load_verify_locations(cafile=path)
        return context

    #: Tailscale's range. Real, routable, and completely useless to a board.
    _TAILNET = ("100.",)

    @staticmethod
    def reachable_by_a_board(address: str) -> bool:
        """Could an ESP32 on the house Wi-Fi actually reach this?
        Applied to BOTH the detected address and anything typed into config,
        which is the point -- a tailnet address is exactly the sort of thing
        that gets pasted into MQTT_HOST because WS_HOSTNAME is one.
        """
        import ipaddress
        address = (address or "").strip()
        if not address or address.startswith("127.") or address in (
                "localhost", "0.0.0.0"):
            return False
        if address.startswith(EspBuilder._TAILNET):
            return False
        try:
            parsed = ipaddress.ip_address(address)
        except ValueError:
            return True                  # a hostname; somebody meant it
        return bool(parsed.is_private)

    @classmethod
    def _detect_address(cls) -> str:
        """This machine's address on the network the boards will join."""
        # What a hub that RUNS the network answers on, tried before the
        # default route, which on such a hub points down Ethernet at the
        # internet and would send every board to the wrong interface.
        for address in hub_net.AP_ADDRESSES:
            if hub_net.holds_address(address):
                return address
        found = hub_net.route_address()
        return found if cls.reachable_by_a_board(found) else ""

    def hub_address(self) -> str:
        """Where a board should look for this hub, ON THE BOARDS' NETWORK.
        That last part is the whole problem. The default route answers "which
        address reaches the internet", which is right only when the hub is a
        CLIENT of the network the boards join. On a Pi running the hotspot it
        is the opposite. So it is stored with the SSID, asked once, and
        re-asked whenever the Wi-Fi changes.
        """
        saved = (self._read_wifi().get("hub_address") or "").strip()
        if saved and self.reachable_by_a_board(saved):
            return saved
        configured = _config.MQTT_HOST.strip()
        if configured and self.reachable_by_a_board(configured):
            return configured
        # _detect_address returns only a reachable address or "" (its AP
        # addresses are all private). 127.0.0.1 is returned rather than
        # raised: provisioning still hands the board its wiring and its
        # Wi-Fi, and _flash_node says it will have nowhere to report to.
        return self._detect_address() or "127.0.0.1"

    def _mqtt_settings(self) -> Dict[str, str]:
        """Broker address, and whether to wrap the link in TLS.
        TLS is on when there is a certificate to pin. No fingerprint means
        TLS off and the plain port -- a board configured for TLS with nothing
        to verify against either refuses to connect or verifies nothing, and
        both are worse than a plaintext link you have been told about.
        """
        # TLS FOLLOWS CONFIG, never the presence of a certificate. It used
        # to drop to "0" when there was nothing to pin, while the port stayed
        # at config.MQTT_PORT (8883) -- plaintext at a TLS listener, which
        # never connects. A missing certificate is now a REFUSAL, made where
        # the settings are used (_provisioning_commands, _mqtt_client).
        tls = bool(_config.MQTT_TLS)
        return {
            "host": self.hub_address(),
            "port": str(_config.MQTT_PORT),
            "tls": "1" if tls else "0",
        }

    def _tls_refusal(self) -> str:
        """Why a board cannot be provisioned right now, or "" when it can:
        TLS is on and there is nothing to pin. Said instead of silently
        sending the Wi-Fi password over a link that will not even open."""
        if not _config.MQTT_TLS:
            return ""
        if self.broker_fingerprint():
            return ""
        return ("TLS is configured on, but there is no usable RSA "
                f"certificate at {_config.BROKER_TLS_CERT} "
                "to pin, so the board cannot be told how to reach the "
                "broker. Run `python setup_manager.py --broker` first, or "
                "set HOMEVOICE_MQTT_TLS=0 on purpose.")

    async def _say_if_plaintext(self) -> bool:
        """False, with the reason said, when provisioning must not go ahead
        (TLS on, nothing to pin). True when it may."""
        why = self._tls_refusal()
        if not why:
            return True
        await self._io.write(f"  ! Not sent. {why}", level="warn")
        return False

    def _wifi_record_stale_here(self) -> str:
        """A sentence when the Wi-Fi record describes a network this machine
        does not run; "" when all is well or unknowable."""
        wifi = self._read_wifi()
        hub = (wifi.get("hub_address") or "").strip()
        if not hub or hub_net.holds_address(hub):
            return ""
        return (f"The Wi-Fi record says boards join "
                f"{wifi.get('ssid', '?')!r} and find this hub at {hub} -- but "
                f"this machine does not hold that address, so that record is "
                f"from another machine or a network that no longer exists.")

    #: An AP address a machine runs ITSELF, and the words that raise it
    #: again. Not guesses: 192.168.137.1 is what Windows Internet Connection
    #: Sharing has given its own hotspot since it existed, 192.168.4.1 is
    #: this project's Pi convention (NETWORK_BINDINGS, the cert SANs, the
    #: app's candidates), 10.42.0.1 is NetworkManager's default for a
    #: shared connection.
    _OWN_AP_HINTS: Dict[str, Tuple[str, str]] = {
        "192.168.137.1": (
            "Windows",
            "Settings -> Network & internet -> Mobile hotspot, and switch it "
            "on. If it keeps switching ITSELF off, turn off 'When no devices "
            "are connected, automatically turn off mobile hotspot' on that "
            "same page -- a board on a USB cable is not a connected device, "
            "so nothing during a flash keeps the hotspot alive."),
        "192.168.4.1": ("Linux", "sudo nmcli con up homevoice-ap"),
        "10.42.0.1": ("Linux", "sudo nmcli con up <the shared connection>"),
    }

    @classmethod
    def _own_ap_down(cls, hub: str, ssid: str) -> str:
        """How to raise THIS machine's own access point -- "" when the record
        belongs to some other machine.

        THE SAME REFUSAL, TWO OPPOSITE CAUSES, and until now one piece of
        advice for both. A record naming a network this machine does not
        run means either (a) it was written on a different machine, where
        Change ESP Wi-Fi is exactly right, or (b) it is this machine's own
        hotspot, currently switched off -- where Change ESP Wi-Fi is wrong
        and cruel: it asks a person to re-type a name, a password and an
        address that were all already correct, and the address it detects
        while the AP is down would be the wrong one. Windows turns its
        hotspot off by itself when nothing is connected, and a board on a
        cable is not a connected device -- so (b) happens between one
        install and the next, with nothing changed by anybody (Augusta,
        2026-08-24, twice in one afternoon).
        """
        import platform
        import socket
        here = "Windows" if platform.system() == "Windows" else "Linux"
        entry = cls._OWN_AP_HINTS.get((hub or "").strip())
        if entry and entry[0] == here:
            return entry[1]
        # The other tell, and a stronger one where it applies: Windows names
        # its hotspot after the machine ("DESKTOP-2JKH971 1094"), so an SSID
        # that opens with this machine's own name is this machine's own
        # hotspot, whatever address it was given.
        if here != "Windows":
            return ""
        try:
            host = socket.gethostname().split(".")[0].strip().lower()
        except Exception:                          # noqa: BLE001
            return ""
        if host and (ssid or "").strip().lower().startswith(host):
            return cls._OWN_AP_HINTS["192.168.137.1"][1]
        return ""

    #: What _wifi_ready_here decided.
    WIFI_READY = "ready"           # the address checks out; the full install
    WIFI_TRUSTED = "trusted"       # details right, network merely not up yet
    WIFI_LATER = "later"           # firmware and wiring only; network pending
    WIFI_STOP = "stop"

    async def _wifi_ready_here(self) -> str:
        """Can this board be told where to find the hub -- and if not, what
        can still be done with the cable that is already plugged in?

        THE FIRMWARE NEEDS NO NETWORK. Erasing a chip and writing an image
        is a conversation over a wire; only the SETTINGS need to know which
        network exists and where the hub answers on it. Those two were one
        step, so a switched-off hotspot cost the whole install (Augusta,
        2026-08-24: "it still flashes the firmware and just says the wifi
        details had to be added"). They are separate now, and which
        separation is offered depends on WHY the check failed:

          - this machine's own AP, currently down: the record is RIGHT.
            Everything can be written, including the network, and the board
            will join the moment the hotspot is back -- Tasmota retries for
            ever. Nothing is pending but the toggle.
          - a record from somewhere else: the details are WRONG for here,
            so writing them would strand the board. Firmware and wiring go
            on; the network is left for when the record is right.
        """
        while True:
            stale = self._wifi_record_stale_here()
            if not stale:
                return self.WIFI_READY
            if await self._wifi_follow_this_hub():
                continue
            wifi = self._read_wifi()
            hub = (wifi.get("hub_address") or "").strip()
            ssid = (wifi.get("ssid") or "").strip()
            how = self._own_ap_down(hub, ssid)
            if how:
                await self._io.write(
                    f"  Held up -- and NOTHING is wrong with the record. "
                    f"{ssid!r} is this machine's own hotspot and it is not "
                    f"running right now, so this hub does not hold {hub}. "
                    f"There is nothing to re-enter; the network just has to "
                    f"come back up.", level="warn")
                await self._io.write(f"  {how}")
                picked = await self._io.choose(
                    "What now?",
                    [{"value": "again", "label": "I turned it on -- check again",
                      "sub": f"re-asks whether this machine holds {hub} -- "
                             f"no menus, no re-typing"},
                     {"value": "anyway", "label": "Set it up anyway",
                      "sub": f"the details are right; the board will join "
                             f"{ssid!r} by itself when the hotspot is back"},
                     {"value": "wifi", "label": "It is a different network now",
                      "sub": "changes the name, password or address for "
                             "every board"},
                     {"value": "stop", "label": "Stop here", "sub": ""}])
            else:
                await self._io.write(f"  Held up. {stale}", level="warn")
                picked = await self._io.choose(
                    "What now?",
                    [{"value": "wifi", "label": "Set the network up now",
                      "sub": "asks for the network and this hub's address "
                             "on it, then carries on"},
                     {"value": "later", "label": "Firmware only, network later",
                      "sub": "writes the firmware and the wiring over this "
                             "cable; the network is added when the record "
                             "is right"},
                     {"value": "stop", "label": "Stop here", "sub": ""}])
            if not picked or picked[0] == "stop":
                await self._io.write("  Nothing was changed.")
                return self.WIFI_STOP
            if picked[0] == "anyway":
                return self.WIFI_TRUSTED
            if picked[0] == "later":
                return self.WIFI_LATER
            if picked[0] == "wifi":
                await self.change_wifi()
            # Either way, round again -- the probe is the only authority.

    async def _wifi_follow_this_hub(self) -> bool:
        """BOARDS JOIN THE WI-FI THIS HUB IS ON -- worked out, not asked.
        When the record's hub address is not this machine's any more and
        this machine is on a Wi-Fi network now: the same network means only
        the address changed (saved, nothing asked); another network needs
        its password, read from this machine's saved connection when
        NetworkManager will say, else asked once. False = not done here (no
        Wi-Fi, this machine's own hotspot is just off, a ';' in the name);
        the caller's menu takes over."""
        wifi = self._read_wifi()
        old_hub = (wifi.get("hub_address") or "").strip()
        old_ssid = (wifi.get("ssid") or "").strip()
        if self._own_ap_down(old_hub, old_ssid):
            return False
        here = self._detect_address()
        profile = await asyncio.to_thread(hub_net.active_wifi)
        ssid = await asyncio.to_thread(hub_net.profile_ssid, profile) \
            if profile else ""
        if (not ssid or ";" in ssid or not here
                or here in hub_net.AP_ADDRESSES):
            return False
        password = wifi.get("password", "")
        if ssid != old_ssid:
            password = await asyncio.to_thread(hub_net.saved_password, profile)
            if not password:
                password = await self._io.ainput(
                    f"  Boards join the Wi-Fi this hub is on: {ssid!r}. "
                    f"Its password (asked once): ", patient=True, secret=True)
            if ";" in password:
                await self._io.write(
                    "  A ';' cannot travel in the Wi-Fi password (boards read "
                    "it as 'next command').", level="warn")
                return False
        self._write_json(self._wifi_path(), {
            **wifi, "ssid": ssid, "password": password, "hub_address": here})
        await self._io.write(
            f"  Boards join {ssid!r} and find this hub at {here}"
            + (" (its new address there)." if ssid == old_ssid else "."))
        await self._broker_cert_must_name(here)
        return True

    def _provisioning_commands(self, record: Dict[str, Any], *,
                               identity: bool = True) -> List[str]:
        """What a board is told, over the cable or over the air.
        ONE source for both. change_wifi used to hand-build its own copy of
        this and the two had already drifted -- different reset token, one
        purging Password2 and the other not.
        TWO backlogs, not one: a single line carrying two 40-odd character
        passwords plus a 59-character fingerprint runs at the serial input
        buffer's limit, and a truncated tail fails silently. Network first so
        the read-back can check one field from each half.
        `identity=False` sends only what a Wi-Fi move changes.
        """
        wifi = self._read_wifi()
        mqtt = self._mqtt_settings()
        tls_command = _config.MQTT_TLS_COMMAND
        mode = _config.MQTT_FINGERPRINT_MODE
        token = _config.WIFI_RESET_TOKEN
        fingerprint = self.broker_fingerprint() if mqtt["tls"] == "1" else ""
        if mqtt["tls"] == "1" and not fingerprint:
            # REFUSED, not downgraded: a board told "TLS off" while the
            # broker only answers TLS on 8883 never connects, and a board
            # quietly provisioned in plaintext is worse than one not
            # provisioned. The certificate has to exist first.
            raise RuntimeError(self._tls_refusal())
        # Password3 and MqttPassword2 are Tasmota's MASKED forms of Password1
        # and MqttPassword: same setting, reply printed as "****" instead of
        # the secret itself (CmndPassword: index > 2 -> show_asterisk).
        # An OPEN network's empty password is sent as the clear token: a bare
        # `Password3` with no value is a QUERY in Tasmota, so the chip kept
        # whatever password it already held and never joined.
        network = [f"SSId1 {wifi.get('ssid', '')}",
                   f"Password3 {wifi.get('password') or token}"]
        if identity:
            # NOT FriendlyName1 -- that is per DEVICE and travels with the
            # wiring. Setting it here is how four relays were all called
            # "Kitchen".
            network.append(f"DeviceName {record['name']}")
            network.append(f"WebPassword {self._web_password(record)}")
        broker = [f"MqttHost {mqtt['host']}", f"MqttPort {mqtt['port']}"]
        if identity:
            broker += [f"MqttUser {record['mqtt_user']}",
                       f"MqttPassword2 {record['mqtt_pass']}",
                       f"Topic {record['topic']}"]
        broker.append(f"{tls_command} {mqtt['tls']}")
        # SetOption19 1 is the legacy HA discovery format Tasmota removed and
        # HA no longer listens for. A second-hand board can arrive set to it
        # and then never appear, with nothing anywhere saying why.
        broker.append("SetOption19 0")
        # KEEP FIRMWARE OFF THE PUBLIC INTERNET. A board's stock OtaUrl points
        # at ota.tasmota.com; on an offline house network a triggered upgrade
        # cannot reach it, drops into the safeboot recovery partition to
        # download, fails, and strands the board there -- how Kitchen bricked
        # (2026-09-09). This hub flashes over the cable, so point OtaUrl at the
        # hub's own address instead: an accidental upgrade stays on the LAN and
        # never phones out. Re-sent on a Wi-Fi move too, since the hub's
        # address moves with the network. Empty -> left untouched.
        # THIS board's family's image: an S3 healing from the classic image
        # would take the bytes and never boot.
        ota = self._ota_url(self._board_of(record))
        if ota:
            broker.append(f"OtaUrl {ota}")
        if fingerprint:
            # ORDER MATTERS: the validation mode, then the fingerprint it
            # validates against.
            broker.append(f"{mode} 1")
            broker.append(f"{_config.MQTT_FINGERPRINT_COMMAND} {fingerprint}")
        else:
            # UNDO. A board provisioned once with TLS keeps demanding a match
            # for ever, so turning TLS off to remove a variable adds one.
            broker.append(f"{mode} 0")
        # SLEEP 0 -- THE ONE THAT BROKE EVERY SENSOR ON KITCHEN (2026-09-12).
        #
        # Tasmota's default is `SleepMode Dynamic` with `Sleep 50`: the CPU
        # idles up to 50 ms between loop passes to save power. That is right
        # for a battery sensor and wrong for every board this hub
        # provisions, because it starves exactly the drivers that need the
        # processor:
        #
        #   PZEM        its serial is bit-banged. A frame that arrives while
        #               the CPU is asleep is corrupt, so the meter reports
        #               a complete ENERGY block of ZEROS -- voltage, current,
        #               power, total. Not missing. Zero.
        #   IR receive  the pulse train is measured in microseconds; a sleep
        #               of milliseconds destroys it. Tasmota's own docs say
        #               Sleep 0 for IR.
        #   RC522       the driver POLLS for a card once per loop. Sleeping
        #               through most of them means most taps are never seen.
        #   displays    I2C writes are issued from the loop too.
        #
        # And what keeps working is the tell: relays are direct GPIO writes
        # and a PIR is an interrupt, so on Kitchen the relays and the motion
        # sensor were fine while the meter, the panel, the card reader and
        # the IR receiver were all "broken". Four faults, one cause, and it
        # was sitting in the board's own STATE the whole time:
        #
        #   {"SleepMode":"Dynamic","Sleep":50,"LoadAvg":46, ...}
        #
        # Only for boards that carry a part that cares. A board of nothing
        # but relays keeps the default, because nothing there is timing
        # critical and this hub should change as little as it can get away
        # with.
        # EVERY BOARD, not a list of types. Tasmota's dynamic sleep exists
        # to save battery, and this hub provisions no battery devices --
        # every board it sets up is mains powered. On mains the setting
        # costs perhaps twenty milliamps and nothing else, while getting
        # the list wrong costs a sensor that reports zeros for three days.
        # Unconditional is both simpler and safer than a list somebody has
        # to keep complete.
        # A METER THAT REPORTS WHEN IT CHANGES, not when a timer says so
        # (2026-09-12: "why do readings like pzem lag / wont it be better
        # as a multimeter measuring incredibly fast and updating").
        #
        # It already measures fast. The PZEM refreshes its own registers
        # about once a second and Tasmota reads them continuously -- but it
        # only PUBLISHES on TelePeriod, which this hub never set, so it is
        # Tasmota's default of 300 SECONDS. Every number the app showed was
        # up to five minutes old, and the meter was not the slow part.
        #
        # The answer is not a faster poll. `PowerDelta` publishes the whole
        # ENERGY block the moment power moves by more than N percent, so a
        # kettle appears the second it is switched on and a quiet house
        # sends nothing at all. Change-driven beats timer-driven in both
        # directions: quicker when something happens, silent when nothing
        # does -- the same shape as the home-tile push in ws_server.
        #
        # TelePeriod comes down to a minute alongside it, as the FLOOR for
        # the values PowerDelta does not watch (voltage, power factor,
        # the running total). Only on boards that carry a meter: a board of
        # relays has nothing to report more often.
        # Both are exactly what maintenance_commands re-asserts later.
        # NOT PowerDelta: Tasmota only knows it once an energy driver is
        # running, and the meter's pins arrive with the template AFTER
        # this -- sent here it came back "Unknown" and stopped the install
        # (2026-11-02). It is re-asserted when the board comes back.
        broker += [c for c in self.maintenance_commands(record)
                   if not c.startswith("PowerDelta")]
        # An old network the board will still join is a way back in.
        broker += [f"SSId2 {token}", f"Password2 {token}", "Restart 1"]
        return ["Backlog " + "; ".join(network),
                "Backlog " + "; ".join(broker)]

    def _provisioning_line(self, record: Dict[str, Any]) -> List[str]:
        """_provisioning_commands for the SERIAL console: both Backlogs in
        ONE write. SSId1/Password3 (and MqttHost, Topic...) restart the board
        a second or two after the queue empties, and _talk_to_board waits
        1.2 s of quiet after each command -- so a separately typed second
        line could land on a restarting board. Typed together, the second is
        queued before the first has finished."""
        return ["\r\n".join(self._provisioning_commands(record))]

    #: Parts whose driver is starved by Tasmota's dynamic sleep. Bit-banged
    #: serial, microsecond pulse timing, and per-loop polling -- see
    #: _provisioning_commands.
    #: Components that are PLAIN GPIO: the chip writes a level or takes an
    #: interrupt, and the processor being asleep between loop passes costs
    #: nothing. Everything else speaks a protocol -- bit-banged serial,
    #: one-wire, a bus, a pulse measured in microseconds, or a driver that
    #: polls once per loop -- and every one of those is starved by
    #: Tasmota's dynamic sleep.
    #:
    #: A SHORT CLOSED LIST, AND THE RIGHT WAY ROUND. The first version of
    #: this named the timing-critical parts instead, and was wrong within
    #: the hour (2026-09-12, her question: "only for these devices??"): it
    #: listed the four that had bitten and omitted dht11, dht22, ds18b20,
    #: hcsr04 and rf_send -- and deriving it properly then turned up
    #: ws2812, the rotary encoder and the 74x595 shift register as well,
    #: none of which I would have thought to type.
    #:
    #: Naming the SIMPLE ones is safe because that set is closed and the
    #: hub owns it; anything not named is treated as timing-critical, the
    #: safe way round.
    _SIMPLE_COMPONENTS = ("Relay", "Switch", "Button", "Buzzer", "PWM",
                          "ADC")

    @classmethod
    def _sleep_critical_types(cls) -> Tuple[str, ...]:
        """Every catalogue part whose driver
        needs the processor awake. Derived, never typed."""
        out = []
        for dev in cat.CATALOGUE:
            for slot in dev.slots:
                head = str(slot.component.name).split()[0]
                if not head.startswith(cls._SIMPLE_COMPONENTS):
                    out.append(dev.id)
                    break
        return tuple(sorted(out))

    #: Energy meters: the boards that get PowerDelta, and the set
    #: nlu/automations.py indexes as ("energy", 0).
    _ENERGY_TYPES = frozenset({"pzem", "pzem_v1", "pzem_dc"})

    @classmethod
    def _sleep_critical_parts(cls, record: Dict[str, Any]) -> List[str]:
        """The parts ON THIS BOARD for which Sleep 0 is load-bearing.
        For the EXPLANATION only -- every board gets the setting."""
        critical = set(cls._sleep_critical_types())
        return sorted({str(i.get("type_id"))
                       for i in (record.get("items") or [])
                       if isinstance(i, dict)
                       and i.get("type_id") in critical})

    @classmethod
    def maintenance_commands(cls, record: Dict[str, Any]) -> List[str]:
        """The settings a board must hold to do its job, re-assertable at
        any time. Empty for a board that needs none.

        WHY THIS IS SEPARATE FROM _provisioning_commands. That one runs
        once, over the cable, and carries secrets -- Wi-Fi, the broker
        account, the TLS fingerprint. These are the handful that are pure
        BEHAVIOUR: no credential, nothing destructive, and nothing that
        re-initialises a driver. So they can be sent again whenever a board
        comes back, and a board that lost its settings heals itself.

        Kitchen lost its DISPLAY settings healing out of safeboot on
        2026-09-10 with its Wi-Fi and broker account intact, which is what
        proved settings can go missing one group at a time.

        WHAT IS DELIBERATELY NOT HERE: `DisplayModel` and the display
        geometry. DisplayModel RE-INITIALISES the driver -- it is what MAKES
        the gibberish -- and re-sending it on every return is a
        fifteen-second reboot loop ("it keeps going on and off",
        2026-09-04). The panel has its own gentler paths: the soft wake,
        and _note_display_model's one-shot re-send when the board itself
        answers DisplayModel 0.

        Every command here is idempotent. Sending a value a board already
        holds changes nothing and writes no flash, which is what makes
        re-asserting them safe rather than merely tolerable.
        """
        out: List[str] = ["Sleep 0"]
        if any(isinstance(i, dict) and i.get("type_id") in cls._ENERGY_TYPES
               for i in (record.get("items") or [])):
            out += [f"PowerDelta {_config.POWER_DELTA_PERCENT}",
                    f"TelePeriod {_config.METER_TELE_PERIOD}"]
        return out

    #: The heads of `configuration_commands` that are safe to send again.
    #:
    #: `SetOption114` detaches a Switch from relay 1 -- without it motion
    #: physically flips whatever lamp shares the board AND the switch is
    #: never published, because Tasmota only announces a switch as its own
    #: thing once it is detached. `SwitchMode<n>` is which way the pin
    #: moves when the thing happens.
    #:
    #: NOTHING ELSE FROM THAT LIST. The template and `Module` restart the
    #: board; `DisplayModel` re-initialises the panel driver and is the
    #: fifteen-second reboot loop; `ShutterRelay` and `Interlock` rewrite
    #: relay groupings and could move a blind. These only tell the
    #: board how to READ a pin it already has -- `SetOption73` too, which
    #: does for a Button what SetOption114 does for a Switch (and, like
    #: it, is a flag that needs no restart).
    _REASSERTABLE_HEADS = ("SetOption114", "SwitchMode", "SetOption73")

    @classmethod
    def switch_commands(cls, plan) -> List[str]:
        """The switch settings a board must hold, re-assertable at any time.

        WHY THEY NEED RE-ASSERTING AT ALL (2026-09-13). They are sent once,
        at install, inside `configuration_commands` -- and Kitchen came out
        of safeboot on 2026-09-10 with its Wi-Fi and broker account intact
        and its DISPLAY settings gone. Settings go missing one group at a
        time, and there is no reason this group is safer than that one.

        What it costs when they go is invisible: a PIR whose SwitchMode is
        wrong reads MOTION in an empty room and nothing when you walk past;
        a PIR whose SetOption114 is gone publishes nothing at all, so no
        rule it was written for can ever fire. Neither errors. Both look
        like a broken sensor.
        """
        try:
            commands = list(plan.configuration_commands())
        except Exception:                              # noqa: BLE001
            return []
        return [c for c in commands
                if str(c).split(" ", 1)[0].startswith(cls._REASSERTABLE_HEADS)]

    #: Fields and blocks that must NEVER get a publish-on-read rule.
    #:
    #: The meter's block is handled by `PowerDelta`, which publishes the
    #: WHOLE ENERGY object the moment power moves -- so every field in it,
    #: voltage and frequency and power factor included, is already both
    #: fast and complete. A rule on top would be a second road to the same
    #: place, chattering once a second for nothing. The running totals are
    #: cumulative besides: a total that has not moved is not news.
    #:
    #: A card reader and an IR receiver report MOMENTS, and a moment is
    #: published the instant it happens already. There is nothing to speed
    #: up and nothing to hold back.
    _NO_RULE_FIELDS = frozenset({
        "total", "energy_total", "today", "yesterday", "period",
        "totalstarttime",
    })
    _NO_RULE_BLOCKS = frozenset({"ENERGY", "IRRECEIVED", "RC522", "PN532"})

    @classmethod
    def reading_rules(cls, sources) -> List[str]:
        """Tasmota rules that make a board's sensors report on every read.

        `sources` is an iterable of "BLOCK#Field" spellings, exactly as the
        board spelled them -- the values of AutomationEngine._field_src
        for that board's devices. Returns the commands to send, in order,
        or [] when nothing is worth a rule.

        SPELLINGS, NOT READING NAMES. The first draft took the {name:
        spelling} dict whole and iterated its KEYS, which merges the moment
        a board carries two sensors that measure the same thing: a BME280
        and a DS18B20 both report "temperature", one overwrote the other,
        and only one of the two ever got a rule. The spelling is what is
        unique -- BME280#Temperature and DS18B20#Temperature are two
        different things to trigger on.

        WHY A RULE AT ALL. Tasmota publishes a sensor only on TelePeriod,
        whose minimum is 10 seconds and whose default is 300 -- so a
        temperature probe was up to five minutes stale. `PowerDelta` fixes
        exactly one sensor, the energy meter, and when somebody asked for
        the same command for temperature and humidity (Tasmota issue
        #6404) it was closed "wont fix", with rules named as the answer. A
        rule trigger fires on every sensor READ, about once a second, and
        TelePeriod never enters into it.

        WHY NOT TASMOTA'S DOCUMENTED DELTA RULE. Theirs holds an upper and
        a lower bound in two Vars and publishes when the reading crosses
        either -- a deadband, on the board. It works, and it costs two
        triggers and TWO VARS per reading out of the sixteen that exist.
        Five readings and half a board's variables are gone, for a
        threshold nobody can retune without writing to every board again.

        So the board is asked only to SPEAK, and the deadband lives in the
        hub (AutomationEngine._DEADBAND), where it costs no Var, half the
        rule text, and can be changed in one file. What that buys on the
        wire is one small message per reading per second, on a broker
        already running on the same machine.

        THE SPELLING IS THE BOARD'S OWN, never a guess. `BME280#Temperature`
        but `SCD40#CarbonDioxide`: capitalising the hub's lowercase name
        would be right for one and wrong for the other. That is also what
        lets this work for a sensor this catalogue has never heard of,
        including somebody's own .ino.

        A RULE SET IS A FIXED BUFFER, AND IT WAS NEVER MEASURED
        (2026-09-14). Every `+ON ... ENDON` here appends to the SAME stored
        `Rule{n}` string, this loop never rolled over into rule set 2 or 3,
        and nothing counted the characters. A board carrying the
        catalogue's own legal maximum of eight DHT11s -- `C_DHT11` declares
        `slots=8`, and BoardPlan enforces that cap without ever forbidding
        it -- produces sixteen fields and **1597 characters**, measured.
        Tasmota's per-set buffer is 511 bytes by default.

        Nothing errors. Tasmota refuses the append once the buffer would
        overflow, so the sensors at the tail of the alphabet never get
        their report-on-every-read rule and quietly stay on TelePeriod --
        and `_maybe_send_reading_rules` records the board as done with no
        check on the reply, so it is never retried. The symptom is a
        sensor that is wired, powered, registered and simply SLOW, with no
        error anywhere. That is the exact failure this whole mechanism was
        built to remove, reintroduced one layer down.

        So the sets are filled in order -- 1, then 2, then 3 -- and each
        one closes when the next trigger would not fit. Three sets hold roughly 1500 characters in
        total: readings past that are dropped DELIBERATELY and named in
        the return value's place by `rules_dropped`, because a caller that
        cannot say which sensor went without is back where it started.
        """
        budget = cls.RULE_SET_BYTES
        # The trigger text, and the characters used, per rule set.
        sets: List[List[str]] = [[] for _ in range(cls.RULE_SET_LAST)]
        used: List[int] = [0] * cls.RULE_SET_LAST
        current = 0                     # index of the set being filled
        dropped: List[str] = []
        for src in sorted({str(s or "") for s in (sources or ())}):
            if "#" not in src:
                continue
            block, field = src.split("#", 1)
            if not block.strip() or not field.strip():
                continue
            if block.upper() in cls._NO_RULE_BLOCKS:
                continue
            if field.strip().lower() in cls._NO_RULE_FIELDS:
                continue
            trigger = (f'ON {block}#{field} DO Publish '
                       f'tele/%topic%/SENSOR '
                       f'{{"{block}":{{"{field}":%value%}}}} ENDON')
            # Roll over into the next set while this one is full.
            while (used[current] + len(trigger) > budget
                   and current < cls.RULE_SET_LAST - 1):
                current += 1
            if used[current] + len(trigger) > budget:
                dropped.append(src)
                continue
            sets[current].append(trigger)
            used[current] += len(trigger)

        out: List[str] = []
        for idx, triggers in enumerate(sets):
            if not triggers:
                continue
            n = 1 + idx
            for i, trigger in enumerate(triggers):
                # The FIRST trigger opens the set, the rest append to it. A
                # bare `RuleN <text>` REPLACES whatever the set held, so
                # only the first may be bare -- which is also why this list
                # has to be sent in order and whole.
                lead = f"Rule{n}" + ("" if i == 0 else " +")
                out.append(f"{lead} {trigger}")
            out.append(f"Rule{n} 1")    # a rule set does nothing until enabled
        cls._last_dropped = tuple(dropped)
        return out

    #: How much trigger text one Tasmota rule set holds. 511 is the real
    #: figure for a default build (`MAX_RULE_SIZE`); a little is left back
    #: for the `RuleN ` / `RuleN +` lead each command carries, which rides
    #: outside the stored text but inside the command buffer.
    RULE_SET_BYTES = 480

    #: Tasmota has exactly three. There is no fourth to roll into.
    RULE_SET_LAST = 3

    #: Spellings the last `reading_rules` call could not fit. A tuple, and
    #: read straight after the call that produced it -- this is a detail
    #: for the sender to report, not state anything depends on.
    _last_dropped: tuple = ()

    @classmethod
    def rules_dropped(cls) -> tuple:
        """Which readings the last `reading_rules` call had no room for.

        Empty is the normal answer. Non-empty means a board has more
        sensors than three Tasmota rule sets can carry, and the named ones
        will report on TelePeriod rather than on every read -- slower, not
        broken. Worth a line to the person, because "wired, registered and
        somehow slow" is otherwise indistinguishable from a fault.
        """
        return cls._last_dropped

    async def heal_safeboot_serial(self, port: str,
                                   board: Any = None) -> bool:
        """The safeboot heal (`OtaUrl` then `Upgrade 1`, two commands and not
        a Backlog -- safeboot knows no Backlog), typed into the console of a
        board that is
        on the cable -- the install flow found it in safeboot (it refused
        Backlog). The board still fetches over Wi-Fi, from this hub; the
        cable only carries the order. True when the console accepted both
        commands (no "Unknown"/"Error" answer). Never raises. `board`: the
        board on the cable (a profile or its record) -- its family decides
        which image it is sent to."""
        url = self._ota_heal_url(board)
        if not url:
            return False
        try:
            spoken = await self._talk_to_board(port, [f"OtaUrl {url}", "Upgrade 1"])
        except Exception as exc:                   # noqa: BLE001
            event("OTA", f"could not send the firmware order over {port}: "
                         f"{type(exc).__name__}: {exc}")
            return False
        for line in spoken:
            event("SERIAL", self._redact(line)[:160])
        if not spoken or any(self._CONSOLE_BAD.search(l) for l in spoken):
            return False
        event("OTA", f"board on {port} told to fetch {url} and upgrade")
        return True

    def _profile(self, board: Any) -> "chips.Board":
        """A chips.Board out of what a caller has in hand: the profile
        itself, a board record, a board's MQTT topic, or nothing (the
        classic board, as before families)."""
        if isinstance(board, chips.Board):
            return board
        if isinstance(board, dict):
            return self._board_of(board)
        if isinstance(board, str) and board:
            try:
                for _slug, record in self._boards():
                    if record.get("topic") == board:
                        return self._board_of(record)
            except Exception:                      # noqa: BLE001
                pass
        return chips.DEFAULT

    def _ota_url(self, board: Any = None) -> str:
        """Where a board is told to look for firmware if an upgrade is
        triggered -- by default the hub's OWN address, never the public
        internet. See config.OTA_URL for the full reasoning; in short, a
        stock board points at ota.tasmota.com, and on an offline network a
        triggered upgrade that cannot download strands the board in safeboot
        (Kitchen, 2026-09-09). The hub updates firmware over the cable, so an
        internet OTA is never needed.

            config.OTA_URL is a string -> sent verbatim (incl. "" = leave the
                                          board's OtaUrl untouched).
            config.OTA_URL is None     -> build a LOCAL url from the hub's
                                          address on the board's network.

        Returns "" when there is no local address to point at (an
        unresolved hub address), rather than inventing one -- better to
        leave the board's URL alone than to aim it at nowhere.

        PER FAMILY: `board` (see _profile) picks the path, one image per
        family; the classic's stays /firmware/tasmota32.bin. A configured
        OTA_URL is one address for every board, as it always was.
        """
        configured = _config.OTA_URL
        if configured is not None:
            return str(configured)
        host = self.hub_address()
        if not host or host == "127.0.0.1":
            return ""
        # The app image, tasmota32.bin -- not the .factory image the cable
        # path writes -- served by hub/ota_server.py on config.OTA_HTTP_PORT
        # (2026-09-10). Until then nothing served it, so a board in safeboot
        # had nowhere to heal from and every failed update was a cable.
        port = _config.OTA_HTTP_PORT
        if port <= 0:
            return ""
        from hub.ota_server import url_for
        return url_for(host, port, self._profile(board).ota_name)

    def _ota_heal_url(self, board: Any = None) -> str:
        """_ota_url for a board that must fetch NOW (safeboot heal) -- or ""
        when it names this hub's own server and there is no image for it to
        serve. _ota_url names that server either way (right for a board's
        stored OtaUrl), so a heal used to send a board to a 404 and leave it
        in safeboot; "" makes the callers say "flash it over the cable".
        `board` as for _ota_url -- SafebootHealer passes the board's topic,
        so each board is checked against (and sent to) its family's image."""
        profile = self._profile(board)
        url = self._ota_url(profile)
        if url and _config.OTA_URL is None:
            # One image object per path, so its own change-stamp cache is
            # used rather than re-reading (or re-cutting) megabytes per call.
            path = _config.firmware_path_for(profile)
            if path not in _HEAL_IMAGES:
                from hub.ota_server import OtaImage
                _HEAL_IMAGES[path] = OtaImage(path, say=lambda _m: None,
                                              name=profile.ota_name)
            if not _HEAL_IMAGES[path].data():
                return ""
        return url

    # ── talking to a board over the broker ──────────────────────────────

    @staticmethod
    def hub_login() -> Tuple[str, str]:
        """The hub's OWN broker account (setup_manager.ensure_hub_account),
        created on first use -- or HOMEVOICE_HUB_MQTT_USER/PASS when both
        are set (an operator override). Every hub-side client signs in with
        it: the broker's topic ACL lets a board see only its own topics, so
        a borrowed board account would be deaf to every other board."""
        user = os.environ.get("HOMEVOICE_HUB_MQTT_USER", "")
        password = os.environ.get("HOMEVOICE_HUB_MQTT_PASS", "")
        if user and password:
            return user, password
        from app import setup_manager
        return setup_manager.ensure_hub_account()

    def _mqtt_client(self):
        """One constructor (core.hv_common.new_paho_client), signed in as
        THE HUB (hub_login), never as a board -- the board records'
        credentials belong to the boards."""
        client = new_paho_client()
        client.username_pw_set(*self.hub_login())
        if self._mqtt_settings()["tls"] == "1":
            context = self._broker_tls_context()
            if context is None:
                raise RuntimeError(
                    "TLS is on but there is no certificate to verify the "
                    "broker against")
            # A context, not paho's dict form: the dict is splatted into
            # tls_set() and POPS its own 'insecure' key on the way through,
            # so it cannot safely be reused.
            client.tls_set_context(context)
        return client

    # ── direct drive (local-first control, 2026-08-26) ──────────────────
    #
    # The hub can command its own boards without Home Assistant in the
    # loop: a registered device with no entity attached (HA slow, HA down,
    # HA never showing it) is still a relay on a chip this hub provisioned.
    # DeviceManager.set_state calls through here -- synchronously, because
    # it runs on the executor's thread -- via the mqtt_driver seam wired in
    # main.py. It shares _publish_sync with _publish below: that method is
    # console-facing (it explains failures in sentences and self-repairs
    # accounts); a drive wants short timeouts and a machine-readable
    # verdict, and those are the only differences.

    @staticmethod
    def _snake_name(name: str) -> str:
        """The same normalisation DeviceManager.board_device_id applies, so
        a device id's suffix and a board item's name meet in the middle."""
        return re.sub(r"[^a-z0-9]+", "_",
                      str(name).strip().lower()).strip("_")

    def _drive_target(self, plan: "cat.BoardPlan",
                      suffix: str) -> Optional[Tuple[str, str]]:
        """(command head, kind) for one item's drivable output --
        ("Power3", "relay") or ("Channel2", "pwm") -- or None when the item
        has no output the hub can drive directly (a sensor, a bus device)."""
        want = self._snake_name(suffix)
        shutters = 0
        for item in plan.items:
            device = cat.BY_ID[item.type_id]
            # THE SHUTTER NUMBER, counted exactly as configuration_commands
            # counts it -- every shutter-declaring item on the board, in
            # plan order -- so the command below names the shutter Tasmota
            # was told about, whatever relays it landed on.
            declares_shutter = any("{sh}" in c for c in device.setup)
            if declares_shutter:
                shutters += 1
            if self._snake_name(item.name) != want:
                continue
            if declares_shutter:
                # A gate, blind, garage, curtain or servo is driven through
                # Tasmota's shutter driver -- ShutterOpen / ShutterClose /
                # ShutterPosition -- not through its first relay. Power1 ON
                # only ever drove the OPEN relay: a position never reached
                # the motor and "close" merely stopped the opening
                # (2026-09-10, the gate that stops half-way).
                return (f"Shutter{shutters}", "shutter")
            for slot in device.slots:
                comp = slot.component.name
                n = item.instances.get(slot.key)
                if not n:
                    continue
                if comp == "Relay":
                    return (f"Power{n}", "relay")
                if comp == "PWM":
                    return (f"Channel{n}", "pwm")
            return None
        return None

    #: Character LCDs the hub can write a line to. TM1637 and MAX7219 are
    #: deliberately absent: DisplayText means something different on a
    #: seven-segment and on a dot matrix, and a line of prose on either is
    #: not a thing anyone can read.
    #:
    #: DERIVED FROM THE CATALOGUE (2026-09-12), not typed out beside it.
    #: A hand-written list here is the same drift that cost a whole
    #: afternoon on the card reader: main.py called a PN532 a card reader
    #: while the engine's `_TAG_TYPES` did not, so a tap arrived and was
    #: dropped in silence. The rule that decides a display is simple and
    #: lives in the catalogue already -- a part whose setup asserts
    #: `DisplayModel` is a display -- so asking the catalogue means a new
    #: screen added there cannot be forgotten here. `_CHARACTER_ROWS`
    #: keeps the second, narrower judgement explicit: which of them takes
    #: a LINE OF TEXT, which is the thing seven-segments cannot.
    _CHARACTER_ROWS = ("DisplayCols", "DisplayRows")

    @classmethod
    def _display_types(cls) -> Tuple[str, ...]:
        """Every catalogue part this hub can wake and write a line to."""
        found = []
        for dev in cat.CATALOGUE:
            setup = [str(s).strip() for s in dev.setup]
            # Model 1 is Tasmota's character-LCD driver (xdsp_01), the only
            # one whose DisplayText takes the `[x<col>y<row>]` character
            # cursor this hub writes with. Graphic panels position in
            # pixels, a TM1637/MAX7219 (models 15/19) takes a few digits:
            # a line of prose sent to either comes out as garbage.
            if "DisplayModel 1" not in setup:
                continue
            # A LINE OF PROSE NEEDS A GRID OF CHARACTERS. A seven-segment
            # answers DisplayModel too and cannot show a sentence, so the
            # geometry is what separates them -- and it is the same pair
            # display_targets already reads for cols and rows.
            if all(any(s.startswith(k) for s in setup)
                   for k in cls._CHARACTER_ROWS):
                found.append(dev.id)
        return tuple(found)

    def display_targets(self) -> List[Dict[str, Any]]:
        """Every character LCD this hub has provisioned: where to write, and
        how wide.

        Width comes from the catalogue entry's OWN setup line
        ("DisplayCols 20"), so the number the hub pads to and the number
        written to the chip are one fact. A catalogue that changes the
        geometry changes both.
        """
        out: List[Dict[str, Any]] = []
        text_types = self._display_types()
        for slug, record in self._boards():
            if not record.get("topic"):
                continue
            # One panel per board: Tasmota drives a single display, and
            # every piece of panel state downstream is keyed by topic.
            if any(p["topic"] == record["topic"] for p in out):
                continue
            for item in (record.get("items") or []):
                if not isinstance(item, dict):
                    continue
                type_id = item.get("type_id")
                if type_id not in text_types or type_id not in cat.BY_ID:
                    continue
                # ROWS TOO, from the same place and for the same reason
                # (2026-09-01). The hub writes at a CURSOR, and a cursor
                # nobody positions walks down the screen -- so the writer
                # has to know how many rows exist in order to address them.
                # Read off the catalogue's own "DisplayRows 4" exactly as
                # the width is, so the geometry stays one fact.
                cols, rows = 20, 2
                # THE ADDRESS THE PANEL ANSWERS ON, read off the same
                # catalogue line that is sent to the chip ("DisplayAddress
                # 39" -- decimal, 0x27), so the number the hub LOOKS FOR on
                # the bus and the number the panel is CONFIGURED for are one
                # fact and cannot drift apart (2026-09-14). None when the
                # catalogue states no address: then there is nothing to look
                # for and the I2C scan draws no conclusion.
                addr: Optional[int] = None
                # THE SETTINGS THAT MUST BE TRUE BEFORE THE HUB CAN WRITE,
                # carried alongside the geometry (2026-09-01: "when I power
                # cycle, no light at all until I send DisplayModel 1").
                # These land on the chip once, at install -- and a board
                # that has rebooted since can come back up with its panel
                # dark and its mode wherever the last person left it, which
                # is a screen the hub owns and cannot use. The catalogue
                # already states them; this hands them to whoever re-asserts
                # them, so the numbers stay in exactly one place.
                wake: List[str] = []
                for line in cat.BY_ID[type_id].setup:
                    head, _, body = str(line).partition(" ")
                    if head == "DisplayCols" and body.strip().isdigit():
                        cols = int(body.strip())
                    elif head == "DisplayRows" and body.strip().isdigit():
                        rows = int(body.strip())
                    elif head == "DisplayAddress" and body.strip().isdigit():
                        addr = int(body.strip())
                    elif head in ("DisplayModel", "DisplayMode",
                                  "DisplayDimmer"):
                        wake.append(str(line))
                # DisplayModel restarts the board: last, so the settings
                # before it are applied first.
                wake.sort(key=lambda c: c.startswith("DisplayModel"))
                setup = [str(line) for line in cat.BY_ID[type_id].setup]
                setup.sort(key=lambda c: c.startswith("DisplayModel"))
                out.append({
                    "slug": slug,
                    "topic": record["topic"],
                    "name": item.get("name") or type_id,
                    "cols": cols,
                    "rows": rows,
                    "addr": addr,
                    "wake": wake,
                    # EVERY display setting the catalogue states -- address,
                    # geometry, model, dimmer -- for the one case the wake
                    # cannot cover: a board whose display settings are GONE
                    # from the chip (it answers DisplayModel 0). Kitchen,
                    # 2026-09-10, straight after healing from safeboot: its
                    # Wi-Fi and broker settings intact, its panel settings
                    # not, and a soft wake of a driver that is not
                    # configured lights nothing. See _note_display_model.
                    "setup": setup,
                })
                break                       # one panel per board
        return out

    def _colour_target(self, plan: "cat.BoardPlan",
                       suffix: str) -> Optional[List[str]]:
        """The three PWM channel heads of an RGB item, red/green/blue in that
        order -- or None when the item is not a colour light.

        PER ITEM, not per board. Tasmota's own `Color` command addresses the
        device's global light channels, which is the wrong light the moment a
        board carries two of them; the item's own slots say exactly which
        three channels are its.
        """
        want = self._snake_name(suffix)
        for item in plan.items:
            if self._snake_name(item.name) != want:
                continue
            heads: List[str] = []
            for key in ("red", "green", "blue"):
                n = item.instances.get(key)
                if not n:
                    return None
                heads.append(f"Channel{n}")
            return heads
        return None

    def drive_commands(self, plan: "cat.BoardPlan", suffix: str, *,
                       on: Optional[bool] = None,
                       level: Optional[float] = None,
                       rgb: Optional[Sequence[float]] = None
                       ) -> Tuple[List[str], str]:
        """The Tasmota commands that put one item in the wanted state, as
        (commands, why). An empty list means nothing can be sent and `why`
        says so. Shared by drive_item_sync and the automation engine so the
        two paths cannot drift apart.

        on=False always wins over a level for relays and dimmers: restoring
        {"on": False, "level": 50} must leave the output OFF, not at 50%.
        A shutter's level is its position, so there a level wins."""
        def pct(v: float) -> int:
            return max(0, min(100, int(round(float(v)))))

        if rgb is not None:
            heads = self._colour_target(plan, suffix)
            if heads is None:
                return [], "not a colour light"
            if on is False:
                return [f"{h} 0" for h in heads], ""
            if len(rgb) != len(heads):
                return [], "colour needs three values"
            return [f"{h} {pct(float(v) / 255 * 100)}"
                    for h, v in zip(heads, rgb, strict=True)], ""
        target = self._drive_target(plan, suffix)
        if target is None:
            return [], "not a drivable output"
        head, kind = target
        if kind == "shutter":
            n = head[len("Shutter"):]
            if level is not None:
                return [f"ShutterPosition{n} {pct(level)}"], ""
            if on is not None:
                return [f"ShutterOpen{n}" if on else f"ShutterClose{n}"], ""
            return [], "nothing to change"
        if kind == "relay":
            want_on = on if on is not None else (
                None if level is None else float(level) > 0)
            if want_on is None:
                return [], "nothing to change"
            return [f"{head} {'ON' if want_on else 'OFF'}"], ""
        if on is False:
            return [f"{head} 0"], ""
        value = level if level is not None else (100.0 if on else None)
        if value is None:
            return [], "nothing to change"
        return [f"{head} {pct(value)}"], ""

    def drive_item_sync(self, slug: str, suffix: str, *,
                        on: Optional[bool] = None,
                        level: Optional[float] = None,
                        rgb: Optional[Tuple[int, int, int]] = None
                        ) -> Tuple[bool, str]:
        """Drive one board item straight over MQTT. Returns (ok, why).

        `why` is a short machine-ish string for the event log; nothing here
        writes to the console -- the caller (DeviceManager.set_state via
        the driver seam) already owns success/failure reporting."""
        record = self._read_board(slug)
        if record is None:
            return False, "no board record"
        plan = self._plan_for(slug)
        if plan is None:
            return False, "no plan"
        commands, why = self.drive_commands(plan, suffix, on=on,
                                            level=level, rgb=rgb)
        if not commands:
            return False, why
        state = self._drive_publish_sync(record, commands)
        event("MQTT", f"drive {slug}.{suffix}: {commands} -> {state}")
        return state == "sent", state

    def _drive_publish_sync(self, record: Dict[str, Any],
                            commands: List[str]) -> str:
        """The quiet, synchronous half of a drive: _publish_sync with short
        timeouts. Returns a state string: sent / offline / refused /
        no-broker / no-creds / no-paho / error:<...>."""
        topic = record.get("topic")
        if not topic:
            return "no-creds"
        mqtt_cfg = self._mqtt_settings()
        state, _rc, exc = self._publish_sync(
            topic, commands, mqtt_cfg["host"], mqtt_cfg["port"], 6.0)
        if state == "unacknowledged":
            return "error:unacknowledged"
        return f"error:{type(exc).__name__}" if state == "error" else state

    @staticmethod
    def _pump(client, done, seconds: float) -> bool:
        """Run paho's network loop until done() or `seconds` pass; done()."""
        deadline = time.monotonic() + seconds
        while not done() and time.monotonic() < deadline:
            client.loop(timeout=0.3)
        return bool(done())

    @contextlib.contextmanager
    def _mqtt_session(self, host: str, port: str, topics: List[str],
                      on_payload, *, wait: float,
                      publish: Optional[Tuple[str, str]] = None):
        """THE ONE broker conversation: sign in as the hub, wait up to `wait`
        seconds for the CONNACK and -- once accepted -- subscribe `topics`,
        then publish `publish` (topic, payload). Subscribe before asking: a
        reply that beats the subscription is a reply that never happened.
        Yields (client, rc); rc None means no CONNACK arrived. Always
        disconnects. Raises what _mqtt_client and connect() raise.

        WAIT FOR THE CONNACK, do not merely connect: connect() only sends
        CONNECT, and the broker's verdict arrives later, in on_connect. The
        client runs MQTT 3.1.1 on the VERSION1 callback API, so `rc` is a
        plain int on paho 1.x and 2.x alike."""
        client = self._mqtt_client()
        box: Dict[str, Any] = {"rc": None}

        def on_connect(c, _u, _flags, rc, *_rest):
            box["rc"] = rc
            if rc == 0:
                for topic in topics:
                    c.subscribe(topic)
                if publish:
                    c.publish(*publish)

        client.on_connect = on_connect
        client.on_message = lambda _c, _u, m: on_payload(
            m.payload.decode("utf-8", "replace"))
        try:
            client.connect(host, int(port), 10)
            self._pump(client, lambda: box["rc"] is not None, wait)
            yield client, box["rc"]
        finally:
            try:
                client.disconnect()
            except Exception:                       # noqa: BLE001
                pass

    def _publish_sync(self, topic: str, commands: List[str], host: str,
                      port: str, wait: float
                      ) -> Tuple[str, Optional[int], Optional[BaseException]]:
        """Connect as the hub, confirm the retained LWT says Online (a
        switched-off board must fail, not 'send'), publish each command at
        QoS 1 and wait for its PUBACK, disconnect. Shared by the console
        path (_publish) and the quiet drive path (_drive_publish_sync), which
        differ only in timeouts and in how a failure is told.

        Returns (state, rc, exc): state is sent / offline / refused /
        no-broker / no-paho / unacknowledged / error; exc is set for
        unacknowledged and error."""
        lwt: List[str] = []
        try:
            with self._mqtt_session(host, port, [f"tele/{topic}/LWT"],
                                    lwt.append, wait=wait) as (client, rc):
                if rc is None:
                    return "no-broker", None, None
                if rc != 0:
                    return "refused", rc, None
                self._pump(client, lambda: lwt, self.LWT_WAIT)
                if not lwt or lwt[0].strip().lower() != "online":
                    return "offline", rc, None
                for command in commands:
                    head, _, body = command.partition(" ")
                    info = client.publish(f"cmnd/{topic}/{head}", body, qos=1)
                    if not self._pump(client, info.is_published, wait):
                        # No PUBACK within the window: the broker never
                        # confirmed it holds the command, so "sent" would
                        # be a guess.
                        return "unacknowledged", rc, TimeoutError(
                            f"the broker did not acknowledge {head}")
                return "sent", rc, None
        except ImportError:
            return "no-paho", None, None
        except Exception as exc:                    # noqa: BLE001
            return "error", None, exc

    async def _publish(self, record: Dict[str, Any],
                       commands: List[str], *,
                       host: Optional[str] = None,
                       _repaired: bool = False) -> bool:
        """Send commands to a board that is actually listening.
        A publish only proves the BROKER took the bytes. Every caller here
        means "the board got it", so the retained LWT is read first -- without
        that, a switched-off board is reported as told, and change_wifi's
        cable fallback never runs.
        Never raises: every caller is a menu action.
        `_repaired` guards the one self-repair this method may attempt: a
        refused login whose password-file entry reads back missing or
        malformed is recreated from the record and retried ONCE. That is
        the automated form of the by-hand mosquitto_passwd that ended the
        migration night, and one attempt is the whole budget -- a second
        refusal means the record and the file genuinely disagree, which a
        loop would only hammer on.
        """
        topic = record.get("topic")
        name = record.get("name") or record.get("slug") or "That board"
        if not topic:
            await self._io.write(
                f"  {name} has no broker topic on file, so there is no "
                f"way to speak to it. It is not a board this hub provisioned.",
                level="warn")
            return False
        mqtt = self._mqtt_settings()
        if host:
            mqtt["host"] = host
        state, rc, exc = await asyncio.to_thread(
            self._publish_sync, topic, commands, mqtt["host"], mqtt["port"],
            10.0)
        if state == "sent":
            return True
        if state == "no-paho":
            await self._io.write(
                "  The MQTT library is not installed on this hub "
                "(pip install paho-mqtt), so no board can be reached.",
                level="warn")
            return False
        if state == "offline":
            event("MQTT", f"{topic}: not online")
            await self._io.write(
                f"  {name} is not connected to the broker, so it was not "
                f"told. It is switched off, or not on the network yet.",
                level="warn")
            return False
        if state == "refused":
            # NOT the board: the broker refused THE HUB'S OWN login. Its
            # password-file entry is repairable from the stored password,
            # right here, once.
            if not _repaired:
                try:
                    from app import setup_manager
                    hub_user = setup_manager.ensure_hub_account()[0]
                    entry = self._account_state(hub_user)
                    if entry in ("missing", "malformed"):
                        await self._io.write(
                            f"  The broker refused the hub's login -- its "
                            f"entry in the password file reads back {entry}. "
                            f"Recreating it...")
                        await asyncio.to_thread(
                            setup_manager.ensure_hub_account, True)
                        return await self._publish(record, commands,
                                                   host=host, _repaired=True)
                except Exception:                  # noqa: BLE001
                    pass
            event("MQTT", f"{topic}: broker refused this hub (rc={rc})")
            await self._io.write(
                f"  The broker refused THIS HUB's own login, so {name} was "
                f"never contacted. Run `python setup_manager.py --broker` "
                f"to re-mint the hub account, or restart Mosquitto so it "
                f"re-reads "
                f"{_config.MQTT_PASSWD_FILE}.",
                level="warn")
            return False
        if isinstance(exc, ssl.SSLError):
            # MUST be tested above OSError -- ssl.SSLError inherits from it,
            # and reporting the hub's own TLS failure as a switched-off board
            # sends you to check a cable when the fault is on this machine.
            event("MQTT", f"{topic}: {type(exc).__name__}: {exc}")
            await self._io.write(
                f"  The hub could not agree TLS with its own broker at "
                f"{mqtt['host']}:{mqtt['port']} -- "
                f"{getattr(exc, 'reason', None) or exc}. This end failed, not "
                f"{name}. Either that broker is not the one setup_manager "
                f"started, or it serves a different certificate to the one at "
                f"{_config.BROKER_TLS_CERT}.", level="warn")
            return False
        if isinstance(exc, RuntimeError):
            await self._io.write(f"  {exc}. Not connecting.", level="warn")
            return False
        if exc is None or isinstance(exc, (OSError, TimeoutError,
                                           asyncio.TimeoutError)):
            # THE BROKER, not the board. A board being off is now its own
            # branch above, so this really is a hub-side fault.
            event("MQTT", f"broker unreachable: {exc}")
            await self._io.write(
                f"  The broker at {mqtt['host']}:{mqtt['port']} did not "
                f"answer, so nothing was sent to {name}. Is Mosquitto "
                f"running on this machine?", level="warn")
            return False
        # The exception was caught on the worker thread: print_exc() here,
        # outside any handler, printed only "NoneType: None".
        traceback.print_exception(exc)
        await self._io.write(
            f"  {name} could not be reached: {type(exc).__name__}: {exc}",
            level="warn")
        return False

    async def _mqtt_listen(self, record: Dict[str, Any], topics: List[str], *,
                           publish: Optional[Tuple[str, str]] = None,
                           timeout: float = 8.0, settle: float = 0.0,
                           want: Optional[str] = None) -> List[str]:
        """Subscribe, optionally ask, and collect what comes back.
        The ONE listener. Subscribe before asking -- a reply that beats the
        subscription is a reply that never happened -- and silence comes back
        empty, never as an invented answer.
        `want` is a substring a payload must carry to count. stat/+/RESULT is
        Tasmota's catch-all, so without it an unrelated message arriving first
        was read as the answer to a question it had nothing to do with.
        `settle` keeps listening that long after the first match, for replies
        Tasmota splits across several publishes (GPIOs1, GPIOs2, ...).

        SILENCE HAS SEVERAL CAUSES AND THIS METHOD REPORTED NONE OF THEM
        (2026-09-12). Every failure in here was `except Exception: return`,
        so a broker that refused the hub's login, a TLS setting with no
        certificate behind it, a board record with no topic, and a room
        where nobody pressed the remote ALL came back as the same empty
        list. Learn IR then said "Nothing heard. Check the receiver is
        pointed at, and try that button again" to somebody whose hub never
        reached the broker at all -- and would have gone on saying it.

        The reason is kept in `listen_problem()` for the caller to add to
        its own sentence. The same shape `_drive_problem` already uses for a
        failed drive, and for the same reason: this seam is the only place
        that knows, and a bare empty list throws it away.
        """
        self._listen_problem = ""
        if not record.get("topic"):
            self._listen_problem = "that board's record carries no MQTT topic"
            return []
        mqtt = self._mqtt_settings()
        seen: List[str] = []

        def heard(text: str) -> None:
            if want is None or want in text:
                seen.append(text)

        def work() -> str:
            deadline = time.monotonic() + timeout
            try:
                with self._mqtt_session(
                        mqtt["host"], mqtt["port"], topics, heard,
                        wait=timeout,
                        publish=(publish and (
                            f"cmnd/{record['topic']}/{publish[0]}",
                            publish[1]))) as (client, rc):
                    if rc is None:
                        return (f"the broker at {mqtt['host']}:{mqtt['port']} "
                                f"never answered the hub's connection")
                    if rc:
                        # 4 and 5 are bad credentials / not authorised: the
                        # hub account is missing from the broker's password
                        # file, or the broker never reloaded it. The
                        # automation engine says exactly this on its own
                        # connection; this listener said nothing at all.
                        return (f"the broker refused the hub login (rc={rc}) "
                                f"-- run: python setup_manager.py --broker, "
                                f"then restart the broker"
                                if rc in (4, 5) else
                                f"the broker refused the connection (rc={rc})")
                    if self._pump(client, lambda: seen,
                                  deadline - time.monotonic()) and settle > 0:
                        self._pump(client, lambda: False, settle)
            except (ImportError, RuntimeError) as exc:
                # _mqtt_client's TLS refusal -- "TLS is on but there is no
                # certificate to verify the broker against" -- a sentence
                # worth repeating verbatim.
                return f"{type(exc).__name__}: {exc}"
            except Exception as exc:               # noqa: BLE001
                return (f"the broker at {mqtt['host']}:{mqtt['port']} could "
                        f"not be reached ({type(exc).__name__}: {exc})")
            return ""
        self._listen_problem = await asyncio.to_thread(work)
        return seen

    def listen_problem(self) -> str:
        """Why the last _mqtt_listen heard nothing, when it was the HUB's end
        that failed rather than the room simply being quiet. Empty string
        when the listener worked and nothing was said -- which is the case
        the caller's own sentence already covers."""
        return getattr(self, "_listen_problem", "") or ""

    async def _mqtt_question(self, record: Dict[str, Any], command: str,
                             payload: str, reply_suffix: str, *,
                             want: Optional[str] = None,
                             settle: float = 0.0) -> List[str]:
        """One question to a running board, and its reply."""
        return await self._mqtt_listen(
            record, [f"stat/{record['topic']}/{reply_suffix}"],
            publish=(command, payload), settle=settle, want=want)

    async def wait_for_ir(self, slug: str,
                          timeout: float) -> Optional[Dict[str, Any]]:
        """One remote-control press, heard by a board's own IR receiver.
        An EVENT, not a state, which is why none of this goes through Home
        Assistant. Bounded on purpose: a learn flow asks for ONE press and
        must give up if none comes. Both tele/ topics, because some builds
        publish IrReceived to RESULT and some to SENSOR.
        """
        record = self._read_board(slug)
        if record is None:
            return None
        heard = await self._mqtt_listen(
            record, [f"tele/{record['topic']}/+"],
            timeout=timeout, want='"IrReceived"')
        for text in heard:
            try:
                payload = json.loads(text)
            except ValueError:
                continue
            if isinstance(payload, dict) and isinstance(
                    payload.get("IrReceived"), dict):
                return payload["IrReceived"]
        return None

    async def send_ir(self, slug: str, code: Dict[str, Any]) -> bool:
        """Replay a learned code: JSON for a decoded one, raw timings for a
        raw one (ir_flows.ir_command_body). False when it cannot be sent."""
        from .ir_flows import ir_command_body
        body = ir_command_body(code)
        record = self._read_board(slug)
        if record is None or body is None:
            return False
        return await self._publish(record, [f"IRsend {body}"])

    def boards_with(self, type_id: str) -> List[str]:
        """Every board carrying a given catalogue device, by slug."""
        return [slug for slug, record in self._boards()
                if any(isinstance(item, dict) and item.get("type_id") == type_id
                       for item in record.get("items") or [])]

    # ── what the board says about itself ────────────────────────────────
    #
    # A WROVER's RAM chip lives on GPIO16/17, and grounding either one
    # crash-loops the board -- so the template only grounds them on a board
    # KNOWN not to have it. Nothing on the chip says which module it sits in;
    # the one authority is the running firmware: `Status 4` reports PsrMax,
    # zero on a WROOM.

    @staticmethod
    def _field(lines: List[str], key: str) -> Optional[str]:
        """A string field out of a Tasmota reply; None when no line had it.
        Pure, and case-insensitive: the read-back this feeds is the check
        that catches settings which were parsed but not kept, and it should
        not turn into a shrug because a firmware capitalised a key
        differently.
        """
        pattern = re.compile(r'"%s"\s*:\s*"([^"]*)"' % re.escape(key), re.I)
        for line in lines:
            found = pattern.search(line)
            if found:
                return found.group(1)
        return None

    @classmethod
    def _psram_answer(cls, lines: List[str],
                      board: Optional["chips.Board"] = None
                      ) -> Optional[bool]:
        """PsrMax out of a Status 4 reply, as "does the module own its PSRAM
        pins". None must stay None: "could not tell" and "told me no" lead
        to opposite decisions about driving a RAM chip's clock line.

        Per family: on the classic ANY PSRAM sits on GPIO16/17 (a WROVER),
        so any size is a yes. On the S3 only OCTAL PSRAM owns 33-37, and
        octal comes in 8 MB and up; a 2 or 4 MB quad chip uses the flash
        bus's own pins and leaves 33-37 free."""
        board = board or chips.DEFAULT
        for line in lines:
            found = re.search(r'"PsrMax"\s*:\s*(\d+)', line, re.I)
            if found:
                size_kb = int(found.group(1))
                if board.family == "esp32s3":
                    return cls._s3_octal(size_kb / 1024.0)
                return size_kb > 0
        return None

    @staticmethod
    def _s3_octal(size_mb: float) -> Optional[bool]:
        """An S3 module's PSRAM size (MB) -> does it own GPIO33-37.
        Tasmota's PsrMax is the heap's view (a little under the chip's
        size), hence the slack. Sizes no S3 module ships are None."""
        if size_mb <= 0:
            return False                    # no PSRAM at all
        if size_mb >= 6:
            return True                     # 8/16 MB: octal
        if size_mb <= 4.1:
            return False                    # 2/4 MB: quad
        return None

    @classmethod
    def _psram_from_chip(cls, chip: str, features: str,
                         board: "chips.Board") -> Optional[bool]:
        """What esptool's own chip description says about an S3's PSRAM
        ("ESP32-S3R8", "Embedded PSRAM 8MB"), BEFORE anything runs on it.
        None when it does not say (a module can carry PSRAM esptool
        cannot see) -- None stays refused until Status 4 answers."""
        if board.family != "esp32s3":
            return None
        text = f"{chip} {features}"
        found = (re.search(r"Embedded PSRAM\s*(\d+)\s*MB", text, re.I)
                 or re.search(r"ESP32-S3R(\d+)", text, re.I))
        if not found:
            return None
        return cls._s3_octal(float(found.group(1)))

    @staticmethod
    def _psram_sentence(board: "chips.Board") -> str:
        pins = sorted(board.psram)
        where = (f"GPIO{pins[0]} and {pins[1]}" if len(pins) == 2
                 else f"GPIO{pins[0]}-{pins[-1]}")
        return (f"  This module carries PSRAM, so {where} belong to its RAM "
                f"chip -- the hub will leave them alone.")

    @staticmethod
    def _pins_reported(lines: List[str]) -> Optional[List[int]]:
        """The board's own pin listing, out of a `Gpio 255` reply.
        The BOARD is the referee for which pins exist; the catalogue stays
        the referee for what they MEAN. None when nothing carried a listing;
        never an empty list invented from silence.
        """
        pins = set()
        for line in lines:
            for found in re.finditer(r'"GPIO(\d+)"', line):
                number = int(found.group(1))
                if number <= 48:                   # no chip goes higher
                    pins.add(number)
        return sorted(pins) if pins else None

    @staticmethod
    def _pins_assigned(lines: List[str]) -> Dict[int, Tuple[int, str]]:
        """What each pin is set to now, from a `Gpio 255` reply (one or
        several publishes): {pin: (component code, the board's name for it)}.
        `{"GPIO5":{"6272":"RC522 CS"},"GPIO4":{"0":"None"}}`. "GPIOs1" (the
        `GPIOs` list) never matches."""
        out: Dict[int, Tuple[int, str]] = {}
        for line in lines:
            for found in re.finditer(
                    r'"GPIO(\d+)"\s*:\s*\{\s*"(\d+)"\s*:\s*"([^"]*)"', line):
                out[int(found.group(1))] = (int(found.group(2)),
                                            found.group(3))
        return out

    @staticmethod
    def _board_of(record: Dict[str, Any]) -> "chips.Board":
        """The board profile a record names. No "board" field: the classic
        DevKit, exactly as before profiles. A board FILE that has since
        gone: the generic board of the family the record also stores,
        never the classic pin table by accident."""
        _load_board_files()
        board_id = record.get("board")
        if board_id and not chips.known(board_id) \
                and record.get("board_family") in chips.FAMILIES:
            return chips.FAMILIES[record["board_family"]]
        return chips.get(board_id)

    def _module_pins(self, record: Dict[str, Any],
                     board: Optional["chips.Board"] = None) -> List[int]:
        """The pins the module keeps for itself (its PSRAM, a PICO's
        flash), for the pin map's `pins_owned_by_module`. Only when the
        board has SAID so: unknown is checked at install (see
        _psram_unknown_stop)."""
        board = board or self._board_of(record)
        owns = self._owns_psram_pins(record)
        if owns is None:
            owns = board.psram_known
        return sorted(board.psram) if owns else []

    async def _psram_unknown_stop(self, plan: "cat.BoardPlan") -> bool:
        """True (and said) when this wiring uses the pins a module's own
        memory can sit on (PSRAM, a PICO's flash) and the board has not
        said yet whether it does. Driving them on a board that has it
        crash-loops it with the template saved, so nothing is sent; the
        install asks the board first, so this is the rare case where it
        could not answer."""
        if plan.owns_psram_pins is not None or not plan.board.psram:
            return False
        on = sorted({item.name for item in plan.items
                     for pin in item.pins.values() if pin in plan.board.psram})
        if not on:
            return False
        pins = ", ".join(f"GPIO{p}" for p in sorted(plan.board.psram))
        await self._io.write(
            f"  Wiring not sent: {', '.join(on)} use {pins}, and this board "
            f"has not said yet whether its own memory (PSRAM) sits on those "
            f"pins -- driving them would crash it if it does. Install again "
            f"with the cable; the board is asked first.", level="warn")
        return True

    @staticmethod
    def _owns_psram_pins(record: Dict[str, Any]) -> Optional[bool]:
        """Does this module keep GPIO16/17 for itself?
        A conclusion drawn from two facts the record stores raw and never
        bends. A PICO keeps its internal flash there, so its truthful "no
        PSRAM" changes nothing -- which is why PsrMax is not overwritten to
        fake a yes.
        """
        if "PICO" in str(record.get("chip") or "").upper():
            return True
        return record.get("psram")

    async def _report_pin_differences(
            self, reported: List[int],
            board: Optional["chips.Board"] = None) -> None:
        """Report, never correct: neither side should silently override the
        other. On a classic ESP32 this prints nothing; if it speaks, either
        the table is wrong or the firmware changed."""
        usable = set((board or chips.DEFAULT).usable)
        extra = sorted(set(reported) - usable)
        missing = sorted(usable - set(reported))
        if extra:
            await self._io.write(
                f"  The board offers GPIO {extra} that this hub's pin table "
                f"does not -- the table may be behind the firmware.",
                level="warn")
        if missing:
            await self._io.write(
                f"  This hub's pin table offers GPIO {missing} that the board "
                f"itself does not list -- trust the board.", level="warn")

    async def _learn_board_facts(self, record: Dict[str, Any]) -> None:
        """Ask a running board whatever its record is missing, for boards
        provisioned before these questions existed."""
        changed = False
        board = self._board_of(record)
        if record.get("psram") is None:
            answer = self._psram_answer(await self._mqtt_question(
                record, "Status", "4", "STATUS4"), board)
            if answer is not None:
                record["psram"] = answer
                changed = True
                if answer and board.psram:
                    await self._io.write(self._psram_sentence(board))
        if record.get("pins_reported") is None:
            # settle: a long `Gpio 255` answer arrives in several RESULT
            # publishes, and the first alone is a truncated pin list.
            reported = self._pins_reported(await self._mqtt_question(
                record, "Gpio", "255", "RESULT", want='"GPIO', settle=1.0))
            if reported is not None:
                record["pins_reported"] = reported
                changed = True
                await self._report_pin_differences(reported, board)
        if changed:
            self._write_board(record)

    # ── does this board's firmware carry the driver? ────────────────────
    #
    # A board accepts ANY component code in a template and echoes it back
    # from `Gpio 255`, whether or not the driver behind it was compiled into
    # its binary -- the RC522 sat like that for a day, perfectly assigned and
    # perfectly silent. Only the SHORT `GPIOs` list is filtered to what the
    # binary can actually use.

    @staticmethod
    def _drivers_needed(plan: "cat.BoardPlan") -> List[str]:
        """Empty for most boards, which is why they never pay for the ask."""
        return sorted({name
                       for item in plan.items
                       if item.type_id in cat.BY_ID
                       for name in cat.BY_ID[item.type_id].driver_names})

    @classmethod
    def _missing_drivers(cls, plan: "cat.BoardPlan",
                         gpios_reply: List[str]) -> Optional[List[str]]:
        """[] means everything needed was there. None means the board did not
        answer -- and None stays None: "could not tell" is not "told me no"."""
        needed = cls._drivers_needed(plan)
        if not needed:
            return []
        if not gpios_reply:
            return None
        text = "\n".join(gpios_reply)
        # Quoted, so "RC522 CS" cannot be satisfied by a key that merely
        # contains those letters.
        return [name for name in needed if f'"{name}"' not in text]

    async def _refuse_if_driver_missing(self, plan: "cat.BoardPlan",
                                        gpios_reply: List[str],
                                        board_name: str, *,
                                        learn: bool = False) -> bool:
        """True = go ahead; False = do not send.

        `learn=True` ONLY when this hub has JUST flashed its firmware file
        onto this very chip -- that is the one moment the board's answer
        is a fact about the FILE. It used to learn from every board it
        asked, which let a board that was OTA'd from its own web page, or
        one still running last month's image, write its verdict against
        the hash of a file it has never run: the fixed build on disk then
        refused installs for drivers it genuinely carries (audit,
        2026-08-25).
        """
        missing = self._missing_drivers(plan, gpios_reply)
        if missing is None:
            await self._io.write(
                "  Could not confirm this board's firmware carries every "
                "driver the wiring needs (it did not answer `GPIOs`). Sending "
                "anyway; if a device stays silent, check that first.",
                level="warn")
            return True
        if not missing:
            if learn:
                self._remember_firmware_drivers(self._drivers_needed(plan),
                                                [], plan.board)
            return True
        # LEARNED, not merely reported -- but only when the chip provably
        # runs this file (see `learn` above). Recording a fresh flash's
        # refusal against the file's own hash is what turns this from a
        # lesson repeated after every cable session into one refusal
        # before the cable comes out.
        if learn:
            self._remember_firmware_drivers([], missing, plan.board)
        shipped = self._firmware(plan.board)
        await self._io.write(
            f"  Not sent. {board_name}'s firmware does not carry "
            f"{', '.join(missing)} -- the template would take the pins and the "
            f"device would never speak. The wiring is saved here either way.",
            level="warn")
        await self._io.write(
            f"  THE FILE THIS HUB FLASHES IS THE ONE TO FIX: "
            f"{shipped if shipped else 'the firmware file'}. It was built "
            f"without those drivers, so every install repeats this. Replace "
            f"it with {plan.board.firmware} from the build that has them "
            f"(the green workflow run whose check found mfrc522 in the map), "
            f"keeping the file name exactly (from the app: Settings "
            f"-> Install firmware on hub), then install this board again. "
            f"A one-off alternative for a board already running: Firmware "
            f"Upgrade on its own web page with {plan.board.ota_name} -- that "
            f"fixes this board and nothing else.", level="warn")
        return False

    # ── what the SHIPPED firmware file is known to carry ─────────────────
    #
    # Keyed by the file's own hash, so replacing the file invalidates the
    # note by construction -- nothing to clear, nothing to go stale.

    def _firmware_notes_path(self) -> Path:
        return self._dir() / "firmware_drivers.json"

    def _firmware_key(self, board: Optional["chips.Board"] = None) -> str:
        """A short hash of the firmware file this hub would flash onto
        `board`'s family -- so each family's file carries its own notes."""
        import hashlib
        path = self._firmware(board)
        if path is None:
            return ""
        try:
            digest = hashlib.sha256()
            with open(path, "rb") as handle:
                for block in iter(lambda: handle.read(1 << 20), b""):
                    digest.update(block)
            return digest.hexdigest()[:16]
        except OSError:
            return ""

    def _remember_firmware_drivers(self, present: List[str],
                                   missing: List[str],
                                   board: Optional["chips.Board"] = None
                                   ) -> None:
        """Record what a board proved about the firmware file it is running
        (its family's file). Never raises: this is a convenience, and a hub
        that cannot write a note must still be able to flash a board."""
        key = self._firmware_key(board)
        if not key or not (present or missing):
            return
        path = self._firmware_notes_path()
        try:
            notes = json.loads(path.read_text("utf-8"))
            if not isinstance(notes, dict):
                notes = {}
        except Exception:                          # noqa: BLE001
            notes = {}
        entry = notes.get(key) if isinstance(notes.get(key), dict) else {}
        has = set(entry.get("has") or []) | set(present)
        lacks = (set(entry.get("lacks") or []) | set(missing)) - set(present)
        notes[key] = {"has": sorted(has), "lacks": sorted(lacks)}
        try:
            self._write_json(path, notes)
        except OSError:
            pass

    def _firmware_lacks(self, wanted: List[str],
                        board: Optional["chips.Board"] = None) -> List[str]:
        """Which of these drivers `board`'s family's firmware file is KNOWN
        to lack. Empty when it carries them, when nothing has been proven
        yet, or when the file cannot be read -- "not proven" is never
        "missing"."""
        key = self._firmware_key(board)
        if not key or not wanted:
            return []
        try:
            notes = json.loads(self._firmware_notes_path().read_text("utf-8"))
            lacks = set(notes[key]["lacks"])
        except Exception:                          # noqa: BLE001
            return []
        return sorted(set(wanted) & lacks)

    # ── sending a board its wiring ──────────────────────────────────────

    async def _wait_board_back(self, record: Dict[str, Any], *,
                               timeout: float = 75.0) -> bool:
        """Wait for a board to come BACK from the Module 0 restart.

        The proof is a ROUND TRIP: `Status 1` answered means the command
        parser is up and listening, which is the one thing the
        configuration half actually needs. The LWT is deliberately NOT
        the signal here -- mid-restart the broker can still be holding
        the stale retained Online, and the fresh Online can land before
        the parser is ready; both lies arrive faster than the truth
        (audit, 2026-08-25). Polls until the deadline; False means the
        board never answered, and the caller says so instead of firing
        settings into the dark.
        """
        deadline = time.monotonic() + timeout
        # It has not even finished going DOWN yet -- asking this early
        # gets an answer from the old boot, which is the same stale-lie
        # problem with extra steps.
        await asyncio.sleep(6.0)
        while time.monotonic() < deadline:
            heard = await self._mqtt_listen(
                record,
                [f"stat/{record['topic']}/STATUS1",
                 f"stat/{record['topic']}/RESULT"],
                publish=("Status", "1"), timeout=5.0)
            if heard:
                return True
            await asyncio.sleep(2.0)
        return False

    async def _send_plan(self, record: Dict[str, Any],
                         plan: "cat.BoardPlan") -> bool:
        """The one path that puts a template on a running board -- inside
        the app's job room, like a flash: the send takes up to a minute
        and a half (template, restart, the wait for the board to come
        back, its device settings) and its narration used to land as
        loose lines wherever the person happened to be standing
        (2026-09-10, "sending wiring should go to the working screen").
        """
        await self.open_job(f"Sending {record.get('name', 'the board')} its wiring",
                            "Template, restart, then its device settings",
                            seconds=90)
        try:
            return await self._send_plan_inner(record, plan)
        finally:
            await self.close_job()

    async def _send_plan_inner(self, record: Dict[str, Any],
                               plan: "cat.BoardPlan") -> bool:
        """The one path that puts a template on a running board.

        TWO PHASES, WITH A WAIT BETWEEN THEM. Module 0 applies the
        template by restarting the board, and Tasmota abandons whatever
        is queued behind it in the same Backlog -- so the per-device
        settings used to report "sent" while the restart ate them, and
        bedroom's DisplayModel read back 0 (2026-08-24). Activation goes
        first, then the round-trip wait, then configuration.
        """
        if self._drivers_needed(plan):
            reply = await self._mqtt_question(record, "GPIOs", "", "RESULT",
                                              want='"GPIO', settle=1.0)
            if not await self._refuse_if_driver_missing(plan, reply,
                                                        record["name"]):
                return False
        if await self._psram_unknown_stop(plan):
            return False
        activation, configuration = plan.backlogs()
        await self._io.write(f"  Sending {record['name']} its wiring...")
        if not await self._publish(record, activation):
            return False
        if not configuration:
            await self._io.write(
                "  Sent. It is restarting to apply it, which takes a few "
                "seconds.")
            return True
        await self._io.write(
            "  Template sent -- the board is restarting to apply it. "
            "Waiting for it to come back before its device settings go "
            "on...")
        if not await self._wait_board_back(record):
            await self._io.write(
                f"  {record['name']} never came back from its restart, so "
                f"its device settings were NOT sent -- the template landed "
                f"and the rest is owed. Run this again when it is "
                f"reachable; the send is safe to repeat.", level="warn")
            return False
        if not await self._publish(record, configuration):
            await self._io.write(
                f"  {record['name']} came back but the settings could not "
                f"be delivered -- run this again; the send is safe to "
                f"repeat.", level="warn")
            return False
        await self._io.write("  Sent, in both halves. The wiring is live.")
        return True

    async def _edit_by_questions(
            self, name: str,
            plan: "cat.BoardPlan") -> Optional["cat.BoardPlan"]:
        """The pin screen's job, as questions -- for clients without one.
        The CLI used to be told "use the phone app" here, which for a board
        sitting on the bench next to the keyboard was not advice, it was a
        wall (Augusta, 2026-08-24). Returns the edited plan, or None when
        nothing changed or the edit was cancelled -- cancelling DISCARDS,
        and says so.
        """
        dirty = False
        while True:
            rows = []
            for i, item in enumerate(plan.items):
                pins = ", ".join(f"{k} GPIO{v}"
                                 for k, v in sorted(item.pins.items()))
                rows.append({"value": f"drop:{i}",
                             "label": f"Remove {item.name}",
                             "sub": f"{cat.BY_ID[item.type_id].label} -- "
                                    f"{pins}"})
            rows.append({"value": "add", "label": "Add a device",
                         "sub": "the device first, then its pins"})
            rows.append({"value": "done",
                         "label": ("Done -- apply this" if dirty
                                   else "Done -- nothing changed"),
                         "sub": ""})
            picked = await self._io.choose(
                f"Editing {name} ({len(plan.items)} device(s))", rows)
            if not picked:
                if dirty:
                    await self._io.write("  Changes discarded.")
                return None
            if picked[0] == "done":
                return plan if dirty else None
            if picked[0] == "add":
                had = len(plan.items)
                await self._add_one(plan)
                if len(plan.items) != had:
                    dirty = True
                continue
            index = int(picked[0].partition(":")[2])
            gone = plan.items[index].name
            # BoardPlan.remove renumbers from scratch, exactly as add
            # does -- the hand-rolled rebuild that used to live here was
            # the same loop with more places to drift (audit, 2026-08-25).
            plan.remove(gone)
            dirty = True
            await self._io.write(f"  {gone} removed from the plan.")

    async def edit_board(self, slug: str,
                         start_items: Optional[List[Dict[str, Any]]] = None
                         ) -> Optional[Dict[str, Any]]:
        """Change what is wired to a board, over the air where it can be.
        Returns {"name", "gone", "added", "changed"} on success, or None.

        SEND BEFORE SAVE -- for a board that has been INSTALLED. The other
        order leaves the hub describing wiring the board does not have. A
        board that was never flashed has nothing to push to: its chip has
        never held any wiring, so the edit saves directly and the install
        carries it.

        `start_items` opens the pin screen on a SAVED LAYOUT instead of the
        board's current wiring ("Use on a board" in Saved layouts). Nothing
        is applied until the person saves, so it is an edit like any other
        -- same push, same bookkeeping, and the same refusals for a pin the
        board cannot take.

        A PUSH THAT FAILS SAVES THE LAYOUT, as a saved layout. `items`
        stays what the chip actually holds (so the rule above is not
        bent), and what was laid out is kept in Saved layouts under the
        board's name -- laying out a board takes minutes, and an
        unreachable board used to throw that away on every attempt (her
        words, 2026-09-09: "I have overdone this pin selection"). It is the
        owner's to delete, never the hub's.
        """
        record = self._read_board(slug)
        if record is None:
            await self._io.write(f"  No board called {slug!r}.", level="warn")
            return None
        name = record.get("name", slug)
        # ASK THE BOARD ITS FACTS FIRST. A record whose
        # `psram` is still None -- a board whose install ran against a chip
        # that could not answer `Status 4` (safeboot, 2026-09-10) -- makes
        # add() REFUSE anything on GPIO16/17 ("this board has not said
        # yet"), so the pin screen drops that device and a Save here would
        # send a template WITHOUT it: Kitchen's power meter on GPIO17,
        # silently gone from the wiring it was about to be given back.
        # One `Status 4` and one `Gpio 255` over the broker settle it; a
        # board that does not answer leaves the record as it was.
        if record.get("flashed"):
            try:
                await self._learn_board_facts(record)
            except Exception as exc:               # noqa: BLE001
                event("BOARD", f"could not ask {name} about itself: {exc}")
            record = self._read_board(slug) or record
        self.migrate_drafts()
        record = self._read_board(slug) or record
        before = record.get("items", [])
        draft = list(start_items) if start_items else []
        if draft:
            await self._io.write(
                "  Starting from the saved layout -- nothing changes on "
                "the board until you save.")
        laid_out = await self._io.pin_map({
            "board_name": name,
            "catalogue": cat.describe(self._board_of(record)),
            "items": draft or before,
            # Pins this module keeps for itself -- a WROVER's RAM or a PICO's
            # internal flash, an S3's octal PSRAM. Empty when known free;
            # add() is the enforcement either way.
            # Unknown (None) is treated as owned, exactly as BoardPlan.add
            # does, so the phone never offers a pin the hub then refuses.
            "pins_owned_by_module": self._module_pins(record),
            # What the BOARD said its pins were at first flash. Empty = never
            # asked.
            "pins_reported": record.get("pins_reported") or [],
        })
        if laid_out is not None and laid_out.get("cancelled"):
            # What was being laid out is not thrown away: if it differs from
            # what the board has, it is kept in Saved layouts.
            if self._clean_layout_items(laid_out.get("draft") or []) \
                    != self._clean_layout_items(before):
                kept = self._remember_layout(
                    f"{name} (unfinished edit)", self._board_of(record),
                    laid_out.get("draft") or [],
                    f"an edit of {name} that was not finished",
                    state="unfinished", for_board=slug)
                if kept:
                    await self._io.write(
                        "  Left as it was. What you had placed is kept in "
                        "Notifications -- Continue picks it up.")
                    return None
            await self._io.write("  Left as it was.")
            return None
        plan = cat.BoardPlan(name,
                             owns_psram_pins=self._owns_psram_pins(record),
                             board=self._board_of(record))
        if laid_out is not None:
            for note in self._apply(plan, laid_out.get("items") or [],
                                    from_phone=True):
                await self._io.write(f"  ! {note}", level="warn")
        else:
            # No pin screen on this client -- the question editor, seeded
            # with what the record already holds (or the draft, if one was
            # left by a push that failed).
            for note in self._apply(plan, draft or before):
                await self._io.write(f"  ! {note}", level="warn")
            edited = await self._edit_by_questions(name, plan)
            if edited is None:
                await self._io.write("  Left as it was.")
                return None
            plan = edited
        if not plan.items:
            await self._io.write(
                "  That would leave the board with nothing on it. If that is "
                "what you want, retire it instead -- same outcome, and it "
                "cleans up after itself.", level="warn")
            return None
        if record.get("flashed"):
            if not await self._send_plan(record, plan):
                # THE AIR PATH ALREADY SAID WHY IT FAILED (off, refused, no
                # broker). remove_item has always offered the cable here;
                # this is the same courtesy for an edit, and the same reason
                # it was missing is worth naming -- editing a board's wiring
                # is exactly the situation a network migration puts someone
                # in, cable in hand, and this flow sent them away instead
                # (found 2026-09-09, chasing "why didn't it push over cable
                # -- the board's right here").
                picked = await self._io.choose(
                    f"{name} is not reachable over the air. Send its new "
                    f"wiring over the cable instead?",
                    [{"value": "cable", "label": "Plug it in now",
                      "sub": "USB into this hub -- the new wiring goes over "
                             "the wire"},
                     {"value": "later", "label": "Later",
                      "sub": "the board stays as it is; this wiring is "
                             "kept in Notifications until it is sent"}])
                if not (picked and picked[0] == "cable"
                        and await self._push_plan_over_cable(plan)):
                    # The chip still holds `items`; what was laid out goes
                    # to Saved layouts, never in its place.
                    self.save_layout(
                        f"{name} (not sent)", self._board_of(record),
                        self._record_items(plan),
                        source=f"an edit of {name} that never reached it",
                        state="not_sent", **self._pending_for(slug))
                    await self._io.write(
                        "  Nothing reached the board, so it and this hub "
                        "still agree on what it has. The new wiring is kept "
                        "in Notifications -- Continue sends it when the "
                        "board is reachable.", level="warn")
                    return None
                await self._io.write(
                    f"  Sent over the cable. {name} is rebooting to apply "
                    f"it.")
        else:
            await self._io.write(
                "  This board is not installed yet, so there is nothing to "
                "push -- the new wiring is saved and goes on at install.")
        record["items"] = self._record_items(plan)
        self._write_board(record)
        before_names = {i.get("name") for i in before}
        after_names = {i.name for i in plan.items}
        # REWIRED: a device that kept its name but not its part or its
        # pins. It is registered already, and its registration may now be
        # wrong in kind (a relay became a dimmer) or merely in need of a
        # look; the rewire question offers it back for setting up along
        # with the new ones, and leaves every untouched device alone
        # (2026-09-10: "only the affected devices").
        before_by_name = {i.get("name"): i for i in before if isinstance(i, dict)}
        changed = sorted(
            i.name for i in plan.items
            if i.name in before_by_name and (
                before_by_name[i.name].get("type_id") != i.type_id
                or dict(before_by_name[i.name].get("pins") or {}) != dict(i.pins or {})))
        return {"name": name,
                "gone": sorted(before_names - after_names),
                "added": sorted(after_names - before_names),
                "changed": changed}

    async def decommission(self, slug: str) -> bool:
        """Tell a board to forget everything and start again blank.
        `Reset 1` clears its settings and restarts it into its own set-up
        access point. The firmware stays, so it is ready for another project.
        Sent BEFORE the record is deleted: the credentials it signs in with
        live in that record.

        TOLD IS NOT WIPED. A QoS-1 publish proves the BROKER holds the
        message; the board gets it when the broker forwards it -- and the
        retire flow's very next act used to be revoking the account and
        RESTARTING that broker, one second later, killing the delivery it
        was still holding. Bedroom kept every setting while the log said
        it had been wiped (2026-08-24). So this now WAITS for the proof: a
        board that resets drops its connection, and the broker publishes
        its LWT as Offline. Seeing that is knowing; the wait itself is
        also the grace the delivery needed.
        """
        record = self._read_board(slug)
        if record is None:
            return False
        if not await self._publish(record, ["Reset 1"]):
            return False
        heard = await self._mqtt_listen(
            record, [f"tele/{record['topic']}/LWT"],
            timeout=12.0, want="ffline")
        if not any("offline" in line.lower() for line in heard):
            await self._io.write(
                "  The wipe was SENT but the board never dropped off the "
                "broker, so it may not have received it. If it comes back "
                "with its old settings, type `Reset 1` in its own web "
                "console (it still serves its page), or erase it over the "
                "cable.", level="warn")
        event("ESP", f"{slug!r} told to reset to factory settings")
        return True

    # ── building firmware (build_firmware.py, cloud_build.py) ───────────

    def _shell_note(self) -> str:
        return getattr(getattr(self, "_shell", None), "failure_note",
                       None) or ""

    def _refused(self, note: str) -> None:
        """Mark this flow as FAILED with its reason, for the result frame."""
        shell = getattr(self, "_shell", None)
        if shell is not None:
            shell.failure_note = note

    def _build_engine(self):
        """Late import: the door must not cost anything until opened.
        Returns None when the engine FILE is absent -- the caller says so
        in a sentence. It used to raise ImportError straight through the
        menu, killing Add device whole (field test 2026-08-25: the bench
        had every file of the door except build_firmware.py itself)."""
        try:
            from . import build_firmware as engine
        except ImportError:
            return None
        return engine

    # ── GitHub builds (esp_firmware/cloud_build.py) ──────────────────────

    async def _github_session(self, fresh: bool = False):
        """A checked cloud_build.GitHub, asking for what is missing (the
        fork, the token) and saving each once GitHub has accepted it.
        None = not usable, the reason in the failure note. `fresh` asks
        for a new token even when one is saved (Settings: change it)."""
        from . import cloud_build as cb
        token, repo = cb.settings()
        typed_token = typed_repo = False
        if not repo:
            await self._io.write(
                "  Which GitHub repository is your Tasmota fork -- the one "
                "with the 'homevoice' branch? Its address works too "
                "(github.com/you/Tasmota).")
            while not repo:
                answer = (await self._io.ainput(
                    "  Your fork (owner/name): ", patient=True)).strip()
                if not answer or means_cancel(answer):
                    raise CancelledByUser()
                repo = cb.normal_repo(answer)
                if not repo:
                    await self._io.write("  That is not owner/name -- for "
                                         "example augusta/Tasmota.", level="warn")
            typed_repo = True
        if fresh or not token:
            await self._io.write(f"  A GitHub token lets this hub build on "
                                 f"your fork. {cb.TOKEN_HELP}")
            answer = (await self._io.ainput(
                "  Paste the token: ", patient=True, secret=True)).strip()
            if not answer or means_cancel(answer):
                raise CancelledByUser()
            if not cb.looks_like_token(answer):
                await self._io.write("  That does not look like a GitHub "
                                     "token (they start ghp_ or github_pat_) "
                                     "-- asking GitHub anyway.", level="warn")
            token, typed_token = answer, True
        gh = cb.GitHub(token, repo)
        await self._io.write(f"  Checking {repo} on GitHub...")
        checks = await asyncio.to_thread(gh.check)
        for what, ok, detail in checks:
            await self._io.write(f"  [{'ok' if ok else ' X'}] {what}: {detail}",
                                 level="info" if ok else "warn")
        passed = {what for what, ok, _ in checks if ok}
        # Kept as far as GitHub agreed: a good token is not asked for again
        # because the fork's workflow was out of date.
        if typed_token and "token" in passed:
            cb.save_token(token)
        if "token" in passed and gh.expires:
            cb.save_expiry(gh.expires)      # as GitHub says, every check
        if typed_repo and "fork" in passed:
            cb.save_repo(repo)
        failed = [detail for _, ok, detail in checks if not ok]
        if failed:
            self._refused(failed[0])
            return None
        return gh

    async def _github_run(self, gh, engine, branch: str, boards, request: str):
        """Start the workflow for `boards`, wait for it, fetch and install
        each family's images. (built, failed) as the local build has them."""
        from . import cloud_build as cb
        envs = [b.pio_env for b in boards]
        run_id = await asyncio.to_thread(gh.dispatch, branch, envs, request)
        tries = 0
        while run_id is None:
            tries += 1
            if tries > 12:
                raise cb.CloudError(
                    "GitHub accepted the build but its run never appeared. "
                    "Look at the Actions tab of your fork; when it finishes, "
                    "Settings -> GitHub builds -> Download the latest build "
                    "fetches it.")
            await _cloud_sleep(10)
            found = await asyncio.to_thread(gh.find_run, branch, request)
            run_id = found.get("id") if found else None
        run = await asyncio.to_thread(gh.run, run_id)
        await self._io.write(f"  Building on GitHub ({len(envs)} "
                             f"famil{'y' if len(envs) == 1 else 'ies'}): "
                             f"{run.get('html_url', '')}")
        await self._io.write("  This takes 10-40 minutes. It keeps going on "
                             "GitHub if you leave this screen; Settings -> "
                             "GitHub builds -> Download the latest build "
                             "fetches it later.")
        started = time.time()
        said, said_at = "", started
        while run.get("status") != "completed":
            if time.time() - started > 3 * 3600:
                raise cb.CloudError(
                    f"The GitHub build is still not finished after 3 hours "
                    f"({run.get('html_url', '')}). Settings -> GitHub builds "
                    f"-> Download the latest build fetches it when it is.")
            await _cloud_sleep(cb.wait_seconds(started))
            run = await asyncio.to_thread(gh.run, run_id)
            jobs = await asyncio.to_thread(gh.jobs, run_id)
            done = sorted({cb.job_family(j, envs) for j in jobs
                           if j.get("status") == "completed"} - {""})
            line = (f"{len(done)} of {len(envs)} finished"
                    + (f" ({', '.join(done)})" if done else ""))
            minutes = int((time.time() - started) // 60)
            if line != said or time.time() - said_at > 180:
                await self._io.write(f"  GitHub: {line} -- {minutes} min")
                said, said_at = line, time.time()
        return await self._install_from_run(gh, engine, run, boards)

    async def _install_from_run(self, gh, engine, run, boards):
        """Each family's artifact from a finished run: downloaded, checked
        to be that family's images, installed beside the others."""
        import tempfile
        from . import cloud_build as cb
        run_id = run["id"]
        jobs = await asyncio.to_thread(gh.jobs, run_id)
        artifacts = await asyncio.to_thread(gh.artifacts, run_id)
        by_name = {a.get("name"): a for a in artifacts if not a.get("expired")}
        envs = [b.pio_env for b in boards]
        verdict = {cb.job_family(j, envs): j.get("conclusion") for j in jobs}
        built: List[Tuple[Any, List[Path]]] = []
        failed: List[Tuple[Any, str]] = []
        dest = Path(_config.FIRMWARE_DIR)
        for board in boards:
            artifact = by_name.get(board.pio_env)
            if artifact is None:
                why = (f"GitHub made no images (its job: "
                       f"{verdict.get(board.pio_env) or 'did not run'}) -- "
                       f"the run's page says why: {run.get('html_url', '')}")
                failed.append((board, why))
                if verdict.get(board.pio_env):
                    await self._io.write(f"  Not installed: "
                                         f"{board.family_label}: {why}",
                                         level="warn")
                continue
            try:
                blob = await asyncio.to_thread(gh.download, artifact["id"])
                images = cb.unpack(blob, board)
                problem = self._image_problem(images, board, engine)
                if problem:
                    raise cb.CloudError(problem)
                with tempfile.TemporaryDirectory() as tmp:
                    folder = Path(tmp)
                    (folder / board.firmware).write_bytes(images["factory"])
                    (folder / board.ota_name).write_bytes(images["app"])
                    installed = engine.install_artifacts(folder, dest, board)
            except (cb.CloudError, engine.BuildError, OSError) as exc:
                failed.append((board, str(exc)))
                await self._io.write(f"  Not installed: {board.family_label}: "
                                     f"{exc}", level="warn")
                continue
            built.append((board, installed))
            for target in installed:
                await self._io.write(f"  Installed: {target}")
        return built, failed

    @staticmethod
    def _image_problem(images: Dict[str, bytes], board, engine) -> str:
        """"" when both images are this family's firmware, else why not --
        the same checks Install firmware on hub makes."""
        from hub.ota_server import cut_app_image, esp_image_length
        try:
            app_of_factory = cut_app_image(images["factory"])
            esp_image_length(images["app"])
        except (ValueError, TypeError) as exc:
            return f"{board.firmware} / {board.ota_name} are not usable firmware ({exc})."
        for name, app in ((board.firmware, app_of_factory),
                          (board.ota_name, images["app"])):
            problem = engine.image_chip_problem(app, board)
            if problem:
                return f"{name}: {problem}"
        return ""

    async def menu_github_builds(self) -> bool:
        """Settings -> GitHub builds. The token and the fork, a check that
        they work, and the standard firmware built or fetched without
        opening GitHub at all."""
        from . import cloud_build as cb
        await self._io.write("\n--- GITHUB BUILDS ---")
        while True:
            token, repo = cb.settings()
            when = cb.expiry_words(cb.token_expiry()) if token else ""
            await self._io.write(
                f"  Fork:  {repo or 'not set'}\n"
                f"  Token: {'saved on this hub' if token else 'not set'}"
                + (f" -- {when}" if when else ""))
            rows = [{"value": "check", "label": "Check it works",
                     "sub": "asks GitHub: token, fork, branch, workflow"}
                    if token and repo else
                    {"value": "setup", "label": "Set it up",
                     "sub": "your fork's name, then a token"}]
            if token and repo:
                rows += [
                    {"value": "standard", "label": "Build the standard firmware",
                     "sub": "every family, from your fork's homevoice "
                            "branch; installed here when done"},
                    {"value": "latest", "label": "Download the latest build",
                     "sub": "the newest finished run of the standard firmware"}]
            rows += [{"value": "token", "label": "Change the token",
                      "sub": "paste a new one"},
                     {"value": "fork", "label": "Change the fork",
                      "sub": repo or "owner/name"}]
            if token:
                rows.append({"value": "forget", "label": "Remove the token",
                             "sub": "this hub stops building on GitHub"})
            rows.append({"value": "done", "label": "Done", "sub": ""})
            picked = await self._io.choose("GitHub builds", rows)
            choice = picked[0] if picked else "done"
            if choice == "done":
                return True
            try:
                if choice in ("setup", "check"):
                    if await self._github_session() is not None:
                        await self._io.write("  Ready: this hub can build on GitHub.")
                elif choice == "token":
                    if await self._github_session(fresh=True) is not None:
                        await self._io.write("  Token saved.")
                elif choice == "fork":
                    cb.save_repo(None)
                    if await self._github_session() is not None:
                        await self._io.write("  Fork saved.")
                elif choice == "forget":
                    cb.save_token(None)
                    cb.save_expiry(None)
                    await self._io.write("  Token removed from this hub. (It "
                                         "still exists on GitHub -- delete it "
                                         "there too if it is no longer needed.)")
                elif choice == "standard":
                    await self._github_standard(cb)
                elif choice == "latest":
                    await self._github_latest(cb)
            except cb.CloudError as exc:
                self._refused(str(exc))
                await self._io.write(f"  {exc}", level="warn")
            if self._shell_note():
                await self._io.write(f"  {self._shell_note()}", level="warn")
                self._refused("")

    async def _github_standard(self, cb) -> None:
        engine = self._build_engine()
        gh = await self._github_session()
        if engine is None or gh is None:
            return
        built, failed = await self._github_run(
            gh, engine, cb.BASE_BRANCH, list(engine.ALL_TARGETS),
            cb.request_id())
        await self._say_fetched(engine, built, failed)

    async def _github_latest(self, cb) -> None:
        engine = self._build_engine()
        gh = await self._github_session()
        if engine is None or gh is None:
            return
        runs = []
        run = await asyncio.to_thread(gh.latest_run, cb.BASE_BRANCH)
        if run:
            runs.append(run)
        if not runs:
            await self._io.write("  No finished Omniscience build on your fork "
                                 "yet.", level="warn")
            return
        picked = await self._io.choose("Which build?", [
            {"value": str(r["id"]),
             "label": "Standard firmware",
             "sub": f"{str(r.get('created_at', ''))[:16].replace('T', ' ')} UTC, "
                    f"{r.get('conclusion', '')}"} for r in runs])
        if not picked:
            raise CancelledByUser()
        run = next(r for r in runs if str(r["id"]) == picked[0])
        built, failed = await self._install_from_run(
            gh, engine, run, list(engine.ALL_TARGETS))
        await self._say_fetched(engine, built, failed, quiet_missing=True)

    async def _say_fetched(self, engine, built, failed,
                           quiet_missing: bool = False) -> None:
        """One sentence for Settings: what arrived, what did not."""
        if quiet_missing:
            failed = [(b, why) for b, why in failed
                      if "did not run" not in why]
        if built:
            note = "Installed for " + engine.family_names([b for b, _ in built])
            if failed:
                note += ("; NOT for " + engine.family_names([b for b, _ in failed])
                         + " (the reasons are above)")
            await self._io.write(f"  {note}.")
            event("ESP", f"GitHub firmware: {note}")
        else:
            self._refused("Nothing was installed -- " + "; ".join(
                f"{b.family_label}: {why}" for b, why in failed)
                if failed else "Nothing was installed.")

    async def menu_firmware_from_phone(self) -> bool:
        """Firmware built elsewhere, carried here by the phone: the factory
        image (new boards, over the cable) and/or the app image (running
        boards, over the air), of any ESP32 family -- each file by its
        family's own name (tasmota32c3.factory.bin, tasmota32c3.bin). Each
        is checked to BE an ESP32 image of its kind, for that family's chip,
        before anything is replaced. Unknown names are refused."""
        from hub.ota_server import cut_app_image, describe, esp_image_length
        await self._io.write("\n--- INSTALL FIRMWARE FROM PHONE ---")
        if not getattr(self._io, "can_send_files", False):
            self._shell.failure_note = ("This needs the phone app -- copy the "
                                        "files into esp_firmware/firmware/ "
                                        "instead.")
            return False
        engine = self._build_engine()
        # The names come from the board profiles; without the engine file
        # the classic pair is still accepted, as before families.
        names: Dict[str, Tuple[Any, str]] = (
            engine.firmware_names() if engine is not None else
            {chips.DEFAULT.firmware: (chips.DEFAULT, "factory"),
             chips.DEFAULT.ota_name: (chips.DEFAULT, "app")})
        # ONE FAMILY PER HAND-OVER: a family's two images fit one frame,
        # several families' do not -- so which one is asked first, and the
        # phone sends only that family's files.
        families: Dict[str, Any] = {}
        for board, _kind in names.values():
            families.setdefault(board.family, board)
        order = [chips.DEFAULT.family] + sorted(f for f in families
                                                if f != chips.DEFAULT.family)
        picked = await self._io.choose(
            "Firmware for which boards?",
            [{"value": fam, "label": families[fam].family_label,
              "sub": f"{families[fam].firmware} and/or "
                     f"{families[fam].ota_name}"}
             for fam in order if fam in families])
        if not picked:
            raise CancelledByUser()
        family = families[picked[0]]
        files = await self._io.pick_files(
            f"Pick {family.firmware} (for new boards) and/or "
            f"{family.ota_name} (for running boards).",
            purpose="firmware", accept=(".bin",), max_bytes=_PHONE_MAX_BYTES,
            family=family.family)
        names = {n: v for n, v in names.items()
                 if v[0].family == family.family}

        def factory_for(board) -> Path:
            finder = getattr(_config, "firmware_path_for", None)
            if board.family == chips.DEFAULT.family or finder is None:
                base = Path(_config.TASMOTA_FIRMWARE_PATH)
                return (base if board.family == chips.DEFAULT.family
                        else base.parent / board.firmware)
            return Path(finder(board))
        plan: List[Tuple[Path, bytes]] = []
        kinds: Dict[Path, Tuple[Any, str]] = {}
        for entry in files:
            name = Path(str(entry.get("name", ""))).name
            known = names.get(name)
            if known is None:
                self._shell.failure_note = (
                    f"{name} is not a firmware name this hub knows. Send "
                    f"tasmota32<family>.factory.bin and/or "
                    f"tasmota32<family>.bin, as the build named them: "
                    + ", ".join(sorted(names)) + ". Nothing was replaced.")
                return False
            board, kind = known
            try:
                data = base64.b64decode(str(entry.get("data", "")), validate=True)
                if kind == "factory":
                    app = cut_app_image(data)
                    target = factory_for(board)
                else:
                    esp_image_length(data)
                    app = data
                    target = factory_for(board).parent / board.ota_name
                problem = (engine.image_chip_problem(app, board)
                           if engine is not None else "")
                if problem:
                    raise ValueError(problem)
            except (ValueError, TypeError) as exc:
                self._shell.failure_note = (f"{name} is not usable firmware "
                                            f"({exc}). Nothing was replaced.")
                return False
            plan.append((target, data))
            kinds[target] = (board, kind)
            await self._io.write(f"  {name}: {describe(app)}")
        if not plan or len({p for p, _ in plan}) != len(plan):
            self._shell.failure_note = ("Send one factory image and/or one "
                                        "app image per family. Nothing was "
                                        "replaced.")
            return False
        picked = await self._io.choose(
            "Replace the firmware this hub gives its boards?",
            [{"value": "yes", "label": "Replace it", "sub": ", ".join(
                f"{kinds[p][0].family_label}: " + (
                    "new boards (cable)" if kinds[p][1] == "factory" else
                    "running boards (over the air)") for p, _ in plan)},
             {"value": "no", "label": "Keep the current one", "sub": ""}])
        if not picked or picked[0] != "yes":
            raise CancelledByUser()
        for path, data in plan:
            def _write(path=path, data=data) -> None:
                path.parent.mkdir(parents=True, exist_ok=True)
                tmp = path.with_suffix(".tmp")
                tmp.write_bytes(data)
                os.replace(tmp, path)
            await asyncio.to_thread(_write)
            await self._io.write(f"  Installed: {path}")
        event("ESP", "firmware installed from phone: "
              + ", ".join(p.name for p, _ in plan))
        # (by family id: a Board holds dicts and is not hashable)
        by_family = {kinds[p][0].family: kinds[p][0] for p, _ in plan}
        for family, board in by_family.items():
            if all(kinds[p][1] == "app" for p, _ in plan
                   if kinds[p][0].family == family):
                await self._io.write(
                    f"  Only the over-the-air image of {board.family_label} "
                    f"changed: its boards set up over the cable still get "
                    f"the old factory image.", level="warn")
        return True

    # ── taking ONE device off a running board ───────────────────────────
    #
    # Remove device used to stop at the registry: the entry here went, the
    # Home Assistant entry could go, and the CHIP kept its template -- still
    # driving the pins, still publishing, as if nothing had happened. The
    # menu's own docstring called that a design decision. It was the bug:
    # removing a thing means the thing stops, and the layer where it
    # actually runs is the one layer the flow never touched.

    async def _template_took(self, port: str,
                             plan: "cat.BoardPlan") -> Optional[bool]:
        """Did the chip KEEP the template it was just sent?
        Asks `Template` back over the cable and compares the echoed GPIO
        array number-for-number. None = could not be asked; callers report
        that as unverified rather than as either answer."""
        try:
            spoken = await self._talk_to_board(port, ["Template"])
        except Exception:                          # noqa: BLE001
            return None
        want = list(plan.template()["GPIO"])
        for line in spoken:
            found = re.search(r'"GPIO"\s*:\s*\[([0-9,\s]*)\]', line)
            if found:
                got = [int(x) for x in found.group(1).split(",") if x.strip()]
                return got == want
        return None

    async def _push_plan_over_cable(self, plan: "cat.BoardPlan") -> bool:
        """A plan onto the board on the cable, when the air path failed --
        shared by edit_board and remove_item. True when every half landed.

        The template travels first and is READ BACK; a different echo stops
        here, before the chip is told to apply it. TWO PHASES ON THE CABLE
        TOO: what is left of activation is Module 0 alone, the restart it
        causes abandons anything queued behind it, so the sleep covers the
        going-down and _talk_to_board's own console probe covers the
        coming-back before the configuration half is typed in (audit,
        2026-08-25)."""
        port = await self._pick_port()
        if not port or not await self._send_over_serial(
                port, [plan.template_command()], plan.board):
            return False
        took = await self._template_took(port, plan)
        if took is False:
            await self._io.write(
                "  The board echoed a DIFFERENT template than the one sent, "
                "so it was not told to apply it. Nothing changed on the "
                "chip.", level="warn")
            return False
        if took is None:
            await self._io.write(
                "  Could not read the template back -- sending the rest "
                "anyway; check the board's console if the device keeps "
                "running.", level="warn")
        if await self._psram_unknown_stop(plan):
            return False
        activation, configuration = plan.backlogs()
        if not await self._send_over_serial(port, activation[1:], plan.board):
            return False
        await asyncio.sleep(6.0)
        return not configuration or await self._send_over_serial(
            port, configuration, plan.board)

    async def remove_item(self, slug: str,
                          device_suffix: str) -> Optional[str]:
        """Strip one device off a board -- the chip included.

        Returns "air" (pushed over MQTT), "cable" (pushed over USB),
        "later" (record updated, chip still carrying the old wiring), or
        None when this board has no item by that name -- a device id like
        kitchen.lamp can belong to a manually-added device in the kitchen
        ROOM, and only a name match against the board's own items proves
        it lives on the board.

        SAVE RULES. On a successful push the record is saved after the
        send, as everywhere. On "later" the record is saved TOO, which is
        the opposite of edit_board's refusal -- deliberately. By the time
        this runs the registry entry is already gone, so a record still
        listing the item would re-register it at the next install: the
        flow would hand back the exact device that was just removed. The
        record now carries the INTENT, the divergence is said out loud,
        and every later push (install, edit, this hub's next contact)
        converges the chip onto it.
        """
        record = self._read_board(slug)
        if record is None:
            return None
        items = [i for i in (record.get("items") or []) if isinstance(i, dict)]
        wanted = self._snake_name(device_suffix)
        gone = next((i for i in items
                     if self._snake_name(i.get("name")) == wanted), None)
        if gone is None:
            return None
        name = gone.get("name") or device_suffix
        remaining = [i for i in items if i is not gone]
        board_name = record.get("name", slug)

        plan = cat.BoardPlan(board_name,
                             owns_psram_pins=self._owns_psram_pins(record),
                             board=self._board_of(record))
        for note in self._apply(plan, remaining):
            await self._io.write(f"  ! {note}", level="warn")

        await self._io.write(
            f"  {name} also lives on the {board_name} board -- taking its "
            f"wiring off the chip so the pins stop.")
        if not remaining:
            await self._io.write(
                f"  That leaves {board_name} with nothing on it. It stays "
                f"provisioned and connected; retire it from Remove device "
                f"when it should go completely.")

        def _saved() -> None:
            record["items"] = remaining
            # A SAVED LAYOUT IS NOT TOUCHED. It belongs to no board and
            # controls nothing; taking a device off this board does not
            # edit the plans the owner has kept for other boards.
            self._write_board(record)

        if await self._send_plan(record, plan):
            _saved()
            event("ESP", f"{slug}: {name!r} stripped over the air")
            await self._io.write(
                f"  Done. The board is rebooting without {name}.")
            return "air"

        # The air path already said WHY it failed (off, refused, no broker).
        picked = await self._io.choose(
            f"Take {name} off the board over the cable instead?",
            [{"value": "cable", "label": "Plug it in now",
              "sub": "USB into this hub -- the corrected wiring goes over "
                     "the wire"},
             {"value": "later", "label": "Later",
              "sub": "saved here; the next install or edit pushes it"}])
        if picked and picked[0] == "cable" \
                and await self._push_plan_over_cable(plan):
            _saved()
            event("ESP", f"{slug}: {name!r} stripped over the cable")
            await self._io.write(
                f"  Done. The board is rebooting without {name}.")
            return "cable"

        _saved()
        await self._io.write(
            f"  Saved here: the record no longer lists {name}, but the CHIP "
            f"still carries its wiring and those pins keep doing their old "
            f"job. The next time this hub pushes wiring to {board_name} -- "
            f"Set up an ESP board over the cable, or an edit -- the removal "
            f"lands with it.", level="warn")
        return "later"

    # ── serial ──────────────────────────────────────────────────────────

    @staticmethod
    def _serial_ports() -> Optional[List[Dict[str, str]]]:
        """None means pyserial itself is not on this hub -- a fact about
        THIS MACHINE, not about what is or is not plugged into it. Folding
        that into an empty list read as "no port found" and sent someone
        checking their cable for a problem that was never there: a board
        plugged in, over a cable that carries data, and reported not found
        anyway (2026-09-09). `_send_over_serial` already tells the two
        apart for the same reason; this is the other place that needed to.
        """
        try:
            from serial.tools import list_ports
        except ImportError:
            return None
        return [{"value": port.device, "label": port.device,
                 "sub": (port.description or "").strip()}
                for port in list_ports.comports()]

    #: How long a flash waits for a board to be plugged in.
    PORT_WAIT_S = 120.0

    async def _pick_port(self) -> Optional[str]:
        ports = self._serial_ports()
        if ports is None:
            await self._io.write(
                "  pyserial is not installed on this hub (pip install "
                "pyserial), so it cannot see ANY serial port -- plugged in "
                "or not. That is not the same as no board being found.",
                level="warn")
            return None
        if not ports:
            # WAIT FOR IT instead of ending the flow: plugging the board in
            # now is the whole fix, so nothing has to be continued later.
            await self._io.write(
                "  Plug the board into THIS HUB (the Pi itself) with a USB "
                "cable -- waiting for it...")
            # Its OWN window while it waits, saying what it waits for --
            # under "Flashing the board" a wait looked like a stuck flash.
            await self.open_job(
                "Waiting for the board",
                "Plug it into the hub (the Pi itself) with a USB cable.",
                seconds=self.PORT_WAIT_S)
            try:
                deadline = time.monotonic() + self.PORT_WAIT_S
                while not ports and time.monotonic() < deadline:
                    await _port_sleep(2.0)
                    ports = self._serial_ports() or []
            finally:
                await self.close_job()
            if not ports:
                await self._io.write(
                    "  No board appeared on a cable. If it IS plugged in, the "
                    "cable may be a charging-only one (they look the same) "
                    "-- try another.", level="warn")
                return None
        if len(ports) == 1:
            await self._io.write(
                f"  Using {ports[0]['label']} ({ports[0]['sub']}).")
            return ports[0]["value"]
        picked = await self._io.choose("Which port is the board on?", ports)
        return picked[0] if picked else None

    async def _talk_to_board(self, port: str, commands: List[str],
                             console_wait: float = CONSOLE_WAIT) -> List[str]:
        """Open the cable, wait for the console, send, and read the replies.
        The ONE place that holds a conversation with a board over serial.
        Raises TimeoutError when the console never answers -- a real answer,
        not an error to hide.
        """
        import serial
        def drain(link, quiet_for: float, limit: float) -> List[str]:
            """Read until the board stops talking, or `limit` runs out.
            A fixed sleep here was too short and produced the worst possible
            result: the commands DID land, the reply arrived after the port
            closed, and the run reported that nothing was heard. A backlog
            executes at roughly 200ms a command, so replies dribble out over
            seconds. Waiting for QUIET rather than a fixed period is the fix.
            """
            heard: List[str] = []
            # WHOLE LINES ONLY. A read returns whatever bytes have arrived,
            # which routinely ends mid-line (a Template or Status reply is
            # hundreds of bytes); splitting each read on its own turned one
            # JSON reply into two fragments -- neither parseable, a split
            # `"Command":"Unknown"` never matched, and the tail of a split
            # `Password3 <secret>` echo escaped _redact into the log. The
            # unfinished line waits here for the rest of it.
            pending = b""
            last = time.monotonic()
            stop = time.monotonic() + limit
            while time.monotonic() < stop:
                waiting = link.in_waiting
                if waiting:
                    parts = re.split(rb"[\r\n]", pending + link.read(waiting))
                    pending = parts.pop()
                    heard.extend(text for text in (
                        p.decode("utf-8", "replace").strip() for p in parts)
                        if text)
                    last = time.monotonic()
                # QUIET ONLY COUNTS AFTER SOMETHING HAS BEEN SAID. Applied
                # from the start it fires before the first reply arrives.
                elif (heard or pending.strip()) and \
                        time.monotonic() - last >= quiet_for:
                    break
                time.sleep(0.1)
            tail = pending.decode("utf-8", "replace").strip()
            if tail:
                heard.append(tail)
            return heard
        def talk() -> List[str]:
            spoken: List[str] = []
            with serial.Serial(port, 115200, timeout=2) as link:
                # WAIT FOR THE CONSOLE TO ANSWER A COMMAND, not merely for
                # bytes to appear: a booting board supplies boot messages in
                # quantity while its command parser is still not running. A
                # Tasmota console answers `Status 1` with STATUS1 or RESULT.
                deadline = time.monotonic() + console_wait
                ready = False
                while time.monotonic() < deadline and not ready:
                    link.reset_input_buffer()
                    link.write(b"\r\nStatus 1\r\n")
                    link.flush()
                    for line in drain(link, quiet_for=0.6, limit=4.0):
                        if "STATUS1" in line.upper() or "RESULT" in line.upper():
                            ready = True
                            break
                if not ready:
                    raise TimeoutError(
                        "the board never answered on the serial console")
                link.reset_input_buffer()
                for command in commands:
                    self._write_chunked(link, command)
                    spoken.extend(drain(link, quiet_for=1.2, limit=20.0))
            return spoken
        return await asyncio.to_thread(talk)

    @staticmethod
    def _write_chunked(link, command: str, *, size: int = 64,
                       pause: float = 0.03) -> None:
        """Write one command to the board's console in SMALL CHUNKS, with a
        breath between them.

        The ESP32's hardware UART FIFO is 128 bytes, and it only empties
        when the firmware's main loop gets a turn. A Template line is ~220
        bytes; sent in one write() it overflows the FIFO the moment the
        board's loop is briefly busy -- and a board thrashing on a Wi-Fi it
        cannot join is busy for seconds at a stretch. The tail is dropped,
        Tasmota sees a truncated line and answers `Command: Unknown`, and a
        wiring push that genuinely reached the cable is reported as
        rejected. (Field log, 2026-09-09: a Kitchen Template echoed back
        cut off at 55 characters while the board looped on `WIF: Connect
        failed with AP timeout`.)

        64 bytes sits well under the 128-byte FIFO, and 30ms per chunk lets
        even a stalled loop drain it before the next arrives -- ~100ms
        added to a Template, nothing to a short command. Short commands
        (one chunk) behave exactly as a single write did.
        """
        payload = (command + "\r\n").encode()
        for i in range(0, len(payload), size):
            link.write(payload[i:i + size])
            link.flush()
            if pause:
                time.sleep(pause)

    async def _send_over_serial(self, port: str,
                                commands: List[str],
                                board: Any = None) -> bool:
        """Type commands into the board's console, and READ WHAT IT SAYS BACK.
        False when the board REJECTED something, not merely when the cable
        failed -- a command the firmware does not recognise means this hub is
        speaking a dialect that board does not, and should stop the run rather
        than leave a half-configured chip that looks finished.
        """
        try:
            spoken = await self._talk_to_board(port, commands)
        except ImportError:
            await self._io.write(
                "  pyserial is not installed on this hub, so the board cannot "
                "be set up over the cable.", level="warn")
            return False
        except TimeoutError:
            # EXPECTED, not exceptional: a board held in reset, one that did
            # not come up after the flash, or a cable pulled early.
            await self._io.write(
                f"  The board on {port} never answered. Unplug it, plug it "
                f"back in, and try again -- the firmware is already written, "
                f"so it only re-sends the settings. If it stays silent, hold "
                f"the BOOT button while plugging it in.", level="warn")
            return False
        except Exception as exc:                   # noqa: BLE001
            traceback.print_exc()
            await self._io.write(
                f"  Could not talk to the board on {port}: "
                f"{type(exc).__name__}: {exc}", level="warn")
            return False
        for line in spoken:
            event("SERIAL", self._redact(line)[:160])
        rejected = [l for l in spoken if self._CONSOLE_BAD.search(l)]
        if rejected:
            await self._io.write(
                f"  The board refused {len(rejected)} of what it was sent:",
                level="warn")
            for line in rejected[:6]:
                await self._io.write(f"    {self._redact(line)[:140]}",
                                     level="warn")
            # WHICH command was refused decides what it means. A refused
            # SetOptionNNN is a wrong option number for this Tasmota; a
            # refused BACKLOG is something else entirely -- Backlog is a
            # core command every full Tasmota knows, and the one build that
            # does not is the SAFEBOOT image: the small recovery firmware
            # the factory image also carries, which the chip falls back to
            # when the main firmware will not start. It joins Wi-Fi with
            # the saved SSID and looks alive on the console, and refuses
            # nearly everything (2026-09-09: every Backlog "Unknown", hours
            # spent on the network). Say so, and say the way out.
            if any(re.search(r'"Input"\s*:\s*"BACKLOG', l, re.I) for l in rejected):
                await self._io.write(
                    "  This board does not know the Backlog command, and every "
                    "full Tasmota does. It is almost certainly running the "
                    "SAFEBOOT recovery firmware instead of the main one: its "
                    "console answers, it tries the saved Wi-Fi, but it will "
                    "not take settings.", level="warn")
                if await self.heal_safeboot_serial(port, board):
                    await self._io.write(
                        "  It has been told, on this cable, to fetch its "
                        "firmware from this hub over Wi-Fi. Give it a minute "
                        "to download and reboot, then try again. If it comes "
                        "back the same, flash it over the cable -- Devices > "
                        "Add device > a board on the cable.",
                        level="warn")
                else:
                    await self._io.write(
                        "  Fix: flash it again over the cable -- Devices > Add "
                        "device > a board on the cable, or Update device > A "
                        "board.", level="warn")
                return False
            await self._io.write(
                "  That usually means one of the Tasmota option numbers in "
                "config.py is wrong for this firmware version.", level="warn")
            return False
        if not spoken:
            # SILENCE IS A FAILURE. It used to be a shrug, which is how a
            # board ended up with its wiring and none of its network
            # settings: the batch went into a console that was not listening
            # yet. Tasmota answers every command with a RESULT line.
            await self._io.write(
                "  The board did not acknowledge any of that, so none of it "
                "can be assumed to have landed. Try again -- the firmware is "
                "already written, so it only re-sends the settings.",
                level="warn")
            return False
        return True

    async def _settings_took(self, port: str, ssid: str,
                             host: str) -> Optional[bool]:
        """Did the chip actually KEEP what it was just sent?
        Asked after the Restart, so this proves the settings reached flash
        and survived a boot -- not merely that a command parsed.
        ONE FIELD FROM EACH BACKLOG. Checking only the first field of the
        first line verifies the one thing most likely to survive a truncated
        send. None = could not be asked, which callers report as unverified.
        """
        await asyncio.sleep(6.0)          # the chip is mid-reboot
        try:
            spoken = await self._talk_to_board(port, ["SSId1", "MqttHost"])
        except Exception:                          # noqa: BLE001
            return None
        got_ssid = self._field(spoken, "SSId1")
        got_host = self._field(spoken, "MqttHost")
        if got_ssid is None or got_host is None:
            return None
        return got_ssid == ssid and got_host == host

    async def _probe_board_facts_serial(
            self, port: str, board: Optional["chips.Board"] = None
    ) -> Tuple[Optional[bool], Optional[List[int]]]:
        """One conversation, two questions -- PsrMax (read for `board`'s
        family) and the board's own pin listing."""
        try:
            spoken = await self._talk_to_board(port, ["Status 4", "Gpio 255"])
        except Exception:                          # noqa: BLE001
            return None, None
        return self._psram_answer(spoken, board), self._pins_reported(spoken)

    async def _read_identity(self, port: str) -> Optional[Dict[str, str]]:
        """What the chip currently thinks it is, or None if it will not say.
        None covers a blank chip, a board in download mode, and no pyserial.
        In each there is no prior identity to protect.
        """
        try:
            spoken = await self._talk_to_board(port, ["Status"],
                                               console_wait=self.CONSOLE_PEEK)
        except (ImportError, TimeoutError, OSError):
            return None
        except Exception:                          # noqa: BLE001
            traceback.print_exc()
            return None
        for line in spoken:
            start = line.find("{")
            if start < 0:
                continue
            try:
                block = json.loads(line[start:]).get("Status") or {}
            except ValueError:
                continue
            if not isinstance(block, dict):
                continue
            topic = str(block.get("Topic") or "")
            name = str(block.get("DeviceName") or "")
            if topic or name:
                return {"topic": topic, "name": name}
        return None

    async def _check_chip_is_free(self, port: str,
                                  record: Dict[str, Any]) -> Optional[str]:
        """Refuse to overwrite one board's identity with another's in silence.
        Without a rename, the only way to change a name was to create a
        SECOND board and flash it onto the same chip -- two records and two
        broker accounts for one piece of hardware, while Home Assistant (which
        keys on the MAC) kept the original device and ignored the new name.
        The chip knew all along; nobody asked it.
        Returns the slug being displaced, "" to carry on, or None to cancel.
        """
        identity = await self._read_identity(port)
        if identity is None:
            return ""                    # blank chip, or not Tasmota yet
        topic = identity["topic"]
        if not topic or topic == record.get("topic"):
            return ""                    # unclaimed, or already this board
        displaced = next(
            (slug for slug, other in self._boards()
             if other.get("topic") == topic), None)
        here = record.get("name") or record.get("slug") or "this board"
        if displaced is None and topic in self._retired_topics():
            # A BOARD THIS HUB RETIRED: nothing of it is left to protect, so
            # the chip is simply reused (no "overwrite?" for your own old
            # board, 2026-11-02).
            await self._io.write(
                f"  This chip was {identity['name'] or topic!r}, a board "
                f"retired earlier -- it becomes {here} now.")
            return ""
        if displaced is None:
            # ONE STRING, used twice. The warning and the Cancel button must
            # name the chip identically -- her correction on the canvas, and
            # the sharp half of it: a screen that calls the same chip two
            # different things is asking you to decide about something it
            # cannot itself name.
            was = identity["name"] or topic
            await self._io.write(
                f"  This chip is already set up as "
                f"{was!r}, which is not a board this hub "
                f"knows about.", level="warn")
            # SHAPE A (her call, 2026-08-29): the destructive answer is the
            # only ROW; Cancel is the screen's own button, and it NAMES WHAT
            # SURVIVES. "Leave the chip as it is" told nobody anything; the
            # chip's own name is the thing being kept, and `was` is the same
            # string the warning above just used -- one source, so the screen
            # cannot contradict itself about the one fact being decided.
            picked = await self._io.choose(
                f"Overwrite it and make it {here}?",
                [{"value": "yes", "label": f"Overwrite it, make it {here}",
                  "sub": "its current settings are lost"}],
                cancel_label=f"Cancel -- leave the chip as {was!r}")
            return "" if picked and picked[0] == "yes" else None
        other = self._read_board(displaced) or {}
        await self._io.write(
            f"\n  This chip is already set up as "
            f"{other.get('name', displaced)!r}.")
        # The same shape, so the two questions cannot teach opposite habits.
        # They used to disagree: this one listed Cancel LAST and the other
        # listed it FIRST, on one screen with one set of stakes.
        was_called = other.get("name", displaced)
        picked = await self._io.choose(
            f"Move it to {here}?",
            [{"value": "move",
              "label": f"Move it -- it becomes {here}, and "
                       f"{was_called} is retired",
              "sub": "one board, one name, nothing left behind"}],
            cancel_label=f"Cancel -- leave the chip as {was_called!r}")
        if not picked or picked[0] != "move":
            return None
        return displaced

    async def _retire_displaced(self, slug: str) -> None:
        """Clean up the board whose chip has just been taken over.
        The record goes only if the ACCOUNT went. Deleting it anyway leaves a
        broker credential for a board that does not exist and no record that
        it was ever issued -- the exact mess _check_chip_is_free exists to
        prevent.
        """
        record = self._read_board(slug) or {}
        name = record.get("name", slug)
        if not await self.revoke_access(slug):
            await self._io.write(
                f"  {name}'s broker account could NOT be removed, so its "
                f"record is being kept rather than leaving a credential "
                f"nobody can account for. Run "
                f"`mosquitto_passwd -D <file> {record.get('mqtt_user', '?')}` "
                f"and retire it from the menu.", level="warn")
            return
        self.delete_node(slug)
        await self._io.write(
            f"  Retired {name} -- its record and its broker account are gone.")

    # ── flashing ────────────────────────────────────────────────────────

    @staticmethod
    def _esptool() -> List[str]:
        """How to run esptool: through THIS Python when it can import it.
        A pip install --user puts the `esptool` command in ~/.local/bin,
        which a service's PATH does not have -- the hub then said "Could
        not run esptool" on a Pi where `python3 -m esptool` worked
        (2026-11-02)."""
        import importlib.util
        if importlib.util.find_spec("esptool") is not None:
            return [sys.executable, "-m", "esptool"]
        return [shutil.which("esptool") or shutil.which("esptool.py")
                or "esptool"]

    def _firmware(self, board: Optional["chips.Board"] = None
                  ) -> Optional[Path]:
        """The prebuilt image that ships with the hub -- for `board`'s
        family (the classic when none is named). Config first, because
        that is where setup_manager puts it."""
        found = self._firmware_candidates(board)
        return found[0] if found else None

    @staticmethod
    def _no_image_sentence(board: "chips.Board") -> str:
        """Why an install of this family cannot start, and the three ways
        to fix it."""
        return (f"  There is no {board.family_label} firmware on this hub "
                f"({_config.firmware_path_for(board)}), so nothing was "
                f"started. Build it with the GitHub workflow "
                f"(the {board.pio_env} environment), or run setup to download "
                f"Tasmota's stock {board.firmware} -- stock works, but lacks "
                f"the house drivers (RC522, LCD, MPU6050...).")

    @staticmethod
    def _firmware_candidates(board: Optional["chips.Board"] = None
                             ) -> List[Path]:
        """EVERY file the search would consider, best first.

        The search reaches an absolute path in config, then this folder,
        then its firmware/ subfolder, then every parent directory above --
        and it used to announce none of that. Replacing "the firmware" in
        one folder while an older copy sits in another therefore did
        nothing visible and everything real: boards kept getting a build
        nobody meant to keep. The list is returned whole now so the flash
        can name the winner and warn about the rest.

        Another FAMILY has one place only, config.firmware_path_for (its
        own factory image by its profile's name): it never had copies
        scattered around to be found.
        """
        if board is not None and board.family != "esp32":
            path = Path(_config.firmware_path_for(board))
            return [path] if path.is_file() else []
        found: List[Path] = []
        configured = _config.TASMOTA_FIRMWARE_PATH
        if configured and Path(configured).is_file():
            found.append(Path(configured))
        name = _config.TASMOTA_FIRMWARE
        here = Path(__file__).resolve().parent
        for base in (here, here / "firmware", *here.parents):
            candidate = base / name
            if candidate.is_file() and candidate not in found:
                found.append(candidate)
        return found

    #: Tasmota assembles BuildDateTime at RUN time from the compiler's
    #: __DATE__ ("Aug 18 2026") and __TIME__ ("09:39:00"), so a binary
    #: carries the two halves separately and never the joined form.
    _BUILT_DATE = re.compile(
        rb"(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)"
        rb"\s+\d{1,2}\s+20\d{2}")
    _BUILT_TIME = re.compile(rb"(?:[01]\d|2[0-3]):[0-5]\d:[0-5]\d")

    @classmethod
    def _firmware_stamp(cls, path: Optional[Path]) -> str:
        """"built Aug 18 2026 09:39:00", straight out of the file -- the one
        fact that can be compared with a board's own boot line. "" when it
        cannot be read; never a guess."""
        if path is None:
            return ""
        try:
            blob = path.read_bytes()
        except OSError:
            return ""
        date = cls._BUILT_DATE.search(blob)
        time_ = cls._BUILT_TIME.search(blob)
        if not date:
            return ""
        stamp = date.group().decode("ascii", "replace")
        if time_:
            stamp += " " + time_.group().decode("ascii", "replace")
        return stamp

    @staticmethod
    def _chip_mismatch(chip: str, board: "chips.Board") -> str:
        """A sentence when the chip on the cable is not the family the
        person CHOSE for this board; "" when it is. The person decides
        which board it is; the cable double-checks, before anything is
        erased -- in a template position means pin, and one family's
        template (its grounding included) lands on another family's flash
        bus and kills it on every boot."""
        if board.matches_chip(chip):
            return ""
        cable = chips.family_of_chip(chip)
        if chip.upper().startswith("ESP8266"):
            have = "an ESP8266, which is not an ESP32 at all"
        elif cable is not None:
            have = f"an {cable.family_label}"
        else:
            have = f"a chip esptool calls {chip}, which this hub does not know"
        return (f"  You chose an {board.family_label} board but the cable has "
                f"{have} -- nothing was erased. Pick the right board, or "
                f"change this board's type.")

    async def _identify_chip(self, port: str, *, quiet: bool = False,
                             timeout: float = 60.0) -> Optional[str]:
        """Ask esptool what is on the cable, WITHOUT touching flash.
        This used to be scraped from the erase, so an S3 was wiped and then
        told the flash had been stopped before writing anything. An erase is
        a write.

        TWO DIALECTS OF THE SAME ANSWER. esptool v4 announces the chip as
        "Chip is ESP32-D0WD-V3"; v5 restyled its entire log and says
        "Chip type:          ESP32-D0WD-V3". This method matched only the
        v4 wording -- so a bench whose esptool had upgraded identified the
        chip PERFECTLY and was then told to check its cable: the parser
        threw the answer away, quiet=True hid the output that showed the
        successful detection, and a person spent an evening holding BOOT
        buttons for a fault that lived on this machine (the bedroom board,
        2026-08-24). Both wordings are matched now -- and when
        identification still fails, esptool's own last words are SHOWN
        instead of a guess about cables, so the next version of this
        mistake introduces itself.

        esptool's "Features:" line is kept in self._chip_features ("" when
        it said none): an S3's embedded PSRAM is named there. `quiet` keeps
        a failure to itself (the design-time peek, which is only a hint).
        """
        seen: List[str] = []
        said: List[str] = []
        self._chip_features = ""
        def spot(line: str) -> None:
            said.append(line)
            found = re.search(r"Chip (?:is|type:)\s+(\S+)", line)
            if found:
                seen.append(found.group(1))
            features = re.search(r"Features:\s*(.*)", line)
            if features:
                self._chip_features = features.group(1).strip()
        # No --chip: let esptool detect, which is the whole question. And
        # `flash_id` with the underscore on purpose: v4 knows no other
        # spelling, v5 keeps it as a deprecated alias -- the one name both
        # generations accept.
        code = await self._stream(
            self._esptool() + ["--port", port, "--baud", "115200", "flash_id"],
            Path.cwd(), timeout, on_line=spot, quiet=True)
        if code == 0 and seen:
            return seen[0]
        if said and not quiet:
            await self._io.write("  esptool's own words were:", level="warn")
            for line in said[-6:]:
                await self._io.write(f"    {line[:160]}", level="warn")
        return None

    async def open_job(self, text: str, detail: str = "",
                       seconds: Optional[float] = None) -> None:
        """Open the WORKING ROOM, and keep it open until close_job matches.

        THE ROOM HAS TO OPEN BEFORE THE CHATTER, not with it. The app pushes
        /working when a `busy` frame arrives, and the only busy frame in this
        file used to be the one wrapped around esptool -- so everything said
        BEFORE that point ("Using COM7 (USB-SERIAL CH340)", the erase
        warning, the wiring notes) was written while the phone was still on
        the home screen, and landed in the orb's feed. Field report
        2026-08-29: "during flashing the home still shows using com7 port".
        Her instruction: push everything into the working screen, so once the
        upload starts the room is already there.

        COUNTED, because these nest. _flash_node opens the room for the whole
        job and _run_esptool opens one per esptool invocation inside it; if
        each `finally` sent its own {"done": true} the first inner step would
        close the room while the job was still running, dropping the person
        back on home mid-flash. Only the outermost close actually ends it.
        """
        self._busy_depth = getattr(self, "_busy_depth", 0) + 1
        payload: Dict[str, Any] = {"text": text}
        if detail:
            payload["detail"] = detail
        if seconds is not None:
            payload["seconds"] = seconds
        await self._io.send_data("busy", payload)

    async def close_job(self) -> None:
        """Close one level of the working room; the last one takes it down."""
        self._busy_depth = max(0, getattr(self, "_busy_depth", 0) - 1)
        if self._busy_depth == 0:
            await self._io.send_data("busy", {"done": True})

    async def _run_esptool(self, args: List[str], cwd: Path, *,
                           busy: Dict[str, Any]) -> int:
        """Run esptool behind a busy card that always comes down."""
        await self.open_job(busy.get("text", "working…"),
                            busy.get("detail", ""), busy.get("seconds"))
        try:
            return await self._stream(self._esptool() + args, cwd,
                                      self.FLASH_TIMEOUT, quiet=True)
        finally:
            await self.close_job()

    async def _flash_node(self, slug: str) -> Optional[str]:
        """Put the firmware on a board and tell it what it is.
        Returns the board's MQTT topic on success, or None -- which is not
        necessarily a fault, since backing out returns None too.
        EVERY REFUSAL COMES BEFORE THE ERASE. The chip identity and the Wi-Fi
        record are both checked while the board is still untouched; both used
        to refuse after it had been wiped.

        THE ROOM OPENS HERE, at the top, and closes in the finally -- so
        every line this flow writes (the port it chose, the erase, the
        wiring push, esptool's own progress) lands in the working screen's
        log instead of the orb's feed. See open_job.
        """
        # The Wi-Fi first: a question asked inside the job's window sat
        # under it.
        wifi_state = await self._wifi_ready_here()
        if wifi_state == self.WIFI_STOP:
            return None
        await self.open_job(
            "Flashing the board",
            "Choosing the port, erasing, writing, then telling it what it "
            "is. Leave the cable alone until this finishes.")
        try:
            return await self._flash_node_inner(slug, wifi_state)
        finally:
            await self.close_job()

    async def _flash_node_inner(self, slug: str,
                                wifi_state: Optional[str] = None
                                ) -> Optional[str]:
        record = self._read_board(slug)
        if record is None:
            await self._io.write(f"  No board called {slug!r}.", level="warn")
            return None
        # THE BOARD THE PERSON CHOSE decides the image, the esptool chip and
        # the pin table; the cable only double-checks it (below).
        board = self._board_of(record)
        firmware = self._firmware(board)
        if firmware is None:
            await self._io.write(self._no_image_sentence(board), level="warn")
            return None
        self.last_flash_needs_network = False
        if wifi_state is None:
            wifi_state = await self._wifi_ready_here()
        if wifi_state == self.WIFI_STOP:
            return None
        network_later = wifi_state == self.WIFI_LATER
        # BEFORE THE CABLE. A board proved once that this exact firmware file
        # lacks a driver; flashing it again into another board can only
        # produce the same refusal an hour later, after the erase, the
        # write, the reboot and the wiring push. Known-lacking is the only
        # thing that stops here: never-proven stays a normal install.
        plan_now = self._plan_for(slug)
        lacking = self._firmware_lacks(
            self._drivers_needed(plan_now) if plan_now else [], board)
        if lacking:
            await self._io.write(
                f"  Not started. The firmware this hub flashes "
                f"({firmware}) has already been proven not to carry "
                f"{', '.join(lacking)}, which {record['name']}'s wiring "
                f"needs -- so this install would end in the same refusal "
                f"after the erase and the write. Replace that file with the "
                f"build that carries them (same file name), then install "
                f"again.", level="warn")
            return None
        await self._io.write(
            f"\n  Flashing {record['name']}. Plug the board in with a data "
            f"cable. This takes about a minute and is the ONLY time the cable "
            f"is needed.")
        # WHICH FILE, AND WHICH BUILD -- said before it is written, not
        # deduced from a boot log afterwards. A replaced firmware that
        # changed nothing is invisible without this line, and invisible is
        # how the same build went onto board after board.
        stamp = self._firmware_stamp(firmware)
        await self._io.write(
            f"  Firmware: {firmware}"
            + (f"\n  ...built {stamp} -- the board's own boot line must say "
               f"the same." if stamp else ""))
        others = self._firmware_candidates(board)[1:]
        if others:
            await self._io.write(
                f"  ! There are {len(others)} other copies of this file where "
                f"the hub looks, and they are IGNORED: "
                f"{', '.join(str(p) for p in others[:3])}. If you replaced "
                f"one of those, the swap did nothing -- the file named above "
                f"is the one that gets written.", level="warn")
        # SAY SO IF THERE IS NOWHERE TO REPORT TO. A board flashed against a
        # placeholder works in every respect except the one being watched: it
        # joins Wi-Fi, applies its wiring, serves its own page, switches its
        # relays -- and never appears in Home Assistant.
        host = self._mqtt_settings()["host"]
        if host in ("", "127.0.0.1", "192.168.1.10"):
            await self._io.write(
                f"  Note: the broker address is still {host!r}. The board will "
                f"set up correctly and then have nowhere to report to. "
                f"Everything else still works -- its own web page can switch "
                f"the pins, which is a real test of the pin map.", level="warn")
        if not network_later and not await self._say_if_plaintext():
            return None                  # refused BEFORE the erase
        port = await self._pick_port()
        if port is None:
            return None
        # ASK THE CHIP WHO IT IS BEFORE WRITING ANYTHING -- the difference
        # between renaming a board and quietly acquiring a second record for
        # hardware you already own.
        displaced = await self._check_chip_is_free(port, record)
        if displaced is None:
            await self._io.write("  Left as it was.")
            return None
        already = bool(record.get("flashed"))
        if already and record.get("chip"):
            # No erase on this path, but a template is still sent: a chip
            # this record once met as another family must not get this
            # family's pin table (a type changed after the install).
            refusal = self._chip_mismatch(str(record["chip"]), board)
            if refusal:
                await self._io.write(refusal, level="warn")
                return None
        if not already:
            chip = await self._identify_chip(port)
            if chip is None:
                await self._io.write(
                    "  Could not identify the chip on that port, so nothing "
                    "was erased. Check the cable and try again.", level="warn")
                return None
            refusal = self._chip_mismatch(chip, board)
            if refusal:
                # STOPPED BEFORE THE ERASE. The fix is offered on the spot
                # when the cable's family can hold this wiring.
                await self._io.write(refusal, level="warn")
                await self._offer_cable_family(slug, chip)
                return None
            # Stored AS ANNOUNCED; _owns_psram_pins draws the conclusions, so
            # a PICO's record stays truthful -- chip says PICO, PsrMax says 0,
            # and both are correct.
            record["chip"] = chip
            features = getattr(self, "_chip_features", "")
            if features:
                record["chip_features"] = features
            # An S3 names its embedded PSRAM here, before anything runs on it.
            if record.get("psram") is None:
                learned = self._psram_from_chip(chip, features, board)
                if learned is not None:
                    record["psram"] = learned
                    if learned:
                        await self._io.write(self._psram_sentence(board))
            self._write_board(record)
            if "PICO" in chip.upper():
                await self._io.write(
                    "  This is a PICO package: GPIO16 and 17 belong to its "
                    "internal flash, so the hub will leave them alone. Treat "
                    "18 and 23 with suspicion too -- some PICO boards use "
                    "those as well.")
            # ERASE, on any chip THIS HUB has not flashed itself. Tasmota
            # keeps settings in a region the image does not cover, so writing
            # over a used chip preserves someone else's Wi-Fi and broker.
            await self._io.write(
                "  Erasing the chip completely first -- its history is "
                "unknown, and a blank page costs fifteen seconds once.")
            # esptool's chip names ARE the family ids (esp32, esp32s3, ...).
            esptool = ["--chip", board.family, "--port", port,
                       "--baud", "460800"]
            code = await self._run_esptool(
                esptool + ["erase_flash"], firmware.parent,
                busy={"text": "Erasing the chip",
                      "detail": "Its history is unknown -- blank page first.",
                      "seconds": 25})
            if code != 0:
                await self._io.write(
                    "  The erase did not complete, so the chip may be partly "
                    "cleared. No firmware was written and the hub's record "
                    "of this board is unchanged. Check the cable (a charge-"
                    "only cable looks fine and cannot flash) and run "
                    "Install again.", level="warn")
                return None
            offset = _config.TASMOTA_FLASH_OFFSET
            code = await self._run_esptool(
                esptool + ["write_flash", "-z", offset, str(firmware)],
                firmware.parent,
                busy={"text": "Writing the firmware",
                      "detail": "About a minute. The cable does the talking.",
                      "seconds": 70})
            if code != 0:
                await self._io.write(
                    "  The firmware did NOT go on -- and the chip was erased "
                    "just before, so it is blank now and will not start "
                    "until it is installed. The hub's record is unchanged "
                    "(still not installed). Check the cable, hold the "
                    "board's BOOT button as the write starts if it keeps "
                    "failing, and run Install again.", level="warn")
                return None
            await self._io.write("  Firmware written. Waiting for it to start...")
            # Only so the port is released cleanly. The REAL wait is in
            # _talk_to_board, which probes until the console answers.
            await asyncio.sleep(2.0)
        else:
            await self._io.write(
                "  This board already has the firmware, so only its settings "
                "are being sent.")
        if network_later:
            # The settings are the ONE part that needs a network this hub can
            # vouch for. Skipped whole rather than half-sent: a board holding
            # a broker password for a hub it cannot name is not further along
            # than one holding nothing, and it is harder to reason about.
            await self._io.write(
                "  Skipping the network and broker settings -- they are what "
                "needs a network this hub can vouch for. The firmware and the "
                "wiring do not.")
        else:
            if not await self._send_over_serial(
                    port, self._provisioning_line(record), record):
                return None
            # READ BACK WHAT WAS WRITTEN. "Sent" is not "kept"; only the chip
            # knows which, and it answers if asked.
            took = await self._settings_took(
                port, self._read_wifi().get("ssid", ""), self.hub_address())
            if took is False:
                await self._io.write(
                    "  The board took the commands but did not KEEP them -- "
                    "they did not reach its flash. Nothing further was sent. "
                    "Try the install again; if it repeats, its console will "
                    "say why.", level="warn")
                return None
            if took is None:
                await self._io.write(
                    "  Could not read the settings back to confirm they "
                    "stuck. Continuing; if the board never joins "
                    f"{self._read_wifi().get('ssid', '')!r}, run this again.",
                    level="warn")
        # TWO QUESTIONS BEFORE THE WIRING, both answered by the board: does
        # this module carry PSRAM, and which pins does the firmware offer.
        if record.get("psram") is None or record.get("pins_reported") is None:
            psram, reported = await self._probe_board_facts_serial(port, board)
            if psram is not None and record.get("psram") is None:
                record["psram"] = psram
                if psram and board.psram:
                    await self._io.write(self._psram_sentence(board))
            if reported is not None:
                record["pins_reported"] = reported
                await self._report_pin_differences(reported, board)
            self._write_board(record)
        # A board's wiring is what its record says. Saved layouts are put on
        # a board from Saved layouts ("Use on a board"), which edits the
        # record first -- a flash never guesses between two plans.
        plan = self._plan_for(slug)
        if plan is None:
            return None
        await self._say_plan_notes()
        # A board that arrived with firmware already on it may be running
        # anything; the hub's own image carries every driver. `learn` only
        # when THIS RUN wrote the file onto the chip -- that is the one
        # moment its GPIOs answer is a fact about the file on disk. On a
        # settings-only run the chip runs whatever it ran before, and its
        # verdict must not be recorded against a file it never proved.
        if self._drivers_needed(plan):
            try:
                reply = await self._talk_to_board(port, ["GPIOs"])
            except Exception:                      # noqa: BLE001
                reply = []
            if not await self._refuse_if_driver_missing(plan, reply,
                                                        record["name"],
                                                        learn=not already):
                return None
        if await self._psram_unknown_stop(plan):
            return None
        activation, configuration = plan.backlogs()
        # AND WHAT THE ENGINE WOULD RE-ASSERT (Sleep, PowerDelta,
        # TelePeriod): after the template, so the meter's commands exist.
        # Over the air it is skipped while a board is mid-install.
        upkeep = self.maintenance_commands(record)
        if upkeep:
            configuration = list(configuration) + ["Backlog " + "; ".join(upkeep)]
        await self._io.write("  Sending its wiring -- the template first...")
        if not await self._send_over_serial(port, activation, record):
            await self._io.write(
                "  The firmware is on the chip but its wiring did not go "
                "on, so it is not working yet. The hub still has it as not "
                "installed -- run Install again; that run skips the erase "
                "and only re-sends the wiring and settings.", level="warn")
            return None
        if configuration:
            # Module 0 restarts the board to apply the template, and the
            # restart ABANDONS anything queued behind it (bedroom's
            # DisplayModel, 2026-08-24). The sleep covers the going-down;
            # _send_over_serial's console probe waits out the coming-back
            # before the settings are typed in.
            await self._io.write(
                "  It is restarting to apply the template -- sending its "
                "device settings when it answers...")
            await asyncio.sleep(6.0)
            if not await self._send_over_serial(port, configuration, record):
                await self._io.write(
                    "  The template landed but the device settings did not "
                    "-- run the install again; it skips the erase and only "
                    "re-sends settings.", level="warn")
                return None
        record["flashed"] = _now()
        # THE ONE THING STILL OWED, written down rather than remembered. A
        # half-installed board is invisible otherwise: it has firmware, it
        # has its wiring, and nothing anywhere says it was never told where
        # to go. The flag rides in the record, so the next install -- this
        # session or next week -- knows to skip the erase and send only the
        # settings.
        if network_later:
            record["network_pending"] = True
        else:
            record.pop("network_pending", None)
        self._write_board(record)
        # THE ACCOUNT, RE-CONFIRMED. It was granted when the board was
        # designed, possibly before Mosquitto existed on this machine -- and
        # a board with no account joins Wi-Fi, looks perfect, and is refused
        # for ever. Idempotent, so this costs nothing when it already exists.
        granted = await self.grant_access(record)
        # THE DISPLACED BOARD GOES NOW, not "later": its record describes
        # hardware that no longer answers to it.
        if displaced:
            await self._retire_displaced(displaced)
        if network_later:
            self.last_flash_needs_network = True
            self._last_node = slug
            await self._io.write(
                f"\n  {record['name']} has its firmware and its wiring. What "
                f"it does NOT have is the network: it does not know which "
                f"Wi-Fi to join or where this hub answers, so it will sit in "
                f"its own set-up access point instead of appearing in Home "
                f"Assistant.")
            await self._io.write(
                "  To finish: sort the Wi-Fi out (Change ESP Wi-Fi, or bring "
                "the right network up), then Add device -> Set up an ESP "
                "board -> this board -> Install. That run skips the erase and "
                "the firmware -- it only sends the settings, and takes "
                "seconds.")
            await self._io.write("  You can unplug the cable now.")
            return None
        if granted and wifi_state == self.WIFI_TRUSTED:
            await self._io.write(
                f"\n  {record['name']} is set up and knows to join "
                f"{self.wifi_ssid()!r}. That network is not running yet, so "
                f"it will keep looking -- Tasmota retries for ever -- and it "
                f"will join the moment the hotspot is back on.")
        elif granted:
            await self._io.write(
                f"\n  {record['name']} is set up. It will join "
                f"{self.wifi_ssid()!r} on its own -- there is nothing to "
                f"type in.")
        else:
            await self._io.write(
                f"\n  {record['name']} is set up and will join "
                f"{self.wifi_ssid()!r}, but it has no account on the broker "
                f"yet, so it will be refused when it reports in. Fix the "
                f"broker and install again.", level="warn")
        await self._io.write("  You can unplug the cable now.")
        self._last_node = slug
        return record["topic"]

    # ── listing and picking ─────────────────────────────────────────────

    @staticmethod
    def board_state(record: Dict[str, Any]) -> str:
        """How far along a board is, in one phrase. ONE definition, because
        the CLI picker, the Add-device picker and the app all showed their
        own -- and none of them knew about a board that has firmware but was
        never told its network."""
        if record.get("network_pending"):
            return "firmware on, but never told its network"
        if record.get("flashed"):
            return "set up " + str(record["flashed"])[:10]
        return "designed, not installed yet"

    async def _pick_existing_node(self) -> Optional[str]:
        boards = self.list_nodes()
        if not boards:
            await self._io.write("  No boards have been set up yet.")
            return None
        rows = []
        for slug in boards:
            record = self._read_board(slug) or {}
            state = self.board_state(record)
            rows.append({"value": slug,
                         "label": record.get("name", slug),
                         "sub": f"{len(record.get('items', []))} device(s), "
                                f"{state}"})
        picked = await self._io.choose("Which board?", rows)
        return picked[0] if picked else None

    # ── subprocess streaming ────────────────────────────────────────────
    #
    # Kept from the ESPHome version. It exists because Windows' Selector
    # event loop cannot spawn child processes at all, which used to surface
    # as "the app disconnects when I press Run". esptool is a child process
    # exactly as the compiler was.

    async def _stream(self, cmd: List[str], cwd: Path, timeout: float,
                      on_line=None, *, quiet: bool = False) -> int:
        """Run a command, forwarding its output as it arrives.
        NOTHING escapes except CancelledByUser: a flash that cannot run is a
        failed flash, and must never reach ws_server's connection handler,
        which answers an exception by closing the socket.
        `quiet` is a parameter and the reset is in a finally -- as an
        instance flag set around the call, a cancellation left every later
        flash silent for the life of the builder.
        """
        self._said_no_board = False
        self._quiet_stream = quiet
        started = time.monotonic()
        try:
            try:
                return await self._stream_asyncio(cmd, cwd, timeout, started,
                                                  on_line)
            except NotImplementedError:
                # WINDOWS, SELECTOR EVENT LOOP. Only Proactor can spawn
                # children; Selector raises outright, with nothing to
                # configure.
                if not self._said_threaded:
                    self._said_threaded = True
                    await self._io.write(
                        "  (this machine cannot spawn processes on its event "
                        "loop, so this runs in a worker thread -- no "
                        "difference to you)")
                return await self._stream_threaded(cmd, cwd, timeout, started,
                                                   on_line)
        except FileNotFoundError:
            await self._io.write(
                f"  Could not run {cmd[0]}. Install it with "
                f"`pip install esptool`.", level="warn")
            return 127
        except CancelledByUser:
            raise
        except Exception as exc:                   # noqa: BLE001
            traceback.print_exc()
            await self._io.write(
                f"  The flash could not be run: {type(exc).__name__}: {exc}",
                level="warn")
            return -1
        finally:
            self._quiet_stream = False

    async def _handle_line(self, line: str, state: Dict[str, float],
                           on_line) -> None:
        line = line.rstrip()
        if not line:
            return
        if on_line is not None:
            on_line(line)
        if self._NO_BOARD.search(line) and not self._said_no_board:
            self._said_no_board = True
            await self._io.write(
                "  ! The board is not answering. Two things cause this almost "
                "every time: a charge-only USB cable, or a board that needs "
                "its BOOT button held while the flash starts.", level="warn")
            return
        if self._quiet_stream:
            # A busy card with its own bar is on screen; progress chatter
            # behind it only makes it look like a crash site. Warnings above
            # still spoke.
            return
        now = time.monotonic()
        if self._LOUD.search(line) or (now - state["last"]) >= self.PROGRESS_EVERY:
            state["last"] = now
            await self._io.write(f"    {line[:160]}")

    async def _forward_lines(self, next_line, timeout: float, started: float,
                             on_line) -> bool:
        """Feed every output line to _handle_line until end of output.
        `next_line` is awaited for each line (bytes or str; empty or None
        at the end). False, said out loud, when `timeout` ran out first."""
        state = {"last": 0.0}
        try:
            while True:
                remaining = timeout - (time.monotonic() - started)
                if remaining <= 0:
                    raise asyncio.TimeoutError()
                line = await asyncio.wait_for(next_line(), timeout=remaining)
                if not line:
                    return True
                if isinstance(line, bytes):
                    line = line.decode("utf-8", "replace")
                await self._handle_line(line, state, on_line)
        except asyncio.TimeoutError:
            await self._io.write("  Gave up waiting -- stopping it.",
                                 level="warn")
            return False

    @staticmethod
    async def _reap(terminate, kill, wait) -> None:
        """An orphaned esptool holds the serial port open, and the next
        attempt then fails with a port-busy error nobody can explain."""
        try:
            terminate()
            await wait(5)
        except Exception:                          # noqa: BLE001
            try:
                kill()
            except Exception:                      # noqa: BLE001
                pass

    async def _stream_asyncio(self, cmd, cwd: Path, timeout: float,
                              started: float, on_line=None) -> int:
        proc = await asyncio.create_subprocess_exec(
            *cmd, cwd=str(cwd),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT)
        try:
            if not await self._forward_lines(proc.stdout.readline, timeout,
                                             started, on_line):
                return -1
            return await proc.wait()
        finally:
            if proc.returncode is None:
                await self._reap(
                    proc.terminate, proc.kill,
                    lambda t: asyncio.wait_for(proc.wait(), timeout=t))

    async def _stream_threaded(self, cmd, cwd: Path, timeout: float,
                               started: float, on_line=None) -> int:
        loop = asyncio.get_running_loop()
        queue: asyncio.Queue = asyncio.Queue()
        proc = subprocess.Popen(
            cmd, cwd=str(cwd),
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            bufsize=1, text=True, errors="replace",
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        def pump() -> None:
            try:
                for line in proc.stdout:           # blocking, off-loop
                    loop.call_soon_threadsafe(queue.put_nowait, line)
            except Exception:                      # noqa: BLE001
                pass
            finally:
                loop.call_soon_threadsafe(queue.put_nowait, None)
        threading.Thread(target=pump, daemon=True,
                         name="esptool-output").start()
        try:
            if not await self._forward_lines(queue.get, timeout, started,
                                             on_line):
                return -1
            return await asyncio.to_thread(proc.wait)
        finally:
            if proc.poll() is None:
                await self._reap(
                    proc.terminate, proc.kill,
                    lambda t: asyncio.to_thread(proc.wait, t))

    # ── the surface the shell actually calls ────────────────────────────
    add_node = menu_add_esp_node
    flash = _flash_node
    pick_existing = _pick_existing_node

    @property
    def last_node(self) -> Optional[str]:
        return self._last_node

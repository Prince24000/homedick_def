"""hub/ota_server.py -- the hub serves its own firmware, so a board can heal.

WHY THIS EXISTS. Tasmota's ESP32 image has ONE app partition and a small
SAFEBOOT partition beside it. Every firmware update -- `Upgrade` with a
URL, or a file upload on the board's web page -- reboots the board into
safeboot, which ERASES the app partition and writes the new image into it
as it arrives. If the download or upload fails after the erase there is no
app to fall back to: the board comes up in safeboot for ever -- joins the
Wi-Fi, connects to the broker over TLS, reports Online, and answers
"Unknown" to nearly every command. Safeboot's own recovery plan is to
download the firmware again from OtaUrl. On this project's boards that
plan never worked: first OtaUrl pointed at ota.tasmota.com on a network
with no internet (Kitchen bricked, 2026-09-09), then at the hub, which
served nothing (Kitchen bricked again, 2026-09-10). Every failed update
was therefore an afternoon with a cable.

WHAT IT DOES. Serves each ESP32 family's app image over plain HTTP (what
Tasmota's downloader speaks), one path per family, at the path esp_builder's
_ota_url() hands every board of that family:

    http://<hub address>:<config.OTA_HTTP_PORT>/firmware/tasmota32.bin
    http://<hub address>:<config.OTA_HTTP_PORT>/firmware/tasmota32s3.bin  ...

The classic path is unchanged, so boards provisioned before families
existed keep healing from the URL they already hold. The names are the
board profiles' own (esp_firmware/chips.py: Board.ota_name).

With that in place a stranded board heals itself: `OtaUrl` + `Upgrade 1`
(sent by the automation engine the moment a board announces a safeboot
version, or by the install flow, or by hand) fetches the app from the hub
and the board reboots into it. It also makes a deliberate update one
command from the hub instead of a cable.

WHERE THE IMAGE COMES FROM (the classic one; every family the same way,
with its own names beside its own factory image).
esp_firmware/firmware/tasmota32.bin, beside the factory image, when
build_firmware.py left one there (that is where it installs it); otherwise
it is CUT OUT OF THE FACTORY IMAGE the cable path writes
(config.TASMOTA_FIRMWARE_PATH): the ESP32 partition table at 0x8000 says
where the app partition (type app, subtype ota_0) sits, the ESP image
header says how long the image really is, and that slice IS tasmota32.bin
-- the same bytes (checked against the v15.6.0 release: the cut of
tasmota32.factory.bin is byte-for-byte the published tasmota32.bin, same
SHA-256), so a healed board runs exactly what a cable-flashed board runs.
The cut is cached at that same path and redone whenever the factory
image is newer than it.

BINDING. 0.0.0.0. The other listeners bind named addresses because they
serve the household's secret-entry page and the phones' private
protocol; this serves a public open-source binary that is read-only,
one path, no listing, and it must be reachable from whichever network a
board is on -- the router today, the hub's hotspot tomorrow -- without
the bind supervisor's help.
"""
from __future__ import annotations

import asyncio
import os
import struct
import threading
import time
import inspect
from typing import Any, Callable, Dict, Optional, Tuple, Union

#: The classic ESP32's image and path -- what every board provisioned
#: before families existed holds as its OtaUrl. Never to change.
OTA_PATH = "/firmware/tasmota32.bin"
OTA_NAME = "tasmota32.bin"
OTA_PREFIX = "/firmware/"
#: The app image sits BESIDE its factory image (esp_firmware/firmware/
#: tasmota32.bin next to tasmota32.factory.bin): one folder for every image.
PARTITION_TABLE_OFFSET = 0x8000
PARTITION_MAGIC = 0x50AA        # the bytes AA 50 on disk, read little-endian
PART_TYPE_APP = 0x00
PART_SUBTYPE_OTA_0 = 0x10
ESP_IMAGE_MAGIC = 0xE9
APP_DESC_MAGIC = 0xABCD5432
#: Seconds one response may take to leave the hub (see OtaServer._reply).
SEND_TIMEOUT = 300.0


# ── the image ──────────────────────────────────────────────────────────────
def partitions(factory: bytes):
    """The ESP32 partition table inside a factory image: (type, subtype,
    offset, size, label) per entry, in order."""
    out = []
    pos = PARTITION_TABLE_OFFSET
    while pos + 32 <= len(factory):
        magic, ptype, subtype, offset, size = struct.unpack_from("<HBBII", factory, pos)
        if magic != PARTITION_MAGIC:
            break
        label = factory[pos + 12:pos + 28].split(b"\0", 1)[0].decode("ascii", "replace")
        out.append((ptype, subtype, offset, size, label))
        pos += 32
    return out


def esp_image_length(data: bytes) -> int:
    """The true length of an ESP32 app image from its own headers: the
    24-byte image header, each segment's 8-byte header plus data, a
    checksum padded to a 16-byte boundary, and a 32-byte SHA-256 when the
    header says one is appended. Raises ValueError on anything else."""
    if len(data) < 24 or data[0] != ESP_IMAGE_MAGIC:
        raise ValueError("not an ESP32 app image (no 0xE9 magic)")
    segments = data[1]
    hash_appended = data[23] == 1
    pos = 24
    for _ in range(segments):
        if pos + 8 > len(data):
            raise ValueError("segment header past the end of the image")
        _addr, size = struct.unpack_from("<II", data, pos)
        pos += 8 + size
        if pos > len(data):
            raise ValueError("segment data past the end of the image")
    pos += 1                                     # the checksum byte...
    pos = (pos + 15) // 16 * 16                  # ...padded to 16 bytes
    if hash_appended:
        pos += 32
    if pos > len(data):
        raise ValueError("image shorter than its headers claim")
    return pos


def app_description(image: bytes) -> Dict[str, str]:
    """Tasmota's esp_app_desc_t, at offset 0x20 of the image: version,
    project, build time and date. Empty when absent."""
    if len(image) < 0x20 + 256 or struct.unpack_from("<I", image, 0x20)[0] != APP_DESC_MAGIC:
        return {}
    def s(off, n):
        return image[off:off + n].split(b"\0", 1)[0].decode("ascii", "replace")
    base = 0x20
    return {"version": s(base + 16, 32), "project": s(base + 48, 32),
            "time": s(base + 80, 16), "date": s(base + 96, 16), "idf": s(base + 112, 32)}


#: The drivers this project's build adds to stock Tasmota, each with a
#: literal that exists ONLY when that driver was compiled in (its own log
#: text or command table, from the v15.5.0/v15.6.0 sources; none of them
#: is in the stock tasmota32 image -- checked). GPIO names such as
#: "TM1637 CLK" are NOT used: those are in every build's enum whether or
#: not the driver is. Nor is "DisplayMode" as one word: Tasmota keeps its
#: command names in a "|"-joined table with the prefix separate
#: (kDisplayCommands = "Display||Model|Type|Width|Height|Mode|..."), so
#: the joined word never exists in a binary -- the first draft of this
#: table looked for it and called a working display "NOT in".
#: Two MAX7219 drivers exist and they are different files: the SEVEN-
#: SEGMENT one (xdsp_15, USE_DISPLAY_MAX7219, shared with TM1637) and the
#: DOT-MATRIX one (xdsp_19, USE_DISPLAY_MAX7219_MATRIX).
PROJECT_DRIVERS = (
    ("RC522", b"MFR: RC522 Rfid Reader"),                       # xsns_80_mfrc522.ino
    ("Display", b"Display||Model|Type|Width|Height|Mode|"),      # xdrv_13_display.ino, the display core
    ("LCD", b"DSP: LCD"),                                       # xdsp_01_lcd.ino
    ("TM1637", b"digits (type %d)"),                            # xdsp_15_tm1637.ino ("DSP: %s with %d digits (type %d)")
    ("MAX7219-matrix", b"MAX7291Matrix_init"),                  # xdsp_19_max7219_matrix.ino (sic, upstream's typo)
    ("MPU6050", b"MPU6050"),                                    # xsns_32_mpu6050.ino
)


def drivers_in(image: bytes) -> Tuple[list, list]:
    """(present, missing) names from PROJECT_DRIVERS for an app image."""
    present = [name for name, marker in PROJECT_DRIVERS if marker in image]
    missing = [name for name, marker in PROJECT_DRIVERS if marker not in image]
    return present, missing


def describe(image: bytes, desc: Optional[Dict[str, str]] = None) -> str:
    """One line about an app image for the log: size, SHA-256 prefix, and
    whatever the descriptor carries. Tasmota's release builds leave the
    version/project/date fields of the descriptor EMPTY (checked on the
    v15.6.0 image), so the hash is the reliable identity; the version and
    build date a board reports come from Status 2 once it runs the image."""
    import hashlib
    d = desc if desc is not None else app_description(image)
    parts = [f"{len(image):,} bytes", f"sha256 {hashlib.sha256(image).hexdigest()[:12]}"]
    stamped = " ".join(x for x in (d.get("project", ""), d.get("version", ""),
                                   d.get("date", ""), d.get("time", "")) if x)
    if stamped:
        parts.append(stamped)
    if d.get("idf"):
        parts.append(f"IDF {d['idf']}")
    present, missing = drivers_in(image)
    if present:
        parts.append("drivers in: " + " ".join(present))
    if missing:
        parts.append("NOT in: " + " ".join(missing))
    return ", ".join(parts)


def cut_app_image(factory: bytes) -> bytes:
    """tasmota32.bin out of tasmota32.factory.bin: the ota_0 app partition's
    slice, trimmed to the image's own length."""
    parts = partitions(factory)
    if not parts:
        raise ValueError("no partition table at 0x8000 -- not an ESP32 factory image")
    app = [p for p in parts if p[0] == PART_TYPE_APP and p[1] == PART_SUBTYPE_OTA_0]
    if not app:
        raise ValueError("no ota_0 app partition in the table: " +
                         ", ".join(f"{p[4]}({p[0]:#x}/{p[1]:#x})" for p in parts))
    _t, _s, offset, size, _label = app[0]
    if offset >= len(factory):
        raise ValueError(f"app partition at {offset:#x} lies beyond the factory image ({len(factory)} bytes)")
    slice_ = factory[offset:offset + size]
    return slice_[:esp_image_length(slice_)]


class OtaImage:
    """The app image the hub serves, found or cut, re-read when it changes."""

    def __init__(self, factory_path: str, ota_path: Optional[str] = None,
                 say: Callable[[str], None] = print, name: str = OTA_NAME):
        #: The app image's file name (tasmota32s3.bin for an S3...): the
        #: cached cut is saved under it, and it is the served path's tail.
        self.name = name
        self.factory_path = factory_path
        self.ota_path = ota_path or os.path.join(os.path.dirname(factory_path) or ".", name)
        self.say = say
        self._data: bytes = b""
        self._stamp: Optional[Tuple[float, float]] = None
        #: data() runs in worker threads (OtaServer), and two requests
        #: arriving as the image changes must not both cut and cache it.
        self._lock = threading.Lock()
        self.source = ""
        self.description: Dict[str, str] = {}

    def _stamps(self) -> Tuple[float, float]:
        def m(p):
            try:
                return os.path.getmtime(p)
            except OSError:
                return 0.0
        return (m(self.ota_path), m(self.factory_path))

    def exists(self) -> bool:
        """Is there a file to serve or to cut from? (Nothing is read.)"""
        return os.path.isfile(self.ota_path) or os.path.isfile(self.factory_path)

    def data(self) -> bytes:
        """The image bytes, or b"" when there is nothing to serve (said once)."""
        with self._lock:
            return self._load()

    def _load(self) -> bytes:
        stamps = self._stamps()
        if stamps == self._stamp:
            return self._data
        self._stamp = stamps
        self._data, self.source, self.description = b"", "", {}
        try:
            ota_bad = ""
            if os.path.isfile(self.ota_path) and stamps[0] >= stamps[1]:
                with open(self.ota_path, "rb") as f:
                    data = f.read()
                try:
                    esp_image_length(data)      # must be a real app image
                    self._data, self.source = data, self.ota_path
                except ValueError as exc:       # a hand-placed file that is not one
                    ota_bad = str(exc)
                    self.say(f"[OTA] ignoring {self.ota_path}: {exc} -- using the factory image instead")
            if not self._data and os.path.isfile(self.factory_path):
                with open(self.factory_path, "rb") as f:
                    factory = f.read()
                self._data = cut_app_image(factory)
                self.source = f"cut from {os.path.basename(self.factory_path)}"
                if not ota_bad:                 # cache it for next time
                    try:                        # (never over a file someone placed)
                        os.makedirs(os.path.dirname(self.ota_path) or ".", exist_ok=True)
                        tmp = self.ota_path + ".tmp"
                        with open(tmp, "wb") as f:
                            f.write(self._data)
                        os.replace(tmp, self.ota_path)
                        self._stamp = self._stamps()
                    except OSError:
                        pass
            if not self._data:
                self.say(f"[OTA] nothing to serve: no {self.name} and no factory image at {self.factory_path}")
                return b""
            self.description = app_description(self._data)
            self.say(f"[OTA] serving {self.name}: {describe(self._data, self.description)} ({self.source})")
        except (OSError, ValueError) as exc:
            self.say(f"[OTA] cannot serve firmware: {exc}")
            self._data = b""
        return self._data


def path_for(ota_name: str) -> str:
    """The served path of one family's app image: /firmware/<ota_name>."""
    return OTA_PREFIX + ota_name


def family_images(factory_path_of: Callable[[Any], str],
                  say: Callable[[str], None] = print) -> Dict[str, OtaImage]:
    """One OtaImage per ESP32 family, by served path: each cut from (or
    found beside) THAT family's factory image. `factory_path_of(board)` is
    config.firmware_path_for -- passed in so this module reads no config.
    The classic family lands on OTA_PATH, as it always has."""
    from esp_firmware import chips
    return {path_for(board.ota_name):
            OtaImage(factory_path_of(board), say=say, name=board.ota_name)
            for board in chips.FAMILIES.values()}


# ── the server ─────────────────────────────────────────────────────────────
class OtaServer:
    """One file per family over plain HTTP, for Tasmota's downloader. Never
    raises out of a connection; every request is logged with its peer."""

    def __init__(self, images: Union[OtaImage, Dict[str, OtaImage]], port: int,
                 host: str = "0.0.0.0", say: Callable[[str], None] = print):
        # One OtaImage (the classic, at OTA_PATH) or {served path: image}.
        if isinstance(images, OtaImage):
            images = {OTA_PATH: images}
        self.images: Dict[str, OtaImage] = dict(images)
        self.port, self.host, self.say = port, host, say
        self._server: Optional[asyncio.AbstractServer] = None

    @property
    def image(self) -> Optional[OtaImage]:
        """The classic image (the one path older callers knew)."""
        return self.images.get(OTA_PATH)

    async def start(self) -> bool:
        try:
            self._server = await asyncio.start_server(self._handle, self.host, self.port)
        except OSError as exc:
            self.say(f"[OTA] not serving firmware: port {self.port} -- {exc}")
            return False
        if self._server.sockets:                 # port 0 -> whatever was bound
            self.port = self._server.sockets[0].getsockname()[1]
        # Say what is being served, now -- IN A THREAD, like every read of
        # the image: the first one cuts a multi-megabyte factory image and
        # hashes the result, and this is the hub's event loop. A family
        # with no file at all is passed over in silence (only a board of
        # that family needs it); the classic one says so either way.
        served = []
        for path, image in sorted(self.images.items()):
            if path == OTA_PATH or image.exists():
                if await asyncio.to_thread(image.data):
                    served.append(path)
        self.say(f"[OTA] firmware for the boards at http://<this hub>:{self.port}"
                 + (", ".join(served) if served else f"{OTA_PATH} (nothing to serve yet)"))
        return True

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        peer = writer.get_extra_info("peername")
        peer = f"{peer[0]}:{peer[1]}" if peer else "?"
        try:
            head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), timeout=10)
            line = head.split(b"\r\n", 1)[0].decode("latin-1", "replace")
            parts = line.split()
            method, target = (parts[0], parts[1]) if len(parts) >= 2 else ("", "")
            image = self.images.get(target.split("?", 1)[0])
            if method not in ("GET", "HEAD") or image is None:
                await self._reply(writer, 404, b"not here\n", "text/plain")
                self.say(f"[OTA] {peer} {method} {target[:60]} -> 404")
                return
            data = await asyncio.to_thread(image.data)
            if not data:
                await self._reply(writer, 503, b"no firmware on this hub\n", "text/plain")
                self.say(f"[OTA] {peer} asked for {image.name} and there is none to give")
                return
            await self._reply(writer, 200, data, "application/octet-stream", body=method == "GET")
            self.say(f"[OTA] {peer} {method} {image.name} -> 200, {len(data):,} bytes"
                     + (" (a board fetching its firmware)" if method == "GET" else ""))
        except (asyncio.TimeoutError, asyncio.IncompleteReadError,
                asyncio.LimitOverrunError, ConnectionError, OSError) as exc:
            self.say(f"[OTA] {peer}: {type(exc).__name__}")
            # A peer that stopped reading still has up to an image's worth
            # queued in the transport; close() would wait to flush it, for
            # ever. Drop it instead.
            writer.transport.abort()
        finally:
            try:
                writer.close()
                await asyncio.wait_for(writer.wait_closed(), timeout=10)
            except Exception:                    # noqa: BLE001
                writer.transport.abort()

    @staticmethod
    async def _reply(writer: asyncio.StreamWriter, status: int, data: bytes,
                     ctype: str, body: bool = True) -> None:
        reason = {200: "OK", 404: "Not Found", 503: "Service Unavailable"}.get(status, "OK")
        writer.write((f"HTTP/1.1 {status} {reason}\r\nContent-Type: {ctype}\r\n"
                      f"Content-Length: {len(data)}\r\nConnection: close\r\n"
                      f"Cache-Control: no-cache\r\n\r\n").encode("ascii"))
        if body:
            writer.write(data)
        # BOUNDED. The port is open to the whole network with no auth; a
        # peer that asks for the image and never reads would otherwise hold
        # the connection -- and a copy of the unsent image -- for ever.
        # Generous: a board on weak Wi-Fi fetches ~1.5 MB in well under this.
        await asyncio.wait_for(writer.drain(), timeout=SEND_TIMEOUT)


def url_for(host: str, port: int, ota_name: str = OTA_NAME) -> str:
    """Where a board of the family whose app image is `ota_name` fetches it."""
    return f"http://{host}:{port}{path_for(ota_name)}"


# ── the heal ───────────────────────────────────────────────────────────────
class SafebootHealer:
    """Answers a board that is running SAFEBOOT with the two commands that
    get it out: OtaUrl (this hub) and Upgrade 1. Safeboot knows no Backlog,
    so they go as two publishes. Rate-limited per board, so a board that
    cannot heal (a bad image) is not asked for ever."""

    ATTEMPTS = 3
    WINDOW_S = 3600.0
    #: A board mid-download answers Unknown to everything the hub sends it
    #: meanwhile; a second Upgrade inside this gap would only restart the
    #: fetch. One order, then time for it to land and reboot.
    GAP_S = 90.0

    def __init__(self, publish: Callable[[str, str], Any], url_of: Callable[..., str],
                 say: Callable[[str], None] = print, now: Callable[[], float] = time.time):
        self.publish, self.url_of, self.say, self.now = publish, url_of, say, now
        # url_of(board_topic) when it takes one -- each FAMILY heals from
        # its own image, so the URL depends on which board asks; a plain
        # url_of() (one image for every board) still works.
        try:
            params = inspect.signature(url_of).parameters.values()
            self._per_board = any(p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD,
                                             p.VAR_POSITIONAL) for p in params)
        except (TypeError, ValueError):
            self._per_board = False
        self.attempts: Dict[str, list] = {}
        self._gave_up: Dict[str, float] = {}

    @staticmethod
    def is_safeboot(version: str) -> bool:
        return "safeboot" in str(version or "").lower()

    def heal(self, board_topic: str, version: str = "") -> bool:
        t = self.now()
        recent = [x for x in self.attempts.get(board_topic, []) if t - x < self.WINDOW_S]
        if recent and t - recent[-1] < self.GAP_S:
            return False                         # told already; let it download
        if len(recent) >= self.ATTEMPTS:
            if self._gave_up.get(board_topic) != recent[0]:   # once per window
                self._gave_up[board_topic] = recent[0]
                self.say(f"[OTA] {board_topic} is still in safeboot after {self.ATTEMPTS} attempts this hour "
                         f"-- not asking again; the served image may be wrong for it, flash over the cable")
            self.attempts[board_topic] = recent
            return False
        url = self.url_of(board_topic) if self._per_board else self.url_of()
        if not url:
            self.say(f"[OTA] {board_topic} is in safeboot ({version}) but this hub has no firmware to give it "
                     f"-- flash it over the cable")
            return False
        recent.append(t)
        self.attempts[board_topic] = recent
        self.say(f"[OTA] {board_topic} is running SAFEBOOT ({version or 'version unknown'}): "
                 f"telling it to fetch its firmware from this hub ({url}) -- attempt {len(recent)} of {self.ATTEMPTS}; "
                 f"about a minute, then it reboots into the real firmware")
        self.publish(f"cmnd/{board_topic}/OtaUrl", url)
        self.publish(f"cmnd/{board_topic}/Upgrade", "1")
        return True
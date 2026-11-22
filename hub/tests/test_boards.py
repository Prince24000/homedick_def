"""Multi-board support in the builder, the OTA server and setup: the board a
record names, per-family templates and pin maps, the cable's double-check
before the erase, per-family firmware and OTA paths, and S3 PSRAM learning.

Run: python3 tests/test_boards.py   (from anywhere; no board, no esptool)
"""
import asyncio
import os
import struct
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.setdefault("HOMEVOICE_HOME", tempfile.mkdtemp())
os.environ["HOMEVOICE_BOARDS_DIR"] = tempfile.mkdtemp()

from core import config                                      # noqa: E402
from esp_firmware import chips                               # noqa: E402
from esp_firmware import esphome_catalog as cat              # noqa: E402
from esp_firmware.esp_builder import EspBuilder              # noqa: E402
from hub import ota_server as ota                            # noqa: E402

PASSED = FAILED = 0
S3, C3, CLASSIC = chips.ESP32_S3, chips.ESP32_C3, chips.DEFAULT


def check(name, ok, detail=""):
    global PASSED, FAILED
    if ok:
        PASSED += 1
        print(f"  [PASS] {name}")
    else:
        FAILED += 1
        print(f"  [FAIL] {name}   {detail}")


# ── a builder with a fake screen ───────────────────────────────────────────

class FakeIO:
    def __init__(self, answers=(), pin_map=None):
        self.answers = list(answers)      # values for choose(), in order
        self.said = []
        self.asked = []                   # (prompt, rows)
        self.frames = []
        self.pin_map_reply = pin_map
        self.typed = []

    async def write(self, text, *, level="info"):
        self.said.append(text)

    async def choose(self, prompt, options, **_kw):
        self.asked.append((prompt, list(options)))
        if not self.answers:
            return []
        answer = self.answers.pop(0)
        return [answer(options) if callable(answer) else answer]

    async def ainput(self, prompt, **_kw):
        return self.typed.pop(0) if self.typed else ""

    async def pin_map(self, payload):
        self.frames.append(payload)
        return self.pin_map_reply

    async def send_data(self, kind, payload):
        pass

    def text(self):
        return "\n".join(self.said)


class FakeShell:
    def __init__(self, io):
        self._io = io


def builder(io=None):
    b = EspBuilder.headless()
    b._shell = FakeShell(io or FakeIO())
    b._said_sidecar_notes = True
    b._sidecar_load_notes = []
    b._board_load_notes = []
    b._last_node = None
    b.last_flash_needs_network = False
    return b


def fresh_dir():
    config.BOARDS_DIR = tempfile.mkdtemp()


def record(slug="bench", board=None, items=(), **extra):
    rec = {"slug": slug, "name": slug.title(), "items": list(items),
           "mqtt_user": f"hv-{slug}", "mqtt_pass": "x", "web_pass": "y",
           "topic": f"hv-{slug}", "created": "", "flashed": ""}
    if board is not None:
        rec["board"] = board.id
        rec["board_family"] = board.family
    rec.update(extra)
    return rec


def relay(name, pin):
    return {"type_id": "relay", "name": name, "pins": {"pin": pin}}


def run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


# ── 1. old records are the classic board ───────────────────────────────────

def old_records():
    print("\nold records")
    b = builder()
    fresh_dir()
    check("a record with no board field is the classic DevKit",
          b._board_of({}) is CLASSIC)
    b._write_board(record("old", items=[relay("lamp", 26)]))
    plan = b._plan_for("old")
    check("its plan is the classic board", plan.board is CLASSIC)
    check("its template is the classic 36 positions",
          len(plan.template()["GPIO"]) == 36 == len(cat.TEMPLATE_ORDER))
    check("a board file that is gone falls back to its FAMILY, not classic",
          b._board_of({"board": "gone-s3-file", "board_family": "esp32s3"})
          is chips.FAMILIES["esp32s3"])
    b._write_board(record("s3", S3, [relay("lamp", 4)]))
    check("an S3 record's plan is the S3 board",
          b._plan_for("s3").board is S3)


# ── 2. per-family templates ────────────────────────────────────────────────

def templates():
    print("\ntemplates")
    for board, length in ((S3, 38), (C3, 22)):
        dead = sorted((board.flash | board.absent) & set(board.template_order))
        plan = cat.BoardPlan("t", owns_psram_pins=False, board=board)
        plan.add("relay", "lamp", {"pin": 4})
        gpio = plan.template()["GPIO"]
        order = board.template_order
        check(f"{board.family_label}: template has {length} positions",
              len(gpio) == length, len(gpio))
        check(f"{board.family_label}: the relay sits at GPIO4's position",
              gpio[order.index(4)] != 0)
        zeroed = [gpio[order.index(n)] for n in dead]
        check(f"{board.family_label}: flash/absent positions are zero",
              all(v == 0 for v in zeroed), zeroed)
        check(f"{board.family_label}: no position is a flash pin's, or "
              f"every such position is zeroed",
              not (set(order) & board.flash) or zeroed)
    check("C3: the flash bus 11-17 has positions, all zero",
          sorted((C3.flash) & set(C3.template_order)) == list(range(11, 18)))
    check("S3: 22-32 have no template position at all",
          not set(range(22, 33)) & set(S3.template_order))
    s3 = cat.BoardPlan("t", board=S3)
    try:
        s3.add("relay", "lamp", {"pin": 35})
        refused = False
    except cat.PlacementError:
        refused = True
    check("S3 with PSRAM unknown: GPIO35 allowed while designing (the "
          "board is asked at install)", not refused)


# ── 3. pin map frames per board ────────────────────────────────────────────

def pin_maps():
    print("\npin map frames")
    # Never installed: nothing is the module's yet (asked at install).
    for board, owned in ((CLASSIC, []), (S3, []), (C3, [])):
        fresh_dir()
        io = FakeIO(pin_map={"cancelled": True})
        b = builder(io)
        b._write_board(record("pm", None if board is CLASSIC else board,
                              [relay("lamp", 4)]))
        run(b.edit_board("pm"))
        frame = io.frames[-1]
        check(f"{board.family_label}: edit frame draws that board",
              frame["catalogue"]["board"] == board.id
              and [p["n"] for p in frame["catalogue"]["pins"]]
              == list(board.usable), frame["catalogue"]["board"])
        check(f"{board.family_label}: pins_owned_by_module is its PSRAM set",
              frame["pins_owned_by_module"] == owned,
              frame["pins_owned_by_module"])
    fresh_dir()
    io = FakeIO(pin_map={"cancelled": True})
    b = builder(io)
    b._write_board(record("known", S3, [relay("lamp", 4)], psram=False))
    run(b.edit_board("known"))
    check("an S3 known to have no octal PSRAM owns nothing",
          io.frames[-1]["pins_owned_by_module"] == [])
    check("describe(board) per family has that family's pins",
          [p["n"] for p in cat.describe(C3)["pins"]] == list(C3.usable)
          and cat.describe(S3)["board"] == "esp32s3")


# ── 4. the board question ──────────────────────────────────────────────────

def which_board():
    print("\nwhich board")
    fresh_dir()
    io = FakeIO(answers=["esp32c3"],
                pin_map={"items": [relay("lamp", 4)]})
    io.typed = ["Porch"]
    b = builder(io)

    async def wifi(**_k):
        return {"ssid": "home"}

    async def granted(_r):
        return True

    async def cable():
        return C3
    b._ensure_wifi = wifi
    b.grant_access = granted
    b._peek_cable_family = cable
    ok = run(b.menu_add_esp_node())
    prompt, rows = io.asked[0]
    check("a new board is asked which kind it is",
          ok and "board" in prompt.lower(), prompt)
    check("every NAMED board is offered (the 'Any ...' lists are not)",
          {r["value"] for r in rows}
          == {x.id for x in chips.pickable_boards()}
          and not any(chips.is_generic(chips.get(r["value"])) for r in rows))
    check("the cable's family comes first, marked",
          rows[0]["value"] == "esp32c3-devkitm-1"
          and "(on the cable now)" in rows[0]["label"]
          and not any("(on the cable now)" in r["label"] for r in rows[1:]),
          rows[:2])
    check("each row carries its family",
          all(chips.get(r["value"]).family_label in r["sub"] for r in rows))
    saved = b._read_board("porch")
    check("the record stores the chosen board",
          saved and saved.get("board") == "esp32c3"
          and saved.get("board_family") == "esp32c3", saved)
    check("the pin map drew the chosen board",
          io.frames[0]["catalogue"]["board"] == "esp32c3"
          and io.frames[0]["pins_owned_by_module"] == [])

    io = FakeIO(answers=[lambda rows: rows[0]["value"]],
                pin_map={"cancelled": True})
    io.typed = ["Hall"]
    b = builder(io)
    b._ensure_wifi = wifi

    async def no_cable():
        return None
    b._peek_cable_family = no_cable
    run(b.menu_add_esp_node())
    check("no cable: the classic DevKit is first and nothing is marked",
          io.asked[0][1][0]["value"] == CLASSIC.id
          and not any("cable" in r["label"] for r in io.asked[0][1]))


# ── 5. changing a board's type ─────────────────────────────────────────────

def change_type():
    print("\nchange board type")
    fresh_dir()
    io = FakeIO()
    b = builder(io)
    b._write_board(record("k", None, [relay("lamp", 25)]))
    changed = run(b.change_board_type("k", C3))
    check("a placement the new board lacks is refused",
          not changed and "GPIO25" in io.text()
          and b._read_board("k").get("board") is None, io.text())
    b._write_board(record("k", None, [relay("lamp", 4)], flashed="2026",
                          chip="ESP32-D0WD-V3", psram=False))
    changed = run(b.change_board_type("k", C3))
    rec = b._read_board("k")
    check("a fitting change is saved",
          changed and rec["board"] == "esp32c3", rec)
    check("another family forgets the old chip's facts and needs a reinstall",
          rec["flashed"] == "" and "chip" not in rec and "psram" not in rec)
    b._write_board(record("s", None, [relay("lamp", 4)]))
    changed = run(b.change_board_type("s", S3))
    check("classic -> S3 with GPIO4 fits", changed)
    b._write_board(record("r", None, [relay("lamp", 35)]))
    io.said.clear()
    changed = run(b.change_board_type("r", S3))
    check("a pin the S3's PSRAM may own: the change goes through (the "
          "board is asked at install)", changed, io.text())


# ── 6. flashing: the cable double-checks, per-family image and chip ────────

FAMILY_DIR = Path(tempfile.mkdtemp())


def flasher(rec, chip, features=""):
    fresh_dir()
    io = FakeIO()
    b = builder(io)
    b._write_board(rec)
    runs = []

    async def ready():
        return b.WIFI_READY

    async def yes():
        return True

    async def port():
        return "/dev/ttyUSB0"

    async def free(_p, _r):
        return ""

    async def identify(_port, **_k):
        b._chip_features = features
        return chip

    async def esptool(args, cwd, *, busy):
        runs.append(args)
        return 1 if "write_flash" in args else 0   # stop after the write
    b._wifi_ready_here = ready
    b._say_if_plaintext = yes
    b._pick_port = port
    b._check_chip_is_free = free
    b._identify_chip = identify
    b._run_esptool = esptool
    b._mqtt_settings = lambda: {"host": "10.0.0.2", "port": "8883",
                                "tls": "1"}
    return b, io, runs


def flashing():
    print("\nflashing")
    config.TASMOTA_FAMILY_FIRMWARE_DIR = str(FAMILY_DIR)
    for fam in (S3, C3):
        (FAMILY_DIR / fam.firmware).write_bytes(b"\xff" * 64)

    b, io, runs = flasher(record("c", C3, [relay("lamp", 4)]), "ESP32-S3")
    run(b._flash_node_inner("c"))
    check("C3 chosen, S3 on the cable: nothing is erased or written",
          runs == [], runs)
    check("the refusal names both families",
          "You chose an ESP32-C3 board but the cable has an ESP32-S3 -- "
          "nothing was erased" in io.text(), io.text())

    b, io, runs = flasher(record("c", C3, [relay("lamp", 4)]), "ESP32-C3")
    run(b._flash_node_inner("c"))
    erase, write = (runs + [[], []])[:2]
    check("a matching chip is erased with --chip esp32c3",
          erase[:2] == ["--chip", "esp32c3"] and "erase_flash" in erase, runs)
    check("the C3's own factory image is written at 0x0",
          "write_flash" in write
          and write[write.index("-z") + 1] == "0x0"
          and write[-1] == str(FAMILY_DIR / C3.firmware), write)

    b, io, runs = flasher(record("o", None, [relay("lamp", 26)]),
                          "ESP32-C3")
    config_path = config.TASMOTA_FIRMWARE_PATH
    tmp_classic = Path(tempfile.mkdtemp()) / "tasmota32.factory.bin"
    tmp_classic.write_bytes(b"\xff" * 64)
    config.TASMOTA_FIRMWARE_PATH = str(tmp_classic)
    try:
        run(b._flash_node_inner("o"))
    finally:
        config.TASMOTA_FIRMWARE_PATH = config_path
    check("an OLD record is classic: a C3 on the cable is refused",
          runs == [] and "You chose an ESP32 (classic) board but the cable "
          "has an ESP32-C3" in io.text(), io.text())

    b, io, runs = flasher(record("o2", None, [relay("lamp", 26)]),
                          "ESP32-D0WD-V3")
    config.TASMOTA_FIRMWARE_PATH = str(tmp_classic)
    try:
        run(b._flash_node_inner("o2"))
    finally:
        config.TASMOTA_FIRMWARE_PATH = config_path
    check("an old record on a classic chip flashes with --chip esp32",
          runs and runs[0][:2] == ["--chip", "esp32"]
          and runs[1][-1] == str(tmp_classic), runs)

    b, io, runs = flasher(record("s", S3, [relay("lamp", 4)]), "ESP32-S3",
                          "Wi-Fi, BT 5 (LE), Dual Core, 240MHz, "
                          "Embedded PSRAM 8MB (AP_3v3)")
    run(b._flash_node_inner("s"))
    rec = b._read_board("s")
    check("an S3 with embedded 8 MB PSRAM is learned at the cable",
          rec.get("psram") is True and "GPIO33-37" in io.text(), rec)

    (FAMILY_DIR / C3.firmware).unlink()
    b, io, runs = flasher(record("c", C3, [relay("lamp", 4)]), "ESP32-C3")
    run(b._flash_node_inner("c"))
    check("no C3 image: a clear sentence, nothing started",
          runs == [] and "no ESP32-C3 firmware" in io.text()
          and "GitHub workflow" in io.text() and "stock" in io.text(),
          io.text())
    (FAMILY_DIR / C3.firmware).write_bytes(b"\xff" * 64)

    b = builder()
    check("the family image is the family's file",
          b._firmware(S3) == FAMILY_DIR / S3.firmware)
    check("the classic search is untouched by family images",
          b._firmware() == b._firmware(CLASSIC)
          and (b._firmware() is None
               or b._firmware().name == "tasmota32.factory.bin"))


def firmware_notes():
    print("\nfirmware notes per family")
    fresh_dir()
    config.TASMOTA_FAMILY_FIRMWARE_DIR = str(FAMILY_DIR)
    (FAMILY_DIR / S3.firmware).write_bytes(b"\xff" * 64 + b"s3")
    (FAMILY_DIR / C3.firmware).write_bytes(b"\xff" * 64 + b"c3")
    b = builder()
    b._remember_firmware_drivers([], ["RC522"], S3)
    check("the S3 file is known to lack RC522",
          b._firmware_lacks(["RC522"], S3) == ["RC522"])
    check("the C3 file is not judged by the S3's note",
          b._firmware_lacks(["RC522"], C3) == [])


# ── 7. S3 PSRAM learning ───────────────────────────────────────────────────

def psram():
    print("\nPSRAM learning")
    line = lambda kb: [f'{{"StatusMEM":{{"PsrMax":{kb},"PsrFree":1}}}}']  # noqa: E731
    check("S3 PsrMax 8 MB -> owns 33-37",
          EspBuilder._psram_answer(line(8189), S3) is True)
    check("S3 PsrMax 16 MB -> owns 33-37",
          EspBuilder._psram_answer(line(16384), S3) is True)
    check("S3 PsrMax 2 MB (quad) -> free",
          EspBuilder._psram_answer(line(2048), S3) is False)
    check("S3 PsrMax 0 -> free", EspBuilder._psram_answer(line(0), S3) is False)
    check("S3 silence stays None", EspBuilder._psram_answer([], S3) is None)
    check("classic keeps its rule: any PSRAM owns 16/17",
          EspBuilder._psram_answer(line(2048)) is True
          and EspBuilder._psram_answer(line(0)) is False)
    f = EspBuilder._psram_from_chip
    check("esptool 'Embedded PSRAM 8MB' -> True",
          f("ESP32-S3", "Wi-Fi, BLE, Embedded PSRAM 8MB (AP_3v3)", S3) is True)
    check("esptool 'ESP32-S3R8' -> True", f("ESP32-S3R8", "", S3) is True)
    check("esptool 'Embedded PSRAM 2MB' -> False",
          f("ESP32-S3", "Embedded PSRAM 2MB", S3) is False)
    check("esptool silent about PSRAM -> None",
          f("ESP32-S3", "Wi-Fi, BLE, Embedded Flash 8MB", S3) is None)
    check("not an S3 -> None", f("ESP32-C3", "Embedded PSRAM 8MB", C3) is None)


# ── 8. OTA per family ──────────────────────────────────────────────────────

def factory_image(marker: bytes) -> bytes:
    """A minimal factory image: a partition table at 0x8000 with one ota_0
    app at 0x10000, holding a one-segment ESP image carrying `marker`."""
    seg = marker.ljust(16, b"\0")[:16]
    app = bytes([0xE9, 1]) + b"\0" * 21 + b"\0"          # 24-byte header
    app += struct.pack("<II", 0x3F400020, len(seg)) + seg
    app += b"\0" * (64 - len(app))                        # checksum + pad
    image = bytearray(b"\xff" * (0x10000 + 0x1000))
    entry = struct.pack("<HBBII", ota.PARTITION_MAGIC, ota.PART_TYPE_APP,
                        ota.PART_SUBTYPE_OTA_0, 0x10000, 0x1000)
    image[0x8000:0x8000 + 32] = entry + b"app0".ljust(16, b"\0") + b"\0" * 4
    image[0x10000:0x10000 + len(app)] = app
    return bytes(image)


async def fetch(port, path):
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    writer.write(f"GET {path} HTTP/1.1\r\nHost: x\r\n\r\n".encode())
    await writer.drain()
    data = await reader.read()
    writer.close()
    head, _, body = data.partition(b"\r\n\r\n")
    return int(head.split()[1]), body


def ota_paths():
    print("\nOTA per family")
    folder = Path(tempfile.mkdtemp())
    classic = folder / "tasmota32.factory.bin"
    classic.write_bytes(factory_image(b"CLASSIC"))
    (folder / S3.firmware).write_bytes(factory_image(b"S3-IMAGE"))

    def factory_of(board):
        return str(classic if board.family == "esp32"
                   else folder / board.firmware)

    async def serve():
        server = ota.OtaServer(ota.family_images(factory_of, say=lambda m: None),
                               0, host="127.0.0.1", say=lambda m: None)
        await server.start()
        try:
            return {p: await fetch(server.port, p) for p in (
                "/firmware/tasmota32.bin", "/firmware/tasmota32s3.bin",
                "/firmware/tasmota32c3.bin", "/firmware/other.bin")}
        finally:
            await server.stop()
    got = run(serve())
    check("the classic path is unchanged and serves the classic app",
          ota.OTA_PATH == "/firmware/tasmota32.bin"
          and got["/firmware/tasmota32.bin"][0] == 200
          and b"CLASSIC" in got["/firmware/tasmota32.bin"][1])
    check("the S3 path serves the S3 app, cut from its factory image",
          got["/firmware/tasmota32s3.bin"][0] == 200
          and b"S3-IMAGE" in got["/firmware/tasmota32s3.bin"][1]
          and len(got["/firmware/tasmota32s3.bin"][1]) == 64
          and (folder / "tasmota32s3.bin").is_file())
    check("a family with no image answers 503",
          got["/firmware/tasmota32c3.bin"][0] == 503)
    check("anything else is 404", got["/firmware/other.bin"][0] == 404)
    check("a single OtaImage still serves the classic path",
          ota.OtaServer(ota.OtaImage(str(classic), say=lambda m: None),
                        0).image is not None)

    # The URLs boards are handed.
    fresh_dir()
    b = builder()
    b.hub_address = lambda: "10.0.0.2"
    saved_url, saved_port = config.OTA_URL, config.OTA_HTTP_PORT
    config.OTA_URL, config.OTA_HTTP_PORT = None, 8766
    config.TASMOTA_FAMILY_FIRMWARE_DIR = str(folder)
    saved_classic = config.TASMOTA_FIRMWARE_PATH
    config.TASMOTA_FIRMWARE_PATH = str(classic)
    try:
        b._write_board(record("s3b", S3))
        b._write_board(record("c3b", C3))
        b._write_board(record("oldb"))
        check("an S3 is pointed at its family's path",
              b._ota_url(b._read_board("s3b"))
              == "http://10.0.0.2:8766/firmware/tasmota32s3.bin")
        check("an old record keeps the classic path",
              b._ota_url(b._read_board("oldb"))
              == "http://10.0.0.2:8766/firmware/tasmota32.bin"
              and b._ota_url() == b._ota_url(b._read_board("oldb")))
        check("the heal URL by topic is the S3's",
              b._ota_heal_url("hv-s3b").endswith("/tasmota32s3.bin"))
        check("no C3 image: its heal URL is empty (flash over the cable)",
              b._ota_heal_url("hv-c3b") == "")
        sent = []
        healer = ota.SafebootHealer(lambda t, v: sent.append((t, v)),
                                    b._ota_heal_url, say=lambda m: None)
        healer.heal("hv-s3b", "safeboot")
        check("SafebootHealer sends each board its family's URL",
              ("cmnd/hv-s3b/OtaUrl",
               "http://10.0.0.2:8766/firmware/tasmota32s3.bin") in sent, sent)
        old_style = ota.SafebootHealer(lambda t, v: sent.append((t, v)),
                                       lambda: "http://h/x", say=lambda m: None)
        check("a url_of() with no argument still works",
              old_style.heal("hv-any", "safeboot"))
        b._wifi_ready = None
        b._read_wifi = lambda: {"ssid": "home", "password": "pw"}
        b._mqtt_settings = lambda: {"host": "10.0.0.2", "port": "1883",
                                    "tls": "0"}
        lines = " ".join(b._provisioning_commands(b._read_board("s3b")))
        check("provisioning an S3 sends its family's OtaUrl",
              "OtaUrl http://10.0.0.2:8766/firmware/tasmota32s3.bin" in lines)
    finally:
        config.OTA_URL, config.OTA_HTTP_PORT = saved_url, saved_port
        config.TASMOTA_FIRMWARE_PATH = saved_classic


# ── 9. setup downloads what flashing expects ───────────────────────────────

def setup_assets():
    print("\nsetup assets")
    from app import setup_manager as sm
    paths = {a["path"]: a["url"] for a in sm.URL_ASSETS}
    for fam in chips.FAMILIES.values():
        want = config.firmware_path_for(fam)
        check(f"setup downloads {fam.family_label}'s image where flashing "
              f"looks", want in paths and paths[want].endswith(fam.firmware),
              want)


# ── the Wi-Fi follows the hub; a DevKit V1's TX2/RX2 are free ─────────────

def wifi_follows_hub():
    print("\nthe boards' Wi-Fi follows the network this hub is on")
    from esp_firmware import esp_builder as EB
    net = EB.hub_net
    saved = {k: getattr(net, k) for k in (
        "holds_address", "active_wifi", "profile_ssid", "saved_password")}
    saved_detect = EspBuilder._detect_address
    seen = {"psk": ""}

    async def no_cert(self, address):
        return None
    saved_cert = EspBuilder._broker_cert_must_name
    EspBuilder._broker_cert_must_name = no_cert
    try:
        net.holds_address = lambda a: a == "192.168.1.50"
        net.active_wifi = lambda device="wlan0": "homevoice-router"
        net.profile_ssid = lambda profile: "Marc4@Airtel_C6D3"
        net.saved_password = lambda profile: seen["psk"]
        EspBuilder._detect_address = classmethod(lambda cls: "192.168.1.50")

        fresh_dir()
        io = FakeIO()
        b = builder(io)
        b._write_json(b._wifi_path(), {"ssid": "Marc4@Airtel_C6D3",
                                       "password": "pw", "hub_address": "192.168.1.9"})
        state = run(b._wifi_ready_here())
        check("same Wi-Fi, new address: saved, nothing asked",
              state == b.WIFI_READY and io.asked == [] and b._read_wifi()
              == {"ssid": "Marc4@Airtel_C6D3", "password": "pw",
                  "hub_address": "192.168.1.50"}, (state, io.asked, b._read_wifi()))

        fresh_dir()
        io = FakeIO()
        io.typed = ["newpass"]
        b = builder(io)
        b._write_json(b._wifi_path(), {"ssid": "OldNet", "password": "old",
                                       "hub_address": "10.0.0.7"})
        state = run(b._wifi_ready_here())
        check("another Wi-Fi: only its password asked, then saved",
              state == b.WIFI_READY and io.asked == [] and io.typed == []
              and b._read_wifi()["ssid"] == "Marc4@Airtel_C6D3"
              and b._read_wifi()["password"] == "newpass", (state, b._read_wifi()))

        fresh_dir()
        seen["psk"] = "frommachine"
        io = FakeIO()
        b = builder(io)
        b._write_json(b._wifi_path(), {"ssid": "OldNet", "password": "old",
                                       "hub_address": "10.0.0.7"})
        run(b._wifi_ready_here())
        check("...or read from this machine's saved Wi-Fi when it says",
              b._read_wifi()["password"] == "frommachine", b._read_wifi())

        fresh_dir()
        net.active_wifi = lambda device="wlan0": ""
        io = FakeIO(answers=["stop"])
        b = builder(io)
        b._write_json(b._wifi_path(), {"ssid": "OldNet", "password": "old",
                                       "hub_address": "10.0.0.7"})
        state = run(b._wifi_ready_here())
        check("not on Wi-Fi: the old question, record untouched",
              state == b.WIFI_STOP and io.asked
              and b._read_wifi()["hub_address"] == "10.0.0.7", (state, io.asked))
    finally:
        for k, v in saved.items():
            setattr(net, k, v)
        EspBuilder._detect_address = saved_detect
        EspBuilder._broker_cert_must_name = saved_cert

    print("\nTX2/RX2 before the board is met: allowed, asked at install")
    plan = cat.BoardPlan("V1", board=chips.ESP32_DEVKIT)
    try:
        plan.add("pzem", "meter", {"tx": 17, "rx": 16})
        ok = True
    except cat.PlacementError:
        ok = False
    check("power meter on TX2/RX2, PSRAM not known yet: allowed", ok)
    io = FakeIO()
    b = builder(io)
    stopped = run(b._psram_unknown_stop(plan))
    check("...but a wiring push stops while the board has not said",
          stopped and "meter" in io.text() and "GPIO16" in io.text(), io.text())
    plan2 = cat.BoardPlan("V1", owns_psram_pins=False, board=chips.ESP32_DEVKIT)
    plan2.add("pzem", "meter", {"tx": 17, "rx": 16})
    check("...and goes once it said no PSRAM",
          not run(builder()._psram_unknown_stop(plan2)))
    plan3 = cat.BoardPlan("V1", owns_psram_pins=True, board=chips.ESP32_DEVKIT)
    try:
        plan3.add("relay", "lamp", {"pin": 17})
        ok = False
    except cat.PlacementError:
        ok = True
    check("a board that SAID its RAM is on 16/17: refused", ok)


def waits_for_the_cable():
    print("\nno board on a cable yet: the flash waits for it")
    from esp_firmware import esp_builder as EB
    looks = {"n": 0}

    async def no_sleep(_s):
        looks["n"] += 1
    saved = (EB._port_sleep, EspBuilder._serial_ports, EspBuilder.PORT_WAIT_S)
    try:
        EB._port_sleep = no_sleep
        port = {"value": "/dev/ttyUSB0", "label": "ttyUSB0", "sub": "CP2102"}
        EspBuilder._serial_ports = staticmethod(
            lambda: [port] if looks["n"] >= 3 else [])
        io = FakeIO()
        got = run(builder(io)._pick_port())
        check("plugged in while it waits: that port is used, nothing to continue later",
              got == "/dev/ttyUSB0" and "waiting" in io.text(), (got, io.text()))
        looks["n"] = -10**9
        EspBuilder.PORT_WAIT_S = 0.0
        io = FakeIO()
        got = run(builder(io)._pick_port())
        check("never plugged in: said plainly, charging-only cables named",
              got is None and "charging-only" in io.text(), io.text())
    finally:
        EB._port_sleep, EspBuilder._serial_ports, EspBuilder.PORT_WAIT_S = saved


def esptool_from_this_python():
    print("\nesptool is run through the hub's own Python")
    import importlib.util
    saved = importlib.util.find_spec
    try:
        importlib.util.find_spec = lambda name, *a: object() \
            if name == "esptool" else saved(name, *a)
        cmd = EspBuilder._esptool()
    finally:
        importlib.util.find_spec = saved
    check("installed for this Python (pip --user too): python -m esptool, "
          "no PATH needed", cmd == [sys.executable, "-m", "esptool"], cmd)


def no_powerdelta_before_wiring():
    print("\nPowerDelta waits for the meter's pins")
    fresh_dir()
    b = builder()
    b.broker_fingerprint = lambda: "AA BB"
    rec = record("dining", items=[{"type_id": "pzem", "name": "meter",
                                   "pins": {"tx": 17, "rx": 16}}])
    sent = " ".join(b._provisioning_commands(rec))
    check("install settings: no PowerDelta (Unknown before the template), "
          "TelePeriod kept", "PowerDelta" not in sent and "TelePeriod" in sent, sent)
    check("...and it is re-asserted once the board is back",
          any(c.startswith("PowerDelta") for c in EspBuilder.maintenance_commands(rec)))


def retired_chip_is_reused():
    print("\na chip from a board this hub retired is reused without asking")
    fresh_dir()
    io = FakeIO(answers=["yes"])
    b = builder(io)
    b._write_board(record("kitchen", items=[relay("lamp", 4)], topic="hv-kitchen"))
    b.delete_node("kitchen")
    new = record("dining", items=[relay("lamp", 4)], topic="hv-dining")

    async def says(topic):
        return {"topic": topic, "name": "Kitchen"}
    b._read_identity = lambda port: says("hv-kitchen")
    got = run(b._check_chip_is_free("/dev/ttyUSB0", new))
    check("the old Kitchen chip becomes Dining: no question",
          got == "" and io.asked == [] and "retired earlier" in io.text(),
          (got, io.asked, io.text()))
    b._read_identity = lambda port: says("someone-else")
    got = run(b._check_chip_is_free("/dev/ttyUSB0", new))
    check("a chip this hub never knew still asks first",
          got == "" and len(io.asked) == 1, io.asked)


def install_owns_the_board():
    print("\nmid-install: the engine leaves the board to the cable")
    from nlu.automations import AutomationEngine
    fresh_dir()
    b = builder()
    b._write_board(record("dining", topic="hv-dining",
                          items=[{"type_id": "pzem", "name": "meter",
                                  "pins": {"tx": 17, "rx": 16}}]))
    eng = AutomationEngine.__new__(AutomationEngine)
    eng.esp = b
    check("not installed yet: hands off", eng._mid_install("hv-dining"))
    rec = b._read_board("dining")
    rec["flashed"] = "2026-11-02"
    b._write_board(rec)
    check("installed: the engine looks after it again",
          not eng._mid_install("hv-dining"))
    check("a topic it does not know is not held", not eng._mid_install("hv-x"))


def unfinished_work_waits_in_notifications():
    print("\nunfinished work waits in Notifications, not in Layouts")
    from app.shell import InteractiveShell
    fresh_dir()
    io = FakeIO()
    b = builder(io)
    lay = b.save_layout("Living", CLASSIC, [relay("lamp", 4)])
    b._write_board(record("living", items=[relay("lamp", 4)]))
    check("a board not installed: its wiring is pending, not a layout",
          b.is_pending(next(r for r in b.list_layouts()
                            if r.get("on_board") == "living")))
    b.delete_node("living")
    check("removed before it was ever installed: no copy left behind",
          [r["id"] for r in b.list_layouts()] == [lay["id"]],
          [r["name"] for r in b.list_layouts()])
    b._write_board(record("kitchen", items=[relay("lamp", 4)],
                          flashed="2026-10-01"))
    b.delete_node("kitchen")
    check("an installed board removed, its plan already saved: still one",
          [r["id"] for r in b.list_layouts()] == [lay["id"]],
          [r["name"] for r in b.list_layouts()])
    b._write_board(record("bench", items=[relay("lamp", 4)],
                          flashed="2026-10-01"))
    first = b.save_layout("Bench (not sent)", CLASSIC, [relay("lamp", 5)],
                          state="not_sent", **b._pending_for("bench"))
    again = b.save_layout("Bench (not sent)", CLASSIC, [relay("lamp", 6)],
                          state="not_sent", **b._pending_for("bench"))
    pend = b.pending_layouts()
    check("an edit that fails twice is ONE pending item, the newest wiring",
          len(pend) == 1 and first["id"] == again["id"]
          and pend[0]["items"][0]["pins"]["pin"] == 6, pend)

    sh = InteractiveShell.__new__(InteractiveShell)
    sh.esp, sh._io = b, io
    recs = InteractiveShell._layout_records(sh)
    check("Layouts shows the plan, not the pending edit",
          [r["id"] for r in recs if r["on_board"] == ""] == [lay["id"]],
          [r["name"] for r in recs])
    notice = InteractiveShell._layout_notice(sh, pend[0])
    check("its notice: Continue / delete, sending the wiring to Bench",
          notice["action_value"] == f"resumelayout:{pend[0]['id']}"
          and "Bench" in notice["title"], notice)
    io.answers = ["delete"]
    run(InteractiveShell.menu_resume_layout(sh, pend[0]["id"]))
    check("Delete forgets it; the board keeps its wiring",
          b.pending_layouts() == []
          and b._read_board("bench")["items"] == [relay("lamp", 4)])


if __name__ == "__main__":
    old_records()
    templates()
    pin_maps()
    which_board()
    change_type()
    flashing()
    firmware_notes()
    psram()
    ota_paths()
    setup_assets()
    wifi_follows_hub()
    waits_for_the_cable()
    esptool_from_this_python()
    no_powerdelta_before_wiring()
    retired_chip_is_reused()
    install_owns_the_board()
    unfinished_work_waits_in_notifications()
    print(f"\n  {PASSED} passed, {FAILED} failed")
    sys.exit(1 if FAILED else 0)

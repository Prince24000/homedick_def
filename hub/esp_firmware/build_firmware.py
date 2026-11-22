#!/usr/bin/env python3
"""Build HomeVoice's Tasmota on THIS machine -- the offline road.

    python esp_firmware\\build_firmware.py            (Windows)
    python3 esp_firmware/build_firmware.py            (Pi / Linux)
    python3 esp_firmware/build_firmware.py --family esp32c3 --family esp32s3
    python3 esp_firmware/build_firmware.py --all      (every ESP32 family)

What it does, in order, refusing loudly at the first thing that is not
right rather than handing over a binary that quietly lacks something:

  1. Finds PlatformIO (the toolchain: a one-time ~1-2 GB install done on
     good Wi-Fi; after that this whole flow is offline).
  2. Finds the Tasmota source tree and checks its version against the one
     this project's option numbers were verified on.
  3. Assembles the build: the override from esp_firmware/.
  4. Builds tasmota32 -- or each family asked for (--family / --all):
     tasmota32s3, tasmota32c3, ... one after another, each on its own.
  5. PROVES the result: greps the linker map for every driver the build
     was asked to carry -- the standard HomeVoice set. A missing driver
     fails ITS OWN line. This is the
     same check the GitHub workflow runs; a binary is blessed by
     evidence, wherever it was built.
  6. Installs both images into esp_firmware/firmware/, side by side (the
     factory image under the exact name the hub flashes,
     tasmota32<family>.factory.bin; the app image as tasmota32<family>.bin)
     and prints what changed. A family whose
     proof fails installs nothing; the others still do. The hub's
     known-lacking notes are keyed to the file's hash, so they clear
     themselves the moment the file changes.

"""

import argparse
import hashlib
import os
import platform
import re
import shutil
import subprocess
import sys
import sysconfig
import time
from pathlib import Path
from typing import Dict, Iterable, List, NoReturn, Optional, Sequence, Tuple

HERE = Path(__file__).resolve().parent          # esp_firmware/
PROJECT = HERE.parent                            # voice_ai/

# The board profiles. chips.py imports nothing from the project, so the
# stand-alone script can load it from beside itself.
try:
    from . import chips
except ImportError:
    sys.path.insert(0, str(HERE))
    import chips                                                  # type: ignore[no-redef]

#: The version the project's Tasmota option numbers (SetOption103/132/19)
#: were verified against. A different source tree gets a WARNING, not a
#: refusal -- rebuilding on a new version is a deliberate act, and the
#: warning names what must be re-verified.
EXPECTED_VERSION = "15.5.0"

#: Every driver the HomeVoice override asks for, and the trace it must
#: leave in the linker map. ci/prove.py (the fork's GitHub workflow) carries
#: a copy of this proof; tests/test_cloud_build.py fails when they differ.
#: Each marker is the driver's OWN ENTRY FUNCTION as the linker writes it:
#: `bool Xsns80(uint32_t)` becomes the section `.text._Z6Xsns80j` (the map
#: is lowercased before matching; a demangling linker writes
#: `Xsns80(unsigned int)`). That function only exists when the driver's
#: `#ifdef USE_...` was true, so it is a proof. The names used before --
#: "mfrc522", "tm1637", "max7219", "mpu6050" -- were NOT: the dependency
#: finder compiles a library the moment a header names it (chain mode
#: does not read #ifdef), so the library's objects and the GPIO enum
#: text put those words in the map of a build that has no such driver.
#: Found out on 10 Sep 2026: a build "proven" to carry TM1637 answered a
#: bare `GPIOs` with no "TM1637 CLK" and carried none of the driver's
#: text. Two MAX7219 drivers exist -- the seven-segment one lives in
#: xdsp_15 with TM1637 (USE_DISPLAY_MAX7219), the dot-matrix one in
#: xdsp_19 (USE_DISPLAY_MAX7219_MATRIX); this build wants the matrix.
#: The entry function is `bool XsnsNN(uint32_t)`. Its mangled name ends in
#: the letter for uint32_t, and that letter changed: `j` (unsigned int) on
#: the old toolchain, `m` (unsigned long) since ESP-IDF 5 on EVERY family.
#: Found 24 Sep 2026: with only `j`, the first all-families run "missed"
#: all five drivers on S2 and S3 -- builds that were fine. Both accepted.
REQUIRED_MARKERS = {
    "RC522 card reader (Xsns80)": r"_z6xsns80[jm]|xsns80\(",
    "Display core (Xdrv13)": r"_z6xdrv13[jm]|xdrv13\(",
    "Character LCD (Xdsp01)": r"_z6xdsp01[jm]|xdsp01\(",
    "TM1637 seven-segment (Xdsp15)": r"_z6xdsp15[jm]|xdsp15\(",
    "MAX7219 dot matrix (Xdsp19)": r"_z6xdsp19[jm]|xdsp19\(",
    "MPU6050 IMU (Xsns32)": r"_z6xsns32[jm]|xsns32\(",
    # The FULL IR driver: only xdrv_05_irremote_full has the IRhvac command
    # (the small driver has IRsend but no IRhvac).
    "Full IR, raw send (IRhvac)": r"cmndirhvac",
}

FACTORY = "tasmota32.factory.bin"
OTA = "tasmota32.bin"


# ── the families a build is for ───────────────────────────────────────

def target_board(family: str) -> "chips.Board":
    """The profile a family is built and pin-checked as: the classic
    DevKit for the classic ESP32 (exactly the hub's historical rules), the
    family's generic profile otherwise."""
    if family == chips.DEFAULT.family:
        return chips.DEFAULT
    return chips.FAMILIES[family]


#: Every family, classic first (chips.FAMILIES' order).
ALL_TARGETS: Tuple["chips.Board", ...] = tuple(target_board(f) for f in chips.FAMILIES)
#: What a build is for when nobody says: the classic board, as always.
CLASSIC: Tuple["chips.Board", ...] = (chips.DEFAULT,)


def parse_families(names: Iterable[str]) -> Tuple["chips.Board", ...]:
    """Family names from a person into target boards, in the catalogue's
    order, once each. Takes the family id (esp32c3), Tasmota's env name
    (tasmota32c3) or the short form (c3, classic). Unknown names refuse."""
    wanted = set()
    for raw in names:
        name = str(raw).strip().lower()
        for board in ALL_TARGETS:
            short = board.family[len("esp32"):] or "classic"
            if name in (board.family, board.pio_env, short):
                wanted.add(board.family)
                break
        else:
            fail(f"'{raw}' is not an ESP32 family -- one of "
                 + ", ".join(b.family for b in ALL_TARGETS) + ".")
    return tuple(b for b in ALL_TARGETS if b.family in wanted) or CLASSIC


def families_of(records: Iterable[Dict[str, object]],
                board_of=None) -> Tuple["chips.Board", ...]:
    """The families of the boards a hub has on record, as target boards.
    A record with no "board" field is the classic board (chips.get), and a
    hub with no boards at all builds for the classic one, as always.
    `board_of`: the hub's own record -> profile reader, when it has one
    (it also knows a record whose board file has since gone)."""
    seen = set()
    for record in records:
        if not isinstance(record, dict):
            continue
        board = (board_of(record) if board_of is not None
                 else chips.get(str(record.get("board") or "") or None))
        seen.add(board.family)
    return tuple(b for b in ALL_TARGETS if b.family in seen) or CLASSIC


def family_names(boards: Sequence["chips.Board"]) -> str:
    return ", ".join(b.family_label for b in boards)


#: The chip id in an ESP32 app image's header (esp_image_header_t, bytes
#: 12-13, ESP-IDF's esp_chip_id_t). A classic image on a C3 does not boot.
IMAGE_CHIP_IDS = {"esp32": 0x0000, "esp32s2": 0x0002, "esp32c3": 0x0005,
                  "esp32s3": 0x0009, "esp32c2": 0x000C, "esp32c6": 0x000D}


def image_chip_problem(app: bytes, board: "chips.Board") -> str:
    """Why this app image is not for this board's family, or ""."""
    if len(app) < 14:
        return "too short to be an ESP32 app image"
    got = int.from_bytes(app[12:14], "little")
    want = IMAGE_CHIP_IDS.get(board.family)
    if want is None or got == want:
        return ""
    other = next((f for f, n in IMAGE_CHIP_IDS.items() if n == got), "")
    label = target_board(other).family_label if other else f"chip id {got}"
    return f"it is built for {label}, not {board.family_label}"


def firmware_names() -> Dict[str, Tuple["chips.Board", str]]:
    """Every image name the hub accepts: {file name: (board, "factory" |
    "app")} -- tasmota32c3.factory.bin, tasmota32c3.bin, ..."""
    out: Dict[str, Tuple["chips.Board", str]] = {}
    for board in ALL_TARGETS:
        out[board.firmware] = (board, "factory")
        out[board.ota_name] = (board, "app")
    return out


def say(text: str) -> None:
    print(text, flush=True)


class BuildError(RuntimeError):
    """A refusal with its reason. The CLI prints it and exits 1; the hub
    catches it where it uses this file and says it in the flow."""


def fail(text: str) -> NoReturn:
    raise BuildError(text)


# ── finding the tools ──────────────────────────────────────────────────

def find_pio() -> str:
    """PlatformIO, wherever it is. None of the fallbacks invent one.

    Beyond PATH it looks where a `pip install --user platformio` puts the
    script (often not on PATH, which is why a fresh install looked absent)
    and where PlatformIO's own installer keeps it."""
    for name in ("pio", "platformio"):
        found = shutil.which(name)
        if found:
            return found
    folders = []
    for scheme in (None, f"{os.name}_user"):
        try:
            folders.append(Path(sysconfig.get_path("scripts", scheme)
                                if scheme else sysconfig.get_path("scripts")))
        except KeyError:
            continue
    penv = Path.home() / ".platformio" / "penv"
    folders += [penv / "bin", penv / "Scripts"]
    for folder in folders:
        for name in ("pio", "pio.exe", "platformio", "platformio.exe"):
            candidate = folder / name
            if candidate.is_file():
                return str(candidate)
    return ""


def install_platformio() -> Tuple[bool, str]:
    """Install PlatformIO once, for this user. (ok, last lines of pip)."""
    in_venv = sys.prefix != sys.base_prefix
    cmd = [sys.executable, "-m", "pip", "install", "--upgrade", "platformio"]
    if not in_venv:
        cmd.insert(4, "--user")
    out = ""
    for attempt in (cmd, cmd + ["--break-system-packages"]):
        try:
            done = subprocess.run(attempt, capture_output=True, text=True,
                                  timeout=1800)
        except (OSError, subprocess.TimeoutExpired) as exc:
            return False, str(exc)
        out = (done.stdout or "") + (done.stderr or "")
        # Debian/Raspberry Pi OS refuse pip outside a venv unless told the
        # user-site install is intended ("externally-managed-environment").
        if done.returncode == 0 or "externally-managed" not in out:
            break
    tail = "\n".join(out.strip().splitlines()[-3:])
    return bool(find_pio()), tail


# ── can THIS machine build? ────────────────────────────────────────────

MIN_RAM_GB = 4
MIN_DISK_GB = 6

REQUIREMENTS = (
    "Minimum to build firmware: a 64-bit Windows, Linux or macOS (a "
    "Raspberry Pi counts, on a 64-bit OS); 4 GB of memory; 6 GB of free "
    "disk; PlatformIO, installed once (the hub can install it); and the "
    "Tasmota source -- your fork's homevoice branch, unzipped as a folder "
    "called 'Tasmota' in the home folder. The first build downloads the "
    "ESP32 toolchain (1-2 GB) and is slow, most of an hour on a Pi; later "
    "builds are minutes and need no internet.")


def _ram_bytes() -> int:
    """Total memory, or 0 when this system will not say."""
    try:
        if sys.platform.startswith("linux"):
            for line in Path("/proc/meminfo").read_text().splitlines():
                if line.startswith("MemTotal:"):
                    return int(line.split()[1]) * 1024
        elif sys.platform == "win32":
            import ctypes

            class _Status(ctypes.Structure):
                _fields_ = [("dwLength", ctypes.c_ulong),
                            ("dwMemoryLoad", ctypes.c_ulong),
                            ("ullTotalPhys", ctypes.c_ulonglong),
                            ("ullAvailPhys", ctypes.c_ulonglong),
                            ("ullTotalPageFile", ctypes.c_ulonglong),
                            ("ullAvailPageFile", ctypes.c_ulonglong),
                            ("ullTotalVirtual", ctypes.c_ulonglong),
                            ("ullAvailVirtual", ctypes.c_ulonglong),
                            ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]
            status = _Status()
            status.dwLength = ctypes.sizeof(_Status)
            if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
                return int(status.ullTotalPhys)
        elif sys.platform == "darwin":
            done = subprocess.run(["sysctl", "-n", "hw.memsize"],
                                  capture_output=True, text=True, timeout=5)
            return int(done.stdout.strip())
    except (OSError, ValueError, AttributeError, subprocess.SubprocessError):
        pass
    return 0


def preflight(source: str = "") -> List[Tuple[str, bool, str]]:
    """Every requirement as (what, met, detail). The build may run only
    when all are met; otherwise the caller says REQUIREMENTS and which
    ones this machine misses."""
    checks: List[Tuple[str, bool, str]] = []
    machine = platform.machine().lower()
    wide = machine in ("x86_64", "amd64", "aarch64", "arm64") \
        and sys.maxsize > 2 ** 32
    checks.append(("64-bit system", wide,
                   f"{platform.system()} {machine}, "
                   f"{'64' if sys.maxsize > 2 ** 32 else '32'}-bit Python"))
    ram = _ram_bytes()
    checks.append(("Memory", ram == 0 or ram >= MIN_RAM_GB * 0.9 * 2 ** 30,
                   (f"{ram / 2 ** 30:.1f} GB" if ram else "could not be read")
                   + f" (needs {MIN_RAM_GB} GB)"))
    try:
        free = shutil.disk_usage(Path.home()).free
    except OSError:
        free = 0
    checks.append(("Free disk", free >= MIN_DISK_GB * 2 ** 30,
                   f"{free / 2 ** 30:.1f} GB free (needs {MIN_DISK_GB} GB)"))
    pio = find_pio()
    checks.append(("PlatformIO", bool(pio), pio or "not installed"))
    try:
        tree = find_source(source)
        version = source_version(tree)
        checks.append(("Tasmota source", True,
                       f"{tree}" + (f" (Tasmota {version})" if version else "")))
    except BuildError:
        checks.append(("Tasmota source", False,
                       "no 'Tasmota' folder in the home, Desktop or "
                       "Downloads folder"))
    return checks


def looks_like_source(path: Path) -> bool:
    return (path / "tasmota" / "tasmota.ino").is_file()


def find_source(explicit: str) -> Path:
    """The Tasmota tree: the --source argument, or the usual places."""
    if explicit:
        path = Path(explicit).expanduser()
        if looks_like_source(path):
            return path
        fail(f"{path} does not look like a Tasmota source tree "
             f"(no tasmota/tasmota.ino inside it).")
    candidates = [
        PROJECT / "Tasmota",
        Path.home() / "Tasmota",
        Path.home() / "Desktop" / "Tasmota",
        Path.home() / "Downloads" / "Tasmota",
    ]
    for candidate in candidates:
        if looks_like_source(candidate):
            return candidate
    fail("No Tasmota source tree found. Download the homevoice branch of "
         "the fork ONCE (Code -> Download ZIP, or git clone), unzip it as "
         "'Tasmota' next to voice_ai or in your home folder, or pass "
         "--source <path>.")


def source_version(source: Path) -> str:
    """The tree's own version, read from tasmota_version.h. "" = unknown."""
    for candidate in (source / "tasmota" / "include" / "tasmota_version.h",
                      source / "tasmota" / "tasmota_version.h"):
        try:
            text = candidate.read_text("utf-8", errors="replace")
        except OSError:
            continue
        found = re.search(r"0x([0-9A-Fa-f]{8})", text)
        if found:
            raw = int(found.group(1), 16)
            return f"{(raw >> 24) & 0xFF}.{(raw >> 16) & 0xFF}." \
                   f"{(raw >> 8) & 0xFF}"
    return ""


# ── assembling the tree ────────────────────────────────────────────────

def assemble(source: Path, override: Path) -> None:
    """Put the override where the compiler looks. A copy -- esp_firmware/
    stays the truth, the source tree is a workbench."""
    target = source / "tasmota" / "user_config_override.h"
    if not override.is_file():
        fail(f"no {override} -- the override IS the build definition; "
             f"without it this would compile stock Tasmota.")
    target.write_text(override.read_text("utf-8"), encoding="utf-8")
    say(f"  override -> {target}")

    # An older version of this script wrote platformio_override.ini (an
    # Arduino IDE library link). A file this script wrote is taken back;
    # anyone else's is left alone.
    pio_override = source / "platformio_override.ini"
    try:
        lines = pio_override.read_text("utf-8", errors="replace").splitlines()
    except OSError:
        lines = []
    if lines and lines[0].startswith("; written by build_firmware.py"):
        added = [ln for ln in lines if ln.strip() and not ln.startswith(";")
                 and ln.strip() != "[env:tasmota32]"
                 and not ln.startswith("lib_extra_dirs")]
        if added:
            say(f"  note: {pio_override.name} has lines of yours beside the "
                f"Arduino link this script once wrote -- not touched. Remove "
                f"its [env:tasmota32] lib_extra_dirs line: it hides "
                f"Tasmota's own libraries from that build.")
        else:
            pio_override.unlink()
            say(f"  removed {pio_override.name} (written by an older version "
                f"of this script)")


# ── proving the result ─────────────────────────────────────────────────

#: Defines Tasmota itself takes back. Its template header
#: (tasmota/include/tasmota_template.h, v15.5.0 lines 739-741) reads:
#:     #ifdef USE_DISPLAY_MAX7219_MATRIX
#:       #undef USE_DISPLAY_MAX7219
#:       #undef USE_DISPLAY_TM1637
#: -- the dot-matrix driver (xdsp_19) and the seven-segment driver
#: (xdsp_15, which serves TM1637, TM1638 and the seven-segment MAX7219)
#: share the MAX7219 pin names, and Tasmota lets only one of them into an
#: image. An override that asks for both gets the matrix, silently. Found
#: on 2026-09-10: Kitchen's image had every driver the override named
#: except TM1637, and the override did name it. A requirement that
#: Tasmota is known to drop is therefore not a failure of the build; it
#: is said, once, at proof time, and left out of the proof.
DROPPED_BY_TASMOTA = {
    "USE_DISPLAY_MAX7219_MATRIX": (
        ("TM1637 seven-segment (Xdsp15)",),
        "Tasmota's template header undefines USE_DISPLAY_TM1637 (and "
        "USE_DISPLAY_MAX7219) whenever USE_DISPLAY_MAX7219_MATRIX is set: "
        "the dot-matrix and the seven-segment drivers share the MAX7219 "
        "pin names and only one can be in an image. This build carries the "
        "MATRIX. To have TM1637 instead, remove USE_DISPLAY_MAX7219_MATRIX "
        "from the override and build again."),
}


def defines_in(override_text: str) -> set:
    """The USE_... names an override defines (commented lines ignored)."""
    out = set()
    for line in override_text.splitlines():
        line = line.split("//", 1)[0]
        m = re.match(r"\s*#\s*define\s+(USE_[A-Z0-9_]+)", line)
        if m:
            out.add(m.group(1))
    return out


def expected_markers(override_text: str = "") -> tuple:
    """(markers to prove, notes to say): REQUIRED_MARKERS minus whatever
    Tasmota is known to drop for THIS override, each drop explained."""
    wanted = dict(REQUIRED_MARKERS)
    notes = []
    present = defines_in(override_text)
    for define, (dropped, why) in DROPPED_BY_TASMOTA.items():
        if define in present:
            for label in dropped:
                if label in wanted:
                    del wanted[label]
            notes.append(why)
    return wanted, notes


def prove(map_path: Path, override_text: "str | None" = None) -> list:
    """Which requested drivers are MISSING from the linker map.
    [] is the only pass. The map lists every symbol and object file that
    made it into the binary; a driver that left no trace was not built
    in, whatever the override said. Drivers Tasmota itself removes for
    this override (DROPPED_BY_TASMOTA) are not required.

    `override_text` None means "the override this build used": the file
    beside this script."""
    if override_text is None:
        try:
            override_text = (HERE / "user_config_override.h").read_text(
                "utf-8", errors="replace")
        except OSError:
            override_text = ""
    try:
        text = map_path.read_text("utf-8", errors="replace").lower()
    except OSError:
        return ["the linker map itself (no file at "
                f"{map_path} -- did the build finish?)"]
    # ONLY WHAT WAS KEPT. The map opens with "Discarded input sections":
    # code that was compiled and then thrown away as unused. A driver
    # listed there is NOT in the binary, so its name there proves nothing.
    kept = text.find("linker script and memory map")
    if kept > 0:
        text = text[kept:]
    missing = []
    wanted, _notes = expected_markers(override_text)
    for label, pattern in wanted.items():
        if not re.search(pattern, text):
            missing.append(label)
    return missing


# ── where the build leaves its images ──────────────────────────────────
#
# Tasmota's own post-build scripts (pio-tools/name-firmware.py, paths from
# tasmotapiolib) write the FINAL images under the env's name:
#     build_output/firmware/<env>.bin            the app image (OTA)
#     build_output/firmware/<env>.factory.bin    the factory image (cable)
#     build_output/map/<env>.map                 the linker map
# and the map is MOVED there, out of .pio/build/<env>/firmware.map. The
# first version of this script looked for .pio/build/tasmota32/
# tasmota32.factory.bin and .pio/build/tasmota32/firmware.map, where a
# stock tree leaves neither. build_output/ is looked in first; PlatformIO's
# own names in .pio/build/<env>/ are the fallback (a tree whose extra
# scripts differ, or an older Tasmota), and which one was used is said.

def artifact_candidates(source: Path, env: str) -> Dict[str, List[Path]]:
    """Where each output of `pio run -e <env>` may be, best first."""
    out = source / "build_output"
    build = source / ".pio" / "build" / env
    return {
        "factory": [out / "firmware" / f"{env}.factory.bin",
                    build / "firmware.factory.bin", build / f"{env}.factory.bin"],
        "app": [out / "firmware" / f"{env}.bin",
                build / "firmware.bin", build / f"{env}.bin"],
        "map": [out / "map" / f"{env}.map",
                build / "firmware.map", build / f"{env}.map"],
    }


def find_artifacts(source: Path, env: str,
                   since: float = 0.0) -> Dict[str, Optional[Path]]:
    """{"factory", "app", "map"} -> the file found, or None. `since` (a
    time.time() taken before the build) skips files older than the build:
    a build whose post-script failed must not hand over the LAST build's
    image from build_output/ as if it were this one."""
    found: Dict[str, Optional[Path]] = {}
    for kind, paths in artifact_candidates(source, env).items():
        found[kind] = None
        for path in paths:
            try:
                if path.is_file() and (not since
                                       or path.stat().st_mtime >= since - 2):
                    found[kind] = path
                    break
            except OSError:
                continue
    return found


def file_stamp(path: Path) -> str:
    digest = hashlib.sha256(path.read_bytes()).hexdigest()[:16]
    return f"{path.stat().st_size:,} bytes, sha {digest}"


def install_artifacts(found, dest: Path,
                      board: Optional["chips.Board"] = None) -> list:
    """Both images of one family into `dest` (esp_firmware/firmware/),
    side by side: the factory image as <board.firmware>
    (tasmota32c3.factory.bin), the app image as <board.ota_name>. `found` is find_artifacts()'s answer, or a folder
    holding both under those final names. Raises BuildError when either is
    missing -- BOTH are checked before EITHER is copied."""
    board = board or chips.DEFAULT
    if isinstance(found, (str, Path)):
        folder = Path(found)
        found = {"factory": folder / board.firmware,
                 "app": folder / board.ota_name}
    pairs = (("factory", board.firmware, dest / board.firmware),
             ("app", board.ota_name, dest / board.ota_name))
    # BOTH checked before EITHER is copied: failing on the second after
    # the first was installed left a new factory image beside a stale OTA
    # image -- cable-flashed and OTA-updated boards on different builds.
    for kind, name, _target in pairs:
        built = found.get(kind)
        if built is None or not Path(built).is_file():
            fail(f"the {board.pio_env} build finished but {name} is not "
                 + (f"at {built}." if built is not None else
                    f"in build_output/firmware/ or .pio/build/{board.pio_env}/."))
    installed = []
    for kind, _name, target in pairs:
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(found[kind], target)
        installed.append(target)
    return installed


def finish_family(source: Path, board: "chips.Board",
                  override_text: str, dest: Path,
                  since: float = 0.0) -> List[Path]:
    """After `pio run -e <board.pio_env>` succeeded: find its images and
    map, PROVE the map, install the images. Says where each file was
    found. Raises BuildError naming the family; nothing of THAT family is
    installed then (the others are not affected)."""
    env = board.pio_env
    found = find_artifacts(source, env, since)
    for kind in ("map", "factory", "app"):
        path = found[kind]
        say(f"  {kind:8} " + (str(path.relative_to(source))
                              if path is not None else "not found"))
    if found["map"] is None:
        fail(f"{board.family_label}: no linker map from {env} (looked in "
             f"build_output/map/{env}.map and .pio/build/{env}/firmware.map) "
             f"-- did the build finish? Nothing of this family was installed.")
    missing = prove(found["map"], override_text)
    if missing:
        for label in missing:
            say(f"  ! NOT in the {env} binary: {label}")
        fail(f"{board.family_label}: " + ", ".join(missing) + " left no trace "
             "in the linker map -- a green compile is not a proof. Nothing of "
             "this family was installed."
             + ("" if board.family == chips.DEFAULT.family else
                f" Tasmota's own settings for the {board.family_label} may "
                f"switch these drivers off (a small chip's build leaves out "
                f"what does not fit); the other families are not affected."))
    say(f"  Every requested driver is in the {env} binary.")
    return install_artifacts(found, dest, board)


# ── the run ────────────────────────────────────────────────────────────

def main() -> int:
    try:
        return _main() or 0
    except BuildError as exc:
        say(f"\n  STOPPED: {exc}")
        return 1


def _main() -> int:
    ap = argparse.ArgumentParser(
        description="Build HomeVoice's Tasmota locally and prove what is "
                    "inside it.")
    ap.add_argument("--source", default="",
                    help="Tasmota source tree (default: looks in the "
                         "usual places)")
    ap.add_argument("--family", action="append", default=[],
                    metavar="FAMILY",
                    help="build for this ESP32 family (repeatable): "
                         + ", ".join(b.family for b in ALL_TARGETS)
                         + " (default: the classic esp32 only)")
    ap.add_argument("--all", action="store_true",
                    help="build for every ESP32 family, one after another")
    ap.add_argument("--assemble-only", action="store_true",
                    help="place files and stop -- no compile (for "
                         "checking what WOULD build)")
    args = ap.parse_args()
    boards = ALL_TARGETS if args.all else parse_families(args.family)
    say(f"\n  Building for: {family_names(boards)}")

    say("\n  Checking this machine...")
    checks = preflight(args.source)
    for what, ok, detail in checks:
        say(f"  {'[ok]' if ok else '[  ]'} {what}: {detail}")
    missing = [what for what, ok, _ in checks if not ok
               and not (args.assemble_only and what == "PlatformIO")]
    if missing == ["PlatformIO"]:
        say("\n  Installing PlatformIO (one time)...")
        ok, tail = install_platformio()
        if tail:
            say(f"  {tail}")
        if ok:
            missing = []
    if missing:
        fail(f"this machine is missing: {', '.join(missing)}.\n  "
             + REQUIREMENTS)
    pio = find_pio()

    source = find_source(args.source)
    say(f"\n  Source: {source}")
    version = source_version(source)
    if version and version != EXPECTED_VERSION:
        say(f"  ! This tree is Tasmota {version}; this project's option "
            f"numbers (SetOption103/132/19) were verified on "
            f"{EXPECTED_VERSION}. Building anyway -- but re-verify those "
            f"options before trusting a board with it.")
    elif version:
        say(f"  Version: {version} -- matches the project.")

    override = HERE / "user_config_override.h"
    if not override.is_file():
        fail(f"no {override} -- the override IS the build definition; "
             f"without it this would compile stock Tasmota, the exact "
             f"thing that cost an afternoon on 2026-08-18.")

    say("\n  Assembling...")
    assemble(source, override)
    if args.assemble_only:
        say("\n  Assembled only, as asked. Nothing was compiled.")
        return 0

    override_text = override.read_text("utf-8", errors="replace")
    for note in expected_markers(override_text)[1]:
        say(f"\n  note: {note}")
    results: List[Tuple["chips.Board", bool, str]] = []
    for number, board in enumerate(boards, 1):
        say(f"\n  [{number}/{len(boards)}] Building {board.pio_env} "
            f"({board.family_label}) -- the first ever build of a family "
            f"downloads its toolchain and takes a while; after that, "
            f"minutes...\n")
        started = time.time()
        code = subprocess.call([pio, "run", "-e", board.pio_env],
                               cwd=str(source))
        if code != 0:
            say(f"\n  ! {board.pio_env}: the build failed -- PlatformIO's "
                f"output above says where.")
            results.append((board, False, "the build failed; nothing of it "
                                          "was installed"))
            continue
        say(f"\n  Proving what is inside the {board.pio_env} binary...")
        try:
            installed = finish_family(source, board, override_text,
                                      HERE / "firmware", since=started)
        except BuildError as exc:
            say(f"  ! {exc}")
            results.append((board, False, str(exc)))
            continue
        for target in installed:
            say(f"  {target}   ({file_stamp(target)})")
        results.append((board, True, ", ".join(
            str(t.relative_to(HERE)) for t in installed)))

    say("\n  Result, per family:")
    for board, ok, detail in results:
        say(f"  {'[ok]' if ok else '[  ]'} " + (
            detail if detail.startswith(board.family_label + ":")
            else f"{board.family_label}: {detail}"))
    if not any(ok for _, ok, _ in results):
        fail("no family built and proved -- nothing was installed; the "
             "firmware in esp_firmware/firmware/ is unchanged.")
    say("\n  Done. The hub flashes the new factory images on the next "
        "install (its known-lacking notes are keyed to the file's hash, so "
        "they cleared themselves just now). Running boards take the OTA "
        "image from their own web page -- Firmware Upgrade -> ota/"
        + " or ota/".join(b.ota_name for b, ok, _ in results if ok) + ".")
    return 0 if all(ok for _, ok, _ in results) else 1


if __name__ == "__main__":
    sys.exit(main())

"""Firmware builds: the linker-map proof, assembly, every ESP32 family
(images, the fork's CI proof) and firmware sent from the phone.

Run: python3 tests/test_firmware_build.py   (from anywhere; no toolchain needed)
"""
import base64
import io
import json
import os
import re
import shutil
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
import sys                                               # noqa: E402
sys.path.insert(0, str(ROOT))
os.environ.setdefault("HOMEVOICE_HOME", tempfile.mkdtemp())

from esp_firmware import build_firmware as bf            # noqa: E402

PASSED = FAILED = 0
OVERRIDE = ROOT / "esp_firmware" / "user_config_override.h"


def check(name, ok, detail=""):
    global PASSED, FAILED
    if ok:
        PASSED += 1
        print(f"  [PASS] {name}")
    else:
        FAILED += 1
        print(f"  [FAIL] {name}   {detail}")


def proof():
    print("\nthe linker-map proof")
    override = OVERRIDE.read_text()
    builtins = "\n".join(f" .text._Z6{n}j 0x400d0000 0x10 x.o"
                         for n in ("Xsns80", "Xdrv13", "Xdsp01", "Xdsp19",
                                   "Xsns32")) + "\n .text._Z10CmndIrHvacv 0x400d0000 0x10 x.o"
    kept = ("Discarded input sections\n .text._Z3foov\n"
            "Memory Configuration\n\nLinker script and memory map\n"
            + builtins + "\n")
    discarded = ("Discarded input sections\n .text._Z6Xsns80j 0x0 0x40 x.o\n"
                 "Memory Configuration\n\nLinker script and memory map\n"
                 + builtins.replace("Xsns80", "Xsns99") + "\n")
    tmp = Path(tempfile.mkdtemp())
    (tmp / "ok.map").write_text(kept)
    (tmp / "gone.map").write_text(discarded)
    check("every built-in kept: passes (TM1637 excused by the MATRIX)",
          bf.prove(tmp / "ok.map", override) == [],
          bf.prove(tmp / "ok.map", override))
    missing = bf.prove(tmp / "gone.map", override)
    check("a driver the linker threw away fails, named",
          missing == ["RC522 card reader (Xsns80)"], missing)
    check("no map: said, not a crash",
          "linker map" in bf.prove(tmp / "none.map", override)[0])


def override_file():
    print("\nthe override")
    text = OVERRIDE.read_text()
    defines = bf.defines_in(text)
    for need in ("USE_SPI", "USE_RC522", "USE_DISPLAY", "USE_DISPLAY_LCD",
                 "USE_DISPLAY_MAX7219_MATRIX", "USE_MPU6050",
                 "USE_IR_REMOTE_FULL"):
        check(f"defines {need}", need in defines)
    wanted, notes = bf.expected_markers(text)
    check("proof covers six drivers, TM1637 explained",
          len(wanted) == 6 and len(notes) == 1, (list(wanted), notes))


def assemble():
    print("\nassembling a source tree")
    source = Path(tempfile.mkdtemp())
    (source / "tasmota").mkdir()
    (source / "platformio_override.ini").write_text(
        "; written by build_firmware.py -- points the build at the\n"
        "; libraries Arduino IDE already downloaded on this machine.\n"
        "[env:tasmota32]\nlib_extra_dirs = /x/Arduino/libraries\n")
    import contextlib
    with contextlib.redirect_stdout(io.StringIO()):
        bf.assemble(source, OVERRIDE)
    out = (source / "tasmota" / "user_config_override.h").read_text()
    check("the override is copied in, unchanged", out == OVERRIDE.read_text())
    check("the old Arduino lib_extra_dirs stanza this script wrote is taken back",
          not (source / "platformio_override.ini").exists())
    try:
        bf.assemble(source, source / "missing.h")
        refused = ""
    except bf.BuildError as exc:
        refused = str(exc)
    check("no override: refused, never stock Tasmota", "override" in refused, refused)


def machine_check():
    print("\ncan this machine build?")
    checks = bf.preflight()
    check("five requirements, each with a detail",
          [c[0] for c in checks] == ["64-bit system", "Memory", "Free disk",
                                     "PlatformIO", "Tasmota source"]
          and all(isinstance(c[2], str) and c[2] for c in checks), checks)
    check("the requirement sentence names every minimum",
          all(w in bf.REQUIREMENTS for w in ("64-bit", "4 GB", "6 GB",
                                             "PlatformIO", "Tasmota")))


# ── every ESP32 family ─────────────────────────────────────────────────

BUILTIN_MAP = "\n".join(f" .text._Z6{n}j 0x400d0000 0x10 x.o"
                        for n in ("Xsns80", "Xdrv13", "Xdsp01", "Xdsp19", "Xsns32")) + "\n .text._Z10CmndIrHvacv 0x400d0000 0x10 x.o"
GOOD_MAP = ("Discarded input sections\n\nLinker script and memory map\n"
            + BUILTIN_MAP + "\n")
#: What a small build that leaves the displays out looks like.
NO_DISPLAY_MAP = ("Linker script and memory map\n .text._Z6Xsns80j\n"
                  " .text._Z6Xsns32j\n .text._Z10CmndIrHvacv\n")


def fake_build(source, env, map_text=GOOD_MAP, layout="tasmota"):
    """What `pio run -e <env>` leaves: Tasmota's build_output/ (the map
    MOVED out of .pio), or only PlatformIO's own names in .pio/build/."""
    build = source / ".pio" / "build" / env
    build.mkdir(parents=True, exist_ok=True)
    (build / "firmware.bin").write_bytes(b"A" * 8)
    (build / "firmware.factory.bin").write_bytes(b"F" * 16)
    if layout == "tasmota":
        (source / "build_output" / "firmware").mkdir(parents=True, exist_ok=True)
        (source / "build_output" / "map").mkdir(parents=True, exist_ok=True)
        (source / "build_output" / "firmware" / f"{env}.bin").write_bytes(b"a" * 10)
        (source / "build_output" / "firmware" / f"{env}.factory.bin").write_bytes(b"f" * 20)
        (source / "build_output" / "map" / f"{env}.map").write_text(map_text)
    else:
        (build / "firmware.map").write_text(map_text)


def artifacts():
    print("\nwhere a build leaves its images, per family")
    chips = bf.chips
    source = Path(tempfile.mkdtemp())
    fake_build(source, "tasmota32c3")
    found = bf.find_artifacts(source, "tasmota32c3")
    check("build_output/ is looked in first (Tasmota's final names)",
          found == {"factory": source / "build_output/firmware/tasmota32c3.factory.bin",
                    "app": source / "build_output/firmware/tasmota32c3.bin",
                    "map": source / "build_output/map/tasmota32c3.map"}, found)
    plain = Path(tempfile.mkdtemp())
    fake_build(plain, "tasmota32", layout="pio")
    found = bf.find_artifacts(plain, "tasmota32")
    check("...and .pio/build/<env>/firmware.* is the fallback",
          found == {"factory": plain / ".pio/build/tasmota32/firmware.factory.bin",
                    "app": plain / ".pio/build/tasmota32/firmware.bin",
                    "map": plain / ".pio/build/tasmota32/firmware.map"}, found)
    old = source / "build_output" / "firmware" / "tasmota32c3.bin"
    os.utime(old, (1_000_000, 1_000_000))
    found = bf.find_artifacts(source, "tasmota32c3", since=2_000_000)
    check("an image older than this build is not taken from build_output/",
          found["app"] == source / ".pio/build/tasmota32c3/firmware.bin", found)
    check("nothing built, nothing found",
          bf.find_artifacts(Path(tempfile.mkdtemp()), "tasmota32s3")
          == {"factory": None, "app": None, "map": None})

    dest = Path(tempfile.mkdtemp())
    c3 = chips.FAMILIES["esp32c3"]
    got = bf.install_artifacts(bf.find_artifacts(source, "tasmota32c3"), dest, c3)
    check("a C3 build installs tasmota32c3.factory.bin and tasmota32c3.bin side by side",
          got == [dest / "tasmota32c3.factory.bin", dest / "tasmota32c3.bin"]
          and (dest / "tasmota32c3.factory.bin").read_bytes() == b"f" * 20, got)
    got = bf.install_artifacts(bf.find_artifacts(plain, "tasmota32"), dest)
    check("the classic build keeps its names (from the fallback folder)",
          got == [dest / "tasmota32.factory.bin", dest / "tasmota32.bin"]
          and (dest / "tasmota32.factory.bin").read_bytes() == b"F" * 16, got)
    empty = Path(tempfile.mkdtemp())
    try:
        bf.install_artifacts({"factory": source / "build_output/firmware/"
                              "tasmota32c3.factory.bin", "app": None}, empty, c3)
        refused = ""
    except bf.BuildError as exc:
        refused = str(exc)
    check("one image missing: refused, NEITHER installed",
          "tasmota32c3.bin" in refused and not any(empty.rglob("*")), refused)

    # One family proves and installs; another (the displays left out)
    # installs nothing and names what it lacks.
    override = OVERRIDE.read_text()
    fake_build(source, "tasmota32c2", NO_DISPLAY_MAP)
    dest = Path(tempfile.mkdtemp())
    buffer = io.StringIO()
    import contextlib
    with contextlib.redirect_stdout(buffer):
        ok = bf.finish_family(source, c3, override, dest)
        try:
            bf.finish_family(source, chips.FAMILIES["esp32c2"],
                             override, dest)
            c2 = ""
        except bf.BuildError as exc:
            c2 = str(exc)
    check("finish_family: C3 proven and installed, says where it looked",
          len(ok) == 2 and "build_output/map/tasmota32c3.map" in buffer.getvalue(),
          buffer.getvalue())
    check("...C2 without the displays: refused, naming them, nothing installed",
          c2.startswith("ESP32-C2") and "Display core (Xdrv13)" in c2
          and "MAX7219 dot matrix (Xdsp19)" in c2
          and not (dest / "tasmota32c2.factory.bin").exists()
          and not (dest / "tasmota32c2.bin").exists(), c2)


def families():
    print("\nfamilies: names, records, image chips")
    chips = bf.chips
    check("default is the classic board only", bf.parse_families([]) == bf.CLASSIC
          and bf.CLASSIC[0] is chips.DEFAULT)
    check("family, env and short names; catalogue order; once each",
          [b.family for b in bf.parse_families(["tasmota32c3", "s3", "esp32c3", "classic"])]
          == ["esp32", "esp32s3", "esp32c3"])
    try:
        bf.parse_families(["esp8266"])
        why = ""
    except bf.BuildError as exc:
        why = str(exc)
    check("an unknown family is refused", "esp8266" in why, why)
    check("every family is a target, classic first",
          [b.pio_env for b in bf.ALL_TARGETS] == ["tasmota32", "tasmota32s3", "tasmota32s2",
                                                  "tasmota32c3", "tasmota32c6", "tasmota32c2"])
    check("records: no 'board' is classic; a C3 record is the C3",
          [b.family for b in bf.families_of([{"slug": "a"}, {"board": "esp32c3"}])]
          == ["esp32", "esp32c3"])
    check("no boards at all: the classic one", bf.families_of([]) == bf.CLASSIC)
    names = bf.firmware_names()
    check("every family's two image names are known",
          names["tasmota32c6.factory.bin"][1] == "factory"
          and names["tasmota32s3.bin"] == (chips.FAMILIES["esp32s3"], "app")
          and len(names) == 12, sorted(names))
    app = (ROOT / "esp_firmware" / "firmware" / "tasmota32.bin").read_bytes()[:64]
    check("the shipped image is a classic-chip image",
          bf.image_chip_problem(app, chips.DEFAULT) == "")
    why = bf.image_chip_problem(app, chips.FAMILIES["esp32c3"])
    check("...and is refused as a C3 image, saying what it is",
          "ESP32 (classic)" in why and "ESP32-C3" in why, why)
    c3 = app[:12] + (5).to_bytes(2, "little") + app[14:]
    check("a C3 image (chip id 5) is a C3 image",
          bf.image_chip_problem(c3, chips.FAMILIES["esp32c3"]) == "")


def one_firmware_folder():
    print("\none folder for every image (and an older install moves in)")
    from core import config
    from esp_firmware import chips
    base = Path(tempfile.mkdtemp())
    old_root, old_ota, new = base, base / "ota", base / "firmware"
    old_ota.mkdir()
    (old_root / "tasmota32.factory.bin").write_bytes(b"F")
    (old_ota / "tasmota32.bin").write_bytes(b"A")
    (old_ota / "tasmota32c3.bin").write_bytes(b"C")
    (old_root / "notes.txt").write_text("not firmware")
    new.mkdir()
    (new / "tasmota32c3.bin").write_bytes(b"NEWER")
    saved = (config._OLD_FIRMWARE_DIRS, config.FIRMWARE_DIR)
    config._OLD_FIRMWARE_DIRS, config.FIRMWARE_DIR = (old_root, old_ota), str(new)
    try:
        moved = config.settle_firmware_folder()
        check("the old factory and app images move into the one folder",
              sorted(p.name for p in moved) == ["tasmota32.bin", "tasmota32.factory.bin"]
              and (new / "tasmota32.factory.bin").read_bytes() == b"F"
              and (new / "tasmota32.bin").read_bytes() == b"A", moved)
        check("...never over a file already there, and nothing but firmware moves",
              (new / "tasmota32c3.bin").read_bytes() == b"NEWER"
              and (old_root / "notes.txt").is_file())
        check("ota/ stays while a file is left in it (never deleted, never "
              "moved over a newer one)", (old_ota / "tasmota32c3.bin").is_file())
        (old_ota / "tasmota32c3.bin").unlink()
        check("a settled install moves nothing", config.settle_firmware_folder() == [])
        check("...and an emptied ota/ folder is removed", not old_ota.exists())
    finally:
        config._OLD_FIRMWARE_DIRS, config.FIRMWARE_DIR = saved
    check("every family's factory and app image share one folder",
          all(os.path.dirname(config.firmware_path_for(b))
              == os.path.dirname(config.ota_path_for(b))
              == config.FIRMWARE_DIR for b in chips.FAMILIES.values()))


def ci_proof_is_the_hub_proof():
    print("\nthe fork's CI proof is the hub's proof")
    import importlib.util
    spec = importlib.util.spec_from_file_location("ci_prove", ROOT / "ci" / "prove.py")
    ci = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(ci)
    check("same markers", ci.REQUIRED_MARKERS == bf.REQUIRED_MARKERS)
    check("same drops", ci.DROPPED_BY_TASMOTA == bf.DROPPED_BY_TASMOTA)
    override = OVERRIDE.read_text()
    check("same expected set", ci.expected_markers(override) == bf.expected_markers(override))
    tmp = Path(tempfile.mkdtemp())
    maps = {"good": GOOD_MAP, "small": NO_DISPLAY_MAP,
            "thrown": "Discarded input sections\n .text._Z6Xsns80j\n"
                      "Linker script and memory map\n"}
    same = True
    for name, text in maps.items():
        (tmp / f"{name}.map").write_text(text)
        same &= (ci.prove(tmp / f"{name}.map", override)
                 == bf.prove(tmp / f"{name}.map", override))
    check("same verdicts on kept, small and thrown-away maps", same)
    # Since ESP-IDF 5, uint32_t is unsigned long: `bool Xsns80(uint32_t)` is
    # _Z6Xsns80m on every family. The first all-families run failed S2/S3
    # on markers that knew only `j`.
    idf5 = GOOD_MAP.replace("j ", "m ").replace("j\n", "m\n")
    (tmp / "idf5.map").write_text(idf5)
    check("an ESP-IDF 5 map (uint32_t mangled m) proves the same drivers",
          "m 0x" in idf5 or "m\n" in idf5)
    check("...and passes both proofs",
          bf.prove(tmp / "idf5.map", override) == []
          and ci.prove(tmp / "idf5.map", override) == [])
    check("same places for the images",
          ci.artifact_candidates(tmp, "tasmota32s3") == bf.artifact_candidates(tmp, "tasmota32s3"))
    source = Path(tempfile.mkdtemp())
    (source / "tasmota").mkdir()
    shutil.copy(OVERRIDE, source / "tasmota" / "user_config_override.h")
    fake_build(source, "tasmota32s3")
    out = Path(tempfile.mkdtemp())
    buffer = io.StringIO()
    import contextlib
    with contextlib.redirect_stdout(buffer):
        code = ci.main(["tasmota32s3", "--source", str(source), "--out", str(out)])
    check("prove.py publishes exactly <env>.factory.bin and <env>.bin",
          code == 0 and sorted(p.name for p in out.iterdir())
          == ["tasmota32s3.bin", "tasmota32s3.factory.bin"], buffer.getvalue())
    fake_build(source, "tasmota32c2", NO_DISPLAY_MAP)
    out = Path(tempfile.mkdtemp())
    with contextlib.redirect_stdout(io.StringIO()):
        code = ci.main(["tasmota32c2", "--source", str(source), "--out", str(out)])
    check("...and nothing for a family that fails its proof",
          code == 1 and not any(out.iterdir()))
    try:
        import yaml
    except ImportError:
        return
    flow = yaml.safe_load((ROOT / "ci" / "homevoice-build.yml").read_text())
    matrix = flow["jobs"]["build"]["strategy"]
    # The matrix is fromJSON(inputs.envs || '<every family>'): the hub may
    # ask for some (cloud_build.py); a push or a button press builds all.
    expr = matrix["matrix"]["env"]
    default = json.loads(re.search(r"\|\|\s*'(\[.*?\])'", expr).group(1))
    check("the workflow builds every family by default, fail-fast off",
          matrix["fail-fast"] is False and "inputs.envs" in expr
          and default == [b.pio_env for b in bf.ALL_TARGETS[:1]]
          + ["tasmota32s3", "tasmota32s2", "tasmota32c3", "tasmota32c6", "tasmota32c2"]
          and sorted(default) == sorted(b.pio_env for b in bf.ALL_TARGETS), expr)
    inputs = flow[True]["workflow_dispatch"]["inputs"]
    check("...and takes the hub's family list and request id",
          set(inputs) == {"envs", "request"}
          and "inputs.request" in flow["run-name"], inputs)


# ── firmware from the phone (Settings -> Install firmware on hub) ──────

def firmware_from_phone():
    print("\nfirmware from the phone, per family, each image checked")
    import asyncio
    from core.hv_common import CancelledByUser
    from core import config
    from esp_firmware import esp_builder as EB

    class IO:
        can_send_files = True

        def __init__(self, answers, files=None):
            self.answers, self.files = list(answers), files
            self.lines, self.questions = [], []

        async def write(self, text, level="info"):
            self.lines.append(text)

        async def choose(self, question, rows):
            self.questions.append((question, rows))
            answer = self.answers.pop(0)
            return answer(rows) if callable(answer) else answer

        async def ainput(self, question, patient=False, secret=False):
            return self.answers.pop(0)

        async def pick_files(self, text, **kw):
            self.requests = getattr(self, "requests", []) + [kw]
            if self.files is None:
                raise CancelledByUser()
            return self.files

    class Shell:
        failure_note = None
        checkpoint_note = None

    saved_cfg = {k: getattr(config, k) for k in (
        "TASMOTA_FIRMWARE_PATH", "TASMOTA_FAMILY_FIRMWARE_DIR")}
    try:
        # Firmware from the phone: per-family names, each checked.
        fw_dir = Path(tempfile.mkdtemp())
        config.TASMOTA_FIRMWARE_PATH = str(fw_dir / "tasmota32.factory.bin")
        config.TASMOTA_FAMILY_FIRMWARE_DIR = str(fw_dir)
        app = (ROOT / "esp_firmware" / "firmware" / "tasmota32.bin").read_bytes()
        c3_app = app[:12] + (5).to_bytes(2, "little") + app[14:]

        def phone_fw(files, family, answers=(["yes"],)):
            answers = ([family],) + tuple(answers)
            builder = EB.EspBuilder.headless()
            shell = Shell()
            builder._shell = shell
            phone = IO(list(answers), [{"name": n, "data": base64.b64encode(d).decode()}
                                       for n, d in files])
            shell._io = phone
            return asyncio.run(builder.menu_firmware_from_phone()), shell, phone
        ok, shell, phone = phone_fw([("tasmota32c3.bin", c3_app)], "esp32c3")
        check("the phone is asked for one family's files",
              [kw.get("family") for kw in phone.requests] == ["esp32c3"],
              getattr(phone, "requests", None))
        check("tasmota32c3.bin (a C3 image) goes beside its factory image",
              ok and (fw_dir / "tasmota32c3.bin").read_bytes() == c3_app
              and any("ESP32-C3" in ln for ln in phone.lines), shell.failure_note)
        ok, shell, phone = phone_fw([("tasmota32.bin", app)], "esp32")
        check("tasmota32.bin still goes where it always went",
              ok and (fw_dir / "tasmota32.bin").is_file(), shell.failure_note)
        ok, shell, _ = phone_fw([("tasmota32c3.bin", app)], "esp32c3")
        check("a classic image named tasmota32c3.bin is refused",
              not ok and "ESP32 (classic)" in (shell.failure_note or ""), shell.failure_note)
        ok, shell, _ = phone_fw([("firmware.bin", app)], "esp32s3")
        check("an unknown name is refused, listing the names it takes",
              not ok and "tasmota32s3.factory.bin" in (shell.failure_note or ""),
              shell.failure_note)
        ok, shell, _ = phone_fw([("tasmota32c3.bin", c3_app)], "esp32")
        check("another family's file is refused when one family was asked for",
              not ok, shell.failure_note)
        ok, shell, _ = phone_fw([("tasmota32s3.factory.bin", b"\xe9junk" * 100)], "esp32s3")
        check("a junk factory image is refused", not ok
              and "not usable" in (shell.failure_note or ""), shell.failure_note)
        check("nothing else was written", sorted(
            str(p.relative_to(fw_dir)) for p in fw_dir.rglob("*") if p.is_file())
            == ["tasmota32.bin", "tasmota32c3.bin"])
    finally:
        for key, value in saved_cfg.items():
            setattr(config, key, value)


if __name__ == "__main__":
    for section in (proof, override_file, assemble, machine_check, artifacts,
                    families, one_firmware_folder, ci_proof_is_the_hub_proof,
                    firmware_from_phone):
        section()
    print(f"\n  {PASSED} passed, {FAILED} failed")
    sys.exit(1 if FAILED else 0)

#!/usr/bin/env python3
"""
setup_manager.py — asset check & install manager.

Checks every model/data file the system needs against the paths declared in
config.py, reports present vs missing, and either downloads what it can
directly or gives clear manual instructions for what it can't. If a
REQUIRED asset is missing and the user declines to get it, the system exits
gracefully rather than limping into a half-configured run.

THREE ADDITIONS made over time:

1. PACKAGE_ASSETS -- Python package checking. Everything above only ever
   checked FILES. There was no check at all for whether the packages that
   import those files (sherpa_onnx, cryptography, bcrypt, ...) were even
   installed -- a missing package failed wherever the first import happened
   to be, with whatever traceback Python felt like giving, rather than here,
   at the one place that's supposed to know what's required. Checked by
   attempting the actual import (a package can be installed but broken, or
   present for a different Python than sys.executable, so asking pip isn't
   enough), installed via `{sys.executable} -m pip install` on consent.

   CONFIRMED against the files actually reviewed for this project so far
   (main.py, ws_server.py, challenge.py, voice_auth.py, config.py). Several
   modules haven't been reviewed yet (nlu/model_module.py, nlu/device_manager.py,
   nlu/scene_manager.py, nlu/slot_extractor.py, nlu/lexicon.py,
   nlu/command_splitter.py, nlu/resolver.py, nlu/executor.py,
   nlu/llm_describer.py, ha_client.py, voice_security/sv.py,
   voice_security/biometric_store.py, voice_security/fallback_auth.py,
   voice_security/enrollment_handler.py, dependency_flags.py) and almost
   certainly import their own third-party packages (something for MPNet
   embeddings, something for talking to Home Assistant's HTTP API, something
   for loading the Qwen GGUF). Add entries once those files are reviewed --
   guessing package names for code that hasn't been read is how this list
   drifts from reality the same way the old Whisper/Piper entries did.

2. and 3. REMOVED with the spoken challenge (Defense, 2026-11-16): its
   wordlist and the Moonshine model the Pi heard it with are no longer
   downloaded. Archive assets (download a .tar.bz2 and extract it) remain
   as a kind for whatever needs one.

CERTIFICATE POLICY (the fourth addition):
   The access-point certificate is generated ONCE and then never touched by
   setup again. The phone app pins local.crt's exact DER bytes
   (socket_client.dart compares byte-for-byte), so regenerating it locks
   every enrolled phone out until the new cert is copied into the app's
   assets/certs/ and the APK rebuilt. The old flow offered "Regenerate now?"
   on every startup -- one accidental 'y' away from bricking every client.
   Regeneration is now an explicit, rare act via --regen-cert, and it reuses
   the existing private key when one is on disk (same key, new cert -- what
   makes a future SPKI pin survive rotation).

Usage:
    from app.setup_manager import run_setup
    run_setup()                     # call at top of main, hard-stops if needed

    python3 setup_manager.py              # a new hub (Linux or Windows): packages, Mosquitto,
                                          #   Tailscale (+ operator), Docker and Home Assistant
                                          #   (asked y/n), broker, secrets, then ASKS about
                                          #   starting by itself, then pairing. Safe to rerun.
    python setup_manager.py --check       # report only, no prompts/downloads
    python setup_manager.py --regen-cert  # DELIBERATE cert regeneration only
    python setup_manager.py --broker      # write the broker config, make HA's login
    python setup_manager.py --doctor      # check the whole chain, fix nothing
    sudo python setup_manager.py --clock-sudo   # let a HAND-STARTED hub set the clock (Pi, once)
    python setup_manager.py --service     # the hub by itself: systemd at boot (Linux) or at sign-in (Windows)
"""

import datetime
import contextlib
import getpass
import hashlib
import io
import http.client
import importlib.util
import ipaddress
import json
import re
import os
import secrets
import shlex
import shutil
import signal
import socket
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.parse
import urllib.request
import urllib.error
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from core import config
from hub import hub_net
from hub.hub_gpio import _is_pi as _on_raspberry_pi


#: True when NOBODY CAN ANSWER A PROMPT: stdin is not a TTY (systemd, cron,
#: docker) or the operator said so explicitly. The Pi is headless -- setup
#: runs interactively exactly once, over SSH, during provisioning; after
#: that every boot is unattended, and an input() prompt on an unattended
#: boot is a boot that hangs forever.
#:
#: Setup itself no longer asks anything: every missing package and
#: downloadable file is fetched automatically, with a progress bar, and the
#: certificate is generated. This flag now only answers the few remaining
#: yes/no questions (--autostart's "Go ahead?") with "no".
NONINTERACTIVE = (not sys.stdin.isatty()
                  or os.environ.get("HOMEVOICE_NONINTERACTIVE") == "1")


# ===========================================================================
# Python package registry
# ===========================================================================

PACKAGE_ASSETS = [
    {
        "import_name": "numpy",
        "pip_name": "numpy",
        "required": True,
        "note": "Used throughout for audio arrays and embeddings.",
    },
    {
        "import_name": "websockets",
        "pip_name": "websockets",
        "required": True,
        "note": "ws_server.py's entire transport layer.",
    },
    {
        "import_name": "sherpa_onnx",
        "pip_name": "sherpa-onnx",
        "required": True,
        "note": "Speaker verification (TitaNet). Note the "
                "import name (sherpa_onnx, underscore) differs from the pip "
                "package name (sherpa-onnx, hyphen) -- pip show / pip list "
                "won't find it under the import name.",
    },
    {
        # THE ENCODER'S RUNTIME. nlu/model_module.py is ONNX-only for
        # all-mpnet-base-v2 and main.py exits at start without it -- yet it
        # was missing from this list, so a fresh Pi passed "All packages OK"
        # and then died at "Loading model" (2026-09-07). dependency_flags'
        # table called it "(VAD)" for the same historical reason.
        "import_name": "onnxruntime",
        "pip_name": "onnxruntime",
        "required": True,
        "note": "Runs the all-mpnet-base-v2 ONNX export (nlu/model_module.py).",
    },
    {
        # Tokenises for the encoder (AutoTokenizer). No torch is needed or
        # installed; transformers says so on import and that line is expected.
        "import_name": "transformers",
        "pip_name": "transformers",
        "required": True,
        "note": "Tokeniser for the MPNet encoder. Without PyTorch on purpose.",
    },
    {
        "import_name": "cryptography",
        "pip_name": "cryptography",
        "required": True,
        "note": "Encrypts voiceprints at rest (config.require_fernet_key). "
                "Without it there is no encryption at rest for enrolled "
                "voiceprints.",
    },
    {
        "import_name": "bcrypt",
        "pip_name": "bcrypt",
        "required": True,
        "note": "PIN hashing (config.PIN_BCRYPT_ROUNDS). Without it, PIN "
                "fallback cannot be set up at all -- main.py's own message "
                "says as much ('must be digits, correct length, and bcrypt "
                "available').",
    },
    {
        "import_name": "filelock",
        "pip_name": "filelock",
        # Required only where fcntl is missing, which is Windows. On Linux and
        # the Pi, biometric_store uses fcntl.flock and never imports this.
        #
        # NOT optional in effect: without it BiometricStore falls back to
        # writing voiceprints and the periodic-challenge counter with NO lock
        # at all -- it says so loudly, but a warning is not a lock. Marked
        # required so setup installs it rather than leaving the warning to be
        # discovered later.
        "required": True,
        "note": "Cross-process file locking for voiceprint writes on Windows "
                "(Linux uses fcntl, which is stdlib). Without it BiometricStore "
                "writes unlocked.",
    },
    {
        "import_name": "requests",
        "pip_name": "requests",
        "required": False,
        "note": "Home Assistant REST client (home_assistant/ha_client.py). "
                "Without it the hub runs with Home Assistant switched off.",
    },
    {
        "import_name": "webcolors",
        "pip_name": "webcolors",
        # Optional: without it, colour recognition falls back to the built-in
        # list in lexicon.py. With it, every CSS3 name works and nobody has to
        # maintain a colour table.
        "required": False,
        "note": "Full CSS3 colour names for commands like 'make it chartreuse'. "
                "Without it, only the built-in colour list is recognised.",
    },
    {
        "import_name": "jellyfish",
        "pip_name": "jellyfish",
        "required": False,
        "note": "Phonetic (double-metaphone) matching for spoken names "
                "(nlu/app_nav.py, nlu/resolver.py). Optional -- listed here "
                "so going without it is a deliberate choice, not a silent "
                "one.",
    },
    {
        "import_name": "soundfile",
        "pip_name": "soundfile",
        "required": False,
        "note": "Fallback audio decoder for non-WAV / non-standard-WAV "
                "files (main.py, ws_server.py). Plain WAV via stdlib `wave` "
                "always works without this.",
    },
    # ── ESP boards ──────────────────────────────────────────────────
    #
    # THE COMPILER IS GONE. Two entries used to sit here: `esphome`, which
    # compiled a bespoke firmware per board, and `esphome-device-builder`,
    # the web UI the phone app embedded. Between them they wanted Python
    # 3.12+, several gigabytes of toolchain on the SD card, and a network
    # connection at the worst possible moment.
    #
    # ── Grammar for the NLU (nlu/grammar.py) ────────────────────────
    # spaCy first, then its small English model via spaCy's own `download`
    # (so the model matches the installed spaCy).
    {
        "import_name": "spacy",
        "pip_name": "spacy",
        "required": True,
        "note": "The grammar check: spots a question by its shape (\"hey, is "
                "the fan still on\"), so it is read and not switched, and "
                "tells one thing from several (\"the news\" vs \"the lights\").",
    },
    {
        "import_name": "en_core_web_sm",
        "pip_name": "en_core_web_sm",
        "required": True,
        "install_cmd": [sys.executable, "-m", "spacy", "download",
                        "en_core_web_sm"],
        "note": "spaCy's small English model (12 MB), used by the grammar "
                "check. Installed with:  python -m spacy download "
                "en_core_web_sm",
    },
    # The boards now run one prebuilt image. What is left is small: a tool
    # to write that image over USB, a way to talk to the serial console
    # while the cable is in, and an MQTT client so the hub can speak to a
    # board once it is on the network. All three are pure Python and none
    # of them compiles anything.
    {
        "import_name": "esptool",
        "pip_name": "esptool",
        "console_script": "esptool",
        "required": False,
        "note": "Writes the Tasmota image to a blank ESP32 over USB. Needed "
                "ONCE per board -- everything after that happens over the "
                "network. On Linux/Pi add yourself to the 'dialout' group or "
                "/dev/ttyUSB* is not readable, and the flash fails with a "
                "permission error that reads like a broken cable.",
    },
    {
        "import_name": "serial",
        "pip_name": "pyserial",
        "required": False,
        "note": "Talks to a board's console over the cable, which is how it "
                "is handed its Wi-Fi, its broker address and its own "
                "password during setup. Note the names differ: the import is "
                "`serial`, the package is `pyserial` -- pip list will not "
                "find it under the import name.",
    },
    {
        "import_name": "paho",
        "pip_name": "paho-mqtt",
        "required": False,
        "note": "The hub's MQTT client -- how a running board is told to "
                "change its Wi-Fi or reconfigure a pin without a cable. "
                "Without it those actions refuse politely; nothing else is "
                "affected.",
    },
    # ── Pairing on a screen ─────────────────────────────────────────
    {
        "import_name": "qrcode",
        "pip_name": "qrcode",
        "required": False,
        "note": "Draws the pairing code (--pair opens pair.png on screen "
                "and draws it in the terminal; --pair-qr writes the file). "
                "Without it --pair still opens the window and prints the "
                "address and key to type into the app.",
    },
    {
        "import_name": "llama_cpp",
        "pip_name": "llama-cpp-python",
        "required": False,
        # Prebuilt CPU wheels first: from plain PyPI this package compiles
        # from source (needs a C++ compiler, and a long time on a Pi).
        "pip_args": ["--prefer-binary", "--extra-index-url",
                     "https://abetlen.github.io/llama-cpp-python/whl/cpu"],
        "timeout": 3600,
        "note": "Runs the optional LLM describer (the Qwen GGUF). Without "
                "it the hub describes devices without the LLM.",
    },
    # ── The Pi's header: LED + pairing button (hub/hub_gpio.py) ─────
    #
    # Checked and offered ONLY on a Raspberry Pi. On a PC there is no
    # header to drive, so listing them as missing would be noise every
    # run; ws_server prints one line and goes without.
    {
        "import_name": "gpiozero",
        "pip_name": "gpiozero",
        "required": False,
        "pi_only": True,
        "note": "The header LEDs (hub LED: solid = listening; phone LED: "
                "slow blink = pairing open, three flashes = a phone paired, "
                "solid = a phone is connected) "
                "and the hold-to-pair button. Without it ws_server runs "
                "exactly the same with no lights and no button.",
    },
    {
        "import_name": "lgpio",
        "pip_name": "lgpio",
        "required": False,
        "pi_only": True,
        "note": "The pin driver gpiozero uses on Bookworm and on the Pi 5, "
                "where the old RPi.GPIO cannot reach the header. If pip "
                "cannot build it here:  sudo apt install python3-lgpio "
                "python3-gpiozero  (and run the hub with the system Python "
                "or a venv created with --system-site-packages).",
    },
]


# NOT included above, and deliberately so:
#   onnxruntime  -- verified NOT a separate requirement. sherpa-onnx-core
#                   declares zero pip dependencies of its own (it bundles
#                   its own runtime); adding a redundant onnxruntime check
#                   would just be one more thing to keep in sync for no
#                   actual need.
#   faster-whisper -- REMOVED FROM THE PROJECT, not merely left out of this
#                   list. It was a SECOND ASR engine whose only job was to
#                   spare the CLI from typing a transcript during a local
#                   test. The hub has never transcribed commands (the phone
#                   does), so this was an
#                   engine and a model folder installed for a path the Pi
#                   never takes. Nothing imports it now and
#                   config.TEST_ASR_MODEL_PATH is gone; `models/
#                   faster-whisper-small/` can be deleted from disk, and if
#                   faster-whisper is in a requirements file or already
#                   pip-installed, uninstall it -- it pulls ctranslate2,
#                   which is not small.
#   sentencepiece, pypinyin -- were needed ONLY by sherpa-onnx-cli text2token,
#                   for the keyword-spotter master file. That listener and its
#                   generation step are gone; nothing installs these now.


def _check_import(import_name: str) -> bool:
    try:
        __import__(import_name)
        return True
    except Exception:                                  # noqa: BLE001
        # Not only ImportError: a package can be installed and still fail
        # to import (sounddevice raises OSError without PortAudio). That is
        # "not usable" here, and must not crash setup.
        return False


def _asset_present(pkg: dict) -> bool:
    """Is this asset installed?

    Two kinds of asset live in PACKAGE_ASSETS. Most are LIBRARIES this code
    imports, and importing them is the honest test. A few are TOOLS we only
    ever invoke as a command -- esptool is used as `esptool write_flash`
    and nothing here imports it, so testing it by import name would be a
    guess, and a wrong guess means setup offers to install it on every
    single run forever. Where an asset declares `console_script`, look for
    the command on PATH instead.
    """
    script = pkg.get("console_script")
    if script:
        return shutil.which(script) is not None
    return _check_import(pkg["import_name"])


def _run_install_cmd(cmd: List[str], quiet: bool = False,
                     timeout: float = 900) -> bool:
    """An install that isn't a plain `pip install` (e.g. `python -m spacy
    download ...`), with the same externally-managed retry."""
    def _run(extra_args):
        return subprocess.run([*cmd, *extra_args], capture_output=True,
                              text=True, timeout=timeout)
    try:
        result = _run([])
        if result.returncode != 0 and "externally-managed-environment" in (
                result.stderr + result.stdout):
            if not quiet:
                print("    (environment is externally-managed -- retrying with --break-system-packages)")
            result = _run(["--break-system-packages"])
        if result.returncode != 0:
            print("    [ERROR] install failed:")
            print("    " + (result.stderr or result.stdout).strip()[-2000:].replace("\n", "\n    "))
            return False
        return True
    except Exception as e:
        print(f"    [ERROR] Could not run {' '.join(cmd[:4])}: {e}")
        return False


def _pip_install(pip_name: str, quiet: bool = False,
                 pip_args: Optional[List[str]] = None,
                 timeout: float = 900) -> bool:
    if not quiet:
        print(f"    Installing: {pip_name}")

    def _run(extra_args):
        return subprocess.run(
            [sys.executable, "-m", "pip", "install", *(pip_args or []),
             *extra_args, pip_name],
            capture_output=True, text=True, timeout=timeout,
        )

    try:
        result = _run([])
        if result.returncode != 0 and "externally-managed-environment" in result.stderr:
            # PEP 668 -- what a stock Debian/Raspberry Pi OS (Bookworm+)
            # install enforces by default for the system Python. Confirmed
            # by actually hitting this in testing, not assumed: a plain
            # `pip install` is refused outright with this exact message.
            # Retrying inside a venv would be the textbook-correct fix, but
            # this project runs directly against system Python on the Pi
            # (main.py/ws_server.py aren't invoked through a venv activation
            # step anywhere), so the pragmatic choice is
            # --break-system-packages, same as this project's other pip
            # installs -- not a silent workaround, printed plainly so it's
            # visible in the log.
            if not quiet:
                print("    (environment is externally-managed -- retrying with --break-system-packages)")
            result = _run(["--break-system-packages"])

        if result.returncode != 0:
            print("    [ERROR] pip install failed:")
            print("    " + result.stderr.strip()[-2000:].replace("\n", "\n    "))
            return False
        if not quiet:
            print(f"    Installed: {pip_name}")
        return True
    except Exception as e:
        print(f"    [ERROR] Could not run pip: {e}")
        return False


# ===========================================================================
# File asset registry
# ===========================================================================

# Directly downloadable via a single HTTPS request.
#
# "sha256": an optional hex digest checked after download. NONE ARE FILLED
# IN YET -- none were invented; every download is announced as unverified
# until a hash is recorded here from a release page that publishes one.
URL_ASSETS = [
    {
        "name": "BIP-39 wordlist (PIN recovery)",
        "path": config.BIP39_WORDLIST_PATH,
        "required": True,
        "url": "https://raw.githubusercontent.com/bitcoin/bips/master/bip-0039/english.txt",
        "note": "2048-word list for PIN recovery phrases. Without it, recovery falls "
                "back to a tiny insecure built-in list -- fine for testing, not for "
                "real enrollment.",
    },
    {
        "name": "Tasmota firmware (ESP32)",
        "path": config.TASMOTA_FIRMWARE_PATH,
        # Optional: everything except flashing a board works without it, and
        # a hub with no boards yet has no use for a 3MB image.
        "required": False,
        "url": "https://ota.tasmota.com/tasmota32/release/tasmota32.factory.bin",
        "note": "The ONE firmware every board runs (~3MB). This single file "
                "is the whole offline story -- there is no toolchain, no "
                "component registry and no warm-up run, because nothing is "
                "compiled. It is the FACTORY image, which is written at "
                "offset 0x0; the plain tasmota32.bin is a different file that "
                "goes at 0x10000 and will brick a first flash if written to "
                "0x0. If you replace this, change TASMOTA_FLASH_OFFSET in "
                "config.py to match. The 'release' folder tracks the current "
                "stable Tasmota; delete the file and re-run setup to update.",
    },
]

# Firmware for every other ESP32 family, generated from the board profiles
# (esp_firmware/chips.py: each family's Board.firmware), saved where the
# flash looks for it (config.firmware_path_for). Adding a family is a
# profile there, not a stanza here. These are Tasmota's STOCK images: a
# board of that family can be flashed with one today, but stock lacks the
# house drivers (RC522, LCD, MPU6050...) -- the hub refuses a wiring that
# needs one, and a build of this project (the GitHub workflow) replaces the file under the same name.
from esp_firmware import chips as _chips          # noqa: E402

URL_ASSETS += [
    {
        "name": f"Tasmota firmware ({_board.family_label})",
        "path": config.firmware_path_for(_board),
        "required": False,
        "url": f"https://ota.tasmota.com/tasmota32/release/{_board.firmware}",
        "note": f"The {_board.family_label} family's own image, factory "
                f"build, offset 0x0 -- what a board chosen as "
                f"{_board.family_label} is flashed with. Stock Tasmota: "
                f"it lacks the house drivers until a build of this project "
                f"replaces it (same file name).",
    }
    for _board in _chips.FAMILIES.values() if _board.family != "esp32"]


def _export_mpnet() -> bool:
    """The MPNet ONNX export, run for you: installs the exporter (optimum)
    if needed, then the same `optimum-cli export onnx` the manual gives.
    The export is what the benchmark measured, so it is produced, not
    swapped for some other ready-made file."""
    if not _check_import("optimum"):
        print("    Installing the exporter (optimum[onnxruntime]) -- one time...")
        if not _pip_install("optimum[onnxruntime]", quiet=True):
            return False
    out = config.MPNET_MODEL_PATH.rstrip("/\\") + os.sep
    os.makedirs(out, exist_ok=True)
    print("    Exporting sentence-transformers/all-mpnet-base-v2 (a few minutes)...")
    try:
        done = subprocess.run(
            [sys.executable, "-m", "optimum.commands.optimum_cli", "export",
             "onnx", "-m", "sentence-transformers/all-mpnet-base-v2",
             "--task", "feature-extraction",
             "--library-name", "sentence_transformers", out],
            timeout=3600)
    except (OSError, subprocess.SubprocessError) as exc:
        print(f"    [ERROR] export did not run: {exc}")
        return False
    ok = done.returncode == 0 and _exists(os.path.join(out, "config.json"))
    print("    Export finished." if ok else "    [ERROR] export failed (see above).")
    return ok


# Can't be safely auto-fetched with a single confident URL -- needs a specific
# file picked up from its own release/download page, or its own library's
# tooling.
MANUAL_ASSETS = [
    {
        # The encoder shell.init() loads right after run_setup(); without the
        # folder it died at "Loading model" behind an "all assets present".
        # config.json is the marker because the .onnx beside it may be named
        # anything (nlu/model_module._find_model_files takes any *.onnx).
        "name": "Intent encoder (all-mpnet-base-v2, ONNX export)",
        "path": os.path.join(config.MPNET_MODEL_PATH, "config.json"),
        "required": True,
        # Exported automatically: exactly the command below, run for you.
        "fetch": lambda: _export_mpnet(),
        "note": "The hub's sentence encoder, exported once from Hugging Face "
                "(installs the exporter first; takes a few minutes).",
        "how": "Export it (needs internet once, then never again):\n"
               "    pip install \"optimum[onnxruntime]\"\n"
               "    optimum-cli export onnx -m sentence-transformers/all-mpnet-base-v2 "
               "--task feature-extraction --library-name sentence_transformers "
               + shlex.quote(config.MPNET_MODEL_PATH + "/"),
    },
]

# The optional LLM describer (~1 GB). Fetched only when llama-cpp-python is
# installed: without it nothing can load the file.
URL_ASSETS += [
    {
        "name": "LLM describer (Qwen2.5-1.5B Instruct Q4_K_M.gguf, ~1 GB)",
        "path": config.LLM_MODEL_PATH,
        "required": False,
        "needs_import": "llama_cpp",
        "url": "https://huggingface.co/Qwen/Qwen2.5-1.5B-Instruct-GGUF/"
               "resolve/main/qwen2.5-1.5b-instruct-q4_k_m.gguf",
        "note": "Optional LLM that describes devices in plain words.",
        "how": "Download qwen2.5-1.5b-instruct-q4_k_m.gguf from\n"
               "    https://huggingface.co/Qwen/Qwen2.5-1.5B-Instruct-GGUF\n"
               "Place it at:\n    {path}",
    },
]

# Downloaded as a .tar.bz2 and extracted, not saved as a single file.
# "path" here is a MARKER file used to check presence -- checking only that
# the directory exists would pass on a half-extracted archive.
ARCHIVE_ASSETS = [
    {
        "name": "Speaker Verification model (TitaNet-Large)",
        "path": config.SHERPA_SV_MODEL,
        "required": True,
        # A single .onnx file (no extract_dir: saved as is). The link was
        # checked against the release; if it ever moves, the "how" below is
        # printed instead. Listed here so all voice_security model assets
        # are visible in one place.
        "url": "https://github.com/k2-fsa/sherpa-onnx/releases/download/"
               "speaker-recongition-models/nemo_en_titanet_large.onnx",
        "note": "Speaker embedding model for voice recognition.",
        "how": "Download 'nemo_en_titanet_large.onnx' (the exact architecture this "
               "project tested and selected) from the sherpa-onnx speaker "
               "recognition models release:\n"
               "    https://github.com/k2-fsa/sherpa-onnx/releases/tag/speaker-recongition-models\n"
               "Place it at:\n    {path}",
    },
]


# ===========================================================================
# Helpers
# ===========================================================================

def _exists(path: str) -> bool:
    try:
        return os.path.exists(path) and (os.path.isdir(path) or os.path.getsize(path) > 0)
    except OSError:
        return False


def _human_size(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.0f}{unit}"
        n /= 1024
    return f"{n:.0f}TB"


def _bar(done: float, total: float, width: int = 30) -> str:
    """A text progress bar: [#########---------]  45%."""
    frac = 0.0 if total <= 0 else max(0.0, min(1.0, done / total))
    filled = int(round(frac * width))
    return "[" + "#" * filled + "-" * (width - filled) + f"] {frac * 100:3.0f}%"


_ONLINE: Optional[bool] = None


def _online(timeout: float = 5.0) -> bool:
    """Can this machine reach the internet? Asked once per run. Goes through
    urllib so a system proxy is honoured; any HTTP answer at all (even an
    error page) means the network is there."""
    global _ONLINE
    if _ONLINE is not None:
        return _ONLINE
    _ONLINE = False
    for url in ("https://pypi.org/simple/", "https://github.com/"):
        try:
            req = urllib.request.Request(url, method="HEAD",
                                         headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=timeout):
                pass
            _ONLINE = True
            break
        except urllib.error.HTTPError:
            _ONLINE = True
            break
        except (OSError, ValueError, http.client.HTTPException):
            continue
    return _ONLINE


def _offline_help() -> None:
    print("\n  !! NO INTERNET. Setup needs it once, to install packages and")
    print("     download models. Switch this machine to a network with internet")
    print("     (home Wi-Fi, phone hotspot with mobile data on), then run setup")
    print("     again. If the hub is on Tailscale, paired phones still reach it")
    print("     over the internet while you do this.")


def _unlink(path: str) -> None:
    """Remove a file if it is there; a leftover is never worth an error."""
    try:
        os.remove(path)
    except OSError:
        pass


def _run(command: List[str], timeout: float = 60) -> Tuple[bool, str]:
    """Run a command: (it succeeded, what it said). The text is its output
    on success and its complaint on failure; a command that cannot even be
    started is a failure with the reason, never an exception."""
    try:
        done = subprocess.run(command, capture_output=True, text=True,
                              timeout=timeout)
    except (OSError, subprocess.SubprocessError) as exc:
        return False, str(exc)
    ok = done.returncode == 0
    return ok, (done.stdout if ok else (done.stderr or done.stdout)).strip()


def _open_private(path: str, mode: str = "w"):
    """Open `path` for writing so that it is NEVER readable by others, not
    even for the moment between open() and chmod(): a new file is created
    0600. An existing file is rewritten in place, keeping its owner, group
    and mode -- the 'mosquitto' group --autostart gave broker.key survives --
    and the caller then settles the final mode."""
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    return os.fdopen(fd, mode, **({} if "b" in mode else {"encoding": "utf-8"}))


def _download(url: str, dest: str, sha256: Optional[str] = None) -> bool:
    """Fetch `url` to `dest`. `sha256` is checked when given; when it is
    not, the download is said to be UNVERIFIED -- the registry below carries
    no hashes yet (none were invented), so today that is every asset."""
    os.makedirs(os.path.dirname(os.path.abspath(dest)), exist_ok=True)
    tmp = dest + ".part"
    try:
        print(f"    Downloading: {url}")
        # A standard browser-like User-Agent -- some CDNs (including GitHub's
        # raw-content servers, occasionally) treat bare/unidentified requests
        # more strictly, which can surface as a reset connection rather than
        # a clean error.
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        # A timeout, so a stalled connection fails instead of hanging setup
        # (and an unattended boot) forever. It bounds each socket wait, not
        # the whole download.
        with urllib.request.urlopen(req, timeout=60) as response:
            total_size = int(response.headers.get("Content-Length", 0))
            downloaded = 0
            with open(tmp, "wb") as f:
                while True:
                    chunk = response.read(1 << 16)
                    if not chunk:
                        break
                    f.write(chunk)
                    downloaded += len(chunk)
                    if total_size > 0:
                        sys.stdout.write(
                            f"\r    {_bar(downloaded, total_size)}  "
                            f"{_human_size(downloaded)}/{_human_size(total_size)}   ")
                    else:
                        sys.stdout.write(f"\r    {_human_size(downloaded)} ...   ")
                    sys.stdout.flush()
        sys.stdout.write("\n")
        if sha256:
            digest = hashlib.sha256()
            with open(tmp, "rb") as f:
                for block in iter(lambda: f.read(1 << 20), b""):
                    digest.update(block)
            if digest.hexdigest().lower() != sha256.strip().lower():
                print(f"    [ERROR] SHA-256 mismatch for {url}: got "
                      f"{digest.hexdigest()}, expected {sha256}. Discarded.")
                _unlink(tmp)
                return False
            print("    SHA-256 verified.")
        else:
            print("    [WARN] no sha256 recorded for this asset -- the "
                  "download is unverified (TLS only).")
        os.replace(tmp, dest)
        print(f"    Saved: {dest}")
        return True
    except (OSError, ValueError, http.client.HTTPException) as e:
        sys.stdout.write("\n")
        print(f"    [ERROR] Download failed: {e}")
        _unlink(tmp)
        return False


def _download_and_extract_archive(url: str, extract_dir: str,
                                  sha256: Optional[str] = None) -> bool:
    """Download a .tar.bz2 to a temp file and extract it, using stdlib
    tarfile rather than shelling out to `tar` -- an external tar binary
    isn't guaranteed on Windows, and this file already needs to run
    identically on Augusta's dev machine and the Pi."""
    os.makedirs(extract_dir, exist_ok=True)
    tmp_archive = os.path.join(extract_dir, "_download.tar.bz2.part")

    if not _download(url, tmp_archive, sha256):
        return False

    try:
        print(f"    Extracting to: {extract_dir}")
        with tarfile.open(tmp_archive, mode="r:bz2") as tf:
            # Guard against path traversal in a maliciously-crafted archive
            # (CVE-2007-4559-style) -- not expected from our own release
            # asset, but cheap insurance for a function that extracts
            # arbitrary tar members.
            def _is_within(base: str, target: str) -> bool:
                base = os.path.abspath(base)
                target = os.path.abspath(target)
                return target == base or target.startswith(base + os.sep)

            safe_members = []
            for member in tf.getmembers():
                target_path = os.path.join(extract_dir, member.name)
                if _is_within(extract_dir, target_path):
                    safe_members.append(member)
                else:
                    print(f"    [WARN] Skipping unsafe archive member: {member.name}")

            # The name check above does not cover link members: a symlink
            # member pointing outside, followed by a file written through it,
            # escapes anyway. The "data" filter (3.11.4+/3.12) refuses those,
            # device nodes and setuid bits.
            if hasattr(tarfile, "data_filter"):
                tf.extractall(extract_dir, members=safe_members, filter="data")
            else:
                safe_members = [m for m in safe_members
                                if not (m.issym() or m.islnk() or m.isdev())]
                tf.extractall(extract_dir, members=safe_members)

        # The release tarball's own top-level directory
        # (sherpa-onnx-kws-zipformer-gigaspeech-3.3M-2024-01-01/...) would
        # nest everything one level deeper than extract_dir expects --
        # flatten if that's what happened, so tokens.txt etc. end up
        # directly inside extract_dir, matching where config.py's paths
        # point.
        entries = [e for e in os.listdir(extract_dir) if not e.startswith("_download")]
        if len(entries) == 1:
            nested = os.path.join(extract_dir, entries[0])
            if os.path.isdir(nested) and os.path.isfile(os.path.join(nested, "tokens.txt")):
                for item in os.listdir(nested):
                    os.replace(os.path.join(nested, item), os.path.join(extract_dir, item))
                os.rmdir(nested)

        return True
    except (tarfile.TarError, OSError) as e:
        print(f"    [ERROR] Extraction failed: {e}")
        return False
    finally:
        _unlink(tmp_archive)


# ===========================================================================
# --doctor
# ===========================================================================
#
# One command that walks the WHOLE chain and says which link is broken.
#
# It exists because every failure so far has looked like a different
# failure. A board that cannot reach the broker looks like bad Wi-Fi. A
# broker with no accounts looks like a wrong password. Home Assistant in
# Docker dialling 127.0.0.1 looks like a broken integration. In each case
# the information needed to tell them apart was available, and scattered
# across four programs.
#
# Every check reports and CONTINUES. Stopping at the first problem means
# finding them one round trip at a time, which is exactly the evening this
# is meant to prevent.

def _port_open(host: str, port: int, timeout: float = 1.5) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def _local_addresses() -> set:
    """Every IPv4 address this machine currently holds, on EVERY interface.

    It used to ask two questions -- "what does my hostname resolve to?"
    and "which address routes to the internet?" -- and on a Pi the first
    answers 127.0.1.1 (from /etc/hosts) and the second names exactly one
    interface. A hub running its hotspot on wlan0 with the router on usb0
    holds two addresses and this reported one, so --pair drew a code for
    one road and forgot the other, and the doctor said "192.168.4.1 is not
    one of this machine's addresses" about an address it was serving
    (2026-09-09). Now the interfaces are listed directly: `ip -4 -o addr`
    on Linux, `ipconfig` on Windows, psutil if it is there, and the two old
    questions only as the last resort.
    """
    import re
    found = set()
    try:
        if os.name == "nt":
            out = subprocess.run(["ipconfig"], capture_output=True, text=True,
                                 timeout=10).stdout
            found.update(re.findall(r"IPv4[^:]*:\s*(\d+\.\d+\.\d+\.\d+)", out))
        else:
            out = subprocess.run(["ip", "-4", "-o", "addr", "show"],
                                 capture_output=True, text=True, timeout=10).stdout
            found.update(re.findall(r"\binet (\d+\.\d+\.\d+\.\d+)/", out))
    except Exception:                                  # noqa: BLE001
        pass
    if not found:
        try:
            import psutil                                  # type: ignore
            for addrs in psutil.net_if_addrs().values():
                for a in addrs:
                    if a.family == socket.AF_INET:
                        found.add(a.address)
        except Exception:                              # noqa: BLE001
            pass
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None,
                                       socket.AF_INET):
            found.add(info[4][0])
    except OSError:
        pass
    found.add(hub_net.route_address())               # "" when no route
    return {a for a in found if a and not a.startswith("127.")}


# ===========================================================================
# Bringing the stack up
# ===========================================================================
#
# Three things have to be running before a board can reach Home Assistant:
# the broker, Home Assistant itself, and the hub. Two of them are servers
# somebody had to remember to start by hand after every reboot, and the cost
# of forgetting was never a clear error -- it was a board that connected to
# nothing, or worse, connected to the WRONG broker.
#
# THE RULE HERE IS: NEVER START A SECOND ONE. On Windows the Mosquitto
# installer registers a service that comes back at every boot with its own
# default config, knowing nothing about our accounts. For an evening that
# service held 0.0.0.0:1883 while our broker sat on loopback, so every board
# reached the imposter and the log we were watching stayed empty. So the
# first thing any of this does is look at the port.
#
# Same code on the Pi and on Windows, deliberately: the difference between
# them is which command launches a process, and everything above that is the
# same question -- is it up, is it ours, can it be reached.

def _pid_path() -> str:
    return os.path.join(config.BROKER_DIR, "mosquitto.pid")


def _broker_log_path() -> str:
    return os.path.join(config.BROKER_DIR, "mosquitto.log")


def _read_pid() -> Optional[int]:
    try:
        with open(_pid_path(), encoding="utf-8") as f:
            return int(f.read().strip())
    except (OSError, ValueError):
        return None


def _process_is_mosquitto(pid: int) -> bool:
    """Is this PID alive AND actually the broker?

    The second half matters. A pid file outlives the process that wrote it,
    the number gets recycled, and "the broker is running" then means "some
    unrelated program has that number today".
    """
    if os.name == "nt":
        try:
            out = subprocess.run(
                ["tasklist", "/FI", f"PID eq {pid}", "/NH"],
                capture_output=True, text=True, timeout=10).stdout.lower()
        except (OSError, subprocess.SubprocessError):
            return False
        return "mosquitto" in out
    try:
        with open(f"/proc/{pid}/cmdline", "rb") as f:
            return b"mosquitto" in f.read().lower()
    except OSError:                    # no such process (Linux; the Pi)
        return False


def _broker_ports() -> Tuple[int, int]:
    """(the boards' port, Home Assistant's port), matching MOSQUITTO_CONF.

    With TLS off there is no TLS listener at all (see _setup_broker): the
    boards share the plain one, so both answers are 1883. Returning 8883
    there made _verify_broker find "nothing listening" on a port nobody
    opened, so --autostart always rolled back and --doctor always failed.
    """
    tls_port = config.MQTT_PORT if config.MQTT_TLS else 1883
    return tls_port, 1883


def broker_status() -> Tuple[str, str]:
    """('ours' | 'foreign' | 'down', detail).

    'foreign' is the important one and it is the reason this returns three
    states rather than a bool: something IS listening, so starting another
    would fail to bind or -- worse, if the ports differ -- succeed and leave
    two brokers up, which is exactly the fault that cost an evening.
    """
    busy = [p for p in _broker_ports() if _port_open("127.0.0.1", p)]
    pid = _read_pid()
    if pid is not None and _process_is_mosquitto(pid):
        return (("ours", f"pid {pid}") if busy
                else ("down", "pid file is stale"))
    if busy:
        # NAME THE PORT ACTUALLY FOUND. The first version printed the plain
        # port whichever one had answered, which is precisely the kind of
        # near-miss detail that sends you to look at the wrong listener.
        ports = " and ".join(str(p) for p in busy)
        return "foreign", f"something else is listening on {ports}"
    return "down", ""


def _has_ambient_caps() -> bool:
    """Does this process hold ambient capabilities (Linux) that a child
    would inherit?"""
    try:
        with open("/proc/self/status", encoding="ascii") as f:
            return any(line.startswith("CapAmb:") and int(line.split()[1], 16)
                       for line in f)
    except (OSError, ValueError, IndexError):
        return False


def start_broker(say=print) -> bool:
    """Start Mosquitto with OUR config, detached, and prove it came up.

    Detached on purpose: it must outlive whatever started it, so that
    `--start` from a terminal and the hub's own startup both leave a broker
    behind rather than one that dies with its parent.
    """
    state, detail = broker_status()
    if state == "ours":
        say(f"    Broker already running ({detail}).")
        return True
    if state == "foreign":
        if _autostarted():
            # --autostart handed the broker to the system service, with this
            # hub's config: not ours to start, and not a rival either.
            say("    Broker running as the system service (--autostart).")
            return True
        say(f"    [X] {detail}, and it is not ours.")
        if os.name == "nt":
            say("        That is almost certainly the Mosquitto Windows "
                "service, which runs its own default config and knows "
                "nothing about your accounts. In an Administrator prompt:")
            say("            net stop mosquitto")
            say("            sc config mosquitto start= demand")
        else:
            say("            sudo systemctl stop mosquitto")
        say("        Then run this again.")
        return False

    if not os.path.isfile(config.MQTT_CONF_FILE):
        say("    [X] No broker config yet -- run: "
            "python setup_manager.py --broker")
        return False
    broker = find_mosquitto_tool("mosquitto")
    if broker is None:
        say("    [X] Mosquitto is not installed.")
        return False

    os.makedirs(config.BROKER_DIR, exist_ok=True)
    # ONE GENERATION KEPT. -v logs every keepalive of every board, appended
    # for ever, on an SD card; past 10MB the old log becomes .1 at start.
    try:
        if os.path.getsize(_broker_log_path()) > 10 << 20:
            os.replace(_broker_log_path(), _broker_log_path() + ".1")
    except OSError:
        pass                                   # no log yet
    log = open(_broker_log_path(), "a", encoding="utf-8", errors="replace")
    try:
        # -v because the log is the only witness when a board cannot get in,
        # and it goes to a FILE rather than a window: a server whose output
        # nobody is reading should not be holding a terminal open.
        command = [broker, "-c", config.MQTT_CONF_FILE, "-v"]
        # THE HUB'S ONE PRIVILEGE STAYS WITH THE HUB. Under homevoice.service
        # it holds CAP_SYS_TIME as an AMBIENT capability, which every child
        # inherits -- so the broker, listening on the network, could set the
        # clock. setpriv (util-linux) clears it on the way in.
        if _has_ambient_caps() and shutil.which("setpriv"):
            command = ["setpriv", "--ambient-caps=-all", *command]
        detach = ({"creationflags": getattr(subprocess, "DETACHED_PROCESS", 0)
                   | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)}
                  if os.name == "nt" else {"start_new_session": True})
        process = subprocess.Popen(command, stdout=log,
                                   stderr=subprocess.STDOUT, **detach)
    except OSError as exc:
        say(f"    [X] Could not start the broker: {exc}")
        return False
    finally:
        # The child holds its own copy of the descriptor; ours is only a leak.
        log.close()

    with open(_pid_path(), "w", encoding="utf-8") as f:
        f.write(str(process.pid))

    tls_port, plain_port = _broker_ports()
    deadline = time.time() + 10
    while time.time() < deadline:
        if _port_open("127.0.0.1", tls_port) or \
                _port_open("127.0.0.1", plain_port):
            # RECORDED HERE, not at the call site: this is the one place
            # that knows a broker was genuinely launched rather than found.
            _WE_STARTED["broker"] = str(process.pid)
            say(f"    Broker started (pid {process.pid}), listening on "
                + " and ".join(str(p) for p in dict.fromkeys(
                    (tls_port, plain_port))) + ".")
            say(f"    Log: {_broker_log_path()}")
            return True
        if process.poll() is not None:
            break
        time.sleep(0.3)

    # IT DIED, OR NEVER LISTENED. Either way the log says why and a bare
    # "failed to start" does not, so the tail of it goes on screen.
    say("    [X] The broker did not come up.")
    if process.poll() is not None:
        _unlink(_pid_path())            # a dead pid is a number to recycle
    try:
        with open(_broker_log_path(), encoding="utf-8",
                  errors="replace") as f:
            for line in f.read().strip().splitlines()[-6:]:
                say(f"        {line}")
    except OSError:
        pass
    return False


def stop_broker(say=print) -> bool:
    """Stop only the broker THIS started. Never one it merely found."""
    pid = _read_pid()
    if pid is None or not _process_is_mosquitto(pid):
        say("    No broker of ours is running.")
        return True
    try:
        if os.name == "nt":
            subprocess.run(["taskkill", "/PID", str(pid), "/F"],
                           capture_output=True, timeout=15)
        else:
            os.kill(pid, 15)
    except (OSError, subprocess.SubprocessError) as exc:
        say(f"    [X] Could not stop pid {pid}: {exc}")
        return False
    # WAIT FOR IT TO ACTUALLY GO. Signalling a process is not the same as
    # it having exited, and the gap is long enough to matter: a restart --
    # which is how a password change is applied on Windows -- called
    # start_broker while the old one still held the port, saw a listener it
    # no longer had a pid file for, concluded a STRANGER had the port, and
    # refused. The new password was then never applied, which is the exact
    # bug the restart existed to fix.
    deadline = time.time() + 10
    while time.time() < deadline:
        if not any(_port_open("127.0.0.1", p) for p in _broker_ports()):
            break
        time.sleep(0.2)
    _unlink(_pid_path())
    # Forget that we started it, so a later shutdown_stack does not go
    # looking for a process this call has already dealt with.
    _WE_STARTED.pop("broker", None)
    say(f"    Broker stopped (pid {pid}).")
    return True


def ha_is_up(timeout: float = 2.0) -> bool:
    """Does Home Assistant answer at all?

    ANY HTTP reply counts, including 401. The question is whether the server
    is there, not whether this process is allowed in -- and the token is
    checked in about six other places already.
    """
    url = config.HA_BASE_URL
    if not url:
        return False
    try:
        urllib.request.urlopen(url, timeout=timeout)
        return True
    except urllib.error.HTTPError:
        return True
    except Exception:
        return False


def docker_ready(timeout: float = 6.0) -> bool:
    """Is the DAEMON there, not merely the `docker` command?

    These are different facts and confusing them produces a bad diagnosis.
    On Windows the CLI is on PATH from the moment Docker Desktop is
    installed, while the daemon only exists once Desktop is actually
    running. `docker ps` then fails, `find_ha_container` finds nothing, and
    the honest answer -- "Docker is asleep" -- comes out as "no container of
    Home Assistant's looks like it", which sends you looking at containers.
    """
    return shutil.which("docker") is not None and \
        _run(["docker", "info"], timeout=timeout)[0]


#: Where Docker Desktop installs itself on Windows. Checked in order; the
#: per-user path exists because Desktop can be installed without admin.
_DOCKER_DESKTOP_PATHS = (
    r"%ProgramFiles%\Docker\Docker\Docker Desktop.exe",
    r"%ProgramW6432%\Docker\Docker\Docker Desktop.exe",
    r"%LOCALAPPDATA%\Docker\Docker Desktop.exe",
)


def start_docker(say=print, wait: float = 90.0) -> bool:
    """Wake the Docker daemon, and be honest about how long that takes.

    Docker Desktop is genuinely slow -- a minute is normal, more on a cold
    boot -- which is why `wait` is a parameter rather than a constant. The
    hub's own startup gives it whatever is left of its budget and carries on;
    `--start`, where you are sitting watching, gives it the full minute and a
    half.

    THE BETTER FIX IS A CHECKBOX. Docker Desktop -> Settings -> General ->
    "Start Docker Desktop when you sign in" removes this problem rather than
    working around it, and is said so below rather than quietly papered over.
    """
    if docker_ready():
        return True
    if shutil.which("docker") is None:
        say("    Docker is not installed here.")
        return False

    say("    The Docker daemon is not answering. Waking it...")
    if os.name == "nt":
        launcher = None
        for template in _DOCKER_DESKTOP_PATHS:
            candidate = os.path.expandvars(template)
            if "%" not in candidate and os.path.isfile(candidate):
                launcher = candidate
                break
        if launcher is None:
            say("    [X] Docker Desktop is not where it usually installs. "
                "Start it from the Start menu.")
            return False
        try:
            flags = getattr(subprocess, "DETACHED_PROCESS", 0)
            subprocess.Popen([launcher], creationflags=flags,
                             stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL)
        except OSError as exc:
            say(f"    [X] Could not launch Docker Desktop: {exc}")
            return False
    else:
        # NOT sudo. A password prompt from a hub that is starting itself in
        # the background is a hang with no explanation -- there is nobody to
        # answer it, and on the Pi docker is enabled at boot anyway.
        if os.geteuid() != 0:
            say("    [X] The Docker daemon is down and this is not running "
                "as root. Start it with: sudo systemctl start docker")
            say("        To have it come back by itself: "
                "sudo systemctl enable docker")
            return False
        started, detail = _run(["systemctl", "start", "docker"], timeout=30)
        if not started:
            # DO NOT THEN WAIT NINETY SECONDS. systemctl has already said
            # no; polling for a daemon that was refused is ninety seconds of
            # pretending, and it reads as "slow" rather than "declined".
            why = f": {detail.splitlines()[0]}" if detail else "."
            say(f"    [X] systemctl refused to start docker{why}")
            return False

    deadline = time.time() + wait
    while time.time() < deadline:
        if docker_ready():
            say("    Docker is ready.")
            return True
        time.sleep(2.0)

    say(f"    Docker did not come up within {int(wait)}s. It often takes "
        f"longer on a cold boot -- nothing is broken, it is just slow.")
    if os.name == "nt":
        say("        Worth turning on once: Docker Desktop -> Settings -> "
            "General -> 'Start Docker Desktop when you sign in'.")
    return False


def find_ha_container(running: bool = False) -> Optional[Tuple[str, str]]:
    """(id, name) of a Home Assistant container, or None.

    `running=False` looks for a STOPPED one, to start it. `running=True`
    looks for a live one, to stop it on the way out. One function because
    the matching is the interesting part and it is identical either way.

    Matched on the IMAGE rather than on a name in config, because the name
    is whatever docker-compose or the person happened to choose, and asking
    for it is one more thing to keep in step with reality.
    """
    if shutil.which("docker") is None:
        return None
    try:
        out = subprocess.run(
            ["docker", "ps", "-a", "--format",
             "{{.ID}}\t{{.Names}}\t{{.Image}}\t{{.State}}"],
            capture_output=True, text=True, timeout=20)
    except (OSError, subprocess.SubprocessError):
        return None
    if out.returncode != 0:
        return None
    for line in out.stdout.strip().splitlines():
        parts = line.split("\t")
        if len(parts) < 4:
            continue
        cid, name, image, state = parts[0], parts[1], parts[2], parts[3]
        haystack = f"{name} {image}".lower().replace("_", "").replace("-", "")
        if "homeassistant" not in haystack:
            continue
        if (state.lower() == "running") == running:
            return cid, name
    return None


def start_ha(say=print) -> bool:
    """Start Home Assistant's container if it is down and findable."""
    if ha_is_up():
        say("    Home Assistant is already up.")
        return True

    # THE DAEMON BEFORE THE CONTAINER. Asking about containers while the
    # daemon is down gets an error that reads like "there is no such
    # container", which is a different problem with a different fix.
    if not start_docker(say):                         # returns at once if up
        return False
    # It may have been the daemon all along: a container set to restart
    # comes back with it, so ask again before starting anything.
    if ha_is_up():
        say("    Home Assistant is up.")
        return True

    found = find_ha_container()
    if found is None:
        say("    Home Assistant is not answering and no stopped container "
            "of its looks like it. Start it however it normally runs.")
        return False
    cid, name = found
    say(f"    Starting the Home Assistant container {name!r}...")
    started, detail = _run(["docker", "start", cid])
    if not started:
        say(f"    [X] docker start failed: {detail}")
        return False
    # OURS TO STOP, because we started it. A container that was already
    # running never reaches this line: ha_is_up() returned long before.
    _WE_STARTED["ha_container"] = cid
    _WE_STARTED["ha_name"] = name

    deadline = time.time() + 25.0
    while time.time() < deadline:
        if ha_is_up():
            say("    Home Assistant is up.")
            return True
        time.sleep(1.0)
    # NOT A FAILURE. Home Assistant takes its time, and reporting a problem
    # that resolves itself thirty seconds later teaches you to ignore this.
    say(f"    {name!r} is started but not answering yet -- it usually takes "
        f"a minute. Nothing else needs doing.")
    return True


#: What THIS process actually started, and therefore what it may stop.
#:
#: The rule everywhere in this file: never stop something you merely found.
#: A broker that was already up belongs to the boot sequence or to another
#: window, and Home Assistant runs the automations for the whole house --
#: closing a console is not a reason to take either of them down. Empty
#: after a run where everything was already up, which is the normal case
#: once --autostart has been used, and then shutdown does nothing at all.
_WE_STARTED: Dict[str, str] = {}

#: Set once shutdown has run, so the two exit paths cannot both do it.
_SHUTDOWN_DONE = False


#: The whole exit path, end to end. Quitting must be BOUNDED and it must be
#: SHORT, because a program that takes two minutes to close is a program that
#: has hung as far as anyone watching is concerned -- and the reflex is
#: Ctrl-C, which is exactly when a database gets caught mid-write.
#:
#: This was 160 seconds: sixty for a service stop, ten for ports, ninety for
#: docker. Each number was defensible and the total was not.
SHUTDOWN_BUDGET = 25.0


def _stop_ha_container(container: str, name: str, say=print,
                       budget: float = 18.0) -> None:
    say(f"    Stopping {name!r} (up to {int(budget)}s)...")
    try:
        # -t is DOCKER'S OWN grace period before it escalates to SIGKILL, and
        # it has to be shorter than our timeout or we give up while docker is
        # still politely waiting. Home Assistant closes its database on
        # SIGTERM and normally takes a few seconds; this is generous for that
        # and still quick enough that quitting feels like quitting.
        subprocess.run(["docker", "stop", "-t", str(max(1, int(budget) - 6)),
                        container],
                       capture_output=True, timeout=budget)
        say("    Stopped.")
    except subprocess.TimeoutExpired:
        # NOT AN ERROR, and not worth waiting out. docker carries on
        # stopping it after we leave.
        say(f"    {name!r} is taking its time -- docker will finish stopping "
            f"it on its own. Carrying on.")
    except (OSError, subprocess.SubprocessError) as exc:
        say(f"    [--] could not stop it: {exc}")


def shutdown_stack(say=print) -> None:
    """Stop, on the way out, only what THIS run started -- after --autostart,
    nothing. The hub is the thing that restarts -- after a crash, an update,
    an edit -- and servers it merely found are meant to outlive it. A Pi
    that tore down its own broker on every hub restart would drop every
    board for a minute for no reason.

    DOCKER DESKTOP IS NEVER STOPPED. It is an operating-system service
    hosting whatever else you run, and stopping the Home Assistant container
    is the part that belongs to this project.
    """
    global _SHUTDOWN_DONE
    if _SHUTDOWN_DONE or not _WE_STARTED:
        # BOTH exit paths call this -- the shell's `finally` and the one in
        # __main__, because Ctrl-C can cut the first one short. Running the
        # whole thing twice doubled the worst-case wait for no gain.
        return
    _SHUTDOWN_DONE = True
    started = time.time()
    say("\n  Stopping what this run started")
    if "broker" in _WE_STARTED:
        stop_broker(say)

    # WHATEVER IS LEFT OF THE BUDGET goes to Home Assistant, and if the
    # broker took it all, HA is left to docker rather than making somebody
    # wait. Quitting has to end.
    remaining = max(0.0, SHUTDOWN_BUDGET - (time.time() - started))
    container = _WE_STARTED.get("ha_container")
    if container and remaining < 5.0:
        say("    Not stopping Home Assistant -- quitting is taking long "
            "enough already. It is still running.")
    elif container:
        _stop_ha_container(container, _WE_STARTED.get("ha_name", container),
                           say, budget=remaining)
    _WE_STARTED.clear()


def ensure_stack(say=print) -> bool:
    """Everything the hub needs, up. Safe to run twice.

    Only the broker's result is returned. Home Assistant is advisory: the
    hub has always run without it, and a hub that refuses to start because
    a container is slow would be a worse hub than one that says so.
    """
    say("\n  Starting what the hub needs")
    ok = start_broker(say)
    start_ha(say)
    return ok


# ===========================================================================
# Starting at boot
# ===========================================================================
#
# ensure_stack covers the gap; this closes it. Servers should be started by
# the machine, not by whichever program happens to notice they are missing --
# and a Pi that has to be logged into after every power cut is not a hub, it
# is a demonstration.
#
# It registers the BROKER and DOCKER only. The hub itself stays something you
# start and watch, so its output is in front of you rather than in a service
# log you have to go looking for.
#
# EVERY CHANGE IS SHOWN, CONFIRMED, VERIFIED, AND ROLLED BACK IF IT DID NOT
# WORK. Pointing an operating-system service at our config is precisely the
# manoeuvre that produced two brokers and an empty log, and doing it at BOOT
# would make that permanent instead of merely confusing. So the config is
# backed up first, and afterwards this counts the listeners and tries an
# actual login before calling it done.

# ===========================================================================
# The hub at boot: one secrets file, one systemd unit
# ===========================================================================

_SERVICE_NAME = "homevoice"
_SERVICE_UNIT = f"/etc/systemd/system/{_SERVICE_NAME}.service"
_SECRET_KEYS = ("HOMEVOICE_API_KEY", "HOMEVOICE_FERNET_KEY", "HOMEVOICE_HA_TOKEN")
_BASHRC_MARK = "# HomeVoice: the keys live in ~/.homevoice/env (setup_manager.py --service)"


def _write_secrets_file(path: str, say=print) -> bool:
    """~/.homevoice/env from the keys this process can see. An existing file
    is KEPT (it is the truth; config.py loaded it), and any key the shell
    holds differently is pointed out rather than silently overwritten."""
    present = {k: os.environ.get(k, "") for k in _SECRET_KEYS}
    if os.path.isfile(path):
        say("  [OK]  Keys")
        try:
            if os.stat(path).st_mode & 0o077:
                os.chmod(path, 0o600)
                say("    (it was readable by others -- now 0600)")
        except OSError:
            pass
        loaded = set(config.SECRETS_LOADED)
        missing = [k for k in ("HOMEVOICE_API_KEY", "HOMEVOICE_FERNET_KEY") if k not in loaded]
        if missing:
            # Asked for (your old ones) or made new, and added to the file.
            fill = {k: os.environ.get(k, "") for k in missing}
            made = _fill_keys(fill, say)
            if made is None:
                return False
            for k in missing:
                if not _env_file_set(path, k, fill[k]):
                    return False
            config.SECRETS_LOADED.extend(missing)
            say(f"    Added to {path}: {', '.join(missing)}"
                + (f" (new: {', '.join(made)})" if made else ""))
        return True
    # A new hub: your old keys if you have them (asked; typed or pasted,
    # not shown), else new ones. Keys already in this shell are used.
    made = _fill_keys(present, say)
    if made is None:
        return False
    os.makedirs(os.path.dirname(path), exist_ok=True, mode=0o700)
    with _open_private(path) as f:
        f.write("# HomeVoice secrets -- the ONE place the keys live. Owner-only.\n")
        f.write("# Read by config.py at import (the file wins over the shell) and by\n")
        f.write("# systemd through EnvironmentFile=. KEY=VALUE, no quotes needed.\n")
        for k in _SECRET_KEYS:
            if present[k]:
                f.write(f"{k}={present[k]}\n")
    say("  [OK]  Keys saved" + (" (new)" if made else "") + f" in {path}")
    # As if config.py had read it: a later step in this run checks the list.
    config.SECRETS_LOADED.extend(k for k in _SECRET_KEYS
                                 if present[k] and k not in config.SECRETS_LOADED)
    return True


def _fill_keys(present: dict, say=print) -> Optional[List[str]]:
    """HOMEVOICE_API_KEY and HOMEVOICE_FERNET_KEY where `present` has none.
    Asked for first: a hub moved to a new machine keeps its paired phones
    and its saved voices only with its OLD keys. Typed or pasted without
    being shown; Enter alone, or no old keys, makes a new one. Fills
    `present` and the environment. Returns the names made new, or None
    when the Fernet key cannot be checked or made."""
    try:
        from cryptography.fernet import Fernet
    except ImportError:
        say("    [X] the cryptography package is missing -- run setup_manager.py first.")
        return None
    need = [k for k in ("HOMEVOICE_API_KEY", "HOMEVOICE_FERNET_KEY")
            if k in present and not present[k]]
    if not need:
        return []
    old = False
    if not NONINTERACTIVE:
        say("    No " + " or ".join(need) + " yet. Old keys keep your phones and voices.")
        old = _prompt_yes_no("    Do you have the old key(s) to paste in?")
    made = []
    for k in need:
        value = ""
        while old:
            value = getpass.getpass(
                f"    {k} (not shown; Enter alone makes a new one): ").strip()
            if not value or k != "HOMEVOICE_FERNET_KEY":
                break
            try:
                Fernet(value.encode())
                break
            except Exception:
                say("    That is not a Fernet key (44 characters, ending in '='). Try again.")
        if not value:
            value = (secrets.token_urlsafe(32) if k == "HOMEVOICE_API_KEY"
                     else Fernet.generate_key().decode())
            made.append(k)
        present[k] = value
        os.environ[k] = value
    return made


def _env_file_set(path: str, key: str, value: str) -> bool:
    """KEY=value in the secrets file, replacing an old line; owner-only."""
    try:
        lines = []
        if os.path.isfile(path):
            with open(path, encoding="utf-8-sig") as f:
                lines = [line for line in f.read().splitlines()
                         if not line.strip().removeprefix("export ").strip()
                         .startswith(key + "=")]
        lines.append(f"{key}={value}")
        os.makedirs(os.path.dirname(path), exist_ok=True, mode=0o700)
        with _open_private(path) as f:
            f.write("\n".join(lines) + "\n")
        os.environ[key] = value
        return True
    except OSError as exc:
        print(f"    [X] could not write {path}: {exc}")
        return False


def _bashrc_points_at_secrets(home: str, say=print) -> None:
    """Make interactive shells read the same file, so `echo $HOMEVOICE_API_KEY`
    in a terminal and the hub under systemd can never disagree. Appended
    once, behind a marker; the old export lines are left in place but the
    file is sourced AFTER them, so the file wins there too."""
    bashrc = os.path.join(home, ".bashrc")
    try:
        with open(bashrc, encoding="utf-8") as f:
            if _BASHRC_MARK in f.read():
                return
    except OSError:
        pass
    try:
        with open(bashrc, "a", encoding="utf-8") as f:
            f.write(f"\n{_BASHRC_MARK}\n"
                    f"if [ -r \"$HOME/.homevoice/env\" ]; then set -a; . \"$HOME/.homevoice/env\"; set +a; fi\n")
        say(f"    Added a line to {bashrc} so terminals read the same file.")
    except OSError as exc:
        say(f"    (could not edit {bashrc}: {exc} -- add the source line yourself)")


def _systemd_path(path: str) -> str:
    """A path for a single-path unit setting (WorkingDirectory=,
    EnvironmentFile=). Those take the rest of the line verbatim -- spaces
    are fine, quotes would become part of the name -- but do expand %
    specifiers, so a literal % must be doubled."""
    return path.replace("%", "%%")


def _systemd_arg(arg: str) -> str:
    """One ExecStart= argument, double-quoted. The project folder is
    "Voice AI" -- with a space -- and a venv inside it puts the space in the
    interpreter's path too, which an unquoted ExecStart splits in two.
    Inside the quotes systemd honours C escapes, and expands % specifiers
    and $VARIABLES, so all of those are escaped."""
    escaped = (arg.replace("\\", "\\\\").replace('"', '\\"')
               .replace("%", "%%").replace("$", "$$"))
    return f'"{escaped}"'


def _service_unit(user: str, home: str, hub_dir: str, python: str) -> str:
    import grp as _grp
    import pwd as _pwd
    groups = [g for g in ("gpio", "dialout", "netdev", "i2c", "spi")
              if _group_exists(_grp, g)]
    # The user's REAL primary group. Group={user} assumed a same-named
    # group, which a Debian-style install has and many others do not --
    # there the unit fails at start with status=216/GROUP.
    try:
        group = _grp.getgrgid(_pwd.getpwnam(user).pw_gid).gr_name
    except (KeyError, OSError):
        group = user
    return f"""[Unit]
Description=HomeVoice hub (ws_server: phones, boards, the NLU stack)
# tailscaled.service is ordered BEFORE the hub, not required: the hub reads
# its own Tailscale name when it starts, and starting ahead of the daemon
# meant no remote address until the next restart. On a machine without
# Tailscale the name matches no unit and is ignored.
After=network-online.target tailscaled.service
Wants=network-online.target

[Service]
Type=simple
User={user}
Group={group}
SupplementaryGroups={' '.join(groups)}
WorkingDirectory={_systemd_path(hub_dir)}
EnvironmentFile={_systemd_path(os.path.join(home, ".homevoice", "env"))}
Environment=PYTHONUNBUFFERED=1
# THE TAILSCALE OPERATOR, RE-ASSERTED AS ROOT ON EVERY START. The install
# step below sets it too -- but only if tailscale was ALREADY installed at
# that moment. A hub that gets Tailscale afterwards (the usual order) was
# left with no operator, so the app's very first join was refused with
# "access denied" and had to be typed on the Pi by hand (2026-09-09). The
# '+' runs this one line as root even though the service is {user}; the
# '-' means a hub with no tailscale installed starts exactly as before.
ExecStartPre=-+/bin/sh -c 'command -v tailscale >/dev/null 2>&1 && tailscale set --operator={user} || true'
ExecStart={_systemd_arg(python)} {_systemd_arg(os.path.join(hub_dir, "ws_server.py"))}
Restart=always
RestartSec=5
# mixed, ON PURPOSE: a broker the hub started is the hub's (shutdown_stack
# stops it on the way out), so after a crash it goes too, instead of being
# adopted by the next run and never stopped. A broker that must outlive the
# hub is the system one, in its own unit: setup_manager.py --autostart.
KillMode=mixed
TimeoutStopSec=25

# The ONE privilege the hub holds beyond an ordinary user: setting the
# clock from a paired phone after a power cut (hub/hub_clock.py). Ambient
# so the process has it from its first instruction; the bounding set so it
# can never acquire another. No sudo, no sudoers file, nothing granted to
# the user's shell.
AmbientCapabilities=CAP_SYS_TIME
CapabilityBoundingSet=CAP_SYS_TIME
NoNewPrivileges=yes

[Install]
WantedBy=multi-user.target
"""


def _group_exists(grp_module, name: str) -> bool:
    try:
        grp_module.getgrnam(name)
        return True
    except KeyError:
        return False


def _service(argv) -> bool:
    """The hub at boot, as the user who owns it. Two halves:

      as you    write ~/.homevoice/env from the keys in this shell, and make
                .bashrc read it;
      as root   write the unit, daemon-reload, enable --now, and show it
                running -- done by re-running this script under sudo, so
                one command does the whole thing and no environment has to
                be smuggled through sudo.
    """
    if os.name == "nt":
        return _startup_windows()
    hub_dir = str(config.PROJECT_ROOT)
    if "--service-root" in argv:
        return _service_root(argv, hub_dir)
    # WHO IS ACTUALLY RUNNING THIS, from the uid rather than $USER, which
    # `su` without a login (and plenty of other paths) leave stale.
    try:
        import pwd as _pwd
        user = _pwd.getpwuid(os.getuid()).pw_name
    except (ImportError, KeyError):
        user = os.environ.get("USER") or ""
    home = os.path.expanduser("~")
    if not user or _is_admin():
        print("  Run this as the user the hub belongs to, without sudo:")
        print("      python3 setup_manager.py --service")
        print("  It asks for your password itself for the root half.")
        return False
    # --quiet (the full setup): only the result.
    quiet = "--quiet" in argv
    hush = (lambda *a, **k: None) if quiet else print
    hush("\n  The hub at boot")
    env_file = os.path.join(home, ".homevoice", "env")
    if not _write_secrets_file(env_file, say=hush):
        return False
    _bashrc_points_at_secrets(home, say=hush)
    if not os.path.isfile(os.path.join(hub_dir, "ws_server.py")):
        print(f"    [X] ws_server.py is not beside this script in {hub_dir}")
        return False
    hush("    Now the root half (systemd unit) -- sudo will ask for your password:")
    cmd = ["sudo", sys.executable, os.path.join(hub_dir, "setup_manager.py"), "--service-root",
           "--user", user, "--home", home]
    # sudo asks on the terminal itself, so its question shows when quiet.
    done = subprocess.run(cmd, capture_output=quiet, text=True)
    if quiet:
        if done.returncode == 0:
            print("  [OK]  The hub starts at every boot (journalctl -u homevoice -f shows its log)")
        else:
            print((done.stdout or "") + (done.stderr or ""))
    return done.returncode == 0


def _startup_entry() -> str:
    return os.path.join(os.environ.get("APPDATA", ""), "Microsoft", "Windows",
                        "Start Menu", "Programs", "Startup", "HomeVoice hub.cmd")


def _startup_windows() -> bool:
    """--service on Windows: start the hub when you sign in, from a small file
    in your own Startup folder. No admin rights and no stored password (a
    task that ran before sign-in would need Windows to keep your password).
    Delete the file to stop it."""
    if not _BRIEF:
        print("\n  The hub at sign-in")
    hub_dir = str(config.PROJECT_ROOT)
    if not _write_secrets_file(os.path.join(os.path.expanduser("~"), ".homevoice", "env"),
                               say=(lambda *a, **k: None) if _BRIEF else print):
        return False
    server = os.path.join(hub_dir, "ws_server.py")
    if not os.path.isfile(server):
        print(f"    [X] ws_server.py is not beside this script in {hub_dir}")
        return False
    entry = _startup_entry()
    if not os.path.isdir(os.path.dirname(entry)):
        print(f"    [X] No Startup folder at {os.path.dirname(entry)}")
        return False
    cmd = lambda path: path.replace("%", "%%")    # cmd expands %...%
    with open(entry, "w", encoding="utf-8") as f:
        f.write("@echo off\r\n"
                "rem HomeVoice: starts the hub when you sign in (setup_manager.py --service).\r\n"
                "rem Delete this file to stop that.\r\n"
                f'cd /d "{cmd(hub_dir)}"\r\n'
                f'start "HomeVoice hub" /min "{cmd(sys.executable)}" "{cmd(server)}"\r\n')
    if _BRIEF:
        print("  [OK]  The hub starts when you sign in")
    else:
        print(f"    Wrote {entry}")
        print("    The hub starts, minimised, each time you sign in. Delete that file to stop it.")
    return True


def _service_root(argv, hub_dir: str) -> bool:
    if not _is_admin():
        print("  --service-root needs root; use --service, which asks for it.")
        return False
    try:
        user = argv[argv.index("--user") + 1]
        home = argv[argv.index("--home") + 1]
    except (ValueError, IndexError):
        print("  --service-root is called by --service; run that instead.")
        return False
    # These two go into a unit file, a polkit rule (JavaScript) and a shell
    # line run as root, so they must name a real account -- not merely be
    # whatever text followed --user.
    try:
        import pwd as _pwd
        _pwd.getpwnam(user)
    except (ImportError, KeyError):
        print(f"  --user {user!r} is not an account on this machine.")
        return False
    if (not os.path.isabs(home) or any(c in user for c in "'\"\\ \n")
            or "\n" in home):
        print("  --service-root: unusable --user/--home; run --service instead.")
        return False
    python = sys.executable
    unit = _service_unit(user, home, hub_dir, python)
    with open(_SERVICE_UNIT, "w", encoding="utf-8") as f:
        f.write(unit)
    os.chmod(_SERVICE_UNIT, 0o644)
    print(f"    Wrote {_SERVICE_UNIT}")
    # If the system's own Mosquitto still holds 1883 with its default
    # config the hub's broker cannot start; --autostart is the deliberate
    # way to hand the broker to systemd, and this is the other way round:
    # the hub starts its own broker. Both cannot be true at once.
    if (_run(["systemctl", "is-enabled", "mosquitto"])[1] == "enabled"
            and not os.path.isfile(_AUTOSTART_CONF)):
        print("    The system Mosquitto is enabled with its default config, which would take port 1883")
        print("    from the hub's own broker at every boot. Disabling it (the hub starts its own):")
        subprocess.run(["systemctl", "disable", "--now", "mosquitto"], capture_output=True)
    # WHAT A SERVICE MAY NOT DO THAT A LOGIN MAY. Two things the hub does
    # from the app need permissions a systemd service lacks by default:
    #   * changing Wi-Fi (Change ESP Wi-Fi's "move this hub too", and
    #     hub_net.join_wifi): NetworkManager asks polkit, and polkit says
    #     "Not authorized to control networking" to a process with no
    #     login session. One rule, scoped to this user and to
    #     NetworkManager's actions only.
    #   * joining Tailscale from the app: tailscaled's socket answers root
    #     or its named operator; `tailscale set --operator` names the hub's
    #     user, once, so `tailscale up` works without sudo.
    rules_dir = "/etc/polkit-1/rules.d"
    if os.path.isdir(rules_dir):
        rule = os.path.join(rules_dir, "50-homevoice-network.rules")
        with open(rule, "w", encoding="utf-8") as f:
            f.write('// HomeVoice: the hub (running as a service, as %s) may change\n'
                    '// networking through NetworkManager -- what it does from the app\n'
                    '// when the boards move to a new Wi-Fi -- and may RELOAD the system\n'
                    '// Mosquitto, which after --autostart is how a new board password\n'
                    '// takes effect (config.MQTT_RELOAD_COMMAND). Nothing else is granted.\n'
                    'polkit.addRule(function(action, subject) {\n'
                    '    if (subject.user === "%s" &&\n'
                    '        (action.id.indexOf("org.freedesktop.NetworkManager.") === 0 ||\n'
                    '         (action.id === "org.freedesktop.systemd1.manage-units" &&\n'
                    '          action.lookup("unit") === "mosquitto.service" &&\n'
                    '          action.lookup("verb") === "reload"))) {\n'
                    '        return polkit.Result.YES;\n'
                    '    }\n'
                    '});\n' % (user, user))
        os.chmod(rule, 0o644)
        print(f"    Wrote {rule} (NetworkManager control and Mosquitto reload for {user})")
    ts = shutil.which("tailscale")
    if ts:
        ok, detail = _run([ts, "set", f"--operator={user}"])
        print(f"    tailscale: {user} is the operator (the app can join a tailnet without sudo)"
              if ok else f"    tailscale set --operator failed: {detail}")
    for step in (["systemctl", "daemon-reload"],
                 ["systemctl", "enable", _SERVICE_NAME],
                 ["systemctl", "restart", _SERVICE_NAME]):
        ok, detail = _run(step)
        if not ok:
            print(f"    [X] {' '.join(step)}: {detail}")
            return False
    print("    Enabled and started. Waiting a moment for it to come up...")
    time.sleep(6)
    active, status = _run(["systemctl", "is-active", _SERVICE_NAME])
    print(f"    homevoice.service is {status}")
    print()
    print("    Watch it:      journalctl -u homevoice -f")
    print("    Restart it:    sudo systemctl restart homevoice")
    print("    Stop it before running ws_server.py by hand:  sudo systemctl stop homevoice")
    return active


def _clock_sudo() -> bool:
    """Install the one root capability the hub needs on a Pi: setting the
    clock from a paired phone after a power cut (hub/hub_clock.py).

    Two files, both root-owned: the wrapper /usr/local/bin/homevoice-setclock
    (one integer in, `date -u -s` out, nothing else) and a sudoers rule that
    names the wrapper and only the wrapper. NOT a rule on /usr/bin/date --
    see hub_clock's docstring for what that would have given away.
    """
    src = os.path.join(str(config.PROJECT_ROOT), "hub", "homevoice-setclock")
    from hub.hub_clock import SETCLOCK as dest
    rule = "/etc/sudoers.d/homevoice-clock"
    user = os.environ.get("SUDO_USER") or os.environ.get("USER") or ""
    if os.name == "nt":
        print("  Only a Linux hub (the Pi) sets its clock this way.")
        return False
    if not os.path.isfile(src):
        print(f"  {src} is missing -- it ships beside hub_clock.py.")
        return False
    if not _is_admin():
        print("  This writes two root-owned files, so run it once with sudo:")
        print("      sudo python3 setup_manager.py --clock-sudo")
        print("  or by hand:")
        print(f"      sudo install -m 755 {shlex.quote(src)} {dest}")
        print(f"      echo \"$USER ALL=(root) NOPASSWD: {dest}\" | sudo tee {rule}")
        print(f"      sudo chmod 440 {rule}")
        return False
    if not user or user == "root":
        print("  Could not tell which user runs the hub (SUDO_USER is unset).")
        print("  Run this as that user, through sudo: sudo python3 setup_manager.py --clock-sudo")
        return False
    shutil.copyfile(src, dest)
    os.chown(dest, 0, 0)
    os.chmod(dest, 0o755)
    print(f"    Installed {dest} (root, 755)")
    line = f"{user} ALL=(root) NOPASSWD: {dest}\n"
    tmp = rule + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(line)
    os.chown(tmp, 0, 0)
    os.chmod(tmp, 0o440)
    # visudo checks the syntax before the rule goes live; a bad sudoers file
    # can lock sudo for everyone, so this step is not optional.
    ok, detail = _run(["visudo", "-c", "-f", tmp])
    if not ok:
        os.remove(tmp)
        print(f"    visudo refused the rule: {detail}")
        return False
    os.replace(tmp, rule)
    print(f"    Wrote {rule} (root, 440):  {line.strip()}")
    # Prove it end to end as the hub's user: `sudo -n` must succeed without a
    # password, and the wrapper must refuse a bad argument.
    probe = subprocess.run(["sudo", "-n", "-u", user, "sudo", "-n", dest, "abc"],
                           capture_output=True, text=True)
    if probe.returncode == 2:
        print(f"    Checked: {user} can run the wrapper without a password, and it refuses bad input.")
    else:
        print(f"    Check failed (exit {probe.returncode}): {probe.stderr.strip()}")
        return False
    print("  Done. Restart the hub; after a power cut it sets its clock from the first paired phone.")
    return True


def _is_admin() -> bool:
    if os.name != "nt":
        return os.geteuid() == 0
    try:
        import ctypes
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def _listener_count(port: int) -> int:
    """How many things are LISTENING on this port.

    Counted from netstat/ss rather than by trying to connect, because the
    question is not "can I reach a broker" -- it is "is there more than
    one", and a connection cannot tell you that. Two listeners is the
    failure this whole section exists to prevent.

    ONE PROCESS ON BOTH ADDRESS FAMILIES IS ONE BROKER, and this counted it
    as two. A listener bound to every interface appears twice -- once as
    0.0.0.0 and once as [::] -- so a perfectly healthy single Mosquitto was
    reported as "there is more than one broker", which sent the user off to
    stop a Windows service that was not running. 1883 escaped it only by
    accident: MQTT_PLAIN_BIND defaults to 127.0.0.1, so the plain listener
    has no v6 twin to double it.

    So: count PROCESSES where the platform will tell us, and where it will
    not, fold the two wildcard addresses together.
    """
    if os.name != "nt":
        # /proc, not `ss` (which was the fallback here, and only ever ran
        # where /proc is missing -- not a Linux). This container has no ss and no netstat, and
        # a minimal Pi OS image may have neither either -- at which point the
        # check that matters most would quietly return "unknown" and be
        # skipped. The kernel's own table needs nothing installed.
        #
        # Hex addresses here: all-zero is the wildcard in both families, so
        # both fold to "*" and a dual-stack broker counts once.
        seen = set()
        found_table = False
        for name in ("/proc/net/tcp", "/proc/net/tcp6"):
            try:
                with open(name, encoding="utf-8") as f:
                    rows = f.read().splitlines()[1:]
            except OSError:
                continue
            found_table = True
            for row in rows:
                fields = row.split()
                if len(fields) < 4 or fields[3] != "0A":   # 0A = LISTEN
                    continue
                address, _, hex_port = fields[1].rpartition(":")
                try:
                    if int(hex_port, 16) != port:
                        continue
                except ValueError:
                    continue
                # Any all-zero address is the wildcard, whatever its width.
                seen.add("*" if set(address) <= {"0"} else address)
        return len(seen) if found_table else -1   # unknown is not 0

    # -o ON WINDOWS, for the PID column. Counting addresses is what
    # produced the false alarm described above; a PID cannot be double
    # counted by an extra address family.
    ran, out = _run(["netstat", "-ano"], timeout=20)
    if not ran:
        return -1                      # unknown, which is not the same as 0
    pids = set()
    for row in out.splitlines():
        if "LISTENING" not in row.upper():
            continue
        fields = row.split()
        # netstat -ano: proto, local address, foreign address, state, pid
        if len(fields) < 5 or not fields[1].endswith(f":{port}"):
            continue
        pids.add(fields[-1])
    return len(pids)


def _listeners(port: int) -> Tuple[int, str]:
    """How many things listen on `port` (see _listener_count), and that
    said in words. Where they cannot be counted, a connection decides
    between 0 and 1 -- less than a count, more than nothing."""
    count = _listener_count(port)
    if count < 0:
        count = 1 if _port_open("127.0.0.1", port) else 0
    if count == 0:
        return 0, f"nothing is listening on {port}"
    if count > 1:
        return count, (f"{count} things are listening on {port} -- the "
                       f"two-broker fault: Home Assistant and the boards may "
                       f"reach DIFFERENT ones, which looks like accounts "
                       f"breaking at random"
                       + (". net stop mosquitto, then sc config mosquitto "
                          "start= demand" if os.name == "nt" else ""))
    return 1, f"one listener on {port}"


def _login_works(say=print) -> Optional[bool]:
    """True/False, or None when there is nothing to test with."""
    from core.hv_common import new_paho_client
    try:
        client = new_paho_client()
    except ImportError:
        return None
    # THE HUB'S OWN ACCOUNT, not a borrowed board's: with topic ACLs it is
    # the one login the hub actually depends on.
    try:
        user, password = ensure_hub_account()
    except (OSError, ValueError):
        return None
    _, plain_port = _broker_ports()

    # WAIT FOR THE CONNACK, do not merely connect.
    #
    # paho's connect() opens the socket and sends CONNECT, then returns. The
    # broker's verdict arrives afterwards and is delivered to on_connect, so
    # a version of this that called connect() and then disconnect() returned
    # True for a WRONG PASSWORD -- a check that could not fail, sitting in
    # the middle of the one place that decides whether to keep or roll back
    # a change to a system service. Found by a test that deliberately signed
    # in with a stale password and was told it worked.
    verdict: list = []
    client.username_pw_set(user, password)
    client.on_connect = lambda _c, _u, _f, rc, *_a: verdict.append(rc)
    try:
        client.connect("127.0.0.1", plain_port, 10)
    except Exception as exc:                       # noqa: BLE001
        say(f"        ({user} could not reach the broker: {exc})")
        return False
    deadline = time.time() + 10
    while not verdict and time.time() < deadline:
        client.loop(timeout=0.3)
    try:
        client.disconnect()
    except Exception:                              # noqa: BLE001
        pass
    if not verdict:
        say(f"        ({user}: the broker never answered the login)")
        return False
    # 0 is "connection accepted"; 4 and 5 are bad credentials / not
    # authorised, which is exactly what this is looking for.
    if verdict[0] != 0:
        say(f"        ({user} was refused, code {verdict[0]})")
        return False
    return True


def _verify_broker(say=print) -> bool:
    """Exactly one listener per port, and an account that still works."""
    tls_port, plain_port = _broker_ports()
    ok = True
    reachable = True
    if not _acl_in_conf():
        say(f"    [X] no topic ACL in force ({_acl_file()} missing or not "
            f"named by {config.MQTT_CONF_FILE}) -- every board could drive "
            f"every other. Run: python setup_manager.py --broker")
        ok = False
    else:
        say("    [ok] topic ACL file is written and referenced")
    for port in dict.fromkeys((tls_port, plain_port)):   # one port with TLS off
        count, what = _listeners(port)
        say(f"    [{'ok' if count == 1 else 'X'}] {what}")
        ok = ok and count == 1
        reachable = reachable and count > 0
    if not reachable:
        # DO NOT THEN BLAME THE PASSWORD FILE. With nothing listening the
        # login cannot fail for the reason this check is looking for, and
        # saying it did would send you to read a file that is perfectly
        # fine.
        say("    [--] nothing to sign in to, so the accounts were not "
            "tested")
        return ok

    signed_in = _login_works(say)
    if signed_in is True:
        say("    [ok] the hub's own account can still sign in")
    elif signed_in is False:
        say("    [X] the accounts did not survive -- the service is reading "
            "a different password file")
        ok = False
    else:
        say("    [--] could not test a login (paho-mqtt missing, or the hub "
            "account could not be read)")
    return ok


#: Where --autostart puts this hub's config for the system Mosquitto (Linux).
_AUTOSTART_CONF = "/etc/mosquitto/conf.d/homevoice.conf"


def _windows_service_conf() -> Optional[Tuple[str, str, str]]:
    """(the service's mosquitto.conf, the include line --autostart adds to
    it, its current text) -- None when Mosquitto is not installed."""
    broker = find_mosquitto_tool("mosquitto")
    if broker is None:
        return None
    service_conf = os.path.join(os.path.dirname(broker), "mosquitto.conf")
    # THE DIRECTORY HOLDING OUR CONFIG, not BROKER_DIR. include_dir reads
    # every .conf in the folder it is given, so it has to be the folder
    # MQTT_CONF_FILE actually lives in; BROKER_DIR now holds only the
    # broker's own runtime droppings and has no .conf in it at all, which
    # would make the service silently fall back to its own defaults.
    include = f"include_dir {os.path.dirname(config.MQTT_CONF_FILE)}"
    try:
        with open(service_conf, encoding="utf-8", errors="replace") as f:
            return service_conf, include, f.read()
    except OSError:
        return service_conf, include, ""


def _autostarted() -> bool:
    """Has --autostart handed the broker to the system service, running
    this hub's config? Then a listener with no pid file of ours is that
    service -- the intended broker, not a rival for the port."""
    if os.name != "nt":
        return os.path.isfile(_AUTOSTART_CONF)
    found = _windows_service_conf()
    return found is not None and found[1] in found[2]


def _autostart_windows(say=print) -> bool:
    found = _windows_service_conf()
    if found is None:
        say("    Mosquitto is not installed.")
        return False
    service_conf, include, existing = found
    if include in existing:
        say(f"    {service_conf} already includes your config.")
    else:
        say("\n  This will change two things:")
        say(f"    1. append to {service_conf}")
        say(f"         {include}")
        say("       so the Windows service reads YOUR config instead of "
            "its own defaults -- which is what makes it your broker rather "
            "than a rival for the port.")
        say("    2. set the Mosquitto service to start automatically")
        say(f"\n    A backup of {os.path.basename(service_conf)} is taken "
            f"first, and everything is undone if the check afterwards "
            f"fails.")
        if not _prompt_yes_no("\n  Go ahead?"):
            say("    Left alone.")
            return False

        backup = service_conf + ".homevoice-backup"
        if os.path.isfile(service_conf) and not os.path.isfile(backup):
            shutil.copy2(service_conf, backup)
            say(f"    Backed up to {backup}")
        try:
            with open(service_conf, "a", encoding="utf-8") as f:
                f.write(f"\n# Added by HomeVoice setup_manager.\n{include}\n")
        except OSError as exc:
            say(f"    [X] Could not write {service_conf}: {exc}")
            return False

    subprocess.run(["sc", "config", "mosquitto", "start=", "auto"],
                   capture_output=True, timeout=30)
    subprocess.run(["net", "stop", "mosquitto"], capture_output=True,
                   timeout=60)
    stop_broker(say)                    # ours, if we had started one
    started = subprocess.run(["net", "start", "mosquitto"],
                             capture_output=True, text=True, timeout=60)
    if started.returncode != 0:
        say(f"    [X] The service would not start: "
            f"{started.stdout.strip() or started.stderr.strip()}")
        return _rollback_windows(service_conf, say)

    time.sleep(2.0)
    say("\n  Checking it")
    if not _verify_broker(say):
        return _rollback_windows(service_conf, say)

    say("\n    The broker now starts with Windows, using your config.")
    say("    Docker Desktop: Settings -> General -> 'Start Docker Desktop "
        "when you sign in'. That one is a checkbox rather than something "
        "this can set safely.")
    return True


def _rollback_windows(service_conf: str, say=print) -> bool:
    backup = service_conf + ".homevoice-backup"
    say("\n    Undoing it.")
    if os.path.isfile(backup):
        try:
            shutil.copy2(backup, service_conf)
            say(f"    Restored {service_conf}")
        except OSError as exc:
            say(f"    [X] Could not restore the backup: {exc}")
            say(f"        It is at {backup} -- copy it back by hand.")
    subprocess.run(["sc", "config", "mosquitto", "start=", "demand"],
                   capture_output=True, timeout=30)
    subprocess.run(["net", "stop", "mosquitto"], capture_output=True,
                   timeout=60)
    say("    The service is stopped and back to manual. Nothing is worse "
        "than before -- use `python setup_manager.py --start` as you were.")
    return False


#: The account the packaged broker drops to. Started by hand it runs as you;
#: started by systemd it does not, which is the whole of the problem below.
_SERVICE_USER = "mosquitto"


def _lock_down_secret(path: str) -> None:
    """Owner-only -- UNLESS the broker's service account reads this file.

    0600 everywhere used to be the rule, and it broke --autostart at the
    very next password write: _grant_service_user hands the passwd file
    (and broker.key) to the 'mosquitto' group as 0640, and the next chmod
    0600 took that away, so the service's next reload could not open its
    password file and every account vanished. 0640 is kept when the file is
    group-owned by that service group, or is already group-readable and not
    owned by the service user itself; everything else is 0600. On Windows
    the bits are decorative and nothing is done.
    """
    if os.name == "nt":
        return
    try:
        st = os.stat(path)
    except OSError:
        return
    mode = 0o600
    try:
        import grp
        import pwd
        service_gid = grp.getgrnam(_SERVICE_USER).gr_gid
        try:
            service_uid = pwd.getpwnam(_SERVICE_USER).pw_uid
        except KeyError:
            service_uid = None
        if st.st_gid == service_gid:
            mode = 0o640
        elif (st.st_mode & 0o040) and service_uid is not None \
                and st.st_uid != service_uid:
            mode = 0o640
    except (ImportError, KeyError):
        pass
    try:
        os.chmod(path, mode)
    except OSError:
        pass


def _service_user_can_read(path: str) -> Optional[bool]:
    """Can the broker's own account read this? None if it cannot be asked."""
    if os.name == "nt" or shutil.which("su") is None:
        return None
    try:
        result = subprocess.run(
            ["su", "-s", "/bin/sh", _SERVICE_USER, "-c",
             f"test -r {shlex.quote(path)}"], capture_output=True, timeout=20)
    except (OSError, subprocess.SubprocessError):
        return None
    return result.returncode == 0


def _service_user_needs() -> Tuple[List[str], str]:
    """The exact files the broker's own account has to reach, and where it
    has to be able to WRITE.

    Named files, not a directory, because certs/ is shared: it also holds
    local.key, which is the phone's half of the pairing and is no business
    of the broker's. Handing the whole folder to the 'mosquitto' account to
    save three lines would give a service that faces the network the key
    that authenticates the hub to every phone.
    """
    reads = [config.MQTT_PASSWD_FILE]
    if config.MQTT_TLS:
        reads += [config.BROKER_TLS_CERT, config.BROKER_TLS_KEY]
    return reads, config.BROKER_DIR


def _grant_service_user(say=print) -> bool:
    """Let the broker's account reach its files, and say what that costs.

    THE FAILURE THIS PREVENTS is quiet and very confusing. Started by hand,
    Mosquitto runs as you and reads the password file perfectly. Started by
    systemd it drops to the 'mosquitto' user, cannot get into the project
    directory at all, and dies with "Unable to open pwfile" in a journal
    nobody thinks to read -- so autostart appears to do nothing.

    Two different grants, because the files are two different kinds:
      * the password file and the broker's certificate are READ. The file
        gets the group and group-read; its folder gets group-EXECUTE only,
        which is enough to reach a known name and not enough to list what
        else is in there.
      * BROKER_DIR is WRITTEN -- mosquitto.db lands in it -- so that one
        does need group-write.

    Group access, not world, throughout: these files are every board's
    password and the broker's private key.
    """
    reads, writable = _service_user_needs()
    folders = {os.path.dirname(os.path.abspath(p)) for p in reads}

    parents = []
    for start in list(folders) + [writable]:
        walk = os.path.dirname(os.path.abspath(start))
        while walk and walk != os.path.dirname(walk):
            if walk not in parents:
                parents.append(walk)
            walk = os.path.dirname(walk)

    say(f"    Letting the {_SERVICE_USER!r} account reach its files:")
    for path in reads:
        say(f"      group-read on {path}")
    say(f"      group-write on {writable}   (the broker writes its "
        f"database there)")
    say("      chmod g+x on the folders holding them, and o+x on the "
        "folders above those, so they can be reached")
    say("      (group access, not world -- these are every board's password "
        "and the broker's private key)")
    try:
        import grp
        gid = grp.getgrnam(_SERVICE_USER).gr_gid
    except (ImportError, KeyError):
        say(f"    [X] There is no {_SERVICE_USER!r} group on this system.")
        return False
    try:
        for path in reads:
            if not os.path.isfile(path):
                say(f"    [X] {path} does not exist yet -- run --broker "
                    f"first.")
                return False
            os.chown(path, -1, gid)
            os.chmod(path, 0o640)
        for folder in folders:
            os.chown(folder, -1, gid)
            # Traverse, do not list. certs/ has other people's keys in it.
            os.chmod(folder, (os.stat(folder).st_mode & 0o700) | 0o010)
        for root, _, files in os.walk(writable):
            os.chown(root, -1, gid)
            os.chmod(root, 0o770)
            for name in files:
                full = os.path.join(root, name)
                if full == _hub_account_path():
                    # The hub's own password, in the clear, for an account
                    # that sees every topic. The broker never reads it; a
                    # 0660 'mosquitto' copy is a key handed to the service
                    # facing the network. (0600 also repairs an older run.)
                    os.chmod(full, 0o600)
                    continue
                os.chown(full, -1, gid)
                os.chmod(full, 0o660)
        for parent in parents:
            mode = os.stat(parent).st_mode & 0o777
            if not mode & 0o001:
                os.chmod(parent, mode | 0o001)
    except OSError as exc:
        say(f"    [X] Could not change permissions: {exc}")
        return False
    return True


def _autostart_linux(say=print) -> bool:
    target = _AUTOSTART_CONF
    say("\n  This will change three things:")
    say(f"    1. copy your broker config to {target}")
    say("       (/etc/mosquitto/conf.d is already included by the system "
        "config, so this is the supported way in rather than editing "
        "theirs)")
    say("    2. systemctl enable --now mosquitto")
    say("    3. systemctl enable --now docker")

    if not os.path.isfile(config.MQTT_CONF_FILE):
        say("    [X] No broker config yet -- run --broker first.")
        return False

    # PERMISSIONS ARE PART OF THE PLAN, not a surprise afterwards. Detected
    # now so it appears in what you are agreeing to; applied only once you
    # have agreed. Nothing above this line has changed anything.
    reads, _ = _service_user_needs()
    needs_permissions = any(_service_user_can_read(p) is False for p in reads)
    if needs_permissions:
        say(f"    4. let the {_SERVICE_USER!r} account read its own files:")
        for path in reads:
            say(f"         {path}")
        say(f"       Started by systemd the broker runs as {_SERVICE_USER!r} "
            f"rather than as you, and cannot currently read them. Without "
            f"this it would start, fail with 'Unable to open pwfile', and "
            f"look as though nothing happened.")

    say("\n    Everything is undone if the check afterwards fails.")
    if not _prompt_yes_no("\n  Go ahead?"):
        say("    Left alone.")
        return False

    if needs_permissions and not _grant_service_user(say):
        return False
    existed = os.path.isfile(target)
    if existed:
        shutil.copy2(target, target + ".homevoice-backup")
    try:
        os.makedirs(os.path.dirname(target), exist_ok=True)
        with open(config.MQTT_CONF_FILE, encoding="utf-8") as f:
            lines = f.read().splitlines(keepends=True)
        # ONE persistence_location PER BROKER. Debian's (and Raspberry Pi
        # OS's) own mosquitto.conf already sets it before include_dir, and
        # Mosquitto 2 refuses to start on a second one ("Duplicate
        # persistence_location value", checked against 2.0.18) -- so this
        # step always failed its check and rolled back. Theirs is kept.
        if _conf_value("/etc/mosquitto/mosquitto.conf",
                       "persistence_location") is not None:
            lines = [line for line in lines
                     if not line.startswith("persistence_location")]
        with open(target, "w", encoding="utf-8") as f:
            f.writelines(lines)
    except OSError as exc:
        say(f"    [X] Could not write {target}: {exc}")
        return False

    stop_broker(say)                    # ours, so it cannot hold the port
    for unit in ("mosquitto", "docker"):
        ok, detail = _run(["systemctl", "enable", "--now", unit])
        if not ok:
            say(f"    [X] enabling {unit} failed"
                + (f": {detail.splitlines()[0]}" if detail else ""))
            if unit == "mosquitto":
                return _rollback_linux(target, existed, say)
    subprocess.run(["systemctl", "restart", "mosquitto"],
                   capture_output=True, timeout=60)

    time.sleep(2.0)
    say("\n  Checking it")
    if not _verify_broker(say):
        return _rollback_linux(target, existed, say)

    say("\n    The broker and Docker now start at boot. After a power cut "
        "this Pi comes back on its own, with no login.")
    return True


def _rollback_linux(target: str, existed: bool, say=print) -> bool:
    say("\n    Undoing it.")
    backup = target + ".homevoice-backup"
    try:
        if existed and os.path.isfile(backup):
            shutil.copy2(backup, target)
        elif os.path.isfile(target):
            os.remove(target)
    except OSError as exc:
        say(f"    [X] Could not undo {target}: {exc}")
    subprocess.run(["systemctl", "disable", "--now", "mosquitto"],
                   capture_output=True, timeout=60)
    say("    Mosquitto is stopped and no longer starts at boot. Nothing is "
        "worse than before -- use `python setup_manager.py --start`.")
    return False


def autostart(say=print) -> bool:
    """Register the broker and Docker to start at boot."""
    say("\n  Starting at boot")
    if not _is_admin():
        if os.name == "nt":
            say("    This needs an Administrator prompt -- it changes a "
                "Windows service. Right-click Command Prompt -> Run as "
                "administrator, then run this again.")
        else:
            say("    This needs root -- it writes to /etc and enables "
                "services. Try: sudo python setup_manager.py --autostart")
        return False
    if NONINTERACTIVE:
        say("    This changes system services and asks before doing it, so "
            "it needs a terminal. Run it by hand once.")
        return False
    if os.name == "nt":
        return _autostart_windows(say)
    owner = os.environ.get("SUDO_USER")
    if owner and owner != "root":
        # UNDER SUDO $HOME IS ROOT'S (env_reset), so config put BROKER_DIR --
        # the hub's account file, the broker's database -- under /root, and
        # the login check afterwards minted a SECOND hub account there and
        # wrote its password over the real hub's. This is the invoking
        # user's hub, so it is their home.
        import pwd
        home, owner_home = str(Path.home()), pwd.getpwnam(owner).pw_dir
        if home != owner_home and config.BROKER_DIR.startswith(home + os.sep):
            config.BROKER_DIR = owner_home + config.BROKER_DIR[len(home):]
    return _autostart_linux(say)


def _boards_wifi() -> dict:
    """BOARDS_DIR/wifi.json -- the network the boards join and the hub
    address they are sent to -- or {} when there is none to read."""
    from esp_firmware.esp_builder import EspBuilder
    return EspBuilder.headless()._read_wifi()


#: Extensions the ESP32's cut-down X.509 parser understands. Anything else
#: marked CRITICAL makes it refuse the certificate outright rather than skip
#: the extension -- which is what "critical" means, and what cost weeks here.
_ESP_KNOWN_EXTENSIONS = {"basicConstraints", "subjectAltName", "keyUsage",
                         "extendedKeyUsage"}


def _inspect_certificate(path: str):
    """Is this certificate usable by an ESP32? Yields (ok, message).

    WHY THIS EXISTS. The certificate was malformed for its job for weeks and
    nothing said so. Python and browsers accepted it happily; Tasmota's
    BearSSL refused the handshake and hung up, which the broker logged as
    "unexpected eof while reading" and the board as `TLS connection error:
    56`. Neither message named the certificate.

    The properties a board needs are checked HERE, where checking takes a
    second and costs nothing, instead of on a board that has to be flashed
    to find out.

    ERROR 56 IS CHECK 5, NOT CHECK 1. That was diagnosed wrong for a while
    and the wrong thing got fixed first: 56 is BR_ERR_X509_BAD_SERVER_NAME,
    the certificate not naming the address the board dialled. Check 1 --
    that a trust anchor is a CA -- is a genuine requirement and stays, but
    it was never what 56 was reporting.
    """
    try:
        from cryptography import x509
        from cryptography.hazmat.primitives.asymmetric import rsa, ec
    except ImportError:
        yield True, "(install `cryptography` to inspect the certificate)"
        return
    try:
        with open(path, "rb") as f:
            certificate = x509.load_pem_x509_certificate(f.read())
    except Exception as exc:                       # noqa: BLE001
        yield False, f"the certificate could not be read: {exc}"
        return

    # NOTES, NOT FAILURES, for 1, 2 and 5. Boards are provisioned with
    # SetOption132 1, and in that mode Tasmota's validator only hashes the
    # first certificate's public key -- it ignores the server name, the CA
    # flag and every extension (WiFiClientSecureLightBearSSL.cpp,
    # pubkeyfingerprint_*). These checks describe CA mode, which nothing
    # here uses, so they are reported for information and never counted.
    # 1. A trust anchor must be a CA (CA mode only).
    try:
        basic = certificate.extensions.get_extension_for_class(
            x509.BasicConstraints)
        if basic.value.ca:
            yield True, "it is a CA, so a board could use it as a trust anchor"
        else:
            yield True, ("(note) basicConstraints says CA=FALSE -- irrelevant "
                         "in fingerprint mode, which is what boards use")
    except x509.ExtensionNotFound:
        yield True, ("(note) no basicConstraints -- irrelevant in fingerprint "
                     "mode, which is what boards use")

    # 2. Critical extensions the board will not recognise (CA mode only).
    unknown = [e.oid._name for e in certificate.extensions
               if e.critical and e.oid._name not in _ESP_KNOWN_EXTENSIONS]
    if unknown:
        yield True, (f"(note) critical extension(s) a CA-mode board would "
                     f"refuse: {', '.join(unknown)} -- ignored in fingerprint "
                     f"mode")
    else:
        yield True, "no critical extension a board would choke on"

    # 3. A key it can actually do the maths on.
    key = certificate.public_key()
    if isinstance(key, rsa.RSAPublicKey):
        bits = key.key_size
        if bits > 2048:
            yield False, (f"an RSA-{bits} key is beyond what the ESP32's TLS "
                          f"stack will handle in the memory it has; 2048 is "
                          f"the practical ceiling")
        else:
            yield True, f"RSA-{bits} key, which a board can handle"
    elif isinstance(key, ec.EllipticCurvePublicKey):
        yield True, f"EC key on {key.curve.name}"
    else:
        yield False, "an unusual key type; a board is unlikely to accept it"

    # 4. In date -- for the hub. The board's own clock is the harder half.
    now = datetime.datetime.now(datetime.timezone.utc)
    try:
        start = certificate.not_valid_before_utc
        end = certificate.not_valid_after_utc
    except AttributeError:                          # older cryptography
        start = certificate.not_valid_before.replace(tzinfo=datetime.timezone.utc)
        end = certificate.not_valid_after.replace(tzinfo=datetime.timezone.utc)
    if start <= now <= end:
        yield True, f"in date until {end.date()}"
    else:
        yield False, f"out of date ({start.date()} to {end.date()})"

    # 5. Does it name the address the boards are sent to?
    wanted = _boards_wifi().get("hub_address") or ""
    if wanted:
        try:
            names = certificate.extensions.get_extension_for_class(
                x509.SubjectAlternativeName).value
        except x509.ExtensionNotFound:
            names = []
        # THE DNS ENTRIES ARE THE ONES THAT COUNT, even when the address is
        # an IP literal. BearSSL matches the name it dialled against DNS
        # entries only, so an iPAddress entry alone -- which is the
        # technically correct way to write it, and what openssl will show
        # you -- is invisible to the board.
        dns = [str(n.value) for n in names
               if isinstance(n, x509.DNSName)]
        ip = [str(n.value) for n in names
              if isinstance(n, x509.IPAddress)]
        if wanted in dns:
            yield True, f"it names {wanted} as a DNS entry"
        elif wanted in ip:
            yield True, (f"(note) {wanted} is present only as an IP entry -- "
                         f"the name is not checked in fingerprint mode")
        else:
            yield True, (f"(note) it does not name {wanted} at all (lists: "
                         f"{', '.join(dns + ip) or 'nothing'}) -- the name "
                         f"is not checked in fingerprint mode; only the "
                         f"hub's own Python client would care, and it has "
                         f"hostname checking off")


# ===========================================================================
# Phones
# ===========================================================================
#
# The phones that may drive this hub. Each one holds its own Ed25519 private
# key and has registered only the public half here, so nothing in this
# section handles a secret and nothing it prints needs hiding.

def _phone_registry():
    from identity import phones
    return phones.PhoneRegistry()


def _describe_age(stamp: int) -> str:
    if not stamp:
        return "never"
    seconds = int(time.time()) - int(stamp)
    if seconds < 90:
        return "just now"
    for limit, per, unit in ((3600, 60, "minute"), (86400, 3600, "hour"),
                             (86400 * 30, 86400, "day")):
        if seconds < limit:
            n = seconds // per
            return f"{n} {unit}{'s' if n != 1 else ''} ago"
    return time.strftime("%Y-%m-%d", time.localtime(stamp))


def _list_phones() -> bool:
    """Who can talk to this hub."""
    from identity import phones
    registry = _phone_registry()
    records = registry.list()
    print()
    print("  Phones paired with this hub")
    print("  " + "-" * 60)
    if not records:
        print("  None.")
        print()
        print("  With no phone registered, the hub accepts the FIRST one to")
        print("  ask, provided it knows HOMEVOICE_API_KEY. That is what makes")
        print("  it possible to get back in with no laptop and no other")
        print("  phone. It closes the moment one phone is paired.")
        return True
    for record in records:
        print(f"  {record.get('name', '?')}")
        print(f"      id          {record.get('id', '?')}")
        print(f"      fingerprint {phones.short_fingerprint(record['public_key'])}")
        print(f"      paired      {_describe_age(record.get('paired_at', 0))}")
        print(f"      last seen   {_describe_age(record.get('last_seen', 0))}")
        # WHO is behind the handset. A phone with no person has been paired
        # but not enrolled: its keypair is trusted, and it still cannot pass
        # a voice check, so it cannot drive a secure device.
        owner = record.get("user_id") or ""
        shown = owner or "nobody yet -- enrols on its next connection"
        print(f"      person      {shown}")
    print()
    print(f"  {len(records)} phone(s). Remove one with:")
    print("      python setup_manager.py --unpair <id>")
    if registry.window.is_open():
        print()
        print(f"  Pairing is OPEN for another "
              f"{registry.window.seconds_left()}s.")
    return True


def _write_pair_qr(argv: "list[str]") -> bool:
    """Write a QR image carrying this hub's address and key: --pair-qr.

    WHY AN IMAGE AND NOT A SCREEN. A one-time token or a QR printed to the
    console both need somebody to be LOOKING at the hub, and the hub is a
    headless Pi in a cupboard (her point, 2026-08-31). So the QR is a thing
    you keep, not a thing the hub shows: generated once, saved, printed or
    left in a drawer beside the key it encodes.

    THE ADDRESS IS THE HALF THAT EARNS IT. Typing a long random string is
    tedious; not knowing which address to type is what actually broke a
    pairing (2026-08-30, "No answer from wss://192.168.4.1:8765" -- the app
    guessing down a compiled-in list). The QR carries host and port, so the
    phone stops guessing.

    IT IS THE KEY. Anyone holding this image can pair a phone exactly as
    anyone holding the written key can. Same secret, same care, no new
    threat model -- which is why it is not a token, has no expiry, and is
    not mailed anywhere.
    """
    if not os.environ.get("HOMEVOICE_API_KEY", "").strip():
        print("HOMEVOICE_API_KEY is not set in this environment, so there "
              "is no key to encode.")
        print("Set it the way the hub gets it, then run this again.")
        return False

    # THE ADDRESS THE PHONE SHOULD TRY, from NETWORK_BINDINGS rather than
    # typed here -- the same list the server binds. `--pair-qr <host>`
    # overrides it for a hub reached by some other name.
    hosts = [a for a in argv[1:] if not a.startswith("-")]
    host = hosts[0] if hosts else ""
    if not host:
        # THE ROAD THE HUB IS ON, not the first line of the list: that put
        # the hotspot's 192.168.4.1 into every code on a hub sitting on a
        # router (2026-09-09). Several roads -> the first; --pair draws all.
        roads = _pair_roads()
        host = roads[0][1] if roads else ""
        if len(roads) > 1:
            print("  This hub is reachable more than one way; this code is for "
                  f"{roads[0][0]} ({host}). `--pair` draws one per road, or "
                  "name one: --pair-qr <address>.")
    if not host:
        print("No address this machine holds is in config.NETWORK_BINDINGS.")
        return False

    out = _write_pair_png(_pair_code_payload(host=host))
    if out is None:
        return False

    print(f"Wrote {out}")
    print(f"  It carries: {host}:{config.WS_PORT}, and the hub key.")
    print("  GUARD IT LIKE THE KEY -- it IS the key. Anyone holding this")
    print("  image can pair a phone. Print it, keep it where you keep the")
    print("  key, and do not put it anywhere it can be photographed by")
    print("  somebody who should not have it.")
    # Restarting opens nothing unless HOMEVOICE_PAIR_ON_BOOT=1 (off since
    # 2026-10-15), so it is named only when it is true.
    print("  Pairing still has to be OPEN for it to work: run --pair, hold "
          "the hub's button, or Pair a new phone on a paired phone"
          + (", or restart the hub." if config.PAIR_ON_BOOT else "."))
    return True


def _pair_roads() -> List[Tuple[str, str, str]]:
    """Every way a phone can reach this hub RIGHT NOW: (binding name, the
    address or name the phone dials, a label for the person).

    ONE CODE PER ROAD. A pairing code carries a single address, and the
    hub is often on two roads at once -- the house router (or its own
    hotspot) for a phone in the building, and Tailscale for a phone
    anywhere. Until 2026-09-07 --pair drew the first address the machine
    HELD, and a tailnet name is never "held" as an address, so the code
    was always the local one and pairing over Tailscale meant knowing to
    type the tailnet name into --pair-qr. Now each road gets its own code,
    labelled, and the phone scans the one for the road it is on.

    Order is config.NETWORK_BINDINGS order: the hub's own hotspot first,
    then the router, then the tailnet. "lan" resolves through hub_net like
    everywhere else. When NO address is held (a code drawn before the
    network is up) the first concrete entry stands in, as before, so the
    printout is never empty for want of a network.
    """
    held = _local_addresses()
    roads: List[Tuple[str, str, str]] = []
    fallback: Optional[Tuple[str, str, str]] = None
    for entry in config.NETWORK_BINDINGS:
        name = str(entry.get("name") or "")
        if entry.get("host") == hub_net.LAN_SENTINEL:
            # EVERY network this machine is on, whatever its number: the
            # router's, a hotspot it runs -- the same list ws_server serves.
            for address in hub_net.local_addresses():
                roads.append((f"{name} {address}", address,
                              f"on the local network at {address}"))
            continue
        resolved = hub_net.resolve_host(str(entry.get("host") or ""))
        if not resolved:
            continue
        if not _is_ip(resolved):
            # A NAME, not an address: the tailnet, reachable whenever
            # Tailscale is up on this machine (config._tailnet_name asks
            # `tailscale status`). A pairing road since 2026-09-08: the
            # enrolment check in ws_server accepts the HMAC over the
            # certificate the tailnet listener actually serves (see
            # ws_server.served_spki_fingerprint), so a phone signed in to
            # the tailnet can pair from anywhere, not only on the local
            # Wi-Fi. The hub must be running THAT ws_server for this code
            # to work; an older one refuses it with bad_hmac.
            roads.append((name, resolved,
                          "over Tailscale -- from anywhere, phone signed in "
                          "to the same tailnet"))
            continue
        if fallback is None and entry.get("host") != hub_net.LAN_SENTINEL:
            fallback = (name, resolved,
                        "first address in NETWORK_BINDINGS (this hub is on "
                        "no network yet)")
        if resolved in held or hub_net.holds_address(resolved):
            roads.append((name, resolved, f"at {resolved}"))
    if not any(_is_ip(r[1]) for r in roads) and fallback:
        roads.insert(0, fallback)
    return roads


def _reachable_road(from_away: bool = False) -> Optional[Tuple[str, str, str]]:
    """A road a NEW phone can come in on RIGHT NOW, or None -- the question
    a pairing window waits on (PairingWindow.ask). Unlike the code's fallback, never
    the stand-in "first address in the list" when the machine holds none:
    an address this machine really holds (hotspot on, Wi-Fi joined). The
    tailnet counts only for a window opened for a phone away from home --
    every other window refuses a new phone over Tailscale (ws_server)."""
    held = _local_addresses()
    tailnet = None
    for road in _pair_roads():
        host = road[1]
        if _is_ip(host):
            if host in held or hub_net.holds_address(host):
                return road
        elif from_away and tailnet is None:
            tailnet = road
    return tailnet


def _is_ip(text: str) -> bool:
    try:
        ipaddress.ip_address(text)
        return True
    except ValueError:
        return False


def _pair_code_payload(until: Optional[float] = None,
                       host: Optional[str] = None) -> Optional[str]:
    """The `homevoice://pair?...` string, or None when there is no key or
    no address to put in it. `until` (epoch seconds) is added when known,
    so the phone can count the window down from the scan. `host` picks
    the road; by default the first one _pair_roads() lists."""
    from urllib.parse import quote
    key = os.environ.get("HOMEVOICE_API_KEY", "").strip()
    if not key:
        return None
    if not host:
        roads = _pair_roads()
        host = roads[0][1] if roads else ""
    if not host:
        return None
    payload = (f"homevoice://pair?host={quote(host)}&port={config.WS_PORT}"
               f"&key={quote(key)}")
    if until:
        payload += f"&until={int(until)}"
    return payload


def _pair_png_path(road: str = "") -> str:
    """pair.png for the first road, pair-<road>.png for the others, so two
    codes drawn in one run do not overwrite each other."""
    road = re.sub(r"[^A-Za-z0-9.-]+", "-", road).strip("-")   # "lan 10.0.0.4" -> lan-10.0.0.4
    stem = f"pair-{road}.png" if road else "pair.png"
    return os.path.join(os.path.dirname(config.TLS_CERT), stem)


def _write_pair_png(payload: str, road: str = "") -> Optional[str]:
    """pair.png beside the certificate, owner-only (this image IS the key).
    Returns the path, or None with the reason printed."""
    try:
        import qrcode                                    # type: ignore
    except ImportError:
        print("  (pip install qrcode  to have the code drawn.)")
        return None
    out = _pair_png_path(road)
    try:
        os.makedirs(os.path.dirname(out) or ".", exist_ok=True)
        with _open_private(out, "wb") as f:
            if os.name != "nt":
                # The mode above applies only to a NEW file; one left by an
                # older run keeps whatever it had.
                os.fchmod(f.fileno(), 0o600)
            # box_size 10 / border 4: a crisp, generously quiet-zoned image
            # that a phone decodes off a monitor at arm's length.
            qr = qrcode.QRCode(box_size=10, border=4)
            qr.add_data(payload)
            qr.make(fit=True)
            qr.make_image().save(f, format="PNG")
    except Exception as exc:                             # noqa: BLE001
        print(f"  Could not write {out}: {exc}")
        return None
    return out


def _open_image(path: str) -> bool:
    """Show an image in whatever this machine uses. False on a headless
    box, which is not an error -- the terminal drawing is still there."""
    try:
        if os.name == "nt":
            os.startfile(path)                           # type: ignore[attr-defined]
            return True
        if os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"):
            subprocess.Popen(["xdg-open", path],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return True
    except Exception:                                    # noqa: BLE001
        pass
    return False


def _gpio_leds_fitted() -> bool:
    """Is this hub the kind with the header LEDs? Only a Raspberry Pi with
    gpiozero and a second LED pin configured, which is exactly when the
    sentence about the light is true. Never guessed on a PC."""
    try:
        from hub import hub_gpio                         # type: ignore
        if not hub_gpio.wanted():
            return False
        import gpiozero                                  # noqa: F401
    except Exception:                                    # noqa: BLE001
        return False
    # config already folds in HOMEVOICE_PAIR_LED_GPIO (and survives a typo
    # in it, which re-reading the variable here did not).
    return config.GPIO_PAIR_LED_PIN > 0


def _print_pair_code(until: Optional[float] = None,
                     only: Optional[str] = None) -> None:
    """Show the pairing code -- ONE PER ROAD the hub is on (see _pair_roads):
    a PNG opened on screen where there is one (a phone decodes that
    reliably), the same code drawn in the terminal as a fallback, and the
    two values typed by hand as the last resort. `only` narrows it to one
    road, by binding name ("tailnet", "lan") or by address.

    THE TERMINAL DRAWING ALONE WAS NOT ENOUGH (field report 2026-09-03:
    "the camera is not decoding the QR"). A console renders the half-block
    glyphs with line-spacing gaps, a light theme inverts the polarity, and
    the payload is long enough to make a dense code -- three separate ways
    for a decoder to see nothing. The PNG has none of them.
    """
    if not os.environ.get("HOMEVOICE_API_KEY", "").strip():
        print("  (No code to show: HOMEVOICE_API_KEY is not set in this")
        print("   environment.)")
        print()
        return
    roads = _pair_roads()
    if only:
        want = only.strip().lower()
        roads = [r for r in roads if want in (r[0].lower(), r[1].lower())
                 or r[0].lower().startswith(want + " ")]
        if not roads:
            print(f"  No road called {only!r}. The hub is reachable:")
            for name, host, label in _pair_roads():
                print(f"      {name:8} {host}  ({label})")
            print()
            return
    if not roads:
        print("  (No code to show: NETWORK_BINDINGS has no address this")
        print("   machine holds, and no tailnet.)")
        print()
        return
    if len(roads) > 1:
        print(f"  This hub is reachable {len(roads)} ways. Scan the code for "
              f"the way the PHONE is connected right now:")
        for name, host, label in roads:
            print(f"      {name:8} {host}  -- {label}")
        print()
    for index, (name, host, label) in enumerate(roads):
        payload = _pair_code_payload(until, host=host)
        if len(roads) > 1:
            print(f"  ── {name}: {label} ──")
            print()
        png = _write_pair_png(payload, road=name if index else "")
        if png:
            if _open_image(png):
                print(f"  The code is open on screen ({png}) -- scan THAT one;")
                print("  it decodes far more reliably than the drawing below.")
            else:
                print(f"  Wrote {png} -- open it on any screen and scan it.")
            print()
        try:
            import qrcode                                    # type: ignore
            qr = qrcode.QRCode(border=2)
            qr.add_data(payload)
            qr.make(fit=True)
            # invert=True draws dark modules as spaces on a light block
            # background, which a DARK terminal renders in the right
            # polarity; on a light theme the PNG above is the one to use.
            qr.print_ascii(invert=True)
        except ImportError:
            print("  (pip install qrcode  to have the code drawn here.)")
        except Exception as exc:                             # noqa: BLE001
            print(f"  (could not draw the code: {exc})")
        print()
        # BY HAND, when no camera will cooperate: the pair screen keeps its
        # two fields, and these are the two values that go in them. Same
        # console, same trust as the code itself.
        print("  Or type it into the app's Pair screen:")
        print(f"      address:  {host}:{config.WS_PORT}")
        print(f"      key:      {os.environ['HOMEVOICE_API_KEY'].strip()}")
        print()
    # WHAT THE HEADER IS DOING, for a hub with the LEDs fitted. Said here
    # because this is the console the person is looking at when they want
    # to know whether the phone got through -- and on a Pi under a shelf,
    # the light is the answer they can see from where they are standing.
    if _gpio_leds_fitted():
        print("  On the hub's header: the phone LED blinks slowly while this")
        print("  window is open, flashes three times when a phone pairs, and")
        print("  stays on for as long as a phone is connected.")
        print()


def _open_pairing(argv: Optional["list[str]"] = None) -> bool:
    """Let ONE new phone register, for a couple of minutes.

    `--pair` alone draws one code per road the hub is on; `--pair tailnet`
    (or `--pair lan`, or an address) draws just that one.

    Knowing the API key is deliberately not enough on its own. If it were,
    removing a phone would achieve nothing -- the removed phone still knows
    the key and would simply pair itself again. Enrollment needs this window
    too, and opening it takes access to the hub.
    """
    registry = _phone_registry()
    # NO ADDRESS, NO COUNTDOWN (her rule, 2026-10-19). With the hotspot off
    # and no Wi-Fi, no phone can reach this hub, and a window opened now
    # would run down for nothing. The ask is left with the hub (ws_server
    # opens it the moment an address appears, even if this console is
    # closed), and this console waits for the same moment to draw the code.
    from identity import phones as _phones_mod
    waited = False
    if _reachable_road() is None:
        waited = True
        lapses = registry.window.ask()
        print()
        print("  This hub is on no network yet (hotspot off, no Wi-Fi), so no")
        print("  phone could reach it. Pairing opens -- the full "
              f"{_phones_mod.window_words()} -- as soon as it")
        print("  is on one. Waiting here (Ctrl+C to stop waiting; the hub still")
        print(f"  opens it by itself until "
              f"{time.strftime('%H:%M', time.localtime(lapses))}).")
        try:
            while _reachable_road() is None:
                if registry.window.pending() is None and not registry.window.is_open():
                    print("  Nothing came up in time -- pairing was not opened.")
                    return False
                time.sleep(2)
        except KeyboardInterrupt:
            print()
            print("  Stopped waiting here. The hub opens pairing by itself when")
            print("  it gets an address; the codes go in certs/.")
            return True
        print("  On a network now.")
    if waited and registry.window.is_open():
        until = registry.window._open_until()     # the hub opened it first
    else:
        until = registry.window.open()
    seconds = int(until - time.time())
    print()
    print(f"  Pairing is open for {seconds} seconds.")
    print()
    # THE CODE, RIGHT HERE, WITH THE CLOCK IN IT. The app pairs by camera
    # (2026-09-02), so the moment the window opens is the moment the code
    # should be on screen -- carrying `until`, so the phone shows the same
    # countdown this console does. Same payload as --pair-qr plus the
    # expiry; a stale printout reads as "closed" on the phone, never as
    # open. Printed to the console only: the console is the hub's own
    # keyboard, which is exactly the trust --pair already assumes.
    only = next((a for a in (argv or [])[1:] if not a.startswith("-")), None)
    _print_pair_code(until, only=only)
    print("  On the phone: be on one of the roads above (the house Wi-Fi,")
    print("  or Tailscale signed in), open the app, tap Pair and point the")
    print("  camera at the code for THAT road. The phone generates")
    print("  its own key, registers the public half, and receives this")
    print("  hub's certificate -- nothing to copy, nothing to rebuild.")
    print()
    print("  The window closes as soon as one phone pairs.")
    if registry.count() == 0:
        print()
        print("  (No phone is registered yet, so the first one could have")
        print("   paired without this. Opening it anyway does no harm.)")
    return True


def _unpair(argv) -> bool:
    """Revoke one phone. Takes effect on its next connection, no restart."""
    registry = _phone_registry()
    wanted = [a for a in argv[1:] if not a.startswith("--")]
    if not wanted:
        print("  Which phone? Run --phones for the list, then:")
        print("      python setup_manager.py --unpair <id>")
        return False

    target = wanted[0]
    # Accept a leading fragment of the id, because nobody wants to type
    # sixteen hex characters off a screen to delete something.
    matches = [r for r in registry.list()
               if r.get("id", "").startswith(target)]
    if not matches:
        print(f"  No phone whose id starts with {target!r}.")
        print("  Run --phones to see what is registered.")
        return False
    if len(matches) > 1:
        print(f"  {target!r} matches {len(matches)} phones. Be more specific:")
        for record in matches:
            print(f"      {record['id']}  {record.get('name', '?')}")
        return False

    record = matches[0]
    if registry.remove(record["id"]):
        print(f"  Removed '{record.get('name', '?')}' ({record['id']}).")
        print("  It will be refused on its next connection. Nothing else")
        print("  changes -- every other phone keeps its own key.")
        if registry.count() == 0:
            print()
            print("  THAT WAS THE LAST PHONE. The hub will now accept the")
            print("  next one that asks and knows HOMEVOICE_API_KEY, without")
            print("  a pairing window. That is the recovery path, and it is")
            print("  open right now.")
        return True
    print("  Nothing was removed.")
    return False


def _doctor() -> bool:
    """Check everything a board needs, in the order a board needs it."""
    from esp_firmware.esp_builder import EspBuilder

    problems: list = []
    def bad(line: str) -> None:
        problems.append(line)
        print(f"    [X] {line}")

    def good(line: str) -> None:
        print(f"    [ok] {line}")

    # The rung BELOW every other check: on a bare machine (a fresh Pi),
    # a missing broker or HA reads as twenty downstream failures. Name
    # the real state instead, with the exact command for this OS.
    print("\n=== 0. Services ===")
    # WHERE THE KEYS COME FROM, first, because a hub and a --pair code that
    # read different keys look like every other failure in this list.
    loaded, secrets_path = config.SECRETS_LOADED, config.SECRETS_FILE
    if loaded:
        good(f"keys read from {secrets_path}: {', '.join(loaded)} (the file wins over the shell)")
    elif secrets_path:
        print(f"    [--] no {secrets_path}: keys come from this shell only. One file for every\n"
              f"         process, and the hub at boot:  python3 setup_manager.py --service")
    if sys.platform.startswith("linux") and shutil.which("systemctl"):
        if _run(["systemctl", "is-enabled", _SERVICE_NAME])[1] == "enabled":
            active, state = _run(["systemctl", "is-active", _SERVICE_NAME])
            (good if active else bad)(
                f"{_SERVICE_NAME}.service is enabled at boot and {state}"
                + ("" if active else "  (journalctl -u homevoice -n 50)"))
        else:
            print("    [--] the hub does not start at boot yet:  python3 setup_manager.py --service")
    # find_mosquitto_tool, not which(): Debian puts the broker in /usr/sbin,
    # off an ordinary user's PATH, and Windows off PATH altogether.
    if find_mosquitto_tool("mosquitto"):
        good("mosquitto is installed")
    else:
        bad("mosquitto is not installed.\n"
            + ("        sudo apt install -y mosquitto"
               if os.name != "nt" else
               "        install it from mosquitto.org/download"))
    # ha_is_up, not `requests`: that package is optional, and without it
    # this said "not answering" about a Home Assistant that was.
    if ha_is_up(timeout=3.0):
        good(f"Home Assistant answers at {config.HA_BASE_URL}")
    else:
        bad("Home Assistant is not answering at "
            f"{config.HA_BASE_URL}.\n"
            "        Pi: docker run -d --name homeassistant --restart="
            "unless-stopped \\\n"
            "          --network=host ghcr.io/home-assistant/"
            "home-assistant:stable\n"
            "        then put a long-lived token in HOMEVOICE_HA_TOKEN")

    # Import probes, if the deps module is present: assets are checked
    # below; this says whether the PACKAGES around them import at all.
    try:
        from core import dependency_flags as _deps
        _deps.print_status()
    except ImportError:
        pass

    print("\n=== 1. Tools ===")
    # (mosquitto_passwd is not among them: passwords are hashed in-process,
    # see write_passwd_entry, and the broker itself is checked above.)
    for pkg in PACKAGE_ASSETS:
        if pkg["import_name"] in ("serial", "paho", "esptool"):
            if _asset_present(pkg) or _check_import(pkg["import_name"]):
                good(f"{pkg['pip_name']} installed")
            else:
                bad(f"{pkg['pip_name']} is missing -- pip install {pkg['pip_name']}")

    print("\n=== 2. Firmware ===")
    config.settle_firmware_folder(say=print)
    firmware = config.TASMOTA_FIRMWARE_PATH
    if os.path.isfile(firmware):
        good(f"{os.path.basename(firmware)} "
             f"({os.path.getsize(firmware) // 1024}KB)")
    else:
        bad(f"the Tasmota image is missing: {firmware}\n"
            f"         run this setup without --doctor to download it")
    # The other families: which can be flashed from this hub today. Not a
    # fault when absent -- only a board chosen as that family needs one.
    have, lack = [], []
    for fam in _chips.FAMILIES.values():
        if fam.family == "esp32":
            continue
        (have if os.path.isfile(config.firmware_path_for(fam))
         else lack).append(fam.family_label)
    if have:
        good(f"other families with an image: {', '.join(have)}")
    if lack:
        print(f"    [--] no image yet for: {', '.join(lack)} (run this setup "
              f"without --doctor to download Tasmota's stock one)")

    print("\n=== 3. Certificates ===")
    # TWO OF THEM, and only one has to satisfy an ESP32. Kept apart so that
    # correcting the boards' certificate never disturbs a phone.
    phone_cert = config.LOCAL_TLS_CERT
    if os.path.isfile(phone_cert):
        good(f"phone: {phone_cert}")
    else:
        bad(f"no certificate at {phone_cert} -- the app cannot verify this "
            f"hub, so it will refuse to connect")

    board_cert = config.BROKER_TLS_CERT
    if not config.MQTT_TLS:
        good("boards: TLS is off, so no broker certificate is needed")
    elif os.path.isfile(board_cert):
        good(f"boards: {board_cert}")
        for ok_line, message in _inspect_certificate(board_cert):
            (good if ok_line else bad)(message)
    else:
        bad(f"no broker certificate at {board_cert} -- run: "
            f"python setup_manager.py --broker")

    print("\n=== 4. The network the boards join ===")
    wifi = _boards_wifi()
    if wifi.get("ssid"):
        good(f"boards join {wifi['ssid']!r}")
    else:
        bad("no Wi-Fi saved yet -- set up a board once and it is asked")
    address = wifi.get("hub_address") or config.MQTT_HOST
    if not address or address.startswith(("127.", "100.")):
        bad("no usable hub address. It is this machine's address on the "
            "network the boards join (set up a board, or run Wi-Fi every "
            "board joins, and it is asked)")
    else:
        good(f"boards look for this hub at {address}")
        # IS THAT ACTUALLY ONE OF THIS MACHINE'S ADDRESSES?
        #
        # The check that was missing. A board was once told to join the
        # laptop's own hotspot and then look for the hub at the laptop's
        # address on the PHONE's hotspot -- two different networks, so the
        # board would have joined perfectly and reached nothing. Both lines
        # read fine on their own; only together are they wrong.
        mine = _local_addresses()
        if mine and address not in mine and not hub_net.holds_address(address):
            bad(f"{address} is not one of this machine's addresses right "
                f"now ({', '.join(sorted(mine))}). Boards would join "
                f"{wifi.get('ssid', 'the Wi-Fi')!r} and find nothing there. "
                f"Use this machine's address on that network (Devices -> "
                f"Wi-Fi every board joins).")

    print("\n=== 5. Broker ===")
    if os.path.isfile(config.MQTT_CONF_FILE):
        good(f"config at {config.MQTT_CONF_FILE}")
    else:
        bad("no broker config -- run: python setup_manager.py --broker")
    if _acl_in_conf():
        good(f"topic ACL at {_acl_file()} -- boards see only their own "
             f"topics")
    else:
        bad(f"no topic ACL in force ({_acl_file()} missing, or not named "
            f"by mosquitto.conf) -- any board could command every other "
            f"board. Run: python setup_manager.py --broker")
    accounts = _broker_accounts()
    if accounts:
        good(f"{len(accounts)} account(s): {', '.join(accounts)}")
    else:
        bad("no broker accounts. Nothing can sign in, including Home "
            "Assistant")
    if _hub_user() not in accounts:
        bad(f"{_hub_user()!r} (the hub's own account) is not in the password "
            f"file -- the hub cannot talk to any board. Run: python "
            f"setup_manager.py --broker")
    if config.MQTT_HA_USER not in accounts:
        bad(f"{config.MQTT_HA_USER!r} has no account -- so Home Assistant "
            f"cannot sign in, and will ask you to re-authenticate for ever. "
            f"Run: python setup_manager.py --broker")
    else:
        # SAID OUT LOUD because "why does it keep changing?" was a fair
        # question with a bad answer. --broker used to mint a new password on
        # every run; it no longer touches an account that exists. The only
        # things that change it now are --reset-ha-password, and the password
        # file being deleted.
        good(f"{config.MQTT_HA_USER!r} has an account. Nothing changes its "
             f"password except --reset-ha-password")

    # THE SAME TWO PORTS THE CONFIG WAS WRITTEN WITH. This read MQTT_PORT
    # directly, which was a third copy of the rule and the one that
    # disagreed: with TLS off MQTT_PORT drops to 1883, so the doctor probed
    # 1883 twice, announced it as "the boards" -- and never checked the
    # boards' actual port at all. _broker_ports() is what _setup_broker and
    # _verify_broker both use, so there is now one answer.
    tls_port, plain_port = _broker_ports()
    for port in dict.fromkeys((plain_port, tls_port)):   # one port, TLS off
        # COUNTED, NOT JUST PROBED. A connection test answers "is a broker
        # there", which is not the question that has cost the most time here.
        # TWO brokers on one port is the fault that hides itself: Home
        # Assistant signs in to one, the boards to the other, each log looks
        # healthy, and the accounts appear to keep breaking for no reason.
        count, what = _listeners(port)
        who = ("the boards" if port != plain_port else "Home Assistant"
               + ("" if tls_port != plain_port else " and the boards"))
        (good if count == 1 else bad)(
            f"{what} ({who})" + (". IS MOSQUITTO RUNNING? That is the single "
                                 "most common answer here" if not count else ""))

    print("\n=== 6. Boards ===")
    # The hub's own list (EspBuilder._boards: a board is a record that
    # names itself), not a glob with a list of files that are not boards --
    # listing firmware_drivers.json as a nameless board that "will be
    # refused" sent people looking for a phantom.
    boards = EspBuilder.headless()._boards()
    if not boards:
        print("    (none set up yet -- nothing to check)")
    for slug, record in boards:
        name = record.get("name") or slug
        if record.get("mqtt_user") in accounts:
            good(f"{name}: has a broker account")
        else:
            bad(f"{name} ({slug}.json): its account is not in the "
                f"password file -- it will be refused. Re-save the board, or run "
                f"--broker; a leftover record with no name can simply be deleted")
        if not record.get("flashed"):
            print(f"    [--] {name}: described but never flashed")
            continue
        # IS IT ACTUALLY THERE? The two checks above are about paperwork.
        # The broker keeps each board's last will (tele/<topic>/LWT, retained:
        # Online while it is connected, Offline when it dropped), so a
        # subscribe with the hub's own account answers in under a second.
        # Nothing is sent to the board.
        state = _board_presence(record)
        if state == "Online":
            good(f"{name}: CONNECTED to the broker (LWT Online)")
        elif state == "Offline":
            bad(f"{name}: NOT connected -- the broker last saw it drop "
                f"(LWT Offline). Wrong Wi-Fi/password/hub address, or off. "
                f"Its web console says why: MQT: ... ")
        elif state == "silent":
            bad(f"{name}: NOT connected -- the broker has never heard from it "
                f"(no LWT). It has not reached the broker since it was "
                f"provisioned: check the Wi-Fi name, password and hub address "
                f"it was given")
        else:
            print(f"    [--] {name}: could not ask the broker ({state})")

    print("\n=== 7. Home Assistant ===")
    # (Whether it answers at all is section 0's question, asked once.)
    if not os.environ.get("HOMEVOICE_HA_TOKEN"):
        bad("HOMEVOICE_HA_TOKEN is not set -- the hub cannot talk to Home "
            "Assistant at all")
    else:
        good("HOMEVOICE_HA_TOKEN is set")

    print("\n=== 7b. Registered devices ===")
    # THE ORPHAN CHECK. A device registered here points at an HA entity by
    # id, and HA ids are not forever: rename the device in HA, retire its
    # board, or let HA regenerate ids, and the registration silently points
    # at nothing. The failure then surfaces at the worst moment -- a voice
    # command answered with a stale state or a shrug -- with a log line that
    # used to blame the network. Checked HERE, where it costs one request
    # and names the exact record to fix.
    try:
        with open(config.DEVICES_JSON_PATH, encoding="utf-8") as f:
            registered = json.load(f)
    except Exception:                                  # noqa: BLE001
        registered = {}
    linked = {did: d for did, d in (registered if isinstance(registered, dict)
                                    else {}).items()
              if isinstance(d, dict) and d.get("ha_entity_id")}
    if not linked:
        print("    (no registered devices point at Home Assistant -- "
              "nothing to check)")
    elif not os.environ.get("HOMEVOICE_HA_TOKEN"):
        print("    (HOMEVOICE_HA_TOKEN not set -- cannot compare against "
              "HA's live entities)")
    else:
        try:
            from home_assistant.ha_client import HAClient
            ha = HAClient()
            try:
                live = {e.get("entity_id") for e in (ha.get_states() or [])}
            finally:
                ha.close()
        except Exception as exc:                       # noqa: BLE001
            live = set()
            print(f"    (could not fetch HA's entities: {exc})")
        if live:
            orphans = [(did, d["ha_entity_id"]) for did, d in linked.items()
                       if d["ha_entity_id"] not in live]
            if not orphans:
                good(f"all {len(linked)} registered device(s) point at "
                     f"entities HA still has")
            for did, entity in orphans:
                bad(f"{did!r} points at {entity}, which no longer exists "
                    f"in HA. Voice commands to it will use stale state or "
                    f"fail. Fix: REMOVE DEVICE -> one device -> {did!r}, "
                    f"then Add a device and pick its current entity.")

    print("\n=== 8. Serial ===")
    try:
        from serial.tools import list_ports
        ports = list(list_ports.comports())
    except Exception:
        ports = []
    if ports:
        for p in ports:
            good(f"{p.device} -- {p.description}")
    else:
        print("    (no serial port -- only needed while flashing a board)")
    if os.name != "nt":
        import grp
        import getpass
        try:
            in_dialout = getpass.getuser() in grp.getgrnam("dialout").gr_mem
        except (KeyError, OSError):
            in_dialout = True      # no such group; not a Debian-style system
        if not in_dialout:
            bad("you are not in the 'dialout' group, so /dev/ttyUSB* is not "
                "readable. sudo usermod -aG dialout $USER, then log out and "
                "back in")

    print()
    if problems:
        print(f"{len(problems)} thing(s) to fix:")
        for line in problems:
            print(f"  - {line.splitlines()[0]}")
        return False
    print("Everything a board needs is in place.")
    return True


# ===========================================================================
# The message broker
# ===========================================================================
#
# Mosquitto sits between the boards and Home Assistant. Everything below
# writes its configuration; it does not install it, because that is a package
# manager's job and differs per platform.
#
# TWO LISTENERS, and the split is the whole security design:
#
#   8883, on every interface, TLS, password required.  <- the boards
#   1883, on 127.0.0.1 ONLY, password required.        <- Home Assistant
#
# The plaintext one exists because Home Assistant runs on this same machine
# and would otherwise need to be taught to trust a self-signed certificate --
# a fiddly, silently-breakable thing. Bound to the loopback interface it
# never reaches a network card, so its traffic cannot be observed by anything
# that is not already running on this host. If HA ever moves to another
# machine, that listener must go and HA must be given the certificate.
#
# The boards get the TLS one, because they ARE on the network, and they
# verify it by fingerprint rather than by name -- see esp_builder.

MOSQUITTO_CONF = """# Written by HomeVoice setup_manager. Edits will be overwritten.
#
# Regenerate with:  python setup_manager.py --broker

allow_anonymous false
password_file {passwd}
# WHO MAY SPEAK ON WHICH TOPIC -- see write_acl_file. Without it every
# authenticated board could command every other board.
acl_file {acl}
{tls_listener}
# Everything that is not using TLS. With MQTT_TLS off, that is the boards
# too, which is why the bind address matters -- see MQTT_PLAIN_BIND.
{plain_listener}

persistence true
# Ends with a separator already -- see the directory= argument below. Adding
# one here as well produced "broker/\\mosquitto.db" on Windows: harmless, but
# it appears in every log line and looks exactly like a broken path.
persistence_location {directory}
log_dest stdout
"""

#: The encrypted listener, written ONLY when TLS is actually on.
#:
#: It used to be written unconditionally, so turning TLS off left a port
#: open that nothing connected to and a certificate loaded for nothing --
#: and, more to the point, `netstat` showed 8883 listening while every board
#: was talking on 1883, which reads as "TLS is working" to anyone glancing
#: at it. A port that is open should be a port that is used.
TLS_LISTENER = """
# Boards. TLS, on the network.
listener {tls_port}
certfile {cert}
keyfile {key}
# NO cafile HERE, and its absence is deliberate.
#
# It used to be set to the SAME FILE as certfile, which looks harmless and
# is not. cafile makes Mosquitto send that certificate as a chain along
# with the leaf, so a self-signed certificate went out TWICE -- once as
# itself and once as its own issuer. An ESP32 reports that as
# `TLS connection error: 1` after a handshake that visibly runs for three
# seconds and then fails, which reads like a certificate fault and is not.
# Two RSA-2048 certificates is roughly 2.5KB against a board whose
# MAX_PACKET_SIZE is 1200.
#
# cafile exists to verify CLIENT certificates. require_certificate is
# false, so there are none to verify and nothing needs it.
require_certificate false
"""


#: Topic authorization. Username == Tasmota topic == hv-<slug> for every
#: board, so one static file covers every board that will ever exist.
#:
#: `pattern` LINES, NOT GENERAL `topic` LINES, for the discovery rule: a
#: `topic` line written before any `user` section applies to ANONYMOUS
#: clients only (checked against mosquitto 2.0.18: hv-a's discovery publish
#: was dropped with `topic`, delivered with `pattern`). A `pattern` line
#: applies to every client, with or without a `user` section present, and
#: %u is the client's username. A board therefore reaches exactly
#: cmnd/stat/tele under its own name plus write-only Tasmota discovery
#: (tasmota/discovery/<MAC>/config and /sensors, retained -- what Home
#: Assistant's Tasmota integration needs); the group topic cmnd/tasmotas/#
#: and every other board's topics are unreachable. Home Assistant and the
#: hub's own account see everything.
ACL_FILE = """# Written by HomeVoice setup_manager. Edits will be overwritten.
# Regenerate with:  python setup_manager.py --broker
#
# Every authenticated client: its own topics, and discovery (write only).
pattern write tasmota/discovery/#
pattern readwrite cmnd/%u/#
pattern readwrite stat/%u/#
pattern readwrite tele/%u/#

# Home Assistant sees and drives every board.
user {ha_user}
topic readwrite #

# The hub's own account, used by every hub-side MQTT client.
user {hub_user}
topic readwrite #
"""


def _acl_file() -> str:
    return config.MQTT_ACL_FILE


def _hub_user() -> str:
    return config.MQTT_HUB_USER


def write_acl_file() -> str:
    """Write the ACL file beside mosquitto.conf and return its path. Static
    content, so writing it again is free; the running broker re-reads it
    on the same reload a password change triggers."""
    path = _acl_file()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    text = ACL_FILE.format(ha_user=config.MQTT_HA_USER, hub_user=_hub_user())
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text)
    os.replace(tmp, path)
    return path


def _conf_value(path: str, key: str) -> Optional[str]:
    """`key`'s value in a Mosquitto config file, or None if it is not set."""
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                # split(None, 1): the value is the REST of the line, as
                # Mosquitto reads it. A plain split() cut "/home/pi/Voice
                # AI/configs/acl" at the space, so this was never true in the
                # project's own folder -- the doctor always said "no ACL" and
                # --autostart always rolled itself back.
                parts = [p.strip() for p in line.split(None, 1)]
                if len(parts) == 2 and parts[0] == key:
                    return parts[1]
    except OSError:
        pass
    return None


def _acl_in_conf() -> bool:
    """Is the ACL file written, and does mosquitto.conf point at it?"""
    return (os.path.isfile(_acl_file())
            and _conf_value(config.MQTT_CONF_FILE, "acl_file") == _acl_file())


def _hub_account_path() -> str:
    return os.path.join(config.BROKER_DIR, "hub_account.json")


def ensure_hub_account(repair: bool = False) -> Tuple[str, str]:
    """The hub's OWN broker account -- (user, password), created on first
    use. The password lives 0600 in BROKER_DIR/hub_account.json and its
    hash in the password file; with topic ACLs a borrowed board account
    would see only that board, so every hub-side client uses this one.
    `repair=True` re-writes the password-file entry from the stored
    password (a refused login whose entry went missing or was mangled).
    Idempotent; never touches any other account."""
    from core.locked_store import atomic_write_json
    path = _hub_account_path()
    user = _hub_user()
    password = ""
    try:
        with open(path, encoding="utf-8") as f:
            stored = json.load(f)
        if isinstance(stored, dict) and stored.get("user") == user \
                and stored.get("password"):
            password = str(stored["password"])
    except (OSError, ValueError):
        pass
    fresh = not password
    if fresh:
        password = secrets.token_urlsafe(32)
        # Unique temp, fsynced, 0600 from creation (was a fixed ".tmp").
        atomic_write_json(path, {"user": user, "password": password})
        _lock_down_secret(path)
    passwd = config.MQTT_PASSWD_FILE
    if fresh or repair or user not in _broker_accounts():
        write_passwd_entry(passwd, user, password)
        reload_accounts()
    return user, password


def find_mosquitto_tool(name: str) -> Optional[str]:
    """A Mosquitto tool, on PATH or where its installer puts it.

    shutil.which() only searches PATH, and the Windows installer does not
    add itself to it -- so "not installed" was being reported about a
    program sitting in an entirely predictable folder. PATH still wins when
    it has an answer.
    """
    found = shutil.which(name)
    if found:
        return found
    if os.name != "nt":
        # Debian and Raspberry Pi OS install the broker in /usr/sbin, which
        # is not on an ordinary user's PATH: --start over SSH said "not
        # installed" about a broker that was.
        return shutil.which(name, path="/usr/local/sbin:/usr/sbin:/sbin")
    for base in (os.environ.get("ProgramFiles", r"C:\Program Files"),
                 os.environ.get("ProgramFiles(x86)",
                                r"C:\Program Files (x86)")):
        candidate = os.path.join(base, "mosquitto", f"{name}.exe")
        if os.path.isfile(candidate):
            return candidate
    return None


def _plain_listener() -> str:
    """The plaintext listener line, and a comment saying what it costs.

    Written into the config rather than decided here, so that reading the
    broker's own file tells you the truth about its exposure without going
    back to Python.
    """
    bind = config.MQTT_PLAIN_BIND
    if bind:
        if not config.MQTT_TLS:
            # A CONTRADICTION WORTH WRITING DOWN RATHER THAN HIDING. With TLS
            # off the boards use THIS listener, and a loopback bind means
            # they cannot reach it -- every board would sit connecting to
            # nothing while the config looked entirely deliberate.
            return (f"# NOTE: TLS is off, so the boards need this listener "
                    f"too -- but\n# MQTT_PLAIN_BIND is {bind!r}, which they "
                    f"cannot reach. Set it to \"\"\n# or turn TLS back on.\n"
                    f"listener 1883 {bind}")
        return (f"# Loopback only: unreachable from anywhere but this "
                f"machine.\nlistener 1883 {bind}")
    return ("# Reachable from the whole network, and NOT encrypted.\n"
            "# With TLS off this is the only listener, so it has to be. What\n"
            "# that rests on: the boards' network is WPA2, and on the Pi it\n"
            "# has no route to the internet at all.\n"
            "listener 1883")


def _board_presence(record: dict, timeout: float = 2.5) -> str:
    """'Online', 'Offline', 'silent' (no retained LWT at all), or a reason
    the question could not be asked. Reads the broker's RETAINED last-will
    for the board's topic with the hub's own account; sends nothing."""
    topic = record.get("topic")
    if not topic:
        return "no topic in the record"
    try:
        # Availability only: the client itself comes from EspBuilder.
        # find_spec raises (not returns None) when `paho` itself is absent.
        if importlib.util.find_spec("paho.mqtt.client") is None:
            raise ImportError("No module named 'paho.mqtt.client'")
        from esp_firmware.esp_builder import EspBuilder
    except Exception as exc:                           # noqa: BLE001
        return f"paho-mqtt or esp_builder unavailable: {exc}"
    try:
        builder = EspBuilder.headless()
        settings = builder._mqtt_settings()
        client = builder._mqtt_client()
    except Exception as exc:                           # noqa: BLE001
        return f"could not build a broker client: {exc}"
    seen: list = []
    def on_connect(c, _u, _flags, rc, *_rest):
        if rc == 0:
            c.subscribe(f"tele/{topic}/LWT")
        else:
            seen.append(f"refused by the broker (rc={rc})")
    def on_message(_c, _u, message):
        seen.append(message.payload.decode("utf-8", "replace").strip())
    client.on_connect = on_connect
    client.on_message = on_message
    try:
        client.connect(settings["host"], int(settings["port"]), 5)
        deadline = time.time() + timeout
        while not seen and time.time() < deadline:
            client.loop(timeout=0.2)
    except Exception as exc:                           # noqa: BLE001
        return f"broker unreachable at {settings.get('host')}:{settings.get('port')} ({exc})"
    finally:
        try:
            client.disconnect()
        except Exception:                              # noqa: BLE001
            pass
    if not seen:
        return "silent"
    word = seen[0]
    if word.lower().startswith("online"):
        return "Online"
    if word.lower().startswith("offline"):
        return "Offline"
    return word


def _broker_accounts() -> list:
    """Every username in the broker's password file, or [] if there is none."""
    try:
        with open(config.MQTT_PASSWD_FILE, encoding="utf-8") as f:
            return [line.split(":", 1)[0] for line in f if ":" in line]
    except OSError:
        return []


def _print_broker_start_hint() -> None:
    """How to start the broker with the config we just wrote.

    --start, not a bare `mosquitto -c ...` line: a broker started by hand
    leaves no pid file, so the hub then calls it a stranger -- refuses to
    start its own, and cannot reload it when a board's password changes.
    (The line printed here was also unquoted, and the project folder has a
    space in its name.)"""
    print()
    print("    Start the broker with:  python3 setup_manager.py --start")
    print("    or at every boot:       sudo python3 setup_manager.py --autostart")


def _reset_ha_password() -> bool:
    """Issue Home Assistant a new password, on purpose.

    Split out of --broker so that rewriting the broker's config and changing
    a secret are two different decisions, taken by two different commands.
    """
    print("\n  New password for Home Assistant")
    password = secrets.token_urlsafe(24)
    if not _set_broker_password(config.MQTT_HA_USER, password):
        return False
    print()
    print("    Home Assistant will stop connecting until you paste this in.")
    print("    Settings -> Devices & Services -> MQTT -> Configure.")
    print()
    print(f"        username: {config.MQTT_HA_USER}")
    print(f"        password: {password}")
    print()
    print("    Not shown again, and not stored anywhere.")
    return True


def reload_accounts(say=print) -> bool:
    """Make the running broker re-read the password file.

    THE BUG THIS FIXES was silent and total on Windows. Mosquitto reads the
    password file once and reloads it only on SIGHUP -- and Windows has no
    SIGHUP, so MQTT_RELOAD_COMMAND is empty there. Every password this hub
    issued therefore went into a file the running broker was not looking at:
    the new one was rejected, the old one still worked, and the only clue
    was "invalid auth" from something that had just been given a fresh
    password thirty seconds earlier.

    Now, when there is no reload command but the broker is OURS, it is
    restarted instead -- which achieves the same thing and is available on
    every platform. A broker somebody else started is left alone and the
    restart is asked for in words.
    """
    state, _ = broker_status()
    if state == "down":
        return True                   # nothing running: nothing to reload
    if state == "ours" and os.name != "nt":
        # OURS GETS A SIGHUP -- Mosquitto's own reload: password_file and
        # acl_file are re-read and every board stays connected. It used to
        # get MQTT_RELOAD_COMMAND first, which reloads the SYSTEM service
        # (failing, "mosquitto.service is not active"), and then a full
        # restart that dropped every board once per password written.
        try:
            os.kill(_read_pid(), signal.SIGHUP)
            return True
        except (OSError, TypeError) as exc:
            say(f"    [--] could not signal the broker: {exc}")
    if state == "foreign" and config.MQTT_RELOAD_COMMAND:
        reloaded, detail = _run(list(config.MQTT_RELOAD_COMMAND), timeout=30)
        if reloaded:
            return True
        say(f"    [--] reload command failed: {detail}")
    if state == "ours":
        say("    Restarting the broker so the new password takes effect...")
        return stop_broker(say) and start_broker(say)
    say("    [!] A broker is running that this hub did not start, so it "
        "is still using the OLD passwords. Restart it, or the new one "
        "will be refused.")
    return False


def _passwd_hash(password: str, style: str = "7") -> str:
    """One Mosquitto password-file value, hashed here rather than by
    `mosquitto_passwd -b`, whose command line put every board's password
    in `ps` and the audit log.

    "7" is what mosquitto_passwd 2.x writes and was checked byte-for-byte
    against mosquitto 2.0.18's own output: `$7$101$<salt>$<hash>` with
    PBKDF2-HMAC-SHA512, 101 iterations, a 12-byte random salt and a 64-byte
    key, both standard base64 WITH padding. "6" is the pre-2.0 form,
    sha512(password + salt), written only when the file already holds such
    entries so it stays homogeneous.
    """
    import base64
    salt = secrets.token_bytes(12)
    b64 = lambda raw: base64.b64encode(raw).decode("ascii")
    if style == "6":
        digest = hashlib.sha512(password.encode("utf-8") + salt).digest()
        return f"$6${b64(salt)}${b64(digest)}"
    iterations = 101
    digest = hashlib.pbkdf2_hmac("sha512", password.encode("utf-8"), salt,
                                 iterations, dklen=64)
    return f"$7${iterations}${b64(salt)}${b64(digest)}"


def write_passwd_entry(path: str, user: str, password: Optional[str] = None,
                       *, remove: bool = False) -> None:
    """Add, replace or drop ONE `user:<hash>` line in a Mosquitto password
    file, in-process. Creates the file when there is none (the old `-c`
    case), keeps every other line untouched, writes through a temp file
    created 0600 and renamed into place, then _lock_down_secret decides
    the final mode. Raises OSError/ValueError; callers report."""
    if not remove and password is None:
        raise ValueError("a password is needed unless remove=True")
    if not user or ":" in user or any(c in user for c in "\r\n"):
        raise ValueError(f"not a usable broker user name: {user!r}")
    try:
        with open(path, encoding="utf-8") as f:
            lines = f.read().splitlines()
    except FileNotFoundError:
        lines = []
    style = "6" if any(l.partition(":")[2].startswith("$6$")
                       for l in lines) else "7"
    kept = [l for l in lines if l.partition(":")[0] != user]
    if not remove:
        kept.append(f"{user}:{_passwd_hash(password, style)}")
    folder = os.path.dirname(os.path.abspath(path))
    os.makedirs(folder, exist_ok=True)
    body = "".join(line + "\n" for line in kept)
    try:
        before = os.stat(path)
    except OSError:
        before = None
    # A UNIQUE temp name, 0600 from creation: ws_server and a terminal hub
    # both write this file, and a shared fixed name let one writer rename
    # the other's half-written temp into place.
    fd, tmp = tempfile.mkstemp(dir=folder, prefix=os.path.basename(path) + ".",
                               suffix=".tmp")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(body)
    # THE GROUP MUST SURVIVE. A renamed-in temp file is a new inode owned
    # by whoever runs the hub, which silently dropped the 'mosquitto' group
    # --autostart gave the file (proved against a real broker: "Unable to
    # open pwfile"). Carry the old group across; if this account may not
    # chown to it, rewrite in place instead -- exactly what mosquitto_passwd
    # does -- so owner and group are untouched.
    if before is not None and os.name != "nt":
        try:
            os.chown(tmp, -1, before.st_gid)
            os.replace(tmp, path)
        except OSError:
            os.remove(tmp)
            with open(path, "w", encoding="utf-8") as f:
                f.write(body)
    else:
        os.replace(tmp, path)
    _lock_down_secret(path)


def _set_broker_password(user: str, password: str) -> bool:
    """Create or update one broker account."""
    passwd = config.MQTT_PASSWD_FILE
    # IN-PROCESS, not `mosquitto_passwd -b`: that put the password on a
    # command line visible to every process on the machine. The file is
    # created when absent (what -c used to do) and other accounts are kept.
    try:
        write_passwd_entry(passwd, user, password)
    except (OSError, ValueError) as e:
        print(f"    [ERROR] could not write {passwd}: {e}")
        return False
    # NEVER WORLD-READABLE (write_passwd_entry creates 0600 and applies
    # _lock_down_secret, which keeps 0640 where the broker's own service
    # group needs it after --autostart). Mosquitto 2.x warns on a
    # world-readable password file and future versions refuse to load it.
    # WRITING THE FILE IS HALF THE JOB. A running broker holds the old
    # contents until it is told otherwise, so a password that exists on disk
    # and is refused on the wire is not a puzzle -- it is this step missing.
    reload_accounts()
    return True


def _resync_board_accounts() -> None:
    """Rebuild every board's broker account from its record.

    THE MIGRATION LESSON, made proactive: carry the truths, regenerate the
    derived. The records hold each board's password in the clear (that is
    their job); the passwd file holds another machine's HASH of it, and a
    hash mangled in transit is an account Mosquitto silently drops at load
    -- "unable to decode password salt ... removing entry", printed only in
    the broker's own log. The hub's per-refusal self-repair cannot catch
    the case that actually happened: the BOARD being refused directly, a
    conversation the hub is not part of. So --broker now re-mints every
    board account from the records, unconditionally -- writing the same
    truth again is free, and writing it on THIS machine is the point.
    """
    from esp_firmware.esp_builder import EspBuilder
    rebuilt, failed = [], []
    # A board is a record that names itself -- wifi.json and any parked
    # registry fall out in EspBuilder._boards.
    for _slug, record in EspBuilder.headless()._boards():
        user = record.get("mqtt_user")
        password = record.get("mqtt_pass")
        if not user or not password:
            continue
        # Written here and reloaded ONCE below: _set_broker_password reloads
        # per account, which for a broker of ours without a reload command
        # meant a full stop/start -- every board dropped -- once PER BOARD.
        try:
            write_passwd_entry(config.MQTT_PASSWD_FILE, user, password)
            rebuilt.append(user)
        except (OSError, ValueError) as exc:
            print(f"    [ERROR] could not write {config.MQTT_PASSWD_FILE}: {exc}")
            failed.append(user)
    if rebuilt:
        print(f"    Rebuilt {len(rebuilt)} board account(s) from the "
              f"records: {', '.join(rebuilt)}")
        reload_accounts()
    if failed:
        print(f"    [X] Could not rebuild: {', '.join(failed)} -- run "
              f"mosquitto_passwd by hand and look at its error.")


def _setup_broker(ha: bool = True, hint: bool = True) -> bool:
    """Write the broker's configuration and give Home Assistant an account.

    Boards get their accounts when they are set up, not here -- one per
    board, minted by the hub. This only creates the two things that exist
    before any board does: the config, and Home Assistant's own login.
    """
    print("\n  Broker (Mosquitto)")
    # Three separate places now, and --broker can be the first thing you
    # ever run, so none of them can be assumed to exist: BROKER_DIR for the
    # broker's runtime files, certs/ for its certificate, configs/ for the
    # config and password file.
    os.makedirs(config.BROKER_DIR, exist_ok=True)
    os.makedirs(os.path.dirname(config.BROKER_TLS_CERT), exist_ok=True)
    os.makedirs(os.path.dirname(config.MQTT_CONF_FILE), exist_ok=True)

    # THE BOARDS' CERTIFICATE, made here if it is not already. It is the
    # broker's own, nothing else uses it, and it is cheap to replace: the
    # only thing that pins it is a board, and the hub re-provisions those
    # itself. That is the entire reason it is no longer local.crt.
    if config.MQTT_TLS and not generate_broker_cert():
        return False

    tls_port, _ = _broker_ports()
    conf = MOSQUITTO_CONF.format(
        passwd=config.MQTT_PASSWD_FILE,
        acl=write_acl_file(),
        tls_listener=(TLS_LISTENER.format(tls_port=tls_port,
                                          cert=config.BROKER_TLS_CERT,
                                          key=config.BROKER_TLS_KEY)
                      if config.MQTT_TLS else
                      "\n# TLS is OFF (HOMEVOICE_MQTT_TLS=0), so no encrypted"
                      "\n# listener is opened at all.\n"),
        plain_listener=_plain_listener(),
        # os.path.join with "" appends the platform's own separator, so the
        # path in the log reads the way the rest of the machine writes paths.
        directory=os.path.join(config.BROKER_DIR, ""),
    )
    with open(config.MQTT_CONF_FILE, "w", encoding="utf-8") as f:
        f.write(conf)
    print(f"    Wrote {config.MQTT_CONF_FILE}")
    print(f"    Wrote {_acl_file()} (per-board topic authorization)")
    # THE HUB'S OWN ACCOUNT, made here beside Home Assistant's. Kept if it
    # exists; only its password-file entry is re-minted, like the boards'.
    hub_user, _ = ensure_hub_account(repair=True)
    print(f"    The hub signs in as {hub_user!r} (password in "
          f"{_hub_account_path()}).")

    # AN EXISTING ACCOUNT IS LEFT ALONE.
    #
    # This used to mint a new password on every run, and it cost an evening.
    # --broker is the command you reach for to change something about the
    # BROKER -- a listener address, a rewritten config -- and every time you
    # did, Home Assistant silently stopped being able to log in, with
    # "not authorised" appearing in a log nowhere near the thing you changed.
    # A secret should only change when changing it is the point.
    accounts = _broker_accounts()
    # The boards' accounts are re-minted from their records EVERY run --
    # see _resync_board_accounts for the night that put it here. Home
    # Assistant's account is the one this cannot cover: its password is
    # deliberately stored nowhere, so it is left alone below and only
    # --reset-ha-password ever changes it.
    _resync_board_accounts()
    # ha=False (the full setup, no Home Assistant): no HA account.
    if not ha:
        if config.MQTT_HA_USER not in accounts:
            print("    Home Assistant: no login made (optional). If you use it:"
                  " python3 setup_manager.py --broker")
        return True
    if config.MQTT_HA_USER in accounts:
        print(f"    The account {config.MQTT_HA_USER!r} already exists and "
              f"has been left as it is.")
        print("    Home Assistant keeps working. To issue a new password "
              "deliberately: --reset-ha-password")
        if hint:
            _print_broker_start_hint()
        return True

    # Generated and PRINTED, once. It is not stored anywhere by the hub:
    # nothing here ever signs in as Home Assistant, so keeping a copy would
    # be a secret held for no reason.
    ha_password = secrets.token_urlsafe(24)
    if not _set_broker_password(config.MQTT_HA_USER, ha_password):
        return False
    print(f"    Created the account {config.MQTT_HA_USER!r}.")
    print()
    print("    PUT THIS INTO HOME ASSISTANT NOW -- it is not stored anywhere")
    print("    and will not be shown again:")
    print()
    bind = config.MQTT_PLAIN_BIND
    if bind:
        print("        host:     127.0.0.1")
    else:
        print("        host:     host.docker.internal   (Home Assistant in")
        print("                  Docker; use this machine's LAN IP if it is")
        print("                  in a VM or on another box)")
    print("        port:     1883")
    print(f"        username: {config.MQTT_HA_USER}")
    print(f"        password: {ha_password}")
    print()
    print("    Settings -> Devices & Services -> Add integration -> MQTT.")
    print("    Then the Tasmota integration, which rides on top of it.")
    if hint:
        _print_broker_start_hint()
    return True



def _self_sign(cert_path: str, key_path: str, hosts: List[str], label: str,
               dns_for_ips: bool) -> bool:
    """Write a self-signed CA certificate and its key. Returns success.

    ONE IMPLEMENTATION for both certificates. There are two of them --
    local.crt for the phone, broker.crt for the boards -- because their
    lifetimes are independent, NOT because they are shaped differently. The
    shape is the same and only wants writing once.

    The key is REUSED when one is already on disk: same key, new
    certificate. That is what lets a public-key pin survive a certificate
    being corrected or re-addressed, with nothing to redistribute.

    `hosts` become the SANs: a name as a DNS entry, an address as an IP
    entry -- and, with `dns_for_ips`, ALSO as a DNS entry of the same text
    (the boards need that; see _broker_cert_names).
    """
    try:
        from cryptography import x509
        from cryptography.x509.oid import NameOID
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import rsa
    except ImportError:
        print("  [ERROR] `cryptography` is required to generate a "
              "certificate. It is in PACKAGE_ASSETS -- install it and "
              "re-run.")
        return False
    if not hosts:
        print(f"  No addresses to put in {label} -- nothing generated.")
        return False
    names = []
    for host in hosts:
        try:
            names.append(x509.IPAddress(ipaddress.ip_address(host)))
        except ValueError:
            names.append(x509.DNSName(host))         # a real hostname
            continue
        if dns_for_ips:
            names.append(x509.DNSName(host))         # what BearSSL reads

    key = None
    if os.path.isfile(key_path):
        try:
            with open(key_path, "rb") as f:
                key = serialization.load_pem_private_key(f.read(),
                                                         password=None)
            print("  (reusing the existing private key -- same key, new "
                  "certificate)")
        except Exception:                          # noqa: BLE001
            key = None                             # unreadable: mint a fresh one
    if key is None:
        # RSA-2048, not larger. The ESP32's TLS stack has to do the maths in
        # the memory it has, and 4096 is beyond what it will manage.
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)

    subject = x509.Name(
        [x509.NameAttribute(NameOID.COMMON_NAME, "HomeVoice local")])
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(subject)              # self-signed: subject == issuer
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(days=1))
        # Ten years. The renewal cadence of a public CA exists to limit the
        # damage of a key nobody can revoke; this one you can simply delete.
        .not_valid_after(now + datetime.timedelta(days=3650))
        .add_extension(x509.SubjectAlternativeName(names), critical=False)
        # ca=True, AND THAT IS THE WHOLE OF A LONG BUG.
        #
        # This said ca=False, marked critical, which is correct for an
        # ordinary server certificate signed by somebody else. It is wrong
        # for these, because each is its own issuer: every client that trusts
        # one uses it as a TRUST ANCHOR, and a trust anchor is a certificate
        # authority by definition. A critical extension declaring "this is
        # not a CA" contradicts the job it is being given.
        #
        # Browsers and Python shrug at that. The ESP32 does not: Tasmota's
        # BearSSL refused the handshake outright and hung up, which the
        # broker logged as "unexpected eof while reading" and the board as
        # `MQT: TLS connection error: 56`. Weeks went on the fingerprint, the
        # port, the firewall and the clock; the certificate was malformed for
        # the use it was put to the entire time.
        #
        # path_length=0: it may sign nothing below itself, which is true.
        .add_extension(x509.BasicConstraints(ca=True, path_length=0),
                       critical=True)
        .sign(key, hashes.SHA256())
    )

    os.makedirs(os.path.dirname(cert_path), exist_ok=True)
    with open(cert_path, "wb") as f:
        f.write(cert.public_bytes(serialization.Encoding.PEM))
    # Created 0600, so never readable by others even for a moment; an
    # existing file is rewritten in place, keeping the 'mosquitto' group
    # --autostart gave broker.key.
    with _open_private(key_path, "wb") as f:
        f.write(key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.TraditionalOpenSSL,
            serialization.NoEncryption()))
    # The private key is the only thing here that must not be readable by
    # anyone else on the machine -- except the broker's own service group
    # after --autostart, which _lock_down_secret preserves. Best effort: on
    # Windows chmod is close to a no-op.
    _lock_down_secret(key_path)
    print(f"  Wrote {cert_path}")
    print(f"    {label}, valid ten years, naming {', '.join(hosts)}")
    return True


def _broker_cert_names():
    """Addresses to put in the BOARDS' certificate.

    Every address this machine holds, plus whatever the boards were told to
    look for.

    THIS LIST IS LOAD-BEARING, and the comment that used to sit here saying
    otherwise -- "boards pin the fingerprint rather than checking the name,
    so this is a convenience" -- was wrong, and cost real time. Tasmota
    checks the name AS WELL as the fingerprint, and has since 14.4.1. A
    certificate that does not name the address the board dialled is refused
    outright:

        MQT: TLS connection error: 56
        BR_ERR_X509_BAD_SERVER_NAME
        X.509 status: expected server name was not found in the chain

    EACH IP GOES IN TWICE, once as an iPAddress entry and once as a DNS
    entry with the same literal text. That looks redundant and is not:
    BearSSL, the TLS stack inside Tasmota, matches the name it dialled
    against the DNS entries only. An iPAddress entry alone is invisible to
    it, so a certificate that looks correct under `openssl x509 -text` still
    fails on the board. This is the single line that produced error 56.
    """
    wanted = set(_local_addresses())
    address = (_boards_wifi().get("hub_address") or "").strip()
    if address:
        wanted.add(address)
    wanted.add("127.0.0.1")
    return sorted(wanted)


def generate_broker_cert(force: bool = False) -> bool:
    """The certificate Mosquitto presents to the BOARDS.

    Separate from local.crt so that fixing the boards' end never touches a
    phone. Regenerating this one costs a re-provision of each board -- which
    the hub does itself over the cable or the air -- and nothing else.
    """
    if os.path.isfile(config.BROKER_TLS_CERT) and not force:
        return True
    print("\n  The broker's certificate (for the boards)")
    return _self_sign(config.BROKER_TLS_CERT, config.BROKER_TLS_KEY,
                      _broker_cert_names(), "the broker's certificate",
                      dns_for_ips=True)


def _generate_local_cert() -> bool:
    """Self-sign the hub's certificate (certs/local.crt), served on every
    local network and on the tailnet.

    The phone pins its KEY, not an address, so no address has to be known
    in advance: the names inside are just the addresses held now plus
    localhost, for the browser page's sake.

    REUSES the existing private key when one is on disk: same key, new
    certificate. Today the phone app pins the whole certificate's bytes, so
    any regeneration still breaks that pin -- but a stable key is what lets a
    future SPKI (public-key) pin survive certificate rotation, and keeping it
    costs nothing now.
    """
    # The phone pins this certificate's KEY, never an address, so the names
    # in it matter only to the browser page: the addresses this machine
    # holds now, plus localhost. Nothing is written down in advance.
    hosts = list(dict.fromkeys(hub_net.local_addresses() + ["localhost"]))
    # Ten years, like the boards' certificate. The phone app never checks
    # validity at all -- it pins the bytes -- so on the AP path expiry is
    # irrelevant; the browser test client is what the long lifetime is for.
    if not _self_sign(config.LOCAL_TLS_CERT, config.LOCAL_TLS_KEY, hosts,
                      "the access-point certificate", dns_for_ips=False):
        return False
    try:
        os.chmod(config.LOCAL_TLS_KEY, 0o600)    # never the broker's group
    except OSError:
        pass          # Windows: no POSIX modes, and the file is user-owned
    print("        Trust it once on each phone, or accept the browser warning. "
          "Without it the client cannot sign the auth nonce at all -- WebCrypto "
          "needs a secure context.")
    print("        The phone app pins this file's exact bytes: copy it into "
          "the app's assets/certs/local.crt and rebuild the APK.")
    return True


def _prompt_yes_no(question: str) -> bool:
    if NONINTERACTIVE:
        # "No" is the only predictable answer with nobody present. For an
        # OPTIONAL asset that means skip-and-use-the-fallback; for a
        # REQUIRED one the caller's decline path exits with instructions,
        # which is exactly what an unattended boot should do instead of
        # hanging on input().
        print(f"{question} [y/n]: n  (non-interactive)")
        return False
    while True:
        ans = input(f"{question} [y/n]: ").strip().lower()
        if ans in ("y", "yes"):
            return True
        if ans in ("n", "no"):
            return False
        print("    Please answer y or n.")


def _graceful_exit(reason: str):
    print("\n" + "=" * 60)
    print("  SETUP INCOMPLETE -- STOPPING")
    print("=" * 60)
    print(f"  {reason}")
    print("  The system cannot run without the required assets above.")
    if NONINTERACTIVE:
        print("  This is an unattended start (no terminal attached). Connect")
        print("  over SSH and run `python3 setup_manager.py` once to fetch")
        print("  what is missing -- then the service starts cleanly on its own.")
    else:
        print("  Re-run setup when ready.")
    print("=" * 60)
    sys.exit(1)


# ===========================================================================
# Main flow
# ===========================================================================

def _check_packages(check_only: bool) -> bool:
    print("-" * 60)
    print("  PYTHON PACKAGES")
    print("-" * 60)
    missing_required, missing_optional = [], []
    on_pi = _on_raspberry_pi()
    for pkg in PACKAGE_ASSETS:
        if pkg.get("pi_only") and not on_pi:
            print(f"  [..]  (Pi only ) {pkg['import_name']}  (pip: "
                  f"{pkg['pip_name']}) -- not a Raspberry Pi, skipped")
            continue
        present = _asset_present(pkg)
        tag = "REQUIRED" if pkg["required"] else "optional"
        mark = "[OK]  " if present else "[--]  "
        print(f"  {mark}({tag:8}) {pkg['import_name']}  (pip: {pkg['pip_name']})")
        if not present:
            (missing_required if pkg["required"] else missing_optional).append(pkg)

    if check_only:
        return len(missing_required) == 0

    todo = missing_required + missing_optional
    if not todo:
        print("\n  All packages present.")
        return True
    if not _online():
        _offline_help()
        if missing_required:
            _graceful_exit("Required packages are missing and there is no "
                           "internet to install them.")
        print("  Optional packages skipped for now -- the hub uses its fallbacks.")
        return True

    # EVERYTHING missing is installed, no questions: required first, then
    # optional. One bar line per package, so the log doubles as progress.
    import importlib
    total = len(todo)
    print(f"\n  Installing {total} package(s) automatically...\n")
    failed = []
    for i, pkg in enumerate(todo):
        label = pkg["pip_name"]
        sys.stdout.write(f"  {_bar(i, total)}  {i + 1}/{total}  {label} ... ")
        sys.stdout.flush()
        if pkg.get("install_cmd"):
            ok = _run_install_cmd(pkg["install_cmd"], quiet=True,
                                  timeout=pkg.get("timeout", 900))
        else:
            ok = _pip_install(label, quiet=True, pip_args=pkg.get("pip_args"),
                              timeout=pkg.get("timeout", 900))
        print("done" if ok else "FAILED")
        if not ok:
            failed.append(pkg)
    importlib.invalidate_caches()
    print(f"  {_bar(total, total)}  {total - len(failed)}/{total} installed\n")

    bad_required = [p for p in failed if p["required"]]
    for pkg in failed:
        print(f"  [WARN] {pkg['pip_name']} did not install"
              + ("" if pkg["required"] else " (optional -- fallback used)"))
    if bad_required:
        _graceful_exit("Could not install required package(s): "
                       + ", ".join(p["pip_name"] for p in bad_required)
                       + ". The pip error is printed above.")
    return True


def run_setup(check_only: bool = False) -> bool:
    print("=" * 60)
    print("  ASSET CHECK / SETUP MANAGER")
    print("=" * 60)

    packages_ok = _check_packages(check_only)

    print("\n" + "-" * 60)
    print("  FILES")
    print("-" * 60)
    print("\n  Checking assets against config.py paths...\n")
    # Images from before the one firmware folder move in first, or the
    # classic factory image would look missing and be fetched again.
    if not check_only:
        config.settle_firmware_folder(say=print)

    missing_required = []
    missing_optional = []

    for a in URL_ASSETS + MANUAL_ASSETS + ARCHIVE_ASSETS:
        present = _exists(a["path"])
        tag = "REQUIRED" if a["required"] else "optional"
        mark = "[OK]  " if present else "[--]  "
        print(f"  {mark}({tag:8}) {a['name']}")
        if not present:
            print(f"           path: {a['path']}")
            (missing_required if a["required"] else missing_optional).append(a)

    if check_only:
        print("\n  (check-only mode -- no downloads)")
        print(f"\n  Missing required packages: {'0' if packages_ok else '1+'}   "
              f"Missing required files: {len(missing_required)}   "
              f"Missing optional files: {len(missing_optional)}")
        return packages_ok and len(missing_required) == 0

    if not missing_required and not missing_optional:
        print("\n  All file assets present.")

    # Downloadable ones are fetched with no questions, required first. A
    # manual one (no "url") prints its instructions; a required manual one
    # ends the run, since the hub cannot start without it.
    skipped = [a for a in missing_optional
               if a.get("needs_import") and not _check_import(a["needs_import"])]
    for a in skipped:
        print(f"\n  (skipped: {a['name']} -- needs {a['needs_import']}, "
              f"which did not install)")
    missing_optional = [a for a in missing_optional if a not in skipped]
    fetchable = [a for a in missing_required + missing_optional
                 if "url" in a or "fetch" in a]
    manual = [a for a in missing_required + missing_optional
              if "url" not in a and "fetch" not in a]
    failed = []
    if fetchable and not _online():
        _offline_help()
        failed = fetchable
        fetchable = []
    total = len(fetchable)
    for i, a in enumerate(fetchable):
        print("\n" + "-" * 60)
        print(f"  {_bar(i, total)}  file {i + 1}/{total}: {a['name']}"
              + ("" if a["required"] else "  (optional)"))
        if a.get("note"):
            print("  " + a["note"].split(". ")[0].rstrip(".") + ".")
        if "fetch" in a:
            fetched = a["fetch"]()
        elif "extract_dir" in a:
            fetched = _download_and_extract_archive(a["url"], a["extract_dir"],
                                                    a.get("sha256"))
        else:
            fetched = _download(a["url"], a["path"], a.get("sha256"))
        if not fetched:
            failed.append(a)
    if total:
        got = total - sum(1 for a in fetchable if a in failed)
        print(f"\n  {_bar(total, total)}  {got}/{total} files downloaded")

    for a in manual:
        if not a["required"]:
            print(f"\n  (optional, not installed: {a['name']} -- see MANUAL.md)")
            continue
        print("\n" + "-" * 60)
        print(f"  MANUAL (REQUIRED): {a['name']}")
        print("    " + a["how"].format(path=a["path"]))
    for a in failed:
        print(f"\n  [WARN] Could not fetch: {a['name']}"
              + ("" if a["required"] else " (optional -- fallback used)"))
        if a.get("how"):
            print("    Or do it by hand:\n    " + a["how"].format(path=a["path"]))
    stop = [a for a in failed + manual if a["required"]]
    if stop:
        _graceful_exit("Required file(s) missing: "
                       + ", ".join(a["name"] for a in stop)
                       + ". Check the internet connection and run setup "
                       "again (it resumes where it stopped), or follow the "
                       "steps above.")

    # ---- Access-point certificate: generated ONCE, never re-offered ----
    # Last, because it reads NETWORK_BINDINGS and there is no point offering
    # to generate one before the packages it needs are confirmed present.
    print("\n" + "-" * 60)
    print("  ACCESS-POINT CERTIFICATE")
    print("-" * 60)
    have = (os.path.isfile(config.LOCAL_TLS_CERT)
            and os.path.isfile(config.LOCAL_TLS_KEY))
    if have:
        print(f"  [OK]  {config.LOCAL_TLS_CERT}")
        # NO regeneration offer here, deliberately. The phone app pins this
        # certificate's exact bytes (socket_client.dart compares DER
        # byte-for-byte), so a casual 'y' at a startup prompt would lock
        # every enrolled phone out until the new cert is copied into the
        # app's assets/certs/ and the APK rebuilt. run_setup() fires on
        # EVERY boot (main.py init()), which made that prompt a standing
        # invitation to brick the clients. Regeneration is an explicit,
        # rare act now.
        print("        Pinned by the phone app -- never regenerated "
              "automatically. If NETWORK_BINDINGS changed, run: "
              "python setup_manager.py --regen-cert")
    else:
        print("  [--]  Not generated yet.")
        print("        The ap-* bindings need it. The tailnet binding uses the "
              "certificate from `tailscale cert`, which cannot cover a private "
              "address and so cannot serve them.")
        # Unattended: generate WITHOUT asking. Unlike a download this is
        # local, fast and safe -- and a first boot that silently lacks its
        # certificate loses the ap-* bindings for no good reason.
        # Generated without asking: local, fast, no network needed.
        _generate_local_cert()

    if not _BRIEF:
        _next_steps()
        print("\n" + "=" * 60)
        print("  Setup finished. Review any optional items skipped above.")
        print("=" * 60)
    return True


def _next_steps() -> None:
    """What setup cannot install for you (it needs admin rights, so it is
    never done silently): say what is there and what is not. MANUAL.md has
    the steps for each."""
    print("\n" + "-" * 60)
    print("  OTHER PARTS (installed by you once -- see MANUAL.md)")
    print("-" * 60)
    tailscale = shutil.which("tailscale") or (
        os.name == "nt" and os.path.isfile(
            r"C:\Program Files\Tailscale\tailscale.exe"))
    rows = [
        ("Mosquitto (MQTT broker)", bool(find_mosquitto_tool("mosquitto")),
         "then: python setup_manager.py --broker, --autostart"),
        ("Docker (runs Home Assistant)", bool(shutil.which("docker")),
         "then the docker run line in MANUAL.md"),
        ("Tailscale (reach the hub from anywhere)", bool(tailscale),
         "optional"),
        ("Secrets (HOMEVOICE_API_KEY, HOMEVOICE_FERNET_KEY)",
         bool(os.environ.get("HOMEVOICE_API_KEY")
              and os.environ.get("HOMEVOICE_FERNET_KEY")),
         "MANUAL.md, 'Secrets'"),
    ]
    for name, ok, hint in rows:
        print(f"  {'[OK]' if ok else '[--]'}  {name}"
              + ("" if ok else f"  -- {hint}"))


def _sudo(command: List[str], timeout: float = 1800) -> bool:
    """One admin step on Linux. sudo asks for the password itself: nothing
    here reads, keeps or passes it on."""
    print("    sudo " + " ".join(command))
    try:
        return subprocess.run(["sudo", *command], timeout=timeout).returncode == 0
    except (OSError, subprocess.TimeoutExpired) as exc:
        print(f"    [WARN] could not run it: {exc}")
        return False


def _as_admin_windows(command: str) -> bool:
    """One admin step on Windows: Windows shows its own permission prompt;
    nothing here sees a password."""
    print(f"    (Windows asks for permission)  {command}")
    ps = ("Start-Process -FilePath cmd.exe -ArgumentList '/c " + command
          + "' -Verb RunAs -Wait -WindowStyle Hidden")
    try:
        return subprocess.run(["powershell", "-NoProfile", "-Command", ps],
                              timeout=600).returncode == 0
    except (OSError, subprocess.TimeoutExpired) as exc:
        print(f"    [WARN] could not run it: {exc}")
        return False


#: winget ids (Windows 10/11 have winget through App Installer).
_WINGET = {"mosquitto": "EclipseFoundation.Mosquitto", "tailscale": "Tailscale.Tailscale",
           "docker": "Docker.DockerDesktop"}


def _tailscale_found() -> bool:
    return bool(shutil.which("tailscale")) or (os.name == "nt" and os.path.isfile(
        os.path.join(os.environ.get("ProgramFiles", r"C:\Program Files"),
                     "Tailscale", "tailscale.exe")))


def _install_system(name: str) -> bool:
    """Mosquitto, Tailscale or Docker: winget on Windows (each shows its own
    progress bar); on Linux apt (Mosquitto), or the project's own install
    script (Tailscale, Docker)."""
    if os.name == "nt":
        if not shutil.which("winget"):
            print("    winget is missing (Microsoft Store -> App Installer).")
            return False
        cmd = ["winget", "install", "-e", "--id", _WINGET[name],
               "--accept-package-agreements", "--accept-source-agreements"]
        print(f"    Installing {name.title()} (winget)...")
        try:
            subprocess.run(cmd, timeout=1800)
        except (OSError, subprocess.TimeoutExpired) as exc:
            print(f"    [WARN] {exc}")
    elif name == "mosquitto":
        if shutil.which("apt-get") and _sudo(["apt-get", "update"]):
            _sudo(["apt-get", "install", "-y", "-o", "Dpkg::Progress-Fancy=1",
                   "mosquitto", "mosquitto-clients"])
    elif name == "docker" and shutil.which("curl"):
        print("    Installing Docker (get.docker.com)...")
        try:
            subprocess.run(["sh", "-c", "curl -fsSL https://get.docker.com | sh"],
                           timeout=3600)
        except (OSError, subprocess.TimeoutExpired) as exc:
            print(f"    [WARN] {exc}")
    elif name == "tailscale" and shutil.which("curl"):
        print("    Installing Tailscale (tailscale.com)...")
        try:
            subprocess.run(["sh", "-c", "curl -fsSL https://tailscale.com/install.sh | sh"],
                           timeout=1800)
        except (OSError, subprocess.TimeoutExpired) as exc:
            print(f"    [WARN] {exc}")
    if name == "mosquitto":
        return bool(find_mosquitto_tool("mosquitto"))
    if name == "docker":
        return bool(shutil.which("docker"))
    return _tailscale_found()


def _login_name() -> str:
    """The user running setup (from the uid; $USER can be stale)."""
    try:
        import pwd as _pwd
        return _pwd.getpwuid(os.getuid()).pw_name
    except (ImportError, KeyError, AttributeError):
        return os.environ.get("USER") or os.environ.get("USERNAME") or ""


def _tailscale_operator() -> None:
    """Linux: make this user Tailscale's operator, so the hub can sign in to
    and out of Tailscale from the app without root ("Access denied:
    checkprefs access denied" otherwise). Through sudo; harmless when it is
    already set, so it runs on every setup."""
    if os.name == "nt" or not shutil.which("tailscale"):
        return
    user = _login_name()
    if user and _sudo(["tailscale", "set", f"--operator={user}"]):
        print(f"  [OK]  Tailscale operator: {user}")
    else:
        print(f"  [--]  Tailscale operator not set. Later: sudo tailscale set --operator={user}")
        _attention(f"Tailscale operator: sudo tailscale set --operator={user}")


def _system_timezone() -> str:
    """This machine's time zone for Home Assistant's container; UTC when it
    cannot be told (Windows: set it in Home Assistant's own settings)."""
    if os.name != "nt":
        ok, tz = _run(["timedatectl", "show", "-p", "Timezone", "--value"])
        if ok and tz:
            return tz
        try:
            with open("/etc/timezone", encoding="utf-8") as f:
                tz = f.read().strip()
            if tz:
                return tz
        except OSError:
            pass
    return "UTC"


_HA_IMAGE = "ghcr.io/home-assistant/home-assistant:stable"


def _ha_token(env_file: str) -> bool:
    """Home Assistant's long-lived token for the hub. ASKED first whether
    you have one to paste (checked with Home Assistant, then saved);
    otherwise, asked, it is made here. On a new Home Assistant it creates the owner
    account (a username and password typed here); on one that already has
    an owner it signs in with that account. The token goes into the
    secrets file as HOMEVOICE_HA_TOKEN; the password is never kept.
    True when the hub has a token afterwards."""
    if os.environ.get("HOMEVOICE_HA_TOKEN"):
        print("  [OK]  Home Assistant token")
        return True
    base = config.HA_BASE_URL.rstrip("/")
    client_id = base + "/"
    # One you already have, pasted (not shown) and checked with Home
    # Assistant, comes first; making a new one is the fallback.
    if _prompt_yes_no("  Do you already have a Home Assistant token to paste in?"):
        while True:
            token = getpass.getpass(
                "    Token (not shown; Enter alone to make a new one instead): ").strip()
            if not token:
                break
            req = urllib.request.Request(base + "/api/",
                                         headers={"Authorization": f"Bearer {token}"})
            try:
                with urllib.request.urlopen(req, timeout=10):
                    pass
                pass
            except urllib.error.HTTPError as e:
                if e.code == 401:
                    print("    Home Assistant refused that token. Paste it again.")
                    continue
                print("    Could not check it; saved anyway.")
            except (OSError, ValueError, http.client.HTTPException):
                print("    Home Assistant is not up yet; saved unchecked.")
            if not _env_file_set(env_file, "HOMEVOICE_HA_TOKEN", token):
                return False
            print("  [OK]  Token saved")
            return True
    if not _prompt_yes_no("  Make the hub's Home Assistant token now? (asks for HA's owner login)"):
        print("  Skipped.")
        return False

    def call(path: str, data=None, form: bool = False, timeout: float = 15):
        body, headers = None, {}
        if data is not None:
            if form:
                body = urllib.parse.urlencode(data).encode()
                headers["Content-Type"] = "application/x-www-form-urlencoded"
            else:
                body = json.dumps(data).encode()
                headers["Content-Type"] = "application/json"
        req = urllib.request.Request(base + path, data=body, headers=headers)
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode() or "null")

    # Its first start can take several minutes (longer on a Pi).
    print("  Waiting for Home Assistant (first start takes a few minutes; Ctrl+C skips)...")
    steps, started = None, time.time()
    try:
        while time.time() - started < 900:
            try:
                steps = call("/api/onboarding", timeout=5)
                break
            except urllib.error.HTTPError as e:
                if e.code == 404:        # set up already: no onboarding left
                    steps = []
                    break
            except (OSError, ValueError, http.client.HTTPException):
                pass
            sys.stdout.write(f"\r    {int(time.time() - started)} s ...   ")
            sys.stdout.flush()
            time.sleep(5)
    except KeyboardInterrupt:
        print("\n  Skipped. Run setup again later.")
        _attention("Home Assistant token: not made. Run setup again.")
        return False
    print()
    if steps is None:
        print("  Home Assistant did not answer in 15 minutes. Run setup again later.")
        _attention("Home Assistant token: Home Assistant was not up. Run setup again.")
        return False
    has_owner = not steps or any(
        s.get("step") == "user" and s.get("done") for s in steps if isinstance(s, dict))
    try:
        if not has_owner:
            print("  Make Home Assistant's owner account:")
            user = ""
            while not user:
                user = input("    Username: ").strip()
            while True:
                password = getpass.getpass("    Password (not shown): ")
                if password and password == getpass.getpass("    Again: "):
                    break
                print("    Empty, or the two did not match. Again.")
            code = call("/api/onboarding/users", {
                "client_id": client_id, "name": user, "username": user,
                "password": password, "language": "en"})["auth_code"]
        else:
            print("  Sign in with Home Assistant's owner account:")
            user = input("    Username: ").strip()
            password = getpass.getpass("    Password (not shown): ")
            flow = call("/auth/login_flow", {
                "client_id": client_id, "handler": ["homeassistant", None],
                "redirect_uri": client_id})
            done = call(f"/auth/login_flow/{flow['flow_id']}", {
                "client_id": client_id, "username": user, "password": password})
            if done.get("type") != "create_entry":
                print("  [X]   Wrong username or password. Run setup again to retry.")
                _attention("Home Assistant token: wrong username or password. Run setup again.")
                return False
            code = done["result"]
        password = ""
        access = call("/auth/token", {"grant_type": "authorization_code",
                                      "code": code, "client_id": client_id},
                      form=True)["access_token"]
        # The long-lived token comes over Home Assistant's websocket.
        from websockets.sync.client import connect
        ws_url = "ws" + base[4:] + "/api/websocket"     # http(s):// -> ws(s)://
        with connect(ws_url, open_timeout=15) as ws:
            ws.recv(timeout=15)                              # auth_required
            ws.send(json.dumps({"type": "auth", "access_token": access}))
            if json.loads(ws.recv(timeout=15)).get("type") != "auth_ok":
                raise ValueError("Home Assistant did not accept the sign-in")
            ws.send(json.dumps({
                "id": 1, "type": "auth/long_lived_access_token", "lifespan": 3650,
                "client_name": "HomeVoice hub " + time.strftime("%Y-%m-%d %H:%M")}))
            answer = json.loads(ws.recv(timeout=15))
        token = answer.get("result") if answer.get("success") else None
        if not token:
            raise ValueError(f"no token came back ({answer.get('error')})")
    except KeyboardInterrupt:
        print("\n  Skipped. Run setup again later.")
        _attention("Home Assistant token: not made. Run setup again.")
        return False
    except Exception as exc:
        print(f"  [X]   Could not make the token: {exc}")
        print("        Make one in Home Assistant: Profile -> Security -> Long-lived access tokens.")
        _attention("Home Assistant token: not made. Make one in Home Assistant "
                   "(Profile -> Security -> Long-lived access tokens) and run setup again to paste it.")
        return False
    if not _env_file_set(env_file, "HOMEVOICE_HA_TOKEN", token):
        return False
    print("  [OK]  Token saved (the password was not kept)")
    return True


def _docker_group() -> None:
    """Linux: this user in the docker group, so the hub (and you) can use
    Docker without sudo -- from the next login, and for the hub's service
    from its next start. Checked on every run, not only after installing.
    (Windows: Docker Desktop's installer adds you to docker-users itself.)"""
    user = _login_name()
    try:
        import grp as _grp
        import pwd as _pwd
        group = _grp.getgrnam("docker")
        if user in group.gr_mem or _pwd.getpwnam(user).pw_gid == group.gr_gid:
            return
    except (ImportError, KeyError):
        return                    # no docker group (Docker missing) or no user
    if _sudo(["usermod", "-aG", "docker", user]):
        print(f"  [OK]  {user} can use Docker (from the next login)")
    else:
        _attention(f"Docker group: sudo usermod -aG docker {user}")


def _docker_and_ha(online: bool) -> bool:
    """Optional, and ASKED first (y/n): Docker, then Home Assistant in it
    (the MANUAL's docker run line). The image download shows Docker's own
    progress bars. True when Home Assistant is there afterwards, so the
    broker gives it a login."""
    windows = os.name == "nt"
    if not windows and shutil.which("docker"):
        _docker_group()
    if find_ha_container(running=True) or find_ha_container(running=False) \
            or ha_is_up(timeout=2.0):
        print("  [OK]  Home Assistant")
        return True
    if not _prompt_yes_no("  Install Docker and Home Assistant now? (a large download)"):
        print("  Skipped.")
        return False
    if not online:
        print("  No internet: skipped.")
        _attention("Home Assistant: skipped, no internet. Run setup again when online.")
        return False
    if not shutil.which("docker"):
        if not _install_system("docker"):
            if windows:
                print("  Start Docker Desktop once, then run setup again for Home Assistant.")
                _attention("Home Assistant: start Docker Desktop once, then run setup again.")
            else:
                print("  [X]   Docker did not install (its output is above).")
                _attention("Docker did not install (its error is above). Run setup again.")
            return False
        if not windows:
            _docker_group()
    if windows and not docker_ready() and not start_docker(say=print):
        print("  Start Docker Desktop, then run setup again to add Home Assistant.")
        _attention("Home Assistant: start Docker Desktop, then run setup again.")
        return False

    def docker(args: List[str], quiet: bool = False) -> bool:
        # Linux: through sudo (the docker group only counts from the next
        # login). Windows: Docker Desktop needs no admin.
        if not windows:
            if quiet:
                return subprocess.run(["sudo", "docker", *args],
                                      capture_output=True).returncode == 0
            return _sudo(["docker", *args], timeout=3600)
        if not quiet:
            print("    docker " + " ".join(args))
        try:
            return subprocess.run(["docker", *args], timeout=3600,
                                  capture_output=quiet).returncode == 0
        except (OSError, subprocess.TimeoutExpired) as exc:
            print(f"    [WARN] {exc}")
            return False

    if docker(["inspect", "homeassistant"], quiet=True):
        ok = docker(["start", "homeassistant"])
    else:
        print("  Downloading Home Assistant...")
        if not docker(["pull", _HA_IMAGE]):
            print("  [X]   The download stopped. Run setup again: it carries on.")
            _attention("Home Assistant: the download stopped. Run setup again: it carries on.")
            return False
        config_dir = os.path.join(os.path.expanduser("~"), "homeassistant")
        ok = docker(["run", "-d", "--name", "homeassistant", "--restart=unless-stopped",
                     *(["-p", "8123:8123"] if windows else ["--network=host"]),
                     "-e", f"TZ={_system_timezone()}",
                     "-v", f"{config_dir}:/config", _HA_IMAGE])
    print("  [OK]  Home Assistant is starting" if ok else
          "  [X]   Home Assistant did not start (Docker's message is above).")
    if not ok:
        _attention("Home Assistant did not start (Docker's message is above). Run setup again.")
    return ok


def _free_broker_port() -> bool:
    """The system's own Mosquitto, started by its installer with a default
    config, would hold port 1883 and stop the hub's broker. It is turned off
    and left installed (--autostart is the deliberate opposite)."""
    if _autostarted():
        return True
    if os.name == "nt":
        found, state = _run(["sc", "query", "mosquitto"])
        if not found:
            return True                          # no Mosquitto service
        if "RUNNING" not in state and "AUTO_START" not in _run(["sc", "qc", "mosquitto"])[1]:
            return True
        print("    Turning off the Mosquitto Windows service (it would block the hub's broker).")
        return _as_admin_windows("net stop mosquitto & sc config mosquitto start= demand")
    if "enabled" != _run(["systemctl", "is-enabled", "mosquitto"])[1] and \
            "active" != _run(["systemctl", "is-active", "mosquitto"])[1]:
        return True
    print("    Turning off the system Mosquitto (it would block the hub's broker).")
    return _sudo(["systemctl", "disable", "--now", "mosquitto"])


#: The one-command run is BRIEF: only what is happening, what to answer and
#: what went wrong. The separate commands (--doctor, --broker...) say more.
_BRIEF = False

#: What the one-command run skipped or could not do, said again at the end
#: ("Needs attention"), each with what to do.
_ATTENTION: List[str] = []


def _attention(line: str) -> None:
    if line not in _ATTENTION:
        _ATTENTION.append(line)

_BRIEF_DROP = re.compile(
    r"^\s*(-{20,}|={20,})\s*$|\[OK\]|\[\.\.\]|\[--\]|^\s+path: |PYTHON PACKAGES|"
    r"^\s*FILES\s*$|ASSET CHECK|Checking assets|All packages present|"
    r"All file assets present|ACCESS-POINT CERTIFICATE|Pinned by the phone app|"
    r"NETWORK_BINDINGS changed|python setup_manager.py --regen-cert")


class _BriefOut:
    """stdout for the package and file checks in brief mode: whole lines that
    only say "already fine" (or are section boxes) are dropped; progress bars,
    questions and warnings pass. A partial line is passed on flush, so an
    input() prompt still shows."""
    def __init__(self, out):
        self._out, self._line = out, ""

    def write(self, text: str) -> int:
        self._line += text
        while "\n" in self._line:
            line, self._line = self._line.split("\n", 1)
            if "[WARN]" in line or line.strip().startswith("(skipped:"):
                _attention(line.strip().replace("[WARN] ", ""))
            if line.strip() and not _BRIEF_DROP.search(line):
                self._out.write(line + "\n")
        return len(text)

    def flush(self) -> None:
        if self._line and not _BRIEF_DROP.search(self._line):
            self._out.write(self._line)
            self._line = ""
        self._out.flush()

    def __getattr__(self, name):
        return getattr(self._out, name)


def _set_up_everything() -> bool:
    """python3 setup_manager.py from a terminal, on Linux or Windows: a new
    hub, in order, and safe to run again.

      1. packages, files and the certificate (run_setup)
      2. Mosquitto and Tailscale, if missing; this user made Tailscale's
         operator (so the app can sign in without root)
      3. ASKS (y/n) whether to install Docker and Home Assistant; then
         (asked) makes the hub's Home Assistant token
      3b. the broker's config (a Home Assistant login only when it is
         there), with the system's own Mosquitto kept off port 1883
      4. the secrets file: your old keys if you have them (asked), else
         new ones
      5. ASKS whether the hub should start by itself (Linux: at boot;
         Windows: at sign-in). No unless you say yes; --service does it later
      6. pairing, when the hub is running

    No password is stored. On Linux sudo asks at each admin step, and
    `sudo -k` at the end makes the next one ask again; on Windows, Windows
    shows its own permission prompt.
    """
    windows = os.name == "nt"
    py = "python" if windows else "python3"
    if _is_admin() and not windows:
        print("  Run it as yourself, without sudo: python3 setup_manager.py")
        return False
    global _BRIEF
    _BRIEF = True
    print("HomeVoice hub setup. " + ("Windows asks for permission" if windows
          else "sudo asks for your password") + " when needed; it is never saved.")

    def head(title: str) -> None:
        print(f"\n== {title}")

    head("Packages and files")
    real_out = sys.stdout
    sys.stdout = _BriefOut(real_out)
    try:
        run_setup()
    finally:
        sys.stdout.flush()
        sys.stdout = real_out
    print("  [OK]  Packages and files")

    head("Mosquitto and Tailscale")
    online = _online()
    ok = True
    if find_mosquitto_tool("mosquitto"):
        print("  [OK]  Mosquitto")
    elif online and _install_system("mosquitto"):
        print("  [OK]  Mosquitto installed")
    else:
        ok = False
        _attention("Mosquitto is missing (see above). Run setup again when online.")
        print("  [X]   Mosquitto is missing: " + (
            "winget install -e --id EclipseFoundation.Mosquitto" if windows
            else "sudo apt install -y mosquitto mosquitto-clients"))
    if _tailscale_found():
        print("  [OK]  Tailscale")
    elif online and _install_system("tailscale"):
        print("  [OK]  Tailscale installed")
    else:
        print("  [--]  Tailscale not installed (optional)")
        _attention("Tailscale not installed: no remote access. Run setup again when online.")
    _tailscale_operator()

    want_ha = False
    if ok:
        head("Home Assistant (optional)")
        want_ha = _docker_and_ha(online)
        if want_ha and windows:
            # Home Assistant in Docker Desktop cannot reach 127.0.0.1 on
            # this PC: the broker's plain port listens for it (MANUAL.md).
            config.MQTT_PLAIN_BIND = ""
    if ok:
        head("Broker")
        # Only its result, and Home Assistant's login when one is made (it
        # is shown once and stored nowhere).
        said = io.StringIO()
        with contextlib.redirect_stdout(said):
            ok = _setup_broker(ha=want_ha, hint=False)
        login = [l for l in said.getvalue().splitlines()
                 if re.match(r"\s*(host|port|username|password):", l)]
        if not ok:
            print(said.getvalue().rstrip())
        else:
            print("  [OK]  Broker")
            if login:
                print("  Home Assistant's MQTT login (shown once, not stored; "
                      "put it in Home Assistant's MQTT integration):")
                for l in login:
                    print("    " + l.strip())
        if ok and not _free_broker_port():
            print("  [WARN] The system Mosquitto is still on: the hub's broker may not start.")
            _attention("The system Mosquitto is still on: sudo systemctl disable --now mosquitto")
    if ok:
        head("Keys")
        home = os.path.expanduser("~")
        env_file = os.path.join(home, ".homevoice", "env")
        ok = _write_secrets_file(env_file)
        if ok and not windows:
            _bashrc_points_at_secrets(home, say=lambda *a, **k: None)
        if ok and want_ha and windows:
            try:
                with open(env_file, encoding="utf-8") as f:
                    has = any(l.startswith("HOMEVOICE_MQTT_PLAIN_BIND=") for l in f)
                if not has:
                    with open(env_file, "a", encoding="utf-8") as f:
                        f.write("HOMEVOICE_MQTT_PLAIN_BIND=\n")
                    pass
            except OSError as exc:
                print(f"    [WARN] could not add HOMEVOICE_MQTT_PLAIN_BIND= to {env_file}: {exc}")
    has_token = False
    if ok and want_ha:
        head("Home Assistant token")
        has_token = _ha_token(env_file)
    by_itself = False
    if ok:
        head("Start by itself")
        by_itself = _prompt_yes_no(
            "  Start the hub by itself " + ("each time you sign in?" if windows
                                            else "at every boot?"))
        if by_itself:
            ok = _service(["--quiet"])
            if ok and windows:
                os.startfile(_startup_entry())       # and start it now
                time.sleep(8)
    if not windows:
        subprocess.run(["sudo", "-k"], capture_output=True)   # forget the sudo login now

    def needs_attention() -> None:
        if _ATTENTION:
            print("\nNeeds attention:")
            for line in _ATTENTION:
                print("  - " + line)

    if not ok:
        needs_attention()
        print(f"\nSetup stopped above. Fix it and run {py} setup_manager.py again.")
        return False
    if by_itself:
        head("Pair your phone")
        _open_pairing([])
    else:
        print(f"\nStart the hub: {py} ws_server.py   Then pair: {py} setup_manager.py --pair")
    if want_ha:
        print("\nHome Assistant: open http://<this machine's address>:8123"
              + (", sign in and answer its first questions." if has_token
                 else " and create the owner account."))
        print("Then add the MQTT integration with the login above, and Tasmota.")
    needs_attention()
    print(("\nDone, apart from the above." if _ATTENTION else "\nDone.")
          + f" To check everything: {py} setup_manager.py --doctor")
    return True


def _start_and_check() -> bool:
    # Brings the stack up and then CHECKS it, because "started" and
    # "working" turned out to be very different claims -- an evening was
    # spent watching a broker that was running perfectly and being
    # talked to by nobody.
    up = ensure_stack()
    print()
    return _doctor() and up


def _regen_broker_cert() -> bool:
    # SAFE TO RUN, unlike --regen-cert. Nothing pins this except the
    # boards, and re-provisioning a board takes seconds over the cable
    # or nothing at all over the air.
    print("Replacing the broker's certificate (the one the BOARDS see).")
    print("The phone app is unaffected -- it pins local.crt, which this")
    print("does not touch. Re-provision each board afterwards so it")
    print("takes the new fingerprint.")
    return generate_broker_cert(force=True)


def _regen_local_cert() -> bool:
    # The ONLY path that can overwrite an existing certificate. Kept off
    # run_setup() entirely so no routine startup is ever one keystroke
    # away from invalidating the bytes every phone has pinned.
    print("Regenerating the access-point certificate DELIBERATELY.")
    print("AFTER THIS: every paired phone must pair again -- each one")
    print("pinned the old key and will refuse the new one, which is the")
    print("pin doing its job. Run --pair once per phone.")
    return _generate_local_cert()


#: flag -> (what it does, given argv). ONE command per run. --broker is its
#: own flag, not part of the ordinary run: it prints a password once, and a
#: secret shown in the middle of a routine setup is a secret somebody
#: scrolls past. --autostart is run once, by hand, with admin rights; it
#: shows what it will change and asks first. --reset-ha-password is apart
#: from --broker, which no longer touches an account that already exists:
#: changing a working secret should take a command that says so.
def _grammar_model(argv) -> bool:
    """--grammar big | small: which English model reads sentence structure.

    small  en_core_web_sm  -- 12 MB, fast (the default; setup installs it)
    big    en_core_web_trf -- a transformer: reads sentences better, but a
           few hundred MB (it brings PyTorch) and slower per sentence.
           Refused on a Raspberry Pi unless --anyway is added.
    Installed only when asked for here. The choice is kept in
    ~/.homevoice/env (HOMEVOICE_GRAMMAR_MODEL); restart the hub after."""
    rest = [a for a in argv[1:] if not a.startswith("-")]
    want = (rest[0].lower() if rest else "")
    if want not in ("big", "small"):
        print("usage: setup_manager.py --grammar big | small")
        return False
    from core import config
    if want == "small":
        config.set_secret("HOMEVOICE_GRAMMAR_MODEL", "sm")
        print("  Grammar: the small model (en_core_web_sm). Restart the hub.")
        return True
    if _on_raspberry_pi() and "--anyway" not in argv:
        # A transformer on every sentence, beside the voice and meaning
        # models: slower commands and a busy CPU for a few rare words.
        print("  Not on a Raspberry Pi: the big model runs a transformer on every "
              "sentence, which makes each command noticeably slower there. The "
              "small one stays. (setup_manager.py --grammar big --anyway to "
              "insist.)")
        return False
    print("  Installing the big grammar model (en_core_web_trf) -- a few hundred "
          "MB, with PyTorch. This can take several minutes ...")
    if not _check_import("en_core_web_trf"):
        ok = _run_install_cmd([sys.executable, "-m", "spacy", "download",
                               "en_core_web_trf"], quiet=False, timeout=3600)
        if not ok:
            print("  [WARN] it did not install -- the small model stays in use.")
            return False
    config.set_secret("HOMEVOICE_GRAMMAR_MODEL", "trf")
    print("  Grammar: the big model (en_core_web_trf). Restart the hub.\n"
          "  Back to the small one any time:  setup_manager.py --grammar small")
    return True


_COMMANDS = {
    "--doctor": lambda argv: _doctor(),
    "--broker": lambda argv: _setup_broker(),
    "--start": lambda argv: _start_and_check(),
    "--stop": lambda argv: stop_broker(),
    "--autostart": lambda argv: autostart(),
    "--reset-ha-password": lambda argv: _reset_ha_password(),
    "--service": _service,
    "--service-root": _service,             # the root half --service runs
    "--clock-sudo": lambda argv: _clock_sudo(),
    "--regen-broker-cert": lambda argv: _regen_broker_cert(),
    "--regen-cert": lambda argv: _regen_local_cert(),
    "--phones": lambda argv: _list_phones(),
    "--pair-qr": _write_pair_qr,
    "--pair": _open_pairing,
    "--unpair": _unpair,
    "--grammar": _grammar_model,
}
#: Flags that are not commands: --check narrows the ordinary run, and
#: --user/--home carry --service-root's arguments.
_MODIFIERS = {"--check", "--user", "--home"}


def cli() -> None:
    """Installer/doctor entry point (python3 setup_manager.py ...).

    Unknown flags are REFUSED, not ignored: a mistyped `--doctr` used to fall
    through to the full interactive setup -- downloads and all -- and two
    commands at once silently ran whichever came first in a list."""
    # --esphome-key USED TO LIVE HERE and is deliberately gone.
    #
    # It printed 32 base64 bytes for a device YAML's `api: encryption: key:`
    # and told you to export HOMEVOICE_ESPHOME_KEY. Both ends of that are
    # obsolete: nothing compiles a key into firmware, and there is no shared
    # key to export. Each board is issued its own password when it is set
    # up, stored in that board's file under BOARDS_DIR, and shown on demand
    # from Settings. You can remove HOMEVOICE_ESPHOME_KEY from your
    # environment.
    argv = sys.argv
    flags = [a for a in argv[1:] if a.startswith("-")]
    usage = ("usage: setup_manager.py [--check | " + " | ".join(_COMMANDS)
             + "] [argument]")
    if any(f in ("-h", "--help") for f in flags):
        print(usage)
        sys.exit(0)
    unknown = [f for f in flags if f not in _COMMANDS and f not in _MODIFIERS]
    commands = [f for f in dict.fromkeys(flags) if f in _COMMANDS]
    if "--service-root" in commands:                  # its own pair, internal
        commands = ["--service-root"]
    clash = commands + (["--check"] if "--check" in flags else [])
    if unknown or len(clash) > 1:
        why = (f"unknown option {unknown[0]}" if unknown
               else "one command at a time: " + " ".join(clash))
        print(f"setup_manager.py: {why}\n{usage}", file=sys.stderr)
        sys.exit(2)
    if commands:
        sys.exit(0 if _COMMANDS[commands[0]](argv) else 1)
    if "--check" in flags or NONINTERACTIVE:
        ok = run_setup(check_only=("--check" in flags))
    else:
        ok = _set_up_everything()
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    cli()

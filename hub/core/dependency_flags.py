"""Optional dependency flags with version checking and diagnostics."""

import importlib.util
import sys
from typing import Any, Optional


def _try_import(module_name: str, attr_name: Optional[str] = None) -> tuple[Any | None, bool]:
    """
    Try importing a module. Return (obj, True) or (None, False).
    If attr_name is given, return getattr(module, attr_name).

    Catches Exception broadly, not just ImportError — this probe's entire
    contract is "never crash the caller," and some packages fail at import
    time for reasons other than being uninstalled (e.g. sounddevice raises
    OSError when the Python package is present but the underlying PortAudio
    C library isn't — a genuinely different failure than "not installed,"
    but one that should degrade the same way: optional, not fatal).
    """
    try:
        module = __import__(module_name, fromlist=[""])
        if attr_name:
            return getattr(module, attr_name), True
        return module, True
    except Exception:
        return None, False


# ── Core / Audio ────────────────────────────────────────────────────────────

# faster-whisper WAS PROBED HERE and is deliberately gone. It is not an
# optional dependency any more -- it is not a dependency at all: the phone
# transcribes, the hub does not, and main.py's last reference went with the
# purge. Probing it kept a ghost alive: the status table listed it MISSING,
# which read as "you still need to install this" on every fresh machine --
# and on the Pi it would have pulled ctranslate2 for nothing. If it is
# still pip-installed somewhere, uninstall it; nothing here will notice.
#
# THE SAME SWEEP, FINISHED (2026-09-07). Four more probes went the same way,
# for the same reason -- each was assigned here and read nowhere:
#   pyrnnoise    denoising is GTCRN on the phone; the hub denoises nothing.
#   scipy        resampling was scipy.signal once; voice_security/audio_io.py
#                now does the one remaining resample in numpy, on purpose,
#                so enrolment and verification resample identically. Its
#                probe also surfaced SciPy's own NumPy-version complaint on
#                every start, about a package nothing used.
#   sounddevice  local-microphone mode; the hub takes audio from the phone.
#   piper        the hub does not speak; the phone does.
# If any of them is still pip-installed, uninstall it; nothing here notices.

# FIX: check numpy explicitly — it's so fundamental we want a clear error
np, _NUMPY = _try_import("numpy")
if not _NUMPY:
    print("[FATAL] numpy is required. pip install numpy")
    sys.exit(1)

sf, _SOUNDFILE = _try_import("soundfile")

# ── ML / ONNX ───────────────────────────────────────────────────────────────
sherpa_onnx, _SHERPA = _try_import("sherpa_onnx")

# ── NLP ─────────────────────────────────────────────────────────────────────
jellyfish, _JELLYFISH = _try_import("jellyfish")
if _JELLYFISH and jellyfish and not hasattr(jellyfish, "double_metaphone"):
        def _dm_shim(s):
            m = jellyfish.metaphone(s) or ""
            return (m, "")
        jellyfish.double_metaphone = _dm_shim
        print("[INFO] Shimmed jellyfish.double_metaphone (removed in jellyfish >= 1.0)")

# ── Auth / Crypto ───────────────────────────────────────────────────────────
bcrypt, _BCRYPT = _try_import("bcrypt")


# THE WEBSOCKETS VERSION BANNER IS GONE. Every process that imported this
# module printed "websockets >= 10.0 detected -- handler signature should
# be (websocket)": advice to a developer, acted on by nothing, on every
# start of the hub, the doctor and the tools.


# ── Diagnostics ──────────────────────────────────────────────────────────────
def print_status():
    """Print dependency status for debugging. Call from main.py --diagnostics.

    The packages ONLY this table asks about are probed HERE, at call time,
    not at import. They were module-level, so every import of this module
    -- audio_io, sv and pin_auth all do it -- loaded llama_cpp
    and onnxruntime (and websockets and cryptography) into the process
    just to set flags that nothing but this table ever read."""
    # The one optional model library left: Qwen2.5 writes a device's
    # description at registration (nlu/llm_describer.py). Without it
    # descriptions are typed.
    _, llama = _try_import("llama_cpp", "Llama")
    # REQUIRED, not optional: the MPNet encoder runs through ONNX Runtime and
    # tokenises with transformers (nlu/model_module.py). main.py exits at start
    # if either is missing, so the table below says so rather than "(VAD)" --
    # voice activity detection runs on the phone and never needed these here.
    # transformers is found, not imported: it is slow to import and prints.
    deps = [
        ("numpy", _NUMPY, "REQUIRED — pip install numpy"),
        ("onnxruntime (MPNet encoder)", _try_import("onnxruntime")[1],
         "REQUIRED — pip install onnxruntime"),
        ("transformers (tokeniser)",
         importlib.util.find_spec("transformers") is not None,
         "REQUIRED — pip install transformers"),
        ("sherpa-onnx (speaker check)", _SHERPA, "REQUIRED — pip install sherpa-onnx"),
        ("websockets", _try_import("websockets")[1], "REQUIRED — pip install websockets"),
        ("cryptography (Fernet)", _try_import("cryptography.fernet", "Fernet")[1],
         "REQUIRED — pip install cryptography"),
        ("bcrypt (PIN)", _BCRYPT, "REQUIRED — pip install bcrypt"),
        ("soundfile (OGG/FLAC audio)", _SOUNDFILE, "pip install soundfile"),
        ("jellyfish (phonetic match)", _JELLYFISH, "pip install jellyfish"),
        ("llama-cpp (device descriptions)", llama, "optional — pip install llama-cpp-python"),
    ]
    print("\n" + "=" * 60)
    print("Dependency Status")
    print("=" * 60)
    for name, ok, install_cmd in deps:
        status = "✔ OK" if ok else "✘ MISSING"
        print(f"  {status:<12} {name:<25} {install_cmd if not ok else ''}")
    print("=" * 60 + "\n")
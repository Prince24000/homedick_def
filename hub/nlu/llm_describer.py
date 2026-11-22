"""
llm_describer.py — Local LLM device metadata generator, 3-call pattern.

Exactly three LLM calls per device:
  1. Step   -> "What's a typical step size?"
  2. Range  -> "What's a typical operating range?"
  3. Desc   -> "Write 2-3 sentences describing this device for voice matching"

state_key is DERIVED from step+range, and has exactly TWO values:
  - range 0..1 with a step that cannot express a middle -> "on"  (binary)
  - everything else                                     -> "level" (numeric)

There is no third "temperature" key. It used to return one for a
temperature-shaped range, and it was wrong twice over: nothing anywhere
branches on the name -- build_command's clamping, stepping and deltas are
identical whichever field holds the number -- and device_manager's HA
translation writes EVERY domain's value into "level", climate included. So a
thermostat registered through this module stored state_key "temperature",
build_command looked up state["temperature"], found nothing, and returned
"flip". "Make it warmer" toggled the thermostat off.

No unit label is produced either. Only one consumer ever read the unit -- a
test for "binary" -- and state_key already answers that, identically,
everywhere.

IR appliances get their own single-call path (describe_ir_appliance, at the
bottom): no step, no range -- the hub cannot read an IR appliance's state,
only fire named codes at it, so the envelope questions would be theatre.

One backend, with model auto-discovery: llama.cpp (GGUF), scanning
GGUF_CANDIDATES. (A transformers backend used to sit beside it; generating
with transformers needs PyTorch, which this project deliberately does not
install, so it could never run.)
"""

import os
import re
import threading
from typing import Optional, Dict, Any, Tuple

from core.config import LLM_MODEL_PATH, MODELS_DIR

# ── Configuration ────────────────────────────────────────────────────────────
# The primary candidate (Qwen) comes from config.py, the project's single
# source of truth for model paths. The gemma entries are genuine fallbacks —
# not "the" model, just what to try if Qwen isn't present — so they stay local.
#
# The gemma paths are joined to MODELS_DIR rather than written as "models/...".
# A relative path resolves against the CURRENT WORKING DIRECTORY, so those two
# fallbacks silently stopped existing whenever the process started from
# anywhere but the project root -- under systemd, under cron, from a different
# shell. config.py resolves every other path against its own location for
# exactly that reason and says so; these two opted out of it.
GGUF_CANDIDATES = [
    (os.environ.get("LLM_DESCRIBER_GGUF", ""), os.environ.get("LLM_DESCRIBER_CHAT_FORMAT", "")),
    (LLM_MODEL_PATH, "qwen"),
    (os.path.join(str(MODELS_DIR), "gemma-3-1b-it-qat-q4_0.gguf"), "gemma"),
    (os.path.join(str(MODELS_DIR), "gemma-2-2b-it-q4_k_m.gguf"), "gemma"),
]
GGUF_CANDIDATES = [(p, f) for p, f in GGUF_CANDIDATES if p]

_model = None
_loaded_path: Optional[str] = None
# describe_* run in worker threads (asyncio.to_thread); two arriving
# together each saw _model None and each loaded a ~1 GB GGUF, which on a
# 4 GB Pi is an OOM. Loading, and generation on the one shared Llama
# object (not thread-safe), are serialised.
_lock = threading.RLock()


# ═══════════════════════════════════════════════════════════════
# PROMPTS
# ═══════════════════════════════════════════════════════════════

_STEP_PROMPT = (
    "What is the typical step size for one small adjustment on this "
    "device, in its natural unit? Answer with a single number and "
    "nothing else.\n\n"
    "Examples:\n"
    "  Thermostat -> 2\n"
    "  Dimmable lamp -> 10\n"
    "  Speaker volume -> 5\n"
    "  On/off lock -> 0\n\n"
    "Device: {device_name}\n"
    "Hint: {user_hint}\n\n"
    "Step:"
)

_RANGE_PROMPT = (
    "What is the typical operating range for this device, as two numbers "
    "min and max separated by a comma? Use the device's natural units "
    "(Celsius for temperature, percent for brightness or volume, 0 and 1 "
    "for binary on/off). Answer with two numbers separated by a comma "
    "and nothing else.\n\n"
    "Examples:\n"
    "  Thermostat -> 15,35\n"
    "  Air conditioner -> 16,30\n"
    "  Dimmable lamp -> 0,100\n"
    "  Lock -> 0,1\n\n"
    "Device: {device_name}\n"
    "Hint: {user_hint}\n\n"
    "Range:"
)

_DESCRIPTION_PROMPT = (
    "Describe a {device_name} in one sentence — what it physically does. "
    "Then in a second sentence, give the exact words someone would say to "
    "control it (like \"turn on the {device_name}\", \"make it warmer\", "
    "etc). Prose only.\n\n"
    "Device: {device_name}\n"
    "Hint: {user_hint}\n\n"
    "Answer:"
)

# WHY IR GETS ITS OWN PROMPT rather than riding _DESCRIPTION_PROMPT: a
# normal device description describes ONE capability ("turn on the lamp",
# "make it warmer"). An IR appliance is a bag of NAMED ACTIONS -- power,
# play, pause, cool_22 -- and the description is the only input semantic
# matching ever sees, so those names must appear in it, in the words a
# person would actually say. "Pause the telly" can only find a device
# whose description talks about pausing. The generic prompt knows nothing
# about buttons and would produce a description that matches "turn on the
# tv" and misses every transport verb.
_IR_PROMPT = (
    "Describe a {device_name} controlled by an infrared remote, in one "
    "sentence -- what it physically is and does. Then in one or two more "
    "sentences, give the exact spoken phrases someone would use for its "
    "buttons, using these button names naturally: {buttons}. "
    "(For example a 'pause' button means someone says \"pause the "
    "{device_name}\"; a 'cool_22' button means \"set the {device_name} to "
    "22 degrees\".) Prose only, no lists.\n\n"
    "Device: {device_name}\n"
    "Buttons: {buttons}\n"
    "Hint: {user_hint}\n\n"
    "Answer:"
)


# ═══════════════════════════════════════════════════════════════
# BACKENDS
# ═══════════════════════════════════════════════════════════════

def _load_llamacpp(gguf_path: Optional[str] = None) -> None:
    global _model, _loaded_path

    if gguf_path:
        name = os.path.basename(gguf_path).lower()
        if "qwen" in name:
            fmt = "qwen"
        elif "gemma" in name:
            fmt = "gemma"
        elif "llama" in name:
            fmt = "llama-3"
        else:
            fmt = None
        candidates = [(gguf_path, fmt)]
    else:
        candidates = GGUF_CANDIDATES

    chosen = None
    for path, fmt in candidates:
        if os.path.exists(path):
            chosen = (path, fmt)
            break

    if chosen is None:
        searched = "\n  ".join(p for p, _ in candidates) if candidates else "(no candidates)"
        raise FileNotFoundError(
            f"No GGUF model found. Searched:\n  {searched}\n"
            f"Set LLM_DESCRIBER_GGUF to override, or download one of the above."
        )

    path, fmt = chosen
    try:
        from llama_cpp import Llama
    except ImportError as e:
        raise ImportError("Run: pip install llama-cpp-python") from e

    print(f"[llm_describer] Loading (llama.cpp): {path} [chat_format={fmt or 'auto'}]")
    try:
        n_threads = int(os.environ.get("LLM_THREADS", "4"))
    except ValueError:
        n_threads = 4
    kwargs = dict(
        model_path=path,
        n_ctx=2048,
        n_threads=n_threads,
        verbose=False,
    )
    if fmt:
        kwargs["chat_format"] = fmt
    _model = Llama(**kwargs)
    _loaded_path = path
    print("[llm_describer] Ready.")


def _generate(prompt: str, max_tokens: int = 200) -> str:
    if _model is None:
        raise RuntimeError(
            "llm_describer._generate called before load_model() succeeded. "
            "Every public entry point loads first; reaching here means one was "
            "added that does not.")
    with _lock:
        response = _model.create_chat_completion(
            messages=[{"role": "user", "content": prompt}],
            max_tokens=max_tokens, temperature=0.3, top_p=0.9,
            # Qwen's own docs specifically recommend presence_penalty=1.5 for
            # QUANTIZED models -- exactly our case (Q4_K_M GGUF) -- to suppress
            # the repetitive, generic-sounding output that quantization makes
            # more likely. This directly targets the "richness" problem, not a
            # cosmetic tuning knob.
            presence_penalty=1.5,
        )
    return response["choices"][0]["message"]["content"]


# ═══════════════════════════════════════════════════════════════
# PER-FIELD GENERATORS
# ═══════════════════════════════════════════════════════════════

def _gen_step(device_name: str, user_hint: str) -> Optional[float]:
    prompt = _STEP_PROMPT.format(
        device_name=device_name, user_hint=user_hint or "(none provided)"
    )
    raw = _generate(prompt, max_tokens=30).strip()
    m = re.search(r'-?\d+(?:\.\d+)?', raw)
    if not m or float(m.group(0)) < 0:
        return None
    return float(m.group(0))


def _gen_range(device_name: str, user_hint: str) -> Optional[Tuple[float, float]]:
    prompt = _RANGE_PROMPT.format(
        device_name=device_name, user_hint=user_hint or "(none provided)"
    )
    raw = _generate(prompt, max_tokens=40).strip()
    # A minus sign straight after a digit is a range dash, not a sign:
    # "16-30" used to parse as 16 and -30, fail hi > lo, and register an
    # air conditioner at the 0..100 default.
    nums = re.findall(r'(?<![\d.])-?\d+(?:\.\d+)?', raw)
    if len(nums) < 2:
        return None
    lo, hi = float(nums[0]), float(nums[1])
    if hi <= lo:
        return None
    return (lo, hi)


def _clean_prose(raw: str) -> str:
    """The model's answer without code fences, an "Answer:" label or
    wrapping quotes; "" when it answered with JSON instead of prose."""
    raw = re.sub(r'^```\w*\s*', '', raw.strip())
    raw = re.sub(r'\s*```$', '', raw).strip()
    raw = re.sub(r'^(description|answer)\s*[:\-]\s*', '', raw, flags=re.IGNORECASE).strip()
    raw = raw.strip('"').strip("'").strip()
    return "" if raw.startswith(("{", "[")) else raw


def _cap_sentences(raw: str) -> str:
    """At most 400 characters, cut after the last sentence end in them."""
    if len(raw) > 400:
        cut = raw[:400]
        last = max(cut.rfind("."), cut.rfind("!"), cut.rfind("?"))
        raw = cut[:last + 1] if last > 0 else cut
    return raw


def _gen_description(device_name: str, user_hint: str) -> str:
    prompt = _DESCRIPTION_PROMPT.format(
        device_name=device_name,
        user_hint=user_hint or "(none provided)",
    )
    raw = _clean_prose(_generate(prompt, max_tokens=200))

    if len(raw) > 400:
        cut = raw[:400]
        last = cut.rfind(".")
        raw = (cut[:last + 1] if last > 100 else cut).strip()

    if len(raw) < 20:
        return ""

    return raw


# ═══════════════════════════════════════════════════════════════
# UNIT / STATE-KEY DERIVATION
# ═══════════════════════════════════════════════════════════════

def _derive_state_key(step: float, range_min: float, range_max: float) -> str:
    """
    Return the state_key from the numeric range shape: "on" or "level".

    This returned (unit_label, state_key). The unit half is gone: nothing read
    it except a "binary" test that state_key answers, and the labels this file
    produced ("celsius"/"fahrenheit"/"percent") did not even match the ones
    device_manager produced for the same device ("degrees"/"percent").
    """
    span = range_max - range_min

    # Binary: a 0..1 range where the step cannot express a middle.
    #
    # Two changes. The module docstring says "step <= 0.5" and the code said
    # 0.05 -- a factor of ten apart, and the docstring is the reasonable one.
    # And a step >= the span is equally binary: the prompt asks for "0" on a
    # lock, but a model answering "1" for a 0..1 device described exactly the
    # same two-state thing and fell through to the generic percent branch --
    # registering a LOCK as 0-1 percent with state_key "level", which is what
    # let "set the front door to 50" mean anything at all.
    if range_min == 0 and range_max <= 1.0 and (step <= 0.5 or step >= span):
        return "on"

    # Everything else -- celsius-, fahrenheit- and freezer-shaped ranges
    # included -- is a numeric level. (Separate branches for those shapes
    # used to sit here; every one of them returned "level".)
    return "level"


# ═══════════════════════════════════════════════════════════════
# PUBLIC API
# ═══════════════════════════════════════════════════════════════

def load_model(model_path: Optional[str] = None) -> None:
    """Load the LLM. Safe to call multiple times — reuses if already loaded."""
    with _lock:
        if _model is not None and model_path in (None, _loaded_path):
            return
        try:
            _load_llamacpp(model_path)
        except (FileNotFoundError, ImportError, OSError, RuntimeError,
                ValueError) as e:
            # Do NOT silently fall back to downloading ~3GB from HuggingFace.
            # The offline-first design means the local GGUF is the model; if it
            # isn't there, say so with the path, don't reach for the network.
            raise FileNotFoundError(
                "Local Qwen GGUF not found and llama.cpp load failed "
                f"({type(e).__name__}: {e}).\n"
                f"Expected one of:\n  " +
                "\n  ".join(p for p, _ in GGUF_CANDIDATES) +
                "\nDownload the GGUF into your models/ folder, or set "
                "LLM_DESCRIBER_GGUF."
            ) from e


def describe_device(
    device_name: str,
    user_hint: str = "",
) -> Dict[str, Any]:
    """
    Generate device metadata via exactly 3 LLM calls.

    Returns:
        {
            "description": "...",
            "step": 2,
            "range_min": 16,
            "range_max": 30,
            "state_key": "level",
        }
    """
    load_model()

    step = _gen_step(device_name, user_hint)
    if step is None:
        step = 10.0

    rng = _gen_range(device_name, user_hint)
    if rng is None:
        range_min, range_max = 0.0, 100.0
    else:
        range_min, range_max = rng

    description = _gen_description(device_name, user_hint)
    if not description:
        description = user_hint or device_name

    return {
        "description": description,
        "step": step,
        "range_min": range_min,
        "range_max": range_max,
        "state_key": _derive_state_key(step, range_min, range_max),
    }


# ═══════════════════════════════════════════════════════════════
# SCENE DESCRIPTIONS
# ═══════════════════════════════════════════════════════════════

_SCENE_PROMPT = (
    "Describe a smart-home scene in one sentence — what it sets up in the "
    "room. Then in a second sentence, give the exact phrases someone would "
    "say to trigger it (like \"{scene_name}\", and natural variations). "
    "Prose only.\n\n"
    "Scene: {scene_name}\n"
    "Purpose: {hint}\n"
    "What it does: {actions}\n\n"
    "Answer:"
)


def _summarize_actions(actions) -> str:
    parts = []
    for a in actions or []:
        did = a.get("device_id", "device")
        st = a.get("state") or {}
        bits = []
        if "on" in st:
            bits.append("on" if st["on"] else "off")
        for k, v in st.items():
            if k != "on":
                bits.append(f"{k} {v}")
        parts.append(f"{did} ({', '.join(bits)})" if bits else did)
    return "; ".join(parts) if parts else "(no actions)"


def describe_scene(scene_name: str, hint: str = "", actions=None) -> str:
    """Generate a rich semantic description for a scene."""
    load_model()
    action_summary = _summarize_actions(actions)
    fallback = f"{scene_name}. Sets: {action_summary}."

    try:
        prompt = _SCENE_PROMPT.format(
            scene_name=scene_name,
            hint=hint or "(none provided)",
            actions=action_summary,
        )
        raw = _cap_sentences(_clean_prose(_generate(prompt, max_tokens=200)))
        if not raw:
            return fallback
        if scene_name.lower() not in raw.lower():
            raw = f"{scene_name}. {raw}"
        return raw
    except Exception:
        return fallback


# ═══════════════════════════════════════════════════════════════
# IR APPLIANCE DESCRIPTIONS
# ═══════════════════════════════════════════════════════════════
#
# The prompt itself (_IR_PROMPT) lives up in the PROMPTS section with its
# siblings, and the reasoning for a separate one is written there: an IR
# appliance is a bag of NAMED ACTIONS, and the description is the only
# input semantic matching ever sees, so the button names must appear in it
# in the words a person would say.
#
# NO STEP, NO RANGE, NO 3-CALL PATTERN. An IR appliance has no envelope --
# the hub cannot read its state, only fire named codes at it -- so the
# step/range questions would be theatre. One call, the description.

def ir_fallback_description(device_name: str, buttons, user_hint: str = "") -> str:
    """The no-model description, and NOT an afterthought: on a hub with no
    LLM this IS the semantic surface, so it must still carry every button
    name -- literal words are exactly what the TF-IDF fallback matcher can
    use. Deliberately a public function: the learn flow uses it as the
    floor whether or not a model improves on it."""
    names = ", ".join(str(b).replace("_", " ") for b in buttons)
    hint = f" {user_hint.strip().rstrip('.')}." if user_hint.strip() else ""
    return (f"{device_name}, controlled by infrared remote.{hint} "
            f"Buttons: {names}.")


def describe_ir_appliance(device_name: str, buttons,
                          user_hint: str = "") -> str:
    """Generate a rich description for an IR appliance. ONE LLM call.

    Returns the fallback (which already names every button) rather than ""
    on any failure -- the caller stores whatever comes back, and a
    description that lists the buttons plainly beats an empty string that
    makes the device deaf to everything but its exact name.
    """
    fallback = ir_fallback_description(device_name, buttons, user_hint)
    try:
        load_model()
    except Exception:
        return fallback

    names = ", ".join(str(b).replace("_", " ") for b in buttons)
    try:
        prompt = _IR_PROMPT.format(
            device_name=device_name,
            buttons=names or "(none yet)",
            user_hint=user_hint or "(none provided)",
        )
        raw = _cap_sentences(_clean_prose(_generate(prompt, max_tokens=200)))
        if not raw:
            return fallback

        # THE BUTTONS MUST SURVIVE THE MODEL. A description that reads
        # beautifully and never says "pause" makes "pause the tv" match
        # nothing -- the exact failure this function exists to prevent. Any
        # button the prose dropped is appended plainly; belt and braces
        # beats trusting a quantized 1.5B model to follow instructions.
        lower = raw.lower()
        missing = [str(b).replace("_", " ") for b in buttons
                   if str(b).replace("_", " ").lower() not in lower]
        if missing:
            raw = f"{raw.rstrip('.')}. Also: {', '.join(missing)}."
        if device_name.lower() not in raw.lower():
            raw = f"{device_name}. {raw}"
        return raw
    except Exception:
        return fallback
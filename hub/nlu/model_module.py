"""
model_module.py — MPNet embedding + intent classification for Pi 4B.

ONNX-only. There is no reason to carry a PyTorch fallback here (the project
does not install PyTorch at all): onnxruntime runs
identically on the Pi and on a Windows dev box, and MPNet (110M params) needs
no quantization tradeoff either way. A missing ONNX export is a setup error,
not a hardware constraint, so load_model() fails loudly with the export
command rather than silently falling back to a heavier backend.

Thread-safety: load_model() is serialised by _model_lock.
Inference (embed/embed_batch) serialises only tokenisation (_tokenizer_lock);
the ONNX Runtime session is thread-safe and runs unlocked.

INDEX PATH
----------
np.savez() silently appends ".npz" to any path that lacks it. Everything that
WRITES the index therefore went to "<path>.npz" while everything that READS or
os.path.exists()-checks it looked at "<path>" — so the index was rebuilt from
scratch on every startup and IntentClassifier then raised FileNotFoundError on
a file that had just been written. main.py catches that as a warning and leaves
self.intent_classifier = None, which silently disables the dismiss/announce
rung entirely. resolve_index_path() is the single normalizer; readers, writers
and existence checks all go through it. Callers outside this module (main.py's
`intent_index_path.exists()` check) should use it too.
"""

import hashlib
import json
import os
import re
import tempfile
import threading
import time
import zipfile
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple
import numpy as np

from core.config import INTENT_INDEX_PATH, INTENT_MIN_SCORE, INTENT_MIN_MARGIN
from core.hv_common import event, times_add
from nlu.intent_prototypes import INTENT_PROTOTYPES
from nlu.lexicon import fold_apostrophes
from core.hv_common import event, times_add

# The SAME numbers main.py passes. They were 0.35/0.05 here while main.py
# supplied 0.55/0.08 from config on every call -- so these defaults were never
# the values in force, and anyone reading this file to find out how strict the
# classifier is got the wrong answer.
INTENT_THRESHOLD = INTENT_MIN_SCORE
INTENT_MARGIN_FLOOR = INTENT_MIN_MARGIN

_EMBED_DIM: int = 768
_embed_fn: Optional[Callable[[str], np.ndarray]] = None
_embed_batch_fn: Optional[Callable[[List[str]], np.ndarray]] = None
_backend: Optional[str] = None
_loaded_dir: Optional[str] = None
_loaded_onnx: Optional[str] = None
_model_lock = threading.Lock()
# A HuggingFace FAST tokenizer is not safe to call from two threads at once:
# padding/truncation are set on the shared Rust object per call, and a
# concurrent call fails with "RuntimeError: Already borrowed". embed() IS
# reached concurrently -- app_nav's semantic pass runs on the WebSocket event
# loop while a command embeds in a worker thread -- so tokenisation is
# serialised. The ONNX session itself is thread-safe and runs unlocked.
_tokenizer_lock = threading.Lock()


def resolve_index_path(path: str = INTENT_INDEX_PATH) -> str:
    """The one true on-disk path for the intent index.

    np.savez appends ".npz" if it isn't already there, so this is what the
    file is ACTUALLY called regardless of what config says. Use this for
    reading, writing, and existence checks alike — never the raw config value.
    """
    p = str(path)
    return p if p.endswith(".npz") else p + ".npz"


# ── Model Loading ──────────────────────────────────────────────────────────

def _find_model_files(model_dir: str) -> Dict[str, str]:
    d = Path(model_dir)
    if not d.is_dir():
        raise FileNotFoundError(f"Model directory not found: {d}")

    # Only scan the top-level dir; subdirs are different model variants
    files = sorted([f for f in d.iterdir() if f.is_file()], key=lambda p: p.name.lower())

    config = next((str(f) for f in files if f.name.lower() == "config.json"), None)
    if not config:
        raise FileNotFoundError(f"config.json not found in {d}")

    onnx = next((str(f) for f in files if f.name.lower().endswith(".onnx")), None)
    if not onnx:
        raise FileNotFoundError(
            f"No .onnx file found in {d}. This project is ONNX-only for MPNet "
            f"(see module docstring). Export one first:\n"
            f"  pip install \"optimum[onnxruntime]\"\n"
            f"  optimum-cli export onnx -m sentence-transformers/all-mpnet-base-v2 "
            f"--task feature-extraction --library-name sentence_transformers {d}/"
        )

    return {"onnx": onnx, "config": config}


def _has_tokenizer(model_dir: str) -> bool:
    d = Path(model_dir)
    return (d / "tokenizer.json").exists() or (
        (d / "tokenizer_config.json").exists() and (d / "vocab.txt").exists()
    )


# NOTE: _load_onnx expects _model_lock to be held by the caller.
def _load_onnx(model_path: str) -> None:
    global _embed_fn, _embed_batch_fn, _backend, _EMBED_DIM

    import onnxruntime as ort
    from transformers import AutoTokenizer

    model_dir = os.path.dirname(model_path)
    if not _has_tokenizer(model_dir):
        raise FileNotFoundError(f"Tokenizer missing in {model_dir}")

    tokenizer = AutoTokenizer.from_pretrained(model_dir)
    sess_options = ort.SessionOptions()
    sess_options.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
    session = ort.InferenceSession(model_path, sess_options, providers=["CPUExecutionProvider"])

    output_name = session.get_outputs()[0].name
    input_names = {inp.name for inp in session.get_inputs()}

    def _run(encoded: dict) -> np.ndarray:
        inputs = {name: encoded[name] for name in input_names if name in encoded}
        if "token_type_ids" in input_names and "token_type_ids" not in encoded:
            inputs["token_type_ids"] = np.zeros(encoded["input_ids"].shape, dtype=np.int64)
        return session.run([output_name], inputs)[0]

    def _pool(emb: np.ndarray, mask: np.ndarray) -> np.ndarray:
        m = np.expand_dims(mask, -1).astype(np.float32)
        summed = np.sum(emb * m, axis=1)
        return summed / np.clip(np.sum(m, axis=1), 1e-9, None)

    def _norm(v: np.ndarray) -> np.ndarray:
        n = np.linalg.norm(v, axis=-1, keepdims=True)
        if np.any(n < 1e-9):
            raise ValueError("Zero vector from embedder")
        return (v / n).astype(np.float32)

    def _encode(texts: List[str]) -> dict:
        with _tokenizer_lock:
            return tokenizer(texts, padding=True, truncation=True, max_length=512,
                             return_tensors="np")

    def embed_single(text: str) -> np.ndarray:
        enc = _encode([text])
        return _norm(_pool(_run(enc), enc["attention_mask"]))[0]

    def embed_batch(texts: List[str]) -> np.ndarray:
        if not texts:
            return np.zeros((0, _EMBED_DIM), dtype=np.float32)
        enc = _encode(texts)
        return _norm(_pool(_run(enc), enc["attention_mask"]))

    # Take the embedding width from the model rather than trusting the 768
    # constant — an index built at a different width would otherwise fail
    # later, inside a matmul, instead of here where the cause is obvious.
    #
    # NOT swallowed. This probe is also the first and only proof that the
    # session actually runs: if it raises, embed() is broken for every caller,
    # and `pass` left _EMBED_DIM at a guessed 768 while load_model went on to
    # log "Ready". A model that cannot embed is not ready.
    try:
        _EMBED_DIM = int(embed_single("dimension probe").shape[0])
    except Exception as e:
        raise RuntimeError(
            f"The ONNX session loaded but could not embed a test string: "
            f"{type(e).__name__}: {e}. The export is likely incomplete or "
            f"mismatched with its tokenizer.") from e

    _embed_fn = embed_single
    _embed_batch_fn = embed_batch
    _backend = "onnx"


def load_model(model_dir: str) -> None:
    """Load the MPNet ONNX export. Loading the SAME directory twice is a no-op.

    It used to raise on any second call. Switching models mid-process really
    is unsupported -- the intent index is built at one width against one
    embedder -- but re-requesting the model already loaded is not switching,
    and llm_describer.load_model (the sibling function, same name) is
    explicitly documented as "safe to call multiple times". Two functions
    called load_model with opposite behaviour is a trap for whoever calls the
    second one.
    """
    global _loaded_dir, _loaded_onnx
    with _model_lock:
        resolved = os.path.abspath(model_dir)
        if _embed_fn is not None:
            if resolved == _loaded_dir:
                return
            raise RuntimeError(
                f"A model is already loaded from {_loaded_dir}. Switching to "
                f"{resolved} in the same process is unsupported: the intent "
                f"index is built for one embedder at one width. Use a fresh "
                f"process.")

        files = _find_model_files(model_dir)  # raises with export instructions if onnx missing
        event("MODEL", f"Loading ONNX: {files['onnx']}")
        _load_onnx(files["onnx"])
        _loaded_dir = resolved
        _loaded_onnx = files["onnx"]
        event("MODEL", f"Ready: {_backend} (dim={_EMBED_DIM})")


#: ONE INFERENCE AT A TIME. Flows are serialised by the shell lock, but
#: the spoken-cancel check (nlu/spoken_cancel.py) runs beside a working
#: flow, and a tokenizer is not guaranteed safe to share between threads.
#: A cancel check waits ~0.1 s for the flow's embed to finish; nothing is
#: ever computed on the event loop.
_infer_lock = threading.Lock()


#: Who has the model right now: "<thread> (<role>, n sentences)". Read by
#: whoever had to wait, so a slow line can say what it waited BEHIND.
_infer_holder = ""


def _timed_infer(run, role: str, n: int):
    """One turn at the model, timed into the command that asked (see
    hv_common.timed): waiting its turn, or in the model itself."""
    global _infer_holder
    asked = time.perf_counter()
    ahead = _infer_holder
    with _infer_lock:
        began = time.perf_counter()
        _infer_holder = f"{threading.current_thread().name} ({role}, {n})"
        try:
            out = run()
        finally:
            _infer_holder = ""
    done = time.perf_counter()
    times_add("model_wait", began - asked)
    times_add("model_run", done - began, n)
    if done - asked > 1.5:
        event("EMBED", f"slow: {n} sentence(s) as {role} -- waited "
                       f"{began - asked:.1f}s for the model"
                       + (f" behind {ahead}" if ahead and began - asked > 0.5 else "")
                       + f", then {done - began:.1f}s in it")
    return out


def warm() -> float:
    """One short sentence through the model, result thrown away, so its
    memory is not left untouched long enough for the system to move it out
    to disk (her hub, 2026-10-05: 5.6 s for one sentence after a quiet
    spell, about 0.6 s when used again soon after). Returns the seconds it
    took, or -1.0 when nothing was run: no model loaded, or the model is in
    use -- which is as warm as it gets, and a beat must never make a
    command wait its turn."""
    global _infer_holder
    if _embed_fn is None or not _infer_lock.acquire(blocking=False):
        return -1.0
    try:
        _infer_holder = "the warm-up pass"
        began = time.perf_counter()
        _embed_fn("keep the model warm")
        return time.perf_counter() - began
    finally:
        _infer_holder = ""
        _infer_lock.release()


def embed(text: str) -> np.ndarray:
    if _embed_fn is None:
        raise RuntimeError("Model not loaded. Call load_model() first.")
    return _timed_infer(lambda: _embed_fn(text), "sentence", 1)


def embed_batch(texts: List[str]) -> np.ndarray:
    if _embed_batch_fn is None:
        raise RuntimeError("Model not loaded. Call load_model() first.")
    return _timed_infer(lambda: _embed_batch_fn(texts), "batch", len(texts))


def embedding_dim() -> int:
    """The width this model actually emits, probed at load.

    Public because other components size themselves against it -- SceneManager
    validated stored embeddings against a hardcoded 768 while this module went
    to real trouble NOT to trust that constant. Anything checking a stored
    vector's length should ask here.
    """
    if _embed_fn is None:
        raise RuntimeError("Model not loaded. Call load_model() first.")
    return _EMBED_DIM


def get_backend() -> str:
    if _backend is None:
        raise RuntimeError("Model not loaded. Call load_model() first.")
    return _backend


# ── Intent Classification ──────────────────────────────────────────────────

_CONTRACTIONS = {
    "it's": "it is", "what's": "what is", "where's": "where is",
    "how's": "how is", "who's": "who is", "when's": "when is",
    "that's": "that is", "there's": "there is", "let's": "let us",
    "won't": "will not", "can't": "cannot",
    **{a + "n't": a + " not" for a in (
        "do", "does", "did", "would", "could", "should", "is", "are", "was",
        "were", "have", "has", "had")},
}
_CONTRACTION_RE = re.compile(
    r"\b(" + "|".join(re.escape(c) for c in _CONTRACTIONS) + r")\b")


def _normalize(text: str) -> str:
    """Lowercase, expand contractions, drop apostrophes, squeeze whitespace.

    The curly-to-straight pass comes FIRST and is the point of it. Every
    pattern in _CONTRACTIONS is written with a straight apostrophe, while
    speech recognisers emit the typographic one (U+2019). So "it's freezing"
    typed into intent_prototypes.py normalised to "it is freezing", and the
    same sentence arriving from the phone as "it\u2019s freezing" fell past
    every pattern, had its apostrophe deleted, and became "its freezing" --
    a different string, a different embedding, matched against a prototype it
    should have been identical to. Verified.
    """
    t = fold_apostrophes(text.lower().strip().replace("-", " "), "'")
    t = _CONTRACTION_RE.sub(lambda m: _CONTRACTIONS[m.group(1)], t)
    return re.sub(r"\s+", " ", t.replace("'", "")).strip()


def _model_fingerprint() -> str:
    """Which export produced the vectors: file name and size of the loaded
    .onnx. The index is only valid for the embedder that built it, and a
    swapped export at the SAME width (an fp32 file replaced by an int8 one)
    passed the dimension check and kept serving the old model's vectors."""
    if not _loaded_onnx:
        return ""
    try:
        size = os.path.getsize(_loaded_onnx)
    except OSError:
        size = -1
    return f"{os.path.basename(_loaded_onnx)}:{size}"


def _prototypes_hash() -> str:
    """Stable hash of the current INTENT_PROTOTYPES dict and the model that
    embeds them."""
    return hashlib.sha256(
        (json.dumps(INTENT_PROTOTYPES, sort_keys=True)
         + "\0" + _model_fingerprint()).encode()
    ).hexdigest()[:16]


def build_intent_index(output_path: str = INTENT_INDEX_PATH) -> str:
    """Embed every prototype and write the index. Returns the ACTUAL path
    written (normalized through resolve_index_path), so callers never have to
    guess whether np.savez added an extension."""
    if _embed_batch_fn is None:
        raise RuntimeError("Model not loaded. Call load_model() first.")

    texts: List[str] = []
    intents: List[str] = []
    for intent, utterances in INTENT_PROTOTYPES.items():
        for u in utterances:
            texts.append(_normalize(u))
            intents.append(intent)

    if not texts:
        # An empty index would make classify() crash on argmax of an empty
        # array, several layers away from the actual cause.
        raise ValueError(
            "INTENT_PROTOTYPES is empty — nothing to embed. Add at least one "
            "intent with one utterance to intent_prototypes.py."
        )

    event("INTENT", f"Embedding {len(texts)} prototypes...")
    vecs = embed_batch(texts)

    path = resolve_index_path(output_path)
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    # Written beside the target and renamed into place: np.savez writes in
    # place, so a power cut mid-write (a Pi on a wall plug) left a truncated
    # zip at the real path that every later start failed to open.
    # A UNIQUE temp name: ws_server and a terminal main.py can rebuild at
    # the same start-up, and one fixed name let each replace or delete the
    # other's half-written file.
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path) or ".",
                               prefix=os.path.basename(path) + ".", suffix=".tmp.npz")
    os.close(fd)
    os.chmod(tmp, 0o644)   # mkstemp makes it 0600; the other process must read it
    try:
        np.savez(
            tmp,
            vecs=vecs.astype(np.float32),
            intents=np.array(intents),
            texts=np.array(texts),           # fixed-width unicode, no pickle needed
            proto_hash=_prototypes_hash(),
        )
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)
    event("INTENT", f"Index saved: {path}")
    return path


def intent_index_exists(path: str = INTENT_INDEX_PATH) -> bool:
    """Existence check that agrees with where build_intent_index actually
    writes. main.py should call this instead of Path(INTENT_INDEX_PATH).exists()."""
    return os.path.exists(resolve_index_path(path))


class IntentClassifier:
    def __init__(self, index_path: str = INTENT_INDEX_PATH):
        if _embed_fn is None:
            raise RuntimeError("Model not loaded. Call load_model() first.")

        path = resolve_index_path(index_path)
        if not os.path.exists(path):
            raise FileNotFoundError(
                f"Intent index not found: {path}. "
                f"Build it first: from nlu.model_module import build_intent_index; build_intent_index()"
            )

        # Read under a context manager. np.load on an .npz returns a LAZY
        # NpzFile that holds the file handle open until closed — the previous
        # code rebuilt the index while still holding that handle on the very
        # same path, which works on Linux (unlink semantics) but raises
        # PermissionError on Windows, and leaked a handle on both. Decide
        # staleness first, close, THEN rebuild.
        stale = True
        loaded = None
        try:
            with np.load(path, allow_pickle=False) as data:
                # A missing proto_hash means an index from an older build that
                # predates hashing — treat that as stale too. The old condition
                # (`"proto_hash" in data.files and ...`) let those through unchecked.
                if "proto_hash" in data.files:
                    stale = str(data["proto_hash"].item()) != _prototypes_hash()
                if not stale:
                    loaded = self._read(data)
        except (OSError, ValueError, KeyError, zipfile.BadZipFile) as e:
            # A truncated or corrupt file is a cache miss, not a reason to
            # run the whole session without the classifier.
            event("INTENT", f"Intent index at {path} is unreadable "
                  f"({type(e).__name__}: {e})")
            stale, loaded = True, None

        # If prototypes changed since this index was built, it's silently
        # wrong (e.g. a new seed phrase like "play the next track" added to
        # intent_prototypes.py isn't in the cached vectors at all). Rebuild
        # automatically rather than warn-and-continue-stale — the whole class
        # of "the file changed but the cache didn't" bug that's bitten this
        # project before should not require the user to notice a log line and
        # manually delete a cache file.
        if stale:
            event("INTENT", "Intent index stale or unversioned — rebuilding "
                  "from current prototypes...")
            path = build_intent_index(index_path)
            with np.load(path, allow_pickle=False) as data:
                loaded = self._read(data)

        self.vecs, self.intents, self.texts = loaded

        if self.vecs.ndim != 2 or self.vecs.shape[0] == 0:
            raise ValueError(f"Intent index at {path} contains no vectors.")
        if self.vecs.shape[0] != len(self.intents):
            raise ValueError(
                f"Intent index at {path} is corrupt: {self.vecs.shape[0]} vectors "
                f"but {len(self.intents)} labels."
            )
        if self.vecs.shape[1] != _EMBED_DIM:
            raise ValueError(
                f"Intent index at {path} was built at dim {self.vecs.shape[1]}, "
                f"but the loaded model emits dim {_EMBED_DIM}. Delete the index "
                f"and rebuild."
            )

    @staticmethod
    def _read(data) -> Tuple[np.ndarray, np.ndarray, Optional[np.ndarray]]:
        """Materialise the arrays while the NpzFile is still open. NpzFile is
        lazy, so this must happen inside the `with`, not after it."""
        return (
            np.asarray(data["vecs"]),
            np.asarray(data["intents"]),
            np.asarray(data["texts"]) if "texts" in data.files else None,
        )

    def classify(self, text: str, threshold: float = INTENT_THRESHOLD,
                 margin_floor: float = INTENT_MARGIN_FLOOR) -> Dict[str, Any]:
        q = embed(_normalize(text))
        sims = self.vecs @ q

        best_idx = int(np.argmax(sims))
        intent = self.intents[best_idx].item()
        score = float(sims[best_idx])

        # --- Top score from a different intent ---
        # No competitor at all (a one-intent index) is not a zero score:
        # the margin is then the score itself.
        others = sims[self.intents != intent]
        has_competition = others.size > 0
        margin = (score - float(others.max())) if has_competition else score
        seed = self.texts[best_idx].item() if self.texts is not None else None

        if score < threshold:
            intent, seed = None, None
        elif has_competition and margin < margin_floor:
            intent = None
        return {
            "intent": intent,
            "score": round(score, 3),
            "margin": round(margin, 3),
            "nearest_seed": seed,
        }
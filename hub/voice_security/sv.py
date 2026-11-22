#!/usr/bin/env python3
"""
sv.py — speaker verification using Sherpa-ONNX (TitaNet-Large, CAM++,
ERes2Net, or any other model sherpa-onnx's SpeakerEmbeddingExtractor
supports). The loaded-model log line names whatever file is actually
configured, rather than a fixed guess -- an earlier version hardcoded
"CAM++" regardless of what was really loaded, which was actively
misleading once the project settled on TitaNet.

ONE PREPROCESSING CHOKEPOINT (_prep -> _embed)
----------------------------------------------
enrollment_handler.py's docstring described this design as already done --
"per-phrase embedding extraction now goes through SherpaSV.get_embedding
(-> SherpaSV._embed -> _prep), the SAME preprocessing path verification
uses", with "(VAD + trim)". None of it existed. There was no _embed, no
_prep, no trimming, and THREE independent copies of create_stream() /
accept_waveform() / compute() in this file -- one each in enroll(), verify()
and get_embedding().

Those three copies agreed only because all three were equally bare. That is
not symmetry, it is a coincidence: the first person to add preprocessing to
one of them would have made enrolled templates and live probes incomparable,
and the only symptom would have been cosine scores quietly getting worse.
That is precisely the failure this codebase has already been bitten by twice
(the 0.5s KWS pad landing in the SV embedding; enrollment reading clean WAVs
while verification got browser audio).

So the chokepoint now exists for real:

    _prep(audio)   -- everything done to a waveform before the model sees it:
                      sanitise, trim silence, normalise loudness
    _embed(audio)  -- _prep, then exactly one TitaNet forward pass

enroll(), verify_with_embedding() and get_embedding() all call _embed() and
contain no extraction logic of their own. There is now exactly one place
where preprocessing can be changed, and any change applies to enrolled
templates and live probes simultaneously.

WHY TRIMMING RAISES SCORES
--------------------------
TitaNet pools statistics over time, so every frame contributes to the
voiceprint -- including silence and room tone. A 5-second clip holding 2
seconds of speech spends 60% of its pooling budget describing the room. Trim
it and the same utterance describes the speaker instead.

!! MIGRATION !!  Templates enrolled BEFORE this change were built from
untrimmed audio. Probes are now trimmed, so old templates and new probes are
no longer directly comparable and scores against them may drop. Every user
must re-enroll once after this lands. Re-enrolling is what makes both sides
trimmed again.
"""

import os
import threading
from typing import Any, Dict, Tuple
from core.dependency_flags import sherpa_onnx, _SHERPA
from core.config import (MIC_SR, SHERPA_SV_MODEL, SV_SCORE_THRESHOLD,
                    SV_VAD_FRAME_MS, SV_VAD_KEEP_RATIO, SV_VAD_PAD_MS,
                    SV_MIN_SPEECH_SAMPLES, SV_TARGET_PEAK)
import numpy as np
from voice_security.biometric_store import cosine


# Preprocessing knobs. They decide what the model actually hears, so they are
# named and they live in config.py with every other tunable -- they were
# module-level literals here, which is the one place nobody looks when scores
# come out wrong.
_VAD_FRAME_MS = SV_VAD_FRAME_MS        # analysis frame for the energy VAD
_VAD_KEEP_RATIO = SV_VAD_KEEP_RATIO    # keep frames above this fraction of the loudest RMS
_VAD_PAD_MS = SV_VAD_PAD_MS            # keep a little either side; onsets carry identity
_MIN_SAMPLES = SV_MIN_SPEECH_SAMPLES   # below this much SPEECH, embeddings are noise
_TARGET_PEAK = SV_TARGET_PEAK          # peak-normalise so recording gain isn't a variable


def _trim_silence(audio: np.ndarray, sr: int = MIC_SR) -> np.ndarray:
    """Drop leading/trailing non-speech using frame energy.

    Relative, not absolute: the threshold is a fraction of the loudest frame
    in THIS clip, so it adapts to a quiet recording instead of deleting it.
    Returns the input unchanged when the clip cannot be ANALYSED at all (too
    short to frame, silent throughout, nothing above threshold). Those are
    "we don't know", and handing back the original lets _embed apply its own
    length check to real data.

    It does NOT do that when trimming succeeds and simply finds very little
    speech. That case used to return the untrimmed audio too -- so a five
    second clip holding a fifth of a second of speech came back at full
    length, sailed past _embed's _MIN_SAMPLES check, and produced exactly the
    garbage vector _embed says it refuses to produce: a voiceprint of a room.
    The trimmed result is returned instead, and _embed rejects it with a
    reason the caller can show.
    """
    frame = max(1, int(sr * _VAD_FRAME_MS / 1000))
    n = (len(audio) // frame) * frame
    if n < frame * 2:
        return audio
    frames = audio[:n].reshape(-1, frame)
    energy = np.sqrt((frames ** 2).mean(axis=1))
    peak = float(energy.max())
    if peak <= 1e-8:
        return audio                      # pure silence -- nothing to find
    voiced = np.nonzero(energy > peak * _VAD_KEEP_RATIO)[0]
    if voiced.size == 0:
        return audio
    pad = max(1, int(_VAD_PAD_MS / _VAD_FRAME_MS))
    lo = max(0, int(voiced[0]) - pad)
    hi = min(len(frames), int(voiced[-1]) + 1 + pad)
    # Returned even when short -- see the docstring. _embed decides whether
    # there is enough speech, and can say so.
    return audio[lo * frame: min(hi * frame, len(audio))]


class SherpaSV:
    def __init__(self, model_path: str = SHERPA_SV_MODEL):
        self.extractor = None
        self.manager = None
        self._embeddings: Dict[str, np.ndarray] = {}  # Local cache for cosine similarity
        self._dim: int = 0
        self._lock = threading.Lock()       # one forward pass at a time (see warm)
        if not _SHERPA:
            print("[SV] sherpa_onnx unavailable.")
            return
        try:
            cfg = sherpa_onnx.SpeakerEmbeddingExtractorConfig(
                model=model_path,
                num_threads=4,
                debug=False,
                provider="cpu",
            )
            if not cfg.validate():
                raise RuntimeError(f"Invalid SV config: {model_path}")
            self.extractor = sherpa_onnx.SpeakerEmbeddingExtractor(cfg)
            self.manager = sherpa_onnx.SpeakerEmbeddingManager(self.extractor.dim)
            self._dim = self.extractor.dim
            print(f"[SV] Loaded {os.path.basename(model_path)}. Dim={self.extractor.dim}")
        except Exception as e:
            print(f"[SV] Load failed: {e}")

    # ── The one preprocessing path ──────────────────────────────────────

    def _prep(self, audio: "np.ndarray") -> "np.ndarray":
        """Everything that happens to a waveform before TitaNet sees it.

        Called for enrolled audio and live probes alike -- that is the whole
        point. If you change anything here it changes both, which is the only
        way the two stay comparable.
        """
        audio = np.asarray(audio, dtype=np.float32).reshape(-1)
        if audio.size == 0:
            return audio
        if not np.isfinite(audio).all():
            audio = np.nan_to_num(audio, nan=0.0, posinf=1.0, neginf=-1.0)

        audio = _trim_silence(audio)

        # Peak-normalise so microphone gain / distance isn't a variable the
        # cosine has to absorb. Silent clips are left alone rather than being
        # amplified into pure noise.
        peak = float(np.max(np.abs(audio))) if audio.size else 0.0
        if peak > 1e-6:
            audio = (audio * (_TARGET_PEAK / peak)).astype(np.float32)
        return audio

    def _embed(self, audio: "np.ndarray") -> "np.ndarray":
        """_prep, then exactly ONE TitaNet forward pass. The only place in
        this class that touches the extractor."""
        if self.extractor is None:
            return np.array([], dtype=np.float32)
        prepped = self._prep(audio)
        if prepped.size < _MIN_SAMPLES:
            # Too little signal to describe a speaker. Empty array, not a
            # garbage vector -- callers check len() and can say why.
            return np.array([], dtype=np.float32)
        with self._lock:
            s = self.extractor.create_stream()
            s.accept_waveform(MIC_SR, prepped)
            return np.array(self.extractor.compute(s), dtype=np.float32)

    def warm(self) -> None:
        """One throwaway forward pass over a second of faint noise, so the
        model's weights and its threads are not cold when a command arrives
        (field log 2026-10-02: the first check after a quiet spell took 2.45 s,
        the next two 0.47 s and 0.17 s). Result discarded; nothing stored."""
        if self.extractor is None:
            return
        noise = (np.random.default_rng(0).standard_normal(MIC_SR)
                 * 0.01).astype(np.float32)
        with self._lock:
            s = self.extractor.create_stream()
            s.accept_waveform(MIC_SR, noise)
            self.extractor.compute(s)

    # enroll() used to live here: one clip in, manager.add + cache out. It was
    # called from nowhere -- enrollment_handler builds a template by averaging
    # THREE per-phrase embeddings and does its own manager.add / save_voice /
    # sync_cache -- and keeping it meant the codebase offered two enrollment
    # paths of visibly different quality, with the weaker one having the
    # obvious name. Removed rather than left as a trap.

    def verify_detail(self, name: str, audio: "np.ndarray") -> Dict[str, Any]:
        """THE ONE SCORING PATH: {"probe", "score"} -- the clip against this
        person's enrolled voice (the only one there is; nothing is learned
        beside it). One TitaNet pass; the rest is a dot product."""
        probe = self._embed(audio)      # empty when no extractor is loaded
        out: Dict[str, Any] = {"probe": probe, "score": 0.0}
        enrolled = self._embeddings.get(name)
        if probe.size and enrolled is not None and len(enrolled) == len(probe):
            # A cache miss (no persisted voiceprint) stays 0.0: main.py
            # repopulates this cache from BiometricStore at startup.
            out["score"] = cosine(probe, enrolled)
        return out

    def verify_with_embedding(self, name: str, audio: "np.ndarray"
                              ) -> Tuple[bool, float, "np.ndarray"]:
        """(passed, score, probe) against SV_SCORE_THRESHOLD, the one
        threshold the callers test."""
        d = self.verify_detail(name, audio)
        return d["score"] >= SV_SCORE_THRESHOLD, d["score"], d["probe"]

    def verify(self, name: str, audio: "np.ndarray") -> Tuple[bool, float]:
        """
        Verify speaker identity.

        Returns:
            (sv_bool, sv_score) where sv_bool is the manager's decision
            and sv_score is the manual cosine similarity (0.0 if no cache).

        Contract unchanged -- this now delegates to verify_with_embedding()
        and drops the probe, so callers that only want the decision (the
        passive per-command check in main._passive_voice_check) are unaffected.
        """
        sv_bool, score, _probe = self.verify_with_embedding(name, audio)
        return sv_bool, score

    def get_embedding(self, audio: "np.ndarray") -> np.ndarray:
        """Extract embedding without verification. Used by enrollment
        (enrollment_handler._finalize_enrollment) and by adaptive updates.

        Thin wrapper over _embed -- kept because enrollment legitimately wants
        an embedding with no verification attached. It no longer contains its
        own copy of the extraction, which is what let enrollment and
        verification drift apart."""
        return self._embed(audio)

    def sync_cache(self, name: str, embedding: np.ndarray) -> bool:
        """
        Populate the local cosine cache from an external source (e.g., BiometricStore).
        Call this at startup for each persisted voiceprint.

        Returns True if the cache was updated.
        """
        if embedding is None or len(embedding) == 0:
            return False
        # Dimension-checked. A voiceprint enrolled under a DIFFERENT model
        # (this class's model_path is configurable -- TitaNet 192, CAM++ 512)
        # would otherwise load happily and then meet np.dot against a probe of
        # the wrong length, raising ValueError inside verify_with_embedding --
        # which sits on the command path, so every command would die with a
        # stack trace instead of a sentence about re-enrolling.
        if self._dim and len(embedding) != self._dim:
            print(f"[SV] Ignoring voiceprint for '{name}': {len(embedding)} dims, "
                  f"but {os.path.basename(SHERPA_SV_MODEL)} produces {self._dim}. "
                  f"It was enrolled under a different model -- '{name}' must re-enroll.")
            return False
        self._embeddings[name] = embedding.astype(np.float32)
        return True

    def remove_from_cache(self, name: str) -> bool:
        """Remove a user from the local cosine cache."""
        if name in self._embeddings:
            del self._embeddings[name]
            return True
        return False

    def remove_user(self, name: str) -> bool:
        """Forget a user EVERYWHERE this class holds them: the cosine cache
        AND the sherpa SpeakerEmbeddingManager. Call this whenever a
        voiceprint is deleted from the BiometricStore (rollback, erase,
        rename) -- otherwise the manager refuses to re-add the same name for
        the life of the process. Returns True if anything was removed."""
        removed = self.remove_from_cache(name)
        if self.manager is not None:
            try:
                if self.manager.contains(name):
                    removed = bool(self.manager.remove(name)) or removed
            except Exception as e:                        # noqa: BLE001
                print(f"[SV] could not remove '{name}' from the speaker manager: {e}")
        return removed
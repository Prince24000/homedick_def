#!/usr/bin/env python3
"""
enrollment_handler.py — voice enrollment session management.

Guides a user through a 3-phrase enrollment, persists the resulting
embedding. Per-phrase extraction with averaging (not concatenation).

Adapted from an earlier WebSocket-oriented draft: that version parsed raw
WAV/PCM bytes per network message and keyed sessions by a live websocket
connection object, for many concurrent phone clients. Neither applies to a
single-user CLI, so the entry point here accepts already-decoded numpy audio
directly, and sessions are keyed by any hashable session id (a plain string
for CLI use) rather than a connection object -- the session dict was always
typed Dict[Any, ...], so this needed no structural change, just a simpler key.
The core state machine (EnrollmentSession) is untouched: it never held a
network reference in the first place.

SV-fix note: per-phrase embedding extraction now goes through
SherpaSV.get_embedding (-> SherpaSV._embed -> _prep), the SAME preprocessing
path verification uses. Previously this file called extractor.create_stream()/
compute() directly -- a second, independent copy of the SV preprocessing,
which is exactly how enrollment and verification could trim audio differently
and tank the cosine score. One chokepoint now; they can no longer diverge.
"""

import time
import numpy as np
from typing import Dict, List, Optional, Any
from core.config import (MIC_SR, ENROLL_MIN_PHRASE_SECONDS, ENROLL_MIN_RMS,
                    ENROLL_SESSION_TIMEOUT, ENROLL_MIN_USABLE_PHRASES)

# Minimum samples for a phrase to be worth embedding. DERIVED from MIC_SR,
# not typed as 8000 -- this file imports the sample rate and then hardcoded a
# constant computed from it, in two places. Change MIC_SR and both silently
# came to mean something else.
_MIN_PHRASE_SAMPLES = int(MIC_SR * ENROLL_MIN_PHRASE_SECONDS)


class EnrollmentSession:
    """Tracks one user's enrollment progress across multiple audio submissions."""

    # Single source of truth: shared with EnrollmentHandler
    PHRASES: List[str] = [
        "My voice is my password",
        "The quick brown fox jumps over the lazy dog",
        "Green foxtrot five alpha bravo"
    ]

    def __init__(self, user_id: str):
        self.user_id = user_id
        self.total_phrases = len(self.PHRASES)
        self.current_phrase = 0
        # Separate buffer per phrase, not one global list
        self.phrase_buffers: List[List[np.ndarray]] = [[] for _ in range(self.total_phrases)]
        self.completed = False
        self._last_activity = time.time()

    def add_audio(self, audio: np.ndarray) -> None:
        """Add audio chunk to CURRENT phrase."""
        if self.current_phrase < self.total_phrases:
            self.phrase_buffers[self.current_phrase].append(audio)
        self._last_activity = time.time()

    def get_current_phrase(self) -> str:
        if self.current_phrase < len(self.PHRASES):
            return self.PHRASES[self.current_phrase]
        return "Enrollment complete"

    def advance(self) -> None:
        self.current_phrase += 1
        if self.current_phrase >= self.total_phrases:
            self.completed = True
        self._last_activity = time.time()

    def get_phrase_audio(self, phrase_index: int) -> Optional[np.ndarray]:
        """Get concatenated audio for a specific phrase."""
        buffers = self.phrase_buffers[phrase_index]
        if not buffers:
            return None
        return np.concatenate(buffers)

    def touch(self) -> None:
        """Mark the session as still in use."""
        self._last_activity = time.time()

    def is_expired(self, timeout_sec: float = ENROLL_SESSION_TIMEOUT) -> bool:
        return (time.time() - self._last_activity) > timeout_sec

    def clear(self) -> None:
        """Explicit cleanup to free memory."""
        for buf_list in self.phrase_buffers:
            buf_list.clear()


class EnrollmentHandler:
    """Manages the enrollment state machine. Sessions are keyed by any
    hashable session id -- a real websocket connection for the (not yet
    built) network server, or a plain string for CLI use."""

    def __init__(self, sv, biometric_store):
        self.sv = sv
        self.store = biometric_store          # BiometricStore
        self.sessions: Dict[Any, EnrollmentSession] = {}
        # A COPY. This was a reference to the mutable class attribute, handed
        # straight out to main.py -- anything that appended to or reordered
        # this list would have changed the phrases for every session in the
        # process, including ones already underway.
        self.enrollment_phrases: List[str] = list(EnrollmentSession.PHRASES)

    def start_enrollment(self, session_key: Any, user_id: str) -> Dict[str, Any]:
        """Begin enrollment for a new user."""
        # Sweep abandoned sessions first. cleanup_expired() existed and was
        # called from NOWHERE, and a session only left self.sessions by
        # completing, being cancelled, or being replaced under the SAME key.
        # Keys are per user ("enroll_<user_id>"), so one abandoned enrollment
        # -- a phone that disconnected mid-flow -- held its three phrase
        # buffers for the life of the process. That is roughly half a
        # megabyte of raw voice audio per abandonment, in a ws_server shell
        # meant to run for months, and it is exactly the data you would least
        # like retained.
        self.cleanup_expired()

        if session_key in self.sessions:
            old_session = self.sessions[session_key]
            old_session.clear()
            print(f"[ENROLL] Overwriting old session for '{old_session.user_id}'")

        session = self.sessions[session_key] = EnrollmentSession(user_id)
        return self._prompt(session)

    @staticmethod
    def _prompt(session: EnrollmentSession) -> Dict[str, Any]:
        """The prompt for the session's current phrase."""
        return {
            "type": "enroll_prompt",
            "instruction": "Please say the following phrase clearly",
            "phrase": session.get_current_phrase(),
            "attempt": session.current_phrase + 1,
            # From the session, not a literal 3. The count lived in three
            # places -- this literal, the total_phrases default, and
            # len(PHRASES) -- under a comment claiming a single source.
            "total": session.total_phrases,
            "user_id": session.user_id
        }

    def receive_audio_array(self, session_key: Any,
                            audio: np.ndarray) -> Dict[str, Any]:
        """Process one already-decoded audio clip for the current phrase.
        (CLI entry point -- no wire-format bytes to parse, audio arrives as a
        plain numpy array from a recording or a loaded WAV file.)"""
        session, err = self._live_session(session_key)
        if err:
            return err

        # Trying counts as activity, whatever the outcome.
        #
        # _last_activity was only refreshed inside add_audio(), which the
        # too-quiet branch below returns BEFORE reaching. So a user in a quiet
        # room, doing exactly what the prompt asked and retrying, never
        # refreshed the session -- and after ENROLL_SESSION_TIMEOUT of active
        # effort their next attempt was answered with "enrollment_timeout" and
        # the session destroyed. main.py retries this path up to
        # MAX_ENROLL_RETRIES times, which made it more likely, not less.
        session.touch()

        if audio is None or audio.size == 0:
            return {"type": "error", "reason": "audio_empty"}
        if not np.isfinite(audio).all():
            print("[ENROLL] Audio contains NaN/Inf, cleaning")
            audio = np.nan_to_num(audio, nan=0.0, posinf=1.0, neginf=-1.0)

        reply = {
            "type": "enroll_audio_received",
            "phrase_index": session.current_phrase,
            "duration_ms": int(len(audio) / MIC_SR * 1000)
        }
        audio_energy = np.sqrt(np.mean(audio ** 2))
        if audio_energy < ENROLL_MIN_RMS:
            reply["warning"] = "audio_too_quiet"
        else:
            session.add_audio(audio)
        return reply

    def _live_session(self, session_key: Any):
        """(session, None) for a live session, else (None, error reply) --
        dropping an expired session and its audio on the way."""
        session = self.sessions.get(session_key)
        if session is None:
            return None, {"type": "error", "reason": "no_active_enrollment"}
        if session.is_expired():
            session.clear()
            self.sessions.pop(session_key, None)
            return None, {"type": "error", "reason": "enrollment_timeout"}
        return session, None

    def advance_phrase(self, session_key: Any) -> Dict[str, Any]:
        """User confirmed they spoke the phrase, move to next."""
        # Expiry checked here too. receive_audio_array was the only entry
        # point that tested it, so an expired session could still advance,
        # finalize, and write a voiceprint.
        session, err = self._live_session(session_key)
        if err:
            return err

        current_audio = session.get_phrase_audio(session.current_phrase)
        if current_audio is None or len(current_audio) < _MIN_PHRASE_SAMPLES:
            return {
                "type": "enroll_error",
                "reason": "no_audio_for_phrase",
                "phrase_index": session.current_phrase,
                "instruction": "Please speak the phrase clearly before continuing."
            }

        session.advance()

        if session.completed:
            return self._finalize_enrollment(session_key)
        return self._prompt(session)

    def _finalize_enrollment(self, session_key: Any) -> Dict[str, Any]:
        """Extract per-phrase embeddings, average, normalize, persist."""
        session = self.sessions.pop(session_key, None)
        if not session:
            return {"type": "error", "reason": "session_lost"}

        if self.sv.extractor is None:
            return {
                "type": "enroll_result",
                "success": False,
                "reason": "sv_model_not_loaded",
                "user_id": session.user_id
            }

        embeddings = []
        for i in range(session.total_phrases):
            phrase_audio = session.get_phrase_audio(i)
            if phrase_audio is None or len(phrase_audio) < _MIN_PHRASE_SAMPLES:
                print(f"[ENROLL] Phrase {i}: too short or missing, skipping")
                continue

            try:
                # Was a private create_stream()/compute() here -- the second
                # copy of SV preprocessing, and the reason enroll and verify
                # could diverge. Now both share SherpaSV._embed (VAD + trim),
                # so the enrolled template and every probe are built the same
                # way. This one line is the enrollment half of the SV fix.
                emb = self.sv.get_embedding(phrase_audio)
                if emb is None or len(emb) == 0:
                    print(f"[ENROLL] Phrase {i}: empty embedding after trim, skipping")
                    continue
                # Unit-normalise EACH phrase before averaging: raw extractor
                # norms vary per utterance, and averaging raw vectors let
                # whichever phrase came out with the largest norm dominate
                # the template instead of each counting equally.
                n = float(np.linalg.norm(emb))
                if not np.isfinite(n) or n <= 1e-10:
                    print(f"[ENROLL] Phrase {i}: degenerate embedding, skipping")
                    continue
                embeddings.append(emb / n)
            except Exception as e:
                print(f"[ENROLL] Phrase {i} extraction failed: {e}")
                continue

        if len(embeddings) < ENROLL_MIN_USABLE_PHRASES:
            return {
                "type": "enroll_result",
                "success": False,
                "reason": "insufficient_phrases",
                "user_id": session.user_id,
                "hint": "At least 2 clear phrases needed. Please re-enroll in a quiet environment."
            }

        embedding = np.mean(embeddings, axis=0)
        norm = np.linalg.norm(embedding)
        if not norm > 1e-10:
            return {
                "type": "enroll_result",
                "success": False,
                "reason": "zero_norm_embedding",
                "user_id": session.user_id
            }
        embedding = embedding / norm

        try:
            # RE-ENROLLING YOURSELF MUST WORK. The in-memory manager keeps a
            # name for the life of the process, so a second enrolment (or a
            # retry after a rolled-back ceremony) was refused with
            # "manager_rejected". The store is the durable truth; the
            # manager's stale copy is dropped and replaced.
            self.sv.remove_user(session.user_id)
            if not self.sv.manager.add(session.user_id, embedding):
                return {
                    "type": "enroll_result",
                    "success": False,
                    "reason": "manager_rejected",
                    "user_id": session.user_id,
                    "hint": "The speaker manager refused this voiceprint. "
                            "Restart the hub and try again.",
                }

            # THE DURABLE WRITE IS THE ONE THAT COUNTS. Its return value used
            # to be discarded: save_voice returns False when the new embedding
            # is within COSINE_DEDUP_THRESHOLD of the stored one, and nothing
            # is persisted -- yet enrollment reported success: True. Someone
            # re-enrolling to fix a problem was told it worked while the file
            # on disk never changed.
            stored = self.store.save_voice(session.user_id, embedding)
            self.sv.sync_cache(session.user_id, embedding)

            if not stored:
                return {
                    "type": "enroll_result",
                    "success": False,
                    "reason": "unchanged_from_stored",
                    "user_id": session.user_id,
                    "hint": "This recording is almost identical to the voiceprint "
                            "already on file, so nothing was changed.",
                }

            return {
                "type": "enroll_result",
                "success": True,
                "user_id": session.user_id,
                "embedding_dim": int(embedding.shape[0]),
                "phrases_used": len(embeddings)
            }
        except Exception as e:
            # manager.add() has already succeeded by the time we can get here,
            # so the in-memory manager holds a voiceprint the durable store may
            # not. Say so -- a half-written identity is worth more than a
            # generic failure string.
            print(f"[ENROLL] Durable save FAILED for '{session.user_id}' after the "
                  f"speaker manager accepted it -- in-memory and on-disk state "
                  f"now disagree until restart: {e}")
            return {
                "type": "enroll_result",
                "success": False,
                "reason": f"save_failed: {e}",
                "user_id": session.user_id
            }

    def cancel_enrollment(self, session_key: Any) -> Dict[str, Any]:
        """User cancelled mid-enrollment."""
        if session_key in self.sessions:
            session = self.sessions.pop(session_key)
            session.clear()
            return {
                "type": "enroll_cancelled",
                "user_id": session.user_id
            }
        return {"type": "error", "reason": "no_active_enrollment"}

    def cleanup_expired(self, timeout_sec: float = ENROLL_SESSION_TIMEOUT) -> None:
        """Remove expired sessions (call periodically, or just before starting
        a new one for the CLI's single-session case)."""
        for key in [k for k, s in self.sessions.items()
                    if s.is_expired(timeout_sec)]:
            session = self.sessions.pop(key)
            session.clear()
            print(f"[ENROLL] Expired session for '{session.user_id}'")
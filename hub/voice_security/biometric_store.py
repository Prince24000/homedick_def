#!/usr/bin/env python3
"""
biometric_store.py — encrypted voiceprint storage.

Voice only, JSON + Fernet encryption + numpy. Adapted from an earlier
standalone draft: path and thresholds now come from config.py (single source
of truth, matches every other store in this project) instead of being
hardcoded or locally duplicated, and the file-lock mechanism now auto-detects
the platform (Linux fcntl, else filelock) instead of requiring a manual
comment/uncomment edit to move between dev (Windows) and the Pi.

Adds one thing the original didn't have: a per-user command_count. It fed
the periodic spoken challenge, which Defense no longer has (2026-11-16); the
count is kept in the file but nothing gates on it.
"""

import os
import time
import numpy as np
from typing import Optional, Dict, List
from pathlib import Path
from cryptography.fernet import Fernet

from core import config
from core.locked_store import (LockedJsonStore, snapshot, mutation,
                                         reader, atomic_write_json)


def cosine(a: np.ndarray, b: np.ndarray) -> float:
    """Cosine similarity of two embeddings -- the one formula every
    voiceprint comparison in voice_security uses."""
    return float(np.dot(a, b) / (np.linalg.norm(a) * np.linalg.norm(b) + 1e-10))


class BiometricStore(LockedJsonStore):
    _TAG = "STORE"
    _WHAT = "Store"

    def __init__(self, path: str = config.BIOMETRIC_STORE_PATH):
        Path(path).parent.mkdir(parents=True, exist_ok=True, mode=0o700)

        key = config.require_fernet_key()
        self._fernet = Fernet(key.encode())
        self._data: Dict[str, str] = {}
        self._cache: Dict[str, np.ndarray] = {}
        self._meta: Dict[str, dict] = {}
        self._init_store(path)

    def _encrypt(self, arr: np.ndarray) -> str:
        raw = arr.astype(np.float32).tobytes()
        return self._fernet.encrypt(raw).decode()

    def _decrypt(self, token: str) -> Optional[np.ndarray]:
        try:
            raw = self._fernet.decrypt(token.encode())
            return np.frombuffer(raw, dtype=np.float32)
        except Exception as e:
            print(f"[STORE] Decryption failed (corrupt token or key mismatch): {e}")
            return None

    def _clear(self):
        self._data, self._meta, self._cache = {}, {}, {}

    def _parse(self, loaded: dict, verbose: bool) -> None:
        data = loaded.get("embeddings", {})
        meta = loaded.get("metadata", {})
        if not isinstance(data, dict) or not isinstance(meta, dict):
            raise ValueError("embeddings/metadata are not JSON objects")
        # Voice variations an older version kept ("<name>_album") are not
        # read, and go at the next save: only enrolled voices are kept.
        self._data = {k: v for k, v in data.items() if not k.endswith("_album")}
        self._meta = meta
        self._cache = {}
        if verbose:
            print(f"[STORE] Loaded {len(self._data)} encrypted voiceprints")

    # ── Cross-platform persist ─────────────────────────────────────────────

    def _write(self, rotate: bool = True) -> bool:
        """Atomic write (LockedJsonStore._persist holds the lock).

        `rotate=False` skips the backup shuffle. Backups exist to protect the
        VOICEPRINTS; the per-command counter is a small integer that is
        rebuilt by simply using the system, and it is written on EVERY
        command. Rotating three files, zeroing and unlinking one of them, and
        fsyncing all of that to record `command_count += 1` is a lot of SD
        card for very little -- and SD cards on a Pi are what wears out."""
        def _backup():
            self._rotate_backups()
            # Demote the current file to bak.0. Inside `rotate` too: skipping
            # it on a counter-only write leaves bak.0 holding the last real
            # VOICEPRINT state, which is the thing worth having a backup of.
            # os.replace is atomic on its own -- this step is purely
            # about keeping history, not about the write being safe.
            #
            # LINKED (or copied), not moved: moving the live file aside left
            # a window with NO store at self.path, and a power cut inside it
            # booted an empty store -- every voiceprint "gone" until someone
            # restored bak.0 by hand.
            if os.path.exists(self.path):
                snapshot(self.path, self.path + ".bak.0")
        # Our own write, under the lock: nobody else can have replaced it.
        self._sig = atomic_write_json(
            self.path, {"embeddings": self._data, "metadata": self._meta},
            before_replace=_backup if rotate else None, indent=2)
        return True

    def _rotate_backups(self):
        oldest = f"{self.path}.bak.{config.MAX_BACKUPS - 1}"
        if os.path.exists(oldest):
            self._secure_unlink(oldest)

        for i in range(config.MAX_BACKUPS - 1, 0, -1):
            src = f"{self.path}.bak.{i - 1}"
            dst = f"{self.path}.bak.{i}"
            if os.path.exists(src):
                os.replace(src, dst)       # dst was moved up (or unlinked) already

        legacy = self.path + ".bak"
        if os.path.exists(legacy):
            self._secure_unlink(legacy)

    @staticmethod
    def _secure_unlink(path: str):
        """Delete a rotated backup.

        This used to zero the file first. It did not do what the name
        promised, for two independent reasons:

          - open(path, 'wb') TRUNCATES to zero length before a byte is
            written, so the zeros landed in freshly allocated blocks and the
            original contents were never overwritten at all.
          - even done correctly, overwrite-in-place does not erase anything
            on flash, SSD, or any copy-on-write filesystem, which is all of
            the media this runs on.

        And the file is Fernet-encrypted: its confidentiality rests on
        HOMEVOICE_FERNET_KEY, not on the bytes being scrubbed. A plain unlink
        is exactly as safe and does not cost an fsync per rotation. False
        assurance is worse than none -- it stops you asking where the real
        protection comes from."""
        try:
            os.unlink(path)
        except OSError:
            pass

    # ── Voice API ─────────────────────────────────────────────────────────

    @mutation
    def save_voice(self, user_id: str, embedding: np.ndarray) -> bool:
        if self._load_failed or self._unreadable:
            # READ-ONLY means read-only in memory too: a voiceprint held only
            # here would read back (and "succeed") until the next restart.
            raise RuntimeError("the voiceprint store failed to load (quarantined "
                               "or unreadable) -- nothing can be saved")
        key = f"{user_id}_voice"
        existing = self.get_voice(user_id)
        if existing is not None:
            cos_sim = cosine(existing, embedding)
            if cos_sim > config.COSINE_DEDUP_THRESHOLD:
                print(f"[STORE] Voice for '{user_id}' nearly identical (cos={cos_sim:.4f}), skipping write")
                return False
        self._data[key] = self._encrypt(embedding)
        self._cache[key] = embedding.copy()

        meta = self._new_meta(user_id)
        meta["updated_at"] = time.time()
        meta["update_count"] = meta.get("update_count", 0) + 1

        self._persist()
        print(f"[STORE] Saved voice for '{user_id}'")
        return True

    def _new_meta(self, user_id: str) -> dict:
        return self._meta.setdefault(user_id, {
            "created_at": time.time(), "update_count": 0, "command_count": 0})

    @reader
    def get_voice(self, user_id: str) -> Optional[np.ndarray]:
        key = f"{user_id}_voice"
        if key in self._cache:
            return self._cache[key].copy()
        token = self._data.get(key)
        if not token:
            return None
        emb = self._decrypt(token)
        if emb is None:
            print(f"[STORE] Corrupt voiceprint for '{user_id}', treating as missing")
            return None
        self._cache[key] = emb.copy()
        # Return the COPY, not `emb`. np.frombuffer gives a read-only view of
        # the decrypted bytes, so the first call after a restart handed back an
        # immutable array while every later call (served from cache) handed
        # back a writable one. Any caller doing arithmetic in place would fail
        # exactly once per process, on the first use, and never reproduce.
        return self._cache[key].copy()

    # No template update and no voice album: the enrolled voice is the only
    # one, and it never changes (2026-10-09; the album removed 2026-10-30).

    @mutation
    def delete_voice(self, user_id: str) -> bool:
        key = f"{user_id}_voice"
        if key in self._data:
            del self._data[key]
            self._cache.pop(key, None)
            self._meta.pop(user_id, None)
            if not self._persist():
                return False            # not on disk: a person removal must not
                                        # report a voiceprint gone that is not
            print(f"[STORE] Deleted voice for '{user_id}'")
            return True
        return False

    @reader
    def list_users(self) -> List[str]:
        return sorted(key[:-6] for key in self._data if key.endswith("_voice"))

    @reader
    def has_voice(self, user_id: str) -> bool:
        """True only if a voiceprint exists AND can still be decrypted.

        This used to test key presence alone, so a token encrypted under a
        replaced FERNET key reported True here and None from get_voice() --
        the voice gate would take the "this user has a voiceprint" branch and
        then fail verification with no explanation. Ask the question that
        callers actually mean."""
        return self.get_voice(user_id) is not None

    def delete_user(self, user_id: str) -> bool:
        return self.delete_voice(user_id)

    # ── Periodic-challenge counter ──────────────────────────────────────────
    # Persisted per user, survives restarts (per the "persisted across
    # sessions" decision) — lives here rather than a new file, since it's
    # security-relevant state tied to the same per-user record as the
    # voiceprint itself, not device state.

    @reader
    def get_command_count(self, user_id: str) -> int:
        return self._meta.get(user_id, {}).get("command_count", 0)

    @mutation
    def increment_command_count(self, user_id: str) -> int:
        meta = self._new_meta(user_id)
        meta["command_count"] = meta.get("command_count", 0) + 1
        self._persist(rotate=False)
        return meta["command_count"]

    @mutation
    def reset_command_count(self, user_id: str) -> None:
        if user_id in self._meta:
            self._meta[user_id]["command_count"] = 0
            self._persist(rotate=False)
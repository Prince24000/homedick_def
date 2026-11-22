"""pin_auth.py — PIN authentication with bcrypt, rate limiting, persistence,
and crypto-style recovery.

5-word ordered BIP-39 mnemonic phrase:
    - Must enter words in EXACT order (position 1, 2, 3, 4, 5)
    - Stored as ONE bcrypt hash of the whole phrase ("phrase-v2"); the
      global recovery budget is the only attempt limit
    - Attacker cannot tell which position failed or attempts remaining
    - Phrase works forever (like a crypto seed)
    - Records minted before v2 (per-word HMACs) still verify; re-setting
      the PIN once rewrites them as v2
"""

import base64
import os
import time
import secrets
import hashlib
import hmac
from typing import Tuple, Dict, List, Optional
from pathlib import Path
from core.dependency_flags import bcrypt, _BCRYPT
from core.locked_store import (JsonRegistry, LockedJsonStore, snapshot,
                                         mutation, reader, atomic_write_json)
from core import config
from core.config import (
    PIN_MAX_ATTEMPTS, PIN_LOCKOUT_SECONDS, PIN_MIN_LENGTH, PIN_MAX_LENGTH,
    PIN_BCRYPT_ROUNDS, DATA_DIR, BIP39_WORDLIST_PATH,
    PIN_RECOVERY_PHRASE_LENGTH, PIN_RECOVERY_MAX_ATTEMPTS_PER_WORD,
    PIN_RECOVERY_WORD_LOCKOUT, PIN_RECOVERY_RATE_LIMIT_WINDOW,
    PIN_RECOVERY_MAX_ATTEMPTS,
)


# ── Configuration ──────────────────────────────────────────────────────────

_PIN_DB_PATH = Path(DATA_DIR) / "pin_db.json"

_RECOVERY_PHRASE_LENGTH = PIN_RECOVERY_PHRASE_LENGTH
_RECOVERY_MAX_ATTEMPTS_PER_WORD = PIN_RECOVERY_MAX_ATTEMPTS_PER_WORD
_RECOVERY_WORD_LOCKOUT = PIN_RECOVERY_WORD_LOCKOUT
_RECOVERY_RATE_LIMIT_WINDOW = PIN_RECOVERY_RATE_LIMIT_WINDOW
_RECOVERY_MAX_ATTEMPTS = PIN_RECOVERY_MAX_ATTEMPTS


def _load_bip39_wordlist() -> List[str]:
    """Load official BIP-39 English wordlist. Download from:
    https://github.com/bitcoin/bips/blob/master/bip-0039/english.txt
    Place at: config.BIP39_WORDLIST_PATH (portable — resolves relative to the
    project folder, not a hardcoded Pi path).
    """
    path = Path(BIP39_WORDLIST_PATH)
    if path.exists():
        with open(path, 'r', encoding='utf-8') as f:
            words = [w.strip().lower() for w in f if w.strip()]
        if len(words) == 2048:
            return words
        print(f"[PIN] Warning: bip39_english.txt has {len(words)} words, expected 2048")

    print("[PIN] WARNING: Using embedded fallback wordlist (only 128 words -- "
          "fine for testing, NOT for real enrollment).")
    print(f"    Place the real 2048-word list at: {path}")
    print("    curl -o bip39_english.txt "
          "https://raw.githubusercontent.com/bitcoin/bips/master/bip-0039/english.txt")

    return [
        "abandon", "ability", "able", "about", "above", "absent", "absorb", "abstract",
        "absurd", "abuse", "access", "accident", "account", "accuse", "achieve", "acid",
        "acoustic", "acquire", "across", "act", "action", "actor", "actress", "actual",
        "adapt", "add", "addict", "address", "adjust", "admit", "adult", "advance",
        "advice", "aerobic", "affair", "afford", "afraid", "again", "age", "agent",
        "agree", "ahead", "aim", "air", "airport", "aisle", "alarm", "album",
        "alcohol", "alert", "alien", "all", "alley", "allow", "almost", "alone",
        "alpha", "already", "also", "alter", "always", "amateur", "amazing", "among",
        "amount", "amused", "analyst", "anchor", "ancient", "anger", "angle", "angry",
        "animal", "ankle", "announce", "annual", "another", "answer", "antenna", "antique",
        "anxiety", "any", "apart", "apology", "appear", "apple", "approve", "april",
        "arch", "arctic", "area", "arena", "argue", "arm", "armed", "armor",
        "army", "around", "arrange", "arrest", "arrive", "arrow", "art", "artefact",
        "artist", "artwork", "ask", "aspect", "assault", "asset", "assist", "assume",
        "asthma", "athlete", "atom", "attack", "attend", "attitude", "attract", "auction",
        "audit", "august", "aunt", "author", "auto", "autumn", "average", "avocado",
        "avoid", "awake", "aware", "away", "awesome", "awful", "awkward", "axis",
    ]


_BIP39_WORDLIST = _load_bip39_wordlist()
#: True when the 128-word embedded list is in use. set_pin then refuses to
#: mint a recovery phrase rather than hand out one drawn from a toy list.
_BIP39_IS_FALLBACK = len(_BIP39_WORDLIST) != 2048


def _pin_format_ok(pin: str) -> bool:
    return pin.isdigit() and PIN_MIN_LENGTH <= len(pin) <= PIN_MAX_LENGTH


class PinAuth(LockedJsonStore):
    """
    PIN auth with 5-word ordered crypto-style recovery.

    Recovery flow:
        1. User sets PIN -> receives 5-word phrase in exact order
        2. User writes it down: "alpha orange dog cheat words"
        3. PIN forgotten -> must enter ALL 5 words in EXACT order
        4. One bcrypt check of the whole phrase, under the global budget
        5. Attacker cannot tell which position failed
        6. Phrase works forever
    """

    _TAG = "PIN"
    _WHAT = "PIN store"

    def __init__(self, rounds: int = PIN_BCRYPT_ROUNDS):
        self.rounds = rounds
        self.pin_db: Dict[str, bytes] = {}
        self.attempts: Dict[str, Tuple[int, float]] = {}
        self.recovery_attempts: Dict[str, Tuple[int, float]] = {}
        self.recovery_data: Dict[str, Dict] = {}
        #: ws_server and a terminal main.py are SEPARATE PROCESSES on one
        #: file. Every mutation runs lock -> reload -> modify -> atomic write
        #: -> unlock (LockedJsonStore), so neither can write back a stale
        #: copy over the other's attempt counters -- which would undo a
        #: lockout.
        self._init_store(_PIN_DB_PATH)

    def _clear(self):
        self.pin_db, self.attempts = {}, {}
        self.recovery_data, self.recovery_attempts = {}, {}

    def _parse(self, data: dict, verbose: bool) -> None:
        pin_db = {k: base64.b64decode(v.encode())
                  for k, v in data.get("hashes", {}).items()}
        attempts = {k: (v[0], v[1]) for k, v in data.get("attempts", {}).items()}
        recovery_attempts = {k: (v[0], v[1]) for k, v in
                             data.get("recovery_attempts", {}).items()}
        self.pin_db, self.attempts = pin_db, attempts
        self.recovery_attempts = recovery_attempts
        self.recovery_data = data.get("recovery", {})
        if verbose:
            print(f"[PIN] Loaded {len(self.pin_db)} PINs, {len(self.recovery_data)} recovery sets")

    def _write(self) -> bool:
        data = {
            "hashes": {
                k: base64.b64encode(v).decode()
                for k, v in self.pin_db.items()
            },
            "attempts": self.attempts,
            "recovery": self.recovery_data,
            "recovery_attempts": self.recovery_attempts,
        }

        def _backup():
            if os.path.exists(self.path):
                # Linked/copied, never moved: a move left a moment with no
                # pin_db.json at all, and a power cut there booted with
                # every PIN and recovery record apparently gone.
                snapshot(self.path, self.path + ".bak")
        try:
            self._sig = atomic_write_json(self.path, data,
                                          before_replace=_backup, indent=2)
            return True
        except Exception as e:
            print(f"[PIN] Persist failed: {e}")
            return False

    @reader
    def has_pin(self, user_id: str) -> bool:
        return user_id in self.pin_db

    @mutation
    def set_pin(self, user_id: str, pin: str) -> Tuple[bool, Optional[List[str]]]:
        """Set PIN. EVERY set — first-time or re-set — mints and returns a
        FRESH 5-word recovery phrase, replacing (and invalidating) any
        earlier one. The phrase is only ever shown at the moment it is
        created, so tying its lifetime to the PIN keeps one rule: the words
        you were shown last are the words that work."""
        if not _pin_format_ok(pin):
            return False, None
        if not _BCRYPT:
            print("[PIN] bcrypt unavailable.")
            return False, None

        self.pin_db[user_id] = bcrypt.hashpw(pin.encode(), bcrypt.gensalt(rounds=self.rounds))
        self.attempts[user_id] = (0, 0)

        # Unconditional: a re-set used to keep the old phrase silently, so
        # the person setting a new PIN was never shown the words that would
        # actually rescue them. New PIN -> new phrase, always displayed.
        if _BIP39_IS_FALLBACK:
            # No phrase at all beats a phrase from a 128-word toy list.
            print(f"[PIN] WARNING: no 2048-word BIP-39 list at {BIP39_WORDLIST_PATH} "
                  f"-- PIN set for '{user_id}' WITHOUT a recovery phrase. Install "
                  f"the list and set the PIN again to get one.")
            self.recovery_data.pop(user_id, None)
            if not self._persist():
                return False, None
            return True, None
        recovery_phrase = self._generate_recovery_phrase(user_id)

        # NOT persisted = NOT set: the next locked operation reloads from
        # disk, and a phrase shown for a PIN that was never saved is a lie.
        if not self._persist():
            return False, None
        print(f"[PIN] Set for '{user_id}'. Fresh 5-word recovery generated "
              f"(any previous phrase is now void).")
        return True, recovery_phrase

    @mutation
    def verify(self, user_id: str, pin: str) -> Tuple[bool, str]:
        if not _pin_format_ok(pin):
            return False, "invalid_format"
        if not _BCRYPT:
            return False, "bcrypt_unavailable"
        # Rate limit BEFORE the existence check, and a missing PIN COUNTS
        # AS A FAILURE -- otherwise "does this user have a PIN?" is answered
        # for free, without consuming an attempt: an enumeration oracle for
        # anyone who reaches this far.
        if not self._check_rate_limit(user_id):
            return False, "locked_out"
        if not self.has_pin(user_id):
            self._record_failure(user_id)
            return False, "no_pin_set"
        # CHARGE FIRST, then check. The attempt is recorded (and must reach
        # the disk) BEFORE the PIN is compared, and undone on success. If
        # the write fails (disk full, read-only card) the guess is refused
        # unchecked: a failure counted only in memory is forgotten by the
        # next locked reload, i.e. unlimited guesses.
        stored = self.pin_db[user_id]
        if not self._record_failure(user_id):
            return False, "system_error"
        if bcrypt.checkpw(pin.encode(), stored):
            self._reset(user_id)
            return True, "success"
        if self.attempts.get(user_id, (0, 0))[1]:
            print(f"[PIN] '{user_id}' locked out for {PIN_LOCKOUT_SECONDS}s.")
        return False, "wrong_pin"

    @reader
    def get_lockout_remaining(self, user_id: str) -> int:
        if user_id not in self.attempts:
            return 0
        _, lockout = self.attempts[user_id]
        # Capped: a lockout can never be longer than PIN_LOCKOUT_SECONDS,
        # even when the wall clock stepped BACKWARDS since it was recorded
        # (a Pi with no RTC boots on a stale clock until NTP syncs).
        return max(0, min(int(lockout - time.time()), PIN_LOCKOUT_SECONDS))

    # ── Recovery ────────────────────────────────────────────────────────────

    def _generate_recovery_phrase(self, user_id: str) -> List[str]:
        """Generate 5-word ordered BIP-39 recovery phrase.

        FORMAT "phrase-v2": the WHOLE ordered phrase, normalised, is hashed
        ONCE with bcrypt (its own salt, PIN_BCRYPT_ROUNDS). No per-word
        hashes -- those let anyone holding the file and the key recover a
        phrase in 5 x 2048 guesses -- and no HOMEVOICE_FERNET_KEY, so
        rotating that key no longer voids every phrase written down. The
        user id is not part of the hash, so a rename needs no stamping.
        """
        rng = secrets.SystemRandom()
        # Use sample() for guaranteed uniqueness (no duplicate words)
        words = rng.sample(_BIP39_WORDLIST, _RECOVERY_PHRASE_LENGTH)

        self.recovery_data[user_id] = {
            "format": "phrase-v2",
            "hash": bcrypt.hashpw(self._normalise_phrase(words),
                                  bcrypt.gensalt(rounds=self.rounds)).decode(),
            "created_at": time.time(),
        }
        return words

    @mutation
    def renew_recovery_phrase(self, user_id: str) -> Optional[List[str]]:
        """FIVE NEW WORDS, SAME PIN. The old words stop working the moment
        these are saved. The caller must have proven the current PIN first:
        whoever knows the PIN can already set a new one (which mints words
        too), so this grants nothing the PIN did not.

        The words are never stored -- only their bcrypt hash, like every
        phrase -- which is why the old ones cannot be shown again and new
        ones are made instead. None when there is no PIN, no real BIP-39
        list, or the store could not be written."""
        if not self.has_pin(user_id) or _BIP39_IS_FALLBACK or not _BCRYPT:
            return None
        words = self._generate_recovery_phrase(user_id)
        if not self._persist():
            return None
        print(f"[PIN] Fresh recovery words for '{user_id}' (PIN unchanged; "
              f"the previous words are now void).")
        return words

    @staticmethod
    def _normalise_phrase(words: List[str]) -> bytes:
        """Lowercase, single-spaced, stripped, UTF-8 -- the one shape both
        minting and checking hash. Five BIP-39 words sit far under bcrypt's
        72-byte limit."""
        return " ".join(" ".join(str(w or "").lower().split())
                        for w in words).strip().encode("utf-8")

    @reader
    def needs_recovery_upgrade(self, user_id: str) -> bool:
        """True for a pre-v2 (per-word HMAC) record. Re-setting the PIN
        mints a fresh phrase and writes it as v2."""
        rec = self.recovery_data.get(user_id)
        return bool(rec) and rec.get("format") != "phrase-v2"

    @mutation
    def verify_recovery_phrase(self, user_id: str,
                               words: List[str]) -> Tuple[bool, str]:
        """
        Verify 5-word recovery phrase in EXACT order: one bcrypt check of
        the whole phrase for v2 records, the per-word path for legacy ones.

        Returns:
            (valid: bool, reason: str)

        NOTE: Always returns generic "invalid_phrase" on failure so an
        attacker cannot tell which position failed or how many attempts
        remain -- nor whether the user has a PIN or a phrase at all. The
        budget is charged BEFORE those questions are asked (as verify()
        does for PINs), and an unknown user still pays one bcrypt, so
        neither the reason, the budget nor the response time tells an
        asker who exists.
        """

        # _check_recovery_rate_limit persists its own bookkeeping.
        if not self._check_recovery_rate_limit(user_id):
            return False, "invalid_phrase"

        rec = self.recovery_data.get(user_id) if self.has_pin(user_id) else None
        # A wrong word COUNT pays the bcrypt too: answered instantly for a
        # known user and after one bcrypt for an unknown one, it told the
        # asker who exists.
        if not rec or len(words) != _RECOVERY_PHRASE_LENGTH:
            self._equalise_timing()
            return False, "invalid_phrase"

        if rec.get("format") == "phrase-v2":
            if not _BCRYPT:
                return False, "bcrypt_unavailable"
            phrase = self._normalise_phrase(words)
            try:
                ok = bool(phrase) and bcrypt.checkpw(phrase, str(rec.get("hash", "")).encode())
            except ValueError:
                ok = False                    # malformed stored hash / >72 bytes
            if not ok:
                return False, "invalid_phrase"  # ANY mismatch, one answer
            print(f"[PIN] Recovery phrase verified for '{user_id}'")
            return True, "success"

        # LEGACY per-word HMAC record (pre-v2). Kept only so a phrase written
        # down before the change still works; set_pin rewrites as v2.
        config.require_fernet_key()
        now = time.time()
        word_attempts = rec.get("word_attempts", {})
        # THE NAME THE HASHES WERE MADE WITH, not the name the person
        # answers to today. Absent on records written before rename
        # existed, and for those the two are the same thing.
        bind_id = rec.get("hash_id") or user_id

        # Check each word in order
        for i in range(_RECOVERY_PHRASE_LENGTH):
            pos_data = word_attempts.get(str(i), [0, 0])
            count, lockout = pos_data

            if now < lockout:
                # Position is locked -- don't reveal which one. No _persist():
                # nothing changed, and a read path should not write.
                return False, "invalid_phrase"

            secret_key = os.environ.get("HOMEVOICE_FERNET_KEY", "").encode()
            test_hash = hmac.new(secret_key, f"{i}:{words[i]}:{bind_id}".encode(), hashlib.sha256).hexdigest()

            if not secrets.compare_digest(test_hash, rec["hashes"][i]):
                # STORE THE INCREMENT UNCONDITIONALLY.
                #
                # The write used to sit INSIDE `if count >= MAX`, so a
                # count of 1 or 2 was computed and thrown away. Every
                # failure re-read 0, became 1, and was discarded -- the
                # counter never reached 3, and the per-word lockout this
                # class documents ("Each position has 3 independent
                # attempts") could not fire even once. Verified by trace.
                #
                # The global cap in _check_recovery_rate_limit still held,
                # so the phrase was never brute-forceable; but the layer
                # the docstring advertises was doing nothing.
                count += 1
                lockout_until = lockout
                if count >= _RECOVERY_MAX_ATTEMPTS_PER_WORD:
                    lockout_until = now + _RECOVERY_WORD_LOCKOUT
                    print(f"[PIN] Recovery position locked for '{user_id}' "
                          f"({_RECOVERY_WORD_LOCKOUT}s) -- position not disclosed "
                          f"to the caller.")
                word_attempts[str(i)] = [count, lockout_until]
                rec["word_attempts"] = word_attempts
                self._persist()
                return False, "invalid_phrase"  # Generic -- don't reveal position

        # All 5 words correct in order
        self._persist()
        print(f"[PIN] Recovery phrase verified for '{user_id}'")
        return True, "success"

    _DUMMY_HASH: Optional[bytes] = None

    def _equalise_timing(self) -> None:
        """Spend one bcrypt check at this instance's cost, so "no such
        person" takes as long as "wrong words"."""
        if not _BCRYPT:
            return
        if PinAuth._DUMMY_HASH is None:
            PinAuth._DUMMY_HASH = bcrypt.hashpw(
                secrets.token_bytes(16), bcrypt.gensalt(rounds=self.rounds))
        bcrypt.checkpw(b"x", PinAuth._DUMMY_HASH)

    @mutation
    def reset_pin_with_recovery(self, user_id: str, words: List[str], new_pin: str) -> Tuple[bool, str]:
        """Reset PIN using 5-word ordered recovery phrase."""
        # The new PIN's FORMAT is checked first: it says nothing about the
        # phrase, and a typo in it used to cost one of the few recovery
        # attempts per window after the phrase had already been accepted.
        if not _pin_format_ok(new_pin):
            return False, "invalid_new_pin_format"

        valid, reason = self.verify_recovery_phrase(user_id, words)
        if not valid:
            return False, reason

        if not _BCRYPT:
            return False, "bcrypt_unavailable"

        self.pin_db[user_id] = bcrypt.hashpw(new_pin.encode(), bcrypt.gensalt(rounds=self.rounds))
        self.attempts[user_id] = (0, 0)
        if not self._persist():
            return False, "system_error"

        print(f"[PIN] Reset completed for '{user_id}'. Recovery phrase unchanged.")
        return True, "success"

    @reader
    def get_recovery_status(self, user_id: str) -> Dict:
        rec = self.recovery_data.get(user_id)
        if not rec:
            return {"has_recovery": False}

        now = time.time()
        locked = 0
        for pos_data in rec.get("word_attempts", {}).values():
            if now < pos_data[1]:
                locked += 1

        return {
            "has_recovery": True,
            "total_words": _RECOVERY_PHRASE_LENGTH,
            "locked_positions": locked,        # always 0 for phrase-v2
            "created_at": rec["created_at"],
            "format": rec.get("format") or "legacy",
        }

    # ── Private Helpers ──────────────────────────────────────────────────────

    def _check_rate_limit(self, user_id: str) -> bool:
        if user_id not in self.attempts:
            # Absent is the same as (0, 0); recording that is a disk write
            # (and an SD-card fsync) that changes nothing.
            return True
        count, lockout = self.attempts[user_id]
        now = time.time()
        if lockout > now + PIN_LOCKOUT_SECONDS:
            # The clock stepped backwards since this lockout was recorded.
            # Re-anchor it to a full lockout from NOW rather than honouring
            # an until-time that may be hours or years away.
            lockout = now + PIN_LOCKOUT_SECONDS
            self.attempts[user_id] = (count, lockout)
            self._persist()
        if lockout and now >= lockout and count:
            # The lockout has run its course: a fresh set of attempts, not a
            # counter stuck at the maximum that re-locks on the next miss.
            self.attempts[user_id] = (0, 0)
            self._persist()
        return now >= lockout

    def _record_failure(self, user_id: str) -> bool:
        """Count one attempt; True only if that count reached the disk."""
        count, _ = self.attempts.get(user_id, (0, 0))
        count += 1
        if count >= PIN_MAX_ATTEMPTS:
            lockout_until = time.time() + PIN_LOCKOUT_SECONDS
            self.attempts[user_id] = (count, lockout_until)
        else:
            self.attempts[user_id] = (count, 0)
        return self._persist()

    def _reset(self, user_id: str):
        if tuple(self.attempts.get(user_id, (0, 0))) == (0, 0):
            return                  # every correct PIN used to rewrite the file
        self.attempts[user_id] = (0, 0)
        self._persist()

    def _check_recovery_rate_limit(self, user_id: str) -> bool:
        """Global cap: max _RECOVERY_MAX_ATTEMPTS per _RECOVERY_RATE_LIMIT_WINDOW."""
        count, window_start = self.recovery_attempts.get(user_id, (0, 0.0))
        now = time.time()
        if window_start > now:
            # The clock stepped BACKWARDS since the window opened (as in
            # _check_rate_limit): re-anchored to now, so a spent budget
            # waits one window, not until the clock catches up again.
            window_start = now
            self.recovery_attempts[user_id] = (count, now)
            self._persist()
        if now - window_start > _RECOVERY_RATE_LIMIT_WINDOW:
            self.recovery_attempts[user_id] = (1, now)
        elif count >= _RECOVERY_MAX_ATTEMPTS:
            return False
        else:
            self.recovery_attempts[user_id] = (count + 1, window_start)
        # An attempt that could not be recorded is not allowed: the next
        # locked call reloads from disk and would never see it.
        return self._persist()

    @mutation
    def rename_user(self, old_id: str, new_id: str) -> Tuple[bool, str]:
        """Move one person's PIN, recovery set and both attempt counters to
        a new name, in a single persist.

        THIS IS THE HALF OF THE RENAME THAT CAN LOCK SOMEONE OUT. A name is
        the key here, in four dicts, and this class is two of the four
        stores a rename has to move (the voiceprint store and the phone
        binding are the others). Moving three and missing one leaves a
        person whose voice resolves to a name the PIN db has never heard
        of -- and whose PIN fallback therefore fails too. Both doors, shut,
        by a spelling correction.

        So: every check first, every move after, ONE _persist() at the end.
        Nothing here can fail between the first write and the last, because
        dict moves do not raise once the keys are known to exist.

        The recovery record is STAMPED before it moves (see
        _generate_recovery_phrase): its hashes are bound to the name they
        were minted under, and that name is not changing just because the
        person's is.
        """
        old_id = (old_id or "").strip()
        new_id = (new_id or "").strip()
        if not old_id or not new_id:
            return False, "empty_name"
        if old_id == new_id:
            return False, "same_name"
        known = (set(self.pin_db) | set(self.recovery_data)
                 | set(self.attempts) | set(self.recovery_attempts))
        if old_id not in known:
            return False, "unknown_user"
        if new_id in self.pin_db or new_id in self.recovery_data:
            # Refused, not merged. Two people's PINs in one record is not a
            # rename, it is a person quietly gaining someone else's key.
            return False, "name_taken"
        # Counters alone under new_id are NOT a person: verify() and the
        # recovery budget record them for any name anyone types. They must
        # not make a name untakeable, and they are replaced below by the
        # counters that belong to the person actually moving in.
        self.attempts.pop(new_id, None)
        self.recovery_attempts.pop(new_id, None)

        rec = self.recovery_data.pop(old_id, None)
        if rec is not None:
            # LEGACY records only: their HMACs contain the name they were
            # minted under, and this is the last moment it is still known.
            # A phrase-v2 hash has no name in it and needs no stamp.
            if rec.get("format") != "phrase-v2":
                rec.setdefault("hash_id", old_id)
            self.recovery_data[new_id] = rec

        if old_id in self.pin_db:
            self.pin_db[new_id] = self.pin_db.pop(old_id)
        # THE LOCKOUTS MOVE TOO. Leaving them behind would hand anyone a
        # free reset of a locked-out account by renaming it.
        if old_id in self.attempts:
            self.attempts[new_id] = self.attempts.pop(old_id)
        if old_id in self.recovery_attempts:
            self.recovery_attempts[new_id] = self.recovery_attempts.pop(old_id)

        if not self._persist():
            # Nothing reached the disk; the next locked call reloads it.
            return False, "persist_failed"
        print(f"[PIN] Renamed '{old_id}' -> '{new_id}' "
              f"(PIN, recovery, lockouts; phrase still valid)")
        return True, "success"

    @mutation
    def delete_pin(self, user_id: str) -> bool:
        """Remove PIN and recovery data for a user."""
        if user_id not in self.pin_db:
            return False
        del self.pin_db[user_id]
        self.attempts.pop(user_id, None)
        self.recovery_attempts.pop(user_id, None)
        self.recovery_data.pop(user_id, None)
        if not self._persist():
            return False
        print(f"[PIN] Deleted PIN and recovery for '{user_id}'")
        return True


# ── cards ──────────────────────────────────────────────────────────────────

_CARD_DB_PATH = Path(DATA_DIR) / "cards.json"


class CardStore(JsonRegistry):
    """UID -> person, for RFID/NFC cards. THE SIXTH STORE a name is a key in.

    A UID IS A NAME, NOT A PROOF (the RFID law, 2026-08-16): it is
    transmitted in clear on every tap and cloneable for the price of a
    takeaway, so nothing here ever gates anything. What it buys is
    LEGIBILITY -- "Augusta's card tapped" instead of "card BA839D07
    tapped", and a rule that answers to a person instead of to hex. The
    guard that keeps a card from opening a door lives in
    automations.action_screening and does not consult this file at all.

    It sits in pin_auth beside the PIN and recovery stores for one
    engineering reason: a rename has to move every store a person's name
    keys, and this file is where two of the others already implement that.
    A sixth store in a seventh module is a sixth chance to forget.
    """

    _TAG = "CARD"
    _WHAT = "cards"
    _DUMP_KW = {"indent": 2}

    def __init__(self, path: Optional[str] = None):
        #: UID -> {"user_id", "added_at", "added_by"}
        self.cards: Dict[str, Dict] = {}
        # JsonRegistry: writers re-read under the cross-process lock, READERS
        # pick up the other process's save (a card enrolled from the console
        # used to stay "unknown" to ws_server's automations until restart),
        # and an unreadable or corrupt file is kept or moved aside rather than
        # read as empty and then saved over with one card. A CARD FILE THAT
        # WILL NOT PARSE IS STILL NOT A REASON TO REFUSE ANYBODY: it gates
        # nothing, and taps simply read as unrecognised.
        self._init_registry(path or _CARD_DB_PATH)

    # ── the file ───────────────────────────────────────────────────────
    def _locked(self):
        return self._writing()

    def _valid(self, raw) -> bool:
        return isinstance(raw, dict) and isinstance(raw.get("cards") or {}, dict)

    def _clear(self) -> None:
        self.cards = {}

    def _install(self, raw) -> None:
        self.cards = {self._key(k): v for k, v in (raw.get("cards") or {}).items()
                      if isinstance(v, dict) and v.get("user_id")}

    def _document(self) -> dict:
        return {"cards": self.cards}

    def _persist(self) -> None:
        try:
            self._save()
        except Exception as exc:                        # noqa: BLE001
            print(f"[CARD] persist failed: {exc}")

    def _fresh(self) -> Dict[str, Dict]:
        """The cards as the file holds them now (another process may have
        saved since)."""
        with self._lock:
            self._reload_if_changed()
            return self.cards

    @staticmethod
    def _key(uid: str) -> str:
        """Tasmota publishes hex in upper case; a person may type it in
        either. One shape in the file, so a lookup cannot miss by case."""
        return str(uid or "").strip().upper()

    # ── reading ────────────────────────────────────────────────────────
    def owner(self, uid: str) -> Optional[str]:
        """Whose card this is, or None -- which is what 'unknown card'
        actually means."""
        rec = self._fresh().get(self._key(uid))
        return (rec or {}).get("user_id") or None

    def cards_of(self, user_id: str) -> List[str]:
        uid = str(user_id or "").strip()
        return sorted(k for k, v in self._fresh().items()
                      if v.get("user_id") == uid)

    def has_card(self, user_id: str) -> bool:
        return bool(self.cards_of(user_id))

    # ── writing ────────────────────────────────────────────────────────
    @mutation
    def add(self, uid: str, user_id: str,
            added_by: str = "") -> Tuple[bool, str]:
        """Bind one card to one person.

        REFUSED, NEVER REASSIGNED, when the card already belongs to
        somebody else. Silently moving it would be one person quietly
        gaining another's card -- the same reasoning that makes
        rename_user refuse a taken name rather than merge into it.
        """
        key = self._key(uid)
        user_id = str(user_id or "").strip()
        if not key or not user_id:
            return False, "empty"
        held = self.owner(key)
        if held and held != user_id:
            return False, held
        self.cards[key] = {"user_id": user_id,
                           "added_at": int(time.time()),
                           "added_by": str(added_by or "").strip()}
        self._persist()
        print(f"[CARD] {key} -> '{user_id}'")
        return True, "success"

    @mutation
    def remove(self, uid: str) -> bool:
        if self.cards.pop(self._key(uid), None) is None:
            return False
        self._persist()
        return True

    @mutation
    def remove_all(self, user_id: str) -> int:
        gone = self.cards_of(user_id)
        for key in gone:
            self.cards.pop(key, None)
        if gone:
            self._persist()
        return len(gone)

    @mutation
    def rename_user(self, old_id: str, new_id: str) -> int:
        """Follow a person to their new name. Returns how many cards moved.

        NON-FATAL by design, and the rename flow treats it that way: a card
        left behind costs a re-tap, while unwinding five good stores over
        one would be the far worse trade. Same call the enrolment journal
        gets, for the same reason -- neither gates a door.
        """
        old_id = str(old_id or "").strip()
        new_id = str(new_id or "").strip()
        if not old_id or not new_id or old_id == new_id:
            return 0
        moved = 0
        for key in self.cards_of(old_id):
            self.cards[key]["user_id"] = new_id
            moved += 1
        if moved:
            self._persist()
            print(f"[CARD] moved {moved} card(s) '{old_id}' -> '{new_id}'")
        return moved
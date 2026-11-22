"""people.py — who was enrolled, when, and by whom.

WHY THIS EXISTS AT ALL.

The hub could always answer "who is enrolled?" -- biometric_store.list_users()
returns the names. It could never answer "who let them in?", and that is the
question that matters when a second voice can command the house.

Two people sharing one handset is not itself a risk: each is known by their
own voiceprint and their own PIN, and neither credential verifies the other.
What was missing is the RECORD. An enrolment that leaves no trace means a
voice can be added to the house and nothing, anywhere, says when it happened
or who authorised it -- so nobody can notice one they did not expect.

This is deliberately not a security control. It cannot stop an enrolment; the
voice/PIN gate on `enroll_voice` does that. It is a LEDGER, and its whole job
is to make the Profile screen able to say "added by you · 26 Aug" -- and, one
day, to make "added by someone else, at 3am" visible.

LETTERS (2026-10-11). A short message waiting for one person: "Chidi
changed your 'Night' scene". Kept here, beside the names they are keyed by,
until that person reads it and says so -- they may not have a phone open
when it happens.

WHAT IT DOES NOT HOLD: voiceprints, PINs, hashes, phrases. Names and times.
The file is written 0600 anyway, because "who lives here" is not public
either.
"""

import time
from pathlib import Path
from typing import Dict, List, Optional

from core.config import DATA_DIR
from core.locked_store import JsonRegistry, mutation

_PEOPLE_PATH = Path(DATA_DIR) / "people.json"


class PeopleJournal(JsonRegistry):
    """A tiny append-mostly record of enrolments, keyed by user id.

    One entry per person:
        {"added_by": "<user id, or ''>", "added_at": <epoch seconds>}

    `added_by` is empty for the first person on a hub -- nobody let them in,
    they are where the house started -- and for anyone enrolled before this
    file existed.
    """

    _TAG = "PEOPLE"
    _WHAT = "people"
    _DUMP_KW = {"indent": 2}

    #: Letters kept per person, newest last; older ones fall off.
    MAX_LETTERS = 30

    def __init__(self, path: Optional[str] = None) -> None:
        self.entries: Dict[str, Dict] = {}
        #: user id -> [{"id", "title", "body", "at"}] -- see LETTERS above.
        self.letters: Dict[str, List[Dict]] = {}
        #: JsonRegistry: every write re-reads the file under a cross-process
        #: lock first (ws_server and a terminal main.py each hold a journal,
        #: and a write from a stale copy would erase the other's enrolments),
        #: readers pick up the other process's saves, and an unreadable file
        #: is never read as empty and then saved over.
        self._init_registry(path or _PEOPLE_PATH)

    # ── persistence ──────────────────────────────────────────────────

    def _locked(self):
        return self._writing()

    def _valid(self, raw) -> bool:
        return isinstance(raw, dict) and isinstance(raw.get("people", {}), dict)

    def _clear(self) -> None:
        # A CORRUPT LEDGER MUST NOT STOP THE HOUSE. Nothing here gates
        # anything, so losing it costs a line of history on one screen
        # -- refusing to boot over that would be the worse trade. (It is
        # moved aside for a human now, not overwritten.)
        self.entries = {}
        self.letters = {}

    def _install(self, raw) -> None:
        self.entries = {str(k): dict(v) for k, v in raw.get("people", {}).items()
                        if isinstance(v, dict)}
        letters = raw.get("letters", {})
        self.letters = {
            str(k): [dict(x) for x in v if isinstance(x, dict)]
            for k, v in (letters.items() if isinstance(letters, dict) else ())
            if isinstance(v, list)}

    def _document(self) -> dict:
        return {"people": self.entries, "letters": self.letters}

    # ── the ledger ───────────────────────────────────────────────────

    @mutation
    def record(self, user_id: str, added_by: str = "") -> bool:
        """Write down that `user_id` was enrolled, by `added_by`.

        FIRST WRITE WINS. Re-recording a voice is not a new person and must
        not rewrite their history -- if this overwrote, then re-enrolling
        your own voice would quietly make you your own sponsor, and the one
        fact the ledger exists to keep would be erasable by the person it
        is about.
        """
        user_id = (user_id or "").strip()
        if not user_id:
            return False
        # Someone REMOVED and then let in again is a new arrival: their old
        # entry is history of a person who left, not of this one.
        if user_id in self.entries and not self.entries[user_id].get("removed_at"):
            return False
        added_by = (added_by or "").strip()
        self.entries[user_id] = {
            # Never let someone be recorded as their own sponsor.
            "added_by": "" if added_by == user_id else added_by,
            "added_at": int(time.time()),
        }
        self._persist()
        return True

    @mutation
    def forget(self, user_id: str) -> bool:
        """Drop one person's entry -- used when a registration rolls back.

        The people they sponsored keep pointing at a name that is gone.
        That is correct: it happened, and rewriting history to tidy up a
        dangling reference is how a ledger stops being one. Readers show
        an unknown sponsor as unknown.
        """
        user_id = (user_id or "").strip()
        if user_id not in self.entries:
            return False
        del self.entries[user_id]
        self._persist()
        return True

    @mutation
    def mark_removed(self, user_id: str, removed_by: str) -> bool:
        """`user_id` was taken out of the house by `removed_by`. The entry
        STAYS -- it happened -- and their letters go with them."""
        user_id = (user_id or "").strip()
        if not user_id:
            return False
        entry = self.entries.setdefault(user_id, {"added_by": "", "added_at": 0})
        entry["removed_by"] = (removed_by or "").strip()
        entry["removed_at"] = int(time.time())
        self.letters.pop(user_id, None)
        self._persist()
        return True

    # ── letters ──────────────────────────────────────────────────────

    @mutation
    def send_letter(self, to: str, letter_id: str, title: str,
                    body: str) -> bool:
        """Leave a message for `to`. The same id REPLACES (two edits of one
        scene are one letter, saying the latest)."""
        to = (to or "").strip()
        if not to or not letter_id:
            return False
        box = [x for x in self.letters.get(to, []) if x.get("id") != letter_id]
        box.append({"id": letter_id, "title": title, "body": body,
                    "at": int(time.time())})
        self.letters[to] = box[-self.MAX_LETTERS:]
        self._persist()
        return True

    def letters_for(self, user_id: str) -> List[Dict]:
        with self._lock:
            self._reload_if_changed()
            return [dict(x) for x in self.letters.get((user_id or "").strip(), [])]

    @mutation
    def drop_letter(self, user_id: str, letter_id: str) -> bool:
        box = self.letters.get((user_id or "").strip(), [])
        kept = [x for x in box if x.get("id") != letter_id]
        if len(kept) == len(box):
            return False
        self.letters[user_id.strip()] = kept
        self._persist()
        return True

    @mutation
    def rename(self, old_id: str, new_id: str) -> bool:
        """Move one person's entry, AND every reference to them as sponsor.

        THE FIFTH PLACE A NAME LIVES. The four stores everyone remembers are
        the voiceprint, the PIN, the recovery set and the phone binding --
        and then there is this file, where the name appears not once but in
        every OTHER person's `added_by`. Miss those and a rename turns
        "added by Augusta" into a reference to nobody.
        """
        old_id = (old_id or "").strip()
        new_id = (new_id or "").strip()
        if not old_id or not new_id or old_id == new_id:
            return False
        if new_id in self.entries and not self.entries[new_id].get("removed_at"):
            return False
        touched = False
        if old_id in self.entries:
            self.entries[new_id] = self.entries.pop(old_id)
            touched = True
        if old_id in self.letters:
            self.letters[new_id] = self.letters.pop(old_id)
            touched = True
        for entry in self.entries.values():
            if (entry.get("added_by") or "").strip() == old_id:
                entry["added_by"] = new_id
                touched = True
        if touched:
            self._persist()
        return touched

    # ── reading ──────────────────────────────────────────────────────

    def _persist(self) -> None:
        try:
            self._save()
        except Exception as e:                       # noqa: BLE001
            print(f"[PEOPLE] Persist failed: {e}")

    def get(self, user_id: str) -> Dict:
        with self._lock:
            self._reload_if_changed()
            return dict(self.entries.get((user_id or "").strip(), {}))
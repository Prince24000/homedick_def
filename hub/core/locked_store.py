"""locked_store.py — the ONE implementation of "a JSON file two processes
share": cross-process lock, reload-on-change, atomic write, quarantine.

ws_server and a terminal main.py are separate processes on the same files
(voiceprints, PINs, phones, the people ledger, cards). Every one of those
stores used to carry its own copy of this machinery. Standard library only,
so identity.phones can import it before the environment is fully built.
"""

import contextlib
import functools
import json
import os
import shutil
import tempfile
import threading
import time
from typing import Optional

from core.hv_common import event

# ── Platform-specific locking — auto-detected, not manually toggled ────────
try:
    import fcntl
    _HAS_FCNTL = True
except ImportError:
    _HAS_FCNTL = False

FileLock = None  # type: ignore
if not _HAS_FCNTL:
    try:
        from filelock import FileLock
    except ImportError:
        pass


def snapshot(src: str, dst: str) -> None:
    """Make `dst` a copy of `src` WITHOUT `src` ever ceasing to exist.

    A hard link costs no write on the SD card; filesystems without links
    (FAT/exFAT) get a real copy. Staged under dst+'.tmp' and swapped in, so
    `dst` itself is never half-written either."""
    tmp = dst + ".tmp"
    with contextlib.suppress(FileNotFoundError):
        os.unlink(tmp)
    try:
        os.link(src, tmp)
    except OSError:
        shutil.copy2(src, tmp)
    os.replace(tmp, dst)


def fingerprint(st: os.stat_result) -> tuple:
    """What "has the file changed?" compares. More than mtime alone: two
    saves inside one filesystem clock tick share an mtime, and the second
    would never be picked up. Every save is a rename of a fresh file, so the
    inode moves too."""
    return (st.st_mtime_ns, st.st_ino, st.st_size)


def path_fingerprint(path: str) -> Optional[tuple]:
    """fingerprint() of the file at `path`; None when it cannot be stat'd."""
    try:
        return fingerprint(os.stat(path))
    except OSError:
        return None


def quarantine(path: str) -> Optional[str]:
    """Move an unparseable file aside under a timestamped name, for a human
    to look at. Returns where it went, or None if it could not be moved."""
    dest = f"{path}.corrupt-{int(time.time())}"
    try:
        os.replace(path, dest)
        return dest
    except OSError:
        return None


def atomic_write_json(path: str, obj, before_replace=None, **dump_kw) -> tuple:
    """Write `obj` to `path` whole or not at all; returns the new file's
    fingerprint(). Staged in a sibling temp file that is 0600 FROM CREATION
    (never chmod'd after a world-readable write), fsynced, then renamed over
    `path`. `before_replace` runs between the fsync and the rename (backup
    rotation). On any failure the temp file is removed and the error
    re-raised; `path` is untouched."""
    d = os.path.dirname(path) or "."
    os.makedirs(d, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=d, prefix=os.path.basename(path) + ".",
                               suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(obj, f, **dump_kw)
            f.flush()
            os.fsync(f.fileno())
        if before_replace is not None:
            before_replace()
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise
    return fingerprint(os.stat(path))


@contextlib.contextmanager
def flock(lock_path: str):
    """Exclusive CROSS-PROCESS lock on `lock_path` (ws_server and a
    terminal main.py are separate processes sharing these files). fcntl on
    Linux; filelock elsewhere, whose timeout RAISES -- a mutation that cannot
    take the lock must not go ahead unlocked."""
    os.makedirs(os.path.dirname(lock_path) or ".", exist_ok=True)
    if _HAS_FCNTL:
        fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)
    elif FileLock is not None:
        with FileLock(lock_path, timeout=10):
            yield
    else:
        # Still correct for a single process, not against a second writer.
        # Loud, not silent, about that gap.
        print(f"[STORE] WARNING: no file-lock backend available (fcntl or "
              f"filelock) -- {lock_path} is not locked.")
        yield


class StoreLock:
    """Thread lock + cross-process flock, re-entrant within one thread.

    flock is per open file, so nesting two raw flock()s in one process
    would deadlock; the depth count lets a locked method call another
    (update_voice -> save_voice, reset_pin_with_recovery ->
    verify_recovery_phrase). `on_acquire` runs only on the OUTERMOST
    acquire, after the flock is held -- that is where the owner re-reads the
    file, so every read-modify-write starts from what is on disk NOW."""

    def __init__(self, lock_path: str):
        self.lock_path = lock_path
        self.thread_lock = threading.RLock()
        self._depth = 0

    @contextlib.contextmanager
    def hold(self, on_acquire=None):
        with self.thread_lock:
            if self._depth:
                self._depth += 1
                try:
                    yield
                finally:
                    self._depth -= 1
                return
            with flock(self.lock_path):
                self._depth = 1
                try:
                    if on_acquire is not None:
                        on_acquire()
                    yield
                finally:
                    self._depth = 0


def mutation(fn):
    """Run a method as lock -> reload from disk -> modify -> atomic write
    -> unlock, via the owner's _locked(). Signature unchanged."""
    @functools.wraps(fn)
    def wrapper(self, *args, **kwargs):
        with self._locked():
            return fn(self, *args, **kwargs)
    return wrapper


def reader(fn):
    """Read paths: the THREAD lock only (no flock -- writers swap the file
    in atomically, so any read sees a whole file) plus a reload if another
    process has written, so a reload here cannot swap the dicts out from
    under a mutation running on another thread."""
    @functools.wraps(fn)
    def wrapper(self, *args, **kwargs):
        with self._lock.thread_lock:
            self._reload_if_changed()
            return fn(self, *args, **kwargs)
    return wrapper


class LockedJsonStore:
    """Base for a security store (voiceprints, PINs) that FAILS CLOSED.

    Subclasses provide `_TAG` / `_WHAT` (log prefix and a name for the
    store), `_clear()` (empty in-memory state), `_parse(data, verbose)`
    (install a loaded JSON document; raise on anything malformed) and
    `_write()` (write the whole store; return True if it reached the disk).
    """

    _TAG = "STORE"
    _WHAT = "Store"

    def _init_store(self, path: str) -> None:
        self.path = str(path)
        #: fingerprint() of the file the in-memory state was read from
        #: (None = no file); readers pick up another process's write by it.
        self._sig: Optional[tuple] = None
        #: Set when the file would not parse: it is quarantined and NOTHING
        #: is written until a human has looked at it.
        self._load_failed = False
        #: Set when the file EXISTS but could not be read (EACCES, EIO,
        #: EMFILE): the last-known state is kept, nothing is written over
        #: the file, and every read and locked operation tries again.
        self._unreadable = False
        #: Every mutation: lock -> reload from disk -> modify -> atomic
        #: write -> unlock. See _locked().
        self._lock = StoreLock(self.path + ".lock")
        self._load(verbose=True)

    def _load(self, verbose: bool = False) -> None:
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                st = os.fstat(f.fileno())      # the file actually read
                self._parse(json.load(f), verbose)
            self._sig = fingerprint(st)
            self._load_failed = self._unreadable = False
        except FileNotFoundError:
            # No file IS the state: every writer holds the lock and swaps
            # the file in atomically, so there is no in-between moment in
            # which it is legitimately absent.
            if verbose:
                print(f"[{self._TAG}] No existing store at {self.path}")
            self._clear()
            self._sig = None
            self._unreadable = False
        except OSError as e:
            # NOT READ is not CORRUPT. Quarantining here moved a perfectly
            # good PIN or voiceprint store aside over a transient fault (too
            # many open files on a busy hub, an SD-card read error), after
            # which the other process saw no file at all: an empty store.
            print(f"[{self._TAG}] Could not read {self.path} ({e}); keeping "
                  f"what was loaded, writing nothing until it can be read.")
            self._sig = None                   # so the next read retries
            self._unreadable = True
        except Exception as e:
            # QUARANTINE, never "start fresh": the next write used to
            # overwrite the unparseable file with an empty store and no
            # backup at all.
            moved = quarantine(self.path)
            where = (f"Moved aside to {moved}." if moved
                     else "It could not be moved aside.")
            print(f"[{self._TAG}] Load failed: {e}. {where} {self._WHAT} is "
                  f"READ-ONLY and empty until a human restores or removes "
                  f"that file.")
            self._clear()
            self._sig = None
            self._load_failed = True

    def _reload_if_changed(self) -> None:
        """READ paths: pick up another process's write. Mutations do not
        rely on this -- they reload unconditionally under the lock."""
        if self._load_failed:
            return
        try:
            sig = fingerprint(os.stat(self.path))
        except FileNotFoundError:
            sig = None
        except OSError:
            return
        if sig != self._sig:
            self._load()

    def _locked(self):
        """Every read-modify-write: take the thread lock and the
        cross-process flock, then re-read the file UNCONDITIONALLY. The
        store is written whole, so a mutation starting from a copy another
        process has since changed would write that stale copy back -- a
        counter reset undone, a lockout erased, a deleted voiceprint
        resurrected."""
        def _reload():
            if not self._load_failed:
                self._load()
        return self._lock.hold(on_acquire=_reload)

    def _persist(self, **kw) -> bool:
        """Write the store. False if nothing was written -- callers that
        record ATTEMPTS must then fail closed: the next locked operation
        reloads from disk, so a counter that was not written never
        happened."""
        if self._load_failed or self._unreadable:
            print(f"[{self._TAG}] Persist REFUSED: the {self._WHAT.lower()} "
                  + ("failed to load and was quarantined" if self._load_failed
                     else "could not be read, so memory may be stale")
                  + " -- nothing is written until that is resolved.")
            return False
        # Callers already hold _locked(); this is then a re-entrant no-op.
        # Held here too so a write can never happen unlocked.
        with self._lock.hold():
            return self._write(**kw)


class JsonRegistry:
    """Base for a shared REGISTRY (devices.json, scenes.json) that RECOVERS,
    where LockedJsonStore fails closed.

    A corrupt file is QUARANTINED, not reset: resetting meant the next save
    wrote an empty (or one-record) registry over everything a person could
    still have repaired by hand; moved aside, the registry starts empty and
    writable. A file that exists but cannot be read -- or be moved aside --
    keeps what is in memory, is retried on every write, and nothing is saved
    over it meanwhile.

    Subclasses call `_init_registry(path)` last in __init__ and provide
    `_TAG` (event tag), `_WHAT` (plural noun), `_MISSING_NOTE` (said when
    there is no file at first load, or None), `_DUMP_KW`, `_valid(raw)`,
    `_install(raw)`, `_clear()` and `_document()` (what _save writes).
    Readers hold `self._lock` (the thread lock) and call _reload_if_changed;
    every read-modify-write runs under `self._writing()`.
    """

    _TAG = "STORE"
    _WHAT = "records"
    _MISSING_NOTE: Optional[str] = None
    _DUMP_KW: dict = {}

    def _init_registry(self, path) -> None:
        self.path = str(path)
        #: fingerprint() of the file last read or written (None = no file).
        #: A different one on disk means another process saved.
        self._sig: Optional[tuple] = None
        #: The file exists but could not be read or parsed, and is still
        #: there: nothing is saved over it until it can be (see _save).
        self._unreadable = False
        self._store_lock = StoreLock(self.path + ".lock")
        self._lock = self._store_lock.thread_lock
        self._load()

    def _valid(self, raw) -> bool:
        return isinstance(raw, dict)

    def _load(self) -> None:
        """(Re)read the file. Callers hold self._lock, or are __init__."""
        try:
            # Explicit UTF-8 both ways. Without it this follows the platform
            # locale, so any non-ASCII character round-trips correctly on one
            # machine and raises on another.
            with open(self.path, encoding="utf-8") as f:
                st = os.fstat(f.fileno())      # the file actually read
                raw = json.load(f)
            if not self._valid(raw):
                raise ValueError(f"not an object of {self._WHAT}")
        except FileNotFoundError:
            if self._sig is None and self._MISSING_NOTE:   # first load
                event(self._TAG, self._MISSING_NOTE.format(path=self.path))
            self._clear()
            self._sig, self._unreadable = None, False
        except OSError as e:
            # Unreadable rather than absent -- a permissions problem, a
            # vanished mount. This used to propagate and kill the process at
            # construction.
            event(self._TAG, f"Cannot read {self.path} ({e}) -- keeping the "
                  f"{self._WHAT} already loaded. NOTHING WILL BE SAVED OVER "
                  f"IT until this is fixed.")
            self._unreadable = True
        except ValueError as e:
            moved = quarantine(self.path)
            event(self._TAG, f"Corrupted {self.path} ({e}) -- starting with "
                  f"zero {self._WHAT}; " + (
                      f"the old file is at {moved}" if moved else
                      "it could not be moved aside, so NOTHING WILL BE SAVED "
                      "OVER IT until it is"))
            self._clear()
            self._sig = None if moved else path_fingerprint(self.path)
            self._unreadable = not moved
        else:
            self._sig, self._unreadable = fingerprint(st), False
            self._install(raw)

    def _reload_if_changed(self) -> None:
        """Pick up another process's save. Callers hold self._lock, so the
        registry is never swapped out from under a mutation on another
        thread."""
        if path_fingerprint(self.path) != self._sig:
            self._load()

    def _refresh(self) -> None:
        if self._unreadable:
            self._load()        # retry: a fixed permission changes no signature
        else:
            self._reload_if_changed()

    def _writing(self):
        """Every read-modify-write: the thread lock, the cross-process lock,
        then a reload if another process saved since. _save writes the WHOLE
        registry, so a mutation starting from a stale copy would write that
        copy back over the other process's work."""
        return self._store_lock.hold(on_acquire=self._refresh)

    def _save(self) -> None:
        """Callers hold _writing()."""
        if self._unreadable:
            event(self._TAG, f"{self.path} could not be read, so it is not "
                  f"being saved over -- this change is held in memory only.")
            return
        self._sig = atomic_write_json(self.path, self._document(),
                                      ensure_ascii=False, **self._DUMP_KW)

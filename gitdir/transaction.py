#!/usr/bin/python3
"""
Per-target transactional publishing.

Contract
--------
A transaction reserves a *not yet existing* final target directory, materialises
an immutable source plan (manifest) in a sibling staging directory, writes and
fsyncs every verified file inside ``<staging>/root`` and only then publishes the
complete tree with a single no-replace rename.  An append-only, checksummed
hash-chain journal records the immutable manifest id and every state transition,
which makes crash recovery deterministic:

* resume / cleanup are only permitted when the requested manifest matches the
  manifest recorded at BEGIN;
* a crash immediately before or after the rename is detected and either
  completed idempotically or reported untouched;
* cooperating processes converge on one deterministic staging directory and
  serialise on an OS file lock, so an interrupted job can never turn into a
  silent overwrite.

The module is deliberately network agnostic: callers supply the immutable
manifest and a ``fetcher`` callable that returns a binary context manager for a
source URL.
"""

import base64
import errno
import hashlib
import json
import os
import shutil
import sys
import threading
import time

FORMAT_VERSION = 1

STAGING_PREFIX = ".gitdir-tx-"
MANIFEST_NAME = "manifest.json"
JOURNAL_NAME = "tx.journal"
ROOT_NAME = "root"

_GENESIS = "0" * 64
_READ_CHUNK = 1024 * 1024

# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class TransactionError(Exception):
    """Base class for transaction failures. The staging dir is left intact."""


class CorruptJournalError(TransactionError):
    """The journal fails checksum validation (a non-tail record is bad)."""


class ManifestMismatchError(TransactionError):
    """The requested manifest does not match the immutable recorded plan."""


class TargetExistsError(TransactionError):
    """The final target already exists; the transaction must not replace it."""


class SameFilesystemError(TransactionError):
    """Staging directory and final target are not on the same filesystem."""


class BusyTransactionError(TransactionError):
    """Another process holds the transaction lock past the timeout."""


class InconsistentTransactionError(TransactionError):
    """The on-disk state cannot be mapped to any known crash window."""


class VerificationError(TransactionError):
    """A downloaded byte stream does not match the immutable source plan."""


# ---------------------------------------------------------------------------
# Paths / manifest
# ---------------------------------------------------------------------------


def _staging_names(target_abs):
    # type: (str) -> tuple
    digest = hashlib.sha256(target_abs.encode("utf-8")).hexdigest()[:16]
    parent = os.path.dirname(target_abs)
    # The lock lives *next to* the staging directory, not inside it: staging
    # is removed while the lock is held, so a lock file inside staging would
    # vanish out from under waiters.
    staging = os.path.join(parent, STAGING_PREFIX + digest)
    lock = os.path.join(parent, STAGING_PREFIX + digest + ".lock")
    return parent, staging, lock


def staging_path(target):
    # type: (str) -> str
    """Deterministic sibling staging path so cooperating processes converge."""
    target_abs = os.path.abspath(os.path.normpath(target))
    _parent, staging, _lock = _staging_names(target_abs)
    return staging


def discover(base_dir):
    # type: (str) -> list
    """Return absolute staging directories found directly under ``base_dir``."""
    try:
        names = os.listdir(base_dir)
    except OSError:
        return []
    found = [
        os.path.join(base_dir, name)
        for name in names
        if name.startswith(STAGING_PREFIX) and os.path.isdir(os.path.join(base_dir, name))
    ]
    return sorted(found)


def normalize_entries(entries):
    # type: (list) -> list
    """Return manifest entries with a stable, validated shape."""
    normalized = []
    for raw in entries:
        raw_path = raw["path"]
        if os.path.isabs(raw_path):
            raise TransactionError("absolute entry path in source plan: %r" % raw_path)
        path = raw_path.replace("\\", "/")
        while path.startswith("./"):
            path = path[2:]
        while path.startswith("/"):
            path = path[1:]
        if (not path or path == ".." or path.startswith("../")
                or "/../" in path):
            raise TransactionError("unsafe entry path in source plan: %r" % raw_path)
        normalized.append(
            {
                "path": path,
                "url": raw["url"],
                "git_sha": (raw.get("git_sha") or None),
                "size": raw.get("size"),
            }
        )
    normalized.sort(key=lambda e: e["path"])
    if len({e["path"] for e in normalized}) != len(normalized):
        raise TransactionError("duplicate paths in source plan")
    return normalized


def canonical_entries(entries):
    # type: (list) -> bytes
    """Canonical byte serialisation used for the immutable manifest id."""
    return json.dumps(normalize_entries(entries), sort_keys=True,
                      separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def manifest_id(entries):
    # type: (list) -> str
    return hashlib.sha256(canonical_entries(entries)).hexdigest()


def _manifest_document(entries, mid):
    # type: (list, str) -> bytes
    doc = {"format": FORMAT_VERSION, "manifest_id": mid,
           "entries": normalize_entries(entries)}
    return json.dumps(doc, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False).encode("utf-8")


# ---------------------------------------------------------------------------
# Durability helpers
# ---------------------------------------------------------------------------


def fsync_file(path):
    # type: (str) -> None
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def fsync_dir(path):
    # type: (str) -> None
    """Fsync a directory so that entry creation/removal/rename is durable."""
    if sys.platform == "win32":
        _fsync_dir_windows(path)
        return
    flags = os.O_RDONLY
    flags |= getattr(os, "O_DIRECTORY", 0)
    fd = os.open(path, flags)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _fsync_dir_windows(path):
    # Best effort: directories need FILE_FLAG_BACKUP_SEMANTICS to be opened.
    try:
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.windll.kernel32
        GENERIC_READ = 0x80000000
        FILE_SHARE_ALL = 0x7
        OPEN_EXISTING = 3
        FILE_FLAG_BACKUP_SEMANTICS = 0x02000000
        INVALID_HANDLE = wintypes.HANDLE(-1).value

        kernel32.CreateFileW.restype = wintypes.HANDLE
        handle = kernel32.CreateFileW(
            os.path.abspath(path), GENERIC_READ, FILE_SHARE_ALL, None,
            OPEN_EXISTING, FILE_FLAG_BACKUP_SEMANTICS, None,
        )
        if not handle or handle == INVALID_HANDLE:
            return
        try:
            kernel32.FlushFileBuffers(handle)
        finally:
            kernel32.CloseHandle(handle)
    except Exception:
        # Directory fsync is an extra durability measure on Windows; the file
        # data fsync and the rename ordering still stand.
        pass


def fsync_tree(root):
    # type: (str) -> None
    """Fsync every directory bottom-up, persisting entries in dependency order."""
    dirs = []
    for current, subdirs, _files in os.walk(root):
        dirs.append(current)
    for current in reversed(dirs):
        fsync_dir(current)


# ---------------------------------------------------------------------------
# Hash-chain journal
# ---------------------------------------------------------------------------


class Journal(object):
    """
    Append-only journal.  Every line is ``base64(json_payload).sha256`` where
    the digest chains over the previous line digest::

        digest_n = sha256(prev_digest + "." + payload_n)
    """

    def __init__(self, path, prev_digest, fd):
        self.path = path
        self._prev = prev_digest
        self._fd = fd

    @classmethod
    def create(cls, path):
        # type: (str) -> Journal
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644)
        return cls(path, _GENESIS, fd)

    @classmethod
    def open_append(cls, path, prev_digest):
        fd = os.open(path, os.O_WRONLY | os.O_APPEND)
        return cls(path, prev_digest, fd)

    def append(self, payload):
        # type: (dict) -> str
        body = json.dumps(payload, sort_keys=True, separators=(",", ":"),
                          ensure_ascii=False).encode("utf-8")
        encoded = base64.b64encode(body)
        digest = hashlib.sha256(self._prev.encode("ascii") + b"." + encoded).hexdigest()
        line = encoded + b"." + digest.encode("ascii") + b"\n"
        os.write(self._fd, line)
        os.fsync(self._fd)
        self._prev = digest
        return digest

    def close(self):
        if self._fd is not None:
            os.close(self._fd)
            self._fd = None

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        self.close()
        return False


def replay_journal(path):
    # type: (str) -> tuple
    """
    Validate and replay the journal.

    Returns ``(records, last_digest, torn, corrupt_reason)``.  A torn *tail*
    line (the writer died mid-write) is discarded; corruption anywhere else is
    fatal because the history can no longer be trusted.
    """
    records = []
    prev = _GENESIS
    torn = False
    with open(path, "rb") as handle:
        raw_lines = [line for line in handle.read().split(b"\n") if line]
    for index, line in enumerate(raw_lines):
        try:
            encoded, check = line.rsplit(b".", 1)
        except ValueError:
            encoded = check = b""
        expected = hashlib.sha256(prev.encode("ascii") + b"." + encoded).hexdigest()
        if expected.encode("ascii") != check:
            if index == len(raw_lines) - 1:
                torn = True  # partial append from a crashed writer
                break
            return records, prev, torn, "checksum mismatch at record %d" % index
        try:
            payload = json.loads(base64.b64decode(encoded).decode("utf-8"))
        except Exception:
            if index == len(raw_lines) - 1:
                torn = True
                break
            return records, prev, torn, "undecodable record %d" % index
        records.append(payload)
        prev = check.decode("ascii")
    return records, prev, torn, None


# ---------------------------------------------------------------------------
# Cross-process lock
# ---------------------------------------------------------------------------


class TransactionLock(object):
    """
    Exclusive lock combining an in-process ``threading.Lock`` (POSIX flock is
    per-process, so two threads in one process would otherwise both hold it)
    with an OS advisory lock for cross-process coordination.  The OS lock is
    released automatically if a process dies.
    """

    _guard = threading.Lock()
    _thread_locks = {}

    def __init__(self, path, timeout=None, poll=0.05):
        self.path = os.path.abspath(path)
        self.timeout = timeout
        self.poll = poll
        self._fd = None
        self._thread_lock = None

    @classmethod
    def _thread_lock_for(cls, path):
        with cls._guard:
            lock = cls._thread_locks.get(path)
            if lock is None:
                lock = threading.Lock()
                cls._thread_locks[path] = lock
            return lock

    def __enter__(self):
        self._thread_lock = self._thread_lock_for(self.path)
        if self.timeout is None:
            acquired = self._thread_lock.acquire()
        else:
            acquired = self._thread_lock.acquire(timeout=self.timeout)
        if not acquired:
            self._thread_lock = None
            raise BusyTransactionError("timed out waiting for %s" % self.path)
        self._fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o644)
        try:
            os.write(self._fd, b"\0")
        except OSError:
            pass
        deadline = None if self.timeout is None else time.monotonic() + self.timeout
        try:
            while True:
                try:
                    if sys.platform == "win32":
                        self._lock_windows(block=False)
                    else:
                        import fcntl
                        fcntl.flock(self._fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    return self
                except (OSError, IOError):
                    if deadline is not None and time.monotonic() >= deadline:
                        raise BusyTransactionError(
                            "timed out waiting for %s" % self.path)
                    time.sleep(self.poll)
        except BaseException:
            self._release_both()
            raise

    def __exit__(self, exc_type, exc, tb):
        self._release_both()
        return False

    def _release_both(self):
        if self._fd is not None:
            try:
                if sys.platform == "win32":
                    self._lock_windows(unlock=True)
                else:
                    import fcntl
                    fcntl.flock(self._fd, fcntl.LOCK_UN)
            except OSError:
                pass
            os.close(self._fd)
            self._fd = None
        if self._thread_lock is not None:
            self._thread_lock.release()
            self._thread_lock = None
        # Best-effort reclamation of the 0-byte lock file, only once the
        # transaction directory itself is gone (state is final and stable).
        # Failures (waiters holding the inode, Windows sharing) are ignored.
        if sys.platform == "win32":
            return
        staging_dir = self.path[:-len(".lock")] if self.path.endswith(".lock") else None
        if staging_dir and not os.path.exists(staging_dir):
            try:
                os.unlink(self.path)
            except OSError:
                pass

    def _lock_windows(self, block=False, unlock=False):
        import msvcrt
        os.lseek(self._fd, 0, os.SEEK_SET)
        mode = msvcrt.LK_UNLCK if unlock else msvcrt.LK_NBLCK
        msvcrt.locking(self._fd, mode, 1)


# ---------------------------------------------------------------------------
# Status
# ---------------------------------------------------------------------------

STAGE_ABSENT = "absent"
STAGE_EMPTY = "empty"          # staging exists, journal does not
STAGE_ACTIVE = "active"        # BEGIN (+files), not prepared
STAGE_PREPARED = "prepared"    # full tree durable, rename pending/done
STAGE_COMMITTED = "committed"  # rename durable, cleanup pending
STAGE_CLEANED = "cleaned"      # journal says done, staging remnant only
STAGE_CORRUPT = "corrupt"


class Status(object):
    def __init__(self):
        self.stage = STAGE_ABSENT
        self.target = None
        self.staging = None
        self.manifest_id = None
        self.expected_files = 0
        self.total_bytes = 0
        self.files = {}          # path -> {sha256, git_sha, size}
        self.torn = False
        self.corrupt_reason = None
        self.target_exists = False
        self.root_exists = False
        self.manifest_file_ok = None

    def as_dict(self, requested_manifest_id=None):
        match = None
        if self.manifest_id is not None and requested_manifest_id is not None:
            match = (self.manifest_id == requested_manifest_id)
        return {
            "stage": self.stage,
            "target": self.target,
            "staging": self.staging,
            "manifest_id": self.manifest_id,
            "requested_manifest_id": requested_manifest_id,
            "manifest_matches": match,
            "expected_files": self.expected_files,
            "files_done": len(self.files),
            "total_bytes": self.total_bytes,
            "torn_tail": self.torn,
            "corrupt_reason": self.corrupt_reason,
            "target_exists": self.target_exists,
            "root_exists": self.root_exists,
            "manifest_file_ok": self.manifest_file_ok,
        }


def inspect_target(target, lock_timeout=2.0):
    # type: (str, float) -> Status
    """Read-only status reconstruction. Never mutates the staging directory."""
    target_abs = os.path.abspath(os.path.normpath(target))
    _parent, staging, lock_file = _staging_names(target_abs)
    if not os.path.isdir(staging):
        return _read_status(target_abs, staging)

    # A short lock attempt keeps a live writer from confusing the report, but
    # report must stay usable even while another process holds the lock: the
    # torn-tail rule makes concurrent reads safe.
    lock = None
    if lock_timeout:
        try:
            lock = TransactionLock(lock_file, timeout=lock_timeout)
            lock.__enter__()
        except BusyTransactionError:
            lock = None
    try:
        return _read_status(target_abs, staging)
    finally:
        if lock is not None:
            lock.__exit__(None, None, None)


def _read_status(target_abs, staging):
    """Reconstruct status from disk. Caller is expected to hold the lock."""
    status = Status()
    status.target = target_abs
    status.staging = staging
    status.target_exists = os.path.lexists(target_abs)
    root = os.path.join(staging, ROOT_NAME)
    status.root_exists = os.path.isdir(root)
    if not os.path.isdir(staging):
        return status

    journal_path = os.path.join(staging, JOURNAL_NAME)
    if not os.path.exists(journal_path):
        status.stage = STAGE_EMPTY
        return status

    try:
        records, _last, torn, corrupt = replay_journal(journal_path)
    except OSError as exc:
        status.stage = STAGE_CORRUPT
        status.corrupt_reason = "journal unreadable: %s" % exc
        return status

    status.torn = torn
    if corrupt:
        status.stage = STAGE_CORRUPT
        status.corrupt_reason = corrupt
        return status

    for rec in records:
        kind = rec.get("t")
        if kind == "BEGIN":
            status.manifest_id = rec.get("manifest_id")
            status.expected_files = rec.get("files", 0)
            status.total_bytes = rec.get("bytes", 0)
        elif kind == "FILE":
            status.files[rec["p"]] = {
                "sha256": rec.get("sha256"),
                "git_sha": rec.get("git_sha"),
                "size": rec.get("size"),
            }
        elif kind == "PREPARED":
            status.stage = STAGE_PREPARED
        elif kind == "COMMITTED":
            status.stage = STAGE_COMMITTED
        elif kind == "CLEANED":
            status.stage = STAGE_CLEANED

    if status.stage == STAGE_ABSENT:
        status.stage = STAGE_ACTIVE if status.manifest_id else STAGE_CORRUPT
        if status.stage == STAGE_CORRUPT:
            status.corrupt_reason = "journal has no BEGIN record"

    manifest_doc = os.path.join(staging, MANIFEST_NAME)
    if os.path.exists(manifest_doc) and status.manifest_id:
        try:
            with open(manifest_doc, "rb") as handle:
                doc = json.loads(handle.read().decode("utf-8"))
            status.manifest_file_ok = (doc.get("manifest_id") == status.manifest_id)
            if status.manifest_file_ok is False and status.stage != STAGE_CORRUPT:
                status.stage = STAGE_CORRUPT
                status.corrupt_reason = "manifest.json does not match journal"
        except Exception:
            status.manifest_file_ok = False
    return status


# ---------------------------------------------------------------------------
# Content verification
# ---------------------------------------------------------------------------


def _git_blob_header(entry):
    size = entry.get("size")
    if entry.get("git_sha") and isinstance(size, int):
        return ("blob %d\0" % size).encode("ascii")
    return None


def verify_existing(path, entry, recorded_sha256=None):
    # type: (str, dict, str) -> str
    """Re-hash a staged/final file against the plan. Returns its sha256."""
    sha256 = hashlib.sha256()
    git = hashlib.sha1()
    header = _git_blob_header(entry)
    if header is None and entry.get("git_sha"):
        raise VerificationError("%s: size unknown, cannot verify git sha" % entry["path"])
    if header:
        git.update(header)
    count = 0
    with open(path, "rb") as handle:
        while True:
            chunk = handle.read(_READ_CHUNK)
            if not chunk:
                break
            count += len(chunk)
            sha256.update(chunk)
            git.update(chunk)
    if entry.get("size") is not None and count != entry["size"]:
        raise VerificationError(
            "%s: size %d != planned %d" % (entry["path"], count, entry["size"]))
    if entry.get("git_sha") and git.hexdigest() != entry["git_sha"].lower():
        raise VerificationError("%s: git blob sha mismatch" % entry["path"])
    if recorded_sha256 and sha256.hexdigest() != recorded_sha256:
        raise VerificationError("%s: sha256 does not match journal" % entry["path"])
    return sha256.hexdigest()


def _download_verified(fetcher, entry, dest):
    """Stream one source into ``dest`` while hashing; fsync before reporting."""
    header = _git_blob_header(entry)
    if entry.get("git_sha") and header is None:
        raise VerificationError("%s: size unknown, cannot verify git sha" % entry["path"])
    sha256 = hashlib.sha256()
    git = hashlib.sha1()
    if header:
        git.update(header)
    count = 0
    parent = os.path.dirname(dest)
    if parent and not os.path.isdir(parent):
        os.makedirs(parent, exist_ok=True)
    with fetcher(entry["url"]) as source, open(dest, "wb") as out:
        while True:
            chunk = source.read(_READ_CHUNK)
            if not chunk:
                break
            count += len(chunk)
            sha256.update(chunk)
            git.update(chunk)
            out.write(chunk)
        out.flush()
        os.fsync(out.fileno())
    if entry.get("size") is not None and count != entry["size"]:
        raise VerificationError(
            "%s: downloaded %d bytes, planned %d" % (entry["path"], count, entry["size"]))
    if entry.get("git_sha") and git.hexdigest() != entry["git_sha"].lower():
        raise VerificationError(
            "%s: expected git sha %s, got %s"
            % (entry["path"], entry["git_sha"], git.hexdigest()))
    return {
        "t": "FILE", "p": entry["path"], "sha256": sha256.hexdigest(),
        "git_sha": git.hexdigest() if header else None, "size": count,
    }


# ---------------------------------------------------------------------------
# Atomic rename (no-replace semantics on every platform)
# ---------------------------------------------------------------------------


def _rename_noreplace(src, dst):
    """Rename ``src`` to ``dst``, failing rather than replacing ``dst``."""
    if sys.platform.startswith("linux") and _renameat2_noreplace(src, dst):
        return
    _rename_with_retry(src, dst)


def _renameat2_noreplace(src, dst):
    """Use renameat2(RENAME_NOREPLACE) on Linux. Returns False if unavailable."""
    try:
        import ctypes

        libc = ctypes.CDLL(None, use_errno=True)
        RENAME_NOREPLACE = 1
        AT_FDCWD = -100
        result = libc.renameat2(
            AT_FDCWD, os.fsencode(src), AT_FDCWD, os.fsencode(dst), RENAME_NOREPLACE
        )
        if result == 0:
            return True
        code = ctypes.get_errno()
        if code == errno.EEXIST:
            raise TargetExistsError("target already exists: %s" % dst)
        if code in (errno.ENOSYS, errno.EINVAL, errno.EPERM, errno.ENOTSUP):
            return False  # kernel/fs does not support it; use safe fallback
        raise OSError(code, os.strerror(code), dst)
    except AttributeError:
        return False


def _rename_with_retry(src, dst):
    if os.path.lexists(dst):
        raise TargetExistsError("target already exists: %s" % dst)
    attempts = 10 if sys.platform == "win32" else 1
    for attempt in range(attempts):
        try:
            os.rename(src, dst)  # Windows MoveFileExW without REPLACE_EXISTING
            return
        except FileExistsError:
            raise TargetExistsError("target already exists: %s" % dst)
        except OSError as exc:
            winerror = getattr(exc, "winerror", None)
            if winerror in (5, 32) and attempt < attempts - 1:
                # Access denied / sharing violation: indexer or AV briefly
                # holds a handle. Back off and retry instead of abandoning the
                # all-or-nothing publish.
                time.sleep(0.1 * (attempt + 1))
                continue
            raise


# ---------------------------------------------------------------------------
# Transaction engine
# ---------------------------------------------------------------------------


class _Transaction(object):
    def __init__(self, target, entries, fetcher, lock_timeout=None, progress=None):
        self.target = os.path.abspath(os.path.normpath(target))
        self.entries = normalize_entries(entries)
        self.fetcher = fetcher
        self.lock_timeout = lock_timeout
        self.progress = progress or (lambda event, entry: None)
        self.parent, self.staging, self.lock_path = _staging_names(self.target)
        self.root = os.path.join(self.staging, ROOT_NAME)
        self.journal_path = os.path.join(self.staging, JOURNAL_NAME)
        self.manifest_path = os.path.join(self.staging, MANIFEST_NAME)
        self.mid = manifest_id(self.entries)
        self.total_bytes = sum(e.get("size") or 0 for e in self.entries)

    # -- entry points -------------------------------------------------------

    def run_fresh_or_join(self):
        if os.path.lexists(self.target):
            raise TargetExistsError("target already exists: %s" % self.target)
        for _attempt in range(2):
            if not os.path.isdir(self.parent):
                os.makedirs(self.parent, exist_ok=True)
            newly_created = True
            try:
                os.mkdir(self.staging)
                fsync_dir(self.parent)
            except FileExistsError:
                if not os.path.isdir(self.staging):
                    raise
                newly_created = False
            try:
                with TransactionLock(self.lock_path, timeout=self.lock_timeout):
                    # A peer may have finished publish + cleanup while waited.
                    if os.path.lexists(self.target):
                        if newly_created:
                            # We re-created the staging dir just after the
                            # peer's cleanup; remove our own empty residue.
                            shutil.rmtree(self.staging, ignore_errors=True)
                            fsync_dir(self.parent)
                        return "joined"
                    status = _read_status(self.target, self.staging)
                    if newly_created and status.stage == STAGE_EMPTY:
                        return self._begin_and_publish()
                    return self._drive_status(status)
            except FileNotFoundError:
                # The peer removed the staging directory (including the lock
                # file) between our election and locking. Re-elect once.
                if os.path.isdir(self.staging) or _attempt == 1:
                    raise
                continue
        raise InconsistentTransactionError("election race unresolved for %s" % self.target)

    def resume(self):
        if not os.path.isdir(self.staging):
            return self.run_fresh_or_join()
        try:
            with TransactionLock(self.lock_path, timeout=self.lock_timeout):
                status = _read_status(self.target, self.staging)
                return self._drive_status(status, resuming=True)
        except FileNotFoundError:
            if not os.path.isdir(self.staging):
                return self.run_fresh_or_join()
            raise

    def cleanup(self):
        """Remove staging residue. The final target is never touched."""
        with TransactionLock(self.lock_path, timeout=self.lock_timeout):
            status = _read_status(self.target, self.staging)
            self._require_matching(status)
            if status.stage == STAGE_ABSENT:
                return "absent"
            if status.stage == STAGE_CORRUPT:
                raise CorruptJournalError(status.corrupt_reason or "corrupt journal")
            if status.stage in (STAGE_COMMITTED, STAGE_CLEANED):
                # The target is the source of truth: verify before sweeping.
                self._verify_final_tree()
            shutil.rmtree(self.staging, ignore_errors=False)
            fsync_dir(self.parent)
            return "cleaned"

    # -- shared state machine ----------------------------------------------

    def _drive_status(self, status, resuming=False):
        if status.stage == STAGE_ABSENT:
            # No staging state: either we just reserved it, or a cooperating
            # process finished publish + cleanup while we waited on the lock.
            if status.target_exists:
                return "joined"
            return self._begin_and_publish()
        if status.stage in (STAGE_COMMITTED, STAGE_CLEANED):
            return self._finish_after_commit(status)
        if status.stage == STAGE_CORRUPT:
            raise CorruptJournalError(status.corrupt_reason or "corrupt journal")
        if status.stage == STAGE_EMPTY:
            # Died between mkdir(stationg) and BEGIN: nothing of value exists.
            shutil.rmtree(self.staging, ignore_errors=True)
            fsync_dir(self.parent)
            os.mkdir(self.staging)
            fsync_dir(self.parent)
            return self._begin_and_publish()
        # active or prepared: only the identical immutable plan may continue.
        self._require_matching(status)
        if status.target_exists and not status.root_exists:
            # Crash window between the rename and the COMMITTED record.
            self._verify_final_tree()
            self._write_commit_then_sweep()
            return "recovered-committed"
        if status.target_exists and status.root_exists:
            raise InconsistentTransactionError(
                "both staging tree and final target exist for %s" % self.target)
        if not status.root_exists:
            raise InconsistentTransactionError(
                "journal predates the staging tree for %s" % self.target)
        self._populate_and_publish(status)
        return "resumed" if resuming else "joined"

    def _require_matching(self, status):
        if status.manifest_id and status.manifest_id != self.mid:
            raise ManifestMismatchError(
                "requested manifest %s does not match recorded plan %s for %s"
                % (self.mid, status.manifest_id, self.target))

    # -- fresh build --------------------------------------------------------

    def _begin_and_publish(self):
        with open(self.manifest_path, "wb") as handle:
            handle.write(_manifest_document(self.entries, self.mid))
            handle.flush()
            os.fsync(handle.fileno())
        fsync_dir(self.staging)

        journal = Journal.create(self.journal_path)
        journal.append({
            "t": "BEGIN", "v": FORMAT_VERSION, "target": self.target,
            "manifest_id": self.mid, "files": len(self.entries),
            "bytes": self.total_bytes, "ts": int(time.time()),
        })
        try:
            os.mkdir(self.root)
            fsync_dir(self.staging)
            status = _read_status(self.target, self.staging)
            self._populate_and_publish(status, journal=journal, recorded={})
        finally:
            journal.close()
        return "published"

    def _open_journal_for_append(self):
        _records, last_digest, _torn, corrupt = replay_journal(self.journal_path)
        if corrupt:
            raise CorruptJournalError(corrupt)
        return Journal.open_append(self.journal_path, last_digest), {
            r["p"]: r for r in _records if r.get("t") == "FILE"}

    def _populate_and_publish(self, status, journal=None, recorded=None):
        own_journal = journal is None
        if own_journal:
            journal, recorded = self._open_journal_for_append()
        elif recorded is None:
            recorded = {}
        try:
            planned = set()
            for entry in self.entries:
                planned.add(entry["path"])
                dest = os.path.join(self.root, entry["path"])
                record = recorded.get(entry["path"])
                if os.path.exists(dest):
                    try:
                        sha = verify_existing(
                            dest, entry,
                            record.get("sha256") if record else None)
                        if record is None:
                            journal.append({
                                "t": "FILE", "p": entry["path"], "sha256": sha,
                                "git_sha": entry.get("git_sha"),
                                "size": entry.get("size"),
                            })
                        self.progress("verified", entry)
                        continue
                    except (VerificationError, OSError):
                        pass  # re-download this file
                file_record = _download_verified(self.fetcher, entry, dest)
                journal.append(file_record)
                self.progress("downloaded", entry)

            self._sweep_unplanned(planned)
            fsync_tree(self.root)
            fsync_dir(self.staging)
            if status.stage != STAGE_PREPARED:
                journal.append({
                    "t": "PREPARED",
                    "root_sha256": self._planned_tree_digest(),
                })
            self._publish(journal)
        finally:
            if own_journal:
                journal.close()

    def _sweep_unplanned(self, planned):
        for current, _subdirs, files in os.walk(self.root):
            for name in files:
                full = os.path.join(current, name)
                rel = os.path.relpath(full, self.root).replace("\\", "/")
                if rel not in planned:
                    os.remove(full)

    def _planned_tree_digest(self):
        h = hashlib.sha256()
        for entry in self.entries:
            h.update(entry["path"].encode("utf-8"))
            h.update(b"\0")
            h.update((entry.get("git_sha") or "").encode("utf-8"))
            h.update(b"\0")
            h.update(str(entry.get("size") if entry.get("size") is not None else "")
                     .encode("utf-8"))
            h.update(b"\n")
        return h.hexdigest()

    # -- publish / crash windows -------------------------------------------

    def _publish(self, journal):
        if os.path.lexists(self.target):
            raise TargetExistsError("target appeared before publish: %s" % self.target)
        if os.stat(self.staging).st_dev != os.stat(self.parent).st_dev:
            raise SameFilesystemError(
                "staging %s and target %s are on different filesystems"
                % (self.staging, self.target))
        _rename_noreplace(self.root, self.target)
        try:
            fsync_dir(self.parent)
        except OSError:
            # The rename itself already happened; make the best effort to mark
            # it so a later run only has to validate the final tree.
            try:
                journal.append({"t": "COMMITTED"})
            except OSError:
                pass
            raise
        journal.append({"t": "COMMITTED"})
        journal.append({"t": "CLEANED"})
        journal.close()
        shutil.rmtree(self.staging, ignore_errors=False)
        fsync_dir(self.parent)

    def _finish_after_commit(self, status):
        # Rename durable, journal already (or almost) records it. Validate the
        # final tree and sweep staging residue; never touch target contents.
        self._verify_final_tree()
        self._write_commit_then_sweep()
        return "recovered-committed"

    def _write_commit_then_sweep(self):
        if not os.path.exists(self.journal_path):
            if os.path.isdir(self.staging):
                shutil.rmtree(self.staging, ignore_errors=True)
                fsync_dir(self.parent)
            return
        records, last_digest, _torn, corrupt = replay_journal(self.journal_path)
        if corrupt:
            raise CorruptJournalError(corrupt)
        kinds = [r.get("t") for r in records]
        if "CLEANED" not in kinds:
            with Journal.open_append(self.journal_path, last_digest) as journal:
                if "COMMITTED" not in kinds:
                    journal.append({"t": "COMMITTED"})
                journal.append({"t": "CLEANED"})
        shutil.rmtree(self.staging, ignore_errors=False)
        fsync_dir(self.parent)

    def _verify_final_tree(self):
        if not os.path.isdir(self.target):
            raise InconsistentTransactionError(
                "journal claims commit but target is missing: %s" % self.target)
        planned = set()
        for entry in self.entries:
            planned.add(entry["path"])
            path = os.path.join(self.target, entry["path"])
            verify_existing(path, entry)
        for current, _subdirs, files in os.walk(self.target):
            for name in files:
                full = os.path.join(current, name)
                rel = os.path.relpath(full, self.target).replace("\\", "/")
                if rel not in planned:
                    raise InconsistentTransactionError(
                        "unexpected file in final tree: %s" % full)


# ---------------------------------------------------------------------------
# Public convenience API
# ---------------------------------------------------------------------------


def run(target, entries, fetcher, lock_timeout=None, progress=None):
    """Run the transaction for ``target``; join/continue a cooperating one."""
    tx = _Transaction(target, entries, fetcher, lock_timeout=lock_timeout,
                      progress=progress)
    return tx.run_fresh_or_join()


def resume(target, entries, fetcher, lock_timeout=None, progress=None):
    """Deterministically continue an interrupted transaction."""
    tx = _Transaction(target, entries, fetcher, lock_timeout=lock_timeout,
                      progress=progress)
    return tx.resume()


def cleanup(target, entries, lock_timeout=None):
    """Remove a matching interrupted transaction's staging directory."""
    tx = _Transaction(target, entries, fetcher=None, lock_timeout=lock_timeout)
    return tx.cleanup()


def report(target, requested_entries=None, lock_timeout=2.0):
    """Read-only recovery report; always permitted, even on mismatch."""
    requested_id = manifest_id(requested_entries) if requested_entries else None
    return inspect_target(target, lock_timeout=lock_timeout).as_dict(requested_id)

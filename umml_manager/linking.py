"""Hardlink-based file moves and an identity-keyed hash cache for Apply.

Hardlinks: game files, prepared files, baselines and recovery snapshots all
live on the same drive in the normal setup, so "copy this file there" can be
a hardlink: instant, and no second copy on disk. The Manager never writes
into a file in place (it always writes a temp file and renames it), so a
file shared by several names is never changed through one of them by the
Manager itself. When a link is impossible (different drive, FAT/exFAT,
policy), the regular atomic copy is used instead.

Hash cache: SHA-256 keyed by file identity (volume + file ID) and checked
against size and modification time. A hardlinked file is one file, so it is
hashed once instead of once per name. It is also the drift check: if
anything replaces a game file (new file ID) or writes into it (new mtime or
size), the cache misses and the file is hashed again.
"""

from __future__ import annotations

import json
import os
import stat
import threading
from pathlib import Path

from .safety import SafetyError, atomic_copy_file, hash_file

CACHE_FILE = "hash-cache.json"
_CACHE_VERSION = 1


def link_or_copy(source: str | Path, target: str | Path) -> bool:
    """Atomically make ``target`` the same content as ``source``.

    Returns True when a hardlink was used, False when the file was copied.
    """

    source_path = Path(source)
    target_path = Path(target)
    try:
        mode = source_path.lstat().st_mode
    except OSError as exc:
        raise SafetyError(f"Source file is unavailable: {source_path}: {exc}") from exc
    if not stat.S_ISREG(mode):
        raise SafetyError(f"Source is not a regular file: {source_path}")
    target_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = target_path.with_name(f".{target_path.name}.link-{os.getpid()}.tmp")
    try:
        temporary.unlink(missing_ok=True)
        os.link(source_path, temporary)
        os.replace(temporary, target_path)
        return True
    except OSError:
        temporary.unlink(missing_ok=True)
    atomic_copy_file(source_path, target_path)
    return False


def same_file(first: str | Path, second: str | Path) -> bool:
    try:
        return os.path.samefile(first, second)
    except OSError:
        return False


class HashCache:
    """SHA-256 per file identity, invalidated by size or mtime changes."""

    def __init__(self, path: Path | None = None):
        self._path = path
        self._lock = threading.Lock()
        self._entries: dict[str, list] = {}
        self._dirty = False
        self.hits = 0
        self.misses = 0
        if path is not None and path.is_file():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(data, dict) and data.get("version") == _CACHE_VERSION:
                    entries = data.get("entries", {})
                    if isinstance(entries, dict):
                        self._entries = {
                            str(key): value
                            for key, value in entries.items()
                            if isinstance(value, list) and len(value) == 3
                        }
            except (OSError, ValueError):
                self._entries = {}

    @staticmethod
    def _identity(info: os.stat_result) -> str | None:
        # Some filesystems (FAT/exFAT, some network shares) have no stable
        # file ID; never cache those.
        if not info.st_ino:
            return None
        return f"{info.st_dev}:{info.st_ino}"

    def sha256(self, path: str | Path) -> str:
        file_path = Path(path)
        info = file_path.stat()
        identity = self._identity(info)
        if identity is not None:
            with self._lock:
                cached = self._entries.get(identity)
            if cached and cached[0] == info.st_size and cached[1] == info.st_mtime_ns:
                self.hits += 1
                return str(cached[2])
        self.misses += 1
        digest = hash_file(file_path)
        if identity is not None:
            after = file_path.stat()
            # Only cache if the file did not change while being hashed.
            if (after.st_size, after.st_mtime_ns) == (info.st_size, info.st_mtime_ns):
                with self._lock:
                    self._entries[identity] = [info.st_size, info.st_mtime_ns, digest]
                    self._dirty = True
        return digest

    def forget(self, path: str | Path) -> None:
        try:
            identity = self._identity(Path(path).stat())
        except OSError:
            return
        if identity is not None:
            with self._lock:
                if self._entries.pop(identity, None) is not None:
                    self._dirty = True

    def save(self) -> None:
        if self._path is None or not self._dirty:
            return
        with self._lock:
            payload = {"version": _CACHE_VERSION, "entries": self._entries}
            self._path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self._path.with_name(f".{self._path.name}.tmp-{os.getpid()}")
            try:
                temporary.write_text(json.dumps(payload, separators=(",", ":")), encoding="utf-8")
                os.replace(temporary, self._path)
                self._dirty = False
            finally:
                temporary.unlink(missing_ok=True)

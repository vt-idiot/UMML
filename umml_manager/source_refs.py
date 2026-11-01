"""Mod sources referenced in place instead of copied into the library.

A referenced mod keeps only a ``SourceOrigin`` (path + sha256 + a cheap
summary). The Manager reads the files from where the user keeps them, for
example an external drive, and notices when they move or change.

Status meanings:
  library  - copied into the Manager's sources folder (the old behavior)
  online   - referenced, present, and unchanged
  offline  - referenced, but nothing is at the recorded path
  changed  - referenced, something is there, but it is not the same content
"""

from __future__ import annotations

import os
import shutil
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass, replace
from pathlib import Path, PurePosixPath
from typing import Callable, Iterator

from .models import ORIGIN_ARCHIVE, ORIGIN_FOLDER, ModRecord, SourceOrigin
from .safety import SafetyError, hash_file, tree_digest, validate_regular_tree

STATUS_LIBRARY = "library"
STATUS_ONLINE = "online"
STATUS_OFFLINE = "offline"
STATUS_CHANGED = "changed"

ARCHIVE_SUFFIXES = (".zip", ".tar", ".tar.gz", ".tgz")


class SourceUnavailableError(RuntimeError):
    """The referenced source is offline or no longer matches its hash."""

    def __init__(self, record: ModRecord, status: str, detail: str = ""):
        self.record = record
        self.status = status
        where = record.origin.path if record.origin else record.source_path
        if status == STATUS_OFFLINE:
            message = (
                f"Source for {record.name} is offline: {where}\n"
                "Reconnect the drive, or use Locate source / Find missing sources."
            )
        else:
            message = (
                f"Source for {record.name} changed since it was imported: {where}\n"
                "Locate the original file, or import the new one as a new version."
            )
        if detail:
            message = f"{message}\n{detail}"
        super().__init__(message)


@dataclass(frozen=True)
class FolderSummary:
    file_count: int
    size_bytes: int
    mtime_ns: int


def summarize_folder(root: Path) -> FolderSummary:
    count = size = newest = 0
    for directory, _dirs, files in os.walk(root):
        for name in files:
            info = os.stat(os.path.join(directory, name), follow_symlinks=False)
            count += 1
            size += info.st_size
            newest = max(newest, info.st_mtime_ns)
    return FolderSummary(count, size, newest)


def folder_origin(root: Path) -> SourceOrigin:
    root = root.resolve()
    summary = summarize_folder(root)
    return SourceOrigin(
        kind=ORIGIN_FOLDER,
        path=str(root),
        sha256=tree_digest(root),
        size_bytes=summary.size_bytes,
        file_count=summary.file_count,
        mtime_ns=summary.mtime_ns,
    )


def archive_origin(archive: Path, *, sha256: str = "", subpath: str = "") -> SourceOrigin:
    archive = archive.resolve()
    info = archive.stat()
    return SourceOrigin(
        kind=ORIGIN_ARCHIVE,
        path=str(archive),
        sha256=sha256 or hash_file(archive),
        subpath=subpath,
        size_bytes=info.st_size,
        mtime_ns=info.st_mtime_ns,
    )


def _origin_matches(origin: SourceOrigin, path: Path) -> tuple[bool, SourceOrigin]:
    """Return (same content?, origin refreshed with the current cheap stats)."""

    if origin.kind == ORIGIN_ARCHIVE:
        info = path.stat()
        if info.st_size != origin.size_bytes:
            return False, origin
        if info.st_mtime_ns == origin.mtime_ns:
            return True, origin
        # Same size, new timestamp (copied to a new drive, touched): hash it.
        if hash_file(path) != origin.sha256:
            return False, origin
        return True, replace(origin, mtime_ns=info.st_mtime_ns)

    summary = summarize_folder(path)
    if (summary.file_count, summary.size_bytes) != (origin.file_count, origin.size_bytes):
        return False, origin
    if summary.mtime_ns == origin.mtime_ns:
        return True, origin
    try:
        same = tree_digest(path) == origin.sha256
    except SafetyError:
        return False, origin
    if not same:
        return False, origin
    return True, replace(origin, mtime_ns=summary.mtime_ns)


def _expected_kind(path: Path, origin: SourceOrigin) -> bool:
    if origin.kind == ORIGIN_ARCHIVE:
        return path.is_file() and not path.is_symlink()
    return path.is_dir() and not path.is_symlink()


def source_status(record: ModRecord) -> str:
    origin = record.origin
    if origin is None:
        return STATUS_LIBRARY
    path = Path(origin.path)
    try:
        if not _expected_kind(path, origin):
            return STATUS_OFFLINE
        same, _ = _origin_matches(origin, path)
    except OSError:
        return STATUS_OFFLINE
    return STATUS_ONLINE if same else STATUS_CHANGED


def browsable_root(record: ModRecord) -> Path | None:
    """Mod root that can be read directly without extracting, if any."""

    if record.origin is None:
        path = Path(record.source_path).expanduser()
        return path if path.is_dir() else None
    if record.origin.kind != ORIGIN_FOLDER:
        return None
    path = Path(record.origin.path)
    return path if path.is_dir() else None


@contextmanager
def open_source(
    record: ModRecord,
    *,
    scratch_parent: Path,
    extract: Callable[[Path, Path], None],
    find_root: Callable[[Path], Path],
    on_refresh: Callable[[ModRecord], None] | None = None,
) -> Iterator[Path]:
    """Yield a directory containing the mod root files.

    Copied mods yield their library folder. Referenced folders are verified and
    used in place. Referenced archives are verified, extracted to a scratch
    folder that is removed afterwards, and the recorded subpath is yielded.
    """

    origin = record.origin
    if origin is None:
        root = Path(record.source_path).expanduser()
        if not root.is_dir():
            raise SourceUnavailableError(record, STATUS_OFFLINE)
        yield root
        return

    path = Path(origin.path)
    try:
        if not _expected_kind(path, origin):
            raise SourceUnavailableError(record, STATUS_OFFLINE)
        same, refreshed = _origin_matches(origin, path)
    except OSError as exc:
        raise SourceUnavailableError(record, STATUS_OFFLINE, str(exc)) from exc
    if not same:
        raise SourceUnavailableError(record, STATUS_CHANGED)
    if refreshed != origin and on_refresh is not None:
        on_refresh(replace(record, origin=refreshed))

    if origin.kind == ORIGIN_FOLDER:
        validate_regular_tree(path)
        yield path
        return

    scratch_parent.mkdir(parents=True, exist_ok=True)
    scratch = Path(tempfile.mkdtemp(prefix="umml-source-", dir=scratch_parent))
    try:
        extract(path, scratch)
        root = scratch / PurePosixPath(origin.subpath) if origin.subpath else find_root(scratch)
        if not root.is_dir():
            raise SourceUnavailableError(
                record, STATUS_CHANGED, f"Mod folder {origin.subpath!r} is missing from the archive."
            )
        yield root
    finally:
        shutil.rmtree(scratch, ignore_errors=True)


def verify_candidate(origin: SourceOrigin, candidate: Path) -> SourceOrigin | None:
    """Return the origin re-pointed at ``candidate`` if it is the same content."""

    candidate = Path(candidate).expanduser()
    try:
        if not _expected_kind(candidate, origin):
            return None
        if origin.kind == ORIGIN_ARCHIVE:
            info = candidate.stat()
            if info.st_size != origin.size_bytes or hash_file(candidate) != origin.sha256:
                return None
            return replace(origin, path=str(candidate.resolve()), mtime_ns=info.st_mtime_ns)
        summary = summarize_folder(candidate)
        if (summary.file_count, summary.size_bytes) != (origin.file_count, origin.size_bytes):
            return None
        if tree_digest(candidate) != origin.sha256:
            return None
        return replace(origin, path=str(candidate.resolve()), mtime_ns=summary.mtime_ns)
    except (OSError, SafetyError):
        return None


def find_moved_sources(
    records: list[ModRecord],
    search_root: Path,
    *,
    progress: Callable[[str], None] | None = None,
) -> dict[str, SourceOrigin]:
    """Search a folder tree for the files behind offline/changed records.

    Only candidates with the right size (archives) or the right file count and
    total size (folders) are hashed, so a big drive is cheap to scan.
    """

    wanted = [
        record for record in records
        if record.origin is not None and source_status(record) != STATUS_ONLINE
    ]
    archives_by_size: dict[int, list[ModRecord]] = {}
    folders_by_shape: dict[tuple[int, int], list[ModRecord]] = {}
    for record in wanted:
        origin = record.origin
        if origin.kind == ORIGIN_ARCHIVE:
            archives_by_size.setdefault(origin.size_bytes, []).append(record)
        else:
            folders_by_shape.setdefault((origin.file_count, origin.size_bytes), []).append(record)

    found: dict[str, SourceOrigin] = {}
    totals: dict[str, list[int]] = {}
    for directory, dirs, files in os.walk(search_root, topdown=False, followlinks=False):
        count = size = 0
        for name in files:
            full = os.path.join(directory, name)
            try:
                info = os.stat(full, follow_symlinks=False)
            except OSError:
                continue
            count += 1
            size += info.st_size
            for record in archives_by_size.get(info.st_size, ()):
                if record.id in found:
                    continue
                if progress:
                    progress(f"Checking {full}")
                match = verify_candidate(record.origin, Path(full))
                if match is not None:
                    found[record.id] = match
        for name in dirs:
            child = totals.pop(os.path.join(directory, name), None)
            if child:
                count += child[0]
                size += child[1]
        totals[directory] = [count, size]
        for record in folders_by_shape.get((count, size), ()):
            if record.id in found:
                continue
            if progress:
                progress(f"Checking {directory}")
            match = verify_candidate(record.origin, Path(directory))
            if match is not None:
                found[record.id] = match
        if len(found) == len(wanted):
            break
    return found

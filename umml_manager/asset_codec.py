"""Single-pass asset resolution and encryption for Manager preparation.

The legacy core (UMML_core.ModLoaderGUI.decrypt_assets_internal) re-opens the
metadata database for every call, and for a Unity bundle whose path is not in
metadata it parses the whole bundle with UnityPy to read its internal name.
The Manager used to call it once for the whole mod and again once per source
file, then fall back to its own lookup, so an unmatched bundle could be parsed
three times per prepare.

This module follows the same rules as UMML 2.0.3 (lookup order, accepted
files, already-encrypted passthrough, recompression) and produces the same
bytes, but:

* loads metadata table ``a`` into memory once per prepare;
* resolves every source file exactly once;
* caches UnityPy bundle names by file content hash, so a bundle is parsed at
  most once ever, not once per prepare.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import struct
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterator

AB_KEY = b"\x53\x2B\x46\x31\xE4\xA7\xB9\x47\x3E\x7C\xFB"
AUDIO_VIDEO_SUFFIXES = (".acb", ".awb", ".usm")
UNITY_MAGIC = b"UnityFS"
ASSET_MAGICS = (b"UnityFS", b"@UTF", b"AFS2", b"CRID")
# Files UMML 2.0.3 never treats as assets.
IGNORED_NAMES = frozenset(
    ("setting.yml", "setting.json", "meta.json", "mod_pack.png", "desktop.ini", ".ds_store", "thumbs.db")
)
IGNORED_SUFFIXES = (".txt", ".md", ".png", ".jpg", ".jpeg", ".zip", ".bat", ".py")
# Leading folders some mods wrap their asset paths in.
STRIPPED_PREFIXES = ("assets", "pc-s", "dmm", "glo")

# Bundle recompression before encryption, as in UMML 2.0.3.
COMPRESSION_MODES = (
    "LZ4HC (Default)",
    "LZ4 (Fast)",
    "LZMA (Smallest)",
    "Original",
    "None (Uncompressed)",
)
DEFAULT_COMPRESSION = COMPRESSION_MODES[0]
CACHE_NAME = "bundle-names.json"
_CACHE_VERSION = 1


class MetadataError(RuntimeError):
    pass


@dataclass(frozen=True)
class AssetRow:
    hash_name: str
    key: int
    name: str = ""  # logical game name (column n)


@dataclass(frozen=True)
class Resolution:
    """How one source file maps into the game, or why it does not."""

    relative: str
    sha256: str
    row: AssetRow | None
    # "hash", "path", "prefix", "audio-name", "bundle-name",
    # "not-in-metadata", "not-an-asset"
    reason: str
    logical_name: str = ""  # the game's name for the asset when known


class MetaIndex:
    """In-memory copy of metadata table ``a`` (logical name -> hash, key)."""

    def __init__(self, rows: dict[str, AssetRow], has_encrypt: bool):
        self._rows = rows
        self.has_encrypt = has_encrypt
        self._suffix_index: dict[str, AssetRow] | None = None
        self._hash_index: dict[str, AssetRow] | None = None

    @classmethod
    def load(cls, meta_path: str | Path) -> "MetaIndex":
        try:
            connection = sqlite3.connect(str(meta_path))
        except sqlite3.Error as exc:
            raise MetadataError(f"Could not open metadata: {exc}") from exc
        try:
            cursor = connection.cursor()
            cursor.execute("PRAGMA table_info(a)")
            columns = {str(row[1]) for row in cursor.fetchall()}
            if "n" not in columns or "h" not in columns:
                raise MetadataError("Metadata table a is missing name/hash columns")
            has_encrypt = "e" in columns
            # ORDER BY rowid so "first match" means the same thing SQLite's
            # fetchone() meant in the legacy core.
            query = (
                "SELECT n, h, e FROM a ORDER BY rowid"
                if has_encrypt
                else "SELECT n, h FROM a ORDER BY rowid"
            )
            rows: dict[str, AssetRow] = {}
            for record in cursor.execute(query):
                name, hash_name = record[0], record[1]
                if name is None or not hash_name:
                    continue
                key = int(record[2] or 0) if has_encrypt else 0
                rows.setdefault(str(name), AssetRow(str(hash_name), key, str(name)))
        except sqlite3.Error as exc:
            raise MetadataError(f"Could not read metadata: {exc}") from exc
        finally:
            connection.close()
        return cls(rows, has_encrypt)

    def __len__(self) -> int:
        return len(self._rows)

    def lookup(self, name: str) -> AssetRow | None:
        return self._rows.get(name)

    def lookup_hash(self, hash_name: str) -> AssetRow | None:
        """Equivalent of ``WHERE h=?`` (first row)."""

        if self._hash_index is None:
            index: dict[str, AssetRow] = {}
            for row in self._rows.values():
                index.setdefault(row.hash_name, row)
            self._hash_index = index
        return self._hash_index.get(hash_name)

    def lookup_suffix(self, file_name: str) -> AssetRow | None:
        """Equivalent of ``WHERE n LIKE '%/<file_name>'`` (first row).

        Like SQLite's LIKE this is ASCII case-insensitive. Unlike it, ``_`` in
        the file name is a literal underscore, not a single-character wildcard.
        """

        if self._suffix_index is None:
            index: dict[str, AssetRow] = {}
            for name, row in self._rows.items():
                slash = name.rfind("/")
                if slash < 0:
                    continue
                index.setdefault(_ascii_casefold(name[slash + 1:]), row)
            self._suffix_index = index
        return self._suffix_index.get(_ascii_casefold(file_name))


class BundleNameCache:
    """Persistent sha256 -> AssetBundle.m_Name (without extension) cache."""

    def __init__(self, path: Path | None):
        self._path = path
        self._lock = threading.Lock()
        self._names: dict[str, str] = {}
        self._dirty = False
        if path is not None and path.is_file():
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
                if payload.get("version") == _CACHE_VERSION:
                    self._names = {
                        str(key): str(value)
                        for key, value in dict(payload.get("names", {})).items()
                    }
            except (OSError, ValueError, AttributeError):
                self._names = {}

    def get(self, sha256: str) -> str | None:
        with self._lock:
            return self._names.get(sha256)

    def put(self, sha256: str, name: str) -> None:
        with self._lock:
            if self._names.get(sha256) != name:
                self._names[sha256] = name
                self._dirty = True

    def save(self) -> None:
        if self._path is None or not self._dirty:
            return
        with self._lock:
            payload = {"version": _CACHE_VERSION, "names": dict(sorted(self._names.items()))}
            self._path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self._path.with_name(f".{self._path.name}.tmp-{os.getpid()}")
            try:
                temporary.write_text(json.dumps(payload, separators=(",", ":")), encoding="utf-8")
                os.replace(temporary, self._path)
                self._dirty = False
            finally:
                temporary.unlink(missing_ok=True)


def read_bundle_name(source: Path) -> str:
    """Return AssetBundle.m_Name without extension, or "" if there is none."""

    try:
        import UnityPy

        environment = UnityPy.load(str(source))
        for obj in environment.objects:
            if obj.type.name != "AssetBundle":
                continue
            bundle = obj.read()
            resolved = os.path.splitext(str(bundle.m_Name or ""))[0]
            if resolved:
                return resolved
    except Exception:
        return ""
    return ""


def derive_asset_key(key_long: int) -> bytes:
    key_bytes = struct.pack("<q", key_long)
    final = bytearray(len(AB_KEY) * 8)
    for index, value in enumerate(AB_KEY):
        offset = index * 8
        for byte_index in range(8):
            final[offset + byte_index] = value ^ key_bytes[byte_index]
    return bytes(final)


def encrypt_asset(data: bytes, key_long: int) -> bytes:
    """Apply the game's asset XOR exactly like the legacy core does.

    Bytes 0-255 stay plain. From byte 256 on, each byte is XORed with the
    derived key, indexed by absolute file offset.
    """

    if key_long == 0 or len(data) <= 256:
        return bytes(data)
    key = derive_asset_key(key_long)
    remaining = len(data) - 256
    keystream = (key * ((len(data) // len(key)) + 1))[256:len(data)]
    mixed = int.from_bytes(data[256:], "big") ^ int.from_bytes(keystream, "big")
    return bytes(data[:256]) + mixed.to_bytes(remaining, "big")


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def hash_style_name(file_name: str) -> str | None:
    """The game hash a file is named after (32 letters/digits), if any."""

    stem = os.path.splitext(file_name)[0]
    if len(stem) == 32 and stem.isalnum():
        return stem
    if len(file_name) == 32 and file_name.isalnum():
        return file_name
    return None


def looks_like_game_asset(relative: str, head: bytes) -> bool:
    """UMML 2.0.3's upfront filter."""

    name = os.path.basename(relative).lower()
    if name in IGNORED_NAMES or name.endswith(IGNORED_SUFFIXES):
        return False
    if name.endswith(AUDIO_VIDEO_SUFFIXES) or hash_style_name(name):
        return True
    return head.startswith(ASSET_MAGICS)


def packer_for(mode: str) -> str | None:
    """UnityPy packer for a compression mode; None keeps the file as is."""

    lowered = str(mode).lower()
    if "lz4hc" in lowered:
        return "lz4hc"
    if "lz4" in lowered:
        return "lz4"
    if "lzma" in lowered:
        return "lzma"
    if "none" in lowered or "uncompressed" in lowered:
        return "none"
    return None


def configured_compression(store) -> str:
    """The Manager's compression setting (UMML 2.0.3's default if unset)."""

    try:
        value = str(store.load_settings(repair=False).get("compression", "") or "")
    except Exception:
        value = ""
    return value if value in COMPRESSION_MODES else DEFAULT_COMPRESSION


def load_bundle(data: bytes):
    import UnityPy

    return UnityPy.load(data)


def encode_asset(
    data: bytes,
    key_long: int,
    compression: str = DEFAULT_COMPRESSION,
    *,
    loader: Callable[[bytes], object] = load_bundle,
) -> bytes:
    """Turn one mod file into the bytes the game expects, like UMML 2.0.3.

    * A UnityFS file that UnityPy cannot open is taken to be already
      encrypted by the game and is returned unchanged (no second XOR).
    * Other bundles are re-saved with the chosen packer, then encrypted.
    * Everything else is encrypted as is.
    """

    if data.startswith(UNITY_MAGIC):
        try:
            environment = loader(data)
            files = getattr(environment, "files", None)
        except Exception:
            return bytes(data)
        if not files:
            return bytes(data)
        packer = packer_for(compression)
        if packer is not None:
            bundle = list(files.values())[0]
            try:
                data = bundle.save(packer=packer)
            except NotImplementedError:
                # UnityPy 1.25.2 (the version bundled with the Manager) has
                # no "lz4hc" packer and UMML 2.0.3 then silently keeps the
                # file uncompressed. Use plain LZ4 instead: UnityPy 1.25.4's
                # lz4hc output is effectively the same size.
                if packer == "lz4hc":
                    try:
                        data = bundle.save(packer="lz4")
                    except Exception:
                        pass
            except Exception:
                pass
    return encrypt_asset(data, key_long)


def iter_source_files(assets: Path) -> Iterator[tuple[str, Path]]:
    for path in sorted(item for item in assets.rglob("*") if item.is_file()):
        yield path.relative_to(assets).as_posix(), path


class AssetResolver:
    """Resolve mod source files to metadata rows using UMML 2.0.3's rules.

    Order, per source file:
      1. skip anything UMML 2.0.3 does not treat as an asset;
      2. a file named after a game hash (32 letters/digits): match column h;
      3. exact logical-name match on the path relative to ``assets``;
      4. the same with a leading assets/, pc-s/, dmm/ or glo/ removed;
      5. .acb/.awb/.usm: match by file name at the end of a logical name;
      6. UnityFS bundles: read the bundle's internal name and match that.
    """

    def __init__(
        self,
        meta: MetaIndex,
        names: BundleNameCache,
        *,
        bundle_name_reader: Callable[[Path], str] = read_bundle_name,
    ):
        self.meta = meta
        self.names = names
        self._read_bundle_name = bundle_name_reader
        self.bundle_parses = 0

    def _found(self, relative, digest, row, reason) -> Resolution:
        return Resolution(relative, digest, row, reason, row.name)

    def resolve(self, relative: str, source: Path, data: bytes) -> Resolution:
        digest = sha256_bytes(data)
        relative = relative.replace("\\", "/")
        if not looks_like_game_asset(relative, data[:16]):
            return Resolution(relative, digest, None, "not-an-asset")

        file_name = os.path.basename(relative)
        hashed = hash_style_name(file_name)
        if hashed:
            row = self.meta.lookup_hash(hashed)
            if row is not None:
                return self._found(relative, digest, row, "hash")

        row = self.meta.lookup(relative)
        if row is not None:
            return self._found(relative, digest, row, "path")

        parts = relative.split("/")
        if len(parts) > 1 and parts[0].lower() in STRIPPED_PREFIXES:
            row = self.meta.lookup("/".join(parts[1:]))
            if row is not None:
                return self._found(relative, digest, row, "prefix")

        if file_name.lower().endswith(AUDIO_VIDEO_SUFFIXES):
            row = self.meta.lookup_suffix(file_name)
            if row is not None:
                return self._found(relative, digest, row, "audio-name")

        if data.startswith(UNITY_MAGIC):
            name = self.names.get(digest)
            if name is None:
                self.bundle_parses += 1
                name = self._read_bundle_name(source)
                self.names.put(digest, name)
            if name:
                row = self.meta.lookup(name)
                if row is not None:
                    return self._found(relative, digest, row, "bundle-name")
                return Resolution(relative, digest, None, "not-in-metadata", name)
        return Resolution(relative, digest, None, "not-in-metadata")


def target_for(hash_name: str) -> str | None:
    name = Path(hash_name).name
    if len(name) < 2:
        return None
    return f"{name[:2]}/{name}"


def write_or_link(data: bytes, destination: Path, *, link_from: Path | None = None) -> None:
    """Write bytes, or hardlink an identical file already written in this stage."""

    destination.parent.mkdir(parents=True, exist_ok=True)
    if link_from is not None:
        try:
            os.link(link_from, destination)
            return
        except OSError:
            pass
    temporary = destination.with_name(f".{destination.name}.tmp-{os.getpid()}")
    try:
        with open(temporary, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)


def _ascii_casefold(value: str) -> str:
    return "".join(chr(ord(ch) + 32) if "A" <= ch <= "Z" else ch for ch in value)

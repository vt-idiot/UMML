from __future__ import annotations

import hashlib
import os
import shutil
import tempfile
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path

from .asset_codec import (
    CACHE_NAME,
    AssetResolver,
    BundleNameCache,
    MetadataError,
    MetaIndex,
    encrypt_asset,
    iter_source_files,
    sha256_bytes,
    target_for,
    write_or_link,
)
from .characters import asset_group, character_of
from .locking import FileLock, LockError
from .models import ASSET_INDEX_VERSION, PACKAGE_UMML_ASSETS, ModRecord
from .options import OptionError, select_source_paths
from .safety import SafetyError, hash_file, validate_regular_tree
from .store import ManagerStore, StoreError


@dataclass
class SourceIndex:
    flat_files: dict[str, str] = field(default_factory=dict)
    source_files: dict[str, str] = field(default_factory=dict)
    source_hashes: dict[str, str] = field(default_factory=dict)
    source_payloads: dict[str, dict[str, str]] = field(default_factory=dict)
    source_roots: dict[str, str] = field(default_factory=dict)
    pending_targets: dict[str, str] = field(default_factory=dict)
    dormant_sources: list[str] = field(default_factory=list)
    target_characters: dict[str, str] = field(default_factory=dict)
    dormant_characters: dict[str, str] = field(default_factory=dict)
    target_groups: dict[str, str] = field(default_factory=dict)
    missing: int = 0


class SourceEncoder:
    """Turn one mod source file into {game target: encrypted bytes}.

    When ``target_filter`` is set, targets it rejects are returned with a value
    of ``None`` and are not encrypted at all (they are pending).
    """

    def __init__(self, resolver: AssetResolver, names: BundleNameCache | None = None):
        self.resolver = resolver
        self._names = names
        self.target_filter = None
        self.last_logical_name = ""

    def encode(self, relative: str, source: Path) -> tuple[dict[str, bytes | None], int]:
        data = source.read_bytes()
        resolution = self.resolver.resolve(relative, source, data)
        self.last_logical_name = resolution.logical_name
        if resolution.row is None:
            return {}, 1 if resolution.reason == "not-in-metadata" else 0
        target = target_for(resolution.row.hash_name)
        if target is None:
            return {}, 1
        if self.target_filter is not None and not self.target_filter(target):
            return {target: None}, 0
        return {target: encrypt_asset(data, resolution.row.key)}, 0

    def close(self) -> None:
        if self._names is not None:
            self._names.save()


class LegacyAssetAdapter:
    """Prepare legacy UMML assets and index source-bundle ownership safely."""

    def __init__(
        self,
        store: ManagerStore,
        meta_path: str | Path,
        dat_path: str | Path | None = None,
    ):
        """``dat_path`` enables bucketing: only targets the game has already
        downloaded are prepared; the rest are recorded as pending."""

        self.store = store
        self.meta_path = Path(meta_path).expanduser()
        dat = Path(dat_path).expanduser() if dat_path else None
        self.dat_path = dat if dat is not None and dat.is_dir() else None

    def prepare(self, record: ModRecord) -> ModRecord:
        if record.package_type != PACKAGE_UMML_ASSETS:
            raise StoreError(
                f"{record.name} is a {record.package_type} package and cannot be prepared "
                "by the legacy asset decoder."
            )
        if not self.meta_path.is_file():
            raise StoreError(f"Metadata database not found: {self.meta_path}")
        output = self.store.prepared_destination(record)
        output.parent.mkdir(parents=True, exist_ok=True)
        try:
            with FileLock(
                self.store.paths.locks / f"prepare-{record.id}.lock",
                purpose=f"preparing {record.id}",
            ):
                # Referenced sources are read where they live (an archive is
                # extracted to a scratch folder for the duration).
                with self.store.open_source(record) as source:
                    assets = source / "assets"
                    if not assets.is_dir():
                        raise StoreError(f"Missing assets folder: {assets}")
                    try:
                        validate_regular_tree(assets)
                    except SafetyError as exc:
                        raise StoreError(str(exc)) from exc
                    return self._prepare_locked(record, assets, output)
        except LockError as exc:
            raise StoreError(str(exc)) from exc

    def _prepare_locked(
        self,
        record: ModRecord,
        assets: Path,
        output: Path,
    ) -> ModRecord:
        configurable = bool(record.option_groups)
        stage_root = Path(
            tempfile.mkdtemp(prefix=f".{output.name}-prepare-", dir=output.parent)
        )
        normalized = stage_root / "normalized"
        normalized.mkdir()
        backup = self._backup_path(output)
        moved_old = False
        moved_new = False
        try:
            index = self._encode_sources(
                assets,
                normalized,
                flat=not configurable,
                strict=configurable,
            )
            try:
                validate_regular_tree(normalized)
            except SafetyError as exc:
                raise StoreError(f"Prepared output was unsafe: {exc}") from exc

            if configurable:
                if not index.source_payloads:
                    raise StoreError(
                        f"No compatible configurable assets produced; {index.missing} entries "
                        "were absent from metadata. The previous prepared cache was preserved."
                    )
                try:
                    selected_sources = select_source_paths(
                        record.option_groups,
                        {},
                        index.source_payloads,
                    )
                except OptionError as exc:
                    raise StoreError(f"Invalid configurable mod manifest: {exc}") from exc
                files = self._flatten_selected_payloads(
                    selected_sources,
                    index.source_payloads,
                    context="default configurable selection",
                )
                if not files:
                    raise StoreError(
                        "The default configurable selection produced no deployable assets; "
                        "the previous prepared cache was preserved."
                    )
            else:
                files = index.flat_files
                if not files and not index.pending_targets:
                    raise StoreError(
                        f"No compatible assets produced; {index.missing} entries were absent "
                        "from metadata. The previous prepared cache was preserved."
                    )

            fingerprint = hash_file(self.meta_path)
            updated = replace(
                record,
                prepared_path=str(output),
                files=files,
                source_files=index.source_files,
                source_hashes=index.source_hashes,
                source_payloads=index.source_payloads,
                source_roots=index.source_roots,
                pending_targets=index.pending_targets,
                dormant_sources=index.dormant_sources,
                target_characters=index.target_characters,
                dormant_characters=index.dormant_characters,
                target_groups=index.target_groups,
                asset_index_version=ASSET_INDEX_VERSION,
                source_indexed_against=fingerprint,
                prepared_against=fingerprint,
                prepared_at=datetime.now(timezone.utc).isoformat(),
            )
            return self._commit_prepared(
                updated,
                output=output,
                normalized=normalized,
                backup=backup,
                moved_old_ref=[moved_old],
                moved_new_ref=[moved_new],
            )
        finally:
            shutil.rmtree(stage_root, ignore_errors=True)
            if backup.exists() and not output.exists():
                os.replace(backup, output)

    def _source_encoder(self) -> "SourceEncoder":
        """Return the object that turns one source file into game targets.

        Tests replace this to fake multi-target bundles without real metadata.
        """

        try:
            meta = MetaIndex.load(self.meta_path)
        except MetadataError as exc:
            raise StoreError(str(exc)) from exc
        names = BundleNameCache(self.store.paths.root / CACHE_NAME)
        return SourceEncoder(AssetResolver(meta, names), names)

    def _encode_sources(
        self,
        assets: Path,
        normalized: Path,
        *,
        flat: bool,
        strict: bool,
    ) -> "SourceIndex":
        """Resolve and encrypt every source file exactly once.

        Writes the per-source tree (``sources/<token>/<aa>/<hash>``) used by the
        UI and configurable mods and, when ``flat`` is set, the flat deploy tree
        (``<aa>/<hash>``). Identical bytes in both trees are hardlinked inside
        the stage instead of being written twice.
        """

        encoder = self._source_encoder()
        if flat and self.dat_path is not None:
            dat = self.dat_path
            # Only link over files the game itself downloaded; anything else
            # would have no stock baseline and might be overwritten anyway.
            encoder.target_filter = lambda target: (dat / target).is_file()
        index = SourceIndex()
        flat_owner: dict[str, Path] = {}
        try:
            for relative, source in iter_source_files(assets):
                root_relative = (
                    Path("sources")
                    / hashlib.sha256(relative.encode("utf-8")).hexdigest()[:20]
                ).as_posix()
                try:
                    outputs, counted_missing = encoder.encode(relative, source)
                except Exception:
                    if strict:
                        raise
                    index.missing += 1
                    continue
                index.missing += counted_missing
                logical_name = getattr(encoder, "last_logical_name", "")
                character = character_of(relative, logical_name)
                group = asset_group(logical_name or relative)
                if not outputs:
                    if counted_missing:
                        index.dormant_sources.append(relative)
                        if character:
                            index.dormant_characters[relative] = character
                    continue

                payload: dict[str, str] = {}
                for target in sorted(outputs):
                    data = outputs[target]
                    if character:
                        index.target_characters[target] = character
                    if group:
                        index.target_groups[target] = group
                    if data is None:
                        index.pending_targets[target] = relative
                        continue
                    digest = sha256_bytes(data)
                    destination = normalized / root_relative / target
                    write_or_link(data, destination)
                    payload[target] = digest
                    if flat:
                        flat_destination = normalized / target
                        if target in flat_owner:
                            # Two sources map to one game file: the later source
                            # (sorted path order) wins, as the legacy decoder's
                            # overwrite did, but deterministically.
                            flat_destination.unlink()
                        write_or_link(data, flat_destination, link_from=destination)
                        flat_owner[target] = destination
                        index.flat_files[target] = digest

                if not payload:
                    continue
                index.source_payloads[relative] = payload
                index.source_roots[relative] = root_relative
                if len(payload) == 1:
                    target, digest = next(iter(payload.items()))
                    index.source_files[relative] = target
                    index.source_hashes[relative] = digest
        finally:
            encoder.close()
        return index

    @staticmethod
    def _flatten_selected_payloads(
        selected_sources,
        source_payloads: dict[str, dict[str, str]],
        *,
        context: str,
    ) -> dict[str, str]:
        files: dict[str, str] = {}
        owners: dict[str, str] = {}
        for source_relative in sorted(selected_sources):
            for target, sha256 in sorted(source_payloads[source_relative].items()):
                previous = owners.get(target)
                if previous is not None:
                    raise StoreError(
                        f"The {context} contains two source bundles for one game target: "
                        f"{previous!r} and {source_relative!r} -> {target}. Put them in "
                        "mutually exclusive choices or remove the duplicate payload."
                    )
                owners[target] = source_relative
                files[target] = sha256
        return files

    @staticmethod
    def _backup_path(output: Path) -> Path:
        return output.with_name(
            f".{output.name}.previous-{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S%f')}"
        )

    def _commit_prepared(
        self,
        updated: ModRecord,
        *,
        output: Path,
        normalized: Path,
        backup: Path,
        moved_old_ref: list[bool],
        moved_new_ref: list[bool],
    ) -> ModRecord:
        if output.exists():
            os.replace(output, backup)
            moved_old_ref[0] = True
        os.replace(normalized, output)
        moved_new_ref[0] = True
        try:
            self.store.save_mod(updated)
        except Exception:
            if moved_new_ref[0] and output.exists():
                shutil.rmtree(output, ignore_errors=True)
            if moved_old_ref[0] and backup.exists():
                os.replace(backup, output)
            raise
        shutil.rmtree(backup, ignore_errors=True)
        return updated

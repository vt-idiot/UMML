from __future__ import annotations

import json
import os
import random
import shutil
import sqlite3
import tempfile
import unittest
import zipfile
from pathlib import Path

from umml_manager.legacy_adapter import LegacyAssetAdapter
from umml_manager.library import ManagerStore
from umml_manager.source_refs import (
    STATUS_CHANGED,
    STATUS_LIBRARY,
    STATUS_OFFLINE,
    STATUS_ONLINE,
)
from umml_manager.store import StoreError


def _bundle(seed: int, size: int = 2000) -> bytes:
    return b"UnityFS\x00" + random.Random(seed).randbytes(size)


def _write_mod(root: Path, title: str, seed: int) -> Path:
    (root / "assets" / "chara" / "chr1001").mkdir(parents=True, exist_ok=True)
    (root / "assets" / "chara" / "chr1001" / "pfb_bdy1001").write_bytes(_bundle(seed))
    (root / "umml-mod.json").write_text(
        json.dumps({"id": title.lower().replace(" ", "-"), "title": title, "version": "1"}),
        encoding="utf-8",
    )
    return root


def _zip_folder(folder: Path, archive: Path, *, prefix: str) -> Path:
    archive.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(archive, "w") as handle:
        for path in sorted(folder.rglob("*")):
            if path.is_file():
                handle.write(path, f"{prefix}/{path.relative_to(folder).as_posix()}")
    return archive


class SourceReferenceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.store = ManagerStore(self.root / "manager")
        self.drive = self.root / "M"
        self.meta = self.root / "meta.db"
        connection = sqlite3.connect(self.meta)
        connection.execute("CREATE TABLE a (n TEXT, h TEXT, e INTEGER)")
        connection.execute(
            "INSERT INTO a VALUES ('chara/chr1001/pfb_bdy1001', 'ab' || hex(randomblob(15)), 77)"
        )
        connection.commit()
        connection.close()

    def tearDown(self):
        self.temp.cleanup()

    def _sources_folder_is_empty(self):
        sources = self.store.paths.sources
        return not sources.exists() or not any(sources.rglob("*"))

    def test_folder_reference_is_not_copied_and_prepares_in_place(self):
        folder = _write_mod(self.drive / "mods" / "Bikini", "Bikini", 1)
        record = self.store.import_folder(folder, reference=True)
        self.assertEqual(record.source_path, "")
        self.assertEqual(record.origin.kind, "folder")
        self.assertTrue(self._sources_folder_is_empty())
        self.assertEqual(self.store.source_status(record), STATUS_ONLINE)
        prepared = LegacyAssetAdapter(self.store, self.meta).prepare(record)
        self.assertEqual(len(prepared.files), 1)

    def test_default_import_still_copies(self):
        folder = _write_mod(self.drive / "mods" / "Copied", "Copied", 2)
        record = self.store.import_folder(folder)
        self.assertIsNone(record.origin)
        self.assertTrue(Path(record.source_path).is_dir())
        self.assertEqual(self.store.source_status(record), STATUS_LIBRARY)

    def test_archive_reference_extracts_only_while_preparing(self):
        staging = _write_mod(self.root / "staging", "Wing Zero", 3)
        archive = _zip_folder(staging, self.drive / "Wing Zero v2.zip", prefix="Wing Zero v2")
        record = self.store.import_archive(archive, reference=True)
        self.assertEqual(record.origin.kind, "archive")
        self.assertEqual(record.origin.subpath, "Wing Zero v2")
        self.assertTrue(self._sources_folder_is_empty())

        prepared = LegacyAssetAdapter(self.store, self.meta).prepare(record)
        self.assertEqual(len(prepared.files), 1)
        scratch = self.store.paths.root / "scratch"
        self.assertFalse(scratch.exists() and any(scratch.iterdir()))

    def test_moved_archive_goes_offline_and_can_be_relinked(self):
        staging = _write_mod(self.root / "staging", "Pack", 4)
        archive = _zip_folder(staging, self.drive / "Pack.zip", prefix="Pack")
        record = self.store.import_archive(archive, reference=True)

        moved = self.drive / "cleaned up" / "Pack (keep).zip"
        moved.parent.mkdir(parents=True)
        shutil.move(archive, moved)
        self.assertEqual(self.store.source_status(record), STATUS_OFFLINE)
        with self.assertRaisesRegex(StoreError, "offline"):
            LegacyAssetAdapter(self.store, self.meta).prepare(record)

        relinked = self.store.relink_source(record.id, moved)
        self.assertEqual(Path(relinked.origin.path), moved.resolve())
        self.assertEqual(self.store.source_status(relinked), STATUS_ONLINE)
        LegacyAssetAdapter(self.store, self.meta).prepare(relinked)

    def test_changed_archive_is_detected_and_wrong_file_is_rejected(self):
        staging = _write_mod(self.root / "staging", "Pack", 5)
        archive = _zip_folder(staging, self.drive / "Pack.zip", prefix="Pack")
        record = self.store.import_archive(archive, reference=True)

        other = _write_mod(self.root / "other", "Pack", 6)
        _zip_folder(other, archive, prefix="Pack")  # same name, different content
        self.assertEqual(self.store.source_status(record), STATUS_CHANGED)
        with self.assertRaisesRegex(StoreError, "changed"):
            LegacyAssetAdapter(self.store, self.meta).prepare(record)
        with self.assertRaisesRegex(StoreError, "not the same content"):
            self.store.relink_source(record.id, archive)

    def test_touched_archive_with_same_bytes_stays_online(self):
        staging = _write_mod(self.root / "staging", "Pack", 7)
        archive = _zip_folder(staging, self.drive / "Pack.zip", prefix="Pack")
        record = self.store.import_archive(archive, reference=True)
        info = archive.stat()
        os.utime(archive, ns=(info.st_atime_ns, info.st_mtime_ns + 5_000_000_000))
        self.assertEqual(self.store.source_status(record), STATUS_ONLINE)

    def test_find_missing_sources_relinks_archives_and_folders(self):
        staging = _write_mod(self.root / "staging", "Zipped", 8)
        archive = _zip_folder(staging, self.drive / "Zipped.zip", prefix="Zipped")
        folder = _write_mod(self.drive / "Loose", "Loose", 9)
        zipped = self.store.import_archive(archive, reference=True)
        loose = self.store.import_folder(folder, reference=True)

        new_drive = self.root / "N" / "archive" / "2026"
        new_drive.mkdir(parents=True)
        shutil.move(archive, new_drive / "Zipped.zip")
        shutil.move(folder, new_drive / "Loose renamed")
        decoy = new_drive / "decoy.zip"
        decoy.write_bytes(os.urandom(zipped.origin.size_bytes))  # same size, wrong bytes

        found = self.store.find_missing_sources(self.root / "N")
        self.assertEqual({record.id for record in found}, {zipped.id, loose.id})
        self.assertEqual(
            Path(self.store.get_mod(zipped.id).origin.path), (new_drive / "Zipped.zip").resolve()
        )
        self.assertEqual(
            Path(self.store.get_mod(loose.id).origin.path), (new_drive / "Loose renamed").resolve()
        )

    def test_reimporting_the_same_reference_reuses_the_record(self):
        staging = _write_mod(self.root / "staging", "Pack", 10)
        archive = _zip_folder(staging, self.drive / "Pack.zip", prefix="Pack")
        first = self.store.import_archive(archive, reference=True)
        copy = self.drive / "dupe" / "Pack.zip"
        copy.parent.mkdir()
        shutil.copy2(archive, copy)
        second = self.store.import_archive(copy, reference=True)
        self.assertEqual(first.id, second.id)
        self.assertEqual(len(self.store.list_mods()), 1)

    def test_workspace_from_referenced_archive(self):
        staging = _write_mod(self.root / "staging", "Pack", 11)
        archive = _zip_folder(staging, self.drive / "Pack.zip", prefix="Pack")
        record = self.store.import_archive(archive, reference=True)
        workspace = self.store.create_workspace(record.id)
        self.assertTrue((workspace / "assets" / "chara" / "chr1001" / "pfb_bdy1001").is_file())

    def test_origin_survives_registry_round_trip(self):
        folder = _write_mod(self.drive / "Round", "Round", 12)
        record = self.store.import_folder(folder, reference=True)
        reloaded = ManagerStore(self.root / "manager").get_mod(record.id)
        self.assertEqual(reloaded.origin, record.origin)


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from umml_manager.discovery import asset_source_root, describe_mod_root, platform_folders
from umml_manager.legacy_adapter import LegacyAssetAdapter
from umml_manager.models import PACKAGE_UMML_ASSETS
from umml_manager.store import ManagerStore


def _file(path: Path, data: bytes = b"UnityFS") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


class PlatformFolderTests(unittest.TestCase):
    """Raw mods that ship PC-s/GLO/DMM folders instead of assets/, like UMML."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def test_folder_with_pc_s_is_a_mod(self):
        mod = self.root / "Bourbon Outfit"
        _file(mod / "PC-s" / "assets" / "chara" / "bdy1026_00")
        candidate = describe_mod_root(mod)
        self.assertIsNotNone(candidate)
        self.assertIn("PC-s", candidate.reason)
        self.assertEqual(asset_source_root(mod), mod / "PC-s")

    def test_match_is_case_insensitive_and_needs_files(self):
        mod = self.root / "mod"
        (mod / "DMM").mkdir(parents=True)  # empty: does not count
        _file(mod / "pc-s" / "a" / "x")
        self.assertEqual(list(platform_folders(mod)), ["PC-s"])
        self.assertIsNone(describe_mod_root(self.root / "nothing-here"))

    def test_precedence_follows_umml(self):
        mod = self.root / "mod"
        for name in ("GLO", "DMM"):
            _file(mod / name / "f")
        self.assertEqual(asset_source_root(mod, "global").name, "GLO")
        self.assertEqual(asset_source_root(mod, "japan").name, "DMM")
        _file(mod / "PC-s" / "f")
        self.assertEqual(asset_source_root(mod, "japan").name, "PC-s")
        _file(mod / "assets" / "f")
        self.assertEqual(asset_source_root(mod, "japan").name, "assets")

    def test_import_and_prepare_use_only_the_chosen_platform_folder(self):
        mod = self.root / "raw"
        _file(mod / "PC-s" / "assets" / "chara" / "steam_bundle")
        _file(mod / "DMM" / "assets" / "chara" / "dmm_bundle")
        _file(mod / "readme.txt", b"hello")
        store = ManagerStore(self.root / "manager")
        record = store.import_folder(mod, reference=True)
        self.assertEqual(record.package_type, PACKAGE_UMML_ASSETS)

        meta = self.root / "meta"
        meta.write_bytes(b"")
        adapter = LegacyAssetAdapter(store, meta)
        seen = {}

        def capture(_record, assets, _output):
            seen["assets"] = assets
            seen["files"] = sorted(
                p.relative_to(assets).as_posix() for p in assets.rglob("*") if p.is_file()
            )
            return _record

        with patch.object(LegacyAssetAdapter, "_prepare_locked", side_effect=capture):
            adapter.prepare(record)
        self.assertEqual(seen["assets"].name, "PC-s")
        self.assertEqual(seen["files"], ["assets/chara/steam_bundle"])

    def test_wrapper_folder_import_finds_the_raw_mod_inside(self):
        outer = self.root / "download"
        _file(outer / "Some Mod v2" / "PC-s" / "x")
        store = ManagerStore(self.root / "manager")
        record = store.import_folder(outer, reference=True)
        self.assertEqual(record.package_type, PACKAGE_UMML_ASSETS)
        self.assertTrue(record.origin.path.endswith("Some Mod v2"))


if __name__ == "__main__":
    unittest.main()

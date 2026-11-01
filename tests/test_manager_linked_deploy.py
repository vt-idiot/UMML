from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from umml_manager.deployment import ApplyEngine
from umml_manager.engine import ApplyError
from umml_manager.library import ManagerStore
from umml_manager.linking import HashCache, link_or_copy, same_file
from umml_manager.models import ModRecord, Profile
from umml_manager.resolver import resolve_profile
from umml_manager.safety import hash_file

TARGETS = ("aa/aa111", "bb/bb222")


class LinkedDeployTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.store = ManagerStore(self.root / "manager")
        self.game = self.root / "game"
        self.dat = self.game / "Persistent" / "dat"
        for target in TARGETS:
            path = self.dat / target
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"stock " + target.encode() * 200)

        prepared = self.store.paths.prepared / "skirt" / "1"
        files = {}
        for target in TARGETS:
            path = prepared / target
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"modded " + target.encode() * 300)
            files[target] = hash_file(path)
        self.prepared = prepared
        self.mod = ModRecord(
            "skirt", "Shorter Skirt", version="1", prepared_path=str(prepared), files=files
        )
        self.store.save_mod(self.mod)

    def tearDown(self):
        self.temp.cleanup()

    def _engine(self):
        return ApplyEngine(self.store, self.dat, game_dir=self.game, process_check=lambda _g: ())

    def _apply(self, enabled):
        mods = self.store.list_mods()
        resolution = resolve_profile(Profile("P", enabled), mods)
        engine = self._engine()
        return engine.apply(resolution), engine

    def test_apply_links_prepared_files_and_keeps_stock_in_baseline(self):
        result, _engine = self._apply(["skirt"])
        self.assertEqual(result.installed, 2)
        self.assertEqual(result.copied, 0)
        self.assertGreater(result.linked, 0)
        for target in TARGETS:
            game_file = self.dat / target
            self.assertTrue(same_file(game_file, self.prepared / target))
            baseline = self.store.paths.baseline / target
            self.assertTrue(baseline.read_bytes().startswith(b"stock "))
            self.assertFalse(same_file(baseline, game_file))
        # Transaction snapshots are cleaned up after commit.
        leftovers = list(self.store.paths.transactions.glob("apply-*"))
        self.assertEqual(leftovers, [])

    def test_switching_stock_and_modded_never_copies(self):
        self._apply(["skirt"])
        stock, _ = self._apply([])
        self.assertEqual(stock.restored, 2)
        self.assertEqual(stock.copied, 0)
        for target in TARGETS:
            self.assertTrue((self.dat / target).read_bytes().startswith(b"stock "))
            self.assertTrue(same_file(self.dat / target, self.store.paths.baseline / target))
        modded, engine = self._apply(["skirt"])
        self.assertEqual(modded.copied, 0)
        self.assertEqual(modded.installed, 2)
        # Every file had been seen before; nothing needed re-reading in full
        # except files whose identity changed.
        self.assertGreater(engine.hashes.hits, engine.hashes.misses)

    def test_in_place_write_is_detected_and_marks_the_mod_stale(self):
        self._apply(["skirt"])
        with open(self.dat / TARGETS[0], "r+b") as handle:
            handle.write(b"GAME UPDATE")
        with self.assertRaises(ApplyError) as raised:
            self._apply([])
        message = str(raised.exception)
        self.assertIn("changed outside UMML Manager", message)
        self.assertIn("written in place", message)
        self.assertEqual(self.store.get_mod("skirt").prepared_against, "")

    def test_replaced_file_is_detected_without_touching_the_prepared_copy(self):
        self._apply(["skirt"])
        game_file = self.dat / TARGETS[0]
        game_file.unlink()
        game_file.write_bytes(b"another mod loader")
        with self.assertRaises(ApplyError) as raised:
            self._apply([])
        self.assertNotIn("written in place", str(raised.exception))
        self.assertTrue((self.prepared / TARGETS[0]).read_bytes().startswith(b"modded "))

    def test_falls_back_to_copies_when_links_are_impossible(self):
        with patch("umml_manager.linking.os.link", side_effect=OSError("cross-device")):
            result, _ = self._apply(["skirt"])
            self.assertEqual(result.linked, 0)
            self.assertGreater(result.copied, 0)
            for target in TARGETS:
                self.assertFalse(same_file(self.dat / target, self.prepared / target))
                self.assertEqual(
                    (self.dat / target).read_bytes(), (self.prepared / target).read_bytes()
                )
            stock, _ = self._apply([])
        self.assertEqual(stock.restored, 2)
        for target in TARGETS:
            self.assertTrue((self.dat / target).read_bytes().startswith(b"stock "))


class HashCacheTests(unittest.TestCase):
    def test_hits_misses_and_invalidation(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            path = root / "file"
            path.write_bytes(b"one")
            cache = HashCache(root / "cache.json")
            first = cache.sha256(path)
            self.assertEqual(cache.sha256(path), first)
            self.assertEqual((cache.hits, cache.misses), (1, 1))

            link = root / "link"
            if link_or_copy(path, link):
                self.assertEqual(cache.sha256(link), first)
                self.assertEqual(cache.hits, 2)

            info = path.stat()
            path.write_bytes(b"two")
            os.utime(path, ns=(info.st_atime_ns, info.st_mtime_ns + 1_000_000))
            self.assertNotEqual(cache.sha256(path), first)

            cache.save()
            reloaded = HashCache(root / "cache.json")
            reloaded.sha256(path)
            self.assertEqual(reloaded.hits, 1)

    def test_link_or_copy_replaces_atomically(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "src").write_bytes(b"new")
            (root / "dst").write_bytes(b"old")
            link_or_copy(root / "src", root / "dst")
            self.assertEqual((root / "dst").read_bytes(), b"new")
            self.assertEqual(sorted(p.name for p in root.iterdir()), ["dst", "src"])


if __name__ == "__main__":
    unittest.main()

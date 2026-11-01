from __future__ import annotations

import random
import sqlite3
import tempfile
import unittest
from pathlib import Path

from umml_manager.deployment import ApplyEngine
from umml_manager.legacy_adapter import LegacyAssetAdapter
from umml_manager.library import ManagerStore
from umml_manager.models import ModRecord, Profile, has_prepared_state
from umml_manager.resolver import resolve_profile
from umml_manager.safety import hash_file
from umml_manager.ui_auto_prepare_actions import AutoPrepareActions

LIVE = ("chara/chr1001/pfb_bdy1001", "aa" + "1" * 30, 11)
PENDING = ("chara/chr1067/pfb_bdy1067", "bb" + "2" * 30, 22)
DORMANT_PATH = "chara/chr9999/pfb_bdy9999"


def _bundle(seed: int) -> bytes:
    return b"UnityFS\x00" + random.Random(seed).randbytes(3000)


def _target(row) -> str:
    return f"{row[1][:2]}/{row[1]}"


class _Var:
    def __init__(self, value):
        self.value = value

    def get(self):
        return self.value


class _Probe(AutoPrepareActions):
    def __init__(self, dat: Path):
        self.dat_path = _Var(str(dat))


class AssetBucketTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.meta = self.root / "meta.db"
        connection = sqlite3.connect(self.meta)
        connection.execute("CREATE TABLE a (n TEXT, h TEXT, e INTEGER)")
        connection.executemany("INSERT INTO a VALUES (?, ?, ?)", [LIVE, PENDING])
        connection.commit()
        connection.close()

        self.package = self.root / "pack"
        for index, relative in enumerate((LIVE[0], PENDING[0], DORMANT_PATH)):
            path = self.package / "assets" / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(_bundle(index))

        self.game = self.root / "game"
        self.dat = self.game / "Persistent" / "dat"
        live_stock = self.dat / _target(LIVE)
        live_stock.parent.mkdir(parents=True)
        live_stock.write_bytes(b"stock live")

        self.store = ManagerStore(self.root / "manager")
        self.record = ModRecord("pack", "Pack", version="1", source_path=str(self.package))

    def tearDown(self):
        self.temp.cleanup()

    def _prepare(self, record=None, dat=True):
        adapter = LegacyAssetAdapter(self.store, self.meta, self.dat if dat else None)
        return adapter.prepare(record or self.record)

    def test_only_downloaded_assets_are_prepared(self):
        prepared = self._prepare()
        self.assertEqual(set(prepared.files), {_target(LIVE)})
        self.assertEqual(prepared.pending_targets, {_target(PENDING): PENDING[0]})
        self.assertEqual(prepared.dormant_sources, [DORMANT_PATH])
        output = Path(prepared.prepared_path)
        self.assertFalse((output / _target(PENDING)).exists())
        self.assertFalse(any(path.name == PENDING[1] for path in output.rglob("*")))

    def test_without_dat_everything_in_metadata_is_prepared(self):
        prepared = self._prepare(dat=False)
        self.assertEqual(set(prepared.files), {_target(LIVE), _target(PENDING)})
        self.assertEqual(prepared.pending_targets, {})

    def test_all_pending_pack_counts_as_prepared_and_blocks_nothing(self):
        (self.dat / _target(LIVE)).unlink()
        prepared = self._prepare()
        self.assertEqual(prepared.files, {})
        self.assertEqual(len(prepared.pending_targets), 2)
        self.assertTrue(has_prepared_state(prepared))
        resolution = resolve_profile(
            Profile("Default", [prepared.id]),
            [prepared],
            metadata_fingerprint=hash_file(self.meta),
        )
        self.assertNotIn(prepared.id, resolution.unprepared)
        self.assertEqual(resolution.winners, {})

    def test_apply_never_places_a_pending_asset(self):
        prepared = self._prepare()
        resolution = resolve_profile(
            Profile("Default", [prepared.id]),
            [prepared],
            metadata_fingerprint=hash_file(self.meta),
        )
        self.assertFalse(resolution.blocking_issues)
        ApplyEngine(
            self.store, self.dat, game_dir=self.game, process_check=lambda _game: ()
        ).apply(resolution)
        self.assertNotEqual((self.dat / _target(LIVE)).read_bytes(), b"stock live")
        self.assertFalse((self.dat / _target(PENDING)).exists())

    def test_download_of_a_pending_asset_triggers_and_completes_reprepare(self):
        prepared = self._prepare()
        probe = _Probe(self.dat)
        self.assertFalse(probe._pending_targets_arrived(prepared))

        downloaded = self.dat / _target(PENDING)
        downloaded.parent.mkdir(parents=True, exist_ok=True)
        downloaded.write_bytes(b"stock almond eye")
        self.assertTrue(probe._pending_targets_arrived(prepared))

        again = self._prepare(prepared)
        self.assertEqual(set(again.files), {_target(LIVE), _target(PENDING)})
        self.assertEqual(again.pending_targets, {})
        self.assertFalse(probe._pending_targets_arrived(again))

    def test_records_from_an_older_manager_are_reprepared(self):
        from dataclasses import replace

        from umml_manager.models import ASSET_INDEX_VERSION

        prepared = self._prepare()
        self.assertEqual(prepared.asset_index_version, ASSET_INDEX_VERSION)

        class _Needs(AutoPrepareActions):
            def __init__(self, dat):
                self.dat_path = _Var(str(dat))
                self.meta_path = _Var(__file__)
                self.metadata_fingerprint = _Var("")

        probe = _Needs(self.dat)
        self.assertFalse(probe._record_needs_auto_prepare(prepared))
        old = replace(prepared, asset_index_version=0, target_characters={})
        self.assertTrue(probe._record_needs_auto_prepare(old))

    def test_buckets_survive_registry_round_trip(self):
        prepared = self._prepare()
        reloaded = ManagerStore(self.root / "manager").get_mod(prepared.id)
        self.assertEqual(reloaded.pending_targets, prepared.pending_targets)
        self.assertEqual(reloaded.dormant_sources, prepared.dormant_sources)


if __name__ == "__main__":
    unittest.main()

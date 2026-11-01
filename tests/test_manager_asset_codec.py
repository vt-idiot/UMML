from __future__ import annotations

import os
import random
import sqlite3
import sys
import tempfile
import types
import unittest
from pathlib import Path

from umml_manager.asset_codec import (
    AssetResolver,
    BundleNameCache,
    MetaIndex,
    encrypt_asset,
)
from umml_manager.legacy_adapter import LegacyAssetAdapter
from umml_manager.models import ModRecord
from umml_manager.store import ManagerStore


def _make_meta(path: Path, rows, *, with_key=True):
    connection = sqlite3.connect(path)
    try:
        if with_key:
            connection.execute("CREATE TABLE a (n TEXT, h TEXT, e INTEGER)")
            connection.executemany("INSERT INTO a VALUES (?, ?, ?)", rows)
        else:
            connection.execute("CREATE TABLE a (n TEXT, h TEXT)")
            connection.executemany("INSERT INTO a VALUES (?, ?)", [(n, h) for n, h, _ in rows])
        connection.commit()
    finally:
        connection.close()


def _bundle(seed: int, size: int = 4000) -> bytes:
    rng = random.Random(seed)
    return b"UnityFS\x00" + bytes(rng.randrange(256) for _ in range(size))


def _load_legacy_core():
    """Import UMML_core (Naufal's loader) for byte-for-byte comparisons."""

    if sys.platform != "win32" and "winreg" not in sys.modules:
        stub = types.ModuleType("winreg")
        stub.HKEY_CURRENT_USER = object()
        stub.HKEY_LOCAL_MACHINE = object()
        sys.modules["winreg"] = stub
    import UMML_core

    return UMML_core


class _Null:
    def __setitem__(self, key, value):
        pass

    def config(self, **kwargs):
        pass

    def update_idletasks(self):
        pass


def _legacy_decrypt(core, meta: Path, src: Path, dst: Path):
    host = types.SimpleNamespace(
        meta_path=str(meta),
        progress_bar=_Null(),
        progress_label=_Null(),
        root=_Null(),
        scan_full_path=lambda root: sorted(
            p.relative_to(root).as_posix() for p in Path(root).rglob("*") if p.is_file()
        ),
    )
    return core.ModLoaderGUI.decrypt_assets_internal(host, str(src), str(dst))


class EncryptAssetTests(unittest.TestCase):
    def test_short_and_zero_key_inputs_are_unchanged(self):
        for data in (b"", b"x" * 10, b"y" * 256):
            self.assertEqual(encrypt_asset(data, 12345), data)
        self.assertEqual(encrypt_asset(b"z" * 1000, 0), b"z" * 1000)

    def test_xor_is_its_own_inverse_and_keeps_header(self):
        data = _bundle(1, 9000)
        encrypted = encrypt_asset(data, -987654321012345)
        self.assertEqual(encrypted[:256], data[:256])
        self.assertNotEqual(encrypted, data)
        self.assertEqual(encrypt_asset(encrypted, -987654321012345), data)


class ResolverTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.meta = self.root / "meta.db"
        _make_meta(
            self.meta,
            [
                ("chara/chr1001/pfb_bdy1001", "AAhash1", 111),
                ("chara/chr1002/pfb_bdy1002", "BBhash2", 222),
                ("sound/c/snd_voi_1001.awb", "CChash3", 0),
                ("sound/c/snd_voi_1001.acb", "DDhash4", 0),
            ],
        )
        self.calls = []

    def tearDown(self):
        self.temp.cleanup()

    def _resolver(self, names=None, reader_result="chara/chr1002/pfb_bdy1002"):
        def reader(path):
            self.calls.append(path.name)
            return reader_result

        return AssetResolver(
            MetaIndex.load(self.meta),
            names or BundleNameCache(None),
            bundle_name_reader=reader,
        )

    def test_resolution_reasons(self):
        resolver = self._resolver()
        assets = self.root / "assets"
        (assets / "chara/chr1001").mkdir(parents=True)
        (assets / "Sound").mkdir(parents=True)
        cases = {
            "chara/chr1001/pfb_bdy1001": (_bundle(1), "path", "AAhash1"),
            "renamed_bundle": (_bundle(2), "bundle-name", "BBhash2"),
            "Sound/SND_VOI_1001.awb": (b"AFS2" + b"\0" * 300, "audio-pair", "CChash3"),
            "Sound/SND_VOI_1001.acb": (b"@UTF" + b"\0" * 300, "audio-pair", "DDhash4"),
            "readme.txt": (b"hello", "not-an-asset", None),
        }
        for relative, (data, _reason, _hash) in cases.items():
            (assets / relative).parent.mkdir(parents=True, exist_ok=True)
            (assets / relative).write_bytes(data)
        for relative, (data, reason, hash_name) in cases.items():
            result = resolver.resolve(relative, assets / relative, data)
            self.assertEqual(result.reason, reason, relative)
            self.assertEqual(result.row.hash_name if result.row else None, hash_name, relative)

    def test_audio_without_its_pair_is_not_matched_by_suffix(self):
        resolver = self._resolver()
        lone = self.root / "lone" / "snd_voi_1001.awb"
        lone.parent.mkdir()
        lone.write_bytes(b"AFS2" + b"\0" * 300)
        result = resolver.resolve("lone/snd_voi_1001.awb", lone, lone.read_bytes())
        self.assertEqual(result.reason, "not-in-metadata")

    def test_bundle_names_are_parsed_once_and_persisted(self):
        cache_path = self.root / "cache.json"
        data = _bundle(7)
        source = self.root / "renamed"
        source.write_bytes(data)

        first = self._resolver(BundleNameCache(cache_path))
        first.resolve("renamed", source, data)
        first.resolve("renamed", source, data)
        first.names.save()
        self.assertEqual(self.calls, ["renamed"])

        second = self._resolver(BundleNameCache(cache_path))
        result = second.resolve("renamed", source, data)
        self.assertEqual(result.reason, "bundle-name")
        self.assertEqual(self.calls, ["renamed"])
        self.assertEqual(second.bundle_parses, 0)

    def test_unresolvable_bundles_are_cached_as_misses(self):
        cache_path = self.root / "cache.json"
        data = _bundle(8)
        source = self.root / "dormant"
        source.write_bytes(data)
        resolver = self._resolver(BundleNameCache(cache_path), reader_result="chara/chr9999/not_on_global")
        for _ in range(3):
            self.assertEqual(resolver.resolve("dormant", source, data).reason, "not-in-metadata")
        self.assertEqual(len(self.calls), 1)

    def test_metadata_without_key_column(self):
        meta = self.root / "plain.db"
        _make_meta(meta, [("x/y", "EEhash5", 0)], with_key=False)
        index = MetaIndex.load(meta)
        self.assertFalse(index.has_encrypt)
        self.assertEqual(index.lookup("x/y").key, 0)


class LegacyEquivalenceTests(unittest.TestCase):
    """The new single pass must produce exactly what UMML 2.0.2 produced."""

    def test_prepared_bytes_match_umml_core(self):
        try:
            core = _load_legacy_core()
        except Exception as exc:  # pragma: no cover - depends on optional deps
            self.skipTest(f"UMML_core unavailable: {exc}")

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            meta = root / "meta.db"
            rows = [
                (f"chara/chr{1000 + i}/pfb_bdy{1000 + i}", f"{i:02x}" + "f" * 30, (i * 7919) - 40000)
                for i in range(40)
            ]
            rows += [
                ("sound/c/snd_voi_2000.awb", "a1" + "0" * 30, 0),
                ("sound/c/snd_voi_2000.acb", "a2" + "0" * 30, 0),
                ("movie/m/intro.usm", "a3" + "0" * 30, 0),
            ]
            _make_meta(meta, rows)

            package = root / "package"
            assets = package / "assets"
            for i in range(40):
                path = assets / f"chara/chr{1000 + i}/pfb_bdy{1000 + i}"
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(_bundle(i, 300 + i * 97))
            (assets / "sound/c").mkdir(parents=True)
            (assets / "sound/c/snd_voi_2000.awb").write_bytes(b"AFS2" + bytes(range(256)) * 3)
            (assets / "sound/c/snd_voi_2000.acb").write_bytes(b"@UTF" + bytes(range(256)) * 2)
            (assets / "movie/m").mkdir(parents=True)
            (assets / "movie/m/intro.usm").write_bytes(b"CRID" + b"\1" * 700)
            (assets / "chara/chr5000").mkdir(parents=True)
            (assets / "chara/chr5000/pfb_bdy5000").write_bytes(_bundle(99))  # not on Global
            (assets / "notes.txt").write_text("junk")

            legacy_out = root / "legacy"
            _count, legacy_missing = _legacy_decrypt(core, meta, assets, legacy_out)
            legacy = {p.name: p.read_bytes() for p in legacy_out.rglob("*") if p.is_file()}

            store = ManagerStore(root / "manager")
            record = ModRecord("pack", "Pack", version="1", source_path=str(package))
            prepared = LegacyAssetAdapter(store, meta).prepare(record)
            output = Path(prepared.prepared_path)
            ours = {
                Path(target).name: (output / target).read_bytes()
                for target in prepared.files
            }

            self.assertEqual(set(ours), set(legacy))
            for name, data in legacy.items():
                self.assertEqual(ours[name], data, name)
            self.assertEqual(legacy_missing, 1)

    def test_flat_and_source_trees_share_one_copy(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            meta = root / "meta.db"
            _make_meta(meta, [("chara/a/b", "c0" + "1" * 30, 5)])
            package = root / "package"
            path = package / "assets" / "chara/a/b"
            path.parent.mkdir(parents=True)
            path.write_bytes(_bundle(3))
            store = ManagerStore(root / "manager")
            record = ModRecord("one", "One", version="1", source_path=str(package))
            prepared = LegacyAssetAdapter(store, meta).prepare(record)
            output = Path(prepared.prepared_path)
            target = next(iter(prepared.files))
            flat = output / target
            per_source = output / prepared.source_roots["chara/a/b"] / target
            self.assertEqual(flat.read_bytes(), per_source.read_bytes())
            if hasattr(os, "link"):
                self.assertTrue(os.path.samefile(flat, per_source))


if __name__ == "__main__":
    unittest.main()

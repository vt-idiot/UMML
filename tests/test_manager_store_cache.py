from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from umml_manager import store as store_module
from umml_manager.models import ModRecord, Profile
from umml_manager.resolver import resolve_profile
from umml_manager.store import ManagerStore
from umml_manager.ui_library_actions import LibraryActions


class _Var:
    def __init__(self, value=""):
        self.value = value

    def get(self):
        return self.value

    def set(self, value):
        self.value = value


class DocumentCacheTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = ManagerStore(Path(self.temp.name))
        self.store.save_mod(ModRecord("a", "A", files={"aa/one": "1" * 64}))

    def tearDown(self):
        self.temp.cleanup()

    def _count_parses(self):
        return patch.object(
            store_module, "_read_json_object", wraps=store_module._read_json_object
        )

    def test_unchanged_registry_is_parsed_once(self):
        with self._count_parses() as reader:
            for _ in range(5):
                self.store.list_mods()
        self.assertEqual(reader.call_count, 1)

    def test_callers_get_independent_copies(self):
        first = self.store.list_mods()[0]
        first.files["zz/evil"] = "2" * 64
        first.name = "changed but not saved"
        again = self.store.list_mods()[0]
        self.assertEqual(again.name, "A")
        self.assertNotIn("zz/evil", again.files)

    def test_own_write_is_seen_immediately(self):
        self.store.list_mods()
        self.store.save_mod(ModRecord("b", "B"))
        self.assertEqual(sorted(m.id for m in self.store.list_mods()), ["a", "b"])

    def test_rewrite_by_another_process_is_seen(self):
        self.store.list_mods()
        path = self.store.paths.registry
        data = json.loads(path.read_text(encoding="utf-8"))
        data["mods"][0]["name"] = "Edited elsewhere"
        # Another program replaces the file (new file identity and mtime).
        replacement = path.with_suffix(".other")
        replacement.write_text(json.dumps(data), encoding="utf-8")
        os.replace(replacement, path)
        self.assertEqual(self.store.list_mods()[0].name, "Edited elsewhere")

    def test_documents_stamp_changes_on_profile_save(self):
        before = self.store.documents_stamp()
        self.store.save_profile(Profile("Default", ["a"]))
        self.assertNotEqual(self.store.documents_stamp(), before)


class ResolutionCacheTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        store = ManagerStore(Path(self.temp.name))
        store.save_mod(ModRecord("a", "A", prepared_path="/p", files={"aa/one": "1" * 64}))
        store.save_profile(Profile("Default", ["a"]))
        self.app = object.__new__(LibraryActions)
        self.app.store = store
        self.app.profile_name = _Var("Default")
        self.app.region = _Var("global")
        self.app.installation_key = _Var("")
        self.app.metadata_fingerprint = _Var("")

    def tearDown(self):
        self.temp.cleanup()

    def test_same_inputs_reuse_the_plan(self):
        with patch(
            "umml_manager.ui_library_actions.resolve_profile", wraps=resolve_profile
        ) as resolver:
            first = self.app.current_resolution()
            second = self.app.current_resolution()
        self.assertIs(first, second)
        self.assertEqual(resolver.call_count, 1)

    def test_profile_change_or_setting_change_recomputes(self):
        first = self.app.current_resolution()
        self.app.store.save_profile(Profile("Default", []))
        second = self.app.current_resolution()
        self.assertIsNot(first, second)
        self.assertEqual(list(first.winners), ["aa/one"])
        self.assertEqual(second.winners, {})
        self.app.region.set("japan")
        self.assertIsNot(self.app.current_resolution(), second)


if __name__ == "__main__":
    unittest.main()

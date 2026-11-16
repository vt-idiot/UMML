from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from umml_manager import safety
from umml_manager.linking import link_or_copy


class ReplaceRetryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.source = self.root / "source"
        self.source.write_bytes(b"new")
        self.target = self.root / "target"
        self.target.write_bytes(b"old")
        self.real_replace = safety.os.replace

    def tearDown(self):
        self.temp.cleanup()

    def _flaky(self, failures):
        calls = {"n": 0}

        def replace(src, dst):
            calls["n"] += 1
            if calls["n"] <= failures:
                raise PermissionError(13, "Access is denied", str(dst))
            return self.real_replace(src, dst)

        return replace, calls

    def test_brief_lock_is_ridden_out_on_windows(self):
        replace, calls = self._flaky(2)
        with patch.object(safety, "_WINDOWS", True), \
             patch.object(safety.os, "replace", replace), \
             patch.object(safety.time, "sleep") as sleep:
            safety.atomic_copy_file(self.source, self.target)
        self.assertEqual(self.target.read_bytes(), b"new")
        self.assertEqual(calls["n"], 3)
        self.assertEqual(sleep.call_count, 2)

    def test_lasting_lock_names_the_file_and_the_game(self):
        replace, _calls = self._flaky(99)
        with patch.object(safety, "_WINDOWS", True), \
             patch.object(safety.os, "replace", replace), \
             patch.object(safety.time, "sleep"):
            with self.assertRaises(PermissionError) as caught:
                safety.atomic_copy_file(self.source, self.target)
        self.assertIn("in use by another program", str(caught.exception))
        self.assertEqual(caught.exception.filename, str(self.target))
        self.assertEqual(self.target.read_bytes(), b"old")
        self.assertEqual(sorted(p.name for p in self.root.iterdir()), ["source", "target"])

    def test_no_retry_off_windows(self):
        replace, calls = self._flaky(1)
        with patch.object(safety, "_WINDOWS", False), \
             patch.object(safety.os, "replace", replace):
            with self.assertRaises(PermissionError):
                safety.atomic_copy_file(self.source, self.target)
        self.assertEqual(calls["n"], 1)

    def test_busy_link_target_is_not_silently_copied_over(self):
        replace, _calls = self._flaky(99)
        with patch.object(safety, "_WINDOWS", True), \
             patch.object(safety.os, "replace", replace), \
             patch.object(safety.time, "sleep"):
            with self.assertRaises(PermissionError):
                link_or_copy(self.source, self.target)
        self.assertEqual(self.target.read_bytes(), b"old")
        self.assertFalse(any(p.name.endswith(".tmp") for p in self.root.iterdir()))


if __name__ == "__main__":
    unittest.main()

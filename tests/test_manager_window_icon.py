from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from umml_manager import window_icon


class _Root:
    def __init__(self, fail=False):
        self.calls = []
        self.fail = fail

    def iconbitmap(self, default=None):
        if self.fail:
            raise RuntimeError("bad icon")
        self.calls.append(default)

    def iconphoto(self, *_args):
        if self.fail:
            raise RuntimeError("bad icon")
        self.calls.append("photo")


class WindowIconTests(unittest.TestCase):
    def test_bundled_icon_is_shipped(self):
        self.assertIsNotNone(window_icon.icon_path())

    def test_missing_icon_is_ignored(self):
        with patch.object(window_icon, "icon_path", return_value=None):
            self.assertFalse(window_icon.apply_window_icon(_Root()))

    def test_windows_uses_iconbitmap_for_all_windows(self):
        root = _Root()
        with patch.object(window_icon.sys, "platform", "win32"):
            self.assertTrue(window_icon.apply_window_icon(root))
        self.assertTrue(str(root.calls[0]).endswith("bourb.ico"))

    def test_errors_never_escape(self):
        with tempfile.TemporaryDirectory() as temp:
            broken = Path(temp) / "bourb.ico"
            broken.write_bytes(b"not an icon")
            with patch.object(window_icon, "icon_path", return_value=broken):
                self.assertFalse(window_icon.apply_window_icon(_Root(fail=True)))


if __name__ == "__main__":
    unittest.main()

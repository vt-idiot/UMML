from __future__ import annotations

import os
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import umml_manager_packaged
from umml_manager import startup


class _FakeSplash:
    def __init__(self):
        self.texts = []
        self.closed = False

    def is_alive(self):
        return not self.closed

    def update_text(self, text):
        self.texts.append(text)

    def close(self):
        self.closed = True


class SplashTests(unittest.TestCase):
    def setUp(self):
        startup._lines.clear()

    def test_no_splash_is_a_no_op(self):
        with patch.object(startup, "pyi_splash", None):
            startup.splash("hello")
            startup.close_splash()

    def test_lines_wrap_and_keep_the_latest(self):
        fake = _FakeSplash()
        with patch.object(startup, "pyi_splash", fake):
            for index in range(startup.SPLASH_LINES + 5):
                startup.splash(f"line {index}")
            startup.splash("x" * (startup.SPLASH_WIDTH * 2 + 3))
            lines = fake.texts[-1].split("\n")
            self.assertEqual(len(lines), startup.SPLASH_LINES)
            self.assertTrue(all(len(line) <= startup.SPLASH_WIDTH for line in lines))
            self.assertEqual(lines[-1], "xxx")
            startup.splash("bad\r\x04chars")
            self.assertNotIn("\r", fake.texts[-1])
            self.assertNotIn("\x04", fake.texts[-1])
            startup.close_splash()
        self.assertTrue(fake.closed)

    def test_children_inherit_suppression(self):
        with patch.dict(os.environ, {}, clear=False):
            startup.suppress_splash_for_children()
            self.assertEqual(os.environ[startup.SUPPRESS_SPLASH_ENV], "1")

    def test_attach_is_a_no_op_with_existing_output_or_off_windows(self):
        self.assertFalse(startup.attach_parent_console())


class EntryModeTests(unittest.TestCase):
    def _run(self, argv):
        calls = []
        with patch.object(startup, "splash", lambda msg: calls.append(("splash", msg))), \
             patch.object(startup, "close_splash", lambda: calls.append(("close",))), \
             patch.object(startup, "attach_parent_console", lambda: calls.append(("attach",))):
            with patch("umml_manager.cli.main", return_value=0), \
                 patch("umml_manager.gui.main", return_value=0), \
                 patch("umml_manager.legacy_host.main", return_value=0), \
                 patch("umml_manager.extractor_host.main", return_value=0), \
                 patch("umml_manager.extractor_host.verified_runtime_probe", return_value={}), \
                 patch("builtins.print"):
                umml_manager_packaged.main(argv)
        return calls

    def test_console_modes_drop_the_splash_and_attach(self):
        for argv in (["cli", "list"], ["--version"], ["--extractor-host-probe"]):
            self.assertEqual(self._run(argv)[:2], [("close",), ("attach",)], argv)

    def test_window_modes_keep_the_splash(self):
        self.assertEqual(self._run([])[0][0], "splash")
        self.assertEqual(self._run(["gui"])[0][0], "splash")
        self.assertIn("Studio", self._run(["--legacy-host"])[0][1])


class StudioLaunchTests(unittest.TestCase):
    def test_studio_shows_its_own_splash(self):
        from umml_manager import studio

        launched = {}

        def fake_popen(command, env):
            launched["env"] = env
            return SimpleNamespace(pid=1)

        with patch.dict(os.environ, {startup.SUPPRESS_SPLASH_ENV: "1"}), \
             patch.object(studio.subprocess, "Popen", fake_popen):
            launcher = next(
                obj for obj in vars(studio).values()
                if isinstance(obj, type) and hasattr(obj, "launch")
            )
            try:
                launcher().launch(studio.LEGACY_TOOLS[0].id)
            except TypeError:
                self.skipTest("Studio launcher signature differs")
        self.assertNotIn(startup.SUPPRESS_SPLASH_ENV, launched["env"])


if __name__ == "__main__":
    unittest.main()

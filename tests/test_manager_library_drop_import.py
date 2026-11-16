from __future__ import annotations

import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from umml_manager import dragdrop
from umml_manager.ui_library_actions import LibraryActions


class _Store:
    def __init__(self):
        self.calls = []

    def import_folder(self, path, reference=False):
        self.calls.append(("folder", Path(path).name, reference))
        return SimpleNamespace(id=Path(path).name, name=Path(path).name)

    def import_archive(self, path, reference=False):
        self.calls.append(("archive", Path(path).name, reference))
        if Path(path).suffix == ".rar":
            raise RuntimeError("Unsupported archive; use ZIP or TAR")
        return SimpleNamespace(id=Path(path).name, name=Path(path).name)


class _App(LibraryActions):
    def __init__(self):
        self.store = _Store()
        self.root = None
        self.status = SimpleNamespace(set=lambda value: setattr(self, "last_status", value))
        self.library = SimpleNamespace(
            tree=SimpleNamespace(exists=lambda _i: True, selection_set=lambda _i: None, see=lambda _i: None)
        )

    def _reference_local_imports(self):
        return True

    def _run_task(self, _label, operation, finished):
        finished(operation())

    def refresh(self):
        pass

    def show_selected_mod(self):
        pass

    def show_page(self, _name):
        pass


class ImportPathsTests(unittest.TestCase):
    def test_files_are_archives_and_each_folder_is_one_mod(self):
        import tempfile

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            folder = root / "Shorter Skirt"
            (folder / "assets").mkdir(parents=True)
            # A ZIP next to a folder must not be picked up from inside it.
            (folder / "Shorter Skirt.zip").write_bytes(b"PK")
            other = root / "Container of mods"
            (other / "inner mod" / "assets").mkdir(parents=True)
            cake = root / "cake.zip"
            cake.write_bytes(b"PK")
            rar = root / "new.rar"
            rar.write_bytes(b"Rar!")
            app = _App()
            with patch("umml_manager.ui_library_actions.messagebox.showwarning") as warn:
                app.import_paths([folder, other, cake, rar, root / "missing"])
        self.assertEqual(
            app.store.calls,
            [
                ("folder", "Shorter Skirt", True),
                ("folder", "Container of mods", True),
                ("archive", "cake.zip", True),
                ("archive", "new.rar", True),
            ],
        )
        self.assertEqual(app.last_status, "Imported 3 mod(s); 2 failed")
        message = warn.call_args.args[1]
        self.assertIn("new.rar", message)
        self.assertIn("missing", message)


class DragDropTests(unittest.TestCase):
    def setUp(self):
        try:
            import tkinter

            self.root = tkinter.Tk()
            self.root.withdraw()
        except Exception as exc:  # pragma: no cover - no display
            self.skipTest(f"Tk unavailable: {exc}")

    def tearDown(self):
        self.root.destroy()

    def test_without_tkdnd_nothing_is_registered(self):
        with patch.object(dragdrop, "tkdnd_dirs", return_value=[]):
            import tkinter

            label = tkinter.Label(self.root)
            self.assertFalse(dragdrop.enable_file_drop([label], lambda _paths: None))

    def test_drop_delivers_paths_with_spaces(self):
        tcl9 = str(self.root.tk.call("info", "tclversion")).startswith("9")
        if not dragdrop.tkdnd_dirs(tcl9):
            self.skipTest("tkdnd is not installed here")
        import tkinter

        label = tkinter.Label(self.root)
        received = []
        self.assertTrue(dragdrop.enable_file_drop([label], received.append))
        script = self.root.tk.call("bind", label._w, "<<Drop>>")
        command = str(script).split()[0]
        result = self.root.tk.call(command, "{C:/Mods/Shorter Skirt} C:/Mods/cake.zip")
        self.assertEqual(str(result), "copy")
        self.root.update()
        self.assertEqual(received, [[Path("C:/Mods/Shorter Skirt"), Path("C:/Mods/cake.zip")]])


if __name__ == "__main__":
    unittest.main()

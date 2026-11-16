from __future__ import annotations

import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from umml_manager.discovery import ModCandidate
from umml_manager.ui_discover_actions import DiscoverActions


class _Tree:
    def __init__(self, children, selected):
        self.children = list(children)
        self.selected = list(selected)
        self.selection_set_calls = []

    def selection(self):
        return tuple(self.selected)

    def get_children(self):
        return tuple(self.children)

    def exists(self, item):
        return True

    def selection_set(self, items):
        self.selection_set_calls.append(list(items))

    def see(self, _item):
        pass


class _Store:
    def __init__(self, fail=()):
        self.calls = []
        self.fail = set(fail)

    def _record(self, kind, path, reference):
        self.calls.append((kind, Path(path).name, reference))
        if Path(path).name in self.fail:
            raise RuntimeError("broken package")
        return SimpleNamespace(id=Path(path).name, name=Path(path).name)

    def import_folder(self, path, reference=False):
        return self._record("folder", path, reference)

    def import_archive(self, path, reference=False):
        return self._record("archive", path, reference)


class _App(DiscoverActions):
    def __init__(self, selected, fail=()):
        self.local_candidates = {
            str(index): ModCandidate(
                path=Path(f"/M/mods/{name}"),
                name=name,
                kind="archive" if name.endswith(".zip") else "folder",
                confidence="high",
                reason="test",
            )
            for index, name in enumerate(["skirt", "cake.zip", "chibi", "other"])
        }
        self.discover = SimpleNamespace(local_tree=_Tree(self.local_candidates, selected))
        self.library = SimpleNamespace(tree=_Tree([], []))
        self.store = _Store(fail)
        self.status = SimpleNamespace(set=lambda value: setattr(self, "last_status", value))
        self.root = None
        self.pages = []
        self.single = []

    def _reference_local_imports(self):
        return True

    def _run_task(self, _label, operation, finished):
        finished(operation())

    def _import(self, operation):
        self.single.append(operation())

    def refresh(self):
        pass

    def show_selected_mod(self):
        pass

    def show_page(self, name):
        self.pages.append(name)


class DiscoverMultiImportTests(unittest.TestCase):
    def test_every_highlighted_row_is_imported_in_list_order(self):
        # Selection order differs from list order on purpose.
        app = _App(selected=["2", "0", "1"])
        app.import_local_candidate()
        self.assertEqual(
            app.store.calls,
            [("folder", "skirt", True), ("archive", "cake.zip", True), ("folder", "chibi", True)],
        )
        self.assertEqual(app.library.tree.selection_set_calls, [["skirt", "cake.zip", "chibi"]])
        self.assertEqual(app.last_status, "Imported 3 mod(s)")
        self.assertEqual(app.pages, ["library"])

    def test_one_failure_does_not_stop_the_rest(self):
        app = _App(selected=["0", "1", "2"], fail={"cake.zip"})
        with patch("umml_manager.ui_discover_actions.messagebox.showwarning") as warn:
            app.import_local_candidate()
        self.assertEqual(len(app.store.calls), 3)
        self.assertEqual(app.last_status, "Imported 2 mod(s); 1 failed")
        self.assertIn("cake.zip", warn.call_args.args[1])

    def test_single_selection_keeps_the_normal_import_path(self):
        app = _App(selected=["3"])
        app.import_local_candidate()
        self.assertEqual([record.id for record in app.single], ["other"])
        self.assertEqual(app.store.calls, [("folder", "other", True)])


if __name__ == "__main__":
    unittest.main()

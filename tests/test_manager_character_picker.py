from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from umml_manager.models import ModRecord, Profile
from umml_manager.resolver import resolve_profile
from umml_manager.ui_character_picker import (
    CharacterPickerDialog,
    build_character_rows,
    load_character_names,
)


def _sha(seed: int) -> str:
    return f"{seed:064x}"


PACK = ModRecord(
    "bikini",
    "Bikini Pack",
    prepared_path="/p/bikini",
    files={"a1/sw": _sha(1), "b1/palmer": _sha(2)},
    pending_targets={"c1/almond": "chara/chr1112/body"},
    dormant_sources=["chara/chr1999/body"],
    target_characters={"a1/sw": "1001", "b1/palmer": "1035", "c1/almond": "1112"},
    dormant_characters={"chara/chr1999/body": "1999"},
)
PALMER = ModRecord(
    "palmer-fix",
    "Palmer Fix",
    prepared_path="/p/palmer",
    files={"b1/palmer": _sha(3)},
    target_characters={"b1/palmer": "1035"},
)
NAMES = {"1001": "Special Week", "1035": "Mejiro Palmer", "1112": "Almond Eye"}


class CharacterRowTests(unittest.TestCase):
    def test_rows_report_every_bucket_and_coverage(self):
        profile = Profile("P", ["bikini", "palmer-fix"])
        resolution = resolve_profile(profile, [PACK, PALMER])
        rows = {
            row.chara_id: row
            for row in build_character_rows(
                PACK, profile, resolution, NAMES, {"palmer-fix": "Palmer Fix"}
            )
        }
        self.assertEqual(set(rows), {"1001", "1035", "1112", "1999"})
        self.assertEqual(rows["1001"].status, "1 active")
        self.assertEqual(rows["1035"].status, "covered by Palmer Fix")
        self.assertEqual(rows["1112"].status, "1 waiting for download")
        self.assertEqual(rows["1999"].status, "not in this game yet")
        self.assertEqual(rows["1999"].name, "Character 1999")

    def test_excluded_characters_start_unticked(self):
        profile = Profile("P", ["bikini"], excluded_characters={"bikini": ["1035"]})
        rows = {
            row.chara_id: row
            for row in build_character_rows(PACK, profile, None, NAMES, {})
        }
        self.assertFalse(rows["1035"].selected)
        self.assertEqual(rows["1035"].status, "left out")
        self.assertTrue(rows["1001"].selected)

    def test_names_and_cards_come_from_master(self):
        with tempfile.TemporaryDirectory() as temp:
            master = Path(temp) / "master.mdb"
            connection = sqlite3.connect(master)
            connection.execute('CREATE TABLE text_data (category INTEGER, "index" INTEGER, text TEXT)')
            connection.execute("CREATE TABLE card_data (id INTEGER, chara_id INTEGER)")
            connection.executemany(
                "INSERT INTO text_data VALUES (?, ?, ?)",
                [(6, 1001, "Special Week"), (6, 1035, "Mejiro Palmer"), (4, 100101, "card")],
            )
            connection.executemany(
                "INSERT INTO card_data VALUES (?, ?)",
                [(100102, 1001), (100101, 1001), (103501, 1035)],
            )
            connection.commit()
            connection.close()
            names, cards = load_character_names(master)
        self.assertEqual(names, {"1001": "Special Week", "1035": "Mejiro Palmer"})
        self.assertEqual(cards, {"1001": 100101, "1035": 103501})
        self.assertEqual(load_character_names(None), ({}, {}))


class CharacterDialogTests(unittest.TestCase):
    def setUp(self):
        try:
            import tkinter as tk

            self.root = tk.Tk()
            self.root.withdraw()
        except Exception as exc:  # pragma: no cover - no display
            self.skipTest(f"Tk unavailable: {exc}")

    def tearDown(self):
        self.root.destroy()

    def test_toggle_invert_and_save(self):
        profile = Profile("P", ["bikini"])
        rows = build_character_rows(PACK, profile, None, NAMES, {})
        dialog = CharacterPickerDialog(
            self.root, mod_name="Bikini Pack", rows=rows, fill_gaps=False
        )
        dialog.grab_release()
        dialog._toggle(("1035",))
        self.assertEqual(dialog.tree.set("1035", "use"), "[ ]")
        dialog._invert()
        self.assertEqual(dialog.selected, {"1035"})
        dialog._set_all(True)
        dialog._toggle(("1035", "1999"))
        dialog.fill_gaps.set(True)
        dialog._save()
        self.assertEqual(dialog.result, ({"1035", "1999"}, True))

    def test_portrait_button_disabled_without_game_data(self):
        portraits = SimpleNamespace(available=False, cached=lambda _card: None)
        dialog = CharacterPickerDialog(
            self.root,
            mod_name="Bikini Pack",
            rows=build_character_rows(PACK, Profile("P", []), None, NAMES, {}),
            fill_gaps=True,
            portraits=portraits,
            cards={"1001": 100101},
        )
        self.assertEqual(str(dialog.portrait_button.cget("state")), "disabled")
        self.assertTrue(dialog.fill_gaps.get())
        dialog.destroy()


if __name__ == "__main__":
    unittest.main()

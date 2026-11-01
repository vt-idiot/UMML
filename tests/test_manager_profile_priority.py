from __future__ import annotations

import unittest

from umml_manager.characters import asset_group, character_of
from umml_manager.models import ModRecord, Profile
from umml_manager.resolver import resolve_profile


def _sha(seed: int) -> str:
    return f"{seed:064x}"


def _mod(mod_id: str, files: dict[str, tuple[int, str]]) -> ModRecord:
    """files: target -> (seed, logical game name)."""

    return ModRecord(
        mod_id,
        mod_id.replace("-", " ").title(),
        version="1",
        prepared_path=f"/prepared/{mod_id}",
        files={target: _sha(seed) for target, (seed, _name) in files.items()},
        target_characters={
            target: ch
            for target, (_seed, name) in files.items()
            if (ch := character_of(name))
        },
        target_groups={
            target: group
            for target, (_seed, name) in files.items()
            if (group := asset_group(name))
        },
    )


# Real asset names from "Mihono Shorter Skirt" (Mihono Bourbon = 1026).
SKIRT = _mod(
    "shorter-skirt",
    {
        "ay/AYRQ75": (1, "3d/chara/body/bdy1026_00/pfb_bdy1026_00"),
        "hh/HHCA7E": (2, "3d/chara/body/bdy1026_00/textures/tex_bdy1026_00_ctrl_wet"),
        "xy/XYCMLF": (3, "3d/chara/body/bdy1026_00/textures/tex_bdy1026_00_base_wet"),
        "u5/U5ILXT": (4, "sourceresources/3d/chara/body/bdy1026_00/materials/mtl_bdy1026_00"),
    },
)
CUTIN_FIX = _mod(
    "master-not-trainer",
    {"cc/CUT01": (10, "cutin/skill/crd102602_001/cutin_crd102602_001")},
)
CHIBI = _mod(
    "poop-chibi",
    {"mm/MINI01": (20, "3d/chara/mini/body/mbdy1026_00/pfb_mbdy1026_00")},
)
RECOLOR = _mod(
    "skirt-recolor",
    {"xy/XYCMLF": (30, "3d/chara/body/bdy1026_00/textures/tex_bdy1026_00_base_wet")},
)
PACK = _mod(
    "bikini-pack",
    {
        "ay/AYRQ75": (40, "3d/chara/body/bdy1026_00/pfb_bdy1026_00"),
        "hh/HHCA7E": (41, "3d/chara/body/bdy1026_00/textures/tex_bdy1026_00_ctrl_wet"),
        "xy/XYCMLF": (42, "3d/chara/body/bdy1026_00/textures/tex_bdy1026_00_base_wet"),
        "u5/U5ILXT": (43, "sourceresources/3d/chara/body/bdy1026_00/materials/mtl_bdy1026_00"),
        "sw/SPWEEK": (44, "3d/chara/body/bdy1001_00/pfb_bdy1001_00"),
    },
)


def _resolve(profile: Profile, *mods: ModRecord):
    return resolve_profile(profile, list(mods))


class ProfilePriorityTests(unittest.TestCase):
    def test_partial_mods_change_only_what_they_ship(self):
        result = _resolve(
            Profile("P", ["shorter-skirt", "master-not-trainer", "poop-chibi"]),
            SKIRT,
            CUTIN_FIX,
            CHIBI,
        )
        self.assertEqual(
            {target: claim.mod_id for target, claim in result.winners.items()},
            {
                "ay/AYRQ75": "shorter-skirt",
                "hh/HHCA7E": "shorter-skirt",
                "xy/XYCMLF": "shorter-skirt",
                "u5/U5ILXT": "shorter-skirt",
                "cc/CUT01": "master-not-trainer",
                "mm/MINI01": "poop-chibi",
            },
        )
        self.assertEqual(result.mixed_groups, [])

    def test_later_mod_wins_file_by_file(self):
        result = _resolve(Profile("P", ["bikini-pack", "shorter-skirt"]), PACK, SKIRT)
        self.assertEqual(result.winners["ay/AYRQ75"].mod_id, "shorter-skirt")
        self.assertEqual(result.winners["sw/SPWEEK"].mod_id, "bikini-pack")
        # The skirt replaces every file of that outfit, so nothing is mixed.
        self.assertEqual(result.mixed_groups, [])
        self.assertEqual(
            result.characters_overridden, {"bikini-pack": {"1026": ["shorter-skirt"]}}
        )

    def test_true_clash_is_reported_but_not_blocking(self):
        result = _resolve(Profile("P", ["shorter-skirt", "skirt-recolor"]), SKIRT, RECOLOR)
        self.assertEqual(result.winners["xy/XYCMLF"].mod_id, "skirt-recolor")
        self.assertEqual(result.winners["ay/AYRQ75"].mod_id, "shorter-skirt")
        self.assertEqual(len(result.mixed_groups), 1)
        mixed = result.mixed_groups[0]
        self.assertEqual(mixed.group, "3d/chara/body/bdy1026_00")
        self.assertEqual(mixed.character, "1026")
        self.assertEqual(mixed.mods, ("shorter-skirt", "skirt-recolor"))
        self.assertEqual(mixed.files["xy/XYCMLF"], "skirt-recolor")
        self.assertFalse(result.blocking_issues)

    def test_fill_gaps_mod_ranks_lowest_wherever_it_sits(self):
        profile = Profile("P", ["shorter-skirt", "bikini-pack"], fill_gaps=["bikini-pack"])
        result = _resolve(profile, SKIRT, PACK)
        self.assertEqual(result.winners["ay/AYRQ75"].mod_id, "shorter-skirt")
        self.assertEqual(result.winners["u5/U5ILXT"].mod_id, "shorter-skirt")
        self.assertEqual(result.winners["sw/SPWEEK"].mod_id, "bikini-pack")
        self.assertEqual(result.mixed_groups, [])

    def test_unticked_character_falls_through(self):
        profile = Profile(
            "P",
            ["bikini-pack", "shorter-skirt"],
            excluded_characters={"shorter-skirt": ["1026"]},
        )
        result = _resolve(profile, PACK, SKIRT)
        self.assertEqual(result.winners["ay/AYRQ75"].mod_id, "bikini-pack")
        self.assertEqual(result.characters_withheld, {"shorter-skirt": ["1026"]})

    def test_unticked_everywhere_leaves_stock(self):
        profile = Profile("P", ["shorter-skirt"], excluded_characters={"shorter-skirt": ["1026"]})
        result = _resolve(profile, SKIRT)
        self.assertEqual(result.winners, {})

    def test_mods_without_index_data_behave_as_before(self):
        plain_a = ModRecord("a", "A", prepared_path="/p/a", files={"x1/f": _sha(1)})
        plain_b = ModRecord("b", "B", prepared_path="/p/b", files={"x1/f": _sha(2)})
        result = _resolve(Profile("P", ["a", "b"]), plain_a, plain_b)
        self.assertEqual(result.winners["x1/f"].mod_id, "b")
        self.assertEqual(result.mixed_groups, [])

    def test_profile_round_trip_keeps_character_settings(self):
        profile = Profile(
            "P",
            ["bikini-pack"],
            excluded_characters={"bikini-pack": ["1035", "1001", "1035"]},
            fill_gaps=["bikini-pack"],
        )
        again = Profile.from_dict(profile.to_dict())
        self.assertEqual(again.excluded_characters, {"bikini-pack": ["1001", "1035"]})
        self.assertEqual(again.fill_gaps, ["bikini-pack"])


class NameParsingTests(unittest.TestCase):
    def test_character_ids(self):
        cases = {
            "3d/chara/body/bdy1001_00/pfb_bdy1001_00": "1001",
            "3d/chara/body/bdy100101/pfb_bdy100101": "1001",
            "3d/chara/head/chr1067_00/pfb_chr1067_00_face": "1067",
            "chara/chr1067/chr_icon_1067": "1067",
            "sound/v/snd_voi_chara_1035_01.awb": "1035",
            "3d/chara/body/bdy0001_00/pfb_bdy0001_00": None,
            "uianimation/flash/common/fl_common_btn": None,
            "chara/chr10670/oops": None,
            "AYRQ75JJ6FVTL3CVBEAZBH6KT7DXXJQ4": None,
        }
        for name, expected in cases.items():
            self.assertEqual(character_of(name), expected, name)
        self.assertEqual(character_of("renamed", "3d/chara/body/bdy1067_00/x"), "1067")

    def test_asset_groups(self):
        cases = {
            "3d/chara/body/bdy1026_00/pfb_bdy1026_00": "3d/chara/body/bdy1026_00",
            "sourceresources/3d/chara/body/bdy1026_00/materials/mtl_bdy1026_00": (
                "3d/chara/body/bdy1026_00"
            ),
            "3d/chara/body/bdy1026_01/pfb_bdy1026_01": "3d/chara/body/bdy1026_01",
            "chara/chr1026/chr_icon_1026": "chara/chr1026",
            "uianimation/flash/common/fl_btn": None,
        }
        for name, expected in cases.items():
            self.assertEqual(asset_group(name), expected, name)


if __name__ == "__main__":
    unittest.main()

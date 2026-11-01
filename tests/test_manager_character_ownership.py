from __future__ import annotations

import unittest

from umml_manager.characters import character_of
from umml_manager.models import ModRecord, Profile
from umml_manager.resolver import resolve_profile


def _sha(seed: int) -> str:
    return f"{seed:064x}"


def _mod(mod_id: str, files: dict[str, tuple[int, str | None]]) -> ModRecord:
    return ModRecord(
        mod_id,
        mod_id.title(),
        version="1",
        prepared_path=f"/prepared/{mod_id}",
        files={target: _sha(seed) for target, (seed, _ch) in files.items()},
        target_characters={target: ch for target, (_seed, ch) in files.items() if ch},
    )


# Bikini pack: Special Week (1001) and Mejiro Palmer (1035), plus a shared file.
PACK = _mod(
    "bikini",
    {
        "a1/sw_body": (1, "1001"),
        "a2/sw_tex": (2, "1001"),
        "b1/palmer_body": (3, "1035"),
        "b2/palmer_tex": (4, "1035"),
        "c1/shared_shader": (5, None),
    },
)
# Dedicated Palmer mod: only her body, not her texture file.
PALMER = _mod("palmer-fix", {"b1/palmer_body": (10, "1035")})
# Some other mod touching the shared file.
SHARED = _mod("shader-tweak", {"c1/shared_shader": (20, None)})


def _resolve(profile: Profile, *mods: ModRecord):
    return resolve_profile(profile, list(mods))


class CharacterOwnershipTests(unittest.TestCase):
    def test_higher_mod_takes_the_whole_character(self):
        result = _resolve(Profile("P", ["bikini", "palmer-fix"]), PACK, PALMER)
        self.assertEqual(result.winners["b1/palmer_body"].mod_id, "palmer-fix")
        # The pack's other Palmer file must not be mixed in.
        self.assertNotIn("b2/palmer_tex", result.winners)
        self.assertEqual(result.winners["a1/sw_body"].mod_id, "bikini")
        self.assertEqual(result.character_owners, {"1001": "bikini", "1035": "palmer-fix"})
        self.assertEqual(result.characters_withheld, {"bikini": ["1035"]})

    def test_lower_mod_loses_the_whole_character(self):
        result = _resolve(Profile("P", ["palmer-fix", "bikini"]), PACK, PALMER)
        self.assertEqual(result.winners["b1/palmer_body"].mod_id, "bikini")
        self.assertEqual(result.winners["b2/palmer_tex"].mod_id, "bikini")
        self.assertEqual(result.character_owners["1035"], "bikini")

    def test_fill_gaps_pack_never_outranks_normal_mods(self):
        profile = Profile("P", ["palmer-fix", "shader-tweak", "bikini"], fill_gaps=["bikini"])
        result = _resolve(profile, PACK, PALMER, SHARED)
        self.assertEqual(result.winners["b1/palmer_body"].mod_id, "palmer-fix")
        self.assertNotIn("b2/palmer_tex", result.winners)
        # Characters nobody else covers still come from the pack.
        self.assertEqual(result.winners["a1/sw_body"].mod_id, "bikini")
        # Shared files: the fill-gaps pack also ranks lowest.
        self.assertEqual(result.winners["c1/shared_shader"].mod_id, "shader-tweak")

    def test_excluded_character_is_left_stock(self):
        profile = Profile("P", ["bikini"], excluded_characters={"bikini": ["1035"]})
        result = _resolve(profile, PACK)
        self.assertNotIn("b1/palmer_body", result.winners)
        self.assertNotIn("b2/palmer_tex", result.winners)
        self.assertIn("a1/sw_body", result.winners)
        self.assertEqual(result.characters_withheld, {"bikini": ["1035"]})
        self.assertNotIn("1035", result.character_owners)

    def test_exclusion_hands_the_character_to_the_next_mod(self):
        profile = Profile(
            "P",
            ["bikini", "palmer-fix"],
            excluded_characters={"palmer-fix": ["1035"]},
        )
        result = _resolve(profile, PACK, PALMER)
        self.assertEqual(result.winners["b1/palmer_body"].mod_id, "bikini")
        self.assertEqual(result.winners["b2/palmer_tex"].mod_id, "bikini")

    def test_shared_files_keep_last_mod_wins(self):
        result = _resolve(Profile("P", ["shader-tweak", "bikini"]), PACK, SHARED)
        self.assertEqual(result.winners["c1/shared_shader"].mod_id, "bikini")
        conflict = next(c for c in result.conflicts if c.path == "c1/shared_shader")
        self.assertEqual(conflict.overridden, ("shader-tweak",))

    def test_mods_without_character_data_behave_as_before(self):
        plain_a = ModRecord("a", "A", prepared_path="/p/a", files={"x1/f": _sha(1)})
        plain_b = ModRecord("b", "B", prepared_path="/p/b", files={"x1/f": _sha(2)})
        result = _resolve(Profile("P", ["a", "b"]), plain_a, plain_b)
        self.assertEqual(result.winners["x1/f"].mod_id, "b")
        self.assertEqual(result.character_owners, {})

    def test_profile_round_trip_keeps_character_settings(self):
        profile = Profile(
            "P",
            ["bikini"],
            excluded_characters={"bikini": ["1035", "1001", "1035"]},
            fill_gaps=["bikini"],
        )
        again = Profile.from_dict(profile.to_dict())
        self.assertEqual(again.excluded_characters, {"bikini": ["1001", "1035"]})
        self.assertEqual(again.fill_gaps, ["bikini"])


class CharacterIdTests(unittest.TestCase):
    def test_known_name_shapes(self):
        cases = {
            "3d/chara/body/bdy1001_00/pfb_bdy1001_00": "1001",
            "3d/chara/body/bdy100101/pfb_bdy100101": "1001",
            "3d/chara/head/chr1067_00/pfb_chr1067_00_face": "1067",
            "chara/chr1067/chr_icon_1067": "1067",
            "sound/v/snd_voi_chara_1035_01.awb": "1035",
            "assets/Dressmod/chr1035/tex_bdy1035_00_diff": "1035",
            "3d/chara/body/bdy0001_00/pfb_bdy0001_00": None,
            "3d/chara/tail/tail0001_00/pfb_tail0001_00": None,
            "uianimation/flash/common/fl_common_btn": None,
            "chara/chr10670/oops": None,
        }
        for name, expected in cases.items():
            self.assertEqual(character_of(name), expected, name)

    def test_falls_back_to_the_second_name(self):
        self.assertEqual(character_of("renamed_by_modder", "3d/chara/body/bdy1067_00/x"), "1067")


if __name__ == "__main__":
    unittest.main()

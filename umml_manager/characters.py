"""Which character an asset belongs to, for whole-character mod ownership.

Umamusume asset names carry the 4-digit character ID (1001 = Special Week)
in a few shapes, for example:

    3d/chara/body/bdy1001_00/pfb_bdy1001_00
    3d/chara/body/bdy100101/...        (6-digit dress ID = chara + costume)
    3d/chara/head/chr1067_00/...
    chara/chr1067/chr_icon_1067
    sound/v/snd_voi_chara_1067_...

Generic bodies (bdy0001...) and anything without a character ID count as
shared assets and keep the normal per-file "later mod wins" rule.
"""

from __future__ import annotations

import re
from collections import Counter
from typing import Iterable

# A playable character ID is 1001-1999. Optionally followed by a 2-digit
# costume suffix (dress IDs). Must not be part of a longer number.
_CHARACTER = re.compile(
    r"(?:^|[/_.\-])(?:chr|bdy|chara|chara_|char)(1\d{3})(?:\d{2})?(?!\d)",
    re.IGNORECASE,
)


def character_of(*names: str) -> str | None:
    """Return the character ID found in the first name that has one."""

    for name in names:
        if not name:
            continue
        found = _CHARACTER.findall(name.replace("\\", "/"))
        if found:
            # A path normally names one character; if several appear, the most
            # frequent one wins and the earliest breaks ties.
            counts = Counter(found)
            return max(found, key=lambda value: (counts[value], -found.index(value)))
    return None


def characters_in(names: Iterable[str]) -> set[str]:
    return {value for value in (character_of(name) for name in names) if value}


_GROUP_SEGMENT = re.compile(r"\d{4}")


def asset_group(name: str) -> str | None:
    """The asset folder a file belongs to, for "true clash" detection.

    Files in one group are meant to be used together (a body's mesh,
    textures and materials), so a group assembled from two different mods is
    worth a warning. The group is the path up to and including the first
    segment that carries an ID:

        3d/chara/body/bdy1026_00/pfb_bdy1026_00                 -> 3d/chara/body/bdy1026_00
        sourceresources/3d/chara/body/bdy1026_00/materials/x    -> 3d/chara/body/bdy1026_00
        chara/chr1026/chr_icon_1026                             -> chara/chr1026
    """

    if not name:
        return None
    path = name.replace("\\", "/").strip("/")
    if path.casefold().startswith("sourceresources/"):
        path = path[len("sourceresources/"):]
    segments = [segment for segment in path.split("/") if segment]
    for index, segment in enumerate(segments):
        if _GROUP_SEGMENT.search(segment):
            if index == len(segments) - 1:
                # The ID is only in the file name: the file is its own group.
                return path
            return "/".join(segments[: index + 1])
    return None

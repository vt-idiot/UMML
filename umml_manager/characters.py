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

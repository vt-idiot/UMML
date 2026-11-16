"""Window icon for the Manager and Studio windows (bourb.ico)."""

from __future__ import annotations

import sys
from pathlib import Path

ICON_NAME = "bourb.ico"


def icon_path() -> Path | None:
    candidates = []
    bundle_root = getattr(sys, "_MEIPASS", None)
    if bundle_root:
        candidates.append(Path(bundle_root) / ICON_NAME)
    candidates.append(Path(__file__).resolve().parent / "assets" / ICON_NAME)
    for path in candidates:
        if path.is_file():
            return path
    return None


def apply_window_icon(root) -> bool:
    """Set the icon for ``root`` and every window opened after it.

    Never raises: a missing or unreadable icon just leaves Tk's default.
    """

    path = icon_path()
    if path is None:
        return False
    try:
        if sys.platform == "win32":
            root.iconbitmap(default=str(path))
            return True
        from PIL import Image, ImageTk

        with Image.open(path) as image:
            image = image.convert("RGBA").resize((64, 64))
            photo = ImageTk.PhotoImage(image, master=root)
        root.iconphoto(True, photo)
        root._umml_icon = photo  # keep a reference so Tk does not drop it
        return True
    except Exception:
        return False

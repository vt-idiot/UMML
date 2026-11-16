"""Accept files and folders dropped from Explorer onto Tk widgets.

Tk has no native drag and drop; this loads the tkdnd Tcl extension
(the same one tkinterdnd2 ships). The packaged build bundles it as a
``tkdnd`` folder next to the other runtime files; running from source uses
an installed ``tkinterdnd2`` if there is one. Without either, drop targets
are simply not registered and everything else works as before.
"""

from __future__ import annotations

import os
import platform
import sys
from pathlib import Path
from typing import Callable, Iterable

DND_FILES = "DND_Files"
_LOADED_ATTR = "_umml_tkdnd_version"


def _platform_folder() -> str | None:
    system = platform.system()
    machine = os.environ.get("PROCESSOR_ARCHITECTURE", platform.machine()) if system == "Windows" else platform.machine()
    return {
        ("Windows", "AMD64"): "win-x64",
        ("Windows", "ARM64"): "win-arm64",
        ("Windows", "x86"): "win-x86",
        ("Linux", "x86_64"): "linux-x64",
        ("Linux", "aarch64"): "linux-arm64",
        ("Darwin", "arm64"): "osx-arm64",
        ("Darwin", "x86_64"): "osx-x64",
    }.get((system, machine))


def tkdnd_dirs(tcl9: bool = False) -> list[Path]:
    """Folders that may hold tkdnd's pkgIndex.tcl, best first.

    tkdnd is built separately for Tcl 8 and Tcl 9; ``tcl9`` picks which.
    """

    found: list[Path] = []
    bundle_root = getattr(sys, "_MEIPASS", None)
    if bundle_root:
        found.append(Path(bundle_root) / ("tkdnd-tcl9" if tcl9 else "tkdnd"))
    folder = _platform_folder()
    if folder:
        try:
            import tkinterdnd2  # optional, only used when running from source

            base = Path(tkinterdnd2.__file__).resolve().parent / "tkdnd"
            found.append(base / (folder + "-tcl9" if tcl9 else folder))
        except Exception:
            pass
    return [path for path in found if (path / "pkgIndex.tcl").is_file()]


def load_tkdnd(widget) -> bool:
    """Load tkdnd into the widget's Tcl interpreter once. Never raises."""

    root = widget.winfo_toplevel()
    if getattr(root, _LOADED_ATTR, None):
        return True
    try:
        tcl9 = int(str(root.tk.call("info", "tclversion")).split(".")[0]) >= 9
    except Exception:
        tcl9 = False
    for folder in tkdnd_dirs(tcl9):
        try:
            root.tk.call("lappend", "auto_path", str(folder))
            version = root.tk.call("package", "require", "tkdnd")
        except Exception:
            continue
        setattr(root, _LOADED_ATTR, str(version))
        return True
    return False


def enable_file_drop(widgets: Iterable, on_drop: Callable[[list[Path]], None]) -> bool:
    """Let files/folders be dropped onto ``widgets``; ``on_drop`` gets their paths.

    Returns False (and does nothing) when tkdnd is unavailable.
    """

    widgets = list(widgets)
    if not widgets or not load_tkdnd(widgets[0]):
        return False
    for widget in widgets:
        def dropped(data, _widget=widget):
            try:
                paths = [Path(item) for item in _widget.tk.splitlist(data) if item]
            except Exception:
                paths = []
            if paths:
                # Let tkdnd finish the drop before doing any work.
                _widget.after_idle(lambda: on_drop(paths))
            return "copy"

        def hovering(*_args):
            return "copy"

        try:
            widget.tk.call("tkdnd::drop_target", "register", widget._w, DND_FILES)
            drop_command = widget.register(dropped)
            hover_command = widget.register(hovering)
            widget.tk.call("bind", widget._w, "<<Drop>>", f"{drop_command} %D")
            widget.tk.call("bind", widget._w, "<<DropEnter>>", hover_command)
            widget.tk.call("bind", widget._w, "<<DropPosition>>", hover_command)
        except Exception:
            return False
    return True

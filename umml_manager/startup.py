"""Startup helpers for the packaged Windows build.

The single-file build uses PyInstaller's windowed bootloader with a splash
screen, so there is no console window flashing up at launch. That means:

* startup progress goes to the splash instead of a console
  (``splash``/``close_splash``; no-ops when there is no splash);
* command-line modes attach to the terminal they were started from
  (``attach_parent_console``), like a console program would.
"""

from __future__ import annotations

import os
import sys
import textwrap

try:  # Only present in builds made with a splash screen.
    import pyi_splash  # type: ignore[import-not-found]
except Exception:  # noqa: BLE001 - any failure just means "no splash"
    pyi_splash = None

# The splash text is anchored bottom-left in the left column of splash.png and
# grows upward; these fit Consolas at 12 px there.
SPLASH_LINES = 26
SPLASH_WIDTH = 36
# Children started by the Manager (extractor host, probes) inherit this and
# skip the splash; Studio removes it again because it is a window of its own.
SUPPRESS_SPLASH_ENV = "PYINSTALLER_SUPPRESS_SPLASH_SCREEN"

_lines: list[str] = []


def splash(message: str) -> None:
    """Add a line to the splash log, if a splash screen is showing."""

    if pyi_splash is None:
        return
    for line in str(message).splitlines() or [""]:
        _lines.extend(textwrap.wrap(line, SPLASH_WIDTH) or [""])
    del _lines[:-SPLASH_LINES]
    try:
        if pyi_splash.is_alive():
            # \r ends a message in the splash IPC protocol and \x04 closes it.
            text = "\n".join(_lines).replace("\r", "").replace("\x04", "")
            pyi_splash.update_text(text)
    except Exception:  # noqa: BLE001 - the splash is cosmetic
        pass


def close_splash() -> None:
    global pyi_splash
    if pyi_splash is None:
        return
    try:
        if pyi_splash.is_alive():
            pyi_splash.close()
    except Exception:  # noqa: BLE001
        pass
    pyi_splash = None


def suppress_splash_for_children() -> None:
    os.environ[SUPPRESS_SPLASH_ENV] = "1"


def attach_parent_console() -> bool:
    """Send output to the terminal this process was started from.

    A windowed Windows program has no stdout/stderr. When one was started
    from cmd or PowerShell, attaching to that console makes ``cli`` output
    appear there. When output is already redirected (pipes, files), nothing
    changes. Returns True if a console was attached.
    """

    if os.name != "nt" or sys.stdout is not None:
        return False
    try:
        import ctypes

        attach = ctypes.WinDLL("kernel32", use_last_error=True).AttachConsole
        attach.argtypes = (ctypes.c_uint32,)
        attach.restype = ctypes.c_int
        if not attach(0xFFFFFFFF):  # ATTACH_PARENT_PROCESS
            return False
        sys.stdout = open("CONOUT$", "w", encoding="utf-8", errors="replace", buffering=1)
        sys.stderr = open("CONOUT$", "w", encoding="utf-8", errors="replace", buffering=1)
        if sys.stdin is None:
            sys.stdin = open("CONIN$", "r", encoding="utf-8", errors="replace")
        return True
    except Exception:  # noqa: BLE001
        return False

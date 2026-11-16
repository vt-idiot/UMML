"""Keep hidden pages out of the main window on Windows.

On Windows every Tk widget is a child window, hidden or not, and Windows
spends time on every child window of a top-level on every step of a window
drag (measured ~16 us each; ~250 of them make dragging visibly lag). Hidden
pages therefore made the Manager drag slowly even though nothing was shown.

``PageParking`` re-parents a hidden page's holder window under an invisible
window of its own with Win32 SetParent, and back before the page is shown
again. Tk is not involved: it still sees the holder as a child of the content
area, and its widgets keep their windows and state. Elsewhere it does nothing.
"""

from __future__ import annotations

import os


class PageParking:
    def __init__(self) -> None:
        self._user32 = None
        self._hwnd = None
        if os.name != "nt":
            return
        try:
            import ctypes
            from ctypes import wintypes

            user32 = ctypes.WinDLL("user32", use_last_error=True)
            user32.CreateWindowExW.argtypes = (
                wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD,
                ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                wintypes.HWND, wintypes.HMENU, wintypes.HINSTANCE, wintypes.LPVOID,
            )
            user32.CreateWindowExW.restype = wintypes.HWND
            user32.SetParent.argtypes = (wintypes.HWND, wintypes.HWND)
            user32.SetParent.restype = wintypes.HWND
            user32.DestroyWindow.argtypes = (wintypes.HWND,)
            user32.DestroyWindow.restype = wintypes.BOOL
            WS_POPUP = 0x80000000
            # An invisible, never-shown popup of a predefined class: no window
            # procedure of our own is needed just to hold hidden children.
            hwnd = user32.CreateWindowExW(
                0, "STATIC", "Uma Mod Manager hidden pages", WS_POPUP,
                0, 0, 0, 0, None, None, None, None,
            )
            if hwnd:
                self._user32 = user32
                self._hwnd = hwnd
        except Exception:  # noqa: BLE001 - parking is an optimisation only
            self._user32 = None
            self._hwnd = None

    @property
    def available(self) -> bool:
        return self._hwnd is not None

    def park(self, hwnd: int) -> bool:
        """Move ``hwnd`` (a hidden holder) under the invisible window."""

        if not self.available or not hwnd:
            return False
        return bool(self._user32.SetParent(hwnd, self._hwnd))

    def restore(self, hwnd: int, parent_hwnd: int) -> bool:
        """Put ``hwnd`` back under its Tk parent before it is shown."""

        if not self.available or not hwnd or not parent_hwnd:
            return False
        return bool(self._user32.SetParent(hwnd, parent_hwnd))

    def close(self) -> None:
        if self.available:
            try:
                self._user32.DestroyWindow(self._hwnd)
            except Exception:  # noqa: BLE001
                pass
        self._hwnd = None

from __future__ import annotations

import os
import unittest
from unittest.mock import patch

from umml_manager.win_parking import PageParking


class PageParkingTests(unittest.TestCase):
    def test_off_windows_it_is_a_no_op(self):
        with patch("umml_manager.win_parking.os.name", "posix"):
            parking = PageParking()
        self.assertFalse(parking.available)
        self.assertFalse(parking.park(123))
        self.assertFalse(parking.restore(123, 456))
        parking.close()

    def test_park_and_restore_call_setparent(self):
        calls = []

        class FakeUser32:
            def SetParent(self, child, parent):
                calls.append((child, parent))
                return 1

        parking = object.__new__(PageParking)
        parking._user32 = FakeUser32()
        parking._hwnd = 999
        self.assertTrue(parking.park(10))
        self.assertTrue(parking.restore(10, 20))
        self.assertFalse(parking.restore(10, 0))
        self.assertEqual(calls, [(10, 999), (10, 20)])


if __name__ == "__main__":
    unittest.main()

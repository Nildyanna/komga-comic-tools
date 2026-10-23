"""Tests for komga_title_fix.py (the title rule). Run: python test_title_fix.py"""
import os, sys, unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from komga_title_fix import is_generic, new_title


class RuleTests(unittest.TestCase):
    def test_generic_titles(self):
        for t in ["Volume 1", "Volume Three", "volume 12", "Vol. 3", "Vol 3", "TPB", "Omnibus", "Hardcover", "Tome 2"]:
            self.assertTrue(is_generic(t), t)

    def test_real_titles_are_left_alone(self):
        for t in ["Distant Rumblings!", "Volume 3: Death of a Goblin", "The Final Chapter", "", None, "Part of the Problem", "Booked", "Part Three", "Part IV", "Book Four", "Book 2"]:
            self.assertFalse(is_generic(t), t)

    def test_numbered_volumes(self):
        self.assertEqual(new_title("Volume Three", "Thunderbolts Classic", "3"), "Thunderbolts Classic Vol. 3")
        self.assertEqual(new_title("Volume 1", "Hawkeye & The Thunderbolts", "1"), "Hawkeye & The Thunderbolts Vol. 1")
        self.assertEqual(new_title("Vol. 2", "Hawkeye & The Thunderbolts", 2), "Hawkeye & The Thunderbolts Vol. 2")

    def test_one_offs_take_the_series_name(self):
        self.assertEqual(new_title("TPB", "Thunderbolts: Worldstrike", "1"), "Thunderbolts: Worldstrike")
        self.assertEqual(new_title("Omnibus", "Daredevil By Chip Zdarsky Omnibus", None), "Daredevil By Chip Zdarsky Omnibus")

    def test_nothing_sensible_means_no_change(self):
        self.assertIsNone(new_title("Distant Rumblings!", "Thunderbolts", "-1"))
        self.assertIsNone(new_title("Volume 1", "", "1"))
        self.assertIsNone(new_title("Volume 1", "Series", None))      # a numbered volume needs its number


if __name__ == "__main__":
    unittest.main()

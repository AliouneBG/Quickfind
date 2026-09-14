import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from qf import hotkey


class TestParse(unittest.TestCase):
    def test_alt_space(self):
        mods, vk = hotkey.parse("alt+space")
        self.assertEqual(mods, hotkey.MOD_ALT)
        self.assertEqual(vk, 0x20)

    def test_multiple_modifiers(self):
        mods, vk = hotkey.parse("ctrl+shift+f")
        self.assertEqual(mods, hotkey.MOD_CONTROL | hotkey.MOD_SHIFT)
        self.assertEqual(vk, ord("F"))

    def test_case_and_whitespace_insensitive(self):
        self.assertEqual(hotkey.parse("  CTRL + Space "), hotkey.parse("ctrl+space"))

    def test_modifier_aliases(self):
        self.assertEqual(hotkey.parse("control+k"), hotkey.parse("ctrl+k"))
        self.assertEqual(hotkey.parse("win+k"), hotkey.parse("super+k"))

    def test_function_keys(self):
        self.assertEqual(hotkey.parse("f1")[1], 0x70)
        self.assertEqual(hotkey.parse("f12")[1], 0x7B)

    def test_digit_key(self):
        self.assertEqual(hotkey.parse("alt+1")[1], ord("1"))

    def test_no_modifier_is_allowed(self):
        mods, vk = hotkey.parse("f13")
        self.assertEqual(mods, 0)

    def test_alt_tab_rejected_with_guidance(self):
        with self.assertRaises(hotkey.HotkeyError) as ctx:
            hotkey.parse("alt+tab")
        message = str(ctx.exception).lower()
        self.assertIn("reserved", message)
        self.assertIn("alt+space", message)

    def test_plain_tab_still_allowed(self):
        self.assertEqual(hotkey.parse("ctrl+tab")[1], 0x09)

    def test_rejects_empty(self):
        with self.assertRaises(hotkey.HotkeyError):
            hotkey.parse("")
        with self.assertRaises(hotkey.HotkeyError):
            hotkey.parse("+++")

    def test_rejects_modifiers_only(self):
        with self.assertRaises(hotkey.HotkeyError):
            hotkey.parse("ctrl+alt")

    def test_rejects_two_real_keys(self):
        with self.assertRaises(hotkey.HotkeyError):
            hotkey.parse("ctrl+a+b")

    def test_rejects_unknown_key(self):
        with self.assertRaises(hotkey.HotkeyError):
            hotkey.parse("ctrl+nonsense")


if __name__ == "__main__":
    unittest.main(verbosity=2)

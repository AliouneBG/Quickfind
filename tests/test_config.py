import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import quickfind


class TestLoadConfig(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, "config.json")
        self._real = quickfind.config_path
        quickfind.config_path = lambda: self.path

    def tearDown(self):
        quickfind.config_path = self._real
        self.tmp.cleanup()

    def write(self, payload):
        with open(self.path, "w", encoding="utf-8") as fh:
            json.dump(payload, fh)

    def read(self):
        with open(self.path, encoding="utf-8") as fh:
            return json.load(fh)

    def test_creates_file_with_defaults(self):
        cfg = quickfind.load_config()
        self.assertEqual(cfg, quickfind.DEFAULT_CONFIG)
        self.assertEqual(self.read(), quickfind.DEFAULT_CONFIG)

    def test_stored_values_win(self):
        self.write({"hotkey": "ctrl+space", "opacity": 0.5})
        cfg = quickfind.load_config()
        self.assertEqual(cfg["hotkey"], "ctrl+space")
        self.assertEqual(cfg["opacity"], 0.5)

    def test_missing_keys_are_filled_and_written_back(self):
        self.write({"hotkey": "ctrl+space"})
        cfg = quickfind.load_config()
        self.assertEqual(cfg["opacity"], quickfind.DEFAULT_CONFIG["opacity"])
        self.assertIn("opacity", self.read(), "new settings must become visible")
        self.assertEqual(self.read()["hotkey"], "ctrl+space",
                         "write-back must not clobber user values")

    def test_unchanged_file_is_not_rewritten(self):
        quickfind.load_config()
        before = os.path.getmtime(self.path)
        quickfind.load_config()
        self.assertEqual(os.path.getmtime(self.path), before)

    def test_corrupt_file_falls_back_without_overwriting(self):
        with open(self.path, "w", encoding="utf-8") as fh:
            fh.write("{not json")
        cfg = quickfind.load_config()
        self.assertEqual(cfg, quickfind.DEFAULT_CONFIG)
        with open(self.path, encoding="utf-8") as fh:
            self.assertEqual(fh.read(), "{not json",
                             "a broken config must not be silently destroyed")

    def test_defaults_cover_every_setting_the_app_reads(self):
        for key in ("hotkey", "roots", "excludes", "supplement", "max_results", "opacity",
                    "fuzzy", "preview", "tray", "elevate", "refresh_after_hours"):
            self.assertIn(key, quickfind.DEFAULT_CONFIG)


if __name__ == "__main__":
    unittest.main(verbosity=2)

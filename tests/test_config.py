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
                    "fuzzy", "preview", "video_preview", "tray", "elevate",
                    "refresh_after_hours"):
            self.assertIn(key, quickfind.DEFAULT_CONFIG)


if __name__ == "__main__":
    unittest.main(verbosity=2)


class TestHostileConfig(unittest.TestCase):
    """A hand-edited config used to be able to stop the app starting at all."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, "config.json")
        self._real = quickfind.config_path
        quickfind.config_path = lambda: self.path

    def tearDown(self):
        quickfind.config_path = self._real
        self.tmp.cleanup()

    def write_raw(self, text):
        with open(self.path, "w", encoding="utf-8") as fh:
            fh.write(text)

    def test_valid_json_that_is_not_an_object_falls_back(self):
        # Each of these raised out of dict.update, and the traceback went to a
        # log file while the window simply never appeared.
        for text in ("[1, 2, 3]", '"hello"', "42", "null"):
            with self.subTest(text=text):
                self.write_raw(text)
                cfg = quickfind.load_config()
                self.assertEqual(cfg["roots"], quickfind.DEFAULT_CONFIG["roots"])
                self.assertEqual(cfg["max_results"],
                                 quickfind.DEFAULT_CONFIG["max_results"])

    def test_a_setting_of_the_wrong_type_is_ignored(self):
        self.write_raw(json.dumps({"max_results": "lots", "opacity": "high"}))
        cfg = quickfind.load_config()
        self.assertEqual(cfg["max_results"],
                         quickfind.DEFAULT_CONFIG["max_results"])
        self.assertEqual(cfg["opacity"], quickfind.DEFAULT_CONFIG["opacity"])

    def test_roots_as_a_bare_string_is_ignored(self):
        # The quiet one: "C:\\" iterates as three one-character roots, so the
        # index came back almost empty with nothing reported.
        self.write_raw(json.dumps({"roots": "C:\\"}))
        cfg = quickfind.load_config()
        self.assertEqual(cfg["roots"], quickfind.DEFAULT_CONFIG["roots"])

    def test_a_list_of_non_strings_is_ignored(self):
        self.write_raw(json.dumps({"excludes": [1, 2, 3]}))
        cfg = quickfind.load_config()
        self.assertEqual(cfg["excludes"],
                         quickfind.DEFAULT_CONFIG["excludes"])

    def test_true_is_not_a_number(self):
        # bool is an int in Python, so this needs saying explicitly.
        self.write_raw(json.dumps({"watch_quiet_seconds": True}))
        cfg = quickfind.load_config()
        self.assertEqual(cfg["watch_quiet_seconds"],
                         quickfind.DEFAULT_CONFIG["watch_quiet_seconds"])

    def test_a_nonsense_number_is_replaced(self):
        self.write_raw(json.dumps({"max_results": -5}))
        cfg = quickfind.load_config()
        self.assertGreater(cfg["max_results"], 0)

    def test_live_folders_takes_a_switch_or_a_list(self):
        for value in (True, False, [r"C:\Downloads"]):
            with self.subTest(value=value):
                self.write_raw(json.dumps({"live_folders": value}))
                self.assertEqual(quickfind.load_config()["live_folders"], value)
        self.write_raw(json.dumps({"live_folders": 7}))
        self.assertEqual(quickfind.load_config()["live_folders"],
                         quickfind.DEFAULT_CONFIG["live_folders"])

    def test_good_settings_alongside_bad_ones_survive(self):
        self.write_raw(json.dumps({"max_results": "lots", "opacity": 0.5}))
        cfg = quickfind.load_config()
        self.assertEqual(cfg["opacity"], 0.5)
        self.assertEqual(cfg["max_results"],
                         quickfind.DEFAULT_CONFIG["max_results"])

    def test_the_rewrite_leaves_no_temporary_behind(self):
        self.write_raw(json.dumps({"max_results": "lots"}))
        quickfind.load_config()
        self.assertFalse(os.path.exists(self.path + ".tmp"))
        # And what it wrote back is loadable, with the bad value gone.
        with open(self.path, encoding="utf-8") as fh:
            self.assertEqual(json.load(fh)["max_results"],
                             quickfind.DEFAULT_CONFIG["max_results"])

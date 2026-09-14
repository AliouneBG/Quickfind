"""Saying what a result is in one word, so a list can be read at a glance."""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from qf import kinds


class TestKinds(unittest.TestCase):
    def test_a_folder_is_a_folder(self):
        self.assertEqual(kinds.kind_of("Downloads", is_dir=True), kinds.FOLDER)

    def test_a_folder_wins_over_its_name(self):
        # A folder called "setup" is still a folder.
        self.assertEqual(kinds.kind_of("setup.exe", is_dir=True), kinds.FOLDER)

    def test_a_program(self):
        self.assertEqual(kinds.kind_of("PotPlayerMini64.exe"), kinds.APP)

    def test_an_installer_is_not_the_program(self):
        # The case this was written for: a search for a media player returned
        # the installer above the player itself, and the rows looked alike.
        self.assertEqual(kinds.kind_of("PotPlayerSetup64.exe"), kinds.INSTALLER)
        self.assertEqual(kinds.kind_of("PotPlayerSetup64_154.exe"),
                         kinds.INSTALLER)

    def test_installer_packages(self):
        for name in ("thing.msi", "thing.msix", "thing.appx"):
            self.assertEqual(kinds.kind_of(name), kinds.INSTALLER, name)

    def test_an_updater_is_an_installer_too(self):
        self.assertEqual(kinds.kind_of("ChromeUpdate.exe"), kinds.INSTALLER)

    def test_an_uninstaller_is_left_as_a_program(self):
        # Calling it an installer would be precisely the wrong way round, and
        # its own name is already the warning.
        self.assertEqual(kinds.kind_of("Uninstall.exe"), kinds.APP)
        self.assertEqual(kinds.kind_of("uninstaller.exe"), kinds.APP)

    def test_the_word_has_to_be_in_the_name(self):
        self.assertEqual(kinds.kind_of("instrument.exe"), kinds.APP)

    def test_shortcuts(self):
        self.assertEqual(kinds.kind_of("PotPlayer 64 bit.lnk"), kinds.SHORTCUT)
        self.assertEqual(kinds.kind_of("bookmark.url"), kinds.SHORTCUT)

    def test_a_library_is_not_a_program(self):
        self.assertEqual(kinds.kind_of("PotPlayer64.dll"), kinds.LIBRARY)

    def test_media(self):
        self.assertEqual(kinds.kind_of("holiday.mp4"), kinds.VIDEO)
        self.assertEqual(kinds.kind_of("logo.png"), kinds.IMAGE)
        self.assertEqual(kinds.kind_of("song.flac"), kinds.AUDIO)

    def test_documents(self):
        self.assertEqual(kinds.kind_of("contract.pdf"), kinds.PDF)
        self.assertEqual(kinds.kind_of("letter.docx"), kinds.DOC)
        self.assertEqual(kinds.kind_of("budget.xlsx"), kinds.SHEET)
        self.assertEqual(kinds.kind_of("deck.pptx"), kinds.SLIDES)
        self.assertEqual(kinds.kind_of("notes.txt"), kinds.TEXT)

    def test_code_and_scripts(self):
        self.assertEqual(kinds.kind_of("ui.py"), kinds.CODE)
        self.assertEqual(kinds.kind_of("build.bat"), kinds.SCRIPT)
        self.assertEqual(kinds.kind_of("QuickFind.vbs"), kinds.SCRIPT)

    def test_archives(self):
        self.assertEqual(kinds.kind_of("backup.zip"), kinds.ARCHIVE)
        self.assertEqual(kinds.kind_of("disc.iso"), kinds.ARCHIVE)

    def test_case_does_not_matter(self):
        self.assertEqual(kinds.kind_of("HOLIDAY.MP4"), kinds.VIDEO)
        self.assertEqual(kinds.kind_of("SETUP.EXE"), kinds.INSTALLER)

    def test_an_unknown_extension_says_nothing(self):
        # Better a blank column than a label that adds no information.
        self.assertEqual(kinds.kind_of("PotPlayerMini64.dpl"), "")
        self.assertEqual(kinds.kind_of("noextension"), "")

    def test_every_label_is_short_enough_to_scan(self):
        labels = {value for name, value in vars(kinds).items()
                  if name.isupper() and isinstance(value, str)}
        for label in labels:
            self.assertLessEqual(len(label), 9, label)


if __name__ == "__main__":
    unittest.main(verbosity=2)

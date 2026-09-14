"""What a result is, in one word.

A list of files tells you their names and where they live, which is not always
enough to say what you are about to open. Searching for a media player returned
the installer, the uninstaller shortcut, a logo, a playlist and the program
itself, all with similar names and similar icons.

The shell can name a file's type, but it is wordy and inconsistent ("Application
extension", "PotPlayer Playlist File"), and it costs a call per extension. These
labels are short enough to scan down a column and cost nothing.
"""
from __future__ import annotations

import os

FOLDER = "Folder"
APP = "App"
INSTALLER = "Installer"
SHORTCUT = "Shortcut"
SCRIPT = "Script"
VIDEO = "Video"
IMAGE = "Image"
AUDIO = "Audio"
PDF = "PDF"
DOC = "Doc"
SHEET = "Sheet"
SLIDES = "Slides"
TEXT = "Text"
CODE = "Code"
ARCHIVE = "Archive"
FONT = "Font"
DATA = "Data"
LIBRARY = "Library"
FILE = ""


def _spread(kind, extensions):
    return {"." + e: kind for e in extensions.split()}


KINDS: dict[str, str] = {}
KINDS.update(_spread(APP, "exe com"))
KINDS.update(_spread(INSTALLER, "msi msix appx appxbundle msixbundle"))
KINDS.update(_spread(SHORTCUT, "lnk url"))
KINDS.update(_spread(SCRIPT, "bat cmd ps1 vbs vbe wsf sh"))
KINDS.update(_spread(LIBRARY, "dll sys ocx cpl ax drv"))
KINDS.update(_spread(VIDEO, "mp4 m4v mkv mov avi wmv webm mpg mpeg ts m2ts flv 3gp"))
KINDS.update(_spread(IMAGE, "jpg jpeg png gif bmp webp tif tiff heic ico svg psd ai"))
KINDS.update(_spread(AUDIO, "mp3 wav flac m4a aac ogg wma opus mid"))
KINDS.update(_spread(PDF, "pdf"))
KINDS.update(_spread(DOC, "doc docx odt rtf pages epub"))
KINDS.update(_spread(SHEET, "xls xlsx xlsm ods csv tsv"))
KINDS.update(_spread(SLIDES, "ppt pptx odp key"))
KINDS.update(_spread(TEXT, "txt md markdown rst log nfo"))
KINDS.update(_spread(CODE, """
py pyw js jsx ts tsx html htm css scss java c h cpp hpp cs go rs rb php sql
lua pl swift kt r m mm ipynb
""".replace("\n", " ")))
KINDS.update(_spread(ARCHIVE, "zip 7z rar tar gz bz2 xz iso cab"))
KINDS.update(_spread(FONT, "ttf otf ttc woff woff2"))
KINDS.update(_spread(DATA, "json xml yaml yml toml ini cfg db sqlite sqlite3 dat"))

# An executable named like this is something you run once, not the program you
# were looking for. This is what separated the media player from its installer.
INSTALLER_WORDS = ("setup", "install", "installer", "update", "updater")
RUNNABLE = (APP, INSTALLER)


def kind_of(name: str, is_dir: bool = False) -> str:
    """A short label for a row, or "" when nothing useful can be said."""
    if is_dir:
        return FOLDER
    stem, extension = os.path.splitext(name.lower())
    kind = KINDS.get(extension, FILE)
    if kind in RUNNABLE and _looks_like_an_installer(stem):
        return INSTALLER
    return kind


def _looks_like_an_installer(stem: str) -> bool:
    # "uninstall" is left alone: it is its own warning, and calling it an
    # installer would be the wrong way round.
    if "uninstall" in stem:
        return False
    return any(word in stem for word in INSTALLER_WORDS)

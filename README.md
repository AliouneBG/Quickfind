# QuickFind

A Spotlight-style file launcher for Windows. Press a hotkey, type, open.

QuickFind indexes every file and folder on the machine and searches roughly 1.4
million entries in 15 to 60 ms per keystroke. It uses only the Python standard
library, so there is nothing to install, no virtualenv, and no build step.

## Features

* Global hotkey overlay that appears over whatever you are doing.
* Substring search on file and folder names, with multi-word path filtering.
* Abbreviation matching, so `qckfnd` finds `quickfind`.
* Ranking that prefers apps, your own files, and things you have opened before.
* Windows shell icons on every row.
* Preview pane with thumbnails for images and video, and text excerpts for code.
* Videos play a short silent clip in the preview, so you can tell which one it is.
* Live index updates through the NTFS change journal when running as admin.
* Tray icon, single-instance handling, and a config file.

## Requirements

* Windows 10 or 11
* Python 3.12 or newer (it needs `os.DirEntry.is_junction`)
* An NTFS volume for the fast indexer

## Running

```sh
python quickfind.py
```

On first launch QuickFind asks for administrator rights. This is not cosmetic.
See [Administrator mode](#administrator-mode) for what it buys and what it
costs. If you decline, everything still works with slower indexing and no live
updates.

To run without a console window, double-click `QuickFind.vbs`. To start it at
login, put a shortcut to that file in `shell:startup`.

To skip the UAC prompt entirely, register a logon task once from an elevated
prompt:

```sh
python quickfind.py --install-task
```

Undo that with `--uninstall-task`.

## Using it

| Key | Action |
| --- | --- |
| `Alt+Space` | Show or hide the launcher |
| `Up` / `Down` | Move the selection |
| `PgUp` / `PgDn` | Move a page at a time |
| Mouse hover | Selects the row under the cursor |
| `Enter` | Open the selection |
| `Ctrl+Enter` | Show it in Explorer |
| `Esc` | Dismiss |

Clicking away also dismisses it. The preview pane follows the selection, and a
selected video starts playing a short silent clip about a second later.

The status line reports how many files matched, not just how many fit on
screen. Typing `resume` on a machine with hundreds of them reads `40 of 535`
and suggests adding a word, because a second word filters by folder and
usually cuts the list to one.

The tray icon has Show, Rebuild index, and Quit. On Windows 11 new tray icons
start hidden behind the `^` chevron, so drag it out if you want it pinned.

`Alt+Tab` cannot be used as the hotkey. Windows reserves it and `RegisterHotKey`
refuses to bind it. QuickFind rejects it with an explanation rather than failing
silently.

### Query syntax

**One word** matches anywhere in a file or folder name:

```
report
```

**Extra words** narrow by path. This finds a name containing `report` that lives
somewhere under a path containing `downloads`:

```
report downloads
```

**Abbreviations** work when nothing else matches. `qckfnd` finds `quickfind`,
and `ntpd` finds `notepad.exe`. These rank strictly below real substring matches
and are labelled *approximate* in the status line.

### Commands

Type a colon to run a command instead of searching.

| Command | Effect |
| --- | --- |
| `:reindex` | Rebuild the index now |
| `:status` | Show entry count, index source, and remembered paths |
| `:forget` | Erase the record of what you have opened |
| `:quit` | Exit QuickFind |

### It learns what you open

Opening something through QuickFind is remembered, and that file ranks higher
next time. One open is worth about as much as matching on a word boundary, and
a file you open often reaches the cap. The boost halves every 30 days, so last
month's habits fade behind this month's.

Showing a file in Explorer with `Ctrl+Enter` counts exactly the same as opening
it. Both mean you found what you were after, and only the follow-up differs.

This is deliberately bounded. History reorders comparable matches but never
promotes something that does not match, so searching `alpha` will not surface
`beta.txt` however often you open it.

QuickFind records paths you opened through QuickFind and nothing else. The file
lives at `%LOCALAPPDATA%\quickfind\usage.json` on this machine only. `:forget`
erases it and `frecency: false` turns it off.

## Configuration

Settings live in `%APPDATA%\quickfind\config.json`, created on first run.
Restart to apply changes. When a new setting is added, it is written into your
file automatically, so the file always shows everything available.

| Setting | Default | Meaning |
| --- | --- | --- |
| `hotkey` | `alt+space` | Global hotkey: `ctrl`, `alt`, `shift`, `win` plus a key |
| `roots` | `["C:\\"]` | What to index |
| `excludes` | `$recycle.bin`, ... | Folder names the directory walk skips |
| `supplement` | `["C:\\Windows"]` | Walked after an MFT build to recover hardlink names |
| `max_results` | `40` | Rows shown |
| `opacity` | `0.92` | Window transparency, from `0.35` to `1.0` |
| `fuzzy` | `true` | Abbreviation fallback |
| `frecency` | `true` | Rank what you have opened before higher |
| `preview` | `true` | Preview pane. Turning it off narrows the window |
| `video_preview` | `true` | Play a short silent clip when a video is selected |
| `tray` | `true` | Notification area icon |
| `elevate` | `true` | Ask for administrator at startup |
| `watch` | `true` | Live index updates via the NTFS change journal |
| `watch_quiet_seconds` | `6` | Settle time before rebuilding after changes |
| `min_rebuild_seconds` | `90` | Minimum gap between rebuilds |
| `refresh_after_hours` | `12` | Rebuild a cached index older than this |

## Administrator mode

Two features need elevation, because both read the NTFS volume directly rather
than going through the filesystem.

**MFT enumeration.** NTFS keeps a Master File Table listing every file on the
volume. Reading it finds everything at once instead of walking directories one
at a time. This requires opening the drive as a raw device, which bypasses file
permissions, which is why Windows restricts it.

**The USN change journal.** NTFS logs every create, delete, and rename.
Watching it keeps the index fresh without rescanning the disk.

| | Directory walk | MFT plus supplement |
| --- | --- | --- |
| Privileges | none | administrator |
| Entries found | 755,163 | 1,456,043 |
| Build time | 42.5 s | 10.7 s |
| Live updates | no | yes |
| Worst keystroke | 17 to 28 ms | 40 to 50 ms |

Note that elevated mode searches slower. It indexes nearly twice as many files,
most of them Windows internals you will never search for, which makes every
keystroke scan a larger haystack. Elevation buys faster indexing and freshness,
and costs some search speed.

Without elevation QuickFind falls back to the directory walk and a periodic
refresh. Nothing breaks. `:status` shows which mode is active.

### Why MFT needs a supplement

`FSCTL_ENUM_USN_DATA` returns one record per MFT entry. A file reachable under
several hardlink names is therefore enumerated under only one of them. Windows
system binaries are hardlinks into WinSxS, and the effect was severe:

| Directory | On disk | MFT alone | Missing |
| --- | --- | --- | --- |
| `C:\Windows\System32` | 4,795 | 544 | 4,251 |
| `C:\Windows\SysWOW64` | 2,870 | 355 | 2,515 |
| `C:\Windows` | 110 | 98 | 12 |

`cmd.exe`, `calc.exe`, and `control.exe` were findable only under their WinSxS
paths, which is useless for a launcher. After an MFT build the directories in
`supplement` are walked normally and merged, which costs about 5 seconds and
restores the names people actually search for. The walk overlaps what MFT
already found, so results are de-duplicated by path.

A plain directory walk never had this problem, because it sees every name.

## Performance

Measured on 1,456,043 entries with an Intel UHD 620 at 2560x1440, 200% scaling.

| Query | Latency |
| --- | --- |
| `e` (one character) | 7 ms |
| `notepad.exe` | 19 ms |
| `python` | 30 ms |
| `settings` | 34 ms |
| `report` | 37 ms |
| `py quickfind` (two words) | 16 ms |
| Query already narrowed by typing | under 1 ms |

The worst single keystroke while typing is 40 to 50 ms. Most keystrokes are far
cheaper, because typing narrows the previous candidate list instead of
rescanning. The expensive one is whichever prefix first becomes specific enough
to scan the whole haystack. Everything typed after that is nearly free.

Memory sits around 137 MB resident, about 99 bytes per indexed entry. The
on-disk cache is 53 MB.

## How it works

| File | Responsibility |
| --- | --- |
| `quickfind.py` | Entry point, config, elevation, and the Controller |
| `qf/fsindex.py` | MFT enumeration, directory walk, packed storage, cache |
| `qf/search.py` | Haystack scanning, ranking, abbreviation fallback |
| `qf/ui.py` | Tk overlay, DPI scaling, rows, preview pane |
| `qf/shellicon.py` | Shell icons and thumbnails, encoded to PNG for Tk |
| `qf/videopreview.py` | Silent video frames decoded with Media Foundation |
| `qf/usage.py` | Frecency store of what you opened and how recently |
| `qf/hotkey.py` | `RegisterHotKey` on its own message loop |
| `qf/tray.py` | `Shell_NotifyIcon` with a message-only window |
| `qf/single.py` | Named mutex and event, so a second launch wakes the first |
| `qf/watcher.py` | USN change journal reader |
| `QuickFind.vbs` | Console-free launcher |

### Threading

The Controller owns a `queue.Queue`. Every background thread, meaning the
indexer, tray, hotkey listener, change watcher, and single-instance listener,
posts to that queue. A Tk `after` loop drains it on the UI thread. No Tk call
ever happens off that thread.

### How the index is stored

Entries live in parallel arrays. Names are one packed UTF-8 `bytes` blob plus an
offsets array, and paths are rebuilt by walking a parent-index chain rather than
being stored.

Two measured reasons, both worth knowing before simplifying this:

1. A `list` of 754,000 short strings cost 52 MB, nearly all of it per-object
   overhead. That is roughly 60 bytes of Python object per 20 bytes of text.
2. A Python `str` uses one width for every character it contains. A single
   filename holding an emoji promoted the entire 15 million character haystack
   to 4 bytes per character, costing 60.5 MB instead of 15 MB. Exactly one such
   file existed on the test machine. `bytes` has no such cliff.

### Icons and the preview pane

Rows carry the real shell icon, and the selected row gets a preview: a
thumbnail, the file's type, size, and modified date, and for text an excerpt of
the first lines.

Both come from Windows through two deliberately different calls:

* **Row icons** use `SHGetFileInfo` with `SHGFI_USEFILEATTRIBUTES`, which
  resolves an icon from the extension without touching the disk, and are cached
  by extension. `.exe`, `.lnk`, and `.ico` carry their own icon, so those are
  keyed per file instead.
* **Previews** use `IShellItemImageFactory`, which reads the file and returns a
  real thumbnail: the photo, the video frame, the PDF page.

Neither runs on the UI thread. A cold icon lookup was measured at 250 ms, which
would be visible as a stall on every keystroke, so a worker thread does the
lookups and posts PNG bytes back to a queue the UI drains. Rows render
immediately and icons appear as they arrive. Previews are debounced 180 ms and
carry a token, so sweeping the mouse down the list neither hammers the disk nor
lets a slow result overwrite a newer one.

Tk cannot take an `HICON` and reads PNG but not JPEG, so `shellicon.py` encodes
everything to PNG with `zlib`. That is about 30 lines, and it keeps the project
on the standard library.

When the shell has no real thumbnail it offers a generic page glyph. Requesting
`SIIGBF_THUMBNAILONLY` first tells the two apart, so a source file shows its
code rather than a blank page.

### Video previews

A single frame often cannot tell two recordings apart, so selecting a video
plays a short silent clip in the preview pane. Tk has no video widget, so
`qf/videopreview.py` decodes sixteen real frames with Media Foundation and the
UI cycles them at 10 frames per second. Only the video stream is read, so there
is no audio to mute.

Three decisions keep it fast enough to run on a hover:

* **The decoder scales, not Python.** The source reader is asked for a small
  RGB32 output instead of the native size. Letting Media Foundation scale during
  conversion took a 1920x1080 decode from 1819 ms to 114 ms. The requested size
  keeps the source aspect ratio, since a fixed size makes the reader stretch.
* **One seek, then read forward.** Seeking to each frame in turn costs a fresh
  run from the preceding keyframe every time. Seeking once to 15% of the
  duration and reading consecutively is far cheaper. Running forward to that
  exact time is bounded, because on a long group of pictures it consumed the
  whole read budget and returned nothing.
* **Stale work is dropped, not decoded.** A clip costs about a second, so
  sweeping down a list of videos would queue one decode per row. The worker
  compares each job's token against the current selection and skips it.

Opening frames that are a flat colour are passed over, so a video that starts on
a black card does not preview as a black rectangle. Decoding stops after 1.5
seconds and animates whatever arrived, and a file Media Foundation cannot open
simply keeps its still thumbnail.

Across a 50 video sample on the development machine, decoding took a median of
703 ms and at worst 1557 ms, all of it on the worker thread with the still
thumbnail already on screen.

### How search stays fast

1. **One haystack.** Every lowercase name is concatenated into a single
   NUL-separated `bytes` buffer, so scanning is `bytes.find` in a loop at C
   speed, not a Python loop over a million strings. Scoring reads this same
   buffer in place, so it never decodes or re-lowercases a candidate.
2. **Typing narrows.** Results for a query are always a subset of results for
   any prefix of it, so an extending query re-filters the previous candidate
   list instead of rescanning. This is why the first word must match the name:
   it is what makes the subset property hold.
3. **Vague queries scan less.** A one-character query has no useful ranking to
   offer, so its candidate pool is capped far lower than a specific one's.
4. **Positions, not indexes.** Converting a haystack position into an entry
   index is a binary search over a 1.4 million element array. Candidates are
   carried as positions and converted only for the few hundred results that
   reach the result pool.
5. **Paths resolved lazily.** Rebuilding a path walks the parent chain, so it
   happens in descending score order and only until the pool is full.

Abbreviation matching runs only when substring results are sparse. It uses a
subsequence regex whose gaps exclude the next wanted character. The obvious
form, `a[^NUL]*?b[^NUL]*?c`, backtracks catastrophically and took 1.7 seconds on
an 11 character query.

### How results are ordered

Finding a match is cheap. Putting the right match first is the hard part. The
rules, in rough order of weight:

1. **How the name matched.** Exact name, then stem match, then prefix, then a
   match after a word boundary, then a plain substring. The stem rule matters
   more than it sounds. Typing `cmd` should mean `cmd.exe`, not a vendor folder
   called `cmd`, so a query with no extension that matches a whole stem counts
   as exact. It has to be tested before the prefix case, or `cmd.exe` scores as
   merely starting with `cmd`.
2. **What kind of thing it is.** Executables and shortcuts outrank documents,
   which outrank source files, which outrank data files.
3. **Where it lives.** Files under your profile beat identically named ones in
   another profile, a system folder, or a vendored copy. `AppData` does not
   count as yours, with one exception: `AppData\Local\Programs`, where per-user
   app installs actually live. Missing that exception put the real Python
   install at rank 21, behind copies bundled inside `Downloads`.
4. **Demotions.** `node_modules`, `__pycache__`, `site-packages`, `WinSxS`, and
   temp directories take a heavy penalty. `SysWOW64`, the 32-bit mirror of
   System32, and Windows' own generated shortcut folders take a mild one.
5. **Spreading.** Each repeat of a filename already shown costs more than the
   last, so eight vendored copies of `shared.txt` cannot bury the one file with
   a distinct name. The best copy still comes first, because spreading runs
   after scoring.
6. **When it was last edited.** Recently changed files rank higher, halving
   every 45 days. This is what separates several versions of a document when
   the name carries no signal at all. It is skipped inside package
   directories, because updating an editor restamps thousands of bundled files
   at once and that is not you working on them.
7. **What you have opened before.** Bounded and decaying, so it settles ties
   between otherwise equal matches. This is most of what makes a launcher feel
   like it knows what you meant. One open lifted a file from rank 3 to rank 1
   among five identically scored siblings.

`tests/evalset.py` measures this against real intent, asking questions like
whether typing `cmd` puts `cmd.exe` first, so ranking changes can be compared
rather than argued about. The current set scores 13 of 13 top-1, but it was
also tuned against, so treat it as a regression guard rather than proof.
`tests/test_ranking.py` pins each rule individually on synthetic indexes that
hold on any machine.

## Development

```sh
python -m unittest discover -s tests     # 326 tests
python quickfind.py --bench report       # time a query
python quickfind.py --selftest-mft       # verify MFT enumeration (needs admin)
```

`--selftest-mft` cross-checks MFT output against a real directory walk and
confirms every file is present at the correct rebuilt path. Run it after
touching `fsindex.py`, because the rest of the suite cannot cover that path
without elevation.

Logs go to `%LOCALAPPDATA%\quickfind\quickfind.log`. Launched through `pythonw`
there is no console, so a startup failure would otherwise leave no trace.

### Things that will bite you

* **Tk `Listbox` selection.** `exportselection` must stay `False` on the results
  widget. Tk's default ties a listbox selection to the system selection, so the
  entry's `select_range` silently clears the highlighted row and Enter opens
  nothing.
* **Activation keys belong on the toplevel**, not the entry widget. Clicking a
  row moves focus, and entry-scoped bindings stop firing there.
* **Tk objects must be freed on the main thread.** A worker holding a bound
  method keeps the Controller, and therefore the Tk root, alive on that thread.
  Freeing it there aborts the process with `Tcl_AsyncDelete`. The indexer takes
  plain values rather than `self` for this reason.
* **`explorer /select` needs one pre-quoted string.** Passing a list makes
  `subprocess` quote the whole argument, which Explorer misparses and opens
  Documents instead of selecting your file.
* **ctypes needs explicit prototypes** for window procedures and for any call
  taking a handle. Without them it assumes 32-bit integers, and pointer-sized
  message parameters or GDI handles overflow.
* **Re-`pack` restores position with `before=`.** A widget re-packed after
  `pack_forget` goes to the end of its parent's pack order, not back where it
  was.
* **Children of a withdrawn window are never mapped**, so `winfo_ismapped`
  assertions in tests pass or fail for the wrong reason unless the window is
  shown first.
* **Media Foundation only offers the codec's own format** unless the source
  reader is created with `MF_SOURCE_READER_ENABLE_ADVANCED_VIDEO_PROCESSING`.
  Without it, asking for RGB32 returns `MF_E_INVALIDMEDIATYPE`.
* **Ask for a positive stride rather than reading one.** Row order is not
  fixed, and a scaled output type publishes no `MF_MT_DEFAULT_STRIDE` to read.
  The default differed between a webm and an mkv, so one of them always came
  out upside down until the top-down stride was requested explicitly.
* **Seeking lands on a keyframe, not your timestamp.** The next samples read
  back are earlier than the time you asked for, which looks like a broken seek
  until you read forward.

### Matching against paths, and why it is not done

Searching also matches folder names would let `adobe` find the resume inside a
folder called Adobe. It was measured rather than assumed, and rejected twice:

* A full-path haystack would cost **83 MB**, because paths average 110
  characters against 20 for names. That roughly doubles memory and makes every
  scan five times longer.
* Rewarding files whose parent folder also matches sounded cheaper and scored
  worse. It dropped the ranking evaluation from 13 of 13 to 9 of 13, because
  a project folder named after the query fills the results with its source
  files.

The case that prompted it turned out to be a ranking problem rather than a
recall one: the wanted file was already in the candidate pool at rank 49. The
fixes that actually worked were removing the false signals ahead of it.

## Limitations

* Matches names, not file contents.
* The index is a snapshot. With live updates it lags by seconds. Without them it
  lags until the next rebuild. Opening a file that has been deleted reports the
  problem rather than failing silently.
* Very generic queries can miss the best match, because the candidate pool is
  capped.
* MFT enumeration reports one name per file, so hardlinked files outside the
  `supplement` directories are findable only under the single name NTFS reports.
  Adding a directory to `supplement` fixes it for that tree.
* Untested on ReFS and FAT32. Both fall back to the directory walk.
* Video previews depend on the codecs Windows has. Anything Media Foundation
  cannot open, such as ProRes, keeps its still thumbnail instead.

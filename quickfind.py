"""QuickFind - press a hotkey, search every file on the machine.

    python quickfind.py                  run the launcher
    python quickfind.py --reindex        rebuild the index, then run
    python quickfind.py --no-elevate     skip the administrator prompt
    python quickfind.py --bench word     time a query against the cached index
    python quickfind.py --selftest-mft   verify MFT enumeration (elevated)
    python quickfind.py --install-task   run elevated at logon without a UAC prompt
    python quickfind.py --uninstall-task remove that scheduled task
"""
from __future__ import annotations

import argparse
import ctypes
import json
import os
import queue
import subprocess
import sys
import threading
import time

from qf import fsindex, hotkey, search, single, tray, ui, usage, watcher

DEFAULT_CONFIG = {
    "hotkey": "alt+space",
    "roots": ["C:\\"],
    "excludes": list(fsindex.DEFAULT_EXCLUDES),
    "supplement": list(fsindex.DEFAULT_SUPPLEMENT),
    "max_results": 40,
    "opacity": 0.92,
    "fuzzy": True,
    "frecency": True,
    "preview": True,
    "tray": True,
    "elevate": True,
    "watch": True,
    "watch_quiet_seconds": 6,
    "min_rebuild_seconds": 90,
    "refresh_after_hours": 12,
}

WARM_THRESHOLD = 50_000
TASK_NAME = "QuickFind"


def log_path() -> str:
    base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    folder = os.path.join(base, "quickfind")
    os.makedirs(folder, exist_ok=True)
    return os.path.join(folder, "quickfind.log")


def log(message: str) -> None:
    """Append a line to the log.

    Launched through pythonw there is no console, so an exception at startup
    otherwise vanishes and the app just appears not to run.
    """
    try:
        stamp = time.strftime("%Y-%m-%d %H:%M:%S")
        with open(log_path(), "a", encoding="utf-8") as fh:
            fh.write(f"{stamp} [{os.getpid()}] {message}\n")
    except OSError:
        pass


def config_path() -> str:
    base = os.environ.get("APPDATA") or os.path.expanduser("~")
    folder = os.path.join(base, "quickfind")
    os.makedirs(folder, exist_ok=True)
    return os.path.join(folder, "config.json")


def load_config() -> dict:
    cfg = dict(DEFAULT_CONFIG)
    path = config_path()
    try:
        with open(path, encoding="utf-8") as fh:
            stored = json.load(fh)
    except FileNotFoundError:
        stored = {}
    except (OSError, json.JSONDecodeError) as exc:
        print(f"config unreadable ({exc}); using defaults", file=sys.stderr)
        return cfg

    cfg.update(stored)
    if set(stored) != set(cfg):
        # Write back so newly added settings are visible and editable rather
        # than being silent defaults the user has no way to discover.
        try:
            with open(path, "w", encoding="utf-8") as fh:
                json.dump(cfg, fh, indent=2)
        except OSError:
            pass
    return cfg


# ---------------------------------------------------------------------------
# Elevation
# ---------------------------------------------------------------------------

def script_path() -> str:
    return os.path.abspath(__file__)


def relaunch_elevated() -> bool:
    """Re-run this script through the UAC prompt. False if the user declines."""
    arguments = [script_path(), "--no-elevate"] + [
        a for a in sys.argv[1:] if a != "--no-elevate"
    ]
    try:
        result = ctypes.windll.shell32.ShellExecuteW(
            None, "runas", sys.executable,
            subprocess.list2cmdline(arguments),
            os.path.dirname(script_path()), 1,
        )
    except Exception:
        return False
    return result > 32  # ShellExecute returns <=32 for every failure


def install_task() -> int:
    """Register a logon task so the elevated launch skips the UAC prompt."""
    command = f'"{sys.executable}" "{script_path()}" --no-elevate'
    completed = subprocess.run(
        ["schtasks", "/Create", "/TN", TASK_NAME, "/SC", "ONLOGON",
         "/RL", "HIGHEST", "/TR", command, "/F"],
        capture_output=True, text=True,
    )
    if completed.returncode != 0:
        print(completed.stderr.strip() or "schtasks failed", file=sys.stderr)
        print("This must be run from an elevated prompt.", file=sys.stderr)
        return 1
    print(f"Registered scheduled task {TASK_NAME!r}: QuickFind will start "
          f"elevated at logon with no UAC prompt.")
    print(f"Remove it with: python {os.path.basename(script_path())} --uninstall-task")
    return 0


def uninstall_task() -> int:
    completed = subprocess.run(
        ["schtasks", "/Delete", "/TN", TASK_NAME, "/F"],
        capture_output=True, text=True,
    )
    if completed.returncode != 0:
        print(completed.stderr.strip() or "schtasks failed", file=sys.stderr)
        return 1
    print(f"Removed scheduled task {TASK_NAME!r}.")
    return 0


# ---------------------------------------------------------------------------
# Indexing worker
# ---------------------------------------------------------------------------

def _index_worker(roots, excludes, events, supplement=()) -> None:
    started = time.time()
    last_report = [0.0]

    def progress(count):
        now = time.time()
        if now - last_report[0] > 0.4:
            last_report[0] = now
            events.put(("progress", count))

    try:
        idx = fsindex.build(roots, excludes, progress=progress,
                            supplement=supplement)
        fsindex.save(idx)
        events.put(("index", (idx, time.time() - started)))
    except Exception as exc:
        events.put(("error", str(exc)))


class Controller:
    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.searcher: search.Searcher | None = None
        self.usage = usage.UsageStore(enabled=cfg.get("frecency", True))
        self.app = ui.Launcher(self, opacity=cfg.get("opacity"),
                               preview=cfg.get("preview", True))
        self._events: queue.Queue = queue.Queue()
        self._indexing = False
        self._scanned = 0
        self._change_at = None
        self._pending_changes = 0
        self._last_rebuild = 0.0
        self._drain_id = self.app.root.after(80, self._drain)

    # -- requests from other threads ---------------------------------------

    def request_show(self) -> None:
        self._events.put(("show", None))

    def request_reindex(self) -> None:
        self._events.put(("reindex", None))

    def request_quit(self) -> None:
        self._events.put(("quit", None))

    def request_refresh(self, changes: int) -> None:
        self._events.put(("changed", changes))

    # -- indexing ----------------------------------------------------------

    def start(self, force_rebuild: bool) -> None:
        cached = None if force_rebuild else fsindex.load()
        if cached is not None:
            self._install(cached)
            age_hours = (time.time() - cached.built_at) / 3600
            if age_hours > self.cfg["refresh_after_hours"]:
                self._rebuild()
        else:
            self._rebuild()

    def _rebuild(self) -> None:
        if self._indexing:
            return
        self._indexing = True
        self._last_rebuild = time.time()
        self.app.set_busy(True)
        # The worker is deliberately a free function taking plain values: a
        # bound method would keep the Controller, and therefore the Tk root,
        # alive on this thread, and Tk aborts if it is freed off the main thread.
        threading.Thread(
            target=_index_worker,
            args=(list(self.cfg["roots"]), tuple(self.cfg["excludes"]),
                  self._events, tuple(self.cfg.get("supplement", ()))),
            daemon=True, name="quickfind-index",
        ).start()

    def _drain(self) -> None:
        if not self.app.alive():
            return
        try:
            while True:
                kind, payload = self._events.get_nowait()
                if kind == "index":
                    idx, elapsed = payload
                    self._indexing = False
                    self.app.set_busy(False)
                    self._install(idx)
                    self.app.set_status(
                        f"Indexed {len(idx):,} items in {elapsed:.1f}s "
                        f"via {idx.source}"
                    )
                elif kind == "progress":
                    self._scanned = payload
                    self.app.set_status(f"Indexing... {payload:,} items")
                elif kind == "error":
                    self._indexing = False
                    self.app.set_busy(False)
                    self.app.set_status(f"Indexing failed: {payload}")
                elif kind == "show":
                    self.app.show()
                elif kind == "reindex":
                    self.app.set_status("Rebuilding index...")
                    self._rebuild()
                elif kind == "changed":
                    self._pending_changes += payload
                    self._change_at = time.time()
                elif kind == "quit":
                    self.app.root.quit()
                    return
        except queue.Empty:
            pass
        self._maybe_refresh()
        if self.app.alive():
            self._drain_id = self.app.root.after(80, self._drain)

    def _maybe_refresh(self) -> None:
        """Rebuild after the filesystem has been quiet for a moment.

        Debounced twice over: a quiet period so an unzip or a build does not
        trigger a rebuild per file, and a floor between rebuilds so a busy
        machine cannot pin the indexer. Never while the window is open, so the
        index is not swapped out from under a search in progress.
        """
        if self._change_at is None or self._indexing or self.app.is_visible():
            return
        now = time.time()
        if now - self._change_at < self.cfg.get("watch_quiet_seconds", 6):
            return
        if now - self._last_rebuild < self.cfg.get("min_rebuild_seconds", 90):
            return
        self._change_at = None
        self._pending_changes = 0
        self._rebuild()

    def stop(self) -> None:
        """Cancel the pending drain so Tk is not left holding a dead callback."""
        if self._drain_id is not None and self.app.alive():
            try:
                self.app.root.after_cancel(self._drain_id)
            except Exception:
                pass
        self._drain_id = None
        self.app.shutdown()

    def _install(self, idx) -> None:
        self.searcher = search.Searcher(idx, self.usage)
        if len(idx) > WARM_THRESHOLD:
            # Precompute the depth table off-thread; otherwise the first query
            # pays for it and lands ~150ms slower than every one after it.
            # Below the threshold it is already sub-millisecond, and spawning a
            # thread there only risks Tk objects being collected off-thread.
            threading.Thread(target=idx.depth, args=(len(idx) - 1,), daemon=True,
                             name="quickfind-warm").start()

    def record_use(self, path: str, weight: float) -> None:
        self.usage.record(path, weight)

    # -- UI callbacks ------------------------------------------------------

    def query(self, text: str):
        text = text.strip()
        if text.startswith(":"):
            return [], "Commands: :reindex  :status  :forget  :quit"
        if not text:
            if self._indexing:
                return [], f"Indexing... {self._scanned:,} items"
            return [], ""
        if self.searcher is None:
            return [], f"Indexing... {self._scanned:,} items"

        started = time.perf_counter()
        results = self.searcher.search(text, self.cfg["max_results"],
                                       fuzzy=self.cfg.get("fuzzy", True))
        elapsed_ms = (time.perf_counter() - started) * 1000
        if not results:
            return [], f"No matches  ({elapsed_ms:.0f} ms)"
        approx = sum(1 for r in results if r.fuzzy)
        note = f"{len(results)} shown"
        if approx:
            note += f" ({approx} approximate)"
        suffix = " - indexing in background" if self._indexing else ""
        return results, (f"{note}  ({elapsed_ms:.0f} ms)"
                         f"  Enter opens, Ctrl+Enter reveals{suffix}")

    def command(self, text: str) -> None:
        cmd = text[1:].strip().lower()
        if cmd in ("q", "quit", "exit"):
            self.app.root.quit()
        elif cmd in ("r", "reindex"):
            self.app.entry.delete(0, "end")
            self.app.set_status("Rebuilding index...")
            self._rebuild()
        elif cmd in ("forget", "clear"):
            remembered = len(self.usage)
            self.usage.clear()
            self.app.set_status(f"Forgot {remembered} remembered paths")
        elif cmd == "status":
            count = len(self.searcher.index) if self.searcher else 0
            source = self.searcher.index.source if self.searcher else "none"
            admin = "yes" if fsindex.is_admin() else "no"
            self.app.set_status(
                f"{count:,} items - source {source} - admin {admin} - "
                f"{len(self.usage)} remembered - hotkey {self.cfg['hotkey']}"
            )
        else:
            self.app.set_status(f"Unknown command {text!r}")

    def run(self) -> None:
        self.app.run()


def selftest_mft(report_path: str | None = None) -> int:
    """Prove the MFT path works by cross-checking it against a real walk.

    This code cannot run without elevation, so it stays unverified until
    someone runs it elevated at least once. The report is written to a file
    because an elevated relaunch gets its own console.
    """
    lines = []

    def say(text):
        lines.append(text)
        print(text, flush=True)

    say(f"admin: {fsindex.is_admin()}")
    if not fsindex.is_admin():
        say("FAIL: not elevated; MFT enumeration cannot be tested")
        _write_report(report_path, lines)
        return 1

    try:
        started = time.perf_counter()
        idx = fsindex.build_by_mft("C:")
        cold = time.perf_counter() - started
        started = time.perf_counter()
        again = fsindex.build_by_mft("C:")
        warm = time.perf_counter() - started
    except OSError as exc:
        say(f"FAIL: build_by_mft raised {exc}")
        _write_report(report_path, lines)
        return 1

    say(f"PASS: MFT enumerated {len(idx):,} entries in {cold:.2f}s "
        f"(second pass {warm:.2f}s, so {'I/O' if cold > warm * 1.5 else 'CPU'} bound)")
    if len(again) != len(idx):
        say(f"WARN: entry count differed between passes "
            f"({len(idx):,} then {len(again):,}) - the disk changed underneath")
    del again

    # Cross-check against a directory walk of this project.
    here = os.path.dirname(script_path())
    walked = fsindex.build_by_walk([here])
    expected = {walked.path(i).lower()
                for i in range(len(walked)) if not walked.isdir[i]}
    wanted_names = {walked.name(i).lower() for i in range(len(walked))}

    seen = set()
    for i in range(len(idx)):
        if idx.name(i).lower() in wanted_names:
            seen.add(idx.path(i).lower())

    missing = {p for p in expected if p not in seen}
    say(f"cross-check: {len(expected) - len(missing)}/{len(expected)} files "
        f"from {here} found with matching reconstructed paths")
    if missing:
        say("FAIL: missing from MFT index:")
        for path in sorted(missing)[:10]:
            say(f"   {path}")
    else:
        say("PASS: every walked file was present at the right path")

    depth_ok = idx.depth(len(idx) - 1) >= 0
    say(f"{'PASS' if depth_ok else 'FAIL'}: depth table built over MFT ordering")

    roots = [idx.name(i) for i in idx.roots]
    say(f"roots: {roots}")

    widest = 0
    for i in range(0, len(idx), 997):
        for ch in idx.name(i):
            widest = max(widest, ord(ch))
    say(f"widest sampled codepoint: U+{widest:04X} (UTF-16 decode intact)")

    verdict = 0 if not missing and depth_ok else 1
    say("RESULT: " + ("all checks passed" if verdict == 0 else "failures above"))
    _write_report(report_path, lines)
    return verdict


def _write_report(report_path: str | None, lines) -> None:
    target = report_path or os.path.join(
        os.environ.get("TEMP", "."), "quickfind-mft-selftest.txt")
    try:
        with open(target, "w", encoding="utf-8") as fh:
            fh.write("\n".join(lines) + "\n")
        print(f"report written to {target}")
    except OSError:
        pass


def bench(term: str) -> int:
    idx = fsindex.load()
    if idx is None:
        print("no cached index; run the launcher once first")
        return 1
    started = time.perf_counter()
    searcher = search.Searcher(idx)
    build_ms = (time.perf_counter() - started) * 1000
    print(f"index: {len(idx):,} entries, source={idx.source}")
    print(f"haystack build: {build_ms:.0f} ms")

    for i in range(1, len(term) + 1):
        prefix = term[:i]
        started = time.perf_counter()
        results = searcher.search(prefix, 40)
        elapsed = (time.perf_counter() - started) * 1000
        print(f"  typing {prefix!r:20} {len(results):3d} shown  {elapsed:7.2f} ms")

    cold = search.Searcher(idx)
    started = time.perf_counter()
    cold.search(term, 40)
    print(f"cold query {term!r}: {(time.perf_counter() - started) * 1000:.2f} ms")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(prog="quickfind")
    parser.add_argument("--reindex", action="store_true",
                        help="rebuild the index instead of loading the cache")
    parser.add_argument("--bench", metavar="TERM",
                        help="time a query against the cached index and exit")
    parser.add_argument("--no-elevate", action="store_true",
                        help="do not prompt for administrator")
    parser.add_argument("--install-task", action="store_true",
                        help="run elevated at logon with no UAC prompt")
    parser.add_argument("--uninstall-task", action="store_true",
                        help="remove the logon task")
    parser.add_argument("--selftest-mft", nargs="?", const="", metavar="REPORT",
                        help="verify MFT enumeration (requires elevation)")
    args = parser.parse_args()

    if args.bench:
        return bench(args.bench)
    if args.selftest_mft is not None:
        return selftest_mft(args.selftest_mft or None)
    if args.install_task:
        return install_task()
    if args.uninstall_task:
        return uninstall_task()

    cfg = load_config()

    log(f"start argv={sys.argv[1:]} admin={fsindex.is_admin()}")

    instance = single.SingleInstance()
    if instance.already_running:
        instance.signal_existing()
        log("another instance already holds the mutex; signalled and exiting")
        print("QuickFind is already running; brought it to the front.")
        return 0

    if cfg.get("elevate") and not args.no_elevate and not fsindex.is_admin():
        # Release the mutex first: the elevated child would otherwise see it and
        # mistake itself for a duplicate.
        instance.close()
        if relaunch_elevated():
            log("relaunched elevated; this process is handing over")
            return 0
        log("elevation declined or failed; continuing unprivileged")
        instance = single.SingleInstance()
        print("Running without administrator - indexing will use the slower "
              "directory walk. Use --no-elevate to stop asking.", file=sys.stderr)

    controller = Controller(cfg)

    try:
        listener = hotkey.HotkeyListener(cfg["hotkey"], controller.app.toggle)
        listener.start()
    except hotkey.HotkeyError as exc:
        log(f"FATAL hotkey: {exc}")
        print(f"hotkey error: {exc}", file=sys.stderr)
        print("edit " + config_path() + " to pick another combination",
              file=sys.stderr)
        instance.close()
        return 2

    instance.listen(controller.request_show)

    tray_icon = None
    if cfg.get("tray", True):
        tray_icon = tray.TrayIcon(controller.request_show,
                                  controller.request_reindex,
                                  controller.request_quit)
        if not tray_icon.start():
            print(f"tray icon unavailable: {tray_icon.error}", file=sys.stderr)
            tray_icon = None

    change_watcher = None
    if cfg.get("watch", True):
        change_watcher = watcher.UsnWatcher(cfg["roots"], controller.request_refresh)
        if not change_watcher.start():
            print(f"live index updates off ({change_watcher.error}); "
                  f"falling back to the {cfg['refresh_after_hours']}h refresh",
                  file=sys.stderr)
            change_watcher = None

    controller.start(force_rebuild=args.reindex)
    mode = "administrator" if fsindex.is_admin() else "standard user"
    live = "live index updates on" if change_watcher else "periodic refresh"
    log(f"ready as {mode}, {live}, tray={'on' if tray_icon else 'off'}")
    print(f"QuickFind running as {mode}, {live}. "
          f"Press {cfg['hotkey']} to search.")
    try:
        controller.run()
    finally:
        controller.stop()
        if change_watcher is not None:
            change_watcher.stop()
        if tray_icon is not None:
            tray_icon.stop()
        instance.close()
        listener.stop()
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except SystemExit:
        raise
    except BaseException:
        import traceback
        log("FATAL " + traceback.format_exc())
        raise

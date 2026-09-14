"""Creating Tcl interpreters, reliably, for the UI tests.

Two problems live here, both about the same thing: making a new Tcl interpreter
occasionally fails on Windows even though Tk is perfectly usable.

* Every UI test module used to open and destroy its own root at import just to
  find out whether Tk worked. Under pytest, which imports all the test modules
  back to back, two of them lost that race, set TK_AVAILABLE to False, and
  silently skipped nineteen real tests. So the probe now runs once for the whole
  run, and retries before giving up.

* The same failure hits tests that build a Launcher. It arrives as a TclError
  saying init.tcl could not be read, with "No error" as the reason, and a second
  attempt a moment later has always worked. `make` wraps that retry.
"""
import gc
import time

ATTEMPTS = 4
PAUSE_SECONDS = 0.05


def make(factory, attempts=ATTEMPTS):
    """Build a Tk object, retrying a transient interpreter-init failure."""
    for attempt in range(attempts):
        try:
            return factory()
        except Exception:
            if attempt == attempts - 1:
                raise
            gc.collect()
            time.sleep(PAUSE_SECONDS)


def _probe() -> bool:
    try:
        import tkinter as tk
    except Exception:
        return False
    try:
        root = make(tk.Tk)
    except Exception:
        return False
    root.destroy()
    gc.collect()
    return True


TK_AVAILABLE = _probe()

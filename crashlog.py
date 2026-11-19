"""
A crash must never be silent.

The released app starts with pythonw and no console window, so stdout and
stderr do not exist: an uncaught exception, or a bad command-line argument,
would otherwise end the program with nothing on screen and nothing on disk.
This module is standard library only and is installed before anything heavy
is imported, so even a missing Qt package is reported.

install() hooks the three places Python sends an unhandled error:
sys.excepthook (the main thread), threading.excepthook (any other thread) and
sys.unraisablehook (errors in __del__ and callbacks). Each one

  1. appends the traceback, with the app and Python versions, to
     <data>\\logs\\crash_<date>_<time>.log (one file per run of the program),
  2. passes it to the session log if one is open (set_sink), and
  3. shows ONE plain message box naming the file - the first error only; a
     second box for every later one would bury the user.

The message box is the Windows MessageBoxW through ctypes, not Qt, because the
failure may be that Qt could not start. Elsewhere it is skipped and the error
goes to stderr. Writing the log can itself fail (read-only folder): that is
swallowed here, because an error reporter that raises has made things worse,
and the message box still appears.

Test hooks (environment, not documented for users): SITREP91_LOG_DIR moves the
log folder, SITREP91_NO_MESSAGEBOX=1 suppresses the box.
"""

from __future__ import annotations

import os
import platform
import sys
import threading
import traceback
from datetime import datetime
from pathlib import Path

TITLE = "SITREP91"

_state = {
    "path": None,       # this process's crash log, once the first error is in
    "shown": False,     # the message box appears once
    "started": False,   # the window is up: the app may carry on after an error
    "sink": None,       # callable(str) - the session log, when one is open
}
_lock = threading.Lock()


def log_dir() -> Path:
    """Where crash and diagnostics logs go: <data>\\logs in the released
    layout. Fixed, not the configurable runs folder, because a failure can
    come before that folder is known."""
    override = os.environ.get("SITREP91_LOG_DIR")
    if override:
        return Path(override)
    import runfiles
    return runfiles.crash_log_dir()


def _version() -> str:
    try:
        import runfiles
        return runfiles.APP_VERSION
    except Exception:
        return "unknown"


def _header(where: str) -> str:
    return (f"{TITLE} {_version()} - {where}\n"
            f"{datetime.now():%Y-%m-%d %H:%M:%S}  Python "
            f"{platform.python_version()}  {platform.platform()}\n"
            f"argv: {sys.argv}\n")


def _write(text: str) -> Path | None:
    """Append `text` to this process's crash log, creating it on first use.
    Returns the file, or None if nothing could be written."""
    with _lock:
        try:
            if _state["path"] is None:
                folder = log_dir()
                folder.mkdir(parents=True, exist_ok=True)
                _state["path"] = folder / f"crash_{datetime.now():%Y-%m-%d_%H%M%S}.log"
            with open(_state["path"], "a", encoding="utf-8") as fh:
                fh.write(text + "\n")
            return _state["path"]
        except Exception:
            return None


def _message_box(title: str, text: str) -> None:
    if os.environ.get("SITREP91_NO_MESSAGEBOX"):
        return
    if sys.platform == "win32":
        try:
            import ctypes
            # MB_OK | MB_ICONERROR | MB_SETFOREGROUND
            ctypes.windll.user32.MessageBoxW(0, text, title, 0x10 | 0x10000)
            return
        except Exception:
            pass
    try:
        print(f"{title}: {text}", file=sys.stderr)
    except Exception:
        pass


def notice(title: str, text: str) -> None:
    """A plain information box for something that is not a crash (for example
    "SITREP91 is already open"). Same test hook as the error box: with
    SITREP91_NO_MESSAGEBOX set it shows nothing."""
    if os.environ.get("SITREP91_NO_MESSAGEBOX"):
        return
    if sys.platform == "win32":
        try:
            import ctypes
            # MB_OK | MB_ICONINFORMATION | MB_SETFOREGROUND
            ctypes.windll.user32.MessageBoxW(0, text, title, 0x40 | 0x10000)
            return
        except Exception:
            pass
    try:
        print(f"{title}: {text}", file=sys.stderr)
    except Exception:
        pass


def _sink(text: str) -> None:
    sink = _state["sink"]
    if sink is None:
        return
    try:
        sink(text)
    except Exception:
        pass


def _explain(path: Path | None) -> str:
    if _state["started"]:
        head = ("SITREP91 hit an unexpected error. It may keep working; if "
                "anything looks wrong, close it and start it again. A run "
                "already on the charger is not affected.")
    else:
        head = "SITREP91 could not start."
    where = (f"Details were saved to:\n{path}" if path
             else "The details could not be saved to a file.")
    return f"{head}\n\n{where}\n\nIf you report this, attach that file."


def report(exc_type, exc, tb, where: str = "unhandled error") -> Path | None:
    """Record one unhandled exception and, the first time, tell the user."""
    detail = "".join(traceback.format_exception(exc_type, exc, tb))
    path = _write(_header(where) + detail)
    _sink(f"UNHANDLED ERROR ({where}):\n{detail}")
    first = False
    with _lock:
        if not _state["shown"]:
            _state["shown"] = first = True
    if first:
        _message_box(TITLE, _explain(path))
    return path


def record(exc_type, exc, tb, where: str) -> Path | None:
    """Put an error in the crash log and the session log WITHOUT a message
    box. For a failure the app survives and that can repeat (a bad frame, a
    capture thread that ended): a modal box would freeze the thread it was
    raised on, and a box per frame would be worse. Returns the file, or None."""
    detail = "".join(traceback.format_exception(exc_type, exc, tb))
    path = _write(_header(where) + detail)
    _sink(f"ERROR ({where}):\n{detail}")
    return path


def fail(title: str, text: str) -> Path | None:
    """A fatal problem we detected ourselves (bad arguments, a guard): log it
    and say it in a box, always - a clean exit would be just as silent."""
    path = _write(_header(title) + text)
    _sink(f"{title}: {text}")
    _message_box(TITLE, f"{title}\n\n{text}"
                 + (f"\n\nThis was also saved to:\n{path}" if path else ""))
    return path


def mark_started() -> None:
    """The window is up: from here an error need not end the program."""
    _state["started"] = True


def set_sink(fn) -> None:
    """Also pass errors to `fn(text)` (the session log); None to stop."""
    _state["sink"] = fn


def _excepthook(exc_type, exc, tb) -> None:
    if issubclass(exc_type, (KeyboardInterrupt, SystemExit)):
        sys.__excepthook__(exc_type, exc, tb)
        return
    report(exc_type, exc, tb, "main thread")


def _threadhook(args) -> None:
    if issubclass(args.exc_type, SystemExit):
        return
    name = args.thread.name if args.thread else "?"
    report(args.exc_type, args.exc_value, args.exc_traceback,
           f"thread {name}")


def _unraisablehook(args) -> None:
    report(args.exc_type, args.exc_value, args.exc_traceback,
           f"unraisable ({args.err_msg or 'no message'})")


def install() -> None:
    """Hook all three. Safe to call once, as early as possible."""
    sys.excepthook = _excepthook
    threading.excepthook = _threadhook
    sys.unraisablehook = _unraisablehook


def reset_for_tests() -> None:
    _state.update(path=None, shown=False, started=False, sink=None)

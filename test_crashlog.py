"""
Tests for crashlog.py: a crash must never be silent. Pure logic plus real
subprocesses that import gui.py and fail on purpose (the released app has no
crash-test option; the failure is injected by a throwaway driver script); no
hardware, no message box (the
SITREP91_NO_MESSAGEBOX test hook), and no writes outside a temp folder.

    python test_crashlog.py
"""

import contextlib
import io
import os
import subprocess
import sys
import tempfile
import threading
from pathlib import Path

HERE = Path(__file__).resolve().parent
# In a release build the tests sit in tests\ beside app\ (build_release.py).
GUI = HERE / "gui.py" if (HERE / "gui.py").exists() else HERE.parent / "app" / "gui.py"
TMP = Path(tempfile.mkdtemp(prefix="crashlog_test_"))
os.environ["SITREP91_LOG_DIR"] = str(TMP / "logs")
os.environ["SITREP91_NO_MESSAGEBOX"] = "1"

import crashlog  # noqa: E402

passed = failed = 0


def check(name, ok, detail=""):
    global passed, failed
    if ok:
        passed += 1
        print(f"PASS  {name}")
    else:
        failed += 1
        print(f"FAIL  {name}  {detail}")


def logs():
    d = TMP / "logs"
    return sorted(d.glob("crash_*.log")) if d.exists() else []


# --- report() ------------------------------------------------------------
boxes = []
crashlog._message_box = lambda title, text: boxes.append((title, text))
crashlog.reset_for_tests()

try:
    raise ValueError("first problem")
except ValueError as e:
    path = crashlog.report(type(e), e, e.__traceback__, "main thread")

text = path.read_text(encoding="utf-8") if path else ""
check("the first error is written to a crash log", path is not None and
      path.parent == TMP / "logs" and path.name.startswith("crash_"))
check("...with the traceback", "ValueError: first problem" in text
      and "Traceback" in text)
check("...and the app version, Python version and argv",
      "SITREP91 " in text and "Python " in text and "argv:" in text)
check("...and one box naming the file, saying it could not start",
      len(boxes) == 1 and str(path) in boxes[0][1]
      and "could not start" in boxes[0][1], str(boxes))

try:
    raise KeyError("second")
except KeyError as e:
    path2 = crashlog.report(type(e), e, e.__traceback__, "main thread")
check("a second error goes to the SAME file", path2 == path
      and "KeyError" in path.read_text(encoding="utf-8")
      and "first problem" in path.read_text(encoding="utf-8"))
check("...but never a second box", len(boxes) == 1, str(boxes))
check("one file for the whole run", len(logs()) == 1, str(logs()))

# After the window is up the wording changes: the app may carry on.
crashlog.reset_for_tests()
boxes.clear()
crashlog.mark_started()
try:
    raise RuntimeError("late")
except RuntimeError as e:
    crashlog.report(type(e), e, e.__traceback__, "main thread")
check("after startup the box says the app may keep working",
      len(boxes) == 1 and "may keep working" in boxes[0][1]
      and "could not start" not in boxes[0][1], str(boxes))

# The session log gets a copy, and a broken sink cannot hurt.
crashlog.reset_for_tests()
seen = []
crashlog.set_sink(seen.append)
try:
    raise RuntimeError("to the session log")
except RuntimeError as e:
    crashlog.report(type(e), e, e.__traceback__, "main thread")
check("the session log gets the traceback too",
      len(seen) == 1 and "to the session log" in seen[0], str(seen))
crashlog.reset_for_tests()


def broken(_):
    raise OSError("sink is gone")


crashlog.set_sink(broken)
try:
    try:
        raise RuntimeError("x")
    except RuntimeError as e:
        crashlog.report(type(e), e, e.__traceback__, "main thread")
    ok = True
except Exception:
    ok = False
check("a failing session-log sink is swallowed", ok)
crashlog.reset_for_tests()

# A log folder that cannot be written costs the file, never the box.
boxes.clear()
blocker = TMP / "blocker"
blocker.write_text("a file where the folder should be", encoding="utf-8")
os.environ["SITREP91_LOG_DIR"] = str(blocker / "logs")
try:
    raise RuntimeError("cannot save")
except RuntimeError as e:
    nopath = crashlog.report(type(e), e, e.__traceback__, "main thread")
check("an unwritable log folder: no file, but the box still appears, "
      "saying so", nopath is None and len(boxes) == 1
      and "could not be saved" in boxes[0][1], str(boxes))
os.environ["SITREP91_LOG_DIR"] = str(TMP / "logs")
crashlog.reset_for_tests()

# --- fail() --------------------------------------------------------------
boxes.clear()
p = crashlog.fail("The --cells option is not valid", "bad pair 'x'")
check("fail() logs and always shows a box with the reason",
      p is not None and "bad pair" in p.read_text(encoding="utf-8")
      and len(boxes) == 1 and "bad pair" in boxes[0][1], str(boxes))
crashlog.reset_for_tests()

# --- the hooks themselves ------------------------------------------------
saved = (sys.excepthook, threading.excepthook, sys.unraisablehook)
crashlog.install()
check("install() takes all three hooks",
      sys.excepthook is crashlog._excepthook
      and threading.excepthook is crashlog._threadhook
      and sys.unraisablehook is crashlog._unraisablehook)

boxes.clear()
t = threading.Thread(target=lambda: 1 / 0, name="worker-1")
t.start()
t.join()
tt = "".join(p.read_text(encoding="utf-8") for p in logs())
check("an exception in another thread is logged, naming the thread",
      "ZeroDivisionError" in tt and "thread worker-1" in tt)

n_before = len(boxes)
with contextlib.redirect_stderr(io.StringIO()):
    sys.excepthook(KeyboardInterrupt, KeyboardInterrupt(), None)
check("Ctrl-C is left alone (no box, no log entry)",
      len(boxes) == n_before and "KeyboardInterrupt" not in
      "".join(p.read_text(encoding="utf-8") for p in logs()))
sys.excepthook, threading.excepthook, sys.unraisablehook = saved
crashlog.reset_for_tests()

# --- real processes: gui.py failing on purpose -----------------------------
# The app has no crash-test option. Importing gui installs
# the crash hooks exactly as launching it does, so a driver script imports it,
# injects the failure and calls main().
EARLY = (
    "import sys\n"
    "sys.argv = ['gui.py', '--demo']\n"
    "import gui, crashlog\n"
    "crashlog.install()   # what running gui.py as the program does\n"
    "def boom(*a, **k):\n"
    "    raise RuntimeError('deliberate startup failure (test driver)')\n"
    "gui.startup_storage = boom\n"
    "gui.main()\n")
LATE = (
    "import sys\n"
    "sys.argv = ['gui.py', '--demo']\n"
    "import gui, crashlog\n"
    "crashlog.install()   # what running gui.py as the program does\n"
    "from PySide6.QtCore import QTimer\n"
    "_show = gui.MainWindow.show\n"
    "def show(self):\n"
    "    _show(self)\n"
    "    def late():\n"
    "        raise RuntimeError('deliberate error after startup (test driver)')\n"
    "    QTimer.singleShot(1500, late)\n"
    "gui.MainWindow.show = show\n"
    "gui.main()\n")
env = dict(os.environ)
env["SITREP91_LOG_DIR"] = str(TMP / "proc_logs")
env["QT_QPA_PLATFORM"] = "offscreen"
r = subprocess.run([sys.executable, "-c", EARLY], env=env, cwd=str(GUI.parent),
                   capture_output=True, text=True, timeout=120)
files = sorted((TMP / "proc_logs").glob("crash_*.log"))
body = files[0].read_text(encoding="utf-8") if files else ""
check("a failure at startup: exits non-zero", r.returncode != 0,
      f"rc={r.returncode}")
check("...and leaves a crash log naming the deliberate failure",
      len(files) == 1 and "deliberate startup failure" in body, str(files))

env["SITREP91_LOG_DIR"] = str(TMP / "proc_logs2")
r = subprocess.run([sys.executable, str(GUI), "--bogus-option"],
                   env=env, capture_output=True, text=True, timeout=120)
files = sorted((TMP / "proc_logs2").glob("crash_*.log"))
body = files[0].read_text(encoding="utf-8") if files else ""
check("a bad option: exits 2, and is logged rather than lost",
      r.returncode == 2 and len(files) == 1 and "--bogus-option" in body
      and "does not understand" in body, f"rc={r.returncode} {files}")

# After the window is up an error is logged and the app keeps running: the
# process must still be alive when we look, so we kill it ourselves.
env["SITREP91_LOG_DIR"] = str(TMP / "proc_logs3")
proc = subprocess.Popen([sys.executable, "-c", LATE], env=env,
                        cwd=str(GUI.parent),
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
try:
    proc.wait(timeout=12)
    alive = False
except subprocess.TimeoutExpired:
    alive = True
    proc.kill()
    proc.wait()
files = sorted((TMP / "proc_logs3").glob("crash_*.log"))
body = files[0].read_text(encoding="utf-8") if files else ""
check("an error after startup: logged, and the app was still running",
      alive and len(files) == 1 and "deliberate error after startup" in body,
      f"alive={alive} {files}")

print(f"\n{passed}/{passed + failed} passed")
sys.exit(1 if failed else 0)

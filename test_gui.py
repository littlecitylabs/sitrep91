"""
Validate gui.py and gui_theme.py - no charger, no Bluetooth, no window.

WHAT THIS COVERS
----------------
Two things the other tests cannot reach:

  1. the handoff from the capture thread to the GUI thread. test_capture.py
     proves Capture emits Samples; this proves they survive the trip across a
     Qt queued signal and land in a plot.
  2. theming, which touches Qt widgets and pyqtgraph separately and gets those
     two out of step easily.

HOW IT RUNS WITHOUT A SCREEN
----------------------------
Qt's "offscreen" platform plugin gives a real event loop, real widgets and real
signal delivery with nothing drawn. Everything below is the actual application
code - the only thing faked is the frames, which are built exactly as
test_capture.py builds them and match what FW 2.02 really sends.

IT DOES NOT TOUCH YOUR SETTINGS
-------------------------------
Every preference (theme, console, temperature unit, logs folder) and every
charger firmware record lives in settings.ini (runfiles.py). This test points
that at a throwaway file rather than saving and restoring the real one - a test
that crashes halfway must not leave a preference changed.
"""

import asyncio
import json
import os
import sys
import tempfile
import time
from pathlib import Path

# Must be set before any Qt import, or Qt tries to find a real display.
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

try:
    from PySide6.QtCore import Qt, QTimer
    from PySide6.QtWidgets import QApplication
except ModuleNotFoundError as e:
    print(f"cannot import PySide6: {e}")
    print("the GUI needs it even for offline tests: pip install PySide6 pyqtgraph")
    sys.exit(1)

try:
    import gui
    import gui_theme
except ModuleNotFoundError as e:
    print(f"cannot import the GUI modules: {e}")
    print("run this from the repository root: python test_gui.py")
    sys.exit(1)


results = []


def assert_true(label, cond, detail=""):
    print(f"{'PASS' if cond else 'FAIL'}  {label}")
    if not cond and detail:
        print(f"    {detail}")
    results.append(bool(cond))
    return cond


# Redirect settings.ini at a throwaway file BEFORE anything reads or writes
# it, so neither a real preference nor a real charger record leaks in or out.
import runfiles                                             # noqa: E402
import capture as _cap_settings                             # noqa: E402
_SETTINGS_TMP = tempfile.TemporaryDirectory()
runfiles.SETTINGS_PATH = Path(_SETTINGS_TMP.name) / "settings.ini"

app = QApplication(sys.argv)
SLOTS = [1, 2]


def slot_run_meta(cap, slot):
    """The meta.json of `slot`'s latest run - one folder per slot per run."""
    run = [r for r in cap.runs if r.slot == slot][-1]
    return json.loads(Path(run.meta_path).read_text(encoding="utf-8"))


# --------------------------------------------------------------------------
print("--- theme resolution ---")

detected = gui_theme.system_mode()
assert_true("system mode reads as light or dark", detected in ("light", "dark"),
            f"got {detected!r}")
assert_true("the system choice resolves to whatever the OS reports",
            gui_theme.resolve(gui_theme.SYSTEM) == detected)
assert_true("an explicit dark choice resolves to dark",
            gui_theme.resolve(gui_theme.DARK) == "dark")
assert_true("an explicit light choice resolves to light",
            gui_theme.resolve(gui_theme.LIGHT) == "light")
print(f"    (this machine currently reports: {detected})")


# --------------------------------------------------------------------------
print()
print("--- theme persistence ---")

gui_theme.save_choice(gui_theme.DARK)
assert_true("a saved choice survives a reload",
            gui_theme.load_choice() == gui_theme.DARK)
gui_theme.save_choice(gui_theme.SYSTEM)
assert_true("the choice can be set back to follow-the-OS",
            gui_theme.load_choice() == gui_theme.SYSTEM)

try:
    gui_theme.save_choice("chartreuse")
    assert_true("an unknown choice is rejected", False, "no ValueError raised")
except ValueError:
    assert_true("an unknown choice is rejected", True)

# show-console preference: off by default, persists, coerces cleanly
assert_true("show-console defaults off", gui_theme.load_show_console() is False)
gui_theme.save_show_console(True)
assert_true("show-console persists on", gui_theme.load_show_console() is True)
gui_theme.save_show_console(False)
assert_true("show-console persists off", gui_theme.load_show_console() is False)


# --------------------------------------------------------------------------
print()
print("--- theme applied to widgets ---")

for choice in (gui_theme.LIGHT, gui_theme.DARK):
    assert_true(f"apply({choice}) reports back {choice}",
                gui_theme.apply(app, choice) == choice)

tmp_theme = tempfile.mkdtemp()
theme_controller = gui.CaptureController(
    gui.SampleBridge(), gui.DemoSource(SLOTS), SLOTS, "theme", tmp_theme)
window = gui.MainWindow(theme_controller, SLOTS, gui_theme.DARK)

window.apply_theme()
dark_pen = window.chart.curves[1].opts["pen"].color().name()
dark_bg = window.chart.plot.backgroundBrush().color().name()

window.theme_choice = gui_theme.LIGHT
window.apply_theme()
light_pen = window.chart.curves[1].opts["pen"].color().name()
light_bg = window.chart.plot.backgroundBrush().color().name()

assert_true("the plot background changes with the theme", dark_bg != light_bg,
            f"dark {dark_bg} vs light {light_bg}")
assert_true("the slot trace color changes with the theme", dark_pen != light_pen,
            f"dark {dark_pen} vs light {light_pen}  - light-mode colors are "
            f"muddy on a near-black background, so they must differ")
assert_true("the dark plot background matches the dark palette",
            dark_bg.lower() == gui_theme.PALETTES["dark"]["plot_bg"].lower(),
            f"got {dark_bg}")
assert_true("the light plot background matches the light palette",
            light_bg.lower() == gui_theme.PALETTES["light"]["plot_bg"].lower(),
            f"got {light_bg}")
assert_true("a stylesheet is applied to the application",
            len(app.styleSheet()) > 100)

# per-state colors for the card's state dot: distinct across states, and
# theme-aware, so the cue reads differently in light and dark.
for a, b in (("running", "finished"), ("finished", "idle"), ("running", "armed")):
    assert_true(f"state color for {a} differs from {b}",
                gui_theme.state_color(a, "dark") != gui_theme.state_color(b, "dark"))
assert_true("state colors are theme-aware",
            gui_theme.state_color("running", "dark")
            != gui_theme.state_color("running", "light"))
assert_true("an unknown state falls back, never crashes",
            isinstance(gui_theme.state_color("nonsense", "dark"), str))

# group colors: distinct between groups, theme-aware, wrap on overflow
assert_true("group colors differ between groups",
            gui_theme.group_color(0, "dark") != gui_theme.group_color(1, "dark"))
assert_true("group colors are theme-aware",
            gui_theme.group_color(0, "dark") != gui_theme.group_color(0, "light"))
assert_true("group color index wraps rather than crashing",
            isinstance(gui_theme.group_color(99, "dark"), str))


# --------------------------------------------------------------------------
print()
print("--- an explicit choice overrides the OS permanently ---")

window.theme_choice = gui_theme.DARK
window.apply_theme()
before = window.mode
window.on_system_theme_changed()
assert_true("an explicit dark choice survives an OS theme change",
            window.mode == before, f"{before} -> {window.mode}")

window.theme_choice = gui_theme.SYSTEM
window.on_system_theme_changed()
assert_true("the follow-the-OS choice does track the OS",
            window.mode == gui_theme.system_mode())


# --------------------------------------------------------------------------
print()
print("--- settings dialog ---")

dialog = gui.SettingsDialog(window, gui_theme.DARK)
assert_true("the dialog opens on the current choice",
            dialog.chosen() == gui_theme.DARK)
assert_true("all three choices are offered", dialog.theme_box.count() == 3)
labels = [dialog.theme_box.itemText(i) for i in range(3)]
assert_true("the default option names Windows", "Windows" in labels[0],
            str(labels))
assert_true("the console checkbox reflects the passed-in state",
            gui.SettingsDialog(window, gui_theme.DARK, True).show_console() is True
            and gui.SettingsDialog(window, gui_theme.DARK, False).show_console()
            is False)

# the console panel: hidden by default, keeps the full status history, and the
# Settings toggle shows/hides + persists it.
gui_theme.save_show_console(False)
con_ctl = gui.CaptureController(gui.SampleBridge(), gui.DemoSource([1]), [1],
                                "console", tempfile.mkdtemp())
con_win = gui.MainWindow(con_ctl, [1], gui_theme.DARK)
assert_true("the console is hidden by default", con_win.console.isHidden())
con_win.on_status("scanning ...")
con_win.on_status("connected")
assert_true("status lines accumulate in the console even while hidden",
            "scanning ..." in con_win.console.toPlainText()
            and "connected" in con_win.console.toPlainText())
# simulate accepting Settings with the console turned on
con_win.show_console = True
gui_theme.save_show_console(True)
con_win.console.setVisible(True)
assert_true("toggling the setting shows the console and persists",
            not con_win.console.isHidden() and gui_theme.load_show_console())
gui_theme.save_show_console(False)      # leave the shared setting off


# --------------------------------------------------------------------------
print()
print("--- capture thread to GUI thread ---")

tmp_run = tempfile.mkdtemp()
bridge = gui.SampleBridge()
controller = gui.CaptureController(
    bridge, gui.DemoSource(SLOTS, interval=0.05), SLOTS, "smoke", tmp_run)
live_window = gui.MainWindow(controller, SLOTS, gui_theme.SYSTEM)

bridge.sample.connect(live_window.on_sample, Qt.ConnectionType.QueuedConnection)
bridge.status.connect(live_window.on_status, Qt.ConnectionType.QueuedConnection)
bridge.stopped.connect(live_window.on_stopped, Qt.ConnectionType.QueuedConnection)

# A consumer that throws on every sample, registered before the capture exists.
# A broken consumer must not be able to take down a long run.
broken_calls = []


def broken_consumer(_sample):
    broken_calls.append(1)
    raise RuntimeError("simulated consumer bug")


controller.subscribe(broken_consumer)

live_window.show()
# Pre-answer the stop confirmation: in Qt 6, app.quit() closes windows first
# and a vetoed close cancels the quit - so without this, the scheduled quit
# below opens a real modal dialog in a headless test and blocks forever.
live_window.confirm_stop = lambda: True
controller.start()
QTimer.singleShot(2500, app.quit)
app.exec()

# Order matters. app.quit() stops the event loop but not the capture thread, so
# frames written between quit and stop land in the CSV with their signals still
# queued and no loop left to deliver them. Stop the capture first, then drain
# what is queued, and the two sides line up exactly.
#
# The gap itself is the design working: the CSV is the product of a run and is
# durable, while the GUI is best-effort and may miss the last frames at
# shutdown. Never the other way round.
controller.stop()
app.processEvents()

ch = live_window.chart
x1, x2 = ch.xs[1], ch.xs[2]
v1 = ch.data[1]["voltage"]

assert_true("samples reached the GUI thread", len(x1) > 5,
            f"slot 1 received {len(x1)} points")
assert_true("both slots plotted", len(x2) > 5,
            f"slot 2 received {len(x2)} points")
assert_true("the plot curve holds the same data as its panel",
            len(ch.curves[1].getData()[0]) == len(x1))
assert_true("voltage falls over the demo discharge", v1[0] > v1[-1],
            f"{v1[0]} -> {v1[-1]}")
assert_true("elapsed time never goes backward",
            all(b >= a for a, b in zip(x1, x1[1:])))
assert_true("the slot card readout is populated",
            live_window.cards[1].values["Volts"].text() not in ("—", ""),
            live_window.cards[1].values["Volts"].text())
assert_true("the health bar updated",
            "samples" in live_window.health_label.text(),
            live_window.health_label.text())

# One folder per slot per run: the demo's two discharging slots
# are two runs, each with its own CSV.
_demo_runs = controller.cap.runs
csv_rows = sum(
    len(open(r.csv_path, encoding="utf-8").read().strip().splitlines()) - 1
    for r in _demo_runs)
assert_true("one run folder per slot, each named for its slot",
            sorted(r.slot for r in _demo_runs) == [1, 2]
            and all(r.dir.name.endswith(f"_S{r.slot}_external")
                    for r in _demo_runs),
            str([r.dir.name for r in _demo_runs]))
assert_true("the CSV is written even though nothing asked for it",
            csv_rows > 5, f"{csv_rows} rows")
assert_true("every CSV row reached a plot",
            csv_rows == len(x1) + len(x2),
            f"{csv_rows} rows vs {len(x1) + len(x2)} points")

assert_true("the broken consumer really was called", len(broken_calls) > 5,
            f"called {len(broken_calls)} times")
assert_true("every broken-consumer failure was counted",
            controller.cap.subscriber_errors == len(broken_calls),
            f"{controller.cap.subscriber_errors} errors vs "
            f"{len(broken_calls)} calls")
assert_true("a consumer raising on every sample did not stop the capture",
            csv_rows == len(broken_calls),
            "CSV rows should still match the samples delivered")


# --------------------------------------------------------------------------
print()
print("--- controller robustness ---")

assert_true("the capture thread stopped cleanly", controller._thread is None)
controller.stop()
assert_true("stop() is safe to call twice", controller._thread is None)

# Both the Stop button and closeEvent call stop(), so a double stop is a real
# path, not a hypothetical one.
live_window.close()
assert_true("closing the window after stopping does not raise", True)

print()
print("--- the BLE source's thread, loop and stop plumbing ---")
#
# BleSource runs capture.run_capture on its own asyncio event loop inside the
# controller's worker thread. Bluetooth cannot run in a test, so run_capture
# itself is substituted with a fake that replays frames and honors the stop
# callback - everything else here is the real code: the thread, asyncio.run,
# the stop event, the bridge, and the controller's save-meta epilogue.

seen_by_fake = {}


async def fake_run_capture(cap, opts, address=None, stop_requested=None,
                           all_stop_event=None, inject_queue=None,
                           stop_queue=None, run_until_stopped=False):
    seen_by_fake["opts"] = opts
    seen_by_fake["has_stop"] = stop_requested is not None
    seen_by_fake["has_inject_queue"] = inject_queue is not None
    seen_by_fake["has_stop_queue"] = stop_queue is not None
    seen_by_fake["run_until_stopped"] = run_until_stopped
    t = 0
    while not stop_requested():
        t += 30
        cap.on_notify(None, bytearray(gui.build_status_frame(
            1, 1500, 3700, round(1500 * t / 3600), t, 40)))
        await asyncio.sleep(0.02)
        if t > 30000:       # backstop so a broken stop cannot hang the test
            return "runaway"
    return "stopped"


real_run_capture = gui.run_capture
gui.run_capture = fake_run_capture
try:
    ble_src = gui.BleSource()
    assert_true("the default BLE options are an attach (start nothing)",
                ble_src.options.start is None,
                "a monitor must be incapable of starting a run by default")

    ble_tmp = tempfile.mkdtemp()
    ble_bridge = gui.SampleBridge()
    ble_samples, ble_stopped = [], []
    ble_bridge.sample.connect(ble_samples.append)
    ble_bridge.stopped.connect(ble_stopped.append)

    ble_controller = gui.CaptureController(
        ble_bridge, ble_src, [1], "ble", ble_tmp)
    ble_controller.start()

    for _ in range(200):    # wait for a few frames to flow, without sleeps
        if ble_controller.cap is not None and ble_controller.cap.rows >= 3:
            break
        time.sleep(0.02)

    ble_controller.stop()
    app.processEvents()

    assert_true("run_capture received the stop callback",
                seen_by_fake.get("has_stop") is True)
    assert_true("the GUI monitor persists (run_until_stopped), so stopping "
                "every slot does not end the session",
                seen_by_fake.get("run_until_stopped") is True)
    assert_true("the options object is a SessionOptions, not parsed args",
                isinstance(seen_by_fake.get("opts"), gui.SessionOptions))
    assert_true("setting the stop event ended the capture",
                ble_stopped == ["stopped"], str(ble_stopped))
    assert_true("frames flowed through the real Capture",
                ble_controller.cap.rows >= 3,
                f"{ble_controller.cap.rows} rows")
    assert_true("samples crossed the bridge", len(ble_samples) >= 3,
                f"{len(ble_samples)} delivered")
    assert_true("the BLE capture's CSV exists on disk, in its run's folder",
                ble_controller.cap.runs
                and ble_controller.cap.runs[0].csv_path.exists()
                and ble_controller.cap.runs[0].dir.parent == Path(ble_tmp))
    ble_meta = slot_run_meta(ble_controller.cap, 1)
    # An attach: the run was not started here and was still going, so only
    # the RECORDING ended - not "stopped".
    assert_true("meta.json written with the end reason",
                ble_meta.get("end_reason") == "recording_ended",
                str(ble_meta.get("end_reason")))
    assert_true("the worker thread ended", ble_controller._thread is None)
finally:
    gui.run_capture = real_run_capture

print()
print("--- the controller can start runs, and refuses bad ones early ---")
#
# Starting flows through the same session() the CLI uses; the controller's
# job is to derive intent from the source's options, validate the request
# BEFORE any thread or run directory exists, and record provenance.

from capture import (CONTROL_RESPONSE_COMMANDS,  # noqa: E402
                     INIT_RESPONSE_COMMANDS)

bad_bridge = gui.SampleBridge()
bad_status, bad_stopped = [], []
bad_bridge.status.connect(bad_status.append)
bad_bridge.stopped.connect(bad_stopped.append)
bad_src = gui.BleSource(gui.SessionOptions(start="discharge", current=0,
                                           capacity=2000, safety_timer=60))
bad_controller = gui.CaptureController(
    bad_bridge, bad_src, [1], "bad", tempfile.mkdtemp())

assert_true("a start source is recognized as starting runs",
            bad_controller.starts_runs)
ok = bad_controller.start()
app.processEvents()
assert_true("an invalid request is refused", ok is False)
assert_true("refused before any thread existed",
            bad_controller._thread is None and bad_controller.cap is None,
            "a request that can never run must not leave debris")
assert_true("the refusal reaches the status line",
            any("REFUSING" in m for m in bad_status), str(bad_status))
assert_true("and ends with a 'refused' stop reason",
            bad_stopped == ["refused"], str(bad_stopped))

# The refusal REASON must survive to the ending, not just the transient line.
bad_win = gui.MainWindow(bad_controller, [1], gui_theme.DARK)
bad_win.on_status("REFUSING TO START: slot(s) [1] already running")
bad_win.on_stopped("refused")
assert_true("the ending shows the refusal reason, not just 'refused'",
            "already running" in bad_win.status_label.text()
            and "REFUSED" in bad_win.status_label.text(),
            bad_win.status_label.text())
assert_true("a refused ending is colored, not plain",
            gui_theme.PALETTES["dark"]["error"] in bad_win.status_label.styleSheet(),
            bad_win.status_label.styleSheet())
bad_win.on_status("scanning ...")
assert_true("a normal status line clears the error color",
            bad_win.status_label.styleSheet() == "")

# The session-length cap (a cap would let an idle window end its own capture
# after a few hours and gray every card). The GUI has NO cap unless asked, the
# cap never stops a run, and a capture ended by it says so in color.
import inspect as _inspect                                       # noqa: E402
assert_true("the GUI's session cap defaults to none",
            gui.DEFAULT_MAX_HOURS is None, str(gui.DEFAULT_MAX_HOURS))
assert_true("...and the --max-hours option uses that default",
            "default=DEFAULT_MAX_HOURS" in _inspect.getsource(gui.main))
assert_true("...so the options handed to the capture carry no cap",
            gui.SessionOptions(max_hours=gui.DEFAULT_MAX_HOURS).max_hours
            is None)
cap_win = gui.MainWindow(bad_controller, [1], gui_theme.DARK)
cap_win.on_stopped("duration")
assert_true("a capture ended by the cap says the run was left going",
            "not stopped" in cap_win.status_label.text()
            and "left going" in cap_win.status_label.text(),
            cap_win.status_label.text())
assert_true("...and it is colored as a warning, not shown as a clean ending",
            gui_theme.PALETTES["dark"]["warn"] in cap_win.status_label.styleSheet(),
            cap_win.status_label.styleSheet())

# No usable Bluetooth is a normal answer on a PC without an adapter, not a bug.
# Windows' scanner raises bleak's BleakBluetoothNotAvailableError for both "no
# adapter" and "radio switched off" (winrt/scanner.py start()); the window used
# to show that error's repr and point at a terminal the packaged app lacks.
import contextlib as _ctx_na                                  # noqa: E402
import io as _io_na                                           # noqa: E402
import threading as _thr_na                                   # noqa: E402
from bleak.exc import (BleakBluetoothNotAvailableError as _BNA,  # noqa: E402
                       BleakBluetoothNotAvailableReason as _BNR)

_na_prev_run_capture = gui.run_capture
_NA_CASES = (
    ("no adapter", ("No Bluetooth adapter found", _BNR.NO_BLUETOOTH)),
    ("radio off", ("Bluetooth radio is not powered on. Turn on Bluetooth and "
                   "try again.", _BNR.POWERED_OFF)),
)
for _na_label, _na_args in _NA_CASES:
    async def _raise_na(*a, _args=_na_args, **k):
        raise _BNA(*_args)
    gui.run_capture = _raise_na
    try:
        na_src = gui.BleSource()
        na_status = []
        na_reason = na_src.run(None, _thr_na.Event(), na_status.append)
    finally:
        gui.run_capture = _na_prev_run_capture
    na_line = na_status[-1] if na_status else ""
    assert_true(f"{_na_label}: the run ends as an error, not a crash",
                na_reason == "error", str(na_reason))
    assert_true(f"{_na_label}: the status line says plainly what is wrong",
                "No Bluetooth adapter was found" in na_line
                and "Bluetooth is switched off" in na_line, na_line)
    assert_true(f"{_na_label}: ...and what to do about it",
                "start SITREP91 again" in na_line, na_line)
    assert_true(f"{_na_label}: no Python error text, no 'capture failed', "
                "no terminal",
                not any(w in na_line for w in
                        ("BleakBluetooth", "NO_BLUETOOTH", "Reason.",
                         "capture failed", "traceback", "terminal")), na_line)
    na_win = gui.MainWindow(
        gui.CaptureController(gui.SampleBridge(), na_src, [1], "na",
                              tempfile.mkdtemp()), [1], gui_theme.DARK)
    na_win.on_stopped("error")
    assert_true(f"{_na_label}: the ending keeps the plain sentence",
                na_win.status_label.text() == na_line
                and "capture failed" not in na_win.status_label.text(),
                na_win.status_label.text())
    assert_true(f"{_na_label}: ...colored as a problem, controls disabled",
                gui_theme.PALETTES["dark"]["error"]
                in na_win.status_label.styleSheet()
                and not na_win.stop_button.isEnabled(),
                na_win.status_label.styleSheet())

# An ordinary error is untouched, and only the exception TYPE counts.
async def _raise_boom(*a, **k):
    raise ValueError("boom")
gui.run_capture = _raise_boom
os.environ["SITREP91_LOG_DIR"] = tempfile.mkdtemp()   # its crash log goes here
try:
    boom_src = gui.BleSource()
    boom_status = []
    with _ctx_na.redirect_stderr(_io_na.StringIO()):
        boom_reason = boom_src.run(None, _thr_na.Event(), boom_status.append)
finally:
    gui.run_capture = _na_prev_run_capture
    os.environ.pop("SITREP91_LOG_DIR", None)
assert_true("any other error still reads 'capture failed: ...', now saying "
            "where the details went",
            boom_reason == "error" and boom_src.error_is_plain is False
            and boom_status[-1].startswith("capture failed: ValueError: boom")
            and "details:" in boom_status[-1] and boom_src.crash_log is not None,
            str(boom_status))
assert_true("only bleak's own type counts: words in other errors do not",
            gui.bluetooth_unavailable(RuntimeError("adapter not available"))
            is None and gui.bluetooth_unavailable(ValueError("boom")) is None)

good_src = gui.BleSource(gui.SessionOptions(start="discharge", current=500,
                                            capacity=2000, safety_timer=60,
                                            cutoff_mv=3000))
gui.run_capture = fake_run_capture
try:
    run_tmp = tempfile.mkdtemp()
    run_bridge = gui.SampleBridge()
    run_stopped = []
    run_bridge.stopped.connect(run_stopped.append)
    run_controller = gui.CaptureController(
        run_bridge, good_src, [1], "started", run_tmp,
        cells={1: "TEST-01"})
    assert_true("a valid start request is accepted",
                run_controller.start() is True)
    for _ in range(200):
        if run_controller.cap is not None and run_controller.cap.rows >= 2:
            break
        time.sleep(0.02)
    run_controller.stop()
    app.processEvents()

    # A starting capture triggers 0x94/0x57 but never init - the start path
    # stopped sending it - so only the init trio's absence is excused.
    assert_true("a starting capture excuses only the init responses",
                run_controller.cap.validator.expected_absent
                == INIT_RESPONSE_COMMANDS,
                str(run_controller.cap.validator.expected_absent))
    run_meta = slot_run_meta(run_controller.cap, 1)
    assert_true("meta.json records the run as a discharge, not an attach",
                run_meta["mode"] == "discharge", str(run_meta["mode"]))
    assert_true("meta.json records what was asked of the charger",
                (run_meta["current_ma"], run_meta["capacity_mah"],
                 run_meta["safety_timer_min"], run_meta["cutoff_mv"])
                == (500, 2000, 60, 3000),
                str({k: run_meta[k] for k in
                     ("current_ma", "capacity_mah", "safety_timer_min",
                      "cutoff_mv")}))
    assert_true("stopping a started run works the same way",
                run_stopped == ["stopped"] and
                run_controller._thread is None, str(run_stopped))
finally:
    gui.run_capture = real_run_capture

attach_controller = gui.CaptureController(
    gui.SampleBridge(), gui.BleSource(), [1], "attach", tempfile.mkdtemp())
assert_true("a default source still attaches",
            not attach_controller.starts_runs)
assert_true("the demo source attaches too",
            not gui.CaptureController(
                gui.SampleBridge(), gui.DemoSource([1]), [1], "d",
                tempfile.mkdtemp()).starts_runs)

print()
print("--- the stop dialog tells the truth about who owns the run ---")

start_win = gui.MainWindow(run_controller, [1], gui_theme.SYSTEM)
attach_win = gui.MainWindow(attach_controller, [1], gui_theme.SYSTEM)
from types import SimpleNamespace as _SNq                       # noqa: E402


def _fake_cap(started, **latest):
    """Just enough of a Capture for the dialog: which slots this app started
    and what the charger last said for each."""
    return _SNq(started_slots=set(started),
                latest_any={int(k[1:]): v for k, v in latest.items()},
                latest={int(k[1:]): v for k, v in latest.items()},
                finished=set(), torn_down=set(), measuring_ir=set(),
                start_failed=set(), gaps=[], user_stopped=set(),
                final_by_slot={}, link_ok=True)


_busy = _SNq(current_ma=500, device_status=3)
_idle = _SNq(current_ma=0, device_status=0)
# The case that matters: normal use (Configure, then Start on a
# card) never sets starts_runs, yet closing stops the run. The old wording said
# "the charger itself keeps running", then the exit sent the stop.
attach_controller.cap = _fake_cap({1}, s1=_busy)
_q = attach_win._stop_question()
assert_true("a run started from a card is a run THIS app stops: the dialog "
            "says so, naming the slot",
            "stops the run" in _q and "slot 1" in _q
            and "keeps running" not in _q, _q)
attach_controller.cap = _fake_cap({1, 3}, s1=_busy, s2=_busy, s3=_busy)
_q = attach_win._stop_question()
assert_true("...every slot it started, and says a panel run is left alone",
            "slot 1, slot 3" in _q and "slot 2" not in _q
            and "left running" in _q, _q)
attach_controller.cap = _fake_cap(set(), s1=_busy, s2=_busy)
_q = attach_win._stop_question()
assert_true("watching only a panel run: ending the session stops nothing, "
            "and the dialog says the charger carries on",
            "keeps running" in _q and "stops the run" not in _q, _q)
attach_controller.cap = _fake_cap({1}, s1=_idle)
assert_true("a slot the app started that has finished has nothing to stop",
            "keeps running" in attach_win._stop_question())
run_controller.cap = _fake_cap(set())
assert_true("--start with nothing left running claims no stop either",
            "stops the run" not in start_win._stop_question(),
            start_win._stop_question())
run_controller.cap = _fake_cap({1}, s1=_busy)
assert_true("--start with its run going: the charger stops too",
            "stops the run" in start_win._stop_question(),
            start_win._stop_question())
attach_controller.cap = None

print()
print("--- slot card state rendering and the configure flow ---")
#
# The card renders each slot state with the right action button and shows the
# pending config when armed; its buttons emit signals the window wires to the
# controller. Then the full Configure -> arm -> card-renders-armed flow, with
# the modal dialog substituted so it runs headless.

from capture import RunRequest as _RR3
from protocol import Chemistry as _C3, Mode as _M3
from PySide6.QtWidgets import QDialog as _QD  # noqa

card = gui.SlotCard(2, "TEST-04", gui_theme.DARK)
# A fresh card must render "unknown" immediately - both buttons disabled - not
# sit at QPushButton's enabled default until the first refresh (a live-looking
# Start that does nothing during connect/init).
assert_true("a fresh card starts with both buttons disabled",
            not card.configure_button.isEnabled()
            and not card.action_button.isEnabled())

emitted = []
card.configure_requested.connect(lambda s: emitted.append(("cfg", s)))
card.start_requested.connect(lambda s: emitted.append(("start", s)))
card.stop_requested.connect(lambda s: emitted.append(("stop", s)))

# Two independent buttons: Configure (setup) and Start/Stop (run).
req3 = _RR3(mode=_M3.DISCHARGE, chemistry=_C3.LIION, current_ma=500,
            capacity_mah=2000, safety_timer_min=60, cutoff_mv=3000)

card.render_state("idle", None)
assert_true("idle: Configure enabled, Start disabled",
            card.configure_button.isEnabled()
            and card.action_button.text() == "Start"
            and not card.action_button.isEnabled())
assert_true("an unconfigured slot hides the config line",
            card.config_label.isHidden())

card.render_state("armed", req3)
assert_true("armed: Configure still available AND Start enabled",
            card.configure_button.isEnabled()
            and card.action_button.text() == "Start"
            and card.action_button.isEnabled())
assert_true("armed shows chemistry, action AND rate before any start",
            not card.config_label.isHidden()
            and "Li-ion" in card.config_label.text()
            and "Discharge" in card.config_label.text()
            and "500 mA" in card.config_label.text(), card.config_label.text())

card.render_state("starting", req3)
assert_true("starting: Start becomes a disabled 'Starting…', no re-click",
            card.action_button.text() == "Starting…"
            and not card.action_button.isEnabled())
assert_true("starting: Configure is locked out mid-commit",
            not card.configure_button.isEnabled())
assert_true("starting still shows the full committed config",
            not card.config_label.isHidden()
            and "500 mA" in card.config_label.text())

card.render_state("running", req3)
assert_true("running: Stop enabled, Configure locked out",
            card.action_button.text() == "Stop" and card.action_button.isEnabled()
            and not card.configure_button.isEnabled())
assert_true("running keeps chemistry + action, drops the rate detail",
            not card.config_label.isHidden()
            and "Li-ion" in card.config_label.text()
            and "Discharge" in card.config_label.text()
            and "mA" not in card.config_label.text(), card.config_label.text())

card.render_state("finished", req3)
assert_true("finished: Configure available again, Start disabled (no pending)",
            card.configure_button.isEnabled()
            and card.action_button.text() == "Start"
            and not card.action_button.isEnabled())
assert_true("finished still reads what the slot was set up for",
            not card.config_label.isHidden()
            and "Li-ion" in card.config_label.text()
            and "Discharge" in card.config_label.text())

card.render_state("empty", None)
assert_true("empty: both buttons disabled",
            not card.configure_button.isEnabled()
            and not card.action_button.isEnabled())

# each button emits its own signal, independent of the other
card.render_state("finished", req3); card._on_configure()
card.render_state("armed", req3); card._on_action()
card.render_state("running", req3); card._on_action()
assert_true("Configure emits configure; the action button emits start/stop",
            emitted == [("cfg", 2), ("start", 2), ("stop", 2)], str(emitted))

# the state dot: a non-text, at-a-glance state cue, colored by state and theme,
# separate from the identity swatch (which is the slot's chart color).
card.render_state("running", req3)
run_dot = card.state_dot.styleSheet()
card.render_state("finished", req3)
fin_dot = card.state_dot.styleSheet()
assert_true("the state dot recolours with the state", run_dot != fin_dot)
assert_true("the running dot uses the running state color",
            gui_theme.state_color("running", gui_theme.DARK) in run_dot, run_dot)
assert_true("the state dot is not the slot identity color",
            gui_theme.slot_color(2, gui_theme.DARK) not in run_dot)

# the group tag: hidden when ungrouped, a colored letter when grouped, and a
# different group reads differently (color AND letter).
card.set_group(None)
assert_true("an ungrouped card hides the group tag", card.group_tag.isHidden())
card.set_group(1)
g1_style, g1_text = card.group_tag.styleSheet(), card.group_tag.text()
assert_true("a grouped card shows a labeled tag ('Group A')",
            not card.group_tag.isHidden() and "Group A" in g1_text, g1_text)
card.set_group(2)
assert_true("a different group differs in color and letter",
            card.group_tag.styleSheet() != g1_style
            and card.group_tag.text() != g1_text, card.group_tag.text())

# the leave-group control: shows with the tag, hides without, emits for its slot
left = []
card.leave_group_requested.connect(lambda s: left.append(s))
card.set_group(1)
assert_true("the leave-group control shows when grouped",
            not card.leave_button.isHidden())
card.leave_button.click()
assert_true("clicking leave emits leave_group_requested for this slot",
            left == [2], str(left))
card.set_group(None)
assert_true("the leave-group control hides when ungrouped",
            card.leave_button.isHidden())

# full Configure flow with a substituted dialog
class _FakeDialog:
    def __init__(self, *a, **k): pass
    def exec(self): return _QD.DialogCode.Accepted
    def build_request(self): return req3
    def target_slots(self): return [2, 3]     # copy to slot 3
    def cell_id(self): return "TEST-04"

flow_tmp = tempfile.mkdtemp()
flow_ctl = gui.CaptureController(gui.SampleBridge(), gui.DemoSource([1, 2, 3]),
                                 [1, 2, 3], "flow", flow_tmp, cells={})
flow_win = gui.MainWindow(flow_ctl, [1, 2, 3], gui_theme.DARK)
_real_dialog = gui.ConfigDialog
gui.ConfigDialog = _FakeDialog
try:
    flow_win.on_configure_slot(2)
finally:
    gui.ConfigDialog = _real_dialog

assert_true("configuring arms the primary slot",
            flow_ctl.slot_state(2) == "armed")
assert_true("copy-to-slot arms the copied slot too",
            flow_ctl.slot_state(3) == "armed")
assert_true("the primary slot took the entered cell id",
            flow_ctl.cells.get(2) == "TEST-04")
assert_true("the copied slot did NOT take the cell id (per-slot identity)",
            flow_ctl.cells.get(3) is None)
assert_true("the two configured-together slots form a group",
            flow_ctl.group_of(2) == {2, 3})
assert_true("configured-together cards wear the same group tag",
            flow_win.cards[2].group_id == flow_win.cards[3].group_id
            and flow_win.cards[2].group_id is not None)
assert_true("an ungrouped card wears none",
            flow_win.cards[1].group_id is None)
assert_true("the armed card now shows Start",
            flow_win.cards[2].action_button.text() == "Start")

print()
print("--- config dialog ---")
#
# The dialog produces a validated RunRequest, never a packet. Chemistry-
# dependent actions with unverified modes disabled; copy-to-slot; both accept
# paths (arm vs arm-and-start).

from protocol import Chemistry as _C2, Mode as _M2
from capture import VERIFIED_MODES as _VM


def _type_in(edit, text):
    """What typing does: set the text AND emit textEdited (setText alone does
    not, which is exactly how the dialog tells its own changes from yours)."""
    edit.setText(text)
    edit.textEdited.emit(text)


cd = gui.ConfigDialog(None, 2, [1, 2, 3], cell_id="TEST-04",
                      chemistry=_C2.LIION)
labels = {cd.action_combo.itemData(i):
          cd.action_combo.model().item(i).isEnabled()
          for i in range(cd.action_combo.count())}
assert_true("li-ion offers charge/discharge/storage enabled",
            labels[_M2.CHARGE] and labels[_M2.DISCHARGE] and labels[_M2.STORAGE])
assert_true("cycle is present but disabled (not yet verified)",
            _M2.CYCLE in labels and not labels[_M2.CYCLE])
assert_true("the default action is discharge (house standard)",
            cd.action_combo.currentData() == _M2.DISCHARGE,
            cd.action_combo.currentText())

assert_true("a fresh dialog has no capacity: Save is OFF and no request can "
            "be built (the old 5000 mAh / 3 A default was a trap for any "
            "other cell)",
            not cd.save_button.isEnabled() and cd.build_request() is None
            and cd.capacity_edit.text() == "")
_type_in(cd.capacity_edit, "3400")
req = cd.build_request()
assert_true("once the capacity is in, Save is on and the dialog builds a "
            "valid RunRequest",
            cd.save_button.isEnabled() and req is not None
            and req.problem() is None)
assert_true("Li-ion discharge is derived from the capacity at 0.2C: 3400 mAh "
            "-> 680 mA (the old default was 2000 mA whatever the cell)",
            req.current_ma == 680 and req.capacity_mah == 3400
            and req.cutoff_mv == 3200,
            f"{req.current_ma}/{req.capacity_mah}/{req.cutoff_mv}")
assert_true("the cell id is read back", cd.cell_id() == "TEST-04")

# copy-to-slot
assert_true("target is just this slot by default", cd.target_slots() == [2])
cd.copy_boxes[1].setChecked(True)
assert_true("checking a copy box adds that slot",
            set(cd.target_slots()) == {1, 2})

# chemistry switch repopulates actions
cd.chem_combo.setCurrentIndex(cd.chem_combo.findData(_C2.NIMH))
nimh = {cd.action_combo.itemData(i):
        cd.action_combo.model().item(i).isEnabled()
        for i in range(cd.action_combo.count())}
assert_true("nickel swaps in refresh/break-in (disabled), drops storage",
            _M2.REFRESH in nimh and not nimh[_M2.REFRESH]
            and _M2.STORAGE not in nimh)

# Chemistries never run on the author's charger are listed but MONITOR-ONLY.
from capture import VERIFIED_CONTROL as _VC
_chem_items = {cd.chem_combo.itemData(i): cd.chem_combo.model().item(i)
               for i in range(cd.chem_combo.count())}
assert_true("verified chemistries are selectable and plainly labeled",
            all(_chem_items[c].isEnabled()
                and "monitor" not in _chem_items[c].text() for c in _VC))
assert_true("every other chemistry is listed, disabled and labeled monitor only",
            all((not it.isEnabled()) and "monitor only" in it.text()
                for c, it in _chem_items.items() if c not in _VC))
assert_true("nickel charge and discharge are startable, storage is not offered",
            nimh[_M2.CHARGE] and nimh[_M2.DISCHARGE] and _M2.STORAGE not in nimh)
# The cell-name field is an editable dropdown of past names.
_cn = gui.ConfigDialog(None, 1, [1], known_cells=["TEST-02", "TEST-01", "ENL-A-01"])
assert_true("the cell field is an editable dropdown of the known names, in order",
            _cn.cell_edit.isEditable()
            and [_cn.cell_edit.itemText(i) for i in range(_cn.cell_edit.count())]
            == ["TEST-02", "TEST-01", "ENL-A-01"])
assert_true("nothing is chosen until the user picks or types: blank is None",
            _cn.cell_id() is None, str(_cn.cell_id()))
_cn.cell_edit.setCurrentIndex(1)
assert_true("picking a past name sets the cell id", _cn.cell_id() == "TEST-01",
            str(_cn.cell_id()))
_cn.cell_edit.setEditText("  BRAND-NEW-7 ")
assert_true("typing a new name works as before (trimmed)",
            _cn.cell_id() == "BRAND-NEW-7", str(_cn.cell_id()))
assert_true("...and does NOT add itself to the list (the runs are the source)",
            _cn.cell_edit.count() == 3 and _cn.cell_edit.findText("BRAND-NEW-7") < 0)
_cn.cell_edit.setEditText("   ")
assert_true("a blank name is still allowed", _cn.cell_id() is None)
_cn2 = gui.ConfigDialog(None, 1, [1], cell_id="TEST-09", known_cells=["TEST-02"])
assert_true("a slot's current cell shows even when it is not in the list",
            _cn2.cell_id() == "TEST-09" and _cn2.cell_edit.count() == 1,
            str(_cn2.cell_id()))
_cn3 = gui.ConfigDialog(None, 1, [1])
assert_true("with no history the field is just an empty box that takes typing",
            _cn3.cell_edit.count() == 0 and _cn3.cell_id() is None)
_cn3.cell_edit.setEditText("X-1")
assert_true("...and typing still gives the cell id", _cn3.cell_id() == "X-1")
# Enter in the Cell id field (typed out of habit, or sent by a barcode scanner)
# must not press Save - that armed the slot before the chemistry and values had
# been looked at. It accepts the text and moves
# on to the next field.
from PySide6.QtCore import Qt as _Qt                    # noqa: E402
from PySide6.QtTest import QTest as _QTest              # noqa: E402
from PySide6.QtWidgets import QDialog as _QDialog       # noqa: E402
_enter_app = gui.QApplication.instance()
from PySide6.QtCore import QEvent as _QEvent           # noqa: E402
from PySide6.QtGui import QKeyEvent as _QKeyEvent      # noqa: E402


def _press_like_a_keyboard(widget, key):
    """Deliver a key press the way the operating system does. A real keystroke
    arrives marked NOT accepted, and Qt hands such an event on to the widget's
    parents (ending at the dialog's default button) unless a handler accepts
    it. QTest.keyClick sends events that are already accepted, so it hides
    exactly that bug - a fix can pass it and still close the dialog on a real
    keyboard."""
    ev = _QKeyEvent(_QEvent.Type.KeyPress, key, _Qt.KeyboardModifier.NoModifier)
    ev.ignore()
    gui.QApplication.sendEvent(widget, ev)
    gui.QApplication.sendEvent(widget, _QKeyEvent(
        _QEvent.Type.KeyRelease, key, _Qt.KeyboardModifier.NoModifier))


# A real keystroke is delivered to the FOCUS widget, which for an editable
# combo box is the combo itself, not its inner line edit (a fix that watched
# only the line edit still closed the dialog on a real keyboard; OS-level
# keystrokes showed the receiver). So each case is run against both receivers.
for _label, _key, _target in (
        (f"{n} to {t}", k, t)
        for n, k in (("Return", _Qt.Key_Return), ("keypad Enter", _Qt.Key_Enter))
        for t in ("combo", "line edit")):
    for _typed in ("NAI-A-03", "", "TEST-01"):
        _de = gui.ConfigDialog(None, 1, [1, 2], known_cells=["TEST-01"])
        _type_in(_de.capacity_edit, "3000")   # Save must be ON for this to count
        _de.show()
        _enter_app.processEvents()
        _le = _de.cell_edit.lineEdit()
        _le.setFocus()
        _QTest.keyClicks(_le, _typed)
        _press_like_a_keyboard(_de.cell_edit if _target == "combo" else _le, _key)
        _enter_app.processEvents()
        assert_true(f"{_label} in Cell id ({_typed or 'empty'!r}) does not save",
                    _de.result() != _QDialog.DialogCode.Accepted
                    and _de.isVisible(), str(_de.result()))
        assert_true("...keeps the text and leaves the field",
                    _de.cell_edit.currentText() == _typed
                    and not _de.cell_edit.hasFocus()
                    and _enter_app.focusWidget() is not _le,
                    repr(_de.cell_edit.currentText()))
        _de.close()
# Enter elsewhere is untouched: in the Current field it still presses Save.
_de = gui.ConfigDialog(None, 1, [1, 2])
_de.show()
_enter_app.processEvents()
_de.current_edit.setFocus()
_press_like_a_keyboard(_de.current_edit, _Qt.Key_Return)
_enter_app.processEvents()
assert_true("with no capacity yet, Enter in another field saves NOTHING "
            "(Save is off until the capacity is entered)",
            _de.result() != _QDialog.DialogCode.Accepted
            and not _de.save_button.isEnabled(), str(_de.result()))
_type_in(_de.capacity_edit, "3000")
_de.current_edit.setFocus()
_press_like_a_keyboard(_de.current_edit, _Qt.Key_Return)
_enter_app.processEvents()
assert_true("Enter in another field still presses Save once it is on "
            "(unchanged)",
            _de.result() == _QDialog.DialogCode.Accepted, str(_de.result()))
_de.close()
# A scanner set to end each scan with CR+LF sends Enter TWICE in a few ms. The
# first moves on from Cell id; the second must not reach Save (with a real
# scanner, CR+LF saved and armed the slot).
for _k1, _k2, _what in ((_Qt.Key_Return, _Qt.Key_Return, "CR then CR"),
                        (_Qt.Key_Return, _Qt.Key_Enter, "CR then keypad Enter")):
    for _target in ("combo", "line edit"):
        _de = gui.ConfigDialog(None, 1, [1, 2], known_cells=["TEST-01"])
        _type_in(_de.capacity_edit, "3000")   # Save must be ON for this to count
        _de.show()
        _enter_app.processEvents()
        _le = _de.cell_edit.lineEdit()
        _le.setFocus()
        _QTest.keyClicks(_le, "NAI-A-03")
        _press_like_a_keyboard(_de.cell_edit if _target == "combo" else _le, _k1)
        _press_like_a_keyboard(_enter_app.focusWidget(), _k2)      # arrives at once
        _enter_app.processEvents()
        assert_true(f"scanner suffix {_what} (to {_target}): the dialog stays open",
                    _de.result() != _QDialog.DialogCode.Accepted
                    and _de.isVisible(), str(_de.result()))
        assert_true("...and the name is kept",
                    _de.cell_edit.currentText() == "NAI-A-03",
                    repr(_de.cell_edit.currentText()))
        # Later, a deliberate Enter on the next field presses Save as usual.
        _de._cell_enter_at -= 1.0
        _press_like_a_keyboard(_enter_app.focusWidget(), _k2)
        _enter_app.processEvents()
        assert_true("...and an Enter a moment later still saves",
                    _de.result() == _QDialog.DialogCode.Accepted, str(_de.result()))
        _de.close()
import inspect as _insp   # noqa: E402
assert_true("Configure is handed the past names, read from the runs folder",
            "known_cells=runfiles.past_cell_names(" in _insp.getsource(
                gui.MainWindow.on_configure_slot))

# The panel's own menus, read off the charger. NiZn and RAM have
# Storage and NO Discharge/Refresh/Break-In; Refresh and Break-In are NiMH, NiCd
# and Eneloop only; nickel has no Storage.
_M, _Ch = _M2, _C2
_PANEL = {
    _Ch.LIION:    {_M.CHARGE, _M.STORAGE, _M.DISCHARGE, _M.CYCLE},
    _Ch.LIION_HV: {_M.CHARGE, _M.STORAGE, _M.DISCHARGE, _M.CYCLE},
    _Ch.LIFEPO4:  {_M.CHARGE, _M.STORAGE, _M.DISCHARGE, _M.CYCLE},
    _Ch.NIMH:     {_M.CHARGE, _M.DISCHARGE, _M.CYCLE, _M.REFRESH, _M.BREAK_IN},
    _Ch.NICD:     {_M.CHARGE, _M.DISCHARGE, _M.CYCLE, _M.REFRESH, _M.BREAK_IN},
    _Ch.ENELOOP:  {_M.CHARGE, _M.DISCHARGE, _M.CYCLE, _M.REFRESH, _M.BREAK_IN},
    _Ch.NIZN:     {_M.CHARGE, _M.STORAGE, _M.CYCLE},
    _Ch.RAM:      {_M.CHARGE, _M.STORAGE, _M.CYCLE},
    _Ch.LTO:      {_M.CHARGE, _M.STORAGE, _M.DISCHARGE, _M.CYCLE},
    _Ch.NAION:    {_M.CHARGE, _M.STORAGE, _M.DISCHARGE, _M.CYCLE},
}
assert_true("the panel table covers all ten chemistries",
            set(_PANEL) == set(_Ch), str(set(_Ch) - set(_PANEL)))
_bad = [c.name for c in _Ch
        if set(gui.actions_for_chemistry(c)) != _PANEL[c]
        or len(gui.actions_for_chemistry(c)) != len(_PANEL[c])]
assert_true("every chemistry's action list is exactly what the panel offers",
            not _bad, str(_bad))
cd.chem_combo.setCurrentIndex(cd.chem_combo.findData(_C2.NIZN))
_nizn = {cd.action_combo.itemData(i) for i in range(cd.action_combo.count())}
assert_true("the dialog shows NiZn with Charge, Storage and Cycle only",
            _nizn == {_M.CHARGE, _M.STORAGE, _M.CYCLE}, str(_nizn))
cd.chem_combo.setCurrentIndex(cd.chem_combo.findData(_C2.RAM))
_ram = {cd.action_combo.itemData(i) for i in range(cd.action_combo.count())}
assert_true("...and RAM the same: no Discharge to offer",
            _ram == {_M.CHARGE, _M.STORAGE, _M.CYCLE}
            and not any(cd.action_combo.model().item(i).isEnabled()
                        for i in range(cd.action_combo.count())), str(_ram))
cd.chem_combo.setCurrentIndex(cd.chem_combo.findData(_C2.LIFEPO4))
assert_true("a monitor-only chemistry offers no startable action",
            not any(cd.action_combo.model().item(i).isEnabled()
                    for i in range(cd.action_combo.count())))
assert_true("...shows the plain-words note",
            (not cd.monitor_note.isHidden())
            and "monitor-only" in cd.monitor_note.text(),
            cd.monitor_note.text())
assert_true("...and points at ROADMAP.md, in the app's own words",
            gui.ROADMAP_NOTE == ("See ROADMAP.md in the SITREP91 folder for "
                                 "what's verified and what's next.")
            and cd.monitor_note.text().endswith(gui.ROADMAP_NOTE),
            cd.monitor_note.text())
assert_true("...after the plain-words refusal, not instead of it",
            cd.monitor_note.text().startswith(
                _cap_settings.monitor_only_message(_C2.LIFEPO4)))

# ROADMAP.md's n/a cells are the panel's menus. The file is looked for at the
# repo root and in packaging/public/; a release build's tests have neither.
_rm_dir = Path(__file__).resolve().parent
_rm = next((p for p in (_rm_dir / "ROADMAP.md",
                        _rm_dir / "packaging" / "public" / "ROADMAP.md")
            if p.exists()), None)
if _rm is None:
    print("SKIP  ROADMAP.md not beside the code (a release build)")
else:
    _rm_names = {"Li-ion": _C2.LIION, "Li-ion HV": _C2.LIION_HV,
                 "LiFePO4": _C2.LIFEPO4, "LTO": _C2.LTO, "Na-ion": _C2.NAION,
                 "NiMH": _C2.NIMH, "Eneloop": _C2.ENELOOP, "NiCd": _C2.NICD,
                 "NiZn": _C2.NIZN, "RAM": _C2.RAM}
    _rm_modes = [_M.CHARGE, _M.STORAGE, _M.DISCHARGE, _M.CYCLE, _M.REFRESH,
                 _M.BREAK_IN]
    _rm_bad = []
    for _ln in _rm.read_text(encoding="utf-8").splitlines():
        _cells = [c.strip() for c in _ln.strip().strip("|").split("|")]
        if _ln.lstrip().startswith("|") and _cells[0] in _rm_names:
            _offered = set(gui.actions_for_chemistry(_rm_names[_cells[0]]))
            for _i, _m in enumerate(_rm_modes):
                if (_cells[1 + _i] == "n/a") == (_m in _offered):
                    _rm_bad.append(f"{_cells[0]} {_m.name}: {_cells[1 + _i]!r}")
    assert_true("ROADMAP.md says n/a exactly where the panel offers no such mode",
                not _rm_bad, "; ".join(_rm_bad))
assert_true("...and Save is switched off", not cd.save_button.isEnabled())
assert_true("...and no request can be built from it",
            cd.build_request() is None
            and "monitor-only" in cd.error_label.text(), cd.error_label.text())
cd.chem_combo.setCurrentIndex(cd.chem_combo.findData(_C2.NIMH))
assert_true("back on a verified chemistry the note goes and Save returns",
            cd.monitor_note.isHidden() and cd.save_button.isEnabled())

# bad input is caught, not turned into a request
cd.current_edit.setText("abc")
assert_true("non-numeric input yields no request and an error",
            cd.build_request() is None and not cd.error_label.isHidden())
cd.current_edit.setText("0")
assert_true("a zero-current request is refused too",
            cd.build_request() is None)

# Save-only: the dialog arms (accepts) and never starts. There is no
# start-from-dialog path any more - starting is a separate press on the card.
cd.current_edit.setText("500")
cd._accept()
assert_true("Save accepts the dialog", cd.result() == _QD.DialogCode.Accepted)
assert_true("the dialog exposes no start-from-config path",
            not hasattr(cd, "start_now"))
cd2 = gui.ConfigDialog(None, 1, [1], chemistry=_C2.LIION)
assert_true("a single-slot dialog has no copy boxes", cd2.copy_boxes == {})

# Reopening Configure must show the config the slot HAS, not the house
# defaults. The card advertises that config, so a dialog that quietly reverted
# made Save look like "confirm" while replacing it - worst on the safety timer,
# which is exactly where it was caught.
from capture import RunRequest as _RR0                                  # noqa: E402
from protocol import Mode as _M0                                        # noqa: E402

_existing = _RR0(mode=_M0.DISCHARGE, chemistry=_C2.LIION, current_ma=750,
                 capacity_mah=3400, safety_timer_min=5, cutoff_mv=3000)
cd3 = gui.ConfigDialog(None, 1, [1], chemistry=_C2.LIION, existing=_existing)
assert_true("reopening shows the saved safety timer, not the 240 default",
            cd3.timer_edit.text() == "5", cd3.timer_edit.text())
assert_true("and the saved current, capacity and cutoff",
            (cd3.current_edit.text(), cd3.capacity_edit.text(),
             cd3.cutoff_edit.text()) == ("750", "3400", "3000"),
            f"{cd3.current_edit.text()}/{cd3.capacity_edit.text()}/"
            f"{cd3.cutoff_edit.text()}")
assert_true("saving it back reproduces the same request",
            cd3.build_request() == _existing, str(cd3.build_request()))

_charge = _RR0(mode=_M0.CHARGE, chemistry=_C2.LIION, current_ma=1000,
               capacity_mah=3000, safety_timer_min=90, cutoff_mv=None)
cd4 = gui.ConfigDialog(None, 1, [1], chemistry=_C2.LIION, existing=_charge)
assert_true("a charge config reopens as Charge, not the Discharge default",
            cd4.action_combo.currentData() == _M0.CHARGE,
            str(cd4.action_combo.currentData()))

cd5 = gui.ConfigDialog(None, 1, [1], chemistry=_C2.LIION)
assert_true("with nothing saved the capacity, current and timer are BLANK (the "
            "cutoff is the chemistry's own) and Save is off",
            (cd5.current_edit.text(), cd5.capacity_edit.text(),
             cd5.cutoff_edit.text(), cd5.timer_edit.text())
            == ("", "", "3200", "") and not cd5.save_button.isEnabled(),
            f"{cd5.current_edit.text()}/{cd5.capacity_edit.text()}/"
            f"{cd5.cutoff_edit.text()}/{cd5.timer_edit.text()}")
_type_in(cd5.capacity_edit, "5000")
assert_true("...entering 5000 mAh works out 1000 mA (0.2C) and the timer "
            "from it (450 min), and switches Save on",
            (cd5.current_edit.text(), cd5.timer_edit.text())
            == ("1000", "450") and cd5.save_button.isEnabled(),
            f"{cd5.current_edit.text()}/{cd5.timer_edit.text()}")
_type_in(cd5.capacity_edit, "")
assert_true("...and clearing it blanks them again and switches Save off",
            (cd5.current_edit.text(), cd5.timer_edit.text()) == ("", "")
            and not cd5.save_button.isEnabled())

print()
print("--- Configure: starting values reset on a change, timer follows ---")


def _fields(dlg):
    return (dlg.current_edit.text(), dlg.capacity_edit.text(),
            dlg.cutoff_edit.text(), dlg.timer_edit.text())


def _typed(edit, text):
    """What typing does: set the text AND emit textEdited (setText alone does
    not, which is exactly how the dialog tells its own changes from yours)."""
    edit.setText(text)
    edit.textEdited.emit(text)


def _pick(combo, data):
    combo.setCurrentIndex(combo.findData(data))


dd = gui.ConfigDialog(None, 1, [1], chemistry=_C2.LIION)
assert_true("the note leads with entering the capacity, then the datasheet",
            dd.defaults_note.text().startswith(
                "Enter your cell's rated capacity first")
            and "datasheet" in dd.defaults_note.text(),
            dd.defaults_note.text())
_typed(dd.capacity_edit, "3000")
assert_true("Li-ion discharge, 3000 mAh: 0.2C = 600 mA, 450 min",
            _fields(dd) == ("600", "3000", "3200", "450"), str(_fields(dd)))
_pick(dd.chem_combo, _C2.NIMH)
assert_true("choosing NiMH keeps the capacity and re-derives the rest "
            "(0.2C = 600 mA, 900 mV, 450 min)",
            _fields(dd) == ("600", "3000", "900", "450"), str(_fields(dd)))
_pick(dd.chem_combo, _C2.ENELOOP)
assert_true("choosing Eneloop does the same (no Std AA 2000 mAh preset)",
            _fields(dd) == ("600", "3000", "900", "450"), str(_fields(dd)))
_pick(dd.chem_combo, _C2.LIION)
assert_true("and back to Li-ion",
            _fields(dd) == ("600", "3000", "3200", "450"), str(_fields(dd)))

# A chemistry change keeps what the user TYPED (capacity, current), recalculates
# the rest, and takes back a cutoff that no longer fits the new chemistry.
_typed(dd.capacity_edit, "3400")
assert_true("a new capacity re-derives the untouched current (3400 mAh -> 680)",
            dd.current_edit.text() == "680", str(_fields(dd)))
_typed(dd.current_edit, "700")
_typed(dd.cutoff_edit, "3000")
_pick(dd.chem_combo, _C2.NIMH)
assert_true("a chemistry change keeps a typed current, drops a typed cutoff "
            "that is outside the new window (Li-ion 3000 on NiMH would "
            "become 900 silently), and re-derives the timer",
            _fields(dd) == ("700", "3400", "900", "440"), str(_fields(dd)))
_pick(dd.chem_combo, _C2.LIION)
_typed(dd.cutoff_edit, "2900")
_pick(dd.chem_combo, _C2.LIFEPO4)
assert_true("...while a typed cutoff that still fits the new window is kept "
            "(2900 is inside LiFePO4's 2600-3000)",
            dd.cutoff_edit.text() == "2900", dd.cutoff_edit.text())

# An action change re-derives the current (unless typed) and the timer, and
# never touches the capacity the user set first.
dd2 = gui.ConfigDialog(None, 1, [1], chemistry=_C2.NIMH)
_typed(dd2.capacity_edit, "2600")
_pick(dd2.action_combo, _M2.CHARGE)
assert_true("an action change re-derives the current at 0.5C, capped at the "
            "panel's 1000 mA (nickel charge x2.5: 2600 mAh at 1.00 A = 390 min)",
            (dd2.current_edit.text(), dd2.timer_edit.text()) == ("1000", "390"),
            str(_fields(dd2)))
assert_true("...and leaves the capacity the user entered",
            dd2.capacity_edit.text() == "2600")
_pick(dd2.action_combo, _M2.DISCHARGE)
assert_true("...and back: 0.2C of 2600 mAh = 520 mA",
            dd2.current_edit.text() == "520", dd2.current_edit.text())
_typed(dd2.current_edit, "450")
_pick(dd2.action_combo, _M2.CHARGE)
assert_true("a current the user typed survives an action change",
            dd2.current_edit.text() == "450", dd2.current_edit.text())
_typed(dd2.current_edit, "")
assert_true("...and blanking it hands it back to the derivation",
            dd2.current_edit.text() == "1000", dd2.current_edit.text())

# The timer follows capacity and current until the user takes it over.
dd = dd2
_typed(dd.capacity_edit, "2000")
assert_true("typing a capacity moves the timer (2000 mAh at 1.00 A = 300 min)",
            dd.timer_edit.text() == "300", dd.timer_edit.text())
_typed(dd.current_edit, "500")
assert_true("typing a current moves it too (2000 mAh at 0.50 A = 600 min)",
            dd.timer_edit.text() == "600", dd.timer_edit.text())
_typed(dd.timer_edit, "45")
_typed(dd.capacity_edit, "2500")
assert_true("once the timer is edited by hand it stays put",
            dd.timer_edit.text() == "45", dd.timer_edit.text())
_pick(dd.chem_combo, _C2.ENELOOP)
assert_true("...and a typed timer survives a chemistry change too: it is the "
            "protection that outlives the app",
            dd.timer_edit.text() == "45", dd.timer_edit.text())

# Reopening a slot shows ITS config and does not reset anything.
_ex = _RR0(mode=_M0.DISCHARGE, chemistry=_C2.NIMH, current_ma=400,
           capacity_mah=2500, safety_timer_min=480, cutoff_mv=1000)
dx = gui.ConfigDialog(None, 1, [1], chemistry=_C2.NIMH, existing=_ex)
assert_true("a saved config reopens exactly as saved",
            _fields(dx) == ("400", "2500", "1000", "480"), str(_fields(dx)))
_typed(dx.capacity_edit, "3000")
assert_true("and editing its capacity does not move its saved timer",
            dx.timer_edit.text() == "480", dx.timer_edit.text())
_no_cut = _RR0(mode=_M0.DISCHARGE, chemistry=_C2.NIMH, current_ma=400,
               capacity_mah=2500, safety_timer_min=480)
dnc = gui.ConfigDialog(None, 1, [1], chemistry=_C2.NIMH, existing=_no_cut)
assert_true("a saved config with no cutoff shows the chemistry's own, not Li-ion's",
            dnc.cutoff_edit.text() == "900", dnc.cutoff_edit.text())

# The 1C warning: shown, never blocking.
dw = gui.ConfigDialog(None, 1, [1], chemistry=_C2.LIION)
assert_true("no warning at the starting values", dw.rate_warning.isHidden())
_pick(dw.action_combo, _M2.CHARGE)
_typed(dw.capacity_edit, "2000")
assert_true("the derived 0.5C current never warns",
            dw.rate_warning.isHidden(), dw.rate_warning.text())
_typed(dw.current_edit, "3000")
assert_true("3.00 A typed on 2000 mAh warns, naming 1C and the datasheet",
            not dw.rate_warning.isHidden()
            and "1C" in dw.rate_warning.text()
            and "datasheet" in dw.rate_warning.text(), dw.rate_warning.text())
assert_true("...and does not stop the request being built",
            dw.build_request() is not None, dw.error_label.text())
_typed(dw.capacity_edit, "3000")
assert_true("the warning clears once the capacity covers the current",
            dw.rate_warning.isHidden())

# Storage: the charge leg is the main Current field, the
# discharge leg is the first Advanced row, and both follow the capacity.
_ds = gui.ConfigDialog(None, 1, [1], chemistry=_C2.LIION)
_pick(_ds.action_combo, _M2.STORAGE)
_dis = _ds.adv_edits["discharge_current_ma"]
assert_true("Storage with no capacity yet: both currents blank, Save off",
            _ds.current_edit.text() == "" and _dis.text() == ""
            and not _ds.save_button.isEnabled(),
            f"{_ds.current_edit.text()!r} {_dis.text()!r}")
_typed(_ds.capacity_edit, "3400")
assert_true("Storage, 3400 mAh: 0.5C charge = 1700, 0.2C discharge = 680 "
            "(the old defaults were 3000 / 2000)",
            (_ds.current_edit.text(), _dis.text()) == ("1700", "680"),
            f"{_ds.current_edit.text()} / {_dis.text()}")
assert_true("...and the timer covers the whole capacity at the slower leg "
            "(3400 mAh at 680 mA x 1.5 = 450 min)",
            _ds.timer_edit.text() == "450", _ds.timer_edit.text())
assert_true("...and neither leg warns", _ds.rate_warning.isHidden(),
            _ds.rate_warning.text())
_req = _ds.build_request()
assert_true("...and the request carries both, always naming the discharge one",
            _req is not None and _req.current_ma == 1700
            and _req.discharge_current_ma == 680
            and _req.safety_timer_min == 450,
            str(_req) + _ds.error_label.text())
_typed(_ds.capacity_edit, "800")
assert_true("a small cell: 400 / 160, and no 1C warning (the old defaults "
            "warned on both legs)",
            (_ds.current_edit.text(), _dis.text()) == ("400", "160")
            and _ds.rate_warning.isHidden(), _ds.rate_warning.text())
_typed(_dis, "500")
_typed(_ds.capacity_edit, "2600")
assert_true("a typed discharge current is kept when the capacity changes, "
            "while the charge leg follows (1300), and the timer follows the "
            "slower leg (2600 at 500 mA x 1.5 = 468, rounded up to 470)",
            (_ds.current_edit.text(), _dis.text(), _ds.timer_edit.text())
            == ("1300", "500", "470"),
            f"{_ds.current_edit.text()} {_dis.text()} {_ds.timer_edit.text()}")
_typed(_dis, "")
assert_true("blanking it hands it back to the derivation (0.2C of 2600 = 520)",
            _dis.text() == "520", _dis.text())
_typed(_dis, "500")
_pick(_ds.action_combo, _M2.DISCHARGE)
_pick(_ds.action_combo, _M2.STORAGE)
assert_true("leaving Storage and coming back takes the page back to its own "
            "values: the typed 500 is gone, 0.2C again",
            _dis.text() == "520", _dis.text())
_typed(_dis, "5000")
assert_true("a discharge leg over 1C still warns, naming Discharge",
            not _ds.rate_warning.isHidden()
            and "Discharge current 5000" in _ds.rate_warning.text(),
            _ds.rate_warning.text())
_sx = _RR0(mode=_M0.STORAGE, chemistry=_C2.LIION, current_ma=1500,
           capacity_mah=3000, safety_timer_min=500, discharge_current_ma=900)
_dsx = gui.ConfigDialog(None, 1, [1], chemistry=_C2.LIION, existing=_sx)
_typed(_dsx.capacity_edit, "2000")
assert_true("a saved Storage config reopens: the discharge current it was "
            "given by hand (900, not the derived 600) is kept, while the "
            "charge current that was only DERIVED (0.5C) follows the new "
            "capacity",
            _dsx.adv_edits["discharge_current_ma"].text() == "900"
            and _dsx.current_edit.text() == "1000",
            f"{_dsx.adv_edits['discharge_current_ma'].text()} "
            f"{_dsx.current_edit.text()}")

# The charger's 24 h limit: held, and said.
dl = gui.ConfigDialog(None, 1, [1], chemistry=_C2.LIION)
_typed(dl.capacity_edit, "9000")
_typed(dl.current_edit, "100")
assert_true("a run needing more than 24 h gets 1440 min and a note saying so",
            dl.timer_edit.text() == "1440" and not dl.timer_note.isHidden(),
            dl.timer_edit.text())
_typed(dl.current_edit, "1500")
assert_true("the note goes when it no longer applies",
            dl.timer_note.isHidden(), dl.timer_edit.text())

# Out-of-range values are refused in the dialog, with the plain message.
dr = gui.ConfigDialog(None, 1, [1], chemistry=_C2.LIION)
_typed(dr.capacity_edit, "5000")
_typed(dr.current_edit, "2500")
assert_true("a discharge current over the charger's 2000 mA is refused, not "
            "left for the firmware to swap",
            dr.build_request() is None and "2000" in dr.error_label.text(),
            dr.error_label.text())
_typed(dr.current_edit, "500")
_typed(dr.timer_edit, "2000")
assert_true("a timer over 1440 min is refused too",
            dr.build_request() is None and "1440" in dr.error_label.text(),
            dr.error_label.text())

print()
print("--- per-slot state machine and arming ---")
#
# The independence backbone: each slot has a state (empty/idle/armed/running/
# finished) and can be armed with a pending config on its own schedule.
# Pure logic, no hardware.

from capture import RunRequest as _RR
from protocol import Chemistry as _Chem, Mode as _Mode, parse_status_response as _ps


def _status(cur, mv, state_ok=True):
    return _ps(gui.build_status_frame(1, cur, mv, 100, 60, 40))


def _good_req(current=500):
    return _RR(mode=_Mode.DISCHARGE, chemistry=_Chem.LIION, current_ma=current,
               capacity_mah=2000, safety_timer_min=60, cutoff_mv=3000)


# derive_slot_state: each state and the priority order
running = _status(500, 3700)
idle = _status(0, 3700)
empty = _ps(gui.build_status_frame(1, 0, 0, 0, 0, 0))
assert_true("running wins over everything",
            gui.derive_slot_state(running, armed=True, finished=True,
                                  starting=True) == "running")
assert_true("starting beats armed and finished (config already committed)",
            gui.derive_slot_state(idle, armed=False, finished=True,
                                  starting=True) == "starting")
assert_true("current flowing wins over a lingering starting flag",
            gui.derive_slot_state(running, armed=False, finished=False,
                                  starting=True) == "running")
assert_true("armed beats finished and idle",
            gui.derive_slot_state(idle, armed=True, finished=True) == "armed")
assert_true("finished when idle and not armed",
            gui.derive_slot_state(idle, armed=False, finished=True) == "finished")
assert_true("empty renders empty",
            gui.derive_slot_state(empty, armed=False, finished=False) == "empty")
assert_true("idle when a cell rests",
            gui.derive_slot_state(idle, armed=False, finished=False) == "idle")
assert_true("unknown before any frame",
            gui.derive_slot_state(None, armed=False, finished=False) == "unknown")

# torn_down: the one thing that overrules a current reading. A safety-timer
# termination leaves voltage/current/state echoing their last pre-teardown
# values (measured 4.138V / 502mA flat for five minutes), so without this the
# card reads "running" forever while the CLI calls the same slot FINISHED.
assert_true("a torn-down slot does not read as running on a latched current",
            gui.derive_slot_state(running, armed=False, finished=True,
                                  torn_down=True) == "latched")
assert_true("latched outranks armed, so a configured slot still shows the "
            "Stop that clears it",
            gui.derive_slot_state(running, armed=True, finished=True,
                                  torn_down=True) == "latched",
            "reading 'armed' here left a card with no way to start at all")
assert_true("latched outranks starting too",
            gui.derive_slot_state(running, armed=False, finished=False,
                                  starting=True, torn_down=True) == "latched")
assert_true("a slot NOT torn down still reads running while finished is set",
            gui.derive_slot_state(running, armed=False, finished=True,
                                  torn_down=False) == "running",
            "a genuine restart after a finished run must still read running")

# the card: a latched slot is the one "finished" slot with something to stop
lat_card = gui.SlotCard(3, "TEST-03", gui_theme.DARK)
lat_card.render_state("latched", None)
assert_true("a latched card says so rather than reading plainly finished",
            lat_card.state_label.text() == "latched",
            lat_card.state_label.text())
assert_true("Configure stays available while latched",
            lat_card.configure_button.isEnabled())
assert_true("and Stop is enabled, so the latch can be cleared",
            (lat_card.action_button.text(), lat_card.action_button.isEnabled())
            == ("Stop", True),
            f"{lat_card.action_button.text()!r} "
            f"enabled={lat_card.action_button.isEnabled()}")
lat_stops = []
lat_card.stop_requested.connect(lat_stops.append)
lat_card._on_action()
assert_true("pressing it asks for a stop, not a start", lat_stops == [3],
            str(lat_stops))

plain_card = gui.SlotCard(3, "TEST-03", gui_theme.DARK)
plain_card.render_state("finished", None)
assert_true("an ordinary finished card is unchanged",
            plain_card.state_label.text() == "finished"
            and plain_card.action_button.text() == "Start"
            and not plain_card.action_button.isEnabled())

# and the controller must take that from the capture, not re-derive it
td_ctl = gui.CaptureController(gui.SampleBridge(), gui.DemoSource([1]), [1],
                               "torn", tempfile.mkdtemp())
td_ctl.cap = type("Cap", (), {"latest": {1: running}, "finished": {1},
                              "torn_down": {1},
                              "measuring_ir": set()})()
assert_true("slot_state honors Capture.torn_down over the latched current",
            td_ctl.slot_state(1) == "latched", td_ctl.slot_state(1))
td_ctl.cap = type("Cap", (), {"latest": {1: running}, "finished": {1},
                              "torn_down": set(),
                              "measuring_ir": set()})()
assert_true("and reads running again once the latch is gone",
            td_ctl.slot_state(1) == "running", td_ctl.slot_state(1))

# "measuring": the pre-flight IR sampler owns the slot, arming and stopping it
# repeatedly. It outranks a current reading for the same reason "latched" does -
# every other state flickers while it runs, and none of them is what is
# happening. It cannot collide with "latched": the capture suspends recording
# for a slot being sampled, so torn_down is never set for one.
assert_true("measuring outranks a current reading",
            gui.derive_slot_state(running, armed=False, finished=False,
                                  measuring_ir=True) == "measuring")
assert_true("measuring outranks armed and starting too",
            gui.derive_slot_state(running, armed=True, finished=False,
                                  starting=True,
                                  measuring_ir=True) == "measuring")
assert_true("and the slot reverts once sampling ends",
            gui.derive_slot_state(running, armed=False, finished=False,
                                  measuring_ir=False) == "running")
ir_ctl = gui.CaptureController(gui.SampleBridge(), gui.DemoSource([1]), [1],
                               "ir", tempfile.mkdtemp())
ir_ctl.cap = type("Cap", (), {"latest": {1: running}, "finished": set(),
                              "torn_down": set(), "measuring_ir": {1}})()
assert_true("the controller takes it from the capture, not re-derived",
            ir_ctl.slot_state(1) == "measuring", ir_ctl.slot_state(1))
_ircard = gui.SlotCard(1, "TEST-01", gui_theme.DARK)
_ircard.render_state("measuring", None)
assert_true("the card says what it is doing",
            _ircard.action_button.text() == "Measuring IR…",
            _ircard.action_button.text())
assert_true("...and offers no action that would race the sampler",
            not _ircard.action_button.isEnabled()
            and not _ircard.configure_button.isEnabled())

# starting a latched slot must explain the latch, not report it as running
ls_ctl = gui.CaptureController(gui.SampleBridge(), gui.DemoSource([1]), [1],
                               "latchstart", tempfile.mkdtemp())
ls_ctl.cap = type("Cap", (), {"latest": {1: running}, "finished": {1},
                              "torn_down": {1},
                              "measuring_ir": set()})()
# look connected, so start_slot gets past its is_running check
ls_ctl._thread = type("T", (), {"is_alive": lambda self: True})()
ls_ctl.arm(1, _good_req())
ls_msg = ls_ctl.start_slot(1)
assert_true("starting a latched slot names the latch, not 'already running'",
            "Press Stop" in ls_msg and "already running" not in ls_msg, ls_msg)
assert_true("and the config is left armed, not consumed",
            ls_ctl.slot_request(1) is not None)
ls_ctl._thread = None

# A slot that is simply busy (a panel run, or a finished panel
# break-in whose last current is still being reported) is refused in words that
# make sense in the window - the CLI's "Use --attach" does not.
bz_ctl = gui.CaptureController(gui.SampleBridge(), gui.DemoSource([1]), [1],
                               "busystart", tempfile.mkdtemp())
bz_ctl.cap = type("Cap", (), {"latest": {1: running}, "finished": set(),
                              "torn_down": set(),
                              "measuring_ir": set()})()
bz_ctl._thread = type("T", (), {"is_alive": lambda self: True})()
bz_ctl.arm(1, _good_req())
bz_msg = bz_ctl.start_slot(1)
assert_true("starting a busy slot is refused, naming the slot",
            bz_msg.startswith("cannot start slot 1:")
            and "already running something" in bz_msg, bz_msg)
assert_true("...without telling a window user to use a command-line flag",
            "--attach" not in bz_msg, bz_msg)
assert_true("...explaining it may be a charger-started run being watched",
            "SITREP91 is only watching it" in bz_msg, bz_msg)
assert_true("...and naming the Done message of a finished panel break-in",
            "break-in" in bz_msg and "Done" in bz_msg, bz_msg)
assert_true("and the config is left armed, not consumed",
            bz_ctl.slot_request(1) is not None)
bz_ctl._thread = None

# Group Start with a member that is already running at the panel: the busy
# member is left alone, the message and the
# group record name only the slots that really started.
gb_ctl = gui.CaptureController(gui.SampleBridge(), gui.DemoSource([1, 3]),
                               [1, 3], "groupbusy", tempfile.mkdtemp())
gb_ctl.cap = type("Cap", (), {"latest": {1: idle, 3: running},
                              "finished": set(), "torn_down": set(),
                              "measuring_ir": set()})()
gb_ctl._thread = type("T", (), {"is_alive": lambda self: True})()
gb_ctl.arm(1, _good_req()); gb_ctl.arm(3, _good_req())
gb_ctl.group([1, 3])
gb_msg = gb_ctl.start_group(gb_ctl.group_id_of(1))
gb_sent = []
while not gb_ctl._inject_queue.empty():
    gb_sent.append(gb_ctl._inject_queue.get_nowait())
assert_true("group Start sends only the slot that can start",
            [i.slot for i in gb_sent] == [1], str([i.slot for i in gb_sent]))
assert_true("...and the message says slot 1 started, not slot 3",
            "[1]" in gb_msg and "[1, 3]" not in gb_msg, gb_msg)
assert_true("...and says why slot 3 did not",
            "slot 3" in gb_msg and "already running something" in gb_msg, gb_msg)
assert_true("a lone starter is not recorded as a group",
            gb_sent[0].group is None, str(gb_sent[0].group))
assert_true("the busy member's config is left armed",
            gb_ctl.slot_request(3) is not None)
# two that can start still share ONE group record
gb2 = gui.CaptureController(gui.SampleBridge(), gui.DemoSource([1, 2, 3]),
                            [1, 2, 3], "groupbusy2", tempfile.mkdtemp())
gb2.cap = type("Cap", (), {"latest": {1: idle, 2: idle, 3: running},
                           "finished": set(), "torn_down": set(),
                           "measuring_ir": set()})()
gb2._thread = type("T", (), {"is_alive": lambda self: True})()
for _s in (1, 2, 3):
    gb2.arm(_s, _good_req())
gb2.group([1, 2, 3])
gb2.start_group(gb2.group_id_of(1))
gb2_sent = []
while not gb2_sent or not gb2._inject_queue.empty():
    gb2_sent.append(gb2._inject_queue.get_nowait())
assert_true("two startable members share one group naming exactly them",
            len(gb2_sent) == 2 and gb2_sent[0].group is gb2_sent[1].group
            and gb2_sent[0].group.slots == (1, 2),
            str([i.group for i in gb2_sent]))
gb_ctl._thread = None
gb2._thread = None

# the "starting" window: start commits the config (armed -> starting) and
# slot_state resolves it - current confirms, or a timeout hands it back.
st_ctl = gui.CaptureController(gui.SampleBridge(), gui.DemoSource([1]), [1],
                               "starting", tempfile.mkdtemp())
st_ctl.arm(1, _good_req(500))
assert_true("a freshly armed slot reads armed", st_ctl.slot_state(1) == "armed")
# commit the config as start_slot would, without a live session
st_ctl._armed.pop(1)
st_ctl._starting[1] = (time.monotonic(), _good_req(500))
assert_true("a committed-but-not-yet-running slot reads starting",
            st_ctl.slot_state(1) == "starting")
assert_true("the config is still visible while starting",
            st_ctl.slot_request(1) is not None)
# a start that never draws current times out - the config is DROPPED, not
# restored, so consume-on-start holds and no stale Start lingers.
st_ctl._starting[1] = (time.monotonic() - (gui.STARTING_TIMEOUT + 5),
                       _good_req(500))
timed_out_state = st_ctl.slot_state(1)
assert_true("a start that never comes up times out and clears the config",
            1 not in st_ctl._armed and 1 not in st_ctl._starting
            and timed_out_state != "starting", timed_out_state)

# _last_config: the card's persistent "set up for" identity - held from arming
# until the NEXT Save, through start/stop/finish (unlike _armed, which
# consume-on-start empties), so a finished slot still reads its chemistry+action.
lc_ctl = gui.CaptureController(gui.SampleBridge(), gui.DemoSource([1]), [1],
                               "lastcfg", tempfile.mkdtemp())
assert_true("no config before arming", lc_ctl.last_config(1) is None)
r_dis = _good_req(500)
lc_ctl.arm(1, r_dis)
assert_true("arming records the last config", lc_ctl.last_config(1) is r_dis)
lc_ctl._armed.pop(1); lc_ctl._starting[1] = (time.monotonic(), r_dis)   # started
assert_true("starting keeps the last config", lc_ctl.last_config(1) is r_dis)
lc_ctl._starting.pop(1)                                     # run stopped/finished
assert_true("a finished slot still has its last config",
            lc_ctl.last_config(1) is r_dis and lc_ctl.slot_request(1) is None)
r_chg = _RR(mode=_Mode.CHARGE, chemistry=_Chem.LIION, current_ma=1000,
            capacity_mah=2000, safety_timer_min=60)
lc_ctl.arm(1, r_chg)
assert_true("only the next Save changes the last config",
            lc_ctl.last_config(1) is r_chg)

# describe_config: chemistry + action always, rate only when full
assert_true("config summary is chemistry + action, human-readable",
            gui.describe_config(r_dis, full=False) == "Li-ion  ·  Discharge",
            gui.describe_config(r_dis, full=False))
assert_true("full config adds the rate and cutoff",
            "500 mA → 3000 mV" in gui.describe_config(r_dis, full=True))

# arm validation and state reflection
sm_ctl = gui.CaptureController(gui.SampleBridge(), gui.DemoSource([1, 2]),
                               [1, 2], "sm", tempfile.mkdtemp())
assert_true("arming an unmonitored slot is refused",
            sm_ctl.arm(3, _good_req()) is not None)
assert_true("arming an invalid request is refused",
            sm_ctl.arm(1, _good_req(current=0)) is not None)
assert_true("a good request arms cleanly", sm_ctl.arm(1, _good_req()) is None)
assert_true("the armed request is retrievable",
            sm_ctl.slot_request(1).current_ma == 500)
assert_true("an armed slot reads 'armed' (no cap yet)",
            sm_ctl.slot_state(1) == "armed")
sm_ctl.disarm(1)
assert_true("disarming clears the pending config",
            sm_ctl.slot_request(1) is None)

# groups: form once, break on edit, leave on disarm; ids run A, B, C in order
sm_ctl.arm(1, _good_req()); sm_ctl.arm(2, _good_req()); sm_ctl.group([1, 2])
assert_true("group() ties the slots", sm_ctl.group_of(1) == {1, 2}
            and sm_ctl.group_of(2) == {1, 2})
assert_true("the first group is A (id 1), not a skipped letter",
            sm_ctl.group_id_of(1) == 1)
sm_ctl.arm(1, _good_req(current=1000))   # re-config slot 1 breaks the group
assert_true("re-configuring a member breaks the group",
            sm_ctl.group_of(1) is None and sm_ctl.group_of(2) is None)
sm_ctl.arm(1, _good_req()); sm_ctl.arm(2, _good_req()); sm_ctl.group([1, 2])
assert_true("after breaking A, a new group reuses A (lowest free id), not B",
            sm_ctl.group_id_of(1) == 1)
sm_ctl.disarm(2)
assert_true("disarming a member dissolves a two-slot group",
            sm_ctl.group_of(1) is None)
assert_true("a group of fewer than two is not formed",
            (sm_ctl.group([1]), sm_ctl.group_of(1))[1] is None)

# sticky letters + leave-group + keep-the-rest, on a four-slot controller
gm_ctl = gui.CaptureController(gui.SampleBridge(), gui.DemoSource([1, 2, 3, 4]),
                               [1, 2, 3, 4], "groupmech", tempfile.mkdtemp())
for s in (1, 2, 3):
    gm_ctl.arm(s, _good_req())
gm_ctl.group([1, 2, 3])
gid = gm_ctl.group_id_of(1)
assert_true("group_id_for_exact finds the exact-set group",
            gm_ctl.group_id_for_exact([1, 2, 3]) == gid)
assert_true("a different set has no exact match",
            gm_ctl.group_id_for_exact([1, 2]) is None)
# leave-group: one out, the rest stay grouped AND keep the letter, config intact
gm_ctl.leave_group(1)
assert_true("leaving removes just that slot, the rest stay grouped",
            gm_ctl.group_of(1) is None and gm_ctl.group_of(2) == {2, 3})
assert_true("the remaining group keeps its id (letter)",
            gm_ctl.group_id_of(2) == gid)
assert_true("leaving does not disturb the slot's config",
            gm_ctl.slot_request(1) is not None)
# sticky: re-forming the exact set reuses its id, so the letter survives a re-run
prior = gm_ctl.group_id_for_exact([2, 3])
gm_ctl.arm(2, _good_req()); gm_ctl.arm(3, _good_req())     # dissolves {2,3}
gm_ctl.group([2, 3], keep_id=prior)
assert_true("re-forming the same set keeps its letter (sticky)",
            gm_ctl.group_id_of(2) == prior == gid)

# lowest-free ids: the letter reflects the groups that currently exist
lf_ctl = gui.CaptureController(gui.SampleBridge(), gui.DemoSource([1, 2, 3, 4]),
                               [1, 2, 3, 4], "lowfree", tempfile.mkdtemp())
for s in (1, 2, 3, 4):
    lf_ctl.arm(s, _good_req())
lf_ctl.group([1, 2]); lf_ctl.group([3, 4])
assert_true("two groups take A (1) then B (2)",
            lf_ctl.group_id_of(1) == 1 and lf_ctl.group_id_of(3) == 2)
lf_ctl.ungroup(1)                        # break A -> id 1 free, B stays B
assert_true("breaking A frees its id but leaves B as B",
            lf_ctl.group_id_of(1) is None and lf_ctl.group_id_of(3) == 2)
lf_ctl.arm(1, _good_req()); lf_ctl.arm(2, _good_req()); lf_ctl.group([1, 2])
assert_true("the next NEW group reuses the freed A (id 1), not C",
            lf_ctl.group_id_of(1) == 1)
# but reconfiguring an existing group (keep_id) never grabs a lower free id
lf_ctl.ungroup(1)                        # free A (id 1) again; B (id 2) remains
prior = lf_ctl.group_id_for_exact([3, 4])
lf_ctl.arm(3, _good_req()); lf_ctl.arm(4, _good_req())
lf_ctl.group([3, 4], keep_id=prior)
assert_true("reconfiguring B keeps it B even though A is free (no rename)",
            lf_ctl.group_id_of(3) == 2)

# slot_state against a live cap
sm_ctl.cap = type("Cap", (), {"latest": {1: running, 2: idle},
                              "finished": set(), "torn_down": set(),
                              "measuring_ir": set()})()
sm_ctl.disarm(1)
assert_true("slot_state reads 'running' from the cap",
            sm_ctl.slot_state(1) == "running")
assert_true("slot_state reads 'idle' from the cap",
            sm_ctl.slot_state(2) == "idle")

print()
print("--- M4 layout: chart, cards, time axis ---")
#
# The experiment-centric widgets: one shared chart with a metric toggle and an
# hours-not-kiloseconds time axis, cards led by cell identity, empty slots
# rendered empty, finished cells kept on the chart.

from datetime import datetime as _dt2
from capture import Sample as _S2
from protocol import parse_status_response as _p2

m4_tmp = tempfile.mkdtemp()
m4_ctl = gui.CaptureController(
    gui.SampleBridge(), gui.DemoSource([1, 2]), [1, 2], "m4", m4_tmp,
    cells={1: "TEST-03", 2: None})
m4_win = gui.MainWindow(m4_ctl, [1, 2], gui_theme.DARK)


def _mk(slot, cur, mv, cap=100, el=60, r=40):
    st = _p2(gui.build_status_frame(slot, cur, mv, cap, el, r))
    return _S2(wall_time=_dt2(2026, 8, 18), elapsed_s=float(el), slot=slot,
               status=st, drift_severity="ok", note="")


# time axis: seconds -> h:mm / m:ss
ax = gui.TimeAxisItem(orientation="bottom")
assert_true("time axis renders 90s as 1:30",
            ax.tickStrings([90], 1, 1) == ["1:30"], ax.tickStrings([90], 1, 1))
assert_true("time axis renders 3900s as 1h05 (an hour or more)",
            ax.tickStrings([3900], 1, 1) == ["1h05"], ax.tickStrings([3900], 1, 1))
assert_true("under an hour every tick is m:ss",
            ax.tickStrings([0, 1200, 3000], 1, 1) == ["0:00", "20:00", "50:00"],
            str(ax.tickStrings([0, 1200, 3000], 1, 1)))
assert_true("once any tick reaches an hour EVERY tick changes style, so "
            "'1h06' can never be read as 1 min 06 s next to '50:00'",
            ax.tickStrings([2000, 3000, 3960], 1, 1) == ["0h33", "0h50", "1h06"],
            str(ax.tickStrings([2000, 3000, 3960], 1, 1)))
_nd = gui.ConfigDialog(None, 1, [1])
_names = [_nd.chem_combo.itemText(i) for i in range(_nd.chem_combo.count())]
assert_true("Configure names chemistries as the cards do (Li-ion, NiMH), not "
            "by their command-line ids",
            any(n == "Li-ion" for n in _names) and any(n == "NiMH" for n in _names)
            and not any(n in ("liion", "nimh", "liion_hv") for n in _names),
            str(_names))

# cards led by cell identity
assert_true("a card with a cell id is titled by it",
            m4_win.cards[1].title() == "TEST-03", m4_win.cards[1].title())
assert_true("a card without a cell id falls back to the slot",
            m4_win.cards[2].title() == "Slot 2", m4_win.cards[2].title())

# a real sample populates the card and the chart
m4_win.on_sample(_mk(1, 500, 3700))
assert_true("card shows the voltage", m4_win.cards[1].values["Volts"].text() == "3.700")
assert_true("chart stored the point", len(m4_win.chart.xs[1]) == 1)

# empty slot renders empty, and is NOT plotted
m4_win.on_sample(_mk(2, 0, 0))
assert_true("an empty slot card says 'empty'",
            m4_win.cards[2].state_label.text() == "empty")
assert_true("an empty slot is not plotted",
            len(m4_win.chart.xs[2]) == 0)

# regression: the frequent sample path must resolve the
# "starting" window too. on_sample once derived a parallel state and skipped it,
# so _starting stuck and a stopped slot rendered "armed" off a consumed config.
# A dedicated window, so feeding it samples does not disturb m4_win's history.
sr_ctl = gui.CaptureController(gui.SampleBridge(), gui.DemoSource([1]), [1],
                               "samplepath", tempfile.mkdtemp())
sr_win = gui.MainWindow(sr_ctl, [1], gui_theme.DARK)
sr_ctl.arm(1, _good_req(500)); sr_ctl._armed.pop(1)     # commit slot 1 to start
sr_ctl._starting[1] = (time.monotonic(), _good_req(500))
sr_win.on_sample(_mk(1, 500, 3700))          # current flowing confirms the run
assert_true("on_sample clears the starting window when current flows",
            1 not in sr_ctl._starting
            and sr_win.cards[1].state_label.text() == "running")
sr_win.on_sample(_mk(1, 0, 3700))            # the slot goes idle after a stop
assert_true("after a stop the sample path shows no stale 'armed'",
            sr_win.cards[1].state_label.text() != "armed"
            and sr_ctl.slot_request(1) is None)

# metric toggle re-renders from stored history
m4_win.on_sample(_mk(1, 500, 3720, cap=140))
before = m4_win.chart.curves[1].getData()[1]
m4_win.chart.set_metric("capacity")
after = m4_win.chart.curves[1].getData()[1]
assert_true("switching metric re-renders the curve from stored data",
            list(after) == [100.0, 140.0] and list(before) != list(after),
            f"{list(before)} -> {list(after)}")
assert_true("the y-axis relabels to the metric",
            "mAh" in m4_win.chart.plot.getAxis("left").labelString())
m4_win.chart.set_metric("voltage")

# finished cells stay on the chart, card says finished
m4_ctl.cap = type("Cap", (), {"latest": {}, "finished": {1}, "gaps": [],
                              "torn_down": set(), "measuring_ir": set(),
                              "csv_path": "m4.csv"})()
m4_win.on_sample(_mk(1, 0, 3600))
assert_true("a finished slot's card says finished",
            m4_win.cards[1].state_label.text() == "finished")
assert_true("a finished slot's curve stays on the chart",
            len(m4_win.chart.xs[1]) == 3)

# health bar reflects the run
assert_true("health bar shows samples and drift-clean",
            "samples" in m4_win.health_label.text()
            and "drift clean" in m4_win.health_label.text(),
            m4_win.health_label.text())

print()
print("--- link losses, unverified firmware, and the stale-link banner ---")
#
# Link losses, unverified firmware and the stale-link warning as the GUI shows
# them. The stale-link warning arrives while NO
# samples flow (the charger has vanished), so it is driven through on_status -
# every retry logs a line - and must persist past the next status message.
_real_cap = m4_ctl.cap
m4_ctl.cap = type("Cap", (), {
    "latest": {}, "finished": set(), "gaps": ["t1", "t2"], "torn_down": set(),
    "measuring_ir": set(), "csv_path": "m4.csv", "firmware_verified": False,
    "stale_link_warning": None})()
m4_win.on_status("connected")
_h = m4_win.health_label.text()
assert_true("the health bar counts link losses in plain words",
            "2 link losses" in _h and "reconnect" not in _h, _h)
assert_true("unverified firmware is labeled, even before the first sample",
            "UNVERIFIED FIRMWARE" in _h, _h)
assert_true("no stale-link banner while the link is fine",
            m4_win.link_banner.isHidden())
import capture as _capture_mod                    # noqa: E402
m4_ctl.cap.stale_link_warning = _capture_mod.stale_link_message(3)
m4_win.on_status("session raised BleakDeviceNotFoundError: not found")
m4_win.on_status("connection lost; retrying in 3s")
assert_true("the stale-link warning raises a banner that outlives the "
            "status line", not m4_win.link_banner.isHidden()
            and "POWER-CYCLE THE CHARGER" in m4_win.link_banner.text()
            and "retrying" in m4_win.status_label.text(),
            m4_win.link_banner.text())
m4_ctl.cap.stale_link_warning = None
m4_win.on_status("connected")
assert_true("...and the banner clears when the charger is back",
            m4_win.link_banner.isHidden())
m4_ctl.cap = _real_cap

print()
print("--- loud drift banner ---")
#
# A drift error must be impossible to miss, not a transient status line the
# next sample overwrites. The banner is hidden until a warn/error Sample
# arrives, then persistent and colored. on_sample is driven directly with
# crafted Samples - the demo source never drifts, and this is the unit anyway.

from datetime import datetime as _dt
from capture import Sample as _Sample
from protocol import parse_status_response as _parse

drift_tmp = tempfile.mkdtemp()
drift_ctl = gui.CaptureController(
    gui.SampleBridge(), gui.DemoSource([1]), [1], "drift", drift_tmp)
drift_win = gui.MainWindow(drift_ctl, [1], gui_theme.DARK)


def _sample(slot, severity):
    st = _parse(gui.build_status_frame(slot, 500, 3700, 100, 60, 40))
    return _Sample(wall_time=_dt(2026, 8, 18), elapsed_s=1.0, slot=slot,
                   status=st, drift_severity=severity, note="")


assert_true("banner hidden before any drift",
            drift_win.drift_banner.isHidden())

drift_win.on_sample(_sample(1, "ok"))
assert_true("a clean sample does not raise the banner",
            drift_win.drift_banner.isHidden())

drift_win.on_sample(_sample(1, "warn"))
assert_true("a warning raises the banner", (not drift_win.drift_banner.isHidden()))
assert_true("the banner counts the warning",
            "1 warning" in drift_win.drift_banner.text(),
            drift_win.drift_banner.text())
warn_style = drift_win.drift_banner.styleSheet()

drift_win.on_sample(_sample(1, "error"))
assert_true("an error is counted", "1 error" in drift_win.drift_banner.text(),
            drift_win.drift_banner.text())
assert_true("the banner recolours to error (error wins over warning)",
            drift_win.drift_banner.styleSheet() != warn_style
            and gui_theme.PALETTES["dark"]["error"] in drift_win.drift_banner.styleSheet())

drift_win.on_sample(_sample(1, "ok"))
assert_true("a later clean sample does NOT clear the banner - run stays suspect",
            (not drift_win.drift_banner.isHidden())
            and "1 error" in drift_win.drift_banner.text())

# The banner recolors on a theme switch rather than going stale.
drift_win.theme_choice = gui_theme.LIGHT
drift_win.apply_theme()
assert_true("the banner survives a theme switch and recolours",
            (not drift_win.drift_banner.isHidden())
            and gui_theme.PALETTES["light"]["error"] in drift_win.drift_banner.styleSheet())

print()
print("--- a torn-down slot keeps its last good readings ---")
#
# When the charger ends a run (a time or capacity limit) or a stop lands, it
# zeroes capacity, elapsed time and resistance at once. The card must not go
# from "1971 mAh" to "0 mAh" at the moment the number matters.

_good = _parse(gui.build_status_frame(1, 500, 3612, 1971, 17957, 22))
_phantom = _parse(gui.build_status_frame(1, 502, 3612, 0, 0, 0))   # latched echo
_stopped = _parse(gui.build_status_frame(1, 0, 3701, 0, 0, 0))     # after a stop
_normal_end = _parse(gui.build_status_frame(1, 0, 3701, 1971, 17957, 22))

fr, held = gui.card_frames(_phantom, "latched", _good)
assert_true("latched: every reading is the last good one",
            held and all(f is _good for f in fr.values()))
fr, held = gui.card_frames(_stopped, "finished", _good)
assert_true("finished after a stop: voltage and current stay live",
            held and fr["Volts"] is _stopped and fr["Current"] is _stopped)
assert_true("...capacity and resistance are the last good ones",
            fr["Capacity"] is _good and fr["Resistance"] is _good)
fr, held = gui.card_frames(_normal_end, "finished", _good)
assert_true("a normal end keeps its own counters, so nothing is held",
            not held and all(f is _normal_end for f in fr.values()))
for _s in ("running", "idle", "armed", "starting"):
    fr, held = gui.card_frames(_stopped, _s, _good)
    assert_true(f"a {_s} slot always shows its live frame",
                not held and all(f is _stopped for f in fr.values()))
fr, held = gui.card_frames(_phantom, "latched", None)
assert_true("with no last good frame there is nothing to hold",
            not held and all(f is _phantom for f in fr.values()))
assert_true("counters_zeroed needs BOTH counters at 0",
            gui.counters_zeroed(_phantom)
            and not gui.counters_zeroed(_good)
            and not gui.counters_zeroed(
                _parse(gui.build_status_frame(1, 500, 3700, 0, 5, 20))))

_lc = gui.SlotCard(1, "TEST-01", gui_theme.DARK)
_lc.add(_Sample(wall_time=_dt(2026, 10, 28), elapsed_s=1.0, slot=1,
                status=_phantom, drift_severity="ok",
                note="transition_artifact"), "latched", _good)
assert_true("the latched card shows 1971 mAh, not 0",
            _lc.values["Capacity"].text() == "1971 mAh",
            _lc.values["Capacity"].text())
assert_true("...and the last good resistance",
            _lc.values["Resistance"].text() == "22 mΩ",
            _lc.values["Resistance"].text())
assert_true("...and the last good current, not the phantom echo",
            _lc.values["Current"].text() == "500 mA",
            _lc.values["Current"].text())
assert_true("...with a plain caption saying why",
            (not _lc.readings_note.isHidden())
            and "charger ended this run" in _lc.readings_note.text(),
            _lc.readings_note.text())
assert_true("...and the held readings are dimmed",
            _lc._held == {"Volts", "Current", "Capacity", "Resistance"}
            and gui_theme.PALETTES["dark"]["text_dim"]
            in _lc.values["Capacity"].styleSheet())

_lc.add(_Sample(wall_time=_dt(2026, 10, 28), elapsed_s=2.0, slot=1,
                status=_stopped, drift_severity="ok", note=""),
        "finished", _good)
assert_true("after a stop: capacity is still the last good one",
            _lc.values["Capacity"].text() == "1971 mAh")
assert_true("...but current is the live 0 and voltage the live rebound",
            _lc.values["Current"].text() == "0 mA"
            and _lc.values["Volts"].text() == "3.701",
            _lc.values["Current"].text() + " " + _lc.values["Volts"].text())
assert_true("...and only capacity and resistance are dimmed",
            _lc._held == {"Capacity", "Resistance"})
assert_true("...with its own caption",
            "last readings" in _lc.readings_note.text(),
            _lc.readings_note.text())

_lc.add(_Sample(wall_time=_dt(2026, 10, 28), elapsed_s=3.0, slot=1,
                status=_good, drift_severity="ok", note=""),
        "running", _good)
assert_true("a running slot shows live readings, undimmed, with no caption",
            _lc.values["Capacity"].text() == "1971 mAh" and _lc._held == set()
            and _lc.readings_note.isHidden())

# The wiring: on_sample must hand the capture's last good frame to the card.
from types import SimpleNamespace as _SNS
_prev_cap = drift_ctl.cap
drift_ctl.cap = _SNS(finished=set(), torn_down={1}, measuring_ir=set(),
                     final_by_slot={1: _good}, latest={1: _phantom}, gaps=[],
                     stale_link_warning=None, reconnect_attempts=0)
drift_win.on_sample(_Sample(wall_time=_dt(2026, 10, 28), elapsed_s=5.0, slot=1,
                            status=_phantom, drift_severity="ok",
                            note="transition_artifact"))
assert_true("on_sample passes the capture's last good frame to a latched card",
            drift_win.cards[1].state == "latched"
            and drift_win.cards[1].values["Capacity"].text() == "1971 mAh",
            drift_win.cards[1].values["Capacity"].text())
drift_ctl.cap = _SNS(finished=set(), torn_down=set(), measuring_ir=set(),
                     final_by_slot={}, latest={1: _good}, gaps=[],
                     stale_link_warning=None, reconnect_attempts=0)
drift_win.on_sample(_Sample(wall_time=_dt(2026, 10, 28), elapsed_s=6.0, slot=1,
                            status=_good, drift_severity="ok", note=""))
assert_true("a capture with no last good frame still renders live",
            drift_win.cards[1].values["Capacity"].text() == "1971 mAh"
            and drift_win.cards[1]._held == set())
drift_ctl.cap = _prev_cap

# A run that ENDED WHILE THE LINK WAS DOWN: the card
# shows the last readings received, all dimmed, and says the numbers may be
# short - not the "charger ended this run" caption, which would read as a
# complete result. Mirrors stopped_by_user; the capture decides, the card shows.
fr, held = gui.card_frames(_stopped, "finished", _good, True)
assert_true("an unwatched end shows EVERY reading as the last good one",
            held and all(f is _good for f in fr.values()))
fr, held = gui.card_frames(_phantom, "latched", _good, True)
assert_true("...latched or finished alike",
            held and all(f is _good for f in fr.values()))
fr, held = gui.card_frames(_good, "running", _good, True)
assert_true("...but a running slot is never held, whatever the flag says",
            not held and all(f is _good for f in fr.values()))
fr, held = gui.card_frames(_stopped, "finished", None, True)
assert_true("...and with no last good frame there is nothing to hold",
            not held and all(f is _stopped for f in fr.values()))

_wc = gui.SlotCard(1, "TEST-01", gui_theme.DARK)
_wc.add(_Sample(wall_time=_dt(2026, 11, 1), elapsed_s=1.0, slot=1,
                status=_stopped, drift_severity="ok", note=""),
        "finished", _good, unwatched_end=True)
assert_true("the unwatched card shows the last good capacity and voltage, "
            "not the live rebound",
            _wc.values["Capacity"].text() == "1971 mAh"
            and _wc.values["Volts"].text() == "3.612",
            _wc.values["Capacity"].text() + " " + _wc.values["Volts"].text())
assert_true("...every reading dimmed",
            _wc._held == {"Volts", "Current", "Capacity", "Resistance"})
assert_true("...with a caption that says the run ended while the link was down",
            (not _wc.readings_note.isHidden())
            and "ended while the link was down" in _wc.readings_note.text()
            and "Last readings received" in _wc.readings_note.text(),
            _wc.readings_note.text())
_wc.add(_Sample(wall_time=_dt(2026, 11, 1), elapsed_s=2.0, slot=1,
                status=_phantom, drift_severity="ok",
                note="transition_artifact"),
        "latched", _good, stopped_by_user=True, unwatched_end=True)
assert_true("the unwatched caption outranks 'charger ended' and 'stopped by you'",
            "ended while the link was down" in _wc.readings_note.text()
            and "charger ended" not in _wc.readings_note.text()
            and "Stopped by you" not in _wc.readings_note.text(),
            _wc.readings_note.text())
_wc.add(_Sample(wall_time=_dt(2026, 11, 1), elapsed_s=3.0, slot=1,
                status=_good, drift_severity="ok", note=""),
        "running", _good, unwatched_end=True)
assert_true("a card that is running again drops the caption",
            _wc.readings_note.isHidden() and _wc._held == set())

# The wiring: on_sample asks the capture whether this slot's end was unwatched.
_prev_cap = drift_ctl.cap
drift_ctl.cap = _SNS(finished={1}, torn_down=set(), measuring_ir=set(),
                     final_by_slot={1: _good}, latest={1: _stopped}, gaps=[],
                     stale_link_warning=None, reconnect_attempts=0,
                     unwatched_end=lambda s: s == 1)
drift_win.on_sample(_Sample(wall_time=_dt(2026, 11, 1), elapsed_s=7.0, slot=1,
                            status=_stopped, drift_severity="ok", note=""))
assert_true("on_sample passes the capture's unwatched-end finding to the card",
            "ended while the link was down"
            in drift_win.cards[1].readings_note.text()
            and drift_win.cards[1].values["Volts"].text() == "3.612",
            drift_win.cards[1].readings_note.text())
drift_ctl.cap = _SNS(finished={1}, torn_down=set(), measuring_ir=set(),
                     final_by_slot={1: _good}, latest={1: _stopped}, gaps=[],
                     stale_link_warning=None, reconnect_attempts=0)
drift_win.on_sample(_Sample(wall_time=_dt(2026, 11, 1), elapsed_s=8.0, slot=1,
                            status=_stopped, drift_severity="ok", note=""))
assert_true("a capture that knows nothing of it leaves the old caption",
            "ended while the link was down"
            not in drift_win.cards[1].readings_note.text())
drift_ctl.cap = _prev_cap

# The chart: the capacity curve ends where the run did, not at zero.
_ch2 = gui.SharedChart([1], gui_theme.DARK)
for _i, _stt in enumerate((_good, _phantom, _phantom, _stopped)):
    _ch2.add(_Sample(wall_time=_dt(2026, 10, 28), elapsed_s=float(_i), slot=1,
                     status=_stt, drift_severity="ok", note=""))
assert_true("the capacity curve holds the last real value after teardown",
            _ch2.data[1]["capacity"] == [1971.0, 1971.0, 1971.0, 1971.0],
            str(_ch2.data[1]["capacity"]))
assert_true("...while current and voltage still plot what the charger reports",
            _ch2.data[1]["current"] == [500.0, 502.0, 502.0, 0.0])
_ch3 = gui.SharedChart([1], gui_theme.DARK)
_ch3.add(_Sample(wall_time=_dt(2026, 10, 28), elapsed_s=0.0, slot=1,
                 status=_stopped, drift_severity="ok", note=""))
assert_true("a slot that never ran still plots its zero (nothing to hold)",
            _ch3.data[1]["capacity"] == [0.0])

# Both axes name their unit and never add an SI prefix: pyqtgraph would
# otherwise label a 2000 mA axis "kmA" and a 900 mAh one "kmAh".
assert_true("the chart's value axis does not add an SI prefix",
            _ch3.plot.getAxis("left").autoSIPrefix is False)
assert_true("...nor does its time axis",
            _ch3.plot.getAxis("bottom").autoSIPrefix is False)

print()
print("--- the All Stop button ---")
#
# The one deliberately unguarded action: halt every slot, whatever started
# it. Always behind a confirmation, and injected into the live session when a
# capture is running (a second BLE connection cannot exist). The demo source
# honors the event, so the whole chain is provable offline: request -> halt
# -> slots go idle -> finished-detection ends the capture "done" - meaning
# the CSV records the halt itself.

as_tmp = tempfile.mkdtemp()
as_bridge = gui.SampleBridge()
as_status, as_stopped = [], []
as_bridge.status.connect(as_status.append)
as_bridge.stopped.connect(as_stopped.append)
as_controller = gui.CaptureController(
    as_bridge, gui.DemoSource([1], interval=0.05), [1], "allstop", as_tmp)
as_window = gui.MainWindow(as_controller, [1], gui_theme.SYSTEM)

assert_true("the window has an All Stop button",
            as_window.all_stop_button.text() == "ALL STOP")

as_controller.start()
for _ in range(100):
    if as_controller.cap is not None and as_controller.cap.rows >= 3:
        break
    time.sleep(0.02)

as_window.confirm_all_stop = lambda: False
as_window.on_all_stop_clicked()
assert_true("declining the All Stop does nothing",
            not as_controller._all_stop.is_set()
            and as_controller.is_running)

as_window.confirm_all_stop = lambda: True
as_window.on_all_stop_clicked()
for _ in range(300):    # halt -> idle frames -> finished -> "done"
    if not as_controller.is_running:
        break
    time.sleep(0.02)
as_controller.stop()
app.processEvents()

assert_true("the capture ended on its own after the All Stop",
            as_stopped == ["done"], str(as_stopped))
assert_true("the halt was announced",
            any("ALL STOP" in m for m in as_status), str(as_status[-3:]))
assert_true("the slot was seen going idle (halt recorded in the data)",
            as_controller.cap.finished == {1},
            str(as_controller.cap.finished))

print()
print("--- the demo source honors per-slot start injection ---")
# With an inject_queue the demo mirrors the real independence flow: every slot
# starts IDLE and a pushed StartInjection brings one alive on its own clock -
# the same queue the live session drains, so arm -> start demos with no BLE.
import queue as _dq                 # noqa: E402
import threading as _dthr           # noqa: E402
from capture import StartInjection as _SI  # noqa: E402

di_tmp = tempfile.mkdtemp()
di_cap = gui.Capture(name="demoinj", slots=[1, 2], outdir=di_tmp, baseline=None)
di_q = _dq.Queue()
di_stop = _dthr.Event()
di_src = gui.DemoSource([1, 2], interval=0.02, auto_run=False)
di_thread = _dthr.Thread(
    target=di_src.run,
    args=(di_cap, di_stop, lambda *a: None, None, di_q), daemon=True)
di_thread.start()

time.sleep(0.1)                     # idle frames flow, but nothing draws current
idle_before = all(di_cap.latest.get(s) is not None
                  and di_cap.latest[s].current_ma == 0 for s in (1, 2))

di_q.put(_SI.build(1, _good_req(500)))
for _ in range(200):
    if di_cap.latest.get(1) and di_cap.latest[1].current_ma == 500:
        break
    time.sleep(0.02)
di_stop.set()
di_thread.join(timeout=2)

assert_true("every slot reports as a present cell but idle until started",
            idle_before, str({s: di_cap.latest.get(s) for s in (1, 2)}))
assert_true("an injected start brings that slot alive in the demo",
            di_cap.latest.get(1) is not None
            and di_cap.latest[1].current_ma == 500,
            str(di_cap.latest.get(1)))
assert_true("a slot never started stays idle in the demo",
            di_cap.latest.get(2) is None
            or di_cap.latest[2].current_ma == 0,
            str(di_cap.latest.get(2)))
di_cap.close()

print()
print("--- controller.start_slot: arm -> inject -> running -> disarm ---")
# The full per-slot start path through the controller: refuse when unarmed or
# not connected, then arm, start the monitor, inject the config, watch the slot
# come alive, and confirm slot_state retires the arming once it is running.
ss_tmp = tempfile.mkdtemp()
ss_ctl = gui.CaptureController(
    gui.SampleBridge(), gui.DemoSource([1, 2], interval=0.02, auto_run=False),
    [1, 2], "startslot", ss_tmp)

assert_true("start_slot refuses an unarmed slot",
            "not armed" in ss_ctl.start_slot(1), ss_ctl.start_slot(1))
ss_ctl.arm(1, _good_req(500))
assert_true("start_slot refuses before the monitor is connected",
            gui.NOT_CONNECTED in ss_ctl.start_slot(1), ss_ctl.start_slot(1))
assert_true("...and queues nothing for later",
            ss_ctl._inject_queue.empty() and 1 in ss_ctl._armed)

ss_ctl.start()
for _ in range(200):        # wait for the first idle frames (a present cell)
    if ss_ctl.is_running and ss_ctl.cap and ss_ctl.cap.latest.get(1):
        break
    time.sleep(0.02)

assert_true("an armed-but-idle slot reads 'armed'",
            ss_ctl.slot_state(1) == "armed", ss_ctl.slot_state(1))
ss_msg = ss_ctl.start_slot(1)
assert_true("start_slot reports the config was injected",
            "injected" in ss_msg, ss_msg)

for _ in range(200):
    if ss_ctl.cap.latest.get(1) and ss_ctl.cap.latest[1].current_ma == 500:
        break
    time.sleep(0.02)
ss_state = ss_ctl.slot_state(1)     # observing "running" also disarms
ss_state2 = ss_ctl.slot_state(2)    # before the stop: afterward it is
                                    # "disconnected", like every slot

ss_ctl.stop()
for _ in range(100):
    if not ss_ctl.is_running:
        break
    time.sleep(0.02)

assert_true("the injected slot is now running",
            ss_state == "running",
            f"{ss_state}, {ss_ctl.cap.latest.get(1)}")
assert_true("slot_state retired the arming once the slot was running",
            1 not in ss_ctl._armed, str(ss_ctl._armed))
assert_true("a slot never started stays idle through the controller",
            ss_state2 == "idle", ss_state2)
assert_true("...and once the session has ended every slot reads disconnected",
            ss_ctl.slot_state(2) == "disconnected", ss_ctl.slot_state(2))

print()
print("--- controller.stop_slot: stop one slot, the other runs on ---")
# The per-slot stop path: with two slots running, stop_slot halts only the
# target - it goes to 0 mA and reads finished, while the other keeps running.
sp_tmp = tempfile.mkdtemp()
sp_ctl = gui.CaptureController(
    gui.SampleBridge(), gui.DemoSource([1, 2], interval=0.02, auto_run=False),
    [1, 2], "stopslot", sp_tmp)

assert_true("stop_slot refuses before the monitor is connected",
            gui.NOT_CONNECTED in sp_ctl.stop_slot(1), sp_ctl.stop_slot(1))
assert_true("...and queues nothing for later", sp_ctl._stop_queue.empty())

sp_ctl.start()
for _ in range(200):
    if sp_ctl.is_running and sp_ctl.cap and sp_ctl.cap.latest.get(1):
        break
    time.sleep(0.02)

assert_true("stop_slot refuses a slot that is not running",
            "not running" in sp_ctl.stop_slot(1), sp_ctl.stop_slot(1))

# start both slots
sp_ctl.arm(1, _good_req(500))
sp_ctl.arm(2, _good_req(700))
sp_ctl.start_slot(1)
sp_ctl.start_slot(2)
for _ in range(200):
    if (sp_ctl.cap.latest.get(1) and sp_ctl.cap.latest[1].current_ma == 500
            and sp_ctl.cap.latest.get(2) and sp_ctl.cap.latest[2].current_ma == 700):
        break
    time.sleep(0.02)

sp_msg = sp_ctl.stop_slot(1)
assert_true("stop_slot reports the per-slot stop", "stopping slot 1" in sp_msg,
            sp_msg)
for _ in range(300):        # 0 mA, then enough idle frames to read finished
    if 1 in sp_ctl.cap.finished:
        break
    time.sleep(0.02)
sp_one = sp_ctl.cap.latest.get(1)
sp_two = sp_ctl.cap.latest.get(2)

sp_ctl.stop()
for _ in range(100):
    if not sp_ctl.is_running:
        break
    time.sleep(0.02)

assert_true("the stopped slot went to 0 mA",
            sp_one is not None and sp_one.current_ma == 0, str(sp_one))
assert_true("the other slot kept running",
            sp_two is not None and sp_two.current_ma == 700, str(sp_two))
assert_true("the stopped slot is seen finished",
            1 in sp_ctl.cap.finished, str(sp_ctl.cap.finished))

print()
print("--- group control cards ---")
# structure: a group creates a titled, colored control card; no group, no row.
gc_ctl = gui.CaptureController(gui.SampleBridge(), gui.DemoSource([1, 2, 3, 4]),
                               [1, 2, 3, 4], "groupcards", tempfile.mkdtemp())
gc_win = gui.MainWindow(gc_ctl, [1, 2, 3, 4], gui_theme.DARK)
assert_true("no group row until a group exists",
            gc_win.group_row_container.isHidden())
gc_ctl.arm(1, _good_req()); gc_ctl.arm(4, _good_req())
gc_ctl.group([1, 4])
gc_win._refresh_groups()
gid = gc_ctl.group_id_of(1)
assert_true("a group creates one control card",
            set(gc_win.group_cards) == {gid})
gcard = gc_win.group_cards[gid]
assert_true("the group card is titled 'Group A'", gcard.title() == "Group A")
assert_true("the group card lists its members (non-adjacent 1 and 4)",
            "1" in gcard.members_label.text()
            and "4" in gcard.members_label.text(), gcard.members_label.text())
assert_true("the group card wears the group color",
            gui_theme.group_color(gid - 1, gui_theme.DARK) in gcard.styleSheet())
assert_true("the group row is shown once a group exists",
            not gc_win.group_row_container.isHidden())
gc_win._refresh_group_states()
assert_true("group Start enabled when a member is armed, Stop disabled",
            gcard.start_button.isEnabled() and not gcard.stop_button.isEnabled())

# ungroup dissolves the group, removes its card, keeps the slots' configs
gcard.ungroup_requested.emit(gid)
app.processEvents()
assert_true("ungroup removes the group card and hides the row",
            gc_win.group_cards == {} and gc_win.group_row_container.isHidden())
assert_true("ungroup leaves the slots armed",
            gc_ctl.slot_request(1) is not None
            and gc_ctl.slot_request(4) is not None)

# start_group / stop_group fan out to the members over a live (demo) session
gsi_ctl = gui.CaptureController(
    gui.SampleBridge(), gui.DemoSource([1, 2], interval=0.02, auto_run=False),
    [1, 2], "groupstart", tempfile.mkdtemp())
gsi_ctl.arm(1, _good_req(500)); gsi_ctl.arm(2, _good_req(500))
gsi_ctl.group([1, 2])
gsi_gid = gsi_ctl.group_id_of(1)
gsi_ctl.start()
for _ in range(200):
    if gsi_ctl.is_running and gsi_ctl.cap and gsi_ctl.cap.latest.get(1):
        break
    time.sleep(0.02)
gsi_ctl.start_group(gsi_gid)
for _ in range(200):
    if all(gsi_ctl.cap.latest.get(s) and gsi_ctl.cap.latest[s].current_ma == 500
           for s in (1, 2)):
        break
    time.sleep(0.02)
both_running = all(gsi_ctl.slot_state(s) == "running" for s in (1, 2))
gsi_ctl.stop_group(gsi_gid)
for _ in range(300):
    if all(s in gsi_ctl.cap.finished for s in (1, 2)):
        break
    time.sleep(0.02)
both_stopped = all(gsi_ctl.cap.latest[s].current_ma == 0 for s in (1, 2))
gsi_ctl.stop()
for _ in range(100):
    if not gsi_ctl.is_running:
        break
    time.sleep(0.02)
assert_true("group Start ran every armed member", both_running)
assert_true("group Stop halted every running member", both_stopped)
_gruns = [r for r in gsi_ctl.cap.runs if r.ours]
assert_true("a group start is recorded as ONE group in each member's run",
            len(_gruns) == 2 and _gruns[0].group is not None
            and _gruns[0].group is _gruns[1].group
            and _gruns[0].group.slots == (1, 2),
            str([r.group for r in _gruns]))
assert_true("...whose folders share its timestamp",
            _gruns[0].dir.name[:15] == _gruns[1].dir.name[:15],
            str([r.dir.name for r in _gruns]))
_gm = slot_run_meta(gsi_ctl.cap, 1)
assert_true("...and meta.json names the group and says the run was stopped",
            _gm["group"]["slots"] == [1, 2] and _gm["end_reason"] == "stopped"
            and _gm["config"]["current_ma"] == 500,
            f"{_gm['group']} {_gm['end_reason']}")

# With no capture running, the button falls back to the standalone path -
# the same stop_all_mode as `capture.py --stop-all`.
standalone_called = {}

async def fake_stop_all_mode(fresh=True, log=None):
    standalone_called["fresh"] = fresh
    if log:
        log("fake: all slots idle")
    return 0

real_stop_all_mode = gui.stop_all_mode
gui.stop_all_mode = fake_stop_all_mode
try:
    # A REAL (Bluetooth) source that has no capture running. The demo source
    # must never take this path - see the --demo check at the end of the file.
    as_ble_bridge = gui.SampleBridge()
    as_ble_bridge.status.connect(as_status.append)
    as_ble_controller = gui.CaptureController(
        as_ble_bridge, gui.BleSource(), [1], "as_ble", tempfile.mkdtemp())
    as_ble_controller.all_stop()
    for _ in range(100):
        if standalone_called:
            break
        time.sleep(0.02)
    app.processEvents()
    assert_true("with no capture running, the standalone path runs",
                standalone_called.get("fresh") is True,
                str(standalone_called))
    assert_true("and reports completion",
                any("ALL STOP complete" in m for m in as_status),
                str(as_status[-3:]))
finally:
    gui.stop_all_mode = real_stop_all_mode

print()
print("--- stopping a live capture asks first ---")
#
# Stopping costs continuity, not data: samples are already flushed, but a
# reattach writes a NEW run directory, splitting the curve across files. So
# both the Stop button and the window's X confirm while a capture is running.
# confirm_stop is substituted here because a real dialog would block a
# headless test - everything around it is the real path, including
# event.ignore() cancelling the close.

conf_tmp = tempfile.mkdtemp()
conf_bridge = gui.SampleBridge()
conf_controller = gui.CaptureController(
    conf_bridge, gui.DemoSource([1], interval=0.2), [1], "confirm", conf_tmp)
conf_window = gui.MainWindow(conf_controller, [1], gui_theme.SYSTEM)
conf_window.show()
conf_controller.start()
for _ in range(100):
    if conf_controller.is_running:
        break
    time.sleep(0.02)
assert_true("the capture reports itself running", conf_controller.is_running)

conf_window.confirm_stop = lambda: False
conf_window.on_stop_clicked()
assert_true("declining the Stop button leaves the capture running",
            conf_controller.is_running)
assert_true("the Stop button stays enabled after declining",
            conf_window.stop_button.isEnabled())

conf_window.close()
assert_true("declining the window close keeps it open",
            conf_window.isVisible() and conf_controller.is_running)

conf_window.confirm_stop = lambda: True
conf_window.close()
assert_true("accepting the close stops the capture",
            not conf_controller.is_running)
assert_true("and the window actually closed", not conf_window.isVisible())

confirmed_after_end = []
conf_window.confirm_stop = lambda: confirmed_after_end.append(1) or True
conf_window.on_stop_clicked()
assert_true("no nagging once the capture has already ended",
            confirmed_after_end == [],
            "the dialog fired with nothing left to protect")


# --------------------------------------------------------------------------
print()
print("--- slot temperature: the card, never a ranking ---")
#
# The load-bearing rule: nothing ranks, compares, flags or colors one slot
# against another, because a middle bay reads ~1 C hotter by position alone -
# the size of a failing cell's signal.

tv = gui.temp_view
v, d, g = tv("running", 37, 5.0, 37)
assert_true("running, fresh: the whole-degree reading, no age", v == "37 °C"
            and not g, v)
v, d, g = tv("running", 37, 130.0, 37)
assert_true("running, stale: the reading WITH its age", v == "37 °C · 2 min ago", v)
v, _d, _g = tv("running", 37, 2 * gui.TEMP_INTERVAL_S, 37)
assert_true("one dropped reply already shows its age (never hidden)",
            "ago" in v, v)
v, d, g = tv("running", None, None, None)
assert_true("running before the first reading: a dash", v == "—")
for idle_state in ("idle", "empty", "unknown", "armed"):
    v, d, g = tv(idle_state, None, None, None)
    assert_true(f"{idle_state}: a dash (reported only while running)",
                v == "—" and d == "" and not g, f"{v!r} {d!r}")
v, d, g = tv("finished", 36, 900.0, 38)
assert_true("finished: the last reading, grayed, with the run's peak",
            v == "36 °C" and g and "Peak 38 °C" in d, f"{v!r} {d!r} {g}")
v, d, g = tv("finished", 36, 900.0, 38, "F")
assert_true("°F is display only: 36 C shows as 97 °F, peak as 100 °F",
            v == "97 °F" and "Peak 100 °F" in d, f"{v!r} {d!r}")
_v, d, _g = tv("finished", 45, 10.0, 45, trip_c=45, cutoff_c=45,
               stopped_by_cutoff=True)
assert_true("a cutoff stop is said in plain words",
            "Stopped by your cutoff at 45 °C" in d, d)
_v, d, _g = tv("latched", 60, 10.0, 70,
               charger_alert="alert code 7")
assert_true("an alert with no proven name is shown as its code, not guessed at",
            "Stopped by charger: alert code 7" in d, d)
_v, d, _g = tv("finished", 36, 10.0, 36, charger_alert="connection break")
assert_true("a pulled cell is said in plain words",
            "Stopped by charger: the cell lost contact" in d, d)
_v, d, _g = tv("running", 33, 400.0, 33, cutoff_c=45, blind=True)
assert_true("a blind cutoff says so", "Cutoff blind: no temperature reading" in d, d)
_v, d, _g = tv("running", 46, 5.0, 46, cutoff_c=45, trip_c=46)
assert_true("a trip on a run not started here: warned, NOT stopped",
            "not stopped" in d and "not started here" in d, d)
_v, d, _g = tv("running", 30, 5.0, 30, cutoff_c=45)
assert_true("an armed cutoff says it lives only while connected",
            "Cutoff 45 °C (while connected)" in d, d)
_v, d, _g = tv("running", None, None, None,
               note="temperature available once the firmware is checked")
assert_true("no temperature is never a silent blank on a running card",
            "once the firmware is checked" in d, d)

# Structural: temp_view is given ONE slot's facts and nothing else - there is
# no parameter through which another slot's reading could arrive.
import inspect                                                 # noqa: E402
_params = set(inspect.signature(gui.temp_view).parameters)
assert_true("temp_view has no way to see another slot",
            not any(w in p for p in _params
                    for w in ("slots", "others", "all_", "rank", "mean")),
            str(sorted(_params)))

# The color of a reading never depends on its value: four cards, four very
# different readings, identical styling.
_cards = [gui.SlotCard(s, None, gui_theme.DARK) for s in (1, 2, 3, 4)]
for _c, _t in zip(_cards, (31, 36, 44, 52)):
    _c.set_temp(*tv("running", _t, 5.0, _t))
_styles = {c.temp_value.styleSheet() for c in _cards}
assert_true("four slots, four readings, ONE style - no color ranking",
            len(_styles) == 1, str(_styles))
assert_true("the middle-bay note is on EVERY card, not singled out",
            all("Middle bays read about 1 °C warmer" in c.bay_info.toolTip()
                for c in _cards))
assert_true("labeled 'Slot temp', not 'Cell temp'",
            all(c.temp_header.text() == "Slot temp" for c in _cards))
assert_true("the tooltip explains the sensor honestly",
            "about 5 °C above the cell" in _cards[0].temp_header.toolTip()
            and "(measured against a probe taped to one cell)"
            in _cards[0].temp_header.toolTip()
            and "Uncalibrated" in _cards[0].temp_header.toolTip())
_cards[0].set_temp(*tv("finished", 36, 900.0, 38))
assert_true("a finished reading grays; a live one does not",
            _cards[0].temp_value.styleSheet() != _cards[1].temp_value.styleSheet())

print()
print("--- the temperature chart: a staircase, bridged and broken honestly ---")
_nan = float("nan")
xs, ys = gui.temp_staircase([(0, 30), (30, 31), (60, 32)], 30)
assert_true("readings draw as steps: each value held until the next",
            xs == [0, 30, 30, 60, 60] and ys == [30, 30, 31, 31, 32],
            f"{xs} {ys}")
xs, ys = gui.temp_staircase([(0, 30), (60, 31)], 30)
assert_true("a single missed reply (a 60 s gap) is BRIDGED",
            all(y == y for y in ys), f"{xs} {ys}")
xs, ys = gui.temp_staircase([(0, 30), (300, 33)], 30)
assert_true("a real gap BREAKS the line (a NaN)",
            any(y != y for y in ys) and ys[-1] == 33, f"{xs} {ys}")
xs, ys = gui.temp_staircase([(0, 100 - 64)], 30, "F")
assert_true("the chart converts only for display", ys == [96.8], str(ys))

_ch = gui.SharedChart([1], gui_theme.DARK)
assert_true("the chart has a fourth metric button for slot temp",
            gui.TEMP_METRIC in _ch.buttons
            and _ch.buttons[gui.TEMP_METRIC].text() == "slot temp")
_ch.add_temp(1, 0, 30)
_ch.add_temp(1, 30, 31)
_ch.set_metric(gui.TEMP_METRIC)
assert_true("switching to it draws the staircase in °C",
            list(_ch.curves[1].getData()[1]) == [30, 30, 31]
            and "°C" in _ch.plot.getAxis("left").labelUnits, "")
_ch.set_unit("F")
assert_true("and redraws in °F when the setting changes",
            list(_ch.curves[1].getData()[1])[0] == 86.0)

print()
print("--- the Fahrenheit setting lives in settings.ini, display only ---")
assert_true("default is °C", gui.load_temp_unit() == "C")
assert_true("°F saves", gui.save_temp_unit("F") and gui.load_temp_unit() == "F")
assert_true("...into settings.ini's [display] section",
            "temperature_unit = F" in runfiles.SETTINGS_PATH.read_text())
_cap_settings.remember_firmware("AA:00:00:00:00:01", "2.02")
assert_true("and a firmware record written after it keeps the unit",
            gui.load_temp_unit() == "F"
            and _cap_settings.remembered_firmware("AA:00:00:00:00:01") == "2.02")
gui.save_temp_unit("C")
_sd = gui.SettingsDialog(None, gui_theme.DARK, False, "F")
assert_true("the settings dialog offers the unit, preselected",
            _sd.temp_unit() == "F")

print()
print("--- the custom temperature cutoff in Configure ---")
from protocol import Chemistry as _C3                         # noqa: E402
tc_dlg = gui.ConfigDialog(None, 1, [1], chemistry=_C3.LIION)
_type_in(tc_dlg.capacity_edit, "3000")
assert_true("the cutoff field is blank (off) by default",
            tc_dlg.temp_cutoff_edit.text() == ""
            and tc_dlg.build_request().temp_cutoff_c is None)
assert_true("its wording says it is active only while connected",
            "Active only while this app is connected"
            in tc_dlg.temp_cutoff_note.text()
            and "Active only while this app is connected"
            in tc_dlg.temp_cutoff_edit.toolTip())
tc_dlg.temp_cutoff_edit.setText("45")
assert_true("45 becomes temp_cutoff_c = 45",
            tc_dlg.build_request().temp_cutoff_c == 45)
tc_dlg.temp_cutoff_edit.setText("70")
assert_true("70 is refused (the charger's own limit), with the range",
            tc_dlg.build_request() is None
            and "30-65 °C" in tc_dlg.error_label.text(),
            tc_dlg.error_label.text())
tc_f = gui.ConfigDialog(None, 1, [1], chemistry=_C3.LIION, temp_unit="F")
_type_in(tc_f.capacity_edit, "3000")
tc_f.temp_cutoff_edit.setText("113")
assert_true("in °F, 113 is stored as 45 C",
            tc_f.build_request().temp_cutoff_c == 45)
tc_f.temp_cutoff_edit.setText("200")
assert_true("in °F the refusal quotes the °F range",
            tc_f.build_request() is None and "86-149 °F" in tc_f.error_label.text(),
            tc_f.error_label.text())
_with_tc = gui.RunRequest(mode=gui.Mode.DISCHARGE, chemistry=_C3.LIION,
                          current_ma=500, capacity_mah=3000,
                          safety_timer_min=240, cutoff_mv=3000,
                          temp_cutoff_c=45)
tc_re = gui.ConfigDialog(None, 1, [1], chemistry=_C3.LIION, existing=_with_tc)
assert_true("reopening Configure shows the saved cutoff",
            tc_re.temp_cutoff_edit.text() == "45")
assert_true("the armed card's config line carries it",
            "stop at 45 °C" in gui.describe_config(_with_tc, full=True))

print()
print("--- temperature through the controller, the demo, and the cutoff ---")
tp_tmp = tempfile.mkdtemp()
tp_ctl = gui.CaptureController(
    gui.SampleBridge(), gui.DemoSource([1, 2], interval=0.02, auto_run=False),
    [1, 2], "temps", tp_tmp)
tp_seen = []
tp_ctl.subscribe(lambda smp: tp_seen.append(smp))
tp_ctl.arm(1, _with_tc)                     # cutoff 45: the demo never gets there
_cut30 = gui.RunRequest(mode=gui.Mode.DISCHARGE, chemistry=_C3.LIION,
                        current_ma=500, capacity_mah=3000,
                        safety_timer_min=240, cutoff_mv=3000, temp_cutoff_c=30)
tp_ctl.arm(2, _cut30)                       # cutoff 30: the demo crosses it
tp_ctl.start()
assert_true("the GUI's capture polls temperature, every 30 s",
            tp_ctl.cap.temp_interval_s == 30.0)
for _ in range(200):
    if tp_ctl.cap.latest.get(1) and tp_ctl.cap.latest.get(2):
        break
    time.sleep(0.02)
tp_ctl.start_slot(1)
tp_ctl.start_slot(2)
for _ in range(400):
    if (2 in tp_ctl.cap.cutoff_stopped and tp_ctl.cap.temp_by_slot.get(1)
            and tp_ctl.cap.latest[2].current_ma == 0):
        break
    time.sleep(0.02)
view1 = tp_ctl.slot_temp_view(1)
tp_ctl.stop()
assert_true("the demo synthesises temperatures (for screenshots)",
            any(smp.slot_temp_c is not None for smp in tp_seen))
assert_true("the card view reads the running slot's temperature",
            view1[0].endswith("°C") and "Cutoff 45 °C" in view1[1], str(view1))
assert_true("the slot whose cutoff was crossed was STOPPED, alone",
            tp_ctl.cap.cutoff_stopped == {2}
            and tp_ctl.cap.latest[2].current_ma == 0
            and tp_ctl.cap.latest[1].current_ma > 0,
            f"{tp_ctl.cap.cutoff_stopped} {tp_ctl.cap.latest}")
assert_true("the trip reading met the cutoff (first reading >= 30)",
            tp_ctl.cap.cutoff_trips.get(2, 0) >= 30, str(tp_ctl.cap.cutoff_trips))
_meta_tp = slot_run_meta(tp_ctl.cap, 2)
_meta_tp1 = slot_run_meta(tp_ctl.cap, 1)
assert_true("slot 2's meta.json records its peak and the cutoff stop",
            "2" in _meta_tp["peak_slot_temp_c"]
            and _meta_tp["temp_cutoff_trips"]["2"]["action"] == "stopped"
            and _meta_tp["end_reason"] == "temp_cutoff",
            f"{_meta_tp['temp_cutoff_trips']} {_meta_tp['end_reason']}")
assert_true("...and slot 1's records no trip - each run describes itself",
            _meta_tp1["temp_cutoff_trips"] == {}
            and set(_meta_tp1["peak_slot_temp_c"]) <= {"1"},
            str(_meta_tp1["temp_cutoff_trips"]))

print()
print("--- a slot re-run in one session is not stuck at 'finished' ---")
rr_tmp = tempfile.mkdtemp()
rr_ctl = gui.CaptureController(
    gui.SampleBridge(), gui.DemoSource([1], interval=0.02, auto_run=False),
    [1], "rerun", rr_tmp)
rr_win = gui.MainWindow(rr_ctl, [1], gui_theme.DARK)
rr_ctl.subscribe(rr_win.on_sample)          # direct, so no event loop needed
rr_ctl.arm(1, _good_req(500))
rr_ctl.start()


def _wait(cond, n=400):
    for _ in range(n):
        if cond():
            return True
        time.sleep(0.02)
    return False


_wait(lambda: rr_ctl.cap.latest.get(1))
rr_ctl.start_slot(1)
_wait(lambda: rr_ctl.cap.latest[1].current_ma > 0)
rr_ctl.stop_slot(1)
first_done = _wait(lambda: 1 in rr_ctl.cap.finished)
rr_ctl.arm(1, _good_req(500))
rr_ctl.start_slot(1)
rerun_live = _wait(lambda: rr_ctl.cap.latest[1].current_ma > 0
                   and 1 not in rr_ctl.cap.finished)
_wait(lambda: 1 not in rr_win.finished, n=50)
rr_state = rr_ctl.slot_state(1)
rr_ctl.stop()
assert_true("the first run finished", first_done)
assert_true("re-starting it clears 'finished' in the capture",
            rerun_live, str(rr_ctl.cap.finished))
assert_true("...and in the window's mirror and the card",
            1 not in rr_win.finished and not rr_win.cards[1].finished,
            f"{rr_win.finished} {rr_win.cards[1].finished}")
assert_true("the re-run slot reads running", rr_state == "running", rr_state)

print()
print("--- a start the charger never took comes back ARMED ---")
from types import SimpleNamespace as _NS                        # noqa: E402
sf_ctl = gui.CaptureController(gui.SampleBridge(), gui.DemoSource([4]), [4],
                               "startfail", tempfile.mkdtemp())
sf_req = _good_req(500)
sf_ctl.arm(4, sf_req)
sf_ctl._armed.pop(4)                            # what start_slot does ...
sf_ctl._starting[4] = (time.monotonic(), sf_req)   # ... committing the config
sf_ctl.cap = _NS(latest={}, finished=set(), torn_down=set(),
                 measuring_ir=set(), start_failed={4})
_idle4 = _NS(current_ma=0, state="resting")
assert_true("the capture declared the start failed: the slot is ARMED again",
            sf_ctl.state_for(4, _idle4, False) == "armed")
assert_true("...with the same config, so one press of Start retries",
            sf_ctl.slot_request(4) is sf_req and 4 not in sf_ctl._starting)
assert_true("...and the failure is consumed, not re-applied on the next frame",
            sf_ctl.cap.start_failed == set())

print()
print("--- the honest line when the firmware is not read yet ---")
_real_cap = m4_ctl.cap
m4_ctl.cap = type("Cap", (), {
    "latest": {}, "finished": set(), "gaps": [], "torn_down": set(),
    "measuring_ir": set(), "csv_path": "m4.csv", "firmware_verified": False,
    "stale_link_warning": None,
    "temperature_note": lambda self: ("temperature available once the "
                                      "firmware is checked - the version is "
                                      "read only while every slot is idle")})()
m4_win.on_status("connected")
_h = m4_win.health_label.text()
assert_true("health line: not read yet, and WHY there is no temperature",
            "not read yet" in _h and "once the firmware is checked" in _h, _h)
m4_ctl.cap = _real_cap


# --------------------------------------------------------------------------

print()
print("--- the portable layout: startup guards ---")
import inspect as _inspect   # noqa: E402

_real_home = runfiles.HOME_DIR
_real_inside = runfiles.inside_temp_dir
_asked = []


def _ask_yes(kind, text):
    _asked.append((kind, text))
    return True


def _ask_no(kind, text):
    _asked.append((kind, text))
    return False


try:
    # An ordinary extracted folder. (Every scratch dir is under %TEMP%, so the
    # zip guard is switched off for the cases that are not about it.)
    runfiles.inside_temp_dir = lambda folder=None: False
    runfiles.HOME_DIR = Path(tempfile.mkdtemp())
    _asked.clear()
    got = gui.startup_storage(ask=_ask_yes)
    assert_true("a normal start asks nothing and records to runs\\",
                got == (runfiles.HOME_DIR / "runs", runfiles.HOME_DIR / "runs")
                and not _asked, str(got))
    assert_true("...and creates no profiles\\ folder (no such v1 feature)",
                not (runfiles.HOME_DIR / "profiles").exists())
    runfiles.write_setting("logging", {"runs_dir": "D:/flash/MC5000"})
    got = gui.startup_storage(ask=_ask_yes)
    assert_true("a configured logs folder is used, the local default kept "
                "as the fallback",
                got == (Path("D:/flash/MC5000"), runfiles.HOME_DIR / "runs"),
                str(got))
    runfiles.write_setting("logging", {"runs_dir": ""})
    assert_true("the demo records to demo_runs\\, never runs\\",
                gui.startup_storage(demo=True, ask=_ask_yes)[0].name
                == "demo_runs")

    # Not writable (Program Files, a read-only share).
    blocker = Path(tempfile.mkdtemp()) / "blocker"
    blocker.write_text("a file where the folder is", encoding="utf-8")
    runfiles.HOME_DIR = blocker / "SITREP91"
    _asked.clear()
    got = gui.startup_storage(ask=_ask_yes)
    docs = runfiles.documents_fallback_dir()
    assert_true("an unwritable folder is said plainly, offering Documents",
                _asked and _asked[0][0] == "documents"
                and "Cannot write" in _asked[0][1] and str(docs) in _asked[0][1],
                str(_asked))
    assert_true("...and says to move the folder, not Program Files",
                "Move the SITREP91 folder" in _asked[0][1]
                and "Program Files" in _asked[0][1], str(_asked))
    assert_true("...and accepting records there", got == (docs, docs), str(got))
    assert_true("...declining quits", gui.startup_storage(ask=_ask_no) is None)

    # The real box: each button says what it does.
    _box, _use, _quit = gui.documents_choice_box(_asked[0][1])
    assert_true("the box has exactly two buttons, named for their outcome",
                [b.text() for b in _box.buttons()]
                == ["Use Documents", "Quit SITREP91"],
                str([b.text() for b in _box.buttons()]))
    assert_true("...Use Documents is the default",
                _box.defaultButton() is _use)
    assert_true("...Esc and the window's X mean Quit, never Documents",
                _box.escapeButton() is _quit)

    # Running from inside the zip: refused, with what to do.
    runfiles.inside_temp_dir = _real_inside
    runfiles.HOME_DIR = (Path(tempfile.gettempdir()) / "Temp1_SITREP91.zip"
                         / "SITREP91")
    _asked.clear()
    got = gui.startup_storage(ask=_ask_yes)
    assert_true("inside the zip: refused, saying to extract it first",
                got is None and _asked and _asked[0][0] == "zip"
                and "Extract" in _asked[0][1], str(_asked))
finally:
    runfiles.HOME_DIR = _real_home
    runfiles.inside_temp_dir = _real_inside

print()
print("--- where the window opens ---")
from PySide6.QtCore import QRect as _QRect   # noqa: E402

_g = gui.initial_geometry(_QRect(0, 0, 1920, 1040))
assert_true("on a big screen: a few px in from the left, at the default size",
            (_g.left(), _g.top(), _g.width(), _g.height())
            == (gui.WINDOW_MARGIN, gui.WINDOW_MARGIN, 940, 700), str(_g))
assert_true("the default is taller than the old 620",
            gui.WINDOW_SIZE[1] > 620)
_g = gui.initial_geometry(_QRect(1920, 0, 1920, 1040))
assert_true("a second monitor: the margin is from ITS left edge",
            _g.left() == 1920 + gui.WINDOW_MARGIN, str(_g))
_avail = _QRect(0, 0, 800, 560)
_g = gui.initial_geometry(_avail)
assert_true("a small screen: shrunk to fit inside it, never off the edge",
            _g.left() >= 0 and _g.right() <= _avail.right()
            and _g.top() + _g.height() + gui.WINDOW_TITLEBAR
            <= _avail.top() + _avail.height(),
            str(_g))
_g = gui.initial_geometry(_QRect(0, 0, 10, 10))
assert_true("a tiny area still gives a valid size",
            _g.width() >= 1 and _g.height() >= 1, str(_g))
_pw = gui.MainWindow(gui.CaptureController(
    gui.SampleBridge(), gui.DemoSource([1], interval=0.02, auto_run=False),
    [1], "placement", Path(tempfile.mkdtemp())), [1], gui_theme.DARK)
_av = _pw.screen().availableGeometry()
assert_true("the real window opens at that position",
            _pw.x() == _av.left() + gui.WINDOW_MARGIN
            and _pw.y() == _av.top() + gui.WINDOW_MARGIN,
            f"{_pw.pos()} in {_av}")

_pw.show()
QApplication.processEvents()
_min_before = _pw.minimumSizeHint().width()
_long = gui.diagnose.NO_ADAPTER + gui.NO_BLUETOOTH_NEXT_STEP
_pw.status_label.setText(_long)
_pw.health_label.setText("logs: " + "C:\\" + "very_long_folder_name_" * 12)
QApplication.processEvents()
assert_true("the status line and the health line wrap",
            _pw.status_label.wordWrap() and _pw.health_label.wordWrap())
assert_true("a long message does not widen the window (it did, to ~1575 px)",
            _pw.minimumSizeHint().width() == _min_before,
            f"{_min_before} -> {_pw.minimumSizeHint().width()}")
_pw.close()

print()
print("--- settings live in settings.ini, not the registry ---")
gui_theme.save_choice(gui_theme.DARK)
gui_theme.save_show_console(True)
assert_true("the theme and console choice are written to settings.ini",
            runfiles.read_setting("display", "theme") == "dark"
            and runfiles.read_setting("display", "show_console") == "true")
gui_theme.save_choice(gui_theme.SYSTEM)
gui_theme.save_show_console(False)
assert_true("gui_theme no longer touches QSettings (the registry)",
            "QSettings(" not in _inspect.getsource(gui_theme))

print()
print("--- the logs folder in Settings ---")
_lf_dir = Path(tempfile.mkdtemp())
_lf_ctl = gui.CaptureController(
    gui.SampleBridge(), gui.DemoSource([1], interval=0.02, auto_run=False),
    [1], "logsfolder", _lf_dir / "a", fallback_dir=_lf_dir / "local")
_lf_win = gui.MainWindow(_lf_ctl, [1], gui_theme.DARK)
_dlg = gui.SettingsDialog(_lf_win, gui_theme.SYSTEM, runs_dir=_lf_dir / "a")
assert_true("the dialog shows the current logs folder",
            _dlg.runs_dir() == _lf_dir / "a", str(_dlg.runs_dir()))
_dlg.runs_edit.setText("relative_runs")
assert_true("a relative entry is taken from the app folder",
            _dlg.runs_dir() == runfiles.HOME_DIR / "relative_runs")
_dlg.runs_edit.setText(str(_lf_dir / "b"))
assert_true("it says a change applies from the next run",
            "next run" in _dlg.runs_note.text(), _dlg.runs_note.text())
_dlg.runs_edit.setText(str(Path("C:/") / ("very_long_folder_name_" * 8)))
assert_true("...and warns when files there would be too long for Excel",
            "218" in _dlg.runs_note.text(), _dlg.runs_note.text())
_lf_ctl.start()
for _ in range(200):
    if _lf_ctl.cap is not None and _lf_ctl.is_running:
        break
    time.sleep(0.02)
_lf_win.set_runs_dir(_lf_dir / "b")
assert_true("changing it reaches the running capture, for its next run",
            _lf_ctl.runs_dir == _lf_dir / "b"
            and _lf_ctl.cap.outdir == _lf_dir / "b")
assert_true("...and is saved in settings.ini",
            Path(runfiles.read_setting("logging", "runs_dir"))
            == (_lf_dir / "b").resolve())
runfiles.write_setting("logging", {"runs_dir": ""})

# A cell entered in Configure AFTER connecting is the run's cell: the capture
# holds its own copy of the cells, so the start has to carry it.
_lf_ctl.arm(1, _good_req(500))
_lf_ctl.cells = {1: "CELL-LATE"}
_lf_ctl.start_slot(1)
for _ in range(200):
    if _lf_ctl.cap.runs:
        break
    time.sleep(0.02)
_lf_run = _lf_ctl.cap.runs[0] if _lf_ctl.cap.runs else None
assert_true("a started run is named for the cell entered after connecting",
            _lf_run is not None and _lf_run.cell_id == "CELL-LATE"
            and "_S1_CELL-LATE_discharge" in _lf_run.dir.name
            and _lf_run.dir.parent == _lf_dir / "b",
            str(_lf_run and _lf_run.dir))
_lf_win.on_sample(_mk(1, 500, 3900))
assert_true("the health line names the runs folder, and says 'runs:'",
            f"runs: {_lf_dir / 'b'}" in _lf_win.health_label.text()
            and "logs:" not in _lf_win.health_label.text(),
            _lf_win.health_label.text())

print()
print("--- a write failure is shown on the slot's card ---")
_lf_run.writer = type("W", (), {"writerow": staticmethod(
    lambda row: (_ for _ in ()).throw(OSError(5, "The device is not ready")))})()
for _ in range(300):
    if _lf_ctl.file_warning(1):
        break
    time.sleep(0.02)
_lf_win.on_sample(_mk(1, 500, 3890))
_warn = _lf_win.cards[1].file_warning.text()
_lf_ctl.stop()
assert_true("the run carried on in the local default folder",
            _lf_run.dir.parent == _lf_dir / "local", str(_lf_run.dir))
assert_true("the card shows the warning, naming where the run went",
            not _lf_win.cards[1].file_warning.isHidden()
            and "not ready" in _warn and str(_lf_dir / "local") in _warn,
            _warn)

print()
print("--- a user-stopped card keeps its readings; a session end grays every card ---")
#
# Guards two failures: a run the card's Stop ended read "idle", 0 mA / 0 mAh /
# 0 mOhm with no caption, and after the bottom-bar Stop the cards froze
# claiming "running" with an enabled Stop.

_us_bridge = gui.SampleBridge()
_us_ctl = gui.CaptureController(
    _us_bridge, gui.BleSource(gui.SessionOptions()), [1, 2], "us",
    tempfile.mkdtemp())
_us_ctl.cap = _SNS(finished=set(), torn_down=set(), measuring_ir=set(),
                   user_stopped={1}, final_by_slot={1: _good, 2: _good},
                   latest={1: _stopped, 2: _good}, gaps=[],
                   stale_link_warning=None, reconnect_attempts=0)
assert_true("a slot the user stopped reads finished at once, not idle",
            _us_ctl.slot_state(1) == "finished", _us_ctl.slot_state(1))
assert_true("...while a slot nobody stopped is unaffected",
            _us_ctl.slot_state(2) == "running", _us_ctl.slot_state(2))
_v, _d, _g = gui.temp_view("finished", 36, 10.0, 37, stopped_by_user=True)
assert_true("...its temperature line says 'Stopped by you'",
            "Stopped by you" in _d and _g, _d)
_v, _d, _g = gui.temp_view("finished", 36, 10.0, 37, stopped_by_user=True,
                           charger_alert="time limit")
assert_true("...but the charger's own reason wins when it gave one",
            "Stopped by you" not in _d and "Stopped by charger" in _d, _d)

_uc = gui.SlotCard(1, "TEST-01", gui_theme.DARK)
_uc.add(_Sample(wall_time=_dt(2026, 10, 29), elapsed_s=1.0, slot=1,
                status=_stopped, drift_severity="ok", note=""),
        "finished", _good, stopped_by_user=True)
assert_true("the user-stopped card keeps the run's capacity and resistance",
            _uc.values["Capacity"].text() == "1971 mAh"
            and _uc.values["Resistance"].text() == "22 mΩ")
assert_true("...with a 'Stopped by you' caption",
            (not _uc.readings_note.isHidden())
            and "Stopped by you" in _uc.readings_note.text(),
            _uc.readings_note.text())

fr, held = gui.card_frames(_stopped, "disconnected", _good)
assert_true("disconnected after a stop: last good capacity, all held",
            held and fr["Capacity"] is _good and fr["Volts"] is _stopped)
fr, held = gui.card_frames(_good, "disconnected", _good)
assert_true("disconnected mid-run: the last frame, all held",
            held and all(f is _good for f in fr.values()))
_v, _d, _g = gui.temp_view("disconnected", 34, 420.0, 36)
assert_true("disconnected temperature: last reading, grayed, with its age",
            _v.startswith("34") and "7 min" in _v and _g and "Peak" in _d, _v)

# After an unwatched end the peak only covers what
# was received, so the card says "Highest seen ..., may have gone higher".
_v, _d, _g = gui.temp_view("finished", 36, 10.0, 38, unwatched_end=True)
assert_true("an unwatched end's peak reads 'Highest seen', not 'Peak'",
            "Highest seen 38" in _d and "may have gone higher" in _d
            and "Peak" not in _d and _g, _d)
_v, _d, _g = gui.temp_view("latched", 36, 10.0, 38, unwatched_end=True)
assert_true("...on a latched card too", "Highest seen 38" in _d, _d)
_v, _d, _g = gui.temp_view("finished", 36, 10.0, 38)
assert_true("a WATCHED end still says 'Peak', with no caveat",
            "Peak 38" in _d and "Highest seen" not in _d
            and "higher" not in _d, _d)
_v, _d, _g = gui.temp_view("finished", 36, 10.0, None, unwatched_end=True)
assert_true("an unwatched end with no peak shows no peak text",
            "Highest seen" not in _d and "Peak" not in _d, _d)
_v, _d, _g = gui.temp_view("finished", 36, 10.0, 38, unwatched_end=True,
                           charger_alert="time limit")
assert_true("...and keeps the charger's reason beside it",
            "Stopped by charger" in _d and "Highest seen 38" in _d, _d)
_up_ctl = gui.CaptureController(
    gui.SampleBridge(), gui.BleSource(gui.SessionOptions()), [1, 2], "up",
    tempfile.mkdtemp())
_up_ctl.cap = _SNS(temp_by_slot={1: 36, 2: 36}, temp_at={},
                   peak_temp_by_slot={1: 38, 2: 38},
                   unwatched_end=lambda s: s == 1)
assert_true("slot_temp_view carries Capture.unwatched_end into the card",
            "Highest seen 38" in _up_ctl.slot_temp_view(1, state="finished")[1],
            _up_ctl.slot_temp_view(1, state="finished")[1])
assert_true("...and only for the slot that was unwatched",
            "Peak 38" in _up_ctl.slot_temp_view(2, state="finished")[1],
            _up_ctl.slot_temp_view(2, state="finished")[1])

_us_win = gui.MainWindow(_us_ctl, [1, 2], gui_theme.DARK)
assert_true("the window title names SITREP91, its version and the charger",
            _us_win.windowTitle().startswith(
                f"SITREP91 {gui.build_label()} · SkyRC MC5000"),
            _us_win.windowTitle())
assert_true("...and a demo says so in the title, a real charger does not",
            _us_win.windowTitle().endswith(gui.DEMO_TITLE_NOTE)
            == _us_ctl.simulated
            and not gui.MainWindow(
                gui.CaptureController(gui.SampleBridge(), gui.BleSource(), [1],
                                      "t", tempfile.mkdtemp()),
                [1], gui_theme.DARK).windowTitle().endswith(
                    gui.DEMO_TITLE_NOTE))
assert_true("the title names the release candidate while a tag is set, and "
            "is the plain version once it is cleared",
            gui.build_label() == (f"1.0.0 {gui.RC_TAG}" if gui.RC_TAG
                                  else "1.0.0"), gui.build_label())
assert_true("...and the tag never reaches the version that meta.json carries",
            gui.runfiles.APP_VERSION == "1.0.0"
            and (not gui.RC_TAG or gui.RC_TAG not in gui.runfiles.APP_VERSION))
_us_ctl.session_ended = True          # what the capture thread sets at its end
_us_win.on_stopped("stopped")
_c2 = _us_win.cards[2]
assert_true("after the session ends every slot reads disconnected",
            all(_us_ctl.slot_state(s) == "disconnected" for s in (1, 2)))
assert_true("...the card says so and its Stop is disabled",
            _c2.state_label.text() == "disconnected"
            and not _c2.action_button.isEnabled()
            and not _c2.configure_button.isEnabled(),
            _c2.state_label.text())
assert_true("...its readings are kept, every one dimmed, with a caption",
            _c2.values["Capacity"].text() == "1971 mAh"
            and _c2._held == {"Volts", "Current", "Capacity", "Resistance"}
            and "Not connected" in _c2.readings_note.text(),
            _c2.readings_note.text())

print()
print("--- a resting slot's voltage is labeled as held ---")
#
# The charger repeats one reading taken at the slot's last change of state;
# the card must not present it as live.

_hv = gui.SlotCard(1, "TEST-01", gui_theme.DARK)
_rest = _parse(gui.build_status_frame(1, 0, 4187, 0, 0, 0))
_run = _parse(gui.build_status_frame(1, 500, 4100, 10, 60, 20))
_empty = _parse(gui.build_status_frame(1, 0, 0, 0, 0, 0))

def _hv_show(st, state):
    _hv.add(_Sample(wall_time=_dt(2026, 10, 29), elapsed_s=1.0, slot=1,
                    status=st, drift_severity="ok", note=""), state, None)
    _hv.render_state(state)
    return _hv.headers["Volts"].text(), _hv.headers["Volts"].toolTip()

for _s in ("idle", "armed", "finished"):
    _h, _t = _hv_show(_rest, _s)
    assert_true(f"{_s}: the voltage header says held, with the explanation",
                _h == "Volts · held" and "last changed state" in _t, _h)
for _s, _st in (("running", _run), ("starting", _rest), ("latched", _rest),
                ("disconnected", _rest)):
    _h, _t = _hv_show(_st, _s)
    assert_true(f"{_s}: the header is plain Volts", _h == "Volts" and not _t, _h)
_h, _ = _hv_show(_empty, "finished")
assert_true("an empty bay is not labeled held, even on a finished card",
            _h == "Volts", _h)
_h, _ = _hv_show(_rest, "idle")
_h2, _ = _hv_show(_run, "running")
assert_true("the label comes and goes with the state",
            _h == "Volts · held" and _h2 == "Volts", f"{_h} / {_h2}")
assert_true("the held number itself is not dimmed",
            "Volts" not in _hv._held)

print()
print("--- the GUI keeps a session log, like the CLI ---")
#
# A stranger's problem report needs a file to attach; GUI runs used to record
# meta.json "log": null.

sl_tmp = tempfile.mkdtemp()
sl_ctl = gui.CaptureController(
    gui.SampleBridge(), gui.DemoSource([1, 2], interval=0.02, auto_run=False),
    [1, 2], None, sl_tmp)
sl_win = gui.MainWindow(sl_ctl, [1, 2], gui_theme.DARK)
sl_ctl.start()
for _ in range(200):
    if sl_ctl.is_running and sl_ctl.cap and sl_ctl.cap.latest.get(1):
        break
    time.sleep(0.02)
sl_ctl.arm(1, _good_req(500))
sl_win.report("a line the window produced")
sl_win.on_start_slot(1)
for _ in range(200):
    if sl_ctl.cap.latest.get(1) and sl_ctl.cap.latest[1].current_ma == 500:
        break
    time.sleep(0.02)
sl_path = sl_ctl.cap.session_log
sl_ctl.stop()
app.processEvents()
sl_text = sl_path.read_text(encoding="utf-8") if sl_path else ""
sl_meta = slot_run_meta(sl_ctl.cap, 1)

assert_true("the session log sits in <logs folder>\\logs, named ..._gui.log",
            sl_path is not None and sl_path.parent == Path(sl_tmp) / "logs"
            and sl_path.name.endswith("_gui.log") and sl_path.exists(),
            str(sl_path))
assert_true("...it opens with the app, its version and the session",
            f"SITREP91 {gui.build_label()}: GUI session" in sl_text
            and "session log" in sl_text, sl_text[:300])
assert_true("...it holds the window's own lines, the Start reply among them",
            "a line the window produced" in sl_text and "injected" in sl_text,
            sl_text)
assert_true("...and the capture's, ending with why the session ended",
            "finished: stopped" in sl_text, sl_text[-300:])
assert_true("...each window line appears once, not echoed back twice",
            sl_text.count("a line the window produced") == 1)
assert_true("meta.json names the log instead of null",
            (sl_meta.get("session") or {}).get("log") == str(sl_path),
            str(sl_meta.get("session")))

# A logs folder that cannot be written costs the log, never the session.
bad_root = Path(tempfile.mkdtemp()) / "not_a_folder"
bad_root.write_text("a file where the logs folder would be", encoding="utf-8")
nl_status = []
nl_bridge = gui.SampleBridge()
nl_bridge.status.connect(nl_status.append)
nl_ctl = gui.CaptureController(
    nl_bridge, gui.DemoSource([1], interval=0.02, auto_run=False),
    [1], None, bad_root)
nl_ctl.start()
for _ in range(200):
    if nl_ctl.is_running and nl_ctl.cap and nl_ctl.cap.latest.get(1):
        break
    time.sleep(0.02)
nl_running = nl_ctl.is_running
nl_ctl.stop()
app.processEvents()
assert_true("an unwritable logs folder: the session still runs",
            nl_running and nl_ctl.cap.session_log is None,
            str(nl_ctl.cap.session_log))
assert_true("...and says it has no session log",
            any("no session log" in m for m in nl_status), str(nl_status[:5]))

# The held-voltage tooltip. It said the reading "steps up" after a run, which is
# true after a discharge (the cell rebounds) and backward after a charge (it
# relaxes down from the ceiling: 4.200 -> 4.186 V on a Li-ion CV end).
print()
print("held-voltage tooltip")
_tip = gui.HELD_VOLTS_TOOLTIP
assert_true("the tooltip does not say the reading only steps up",
            "step up" not in _tip, _tip)
assert_true("...and names both directions",
            "up after a discharge" in _tip and "down after a charge" in _tip,
            _tip)

# ---------------------------------------------------------------------------
# Configure's Advanced section
# ---------------------------------------------------------------------------
print()
print("--- Configure: the Advanced section ---")

from capture import RunRequest as _RR                                # noqa: E402


def _shown(dlg):
    """The Advanced rows currently offered, in dialog order."""
    return tuple(n for n, e in dlg.adv_edits.items()
                 if e.isVisibleTo(dlg.advanced_box))


def _vals(dlg, names=None):
    return {n: dlg.adv_edits[n].text() for n in (names or _shown(dlg))}


def _setup(chem, mode):
    d = gui.ConfigDialog(None, 1, [1], chemistry=chem)
    _pick(d.chem_combo, chem)
    _pick(d.action_combo, mode)
    _typed(d.capacity_edit, "5000")     # the dialog starts with none
    return d


dadv = gui.ConfigDialog(None, 1, [1], chemistry=_C2.LIION)
assert_true("Advanced starts collapsed, with its button showing",
            dadv.advanced_button.isVisibleTo(dadv)
            and not dadv.advanced_button.isChecked()
            and not dadv.advanced_box.isVisibleTo(dadv)
            and dadv.advanced_button.text().endswith("▸"))
dadv.advanced_button.setChecked(True)
assert_true("pressing it opens the section and the arrow turns",
            dadv.advanced_box.isVisibleTo(dadv)
            and dadv.advanced_button.text().endswith("▾"))
dadv.advanced_button.setChecked(False)
assert_true("pressing it again closes it",
            not dadv.advanced_box.isVisibleTo(dadv))

# The dialog must be as tall as its layout needs, open or shut. Its own size
# hint ignores word-wrapped text, so it came out short and the notes above
# Advanced were squeezed (the temperature-cutoff note lost its last lines). The layout's own height-for-width is the measure.
_dsz = gui.ConfigDialog(None, 1, [1, 2, 3, 4], chemistry=_C2.LIION)
_dsz.show()
_app_events = gui.QApplication.instance().processEvents
_app_events()


def _short_by(d):
    return d.layout().totalHeightForWidth(d.width()) - d.height()


assert_true("shown with Advanced closed, the dialog is as tall as its layout needs",
            _short_by(_dsz) <= 0, str(_short_by(_dsz)))
_dsz.advanced_button.setChecked(True)
_app_events()
assert_true("opening Advanced grows the dialog to fit - nothing is squeezed",
            _short_by(_dsz) <= 0, str(_short_by(_dsz)))
_h_open = _dsz.height()
_dsz.advanced_button.setChecked(False)
_app_events()
assert_true("closing it shrinks the dialog back, and still fits",
            _short_by(_dsz) <= 0 and _dsz.height() < _h_open,
            f"{_short_by(_dsz)} {_dsz.height()} {_h_open}")
_dsz.close()

# Changing action or chemistry with Advanced ALREADY OPEN swaps the rows (Li-ion
# discharge has one, charge has three), so the dialog must re-fit then too -
# otherwise the temperature-cutoff note is clipped (Charge showed two of the
# note's three lines).
_dsz = gui.ConfigDialog(None, 1, [1, 2, 3, 4], chemistry=_C2.LIION)
_dsz.show()
_app_events()
_dsz.advanced_button.setChecked(True)
_app_events()
_pick(_dsz.action_combo, _M2.CHARGE)
_app_events()
assert_true("switching action with Advanced open re-fits the dialog",
            _short_by(_dsz) <= 0, str(_short_by(_dsz)))
_pick(_dsz.action_combo, _M2.DISCHARGE)
_app_events()
assert_true("...and switching back does too",
            _short_by(_dsz) <= 0, str(_short_by(_dsz)))
_dsz.close()

# The 1C warning appearing is the same thing: it is wrapped text above the
# temperature-cutoff note, and the dialog kept its height, clipping that note
# mid-sentence.
_dsz = gui.ConfigDialog(None, 1, [1, 2, 3, 4], chemistry=_C2.LIION)
_typed(_dsz.capacity_edit, "800")
_dsz.show()
_app_events()
assert_true("no warning yet, and the dialog fits",
            not _dsz.rate_warning.isVisibleTo(_dsz) and _short_by(_dsz) <= 0)
_typed(_dsz.current_edit, "3000")
_app_events()
assert_true("the 1C warning appears and the dialog grows to fit it - nothing "
            "below is clipped",
            _dsz.rate_warning.isVisibleTo(_dsz) and _short_by(_dsz) <= 0,
            str(_short_by(_dsz)))
_typed(_dsz.current_edit, "100")
_app_events()
assert_true("...and shrinks back when it goes, still fitting",
            not _dsz.rate_warning.isVisibleTo(_dsz) and _short_by(_dsz) <= 0)
_dsz.close()

# A refusal is a long sentence; it must wrap, and the dialog must grow to show
# all of it (as one line it was clipped mid-word).
_dsz = gui.ConfigDialog(None, 1, [1, 2, 3, 4], chemistry=_C2.NIMH)
_typed(_dsz.capacity_edit, "2500")
_pick(_dsz.action_combo, _M2.CHARGE)
_dsz.show()
_app_events()
_typed(_dsz.adv_edits["delta_v_mv"], "13")
_h_before = _dsz.height()
_dsz.build_request()
_app_events()
assert_true("the refusal label wraps, so a long message is not clipped",
            _dsz.error_label.wordWrap() and _dsz.error_label.isVisibleTo(_dsz))
assert_true("...and the dialog grows to show all of it",
            _dsz.error_label.height()
            >= _dsz.error_label.heightForWidth(_dsz.error_label.width())
            and _short_by(_dsz) <= 0 and _dsz.height() > _h_before,
            f"{_dsz.error_label.height()} vs "
            f"{_dsz.error_label.heightForWidth(_dsz.error_label.width())}, "
            f"short by {_short_by(_dsz)}, {_h_before} -> {_dsz.height()}")
_dsz.close()


def _refused(chem=_C2.NIMH, mode=_M2.CHARGE):
    d = gui.ConfigDialog(None, 1, [1, 2, 3, 4], chemistry=chem)
    _typed(d.capacity_edit, "2500")
    _pick(d.action_combo, mode)
    d.show()
    _app_events()
    _typed(d.adv_edits["delta_v_mv"], "13")
    d.build_request()
    _app_events()
    return d


# A refusal must not outlive what it was about (a -dV refusal must not stay up
# on a Storage page, which has no -dV).
_dr = _refused()
assert_true("a refusal is showing", _dr.error_label.isVisibleTo(_dr))
_pick(_dr.action_combo, _M2.DISCHARGE)
assert_true("changing the action clears it",
            not _dr.error_label.isVisibleTo(_dr) and _dr.error_label.text() == "")
_dr.close()
_dr = _refused()
_pick(_dr.chem_combo, _C2.LIION)
assert_true("changing the chemistry clears it",
            not _dr.error_label.isVisibleTo(_dr))
_dr.close()
_dr = _refused()
_typed(_dr.adv_edits["delta_v_mv"], "9")
assert_true("editing the field it was about clears it",
            not _dr.error_label.isVisibleTo(_dr))
_dr.close()
_dr = _refused()
_typed(_dr.capacity_edit, "2500")
assert_true("editing any other field clears it too",
            not _dr.error_label.isVisibleTo(_dr))
assert_true("...and the dialog shrinks back to fit without it",
            _short_by(_dr) <= 0)
_dr.close()
_dr = _refused()
assert_true("a second Save that still fails shows the refusal again",
            _dr.build_request() is None and _dr.error_label.isVisibleTo(_dr))
_dr.close()

_expect_rows = {
    (_C2.LIION, _M2.CHARGE): ("target_mv", "charge_cutoff_current_ma", "keep_mv"),
    (_C2.LIION, _M2.DISCHARGE): ("discharge_cutoff_current_ma",),
    (_C2.LIION, _M2.STORAGE): ("storage_mv", "discharge_current_ma",
                               "charge_cutoff_current_ma",
                               "discharge_cutoff_current_ma"),
    (_C2.NIMH, _M2.CHARGE): ("delta_v_mv", "target_mv", "trickle_ma"),
    (_C2.NIMH, _M2.DISCHARGE): ("discharge_cutoff_current_ma",),
    (_C2.ENELOOP, _M2.CHARGE): ("delta_v_mv", "target_mv", "trickle_ma"),
    (_C2.ENELOOP, _M2.DISCHARGE): ("discharge_cutoff_current_ma",),
}
for (_c, _m), _rows in _expect_rows.items():
    _d = _setup(_c, _m)
    assert_true(f"{_c.name} {_m.name}: shows exactly {len(_rows)} setting(s), "
                f"in the panel's order",
                _shown(_d) == _rows, str(_shown(_d)))

_d = _setup(_C2.NIMH, _M2.CHARGE)
assert_true("nickel charge opens at the panel's values: -dV 6, target 1650, "
            "trickle off - and has no keep voltage row (the charger discards "
            "it over Bluetooth for nickel)",
            _vals(_d) == dict(delta_v_mv="6", target_mv="1650", trickle_ma="0"),
            str(_vals(_d)))
_d = _setup(_C2.LIION, _M2.CHARGE)
assert_true("Li-ion charge: target 4200, C cut-off 100, keep voltage off",
            _vals(_d) == dict(target_mv="4200", charge_cutoff_current_ma="100",
                              keep_mv="0"), str(_vals(_d)))
_d = _setup(_C2.LIION, _M2.STORAGE)
# (_setup types 5000 mAh, so the discharge leg is 0.2C = 1000, not the panel's
# 2000, and the charge leg 0.5C = 2500, not 3000.)
assert_true("Li-ion storage: 3800 mV, discharge 1000 mA (0.2C), both cut-offs 100",
            _vals(_d) == dict(storage_mv="3800", discharge_current_ma="1000",
                              charge_cutoff_current_ma="100",
                              discharge_cutoff_current_ma="100"),
            str(_vals(_d)))
assert_true("storage's main current is its CHARGE current (0.5C = 2500), "
            "labeled so",
            _d.current_edit.text() == "2500"
            and _d._form.labelForField(_d.current_edit).text()
            == "Charge current (mA)")
_dchg = _setup(_C2.LIION, _M2.CHARGE)
assert_true("...and every other action's is plain Current",
            _dchg._form.labelForField(_dchg.current_edit).text()
            == "Current (mA)")
assert_true("storage's timer follows the slower of its two currents "
            "(5000 mAh at 1.00 A = 450 min)",
            _d.timer_edit.text() == "450", _d.timer_edit.text())

_mon = gui.ConfigDialog(None, 1, [1], chemistry=_C2.LIION)
_mon.chem_combo.setCurrentIndex(_mon.chem_combo.findData(_C2.NICD))
assert_true("a monitor-only chemistry offers no Advanced section at all",
            not _mon.advanced_button.isVisibleTo(_mon)
            and _shown(_mon) == ())

# --- resets: a chemistry or action change puts the panel's values back ------
_d = _setup(_C2.NIMH, _M2.CHARGE)
_typed(_d.adv_edits["delta_v_mv"], "9")
_typed(_d.adv_edits["trickle_ma"], "120")
_pick(_d.chem_combo, _C2.ENELOOP)
assert_true("changing the chemistry resets the Advanced values",
            _vals(_d) == dict(delta_v_mv="6", target_mv="1650", trickle_ma="0"),
            str(_vals(_d)))
_typed(_d.adv_edits["trickle_ma"], "120")
_pick(_d.action_combo, _M2.DISCHARGE)
_pick(_d.action_combo, _M2.CHARGE)
assert_true("changing the action resets them too, and brings a page's rows back",
            _vals(_d)["trickle_ma"] == "0" and "trickle_ma" in _shown(_d),
            str(_vals(_d)))
_d = _setup(_C2.LIION, _M2.CHARGE)
_typed(_d.adv_edits["keep_mv"], "4000")
_pick(_d.action_combo, _M2.DISCHARGE)
_pick(_d.action_combo, _M2.CHARGE)
assert_true("a Li-ion keep voltage resets with the action too",
            _vals(_d)["keep_mv"] == "0", str(_vals(_d)))

# --- what reaches the request ------------------------------------------------
_d = _setup(_C2.NIMH, _M2.CHARGE)
_r = _d.build_request()
assert_true("left on the panel's values, the request names NO Advanced field - "
            "it is the request it always was",
            _r is not None and (_r.delta_v_mv, _r.target_mv, _r.trickle_ma,
                                _r.keep_mv, _r.charge_cutoff_current_ma,
                                _r.discharge_cutoff_current_ma,
                                _r.discharge_current_ma, _r.storage_mv)
            == (None,) * 8, str(_r))
_typed(_d.adv_edits["delta_v_mv"], "9")
_typed(_d.adv_edits["target_mv"], "1700")
_typed(_d.adv_edits["trickle_ma"], "120")
_r = _d.build_request()
assert_true("changed values reach the request, and only the changed ones - a "
            "nickel request never carries a keep voltage",
            _r is not None and (_r.delta_v_mv, _r.target_mv, _r.trickle_ma,
                                _r.keep_mv) == (9, 1700, 120, None), str(_r))
_d = _setup(_C2.LIION, _M2.CHARGE)
_typed(_d.adv_edits["keep_mv"], "4000")
_r = _d.build_request()
assert_true("a Li-ion keep voltage reaches the request",
            _r is not None and _r.keep_mv == 4000, str(_r))
_d = _setup(_C2.LIION, _M2.DISCHARGE)
_typed(_d.adv_edits["discharge_cutoff_current_ma"], "250")
_r = _d.build_request()
assert_true("a discharge cut-off current of 250 mA reaches the request",
            _r is not None and _r.discharge_cutoff_current_ma == 250, str(_r))
_d = _setup(_C2.LIION, _M2.STORAGE)
_r = _d.build_request()
assert_true("storage always names both currents (2500 charge, 1000 discharge) "
            "and nothing else",
            _r is not None and (_r.current_ma, _r.discharge_current_ma)
            == (2500, 1000) and (_r.storage_mv, _r.charge_cutoff_current_ma,
                                 _r.discharge_cutoff_current_ma) == (None,) * 3,
            str(_r))
_typed(_d.adv_edits["storage_mv"], "3750")
_typed(_d.adv_edits["discharge_current_ma"], "1500")
_r = _d.build_request()
assert_true("a changed storage voltage and discharge current reach it",
            _r is not None and _r.storage_mv == 3750
            and _r.discharge_current_ma == 1500, str(_r))
assert_true("...and storage's timer follows the new discharge current "
            "(5000 mAh at 1.50 A = 300 min)",
            _d.timer_edit.text() == "300", _d.timer_edit.text())
_d = _setup(_C2.LIION, _M2.DISCHARGE)
_d.adv_edits["trickle_ma"].setText("999")      # a row this page does not show
_r = _d.build_request()
assert_true("a row the page does not show is never read",
            _r is not None and _r.trickle_ma is None, str(_r))

# --- refusals ----------------------------------------------------------------
_d = _setup(_C2.NIMH, _M2.CHARGE)
_typed(_d.adv_edits["delta_v_mv"], "13")
assert_true("starts closed", not _d.advanced_button.isChecked())
assert_true("an out-of-range -dV is refused naming the range",
            _d.build_request() is None and "3-12mV" in _d.error_label.text(),
            _d.error_label.text())
assert_true("...and the section opens so the message is about something visible",
            _d.advanced_button.isChecked()
            and _d.advanced_box.isVisibleTo(_d))
_d = _setup(_C2.NIMH, _M2.CHARGE)
_typed(_d.adv_edits["trickle_ma"], "55")
assert_true("a trickle that is not a 10 mA step is refused",
            _d.build_request() is None and "50-300mA" in _d.error_label.text(),
            _d.error_label.text())
_d = _setup(_C2.LIION, _M2.CHARGE)
_typed(_d.adv_edits["keep_mv"], "3000")
assert_true("a keep voltage outside the window is refused naming it",
            _d.build_request() is None and "3980-4180mV" in _d.error_label.text(),
            _d.error_label.text())
_d = _setup(_C2.LIION, _M2.CHARGE)
_typed(_d.adv_edits["target_mv"], "abc")
assert_true("a non-number is refused, and the section opens",
            _d.build_request() is None
            and "Advanced" in _d.error_label.text()
            and _d.advanced_button.isChecked(), _d.error_label.text())
_d = _setup(_C2.LIION, _M2.STORAGE)
_typed(_d.adv_edits["discharge_current_ma"], "2500")
assert_true("storage's discharge current is held to 2000 mA",
            _d.build_request() is None and "2000" in _d.error_label.text(),
            _d.error_label.text())
_d = _setup(_C2.LIION, _M2.CHARGE)
_typed(_d.current_edit, "6000")
assert_true("a problem in a MAIN field leaves Advanced as it was (closed)",
            _d.build_request() is None and not _d.advanced_button.isChecked(),
            _d.error_label.text())

# --- the 1C warning names each of storage's currents -------------------------
_d = _setup(_C2.LIION, _M2.STORAGE)
_typed(_d.capacity_edit, "1000")
assert_true("the derived storage currents never warn (500 / 200 on 1000 mAh)",
            _d.rate_warning.isHidden(), _d.rate_warning.text())
_typed(_d.current_edit, "3000")
_typed(_d.adv_edits["discharge_current_ma"], "2000")
_w = _d.rate_warning.text()
assert_true("storage warns about BOTH currents when both are above 1C, each by "
            "its direction",
            _d.rate_warning.isVisibleTo(_d) and "Charge current 3000" in _w
            and "Discharge current 2000" in _w, _w)
_typed(_d.adv_edits["discharge_current_ma"], "500")
assert_true("...and only the one that still is, when the other is fixed",
            "Charge current" in _d.rate_warning.text()
            and "Discharge current" not in _d.rate_warning.text(),
            _d.rate_warning.text())

# --- reopening a saved config -----------------------------------------------
_ex = _RR(mode=_M2.CHARGE, chemistry=_C2.NIMH, current_ma=1000,
          capacity_mah=3000, safety_timer_min=450)
_d = gui.ConfigDialog(None, 1, [1], chemistry=_C2.NIMH, existing=_ex)
assert_true("a saved config on the defaults reopens with Advanced closed",
            not _d.advanced_button.isChecked()
            and _vals(_d)["delta_v_mv"] == "6", str(_vals(_d)))
_ex = _RR(mode=_M2.CHARGE, chemistry=_C2.NIMH, current_ma=1000,
          capacity_mah=3000, safety_timer_min=450, trickle_ma=120,
          delta_v_mv=9)
_d = gui.ConfigDialog(None, 1, [1], chemistry=_C2.NIMH, existing=_ex)
assert_true("a saved config with a changed setting reopens with it open and "
            "shown - a changed value is never hidden",
            _d.advanced_button.isChecked()
            and _vals(_d)["trickle_ma"] == "120"
            and _vals(_d)["delta_v_mv"] == "9", str(_vals(_d)))
_r2 = _d.build_request()
assert_true("...and Save hands back the same request",
            _r2 is not None and (_r2.trickle_ma, _r2.delta_v_mv) == (120, 9))
_ex = _RR(mode=_M2.STORAGE, chemistry=_C2.LIION, current_ma=1500,
          capacity_mah=3000, safety_timer_min=300)
_d = gui.ConfigDialog(None, 1, [1], chemistry=_C2.LIION, existing=_ex)
assert_true("an older storage config (one current, both ways) reopens showing "
            "that current as its discharge current too",
            _vals(_d)["discharge_current_ma"] == "1500"
            and _d.current_edit.text() == "1500", str(_vals(_d)))

# --- the card's config line names the voltage the run is heading FOR --------
print()
print("--- the armed card's config line ---")


def _line(mode, chem, **kw):
    return gui.describe_config(
        _RR(mode=mode, chemistry=chem, current_ma=kw.pop("current_ma", 1000),
            capacity_mah=3000, safety_timer_min=240, **kw), full=True)


assert_true("a discharge still reads current -> its cutoff",
            "1000 mA → 3200 mV" in _line(_M2.DISCHARGE, _C2.LIION)
            and "1000 mA → 900 mV" in _line(_M2.DISCHARGE, _C2.NIMH)
            and "1000 mA → 3000 mV" in _line(_M2.DISCHARGE, _C2.LIION,
                                             cutoff_mv=3000))
assert_true("a Li-ion charge reads current -> its TARGET (4200 default), not the "
            "discharge cutoff",
            "1000 mA → 4200 mV" in _line(_M2.CHARGE, _C2.LIION)
            and "3200" not in _line(_M2.CHARGE, _C2.LIION),
            _line(_M2.CHARGE, _C2.LIION))
assert_true("...or the target the user set (4100)",
            "1000 mA → 4100 mV" in _line(_M2.CHARGE, _C2.LIION, target_mv=4100))
assert_true("a nickel charge names no voltage by default - it ends on a -dV drop, "
            "so the old 900 mV is gone",
            _line(_M2.CHARGE, _C2.NIMH).endswith("1000 mA")
            and "900" not in _line(_M2.CHARGE, _C2.NIMH)
            and "1650" not in _line(_M2.CHARGE, _C2.ENELOOP),
            _line(_M2.CHARGE, _C2.NIMH))
assert_true("...but does name a target the user changed",
            "1000 mA → 1700 mV" in _line(_M2.CHARGE, _C2.NIMH, target_mv=1700))
assert_true("a storage run reads its storage voltage (3800 default)",
            "1500 mA → 3800 mV" in _line(_M2.STORAGE, _C2.LIION, current_ma=1500),
            _line(_M2.STORAGE, _C2.LIION, current_ma=1500))
assert_true("...with both currents when it has two, and its own voltage if set",
            "3000 / 2000 mA → 3750 mV" in _line(
                _M2.STORAGE, _C2.LIION, current_ma=3000,
                discharge_current_ma=2000, storage_mv=3750))
assert_true("the short form (no rate) is unchanged for every action",
            all(gui.describe_config(
                _RR(mode=m, chemistry=c, current_ma=1000, capacity_mah=3000,
                    safety_timer_min=240), full=False).count("mA") == 0
                for c, m in ((_C2.LIION, _M2.CHARGE), (_C2.NIMH, _M2.CHARGE),
                             (_C2.LIION, _M2.STORAGE))))

print()
print("--- no deferred commands: nothing is accepted while the link is down "
      "---")
import threading as _thr3                                          # noqa: E402

ld_bridge = gui.SampleBridge()
ld_status = []
ld_bridge.status.connect(ld_status.append)
ld_ctl = gui.CaptureController(ld_bridge, gui.BleSource(), [1], "linkdown",
                               tempfile.mkdtemp())
_stay = _thr3.Event()
ld_ctl._thread = _thr3.Thread(target=_stay.wait, daemon=True)   # "alive"
ld_ctl._thread.start()
_logged = []
ld_ctl.cap = _SNq(link_ok=False, latest={1: _idle}, latest_any={1: _idle},
                  started_slots=set(), log=_logged.append, finished=set(),
                  torn_down=set(), measuring_ir=set(), start_failed=set())
ld_ctl.arm(1, _good_req(500))
assert_true("a capture that is retrying counts as link down",
            ld_ctl.is_running and ld_ctl.link_down)
_r = ld_ctl.start_slot(1)
assert_true("Start is refused with the panel message",
            _r == f"cannot start slot 1: {gui.NOT_CONNECTED}"
            and gui.NOT_CONNECTED == "Not connected. Use the charger's panel.",
            _r)
assert_true("...nothing is queued, and the slot stays armed",
            ld_ctl._inject_queue.empty() and 1 in ld_ctl._armed
            and 1 not in ld_ctl._starting)
assert_true("Stop is refused the same way, nothing queued",
            gui.NOT_CONNECTED in ld_ctl.stop_slot(1)
            and ld_ctl._stop_queue.empty())
_noted_as = []
ld_ctl.note = _noted_as.append
ld_ctl.all_stop()
assert_true("ALL STOP is refused: the event is not set, and it says why",
            not ld_ctl._all_stop.is_set()
            and any(gui.NOT_CONNECTED in m for m in ld_status), str(ld_status))
assert_true("...and the refusal is written to the session log too, not only "
            "the status line",
            any("ALL STOP refused" in m and gui.NOT_CONNECTED in m
                for m in _noted_as), str(_noted_as))
del ld_ctl.note
ld_ctl.cap.link_ok = True
ld_ctl.cap.latest = {1: _busy}
ld_ctl.all_stop()
assert_true("with the link up ALL STOP goes through",
            ld_ctl._all_stop.is_set())
assert_true("...and Stop is accepted",
            "stopping slot 1" in ld_ctl.stop_slot(1)
            and not ld_ctl._stop_queue.empty())
_stay.set()

print()
print("--- a capture-thread failure reaches the logs, not a terminal that "
      "does not exist ---")
import crashlog as _cl5g                                            # noqa: E402

_cl5g_dir = tempfile.mkdtemp()
os.environ["SITREP91_LOG_DIR"] = _cl5g_dir
_cl5g.reset_for_tests()


class _ExplodingSource:
    options = None
    last_error = None
    error_is_plain = False
    crash_log = None

    def run(self, cap, stop, status, all_stop=None, inject_queue=None,
            stop_queue=None):
        raise RuntimeError("deliberate capture-thread failure")


ex_ctl = gui.CaptureController(gui.SampleBridge(), _ExplodingSource(), [1],
                               "explode", tempfile.mkdtemp())
ex_ctl.start()
_wait(lambda: not ex_ctl.is_running)
_cl5g_files = sorted(Path(_cl5g_dir).glob("crash_*.log"))
assert_true("the failure is in a crash log, with its traceback",
            len(_cl5g_files) == 1 and "deliberate capture-thread failure"
            in _cl5g_files[0].read_text(encoding="utf-8")
            and "Traceback" in _cl5g_files[0].read_text(encoding="utf-8"),
            str(_cl5g_files))
_ex_session = Path(ex_ctl.cap.session_log).read_text(encoding="utf-8")
assert_true("...and in the session log, which says where the details are",
            "capture failed" in _ex_session
            and "deliberate capture-thread failure" in _ex_session,
            _ex_session[-300:])
os.environ.pop("SITREP91_LOG_DIR")
_cl5g.reset_for_tests()
_ui_src = _inspect.getsource(gui.MainWindow)
assert_true("no message the window can show points at a terminal",
            "in terminal" not in _ui_src and "terminal log" not in _ui_src,
            "a UI string still names a terminal")

print()
print("--- --demo never reaches a charger, ALL STOP included ---")
# Once the replay ended, the capture thread was dead, ALL STOP stayed enabled,
# and it took the "no capture running" path: scan, connect to a REAL charger,
# send the init that halts every slot.
_real_stop_all_mode = gui.stop_all_mode
_real_run_capture_d = gui.run_capture
_reached = []


async def _would_touch_a_charger(*a, **k):
    _reached.append("stop_all_mode")
    return 0


async def _no_ble_either(*a, **k):
    _reached.append("run_capture")
    return "stopped"


gui.stop_all_mode = _would_touch_a_charger
gui.run_capture = _no_ble_either
try:
    dm_ctl = gui.CaptureController(
        gui.SampleBridge(), gui.DemoSource([1, 2], interval=0.001),
        [1, 2], "demo_allstop", tempfile.mkdtemp())
    dm_ctl.bridge.status.connect(lambda m: None)
    dm_ctl.start()
    assert_true("the demo's replay ends on its own",
                _wait(lambda: not dm_ctl.is_running, n=1500))
    dm_ctl.all_stop()
    time.sleep(0.5)             # a worker thread, if one was started, ran by now
    assert_true("ALL STOP after the replay reaches no charger",
                _reached == [], str(_reached))
    dm_ctl.all_stop()
    dm_ctl._standalone_all_stop()
    time.sleep(0.3)
    assert_true("...however it is called",
                _reached == [], str(_reached))
    live_ctl = gui.CaptureController(
        gui.SampleBridge(), gui.DemoSource([1], interval=0.02, auto_run=False),
        [1], "demo_live", tempfile.mkdtemp())
    live_ctl.start()
    _wait(lambda: live_ctl.cap.latest.get(1))
    live_ctl.all_stop()
    time.sleep(0.2)
    live_ctl.stop()
    assert_true("...and during the replay it is simulated, still no charger",
                _reached == [], str(_reached))
    assert_true("a real source is not simulated",
                not gui.CaptureController(
                    gui.SampleBridge(), gui.BleSource(), [1], "x",
                    tempfile.mkdtemp()).simulated)
finally:
    gui.stop_all_mode = _real_stop_all_mode
    gui.run_capture = _real_run_capture_d

print()
print("--- --demo is interactive: idle slots, Configure and Start work "
      "---")
# The demo used to start every slot discharging at once, so Configure was
# disabled and the replay ended after ~30 s. main() now builds it with
# auto_run=False. Offline: the real main() against a stubbed QApplication is
# overkill, so the construction is inspected and the behavior is exercised.
_main_src = _inspect.getsource(gui.main)
assert_true("main() builds the demo interactive",
            "DemoSource(slots, auto_run=False)" in _main_src, _main_src[:200])
im_ctl = gui.CaptureController(
    gui.SampleBridge(), gui.DemoSource([1, 2], interval=0.02, auto_run=False),
    [1, 2], "demo_interactive", tempfile.mkdtemp())
im_win = gui.MainWindow(im_ctl, [1, 2], gui_theme.DARK)
im_ctl.subscribe(im_win.on_sample)
im_ctl.start()
_wait(lambda: im_ctl.cap.latest.get(1) and im_ctl.cap.latest.get(2))
assert_true("every slot starts IDLE and Configure is available",
            im_ctl.slot_state(1) == "idle"
            and im_ctl.cap.latest[1].current_ma == 0
            and im_win.cards[1].configure_button.isEnabled(),
            f"{im_ctl.slot_state(1)}")
im_ctl.arm(1, _good_req(500))
_msg = im_ctl.start_slot(1)
assert_true("an armed slot can be started in the demo",
            "starting slot 1" in _msg, _msg)
_wait(lambda: im_ctl.cap.latest[1].current_ma > 0)
assert_true("...and runs, while slot 2 stays idle",
            im_ctl.cap.latest[1].current_ma == 500
            and im_ctl.cap.latest[2].current_ma == 0)
time.sleep(0.5)
assert_true("the demo runs on until stopped (no 30-second end)",
            im_ctl.is_running)
assert_true("closing a demo says nothing real is running",
            "Nothing real" in im_win._stop_question())
im_ctl.stop_slot(1)
_wait(lambda: im_ctl.cap.latest[1].current_ma == 0)
im_ctl.stop()

print()
print("--- closing waits long enough for the slots to be stopped ---")
import threading as _thr4                                          # noqa: E402

assert_true("the wait is longer than the old 10 s (stopping four slots plus "
            "the all-slot fallback takes more than that)",
            gui.STOP_TIMEOUT_S >= 25.0, str(gui.STOP_TIMEOUT_S))
_slow_done = []


def _slow_teardown():
    time.sleep(0.4)             # a teardown that outlasts a short wait
    _slow_done.append(True)


st_ctl = gui.CaptureController(gui.SampleBridge(), gui.BleSource(), [1], "st",
                               tempfile.mkdtemp())
st_ctl._thread = _thr4.Thread(target=_slow_teardown, daemon=True)
st_ctl._thread.start()
_pumped = []
st_ctl.stop(pump=lambda: _pumped.append(1))
assert_true("stop() waits for a slow teardown to finish, keeping the window "
            "alive (pumping events) while it does",
            _slow_done == [True] and len(_pumped) > 3
            and st_ctl._thread is None, f"{_slow_done} {len(_pumped)}")
_never = _thr4.Event()
st_ctl2_status = []
st_bridge = gui.SampleBridge()
st_bridge.status.connect(st_ctl2_status.append)
st_ctl2 = gui.CaptureController(st_bridge, gui.BleSource(), [1], "st2",
                                tempfile.mkdtemp())
st_ctl2._thread = _thr4.Thread(target=_never.wait, daemon=True)
st_ctl2._thread.start()
_t0 = time.monotonic()
st_ctl2.stop(timeout=0.3)
app.processEvents()
assert_true("a teardown that never ends is given up on after the timeout, "
            "and says so",
            0.25 < time.monotonic() - _t0 < 3.0
            and any("did not stop cleanly" in m for m in st_ctl2_status),
            str(st_ctl2_status))
_never.set()

print()
print("--- connection marker and held refusals ---")
#
# After a Stop the window gave no sign it was disconnected, and the "Not
# connected" refusal was overwritten by the reconnect loop within a second.
assert_true("marker: a live link names the charger",
            gui.connection_marker(False, True, "#Charger-1A2B")
            == ("connected", "CONNECTED  #Charger-1A2B"))
assert_true("marker: a live link with no name still says CONNECTED",
            gui.connection_marker(False, True, None)
            == ("connected", "CONNECTED"))
assert_true("marker: no live link is DISCONNECTED, whatever the name",
            gui.connection_marker(False, False, "#Charger-1A2B")
            == ("disconnected", "DISCONNECTED"))
assert_true("marker: the demo is neither (it never touches Bluetooth)",
            gui.connection_marker(True, True, None) == ("demo", "DEMO")
            and gui.connection_marker(True, False, "x") == ("demo", "DEMO"))
assert_true("a 'Not connected' line and any 'cannot ...' line are refusals; a "
            "reconnect line is not",
            gui.is_refusal(f"cannot start slot 1: {gui.NOT_CONNECTED}")
            and gui.is_refusal(f"ALL STOP: {gui.NOT_CONNECTED}")
            and gui.is_refusal("cannot stop slot 2: x")
            and not gui.is_refusal("--- reconnecting (attempt 5; 1 link loss "
                                   "so far) ---")
            and not gui.is_refusal("ALL STOP complete; slots idle"))

mk_bridge = gui.SampleBridge()
mk_ctl = gui.CaptureController(mk_bridge, gui.BleSource(), [1, 2], "marker",
                               tempfile.mkdtemp())
mk_win = gui.MainWindow(mk_ctl, [1, 2], gui_theme.DARK)
mk_win.show()
app.processEvents()
assert_true("before any session the marker says DISCONNECTED, in the error "
            "color",
            mk_win.connection_label.text() == "DISCONNECTED"
            and gui_theme.PALETTES[gui_theme.DARK]["error"]
            in mk_win.connection_label.styleSheet(),
            mk_win.connection_label.styleSheet())
import threading as _thr_mk                                        # noqa: E402
_stay_mk = _thr_mk.Event()
mk_ctl._thread = _thr_mk.Thread(target=_stay_mk.wait, daemon=True)
mk_ctl._thread.start()
mk_ctl.cap = _SNq(link_ok=True, device_name="#Charger-1A2B", latest={},
                  gaps=[], latest_any={}, started_slots=set(),
                  log=lambda m: None,
                  finished=set(), torn_down=set(), measuring_ir=set(),
                  start_failed=set())
mk_win._refresh_connection()
assert_true("a live session shows CONNECTED and the charger's name, in the ok "
            "color",
            mk_win.connection_label.text() == "CONNECTED  #Charger-1A2B"
            and gui_theme.PALETTES[gui_theme.DARK]["ok"]
            in mk_win.connection_label.styleSheet())
mk_ctl.cap.link_ok = False
mk_win._refresh_connection()
assert_true("a retrying session (link lost) goes back to DISCONNECTED",
            mk_win.connection_label.text() == "DISCONNECTED")
from PySide6.QtCore import QPoint as _QPoint                       # noqa: E402
mk_win.retry_button.setVisible(True)
app.processEvents()
_rx_mk = mk_win.retry_button.mapTo(mk_win, _QPoint(0, 0)).x()
assert_true("the marker sits beside Retry, to its left",
            mk_win.connection_label.mapTo(mk_win, _QPoint(0, 0)).x() < _rx_mk)

# held refusal
_refusal = f"cannot start slot 1: {gui.NOT_CONNECTED}"
mk_win.on_status(_refusal)
for _i in range(4):
    mk_win.on_status(f"--- reconnecting (attempt {_i}; 1 link loss so far) ---")
assert_true("a refusal stays on the status line while the reconnect loop "
            "rewrites it every few seconds",
            mk_win.status_label.text() == _refusal,
            mk_win.status_label.text())
assert_true("...in the warning color, bold, so it is noticed",
            gui_theme.PALETTES[gui_theme.DARK]["warn"]
            in mk_win.status_label.styleSheet())
assert_true("...while the console still receives every line",
            "attempt 3" in mk_win.console.toPlainText())
mk_win._hold_until = 0.0                      # the hold has run out
mk_win._release_status_hold()
assert_true("when the hold ends the latest status line is put back",
            "attempt 3" in mk_win.status_label.text(),
            mk_win.status_label.text())
mk_win.on_status("a later line")
assert_true("...and ordinary lines show normally again",
            mk_win.status_label.text() == "a later line")
# an ending written during a hold is not overwritten when the hold releases
mk_win.on_status(_refusal)
mk_win.on_stopped("no-device")
_ending = mk_win.status_label.text()
mk_win._hold_until = 0.0
mk_win._release_status_hold()
assert_true("a session ending written during a hold is not clobbered by the "
            "hold's release",
            mk_win.status_label.text() == _ending, mk_win.status_label.text())
_stay_mk.set()
mk_win.close()

print()
print("--- only one SITREP91 per computer ---")
#
# A window left open from the day before reconnected the moment the charger was
# powered on and held it; the new window then scanned for 5 minutes and asked
# "is the charger awake?". A real session now claims a named mutex first.
import subprocess as _sp_si                                      # noqa: E402
from PySide6.QtCore import QPoint as _QPoint                     # noqa: E402
import crashlog                                                  # noqa: E402
import uuid as _uuid_si                                          # noqa: E402

if sys.platform == "win32":
    _nm = "Local\\SITREP91-test-" + _uuid_si.uuid4().hex
    assert_true("the first claim succeeds", gui.acquire_single_instance(_nm))
    assert_true("a second claim on the same name is refused (another SITREP91 "
                "holds it)", gui.acquire_single_instance(_nm) is False)
    gui.release_single_instance()
    assert_true("...and once the first lets go, the name is free again",
                gui.acquire_single_instance(_nm) is True)
    gui.release_single_instance()

    # A second real copy, as a separate process, must refuse and exit. The
    # parent holds the name; the child is told to use it.
    assert_true("the parent claims the test name",
                gui.acquire_single_instance(_nm) is True)
    _env_si = dict(os.environ)
    _env_si.update(SITREP91_INSTANCE_NAME=_nm, SITREP91_NO_MESSAGEBOX="1",
                   QT_QPA_PLATFORM="offscreen",
                   SITREP91_LOG_DIR=tempfile.mkdtemp())
    _gui_dir = str(Path(gui.__file__).resolve().parent)
    _real = ("import sys\nsys.argv = ['gui.py']\nimport gui\ngui.main()\n")
    try:
        _r_si = _sp_si.run([sys.executable, "-c", _real], env=_env_si,
                           cwd=_gui_dir, capture_output=True, text=True,
                           timeout=60)
        _rc_si = _r_si.returncode
    except _sp_si.TimeoutExpired:
        _rc_si = "still running (it should have refused)"
    assert_true("a second real SITREP91 exits at once with status 3 instead "
                "of opening a window", _rc_si == 3, str(_rc_si))

    # The demo never touches Bluetooth, so it may run beside a real one.
    _demo = ("import sys\nsys.argv = ['gui.py', '--demo']\nimport gui\n"
             "gui.main()\n")
    _p_si = _sp_si.Popen([sys.executable, "-c", _demo], env=_env_si,
                         cwd=_gui_dir, stdout=_sp_si.DEVNULL,
                         stderr=_sp_si.DEVNULL)
    try:
        _p_si.wait(timeout=8)
        _alive_si = False
    except _sp_si.TimeoutExpired:
        _alive_si = True
        _p_si.kill()
        _p_si.wait()
    assert_true("the demo is NOT refused beside a real session (it stays "
                "open)", _alive_si)
    gui.release_single_instance()

assert_true("the refusal message tells the person what to do",
            "taskbar" in gui.ALREADY_OPEN_TEXT
            and "Task Manager" in gui.ALREADY_OPEN_TEXT
            and "one SITREP91" in gui.ALREADY_OPEN_TEXT)
assert_true("crashlog has an information box for it (not an error box)",
            hasattr(crashlog, "notice"))

print()
print("--- no charger found: the message stays, and Retry looks again "
      "---")
# The helpful "no MC5000 found" line was overwritten at once by "capture ended:
# no-device | runs saved in ...", every card went "disconnected", Stop grayed
# out, and the only way forward was to close and reopen the app.
_nd_calls = []


async def _no_charger(cap, opts, **kw):
    _nd_calls.append(cap)
    cap.log("no MC5000 found after 2.5 min of looking")
    return "no-device"


_real_rc_nd = gui.run_capture
gui.run_capture = _no_charger
try:
    nd_bridge = gui.SampleBridge()
    nd_ctl = gui.CaptureController(nd_bridge, gui.BleSource(), [1, 2],
                                   "nodevice", tempfile.mkdtemp())
    nd_win = gui.MainWindow(nd_ctl, [1, 2], gui_theme.DARK)
    nd_ctl.start()
    _wait(lambda: not nd_ctl.is_running and nd_ctl.session_ended)
    nd_win.on_stopped("no-device")
    _t = nd_win.status_label.text()
    assert_true("the status says no charger was found, that the charger may be "
                "asleep or out of range, and what to do next",
                "No MC5000 found" in _t and "awake and in range" in _t
                and "Retry" in _t and "close and reopen" in _t, _t)
    assert_true("...and does not claim runs were saved (nothing was recorded)",
                "runs saved" not in _t, _t)
    assert_true("...and says another SITREP91 window or SkyRC's phone app may "
                "be holding the charger",
                "another sitrep91 window" in _t.lower()
                and "SkyRC's phone app" in _t, _t)
    assert_true("Retry is offered and enabled; Stop is not",
                nd_win.retry_button.isVisibleTo(nd_win)
                and nd_win.retry_button.isEnabled()
                and not nd_win.stop_button.isEnabled())
    nd_win.show()
    app.processEvents()
    _rx = nd_win.retry_button.mapTo(nd_win, _QPoint(0, 0)).x()
    _sx = nd_win.settings_button.mapTo(nd_win, _QPoint(0, 0)).x()
    _stx = nd_win.stop_button.mapTo(nd_win, _QPoint(0, 0)).x()
    assert_true("Retry sits to the LEFT of Settings, which sits left of Stop",
                _rx < _sx < _stx, f"{_rx} {_sx} {_stx}")
    assert_true("no label points at a 'Start monitoring' control that does "
                "not exist",
                "Start monitoring" not in _inspect.getsource(gui))
    _first_cap = nd_ctl.cap
    nd_win.chart.xs[1].append(5.0)
    nd_win.chart.data[1]["voltage"].append(4.1)
    nd_win.drift_errors = 3
    nd_win.on_retry_clicked()
    _wait(lambda: len(_nd_calls) == 2)
    assert_true("Retry starts a NEW capture session (a second scan)",
                len(_nd_calls) == 2 and nd_ctl.cap is not _first_cap
                and nd_ctl.cap is _nd_calls[1], str(len(_nd_calls)))
    assert_true("...clearing the last session's chart and drift count",
                nd_win.chart.xs[1] == [] and nd_win.drift_errors == 0)
    _wait(lambda: nd_ctl.session_ended)
    nd_win.on_stopped("no-device")
    assert_true("and when the charger is still not there, Retry comes back",
                nd_win.retry_button.isVisibleTo(nd_win)
                and "No MC5000 found" in nd_win.status_label.text())
    _stay2 = _thr3.Event()
    nd_ctl._thread = _thr3.Thread(target=_stay2.wait, daemon=True)
    nd_ctl._thread.start()
    assert_true("Retry does nothing while a session is live",
                nd_ctl.restart() is False)
    _stay2.set()
    nd_ctl._thread.join()
finally:
    gui.run_capture = _real_rc_nd

# Armed configs survive a restart; nothing queued for the old link does.
rs_ctl = gui.CaptureController(
    gui.SampleBridge(), gui.DemoSource([1], interval=0.02, auto_run=False),
    [1], "restart_keeps", tempfile.mkdtemp())
rs_ctl.arm(1, _good_req(500))
rs_ctl.start()
_wait(lambda: rs_ctl.cap.latest.get(1))
rs_ctl._inject_queue.put("stale")
rs_ctl.stop()
rs_ctl._inject_queue.put("stale")
assert_true("restart keeps the armed config", rs_ctl.restart()
            and 1 in rs_ctl._armed)
assert_true("...and drops what was queued for the old link",
            rs_ctl._inject_queue.empty())
rs_ctl.stop()

print()
print("--- close during Stop, link-down dialog, "
      "Configure re-derive, Retry ---")
import subprocess as _sp18                                       # noqa: E402
import threading as _th18                                        # noqa: E402
from pathlib import Path as _P18                                 # noqa: E402

# The X clicked while a Stop is being waited out must not raise, must not
# ask again, and must close the window once the stop is done.
_hook_errors = []
_old_hook = sys.excepthook
sys.excepthook = lambda *a: _hook_errors.append(a)
_hold = _th18.Event()
n1_ctl = gui.CaptureController(gui.SampleBridge(), gui.BleSource(), [1],
                               "n1", tempfile.mkdtemp())
n1_ctl._thread = _th18.Thread(target=_hold.wait, daemon=True)
n1_ctl._thread.start()
_fired = []


def _pump_nested_stop():
    if not _fired:
        _fired.append(1)
        n1_ctl.stop(timeout=0.2)         # a nested stop clears _thread ...
        _hold.set()                      # ... then the real one finishes


n1_ctl.stop(pump=_pump_nested_stop)      # ... and the outer loop must cope
assert_true("a stop re-entered from its own event pump does not raise",
            not _hook_errors and n1_ctl._thread is None, str(_hook_errors))

_hold2 = _th18.Event()
n1b_ctl = gui.CaptureController(gui.SampleBridge(), gui.BleSource(), [1],
                                "n1b", tempfile.mkdtemp())
n1b_ctl._thread = _th18.Thread(target=_hold2.wait, daemon=True)
n1b_ctl._thread.start()
n1b_win = gui.MainWindow(n1b_ctl, [1], gui_theme.DARK)
n1b_win.show()
app.processEvents()
_asked = []
n1b_win.confirm_stop = lambda: _asked.append(1) or True
_real_stop = n1b_ctl.stop
_x_done = []


def _stop_with_x_click(timeout=None, pump=None):
    def pump2():
        if not _x_done:
            _x_done.append(1)
            n1b_win.close()              # the X, mid-wait
            _hold2.set()
        if pump is not None:
            pump()
    return _real_stop(timeout, pump2)


n1b_ctl.stop = _stop_with_x_click
n1b_win.on_stop_clicked()
app.processEvents()
assert_true("Stop then the X mid-wait raises nothing", not _hook_errors,
            str(_hook_errors))
assert_true("...asks the question once, not again for the X", len(_asked) == 1,
            str(_asked))
assert_true("...and the window closes when the stop has finished",
            not n1b_win.isVisible() and not n1b_win._stopping)
sys.excepthook = _old_hook

# With the link down, the dialog says it is not connected.
_hold3 = _th18.Event()
n2_ctl = gui.CaptureController(gui.SampleBridge(), gui.BleSource(), [1, 3],
                               "n2", tempfile.mkdtemp())
n2_ctl._thread = _th18.Thread(target=_hold3.wait, daemon=True)
n2_ctl._thread.start()
n2_win = gui.MainWindow(n2_ctl, [1, 3], gui_theme.DARK)
n2_ctl.cap = _fake_cap({1}, s1=_busy)
n2_ctl.cap.link_ok = False
_q = n2_win._stop_question()
assert_true("link down with a run the app started - says NOT connected, "
            "names the slot, says it cannot stop it and points at the panel",
            "NOT connected" in _q and "slot 1" in _q
            and "cannot stop" in _q and "panel" in _q
            and "This stops the run" not in _q, _q)
n2_ctl.cap = _fake_cap(set(), s1=_busy)
n2_ctl.cap.link_ok = False
_q = n2_win._stop_question()
assert_true("link down with nothing of ours going - still says it cannot "
            "stop anything", "NOT connected" in _q and "panel" in _q, _q)
n2_ctl.cap = _fake_cap({1}, s1=_busy)
assert_true("link up: the original promise stands",
            "This stops the run SITREP91 started on slot 1" in
            n2_win._stop_question())
_hold3.set()

# Group Stop while disconnected says so instead of "stopping".
n4_ctl = gui.CaptureController(gui.SampleBridge(), gui.BleSource(), [1, 2],
                               "n4", tempfile.mkdtemp())
n4_ctl.cap = _fake_cap({1, 2}, s1=_busy, s2=_busy)
n4_ctl._groups = {1: {1, 2}}
_msg = n4_ctl.stop_group(1)
assert_true("Group Stop with no link reports the refusal, queues nothing",
            gui.NOT_CONNECTED in _msg and "stopping" not in _msg
            and n4_ctl._stop_queue.empty(), _msg)

# BleSource forgets an earlier failure when a new session begins.
_old_rc = gui.run_capture


async def _rc_done(*a, **k):
    return "done"


gui.run_capture = _rc_done
_src = gui.BleSource()
_src.last_error, _src.error_is_plain, _src.crash_log = "old", True, "x.log"
_src.run(None, _th18.Event(), lambda m: None)
gui.run_capture = _old_rc
assert_true("Retry starts BleSource with no old error wording or log path",
            (_src.last_error, _src.error_is_plain, _src.crash_log)
            == (None, False, None))

# Restart hands the old session's runs to the new one.
n6_ctl = gui.CaptureController(gui.SampleBridge(), gui.BleSource(), [1],
                               "n6", tempfile.mkdtemp())
n6_ctl.cap = _SNq(carry_forward=lambda: {1: {"elapsed": 60, "action": "charge",
                                             "temp_cutoff_c": 40}})
_got = []
n6_ctl.start = lambda carried=None: _got.append(carried) or True
n6_ctl.restart()
assert_true("Retry passes the runs this app started to the new session",
            _got == [{1: {"elapsed": 60, "action": "charge",
                          "temp_cutoff_c": 40}}], str(_got))

# Reopening Configure.
from capture import suggest_safety_timer_min as _stm              # noqa: E402
_m = _M0.DISCHARGE
_derived_timer = _stm(_C2.LIION, _m, 3000, 600)[0]
_ex = _RR0(mode=_m, chemistry=_C2.LIION, current_ma=600, capacity_mah=3000,
           safety_timer_min=_derived_timer, cutoff_mv=None)
assert_true("typed_fields: a config that is exactly what the derivation gives "
            "has nothing typed",
            gui.typed_fields(_ex) == (set(), False), str(gui.typed_fields(_ex)))
_dn = gui.ConfigDialog(None, 1, [1], chemistry=_C2.LIION, existing=_ex)
_typed(_dn.capacity_edit, "800")
assert_true("reopened, then capacity changed: the derived current and "
            "timer follow (800 mAh: 160 mA)",
            _dn.current_edit.text() == "160"
            and _dn.timer_edit.text() == str(_stm(_C2.LIION, _m, 800, 160)[0]),
            f"{_dn.current_edit.text()} {_dn.timer_edit.text()}")
_ex2 = _RR0(mode=_m, chemistry=_C2.LIION, current_ma=700, capacity_mah=3000,
            safety_timer_min=999, cutoff_mv=3100)
assert_true("typed_fields: a different current, timer and cutoff are typed",
            gui.typed_fields(_ex2) == ({"current", "cutoff"}, True),
            str(gui.typed_fields(_ex2)))
_dn2 = gui.ConfigDialog(None, 1, [1], chemistry=_C2.LIION, existing=_ex2)
_typed(_dn2.capacity_edit, "800")
assert_true("...typed values stay when the capacity changes",
            (_dn2.current_edit.text(), _dn2.timer_edit.text(),
             _dn2.cutoff_edit.text()) == ("700", "999", "3100"),
            str(_fields(_dn2)))
_pick(_dn2.chem_combo, _C2.NIMH)
assert_true("...and a typed timer survives a chemistry change; a cutoff "
            "outside the new window is taken back, the typed current stays",
            _dn2.timer_edit.text() == "999" and _dn2.current_edit.text() == "700"
            and _dn2.cutoff_edit.text() != "3100", str(_fields(_dn2)))
_dn3 = gui.ConfigDialog(None, 1, [1], chemistry=_C2.LIION, existing=_ex)
_pick(_dn3.chem_combo, _C2.NIMH)
_typed(_dn3.capacity_edit, "2000")
assert_true("an untouched reopened config re-derives on a chemistry change too "
            "(NiMH 2000 mAh discharge: 400 mA)",
            _dn3.current_edit.text() == "400", str(_fields(_dn3)))
_below = gui.ConfigDialog(None, 1, [1], chemistry=_C2.LIION)
_typed(_below.capacity_edit, "100")
assert_true("a 100 mAh cell derives 50 mA (the panel's lowest), not 20",
            _below.current_edit.text() == "50", _below.current_edit.text())
_typed(_below.current_edit, "30")
assert_true("a typed 30 mA is refused on Save, naming the panel's 50 mA",
            _below.build_request() is None
            and "50mA" in _below.error_label.text(), _below.error_label.text())

# The bottom-bar Stop's wording follows the link, and a Yes is
# re-checked - if the link changed while the box was open, the person is asked
# the question that is now true.
_hold4 = _th18.Event()
n2b_ctl = gui.CaptureController(gui.SampleBridge(), gui.BleSource(), [1],
                                "n2b", tempfile.mkdtemp())
n2b_ctl._thread = _th18.Thread(target=_hold4.wait, daemon=True)
n2b_ctl._thread.start()
n2b_win = gui.MainWindow(n2b_ctl, [1], gui_theme.DARK)
n2b_ctl.cap = _fake_cap({1}, s1=_busy)
assert_true("link up: the status line says the slots are being stopped",
            "slots are stopped" in n2b_win._stopping_text())
n2b_ctl.cap.link_ok = False
_st = n2b_win._stopping_text()
assert_true("link down: it says nothing is sent to the charger, not that slots "
            "are being stopped",
            "not connected" in _st and "nothing is sent" in _st
            and "slots are stopped" not in _st, _st)
n2b_ctl.cap.link_ok = True

_real_q = gui.QMessageBox.question
_seen = []


def _flip_then_yes(parent, title, text, *a, **k):
    _seen.append(text)
    if len(_seen) == 1:
        n2b_ctl.cap.link_ok = False        # the link drops while the box is up
    return gui.QMessageBox.StandardButton.Yes


gui.QMessageBox.question = _flip_then_yes
try:
    _ok = n2b_win.confirm_stop()
finally:
    gui.QMessageBox.question = _real_q
assert_true("a Yes given while the link dropped is not taken: the box is asked "
            "again with the NOT connected wording, and that Yes counts",
            _ok is True and len(_seen) == 2
            and "This stops the run" in _seen[0]
            and "NOT connected" in _seen[1], str(_seen))

n2b_ctl.cap.link_ok = True
_seen.clear()


def _flip_then_no(parent, title, text, *a, **k):
    _seen.append(text)
    if len(_seen) == 1:
        n2b_ctl.cap.link_ok = False
        return gui.QMessageBox.StandardButton.Yes
    return gui.QMessageBox.StandardButton.No


gui.QMessageBox.question = _flip_then_no
try:
    _ok = n2b_win.confirm_stop()
finally:
    gui.QMessageBox.question = _real_q
assert_true("...and a No to the re-asked question stops nothing",
            _ok is False and len(_seen) == 2, str(_seen))
n2b_ctl.cap.link_ok = True
_seen.clear()
gui.QMessageBox.question = (lambda p, t, x, *a, **k:
                            _seen.append(x) or gui.QMessageBox.StandardButton.Yes)
try:
    _ok = n2b_win.confirm_stop()
finally:
    gui.QMessageBox.question = _real_q
assert_true("a link that does not change asks once", _ok is True and len(_seen) == 1)
_hold4.set()

# --start is gone from the GUI launcher.
_res = _sp18.run([sys.executable, str(_P18(gui.__file__)),
                  "--start", "charge"], capture_output=True, text=True,
                 timeout=60, env={**os.environ, "SITREP91_NO_MESSAGEBOX": "1",
                                  "SITREP91_LOG_DIR": tempfile.mkdtemp()})
assert_true("gui.py no longer accepts --start (argparse refuses it, exit 2)",
            _res.returncode == 2,
            f"{_res.returncode} {_res.stderr[-200:]}")
assert_true("...nor the options that only went with it",
            "--safety-timer" not in (_P18(gui.__file__)
                                    .read_text(encoding="utf-8")))

print()
print("=" * 55)
print(f"{sum(results)}/{len(results)} checks passed")
print("=" * 55)

sys.exit(0 if all(results) else 1)

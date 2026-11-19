"""
MC5000 live monitor - PySide6 + pyqtgraph front end for capture.py.

WHAT THIS IS
------------
A window showing live per-slot plots. It attaches read-only by default, but
can start and stop operations from a slot card's Configure and Start, and has
an All Stop that halts every slot. Every value it displays arrives through Capture.subscribe(), the same
seam a database logger or a report generator would use.

The core knows nothing about this file. capture.py, protocol.py and drift.py
import no Qt and have no idea a GUI exists.

THE TWO EVENT LOOPS
-------------------
bleak drives an asyncio event loop; Qt drives its own. Neither will yield the
main thread to the other, so the capture runs on a BACKGROUND thread and hands
samples to the GUI thread through a Qt signal.

That handoff is the whole design:

    capture thread                        GUI thread
    --------------                        ----------
    on_notify(frame)
      -> writes CSV row
      -> _emit(sample)
           -> bridge.sample.emit(s) ....> MainWindow.on_sample(s)
                                            appends to plot data
                                            redraws

Qt notices the two ends live on different threads and QUEUES the signal, so
on_sample runs on the GUI thread even though emit() was called from the capture
thread. That is what makes this safe without any locking of our own.

It also satisfies the constraint documented on Capture.subscribe: the callback
must return fast. Emitting a queued signal just posts an event and returns - it
never waits for the plot to redraw, so BLE frame reception is never stalled.

RUNNING IT
----------
Live, against the charger - a read-only attach that watches whatever is
running and starts nothing:

    python gui.py --slots 1 --cells 1=ENL-A-01 --name enl_a_01_watch

Without hardware, --demo replays synthetic frames through the same pipeline:

    python gui.py --demo --slots 1,2

Demo output is written to demo_runs/ rather than runs/, so fake data can never
be mistaken for a real capture. Live output goes to runs/ like any other
capture, because it IS one - same CSV, same meta.json, same drift validation.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import queue
import signal
import sys
import threading
import time
import traceback
from collections import defaultdict
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

# A crash must never be silent: the released app has no console, so hook the
# exception paths before the heavy imports below - then even a missing package
# is reported. Only when run as the program, never when tests import this file.
import crashlog
if __name__ == "__main__":
    crashlog.install()

import pyqtgraph as pg
from PySide6.QtCore import QEvent, QObject, QRect, Qt, QTimer, Signal
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFileDialog,
    QFormLayout, QGridLayout, QGroupBox, QHBoxLayout, QLabel, QLineEdit,
    QMainWindow, QMessageBox, QPlainTextEdit, QPushButton, QSizePolicy,
    QVBoxLayout, QWidget,
)

import diagnose
import gui_theme
import runfiles
from capture import (
    CHEMISTRY_BY_NAME, MODE_BY_NAME, TEMP_CUTOFF_MAX_C,
    TEMP_CUTOFF_MIN_C, TEMP_INTERVAL_S, VERIFIED_CONTROL, Capture, Log,
    RunConfig, RunGroup, RunRequest, SessionOptions, StartInjection,
    advanced_defaults, advanced_fields, c_rate_warning, configure_defaults,
    derive_current_ma, derive_storage_discharge_ma,
    control_verified, monitor_only_message, parse_cells, refuse_reason,
    run_capture, _reports_idle,
    stop_all_mode, suggest_safety_timer_min, temp_cutoff_problem,
)
from runfiles import read_setting, session_log_path, write_setting
from protocol import (CHEMISTRY_DEFAULTS, NICKEL_CHEMISTRIES, SLOT_BITMASK,
                      STORAGE_VOLTAGE_MV, VOLTAGE_GATES, Chemistry, Mode)

pg.setConfigOptions(antialias=True)

# TESTING AID - CLEAR BEFORE THE RELEASE BUILD. While release candidates are
# being checked this names the build in the window title and in the session
# log, so nobody tests an older unpacked copy by mistake. Bump it on every
# rebuild; set it to "" for the release (build_release.py says so while it is
# set). It is deliberately NOT part of runfiles.APP_VERSION, so meta.json,
# crash logs and the zip name never carry it.
RC_TAG = ""


def build_label() -> str:
    """The version as the window shows it: '1.0.0' followed by the tag while a
    tag is set, plain '1.0.0' for the release."""
    return f"{runfiles.APP_VERSION} {RC_TAG}" if RC_TAG else runfiles.APP_VERSION


# --------------------------------------------------------------------------
# the join between core and GUI
# --------------------------------------------------------------------------

# How long a slot may sit in "starting" (Start pressed, no current yet) before
# we assume the start did not take and clear it. It must comfortably exceed the
# real config-to-current latency, which on FW 2.02 is ~15 s - the charger latches
# IR and sets up before a discharge draws current - NOT a couple of seconds. Set
# too low it fires mid-start (observed on hardware: a 15 s value tripped during a
# normal ~15 s start, flickering the button back to an active Start and leaving
# a stale config behind). 60 s clears real starts with margin while still
# freeing a genuinely stuck card within a minute. Tests set it low deliberately.
STARTING_TIMEOUT = 60.0

# How long closing (or Stop) waits for the capture thread to finish: stopping
# four slots (~5 s of 0xFE and checks), the all-slot fallback if one does not
# take (~6 s more), the polling interval and the disconnect. The old 10 s was
# shorter than that worst case, and what was left over died with the process
# and left meta.json unfinished.
STOP_TIMEOUT_S = 30.0

# What Start, Stop and ALL STOP say while the link is down. Nothing is queued
# for later: a command that waited would fire whenever the link came back.
NOT_CONNECTED = "Not connected. Use the charger's panel."

# A refusal ("Not connected...") is the only explanation the person gets for a
# click that did nothing, and during an outage the reconnect loop rewrites the
# status line every 3 s, so it was on screen for under a second. A refusal is
# therefore held on the line this long; the console and the session log
# still receive every line meanwhile.
STATUS_HOLD_MS = 10000


def is_refusal(msg: str) -> bool:
    """Whether a status line is the window declining something the person just
    asked for (Start, Stop, ALL STOP) - the lines worth holding on screen."""
    return msg.startswith("cannot ") or NOT_CONNECTED in msg


def connection_marker(simulated: bool, link_live: bool,
                      name: str | None) -> tuple[str, str]:
    """What the always-visible connection marker says: (kind, text) with kind
    "connected", "disconnected" or "demo". Pure, so it is tested without a
    window. The words carry the meaning; color only backs them up."""
    if simulated:
        return "demo", "DEMO"
    if link_live:
        return "connected", f"CONNECTED  {name}" if name else "CONNECTED"
    return "disconnected", "DISCONNECTED"


# Only one SITREP91 may hold the charger. A second window used to scan for
# 2.5 minutes and then ask "is the charger awake?", when the charger was in fact
# connected to the first (on the bench, a window left open from the day
# before reconnected the moment the charger was powered on). A real session
# therefore claims a Windows named mutex before it does anything else; the
# demo (no Bluetooth at all) and --diagnose (listen-only) do not take part.
# The mutex belongs to the process, so a crash or kill releases it by itself.
INSTANCE_MUTEX = "Local\\SITREP91-charger-session"
ALREADY_OPEN_TITLE = "SITREP91 is already open"
ALREADY_OPEN_TEXT = (
    "SITREP91 is already open on this computer (in another window, another "
    "folder, or minimized), so this one is closing instead of fighting it for "
    "the charger.\n\n"
    "Only one SITREP91 can talk to the charger at a time. Find the other "
    "window in the taskbar and close it, then open this one again. If you "
    "cannot find it, end the pythonw.exe process in Task Manager.")
_ERROR_ALREADY_EXISTS = 183
_instance_handle = None


def acquire_single_instance(name: str | None = None) -> bool:
    """Claim this computer's one SITREP91 session. True: this process may go
    on. False: another SITREP91 already holds it. Never raises, and never
    blocks the app because the check itself failed (an unexpected error means
    True). Off Windows there is no check. `name` is for tests; the
    SITREP91_INSTANCE_NAME environment variable does the same for a child
    process, so a test run is never stopped by a real window left open."""
    global _instance_handle
    if sys.platform != "win32":
        return True
    name = name or os.environ.get("SITREP91_INSTANCE_NAME") or INSTANCE_MUTEX
    try:
        import ctypes
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.CreateMutexW.restype = ctypes.c_void_p
        k32.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_int,
                                     ctypes.c_wchar_p]
        k32.CloseHandle.argtypes = [ctypes.c_void_p]
        handle = k32.CreateMutexW(None, False, name)
        if not handle:
            return True
        if ctypes.get_last_error() == _ERROR_ALREADY_EXISTS:
            k32.CloseHandle(handle)
            return False
        _instance_handle = handle
        return True
    except Exception:                                   # noqa: BLE001
        return True


def release_single_instance() -> None:
    """Let go of the claim (tests; a normal exit releases it with the process)."""
    global _instance_handle
    if _instance_handle is None or sys.platform != "win32":
        return
    try:
        import ctypes
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.CloseHandle.argtypes = [ctypes.c_void_p]
        k32.CloseHandle(_instance_handle)
    except Exception:                                   # noqa: BLE001
        pass
    _instance_handle = None

# Added to the window title in --demo, so a demo is never mistaken for a real
# charger.
DEMO_TITLE_NOTE = " · DEMO (no charger)"
# How long the app records before ending its own capture. None = never: the
# window is meant to stay open for days. (It was once 8 h, until an
# idle window quietly ended its capture and grayed every card; the cap counted
# from connect, so it could also land hours into someone's run.) Passing
# --max-hours sets one, and even then it only ends the recording - it never
# stops a run.
DEFAULT_MAX_HOURS = None


def derive_slot_state(status, armed: bool, finished: bool,
                      starting: bool = False, torn_down: bool = False,
                      measuring_ir: bool = False) -> str:
    """
    One slot's state, from what we know about it. Pure and testable - the card
    renders from this and the controller decides what actions are legal.

      measuring - the pre-flight IR sampler owns the slot, arming and stopping
                  it to collect resistance samples before the real run
      latched   - the run is over but the charger is still echoing its last
                  reading; only an explicit stop clears it
      running   - current is flowing (physical reality, wins over everything
                  except a reading known to be a phantom)
      starting  - a start was issued; waiting for current to confirm it
      armed     - a config is pending but not started
      finished  - a run completed and the slot went idle
      empty     - no cell present
      idle      - a cell is present and resting
      unknown   - no frame has arrived yet

    Priority is deliberate: a slot that is physically running cannot also be
    "starting"; a slot mid-start is neither "armed" nor "idle"; and arming a
    finished slot for a new run reads as "armed", not "finished". "starting" is
    the bridge between hitting Start and current actually flowing - the config
    is already committed, so the card must not offer Start again.

    "latched" outranks everything, including a current reading, because on this
    firmware a current reading can LIE. When the charger's safety timer ends a
    run it zeroes the slot's counters but leaves voltage, current and state
    echoing their last pre-teardown values - measured on hardware at a dead-flat
    4.138 V / 502 mA for five minutes after the run was over. Capture.torn_down
    is the capture layer's finding that this has happened, and it holds only
    while the phantom does, so a slot that restarts or genuinely goes idle
    leaves the state behind on its own.

    It is a state rather than a decoration on "finished" because it is the one
    ended slot that still has something to do: Stop clears the latch, and until
    it is cleared refuse_reason sees the phantom current and will not start the
    slot. An earlier attempt rendered it as "armed (latched)" with the Stop
    button forced on, which left a configured slot showing no way to start at
    all.
    """
    # Outranks everything, including a current reading. The sampler arms and
    # stops the slot repeatedly, so every other state flickers while it runs -
    # and none of them describes what is actually happening. It cannot collide
    # with "latched": the capture suspends recording for a slot being sampled,
    # so torn_down is never set for one.
    if measuring_ir:
        return "measuring"
    if torn_down:
        return "latched"
    if status is not None and status.current_ma > 0:
        return "running"
    if starting:
        return "starting"
    if armed:
        return "armed"
    if finished:
        return "finished"
    if status is None:
        return "unknown"
    if status.state == "empty":
        return "empty"
    return "idle"


def counters_zeroed(status) -> bool:
    """The charger's capacity and elapsed counters both read 0 - what a slot
    reports after it has been torn down (a timer or capacity limit, or a stop)."""
    return status.capacity_mah == 0 and status.elapsed_seconds == 0


def card_frames(live, state: str, last_good,
                unwatched_end: bool = False) -> tuple[dict, bool]:
    """
    Which frame supplies each reading on a slot's card: ({column: frame}, held).

    The charger's teardown zeroes capacity, elapsed time and resistance the
    moment a run ends, so a card that simply shows the live frame goes from
    "1971 mAh" to "0 mAh" at exactly the moment the number matters. `last_good`
    is the capture's last frame with the counters still advancing; `held` is
    True when any reading comes from it, so the card can say so.

      latched   the charger ended the run and keeps echoing its last voltage
                and current, which are the same stale values - every reading
                is the last good one
      finished  with the counters zeroed (an explicit stop): voltage and
                current are live and real, capacity and resistance are the
                last good ones
      disconnected  the session is over: the last frame received, with the
                last good capacity and resistance if a stop had zeroed them -
                all of it held, since none of it is live any more
      otherwise live, unchanged - a normal end keeps its counters intact

    `unwatched_end`: the run ended while the link was down, so the capture's
    last good frame is all there is to show and anything newer would mix a
    resting voltage into a run it never watched finish. Every reading is the
    last good one, whether the slot reads latched or finished.
    """
    cols = ("Volts", "Current", "Capacity", "Resistance")
    if (unwatched_end and last_good is not None
            and state in ("latched", "finished")):
        return {c: last_good for c in cols}, True
    if state == "disconnected":
        if last_good is not None and live is not None and counters_zeroed(live):
            return ({"Volts": live, "Current": live,
                     "Capacity": last_good, "Resistance": last_good}, True)
        return {c: live for c in cols}, True
    if last_good is not None:
        if state == "latched":
            return {c: last_good for c in cols}, True
        if state == "finished" and counters_zeroed(live):
            return ({"Volts": live, "Current": live,
                     "Capacity": last_good, "Resistance": last_good}, True)
    return {c: live for c in cols}, False


# Human-readable chemistry names for the card (the CLI/config uses the terse
# forms like "liion"). Falls back to the enum name for anything unlisted.
CHEM_DISPLAY = {
    Chemistry.LIION: "Li-ion", Chemistry.LIION_HV: "Li-ion HV",
    Chemistry.LIFEPO4: "LiFePO4", Chemistry.NIMH: "NiMH",
    Chemistry.NICD: "NiCd", Chemistry.ENELOOP: "Eneloop",
    Chemistry.NIZN: "NiZn", Chemistry.RAM: "RAM",
    Chemistry.LTO: "LTO", Chemistry.NAION: "Na-ion",
}


def group_letter(group_id: int) -> str:
    """A, B, C ... for a group id. One place, so cards and the controller agree."""
    return chr(ord("A") + (group_id - 1) % 26)


def _rate_and_end(request) -> str:
    """The current, and the voltage the run is heading for - which depends on
    the action. A discharge heads for its cutoff; a charge for its target
    voltage; a storage run for its storage voltage. The card used to show the
    cutoff on every action, so a charge read as '1000 mA -> 900 mV', which says
    the charge stops at 0.9 V. A nickel charge ends on a
    -dV drop rather than at a voltage, so it names one only when the target was
    changed; showing the 1650 mV default would imply the wrong ending."""
    current = f"{request.current_ma} mA"
    if request.mode is Mode.STORAGE:
        second = getattr(request, "discharge_current_ma", None)
        if second is not None:
            current = f"{request.current_ma} / {second} mA"
        volts = request.storage_mv or STORAGE_VOLTAGE_MV.get(request.chemistry)
    elif request.mode is Mode.CHARGE:
        volts = request.target_mv
        if volts is None and request.chemistry not in NICKEL_CHEMISTRIES:
            volts = CHEMISTRY_DEFAULTS[request.chemistry]["target_mv"]
    else:
        volts = request.effective_cutoff_mv()
    return f"{current} → {volts} mV" if volts else current


def describe_config(request, full: bool, unit: str = "C") -> str:
    """
    The card's config line for a RunRequest: chemistry and action always, plus
    the rate and cutoff when `full` (armed/starting, where the exact numbers
    matter). The chemistry-and-action half is the persistent "set up for"
    identity shown from arming through finished.
    """
    chem = CHEM_DISPLAY.get(request.chemistry, request.chemistry.name.title())
    action = request.mode.name.replace("_", "-").title()
    head = f"{chem}  ·  {action}"
    if not full:
        return head
    line = f"{head}  ·  {_rate_and_end(request)}"
    tc = getattr(request, "temp_cutoff_c", None)
    if tc is not None:
        line += f"  ·  stop at {format_temp(tc, unit)}"
    return line


# --------------------------------------------------------------------------
# slot temperature
# --------------------------------------------------------------------------
#
# THE LOAD-BEARING RULE: nothing here ranks, compares, flags or colors one
# slot against another. A middle bay reads ~1 C hotter than an edge bay by
# POSITION ALONE when all four are loaded, and a genuinely failing cell read
# only +2 C against a position-matched healthy one - and 1 C COOLER than a
# healthy cell in a middle bay (measured with a known-bad cell). So "spot the odd
# one out" fingers a good cell and clears the dead one. Every function below
# takes ONE slot's facts and never sees another slot's; the reading is shown
# in the same neutral color whatever its value.

# Files are always Celsius; this is display only (settings.ini [display]).
TEMP_UNITS = ("C", "F")
_DISPLAY_SECTION = "display"

# A reading older than this shows its age. 1.5 polls, so ONE dropped reply
# (~3 % of them) is visible as "1 min ago" rather than hidden.
TEMP_STALE_AFTER_S = 1.5 * TEMP_INTERVAL_S

TEMP_TOOLTIP = (
    "Slot temp: the charger's own sensor in this bay, not the cell itself.\n"
    "Once a run is warm it reads about 5 °C above the cell's surface\n"
    "(measured against a probe taped to one cell).\n"
    "Uncalibrated, and reported in whole degrees.\n"
    "Reported only while a run is going; its age is shown when it is old.")
MIDDLE_BAY_TOOLTIP = ("Middle bays read about 1 °C warmer when all four slots "
                      "are loaded. Compare like positions.")
TEMP_IDLE_TOOLTIP = "Reported only while a run is going."

# A resting slot's voltage is NOT a live reading: the charger converts once
# when the slot changes state (a cell in, a run starting or ending) and
# repeats that value until the next change - the front panel too (measured
# on hardware). So the card says
# so wherever a cell sits with no run going. The number is not dimmed: it is
# a real reading, just not a current one, and dimming means "the last good
# reading of a run that ended".
HELD_VOLTS_STATES = ("idle", "armed", "finished")

# The caption on a card whose run ended while the link was down. It states the
# one thing the numbers cannot: they are the last readings RECEIVED, so
# capacity and time may be short of what the run really delivered.
UNWATCHED_END_NOTE = ("Last readings received: this run ended while the link "
                      "was down, so capacity and time may be short.")
HELD_VOLTS_HEADER = "Volts · held"
HELD_VOLTS_TOOLTIP = (
    "The charger takes this reading when the slot last changed state (a cell "
    "put in, a run started or ended) and repeats it until the next change. "
    "It does not follow a resting cell. Just after a run it may move in the "
    "first couple of minutes (up after a discharge, down after a charge), "
    "then it holds.")
CUTOFF_TOOLTIP = (
    "A line you choose, below the charger's own 70 °C limit.\n"
    "Active only while this app is connected - the charger itself does not\n"
    "enforce it. The charger's safety timer and thermal limit remain the\n"
    "backstop. Leave blank for no cutoff.")

# The charger's own reasons for stopping, in plain words where the raw name
# is not plain already. Only alerts with a PROVEN name (protocol.ALERT_NAMES)
# can appear here; the over-temperature codes are not proven yet, so they reach
# the card as "alert code N" and have no entry.
CHARGER_STOP_WORDS = {
    # Wire code 13, seen on a real charger.
    "fully charged": "the cell is already full",
    # Wire code 9, seen on a real charger: a cell pulled mid-run.
    "connection break": "the cell lost contact (removed, or a bad connection)",
}


def load_temp_unit() -> str:
    """The display unit from settings.ini, "C" unless it says "F"."""
    unit = (read_setting(_DISPLAY_SECTION, "temperature_unit") or "C").upper()
    return unit if unit in TEMP_UNITS else "C"


def save_temp_unit(unit: str) -> bool:
    return write_setting(_DISPLAY_SECTION, {"temperature_unit": unit})


def to_unit(temp_c: float, unit: str) -> float:
    return temp_c * 9 / 5 + 32 if unit == "F" else temp_c


def from_unit(value: float, unit: str) -> int:
    """A value typed in `unit`, as whole degrees C (what is stored)."""
    return round((value - 32) * 5 / 9) if unit == "F" else round(value)


def format_temp(temp_c: float, unit: str = "C") -> str:
    return f"{round(to_unit(temp_c, unit))} °{unit}"


def format_age(seconds: float) -> str:
    seconds = max(0, int(seconds))
    if seconds < 60:
        return f"{seconds} s ago"
    if seconds < 3600:
        return f"{seconds // 60} min ago"
    return f"{seconds // 3600} h ago"


def temp_view(state: str, reading: int | None, age_s: float | None,
              peak: int | None, unit: str = "C", *, note: str | None = None,
              cutoff_c: int | None = None, trip_c: int | None = None,
              stopped_by_cutoff: bool = False, blind: bool = False,
              charger_alert: str | None = None,
              stopped_by_user: bool = False,
              unwatched_end: bool = False) -> tuple[str, str, bool]:
    """
    What ONE slot's card shows for temperature: (value, detail line, grayed).

    Pure, and deliberately given nothing about any other slot (see the rule
    above). `state` is derive_slot_state's; `reading` the latest 0x55 value
    for THIS slot's current run; `age_s` how old it is.

      running           the reading, with its age once stale; "—" until the
                        first reading arrives
      finished/latched  the last reading, grayed, with the run's peak - or,
                        when the run ended while the link was down
                        (`unwatched_end`), the highest seen, since the peak
                        only covers what was received
      disconnected     the last reading, grayed, with its age - the session
                        is over, so nothing here is live
      anything else     "—" (reported only while a run is going)
    """
    parts = []
    if state == "disconnected":
        value = format_temp(reading, unit) if reading is not None else "—"
        greyed = reading is not None
        if reading is not None and age_s is not None:
            value += f" · {format_age(age_s)}"
        if peak is not None:
            parts.append(f"Peak {format_temp(peak, unit)}")
    elif state in ("finished", "latched"):
        value = format_temp(reading, unit) if reading is not None else "—"
        greyed = reading is not None
        if stopped_by_user and not charger_alert:
            parts.append("Stopped by you")
        if charger_alert:
            words = CHARGER_STOP_WORDS.get(charger_alert, charger_alert)
            parts.append(f"Stopped by charger: {words}")
        if stopped_by_cutoff and trip_c is not None:
            parts.append(f"Stopped by your cutoff at {format_temp(trip_c, unit)}")
        if peak is not None and unwatched_end:
            parts.append(f"Highest seen {format_temp(peak, unit)}, "
                         f"may have gone higher")
        elif peak is not None:
            parts.append(f"Peak {format_temp(peak, unit)}")
    elif state in ("running", "measuring", "starting"):
        greyed = False
        if reading is None:
            value = "—"
        else:
            value = format_temp(reading, unit)
            if age_s is not None and age_s >= TEMP_STALE_AFTER_S:
                value += f" · {format_age(age_s)}"
        if note:
            parts.append(note)
        if trip_c is not None and stopped_by_cutoff:
            parts.append(f"Stopped by your cutoff at {format_temp(trip_c, unit)}")
        elif trip_c is not None:
            parts.append(f"At or above your {format_temp(cutoff_c or trip_c, unit)}"
                         f" cutoff - not stopped: this run was not started here")
        elif blind:
            parts.append("Cutoff blind: no temperature reading")
        elif cutoff_c is not None:
            parts.append(f"Cutoff {format_temp(cutoff_c, unit)} (while connected)")
    else:
        value, greyed = "—", False
    return value, "  ·  ".join(parts), greyed


def temp_staircase(points, interval_s: float, unit: str = "C"):
    """
    Chart data for one slot's temperature: (xs, ys) drawn as a STAIRCASE,
    because each reading is a whole degree held until the next one - a sloped
    line would invent values between polls.

    A SINGLE missed reply (a gap of about two intervals) is bridged: the step
    just runs on. A longer gap is a real absence - a lost link, a slot that
    stopped - so the line BREAKS (a NaN, drawn with connect="finite"): the
    last step runs one interval on, then nothing until readings resume.
    `points` are (elapsed_s, temp_c) in time order.
    """
    nan = float("nan")
    # A poll can never be closer than a status cycle, so --temp-interval 0
    # ("every poll") still bridges a single miss.
    bridge = 2.5 * max(interval_s, 2.0)
    xs, ys = [], []
    for i, (x, c) in enumerate(points):
        v = to_unit(c, unit)
        if i:
            px, pc = points[i - 1]
            pv = to_unit(pc, unit)
            if x - px > bridge:
                xs += [px + max(interval_s, 2.0), nan]
                ys += [pv, nan]
            else:
                xs.append(x)
                ys.append(pv)
        xs.append(x)
        ys.append(v)
    return xs, ys


class SampleBridge(QObject):
    """
    Carries data from the capture thread to the GUI thread.

    A QObject exists purely so it can own signals. It is created on the GUI
    thread, but emit() is called from the capture thread - which Qt supports:
    signal emission is thread-safe, and a cross-thread connection is delivered
    as a queued event on the receiver's thread.
    """

    sample = Signal(object)     # a capture.Sample
    status = Signal(str)        # human-readable progress line
    stopped = Signal(str)       # capture ended; carries the reason


# --------------------------------------------------------------------------
# data sources
# --------------------------------------------------------------------------

class DemoSource:
    """
    Replays synthetic frames through a real Capture, with no BLE anywhere.

    The frames are built the same way test_capture.py builds them, which is the
    same shape the charger really sends (0x91, 24 bytes, length byte 22 -
    confirmed against FW 2.02 hardware).
    """

    # Nothing here is real: no scan, no connection, no packet, ever. The
    # controller reads this to refuse every route to the charger (ALL STOP
    # included) whether or not the replay is still running.
    simulated = True

    def __init__(self, slots, interval=0.4, auto_run=True):
        self.slots = slots
        self.interval = interval
        # auto_run True: every slot discharges from t=0 (the standalone demo,
        # ends at floor voltage). auto_run False: every slot starts IDLE and
        # comes alive only when a StartInjection arrives - the interactive
        # independence demo, which runs until stopped. Injection is honored in
        # both; the flag only decides the starting state and the end condition.
        self.auto_run = auto_run

    def run(self, cap: Capture, stop: threading.Event, status,
            all_stop: threading.Event | None = None, inject_queue=None,
            stop_queue=None):
        status("demo source: replaying synthetic frames (no hardware)")
        voltage = {s: 4150 - 30 * i for i, s in enumerate(self.slots)}
        running = {s: self.auto_run for s in self.slots}
        current = {s: (1500 if running[s] else 0) for s in self.slots}
        elapsed = {s: 0 for s in self.slots}
        # Synthetic slot temperatures (for screenshots and offline testing):
        # each run warms from a warm-charger start toward a load plateau.
        # The middle bays settle a degree higher, as the real ones do under
        # four-slot load - which is exactly why the card never compares them.
        temp = {s: DEMO_TEMP_START_C for s in self.slots}
        tick = 0

        while not stop.is_set():
            if all_stop is not None and all_stop.is_set():
                all_stop.clear()
                status("demo: ALL STOP - operations halted")
                # Zero the current but keep emitting: the real capture keeps
                # polling after a halt and records each slot going idle, which
                # is what finished-detection needs to end the run "done". A slot
                # that simply stopped sending frames would never be seen to
                # stop. (Floor-voltage detection below then retires it.)
                for s in self.slots:
                    current[s] = 0

            # A config injected for a slot brings it alive - same queue the real
            # session drains, so the arm -> start flow demos with no hardware.
            if inject_queue is not None:
                while True:
                    try:
                        item: StartInjection = inject_queue.get_nowait()
                    except queue.Empty:
                        break
                    if item.slot in self.slots:
                        running[item.slot] = True
                        current[item.slot] = item.request.current_ma
                        elapsed[item.slot] = 0
                        temp[item.slot] = DEMO_TEMP_START_C
                        # What the real session does on an injected start.
                        cap.begin_slot_run(item.slot, item.request,
                                           cell_id=item.cell_id,
                                           group=item.group)
                        status(f"demo: slot {item.slot} started "
                               f"({item.request.mode.name.lower()} "
                               f"{item.request.current_ma} mA)")

            # A per-slot stop idles just that slot - it keeps reporting (0 mA,
            # resting), so finished-detection sees it stop, while every other
            # slot runs on. Mirrors 0xFE-to-one-bitmask on real hardware.
            if stop_queue is not None:
                while True:
                    try:
                        s_stop = stop_queue.get_nowait()
                    except queue.Empty:
                        break
                    if s_stop in self.slots and running[s_stop]:
                        current[s_stop] = 0
                        cap.note_stop_sent(s_stop)
                        status(f"demo: slot {s_stop} stopped (others untouched)")

            for s in self.slots:
                # Every slot always reports, exactly as the charger's polling
                # does: a running slot discharges and sags, an idle slot just
                # rests at voltage with no current. Emitting idle frames is what
                # lets a not-yet-started slot show as a present cell, so the
                # start guard (refuse_reason) can see it and allow a start.
                if running[s]:
                    elapsed[s] += 30
                    # a gentle discharge curve, steepening as it empties
                    sag = 12 if voltage[s] > 3500 else 45
                    voltage[s] = max(3000, voltage[s] - sag)
                    cur = current[s]
                    if voltage[s] <= 3000:
                        running[s] = False      # hit the floor; now resting
                else:
                    cur = 0
                frame = build_status_frame(
                    slot=s, current_ma=cur, voltage_mv=voltage[s],
                    capacity_mah=round(cur * elapsed[s] / 3600),
                    elapsed_s=elapsed[s], resistance_mohm=38 + s,
                )
                cap.on_notify(None, bytearray(frame))

            # Temperature, every other tick and only for a running slot - the
            # charger fills byte 14 only while a run is going. Honors the
            # capture's own switch, exactly as the real poll loop does.
            tick += 1
            if cap.temp_interval_s is not None and tick % 2 == 0:
                for s in self.slots:
                    if running[s] and current[s] > 0:
                        plateau = DEMO_TEMP_PLATEAU_C + (1 if s in (2, 3) else 0)
                        temp[s] += (plateau - temp[s]) * 0.25
                        cap.on_notify(None, bytearray(build_temperature_frame(
                            s, int(temp[s]))))
            # The temperature cutoff's stops, which the real session sends as
            # a per-slot 0xFE: the slot idles, the others run on.
            for s_cut in sorted(cap.pending_cutoff_stops):
                cap.pending_cutoff_stops.discard(s_cut)
                cap.mark_cutoff_stopped(s_cut)
                if s_cut in current:
                    current[s_cut] = 0
                status(f"demo: slot {s_cut} stopped by the temperature cutoff")
            cap.update_cutoff_blind()

            # The auto-run demo ends when every cell reaches the floor. The
            # interactive demo is user-driven, so it runs until stopped - idle
            # slots are waiting to be started, not finished.
            if self.auto_run and all(v <= 3000 for v in voltage.values()):
                status("demo source: cells reached floor voltage")
                return "done"

            stop.wait(self.interval)

        return "stopped"


DEMO_TEMP_START_C = 29.0
DEMO_TEMP_PLATEAU_C = 36


def build_temperature_frame(slot, temp_c, status=3):
    """A 36-byte 0x55 response in the FW 2.02 layout: byte 4 run status
    (3 = running, so byte 14 is filled), byte 14 temperature in whole C."""
    raw = [0x00] * 36
    raw[0], raw[1], raw[2], raw[3] = 0x0F, 0x22, 0x55, SLOT_BITMASK[slot]
    raw[4] = status
    raw[14] = temp_c
    raw[-1] = sum(raw[2:-1]) % 256
    return bytes(raw)


def build_status_frame(slot, current_ma, voltage_mv, capacity_mah,
                       elapsed_s, resistance_mohm):
    """A 24-byte 0x91 status frame in the FW 2.02 layout."""
    channel = {1: 0x01, 2: 0x02, 3: 0x04, 4: 0x08}[slot]

    def be16(v):
        return [(v >> 8) & 0xFF, v & 0xFF]

    raw = [0x00] * 24
    raw[0], raw[1], raw[2], raw[3] = 0x0F, 22, 0x91, channel
    raw[4:6] = be16(current_ma)
    raw[6:8] = be16(voltage_mv)
    raw[10:12] = be16(capacity_mah)
    raw[14:16] = be16(elapsed_s)
    raw[16:18] = be16(resistance_mohm)
    raw[-1] = sum(raw[2:-1]) % 256
    return bytes(raw)


NO_BLUETOOTH_NEXT_STEP = (" Turn Bluetooth on (or plug in an adapter), then "
                          "start SITREP91 again.")


def bluetooth_unavailable(exc: BaseException) -> str | None:
    """
    Plain words when `exc` is "this PC has no usable Bluetooth", else None.

    Windows' scanner raises bleak's BleakBluetoothNotAvailableError for both no
    adapter at all and a radio that is switched off (simulated on the bench; the
    GUI used to show that error's repr and point at a terminal the packaged app
    does not have). Matched by TYPE only: diagnose.scan_problem also matches on
    words, which is right for a scan and too loose for an error from the middle
    of a session.
    """
    try:
        from bleak.exc import BleakBluetoothNotAvailableError
    except Exception:                                # noqa: BLE001
        return None
    if isinstance(exc, BleakBluetoothNotAvailableError):
        return diagnose.NO_ADAPTER + NO_BLUETOOTH_NEXT_STEP
    return None


class BleSource:
    """
    The real thing: runs capture.py's full capture lifecycle - scan, sessions,
    reconnects - on the controller's worker thread.

    asyncio.run() gives this thread its own event loop, which is the resolution
    of the two-event-loops problem: Qt owns the main thread, bleak owns this
    one, and samples cross between them on the bridge's queued signal.

    run_capture is shared with the command line, so the GUI cannot drift from
    what `python capture.py --attach` does. The default SessionOptions() is an
    attach - start nothing, disturb nothing - which means a monitor built on
    this source is *incapable* of touching a running operation unless someone
    deliberately hands it options that start one.
    """

    def __init__(self, options: SessionOptions | None = None):
        self.options = options or SessionOptions()
        self.last_error: str | None = None
        self.error_is_plain = False     # last_error is already a sentence
        self.crash_log = None           # where the traceback of a failure went

    def run(self, cap, stop, status, all_stop=None, inject_queue=None,
            stop_queue=None):
        # A new session (Retry) starts with a clean slate: an earlier failure's
        # wording and crash-log path must not be shown for a later ending.
        self.last_error = None
        self.error_is_plain = False
        self.crash_log = None
        status("scanning for the charger ...")
        try:
            reason = asyncio.run(
                run_capture(cap, self.options, stop_requested=stop.is_set,
                            all_stop_event=all_stop, inject_queue=inject_queue,
                            stop_queue=stop_queue,
                            # A GUI monitor is a persistent control surface: it
                            # stays live when every slot goes idle, so you can
                            # start the next one. It ends only when the window
                            # is closed (stop) or the link drops.
                            run_until_stopped=True))
        except Exception as e:                       # noqa: BLE001
            plain = bluetooth_unavailable(e)
            if plain:
                # Not a bug and nothing to debug: say what is wrong and what
                # to do, with no traceback and no "capture failed".
                self.last_error = plain
                self.error_is_plain = True
                status(plain)
                return "error"
            # The status line is transient - "capture ended" overwrites it
            # moments later - so the full story goes to the crash log, which
            # the session log also carries. There is no terminal in the
            # packaged app: print_exc() here went nowhere.
            self.crash_log = crashlog.record(type(e), e, e.__traceback__,
                                             "capture thread")
            self.last_error = f"{type(e).__name__}: {e}"
            status(f"capture failed: {self.last_error}"
                   + (f" (details: {self.crash_log})" if self.crash_log
                      else ""))
            return "error"
        if reason == "no-device":
            status("no MC5000 found - is the charger awake and in range?")
        return reason


# --------------------------------------------------------------------------
# controller
# --------------------------------------------------------------------------

class StatusLog(Log):
    """
    capture.py's Log, additionally mirrored to the bridge's status signal, so
    "scanning ...", "connected" and reconnect lines reach the window instead of
    living only in the terminal. Emitting a queued signal is cheap, so this
    stays safe to call from the capture thread.
    """

    def __init__(self, emit, path=None):
        super().__init__(path)
        self._emit = emit

    def __call__(self, msg: str):
        super().__call__(msg)
        text = msg.strip()
        if text and not text.startswith("="):
            self._emit(text)

    def note(self, msg: str):
        """Into the file (and terminal) only: for a line the window is
        already showing itself, so it is not emitted back to it twice."""
        Log.__call__(self, msg)


class CaptureController:
    """
    Owns the Capture and the thread it runs on. The GUI talks only to this.

    Run control (start/stop/configure a slot on the charger) belongs here when
    it arrives, so the window never touches protocol.py directly.
    """

    def __init__(self, bridge: SampleBridge, source, slots, name, outdir,
                 chemistry=Chemistry.LIION, cells=None, fallback_dir=None):
        self.bridge = bridge
        self.source = source
        self.slots = slots
        self.name = name
        # The logs folder. Read by the capture for every run it opens, so a
        # change (set_outdir) applies from the next run.
        self.outdir = Path(outdir)
        # Where a run moves if writing to `outdir` fails mid-run: the local
        # default. The public app warns and carries on rather than crash.
        self.fallback_dir = Path(fallback_dir) if fallback_dir else None
        self.chemistry = chemistry
        # Cell identity is per slot. Nothing in the GUI sets this yet; when a
        # slot gains a cell picker, the mapping arrives here.
        self.cells = cells
        # Whether this controller will START an operation rather than watch
        # one. Read off the source's options so there is exactly one place
        # that decides it - a source without options (the demo) attaches.
        self.options = getattr(source, "options", None)
        self.starts_runs = bool(self.options and self.options.start)
        self.cap: Capture | None = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._all_stop = threading.Event()
        # Armed slots' start configs, handed to the live session to inject with
        # no init (probe_independent.py). Filled by start_slot,
        # drained by the capture's polling loop. A plain thread-safe queue: the
        # Qt thread puts, the bleak thread gets.
        self._inject_queue: queue.Queue = queue.Queue()
        # Per-slot stop requests (slot numbers). Filled by stop_slot, drained by
        # the polling loop, which sends 0xFE to that slot's bitmask - halting it
        # alone, no init, others untouched (probe_slot_stop.py).
        self._stop_queue: queue.Queue = queue.Queue()
        self._pending_subscribers = []
        # Per-slot pending configs (the "armed" state). slot -> RunRequest.
        # A slot is armed when the user has configured it but not yet started
        # it; starting injects the config into the live session. This is the
        # backbone of per-slot independence - each slot armed/started on its
        # own schedule. See derive_slot_state.
        self._armed: dict[int, RunRequest] = {}
        # Slots between hitting Start and current confirming the run - the
        # "starting" window. slot -> (monotonic start time, its request). The
        # config is committed here (popped from _armed) the instant Start is
        # pressed, so it is deterministic: after a stop a slot always lands in
        # the same place, no matter how briefly it ran. If current never comes,
        # a timeout (STARTING_TIMEOUT) drops it - the config is consumed, and a
        # retry goes back through Configure like any other start.
        self._starting: dict[int, tuple[float, RunRequest]] = {}
        # True once the capture thread has ended: nothing on a card is live
        # any more, and every card says so (see state_for).
        self.session_ended = False
        # The last config SAVED for a slot, kept for display: what the slot is
        # set up for (chemistry + action), shown on the card from arming onward
        # and held until the next Save. Unlike _armed, start/stop/finish never
        # clear it - so a finished slot, whose _armed config was consumed on
        # start, still reads its identity at a glance after a break away.
        self._last_config: dict[int, RunRequest] = {}
        # Groups: config'd-together slots that can be started as one. Editing a
        # member breaks its group (groups are a convenience, not a contract).
        # group id -> set of slots. Kept here so it survives across dialogs. A
        # new group takes the LOWEST free id (so the letter after breaking A is
        # A again, not the next-in-sequence); an existing group keeps its id
        # while it lives, including across a reconfigure (see group()).
        self._groups: dict[int, set] = {}

    def subscribe(self, fn):
        """
        Register a consumer before the capture exists.

        The Capture is not built until start(), so anything wanting samples
        from the very first frame has to queue up here. Opt-in database
        logging will attach this way.
        """
        self._pending_subscribers.append(fn)
        if self.cap is not None:
            self.cap.subscribe(fn)

    def _run_request(self) -> RunRequest | None:
        """What this controller will ask a slot to do, or None for an attach."""
        if not self.starts_runs:
            return None
        o = self.options
        return RunRequest(
            mode=MODE_BY_NAME[o.mode] if o.mode else (
                Mode.DISCHARGE if o.start == "discharge" else Mode.CHARGE),
            chemistry=self.chemistry,
            current_ma=o.current,
            capacity_mah=o.capacity,
            safety_timer_min=o.safety_timer,
            cutoff_mv=o.cutoff_mv,
            target_mv=o.target_mv,
            temp_cutoff_c=o.temp_cutoff,
        )

    # ---------- per-slot arming (the independence model) ----------

    def state_for(self, slot: int, status, finished: bool) -> str:
        """
        One slot's state from an explicit status - the SINGLE place slot state
        is decided, shared by slot_state (reading cap.latest) and the sample
        path (using the fresh Sample). Resolves the "starting" window here, on
        the Qt thread that owns these dicts: current flowing confirms the start
        and clears it; a start that never draws current within STARTING_TIMEOUT
        is dropped (consume-on-start holds - reconfigure to retry). Keeping this
        in one method is what stops the two render paths from disagreeing.
        """
        # The session is over: whatever the last frame said, nothing on the
        # card is live, and no action can reach the charger. Seen on the bench:
        # cards froze claiming "running" with an enabled Stop after the
        # bottom-bar Stop had disconnected.
        if self.session_ended:
            return "disconnected"
        running = status is not None and status.current_ma > 0
        # A run the card's Stop ended reads finished as soon as its current
        # is gone, rather than "idle" with zeroed readings until the capture's
        # finished-detection catches up (card_frames then holds its last good
        # capacity and resistance, and the card says "Stopped by you").
        if (not running and self.cap is not None
                and slot in getattr(self.cap, "user_stopped", ())):
            finished = True
        # The capture declared this slot's start failed (confirm_starts): the
        # config never took, so hand it back ARMED - one press of Start
        # retries, no trip through Configure. The capture has already logged
        # why, which the status line shows.
        failed = getattr(self.cap, "start_failed", None) if self.cap else None
        if failed and slot in failed and slot in self._starting:
            _t, req = self._starting.pop(slot)
            self._armed.setdefault(slot, req)
            failed.discard(slot)
        if slot in self._starting:
            if running:
                self._starting.pop(slot, None)
            elif time.monotonic() - self._starting[slot][0] > STARTING_TIMEOUT:
                self._starting.pop(slot, None)
        # torn_down comes from the capture, never re-derived here: it is the
        # same finding that drives finished-detection, and two derivations of
        # one fact is how the render paths drifted apart before.
        return derive_slot_state(status, slot in self._armed, finished,
                                 starting=slot in self._starting,
                                 torn_down=bool(self.cap
                                                and slot in self.cap.torn_down),
                                 measuring_ir=bool(self.cap
                                                   and slot in self.cap.measuring_ir))

    def slot_latched(self, slot: int) -> bool:
        """
        Whether this slot is echoing a phantom reading after a teardown.

        Straight from Capture.torn_down - the capture layer's finding, never
        re-derived here. The card needs it separately from the state because a
        latched slot reads "finished" but is the ONE finished slot that still
        has something to stop: only an explicit stop clears the latch, and
        until it does, refuse_reason sees the latched current and will not let
        the slot start again.
        """
        return bool(self.cap and slot in self.cap.torn_down)

    def slot_state(self, slot: int) -> str:
        """The current state of one slot, from cap.latest; see state_for."""
        cap = self.cap
        status = cap.latest.get(slot) if cap else None
        finished = bool(cap and slot in cap.finished)
        return self.state_for(slot, status, finished)

    def slot_temp_view(self, slot: int, unit: str = "C",
                       now: float | None = None,
                       state: str | None = None) -> tuple[str, str, bool]:
        """
        This slot's temperature for its card - (value, detail, grayed) from
        temp_view, fed only this slot's facts. Reads the capture's state with
        defaults, so it copes with no capture yet (and with test doubles).
        """
        cap = self.cap
        if state is None:
            state = self.slot_state(slot)
        if cap is None:
            return temp_view(state, None, None, None, unit)
        now = time.monotonic() if now is None else now
        at = getattr(cap, "temp_at", {}).get(slot)
        note_fn = getattr(cap, "temperature_note", None)
        return temp_view(
            state,
            getattr(cap, "temp_by_slot", {}).get(slot),
            None if at is None else now - at,
            getattr(cap, "peak_temp_by_slot", {}).get(slot),
            unit,
            note=note_fn() if callable(note_fn) else None,
            cutoff_c=getattr(cap, "temp_cutoff_by_slot", {}).get(slot),
            trip_c=getattr(cap, "cutoff_trips", {}).get(slot),
            stopped_by_cutoff=slot in getattr(cap, "cutoff_stopped", ()),
            blind=slot in getattr(cap, "cutoff_blind", ()),
            charger_alert=getattr(cap, "alert_by_slot", {}).get(slot),
            stopped_by_user=slot in getattr(cap, "user_stopped", ()),
            unwatched_end=bool(getattr(cap, "unwatched_end",
                                       lambda _s: False)(slot)),
        )

    def slot_request(self, slot: int) -> RunRequest | None:
        """The pending or starting config for a slot, if any."""
        if slot in self._armed:
            return self._armed[slot]
        if slot in self._starting:
            return self._starting[slot][1]
        return None

    def last_config(self, slot: int) -> RunRequest | None:
        """The last config saved for a slot (held until the next Save), for the
        card's persistent chemistry+action line. Survives start/stop/finish."""
        return self._last_config.get(slot)

    def arm(self, slot: int, request: RunRequest) -> str | None:
        """
        Arm a slot with a pending config. Returns a problem string, or None on
        success. Validated up front so an impossible request never becomes an
        armed state. Arming a slot breaks whatever group it was in (editing one
        member is not editing the group); form a new group with group().
        """
        if slot not in self.slots:
            return f"slot {slot} is not being monitored"
        problem = request.problem()
        if problem:
            return problem
        self._leave_group(slot)
        self._armed[slot] = request
        # Remember it for the card's persistent "set up for" line, held until
        # the next Save regardless of start/stop/finish.
        self._last_config[slot] = request
        return None

    def group(self, slots: list[int], keep_id: int | None = None) -> None:
        """
        Tie the given slots into ONE group - a convenience for starting/stopping
        them together. Leaves any prior groups they were in. Needs at least two
        members; a lone slot is no group.

        A NEW group takes the lowest free id, so a group's LETTER reflects the
        groups that currently exist: break Group A and the next new group is A
        again (A is free), not the next in sequence. `keep_id` instead reuses an
        existing group's id, so reconfiguring the SAME set keeps its letter and
        editing a group never renames it out from under you - captured before
        re-arming dissolved it (see on_configure_slot).
        """
        members = {s for s in slots if s in self.slots}
        if len(members) < 2:
            return
        for s in members:
            self._leave_group(s)
        gid = keep_id if keep_id is not None else self._lowest_free_gid()
        self._groups[gid] = members

    def _lowest_free_gid(self) -> int:
        gid = 1
        while gid in self._groups:
            gid += 1
        return gid

    def group_id_for_exact(self, slots) -> int | None:
        """The id of the existing group whose members are exactly this set, or
        None. Used to keep a group's letter sticky when it is reconfigured."""
        members = {s for s in slots if s in self.slots}
        for gid, mem in self._groups.items():
            if mem == members:
                return gid
        return None

    def leave_group(self, slot: int) -> None:
        """Remove one slot from its group in a single step, WITHOUT touching its
        config. The rest of the group stays (dissolving only if it drops below
        two). This is the per-card 'leave group', distinct from reconfiguring."""
        self._leave_group(slot)

    def group_ids(self) -> list[int]:
        """Current group ids, in a stable order (so the cards keep their spot)."""
        return sorted(self._groups)

    def group_members(self, gid: int) -> list[int]:
        return sorted(self._groups.get(gid, ()))

    def start_group(self, gid: int) -> str:
        """Start every ARMED member of the group (others are left as they are -
        a running member keeps running, a finished one needs reconfiguring)."""
        armed = [s for s in self.group_members(gid) if s in self._armed]
        # An armed member can still be unable to start - a run begun at the
        # panel keeps its armed config here.
        # Only the slots that can start are started, named in the message and
        # recorded in the group; the others are left alone and said so.
        refused = {s: why for s in armed
                   if (why := self._start_refusal(s)) is not None}
        started = [s for s in armed if s not in refused]
        skipped = " | ".join(refused.values())
        if not started:
            if not armed:
                return f"Group {group_letter(gid)}: no armed slots to start"
            return f"Group {group_letter(gid)}: nothing started. {skipped}"
        # One group for the run record: the members' folders share its
        # timestamp, and each meta.json names the others.
        now = datetime.now()
        group = (RunGroup(id=f"{now:%Y-%m-%d_%H%M%S}-{group_letter(gid)}",
                          slots=tuple(started), started=now)
                 if len(started) > 1 else None)
        for s in started:
            self.start_slot(s, group=group)
        note = f". Not started: {skipped}" if refused else ""
        return f"Group {group_letter(gid)}: starting slot(s) {started}{note}"

    def stop_group(self, gid: int) -> str:
        """Stop every RUNNING member of the group; leave the rest untouched."""
        stopped, refused = [], []
        for s in self.group_members(gid):
            st = self.cap.latest.get(s) if self.cap else None
            if st and st.current_ma > 0:
                reply = self.stop_slot(s)
                if reply.startswith("cannot stop"):
                    refused.append(reply)
                else:
                    stopped.append(s)
        if refused and not stopped:
            # Nothing was queued: say so, in the refusal's own words.
            return f"Group {group_letter(gid)}: {refused[0]}"
        if not stopped:
            return f"Group {group_letter(gid)}: no running slots to stop"
        note = f". Not stopped: {refused[0]}" if refused else ""
        return f"Group {group_letter(gid)}: stopping slot(s) {stopped}{note}"

    def ungroup(self, gid: int) -> None:
        """Dissolve the whole group at once (members keep their configs)."""
        self._groups.pop(gid, None)

    def disarm(self, slot: int) -> None:
        self._armed.pop(slot, None)
        self._starting.pop(slot, None)
        self._leave_group(slot)

    def _start_refusal(self, slot: int) -> str | None:
        """Why this slot cannot be started right now, in the words the window
        shows, or None when it can. One place, so a single Start and a group's
        Start refuse for the same reasons."""
        if self.link_down:
            return f"cannot start slot {slot}: {NOT_CONNECTED}"
        if self.slot_latched(slot):
            # refuse_reason would say "already running" off the latched current,
            # which is true of the reading and false of the slot.
            return (f"cannot start slot {slot}: its last run ended but the "
                    f"charger is still echoing that run's reading. Press Stop "
                    f"to clear it, then start.")
        refusal = (refuse_reason([slot], self.cap.latest, for_gui=True)
                   if self.cap else None)
        if refusal:
            return f"cannot start slot {slot}: {refusal}"
        return None

    def start_slot(self, slot: int, group: RunGroup | None = None) -> str:
        """
        Start an armed slot by injecting its config into the live session.

        Verified on hardware (probe_independent.py): a 0x94 config
        starts an idle slot WITHOUT init, so any slot already running survives.
        The config is handed to the polling loop, which writes it and re-checks
        the same guards at the instant of the write.

        The config is COMMITTED here - moved out of _armed into _starting - the
        moment Start is pressed, so Start can never fire twice and the slot's
        state is deterministic regardless of how quickly it is stopped. The card
        shows "starting" until current confirms the run; only a fresh Configure
        re-arms it. (Per-slot stop exists too; see stop_slot.)
        """
        request = self._armed.get(slot)
        if request is None:
            return f"slot {slot} is not armed"
        refusal = self._start_refusal(slot)
        if refusal:
            return refusal
        # Commit the config now: out of _armed, into _starting. Hand it to the
        # live session, which injects it with no init. slot_state clears the
        # starting mark when current confirms, or hands it back on timeout.
        self._armed.pop(slot, None)
        self._starting[slot] = (time.monotonic(), request)
        self._inject_queue.put(StartInjection.build(
            slot, request, cell_id=(self.cells or {}).get(slot), group=group))
        return (f"starting slot {slot}: {request.mode.name.lower()} "
                f"{request.current_ma} mA — config injected, no init "
                f"(other slots undisturbed)")

    def stop_slot(self, slot: int) -> str:
        """
        Stop one slot without touching the others, over the live session.

        Uses the per-slot stop (probe_slot_stop.py): 0xFE to
        the slot's bitmask halts it alone, no init. An explicit human action (a
        slot's Stop button), so it is unguarded - but surgical, so it cannot
        reach beyond the named slot the way All Stop's init does. The slot then
        reports 0 mA and reads as finished.
        """
        if self.link_down:
            return f"cannot stop slot {slot}: {NOT_CONNECTED}"
        st = self.cap.latest.get(slot) if self.cap else None
        if not st or st.current_ma <= 0:
            return f"slot {slot} is not running; nothing to stop"
        self._stop_queue.put(slot)
        return (f"stopping slot {slot} (0xFE to its bitmask, no init; "
                f"other slots keep running)")

    def group_of(self, slot: int) -> set | None:
        """The set of slots grouped with this one, or None if ungrouped."""
        for members in self._groups.values():
            if slot in members:
                return members
        return None

    def group_id_of(self, slot: int) -> int | None:
        """The id of the slot's group, or None. Stable while the group lives;
        the card maps it to a shared color + letter so grouped cards match."""
        for gid, members in self._groups.items():
            if slot in members:
                return gid
        return None

    def _leave_group(self, slot: int) -> None:
        for gid, members in list(self._groups.items()):
            if slot in members:
                members.discard(slot)
                if len(members) < 2:      # a group of one is no group
                    del self._groups[gid]

    def start(self, carried: dict | None = None) -> bool:
        """Begin a session. `carried` (a Retry) names runs the previous session
        started and left going, so this one goes on owning them."""
        if self._thread is not None:
            return True
        self.session_ended = False

        # Validate a start request BEFORE any thread, Capture or run directory
        # exists. session() re-checks and would refuse too, but a request that
        # could never run should fail here, with nothing to clean up.
        req = self._run_request()
        if req is not None:
            problem = req.problem()
            if problem:
                self.bridge.status.emit(f"REFUSING TO START: {problem}")
                self.bridge.stopped.emit("refused")
                return False

        self.cap = Capture(
            name=self.name,
            slots=self.slots,
            chemistry=self.chemistry,
            outdir=self.outdir,
            fallback_dir=self.fallback_dir,
            # A run folder opens when a slot is started here or seen running;
            # an idle slot with nothing on it has no file.
            record_idle=False,
            external_runs=True,
            cells=self.cells,
            # Temperature is the headline; the GUI polls it on every connect.
            # On unverified firmware the capture switches it off itself (and
            # says why, via temperature_note).
            temp_interval_s=TEMP_INTERVAL_S,
            # An attach never sends init, so the drift report must not expect
            # the control-traffic responses; a started run sends it and should.
            sends_control=self.starts_runs,
            # Provenance: what the run was asked to do, mirrored from the CLI's
            # from_args. Without this a GUI-started discharge would be recorded
            # in meta.json as an attach.
            run_config=RunConfig(
                mode=self.options.start,
                current_ma=self.options.current,
                capacity_mah=self.options.capacity,
                safety_timer_min=self.options.safety_timer,
                cutoff_mv=self.options.cutoff_mv,
                target_mv=self.options.target_mv,
                temp_cutoff_c=self.options.temp_cutoff,
            ) if self.starts_runs else None,
            log=StatusLog(self.bridge.status.emit),
        )
        self._open_session_log()
        if carried:
            self.cap.inherit_runs(carried)
        # THE seam. One line joins the core to the GUI.
        self.cap.subscribe(self.bridge.sample.emit)
        for fn in self._pending_subscribers:
            self.cap.subscribe(fn)
        self._thread = threading.Thread(target=self._run, daemon=True,
                                        name="mc5000-capture")
        self._thread.start()
        return True

    def restart(self) -> bool:
        """Begin a NEW capture session after the last one ended - no charger
        found, a link that did not recover, an error, or a Stop. Does nothing
        while a session is live. Armed configs stay; anything that was waiting
        for the old link does not (nothing may fire late), and a fresh Capture
        opens its own session log."""
        if self.is_running:
            return False
        # Runs this app started that are still going: the new session must go
        # on owning them (temperature cutoff, the close-stop) - see
        # Capture.carry_forward. Read before the old capture is replaced.
        carried = self.cap.carry_forward() if self.cap is not None else {}
        self._thread = None
        self._stop.clear()
        self._all_stop.clear()
        for q in (self._inject_queue, self._stop_queue):
            while True:
                try:
                    q.get_nowait()
                except queue.Empty:
                    break
        self._starting.clear()
        return self.start(carried=carried)

    def _run(self):
        reason = "error"
        try:
            # cap.log, not the bridge directly: it shows the line AND puts it
            # in the session log.
            reason = self.source.run(self.cap, self._stop,
                                     self.cap.log, self._all_stop,
                                     self._inject_queue, self._stop_queue)
        except Exception as e:                      # noqa: BLE001
            path = crashlog.record(type(e), e, e.__traceback__,
                                   "capture thread")
            self.cap.log(f"capture failed: {e!r}"
                         + (f" (details: {path})" if path else ""))
        finally:
            # Same epilogue as the CLI: the run's metadata is part of the
            # product, whichever front end drove it.
            try:
                self.cap.save_meta({"end_reason": reason})
            except Exception:
                pass
            try:
                self.cap.close()
            except Exception:
                pass
            try:
                self.cap.log(f"finished: {reason}")
                self.cap.log.close()
            except Exception:
                pass
            self.session_ended = True
            self.bridge.stopped.emit(reason)

    def _open_session_log(self) -> None:
        """
        Give this session the same running log the CLI keeps:
        runs\\logs\\<date>_<time>_<name>.log, "gui" when there is no --name,
        in the logs folder as it is when the session starts (a later change in
        Settings leaves it where it is, like a run already recording). It is
        what a stranger's problem report has to attach.

        Softer than a run's CSV on purpose: the log is for troubleshooting,
        so a logs folder that cannot be written costs the log, says so, and
        the capture carries on.
        """
        cap = self.cap
        path = session_log_path(self.outdir, cap.started_at,
                                self.name or "gui")
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            log = StatusLog(self.bridge.status.emit, path)
        except OSError as e:
            cap.log(f"no session log: cannot write {path} ({e}); the capture "
                    f"carries on without one")
            return
        cap.log = log
        cap.session_log = path
        crashlog.set_sink(log.note)
        log.note("=" * 68)
        log.note(f"SITREP91 {build_label()}: GUI session"
                 + (f" {self.name}" if self.name else ""))
        log.note(f"  slots {self.slots}, "
                 + ("START " + self.options.start if self.starts_runs
                    else "watching (start nothing until asked)"))
        log.note("  cells " + ", ".join(
            f"{s}={(self.cells or {}).get(s) or '(unlabeled)'}"
            for s in self.slots))
        log.note(f"  writing to {self.outdir} (one folder per slot per run)")
        log.note(f"  session log {path}")
        log.note("=" * 68)

    def note(self, msg: str) -> None:
        """Put a line the WINDOW produced (a Configure, a Start or Stop
        reply, a refusal) into the session log. The window shows it itself."""
        log = getattr(self.cap, "log", None)
        if isinstance(log, StatusLog) and log.fh and not log.fh.closed:
            log.note(msg)

    def all_stop(self):
        """
        Halt every slot on the charger, whatever started it - the one
        deliberately unguarded action, because it is an explicit human
        decision, never automatic.

        With a capture live, the request is injected into the running session
        (a second BLE connection cannot exist); the capture then watches the
        slots go idle and ends "done", so the CSV records the halt. With no
        capture, a one-shot worker runs the same standalone path as
        `capture.py --stop-all`.
        """
        if self.simulated:
            # The demo has no charger. While the replay runs it handles the
            # event itself; once it has ended there is nothing to halt, and
            # the standalone path below would scan for and connect to a REAL
            # charger and stop every slot on it.
            if self.is_running:
                self._all_stop.set()
                self.cap.log("ALL STOP requested - halting every slot ...")
            else:
                self.bridge.status.emit(
                    "demo: nothing to stop - the replay has ended and no "
                    "charger is involved")
            return
        if self.is_running:
            if self.link_down:
                # A capture is retrying: the request would wait in the event
                # and fire whenever the link next came back. Refused instead.
                self.note(f"ALL STOP refused: {NOT_CONNECTED}")
                self.bridge.status.emit(f"ALL STOP: {NOT_CONNECTED}")
                return
            self._all_stop.set()
            self.cap.log("ALL STOP requested - halting every slot ...")
            return
        self.bridge.status.emit("ALL STOP: connecting to the charger ...")
        threading.Thread(target=self._standalone_all_stop, daemon=True,
                         name="mc5000-all-stop").start()

    @property
    def link_down(self) -> bool:
        """No command can be sent right now: no live session, or one that is
        reconnecting. Start, Stop and ALL STOP are refused then - queued, they
        would fire whenever the link returned. The demo has no link to lose."""
        if self.simulated:
            return not self.is_running
        return not (self.is_running and getattr(self.cap, "link_ok", True))

    @property
    def simulated(self) -> bool:
        """True when the source is the demo: it never touches Bluetooth."""
        return bool(getattr(self.source, "simulated", False))

    def _standalone_all_stop(self):
        if self.simulated:                      # belt and braces; see all_stop
            return
        try:
            code = asyncio.run(stop_all_mode(
                fresh=True, log=self.bridge.status.emit))
            if code == 0:
                self.bridge.status.emit("ALL STOP complete; slots idle")
        except Exception as e:                      # noqa: BLE001
            crashlog.record(type(e), e, e.__traceback__, "ALL STOP")
            self.bridge.status.emit(
                f"ALL STOP failed: {type(e).__name__}: {e} - "
                f"use the charger's panel")

    def app_started_running(self) -> list[int]:
        """Slots THIS app started that closing it would stop on the charger:
        started here (not a panel run it only watches) and not reported idle.
        The same test the session's exit stop applies, so the dialog's wording
        and what actually happens cannot disagree."""
        cap = self.cap
        if cap is None or self.simulated:
            return []
        started = getattr(cap, "started_slots", None) or ()
        latest_any = getattr(cap, "latest_any", {})
        return sorted(s for s in started
                      if not _reports_idle(latest_any.get(s)))

    @property
    def is_running(self) -> bool:
        """Whether a capture is actively recording right now."""
        return self._thread is not None and self._thread.is_alive()

    def stop(self, timeout: float | None = None, pump=None):
        """End the session and wait for it. `pump` (the window passes Qt's
        processEvents) is called while waiting, so the window repaints and
        stays responsive through a teardown that can take many seconds."""
        timeout = STOP_TIMEOUT_S if timeout is None else timeout
        self._stop.set()
        # A local reference: `pump` runs the window's event loop, which can
        # re-enter stop() (the X clicked mid-wait) and clear self._thread under
        # this loop.
        thread = self._thread
        if thread is not None:
            deadline = time.monotonic() + timeout
            while thread.is_alive() and time.monotonic() < deadline:
                thread.join(timeout=0.05)
                if pump is not None:
                    pump()
            if thread.is_alive():
                self.bridge.status.emit(
                    "capture thread did not stop cleanly; it will end with "
                    "the process")
            if self._thread is thread:
                self._thread = None

    def set_outdir(self, folder) -> None:
        """Change the logs folder. Applies from the next run: a run already
        recording stays in its folder."""
        self.outdir = Path(folder)
        if self.cap is not None:
            self.cap.outdir = self.outdir

    @property
    def runs_dir(self) -> Path:
        return self.outdir

    def file_warning(self, slot: int) -> str | None:
        """The write-failure warning for a slot's current run, if any."""
        fn = getattr(self.cap, "warning_for", None)
        return fn(slot) if fn else None


# --------------------------------------------------------------------------
# widgets
# --------------------------------------------------------------------------

class SettingsDialog(QDialog):
    """App settings: theme, the console/log panel, and the temperature unit."""

    def __init__(self, parent, current_choice: str, show_console: bool = False,
                 temp_unit: str = "C", runs_dir=None):
        super().__init__(parent)
        self.setWindowTitle("Settings")
        self.setMinimumWidth(420)

        self.theme_box = QComboBox()
        for choice in gui_theme.CHOICES:
            self.theme_box.addItem(gui_theme.CHOICE_LABELS[choice], choice)
        self.theme_box.setCurrentIndex(
            self.theme_box.findData(current_choice))

        self.console_check = QCheckBox("Show console (log panel)")
        self.console_check.setChecked(show_console)

        # Display only: every file (samples.csv, meta.json) stays Celsius.
        self.unit_box = QComboBox()
        for unit in TEMP_UNITS:
            self.unit_box.addItem(f"°{unit}", unit)
        self.unit_box.setCurrentIndex(max(0, self.unit_box.findData(temp_unit)))
        self.unit_box.setToolTip("How temperatures are shown. Files are always "
                                 "recorded in °C.")

        # Where runs are recorded (settings.ini [logging] runs_dir). Checked
        # when a run opens there; a change applies from the NEXT run.
        self.runs_edit = QLineEdit(str(runs_dir or runfiles.configured_runs_dir()))
        self.runs_edit.setToolTip("Where each run's folder is created. A change "
                                  "applies from the next run.")
        self.runs_edit.textChanged.connect(self._check_runs_dir)
        browse = QPushButton("Browse…")
        browse.clicked.connect(self._browse_runs_dir)
        runs_row = QHBoxLayout()
        runs_row.addWidget(self.runs_edit, 1)
        runs_row.addWidget(browse)
        self.runs_note = QLabel("")
        self.runs_note.setWordWrap(True)
        self.runs_note.setStyleSheet("font-size: 11px;")

        form = QFormLayout()
        form.addRow("Theme", self.theme_box)
        form.addRow("Console", self.console_check)
        form.addRow("Temperature", self.unit_box)
        form.addRow("Logs folder", runs_row)
        form.addRow("", self.runs_note)
        self._check_runs_dir()

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)

        layout = QVBoxLayout()
        layout.addLayout(form)
        layout.addWidget(buttons)
        self.setLayout(layout)

    def chosen(self) -> str:
        return self.theme_box.currentData()

    def runs_dir(self) -> Path:
        text = self.runs_edit.text().strip()
        if not text:
            return runfiles.default_runs_dir()
        p = Path(text).expanduser()
        return p if p.is_absolute() else runfiles.HOME_DIR / p

    def _browse_runs_dir(self):
        folder = QFileDialog.getExistingDirectory(
            self, "Logs folder", str(self.runs_dir()))
        if folder:
            self.runs_edit.setText(folder)

    def _check_runs_dir(self):
        """Say plainly if the chosen folder will not work - but do not refuse
        it: a flash drive that is not plugged in yet is a legitimate choice,
        and a run that cannot write there falls back to the local default."""
        folder = self.runs_dir()
        problem = runfiles.path_budget_problem(folder)
        if not problem and folder.exists() and not os.access(folder, os.W_OK):
            problem = (f"{folder} cannot be written right now. Runs will be "
                       f"recorded in {runfiles.default_runs_dir()} until it "
                       f"can.")
        note = "Applies from the next run."
        if not folder.exists():
            note = "Created when the next run starts; applies from then."
        self.runs_note.setText(problem or note)

    def show_console(self) -> bool:
        return self.console_check.isChecked()

    def temp_unit(self) -> str:
        return self.unit_box.currentData()


# The metrics the shared chart can show. Each: key, axis label, unit, and how
# to pull the value from a SlotStatus.
METRICS = [
    ("voltage", "voltage", "V", lambda st: st.voltage_v),
    ("current", "current", "mA", lambda st: float(st.current_ma)),
    ("capacity", "capacity", "mAh", lambda st: float(st.capacity_mah)),
]
METRIC_BY_KEY = {m[0]: m for m in METRICS}
# The fourth metric. Not in METRICS because it does not come from the 0x91
# status frame: it arrives sparsely (Sample.slot_temp_c) and is drawn as a
# staircase with real gaps broken - see temp_staircase.
TEMP_METRIC = "temperature"


class TimeAxisItem(pg.AxisItem):
    """
    Elapsed-time x-axis: m:ss while the axis stays under an hour, and "1h05"
    style for EVERY tick once any tick reaches an hour.

    pyqtgraph's default SI prefixing turns a seconds axis into "ks" on a long
    run - correct, but nobody plans an evening in kiloseconds (seen at hour one
    of the first six-hour capture). The labels used to switch from m:ss to h:mm
    at the hour with nothing to tell them apart, so one axis read "33:20, 50:00,
    1:06" - the last being 1 h 06 min, not 1 min 06 s.
    """

    def tickStrings(self, values, scale, spacing):
        whole = [max(0, int(round(v))) for v in values]
        hours = any(v >= 3600 for v in whole)
        out = []
        for v in whole:
            h, rem = divmod(v, 3600)
            m, s = divmod(rem, 60)
            out.append(f"{h}h{m:02d}" if hours else f"{m}:{s:02d}")
        return out


class SharedChart(QWidget):
    """
    One plot, all slots overlaid, with a metric toggle (voltage / current /
    capacity). The comparison view: is cell 3 holding up like cell 1? Each
    slot keeps its full history of all three metrics, so switching metric
    re-renders from stored data rather than needing a re-run. Finished slots
    stay on the chart for comparison.
    """

    def __init__(self, slots, mode, unit: str = "C"):
        super().__init__()
        self.slots = slots
        self.mode = mode
        self.unit = unit
        self.metric = "voltage"
        # per slot: xs plus a list per metric key
        self.xs = {s: [] for s in slots}
        self.data = {s: {k: [] for k, *_ in METRICS} for s in slots}
        # per slot: (elapsed_s, temp_c) readings, in C whatever the display
        self.temps = {s: [] for s in slots}
        self.temp_interval_s = TEMP_INTERVAL_S

        self.plot = pg.PlotWidget(
            axisItems={"bottom": TimeAxisItem(orientation="bottom")})
        self.plot.setLabel("bottom", "elapsed", units="")
        self.plot.getAxis("bottom").enableAutoSIPrefix(False)
        # Without this a 2000 mA axis reads "2 kmA" (kilo-milliamps) and a
        # 900 mAh one "0.9 kmAh"; the readings are already in the unit named.
        self.plot.getAxis("left").enableAutoSIPrefix(False)
        self.curves = {
            s: self.plot.plot([], [], pen=pg.mkPen("#888", width=2))
            for s in slots
        }

        # metric toggle
        self.buttons = {}
        toggle = QHBoxLayout()
        toggle.addWidget(QLabel("show:"))
        for key, label, _unit, _fn in METRICS + [(TEMP_METRIC, "slot temp",
                                                   None, None)]:
            b = QPushButton(label)
            b.setCheckable(True)
            b.setChecked(key == self.metric)
            b.clicked.connect(lambda _c, k=key: self.set_metric(k))
            self.buttons[key] = b
            toggle.addWidget(b)
        self.buttons[TEMP_METRIC].setToolTip(TEMP_TOOLTIP)
        toggle.addStretch(1)

        root = QVBoxLayout()
        root.setContentsMargins(0, 0, 0, 0)
        root.addLayout(toggle)
        root.addWidget(self.plot, stretch=1)
        self.setLayout(root)

        self.apply_theme(mode)
        self.set_metric(self.metric)

    def set_metric(self, key):
        self.metric = key
        for k, b in self.buttons.items():
            b.setChecked(k == key)
        if key == TEMP_METRIC:
            self.plot.setLabel("left", "slot temp", units=f"°{self.unit}")
            for s in self.slots:
                self._draw_temp(s)
            return
        _k, label, unit, _fn = METRIC_BY_KEY[key]
        self.plot.setLabel("left", label, units=unit)
        for s in self.slots:
            self.curves[s].setData(self.xs[s], self.data[s][key])

    def _draw_temp(self, s):
        xs, ys = temp_staircase(self.temps[s], self.temp_interval_s, self.unit)
        self.curves[s].setData(xs, ys, connect="finite")

    def add_temp(self, slot, elapsed_s, temp_c):
        """One temperature reading - the sparse Sample.slot_temp_c."""
        if slot not in self.temps:
            return
        self.temps[slot].append((elapsed_s, temp_c))
        if self.metric == TEMP_METRIC:
            self._draw_temp(slot)

    def set_unit(self, unit):
        self.unit = unit
        if self.metric == TEMP_METRIC:
            self.set_metric(TEMP_METRIC)

    def clear(self):
        """Forget every curve: a new session counts its time from zero, so the
        old session's lines would be drawn straight through it."""
        self.xs = {s: [] for s in self.slots}
        self.data = {s: {k: [] for k, *_ in METRICS} for s in self.slots}
        self.temps = {s: [] for s in self.slots}
        self.set_metric(self.metric)

    def add(self, sample):
        s = sample.slot
        if s not in self.curves:
            return
        st = sample.status
        # Empty slots emit 0 V frames; plotting them draws a flat line at the
        # floor. Only chart a slot that actually holds a cell.
        if st.state == "empty":
            return
        self.xs[s].append(sample.elapsed_s)
        # A torn-down slot reports 0 mAh; plotting it would drop the capacity
        # curve to the floor the instant the run ends. Hold the last real
        # value instead - the curve then ends where the run did.
        prev_cap = self.data[s]["capacity"][-1] if self.data[s]["capacity"] else 0.0
        hold_cap = counters_zeroed(st) and prev_cap > 0
        for key, _label, _unit, fn in METRICS:
            self.data[s][key].append(prev_cap if hold_cap and key == "capacity"
                                     else fn(st))
        if sample.slot_temp_c is not None:
            self.add_temp(s, sample.elapsed_s, sample.slot_temp_c)
        if self.metric != TEMP_METRIC:
            self.curves[s].setData(self.xs[s], self.data[s][self.metric])

    def apply_theme(self, mode):
        self.mode = mode
        gui_theme.style_plot(self.plot, mode)
        for s in self.slots:
            self.curves[s].setPen(
                pg.mkPen(gui_theme.slot_color(s, mode), width=2))


# Which actions each chemistry family offers. Lithium-class cells can be put
# into Storage; nickel-class have Refresh and Break-in instead. Combinations not
# in capture.VERIFIED_CONTROL appear in the dialog but disabled - visible so you
# know they exist, unselectable until they have been run on the author's charger.
_LITHIUM = {Chemistry.LIION, Chemistry.LIION_HV, Chemistry.LIFEPO4,
            Chemistry.LTO, Chemistry.NAION}

# Added to Configure's monitor-only note. Only the GUI says it: the same refusal
# is printed by the CLI and written to logs, where "the SITREP91 folder" means
# nothing. ROADMAP.md sits in the zip root beside README.md.
ROADMAP_NOTE = ("See ROADMAP.md in the SITREP91 folder for what's verified "
                "and what's next.")


# NiZn and RAM are neither: the panel offers them Charge, Storage and Cycle only -
# no Discharge, Refresh or Break-In (read off the charger's panel).
_NO_DISCHARGE = {Chemistry.NIZN, Chemistry.RAM}


def actions_for_chemistry(chemistry):
    """The actions the charger's own panel offers for `chemistry`. Whether
    each may be STARTED is VERIFIED_CONTROL's question."""
    if chemistry in _NO_DISCHARGE:
        return [Mode.CHARGE, Mode.STORAGE, Mode.CYCLE]
    if chemistry in _LITHIUM:
        return [Mode.CHARGE, Mode.DISCHARGE, Mode.STORAGE, Mode.CYCLE]
    return [Mode.CHARGE, Mode.DISCHARGE, Mode.REFRESH, Mode.BREAK_IN, Mode.CYCLE]


DEFAULTS_NOTE = ("Enter your cell's rated capacity first: the current and the "
                 "safety timer are worked out from it (charge at 0.5C, "
                 "discharge at 0.2C, never above the charger's own settings). "
                 "Check your cell's datasheet.")
CAPACITY_FIRST_TIP = "Enter the cell's rated capacity first."
TIMER_CAPPED_NOTE = ("This run may need longer than the charger's 24 h timer "
                     "limit, so the timer is held at 24 h.")
# Configure's Advanced section. Each row edits the RunRequest field of the same
# name; the ORDER is fixed and was chosen so that every chemistry-and-action
# list in capture.advanced_fields is a subsequence of it, which makes it the
# panel's own order wherever the panel shows the setting.
ADVANCED_LABELS = {
    "storage_mv": "Storage voltage (mV)",
    "discharge_current_ma": "Discharge current (mA)",
    "delta_v_mv": "-ΔV (mV)",
    "target_mv": "Target voltage (mV)",
    "charge_cutoff_current_ma": "Charge cut-off current (mA)",
    "trickle_ma": "Trickle (mA, 0 = off)",
    "keep_mv": "Keep voltage (mV, 0 = off)",
    "discharge_cutoff_current_ma": "Discharge cut-off current (mA)",
}
ADVANCED_NOTE = ("The charger's own settings for this chemistry and action, "
                 "filled with its own starting values. Leave them alone unless "
                 "your cell needs something else.")


def typed_fields(existing: RunRequest) -> tuple[set, bool]:
    """Which of a saved config's fields the person set by hand: (the names among
    "current", "cutoff" and "discharge_current_ma", and whether the safety timer
    is theirs). A value equal to what the derivation gives for the saved
    capacity was derived, not typed, so reopening Configure and changing the
    capacity re-derives it; a value that differs was typed and is kept.
    Needs nothing stored beside the config."""
    chem, mode, cap = existing.chemistry, existing.mode, existing.capacity_mah
    touched = set()
    if existing.current_ma != derive_current_ma(chem, mode, cap):
        touched.add("current")
    if (existing.cutoff_mv is not None
            and existing.cutoff_mv != CHEMISTRY_DEFAULTS[chem]["cutoff_mv"]):
        touched.add("cutoff")
    current = existing.current_ma
    if mode is Mode.STORAGE:
        second = (existing.discharge_current_ma
                  if existing.discharge_current_ma is not None
                  else existing.current_ma)
        if second != derive_storage_discharge_ma(chem, cap):
            touched.add("discharge_current_ma")
        current = min(current, second)
    got = suggest_safety_timer_min(chem, mode, cap, current)
    timer_typed = got is None or existing.safety_timer_min != got[0]
    return touched, timer_typed


class ConfigDialog(QDialog):
    """
    Configure one slot (and optionally copy the settings to others). Produces a
    validated RunRequest - the same object the CLI builds - never a packet. The
    dialog does no charger I/O; the controller arms from its result.

    Save-only: the dialog just arms the slot and returns. Starting is always a
    separate, deliberate press on the slot card, so a run can never begin from
    the same click that sets it up - you always see the armed config, chemistry
    and all, before Start is even available.

    Starting values are the charger's own (capture.CONFIGURE_DEFAULTS), with the
    safety timer worked out from capacity and current rather than left off.
    Choosing a chemistry or an action re-derives every field you have not typed
    over (the current, the timer, a cutoff that no longer fits); what you typed
    is kept, a typed timer included. Reopening a slot that already has a config
    shows THAT config, and which of its fields were typed is worked out from the
    config itself (typed_fields). The timer follows capacity and current until
    you edit it by hand.
    """

    def __init__(self, parent, slot, monitored_slots, cell_id=None,
                 chemistry=Chemistry.LIION, existing: RunRequest | None = None,
                 temp_unit: str = "C", known_cells=()):
        super().__init__(parent)
        self.slot = slot
        self.temp_unit = temp_unit
        self.setWindowTitle(f"Configure slot {slot}")
        self.setMinimumWidth(340)

        # Reopening Configure shows the config the slot already has, not the
        # house defaults. The card advertises that config on its "set up for"
        # line, so a dialog that silently reverted to 240-minute defaults made
        # Save look like "confirm this" while actually replacing it - the
        # safety timer is the field where that matters most.
        self.existing = existing
        # No resets while the first values go in; the guards also keep the
        # action list's own churn (it clears and refills) from resetting fields.
        self._loading = True
        self._repopulating = False
        # A saved config's timer is the user's own and stays put; a fresh
        # dialog's follows capacity and current until they edit it.
        # Which of a saved config's fields the user set by hand is worked out
        # from the config itself (typed_fields): a value that is what the
        # derivation would give is derived and follows a new capacity; only a
        # value that differs was typed, and stays. A fresh dialog has none.
        if existing is not None:
            self._touched, self._timer_touched = typed_fields(existing)
        else:
            self._touched, self._timer_touched = set(), False
        if existing is not None:
            chemistry = existing.chemistry
            start = dict(
                current=existing.current_ma, capacity=existing.capacity_mah,
                timer=existing.safety_timer_min,
                cutoff=(existing.cutoff_mv if existing.cutoff_mv is not None
                        else CHEMISTRY_DEFAULTS[chemistry]["cutoff_mv"]))
        else:
            # A fresh dialog opens on Discharge (the first enabled action), so
            # that is the mode its starting values are for.
            d = (configure_defaults(chemistry, Mode.DISCHARGE)
                 or configure_defaults(Chemistry.LIION, Mode.DISCHARGE))
            # Capacity starts BLANK, and with it the current and the timer
            # that are worked out from it (see capture.derive_current_ma).
            start = dict(current="", capacity="", cutoff=d["cutoff_mv"],
                         timer="")

        # An editable dropdown of the cell names earlier runs were recorded
        # under (`known_cells`, most recent first - runfiles.past_cell_names).
        # Typing a new name works exactly as before; it joins the list after
        # its first run, because the runs are the list's only source.
        self.cell_edit = QComboBox()
        self.cell_edit.setEditable(True)
        self.cell_edit.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        self.cell_edit.addItems([str(n) for n in known_cells])
        self.cell_edit.setCurrentIndex(-1)
        self.cell_edit.setCurrentText(cell_id or "")
        # Enter here (typed out of habit, or sent by a barcode scanner) must not
        # reach the dialog's default Save button: that armed the slot before the
        # chemistry and values had been looked at. See eventFilter.
        # On a real keyboard the key arrives at the combo box itself (it is the
        # focus widget), not at the text field inside it, so both are watched.
        self._cell_enter_at = float("-inf")     # see keyPressEvent
        self.cell_edit.installEventFilter(self)
        self.cell_edit.lineEdit().installEventFilter(self)
        self.chem_combo = QComboBox()
        # Named as the cards name them ("Li-ion", "NiMH") - the list showed
        # the command-line ids ("liion", "nimh").
        for name, chem in sorted(
                ((CHEM_DISPLAY.get(c, n), c)
                 for n, c in CHEMISTRY_BY_NAME.items()),
                key=lambda item: item[0].lower()):
            # A chemistry never run on the author's charger stays visible but
            # cannot be started: it is MONITOR-ONLY (see capture.VERIFIED_CONTROL).
            if chem in VERIFIED_CONTROL:
                self.chem_combo.addItem(name, chem)
            else:
                self.chem_combo.addItem(f"{name} — monitor only", chem)
                item = self.chem_combo.model().item(self.chem_combo.count() - 1)
                item.setEnabled(False)
        self.chem_combo.setCurrentIndex(self.chem_combo.findData(chemistry))
        self.chem_combo.currentIndexChanged.connect(self._repopulate_actions)

        # Shown only while a monitor-only chemistry is selected.
        self.monitor_note = QLabel("")
        self.monitor_note.setWordWrap(True)
        self.monitor_note.setStyleSheet("font-size: 11px;")
        self.monitor_note.setVisible(False)

        self.action_combo = QComboBox()
        self.current_edit = QLineEdit(str(start["current"]))
        self.capacity_edit = QLineEdit(str(start["capacity"]))
        self.cutoff_edit = QLineEdit(str(start["cutoff"]))
        self.timer_edit = QLineEdit(str(start["timer"]))
        # Starting values come from the charger, not from us: say so, and say
        # the one thing the user must do first. The 1C warning and the
        # timer-limit note are hidden until they apply.
        self.defaults_note = QLabel(DEFAULTS_NOTE)
        self.defaults_note.setWordWrap(True)
        self.defaults_note.setStyleSheet("font-size: 11px;")
        self.rate_warning = QLabel("")
        self.rate_warning.setWordWrap(True)
        self.rate_warning.setStyleSheet(
            "color: #c98a1b; font-size: 11px; font-weight: bold;")
        self.rate_warning.setVisible(False)
        self.timer_note = QLabel("")
        self.timer_note.setWordWrap(True)
        self.timer_note.setStyleSheet("font-size: 11px;")
        self.timer_note.setVisible(False)
        # The app-side temperature cutoff: optional, blank = off. Shown in the
        # display unit, stored in C. Honest about what it is - the firmware
        # does not enforce it, so it lives only as long as the app is connected.
        tc = existing.temp_cutoff_c if existing is not None else None
        self.temp_cutoff_edit = QLineEdit(
            "" if tc is None else str(round(to_unit(tc, temp_unit))))
        self.temp_cutoff_edit.setPlaceholderText("off")
        self.temp_cutoff_edit.setToolTip(CUTOFF_TOOLTIP)
        self.temp_cutoff_note = QLabel(
            "Optional. Active only while this app is connected; the charger's "
            "safety timer and its own 70 °C limit remain the backstop.")
        self.temp_cutoff_note.setWordWrap(True)
        self.temp_cutoff_note.setStyleSheet("font-size: 11px;")

        # The Advanced section: collapsed, one row per setting the charger's
        # own pages have (capture.advanced_fields says which apply to the
        # chemistry and action chosen), each holding the panel's own value.
        self.adv_edits = {name: QLineEdit() for name in ADVANCED_LABELS}
        self.advanced_form = QFormLayout()
        for name, label in ADVANCED_LABELS.items():
            self.advanced_form.addRow(label, self.adv_edits[name])
        advanced_note = QLabel(ADVANCED_NOTE)
        advanced_note.setWordWrap(True)
        advanced_note.setStyleSheet("font-size: 11px;")
        self.advanced_box = QWidget()
        box_layout = QVBoxLayout(self.advanced_box)
        box_layout.setContentsMargins(0, 0, 0, 0)
        box_layout.addWidget(advanced_note)
        box_layout.addLayout(self.advanced_form)
        self.advanced_box.setVisible(False)
        self.advanced_button = QPushButton("Advanced ▸")
        self.advanced_button.setCheckable(True)
        self.advanced_button.setFlat(True)
        self.advanced_button.setStyleSheet("text-align: left;")
        self.advanced_button.toggled.connect(self._advanced_toggled)

        form = QFormLayout()
        self._form = form
        form.addRow("Cell id", self.cell_edit)
        form.addRow("Chemistry", self.chem_combo)
        form.addRow("Action", self.action_combo)
        form.addRow("Current (mA)", self.current_edit)
        form.addRow("Capacity (mAh)", self.capacity_edit)
        form.addRow("Cutoff (mV)", self.cutoff_edit)
        form.addRow("Safety timer (min)", self.timer_edit)
        form.addRow(f"Stop at slot temp (°{temp_unit})", self.temp_cutoff_edit)
        form.addRow("", self.temp_cutoff_note)

        # copy-to-slot: the OTHER monitored slots
        self.copy_boxes = {}
        others = [s for s in monitored_slots if s != slot]
        copy_row = QHBoxLayout()
        copy_row.addWidget(QLabel("Also apply to"))
        for s in others:
            box = QCheckBox(f"slot {s}")
            self.copy_boxes[s] = box
            copy_row.addWidget(box)
        copy_row.addStretch(1)

        self.error_label = QLabel("")
        # A refusal names the window and says the charger would silently swap
        # the value - several lines, never one clipped line.
        self.error_label.setWordWrap(True)
        self.error_label.setStyleSheet("color: #d9534f; font-size: 12px;")
        self.error_label.setVisible(False)

        cancel = QPushButton("Cancel")
        cancel.clicked.connect(self.reject)
        save = QPushButton("Save")
        save.setDefault(True)
        save.clicked.connect(self._accept)
        self.save_button = save
        buttons = QHBoxLayout()
        buttons.addWidget(cancel)
        buttons.addStretch(1)
        buttons.addWidget(save)

        layout = QVBoxLayout()
        layout.addLayout(form)
        layout.addWidget(self.timer_note)
        layout.addWidget(self.rate_warning)
        layout.addWidget(self.defaults_note)
        layout.addWidget(self.advanced_button)
        layout.addWidget(self.advanced_box)
        layout.addWidget(self.monitor_note)
        if others:
            layout.addLayout(copy_row)
        layout.addWidget(self.error_label)
        layout.addLayout(buttons)
        self.setLayout(layout)

        self._repopulate_actions()
        # Connected only now, so building the dialog resets nothing.
        self.action_combo.currentIndexChanged.connect(self._action_changed)
        self.timer_edit.textEdited.connect(self._timer_edited)
        self.current_edit.textEdited.connect(self._current_edited)
        self.cutoff_edit.textEdited.connect(self._cutoff_edited)
        self.capacity_edit.textEdited.connect(self._capacity_edited)
        self.current_edit.textChanged.connect(self._update_rate_warning)
        self.capacity_edit.textChanged.connect(self._update_rate_warning)
        # A refusal describes the form as it was; any change to it retires it.
        for edit in (self.current_edit, self.capacity_edit, self.cutoff_edit,
                     self.timer_edit, self.temp_cutoff_edit,
                     *self.adv_edits.values()):
            edit.textEdited.connect(self._clear_error)
        self.chem_combo.currentIndexChanged.connect(self._clear_error)
        self.action_combo.currentIndexChanged.connect(self._clear_error)
        discharge = self.adv_edits["discharge_current_ma"]
        discharge.textEdited.connect(self._discharge_edited)
        discharge.textChanged.connect(self._update_rate_warning)
        self._show_advanced(self.chem_combo.currentData(),
                            self.action_combo.currentData(),
                            existing=self.existing)
        self._loading = False
        self._recompute_timer()          # shows the 24 h note if it applies
        self._update_rate_warning()
        self._update_save_enabled()

    def _repopulate_actions(self):
        chem = self.chem_combo.currentData()
        # First pass has nothing selected yet, so fall back to the config being
        # edited - otherwise reopening a Charge config would land on Discharge.
        keep = self.action_combo.currentData()
        if keep is None and self.existing is not None:
            keep = self.existing.mode
        self._repopulating = True
        self.action_combo.clear()
        monitor_only = chem not in VERIFIED_CONTROL
        for i, mode in enumerate(actions_for_chemistry(chem)):
            label = mode.name.replace("_", "-").title()
            ok = control_verified(chem, mode)
            if not ok:
                label += " — monitor only" if monitor_only else " — not yet verified"
            self.action_combo.addItem(label, mode)
            if not ok:
                item = self.action_combo.model().item(i)
                item.setEnabled(False)
        # keep the prior action if still valid; else default to Discharge (the
        # house-standard capacity test), else the first verified action
        idx = self.action_combo.findData(keep)
        if idx < 0 or not self.action_combo.model().item(idx).isEnabled():
            idx = self.action_combo.findData(Mode.DISCHARGE)
        if idx < 0 or not self.action_combo.model().item(idx).isEnabled():
            idx = next((j for j in range(self.action_combo.count())
                        if self.action_combo.model().item(j).isEnabled()), 0)
        self.action_combo.setCurrentIndex(idx)
        self._repopulating = False
        if not self._loading:
            # The user chose a chemistry: every field goes back to that
            # chemistry's own starting values (not just the cutoff, which used
            # to be carried over when it happened to fit).
            self._fill_defaults(chem, self.action_combo.currentData(),
                                chemistry_changed=True)
        self._update_rate_warning()
        # A monitor-only chemistry cannot be saved as a config at all.
        self.monitor_note.setText(
            f"{monitor_only_message(chem)} {ROADMAP_NOTE}" if monitor_only
            else "")
        self.monitor_note.setVisible(monitor_only)
        self._update_save_enabled()

    def _capacity_value(self) -> int | None:
        """The capacity as typed, or None while it is blank or not a positive
        whole number."""
        try:
            value = int(self.capacity_edit.text().strip())
        except ValueError:
            return None
        return value if value > 0 else None

    def _update_save_enabled(self):
        """Save needs a capacity (the current and the timer come from it) and
        an action the charger can be sent."""
        action_ok = (self.action_combo.count() > 0
                     and self.action_combo.model().item(
                         self.action_combo.currentIndex()).isEnabled())
        has_capacity = self._capacity_value() is not None
        self.save_button.setEnabled(action_ok and has_capacity)
        self.save_button.setToolTip(
            "" if has_capacity or not action_ok else CAPACITY_FIRST_TIP)

    def _fill_defaults(self, chem, mode, chemistry_changed: bool):
        """A chemistry or action was chosen: show that page's settings, take
        back any cutoff that no longer fits, and re-derive what the user has
        not set by hand (the current, and the timer unless it was typed). The capacity is
        the user's own and is never touched. Where the charger has no values
        we have read (a monitor-only chemistry), the fields are left alone."""
        # The Advanced rows follow the same rule: a chemistry or action change
        # shows that page's settings at the charger's own values.
        self._show_advanced(chem, mode)
        if chemistry_changed and chem in CHEMISTRY_DEFAULTS:
            self._retarget_cutoff(chem)
        if configure_defaults(chem, mode) is None:
            return
        # A timer the user typed survives a chemistry or action change (it is
        # the one protection that outlives the app); an untouched one follows.
        self._refresh_derived()

    def _retarget_cutoff(self, chem):
        """The chemistry changed. A cutoff the user typed stays only if it is
        inside the new chemistry's window - the charger would silently swap one
        outside it (see protocol.VOLTAGE_GATES), and a Li-ion figure on a NiMH
        cell becomes 900 mV. Anything else goes back to the chemistry's own."""
        lo, hi = VOLTAGE_GATES[chem]["cutoff_mv"]
        if "cutoff" in self._touched:
            try:
                if lo <= int(self.cutoff_edit.text()) <= hi:
                    return
            except ValueError:
                pass
            self._touched.discard("cutoff")
        self.cutoff_edit.setText(str(CHEMISTRY_DEFAULTS[chem]["cutoff_mv"]))

    def _refresh_derived(self):
        """Work out what is derived and not set by hand: the current from the
        capacity (0.5C charge, 0.2C discharge), then the timer from both."""
        if self._loading or self._repopulating:
            return
        chem = self.chem_combo.currentData()
        mode = self.action_combo.currentData()
        d = configure_defaults(chem, mode)
        if d is not None and "current" not in self._touched:
            got = derive_current_ma(chem, mode, self._capacity_value())
            self.current_edit.setText("" if got is None else str(got))
        if (mode is Mode.STORAGE and d is not None
                and "discharge_current_ma" not in self._touched):
            # Storage's second current is the Advanced row: 0.2C, as a plain
            # Discharge run gets.
            got = derive_storage_discharge_ma(chem, self._capacity_value())
            self.adv_edits["discharge_current_ma"].setText(
                "" if got is None else str(got))
        self._recompute_timer()
        self._update_rate_warning()
        self._update_save_enabled()

    def _current_edited(self, text=None):
        """Typed over the derived current: it is kept from now on (blanking
        the field gives it back to the derivation)."""
        if self.current_edit.text().strip():
            self._touched.add("current")
        else:
            self._touched.discard("current")
            self._refresh_derived()
        self._recompute_timer()

    def _discharge_edited(self, text=None):
        """Typed over Storage's derived discharge current: kept from now on
        (blanking the row gives it back to the derivation)."""
        if self.adv_edits["discharge_current_ma"].text().strip():
            self._touched.add("discharge_current_ma")
        else:
            self._touched.discard("discharge_current_ma")
            self._refresh_derived()
        self._recompute_timer()

    def _cutoff_edited(self, text=None):
        if self.cutoff_edit.text().strip():
            self._touched.add("cutoff")
        else:
            self._touched.discard("cutoff")

    def _capacity_edited(self, text=None):
        self._refresh_derived()

    def _show_advanced(self, chem, mode, existing=None):
        """Show the Advanced rows that apply to `chem` and `mode`, each holding
        the charger's own value - or, for a saved config being reopened, the
        value that config has (a field it left at None shows the default).
        Opens the section by itself when a saved value differs from the
        default, so a changed setting is never hidden from the person looking
        at it; otherwise it stays as the user left it. Storage's main Current
        row is its CHARGE current, since the discharge one is an Advanced row."""
        fields = advanced_fields(chem, mode) if mode is not None else ()
        defaults = advanced_defaults(chem, mode) if mode is not None else {}
        values = dict(defaults)
        if existing is None:
            # A fresh page puts its rows back to the charger's own values, so a
            # discharge current typed on the old page is gone with them.
            self._touched.discard("discharge_current_ma")
        if existing is not None and existing.mode is mode:
            for name in fields:
                saved = getattr(existing, name, None)
                if saved is None and name == "discharge_current_ma":
                    # A storage config saved with one current sends it both ways.
                    saved = existing.current_ma
                if saved is not None:
                    values[name] = saved
        for name, edit in self.adv_edits.items():
            self.advanced_form.setRowVisible(edit, name in fields)
            if name in fields:
                edit.setText(str(values[name]))
        self.advanced_button.setVisible(bool(fields))
        if not fields:
            self.advanced_button.setChecked(False)
        elif existing is not None and values != defaults:
            self.advanced_button.setChecked(True)
        self._form.labelForField(self.current_edit).setText(
            "Charge current (mA)" if mode is Mode.STORAGE else "Current (mA)")
        self._recompute_timer()
        self._update_rate_warning()
        # The rows just changed (Li-ion discharge has one, charge has three), so
        # an open section needs the dialog re-fitted or the notes get squeezed.
        if self.isVisible():
            self._fit_height()

    def _advanced_toggled(self, shown: bool):
        self.advanced_box.setVisible(shown)
        self.advanced_button.setText("Advanced ▾" if shown
                                     else "Advanced ▸")
        self._fit_height()

    def eventFilter(self, obj, event):
        """Enter in the Cell id field accepts the text and moves on to the next
        field instead of pressing Save. A QLineEdit ignores Enter so a dialog's
        default button can have it, which is right for the other fields but not
        for the one a person types or scans into first. While the dropdown's
        suggestion list is open Enter still belongs to it (it accepts the
        suggestion), so it is left alone then."""
        if (obj in (self.cell_edit, self.cell_edit.lineEdit())
                and event.type() == QEvent.Type.KeyPress
                and event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter)):
            completer = self.cell_edit.completer()
            popup = completer.popup() if completer is not None else None
            if popup is None or not popup.isVisible():
                # A real keystroke arrives NOT accepted, and returning True
                # alone lets Qt pass it on up to the dialog's default button.
                event.accept()
                self._cell_enter_at = time.monotonic()
                self.focusNextChild()
                return True
        return super().eventFilter(obj, event)

    # A barcode scanner set to end each scan with CR+LF sends Enter TWICE, a few
    # milliseconds apart. The first is taken by the Cell id field above; the
    # second would land on the next field and press Save (found in bench
    # testing with a scanner). A person cannot press Enter again this fast on purpose, and
    # if they do the key simply has to be pressed once more.
    ENTER_SETTLE_S = 0.3

    def keyPressEvent(self, event):
        if (event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter)
                and time.monotonic() - self._cell_enter_at < self.ENTER_SETTLE_S):
            event.accept()
            return
        super().keyPressEvent(event)

    def _fit_height(self):
        """Give the dialog the height its layout needs at its current width. The
        dialog's own size hint ignores word-wrapped text, so it came out too
        short and the layout squeezed the notes (the temperature-cutoff note
        lost its last lines once Advanced was open). Called when the section
        opens or closes, and when the dialog is first shown."""
        layout = self.layout()
        layout.activate()
        self.resize(self.width(), max(layout.totalHeightForWidth(self.width()),
                                      self.minimumSizeHint().height()))

    def showEvent(self, event):
        super().showEvent(event)
        self._fit_height()

    def _storage_discharge_text(self):
        """Storage's discharge current as typed, or None when this action has
        no second current."""
        if self.action_combo.currentData() is not Mode.STORAGE:
            return None
        return self.adv_edits["discharge_current_ma"].text()

    def _action_changed(self, _index=None):
        """The user chose an action: the current differs between charge and
        discharge, so a derived current and timer follow it; anything typed
        stays. Capacity and cutoff are left."""
        if self._loading or self._repopulating:
            return
        self._fill_defaults(self.chem_combo.currentData(),
                            self.action_combo.currentData(),
                            chemistry_changed=False)
        self._update_rate_warning()

    def _timer_edited(self, _text=None):
        """The user typed in the timer: stop following capacity and current."""
        self._timer_touched = True
        self.timer_note.setVisible(False)

    def _recompute_timer(self, _text=None):
        """Work the timer out from capacity and current, unless the user has
        taken it over (see capture.suggest_safety_timer_min)."""
        if self._timer_touched:
            return
        try:
            capacity = int(self.capacity_edit.text())
            current = int(self.current_edit.text())
            second = self._storage_discharge_text()
            if second is not None:
                # Storage moves the cell both ways; the slower current sets how
                # long it can take.
                current = min(current, int(second))
        except ValueError:
            got = None          # blank or not a number yet: nothing to derive
        else:
            got = suggest_safety_timer_min(self.chem_combo.currentData(),
                                           self.action_combo.currentData(),
                                           capacity, current)
        if got is None:
            self.timer_edit.setText("")
            self.timer_note.setVisible(False)
            return
        minutes, capped = got
        self.timer_edit.setText(str(minutes))
        self.timer_note.setText(TIMER_CAPPED_NOTE if capped else "")
        self.timer_note.setVisible(capped)

    def _update_rate_warning(self, _text=None):
        """Show the 1C warning while the current is above 1C for the capacity
        entered. Warns only; Save is never blocked by it."""
        mode = self.action_combo.currentData()
        try:
            capacity = int(self.capacity_edit.text())
            current = int(self.current_edit.text())
            second = self._storage_discharge_text()
            if second is None:
                text = c_rate_warning(mode, current, capacity)
            else:
                # Storage has two currents; name each by its direction.
                lines = [c_rate_warning(Mode.CHARGE, current, capacity),
                         c_rate_warning(Mode.DISCHARGE, int(second), capacity)]
                text = "\n".join(line for line in lines if line) or None
        except ValueError:
            text = None
        if mode is None:
            text = None
        changed = (self.rate_warning.text() != (text or "")
                   or self.rate_warning.isVisibleTo(self) != bool(text))
        self.rate_warning.setText(text or "")
        self.rate_warning.setVisible(bool(text))
        if changed and self.isVisible():
            # The warning is several wrapped lines; without this the dialog
            # kept its old height and the temperature-cutoff note below it was
            # clipped mid-sentence.
            self._fit_height()

    def build_request(self) -> RunRequest | None:
        """A validated RunRequest, or None with self.error_label set on bad input."""
        try:
            current = int(self.current_edit.text())
            capacity = int(self.capacity_edit.text())
            timer = int(self.timer_edit.text())
            cutoff = int(self.cutoff_edit.text()) if self.cutoff_edit.text().strip() else None
        except ValueError:
            self._fail("current, capacity, cutoff and timer must be numbers")
            return None
        text = self.temp_cutoff_edit.text().strip()
        temp_cutoff = None
        if text:
            unit = self.temp_unit
            try:
                temp_cutoff = from_unit(float(text), unit)
            except ValueError:
                self._fail("the temperature cutoff must be a number, or blank "
                           "for none")
                return None
            if temp_cutoff_problem(temp_cutoff):
                lo = round(to_unit(TEMP_CUTOFF_MIN_C, unit))
                hi = round(to_unit(TEMP_CUTOFF_MAX_C, unit))
                self._fail(f"the temperature cutoff must be {lo}-{hi} °{unit}: "
                           f"lower would trip on a warm charger's idle bay, "
                           f"higher sits on the charger's own 70 °C limit")
                return None
        chem = self.chem_combo.currentData()
        mode = self.action_combo.currentData()
        # Advanced values go in only where they differ from the charger's own,
        # so a run left on the defaults is the request it always was and its
        # meta.json says "the charger's default" (null). The exception is
        # Storage's discharge current: Storage has two currents, so it is
        # always named.
        advanced = {}
        defaults = advanced_defaults(chem, mode)
        for name, default in defaults.items():
            try:
                value = int(self.adv_edits[name].text())
            except ValueError:
                self._fail(f"Advanced: {ADVANCED_LABELS[name]} must be a whole "
                           f"number")
                self.advanced_button.setChecked(True)
                return None
            if value != default or name == "discharge_current_ma":
                advanced[name] = value
        base = RunRequest(
            mode=mode, chemistry=chem,
            current_ma=current, capacity_mah=capacity,
            safety_timer_min=timer, cutoff_mv=cutoff,
            temp_cutoff_c=temp_cutoff,
        )
        req = replace(base, **advanced)
        problem = req.problem()
        if problem:
            self._fail(problem)
            # A refusal that comes from the Advanced rows is shown with them
            # open, so the message is never about something the person cannot see.
            if advanced and base.problem() != problem:
                self.advanced_button.setChecked(True)
            return None
        return req

    def target_slots(self) -> list[int]:
        return [self.slot] + [s for s, b in self.copy_boxes.items() if b.isChecked()]

    def cell_id(self) -> str | None:
        return self.cell_edit.currentText().strip() or None

    def _fail(self, msg):
        self.error_label.setText(msg)
        self.error_label.setVisible(True)
        self._fit_height()

    def _clear_error(self, *_):
        """Drop a refusal once anything it was about has changed - otherwise it
        stayed up, describing a setting the chosen chemistry may not even have
        (a -dV refusal still showing on a Storage page)."""
        if self.error_label.isVisibleTo(self):
            self.error_label.setVisible(False)
            self.error_label.setText("")
            self._fit_height()

    def _accept(self):
        if self.build_request() is None:
            return       # error shown, stay open
        self.accept()


class SlotCard(QGroupBox):
    """
    One slot's live readout, led by cell identity. Labeled columns (Volts /
    Current / Capacity / Resistance) over monospace values, a state line, and
    a color swatch matching the slot's chart curve. Empty slots render as
    empty rather than as 0.000 V; finished slots say so.

    The card carries two independent buttons - Configure (setup) and Start/Stop
    (run) - plus a pending-config line shown while armed or starting. Configure
    is available whenever the slot is not running or mid-start; Start appears
    only on a freshly-armed slot, right below its shown config. Each button
    emits its own signal; the window wires them to the controller. The card
    itself does no control - it only asks.
    """

    configure_requested = Signal(int)
    start_requested = Signal(int)
    stop_requested = Signal(int)
    leave_group_requested = Signal(int)

    COLUMNS = [
        ("Volts", lambda st: f"{st.voltage_v:.3f}"),
        ("Current", lambda st: f"{st.current_ma} mA"),
        ("Capacity", lambda st: f"{st.capacity_mah} mAh"),
        ("Resistance", lambda st: f"{st.resistance_mohm} mΩ"),
    ]

    def __init__(self, slot: int, cell_id: str | None, mode: str):
        super().__init__(cell_id or f"Slot {slot}")
        self.slot = slot
        self.cell_id = cell_id
        self.finished = False
        self.mode = mode
        self.group_id = None
        self.temp_greyed = False
        self.temp_unit = "C"

        # left: the identity swatch (which SLOT, chart color), then the group
        # tag (a shared color + letter tying configured-together slots).
        self.swatch = QLabel("●")
        self.subtitle = QLabel(f"slot {slot}")
        self.subtitle.setStyleSheet("font-size: 11px;")
        self.group_tag = QLabel("")
        self.group_tag.setVisible(False)
        # A chip-style "leave group" next to the tag: one step out of the group
        # (this slot only), without a trip through Configure.
        self.leave_button = QPushButton("✕")
        self.leave_button.setToolTip("Leave group")
        self.leave_button.setFlat(True)
        self.leave_button.setFixedWidth(20)
        self.leave_button.setStyleSheet(
            "QPushButton { border: none; padding: 0; font-size: 11px; }")
        self.leave_button.clicked.connect(
            lambda: self.leave_group_requested.emit(self.slot))
        self.leave_button.setVisible(False)
        head = QHBoxLayout()
        head.addWidget(self.swatch)
        head.addWidget(self.subtitle)
        head.addWidget(self.group_tag)
        head.addWidget(self.leave_button)
        head.addStretch(1)
        # right: the state dot (what STATE, its own palette) + the state word.
        # Two separate color languages, opposite ends of the card, so identity
        # and state never read as the same signal.
        self.state_dot = QLabel("●")
        self.state_label = QLabel("—")
        self.state_label.setStyleSheet("font-size: 11px; font-weight: bold;")
        head.addWidget(self.state_dot)
        head.addWidget(self.state_label)

        grid = QGridLayout()
        grid.setHorizontalSpacing(14)
        self.values = {}
        # Readings currently taken from the run's last good frame (see
        # card_frames) rather than the live one; shown dimmed, with a caption.
        self._held: set[str] = set()
        self.headers = {}
        # The last frame showed an empty bay (no cell, so nothing is held).
        self._empty = False
        for col, (name, _fn) in enumerate(self.COLUMNS):
            lab = QLabel(name)
            lab.setStyleSheet("font-size: 11px;")
            self.headers[name] = lab
            val = QLabel("—")
            val.setStyleSheet("font-family: Consolas, monospace; font-size: 15px;")
            grid.addWidget(lab, 0, col)
            grid.addWidget(val, 1, col)
            self.values[name] = val

        # Slot temp: its own column, fed by set_temp rather than the status
        # frame. The info mark carries the middle-bay note on EVERY card - it
        # is a fact about positions, not a remark about this slot's reading.
        temp_head = QHBoxLayout()
        temp_head.setSpacing(3)
        self.temp_header = QLabel("Slot temp")
        self.temp_header.setStyleSheet("font-size: 11px;")
        self.temp_header.setToolTip(TEMP_TOOLTIP)
        self.bay_info = QLabel("ⓘ")
        self.bay_info.setStyleSheet("font-size: 11px;")
        self.bay_info.setToolTip(MIDDLE_BAY_TOOLTIP)
        temp_head.addWidget(self.temp_header)
        temp_head.addWidget(self.bay_info)
        temp_head.addStretch(1)
        self.temp_value = QLabel("—")
        self.temp_value.setToolTip(TEMP_IDLE_TOOLTIP)
        col = len(self.COLUMNS)
        grid.addLayout(temp_head, 0, col)
        grid.addWidget(self.temp_value, 1, col)
        # One line under the readings for what the temperature is DOING: the
        # peak, a cutoff and what it did, the charger's own thermal stop, or
        # why there is no temperature at all. Never a verdict on the cell.
        self.temp_detail = QLabel("")
        self.temp_detail.setWordWrap(True)
        self.temp_detail.setVisible(False)
        # Says why some readings are dimmed: they are the run's last good ones.
        self.readings_note = QLabel("")
        self.readings_note.setWordWrap(True)
        self.readings_note.setVisible(False)
        # A write failure moved this slot's run to the local default folder.
        # Loud on purpose: the run carries on, but the user chose a folder and
        # the data is no longer going there.
        self.file_warning = QLabel("")
        self.file_warning.setWordWrap(True)
        self.file_warning.setVisible(False)

        # Config line: what the slot is set up for. Chemistry + action from
        # arming onward, held until the next Save (so a finished slot still reads
        # its identity); the exact rate and cutoff are added while armed/starting,
        # where those numbers matter and Start is one press away.
        self.config_label = QLabel("")
        self.config_label.setStyleSheet("font-size: 12px;")
        self.config_label.setVisible(False)
        # Configure is always about the setup; Start/Stop is always about the
        # run. Splitting them means an armed or finished slot can always be
        # reconfigured, and Start only ever appears on a freshly-armed slot whose
        # config is shown right above it - you cannot Start without seeing the
        # chemistry and rate first.
        self.configure_button = QPushButton("Configure")
        self.configure_button.clicked.connect(self._on_configure)
        self.action_button = QPushButton("Start")
        self.action_button.clicked.connect(self._on_action)
        self.state = "unknown"

        buttons = QHBoxLayout()
        buttons.addWidget(self.configure_button)
        buttons.addWidget(self.action_button)

        layout = QVBoxLayout()
        layout.addLayout(head)
        layout.addLayout(grid)
        layout.addWidget(self.readings_note)
        layout.addWidget(self.temp_detail)
        layout.addWidget(self.file_warning)
        layout.addWidget(self.config_label)
        layout.addLayout(buttons)
        self.setLayout(layout)
        self.apply_theme(mode)
        # Render the initial "unknown" state now, before the first frame or
        # refresh tick, so both buttons start correctly DISABLED. Without this
        # they show their constructed default (enabled) during connect/init - a
        # live-looking Start that does nothing.
        self.render_state("unknown")

    def apply_theme(self, mode: str):
        self.mode = mode
        self.swatch.setStyleSheet(
            f"color: {gui_theme.slot_color(self.slot, mode)}; font-size: 14px;")
        p = gui_theme.PALETTES[mode]
        self.subtitle.setStyleSheet(
            f"font-size: 11px; color: {p['text_dim']};")
        self._paint_state_dot()
        self._paint_group_tag()
        self._paint_temp()
        self._paint_values()
        self.file_warning.setStyleSheet(
            f"font-size: 11px; font-weight: bold; color: {p['warn']};")

    def set_file_warning(self, text: str | None):
        """Show (or clear) the slot's file warning."""
        text = f"⚠ {text}" if text else ""
        if text != self.file_warning.text():
            self.file_warning.setText(text)
            self.file_warning.setVisible(bool(text))

    def _paint_temp(self):
        """The reading's color depends ONLY on whether it is a live reading
        or a finished run's last one - never on its value, and never on any
        other slot's (see the rule above temp_view)."""
        p = gui_theme.PALETTES[self.mode]
        colour = f" color: {p['text_dim']};" if self.temp_greyed else ""
        self.temp_value.setStyleSheet(
            f"font-family: Consolas, monospace; font-size: 15px;{colour}")
        self.temp_detail.setStyleSheet(
            f"font-size: 11px; color: {p['text_dim']};")

    def set_temp(self, value: str, detail: str, greyed: bool):
        """Show one slot's temperature, as temp_view produced it."""
        self.temp_value.setText(value)
        self.temp_value.setToolTip(TEMP_IDLE_TOOLTIP if value == "—"
                                   else TEMP_TOOLTIP)
        self.temp_detail.setText(detail)
        self.temp_detail.setVisible(bool(detail))
        if greyed != self.temp_greyed:
            self.temp_greyed = greyed
            self._paint_temp()

    def _paint_state_dot(self):
        self.state_dot.setStyleSheet(
            f"color: {gui_theme.state_color(self.state, self.mode)}; "
            f"font-size: 13px;")

    def set_group(self, group_id):
        """Tie this card into a group (a shared color + letter), or None to
        stand alone. Groups change only when a slot is configured."""
        self.group_id = group_id
        self._paint_group_tag()

    def _paint_group_tag(self):
        if self.group_id is None:
            self.group_tag.setVisible(False)
            self.leave_button.setVisible(False)
            return
        self.leave_button.setVisible(True)
        # Letter for redundancy (readable without relying on color); color to
        # tie non-adjacent cards at a glance.
        self.group_tag.setText(f"◆ Group {group_letter(self.group_id)}")
        self.group_tag.setStyleSheet(
            f"color: {gui_theme.group_color(self.group_id - 1, self.mode)}; "
            f"font-size: 11px; font-weight: bold;")
        self.group_tag.setVisible(True)

    def _paint_values(self):
        """Readings taken from the run's last good frame are dimmed - the same
        convention as a finished run's temperature. Never color by value."""
        p = gui_theme.PALETTES[self.mode]
        for name, lab in self.values.items():
            colour = f" color: {p['text_dim']};" if name in self._held else ""
            lab.setStyleSheet(
                f"font-family: Consolas, monospace; font-size: 15px;{colour}")
        self.readings_note.setStyleSheet(
            f"font-size: 11px; color: {p['text_dim']};")

    def add(self, sample, state: str | None = None, last_good=None,
            stopped_by_user: bool = False, unwatched_end: bool = False):
        """Show one frame. `state` and `last_good` (the capture's last frame
        with the counters still advancing) let a slot the charger has torn
        down keep showing its last real reading - see card_frames.
        `stopped_by_user`: the card's Stop ended this run (Capture.user_stopped),
        which the caption says, mirroring a run the charger ended.
        `unwatched_end`: the run ended while the link was down
        (Capture.unwatched_end); the caption says the figures are the last
        readings received, and outranks the other two captions because it is
        the one that tells the reader the numbers may be short."""
        st = sample.status
        self._empty = st.state == "empty"
        if self._empty:
            for v in self.values.values():
                v.setText("—")
            self._held = set()
            self.readings_note.setVisible(False)
            self._paint_values()
            return
        unwatched = bool(unwatched_end and state in ("latched", "finished"))
        frames, held = card_frames(st, state or "", last_good, unwatched)
        for name, fn in self.COLUMNS:
            self.values[name].setText(fn(frames[name]))
        if state == "disconnected":
            held_cols = set(frames)          # nothing on the card is live
        else:
            held_cols = ({n for n, f in frames.items() if f is not st}
                         if held else set())
        if held_cols != self._held:
            self._held = held_cols
            self._paint_values()
        if state == "disconnected":
            note = "Not connected: these are the last readings received."
        elif unwatched and held:
            note = (UNWATCHED_END_NOTE)
        elif state == "latched":
            note = "Last good readings: the charger ended this run."
        elif state == "finished" and stopped_by_user:
            note = ("Stopped by you. Capacity and resistance are the run's "
                    "last readings.")
        else:
            note = "Capacity and resistance are the run's last readings."
        if held or (state == "finished" and stopped_by_user):
            self.readings_note.setText(note)
        self.readings_note.setVisible(
            held or (state == "finished" and stopped_by_user))

    def render_state(self, state: str, config=None):
        """
        Set the state line, the config line, and both buttons to match the slot
        state. `config` is the slot's last-saved RunRequest (held from arming
        until the next Save), or None if it was never configured.

        The config line shows the chemistry + action whenever there is a config
        - from armed through finished, the persistent "set up for" identity -
        and adds the exact rate and cutoff while armed/starting, where those
        numbers matter and Start is one press away.
        """
        self.state = state
        self.state_label.setText(state)
        self._paint_state_dot()          # the non-text, at-a-glance state cue
        held = state in HELD_VOLTS_STATES and not self._empty
        self.headers["Volts"].setText(HELD_VOLTS_HEADER if held else "Volts")
        self.headers["Volts"].setToolTip(HELD_VOLTS_TOOLTIP if held else "")
        if config is None:
            self.config_label.setVisible(False)
        else:
            self.config_label.setText(
                describe_config(config, full=state in ("armed", "starting"),
                                unit=self.temp_unit))
            self.config_label.setVisible(True)

        # Configure: available whenever the slot is not running or mid-start.
        # Configure is allowed on a latched slot - the config simply waits
        # behind the Stop that clears the latch.
        # Deliberately excludes "measuring": the pre-flight sampler is arming
        # and stopping this slot, and a config landing mid-cycle would race it.
        self.configure_button.setEnabled(
            state in ("idle", "armed", "finished", "latched"))

        # Start/Stop: Start only on a freshly-armed slot (config shown above),
        # Stop while running, a disabled "Starting…" during the commit window,
        # and a disabled Start otherwise so the button's slot never vanishes.
        action = {
            "running": ("Stop", True),
            "starting": ("Starting…", False),
            "armed": ("Start", True),
            # The latch is cleared by stopping it, so that is the only offer.
            "latched": ("Stop", True),
            # The sampler owns the slot for a second or two; interfering would
            # race its arm/stop cycle.
            "measuring": ("Measuring IR…", False),
        }.get(state, ("Start", False))
        self.action_button.setText(action[0])
        self.action_button.setEnabled(action[1])

    def _on_configure(self):
        self.configure_requested.emit(self.slot)

    def _on_action(self):
        if self.state in ("running", "latched"):
            self.stop_requested.emit(self.slot)
        elif self.state == "armed":
            self.start_requested.emit(self.slot)

    def mark_finished(self):
        self.finished = True


class GroupControlCard(QGroupBox):
    """
    A control card for one group: its group-colored "Group X" title, its
    members, and Start / Stop / Ungroup buttons that fan the action out to the
    grouped slots. The card only asks; MainWindow wires the signals to the
    controller. Start acts on the armed members, Stop on the running ones, so a
    group in mixed states does the sensible thing for each slot.
    """

    start_requested = Signal(int)
    stop_requested = Signal(int)
    ungroup_requested = Signal(int)

    def __init__(self, group_id: int, members, mode: str):
        super().__init__(f"Group {group_letter(group_id)}")
        self.group_id = group_id
        self.members_label = QLabel("")
        self.members_label.setStyleSheet("font-size: 11px;")

        self.start_button = QPushButton("Start")
        self.start_button.clicked.connect(
            lambda: self.start_requested.emit(self.group_id))
        self.stop_button = QPushButton("Stop")
        self.stop_button.clicked.connect(
            lambda: self.stop_requested.emit(self.group_id))
        self.ungroup_button = QPushButton("Ungroup")
        self.ungroup_button.clicked.connect(
            lambda: self.ungroup_requested.emit(self.group_id))

        buttons = QHBoxLayout()
        buttons.addWidget(self.start_button)
        buttons.addWidget(self.stop_button)
        buttons.addWidget(self.ungroup_button)

        layout = QVBoxLayout()
        layout.addWidget(self.members_label)
        layout.addLayout(buttons)
        self.setLayout(layout)
        self.update_members(members, mode)

    def update_members(self, members, mode: str):
        self.members_label.setText(
            "slots " + ", ".join(str(s) for s in members))
        # Title + border in the group's color, matching the ◆ tags on the slot
        # cards so the tie reads across both.
        p = gui_theme.PALETTES[mode]
        colour = gui_theme.group_color(self.group_id - 1, mode)
        self.setStyleSheet(
            f"QGroupBox {{ background: {p['card']}; border: 1px solid {colour}; "
            f"border-radius: 8px; margin-top: 10px; padding-top: 8px; }} "
            f"QGroupBox::title {{ subcontrol-origin: margin; left: 10px; "
            f"padding: 0 4px; color: {colour}; font-weight: bold; }}")

    def set_action_states(self, can_start: bool, can_stop: bool):
        self.start_button.setEnabled(can_start)
        self.stop_button.setEnabled(can_stop)


WINDOW_SIZE = (940, 700)
WINDOW_MARGIN = 12       # px in from the left and top of the usable screen
WINDOW_TITLEBAR = 40     # room kept for the title bar, which resize() excludes


def initial_geometry(avail: QRect, size=WINDOW_SIZE,
                     margin=WINDOW_MARGIN) -> QRect:
    """Where the window first opens: a few pixels in from the top-left of the
    usable screen area `avail`, at `size` but never larger than that area. The
    position is set rather than left to Windows, which put a new window where
    its last one was or wherever it liked."""
    w = min(size[0], avail.width() - 2 * margin)
    h = min(size[1], avail.height() - margin - WINDOW_TITLEBAR)
    return QRect(avail.left() + margin, avail.top() + margin,
                 max(w, 1), max(h, 1))


def wrap_not_widen(label: QLabel) -> QLabel:
    """Let a label's text wrap instead of setting the window's minimum width.
    An unwrapped label's width is part of the window's minimum, so the long
    no-adapter sentence made the window ~1575 px wide (seen on the VM) and, on
    a narrower screen, pushed its right edge off it. Ignoring the horizontal
    hint means no message, however long, can resize the window."""
    label.setWordWrap(True)
    label.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
    return label


class MainWindow(QMainWindow):
    # A Stop (or a close) is being waited out. The wait pumps Qt's events, so a
    # click on the window's X is delivered in the middle of it: it must neither
    # ask the question again nor re-enter the stop. `_close_after_stop` makes
    # the window close once the stop is done.
    _stopping = False
    _close_after_stop = False

    def __init__(self, controller: CaptureController, slots, theme_choice: str):
        super().__init__()
        self.controller = controller
        self.theme_choice = theme_choice
        self.mode = gui_theme.resolve(theme_choice)
        self.slots = slots
        self.setWindowTitle(f"SITREP91 {build_label()} · SkyRC MC5000"
                            + (DEMO_TITLE_NOTE if controller.simulated else ""))
        screen = self.screen() or QApplication.primaryScreen()
        if screen is not None:
            geo = initial_geometry(screen.availableGeometry())
            self.resize(geo.size())
            self.move(geo.topLeft())
        else:
            self.resize(*WINDOW_SIZE)

        cells = getattr(controller, "cells", None) or {}
        # Display only - files stay in C. From settings.ini, beside the code.
        self.temp_unit = load_temp_unit()
        self.cards = {s: SlotCard(s, cells.get(s), self.mode) for s in slots}
        for card in self.cards.values():
            card.temp_unit = self.temp_unit
        for s, card in self.cards.items():
            card.configure_requested.connect(self.on_configure_slot)
            card.start_requested.connect(self.on_start_slot)
            card.stop_requested.connect(self.on_stop_slot)
            card.leave_group_requested.connect(self.on_leave_group)
        self.chart = SharedChart(slots, self.mode, unit=self.temp_unit)
        self.counts = defaultdict(int)
        self.finished = set()
        self.drift_warns = 0
        self.drift_errors = 0
        self._last_refusal = None

        # Drift banner: hidden until a warn/error arrives, then persistent and
        # loud. A drift error means a reading was structurally or physically
        # wrong - the whole point of drift.py - and a monitor whose only alarm
        # is a transient status line the next sample overwrites is not loud
        # enough. Once raised it stays for the rest of the run, because a run
        # that drifted is suspect even if later frames look clean.
        self.drift_banner = QLabel()
        self.drift_banner.setVisible(False)
        self.drift_banner.setWordWrap(True)

        # Stale-link banner: the charger has stopped being found and probably
        # needs a POWER CYCLE (capture.stale_link_message). It must outlive
        # the status line, which the retry messages overwrite every few
        # seconds, and it clears itself when the link comes back.
        self.link_banner = QLabel()
        self.link_banner.setVisible(False)
        self.link_banner.setWordWrap(True)

        # Slot strip: cards side by side, cell identity leading.
        strip = QHBoxLayout()
        for s in slots:
            strip.addWidget(self.cards[s])
        strip.addStretch(1)

        # Group control cards: a row directly below the slot strip, shown only
        # when at least one group exists (no cost in the common single case).
        self.group_cards = {}
        self.group_row = QHBoxLayout()
        self.group_row.addStretch(1)
        self.group_row_container = QWidget()
        self.group_row_container.setLayout(self.group_row)
        self.group_row_container.setVisible(False)

        # Health bar: the quiet always-on status (samples / drift / gaps / CSV).
        # The loud drift banner above handles alarms; this is the pulse.
        self.health_label = wrap_not_widen(QLabel("idle"))
        self.health_label.setStyleSheet("font-size: 12px;")

        self.status_label = wrap_not_widen(QLabel(""))
        # Always visible: is this window talking to a charger right now? A
        # session that had ended looked, after a Stop, like a connected one.
        # Restyled every second, so a theme change and a
        # link change both show without waiting for a status line.
        self.connection_label = QLabel("DISCONNECTED")
        self.connection_label.setToolTip(
            "Whether this window is connected to the charger right now. "
            "DISCONNECTED means nothing is being recorded and no command can "
            "be sent.")
        self._hold_text: str | None = None
        self._hold_until = 0.0
        self._latest_status = ""
        self.stop_button = QPushButton("Stop")
        self.stop_button.clicked.connect(self.on_stop_clicked)
        # Shown only once a session has ended: the way back to a live one
        # without closing and reopening the app (no charger found, a link that
        # did not recover, a Stop).
        self.retry_button = QPushButton("Retry")
        self.retry_button.setToolTip(
            "Look for the charger and connect again. Settings you have armed "
            "are kept; nothing you pressed while it was away is sent.")
        self.retry_button.clicked.connect(self.on_retry_clicked)
        self.retry_button.setVisible(False)
        self.settings_button = QPushButton("Settings")
        self.settings_button.clicked.connect(self.on_settings_clicked)
        # The All Stop: kept on the far side of the bar from Stop so the two
        # cannot be confused mid-reach, and always behind a confirmation.
        self.all_stop_button = QPushButton("ALL STOP")
        self.all_stop_button.clicked.connect(self.on_all_stop_clicked)

        bar = QHBoxLayout()
        bar.addWidget(self.all_stop_button)
        bar.addWidget(self.status_label, stretch=1)
        bar.addWidget(self.connection_label)
        bar.addWidget(self.retry_button)
        bar.addWidget(self.settings_button)
        bar.addWidget(self.stop_button)

        # The console: the full status stream (what the terminal shows), in a
        # scrolling read-only panel. Off by default - the one-line status label
        # is enough for normal use - and toggled from Settings, persisted.
        self.show_console = gui_theme.load_show_console()
        self.console = QPlainTextEdit()
        self.console.setReadOnly(True)
        self.console.setMaximumBlockCount(2000)     # bound memory on long runs
        self.console.setMaximumHeight(150)
        self.console.setVisible(self.show_console)

        root = QVBoxLayout()
        root.addWidget(self.drift_banner)
        root.addWidget(self.link_banner)
        root.addLayout(strip)
        root.addWidget(self.group_row_container)
        root.addWidget(self.chart, stretch=1)
        root.addWidget(self.health_label)
        root.addWidget(self.console)
        root.addLayout(bar)

        central = QWidget()
        central.setLayout(root)
        self.setCentralWidget(central)

        # Temperature readings arrive every 30 s and age between them; a
        # stale reading must say so even when no sample happens to arrive.
        self.temp_timer = QTimer(self)
        self.temp_timer.timeout.connect(self._refresh_temps)
        self.temp_timer.start(5000)
        self.conn_timer = QTimer(self)
        self.conn_timer.timeout.connect(self._refresh_connection)
        self.conn_timer.start(1000)
        self._refresh_connection()

    # --- slots receiving bridge signals; these run on the GUI thread ---

    def on_sample(self, sample):
        card = self.cards.get(sample.slot)
        if card is None:
            return

        # Finished-detection is the capture's, not ours - mirror it so a card
        # reads "finished" the moment capture.py declares the slot done.
        cap = self.controller.cap
        if cap is not None:
            newly = set(cap.finished) - self.finished
            for s in newly:
                if s in self.cards:
                    self.cards[s].mark_finished()
            # ...and un-mirror a slot the capture has seen start a new run.
            for s in self.finished - set(cap.finished):
                if s in self.cards:
                    self.cards[s].finished = False
            self.finished = set(cap.finished)

        # Render through the controller's state_for using THIS sample's fresh
        # status - the same resolver slot_state uses, so the "starting" window
        # is cleared consistently on this frequent path. Deriving a parallel
        # state here instead (an earlier mistake) skipped that resolution:
        # _starting was never cleared, so the card flickered "starting" ->
        # "armed" and a stopped slot read "armed" off a stuck pending config.
        # One resolver, one truth.
        state = self.controller.state_for(
            sample.slot, sample.status,
            bool(cap and sample.slot in cap.finished))
        # The capture's last frame with the counters still advancing: what the
        # card shows once the charger has zeroed them (see card_frames).
        last_good = (getattr(cap, "final_by_slot", {}).get(sample.slot)
                     if cap is not None else None)
        card.add(sample, state, last_good,
                 stopped_by_user=bool(cap is not None and sample.slot
                                      in getattr(cap, "user_stopped", ())),
                 unwatched_end=bool(cap is not None and getattr(
                     cap, "unwatched_end", lambda _s: False)(sample.slot)))
        if cap is not None:
            self.chart.temp_interval_s = (getattr(cap, "temp_interval_s", None)
                                          or TEMP_INTERVAL_S)
        self.chart.add(sample)
        card.render_state(state, self.controller.last_config(sample.slot))
        card.set_temp(*self.controller.slot_temp_view(
            sample.slot, self.temp_unit, state=state))
        card.set_file_warning(self.controller.file_warning(sample.slot))
        self.counts[sample.slot] += 1
        if sample.drift_severity == "error":
            self.drift_errors += 1
            self._refresh_drift_banner()
        elif sample.drift_severity == "warn":
            self.drift_warns += 1
            self._refresh_drift_banner()
        self._refresh_health()
        self._refresh_group_states()   # group buttons track live slot states

    def _refresh_cards(self):
        for s, card in self.cards.items():
            card.render_state(self.controller.slot_state(s),
                              self.controller.last_config(s))
            card.set_group(self.controller.group_id_of(s))
            card.set_file_warning(self.controller.file_warning(s))
        self._refresh_temps()
        self._refresh_groups()

    def _refresh_temps(self):
        """Every card's temperature, each from its own slot's facts alone."""
        for s, card in self.cards.items():
            card.set_temp(*self.controller.slot_temp_view(s, self.temp_unit))

    def _refresh_groups(self):
        """Sync the group control row: rebuild it when the set of groups changes,
        re-theme and re-label the cards, and update their button states."""
        current = self.controller.group_ids()
        if set(current) != set(self.group_cards):
            while self.group_row.count():
                item = self.group_row.takeAt(0)
                if item.widget():
                    item.widget().deleteLater()
            self.group_cards = {}
            for gid in current:
                gcard = GroupControlCard(
                    gid, self.controller.group_members(gid), self.mode)
                gcard.start_requested.connect(self.on_group_start)
                gcard.stop_requested.connect(self.on_group_stop)
                gcard.ungroup_requested.connect(self.on_ungroup)
                self.group_cards[gid] = gcard
                self.group_row.addWidget(gcard)
            self.group_row.addStretch(1)
            self.group_row_container.setVisible(bool(current))
        for gid, gcard in self.group_cards.items():
            gcard.update_members(self.controller.group_members(gid), self.mode)
        self._refresh_group_states()

    def _refresh_group_states(self):
        """Just the group buttons' enabled state - cheap enough for every frame,
        so Start/Stop track members starting and finishing on their own."""
        for gid, gcard in self.group_cards.items():
            members = self.controller.group_members(gid)
            can_start = any(self.controller.slot_state(s) == "armed"
                            for s in members)
            can_stop = any(self.controller.slot_state(s) == "running"
                           for s in members)
            gcard.set_action_states(can_start, can_stop)

    def on_group_start(self, gid):
        self.report(self.controller.start_group(gid))
        self._refresh_cards()

    def on_group_stop(self, gid):
        self.report(self.controller.stop_group(gid))
        self._refresh_cards()

    def on_ungroup(self, gid):
        self.controller.ungroup(gid)
        self._refresh_cards()

    # --- per-slot configuration ---

    def on_configure_slot(self, slot):
        dlg = ConfigDialog(self, slot, self.slots,
                           cell_id=(self.controller.cells or {}).get(slot),
                           chemistry=self.controller.chemistry,
                           existing=self.controller.last_config(slot),
                           temp_unit=self.temp_unit,
                           known_cells=runfiles.past_cell_names(
                               self.controller.runs_dir))
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        request = dlg.build_request()
        if request is None:
            return
        targets = dlg.target_slots()
        # Capture the group these exact slots were already in BEFORE arming
        # dissolves it, so re-running the same set keeps its letter (sticky).
        prior_gid = (self.controller.group_id_for_exact(targets)
                     if len(targets) > 1 else None)
        # Cell identity is per slot: only the configured slot takes the entered
        # id; copy-to-slot duplicates the test PARAMETERS, not the cell.
        if self.controller.cells is None:
            self.controller.cells = {}
        if dlg.cell_id():
            self.controller.cells[slot] = dlg.cell_id()
            self.cards[slot].setTitle(dlg.cell_id())
        for t in targets:
            problem = self.controller.arm(t, request)
            if problem:
                self.report(f"slot {t}: {problem}")
            else:
                self.controller.note(
                    f"slot {t} configured: "
                    f"{describe_config(request, full=True, unit='C')}"
                    + (f", cell {self.controller.cells.get(t)}"
                       if self.controller.cells.get(t) else ""))
        # Form the group ONCE for the whole set (not per-slot), reusing the prior
        # id so the letter is sticky. Saving only arms; starting is always a
        # separate press, so a run never begins from the configure click.
        if len(targets) > 1:
            self.controller.group(targets, keep_id=prior_gid)
        self._refresh_cards()

    def on_leave_group(self, slot):
        self.controller.leave_group(slot)
        self._refresh_cards()

    def on_start_slot(self, slot):
        self.report(self.controller.start_slot(slot))
        self._refresh_cards()

    def on_stop_slot(self, slot):
        self.report(self.controller.stop_slot(slot))
        self._refresh_cards()

    def _refresh_health(self):
        total = sum(self.counts.values())
        cap = self.controller.cap
        # Link losses, not reconnect attempts - see capture.Capture.gaps.
        gaps = len(cap.gaps) if cap else 0
        parts = [f"{total} samples"]
        if getattr(cap, "firmware_verified", None) is False:
            # Never a silent blank: say WHY there is no temperature. A version
            # not yet read is a different story from one read and unsupported.
            note_fn = getattr(cap, "temperature_note", None)
            note = note_fn() if callable(note_fn) else None
            if note and "once the firmware is checked" in note:
                parts.insert(0, "UNVERIFIED FIRMWARE (not read yet): watching "
                                "only - " + note)
            else:
                parts.insert(0, "UNVERIFIED FIRMWARE: watching only, "
                                "no temperature")
        if self.drift_errors or self.drift_warns:
            parts.append(f"drift {self.drift_errors}E/{self.drift_warns}W")
        else:
            parts.append("drift clean")
        if gaps:
            parts.append(f"{gaps} link loss{'es' if gaps != 1 else ''}")
        if self.finished:
            parts.append(f"finished {sorted(self.finished)}")
        parts.append(f"runs: {self.controller.runs_dir}")
        self.health_label.setText("   |   ".join(parts))

    def _refresh_link_banner(self):
        cap = self.controller.cap
        if cap is not None:
            # The health line carries the firmware label, and the verdict is
            # made (and logged) before the first sample would refresh it.
            self._refresh_health()
        warning = getattr(cap, "stale_link_warning", None) if cap else None
        if not warning:
            self.link_banner.setVisible(False)
            return
        p = gui_theme.PALETTES[self.mode]
        self.link_banner.setText("  ⚠  CHARGER NOT FOUND — "
                                 + warning.replace("\n", " "))
        self.link_banner.setStyleSheet(
            f"QLabel {{ background: {p['error']}; color: {p['card']}; "
            f"font-weight: bold; padding: 6px 10px; border-radius: 6px; }}")
        self.link_banner.setVisible(True)

    def _refresh_drift_banner(self):
        if not (self.drift_errors or self.drift_warns):
            self.drift_banner.setVisible(False)
            return
        p = gui_theme.PALETTES[self.mode]
        # Error color wins once any error has occurred - a warning-colored
        # banner must never mask that a reading was actually wrong.
        colour = p["error"] if self.drift_errors else p["warn"]
        parts = []
        if self.drift_errors:
            parts.append(f"{self.drift_errors} error"
                         f"{'s' if self.drift_errors != 1 else ''}")
        if self.drift_warns:
            parts.append(f"{self.drift_warns} warning"
                         f"{'s' if self.drift_warns != 1 else ''}")
        self.drift_banner.setText(
            f"  ⚠  PROTOCOL DRIFT: {', '.join(parts)} — readings may "
            f"be wrong; see the session log. This run is suspect.")
        self.drift_banner.setStyleSheet(
            f"QLabel {{ background: {colour}; color: {p['card']}; "
            f"font-weight: bold; padding: 6px 10px; border-radius: 6px; }}")
        self.drift_banner.setVisible(True)

    def _remember_refusal(self, msg):
        """A guardrail refusal reason, kept so on_stopped can show it - the
        transient status line would otherwise be lost when the run ends."""
        if "REFUSING TO START:" in msg:
            self._last_refusal = msg.split("REFUSING TO START:", 1)[1].strip()

    def report(self, msg):
        """A line the window itself produced: shown like any status line,
        and kept in the session log, so a problem report has the user's side
        of the story as well as the charger's."""
        self.controller.note(msg)
        self.on_status(msg)

    def on_status(self, msg):
        self._remember_refusal(msg)
        # Every retry logs a line, so an outage keeps this called while no
        # samples arrive - which is when the banner matters.
        self._refresh_link_banner()
        self._latest_status = msg
        # The console keeps the full history regardless of whether it is shown,
        # so toggling it on mid-run reveals what already scrolled past.
        self.console.appendPlainText(msg)
        if is_refusal(msg):
            self._hold_status(msg)
            return
        if self._status_held():
            return          # a refusal is on screen: leave it long enough to read
        self.status_label.setStyleSheet("")   # normal; on_stopped colors endings
        self.status_label.setText(msg)

    def _status_held(self) -> bool:
        return (self._hold_text is not None
                and time.monotonic() < self._hold_until)

    def _hold_status(self, msg):
        """Show a refusal in the warning color and keep other status lines off
        the label for STATUS_HOLD_MS, then put the latest one back."""
        p = gui_theme.PALETTES[self.mode]
        self.status_label.setStyleSheet(f"color: {p['warn']}; font-weight: bold;")
        self.status_label.setText(msg)
        self._hold_text = msg
        self._hold_until = time.monotonic() + STATUS_HOLD_MS / 1000.0
        QTimer.singleShot(STATUS_HOLD_MS, self._release_status_hold)

    def _release_status_hold(self):
        if self._hold_text is None or self._status_held():
            return          # nothing held, or a newer refusal re-armed the hold
        shown = self.status_label.text() == self._hold_text
        self._hold_text = None
        if shown:           # on_stopped may have written an ending meanwhile
            self.status_label.setStyleSheet("")
            self.status_label.setText(self._latest_status)

    def _refresh_connection(self):
        """Paint the connection marker from the controller's live state."""
        c = self.controller
        cap = c.cap
        kind, text = connection_marker(
            c.simulated, not c.link_down,
            getattr(cap, "device_name", None) if cap is not None else None)
        p = gui_theme.PALETTES[self.mode]
        if kind == "demo":
            style = (f"QLabel {{ color: {p['text_dim']}; font-weight: bold; "
                     f"padding: 4px 10px; }}")
        else:
            colour = p["ok"] if kind == "connected" else p["error"]
            style = (f"QLabel {{ background: {colour}; color: {p['card']}; "
                     f"font-weight: bold; padding: 4px 10px; "
                     f"border-radius: 6px; }}")
        if self.connection_label.text() != text:
            self.connection_label.setText(text)
        if self.connection_label.styleSheet() != style:
            self.connection_label.setStyleSheet(style)

    def on_stopped(self, reason):
        # A bad ending must not look like a clean one. Refusals and failures
        # carry a reason that the transient status line would otherwise lose,
        # and the label is colored so the outcome reads at a glance.
        p = gui_theme.PALETTES[self.mode]
        colour = None
        detail = getattr(self.controller.source, "last_error", None)
        if reason == "refused":
            why = self._last_refusal or "a guardrail declined to start"
            text = f"REFUSED: {why}"
            colour = p["error"]
        elif (reason == "error" and detail
              and getattr(self.controller.source, "error_is_plain", False)):
            text = detail
            colour = p["error"]
        elif reason == "error" and detail:
            where = getattr(self.controller.source, "crash_log", None)
            text = (f"capture failed: {detail}   (details: "
                    f"{where or 'the session log'})")
            colour = p["error"]
        elif reason == "drift":
            text = ("ended on protocol drift errors — the data is suspect; "
                    "see the session log")
            colour = p["error"]
        elif reason == "no-device":
            # The line BleSource sets ("no MC5000 found ...") used to be
            # overwritten at once by "capture ended: no-device | runs saved",
            # leaving a frozen window with nothing to do.
            text = ("No MC5000 found - is the charger awake and in range? "
                    "Another SITREP91 window or SkyRC's phone app may be "
                    "holding it: close it, then press Retry (or close and "
                    "reopen SITREP91). (Nothing was recorded.)")
            colour = p["warn"]
        elif reason == "disconnected":
            text = "ended: Bluetooth disconnected and did not recover"
            colour = p["warn"]
        elif reason == "duration":
            text = ("recording ended at the --max-hours limit; a run still "
                    "going was left going on the charger, not stopped   |   "
                    f"runs saved in {self.controller.runs_dir}")
            colour = p["warn"]
        else:   # done, stopped - clean endings
            text = (f"capture ended: {reason}   |   runs saved in "
                    f"{self.controller.runs_dir}")
        self.status_label.setStyleSheet(
            f"color: {colour}; font-weight: bold;" if colour else "")
        self.status_label.setText(text)
        self.stop_button.setEnabled(False)
        self.retry_button.setVisible(True)
        self.retry_button.setEnabled(True)
        self._mark_disconnected()

    def _mark_disconnected(self):
        """The session is over: every card re-renders as "disconnected" from
        the last frame it had, readings dimmed and every action disabled - no
        card may go on claiming "running" with a live-looking Stop."""
        cap = self.controller.cap
        for s, card in self.cards.items():
            state = self.controller.slot_state(s)
            st = cap.latest.get(s) if cap is not None else None
            if st is not None:
                last_good = getattr(cap, "final_by_slot", {}).get(s)
                card.add(SimpleNamespace(status=st), state, last_good)
            card.render_state(state, self.controller.last_config(s))
        self._refresh_temps()
        self._refresh_group_states()

    def _stop_question(self) -> str:
        """
        The right warning depends on who owns the operation: ending the
        session stops the runs THIS app started (the session's exit sends each
        a per-slot stop) and leaves everything else on the charger running. It
        is decided from what is running now, not from how the app was
        launched - a run started from a card's Start is stopped too, and the
        dialog used to say the opposite.
        """
        if self.controller.simulated:
            return ("This ends the demo.\n"
                    "Nothing real is running: no charger is connected.\n\n"
                    "End the demo?")
        ours = self.controller.app_started_running()
        if (self.controller.is_running
                and not getattr(self.controller.cap, "link_ok", True)):
            # No link: closing cannot send a stop. Say so, and where the run
            # can be stopped.
            if ours:
                names = ", ".join(f"slot {s}" for s in ours)
                return (f"SITREP91 is NOT connected to the charger right now, "
                        f"so it cannot stop anything.\n"
                        f"The run it started on {names} keeps going on the "
                        f"charger until its own safety timer ends it. To stop "
                        f"it, use the charger's panel.\n"
                        f"Closing now ends data collection.\n\n"
                        f"Stop data collection anyway?")
            return ("SITREP91 is NOT connected to the charger right now, so it "
                    "cannot stop anything.\n"
                    "Whatever is on the charger keeps running; use the "
                    "charger's panel to stop it.\n\n"
                    "Stop data collection anyway?")
        if ours:
            names = ", ".join(f"slot {s}" for s in ours)
            return (f"This stops the run SITREP91 started on {names} on the "
                    f"charger AND ends data collection.\n"
                    f"A run started at the charger's panel or in SkyRC's app "
                    f"is left running.\n\nStop?")
        return ("This ends data collection.\n"
                "SITREP91 started nothing that is still running, so whatever "
                "is on the charger keeps running.\n\n"
                "Stop data collection?")

    def confirm_stop(self) -> bool:
        """
        Ask before ending a live capture. A separate method so tests (and any
        future auto-shutdown path) can substitute the answer without a dialog.
        """
        # Brief on purpose; the full continuity story lives in README's
        # "Getting clean captures". Samples already written are safe either
        # way - what stopping costs is the rest of this run's curve.
        #
        # The wording depends on the link, and the link can change while the
        # box is open (the app retries every 3 s). So a Yes is checked: if the
        # question would now read differently, the person answered a different
        # question, and is asked the right one.
        text = self._stop_question()
        for _ in range(3):
            reply = QMessageBox.question(
                self, "Stop?", text,
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No)
            if reply != QMessageBox.StandardButton.Yes:
                return False
            now = self._stop_question()
            if now == text:
                return True
            text = now
        return False        # it kept changing: do nothing rather than guess

    def confirm_all_stop(self) -> bool:
        """Always asks - the All Stop reaches beyond runs this window owns."""
        reply = QMessageBox.question(
            self, "All Stop?",
            "Halt EVERY slot on the charger, whatever started it -\n"
            "including operations set up by hand on the panel.\n\n"
            "All Stop?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No)
        return reply == QMessageBox.StandardButton.Yes

    def on_all_stop_clicked(self):
        if self.confirm_all_stop():
            self.controller.note("ALL STOP pressed")
            self.controller.all_stop()

    def on_retry_clicked(self):
        """Start a new session. The window's own record of the last one -
        drift counts, finished slots, banners, the chart - goes with it."""
        if self.controller.is_running:
            return
        self.controller.note("Retry pressed: starting a new session")
        self.retry_button.setVisible(False)
        self.finished = set()
        self.drift_warns = self.drift_errors = 0
        self._last_refusal = None
        self.drift_banner.setVisible(False)
        self.link_banner.setVisible(False)
        self.chart.clear()
        self.status_label.setStyleSheet("")
        self.status_label.setText("scanning for the charger ...")
        self.stop_button.setEnabled(True)
        if not self.controller.restart():
            self.retry_button.setVisible(True)

    def _stopping_text(self) -> str:
        """The status line while a Stop is waited out: with no link nothing is
        sent to the charger, and saying slots are being stopped would be false."""
        if (self.controller.is_running
                and not getattr(self.controller.cap, "link_ok", True)):
            return ("stopping - not connected, so nothing is sent to the "
                    "charger; ending data collection ...")
        return ("stopping - this can take several seconds while the slots "
                "are stopped ...")

    def on_stop_clicked(self):
        if self._stopping:
            return
        if self.controller.is_running and not self.confirm_stop():
            return
        self._stopping = True
        try:
            self.stop_button.setEnabled(False)
            self.controller.note("Stop pressed: ending the session")
            self.status_label.setText(self._stopping_text())
            QApplication.processEvents()
            self.controller.stop(pump=QApplication.processEvents)
        finally:
            self._stopping = False
        if self._close_after_stop:
            # The X was clicked while the stop was being waited out.
            self._close_after_stop = False
            self.close()

    # --- theme ---

    def on_settings_clicked(self):
        dialog = SettingsDialog(self, self.theme_choice, self.show_console,
                                self.temp_unit,
                                runs_dir=self.controller.runs_dir)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self.set_temp_unit(dialog.temp_unit())
            self.set_runs_dir(dialog.runs_dir())
            choice = dialog.chosen()
            if choice != self.theme_choice:
                self.theme_choice = choice
                gui_theme.save_choice(choice)
                self.apply_theme()
            show = dialog.show_console()
            if show != self.show_console:
                self.show_console = show
                gui_theme.save_show_console(show)
                self.console.setVisible(show)

    def set_runs_dir(self, folder) -> None:
        """Switch the logs folder and persist it. Applies from the NEXT run;
        a run already recording stays where it is. The folder is checked when
        a run opens there - a failure moves that run to the local default
        with a warning on its card."""
        folder = Path(folder)
        if folder == Path(self.controller.runs_dir):
            return
        self.controller.set_outdir(folder)
        runfiles.save_runs_dir(folder, log=self.on_status)
        self.report(f"logs folder is now {folder} (applies from the next "
                    f"run)")
        self._refresh_health()

    def set_temp_unit(self, unit: str):
        """Switch the display unit (files stay in C) and persist it."""
        if unit == self.temp_unit:
            return
        self.temp_unit = unit
        save_temp_unit(unit)
        for card in self.cards.values():
            card.temp_unit = unit
        self.chart.set_unit(unit)
        self._refresh_cards()

    def apply_theme(self):
        """Repaint everything for the current choice."""
        self.mode = gui_theme.apply(QApplication.instance(), self.theme_choice)
        for card in self.cards.values():
            card.apply_theme(self.mode)
        self.chart.apply_theme(self.mode)
        p = gui_theme.PALETTES[self.mode]
        self.all_stop_button.setStyleSheet(
            f"QPushButton {{ color: {p['error']}; font-weight: bold; "
            f"border: 1px solid {p['error']}; border-radius: 6px; "
            f"padding: 5px 14px; background: {p['card']}; }}")
        self._refresh_drift_banner()   # recolor if a theme switch happened
        self._refresh_groups()         # re-theme the group cards to match

    def on_system_theme_changed(self, _scheme=None):
        """
        Windows switched light/dark. Only repaint if we are following it -
        an explicit Light or Dark choice should survive the OS changing.
        """
        if self.theme_choice == gui_theme.SYSTEM:
            self.apply_theme()

    def closeEvent(self, event):
        # The window's X is as final as the Stop button, so it asks the same
        # question first - event.ignore() cancels the close, Qt's equivalent
        # of VBA's QueryClose with Cancel = True.
        #
        # Note this also intercepts app.quit(): in Qt 6, quit() closes the
        # windows first and a vetoed close cancels the quit. Anything that
        # quits programmatically while a capture runs will face this dialog.
        if self._stopping:
            # A stop is already being waited out (this X arrived through its
            # event pump): no second question, no second stop. The window
            # closes when that stop finishes.
            self._close_after_stop = True
            event.ignore()
            return
        if self.controller.is_running and not self.confirm_stop():
            event.ignore()
            return
        # Stop the capture thread before the window goes away, so the CSV file
        # handle is closed properly rather than left to the interpreter exiting.
        # Waits as long as stopping the slots can take (STOP_TIMEOUT_S), with
        # the window kept alive, so meta.json is finished before the process
        # ends.
        self._stopping = True
        try:
            if self.controller.is_running:
                self.status_label.setText(
                    "closing - saving, a few seconds ..."
                    if not getattr(self.controller.cap, "link_ok", True) else
                    "closing - stopping the slots and saving, a few seconds ...")
                self.stop_button.setEnabled(False)
                QApplication.processEvents()
            self.controller.stop(pump=QApplication.processEvents)
        finally:
            self._stopping = False
            self._close_after_stop = False
        super().closeEvent(event)


# --------------------------------------------------------------------------

USE_DOCUMENTS_LABEL = "Use Documents"
QUIT_LABEL = "Quit SITREP91"


def documents_choice_box(text: str) -> tuple[QMessageBox, QPushButton, QPushButton]:
    """
    The "Can't write here" question, with each button saying what it does
    (a plain Yes/No left "No" to be guessed at). Use Documents is the default;
    Esc and the window's X mean Quit, so closing the box can never quietly
    pick Documents. Returns (box, use button, quit button).
    """
    box = QMessageBox(QMessageBox.Icon.Question, "Can't write here", text,
                      QMessageBox.StandardButton.NoButton)
    use = box.addButton(USE_DOCUMENTS_LABEL, QMessageBox.ButtonRole.AcceptRole)
    quit_ = box.addButton(QUIT_LABEL, QMessageBox.ButtonRole.RejectRole)
    box.setDefaultButton(use)
    box.setEscapeButton(quit_)
    return box, use, quit_


def startup_storage(demo: bool = False, ask=None) -> tuple[Path, Path | None] | None:
    """
    The startup guards of the portable layout, then where runs go: (logs folder, fallback folder), or None to quit.

    - Running from inside the zip (Windows unpacks a double-clicked zip to a
      throwaway folder under %TEMP%): refuse, with "extract the zip first" -
      anything recorded there would silently vanish.
    - The app folder not writable (Program Files, a read-only share): say so
      plainly and offer Documents\\SITREP91 instead.
    Otherwise the logs folder is the one in settings.ini (runs\\ by default),
    and the fallback for a run whose folder fails mid-run is the local
    default. The demo writes to demo_runs\\ and never to runs\\.

    `ask(kind, text) -> bool` replaces the message boxes (tests); kind is
    "zip" (a notice; the return value is ignored) or "documents" (a question).
    """
    if ask is None:
        def ask(kind, text):
            if kind == "zip":
                QMessageBox.critical(None, "Extract the zip first", text)
                return False
            box, use, _quit = documents_choice_box(text)
            box.exec()
            return box.clickedButton() is use

    if runfiles.inside_temp_dir():
        ask("zip", runfiles.INSIDE_ZIP_MESSAGE)
        return None

    problem = runfiles.writable_problem(runfiles.HOME_DIR)
    if problem:
        docs = runfiles.documents_fallback_dir()
        if not ask("documents",
                   f"{problem}\n\nMove the SITREP91 folder somewhere you can "
                   f"write to, such as your Desktop or Documents (not Program "
                   f"Files), and run it from there.\n\nOr record runs in "
                   f"{docs} for now? (Settings cannot be saved while the "
                   f"program's own folder is read-only.)"):
            return None
        return docs, docs

    runfiles.ensure_home_folders()
    if demo:
        # Never runs\ - synthetic data stays separate.
        demo_dir = runfiles.HOME_DIR / "demo_runs"
        return demo_dir, demo_dir
    return runfiles.configured_runs_dir(), runfiles.default_runs_dir()


class _Parser(argparse.ArgumentParser):
    """Without a console, argparse's complaint goes nowhere and the program
    just exits. Say it in a box and the crash log instead."""

    def error(self, message):
        crashlog.fail("SITREP91 was started with options it does not "
                      "understand", f"{message}\n\n{self.format_usage()}")
        sys.exit(2)


def main():
    ap = _Parser(description="SITREP91: a standalone telemetry and control "
                             "app for the SkyRC MC5000")
    ap.add_argument("--slots", default="1,2,3,4",
                    help="comma separated, e.g. 1 or 1,2. Default: all four.")
    ap.add_argument("--demo", action="store_true",
                    help="replay synthetic frames instead of using a charger")
    ap.add_argument("--name", default=None, help="run label")
    ap.add_argument("--cells", default=None,
                    help="which cell is in which slot, as SLOT=CELL_ID pairs "
                         "(e.g. 1=ENL-A-01,2=ENL-A-02). Recorded in meta.json; "
                         "a run captured without it can never be linked back "
                         "to the physical cell afterward.")
    ap.add_argument("--max-hours", type=float, default=DEFAULT_MAX_HOURS,
                    help="stop RECORDING after this long. Default: no limit - "
                         "the app is meant to stay open. The limit never "
                         "stops a run: one still going is left going on the "
                         "charger under its own safety timer.")
    ap.add_argument("--chemistry", default="liion",
                    choices=sorted(CHEMISTRY_BY_NAME))
    args = ap.parse_args()

    slots = [int(s) for s in args.slots.split(",")]
    try:
        cells = parse_cells(args.cells, slots)
    except ValueError as e:
        crashlog.fail("The --cells option is not valid", str(e))
        sys.exit(2)

    if args.demo:
        # Interactive: every slot starts idle and the demo runs until stopped,
        # so Configure and Start can be tried. Nothing is real (see
        # DemoSource.simulated): no scan, no connection, no packet.
        source = DemoSource(slots, auto_run=False)
        name = args.name or "gui_demo"
    else:
        # An attach: watch, start nothing, disturb nothing. A run is started
        # from a slot card's Configure and Start, never from the command line.
        opts = SessionOptions(max_hours=args.max_hours)
        source = BleSource(opts)
        name = args.name or "gui_capture"

    # One SITREP91 per computer for a real session; the demo never touches
    # Bluetooth, so it may run beside one.
    if not args.demo and not acquire_single_instance():
        crashlog.notice(ALREADY_OPEN_TITLE, ALREADY_OPEN_TEXT)
        sys.exit(3)

    app = QApplication(sys.argv)

    theme_choice = gui_theme.load_choice()
    gui_theme.apply(app, theme_choice)

    storage = startup_storage(demo=args.demo)
    if storage is None:
        sys.exit(1)
    outdir, fallback_dir = storage

    bridge = SampleBridge()
    controller = CaptureController(bridge, source, slots, name, outdir,
                                   chemistry=CHEMISTRY_BY_NAME[args.chemistry],
                                   cells=cells, fallback_dir=fallback_dir)
    window = MainWindow(controller, slots, theme_choice)

    # Follow the OS live, but only while the choice is "system".
    hints = app.styleHints()
    if hasattr(hints, "colorSchemeChanged"):
        hints.colorSchemeChanged.connect(window.on_system_theme_changed)

    # Queued automatically, because bridge emits from the capture thread while
    # window lives on the GUI thread.
    bridge.sample.connect(window.on_sample, Qt.ConnectionType.QueuedConnection)
    bridge.status.connect(window.on_status, Qt.ConnectionType.QueuedConnection)
    bridge.stopped.connect(window.on_stopped, Qt.ConnectionType.QueuedConnection)

    # Ctrl-C in the terminal should behave like closing the window. Two
    # pieces are needed: routing SIGINT into close(), and a heartbeat timer -
    # once app.exec() starts, Qt's C++ loop owns the thread and Python never
    # gets a turn to run the signal handler unless something periodically
    # yields back to the interpreter. Without the timer, Ctrl-C is silently
    # ignored.
    signal.signal(signal.SIGINT, lambda *_: window.close())
    heartbeat = QTimer()
    heartbeat.timeout.connect(lambda: None)
    heartbeat.start(250)

    window.show()
    crashlog.mark_started()
    controller.start()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
